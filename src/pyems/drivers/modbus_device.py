"""
Generic, profile-driven Modbus device driver.

Register maps live in YAML profile files (data), NOT in code. One profile per
device model. Adding a new device = add a YAML, zero code changes.

This mirrors how SunSpec / OpenEMS / Elum eConf treat device definitions:
the I/O mapping is configuration of a resource, not a program.
"""
from __future__ import annotations

import logging
import inspect
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from pymodbus.client import ModbusSerialClient, ModbusTcpClient

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver

logger = logging.getLogger(__name__)

# registers per Modbus data type
_REG_COUNT = {"int16": 1, "uint16": 1, "int32": 2, "uint32": 2}


def namespaced(local: str, prefix: str | None) -> str:
    """Apply a device instance prefix to a profile-local channel name.

    Profiles name channels '<class>.<field>' (e.g. 'pv.W'). The site assigns
    each device an instance id; that id replaces the class segment so two
    identical devices get distinct tags ('pv1.W', 'pv2.W'). With no prefix the
    profile name is kept verbatim (single-device sites need no id).
    """
    if not prefix:
        return local
    _head, dot, tail = local.partition(".")
    return f"{prefix}.{tail}" if dot else f"{prefix}.{local}"


@dataclass
class RegisterDef:
    channel: str
    address: int
    type: str
    scale: float
    unit: str
    access: str  # "read" | "read_write"
    min_val: float = float("-inf")
    max_val: float = float("inf")

    @property
    def count(self) -> int:
        return _REG_COUNT[self.type]

    @property
    def signed(self) -> bool:
        return self.type.startswith("int")

    @property
    def writable(self) -> bool:
        return self.access == "read_write"


@dataclass
class DeviceProfile:
    model: str
    protocol: str
    default_port: int
    registers: list[RegisterDef]

    @classmethod
    def load(cls, path: str | Path) -> "DeviceProfile":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        regs = [RegisterDef(**r) for r in data["registers"]]
        return cls(
            model=data["model"],
            protocol=data["protocol"],
            default_port=data.get("default_port", 502),
            registers=regs,
        )

    def channels(self) -> list[Channel]:
        """Derive SystemState channels directly from the profile."""
        return [
            Channel(
                name=r.channel,
                unit=r.unit,
                min_val=r.min_val,
                max_val=r.max_val,
                writable=r.writable,
            )
            for r in self.registers
        ]


def _decode(raw_regs: list[int], reg: RegisterDef) -> int:
    if reg.count == 2:
        value = (raw_regs[0] << 16) | raw_regs[1]
        bits = 32
    else:
        value = raw_regs[0]
        bits = 16
    if reg.signed and value >= (1 << (bits - 1)):
        value -= 1 << bits
    return value


def _encode(value: int, reg: RegisterDef) -> list[int]:
    if reg.count == 2:
        v = value & 0xFFFFFFFF
        return [(v >> 16) & 0xFFFF, v & 0xFFFF]
    return [value & 0xFFFF]


def _modbus_unit_kw(method) -> str | None:
    """Return the unit/slave keyword accepted by this pymodbus client version."""
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return "device_id"
    for name in ("device_id", "slave", "unit"):
        if name in params:
            return name
    return None


def _read_holding_registers(client, address: int, count: int, slave_id: int):
    method = client.read_holding_registers
    unit_kw = _modbus_unit_kw(method)
    if unit_kw is None:
        return method(address, count=count)
    return method(address, count=count, **{unit_kw: slave_id})


def _write_registers(client, address: int, values: list[int], slave_id: int):
    method = client.write_registers
    unit_kw = _modbus_unit_kw(method)
    if unit_kw is None:
        return method(address, values)
    return method(address, values, **{unit_kw: slave_id})


class ModbusDeviceDriver(Driver):
    def __init__(
        self, profile: DeviceProfile, client, slave_id: int = 1, prefix: str | None = None
    ) -> None:
        self._profile = profile
        self._client = client
        self._slave = slave_id
        self._prefix = prefix
        # profile-local channel name → namespaced state tag (see namespaced()).
        self._tag = {r.channel: namespaced(r.channel, prefix) for r in profile.registers}

    @classmethod
    def from_profile(
        cls,
        profile_path: str | Path,
        host: str,
        port: int | None = None,
        slave_id: int = 1,
        prefix: str | None = None,
        client=None,
    ) -> "ModbusDeviceDriver":
        profile = DeviceProfile.load(profile_path)
        if client is None:
            if profile.protocol == "modbus_tcp":
                client = ModbusTcpClient(host, port=port or profile.default_port)
            elif profile.protocol == "modbus_rtu":
                client = ModbusSerialClient(port=host)  # host = serial port path
            else:
                raise ValueError(f"Unknown protocol: {profile.protocol}")
        return cls(profile, client, slave_id, prefix)

    def connection_identity(self) -> object:
        """Identity used by CompositeDriver to connect a shared client once."""
        return self._client

    def connect(self) -> None:
        ok = self._client.connect()
        logger.info(
            "Connecting %s (%s, slave %d, prefix %r): %s",
            self._profile.model, self._profile.protocol, self._slave, self._prefix,
            "ok" if ok else "FAILED",
        )

    def disconnect(self) -> None:
        self._client.close()

    def channels(self) -> list[Channel]:
        return [
            replace(ch, name=self._tag[ch.name]) for ch in self._profile.channels()
        ]

    def read_state(self, state: SystemState) -> None:
        for reg in self._profile.registers:
            result = _read_holding_registers(self._client, reg.address, reg.count, self._slave)
            if result.isError():
                logger.debug("Read error %s @%d (%s)", reg.channel, reg.address, result)
                continue
            state._channels[self._tag[reg.channel]].value = (
                _decode(result.registers, reg) * reg.scale
            )

    def write_setpoints(self, state: SystemState) -> None:
        for reg in self._profile.registers:
            if not reg.writable:
                continue
            raw = int(state.get(self._tag[reg.channel]) / reg.scale)
            _write_registers(self._client, reg.address, _encode(raw, reg), self._slave)
