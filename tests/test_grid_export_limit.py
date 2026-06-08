"""Tests for the export-limit control law (src/controllers/grid_export_limit.py)."""
import pytest

from pyems.controllers.grid_export_limit import GridExportLimitController
from pyems.controllers.safety import SAFE_MODE_CHANNEL


def make_ctrl(**kw):
    params = dict(
        cycle_s=1.0,
        export_limit_w=50000.0,
        p_max_w=100000.0,
        connection_point_active_power_channel="grid.W",
        unit_active_power_channel="pv.W",
        unit_active_power_setpoint_channel="pv.WSet",
        ramp_rate_w_per_s=1e9,  # effectively unlimited ramp for deadbeat tests
        deadband_w=0.0,
    )
    params.update(kw)
    return GridExportLimitController(**params)


def test_negative_export_limit_rejected():
    with pytest.raises(ValueError, match="must be >= 0"):
        make_ctrl(export_limit_w=-1.0)


def test_over_export_curtails_to_limit(state):
    # exporting 70 kW (grid.W = -70k), PV producing 70 kW, limit 50 kW.
    state._channels["grid.W"].value = -70000.0
    state._channels["pv.W"].value = 70000.0
    make_ctrl().execute(state)
    # target = pv + grid + limit = 70000 - 70000 + 50000 = 50000
    assert state.get("pv.WSet") == pytest.approx(50000.0)


def test_within_limit_runs_free(state):
    # Importing (grid.W > 0): the cap (80 kW) sits well above production (20 kW),
    # so the unit runs free. target = pv + grid + limit = 20k + 10k + 50k = 80k.
    state._channels["grid.W"].value = 10000.0
    state._channels["pv.W"].value = 20000.0
    ctrl = make_ctrl()
    ctrl.execute(state)
    assert state.get("pv.WSet") == pytest.approx(80000.0)
    assert ctrl._curtailing is False  # cap above production → not curtailing


def test_clamps_to_p_max(state):
    # Large import headroom would push the cap past P_max → clamp to P_max.
    state._channels["grid.W"].value = 60000.0
    state._channels["pv.W"].value = 30000.0
    make_ctrl().execute(state)  # 30k + 60k + 50k = 140k → clamp 100k
    assert state.get("pv.WSet") == pytest.approx(100000.0)


def test_yields_to_safe_mode(state):
    # safety tripped → controller must not touch the setpoint it would otherwise drive.
    state._channels[SAFE_MODE_CHANNEL].value = 1.0
    state._channels["pv.WSet"].value = 33000.0  # value forced by safety
    state._channels["grid.W"].value = -70000.0
    state._channels["pv.W"].value = 70000.0
    make_ctrl().execute(state)
    assert state.get("pv.WSet") == 33000.0  # untouched


def test_ramp_rate_limits_step(state):
    # slow ramp: 1000 W/s * 1 s cycle = max 1000 W move per cycle.
    ctrl = make_ctrl(ramp_rate_w_per_s=1000.0)
    state._channels["grid.W"].value = -70000.0
    state._channels["pv.W"].value = 70000.0
    ctrl.execute(state)
    # starts at P_max=100000, target 50000, but limited to one 1000 W step down.
    assert state.get("pv.WSet") == pytest.approx(99000.0)


def test_deadband_suppresses_micro_adjust(state):
    ctrl = make_ctrl(deadband_w=5000.0)
    state._channels["grid.W"].value = -70000.0
    state._channels["pv.W"].value = 70000.0
    ctrl.execute(state)
    first = state.get("pv.WSet")
    # tiny change in measurement within deadband → setpoint stays put
    state._channels["grid.W"].value = -70100.0
    ctrl.execute(state)
    assert state.get("pv.WSet") == first
