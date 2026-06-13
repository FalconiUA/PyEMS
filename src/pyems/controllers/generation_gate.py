"""Generation gate: pin the unit to a safe floor until an operator enables it.

Operational interlock (NOT a safety-rated E-stop — see pyems.commands). The
two-level model: the EMS process being alive (safety + polling + control logic)
is separate from generation being permitted. After the EMS starts, generation
is disabled by default; the operator verifies device reads, then enables it from
the UI. `sys.generation_allowed` carries the decision (written fail-closed by
CommandFileReader); this controller turns it into a board claim.

How it wins despite the economic layer wanting more:
  - When disabled, it posts a PRIORITY-1 claim (min=max=target=floor_w) on the
    guarded setpoint channel. Priority 1 sits below safety (0) and above every
    economic requester, so the allocator pins the unit to the floor and discards
    any conflicting lower-priority range — exactly like a safety pin, but it
    yields to a real safety trip.
  - When enabled, it withdraws the claim and the allocator ramps the unit back
    up toward whatever the economic layer wants (the gradient stays the bound).

`floor_w` is chosen by the builder (see ems.py): 0 W when 0 is inside the unit
envelope, else p_min_w — so disabling generation on a storage unit (p_min_w < 0)
parks it at its safe minimum, never forces a charge.

IEC 61131-3 equivalent:
  FUNCTION_BLOCK GenerationGate
    VAR_INPUT  generation_allowed : BOOL; END_VAR
    VAR_OUTPUT gate_active : BOOL; p_setpoint_pin_w : REAL; END_VAR
  END_FUNCTION_BLOCK
"""
import logging

from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import SystemState
from pyems.controllers.base import Controller
from pyems.system_tags import (
    GENERATION_ALLOWED_CHANNEL,
    GENERATION_GATE_ACTIVE_CHANNEL,
)

logger = logging.getLogger(__name__)


class GenerationGateController(Controller):
    def __init__(
        self,
        name: str,
        priority: int,
        unit_active_power_setpoint_channel: str,
        floor_w: float,
    ) -> None:
        if priority == 0:
            raise ValueError("priority 0 is reserved for safety claims")
        self._name = name
        self._priority = priority
        self._setpoint_ch = unit_active_power_setpoint_channel
        self._floor_w = float(floor_w)
        self._pinning = False  # last state — log only on transition

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        allowed = state.get(GENERATION_ALLOWED_CHANNEL) >= 0.5
        if allowed:
            board.withdraw(self._setpoint_ch, self._name)
            state.set(GENERATION_GATE_ACTIVE_CHANNEL, 0.0)
            if self._pinning:
                logger.info("Generation ENABLED: releasing %s", self._setpoint_ch)
                self._pinning = False
        else:
            board.post(
                self._setpoint_ch,
                ActivePowerRequest(
                    requester=self._name,
                    priority=self._priority,
                    min_w=self._floor_w,
                    max_w=self._floor_w,
                    target_w=self._floor_w,
                ),
            )
            state.set(GENERATION_GATE_ACTIVE_CHANNEL, 1.0)
            if not self._pinning:
                logger.info(
                    "Generation DISABLED: pinning %s to %.0f W",
                    self._setpoint_ch, self._floor_w,
                )
                self._pinning = True
