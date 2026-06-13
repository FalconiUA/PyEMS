"""Local web API and static file server for PyEMS configuration UI."""

from __future__ import annotations

import argparse
import json
import logging
import math
import mimetypes
import subprocess
import sys
import threading
import time
import urllib.request
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from pyems.channels import Channel, SystemState
from pyems.commands import (
    read_command_file,
    write_command_file,
    write_inverter_command,
)
from pyems.device_fields import FIELD_LABELS, field_label
from pyems.system_tags import (
    COMMAND_AGE_CHANNEL,
    COMMS_AGE_CHANNEL,
    GENERATION_ALLOWED_CHANNEL,
    GENERATION_GATE_ACTIVE_CHANNEL,
    INVERTER_COMMAND_CHANNEL,
    INVERTER_COMMAND_ID_CHANNEL,
    INVERTER_RUN_STATE_CHANNEL,
    SAFE_MODE_CHANNEL,
    SETPOINT_VIOLATION_CHANNEL,
    WRITE_AGE_CHANNEL,
    comms_age_channel,
)
from pyems.drivers.composite import CompositeDriver
from pyems.ems import (
    DEFAULT_SITE,
    IMPORT_LIMIT_MODE,
    PROFILES,
    ROOT,
    _hard_switch_config,
    _validate_hard_switch_channels,
    build_device_drivers,
    required_channels,
    validate_bindings,
    validate_safety_allocation,
    validate_setpoint_keepalive,
    validate_write_age_guard,
)
from pyems.logging import setup_logging


DEFAULT_SITE_TEMPLATE = DEFAULT_SITE.parent / "default_site.yaml"
DEFAULT_SIM_SITE = DEFAULT_SITE.parent / "site.sim.yaml"
STATIC_ROOT = Path(__file__).with_name("ui_static")
AUTO_PID_GAINS = {"kp": 0.4, "ki": 0.08, "kd": 0.0, "tt": 5.0}
VERY_HIGH_IMPORT_LIMIT_W = 1_000_000_000.0
MAX_ERROR_LOG_ENTRIES = 100

logger = logging.getLogger(__name__)


def _deep_merge(default: Any, value: Any) -> Any:
    if isinstance(default, dict) and isinstance(value, dict):
        merged = deepcopy(default)
        for key, item in value.items():
            merged[key] = _deep_merge(merged.get(key), item)
        return merged
    if value is None:
        return deepcopy(default)
    return deepcopy(value)


