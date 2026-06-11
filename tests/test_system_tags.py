"""system_tags must stay THE single definition of every internal name —
the producing modules import (never redefine) the same objects, so a
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


def _registry_constants() -> dict[str, str]:
    """Every UPPER_CASE string constant in system_tags — introspected, so a
    NEWLY ADDED tag or requester is covered by these checks automatically,
    with no test edits."""
    return {
        name: value
        for name, value in vars(system_tags).items()
        if name.isupper() and isinstance(value, str)
    }


def test_registry_naming_convention_and_uniqueness():
    consts = _registry_constants()
    channels = {n: v for n, v in consts.items() if n.endswith("_CHANNEL")}
    requesters = {n: v for n, v in consts.items() if n.endswith("_REQUESTER")}
    # every constant is either a *_CHANNEL or a *_REQUESTER — no third family
    assert set(consts) == set(channels) | set(requesters), (
        "system_tags constants must end in _CHANNEL or _REQUESTER"
    )
    # status words live in the sys. namespace, never clash with device tags
    assert all(v.startswith("sys.") for v in channels.values()), (
        "every *_CHANNEL constant must be a sys.* tag"
    )
    # all names unique across BOTH families (requesters appear in logs/boards)
    values = list(channels.values()) + list(requesters.values())
    assert len(set(values)) == len(values), "duplicate internal name"


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
