"""Tests for the non-blocking cache layer (src/drivers/cached.py)."""
import logging
import threading
import time
from pathlib import Path

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver
from pyems.drivers.cached import CachedDriver
from pyems.drivers.composite import CompositeReadError
from pyems.system_tags import COMMS_AGE_CHANNEL, WRITE_AGE_CHANNEL, comms_age_channel
from pyems.drivers.modbus_device import DeviceProfile, ModbusDeviceDriver


class FakeInner(Driver):
    """Inner driver whose 'bus' returns fixed values and signals each read.

    `fail` makes read_state raise, to exercise the stale-cache / age path;
    `fail_writes` makes write_setpoints raise, to exercise flush logging.
    """

    def __init__(self, measurements: dict[str, float]) -> None:
        self._measurements = measurements
        self._channels = [Channel(n) for n in measurements] + [
            Channel("pv.WSet", writable=True, min_val=0, max_val=1e5)
        ]
        self.fail = False
        self.fail_writes = False
        self.connect_calls = 0
        self.read_event = threading.Event()
        self.write_event = threading.Event()
        self.read_gate: threading.Event | None = None  # if set, read blocks on it
        self.written: dict[str, float] = {}
        self.write_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        pass

    def channels(self) -> list[Channel]:
        return self._channels

    def read_state(self, state: SystemState) -> None:
        if self.read_gate is not None:  # simulate a slow/hung bus read
            self.read_gate.wait()
        if self.fail:
            raise OSError("bus down")
        for name, value in self._measurements.items():
            state.apply_driver_value(name, value)
        self.read_event.set()

    def write_setpoints(self, state: SystemState, channels: set[str] | None = None) -> None:
        self.write_calls += 1
        if self.fail_writes:
            self.write_event.set()
            raise OSError("write rejected")
        if channels is None or "pv.WSet" in channels:
            self.written["pv.WSet"] = state.get("pv.WSet")
        self.write_event.set()


class PerDeviceInner(Driver):
    def __init__(self) -> None:
        self.measurements = {"grid.W": 1.0, "pv.W": 10.0}
        self.failed: set[str] = set()
        self.connect_calls = 0
        self.disconnect_calls = 0
        self._channels = [
            Channel("grid.W", unit="W"),
            Channel("pv.W", unit="W"),
            Channel("pv.WSet", unit="W", writable=True),
        ]

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def channels(self) -> list[Channel]:
        return self._channels

    def device_channel_map(self) -> dict[str, list[str]]:
        return {"grid": ["grid.W"], "pv": ["pv.W", "pv.WSet"]}

    def read_state(self, state: SystemState) -> None:
        failed = []
        for dev_id, names in self.device_channel_map().items():
            if dev_id in self.failed:
                failed.append(dev_id)
                continue
            for name in names:
                if name in self.measurements:
                    state.apply_driver_value(name, self.measurements[name])
        if failed:
            raise CompositeReadError(
                f"{len(failed)}/2 device reads failed",
                frozenset(failed),
            )

    def write_setpoints(self, state: SystemState, channels: set[str] | None = None) -> None:
        pass


def test_channels_include_comms_age_tag():
    drv = CachedDriver(FakeInner({"grid.W": 0.0}))
    assert COMMS_AGE_CHANNEL in [c.name for c in drv.channels()]


def test_age_is_inf_before_first_read():
    drv = CachedDriver(FakeInner({"grid.W": 0.0}))
    assert drv.age_s() == float("inf")


def test_channels_include_write_age_tag():
    drv = CachedDriver(FakeInner({"grid.W": 0.0}))
    assert WRITE_AGE_CHANNEL in [c.name for c in drv.channels()]


def test_write_age_is_inf_before_first_flush():
    drv = CachedDriver(FakeInner({"grid.W": 0.0}))
    assert drv.write_age_s() == float("inf")


def test_write_age_finite_after_flush_and_published():
    inner = FakeInner({"grid.W": 1.0})
    drv = CachedDriver(inner, poll_interval_s=0.01)
    drv.connect()
    try:
        st = SystemState(drv.channels())
        st.set("pv.WSet", 5000.0)
        drv.write_setpoints(st)
        deadline = time.monotonic() + 2.0
        while inner.written.get("pv.WSet") != 5000.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert drv.write_age_s() < 2.0  # finite once a flush succeeded
        read = SystemState(drv.channels())
        drv.read_state(read)
        assert read.get(WRITE_AGE_CHANNEL) < 2.0  # published into state
    finally:
        drv.disconnect()


