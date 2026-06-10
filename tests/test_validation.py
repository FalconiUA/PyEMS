"""Tests for fail-fast binding validation (src/pyems/ems.py)."""
import pytest

from pyems.channels import Channel
from pyems.ems import required_channels, validate_binding_directions, validate_bindings


def make_site():
    return {
        "control": {"fast_cycle_s": 1.0},
        "export_limit": {
            "limit_w": 50000.0,
            "priority": 5,
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "connection_point_active_power": {
            "export_limit_w": 50000.0,
            "import_limit_w": 100000.0,
            "priority": 10,
            "gains": {"kp": 0.4, "ki": 0.08, "kd": 0.0, "tt": 5.0},
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "safety": {
            "max_comms_age_s": 2.0,
            "unit_active_power_setpoint_channels": ["pv.WSet"],
        },
        "allocation": {
            "channels": [
                {
                    "setpoint_channel": "pv.WSet",
                    "p_min_w": 0.0,
                    "p_max_w": 100000.0,
                    "default_w": 100000.0,
                },
            ],
        },
    }


def test_required_channels_lists_all_bindings():
    req = set(required_channels(make_site()))
    assert {"grid.W", "pv.W", "pv.WSet", "sys.safe_mode"} <= req


def test_validate_passes_when_all_present():
    site = make_site()
    pool = ["grid.W", "pv.W", "pv.WSet", "sys.safe_mode", "sys.comms_age_s"]
    validate_bindings(site, pool)  # must not raise


def test_validate_raises_on_missing_tag():
    site = make_site()
    site["export_limit"]["unit_active_power_channel"] = "pv.Wx"  # typo
    pool = ["grid.W", "pv.W", "pv.WSet", "sys.safe_mode"]
    with pytest.raises(ValueError, match="pv.Wx"):
        validate_bindings(site, pool)


def test_validate_raises_on_missing_connection_point_active_power_tag():
    site = make_site()
    site["connection_point_active_power"]["unit_active_power_setpoint_channel"] = "pv.WSetx"
    pool = ["grid.W", "pv.W", "pv.WSet", "sys.safe_mode"]
    with pytest.raises(ValueError, match="pv.WSetx"):
        validate_bindings(site, pool)


# ── binding direction validation (measurement vs setpoint) ───────────────────
def make_channels(grid_w_writable=False, pv_wset_writable=True):
    return [
        Channel("grid.W", writable=grid_w_writable),
        Channel("pv.W"),
        Channel("pv.WSet", writable=pv_wset_writable),
        Channel("sys.safe_mode", writable=True),
        Channel("sys.comms_age_s"),
    ]


def test_direction_validation_passes_for_correct_profile():
    validate_binding_directions(make_site(), make_channels())  # must not raise


def test_measurement_bound_to_writable_channel_raises():
    # A meter profile mistakenly marking grid.W read_write: the cache would
    # never publish the measurement AND would flush state values to the meter.
    with pytest.raises(ValueError, match="grid.W"):
        validate_binding_directions(make_site(), make_channels(grid_w_writable=True))


def test_setpoint_bound_to_read_only_channel_raises():
    with pytest.raises(ValueError, match="pv.WSet"):
        validate_binding_directions(make_site(), make_channels(pv_wset_writable=False))