def load_default_site(template_path: str | Path = DEFAULT_SITE_TEMPLATE) -> dict[str, Any]:
    path = Path(template_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _first_allocation(site: dict[str, Any]) -> dict[str, Any]:
    site.setdefault("allocation", {}).setdefault("channels", [])
    if not site["allocation"]["channels"]:
        site["allocation"]["channels"].append(
            {
                "setpoint_channel": "pv.WSet",
                "p_min_w": 0.0,
                "p_max_w": 100000.0,
                "default_w": 100000.0,
                "ramp_rate_w_per_s": 5000.0,
                "deadband_w": 200.0,
            }
        )
    return site["allocation"]["channels"][0]


def apply_scenario(site: dict[str, Any]) -> dict[str, Any]:
    scenario = site.setdefault("scenario", {})
    mode = scenario.get("control_mode") or "export_limit"
    scenario["control_mode"] = mode
    limit_w = float(scenario.get("active_power_limit_w", site["export_limit"].get("limit_w", 0.0)))
    scenario["active_power_limit_w"] = limit_w
    scenario.setdefault("connection_point_device_id", "grid")
    scenario.setdefault("unit_device_id", "pv")
    scenario.setdefault("pid_tuning", "auto")
    scenario.setdefault("export_priority", 5)
    scenario.setdefault("regulation_priority", 10)

    cp_active_power_channel = f"{scenario['connection_point_device_id']}.W"
    unit_active_power_channel = f"{scenario['unit_device_id']}.W"
    unit_active_power_setpoint_channel = f"{scenario['unit_device_id']}.WSet"

    site.setdefault("export_limit", {})
    site["export_limit"].update(
        {
            "limit_w": limit_w if mode == "export_limit" else 0.0,
            "priority": int(scenario["export_priority"]),
            "connection_point_active_power_channel": cp_active_power_channel,
            "unit_active_power_channel": unit_active_power_channel,
            "unit_active_power_setpoint_channel": unit_active_power_setpoint_channel,
        }
    )

    site.setdefault("connection_point_active_power", {}).setdefault("gains", {})
    if scenario["pid_tuning"] == "auto":
        site["connection_point_active_power"]["gains"] = dict(AUTO_PID_GAINS)
    site["connection_point_active_power"].update(
        {
            "export_limit_w": limit_w if mode == "export_limit" else 0.0,
            "import_limit_w": limit_w if mode == IMPORT_LIMIT_MODE else VERY_HIGH_IMPORT_LIMIT_W,
            "priority": int(scenario["regulation_priority"]),
            "connection_point_active_power_channel": cp_active_power_channel,
            "unit_active_power_channel": unit_active_power_channel,
            "unit_active_power_setpoint_channel": unit_active_power_setpoint_channel,
        }
    )

    site.setdefault("safety", {})
    site["safety"]["unit_active_power_setpoint_channels"] = [unit_active_power_setpoint_channel]

    allocation = _first_allocation(site)
    allocation["setpoint_channel"] = unit_active_power_setpoint_channel
    site["allocation"]["channels"] = [allocation]

    # Available-power headroom: channel bindings always follow the selected
    # unit; the numbers (floor W / % of output) are operator-tunable here.
    headroom = site.setdefault("setpoint_headroom", {})
    headroom.setdefault(
        "headroom_w", round(0.1 * float(allocation.get("p_max_w", 100000.0)))
    )
    headroom.setdefault("headroom_pct", 0)
    headroom["unit_active_power_channel"] = unit_active_power_channel
    headroom["unit_active_power_setpoint_channel"] = unit_active_power_setpoint_channel

    compliance = site.get("setpoint_compliance")
    if compliance is not None:
        compliance.setdefault("tolerance_w", 2000.0)
        compliance.setdefault("max_violation_s", 30.0)
        compliance["unit_active_power_channel"] = unit_active_power_channel
        compliance["unit_active_power_setpoint_channel"] = unit_active_power_setpoint_channel
    return site


def normalize_site(site: dict[str, Any]) -> dict[str, Any]:
    normalized = _deep_merge(load_default_site(), site)
    normalized.setdefault("devices", [])
    normalized.setdefault("control", {})
    normalized.setdefault("safety", {})
    normalized.setdefault("allocation", {}).setdefault("channels", [])
    return apply_scenario(normalized)


def load_site(site_path: str | Path = DEFAULT_SITE) -> dict[str, Any]:
    path = Path(site_path)
    if not path.exists():
        return normalize_site(load_default_site())
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return normalize_site(data)


def list_profiles() -> list[str]:
    if not PROFILES.exists():
        return []
    return sorted(path.relative_to(PROFILES).as_posix() for path in PROFILES.rglob("*.yaml"))


def _system_channels(device_ids: list[str] | None = None) -> list[Channel]:
    channels = [
        Channel(COMMS_AGE_CHANNEL, unit="s", value=0.0),
        Channel(WRITE_AGE_CHANNEL, unit="s", value=0.0),
        Channel(SAFE_MODE_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
        Channel(SETPOINT_VIOLATION_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
    ]
    channels.extend(
        Channel(comms_age_channel(device_id), unit="s", value=0.0)
        for device_id in (device_ids or [])
    )
    return channels


def _generation_channels(site: dict[str, Any]) -> list[Channel]:
    """Operation status tags (generation gate + hard switch), present only when
    the site enables them. Mirrors the channels build_ems adds, so the UI tag
    pool matches the EMS one and validate_bindings agrees on both sides."""
    channels: list[Channel] = []
    if (site.get("control") or {}).get("command_json"):
        channels += [
            Channel(GENERATION_ALLOWED_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
            Channel(GENERATION_GATE_ACTIVE_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
            Channel(COMMAND_AGE_CHANNEL, unit="s", value=0.0),
        ]
    if site.get("hard_switch"):
        channels += [
            Channel(INVERTER_COMMAND_CHANNEL, unit="", min_val=0, max_val=1),
            Channel(INVERTER_COMMAND_ID_CHANNEL, unit=""),
            Channel(INVERTER_RUN_STATE_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
        ]
    return channels


def _device_channels_for_site(site: dict[str, Any]) -> list[Channel]:
    drivers = build_device_drivers(site["devices"])
    configured_ids = [device.get("id") for device in site["devices"]]
    device_ids = configured_ids if all(configured_ids) else None
    return (
        CompositeDriver(drivers, device_ids=device_ids).channels()
        + _system_channels(device_ids)
        + _generation_channels(site)
    )


def _require_mapping(mapping: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{path}.{key} must be a mapping")
    return value


def _require_list(mapping: dict[str, Any], key: str, path: str) -> list[Any]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{path}.{key} must be a list")
    return value


def _require_text(mapping: dict[str, Any], key: str, path: str) -> None:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}.{key} is required")


def _require_number(mapping: dict[str, Any], key: str, path: str) -> None:
    value = mapping.get(key)
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{path}.{key} must be a finite number")


def validate_site_for_ui(site: dict[str, Any]) -> list[Channel]:
    site = normalize_site(site)
    scenario = _require_mapping(site, "scenario", "site")
    _require_text(scenario, "control_mode", "scenario")
    _require_number(scenario, "active_power_limit_w", "scenario")
    for key in ("connection_point_device_id", "unit_device_id"):
        _require_text(scenario, key, "scenario")

    control = _require_mapping(site, "control", "site")
    for key in ("fast_cycle_s", "poll_interval_s"):
        _require_number(control, key, "control")
    for key in ("setpoint_rewrite_s", "command_max_age_s", "generation_gate_priority"):
        if key in control:
            _require_number(control, key, "control")
    if "command_json" in control and not isinstance(control["command_json"], str):
        raise ValueError("control.command_json must be text")

    safety = _require_mapping(site, "safety", "site")
    _require_number(safety, "max_comms_age_s", "safety")
    for key in (
        "safe_active_power_w",
        "max_write_age_s",
        "device_comms_watchdog_s",
        "max_measurement_frozen_s",
    ):
        if key in safety:
            _require_number(safety, key, "safety")
    device_comms = safety.get("device_comms_max_age_s") or {}
    if not isinstance(device_comms, dict):
        raise ValueError("safety.device_comms_max_age_s must be a mapping")
    for device_id, _ in device_comms.items():
        _require_number(device_comms, device_id, "safety.device_comms_max_age_s")
    frozen = safety.get("frozen_measurement_channels") or []
    if not isinstance(frozen, list):
        raise ValueError("safety.frozen_measurement_channels must be a list")
    setpoint_channels = _require_list(safety, "unit_active_power_setpoint_channels", "safety")
    if not setpoint_channels:
        raise ValueError("safety.unit_active_power_setpoint_channels must not be empty")

    allocation = _require_mapping(site, "allocation", "site")
    allocation_channels = _require_list(allocation, "channels", "allocation")
    if len(allocation_channels) != 1:
        raise ValueError("allocation.channels must contain exactly one scenario-generated channel")
    _require_text(allocation_channels[0], "setpoint_channel", "allocation.channels[0]")
    for key in ("p_min_w", "p_max_w", "default_w", "ramp_rate_w_per_s", "deadband_w"):
        _require_number(allocation_channels[0], key, "allocation.channels[0]")
    if "ramp_down_w_per_s" in allocation_channels[0]:
        _require_number(allocation_channels[0], "ramp_down_w_per_s", "allocation.channels[0]")

    headroom = site.get("setpoint_headroom") or {}
    if headroom.get("enabled", True) is not False:
        _require_number(headroom, "headroom_w", "setpoint_headroom")
        _require_number(headroom, "headroom_pct", "setpoint_headroom")
        if "priority" in headroom:
            _require_number(headroom, "priority", "setpoint_headroom")

    compliance = site.get("setpoint_compliance")
    if compliance is not None:
        if not isinstance(compliance, dict):
            raise ValueError("setpoint_compliance must be a mapping")
        for key in ("tolerance_w", "max_violation_s"):
            if key in compliance:
                _require_number(compliance, key, "setpoint_compliance")

    telemetry = site.get("telemetry") or {}
    if not isinstance(telemetry, dict):
        raise ValueError("telemetry must be a mapping")
    if "live_json" in telemetry and not isinstance(telemetry["live_json"], str):
        raise ValueError("telemetry.live_json must be text")

    recording = site.get("recording") or {}
    if not isinstance(recording, dict):
        raise ValueError("recording must be a mapping")
    if "cycle_csv" in recording and not isinstance(recording["cycle_csv"], str):
        raise ValueError("recording.cycle_csv must be text")
    if "channels" in recording and not isinstance(recording["channels"], list):
        raise ValueError("recording.channels must be a list")

    simulation = site.get("simulation") or {}
    if not isinstance(simulation, dict):
        raise ValueError("simulation must be a mapping")
    for key in ("tick_s", "meter_noise_w"):
        if key in simulation:
            _require_number(simulation, key, "simulation")
    for group_key in ("unit", "load"):
        group = simulation.get(group_key) or {}
        if not isinstance(group, dict):
            raise ValueError(f"simulation.{group_key} must be a mapping")
        for key in group:
            _require_number(group, key, f"simulation.{group_key}")

    devices = _require_list(site, "devices", "site")
    if not devices:
        raise ValueError("devices must not be empty")
    ids = {device.get("id") for device in devices if isinstance(device, dict)}
    for required_id in (scenario["connection_point_device_id"], scenario["unit_device_id"]):
        if required_id not in ids:
            raise ValueError(f"scenario references unknown device id: {required_id}")
    for idx, device in enumerate(devices):
        if not isinstance(device, dict):
            raise ValueError(f"devices[{idx}] must be a mapping")
        for key in ("id", "profile", "host"):
            _require_text(device, key, f"devices[{idx}]")
        _require_number(device, "slave_id", f"devices[{idx}]")
        if "port" in device:
            _require_number(device, "port", f"devices[{idx}]")
        for key in ("timeout_s", "retries"):
            if key in device:
                _require_number(device, key, f"devices[{idx}]")

    channels = _device_channels_for_site(site)
    validate_bindings(site, [channel.name for channel in channels])
    validate_safety_allocation(site)
    validate_setpoint_keepalive(site)
    validate_write_age_guard(site)
    hard_cfg = _hard_switch_config(site)
    if hard_cfg:
        if not (site.get("control") or {}).get("command_json"):
            raise ValueError("hard_switch requires control.command_json")
        _validate_hard_switch_channels(hard_cfg, channels)
    return channels


def _json_number(value: float) -> float | None:
    return float(value) if math.isfinite(float(value)) else None


def channel_metadata(channels: list[Channel]) -> list[dict[str, Any]]:
    return [
        {
            "name": channel.name,
            "unit": channel.unit,
            "writable": channel.writable,
            "min_val": _json_number(channel.min_val),
            "max_val": _json_number(channel.max_val),
        }
        for channel in channels
    ]


def _device_name(channel_name: str) -> str:
    return channel_name.split(".", 1)[0] if "." in channel_name else "sys"


# Human-readable meaning of the sys.* status words (device fields get theirs
# from the canonical vocabulary, see field_label in pyems/device_fields.py).
_SYS_LABELS = {
    COMMS_AGE_CHANNEL: "Seconds since the last good bus read (inf = never)",
    WRITE_AGE_CHANNEL: "Seconds since the last good setpoint flush (inf = never)",
    SAFE_MODE_CHANNEL: "Safety trip active (1) / healthy (0)",
    SETPOINT_VIOLATION_CHANNEL: "Unit is not following its applied setpoint",
}


def channel_description(channel_name: str) -> str:
    if (
        channel_name.startswith("sys.")
        and channel_name.endswith(".comms_age_s")
        and channel_name != COMMS_AGE_CHANNEL
    ):
        device_id = channel_name[len("sys."):-len(".comms_age_s")]
        return f"Seconds since the last good read for device '{device_id}'"
    return _SYS_LABELS.get(channel_name) or field_label(channel_name)


def channel_rows(site: dict[str, Any], channels: list[Channel], snapshot: dict[str, float]) -> list[dict[str, Any]]:
    scenario_channels = set(required_channels(site))
    setpoint_channels = {channel["setpoint_channel"] for channel in site["allocation"]["channels"]}
    rows = []
    for channel in channels:
        if channel.name in setpoint_channels:
            role = "active power setpoint"
        elif channel.name in scenario_channels:
            role = "required for scenario"
        elif channel.name.startswith("sys."):
            role = "system"
        else:
            role = "measurement"
        rows.append(
            {
                "device": _device_name(channel.name),
                "channel": channel.name,
                "description": channel_description(channel.name),
                "value": _json_number(snapshot.get(channel.name, channel.value)),
                "unit": channel.unit,
                "access": "write" if channel.writable else "read",
                "role": role,
            }
        )
    return rows


def empty_live_rows(site: dict[str, Any], channels: list[Channel]) -> list[dict[str, Any]]:
    return channel_rows(site, channels, {channel.name: 0.0 for channel in channels})


def _profile_abs(profile_path: str) -> Path:
    path = (PROFILES / profile_path).resolve()
    root = PROFILES.resolve()
    if not str(path).startswith(str(root)):
        raise ValueError("profile path must stay under profiles/")
    return path


def load_profile_yaml(profile_path: str) -> dict[str, Any]:
    path = _profile_abs(profile_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{profile_path} must contain a YAML mapping")
    data.setdefault("default_port", 502)
    data.setdefault("registers", [])
    return data


def _field(channel_name: str) -> str:
    return channel_name.split(".", 1)[1] if "." in channel_name else channel_name


def _profile_channel_for_tag(profile: dict[str, Any], expected_tag: str) -> tuple[int | None, dict[str, Any] | None]:
    field = _field(expected_tag)
    for idx, reg in enumerate(profile.get("registers", [])):
        if _field(str(reg.get("channel", ""))) == field:
            return idx, reg
    return None, None


def device_profile_requirements(site: dict[str, Any]) -> list[dict[str, Any]]:
    site = normalize_site(site)
    scenario = site["scenario"]
    wanted: dict[str, list[tuple[str, str]]] = {
        scenario["connection_point_device_id"]: [
            ("connection point active power", f"{scenario['connection_point_device_id']}.W")
        ],
        scenario["unit_device_id"]: [
            ("unit active power", f"{scenario['unit_device_id']}.W"),
            ("unit active power setpoint", f"{scenario['unit_device_id']}.WSet"),
        ],
    }
    results = []
    for device in site["devices"]:
        profile_path = device["profile"]
        profile = load_profile_yaml(profile_path)
        for label, expected_tag in wanted.get(device["id"], []):
            idx, reg = _profile_channel_for_tag(profile, expected_tag)
            results.append(
                {
                    "device_id": device["id"],
                    "profile": profile_path,
                    "field": label,
                    "expected_tag": expected_tag,
                    "present": reg is not None,
                    "profile_channel": reg.get("channel") if reg else "",
                    "address": reg.get("address") if reg else "",
                    "register_index": idx,
                }
            )
    return results


def profile_payload(site: dict[str, Any], device_id: str) -> dict[str, Any]:
    site = normalize_site(site)
    device = next((d for d in site["devices"] if d["id"] == device_id), None)
    if device is None:
        raise ValueError(f"unknown device id: {device_id}")
    profile = load_profile_yaml(device["profile"])
    requirements = [item for item in device_profile_requirements(site) if item["device_id"] == device_id]
    return {
        "device_id": device_id,
        "profile_path": device["profile"],
        "profile": _json_safe(profile),
        "requirements": requirements,
    }


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    for key in ("model", "protocol"):
        _require_text(profile, key, "profile")
    _require_number(profile, "default_port", "profile")
    registers = _require_list(profile, "registers", "profile")
    for idx, reg in enumerate(registers):
        if not isinstance(reg, dict):
            raise ValueError(f"profile.registers[{idx}] must be a mapping")
        for key in ("channel", "type", "access"):
            _require_text(reg, key, f"profile.registers[{idx}]")
        if "unit" not in reg or not isinstance(reg["unit"], str):
            raise ValueError(f"profile.registers[{idx}].unit must be a string")
        for key in ("address", "scale"):
            _require_number(reg, key, f"profile.registers[{idx}]")
        if reg.get("command") and reg.get("access") != "read_write":
            raise ValueError(
                f"profile.registers[{idx}]: a command register must be "
                f"'read_write' (written one-shot, not continuously)"
            )
    return profile


def save_profile_yaml(profile_path: str, profile: dict[str, Any]) -> None:
    profile = validate_profile(profile)
    _profile_abs(profile_path).write_text(
        yaml.safe_dump(profile, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def config_payload(site_path: str | Path = DEFAULT_SITE) -> dict[str, Any]:
    site = load_site(site_path)
    try:
        channels = validate_site_for_ui(site)
        validation = {"ok": True, "error": ""}
    except Exception as exc:
        channels = []
        validation = {"ok": False, "error": str(exc)}
    return {
        "site": site,
        "profiles": list_profiles(),
        "available_channels": channel_metadata(channels),
        "profile_requirements": device_profile_requirements(site),
        "live_rows": empty_live_rows(site, channels),
        "validation": validation,
        # canonical vocabulary decode: field -> human label (profile editor
        # shows it next to the raw channel name)
        "field_labels": FIELD_LABELS,
    }


def app_config_payload(app: "UIApp") -> dict[str, Any]:
    """config_payload plus which site file is being edited (and the choices)."""
    payload = config_payload(app.site_path)
    payload["site_path"] = str(app.site_path)
    payload["site_choices"] = [str(path) for path in app.site_choices]
    payload["sim_site_path"] = str(app.sim.sim_site_path)
    return payload


def save_site(site: dict[str, Any], site_path: str | Path = DEFAULT_SITE) -> dict[str, Any]:
    site = normalize_site(site)
    validate_site_for_ui(site)
    path = Path(site_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(site, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return site


def _build_read_only_driver_state(site: dict[str, Any]) -> tuple[CompositeDriver, SystemState, list[Channel]]:
    channels = validate_site_for_ui(site)
    drivers = build_device_drivers(site["devices"])
    driver = CompositeDriver(drivers)
    state = SystemState(channels)
    return driver, state, channels


class ReadOnlyDeviceSession:
    def __init__(self, site: dict[str, Any]) -> None:
        self._site = normalize_site(site)
        self._driver, self._state, self._channels = _build_read_only_driver_state(self._site)
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._driver.connect()
            self._started = True

    def read_once(self) -> dict[str, Any]:
        with self._lock:
            if not self._started:
                self._driver.connect()
                self._started = True
            started_at = time.monotonic()
            self._driver.read_state(self._state)
            read_s = time.monotonic() - started_at
            self._state.apply_driver_value(COMMS_AGE_CHANNEL, 0.0)
            snapshot = self._state.snapshot()
        return {
            "ok": True,
            "read_s": read_s,
            "read_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rows": channel_rows(self._site, self._channels, snapshot),
        }

    def close(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._driver.disconnect()
            self._started = False


def test_read_once(site: dict[str, Any]) -> dict[str, Any]:
    session = ReadOnlyDeviceSession(site)
    try:
        session.start()
        return session.read_once()
    finally:
        session.close()


def fast_loop_state_path(site: dict[str, Any]) -> Path:
    """Resolve the live-state snapshot path from the site's `telemetry:` section
    (default logs/live_state.json) — the file the production EMS rewrites every
    cycle. Relative paths resolve against the repo root, as in build_publisher."""
    telemetry = site.get("telemetry") or {}
    json_path = telemetry.get("live_json") or "logs/live_state.json"
    path = Path(json_path)
    if not path.is_absolute():
        path = ROOT / path
    return path


def _fast_loop_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build display rows straight from the published snapshot — no device
    drivers, no bus. Role/access come from the channel metadata the EMS wrote."""
    values = data.get("values") or {}
    meta = {item["name"]: item for item in (data.get("channels") or [])}
    rows = []
    for name in sorted(values):
        item = meta.get(name, {})
        writable = bool(item.get("writable"))
        if name.startswith("sys."):
            role = "system"
        elif writable:
            role = "active power setpoint"
        else:
            role = "measurement"
        value = values[name]
        rows.append(
            {
                "device": _device_name(name),
                "channel": name,
                "description": channel_description(name),
                "value": _json_number(value) if isinstance(value, (int, float)) else None,
                "unit": item.get("unit", ""),
                "access": "write" if writable else "read",
                "role": role,
            }
        )
    return rows


def fast_loop_state(site: dict[str, Any]) -> dict[str, Any]:
    """Read the snapshot the running EMS publishes each cycle (read-only).

    The realtime view's primary source: it reflects the values the control loop
    actually acted on this cycle, with NO second Modbus session competing for
    the bus. Returns a clean error (not an exception) when the EMS is not
    running or telemetry is disabled, so the UI can say so plainly.
    """
    path = fast_loop_state_path(site)
    if not path.exists():
        return {
            "ok": False,
            "error": "pyems service not running or telemetry disabled",
            "path": str(path),
            "rows": [],
        }
    try:
        age_s = max(0.0, time.time() - path.stat().st_mtime)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "error": f"could not read telemetry snapshot: {exc}",
            "path": str(path),
            "rows": [],
        }
    return {
        "ok": True,
        "path": str(path),
        "read_at": data.get("timestamp"),
        # File freshness measured server-side (mtime vs wall clock), so the
        # browser can flag a stale snapshot without comparing clock strings.
        "age_s": round(age_s, 3),
        "monotonic_s": data.get("monotonic_s"),
        "cycle_s": data.get("cycle_s"),
        "cycle_overrun": data.get("cycle_overrun"),
        # Raw tag → value map as published, for tag-addressed consumers (the
        # Overview cards); `rows` below is the same data shaped for tables.
        "values": data.get("values") or {},
        "rows": _fast_loop_rows(data),
    }


def telemetry_fresh(fls: dict[str, Any]) -> bool:
    """Is the published snapshot recent enough to mean the EMS is running now?

    Mirrors the browser's `telemetryStale()`: fresh when the file age is within
    max(5s, 3 cycles). A stale-but-parseable file (EMS stopped) is NOT fresh, so
    this also tells a leftover snapshot apart from a live one."""
    if not fls.get("ok"):
        return False
    age_s = fls.get("age_s")
    cycle_s = fls.get("cycle_s") or 1.0
    return isinstance(age_s, (int, float)) and age_s <= max(5.0, 3.0 * cycle_s)


def command_file_path(site: dict[str, Any]) -> Path | None:
    """Resolve the operator command file from `control.command_json` (relative
    paths resolve against the repo root, as in build_ems). None when the site
    has no generation gate configured."""
    cmd_json = (site.get("control") or {}).get("command_json")
    if not cmd_json:
        return None
    path = Path(cmd_json)
    return path if path.is_absolute() else ROOT / path


def generation_state(site: dict[str, Any]) -> dict[str, Any]:
    """Current Operation state for the UI: the soft generation gate AND the hard
    inverter switch — what the EMS reports (snapshot) plus what the UI last wrote
    to the command file."""
    path = command_file_path(site)
    if path is None:
        return {"ok": False, "error": "generation gate not configured (control.command_json)"}
    fls = fast_loop_state(site)
    values = fls.get("values") or {}
    command = read_command_file(path)
    return {
        "ok": True,
        "configured": True,
        "ems_running": telemetry_fresh(fls),
        # EMS-reported (authoritative) state; null when the EMS is not publishing.
        "allowed": values.get(GENERATION_ALLOWED_CHANNEL),
        "gate_active": values.get(GENERATION_GATE_ACTIVE_CHANNEL),
        "command_age_s": values.get(COMMAND_AGE_CHANNEL),
        # hard inverter switch (separate level: de-energizes, not just curtails).
        "hard_switch": bool(site.get("hard_switch")),
        # what the EMS last COMMANDED (1 started / 0 stopped / null never).
        "inverter_run_state": values.get(INVERTER_RUN_STATE_CHANNEL),
        # what the UI last wrote (may differ from EMS until the next cycle, or
        # be ignored by the EMS as stale/leftover — fail-closed).
        "command_file": command,
        "path": str(path),
    }


class EmsManager:
    """Start/stop the EMS control loop (`pyems`) as a child of the configuration UI.

    Symmetric to SimManager. The EMS is a SEPARATE process — the UI never runs
    the control loop in-process; it only spawns/terminates it and reports whether
    a control loop is publishing telemetry (which also detects an EMS started by
    systemd or by hand outside the UI).

    `process_control` gates spawning/terminating: in plain production the UI is
    a read-only status + generation console (`process_control=False`), and only
    a managed/dev launch (`--manage-ems`) may start or stop the process. Either
    way the UI never starts a SECOND EMS: start refuses while telemetry is fresh.
    """

    def __init__(
        self,
        site_path: str | Path,
        process_control: bool = False,
        log_level: str | None = None,
    ) -> None:
        self.site_path = Path(site_path)
        self.process_control = process_control
        self._log_level = log_level
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def _site(self) -> dict[str, Any]:
        return load_site(self.site_path)

    def _managed_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def running(self) -> bool:
        """A control loop is publishing fresh telemetry (managed or external)."""
        return telemetry_fresh(fast_loop_state(self._site()))

    def status(self) -> dict[str, Any]:
        with self._lock:
            managed = self._managed_alive()
        fls = fast_loop_state(self._site())
        running = telemetry_fresh(fls)
        return {
            "ok": True,
            "managed": managed,
            "running": running,
            "external": running and not managed,
            "process_control": self.process_control,
            "site": str(self.site_path),
            "age_s": fls.get("age_s"),
            "command": f"pyems --site {self.site_path}",
        }

    def start(self, wait_s: float = 20.0) -> dict[str, Any]:
        if not self.process_control:
            raise ValueError(
                "EMS process control is disabled in this UI; start the EMS where "
                "it is supervised (e.g. systemctl start pyems), or run the UI "
                "with --manage-ems"
            )
        if self.running():
            raise ValueError(
                "an EMS is already running (fresh telemetry); the UI will not "
                "start a second control loop"
            )
        logger.info("Operator command: START EMS (%s)", self.site_path)
        with self._lock:
            if not self._managed_alive():
                cmd = [sys.executable, "-m", "pyems.ems", "--site", str(self.site_path)]
                if self._log_level:
                    cmd += ["--log-level", self._log_level]
                self._proc = subprocess.Popen(cmd)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.running():
                return self.status()
            with self._lock:
                if self._proc is not None and self._proc.poll() is not None:
                    code = self._proc.returncode
                    self._proc = None
                    raise ValueError(
                        f"EMS exited immediately (code {code}) — likely a bad "
                        f"{self.site_path}, a bus/connect error, or telemetry "
                        f"disabled; see the pyems-ui console for its log"
                    )
            time.sleep(0.2)
        # Process is alive but no fresh telemetry yet (e.g. telemetry section
        # absent). Report status rather than killing a possibly-healthy loop.
        return self.status()

    def stop_managed(self) -> None:
        """Terminate the child if we own one; never raises (shutdown path)."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()  # POSIX SIGTERM → clean scheduler shutdown handler
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)

    def stop(self) -> dict[str, Any]:
        if not self.process_control:
            raise ValueError(
                "EMS process control is disabled in this UI; stop the EMS where "
                "it is supervised (e.g. systemctl stop pyems)"
            )
        with self._lock:
            external = not self._managed_alive()
        if external and self.running():
            raise ValueError(
                "the EMS was started outside this UI; stop it where it was "
                "started (Ctrl-C in its terminal, or systemctl stop pyems)"
            )
        logger.info("Operator command: STOP EMS (%s)", self.site_path)
        self.stop_managed()
        return self.status()


class SimManager:
    """Start/stop the device simulator (`pyems-sim`) from the configuration UI.

    The simulator stays a separate PROCESS — the EMS under test must connect
    to it over real Modbus TCP exactly as it would to field hardware. This
    class only manages that process and reports whether its control panel is
    reachable (it also detects a simulator started by hand outside the UI).
    """

    def __init__(
        self,
        sim_site_path: str | Path = DEFAULT_SIM_SITE,
        panel_host: str = "127.0.0.1",
        panel_port: int = 8766,
    ) -> None:
        self.sim_site_path = Path(sim_site_path)
        self.panel_host = panel_host
        self.panel_port = panel_port
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def _probe_host(self) -> str:
        # the panel may be bound to a wildcard address; probe via loopback then
        return "127.0.0.1" if self.panel_host in ("0.0.0.0", "::") else self.panel_host

    def reachable(self, timeout_s: float = 1.0) -> bool:
        url = f"http://{self._probe_host()}:{self.panel_port}/api/state"
        try:
            with urllib.request.urlopen(url, timeout=timeout_s):
                return True
        except Exception:
            return False

    def _managed_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict[str, Any]:
        with self._lock:
            managed = self._managed_alive()
        return {
            "ok": True,
            "managed": managed,
            "reachable": self.reachable(),
            "panel_port": self.panel_port,
            "sim_site": str(self.sim_site_path),
            "ems_command": f"pyems --site {self.sim_site_path}",
        }

    def start(self, wait_s: float = 10.0) -> dict[str, Any]:
        if not self.sim_site_path.exists():
            raise ValueError(f"simulator site file not found: {self.sim_site_path}")
        logger.info("Operator command: START simulator (%s)", self.sim_site_path)
        with self._lock:
            if not self._managed_alive() and not self.reachable():
                self._proc = subprocess.Popen(
                    [
                        sys.executable, "-m", "pyems.sim.harness",
                        "--site", str(self.sim_site_path),
                        "--ui-host", self.panel_host,
                        "--ui-port", str(self.panel_port),
                    ],
                )
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.reachable(timeout_s=0.5):
                return self.status()
            with self._lock:
                if self._proc is not None and self._proc.poll() is not None:
                    code = self._proc.returncode
                    self._proc = None
                    raise ValueError(
                        f"simulator exited immediately (code {code}) — likely a "
                        f"port conflict or a bad {self.sim_site_path}; see the "
                        f"pyems-ui console for its log"
                    )
            time.sleep(0.2)
        raise ValueError("simulator did not come up in time")

    def stop_managed(self) -> None:
        """Terminate the child process if we own one; never raises (shutdown path)."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            external = self._proc is None
        if external and self.reachable():
            raise ValueError(
                "the simulator was started outside pyems-ui; stop it where it "
                "was started (Ctrl-C in its terminal)"
            )
        logger.info("Operator command: STOP simulator (%s)", self.sim_site_path)
        self.stop_managed()
        return self.status()


class UIApp:
    def __init__(
        self,
        site_path: str | Path,
        sim: SimManager | None = None,
        ems: EmsManager | None = None,
    ) -> None:
        self.site_path = Path(site_path)
        self.sim = sim or SimManager()
        self.ems = ems or EmsManager(self.site_path)
        # The UI can edit either the hardware site or the simulation site.
        # They are SEPARATE configs on purpose (real device addresses vs
        # localhost simulators) — but editing one while testing against the
        # other is a silent trap, so the choice must be explicit and visible.
        self.site_choices: list[Path] = [self.site_path]
        sim_site = self.sim.sim_site_path
        if sim_site.exists() and sim_site.resolve() != self.site_path.resolve():
            self.site_choices.append(sim_site)
        self._lock = threading.Lock()
        self._session: ReadOnlyDeviceSession | None = None
        self._error_log: list[dict[str, Any]] = []
        self._next_error_id = 1

    def generation_status(self) -> dict[str, Any]:
        return generation_state(load_site(self.site_path))

    def set_generation(self, enabled: bool) -> dict[str, Any]:
        """Write the operator command file; the running EMS honors it next cycle
        (fail-closed: a stale/leftover enable is ignored — see pyems.commands)."""
        site = load_site(self.site_path)
        path = command_file_path(site)
        if path is None:
            raise ValueError(
                "generation gate not configured for this site "
                "(set control.command_json in site.yaml)"
            )
        write_command_file(path, generation_enabled=enabled)
        return generation_state(site)

    def set_inverter_command(self, action: str) -> dict[str, Any]:
        """Issue a latched hard start/stop (writes the device command register).

        Distinct from generation (soft curtail): this de-energizes/energizes the
        inverter. The EMS fires it once on the new id (see HardSwitchController)."""
        site = load_site(self.site_path)
        if not site.get("hard_switch"):
            raise ValueError(
                "hard inverter switch not configured for this site "
                "(add a hard_switch: section to site.yaml)"
            )
        path = command_file_path(site)
        if path is None:
            raise ValueError("hard_switch requires control.command_json in site.yaml")
        write_inverter_command(path, action=action)
        return generation_state(site)

    def set_site_file(self, path_str: str) -> dict[str, Any]:
        """Switch which site file the whole UI edits (must be a known choice)."""
        target = next((p for p in self.site_choices if str(p) == str(path_str)), None)
        if target is None:
            raise ValueError(
                f"unknown site file {path_str!r}; "
                f"choices: {[str(p) for p in self.site_choices]}"
            )
        self.close()  # the live session is bound to the previous file's devices
        self.site_path = target
        self.ems.site_path = target  # EMS status/command channel follows the file
        return {"ok": True, "site_path": str(target)}

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None

    def save(self, site: dict[str, Any]) -> dict[str, Any]:
        saved = save_site(site, self.site_path)
        self.close()
        return saved

    def record_error(self, source: str, exc: Exception) -> dict[str, Any]:
        message = str(exc) or exc.__class__.__name__
        with self._lock:
            entry = {
                "id": self._next_error_id,
                "logged_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "level": "error",
                "source": source.strip() or "ui",
                "message": message,
            }
            self._next_error_id += 1
            self._error_log.append(entry)
            self._error_log = self._error_log[-MAX_ERROR_LOG_ENTRIES:]
            return dict(entry)

    def error_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in reversed(self._error_log)]

    def clear_error_log(self) -> dict[str, Any]:
        with self._lock:
            self._error_log.clear()
        return {"ok": True, "entries": []}

    def start_live(self) -> dict[str, Any]:
        with self._lock:
            if self._session is None:
                self._session = ReadOnlyDeviceSession(load_site(self.site_path))
            self._session.start()
            return {"ok": True}

    def read_live(self) -> dict[str, Any]:
        with self._lock:
            if self._session is None:
                self._session = ReadOnlyDeviceSession(load_site(self.site_path))
            session = self._session
        return session.read_once()

    def stop_live(self) -> dict[str, Any]:
        self.close()
        return {"ok": True}


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw or "{}")


def _json_safe(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: _json_safe(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_json_safe(value) for value in data]
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    return data


def _static_path(request_path: str) -> Path:
    if request_path == "/":
        relative = "index.html"
    elif request_path.startswith("/static/"):
        relative = unquote(request_path[len("/static/") :])
    else:
        raise FileNotFoundError(request_path)
    path = (STATIC_ROOT / relative).resolve()
    root = STATIC_ROOT.resolve()
    if not str(path).startswith(str(root)):
        raise FileNotFoundError(request_path)
    return path


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """HTTPServer that keeps browser disconnects out of the operator log."""

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            logger.debug("UI client disconnected: %s", client_address)
            return
        super().handle_error(request, client_address)


def make_handler(app: UIApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
            body = json.dumps(_json_safe(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, request_path: str) -> None:
            path = _static_path(request_path)
            if not path.exists() or not path.is_file():
                raise FileNotFoundError(request_path)
            body = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix == ".js":
                content_type = "text/javascript"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, exc: Exception, status: int = HTTPStatus.BAD_REQUEST) -> None:
            path = urlparse(self.path).path
            entry = app.record_error(f"{self.command} {path}", exc)
            self._send_json({"ok": False, "error": str(exc), "error_entry": entry}, status)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path == "/" or path.startswith("/static/"):
                    self._send_static(path)
                elif path == "/api/config":
                    self._send_json(app_config_payload(app))
                elif path == "/api/profile":
                    device_id = query.get("device_id", [""])[0]
                    self._send_json(profile_payload(load_site(app.site_path), device_id))
                elif path == "/api/error-log":
                    self._send_json({"ok": True, "entries": app.error_log()})
                elif path == "/api/live":
                    self._send_json(app.read_live())
                elif path == "/api/fast-loop-state":
                    self._send_json(fast_loop_state(load_site(app.site_path)))
                elif path == "/api/sim/status":
                    self._send_json(app.sim.status())
                elif path == "/api/ems/status":
                    self._send_json(app.ems.status())
                elif path == "/api/generation":
                    self._send_json(app.generation_status())
                else:
                    self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            except FileNotFoundError:
                self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_error_json(exc)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                payload = _read_json(self)
                if path == "/api/config":
                    app.save(payload.get("site", payload))
                    self._send_json(app_config_payload(app))
                elif path == "/api/site-file":
                    self._send_json(app.set_site_file(payload.get("path", "")))
                elif path == "/api/profile":
                    save_profile_yaml(payload["profile_path"], payload["profile"])
                    self._send_json(profile_payload(load_site(app.site_path), payload["device_id"]))
                elif path == "/api/test-read":
                    self._send_json(test_read_once(load_site(app.site_path)))
                elif path == "/api/live/start":
                    self._send_json(app.start_live())
                elif path == "/api/live/stop":
                    self._send_json(app.stop_live())
                elif path == "/api/error-log/clear":
                    self._send_json(app.clear_error_log())
                elif path == "/api/sim/start":
                    self._send_json(app.sim.start())
                elif path == "/api/sim/stop":
                    self._send_json(app.sim.stop())
                elif path == "/api/ems/start":
                    self._send_json(app.ems.start())
                elif path == "/api/ems/stop":
                    self._send_json(app.ems.stop())
                elif path == "/api/generation/start":
                    self._send_json(app.set_generation(True))
                elif path == "/api/generation/stop":
                    self._send_json(app.set_generation(False))
                elif path == "/api/inverter/start":
                    self._send_json(app.set_inverter_command("start"))
                elif path == "/api/inverter/stop":
                    self._send_json(app.set_inverter_command("stop"))
                else:
                    self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_error_json(exc)

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the local PyEMS configuration UI.")
    parser.add_argument("--site", default=str(DEFAULT_SITE), help="Path to site.yaml")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", default=8765, type=int, help="Bind port")
    parser.add_argument(
        "--sim-site", default=str(DEFAULT_SIM_SITE),
        help=f"site yaml for the device simulator (default: {DEFAULT_SIM_SITE})",
    )
    parser.add_argument(
        "--sim-port", default=8766, type=int,
        help="port for the simulator control panel (default: 8766)",
    )
    parser.add_argument(
        "--manage-ems", action="store_true",
        help="allow starting/stopping the EMS control loop from the UI "
             "(off by default: the UI is a read-only status + generation console)",
    )
    parser.add_argument(
        "--autostart-ems", action="store_true",
        help="start the EMS control loop on launch (implies --manage-ems); "
             "generation stays disabled until enabled from the UI",
    )
    parser.add_argument(
        "--start-sim", action="store_true",
        help="start the device simulator on launch (local testing)",
    )
    parser.add_argument("--log-level", default=None, help="log level for a managed EMS")
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    manage_ems = args.manage_ems or args.autostart_ems
    sim = SimManager(args.sim_site, panel_host=args.host, panel_port=args.sim_port)
    ems = EmsManager(args.site, process_control=manage_ems, log_level=args.log_level)
    app = UIApp(args.site, sim=sim, ems=ems)
    server = QuietThreadingHTTPServer((args.host, args.port), make_handler(app))
    host, port = server.server_address[:2]
    print(f"PyEMS UI running at http://{host}:{port}")

    if args.start_sim:
        try:
            app.sim.start()
            print(f"Device simulator running at http://{host}:{args.sim_port}")
        except Exception as exc:  # a sim failure must not stop the UI from serving
            print(f"WARNING: could not start the simulator: {exc}")
    if args.autostart_ems:
        try:
            app.ems.start()
            print(f"EMS control loop started (generation disabled): {app.ems.site_path}")
        except Exception as exc:
            print(f"WARNING: could not start the EMS: {exc}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        app.ems.stop_managed()  # an EMS we spawned dies with the UI
        app.sim.stop_managed()  # a simulator we spawned dies with the UI
        server.server_close()


def main_dev(argv: list[str] | None = None) -> None:
    """Local all-in-one entry point (`pyems-dev`): bring up the simulator, the
    EMS (against the simulation site) and the UI with one command, EMS managed
    and autostarted. Generation stays DISABLED until enabled from the web UI."""
    preset = [
        "--site", str(DEFAULT_SIM_SITE),
        "--start-sim",
        "--autostart-ems",
    ]
    main(preset + list(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    main()
