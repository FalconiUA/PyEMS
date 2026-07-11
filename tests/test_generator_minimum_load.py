"""Tests for the generator minimum-load module
(src/pyems/controllers/generator_minimum_load.py + the ems.py wiring).

The controller is a pure constraint like the export limit: while the generator
runs it posts an upper-bound (max_w) request on the unit setpoint channel so
the generator never drops below its minimum load; on grid operation (generator
meter ~0 W) it withdraws and the connection-point program governs alone.
"""
import logging

import pytest

from pyems.allocation.request import RequestBoard
from pyems.channels import Channel, SystemState
from pyems.controllers.generator_minimum_load import GeneratorMinimumLoadController
from pyems.system_tags import GENERATOR_MIN_LOAD_REQUESTER, GENERATOR_RUNNING_CHANNEL

CH = "pv.WSet"

# Nameplate used across the tests: 125 kVA / 100 kW genset, 30% minimum load
# → floor 30 kW; default running threshold 2% → 2 kW.
S_RATED_VA = 125000.0
P_RATED_W = 100000.0
MIN_LOAD_PCT = 30.0
FLOOR_W = 30000.0


def make_ctrl(**kw):
    params = dict(
        name=GENERATOR_MIN_LOAD_REQUESTER,
        priority=5,
        rated_apparent_power_va=S_RATED_VA,
        rated_active_power_w=P_RATED_W,
        minimum_load_pct=MIN_LOAD_PCT,
        generator_active_power_channel="gen.W",
        unit_active_power_channel="pv.W",
        unit_active_power_setpoint_channel=CH,
        deadband_w=0.0,
    )
    params.update(kw)
    return GeneratorMinimumLoadController(**params)


@pytest.fixture
def gen_state() -> SystemState:
    return SystemState(
        [
            Channel("gen.W", unit="W"),
            Channel("pv.W", unit="W"),
            Channel(CH, unit="W", min_val=0, max_val=100000, writable=True),
            Channel(GENERATOR_RUNNING_CHANNEL, min_val=0, max_val=1, writable=True),
        ]
    )


def posted_cap(board: RequestBoard, now: float = 0.0) -> float:
    reqs = board.valid_requests(CH, now=now)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.requester == GENERATOR_MIN_LOAD_REQUESTER
    assert r.priority == 5
    assert r.target_w is None          # pure constraint, no preference
    assert r.min_w == float("-inf")    # only narrows from above
    return r.max_w


# ── nameplate validation ──────────────────────────────────────────────────────

def test_rated_active_power_must_be_positive():
    with pytest.raises(ValueError, match="rated_active_power_w"):
        make_ctrl(rated_active_power_w=0.0)


def test_apparent_power_must_cover_active_power():
    with pytest.raises(ValueError, match="rated_apparent_power_va"):
        make_ctrl(rated_apparent_power_va=P_RATED_W - 1.0)


@pytest.mark.parametrize("pct", [0.0, -5.0, 101.0])
def test_minimum_load_pct_range(pct):
    with pytest.raises(ValueError, match="minimum_load_pct"):
        make_ctrl(minimum_load_pct=pct)


def test_minimum_load_derives_from_rated_active_power():
    assert make_ctrl().minimum_load_w == pytest.approx(FLOOR_W)


# ── control law while the generator runs ─────────────────────────────────────

def test_generator_above_floor_cap_leaves_margin(gen_state):
    # gen at 50 kW (20 kW above the 30 kW floor), PV at 40 kW:
    # PV may rise by the generator's margin → cap = 40k + (50k − 30k) = 60 kW.
    gen_state.apply_driver_value("gen.W", 50000.0)
    gen_state.apply_driver_value("pv.W", 40000.0)
    board = RequestBoard([CH])
    make_ctrl().execute(gen_state, board)
    assert posted_cap(board) == pytest.approx(60000.0)
    assert gen_state.get(GENERATOR_RUNNING_CHANNEL) == 1.0


