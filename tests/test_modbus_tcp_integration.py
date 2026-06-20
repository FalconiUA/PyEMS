"""End-to-end Modbus TCP tests against a REAL in-process pymodbus server.

Everything else in the suite uses fake clients; this file exercises the actual
wire path — pymodbus client construction (incl. the unit/device_id keyword of
the installed pymodbus version), framing, decoding, write round-trips and the
error-response path — without hardware, via pymodbus's own TCP server.
"""
import asyncio
import socket
import threading
import time

import pytest
from pymodbus.client import ModbusTcpClient
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from pyems.channels import SystemState
from pyems.drivers.cached import CachedDriver
from pyems.drivers.modbus_device import (
    ModbusDeviceDriver,
    ModbusReadError,
    make_client,
)

PROFILE = """
model: Integration Test Device
protocol: modbus_tcp
default_port: 502
registers:
  - channel: dev.W
    address: 100
    type: int32
    scale: 1.0
    unit: W
    access: read
  - channel: dev.Hz
    address: 110
    type: uint16
    scale: 0.01
    unit: Hz
    access: read
  - channel: dev.WSet
    address: 120
    type: uint32
    scale: 1.0
    unit: W
    access: read_write
    min_val: 0
    max_val: 100000
"""

# Reads a register the server does not serve → real ExceptionResponse.
BROKEN_PROFILE = """
model: Broken Address Device
protocol: modbus_tcp
default_port: 502
registers:
  - channel: dev.W
    address: 5000
    type: int16
    scale: 1.0
    unit: W
    access: read
"""


