"""
Channels = IEC 61131-3 §2.4 typed variables with engineering units.
Each channel is a named tag: battery.soc, inverter.setpoint_w, etc.
"""
from dataclasses import dataclass


@dataclass
class Channel:
    name: str
    value: float = 0.0
    unit: str = ""
    min_val: float = float("-inf")
    max_val: float = float("inf")
    writable: bool = False  # True = setpoint (VAR_OUTPUT to hardware)


class SystemState:
    """
    Shared tag database for one scan cycle.
    Controllers read inputs and write setpoints here — never directly to hardware.
    IEC equivalent: global variable pool visible across all POUs in a resource.
    """

    def __init__(self, channels: list[Channel]) -> None:
        self._channels: dict[str, Channel] = {ch.name: ch for ch in channels}

    def get(self, name: str) -> float:
        return self._channels[name].value

    def set(self, name: str, value: float) -> None:
        ch = self._channels[name]
        if not ch.writable:
            raise ValueError(f"Channel '{name}' is read-only")
        ch.value = max(ch.min_val, min(ch.max_val, value))

    def snapshot(self) -> dict[str, float]:
        return {name: ch.value for name, ch in self._channels.items()}
