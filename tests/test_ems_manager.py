"""EmsManager (src/pyems/ui.py): process-control gating and the no-second-EMS
guard. These are pure-logic tests — no real process is spawned."""
import pytest

import pyems.ui as ui
from pyems.ui import DEFAULT_SIM_SITE, EmsManager, telemetry_fresh


def test_start_refused_without_process_control():
    ems = EmsManager(DEFAULT_SIM_SITE, process_control=False)
    with pytest.raises(ValueError, match="process control is disabled"):
        ems.start()


def test_stop_refused_without_process_control():
    ems = EmsManager(DEFAULT_SIM_SITE, process_control=False)
    with pytest.raises(ValueError, match="process control is disabled"):
        ems.stop()


def test_start_refused_when_already_running(monkeypatch):
    ems = EmsManager(DEFAULT_SIM_SITE, process_control=True)
    monkeypatch.setattr(ems, "running", lambda: True)
    with pytest.raises(ValueError, match="already running"):
        ems.start()


def test_status_reports_process_control(monkeypatch):
    monkeypatch.setattr(ui, "fast_loop_state", lambda site: {"ok": False})
    ems = EmsManager(DEFAULT_SIM_SITE, process_control=True)
    monkeypatch.setattr(ems, "_site", lambda: {})  # skip load_site/file I/O
    status = ems.status()
    assert status["process_control"] is True
    assert status["running"] is False
    assert status["managed"] is False


def test_telemetry_fresh_thresholds():
    assert telemetry_fresh({"ok": True, "age_s": 0.5, "cycle_s": 1.0})
    assert not telemetry_fresh({"ok": True, "age_s": 30.0, "cycle_s": 1.0})
    assert not telemetry_fresh({"ok": False})
    # stale-but-parseable snapshot (EMS stopped) is not "running"
    assert not telemetry_fresh({"ok": True, "age_s": 120.0, "cycle_s": 1.0})
