import math

import pytest

from pyems.control.pid import PIDController, PIDGains, first_order_plant, simulate


def test_tracking_time_explicit():
    assert PIDGains(kp=1, ki=1, tt=3.0).tracking_time() == 3.0


def test_tracking_time_derived_from_ti():
    assert PIDGains(kp=2.0, ki=1.0, kd=0.0).tracking_time() == pytest.approx(2.0)


def test_proportional_only():
    pid = PIDController(PIDGains(kp=2.0))
    assert pid.step(setpoint=10.0, measurement=4.0, dt=1.0) == pytest.approx(12.0)


def test_integral_accumulates():
    pid = PIDController(PIDGains(kp=0.0, ki=1.0))
    assert pid.step(setpoint=1.0, measurement=0.0, dt=1.0) == pytest.approx(1.0)
    assert pid.step(setpoint=1.0, measurement=0.0, dt=1.0) == pytest.approx(2.0)


def test_output_saturates_and_integral_stays_bounded():
    pid = PIDController(PIDGains(kp=1.0, ki=5.0, tt=1.0), out_min=0.0, out_max=10.0)
    for _ in range(50):
        assert pid.step(setpoint=100.0, measurement=0.0, dt=1.0) == pytest.approx(10.0)
    assert pid.integral < 50.0


def test_anti_windup_stable_for_large_dt():
    pid = PIDController(PIDGains(kp=0.7, ki=0.6), out_min=0.0, out_max=20_000.0)
    for _ in range(200):
        pid.step(setpoint=20_000.0, measurement=0.0, dt=60.0)
    assert math.isfinite(pid.integral)


def test_external_tracking_unwinds_integrator():
    pid = PIDController(PIDGains(kp=0.0, ki=1.0, tt=1.0))
    pid.step(setpoint=1000.0, measurement=0.0, dt=1.0)
    before = pid.integral
    pid.track_applied_output(applied=100.0, requested=1000.0, dt=1.0)
    assert pid.integral < before


def test_derivative_no_kick_on_setpoint_change():
    pid = PIDController(PIDGains(kp=1.0, kd=5.0), derivative_on_measurement=True)
    pid.step(setpoint=0.0, measurement=0.0, dt=1.0)
    assert pid.step(setpoint=100.0, measurement=0.0, dt=1.0) == pytest.approx(100.0)


def test_simulate_converges_to_setpoint():
    pid = PIDController(PIDGains(kp=0.8, ki=0.5), out_min=0.0, out_max=1000.0)
    res = simulate(pid, [100.0] * 200, first_order_plant(tau=2.0), dt=0.5)
    assert res.measurement[-1] == pytest.approx(100.0, abs=1.0)


def test_dt_must_be_positive():
    pid = PIDController(PIDGains(kp=1.0))
    with pytest.raises(ValueError):
        pid.step(1.0, 0.0, dt=0.0)
