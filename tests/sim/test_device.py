"""SimulatedDevice over real Modbus TCP: codec round-trips and fault injection."""
import socket
import time

import pytest
from pymodbus.client import ModbusTcpClient

from pyems.drivers.modbus_device import DeviceProfile
from pyems.ems import PROFILES
from pyems.sim.device import SimulatedDevice


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture()
def meter_device():
    profile = DeviceProfile.load(PROFILES / "meters/example_grid_meter.yaml")
    dev = SimulatedDevice("grid", profile, "127.0.0.1", free_port(), slave_id=1)
    dev.start()
    yield dev
    dev.stop()


@pytest.fixture()
def pv_device():
    profile = DeviceProfile.load(PROFILES / "inverters/huawei_sun2000_100ktl_m1.yaml")
    setpoints: list[tuple[str, float]] = []
    dev = SimulatedDevice(
        "pv", profile, "127.0.0.1", free_port(), slave_id=1,
        on_setpoint=lambda field, value: setpoints.append((field, value)),
    )
    dev.setpoints = setpoints
    dev.start()
    yield dev
    dev.stop()


def connect(dev: SimulatedDevice) -> ModbusTcpClient:
    client = ModbusTcpClient(dev.host, port=dev.port, timeout=2)
    deadline = time.monotonic() + 5.0
    while not client.connect() and time.monotonic() < deadline:
        time.sleep(0.05)
    return client


def read_grid_w(client: ModbusTcpClient) -> float:
    result = client.read_holding_registers(40001, count=2, device_id=1)
    assert not result.isError()
    value = (result.registers[0] << 16) | result.registers[1]
    if value >= 1 << 31:
        value -= 1 << 32
    return float(value)


def test_meter_serves_negative_int32(meter_device):
    meter_device.set_fields({"W": -23456.0})
    client = connect(meter_device)
    try:
        assert read_grid_w(client) == -23456.0
    finally:
        client.close()


def test_write_setpoint_decoded_and_routed(pv_device):
    client = connect(pv_device)
    try:
        # pv.WSet @ 40126 uint32: 54321 W
        result = client.write_registers(40126, [0x0000, 0xD431], device_id=1)
        assert not result.isError()
    finally:
        client.close()
    assert pv_device.setpoints == [("WSet", 54321.0)]


def test_freeze_fault_serves_stale_values(meter_device):
    client = connect(meter_device)
    try:
        meter_device.set_fields({"W": 1000.0})
        assert read_grid_w(client) == 1000.0
        meter_device.set_fault("freeze", True)
        meter_device.set_fields({"W": 9999.0})
        assert read_grid_w(client) == 1000.0  # frozen
        meter_device.set_fault("freeze", False)
        assert read_grid_w(client) == 9999.0
    finally:
        client.close()


def test_modbus_exception_fault(meter_device):
    client = connect(meter_device)
    try:
        meter_device.set_fault("modbus_exception", True)
        result = client.read_holding_registers(40001, count=2, device_id=1)
        assert result.isError()
        meter_device.set_fault("modbus_exception", False)
        meter_device.set_fields({"W": 5.0})
        assert read_grid_w(client) == 5.0
    finally:
        client.close()


def test_reject_writes_fault(pv_device):
    client = connect(pv_device)
    try:
        pv_device.set_fault("reject_writes", True)
        result = client.write_registers(40126, [0, 100], device_id=1)
        assert result.isError()
        # the rejected write must not reach the plant model
        assert pv_device.setpoints == []
        # reads still work while writes are rejected
        pv_device.set_fields({"W": 1234.0})
        read = client.read_holding_registers(32080, count=2, device_id=1)
        assert not read.isError()
    finally:
        client.close()


def test_offline_fault_kills_and_restores_server(meter_device):
    meter_device.set_fields({"W": 777.0})
    client = connect(meter_device)
    try:
        assert read_grid_w(client) == 777.0
    finally:
        client.close()

    meter_device.set_fault("offline", True)
    assert not meter_device.online()
    dead = ModbusTcpClient(meter_device.host, port=meter_device.port, timeout=0.5)
    try:
        ok = dead.connect()
        if ok:  # connection may be accepted by the OS backlog; the read must fail
            with pytest.raises(Exception):
                result = dead.read_holding_registers(40001, count=2, device_id=1)
                assert result.isError()
    finally:
        dead.close()

    meter_device.set_fault("offline", False)
    assert meter_device.online()
    client = connect(meter_device)
    try:
        assert read_grid_w(client) == 777.0
    finally:
        client.close()
