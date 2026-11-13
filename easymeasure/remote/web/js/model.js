// Client-side cache of the backend state (port of frontend/model.py).

export class Model {
  constructor(conn) {
    this.conn = conn;
    this.state = {};
    this.handlers = {};
    this._t = null;
    conn.on("connected", () => this.refresh());
    conn.on("event", (kind, data) => this._event(kind, data));
  }

  on(evt, fn) { (this.handlers[evt] ||= []).push(fn); }
  emit(evt, ...a) { for (const f of this.handlers[evt] || []) { try { f(...a); } catch (e) { console.error(e); } } }

  async refresh() {
    try {
      const st = await this.conn.request("get_state");
      if (st && typeof st === "object") {
        this.state = st;
        this.emit("state", st);
        if (st.queue) this.emit("queue", st.queue);
        this.emit("ramps", st.ramps || []);
      }
    } catch (e) {
      this.emit("message", "error", "get_state: " + e.message);
    }
  }

  scheduleRefresh() {
    clearTimeout(this._t);
    this._t = setTimeout(() => this.refresh(), 150);
  }

  _event(kind, data) {
    if (kind === "changed" || kind === "settings" || kind === "user_action") this.scheduleRefresh();
    else if (kind === "queue" && data) { this.state.queue = data; this.emit("queue", data); }
    else if (kind === "ramps" && data) { this.state.ramps = data.active || []; this.emit("ramps", this.state.ramps); }
    else if (kind === "sweep_warning") this.emit("message", "warning", String(data && data.message));
    else if (kind === "instrument_fault") {
      this.emit("message", "error", `FAULT ${data && data.instrument}: ${data && data.message}` +
        (data && data.aborted ? " - the running item was aborted and the queue stopped" : " - the queue was stopped"));
      this.scheduleRefresh();
    }
    else if (kind === "ppt") this.emit("message", "info", `slide added to ${data && data.path}`);
  }

  aliases() { return ((this.state.channels || {}).aliases) || []; }
  composites() { return ((this.state.channels || {}).composites) || []; }
  /** Every channel of the backend, one list: {name, kind, access, readable, writable, dtype,
   *  shape, unit, min, max, ramp_rate, choices, description, last, …}; kind is hardware,
   *  config, action, alias, variable, map, read, parameter, user_action or special. */
  channelList() { return ((this.state.channels || {}).channels) || []; }
  /** channels the composites define: kind "variable" (of a multivariate write), "map", "read" */
  virtualChannels() { return this.channelList().filter((c) => ["variable", "map", "read"].includes(c.kind)); }
  /** multivariate composites with several variables (set together with set_many); the
   *  variables by their channel names (nD.n) */
  compositeGroups() {
    return this.composites().filter((c) => c.type === "multi" && (c.variables || []).length > 1)
      .map((c) => ({ name: c.name, variables: c.variables.map((v) => `${c.name}.${v.name}`) }));
  }
  instruments() { return this.state.instruments || []; }
  /** user-defined actions: {name, code, description, confirm, running, last} */
  userActions() { return this.state.actions || []; }
  /** The setup's parameters: {name, value | formula, unit, description, current, used_by}. */
  parameters() { return ((this.state.channels || {}).parameters) || []; }
  settings() { return this.state.settings || {}; }

  /** names of the channels that fit pred: named channels first, the instruments' after */
  _names(pred) {
    const order = { alias: 0, variable: 1, map: 1, read: 1, parameter: 2, special: 3, user_action: 4,
      hardware: 5, config: 6, action: 7 };
    return this.channelList().filter(pred)
      .sort((a, b) => (order[a.kind] ?? 9) - (order[b.kind] ?? 9)).map((c) => c.name);
  }
  /** channels with a value that can be written (not actions); an instrument's own
   *  channels only if this session may write them (not remote operators) */
  writableChannels(numericOnly = false, hardware = true) {
    if (this.conn && this.conn.canWriteRaw && !this.conn.canWriteRaw()) hardware = false;
    return this._names((c) => c.writable && !["action", "user_action"].includes(c.kind) &&
      !(numericOnly && c.dtype === "str") && (hardware || !["hardware", "config"].includes(c.kind)));
  }
  readableChannels(hardware = true) {
    return this._names((c) => c.readable && c.kind !== "special" &&
      (hardware || !["hardware", "config"].includes(c.kind)));
  }
  /** actions: the user's (plain names), then the instruments' (dmm.reset) */
  actionChannels() { return this._names((c) => c.kind === "user_action" || c.kind === "action"); }
  /** the channels of the instruments (instrument.channel) */
  rawChannels() { return this._names((c) => c.kind === "hardware" || c.kind === "config"); }
  aliasableChannels() { return this.rawChannels(); }
  configChannels(writable = false) {
    return this._names((c) => c.kind === "config" && (writable ? c.writable : c.readable));
  }
  /** name -> allowed values, for channels that only take certain values */
  channelChoices() {
    const out = {};
    for (const c of this.channelList()) if (c.choices && c.choices.length) out[c.name] = c.choices;
    return out;
  }
  /** the instruments' actions (instrument.action) */
  actions() { return this._names((c) => c.kind === "action"); }
  channelMeta(name) {
    const a = this.aliases().find((x) => x.name === name);
    if (a) return a;
    return this.channelList().find((x) => x.name === name) || null;
  }
}
