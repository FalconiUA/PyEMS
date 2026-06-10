"""Tests for the multi-device merge (src/drivers/composite.py)."""
import pytest

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver
from pyems.drivers.composite import CompositeDriver


class StubDevice(Driver):
    def __init__(self, channels: list[Channel]) -> None:
        self._channels = channels
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def channels(self) -> list[Channel]:
        return self._channels

    def read_state(self, state: SystemState) -> None:
        for ch in self._channels:
            state._channels[ch.name].value = ch.value

    def write_setpoints(self, state: SystemState) -> None:
        pass


class SharedConnectionDevice(StubDevice):
    def __init__(self, channels: list[Channel], identity: object) -> None:
        super().__init__(channels)
        self._identity = identity
        self.connect_calls = 0
        self.disconnect_calls = 0

    def connection_identity(self) -> object:
        return self._identity

    def connect(self) -> None:
        self.connect_calls += 1
        super().connect()

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        super().disconnect()


def test_merges_channels_from_all_devices():
    a = StubDevice([Channel("grid.W")])
    b = StubDevice([Channel("pv.W"), Channel("pv.WSet", writable=True)])
    names = [c.name for c in CompositeDriver([a, b]).channels()]
    assert names == ["grid.W", "pv.W", "pv.WSet"]


def test_duplicate_channel_across_devices_raises():
    a = StubDevice([Channel("pv.W")])
    b = StubDevice([Channel("pv.W")])  # collision (e.g. two un-namespaced inverters)
    with pytest.raises(ValueError, match="Duplicate channel"):
        CompositeDriver([a, b]).channels()


def test_connect_disconnect_fan_out():
    a, b = StubDevice([Channel("a")]), StubDevice([Channel("b")])
    comp = CompositeDriver([a, b])
    comp.connect()
    assert a.connected and b.connected
    comp.disconnect()
    assert not a.connected and not b.connected


def test_shared_connection_connects_once_per_composite_call():
    identity = object()
    a = SharedConnectionDevice([Channel("a")], identity)
    b = SharedConnectionDevice([Channel("b")], identity)
    comp = CompositeDriver([a, b])

    comp.connect()
    comp.disconnect()

    assert a.connect_calls + b.connect_calls == 1
    assert a.disconnect_calls + b.disconnect_calls == 1


def test_read_fans_out_to_all_devices():
    a = StubDevice([Channel("grid.W", value=111.0)])
    b = StubDevice([Channel("pv.W", value=222.0)])
    comp = CompositeDriver([a, b])
    st = SystemState([Channel("grid.W"), Channel("pv.W")])
    comp.read_state(st)
    assert st.get("grid.W") == 111.0
    assert st.get("pv.W") == 222.0


class FailingDevice(StubDevice):
    def read_state(self, state: SystemState) -> None:
        raise OSError("device offline")


def test_one_failing_device_does_not_block_others_and_read_raises():
    """Healthy devices keep their values fresh; the aggregate error still marks
    the poll failed (conservative — the comms age grows, safety may trip)."""
    dead = FailingDevice([Channel("grid.W")])
    alive = StubDevice([Channel("pv.W", value=333.0)])
    comp = CompositeDriver([dead, alive])
    st = SystemState([Channel("grid.W"), Channel("pv.W")])
    with pytest.raises(IOError, match="1/2 device reads failed"):
        comp.read_state(st)
    assert st.get("pv.W") == 333.0  # the healthy device was still polled
