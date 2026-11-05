// Tiny arithmetic expression evaluator (no eval): numbers, x, + - * / % ** ^,
// parentheses, unary minus, constants pi / e and common math functions.

const FUNCS = {
  sqrt: Math.sqrt, abs: Math.abs, sin: Math.sin, cos: Math.cos, tan: Math.tan,
  asin: Math.asin, acos: Math.acos, atan: Math.atan, arcsin: Math.asin, arccos: Math.acos,
  arctan: Math.atan, exp: Math.exp, log: Math.log, ln: Math.log, log10: Math.log10,
  log2: Math.log2, floor: Math.floor, ceil: Math.ceil, round: Math.round, sign: Math.sign,
  min: Math.min, max: Math.max, hypot: Math.hypot, atan2: Math.atan2, pow: Math.pow,
  sinh: Math.sinh, cosh: Math.cosh, tanh: Math.tanh,
};
const CONSTS = { pi: Math.PI, e: Math.E };

function tokenize(s) {
  const re = /\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|([A-Za-z_][A-Za-z_0-9]*)|(\*\*|[-+*/%^(),]))/y;
  const out = [];
  let m;
  re.lastIndex = 0;
  while (re.lastIndex < s.length) {
    const start = re.lastIndex;
    m = re.exec(s);
    if (!m) {
      if (/^\s*$/.test(s.slice(start))) break;
      throw new Error(`unexpected '${s.slice(start).trim()[0]}'`);
    }
    if (m[1] !== undefined) out.push({ t: "num", v: parseFloat(m[1]) });
    else if (m[2] !== undefined) out.push({ t: "id", v: m[2] });
    else out.push({ t: "op", v: m[3] });
  }
  return out;
}

export function evaluate(src, vars = {}) {
  if (src.length > 500) throw new Error("expression too long");
  const tk = tokenize(src);
  let i = 0;
  const peek = () => tk[i];
  const take = (v) => { const t = tk[i]; if (!t || (v && t.v !== v)) throw new Error(`expected ${v || "more input"}`); i++; return t; };
  function primary() {
    const t = peek();
    if (!t) throw new Error("unexpected end");
    if (t.t === "num") { i++; return t.v; }
    if (t.t === "op" && t.v === "(") { i++; const v = add(); take(")"); return v; }
    if (t.t === "op" && (t.v === "-" || t.v === "+")) { i++; const v = power(); return t.v === "-" ? -v : v; }
    if (t.t === "id") {
      i++;
      if (peek() && peek().v === "(") {
        const f = FUNCS[t.v];
        if (!f) throw new Error(`unknown function ${t.v}`);
        i++;
        const args = [];
        if (peek() && peek().v !== ")") { args.push(add()); while (peek() && peek().v === ",") { i++; args.push(add()); } }
        take(")");
        return f(...args);
      }
      if (t.v in vars) return Number(vars[t.v]);
      if (t.v in CONSTS) return CONSTS[t.v];
      throw new Error(`unknown name ${t.v}`);
    }
    throw new Error(`unexpected '${t.v}'`);
  }
  function power() {
    const b = primary();
    if (peek() && (peek().v === "**" || peek().v === "^")) { i++; return Math.pow(b, unary()); }
    return b;
  }
  function unary() {
    const t = peek();
    if (t && t.t === "op" && (t.v === "-" || t.v === "+")) { i++; const v = unary(); return t.v === "-" ? -v : v; }
    return power();
  }
  function mul() {
    let v = unary();
    while (peek() && ["*", "/", "%"].includes(peek().v)) {
      const op = take().v, r = unary();
      v = op === "*" ? v * r : op === "/" ? v / r : v % r;
    }
    return v;
  }
  function add() {
    let v = mul();
    while (peek() && (peek().v === "+" || peek().v === "-")) { const op = take().v; const r = mul(); v = op === "+" ? v + r : v - r; }
    return v;
  }
  const v = add();
  if (i !== tk.length) throw new Error(`unexpected '${tk[i].v}'`);
  if (typeof v !== "number" || !Number.isFinite(v)) throw new Error(`result is ${v}`);
  return v;
}
