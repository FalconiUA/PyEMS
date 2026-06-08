"""
Active-power export limitation at the connection point (EN 50549-1 §4.6.2
"active power constraint", ENTSO-E NC RfG active-power control). Caps the
active power fed into the network by curtailing a generating unit.

Terminology follows EN 50549 / ENTSO-E RfG, not vendor jargon:
  - connection point  : point where the plant joins the network (POC/PCC).
  - P                 : active power [W].
  - P_max             : maximum active power of the unit (RfG "Maximum
                        Capacity") — the upper clamp for the setpoint.
  - active power setpoint : the P command sent to the unit.
The unit may be any generator (PV inverter, genset, storage) — the controller
is unit-agnostic and bound to tags per device in site.yaml.

Sign convention at the connection point (import positive):
    P_cp > 0  → importing from the network
    P_cp < 0  → exporting to the network
Export magnitude = max(0, -P_cp). Kept at or below export_limit_w.

IEC 61131-3 equivalent:
  FUNCTION_BLOCK GridExportLimit
    VAR_INPUT
      p_connection_point_w : REAL;   (* + = import, - = export *)
      p_unit_w             : REAL;   (* unit active power, >= 0 *)
      export_limit_w       : REAL;   (* allowed export magnitude, >= 0 *)
    END_VAR
    VAR_OUTPUT
      p_setpoint_w : REAL;           (* active power setpoint to the unit *)
    END_VAR
    VAR
      last_setpoint : REAL;          (* RETAIN: persists between cycles *)
    END_VAR
  END_FUNCTION_BLOCK

Control law (feed-forward, self-correcting each scan):
  Reducing unit power by ΔP raises P_cp by ΔP, so to move P_cp up to the
  most-negative allowed value (P_cp_min = -export_limit) the required active
  power setpoint is:
        p_setpoint = p_unit + (P_cp - P_cp_min)
                   = p_unit + P_cp + export_limit_w
  When not over-exporting this exceeds p_unit → clamps to P_max → unit runs
  free. Because p_unit is re-measured every cycle, the loop converges (deadbeat).
"""
import logging

from src.channels import SystemState
from src.controllers.base import Controller
from src.controllers.safety import SAFE_MODE_CHANNEL

logger = logging.getLogger(__name__)


class GridExportLimitController(Controller):
    def __init__(
        self,
        cycle_s: float,
        export_limit_w: float,
        p_max_w: float,
        connection_point_active_power_channel: str,
        unit_active_power_channel: str,
        unit_active_power_setpoint_channel: str,
        deadband_w: float = 200.0,
        ramp_rate_w_per_s: float = 5000.0,
    ) -> None:
        if export_limit_w < 0:
            raise ValueError("export_limit_w must be >= 0 (magnitude)")
        self._export_limit_w = export_limit_w
        self._p_max_w = p_max_w  # unit maximum active power (RfG Maximum Capacity)
        # IEC VAR_INPUT/VAR_OUTPUT binding — which tags this instance reads/drives.
        # Set per device from site.yaml, so the same class serves any unit.
        # All three are ACTIVE power (P, W) — distinct from reactive (Q) / apparent (S).
        self._cp_active_power_ch = connection_point_active_power_channel
        self._unit_active_power_ch = unit_active_power_channel
        self._setpoint_ch = unit_active_power_setpoint_channel
        self._deadband_w = deadband_w
        self._max_ramp = ramp_rate_w_per_s * cycle_s  # active power gradient limit
        # Fail-safe default: full power = no curtailment until first scan computes.
        self._last_setpoint = p_max_w  # VAR RETAIN
        self._curtailing = False  # last state — log only on transition, not per cycle

    def execute(self, state: SystemState) -> None:
        # Yield to the PRIORITY 0 safety interlock: when tripped, the
        # SafetyController owns the setpoint. Resync RETAIN so we resume smoothly
        # (ramp up from the forced safe value, not from a stale pre-trip target).
        if state.get(SAFE_MODE_CHANNEL) >= 0.5:
            self._last_setpoint = state.get(self._setpoint_ch)
            return

        # VAR_INPUT reads (P = active power)
        p_cp = state.get(self._cp_active_power_ch)      # + import, - export (connection point)
        p_unit = state.get(self._unit_active_power_ch)  # actual unit active power

        # feed-forward target setpoint (see module docstring derivation)
        target = p_unit + p_cp + self._export_limit_w

        # clamp to unit active-power limits (0 .. P_max)
        target = max(0.0, min(self._p_max_w, target))

        # deadband: ignore micro-adjustments to avoid hunting the setpoint
        if abs(target - self._last_setpoint) < self._deadband_w:
            target = self._last_setpoint

        # rate-limit (active power gradient) — never slam the setpoint
        delta = target - self._last_setpoint
        delta = max(-self._max_ramp, min(self._max_ramp, delta))
        setpoint = self._last_setpoint + delta

        self._last_setpoint = setpoint  # persist for next cycle (RETAIN)

        # Log curtailment as a state transition. We are actually curtailing only
        # when the cap holds production BELOW what the unit is currently making
        # (setpoint < measured P) — not merely when setpoint < P_max, since the
        # deadbeat law usually sits below P_max even when running free.
        curtailing = setpoint < p_unit - self._deadband_w
        if curtailing and not self._curtailing:
            logger.info(
                "Export-limit ENGAGED: P_cp=%.0f W, capping %s to %.0f W (limit %.0f W)",
                p_cp, self._setpoint_ch, setpoint, self._export_limit_w,
            )
        elif not curtailing and self._curtailing:
            logger.info("Export-limit RELEASED: %s back to P_max", self._setpoint_ch)
        self._curtailing = curtailing
        logger.debug(
            "%s: P_cp=%.0f P_unit=%.0f -> setpoint=%.0f W", self._setpoint_ch, p_cp, p_unit, setpoint
        )

        # VAR_OUTPUT write
        state.set(self._setpoint_ch, setpoint)
