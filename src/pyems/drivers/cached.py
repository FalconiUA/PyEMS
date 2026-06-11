"""
CachedDriver = non-blocking I/O layer with a tag cache.

Problem: Scheduler.run() calls driver.read_state()/write_setpoints() inside the
scan cycle. A slow or hung Modbus transaction blocks the whole control loop —
the single biggest source of jitter (esp. on a Raspberry Pi field bus).

Solution: run the real Modbus I/O in a background thread against a private
"I/O state", and publish the latest values into an in-memory tag cache. The
foreground scan cycle only copies values to/from that cache (microseconds,
never touches the bus). The cycle period is decoupled from bus latency.

This wraps any Driver (e.g. CompositeDriver) and IS a Driver itself, so the
Scheduler and controllers are unchanged — they still see one Driver.

Data freshness: age_s() reports how long since the last successful read. A
safety controller can read it and fail-safe if the cache goes stale (bus down),
instead of silently acting on old measurements.

Concurrency: locks are held only for tiny dict copies; the slow Modbus calls
run OUTSIDE the lock, so the foreground cycle never waits on the bus.
"""
from __future__ import annotations

import logging
import threading
import time

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver
# System diagnostic tag (IEC system status word, not a device register):
# seconds since the last successful bus read. Safety logic reads it to detect a
# dead bus and fail-safe. inf until the first successful read.
# All EMS-internal names come from the single registry, pyems/system_tags.py.
from pyems.system_tags import COMMS_AGE_CHANNEL

logger = logging.getLogger(__name__)


