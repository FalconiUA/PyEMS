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
  With no grid measurement we cannot know the real export, so we cap PV at the
  export limit itself. Then even with zero site load, export = limit (never
  exceeds it); any real load makes export smaller. This stays compliant while
  preserving as much PV as is provably safe — better than killing PV to 0.

IEC 61131-3 equivalent:
  FUNCTION_BLOCK Safety
    VAR_INPUT  comms_age_s : REAL; END_VAR
    VAR_OUTPUT safe_mode : BOOL; pv_wset_w : REAL; END_VAR
  END_FUNCTION_BLOCK
"""
from src.channels import SystemState
from src.controllers.base import Controller
from src.drivers.cached import COMMS_AGE_CHANNEL

SAFE_MODE_CHANNEL = "sys.safe_mode"  # 1.0 = tripped, 0.0 = healthy


class SafetyController(Controller):
    def __init__(self, max_comms_age_s: float, safe_wset_w: float) -> None:
        self._max_age = max_comms_age_s
        self._safe_wset_w = safe_wset_w

    def execute(self, state: SystemState) -> None:
        # VAR_INPUT
        comms_age = state.get(COMMS_AGE_CHANNEL)

        if comms_age > self._max_age:
            # bus is stale → trip: assert interlock and force the safe setpoint
            state.set(SAFE_MODE_CHANNEL, 1.0)
            state.set("pv.WSet", self._safe_wset_w)
        else:
            state.set(SAFE_MODE_CHANNEL, 0.0)
