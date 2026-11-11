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
    else if (kind === "sweep_warning") this.emit("message", "warning", String(data && data.message));
    else if (kind === "ppt") this.emit("message", "info", `slide added to ${data && data.path}`);
  }

  aliases() { return ((this.state.channels || {}).aliases) || []; }
  composites() { return ((this.state.channels || {}).composites) || []; }
  /** channels the composites define: kind "variable" (of a multivariate write), "map", "read" */
  virtualChannels() { return ((this.state.channels || {}).virtual) || []; }
  /** multivariate composites with several variables (set together with set_many) */
  compositeGroups() {
    return this.composites().filter((c) => c.type === "multi" && (c.variables || []).length > 1)
      .map((c) => ({ name: c.name, variables: c.variables.map((v) => v.name) }));
  }
  instruments() { return this.state.instruments || []; }
  /** user-defined actions: {name, code, description, confirm, running, last} */
  userActions() { return this.state.actions || []; }
  settings() { return this.state.settings || {}; }

  writableChannels(numericOnly = false) {
    const out = this.aliases().filter((a) => a.writable && !(numericOnly && a.dtype === "str")).map((a) => a.name);
    return out.concat(this.virtualChannels().filter((v) => v.writable).map((v) => v.name));
  }
  readableChannels() {
    return this.aliases().filter((a) => a.readable !== false).map((a) => a.name)
      .concat(this.virtualChannels().map((v) => v.name));
  }
  rawChannels() {
    return this.instruments().flatMap((i) => (i.channels || []).map((c) => `${i.name}.${c.name}`));
  }
  aliasableChannels() {
    return this.instruments().flatMap((i) => (i.channels || []).filter((c) => !c.config).map((c) => `${i.name}.${c.name}`));
  }
  configChannels(writable = false) {
    return this.instruments().flatMap((i) => (i.channels || []).filter((c) => c.config &&
      (writable ? (c.access || "rw").includes("w") : (c.access || "rw").includes("r"))).map((c) => `${i.name}.${c.name}`));
  }
  /** name -> allowed values, for aliases and configuration channels that only take certain values */
  channelChoices() {
    const out = {};
    for (const a of this.aliases()) if (a.choices && a.choices.length) out[a.name] = a.choices;
    for (const i of this.instruments()) for (const c of i.channels || []) if (c.allowed && c.allowed.length) out[`${i.name}.${c.name}`] = c.allowed;
    return out;
  }
  actions() {
    return this.instruments().flatMap((i) => (i.actions || []).map((a) => `${i.name}.${a.name}`));
  }
  channelMeta(name) {
    const a = this.aliases().find((x) => x.name === name);
    if (a) return a;
    return this.virtualChannels().find((x) => x.name === name) || null;
  }
}
