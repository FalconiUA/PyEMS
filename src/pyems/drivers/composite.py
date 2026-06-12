"""
CompositeDriver = several physical devices presented as one I/O resource.

IEC 61131-3 §2.4.1.1: a RESOURCE may bind I/O from multiple field devices.
The Scheduler still sees a single Driver; this class fans the scan-cycle
read/write across every underlying device driver and merges their channels
into one shared SystemState tag pool.

Channel names are globally unique across devices (grid.*, pv.*, battery.*),
so the merged pool has no collisions — controllers address any device by tag.

Per-device freshness (opt-in): pass `device_ids` and one failing device no
longer fails the whole site. `read_state` then reads each device independently,
keeps the healthy ones fresh, reconnects only the endpoints that dropped, and
raises a `CompositeReadError` naming exactly which devices failed — so the
CachedDriver can age each device on its own (`sys.<id>.comms_age_s`) instead of
tripping the entire plant on one bad register. With `device_ids=None` the
behavior is exactly as before (a single aggregate IOError on any failure).
"""
from __future__ import annotations

import logging

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver
# A device that ANSWERED with an error response / implausible value (vs a dead
# socket) — its endpoint is alive, so it must not trigger a reconnect.
from pyems.drivers.modbus_device import ModbusReadError

logger = logging.getLogger(__name__)

# Transport-level failures: the socket/session is the problem, so the endpoint
# needs a reconnect before its next read. pymodbus raises these; OSError covers
# the generic socket case (and ModbusIOException subclasses it). ModbusReadError
# is deliberately excluded above — the device replied, the link is fine.
try:  # keep the import soft so non-Modbus inner drivers still work
    from pymodbus.exceptions import ConnectionException, ModbusIOException
    _TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
        ConnectionException, ModbusIOException, OSError,
    )
except Exception:  # pragma: no cover - pymodbus always present in practice
    _TRANSPORT_ERRORS = (OSError,)


class CompositeReadError(IOError):
    """One or more devices failed their read in per-device (id) mode.

    Carries the ids of the failed devices so the CachedDriver can keep the
    healthy devices fresh and age only the failed ones. Healthy devices' values
    still land in `state` before this is raised.
    """

    def __init__(self, message: str, failed_device_ids: frozenset[str]) -> None:
        super().__init__(message)
        self.failed_device_ids = failed_device_ids


def _identity(driver: Driver) -> object:
    """Bus endpoint identity used to connect/reconnect a shared client once."""
    return getattr(driver, "connection_identity", lambda: driver)()