class CachedDriver(Driver):
    def __init__(
        self,
        inner: Driver,
        poll_interval_s: float = 0.5,
        setpoint_rewrite_s: float = 10.0,
    ) -> None:
        """`setpoint_rewrite_s`: keep-alive period for UNCHANGED setpoints.

        Changed setpoints flush on the next poll; unchanged ones are re-written
        only every `setpoint_rewrite_s` — often enough to feed a device-side
        comms watchdog, without hammering every writable register each poll.
        """
        self._inner = inner
        self._device_channels: list[Channel] = inner.channels()
        self._age_channel = Channel(
            name=COMMS_AGE_CHANNEL, unit="s", value=float("inf")
        )
        self._channels: list[Channel] = self._device_channels + [self._age_channel]
        # I/O sets are device channels only — the age tag is set locally, not polled.
        self._writable = [c.name for c in self._device_channels if c.writable]
        self._measured = [c.name for c in self._device_channels if not c.writable]
        self._poll = poll_interval_s

        # worker's private state for performing real Modbus transactions
        self._io_state = SystemState(self._device_channels)

        self._lock = threading.Lock()
        self._meas_cache: dict[str, float] = {n: 0.0 for n in self._measured}
        self._sp_cache: dict[str, float] = {}          # setpoints published by controllers
        self._sp_dirty: set[str] = set()                # changed since last good flush
        self._sp_flushed_at: dict[str, float] = {}      # monotonic ts of last good flush
        self._rewrite_s = setpoint_rewrite_s
        self._sp_ready = False                          # gate: don't write before first setpoint
        self._last_ok = 0.0                             # monotonic ts of last good read
        self._bus_down = False                          # last bus health — log on transition
        self._write_failed = False                      # last flush health — log on transition
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="modbus-io", daemon=True)

    # ── Driver interface (foreground: fast, no bus access) ───────────────────
    def connect(self) -> None:
        self._inner.connect()
        self._thread.start()
        logger.info("CachedDriver started: %d channels, poll %.2fs", len(self._channels), self._poll)

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:  # join only a started thread —
            # disconnect() must stay safe on a driver that never connected
            # (e.g. teardown after a failed startup).
            self._thread.join(timeout=2 * self._poll + 1.0)
        self._inner.disconnect()
        logger.info("CachedDriver stopped")

    def channels(self) -> list[Channel]:
        return self._channels

    def read_state(self, state: SystemState) -> None:
        """Copy cached measurements + comms-age tag → live state. No Modbus here.

        Raises RuntimeError if the background I/O worker died: a dead worker
        means measurements freeze AND a safety trip could never be flushed to
        the device, so the only safe recovery is to crash the process and let
        the supervisor (systemd Restart=on-failure) start a fresh one — the
        device's own comms watchdog covers the gap.
        """
        if (
            self._thread.ident is not None  # was started
            and not self._stop.is_set()     # not a clean shutdown
            and not self._thread.is_alive()
        ):
            raise RuntimeError(
                "modbus-io worker thread died; restarting the process is the "
                "only safe recovery (setpoints can no longer reach the bus)"
            )
        with self._lock:
            cache = dict(self._meas_cache)
        for name, value in cache.items():
            state._channels[name].value = value
        if COMMS_AGE_CHANNEL in state._channels:
            state._channels[COMMS_AGE_CHANNEL].value = self.age_s()

    def write_setpoints(self, state: SystemState, channels: set[str] | None = None) -> None:
        """Publish live setpoints → cache for the worker to flush. No Modbus here.

        Only values that actually changed are marked dirty; unchanged ones are
        re-flushed by the worker on the keep-alive period (`setpoint_rewrite_s`).
        """
        names = (
            self._writable
            if channels is None
            else [n for n in self._writable if n in channels]
        )
        sp = {name: state._channels[name].value for name in names}
        with self._lock:
            for name, value in sp.items():
                if self._sp_cache.get(name) != value:
                    self._sp_cache[name] = value
                    self._sp_dirty.add(name)
            self._sp_ready = True

    # ── freshness signal for safety logic ────────────────────────────────────
    def age_s(self) -> float:
        """Seconds since the last successful read; grows while the bus is down."""
        with self._lock:
            last = self._last_ok
        return float("inf") if last == 0.0 else time.monotonic() - last

    # ── background worker (slow: owns the bus) ───────────────────────────────
    def _loop(self) -> None:
        # Per-poll bus errors are handled inside _poll_once; anything escaping
        # to here is a worker bug. Log it CRITICAL — read_state() then raises in
        # the foreground, crashing the process for a supervised restart.
        try:
            self._poll_loop()
        except Exception:
            logger.critical(
                "modbus-io worker crashed; measurements frozen and setpoints "
                "no longer flushed — control loop will abort", exc_info=True,
            )
            raise

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()

            # READ: slow Modbus into private io_state, then publish under lock
            try:
                if self._bus_down:
                    # SmartLogger / TCP gateways drop idle or overloaded sessions.
                    # Drop the old sockets first: pymodbus connect() is a no-op
                    # while a (possibly half-dead) socket object still exists,
                    # and an aborted connection escapes recv() without close()
                    # — without this the EMS would retry the zombie socket
                    # forever and a safety trip would never release.
                    self._inner.disconnect()
                    self._inner.connect()
                self._inner.read_state(self._io_state)
                snap = {n: self._io_state.get(n) for n in self._measured}
                with self._lock:
                    self._meas_cache.update(snap)
                    self._last_ok = time.monotonic()
                if self._bus_down:  # recovered — log the up transition once
                    logger.warning("Modbus bus RECOVERED after failure")
                    self._bus_down = False
            except Exception:
                # keep last cached values; age_s() grows so safety can react.
                # Log only the down transition — never every failed poll (spam).
                if not self._bus_down:
                    logger.exception("Modbus READ failed; serving stale cache, comms age growing")
                    self._bus_down = True

            # WRITE: flush setpoints that changed (dirty) or are due a keep-alive
            # rewrite. A failed flush keeps channels dirty/due, so it retries.
            if self._sp_ready:
                now = time.monotonic()
                with self._lock:
                    due = {
                        n
                        for n in self._sp_cache
                        if n in self._sp_dirty
                        or now - self._sp_flushed_at.get(n, float("-inf")) >= self._rewrite_s
                    }
                    pending = {n: self._sp_cache[n] for n in due}
                if pending:
                    for name, value in pending.items():
                        self._io_state._channels[name].value = value
                    try:
                        self._inner.write_setpoints(self._io_state, channels=due)
                        flushed = time.monotonic()
                        with self._lock:
                            for name, value in pending.items():
                                self._sp_flushed_at[name] = flushed
                                if self._sp_cache.get(name) == value:  # not updated mid-flush
                                    self._sp_dirty.discard(name)
                        if self._write_failed:  # recovered — log the up transition once
                            logger.warning("Modbus WRITE recovered; setpoints flushing again")
                            self._write_failed = False
                    except Exception:
                        # Log only the down transition — never every failed flush (spam).
                        if not self._write_failed:
                            logger.exception("Modbus WRITE failed; setpoints not flushed")
                            self._write_failed = True

            self._stop.wait(max(0.0, self._poll - (time.monotonic() - t0)))
