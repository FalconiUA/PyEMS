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
