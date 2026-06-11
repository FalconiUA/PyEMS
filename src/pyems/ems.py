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
from pathlib import Path

import yaml

from pyems.allocation.allocator import PowerAllocator, SetpointChannelConfig
from pyems.allocation.request import RequestBoard
from pyems.channels import Channel, SystemState
from pyems.control.pid import PIDGains
from pyems.controllers.connection_point_import_limit import ConnectionPointImportLimitController
from pyems.controllers.connection_point_power import ConnectionPointPowerController
from pyems.controllers.grid_export_limit import GridExportLimitController
from pyems.controllers.safety import SAFE_MODE_CHANNEL, SafetyController
from pyems.controllers.setpoint_compliance import (
    SETPOINT_VIOLATION_CHANNEL,
    SetpointComplianceMonitor,
)
from pyems.controllers.setpoint_headroom import SetpointHeadroomLimiter
from pyems.drivers.cached import COMMS_AGE_CHANNEL, CachedDriver
from pyems.drivers.composite import CompositeDriver
import pyems.drivers.modbus_device as md
from pyems.logging_config import setup_logging
from pyems.recording import CycleRecorder
from pyems.scheduler import Scheduler, Task

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
        "unit_active_power_channel": unit_ch,
        "unit_active_power_setpoint_channel": setpoint_ch,
    }


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
        SAFE_MODE_CHANNEL,
        *safe_cfg["unit_active_power_setpoint_channels"],
        *safe_cfg.get("frozen_measurement_channels", []),
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


def build_tasks(site: dict) -> list[Task]:
    """Build the control tasks from a site config dict.

    Shared by build_ems() (real Modbus) and the simulation harness, so both
    exercise the *same* controllers and tuning — the sim verifies production.
    """
    exp_cfg = site["export_limit"]
    cp_cfg = site["connection_point_active_power"]
    safe_cfg = site["safety"]
    fast_cycle_s = site["control"]["fast_cycle_s"]
    mode = control_mode(site)

    # PRIORITY 0 interlock — runs first, asserts safe state on a dead/frozen bus.
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
            ),
        ],
    )

    fast_controllers = []
    if mode == EXPORT_LIMIT_MODE:
        fast_controllers.extend(
            [
                GridExportLimitController(
                    name="export_limit",
                    priority=exp_cfg["priority"],
                    export_limit_w=exp_cfg["limit_w"],
                    connection_point_active_power_channel=exp_cfg["connection_point_active_power_channel"],
                    unit_active_power_channel=exp_cfg["unit_active_power_channel"],
                    unit_active_power_setpoint_channel=exp_cfg["unit_active_power_setpoint_channel"],
                ),
                ConnectionPointPowerController(
                    name="connection_point_active_power",
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
            ConnectionPointImportLimitController(
                name="connection_point_import_limit",
                priority=cp_cfg["priority"],
                import_limit_w=cp_cfg["import_limit_w"],
                connection_point_active_power_channel=cp_cfg["connection_point_active_power_channel"],
                unit_active_power_channel=cp_cfg["unit_active_power_channel"],
                unit_active_power_setpoint_channel=cp_cfg["unit_active_power_setpoint_channel"],
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
            "Available-power headroom: %s <= %s + %g W (priority %d)",
            head_cfg["unit_active_power_setpoint_channel"],
            head_cfg["unit_active_power_channel"],
            head_cfg["headroom_w"], head_cfg["priority"],
        )
        fast_controllers.append(
            SetpointHeadroomLimiter(
                name="setpoint_headroom",
                priority=head_cfg["priority"],
                headroom_w=head_cfg["headroom_w"],
                unit_active_power_channel=head_cfg["unit_active_power_channel"],
                unit_active_power_setpoint_channel=head_cfg["unit_active_power_setpoint_channel"],
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

    ctrl_cfg = site["control"]

    # Field devices from site config. Logical devices that share a TCP endpoint
    # also share one client; each driver keeps its own slave ID and tag prefix.
    device_drivers = build_device_drivers(site["devices"])

    # One resource, multiple field devices (IEC §2.4.1.1): merged tag pool.
    devices = CompositeDriver(device_drivers)

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
    state = SystemState(channels)

    # Fail fast: every controller-bound tag must exist before we start driving
    # hardware (a typo must fail here, not as a mid-cycle KeyError on the bus),
    # and measurements/setpoints must land on channels of the right direction.
    validate_bindings(site, [c.name for c in channels])
    validate_binding_directions(site, channels)

    tasks = build_tasks(site)
    allocator, board = build_allocation(site)
    recorder = build_recorder(site, [c.name for c in channels])

    logger.info(
        "EMS built: %d devices, %d channels, tasks=%s, allocator channels=%s",
        len(device_drivers), len(channels), [t.name for t in tasks],
        allocator.channels,
    )
    return Scheduler(
        tasks=tasks, state=state, driver=driver, allocator=allocator, board=board,
        recorder=recorder,
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
