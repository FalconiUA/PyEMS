"""Tests for the non-blocking cache layer (src/drivers/cached.py)."""
import logging
import threading
import time
from pathlib import Path

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver
from pyems.drivers.cached import COMMS_AGE_CHANNEL, CachedDriver
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
        self.written: dict[str, float] = {}

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        pass

    def channels(self) -> list[Channel]:
        return self._channels

    def read_state(self, state: SystemState) -> None:
        if self.fail:
            raise OSError("bus down")
        for name, value in self._measurements.items():
            state._channels[name].value = value
        self.read_event.set()

    def write_setpoints(self, state: SystemState) -> None:
        if self.fail_writes:
            self.write_event.set()
            raise OSError("write rejected")
        self.written["pv.WSet"] = state.get("pv.WSet")
        self.write_event.set()


def test_channels_include_comms_age_tag():
    drv = CachedDriver(FakeInner({"grid.W": 0.0}))
    assert COMMS_AGE_CHANNEL in [c.name for c in drv.channels()]


def test_age_is_inf_before_first_read():
    drv = CachedDriver(FakeInner({"grid.W": 0.0}))
    assert drv.age_s() == float("inf")


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
            while inner.written.get("pv.WSet") != 1000.0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert any("WRITE recovered" in r.message for r in caplog.records)
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
