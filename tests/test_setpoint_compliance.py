"""Tests for actuator monitoring (src/pyems/controllers/setpoint_compliance.py).

The monitor compares measured unit active power against the APPLIED setpoint:
sustained overshoot means the unit ignores its commands (e.g. remote power
control disabled) — raised via the sys.setpoint_violation status word.
"""
import logging

import pytest

from pyems.allocation.request import RequestBoard
from pyems.channels import Channel, SystemState
from pyems.controllers.setpoint_compliance import (
    SETPOINT_VIOLATION_CHANNEL,
    SetpointComplianceMonitor,
)


@pytest.fixture
def comp_state() -> SystemState:
    return SystemState([
        Channel("pv.W", unit="W"),
        Channel("pv.WSet", unit="W", min_val=0, max_val=100000, writable=True),
        Channel(SETPOINT_VIOLATION_CHANNEL, min_val=0, max_val=1, writable=True),
    ])


def make_monitor() -> SetpointComplianceMonitor:
    return SetpointComplianceMonitor(
        unit_active_power_channel="pv.W",
        unit_active_power_setpoint_channel="pv.WSet",
        tolerance_w=2000.0,
        max_violation_s=30.0,
    )


def run_cycle(monitor, state, now: float, p_unit: float, p_set: float) -> None:
    board = RequestBoard(["pv.WSet"])
    board.tick(now)
    state._channels["pv.W"].value = p_unit
    state._channels["pv.WSet"].value = p_set
    monitor.execute(state, board)


def test_unit_below_cap_is_compliant(comp_state):
    """Producing under the setpoint is normal for PV (clouds, night)."""
    mon = make_monitor()
    run_cycle(mon, comp_state, 0.0, p_unit=30000.0, p_set=50000.0)
    run_cycle(mon, comp_state, 100.0, p_unit=0.0, p_set=50000.0)
    assert comp_state.get(SETPOINT_VIOLATION_CHANNEL) == 0.0


def test_brief_overshoot_within_window_is_tolerated(comp_state):
    """Response lag while the unit ramps down must not raise the alarm."""
    mon = make_monitor()
    run_cycle(mon, comp_state, 0.0, p_unit=60000.0, p_set=50000.0)
    run_cycle(mon, comp_state, 10.0, p_unit=55000.0, p_set=50000.0)
    run_cycle(mon, comp_state, 20.0, p_unit=50500.0, p_set=50000.0)  # within tolerance
    assert comp_state.get(SETPOINT_VIOLATION_CHANNEL) == 0.0


def test_sustained_overshoot_raises_violation(comp_state, caplog):
    mon = make_monitor()
    with caplog.at_level(logging.ERROR):
        run_cycle(mon, comp_state, 0.0, p_unit=80000.0, p_set=50000.0)
        run_cycle(mon, comp_state, 31.0, p_unit=80000.0, p_set=50000.0)
    assert comp_state.get(SETPOINT_VIOLATION_CHANNEL) == 1.0
    violations = [r for r in caplog.records if "SETPOINT VIOLATION" in r.message]
    assert len(violations) == 1
    # third violating cycle must not re-log (transition logging only)
    with caplog.at_level(logging.ERROR):
        run_cycle(mon, comp_state, 32.0, p_unit=80000.0, p_set=50000.0)
    assert len([r for r in caplog.records if "SETPOINT VIOLATION:" in r.message]) == 1


def test_violation_clears_when_unit_follows_again(comp_state, caplog):
    mon = make_monitor()
    run_cycle(mon, comp_state, 0.0, p_unit=80000.0, p_set=50000.0)
    run_cycle(mon, comp_state, 31.0, p_unit=80000.0, p_set=50000.0)
    assert comp_state.get(SETPOINT_VIOLATION_CHANNEL) == 1.0
    with caplog.at_level(logging.WARNING):
        run_cycle(mon, comp_state, 40.0, p_unit=50000.0, p_set=50000.0)
    assert comp_state.get(SETPOINT_VIOLATION_CHANNEL) == 0.0
    assert any("cleared" in r.message for r in caplog.records)


def test_overshoot_clock_resets_on_compliance(comp_state):
    """Intermittent overshoot keeps resetting the window — no accumulation."""
    mon = make_monitor()
    run_cycle(mon, comp_state, 0.0, p_unit=80000.0, p_set=50000.0)
    run_cycle(mon, comp_state, 20.0, p_unit=50000.0, p_set=50000.0)  # back in line
    run_cycle(mon, comp_state, 21.0, p_unit=80000.0, p_set=50000.0)  # new episode
    run_cycle(mon, comp_state, 45.0, p_unit=80000.0, p_set=50000.0)  # 24 s < 30 s
    assert comp_state.get(SETPOINT_VIOLATION_CHANNEL) == 0.0


def test_invalid_parameters_rejected():
    with pytest.raises(ValueError):
        SetpointComplianceMonitor("pv.W", "pv.WSet", tolerance_w=-1.0)
    with pytest.raises(ValueError):
        SetpointComplianceMonitor("pv.W", "pv.WSet", max_violation_s=0.0)
