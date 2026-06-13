"""
EMS configuration loader.

Nothing here is site-specific: device addresses, setpoints and safety
thresholds all come from config/site.yaml (data). Device register maps come
from profiles/ (data). This module only wires objects together.

IEC 61131-3 analogy: build_ems() assembles the RESOURCE (Scheduler) with its
TASKs and FUNCTION_BLOCKs, binding I/O per the CONFIGURATION in site.yaml.
"""
import argparse
import logging
import signal
import time
from pathlib import Path

import yaml

from pyems.allocation.allocator import PowerAllocator, SetpointChannelConfig
from pyems.allocation.request import RequestBoard
from pyems.channels import Channel, SystemState
from pyems.commands import DEFAULT_COMMAND_MAX_AGE_S, CommandFileReader
from pyems.control.pid import PIDGains
from pyems.controllers.connection_point_power import ConnectionPointPowerController
from pyems.controllers.generation_gate import GenerationGateController
from pyems.controllers.grid_export_limit import GridExportLimitController
from pyems.controllers.hard_switch import HardSwitchController
from pyems.controllers.safety import SafetyController
from pyems.controllers.setpoint_compliance import SetpointComplianceMonitor
from pyems.controllers.setpoint_headroom import SetpointHeadroomLimiter
from pyems.drivers.cached import CachedDriver
from pyems.drivers.composite import CompositeDriver
import pyems.drivers.modbus_device as md
from pyems.logging import setup_logging
from pyems.recording import CycleRecorder
from pyems.scheduler import Scheduler, Task
from pyems.telemetry import LiveSnapshotPublisher
from pyems.system_tags import (
    COMMAND_AGE_CHANNEL,
    COMMS_AGE_CHANNEL,
    CONNECTION_POINT_POWER_REQUESTER,
    EXPORT_LIMIT_REQUESTER,
    GENERATION_ALLOWED_CHANNEL,
    GENERATION_GATE_ACTIVE_CHANNEL,
    GENERATION_GATE_REQUESTER,
    IMPORT_LIMIT_REQUESTER,
    INVERTER_COMMAND_CHANNEL,
    INVERTER_COMMAND_ID_CHANNEL,
    INVERTER_RUN_STATE_CHANNEL,
    SAFE_MODE_CHANNEL,
    SETPOINT_HEADROOM_REQUESTER,
    SETPOINT_VIOLATION_CHANNEL,
    WRITE_AGE_CHANNEL,
    comms_age_channel,
)

logger = logging.getLogger(__name__)

# src/pyems/ems.py → parents[2] is the repo root holding profiles/ and config/.
ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "profiles"
DEFAULT_SITE = ROOT / "config" / "site.yaml"


_ACTIVE_POWER_BINDING_KEYS = (
    "connection_point_active_power_channel",
    "unit_active_power_channel",
    "unit_active_power_setpoint_channel",
)

EXPORT_LIMIT_MODE = "export_limit"
IMPORT_LIMIT_MODE = "import_limit"


def control_mode(site: dict) -> str:
    mode = site.get("scenario", {}).get("control_mode", EXPORT_LIMIT_MODE)
    if mode not in (EXPORT_LIMIT_MODE, IMPORT_LIMIT_MODE):
        raise ValueError(
            "scenario.control_mode must be 'export_limit' or 'import_limit', "
            f"got {mode!r}"
        )
    return mode


def _active_power_binding_channels(controller_cfg: dict) -> list[str]:
    return [controller_cfg[key] for key in _ACTIVE_POWER_BINDING_KEYS]


def _setpoint_headroom_config(site: dict) -> dict | None:
    """Resolve the available-power headroom config — ENABLED BY DEFAULT.

    A setpoint parked far above what the unit can deliver is a loaded spring:
    if the device's own ramp is fast, the returning resource jumps production
    to the stale value, bypassing the configured active power gradient. So
    the limiter is on unless explicitly disabled (`setpoint_headroom:
    enabled: false`); bindings default to the regulation unit and headroom_w
    to 10 % of the unit's allocation envelope (p_max_w).
    """
    cfg = site.get("setpoint_headroom") or {}
    if cfg.get("enabled", True) is False:
        return None
    cp_cfg = site["connection_point_active_power"]
    unit_ch = cfg.get("unit_active_power_channel", cp_cfg["unit_active_power_channel"])
    setpoint_ch = cfg.get(
        "unit_active_power_setpoint_channel", cp_cfg["unit_active_power_setpoint_channel"]
    )
    headroom_w = cfg.get("headroom_w")
    if headroom_w is None:
        alloc = next(
            (
                ch
                for ch in site["allocation"]["channels"]
                if ch["setpoint_channel"] == setpoint_ch
            ),
            None,
        )
        if alloc is None or alloc.get("p_max_w") is None:
            raise ValueError(
                f"setpoint_headroom: no allocation channel with p_max_w for "
                f"'{setpoint_ch}', so the default headroom (10% of p_max_w) "
                f"cannot be derived — set headroom_w explicitly"
            )
        headroom_w = 0.1 * float(alloc["p_max_w"])
    return {
        "priority": cfg.get("priority", 6),
        "headroom_w": float(headroom_w),
        "headroom_pct": float(cfg.get("headroom_pct", 0.0)),
        "unit_active_power_channel": unit_ch,
        "unit_active_power_setpoint_channel": setpoint_ch,
    }


