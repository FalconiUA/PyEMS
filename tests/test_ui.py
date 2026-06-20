import json

import pytest

from pyems import ui


def test_config_payload_lists_profiles_and_channels():
    payload = ui.config_payload()

    assert "inverters/huawei_sun2000_100ktl_m1.yaml" in payload["profiles"]
    assert "inverters/sim_sun2000_switch.yaml" in payload["profiles"]
    assert "inverters/sim_sun2000_runstop.yaml" in payload["profiles"]
    assert payload["validation"]["ok"] is True
    names = {channel["name"] for channel in payload["available_channels"]}
    assert {"grid.W", "pv.W", "pv.WSet", "sys.safe_mode", "sys.comms_age_s"} <= names


def test_validate_site_for_ui_rejects_unknown_scenario_device():
    site = ui.load_site()
    site["scenario"]["unit_device_id"] = "missing_unit"

    with pytest.raises(ValueError, match="missing_unit"):
        ui.validate_site_for_ui(site)


def test_validate_site_for_ui_rejects_bad_write_age_tuning():
    site = ui.load_site(ui.DEFAULT_SIM_SITE)
    site["control"]["setpoint_rewrite_s"] = 10.0
    site["control"]["poll_interval_s"] = 0.5
    site["safety"]["max_write_age_s"] = 8.0

    with pytest.raises(ValueError, match="max_write_age_s"):
        ui.validate_site_for_ui(site)


def test_channel_rows_marks_scenario_and_setpoint_roles():
    site = ui.load_site()
    channels = ui.validate_site_for_ui(site)
    snapshot = {channel.name: 0.0 for channel in channels}

    rows = {row["channel"]: row for row in ui.channel_rows(site, channels, snapshot)}

    assert rows["grid.W"]["role"] == "required for scenario"
    assert rows["pv.WSet"]["role"] == "active power setpoint"
    assert rows["sys.comms_age_s"]["role"] == "required for scenario"


def test_apply_scenario_derives_channels_and_single_allocation():
    site = ui.load_site()
    site["scenario"]["control_mode"] = "import_limit"
    site["scenario"]["active_power_limit_w"] = 25000.0
    site["scenario"]["connection_point_device_id"] = "grid"
    site["scenario"]["unit_device_id"] = "pv"

    normalized = ui.normalize_site(site)

    assert normalized["connection_point_active_power"]["import_limit_w"] == 25000.0
    assert normalized["connection_point_active_power"]["unit_active_power_setpoint_channel"] == "pv.WSet"
    assert normalized["safety"]["unit_active_power_setpoint_channels"] == ["pv.WSet"]
    assert len(normalized["allocation"]["channels"]) == 1
    assert normalized["allocation"]["channels"][0]["setpoint_channel"] == "pv.WSet"


def test_profile_requirements_report_needed_registers():
    requirements = ui.device_profile_requirements(ui.load_site())
    expected = {(item["device_id"], item["expected_tag"]) for item in requirements}

    assert ("grid", "grid.W") in expected
    assert ("pv", "pv.W") in expected
    assert ("pv", "pv.WSet") in expected
    assert all(item["present"] for item in requirements)


def test_ui_app_error_log_keeps_recent_entries_newest_first(tmp_path):
    app = ui.UIApp(tmp_path / "site.yaml")

    for idx in range(ui.MAX_ERROR_LOG_ENTRIES + 1):
        app.record_error("POST /api/test-read", RuntimeError(f"Modbus Error {idx}"))

    entries = app.error_log()

    assert len(entries) == ui.MAX_ERROR_LOG_ENTRIES
    assert entries[0]["message"] == f"Modbus Error {ui.MAX_ERROR_LOG_ENTRIES}"
    assert entries[0]["source"] == "POST /api/test-read"
    assert entries[0]["level"] == "error"
    assert entries[-1]["message"] == "Modbus Error 1"

    assert app.clear_error_log() == {"ok": True, "entries": []}
    assert app.error_log() == []


