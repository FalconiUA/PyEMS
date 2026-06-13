"""Hard inverter switch: one-shot command path (CachedDriver), the latched
controller, config validation, and the sim plant's enable gate."""
from dataclasses import replace
import math

import pytest
import yaml

from pyems.channels import Channel, SystemState
from pyems.controllers.hard_switch import HardSwitchController
from pyems.drivers.base import Driver
from pyems.drivers.cached import CachedDriver
from pyems.drivers.modbus_device import DeviceProfile, namespaced
from pyems.ems import PROFILES, ROOT, _hard_switch_config, _validate_hard_switch_channels
from pyems.sim.harness import SimHarness
from pyems.sim.plant import GeneratingUnitSim
from pyems.system_tags import (
    INVERTER_COMMAND_CHANNEL,
    INVERTER_COMMAND_ID_CHANNEL,
    INVERTER_RUN_STATE_CHANNEL,
)


# ── CachedDriver one-shot command path ───────────────────────────────────────
class RecordingInner(Driver):
    """Inner driver that records every register written, by tag."""

    def __init__(self) -> None:
        self._channels = [
            Channel("pv.W"),
            Channel("pv.WSet", writable=True, min_val=0, max_val=1e5),
            Channel("pv.StartCmd", writable=True, command=True, min_val=0, max_val=0),
            Channel("pv.StopCmd", writable=True, command=True, min_val=0, max_val=0),
        ]
        self.written: dict[str, float] = {}

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def channels(self) -> list[Channel]:
        return self._channels

    def read_state(self, state: SystemState) -> None: ...

    def write_setpoints(self, state: SystemState, channels=None) -> None:
        for ch in self._channels:
            if ch.writable and (channels is None or ch.name in channels):
                self.written[ch.name] = state.get(ch.name)


def make_cached() -> tuple[CachedDriver, RecordingInner]:
    inner = RecordingInner()
    return CachedDriver(inner, poll_interval_s=0.01), inner


def test_command_channel_excluded_from_continuous_flush():
    drv, inner = make_cached()
    assert "pv.StartCmd" not in drv._writable     # not in the continuous mirror
    assert "pv.StopCmd" not in drv._writable
    assert {"pv.StartCmd", "pv.StopCmd"} <= drv._command
    # a normal setpoint flush must never touch the command register, even on the
    # first cycle (no startup write of its default value)
    state = SystemState(inner.channels())
    state.set("pv.WSet", 5000.0)
    drv.write_setpoints(state)
    drv._flush_setpoints()
    assert inner.written == {"pv.WSet": 5000.0}


def test_send_command_writes_once_forced():
    drv, inner = make_cached()
    drv.send_command("pv.StartCmd", 0.0)
    drv._flush_commands()
    assert inner.written == {"pv.StartCmd": 0.0}
    # queue drained — a second flush writes nothing new
    inner.written.clear()
    drv._flush_commands()
    assert inner.written == {}


def test_send_command_rejects_non_command_channel():
    drv, _ = make_cached()
    with pytest.raises(ValueError, match="not a command channel"):
        drv.send_command("pv.WSet", 1.0)


# ── HardSwitchController (latched, edge-triggered) ───────────────────────────
class FakeSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def send_command(self, tag: str, value: float) -> None:
        self.calls.append((tag, value))


def make_state(cmd: float, cmd_id: float) -> SystemState:
    return SystemState(
        [
            Channel(INVERTER_COMMAND_CHANNEL, value=cmd, min_val=0, max_val=1),
            Channel(INVERTER_COMMAND_ID_CHANNEL, value=cmd_id),
            Channel(INVERTER_RUN_STATE_CHANNEL, min_val=0, max_val=1, writable=True, value=float("nan")),
        ]
    )


def make_controller(sink):
    return HardSwitchController(
        command_sink=sink,
        start_writes=[("pv.StartCmd", 0.0)],
        stop_writes=[("pv.StopCmd", 0.0)],
    )


def test_no_command_does_nothing():
    sink = FakeSink()
    make_controller(sink).execute(make_state(float("nan"), float("nan")), board=None)
    assert sink.calls == []


def test_new_start_id_fires_once():
    sink = FakeSink()
    ctrl = make_controller(sink)
    state = make_state(cmd=1.0, cmd_id=100.0)
    ctrl.execute(state, board=None)
    assert sink.calls == [("pv.StartCmd", 0.0)]
    assert state.get(INVERTER_RUN_STATE_CHANNEL) == 1.0
    # same id again → no refire
    ctrl.execute(make_state(cmd=1.0, cmd_id=100.0), board=None)
    assert sink.calls == [("pv.StartCmd", 0.0)]


def test_new_stop_id_fires_stop_writes():
    sink = FakeSink()
    ctrl = make_controller(sink)
    ctrl.execute(make_state(cmd=1.0, cmd_id=100.0), board=None)
    ctrl.execute(make_state(cmd=0.0, cmd_id=200.0), board=None)
    assert sink.calls == [("pv.StartCmd", 0.0), ("pv.StopCmd", 0.0)]
    state = make_state(cmd=0.0, cmd_id=200.0)
    ctrl.execute(state, board=None)  # already-acted id → no run_state change here
    assert ctrl._last_id == 200.0


