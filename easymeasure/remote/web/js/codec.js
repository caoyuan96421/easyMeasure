// Wire codec - the browser side of easymeasure/common/codec.py.
//
// frame = u32 frame_len | u32 json_len | JSON {"m": message, "b": [buffer sizes]} | buffers
// Arrays travel as raw little-endian buffers referenced from the JSON.

const te = new TextEncoder();
const td = new TextDecoder();

/** N-dimensional array (C order).  Numeric data is always a Float64Array. */
export class NDArray {
  constructor(shape, data, dtype = "f8") {
    this.shape = shape;
    this.data = data;          // Float64Array (numeric) or Array (dtype "O": strings / objects)
    this.dtype = dtype;
  }
  get size() { return this.shape.reduce((a, b) => a * b, 1); }
  get ndim() { return this.shape.length; }
  get strides() {
    const s = new Array(this.shape.length);
    let acc = 1;
    for (let i = this.shape.length - 1; i >= 0; i--) { s[i] = acc; acc *= this.shape[i]; }
    return s;
  }
  static full(shape, value, dtype = "f8") {
    const n = shape.reduce((a, b) => a * b, 1);
    if (dtype === "O") return new NDArray(shape, new Array(n).fill(value), "O");
    const d = new Float64Array(n);
    d.fill(value);
    return new NDArray(shape, d, "f8");
  }
  static from(value) {
    if (value instanceof NDArray) return value;
    if (typeof value === "number") return new NDArray([], Float64Array.of(value));
    if (Array.isArray(value)) {
      const shape = [];
      let v = value;
      while (Array.isArray(v)) { shape.push(v.length); v = v[0]; }
      const flat = value.flat(Infinity).map((x) => (typeof x === "number" ? x : Number(x)));
      return new NDArray(shape, Float64Array.from(flat));
    }
    return new NDArray([], Float64Array.of(Number(value)));
  }
  toNested() {
    if (this.shape.length === 0) return this.data[0];
    const rec = (dim, off) => {
      const n = this.shape[dim];
      const st = this.strides[dim];
      const out = [];
      for (let i = 0; i < n; i++) {
        out.push(dim === this.shape.length - 1 ? this.data[off + i] : rec(dim + 1, off + i * st));
      }
      return out;
    };
    return rec(0, 0);
  }
}

export class Complex {
  constructor(re, im) { this.re = re; this.im = im; }
  toString() { return `${this.re}${this.im >= 0 ? "+" : ""}${this.im}j`; }
  valueOf() { return this.re; }
}

const VIEW = {
  f8: Float64Array, f4: Float32Array, i4: Int32Array, i2: Int16Array, i1: Int8Array,
  u4: Uint32Array, u2: Uint16Array, u1: Uint8Array, b1: Uint8Array,
};

