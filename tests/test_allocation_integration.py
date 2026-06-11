"""End-to-end arbitration through Scheduler.step (§6.3).

Drives the full scan cycle (read → tasks → allocator.resolve → driver write) with
FakeDriver and an in-memory board, exercising safety + export limit + a stub
economic controller contending for the same setpoint channel.
"""
from pyems.allocation.allocator import PowerAllocator, SetpointChannelConfig
from pyems.allocation.request import ActivePowerRequest, RequestBoard
from pyems.channels import Channel, SystemState
from pyems.controllers.base import Controller
from pyems.controllers.grid_export_limit import GridExportLimitController
from pyems.controllers.safety import SafetyController
from pyems.system_tags import COMMS_AGE_CHANNEL, SAFE_MODE_CHANNEL
from pyems.scheduler import Scheduler, Task


class EconomicStub(Controller):
    """Lowest-priority economic layer: posts a fixed target on one channel."""

    def __init__(self, name, channel, target_w, priority=50):
        self._name, self._ch, self._target, self._prio = name, channel, target_w, priority

    def execute(self, state, board):
        board.post(
            self._ch,
            ActivePowerRequest(requester=self._name, priority=self._prio, target_w=self._target),
        )


def build(channels, configs, tasks, driver):
    state = SystemState(channels)
    board = RequestBoard([c.setpoint_channel for c in configs])
    allocator = PowerAllocator(configs, board, cycle_s=1.0)
    return Scheduler(tasks, state, driver, allocator=allocator, board=board), state


def pv_channels():
    return [
        Channel("grid.W", unit="W"),
        Channel("pv.W", unit="W"),
        Channel("pv.WSet", unit="W", min_val=0, max_val=100000, writable=True),
        Channel(SAFE_MODE_CHANNEL, min_val=0, max_val=1, writable=True),
        Channel(COMMS_AGE_CHANNEL, unit="s", value=0.0),
    ]


def test_safety_trip_forces_then_release_ramps(fake_driver_cls):
    channels = pv_channels()
    driver = fake_driver_cls(channels)
    configs = [SetpointChannelConfig("pv.WSet", 0.0, 100000.0, 100000.0,
                                     ramp_rate_w_per_s=5000.0, deadband_w=200.0)]
    tasks = [
        Task("safety", 1.0, priority=0, controllers=[
            SafetyController(2.0, 50000.0, ["pv.WSet"])]),
        Task("fast", 1.0, priority=1, controllers=[
            GridExportLimitController(
                name="export_limit", priority=5, export_limit_w=50000.0,
                connection_point_active_power_channel="grid.W",
                unit_active_power_channel="pv.W",
                unit_active_power_setpoint_channel="pv.WSet"),
            EconomicStub("economic", "pv.WSet", target_w=80000.0, priority=50),
        ]),
    ]
    sched, state = build(channels, configs, tasks, driver)

    # Healthy, not over-exporting: export cap is high, economic wants 80 kW.
    # First cycle lands directly at the economic target (no ramp reference yet).
    driver.measurements = {"grid.W": 5000.0, "pv.W": 30000.0, COMMS_AGE_CHANNEL: 0.1}
    sched.step(now=0.0)
    assert driver.written["pv.WSet"] == 80000.0

    # Bus goes stale → safety trips: priority-0 forced value lands in ONE cycle,
    # bypassing the 5 kW/cycle ramp.
    driver.measurements = {"grid.W": 5000.0, "pv.W": 30000.0, COMMS_AGE_CHANNEL: 9.0}
    sched.step(now=1.0)
    assert driver.written["pv.WSet"] == 50000.0
    assert state.get(SAFE_MODE_CHANNEL) == 1.0

    # Bus recovers → safety withdraws; economic target (80 kW) resumes but the
    # allocator ramp-limits the climb to 5 kW per cycle.
    driver.measurements = {"grid.W": 5000.0, "pv.W": 30000.0, COMMS_AGE_CHANNEL: 0.1}
    sched.step(now=2.0)
    assert driver.written["pv.WSet"] == 55000.0   # 50k + one 5k step
    assert state.get(SAFE_MODE_CHANNEL) == 0.0
    sched.step(now=3.0)
    assert driver.written["pv.WSet"] == 60000.0   # keeps ramping toward 80k


