// easyMeasure web frontend: login, layout and wiring of all panels.

import { Connection, login, logout, session } from "./conn.js";
import { Model } from "./model.js";
import { h, $, btn, Tabs, splitter, modal, formDialog, confirmDialog, applyPermissions, message,
         setStatusElement, Table, fmt } from "./ui.js";
import { SetupPanel } from "./panels/setup.js";
import { SweepEditor, RunningSweepView } from "./panels/sweep.js";
import { QueuePanel } from "./panels/queue.js";
import { MonitorPanel, ManualPanel, LogPanel } from "./panels/side.js";
import { LiveDataView, FileDataView, fmtSize } from "./panels/data.js";

const store = {
  get(k, d) { try { const v = localStorage.getItem("em." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("em." + k, JSON.stringify(v)); } catch (e) { /* blocked */ } },
};

function applyDisplay() {
  const root = document.documentElement;
  const theme = store.get("theme", "auto");
  if (theme === "auto") delete root.dataset.theme; else root.dataset.theme = theme;
  root.style.fontSize = `${store.get("font_px", 13)}px`;
}

class App {
  constructor() {
    this.conn = new Connection();
    this.model = new Model(this.conn);
    this.message = message;
    this.fileViews = [];
  }

  applyPermissions(root = document.body) { applyPermissions(root, (c) => this.conn.can(c)); }

  // ------------------------------------------------------------------ login --
  async boot() {
    applyDisplay();
    $("#login-form").addEventListener("submit", (ev) => { ev.preventDefault(); this._login(); });
    try {
      const s = await session();
      if (s && s.user) this._start(); else this._showLogin("");
    } catch (e) {
      this._showLogin(`gateway not reachable (${e.message})`);
    }
  }

  _showLogin(msg = "") {
    $("#app").hidden = true;
    $("#login").hidden = false;
    $("#login-error").textContent = msg;
    $("#login-user").focus();
  }

  async _login() {
    const user = $("#login-user").value.trim(), pw = $("#login-password").value;
    const b = $("#login-button");
    b.disabled = true;
    $("#login-error").textContent = "";
    try {
      await login(user, pw);
      $("#login-password").value = "";
      this._start();
    } catch (e) {
      $("#login-error").textContent = e.message;
    } finally { b.disabled = false; }
  }

  async _logout() {
    this.conn.stop();
    try { await logout(); } catch (e) { /* already gone */ }
    location.reload();
  }

  _start() {
    $("#login").hidden = true;
    $("#app").hidden = false;
    if (!this.built) { this._build(); this.built = true; }
    this.conn.start();
  }

  // ----------------------------------------------------------------- layout --
  _build() {
    const app = $("#app");
    this.connLabel = h("span", { class: "conn warn", text: "connecting…" });
    this.userLabel = h("span", { class: "user" });
    this.statusMsg = h("span", { class: "status-msg" });
    setStatusElement(this.statusMsg);
    this.queueLabel = h("span", { class: "muted" });
    this.progress = h("progress", { max: 1, value: 0, hidden: true });
    this.rate = h("span", { class: "muted small", title: "data received from the backend" });

    this.setup = new SetupPanel(this);
    this.editor = new SweepEditor(this);
    this.queue = new QueuePanel(this);
    this.running = new RunningSweepView(this);
    this.live = new LiveDataView(this);
    this.monitor = new MonitorPanel(this);
    this.manual = new ManualPanel(this);
    this.log = new LogPanel(this);

    this.leftTabs = new Tabs();
    this.tSetup = this.leftTabs.add("Instruments & channels", this.setup.el);
    this.tEditor = this.leftTabs.add("Sweep editor", this.editor.el);
    this.tQueue = this.leftTabs.add("Queue", this.queue.el);
    this.dataTabs = new Tabs({ closable: true, onClose: (it) => { this.fileViews = this.fileViews.filter((v) => v.el !== it.el); if (it.view) it.view.destroy(); } });
    this.tLive = this.dataTabs.add("Live data", this.live.el, { closable: false });

    const left = h("section", { class: "pane left" }, this.leftTabs.el);
    const right = h("section", { class: "pane right" }, this.dataTabs.el);
    this.monBox = h("div", { class: "side-box mon" }, h("div", { class: "side-title", text: "Monitor" }), this.monitor.el);
    this.manBox = h("div", { class: "side-box man" }, h("div", { class: "side-title", text: "Manual" }), this.manual.el);
    this.side = h("aside", { class: "pane side" }, this.monBox, splitter(this.monBox, { vertical: false, key: "side" }), this.manBox);
    this.logBox = h("section", { class: "logpane" }, this.log.el);
    this.workspace = h("main", { class: "workspace" }, left, splitter(left, { key: "left" }), right,
      splitter(right, { key: "right" }), this.side);

    const viewMenu = this._menu("View", [
      ["Monitor", () => this._toggle(this.monBox, "show_monitor"), () => !this.monBox.hidden],
      ["Manual", () => this._toggle(this.manBox, "show_manual"), () => !this.manBox.hidden],
      ["Log", () => this._toggle(this.logBox, "show_log"), () => !this.logBox.hidden],
      ["Running sweep view", () => this._toggleRunning(), () => !!this.tRunning],
    ]);
    const backendMenu = this._menu("Backend", [
      ["Browse recent runs…", () => this.browseRuns()],
      ["Settings…", () => this.settings()],
      ["Download PowerPoint file", () => { location.href = "/api/ppt"; }],
      ["Connected clients…", () => this.clients()],
      ["Shut down backend…", () => this.shutdown(), null, "shutdown"],
    ]);
    const header = h("header", { class: "toolbar" },
      h("span", { class: "brand", text: "easyMeasure" }), this.connLabel,
      h("span", { class: "sep" }),
      btn("▶ Start queue", () => this._cmd("queue_start"), { cmd: "queue_start" }),
      btn("⏸ Pause", () => this._cmd("pause"), { cmd: "pause" }),
      btn("Resume", () => this._cmd("resume"), { cmd: "resume" }),
      btn("■ Abort", () => this._cmd("abort"), { cmd: "abort", cls: "danger" }),
      h("span", { class: "sep" }),
      btn("Add slide to PPT", () => this._ppt(), { cmd: "make_ppt", title: "append a slide for the running / last sweep" }),
      btn("Open data…", () => this.browseRuns(), { cmd: "list_runs" }),
      viewMenu, backendMenu,
      h("span", { class: "grow" }), this.userLabel,
      btn("Log out", () => this._logout()));
    const footer = h("footer", { class: "statusbar" }, this.statusMsg, h("span", { class: "grow" }), this.rate, this.queueLabel, this.progress);
    app.replaceChildren(header, this.workspace, splitter(this.workspace, { vertical: false, key: "log", min: 120 }), this.logBox, footer);

    for (const [el, key, def] of [[this.monBox, "show_monitor", true], [this.manBox, "show_manual", true], [this.logBox, "show_log", true]]) el.hidden = !store.get(key, def);
    this._sideVisibility();
    if (store.get("show_running", false)) this._toggleRunning(true);

    this.conn.on("status", (text, cls) => { this.connLabel.textContent = text; this.connLabel.className = "conn " + cls; });
    this.conn.on("connected", (w) => this._connected(w));
    this.conn.on("disconnected", () => message("warning", "lost connection to the backend – retrying"));
    this.conn.on("auth", (why) => { this.conn.stop(); this._showLogin(`session ended: ${why}`); });
    this.conn.on("event", (k, d) => this._event(k, d));
    this.model.on("message", (lvl, text) => message(lvl, text));
    this.model.on("queue", (q) => { this.queueLabel.textContent = `queue: ${q.state}`; });
    setInterval(() => this._rates(), 2000);
    window.addEventListener("beforeunload", () => this.conn.stop());
  }

  _menu(title, items) {
    const pop = h("div", { class: "menu-pop", hidden: true });
    const b = h("button", { class: "menu-btn", text: title + " ▾", on: { click: (ev) => {
      ev.stopPropagation();
      const show = pop.hidden;
      for (const p of document.querySelectorAll(".menu-pop")) p.hidden = true;
      pop.hidden = !show;
      if (show) {
        pop.replaceChildren(...items.map(([label, fn, checked, cmd]) => {
          const it = h("div", { class: "menu-item", text: (checked ? (checked() ? "✓ " : "   ") : "") + label,
            on: { click: () => { pop.hidden = true; fn(); } } });
          if (cmd && !this.conn.can(cmd)) { it.classList.add("denied"); it.title = "not allowed for your role"; }
          return it;
        }));
      }
    } } });
    document.addEventListener("click", () => { pop.hidden = true; });
    return h("span", { class: "menu" }, b, pop);
  }

  _toggle(el, key) { el.hidden = !el.hidden; store.set(key, !el.hidden); this._sideVisibility(); window.dispatchEvent(new Event("resize")); }
  _sideVisibility() {
    this.side.hidden = this.monBox.hidden && this.manBox.hidden;
    const s = this.side.querySelector(".splitter");
    if (s) s.hidden = this.monBox.hidden || this.manBox.hidden;
    const sp = this.workspace.querySelectorAll(":scope > .splitter")[1];
    if (sp) sp.hidden = this.side.hidden;
    const lsp = $("#app").querySelector(":scope > .splitter");
    if (lsp) lsp.hidden = this.logBox.hidden;
  }

  _toggleRunning(on = null) {
    on = on === null ? !this.tRunning : on;
    if (on && !this.tRunning) { this.tRunning = this.leftTabs.add("Running sweep", this.running.el, { index: 2 }); this.running.refresh(); }
    else if (!on && this.tRunning) { this.leftTabs.remove(this.tRunning); this.tRunning = null; }
    store.set("show_running", on);
  }

  _connected(w) {
    this.userLabel.textContent = `${w.user} (${w.role})`;
    this.userLabel.title = `session valid until ${new Date(w.session_expires * 1000).toLocaleString()}`;
    this.applyPermissions();
    message("info", `connected to backend ${w.version || ""} as ${w.user} (${w.role})`);
    this.model.refresh();
  }

  _event(kind, data) {
    if (kind === "sweep_point" && data && data.progress) {
      const p = data.progress;
      if (p.total) { this.progress.hidden = false; this.progress.max = p.total; this.progress.value = p.done; }
    } else if (kind === "sweep_end") {
      this.progress.hidden = true;
      if (data) message(data.status === "finished" ? "info" : "warning", `run ${data.run} ${data.status} ${data.error || ""}`);
    } else if (kind === "sweep_start") this.dataTabs.show(this.tLive);
  }

  _rates() {
    const s = this.conn.stats, now = performance.now();
    const dt = (now - (this._rt || s.t0)) / 1000;
    const bytes = s.rxBytes - (this._rb || 0);
    this._rt = now; this._rb = s.rxBytes;
    this.rate.textContent = `↓ ${fmt(bytes / Math.max(dt, 1e-3) / 1024, 3)} kB/s`;
  }

  async _cmd(cmd, args = {}) {
    try { await this.conn.request(cmd, args); message("info", `${cmd}: ok`); } catch (e) { message("error", `${cmd}: ${e.message}`); }
  }

  async _ppt() {
    try { const p = await this.conn.request("make_ppt", {}, { timeout: 300000 }); message("info", `slide added to ${p}`); }
    catch (e) { message("error", "PowerPoint: " + e.message); }
  }

  // ------------------------------------------------------------------- data --
  copyToEditor(def) { this.editor.loadDefinition(def); this.leftTabs.show(this.tEditor); }

  openRun(path) {
    const existing = this.dataTabs.items.find((it) => it.path === path);
    if (existing) { this.dataTabs.show(existing); return; }
    const v = new FileDataView(this, path);
    const name = path.split(/[\\/]/).pop();
    const it = this.dataTabs.add(name, v.el, { title: path });
    it.path = path;
    it.view = v;
    this.fileViews.push(v);
    this.applyPermissions(v.el);
    this.dataTabs.show(it);
  }

  async pickRun(title = "Open data") {
    let runs;
    try { runs = await this.conn.request("list_runs", { limit: 300 }); } catch (e) { message("error", e.message); return null; }
    if (!runs || !runs.length) { message("info", "no runs found"); return null; }
    let chosen = null;
    const filter = h("input", { type: "search", placeholder: "filter" });
    const t = new Table([{ key: "name", label: "Run file" }, { key: "size", label: "Size", fmt: (v) => fmtSize(v) },
                         { key: "mtime", label: "Modified", fmt: (v) => new Date(v * 1000).toLocaleString() }],
                        { onSelect: (s) => { chosen = s[0] || null; }, onActivate: (r) => { chosen = r; done.click(); } });
    t.keyOf = (r) => r.path;
    const show = () => { const q = filter.value.toLowerCase(); t.set(runs.filter((r) => r.name.toLowerCase().includes(q))); };
    filter.addEventListener("input", show);
    show();
    let done;
    const body = h("div", { class: "picker" }, filter, t.el);
    const p = modal(title, body, [["Cancel", null], ["Open", true]], { wide: true, onOk: () => (chosen ? chosen : false) });
    done = document.querySelector("dialog.modal:last-of-type .modal-buttons button.primary");
    return p;
  }

  async browseRuns() {
    const r = await this.pickRun("Open data");
    if (r) this.openRun(r.path);
  }

  // ---------------------------------------------------------------- backend --
  async settings() {
    let st = null;
    try { st = await this.conn.request("get_settings"); } catch (e) { /* not allowed */ }
    const admin = this.conn.can("set_settings");
    const fields = [
      { key: "font_px", label: "Font size (px, this browser)", value: String(store.get("font_px", 13)) },
      { key: "theme", label: "Theme (this browser)", type: "select", options: [["auto", "follow system"], ["dark", "dark"], ["light", "light"]], value: store.get("theme", "auto") },
    ];
    if (st) {
      fields.push({ key: "_h", label: "", type: "info", value: admin ? "Backend settings" : "Backend settings (read only: admin role required)" },
        { key: "data_dir", label: "Data directory", value: st.data_dir || "" },
        { key: "ppt_path", label: "PowerPoint file", value: st.ppt_path || "" },
        { key: "auto_ppt", label: "append a slide automatically when a sweep ends", type: "checkbox", value: st.auto_ppt },
        { key: "export_hdf5", label: "export an HDF5 copy of every run", type: "checkbox", value: st.export_hdf5 },
        { key: "fsync_interval", label: "fsync interval (s)", value: String(st.fsync_interval ?? 0.5) },
        { key: "driver_paths", label: "Driver paths (one per line)", type: "textarea", rows: 2, value: (st.driver_paths || []).join("\n") },
        { key: "ppt_colormap", label: "PPT colormap", value: st.ppt_colormap || "viridis" });
    }
    const v = await formDialog("Settings", fields, { wide: true });
    if (!v) return;
    store.set("font_px", Math.max(8, Math.min(32, Number(v.font_px) || 13)));
    store.set("theme", v.theme);
    applyDisplay();
    window.dispatchEvent(new Event("resize"));
    if (st && admin) {
      const vals = { ppt_path: v.ppt_path.trim(), auto_ppt: v.auto_ppt, export_hdf5: v.export_hdf5,
        fsync_interval: Number(v.fsync_interval) || 0.5, driver_paths: v.driver_paths.split("\n").map((s) => s.trim()).filter(Boolean),
        ppt_colormap: v.ppt_colormap.trim() || "viridis" };
      if (v.data_dir.trim()) vals.data_dir = v.data_dir.trim();
      try { await this.conn.request("set_settings", vals); message("info", "settings saved"); } catch (e) { message("error", "settings: " + e.message); }
    }
  }

  async clients() {
    let cl;
    try { cl = await this.conn.request("list_clients"); } catch (e) { message("error", e.message); return; }
    const t = new Table([{ key: "name", label: "Client" }, { key: "id", label: "Address" }, { key: "events", label: "Events", fmt: (v) => (Array.isArray(v) ? `${v.length} kinds` : v) }, { key: "dropped", label: "Dropped events" }]);
    t.keyOf = (r) => r.id;
    t.set(cl || []);
    await modal("Connected clients", t.el, [["Close", null]], { wide: true });
  }

  async shutdown() {
    if (!this.conn.can("shutdown")) return;
    if (!(await confirmDialog("Shut down backend", "Shut down the backend? Instruments will be disconnected and remote access ends."))) return;
    this._cmd("shutdown", { force: false });
  }
}

const app = new App();
window.addEventListener("DOMContentLoaded", () => app.boot());
