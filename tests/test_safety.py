"""Tests for the PRIORITY 0 safety interlock (src/controllers/safety.py)."""
from src.controllers.safety import SAFE_MODE_CHANNEL, SafetyController
from src.drivers.cached import COMMS_AGE_CHANNEL


def make_safety():
    return SafetyController(
        max_comms_age_s=2.0,
        safe_active_power_w=50000.0,
        unit_active_power_setpoint_channels=["pv.WSet"],
    )


def test_healthy_clears_safe_mode(state):
    state._channels[COMMS_AGE_CHANNEL].value = 0.5
    make_safety().execute(state)
    assert state.get(SAFE_MODE_CHANNEL) == 0.0


def test_stale_bus_trips_and_caps_setpoint(state):
    state._channels[COMMS_AGE_CHANNEL].value = 5.0  # > 2.0 limit
    make_safety().execute(state)
    assert state.get(SAFE_MODE_CHANNEL) == 1.0
    assert state.get("pv.WSet") == 50000.0


def test_trip_then_release(state):
    safety = make_safety()
    state._channels[COMMS_AGE_CHANNEL].value = 5.0
    safety.execute(state)
    assert state.get(SAFE_MODE_CHANNEL) == 1.0
    state._channels[COMMS_AGE_CHANNEL].value = 0.1
    safety.execute(state)
    assert state.get(SAFE_MODE_CHANNEL) == 0.0


def test_caps_multiple_units():
    from src.channels import Channel, SystemState

    chans = [
        Channel("pv1.WSet", writable=True, min_val=0, max_val=1e5),
        Channel("pv2.WSet", writable=True, min_val=0, max_val=1e5),
        Channel(SAFE_MODE_CHANNEL, writable=True, min_val=0, max_val=1),
        Channel(COMMS_AGE_CHANNEL, value=9.0),
    ]
    st = SystemState(chans)
    SafetyController(2.0, 40000.0, ["pv1.WSet", "pv2.WSet"]).execute(st)
    assert st.get("pv1.WSet") == 40000.0
    assert st.get("pv2.WSet") == 40000.0


def test_trip_logs_once(state, caplog):
    import logging

    safety = make_safety()
    state._channels[COMMS_AGE_CHANNEL].value = 5.0
    with caplog.at_level(logging.WARNING):
        safety.execute(state)
        safety.execute(state)  # second stale cycle must not re-log
    trips = [r for r in caplog.records if "SAFETY TRIP" in r.message]
    assert len(trips) == 1
