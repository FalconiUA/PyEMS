"""Tests for Task scheduling and the scan cycle (src/scheduler.py)."""
from src.channels import Channel, SystemState
from src.controllers.base import Controller
from src.scheduler import Scheduler, Task
from tests.conftest import FakeDriver


class RecordingController(Controller):
    """Appends its label to a shared list each time it runs — to check order."""

    def __init__(self, label: str, order: list[str]) -> None:
        self._label = label
        self._order = order

    def execute(self, state: SystemState) -> None:
        self._order.append(self._label)


def test_task_is_due_and_mark_ran():
    task = Task("t", interval_s=1.0, priority=1)
    assert task.is_due(now=0.0)        # _next_run starts at 0
    task.mark_ran(now=10.0)
    assert not task.is_due(now=10.5)   # next run at 11.0
    assert task.is_due(now=11.0)


def test_step_runs_tasks_in_priority_order():
    order: list[str] = []
    driver = FakeDriver([Channel("x")])
    state = SystemState([Channel("x")])
    # Build out of priority order; scheduler must sort 0 before 5.
    low = Task("low", 1.0, priority=5, controllers=[RecordingController("low", order)])
    high = Task("high", 1.0, priority=0, controllers=[RecordingController("high", order)])
    Scheduler(tasks=[low, high], state=state, driver=driver).step(now=0.0)
    assert order == ["high", "low"]


def test_step_skips_tasks_not_due():
    order: list[str] = []
    driver = FakeDriver([Channel("x")])
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
        def execute(self, s: SystemState) -> None:
            # echo a measurement into the writable setpoint
            s.set("pv.WSet", s.get("pv.W"))

    task = Task("t", 1.0, priority=1, controllers=[CopyController()])
    Scheduler([task], state, fake_driver).step(now=0.0)
    assert fake_driver.written["pv.WSet"] == 60000.0
