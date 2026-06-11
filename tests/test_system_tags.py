"""system_tags must stay THE single definition of every internal name —
the re-exports in the producing modules must be the same objects, so a
rename in system_tags.py provably propagates everywhere."""
from pyems import system_tags
from pyems.controllers import safety, setpoint_compliance
from pyems.drivers import cached


def test_reexports_are_the_same_objects():
    assert cached.COMMS_AGE_CHANNEL is system_tags.COMMS_AGE_CHANNEL
    assert safety.SAFE_MODE_CHANNEL is system_tags.SAFE_MODE_CHANNEL
    assert safety.SAFETY_REQUESTER is system_tags.SAFETY_REQUESTER
    assert (
        setpoint_compliance.SETPOINT_VIOLATION_CHANNEL
        is system_tags.SETPOINT_VIOLATION_CHANNEL
    )


def test_system_tags_use_sys_namespace_and_unique():
    tags = [
        system_tags.COMMS_AGE_CHANNEL,
        system_tags.SAFE_MODE_CHANNEL,
        system_tags.SETPOINT_VIOLATION_CHANNEL,
    ]
    assert all(t.startswith("sys.") for t in tags)
    requesters = [
        system_tags.SAFETY_REQUESTER,
        system_tags.EXPORT_LIMIT_REQUESTER,
        system_tags.CONNECTION_POINT_POWER_REQUESTER,
        system_tags.IMPORT_LIMIT_REQUESTER,
        system_tags.SETPOINT_HEADROOM_REQUESTER,
    ]
    assert len(set(tags)) == len(tags)
    assert len(set(requesters)) == len(requesters)


def test_build_tasks_requesters_match_registry():
    """The names controllers actually post under must be the registry ones."""
    from pyems.ems import build_tasks

    site = {
        "scenario": {"control_mode": "export_limit"},
        "control": {"fast_cycle_s": 1.0},
        "export_limit": {
            "limit_w": 30000.0, "priority": 5,
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "connection_point_active_power": {
            "export_limit_w": 30000.0, "import_limit_w": 1e9, "priority": 10,
            "gains": {"kp": 0.4, "ki": 0.08, "kd": 0.0, "tt": 5.0},
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "safety": {"max_comms_age_s": 2.0,
                   "unit_active_power_setpoint_channels": ["pv.WSet"]},
        "allocation": {"channels": [{
            "setpoint_channel": "pv.WSet", "p_min_w": 0.0, "p_max_w": 100000.0,
            "default_w": 100000.0, "deadband_w": 200.0,
        }]},
    }
    fast = next(t for t in build_tasks(site) if t.name == "fast")
    names = {getattr(c, "_name", None) for c in fast.controllers}
    assert system_tags.EXPORT_LIMIT_REQUESTER in names
    assert system_tags.CONNECTION_POINT_POWER_REQUESTER in names
    assert system_tags.SETPOINT_HEADROOM_REQUESTER in names
