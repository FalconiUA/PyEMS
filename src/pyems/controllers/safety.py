"""
SafetyController = IEC 61131-3 PRIORITY 0 interlock layer.

Runs first every cycle (highest priority TASK). It does not optimize — it only
asserts a safe state when preconditions fail. Right now it guards one fault:
stale measurements (the field bus went down) — see CachedDriver.age_s().

Interlock pattern (how safety wins despite running before normal control):
  - On trip, safety posts a PRIORITY 0 claim (min=max=target=safe value) on each
    guarded setpoint channel. The PowerAllocator honors priority 0 above every
    other requester and bypasses deadband/ramp for it, so the safe value lands
    exactly, in one cycle. On release, safety withdraws the claims and the
    allocator ramps smoothly back toward whatever the economic layer wants.
  - `sys.safe_mode` (1 = tripped, 0 = healthy) is kept as an observability/status
    word for UI and history — it is no longer a behavioral interlock other
    controllers must check.

Fail-safe value for export-limit mode:
  With no connection-point measurement we cannot know the real export, so we cap
  unit active power at the export limit itself. Then even with zero site load,
  export = limit (never exceeds it); any real load makes export smaller. This
  stays compliant while preserving as much generation as is provably safe —
  better than curtailing the unit to 0.

IEC 61131-3 equivalent:
  FUNCTION_BLOCK Safety
    VAR_INPUT  comms_age_s : REAL; END_VAR
    VAR_OUTPUT safe_mode : BOOL; p_setpoint_claim_w : REAL; END_VAR
  END_FUNCTION_BLOCK
"""
import logging

from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import SystemState
from pyems.controllers.base import Controller
from pyems.drivers.cached import COMMS_AGE_CHANNEL

logger = logging.getLogger(__name__)

SAFE_MODE_CHANNEL = "sys.safe_mode"  # 1.0 = tripped, 0.0 = healthy
SAFETY_REQUESTER = "safety"          # board key + reserved priority-0 owner


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

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        # VAR_INPUT
        comms_age = state.get(COMMS_AGE_CHANNEL)

        if comms_age > self._max_age:
            # bus is stale → trip: pin each guarded setpoint to the safe value via
            # a priority-0 claim (min=max=target), and raise the status word.
            for ch in self._setpoint_channels:
                board.post(
                    ch,
                    ActivePowerRequest(
                        requester=SAFETY_REQUESTER,
                        priority=0,
                        min_w=self._safe_active_power_w,
                        max_w=self._safe_active_power_w,
                        target_w=self._safe_active_power_w,
                        ttl_s=None,  # holds until release withdraws it
                    ),
                )
            state.set(SAFE_MODE_CHANNEL, 1.0)
            if not self._tripped:
                logger.warning(
                    "SAFETY TRIP: comms age %.1fs > %.1fs limit; pinning %s to %.0f W",
                    comms_age, self._max_age, self._setpoint_channels,
                    self._safe_active_power_w,
                )
                self._tripped = True
        else:
            # healthy → withdraw the claims (allocator ramps back up) and clear flag.
            for ch in self._setpoint_channels:
                board.withdraw(ch, SAFETY_REQUESTER)
            state.set(SAFE_MODE_CHANNEL, 0.0)
            if self._tripped:
                logger.info("SAFETY RELEASE: comms age %.1fs back within limit", comms_age)
                self._tripped = False
