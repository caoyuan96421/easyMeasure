// Instruments & channels: instruments, raw channels and actions, aliases,
// composites, manual get/set, setup save/load (port of panels/setup_panel.py).

import { h, Table, btn, bar, group, fmt, formDialog, confirmDialog, download, pickFile, message, modal } from "../ui.js";
import { toJSONable } from "../codec.js";

const STATE_CLS = { connected: "ok", error: "err", hung: "hung", disconnected: "muted" };
const optNum = (s) => { s = String(s ?? "").trim(); if (!s) return null; const v = Number(s); if (!Number.isFinite(v)) throw new Error(`invalid number '${s}'`); return v; };
const optStr = (v) => (v === null || v === undefined ? "" : String(v));

function parseValue(text) {
  const t = text.trim();
  if (t === "") throw new Error("enter a value");
  if (/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(t)) return Number(t);
  if (t.startsWith("[")) return JSON.parse(t);
  return t;
}

const ACTION_HELP = "get(name) · set(name, value[, ramp=False]) · set_many({name: value, …}) · set_raw('inst.ch', v) · " +
  "sleep(s) · wait_until(f, timeout) · action('inst.action') · user_action('other action') · print(…) · " +
  "state (a dict kept between runs) · np, math. Limits and ramp rates apply; Abort stops at the next line, sleep or ramp step.";

function uactStatus(a) {
  if (a.running) return "running…";
  const l = a.last;
  if (!l) return "";
  const mark = { finished: "✔", aborted: "■", error: "✘" }[l.status] || "";
  return `${mark} ${new Date((l.ended || 0) * 1000).toLocaleTimeString()} (${fmt(l.elapsed || 0, 3)} s)`;
}

const COMP_TYPES = { multi: "Multivariate write", read: "Composite read", map: "Simple mapping" };
const FORMULA_HELP = "Formulas: + - * / **, abs, sqrt, exp, log, sin …, min/max, a if cond else b, " +
  "piecewise(cond1, v1, cond2, v2, …, default), interp(x, xs, ys)";

function compSummary(c) {
  if ((c.type || "multi") === "multi") return Object.entries(c.targets || {}).map(([k, v]) => `${k} = ${v}`).join("; ");
  if (c.type === "read") return c.expression || "";
  return `${c.target} = ${c.forward}  (x = ${c.backward})`;
}
function compChannels(c) {
  return (c.type || "multi") === "multi" ? (c.variables || []).map((v) => v.name) : [c.name];
}
function compLast(v) {
  if (v && typeof v === "object" && !Array.isArray(v) && !v.shape) {
    return Object.entries(v).map(([k, x]) => `${k} = ${x === null || x === undefined ? "—" : lastText(x)}`).join(", ");
  }
  return lastText(v);
}

/** The composite channel editor: type, then the fields of that type; "Check" asks
 *  the backend (check_composite) without defining anything. Resolves with the
 *  definition or null. */