def _generation_gate_config(site: dict) -> dict | None:
    """Resolve the generation-gate config, or None when not enabled.

    The gate is opt-in via `control.command_json` (the file the UI writes and the
    EMS reads — see pyems.commands). When set, the gate pins the controlled unit
    to a safe floor while generation is disabled. Bindings follow the scenario's
    regulation unit; the floor is 0 W when 0 sits inside the unit's allocation
    envelope, else p_min_w — so a storage unit (p_min_w < 0) parks at its safe
    minimum rather than being forced to charge.
    """
    cmd_json = site.get("control", {}).get("command_json")
    if not cmd_json:
        return None
    setpoint_ch = site["connection_point_active_power"]["unit_active_power_setpoint_channel"]
    alloc = next(
        (ch for ch in site["allocation"]["channels"] if ch["setpoint_channel"] == setpoint_ch),
        None,
    )
    if alloc is None:
        raise ValueError(
            f"generation gate: no allocation channel for setpoint '{setpoint_ch}', "
            f"so the disabled-floor cannot be derived"
        )
    p_min_w = float(alloc["p_min_w"])
    p_max_w = float(alloc["p_max_w"])
    floor_w = 0.0 if p_min_w <= 0.0 <= p_max_w else p_min_w
    path = Path(cmd_json)
    if not path.is_absolute():
        path = ROOT / path
    return {
        "path": path,
        "max_age_s": float(site["control"].get("command_max_age_s", DEFAULT_COMMAND_MAX_AGE_S)),
        "priority": int(site["control"].get("generation_gate_priority", 1)),
        "unit_active_power_setpoint_channel": setpoint_ch,
        "floor_w": floor_w,
    }


def _hard_switch_config(site: dict) -> dict | None:
    """Resolve the hard inverter switch config, or None when not enabled.

    Opt-in via the `hard_switch:` section. `start_writes`/`stop_writes` are lists
    of `{channel, value}` — the device command register(s) and the values to
    write for a remote start / stop. Vendor-flexible: one run/stop register or
    two separate command registers, any values. Channel existence and the
    `command` flag are validated in build_ems against the device tag pool.
    """
    cfg = site.get("hard_switch")
    if not cfg:
        return None

    def parse(key: str) -> list[tuple[str, float]]:
        pairs = []
        for item in cfg.get(key) or []:
            if not isinstance(item, dict) or "channel" not in item or "value" not in item:
                raise ValueError(f"hard_switch.{key} entries must be {{channel, value}}")
            pairs.append((str(item["channel"]), float(item["value"])))
        return pairs

    start_writes = parse("start_writes")
    stop_writes = parse("stop_writes")
    if not start_writes or not stop_writes:
        raise ValueError("hard_switch needs non-empty start_writes and stop_writes")
    return {"start_writes": start_writes, "stop_writes": stop_writes}


def _validate_hard_switch_channels(hard_cfg: dict, channels: list[Channel]) -> None:
    """Raise unless every hard-switch write targets a real `command` channel.

    A start/stop write must land on a register flagged `command: true` in the
    device profile: those are the only channels written one-shot (a plain
    setpoint would be continuously mirrored/keep-alived, spamming the command).
    """
    by_name = {c.name: c for c in channels}
    problems = []
    for key in ("start_writes", "stop_writes"):
        for ch, _ in hard_cfg[key]:
            channel = by_name.get(ch)
            if channel is None:
                problems.append(f"hard_switch.{key} channel '{ch}' is not in the tag pool")
            elif not getattr(channel, "command", False):
                problems.append(
                    f"hard_switch.{key} channel '{ch}' is not a command register "
                    f"(set `command: true` on it in the device profile)"
                )
    if problems:
        raise ValueError("hard_switch config error: " + "; ".join(problems))


