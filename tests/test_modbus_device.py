"""Tests for the profile-driven Modbus driver (src/drivers/modbus_device.py)."""
import pytest

import pyems.drivers.modbus_device as md
from pyems.channels import SystemState
from pyems.drivers.modbus_device import (
    DEFAULT_SERIAL,
    DeviceProfile,
    ModbusDeviceDriver,
    ModbusReadError,
    ModbusWriteError,
    RegisterDef,
    _decode,
    _encode,
    make_client,
    namespaced,
)


# ── namespacing (feature 2.1) ────────────────────────────────────────────────
@pytest.mark.parametrize("local,prefix,expected", [
    ("pv.W", "pv1", "pv1.W"),
    ("pv.WSet", "pv2", "pv2.WSet"),
    ("grid.W", None, "grid.W"),     # no prefix → verbatim
    ("grid.W", "", "grid.W"),       # empty prefix → verbatim
    ("Status", "pv1", "pv1.Status"),  # no dot → prepend
])
def test_namespaced(local, prefix, expected):
    assert namespaced(local, prefix) == expected


# ── register decode/encode ───────────────────────────────────────────────────
def reg(type_):
    return RegisterDef(channel="x", address=0, type=type_, scale=1.0, unit="W", access="read")


def test_decode_uint16():
    assert _decode([1234], reg("uint16")) == 1234


def test_decode_int16_negative():
    assert _decode([0xFFFF], reg("int16")) == -1


def test_decode_int32_negative():
    # -2 as 32-bit two's complement = 0xFFFFFFFE → regs [0xFFFF, 0xFFFE]
    assert _decode([0xFFFF, 0xFFFE], reg("int32")) == -2


def test_decode_uint32():
    assert _decode([0x0001, 0x0000], reg("uint32")) == 0x00010000


def test_encode_roundtrip_int32():
    r = reg("int32")
    assert _decode(_encode(-2, r), r) == -2


def test_encode_uint16():
    assert _encode(0x1234, reg("uint16")) == [0x1234]


# ── RegisterDef derived properties ───────────────────────────────────────────
def test_registerdef_properties():
    r = RegisterDef("pv.WSet", 40126, "uint32", 1.0, "W", "read_write")
    assert r.count == 2
    assert r.writable is True
    assert r.signed is False
    assert RegisterDef("pv.W", 0, "int16", 1.0, "W", "read").signed is True


# ── profile loading + namespaced channels ────────────────────────────────────
# Anchor to the repo root so the test does not depend on the working directory.
from pathlib import Path

HUAWEI = Path(__file__).resolve().parents[1] / "profiles" / "inverters" / "huawei_sun2000_100ktl_m1.yaml"


def test_profile_load():
    prof = DeviceProfile.load(HUAWEI)
    assert prof.protocol == "modbus_tcp"
    assert prof.default_port == 502
    assert any(r.channel == "pv.WSet" and r.writable for r in prof.registers)


def test_driver_channels_are_namespaced():
    prof = DeviceProfile.load(HUAWEI)
    drv = ModbusDeviceDriver(prof, client=None, slave_id=1, prefix="pv1")
    names = [c.name for c in drv.channels()]
    assert "pv1.W" in names and "pv1.WSet" in names
    assert "pv.W" not in names  # original class prefix replaced


# ── read/write against a fake Modbus client ──────────────────────────────────
class FakeResult:
    def __init__(self, registers, error=False):
        self.registers = registers
        self._error = error

    def isError(self):
        return self._error


class FakeModbusClient:
    def __init__(self, reads: dict[int, list[int]], fail_unknown: bool = False,
                 fail_writes: bool = False):
        self._reads = reads
        self._fail_unknown = fail_unknown  # unknown address → Modbus error response
        self._fail_writes = fail_writes    # every write → Modbus error response
        self.writes: dict[int, list[int]] = {}
        self.reads: list[tuple[int, int, int]] = []
        self.connect_calls = 0
        self.close_calls = 0

    def connect(self):
        self.connect_calls += 1
        return True

    def close(self):
        self.close_calls += 1

    def read_holding_registers(self, address, count, slave):
        self.reads.append((address, count, slave))
        if address not in self._reads:
            if self._fail_unknown:
                return FakeResult([], error=True)
            return FakeResult([0] * count)
        return FakeResult(self._reads[address])

    def write_registers(self, address, values, slave):
        if self._fail_writes:
            return FakeResult([], error=True)
        self.writes[address] = values
        return FakeResult(values)


def test_read_state_decodes_and_scales_into_namespaced_tag():
    prof = DeviceProfile.load(HUAWEI)
    # pv.W @32080 int32 scale 1.0 → value 65000 across two regs
    client = FakeModbusClient({32080: [0x0000, 0xFDE8]})  # 65000
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    drv.read_state(st)
    assert st.get("pv1.W") == pytest.approx(65000.0)


def test_write_setpoints_encodes_from_namespaced_tag():
    prof = DeviceProfile.load(HUAWEI)
    client = FakeModbusClient({})
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    st.set("pv1.WSet", 50000.0)
    drv.write_setpoints(st)
    # pv.WSet @40126; 50000 = 0x0000C350 → [0x0000, 0xC350]
    assert client.writes[40126] == [0x0000, 0xC350]