export function compositeDialog(model, conn, c, onSave = null) {
  const own = new Set(c.name ? compChannels(c) : []);
  const writable = model.writableChannels(true).filter((n) => !own.has(n));
  const readable = model.readableChannels().filter((n) => !own.has(n));
  const kind = c.type || "multi";
  const inp = (value, placeholder = "", cls = "") => {
    const e = h("input", { type: "text", placeholder, autocomplete: "off", spellcheck: false, class: cls });
    e.value = value ?? "";
    return e;
  };
  const num = (s) => { const t = String(s).trim(); if (!t) return null; const v = Number(t); if (!Number.isFinite(v)) throw new Error(`invalid number: ${t}`); return v; };
  const name = inp(c.name, "", "comp-name");
  const desc = inp(c.description);
  const type = h("select", { class: "comp-type" }, Object.entries(COMP_TYPES).map(([k, l]) => h("option", { value: k, text: l, selected: k === kind })));

  // multivariate write
  const vbody = h("tbody");
  const addVar = (v = {}) => {
    const cells = ["name", "min", "max", "unit", "ramp_rate", "description"].map((k) =>
      inp(typeof v[k] === "number" ? String(v[k]) : v[k] || "", k === "ramp_rate" ? "/s" : ""));
    const del = h("button", { text: "✕", title: "remove this variable", on: { click: () => tr.remove() } });
    const tr = h("tr", { class: "comp-var" }, cells.map((e) => h("td", {}, e)), h("td", {}, del));
    tr._cells = cells;
    vbody.append(tr);
  };
  for (const v of kind === "multi" ? c.variables || [] : []) addVar(v);
  if (!vbody.children.length) addVar({ name: "x" });
  const targets = h("textarea", { rows: 5, spellcheck: false, class: "comp-targets",
    placeholder: "one per line:  channel = formula of the variables\ne.g.\nVL = U + eps\nVR = U - eps" });
  targets.value = Object.entries(kind === "multi" ? c.targets || {} : {}).map(([k, e]) => `${k} = ${e}`).join("\n");
  const multi = h("div", { class: "form" },
    h("label", { text: "Variables" }), h("div", {},
      h("table", { class: "comp-vars" }, h("thead", {}, h("tr", {}, ["Name", "Min", "Max", "Unit", "Ramp", "Description", ""].map((t) => h("th", { text: t })))), vbody),
      h("button", { text: "Add variable", class: "comp-add-var", on: { click: () => addVar({}) } })),
    h("label", { text: "Targets" }), targets,
    h("span"), h("div", { class: "help", text: "channels that can be driven: " + writable.join(", ") }));

  // composite read
  const expr = inp(kind === "read" ? c.expression : "", "e.g. (VL - VR)/2", "comp-expr");
  const rUnit = inp(kind === "read" ? c.unit : "");
  const read = h("div", { class: "form" }, h("label", { text: "Formula" }), expr, h("label", { text: "Unit" }), rUnit,
    h("span"), h("div", { class: "help", text: "may use: " + readable.join(", ") }));

  // mapping
  const m = kind === "map" ? c : {};
  const listId = "dl-" + Math.random().toString(36).slice(2);
  const mTarget = inp(m.target, "", "comp-target");
  mTarget.setAttribute("list", listId);
  const f = {
    forward: inp(m.forward, "target y from x, e.g.  x + 273.15", "comp-forward"),
    backward: inp(m.backward, "x from the target y, e.g.  y - 273.15", "comp-backward"),
    min: inp(m.min ?? "", "", "comp-min"), max: inp(m.max ?? "", "", "comp-max"), unit: inp(m.unit),
    ramp_rate: inp(m.ramp_rate ?? "", "units of x per s, optional"),
    tolerance: inp(m.tolerance ?? "", "default 1e-6 × range"),
  };
  const map = h("div", { class: "form" }, h("datalist", { id: listId }, writable.map((n) => h("option", { value: n }))),
    h("label", { text: "Target channel" }), mTarget, h("label", { text: "Forward  y = f(x)" }), f.forward,
    h("label", { text: "Backward  x = g(y)" }), f.backward, h("label", { text: "Minimum of x" }), f.min,
    h("label", { text: "Maximum of x" }), f.max, h("label", { text: "Unit" }), f.unit,
    h("label", { text: "Ramp rate" }), f.ramp_rate, h("label", { text: "Inverse tolerance" }), f.tolerance);

  const pages = { multi, read, map };
  const show = () => { for (const [k, p] of Object.entries(pages)) p.hidden = k !== type.value; };
  type.addEventListener("change", show);
  show();
  const info = h("div", { class: "help comp-info", text: FORMULA_HELP });

  const value = () => {
    const d = { type: type.value, name: name.value.trim(), description: desc.value };
    if (d.type === "multi") {
      d.variables = [...vbody.children].map((tr) => tr._cells.map((e) => e.value.trim()))
        .filter((x) => x[0]).map(([n, lo, hi, unit, rate, dsc]) => ({ name: n, min: num(lo), max: num(hi), unit, ramp_rate: num(rate), description: dsc }));
      d.targets = {};
      for (let line of targets.value.split("\n")) {
        line = line.trim();
        if (!line || line.startsWith("#")) continue;
        const i = line.indexOf("=");
        if (i < 0) throw new Error(`line '${line}' needs 'channel = formula'`);
        d.targets[line.slice(0, i).trim()] = line.slice(i + 1).trim();
      }
    } else if (d.type === "read") {
      Object.assign(d, { expression: expr.value.trim(), unit: rUnit.value.trim() });
    } else {
      Object.assign(d, { target: mTarget.value.trim(), forward: f.forward.value.trim(), backward: f.backward.value.trim(),
        min: num(f.min.value), max: num(f.max.value), unit: f.unit.value.trim(), ramp_rate: num(f.ramp_rate.value),
        tolerance: num(f.tolerance.value) });
    }
    return d;
  };
  const check = async () => {
    let d;
    try { d = value(); } catch (e) { info.className = "help comp-info err"; info.textContent = "✘ " + e.message; return; }
    info.className = "help comp-info";
    info.textContent = "checking …";
    try {
      const r = await conn.request("check_composite", { ...d, replace: !!c.name && c.name === d.name });
      const w = r.warnings || [];
      info.className = "help comp-info " + (w.length ? "warn" : "ok");
      info.textContent = `✔ defines ${(r.channels || []).join(", ")}` + w.map((x) => "\n⚠ " + x).join("");
    } catch (e) { info.className = "help comp-info err"; info.textContent = "✘ " + e.message; }
  };
  const checkBtn = h("button", { text: "Check", class: "comp-check", title: "check with the backend (references, dependency loops, the inverse of a mapping) without saving",
    on: { click: check } });
  if (!conn.can("check_composite")) checkBtn.disabled = true;
  const body = h("div", { class: "comp-dialog" },
    h("div", { class: "form" }, h("label", { text: "Name" }), name, h("label", { text: "Type" }), type),
    multi, read, map,
    h("div", { class: "form" }, h("label", { text: "Description" }), desc),
    h("div", { class: "bar" }, checkBtn), info);
  return modal(c.name ? `Composite ${c.name}` : "Add composite channel", body, [["Cancel", null], ["OK", true]],
    { wide: true, onOk: async () => { const d = value(); if (onSave) await onSave(d); return d; } });
}

