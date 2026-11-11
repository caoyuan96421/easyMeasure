"""A tiny simulated "physical world" shared by all simulated instruments.

Sources write their outputs into the world (``world.set("src.V1", 0.3)``) and
detectors compute their readings from it, so a sweep over a simulated source
produces a meaningful simulated measurement (Coulomb-diamond like current maps,
spectra that shift with field, a camera spot that moves with two voltages, ...).
"""

from __future__ import annotations

import math
import threading
import time
from typing import Dict

import numpy as np


class SimWorld:
    def __init__(self):
        self._vals: Dict[str, float] = {}
        self._dynamic: Dict[str, object] = {}     # key -> function giving its present value
        self._lock = threading.Lock()
        self.rng = np.random.default_rng()
        self.t0 = time.time()

    def set(self, key: str, value) -> None:
        with self._lock:
            self._vals[key] = value

    def get(self, key: str, default=0.0):
        with self._lock:
            fn = self._dynamic.get(key)
            if fn is None:
                return self._vals.get(key, default)
        return fn()                     # outside the lock: it may call set()

    def set_dynamic(self, key: str, fn) -> None:
        """A value that changes by itself (a magnet ramping in real time): ``get``
        calls *fn* for its present value, whether or not anybody polls the source."""
        with self._lock:
            self._dynamic[key] = fn

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._vals)

    def reset(self) -> None:
        with self._lock:
            self._vals.clear()
            self._dynamic.clear()

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

    # -- graphene Hall bar (tutorials) ---------------------------------------------
    #: dual-gated bilayer graphene: gate capacitances in 1e12 cm^-2 per volt
    CB, CT, DOPING = 0.6, 0.9, -0.15
    D_PER_N = 0.0452            # V/nm of displacement field per 1e12 cm^-2 (e/2ε0)

    def graphene(self, vbg: float, vtg: float, b: float = 0.0) -> Dict[str, float]:
        """Carrier density n (1e12 cm^-2), displacement field D (V/nm) and the
        four-probe resistance Rxx (Ω) of a dual-gated bilayer graphene Hall bar:
        a Dirac peak at n = 0 that grows when D opens a band gap, and Landau-level
        minima (fan) in a magnetic field b (T)."""
        n = self.CB * vbg + self.CT * vtg + self.DOPING
        d = self.D_PER_N * (self.CB * vbg - self.CT * vtg)
        gap = 100.0 * abs(d)                                   # meV
        r_cnp = 4.0e3 * math.exp(min(gap, 45.0) / 9.0)         # insulating with the gap
        n_star = 0.08 + 0.02 * gap / 10.0                      # disorder broadening
        r = 150.0 + r_cnp / math.sqrt(1.0 + (n / n_star) ** 2)
        if abs(b) > 1e-3:
            nu = 41.36 * n / abs(b)                            # filling factor
            amp = math.exp(-0.8 / abs(b))                      # Landau-level resolution
            r *= 1.0 - 0.9 * amp * 0.5 * (1.0 + math.cos(2 * math.pi * nu / 4.0))
            r += 15.0 * b * b                                  # magnetoresistance
        r *= 1.0 + 0.004 * self.rng.standard_normal()
        return {"n": n, "D": d, "Rxx": r}

    def gate_leakage(self, v: float, r_leak: float = 2e12, v_bd: float = 8.0) -> float:
        """Leakage current (A) through a gate dielectric that breaks down above
        about ``v_bd`` volts."""
        i = v / r_leak
        x = abs(v) - v_bd
        i += math.copysign(2e-11 * math.exp(min(x, 30.0) / 0.4), v) if x > -6 else 0.0
        return i + 2e-13 * self.rng.standard_normal()

    # -- superconducting resonator (VNA tutorial) ---------------------------------
    def resonator_s11(self, freqs: np.ndarray, power_dbm: float, b: float = 0.0,
                      ifbw: float = 1e3) -> np.ndarray:
        """Complex reflection S11 of a superconducting λ/4 resonator: TLS losses
        saturate with power (deeper, narrower dip), the Kerr effect pulls the
        resonance down at high power, and a magnetic field lowers its frequency
        (kinetic inductance) and adds loss (vortices)."""
        p_w = 10 ** ((power_dbm - 30.0) / 10.0)               # at the VNA port
        p_dev = p_w * 1e-6                                      # 60 dB of attenuation
        f0 = 6.0e9 * (1.0 - 0.0005 * (b / 0.5) ** 2) - 4e5 * p_dev / 1e-9
        k_e = 120e3                                            # coupling linewidth (Hz)
        k_tls = 350e3 / math.sqrt(1.0 + p_dev / 3e-15)         # saturable TLS loss
        k_i = 25e3 + k_tls + 300e3 * abs(b) + 60e3 * p_dev / 1e-9
        delta = freqs - f0
        s = (k_i - k_e + 2j * delta) / (k_i + k_e + 2j * delta)
        s *= np.exp(-2j * np.pi * freqs * 12e-9)               # cable delay
        noise = 2e-2 * math.sqrt(ifbw / 1e3) / math.sqrt(p_w / 1e-9)
        s = s + min(noise, 2.0) * (self.rng.standard_normal(freqs.shape)
                                   + 1j * self.rng.standard_normal(freqs.shape))
        return s * 10 ** (-35.0 / 20.0)                        # line attenuation


WORLD = SimWorld()
