import pytest

from pyems.sim.plant import (
    GeneratingUnitSim,
    SimWorld,
    meter_register_fields,
    unit_register_fields,
)
from pyems.sim.sources import ManualSource, SourceBox


def settle(unit: GeneratingUnitSim, available_w: float, seconds: float = 30.0) -> float:
    for _ in range(int(seconds / 0.2)):
        unit.step(0.2, available_w)
    return unit.active_power_w


def test_unit_tracks_available_when_uncurtailed():
    unit = GeneratingUnitSim(p_max_w=100000.0, tau_s=2.0)
    assert settle(unit, 60000.0) == pytest.approx(60000.0, rel=0.01)


def test_unit_respects_setpoint_cap():
    unit = GeneratingUnitSim(p_max_w=100000.0, tau_s=2.0)
    unit.active_power_setpoint_w = 40000.0
    assert settle(unit, 90000.0) == pytest.approx(40000.0, rel=0.01)


def test_unit_first_order_lag_no_instant_step():
    unit = GeneratingUnitSim(p_max_w=100000.0, tau_s=2.0)
    unit.step(0.2, 100000.0)
    assert unit.active_power_w < 20000.0  # far from target after one tick


def test_unit_ignore_setpoint_fault():
    unit = GeneratingUnitSim(p_max_w=100000.0, tau_s=0.0)
    unit.active_power_setpoint_w = 10000.0
    unit.ignore_setpoint = True
    assert settle(unit, 80000.0) == pytest.approx(80000.0)


def make_world(pv_w: float, load_w: float, tau_s: float = 0.0) -> SimWorld:
    return SimWorld(
        SourceBox("pv", ManualSource(pv_w)),
        SourceBox("load", ManualSource(load_w)),
        unit_p_max_w=100000.0,
        unit_tau_s=tau_s,
        meter_noise_w=0.0,
    )


def test_connection_point_sign_convention():
    world = make_world(pv_w=5000.0, load_w=20000.0)
    snap = world.tick(0.0)
    # producing less than the load → importing → P_cp positive
    assert snap["connection_point_w"] == pytest.approx(15000.0)

    world = make_world(pv_w=50000.0, load_w=20000.0)
    snap = world.tick(0.0)
    # producing more than the load → exporting → P_cp negative
    assert snap["connection_point_w"] == pytest.approx(-30000.0)


def test_setpoint_write_path_reaches_unit():
    world = make_world(pv_w=80000.0, load_w=10000.0)
    world.set_unit_active_power_setpoint_w(25000.0)
    snap = world.tick(0.0)
    assert snap["unit_active_power_w"] == pytest.approx(25000.0)
    assert snap["unit_active_power_setpoint_w"] == 25000.0


def test_register_fields_cover_profile_channels():
    import random
    world = make_world(pv_w=30000.0, load_w=10000.0)
    snap = world.tick(0.0)
    rng = random.Random(0)
    unit = unit_register_fields(snap, rng)
    meter = meter_register_fields(snap, rng)
    assert unit["W"] == pytest.approx(30000.0)
    assert meter["W"] == pytest.approx(-20000.0)
    # every read-only field of the real profiles must be served
    from pyems.drivers.modbus_device import DeviceProfile
    from pyems.ems import PROFILES
    pv_profile = DeviceProfile.load(PROFILES / "inverters/huawei_sun2000_100ktl_m1.yaml")
    meter_profile = DeviceProfile.load(PROFILES / "meters/example_grid_meter.yaml")
    for reg in pv_profile.registers:
        field = reg.channel.split(".", 1)[-1]
        if not reg.writable:
            assert field in unit, f"unit field {field} missing"
    for reg in meter_profile.registers:
        field = reg.channel.split(".", 1)[-1]
        assert field in meter, f"meter field {field} missing"
