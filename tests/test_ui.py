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
    # no alarms in the snapshot → an empty banner list (never None)
    assert result["alarms"] == []


def test_fast_loop_state_passes_through_active_alarms(tmp_path):
    snap = tmp_path / "live_state.json"
    snap.write_text(
        json.dumps(
            {
                "ok": True,
                "timestamp": "2026-06-13 10:00:00",
                "monotonic_s": 1.0,
                "values": {"grid.W": -500.0},
                "alarms": [
                    {"key": "safety.trip", "severity": "alarm", "acked": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    result = ui.fast_loop_state({"telemetry": {"live_json": str(snap)}})
    assert result["alarms"] == [
        {"key": "safety.trip", "severity": "alarm", "acked": False}
    ]


def _events_site(tmp_path, journal="journal.jsonl", audit="audit.jsonl"):
    # Both sources point at tmp so a stray repo-level logs/ file can't leak in.
    return {"events": {
        "journal_jsonl": str(tmp_path / journal),
        "ui_audit_jsonl": str(tmp_path / audit),
    }}


def test_event_log_absent_file_gives_clean_error(tmp_path):
    result = ui.event_log(_events_site(tmp_path))  # neither file exists
    assert result["ok"] is False
    assert result["events"] == []


def test_event_log_reads_tail_newest_first(tmp_path):
    (tmp_path / "journal.jsonl").write_text(
        '{"seq":1,"timestamp":"t1","severity":"alarm","source":"safety",'
        '"kind":"raised","key":"safety.trip","message":"a"}\n'
        '{"seq":2,"timestamp":"t2","severity":"alarm","source":"safety",'
        '"kind":"cleared","key":"safety.trip","message":"b"}\n',
        encoding="utf-8",
    )
    result = ui.event_log(_events_site(tmp_path))
    assert result["ok"] is True
    assert [e["seq"] for e in result["events"]] == [2, 1]  # newest first


def test_event_log_skips_torn_final_line(tmp_path):
    (tmp_path / "journal.jsonl").write_text(
        '{"seq":1,"timestamp":"t1","severity":"info","source":"safety",'
        '"kind":"info","key":null,"message":"ok"}\n'
        '{"seq":2,"timestamp":"t2","severi',  # torn write (crash mid-append)
        encoding="utf-8",
    )
    result = ui.event_log(_events_site(tmp_path))
    assert result["ok"] is True
    assert [e["seq"] for e in result["events"]] == [1]


def test_event_log_merges_ems_journal_and_ui_audit_newest_first(tmp_path):
    (tmp_path / "journal.jsonl").write_text(
        '{"timestamp":"2026-06-21 10:00:00","severity":"alarm","source":"safety",'
        '"kind":"raised","key":"safety.trip","message":"trip"}\n',
        encoding="utf-8",
    )
    (tmp_path / "audit.jsonl").write_text(
        '{"timestamp":"2026-06-21 10:05:00","severity":"info","source":"ui",'
        '"kind":"config","key":null,"message":"site.yaml saved — 1 change"}\n',
        encoding="utf-8",
    )
    result = ui.event_log(_events_site(tmp_path))
    assert result["ok"] is True
    # the later UI config edit sorts above the earlier EMS alarm
    assert [(e["source"], e["kind"]) for e in result["events"]] == [
        ("ui", "config"), ("safety", "raised"),
    ]


def test_diff_config_reports_changed_added_removed_paths():
    old = {"safety": {"max_comms_age_s": 2.0}, "recording": {"cycle_csv": "x"},
           "devices": [{"id": "pv", "host": "1.1.1.1"}]}
    new = {"safety": {"max_comms_age_s": 3.0}, "telemetry": {"live_json": "y"},
           "devices": [{"id": "pv", "host": "2.2.2.2"}]}
    changes = ui.diff_config(old, new)
    assert "safety.max_comms_age_s: 2.0 → 3.0" in changes
    assert "devices[0].host: 1.1.1.1 → 2.2.2.2" in changes
    assert any(c.startswith("− recording") for c in changes)
    assert any(c.startswith("+ telemetry") for c in changes)


def test_ui_app_save_writes_config_audit(tmp_path):
    site_path = tmp_path / "site.yaml"
    app = ui.UIApp(site_path)
    base = ui.load_site(site_path)  # normalized default (file not created yet)
    base.setdefault("events", {})["ui_audit_jsonl"] = str(tmp_path / "audit.jsonl")
    app.save(base)  # initial save → one audit event

    changed = ui.load_site(site_path)
    changed["safety"]["max_comms_age_s"] = 99.0
    app.save(changed)  # second save → audit records the single change

    lines = [
        __import__("json").loads(ln)
        for ln in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    last = lines[-1]
    assert last["source"] == "ui" and last["kind"] == "config"
    assert any("safety.max_comms_age_s" in d for d in last["details"])


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


class _RecordingTimeController:
    """Stand-in for TimeController that records dispatched calls."""

    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append(("status", None))
        return {"ok": True, "settings": {"mode": "manual"}}

    def clock(self):
        self.calls.append(("clock", None))
        return {"ok": True, "local_time": "2026-06-20 10:00:00"}

    def configure_ntp(self, payload):
        self.calls.append(("configure_ntp", payload))
        return {"ok": True, "settings": {"mode": "ntp"}}

    def set_manual_time(self, payload):
        self.calls.append(("set_manual_time", payload))
        return {"ok": True, "settings": {"mode": "manual"}}

    def set_timezone_policy(self, payload):
        self.calls.append(("set_timezone_policy", payload))
        return {"ok": True, "settings": {"dst_mode": "fixed"}}

    def test_ntp(self, payload):
        self.calls.append(("test_ntp", payload))
        return {"ok": True, "server": payload.get("server")}

    def synchronize_now(self):
        self.calls.append(("synchronize_now", None))
        return {"ok": True, "settings": {"mode": "ntp"}}


@pytest.fixture
def time_http_client(tmp_path):
    """Serve a UIApp over a real loopback socket so /api/time routing is exercised."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    fake = _RecordingTimeController()
    app = ui.UIApp(tmp_path / "site.yaml", time_controller=fake)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        payload = None if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    try:
        yield fake, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_time_get_endpoints_dispatch_to_the_controller(time_http_client):
    fake, request = time_http_client

    status, data = request("GET", "/api/time")
    assert status == 200 and data["ok"] is True
    request("GET", "/api/time/clock")

    assert ("status", None) in fake.calls
    assert ("clock", None) in fake.calls


def test_time_post_endpoints_forward_their_payloads(time_http_client):
    fake, request = time_http_client

    request("POST", "/api/time/ntp", {"server": "time.google.com", "sync_at": "03:15"})
    request("POST", "/api/time/manual", {"time": "2026-06-20T10:26"})
    request("POST", "/api/time/timezone", {"dst_mode": "fixed", "fixed_timezone": "Etc/GMT-2"})
    request("POST", "/api/time/test-ntp", {"server": "time.google.com"})
    status, data = request("POST", "/api/time/sync", {})

    dispatched = {name: arg for name, arg in fake.calls}
    assert dispatched["configure_ntp"]["server"] == "time.google.com"
    assert dispatched["set_manual_time"]["time"] == "2026-06-20T10:26"
    assert dispatched["set_timezone_policy"]["fixed_timezone"] == "Etc/GMT-2"
    assert dispatched["test_ntp"]["server"] == "time.google.com"
    assert status == 200 and data["ok"] is True


def test_time_endpoint_reports_controller_errors_as_json(time_http_client):
    fake, request = time_http_client

    def _boom():
        raise ValueError("configure an NTP server before synchronizing")

    fake.synchronize_now = _boom

    status, data = request("POST", "/api/time/sync", {})

    assert status == 400
    assert data["ok"] is False
    assert "configure an NTP server" in data["error"]


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
