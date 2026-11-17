// Plugins in the web page: the Plugins menu, the dialog to manage them, and the
// panels and data tabs of the running plugins (port of frontend/plugins.py).
//
// A plugin describes its panels as data (easymeasure.plugins.ui); they are drawn
// here - seven-segment digits as SVG, LEDs, bar graphs and knobs in CSS - updated by
// "plugin_ui" events, and what the user does goes back as "plugin_event".

import { h, btn, modal, message, Table } from "./ui.js";
import { PlotCanvas } from "./plot.js";

const COLORS = { red: "#ff3b1f", green: "#38e05a", amber: "#ffb000", blue: "#3aa0ff", white: "#f2f2f2", yellow: "#ffe14a" };
const color = (c, d = "red") => COLORS[c || d] || c || COLORS[d];
const PENS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"];
const SVG = "http://www.w3.org/2000/svg";

const SEGMENTS = {
  "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd", "6": "afgedc",
  "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "", "_": "d", "A": "abcefg", "a": "abcdeg",
  "b": "cdefg", "C": "adef", "c": "deg", "d": "bcdeg", "E": "adefg", "e": "abdefg", "F": "aefg",
  "H": "bcefg", "h": "cefg", "L": "def", "n": "ceg", "o": "cdeg", "O": "abcdef", "P": "abefg",
  "r": "eg", "t": "defg", "U": "bcdef", "u": "cde", "y": "bcdfg", "°": "abfg",
};

/** "-1.234" -> [[char, dot]] right-aligned in *digits* cells */
export function segCells(text, digits) {
  const cells = [];
  for (const ch of String(text ?? "")) {
    if (ch === "." && cells.length && !cells[cells.length - 1][1]) cells[cells.length - 1][1] = true;
    else if (ch === ".") cells.push([" ", true]);
    else cells.push([ch, false]);
  }
  const tail = cells.slice(-digits);
  return Array.from({ length: digits - tail.length }, () => [" ", false]).concat(tail);
}

function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/** Seven-segment digits (SVG): lit segments bright, the others faint. */
class Seg7 {
  constructor(p) {
    this.el = h("span", { class: "pl-seg7" });
    this.props = { value: "", digits: 6, color: "red", size: 32 };
    this.set(p);
  }

  set(p) {
    const rebuild = ("digits" in p && p.digits !== this.props.digits) || ("size" in p && p.size !== this.props.size) || !this.svg;
    Object.assign(this.props, p);
    if (rebuild) this._build();
    this._paint();
  }

  _build() {
    const H = 100, cw = 70, n = this.props.digits, W = n * cw + 30;
    const t = 10, w = 46, half = H / 2, sk = 8;
    this.svg = svg("svg", { viewBox: `0 0 ${W} ${H * 1.2}`, height: this.props.size * 1.2, width: this.props.size * 1.2 * W / (H * 1.2) });
    this.svg.append(svg("rect", { x: 0, y: 0, width: W, height: H * 1.2, fill: "#0b0b0b" }));
    const hseg = (x, y) => [[x + t / 2, y], [x + t, y - t / 2], [x + w - t, y - t / 2], [x + w - t / 2, y], [x + w - t, y + t / 2], [x + t, y + t / 2]];
    const vseg = (x, y, L) => [[x, y + t / 2], [x + t / 2, y + t], [x + t / 2, y + L - t], [x, y + L - t / 2], [x - t / 2, y + L - t], [x - t / 2, y + t]];
    this.cells = [];
    const x00 = W - n * cw - 5, y0 = 10;
    for (let i = 0; i < n; i++) {
      const x = x00 + i * cw, y = y0;
      const shapes = { a: hseg(x, y), g: hseg(x, y + half), d: hseg(x, y + H), f: vseg(x, y, half), b: vseg(x + w, y, half), e: vseg(x, y + half, half), c: vseg(x + w, y + half, half) };
      const segs = {};
      for (const [s, pts] of Object.entries(shapes)) {
        const slanted = pts.map(([px, py]) => `${px + sk * (1 - (py - y) / H)},${py}`).join(" ");
        segs[s] = svg("polygon", { points: slanted });
        this.svg.append(segs[s]);
      }
      const dot = svg("circle", { cx: x + w + t * 1.1, cy: y + H - 3, r: t * 0.65 });
      this.svg.append(dot);
      this.cells.push({ segs, dot });
    }
    this.el.replaceChildren(this.svg);
  }

