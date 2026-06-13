"""Tests for the profile-driven Modbus driver (src/drivers/modbus_device.py)."""
from dataclasses import replace
from pathlib import Path

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
HUAWEI = Path(__file__).resolve().parents[1] / "profiles" / "inverters" / "huawei_sun2000_100ktl_m1.yaml"


def profile_reg(profile: DeviceProfile, channel: str) -> RegisterDef:
    return next(r for r in profile.registers if r.channel == channel)


def words_for(value: float, regdef: RegisterDef) -> list[int]:
    return _encode(int(value / regdef.scale), regdef)


def profile_with_bounds(
    profile: DeviceProfile, channel: str, min_val: float, max_val: float
) -> DeviceProfile:
    return replace(
        profile,
        registers=[
            replace(r, min_val=min_val, max_val=max_val)
            if r.channel == channel
            else r
            for r in profile.registers
        ],
    )


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
        self.single_writes: dict[int, int] = {}
        self.reads: list[tuple[int, int, int]] = []
        self.input_reads: list[tuple[int, int, int]] = []
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

    def read_input_registers(self, address, count, slave):
        self.input_reads.append((address, count, slave))
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

    def write_register(self, address, value, slave):
        if self._fail_writes:
            return FakeResult([], error=True)
        self.single_writes[address] = value
        return FakeResult([value])


def test_read_state_decodes_and_scales_into_namespaced_tag():
    prof = DeviceProfile.load(HUAWEI)
    reg_w = profile_reg(prof, "pv.W")
    client = FakeModbusClient({reg_w.address: words_for(65000.0, reg_w)})
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    drv.read_state(st)
    assert st.get("pv1.W") == pytest.approx(65000.0)


def test_write_setpoints_encodes_from_namespaced_tag():
    prof = DeviceProfile.load(HUAWEI)
    reg_wset = profile_reg(prof, "pv.WSet")
    client = FakeModbusClient({})
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    setpoint_w = 5000.0
    st.set("pv1.WSet", setpoint_w)
    drv.write_setpoints(st)
    assert client.writes[reg_wset.address] == words_for(setpoint_w, reg_wset)


def test_read_error_keeps_value_unchanged_and_raises():
    prof = DeviceProfile.load(HUAWEI)
    client = FakeModbusClient({}, fail_unknown=True)  # every read returns error
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    st.apply_driver_value("pv1.W", 42.0)
    # Error responses must fail the poll loudly — a gateway answering every
    # request with an exception code must not count as a successful read.
    with pytest.raises(ModbusReadError):
        drv.read_state(st)
    assert st.get("pv1.W") == 42.0  # untouched on error


def test_implausible_value_fails_poll_and_keeps_last_value():
    prof = profile_with_bounds(DeviceProfile.load(HUAWEI), "pv.W", -10000.0, 110000.0)
    reg_w = profile_reg(prof, "pv.W")
    # Feed 500000 W outside the test-only bounded measurement range.
    # decodable, but implausible for a 100 kW unit (wrong scale/profile/gateway
    # garbage). It must fail the poll, not enter the loop as a measurement.
    client = FakeModbusClient({reg_w.address: words_for(500000.0, reg_w)})
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    st.apply_driver_value("pv1.W", 42.0)
    with pytest.raises(ModbusReadError, match="pv1.W"):
        drv.read_state(st)
    assert st.get("pv1.W") == 42.0  # untouched on implausible read


def test_out_of_range_writable_register_does_not_fail_poll():
    """Bounds on a read_write register guard what WE write; device-side content
    (e.g. an out-of-range factory default in WSet before our first write) must
    not fail the poll — that would be a permanent spurious safety trip."""
    prof = DeviceProfile.load(HUAWEI)
    reg_wset = profile_reg(prof, "pv.WSet")
    client = FakeModbusClient({reg_wset.address: [0xFFFF] * reg_wset.count})
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    drv.read_state(st)  # must not raise


# ── profile validation: a bad YAML must fail at load, not mid-poll ───────────
def test_unknown_register_type_rejected_at_load():
    with pytest.raises(ValueError, match="unknown type"):
        RegisterDef("pv.W", 0, "int64", 1.0, "W", "read")


def test_misspelled_access_rejected_at_load():
    # 'read-write' would otherwise silently demote the setpoint to read-only.
    with pytest.raises(ValueError, match="access"):
        RegisterDef("pv.WSet", 0, "uint32", 1.0, "W", "read-write")


def test_zero_scale_rejected_at_load():
    # would otherwise ZeroDivisionError on the first setpoint write
    with pytest.raises(ValueError, match="scale"):
        RegisterDef("pv.WSet", 0, "uint32", 0.0, "W", "read_write")


def test_inverted_bounds_rejected_at_load():
    with pytest.raises(ValueError, match="min_val"):
        RegisterDef("pv.W", 0, "int16", 1.0, "W", "read", min_val=10, max_val=-10)


def test_partial_read_error_updates_good_registers_then_raises():
    prof = DeviceProfile.load(HUAWEI)
    reg_w = profile_reg(prof, "pv.W")
    client = FakeModbusClient({reg_w.address: words_for(65000.0, reg_w)}, fail_unknown=True)
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    with pytest.raises(ModbusReadError, match="pv1"):
        drv.read_state(st)
    assert st.get("pv1.W") == pytest.approx(65000.0)  # good register still landed


