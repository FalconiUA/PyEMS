"""
EMS configuration loader.

Nothing here is site-specific: device addresses, setpoints and safety
thresholds all come from config/site.yaml (data). Device register maps come
from profiles/ (data). This module only wires objects together.

IEC 61131-3 analogy: build_ems() assembles the RESOURCE (Scheduler) with its
TASKs and FUNCTION_BLOCKs, binding I/O per the CONFIGURATION in site.yaml.
"""
import logging
from pathlib import Path

import yaml

from pyems.allocation.allocator import PowerAllocator, SetpointChannelConfig
from pyems.allocation.request import RequestBoard
from pyems.channels import Channel, SystemState
from pyems.controllers.grid_export_limit import GridExportLimitController
from pyems.controllers.safety import SAFE_MODE_CHANNEL, SafetyController
from pyems.drivers.cached import CachedDriver
from pyems.drivers.composite import CompositeDriver
import pyems.drivers.modbus_device as md
from pyems.logging_config import setup_logging
from pyems.scheduler import Scheduler, Task

logger = logging.getLogger(__name__)

# src/pyems/ems.py → parents[2] is the repo root holding profiles/ and config/.
ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "profiles"
DEFAULT_SITE = ROOT / "config" / "site.yaml"


def required_channels(site: dict) -> list[str]:
    """All tags the controllers will read/drive, per site.yaml bindings.

    Used to fail fast at startup: a typo in a binding must blow up here, not
    mid-control as a KeyError deep inside a controller on live hardware.
    """
    exp_cfg = site["export_limit"]
    safe_cfg = site["safety"]
    alloc_cfg = site["allocation"]
    tags = [
        exp_cfg["connection_point_active_power_channel"],
        exp_cfg["unit_active_power_channel"],
        exp_cfg["unit_active_power_setpoint_channel"],
        SAFE_MODE_CHANNEL,
        *safe_cfg["unit_active_power_setpoint_channels"],
        *(ch["setpoint_channel"] for ch in alloc_cfg["channels"]),
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


def build_tasks(site: dict) -> list[Task]:
    """Build the control tasks (safety + export-limit) from a site config dict.

    Shared by build_ems() (real Modbus) and the simulation harness, so both
    exercise the *same* controllers and tuning — the sim verifies production.
    """
    exp_cfg = site["export_limit"]
    safe_cfg = site["safety"]
    fast_cycle_s = site["control"]["fast_cycle_s"]

    # PRIORITY 0 interlock — runs first, asserts safe state on a dead bus.
    safety_task = Task(
        name="safety",
        interval_s=fast_cycle_s,
        priority=0,
        controllers=[
            SafetyController(
                max_comms_age_s=safe_cfg["max_comms_age_s"],
                safe_active_power_w=exp_cfg["limit_w"],  # export ≤ limit even at zero load
                unit_active_power_setpoint_channels=safe_cfg["unit_active_power_setpoint_channels"],
            ),
        ],
    )

    fast_task = Task(
        name="fast",
        interval_s=fast_cycle_s,
        priority=1,
        controllers=[
            GridExportLimitController(
                name="export_limit",
                priority=exp_cfg["priority"],
                export_limit_w=exp_cfg["limit_w"],
                connection_point_active_power_channel=exp_cfg["connection_point_active_power_channel"],
                unit_active_power_channel=exp_cfg["unit_active_power_channel"],
                unit_active_power_setpoint_channel=exp_cfg["unit_active_power_setpoint_channel"],
            ),
        ],
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
    board = RequestBoard([c.setpoint_channel for c in configs])
    allocator = PowerAllocator(configs, board, cycle_s=fast_cycle_s)
    return allocator, board


def build_device_drivers(devices_cfg: list[dict]) -> list[md.ModbusDeviceDriver]:
    """Build field-device drivers, sharing one TCP client per endpoint."""
    clients: dict[tuple[str, str, int | None], object] = {}
    drivers: list[md.ModbusDeviceDriver] = []
    for dev in devices_cfg:
        profile = md.DeviceProfile.load(PROFILES / dev["profile"])
        port = dev.get(
            "port",
            profile.default_port if profile.protocol == "modbus_tcp" else None,
        )
        key = (profile.protocol, dev["host"], port)
        client = clients.get(key)
        if client is None:
            if profile.protocol == "modbus_tcp":
                client = md.ModbusTcpClient(dev["host"], port=port)
            elif profile.protocol == "modbus_rtu":
                client = md.ModbusSerialClient(port=dev["host"])
            else:
                raise ValueError(f"Unknown protocol: {profile.protocol}")
            clients[key] = client
        drivers.append(
            md.ModbusDeviceDriver(
                profile,
                client=client,
                slave_id=dev["slave_id"],
                prefix=dev.get("id"),
            )
        )
    return drivers


def build_ems(site_path: str | Path = DEFAULT_SITE) -> Scheduler:
    logger.info("Building EMS from %s", site_path)
    site = yaml.safe_load(Path(site_path).read_text(encoding="utf-8"))

    ctrl_cfg = site["control"]

    # Field devices from site config. Logical devices that share a TCP endpoint
    # also share one client; each driver keeps its own slave ID and tag prefix.
    device_drivers = build_device_drivers(site["devices"])

    # One resource, multiple field devices (IEC §2.4.1.1): merged tag pool.
    devices = CompositeDriver(device_drivers)

    # Non-blocking I/O: real Modbus runs in a background thread against a tag
    # cache, so a slow/hung bus transaction never stalls the control cycle.
    driver = CachedDriver(devices, poll_interval_s=ctrl_cfg["poll_interval_s"])
    driver.connect()

    # Channels: device profiles (incl. sys.comms_age_s from CachedDriver) plus
    # the inter-controller interlock tag (system status word, not a register).
    channels = driver.channels() + [
        Channel(SAFE_MODE_CHANNEL, unit="", min_val=0, max_val=1, writable=True),
    ]
    state = SystemState(channels)

    # Fail fast: every controller-bound tag must exist before we start driving
    # hardware (a typo must fail here, not as a mid-cycle KeyError on the bus).
    validate_bindings(site, [c.name for c in channels])

    tasks = build_tasks(site)
    allocator, board = build_allocation(site)

    logger.info(
        "EMS built: %d devices, %d channels, tasks=%s, allocator channels=%s",
        len(device_drivers), len(channels), [t.name for t in tasks],
        allocator.channels,
    )
    return Scheduler(
        tasks=tasks, state=state, driver=driver, allocator=allocator, board=board
    )


def main() -> None:
    """Console entry point (see [project.scripts] in pyproject.toml)."""
    setup_logging()
    build_ems().run()


if __name__ == "__main__":
    main()