export class SetupPanel {
  constructor(app) {
    this.app = app;
    this.conn = app.conn;
    this.model = app.model;
    this.drivers = {};
    this.msg = h("span", { class: "msg" });
    const say = (t, err = false) => { this.msg.textContent = (err ? "✘ " : "") + t; this.msg.className = "msg " + (err ? "err" : ""); };
    this.say = say;

    this.inst = new Table([
      { key: "name", label: "Name" }, { key: "driver", label: "Driver" }, { key: "address", label: "Address" },
      { key: "state", label: "State", cls: (r) => STATE_CLS[r.state] || "" },
      { key: "calls", label: "Calls", fmt: (_, r) => (r.stats || {}).calls ?? 0 },
      { key: "errors", label: "Errors", fmt: (_, r) => (r.stats || {}).errors ?? 0 },
      { key: "timeouts", label: "Timeouts", fmt: (_, r) => (r.stats || {}).timeouts ?? 0 },
      { key: "last_error", label: "Last error" },
    ], { onSelect: () => this._showRaw(), onActivate: () => this._editInst(), empty: "no instruments" });
    this.raw = new Table([
      { key: "name", label: "Channel" }, { key: "config", label: "Config", fmt: (v) => (v ? "⚙" : ""), title: "config channels are settings: set directly, never aliased or swept" },
      { key: "access", label: "Access" }, { key: "dtype", label: "Type" },
      { key: "shape", label: "Shape", fmt: (s) => (s === null || s === undefined ? "any" : s.length === 0 ? "scalar" : s.map((x) => (x === -1 ? "N" : x)).join("×")) },
      { key: "unit", label: "Unit" },
      { key: "last", label: "Last value", fmt: (v) => lastText(v) },
      { key: "allowed", label: "Allowed values", fmt: (v) => (v && v.length ? v.join(" | ") : "") },
      { key: "description", label: "Description" },
    ], { empty: "select an instrument" });
    this.raw.keyOf = (r) => r.name;
    this.lastLocal = new Map();          // "inst.ch" -> [value, unix time] of our own get/set
    this.actions = new Table([{ key: "name", label: "Action" }, { key: "description", label: "Description" }],
      { onActivate: () => this._runAction(), empty: "no actions" });
    this.alias = new Table([
      { key: "name", label: "Alias" }, { key: "target", label: "Target" },
      { key: "min", label: "Min" }, { key: "max", label: "Max" }, { key: "ramp_rate", label: "Ramp /s" },
      { key: "ramp_step", label: "Step" }, { key: "scale", label: "Scale" }, { key: "offset", label: "Offset" },
      { key: "unit", label: "Unit" }, { key: "last", label: "Last set" },
    ], { onActivate: () => this._editAlias(this.alias.current()), empty: "no aliases" });
    this.comp = new Table([
      { key: "name", label: "Composite" }, { key: "typeLabel", label: "Type" },
      { key: "summary", label: "Definition" }, { key: "chans", label: "Channels" },
      { key: "lastText", label: "Last set" },
    ], { onActivate: () => this._editComp(this.comp.current()), empty: "no composites" });

    // manual control
    // user actions: named snippets of code that control any channel
    this.uact = new Table([
      { key: "name", label: "Action" }, { key: "description", label: "Description" },
      { key: "lastText", label: "Last run" },
    ], { onActivate: () => this._runUact(this.uact.current()), empty: "no actions - Add… one: a few lines of get / set / sleep" });

    this.el = h("div", { class: "panel setup" },
      bar(btn("Save setup…", () => this._saveSetup(), { cmd: "get_setup", title: "download the setup (instruments, aliases, composites, monitor) as JSON" }),
        btn("Load setup…", () => this._loadSetup(), { cmd: "load_setup", title: "load a setup JSON file from this computer into the backend" }), this.msg),
      h("div", { class: "row2" },
        group("Instruments", this.inst.el, bar(
          btn("Add…", () => this._addInst(), { cmd: "add_instrument" }),
          btn("Edit…", () => this._editInst(), { cmd: "update_instrument" }),
          btn("Reconnect", () => this._instCmd("reconnect_instrument"), { cmd: "reconnect_instrument" }),
          btn("Disconnect", () => this._instCmd("disconnect_instrument"), { cmd: "disconnect_instrument" }),
          btn("Remove", () => this._instCmd("remove_instrument", true), { cmd: "remove_instrument" }))),
        group("Instrument channels & actions", this.raw.el, bar(
          btn("Make alias…", () => this._aliasFromRaw(), { cmd: "define_alias" }),
          btn("Get", () => this._rawGet(), { cmd: "read_channel" }),
          btn("Set…", () => this._rawSet(), { cmd: "write_channel", title: "set a configuration channel" })),
          this.actions.el, bar(btn("▶ Run action", () => this._runAction(), { cmd: "run_action" })))),
      h("div", { class: "row2" },
        group("Aliases", this.alias.el, bar(
          btn("Add…", () => this._editAlias(null), { cmd: "define_alias" }),
          btn("Edit…", () => this._editAlias(this.alias.current()), { cmd: "define_alias" }),
          btn("Remove", () => this._remove("remove_alias", this.alias.current()), { cmd: "remove_alias" }))),
        group("Composites", this.comp.el, bar(
          btn("Add…", () => this._editComp(null), { cmd: "define_composite" }),
          btn("Edit…", () => this._editComp(this.comp.current()), { cmd: "define_composite" }),
          btn("Remove", () => this._remove("remove_composite", this.comp.current()), { cmd: "remove_composite" })))),
      group("Actions", this.uact.el, bar(
        btn("▶ Run", () => this._runUact(this.uact.current()), { cmd: "run_user_action", title: "run the selected action now (double click works too)" }),
        this.uactAbort = btn("■ Abort", () => this._abortUact(), { cmd: "abort_user_action", title: "stop the running action(s) at the next line, sleep or ramp step" }),
        btn("Add…", () => this._editUact(null), { cmd: "define_user_action" }),
        btn("Edit…", () => this._editUact(this.uact.current()), { cmd: "define_user_action" }),
        btn("Remove", () => this._removeUact(), { cmd: "remove_user_action" }),
        btn("↑", () => this._moveUact(-1), { cmd: "move_user_action", title: "move up" }),
        btn("↓", () => this._moveUact(1), { cmd: "move_user_action", title: "move down" }))));
    this.model.on("state", (st) => this._state(st));
    this.conn.on("connected", () => this._loadDrivers());
  }

