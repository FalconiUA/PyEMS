"""
Driver = IEC 61131-3 hardware binding (I/O mapping §2.4.1.1).
Maps physical registers/signals ↔ named channels with engineering units.
Modbus registers, CAN frames, MQTT topics — all hidden behind this interface.
"""
from abc import ABC, abstractmethod

from src.channels import SystemState


class Driver(ABC):
    @abstractmethod
    def read_state(self, state: SystemState) -> None:
        """Read hardware → fill measurement channels in state."""

    @abstractmethod
    def write_setpoints(self, state: SystemState) -> None:
        """Read writable channels from state → write to hardware."""
