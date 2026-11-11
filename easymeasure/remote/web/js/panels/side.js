// Right-hand side: Monitor (live tiles pushed by the backend), Manual control
// (spin box + slider per writable alias) and the Log panel.

import { h, btn, bar, fmt, clock, promptDialog, throttle, autoColumns } from "../ui.js";
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
    this.list.replaceChildren(...this.app.model.readableChannels().concat(this.app.model.rawChannels()).map((n) => h("option", { value: n })));
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
    const v = Number(this.editor.value);
    if (!Number.isFinite(v)) return;
    if (this.slider) this.slider.value = this._toSlider(v);
    this.panel.requestSet(this.name, v);
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
    const cur = Number(this.editor.value);
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
    if ((force && !this.typing) || !focused) this.editor.value = String(Number(f.toPrecision(10)));
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
    this.el = h("div", { class: "panel manual" }, bar(this.filter, h("label", { class: "check" }, this.settings, " settings"), h("label", { class: "check" }, this.ramp, " ramp")), this.host, this.status);
    app.model.on("state", (st) => this._state(st));
  }

  say(t) { this.status.textContent = t; }

  _channels() {
    const out = [];
    for (const a of this.app.model.aliases()) if (a.writable && (a.dtype === "str" || !(a.shape && a.shape.length))) out.push(a);
    for (const v of this.app.model.virtualChannels()) if (v.writable) out.push(v);
    if (this.settings.checked) {
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

  _state() {
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
        this.host.append(r.el);
        if (m.last === null || m.last === undefined) this.readBack(m.name);
      }
      this.app.applyPermissions(this.host);
      this._filter();
      this.say(chans.length ? "" : "no writable aliases, composites or instrument settings");
    }
    for (const m of chans) {
      const r = this.rows.get(m.name);
      if (r && !this.pending[m.name]) r.show(m.last);
    }
  }

  _filter() {
    const t = this.filter.value.trim().toLowerCase();
    for (const [n, r] of this.rows) r.el.hidden = !!t && !n.toLowerCase().includes(t) && !(r.meta.composite || "").toLowerCase().includes(t);
    for (const [c, g] of this.groups || []) g.el.hidden = !!t && !c.toLowerCase().includes(t) && !g.variables.some((v) => v.toLowerCase().includes(t));
  }

  /** Header of the variables of one multivariate composite: "hold" collects edits,
   *  "Set all" sends them together (set_many), as the first setting needs. */
  _groupHeader(comp, variables) {
    const hold = h("input", { type: "checkbox", title: "collect edits of these variables and send them with Set all",
      on: { change: () => { if (!hold.checked) delete this.held[comp]; } } });
    const set = btn("Set all", () => this.setGroup(comp), { cmd: "set_many", title: `set every variable of ${comp} together to the values shown` });
    const el = h("div", { class: "man-group" }, h("b", { text: comp }), h("span", { class: "muted", text: `(${variables.join(", ")})` }),
      h("label", { class: "check" }, hold, " hold"), set);
    el.dataset.composite = comp;
    this.groups.set(comp, { el, hold, variables });
    return el;
  }

  _groupOf(name) {
    const r = this.rows.get(name);
    const c = r && r.meta.kind === "variable" ? r.meta.composite : null;
    return c && this.groups && this.groups.has(c) ? c : null;
  }

  async setGroup(comp) {
    const g = this.groups.get(comp);
    if (!g) return;
    const values = {};
    for (const n of g.variables) {
      const r = this.rows.get(n);
      if (!r) continue;
      const held = (this.held[comp] || {})[n];
      values[n] = held !== undefined ? held : Number(r.editor.value);
    }
    for (const n of Object.keys(values)) { this.pending[n] = true; this.rows.get(n).setState("busy"); }
    this.say(`setting ${Object.keys(values).join(", ")} together …`);
    let ok = true, err = "";
    try { await this.conn.request("set_many", { values, ramp: this.ramp.checked }, { timeout: 3600000 }); }
    catch (e) { ok = false; err = e.message; }
    delete this.held[comp];
    for (const [n, v] of Object.entries(values)) {
      this.pending[n] = false;
      const r = this.rows.get(n);
      if (r) { r.setState(ok ? "ok" : "error", err); if (ok) r.show(v, true); }
    }
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