  async _loadDrivers() {
    try { this.drivers = (await this.conn.request("list_drivers")).drivers || {}; } catch (e) { /* viewer */ }
  }

  _state() {
    const insts = this.model.instruments();
    this.inst.set(insts, { rowTitle: (r) => r.last_error || null });
    this._showRaw();
    this.alias.set(this.model.aliases().map((a) => ({ ...a, last: a.last && a.last.shape ? `array(${a.last.shape.join("×")})` : a.last })),
      { rowClass: (r) => (r.error ? "err" : ""), rowTitle: (r) => r.error || r.description || null });
    this.comp.set(this.model.composites().map((c) => ({ ...c, typeLabel: COMP_TYPES[c.type] || c.type,
      summary: compSummary(c), chans: compChannels(c).join(", "), lastText: compLast(c.last) })),
    { rowTitle: (r) => r.description || null });
    const acts = this.model.userActions();
    this.uact.set(acts.map((a) => ({ ...a, lastText: uactStatus(a) })), {
      rowClass: (r) => (r.running ? "busy" : r.last && r.last.status === "error" ? "err" : ""),
      rowTitle: (r) => [r.description, r.last && r.last.error ? "last run: " + r.last.error : ""].filter(Boolean).join("\n") || null });
    if (this.uactAbort) this.uactAbort.disabled = !acts.some((a) => a.running) || !this.conn.can("abort_user_action");
  }

