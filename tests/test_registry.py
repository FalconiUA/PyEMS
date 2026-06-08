"""
Tests for the controller registry and declarative scenario assembly
(src/pyems/controllers/registry.py). This is the seam that makes a control
scenario data: build controllers by `type` + `params`, validate bindings.
"""
import pytest

import pyems.controllers  # noqa: F401  — populate the registry
from pyems.controllers.grid_export_limit import GridExportLimitController
from pyems.controllers.registry import (
    BuildContext,
    build_controller,
    registered_types,
)
from pyems.controllers.safety import SafetyController


def make_ctx() -> BuildContext:
    """A representative tag pool: one meter, one inverter, the safe-mode tag."""
    return BuildContext(
        cycle_s=1.0,
        channel_names=frozenset({"grid.W", "pv.W", "pv.WSet", "sys.safe_mode"}),
        writable_names=frozenset({"pv.WSet", "sys.safe_mode"}),
    )


def test_builtin_types_registered():
    types = registered_types()
    assert types["safety"] is SafetyController
    assert types["grid_export_limit"] is GridExportLimitController


def test_build_grid_export_limit_from_config():
    spec = {
        "type": "grid_export_limit",
        "params": {
            "export_limit_w": 50000.0,
            "p_max_w": 100000.0,
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
    }
    ctrl = build_controller(spec, make_ctx())
    assert isinstance(ctrl, GridExportLimitController)


def test_build_safety_from_config():
    spec = {
        "type": "safety",
        "params": {
            "max_comms_age_s": 2.0,
            "safe_active_power_w": 50000.0,
            "unit_active_power_setpoint_channels": ["pv.WSet"],
        },
    }
    ctrl = build_controller(spec, make_ctx())
    assert isinstance(ctrl, SafetyController)


def test_unknown_type_rejected():
    with pytest.raises(ValueError, match="unknown controller type"):
        build_controller({"type": "does_not_exist", "params": {}}, make_ctx())


def test_unknown_read_binding_rejected():
    spec = {
        "type": "grid_export_limit",
        "params": {
            "export_limit_w": 50000.0,
            "p_max_w": 100000.0,
            "connection_point_active_power_channel": "grid.TYPO",  # not in pool
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
    }
    with pytest.raises(ValueError, match="not a known tag"):
        build_controller(spec, make_ctx())


def test_setpoint_binding_must_be_writable():
    spec = {
        "type": "grid_export_limit",
        "params": {
            "export_limit_w": 50000.0,
            "p_max_w": 100000.0,
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.W",  # read-only — invalid setpoint
        },
    }
    with pytest.raises(ValueError, match="read-only"):
        build_controller(spec, make_ctx())
