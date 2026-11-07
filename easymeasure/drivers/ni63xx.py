"""National Instruments USB-63xx X Series multifunction DAQ (NI-DAQmx).

One driver for the USB-6341, 6343, 6346, 6349, 6351, 6353, 6356, 6361, 6363
and 6366 (BNC, screw / spring terminal, mass termination and OEM variants):
the **model** (option and configuration channel ``model``; ``auto`` = what the
device reports) decides the channel list — number of analog inputs and outputs,
digital lines, input / output ranges, terminal configurations and rate limits,
from NI's specifications (:mod:`._nidaq_models`).

Needs NI's NI-DAQmx driver runtime (from ni.com); the ``nidaqmx`` Python package
is installed with easyMeasure.  Address: the device name in NI MAX, ``DAQ::Dev1``.
``DAQ::SIM…`` (or option ``simulate``) uses the built-in emulation of the chosen
model (``auto`` → USB-6363); NI MAX can also create a simulated device that the
driver drives like a real one.

Channels (counts for the model)

======================  ====  ===================================================
``ai0`` …               V     analog inputs: mean of ``samples`` samples at ``rate``;
                              DIFF uses the pairs aiK/aiK+8 (K = 0…7, 16…23), so
                              only those K exist; simultaneous models (6346, 6349,
                              6356, 6366) are differential only
``ai``                  V     vector of all scanned inputs from one scan
``ao0`` …               V     analog outputs (±10 V, or ±5 V with ``ao_range``)
``dio0`` …                    digital port 0 lines P0.K, 0 / 1
``pfi0`` … ``pfi15``          PFI lines as digital lines (PFI 0…7 = P1, 8…15 = P2)
``ctr0`` … ``ctr3``           rising edges on the counter's default source
                              (PFI 8 / 3 / 0 / 5) since its first reading or
                              ``reset_counters``
======================  ====  ===================================================

Every digital line is an input until written: writing makes it an output driving
that level (reading it then returns the driven level); the action
``release_digital`` makes all of them inputs (high impedance) again.  A counter
reserves its source PFI line, which then cannot be used as a digital line.

Settings (configuration channels): ``model``, ``rate`` (samples/s per input;
multiplexed models: all scanned inputs together at most the aggregate rate),
``samples`` (per reading, averaged; 1 = one on-demand conversion, fastest),
``terminal`` (RSE / NRSE / DIFF), ``ai_range`` and ``ao_range`` (those of the
model).  Option ``ai_channels`` (e.g. ``"0-3"``) limits the scanned and exposed
inputs, which allows higher rates on the multiplexed models.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional

import numpy as np

from ..backend.instrument import ActionSpec, ChannelSpec, Instrument
from ..backend.logsys import get_logger
from ..common.errors import ChannelError
from ._nidaq_models import MODELS, X_COUNTER_SOURCE_PFI, X_SERIES, DaqModel, fmt_range, lookup
from .ni6211 import _parse_list

log = get_logger("drivers.ni63xx")

SIM_DEFAULT = "USB-6363"
# DAQmx errors that a retry cannot fix (reserved resource, invalid channel / value):
# reported at once instead of retried with a recovery that would close every task
PERMANENT_DAQ_ERRORS = {-50103, -200170, -200077, -200559, -200524}
MODEL_CHOICES = ["auto"] + X_SERIES
_PFI_OF_COUNTER = {pfi: ctr for ctr, pfi in X_COUNTER_SOURCE_PFI.items()}


def _rate_text(m: DaqModel) -> str:
    if m.simultaneous:
        return "%.4g kS/s per input, every input at once" % (m.ai_single / 1e3)
    if m.ai_single != m.ai_aggregate:
        return "%.4g kS/s for one input, %.4g kS/s for all scanned inputs together" % (
            m.ai_single / 1e3, m.ai_aggregate / 1e3)
    return "%.4g kS/s for all scanned inputs together" % (m.ai_aggregate / 1e3)


class NI63xx(Instrument):
    DESCRIPTION = ("NI USB-63xx X Series DAQ (USB-6341 … 6366, NI-DAQmx): analog / digital "
                   "channels follow the model")
    INTERFACES = ("daq",)

    @classmethod
    def suggested_addresses(cls, driver_name: str):
        return [{"address": "DAQ::SIM", "description": "built-in emulation"}]
    IDN_MATCH = tuple(X_SERIES)
    ACTIONS = [ActionSpec("reset_counters", "restart the running edge counters at 0"),
               ActionSpec("release_digital", "make every digital line an input again "
                          "(outputs off, high impedance)")]
    DEFAULT_OPTIONS = {"model": "auto", "ai_channels": "all", "simulate": False,
                       "rate": 10000.0, "samples": 100, "terminal": "RSE",
                       "ai_range": "±10 V", "ao_range": "±10 V"}

    def __init__(self, name, address, options=None):
        super().__init__(name, address, options)
        choice = str(self.options.get("model") or "auto")
        self.model: DaqModel = MODELS.get(choice) if choice in X_SERIES else MODELS[SIM_DEFAULT]
        self.model_choice = choice
        self.product: Optional[str] = None
        self.tasks: Dict[str, object] = {}
        self.ao_last: Dict[int, float] = {}
        self.dout: Dict[str, bool] = {}                 # channel -> level driven
        self._settings_from_options()
        self._fit_settings(quiet=True)

    # -- connection -----------------------------------------------------------------------
    @property
    def simulated(self) -> bool:
        return bool(self.options.get("simulate")) or self.device.upper().startswith("SIM")

    def _module(self):
        if self.simulated:
            from .emulators import nidaqmx_sim
            return nidaqmx_sim
        try:
            import nidaqmx                          # noqa: F401
        except ImportError:
            raise IOError("the nidaqmx package is missing (it comes with easyMeasure: "
                          "pip install -e . again, or pip install nidaqmx)") from None
        return nidaqmx

    @property
    def device(self) -> str:
        a = self.address
        if a.upper().startswith("DAQ::"):
            return a[5:]
        return self.options.get("device") or "Dev1"

    def _settings_from_options(self) -> None:
        self.rate = float(self.options.get("rate", 10000.0))
        self.samples = int(self.options.get("samples", 100))
        self.terminal = str(self.options.get("terminal", "RSE")).upper()
        self.ai_range = str(self.options.get("ai_range", "±10 V"))
        self.ao_range = str(self.options.get("ao_range", "±10 V"))

    def connect(self):
        self.daq = self._module()
        self.C = self.daq.constants
        self.tasks, self.ao_last, self.dout = {}, {}, {}
        choice = str(self.options.get("model") or "auto")
        if self.simulated:
            self.daq.define(self.device, choice if choice in X_SERIES else SIM_DEFAULT)
        try:
            self.product = str(self.daq.system.Device(self.device).product_type)
        except Exception as exc:
            raise IOError("NI-DAQmx: cannot open device %r: %s" % (self.device, exc)) from None
        try:
            self.model = self._resolve(choice)
        except ChannelError as exc:
            raise IOError(str(exc)) from None
        self.model_choice = choice
        self._settings_from_options()
        self._fit_settings()
        self._check_rate(self.rate)

    def disconnect(self):
        self._close_all()

    def recover(self):
        self._close_all()                         # tasks are recreated on demand
        self.ao_last = {}

    def _close_all(self):
        for t in list(self.tasks.values()):
            try:
                t.close()
            except Exception:
                pass
        self.tasks = {}

    def _close(self, key: str) -> None:
        t = self.tasks.pop(key, None)
        if t is not None:
            try:
                t.close()
            except Exception:
                pass

    # -- model ------------------------------------------------------------------------------
    def _resolve(self, choice: str) -> DaqModel:
        """The model for a ``model`` setting, checked against what the device reports."""
        detected = lookup(self.product or "")
        if choice in ("", "auto"):
            if detected is not None and detected.family == "X":
                return detected
            hint = ("; use the ni6211 driver" if detected is not None else
                    "; if it is a USB-63xx, set the model (one of %s)" % ", ".join(X_SERIES))
            raise ChannelError("%s is a %s, not a USB-63xx X Series device%s"
                               % (self.device, self.product, hint))
        if choice not in X_SERIES:
            raise ChannelError("unknown model %r (one of auto, %s)" % (choice, ", ".join(X_SERIES)))
        if detected is not None and detected.product != choice and not self.simulated:
            raise ChannelError("%s is a %s, not a %s (model auto takes what the device reports)"
                               % (self.device, self.product, choice))
        if detected is None:
            log.warning("%s: device type %r not recognised; using the channels of a %s"
                        % (self.name, self.product, choice))
        return MODELS[choice]

    def _fit_settings(self, quiet: bool = False) -> None:
        """Bring terminal / ranges into what the model supports."""
        m = self.model
        fixes = []
        if self.terminal not in m.terminals():
            new = "DIFF" if m.simultaneous else "RSE"
            fixes.append("terminal %s → %s" % (self.terminal, new))
            self.terminal = new
        for attr, ranges in (("ai_range", m.ai_ranges), ("ao_range", m.ao_ranges)):
            if getattr(self, attr) not in [fmt_range(v) for v in ranges]:
                fixes.append("%s %s → ±10 V" % (attr, getattr(self, attr)))
                setattr(self, attr, "±10 V")
        if fixes and not quiet:
            log.info("%s (%s): %s" % (self.name, m.product, "; ".join(fixes)))

    def _set_model(self, choice: str) -> None:
        if self.simulated:
            new = MODELS[choice] if choice in X_SERIES else self.model
            self.daq.define(self.device, new.product)
            self.product = new.product
        else:
            new = self._resolve(choice)
        if any(self.dout.values()):
            log.info("%s: model change - digital outputs released" % self.name)
        self._close_all()
        self.ao_last, self.dout = {}, {}
        self.model, self.model_choice = new, choice
        self._fit_settings()
        try:
            self._check_rate(self.rate)
        except ChannelError:
            self.rate = self._max_rate()
            log.info("%s: rate lowered to %.6g S/s for the %s" % (self.name, self.rate, new.product))

    # -- channel list -----------------------------------------------------------------------
    def ai_list(self) -> List[int]:
        m = self.model
        usable = m.diff_inputs() if self.terminal == "DIFF" else list(range(m.ai))
        try:
            chans = _parse_list(self.options.get("ai_channels", "all"), m.ai - 1)
        except ValueError as exc:
            raise ChannelError("%s (%s): %s" % (self.name, m.product, exc)) from None
        return [k for k in chans if k in usable]

    def dio_lines(self) -> List[tuple]:
        """(channel, physical line, label) of every digital line of the model."""
        out = []
        for port, n in enumerate(self.model.ports):
            for k in range(n):
                phys = "port%d/line%d" % (port, k)
                if port == 0:
                    out.append(("dio%d" % k, phys, "P0.%d" % k))
                else:
                    pfi = (port - 1) * 8 + k
                    out.append(("pfi%d" % pfi, phys, "PFI %d / P%d.%d" % (pfi, port, k)))
        return out

    def get_channels(self):
        m = self.model
        ai = self.ai_list()
        t_read = 5.0 + self.samples / self.rate
        ch = [ChannelSpec("ai%d" % k, "r", "float", (), "V", "analog input %d (%s, %s)"
                          % (k, self.terminal, self.ai_range), timeout=t_read) for k in ai]
        ch.append(ChannelSpec("ai", "r", "float", (len(ai),), "V", "all scanned analog inputs "
                              "(%s) from one scan" % ", ".join("ai%d" % k for k in ai),
                              timeout=t_read))
        ch += [ChannelSpec("ao%d" % k, "rw", "float", (), "V", "analog output %d (%s)"
                           % (k, self.ao_range)) for k in range(m.ao)]
        for name, _, label in self.dio_lines():
            note = ""
            if name.startswith("pfi") and int(name[3:]) in _PFI_OF_COUNTER:
                note = "; source of ctr%d while that counts" % _PFI_OF_COUNTER[int(name[3:])]
            ch.append(ChannelSpec(name, "rw", "float", (), "", "digital line %s: input until "
                                  "written, then an output%s" % (label, note), allowed=[0, 1]))
        ch += [ChannelSpec("ctr%d" % k, "r", "float", (), "", "rising edges on PFI %d (counter "
                           "%d's default source) since its first reading / reset_counters"
                           % (X_COUNTER_SOURCE_PFI[k], k)) for k in range(m.counters)]
        ch += [
            ChannelSpec("model", "rw", "str", (), "", "device model: sets the channels; auto = "
                        "what the device reports", config=True, allowed=MODEL_CHOICES),
            ChannelSpec("rate", "rw", "float", (), "S/s", "sample rate per analog input (%s)"
                        % _rate_text(m), config=True),
            ChannelSpec("samples", "rw", "float", (), "", "samples averaged per analog reading "
                        "(1 = single conversion)", config=True),
            ChannelSpec("terminal", "rw", "str", (), "", "analog input terminal configuration",
                        config=True, allowed=m.terminals()),
            ChannelSpec("ai_range", "rw", "str", (), "", "analog input range", config=True,
                        allowed=[fmt_range(v) for v in m.ai_ranges]),
            ChannelSpec("ao_range", "rw", "str", (), "", "analog output range", config=True,
                        allowed=[fmt_range(v) for v in m.ao_ranges]),
        ]
        return ch

    def get_actions(self):
        return list(self.ACTIONS)

    # -- analog input ---------------------------------------------------------------------
    def _max_rate(self) -> float:
        m = self.model
        n = max(1, len(self.ai_list()))
        if m.simultaneous or n == 1:
            return m.ai_single
        return m.ai_aggregate / n

    def _check_rate(self, rate: float) -> None:
        n = max(1, len(self.ai_list()))
        top = self._max_rate()
        if not rate > 0 or rate > top * 1.0000001:
            raise ChannelError("rate %.6g S/s × %d inputs is too fast for the %s (%s; max %.6g "
                               "S/s per input now%s)" % (
                                   rate, n, self.model.product, _rate_text(self.model), top,
                                   "" if self.model.simultaneous else
                                   "; fewer inputs: option ai_channels"))

    def _ai_task(self):
        t = self.tasks.get("ai")
        if t is not None:
            return t
        C = self.C
        v = float(self.ai_range.strip("±V "))
        t = self.daq.Task()
        try:
            chans = ",".join("%s/ai%d" % (self.device, k) for k in self.ai_list())
            t.ai_channels.add_ai_voltage_chan(chans, terminal_config=getattr(
                C.TerminalConfiguration, self.terminal), min_val=-v, max_val=v)
            if self.samples > 1:
                t.timing.cfg_samp_clk_timing(self.rate, sample_mode=C.AcquisitionType.FINITE,
                                             samps_per_chan=self.samples)
            t.control(C.TaskMode.TASK_COMMIT)       # fast start/stop for every reading
        except Exception:
            t.close()
            raise
        self.tasks["ai"] = t
        return t

    def scan(self) -> np.ndarray:
        """One acquisition of every scanned input; returns the per-input means."""
        n_ch = len(self.ai_list())
        if not n_ch:
            raise ChannelError("%s: no analog input is scanned" % self.name)
        t = self._ai_task()
        if self.samples > 1:
            t.start()
            try:
                data = t.read(number_of_samples_per_channel=self.samples,
                              timeout=2.0 + self.samples / self.rate)
            finally:
                t.stop()
        else:
            data = t.read()
        return np.asarray(data, dtype=float).reshape(n_ch, -1).mean(axis=1)

    # -- outputs ------------------------------------------------------------------------
    def _task(self, key: str, make):
        t = self.tasks.get(key)
        if t is None:
            t = self.daq.Task()
            try:
                make(t)
            except Exception:
                t.close()
                raise
            self.tasks[key] = t
        return t

    def _ao_lim(self) -> float:
        return float(self.ao_range.strip("±V "))

    def _ao_task(self, k: int):
        lim = self._ao_lim()
        return self._task("ao%d" % k, lambda t: t.ao_channels.add_ao_voltage_chan(
            "%s/ao%d" % (self.device, k), min_val=-lim, max_val=lim))

    def _ao_readback(self, k: int) -> float:
        """An output nobody has written since connect: measured on the internal
        ``_aoK_vs_aognd`` channel (the AI task makes way for it)."""
        self._close("ai")
        t = self.daq.Task()
        try:
            t.ai_channels.add_ai_voltage_chan("%s/_ao%d_vs_aognd" % (self.device, k),
                                              min_val=-10.0, max_val=10.0)
            return float(np.asarray(t.read(), dtype=float).ravel()[0])
        except Exception:
            return math.nan
        finally:
            t.close()

    # -- digital lines ----------------------------------------------------------------------
    def _line(self, channel: str) -> Optional[str]:
        for name, phys, _ in self.dio_lines():
            if name == channel:
                return phys
        return None

    def _dio_read(self, channel: str, phys: str) -> float:
        self._check_free(channel)
        if channel in self.dout:
            t = self.tasks.get("do:" + phys)
            if t is None:                           # after a recovery: same level again
                self._dio_write(channel, phys, self.dout[channel])
                t = self.tasks["do:" + phys]
            return 1.0 if bool(np.ravel(t.read())[0]) else 0.0
        t = self._task("di:" + phys, lambda t: t.di_channels.add_di_chan(
            "%s/%s" % (self.device, phys), line_grouping=self.C.LineGrouping.CHAN_PER_LINE))
        return 1.0 if bool(np.ravel(t.read())[0]) else 0.0

    def _check_free(self, channel: str) -> None:
        if channel.startswith("pfi") and int(channel[3:]) in _PFI_OF_COUNTER:
            k = _PFI_OF_COUNTER[int(channel[3:])]
            if "ctr%d" % k in self.tasks:
                raise ChannelError("%s is the source of ctr%d, which is counting: it cannot be "
                                   "a digital line until the instrument reconnects" % (channel, k))

    def _dio_write(self, channel: str, phys: str, value) -> None:
        if value not in (0, 1, True, False):
            raise ChannelError("%s: a digital line takes 0 or 1" % channel)
        self._check_free(channel)
        self._close("di:" + phys)                   # the line becomes an output
        t = self._task("do:" + phys, lambda t: t.do_channels.add_do_chan(
            "%s/%s" % (self.device, phys), line_grouping=self.C.LineGrouping.CHAN_PER_LINE))
        t.write(bool(value), auto_start=True)
        self.dout[channel] = bool(value)

    def _action_release_digital(self):
        for channel in list(self.dout):
            phys = self._line(channel)
            self._close("do:" + phys)
            self.dout.pop(channel)
            self._dio_read(channel, phys)           # an input task: high impedance

    # -- counters -------------------------------------------------------------------------
    def _counter(self, k: int):
        key = "ctr%d" % k
        t = self.tasks.get(key)
        if t is None:
            t = self._task(key, lambda t: t.ci_channels.add_ci_count_edges_chan(
                "%s/ctr%d" % (self.device, k)))
            try:
                t.start()
            except Exception:
                self._close(key)
                raise
        return t

    def _action_reset_counters(self):
        for k in range(self.model.counters):
            if "ctr%d" % k in self.tasks:
                self._close("ctr%d" % k)
                self._counter(k)

    # -- channel I/O ------------------------------------------------------------------------
    def read(self, channel):
        try:
            return self._read(channel)
        except Exception as exc:
            self._permanent(exc)
            raise

    def write(self, channel, value):
        try:
            self._write(channel, value)
        except Exception as exc:
            self._permanent(exc)
            raise

    def _permanent(self, exc: Exception) -> None:
        if getattr(exc, "error_code", None) in PERMANENT_DAQ_ERRORS:
            raise ChannelError("%s: %s" % (self.name, str(exc).splitlines()[0])) from None

    def _read(self, channel):
        if channel == "ai":
            return self.scan()
        m = re.fullmatch(r"(ai|ao|ctr)(\d+)", channel)
        if m:
            kind, k = m.group(1), int(m.group(2))
            if kind == "ai":
                ai = self.ai_list()
                if k not in ai:
                    raise ChannelError("ai%d is not scanned (%s, %s)" % (k, self.model.product,
                                                                         self.terminal))
                return float(self.scan()[ai.index(k)])
            if kind == "ao" and k < self.model.ao:
                if k not in self.ao_last:
                    v = self._ao_readback(k)
                    if not math.isnan(v):
                        self.ao_last[k] = v
                    return v
                return self.ao_last[k]
            if kind == "ctr" and k < self.model.counters:
                return float(self._counter(k).read())
        phys = self._line(channel)
        if phys is not None:
            return self._dio_read(channel, phys)
        if channel == "model":
            return self.model.product
        if channel in ("rate", "terminal", "ai_range", "ao_range"):
            return getattr(self, channel)
        if channel == "samples":
            return float(self.samples)
        raise ChannelError("%s (%s) has no channel %s" % (self.name, self.model.product, channel))

    def _write(self, channel, value):
        m = re.fullmatch(r"ao(\d+)", channel)
        if m and int(m.group(1)) < self.model.ao:
            k = int(m.group(1))
            lim = self._ao_lim()
            if not -lim <= value <= lim:
                raise ChannelError("analog outputs are limited to %s (ao_range)" % self.ao_range)
            self._ao_task(k).write(float(value), auto_start=True)
            self.ao_last[k] = float(value)
            return
        phys = self._line(channel)
        if phys is not None:
            self._dio_write(channel, phys, value)
            return
        if channel == "model":
            self._set_model(str(value))
            return
        if channel == "rate":
            self._check_rate(float(value))
            self.rate = float(value)
        elif channel == "samples":
            if value != int(value) or not 1 <= value <= 1_000_000:
                raise ChannelError("samples must be an integer 1 … 1000000")
            self.samples = int(value)
        elif channel == "terminal":
            if value not in self.model.terminals():
                raise ChannelError("the %s supports %s" % (self.model.product,
                                                          " / ".join(self.model.terminals())))
            old = self.terminal
            self.terminal = value
            try:
                self._check_rate(self.rate)
            except ChannelError:
                self.terminal = old
                raise
        elif channel == "ai_range":
            self.ai_range = value
        elif channel == "ao_range":
            lim = float(str(value).strip("±V "))
            high = [k for k, v in self.ao_last.items() if abs(v) > lim]
            if high:
                raise ChannelError("ao%d is at %.4g V: bring it within %s first"
                                   % (high[0], self.ao_last[high[0]], value))
            for k in range(self.model.ao):
                self._close("ao%d" % k)             # rebuilt with the new range
            self.ao_range = value
            return
        else:
            raise ChannelError("%s: channel %s is not writable" % (self.name, channel))
        self._close("ai")                       # rebuilt with the new settings


NI63xx.CHANNELS = NI63xx("static", "DAQ::Dev1", {"model": SIM_DEFAULT}).get_channels()
DRIVER = NI63xx
