"""Controller clock configuration and NTP probe tests (no real clock changes)."""

from __future__ import annotations

import pytest

from pyems import time_sync
from pyems import ui
from pyems.time_sync import (
    available_timezones,
    effective_timezone,
    fixed_timezone_options,
    load_time_settings,
    load_time_sync_status,
    normalize_time_settings,
    ntp_probe,
    validate_manual_time,
    validate_ntp_server,
    validate_sync_time,
    validate_timezone,
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


# ── settings file and status validation error paths ──────────────────────────
def test_manual_time_rejects_an_unparseable_value():
    with pytest.raises(ValueError, match="manual time must be"):
        validate_manual_time("yesterday")


@pytest.mark.parametrize("value", ["", "../etc", "Europe", "Europe/..", "x/y z"])
def test_timezone_rejects_non_iana_or_traversal_values(value):
    with pytest.raises(ValueError, match="IANA"):
        validate_timezone(value)


@pytest.mark.parametrize("value", [42, "ntp", ["mode"]])
def test_normalize_time_settings_requires_a_json_object(value):
    with pytest.raises(ValueError, match="must be a JSON object"):
        normalize_time_settings(value)


def test_normalize_time_settings_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="time mode must be"):
        normalize_time_settings({"mode": "gps"})


def test_normalize_timezone_policy_rejects_unknown_dst_mode():
    with pytest.raises(ValueError, match="DST mode"):
        normalize_time_settings({"mode": "manual", "dst_mode": "summer"})


def test_fixed_dst_mode_requires_a_fixed_offset_zone():
    with pytest.raises(ValueError, match="fixed UTC offset"):
        normalize_time_settings(
            {"mode": "manual", "dst_mode": "fixed", "fixed_timezone": "Europe/Kyiv"}
        )


def test_load_time_settings_returns_defaults_when_absent(tmp_path):
    assert load_time_settings(tmp_path / "missing.json") == {
        "mode": "manual",
        "dst_mode": "automatic",
    }


def test_load_time_settings_reports_unreadable_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="could not read time settings"):
        load_time_settings(path)


def test_load_time_sync_status_is_none_when_absent(tmp_path):
    assert load_time_sync_status(tmp_path / "missing.json") is None


