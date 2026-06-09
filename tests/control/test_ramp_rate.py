import pytest

from pyems.control.ramp_rate import RampLimits, RampRateLimiter, apply_ramp_limit


def test_from_percent_per_minute():
    limits = RampLimits.from_percent_per_minute(60_000, up_pct_per_min=10)
    assert limits.rate_up == pytest.approx(100.0)
    assert limits.rate_down == pytest.approx(100.0)


def test_from_percent_per_minute_asymmetric():
    limits = RampLimits.from_percent_per_minute(60_000, 10, down_pct_per_min=60)
    assert limits.rate_up == pytest.approx(100.0)
    assert limits.rate_down == pytest.approx(600.0)


def test_ramp_up_clamped():
    limiter = RampRateLimiter(RampLimits(rate_up=10.0, rate_down=10.0), initial=0.0)
    assert limiter.step(target=1000.0, dt=1.0) == pytest.approx(10.0)
    assert limiter.step(target=1000.0, dt=1.0) == pytest.approx(20.0)


def test_ramp_down_clamped():
    limiter = RampRateLimiter(RampLimits(rate_up=10.0, rate_down=5.0), initial=100.0)
    assert limiter.step(target=0.0, dt=1.0) == pytest.approx(95.0)


def test_unlimited_passes_through():
    limiter = RampRateLimiter(RampLimits(), initial=0.0)
    assert limiter.step(target=12345.0, dt=0.1) == pytest.approx(12345.0)


def test_apply_ramp_limit_vectorised():
    out = apply_ramp_limit(
        [100.0, 100.0, 100.0],
        RampLimits(rate_up=10.0, rate_down=10.0),
        dt=1.0,
        initial=0.0,
    )
    assert out == pytest.approx([10.0, 20.0, 30.0])


def test_dt_validation():
    limiter = RampRateLimiter(RampLimits(rate_up=1.0, rate_down=1.0))
    with pytest.raises(ValueError):
        limiter.step(1.0, dt=0.0)
