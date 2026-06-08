"""
POC export-limit control: cap power fed to the grid by curtailing PV.

Sign convention (Elum ePowerControl §2.5.3 Table 7, grid.W):
    grid.W > 0  → importing from grid
    grid.W < 0  → exporting to grid
Export magnitude = max(0, -grid.W). We keep it at or below export_limit_w.

Actuator: pv.WSet — the inverter's max active-power setpoint (Huawei
"Active power fixed value derated", W). Lowering it curtails PV.

IEC 61131-3 equivalent:
  FUNCTION_BLOCK GridExportLimit
    VAR_INPUT
      grid_power_w   : REAL;   (* + = import, - = export *)
      pv_power_w     : REAL;   (* actual PV active power, >= 0 *)
      export_limit_w : REAL;   (* allowed export magnitude, >= 0 *)
    END_VAR
    VAR_OUTPUT
      pv_wset_w : REAL;        (* max active-power setpoint to inverter *)
    END_VAR
    VAR
      last_setpoint : REAL;    (* RETAIN: persists between cycles *)
    END_VAR
  END_FUNCTION_BLOCK

Control law (feed-forward, self-correcting each scan):
  Reducing PV by ΔP raises grid.W by ΔP, so to move grid.W up to the
  most-negative allowed value (grid_min = -export_limit) the required
  PV cap is:
        target_WSet = pv_w + (grid_w - grid_min)
                    = pv_w + grid_w + export_limit_w
  When not over-exporting this exceeds pv_w → clamps to rated → PV runs free.
  Because pv_w is re-measured every cycle, the loop converges (deadbeat).
"""
from src.channels import SystemState
from src.controllers.base import Controller


class GridExportLimitController(Controller):
    def __init__(
        self,
        cycle_s: float,
        export_limit_w: float,
        rated_w: float,
        deadband_w: float = 200.0,
        ramp_rate_w_per_s: float = 5000.0,
    ) -> None:
        if export_limit_w < 0:
            raise ValueError("export_limit_w must be >= 0 (magnitude)")
        self._export_limit_w = export_limit_w
        self._rated_w = rated_w
        self._deadband_w = deadband_w
        self._max_ramp = ramp_rate_w_per_s * cycle_s
        # Fail-safe default: full power = no curtailment until first scan computes.
        self._last_setpoint = rated_w  # VAR RETAIN

    def execute(self, state: SystemState) -> None:
        # Yield to the PRIORITY 0 safety interlock: when tripped, the
        # SafetyController owns pv.WSet. Resync RETAIN so we resume smoothly
        # (ramp up from the forced safe value, not from a stale pre-trip target).
        if state.get("sys.safe_mode") >= 0.5:
            self._last_setpoint = state.get("pv.WSet")
            return

        # VAR_INPUT reads
        grid_w = state.get("grid.W")   # + import, - export
        pv_w = state.get("pv.W")       # actual PV production

        # feed-forward target cap (see module docstring derivation)
        target = pv_w + grid_w + self._export_limit_w

        # clamp to inverter limits (0 .. rated)
        target = max(0.0, min(self._rated_w, target))

        # deadband: ignore micro-adjustments to avoid hunting the setpoint
        if abs(target - self._last_setpoint) < self._deadband_w:
            target = self._last_setpoint

        # rate-limit (ramp) — never slam the inverter setpoint
        delta = target - self._last_setpoint
        delta = max(-self._max_ramp, min(self._max_ramp, delta))
        setpoint = self._last_setpoint + delta

        self._last_setpoint = setpoint  # persist for next cycle (RETAIN)

        # VAR_OUTPUT write
        state.set("pv.WSet", setpoint)
