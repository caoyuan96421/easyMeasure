// Data views: the live view of the running sweep and views of stored runs.

import { h, btn, bar, fmt, fmtTime, autoColumns } from "../ui.js";
import { NDPlot } from "../plot.js";
import { DataSet } from "../dataset.js";
import { toJSONable } from "../codec.js";

const PLOT_MIN_W = 240;   // px: below this the plots scroll sideways instead of shrinking
const PLOT_MIN_H = 110;   // px of the plot area itself

export class DataView {
  constructor(app, { live = false } = {}) {
    this.app = app;
    this.conn = app.conn;
    this.live = live;
    this.ds = null;
    this.plots = [];
    this.title = h("div", { class: "dv-title", text: "no data" });
    this.progress = h("progress", { max: 100, value: 0, hidden: !live });
    this.eta = h("span", { class: "muted" });
    this.cols = h("select", { title: "number of plot columns", on: { change: () => this._relayout() } },
      [["0", "columns: auto"], ["1", "1 column"], ["2", "2 columns"], ["3", "3 columns"], ["4", "4 columns"]].map(([v, l]) => h("option", { value: v, text: l })));
    this.details = h("pre", { class: "details", hidden: true });
    this.grid = h("div", { class: "plot-grid" });
    this.bar = bar(this.eta, this.progress, this.cols,
      btn("Add plot", () => this.addPlot()),
      btn("Copy sweep to editor", () => { const d = this.ds && this.ds.header.definition; if (d) app.copyToEditor(d); }),
      btn("Add slide to PPT", () => this._ppt(), { cmd: "make_ppt", title: "append a slide for this run to the PowerPoint file configured on the backend" }),
      btn("Details", () => { this.details.hidden = !this.details.hidden; }));
    this.el = h("div", { class: "dataview" }, this.title, this.bar, this.grid, this.details);
    // every plot in view: the plots share the area; it scrolls only below their minimum size
    new ResizeObserver(() => { if (this.cols.value === "0" && this._effCols() !== this._eff) this._relayout(); }).observe(this.grid);
    this._hdrTimer = setInterval(() => {
      this._updateHeader();
      if (this.cols.value === "0" && this.plots.length && this.el.offsetParent !== null && this._effCols() !== this._eff) this._relayout();
    }, 500);
  }

  destroy() { clearInterval(this._hdrTimer); for (const p of this.plots) p.destroy(); }

  setDataset(ds) {
    const old = this.plots.map((p) => p.channel);
    this.ds = ds;
    const names = ds ? ds.recordNames() : [];
    if (!ds) { for (const p of this.plots) p.setDataset(null); this._updateHeader(); return; }
    if (!this.plots.length) {
      const numeric = names.filter((n) => !(ds.data[n] && ds.data[n].dtype === "O"));
      for (const n of (numeric.slice(0, 4).length ? numeric.slice(0, 4) : [null])) this.addPlot(n);
    } else this.plots.forEach((p, i) => p.setDataset(ds, names.includes(old[i]) ? old[i] : null));
    this._updateHeader();
    this._updateDetails();
  }

  addPlot(channel = null) {
    const p = new NDPlot({ closable: true, onClose: (x) => this._removePlot(x) });
    this.plots.push(p);
    if (this.ds) {
      const names = this.ds.recordNames();
      if (channel === null && names.length) channel = names[Math.min(this.plots.length - 1, names.length - 1)];
      p.setDataset(this.ds, channel);
    }
    this._relayout();
    return p;
  }

  _removePlot(p) {
    this.plots = this.plots.filter((x) => x !== p);
    p.destroy();
    this._relayout();
  }

  _effCols() {
    const n = Number(this.cols.value);
    if (n > 0) return n;
    if (!this.grid.clientHeight) return Math.max(1, Math.min(4, this.plots.length || 1, Math.floor(this.el.clientWidth / 420) || 1));
    let minH = 180;
    for (const p of this.plots) if (p.el.offsetHeight) minH = Math.max(minH, p.el.offsetHeight - p.pc.el.offsetHeight + PLOT_MIN_H);
    return autoColumns(this.plots.length, this.grid.clientWidth, this.grid.clientHeight, PLOT_MIN_W, minH, 8, 4);
  }

  _relayout() {
    const n = this._effCols();
    this._eff = n;
    this.grid.style.gridTemplateColumns = `repeat(${n}, minmax(${PLOT_MIN_W}px, 1fr))`;
    this.grid.replaceChildren(...this.plots.map((p) => p.el));
  }

  dataChanged() { for (const p of this.plots) { p.refreshChannelList(); p.markDirty(); } }

