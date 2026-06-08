"""
Shared test fixtures.

FakeDriver is an in-memory Driver: it lets controller/scheduler tests run the
full scan cycle (read → execute → write) with zero hardware and zero Modbus.
This is the testability payoff of routing all I/O through SystemState.
"""
from __future__ import annotations

import pytest

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver


class FakeDriver(Driver):
    """In-memory stand-in for a real bus driver.

    - `measurements`: values the "hardware" reports, copied into state on read.
    - `written`: setpoints captured from state on write, for assertions.
    """

    def __init__(self, channels: list[Channel]) -> None:
        self._channels = channels
        self.measurements: dict[str, float] = {}
        self.written: dict[str, float] = {}
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def channels(self) -> list[Channel]:
        return self._channels

    def read_state(self, state: SystemState) -> None:
        for name, value in self.measurements.items():
            state._channels[name].value = value

    def write_setpoints(self, state: SystemState) -> None:
        for ch in self._channels:
            if ch.writable:
                self.written[ch.name] = state.get(ch.name)


@pytest.fixture
def channels() -> list[Channel]:
    """A representative tag pool: one meter, one inverter, system status tags."""
    return [
        Channel("grid.W", unit="W"),
        Channel("pv.W", unit="W"),
        Channel("pv.WSet", unit="W", min_val=0, max_val=100000, writable=True),
        Channel("sys.safe_mode", min_val=0, max_val=1, writable=True),
        Channel("sys.comms_age_s", unit="s", value=0.0),
    ]


@pytest.fixture
def state(channels) -> SystemState:
    return SystemState(channels)


@pytest.fixture
def fake_driver(channels) -> FakeDriver:
    return FakeDriver(channels)


@pytest.fixture
def fake_driver_cls() -> type[FakeDriver]:
    """The FakeDriver class itself, for tests that build it with custom channels
    (avoids importing across the tests package, which isn't on sys.path)."""
    return FakeDriver
