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
  constructor({ closable = false, onClose = null, onChange = null } = {}) {
    this.bar = h("div", { class: "tabbar" });
    this.body = h("div", { class: "tabbody" });
    this.el = h("div", { class: "tabs" }, this.bar, this.body);
    this.items = [];
    this.active = null;
    this.closable = closable;
    this.onClose = onClose;
    this.onChange = onChange;
  }

  add(name, el, { closable = this.closable, title = null, index = null } = {}) {
    const tab = h("div", { class: "tab", title }, h("span", { text: name }));
    const item = { name, el, tab };
    if (closable) {
      tab.append(h("button", { class: "tab-x", text: "✕", title: "close",
        on: { click: (ev) => { ev.stopPropagation(); this.remove(item); if (this.onClose) this.onClose(item); } } }));
    }
    tab.addEventListener("click", () => this.show(item));
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
export function formDialog(title, fields, { wide = false, validate = null } = {}) {
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
  return modal(title, grid, [["Cancel", null], ["OK", true]], { wide,
    onOk: async () => {
      const v = read();
      if (validate) { const r = await validate(v); if (r === false) return false; if (r !== undefined && r !== true) return r; }
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
let statusEl = null;
let statusTimer = null;
export function setStatusElement(el) { statusEl = el; }
export function message(level, text) {
  if (!statusEl) return;
  statusEl.textContent = (level === "error" || level === "warning" ? "⚠ " : "") + text;
  statusEl.className = "status-msg " + level;
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { statusEl.textContent = ""; }, 9000);
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
