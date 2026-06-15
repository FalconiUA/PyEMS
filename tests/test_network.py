"""NetworkController (src/pyems/ui.py): nmcli parsing, request validation, the
device-subnet cross-check, and the apply path. Pure-logic — no real nmcli runs;
the subprocess boundary (`_nmcli`) is faked."""
import pytest

import pyems.ui as ui
from pyems.ui import (
    DEFAULT_SIM_SITE,
    NetworkController,
    parse_nmcli_connections,
    parse_nmcli_ipv4,
    pick_primary_connection,
    subnet_mismatch_hosts,
    validate_network_request,
)


# ── pure parsers ─────────────────────────────────────────────────────────────

def test_parse_connections_prefers_ethernet():
    out = (
        "Wired connection 1:eth0:ethernet:activated\n"
        "preconfigured:wlan0:wifi:activated\n"
    )
    conns = parse_nmcli_connections(out)
    assert len(conns) == 2
    assert pick_primary_connection(conns)["device"] == "eth0"


def test_pick_primary_none_when_empty():
    assert pick_primary_connection([]) is None


def test_parse_ipv4_manual():
    out = (
        "ipv4.method:manual\n"
        "ipv4.addresses:192.168.0.11/24\n"
        "ipv4.gateway:192.168.0.1\n"
        "ipv4.dns:192.168.0.1\n"
    )
    parsed = parse_nmcli_ipv4(out)
    assert parsed["method"] == "manual"
    assert parsed["address"] == "192.168.0.11"
    assert parsed["prefix"] == 24
    assert parsed["gateway"] == "192.168.0.1"


def test_parse_ipv4_auto_has_no_address():
    out = "ipv4.method:auto\nipv4.addresses:\nipv4.gateway:\nipv4.dns:\n"
    parsed = parse_nmcli_ipv4(out)
    assert parsed["method"] == "auto"
    assert parsed["address"] == ""
    assert parsed["prefix"] is None


# ── request validation ───────────────────────────────────────────────────────

def test_validate_manual_normalizes_dns_list():
    spec = validate_network_request({
        "method": "manual", "address": "192.168.0.11", "prefix": 24,
        "gateway": "192.168.0.1", "dns": "192.168.0.1, 8.8.8.8",
    })
    assert spec["method"] == "manual"
    assert spec["dns"] == ["192.168.0.1", "8.8.8.8"]


def test_validate_auto_is_minimal():
    assert validate_network_request({"method": "auto"}) == {"method": "auto"}


@pytest.mark.parametrize("req", [
    {"method": "manual", "address": "999.1.1.1", "prefix": 24},
    {"method": "manual", "address": "192.168.0.11", "prefix": 40},
    {"method": "manual", "address": "192.168.0.11", "prefix": 24, "dns": "not-an-ip"},
    {"method": "sideways"},
])
def test_validate_rejects_bad_input(req):
    with pytest.raises(ValueError):
        validate_network_request(req)


# ── device-subnet cross-check ────────────────────────────────────────────────

def test_subnet_mismatch_flags_other_subnet_and_skips_hostnames():
    outside = subnet_mismatch_hosts(
        "192.168.0.11", 24, ["192.168.0.100", "192.168.1.50", "inverter.local"]
    )
    assert outside == ["192.168.1.50"]


# ── NetworkController ────────────────────────────────────────────────────────

def test_status_degrades_without_nmcli(monkeypatch):
    nc = NetworkController(DEFAULT_SIM_SITE)

    def boom(args):
        raise ui._NmcliUnavailable("nmcli not found")

    monkeypatch.setattr(nc, "_nmcli", boom)
    status = nc.status()
    assert status["ok"] is True
    assert status["available"] is False
    assert status["suggested"]["address"] == "192.168.0.11"


def test_apply_manual_modifies_then_brings_up(monkeypatch):
    nc = NetworkController(DEFAULT_SIM_SITE, ui_port=8765)
    calls = []
    monkeypatch.setattr(nc, "_primary", lambda: {"name": "Wired connection 1", "device": "eth0"})
    monkeypatch.setattr(nc, "_device_hosts", lambda: [])
    monkeypatch.setattr(nc, "_bring_up_later", lambda name: calls.append(("up", name)))
    monkeypatch.setattr(nc, "_nmcli", lambda args: (calls.append(tuple(args)) or (0, "", "")))

    result = nc.apply({
        "method": "manual", "address": "192.168.0.11", "prefix": 24,
        "gateway": "192.168.0.1", "dns": "192.168.0.1",
    })

    assert result["ok"] is True
    assert result["new_url"] == "http://192.168.0.11:8765"
    assert result["reconnect"] is True
    modify = next(c for c in calls if c[:2] == ("connection", "modify"))
    assert "manual" in modify and "192.168.0.11/24" in modify
    assert ("up", "Wired connection 1") in calls


def test_apply_auto_has_no_reconnect_url(monkeypatch):
    nc = NetworkController(DEFAULT_SIM_SITE)
    calls = []
    monkeypatch.setattr(nc, "_primary", lambda: {"name": "Wired connection 1", "device": "eth0"})
    monkeypatch.setattr(nc, "_device_hosts", lambda: [])
    monkeypatch.setattr(nc, "_bring_up_later", lambda name: None)
    monkeypatch.setattr(nc, "_nmcli", lambda args: (calls.append(tuple(args)) or (0, "", "")))

    result = nc.apply({"method": "auto"})

    assert result["new_url"] is None
    assert result["reconnect"] is False
    modify = next(c for c in calls if c[:2] == ("connection", "modify"))
    assert "auto" in modify