def test_load_time_sync_status_degrades_on_corrupt_file(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("{broken", encoding="utf-8")

    status = load_time_sync_status(path)
    assert status["ok"] is False
    assert "could not read" in status["message"]


def test_available_timezones_includes_utc_and_real_names():
    zones = available_timezones()
    assert "UTC" in zones
    assert any("/" in zone for zone in zones)


# ── ntp_probe failure branches ───────────────────────────────────────────────
def test_ntp_probe_reports_unresolvable_servers(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("name resolution failed")

    monkeypatch.setattr(time_sync.socket, "getaddrinfo", _boom)

    with pytest.raises(ValueError, match="cannot resolve NTP server"):
        ntp_probe("time.example")


def _patch_single_address(monkeypatch):
    monkeypatch.setattr(
        time_sync.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (time_sync.socket.AF_INET, time_sync.socket.SOCK_DGRAM, 0, "", ("192.0.2.1", 123))
        ],
    )


def test_ntp_probe_reports_a_socket_timeout(monkeypatch):
    _patch_single_address(monkeypatch)

    class TimingOutSocket:
        def settimeout(self, value):
            pass

        def sendto(self, data, target):
            pass

        def recvfrom(self, size):
            raise OSError("timed out")

        def close(self):
            pass

    monkeypatch.setattr(time_sync.socket, "socket", lambda *a: TimingOutSocket())

    with pytest.raises(ValueError, match="did not respond"):
        ntp_probe("time.example")


def _fake_socket_returning(response: bytes, monkeypatch):
    _patch_single_address(monkeypatch)

    class FixedSocket:
        def settimeout(self, value):
            pass

        def sendto(self, data, target):
            pass

        def recvfrom(self, size):
            return response, ("192.0.2.1", 123)

        def close(self):
            pass

    monkeypatch.setattr(time_sync.socket, "socket", lambda *a: FixedSocket())


def test_ntp_probe_rejects_a_truncated_response(monkeypatch):
    _fake_socket_returning(bytes(20), monkeypatch)

    with pytest.raises(ValueError, match="truncated response"):
        ntp_probe("time.example")


def test_ntp_probe_rejects_an_unsynchronised_server(monkeypatch):
    response = bytearray(48)
    response[0] = 0x24  # server mode 4
    response[1] = 0  # stratum 0 = unsynchronised / kiss-o'-death
    _fake_socket_returning(bytes(response), monkeypatch)

    with pytest.raises(ValueError, match="unsynchronised response"):
        ntp_probe("time.example")


def test_ntp_probe_rejects_an_empty_timestamp(monkeypatch):
    response = bytearray(48)
    response[0] = 0x24
    response[1] = 2
    # transmit/receive timestamps left as zero → invalid
    _fake_socket_returning(bytes(response), monkeypatch)

    with pytest.raises(ValueError, match="invalid timestamp"):
        ntp_probe("time.example")


# ── TimeController OS-facing methods (timedatectl / NTP faked) ────────────────
def test_time_controller_status_parses_timedatectl(tmp_path, monkeypatch):
    controller = TimeController(tmp_path / "time-settings.json", tmp_path / "status.json")

    class FakeCompleted:
        returncode = 0
        stdout = (
            "Timezone=Europe/Kyiv\nNTP=no\nNTPSynchronized=yes\nCanNTP=yes\nLocalRTC=no\n"
        )
        stderr = ""

    monkeypatch.setattr(ui.subprocess, "run", lambda *a, **k: FakeCompleted())

    status = controller.status()

    assert status["available"] is True
    assert status["timezone"] == "Europe/Kyiv"
    assert status["automatic_ntp"] is False
    assert status["ntp_synchronized"] is True
    assert "UTC" in status["timezones"]


def test_time_controller_status_marks_unavailable_without_timedatectl(tmp_path, monkeypatch):
    controller = TimeController(tmp_path / "time-settings.json", tmp_path / "status.json")

    def _boom(*a, **k):
        raise OSError("no timedatectl here")

    monkeypatch.setattr(ui.subprocess, "run", _boom)

    status = controller.status()

    assert status["available"] is False
    assert "unavailable" in status["reason"]


def test_time_controller_set_manual_time_runs_the_manual_unit(tmp_path, monkeypatch):
    controller = TimeController(tmp_path / "time-settings.json")
    started = []
    monkeypatch.setattr(controller, "_systemctl_start_wait", lambda unit: started.append(unit))

    result = controller.set_manual_time({"time": "2026-06-20T10:26"})

    assert result["settings"]["manual_time"] == "2026-06-20 10:26:00"
    assert result["settings"]["mode"] == "manual"
    assert started == [controller.manual_time_unit]


def test_time_controller_test_ntp_delegates_to_the_probe(tmp_path, monkeypatch):
    controller = TimeController(tmp_path / "time-settings.json")
    monkeypatch.setattr(ui, "ntp_probe", lambda server: {"ok": True, "server": server})

    assert controller.test_ntp({"server": "time.google.com"}) == {
        "ok": True,
        "server": "time.google.com",
    }


def test_time_controller_synchronize_now_starts_the_sync_unit_when_ntp(tmp_path, monkeypatch):
    state = tmp_path / "time-settings.json"
    write_time_settings({"mode": "ntp", "server": "time.google.com", "sync_at": "03:15"}, state)
    controller = TimeController(state)
    started = []
    monkeypatch.setattr(controller, "_systemctl_start_wait", lambda unit: started.append(unit))

    result = controller.synchronize_now()

    assert result["ok"] is True
    assert started == [controller.sync_now_unit]