  _updateHeader() {
    const ds = this.ds;
    if (!ds) { this.title.textContent = "no data"; this.progress.value = 0; this.eta.textContent = ""; return; }
    const hd = ds.header;
    const errs = Object.values(ds.errors).reduce((a, b) => a + b, 0);
    this.title.textContent = `Run ${hd.run ?? "?"} – ${hd.name || ""}   [${ds.status}]   ${ds.points}/${ds.totalPoints} points${errs ? `   ${errs} read errors` : ""}`;
    if (this.live) {
      this.progress.value = 100 * ds.points / (ds.totalPoints || 1);
      // a continued run: the rate since it was continued
      const ab = hd.aborts || [];
      const p0 = ab.length ? ab[ab.length - 1].points || 0 : 0;
      const t0 = hd.continued || hd.started;
      if (t0 && ds.status === "running" && ds.points > p0) {
        const el = Date.now() / 1000 - t0;
        this.eta.textContent = `elapsed ${fmtTime(el)}, remaining ~${fmtTime(el / (ds.points - p0) * (ds.totalPoints - ds.points))}`;
      } else if (ds.status !== "running") this.eta.textContent = "";
    }
  }

  _updateDetails() {
    const ds = this.ds;
    if (!ds) return;
    const short = (o) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, v && v.shape ? `array(${v.shape.join("×")})` : v]));
    const lines = [`file: ${ds.header.file || "(live)"}`, `shape: ${ds.shape.join(" × ")}`, `status: ${ds.status}`];
    if (Object.keys(ds.pre).length) lines.push("pre: " + JSON.stringify(short(ds.pre)));
    if (Object.keys(ds.post).length) lines.push("post: " + JSON.stringify(short(ds.post)));
    if (ds.footer) lines.push("footer: " + JSON.stringify(toJSONable(ds.footer)));
    const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "?");
    (ds.header.aborts || []).forEach((a, i) => lines.push(`abort ${i + 1}: run ${a.run} ${a.status}` +
      `${a.error ? ` (${a.error})` : ""} at ${when(a.ended)} after ${a.points} of ${a.total} points; ` +
      `continued ${when(a.continued)} (wait ${a.wait || 0} s) as run ${a.continued_as}`));
    lines.push("", JSON.stringify(toJSONable(ds.header.definition || {}), null, 2));
    this.details.textContent = lines.join("\n");
  }

  async _ppt() {
    const f = this.ds && this.ds.header.file;
    try {
      const p = await this.conn.request("make_ppt", this.live || !f ? {} : { path: f }, { timeout: 300000 });
      this.app.message("info", `slide added to ${p}`);
    } catch (e) { this.app.message("error", "PowerPoint: " + e.message); }
  }
}

export class LiveDataView extends DataView {
  constructor(app) {
    super(app, { live: true });
    this.run = null;
    this.conn.on("connected", () => this.resync());
    this.conn.on("event", (k, d) => this._event(k, d));
  }

  async resync() {
    try {
      const snap = await this.conn.request("run_snapshot", {}, { timeout: 300000 });
      if (!snap) return;
      const ds = DataSet.fromSnapshot(snap);
      this.run = ds.header.run;
      if (snap.running) ds.header.status = "running";
      this.setDataset(ds);
      this.dataChanged();
    } catch (e) { this.title.textContent = "cannot load the live data: " + e.message; }
  }

  _event(kind, data) {
    if (kind !== "resync" && (!data || typeof data !== "object")) return;
    try {
      if (kind === "sweep_start") {
        const hdr = data.header;
        hdr.file = data.path;
        this.run = data.run;
        this.setDataset(new DataSet(hdr));
        // a continued run: its data so far come from the backend
        if (data.progress && data.progress.done) this.resync();
      } else if (kind === "sweep_point") {
        if (!this.ds || data.run !== this.run) { this.resync(); return; }
        this.ds.applyPoint(data.k, data.i, data.v || {}, data.e, !!data.skip);
        this.dataChanged();
      } else if (kind === "sweep_phase" && this.ds && data.run === this.run) {
        this.ds.applyPhase(data.phase, data.v);
        if (data.phase === "axes") this.dataChanged();
      } else if (kind === "sweep_end" && this.ds && data.run === this.run) {
        this.ds.footer = data.footer || { status: data.status };
        this.ds.header.status = data.status;
        this.ds.version++;
        this._updateHeader();
        this._updateDetails();
        this.dataChanged();
      } else if (kind === "resync") this.resync();
    } catch (e) {
      this.title.textContent = `live data error: ${e.message} (resyncing)`;
      this.resync();
    }
  }
}

export class FileDataView extends DataView {
  constructor(app, path) {
    super(app);
    this.path = path;
    this.bar.prepend(btn("Reload", () => this.load(), { cmd: "load_run" }),
      h("a", { class: "button", href: "/api/file?path=" + encodeURIComponent(path), text: "⬇ Download", title: "download the data file", download: "" }));
    this.load();
  }

  async load() {
    this.title.textContent = `loading ${this.path} …`;
    try {
      const snap = await this.conn.request("load_run", { path: this.path }, { timeout: 300000 });
      const ds = DataSet.fromSnapshot(snap);
      ds.header.file = ds.header.file || this.path;
      this.setDataset(ds);
      this.dataChanged();
    } catch (e) { this.title.textContent = `cannot load ${this.path}: ${e.message}`; }
  }
}

export function fmtSize(b) { return b > 1e6 ? `${fmt(b / 1e6, 3)} MB` : `${fmt(b / 1024, 3)} kB`; }