  _paint() {
    const on = color(this.props.color), cells = segCells(this.props.value, this.props.digits);
    this.svg.dataset.value = this.props.value ?? "";
    cells.forEach(([ch, dot], i) => {
      const lit = SEGMENTS[ch] ?? SEGMENTS[String(ch).toUpperCase()] ?? "g";
      const c = this.cells[i];
      for (const [s, el] of Object.entries(c.segs)) { el.setAttribute("fill", on); el.setAttribute("fill-opacity", lit.includes(s) ? "1" : "0.07"); }
      c.dot.setAttribute("fill", on);
      c.dot.setAttribute("fill-opacity", dot ? "1" : "0.07");
    });
  }
}

function ledEl(on, c) {
  const el = h("span", { class: "pl-led" + (on ? " on" : "") });
  el.style.setProperty("--led", color(c));
  return el;
}

// ------------------------------------------------------------------------------
/** One panel (or data tab) of a plugin, drawn from its description. */
export class PanelView {
  constructor(plugin, panel, send) {
    this.plugin = plugin;
    this.id = panel.id;
    this.signature = structure(panel.root);
    this.send = send;
    this.apply = {};
    const root = this._build(panel.root);
    this.el = h("div", { class: "pl-panel" + (hasPlot(panel.root) ? " grows" : ""), dataset: { plugin, panel: panel.id } }, root);
  }

  set(id, props) { const f = this.apply[id]; if (f) f(props); }

  _event(id, event, value) { this.send(this.plugin, id, event, value); }

  _build(w) {
    const p = { ...(w.props || {}) };
    const make = this["_" + w.type];
    if (!make) return h("span", { text: `(${w.type})` });
    const [el, apply] = make.call(this, w, p);
    if (w.id) {
      el.dataset.wid = w.id;
      this.apply[w.id] = (props) => { if ("visible" in props) el.hidden = props.visible === false; apply(props); };
    }
    if (p.visible === false) el.hidden = true;
    return el;
  }

  _box(w, p, cls) {
    const el = h("div", { class: cls });
    el.style.gap = `${p.gap ?? 6}px`;
    if (p.align) el.dataset.align = p.align;
    for (const c of w.children || []) {
      if (c.type === "spacer") {
        const size = (c.props || {}).size || 0;
        el.append(h("span", { class: "pl-spacer", style: size ? { flex: `0 0 ${size}px` } : { flex: "1 1 auto" } }));
        continue;
      }
      const ce = this._build(c);
      if (hasPlot(c)) ce.classList.add("grows");
      el.append(ce);
    }
    return el;
  }

  _row(w, p) { return [this._box(w, p, "pl-row"), () => {}]; }
  _column(w, p) { return [this._box(w, p, "pl-col"), () => {}]; }
  _group(w, p) {
    const title = h("div", { class: "pl-gtitle", text: p.title || "" });
    const body = this._box(w, p, "pl-col");
    const el = h("div", { class: "pl-group" + (p.dark ? " dark" : "") }, title, body);
    return [el, (q) => { if ("title" in q) title.textContent = q.title || ""; }];
  }

  _label(w, p) {
    const el = h("span", { class: "pl-label" });
    const apply = (q) => {
      if ("text" in q) el.textContent = q.text ?? "";
      if (q.size) el.dataset.size = q.size;
      if ("bold" in q) el.style.fontWeight = q.bold ? "bold" : "";
      if (q.color) el.style.color = color(q.color);
    };
    apply(p);
    return [el, apply];
  }

