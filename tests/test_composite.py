"""Tests for the multi-device merge (src/drivers/composite.py)."""
import pytest

from src.channels import Channel, SystemState
from src.drivers.base import Driver
from src.drivers.composite import CompositeDriver


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


def test_read_fans_out_to_all_devices():
    a = StubDevice([Channel("grid.W", value=111.0)])
    b = StubDevice([Channel("pv.W", value=222.0)])
    comp = CompositeDriver([a, b])
    st = SystemState([Channel("grid.W"), Channel("pv.W")])
    comp.read_state(st)
    assert st.get("grid.W") == 111.0
    assert st.get("pv.W") == 222.0
