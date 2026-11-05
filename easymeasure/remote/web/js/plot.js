// Canvas plotting: curves, heat maps (draggable/zoomable, adjustable colour
// levels) and the N-D plot widget with axis selection and slice sliders.
//
// Redraws are coalesced (one per animation frame, ~15 fps max) and skipped while
// a plot is hidden, so fast sweeps never make the page sluggish.

import { h, fmt } from "./ui.js";
import * as cmaps from "./colormaps.js";
import { axesFor, defaultView, extract, latestIndices } from "./dataset.js";

const MODES = ["Auto", "Curve", "Heatmap", "Stacked curves"];
const MAX_STACKED = 150;
const SERIES = ["#4cc9f0", "#f72585", "#80ed99", "#ffb703", "#b388eb", "#ff7b54"];

function css(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

export function niceTicks(lo, hi, n = 6) {
  if (!(hi > lo) || !Number.isFinite(lo) || !Number.isFinite(hi)) return [];
  const span = hi - lo;
  const raw = span / Math.max(1, n);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const r = raw / mag;
  const step = (r < 1.5 ? 1 : r < 3.5 ? 2 : r < 7.5 ? 5 : 10) * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
    out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    if (out.length > 50) break;
  }
  return out;
}

function tickLabel(v, step) {
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e5 || a < 1e-3) return v.toExponential(Math.max(0, Math.min(4, Math.ceil(-Math.log10(step / a)) ))).replace("e+", "e");
  const d = Math.max(0, Math.min(8, -Math.floor(Math.log10(step)) + (step / Math.pow(10, Math.floor(Math.log10(step))) < 2 ? 1 : 0)));
  return v.toFixed(d);
}

function finiteRange(arr) {
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < arr.length; i++) {
    const v = arr[i];
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  return lo <= hi ? [lo, hi] : null;
}

function edges(c) {
  if (!c || c.length === 0) return [0, 1];
  if (c.length === 1) return [c[0] - 0.5, c[0] + 0.5];
  let d = (c[c.length - 1] - c[0]) / (c.length - 1);
  if (!d || !Number.isFinite(d)) d = 1;
  return [c[0] - d / 2, c[c.length - 1] + d / 2];
}

function nearest(coords, v) {
  let best = 0, bd = Infinity;
  for (let i = 0; i < coords.length; i++) {
    const d = Math.abs(coords[i] - v);
    if (d < bd) { bd = d; best = i; }
  }
  return best;
}

// ------------------------------------------------------------------------------
export class PlotCanvas {
  constructor({ compact = false, onHover = null, onLevels = null } = {}) {
    this.compact = compact;
    this.canvas = h("canvas", { class: "plot-canvas" });
    this.el = h("div", { class: "plot-host" }, this.canvas);
    this.content = null;          // {kind: "curves"|"image", ...}
    this.view = null;             // [x0, x1, y0, y1] when zoomed / panned
    this.levels = [0, 1];
    this.cmap = "viridis";
    this.xlabel = "";
    this.ylabel = "";
    this.message = "";
    this.onHover = onHover;
    this.onLevels = onLevels;
    this._img = null;
    this._imgKey = null;
    this._pending = false;
    new ResizeObserver(() => this.draw()).observe(this.el);
    this._bind();
  }

  // layout ------------------------------------------------------------------
  _rect() {
    const w = this.canvas.clientWidth, hh = this.canvas.clientHeight;
    const cb = this.content && this.content.kind === "image" && !this.compact ? 64 : 0;
    const L = this.compact ? 40 : 62, B = this.compact ? 18 : 38, T = 8, R = 10 + cb;
    return { L, T, W: Math.max(10, w - L - R), H: Math.max(10, hh - T - B), w, h: hh, cb };
  }

