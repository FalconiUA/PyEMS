"""
EMS configuration loader.

Nothing here is site-specific: device addresses, setpoints and safety
thresholds all come from config/site.yaml (data). Device register maps come
from profiles/ (data). This module only wires objects together.

IEC 61131-3 analogy: build_ems() assembles the RESOURCE (Scheduler) with its
TASKs and FUNCTION_BLOCKs, binding I/O per the CONFIGURATION in site.yaml.
"""
from pathlib import Path

import yaml

from src.channels import Channel, SystemState
from src.controllers.grid_export_limit import GridExportLimitController
from src.controllers.safety import SAFE_MODE_CHANNEL, SafetyController
from src.drivers.cached import CachedDriver
from src.drivers.composite import CompositeDriver
from src.drivers.modbus_device import ModbusDeviceDriver
from src.scheduler import Scheduler, Task

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "profiles"
DEFAULT_SITE = ROOT / "config" / "site.yaml"


def build_ems(site_path: str | Path = DEFAULT_SITE) -> Scheduler:
    site = yaml.safe_load(Path(site_path).read_text(encoding="utf-8"))

    ctrl_cfg = site["control"]
    exp_cfg = site["export_limit"]
    safe_cfg = site["safety"]
    fast_cycle_s = ctrl_cfg["fast_cycle_s"]

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
                cycle_s=fast_cycle_s,
                export_limit_w=exp_cfg["limit_w"],
                p_max_w=exp_cfg["p_max_w"],
                connection_point_active_power_channel=exp_cfg["connection_point_active_power_channel"],
                unit_active_power_channel=exp_cfg["unit_active_power_channel"],
                unit_active_power_setpoint_channel=exp_cfg["unit_active_power_setpoint_channel"],
            ),
        ],
    )

    return Scheduler(tasks=[safety_task, fast_task], state=state, driver=driver)


if __name__ == "__main__":
    build_ems().run()
