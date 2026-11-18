"""Numerical SHG analysis; no hardware or MATLAB process is used at runtime."""
import json
from pathlib import Path
import numpy as np


def circular_mean(values):
    z = np.exp(1j * np.asarray(values, float)).mean()
    if abs(z) < 1e-8:
        raise ValueError("Phases have no well-defined circular mean")
    return float(np.angle(z) % (2*np.pi))


def baseline(spectrum, left, right):
    """Exact MATLAB smcZWOSpec: inclusive [IntL-3:IntR-4], sorted ranks 50:100."""
    y = np.asarray(spectrum, float).copy()
    a, b = int(left)-4, int(right)-4
    if not 0 <= a < b <= y.size or b-a < 100:
        raise ValueError("MATLAB baseline requires IntL >= 4 and at least 100 pixels in shifted ROI")
    y[a:b] -= np.sort(y[a:b])[49:100].mean()
    return y


def morse_cwt(signal,diagnostic=False):
    """Analytic Morse (gamma=3, time-bandwidth=60), 10 voices/octave.

    A MATLAB-exported frequency bank is used when available. The fallback is
    explicitly identified as requiring a fresh calibration, never legacy indices.
    """
    y = np.asarray(signal, float)
    if y.ndim != 1 or not np.isfinite(y).all():
        raise ValueError("Spectrum must be a finite vector")
    bank = Path(__file__).parent / ("diagnostic_bank.npz" if diagnostic else "cwt_bank.npz")
    if bank.exists():
        with np.load(bank) as d:
            if int(d["n"]) != len(y):
                raise ValueError("CWT bank requires %d-pixel spectra" % int(d["n"]))
            padding = int(d["padding"])
            mode = str(d["mode"])
            extended = np.pad(y, (padding, padding), mode=mode)
            cc = np.fft.ifft(np.fft.fft(extended)[None, :] * d["filters"], axis=1)
            return cc[:, padding:padding+len(y)], d["frequencies"].copy()
    if diagnostic: raise FileNotFoundError("Diagnostic CWT bank is missing")
    f = .1 * 2 ** (-np.arange(34)/10)
    f = f[f >= .01]
    pad = len(y)//2
    x = np.pad(y, (pad,pad), mode="symmetric")
    omega = 2*np.pi*np.fft.fftfreq(len(x))
    peak = (20/3)**(1/3)
    scaled = peak/(2*np.pi*f[:,None]) * np.maximum(omega, 0)[None,:]
    positive = scaled > 0
    filters = np.zeros_like(scaled)
    filters[positive] = 2*np.exp(20*np.log(scaled[positive]/peak) - scaled[positive]**3 + peak**3)
    return np.fft.ifft(np.fft.fft(x)[None,:]*filters, axis=1)[:,pad:pad+len(y)], f


def strongest(cc):
    # MATLAB uses column-major linear indices, including first-maximum tie handling.
    linear = int(np.argmax(np.abs(cc).ravel(order="F")))
    return tuple(int(v) for v in np.unravel_index(linear, cc.shape, order="F"))


def extract(cc, center):
    row, col = (int(v) for v in center)
    if not (0 <= row < cc.shape[0] and 0 <= col < cc.shape[1]):
        raise ValueError("Phase center is outside CWT; recalibrate")
    z = cc[row,col]
    return float(np.angle(z) % (2*np.pi)), float(abs(z))


def retardation(coefficients, angle):
    a,b,c,d = np.asarray(coefficients,float)
    return a+b*np.sin(c*np.asarray(angle)+d)


def fit_retardation(angles, phases, frequency_bounds=(.001,.1)):
    """Fit a+b*sin(c*angle+d) by variable projection, avoiding a local nonlinear fit."""
    x,y = np.asarray(angles,float), np.unwrap(np.asarray(phases,float))
    if len(x)<5 or x.shape != y.shape or not np.isfinite(x+y).all():
        raise ValueError("At least five finite angle/phase pairs are required")
    lo,hi = frequency_bounds
    best = None
    for _ in range(4):
        frequencies = np.linspace(lo,hi,501)
        for freq in frequencies:
            design=np.column_stack([np.ones_like(x),np.sin(freq*x),np.cos(freq*x)])
            beta=np.linalg.lstsq(design,y,rcond=None)[0]
            error=float(np.sum((design@beta-y)**2))
            if best is None or error<best[0]:
                best=(error,freq,beta)
        step=(hi-lo)/500
        lo,hi=max(frequency_bounds[0],best[1]-step),min(frequency_bounds[1],best[1]+step)
    error,freq,beta=best
    return [float(beta[0]),float(np.hypot(beta[1],beta[2])),float(freq),float(np.arctan2(beta[2],beta[1]))],error


def corrected_xy(x,y,pol):
    return (x-1.114004*np.cos(pol*np.pi/360+2.753500)-1.234356,
            y-.996269*np.cos(pol*np.pi/360-1.766456)-.393496,
            28+pol/2)
