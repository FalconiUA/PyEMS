"""Tag registry: one command that maps every tag of a site to its origin and
its users.

The codebase deliberately keeps names in three families:

  1. device tags      `<device id>.<field>`  — DATA: profiles/*.yaml + site.yaml
  2. system tags      `sys.*`                — CODE: pyems/system_tags.py (only)
  3. binding keys     `unit_active_power_channel`, ... — site.yaml keys wiring
                      controllers to tags (documented in internal-tags.md)

This module derives, for ONE concrete site, the full cross-reference: where
each tag comes from (device register / system status word), who reads it,
who writes it, and under which binding key — generated from the same config
the EMS runs, so it cannot go stale:

    pyems-tags --site config/site.sim.yaml
    pyems-tags --site config/site.sim.yaml --markdown documents/tag-map.md
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from pyems.drivers.modbus_device import DeviceProfile, namespaced
from pyems.ems import (
    DEFAULT_SITE,
    EXPORT_LIMIT_MODE,
    PROFILES,
    _setpoint_headroom_config,
    control_mode,
)
from pyems.system_tags import (
    COMMS_AGE_CHANNEL,
    CONNECTION_POINT_POWER_REQUESTER,
    EXPORT_LIMIT_REQUESTER,
    IMPORT_LIMIT_REQUESTER,
    SAFE_MODE_CHANNEL,
    SAFETY_REQUESTER,
    SETPOINT_HEADROOM_REQUESTER,
    SETPOINT_VIOLATION_CHANNEL,
)


@dataclass
class TagEntry:
    tag: str
    origin: str               # where the value comes from
    access: str               # read | read_write | status
    unit: str = ""
    reads: list[str] = field(default_factory=list)   # who consumes it
    writes: list[str] = field(default_factory=list)  # who produces/commands it


def collect(site: dict) -> dict[str, TagEntry]:
    """Build the tag cross-reference for one site config."""
    entries: dict[str, TagEntry] = {}

    def entry(tag: str) -> TagEntry:
        if tag not in entries:
            # a binding that points nowhere — visible instead of silent
            entries[tag] = TagEntry(tag, origin="!! NOT IN TAG POOL", access="?")
        return entries[tag]

    # 1) device tags from profiles (the measured/commanded reality)
    for dev in site.get("devices", []):
        profile = DeviceProfile.load(PROFILES / dev["profile"])
        for reg in profile.registers:
            tag = namespaced(reg.channel, dev.get("id"))
            e = TagEntry(
                tag,
                origin=f"device '{dev.get('id')}' ({profile.model}) reg @{reg.address}",
                access=reg.access,
                unit=reg.unit,
            )
            if reg.writable:
                e.writes.append("CachedDriver flush (keep-alive rewrite)")
            else:
                e.reads.append("CachedDriver poll -> tag cache")
            entries[tag] = e

    # 2) system status words (names live in pyems/system_tags.py)
    entries[COMMS_AGE_CHANNEL] = TagEntry(
        COMMS_AGE_CHANNEL, origin="system_tags.py (CachedDriver)", access="status",
        unit="s", writes=["CachedDriver (age of last good bus read)"],
    )
    entries[SAFE_MODE_CHANNEL] = TagEntry(
        SAFE_MODE_CHANNEL, origin="system_tags.py (SafetyController)", access="status",
        writes=["SafetyController (1 = trip)"],
    )

    mode = control_mode(site)
    exp_cfg = site.get("export_limit", {})
    cp_cfg = site.get("connection_point_active_power", {})

    # 3) regulation bindings
    if mode == EXPORT_LIMIT_MODE:
        regulators = [
            (EXPORT_LIMIT_REQUESTER, exp_cfg, f"priority {exp_cfg.get('priority')}"),
            (CONNECTION_POINT_POWER_REQUESTER, cp_cfg, f"priority {cp_cfg.get('priority')}"),
        ]
    else:
        regulators = [
            (IMPORT_LIMIT_REQUESTER, cp_cfg, f"priority {cp_cfg.get('priority')}"),
        ]
    for requester, cfg, prio in regulators:
        if not cfg:
            continue
        entry(cfg["connection_point_active_power_channel"]).reads.append(
            f"{requester} (connection_point_active_power_channel)"
        )
        entry(cfg["unit_active_power_channel"]).reads.append(
            f"{requester} (unit_active_power_channel)"
        )
        entry(cfg["unit_active_power_setpoint_channel"]).writes.append(
            f"{requester} -> request on board ({prio})"
        )

    # 4) safety
    safe_cfg = site.get("safety", {})
    entry(COMMS_AGE_CHANNEL).reads.append(
        f"{SAFETY_REQUESTER} (trip if > {safe_cfg.get('max_comms_age_s')} s)"
    )
    for ch in safe_cfg.get("frozen_measurement_channels", []) or []:
        entry(ch).reads.append(
            f"{SAFETY_REQUESTER} freeze guard "
            f"(trip if identical {safe_cfg.get('max_measurement_frozen_s')} s)"
        )
    for ch in safe_cfg.get("unit_active_power_setpoint_channels", []) or []:
        entry(ch).writes.append(
            f"{SAFETY_REQUESTER} -> PRIORITY-0 claim on trip (forced safe value)"
        )

    # 5) setpoint compliance (actuator monitoring)
    comp_cfg = site.get("setpoint_compliance")
    if comp_cfg:
        entries[SETPOINT_VIOLATION_CHANNEL] = TagEntry(
            SETPOINT_VIOLATION_CHANNEL,
            origin="system_tags.py (SetpointComplianceMonitor)", access="status",
            writes=["SetpointComplianceMonitor (1 = unit ignores setpoint)"],
        )
        entry(comp_cfg["unit_active_power_channel"]).reads.append(
            "SetpointComplianceMonitor (unit_active_power_channel)"
        )
        entry(comp_cfg["unit_active_power_setpoint_channel"]).reads.append(
            "SetpointComplianceMonitor (applied setpoint read-back)"
        )

    # 6) available-power headroom
    head_cfg = _setpoint_headroom_config(site)
    if head_cfg:
        entry(head_cfg["unit_active_power_channel"]).reads.append(
            f"{SETPOINT_HEADROOM_REQUESTER} (unit_active_power_channel)"
        )
        entry(head_cfg["unit_active_power_setpoint_channel"]).writes.append(
            f"{SETPOINT_HEADROOM_REQUESTER} -> cap P_unit + "
            f"max({head_cfg['headroom_w']:g} W, {head_cfg['headroom_pct']:g}%)"
        )

    # 7) allocation: the SOLE writer of unit setpoint channels
    for ch_cfg in site.get("allocation", {}).get("channels", []):
        ch = ch_cfg["setpoint_channel"]
        entry(ch).writes.append(
            f"PowerAllocator — SOLE WRITER (envelope [{ch_cfg.get('p_min_w')}, "
            f"{ch_cfg.get('p_max_w')}] W, ramp up {ch_cfg.get('ramp_rate_w_per_s')}"
            f" / down {ch_cfg.get('ramp_down_w_per_s', ch_cfg.get('ramp_rate_w_per_s'))} W/s)"
        )

    # 8) cycle recorder
    rec_cfg = site.get("recording") or {}
    if rec_cfg.get("cycle_csv"):
        recorded = rec_cfg.get("channels")
        for ch in recorded if recorded is not None else sorted(entries):
            if recorded is not None or ch in entries:
                entry(ch).reads.append("CycleRecorder -> " + str(rec_cfg["cycle_csv"]))

    return entries


def requester_rows(site: dict) -> list[tuple[str, str, str]]:
    """(requester, priority, role) rows for the arbitration table."""
    mode = control_mode(site)
    rows = [(SAFETY_REQUESTER, "0 (reserved)", "forced safe value on trip; bypasses deadband+ramp")]
    if mode == EXPORT_LIMIT_MODE:
        rows.append((EXPORT_LIMIT_REQUESTER, str(site["export_limit"].get("priority")),
                     "upper bound: P_unit + P_cp + export limit"))
        rows.append((CONNECTION_POINT_POWER_REQUESTER,
                     str(site["connection_point_active_power"].get("priority")),
                     "regulation target at the connection point (PID)"))
    else:
        rows.append((IMPORT_LIMIT_REQUESTER,
                     str(site["connection_point_active_power"].get("priority")),
                     "import-limit regulation target"))
    head_cfg = _setpoint_headroom_config(site)
    if head_cfg:
        rows.append((SETPOINT_HEADROOM_REQUESTER, str(head_cfg["priority"]),
                     "upper bound: P_unit + headroom (available-power tracking)"))
    return sorted(rows, key=lambda r: (r[1], r[0]))


def render_text(entries: dict[str, TagEntry], site: dict) -> str:
    lines = []
    devices = sorted({t.split(".", 1)[0] for t in entries})
    for dev in devices:
        lines.append(f"\n== {dev} " + "=" * max(1, 60 - len(dev)))
        for tag in sorted(t for t in entries if t.split(".", 1)[0] == dev):
            e = entries[tag]
            unit = f" [{e.unit}]" if e.unit else ""
            lines.append(f"{tag}{unit}  ({e.access})  <- {e.origin}")
            for r in e.reads:
                lines.append(f"    read:  {r}")
            for w in e.writes:
                lines.append(f"    write: {w}")
    lines.append("\n== setpoint arbitration (RequestBoard) " + "=" * 24)
    for requester, prio, role in requester_rows(site):
        lines.append(f"priority {prio:<12} {requester:<32} {role}")
    lines.append(
        "\nRename rules: device tags -> profiles/*.yaml + site.yaml device id; "
        "sys.* tags and requester names -> src/pyems/system_tags.py; "
        "binding keys -> documents/internal-tags.md"
    )
    return "\n".join(lines)


def render_markdown(entries: dict[str, TagEntry], site: dict, site_path: str) -> str:
    lines = [
        f"# Tag map — `{site_path}`",
        "",
        "GENERATED by `pyems-tags` — do not edit by hand; regenerate with:",
        "",
        f"    pyems-tags --site {site_path} --markdown <this file>",
        "",
        "| Tag | Unit | Access | Origin | Read by | Written by |",
        "|---|---|---|---|---|---|",
    ]
    for tag in sorted(entries):
        e = entries[tag]
        lines.append(
            f"| `{tag}` | {e.unit} | {e.access} | {e.origin} | "
            f"{'<br>'.join(e.reads) or '—'} | {'<br>'.join(e.writes) or '—'} |"
        )
    lines += ["", "## Setpoint arbitration (RequestBoard)", "",
              "| Priority | Requester | Role |", "|---|---|---|"]
    for requester, prio, role in requester_rows(site):
        lines.append(f"| {prio} | `{requester}` | {role} |")
    lines += ["", "Rename rules: device tags → `profiles/*.yaml` + device id in "
              "site.yaml; `sys.*` tags and requester names → "
              "`src/pyems/system_tags.py`; binding keys → "
              "`documents/internal-tags.md`.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="pyems-tags",
        description="Map every tag of a site: origin, readers, writers.",
    )
    parser.add_argument("--site", type=Path, default=DEFAULT_SITE)
    parser.add_argument("--markdown", type=Path, default=None,
                        help="also write the map as a markdown file")
    args = parser.parse_args(argv)

    site = yaml.safe_load(Path(args.site).read_text(encoding="utf-8"))
    entries = collect(site)
    print(render_text(entries, site))
    if args.markdown:
        args.markdown.write_text(
            render_markdown(entries, site, str(args.site)), encoding="utf-8"
        )
        print(f"\nmarkdown written to {args.markdown}")


if __name__ == "__main__":
    main()
