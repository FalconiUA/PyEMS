"""Tests for RequestBoard + ActivePowerRequest (src/pyems/allocation/request.py)."""
import logging

import pytest

from pyems.allocation.request import ActivePowerRequest, RequestBoard

CH = "pv.WSet"


def board() -> RequestBoard:
    return RequestBoard([CH, "ess.WSet"])


def req(requester="a", priority=10, **kw) -> ActivePowerRequest:
    return ActivePowerRequest(requester=requester, priority=priority, **kw)


# -- post / replace / withdraw -------------------------------------------------

def test_post_then_valid():
    b = board()
    b.post(CH, req("a", max_w=50000.0), now=0.0)
    got = b.valid_requests(CH, now=0.0)
    assert [r.requester for r in got] == ["a"]
    assert got[0].max_w == 50000.0


def test_repost_replaces_same_requester():
    b = board()
    b.post(CH, req("a", max_w=50000.0), now=0.0)
    b.post(CH, req("a", max_w=30000.0), now=0.0)  # same requester, new claim
    got = b.valid_requests(CH, now=0.0)
    assert len(got) == 1
    assert got[0].max_w == 30000.0


def test_withdraw_removes_claim():
    b = board()
    b.post(CH, req("a"), now=0.0)
    b.withdraw(CH, "a")
    assert b.valid_requests(CH, now=0.0) == []


def test_withdraw_absent_is_noop():
    b = board()
    b.withdraw(CH, "nobody")  # must not raise
    b.post(CH, req("a"), now=0.0)
    b.withdraw(CH, "nobody")  # still a no-op, leaves 'a' alone
    assert [r.requester for r in b.valid_requests(CH, now=0.0)] == ["a"]


# -- TTL -----------------------------------------------------------------------

def test_ttl_valid_before_expiry_gone_after():
    b = board()
    b.tick(100.0)
    b.post(CH, req("a", ttl_s=10.0))  # posted at tick time 100.0
    assert b.valid_requests(CH, now=105.0) != []   # within window
    assert b.valid_requests(CH, now=111.0) == []   # past expiry -> purged


def test_ttl_purge_logs_once(caplog):
    b = board()
    b.post(CH, req("a", ttl_s=10.0), now=0.0)
    with caplog.at_level(logging.WARNING):
        b.valid_requests(CH, now=20.0)   # purge + warn
        b.valid_requests(CH, now=20.0)   # already gone -> no second warning
    drops = [r for r in caplog.records if "TTL" in r.message]
    assert len(drops) == 1


# -- ordering ------------------------------------------------------------------

def test_valid_requests_sorted_by_priority_then_requester():
    b = board()
    b.post(CH, req("z", priority=5), now=0.0)
    b.post(CH, req("a", priority=5), now=0.0)
    b.post(CH, req("m", priority=0), now=0.0)
    order = [(r.priority, r.requester) for r in b.valid_requests(CH, now=0.0)]
    assert order == [(0, "m"), (5, "a"), (5, "z")]


def test_tick_drives_post_time():
    b = board()
    b.tick(50.0)
    b.post(CH, req("a", ttl_s=5.0))  # no explicit now -> uses tick time 50.0
    assert b.valid_requests(CH, now=54.0) != []
    assert b.valid_requests(CH, now=56.0) == []


# -- validation ----------------------------------------------------------------

def test_min_greater_than_max_rejected():
    with pytest.raises(ValueError, match="min_w"):
        req("a", min_w=100.0, max_w=10.0)


def test_target_outside_range_rejected():
    with pytest.raises(ValueError, match="target_w"):
        req("a", min_w=0.0, max_w=100.0, target_w=500.0)


def test_negative_priority_rejected():
    with pytest.raises(ValueError, match="priority"):
        req("a", priority=-1)


def test_non_positive_ttl_rejected():
    with pytest.raises(ValueError, match="ttl_s"):
        req("a", ttl_s=0.0)


def test_post_unknown_channel_rejected():
    b = board()
    with pytest.raises(ValueError, match="unknown setpoint channel"):
        b.post("nope.WSet", req("a"), now=0.0)
