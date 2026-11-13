// Right-hand side: Monitor (live tiles pushed by the backend), Manual control
// (spin box + slider per writable alias) and the Log panel.

import { h, btn, bar, fmt, clock, promptDialog, confirmDialog, throttle, autoColumns } from "../ui.js";
import { PlotCanvas } from "../plot.js";
import { evaluate } from "../expr.js";
import { NDArray } from "../codec.js";
import * as cmaps from "../colormaps.js";

const HISTORY = 3000;
const MAX_TRACES = 64;      // vectors up to this length keep a per-element history
const TIME_VIEW_MAX = 16;   // ... and start in the "vs time" view up to this length
const MAX_COLUMNS = 8;
const TILE_MIN_W = 190;       // px: below this the tiles scroll sideways instead of shrinking
const TILE_PLOT_MIN_H = 50;
const traceColor = (i, n) => cmaps.color("turbo", n > 1 ? 0.08 + 0.84 * i / (n - 1) : 0.5);
function fmtVector(d) {
  if (d.length <= 8) return "[" + Array.from(d, (x) => fmt(x, 4)).join(", ") + "]";
  let lo = Infinity, hi = -Infinity;
  for (const q of d) { if (q < lo) lo = q; if (q > hi) hi = q; }
  return `[${d.length}]  min ${fmt(lo, 4)}  max ${fmt(hi, 4)}`;
}
const SPANS = [["1 min", 60], ["10 min", 600], ["1 h", 3600], ["all", 0]];

class MonitorTile {
  constructor(name, onRemove) {
    this.name = name;
    this.t = [];
    this.v = [];
    this.ta = [];      // 1-D vectors: times and values
    this.va = [];
    this.latest = null;
    this.kind = null;
    this.dirty = false;
    this.viewSet = false;
    this.view = h("select", { class: "mon-view", hidden: true, title: "vector channel: every element against time, or the latest value against the element index",
      on: { change: () => { this.dirty = true; } } }, [h("option", { text: "vs time", value: "time" }), h("option", { text: "vs index", value: "index" })]);
    this.value = h("div", { class: "mon-value", text: "—" });
    this.err = h("div", { class: "mon-err" });
    this.span = h("select", { on: { change: () => { this.dirty = true; } } }, SPANS.map(([l], i) => h("option", { text: l, value: i, selected: i === 1 })));
    this.pc = new PlotCanvas({ compact: true });
    this.rm = btn("✕", () => onRemove(name), { cmd: "monitor_remove", title: "stop monitoring" });
    this.el = h("div", { class: "mon-tile" },
      h("div", { class: "mon-head" }, h("b", { text: name }), h("span", { class: "grow" }), this.view, this.span, this.rm),
      this.value, this.pc.el, this.err);
  }

  push(msg) {
    if (msg.error !== undefined) { this.err.textContent = "⚠ " + String(msg.error).slice(0, 200); return; }
    this.err.textContent = "";
    const v = msg.value, t = Number(msg.t || Date.now() / 1000);
    if (typeof v === "string") { this.kind = "str"; this.latest = v; }
    else if (v instanceof NDArray && v.ndim >= 1) {
      this.kind = "array";
      this.latest = v;
      if (v.ndim === 1 && v.size > 0 && v.size <= MAX_TRACES) {
        if (this.va.length && this.va[this.va.length - 1].length !== v.size) { this.ta = []; this.va = []; }
        this.ta.push(t);
        this.va.push(Float64Array.from(v.data));
        if (this.ta.length > HISTORY) { this.ta.splice(0, this.ta.length - HISTORY); this.va.splice(0, this.va.length - HISTORY); }
      }
      if (!this.viewSet) { this.view.value = v.ndim === 1 && v.size <= TIME_VIEW_MAX ? "time" : "index"; this.viewSet = true; }
    }
    else {
      const f = Number(v instanceof NDArray ? v.data[0] : v);
      if (!Number.isFinite(f) && !Number.isNaN(f)) return;
      this.kind = "scalar";
      this.latest = f;
      this.t.push(t); this.v.push(f);
      if (this.t.length > HISTORY) { this.t.splice(0, this.t.length - HISTORY); this.v.splice(0, this.v.length - HISTORY); }
    }
    this.dirty = true;
  }

