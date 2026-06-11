"""
SafetyController = IEC 61131-3 PRIORITY 0 interlock layer.

Runs first every cycle (highest priority TASK). It does not optimize — it only
asserts a safe state when preconditions fail. It guards two faults:
  - stale measurements: the field bus went down — see CachedDriver.age_s();
  - frozen measurements: the bus answers but a watched tag has not changed for
    too long. A gateway serving cached register data (or a meter whose CPU
    hung) passes every Modbus read, so the comms age never grows — yet acting
    on a frozen connection-point measurement can silently violate the export
    limit. Bit-identical repeats are the freeze signature: a live power
    measurement always jitters at least one LSB. Watch jittery tags only
    (the connection-point meter, not pv.W, which sits at exactly 0 all night).

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
# Names live in pyems.system_tags (single place for all sys.* and requester
# names); re-exported here so existing imports keep working.
from pyems.system_tags import (
    SAFE_MODE_CHANNEL,  # 1.0 = tripped, 0.0 = healthy
    SAFETY_REQUESTER,   # board key + reserved priority-0 owner
)

logger = logging.getLogger(__name__)


class SafetyController(Controller):
    def __init__(
        self,
        max_comms_age_s: float,
        safe_active_power_w: float,
        unit_active_power_setpoint_channels: list[str],
        frozen_measurement_channels: list[str] | None = None,
        max_frozen_s: float | None = None,
    ) -> None:
        self._max_age = max_comms_age_s
        self._safe_active_power_w = safe_active_power_w
        # Generating-unit active-power setpoint tags to cap on trip — one per unit.
        self._setpoint_channels = unit_active_power_setpoint_channels
        # Frozen-measurement guard (disabled unless both are configured):
        # measurement tags that must keep changing, and for how long a
        # bit-identical value is still plausible.
        self._frozen_channels = frozen_measurement_channels or []
        self._max_frozen_s = max_frozen_s
        # per-channel (last value, monotonic time it last changed)
        self._last_change: dict[str, tuple[float, float]] = {}
        self._tripped = False  # last state — log only on transition, not per cycle

    def _frozen_measurements(self, state: SystemState, now: float) -> list[str]:
        """Watched tags whose value has not changed for longer than allowed."""
        if self._max_frozen_s is None:
            return []
        frozen: list[str] = []
        for ch in self._frozen_channels:
            value = state.get(ch)
            last = self._last_change.get(ch)
            if last is None or value != last[0]:
                self._last_change[ch] = (value, now)
            elif now - last[1] > self._max_frozen_s:
                frozen.append(ch)
        return frozen

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        # VAR_INPUT
        comms_age = state.get(COMMS_AGE_CHANNEL)
        frozen = self._frozen_measurements(state, board.now)

        faults: list[str] = []
        if comms_age > self._max_age:
            faults.append(f"comms age {comms_age:.1f}s > {self._max_age:.1f}s limit")
        if frozen:
            faults.append(
                f"measurements {frozen} frozen > {self._max_frozen_s:.1f}s"
            )

        if faults:
            # preconditions failed → trip: pin each guarded setpoint to the safe
            # value via a priority-0 claim (min=max=target), raise the status word.
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
                    "SAFETY TRIP: %s; pinning %s to %.0f W",
                    "; ".join(faults), self._setpoint_channels,
                    self._safe_active_power_w,
                )
                self._tripped = True
        else:
            # healthy → withdraw the claims (allocator ramps back up) and clear flag.
            for ch in self._setpoint_channels:
                board.withdraw(ch, SAFETY_REQUESTER)
            state.set(SAFE_MODE_CHANNEL, 0.0)
            if self._tripped:
                logger.info("SAFETY RELEASE: all preconditions healthy again")
                self._tripped = False