  bounds() {
    const c = this.content;
    if (!c) return [0, 1, 0, 1];
    if (c.kind === "image") {
      const [x0, x1] = edges(c.x), [y0, y1] = edges(c.y);
      return [Math.min(x0, x1), Math.max(x0, x1), Math.min(y0, y1), Math.max(y0, y1)];
    }
    let xr = null, yr = null;
    for (const s of c.series) {
      const a = finiteRange(s.x), b = finiteRange(s.y);
      if (a) xr = xr ? [Math.min(xr[0], a[0]), Math.max(xr[1], a[1])] : a;
      if (b) yr = yr ? [Math.min(yr[0], b[0]), Math.max(yr[1], b[1])] : b;
    }
    xr = xr || [0, 1];
    yr = yr || [0, 1];
    if (xr[0] === xr[1]) xr = [xr[0] - 0.5, xr[1] + 0.5];
    if (yr[0] === yr[1]) { const d = Math.abs(yr[0]) * 0.05 || 0.5; yr = [yr[0] - d, yr[1] + d]; }
    const py = (yr[1] - yr[0]) * 0.05;
    return [xr[0], xr[1], yr[0] - py, yr[1] + py];
  }

  setContent(c) { this.content = c; this._imgKey = null; this.draw(); }
  resetView() { this.view = null; this.draw(); }

  // drawing ------------------------------------------------------------------
  draw() {
    if (this._pending) return;
    this._pending = true;
    requestAnimationFrame(() => { this._pending = false; this._draw(); });
  }

