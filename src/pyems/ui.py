"""Local web API and static file server for PyEMS configuration UI."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import threading
import time
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from pyems.channels import Channel, SystemState
from pyems.controllers.safety import SAFE_MODE_CHANNEL
from pyems.drivers.cached import COMMS_AGE_CHANNEL
from pyems.drivers.composite import CompositeDriver
from pyems.ems import (
    DEFAULT_SITE,
    IMPORT_LIMIT_MODE,
    PROFILES,
    build_device_drivers,
    required_channels,
    validate_bindings,
)


DEFAULT_SITE_TEMPLATE = DEFAULT_SITE.parent / "default_site.yaml"
STATIC_ROOT = Path(__file__).with_name("ui_static")
AUTO_PID_GAINS = {"kp": 0.4, "ki": 0.08, "kd": 0.0, "tt": 5.0}
VERY_HIGH_IMPORT_LIMIT_W = 1_000_000_000.0
MAX_ERROR_LOG_ENTRIES = 100


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


def _system_channels() -> list[Channel]:
    return [
        Channel(COMMS_AGE_CHANNEL, unit="s", value=0.0),
        Channel(SAFE_MODE_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
    ]


def _device_channels_for_site(site: dict[str, Any]) -> list[Channel]:
    drivers = build_device_drivers(site["devices"])
    return CompositeDriver(drivers).channels() + _system_channels()


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

    safety = _require_mapping(site, "safety", "site")
    _require_number(safety, "max_comms_age_s", "safety")
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

    channels = _device_channels_for_site(site)
    validate_bindings(site, [channel.name for channel in channels])
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
    }


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
            self._state._channels[COMMS_AGE_CHANNEL].value = 0.0
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


class UIApp:
    def __init__(self, site_path: str | Path) -> None:
        self.site_path = Path(site_path)
        self._lock = threading.Lock()
        self._session: ReadOnlyDeviceSession | None = None
        self._error_log: list[dict[str, Any]] = []
        self._next_error_id = 1

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
                    self._send_json(config_payload(app.site_path))
                elif path == "/api/profile":
                    device_id = query.get("device_id", [""])[0]
                    self._send_json(profile_payload(load_site(app.site_path), device_id))
                elif path == "/api/error-log":
                    self._send_json({"ok": True, "entries": app.error_log()})
                elif path == "/api/live":
                    self._send_json(app.read_live())
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
                    self._send_json(config_payload(app.site_path))
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
    args = parser.parse_args(argv)

    app = UIApp(args.site)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    host, port = server.server_address[:2]
    print(f"PyEMS UI running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()


if __name__ == "__main__":
    main()
