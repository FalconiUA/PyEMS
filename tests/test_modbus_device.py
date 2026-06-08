"""Tests for the profile-driven Modbus driver (src/drivers/modbus_device.py)."""
import pytest

from pyems.channels import SystemState
from pyems.drivers.modbus_device import (
    DeviceProfile,
    ModbusDeviceDriver,
    RegisterDef,
    _decode,
    _encode,
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
    def __init__(self, reads: dict[int, list[int]]):
        self._reads = reads
        self.writes: dict[int, list[int]] = {}

    def read_holding_registers(self, address, count, slave):
        if address not in self._reads:
            return FakeResult([], error=True)
        return FakeResult(self._reads[address])

    def write_registers(self, address, values, slave):
        self.writes[address] = values


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


def test_read_error_keeps_value_unchanged():
    prof = DeviceProfile.load(HUAWEI)
    client = FakeModbusClient({})  # every read returns error
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    st._channels["pv1.W"].value = 42.0
    drv.read_state(st)
    assert st.get("pv1.W") == 42.0  # untouched on error