  _seg7(w, p) {
    const seg = new Seg7(p);
    const unit = h("span", { class: "pl-unit", text: p.unit || "" });
    const cap = h("div", { class: "pl-caption", text: p.label || "", hidden: !p.label });
    const el = h("div", { class: "pl-seg7box" }, h("div", { class: "pl-row", style: { gap: "6px" }, dataset: { align: "center" } }, seg.el, unit), cap);
    return [el, (q) => {
      seg.set(q);
      if ("unit" in q) unit.textContent = q.unit || "";
      if ("label" in q) { cap.textContent = q.label || ""; cap.hidden = !q.label; }
    }];
  }

  _led(w, p) {
    let led = ledEl(p.on, p.color);
    const lab = h("span", { class: "pl-ledtext", text: p.label || "" });
    const el = h("span", { class: "pl-ledrow" }, led, lab);
    const st = { on: !!p.on, color: p.color };
    return [el, (q) => {
      Object.assign(st, q);
      const n = ledEl(st.on, st.color);
      led.replaceWith(n);
      led = n;
      if ("label" in q) lab.textContent = q.label || "";
    }];
  }

  _ledlist(w, p) {
    const el = h("div", { class: "pl-ledlist" });
    const st = { options: [], orient: "vertical", color: "red", selected: null, label: null };
    const draw = () => {
      el.dataset.orient = st.orient;
      const lit = new Set(Array.isArray(st.selected) ? st.selected : st.selected === null || st.selected === undefined ? [] : [st.selected]);
      const items = st.options.map((o, i) => h("span", { class: "pl-ledrow", dataset: { lit: lit.has(i) ? "1" : "0" } }, ledEl(lit.has(i), st.color), h("span", { class: "pl-ledtext", text: o })));
      el.replaceChildren(...(st.label ? [h("div", { class: "pl-caption", text: st.label })] : []), h("div", { class: "pl-leds" }, ...items));
    };
    const apply = (q) => { Object.assign(st, q); draw(); };
    apply(p);
    return [el, apply];
  }

  _bar(w, p) {
    const fill = h("span", { class: "pl-barfill" });
    const el = h("div", { class: "pl-bar" }, fill);
    const st = { value: 0, min: 0, max: 1, color: "red" };
    const apply = (q) => {
      Object.assign(st, q);
      const span = (st.max - st.min) || 1;
      const zero = st.min < 0 && st.max > 0 ? 0 : st.min;
      const a = (zero - st.min) / span, b = (Math.min(Math.max(st.value || 0, st.min), st.max) - st.min) / span;
      fill.style.left = `${Math.min(a, b) * 100}%`;
      fill.style.width = `${Math.abs(b - a) * 100}%`;
      el.style.setProperty("--led", color(st.color));
    };
    apply(p);
    return [el, apply];
  }

  _plot(w, p) {
    const pc = new PlotCanvas({});
    const legend = h("div", { class: "pl-legend" });
    const title = h("div", { class: "pl-ptitle", text: p.title || "" });
    const el = h("div", { class: "pl-plot" }, title, legend, pc.el);
    const apply = (q) => {
      if ("title" in q) title.textContent = q.title || "";
      if ("xlabel" in q) pc.xlabel = q.xlabel || "";
      if ("ylabel" in q) pc.ylabel = q.ylabel || "";
      if ("series" in q) {
        const series = (q.series || []).map((s, i) => ({ name: s.name, color: PENS[i % PENS.length], width: 1.6,
          x: (s.x || []).map((v) => (v === null ? NaN : v)), y: (s.y || []).map((v) => (v === null ? NaN : v)) }));
        legend.replaceChildren(...series.map((s) => h("span", { class: "pl-key" }, h("span", { class: "pl-swatch", style: { background: s.color } }), s.name)));
        pc.setContent({ kind: "curves", series });
      }
    };
    apply(p);
    return [el, apply];
  }

