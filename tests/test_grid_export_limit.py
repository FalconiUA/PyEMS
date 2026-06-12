"""Tests for the export-limit control law (src/pyems/controllers/grid_export_limit.py).

The controller is now a pure constraint: it posts an upper-bound (max_w) request
on the unit setpoint channel. These tests assert the posted cap; ramp/deadband
behavior lives with the allocator (see test_allocator.py).
"""
import pytest

from pyems.allocation.request import RequestBoard
from pyems.controllers.grid_export_limit import GridExportLimitController

CH = "pv.WSet"


def make_ctrl(**kw):
    params = dict(
        name="export_limit",
        priority=5,
        export_limit_w=50000.0,
        connection_point_active_power_channel="grid.W",
        unit_active_power_channel="pv.W",
        unit_active_power_setpoint_channel=CH,
        deadband_w=0.0,
    )
    params.update(kw)
    return GridExportLimitController(**params)


def posted_cap(board: RequestBoard) -> float:
    reqs = board.valid_requests(CH, now=0.0)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.requester == "export_limit"
    assert r.priority == 5
    assert r.target_w is None          # pure constraint, no preference
    assert r.min_w == float("-inf")    # only narrows from above
    return r.max_w


def run(state, **kw) -> float:
    board = RequestBoard([CH])
    make_ctrl(**kw).execute(state, board)
    return posted_cap(board)


def test_negative_export_limit_rejected():
    with pytest.raises(ValueError, match="must be >= 0"):
        make_ctrl(export_limit_w=-1.0)


def test_over_export_posts_cap_at_limit(state):
    # exporting 70 kW (grid.W = -70k), PV producing 70 kW, limit 50 kW.
    state.apply_driver_value("grid.W", -70000.0)
    state.apply_driver_value("pv.W", 70000.0)
    # cap = pv + grid + limit = 70000 - 70000 + 50000 = 50000
    assert run(state) == pytest.approx(50000.0)


def test_within_limit_posts_cap_above_production(state):
    # Importing (grid.W > 0): cap (80 kW) sits well above production (20 kW).
    state.apply_driver_value("grid.W", 10000.0)
    state.apply_driver_value("pv.W", 20000.0)
    board = RequestBoard([CH])
    ctrl = make_ctrl()
    ctrl.execute(state, board)
    assert posted_cap(board) == pytest.approx(80000.0)
    assert ctrl._curtailing is False  # cap above production → not curtailing


def test_cap_not_lower_bounded_below_zero(state):
    # Pathological over-export beyond the limit would give a negative raw cap;
    # the controller floors it at 0 (the allocator envelope handles the top).
    state.apply_driver_value("grid.W", -200000.0)
    state.apply_driver_value("pv.W", 100000.0)
    assert run(state) == pytest.approx(0.0)  # 100k - 200k + 50k = -50k → 0


def test_engaged_release_logging(state, caplog):
    import logging

    ctrl = make_ctrl()
    board = RequestBoard([CH])
    state.apply_driver_value("grid.W", -70000.0)
    state.apply_driver_value("pv.W", 70000.0)
    with caplog.at_level(logging.INFO):
        ctrl.execute(state, board)              # cap 50k < prod 70k → ENGAGED
        ctrl.execute(state, board)              # still engaged → no re-log
        state.apply_driver_value("grid.W", 10000.0)  # now importing
        ctrl.execute(state, board)              # cap above prod → RELEASED
    engaged = [r for r in caplog.records if "ENGAGED" in r.message]
    released = [r for r in caplog.records if "RELEASED" in r.message]
    assert len(engaged) == 1
    assert len(released) == 1
