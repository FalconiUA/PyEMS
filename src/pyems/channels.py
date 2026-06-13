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
    # True = discrete COMMAND register (e.g. remote start/stop): writable, but
    # written only via a one-shot forced command, never mirrored by the
    # continuous setpoint flush / keep-alive rewrite (see CachedDriver).
    command: bool = False


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

    def apply_driver_value(self, name: str, value: float) -> None:
        """Driver-side update: a value observed on the hardware (or I/O cache).

        Unlike set(), this bypasses the writable check (drivers refresh
        measurements and setpoint readbacks alike) and does NOT clamp: the
        state records what the hardware reported. Plausibility checks on
        inputs are the driver's job (see ModbusDeviceDriver.read_state);
        clamping applies only to values WE command, in set().
        """
        self._channels[name].value = value

    def __contains__(self, name: str) -> bool:
        return name in self._channels

    def snapshot(self) -> dict[str, float]:
        return {name: ch.value for name, ch in self._channels.items()}
