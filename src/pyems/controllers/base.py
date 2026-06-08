"""
Controller = IEC 61131-3 §2.5.2 FUNCTION_BLOCK.

IEC structure:
  FUNCTION_BLOCK <Name>
    VAR_INPUT  ... END_VAR   -- reads from SystemState
    VAR_OUTPUT ... END_VAR   -- writes setpoints to SystemState
    VAR        ... END_VAR   -- internal state (RETAIN = lives between cycles)
    <body>
  END_FUNCTION_BLOCK

Each controller is called once per scheduler cycle. Internal state persists on self.
Controllers never talk to hardware directly — only through SystemState.
"""
from abc import ABC, abstractmethod

from pyems.channels import SystemState


class Controller(ABC):
    @abstractmethod
    def execute(self, state: SystemState) -> None:
        """Called each cycle. Read inputs from state, write setpoints to state."""
