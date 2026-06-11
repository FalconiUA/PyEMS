"""device_fields must stay THE single vocabulary for profile channel names.

Mirrors test_system_tags.py: the checks introspect the vocabulary, glob the
real profiles and parse the SunSpec model JSONs checked into
documents/SunSpec/, so a newly added field or profile is covered
automatically, with no test edits.
"""
import json

import pytest
import yaml

from pyems.device_fields import (
    DEVICE_FIELDS,
    EMS_FIELDS,
    SUNSPEC_FIELDS,
    validate_channel,
)
from pyems.drivers.modbus_device import DeviceProfile
from pyems.ems import PROFILES, ROOT

SUNSPEC_MODELS = sorted((ROOT / "documents" / "SunSpec").glob("model_*.json"))


def _sunspec_points() -> dict[str, str | None]:
    """name -> units of every point in the checked-in SunSpec models."""
    points: dict[str, str | None] = {}
    for path in SUNSPEC_MODELS:
        def walk(group: dict) -> None:
            for p in group.get("points", []):
                points.setdefault(p["name"], p.get("units"))
            for sub in group.get("groups", []):
                walk(sub)
        walk(json.loads(path.read_text(encoding="utf-8"))["group"])
    return points


# ── the vocabulary itself ─────────────────────────────────────────────────────
def test_vocabulary_names_and_units_are_well_formed():
    allowed_units = {"W", "var", "VA", "Hz", "A", "V", "%", "%WHRtg", ""}
    for name, unit in DEVICE_FIELDS.items():
        assert name.isidentifier(), f"field {name!r} must be a bare identifier"
        assert unit in allowed_units, f"field {name!r}: non-SI unit {unit!r}"


def test_sunspec_fields_match_the_standard_models():
    """Every SUNSPEC_FIELDS entry is a real point name with the model's unit
    — the vocabulary provably cannot drift from the checked-in standard."""
    assert SUNSPEC_MODELS, "documents/SunSpec/model_*.json missing"
    points = _sunspec_points()
    for name, unit in SUNSPEC_FIELDS.items():
        assert name in points, f"{name!r} is not a SunSpec point name"
        assert points[name] == unit, (
            f"{name!r}: vocabulary unit {unit!r} != SunSpec {points[name]!r}"
        )


def test_ems_fields_never_shadow_a_sunspec_point():
    collisions = set(EMS_FIELDS) & set(_sunspec_points())
    assert not collisions, (
        f"EMS-specific fields {sorted(collisions)} collide with SunSpec point "
        f"names — move them to SUNSPEC_FIELDS with the standard unit instead"
    )


# ── every real profile conforms ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "profile_path",
    sorted(PROFILES.rglob("*.yaml")),
    ids=lambda p: p.name,
)
def test_repo_profiles_conform_to_vocabulary(profile_path):
    profile = DeviceProfile.load(profile_path)  # raises on a violation
    assert profile.registers


# ── violations fail at load, with actionable messages ────────────────────────
def test_vendor_register_map_name_is_rejected():
    with pytest.raises(ValueError, match="<class>.<Field>"):
        validate_channel("Phase A active power", "kW")


def test_unknown_field_is_rejected():
    with pytest.raises(ValueError, match="unknown field 'Foo'"):
        validate_channel("grid.Foo", "W")


def test_non_canonical_unit_is_rejected():
    # the SmartLogger trap: gain-1000 register left as kW would feed values
    # 1000x off into the export-limit controller
    with pytest.raises(ValueError, match="scale"):
        validate_channel("grid.W", "kW")


def test_whitespace_in_channel_is_rejected():
    with pytest.raises(ValueError, match="whitespace"):
        validate_channel("grid.PhVphC ", "V")


def test_profile_load_reports_the_file(tmp_path):
    bad = tmp_path / "meter.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "model": "Bad Meter",
                "protocol": "modbus_tcp",
                "registers": [
                    {
                        "channel": "grid.W",
                        "address": 32278,
                        "type": "int32",
                        "scale": 1000,
                        "unit": "kW",
                        "access": "read",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="meter.yaml"):
        DeviceProfile.load(bad)