def test_write_age_grows_while_writes_fail():
    """Reads keep succeeding (comms age fresh) but the write age grows — the
    whole point of the signal: a write-blind EMS is not a dead bus."""
    inner = FakeInner({"grid.W": 1.0})
    inner.fail_writes = True
    drv = CachedDriver(inner, poll_interval_s=0.01)
    drv.connect()
    try:
        st = SystemState(drv.channels())
        st.set("pv.WSet", 6000.0)
        drv.write_setpoints(st)
        assert inner.write_event.wait(timeout=2.0)  # at least one failed flush
        time.sleep(0.05)
        assert drv.write_age_s() == float("inf")  # never a successful flush
        assert drv.age_s() < 2.0                   # ...yet reads are fresh
        inner.fail_writes = False                  # writes accepted again
        deadline = time.monotonic() + 2.0
        while drv.write_age_s() == float("inf") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert drv.write_age_s() < 2.0             # recovers once a flush lands
    finally:
        drv.disconnect()


def test_read_state_copies_cache_and_age_no_bus():
    drv = CachedDriver(FakeInner({"grid.W": 0.0}))
    # simulate the worker having published a measurement and a fresh read
    drv._meas_cache["grid.W"] = 123.0
    drv._last_ok = time.monotonic()
    st = SystemState(drv.channels())
    drv.read_state(st)
    assert st.get("grid.W") == 123.0
    assert st.get(COMMS_AGE_CHANNEL) < 1.0  # fresh


def test_write_setpoints_publishes_and_gates():
    drv = CachedDriver(FakeInner({"grid.W": 0.0}))
    st = SystemState(drv.channels())
    st.set("pv.WSet", 4200.0)
    assert drv._sp_ready is False     # no setpoint published yet
    drv.write_setpoints(st)
    assert drv._sp_ready is True
    assert drv._sp_cache["pv.WSet"] == 4200.0


def test_worker_populates_cache_and_flushes_setpoint():
    inner = FakeInner({"grid.W": 777.0})
    drv = CachedDriver(inner, poll_interval_s=0.01)
    drv.connect()
    try:
        assert inner.read_event.wait(timeout=2.0), "worker never polled the bus"
        # publish a setpoint and let the worker flush it
        st = SystemState(drv.channels())
        st.set("pv.WSet", 5000.0)
        drv.write_setpoints(st)
        deadline = time.monotonic() + 2.0
        while inner.written.get("pv.WSet") != 5000.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inner.written.get("pv.WSet") == 5000.0
        # measurement made it into the cache, age is finite
        read = SystemState(drv.channels())
        drv.read_state(read)
        assert read.get("grid.W") == 777.0
        assert drv.age_s() < 2.0
    finally:
        drv.disconnect()


def test_dead_worker_thread_aborts_the_control_loop(monkeypatch):
    """If the modbus-io worker dies, measurements freeze AND a safety trip
    could never be flushed — read_state must raise so the process restarts
    (supervised) instead of silently controlling on a corpse."""
    import pytest

    monkeypatch.setattr(CachedDriver, "_loop", lambda self: None)  # dies at once
    drv = CachedDriver(FakeInner({"grid.W": 0.0}), poll_interval_s=0.01)
    drv.connect()
    try:
        drv._thread.join(timeout=2.0)
        assert not drv._thread.is_alive()
        with pytest.raises(RuntimeError, match="worker thread died"):
            drv.read_state(SystemState(drv.channels()))
    finally:
        drv.disconnect()


def test_clean_shutdown_does_not_flag_dead_worker():
    """After disconnect() the worker exits by request — not a crash."""
    drv = CachedDriver(FakeInner({"grid.W": 0.0}), poll_interval_s=0.01)
    drv.connect()
    drv.disconnect()
    drv.read_state(SystemState(drv.channels()))  # must not raise


def test_disconnect_without_connect_is_safe():
    """Teardown after a failed startup must not crash on the unstarted worker
    (Thread.join before start raises RuntimeError)."""
    drv = CachedDriver(FakeInner({"grid.W": 0.0}), poll_interval_s=0.01)
    drv.disconnect()  # must not raise


def test_stale_bus_grows_age_and_keeps_last_value():
    inner = FakeInner({"grid.W": 50.0})
    drv = CachedDriver(inner, poll_interval_s=0.01)
    drv.connect()
    try:
        assert inner.read_event.wait(timeout=2.0)
        time.sleep(0.05)  # let one good read land
        good_age = drv.age_s()
        inner.fail = True  # bus goes down
        time.sleep(0.1)    # several failed polls
        assert drv.age_s() > good_age  # age grows while bus is down
        # last good value still served from cache
        read = SystemState(drv.channels())
        drv.read_state(read)
        assert read.get("grid.W") == 50.0
    finally:
        drv.disconnect()