def required_channels(site: dict) -> list[str]:
    """All tags the controllers will read/drive, per site.yaml bindings.

    Used to fail fast at startup: a typo in a binding must blow up here, not
    mid-control as a KeyError deep inside a controller on live hardware.
    """
    exp_cfg = site["export_limit"]
    cp_cfg = site["connection_point_active_power"]
    safe_cfg = site["safety"]
    alloc_cfg = site["allocation"]
    tags = [
        *_active_power_binding_channels(exp_cfg),
        *_active_power_binding_channels(cp_cfg),
        COMMS_AGE_CHANNEL,
        WRITE_AGE_CHANNEL,
        SAFE_MODE_CHANNEL,
        *safe_cfg["unit_active_power_setpoint_channels"],
        *safe_cfg.get("frozen_measurement_channels", []),
        *(
            comms_age_channel(dev_id)
            for dev_id in safe_cfg.get("device_comms_max_age_s", {})
        ),
        *(ch["setpoint_channel"] for ch in alloc_cfg["channels"]),
    ]
    comp_cfg = site.get("setpoint_compliance")
    if comp_cfg:
        tags += [
            comp_cfg["unit_active_power_channel"],
            comp_cfg["unit_active_power_setpoint_channel"],
            SETPOINT_VIOLATION_CHANNEL,
        ]
    head_cfg = _setpoint_headroom_config(site)
    if head_cfg:
        tags += [
            head_cfg["unit_active_power_channel"],
            head_cfg["unit_active_power_setpoint_channel"],
        ]
    if _generation_gate_config(site):
        tags += [GENERATION_ALLOWED_CHANNEL, GENERATION_GATE_ACTIVE_CHANNEL]
    if _hard_switch_config(site):
        tags += [
            INVERTER_COMMAND_CHANNEL,
            INVERTER_COMMAND_ID_CHANNEL,
            INVERTER_RUN_STATE_CHANNEL,
        ]
    return tags


def validate_bindings(site: dict, available: list[str]) -> None:
    """Raise if any controller-bound tag is absent from the tag pool."""
    pool = set(available)
    missing = [t for t in required_channels(site) if t not in pool]
    if missing:
        raise ValueError(
            f"site.yaml binds tags not present in the device tag pool: {missing}. "
            f"Check the bindings against the device profile channels."
        )


def _measurement_binding_channels(site: dict) -> list[str]:
    tags = [
        cfg[key]
        for cfg in (site["export_limit"], site["connection_point_active_power"])
        for key in (
            "connection_point_active_power_channel",
            "unit_active_power_channel",
        )
    ]
    tags += site["safety"].get("frozen_measurement_channels", [])
    comp_cfg = site.get("setpoint_compliance")
    if comp_cfg:
        tags.append(comp_cfg["unit_active_power_channel"])
    return tags


def _setpoint_binding_channels(site: dict) -> list[str]:
    return [
        site["export_limit"]["unit_active_power_setpoint_channel"],
        site["connection_point_active_power"]["unit_active_power_setpoint_channel"],
        *site["safety"]["unit_active_power_setpoint_channels"],
        *(ch["setpoint_channel"] for ch in site["allocation"]["channels"]),
    ]


def validate_binding_directions(site: dict, channels: list[Channel]) -> None:
    """Raise if a measurement binding hits a writable channel or vice versa.

    A measurement tag on a writable channel is silently fatal at runtime: the
    CachedDriver classifies channels by `writable`, so the polled value would
    never reach the controllers AND the stale state value would be flushed to
    the device as a setpoint (e.g. a meter profile mistakenly marking grid.W
    as read_write). Fail at startup instead.
    """
    writable = {c.name for c in channels if c.writable}
    bad_measurements = [t for t in _measurement_binding_channels(site) if t in writable]
    bad_setpoints = [t for t in _setpoint_binding_channels(site) if t not in writable]
    problems = []
    if bad_measurements:
        problems.append(
            f"measurement bindings point at writable channels {bad_measurements} "
            f"(check `access:` in the device profile — measurements must be 'read')"
        )
    if bad_setpoints:
        problems.append(
            f"setpoint bindings point at read-only channels {bad_setpoints} "
            f"(setpoints must be 'read_write' in the device profile)"
        )
    if problems:
        raise ValueError("site.yaml binding direction error: " + "; ".join(problems))


