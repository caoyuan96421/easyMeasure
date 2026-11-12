"""Make the screenshots of the quick start and the tutorials (docs/images/).

Drives the real desktop GUI against a real (in-process) backend with the simulated
graphene sample and VNA, exactly as the tutorials describe: dialogs are opened by
the same code as the buttons, filled in, photographed and accepted.

    python tools/make_doc_screenshots.py              # all (≈ 10 min)
    python tools/make_doc_screenshots.py qs t2         # only some parts

Runs offscreen; needs nothing but easyMeasure itself.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "images"
HOME = Path(tempfile.mkdtemp(prefix="emshots"))
os.environ["EASYMEASURE_HOME"] = str(HOME / "home")
os.environ["XDG_CONFIG_HOME"] = str(HOME / "config")      # fresh GUI settings

from PySide6.QtCore import QEvent, QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QDockWidget, QScrollArea, QWidget  # noqa: E402

APP = QApplication.instance() or QApplication([])
APP.setFont(QFont(APP.font().family(), 10))

from easymeasure.backend.server import Server  # noqa: E402
from easymeasure.backend.service import Backend  # noqa: E402
from easymeasure.frontend.connection import BackendConnection  # noqa: E402
from easymeasure.frontend import main_window as mw_mod  # noqa: E402
from easymeasure.frontend.panels import setup_panel as sp_mod  # noqa: E402

RED = QColor(230, 40, 40)


# -- helpers --------------------------------------------------------------------------
def pump(seconds: float = 0.3, until=None) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        APP.processEvents()
        # no running event loop here: deliver deleteLater() explicitly
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        if until is not None and until():
            return True
        time.sleep(0.01)
    return bool(until()) if until else True


def save(widget: QWidget, name: str, marks=(), labels=None, crop: QWidget = None) -> None:
    """Photograph *widget*; outline the widgets in *marks* (with optional numbered
    *labels*), optionally only the region of *crop*."""
    pump(0.25)
    pix = widget.grab()
    if marks:
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(RED, 3)
        p.setPen(pen)
        f = QFont(APP.font())
        f.setBold(True)
        f.setPointSize(11)
        p.setFont(f)
        limit = QRect(QPoint(0, 0), widget.size())
        if crop is not None:
            limit = QRect(crop.mapTo(widget, QPoint(0, 0)), crop.size())
        rects = []
        for m in marks:
            if m is not None and m.isVisible():
                tl = m.mapTo(widget, QPoint(0, 0))
                rects.append(QRect(tl.x() - 3, tl.y() - 3, m.width() + 6, m.height() + 6))
            else:
                rects.append(None)
        for k, m in enumerate(marks):
            r = rects[k]
            if r is None:
                continue
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 5, 5)
            if labels and k < len(labels) and labels[k]:
                txt = str(labels[k])
                br = p.fontMetrics().boundingRect(txt)
                box = QRect(r.right() - br.width() - 10, r.top() - br.height() - 6,
                            br.width() + 12, br.height() + 6)
                clash = any(o is not None and o is not r and o.intersects(box) and
                            not o.contains(r) for o in rects)
                if box.top() < limit.top() or clash:
                    # no room above: beside it, else inside its right end
                    mid = r.center().y() - box.height() // 2
                    if r.right() + 4 + box.width() <= limit.right():
                        box.moveTopLeft(QPoint(r.right() + 4, mid))
                    else:
                        box.moveTopRight(QPoint(r.right() - 4, mid))
                p.setBrush(RED)
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(box, 4, 4)
                p.setPen(QColor("white"))
                p.drawText(box, Qt.AlignCenter, txt)
        p.end()
    if crop is not None:
        tl = crop.mapTo(widget, QPoint(0, 0))
        pix = pix.copy(QRect(tl, crop.size()))
    OUT.mkdir(parents=True, exist_ok=True)
    pix.save(str(OUT / (name + ".png")))
    print("  ", name)


#: dialog class -> list of (fill(dialog), screenshot name, marks(dialog))
SCRIPT = {}


def expect(cls, fill, name=None, marks=lambda d: ()):
    SCRIPT.setdefault(cls, []).append((fill, name, marks))


def _patched_exec(self):
    fill, name, marks = SCRIPT[type(self)].pop(0)
    self.show()
    self.resize(max(self.width(), 600), self.height())
    pump(0.3)
    fill(self)
    pump(0.4)
    if name:
        m = marks(self)
        if isinstance(m, tuple):          # (widgets, labels)
            save(self, name, *m)
        else:
            save(self, name, m)
    if hasattr(self, "_ok"):
        self._ok()
    else:
        self.accept()
    self.hide()
    return QDialog.Accepted if self.result() == QDialog.Accepted else QDialog.Rejected


for _cls in (sp_mod.InstrumentDialog, sp_mod.AliasDialog, sp_mod.CompositeDialog,
             sp_mod.UserActionDialog, sp_mod.ParameterDialog):
    _cls.exec = _patched_exec


def _first_start_open(self):
    self.show()
    pump(0.3)
    self.data_dir.setText(str(HOME / "data"))
    self.ppt.setText(str(HOME / "lab_notebook.pptx"))
    save(self, "qs-first-start", [self.data_dir, self.ppt, self.ok], ["1", "2", "3"])
    self.accept()


mw_mod.FirstStartDialog.open = _first_start_open


def set_options(d, **kw):
    o = json.loads(d.options.toPlainText() or "{}")
    o.update(kw)
    d.options.setPlainText(json.dumps(o, indent=2))


def option_line(d, key):
    """The options box itself (the text cannot be outlined line by line)."""
    return d.options


def select_row(table, text, col=0):
    for r in range(table.rowCount()):
        it = table.item(r, col)
        if it is not None and it.text() == text:
            table.selectRow(r)
            table.setCurrentCell(r, col)
            return r
    raise KeyError(text)


def wait_idle(be, timeout=900):
    t0 = time.time()
    pump(1.0)
    while time.time() - t0 < timeout:
        pump(0.5)
        st = be.queue_state()
        if st["state"] == "idle" and not st["processing"]:
            pump(1.5)
            return st["history"][-1]
    raise TimeoutError("sweep did not finish")


def tab(w, name):
    w.splitter.setSizes([860, 740])
    for i in range(w.left_tabs.count()):
        if w.left_tabs.tabText(i).replace("&&", "&").startswith(name):
            w.left_tabs.setCurrentIndex(i)
            pump(0.3)
            return w.left_tabs.widget(i)
    raise KeyError(name)


def show_channels(w, channels):
    """Make the live view show exactly these channels, one plot each."""
    lv = w.live
    while len(lv.plots) > len(channels):
        lv.plots[-1].closeRequested.emit(lv.plots[-1])
        pump(0.2)
    while len(lv.plots) < len(channels):
        lv.add_plot()
        pump(0.2)
    for p, ch in zip(lv.plots, channels):
        p.channel_box.setCurrentText(ch)
        p.mode_box.setCurrentText("Auto")
    pump(1.5)
    return lv.plots


def magnet_at_zero(be):
    be.write_channel(name="B", value=0.0)
    pump(120, lambda: be.read_channel(name="magnet.status") == "HOLDING")


def run_sweep(w, be, definition):
    w.editor.load_definition(definition)
    w.editor._queue(True)
    return wait_idle(be)


# -- the session ----------------------------------------------------------------------
def start(settings=None):
    be = Backend(home=HOME / "home", log_to_console=False,
                 settings=dict({"folders_confirmed": False, "autosave_setup": True,
                                "data_dir": str(HOME / "data"), "auto_ppt": False},
                               **(settings or {})))
    srv = Server(be, port=0, runtime_file=HOME / "rt.json")
    srv.start()
    conn = BackendConnection(runtime_file=HOME / "rt.json")
    w = mw_mod.MainWindow(conn)
    w.resize(1600, 1250)
    w.show()
    pump(0.5)
    w.splitter.setSizes([720, 880])
    pump(10, lambda: conn.is_connected and w.model.state.get("settings"))
    pump(1.0)
    pump(5, lambda: bool(w.setup_panel.drivers))
    return be, srv, w


def quick_start(be, w):
    print("quick start")
    sp = w.setup_panel
    tab(w, "Instruments")
    save(w, "qs-window", [w.left_tabs, w.data_tabs, w.monitor, w.manual],
         ["instruments · sweeps · queue", "data", "monitor", "manual"])
    add_btn = next(b for b in sp.findChildren(QWidget)
                   if getattr(b, "text", lambda: "")() == "Add…" and b.isVisible())
    save(w, "qs-add-button", [add_btn], crop=w.left_tabs)

    def fill_bg(d):
        d.name.setText("bg")
        d.driver.setCurrentText("sim_smu")
        d.address.setEditText("NONE")
    expect(sp_mod.InstrumentDialog, fill_bg, "qs-add-bg",
           lambda d: [d.name, d.driver, d.address])
    sp._add_inst()
    pump(3, lambda: any(i["name"] == "bg" for i in w.model.instruments()))

    def fill_lockin(d):
        d.name.setText("lockin")
        d.driver.setCurrentText("sim_lockin")
        d.address.setEditText("NONE")
        set_options(d, sample="graphene")
    expect(sp_mod.InstrumentDialog, fill_lockin, "qs-add-lockin",
           lambda d: [d.name, d.driver, option_line(d, "sample")], )
    sp._add_inst()
    pump(3, lambda: any(i["name"] == "lockin" for i in w.model.instruments()))
    pump(1.0)
    select_row(sp.inst, "bg")
    pump(0.5)
    select_row(sp.raw, "voltage")
    save(w, "qs-instruments", [sp.inst, sp.raw, sp.mk_alias], ["1", "2", "3"], crop=w.left_tabs)

    def fill_vbg(d):
        d.name.setText("Vbg")
        d.min.setText("-10")
        d.max.setText("10")
        d.rate.setText("5")
        d.unit.setText("V")
        d.description.setText("back gate")
    expect(sp_mod.AliasDialog, fill_vbg, "qs-alias-vbg",
           lambda d: [d.name, d.min, d.max, d.rate])
    sp._alias_from_raw()
    pump(3, lambda: any(a["name"] == "Vbg" for a in w.model.aliases()))

    select_row(sp.raw, "current")

    def fill_ileak(d):
        d.name.setText("Ileak")
        d.unit.setText("A")
        d.description.setText("back-gate leakage current")
    expect(sp_mod.AliasDialog, fill_ileak, None)
    sp._alias_from_raw()
    pump(3, lambda: any(a["name"] == "Ileak" for a in w.model.aliases()))

    select_row(sp.inst, "lockin")
    pump(0.5)
    select_row(sp.raw, "X")

    def fill_rxx(d):
        d.name.setText("Rxx")
        d.scale.setText("1e8")
        d.unit.setText("Ω")
        d.description.setText("four-probe resistance (lock-in voltage / 10 nA)")
    expect(sp_mod.AliasDialog, fill_rxx, "qs-alias-rxx",
           lambda d: [d.name, d.scale, d.unit])
    sp._alias_from_raw()
    pump(3, lambda: any(a["name"] == "Rxx" for a in w.model.aliases()))
    pump(1.0)
    save(w, "qs-aliases", [sp.alias], crop=w.left_tabs)

    # try it by hand
    mp = w.manual
    pump(5, lambda: "Vbg" in mp.rows)
    mp.rows["Vbg"].editor.setValue(2.0)
    pump(2.0)
    be.monitor_add("Rxx", 0.3)
    pump(3.0)
    save(w, "qs-manual", [mp.rows["Vbg"].editor, w.monitor], ["1", "2"])
    mp.rows["Vbg"].editor.setValue(0.0)
    pump(1.5)
    be.monitor_remove("Rxx")
    # Stop ramps lights up while a ramp runs
    mp.rows["Vbg"].editor.setValue(-8.0)
    pump(3, lambda: mp.stop_ramps.isEnabled())
    pump(0.4)
    save(w, "qs-stop-ramps", [mp.stop_ramps, mp.rows["Vbg"].editor], ["1", "2"], crop=w.manual)
    mp.stop_ramps.click()
    pump(3, lambda: not mp.stop_ramps.isEnabled() and not mp._pending.get("Vbg"))
    mp.rows["Vbg"].editor.setValue(0.0)
    pump(3, lambda: not mp._pending.get("Vbg"))
    pump(0.5)

    # the first sweep
    ed = tab(w, "Sweep")
    d = json.loads((ROOT / "examples" / "graphene_dirac.json").read_text())
    w.editor.load_definition(d)
    pump(0.5)
    w.editor.validate()
    pump(5, lambda: "✔" in w.editor.status.text())
    dim = w.editor.dim_stack.currentWidget()
    save(w, "qs-sweep-editor", [w.editor.name, dim.points, dim.sweep_box, dim.rec_box,
                                w.editor.btn_validate, w.editor.btn_run],
         ["1", "2", "3", "4", "5", "6"], crop=w.left_tabs)
    w.editor._queue(True)
    pump(4.0)
    save(w, "qs-running", [w.data_tabs])
    wait_idle(be)
    pump(2.0)
    plots = show_channels(w, ["Rxx", "Ileak"])
    save(w, "qs-live", [plots[0]])
    tab(w, "Queue")
    save(w, "qs-queue", [], crop=w.left_tabs)
    del ed


def tutorial_fan(be, w):
    print("tutorial 1: Landau fan")
    sp = w.setup_panel
    tab(w, "Instruments")

    def fill_magnet(d):
        d.name.setText("magnet")
        d.driver.setCurrentText("sim_magnet")
        d.address.setEditText("NONE")
    expect(sp_mod.InstrumentDialog, fill_magnet, "t1-add-magnet",
           lambda d: [d.name, d.driver, d.address])
    sp._add_inst()
    pump(3, lambda: any(i["name"] == "magnet" for i in w.model.instruments()))
    pump(1.0)
    select_row(sp.inst, "magnet")
    pump(0.5)
    select_row(sp.raw, "field")

    def fill_b(d):
        d.name.setText("B")
        d.min.setText("-9")
        d.max.setText("9")
        d.unit.setText("T")
        d.description.setText("magnetic field")
    expect(sp_mod.AliasDialog, fill_b, "t1-alias-b", lambda d: [d.name, d.min, d.max])
    sp._alias_from_raw()
    pump(3, lambda: any(a["name"] == "B" for a in w.model.aliases()))

    tab(w, "Sweep")
    d = json.loads((ROOT / "examples" / "graphene_fan.json").read_text())
    w.editor.load_definition(d)
    pump(0.5)
    w.editor.dim_list.setCurrentRow(0)
    pump(0.3)
    dim0 = w.editor.dim_stack.currentWidget()
    save(w, "t1-dim1", [w.editor.dim_list, dim0.sweep_box, dim0.snake, dim0.rec_box],
         ["1", "2", "3", "4"], crop=w.left_tabs)
    w.editor.dim_list.setCurrentRow(1)
    pump(0.3)
    dim1 = w.editor.dim_stack.currentWidget()
    save(w, "t1-dim2", [dim1.sweep_box, dim1.hook_checks["post_sweep"], dim1.hooks["post_sweep"]],
         ["1", "2", "3"], crop=w.left_tabs)
    w.editor.validate()
    pump(5, lambda: "✔" in w.editor.status.text())
    be.monitor_add("B", 1.0)
    w.editor._queue(True)
    pump(25.0)
    save(w, "t1-fan-running", [w.monitor])
    wait_idle(be, 1800)
    pump(2.0)
    plots = show_channels(w, ["Rxx"])
    save(w, "t1-fan", [plots[0]])
    # the same data as curves: one line per field, slider for B
    p = plots[0]
    p.mode_box.setCurrentText("Curve")
    pump(1.0)
    for s in p._sliders.values():
        s.setValue(int(s.maximum() * 0.8))
    pump(1.5)
    save(w, "t1-fan-curve", [p.mode_box] + list(p._sliders.values()), ["1", "2"],
         crop=w.data_tabs)
    p.mode_box.setCurrentText("Auto")
    be.monitor_remove("B")
    magnet_at_zero(be)


def tutorial_nd(be, w):
    print("tutorial 2: n and D")
    sp = w.setup_panel
    tab(w, "Instruments")

    def fill_tg(d):
        d.name.setText("tg")
        d.driver.setCurrentText("sim_smu")
        d.address.setEditText("NONE")
    expect(sp_mod.InstrumentDialog, fill_tg, None)
    sp._add_inst()
    pump(3, lambda: any(i["name"] == "tg" for i in w.model.instruments()))
    pump(1.0)
    select_row(sp.inst, "tg")
    pump(0.5)
    select_row(sp.raw, "voltage")

    def fill_vtg(d):
        d.name.setText("Vtg")
        d.min.setText("-10")
        d.max.setText("10")
        d.rate.setText("5")
        d.unit.setText("V")
        d.description.setText("top gate")
    expect(sp_mod.AliasDialog, fill_vtg, None)
    sp._alias_from_raw()
    pump(3, lambda: any(a["name"] == "Vtg" for a in w.model.aliases()))

    # -- a second lock-in for the current, and Rxx = Vxx / I
    def fill_ilockin(d):
        d.name.setText("ilockin")
        d.driver.setCurrentText("sim_lockin")
        d.address.setEditText("NONE")
        set_options(d, sample="graphene", measure="current", source="lockin.amplitude")
    expect(sp_mod.InstrumentDialog, fill_ilockin, "t2-add-ilockin",
           lambda d: ([d.name, d.driver, d.options], ["1", "2", "3"]))
    sp._add_inst()
    pump(3, lambda: any(i["name"] == "ilockin" for i in w.model.instruments()))
    pump(1.0)
    for inst, alias, unit, desc in (("lockin", "Vxx", "V", "four-probe voltage"),
                                    ("ilockin", "I", "A", "AC current through the sample")):
        select_row(sp.inst, inst)
        pump(0.5)
        select_row(sp.raw, "X")

        def fill(d, alias=alias, unit=unit, desc=desc):
            d.name.setText(alias)
            d.unit.setText(unit)
            d.description.setText(desc)
        expect(sp_mod.AliasDialog, fill, None)
        sp._alias_from_raw()
        pump(3, lambda alias=alias: any(a["name"] == alias for a in w.model.aliases()))
    pump(0.5)
    select_row(sp.alias, "Rxx")
    sp._remove_alias()
    pump(3, lambda: not any(a["name"] == "Rxx" for a in w.model.aliases()))

    def fill_read(d):
        d.name.setText("Rxx")
        d.type.setCurrentIndex(d.type.findData("read"))
        d.expression.setText("Vxx / I")
        d.r_unit.setText("Ω")
        d.description.setText("four-probe resistance")
        d.check()
        pump(3, lambda: "✔" in d.info.text())
    expect(sp_mod.CompositeDialog, fill_read, "t2-read",
           lambda d: ([d.name, d.type, d.expression, d.r_unit, d.check_btn],
                      ["1", "2", "3", "4", "5"]))
    sp._edit_comp(None)
    pump(3, lambda: any(c["name"] == "Rxx" for c in w.model.composites()))

    # the device's constants as parameters
    params = [("Cb", 0.6, "1e12 cm^-2/V", "back-gate capacitance / e"),
              ("Ct", 0.9, "1e12 cm^-2/V", "top-gate capacitance / e"),
              ("n0", -0.15, "1e12 cm^-2", "intrinsic doping"),
              ("kD", 0.0452, "V/nm per 1e12 cm^-2", "e / 2 eps0")]
    for i, (pn, pv, pu, pd) in enumerate(params):
        def fill_p(d, pn=pn, pv=pv, pu=pu, pd=pd):
            d.name.setText(pn)
            d.kind.setCurrentIndex(0)
            d.value.setText(repr(pv))
            d.unit.setText(pu)
            d.description.setText(pd)
        expect(sp_mod.ParameterDialog, fill_p, "t2-parameter" if i == 0 else None,
               lambda d: ([d.name, d.kind, d.value, d.unit], ["1", "2", "3", "4"]))
        sp._edit_param(None)
        pump(3, lambda pn=pn: any(p["name"] == pn for p in w.model.parameters()))
    pump(1.0)
    # give the Parameters box room: a wide left side, a tall bottom row
    old = w.splitter.sizes()
    log = w.findChild(QDockWidget, "log_dock")
    log.hide()
    w.splitter.setSizes([1150, 450])
    pbox = sp.param.parentWidget()
    last = pbox.parentWidget()                     # the splitter Parameters | Actions
    vsplit = last.parentWidget()
    vsplit.setSizes([330, 250, 260])
    last.setSizes([800, 300])
    for c in range(sp.param.columnCount() - 1):
        sp.param.resizeColumnToContents(c)
    pump(0.8)
    save(w, "t2-parameters", crop=pbox)
    log.show()
    w.splitter.setSizes(old)
    pump(0.3)

    def fill_nd(d):
        d.name.setText("nD")
        d.type.setCurrentIndex(0)
        while d.vars.rowCount():
            d.vars.removeRow(0)
        d._add_var({"name": "n", "min": -4, "max": 4, "unit": "1e12 cm^-2",
                    "description": "carrier density"})
        d._add_var({"name": "D", "min": -0.45, "max": 0.45, "unit": "V/nm",
                    "description": "displacement field"})
        d.targets.setPlainText("Vbg = (n - n0 + D/kD) / (2*Cb)\n"
                               "Vtg = (n - n0 - D/kD) / (2*Ct)")
        d.out_of_range.setCurrentIndex(d.out_of_range.findData("skip"))
        d.description.setText("carrier density and displacement field of the dual-gated bilayer")
        d.check()
        pump(3, lambda: "✔" in d.info.text())
    expect(sp_mod.CompositeDialog, fill_nd, "t2-composite",
           lambda d: ([d.type, d.vars, d.targets, d.out_of_range, d.check_btn],
                      ["1", "2", "3", "4", "5"]))
    sp._edit_comp(None)
    pump(3, lambda: any(c["name"] == "nD" for c in w.model.composites()))
    pump(1.0)
    save(w, "t2-composites", [sp.comp], crop=w.left_tabs)

    mp = w.manual
    pump(5, lambda: "nD" in mp.groups and "n" in mp.rows)
    g = mp.groups["nD"]
    mp.set_group("nD", zero=True)
    pump(10, lambda: be.channels.last_value("D") == 0.0 and not mp._pending.get("D"))
    pump(1.0)
    save(w, "t2-manual", [g["label"], g["zero"]], ["1", "2"], crop=w.manual)

    tab(w, "Sweep")
    d = json.loads((ROOT / "examples" / "graphene_nD.json").read_text())
    w.editor.load_definition(d)
    pump(0.5)
    w.editor.dim_list.setCurrentRow(0)
    pump(0.3)
    w.editor.validate()
    pump(5, lambda: "valid" in w.editor.status.text())
    pump(0.5)
    dim0 = w.editor.dim_stack.currentWidget()
    save(w, "t2-sweep", [w.editor.dim_list, dim0.sweep_box, dim0.rec_box, w.editor.status],
         ["1", "2", "3", "4"], crop=w.left_tabs)
    magnet_at_zero(be)
    w.editor._queue(True)
    wait_idle(be, 1800)
    pump(2.0)
    plots = show_channels(w, ["Rxx"])
    save(w, "t2-map", [plots[0]])


def tutorial_threshold(be, w):
    print("tutorial 3: stop at a threshold")
    tab(w, "Sweep")
    d = json.loads((ROOT / "examples" / "graphene_leakage.json").read_text())
    w.editor.load_definition(d)
    pump(0.5)
    dim0 = w.editor.dim_stack.currentWidget()
    hk = dim0.hooks["post_record"]
    save(w, "t3-action", [dim0.hook_checks["post_record"], hk.code, hk.action_box],
         ["1", "2", "3"], crop=w.left_tabs)
    be.set_many(values={"Vbg": 0.0, "Vtg": 0.0})
    w.editor._queue(True)
    wait_idle(be)
    pump(2.0)
    plots = show_channels(w, ["Ileak", "Rxx"])
    save(w, "t3-result", [plots[0]])

    sp = w.setup_panel
    tab(w, "Instruments")

    def fill_ua(d):
        d.name.setText("gates to zero")
        d.description.setText("ramp both gates to 0 V")
        d.code.setPlainText("set_many({'Vbg': 0, 'Vtg': 0})\nprint('both gates at 0 V')")
    expect(sp_mod.UserActionDialog, fill_ua, "t3-user-action",
           lambda d: [d.name, d.code])
    sp._edit_uact(None)
    pump(3, lambda: bool(w.model.user_actions()))
    pump(1.0)
    sp.uact.selectRow(0)
    sp._run_uact()
    pump(8, lambda: "done" in sp.msg.text())
    pump(1.0)
    save(w, "t3-actions-panel", [sp.uact, sp.uact_run], ["1", "2"], crop=w.left_tabs)
    mp = w.manual
    pump(3, lambda: "gates to zero" in mp.action_buttons)
    pump(0.5)
    # the actions bar stays on top; scroll the channel list down to the parameters
    area = next(a for a in mp.findChildren(QScrollArea) if a.widget() is mp.host)
    area.ensureWidgetVisible(mp.param_header, 0, 0)
    bar = area.verticalScrollBar()
    bar.setValue(bar.maximum())
    area.horizontalScrollBar().setValue(0)
    pump(0.5)
    save(w, "t3-manual-actions", [mp.action_buttons["gates to zero"], mp.params["Cb"]["label"]],
         ["1", "2"], crop=w.manual)


def scripting(be, w):
    print("scripting: a queue script")
    qp = w.queue_panel
    tab(w, "Queue")
    qp.load_script_file(str(ROOT / "examples" / "scripts" / "find_dirac_then_zoom.py"))
    from PySide6.QtWidgets import QSplitter
    split = qp.findChild(QSplitter)
    old_sizes = split.sizes()
    split.setSizes([120, 160, 700])            # room for the script
    pump(0.5)
    save(w, "s-script-editor", [qp.script_open, qp.script, qp.script_name, qp.script_add],
         ["1", "2", "3", "4"], crop=w.left_tabs)
    split.setSizes(old_sizes)
    be.set_many(values={"Vbg": 0.0, "Vtg": 0.0})
    qp._add_script(False)
    pump(3, lambda: bool(be.queue_state()["queued"]))
    be.queue_start()
    pump(3.0)
    wait_idle(be)
    pump(2.0)
    plots = show_channels(w, ["Rxx"])
    pump(1.0)
    save(w, "s-result", [qp.history, w.log_panel], ["1", "2"])

    # a custom path in the editor: two channels with mappings in one dimension
    tab(w, "Sweep")
    w.editor.load_definition(json.loads((ROOT / "examples" / "graphene_circle.json").read_text()))
    pump(0.5)
    dim0 = w.editor.dim_stack.currentWidget()
    t = dim0.table
    w.splitter.setSizes([1250, 350])               # wide enough for every column
    t.setColumnWidth(1, 70)
    t.setColumnWidth(2, 70)
    pump(0.8)
    save(w, "s-mapping", [dim0.sweep_box], ["1"], crop=w.left_tabs)
    tab(w, "Sweep")
    be.set_many(values={"n": 0.0, "D": 0.0})
    w.editor._queue(True)
    wait_idle(be)
    pump(2.0)
    plots = show_channels(w, ["Rxx"])
    pump(1.0)
    save(w, "s-circle", [plots[0]])


def tutorial_vna():
    print("tutorial 4: VNA")
    be, srv, w = start({"folders_confirmed": True})
    try:
        sp = w.setup_panel
        tab(w, "Instruments")

        def fill_vna(d):
            d.name.setText("vna")
            d.driver.setCurrentText("sim_vna")
            d.address.setEditText("NONE")
        expect(sp_mod.InstrumentDialog, fill_vna, "t4-add-vna",
               lambda d: [d.name, d.driver, d.address])
        sp._add_inst()
        pump(3, lambda: any(i["name"] == "vna" for i in w.model.instruments()))
        be.load_setup(path=str(ROOT / "examples" / "vna_setup.json"), replace=False)
        pump(2.0)
        select_row(sp.inst, "vna")
        pump(1.0)
        save(w, "t4-vna-channels", [sp.raw], crop=w.left_tabs)
        be.monitor_add("S11", 0.5)
        pump(4.0)
        save(w, "t4-monitor", [w.monitor])
        be.monitor_remove("S11")

        tab(w, "Sweep")
        d = json.loads((ROOT / "examples" / "vna_power.json").read_text())
        w.editor.load_definition(d)
        pump(0.5)
        dim0 = w.editor.dim_stack.currentWidget()
        save(w, "t4-sweep", [dim0.sweep_box, dim0.rec_box], ["1", "2"], crop=w.left_tabs)
        w.editor._queue(True)
        wait_idle(be)
        pump(2.0)
        plots = show_channels(w, ["S11", "phase"])
        save(w, "t4-power-map", [plots[0]])
        p = plots[0]
        p.mode_box.setCurrentText("Stacked curves")
        pump(1.5)
        save(w, "t4-power-stacked", [p.mode_box], crop=w.data_tabs)
        p.mode_box.setCurrentText("Auto")

        d = json.loads((ROOT / "examples" / "vna_power_field.json").read_text())
        w.editor.load_definition(d)
        pump(0.5)
        w.editor.dim_list.setCurrentRow(1)
        pump(0.3)
        dim1 = w.editor.dim_stack.currentWidget()
        save(w, "t4-3d-editor", [w.editor.dim_list, dim1.sweep_box, dim1.hooks["post_sweep"]],
             ["1", "2", "3"], crop=w.left_tabs)
        w.editor._queue(True)
        wait_idle(be, 1800)
        pump(2.0)
        # plot 1: S11 vs frequency and power, slider on B; plot 2: vs frequency and B
        p0, p1 = show_channels(w, ["S11", "S11"])
        p1.y_box.setCurrentIndex(p1.y_box.findData("d1"))
        pump(0.5)
        for s in p0._sliders.values():
            s.setValue(s.maximum() // 2)
        for s in p1._sliders.values():
            s.setValue(int(s.maximum() * 0.6))
        pump(2.0)
        save(w, "t4-3d-maps", [p0.y_box, list(p0._sliders.values())[0] if p0._sliders else None,
                               p1.y_box, list(p1._sliders.values())[0] if p1._sliders else None],
             ["1", "2", "3", "4"])
    finally:
        w.close()
        pump(0.3)
        srv.stop()
        be.close()


def main(parts):
    t0 = time.time()
    if OUT.exists() and not parts:
        shutil.rmtree(OUT)
    want = set(parts) or {"qs", "t1", "t2", "t3", "t4", "s"}
    if want & {"qs", "t1", "t2", "t3", "s"}:
        be, srv, w = start()
        try:
            pump(5, lambda: hasattr(w, "first_start_dialog"))
            pump(2.0)
            if "qs" in want:
                quick_start(be, w)
            else:
                be.load_setup(path=str(ROOT / "examples" / "graphene_setup.json"))
                pump(3.0)
            if "t1" in want:
                tutorial_fan(be, w)
            if "t2" in want:
                tutorial_nd(be, w)
            if "t3" in want:
                tutorial_threshold(be, w)
            if "s" in want:
                scripting(be, w)
        finally:
            w.close()
            pump(0.3)
            srv.stop()
            be.close()
    if "t4" in want:
        tutorial_vna()
    print("done in %.0f s; images in %s" % (time.time() - t0, OUT))


if __name__ == "__main__":
    main(sys.argv[1:])
    os._exit(0)
