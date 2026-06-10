"""
Generic, profile-driven Modbus device driver.

Register maps live in YAML profile files (data), NOT in code. One profile per
device model. Adding a new device = add a YAML, zero code changes.

This mirrors how SunSpec / OpenEMS / Elum eConf treat device definitions:
the I/O mapping is configuration of a resource, not a program.
"""
from __future__ import annotations

import logging
import inspect
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from pymodbus.client import ModbusSerialClient, ModbusTcpClient

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver

logger = logging.getLogger(__name__)

# registers per Modbus data type
_REG_COUNT = {"int16": 1, "uint16": 1, "int32": 2, "uint32": 2}

# RTU defaults applied when site.yaml gives no `serial:` mapping. 9600 8N1 is
# the most common Modbus RTU factory setting; override per device in site.yaml.
DEFAULT_SERIAL = {"baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1}
_SERIAL_KEYS = frozenset(DEFAULT_SERIAL)


class ModbusReadError(IOError):
    """One or more register reads returned a Modbus error response, or a
    decoded value outside the register's plausibility bounds.

    Raised AFTER all registers were attempted (good registers still update),
    so CachedDriver treats the poll as failed and the comms age keeps growing —
    a gateway answering every request with an exception code (e.g. SmartLogger
    0x0B for an offline inverter) must not count as fresh data.
    """


class ModbusWriteError(IOError):
    """One or more setpoint writes returned a Modbus error response.

    A rejected write (illegal value, remote control not enabled on the device)
    must surface — silently assuming the command landed is unacceptable when
    driving real units.
    """


def make_client(
    protocol: str,
    host: str,
    port: int | None = None,
    default_port: int = 502,
    serial: dict | None = None,
    timeout_s: float | None = None,
    retries: int | None = None,
):
    """Build a pymodbus client for one bus endpoint.

    For `modbus_tcp`, `host`/`port` are the TCP endpoint. For `modbus_rtu`,
    `host` is the serial port path and `serial` overrides DEFAULT_SERIAL
    (keys: baudrate, bytesize, parity, stopbits). `timeout_s` and `retries`
    (per-transaction) apply to both protocols; left to pymodbus defaults
    when None.
    """
    common: dict = {}
    if timeout_s is not None:
        common["timeout"] = timeout_s
    if retries is not None:
        common["retries"] = retries
    if protocol == "modbus_tcp":
        return ModbusTcpClient(host, port=port or default_port, **common)
    if protocol == "modbus_rtu":
        unknown = set(serial or {}) - _SERIAL_KEYS
        if unknown:
            raise ValueError(
                f"unknown serial option(s) {sorted(unknown)} for {host}; "
                f"allowed: {sorted(_SERIAL_KEYS)}"
            )
        return ModbusSerialClient(port=host, **{**DEFAULT_SERIAL, **(serial or {}), **common})
    raise ValueError(f"Unknown protocol: {protocol}")


def namespaced(local: str, prefix: str | None) -> str:
    """Apply a device instance prefix to a profile-local channel name.

    Profiles name channels '<class>.<field>' (e.g. 'pv.W'). The site assigns
    each device an instance id; that id replaces the class segment so two
    identical devices get distinct tags ('pv1.W', 'pv2.W'). With no prefix the
    profile name is kept verbatim (single-device sites need no id).
    """
    if not prefix:
        return local
    _head, dot, tail = local.partition(".")
    return f"{prefix}.{tail}" if dot else f"{prefix}.{local}"


@dataclass
class RegisterDef:
    channel: str
    address: int
    type: str
    scale: float
    unit: str
    access: str  # "read" | "read_write"
    min_val: float = float("-inf")
    max_val: float = float("inf")

    def __post_init__(self) -> None:
        # Fail at profile load, not mid-poll: an unknown type would KeyError on
        # the first bus read and masquerade as a comms failure (permanent
        # safety trip); a zero scale would ZeroDivisionError on the first
        # setpoint write; a misspelled access silently demotes a setpoint to
        # read-only.
        if self.type not in _REG_COUNT:
            raise ValueError(
                f"register '{self.channel}': unknown type {self.type!r}; "
                f"supported: {sorted(_REG_COUNT)}"
            )
        if self.access not in ("read", "read_write"):
            raise ValueError(
                f"register '{self.channel}': access must be 'read' or "
                f"'read_write', got {self.access!r}"
            )
        if self.scale == 0:
            raise ValueError(f"register '{self.channel}': scale must be non-zero")
        if self.min_val > self.max_val:
            raise ValueError(
                f"register '{self.channel}': min_val ({self.min_val}) > "
                f"max_val ({self.max_val})"
            )

    @property
    def count(self) -> int:
        return _REG_COUNT[self.type]

    @property
    def signed(self) -> bool:
        return self.type.startswith("int")

    @property
    def writable(self) -> bool:
        return self.access == "read_write"


@dataclass
class DeviceProfile:
    model: str
    protocol: str
    default_port: int
    registers: list[RegisterDef]

    @classmethod
    def load(cls, path: str | Path) -> DeviceProfile:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        regs = [RegisterDef(**r) for r in data["registers"]]
        return cls(
            model=data["model"],
            protocol=data["protocol"],
            default_port=data.get("default_port", 502),
            registers=regs,
        )

    def channels(self) -> list[Channel]:
        """Derive SystemState channels directly from the profile."""
        return [
            Channel(
                name=r.channel,
                unit=r.unit,
                min_val=r.min_val,
                max_val=r.max_val,
                writable=r.writable,
            )
            for r in self.registers
        ]


def _decode(raw_regs: list[int], reg: RegisterDef) -> int:
    if reg.count == 2:
        value = (raw_regs[0] << 16) | raw_regs[1]
        bits = 32
    else:
        value = raw_regs[0]
        bits = 16
    if reg.signed and value >= (1 << (bits - 1)):
        value -= 1 << bits
    return value


def _encode(value: int, reg: RegisterDef) -> list[int]:
    if reg.count == 2:
        v = value & 0xFFFFFFFF
        return [(v >> 16) & 0xFFFF, v & 0xFFFF]
    return [value & 0xFFFF]


def _resolve_unit_kw(func) -> str | None:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return "device_id"
    for name in ("device_id", "slave", "unit"):
        if name in params:
            return name
    return None


# Signature introspection is far too slow to repeat per register per poll
# cycle; the answer is a constant per client method, so cache it keyed by the
# underlying function (shared across bound methods of all client instances).
_unit_kw_cache: dict[object, str | None] = {}


def _modbus_unit_kw(method) -> str | None:
    """Return the unit/slave keyword accepted by this pymodbus client version."""
    func = getattr(method, "__func__", method)
    try:
        return _unit_kw_cache[func]
    except KeyError:
        kw = _unit_kw_cache[func] = _resolve_unit_kw(func)
        return kw
    except TypeError:  # unhashable callable; compute without caching
        return _resolve_unit_kw(func)


def _read_holding_registers(client, address: int, count: int, slave_id: int):
    method = client.read_holding_registers
    unit_kw = _modbus_unit_kw(method)
    if unit_kw is None:
        return method(address, count=count)
    return method(address, count=count, **{unit_kw: slave_id})


def _write_registers(client, address: int, values: list[int], slave_id: int):
    method = client.write_registers
    unit_kw = _modbus_unit_kw(method)
    if unit_kw is None:
        return method(address, values)
    return method(address, values, **{unit_kw: slave_id})


class ModbusDeviceDriver(Driver):
    def __init__(
        self, profile: DeviceProfile, client, slave_id: int = 1, prefix: str | None = None
    ) -> None:
        self._profile = profile
        self._client = client
        self._slave = slave_id
        self._prefix = prefix
        # profile-local channel name → namespaced state tag (see namespaced()).
        self._tag = {r.channel: namespaced(r.channel, prefix) for r in profile.registers}
        self._writable_regs = [r for r in profile.registers if r.writable]

    @classmethod
    def from_profile(
        cls,
        profile_path: str | Path,
        host: str,
        port: int | None = None,
        slave_id: int = 1,
        prefix: str | None = None,
        client=None,
        serial: dict | None = None,
        timeout_s: float | None = None,
        retries: int | None = None,
    ) -> ModbusDeviceDriver:
        profile = DeviceProfile.load(profile_path)
        if client is None:
            client = make_client(
                profile.protocol,
                host,
                port=port,
                default_port=profile.default_port,
                serial=serial,
                timeout_s=timeout_s,
                retries=retries,
            )
        return cls(profile, client, slave_id, prefix)

    def connection_identity(self) -> object:
        """Identity used by CompositeDriver to connect a shared client once."""
        return self._client

    def connect(self) -> None:
        ok = self._client.connect()
        logger.info(
            "Connecting %s (%s, slave %d, prefix %r): %s",
            self._profile.model, self._profile.protocol, self._slave, self._prefix,
            "ok" if ok else "FAILED",
        )

    def disconnect(self) -> None:
        self._client.close()

    def channels(self) -> list[Channel]:
        return [
            replace(ch, name=self._tag[ch.name]) for ch in self._profile.channels()
        ]

    def read_state(self, state: SystemState) -> None:
        """Read every register; raise ModbusReadError if any read failed.

        All registers are attempted first (good values still land in `state`),
        the error is raised at the end. The caller (CachedDriver) must treat
        the poll as failed so the comms age grows — error *responses* are as
        much a dead bus as a socket error.

        A decoded MEASUREMENT outside the register's [min_val, max_val] bounds
        is treated the same as an error response: the tag keeps its last value
        and the poll fails. A wrong profile (scale/address/type) or a gateway
        serving garbage must raise the comms age, not feed implausible numbers
        into the control loop as if they were measurements. Writable registers
        are exempt: their bounds guard what WE write, while the device-side
        content (e.g. an out-of-range factory default in a setpoint register
        we have not written yet) must not fail the poll.
        """
        failed: list[str] = []
        for reg in self._profile.registers:
            tag = self._tag[reg.channel]
            result = _read_holding_registers(self._client, reg.address, reg.count, self._slave)
            if result.isError():
                logger.debug("Read error %s @%d (%s)", reg.channel, reg.address, result)
                failed.append(tag)
                continue
            value = _decode(result.registers, reg) * reg.scale
            if not reg.writable and not (reg.min_val <= value <= reg.max_val):
                logger.debug(
                    "Implausible %s @%d: %s outside [%s, %s]",
                    reg.channel, reg.address, value, reg.min_val, reg.max_val,
                )
                failed.append(tag)
                continue
            state._channels[tag].value = value
        if failed:
            raise ModbusReadError(
                f"{self._profile.model} (slave {self._slave}): "
                f"{len(failed)}/{len(self._profile.registers)} register reads "
                f"returned errors or implausible values: {failed}"
            )

    def write_setpoints(self, state: SystemState, channels: set[str] | None = None) -> None:
        """Write writable registers; raise ModbusWriteError on rejection.

        `channels` restricts the write to that subset of tags (None = all
        writable registers) — CachedDriver uses it to flush only changed or
        keep-alive-due setpoints instead of rewriting everything each poll.
        All due registers are attempted before raising, so one rejected
        setpoint does not block the others.
        """
        failed: list[str] = []
        for reg in self._writable_regs:
            tag = self._tag[reg.channel]
            if channels is not None and tag not in channels:
                continue
            raw = int(state.get(tag) / reg.scale)
            result = _write_registers(self._client, reg.address, _encode(raw, reg), self._slave)
            if result is not None and result.isError():
                logger.debug("Write error %s @%d (%s)", reg.channel, reg.address, result)
                failed.append(tag)
        if failed:
            raise ModbusWriteError(
                f"{self._profile.model} (slave {self._slave}): "
                f"setpoint writes rejected: {failed}"
            )
