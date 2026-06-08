"""
EMS configuration loader.

Nothing here is site-specific or scenario-specific. Device addresses come from
profiles/ (data); which devices exist, the tunable setpoints/thresholds, AND
which controllers run in which tasks all come from config/site.yaml (data).
This module only wires objects together — no controller, task or binding is
hardcoded, so a new scenario or equipment combination is a YAML change.

IEC 61131-3 analogy: build_ems() assembles the RESOURCE (Scheduler) with its
TASKs and FUNCTION_BLOCKs, binding I/O per the CONFIGURATION in site.yaml.
"""
import logging
from pathlib import Path

import yaml

import pyems.controllers  # noqa: F401  — import populates the controller registry
from pyems.channels import Channel, SystemState
from pyems.controllers import BuildContext, build_controller
from pyems.controllers.safety import SAFE_MODE_CHANNEL
from pyems.drivers.cached import CachedDriver
from pyems.drivers.composite import CompositeDriver
from pyems.drivers.modbus_device import ModbusDeviceDriver
from pyems.logging_config import setup_logging
from pyems.scheduler import Scheduler, Task

logger = logging.getLogger(__name__)

# src/pyems/ems.py → parents[2] is the repo root holding profiles/ and config/.
ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "profiles"
DEFAULT_SITE = ROOT / "config" / "site.yaml"


def build_ems(site_path: str | Path = DEFAULT_SITE) -> Scheduler:
    logger.info("Building EMS from %s", site_path)
    site = yaml.safe_load(Path(site_path).read_text(encoding="utf-8"))

    ctrl_cfg = site["control"]

    # Field devices from site config — one ModbusDeviceDriver per entry.
    # dev["id"] namespaces the device's tags (pv.W → pv1.W) so identical
    # devices don't collide in the merged tag pool.
    device_drivers = [
        ModbusDeviceDriver.from_profile(
            PROFILES / dev["profile"],
            host=dev["host"],
            slave_id=dev["slave_id"],
            prefix=dev.get("id"),
        )
        for dev in site["devices"]
    ]

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

    # Tag pool the scenario's bindings are validated against (fail fast on a
    # mistyped tag, rather than KeyError mid-cycle on hardware).
    channel_names = frozenset(ch.name for ch in channels)
    writable_names = frozenset(ch.name for ch in channels if ch.writable)

    # Build TASKs and their FUNCTION_BLOCKs declaratively from site.yaml.
    # PRIORITY 0 runs first (e.g. safety interlock); higher number = lower
    # priority. Each controller is resolved by `type` via the registry.
    tasks = []
    for tcfg in site["tasks"]:
        ctx = BuildContext(
            cycle_s=tcfg["interval_s"],
            channel_names=channel_names,
            writable_names=writable_names,
        )
        controllers = [build_controller(spec, ctx) for spec in tcfg["controllers"]]
        tasks.append(
            Task(
                name=tcfg["name"],
                interval_s=tcfg["interval_s"],
                priority=tcfg["priority"],
                controllers=controllers,
            )
        )

    logger.info(
        "EMS built: %d devices, %d channels, tasks=%s",
        len(device_drivers), len(channels),
        [(t.name, t.priority, [type(c).__name__ for c in t.controllers]) for t in tasks],
    )
    return Scheduler(tasks=tasks, state=state, driver=driver)


def main() -> None:
    """Console entry point (see [project.scripts] in pyproject.toml)."""
    setup_logging()
    build_ems().run()


if __name__ == "__main__":
    main()