def _safe_active_power_w(site: dict, mode: str) -> float:
    """Fail-safe unit active power asserted on a safety trip (see safety.py)."""
    if mode == IMPORT_LIMIT_MODE:
        guarded_channel = site["safety"]["unit_active_power_setpoint_channels"][0]
        return next(
            (
                ch["p_min_w"]
                for ch in site["allocation"]["channels"]
                if ch["setpoint_channel"] == guarded_channel
            ),
            0.0,
        )
    return site["export_limit"]["limit_w"]


def validate_safety_allocation(site: dict) -> None:
    """Raise if a safety trip could not actually land on the unit.

    Two silent failure modes guarded here:
      - a guarded setpoint channel that is not an allocation channel: the
        RequestBoard only accepts posts for configured channels, so the FIRST
        trip would raise mid-control instead of curtailing;
      - a safe value outside the channel's device envelope: the priority-0
        claim would intersect to an empty range and be REJECTED by the
        arbiter — safety neutralized by a config error, with only a log line.
    """
    mode = control_mode(site)
    safe_w = _safe_active_power_w(site, mode)
    alloc = {ch["setpoint_channel"]: ch for ch in site["allocation"]["channels"]}
    problems = []
    for ch in site["safety"]["unit_active_power_setpoint_channels"]:
        cfg = alloc.get(ch)
        if cfg is None:
            problems.append(
                f"guarded setpoint channel '{ch}' is not an allocation channel "
                f"{sorted(alloc)} — a trip would post to an unknown channel"
            )
        elif not (cfg["p_min_w"] <= safe_w <= cfg["p_max_w"]):
            problems.append(
                f"safe active power {safe_w} W is outside the device envelope "
                f"[{cfg['p_min_w']}, {cfg['p_max_w']}] of '{ch}' — the "
                f"priority-0 claim would be rejected"
            )
    if problems:
        raise ValueError("safety/allocation mismatch: " + "; ".join(problems))


def validate_setpoint_keepalive(site: dict) -> None:
    """Raise if the keep-alive rewrite cannot feed the device comms watchdog.

    The device's own comms watchdog is the LAST line of defence (it fail-safes
    the unit when the EMS stops writing). `safety.device_comms_watchdog_s`
    declares the watchdog period the unit was commissioned with; the unchanged-
    setpoint rewrite must run at least twice per period or normal operation
    would starve the watchdog. Optional: omit the key if the device has no
    comms watchdog (and accept that an EMS outage leaves the last setpoint).
    """
    watchdog_s = site["safety"].get("device_comms_watchdog_s")
    if watchdog_s is None:
        return
    rewrite_s = site["control"].get("setpoint_rewrite_s", 10.0)
    if 2 * rewrite_s > watchdog_s:
        raise ValueError(
            f"control.setpoint_rewrite_s ({rewrite_s}s) must be at most half of "
            f"safety.device_comms_watchdog_s ({watchdog_s}s), or the device "
            f"watchdog trips during normal operation"
        )


def validate_write_age_guard(site: dict) -> None:
    """Raise if the optional write-age safety guard is mis-tuned.

    `safety.max_write_age_s` trips safety when setpoints stop reaching the bus
    even though reads still succeed (remote control lost, half-open socket).
    Omit the key to disable the guard. When set, two bounds must hold:
      - it must exceed the healthy keep-alive cadence (`setpoint_rewrite_s` plus
        a couple of polls), or normal operation trips it every rewrite period;
      - if the device has its own comms watchdog, the EMS must raise
        `sys.safe_mode` no later than the device fail-safes, so
        `max_write_age_s <= device_comms_watchdog_s`.
    """
    safe_cfg = site["safety"]
    max_write_age_s = safe_cfg.get("max_write_age_s")
    if max_write_age_s is None:
        return
    ctrl = site["control"]
    rewrite_s = ctrl.get("setpoint_rewrite_s", 10.0)
    poll_s = ctrl["poll_interval_s"]
    floor = rewrite_s + 2 * poll_s
    if max_write_age_s < floor:
        raise ValueError(
            f"safety.max_write_age_s ({max_write_age_s}s) must be at least "
            f"setpoint_rewrite_s + 2*poll_interval_s ({floor}s), or the healthy "
            f"keep-alive cadence itself trips the write-age guard"
        )
    watchdog_s = safe_cfg.get("device_comms_watchdog_s")
    if watchdog_s is not None and max_write_age_s > watchdog_s:
        raise ValueError(
            f"safety.max_write_age_s ({max_write_age_s}s) must not exceed "
            f"safety.device_comms_watchdog_s ({watchdog_s}s) — the EMS must "
            f"raise sys.safe_mode no later than the device fail-safes"
        )


