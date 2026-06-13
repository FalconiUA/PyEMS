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


def test_ems_log_path_defaults_and_override():
    assert ui.ems_log_path({}).name == "pyems.log"
    assert ui.ems_log_path({}).is_absolute()
    assert ui.ems_log_path({"logging": {"file": "/var/log/ems.log"}}) == \
        __import__("pathlib").Path("/var/log/ems.log")
    # explicit null/empty disables file logging
    assert ui.ems_log_path({"logging": {"file": None}}) is None
    assert ui.ems_log_path({"logging": {"file": ""}}) is None


def test_read_ems_log_parses_levels_logger_and_filters(tmp_path):
    log = tmp_path / "pyems.log"
    log.write_text(
        "2026-06-13 10:00:00 INFO    pyems.ems: Scenario: export_limit\n"
        "2026-06-13 10:00:01 WARNING pyems.controllers.safety: SAFETY TRIP: comms age 9.0s > 5.0s limit\n"
        "2026-06-13 10:00:02 ERROR   pyems.drivers.cached: Modbus WRITE failed\n",
        encoding="utf-8",
    )
    site = {"logging": {"file": str(log)}}

    full = ui.read_ems_log(site)
    assert full["ok"] is True
    # newest first
    assert full["entries"][0]["level"] == "ERROR"
    assert full["entries"][0]["logger"] == "pyems.drivers.cached"
    assert full["entries"][-1]["message"] == "Scenario: export_limit"
    trip = next(e for e in full["entries"] if e["level"] == "WARNING")
    assert "SAFETY TRIP" in trip["message"]

    warn = ui.read_ems_log(site, level="WARNING")
    levels = {e["level"] for e in warn["entries"]}
    assert levels == {"WARNING", "ERROR"}  # INFO filtered out


def test_read_ems_log_groups_traceback_continuation(tmp_path):
    log = tmp_path / "pyems.log"
    log.write_text(
        "2026-06-13 10:00:02 ERROR   pyems.scheduler: unhandled error in control cycle\n"
        "Traceback (most recent call last):\n"
        '  File "ems.py", line 1, in <module>\n'
        "ValueError: boom\n",
        encoding="utf-8",
    )
    result = ui.read_ems_log({"logging": {"file": str(log)}})

    assert len(result["entries"]) == 1
    msg = result["entries"][0]["message"]
    assert "unhandled error in control cycle" in msg
    assert "Traceback (most recent call last):" in msg
    assert "ValueError: boom" in msg


def test_read_ems_log_missing_and_disabled_give_clean_errors(tmp_path):
    missing = ui.read_ems_log({"logging": {"file": str(tmp_path / "none.log")}})
    assert missing["ok"] is False and missing["entries"] == []

    disabled = ui.read_ems_log({"logging": {"file": None}})
    assert disabled["ok"] is False and "disabled" in disabled["error"]


def test_logs_page_polls_ems_log_only_when_active():
    """The EMS log must auto-refresh only while the Logs view is active — the
    same active-only polling discipline as Overview."""
    app_js = (ui.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "/api/ems-log" in app_js
    show = app_js[app_js.index("function showView") :]
    body = show[: show.index("\n}\n")]
    assert 'name === "logs"' in body


def test_overview_page_is_first_and_default_view():
    """The Overview tab must be the first/default view (acceptance criterion),
    served from its own static page that exists on disk."""
    index = (ui.STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    first_tab = index[index.index('class="tab') :]
    assert 'data-view="overview"' in first_tab[: first_tab.index("</button>")]
    assert (ui.STATIC_ROOT / "pages" / "overview.html").exists()


def test_overview_does_not_poll_live_api():
    """Overview must use only the fast-loop snapshot — never /api/live (which
    opens a Modbus session) from refreshOverview."""
    app_js = (ui.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    overview = app_js[app_js.index("async function refreshOverview") :]
    body = overview[: overview.index("\n}")]
    assert "/api/fast-loop-state" in body
    assert "/api/live" not in body
