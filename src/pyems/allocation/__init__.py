"""
Setpoint arbitration layer.

Controllers (IEC FUNCTION_BLOCKs) no longer write unit setpoint channels
directly. Instead each posts a *request* — a standing claim on one setpoint
channel describing a hard range, an optional preferred value, and a priority —
into the `RequestBoard`. Once per scan cycle, after all tasks and before the
driver output flush, the `PowerAllocator` merges the valid requests
deterministically (interval intersection in strict priority order) and writes
exactly one resolved setpoint per channel. The allocator is the single owner of
those channels and centralizes per-unit ramp limiting and deadband.

Sign convention (generating-unit): positive active power = injection into the
site AC bus. For storage P > 0 = discharge, P < 0 = charge. This must match the
channel semantics in the device profile.
"""
from pyems.allocation.allocator import (
    ChannelArbiter,
    PowerAllocator,
    SetpointChannelConfig,
)
from pyems.allocation.request import ActivePowerRequest, RequestBoard

__all__ = [
    "ActivePowerRequest",
    "RequestBoard",
    "SetpointChannelConfig",
    "ChannelArbiter",
    "PowerAllocator",
]
