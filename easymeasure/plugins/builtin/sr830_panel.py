"""SR830 front panel: an SR830 lock-in amplifier, as its own front panel.

The panel shows and operates an instrument of the ``sr830`` driver (real or
simulated) through the backend, like any front end: it never talks to the
instrument itself.

* **Channel 1 / Channel 2**: seven-segment displays of X, R, Y, θ or an aux input
  (*DISPLAY* chooses), a bar graph of the reading against the sensitivity, and the
  OVLD LED when the reading is beyond it.
* **Sensitivity, time constant**: shown by LEDs in parts, as on the instrument
  (mantissa · decade · unit, e.g. 5 · ×100 · µV), stepped with ▲ / ▼; the slope, the
  reserve, the input, coupling, grounding and notch filters with one key each.
* **Reference**: phase, frequency, amplitude or harmonic on the display, changed
  with the knob; +90° / −90°, AUTO PHASE, AUTO GAIN, AUTO RESERVE.
* A **trace** tab with X and Y over the last minutes.

It provides channels of its own (instrument ``sr830_panel``): ``ch1`` / ``ch2``
(what the displays show), ``ch1_display`` / ``ch2_display`` (which quantity) and
``overload``, and the action ``auto_sensitivity``: the sensitivity that puts R
between 10 % and 80 % of full scale, by stepping (quicker than AUTO GAIN).

Options: ``instrument`` (which SR830; empty: the first), ``poll`` (seconds between
readings), ``history`` (seconds of trace).
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from typing import List, Optional

from easymeasure.plugins import Action, Channel, Plugin, ui

PREFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "µ": 1e-6, "u": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3}
CH1 = ["X", "R", "AUX IN 1", "AUX IN 2"]
CH2 = ["Y", "θ", "AUX IN 3", "AUX IN 4"]
READS = {"X": "X", "R": "R", "Y": "Y", "θ": "theta", "AUX IN 1": "aux_in1",
         "AUX IN 2": "aux_in2", "AUX IN 3": "aux_in3", "AUX IN 4": "aux_in4"}
REF = ["PHASE", "FREQ", "AMPL", "HARM #"]
SETTINGS = ("sensitivity", "time_constant", "rolloff", "reserve", "input", "coupling",
            "ground", "line_filter", "sync_filter", "reference", "phase", "frequency",
            "sine_out", "harmonic")
DIM = "#3a1010"


def parse_si(text: str):
    """``"500 µV"`` -> (500, "µ", "V") ; None if it is not such a value."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([fpnµumk]?)([A-Za-z]+)\s*", str(text or ""))
    if not m:
        return None
    return float(m.group(1)), m.group(2), m.group(3)


DECADES = ["x1", "x10", "x100"]


def radix(text: str, mantissas, units):
    """The LEDs to light for a value as an instrument shows it in parts:
    (mantissa, decade, unit) indices, e.g. "500 µV" -> (2, 2, 1) of
    (1, 2, 5) · (×1, ×10, ×100) · (nV, µV, mV, V); (None, None, None) if it does not
    parse."""
    p = parse_si(text)
    if p is None:
        return None, None, None
    v, prefix, unit = p
    full = prefix + unit
    if full not in units or v <= 0:
        return None, None, None
    decade = int(math.floor(math.log10(v) + 1e-9))
    mant = int(round(v / 10 ** decade))
    if mant not in mantissas or not 0 <= decade < len(DECADES):
        return None, None, None
    return mantissas.index(mant), decade, units.index(full)


def seg_number(v: Optional[float], unit_scale: float, digits: int = 5) -> str:
    """*v* / *unit_scale* with *digits* significant digits, for a 7-segment display."""
    if v is None or not math.isfinite(v):
        return "-----"
    x = v / unit_scale
    if x == 0:
        return "0.0000"
    mag = int(math.floor(math.log10(abs(x))))
    dec = max(0, digits - 1 - mag)
    if mag >= digits:
        return "%.0f" % x
    return "%.*f" % (dec, x)


def seg_scaled(v: Optional[float], full_scale: float, scale: float) -> str:
    """A reading as the instrument shows it: 4½ digits of its full scale, e.g. 866.03
    at 1 mV full scale in µV, 0.8660 at 1 V."""
    if v is None or not math.isfinite(v):
        return "-----"
    dec = max(0, 4 - int(math.floor(math.log10(full_scale / scale) + 1e-9)))
    return "%.*f" % (dec, v / scale)


def si_unit(full_scale: float, unit: str):
    """The display unit for readings at *full_scale*: (scale, "µV")."""
    for p, s in (("k", 1e3), ("", 1.0), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12),
                 ("f", 1e-15)):
        if full_scale >= s * 0.999:
            return s, p + unit
    return 1e-15, "f" + unit


