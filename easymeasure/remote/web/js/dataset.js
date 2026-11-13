// N-dimensional sweep data: port of DataSet (backend/storage.py) and ndview.py.
//
// data[name] for a channel recorded in dimension k has shape
// (N_k, ..., N_{D-1}) + value_shape (dim 0 = innermost / fastest).

import { NDArray } from "./codec.js";

const prod = (a) => a.reduce((x, y) => x * y, 1);

function flatIndex(idx, shape) {
  let f = 0;
  for (let j = 0; j < idx.length; j++) f = f * shape[j] + idx[j];
  return f;
}

function asArray(v) {
  if (v instanceof NDArray) return v;
  return NDArray.from(v);
}

export class DataSet {
  /** a sweep_phase event / journal phase frame: pre or post recordings, or the
   *  coordinates of value axes ("axes") */
  applyPhase(phase, v) {
    if (phase === "axes") Object.assign(this.valueAxes, v || {});
    else Object.assign(phase === "pre" ? this.pre : this.post, v || {});
    this.version++;
  }

  constructor(header) {
    this.header = header;
    this.shape = (header.shape || []).map(Number);
    this.dims = header.dims || [];
    this.data = {};
    this.dimOf = {};
    this.filled = {};
    this.errors = {};
    this.pre = {};
    this.post = {};
    this.valueAxes = {};           // channel -> [{name, unit, label, coords} | null per value axis]
    this.footer = null;
    this.points = 0;
    this.lastIndex = null;
    this.lastByDim = {};
    this.version = 0;              // bumped on every change (plots redraw lazily)
    this.dims.forEach((d, k) => {
      this.filled[k] = new Uint8Array(prod(this.shape.slice(k)));
      for (const n of d.record || []) this.dimOf[n] = k;
    });
    this.setpoints = {};
    for (const [k, sp] of Object.entries(header.setpoints || {})) {
      this.setpoints[Number(k)] = {};
      for (const [n, v] of Object.entries(sp)) this.setpoints[Number(k)][n] = asArray(v);
    }
  }

  get ndim() { return this.shape.length; }
  get totalPoints() { return this.shape.length ? prod(this.shape) : 0; }
  get status() {
    if (this.footer) return this.footer.status || "finished";
    return this.header.status || "incomplete";
  }
  recordNames() { return Object.keys(this.dimOf); }

  sweepAxis(k) {
    const sp = this.setpoints[k] || {};
    for (const [name, a] of Object.entries(sp)) {
      if (a.shape.length === 1 && a.dtype !== "O") return [name, a.data];
    }
    const n = this.shape[k];
    return [`index ${k}`, Float64Array.from({ length: n }, (_, i) => i)];
  }

  static fromSnapshot(snap) {
    const ds = new DataSet(snap.header);
    for (const [k, v] of Object.entries(snap.data || {})) ds.data[k] = asArray(v);
    for (const [k, v] of Object.entries(snap.filled || {})) {
      const a = asArray(v);
      ds.filled[Number(k)] = Uint8Array.from(a.data, (x) => (x ? 1 : 0));
    }
    ds.errors = { ...(snap.errors || {}) };
    ds.pre = { ...(snap.pre || {}) };
    ds.post = { ...(snap.post || {}) };
    ds.valueAxes = { ...(snap.value_axes || {}) };
    ds.footer = snap.footer || null;
    ds.points = Number(snap.points || 0);
    ds.skipped = Number(snap.skipped || 0);
    const li = snap.last_index;
    ds.lastIndex = li ? [Number(li[0]), li[1].map(Number)] : null;
    for (const [k, idx] of Object.entries(snap.last_by_dim || {})) ds.lastByDim[Number(k)] = idx.map(Number);
    ds.version++;
    return ds;
  }

  _alloc(name, k, value) {
    const base = this.shape.slice(k);
    let arr;
    if (typeof value === "string") arr = NDArray.full(base, null, "O");
    else arr = NDArray.full(base.concat(asArray(value).shape), NaN);
    this.data[name] = arr;
    return arr;
  }

