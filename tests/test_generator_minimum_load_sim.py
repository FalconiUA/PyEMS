"""Closed-loop scenario simulation for the generator minimum-load module.

Drives the REAL control stack — build_tasks() (safety + export limit +
connection-point PID + headroom + generator minimum load) arbitrated by the
REAL PowerAllocator through Scheduler.step() — against scripted island/grid
plant physics, cycle by cycle:

  phase 1  grid present: the export-limit program governs, generator off;
  phase 2  grid lost, genset carries the site: PV trips (anti-islanding),
           the generator picks up the load, PV returns and is capped so the
           generator never drops below its minimum load;
  phase 3  site load falls: PV is curtailed deeper to hold the floor;
  phase 4  load below the generator floor: PV fully curtailed (cap 0);
  phase 5  grid returns, generator stops: the cap is withdrawn and the
           export-limit program resumes alone.

Physics (one tick per control cycle, generating convention):
  PV tracks min(available, WSet) through a first-order lag (like the sim
  harness plant); on grid  P_grid = load − P_pv, P_gen = 0; on the island
  P_gen = load − P_pv, P_grid = 0 (ATS open). Seeded meter noise keeps the
  run deterministic but honest.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import pytest

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver
from pyems.ems import build_allocation, build_tasks
from pyems.scheduler import Scheduler
from pyems.system_tags import (
    COMMS_AGE_CHANNEL,
    GENERATOR_RUNNING_CHANNEL,
    SAFE_MODE_CHANNEL,
    SETPOINT_VIOLATION_CHANNEL,
    WRITE_AGE_CHANNEL,
)

# Plant nameplate: 100 kW PV unit, 125 kVA / 100 kW genset, 30% minimum load.
PV_P_MAX_W = 100000.0
GEN_S_RATED_VA = 125000.0
GEN_P_RATED_W = 100000.0
GEN_MIN_LOAD_PCT = 30.0
GEN_FLOOR_W = 30000.0
EXPORT_LIMIT_W = 30000.0

CYCLE_S = 1.0
METER_NOISE_W = 100.0


def scenario_site() -> dict:
    """A production-shaped site dict: same sections build_ems consumes."""
    return {
        "scenario": {"control_mode": "export_limit"},
        "control": {"fast_cycle_s": CYCLE_S},
        "export_limit": {
            "limit_w": EXPORT_LIMIT_W, "priority": 5,
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "connection_point_active_power": {
            "export_limit_w": EXPORT_LIMIT_W, "import_limit_w": 1e9, "priority": 10,
            "gains": {"kp": 0.4, "ki": 0.08, "kd": 0.0, "tt": 5.0},
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "safety": {
            "max_comms_age_s": 10.0,
            "unit_active_power_setpoint_channels": ["pv.WSet"],
        },
        "allocation": {"channels": [{
            "setpoint_channel": "pv.WSet",
            "p_min_w": 0.0, "p_max_w": PV_P_MAX_W, "default_w": PV_P_MAX_W,
            "ramp_rate_w_per_s": 5000.0, "ramp_down_w_per_s": 50000.0,
            "deadband_w": 200.0,
        }]},
        "generator_minimum_load": {
            "rated_apparent_power_va": GEN_S_RATED_VA,
            "rated_active_power_w": GEN_P_RATED_W,
            "minimum_load_pct": GEN_MIN_LOAD_PCT,
            "generator_active_power_channel": "gen.W",
            "off_delay_s": 5.0,
        },
    }


def scenario_channels() -> list[Channel]:
    return [
        Channel("grid.W", unit="W"),
        Channel("gen.W", unit="W"),
        Channel("pv.W", unit="W"),
        Channel("pv.WSet", unit="W", min_val=0, max_val=PV_P_MAX_W, writable=True),
        Channel(SAFE_MODE_CHANNEL, min_val=0, max_val=1, writable=True),
        Channel(SETPOINT_VIOLATION_CHANNEL, min_val=0, max_val=1, writable=True),
        Channel(GENERATOR_RUNNING_CHANNEL, min_val=0, max_val=1, writable=True),
        Channel(COMMS_AGE_CHANNEL, unit="s", value=0.0),
        Channel(WRITE_AGE_CHANNEL, unit="s", value=0.0),
    ]


class ScenarioDriver(Driver):
    """In-memory driver (module-scope copy of conftest.FakeDriver: the shared
    fixture is function-scoped and cannot feed a module-scoped history)."""

    def __init__(self, channels: list[Channel]) -> None:
        self._channels = channels
        self.measurements: dict[str, float] = {}
        self.written: dict[str, float] = {}

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def channels(self) -> list[Channel]:
        return self._channels

    def read_state(self, state: SystemState) -> None:
        for name, value in self.measurements.items():
            state.apply_driver_value(name, value)

    def write_setpoints(self, state: SystemState, channels: set[str] | None = None) -> None:
        for ch in self._channels:
            if ch.writable and (channels is None or ch.name in channels):
                self.written[ch.name] = state.get(ch.name)


@dataclass
class IslandPlant:
    """Scripted plant physics for one PV unit + genset + ATS (no Modbus)."""

    pv_available_w: float = 80000.0
    load_w: float = 30000.0
    grid_present: bool = True
    pv_online: bool = True          # anti-islanding trip on grid loss
    pv_tau_s: float = 1.0
    pv_w: float = 0.0
    rng: random.Random = field(default_factory=lambda: random.Random(42))

    def step(self, setpoint_w: float, dt_s: float) -> dict[str, float]:
        target = 0.0
        if self.pv_online:
            target = max(0.0, min(self.pv_available_w, setpoint_w, PV_P_MAX_W))
        alpha = 1.0 - math.exp(-dt_s / self.pv_tau_s)
        self.pv_w += (target - self.pv_w) * alpha
        balance_w = self.load_w - self.pv_w    # + = drawn from the active source
        noise = self.rng.gauss(0.0, METER_NOISE_W)
        if self.grid_present:
            grid_w, gen_w = balance_w + noise, 0.0
        else:
            grid_w, gen_w = 0.0, balance_w + noise
        return {"grid.W": grid_w, "gen.W": gen_w, "pv.W": self.pv_w}


# (start_s, description, plant mutation) — the scenario script.
PHASES = [
    (0.0, "grid present, export-limit program governs",
     dict(grid_present=True, load_w=30000.0, pv_available_w=80000.0, pv_online=True)),
    (60.0, "grid LOST: PV anti-islanding trip, genset picks up the load",
     dict(grid_present=False, load_w=60000.0, pv_online=False)),
    (70.0, "PV back online on the island; min-load cap must govern",
     dict(pv_online=True)),
    (180.0, "island load falls; PV curtailed deeper to hold the floor",
     dict(load_w=35000.0)),
    (260.0, "island load below the generator floor; PV fully curtailed",
     dict(load_w=25000.0)),
    (300.0, "grid RETURNS, generator off; export-limit program resumes",
     dict(grid_present=True, load_w=30000.0)),
]
END_S = 360.0


def run_scenario() -> list[dict[str, float]]:
    """Run the scripted scenario; one history row per control cycle."""
    site = scenario_site()
    channels = scenario_channels()
    driver = ScenarioDriver(channels)
    state = SystemState(channels)
    allocator, board = build_allocation(site)
    sched = Scheduler(build_tasks(site), state, driver, allocator=allocator, board=board)

    plant = IslandPlant()
    history: list[dict[str, float]] = []
    phase_idx = -1
    t = 0.0
    while t < END_S:
        while phase_idx + 1 < len(PHASES) and t >= PHASES[phase_idx + 1][0]:
            phase_idx += 1
            for key, value in PHASES[phase_idx][2].items():
                setattr(plant, key, value)
        setpoint_w = driver.written.get("pv.WSet", PV_P_MAX_W)
        driver.measurements = {
            **plant.step(setpoint_w, CYCLE_S),
            COMMS_AGE_CHANNEL: 0.1,
            WRITE_AGE_CHANNEL: 0.1,
        }
        sched.step(now=t)
        history.append({
            "t_s": t,
            "phase": float(phase_idx),
            "load_w": plant.load_w,
            "pv_available_w": plant.pv_available_w if plant.pv_online else 0.0,
            "grid_w": state.get("grid.W"),
            "gen_w": state.get("gen.W"),
            "pv_w": state.get("pv.W"),
            "pv_wset_w": driver.written["pv.WSet"],
            "generator_running": state.get(GENERATOR_RUNNING_CHANNEL),
            "safe_mode": state.get(SAFE_MODE_CHANNEL),
        })
        t += CYCLE_S
    return history


def window(history, t_from, t_to):
    return [r for r in history if t_from <= r["t_s"] < t_to]


@pytest.fixture(scope="module")
def history():
    return run_scenario()


def test_phase1_grid_export_limited_generator_ignored(history):
    steady = window(history, 30.0, 60.0)
    # export magnitude held at the 30 kW limit (PID converged, noise allowed)
    assert all(r["grid_w"] > -(EXPORT_LIMIT_W + 1500.0) for r in steady)
    # PV carries load + full allowed export: ~60 kW
    assert steady[-1]["pv_w"] == pytest.approx(60000.0, abs=3000.0)
    assert all(r["generator_running"] == 0.0 for r in steady)


def test_phase2_minimum_load_held_on_the_island(history):
    # generator detected running promptly after it picks up the load
    assert any(r["generator_running"] == 1.0 for r in window(history, 60.0, 70.0))
    # after PV returns and the loop settles: floor held, PV still producing
    steady = window(history, 100.0, 180.0)
    assert all(r["gen_w"] > GEN_FLOOR_W - 1500.0 for r in steady)
    assert steady[-1]["gen_w"] == pytest.approx(GEN_FLOOR_W, abs=1500.0)
    assert steady[-1]["pv_w"] == pytest.approx(30000.0, abs=2000.0)  # 60k load − 30k floor
    # the generator was never reverse-powered at any point of the island phases
    assert all(r["gen_w"] > -500.0 for r in window(history, 60.0, 300.0))


def test_phase3_load_drop_curtails_pv_deeper(history):
    steady = window(history, 220.0, 260.0)
    assert all(r["gen_w"] > GEN_FLOOR_W - 1500.0 for r in steady)
    assert steady[-1]["pv_w"] == pytest.approx(5000.0, abs=2000.0)  # 35k load − 30k floor


def test_phase4_load_below_floor_pv_fully_curtailed(history):
    steady = window(history, 280.0, 300.0)
    # the cap floors at 0: PV out, the generator simply carries the whole load
    assert steady[-1]["pv_wset_w"] == pytest.approx(0.0, abs=1.0)
    assert steady[-1]["pv_w"] < 1000.0
    assert steady[-1]["gen_w"] == pytest.approx(25000.0, abs=1500.0)


def test_phase5_grid_return_releases_the_cap(history):
    # off-delay elapsed → running flag drops, claim withdrawn
    steady = window(history, 340.0, END_S)
    assert all(r["generator_running"] == 0.0 for r in steady)
    # export-limit program back in charge: PV recovered, export at the limit
    assert steady[-1]["pv_w"] > 50000.0
    assert all(r["grid_w"] > -(EXPORT_LIMIT_W + 1500.0) for r in steady)


def test_safety_never_tripped(history):
    assert all(r["safe_mode"] == 0.0 for r in history)
