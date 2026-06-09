"""
CompositeDriver = several physical devices presented as one I/O resource.

IEC 61131-3 §2.4.1.1: a RESOURCE may bind I/O from multiple field devices.
The Scheduler still sees a single Driver; this class fans the scan-cycle
read/write across every underlying device driver and merges their channels
into one shared SystemState tag pool.

Channel names are globally unique across devices (grid.*, pv.*, battery.*),
so the merged pool has no collisions — controllers address any device by tag.
"""
from __future__ import annotations

import logging

from pyems.channels import Channel, SystemState
from pyems.drivers.base import Driver

logger = logging.getLogger(__name__)


class CompositeDriver(Driver):
    def __init__(self, drivers: list[Driver]) -> None:
        self._drivers = drivers
        logger.debug("CompositeDriver wrapping %d device drivers", len(drivers))

    def connect(self) -> None:
        seen: set[int] = set()
        for d in self._drivers:
            ident = getattr(d, "connection_identity", lambda: d)()
            ident_id = id(ident)
            if ident_id in seen:
                continue
            seen.add(ident_id)
            d.connect()

    def disconnect(self) -> None:
        seen: set[int] = set()
        for d in self._drivers:
            ident = getattr(d, "connection_identity", lambda: d)()
            ident_id = id(ident)
            if ident_id in seen:
                continue
            seen.add(ident_id)
            d.disconnect()

    def channels(self) -> list[Channel]:
        merged: list[Channel] = []
        seen: set[str] = set()
        for d in self._drivers:
            for ch in d.channels():
                if ch.name in seen:
                    raise ValueError(f"Duplicate channel '{ch.name}' across device profiles")
                seen.add(ch.name)
                merged.append(ch)
        return merged

    def read_state(self, state: SystemState) -> None:
        for d in self._drivers:
            d.read_state(state)

    def write_setpoints(self, state: SystemState) -> None:
        for d in self._drivers:
            d.write_setpoints(state)
