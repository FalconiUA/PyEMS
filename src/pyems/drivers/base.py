"""
Driver = IEC 61131-3 hardware binding (I/O mapping §2.4.1.1).
Maps physical registers/signals ↔ named channels with engineering units.
Modbus registers, CAN frames, MQTT topics — all hidden behind this interface.
"""
from abc import ABC, abstractmethod

from pyems.channels import SystemState


class Driver(ABC):
    @abstractmethod
    def read_state(self, state: SystemState) -> None:
        """Read hardware → fill measurement channels in state."""

    @abstractmethod
    def write_setpoints(self, state: SystemState, channels: set[str] | None = None) -> None:
        """Read writable channels from state → write to hardware.

        `channels` restricts the write to that subset of channel tags
        (None = all writable channels). Drivers skip tags they don't own,
        so a composite can pass one subset to every device.
        """
