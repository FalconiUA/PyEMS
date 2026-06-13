"""Plant model: one generating unit + the connection-point meter.

Pure simulation physics — no Modbus, no threads — so it is unit-testable and
the register/server layer (device.py) stays a thin codec around it.

Sign conventions match the controllers:
  - unit active power P >= 0 (generating-unit convention, injection into AC bus)
  - connection point P_cp: import positive, export negative
        P_cp = site load - unit production
"""
from __future__ import annotations

import math
import random
import threading

from pyems.sim.sources import SourceBox

NOMINAL_PHASE_VOLTAGE_V = 230.0
NOMINAL_FREQUENCY_HZ = 50.0


class GeneratingUnitSim:
    """A PV-style generating unit that tracks min(available, setpoint).

    First-order lag with time constant `tau_s` models the inverter's actual
    response to an active power setpoint — the EMS never sees an instant step,
    just like with real iron.
    """

    def __init__(self, p_max_w: float, tau_s: float = 2.0) -> None:
        if p_max_w <= 0:
            raise ValueError("p_max_w must be > 0")
        self.p_max_w = p_max_w
        self.tau_s = tau_s
        self.active_power_w = 0.0
        # Until the EMS writes the setpoint register, the unit follows whatever
        # is available — a real inverter's default derate is its rated power.
        self.active_power_setpoint_w = p_max_w
        # Fault: remote power control disabled — the unit ignores the setpoint
        # (drives SetpointComplianceMonitor in the EMS).
        self.ignore_setpoint = False
        # Hard remote switch: when stopped, the inverter is de-energized and
        # produces nothing regardless of available resource or setpoint.
        self.enabled = True

    def step(self, dt_s: float, available_w: float) -> float:
        if not self.enabled:
            target = 0.0
        else:
            cap = self.p_max_w if self.ignore_setpoint else self.active_power_setpoint_w
            target = max(0.0, min(available_w, cap, self.p_max_w))
        if self.tau_s <= 0:
            self.active_power_w = target
        else:
            alpha = 1.0 - math.exp(-dt_s / self.tau_s)
            self.active_power_w += (target - self.active_power_w) * alpha
        return self.active_power_w


class SimWorld:
    """Owns the unit + meter physics and the value sources; ticks in sim time.

    Thread contract: tick() runs on the world thread; setters (setpoint from
    the Modbus write path, fault flags and source swaps from the UI thread)
    are serialized with the same lock.
    """

    def __init__(
        self,
        unit_available_source: SourceBox,
        load_source: SourceBox,
        unit_p_max_w: float,
        unit_tau_s: float = 2.0,
        meter_noise_w: float = 25.0,
        seed: int | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.unit_available_source = unit_available_source
        self.load_source = load_source
        self.unit = GeneratingUnitSim(unit_p_max_w, unit_tau_s)
        self.meter_noise_w = meter_noise_w
        self._rng = random.Random(seed)
        self._last_t: float | None = None
        self._snapshot: dict[str, float] = {}

    def set_unit_active_power_setpoint_w(self, value_w: float) -> None:
        """Called from the Modbus server thread when the EMS writes WSet."""
        with self._lock:
            self.unit.active_power_setpoint_w = value_w

    def set_ignore_setpoint(self, active: bool) -> None:
        with self._lock:
            self.unit.ignore_setpoint = active

    def set_unit_enabled(self, enabled: bool) -> None:
        """Hard remote start/stop from EMS command register(s)."""
        with self._lock:
            self.unit.enabled = enabled

    def tick(self, now_s: float) -> dict[str, float]:
        available_w = self.unit_available_source.value_w(now_s)
        load_w = self.load_source.value_w(now_s)
        with self._lock:
            dt = 0.0 if self._last_t is None else max(0.0, now_s - self._last_t)
            self._last_t = now_s
            unit_w = self.unit.step(dt, available_w)
            cp_w = load_w - unit_w
            if self.meter_noise_w:
                cp_w += self._rng.gauss(0.0, self.meter_noise_w)
            self._snapshot = {
                "t_s": now_s,
                "unit_available_w": available_w,
                "unit_active_power_w": unit_w,
                "unit_active_power_setpoint_w": self.unit.active_power_setpoint_w,
                "load_w": load_w,
                "connection_point_w": cp_w,
            }
            return dict(self._snapshot)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._snapshot)


def _jitter(rng: random.Random, value: float, spread: float) -> float:
    return value + rng.uniform(-spread, spread)


def unit_register_fields(snap: dict[str, float], rng: random.Random) -> dict[str, float]:
    """Profile-local field values (`<field>` part of the channel tag) for the
    generating unit, derived from a world snapshot. Fields absent from a given
    device profile are simply ignored by the register codec."""
    w = snap.get("unit_active_power_w", 0.0)
    phase_a = w / (3 * NOMINAL_PHASE_VOLTAGE_V) if w > 0 else 0.0
    return {
        "W": w,
        "VA": w,
        "VAR": 0.0,
        "Hz": _jitter(rng, NOMINAL_FREQUENCY_HZ, 0.01),
        "PhVphA": _jitter(rng, NOMINAL_PHASE_VOLTAGE_V, 0.3),
        "PhVphB": _jitter(rng, NOMINAL_PHASE_VOLTAGE_V, 0.3),
        "PhVphC": _jitter(rng, NOMINAL_PHASE_VOLTAGE_V, 0.3),
        "AphA": phase_a,
        "AphB": phase_a,
        "AphC": phase_a,
        "Status": 512.0,  # Huawei: on-grid
        "Alarm": 0.0,
    }


def meter_register_fields(snap: dict[str, float], rng: random.Random) -> dict[str, float]:
    """Connection-point meter fields from a world snapshot (import positive)."""
    w = snap.get("connection_point_w", 0.0)
    per_phase = w / 3
    amps = abs(per_phase) / NOMINAL_PHASE_VOLTAGE_V
    return {
        "W": w,
        "WphA": per_phase,
        "WphB": per_phase,
        "WphC": per_phase,
        "VAR": 0.0,
        "VARphA": 0.0,
        "VARphB": 0.0,
        "VARphC": 0.0,
        "VA": abs(w),
        "VAphA": abs(per_phase),
        "VAphB": abs(per_phase),
        "VAphC": abs(per_phase),
        "Hz": _jitter(rng, NOMINAL_FREQUENCY_HZ, 0.01),
        "PhVphA": _jitter(rng, NOMINAL_PHASE_VOLTAGE_V, 0.3),
        "PhVphB": _jitter(rng, NOMINAL_PHASE_VOLTAGE_V, 0.3),
        "PhVphC": _jitter(rng, NOMINAL_PHASE_VOLTAGE_V, 0.3),
        "AphA": amps,
        "AphB": amps,
        "AphC": amps,
    }