def build_tasks(site: dict, command_sink=None) -> list[Task]:
    """Build the control tasks from a site config dict.

    Shared by build_ems() (real Modbus) and the simulation harness, so both
    exercise the *same* controllers and tuning — the sim verifies production.

    `command_sink` (the CachedDriver) is required only for the hard inverter
    switch (one-shot command writes); when None, the hard switch is skipped even
    if configured — keeps build_tasks usable in tests with no driver.
    """
    exp_cfg = site["export_limit"]
    cp_cfg = site["connection_point_active_power"]
    safe_cfg = site["safety"]
    fast_cycle_s = site["control"]["fast_cycle_s"]
    mode = control_mode(site)

    # PRIORITY 0 interlock — runs first, asserts safe state on a dead/frozen bus.
    device_comms_limits = {
        comms_age_channel(dev_id): float(limit_s)
        for dev_id, limit_s in safe_cfg.get("device_comms_max_age_s", {}).items()
    } or None
    safety_task = Task(
        name="safety",
        interval_s=fast_cycle_s,
        priority=0,
        controllers=[
            SafetyController(
                max_comms_age_s=safe_cfg["max_comms_age_s"],
                safe_active_power_w=_safe_active_power_w(site, mode),
                unit_active_power_setpoint_channels=safe_cfg["unit_active_power_setpoint_channels"],
                frozen_measurement_channels=safe_cfg.get("frozen_measurement_channels"),
                max_frozen_s=safe_cfg.get("max_measurement_frozen_s"),
                max_write_age_s=safe_cfg.get("max_write_age_s"),
                comms_age_limits=device_comms_limits,
            ),
        ],
    )

    fast_controllers = []
    if mode == EXPORT_LIMIT_MODE:
        fast_controllers.extend(
            [
                GridExportLimitController(
                    name=EXPORT_LIMIT_REQUESTER,
                    priority=exp_cfg["priority"],
                    export_limit_w=exp_cfg["limit_w"],
                    connection_point_active_power_channel=exp_cfg["connection_point_active_power_channel"],
                    unit_active_power_channel=exp_cfg["unit_active_power_channel"],
                    unit_active_power_setpoint_channel=exp_cfg["unit_active_power_setpoint_channel"],
                ),
                ConnectionPointPowerController(
                    name=CONNECTION_POINT_POWER_REQUESTER,
                    priority=cp_cfg["priority"],
                    export_limit_w=cp_cfg["export_limit_w"],
                    import_limit_w=cp_cfg["import_limit_w"],
                    connection_point_active_power_channel=cp_cfg["connection_point_active_power_channel"],
                    unit_active_power_channel=cp_cfg["unit_active_power_channel"],
                    unit_active_power_setpoint_channel=cp_cfg["unit_active_power_setpoint_channel"],
                    gains=PIDGains(**cp_cfg["gains"]),
                ),
            ]
        )
    else:
        fast_controllers.append(
            ConnectionPointPowerController(
                name=IMPORT_LIMIT_REQUESTER,
                priority=cp_cfg["priority"],
                export_limit_w=cp_cfg.get("export_limit_w", 0.0),
                import_limit_w=cp_cfg["import_limit_w"],
                connection_point_active_power_channel=cp_cfg["connection_point_active_power_channel"],
                unit_active_power_channel=cp_cfg["unit_active_power_channel"],
                unit_active_power_setpoint_channel=cp_cfg["unit_active_power_setpoint_channel"],
                gains=PIDGains(**cp_cfg["gains"]),
                mode=IMPORT_LIMIT_MODE,
                deadband_w=site["allocation"]["channels"][0].get("deadband_w", 200.0),
            )
        )

    # Actuator monitoring: sustained overshoot of the applied setpoint means
    # the unit ignores commands (e.g. remote control not enabled) — a fault no
    # write can fix, surfaced via sys.setpoint_violation + ERROR log.
    comp_cfg = site.get("setpoint_compliance")
    if comp_cfg:
        fast_controllers.append(
            SetpointComplianceMonitor(
                unit_active_power_channel=comp_cfg["unit_active_power_channel"],
                unit_active_power_setpoint_channel=comp_cfg["unit_active_power_setpoint_channel"],
                tolerance_w=comp_cfg.get("tolerance_w", 2000.0),
                max_violation_s=comp_cfg.get("max_violation_s", 30.0),
            )
        )

    # Available-power tracking: keep the setpoint at most headroom_w above
    # actual production, so a returning resource (cloud edge) cannot jump the
    # unit to a stale inflated setpoint faster than the configured gradient.
    # ON by default; opt out with `setpoint_headroom: {enabled: false}`.
    head_cfg = _setpoint_headroom_config(site)
    if head_cfg:
        logger.info(
            "Available-power headroom: %s <= %s + max(%g W, %g%% of unit output) (priority %d)",
            head_cfg["unit_active_power_setpoint_channel"],
            head_cfg["unit_active_power_channel"],
            head_cfg["headroom_w"], head_cfg["headroom_pct"], head_cfg["priority"],
        )
        fast_controllers.append(
            SetpointHeadroomLimiter(
                name=SETPOINT_HEADROOM_REQUESTER,
                priority=head_cfg["priority"],
                headroom_w=head_cfg["headroom_w"],
                headroom_pct=head_cfg["headroom_pct"],
                unit_active_power_channel=head_cfg["unit_active_power_channel"],
                unit_active_power_setpoint_channel=head_cfg["unit_active_power_setpoint_channel"],
            )
        )

    # Generation gate (operational interlock, opt-in via control.command_json):
    # a priority-1 pin to a safe floor while the operator has not enabled
    # production. Below safety (0), above every economic requester.
    gate_cfg = _generation_gate_config(site)
    if gate_cfg:
        logger.info(
            "Generation gate: %s pinned to %.0f W while disabled (priority %d)",
            gate_cfg["unit_active_power_setpoint_channel"], gate_cfg["floor_w"],
            gate_cfg["priority"],
        )
        fast_controllers.append(
            GenerationGateController(
                name=GENERATION_GATE_REQUESTER,
                priority=gate_cfg["priority"],
                unit_active_power_setpoint_channel=gate_cfg["unit_active_power_setpoint_channel"],
                floor_w=gate_cfg["floor_w"],
            )
        )

    # Hard inverter switch (latched remote start/stop): drives the device command
    # register(s) via the one-shot command sink. Needs the driver, so it is only
    # added when a command_sink is supplied (build_ems passes the CachedDriver).
    hard_cfg = _hard_switch_config(site)
    if hard_cfg and command_sink is not None:
        logger.info(
            "Hard inverter switch: start=%s stop=%s",
            hard_cfg["start_writes"], hard_cfg["stop_writes"],
        )
        fast_controllers.append(
            HardSwitchController(
                command_sink=command_sink,
                start_writes=hard_cfg["start_writes"],
                stop_writes=hard_cfg["stop_writes"],
            )
        )

    fast_task = Task(
        name="fast",
        interval_s=fast_cycle_s,
        priority=1,
        controllers=fast_controllers,
    )
    return [safety_task, fast_task]


