"""Live state publisher: one JSON snapshot per scan cycle.

The configuration UI's realtime view should show the state of the *running*
control loop — the same values the controllers acted on this cycle — not a
second, independent Modbus poll competing for the bus. So after every cycle the
production EMS publishes a small JSON document that any read-only consumer
(`pyems-ui`) can poll off the filesystem. This is a one-way telemetry channel:
the consumer never touches the scheduler, the bus, or the setpoints.

Unlike the CSV flight recorder (history, append-only), this is a single
*current-state* file rewritten in place every cycle, so the write must be
atomic — a reader must never see a half-written document. We write a sibling
temp file and os.replace() it onto the target (atomic rename on the same
directory, on POSIX and Windows alike).

Configured from site.yaml (the EMS ignores a missing section):

    telemetry:
      live_json: logs/live_state.json   # relative paths resolve against repo root
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path

from pyems.channels import Channel, SystemState


def _finite(value: float) -> float | None:
    """JSON has no Infinity/NaN; map them to null so the document parses in a
    browser (sys.comms_age_s is +inf until the first good read)."""
    return value if math.isfinite(value) else None


class LiveSnapshotPublisher:
    """Atomically writes the current SystemState to a JSON file each cycle."""

    def __init__(
        self,
        path: str | Path,
        channels: list[Channel] | None = None,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Static per-channel metadata (unit/writable) so a consumer can render
        # the snapshot without rebuilding the device drivers. Captured once;
        # only the values change cycle to cycle.
        self._channels_meta = (
            [
                {"name": ch.name, "unit": ch.unit, "writable": ch.writable}
                for ch in channels
            ]
            if channels is not None
            else None
        )

    @property
    def path(self) -> Path:
        return self._path

    def publish(
        self,
        now: float,
        state: SystemState,
        metadata: dict | None = None,
    ) -> None:
        doc: dict = {
            "ok": True,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "monotonic_s": round(now, 3),
            "values": {
                name: _finite(value) for name, value in state.snapshot().items()
            },
        }
        if self._channels_meta is not None:
            doc["channels"] = self._channels_meta
        if metadata:
            # cycle_s / cycle_overrun / task info — never the reserved keys above.
            doc.update(metadata)
        self._write_atomic(json.dumps(doc, ensure_ascii=False))

    def _write_atomic(self, text: str) -> None:
        # Temp file in the SAME directory so os.replace() is an atomic rename,
        # not a cross-filesystem copy. Clean up the temp on any write failure so
        # a full disk does not leave a litter of *.tmp files.
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=self._path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