  _draw() {
    const cv = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, hh = cv.clientHeight;
    if (!w || !hh) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(hh * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(hh * dpr);
    }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const fg = css("--plot-fg", "#ccc"), grid = css("--plot-grid", "rgba(128,128,128,.25)");
    const bg = css("--plot-bg", "#111");
    const font = `${this.compact ? 10 : 11}px ${css("--font-ui", "sans-serif")}`;
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, hh);
    const r = this._rect();
    this._r = r;
    const [x0, x1, y0, y1] = this.view || this.bounds();
    this._vb = [x0, x1, y0, y1];
    const px = (x) => r.L + (x - x0) / (x1 - x0) * r.W;
    const py = (y) => r.T + r.H - (y - y0) / (y1 - y0) * r.H;
    ctx.font = font;
    ctx.lineWidth = 1;
    // grid + ticks
    const xt = niceTicks(x0, x1, Math.max(2, Math.floor(r.W / (this.compact ? 70 : 90))));
    const yt = niceTicks(y0, y1, Math.max(3, Math.floor(r.H / (this.compact ? 22 : 45))));
    const xs = xt.length > 1 ? xt[1] - xt[0] : 1, ys = yt.length > 1 ? yt[1] - yt[0] : 1;
    ctx.strokeStyle = grid;
    ctx.beginPath();
    for (const t of xt) { const X = Math.round(px(t)) + 0.5; ctx.moveTo(X, r.T); ctx.lineTo(X, r.T + r.H); }
    for (const t of yt) { const Y = Math.round(py(t)) + 0.5; ctx.moveTo(r.L, Y); ctx.lineTo(r.L + r.W, Y); }
    ctx.stroke();
    // content
    ctx.save();
    ctx.beginPath();
    ctx.rect(r.L, r.T, r.W, r.H);
    ctx.clip();
    if (this.content && this.content.kind === "image") this._drawImage(ctx, px, py);
    else if (this.content) this._drawCurves(ctx, px, py);
    ctx.restore();
    // frame + labels
    ctx.strokeStyle = fg;
    ctx.globalAlpha = 0.6;
    ctx.strokeRect(r.L + 0.5, r.T + 0.5, r.W, r.H);
    ctx.globalAlpha = 1;
    ctx.fillStyle = fg;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const t of xt) ctx.fillText(tickLabel(t, xs), px(t), r.T + r.H + 3);
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const t of yt) ctx.fillText(tickLabel(t, ys), r.L - 4, py(t));
    if (!this.compact) {
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillText(this.xlabel, r.L + r.W / 2, hh - 1);
      ctx.save();
      ctx.translate(11, r.T + r.H / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textBaseline = "middle";
      ctx.fillText(this.ylabel, 0, 0);
      ctx.restore();
    }
    if (r.cb) this._drawColorbar(ctx, r, fg);
    if (this.message) {
      ctx.fillStyle = fg;
      ctx.globalAlpha = 0.7;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(this.message, r.L + r.W / 2, r.T + r.H / 2);
      ctx.globalAlpha = 1;
    }
  }

  _drawCurves(ctx, px, py) {
    const c = this.content;
    for (const s of c.series) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.width || 1.4;
      ctx.beginPath();
      let pen = false;
      const n = Math.min(s.x.length, s.y.length);
      for (let i = 0; i < n; i++) {
        const X = px(s.x[i]), Y = py(s.y[i]);
        if (!Number.isFinite(X) || !Number.isFinite(Y)) { pen = false; continue; }
        if (pen) ctx.lineTo(X, Y); else { ctx.moveTo(X, Y); pen = true; }
      }
      ctx.stroke();
      if (n <= 60 && !this.compact) {
        ctx.fillStyle = s.color;
        for (let i = 0; i < n; i++) {
          const X = px(s.x[i]), Y = py(s.y[i]);
          if (Number.isFinite(X) && Number.isFinite(Y)) ctx.fillRect(X - 2, Y - 2, 4, 4);
        }
      }
    }
  }

  _image() {
    const c = this.content;
    const key = `${c.version}|${this.levels[0]}|${this.levels[1]}|${this.cmap}`;
    if (this._img && this._imgKey === key) return this._img;
    const { nx, ny, z } = c;
    const off = this._img && this._img.width === nx && this._img.height === ny
      ? this._img : Object.assign(document.createElement("canvas"), { width: nx, height: ny });
    const octx = off.getContext("2d");
    const im = octx.createImageData(nx, ny);
    const d = im.data, l = cmaps.lut(this.cmap);
    const [lo, hi] = this.levels;
    const sc = hi > lo ? 255 / (hi - lo) : 0;
    for (let j = 0; j < ny; j++) {
      const row = (ny - 1 - j) * nx;
      for (let i = 0; i < nx; i++) {
        const v = z[j * nx + i];
        const o = (row + i) * 4;
        if (v !== v) { d[o + 3] = 0; continue; }       // NaN: transparent
        let t = Math.round((v - lo) * sc);
        t = t < 0 ? 0 : t > 255 ? 255 : t;
        d[o] = l[t * 3]; d[o + 1] = l[t * 3 + 1]; d[o + 2] = l[t * 3 + 2]; d[o + 3] = 255;
      }
    }
    octx.putImageData(im, 0, 0);
    this._img = off;
    this._imgKey = key;
    return off;
  }

  _drawImage(ctx, px, py) {
    const c = this.content;
    const img = this._image();
    const [xe0, xe1] = edges(c.x), [ye0, ye1] = edges(c.y);
    const a = (px(xe1) - px(xe0)) / c.nx, e = px(xe0);
    const d = (py(ye0) - py(ye1)) / c.ny, f = py(ye1);
    const dpr = window.devicePixelRatio || 1;
    ctx.save();
    ctx.imageSmoothingEnabled = false;
    ctx.setTransform(a * dpr, 0, 0, d * dpr, e * dpr, f * dpr);
    ctx.drawImage(img, 0, 0);
    ctx.restore();
  }

  _drawColorbar(ctx, r, fg) {
    const x = r.L + r.W + 12, w = 14, top = r.T, hgt = r.H;
    const l = cmaps.lut(this.cmap);
    for (let i = 0; i < hgt; i++) {
      const t = Math.round((1 - i / Math.max(1, hgt - 1)) * 255);
      ctx.fillStyle = `rgb(${l[t * 3]},${l[t * 3 + 1]},${l[t * 3 + 2]})`;
      ctx.fillRect(x, top + i, w, 1);
    }
    ctx.strokeStyle = fg;
    ctx.globalAlpha = 0.6;
    ctx.strokeRect(x + 0.5, top + 0.5, w, hgt);
    ctx.globalAlpha = 1;
    const [lo, hi] = this.levels;
    const ticks = niceTicks(lo, hi, Math.max(2, Math.floor(hgt / 40)));
    const st = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
    ctx.fillStyle = fg;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    for (const t of ticks) {
      const Y = top + hgt - (t - lo) / (hi - lo) * hgt;
      ctx.fillRect(x + w, Y, 3, 1);
      ctx.fillText(tickLabel(t, st), x + w + 5, Y);
    }
    this._cbRect = { x, w: w + 50, top, h: hgt };
  }

  // interaction ---------------------------------------------------------------
  _toData(ev) {
    const b = this.canvas.getBoundingClientRect();
    const X = ev.clientX - b.left, Y = ev.clientY - b.top;
    const r = this._r, [x0, x1, y0, y1] = this._vb || [0, 1, 0, 1];
    if (!r) return null;
    return { X, Y, inPlot: X >= r.L && X <= r.L + r.W && Y >= r.T && Y <= r.T + r.H,
             inBar: !!(this._cbRect && r.cb && X >= this._cbRect.x - 4 && X <= this._cbRect.x + this._cbRect.w && Y >= r.T && Y <= r.T + r.H),
             x: x0 + (X - r.L) / r.W * (x1 - x0), y: y0 + (r.T + r.H - Y) / r.H * (y1 - y0) };
  }

  _bind() {
    const cv = this.canvas;
    cv.addEventListener("wheel", (ev) => {
      const p = this._toData(ev);
      if (!p) return;
      ev.preventDefault();
      const k = Math.exp(Math.max(-0.5, Math.min(0.5, ev.deltaY * 0.0015)));
      if (p.inBar) {
        const [lo, hi] = this.levels, mid = (lo + hi) / 2;
        this._setLevels([mid - (mid - lo) * k, mid + (hi - mid) * k]);
        return;
      }
      const [x0, x1, y0, y1] = this._vb;
      const zx = ev.altKey || ev.ctrlKey ? 1 : k, zy = ev.shiftKey ? 1 : k;
      this.view = [p.x - (p.x - x0) * zx, p.x + (x1 - p.x) * zx, p.y - (p.y - y0) * zy, p.y + (y1 - p.y) * zy];
      this.draw();
    }, { passive: false });
    cv.addEventListener("dblclick", (ev) => {
      const p = this._toData(ev);
      if (p && p.inBar && this.onLevels) this.onLevels(null);     // back to auto levels
      else this.resetView();
    });
    cv.addEventListener("pointerdown", (ev) => {
      const p = this._toData(ev);
      if (!p || ev.button !== 0) return;
      cv.setPointerCapture(ev.pointerId);
      const start = p, vb = this._vb.slice(), lv = this.levels.slice();
      const r = this._r;
      const box = ev.shiftKey && p.inPlot;
      const which = p.inBar ? (p.Y < r.T + r.H * 0.4 ? "hi" : p.Y > r.T + r.H * 0.6 ? "lo" : "both") : null;
      const move = (e) => {
        const q = this._toData(e);
        if (!q) return;
        if (which) {
          const dv = -(q.Y - start.Y) / r.H * (lv[1] - lv[0]);
          if (which === "hi") this._setLevels([lv[0], Math.max(lv[0] + 1e-300, lv[1] + dv)]);
          else if (which === "lo") this._setLevels([Math.min(lv[1] - 1e-300, lv[0] + dv), lv[1]]);
          else this._setLevels([lv[0] + dv, lv[1] + dv]);
        } else if (box) {
          this._box = [start.X, start.Y, q.X, q.Y];
          this.draw();
          requestAnimationFrame(() => this._drawBox());
        } else if (p.inPlot) {
          const dx = (q.X - start.X) / r.W * (vb[1] - vb[0]);
          const dy = (q.Y - start.Y) / r.H * (vb[3] - vb[2]);
          this.view = [vb[0] - dx, vb[1] - dx, vb[2] + dy, vb[3] + dy];
          this.draw();
        }
      };
      const up = (e) => {
        cv.removeEventListener("pointermove", move);
        cv.removeEventListener("pointerup", up);
        if (box && this._box) {
          const q = this._toData(e);
          if (q && Math.abs(q.X - start.X) > 4 && Math.abs(q.Y - start.Y) > 4) {
            this.view = [Math.min(start.x, q.x), Math.max(start.x, q.x), Math.min(start.y, q.y), Math.max(start.y, q.y)];
          }
          this._box = null;
          this.draw();
        }
      };
      cv.addEventListener("pointermove", move);
      cv.addEventListener("pointerup", up);
    });
    cv.addEventListener("pointermove", (ev) => {
      if (ev.buttons) return;
      const p = this._toData(ev);
      if (p && p.inPlot && this.onHover) this.onHover(p.x, p.y);
      cv.style.cursor = p && p.inBar ? "ns-resize" : "crosshair";
    });
  }

  _drawBox() {
    if (!this._box) return;
    const ctx = this.canvas.getContext("2d");
    const [a, b, c, d] = this._box;
    ctx.strokeStyle = css("--accent", "#4cc9f0");
    ctx.setLineDash([4, 3]);
    ctx.strokeRect(Math.min(a, c), Math.min(b, d), Math.abs(c - a), Math.abs(d - b));
    ctx.setLineDash([]);
  }

  _setLevels(lv) {
    if (!(lv[1] > lv[0])) return;
    this.levels = lv;
    if (this.onLevels) this.onLevels(lv);
    this.draw();
  }
}