def test_generator_below_floor_curtails_unit(gen_state):
    # gen squeezed to 10 kW (20 kW below floor), PV at 70 kW:
    # cap = 70k + (10k − 30k) = 50 kW — PV curtailed by the missing 20 kW.
    gen_state.apply_driver_value("gen.W", 10000.0)
    gen_state.apply_driver_value("pv.W", 70000.0)
    board = RequestBoard([CH])
    make_ctrl().execute(gen_state, board)
    assert posted_cap(board) == pytest.approx(50000.0)


def test_reverse_power_caps_at_zero(gen_state):
    # Reverse power into the generator (gen.W < 0) with modest PV: the raw cap
    # is negative → floored at 0 (full curtailment in one posting).
    gen_state.apply_driver_value("gen.W", -10000.0)
    gen_state.apply_driver_value("pv.W", 15000.0)
    board = RequestBoard([CH])
    make_ctrl().execute(gen_state, board)
    assert posted_cap(board) == pytest.approx(0.0)  # 15k + (−10k − 30k) = −25k → 0
    # reverse power counts as RUNNING (|P| above threshold): the cap must hold
    assert gen_state.get(GENERATOR_RUNNING_CHANNEL) == 1.0


# ── run detection: grid vs generator ─────────────────────────────────────────

def test_generator_stopped_no_claim_grid_program_untouched(gen_state):
    # Generator meter ~0 W (grid operation): no claim, status word 0.
    gen_state.apply_driver_value("gen.W", 0.0)
    gen_state.apply_driver_value("pv.W", 70000.0)
    board = RequestBoard([CH])
    make_ctrl().execute(gen_state, board)
    assert board.valid_requests(CH, now=0.0) == []
    assert gen_state.get(GENERATOR_RUNNING_CHANNEL) == 0.0


def test_claim_withdrawn_after_off_delay(gen_state):
    ctrl = make_ctrl(off_delay_s=5.0)
    board = RequestBoard([CH])
    gen_state.apply_driver_value("pv.W", 40000.0)

    board.tick(0.0)
    gen_state.apply_driver_value("gen.W", 50000.0)
    ctrl.execute(gen_state, board)              # running, claim posted
    assert len(board.valid_requests(CH, now=0.0)) == 1

    board.tick(1.0)
    gen_state.apply_driver_value("gen.W", 0.0)  # ATS back to grid
    ctrl.execute(gen_state, board)              # below threshold, delay not elapsed
    assert len(board.valid_requests(CH, now=1.0)) == 1  # cap held through the dip
    assert gen_state.get(GENERATOR_RUNNING_CHANNEL) == 1.0

    board.tick(6.5)                              # 5.5 s below threshold > off_delay
    ctrl.execute(gen_state, board)
    assert board.valid_requests(CH, now=6.5) == []
    assert gen_state.get(GENERATOR_RUNNING_CHANNEL) == 0.0


def test_transient_dip_through_zero_keeps_the_cap(gen_state):
    # A PV overshoot swings gen.W through 0 for one cycle; the latch + off
    # delay must keep the (fully curtailing) cap alive, not withdraw it.
    ctrl = make_ctrl(off_delay_s=5.0)
    board = RequestBoard([CH])
    gen_state.apply_driver_value("pv.W", 60000.0)

    board.tick(0.0)
    gen_state.apply_driver_value("gen.W", 31000.0)
    ctrl.execute(gen_state, board)

    board.tick(1.0)
    gen_state.apply_driver_value("gen.W", 500.0)  # inside ±threshold for a cycle
    ctrl.execute(gen_state, board)
    cap = posted_cap(board, now=1.0)
    assert cap == pytest.approx(60000.0 + 500.0 - FLOOR_W)

    board.tick(2.0)
    gen_state.apply_driver_value("gen.W", 35000.0)  # recovered
    ctrl.execute(gen_state, board)
    assert gen_state.get(GENERATOR_RUNNING_CHANNEL) == 1.0