def test_error_responses_do_not_refresh_comms_age():
    """Regression: a device answering every read with a Modbus error response
    (e.g. SmartLogger 0x0B for an offline inverter) must NOT count as a
    successful poll — otherwise comms age stays fresh and safety never trips."""

    class ErrorResult:
        registers: list[int] = []
        def isError(self):  # noqa: N802 (pymodbus API name)
            return True

    class ExceptionRespondingClient:
        def connect(self):
            return True
        def close(self):
            pass
        def read_holding_registers(self, address, count, slave):
            return ErrorResult()
        def write_registers(self, address, values, slave):
            return ErrorResult()

    huawei = Path(__file__).resolve().parents[1] / "profiles" / "inverters" / "huawei_sun2000_100ktl_m1.yaml"
    inner = ModbusDeviceDriver(
        DeviceProfile.load(huawei), client=ExceptionRespondingClient(), prefix="pv"
    )
    drv = CachedDriver(inner, poll_interval_s=0.01)
    drv.connect()
    try:
        time.sleep(0.1)  # several polls, all answered with error responses
        assert drv.age_s() == float("inf")  # never a successful read
    finally:
        drv.disconnect()


def test_write_failure_logged_once_until_recovery(caplog):
    inner = FakeInner({"grid.W": 1.0})
    drv = CachedDriver(inner, poll_interval_s=0.01)
    inner.fail_writes = True
    drv.connect()
    try:
        st = SystemState(drv.channels())
        st.set("pv.WSet", 1000.0)
        with caplog.at_level(logging.WARNING, logger="pyems.drivers.cached"):
            drv.write_setpoints(st)
            assert inner.write_event.wait(timeout=2.0)
            time.sleep(0.1)  # many more failing flushes
            failures = [r for r in caplog.records if "WRITE failed" in r.message]
            assert len(failures) == 1  # transition-gated, no per-cycle spam
            inner.fail_writes = False  # bus accepts writes again
            deadline = time.monotonic() + 2.0
            while (
                not any("WRITE recovered" in r.message for r in caplog.records)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert any("WRITE recovered" in r.message for r in caplog.records)
    finally:
        drv.disconnect()


def test_unchanged_setpoint_not_rewritten_every_poll():
    """After a successful flush, an unchanged setpoint is not re-written until
    the keep-alive period elapses — no hammering every writable register."""
    inner = FakeInner({"grid.W": 1.0})
    drv = CachedDriver(inner, poll_interval_s=0.01, setpoint_rewrite_s=60.0)
    drv.connect()
    try:
        st = SystemState(drv.channels())
        st.set("pv.WSet", 3000.0)
        drv.write_setpoints(st)
        deadline = time.monotonic() + 2.0
        while inner.written.get("pv.WSet") != 3000.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        calls_after_flush = inner.write_calls
        drv.write_setpoints(st)  # same value republished by the control cycle
        time.sleep(0.1)          # many polls — none should write
        assert inner.write_calls == calls_after_flush
        st.set("pv.WSet", 4000.0)  # a real change flushes promptly again
        drv.write_setpoints(st)
        deadline = time.monotonic() + 2.0
        while inner.written.get("pv.WSet") != 4000.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inner.written["pv.WSet"] == 4000.0
    finally:
        drv.disconnect()


def test_unchanged_setpoint_rewritten_as_keepalive():
    """Unchanged setpoints ARE periodically re-written (device watchdog food)."""
    inner = FakeInner({"grid.W": 1.0})
    drv = CachedDriver(inner, poll_interval_s=0.01, setpoint_rewrite_s=0.03)
    drv.connect()
    try:
        st = SystemState(drv.channels())
        st.set("pv.WSet", 3000.0)
        drv.write_setpoints(st)
        deadline = time.monotonic() + 2.0
        while inner.write_calls < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inner.write_calls >= 3  # initial flush + keep-alive rewrites
    finally:
        drv.disconnect()


def test_failed_flush_keeps_setpoint_dirty_and_retries():
    inner = FakeInner({"grid.W": 1.0})
    inner.fail_writes = True
    drv = CachedDriver(inner, poll_interval_s=0.01, setpoint_rewrite_s=60.0)
    drv.connect()
    try:
        st = SystemState(drv.channels())
        st.set("pv.WSet", 7000.0)
        drv.write_setpoints(st)
        assert inner.write_event.wait(timeout=2.0)  # at least one failed attempt
        inner.fail_writes = False                   # bus accepts writes again
        deadline = time.monotonic() + 2.0
        while inner.written.get("pv.WSet") != 7000.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inner.written["pv.WSet"] == 7000.0   # retried until it landed
    finally:
        drv.disconnect()


def test_setpoint_flush_not_delayed_by_slow_read():
    """A hung/blocking bus read must not hold up flushing a (safety-critical)
    setpoint to a healthy device: the worker writes before it reads. With the
    old read-first order the first iteration would block forever on the read
    and the setpoint would never flush."""
    inner = FakeInner({"grid.W": 1.0})
    inner.read_gate = threading.Event()  # never set → every read blocks
    drv = CachedDriver(inner, poll_interval_s=0.01)
    # publish the setpoint BEFORE connect, so it is pending on the first poll
    st = SystemState(drv.channels())
    st.set("pv.WSet", 8000.0)
    drv.write_setpoints(st)
    drv.connect()
    try:
        deadline = time.monotonic() + 2.0
        while inner.written.get("pv.WSet") != 8000.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert inner.written.get("pv.WSet") == 8000.0  # flushed despite the hung read
    finally:
        inner.read_gate.set()  # release the worker so it can shut down
        drv.disconnect()


def test_write_runs_before_read_each_iteration():
    """The worker flushes setpoints before polling the bus."""
    inner = FakeInner({"grid.W": 1.0})
    calls: list[str] = []
    orig_read, orig_write = inner.read_state, inner.write_setpoints

    def rec_read(state):
        calls.append("read")
        return orig_read(state)

    def rec_write(state, channels=None):
        calls.append("write")
        return orig_write(state, channels)

    inner.read_state, inner.write_setpoints = rec_read, rec_write
    drv = CachedDriver(inner, poll_interval_s=0.01)
    st = SystemState(drv.channels())
    st.set("pv.WSet", 1234.0)
    drv.write_setpoints(st)  # pending before the worker's first iteration
    drv.connect()
    try:
        deadline = time.monotonic() + 2.0
        while inner.written.get("pv.WSet") != 1234.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls[:2] == ["write", "read"]  # flush precedes the poll
    finally:
        drv.disconnect()


def test_worker_reconnects_after_bus_failure():
    inner = FakeInner({"grid.W": 50.0})
    drv = CachedDriver(inner, poll_interval_s=0.01)
    drv.connect()  # connect_calls == 1
    try:
        assert inner.read_event.wait(timeout=2.0)
        inner.fail = True
        time.sleep(0.05)               # worker notices the bus is down
        calls_before = inner.connect_calls
        inner.fail = False             # bus comes back
        deadline = time.monotonic() + 2.0
        while inner.connect_calls <= calls_before and time.monotonic() < deadline:
            time.sleep(0.01)
        # worker called connect() again to re-establish the dropped session
        assert inner.connect_calls > calls_before
    finally:
        drv.disconnect()


def test_per_device_partial_failure_keeps_healthy_cache_fresh():
    inner = PerDeviceInner()
    drv = CachedDriver(inner)

    drv._read_once()
    inner.measurements["grid.W"] = 2.0
    inner.measurements["pv.W"] = 99.0
    inner.failed = {"pv"}
    drv._read_once()

    read = SystemState(drv.channels())
    drv.read_state(read)
    assert read.get("grid.W") == 2.0
    assert read.get("pv.W") == 10.0  # failed device freezes at last good value
    assert read.get(comms_age_channel("grid")) < 1.0
    assert read.get(comms_age_channel("pv")) >= 0.0
    assert drv.device_age_s("grid") < 1.0
    assert drv.age_s() >= drv.device_age_s("grid")
    assert inner.disconnect_calls == 0
    assert inner.connect_calls == 0


def test_per_device_age_splits_and_recovers():
    inner = PerDeviceInner()
    drv = CachedDriver(inner)

    drv._read_once()
    inner.failed = {"pv"}
    time.sleep(0.02)
    drv._read_once()
    grid_age = drv.device_age_s("grid")
    pv_age = drv.device_age_s("pv")
    assert pv_age > grid_age
    assert drv.age_s() >= pv_age

    inner.failed = set()
    drv._read_once()
    assert drv.device_age_s("pv") < pv_age
    assert drv.age_s() < pv_age


def test_per_device_all_failed_uses_full_reconnect_next_poll():
    inner = PerDeviceInner()
    drv = CachedDriver(inner)

    inner.failed = {"grid", "pv"}
    drv._read_once()
    assert inner.disconnect_calls == 0
    assert inner.connect_calls == 0

    inner.failed = set()
    drv._read_once()

    assert inner.disconnect_calls == 1
    assert inner.connect_calls == 1
