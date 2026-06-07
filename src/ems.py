"""
EMS Configuration.
Device channels are derived from profile files (data), not hardcoded.
Channel naming follows Elum ePowerControl Manual §2.5.1 Table 3.
"""
from pathlib import Path

from src.channels import Channel, SystemState
from src.controllers.power_balance import PowerBalanceController
from src.drivers.modbus_device import ModbusDeviceDriver
from src.scheduler import Scheduler, Task

PROFILES = Path(__file__).resolve().parent.parent / "profiles"

FAST_CYCLE_S = 1.0    # safety + balance  (IEC PRIORITY 1)
SLOW_CYCLE_S = 900.0  # optimization      (IEC PRIORITY 5)


def build_ems() -> Scheduler:
    pv_driver = ModbusDeviceDriver.from_profile(
        PROFILES / "inverters" / "huawei_sun2000_100ktl_m1.yaml",
        host="192.168.1.100",
        slave_id=1,
    )
    pv_driver.connect()

    # Channels come from the device profile — no hardcoded register list here.
    channels = pv_driver.channels()
    state = SystemState(channels)

    fast_task = Task(
        name="fast",
        interval_s=FAST_CYCLE_S,
        priority=1,
        controllers=[PowerBalanceController(cycle_s=FAST_CYCLE_S)],
    )

    return Scheduler(tasks=[fast_task], state=state, driver=pv_driver)


if __name__ == "__main__":
    build_ems().run()