def test_custom_running_threshold(gen_state):
    ctrl = make_ctrl(running_threshold_w=10000.0)
    board = RequestBoard([CH])
    gen_state.apply_driver_value("gen.W", 5000.0)  # below the custom threshold
    gen_state.apply_driver_value("pv.W", 20000.0)
    ctrl.execute(gen_state, board)
    assert board.valid_requests(CH, now=0.0) == []
    assert gen_state.get(GENERATOR_RUNNING_CHANNEL) == 0.0


# ── logging transitions ──────────────────────────────────────────────────────

def test_engaged_release_logging(gen_state, caplog):
    ctrl = make_ctrl()
    board = RequestBoard([CH])
    with caplog.at_level(logging.INFO):
        board.tick(0.0)
        gen_state.apply_driver_value("gen.W", 10000.0)   # below floor
        gen_state.apply_driver_value("pv.W", 70000.0)
        ctrl.execute(gen_state, board)                   # RUNNING + ENGAGED
        ctrl.execute(gen_state, board)                   # still engaged → no re-log
        gen_state.apply_driver_value("gen.W", 60000.0)   # comfortably above floor
        ctrl.execute(gen_state, board)                   # cap above prod → RELEASED
    assert len([r for r in caplog.records if "RUNNING detected" in r.message]) == 1
    assert len([r for r in caplog.records if "ENGAGED" in r.message]) == 1
    assert len([r for r in caplog.records if "RELEASED" in r.message]) == 1


# ── ems.py wiring ────────────────────────────────────────────────────────────

def _site_with_generator() -> dict:
    return {
        "scenario": {"control_mode": "export_limit"},
        "control": {"fast_cycle_s": 1.0},
        "export_limit": {
            "limit_w": 30000.0, "priority": 5,
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": CH,
        },
        "connection_point_active_power": {
            "export_limit_w": 30000.0, "import_limit_w": 1e9, "priority": 10,
            "gains": {"kp": 0.4, "ki": 0.08, "kd": 0.0, "tt": 5.0},
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": CH,
        },
        "safety": {"max_comms_age_s": 2.0,
                   "unit_active_power_setpoint_channels": [CH]},
        "allocation": {"channels": [{
            "setpoint_channel": CH, "p_min_w": 0.0, "p_max_w": 100000.0,
            "default_w": 100000.0, "deadband_w": 200.0,
        }]},
        "generator_minimum_load": {
            "rated_apparent_power_va": S_RATED_VA,
            "rated_active_power_w": P_RATED_W,
            "minimum_load_pct": MIN_LOAD_PCT,
            "generator_active_power_channel": "gen.W",
        },
    }


def test_build_tasks_adds_generator_controller():
    from pyems.ems import build_tasks

    fast = next(t for t in build_tasks(_site_with_generator()) if t.name == "fast")
    ctrl = next(
        c for c in fast.controllers if isinstance(c, GeneratorMinimumLoadController)
    )
    assert ctrl._name == GENERATOR_MIN_LOAD_REQUESTER
    # unit bindings defaulted from the connection_point_active_power section
    assert ctrl._unit_active_power_ch == "pv.W"
    assert ctrl._setpoint_ch == CH
    assert ctrl.minimum_load_w == pytest.approx(FLOOR_W)


def test_required_channels_include_generator_tags():
    from pyems.ems import required_channels

    tags = required_channels(_site_with_generator())
    assert "gen.W" in tags
    assert GENERATOR_RUNNING_CHANNEL in tags


def test_missing_nameplate_key_fails_fast():
    from pyems.ems import build_tasks

    site = _site_with_generator()
    del site["generator_minimum_load"]["rated_apparent_power_va"]
    with pytest.raises(ValueError, match="rated_apparent_power_va"):
        build_tasks(site)


def test_generator_meter_profile_loads():
    """The generator meter profile must pass the canonical-vocabulary check."""
    from pyems.ems import PROFILES
    import pyems.drivers.modbus_device as md

    profile = md.DeviceProfile.load(
        PROFILES / "meters/generator_meter_huawei_smartlogger3000.yaml"
    )
    channels = {r.channel for r in profile.registers}
    assert "gen.W" in channels
    assert all(not r.writable for r in profile.registers)  # a meter is read-only
