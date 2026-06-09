"""
Controller = IEC 61131-3 §2.5.2 FUNCTION_BLOCK.

IEC structure:
  FUNCTION_BLOCK <Name>
    VAR_INPUT  ... END_VAR   -- reads from SystemState
    VAR_OUTPUT ... END_VAR   -- measurement/status -> SystemState; unit setpoints -> RequestBoard
    VAR        ... END_VAR   -- internal state (RETAIN = lives between cycles)
    <body>
  END_FUNCTION_BLOCK

Each controller is called once per scheduler cycle. Internal state persists on self.
Controllers never talk to hardware directly — only through SystemState.

Outputs are split by kind:
  - VAR_OUTPUT for measurement/status tags (e.g. `sys.safe_mode`) goes through
    `state.set` as before.
  - VAR_OUTPUT for **unit setpoints** goes through `board.post` — a controller
    must never call `state.set` on an allocator-configured setpoint channel; the
    PowerAllocator is the sole writer of those channels.
The board is per-cycle context like `state`. It carries the cycle's `now` (set
by the scheduler via `board.tick`), so `board.post(channel, request)` needs no
explicit timestamp.
"""
from abc import ABC, abstractmethod

from pyems.allocation.request import RequestBoard
from pyems.channels import SystemState


class Controller(ABC):
    @abstractmethod
    def execute(self, state: SystemState, board: RequestBoard) -> None:
        """Called each cycle. Read inputs from state; write status tags to state
        and unit-setpoint requests to board."""
