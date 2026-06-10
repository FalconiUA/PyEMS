"""Simulator lifecycle from the configuration UI (Simulation tab backend)."""
import json
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from pyems import ui
from pyems.ems import ROOT

SIM_SITE = ROOT / "config" / "site.sim.yaml"


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture()
def sim_site(tmp_path) -> Path:
    site = yaml.safe_load(SIM_SITE.read_text(encoding="utf-8"))
    site["devices"][0]["port"] = free_port()
    site["devices"][1]["port"] = free_port()
    path = tmp_path / "site.sim.yaml"
    path.write_text(yaml.safe_dump(site, sort_keys=False), encoding="utf-8")
    return path


def test_sim_manager_start_status_stop(sim_site):
    manager = ui.SimManager(sim_site, panel_host="127.0.0.1", panel_port=free_port())
    try:
        status = manager.status()
        assert status["reachable"] is False
        assert status["managed"] is False
        assert str(sim_site) in status["ems_command"]

        status = manager.start()
        assert status["reachable"] is True
        assert status["managed"] is True

        # idempotent: a second start must not spawn a second process
        proc = manager._proc
        manager.start()
        assert manager._proc is proc

        status = manager.stop()
        assert status["managed"] is False
    finally:
        manager.stop_managed()


def test_site_file_switching(tmp_path):
    app = ui.UIApp(tmp_path / "site.yaml",
                   sim=ui.SimManager(SIM_SITE, panel_port=free_port()))
    try:
        # both the hardware site and the simulation site are offered
        assert [str(p) for p in app.site_choices] == [
            str(tmp_path / "site.yaml"), str(SIM_SITE),
        ]
        result = app.set_site_file(str(SIM_SITE))
        assert result["site_path"] == str(SIM_SITE)
        assert app.site_path == SIM_SITE

        payload = ui.app_config_payload(app)
        assert payload["site_path"] == str(SIM_SITE)
        assert payload["sim_site_path"] == str(SIM_SITE)
        assert str(tmp_path / "site.yaml") in payload["site_choices"]
        # the sim site loads and validates through the same UI pipeline
        assert payload["validation"]["ok"], payload["validation"]["error"]

        # arbitrary paths are rejected — only the known choices are editable
        with pytest.raises(ValueError, match="unknown site file"):
            app.set_site_file(str(tmp_path / "evil.yaml"))
    finally:
        app.close()
        app.sim.stop_managed()


def test_sim_manager_missing_site_fails_clearly(tmp_path):
    manager = ui.SimManager(tmp_path / "nope.yaml", panel_port=free_port())
    with pytest.raises(ValueError, match="not found"):
        manager.start()


def test_sim_status_route_and_pages(sim_site, tmp_path):
    app = ui.UIApp(tmp_path / "site.yaml",
                   sim=ui.SimManager(sim_site, panel_port=free_port()))
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), ui.make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/sim/status", timeout=5) as resp:
            status = json.loads(resp.read())
        assert status["ok"] is True
        assert status["reachable"] is False
        assert status["panel_port"] == app.sim.panel_port

        # every tab page referenced by index.html must be servable, or the
        # whole UI fails to boot (loadPages is all-or-nothing)
        index = (ui.STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        import re
        for page in re.findall(r'data-page="([^"]+)"', index):
            with urllib.request.urlopen(base + page, timeout=5) as resp:
                assert resp.status == 200, page
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.close()
        app.sim.stop_managed()
