"""Available-power tracking: cap the setpoint near actual unit output.

Problem: when the unit cannot reach the commanded active power (clouds, fuel
derate), the regulation layer happily parks the setpoint at the export-limit
cap — tens of kW above what the unit is producing. The inverter ignores the
excess, but the EMS has silently lost gradient control: the moment the
resource returns (cloud edge passes), production JUMPS to the inflated
setpoint at the inverter's own speed, bypassing the configured active power
gradient and spiking export over the limit until curtailment walks it back.

Fix: a standing pure constraint `max_w = P_unit + headroom_w` posted every
cycle. The setpoint can stay at most `headroom_w` above what the unit
actually delivers, so a returning resource raises production stepwise:
production climbs toward the cap, the cap follows production up — and the
allocator's up-ramp stays the binding gradient. The constraint composes with
everything else by interval intersection; a priority-0 safety claim still
overrides it (the conflicting lower-priority range is discarded whole).

IEC 61131-3 equivalent:
  FUNCTION_BLOCK SetpointHeadroomLimiter
    VAR_INPUT
      p_unit_w   : REAL;  (* measured unit active power, >= 0 generating *)
      headroom_w : REAL;  (* allowed setpoint excess above production *)
    END_VAR
    VAR_OUTPUT
      p_setpoint_cap_w : REAL;  (* max_w = max(0, p_unit) + headroom *)
    END_VAR
  END_FUNCTION_BLOCK
"""
import logging

from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import SystemState
from pyems.controllers.base import Controller

logger = logging.getLogger(__name__)


class SetpointHeadroomLimiter(Controller):
    def __init__(
        self,
        name: str,
        priority: int,
        headroom_w: float,
        unit_active_power_channel: str,
        unit_active_power_setpoint_channel: str,
        headroom_pct: float = 0.0,
    ) -> None:
        """`headroom_w` is the absolute FLOOR of the allowed excess;
        `headroom_pct` (percent of current unit output) makes the headroom
        dynamic: cap = P_unit + max(headroom_w, headroom_pct/100 * P_unit).
        The floor keeps the unit startable at zero production, where any
        relative term vanishes."""
        if headroom_w <= 0:
            raise ValueError(
                "headroom_w must be > 0 — with no headroom the setpoint could "
                "never rise above current production and the unit would be "
                "locked at its present output"
            )
        if headroom_pct < 0:
            raise ValueError("headroom_pct must be >= 0")
        if priority == 0:
            raise ValueError("priority 0 is reserved for safety claims")
        self._name = name
        self._priority = priority
        self._headroom_w = float(headroom_w)
        self._headroom_pct = float(headroom_pct)
        self._unit_active_power_ch = unit_active_power_channel
        self._setpoint_ch = unit_active_power_setpoint_channel

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        p_unit_w = state.get(self._unit_active_power_ch)
        # Generating convention: standby self-consumption (slightly negative
        # readings) must not drag the cap below the headroom itself.
        base_w = max(0.0, p_unit_w)
        headroom_w = max(self._headroom_w, base_w * self._headroom_pct / 100.0)
        cap_w = base_w + headroom_w
        board.post(
            self._setpoint_ch,
            ActivePowerRequest(
                requester=self._name,
                priority=self._priority,
                max_w=cap_w,  # pure constraint: no target, min stays -inf
            ),
        )
        logger.debug(
            "%s: P_unit=%.0f W -> setpoint cap %.0f W (headroom %.0f W)",
            self._setpoint_ch, p_unit_w, cap_w, headroom_w,
        )
