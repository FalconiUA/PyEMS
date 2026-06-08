"""
Generic, profile-driven Modbus device driver.

Register maps live in YAML profile files (data), NOT in code. One profile per
device model. Adding a new device = add a YAML, zero code changes.

This mirrors how SunSpec / OpenEMS / Elum eConf treat device definitions:
the I/O mapping is configuration of a resource, not a program.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from pymodbus.client import ModbusSerialClient, ModbusTcpClient

from src.channels import Channel, SystemState
from src.drivers.base import Driver

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
    ) -> "ModbusDeviceDriver":
        profile = DeviceProfile.load(profile_path)
        if profile.protocol == "modbus_tcp":
            client = ModbusTcpClient(host, port=port or profile.default_port)
        elif profile.protocol == "modbus_rtu":
            client = ModbusSerialClient(port=host)  # host = serial port path
        else:
            raise ValueError(f"Unknown protocol: {profile.protocol}")
        return cls(profile, client, slave_id, prefix)

    def connect(self) -> None:
        self._client.connect()

    def disconnect(self) -> None:
        self._client.close()

    def channels(self) -> list[Channel]:
        return [
            replace(ch, name=self._tag[ch.name]) for ch in self._profile.channels()
        ]

    def read_state(self, state: SystemState) -> None:
        for reg in self._profile.registers:
            result = self._client.read_holding_registers(
                reg.address, count=reg.count, slave=self._slave
            )
            if result.isError():
                continue
            state._channels[self._tag[reg.channel]].value = (
                _decode(result.registers, reg) * reg.scale
            )

    def write_setpoints(self, state: SystemState) -> None:
        for reg in self._profile.registers:
            if not reg.writable:
                continue
            raw = int(state.get(self._tag[reg.channel]) / reg.scale)
            self._client.write_registers(reg.address, _encode(raw, reg), slave=self._slave)
