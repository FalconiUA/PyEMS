"""Hard inverter switch: latched remote start/stop as an OPERATOR ACTION.

Second level above the soft generation gate. The gate curtails active power to
a floor (the inverter stays energized); this drives the device's own start/stop
COMMAND register(s), de-energizing it. It is NOT a safety reflex — safety stays
on the priority-0 SafetyController and the device's comms watchdog.

Latched, edge-triggered: the controller fires ONCE per new
`sys.inverter_command_id` (a fresh id is stamped each time the operator presses
Hard start / Hard stop). On EMS startup it touches nothing — `CommandFileReader`
publishes a NaN id for a leftover command from a previous run, so a restart
never re-fires it.

Vendor-flexible: `start_writes` / `stop_writes` are lists of (channel, value)
pairs from `site.yaml` `hard_switch:`. One register run/stop, or two separate
command registers, or any values — the controller just sends the configured
pairs once via the command sink (CachedDriver.send_command), which performs a
single forced write (no continuous mirror, no keep-alive) so it is correct for
pulse and level registers alike.

IEC 61131-3 equivalent:
  FUNCTION_BLOCK HardSwitch
    VAR_INPUT  command : INT; command_id : REAL; END_VAR  (* from the command file *)
    VAR_OUTPUT run_state : INT; END_VAR                   (* last commanded state *)
    VAR        last_id : REAL;  (* RETAIN: id already acted on *)  END_VAR
  END_FUNCTION_BLOCK
"""
import logging
import math
from typing import Protocol

from pyems.allocation.request import RequestBoard
from pyems.channels import SystemState
from pyems.controllers.base import Controller
from pyems.system_tags import (
    INVERTER_COMMAND_CHANNEL,
    INVERTER_COMMAND_ID_CHANNEL,
    INVERTER_RUN_STATE_CHANNEL,
)

logger = logging.getLogger(__name__)


class CommandSink(Protocol):
    """A one-shot forced writer of command registers (CachedDriver implements it)."""

    def send_command(self, tag: str, value: float) -> None: ...


class HardSwitchController(Controller):
    def __init__(
        self,
        command_sink: CommandSink,
        start_writes: list[tuple[str, float]],
        stop_writes: list[tuple[str, float]],
    ) -> None:
        if not start_writes or not stop_writes:
            raise ValueError("hard switch needs non-empty start_writes and stop_writes")
        self._sink = command_sink
        self._start_writes = [(str(ch), float(v)) for ch, v in start_writes]
        self._stop_writes = [(str(ch), float(v)) for ch, v in stop_writes]
        self._last_id: float | None = None  # RETAIN: id we already acted on

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        cmd_id = state.get(INVERTER_COMMAND_ID_CHANNEL)
        if not math.isfinite(cmd_id) or cmd_id == self._last_id:
            return  # no command, leftover from a previous run, or already acted
        self._last_id = cmd_id
        start = state.get(INVERTER_COMMAND_CHANNEL) >= 0.5
        pairs = self._start_writes if start else self._stop_writes
        for channel, value in pairs:
            self._sink.send_command(channel, value)
        state.set(INVERTER_RUN_STATE_CHANNEL, 1.0 if start else 0.0)
        logger.info(
            "Hard inverter %s: sent %s",
            "START" if start else "STOP",
            ", ".join(f"{ch}={v:g}" for ch, v in pairs),
        )