def test_fast_loop_state_reads_published_snapshot(tmp_path):
    snap = tmp_path / "live_state.json"
    snap.write_text(
        json.dumps(
            {
                "ok": True,
                "timestamp": "2026-06-13 10:00:00",
                "monotonic_s": 1.0,
                "values": {
                    "grid.W": -500.0,
                    "pv.WSet": 4000.0,
                    "sys.safe_mode": 0.0,
                    "sys.comms_age_s": None,
                },
                "channels": [
                    {"name": "grid.W", "unit": "W", "writable": False},
                    {"name": "pv.WSet", "unit": "W", "writable": True},
                    {"name": "sys.safe_mode", "unit": "", "writable": True},
                    {"name": "sys.comms_age_s", "unit": "s", "writable": False},
                ],
                "cycle_s": 1.0,
                "cycle_overrun": False,
            }
        ),
        encoding="utf-8",
    )
    result = ui.fast_loop_state({"telemetry": {"live_json": str(snap)}})

    assert result["ok"] is True
    assert result["read_at"] == "2026-06-13 10:00:00"
    # raw tag -> value map passes through for tag-addressed consumers (Overview)
    assert result["values"]["grid.W"] == -500.0
    assert result["values"]["sys.comms_age_s"] is None
    # freshness measured from the file mtime, for the stale-telemetry banner
    assert result["age_s"] >= 0.0
    rows = {row["channel"]: row for row in result["rows"]}
    assert rows["grid.W"]["value"] == -500.0
    assert rows["grid.W"]["access"] == "read"
    assert rows["pv.WSet"]["access"] == "write"
    assert rows["pv.WSet"]["role"] == "active power setpoint"
    assert rows["sys.safe_mode"]["role"] == "system"
    assert rows["sys.comms_age_s"]["value"] is None  # +inf was published as null


def test_fast_loop_state_missing_snapshot_gives_clean_error(tmp_path):
    result = ui.fast_loop_state({"telemetry": {"live_json": str(tmp_path / "nope.json")}})

    assert result["ok"] is False
    assert "not running" in result["error"]
    assert result["rows"] == []


def test_fast_loop_state_path_defaults_to_logs_live_state():
    path = ui.fast_loop_state_path({})
    assert path.name == "live_state.json"
    assert path.is_absolute()


def test_fast_loop_state_reports_snapshot_age(tmp_path):
    import os
    import time

    snap = tmp_path / "live_state.json"
    snap.write_text(json.dumps({"values": {}, "channels": []}), encoding="utf-8")
    old = time.time() - 120.0
    os.utime(snap, (old, old))

    result = ui.fast_loop_state({"telemetry": {"live_json": str(snap)}})

    assert result["ok"] is True
    assert result["age_s"] >= 119.0  # a two-minute-old snapshot reads as stale


