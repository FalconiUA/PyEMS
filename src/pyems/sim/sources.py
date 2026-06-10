"""Value sources for simulated quantities (PV available power, site load).

A SourceBox holds one active source and can be re-pointed at runtime from the
sim UI without restarting anything:

  - manual     : a fixed value the operator types/slides
  - synthetic  : base + amplitude * sin(2*pi*t/period) + gaussian noise
  - replay     : recorded 1-second samples (e.g. real PV / load data) played
                 back at a configurable speed, looping or holding the last value

All values are active power in W (generating-unit convention for PV).
"""
from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass


def parse_csv_series(text: str) -> list[float]:
    """Parse a 1-sample-per-line series; one value per second of recording.

    Accepted line shapes (auto-detected per line, header lines skipped):
        1234.5
        2024-06-01 12:00:00,1234.5
        12:00:00;1234.5
    The LAST numeric field on each line is the sample.
    """
    samples: list[float] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [f for chunk in line.split(";") for f in chunk.split(",")]
        value = None
        for f in reversed(fields):
            f = f.strip()
            try:
                value = float(f)
                break
            except ValueError:
                continue
        if value is not None and math.isfinite(value):
            samples.append(value)
    return samples


@dataclass
class ManualSource:
    value_w_fixed: float

    def value_w(self, t_s: float) -> float:
        return self.value_w_fixed

    def describe(self) -> dict:
        return {"mode": "manual", "value_w": self.value_w_fixed}


class SyntheticSource:
    """base + amplitude*sin(2*pi*t/period) + noise, clamped to >= floor_w.

    PV shape: base_w=0, amplitude_w=peak → half-sine "days" with zero "nights".
    Load shape: base_w=mean, amplitude_w=swing.
    """

    def __init__(
        self,
        base_w: float,
        amplitude_w: float,
        period_s: float = 600.0,
        noise_w: float = 0.0,
        floor_w: float = 0.0,
        seed: int | None = None,
    ) -> None:
        if period_s <= 0:
            raise ValueError("period_s must be > 0")
        self.base_w = base_w
        self.amplitude_w = amplitude_w
        self.period_s = period_s
        self.noise_w = noise_w
        self.floor_w = floor_w
        self._rng = random.Random(seed)

    def value_w(self, t_s: float) -> float:
        value = self.base_w + self.amplitude_w * math.sin(2 * math.pi * t_s / self.period_s)
        if self.noise_w:
            value += self._rng.gauss(0.0, self.noise_w)
        return max(self.floor_w, value)

    def describe(self) -> dict:
        return {
            "mode": "synthetic",
            "base_w": self.base_w,
            "amplitude_w": self.amplitude_w,
            "period_s": self.period_s,
            "noise_w": self.noise_w,
        }


class ReplaySource:
    """Play back recorded samples (one per second of recording time).

    `speed` compresses/stretches time (2.0 = a recorded hour passes in 30 min).
    At the end: loop, or hold the last sample (a recording that ran out must
    not snap the plant to zero mid-experiment).
    """

    def __init__(
        self,
        samples: list[float],
        speed: float = 1.0,
        loop: bool = True,
        start_at_s: float = 0.0,
    ) -> None:
        if not samples:
            raise ValueError("replay needs at least one sample")
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.samples = samples
        self.speed = speed
        self.loop = loop
        self._t0: float | None = None
        self._offset_s = start_at_s
        self._index = 0

    def value_w(self, t_s: float) -> float:
        if self._t0 is None:
            self._t0 = t_s
        idx = int((t_s - self._t0) * self.speed + self._offset_s)
        if self.loop:
            idx %= len(self.samples)
        else:
            idx = min(idx, len(self.samples) - 1)
        self._index = idx
        return self.samples[idx]

    def describe(self) -> dict:
        return {
            "mode": "replay",
            "speed": self.speed,
            "loop": self.loop,
            "index": self._index,
            "total": len(self.samples),
        }


class SourceBox:
    """Thread-safe holder of the currently active source for one quantity.

    The sim world thread samples it every tick; the UI thread swaps the source
    or tweaks manual values concurrently.
    """

    def __init__(self, name: str, source) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._source = source
        self._last_w = 0.0

    def value_w(self, t_s: float) -> float:
        with self._lock:
            self._last_w = float(self._source.value_w(t_s))
            return self._last_w

    def set_source(self, source) -> None:
        with self._lock:
            self._source = source

    def describe(self) -> dict:
        with self._lock:
            info = self._source.describe()
            info["last_w"] = self._last_w
            return info
