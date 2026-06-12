"""Tests for fail-fast binding validation (src/pyems/ems.py)."""
import pytest

from pyems.channels import Channel
from pyems.ems import (
    required_channels,
    validate_binding_directions,
    validate_bindings,
    validate_safety_allocation,
    validate_setpoint_keepalive,
    validate_write_age_guard,
)
from pyems.system_tags import comms_age_channel


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
    pool = ["grid.W", "pv.W", "pv.WSet", "sys.safe_mode", "sys.comms_age_s",
            "sys.write_age_s"]
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


# ── safety vs allocation consistency (a trip must be able to land) ───────────
def test_safety_allocation_consistent_site_passes():
    validate_safety_allocation(make_site())  # must not raise


def test_safe_value_outside_device_envelope_raises():
    # Safe value = export limit 200 kW, but the unit envelope tops at 100 kW:
    # the priority-0 claim would intersect to empty and be rejected at trip time.
    site = make_site()
    site["export_limit"]["limit_w"] = 200000.0
    with pytest.raises(ValueError, match="envelope"):
        validate_safety_allocation(site)


def test_guarded_channel_missing_from_allocation_raises():
    # Safety guards a channel the board does not know: the FIRST trip would
    # raise mid-control instead of curtailing. Must fail at startup.
    site = make_site()
    site["safety"]["unit_active_power_setpoint_channels"] = ["bat.WSet"]
    with pytest.raises(ValueError, match="bat.WSet"):
        validate_safety_allocation(site)


# ── keep-alive vs device comms watchdog ──────────────────────────────────────
def test_keepalive_validation_skipped_without_watchdog_key():
    validate_setpoint_keepalive(make_site())  # must not raise


def test_keepalive_too_slow_for_device_watchdog_raises():
    site = make_site()
    site["safety"]["device_comms_watchdog_s"] = 15.0  # default rewrite 10 s
    with pytest.raises(ValueError, match="setpoint_rewrite_s"):
        validate_setpoint_keepalive(site)


def test_keepalive_fast_enough_passes():
    site = make_site()
    site["safety"]["device_comms_watchdog_s"] = 60.0
    site["control"]["setpoint_rewrite_s"] = 10.0
    validate_setpoint_keepalive(site)  # must not raise


# ── write-age guard tuning ───────────────────────────────────────────────────
def test_write_age_guard_skipped_when_unset():
    validate_write_age_guard(make_site())  # no max_write_age_s → no-op


def test_write_age_below_keepalive_floor_raises():
    site = make_site()
    site["control"]["poll_interval_s"] = 0.5
    site["control"]["setpoint_rewrite_s"] = 10.0
    site["safety"]["max_write_age_s"] = 8.0  # < 10 + 2*0.5 = 11 floor
    with pytest.raises(ValueError, match="max_write_age_s"):
        validate_write_age_guard(site)


def test_write_age_above_device_watchdog_raises():
    site = make_site()
    site["control"]["poll_interval_s"] = 0.5
    site["control"]["setpoint_rewrite_s"] = 10.0
    site["safety"]["device_comms_watchdog_s"] = 12.0
    site["safety"]["max_write_age_s"] = 15.0  # > watchdog 12
    with pytest.raises(ValueError, match="device_comms_watchdog_s"):
        validate_write_age_guard(site)


def test_write_age_guard_well_tuned_passes():
    site = make_site()
    site["control"]["poll_interval_s"] = 0.5
    site["control"]["setpoint_rewrite_s"] = 10.0
    site["safety"]["device_comms_watchdog_s"] = 60.0
    site["safety"]["max_write_age_s"] = 15.0
    validate_write_age_guard(site)  # must not raise


def test_required_channels_lists_write_age():
    assert "sys.write_age_s" in set(required_channels(make_site()))


def test_required_channels_include_optional_guards():
    site = make_site()
    site["safety"]["frozen_measurement_channels"] = ["grid.W"]
    site["setpoint_compliance"] = {
        "unit_active_power_channel": "pv.W",
        "unit_active_power_setpoint_channel": "pv.WSet",
    }
    req = set(required_channels(site))
    assert {"grid.W", "pv.W", "pv.WSet", "sys.setpoint_violation"} <= req


def test_required_channels_include_configured_device_comms_ages():
    site = make_site()
    site["safety"]["device_comms_max_age_s"] = {"grid": 6.0, "pv": 6.0}
    req = set(required_channels(site))
    assert {comms_age_channel("grid"), comms_age_channel("pv")} <= req


def test_device_comms_age_typo_fails_binding_validation():
    site = make_site()
    site["safety"]["device_comms_max_age_s"] = {"pvv": 6.0}
    pool = [
        "grid.W",
        "pv.W",
        "pv.WSet",
        "sys.safe_mode",
        "sys.comms_age_s",
        "sys.write_age_s",
        comms_age_channel("grid"),
        comms_age_channel("pv"),
    ]
    with pytest.raises(ValueError, match="sys.pvv.comms_age_s"):
        validate_bindings(site, pool)
