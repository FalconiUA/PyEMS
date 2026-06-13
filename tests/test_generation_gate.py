"""Generation gate: the operational interlock that pins the unit to a safe
floor until an operator enables production (src/pyems/controllers/generation_gate.py
plus the wiring in src/pyems/ems.py).
"""
import pytest

from pyems.allocation.allocator import PowerAllocator, SetpointChannelConfig
from pyems.allocation.request import RequestBoard
from pyems.channels import Channel, SystemState
from pyems.controllers.generation_gate import GenerationGateController
from pyems.ems import _generation_gate_config, build_tasks, required_channels
from pyems.system_tags import (
    GENERATION_ALLOWED_CHANNEL,
    GENERATION_GATE_ACTIVE_CHANNEL,
    GENERATION_GATE_REQUESTER,
)


def make_state(allowed: float, *, p_min=0.0, p_max=100000.0) -> SystemState:
    return SystemState(
        [
            Channel(GENERATION_ALLOWED_CHANNEL, value=allowed, min_val=0, max_val=1, writable=True),
            Channel(GENERATION_GATE_ACTIVE_CHANNEL, min_val=0, max_val=1, writable=True),
            Channel("pv.WSet", min_val=p_min, max_val=p_max, writable=True),
        ]
    )


def make_gate(floor_w=0.0, priority=1) -> GenerationGateController:
    return GenerationGateController(
        name=GENERATION_GATE_REQUESTER,
        priority=priority,
        unit_active_power_setpoint_channel="pv.WSet",
        floor_w=floor_w,
    )


def posted(board):
    return board.valid_requests("pv.WSet", now=0.0)


def test_priority_zero_rejected():
    with pytest.raises(ValueError, match="priority 0 is reserved"):
        make_gate(priority=0)


def test_disabled_pins_to_floor():
    board = RequestBoard(["pv.WSet"])
    board.tick(0.0)
    state = make_state(allowed=0.0)
    make_gate(floor_w=0.0).execute(state, board)
    (req,) = posted(board)
    assert req.requester == GENERATION_GATE_REQUESTER
    assert req.priority == 1
    assert req.min_w == req.max_w == req.target_w == 0.0
    assert state.get(GENERATION_GATE_ACTIVE_CHANNEL) == 1.0


def test_enabled_withdraws_claim():
    board = RequestBoard(["pv.WSet"])
    board.tick(0.0)
    gate = make_gate(floor_w=0.0)
    # first disabled (posts), then enabled (withdraws)
    gate.execute(make_state(allowed=0.0), board)
    assert posted(board)
    gate.execute(make_state(allowed=1.0), board)
    assert posted(board) == []


def test_storage_floor_is_not_a_forced_charge():
    """A storage unit (p_min < 0): the gate parks at the configured floor (0),
    never at p_min — disabling generation must not command a charge."""
    board = RequestBoard(["pv.WSet"])
    board.tick(0.0)
    state = make_state(allowed=0.0, p_min=-50000.0, p_max=50000.0)
    make_gate(floor_w=0.0).execute(state, board)
    (req,) = posted(board)
    assert req.target_w == 0.0


# ── floor derivation in ems._generation_gate_config ──────────────────────────
def _site(command_json="logs/commands.json", p_min=0.0, p_max=100000.0):
    site = {
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
            "setpoint_channel": "pv.WSet", "p_min_w": p_min, "p_max_w": p_max,
            "default_w": p_max, "deadband_w": 200.0,
        }]},
    }
    if command_json is not None:
        site["control"]["command_json"] = command_json
    return site


def test_no_command_json_means_no_gate():
    site = _site(command_json=None)
    assert _generation_gate_config(site) is None
    fast = next(t for t in build_tasks(site) if t.name == "fast")
    assert not any(isinstance(c, GenerationGateController) for c in fast.controllers)
    assert GENERATION_ALLOWED_CHANNEL not in set(required_channels(site))


def test_command_json_enables_gate_and_required_channels():
    site = _site()
    cfg = _generation_gate_config(site)
    assert cfg["unit_active_power_setpoint_channel"] == "pv.WSet"
    fast = next(t for t in build_tasks(site) if t.name == "fast")
    assert any(isinstance(c, GenerationGateController) for c in fast.controllers)
    req = set(required_channels(site))
    assert GENERATION_ALLOWED_CHANNEL in req
    assert GENERATION_GATE_ACTIVE_CHANNEL in req


@pytest.mark.parametrize(
    "p_min, p_max, expected_floor",
    [
        (0.0, 100000.0, 0.0),       # PV: 0 inside envelope -> 0
        (-50000.0, 50000.0, 0.0),   # storage: 0 inside envelope -> 0 (idle)
        (20000.0, 100000.0, 20000.0),  # must-run genset: 0 outside -> p_min
    ],
)
def test_floor_derivation(p_min, p_max, expected_floor):
    cfg = _generation_gate_config(_site(p_min=p_min, p_max=p_max))
    assert cfg["floor_w"] == expected_floor


# ── end-to-end through the allocator ─────────────────────────────────────────
def test_gate_pins_then_releases_through_allocator():
    board = RequestBoard(["pv.WSet"])
    alloc = PowerAllocator(
        [SetpointChannelConfig(
            setpoint_channel="pv.WSet", p_min_w=0.0, p_max_w=100000.0,
            default_w=100000.0, ramp_rate_w_per_s=1e9, deadband_w=0.0,
        )],
        board,
        cycle_s=1.0,
    )
    gate = make_gate(floor_w=0.0)

    # disabled -> allocator lands the floor (first cycle has no ramp reference)
    board.tick(0.0)
    state = make_state(allowed=0.0)
    gate.execute(state, board)
    alloc.resolve(state, now=0.0)
    assert state.get("pv.WSet") == 0.0

    # enabled -> claim withdrawn, allocator falls back to default and ramps up.
    # The arbiter retains last_setpoint (0) internally, so a fresh state is fine.
    board.tick(1.0)
    state2 = make_state(allowed=1.0)
    gate.execute(state2, board)
    alloc.resolve(state2, now=1.0)
    assert state2.get("pv.WSet") == 100000.0
