"""Power ramp-rate limiter for grid-code compliant active-power control."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class RampLimits:
    """Ramp-rate limits expressed as power per second."""

    rate_up: float = math.inf
    rate_down: float = math.inf

    def __post_init__(self) -> None:
        if self.rate_up < 0 or self.rate_down < 0:
            raise ValueError("ramp rates must be non-negative")

    @classmethod
    def from_percent_per_minute(
        cls,
        p_rated: float,
        up_pct_per_min: float,
        down_pct_per_min: float | None = None,
    ) -> RampLimits:
        down = up_pct_per_min if down_pct_per_min is None else down_pct_per_min
        return cls(
            rate_up=p_rated * (up_pct_per_min / 100.0) / 60.0,
            rate_down=p_rated * (down / 100.0) / 60.0,
        )


class RampRateLimiter:
    """Stateful slew-rate limiter."""

    def __init__(self, limits: RampLimits, initial: float = 0.0) -> None:
        self.limits = limits
        self._value = float(initial)

    @property
    def value(self) -> float:
        return self._value

    def reset(self, value: float = 0.0) -> None:
        self._value = float(value)

    def step(self, target: float, dt: float) -> float:
        if dt <= 0:
            raise ValueError(f"dt must be > 0, got {dt}")

        lo = self._value - self.limits.rate_down * dt
        hi = self._value + self.limits.rate_up * dt
        self._value = min(max(target, lo), hi)
        return self._value


def apply_ramp_limit(
    targets: Sequence[float],
    limits: RampLimits,
    dt: float,
    initial: float = 0.0,
) -> list[float]:
    """Apply the limiter over a whole target sequence at a fixed sample time."""
    limiter = RampRateLimiter(limits, initial=initial)
    return [limiter.step(float(t), dt) for t in targets]


__all__ = [
    "RampLimits",
    "RampRateLimiter",
    "apply_ramp_limit",
]