  applyPoint(k, index, values, errors, skip = false) {
    const idx = index.map(Number);
    if (idx.length !== this.ndim - k) throw new Error(`bad index length for dimension ${k}`);
    const base = this.shape.slice(k);
    idx.forEach((i, j) => { if (!(i >= 0 && i < base[j])) throw new Error(`index ${idx} out of range`); });
    const fi = flatIndex(idx, base);
    if (skip) { this._skip(k, idx, fi); return; }
    for (const [name, val0] of Object.entries(values || {})) {
      let val = val0;
      if (val === null || val === undefined) continue;
      let arr = this.data[name] || this._alloc(name, k, val);
      if (arr.dtype === "O") { arr.data[fi] = String(val); continue; }
      if (typeof val === "string") { const f = parseFloat(val); val = Number.isFinite(f) ? f : NaN; }
      const v = asArray(val);
      let tshape = arr.shape.slice(idx.length);
      if (v.ndim === tshape.length && v.ndim > 0 && v.shape.some((a, j) => a > tshape[j])) {
        // the instrument returns more data than before: grow, pad older points with NaN
        const nv = v.shape.map((a, j) => Math.max(a, tshape[j]));
        const grown = NDArray.full(base.concat(nv), NaN);
        const oldBlock = prod(tshape), newBlock = prod(nv);
        const nOuter = prod(base);
        for (let o = 0; o < nOuter; o++) copyBlock(arr.data, o * oldBlock, tshape, grown.data, o * newBlock, nv);
        arr = this.data[name] = grown;
        tshape = nv;
      }
      const block = prod(tshape);
      const off = fi * block;
      if (v.shape.length === tshape.length && v.shape.every((a, j) => a === tshape[j])) {
        arr.data.set(v.data, off);
      } else {
        arr.data.fill(NaN, off, off + block);
        if (v.ndim === tshape.length) copyBlock(v.data, 0, v.shape, arr.data, off, tshape);
        else if (v.size === 1) arr.data.fill(v.data[0], off, off + block);
      }
    }
    for (const name of Object.keys(errors || {})) this.errors[name] = (this.errors[name] || 0) + 1;
    if (k === 0 && !this.filled[0][fi]) this.points++;   // applied twice (a resync): once
    this.filled[k][fi] = 1;
    this.lastIndex = [k, idx];
    this.lastByDim[k] = idx;
    this.version++;
  }

  /** A point skipped by skip_point(): data of dimension k at idx are NaN, and the inner
   *  points under it that never ran count as done (they stay NaN too). */
  _skip(k, idx, fi) {
    for (const [name, kk] of Object.entries(this.dimOf)) {
      const arr = this.data[name];
      if (kk !== k || !arr) continue;
      const block = prod(arr.shape.slice(idx.length));
      if (arr.dtype === "O") arr.data.fill(null, fi * block, (fi + 1) * block);
      else arr.data.fill(NaN, fi * block, (fi + 1) * block);
    }
    const P = prod(this.shape.slice(k));
    for (let j = 0; j <= k; j++) {
      const outer = prod(this.shape.slice(j, k));
      for (let a = 0; a < outer; a++) this.filled[j][a * P + fi] = 1;
    }
    this.points = this.filled[0].reduce((s, x) => s + x, 0);
    this.skipped = (this.skipped || 0) + 1;
    this.lastIndex = [k, idx];
    this.lastByDim[k] = idx;
    this.version++;
  }
}

// copy the overlapping part of a C-ordered block src(shape s) into dst(shape d)
function copyBlock(src, so, s, dst, doff, d) {
  const n = s.length;
  if (n === 0) { dst[doff] = src[so]; return; }
  const m = s.map((a, j) => Math.min(a, d[j]));
  const ss = strides(s), ds = strides(d);
  const idx = new Array(n).fill(0);
  const total = prod(m);
  for (let c = 0; c < total; c++) {
    let a = so, b = doff;
    for (let j = 0; j < n; j++) { a += idx[j] * ss[j]; b += idx[j] * ds[j]; }
    dst[b] = src[a];
    for (let j = n - 1; j >= 0; j--) { if (++idx[j] < m[j]) break; idx[j] = 0; }
  }
}

function strides(shape) {
  const s = new Array(shape.length);
  let acc = 1;
  for (let i = shape.length - 1; i >= 0; i--) { s[i] = acc; acc *= shape[i]; }
  return s;
}