class SR830Panel(Plugin):
    NAME = "sr830_panel"
    TITLE = "SR830 front panel"
    DESCRIPTION = ("The front panel of an SR830 lock-in amplifier: seven-segment displays, "
                   "LEDs for sensitivity, time constant and the other settings, the keys, "
                   "the reference knob, and a trace of X and Y.")
    VERSION = "1.0"
    OPTIONS = {"instrument": "", "poll": 0.25, "history": 120.0}
    EVENTS = ("changed", "instrument_state")

    # ------------------------------------------------------------------ start --
    def start(self):
        self.inst: Optional[str] = None
        self.allowed: dict = {}
        self.units = "V"
        self.st: dict = {}                     # the settings last read
        self.meas: dict = {}                   # the readings last read
        self.ch1, self.ch2 = "X", "Y"
        self.ref_sel = "PHASE"
        self.trace = deque()
        self.ui.add_panel("panel", "SR830", self._layout())
        self.ui.add_tab("trace", "SR830 trace", ui.column(
            ui.row(ui.label("X and Y of the lock-in, as the panel reads them", size="small")),
            ui.plot("plot", "SR830", "time (s)", "V")))
        self.provide([
            Channel("ch1", "r", read=lambda: self._shown(self.ch1), unit="V",
                    description="what the CH1 display shows"),
            Channel("ch2", "r", read=lambda: self._shown(self.ch2), unit="",
                    description="what the CH2 display shows"),
            Channel("ch1_display", "rw", read=lambda: self.ch1, write=self._set_ch1, dtype="str",
                    allowed=CH1, config=True, description="what CH1 shows"),
            Channel("ch2_display", "rw", read=lambda: self.ch2, write=self._set_ch2, dtype="str",
                    allowed=CH2, config=True, description="what CH2 shows"),
            Channel("overload", "r", read=lambda: "YES" if self._overload() else "NO",
                    dtype="str", allowed=["NO", "YES"], description="R beyond the sensitivity"),
        ], [Action("auto_sensitivity", self.auto_sensitivity,
                   "step the sensitivity until R is 10 … 80 % of full scale", 60.0)])
        self._pick_instrument()
        self.every(float(self.options["poll"]), self.poll)
        self.every(2.0, self.read_settings)
        self.t0 = time.monotonic()

    # ------------------------------------------------------------------ layout --
    def _layout(self):
        dark = True
        key = ui.button
        signal = ui.group("SIGNAL INPUT", ui.row(
            ui.column(ui.ledlist("input", ["A", "A-B", "I (1M)", "I (100M)"]),
                      key("k_input", "INPUT", on_click=lambda: self._cycle("input"))),
            ui.column(ui.ledlist("coupling", ["AC", "DC"]),
                      key("k_couple", "COUPLE", on_click=lambda: self._cycle("coupling")),
                      ui.ledlist("ground", ["FLOAT", "GROUND"]),
                      key("k_ground", "GROUND", on_click=lambda: self._cycle("ground"))),
            ui.column(ui.ledlist("notch", ["LINE", "2xLINE"], label="NOTCH"),
                      key("k_notch", "NOTCH", on_click=lambda: self._cycle("line_filter")))),
            dark=dark)
        sens = ui.group("SENSITIVITY", ui.row(
            ui.ledlist("sens_m", ["1", "2", "5"]), ui.ledlist("sens_d", DECADES),
            ui.ledlist("sens_u", ["nV", "µV", "mV", "V"]),
            ui.column(key("sens_up", "▲", on_click=lambda: self._step("sensitivity", +1)),
                      key("sens_dn", "▼", on_click=lambda: self._step("sensitivity", -1)),
                      ui.led("ovld", "OVLD"))),
            ui.ledlist("reserve", ["HIGH RES", "NORMAL", "LOW NOISE"], orient="horizontal"),
            ui.row(key("k_reserve", "RESERVE", on_click=lambda: self._cycle("reserve")),
                   key("auto_gain", "AUTO GAIN", on_click=lambda: self._action("auto_gain")),
                   key("auto_reserve", "AUTO RES",
                       on_click=lambda: self._action("auto_reserve"))), dark=dark)
        tc = ui.group("TIME CONSTANT", ui.row(
            ui.ledlist("tc_m", ["1", "3"]), ui.ledlist("tc_d", DECADES),
            ui.ledlist("tc_u", ["µs", "ms", "s", "ks"]),
            ui.column(key("tc_up", "▲", on_click=lambda: self._step("time_constant", +1)),
                      key("tc_dn", "▼", on_click=lambda: self._step("time_constant", -1)))),
            ui.ledlist("slope", ["6", "12", "18", "24 dB"], orient="horizontal",
                       label="SLOPE /OCT"),
            ui.row(key("k_slope", "SLOPE", on_click=lambda: self._cycle("rolloff")),
                   ui.led("sync", "SYNC"),
                   key("k_sync", "SYNC", on_click=lambda: self._cycle("sync_filter"))),
            dark=dark)
        ch = []
        for n, opts in ((1, CH1), (2, CH2)):
            ch.append(ui.group("CHANNEL %d" % n,
                               ui.row(ui.seg7("ch%d" % n, "", digits=6, size=40),
                                      ui.label("", id="ch%d_unit" % n, size="large", bold=True,
                                               color="#ff5a3c")),
                               ui.bar("ch%d_bar" % n, 0.0, -1.0, 1.0),
                               ui.ledlist("ch%d_sel" % n, opts, orient="horizontal"),
                               key("k_disp%d" % n, "DISPLAY",
                                   on_click=lambda n=n: self._cycle_display(n)),
                               dark=dark))
        ref = ui.group("REFERENCE",
                       ui.row(ui.seg7("ref", "", digits=7, size=40),
                              ui.label("", id="ref_unit", size="large", bold=True,
                                       color="#ff5a3c")),
                       ui.row(ui.ledlist("ref_sel", REF, orient="horizontal"),
                              ui.knob("knob", "ADJUST", on_turn=self._turn)),
                       ui.row(*[key("k_" + r.split()[0].lower(), r,
                                    on_click=lambda r=r: self._ref(r)) for r in REF]),
                       ui.row(key("p90", "+90°", on_click=lambda: self._phase(+90)),
                              key("m90", "−90°", on_click=lambda: self._phase(-90)),
                              key("auto_phase", "AUTO PHASE",
                                  on_click=lambda: self._action("auto_phase"))),
                       ui.row(ui.ledlist("source", ["INTERNAL", "EXTERNAL"], orient="horizontal",
                                         label="SOURCE"),
                              key("k_source", "SOURCE", on_click=lambda: self._cycle("reference"))),
                       dark=dark)
        head = ui.row(ui.select("which", [], label="Lock-in", on_change=self._choose),
                      ui.label("", id="status", size="small"), align="center")
        return ui.column(head, ui.row(ui.column(signal, sens, tc), ui.column(*ch), ref))

    # ------------------------------------------------------------- the instrument --
    def _pick_instrument(self) -> None:
        mine = [d for d in self.backend.instruments("sr830")]
        names = [d["name"] for d in mine]
        want = self.options.get("instrument") or ""
        inst = want if want in names else (self.inst if self.inst in names else
                                           (names[0] if names else None))
        self.ui.set("which", options=names, value=inst or "")
        if inst != self.inst:
            self.inst = inst
            self.st, self.meas = {}, {}
            self.trace.clear()
        d = next((d for d in mine if d["name"] == inst), None)
        if d is None:
            self.ui.set("status", text="no SR830 instrument: add one with the sr830 driver")
            return
        self.allowed = {c["name"]: c.get("allowed") for c in d["channels"]}
        self.units = next((c["unit"] for c in d["channels"] if c["name"] == "X"), "V") or "V"
        self.ui.set("status", text="%s: %s" % (inst, d["state"]))
        self.read_settings()

    def _choose(self, name: str) -> None:
        self.options["instrument"] = name
        self.inst = None
        self._pick_instrument()

    def on_backend_event(self, kind, data):
        if kind == "changed" and (data or {}).get("what") in ("instruments", "setup"):
            self._pick_instrument()
        elif kind == "instrument_state" and (data or {}).get("instrument") == self.inst:
            self.ui.set("status", text="%s: %s" % (self.inst, data.get("state")))

    def _ch(self, channel: str) -> str:
        return "%s.%s" % (self.inst, channel)

    def _read(self, channel: str):
        return self.backend.read(self._ch(channel))

    def _write(self, channel: str, value) -> None:
        self.backend.write(self._ch(channel), value)

    # ------------------------------------------------------------------- reading --
    def poll(self) -> None:
        if self.inst is None:
            return
        want = {"X", "Y", "R", READS[self.ch1], READS[self.ch2]}
        try:
            for c in want:
                self.meas[c] = float(self._read(c))
        except Exception as exc:
            self.ui.set("status", text="%s: %s" % (self.inst, exc))
            return
        t = time.monotonic() - self.t0
        self.trace.append((t, self.meas.get("X"), self.meas.get("Y")))
        keep = float(self.options["history"])
        while self.trace and self.trace[0][0] < t - keep:
            self.trace.popleft()
        self._show_channels()
        xs = [p[0] for p in self.trace]
        self.ui.set("plot", series=[{"name": "X", "x": xs, "y": [p[1] for p in self.trace]},
                                    {"name": "Y", "x": xs, "y": [p[2] for p in self.trace]}])

    def read_settings(self) -> None:
        if self.inst is None:
            return
        for c in SETTINGS:
            try:
                self.st[c] = self._read(c)
            except Exception:
                self.st.pop(c, None)
        self._show_settings()

    def _full_scale(self) -> Optional[float]:
        p = parse_si(self.st.get("sensitivity"))
        return p[0] * PREFIX[p[1]] if p else None

    def _overload(self) -> bool:
        fs, r = self._full_scale(), self.meas.get("R")
        return fs is not None and r is not None and abs(r) > fs

    def _shown(self, which: str) -> float:
        if self.inst is None:
            raise RuntimeError("no SR830 instrument")
        v = float(self._read(READS[which]))
        self.meas[READS[which]] = v
        return v

    # ------------------------------------------------------------------- showing --
    def _show_channels(self) -> None:
        fs = self._full_scale() or 1.0
        scale, unit = si_unit(fs, self.units)
        for n, which in ((1, self.ch1), (2, self.ch2)):
            v = self.meas.get(READS[which])
            if which == "θ":
                text, u, frac = seg_number(v, 1.0), "deg", (v or 0) / 180.0
            elif which.startswith("AUX"):
                text, u, frac = seg_number(v, 1.0), "V", (v or 0) / 10.5
            else:
                text, u, frac = seg_scaled(v, fs, scale), unit, (v or 0) / fs
            self.ui.set("ch%d" % n, value=text)
            self.ui.set("ch%d_unit" % n, text=u)
            self.ui.set("ch%d_bar" % n, value=max(-1.0, min(1.0, frac)))
            self.ui.set("ch%d_sel" % n, selected=(CH1 if n == 1 else CH2).index(which))
        self.ui.set("ovld", on=self._overload())

    def _show_settings(self) -> None:
        st = self.st
        units_v = ["nV", "µV", "mV", "V"]
        units_i = ["fA", "pA", "nA", "µA"]
        units = units_i if self.units == "A" else units_v
        m, d, u = radix(st.get("sensitivity"), [1, 2, 5], units)
        self.ui.set("sens_m", selected=m)
        self.ui.set("sens_d", selected=d)
        self.ui.set("sens_u", options=units, selected=u)
        tcu = ["µs", "ms", "s", "ks"]
        m, d, u = radix(st.get("time_constant"), [1, 3], tcu)
        self.ui.set("tc_m", selected=m)
        self.ui.set("tc_d", selected=d)
        self.ui.set("tc_u", selected=u)
        self._lit("slope", st.get("rolloff"), self.allowed.get("rolloff"))
        self._lit("reserve", st.get("reserve"), self.allowed.get("reserve"))
        self._lit("input", st.get("input"), self.allowed.get("input"))
        self._lit("coupling", st.get("coupling"), self.allowed.get("coupling"))
        self._lit("ground", st.get("ground"), self.allowed.get("ground"))
        notch = {"Off": [], "Line": [0], "2x Line": [1], "Both": [0, 1]}
        self.ui.set("notch", selected=notch.get(st.get("line_filter"), []))
        self.ui.set("sync", on=st.get("sync_filter") == "On")
        src = st.get("reference")
        self.ui.set("source", selected=None if src is None else (0 if src == "Internal" else 1))
        self._show_ref()
        self._show_channels()

    def _lit(self, widget: str, value, options) -> None:
        sel = options.index(value) if options and value in options else None
        self.ui.set(widget, selected=sel)

    def _show_ref(self) -> None:
        st = self.st
        self.ui.set("ref_sel", selected=REF.index(self.ref_sel))
        if self.ref_sel == "PHASE":
            v, u = st.get("phase"), "deg"
            text = "-----" if v is None else "%.2f" % float(v)
        elif self.ref_sel == "FREQ":
            v = st.get("frequency")
            if v is None:
                text, u = "-----", "Hz"
            else:
                s, u = si_unit(max(float(v), 1e-3), "Hz") if float(v) >= 1e3 else (1.0, "Hz")
                text = seg_number(float(v), s)
        elif self.ref_sel == "AMPL":
            v, u = st.get("sine_out"), "V"
            text = "-----" if v is None else "%.3f" % float(v)
        else:
            v, u = st.get("harmonic"), ""
            text = "-----" if v is None else "%d" % int(float(v))
        self.ui.set("ref", value=text)
        self.ui.set("ref_unit", text=u)

    # ------------------------------------------------------------------- the keys --
    def _need(self) -> bool:
        if self.inst is None:
            self.ui.set("status", text="no SR830 instrument")
            return False
        return True

    def _done(self, what: str, exc: Optional[Exception] = None) -> None:
        self.ui.set("status", text="%s: %s" % (self.inst, "%s: %s" % (what, exc) if exc else what))
        self.read_settings()

    def _step(self, setting: str, d: int) -> None:
        """▲ / ▼: the next setting in the instrument's own list (▲ = larger)."""
        if not self._need():
            return
        opts: List[str] = self.allowed.get(setting) or []
        cur = self.st.get(setting)
        if cur not in opts:
            return
        i = max(0, min(len(opts) - 1, opts.index(cur) + d))
        try:
            self._write(setting, opts[i])
            self._done("%s %s" % (setting.replace("_", " "), opts[i]))
        except Exception as exc:
            self._done(setting, exc)

    def _cycle(self, setting: str) -> None:
        """A key that steps through its LEDs, as on the instrument."""
        if not self._need():
            return
        opts: List[str] = self.allowed.get(setting) or (["Off", "On"]
                                                        if setting == "sync_filter" else [])
        cur = self.st.get(setting)
        if not opts:
            return
        nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
        try:
            self._write(setting, nxt)
            self._done("%s %s" % (setting.replace("_", " "), nxt))
        except Exception as exc:
            self._done(setting, exc)

    def _action(self, name: str) -> None:
        if not self._need():
            return
        self.ui.set("status", text="%s: %s …" % (self.inst, name.replace("_", " ")))
        try:
            self.backend.action(self._ch(name))
            self._done(name.replace("_", " ") + " done")
        except Exception as exc:
            self._done(name, exc)

    def _ref(self, which: str) -> None:
        self.ref_sel = which
        self._show_ref()

    def _phase(self, d: float) -> None:
        if not self._need() or self.st.get("phase") is None:
            return
        p = float(self.st["phase"]) + d
        p = (p + 180.0) % 360.0 - 180.0
        try:
            self._write("phase", p)
            self._done("phase %.2f°" % p)
        except Exception as exc:
            self._done("phase", exc)

    def _turn(self, steps) -> None:
        """The knob changes what the reference display shows."""
        if not self._need():
            return
        steps = int(steps or 0)
        sel, st = self.ref_sel, self.st
        try:
            if sel == "PHASE" and st.get("phase") is not None:
                p = (float(st["phase"]) + steps + 180.0) % 360.0 - 180.0
                self._write("phase", p)
            elif sel == "FREQ" and st.get("frequency") is not None:
                f = float(st["frequency"]) * 1.01 ** steps
                self._write("frequency", float("%.5g" % f))
            elif sel == "AMPL" and st.get("sine_out") is not None:
                a = min(5.0, max(0.004, float(st["sine_out"]) + 0.002 * steps))
                self._write("sine_out", round(a, 3))
            elif sel == "HARM #" and st.get("harmonic") is not None:
                self._write("harmonic", max(1, int(float(st["harmonic"])) + steps))
            else:
                return
            self.read_settings()
        except Exception as exc:
            self._done(sel.lower(), exc)

    def _cycle_display(self, n: int) -> None:
        opts = CH1 if n == 1 else CH2
        cur = self.ch1 if n == 1 else self.ch2
        nxt = opts[(opts.index(cur) + 1) % len(opts)]
        (self._set_ch1 if n == 1 else self._set_ch2)(nxt)

    def _set_ch1(self, v: str) -> None:
        self.ch1 = v
        self.poll()

    def _set_ch2(self, v: str) -> None:
        self.ch2 = v
        self.poll()

    # ---------------------------------------------------------------- processing --
    def auto_sensitivity(self) -> str:
        """Step the sensitivity until R is between 10 % and 80 % of full scale."""
        if not self._need():
            raise RuntimeError("no SR830 instrument")
        opts: List[str] = self.allowed.get("sensitivity") or []
        for _ in range(len(opts)):
            self.st["sensitivity"] = self._read("sensitivity")
            fs = self._full_scale()
            r = abs(float(self._read("R")))
            i = opts.index(self.st["sensitivity"])
            if r > 0.8 * fs and i < len(opts) - 1:
                i += 1
            elif r < 0.1 * fs and i > 0:
                i -= 1
            else:
                break
            self._write("sensitivity", opts[i])
            time.sleep(0.05)
        self.read_settings()
        return self.st.get("sensitivity", "")
