"""
Task + Scheduler = IEC 61131-3 §2.7.2 TASK inside a RESOURCE.

IEC syntax:
  TASK fast_cycle (INTERVAL := T#1s,  PRIORITY := 1);
  TASK slow_cycle (INTERVAL := T#15m, PRIORITY := 5);
  PROGRAM safety WITH fast_cycle : SafetyController;
  PROGRAM balance WITH fast_cycle : PowerBalance;
  PROGRAM optimize WITH slow_cycle : Optimizer;

Rules from §2.7.2:
  - PRIORITY 0 = highest; higher number = lower priority
  - INTERVAL != 0 → periodic scheduling
  - Higher-priority tasks preempt lower-priority ones
"""
import logging
import time
from dataclasses import dataclass, field

from pyems.channels import SystemState
from pyems.controllers.base import Controller

logger = logging.getLogger(__name__)


@dataclass
class Task:
    name: str
    interval_s: float           # IEC: INTERVAL := T#1s
    priority: int               # IEC: PRIORITY := 1  (0 = highest)
    controllers: list[Controller] = field(default_factory=list)
    _next_run: float = field(default=0.0, init=False, repr=False)

    def is_due(self, now: float) -> bool:
        return now >= self._next_run

    def mark_ran(self, now: float) -> None:
        self._next_run = now + self.interval_s


class Scheduler:
    """
    IEC RESOURCE: owns tasks, runs the control loop.
    Tasks execute in priority order (lowest number first) each cycle tick.
    """

    def __init__(self, tasks: list[Task], state: SystemState, driver) -> None:
        self._tasks = sorted(tasks, key=lambda t: t.priority)
        self._state = state
        self._driver = driver
        self._overrunning = False  # last cycle-overrun state — log on transition

    def step(self, now: float) -> None:
        """One IEC scan cycle: read inputs → run due tasks (priority order) →
        write outputs. Extracted from run() so a single cycle is testable."""
        # read all inputs first
        self._driver.read_state(self._state)

        # execute due tasks in priority order
        for task in self._tasks:
            if task.is_due(now):
                for ctrl in task.controllers:
                    ctrl.execute(self._state)
                task.mark_ran(now)

        # write all outputs last
        self._driver.write_setpoints(self._state)

    def run(self) -> None:
        tick = min(t.interval_s for t in self._tasks)
        logger.info(
            "Scheduler start: %d tasks, tick %.3fs, priorities %s",
            len(self._tasks), tick, [t.priority for t in self._tasks],
        )
        try:
            while True:
                now = time.monotonic()
                self.step(now)

                elapsed = time.monotonic() - now
                if elapsed > tick:
                    if not self._overrunning:  # log only on transition into overrun
                        logger.warning("Cycle overrun: %.3fs > %.3fs tick", elapsed, tick)
                        self._overrunning = True
                elif self._overrunning:
                    self._overrunning = False
                time.sleep(max(0.0, tick - elapsed))
        except KeyboardInterrupt:
            logger.info("Scheduler stopping (interrupt)")
            self._driver.disconnect()