// ---------------------------------------------------------------- ndview -----
export function axesFor(ds, name) {
  const arr = ds.data[name];
  const k = ds.dimOf[name] ?? 0;
  const out = [];
  for (let dim = k; dim < ds.ndim; dim++) {
    const [label, coords] = ds.sweepAxis(dim);
    const unit = (ds.header.units || {})[label] || "";
    out.push({ id: "d" + dim, kind: "sweep", dim, name: label, unit,
               label: label + (unit ? ` (${unit})` : ""), size: ds.shape[dim], coords });
  }
  if (arr) {
    const vshape = arr.shape.slice(ds.ndim - k);
    const given = (ds.valueAxes || {})[name] || [];
    vshape.forEach((n, j) => {
      const ax = given[j];
      const c = ax && ax.coords ? asArray(ax.coords) : null;
      if (c && c.size === n) {
        const unit = ax.unit || "";
        const label = ax.name || `${name}[axis ${j}]`;
        out.push({ id: "v" + j, kind: "value", dim: j, unit, name: label,
          label: label + (unit ? ` (${unit})` : ""), size: n, coords: Float64Array.from(c.data) });
      } else {
        out.push({ id: "v" + j, kind: "value", dim: j, unit: "",
          name: `${name}[axis ${j}]`, label: `${name}[axis ${j}]`, size: n,
          coords: Float64Array.from({ length: n }, (_, i) => i) });
      }
    });
  }
  return out;
}

export function latestIndices(ds, name) {
  const out = {};
  const k = ds.dimOf[name] ?? 0;
  let li = null;
  if (ds.lastByDim[k]) li = [k, ds.lastByDim[k]];
  else if (ds.lastIndex) li = ds.lastIndex;
  if (li) {
    const [lk, idx] = li;
    for (let dim = Math.max(k, lk); dim < ds.ndim; dim++) out["d" + dim] = idx[dim - lk];
  }
  return out;
}

/** -> {x: coords, y: coords|null, z: Float64Array (ny*nx, row = y), nx, ny} */
export function extract(ds, name, x, y, fixed = {}) {
  const arr = ds.data[name];
  if (!arr || arr.dtype === "O") throw new Error(`no numeric data for ${name}`);
  const axes = axesFor(ds, name);
  const ids = axes.map((a) => a.id);
  if (!ids.includes(x) || (y && !ids.includes(y)) || x === y) throw new Error("invalid axis selection");
  const st = arr.strides;
  let base = 0;
  axes.forEach((a, j) => {
    if (a.id !== x && a.id !== y) {
      const i = Math.min(Math.max(Number(fixed[a.id] ?? 0), 0), a.size - 1);
      base += i * st[j];
    }
  });
  const jx = ids.indexOf(x);
  const ax = axes[jx], nx = ax.size, sx = st[jx];
  if (!y) {
    const z = new Float64Array(nx);
    for (let i = 0; i < nx; i++) z[i] = arr.data[base + i * sx];
    return { x: ax.coords, y: null, z, nx, ny: 1 };
  }
  const jy = ids.indexOf(y);
  const ay = axes[jy], ny = ay.size, sy = st[jy];
  const z = new Float64Array(nx * ny);
  for (let j = 0; j < ny; j++) {
    const o = base + j * sy;
    for (let i = 0; i < nx; i++) z[j * nx + i] = arr.data[o + i * sx];
  }
  return { x: ax.coords, y: ay.coords, z, nx, ny };
}

export function defaultView(ds, name) {
  const axes = axesFor(ds, name);
  if (!axes.length) return ["d0", null];
  const vals = axes.filter((a) => a.kind === "value" && a.size > 1);
  const sweeps = axes.filter((a) => a.kind === "sweep" && a.size > 1);
  if (vals.length >= 2) return [vals[0].id, vals[1].id];
  if (vals.length === 1) return [vals[0].id, sweeps.length ? sweeps[0].id : null];
  if (sweeps.length >= 2) return [sweeps[0].id, sweeps[1].id];
  if (sweeps.length) return [sweeps[0].id, null];
  return [axes[0].id, null];
}
