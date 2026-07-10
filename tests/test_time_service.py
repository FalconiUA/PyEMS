"""Root-owned time helper orchestration tests (no real clock or systemd calls).

``time_service`` is the only code that changes the OS clock.  These tests
exercise its decision logic (timezone/NTP policy, scheduled-minute gating,
status recording) with ``timedatectl`` and the NTP probe faked out, so nothing
here touches the host's wall clock.
"""

from __future__ import annotations

import datetime as dt

import pytest

from pyems import time_service
from pyems.time_sync import load_time_sync_status, write_time_settings


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_timedatectl(monkeypatch):
    """Record every ``timedatectl`` invocation and report success by default."""
    calls: list[list[str]] = []

    def _run(args, capture_output=True, text=True):
        calls.append(list(args))
        return FakeCompleted()

    monkeypatch.setattr(time_service.subprocess, "run", _run)
    return calls


def _write(tmp_path, settings):
    path = tmp_path / "time-settings.json"
    write_time_settings(settings, path)
    return path


def test_apply_settings_for_ntp_disables_auto_ntp_and_sets_timezone(tmp_path, fake_timedatectl):
    state = _write(
        tmp_path,
        {"mode": "ntp", "server": "time.google.com", "sync_at": "03:15", "timezone": "Europe/Kyiv"},
    )

    message = time_service.apply_settings(state)

    assert "scheduled NTP synchronization configured for 03:15" in message
    assert "time.google.com" in message
    assert ["timedatectl", "set-timezone", "Europe/Kyiv"] in fake_timedatectl
    assert ["timedatectl", "set-ntp", "false"] in fake_timedatectl


def test_apply_settings_for_manual_mode_reports_disabled_ntp(tmp_path, fake_timedatectl):
    state = _write(tmp_path, {"mode": "manual"})

    message = time_service.apply_settings(state)

    assert "automatic NTP disabled" in message
    # No timezone was configured, so only the set-ntp call should run.
    assert ["timedatectl", "set-ntp", "false"] in fake_timedatectl
    assert all(call[1] != "set-timezone" for call in fake_timedatectl)


def test_set_manual_time_writes_the_stored_value(tmp_path, fake_timedatectl):
    state = _write(tmp_path, {"mode": "manual", "manual_time": "2026-06-20T10:26"})

    message = time_service.set_manual_time(state)

    assert "manual controller time set to" in message
    assert any(call[:2] == ["timedatectl", "set-time"] for call in fake_timedatectl)


def test_set_manual_time_requires_a_stored_value(tmp_path, fake_timedatectl):
    state = _write(tmp_path, {"mode": "manual"})

    with pytest.raises(ValueError, match="manual controller time was not supplied"):
        time_service.set_manual_time(state)


def test_synchronize_is_a_no_op_for_manual_mode(tmp_path, fake_timedatectl):
    state = _write(tmp_path, {"mode": "manual"})

    assert time_service.synchronize(state, only_if_due=True) == ""
    assert "disabled (manual time mode)" in time_service.synchronize(state, only_if_due=False)


def test_synchronize_skips_when_the_scheduled_minute_has_not_arrived(tmp_path, fake_timedatectl, monkeypatch):
    not_now = (dt.datetime.now().astimezone() + dt.timedelta(minutes=1)).strftime("%H:%M")
    state = _write(tmp_path, {"mode": "ntp", "server": "time.google.com", "sync_at": not_now})

    def _unexpected(_server):
        raise AssertionError("must not probe NTP before the scheduled minute")

    monkeypatch.setattr(time_service, "ntp_probe", _unexpected)

    assert time_service.synchronize(state, only_if_due=True) == ""


def test_synchronize_sets_the_clock_from_the_ntp_sample(tmp_path, fake_timedatectl, monkeypatch):
    state = _write(tmp_path, {"mode": "ntp", "server": "time.google.com", "sync_at": "03:15"})
    monkeypatch.setattr(
        time_service,
        "ntp_probe",
        lambda server: {"server": server, "offset_ms": 12.0, "unix_time_s": 1_800_000_000.0},
    )

    message = time_service.synchronize(state, only_if_due=False)

    assert "controller synchronized with time.google.com" in message
    assert "+12.0 ms" in message
    assert any(call[:2] == ["timedatectl", "set-time"] for call in fake_timedatectl)


def test_run_checked_surfaces_a_failing_command(tmp_path, monkeypatch):
    state = _write(tmp_path, {"mode": "manual"})
    monkeypatch.setattr(
        time_service.subprocess,
        "run",
        lambda *a, **k: FakeCompleted(returncode=1, stderr="permission denied"),
    )

    with pytest.raises(ValueError, match="permission denied"):
        time_service.apply_settings(state)


def test_run_checked_reports_when_the_binary_is_missing(tmp_path, monkeypatch):
    state = _write(tmp_path, {"mode": "manual"})

    def _boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(time_service.subprocess, "run", _boom)

    with pytest.raises(ValueError, match="cannot run"):
        time_service.apply_settings(state)


def test_main_records_success_status_for_a_manual_sync(tmp_path, fake_timedatectl, monkeypatch):
    state = _write(tmp_path, {"mode": "ntp", "server": "time.google.com", "sync_at": "03:15"})
    status = tmp_path / "status.json"
    monkeypatch.setattr(
        time_service,
        "ntp_probe",
        lambda server: {"server": server, "offset_ms": -3.0, "unix_time_s": 1_800_000_000.0},
    )

    time_service.main(["--sync-now", "--state", str(state), "--status", str(status)])

    recorded = load_time_sync_status(status)
    assert recorded["ok"] is True
    assert recorded["operation"] == "manual_ntp"


def test_main_records_failure_status_and_reraises(tmp_path, fake_timedatectl):
    state = _write(tmp_path, {"mode": "manual"})  # no manual_time → set-manual fails
    status = tmp_path / "status.json"

    with pytest.raises(ValueError):
        time_service.main(["--set-manual-time", "--state", str(state), "--status", str(status)])

    recorded = load_time_sync_status(status)
    assert recorded["ok"] is False
    assert recorded["operation"] == "manual_time"
    assert "manual controller time was not supplied" in recorded["message"]


def test_main_apply_does_not_write_a_sync_status(tmp_path, fake_timedatectl):
    state = _write(tmp_path, {"mode": "manual"})
    status = tmp_path / "status.json"

    time_service.main(["--apply", "--state", str(state), "--status", str(status)])

    # --apply configures policy only; it is not an operator-visible sync event.
    assert load_time_sync_status(status) is None


def test_main_requires_exactly_one_action(tmp_path):
    with pytest.raises(SystemExit):
        time_service.main(["--state", str(tmp_path / "x.json")])
