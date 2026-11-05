// Instruments & channels: instruments, raw channels and actions, aliases,
// composites, manual get/set, setup save/load (port of panels/setup_panel.py).

import { h, Table, btn, bar, group, fmt, formDialog, confirmDialog, download, pickFile, message } from "../ui.js";
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
      { key: "unit", label: "Unit" }, { key: "description", label: "Description" },
    ], { empty: "select an instrument" });
    this.actions = new Table([{ key: "name", label: "Action" }, { key: "description", label: "Description" }],
      { onActivate: () => this._runAction(), empty: "no actions" });
    this.alias = new Table([
      { key: "name", label: "Alias" }, { key: "target", label: "Target" },
      { key: "min", label: "Min" }, { key: "max", label: "Max" }, { key: "ramp_rate", label: "Ramp /s" },
      { key: "ramp_step", label: "Step" }, { key: "scale", label: "Scale" }, { key: "offset", label: "Offset" },
      { key: "unit", label: "Unit" }, { key: "last", label: "Last set" },
    ], { onActivate: () => this._editAlias(this.alias.current()), empty: "no aliases" });
    this.comp = new Table([
      { key: "name", label: "Composite" },
      { key: "targets", label: "Targets", fmt: (t) => Object.entries(t || {}).map(([k, v]) => `${k}=${v}`).join("; ") },
      { key: "read", label: "Read" }, { key: "min", label: "Min" }, { key: "max", label: "Max" },
      { key: "ramp_rate", label: "Ramp /s" }, { key: "last", label: "Last set" },
    ], { onActivate: () => this._editComp(this.comp.current()), empty: "no composites" });

    // manual control
    this.mChan = h("input", { placeholder: "channel", list: "em-all-channels", class: "w-chan" });
    this.mList = h("datalist", { id: "em-all-channels" });
    this.mValue = h("input", { placeholder: "value (number, text, or JSON list)" });
    this.mRamp = h("input", { type: "checkbox", checked: true });
    this.mResult = h("span", { class: "msg" });
    this.mValue.addEventListener("keydown", (e) => { if (e.key === "Enter") this._manualSet(); });

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
      group("Manual control", bar(this.mChan, this.mList, this.mValue, h("label", { class: "check" }, this.mRamp, " ramp"),
        btn("Get", () => this._manualGet(), { cmd: "read_channel" }), btn("Set", () => this._manualSet(), { cmd: "write_channel" })),
        this.mResult));
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
    this.comp.set(this.model.composites(), { rowTitle: (r) => r.description || null });
    const names = this.model.readableChannels().concat(this.model.rawChannels());
    this.mList.replaceChildren(...names.map((n) => h("option", { value: n })));
  }

  _selInst() {
    const cur = this.inst.current();
    return cur ? this.model.instruments().find((i) => i.name === cur.name) : null;
  }

  _showRaw() {
    const inst = this._selInst();
    this.raw.set(inst ? inst.channels || [] : []);
    this.actions.set(inst ? inst.actions || [] : []);
  }

  async _do(cmd, args, ok) {
    try {
      const r = await this.conn.request(cmd, args, { timeout: 600000 });
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
    const v = await formDialog(`Set ${ref}`, [{ key: "value", label: "Value", value: "" }]);
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
    const aliases = this.model.aliases().map((a) => a.name);
    const v = await formDialog(existing ? `Composite ${c.name}` : "Add composite channel", [
      { key: "name", label: "Name", value: c.name || "" },
      { key: "targets", label: "Targets", type: "textarea", rows: 5,
        value: Object.entries(c.targets || {}).map(([k, e]) => `${k} = ${e}`).join("\n"),
        placeholder: "one per line:  alias = expression of x\ne.g.\nVL = x\nVR = -0.8*x + 0.1",
        help: "available aliases: " + aliases.join(", ") },
      { key: "read", label: "Read formula", value: c.read || "", placeholder: "optional read-back from targets, e.g. (VL - VR)/2" },
      { key: "min", label: "Minimum", value: optStr(c.min) }, { key: "max", label: "Maximum", value: optStr(c.max) },
      { key: "ramp_rate", label: "Ramp rate", value: optStr(c.ramp_rate) },
      { key: "unit", label: "Unit", value: c.unit || "" },
      { key: "description", label: "Description", value: c.description || "" },
    ], { wide: true, validate: (v) => {
      const targets = {};
      for (let line of v.targets.split("\n")) {
        line = line.trim();
        if (!line || line.startsWith("#")) continue;
        const i = line.indexOf("=");
        if (i < 0) throw new Error(`line '${line}' needs 'alias = expression'`);
        targets[line.slice(0, i).trim()] = line.slice(i + 1).trim();
      }
      return { name: v.name.trim(), targets, read: v.read.trim() || null, min: optNum(v.min), max: optNum(v.max),
               ramp_rate: optNum(v.ramp_rate), unit: v.unit.trim(), description: v.description };
    } });
    if (v) await this._do("define_composite", { ...v, replace: existing }, `composite ${v.name} saved`).catch(() => {});
  }

  async _remove(cmd, row) {
    if (!row) { this.say("select a row", true); return; }
    if (!(await confirmDialog("Remove", `Remove ${row.name}?`))) return;
    await this._do(cmd, { name: row.name }, `${row.name} removed`).catch(() => {});
  }

  // ---- manual ----------------------------------------------------------------------
  async _manualGet() {
    const n = this.mChan.value.trim();
    if (!n) return;
    try {
      const v = await this.conn.request("read_channel", { name: n });
      this.mResult.textContent = `${n} = ${fmt(v, 8)}`;
      this.mResult.className = "msg";
    } catch (e) { this.mResult.textContent = `✘ ${e.message}`; this.mResult.className = "msg err"; }
  }

  async _manualSet() {
    const n = this.mChan.value.trim();
    if (!n) return;
    try {
      const value = parseValue(this.mValue.value);
      this.mResult.textContent = `setting ${n} …`;
      const v = await this.conn.request("write_channel", { name: n, value, ramp: this.mRamp.checked }, { timeout: 3600000 });
      this.mResult.textContent = `${n} = ${fmt(v, 8)}`;
      this.mResult.className = "msg";
      this.model.scheduleRefresh();
    } catch (e) { this.mResult.textContent = `✘ ${e.message}`; this.mResult.className = "msg err"; }
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
