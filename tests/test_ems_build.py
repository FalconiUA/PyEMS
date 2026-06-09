"""Wiring test for build_ems() (src/ems.py) — no real network."""
import pyems.drivers.modbus_device as md
from pyems.ems import build_device_drivers, build_ems
from pyems.scheduler import Scheduler


class FakeTcpClient:
    """Stand-in for ModbusTcpClient: never touches the network."""

    instances = []

    def __init__(self, host, port=502):
        self.host = host
        self.port = port
        self.connect_calls = 0
        FakeTcpClient.instances.append(self)

    def connect(self):
        self.connect_calls += 1
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
    FakeTcpClient.instances = []
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


def test_build_device_drivers_shares_tcp_client_by_endpoint(monkeypatch):
    FakeTcpClient.instances = []
    monkeypatch.setattr(md, "ModbusTcpClient", FakeTcpClient)
    drivers = build_device_drivers(
        [
            {
                "id": "plant",
                "profile": "inverters/huawei_sun2000_100ktl_m1.yaml",
                "host": "192.168.1.10",
                "slave_id": 0,
            },
            {
                "id": "grid",
                "profile": "meters/example_grid_meter.yaml",
                "host": "192.168.1.10",
                "slave_id": 11,
            },
        ]
    )

    assert len(drivers) == 2
    assert len(FakeTcpClient.instances) == 1
    assert drivers[0].connection_identity() is drivers[1].connection_identity()
