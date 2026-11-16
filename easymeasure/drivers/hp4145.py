"""HP 4145A/B semiconductor parameter analyzer, and HP/Agilent 4155/4156 in its HP 4145
command set, driven in HP-IB *user mode* (direct control of the SMUs).

The protocol follows a 4155 driver verified on the hardware (HP 4155A/4156A HP-IB
Command Reference 04155-90120: language selection p. 1-329, user mode
pp. 2-45 … 53, precision / EOI pp. 2-59 … 64):

* **Command set.** A 4155/4156 must be in its HP 4145 command set.  Option
  ``command_set``: ``HP4145`` (the default; it is selected already - always so for a
  4145A/B) or ``HP4155`` (it is in its own SCPI or FLEX command set: the driver
  switches it once, with ``*IDN?``, ``CMD?``, ``:PAGE`` from FLEX and
  ``:SYST:LANG COMP``, then waits 2 s).  After that no SCPI command is ever sent -
  not ``*IDN?``, ``CMD?``, ``*CLS`` or ``:SYST:ERR?`` - the 4145 command set does not
  have them.  The analyzer stays in the HP 4145 command set (also after a
  disconnect); a later connection in the same backend session does not switch it
  again.  Nothing is probed: a wrong ``command_set`` is an error.
* **Initialisation.** ``BC``, ``US`` (user mode), every source off (``DV1`` …
  ``DV4``, ``DS1``, ``DS2``), ``ID``, ``DP1`` and ``EI1`` (not on a 4145, nor on a
  4155A with HOSTC firmware 01.00 / 01.01), ``IT<n>``, ``BC``.
* **Sources.** ``DV<n>,<range>,<V>,<I comp>`` / ``DI<n>,<range>,<I>,<V comp>`` set
  and enable SMU *n*, ``DV<n>`` alone disables it; ``DS<k>,<V>`` sets voltage source
  VS*k*, ``DS<k>`` alone disables it.  Values are rounded to the resolution of the
  source range.
* **Readings.** ``BC;TV<n>`` / ``BC;TI<n>`` (SMU 1 … 4, voltmeters VM1 / VM2 are
  ``TV5`` / ``TV6``): a fresh reading, e.g. ``NAV+1.2345E+00`` - status, channel
  letter (A … D the SMUs, E / F the voltmeters), V or I, value.  Status ``N``
  normal, ``C`` / ``T`` compliance (this / another channel: the reading is the
  value), ``V`` A/D overflow (±inf), ``X`` oscillation (NaN), ``P`` PGU current
  limit.  An ``ERROR`` reply is an error.  No error queue is polled.

Per SMU *n* (1 … 4, option ``smus``; the same mode logic as the ``keithley2400``):

==================  ======  =============================================================
``mode<n>``         config  ``Voltage`` (force V, measure I) / ``Current`` (force I, measure V)
``voltage<n>``      V       forced (Voltage mode) or measured; ±100 V
``current<n>``      A       measured (Voltage mode) or forced; ±100 mA
``Icompliance<n>``  config  current limit in Voltage mode (A) …
``Vcompliance<n>``  config  … voltage limit in Current mode (V)
``output<n>``       config  ``ON`` (programmed with DV/DI) / ``OFF`` (``DV<n>``)
``range<n>``        config  source range: ``AUTO`` or a fixed one
==================  ======  =============================================================

Plus ``vs1``, ``vs2`` (voltage sources, ±20 V, 1 mV) and ``vm1``, ``vm2``
(voltmeters) unless option ``vs_vm`` is false, and ``integration`` (short / medium /
long, ``IT``).

The SMUs' output envelope is checked before anything is sent: a voltage source's
current compliance at most 100 mA on the 2 / 20 V ranges, 50 mA on 40 V, 20 mA on
100 V; a current source's voltage compliance at most 100 V up to 20 mA, 40 V up to
50 mA, 20 V above.  Both quantities of an SMU are measured with TV / TI, so the
forced value reads back as measured.  While an SMU's output is OFF its readings
are NaN; changing its mode needs the output OFF.

User mode has no setting queries: the driver remembers what it programmed.
Connecting switches every source off, and so does disconnecting; a reconnect
(after a failed call, or Reconnect) programs the SMUs and sources that were on
again.  Address e.g. ``GPIB0::17::INSTR``.
``SIM::<name>`` uses the built-in emulator (option ``emulate_model`` = ``4145B``,
``4155A``/``B``/``C``, ``4156C``).
"""