def test_write_setpoints_channel_subset_skips_other_registers():
    prof = DeviceProfile.load(HUAWEI)
    reg_wset = profile_reg(prof, "pv.WSet")
    client = FakeModbusClient({})
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    setpoint_w = 5000.0
    st.set("pv1.WSet", setpoint_w)
    drv.write_setpoints(st, channels=set())            # nothing due
    assert client.writes == {}
    drv.write_setpoints(st, channels={"other.WSet"})   # not this device's tag
    assert client.writes == {}
    drv.write_setpoints(st, channels={"pv1.WSet"})     # due → written
    assert client.writes[reg_wset.address] == words_for(setpoint_w, reg_wset)


def test_write_error_response_raises():
    prof = DeviceProfile.load(HUAWEI)
    client = FakeModbusClient({}, fail_writes=True)
    drv = ModbusDeviceDriver(prof, client=client, slave_id=1, prefix="pv1")
    st = SystemState(drv.channels())
    st.set("pv1.WSet", 5000.0)
    with pytest.raises(ModbusWriteError, match="pv1.WSet"):
        drv.write_setpoints(st)


def test_shared_client_keeps_per_driver_slave_id():
    prof = DeviceProfile.load(HUAWEI)
    reg_w = profile_reg(prof, "pv.W")
    client = FakeModbusClient({reg_w.address: words_for(1000.0, reg_w)})
    d1 = ModbusDeviceDriver(prof, client=client, slave_id=0, prefix="plant")
    d2 = ModbusDeviceDriver(prof, client=client, slave_id=11, prefix="meter")

    d1.read_state(SystemState(d1.channels()))
    d2.read_state(SystemState(d2.channels()))

    assert any(slave == 0 for _, _, slave in client.reads)
    assert any(slave == 11 for _, _, slave in client.reads)


# ── word order / byte swap (32-bit endianness) ───────────────────────────────
def test_decode_int32_word_order_little():
    r = RegisterDef("x", 0, "uint32", 1.0, "W", "read", word_order="little")
    # 0x00010000 low-word-first → regs [low=0x0000, high=0x0001]
    assert _decode([0x0000, 0x0001], r) == 0x00010000


def test_encode_word_order_little_is_reversed_words():
    little = RegisterDef("x", 0, "int32", 1.0, "W", "read_write", word_order="little")
    big = RegisterDef("x", 0, "int32", 1.0, "W", "read_write")
    assert _encode(123456, little) == list(reversed(_encode(123456, big)))
    assert _decode(_encode(-2, little), little) == -2  # roundtrip


def test_byte_swap_roundtrip_and_swaps_bytes():
    r = RegisterDef("x", 0, "uint16", 1.0, "W", "read", byte_swap=True)
    assert _encode(0x1234, r) == [0x3412]
    assert _decode(_encode(0x1234, r), r) == 0x1234


def test_word_order_invalid_rejected_at_load():
    with pytest.raises(ValueError, match="word_order"):
        RegisterDef("x", 0, "uint32", 1.0, "W", "read", word_order="middle")


# ── write range guard (no silent two's-complement wrap) ──────────────────────
def _single_reg_profile(type_, access="read_write", channel="pv.WSet", **kw):
    return DeviceProfile(
        "test", "modbus_tcp", 502,
        [RegisterDef(channel, 0, type_, 1.0, "W", access, **kw)],
    )


def test_write_out_of_range_is_refused_not_wrapped():
    drv = ModbusDeviceDriver(_single_reg_profile("uint16"), client=FakeModbusClient({}))
    st = SystemState(drv.channels())
    st.set("pv.WSet", -1000.0)  # negative into uint16 would wrap to 64536
    with pytest.raises(ModbusWriteError, match="pv.WSet"):
        drv.write_setpoints(st)
    assert drv._client.writes == {}  # nothing reached the bus


def test_write_in_range_still_succeeds():
    client = FakeModbusClient({})
    drv = ModbusDeviceDriver(_single_reg_profile("int16"), client=client)
    st = SystemState(drv.channels())
    st.set("pv.WSet", -1000.0)  # valid for int16
    drv.write_setpoints(st)
    assert client.writes[0] == _encode(-1000, drv._profile.registers[0])


# ── FC04 input registers / FC06 single-register write ────────────────────────
def test_input_register_read_uses_fc04():
    prof = _single_reg_profile("uint16", access="read", channel="grid.W",
                               register_type="input")
    client = FakeModbusClient({0: [1234]})
    drv = ModbusDeviceDriver(prof, client=client)
    drv.read_state(SystemState(drv.channels()))
    assert client.input_reads and not client.reads  # FC04 used, never FC03


def test_write_single_register_uses_fc06():
    client = FakeModbusClient({})
    drv = ModbusDeviceDriver(_single_reg_profile("uint16", write_single=True), client=client)
    st = SystemState(drv.channels())
    st.set("pv.WSet", 50.0)
    drv.write_setpoints(st)
    assert client.single_writes[0] == 50
    assert client.writes == {}  # FC16 not used


def test_input_register_must_be_read_only():
    with pytest.raises(ValueError, match="input registers are read-only"):
        RegisterDef("x", 0, "uint16", 1.0, "W", "read_write", register_type="input")


def test_write_single_requires_one_register():
    with pytest.raises(ValueError, match="write_single"):
        RegisterDef("x", 0, "uint32", 1.0, "W", "read_write", write_single=True)


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