def build_allocation(site: dict) -> tuple[PowerAllocator, RequestBoard]:
    """Build the RequestBoard + PowerAllocator from the `allocation` section.

    The board (where controllers post) and the allocator (sole writer of the
    setpoint channels) share the same channel list, so a typo is impossible.
    """
    fast_cycle_s = site["control"]["fast_cycle_s"]
    configs = [
        SetpointChannelConfig(**ch) for ch in site["allocation"]["channels"]
    ]
    # Echo the numbers actually loaded: "which config is this EMS running"
    # must be answerable from the log alone (site.yaml vs site.sim.yaml vs a
    # stale edit is a recurring failure mode during commissioning/simulation).
    for c in configs:
        logger.info(
            "Allocation %s: envelope [%g, %g] W, default %g W, "
            "ramp up %g / down %g W/s, deadband %g W",
            c.setpoint_channel, c.p_min_w, c.p_max_w, c.default_w,
            c.ramp_up_w_per_s, c.ramp_down_w_per_s, c.deadband_w,
        )
    board = RequestBoard([c.setpoint_channel for c in configs])
    allocator = PowerAllocator(configs, board, cycle_s=fast_cycle_s)
    return allocator, board


def build_device_drivers(devices_cfg: list[dict]) -> list[md.ModbusDeviceDriver]:
    """Build field-device drivers, sharing one client per bus endpoint.

    Per-device optional keys in site.yaml:
      port       TCP port (default: profile default_port)
      serial     RTU bus settings {baudrate, bytesize, parity, stopbits}
                 (default: 9600 8N1 — see DEFAULT_SERIAL in modbus_device)
      timeout_s  transaction timeout for either protocol
      retries    per-transaction retries for either protocol

    Devices on the same endpoint share one client, so their serial/timeout
    settings must agree — a conflict is a config error, not a second client.
    """
    clients: dict[tuple[str, str, int | None], tuple[object, tuple]] = {}
    drivers: list[md.ModbusDeviceDriver] = []
    for dev in devices_cfg:
        profile = md.DeviceProfile.load(PROFILES / dev["profile"])
        port = dev.get(
            "port",
            profile.default_port if profile.protocol == "modbus_tcp" else None,
        )
        serial = dev.get("serial")
        timeout_s = dev.get("timeout_s")
        retries = dev.get("retries")
        settings = (tuple(sorted((serial or {}).items())), timeout_s, retries)
        key = (profile.protocol, dev["host"], port)
        if key in clients:
            client, first_settings = clients[key]
            if settings != first_settings:
                raise ValueError(
                    f"device '{dev.get('id', dev['host'])}' shares endpoint {key} "
                    f"but has conflicting serial/timeout settings "
                    f"({settings} vs {first_settings})"
                )
        else:
            client = md.make_client(
                profile.protocol,
                dev["host"],
                port=port,
                default_port=profile.default_port,
                serial=serial,
                timeout_s=timeout_s,
                retries=retries,
            )
            clients[key] = (client, settings)
        drivers.append(
            md.ModbusDeviceDriver(
                profile,
                client=client,
                slave_id=dev["slave_id"],
                prefix=dev.get("id"),
            )
        )
    return drivers


