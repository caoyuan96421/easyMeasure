// Connection to the gateway: login, secure WebSocket with automatic reconnect,
// promise-based requests and pushed events.  Nothing here ever blocks the page.

import { decode, encode } from "./codec.js";

export class AuthError extends Error {}

async function api(path, body) {
  const opts = { method: body === undefined ? "GET" : "POST", credentials: "same-origin",
                 headers: { "Accept": "application/json" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok) {
    const err = new Error((data && data.error) || `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return data;
}

export const session = () => api("/api/session");
export const login = (user, password) => api("/api/login", { user, password });
export const logout = () => api("/api/logout", {});

export class Connection {
  constructor() {
    this.ws = null;
    this.pending = new Map();
    this.nextId = 1;
    this.handlers = {};
    this.connected = false;
    this.welcome = null;
    this.allowed = new Set();
    this.restrict = new Set();
    this.role = null;
    this.user = null;
    this._retry = 1000;
    this._stopped = true;
    this._timer = null;
    this.stats = { rx: 0, rxBytes: 0, tx: 0, t0: performance.now() };
  }

  on(evt, fn) { (this.handlers[evt] ||= []).push(fn); return fn; }
  off(evt, fn) { this.handlers[evt] = (this.handlers[evt] || []).filter((f) => f !== fn); }
  emit(evt, ...args) {
    for (const fn of this.handlers[evt] || []) {
      try { fn(...args); } catch (e) { console.error(e); }
    }
  }
  can(cmd) { return this.allowed.has(cmd); }
  /** may this session write an instrument's own channels (instrument.channel)? */
  canWriteRaw() { return !this.restrict.has("raw_writes"); }

  start() {
    this._stopped = false;
    this._connect();
  }

  stop() {
    this._stopped = true;
    clearTimeout(this._timer);
    if (this.ws) { try { this.ws.close(1000, "bye"); } catch (e) { /* closed */ } }
  }

  _connect() {
    if (this._stopped) return;
    clearTimeout(this._timer);
    this.emit("status", "connecting…", "warn");
    let ws;
    try {
      ws = new WebSocket(`wss://${location.host}/ws`);
    } catch (e) {
      this._schedule();
      return;
    }
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (ev) => {
      let msg;
      this.stats.rx++;
      this.stats.rxBytes += ev.data.byteLength || 0;
      try { msg = decode(ev.data); } catch (e) { console.error("bad frame", e); return; }
      this._dispatch(msg);
    };
    ws.onclose = (ev) => this._closed(ws, ev.code, ev.reason);
    ws.onerror = () => { /* followed by onclose */ };
  }

  _dispatch(msg) {
    if (!msg || typeof msg !== "object") return;
    const t = msg.type;
    if (t === "resp") {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      clearTimeout(p.timer);
      if (msg.ok) p.resolve(msg.result);
      else {
        const e = new Error(msg.error || "error");
        e.kind = msg.kind;
        p.reject(e);
      }
    } else if (t === "event") {
      this.emit("event", msg.event, msg.data);
    } else if (t === "welcome") {
      this.welcome = msg;
      this.allowed = new Set(msg.allowed || []);
      /** what the backend refuses for this role: "raw_writes" (an instrument's own channels) */
      this.restrict = new Set(msg.restrict || []);
      this.role = msg.role;
      this.user = msg.user;
      this.connected = true;
      this._retry = 1000;
      this._send({ type: "subscribe", events: "*" });
      this.emit("status", "connected", "ok");
      this.emit("connected", msg);
    }
  }

  async _closed(ws, code, reason) {
    if (ws !== this.ws) return;
    const was = this.connected;
    this.connected = false;
    this.ws = null;
    for (const [, p] of this.pending) { clearTimeout(p.timer); p.reject(new Error("connection lost")); }
    this.pending.clear();
    if (was) this.emit("disconnected", code, reason);
    if (this._stopped) return;
    if (code === 4401 || code === 4403) {
      this.emit("auth", reason || "session ended");
      return;
    }
    if (code === 1006 || code === 1008) {
      // the upgrade may have been refused: is the session still valid?
      try {
        const s = await session();
        if (!s || !s.user) { this.emit("auth", "please log in"); return; }
      } catch (e) { /* gateway unreachable: keep retrying */ }
    }
    const why = code === 1013 ? "backend not running" : code === 1012 ? "backend restarting"
      : code === 1001 ? "gateway restarting" : reason || "connection lost";
    this.emit("status", `${why} – retrying`, "err");
    this._schedule();
  }

  _schedule() {
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this._connect(), this._retry);
    this._retry = Math.min(15000, this._retry * 1.7);
  }

  _send(msg) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(encode(msg));
    this.stats.tx++;
    return true;
  }

  /** Send a request; resolves with the result, rejects with an Error. */
  request(cmd, args = {}, { timeout = 120000 } = {}) {
    return new Promise((resolve, reject) => {
      if (!this.connected) { reject(new Error("not connected")); return; }
      const id = this.nextId++;
      const timer = setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`no answer to ${cmd} in time`));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer, cmd });
      if (!this._send({ type: "req", id, cmd, args })) {
        this.pending.delete(id);
        clearTimeout(timer);
        reject(new Error("not connected"));
      }
    });
  }
}
