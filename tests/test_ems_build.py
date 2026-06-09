"""Wiring test for build_ems() (src/ems.py) — no real network."""
import pyems.drivers.modbus_device as md
from pyems.ems import build_ems
from pyems.scheduler import Scheduler


class FakeTcpClient:
    """Stand-in for ModbusTcpClient: never touches the network."""

    def __init__(self, host, port=502):
        self.host = host
        self.port = port

    def connect(self):
        return True

    def close(self):
        pass

    def read_holding_registers(self, address, count, slave):
        class R:
            registers = [0] * count
            def isError(self):  # noqa: N802 (pymodbus API name)
                return False
        return R()

    def write_registers(self, address, values, slave):
        pass


def test_build_ems_wires_scheduler(monkeypatch):
    monkeypatch.setattr(md, "ModbusTcpClient", FakeTcpClient)
    sched = build_ems()
    try:
        assert isinstance(sched, Scheduler)
        # two priority tasks: safety (0) before fast (1)
        priorities = [t.priority for t in sched._tasks]
        assert priorities == sorted(priorities)
        assert priorities[0] == 0
        # all controller-bound tags from site.yaml exist in the tag pool
        names = set(sched._state.snapshot())
        for tag in ("grid.W", "pv.W", "pv.WSet", "sys.safe_mode", "sys.comms_age_s"):
            assert tag in names
        # allocator + board wired from the allocation section, owning pv.WSet
        assert sched._allocator is not None
        assert sched._board is not None
        assert sched._allocator.channels == ["pv.WSet"]
    finally:
        sched._driver.disconnect()