from __future__ import annotations

import math
import re
import time
from decimal import ROUND_HALF_EVEN, Decimal
from typing import List, Optional, Tuple

from ..backend.instrument import ActionSpec, ChannelSpec
from ..backend.logsys import get_logger
from ..backend.resources import GPIB_LIKE
from ..common.errors import ChannelError
from ._scpi import SCPIInstrument

log = get_logger("drivers.hp4145")

#: voltage source ranges: (code, label, full scale V, resolution V, max current compliance A)
V_RANGES = [(-1, "2 V", 2.0, 1e-4, 0.1), (1, "20 V", 20.0, 1e-3, 0.1),
            (2, "40 V", 40.0, 2e-3, 0.05), (3, "100 V", 100.0, 5e-3, 0.02)]
#: current source ranges: (code, label, full scale A, resolution A = range / 10000)
I_RANGES = [(k, lab, 10.0 ** (k - 10), 10.0 ** (k - 14)) for k, lab in (
    (1, "1 nA"), (2, "10 nA"), (3, "100 nA"), (4, "1 µA"), (5, "10 µA"), (6, "100 µA"),
    (7, "1 mA"), (8, "10 mA"), (9, "100 mA"))]
INTEGRATION = ["short", "medium", "long"]
COMMAND_SETS = ("HP4145", "HP4155")

_READING = re.compile(
    r"([NTCPXV])([A-H])([VI])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d{1,3})?)")
_ERROR = re.compile(r'(?:ERROR\s*:?\s*)?([+-]?\d+)\s*[:,]\s*"?([A-Za-z][^\r\n]*?)"?', re.I)
_CH = re.compile(r"^(mode|voltage|current|Icompliance|Vcompliance|output|range)(\d)$")

#: addresses switched to the HP 4145 command set in this backend session (it stays
#: there: a later connection must not send SCPI again)
_SWITCHED = set()


def number(x: float) -> str:
    """A value for a command, as the verified driver writes it (``5E-1``)."""
    d = Decimal(repr(float(x)))
    return "0" if d == 0 else format(d.normalize(), "E")


def _round(x: float, res: float) -> float:
    d, r = Decimal(repr(float(x))), Decimal(repr(res))
    return float((d / r).to_integral_value(rounding=ROUND_HALF_EVEN) * r)


def _vcomp_max(i: float) -> float:
    """The largest voltage compliance a current source of *i* A may have."""
    a = abs(i)
    return 100.0 if a <= 0.02 + 1e-15 else 40.0 if a <= 0.05 + 1e-15 else 20.0