  _selInst() {
    const cur = this.inst.current();
    return cur ? this.model.instruments().find((i) => i.name === cur.name) : null;
  }

  _showRaw() {
    const inst = this._selInst();
    const rows = (inst ? inst.channels || [] : []).map((c) => {
      const loc = this.lastLocal.get(`${inst.name}.${c.name}`);
      // our own get/set may be newer than the last state snapshot
      return loc && (c.last_t === null || c.last_t === undefined || loc[1] >= c.last_t) ? { ...c, last: loc[0], last_t: loc[1] } : c;
    });
    this.raw.set(rows, { rowTitle: (r) => (r.last_t ? `last value read or set at ${new Date(r.last_t * 1000).toLocaleTimeString()}` : null) });
    this.actions.set(inst ? inst.actions || [] : []);
  }

  /** A value we just read or set: show it in the channel table at once. */
  _noteValue(ref, v) {
    if (!String(ref).includes(".")) return;
    this.lastLocal.set(ref, [v, Date.now() / 1000]);
    this._showRaw();
  }

  async _do(cmd, args, ok) {
    try {
      const r = await this.conn.request(cmd, args, { timeout: 600000 });
      if ((cmd === "read_channel" || cmd === "write_channel") && args.name) this._noteValue(args.name, r);
      this.say(typeof ok === "function" ? ok(r) : ok);
      this.model.scheduleRefresh();
      return r;
    } catch (e) { this.say(`${cmd}: ${e.message}`, true); throw e; }
  }

