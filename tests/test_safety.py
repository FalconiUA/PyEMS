"""Tests for the PRIORITY 0 safety interlock (src/pyems/controllers/safety.py).

Safety now posts priority-0 claims on the board (min=max=target=safe value) and
maintains `sys.safe_mode` as a status word. These tests assert both.
"""
from pyems.allocation.request import RequestBoard
from pyems.channels import Channel, SystemState
from pyems.controllers.safety import SafetyController
from pyems.system_tags import (
    COMMS_AGE_CHANNEL,
    SAFE_MODE_CHANNEL,
    SAFETY_REQUESTER,
    WRITE_AGE_CHANNEL,
    comms_age_channel,
)


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
    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.5)
    make_safety().execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 0.0
    assert b.valid_requests("pv.WSet", now=0.0) == []


def test_stale_bus_trips_and_pins_claim(state):
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 5.0)  # > 2.0 limit
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
    state.apply_driver_value(COMMS_AGE_CHANNEL, 5.0)
    safety.execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 1.0
    assert only_claim(b).target_w == 50000.0

    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.1)
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


# ── write-path freshness guard ───────────────────────────────────────────────
def make_write_age_safety():
    return SafetyController(
        max_comms_age_s=2.0,
        safe_active_power_w=50000.0,
        unit_active_power_setpoint_channels=["pv.WSet"],
        max_write_age_s=15.0,
    )


def test_write_age_guard_disabled_by_default(state):
    """Default-constructed safety ignores the write age — backward compatible
    with sites (and the sim harness) that have not opted in."""
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.1)
    state.apply_driver_value(WRITE_AGE_CHANNEL, 9999.0)  # would trip if guarded
    make_safety().execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 0.0
    assert b.valid_requests("pv.WSet", now=0.0) == []


def test_stale_write_path_trips_while_reads_fresh(state):
    """Reads are fresh (comms age healthy) but setpoints stopped reaching the
    bus — safety must still trip on the write age alone."""
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.1)   # bus reads healthy
    state.apply_driver_value(WRITE_AGE_CHANNEL, 20.0)  # > 15 s limit
    make_write_age_safety().execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 1.0
    claim = only_claim(b)
    assert claim.priority == 0
    assert claim.min_w == claim.max_w == claim.target_w == 50000.0


def test_write_path_recovery_releases_claim(state):
    safety = make_write_age_safety()
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.1)
    state.apply_driver_value(WRITE_AGE_CHANNEL, 20.0)
    safety.execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 1.0

    state.apply_driver_value(WRITE_AGE_CHANNEL, 1.0)  # flush landed again
    safety.execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 0.0
    assert b.valid_requests("pv.WSet", now=0.0) == []


# per-device comms freshness guard
def make_device_age_state(grid_age=0.1, pv_age=0.1) -> SystemState:
    st = SystemState(
        [
            Channel(COMMS_AGE_CHANNEL, unit="s", value=0.1),
            Channel(comms_age_channel("grid"), unit="s", value=grid_age),
            Channel(comms_age_channel("pv"), unit="s", value=pv_age),
            Channel(SAFE_MODE_CHANNEL, writable=True, min_val=0, max_val=1),
        ]
    )
    return st


def make_device_age_safety():
    return SafetyController(
        max_comms_age_s=10.0,
        safe_active_power_w=50000.0,
        unit_active_power_setpoint_channels=["pv.WSet"],
        comms_age_limits={
            comms_age_channel("grid"): 6.0,
            comms_age_channel("pv"): 6.0,
        },
    )


def test_per_device_comms_age_trips_while_global_backstop_healthy():
    st = make_device_age_state(grid_age=0.2, pv_age=7.0)
    b = board()
    make_device_age_safety().execute(st, b)
    assert st.get(SAFE_MODE_CHANNEL) == 1.0
    assert only_claim(b).target_w == 50000.0


def test_per_device_comms_age_recovery_releases_claim():
    st = make_device_age_state(grid_age=0.2, pv_age=7.0)
    safety = make_device_age_safety()
    b = board()
    safety.execute(st, b)
    assert st.get(SAFE_MODE_CHANNEL) == 1.0

    st.apply_driver_value(comms_age_channel("pv"), 0.2)
    safety.execute(st, b)
    assert st.get(SAFE_MODE_CHANNEL) == 0.0
    assert b.valid_requests("pv.WSet", now=0.0) == []