@pytest.fixture(scope="module")
def modbus_server():
    """Real pymodbus TCP server with holding registers 0..399, on a free port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    def serve():
        asyncio.set_event_loop(loop)

        async def run():
            block = SimData(0, count=400, values=0, datatype=DataType.REGISTERS)
            server = ModbusTcpServer(
                SimDevice(id=1, simdata=[block]), address=("127.0.0.1", port)
            )
            holder["server"] = server
            ready.set()
            await server.serve_forever()

        loop.run_until_complete(run())

    thread = threading.Thread(target=serve, name="modbus-test-server", daemon=True)
    thread.start()
    assert ready.wait(timeout=5.0), "test Modbus server never started"
    yield "127.0.0.1", port
    asyncio.run_coroutine_threadsafe(holder["server"].shutdown(), loop).result(5.0)
    thread.join(timeout=5.0)


@pytest.fixture()
def seeded_client(modbus_server):
    """Raw pymodbus client to seed/inspect server registers from the test."""
    host, port = modbus_server
    client = ModbusTcpClient(host, port=port, timeout=2)
    deadline = time.monotonic() + 5.0
    while not client.connect() and time.monotonic() < deadline:
        time.sleep(0.05)
    yield client
    client.close()


def write_profile(tmp_path, text):
    path = tmp_path / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_read_over_real_tcp_decodes_and_scales(modbus_server, seeded_client, tmp_path):
    host, port = modbus_server
    # dev.W @100 int32 = -2 (two's complement over two regs); dev.Hz @110 = 50.01
    assert not seeded_client.write_registers(100, [0xFFFF, 0xFFFE], device_id=1).isError()
    assert not seeded_client.write_registers(110, [5001], device_id=1).isError()

    drv = ModbusDeviceDriver.from_profile(
        write_profile(tmp_path, PROFILE), host, port=port, slave_id=1, prefix="dev1"
    )
    drv.connect()
    try:
        st = SystemState(drv.channels())
        drv.read_state(st)
        assert st.get("dev1.W") == pytest.approx(-2.0)
        assert st.get("dev1.Hz") == pytest.approx(50.01)
    finally:
        drv.disconnect()


def test_write_over_real_tcp_lands_in_server_register(modbus_server, seeded_client, tmp_path):
    host, port = modbus_server
    drv = ModbusDeviceDriver.from_profile(
        write_profile(tmp_path, PROFILE), host, port=port, slave_id=1, prefix="dev1"
    )
    drv.connect()
    try:
        st = SystemState(drv.channels())
        st.set("dev1.WSet", 54321.0)
        drv.write_setpoints(st)
    finally:
        drv.disconnect()
    readback = seeded_client.read_holding_registers(120, count=2, device_id=1)
    assert not readback.isError()
    assert readback.registers == [0x0000, 0xD431]  # 54321 as uint32 big-endian words


def test_error_response_over_real_tcp_raises_read_error(modbus_server, tmp_path):
    """The server answers an out-of-range address with a true Modbus exception
    response — the driver must raise, so CachedDriver ages the comms."""
    host, port = modbus_server
    drv = ModbusDeviceDriver.from_profile(
        write_profile(tmp_path, BROKEN_PROFILE), host, port=port, slave_id=1, prefix="dev1"
    )
    drv.connect()
    try:
        with pytest.raises(ModbusReadError):
            drv.read_state(SystemState(drv.channels()))
    finally:
        drv.disconnect()


def test_cached_driver_full_path_over_real_tcp(modbus_server, seeded_client, tmp_path):
    """Production wiring end to end: CachedDriver worker polls the real server
    and flushes a setpoint to it, comms age goes finite."""
    host, port = modbus_server
    assert not seeded_client.write_registers(100, [0x0000, 0x03E8], device_id=1).isError()  # 1000 W

    inner = ModbusDeviceDriver.from_profile(
        write_profile(tmp_path, PROFILE), host, port=port, slave_id=1, prefix="dev1"
    )
    drv = CachedDriver(inner, poll_interval_s=0.05, setpoint_rewrite_s=60.0)
    drv.connect()
    try:
        deadline = time.monotonic() + 5.0
        while drv.age_s() == float("inf") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert drv.age_s() < 5.0  # at least one successful real poll

        st = SystemState(drv.channels())
        drv.read_state(st)
        assert st.get("dev1.W") == pytest.approx(1000.0)

        st.set("dev1.WSet", 42000.0)
        drv.write_setpoints(st)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            readback = seeded_client.read_holding_registers(120, count=2, device_id=1)
            if not readback.isError() and readback.registers == [0x0000, 0xA410]:
                break
            time.sleep(0.05)
        assert readback.registers == [0x0000, 0xA410]  # 42000 landed on the server
    finally:
        drv.disconnect()


def test_make_client_builds_working_real_tcp_client(modbus_server):
    """make_client + the installed pymodbus: connect and transact for real."""
    host, port = modbus_server
    client = make_client("modbus_tcp", host, port=port, timeout_s=2.0, retries=1)
    assert client.connect()
    try:
        from pyems.drivers.modbus_device import _read_holding_registers

        result = _read_holding_registers(client, 0, 1, 1)
        assert not result.isError()
    finally:
        client.close()


def test_probe_registers_over_real_tcp_reports_raw_and_value(
    modbus_server, seeded_client, tmp_path
):
    """The UI diagnostic primitive over the real wire: raw words and the decoded,
    scaled value per register — exactly what the operator needs to see."""
    from pyems.drivers.modbus_device import DeviceProfile, probe_registers

    host, port = modbus_server
    assert not seeded_client.write_registers(100, [0x0000, 0x04D2], device_id=1).isError()  # 1234
    assert not seeded_client.write_registers(110, [5001], device_id=1).isError()  # 50.01 Hz

    profile = DeviceProfile.load(write_profile(tmp_path, PROFILE))
    client = make_client("modbus_tcp", host, port=port, timeout_s=2.0, retries=0)
    assert client.connect()
    try:
        results = probe_registers(client, profile, 1, prefix="dev1")
    finally:
        client.close()

    by_channel = {r["channel"]: r for r in results if not r.get("aborted")}
    assert by_channel["dev1.W"]["ok"] is True
    assert by_channel["dev1.W"]["raw"] == [0x0000, 0x04D2]
    assert by_channel["dev1.W"]["value"] == pytest.approx(1234.0)
    assert by_channel["dev1.Hz"]["value"] == pytest.approx(50.01)


def test_probe_registers_over_real_tcp_reports_exception_code(modbus_server, tmp_path):
    """A read of an address the server does not serve comes back as a true Modbus
    exception RESPONSE (0x02), not a timeout — the diagnostic must name it."""
    from pyems.drivers.modbus_device import DeviceProfile, probe_registers

    host, port = modbus_server
    profile = DeviceProfile.load(write_profile(tmp_path, BROKEN_PROFILE))
    client = make_client("modbus_tcp", host, port=port, timeout_s=2.0, retries=0)
    assert client.connect()
    try:
        results = probe_registers(client, profile, 1, prefix="dev1")
    finally:
        client.close()

    entry = results[0]
    assert entry["ok"] is False and entry["timeout"] is False
    assert "0x02" in entry["error"]  # illegal data address
