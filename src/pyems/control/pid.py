"""Discrete-time PID controller for scalar control loops."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

PlantStep = Callable[[float, float, float], float]
"""Plant model signature for `simulate`: (command, prev_measurement, dt) -> measurement."""


@dataclass
class PIDGains:
    """PID tuning parameters."""

    kp: float = 1.0
    ki: float = 0.0
    kd: float = 0.0
    n_filter: float = 10.0
    tt: float | None = None

    def tracking_time(self) -> float:
        """Anti-windup tracking time constant Tt [s]."""
        if self.tt is not None and self.tt > 0:
            return float(self.tt)
        ti = self.kp / self.ki if self.ki > 0 else math.inf
        td = self.kd / self.kp if self.kp > 0 else 0.0
        if math.isfinite(ti) and td > 0:
            return math.sqrt(ti * td)
        if math.isfinite(ti):
            return ti
        return 1.0


class PIDController:
    """Stateful discrete PID controller with back-calculation anti-windup."""

    def __init__(
        self,
        gains: PIDGains,
        out_min: float = -math.inf,
        out_max: float = math.inf,
        derivative_on_measurement: bool = True,
    ) -> None:
        if out_min > out_max:
            raise ValueError(f"out_min ({out_min}) must be <= out_max ({out_max})")

        self.gains = gains
        self.out_min = float(out_min)
        self.out_max = float(out_max)
        self.derivative_on_measurement = derivative_on_measurement
        self.reset()

    def reset(self, integral: float = 0.0) -> None:
        """Clear internal state. Optionally seed the integrator."""
        self._integral = float(integral)
        self._deriv = 0.0
        self._prev_meas: float | None = None
        self._prev_error: float | None = None
        self._last_output = 0.0
        self._initialized = False

    @property
    def integral(self) -> float:
        return self._integral

    @property
    def last_output(self) -> float:
        return self._last_output

    def track_applied_output(self, applied: float, requested: float, dt: float) -> None:
        """Back-calculate external saturation into the integrator.

        This is used when a downstream stage, such as PowerAllocator ramping or
        envelope clamping, applied a different total command than the controller
        requested on the previous cycle.
        """
        self._check_dt(dt)
        if self.gains.ki == 0.0:
            return
        self._integral += self._tracking_beta(dt) * (float(applied) - float(requested))

    def step(self, setpoint: float, measurement: float, dt: float) -> float:
        """Advance the controller by one sample."""
        self._check_dt(dt)

        g = self.gains
        error = setpoint - measurement
        p_term = g.kp * error
        d_term = self._derivative_term(error, measurement, dt)

        integral_candidate = self._integral + g.ki * error * dt
        u_unsat = p_term + integral_candidate + d_term
        u_sat = self._clamp(u_unsat)

        self._integral = integral_candidate
        if g.ki != 0.0 and u_sat != u_unsat:
            self._integral += self._tracking_beta(dt) * (u_sat - u_unsat)

        self._prev_meas = measurement
        self._prev_error = error
        self._last_output = u_sat
        self._initialized = True
        return u_sat

    def _derivative_term(self, error: float, measurement: float, dt: float) -> float:
        g = self.gains
        if g.kd == 0.0 or not self._initialized:
            return 0.0

        if self.derivative_on_measurement:
            raw = -(measurement - self._prev_meas) / dt
        else:
            raw = (error - self._prev_error) / dt

        if math.isfinite(g.n_filter):
            alpha = dt / (g.kd / (g.kp * g.n_filter) + dt) if g.kp != 0 else 1.0
            alpha = min(max(alpha, 0.0), 1.0)
            self._deriv += alpha * (g.kd * raw - self._deriv)
        else:
            self._deriv = g.kd * raw
        return self._deriv

    def _tracking_beta(self, dt: float) -> float:
        """Fraction of the saturation excess fed back into the integrator."""
        tt = self.gains.tracking_time()
        return min(dt / tt, 1.0) if math.isfinite(tt) and tt > 0 else 0.0

    @staticmethod
    def _check_dt(dt: float) -> None:
        if dt <= 0:
            raise ValueError(f"dt must be > 0, got {dt}")

    def _clamp(self, value: float) -> float:
        return min(max(value, self.out_min), self.out_max)


@dataclass
class PIDSimResult:
    """Result of an offline PID simulation."""

    time: list[float] = field(default_factory=list)
    output: list[float] = field(default_factory=list)
    measurement: list[float] = field(default_factory=list)
    setpoint: list[float] = field(default_factory=list)


def simulate(
    pid: PIDController,
    setpoints: Sequence[float],
    plant_step: PlantStep,
    dt: float,
    measurement0: float = 0.0,
) -> PIDSimResult:
    """Run the controller against a user-supplied plant model."""
    pid.reset()
    res = PIDSimResult()
    meas = float(measurement0)
    t = 0.0
    for sp in setpoints:
        cmd = pid.step(setpoint=float(sp), measurement=meas, dt=dt)
        meas = float(plant_step(cmd, meas, dt))
        res.time.append(t)
        res.output.append(cmd)
        res.measurement.append(meas)
        res.setpoint.append(float(sp))
        t += dt
    return res


def first_order_plant(tau: float, gain: float = 1.0) -> PlantStep:
    """Build a simple first-order lag plant for `simulate`."""

    def _step(u: float, y_prev: float, dt: float) -> float:
        return y_prev + (dt / tau) * (gain * u - y_prev)

    return _step


__all__ = [
    "PIDGains",
    "PIDController",
    "PIDSimResult",
    "PlantStep",
    "simulate",
    "first_order_plant",
]
