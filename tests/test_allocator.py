"""Tests for the arbitration algorithm (src/pyems/allocation/allocator.py, §3.3)."""
import logging

import pytest

from pyems.allocation.allocator import (
    ChannelArbiter,
    PowerAllocator,
    SetpointChannelConfig,
)
from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import Channel, SystemState

CH = "pv.WSet"


def cfg(**kw) -> SetpointChannelConfig:
    params = dict(
        setpoint_channel=CH,
        p_min_w=0.0,
        p_max_w=100000.0,
        default_w=100000.0,
        ramp_rate_w_per_s=1e9,  # effectively unlimited unless a test overrides
        deadband_w=0.0,
    )
    params.update(kw)
    return SetpointChannelConfig(**params)


def arbiter(**kw) -> ChannelArbiter:
    return ChannelArbiter(cfg(**kw), cycle_s=1.0)


def req(requester="a", priority=10, **kw) -> ActivePowerRequest:
    return ActivePowerRequest(requester=requester, priority=priority, **kw)


# -- target selection & range intersection ------------------------------------

def test_single_target_written_clamped_to_envelope():
    a = arbiter(p_max_w=80000.0)
    # target above the device envelope clamps down to p_max.
    assert a.resolve([req(target_w=95000.0, max_w=200000.0)]) == 80000.0


def test_constraint_only_plus_lower_priority_target():
    a = arbiter()
    cap = req("export", priority=5, max_w=40000.0)          # pure constraint
    plan = req("tou", priority=50, target_w=70000.0)        # preference
    # plan's target clamps under the compliance cap.
    assert a.resolve([cap, plan]) == 40000.0


def test_disjoint_ranges_higher_priority_wins_and_logs_once(caplog):
    a = arbiter()
    hi = req("compliance", priority=5, min_w=0.0, max_w=30000.0)
    lo = req("economic", priority=50, min_w=60000.0, max_w=80000.0, target_w=70000.0)
    with caplog.at_level(logging.WARNING):
        v1 = a.resolve([hi, lo])
        warns = [r for r in caplog.records if "rejected" in r.message]
        v2 = a.resolve([hi, lo])  # steady state -> no repeat warning
    # result lands in the higher-priority range; lower's target discarded.
    assert v1 == 30000.0 and v2 == 30000.0
    assert len(warns) == 1


def test_no_target_holds_last_after_first_cycle():
    a = arbiter()
    a.resolve([req("x", target_w=40000.0)])          # establishes last
    # next cycle only a pure constraint that permits a wide range -> hold 40k.
    held = a.resolve([req("x", max_w=90000.0)])
    assert held == 40000.0


def test_no_target_first_ever_cycle_uses_default():
    a = arbiter(default_w=100000.0)
    # only a pure constraint, never resolved before -> default clamped under cap.
    v = a.resolve([req("x", max_w=60000.0)])
    assert v == 60000.0


def test_no_requests_at_all_uses_default():
    a = arbiter(default_w=100000.0)
    assert a.resolve([]) == 100000.0


def test_no_requests_returns_default_even_after_resolving():
    a = arbiter(default_w=100000.0)
    a.resolve([req("x", target_w=20000.0)])
    # board empties (claim withdrawn): fail-safe back to default, not hold.
    assert a.resolve([]) == 100000.0


# -- deadband ------------------------------------------------------------------

def test_deadband_suppresses_micro_move():
    a = arbiter(deadband_w=5000.0)
    a.resolve([req("x", target_w=40000.0)])
    # +1 kW request is within deadband -> stays at 40k.
    assert a.resolve([req("x", target_w=41000.0)]) == 40000.0


def test_priority_zero_bypasses_deadband():
    a = arbiter(deadband_w=5000.0)
    a.resolve([req("x", target_w=40000.0)])
    safe = req("safety", priority=0, min_w=41000.0, max_w=41000.0, target_w=41000.0)
    # within deadband, but safety must land exactly.
    assert a.resolve([safe]) == 41000.0


# -- ramp ----------------------------------------------------------------------

def test_ramp_limits_the_step():
    a = arbiter(ramp_rate_w_per_s=1000.0)   # 1 kW/cycle at cycle_s=1
    a.resolve([req("x", target_w=100000.0)])   # first cycle lands at 100k
    # now ask for a big drop; limited to one 1 kW step.
    assert a.resolve([req("x", target_w=0.0)]) == 99000.0


def test_asymmetric_ramp_limits_up_and_down_independently():
    a = arbiter(ramp_up_w_per_s=1000.0, ramp_down_w_per_s=5000.0)
    a.resolve([req("x", target_w=50000.0)])
    assert a.resolve([req("x", target_w=100000.0)]) == 51000.0
    assert a.resolve([req("x", target_w=0.0)]) == 46000.0


def test_priority_zero_bypasses_ramp():
    a = arbiter(ramp_rate_w_per_s=1000.0)
    a.resolve([req("x", target_w=100000.0)])
    safe = req("safety", priority=0, min_w=50000.0, max_w=50000.0, target_w=50000.0)
    # safety step lands in a single cycle despite the slow ramp.
    assert a.resolve([safe]) == 50000.0


# -- determinism & TTL ---------------------------------------------------------

def test_determinism_regardless_of_post_order():
    r1 = req("a", priority=10, max_w=60000.0)
    r2 = req("b", priority=10, min_w=10000.0, target_w=55000.0)
    a1, a2 = arbiter(), arbiter()
    assert a1.resolve([r1, r2]) == a2.resolve([r2, r1])


def test_ttl_expiry_mid_run_falls_back():
    board = RequestBoard([CH])
    alloc = PowerAllocator([cfg(default_w=100000.0)], board, cycle_s=1.0)
    state = SystemState([Channel(CH, writable=True, min_val=0, max_val=100000)])

    board.post(CH, req("plan", target_w=30000.0, ttl_s=10.0), now=0.0)
    alloc.resolve(state, now=0.0)
    assert state.get(CH) == 30000.0
    # after TTL the claim vanishes -> §3.3.3 fail-safe to default.
    alloc.resolve(state, now=20.0)
    assert state.get(CH) == 100000.0


def test_allocator_never_writes_unconfigured_channels():
    board = RequestBoard([CH])
    alloc = PowerAllocator([cfg()], board, cycle_s=1.0)
    state = SystemState([
        Channel(CH, writable=True, min_val=0, max_val=100000),
        Channel("ess.WSet", writable=True, min_val=-50000, max_val=50000, value=123.0),
    ])
    alloc.resolve(state, now=0.0)
    assert state.get("ess.WSet") == 123.0  # untouched