function toFloat64(buf, dtype) {
  const code = dtype.replace(/^[<>|=]/, "");
  if (dtype[0] === ">") throw new Error("big-endian arrays are not supported");
  if (code === "f8") return new Float64Array(buf);      // buf is an aligned copy
  if (code === "i8" || code === "u8") {
    const a = code === "i8" ? new BigInt64Array(buf) : new BigUint64Array(buf);
    return Float64Array.from(a, (x) => Number(x));
  }
  if (code === "c16" || code === "c8") {                  // complex: keep the real part
    const a = code === "c16" ? new Float64Array(buf) : new Float32Array(buf);
    const out = new Float64Array(a.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = a[2 * i];
    return out;
  }
  const V = VIEW[code];
  if (!V) throw new Error("unsupported dtype " + dtype);
  return Float64Array.from(new V(buf));
}

function restore(obj, bufs) {
  if (Array.isArray(obj)) return obj.map((v) => restore(v, bufs));
  if (obj === null || typeof obj !== "object") return obj;
  const keys = Object.keys(obj);
  if ("__nd__" in obj && keys.length === 3) {
    const b = bufs[obj.__nd__];
    if (!b) throw new Error("bad buffer index");
    return new NDArray(obj.shape, toFloat64(b, obj.dtype));
  }
  if ("__ndstr__" in obj && keys.length === 2) return new NDArray(obj.shape, obj.__ndstr__.map((v) => restore(v, bufs)), "O");
  if ("__ndobj__" in obj && keys.length === 2) return new NDArray(obj.shape, obj.__ndobj__.map((v) => restore(v, bufs)), "O");
  if ("__f__" in obj && keys.length === 1) {
    const s = String(obj.__f__);
    return s === "nan" ? NaN : s === "inf" ? Infinity : s === "-inf" ? -Infinity : parseFloat(s);
  }
  if ("__c__" in obj && keys.length === 1) return new Complex(obj.__c__[0], obj.__c__[1]);
  if ("__b__" in obj && keys.length === 1) return new Uint8Array(bufs[obj.__b__]);
  if ("__repr__" in obj && keys.length === 1) return obj.__repr__;
  const out = {};
  for (const k of keys) out[k] = restore(obj[k], bufs);
  return out;
}

/** Decode one frame (ArrayBuffer, including the length prefix). */
export function decode(ab) {
  const dv = new DataView(ab);
  if (ab.byteLength < 8) throw new Error("frame too short");
  const n = dv.getUint32(0, true);
  if (n !== ab.byteLength - 4) throw new Error("frame length mismatch");
  const jlen = dv.getUint32(4, true);
  if (8 + jlen > ab.byteLength) throw new Error("bad json length");
  const meta = JSON.parse(td.decode(new Uint8Array(ab, 8, jlen)));
  const bufs = [];
  let off = 8 + jlen;
  for (const s of meta.b || []) {
    if (off + s > ab.byteLength) throw new Error("bad buffer length");
    bufs.push(ab.slice(off, off + s));
    off += s;
  }
  return restore(meta.m, bufs);
}

function flatten(obj, bufs) {
  if (obj === null || obj === undefined) return null;
  if (typeof obj === "number") {
    if (Number.isFinite(obj)) return obj;
    return { __f__: Number.isNaN(obj) ? "nan" : obj > 0 ? "inf" : "-inf" };
  }
  if (typeof obj === "string" || typeof obj === "boolean") return obj;
  if (obj instanceof NDArray) {
    if (obj.dtype === "O") return { __ndobj__: obj.data.map((v) => flatten(v, bufs)), shape: obj.shape };
    bufs.push(new Uint8Array(Float64Array.from(obj.data).buffer));
    return { __nd__: bufs.length - 1, dtype: "<f8", shape: obj.shape };
  }
  if (obj instanceof Float64Array) {
    bufs.push(new Uint8Array(Float64Array.from(obj).buffer));
    return { __nd__: bufs.length - 1, dtype: "<f8", shape: [obj.length] };
  }
  if (Array.isArray(obj)) return obj.map((v) => flatten(v, bufs));
  if (typeof obj === "object") {
    const out = {};
    for (const [k, v] of Object.entries(obj)) if (v !== undefined) out[k] = flatten(v, bufs);
    return out;
  }
  return String(obj);
}

/** Encode a message into a frame (ArrayBuffer). */
export function encode(msg) {
  const bufs = [];
  const flat = flatten(msg, bufs);
  const js = te.encode(JSON.stringify({ m: flat, b: bufs.map((b) => b.byteLength) }));
  const total = 8 + js.length + bufs.reduce((a, b) => a + b.byteLength, 0);
  const out = new Uint8Array(total);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, total - 4, true);
  dv.setUint32(4, js.length, true);
  out.set(js, 8);
  let off = 8 + js.length;
  for (const b of bufs) { out.set(b, off); off += b.byteLength; }
  return out.buffer;
}

/** Plain JSON-able copy (NDArray -> nested lists, NaN -> null). */
export function toJSONable(v) {
  if (v instanceof NDArray) return v.dtype === "O" ? v.data : v.toNested();
  if (v instanceof Float64Array) return Array.from(v);
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (Array.isArray(v)) return v.map(toJSONable);
  if (v && typeof v === "object" && !(v instanceof Complex)) {
    const o = {};
    for (const [k, x] of Object.entries(v)) o[k] = toJSONable(x);
    return o;
  }
  if (v instanceof Complex) return v.re;
  return v;
}
