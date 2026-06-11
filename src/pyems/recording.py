"""Cycle flight recorder: one CSV row per scan cycle.

Post-mortem analysis of control behaviour ("why was export over the limit for
6 s after the load step?") needs synchronized per-cycle values of the meter,
the unit and the EMS outputs — log files with transition-only messages cannot
answer rate questions. The recorder appends one row per cycle with the bound
measurement/setpoint/status tags, cheap enough to stay on in production.

Configured from site.yaml (the EMS ignores a missing section):

    recording:
      cycle_csv: logs/cycles.csv      # relative paths resolve against repo root
      channels: [grid.W, pv.W, pv.WSet]   # optional; default = all bound tags
"""
from __future__ import annotations

import csv
import logging
import time
from pathlib import Path

from pyems.channels import SystemState

logger = logging.getLogger(__name__)


class CycleRecorder:
    """Appends one CSV row per control cycle; flushes at most every few seconds
    (SD-card friendly on the Raspberry Pi target)."""

    def __init__(
        self,
        path: str | Path,
        channels: list[str],
        flush_every_s: float = 5.0,
    ) -> None:
        if not channels:
            raise ValueError("recording needs at least one channel")
        self._channels = list(channels)
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self._path.exists() or self._path.stat().st_size == 0
        self._fh = self._path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        if write_header:
            self._writer.writerow(["timestamp", "monotonic_s", *self._channels])
            self._fh.flush()
        self._flush_every_s = flush_every_s
        self._last_flush = time.monotonic()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def channels(self) -> list[str]:
        return list(self._channels)

    def record(self, now: float, state: SystemState) -> None:
        snapshot = state.snapshot()
        self._writer.writerow(
            [
                time.strftime("%Y-%m-%d %H:%M:%S"),
                f"{now:.3f}",
                *(f"{snapshot[ch]:.1f}" for ch in self._channels),
            ]
        )
        if time.monotonic() - self._last_flush >= self._flush_every_s:
            self._fh.flush()
            self._last_flush = time.monotonic()

    def close(self) -> None:
        try:
            self._fh.flush()
        finally:
            self._fh.close()