  redraw() {
    if (!this.dirty) return;
    this.dirty = false;
    if (this.kind === "scalar") {
      this.value.textContent = fmt(this.latest, 7);
      this.pc.el.hidden = false;
      const span = SPANS[Number(this.span.value)][1];
      const tl = this.t[this.t.length - 1];
      let i0 = 0;
      if (span) while (i0 < this.t.length && this.t[i0] < tl - span) i0++;
      const x = Float64Array.from(this.t.slice(i0), (t) => t - tl), y = Float64Array.from(this.v.slice(i0));
      this.pc.setContent({ kind: "curves", series: [{ x, y, color: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#4cc9f0", width: 1.2 }] });
    } else if (this.kind === "str") {
      this.value.textContent = String(this.latest).slice(0, 80);
      this.pc.el.hidden = true;
    } else if (this.kind === "array") {
      const a = this.latest;
      this.pc.el.hidden = false;
      this.view.hidden = !(a.ndim === 1 && a.size <= MAX_TRACES);
      if (a.ndim === 1) {
        this.value.textContent = fmtVector(a.data);
        if (!this.view.hidden && this.view.value === "time" && this.va.length) {
          // one curve per element against time
          const span = SPANS[Number(this.span.value)][1];
          const tl = this.ta[this.ta.length - 1];
          let i0 = 0;
          if (span) while (i0 < this.ta.length && this.ta[i0] < tl - span) i0++;
          const x = Float64Array.from(this.ta.slice(i0), (q) => q - tl);
          const n = this.va[0].length, rows = this.va.slice(i0);
          const series = [];
          for (let j = 0; j < n; j++) series.push({ x, y: Float64Array.from(rows, (r) => r[j]), color: traceColor(j, n), width: 1.2, name: `[${j}]` });
          this.pc.setContent({ kind: "curves", series });
          return;
        }
        const x = Float64Array.from({ length: a.size }, (_, i) => i);
        this.pc.setContent({ kind: "curves", series: [{ x, y: a.data, color: "#80ed99", width: 1 }] });
      } else {
        const ny = a.shape[0], nx = a.size / ny;
        let s = 0, n = 0; for (const q of a.data) if (q === q) { s += q; n++; }
        let lo = Infinity, hi = -Infinity; for (const q of a.data) { if (q < lo) lo = q; if (q > hi) hi = q; }
        this.pc.levels = hi > lo ? [lo, hi] : [lo - 0.5, lo + 0.5];
        this.value.textContent = `${a.shape.join("×")}  mean ${fmt(n ? s / n : NaN, 4)}`;
        this.pc.setContent({ kind: "image", x: Float64Array.from({ length: nx }, (_, i) => i),
          y: Float64Array.from({ length: ny }, (_, i) => i), z: a.data, nx, ny, version: Math.random() });
      }
    }
  }
}

export class MonitorPanel {
  constructor(app) {
    this.app = app;
    this.conn = app.conn;
    this.tiles = new Map();
    this.chan = h("input", { placeholder: "channel", list: "em-mon-channels", class: "w-chan" });
    this.list = h("datalist", { id: "em-mon-channels" });
    this.interval = h("input", { type: "number", value: "1", min: "0.1", step: "0.1", class: "w-num", title: "poll interval (s)" });
    this.msg = h("div", { class: "msg" });
    this.host = h("div", { class: "mon-tiles" });
    let cols = "0";
    try { const v = localStorage.getItem("em.monitor.columns"); if (v !== null) cols = v; } catch (e) { /* storage blocked */ }
    this.columns = h("select", { class: "mon-columns", title: "number of monitor tiles side by side (auto = the arrangement that shows every tile largest)",
      on: { change: () => this._setColumns() } },
      [h("option", { value: "0", text: "columns: auto" })].concat(Array.from({ length: MAX_COLUMNS }, (_, i) =>
        h("option", { value: String(i + 1), text: `${i + 1} column${i ? "s" : ""}` }))));
    this.columns.value = /^[0-8]$/.test(cols) ? cols : "0";
    this._eff = null;
    this._setColumns();
    this.el = h("div", { class: "panel monitor" },
      bar(this.chan, this.list, this.interval, h("span", { text: "s" }), btn("Monitor", () => this._add(), { cmd: "monitor_add" }),
        this.columns),
      this.msg, this.host);
    app.model.on("state", (st) => this._state(st));
    this.conn.on("event", (k, d) => { if (k === "monitor" && d) this._push(d); });
    this.conn.on("connected", () => this._history());
    setInterval(() => { if (this.el.offsetParent !== null) for (const t of this.tiles.values()) t.redraw(); }, 150);
    // every tile in view: they share the panel; it scrolls only below their minimum size
    new ResizeObserver(() => { if (this.columns.value === "0" && this._effCols() !== this._eff) this._relayout(); }).observe(this.host);
    setInterval(() => { if (this.columns.value === "0" && this.el.offsetParent !== null && this._effCols() !== this._eff) this._relayout(); }, 700);
  }

  _state(st) {
    const names = (st.monitor || []).map((m) => m.channel);
    this.list.replaceChildren(...this.app.model.readableChannels().map((n) => h("option", { value: n })));
    for (const n of names) {
      if (!this.tiles.has(n)) {
        const t = new MonitorTile(n, (x) => this._remove(x));
        this.tiles.set(n, t);
        this.host.append(t.el);
        this.app.applyPermissions(t.el);
      }
    }
    for (const [n, t] of this.tiles) if (!names.includes(n)) { t.el.remove(); this.tiles.delete(n); }
    this._relayout();
    for (const m of st.monitor || []) {
      const t = this.tiles.get(m.channel);
      if (t && t.kind === null && m.last_value !== null && m.last_value !== undefined) t.push({ value: m.last_value, t: m.last_time });
    }
    this.msg.textContent = names.length ? "" : "no monitored channels";
  }

  _effCols() {
    const n = Number(this.columns.value) || 0;
    if (n > 0) return n;
    if (!this.tiles.size || !this.host.clientHeight) return 1;
    let minH = 90;
    for (const t of this.tiles.values()) {
      if (t.el.offsetHeight) minH = Math.max(minH, t.el.offsetHeight - (t.pc.el.hidden ? 0 : t.pc.el.offsetHeight - TILE_PLOT_MIN_H));
    }
    return autoColumns(this.tiles.size, this.host.clientWidth, this.host.clientHeight, TILE_MIN_W, minH, 6, MAX_COLUMNS);
  }

  _setColumns() {
    try { localStorage.setItem("em.monitor.columns", this.columns.value); } catch (e) { /* storage blocked */ }
    this._relayout();
  }

  _relayout() {
    const n = this._effCols();
    this._eff = n;
    this.host.style.gridTemplateColumns = `repeat(${n}, minmax(${TILE_MIN_W}px, 1fr))`;
  }

  _push(d) {
    const t = this.tiles.get(d.channel);
    if (t) t.push(d);
  }

  async _history() {
    try {
      const hist = await this.conn.request("monitor_history");
      for (const [name, pts] of Object.entries(hist || {})) {
        let t = this.tiles.get(name);
        if (!t) { t = new MonitorTile(name, (x) => this._remove(x)); this.tiles.set(name, t); this.host.append(t.el); this.app.applyPermissions(t.el); }
        t.t = []; t.v = []; t.ta = []; t.va = [];
        for (const [tt, v] of pts) t.push({ t: tt, value: v });
      }
    } catch (e) { /* ignore */ }
  }

  async _add() {
    const name = this.chan.value.trim();
    if (!name) return;
    try { await this.conn.request("monitor_add", { channel: name, interval: Number(this.interval.value) || 1 }); this.app.model.scheduleRefresh(); }
    catch (e) { this.msg.textContent = "✘ " + e.message; }
  }

  async _remove(name) {
    try { await this.conn.request("monitor_remove", { channel: name }); this.app.model.scheduleRefresh(); }
    catch (e) { this.msg.textContent = "✘ " + e.message; }
  }
}

// ------------------------------------------------------------------ manual ----
const SLIDER_STEPS = 1000;

class ChannelRow {
  constructor(panel, meta) {
    this.panel = panel;
    this.meta = meta;
    this.name = meta.name;
    this.lo = typeof meta.min === "number" ? meta.min : null;
    this.hi = typeof meta.max === "number" ? meta.max : null;
    this.typing = false;
    this.unset = false;                       // no value yet: shows NaN
    this.backendSet = Number.isFinite(meta.last);
    const unitText = meta.display_unit || meta.unit || "";
    const choices = meta.choices || meta.allowed;
    this.numChoices = !!(choices && choices.length) && meta.dtype !== "str";
    this.label = h("span", { class: "man-label" + (meta.kind === "config" ? " config" : ""), text: this.name,
      title: `${this.name}${meta.target ? " → " + meta.target : ""}${meta.description ? "\n" + meta.description : ""}` });
    this.slider = null;
    if (choices && choices.length) {
      // only certain values accepted: a drop-down (also for numeric settings, e.g. NPLC)
      this.choices = choices;
      this.editor = h("select", { on: { change: () => panel.requestSet(this.name, this.choices[Number(this.editor.value)]) } },
        [h("option", { text: "", value: "-1", hidden: true })].concat(choices.map((a, i) =>
          h("option", { text: this.numChoices ? `${fmt(a, 6)}${unitText ? " " + unitText : ""}` : String(a), value: String(i) }))));
      this.editor.value = "-1";
    } else if (meta.dtype === "str" || meta.kind === "config") {
      // text, or a numeric setting without limits (any magnitude)
      const numeric = meta.dtype !== "str";
      this.editor = h("input", { placeholder: numeric ? unitText : "", class: numeric ? "man-num" : "",
        on: { change: () => {
          const t = this.editor.value.trim();
          panel.requestSet(this.name, numeric && t !== "" && Number.isFinite(Number(t)) ? Number(t) : t);
        } } });
    } else {
      const unit = unitText ? ` ${unitText}` : "";
      this.editor = h("input", { type: "number", step: "any", class: "man-num",
        title: `Enter: set (ramped)${unit ? "\nunit:" + unit : ""}\nright click: expression / read back` });
      if (this.lo !== null) this.editor.min = this.lo;
      if (this.hi !== null) this.editor.max = this.hi;
      this.editor.addEventListener("input", () => { this.typing = true; });
      this.editor.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { this.typing = false; this._commit(); }
        else if (e.key === "Escape") { this.typing = false; this.editor.blur(); panel.readBack(this.name); }
      });
      this.editor.addEventListener("blur", () => { if (this.typing) { this.typing = false; this._commit(); } });
      this.editor.addEventListener("contextmenu", (e) => { e.preventDefault(); this._menu(e); });
      if (this.lo !== null && this.hi !== null && this.hi > this.lo) {
        this.slider = h("input", { type: "range", min: 0, max: SLIDER_STEPS, step: 1, class: "man-slider",
          title: `${this.lo} … ${this.hi}${unit} (applied when released)` });
        this.slider.addEventListener("input", () => { this.editor.value = this._fromSlider(Number(this.slider.value)); });
        this.slider.addEventListener("change", () => panel.requestSet(this.name, this._fromSlider(Number(this.slider.value))));
      }
      this.unit = h("span", { class: "unit", text: unitText });
    }
    this.read = btn("↻", () => panel.readBack(this.name), { cmd: "read_channel", title: "read the present value from the instrument" });
    this.editor.dataset.cmd = "write_channel";
    if (this.slider) this.slider.dataset.cmd = "write_channel";
    this.el = h("div", { class: "man-row" }, this.label, this.editor, this.unit || null, this.slider || h("span"), this.read);
  }

  _commit() {
    if (this.editor.value.trim() === "") return;           // still NaN: nothing typed
    const v = Number(this.editor.value);
    if (!Number.isFinite(v)) return;
    if (this.unset) this.setUnset(false, v);
    if (this.slider) this.slider.value = this._toSlider(v);
    this.panel.requestSet(this.name, v);
  }

  /** No value yet: an empty box showing "NaN", the slider greyed out. */
  setUnset(on, keep = null) {
    if (on === this.unset || this.editor.type !== "number") return;
    this.unset = on;
    this.editor.placeholder = on ? "NaN" : "";
    this.editor.classList.toggle("nan", on);
    if (on) this.editor.value = "";
    else if (keep !== null && this.editor.value === "") this.editor.value = String(keep);
    if (this.slider) {
      this.slider.classList.toggle("nan", on);
      this.slider.dataset.busy = on ? "1" : "";      // applyPermissions keeps it disabled
      this.slider.disabled = on || !this.panel.app.conn.can("write_channel");
    }
  }

  _toSlider(v) { return Math.round(Math.min(1, Math.max(0, (v - this.lo) / (this.hi - this.lo))) * SLIDER_STEPS); }
  _fromSlider(i) { return Number((this.lo + (this.hi - this.lo) * i / SLIDER_STEPS).toPrecision(10)); }

  async _menu(ev) {
    const menu = h("div", { class: "ctxmenu" },
      h("div", { text: "Set from expression…", on: { click: () => { close(); this._expression(); } } }),
      h("div", { text: "Read back from instrument", on: { click: () => { close(); this.panel.readBack(this.name); } } }),
      h("div", { text: "Copy value", on: { click: () => { close(); navigator.clipboard && navigator.clipboard.writeText(this.editor.value); } } }));
    const close = () => { menu.remove(); document.removeEventListener("pointerdown", outside, true); };
    const outside = (e) => { if (!menu.contains(e.target)) close(); };
    Object.assign(menu.style, { left: ev.clientX + "px", top: ev.clientY + "px" });
    document.body.append(menu);
    setTimeout(() => document.addEventListener("pointerdown", outside, true));
  }

  async _expression() {
    if (!this.panel.app.conn.can("write_channel")) return;
    const cur = this.unset ? NaN : Number(this.editor.value);
    const txt = await promptDialog(`Set ${this.name}`, "Expression", this.panel.lastExpr[this.name] || "x",
      `New value as an expression of the present value x = ${fmt(cur, 8)} (e.g. x + 0.01, x/2, 0.1*pi, sqrt(2)/10)`);
    if (txt === null || !txt.trim()) return;
    let v;
    try { v = evaluate(txt, { x: cur }); } catch (e) { this.panel.say(`✘ ${this.name}: ${e.message}`); return; }
    this.panel.lastExpr[this.name] = txt;
    this.show(v, true);                       // show the requested value at once
    this.panel.requestSet(this.name, v);
  }

  show(v, force = false) {
    if (v === null || v === undefined) return;
    if (this.editor.tagName === "SELECT") {
      if (document.activeElement === this.editor) return;
      const i = this.choices.findIndex((c) => (this.numChoices ? Math.abs(Number(v) - c) <= 1e-9 * Math.abs(c || 1)
        : String(c).toLowerCase() === String(v).toLowerCase()));
      this.editor.value = String(i);
      return;
    }
    if (this.editor.type !== "number") {
      if (document.activeElement !== this.editor) this.editor.value = typeof v === "number" ? String(Number(v.toPrecision(10))) : String(v);
      return;
    }
    const f = Number(v);
    if (!Number.isFinite(f)) return;
    const focused = document.activeElement === this.editor;
    if ((force && !this.typing) || !focused) {
      this.setUnset(false);
      this.editor.value = String(Number(f.toPrecision(10)));
    }
    if (this.slider) this.slider.value = this._toSlider(f);
  }

  setState(state, tip = "") {
    this.label.classList.toggle("busy", state === "busy");
    this.label.classList.toggle("err", state === "error");
    this.label.classList.toggle("held", state === "held");
    this.label.title = this.label.title.split("\n⚠")[0] + (tip ? "\n⚠ " + tip : "");
  }
}

export class ManualPanel {
  constructor(app) {
    this.app = app;
    this.conn = app.conn;
    this.rows = new Map();
    this.sig = null;
    this.pending = {};
    this.queued = {};
    this.held = {};
    this.groups = new Map();
    this.lastExpr = {};
    this.filter = h("input", { placeholder: "filter channels", type: "search", on: { input: () => this._filter() } });
    this.ramp = h("input", { type: "checkbox", checked: true, title: "respect the alias ramp rates (recommended)" });
    this.settings = h("input", { type: "checkbox", checked: true, on: { change: () => this._state() },
      title: "also list the instrument settings (configuration channels: modes, ranges, time constants …)" });
    this.host = h("div", { class: "man-rows" });
    this.status = h("div", { class: "msg muted" });
    // lit only while a ramp of a direct write runs (from any window); no data-cmd: the
    // permission is applied here together with that
    this.stopBtn = h("button", { class: "stop-ramps", text: "■ Stop ramps", title: "no ramp is running", disabled: true,
      on: { click: () => this._stopRamps() } });
    // user actions as buttons (hidden while there are none); parameters below the channels
    this.actionsBar = h("div", { class: "man-actions", hidden: true });
    this.actionBtns = new Map();
    this.actSig = null;
    this.params = new Map();
    this.paramHost = h("div", { class: "man-params" });
    this.settingsLabel = h("label", { class: "check" }, this.settings, " settings");
    this.el = h("div", { class: "panel manual" }, bar(this.filter, this.settingsLabel,
      h("label", { class: "check" }, this.ramp, " ramp")), this.stopBtn, this.actionsBar, this.host, this.paramHost, this.status);
    app.model.on("state", (st) => this._state(st));
    app.model.on("ramps", (act) => this.showRamps(act));
  }

  /** Light "Stop ramps" while ramps of direct writes run. */
  showRamps(active) {
    const names = [...new Set((active || []).flatMap((r) => r.channels || []))].sort();
    const allowed = !this.conn.can || this.conn.can("stop_ramps");
    this.stopBtn.disabled = !names.length || !allowed;
    this.stopBtn.classList.toggle("active", names.length > 0);
    this.stopBtn.textContent = !names.length ? "■ Stop ramps" : names.length <= 3 ? `■ Stop ramps (${names.join(", ")})` : `■ Stop ramps (${names.length})`;
    this.stopBtn.title = !names.length ? "no ramp is running" : !allowed ? "not allowed for your role" : `stop the ramps of ${names.join(", ")} where they are now`;
  }

  async _stopRamps() {
    try {
      const names = await this.conn.request("stop_ramps");
      this.say(names && names.length ? `stopped: ${names.join(", ")}` : "no ramp was running");
    } catch (e) { this.say(`✘ stop ramps: ${e.message}`); }
  }

  say(t) { this.status.textContent = t; }

  _channels() {
    const out = [];
    for (const a of this.app.model.aliases()) if (a.writable && (a.dtype === "str" || !(a.shape && a.shape.length))) out.push(a);
    for (const v of this.app.model.virtualChannels()) if (v.writable) out.push(v);
    if (this.settings.checked && this.conn.canWriteRaw()) {
      for (const i of this.app.model.instruments()) {
        for (const c of i.channels || []) {
          if (c.config && (c.access || "rw").includes("w") && (c.dtype === "str" || !(c.shape && c.shape.length))) {
            out.push({ ...c, name: `${i.name}.${c.name}`, choices: c.allowed, kind: "config" });
          }
        }
      }
    }
    return out;
  }

  // ---- user actions ------------------------------------------------------------------
  _showActions() {
    const acts = this.app.model.userActions();
    const sig = JSON.stringify(acts.map((a) => [a.name, a.description || "", !!a.confirm]));
    if (sig !== this.actSig) {
      this.actSig = sig;
      this.actionsBar.replaceChildren(...(acts.length ? [h("b", { text: "Actions" })] : []));
      this.actionBtns.clear();
      for (const a of acts) {
        const b = h("button", { class: "man-action", on: { click: () => this._actionClicked(a.name) } });
        this.actionBtns.set(a.name, b);
        this.actionsBar.append(b);
      }
      this.actionsBar.hidden = !acts.length;
    }
    const canRun = this.conn.can("write_channel"), canAbort = this.conn.can("abort_user_action");
    for (const a of acts) {
      const b = this.actionBtns.get(a.name);
      if (!b) continue;
      const running = !!a.running;
      b.textContent = (running ? "■ " : "▶ ") + a.name;
      b.classList.toggle("running", running);
      b.disabled = running ? !canAbort : !canRun;
      const last = a.last || {};
      b.title = (running ? "running - click to abort\n" : "") + (a.description || a.name) +
        (!running && last.status === "error" ? `\nlast run failed: ${last.error}` : "");
    }
  }

  async _actionClicked(name) {
    const a = this.app.model.userActions().find((x) => x.name === name);
    if (!a) return;
    if (a.running) {
      try { await this.conn.request("abort_user_action", { name }); this.say(`aborting ${name}`); }
      catch (e) { this.say(`✘ abort ${name}: ${e.message}`); }
      return;
    }
    if (a.confirm && !(await confirmDialog("Run action", `Run ${name}?` + (a.description ? `\n\n${a.description}` : "")))) return;
    this.say(`running ${name} …`);
    this.app.model.scheduleRefresh();
    try {
      const r = await this.conn.request("write_channel", { name }, { timeout: 7 * 24 * 3600 * 1000 });
      this.say(r.status === "finished" ? `✔ ${name} done (${fmt(r.elapsed || 0, 3)} s)` : `✘ ${name} ${r.status}${r.error ? ": " + r.error : ""}`);
    } catch (e) { this.say(`✘ ${name}: ${e.message}`); }
    this.app.model.scheduleRefresh();
  }

  // ---- parameters ----------------------------------------------------------------------
  _showParams() {
    const ps = this.app.model.parameters();
    const sig = JSON.stringify(ps.map((p) => [p.name, p.unit || "", p.formula || "", p.description || ""]));
    if (sig !== this.paramSig) {
      this.paramSig = sig;
      this.params.clear();
      const rows = [];
      for (const p of ps) {
        const label = h("span", { class: "man-label", text: p.name + (p.unit ? ` (${p.unit})` : ""), title: p.description || p.name });
        let ed;
        if (p.formula) ed = h("span", { class: "muted man-pformula", title: `computed: ${p.name} = ${p.formula}` });
        else {
          ed = h("input", { class: "man-num man-pvalue", type: "text", "data-cmd": "write_channel",
            title: "type a value and press Enter (moves no instrument)" });
          ed.addEventListener("keydown", (ev) => { if (ev.key === "Enter") this.setParameter(p.name, ed.value); });
        }
        const row = h("div", { class: "man-row in-group man-param" }, label, ed);
        row.dataset.param = p.name;
        this.params.set(p.name, { row, ed, meta: p });
        rows.push(row);
      }
      this.paramHost.replaceChildren(...(ps.length ? [h("div", { class: "man-group", title: "constants of the setup, used by composite channels and actions; setting one moves no instrument" }, h("b", { text: "Parameters" }))] : []), ...rows);
      this.app.applyPermissions(this.paramHost);
      this._filter();
    }
    for (const p of ps) {
      const r = this.params.get(p.name);
      if (!r) continue;
      const text = p.current === null || p.current === undefined ? "" : fmt(p.current, 6);
      if (p.formula) r.ed.textContent = `${text}  = ${p.formula}`;
      else if (document.activeElement !== r.ed) r.ed.value = text;
    }
  }

  async setParameter(name, text) {
    const value = Number(String(text).trim());
    if (!String(text).trim() || !Number.isFinite(value)) { this.say(`✘ ${name}: "${text}" is not a number`); return; }
    try {
      const v = await this.conn.request("write_channel", { name, value });
      this.say(`${name} = ${fmt(v, 8)}`);
    } catch (e) { this.say(`✘ ${name}: ${e.message}`); }
    this.app.model.scheduleRefresh();
  }

  _state() {
    this.settingsLabel.hidden = !this.conn.canWriteRaw();   // remote operators: aliases only
    this._showActions();
    this._showParams();
    const chans = this._channels();
    const sig = JSON.stringify(chans.map((m) => [m.name, m.composite || "", m.dtype, m.min, m.max, m.display_unit || m.unit,
      m.choices || m.allowed || [], m.kind || ""]));
    if (sig !== this.sig) {
      this.sig = sig;
      this.host.replaceChildren();
      this.rows.clear();
      this.groups = new Map();
      const multi = new Map(this.app.model.compositeGroups().map((g) => [g.name, g.variables]));
      for (const m of chans) {
        const comp = m.kind === "variable" ? m.composite : null;
        if (comp && multi.has(comp) && !this.groups.has(comp)) this.host.append(this._groupHeader(comp, multi.get(comp)));
        const r = new ChannelRow(this, m);
        this.rows.set(m.name, r);
        if (comp && multi.has(comp)) r.el.classList.add("in-group");   // indented under its header
        this.host.append(r.el);
        const inGroup = comp && multi.has(comp);
        if ((m.last === null || m.last === undefined) && !inGroup) this.readBack(m.name);
      }
      this.app.applyPermissions(this.host);
      this._filter();
      this.say(chans.length || this.app.model.parameters().length ? "" : "no writable aliases, composites or instrument settings");
    }
    for (const m of chans) {
      const r = this.rows.get(m.name);
      if (r) r.backendSet = Number.isFinite(m.last);
      if (r && !this.pending[m.name]) r.show(m.last);
    }
    this._updateGroups();
  }

  /** Variables never set show NaN (slider greyed out); their group keeps hold on
   *  until Set all to zero, or Set all once every one has a value. */
  _updateGroups() {
    const canSet = this.conn.can("set_many");
    for (const [comp, g] of this.groups) {
      const held = this.held[comp] || {};
      const rows = g.variables.map((n) => [n, this.rows.get(n)]).filter(([, r]) => r);
      const unset = rows.filter(([, r]) => !r.backendSet).map(([n]) => n);
      for (const [n, r] of rows) if (!this.pending[n]) r.setUnset(!r.backendSet && !(n in held));
      if (unset.length) {
        if (!g.forced) {
          g.forced = true;
          g.hold.checked = true;
          g.hold.disabled = true;
          g.hold.title = `${unset.join(", ")} not set yet: hold stays on until all variables are set together (Set all, or Set all to zero)`;
        }
        const missing = unset.filter((n) => !(n in held));
        g.set.dataset.busy = missing.length ? "1" : "";
        g.set.disabled = !canSet || missing.length > 0;
        g.set.title = missing.length ? `type a value for ${missing.join(", ")} first (it shows NaN)` : `set ${g.variables.join(", ")} together to the values shown`;
        g.note.hidden = false;
      } else {
        if (g.forced) {
          g.forced = false;
          g.hold.checked = false;
          g.hold.disabled = false;
          g.hold.title = "collect edits of these variables and send them with Set all";
          delete this.held[comp];
        }
        g.set.dataset.busy = "";
        g.set.disabled = !canSet;
        g.set.title = `set every variable of ${comp} together to the values shown`;
        g.note.hidden = true;
      }
    }
  }

  _filter() {
    const t = this.filter.value.trim().toLowerCase();
    for (const [n, r] of this.rows) r.el.hidden = !!t && !n.toLowerCase().includes(t) && !(r.meta.composite || "").toLowerCase().includes(t);
    for (const [c, g] of this.groups || []) g.el.hidden = !!t && !c.toLowerCase().includes(t) && !g.variables.some((v) => v.toLowerCase().includes(t));
    let anyP = false;
    for (const [n, r] of this.params || []) { r.row.hidden = !!t && !n.toLowerCase().includes(t) && !"parameters".includes(t); anyP = anyP || !r.row.hidden; }
    const ph = this.paramHost && this.paramHost.querySelector(".man-group");
    if (ph) ph.hidden = !anyP;
  }

  /** Header of the variables of one multivariate composite: "hold" collects edits,
   *  "Set all" sends them together (set_many), as the first setting needs;
   *  "Set all to zero" sets every variable to 0 together. */
  _groupHeader(comp, variables) {
    const hold = h("input", { type: "checkbox", title: "collect edits of these variables and send them with Set all",
      on: { change: () => { if (!hold.checked) delete this.held[comp]; } } });
    const set = btn("Set all", () => this.setGroup(comp), { cmd: "set_many", title: `set every variable of ${comp} together to the values shown` });
    const zero = btn("Set all to zero", () => this.setGroup(comp, true), { cmd: "set_many", title: `set every variable of ${comp} to 0 together (limits and ramps apply)` });
    const note = h("span", { class: "man-unset", text: "not set yet", hidden: true,
      title: `${variables.join(", ")}: no value yet. Set all to zero, or type every value and Set all` });
    const el = h("div", { class: "man-group" }, h("b", { text: comp }), h("span", { class: "muted", text: `(${variables.join(", ")})` }),
      h("label", { class: "check" }, hold, " hold"), set, zero, note);
    el.dataset.composite = comp;
    this.groups.set(comp, { el, hold, set, zero, note, variables, forced: false });
    return el;
  }

  _groupOf(name) {
    const r = this.rows.get(name);
    const c = r && r.meta.kind === "variable" ? r.meta.composite : null;
    return c && this.groups && this.groups.has(c) ? c : null;
  }

  async setGroup(comp, zero = false) {
    const g = this.groups.get(comp);
    if (!g) return;
    const values = {};
    for (const n of g.variables) {
      const r = this.rows.get(n);
      if (!r) continue;
      const held = (this.held[comp] || {})[n];
      if (!zero && held === undefined && r.unset) { this.say(`✘ ${n} has no value yet: type one, or use Set all to zero`); return; }
      values[n] = zero ? 0 : held !== undefined ? held : Number(r.editor.value);
    }
    for (const n of Object.keys(values)) { this.pending[n] = true; this.rows.get(n).setState("busy"); }
    this.say(`setting ${Object.keys(values).join(", ")} together …`);
    let ok = true, err = "";
    try { await this.conn.request("set_many", { values, ramp: this.ramp.checked }, { timeout: 3600000 }); }
    catch (e) { ok = false; err = e.message; }
    if (ok) delete this.held[comp];           // on failure the held values stay
    for (const [n, v] of Object.entries(values)) {
      this.pending[n] = false;
      const r = this.rows.get(n);
      if (r) {
        r.setState(ok ? "ok" : (this.held[comp] || {})[n] !== undefined ? "held" : "error", err);
        if (ok) { r.backendSet = true; r.show(v, true); }
      }
    }
    this._updateGroups();
    this.say(ok ? Object.entries(values).map(([n, v]) => `${n} = ${fmt(v, 8)}`).join(", ") + " set" : `✘ ${comp}: ${err}`);
    this.app.model.scheduleRefresh();
  }

  /** One write in flight per channel; newer requests replace queued ones. */
  async requestSet(name, value) {
    const comp = this._groupOf(name);
    if (comp && this.groups.get(comp).hold.checked) {
      (this.held[comp] = this.held[comp] || {})[name] = value;
      this.rows.get(name).setState("held", "held: sent with Set all");
      this.say(`${name} = ${fmt(value)} held; Set all sends ${Object.keys(this.held[comp]).join(", ")}`);
      this._updateGroups();
      return;
    }
    if (this.pending[name]) { this.queued[name] = value; return; }
    this.pending[name] = true;
    const row = this.rows.get(name);
    if (row) row.setState("busy");
    this.say(`setting ${name} → ${fmt(value)} …`);
    let ok = true, result = null, err = "";
    try { result = await this.conn.request("write_channel", { name, value, ramp: this.ramp.checked }, { timeout: 3600000 }); }
    catch (e) { ok = false; err = e.message; }
    this.pending[name] = false;
    const r = this.rows.get(name);
    if (r) { r.setState(ok ? "ok" : "error", err); if (ok) r.show(result, true); }
    this.say(ok ? `${name} = ${fmt(result, 8)}` : `✘ ${name}: ${err}`);
    if (name in this.queued) { const nxt = this.queued[name]; delete this.queued[name]; this.requestSet(name, nxt); }
    else if (!ok) this.readBack(name);
    else this.app.model.scheduleRefresh();
  }

  /** Read-backs wait in line per instrument (a rebuild asks for every value at once,
   *  and an instrument serves one request at a time anyway). */
  readBack(name) {
    const r = this.rows.get(name);
    const target = (r && r.meta.target) || name;
    const key = target.includes(".") ? target.split(".")[0] : "@" + name;
    this.rbWait = this.rbWait || {};
    this.rbBusy = this.rbBusy || {};
    const q = (this.rbWait[key] = this.rbWait[key] || []);
    if (!q.includes(name)) q.push(name);
    this._nextReadBack(key);
  }

  async _nextReadBack(key) {
    if (this.rbBusy[key] || !this.rbWait[key].length) return;
    const name = this.rbWait[key].shift();
    this.rbBusy[key] = true;
    try {
      const v = await this.conn.request("read_channel", { name });
      const r = this.rows.get(name);
      if (r && !this.pending[name]) r.show(v);
    } catch (e) { this.say(`✘ read ${name}: ${e.message}`); }
    this.rbBusy[key] = false;
    this._nextReadBack(key);
  }
}

// ---------------------------------------------------------------------- log ----
const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40 };

export class LogPanel {
  constructor(app) {
    this.app = app;
    this.conn = app.conn;
    this.level = h("select", { on: { change: () => this.reload() } }, Object.keys(LEVELS).map((l) => h("option", { text: l, value: l, selected: l === "INFO" })));
    this.follow = h("input", { type: "checkbox", checked: true });
    this.box = h("div", { class: "logbox" });
    this.lines = [];
    this.el = h("div", { class: "panel log" },
      bar(h("b", { text: "Log" }), h("label", {}, "level ", this.level), h("label", { class: "check" }, this.follow, " follow"),
        btn("Clear", () => { this.box.replaceChildren(); this.lines = []; }), btn("Reload", () => this.reload(), { cmd: "recent_logs" })),
      this.box);
    this._flush = throttle(() => this._render(), 200);
    this.conn.on("connected", () => this.reload());
    this.conn.on("event", (k, d) => { if (k === "log" && d) this._add(d); });
  }

