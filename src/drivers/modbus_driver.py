"""
Modbus TCP driver stub.
Replace register addresses and scaling with your actual device map.
Pattern from GrugBus: one register address → one named channel with scaling.
"""
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient

from src.channels import SystemState
from src.drivers.base import Driver


@dataclass
class RegisterMap:
    channel: str    # target channel name in SystemState
    address: int    # Modbus register address
    scale: float    # raw → engineering unit  (e.g. 0.1 for 10ths of a Watt)
    offset: float = 0.0


class ModbusDriver(Driver):
    # Read-only measurements
    READ_MAP: list[RegisterMap] = [
        RegisterMap("grid.power_w",    address=0x0000, scale=1.0),
        RegisterMap("battery.soc_pct", address=0x0001, scale=0.1),
        RegisterMap("pv.power_w",      address=0x0002, scale=1.0),
    ]

    # Writable setpoints
    WRITE_MAP: list[RegisterMap] = [
        RegisterMap("battery.setpoint_w", address=0x0100, scale=1.0),
    ]

    def __init__(self, host: str, port: int = 502, unit: int = 1) -> None:
        self._client = ModbusTcpClient(host, port=port)
        self._unit = unit

    def connect(self) -> None:
        self._client.connect()

    def disconnect(self) -> None:
        self._client.close()

    def read_state(self, state: SystemState) -> None:
        for reg in self.READ_MAP:
            result = self._client.read_holding_registers(reg.address, count=1, slave=self._unit)
            if not result.isError():
                raw = result.registers[0]
                state._channels[reg.channel].value = raw * reg.scale + reg.offset

    def write_setpoints(self, state: SystemState) -> None:
        for reg in self.WRITE_MAP:
            value = state.get(reg.channel)
            raw = int((value - reg.offset) / reg.scale)
            self._client.write_register(reg.address, raw, slave=self._unit)