// ------------------------------------------------------------------------------
/** The N-D plot widget (port of NDPlotWidget). */
export class NDPlot {
  constructor({ closable = false, onClose = null } = {}) {
    this.ds = null;
    this.channel = null;
    this.axes = [];
    this.fixed = {};
    this.follow = true;
    this.autoLevels = true;
    this._seen = -1;
    this._building = false;
    this.channelBox = h("select", { title: "recorded channel to show",
      on: { change: () => this._channelChanged(this.channelBox.value) } });
    this.modeBox = h("select", { title: "Auto: heat map for 2 axes, curve for 1",
      on: { change: () => this._layoutChanged() } }, MODES.map((m) => h("option", { text: m, value: m })));
    this.xBox = h("select", { on: { change: () => this._layoutChanged() } });
    this.yBox = h("select", { on: { change: () => this._layoutChanged() } });
    this.cmapBox = h("select", { on: { change: () => { this.pc.cmap = this.cmapBox.value; this.pc.draw(); } } },
      cmaps.NAMES.map((n) => h("option", { text: n, value: n })));
    this.offset = h("input", { type: "number", step: "any", value: "0", title: "vertical offset between stacked curves (0 = automatic)",
      on: { input: () => this.markDirty(true) } });
    this.followBox = h("input", { type: "checkbox", checked: true,
      on: { change: () => { this.follow = this.followBox.checked; this.markDirty(true); } } });
    this.autoBox = h("input", { type: "checkbox", checked: true,
      on: { change: () => { this.autoLevels = this.autoBox.checked; this.markDirty(true); } } });
    this.resetBtn = h("button", { text: "⤢", title: "reset zoom (or double-click the plot)", on: { click: () => this.pc.resetView() } });
    this.optsPop = h("div", { class: "popover", hidden: true },
      h("label", {}, "Colormap ", this.cmapBox),
      h("label", {}, "Stack offset ", this.offset),
      h("label", { class: "check" }, this.followBox, " follow latest"),
      h("label", { class: "check" }, this.autoBox, " auto levels"),
      h("div", { class: "help", text: "Wheel: zoom (Shift: Y only, Ctrl: X only) · drag: pan · Shift+drag: box zoom · double-click: reset. Colour bar: drag ends or middle, wheel to widen, double-click for auto levels." }));
    this.optsBtn = h("button", { text: "⚙", title: "colormap, levels, follow, stacked-curve offset",
      on: { click: (ev) => { ev.stopPropagation(); this.optsPop.hidden = !this.optsPop.hidden; } } });
    document.addEventListener("click", (ev) => { if (!this.optsPop.contains(ev.target)) this.optsPop.hidden = true; });
    this.yLabel = h("span", { text: "Y" });
    const top = h("div", { class: "plot-bar" }, this.channelBox, this.modeBox,
      h("span", { class: "lbl" }, "X ", this.xBox), h("span", { class: "lbl" }, this.yLabel, " ", this.yBox),
      this.resetBtn, h("span", { class: "pop-host" }, this.optsBtn, this.optsPop),
      closable ? h("button", { text: "✕", title: "remove this plot", on: { click: () => onClose && onClose(this) } }) : null);
    this.pc = new PlotCanvas({
      onHover: (x, y) => this._hover(x, y),
      onLevels: (lv) => {
        if (lv === null) { this.autoLevels = true; this.autoBox.checked = true; this.markDirty(true); }
        else { this.autoLevels = false; this.autoBox.checked = false; }
      },
    });
    this.sliders = h("div", { class: "sliders" });
    this.readout = h("span", { class: "readout", text: " " });
    this.info = h("span", { class: "info", text: " " });
    this.el = h("div", { class: "ndplot" }, top, this.pc.el, this.sliders,
      h("div", { class: "plot-foot" }, this.readout, this.info));
    this._timer = setInterval(() => this._tick(), 70);
  }