  // ---- instruments -------------------------------------------------------------
  async _instDialog(inst) {
    if (!Object.keys(this.drivers).length) await this._loadDrivers();
    const names = Object.keys(this.drivers).sort();
    const desc = (d) => (this.drivers[d] || {}).description || "";
    const optsFor = (d) => JSON.stringify(toJSONable((this.drivers[d] || {}).options || {}), null, 2);
    const first = inst ? inst.driver : names[0];
    return formDialog(inst ? `Instrument ${inst.name}` : "Add instrument", [
      { key: "name", label: "Name", value: inst ? inst.name : "" },
      { key: "driver", label: "Driver", type: "select", options: names, value: first,
        onChange: (el, inp) => { inp.info.textContent = desc(el.value); if (!inst) inp.options.value = optsFor(el.value); } },
      { key: "info", label: "", type: "info", value: desc(first) },
      { key: "address", label: "Address", value: inst ? inst.address : "NONE", placeholder: "GPIB0::12::INSTR, COM3, TCP::host::port, SIM::bus, NONE" },
      { key: "options", label: "Options (JSON)", type: "textarea", rows: 8,
        value: inst ? JSON.stringify(toJSONable(inst.options || {}), null, 2) : optsFor(first) },
    ], { wide: true, validate: (v) => {
      if (!v.name.trim()) throw new Error("name required");
      const o = JSON.parse(v.options || "{}");
      if (typeof o !== "object" || Array.isArray(o) || o === null) throw new Error("options must be a JSON object");
      return { name: v.name.trim(), driver: v.driver, address: v.address.trim() || "NONE", options: o };
    } });
  }

  async _addInst() {
    const v = await this._instDialog(null);
    if (v) await this._do("add_instrument", v, `instrument ${v.name} added`).catch(() => {});
  }

  async _editInst() {
    const inst = this._selInst();
    if (!inst || !this.conn.can("update_instrument")) return;
    const v = await this._instDialog(inst);
    if (v) await this._do("update_instrument", { name: inst.name, address: v.address, options: v.options, driver: v.driver }, `instrument ${inst.name} updated`).catch(() => {});
  }

  async _instCmd(cmd, confirm = false) {
    const inst = this._selInst();
    if (!inst) { this.say("select an instrument", true); return; }
    if (confirm && !(await confirmDialog("Remove instrument", `Remove instrument ${inst.name}?`))) return;
    await this._do(cmd, { name: inst.name }, `${cmd.replace("_instrument", "")} ${inst.name}: done`).catch(() => {});
  }

  // ---- raw channels ------------------------------------------------------------
  _selRaw() {
    const inst = this._selInst(), c = this.raw.current();
    return inst && c ? `${inst.name}.${c.name}` : null;
  }

  async _rawGet() {
    const ref = this._selRaw();
    if (!ref) { this.say("select a channel", true); return; }
    await this._do("read_channel", { name: ref }, (v) => `${ref} = ${fmt(v)}`).catch(() => {});
  }

  async _rawSet() {
    const ref = this._selRaw();
    if (!ref) { this.say("select a channel", true); return; }
    const c = this.raw.current();
    if (!c.config) { this.say("use an alias to set measurement channels (limits and ramps apply)", true); }
    let current = "";
    try { current = await this.conn.request("read_channel", { name: ref }); this._noteValue(ref, current); } catch (e) { /* unknown */ }
    if (c.allowed && c.allowed.length) {
      // only certain values accepted: offer them in a drop-down
      const idx = c.allowed.findIndex((x) => (typeof x === "number" ? Math.abs(Number(current) - x) <= 1e-9 * Math.abs(x || 1)
        : String(x).toLowerCase() === String(current).toLowerCase()));
      const v = await formDialog(`Set ${ref}`, [{ key: "value", label: c.description || "Value", type: "select",
        value: String(Math.max(0, idx)), options: c.allowed.map((x, i) => [String(i), typeof x === "number" ? fmt(x, 6) + (c.unit ? " " + c.unit : "") : String(x)]) }]);
      if (v) await this._do("write_channel", { name: ref, value: c.allowed[Number(v.value)], ramp: true }, (r) => `${ref} = ${fmt(r)}`).catch(() => {});
      return;
    }
    const v = await formDialog(`Set ${ref}`, [{ key: "value", label: c.description || "Value",
      value: current === null || current === undefined ? "" : (typeof current === "number" ? String(Number(current.toPrecision(10))) : String(current)),
      placeholder: c.unit || "" }]);
    if (v) await this._do("write_channel", { name: ref, value: parseValue(v.value), ramp: true }, (r) => `${ref} = ${fmt(r)}`).catch(() => {});
  }

