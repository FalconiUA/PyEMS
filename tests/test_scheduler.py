"""Tests for Task scheduling and the scan cycle (src/scheduler.py)."""
import threading

import pytest

from pyems.channels import Channel, SystemState
from pyems.controllers.base import Controller
from pyems.scheduler import Scheduler, Task


class RecordingController(Controller):
    """Appends its label to a shared list each time it runs — to check order."""

    def __init__(self, label: str, order: list[str]) -> None:
        self._label = label
        self._order = order

    def execute(self, state: SystemState, board=None) -> None:
        self._order.append(self._label)


def test_task_is_due_and_mark_ran():
    task = Task("t", interval_s=1.0, priority=1)
    assert task.is_due(now=0.0)        # _next_run starts at 0
    task.mark_ran(now=10.0)
    assert not task.is_due(now=10.5)   # next run at 11.0
    assert task.is_due(now=11.0)


def test_step_runs_tasks_in_priority_order(fake_driver_cls):
    order: list[str] = []
    driver = fake_driver_cls([Channel("x")])
    state = SystemState([Channel("x")])
    # Build out of priority order; scheduler must sort 0 before 5.
    low = Task("low", 1.0, priority=5, controllers=[RecordingController("low", order)])
    high = Task("high", 1.0, priority=0, controllers=[RecordingController("high", order)])
    Scheduler(tasks=[low, high], state=state, driver=driver).step(now=0.0)
    assert order == ["high", "low"]


def test_step_skips_tasks_not_due(fake_driver_cls):
    order: list[str] = []
    driver = fake_driver_cls([Channel("x")])
    state = SystemState([Channel("x")])
    task = Task("slow", 100.0, priority=1, controllers=[RecordingController("slow", order)])
    sched = Scheduler(tasks=[task], state=state, driver=driver)
    sched.step(now=0.0)        # due (first run)
    task.mark_ran(0.0)
    sched.step(now=1.0)        # not due yet (interval 100s)
    assert order == ["slow"]   # ran exactly once


def test_step_reads_then_writes(state, fake_driver):
    """Inputs are read before controllers run; outputs flushed after."""
    fake_driver.measurements = {"grid.W": -60000.0, "pv.W": 60000.0}

    class CopyController(Controller):
        def execute(self, s: SystemState, board=None) -> None:
            # echo a measurement into the writable setpoint
            s.set("pv.WSet", s.get("pv.W"))

    task = Task("t", 1.0, priority=1, controllers=[CopyController()])
    Scheduler([task], state, fake_driver).step(now=0.0)
    assert fake_driver.written["pv.WSet"] == 60000.0


def test_step_runs_allocator_after_tasks_before_write(state, fake_driver):
    """Cycle order: tasks → allocator.resolve → driver write. The allocator's
    resolved value (not a controller's direct write) is what reaches the driver."""
    events: list[str] = []

    class TaskController(Controller):
        def execute(self, s: SystemState, board) -> None:
            events.append("task")

    class RecordingAllocator:
        def resolve(self, s: SystemState, now: float) -> None:
            events.append("resolve")
            s.set("pv.WSet", 42000.0)

    class RecordingBoard:
        def tick(self, now: float) -> None:
            events.append("tick")

    task = Task("t", 1.0, priority=1, controllers=[TaskController()])
    sched = Scheduler(
        [task], state, fake_driver,
        allocator=RecordingAllocator(), board=RecordingBoard(),
    )
    sched.step(now=0.0)
    assert events == ["tick", "task", "resolve"]
    assert fake_driver.written["pv.WSet"] == 42000.0  # allocator's value, flushed last


# ── shutdown paths: stop(), crash, Ctrl-C — driver must always disconnect ────
class RaisingController(Controller):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def execute(self, state: SystemState, board=None) -> None:
        raise self._exc


def test_stop_request_ends_run_and_disconnects(state, fake_driver):
    """stop() (e.g. from a SIGTERM handler) ends run() cleanly."""
    task = Task("t", 0.01, priority=1, controllers=[])
    sched = Scheduler([task], state, fake_driver)
    fake_driver.connect()
    runner = threading.Thread(target=sched.run)
    runner.start()
    sched.stop()
    runner.join(timeout=2.0)
    assert not runner.is_alive()
    assert not fake_driver.connected  # bus released on the way out


def test_unhandled_controller_error_reraises_and_disconnects(state, fake_driver, caplog):
    """A controller bug must be logged, re-raised (non-zero exit for systemd)
    and must still release the bus."""
    task = Task("t", 0.01, priority=1, controllers=[RaisingController(RuntimeError("bug"))])
    sched = Scheduler([task], state, fake_driver)
    fake_driver.connect()
    with pytest.raises(RuntimeError, match="bug"):
        sched.run()
    assert not fake_driver.connected
    assert any("unhandled error" in r.message for r in caplog.records)


def test_keyboard_interrupt_exits_cleanly_and_disconnects(state, fake_driver):
    task = Task("t", 0.01, priority=1, controllers=[RaisingController(KeyboardInterrupt())])
    sched = Scheduler([task], state, fake_driver)
    fake_driver.connect()
    sched.run()  # must not raise
    assert not fake_driver.connected
