"""Append-only JSONL event / alarm journal for PyEMS.

One JSON object per line records an alarm TRANSITION (raise / clear) or a
discrete event (e.g. a UI configuration edit). Two readers consume it:

  * the live alarm banner — :meth:`EventJournal.active_alarms` rides every
    telemetry snapshot (see ``Scheduler.step``), so the HMI shows what is
    *currently* latched, not a replay of history;
  * the post-hoc event history — ``ui.event_log`` tails the file(s) newest-first.

The EMS runtime owns one journal (the ``events.journal_jsonl`` file) and is its
only writer; the UI process appends operator / configuration events to a
SEPARATE file (``events.ui_audit_jsonl``) with :func:`make_event_dict` +
:func:`append_event_line`, so the two OS-supervised processes never share a file
handle. Writes are append-only and line-atomic; a reader discards a torn final
line (a crash mid-append).

Severities are plain lowercase strings so they serialise verbatim into the JSONL
record and the telemetry snapshot — the HMI never has to decode an enum.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

# Severity vocabulary. Lowercase strings on purpose: they ride the JSONL line and
# the telemetry snapshot as-is, with no enum for the HMI to translate.
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ALARM = "alarm"


def _wall_clock() -> str:
    """Local wall-clock stamp, identical format to telemetry / recording rows.

    The control loop hands controllers a ``time.monotonic()`` value (immune to an
    NTP step) for alarm bookkeeping, so a journal line takes its human-facing
    timestamp here, at write time, instead of trying to render the monotonic one.
    """
    return time.strftime("%Y-%m-%d %H:%M:%S")


def make_event_dict(
    source: str,
    severity: str,
    kind: str,
    message: str,
    *,
    key: str | None = None,
    details: list[str] | None = None,
) -> dict[str, Any]:
    """Build one journal record.

    ``source`` is the emitter (``"safety"``, ``"allocation"``, ``"ui"``);
    ``kind`` is the record's nature in the canonical vocabulary (``"raised"`` /
    ``"cleared"`` for alarm transitions, ``"config"`` for a UI edit); ``key``
    identifies a latchable alarm (``None`` for a one-shot event); ``details`` is
    an optional list of human-readable lines (e.g. the config diff) and is
    omitted from the record when not given.
    """
    event: dict[str, Any] = {
        "timestamp": _wall_clock(),
        "severity": severity,
        "source": source,
        "kind": kind,
        "key": key,
        "message": message,
    }
    if details is not None:
        event["details"] = details
    return event


def append_event_line(path: str | Path, event: dict[str, Any]) -> None:
    """Append one event as a single JSON line, creating parent dirs on demand.

    The journal lives under ``logs/``, which may not exist yet on a fresh
    install. A single ``write`` of a newline-terminated line is atomic enough for
    the tail reader, which simply discards a torn final line.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class EventJournal:
    """Append-only alarm journal: latched active alarms + a file of transitions.

    Alarms latch by ``(source, key)``: a second :meth:`raise_alarm` for an
    already-active alarm is a no-op (no duplicate line — one line per
    transition), and :meth:`clear` writes a ``cleared`` line only if the alarm
    was actually active (idempotent release). Controllers already fire on
    transition only; this latching keeps the file and the banner correct even if
    one fires twice.

    Thread-safe: the control loop raises / clears from the task thread while the
    telemetry publish reads :meth:`active_alarms`; a lock keeps a raise and a
    concurrent snapshot from interleaving.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        # (source, key) -> active alarm record. Insertion order == age order,
        # which active_alarms() preserves for the banner.
        self._active: dict[tuple[str, str], dict[str, Any]] = {}

    def raise_alarm(
        self,
        source: str,
        key: str,
        message: str,
        severity: str = SEVERITY_ALARM,
        *,
        now: float,
    ) -> bool:
        """Latch an alarm and append a ``raised`` line.

        Returns ``True`` if this was a new transition (a line was written),
        ``False`` if the alarm was already active — callers fire only on
        transition, but a repeat must never spam the journal. ``now`` is the
        control-loop monotonic time, kept as the alarm's ``since`` marker.
        """
        ident = (source, key)
        with self._lock:
            if ident in self._active:
                return False
            event = make_event_dict(
                source=source, severity=severity, kind="raised",
                message=message, key=key,
            )
            self._active[ident] = {
                "source": source,
                "key": key,
                "severity": severity,
                "message": message,
                "since": now,
                "timestamp": event["timestamp"],
                "acked": False,
            }
            append_event_line(self._path, event)
        return True

    def clear(
        self,
        source: str,
        key: str,
        *,
        now: float,
        message: str = "cleared",
    ) -> bool:
        """Release a latched alarm and append a ``cleared`` line.

        The cleared line carries the alarm's ORIGINAL severity, so the history
        shows the weight of what just cleared. No-op (no line) if the alarm was
        not active, so a release is idempotent. Returns ``True`` if a line was
        written.
        """
        ident = (source, key)
        with self._lock:
            active = self._active.pop(ident, None)
            if active is None:
                return False
            event = make_event_dict(
                source=source, severity=active["severity"], kind="cleared",
                message=message, key=key,
            )
            append_event_line(self._path, event)
        return True

    def active_alarms(self) -> list[dict[str, Any]]:
        """The currently latched alarms, oldest-first, as JSON-safe dicts.

        Rides the telemetry snapshot's ``metadata["alarms"]`` every cycle, so the
        HMI banner reflects live state. A fresh copy per call: the caller
        serialises it and must never see a later in-place mutation.
        """
        with self._lock:
            return [dict(rec) for rec in self._active.values()]

    def close(self) -> None:
        """Lifecycle symmetry with the flight recorder (the Scheduler closes both
        on shutdown). Lines are written open / append / close, so there is no
        long-lived handle to release — kept so the journal is a drop-in
        supervised resource.
        """
        return None