  async _runAction() {
    const inst = this._selInst(), a = this.actions.current();
    if (!inst || !a) { this.say("select an action", true); return; }
    const ref = `${inst.name}.${a.name}`;
    this.say(`running ${ref} …`);
    await this._do("run_action", { ref }, `${ref}: done`).catch(() => {});
  }

  _aliasFromRaw() {
    const ref = this._selRaw(), c = this.raw.current();
    if (!ref) { this.say("select a channel", true); return; }
    if (c.config) { this.say("configuration channels cannot be aliased", true); return; }
    this._editAlias({ name: c.name, target: ref, unit: c.unit || "" }, true);
  }

  // ---- aliases & composites ------------------------------------------------------
  async _editAlias(a, isNew = false) {
    if (!this.conn.can("define_alias")) return;
    const existing = a && !isNew;
    a = a || {};
    const v = await formDialog(existing ? `Alias ${a.name}` : "Add alias", [
      { key: "name", label: "Name", value: a.name || "" },
      { key: "target", label: "Target (instrument.channel)", value: a.target || "", list: this.model.aliasableChannels() },
      { key: "min", label: "Minimum", value: optStr(a.min) }, { key: "max", label: "Maximum", value: optStr(a.max) },
      { key: "ramp_rate", label: "Ramp rate", value: optStr(a.ramp_rate), placeholder: "units/s, empty = jump" },
      { key: "ramp_step", label: "Ramp step", value: optStr(a.ramp_step), placeholder: "max step per write, optional" },
      { key: "scale", label: "Scale (alias = raw·scale + offset)", value: optStr(a.scale ?? 1) },
      { key: "offset", label: "Offset", value: optStr(a.offset ?? 0) },
      { key: "unit", label: "Unit", value: a.unit || "" },
      { key: "allowed", label: "Allowed values", value: (a.allowed || []).join(", "), placeholder: "for text channels: allowed values, comma separated" },
      { key: "read_only", label: "read only", type: "checkbox", value: !!a.read_only },
      { key: "description", label: "Description", value: a.description || "" },
    ], { validate: (v) => ({
      name: v.name.trim(), target: v.target.trim(), min: optNum(v.min), max: optNum(v.max),
      ramp_rate: optNum(v.ramp_rate), ramp_step: optNum(v.ramp_step), scale: optNum(v.scale) ?? 1,
      offset: optNum(v.offset) ?? 0, unit: v.unit.trim(),
      allowed: v.allowed.split(",").map((s) => s.trim()).filter(Boolean).length ? v.allowed.split(",").map((s) => s.trim()).filter(Boolean) : null,
      read_only: v.read_only, description: v.description }) });
    if (v) await this._do("define_alias", { ...v, replace: !!existing }, `alias ${v.name} saved`).catch(() => {});
  }

  async _editComp(c) {
    if (!this.conn.can("define_composite")) return;
    const existing = !!c;
    c = c || {};
    // saved from inside the dialog: a refused definition keeps it open with the reason
    await compositeDialog(this.model, this.conn, c, async (v) => {
      const replace = existing && c.name === v.name;
      if (!replace) {
        await this.conn.request("check_composite", { ...v, replace: false });
        if (existing) await this.conn.request("remove_composite", { name: c.name });
      }
      const r = await this.conn.request("define_composite", { ...v, replace }, { timeout: 600000 });
      const w = (r && r.warnings) || [];
      this.say(`composite ${v.name} saved` + (w.length ? `  ⚠ ${w.join("; ")}` : ""));
      this.model.scheduleRefresh();
    });
  }

  async _remove(cmd, row) {
    if (!row) { this.say("select a row", true); return; }
    if (!(await confirmDialog("Remove", `Remove ${row.name}?`))) return;
    await this._do(cmd, { name: row.name }, `${row.name} removed`).catch(() => {});
  }

