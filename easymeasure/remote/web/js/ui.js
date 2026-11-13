// Small DOM toolkit: element builder, tables, tabs, splitters, dialogs, messages.
// No innerHTML with data anywhere: everything is built with textContent, so text
// coming from the backend can never inject markup.

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "on") for (const [e, fn] of Object.entries(v)) el.addEventListener(e, fn);
    else if (k === "style") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else if (k === "value" || k === "checked" || k === "disabled" || k === "selected") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

export function fmt(v, digits = 6) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") {
    if (Number.isNaN(v)) return "NaN";
    if (!Number.isFinite(v)) return v > 0 ? "inf" : "-inf";
    if (Number.isInteger(v) && Math.abs(v) < 1e12) return String(v);
    return Number(v.toPrecision(digits)).toString();
  }
  if (v && v.shape) return `array(${v.shape.join("×")})`;
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function fmtTime(s) {
  if (s === null || s === undefined || !Number.isFinite(s)) return "?";
  s = Math.round(s);
  const hh = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return hh ? `${hh}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}

export const clock = (t) => (t ? new Date(t * 1000).toLocaleTimeString() : "");

// ---------------------------------------------------------------- buttons ----
/** A button bound to a backend command (disabled when the role does not allow it). */
export function btn(text, onclick, { cmd = null, title = null, cls = "" } = {}) {
  const b = h("button", { class: cls, text, title, on: { click: onclick } });
  if (cmd) b.dataset.cmd = cmd;
  return b;
}

/** Enable/disable every element carrying data-cmd according to the allowed set. */
export function applyPermissions(root, can) {
  for (const el of root.querySelectorAll("[data-cmd]")) {
    const cmds = el.dataset.cmd.split(" ");
    const ok = cmds.every((c) => can(c));
    el.classList.toggle("denied", !ok);
    if ("disabled" in el && !el.dataset.lockedRo) el.disabled = !ok || el.dataset.busy === "1";
    if (!ok) el.title = (el.dataset.titleOrig ??= el.title || "") +
      (el.dataset.titleOrig ? "\n" : "") + "not allowed for your role";
    else if (el.dataset.titleOrig !== undefined) el.title = el.dataset.titleOrig;
  }
}

/** Queue buttons (data-qcan="start|pause|resume|abort|...") follow what the queue
 *  can do now (q.can from the backend); role permissions still apply on top. */
export function qcan(el, what) { el.dataset.qcan = what; el.dataset.busy = "1"; el.disabled = true; return el; }

export function applyQueueControls(root, q) {
  const can = (q && q.can) || {};
  for (const el of root.querySelectorAll("[data-qcan]")) {
    const ok = !!can[el.dataset.qcan];
    el.dataset.busy = ok ? "" : "1";
    el.disabled = !ok || el.classList.contains("denied");
  }
}

// ------------------------------------------------------------------ table ----
export class Table {
  /** columns: [{key, label, fmt?, cls?, width?}] */
  constructor(columns, { onSelect = null, onActivate = null, multi = false, empty = "" } = {}) {
    this.columns = columns;
    this.rows = [];
    this.keyOf = (r) => r.name ?? r.id ?? JSON.stringify(r);
    this.selected = new Set();
    this.onSelect = onSelect;
    this.onActivate = onActivate;
    this.multi = multi;
    this.empty = empty;
    this.tbody = h("tbody");
    this.el = h("div", { class: "table-wrap" },
      h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, columns.map((c) => h("th", { text: c.label, title: c.title || null })))),
        this.tbody));
  }

  set(rows, { rowClass = null, rowTitle = null } = {}) {
    this.rows = rows;
    const keys = new Set(rows.map(this.keyOf));
    for (const k of [...this.selected]) if (!keys.has(k)) this.selected.delete(k);
    this.tbody.replaceChildren();
    if (!rows.length && this.empty) {
      this.tbody.append(h("tr", { class: "empty" }, h("td", { colSpan: this.columns.length, text: this.empty })));
    }
    rows.forEach((r, i) => {
      const key = this.keyOf(r);
      const tr = h("tr", { class: (rowClass && rowClass(r)) || "", title: (rowTitle && rowTitle(r)) || null },
        this.columns.map((c) => {
          const v = c.fmt ? c.fmt(r[c.key], r) : fmt(r[c.key]);
          const td = h("td", { class: c.cls ? (typeof c.cls === "function" ? c.cls(r) : c.cls) : "" });
          if (v instanceof Node) td.append(v); else { td.textContent = v ?? ""; td.title = v ?? ""; }
          return td;
        }));
      tr.classList.toggle("sel", this.selected.has(key));
      tr.addEventListener("click", (ev) => {
        if (!(this.multi && (ev.ctrlKey || ev.metaKey))) this.selected.clear();
        if (this.selected.has(key) && this.multi) this.selected.delete(key); else this.selected.add(key);
        this._paint();
        if (this.onSelect) this.onSelect(this.selection());
      });
      tr.addEventListener("dblclick", () => this.onActivate && this.onActivate(r));
      tr.dataset.i = i;
      this.tbody.append(tr);
    });
  }

  _paint() {
    for (const tr of this.tbody.children) {
      const r = this.rows[Number(tr.dataset.i)];
      if (r) tr.classList.toggle("sel", this.selected.has(this.keyOf(r)));
    }
  }

  selection() { return this.rows.filter((r) => this.selected.has(this.keyOf(r))); }
  current() { return this.selection()[0] || null; }
  select(key) { this.selected = new Set([key]); this._paint(); }
}

// ------------------------------------------------------------------- tabs ----
export class Tabs {
  /** movable: tabs can be dragged into another order (onMove(names) is told). */
  constructor({ closable = false, onClose = null, onChange = null, movable = false, onMove = null } = {}) {
    this.bar = h("div", { class: "tabbar" });
    this.body = h("div", { class: "tabbody" });
    this.el = h("div", { class: "tabs" }, this.bar, this.body);
    this.items = [];
    this.active = null;
    this.closable = closable;
    this.onClose = onClose;
    this.onChange = onChange;
    this.movable = movable;
    this.onMove = onMove;
    this._drag = null;
  }

  add(name, el, { closable = this.closable, title = null, index = null } = {}) {
    const tab = h("div", { class: "tab", title }, h("span", { text: name }));
    const item = { name, el, tab };
    if (closable) {
      tab.append(h("button", { class: "tab-x", text: "✕", title: "close",
        on: { click: (ev) => { ev.stopPropagation(); this.remove(item); if (this.onClose) this.onClose(item); } } }));
    }
    tab.addEventListener("click", () => this.show(item));
    if (this.movable) {
      tab.draggable = true;
      tab.title = (title ? title + "\n" : "") + "drag to change the order of the tabs";
      tab.addEventListener("dragstart", (ev) => {
        this._drag = item;
        ev.dataTransfer.effectAllowed = "move";
        try { ev.dataTransfer.setData("text/plain", name); } catch (e) { /* */ }
      });
      tab.addEventListener("dragover", (ev) => { if (this._drag && this._drag !== item) { ev.preventDefault(); tab.classList.add("drop"); } });
      tab.addEventListener("dragleave", () => tab.classList.remove("drop"));
      tab.addEventListener("drop", (ev) => {
        ev.preventDefault();
        tab.classList.remove("drop");
        const d = this._drag;
        this._drag = null;
        if (d && d !== item) this.move(d, this.items.indexOf(item));
      });
      tab.addEventListener("dragend", () => { this._drag = null; });
    }
    el.classList.add("tabpage");
    if (index === null || index >= this.items.length) {
      this.items.push(item); this.bar.append(tab); this.body.append(el);
    } else {
      this.items.splice(index, 0, item);
      this.bar.insertBefore(tab, this.bar.children[index]);
      this.body.append(el);
    }
    if (!this.active) this.show(item);
    else el.hidden = true;
    return item;
  }

  show(item) {
    if (typeof item === "number") item = this.items[item];
    if (!item) return;
    this.active = item;
    for (const it of this.items) { it.el.hidden = it !== item; it.tab.classList.toggle("active", it === item); }
    if (this.onChange) this.onChange(item);
    item.el.dispatchEvent(new CustomEvent("shown"));
  }

  remove(item) {
    const i = this.items.indexOf(item);
    if (i < 0) return;
    this.items.splice(i, 1);
    item.tab.remove();
    item.el.remove();
    if (this.active === item) this.show(this.items[Math.max(0, i - 1)]);
  }

  /** Put *item* at position *index* (the tab bar follows). */
  move(item, index) {
    if (typeof item === "string") item = this.items.find((it) => it.name === item);
    const i = this.items.indexOf(item);
    if (i < 0) return;
    this.items.splice(i, 1);
    index = Math.max(0, Math.min(this.items.length, index));
    this.items.splice(index, 0, item);
    for (const it of this.items) this.bar.append(it.tab);
    if (this.onMove) this.onMove(this.items.map((it) => it.name));
  }

  rename(item, name) { item.name = name; item.tab.firstChild.textContent = name; }
}

// -------------------------------------------------------------- splitters ----
/** Drag handle between `a` and its next sibling; `vertical` = side by side. */
export function splitter(a, { vertical = true, key = null, min = 80 } = {}) {
  const s = h("div", { class: vertical ? "splitter v" : "splitter h" });
  const restore = () => {
    if (!key) return;
    try {
      const v = localStorage.getItem("em.split." + key);
      if (v) a.style.flexBasis = v;
    } catch (e) { /* storage blocked */ }
  };
  s.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    s.setPointerCapture(ev.pointerId);
    const r0 = a.getBoundingClientRect();
    const start = vertical ? ev.clientX : ev.clientY;
    const size0 = vertical ? r0.width : r0.height;
    const parent = a.parentElement.getBoundingClientRect();
    const max = (vertical ? parent.width : parent.height) - min;
    const move = (e) => {
      const d = (vertical ? e.clientX : e.clientY) - start;
      const sz = Math.max(min, Math.min(max, size0 + d));
      a.style.flexBasis = sz + "px";
      a.style.flexGrow = "0";
      window.dispatchEvent(new Event("resize"));
    };
    const up = () => {
      s.removeEventListener("pointermove", move);
      s.removeEventListener("pointerup", up);
      if (key) { try { localStorage.setItem("em.split." + key, a.style.flexBasis); } catch (e) { /* */ } }
    };
    s.addEventListener("pointermove", move);
    s.addEventListener("pointerup", up);
  });
  restore();
  if (a.style.flexBasis) a.style.flexGrow = "0";
  return s;
}

// ---------------------------------------------------------------- dialogs ----
/** Generic modal; resolves with the value of the clicked button (or null). */
export function modal(title, body, buttons = [["Cancel", null], ["OK", true]], { wide = false, onOk = null } = {}) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: "modal" + (wide ? " wide" : "") });
    const msg = h("div", { class: "modal-msg" });
    const close = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    const bar = h("div", { class: "modal-buttons" }, buttons.map(([label, val], i) =>
      h("button", { text: label, class: i === buttons.length - 1 ? "primary" : "",
        on: { click: async () => {
          if (val !== null && onOk) {
            try {
              const r = await onOk(val);
              if (r === false) return;
              close(r === undefined ? val : r);
            } catch (e) { msg.textContent = "✘ " + e.message; }
            return;
          }
          close(val);
        } } })));
    dlg.append(h("h3", { text: title }), h("div", { class: "modal-body" }, body), msg, bar);
    dlg.addEventListener("cancel", (ev) => { ev.preventDefault(); close(null); });
    document.body.append(dlg);
    dlg.showModal();
    const f = dlg.querySelector("input, select, textarea");
    if (f) f.focus();
  });
}

/** fields: [{key, label, type, value, options, placeholder, rows, help, list}] */
export function formDialog(title, fields, { wide = false, validate = null, extra = null } = {}) {
  const inputs = {};
  const grid = h("div", { class: "form" });
  for (const f of fields) {
    let el;
    if (f.type === "select") {
      el = h("select", {}, (f.options || []).map((o) => {
        const [v, label] = Array.isArray(o) ? o : [o, o];
        return h("option", { value: v, text: label, selected: String(v) === String(f.value ?? "") });
      }));
    } else if (f.type === "textarea") {
      el = h("textarea", { rows: f.rows || 6, placeholder: f.placeholder || "", spellcheck: false });
      el.value = f.value ?? "";
    } else if (f.type === "checkbox") {
      el = h("input", { type: "checkbox", checked: !!f.value });
    } else if (f.type === "info") {
      el = h("div", { class: "help", text: f.value || "" });
    } else {
      el = h("input", { type: f.type === "password" ? "password" : "text", placeholder: f.placeholder || "",
        autocomplete: "off", spellcheck: false });
      el.value = f.value ?? "";
      if (f.list) {
        const id = "dl-" + Math.random().toString(36).slice(2);
        const dl = h("datalist", { id }, f.list.map((o) => h("option", { value: o })));
        el.setAttribute("list", id);
        grid.append(dl);
      }
    }
    if (f.onInput) el.addEventListener("input", () => f.onInput(el, inputs));
    if (f.onChange) el.addEventListener("change", () => f.onChange(el, inputs));
    inputs[f.key] = el;
    if (f.type === "checkbox") grid.append(h("span"), h("label", { class: "check" }, el, " " + f.label));
    else grid.append(h("label", { text: f.label }), el);
    if (f.help) grid.append(h("span"), h("div", { class: "help", text: f.help }));
  }
  const read = () => {
    const out = {};
    for (const f of fields) {
      const el = inputs[f.key];
      if (f.type === "info") continue;
      out[f.key] = f.type === "checkbox" ? el.checked : el.value;
    }
    return out;
  };
  const body = extra ? h("div", {}, grid, extra(inputs, read)) : grid;
  return modal(title, body, [["Cancel", null], ["OK", true]], { wide,
    onOk: async () => {
      const v = read();
      if (validate) { const r = await validate(v, inputs); if (r === false) return false; if (r !== undefined && r !== true) return r; }
      return v;
    } });
}

export function confirmDialog(title, text) {
  return modal(title, h("p", { text }), [["Cancel", null], ["Yes", true]]);
}

export async function promptDialog(title, label, value = "", help = "") {
  const r = await formDialog(title, [{ key: "v", label, value, help }]);
  return r ? r.v : null;
}

/** Download text as a file. */
export function download(name, text, type = "application/json") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Let the user pick a local file; resolves with its text (or null). */
export function pickFile(accept = ".json,application/json") {
  return new Promise((resolve) => {
    const inp = h("input", { type: "file", accept });
    inp.addEventListener("change", async () => {
      const f = inp.files[0];
      if (!f) { resolve(null); return; }
      if (f.size > 20 * 1024 * 1024) { resolve(null); return; }
      resolve({ name: f.name, text: await f.text() });
    });
    inp.click();
  });
}

// ---------------------------------------------------------- status messages --
// One status line at the bottom of the window: errors and warnings stay until the
// next message, the rest clears after 10 s; every message also goes to the Log.
let statusEl = null;
let statusTimer = null;
let statusListener = null;
export function setStatusElement(el, onMessage = null) { statusEl = el; statusListener = onMessage; }
export function message(level, text) {
  if (statusListener) { try { statusListener(level, text); } catch (e) { /* log closed */ } }
  if (!statusEl) return;
  const bad = level === "error" || level === "warning";
  statusEl.textContent = (bad ? (level === "error" ? "✘ " : "⚠ ") : "") + text;
  statusEl.className = "status-msg " + level;
  statusEl.title = text;
  clearTimeout(statusTimer);
  if (!bad) statusTimer = setTimeout(() => { statusEl.textContent = ""; statusEl.title = ""; }, 10000);
}

// ------------------------------------------------------------------ checks ----
// Every editor of code or formulas has a Check button, and its OK checks first:
// the backend answers (it knows the channels, parameters and rules).
let defaultConn = null;
export function setConnection(conn) { defaultConn = conn; }

/** cmd: the backend check command; request() -> args (throws for what can be told
 *  locally); okText(reply) -> the line shown when it passed.  run() resolves with
 *  the reply or rejects; a reply {ok: false, errors} counts as failed. */
export function checker(cmd, request, okText, { conn = null, title = "check it with the backend without saving anything" } = {}) {
  const info = h("div", { class: "check-info" });
  const show = (t, cls) => {
    info.classList.remove("ok", "warn", "err", "busy");
    if (cls) info.classList.add(cls);
    info.textContent = t;
  };
  const getConn = () => conn || defaultConn;
  const run = async () => {
    let args;
    try { args = request(); } catch (e) { show("✘ " + e.message, "err"); throw e; }
    const c = getConn();
    if (!c || !c.can(cmd)) { show("✔ looks right (not checked with the backend)", "ok"); return null; }
    show("checking …", "busy");
    let r;
    try { r = await c.request(cmd, args); } catch (e) { show("✘ " + e.message, "err"); throw e; }
    if (r && r.ok === false) {
      const m = (r.errors || ["not valid"]).join("; ");
      show("✘ " + m, "err");
      throw new Error(m);
    }
    const w = (r && r.warnings) || [];
    show(okText(r) + w.map((x) => "\n⚠ " + x).join(""), w.length ? "warn" : "ok");
    return r;
  };
  const button = h("button", { text: "Check", class: "check-btn", title, on: { click: () => run().catch(() => {}) } });
  return { info, button, run, show };
}

/** A Check button and result line for a code box (check_code). */
export function codeChecker(textarea, what) {
  return checker("check_code", () => {
    const code = textarea.value;
    if (!code.trim()) throw new Error("no code");
    return { code, what };
  }, () => "✔ valid Python", { title: "check the code: syntax, and that the channels it quotes exist" });
}

export function group(title, ...children) {
  return h("fieldset", { class: "group" }, h("legend", { text: title }), ...children);
}

export function bar(...children) { return h("div", { class: "bar" }, ...children); }

/** Coalesce calls into one per animation frame (at most every `ms`). */
export function throttle(fn, ms = 80) {
  let pending = false, last = 0;
  return () => {
    if (pending) return;
    pending = true;
    const wait = Math.max(0, ms - (performance.now() - last));
    setTimeout(() => requestAnimationFrame(() => { pending = false; last = performance.now(); fn(); }), wait);
  };
}

/** Columns for `n` items of at least `minW` × `minH` in a `W` × `H` area: among the
 *  arrangements where every item keeps its minimum, the one with the largest items
 *  (the shorter of width and 1.5 × height); when none fits, the one that overflows
 *  least, preferring any that needs no sideways scrolling. */
export function autoColumns(n, W, H, minW, minH, gap = 8, maxCols = 4) {
  if (n <= 1) return 1;
  W = Math.max(W, 1); H = Math.max(H, 1);
  let best = 1, bestScore = null, least = 1, leastOver = null;
  for (let c = 1; c <= Math.min(maxCols, n); c++) {
    const rows = Math.ceil(n / c);
    const cw = (W - gap * (c - 1)) / c, ch = (H - gap * (rows - 1)) / rows;
    const hover = Math.max(0, minW - cw) * c, vover = Math.max(0, minH - ch) * rows;
    if (hover === 0 && vover === 0) {
      const score = Math.min(cw, 1.5 * ch);
      if (bestScore === null || score > bestScore + 1e-9) { best = c; bestScore = score; }
    } else {
      const over = (hover > 0 ? 1e9 : 0) + hover + vover;
      if (leastOver === null || over < leastOver) { least = c; leastOver = over; }
    }
  }
  return bestScore !== null ? best : least;
}