# ── ems config parsing / validation ──────────────────────────────────────────
def _hs(start=(("pv.StartCmd", 0),), stop=(("pv.StopCmd", 0),)):
    return {
        "hard_switch": {
            "start_writes": [{"channel": c, "value": v} for c, v in start],
            "stop_writes": [{"channel": c, "value": v} for c, v in stop],
        }
    }


def test_hard_switch_config_absent_is_none():
    assert _hard_switch_config({}) is None


def test_hard_switch_config_parses_pairs():
    cfg = _hard_switch_config(_hs())
    assert cfg["start_writes"] == [("pv.StartCmd", 0.0)]
    assert cfg["stop_writes"] == [("pv.StopCmd", 0.0)]


def test_hard_switch_config_requires_both_lists():
    with pytest.raises(ValueError, match="non-empty"):
        _hard_switch_config({"hard_switch": {"start_writes": [{"channel": "pv.StartCmd", "value": 0}], "stop_writes": []}})


def test_validate_rejects_non_command_channel():
    channels = [
        Channel("pv.StartCmd", writable=True, command=True, min_val=0, max_val=0),
        Channel("pv.StopCmd", writable=True, command=True, min_val=0, max_val=0),
        Channel("pv.WSet", writable=True),
    ]
    _validate_hard_switch_channels(_hard_switch_config(_hs()), channels)  # ok
    with pytest.raises(ValueError, match="not a command register"):
        _validate_hard_switch_channels(
            {"start_writes": [("pv.WSet", 1.0)], "stop_writes": [("pv.WSet", 0.0)]}, channels
        )
    with pytest.raises(ValueError, match="not in the tag pool"):
        _validate_hard_switch_channels(
            {"start_writes": [("pv.Nope", 0.0)], "stop_writes": [("pv.StopCmd", 0.0)]}, channels
        )


# ── sim plant enable gate ────────────────────────────────────────────────────
def test_site_sim_hard_switch_targets_profile_command_channels():
    site = yaml.safe_load((ROOT / "config" / "site.sim.yaml").read_text(encoding="utf-8"))
    assert site["control"]["command_json"] == "logs/commands.json"
    channels = []
    for dev in site["devices"]:
        profile = DeviceProfile.load(PROFILES / dev["profile"])
        channels.extend(
            replace(ch, name=namespaced(ch.name, dev.get("id")))
            for ch in profile.channels()
        )

    _validate_hard_switch_channels(_hard_switch_config(site), channels)
    by_name = {ch.name: ch for ch in channels}
    assert by_name["pv.StartCmd"].command is True
    assert by_name["pv.StopCmd"].command is True


def test_single_runstop_profile_supports_one_register_variant():
    profile = DeviceProfile.load(PROFILES / "inverters/sim_sun2000_runstop.yaml")
    channels = [
        replace(ch, name=namespaced(ch.name, "pv"))
        for ch in profile.channels()
    ]
    cfg = _hard_switch_config(
        {
            "hard_switch": {
                "start_writes": [{"channel": "pv.RunStop", "value": 1}],
                "stop_writes": [{"channel": "pv.RunStop", "value": 0}],
            }
        }
    )

    _validate_hard_switch_channels(cfg, channels)
    assert cfg["start_writes"] == [("pv.RunStop", 1.0)]
    assert cfg["stop_writes"] == [("pv.RunStop", 0.0)]
    assert {ch.name: ch for ch in channels}["pv.RunStop"].command is True


def test_sim_harness_separate_zero_commands_distinguish_start_and_stop():
    site = yaml.safe_load((ROOT / "config" / "site.sim.yaml").read_text(encoding="utf-8"))
    harness = SimHarness(site)

    harness.world.unit.enabled = False
    harness._on_unit_setpoint("StartCmd", 0.0)
    assert harness.world.unit.enabled is True

    harness._on_unit_setpoint("StopCmd", 0.0)
    assert harness.world.unit.enabled is False


def test_sim_harness_single_runstop_distinguishes_values():
    site = yaml.safe_load((ROOT / "config" / "site.sim.yaml").read_text(encoding="utf-8"))
    harness = SimHarness(site)

    harness.world.unit.enabled = False
    harness._on_unit_setpoint("RunStop", 1.0)
    assert harness.world.unit.enabled is True

    harness._on_unit_setpoint("RunStop", 0.0)
    assert harness.world.unit.enabled is False


def test_generating_unit_hard_stop_forces_zero():
    unit = GeneratingUnitSim(p_max_w=100000.0, tau_s=0.0)
    unit.active_power_setpoint_w = 50000.0
    assert unit.step(1.0, available_w=80000.0) == 50000.0  # min(available, setpoint)
    unit.enabled = False
    assert unit.step(1.0, available_w=80000.0) == 0.0      # de-energized despite resource
    unit.enabled = True
    assert unit.step(1.0, available_w=80000.0) == 50000.0  # back online