  destroy() { clearInterval(this._timer); this.el.remove(); }

  setDataset(ds, channel = null) {
    this.ds = ds;
    this.autoLevels = this.autoBox.checked;
    const names = this._plottable();
    this._building = true;
    this.channelBox.replaceChildren(...names.map((n) => h("option", { text: n, value: n })));
    this._building = false;
    if (channel && names.includes(channel)) this.channelBox.value = channel;
    this._channelChanged(this.channelBox.value);
  }

  refreshChannelList() {
    if (!this.ds) return;
    const names = this._plottable();
    const cur = [...this.channelBox.options].map((o) => o.value);
    if (names.join("\n") !== cur.join("\n")) {
      const keep = this.channelBox.value;
      this.channelBox.replaceChildren(...names.map((n) => h("option", { text: n, value: n })));
      if (names.includes(keep)) this.channelBox.value = keep;
      else this._channelChanged(this.channelBox.value);
    }
  }

  markDirty(force = false) { this._dirty = true; if (force) this._seen = -1; }

  _plottable() {
    if (!this.ds) return [];
    return this.ds.recordNames().filter((n) => !(this.ds.data[n] && this.ds.data[n].dtype === "O"));
  }

  _channelChanged(name) {
    if (this._building) return;
    this.channel = name || null;
    this._rebuildAxes();
  }

