"""
Example controller: zero-grid-import balance.

IEC 61131-3 equivalent:
  FUNCTION_BLOCK PowerBalance
    VAR_INPUT
      grid_power_w  : REAL;   (* + = import, - = export *)
      pv_power_w    : REAL;
      battery_soc   : REAL;   (* 0..100 % *)
    END_VAR
    VAR_OUTPUT
      battery_setpoint_w : REAL;  (* + = charge, - = discharge *)
    END_VAR
    VAR
      last_setpoint : REAL := 0.0;   (* RETAIN: persists between cycles *)
    END_VAR
  END_FUNCTION_BLOCK
"""
from src.channels import SystemState
from src.controllers.base import Controller

SOC_MIN = 10.0
SOC_MAX = 90.0
MAX_POWER_W = 5000.0
RAMP_RATE_W_PER_S = 500.0  # rate-limit: max change per cycle (anti-slam)


class PowerBalanceController(Controller):
    def __init__(self, cycle_s: float) -> None:
        self._last_setpoint = 0.0       # VAR RETAIN
        self._max_ramp = RAMP_RATE_W_PER_S * cycle_s

    def execute(self, state: SystemState) -> None:
        # VAR_INPUT reads
        grid_w = state.get("grid.power_w")
        soc = state.get("battery.soc_pct")

        # enforce SoC limits (safety — runs before optimization layer)
        if soc <= SOC_MIN and grid_w < 0:
            target = 0.0
        elif soc >= SOC_MAX and grid_w > 0:
            target = 0.0
        else:
            # absorb grid imbalance with battery
            target = -grid_w

        # clamp to hardware limits
        target = max(-MAX_POWER_W, min(MAX_POWER_W, target))

        # rate-limit (ramp) — never slam the inverter
        delta = target - self._last_setpoint
        delta = max(-self._max_ramp, min(self._max_ramp, delta))
        setpoint = self._last_setpoint + delta

        self._last_setpoint = setpoint  # persist for next cycle (RETAIN)

        # VAR_OUTPUT write
        state.set("battery.setpoint_w", setpoint)