def test_export_cap_clamps_economic_target(fake_driver_cls):
    """A pure-constraint cap composes with a lower-priority target: the plan's
    target is clamped under the compliance cap with no special-casing."""
    channels = pv_channels()
    driver = fake_driver_cls(channels)
    configs = [SetpointChannelConfig("pv.WSet", 0.0, 100000.0, 100000.0,
                                     ramp_rate_w_per_s=1e9, deadband_w=0.0)]
    tasks = [
        Task("fast", 1.0, priority=1, controllers=[
            GridExportLimitController(
                name="export_limit", priority=5, export_limit_w=50000.0,
                connection_point_active_power_channel="grid.W",
                unit_active_power_channel="pv.W",
                unit_active_power_setpoint_channel="pv.WSet"),
            EconomicStub("economic", "pv.WSet", target_w=90000.0, priority=50),
        ]),
    ]
    sched, _ = build(channels, configs, tasks, driver)
    # Exporting 70 kW at 70 kW production → cap = 70k - 70k + 50k = 50k.
    driver.measurements = {"grid.W": -70000.0, "pv.W": 70000.0, COMMS_AGE_CHANNEL: 0.1}
    sched.step(now=0.0)
    assert driver.written["pv.WSet"] == 50000.0   # 90k target clamped under 50k cap


def test_two_units_resolved_independently(fake_driver_cls):
    """ESS envelope [-50k, +50k] accepts a negative (charge) target; pv resolves
    independently in the same cycle."""
    channels = [
        Channel("pv.WSet", unit="W", min_val=0, max_val=100000, writable=True),
        Channel("ess.WSet", unit="W", min_val=-50000, max_val=50000, writable=True),
    ]
    driver = fake_driver_cls(channels)
    configs = [
        SetpointChannelConfig("pv.WSet", 0.0, 100000.0, 100000.0,
                              ramp_rate_w_per_s=1e9, deadband_w=0.0),
        SetpointChannelConfig("ess.WSet", -50000.0, 50000.0, 0.0,
                              ramp_rate_w_per_s=1e9, deadband_w=0.0),
    ]
    tasks = [
        Task("fast", 1.0, priority=1, controllers=[
            EconomicStub("pv_econ", "pv.WSet", target_w=60000.0),
            EconomicStub("ess_econ", "ess.WSet", target_w=-30000.0),  # charge
        ]),
    ]
    sched, _ = build(channels, configs, tasks, driver)
    sched.step(now=0.0)
    assert driver.written["pv.WSet"] == 60000.0
    assert driver.written["ess.WSet"] == -30000.0   # negative = charge, accepted


def test_direct_state_set_is_overwritten_by_allocator(fake_driver_cls):
    """Single-ownership guard: a controller writing the setpoint channel directly
    is overwritten by the allocator in the same cycle."""
    channels = pv_channels()
    driver = fake_driver_cls(channels)
    configs = [SetpointChannelConfig("pv.WSet", 0.0, 100000.0, 100000.0,
                                     ramp_rate_w_per_s=1e9, deadband_w=0.0)]

    class RogueController(Controller):
        def execute(self, state, board):
            state.set("pv.WSet", 12345.0)  # illegal direct write
            board.post("pv.WSet", ActivePowerRequest("rogue", 50, target_w=70000.0))

    tasks = [Task("fast", 1.0, priority=1, controllers=[RogueController()])]
    sched, _ = build(channels, configs, tasks, driver)
    sched.step(now=0.0)
    assert driver.written["pv.WSet"] == 70000.0   # allocator wins, not 12345