  // ---- user actions ----------------------------------------------------------------
  async _runUact(a) {
    if (!a) { this.say("select an action", true); return; }
    if (!this.conn.can("run_user_action")) return;
    if (a.confirm && !(await confirmDialog("Run action", `Run ${a.name}?${a.description ? "\n\n" + a.description : ""}`))) return;
    this.say(`running ${a.name} …`);
    try {
      const r = await this.conn.request("run_user_action", { name: a.name }, { timeout: 7 * 24 * 3600 * 1000 });
      if (r.status === "finished") this.say(`✔ ${a.name} done (${fmt(r.elapsed, 3)} s)`);
      else this.say(`${a.name}: ${r.status}${r.error ? ": " + r.error : ""}`, true);
    } catch (e) { this.say(`${a.name}: ${e.message}`, true); }
    this.model.scheduleRefresh();
  }

  async _abortUact() {
    const a = this.uact.current();
    try {
      const r = await this.conn.request("abort_user_action", a && a.running ? { name: a.name } : {});
      this.say(`aborting ${(r || []).join(", ")}`);
    } catch (e) { this.say(e.message, true); }
  }

  async _editUact(a) {
    if (!this.conn.can("define_user_action")) return;
    const existing = !!a;
    a = a || {};
    const chans = this.model.readableChannels().concat(this.model.configChannels(true));
    const v = await formDialog(existing ? `Action ${a.name}` : "Add action", [
      { key: "name", label: "Name", value: a.name || "" },
      { key: "description", label: "Description", value: a.description || "" },
      { key: "confirm", label: "ask before running", type: "checkbox", value: !!a.confirm },
      { key: "code", label: "Code", type: "textarea", rows: 10, value: a.code || "",
        placeholder: "e.g.\nset_many({'VL': 0, 'VR': 0})\nset('B', 0)  # ramps at the alias rate\nprint('gates and field at 0')",
        help: ACTION_HELP + "\nChannels: " + chans.join(", ") },
    ], { wide: true, validate: async (v) => {
      const d = { name: v.name.trim(), code: v.code, description: v.description.trim(), confirm: v.confirm };
      if (!d.name || !d.code.trim()) throw new Error("an action needs a name and code");
      const args = existing ? { ...d, replace: a.name === d.name, rename_from: a.name } : d;
      await this.conn.request("define_user_action", args);     // refused: the dialog stays open
      return d;
    } });
    if (v) { this.say(`action ${v.name} saved`); this.model.scheduleRefresh(); }
  }

  async _removeUact() {
    const a = this.uact.current();
    if (!a) { this.say("select an action", true); return; }
    if (!(await confirmDialog("Remove action", `Remove the action ${a.name}?`))) return;
    await this._do("remove_user_action", { name: a.name }, `action ${a.name} removed`).catch(() => {});
  }

  async _moveUact(step) {
    const a = this.uact.current();
    if (!a) return;
    const names = this.model.userActions().map((x) => x.name);
    const pos = Math.max(0, Math.min(names.length - 1, names.indexOf(a.name) + step));
    await this._do("move_user_action", { name: a.name, position: pos }, "").catch(() => {});
  }

  // ---- setup files -----------------------------------------------------------------
  async _saveSetup() {
    try {
      const s = await this.conn.request("get_setup");
      download("setup.json", JSON.stringify(toJSONable(s), null, 2));
      this.say("setup downloaded");
    } catch (e) { this.say(e.message, true); }
  }

  async _loadSetup() {
    const f = await pickFile();
    if (!f) return;
    let setup;
    try { setup = JSON.parse(f.text); } catch (e) { this.say(`${f.name}: invalid JSON`, true); return; }
    if (!(await confirmDialog("Load setup", `Replace the backend's instruments, aliases and composites with ${f.name}?`))) return;
    const problems = await this._do("load_setup", { setup, replace: true }, `setup ${f.name} loaded`).catch(() => null);
    if (problems && problems.length) { this.say(`loaded with problems: ${problems.join("; ")}`, true); message("warning", problems.join("; ")); }
  }
}

function lastText(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "object" && Array.isArray(v.shape)) return `array ${v.shape.join("×")}`;
  if (v && v.shape && v.data) return `array ${v.shape.join("×")}`;
  if (typeof v === "number") return Number.isFinite(v) ? String(Number(v.toPrecision(6))) : String(v);
  return String(v);
}
