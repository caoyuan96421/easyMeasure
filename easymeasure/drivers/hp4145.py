"""HP 4145A/B semiconductor parameter analyzer, and HP/Agilent 4155/4156 in
4145 syntax mode, driven in HP-IB *user mode* (direct control of the SMUs).

User mode (``US``) sets each SMU directly - ``DV`` (force voltage), ``DI``
(force current), ``DS`` (disable) - and measures with ``TV`` / ``TI``, point by
point, which is what a sweep framework needs.  Everything the analyzer's own
sweep engine does (``SS``, ``MD`` ...) is not used.

Per SMU *n* (1 … 4, option ``smus``; same mode logic as the ``keithley2400``):

==================  ======  =============================================================
``mode<n>``         config  ``Voltage`` (force V, measure I) / ``Current`` (force I, measure V)
``voltage<n>``      V       forced (Voltage mode) or measured; ±100 V
``current<n>``      A       measured (Voltage mode) or forced; ±100 mA
``Icompliance<n>``  config  current limit in Voltage mode (A) …
``Vcompliance<n>``  config  … voltage limit in Current mode (V)
``output<n>``       config  ``ON`` (programmed with DV/DI) / ``OFF`` (``DS``)
``range<n>``        config  source range: ``AUTO`` or a fixed one
==================  ======  =============================================================

Plus ``vs1``, ``vs2`` (voltage sources, ±20 V) and ``vm1``, ``vm2`` (voltmeters)
unless option ``vs_vm`` is false, and ``integration`` (short / medium / long, ``IT``).

The SMUs' power envelope is checked before anything is sent (the analyzer would
just flag an error): 100 mA up to 20 V, 50 mA up to 40 V, 20 mA up to 100 V,
for the forced value against the compliance.  Both quantities of an SMU are
measured with TV / TI, so the forced value reads back as measured.  While an
SMU's output is OFF its readings are NaN; changing its mode needs the output OFF.
A reading flagged "overflow" is ±inf, "oscillation" NaN.

The analyzer has no setting queries in user mode: the driver remembers what it
programmed.  On (re)connect it assumes every SMU is OFF and does *not* touch
the outputs (a reconnect in the middle of a measurement leaves the bias alone);
set ``output<n>`` to ON to (re)program an SMU.

4155/4156: select the 4145 syntax command mode first (front panel, or SCPI
``:SYST:LANG COMP``); the driver checks it (``CMD?`` = 2).  Address e.g.
``GPIB0::17::INSTR``.  ``SIM::<name>`` uses the built-in emulator (option
``emulate_model`` = ``4145B`` or ``4155C``).

Not verified against the manual text (the scanned manuals were not readable):
the VS / VM channel numbers 5 and 6, the ``DS <ch>`` form and the exact reply
layout of TV / TI (the driver takes the status from the first character and the
number from the end, which covers the documented variants).
"""

from __future__ import annotations

import math
import re
from typing import List

from ..backend.instrument import ActionSpec, ChannelSpec
from ..common.errors import ChannelError
from ._scpi import SCPIInstrument

V_RANGES = [(0, "AUTO", None), (1, "20 V", 20.0), (2, "40 V", 40.0), (3, "100 V", 100.0)]
I_RANGES = [(0, "AUTO", None)] + [(k, lab, val) for k, lab, val in (
    (1, "1 nA", 1e-9), (2, "10 nA", 1e-8), (3, "100 nA", 1e-7), (4, "1 µA", 1e-6),
    (5, "10 µA", 1e-5), (6, "100 µA", 1e-4), (7, "1 mA", 1e-3), (8, "10 mA", 1e-2),
    (9, "100 mA", 1e-1))]
#: (|V| up to, |I| up to): the SMU's output envelope
ENVELOPE = [(20.0, 0.1), (40.0, 0.05), (100.0, 0.02)]
INTEGRATION = ["short", "medium", "long"]
_NUM = re.compile(r"([+-]?\d+(?:\.\d*)?(?:[Ee][+-]?\d+)?)\s*$")
_CH = re.compile(r"^(mode|voltage|current|Icompliance|Vcompliance|output|range)(\d)$")


def _max_current(v: float) -> float:
    for vmax, imax in ENVELOPE:
        if abs(v) <= vmax + 1e-12:
            return imax
    return 0.0


def _max_voltage(i: float) -> float:
    ok = [vmax for vmax, imax in ENVELOPE if abs(i) <= imax + 1e-15]
    return max(ok) if ok else 0.0


