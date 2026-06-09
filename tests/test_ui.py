import pytest

from pyems import ui


def test_config_payload_lists_profiles_and_channels():
    payload = ui.config_payload()

    assert "inverters/huawei_sun2000_100ktl_m1.yaml" in payload["profiles"]
    assert payload["validation"]["ok"] is True
    names = {channel["name"] for channel in payload["available_channels"]}
    assert {"grid.W", "pv.W", "pv.WSet", "sys.safe_mode", "sys.comms_age_s"} <= names


def test_validate_site_for_ui_rejects_unknown_scenario_device():
    site = ui.load_site()
    site["scenario"]["unit_device_id"] = "missing_unit"

    with pytest.raises(ValueError, match="missing_unit"):
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
