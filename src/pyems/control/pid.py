"""Discrete-time PID controller for scalar control loops."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass
class PIDGains:
    """PID tuning parameters."""

    kp: float = 1.0
    ki: float = 0.0
    kd: float = 0.0
    n_filter: float = 10.0
    tt: Optional[float] = None

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

        self._integral: float = 0.0
        self._deriv: float = 0.0
        self._prev_meas: Optional[float] = None
        self._prev_error: Optional[float] = None
        self._last_output: float = 0.0
        self._initialized: bool = False

    def reset(self, integral: float = 0.0) -> None:
        """Clear internal state. Optionally seed the integrator."""
        self._integral = float(integral)
        self._deriv = 0.0
        self._prev_meas = None
        self._prev_error = None
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
        if dt <= 0:
            raise ValueError(f"dt must be > 0, got {dt}")
        if self.gains.ki == 0.0:
            return
        tt = self.gains.tracking_time()
        beta = min(dt / tt, 1.0) if math.isfinite(tt) and tt > 0 else 0.0
        self._integral += beta * (float(applied) - float(requested))

    def step(self, setpoint: float, measurement: float, dt: float) -> float:
        """Advance the controller by one sample."""
        if dt <= 0:
            raise ValueError(f"dt must be > 0, got {dt}")

        g = self.gains
        error = setpoint - measurement
        p_term = g.kp * error

        d_term = 0.0
        if g.kd != 0.0 and self._initialized:
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
            d_term = self._deriv

        integral_candidate = self._integral + g.ki * error * dt
        u_unsat = p_term + integral_candidate + d_term
        u_sat = self._clamp(u_unsat)

        if g.ki != 0.0:
            tt = g.tracking_time()
            beta = min(dt / tt, 1.0) if math.isfinite(tt) and tt > 0 else 0.0
            self._integral = integral_candidate + beta * (u_sat - u_unsat)
        else:
            self._integral = integral_candidate

        self._prev_meas = measurement
        self._prev_error = error
        self._last_output = u_sat
        self._initialized = True
        return u_sat

    def _clamp(self, value: float) -> float:
        return min(max(value, self.out_min), self.out_max)


@dataclass
class PIDSimResult:
    """Result of an offline PID simulation."""

    time: List[float] = field(default_factory=list)
    output: List[float] = field(default_factory=list)
    measurement: List[float] = field(default_factory=list)
    setpoint: List[float] = field(default_factory=list)


def simulate(
    pid: PIDController,
    setpoints: Sequence[float],
    plant_step,
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


def first_order_plant(tau: float, gain: float = 1.0):
    """Build a simple first-order lag plant for `simulate`."""

    def _step(u: float, y_prev: float, dt: float) -> float:
        return y_prev + (dt / tau) * (gain * u - y_prev)

    return _step


__all__ = [
    "PIDGains",
    "PIDController",
    "PIDSimResult",
    "simulate",
    "first_order_plant",
]