def build_recorder(site: dict, available: list[str]) -> CycleRecorder | None:
    """Build the per-cycle CSV recorder from the optional `recording:` section.

    Defaults to every controller-bound tag; an explicit channel list is
    validated against the tag pool (a typo must fail at startup, not record
    empty columns for a week).
    """
    rec_cfg = site.get("recording") or {}
    csv_path = rec_cfg.get("cycle_csv")
    if not csv_path:
        return None
    channels = rec_cfg.get("channels")
    if channels is None:
        channels = sorted(set(required_channels(site)))
    pool = set(available)
    missing = [t for t in channels if t not in pool]
    if missing:
        raise ValueError(
            f"recording.channels lists tags not present in the tag pool: {missing}"
        )
    path = Path(csv_path)
    if not path.is_absolute():
        path = ROOT / path
    recorder = CycleRecorder(path, channels)
    logger.info("Cycle recording to %s (%d channels)", path, len(channels))
    return recorder


def build_publisher(
    site: dict, channels: list[Channel]
) -> LiveSnapshotPublisher | None:
    """Build the live-state JSON publisher from the optional `telemetry:` section.

    One snapshot file rewritten each cycle, for the read-only UI to poll off the
    filesystem (no second Modbus session). Channel metadata (unit/writable) is
    captured so a consumer can render the snapshot without the device drivers.
    """
    tele_cfg = site.get("telemetry") or {}
    json_path = tele_cfg.get("live_json")
    if not json_path:
        return None
    path = Path(json_path)
    if not path.is_absolute():
        path = ROOT / path
    publisher = LiveSnapshotPublisher(path, channels=channels)
    logger.info("Live telemetry snapshot to %s (%d channels)", path, len(channels))
    return publisher


