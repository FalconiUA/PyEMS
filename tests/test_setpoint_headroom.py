"""Available-power tracking constraint (SetpointHeadroomLimiter).

The returning-resource scenario is the point: with the unit short on resource
the setpoint must hug actual production (+headroom), so production recovers at
the allocator's up-ramp instead of jumping to a stale inflated setpoint.
"""
import pytest

from pyems.allocation.allocator import PowerAllocator, SetpointChannelConfig
from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import Channel, SystemState
from pyems.controllers.setpoint_headroom import SetpointHeadroomLimiter


def make_limiter(headroom_w=10000.0, priority=6):
    return SetpointHeadroomLimiter(
        name="setpoint_headroom",
        priority=priority,
        headroom_w=headroom_w,
        unit_active_power_channel="pv.W",
        unit_active_power_setpoint_channel="pv.WSet",
    )


def make_state(p_unit_w):
    return SystemState(
        [
            Channel("pv.W", value=p_unit_w),
            Channel("pv.WSet", min_val=0, max_val=100000, writable=True),
        ]
    )


def posted(board):
    return board.valid_requests("pv.WSet", now=0.0)


def test_posts_pure_max_constraint_above_production():
    board = RequestBoard(["pv.WSet"])
    make_limiter().execute(make_state(26900.0), board)
    (req,) = posted(board)
    assert req.max_w == 36900.0
    assert req.target_w is None
    assert req.min_w == float("-inf")


def test_negative_standby_reading_keeps_full_headroom():
    board = RequestBoard(["pv.WSet"])
    make_limiter().execute(make_state(-300.0), board)
    (req,) = posted(board)
    assert req.max_w == 10000.0


def test_rejects_nonpositive_headroom_and_safety_priority():
    with pytest.raises(ValueError, match="headroom_w"):
        make_limiter(headroom_w=0.0)
    with pytest.raises(ValueError, match="priority 0"):
        make_limiter(priority=0)


def test_returning_resource_is_ramped_not_jumped():
    """Cloud passes: production 5 kW -> available 90 kW. With the headroom
    constraint the resolved setpoint rises from ~15 kW at the up-ramp, instead
    of sitting at the 60 kW regulation cap and letting production jump."""
    board = RequestBoard(["pv.WSet"])
    cfg = SetpointChannelConfig(
        "pv.WSet", 0.0, 100000.0, 100000.0,
        ramp_rate_w_per_s=5000.0, ramp_down_w_per_s=50000.0, deadband_w=200.0,
    )
    allocator = PowerAllocator([cfg], board, cycle_s=1.0)
    state = make_state(5000.0)
    limiter = make_limiter()

    # Cloudy steady state: regulation wants 60 kW, unit makes 5 kW.
    regulation = ActivePowerRequest(
        requester="connection_point_active_power", priority=10, target_w=60000.0
    )
    for cycle in range(3):
        board.tick(float(cycle))
        limiter.execute(state, board)
        board.post("pv.WSet", regulation)
        allocator.resolve(state, float(cycle))
    assert state.get("pv.WSet") == 15000.0  # 5 kW production + 10 kW headroom

    # Sun returns: the unit could now deliver whatever the setpoint allows.
    # Each cycle production catches up to the setpoint; the setpoint may only
    # climb by min(ramp, headroom) per cycle — never a jump to 60 kW.
    previous = state.get("pv.WSet")
    for cycle in range(3, 12):
        state._channels["pv.W"].value = min(previous, 90000.0)  # unit at its cap
        board.tick(float(cycle))
        limiter.execute(state, board)
        board.post("pv.WSet", regulation)
        allocator.resolve(state, float(cycle))
        value = state.get("pv.WSet")
        assert value - previous <= 5000.0 + 1e-9, "setpoint jumped past the up-ramp"
        previous = value
    assert previous == pytest.approx(60000.0)  # reached the regulation target


def minimal_site():
    return {
        "scenario": {"control_mode": "export_limit"},
        "control": {"fast_cycle_s": 1.0},
        "export_limit": {
            "limit_w": 30000.0, "priority": 5,
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "connection_point_active_power": {
            "export_limit_w": 30000.0, "import_limit_w": 1e9, "priority": 10,
            "gains": {"kp": 0.4, "ki": 0.08, "kd": 0.0, "tt": 5.0},
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "safety": {"max_comms_age_s": 2.0,
                   "unit_active_power_setpoint_channels": ["pv.WSet"]},
        "allocation": {"channels": [{
            "setpoint_channel": "pv.WSet", "p_min_w": 0.0, "p_max_w": 100000.0,
            "default_w": 100000.0, "deadband_w": 200.0,
        }]},
    }


def headroom_controllers(site):
    from pyems.ems import build_tasks
    fast = next(t for t in build_tasks(site) if t.name == "fast")
    return [c for c in fast.controllers if isinstance(c, SetpointHeadroomLimiter)]


def test_headroom_enabled_by_default_with_derived_values():
    site = minimal_site()  # NO setpoint_headroom section at all
    (limiter,) = headroom_controllers(site)
    assert limiter._headroom_w == 10000.0  # 10% of p_max_w
    assert limiter._unit_active_power_ch == "pv.W"
    assert limiter._setpoint_ch == "pv.WSet"


def test_headroom_explicit_opt_out():
    site = minimal_site()
    site["setpoint_headroom"] = {"enabled": False}
    assert headroom_controllers(site) == []


def test_headroom_section_overrides_defaults():
    site = minimal_site()
    site["setpoint_headroom"] = {"headroom_w": 5000.0, "priority": 7}
    (limiter,) = headroom_controllers(site)
    assert limiter._headroom_w == 5000.0
    assert limiter._priority == 7


def test_headroom_default_needs_matching_allocation_channel():
    from pyems.ems import _setpoint_headroom_config
    site = minimal_site()
    site["allocation"]["channels"][0]["setpoint_channel"] = "other.WSet"
    with pytest.raises(ValueError, match="headroom_w explicitly"):
        _setpoint_headroom_config(site)


def test_safety_claim_overrides_headroom_constraint():
    """A priority-0 forced value conflicting with the headroom cap discards the
    headroom request whole — safety must land exactly, in one cycle."""
    board = RequestBoard(["pv.WSet"])
    cfg = SetpointChannelConfig("pv.WSet", 0.0, 100000.0, 100000.0)
    allocator = PowerAllocator([cfg], board, cycle_s=1.0)
    state = make_state(2000.0)  # headroom cap = 12 kW < safety's 30 kW

    board.tick(0.0)
    make_limiter().execute(state, board)
    board.post(
        "pv.WSet",
        ActivePowerRequest(
            requester="safety", priority=0,
            min_w=30000.0, max_w=30000.0, target_w=30000.0,
        ),
    )
    allocator.resolve(state, 0.0)
    assert state.get("pv.WSet") == 30000.0
