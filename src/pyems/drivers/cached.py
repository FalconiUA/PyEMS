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
from pyems.drivers.composite import CompositeReadError
# System diagnostic tags (IEC system status words, not device registers), both
# inf until their first success. Safety logic reads them to fail-safe:
#   COMMS_AGE_CHANNEL — seconds since the last successful bus READ (dead bus);
#   WRITE_AGE_CHANNEL — seconds since the last successful setpoint FLUSH. Reads
#     can keep succeeding while writes fail (remote control lost, half-open
#     socket), so a fresh comms age is NOT proof the EMS can still actuate.
# When the inner driver exposes per-device channels, each device also gets a
# `sys.<id>.comms_age_s` tag (comms_age_channel) so one dead device no longer
# ages the whole site. All EMS-internal names come from pyems/system_tags.py.
from pyems.system_tags import (
    COMMS_AGE_CHANNEL,
    WRITE_AGE_CHANNEL,
    comms_age_channel,
)

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
        self._write_age_channel = Channel(
            name=WRITE_AGE_CHANNEL, unit="s", value=float("inf")
        )
        self._channels: list[Channel] = self._device_channels + [
            self._age_channel, self._write_age_channel,
        ]
        # I/O sets are device channels only — the age tag is set locally, not polled.
        # Command channels (discrete remote start/stop) are writable but NOT part
        # of the continuous setpoint mirror: they must never be flushed on startup
        # or re-asserted by the keep-alive (would spam a one-shot command). They
        # are written ONLY via send_command(), one forced write per call.
        self._command = {
            c.name for c in self._device_channels if c.writable and c.command
        }
        self._writable = [
            c.name for c in self._device_channels if c.writable and c.name not in self._command
        ]
        self._measured = [c.name for c in self._device_channels if not c.writable]
        self._poll = poll_interval_s

        # Per-device freshness (opt-in): if the inner driver maps device id ->
        # its channels, age each device on its own and publish sys.<id>.comms_age_s.
        # None keeps the single-global-age behavior unchanged.
        get_map = getattr(inner, "device_channel_map", None)
        device_map = get_map() if callable(get_map) else None
        measured_set = set(self._measured)
        if device_map is None:
            self._dev_measured: dict[str, list[str]] | None = None
        else:
            self._dev_measured = {
                dev_id: [n for n in names if n in measured_set]
                for dev_id, names in device_map.items()
            }
            self._channels += [
                Channel(name=comms_age_channel(dev_id), unit="s", value=float("inf"))
                for dev_id in self._dev_measured
            ]

        # worker's private state for performing real Modbus transactions
        self._io_state = SystemState(self._device_channels)

        self._lock = threading.Lock()
        self._meas_cache: dict[str, float] = {n: 0.0 for n in self._measured}
        # monotonic ts of each device's last good read (0.0 = never → inf age)
        self._last_ok_dev: dict[str, float] = (
            {dev_id: 0.0 for dev_id in self._dev_measured}
            if self._dev_measured is not None
            else {}
        )
        self._sp_cache: dict[str, float] = {}          # setpoints published by controllers
        self._sp_dirty: set[str] = set()                # changed since last good flush
        self._sp_flushed_at: dict[str, float] = {}      # monotonic ts of last good flush
        self._rewrite_s = setpoint_rewrite_s
        self._sp_ready = False                          # gate: don't write before first setpoint
        # One-shot command queue (remote start/stop): each send_command() enqueues
        # one forced write the worker drains on its next pass. Not a level mirror —
        # a command is performed exactly once per call (works for pulse or level).
        self._cmd_queue: list[tuple[str, float]] = []
        self._last_ok = 0.0                             # monotonic ts of last good read
        self._last_write_ok = 0.0                       # monotonic ts of last good flush
        self._bus_down = False                          # last bus health — log on transition
        self._write_failed = False                      # last flush health — log on transition
        self._cmd_failed = False                        # last command-write health — log on transition
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
            state.apply_driver_value(name, value)
        if COMMS_AGE_CHANNEL in state:
            state.apply_driver_value(COMMS_AGE_CHANNEL, self.age_s())
        if WRITE_AGE_CHANNEL in state:
            state.apply_driver_value(WRITE_AGE_CHANNEL, self.write_age_s())
        if self._dev_measured is not None:
            for dev_id in self._dev_measured:
                tag = comms_age_channel(dev_id)
                if tag in state:
                    state.apply_driver_value(tag, self.device_age_s(dev_id))

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
        sp = {name: state.get(name) for name in names}
        with self._lock:
            for name, value in sp.items():
                if self._sp_cache.get(name) != value:
                    self._sp_cache[name] = value
                    self._sp_dirty.add(name)
            self._sp_ready = True

    def send_command(self, tag: str, value: float) -> None:
        """Enqueue ONE forced write of a command register (remote start/stop).

        Unlike a setpoint, a command is not a level the EMS keeps mirroring: it
        is performed exactly once per call and never re-asserted by the
        keep-alive. Posting against a non-command channel is a programming error.
        """
        if tag not in self._command:
            raise ValueError(
                f"send_command against '{tag}' which is not a command channel; "
                f"command channels: {sorted(self._command)}"
            )
        with self._lock:
            self._cmd_queue.append((tag, value))

    # ── freshness signals for safety logic ───────────────────────────────────
    @staticmethod
    def _age_of(last: float, now: float) -> float:
        return float("inf") if last == 0.0 else now - last

    def age_s(self) -> float:
        """Seconds since the last successful read; grows while the bus is down.

        In per-device mode this is the MAX over devices (age of the oldest
        device), so the single global tag stays conservative-compatible: it
        equals the age of the last fully-successful poll when devices are
        healthy or fail together, and grows whenever ANY device is stale.
        """
        now = time.monotonic()
        with self._lock:
            if self._dev_measured is None:
                return self._age_of(self._last_ok, now)
            stamps = list(self._last_ok_dev.values())
        if not stamps:
            return self._age_of(self._last_ok, now)
        return max(self._age_of(s, now) for s in stamps)

    def device_age_s(self, device_id: str) -> float:
        """Seconds since `device_id`'s last good read; inf until its first."""
        with self._lock:
            last = self._last_ok_dev[device_id]
        return self._age_of(last, time.monotonic())

    def write_age_s(self) -> float:
        """Seconds since the last successful setpoint flush; inf until the first.

        Grows while writes fail even though reads keep the comms age fresh, so
        safety can react to a write-blind EMS (remote control disabled, half-open
        socket), not just a dead bus. The keep-alive rewrite bounds it at roughly
        `setpoint_rewrite_s + poll` during healthy operation.
        """
        with self._lock:
            last = self._last_write_ok
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
            # WRITE before READ: a slow or hung device on the bus must never
            # delay flushing a (possibly safety-critical) setpoint to a HEALTHY
            # device behind a blocking read. The reconnect cascade lives in the
            # read step, so it can never push back the flush either. Healthy-bus
            # latency is unchanged: a setpoint published this cycle still flushes
            # within one poll, just at the top of the next iteration.
            self._flush_commands()
            self._flush_setpoints()
            self._read_once()
            self._stop.wait(max(0.0, self._poll - (time.monotonic() - t0)))

    def _read_once(self) -> None:
        """Read the inner driver into io_state and publish under the lock.

        Two modes: a single global age (legacy) reconnects the whole bus on a
        failure; per-device mode keeps healthy devices fresh and lets the inner
        CompositeDriver reconnect only the endpoints that dropped.
        """
        if self._dev_measured is None:
            self._read_once_global()
        else:
            self._read_once_per_device()

    def _read_once_global(self) -> None:
        """Single-age path: reconnect a dropped session, read, publish.

        Keeps last cached values on failure; age_s() grows so safety can react.
        Logs only the down/up transition — never every failed poll (spam).
        """
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
            if not self._bus_down:
                logger.exception("Modbus READ failed; serving stale cache, comms age growing")
                self._bus_down = True

    def _read_once_per_device(self) -> None:
        """Per-device path: keep healthy devices fresh, age the failed ones alone.

        Partial failures are left to CompositeDriver's per-endpoint reconnect
        path. They publish ONLY the healthy devices' values (a failed device's
        good registers are withheld — one bad register fails its whole device's
        poll, conservative per device) and stamp only the healthy ids; the
        failed devices' ages grow until they read again. If every device fails
        (or an aggregate error escapes), keep the old whole-resource reconnect.
        This also fixes a latent freeze: a partial failure used to publish
        nothing, stalling the healthy devices' cache too.
        """
        try:
            if self._bus_down:
                # Only reached after every device failed (or an unexpected
                # aggregate failure). Keep the old whole-resource reconnect for
                # that case; partial failures are handled by CompositeDriver.
                self._inner.disconnect()
                self._inner.connect()
            self._inner.read_state(self._io_state)
            failed_ids: frozenset[str] = frozenset()
        except CompositeReadError as exc:
            failed_ids = exc.failed_device_ids
        except Exception:
            # Unexpected non-composite error: treat every device as failed
            # (ages grow → safety trips). The inner driver owns reconnection.
            if not self._bus_down:
                logger.exception("Modbus READ failed; serving stale cache, comms age growing")
            failed_ids = frozenset(self._dev_measured)
        now = time.monotonic()
        ok_ids = [d for d in self._dev_measured if d not in failed_ids]
        with self._lock:
            for dev_id in ok_ids:
                for name in self._dev_measured[dev_id]:
                    self._meas_cache[name] = self._io_state.get(name)
                self._last_ok_dev[dev_id] = now
        all_failed = not ok_ids
        if all_failed and not self._bus_down:
            logger.warning("Modbus READ: all %d devices failed", len(self._dev_measured))
            self._bus_down = True
        elif ok_ids and self._bus_down:
            logger.warning("Modbus bus RECOVERED (a device is responding again)")
            self._bus_down = False

    def _flush_commands(self) -> None:
        """Drain the one-shot command queue (remote start/stop): one forced write
        per queued command, in FIFO order. A failed command is re-queued so a
        stop/start is never silently dropped; logged on transition.
        """
        with self._lock:
            pending = self._cmd_queue
            self._cmd_queue = []
        if not pending:
            return
        for tag, value in pending:
            self._io_state.set(tag, value)
        tags = {tag for tag, _ in pending}
        try:
            self._inner.write_setpoints(self._io_state, channels=tags)
            flushed = time.monotonic()
            with self._lock:
                self._last_write_ok = flushed  # a delivered command is a healthy write
            if self._cmd_failed:
                logger.warning("Modbus COMMAND write recovered")
                self._cmd_failed = False
        except Exception:
            if not self._cmd_failed:
                logger.exception(
                    "Modbus COMMAND write failed; remote start/stop not delivered, retrying"
                )
                self._cmd_failed = True
            with self._lock:  # retry on the next pass — a stop must not be lost
                self._cmd_queue[:0] = pending

    def _flush_setpoints(self) -> None:
        """Flush setpoints that changed (dirty) or are due a keep-alive rewrite.

        A failed flush keeps channels dirty/due, so it retries on the next poll.
        Logs only the down/up transition — never every failed flush (spam).
        """
        if not self._sp_ready:
            return
        now = time.monotonic()
        with self._lock:
            due = {
                n
                for n in self._sp_cache
                if n in self._sp_dirty
                or now - self._sp_flushed_at.get(n, float("-inf")) >= self._rewrite_s
            }
            pending = {n: self._sp_cache[n] for n in due}
        if not pending:
            return
        for name, value in pending.items():
            self._io_state.set(name, value)
        try:
            self._inner.write_setpoints(self._io_state, channels=due)
            flushed = time.monotonic()
            with self._lock:
                self._last_write_ok = flushed  # freshness signal for safety
                for name, value in pending.items():
                    self._sp_flushed_at[name] = flushed
                    if self._sp_cache.get(name) == value:  # not updated mid-flush
                        self._sp_dirty.discard(name)
            if self._write_failed:  # recovered — log the up transition once
                logger.warning("Modbus WRITE recovered; setpoints flushing again")
                self._write_failed = False
        except Exception:
            if not self._write_failed:
                logger.exception("Modbus WRITE failed; setpoints not flushed")
                self._write_failed = True
