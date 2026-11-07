"""Channel counts and limits of the NI USB multifunction DAQ models the drivers
know, from NI's device specifications.  Used by the ``ni63xx`` / ``ni6211``
drivers and by the NI-DAQmx emulator (so the emulated card has the same channels
as the real one).

X Series USB-63xx (specifications of the NI 6341 … 6366; BNC, screw/spring
terminal, mass termination and OEM variants have the same channels):

=========  ===============  =====  ===========  =====================  ===  =========  ======
model      analog inputs    sim.   max rate     AI ranges (±V)         AO   AO (±V)    P0 / PFI
=========  ===============  =====  ===========  =====================  ===  =========  ======
USB-6341   16 SE / 8 DIFF   no     500 kS/s     0.2 1 5 10             2    10         8 / 16
USB-6343   32 SE / 16 DIFF  no     500 kS/s     0.2 1 5 10             4    10         32 / 16
USB-6346   8 DIFF           yes    500 kS/s/ch  1 2 5 10               2    10         8 / 16
USB-6349   32 DIFF          yes    500 kS/s/ch  1 2 5 10               2    10         8 / 16
USB-6351   16 SE / 8 DIFF   no     1.25 / 1 MS  0.1 0.2 0.5 1 2 5 10   2    10, 5      8 / 16
USB-6353   32 SE / 16 DIFF  no     1.25 / 1 MS  0.1 0.2 0.5 1 2 5 10   4    10, 5      32 / 16
USB-6356   8 DIFF           yes    1.25 MS/ch   1 2 5 10               2    10, 5      8 / 16
USB-6361   16 SE / 8 DIFF   no     2 / 1 MS     0.1 0.2 0.5 1 2 5 10   2    10, 5      8 / 16
USB-6363   32 SE / 16 DIFF  no     2 / 1 MS     0.1 0.2 0.5 1 2 5 10   4    10, 5      32 / 16
USB-6366   8 DIFF           yes    2 MS/ch      1 2 5 10               2    10, 5      8 / 16
=========  ===============  =====  ===========  =====================  ===  =========  ======

(max rate of the multiplexed models: single channel / all channels together.)
All have 4 counters; PFI 0 … 7 are port 1 and PFI 8 … 15 port 2; every digital
line is individually an input or an output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class DaqModel:
    product: str
    ai: int                     # analog inputs (single-ended count; DIFF count if simultaneous)
    simultaneous: bool          # one ADC per input (differential only)
    ai_single: float            # S/s: one input (multiplexed) / every input (simultaneous)
    ai_aggregate: float         # S/s: all scanned inputs together (multiplexed)
    ai_ranges: Tuple[float, ...]
    ao: int
    ao_ranges: Tuple[float, ...]
    ports: Tuple[int, ...]      # lines per port
    port_dirs: Tuple[str, ...]  # per port: "io" (each line in or out), "i" or "o"
    counters: int

    def diff_inputs(self) -> List[int]:
        """Inputs usable as differential pairs: aiK with aiK+8 for K = 0…7 (and 16…23)."""
        if self.simultaneous:
            return list(range(self.ai))
        return [k for k in range(self.ai) if k % 16 < 8]

    def terminals(self) -> List[str]:
        return ["DIFF"] if self.simultaneous else ["RSE", "NRSE", "DIFF"]

    @property
    def family(self) -> str:
        return "X" if self.product.startswith("USB-63") else "M"


_MUX4 = (0.2, 1.0, 5.0, 10.0)
_MUX7 = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0)
_SIM = (1.0, 2.0, 5.0, 10.0)
_X8 = (8, 8, 8)
_X32 = (32, 8, 8)
_IO = ("io", "io", "io")


def _x(product, ai, sim, single, agg, rng, ao, aor, ports):
    return DaqModel(product, ai, sim, single, agg, rng, ao, aor, ports, _IO, 4)


MODELS: Dict[str, DaqModel] = {m.product: m for m in [
    _x("USB-6341", 16, False, 500e3, 500e3, _MUX4, 2, (10.0,), _X8),
    _x("USB-6343", 32, False, 500e3, 500e3, _MUX4, 4, (10.0,), _X32),
    _x("USB-6346", 8, True, 500e3, 500e3 * 8, _SIM, 2, (10.0,), _X8),
    _x("USB-6349", 32, True, 500e3, 500e3 * 32, _SIM, 2, (10.0,), _X8),
    _x("USB-6351", 16, False, 1.25e6, 1e6, _MUX7, 2, (10.0, 5.0), _X8),
    _x("USB-6353", 32, False, 1.25e6, 1e6, _MUX7, 4, (10.0, 5.0), _X32),
    _x("USB-6356", 8, True, 1.25e6, 1.25e6 * 8, _SIM, 2, (10.0, 5.0), _X8),
    _x("USB-6361", 16, False, 2e6, 1e6, _MUX7, 2, (10.0, 5.0), _X8),
    _x("USB-6363", 32, False, 2e6, 1e6, _MUX7, 4, (10.0, 5.0), _X32),
    _x("USB-6366", 8, True, 2e6, 2e6 * 8, _SIM, 2, (10.0, 5.0), _X8),
    # M Series, for the ni6211 driver and the emulator
    DaqModel("USB-6211", 16, False, 250e3, 250e3, _MUX4, 2, (10.0,), (4, 4), ("i", "o"), 2),
]}

X_SERIES = [m for m in MODELS if MODELS[m].family == "X"]

# default source terminals of the X Series counters (X Series User Manual)
X_COUNTER_SOURCE_PFI = {0: 8, 1: 3, 2: 0, 3: 5}


def lookup(product: str) -> Optional[DaqModel]:
    """The model of a DAQmx product type ("USB-6343 (BNC)", "USB-6363 (Mass Termination)",
    "NI USB-6341" …), or None."""
    m = re.search(r"USB-?(6\d{3})", str(product).upper())
    return MODELS.get("USB-%s" % m.group(1)) if m else None


def fmt_range(v: float) -> str:
    return "±%g V" % v
