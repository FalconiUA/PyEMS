"""Active-power import limitation at the connection point.

Sign convention at the connection point:
  P_cp > 0 means import from the grid.
  P_cp < 0 means export to the grid.

This controller is intentionally one-sided: it only acts while import exceeds
the configured limit, then withdraws its request once the site is back inside
the limit. It does not try to regulate export.
"""
from __future__ import annotations

import logging

from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import SystemState
from pyems.controllers.base import Controller

logger = logging.getLogger(__name__)


class ConnectionPointImportLimitController(Controller):
    def __init__(
        self,
        name: str,
        priority: int,
        import_limit_w: float,
        connection_point_active_power_channel: str,
        unit_active_power_channel: str,
        unit_active_power_setpoint_channel: str,
        deadband_w: float = 200.0,
    ) -> None:
        if import_limit_w < 0:
            raise ValueError("import_limit_w must be >= 0 (magnitude)")
        self._name = name
        self._priority = priority
        self._import_limit_w = import_limit_w
        self._cp_active_power_ch = connection_point_active_power_channel
        self._unit_active_power_ch = unit_active_power_channel
        self._setpoint_ch = unit_active_power_setpoint_channel
        self._deadband_w = deadband_w
        self._limiting = False

    def execute(self, state: SystemState, board: RequestBoard) -> None:
        p_cp_w = state.get(self._cp_active_power_ch)
        p_unit_w = state.get(self._unit_active_power_ch)

        if p_cp_w <= self._import_limit_w + self._deadband_w:
            board.withdraw(self._setpoint_ch, self._name)
            if self._limiting:
                logger.info("Import-limit RELEASED: connection point import back inside limit")
                self._limiting = False
            return

        target_w = max(0.0, p_unit_w + p_cp_w - self._import_limit_w)
        board.post(
            self._setpoint_ch,
            ActivePowerRequest(
                requester=self._name,
                priority=self._priority,
                min_w=target_w,
                target_w=target_w,
            ),
        )
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