class HP4145(SCPIInstrument):
    DESCRIPTION = "HP 4145A/B (or 4155/4156 in 4145 syntax) parameter analyzer, user mode"
    ACTIONS = [ActionSpec("disable_all", "DS: disable every SMU and source (outputs OFF)")]
    DEFAULT_OPTIONS = {"timeout": 10.0, "read_termination": "\n", "write_termination": "\r\n",
                       "smus": [1, 2, 3, 4], "vs_vm": True}

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.hp4145 import HP4145Emulator
        return HP4145Emulator(options)

    model = "4145B"
    integration = "short"

    def _smus(self) -> List[int]:
        smus = [int(n) for n in (self.options.get("smus") or [1, 2, 3, 4])]
        if not smus or any(not 1 <= n <= 4 for n in smus):
            raise ChannelError("option smus: SMU numbers 1 … 4")
        return smus

    # -- channel list -----------------------------------------------------------------
    def get_channels(self):
        st = getattr(self, "st", None) or {}
        ch: List[ChannelSpec] = []
        for n in self._smus():
            mode = st.get(n, {}).get("mode", "Voltage")
            v = mode == "Voltage"
            what = " (SMU%d)" % n
            ch += [
                ChannelSpec("mode%d" % n, "rw", "str", (), "", "force voltage or force current" + what,
                            config=True, allowed=["Voltage", "Current"]),
                ChannelSpec("voltage%d" % n, "rw" if v else "r", "float", (), "V",
                            ("forced" if v else "measured") + " voltage" + what),
                ChannelSpec("current%d" % n, "r" if v else "rw", "float", (), "A",
                            ("measured" if v else "forced") + " current" + what),
                ChannelSpec(("Icompliance%d" if v else "Vcompliance%d") % n, "rw", "float", (),
                            "A" if v else "V", ("current" if v else "voltage") + " compliance" + what,
                            config=True),
                ChannelSpec("output%d" % n, "rw", "str", (), "", "ON = programmed, OFF = disabled"
                            + what, config=True, allowed=["ON", "OFF"]),
                ChannelSpec("range%d" % n, "rw", "str", (), "", "source range" + what, config=True,
                            allowed=[r[1] for r in (V_RANGES if v else I_RANGES)]),
            ]
        if self.options.get("vs_vm", True):
            ch += [ChannelSpec("vs%d" % k, "rw", "float", (), "V", "voltage source VS%d (±20 V)" % k)
                   for k in (1, 2)]
            ch += [ChannelSpec("vm%d" % k, "r", "float", (), "V", "voltmeter VM%d" % k)
                   for k in (1, 2)]
        ch.append(ChannelSpec("integration", "rw", "str", (), "", "A/D integration time (IT1/2/3)",
                              config=True, allowed=INTEGRATION))
        return ch

    # -- connection ---------------------------------------------------------------------
    def connect(self):
        self.attach_emulator()
        self.select(self.resource)
        idn = self.q("ID")
        if "4145" in idn:
            self.model = "4145B" if "4145B" in idn.upper() else "4145A"
        elif re.search(r"415[56]", idn):
            self.model = re.search(r"415[56][A-Z]?", idn).group(0)
            mode = self.q("CMD?").strip()
            if mode != "2":
                raise IOError("the %s is not in 4145 syntax mode (CMD? = %s): select it on the "
                              "front panel or with :SYST:LANG COMP" % (self.model, mode))
        else:
            raise IOError("unexpected instrument %r (expected an HP 4145 / 4155 / 4156)" % idn[:60])
        self.idn = idn
        self.w("US")                              # user mode: direct SMU control
        self.w("IT%d" % (INTEGRATION.index(self.integration) + 1))
        # nothing is known about the outputs: assume OFF, leave the hardware alone
        old = getattr(self, "st", None) or {}
        self.st = {n: {"mode": old.get(n, {}).get("mode", "Voltage"), "on": False, "value": 0.0,
                       "comp": old.get(n, {}).get("comp"), "range": "AUTO"} for n in self._smus()}
        self.vs = {1: None, 2: None}

    # -- helpers ------------------------------------------------------------------------
    def _split(self, channel: str):
        m = _CH.match(channel)
        if not m or int(m.group(2)) not in self.st:
            raise ChannelError("%s has no channel %s" % (self.name, channel))
        return m.group(1), int(m.group(2))

    def _measure(self, kind: str, ch: int) -> float:
        r = self.q("T%s%d" % (kind, ch)).strip()
        m = _NUM.search(r)
        if not m:
            raise IOError("unexpected measurement reply %r" % r[:40])
        x = float(m.group(1))
        status = r[0].upper() if r[:1].isalpha() else "N"
        if status == "V":                         # A/D overflow
            return math.copysign(math.inf, x) if x else math.inf
        if status == "X":                         # SMU oscillating
            return math.nan
        return x

    def _program(self, n: int) -> None:
        """Send DV / DI with the remembered value, range and compliance."""
        s = self.st[n]
        if s["comp"] is None:
            raise ChannelError("%s: set the compliance of SMU%d before switching it ON"
                               % (self.name, n))
        v = s["mode"] == "Voltage"
        self._check_envelope(n, s["value"], s["comp"], v)
        rng = next(k for k, lab, _ in (V_RANGES if v else I_RANGES) if lab == s["range"])
        self.w("D%s%d,%d,%+.4E,%+.4E" % ("V" if v else "I", n, rng, s["value"], s["comp"]))

    def _check_envelope(self, n: int, value: float, comp: float, voltage_mode: bool) -> None:
        if voltage_mode:
            if abs(value) > 100.0:
                raise ChannelError("SMU%d: %g V is beyond ±100 V" % (n, value))
            imax = _max_current(value)
            if abs(comp) > imax + 1e-15:
                raise ChannelError("SMU%d: a current compliance of %g A is not available at %g V "
                                   "(max %g A up to %g V)" % (n, comp, value, imax,
                                                              next(vm for vm, im in ENVELOPE if im == imax)))
        else:
            if abs(value) > 0.1:
                raise ChannelError("SMU%d: %g A is beyond ±100 mA" % (n, value))
            vmax = _max_voltage(value)
            if abs(comp) > vmax + 1e-12:
                raise ChannelError("SMU%d: a voltage compliance of %g V is not available at %g A "
                                   "(max %g V)" % (n, comp, value, vmax))
        rng = self.st[n]["range"]
        if rng != "AUTO":
            full = next(val for k, lab, val in (V_RANGES if voltage_mode else I_RANGES) if lab == rng)
            if abs(value) > full * (1 + 1e-9):
                raise ChannelError("SMU%d: %g does not fit the %s range" % (n, value, rng))

    # -- I/O --------------------------------------------------------------------------
    def read(self, channel):
        if channel == "integration":
            return self.integration
        m = re.fullmatch(r"(vs|vm)([12])", channel)
        if m and self.options.get("vs_vm", True):
            k = int(m.group(2))
            if m.group(1) == "vm":
                return self._measure("V", 4 + k)
            return math.nan if self.vs[k] is None else self.vs[k]
        base, n = self._split(channel)
        s = self.st[n]
        if base in ("voltage", "current"):
            if not s["on"]:
                return math.nan                    # disabled: nothing to measure
            return self._measure("V" if base == "voltage" else "I", n)
        if base == "mode":
            return s["mode"]
        if base in ("Icompliance", "Vcompliance"):
            self._check_comp_name(base, n)
            return math.nan if s["comp"] is None else s["comp"]
        if base == "output":
            return "ON" if s["on"] else "OFF"
        if base == "range":
            return s["range"]
        raise ChannelError("%s has no channel %s" % (self.name, channel))

    def write(self, channel, value):
        if channel == "integration":
            self.w("IT%d" % (INTEGRATION.index(value) + 1))
            self.integration = value
            return
        m = re.fullmatch(r"vs([12])", channel)
        if m and self.options.get("vs_vm", True):
            k = int(m.group(1))
            if abs(value) > 20.0:
                raise ChannelError("VS%d is limited to ±20 V" % k)
            self.w("DV%d,0,%+.4E,%+.4E" % (4 + k, value, 0.0))
            self.vs[k] = float(value)
            return
        base, n = self._split(channel)
        s = self.st[n]
        v = s["mode"] == "Voltage"
        if base in ("voltage", "current"):
            if (base == "voltage") != v:
                raise ChannelError("%s: %s%d is measured in %s mode; set mode%d first"
                                   % (self.name, base, n, s["mode"], n))
            if s["comp"] is not None:
                self._check_envelope(n, float(value), s["comp"], v)
            s["value"] = float(value)
            if s["on"]:
                self._program(n)
        elif base == "mode":
            if value == s["mode"]:
                return
            if s["on"]:
                raise ChannelError("%s: switch output%d OFF before changing the mode" % (self.name, n))
            s.update(mode=value, value=0.0, comp=None, range="AUTO")
        elif base in ("Icompliance", "Vcompliance"):
            self._check_comp_name(base, n)
            self._check_envelope(n, s["value"], float(value), v)
            s["comp"] = float(value)
            if s["on"]:
                self._program(n)
        elif base == "output":
            if value == "ON":
                self._program(n)
                s["on"] = True
            else:
                self.w("DS%d" % n)
                s["on"] = False
        elif base == "range":
            old = s["range"]
            s["range"] = value
            try:
                self._check_envelope(n, s["value"], s["comp"] or 0.0, v)
            except ChannelError:
                s["range"] = old
                raise
            if s["on"]:
                self._program(n)
        else:
            raise ChannelError("%s: channel %s is not writable" % (self.name, channel))

    def _check_comp_name(self, base: str, n: int) -> None:
        want = "Icompliance" if self.st[n]["mode"] == "Voltage" else "Vcompliance"
        if base != want:
            raise ChannelError("SMU%d is in %s mode: the compliance is %s%d"
                               % (n, self.st[n]["mode"], want, n))

    def _action_disable_all(self):
        self.w("DS")
        for s in self.st.values():
            s["on"] = False
        self.vs = {1: None, 2: None}


HP4145.CHANNELS = HP4145("static", "SIM::x").get_channels()
DRIVER = HP4145
