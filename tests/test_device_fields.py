"""device_fields must stay THE single vocabulary for profile channel names.

Mirrors test_system_tags.py: the checks introspect the vocabulary and glob
the real profiles, so a newly added field or profile is covered
automatically, with no test edits.
"""
import pytest
import yaml

from pyems.device_fields import DEVICE_FIELDS, validate_channel
from pyems.drivers.modbus_device import DeviceProfile
from pyems.ems import PROFILES


# ── the vocabulary itself ─────────────────────────────────────────────────────
def test_vocabulary_names_and_units_are_well_formed():
    allowed_units = {"W", "VAr", "VA", "Hz", "A", "V", ""}
    for name, unit in DEVICE_FIELDS.items():
        assert name.isidentifier(), f"field {name!r} must be a bare identifier"
        assert unit in allowed_units, f"field {name!r}: non-SI unit {unit!r}"


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