# ── frozen-measurement guard ─────────────────────────────────────────────────
def make_frozen_safety():
    return SafetyController(
        max_comms_age_s=2.0,
        safe_active_power_w=50000.0,
        unit_active_power_setpoint_channels=["pv.WSet"],
        frozen_measurement_channels=["grid.W"],
        max_frozen_s=10.0,
    )


def test_frozen_measurement_trips(state):
    """A bus that answers but serves a bit-identical measurement for too long
    (hung meter/gateway) must trip exactly like a dead bus."""
    safety = make_frozen_safety()
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.1)  # comms healthy
    state.apply_driver_value("grid.W", -5000.0)

    b.tick(0.0)
    safety.execute(state, b)  # first sight — starts the freeze clock
    assert state.get(SAFE_MODE_CHANNEL) == 0.0

    b.tick(11.0)  # unchanged for 11 s > 10 s limit
    safety.execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 1.0
    assert only_claim(b).target_w == 50000.0


def test_changing_measurement_never_trips(state):
    safety = make_frozen_safety()
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.1)
    for now, value in [(0.0, -5000.0), (11.0, -5001.0), (22.0, -5000.0)]:
        state.apply_driver_value("grid.W", value)
        b.tick(now)
        safety.execute(state, b)
        assert state.get(SAFE_MODE_CHANNEL) == 0.0
    assert b.valid_requests("pv.WSet", now=22.0) == []


def test_frozen_trip_releases_when_value_moves_again(state):
    safety = make_frozen_safety()
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.1)
    state.apply_driver_value("grid.W", -5000.0)
    b.tick(0.0)
    safety.execute(state, b)
    b.tick(11.0)
    safety.execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 1.0

    state.apply_driver_value("grid.W", -4000.0)  # meter alive again
    b.tick(12.0)
    safety.execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 0.0
    assert b.valid_requests("pv.WSet", now=12.0) == []


def test_frozen_guard_disabled_without_config(state):
    """Default-constructed safety (no frozen params) must ignore frozen tags —
    backward compatible with sites that have not opted in."""
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.1)
    state.apply_driver_value("grid.W", -5000.0)
    safety = make_safety()
    for now in (0.0, 100.0, 1000.0):
        b.tick(now)
        safety.execute(state, b)
    assert state.get(SAFE_MODE_CHANNEL) == 0.0


def test_trip_logs_once(state, caplog):
    import logging

    safety = make_safety()
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 5.0)
    with caplog.at_level(logging.WARNING):
        safety.execute(state, b)
        safety.execute(state, b)  # second stale cycle must not re-log
    trips = [r for r in caplog.records if "SAFETY TRIP" in r.message]
    assert len(trips) == 1


# ── event journal: a trip raises one alarm, a release clears it ──────────────
class FakeJournal:
    """Captures the journal calls the controller makes."""

    def __init__(self) -> None:
        self.raised: list[tuple] = []
        self.cleared: list[tuple] = []

    def raise_alarm(self, source, key, message, severity="alarm", *, now):
        self.raised.append((source, key, severity, message, now))
        return True

    def clear(self, source, key, *, now, message="cleared"):
        self.cleared.append((source, key, now))
        return True


def make_journal_safety(journal):
    return SafetyController(
        max_comms_age_s=2.0,
        safe_active_power_w=50000.0,
        unit_active_power_setpoint_channels=["pv.WSet"],
        journal=journal,
    )


def test_trip_then_release_emits_journal_alarm(state):
    j = FakeJournal()
    safety = make_journal_safety(j)
    b = board()

    state.apply_driver_value(COMMS_AGE_CHANNEL, 5.0)
    b.tick(1.0)
    safety.execute(state, b)
    safety.execute(state, b)  # still tripped → raise exactly once
    assert len(j.raised) == 1
    source, key, severity, message, now = j.raised[0]
    assert (source, key, severity) == ("safety", "safety.trip", "alarm")
    assert "comms age" in message
    assert now == 1.0
    assert j.cleared == []

    state.apply_driver_value(COMMS_AGE_CHANNEL, 0.1)
    b.tick(2.0)
    safety.execute(state, b)
    assert j.cleared == [("safety", "safety.trip", 2.0)]


def test_journal_is_optional(state):
    """A journal-less safety controller behaves exactly as before."""
    safety = make_safety()
    b = board()
    state.apply_driver_value(COMMS_AGE_CHANNEL, 5.0)
    safety.execute(state, b)  # must not raise without a journal
    assert state.get(SAFE_MODE_CHANNEL) == 1.0