def test_overview_page_is_first_and_default_view():
    """The Overview item must be the first/default view (acceptance criterion),
    served from its own static page that exists on disk."""
    index = (ui.STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    first_item = index[index.index('class="nav-item') :]
    assert 'data-view="overview"' in first_item[: first_item.index("</button>")]
    assert (ui.STATIC_ROOT / "pages" / "overview.html").exists()


def test_overview_does_not_poll_live_api():
    """Overview must use only the fast-loop snapshot — never /api/live (which
    opens a Modbus session) from refreshOverview."""
    app_js = (ui.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    overview = app_js[app_js.index("async function refreshOverview") :]
    body = overview[: overview.index("\n}")]
    assert "/api/fast-loop-state" in body
    assert "/api/live" not in body


def test_time_page_uses_the_os_clock_endpoint_not_ems_telemetry():
    index = (ui.STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    page = (ui.STATIC_ROOT / "pages" / "time.html").read_text(encoding="utf-8")
    app_js = (ui.STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'data-view="time"' in index
    assert 'id="timeCurrent"' in page
    assert 'id="time.timezone"' in page
    assert 'id="timeSyncDiagnostics"' in page
    assert "/api/time/clock" in app_js
    assert 'api("/api/time")' in app_js


# ── Modbus connection diagnostics ────────────────────────────────────────────
HUAWEI_PROFILE = "inverters/huawei_sun2000_100ktl_m1.yaml"
GRID_PROFILE = "meters/example_grid_meter.yaml"


class _FakeOkClient:
    """A Modbus client that connects and answers every read with zeros — enough
    for diagnose_device to walk the full layered probe."""

    def __init__(self):
        self.reads = []

    def connect(self):
        return True

    def close(self):
        pass

    def read_holding_registers(self, address, count, slave):
        self.reads.append((address, count, slave))

        class _R:
            registers = [0] * count

            def isError(self):
                return False

        return _R()


def test_diagnose_device_tcp_unreachable_short_circuits(monkeypatch):
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr(ui.socket, "create_connection", refuse)
    result = ui.diagnose_device(
        {"id": "pv", "profile": HUAWEI_PROFILE, "host": "10.9.9.9", "slave_id": 1}
    )

    assert result["ok"] is False
    assert [c["step"] for c in result["checks"]] == ["tcp"]  # no Modbus attempt
    assert result["checks"][0]["ok"] is False
    assert result["checks"][0]["cause"] == "refused"  # named, not generic
    assert "unreachable" in result["summary"]
    assert result["causes"] and "wrong port" in result["causes"][0]
    assert result["endpoint"]["port"] == 502  # profile default_port resolved


def test_classify_tcp_error_names_each_cause():
    import socket as _socket

    assert ui._classify_tcp_error(ConnectionRefusedError()) == "refused"
    assert ui._classify_tcp_error(_socket.gaierror()) == "dns"
    assert ui._classify_tcp_error(TimeoutError()) == "timeout"
    assert ui._classify_tcp_error(OSError(ui.errno.EHOSTUNREACH, "x")) == "no_route"
    assert ui._classify_tcp_error(OSError("something else")) == "other"


def test_read_failure_causes_rtu_lists_serial_culprits():
    regs = [{"ok": False, "timeout": True, "exception_code": None}]
    serial = {"baudrate": 19200, "parity": "E", "bytesize": 8, "stopbits": 1}
    causes = ui._read_failure_causes("modbus_rtu", regs, serial)
    text = " ".join(causes)
    assert "19200" in text  # the actual baud is echoed into the checklist
    assert "A/B" in text and "unit-id scan" in text


def test_read_failure_causes_address_mismatch_points_at_profile():
    regs = [{"ok": False, "timeout": False, "exception_code": 2}]
    causes = ui._read_failure_causes("modbus_tcp", regs, None)
    assert any("illegal data address" in c for c in causes)


def test_read_failure_causes_flags_implausible_value():
    regs = [{"ok": True, "in_bounds": False}]
    causes = ui._read_failure_causes("modbus_tcp", regs, None)
    assert any("word/byte order" in c for c in causes)


def test_diagnose_device_rtu_echoes_serial_params(monkeypatch):
    fake = _FakeOkClient()
    monkeypatch.setattr(ui.md, "make_client", lambda *a, **k: fake)
    monkeypatch.setattr(ui.Path, "exists", lambda self: True)  # pretend the port exists
    result = ui.diagnose_device(
        {"id": "gen", "profile": "gensets/example_genset.yaml", "host": "/dev/ttyUSB0",
         "slave_id": 1, "serial": {"baudrate": 19200, "parity": "E"}}
    )

    serial = result["endpoint"]["serial"]
    assert serial["baudrate"] == 19200 and serial["parity"] == "E"
    assert serial["stopbits"] == 1 and serial["bytesize"] == 8  # defaults filled in
    assert [c["step"] for c in result["checks"]] == ["serial", "modbus_connect"]


def test_diagnose_device_probes_every_register(monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def fake_conn(addr, timeout=None):
        yield object()

    fake = _FakeOkClient()
    monkeypatch.setattr(ui.socket, "create_connection", fake_conn)
    monkeypatch.setattr(ui.md, "make_client", lambda *a, **k: fake)

    result = ui.diagnose_device(
        {"id": "pv", "profile": HUAWEI_PROFILE, "host": "192.168.0.100", "slave_id": 7}
    )

    assert [c["step"] for c in result["checks"]] == ["tcp", "modbus_connect"]
    assert all(c["ok"] for c in result["checks"])
    assert len(result["registers"]) == result["endpoint"]["register_count"]
    assert result["registers"][0]["channel"].startswith("pv.")
    # the device's slave/unit id is threaded into the actual reads
    assert fake.reads and all(slave == 7 for (_a, _c, slave) in fake.reads)


def test_diagnose_site_runs_each_device_without_scenario_validation(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("Connection refused")

    monkeypatch.setattr(ui.socket, "create_connection", refuse)
    # No scenario/safety/allocation — diagnose must not require a valid site.
    site = {
        "devices": [
            {"id": "grid", "profile": GRID_PROFILE, "host": "1.2.3.4", "slave_id": 1},
            {"id": "pv", "profile": HUAWEI_PROFILE, "host": "1.2.3.5", "slave_id": 1},
        ]
    }

    result = ui.diagnose_site(site)

    assert result["ok"] is False
    assert [d["device_id"] for d in result["devices"]] == ["grid", "pv"]
    assert all(d["checks"][0]["step"] == "tcp" for d in result["devices"])


def test_diagnostics_route_and_page_are_wired():
    """The Diagnostics view exists end to end: nav tab, static page, the POST
    route and the button handler."""
    index = (ui.STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert 'data-view="diagnostics"' in index
    assert (ui.STATIC_ROOT / "pages" / "diagnostics.html").exists()
    app_js = (ui.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "/api/diagnose" in app_js
    assert "runDiagnosticsBtn" in app_js
