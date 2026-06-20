"""Controller clock configuration and NTP probe tests (no real clock changes)."""

from __future__ import annotations

import pytest

from pyems import time_sync
from pyems.time_sync import (
    effective_timezone,
    fixed_timezone_options,
    load_time_settings,
    load_time_sync_status,
    normalize_time_settings,
    ntp_probe,
    validate_manual_time,
    validate_ntp_server,
    validate_sync_time,
    write_time_sync_status,
    write_time_settings,
)
from pyems.ui import TimeController


def test_time_settings_validate_and_round_trip_atomically(tmp_path):
    settings = write_time_settings(
        {"mode": "ntp", "server": "TIME.GOOGLE.COM.", "sync_at": "03:15"},
        tmp_path / "time-settings.json",
    )

    assert settings == {
        "mode": "ntp",
        "server": "time.google.com",
        "sync_at": "03:15",
        "dst_mode": "automatic",
    }
    assert load_time_settings(tmp_path / "time-settings.json") == settings


@pytest.mark.parametrize("value", ["", "host name", "host:123", "../bad", "bad_host"])
def test_ntp_server_rejects_unsafe_or_invalid_values(value):
    with pytest.raises(ValueError):
        validate_ntp_server(value)


@pytest.mark.parametrize("value", ["3:00", "24:00", "12:60", "03:00:10"])
def test_sync_time_requires_exact_local_minute(value):
    with pytest.raises(ValueError):
        validate_sync_time(value)


def test_manual_mode_normalizes_a_datetime_local_value():
    normalized = normalize_time_settings({"mode": "manual", "manual_time": "2026-06-20T10:26"})

    assert normalized == {
        "mode": "manual",
        "manual_time": "2026-06-20 10:26:00",
        "dst_mode": "automatic",
    }
    assert validate_manual_time("2026-06-20T10:26").year == 2026


def test_fixed_utc_offset_disables_seasonal_clock_changes():
    settings = normalize_time_settings(
        {
            "mode": "manual",
            "timezone": "Europe/Kyiv",
            "dst_mode": "fixed",
            "fixed_timezone": "Etc/GMT-2",
        }
    )

    assert effective_timezone(settings) == "Etc/GMT-2"
    assert any(item["id"] == "Etc/GMT-2" for item in fixed_timezone_options())


def test_sync_status_round_trip(tmp_path):
    status_path = tmp_path / "time-sync-status.json"
    write_time_sync_status(
        {
            "ok": False,
            "operation": "scheduled_ntp",
            "recorded_at": "2026-06-20 11:05:00",
            "message": "NTP server did not respond within 3 s",
        },
        status_path,
    )

    assert load_time_sync_status(status_path)["message"] == "NTP server did not respond within 3 s"


def test_time_controller_saves_ntp_settings_then_runs_only_the_fixed_apply_unit(tmp_path, monkeypatch):
    controller = TimeController(tmp_path / "time-settings.json")
    started = []
    monkeypatch.setattr(controller, "_systemctl_start_wait", lambda unit: started.append(unit))

    result = controller.configure_ntp({"server": "time.google.com", "sync_at": "04:05"})

    assert result["settings"]["server"] == "time.google.com"
    assert load_time_settings(controller.state_path)["sync_at"] == "04:05"
    assert started == [controller.APPLY_UNIT]


def test_time_controller_refuses_manual_sync_without_an_ntp_schedule(tmp_path):
    controller = TimeController(tmp_path / "time-settings.json")

    with pytest.raises(ValueError, match="configure an NTP server"):
        controller.synchronize_now()


def test_time_controller_applies_fixed_timezone_policy_with_the_settings_unit(tmp_path, monkeypatch):
    controller = TimeController(tmp_path / "time-settings.json")
    started = []
    monkeypatch.setattr(controller, "_systemctl_start_wait", lambda unit: started.append(unit))

    result = controller.set_timezone_policy(
        {
            "timezone": "Europe/Kyiv",
            "dst_mode": "fixed",
            "fixed_timezone": "Etc/GMT-2",
        }
    )

    assert result["settings"]["fixed_timezone"] == "Etc/GMT-2"
    assert started == [controller.APPLY_UNIT]


def test_ntp_probe_reports_a_valid_udp_server_sample(monkeypatch):
    class FakeSocket:
        def settimeout(self, value):
            self.timeout = value

        def sendto(self, data, target):
            self.data = data
            self.target = target

        def recvfrom(self, size):
            response = bytearray(48)
            response[0] = 0x24  # NTPv4 server response
            response[1] = 2
            response[32:40] = time_sync._unix_to_ntp(1_000.02)
            response[40:48] = time_sync._unix_to_ntp(1_000.03)
            return bytes(response), ("192.0.2.123", 123)

        def close(self):
            pass

    monkeypatch.setattr(
        time_sync.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(time_sync.socket.AF_INET, time_sync.socket.SOCK_DGRAM, 0, "", ("192.0.2.123", 123))],
    )
    monkeypatch.setattr(time_sync.socket, "socket", lambda *args: FakeSocket())
    samples = iter([1_000.0, 1_000.05])
    monkeypatch.setattr(time_sync.time, "time", lambda: next(samples))

    result = ntp_probe("time.example")

    assert result["ok"] is True
    assert result["peer"] == "192.0.2.123"
    assert result["stratum"] == 2
    assert result["round_trip_ms"] == 50.0
