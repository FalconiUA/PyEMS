"""
SetpointComplianceMonitor = actuator monitoring for the active power setpoint.

A Modbus ACK proves the register was written, not that the command acts: most
inverters (e.g. Huawei SUN2000) silently ignore the active-power setpoint until
remote power control is enabled during commissioning. Without a read-back
check the EMS believes it is curtailing while the unit keeps full output — the
export limit is violated with no fault anywhere.

This block compares the unit's measured active power against the *applied*
setpoint (the allocator-resolved value, post ramp/clamp). Only sustained
OVERSHOOT is a fault: the setpoint is a cap in generating-unit convention, so
a PV unit producing below it (clouds, night) is normal. The tolerance plus the
violation window absorb measurement noise and the unit's own response lag.

There is no actuation path that can fix a unit ignoring its commands — writing
harder will not help. The block therefore raises `sys.setpoint_violation`
(status word for UI/SCADA/history) and logs an ERROR transition for the
operator; tripping breakers is plant protection, not EMS logic.

IEC 61131-3 equivalent:
  FUNCTION_BLOCK SetpointCompliance
    VAR_INPUT  p_unit_w : REAL; p_setpoint_w : REAL; END_VAR
    VAR_OUTPUT setpoint_violation : BOOL; END_VAR
  END_FUNCTION_BLOCK
"""
import logging

from pyems.allocation.request import RequestBoard
from pyems.channels import SystemState
from pyems.controllers.base import Controller
# Name lives in pyems.system_tags (single place for all sys.* names);
# re-exported here so existing imports keep working. 1.0 = unit not following.
from pyems.system_tags import SETPOINT_VIOLATION_CHANNEL

logger = logging.getLogger(__name__)


class SetpointComplianceMonitor(Controller):
    def __init__(
        self,
        unit_active_power_channel: str,
        unit_active_power_setpoint_channel: str,
        tolerance_w: float = 2000.0,
        max_violation_s: float = 30.0,
    ) -> None:
        if tolerance_w < 0:
            raise ValueError("tolerance_w must be >= 0")
        if max_violation_s <= 0:
            raise ValueError("max_violation_s must be > 0")
        self._unit_active_power_ch = unit_active_power_channel
        self._setpoint_ch = unit_active_power_setpoint_channel
        self._tolerance_w = tolerance_w
        self._max_violation_s = max_violation_s
        self._over_since: float | None = None  # monotonic start of current overshoot
        self._violating = False  # last state — log only on transition

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        now = board.now
        p_unit_w = state.get(self._unit_active_power_ch)
        p_setpoint_w = state.get(self._setpoint_ch)

        over = p_unit_w > p_setpoint_w + self._tolerance_w
        if not over:
            self._over_since = None
        elif self._over_since is None:
            self._over_since = now

        violating = (
            self._over_since is not None
            and now - self._over_since >= self._max_violation_s
        )
        state.set(SETPOINT_VIOLATION_CHANNEL, 1.0 if violating else 0.0)

        if violating and not self._violating:
            logger.error(
                "SETPOINT VIOLATION: %s=%.0f W exceeds applied %s=%.0f W "
                "by more than %.0f W for over %.0fs — unit is not following "
                "commands (remote power control disabled on the device?)",
                self._unit_active_power_ch, p_unit_w,
                self._setpoint_ch, p_setpoint_w,
                self._tolerance_w, self._max_violation_s,
            )
        elif not violating and self._violating:
            logger.warning(
                "SETPOINT VIOLATION cleared: %s back within %.0f W of %s",
                self._unit_active_power_ch, self._tolerance_w, self._setpoint_ch,
            )
        self._violating = violating
