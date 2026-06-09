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
      p_setpoint_cap_w : REAL;       (* upper bound posted as a request *)
    END_VAR
  END_FUNCTION_BLOCK

Control law (feed-forward, self-correcting each scan):
  Reducing unit power by ΔP raises P_cp by ΔP, so to move P_cp up to the
  most-negative allowed value (P_cp_min = -export_limit) the required active
  power setpoint is:
        p_setpoint = p_unit + (P_cp - P_cp_min)
                   = p_unit + P_cp + export_limit_w
  Because p_unit is re-measured every cycle, the loop converges (deadbeat).

This controller is a **pure constraint**: it computes the cap and posts it as an
upper bound (`max_w`) request, expressing no preferred value. The PowerAllocator
owns the setpoint channel and applies the device envelope (P_max), ramp limit,
deadband, and arbitration against other requesters. A lower-priority target
request (e.g. a TOU plan) naturally clamps under this cap — no special-casing.
The old safe-mode yield is gone: safety now posts a priority-0 claim the
allocator honors above this one.
"""
import logging

from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import SystemState
from pyems.controllers.base import Controller

logger = logging.getLogger(__name__)


class GridExportLimitController(Controller):
    def __init__(
        self,
        name: str,
        priority: int,
        export_limit_w: float,
        connection_point_active_power_channel: str,
        unit_active_power_channel: str,
        unit_active_power_setpoint_channel: str,
        deadband_w: float = 200.0,
    ) -> None:
        if export_limit_w < 0:
            raise ValueError("export_limit_w must be >= 0 (magnitude)")
        self._name = name              # requester key on the board (unique per instance)
        self._priority = priority      # grid-code compliance band (e.g. 5)
        self._export_limit_w = export_limit_w
        # IEC VAR_INPUT/VAR_OUTPUT binding — which tags this instance reads/drives.
        # Set per device from site.yaml, so the same class serves any unit.
        # All are ACTIVE power (P, W) — distinct from reactive (Q) / apparent (S).
        self._cp_active_power_ch = connection_point_active_power_channel
        self._unit_active_power_ch = unit_active_power_channel
        self._setpoint_ch = unit_active_power_setpoint_channel
        # Hysteresis for the ENGAGED/RELEASED log transition only (control
        # deadband lives in the channel's allocator config, not here).
        self._deadband_w = deadband_w
        self._curtailing = False  # last state — log only on transition, not per cycle

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        # VAR_INPUT reads (P = active power)
        p_cp = state.get(self._cp_active_power_ch)      # + import, - export (connection point)
        p_unit = state.get(self._unit_active_power_ch)  # actual unit active power

        # feed-forward cap (see module docstring derivation). Lower-bounded at 0
        # (a negative export cap would be meaningless); the unit's P_max upper
        # bound is enforced by the allocator's device envelope.
        cap = max(0.0, p_unit + p_cp + self._export_limit_w)

        # VAR_OUTPUT: post the cap as a pure upper-bound constraint (no target).
        board.post(
            self._setpoint_ch,
            ActivePowerRequest(
                requester=self._name,
                priority=self._priority,
                max_w=cap,  # min stays -inf; no target_w
            ),
        )

        # Log curtailment as a state transition. We are actually curtailing only
        # when the cap holds production BELOW what the unit is currently making
        # (cap < measured P) — with hysteresis so the log does not flap.
        curtailing = cap < p_unit - self._deadband_w
        if curtailing and not self._curtailing:
            logger.info(
                "Export-limit ENGAGED: P_cp=%.0f W, capping %s to %.0f W (limit %.0f W)",
                p_cp, self._setpoint_ch, cap, self._export_limit_w,
            )
        elif not curtailing and self._curtailing:
            logger.info("Export-limit RELEASED: %s cap above production", self._setpoint_ch)
        self._curtailing = curtailing
        logger.debug(
            "%s: P_cp=%.0f P_unit=%.0f -> cap=%.0f W", self._setpoint_ch, p_cp, p_unit, cap
        )
