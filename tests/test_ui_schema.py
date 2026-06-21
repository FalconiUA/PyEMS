"""The settings registry (ui_schema): validation, helpers, and the nav/forms
contracts the front end depends on."""
import html
import re

import pytest

from pyems import ui, ui_schema


# ── path helpers ─────────────────────────────────────────────────────────────
def test_get_set_path_round_trip_with_list_index():
    data = {}
    ui_schema.set_path(data, "allocation.channels.0.p_min_w", 0)
    ui_schema.set_path(data, "allocation.channels.0.p_max_w", 100000)
    ui_schema.set_path(data, "safety.max_comms_age_s", 10)

    assert data == {
        "allocation": {"channels": [{"p_min_w": 0, "p_max_w": 100000}]},
        "safety": {"max_comms_age_s": 10},
    }
    assert ui_schema.get_path(data, "allocation.channels.0.p_max_w") == 100000
    assert ui_schema.get_path(data, "missing.key", "fallback") == "fallback"
    assert ui_schema.has_path(data, "safety.max_comms_age_s")
    assert not ui_schema.has_path(data, "safety.nope")


# ── validation ───────────────────────────────────────────────────────────────
def test_validate_site_accepts_default_and_sim_sites():
    ui_schema.validate_site(ui.load_site())
    ui_schema.validate_site(ui.load_site(ui.DEFAULT_SIM_SITE))


def test_validate_site_rejects_non_finite_number():
    site = ui.load_site()
    site["control"]["fast_cycle_s"] = "fast"
    with pytest.raises(ValueError, match="control.fast_cycle_s"):
        ui_schema.validate_site(site)


def test_validate_site_rejects_missing_required_field():
    site = ui.load_site()
    del site["safety"]["max_comms_age_s"]
    with pytest.raises(ValueError, match="safety.max_comms_age_s is required"):
        ui_schema.validate_site(site)


def test_validate_site_skips_absent_optional_section():
    site = ui.load_site()
    site.pop("simulation", None)
    site.pop("setpoint_compliance", None)
    ui_schema.validate_site(site)  # no error: optional sections simply absent


def test_optional_section_field_checked_only_when_present():
    site = ui.load_site()
    site["setpoint_compliance"] = {"tolerance_w": "lots"}
    with pytest.raises(ValueError, match="setpoint_compliance.tolerance_w"):
        ui_schema.validate_site(site)


# ── served payload ───────────────────────────────────────────────────────────
def test_schema_payload_shape():
    payload = ui_schema.schema_payload()
    assert [group["group"] for group in payload["nav"]] == [
        "Monitor", "Control", "Setup", "Tools",
    ]
    paths = {field["path"] for field in payload["fields"]}
    assert {"control.fast_cycle_s", "safety.max_comms_age_s", "telemetry.live_json"} <= paths


def test_settings_schema_is_served_by_get():
    # The endpoint is wired in do_GET; the payload helper is its body.
    app_js = (ui.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    ui_py = (ui.STATIC_ROOT.parent / "ui.py").read_text(encoding="utf-8")
    assert '"/api/settings-schema"' in ui_py
    assert "/api/settings-schema" in app_js


# ── nav tree mirrors index.html ──────────────────────────────────────────────
def test_index_sidebar_mirrors_nav_tree():
    index = (ui.STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    items = re.findall(r'data-view="([a-z-]+)">([^<]+)</button>', index)
    index_views = [view for view, _ in items]

    nav_views = [page["view"] for group in ui_schema.NAV for page in group["pages"]]
    assert index_views == nav_views, "sidebar order/views must match ui_schema.NAV"
    assert index_views[0] == "overview", "Overview stays the first/default view"

    labels = dict(items)
    for group in ui_schema.NAV:
        assert f'class="nav-group-title">{group["group"]}' in index
        for page in group["pages"]:
            assert html.unescape(labels[page["view"]].strip()) == page["label"]


# ── every grouped field has a render target ──────────────────────────────────
def test_schema_groups_have_containers_in_pages():
    pages = "".join(
        path.read_text(encoding="utf-8")
        for path in (ui.STATIC_ROOT / "pages").glob("*.html")
    )
    groups = {field["group"] for field in ui_schema.FIELDS if field.get("group")}
    for group in groups:
        assert f'data-schema-group="{group}"' in pages, f"no container for group {group}"
