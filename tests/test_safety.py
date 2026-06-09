"""Tests for the PRIORITY 0 safety interlock (src/pyems/controllers/safety.py).

Safety now posts priority-0 claims on the board (min=max=target=safe value) and
maintains `sys.safe_mode` as a status word. These tests assert both.
"""
from pyems.allocation.request import RequestBoard
from pyems.controllers.safety import (
    SAFE_MODE_CHANNEL,
    SAFETY_REQUESTER,
    SafetyController,
)
from pyems.drivers.cached import COMMS_AGE_CHANNEL


def make_safety(channels=("pv.WSet",)):
    return SafetyController(
        max_comms_age_s=2.0,
        safe_active_power_w=50000.0,
        unit_active_power_setpoint_channels=list(channels),
    )


def board(channels=("pv.WSet",)) -> RequestBoard:
    return RequestBoard(list(channels))


def only_claim(b: RequestBoard, channel="pv.WSet"):
    reqs = b.valid_requests(channel, now=0.0)
    assert len(reqs) == 1
    return reqs[0]


def test_healthy_clears_safe_mode_and_posts_nothing(state):
    b = board()
    state._channels[COMMS_AGE_CHANNEL].value = 0.5
    make_safety().execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 0.0
    assert b.valid_requests("pv.WSet", now=0.0) == []


def test_stale_bus_trips_and_pins_claim(state):
    b = board()
    state._channels[COMMS_AGE_CHANNEL].value = 5.0  # > 2.0 limit
    make_safety().execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 1.0
    claim = only_claim(b)
    assert claim.requester == SAFETY_REQUESTER
    assert claim.priority == 0
    assert claim.min_w == claim.max_w == claim.target_w == 50000.0
    assert claim.ttl_s is None


def test_trip_then_release_withdraws_claim(state):
    safety = make_safety()
    b = board()
    state._channels[COMMS_AGE_CHANNEL].value = 5.0
    safety.execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 1.0
    assert only_claim(b).target_w == 50000.0

    state._channels[COMMS_AGE_CHANNEL].value = 0.1
    safety.execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 0.0
    assert b.valid_requests("pv.WSet", now=0.0) == []  # claim withdrawn


def test_caps_multiple_units():
    from pyems.channels import Channel, SystemState

    chans = [
        Channel(SAFE_MODE_CHANNEL, writable=True, min_val=0, max_val=1),
        Channel(COMMS_AGE_CHANNEL, value=9.0),
    ]
    st = SystemState(chans)
    b = board(["pv1.WSet", "pv2.WSet"])
    SafetyController(2.0, 40000.0, ["pv1.WSet", "pv2.WSet"]).execute(st, b)
    assert only_claim(b, "pv1.WSet").target_w == 40000.0
    assert only_claim(b, "pv2.WSet").target_w == 40000.0


def test_trip_logs_once(state, caplog):
    import logging

    safety = make_safety()
    b = board()
    state._channels[COMMS_AGE_CHANNEL].value = 5.0
    with caplog.at_level(logging.WARNING):
        safety.execute(state, b)
        safety.execute(state, b)  # second stale cycle must not re-log
    trips = [r for r in caplog.records if "SAFETY TRIP" in r.message]
    assert len(trips) == 1
