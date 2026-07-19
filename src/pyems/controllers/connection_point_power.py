"""PID-trim active-power control at the connection point.

The controller is unit-agnostic: it reads a connection-point active-power tag,
the unit active-power tag, and posts a request for a bound setpoint tag. It does
not write setpoints directly; PowerAllocator remains the sole writer.

Island suspend (`suspend_channel`, optional): connection-point regulation is
meaningless while the changeover switch has disconnected the plant from the
network — the bound meter reads a dead feeder (0 W), the loop error freezes and
a wound-up integrator can pin the unit at a stale target. While the bound status
word (e.g. `sys.generator_running`) is 1, the controller withdraws its claim and
resets its PID; the island constraint (generator minimum load) governs instead.
"""

from __future__ import annotations

import logging
import math

from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import SystemState
from pyems.control.pid import PIDController, PIDGains
from pyems.controllers.base import Controller

logger = logging.getLogger(__name__)

EXPORT_LIMIT_MODE = "export_limit"
IMPORT_LIMIT_MODE = "import_limit"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class ConnectionPointPowerController(Controller):
    """Regulate P at the connection point inside an export/import band.

    Sign convention at the connection point:
      P_cp > 0 means import from the grid.
      P_cp < 0 means export to the grid.
    """

    def __init__(
        self,
        name: str,
        priority: int,
        export_limit_w: float,
        connection_point_active_power_channel: str,
        unit_active_power_channel: str,
        unit_active_power_setpoint_channel: str,
        gains: PIDGains | None = None,
        import_limit_w: float = math.inf,
        mode: str = EXPORT_LIMIT_MODE,
        deadband_w: float = 200.0,
        suspend_channel: str | None = None,
    ) -> None:
        if export_limit_w < 0:
            raise ValueError("export_limit_w must be >= 0 (magnitude)")
        if import_limit_w < 0:
            raise ValueError("import_limit_w must be >= 0 (magnitude)")
        if mode not in (EXPORT_LIMIT_MODE, IMPORT_LIMIT_MODE):
            raise ValueError(
                f"mode must be '{EXPORT_LIMIT_MODE}' or '{IMPORT_LIMIT_MODE}', got {mode!r}"
            )
        if mode == IMPORT_LIMIT_MODE and not math.isfinite(import_limit_w):
            raise ValueError("import_limit_w must be finite in import_limit mode")
        if deadband_w < 0:
            raise ValueError("deadband_w must be >= 0")
        self._name = name
        self._priority = priority
        self._export_limit_w = float(export_limit_w)
        self._import_limit_w = float(import_limit_w)
        self._mode = mode
        self._deadband_w = deadband_w
        self._cp_active_power_ch = connection_point_active_power_channel
        self._unit_active_power_ch = unit_active_power_channel
        self._setpoint_ch = unit_active_power_setpoint_channel
        self._pid = PIDController(gains or PIDGains(kp=0.4, ki=0.08, kd=0.0))
        self._last_now: float | None = None
        self._last_requested_w: float | None = None
        self._limiting = False
        # Optional island suspend: status word (0/1); while 1 the claim is
        # withdrawn and the PID reset (see module docstring). None = never.
        self._suspend_ch = suspend_channel
        self._suspended = False  # last state — log only on transition

    @property
    def pid(self) -> PIDController:
        return self._pid

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        if self._suspend_ch is not None and state.get(self._suspend_ch) >= 0.5:
            board.withdraw(self._setpoint_ch, self._name)
            self._pid.reset()
            self._last_now = None
            self._last_requested_w = None
            if not self._suspended:
                logger.info(
                    "Connection-point regulation SUSPENDED: %s active — "
                    "connection point disconnected, claim withdrawn", self._suspend_ch,
                )
                self._suspended = True
                self._limiting = False
            return
        if self._suspended:
            logger.info("Connection-point regulation RESUMED: %s cleared", self._suspend_ch)
            self._suspended = False

        if self._mode == IMPORT_LIMIT_MODE:
            self._execute_import_limit(state, board)
        else:
            self._execute_export_limit(state, board)

    def _dt_and_track_applied(self, state: SystemState, now: float) -> float | None:
        dt = None if self._last_now is None else now - self._last_now
        applied_w = state.get(self._setpoint_ch)

        if dt is not None and dt > 0 and self._last_requested_w is not None:
            self._pid.track_applied_output(
                applied=applied_w,
                requested=self._last_requested_w,
                dt=dt,
            )
        return dt

    def _execute_export_limit(self, state: SystemState, board: RequestBoard) -> None:
        now = board.now
        dt = self._dt_and_track_applied(state, now)
        p_cp_w = state.get(self._cp_active_power_ch)
        p_unit_w = state.get(self._unit_active_power_ch)

        cap_w = max(0.0, p_unit_w + p_cp_w + self._export_limit_w)
        floor_w = 0.0
        if math.isfinite(self._import_limit_w):
            floor_w = max(0.0, p_unit_w + p_cp_w - self._import_limit_w)
        if floor_w > cap_w:
            floor_w = cap_w

        p_cp_target_w = -self._export_limit_w
        if math.isfinite(self._import_limit_w) and p_cp_w > self._import_limit_w:
            p_cp_target_w = self._import_limit_w

        feedforward_w = _clamp(p_unit_w + p_cp_w - p_cp_target_w, floor_w, cap_w)

        trim_w = 0.0
        if dt is not None and dt > 0:
            self._pid.out_min = floor_w - feedforward_w
            self._pid.out_max = cap_w - feedforward_w
            trim_w = self._pid.step(
                setpoint=-p_cp_target_w,
                measurement=-p_cp_w,
                dt=dt,
            )

        target_w = _clamp(feedforward_w + trim_w, floor_w, cap_w)
        board.post(
            self._setpoint_ch,
            ActivePowerRequest(
                requester=self._name,
                priority=self._priority,
                min_w=floor_w,
                max_w=cap_w,
                target_w=target_w,
            ),
        )
        self._last_now = now
        self._last_requested_w = target_w

        logger.debug(
            "%s: P_cp=%.0f P_unit=%.0f floor=%.0f cap=%.0f target=%.0f W",
            self._setpoint_ch,
            p_cp_w,
            p_unit_w,
            floor_w,
            cap_w,
            target_w,
        )

    def _execute_import_limit(self, state: SystemState, board: RequestBoard) -> None:
        now = board.now
        p_cp_w = state.get(self._cp_active_power_ch)
        p_unit_w = state.get(self._unit_active_power_ch)

        if p_cp_w <= self._import_limit_w + self._deadband_w:
            board.withdraw(self._setpoint_ch, self._name)
            self._pid.reset()
            self._last_now = None
            self._last_requested_w = None
            if self._limiting:
                logger.info("Import-limit RELEASED: connection point import back inside limit")
                self._limiting = False
            return

        dt = self._dt_and_track_applied(state, now)
        floor_w = max(0.0, p_unit_w + p_cp_w - self._import_limit_w)
        feedforward_w = floor_w

        trim_w = 0.0
        if dt is not None and dt > 0:
            self._pid.out_min = floor_w - feedforward_w
            self._pid.out_max = math.inf
            trim_w = self._pid.step(
                setpoint=-self._import_limit_w,
                measurement=-p_cp_w,
                dt=dt,
            )

        target_w = max(floor_w, feedforward_w + trim_w)
        board.post(
            self._setpoint_ch,
            ActivePowerRequest(
                requester=self._name,
                priority=self._priority,
                min_w=floor_w,
                target_w=target_w,
            ),
        )
        self._last_now = now
        self._last_requested_w = target_w

        if not self._limiting:
            logger.info(
                "Import-limit ENGAGED: P_cp=%.0f W, requesting %s >= %.0f W "
                "(limit %.0f W)",
                p_cp_w,
                self._setpoint_ch,
                target_w,
                self._import_limit_w,
            )
            self._limiting = True

        logger.debug(
            "%s: P_cp=%.0f P_unit=%.0f floor=%.0f target=%.0f W",
            self._setpoint_ch,
            p_cp_w,
            p_unit_w,
            floor_w,
            target_w,
        )