class HP4145(SCPIInstrument):
    DESCRIPTION = "HP 4145A/B (or 4155/4156 in its 4145 command set) parameter analyzer, user mode"
    INTERFACES = GPIB_LIKE    # GPIB; COM / TCP: RS-232 where it has one, or a Prologix GPIB adapter
    ACTIONS = [ActionSpec("disable_all", "every SMU and voltage source off (DV1 … DV4, DS1, DS2)")]
    DEFAULT_OPTIONS = {"timeout": 10.0, "read_termination": "\n", "write_termination": "\r\n",
                       "gpib_addr": None, "command_set": "HP4145", "smus": [1, 2, 3, 4],
                       "vs_vm": True}

    @staticmethod
    def EMULATOR(options):                     # noqa: N802
        from .emulators.hp4145 import HP4145Emulator
        return HP4145Emulator(options)

    model = "4145B"
    integration = "short"
    st: dict = {}                     # SMU n -> what was programmed (no queries in user mode)
    vs: dict = {}

    def _smus(self) -> List[int]:
        smus = [int(n) for n in self.options["smus"]]
        if not smus or any(not 1 <= n <= 4 for n in smus):
            raise ChannelError("option smus: SMU numbers 1 … 4")
        return smus

    def _v_ranges(self):
        """The 2 V range is the 4155/4156's (the 4145's SMUs start at 20 V)."""
        return [r for r in V_RANGES if r[0] != -1 or not self.model.startswith("4145")]

    # -- channel list -----------------------------------------------------------------
    def get_channels(self):
        ch: List[ChannelSpec] = []
        for n in self._smus():
            mode = self.st[n]["mode"] if n in self.st else "Voltage"
            v = mode == "Voltage"
            what = " (SMU%d)" % n
            ranges = ["AUTO"] + [r[1] for r in (self._v_ranges() if v else I_RANGES)]
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
                            allowed=ranges),
            ]
        if self.options["vs_vm"]:
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
        cs = str(self.options["command_set"]).upper()
        if cs not in COMMAND_SETS:
            raise ChannelError("option command_set: %s or %s" % COMMAND_SETS)
        if cs == "HP4155" and self.address not in _SWITCHED:
            self._select_4145_command_set()
        self.w("BC")
        self.w("US")                              # user mode: direct SMU control
        time.sleep(0.5)
        self._all_off()
        try:
            idn = self.q("BC;ID").strip()
        except Exception as exc:
            raise IOError("no reply to ID (%s): is option command_set right? A 4155/4156 in "
                          "its own SCPI or FLEX command set needs HP4155" % exc) from None
        m = re.search(r"41(45|5[56])[ABC]?", idn.upper())
        if not m:
            raise IOError("unexpected ID reply %r (expected an HP 4145 / 4155 / 4156; is "
                          "option command_set right?)" % idn[:60])
        self.model, self.idn = m.group(0), idn
        if not self.model.startswith("4145"):
            host = idn.split(",")[-1].split(":")[0].strip()
            if not (self.model == "4155A" and host in ("01.00", "01.01")):
                self.w("DP1")                     # (the earliest 4155A firmware has neither)
                self.w("EI1")
        self.w("IT%d" % (INTEGRATION.index(self.integration) + 1))
        self.w("BC")
        # a reconnect: what was on goes on again; a new connection: all off
        old, old_vs = (self.st, self.vs) if self.reconnecting else ({}, {})
        self.st = {n: {"mode": old[n]["mode"] if n in old else "Voltage", "on": False,
                       "value": old[n]["value"] if n in old else 0.0,
                       "comp": old[n]["comp"] if n in old else None,
                       "range": old[n]["range"] if n in old else "AUTO"} for n in self._smus()}
        self.vs = {1: None, 2: None}
        for n in self._smus():
            if n in old and old[n]["on"]:
                self._program(n)
                self.st[n]["on"] = True
                log.info("%s: SMU%d programmed again after the reconnect", self.name, n)
        for k in (1, 2):
            if old_vs.get(k) is not None:
                self._set_vs(k, old_vs[k])

    def _select_4145_command_set(self) -> None:
        """A 4155/4156 in its own command set: switch it once to the HP 4145 one -
        the only SCPI this driver sends."""
        try:
            idn = self.q("*IDN?").strip()
        except Exception as exc:
            raise IOError("no reply to *IDN? (%s): the analyzer is probably in the HP 4145 "
                          "command set already (switched before): set option command_set to "
                          "HP4145" % exc) from None
        f = idn.split(",")
        model = f[1].strip().upper() if len(f) >= 2 else ""
        if not re.fullmatch(r"415[56][ABC]", model):
            raise IOError("*IDN? %r: not an HP 4155/4156 (option command_set HP4155 is for "
                          "those)" % idn[:60])
        if model != "4155A":
            mode = self.q("CMD?").strip()
            if mode == "1":
                self.w(":PAGE")                   # FLEX -> SCPI
            elif mode != "0":
                raise IOError("unexpected command mode CMD? = %r" % mode)
        self.w(":SYST:LANG COMP")
        time.sleep(2.0)                           # the analyzer resets into the 4145 set
        _SWITCHED.add(self.address)
        log.info("%s: %s switched to the HP 4145 command set", self.name, model)

    def disconnect(self):
        if not self.reconnecting:              # the end of the session: everything off
            self._all_off()
            self.w("BC")
            for s in self.st.values():
                s["on"] = False
            self.vs = {1: None, 2: None}

    def _all_off(self) -> None:
        for n in (1, 2, 3, 4):
            self.w("DV%d" % n)
        self.w("DS1")
        self.w("DS2")

    # -- helpers ------------------------------------------------------------------------
    def _split(self, channel: str):
        m = _CH.match(channel)
        if not m or int(m.group(2)) not in self.st:
            raise ChannelError("%s has no channel %s" % (self.name, channel))
        return m.group(1), int(m.group(2))

    def _measure(self, kind: str, ch: int) -> float:
        """``BC;T<kind><ch>``: one fresh reading."""
        raw = self.q("BC;T%s%d" % (kind, ch)).strip()
        if _ERROR.fullmatch(raw):
            e = _ERROR.fullmatch(raw)
            raise ChannelError("%s: T%s%d: error %s: %s" % (self.name, kind, ch, e.group(1),
                                                             e.group(2)))
        m = _READING.fullmatch(raw)
        if m is None:
            raise IOError("malformed reading %r for T%s%d" % (raw[:40], kind, ch))
        status, letter, quantity, text = m.groups()
        if (letter, quantity) != (chr(64 + ch), kind):
            raise IOError("T%s%d answered %r" % (kind, ch, raw[:40]))
        x = float(text)
        if status == "V":                         # A/D overflow
            return math.copysign(math.inf, x) if x else math.inf
        if status == "X":                         # SMU oscillating
            return math.nan
        return x                                  # N, C / T (compliance), P

    def _source_range(self, n: int) -> Tuple[int, float]:
        """(range code, resolution) for SMU *n*'s value (AUTO: the smallest that fits)."""
        s = self.st[n]
        v = s["mode"] == "Voltage"
        ranges = self._v_ranges() if v else I_RANGES
        fit = [r for r in ranges if abs(s["value"]) <= r[2] * (1 + 1e-9)]
        if s["range"] != "AUTO":
            fixed = next(r for r in ranges if r[1] == s["range"])
            if fixed not in fit:
                raise ChannelError("SMU%d: %g does not fit the %s range" % (n, s["value"],
                                                                           s["range"]))
            return fixed[0], fixed[3]
        if not fit:
            raise ChannelError("SMU%d: %g %s is beyond the largest range" % (
                n, s["value"], "V" if v else "A"))
        return 0, fit[0][3]

    def _program(self, n: int) -> None:
        """Send DV / DI with the remembered value, range and compliance."""
        s = self.st[n]
        if s["comp"] is None:
            raise ChannelError("%s: set the compliance of SMU%d before switching it ON"
                               % (self.name, n))
        v = s["mode"] == "Voltage"
        self._check_envelope(n, s["value"], s["comp"], v)
        code, res = self._source_range(n)
        value = _round(s["value"], res)
        self.w("D%s%d,%d,%s,%s" % ("V" if v else "I", n, code, number(value),
                                   number(abs(s["comp"]))))

    def _check_envelope(self, n: int, value: float, comp: float, voltage_mode: bool) -> None:
        if voltage_mode:
            if abs(value) > 100.0:
                raise ChannelError("SMU%d: %g V is beyond ±100 V" % (n, value))
            rng = self._v_ranges()
            fit = next((r for r in rng if abs(value) <= r[2] * (1 + 1e-9)), rng[-1])
            if self.st[n]["range"] != "AUTO":
                fit = next(r for r in rng if r[1] == self.st[n]["range"])
            if abs(comp) > fit[4] + 1e-15:
                raise ChannelError("SMU%d: a current compliance of %g A is not available at %g V "
                                   "(max %g A on the %s range)" % (n, comp, value, fit[4], fit[1]))
        else:
            if abs(value) > 0.1:
                raise ChannelError("SMU%d: %g A is beyond ±100 mA" % (n, value))
            vmax = _vcomp_max(value)
            if abs(comp) > vmax + 1e-12:
                raise ChannelError("SMU%d: a voltage compliance of %g V is not available at %g A "
                                   "(max %g V)" % (n, comp, value, vmax))
        self.st[n]["value"], old = value, self.st[n]["value"]
        try:
            self._source_range(n)
        finally:
            self.st[n]["value"] = old

    def _set_vs(self, k: int, value: Optional[float]) -> None:
        if value is None:
            self.w("DS%d" % k)
            self.vs[k] = None
            return
        if abs(value) > 20.0:
            raise ChannelError("VS%d is limited to ±20 V" % k)
        v = _round(value, 1e-3)
        self.w("DS%d,%s" % (k, number(v)))
        self.vs[k] = v

    # -- I/O --------------------------------------------------------------------------
    def read(self, channel):
        if channel == "integration":
            return self.integration
        m = re.fullmatch(r"(vs|vm)([12])", channel)
        if m and self.options["vs_vm"]:
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
        if m and self.options["vs_vm"]:
            self._set_vs(int(m.group(1)), float(value))
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
                self.w("DV%d" % n)
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
        self._all_off()
        self.w("BC")
        for s in self.st.values():
            s["on"] = False
        self.vs = {1: None, 2: None}


HP4145.CHANNELS = HP4145("static", "NONE").get_channels()
DRIVER = HP4145