  _min() { return LEVELS[this.level.value] || 20; }

  /** A message of this window (the status line's), kept in the Log too. */
  addLocal(level, text) {
    const name = { error: "ERROR", warning: "WARNING", debug: "DEBUG" }[level] || "INFO";
    this._add({ levelno: LEVELS[name] || 20, level: name, logger: "web", msg: text, t: Date.now() / 1000 });
  }

  async reload() {
    try {
      const rows = await this.conn.request("recent_logs", { n: 1500, min_level: this._min() });
      this.box.replaceChildren();
      this.lines = [];
      for (const r of rows || []) this._add(r, false);
      this._render();
    } catch (e) { /* not allowed */ }
  }

  _add(e, render = true) {
    if ((e.levelno || 0) < this._min()) return;
    this.lines.push(e);
    if (render) this._flush();
  }

  _render() {
    const frag = document.createDocumentFragment();
    for (const e of this.lines.splice(0)) {
      const lvl = (e.level || "").toLowerCase();
      frag.append(h("div", { class: "logline " + lvl }, h("span", { class: "t", text: clock(e.t || e.time) }),
        " ", h("span", { class: "lv", text: (e.level || "").padEnd(7) }), " ", h("span", { class: "src", text: e.logger || e.name || "" }), " ", e.msg || e.message || ""));
    }
    this.box.append(frag);
    while (this.box.childElementCount > 3000) this.box.firstElementChild.remove();
    if (this.follow.checked) this.box.scrollTop = this.box.scrollHeight;
  }
}
