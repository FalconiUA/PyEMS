"""Tests for the SystemState tag database (src/channels.py)."""
import pytest

from pyems.channels import Channel, SystemState


def test_get_returns_channel_value():
    st = SystemState([Channel("pv.W", value=1234.0)])
    assert st.get("pv.W") == 1234.0


def test_set_writable_channel():
    st = SystemState([Channel("pv.WSet", writable=True, min_val=0, max_val=100)])
    st.set("pv.WSet", 50.0)
    assert st.get("pv.WSet") == 50.0


def test_set_read_only_channel_raises():
    st = SystemState([Channel("grid.W", writable=False)])
    with pytest.raises(ValueError, match="read-only"):
        st.set("grid.W", 10.0)


def test_set_clamps_to_limits():
    st = SystemState([Channel("pv.WSet", writable=True, min_val=0, max_val=100)])
    st.set("pv.WSet", 999.0)
    assert st.get("pv.WSet") == 100.0  # clamped to max
    st.set("pv.WSet", -50.0)
    assert st.get("pv.WSet") == 0.0    # clamped to min


def test_unknown_channel_raises_keyerror():
    st = SystemState([Channel("pv.W")])
    with pytest.raises(KeyError):
        st.get("does.not.exist")


def test_snapshot_returns_all_values():
    st = SystemState([Channel("a", value=1.0), Channel("b", value=2.0)])
    assert st.snapshot() == {"a": 1.0, "b": 2.0}