  _button(w, p) {
    const cap = h("div", { class: "pl-caption", text: p.label || "", hidden: !p.label });
    const b = h("button", { class: "pl-key", text: p.text || "", on: { click: () => this._event(w.id, "click") } });
    b.dataset.cmd = "plugin_event";
    const el = h("div", { class: "pl-button" }, cap, b);
    const apply = (q) => {
      if ("text" in q) b.textContent = q.text || "";
      if ("label" in q) { cap.textContent = q.label || ""; cap.hidden = !q.label; }
      if ("enabled" in q) { b.dataset.off = q.enabled === false ? "1" : ""; b.disabled = q.enabled === false || b.classList.contains("denied"); }
      if (q.color) b.style.color = color(q.color);
    };
    apply(p);
    return [el, apply];
  }

  _knob(w, p) {
    const mark = h("span", { class: "pl-knobmark" });
    const k = h("div", { class: "pl-knob", tabindex: 0, title: "turn: mouse wheel, drag up / down, or the arrow keys" }, mark);
    k.dataset.cmd = "plugin_event";
    let angle = 0, drag = null;
    const turn = (steps) => {
      if (!steps || k.dataset.off === "1" || k.classList.contains("denied")) return;
      angle = (angle + 15 * steps) % 360;
      mark.style.transform = `rotate(${angle}deg)`;
      this._event(w.id, "turn", steps);
    };
    k.addEventListener("wheel", (ev) => { ev.preventDefault(); turn(ev.deltaY < 0 ? 1 : -1); }, { passive: false });
    k.addEventListener("pointerdown", (ev) => { drag = ev.clientY; k.setPointerCapture(ev.pointerId); });
    k.addEventListener("pointermove", (ev) => {
      if (drag === null) return;
      const d = drag - ev.clientY;
      if (Math.abs(d) >= 8) { turn(Math.trunc(d / 8)); drag = ev.clientY; }
    });
    k.addEventListener("pointerup", () => { drag = null; });
    k.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowUp" || ev.key === "ArrowRight") { turn(1); ev.preventDefault(); }
      if (ev.key === "ArrowDown" || ev.key === "ArrowLeft") { turn(-1); ev.preventDefault(); }
    });
    const cap = h("div", { class: "pl-caption", text: p.label || "" });
    const el = h("div", { class: "pl-knobbox" }, k, cap);
    return [el, (q) => {
      if ("label" in q) cap.textContent = q.label || "";
      if ("enabled" in q) k.dataset.off = q.enabled === false ? "1" : "";
    }];
  }

  _select(w, p) {
    const cap = h("span", { class: "pl-label", text: p.label || "" });
    const sel = h("select", { on: { change: () => this._event(w.id, "change", sel.value) } });
    sel.dataset.cmd = "plugin_event";
    const st = { options: [] };
    const apply = (q) => {
      if ("options" in q) {
        st.options = q.options || [];
        const cur = sel.value;
        sel.replaceChildren(...st.options.map((o) => h("option", { value: o, text: o })));
        sel.value = cur;
      }
      if ("value" in q) sel.value = q.value ?? "";
      if ("label" in q) cap.textContent = q.label || "";
      if ("enabled" in q) sel.disabled = q.enabled === false;
    };
    apply(p);
    return [h("span", { class: "pl-row", dataset: { align: "center" } }, cap, sel), apply];
  }

  _number(w, p) {
    const cap = h("span", { class: "pl-label", text: p.label || "" });
    const inp = h("input", { type: "number", on: { change: () => this._event(w.id, "change", Number(inp.value)) } });
    inp.dataset.cmd = "plugin_event";
    const unit = h("span", { class: "pl-label", text: p.unit || "" });
    const apply = (q) => {
      if (q.value !== undefined && q.value !== null && document.activeElement !== inp) inp.value = q.value;
      if ("min" in q && q.min !== null) inp.min = q.min;
      if ("max" in q && q.max !== null) inp.max = q.max;
      if ("step" in q) inp.step = q.step;
      if ("label" in q) cap.textContent = q.label || "";
      if ("unit" in q) unit.textContent = q.unit || "";
      if ("enabled" in q) inp.disabled = q.enabled === false;
    };
    apply(p);
    return [h("span", { class: "pl-row", dataset: { align: "center" } }, cap, inp, unit), apply];
  }
}

