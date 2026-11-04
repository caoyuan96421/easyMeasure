"""A tiny simulated "physical world" shared by all simulated instruments.

Sources write their outputs into the world (``world.set("src.V1", 0.3)``) and
detectors compute their readings from it, so a sweep over a simulated source
produces a meaningful simulated measurement (Coulomb-diamond like current maps,
spectra that shift with field, a camera spot that moves with two voltages, ...).
"""

from __future__ import annotations

import threading
import time
from typing import Dict

import numpy as np


class SimWorld:
    def __init__(self):
        self._vals: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.rng = np.random.default_rng()
        self.t0 = time.time()

    def set(self, key: str, value) -> None:
        with self._lock:
            self._vals[key] = value

    def get(self, key: str, default=0.0):
        with self._lock:
            return self._vals.get(key, default)

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._vals)

    def reset(self) -> None:
        with self._lock:
            self._vals.clear()

    # -- models -----------------------------------------------------------------
    def current(self, vg: float, vb: float, b: float = 0.0, noise: float = 0.02) -> float:
        """Coulomb-blockade style current (nA) through a simulated quantum dot."""
        ec = 0.2            # charging energy (V of bias)
        period = 0.25       # gate period (V)
        lever = 0.4
        phase = ((vg + 0.03 * b) / period) % 1.0 - 0.5     # -0.5..0.5 distance to degeneracy
        e = abs(phase) * period * lever * 2.0
        # conduction if |bias| exceeds the detuning-dependent gap
        gap = max(ec * (2 * abs(phase)) - 0.0, 0.0)
        width = 0.01
        i = np.tanh(vb / 0.05) * (1.0 / (1.0 + np.exp((gap - abs(vb)) / width)))
        i += 0.15 * np.exp(-(e / 0.004) ** 2) * np.tanh(vb / 0.01)  # sharp peaks near zero bias
        i *= 1.0 + 0.1 * np.cos(2 * np.pi * b / 2.0)
        return float(i + noise * 0.05 * self.rng.standard_normal())

    def lockin(self, vg: float, vb: float, b: float = 0.0, amp: float = 0.01):
        """Differential conductance (numerical derivative of current)."""
        dv = max(abs(amp), 1e-4)
        g = (self.current(vg, vb + dv, b, 0) - self.current(vg, vb - dv, b, 0)) / (2 * dv)
        x = g * amp + 1e-4 * self.rng.standard_normal()
        y = 0.05 * x + 1e-4 * self.rng.standard_normal()
        return float(x), float(y)

    def spectrum(self, freqs: np.ndarray, b: float, v: float) -> np.ndarray:
        f0 = 5.0 + 0.8 * b + 0.3 * v
        gamma = 0.05 + 0.02 * abs(v)
        s = 1.0 / (1.0 + ((freqs - f0) / gamma) ** 2)
        s += 0.4 / (1.0 + ((freqs - (f0 * 1.5)) / (2 * gamma)) ** 2)
        return s + 0.01 * self.rng.standard_normal(freqs.shape)

    def image(self, nx: int, ny: int, x0: float, y0: float, exposure: float) -> np.ndarray:
        x = np.linspace(-1, 1, nx)[None, :]
        y = np.linspace(-1, 1, ny)[:, None]
        spot = np.exp(-((x - x0) ** 2 + (y - y0) ** 2) / 0.05)
        return exposure * (spot + 0.05 * self.rng.standard_normal((ny, nx)))


WORLD = SimWorld()