class CompositeDriver(Driver):
    def __init__(self, drivers: list[Driver], device_ids: list[str] | None = None) -> None:
        self._drivers = drivers
        if device_ids is not None:
            if len(device_ids) != len(drivers):
                raise ValueError(
                    f"device_ids ({len(device_ids)}) must match drivers "
                    f"({len(drivers)})"
                )
            if len(set(device_ids)) != len(device_ids):
                raise ValueError(f"device_ids must be unique, got {device_ids}")
        self._device_ids = device_ids
        # Endpoint identities currently considered down (transport failure):
        # reconnected before their next read, then cleared on a good read.
        self._down: set[int] = set()
        # Per-device read health, for transition-only logging.
        self._dev_failed: dict[str, bool] = {}
        logger.debug(
            "CompositeDriver wrapping %d device drivers (per-device ids: %s)",
            len(drivers), "yes" if device_ids is not None else "no",
        )

    def connect(self) -> None:
        seen: set[int] = set()
        for d in self._drivers:
            ident_id = id(_identity(d))
            if ident_id in seen:
                continue
            seen.add(ident_id)
            d.connect()

    def disconnect(self) -> None:
        seen: set[int] = set()
        for d in self._drivers:
            ident_id = id(_identity(d))
            if ident_id in seen:
                continue
            seen.add(ident_id)
            d.disconnect()

    def channels(self) -> list[Channel]:
        merged: list[Channel] = []
        seen: set[str] = set()
        for d in self._drivers:
            for ch in d.channels():
                if ch.name in seen:
                    raise ValueError(f"Duplicate channel '{ch.name}' across device profiles")
                seen.add(ch.name)
                merged.append(ch)
        return merged

    def device_channel_map(self) -> dict[str, list[str]] | None:
        """{device id: its channel names}, or None when ids were not supplied.

        Lets the CachedDriver age each device independently. None keeps the
        driver in legacy single-age mode.
        """
        if self._device_ids is None:
            return None
        return {
            dev_id: [c.name for c in d.channels()]
            for dev_id, d in zip(self._device_ids, self._drivers)
        }

    def read_state(self, state: SystemState) -> None:
        """Read every device even if some fail.

        Legacy mode (no device_ids): healthy devices keep their values fresh and
        the aggregate failure is raised as one IOError (conservative — the whole
        poll counts as failed). Per-device mode: same fresh-on-success, but the
        failed devices are named in a CompositeReadError and only their dropped
        endpoints are reconnected (before their next read), so a single bad
        device no longer ages the whole site.
        """
        if self._device_ids is None:
            self._fan_out("read", lambda d: d.read_state(state))
            return
        self._read_per_device(state)

    def _read_per_device(self, state: SystemState) -> None:
        errors: list[Exception] = []
        failed_ids: list[str] = []
        # Snapshot the down set so a failure DURING this poll only schedules a
        # reconnect for the NEXT poll — never a mid-poll bounce of a sibling.
        to_reconnect = set(self._down)
        reconnected: set[int] = set()
        new_down: set[int] = set()
        healthy_idents: set[int] = set()

        for dev_id, d in zip(self._device_ids, self._drivers):
            ident_id = id(_identity(d))
            if ident_id in to_reconnect and ident_id not in reconnected:
                reconnected.add(ident_id)
                self._reconnect(dev_id, d)
            try:
                d.read_state(state)
            except Exception as exc:  # noqa: BLE001 - classified below
                errors.append(exc)
                failed_ids.append(dev_id)
                if isinstance(exc, _TRANSPORT_ERRORS) and not isinstance(exc, ModbusReadError):
                    new_down.add(ident_id)
                self._log_device(dev_id, failed=True, exc=exc)
            else:
                healthy_idents.add(ident_id)
                self._log_device(dev_id, failed=False)

        # A sibling on a shared endpoint reading OK proves the socket is alive,
        # so clearing wins over marking-down for that endpoint.
        self._down |= new_down
        self._down -= healthy_idents

        if errors:
            raise CompositeReadError(
                f"{len(errors)}/{len(self._drivers)} device reads failed: "
                + "; ".join(str(e) for e in errors),
                frozenset(failed_ids),
            )

    def _reconnect(self, dev_id: str, driver: Driver) -> None:
        """Drop and re-establish one device's endpoint before its next read.

        Mirrors the old whole-bus reconnect but scoped to the endpoint that
        dropped, so earlier healthy devices are never delayed. connect() may
        itself fail; the read right after will fail too and re-mark it down.
        """
        try:
            driver.disconnect()
            driver.connect()
        except Exception:
            logger.debug("reconnect of device '%s' endpoint failed", dev_id, exc_info=True)

    def _log_device(self, dev_id: str, failed: bool, exc: Exception | None = None) -> None:
        if failed and not self._dev_failed.get(dev_id):
            logger.warning("device '%s' READ failed: %s", dev_id, exc)
            self._dev_failed[dev_id] = True
        elif not failed and self._dev_failed.get(dev_id):
            logger.warning("device '%s' READ recovered", dev_id)
            self._dev_failed[dev_id] = False

    def write_setpoints(self, state: SystemState, channels: set[str] | None = None) -> None:
        self._fan_out("write", lambda d: d.write_setpoints(state, channels))

    def _fan_out(self, op: str, call) -> None:
        errors: list[Exception] = []
        for d in self._drivers:
            try:
                call(d)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise IOError(
                f"{len(errors)}/{len(self._drivers)} device {op}s failed: "
                + "; ".join(str(e) for e in errors)
            ) from errors[0]