export function structure(w) {
  const s = (x) => [x.type, x.id ?? null, (x.children || []).map(s)];
  return JSON.stringify(s(w));
}

function hasPlot(w) { return w.type === "plot" || (w.children || []).some(hasPlot); }

function* widgets(w) { yield w; for (const c of w.children || []) yield* widgets(c); }

// ------------------------------------------------------------------------------
/** The panels and data tabs of the running plugins, and the Plugins menu items. */
export class PluginHost {
  constructor(app) {
    this.app = app;
    this.conn = app.conn;
    this.views = new Map();          // "plugin/panel" -> {view, item, tabs}
    this.plugins = [];
    this.menuItems = [];             // shown by the Plugins menu (filled here)
    this._menu();
    app.model.on("state", (st) => this.update(((st && st.plugins) || {}).plugins || []));
    this.conn.on("event", (kind, data) => {
      if (kind === "plugins" && data) this.update(data.plugins || []);
      else if (kind === "plugin_ui" && data) {
        for (const { view } of this.views.values()) {
          if (view.plugin !== data.plugin) continue;
          for (const [id, props] of Object.entries(data.set || {})) view.set(id, props);
        }
      }
    });
  }

  _menu() {
    this.menuItems.length = 0;
    this.menuItems.push(["Manage plugins…", () => this.manage(), null, "list_plugins"]);
    for (const p of this.plugins) {
      for (const panel of p.panels || []) {
        this.menuItems.push([`${p.title} – ${panel.title}`, () => this.show(`${p.name}/${panel.id}`)]);
      }
    }
  }

  show(key) {
    const v = this.views.get(key);
    if (v) v.tabs.show(v.item);
  }

  update(plugins) {
    this.plugins = plugins || [];
    const want = new Map();
    for (const p of this.plugins) {
      if (p.state !== "running" && p.state !== "starting") continue;
      for (const panel of p.panels || []) want.set(`${p.name}/${panel.id}`, [p, panel]);
    }
    for (const [key, v] of [...this.views]) {
      const w = want.get(key);
      if (!w || v.view.signature !== structure(w[1].root)) { v.tabs.remove(v.item); this.views.delete(key); }
    }
    for (const [key, [p, panel]] of want) {
      const v = this.views.get(key);
      if (!v) {
        const view = new PanelView(p.name, panel, (plugin, widget, event, value) => this._send(plugin, widget, event, value));
        const tabs = panel.where === "data" ? this.app.dataTabs : this.app.leftTabs;
        const item = tabs.add(panel.title, view.el, { closable: false, title: `${panel.title} (plugin ${p.title})` });
        this.views.set(key, { view, item, tabs });
        this.app.applyPermissions(view.el);
      } else {
        for (const x of widgets(panel.root)) if (x.id) v.view.set(x.id, x.props || {});
      }
    }
    this._menu();
  }

  async _send(plugin, widget, event, value) {
    const args = { plugin, widget, event };
    if (value !== undefined) args.value = value;
    try { await this.conn.request("plugin_event", args); } catch (e) { message("error", `${plugin}: ${e.message}`); }
  }