def build_ems(site_path: str | Path = DEFAULT_SITE) -> Scheduler:
    logger.info("Building EMS from %s", Path(site_path).resolve())
    site = yaml.safe_load(Path(site_path).read_text(encoding="utf-8"))
    mode = control_mode(site)
    limit_w = (
        site["export_limit"]["limit_w"]
        if mode == EXPORT_LIMIT_MODE
        else site["connection_point_active_power"]["import_limit_w"]
    )
    logger.info("Scenario: %s, %g W at the connection point", mode, limit_w)

    # Config-only consistency checks first — cheaper than touching the bus,
    # and a config that would neutralize the safety layer must never start.
    validate_safety_allocation(site)
    validate_setpoint_keepalive(site)
    validate_write_age_guard(site)

    ctrl_cfg = site["control"]

    # Field devices from site config. Logical devices that share a TCP endpoint
    # also share one client; each driver keeps its own slave ID and tag prefix.
    device_drivers = build_device_drivers(site["devices"])

    # One resource, multiple field devices (IEC §2.4.1.1): merged tag pool.
    configured_ids = [dev.get("id") for dev in site["devices"]]
    device_ids = configured_ids if all(configured_ids) else None
    devices = CompositeDriver(device_drivers, device_ids=device_ids)

    # Non-blocking I/O: real Modbus runs in a background thread against a tag
    # cache, so a slow/hung bus transaction never stalls the control cycle.
    driver = CachedDriver(
        devices,
        poll_interval_s=ctrl_cfg["poll_interval_s"],
        setpoint_rewrite_s=ctrl_cfg.get("setpoint_rewrite_s", 10.0),
    )
    driver.connect()

    # Channels: device profiles (incl. sys.comms_age_s from CachedDriver) plus
    # the system status words (not device registers).
    channels = driver.channels() + [
        Channel(SAFE_MODE_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
        Channel(SETPOINT_VIOLATION_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
    ]

    # Generation gate (opt-in): the command tag the gate reads, its active flag,
    # and the age of the UI command file. Added only when the gate is enabled so
    # the tag pool stays unchanged for sites without an operator command channel.
    gate_cfg = _generation_gate_config(site)
    commands = None
    if gate_cfg:
        channels += [
            Channel(GENERATION_ALLOWED_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
            Channel(GENERATION_GATE_ACTIVE_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
            Channel(COMMAND_AGE_CHANNEL, unit="s", value=float("inf")),
        ]
        # run_start_wall pins "this run": a leftover command file that enabled
        # generation in a previous run must not re-enable it after a restart.
        commands = CommandFileReader(
            gate_cfg["path"],
            run_start_wall=time.time(),
            max_age_s=gate_cfg["max_age_s"],
        )
        logger.info(
            "Generation gate command file: %s (max age %.0fs); generation starts DISABLED",
            gate_cfg["path"], gate_cfg["max_age_s"],
        )

    # Hard inverter switch (opt-in): the latched command tags it reads + the
    # run-state it reports. The command file (above) carries the action; require
    # the gate's command_json so there IS a reader to publish the inverter tags.
    hard_cfg = _hard_switch_config(site)
    if hard_cfg:
        if commands is None:
            raise ValueError(
                "hard_switch requires control.command_json (the UI command file "
                "the EMS reads), so the inverter command can reach the EMS"
            )
        channels += [
            Channel(INVERTER_COMMAND_CHANNEL, unit="", min_val=0, max_val=1, value=float("nan")),
            Channel(INVERTER_COMMAND_ID_CHANNEL, unit="", value=float("nan")),
            Channel(INVERTER_RUN_STATE_CHANNEL, unit="", min_val=0, max_val=1, writable=True, value=float("nan")),
        ]
        _validate_hard_switch_channels(hard_cfg, channels)

    state = SystemState(channels)

    # Fail fast: every controller-bound tag must exist before we start driving
    # hardware (a typo must fail here, not as a mid-cycle KeyError on the bus),
    # and measurements/setpoints must land on channels of the right direction.
    validate_bindings(site, [c.name for c in channels])
    validate_binding_directions(site, channels)

    # The CachedDriver is the command sink for the hard switch (one-shot writes).
    tasks = build_tasks(site, command_sink=driver)
    allocator, board = build_allocation(site)
    recorder = build_recorder(site, [c.name for c in channels])
    publisher = build_publisher(site, channels)

    logger.info(
        "EMS built: %d devices, %d channels, tasks=%s, allocator channels=%s",
        len(device_drivers), len(channels), [t.name for t in tasks],
        allocator.channels,
    )
    return Scheduler(
        tasks=tasks, state=state, driver=driver, allocator=allocator, board=board,
        recorder=recorder, telemetry=publisher, commands=commands,
    )


def main() -> None:
    """Console entry point (see [project.scripts] in pyproject.toml)."""
    parser = argparse.ArgumentParser(
        prog="pyems", description="Local energy management system (Modbus TCP/RTU)."
    )
    parser.add_argument(
        "--site", type=Path, default=DEFAULT_SITE,
        help=f"path to site.yaml (default: {DEFAULT_SITE})",
    )
    parser.add_argument(
        "--log-level", default=None, metavar="LEVEL",
        help="DEBUG, INFO, WARNING, ... (default: $PYEMS_LOG_LEVEL or INFO)",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    scheduler = build_ems(args.site)

    # systemd stops services with SIGTERM: request a clean shutdown (finish the
    # cycle, disconnect the bus) instead of dying mid-Modbus-transaction.
    # Ctrl-C (SIGINT/KeyboardInterrupt) is already handled inside run().
    def _on_sigterm(signum, frame) -> None:
        logger.info("Received SIGTERM; requesting scheduler stop")
        scheduler.stop()

    signal.signal(signal.SIGTERM, _on_sigterm)
    scheduler.run()


if __name__ == "__main__":
    main()
