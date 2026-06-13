"""Production wiring against the simulator: the REAL build_ems() pipeline
(CachedDriver worker, Modbus TCP on the wire, controllers, allocator) drives
the simulated plant, curtails export, then trips safety when the simulated
inverter goes offline — exactly the experiment the sim UI is for.
"""
import json
import socket
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from pyems.commands import write_command_file
from pyems.system_tags import SAFE_MODE_CHANNEL
from pyems.ems import ROOT, build_ems
from pyems.sim.harness import SimHarness, make_handler

SIM_SITE = ROOT / "config" / "site.sim.yaml"


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def fast_sim_site(tmp_path: Path) -> dict:
    """site.sim.yaml with free ports and compressed time constants so the
    full convergence story fits in a few wall-clock seconds."""
    site = yaml.safe_load(SIM_SITE.read_text(encoding="utf-8"))
    site["scenario"]["control_mode"] = "export_limit"
    site["scenario"]["active_power_limit_w"] = 30000
    site["export_limit"]["limit_w"] = 30000
    site["connection_point_active_power"]["export_limit_w"] = 30000
    site["connection_point_active_power"]["import_limit_w"] = 1000000000.0
    site["devices"][0]["port"] = free_port()
    site["devices"][1]["port"] = free_port()
    site["control"]["fast_cycle_s"] = 0.2
    site["control"]["poll_interval_s"] = 0.1
    site["safety"]["max_comms_age_s"] = 1.0
    site["allocation"]["channels"][0]["ramp_rate_w_per_s"] = 50000
    site["control"]["command_json"] = str(tmp_path / "commands.json")
    site["recording"]["cycle_csv"] = str(tmp_path / "sim_cycles.csv")
    site["telemetry"]["live_json"] = str(tmp_path / "live_state.json")
    site["simulation"]["tick_s"] = 0.05
    site["simulation"]["unit"]["tau_s"] = 0.3
    path = tmp_path / "site.sim.yaml"
    path.write_text(yaml.safe_dump(site, sort_keys=False), encoding="utf-8")
    site["_path"] = str(path)
    return site


def run_cycles(sched, seconds: float, cycle_s: float = 0.2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        sched.step(now=time.monotonic())
        time.sleep(cycle_s)


def test_real_ems_controls_simulated_plant_and_trips_on_offline(tmp_path):
    site = fast_sim_site(tmp_path)
    harness = SimHarness(site)
    harness.start()
    sched = None
    try:
        # deterministic plant: 100 kW available PV, 10 kW load, 30 kW export limit
        harness.set_source("pv", {"mode": "manual", "value_w": 100000.0})
        harness.set_source("load", {"mode": "manual", "value_w": 10000.0})

        sched = build_ems(site["_path"])
        write_command_file(site["control"]["command_json"], generation_enabled=True)
        run_cycles(sched, seconds=6.0)

        state = sched._state
        snap = harness.world.snapshot()
        # the EMS must have curtailed the unit: uncontrolled it would export
        # 90 kW; the limit is 30 kW, so steady state is ~40 kW production.
        assert snap["unit_active_power_setpoint_w"] < 60000.0, (
            "EMS setpoint never reached the simulated inverter"
        )
        assert state.get("grid.W") > -36000.0, "export limit not enforced"
        assert state.get(SAFE_MODE_CHANNEL) == 0.0
        assert state.get("pv.W") == pytest.approx(snap["unit_active_power_w"], rel=0.2)

        # ── inverter drops off the bus → comms age grows → safety trips ──────
        harness.set_fault("pv", "offline", True)
        run_cycles(sched, seconds=3.0)
        assert sched._state.get(SAFE_MODE_CHANNEL) == 1.0, (
            "safety did not trip on a dead simulated inverter"
        )

        # ── inverter recovers → safety releases ─────────────────────────────
        harness.set_fault("pv", "offline", False)
        run_cycles(sched, seconds=4.0)
        assert sched._state.get(SAFE_MODE_CHANNEL) == 0.0, (
            "safety did not release after the simulated inverter recovered"
        )
    finally:
        if sched is not None:
            sched._driver.disconnect()
        harness.stop()


def test_control_panel_http_api(tmp_path):
    site = fast_sim_site(tmp_path)
    harness = SimHarness(site)
    harness.start()
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(harness))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        time.sleep(0.3)  # let the world tick at least once

        with urllib.request.urlopen(f"{base}/api/state", timeout=5) as resp:
            state = json.loads(resp.read())
        assert state["scenario"]["active_power_limit_w"] == site["scenario"]["active_power_limit_w"]
        assert {d["id"] for d in state["devices"]} == {"grid", "pv"}
        assert state["history_keys"][0] == "t_s"
        # EMS link diagnostics: no EMS is running in this test
        assert all(d["read_age_s"] is None for d in state["devices"])
        assert "pyems --site" in state["ems_command"]

        body = json.dumps(
            {"target": "load", "mode": "replay", "csv": "1000\n2000\n3000", "speed": 5}
        ).encode()
        req = urllib.request.Request(
            f"{base}/api/source", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert json.loads(resp.read())["ok"] is True
        assert harness.load.describe()["mode"] == "replay"

        body = json.dumps({"device": "pv", "fault": "ignore_setpoint", "active": True}).encode()
        req = urllib.request.Request(
            f"{base}/api/fault", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert json.loads(resp.read())["ok"] is True
        assert harness.world.unit.ignore_setpoint is True

        # the UI page itself is served
        with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
            assert b"PyEMS simulator" in resp.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        harness.stop()