  async manage() {
    let data = { plugins: this.plugins, errors: {}, folders: [] };
    const msg = h("div", { class: "muted small" });
    const about = h("div", { class: "pl-about" });
    const opts = h("textarea", { rows: 5, spellcheck: false, class: "mono" });
    const err = h("pre", { class: "pl-error" });
    const folders = h("div", { class: "muted small" });
    let current = null;
    const table = new Table([
      { key: "enabled", label: "Enabled", fmt: (_, p) => {
        const box = h("input", { type: "checkbox", checked: p.state === "running" || p.state === "starting", title: `enable / disable ${p.title}`,
          on: { click: (ev) => ev.stopPropagation(), change: (ev) => toggle(p.name, ev.target.checked) } });
        box.dataset.cmd = "enable_plugin";
        box.dataset.plugin = p.name;
        return box;
      } },
      { key: "title", label: "Plugin" },
      { key: "state", label: "State", fmt: (_, p) => h("span", { class: p.state === "error" ? "bad" : "", text: p.state + (p.errors ? ` (${p.errors} errors)` : "") }) },
      { key: "version", label: "Version" },
      { key: "source", label: "From", fmt: (_, p) => (p.builtin ? "built in" : p.source) },
    ], { onSelect: (rows) => { current = rows[0] || null; details(); } });
    table.keyOf = (p) => p.name;
    const details = () => {
      const p = current;
      if (!p) { about.textContent = "No plugins found."; opts.value = ""; err.textContent = Object.entries(data.errors || {}).map(([f, e]) => `${f}:\n${e}`).join("\n\n"); return; }
      about.replaceChildren(h("b", { text: p.title }), ` (${p.name}) ${p.version || ""} · plugin API ${p.api || "?"}`, h("br"), p.description || "",
        p.instrument ? h("span", { class: "muted", text: ` · channels: instrument ${p.instrument}` }) : "");
      opts.value = JSON.stringify(p.options || {}, null, 1);
      opts.disabled = !Object.keys(p.defaults || {}).length || !this.conn.can("set_plugin_options");
      err.textContent = (p.recent_errors || []).join("\n") || p.last_error || "";
    };
    const fill = (d) => {
      data = d || data;
      table.set(data.plugins || []);
      const keep = current && (data.plugins || []).find((p) => p.name === current.name);
      current = keep || (data.plugins || [])[0] || null;
      if (current) table.select(current.name);
      details();
      const errs = Object.keys(data.errors || {});
      folders.textContent = `Plugin API ${data.api || "?"}. Plugins are looked for in installed packages and in: ${(data.folders || []).join(", ")}` + (errs.length ? ` · not loaded: ${errs.join(", ")}` : "");
      this.app.applyPermissions(body);
    };
    const toggle = async (name, on) => {
      msg.textContent = `${on ? "starting" : "stopping"} ${name}…`;
      try { await this.conn.request(on ? "enable_plugin" : "disable_plugin", { name }); msg.textContent = ""; } catch (e) { msg.textContent = "✘ " + e.message; }
      fill(await this.conn.request("list_plugins"));
    };
    const setOpts = async () => {
      if (!current) return;
      let o;
      try { o = JSON.parse(opts.value || "{}"); } catch (e) { msg.textContent = "✘ options: " + e.message; return; }
      try { await this.conn.request("set_plugin_options", { name: current.name, options: o }); msg.textContent = `options of ${current.name} saved`; } catch (e) { msg.textContent = "✘ " + e.message; }
      fill(await this.conn.request("list_plugins"));
    };
    const rescan = async () => { try { fill(await this.conn.request("rescan_plugins")); } catch (e) { msg.textContent = "✘ " + e.message; } };
    const body = h("div", { class: "pl-manage" },
      h("p", { class: "muted", text: "Plugins add panels, data tabs, channels and actions. Each runs on its own thread; disabling one removes all of it at once." }),
      table.el, about,
      h("div", { class: "bar" }, h("span", { text: "Options (JSON)" }), h("span", { class: "grow" }), btn("Apply options", setOpts, { cmd: "set_plugin_options" })),
      opts, err, folders,
      h("div", { class: "bar" }, btn("Look for plugins again", rescan, { cmd: "rescan_plugins" })), msg);
    const off = (kind, d) => { if (kind === "plugins" && d) fill(d); };
    this.conn.on("event", off);
    fill(data);
    try { fill(await this.conn.request("list_plugins")); } catch (e) { msg.textContent = "✘ " + e.message; }
    await modal("Plugins", body, [["Close", null]], { wide: true });
    this.conn.off("event", off);
  }
}
