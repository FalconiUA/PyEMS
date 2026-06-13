"""Operator command channel: a single JSON file the UI writes and the EMS reads.

Symmetric to `pyems.telemetry` (which is EMS → UI, one snapshot per cycle):
this is UI → EMS, one standing command document. The only command today is
whether inverter generation is permitted; the EMS reads it each cycle and a
PRIORITY-1 `GenerationGateController` pins the unit to a safe floor while it is
disabled.

This is an OPERATIONAL interlock, not a safety-rated E-stop. The safety-rated
layers remain the device comms watchdog, the inverter's own protections and the
priority-0 `SafetyController`. Accordingly the channel is **fail-closed on
startup/input uncertainty**, while a freshly accepted operator enable is latched
for the current EMS run until a stop command or process restart:

  - missing / malformed / unreadable file              → generation disabled;
  - a start command older than `max_age_s` before EMS first sees it
                                                        → generation disabled;
  - `generation_enabled: true` issued at or before the current EMS run started
    (a leftover file from a previous run)               → generation disabled.

A `generation_enabled: false` command is always honored (disabling never needs
freshness). Times are wall-clock `time.time()` because the writer (UI) and the
reader (EMS) are different processes that do not share a monotonic clock.

Document format (written atomically by `write_command_file`):

    {"generation_enabled": true, "issued_at": 1734105600.12}
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from pyems.channels import SystemState
from pyems.system_tags import (
    COMMAND_AGE_CHANNEL,
    GENERATION_ALLOWED_CHANNEL,
    INVERTER_COMMAND_CHANNEL,
    INVERTER_COMMAND_ID_CHANNEL,
)

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_MAX_AGE_S = 30.0


def _write_atomic(path: Path, text: str) -> None:
    """Temp file in the same directory + os.replace, so the EMS never reads a
    half-written file — same contract as LiveSnapshotPublisher._write_atomic."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_command_file(path: str | Path, **fields: Any) -> dict[str, Any]:
    """Merge `fields` into the command document and rewrite it atomically.

    Read-modify-write so the soft gate (generation_*) and the hard switch
    (inverter_*) share ONE file without clobbering each other's keys.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = read_command_file(path) or {}
    doc.update(fields)
    _write_atomic(path, json.dumps(doc, ensure_ascii=False))
    return doc


def write_command_file(path: str | Path, *, generation_enabled: bool) -> dict[str, Any]:
    """Set the soft generation gate (preserves any inverter_* keys)."""
    doc = update_command_file(
        path, generation_enabled=bool(generation_enabled), issued_at=time.time()
    )
    logger.info(
        "Operator command written: SOFT %s generation -> %s",
        "START" if generation_enabled else "STOP",
        Path(path),
    )
    return doc


def write_inverter_command(path: str | Path, *, action: str) -> dict[str, Any]:
    """Issue a latched hard start/stop action (preserves the generation keys).

    Stamps a fresh `inverter_command_id` (wall clock) so the EMS fires the action
    exactly once on this new id; an id from a previous run is ignored on restart.
    """
    if action not in ("start", "stop"):
        raise ValueError(f"inverter action must be 'start' or 'stop', got {action!r}")
    doc = update_command_file(
        path, inverter_command=action, inverter_command_id=time.time()
    )
    logger.info(
        "Operator command written: inverter HARD %s -> %s",
        action.upper(),
        Path(path),
    )
    return doc


def read_command_file(path: str | Path) -> dict[str, Any] | None:
    """Read the raw command document, or None if absent/unreadable (UI status)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class CommandFileReader:
    """EMS-side input stage: maps the command file onto system tags each cycle.

    Writes `sys.generation_allowed` (fail-closed) and `sys.command_age_s` into
    SystemState. Holds no setpoint authority — the gate controller reads the tag
    and posts the actual board claim.
    """

    def __init__(
        self,
        path: str | Path,
        run_start_wall: float,
        max_age_s: float = DEFAULT_COMMAND_MAX_AGE_S,
    ) -> None:
        self._path = Path(path)
        # Commands issued at or before this wall-clock instant belong to a
        # previous EMS run and must not re-enable generation after a restart.
        self._run_start_wall = float(run_start_wall)
        self._max_age_s = float(max_age_s)
        self._read_failed = False  # log a corrupt file once, not every cycle
        self._last_generation_command_id: float | None = None
        self._last_inverter_command_id: float | None = None
        self._generation_allowed = False

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, Any] | None:
        """Parsed document, or None for absent/unreadable/malformed.

        A present-but-unreadable file is logged once on the transition (and once
        on recovery); an absent file is the normal pre-command state, never logged.
        """
        if not self._path.exists():
            if self._read_failed:
                self._read_failed = False
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("command file is not a JSON object")
        except (OSError, ValueError):
            if not self._read_failed:
                logger.warning(
                    "Command file %s unreadable/malformed; generation stays disabled",
                    self._path,
                )
                self._read_failed = True
            return None
        if self._read_failed:
            logger.info("Command file %s readable again", self._path)
            self._read_failed = False
        return data

    def apply(self, state: SystemState, now_wall: float) -> None:
        doc = self._load() or {}

        # ── soft generation gate ──────────────────────────────────────────────
        age_s = float("inf")
        issued_at = doc.get("issued_at")
        if isinstance(issued_at, (int, float)) and math.isfinite(issued_at):
            age_s = max(0.0, now_wall - float(issued_at))
            fresh = age_s <= self._max_age_s
            from_this_run = issued_at > self._run_start_wall
            if not from_this_run:
                self._generation_allowed = False
            elif issued_at != self._last_generation_command_id:
                requested_enabled = bool(doc.get("generation_enabled"))
                logger.info(
                    "Operator command observed: SOFT %s generation (age %.1fs%s)",
                    "START" if requested_enabled else "STOP",
                    age_s,
                    "" if fresh or not requested_enabled else ", stale start ignored",
                )
                if not requested_enabled or fresh:
                    self._generation_allowed = requested_enabled
                self._last_generation_command_id = float(issued_at)
        else:
            self._generation_allowed = False
        state.apply_driver_value(
            GENERATION_ALLOWED_CHANNEL, 1.0 if self._generation_allowed else 0.0
        )
        state.apply_driver_value(COMMAND_AGE_CHANNEL, age_s)

        # ── hard inverter switch (latched action) ─────────────────────────────
        # Publish the action + its id only when it belongs to THIS run, so a
        # leftover hard command never re-fires after a restart. The controller
        # acts on a NEW id; staleness does not apply to a one-shot action.
        if INVERTER_COMMAND_ID_CHANNEL in state:
            cmd = doc.get("inverter_command")
            cmd_id = doc.get("inverter_command_id")
            fire_id = float("nan")
            fire_cmd = float("nan")
            if (
                cmd in ("start", "stop")
                and isinstance(cmd_id, (int, float))
                and math.isfinite(cmd_id)
                and cmd_id > self._run_start_wall
            ):
                fire_id = float(cmd_id)
                fire_cmd = 1.0 if cmd == "start" else 0.0
                if fire_id != self._last_inverter_command_id:
                    logger.info(
                        "Operator command accepted: inverter HARD %s",
                        cmd.upper(),
                    )
                    self._last_inverter_command_id = fire_id
            state.apply_driver_value(INVERTER_COMMAND_ID_CHANNEL, fire_id)
            if INVERTER_COMMAND_CHANNEL in state:
                state.apply_driver_value(INVERTER_COMMAND_CHANNEL, fire_cmd)
