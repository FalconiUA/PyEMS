"""PID-trim active-power control at the connection point.

The controller is unit-agnostic: it reads a connection-point active-power tag,
the unit active-power tag, and posts a request for a bound setpoint tag. It does
not write setpoints directly; PowerAllocator remains the sole writer.
"""

from __future__ import annotations

import logging
import math

from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import SystemState
from pyems.control.pid import PIDController, PIDGains
from pyems.controllers.base import Controller

logger = logging.getLogger(__name__)


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
    ) -> None:
        if export_limit_w < 0:
            raise ValueError("export_limit_w must be >= 0 (magnitude)")
        if import_limit_w < 0:
            raise ValueError("import_limit_w must be >= 0 (magnitude)")
        self._name = name
        self._priority = priority
        self._export_limit_w = float(export_limit_w)
        self._import_limit_w = float(import_limit_w)
        self._cp_active_power_ch = connection_point_active_power_channel
        self._unit_active_power_ch = unit_active_power_channel
        self._setpoint_ch = unit_active_power_setpoint_channel
        self._pid = PIDController(gains or PIDGains(kp=0.4, ki=0.08, kd=0.0))
        self._last_now: float | None = None
        self._last_requested_w: float | None = None

    @property
    def pid(self) -> PIDController:
        return self._pid

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        now = board.now
        dt = None if self._last_now is None else now - self._last_now
        applied_w = state.get(self._setpoint_ch)

        if dt is not None and dt > 0 and self._last_requested_w is not None:
            self._pid.track_applied_output(
                applied=applied_w,
                requested=self._last_requested_w,
                dt=dt,
            )

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