  _rebuildAxes() {
    const ds = this.ds;
    this.axes = [];
    if (ds && this.channel && ds.data[this.channel]) this.axes = axesFor(ds, this.channel);
    else if (ds && this.channel) {
      const k = ds.dimOf[this.channel] ?? 0;
      for (let d = k; d < ds.ndim; d++) this.axes.push({ id: "d" + d, label: ds.sweepAxis(d)[0], size: ds.shape[d], kind: "sweep" });
    }
    this._axesSig = this.axes.map((a) => a.id + a.size).join();
    this.xBox.replaceChildren(...this.axes.map((a) => h("option", { text: a.label, value: a.id })));
    const two = this.axes.length >= 2;
    this.yBox.replaceChildren(...(two ? [h("option", { text: "(none)", value: "" })] : []),
      ...(two ? this.axes.map((a) => h("option", { text: a.label, value: a.id })) : []));
    if (this.axes.length && ds && ds.data[this.channel]) {
      const [x, y] = defaultView(ds, this.channel);
      this.xBox.value = x;
      this.yBox.value = y || "";
    } else if (two) {
      this.xBox.value = this.axes[0].id;
      this.yBox.value = this.axes[1].id;
    }
    this.autoLevels = this.autoBox.checked;
    this.pc.view = null;
    this._layoutChanged();
  }

  _selected() {
    let x = this.xBox.value || null, y = this.yBox.value || null;
    let mode = this.modeBox.value;
    if (mode === "Curve" || y === x) y = null;
    if (mode === "Auto") mode = y ? "Heatmap" : "Curve";
    else if ((mode === "Heatmap" || mode === "Stacked curves") && !y) mode = "Curve";
    return [x, y, mode];
  }

  _layoutChanged() {
    const [x, y] = this._selected();
    const two = this.axes.length >= 2;
    this.yBox.disabled = !two;                   // 1-D data: Y is grayed out
    this.yLabel.classList.toggle("muted", !two);
    this.sliders.replaceChildren();
    this._sliderEls = {};
    for (const a of this.axes) {
      if (a.id === x || a.id === y || a.size <= 1) continue;
      const val = h("span", { class: "sval" });
      const s = h("input", { type: "range", min: 0, max: a.size - 1, step: 1, value: this.fixed[a.id] || 0,
        on: { input: () => { this.follow = false; this.followBox.checked = false; this.fixed[a.id] = Number(s.value); this.markDirty(true); } } });
      this._sliderEls[a.id] = { s, val, a };
      this.sliders.append(h("div", { class: "slider-row" }, h("span", { class: "slbl", text: a.label }), s, val));
    }
    this.pc.view = null;
    this.markDirty(true);
  }

  _visible() { return this.el.isConnected && this.el.offsetParent !== null; }

  _tick() {
    if (!this._dirty && (!this.ds || this.ds.version === this._seen)) return;
    if (!this._visible()) return;
    this._dirty = false;
    this._render();
  }