def test_read_error_keeps_value_unchanged_and_raises():
    prof = DeviceProfile.load(HUAWEI)
    client = FakeModbusClient({}, fail_unknown=True)  # every read returns error
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    st._channels["pv1.W"].value = 42.0
    # Error responses must fail the poll loudly — a gateway answering every
    # request with an exception code must not count as a successful read.
    with pytest.raises(ModbusReadError):
        drv.read_state(st)
    assert st.get("pv1.W") == 42.0  # untouched on error


def test_implausible_value_fails_poll_and_keeps_last_value():
    prof = DeviceProfile.load(HUAWEI)
    # pv.W @32080 bounded [-10000, 110000] in the profile; feed 500000 W —
    # decodable, but implausible for a 100 kW unit (wrong scale/profile/gateway
    # garbage). It must fail the poll, not enter the loop as a measurement.
    client = FakeModbusClient({32080: [0x0007, 0xA120]})  # 500000
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    st._channels["pv1.W"].value = 42.0
    with pytest.raises(ModbusReadError, match="pv1.W"):
        drv.read_state(st)
    assert st.get("pv1.W") == 42.0  # untouched on implausible read


def test_partial_read_error_updates_good_registers_then_raises():
    prof = DeviceProfile.load(HUAWEI)
    # only pv.W @32080 answers; every other register returns an error response
    client = FakeModbusClient({32080: [0x0000, 0xFDE8]}, fail_unknown=True)
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    with pytest.raises(ModbusReadError, match="pv1"):
        drv.read_state(st)
    assert st.get("pv1.W") == pytest.approx(65000.0)  # good register still landed


def test_write_setpoints_channel_subset_skips_other_registers():
    prof = DeviceProfile.load(HUAWEI)
    client = FakeModbusClient({})
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    st.set("pv1.WSet", 50000.0)
    drv.write_setpoints(st, channels=set())            # nothing due
    assert client.writes == {}
    drv.write_setpoints(st, channels={"other.WSet"})   # not this device's tag
    assert client.writes == {}
    drv.write_setpoints(st, channels={"pv1.WSet"})     # due → written
    assert client.writes[40126] == [0x0000, 0xC350]


def test_write_error_response_raises():
    prof = DeviceProfile.load(HUAWEI)
    client = FakeModbusClient({}, fail_writes=True)
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    st.set("pv1.WSet", 50000.0)
    with pytest.raises(ModbusWriteError, match="pv1.WSet"):
        drv.write_setpoints(st)


def test_shared_client_keeps_per_driver_slave_id():
    prof = DeviceProfile.load(HUAWEI)
    client = FakeModbusClient({32080: [0x0000, 0x03E8]})
    d1 = ModbusDeviceDriver(prof, client=client, slave_id=0, prefix="plant")
    d2 = ModbusDeviceDriver(prof, client=client, slave_id=11, prefix="meter")

    d1.read_state(SystemState(d1.channels()))
    d2.read_state(SystemState(d2.channels()))

    assert any(slave == 0 for _, _, slave in client.reads)
    assert any(slave == 11 for _, _, slave in client.reads)


# ── make_client: per-protocol client construction (RTU serial params) ────────
class FakeSerialClient:
    def __init__(self, port, **kwargs):
        self.port = port
        self.kwargs = kwargs


class FakeTcpClientKw:
    def __init__(self, host, port=502, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs


def test_make_client_rtu_applies_serial_defaults(monkeypatch):
    monkeypatch.setattr(md, "ModbusSerialClient", FakeSerialClient)
    client = make_client("modbus_rtu", "/dev/ttyUSB0")
    assert client.port == "/dev/ttyUSB0"
    assert client.kwargs == DEFAULT_SERIAL  # 9600 8N1


def test_make_client_rtu_overrides_serial_and_timeout(monkeypatch):
    monkeypatch.setattr(md, "ModbusSerialClient", FakeSerialClient)
    client = make_client(
        "modbus_rtu", "/dev/ttyUSB0",
        serial={"baudrate": 19200, "parity": "E"}, timeout_s=0.4,
    )
    assert client.kwargs["baudrate"] == 19200
    assert client.kwargs["parity"] == "E"
    assert client.kwargs["stopbits"] == DEFAULT_SERIAL["stopbits"]
    assert client.kwargs["timeout"] == 0.4


def test_make_client_rtu_rejects_unknown_serial_key(monkeypatch):
    monkeypatch.setattr(md, "ModbusSerialClient", FakeSerialClient)
    with pytest.raises(ValueError, match="baud_rate"):
        make_client("modbus_rtu", "/dev/ttyUSB0", serial={"baud_rate": 9600})


def test_make_client_tcp_port_timeout_and_retries(monkeypatch):
    monkeypatch.setattr(md, "ModbusTcpClient", FakeTcpClientKw)
    client = make_client(
        "modbus_tcp", "10.0.0.5", default_port=1502, timeout_s=2.0, retries=1
    )
    assert (client.host, client.port) == ("10.0.0.5", 1502)
    assert client.kwargs["timeout"] == 2.0
    assert client.kwargs["retries"] == 1


def test_make_client_unknown_protocol_raises():
    with pytest.raises(ValueError, match="Unknown protocol"):
        make_client("modbus_ascii", "/dev/ttyUSB0")
