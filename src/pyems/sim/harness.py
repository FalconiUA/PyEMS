"""Simulation harness: simulated devices + plant physics + web control panel.

Run it next to the real, unmodified EMS:

    terminal 1:  pyems-sim                      # devices + control UI
    terminal 2:  pyems --site config/site.sim.yaml

Then open http://127.0.0.1:8766 to drive PV generation / site load (manual
value, synthetic curve, or replay of recorded 1-second CSV data) and to inject
faults (device offline, frozen registers, Modbus exceptions, rejected writes,
setpoint ignored) while watching how the EMS reacts.

The harness reads the SAME site.yaml as the EMS: device list, profiles, ports
and the controlled-unit envelope all come from there. Sim-only knobs live in
an optional `simulation:` section that the EMS ignores.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import mimetypes
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from pyems.drivers.modbus_device import DeviceProfile
from pyems.ems import PROFILES, ROOT, control_mode
from pyems.logging import setup_logging
from pyems.sim.device import FAULTS, SimulatedDevice
from pyems.sim.plant import SimWorld, meter_register_fields, unit_register_fields
from pyems.sim.sources import (
    ManualSource,
    ReplaySource,
    SourceBox,
    SyntheticSource,
    parse_csv_series,
)

logger = logging.getLogger(__name__)

DEFAULT_SIM_SITE = ROOT / "config" / "site.sim.yaml"
STATIC_ROOT = Path(__file__).with_name("static")
HISTORY_POINTS = 1200          # at one point per tick decimated to 0.5 s = 10 min
MAX_EVENTS = 200
UNIT_FAULTS = FAULTS + ("ignore_setpoint",)

# History keys, in the order the UI receives them.
HISTORY_KEYS = (
    "t_s",
    "unit_available_w",
    "unit_active_power_w",
    "unit_active_power_setpoint_w",
    "load_w",
    "connection_point_w",
)


def _default_unit_envelope(site: dict) -> tuple[float, float]:
    ch = site["allocation"]["channels"][0]
    return float(ch["p_min_w"]), float(ch["p_max_w"])


class SimHarness:
    """Wires sources -> world -> simulated devices and ticks them."""

    def __init__(self, site: dict, site_path: str | Path | None = None) -> None:
        self.site = site
        self.site_path = None if site_path is None else str(site_path)
        sim_cfg = site.get("simulation", {})
        scenario = site.get("scenario", {})
        self.unit_device_id = scenario.get("unit_device_id", "pv")
        self.cp_device_id = scenario.get("connection_point_device_id", "grid")
        _p_min, p_max = _default_unit_envelope(site)
        self.unit_p_max_w = float(sim_cfg.get("unit_p_max_w", p_max))

        unit_cfg = sim_cfg.get("unit", {})
        load_cfg = sim_cfg.get("load", {})
        self.tick_s = float(sim_cfg.get("tick_s", 0.2))

        self.unit_available = SourceBox(
            "unit_available",
            SyntheticSource(
                base_w=0.0,
                amplitude_w=float(unit_cfg.get("peak_w", self.unit_p_max_w)),
                period_s=float(unit_cfg.get("period_s", 600.0)),
                noise_w=float(unit_cfg.get("noise_w", self.unit_p_max_w * 0.005)),
            ),
        )
        self.load = SourceBox(
            "load",
            SyntheticSource(
                base_w=float(load_cfg.get("base_w", self.unit_p_max_w * 0.3)),
                amplitude_w=float(load_cfg.get("amplitude_w", self.unit_p_max_w * 0.1)),
                period_s=float(load_cfg.get("period_s", 900.0)),
                noise_w=float(load_cfg.get("noise_w", 100.0)),
            ),
        )
        self.world = SimWorld(
            self.unit_available,
            self.load,
            unit_p_max_w=self.unit_p_max_w,
            unit_tau_s=float(unit_cfg.get("tau_s", 2.0)),
            meter_noise_w=float(sim_cfg.get("meter_noise_w", 25.0)),
        )

        self.devices: dict[str, SimulatedDevice] = {}
        for dev in site["devices"]:
            profile = DeviceProfile.load(PROFILES / dev["profile"])
            if profile.protocol != "modbus_tcp":
                raise ValueError(
                    f"sim device '{dev['id']}' uses {profile.protocol}; the sim "
                    f"serves modbus_tcp only — point the profile/site at TCP"
                )
            on_setpoint = self._on_unit_setpoint if dev["id"] == self.unit_device_id else None
            self.devices[dev["id"]] = SimulatedDevice(
                device_id=dev["id"],
                profile=profile,
                host=dev["host"],
                port=int(dev.get("port", profile.default_port)),
                slave_id=int(dev["slave_id"]),
                on_setpoint=on_setpoint,
            )
        if self.unit_device_id not in self.devices:
            raise ValueError(f"scenario unit device '{self.unit_device_id}' not in devices")
        if self.cp_device_id not in self.devices:
            raise ValueError(f"scenario meter device '{self.cp_device_id}' not in devices")

        self._lock = threading.Lock()
        self._history: deque[tuple[float, ...]] = deque(maxlen=HISTORY_POINTS)
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._last_history_t = float("-inf")
        self._rng = __import__("random").Random()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sim-world", daemon=True)
        self._t0 = time.monotonic()

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        for dev in self.devices.values():
            dev.start()
        self._thread.start()
        self.log_event("simulation started")

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        for dev in self.devices.values():
            dev.stop()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick(started - self._t0)
            except Exception:
                logger.exception("sim world tick failed")
            self._stop.wait(max(0.0, self.tick_s - (time.monotonic() - started)))

    def tick(self, now_s: float) -> dict[str, float]:
        snap = self.world.tick(now_s)
        self.devices[self.unit_device_id].set_fields(
            unit_register_fields(snap, self._rng)
        )
        self.devices[self.cp_device_id].set_fields(
            meter_register_fields(snap, self._rng)
        )
        with self._lock:
            if now_s - self._last_history_t >= 0.5:
                self._history.append(tuple(snap[k] for k in HISTORY_KEYS))
                self._last_history_t = now_s
        return snap

    def _on_unit_setpoint(self, field: str, value_w: float) -> None:
        if field == "WSet":
            self.world.set_unit_active_power_setpoint_w(value_w)
        elif field == "StartCmd":
            self.world.set_unit_enabled(True)
            self.log_event(f"unit hard START (StartCmd={value_w:g})")
        elif field == "StopCmd":
            self.world.set_unit_enabled(False)
            self.log_event(f"unit hard STOP (StopCmd={value_w:g})")
        elif field == "RunStop":
            # Hard remote switch: 1 = start (energize), 0 = stop (de-energize).
            self.world.set_unit_enabled(value_w >= 0.5)
            self.log_event(f"unit hard {'START' if value_w >= 0.5 else 'STOP'} (RunStop={value_w:g})")

    # ── UI-facing state/control ───────────────────────────────────────────────
    def log_event(self, message: str) -> None:
        with self._lock:
            self._events.append(
                {"at": time.strftime("%H:%M:%S"), "message": message}
            )
        logger.info("sim event: %s", message)

    def set_source(self, target: str, payload: dict[str, Any]) -> None:
        box = {"pv": self.unit_available, "load": self.load}.get(target)
        if box is None:
            raise ValueError(f"unknown source target {target!r}; use 'pv' or 'load'")
        mode = payload.get("mode")
        if mode == "manual":
            value = float(payload["value_w"])
            if not math.isfinite(value):
                raise ValueError("value_w must be finite")
            box.set_source(ManualSource(value))
            self.log_event(f"{target}: manual {value:.0f} W")
        elif mode == "synthetic":
            box.set_source(
                SyntheticSource(
                    base_w=float(payload.get("base_w", 0.0)),
                    amplitude_w=float(payload.get("amplitude_w", self.unit_p_max_w)),
                    period_s=float(payload.get("period_s", 600.0)),
                    noise_w=float(payload.get("noise_w", 0.0)),
                )
            )
            self.log_event(f"{target}: synthetic curve")
        elif mode == "replay":
            samples = parse_csv_series(payload.get("csv", ""))
            if not samples:
                raise ValueError("replay CSV contains no numeric samples")
            speed = float(payload.get("speed", 1.0))
            loop = bool(payload.get("loop", True))
            box.set_source(ReplaySource(samples, speed=speed, loop=loop))
            self.log_event(
                f"{target}: replay of {len(samples)} samples at x{speed:g}"
                f"{' (loop)' if loop else ''}"
            )
        else:
            raise ValueError(f"unknown source mode {mode!r}")

    def set_fault(self, device_id: str, fault: str, active: bool) -> None:
        if device_id not in self.devices:
            raise ValueError(f"unknown device {device_id!r}")
        if fault == "ignore_setpoint":
            if device_id != self.unit_device_id:
                raise ValueError("ignore_setpoint applies to the generating unit only")
            self.world.set_ignore_setpoint(active)
        else:
            self.devices[device_id].set_fault(fault, active)
        self.log_event(
            f"{device_id}: fault {fault} {'ACTIVE' if active else 'cleared'}"
        )

    def _fault_state(self, device_id: str) -> dict[str, bool]:
        faults = self.devices[device_id].faults()
        if device_id == self.unit_device_id:
            faults["ignore_setpoint"] = self.world.unit.ignore_setpoint
        return faults

    def state(self, since_s: float = float("-inf")) -> dict[str, Any]:
        snap = self.world.snapshot()
        with self._lock:
            history = [p for p in self._history if p[0] > since_s]
            events = list(self._events)
        scenario = self.site.get("scenario", {})
        return {
            "now_s": time.monotonic() - self._t0,
            "snapshot": snap,
            "history_keys": list(HISTORY_KEYS),
            "history": history,
            "sources": {
                "pv": self.unit_available.describe(),
                "load": self.load.describe(),
            },
            "devices": [
                {
                    "id": dev_id,
                    "role": (
                        "unit" if dev_id == self.unit_device_id
                        else "connection_point" if dev_id == self.cp_device_id
                        else "other"
                    ),
                    "endpoint": f"{dev.host}:{dev.port}",
                    "model": dev.profile.model,
                    "online": dev.online(),
                    "faults": self._fault_state(dev_id),
                    **dev.link_age_s(),
                }
                for dev_id, dev in self.devices.items()
            ],
            "ems_command": (
                f"pyems --site {self.site_path}" if self.site_path else "pyems --site <site.sim.yaml>"
            ),
            "scenario": {
                "control_mode": control_mode(self.site),
                "active_power_limit_w": scenario.get("active_power_limit_w"),
                "unit_p_max_w": self.unit_p_max_w,
            },
            "events": list(reversed(events)),
        }


# ── web control panel ─────────────────────────────────────────────────────────
def _json_safe(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: _json_safe(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_json_safe(v) for v in data]
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    return data


def make_handler(harness: SimHarness) -> type[BaseHTTPRequestHandler]:
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
            relative = "index.html" if request_path == "/" else unquote(request_path.lstrip("/"))
            path = (STATIC_ROOT / relative).resolve()
            if not str(path).startswith(str(STATIC_ROOT.resolve())) or not path.is_file():
                raise FileNotFoundError(request_path)
            body = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/state":
                    since = float(parse_qs(parsed.query).get("since", ["-inf"])[0])
                    self._send_json(harness.state(since_s=since))
                else:
                    self._send_static(parsed.path)
            except FileNotFoundError:
                self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if path == "/api/source":
                    harness.set_source(payload.get("target", ""), payload)
                    self._send_json({"ok": True})
                elif path == "/api/fault":
                    harness.set_fault(
                        payload.get("device", ""),
                        payload.get("fault", ""),
                        bool(payload.get("active", False)),
                    )
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="pyems-sim",
        description="Simulated Modbus devices + web control panel for PyEMS.",
    )
    parser.add_argument(
        "--site", type=Path, default=DEFAULT_SIM_SITE,
        help=f"site yaml shared with the EMS (default: {DEFAULT_SIM_SITE})",
    )
    parser.add_argument("--ui-host", default="127.0.0.1", help="control panel bind host")
    parser.add_argument("--ui-port", default=8766, type=int, help="control panel bind port")
    parser.add_argument("--log-level", default=None, metavar="LEVEL")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    site = yaml.safe_load(Path(args.site).read_text(encoding="utf-8"))
    harness = SimHarness(site, site_path=args.site)
    harness.start()

    server = ThreadingHTTPServer((args.ui_host, args.ui_port), make_handler(harness))
    print(f"PyEMS simulator control panel at http://{args.ui_host}:{args.ui_port}")
    print(f"Run the EMS against it with:  pyems --site {args.site}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        harness.stop()
        server.server_close()


if __name__ == "__main__":
    main()
