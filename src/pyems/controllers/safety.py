"""
SafetyController = IEC 61131-3 PRIORITY 0 interlock layer.

Runs first every cycle (highest priority TASK). It does not optimize — it only
asserts a safe state when preconditions fail. Right now it guards one fault:
stale measurements (the field bus went down) — see CachedDriver.age_s().

Interlock pattern (how safety wins despite running before normal control):
  - Safety sets the system tag `sys.safe_mode` (1 = tripped, 0 = healthy).
  - Lower-priority controllers read `sys.safe_mode` at the top and YIELD when set,
    so they never overwrite the safe setpoint. Priority order is honored AND
    safety has the final say. This is the classic PLC permissive/interlock bit.

Fail-safe value for export-limit mode:
  With no connection-point measurement we cannot know the real export, so we cap
  unit active power at the export limit itself. Then even with zero site load,
  export = limit (never exceeds it); any real load makes export smaller. This
  stays compliant while preserving as much generation as is provably safe —
  better than curtailing the unit to 0.

IEC 61131-3 equivalent:
  FUNCTION_BLOCK Safety
    VAR_INPUT  comms_age_s : REAL; END_VAR
    VAR_OUTPUT safe_mode : BOOL; p_setpoint_w : REAL; END_VAR
  END_FUNCTION_BLOCK
"""
import logging

from pyems.channels import SystemState
from pyems.controllers.base import Controller
from pyems.drivers.cached import COMMS_AGE_CHANNEL

logger = logging.getLogger(__name__)

SAFE_MODE_CHANNEL = "sys.safe_mode"  # 1.0 = tripped, 0.0 = healthy


class SafetyController(Controller):
    def __init__(
        self,
        max_comms_age_s: float,
        safe_active_power_w: float,
        unit_active_power_setpoint_channels: list[str],
    ) -> None:
        self._max_age = max_comms_age_s
        self._safe_active_power_w = safe_active_power_w
        # Generating-unit active-power setpoint tags to cap on trip — one per unit.
        self._setpoint_channels = unit_active_power_setpoint_channels
        self._tripped = False  # last state — log only on transition, not per cycle

    def execute(self, state: SystemState) -> None:
        # VAR_INPUT
        comms_age = state.get(COMMS_AGE_CHANNEL)

        if comms_age > self._max_age:
            # bus is stale → trip: assert interlock and force the safe setpoint
            state.set(SAFE_MODE_CHANNEL, 1.0)
            for ch in self._setpoint_channels:
                state.set(ch, self._safe_active_power_w)
            if not self._tripped:
                logger.warning(
                    "SAFETY TRIP: comms age %.1fs > %.1fs limit; capping %s to %.0f W",
                    comms_age, self._max_age, self._setpoint_channels,
                    self._safe_active_power_w,
                )
                self._tripped = True
        else:
            state.set(SAFE_MODE_CHANNEL, 0.0)
            if self._tripped:
                logger.info("SAFETY RELEASE: comms age %.1fs back within limit", comms_age)
                self._tripped = False
