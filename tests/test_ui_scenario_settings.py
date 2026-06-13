"""Scenario tab settings: headroom and curtailment gradient round-trip."""
from pyems import ui


def test_apply_scenario_binds_headroom_to_selected_unit():
    site = ui.normalize_site({
        "scenario": {"unit_device_id": "inv7", "connection_point_device_id": "grid"},
        "devices": [
            {"id": "grid", "profile": "meters/example_grid_meter.yaml",
             "host": "127.0.0.1", "slave_id": 1},
            {"id": "inv7", "profile": "inverters/huawei_sun2000_100ktl_m1.yaml",
             "host": "127.0.0.1", "slave_id": 1},
        ],
    })
    headroom = site["setpoint_headroom"]
    # channels always follow the selected unit, numbers get defaults
    assert headroom["unit_active_power_channel"] == "inv7.W"
    assert headroom["unit_active_power_setpoint_channel"] == "inv7.WSet"
    assert headroom["headroom_w"] > 0
    assert headroom["headroom_pct"] == 0


def test_apply_scenario_keeps_operator_headroom_values():
    site = ui.normalize_site({
        "setpoint_headroom": {"headroom_w": 25000, "headroom_pct": 20},
        "devices": [
            {"id": "grid", "profile": "meters/example_grid_meter.yaml",
             "host": "127.0.0.1", "slave_id": 1},
            {"id": "pv", "profile": "inverters/huawei_sun2000_100ktl_m1.yaml",
             "host": "127.0.0.1", "slave_id": 1},
        ],
    })
    assert site["setpoint_headroom"]["headroom_w"] == 25000
    assert site["setpoint_headroom"]["headroom_pct"] == 20


def test_normalize_preserves_curtailment_gradient():
    site = ui.normalize_site({
        "allocation": {"channels": [{
            "setpoint_channel": "pv.WSet", "p_min_w": 0, "p_max_w": 100000,
            "default_w": 100000, "ramp_rate_w_per_s": 5000,
            "ramp_down_w_per_s": 50000, "deadband_w": 200,
        }]},
        "devices": [
            {"id": "grid", "profile": "meters/example_grid_meter.yaml",
             "host": "127.0.0.1", "slave_id": 1},
            {"id": "pv", "profile": "inverters/huawei_sun2000_100ktl_m1.yaml",
             "host": "127.0.0.1", "slave_id": 1},
        ],
    })
    assert site["allocation"]["channels"][0]["ramp_down_w_per_s"] == 50000


def test_frontend_gather_site_preserves_hidden_operational_settings():
    app_js = (ui.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    gather = app_js[app_js.index("function gatherSite()") :]
    gather = gather[: gather.index("function gatherProfile()")]

    assert "const data = { ...(site.devices[idx] || {}) };" in app_js
    assert "next.control = {\n    ...(site.control || {})," in gather
    assert "next.safety = {\n    ...(site.safety || {})," in gather
    assert "const allocChannel = {\n    ...(allocationCfg() || {})," in gather