  _render() {
    const ds = this.ds;
    this._seen = ds ? ds.version : -1;
    const pc = this.pc;
    if (!ds || !this.channel) { pc.message = ds ? "no recorded channels" : "no data"; pc.setContent(null); return; }
    if (!ds.data[this.channel]) { pc.message = "waiting for data…"; pc.setContent(null); this.info.textContent = ""; return; }
    // axes may change when an array channel delivers its first value
    const sig = axesFor(ds, this.channel).map((a) => a.id + a.size).join();
    if (sig !== this._axesSig) { this._rebuildAxes(); }
    const [x, y, mode] = this._selected();
    if (!x) return;
    if (this.follow) Object.assign(this.fixed, latestIndices(ds, this.channel));
    for (const { s, val, a } of Object.values(this._sliderEls || {})) {
      const i = Math.min(this.fixed[a.id] || 0, a.size - 1);
      s.value = i;
      const c = a.coords ? a.coords[i] : i;
      val.textContent = `${i + 1}/${a.size}  (${fmt(c, 5)})`;
    }
    let ex;
    try { ex = extract(ds, this.channel, x, mode === "Curve" ? null : y, this.fixed); } catch (e) {
      pc.message = String(e.message); pc.setContent(null); return;
    }
    pc.message = "";
    const ax = this.axes.find((a) => a.id === x), ay = this.axes.find((a) => a.id === y);
    const unit = (ds.header.units || {})[this.channel] || "";
    const zl = this.channel + (unit ? ` (${unit})` : "");
    this._ex = { ex, mode };
    if (mode === "Heatmap") {
      if (this.autoLevels) {
        const r = finiteRange(ex.z);
        if (r) pc.levels = r[0] === r[1] ? [r[0] - 0.5, r[1] + 0.5] : r;
      }
      pc.cmap = this.cmapBox.value;
      pc.xlabel = ax ? ax.label : "";
      pc.ylabel = ay ? ay.label : "";
      pc.setContent({ kind: "image", x: ex.x, y: ex.y, z: ex.z, nx: ex.nx, ny: ex.ny, version: ds.version + ":" + JSON.stringify(this.fixed) + x + y });
      this.info.textContent = `${ex.nx}×${ex.ny}  levels ${fmt(pc.levels[0], 4)} … ${fmt(pc.levels[1], 4)}`;
    } else if (mode === "Stacked curves") {
      const n = Math.min(ex.ny, MAX_STACKED);
      let off = Number(this.offset.value) || 0;
      if (!off) { const r = finiteRange(ex.z); off = r ? (r[1] - r[0]) * 0.1 || 1 : 1; }
      const series = [];
      for (let j = 0; j < n; j++) {
        const row = ex.z.subarray(j * ex.nx, (j + 1) * ex.nx);
        const yv = new Float64Array(row.length);
        for (let i = 0; i < row.length; i++) yv[i] = row[i] + j * off;
        series.push({ x: ex.x, y: yv, color: cmaps.color(this.cmapBox.value, n > 1 ? 0.05 + 0.9 * j / (n - 1) : 0.5), width: 1 });
      }
      pc.xlabel = ax ? ax.label : "";
      pc.ylabel = zl + " (stacked)";
      pc.setContent({ kind: "curves", series });
      this.info.textContent = `${n} curves${ex.ny > n ? ` (of ${ex.ny})` : ""}, offset ${fmt(off, 3)}`;
    } else {
      pc.xlabel = ax ? ax.label : "";
      pc.ylabel = zl;
      pc.setContent({ kind: "curves", series: [{ x: ex.x, y: ex.z, color: css("--accent", SERIES[0]) }] });
      const fin = ex.z.filter(Number.isFinite).length;
      this.info.textContent = `${fin}/${ex.nx} points`;
    }
  }

  _hover(xv, yv) {
    if (!this._ex) return;
    const { ex, mode } = this._ex;
    const i = nearest(ex.x, xv);
    if (mode === "Heatmap") {
      const j = nearest(ex.y, yv);
      this.readout.textContent = `x = ${fmt(ex.x[i], 6)}   y = ${fmt(ex.y[j], 6)}   ${this.channel} = ${fmt(ex.z[j * ex.nx + i], 7)}`;
    } else if (mode === "Curve") {
      this.readout.textContent = `x = ${fmt(ex.x[i], 6)}   ${this.channel} = ${fmt(ex.z[i], 7)}`;
    } else {
      this.readout.textContent = `x = ${fmt(xv, 6)}   y = ${fmt(yv, 6)}`;
    }
  }
}
