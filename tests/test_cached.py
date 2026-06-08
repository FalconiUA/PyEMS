"""Tests for the non-blocking cache layer (src/drivers/cached.py)."""
import threading
import time

from src.channels import Channel, SystemState
from src.drivers.base import Driver
from src.drivers.cached import COMMS_AGE_CHANNEL, CachedDriver


class FakeInner(Driver):
    """Inner driver whose 'bus' returns fixed values and signals each read.

    `fail` makes read_state raise, to exercise the stale-cache / age path.
    """

    def __init__(self, measurements: dict[str, float]) -> None:
        self._measurements = measurements
        self._channels = [Channel(n) for n in measurements] + [
            Channel("pv.WSet", writable=True, min_val=0, max_val=1e5)
        ]
        self.fail = False
        self.read_event = threading.Event()
        self.written: dict[str, float] = {}

    def connect(self) -> None:
        pass

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
        self.written["pv.WSet"] = state.get("pv.WSet")


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
