"""Tests for the multi-device merge (src/drivers/composite.py)."""
import pytest

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver
from pyems.drivers.composite import CompositeDriver, CompositeReadError
from pyems.drivers.modbus_device import ModbusReadError


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
            state.apply_driver_value(ch.name, ch.value)

    def write_setpoints(self, state: SystemState, channels: set[str] | None = None) -> None:
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


class ControlledDevice(SharedConnectionDevice):
    def __init__(
        self,
        channels: list[Channel],
        identity: object | None = None,
        fail_exc: Exception | None = None,
    ) -> None:
        super().__init__(channels, identity or object())
        self.fail_exc = fail_exc

    def read_state(self, state: SystemState) -> None:
        if self.fail_exc is not None:
            raise self.fail_exc
        super().read_state(state)


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


def test_device_id_validation_and_channel_map():
    grid = StubDevice([Channel("grid.W")])
    pv = StubDevice([Channel("pv.W"), Channel("pv.WSet", writable=True)])

    with pytest.raises(ValueError, match="must match"):
        CompositeDriver([grid, pv], device_ids=["grid"])
    with pytest.raises(ValueError, match="unique"):
        CompositeDriver([grid, pv], device_ids=["pv", "pv"])

    comp = CompositeDriver([grid, pv], device_ids=["grid", "pv"])
    assert comp.device_channel_map() == {
        "grid": ["grid.W"],
        "pv": ["pv.W", "pv.WSet"],
    }


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


def test_id_mode_read_error_carries_failed_ids_and_healthy_values_land():
    dead = ControlledDevice([Channel("grid.W")], fail_exc=OSError("device offline"))
    alive = ControlledDevice([Channel("pv.W", value=333.0)])
    comp = CompositeDriver([dead, alive], device_ids=["grid", "pv"])
    st = SystemState([Channel("grid.W"), Channel("pv.W")])

    with pytest.raises(CompositeReadError, match="1/2 device reads failed") as exc:
        comp.read_state(st)

    assert exc.value.failed_device_ids == frozenset({"grid"})
    assert st.get("pv.W") == 333.0


def test_transport_failure_reconnects_only_that_endpoint_on_next_read():
    dead = ControlledDevice([Channel("grid.W")], fail_exc=OSError("socket down"))
    alive = ControlledDevice([Channel("pv.W", value=123.0)])
    comp = CompositeDriver([dead, alive], device_ids=["grid", "pv"])
    st = SystemState([Channel("grid.W"), Channel("pv.W")])

    with pytest.raises(CompositeReadError):
        comp.read_state(st)

    dead.fail_exc = None
    comp.read_state(st)

    assert dead.disconnect_calls == 1
    assert dead.connect_calls == 1
    assert alive.disconnect_calls == 0
    assert alive.connect_calls == 0


def test_modbus_read_error_does_not_reconnect_endpoint():
    dead = ControlledDevice(
        [Channel("grid.W")],
        fail_exc=ModbusReadError("device answered with exception"),
    )
    alive = ControlledDevice([Channel("pv.W", value=123.0)])
    comp = CompositeDriver([dead, alive], device_ids=["grid", "pv"])
    st = SystemState([Channel("grid.W"), Channel("pv.W")])

    with pytest.raises(CompositeReadError):
        comp.read_state(st)

    dead.fail_exc = None
    comp.read_state(st)

    assert dead.disconnect_calls == 0
    assert dead.connect_calls == 0
