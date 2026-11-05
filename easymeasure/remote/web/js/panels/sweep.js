// Sweep editor: create / modify / save / load N-dimensional sweeps
// (port of panels/sweep_editor.py).  read_only=true for the running-sweep view.

import { h, btn, bar, group, fmt, download, pickFile } from "../ui.js";
import { toJSONable } from "../codec.js";

const SPECIAL_RECORD = ["@time", "@elapsed"];

function numIn(value, { min = 0, step = "any", width = "" } = {}) {
  return h("input", { type: "number", min, step, value: String(value), class: width });
}

/** Checkable channel list that remembers the order names were loaded in. */
class CheckList {
  constructor() { this.el = h("div", { class: "checklist" }); this.order = []; this.boxes = new Map(); this.ro = false; }
  setNames(names) {
    const checked = new Set(this.checked());
    const all = names.concat([...checked].filter((c) => !names.includes(c)).sort());
    this.el.replaceChildren();
    this.boxes.clear();
    for (const n of all) {
      const cb = h("input", { type: "checkbox", checked: checked.has(n), disabled: this.ro });
      this.boxes.set(n, cb);
      this.el.append(h("label", { class: "check" }, cb, " " + n));
    }
  }
  checked() {
    const names = [...this.boxes].filter(([, cb]) => cb.checked).map(([n]) => n);
    const ord = new Map(this.order.map((n, i) => [n, i]));
    return names.sort((a, b) => (ord.get(a) ?? 1e9) - (ord.get(b) ?? 1e9) || names.indexOf(a) - names.indexOf(b));
  }
  setChecked(names) {
    this.order = names.slice();
    const missing = names.filter((n) => !this.boxes.has(n));
    if (missing.length) this.setNames([...this.boxes.keys()].concat(missing));
    for (const [n, cb] of this.boxes) cb.checked = names.includes(n);
  }
  setReadOnly(ro) { this.ro = ro; for (const cb of this.boxes.values()) cb.disabled = ro; }
}

class SweepChannelTable {
  constructor(onChange) {
    this.rows = [];
    this.channels = [];
    this.onChange = onChange;
    this.listId = "sw-ch-" + Math.random().toString(36).slice(2);
    this.list = h("datalist", { id: this.listId });
    this.tbody = h("tbody");
    this.el = h("div", { class: "table-wrap" }, this.list, h("table", { class: "grid edit" },
      h("thead", {}, h("tr", {}, ["Channel", "Start", "Stop", "Mapping f(i,N,t,start,stop,x)", "Values (comma list)", ""].map((t) => h("th", { text: t })))),
      this.tbody));
    this.ro = false;
  }
  setChannels(names) { this.channels = names; this.list.replaceChildren(...names.map((n) => h("option", { value: n }))); }
  addRow(e = {}) {
    const ch = h("input", { value: e.channel ?? (this.channels[0] || ""), class: "w-chan" });
    ch.setAttribute("list", this.listId);
    const start = h("input", { value: e.start ?? (e.values ? "" : 0), class: "w-num" });
    const stop = h("input", { value: e.stop ?? (e.values ? "" : 0), class: "w-num" });
    const mapping = h("input", { value: e.mapping || "", placeholder: "optional", class: "w-expr" });
    const values = h("input", { value: e.values ? e.values.join(", ") : "", placeholder: "optional", class: "w-expr" });
    const row = { ch, start, stop, mapping, values };
    const rm = h("button", { text: "−", title: "remove this row", on: { click: () => { this.rows = this.rows.filter((r) => r !== row); tr.remove(); this.onChange(); } } });
    row.rm = rm;
    const tr = h("tr", {}, [ch, start, stop, mapping, values, rm].map((x) => h("td", {}, x)));
    row.tr = tr;
    for (const x of [ch, start, stop, mapping, values]) { x.addEventListener("input", () => this.onChange()); x.readOnly = this.ro; }
    rm.disabled = this.ro;
    this.rows.push(row);
    this.tbody.append(tr);
  }
  clear() { this.rows = []; this.tbody.replaceChildren(); }
  entries() {
    const out = [];
    this.rows.forEach((r, i) => {
      const ch = r.ch.value.trim();
      if (!ch) return;
      const e = { channel: ch };
      const values = r.values.value.trim(), mapping = r.mapping.value.trim();
      if (values && !mapping) {
        e.values = values.replace(/;/g, ",").split(",").filter((x) => x.trim()).map((x) => {
          const v = Number(x); if (!Number.isFinite(v)) throw new Error(`row ${i + 1}: values must be numbers`); return v;
        });
      } else {
        for (const key of ["start", "stop"]) {
          const t = r[key].value.trim();
          if (t) { const v = Number(t); if (!Number.isFinite(v)) throw new Error(`row ${i + 1}: ${key} must be a number`); e[key] = v; }
        }
        if (mapping) e.mapping = mapping;
        e.start ??= 0; e.stop ??= 0;
      }
      out.push(e);
    });
    return out;
  }
  setReadOnly(ro) { this.ro = ro; for (const r of this.rows) { for (const k of ["ch", "start", "stop", "mapping", "values"]) r[k].readOnly = ro; r.rm.disabled = ro; } }
}

const HOOKS = [
  ["pre_sweep", "Pre-sweep action", "before the swept channels are set"],
  ["post_sweep", "Post-sweep action", "after the swept channels are set"],
  ["pre_record", "Pre-record action", "right before recording (e.g. trigger a meter)"],
  ["post_record", "Post-record action", "right after recording (also sees 'values')"],
];

/** Code of one per-point hook, with a helper to insert an instrument action. */
class HookEditor {
  constructor(name, title, when) {
    this.name = name;
    this.code = h("textarea", { class: "code", rows: 3, spellcheck: false,
      placeholder: `# Python, runs at every point ${when}. Names: k, i, index, setpoints${name === "post_record" ? ", values" : ""};\n# get, set, action, sleep, log, state ...   e.g.  action('dmm.trigger')` });
    this.actionBox = h("select", { title: "instrument actions (commands without arguments)" });
    this.addAction = btn("+ action", () => {
      const ref = this.actionBox.value;
      if (!ref) return;
      let t = this.code.value;
      if (t && !t.endsWith("\n")) t += "\n";
      this.code.value = t + `action('${ref}')\n`;
    }, { title: "append the selected action to this code" });
    this.legend = h("legend", { text: title });
    this.el = h("fieldset", { class: "group hook", title: "Python code run " + when, hidden: true },
      this.legend, this.code, bar(this.actionBox, this.addAction));
  }
  setActions(actions) {
    const cur = this.actionBox.value;
    this.actionBox.replaceChildren(...actions.map((a) => h("option", { text: a, value: a })));
    if (actions.includes(cur)) this.actionBox.value = cur;
  }
  setReadOnly(ro) { this.code.readOnly = ro; this.actionBox.disabled = ro; this.addAction.disabled = ro; }
}

/** One dimension, laid out in the order every point runs:
 *  [pre-sweep action] → set swept channels → [post-sweep action] → pre-sample delay
 *  → [pre-record action] → record → [post-record action] → post-sample delay.
 *  The four actions are hidden and off until ticked. */
class DimensionEditor {
  constructor(onChange, canHook = () => true) {
    this.canHook = canHook;
    this.points = numIn(11, { min: 1, step: 1, width: "w-num" });
    this.pre = numIn(0, { width: "w-num" });
    this.post = numIn(0, { width: "w-num" });
    this.pre.title = "wait after setting, before recording (settling time)";
    this.post.title = "wait after recording, before the next point";
    this.label = h("input", { class: "w-chan", placeholder: "optional" });
    this.snake = h("input", { type: "checkbox", title: "alternate the sweep direction on every pass" });
    this.after = h("input", { type: "checkbox", title: "record this dimension's channels after the inner dimensions have run" });
    this.table = new SweepChannelTable(onChange);
    this.record = new CheckList();
    this.addBtn = btn("+ sweep channel", () => { this.table.addRow(); onChange(); });
    this.points.addEventListener("input", onChange);
    this.hooks = {};
    this.hookChecks = {};
    for (const [name, title, when] of HOOKS) {
      this.hooks[name] = new HookEditor(name, title, when);
      this.hookChecks[name] = h("input", { type: "checkbox", title: `run Python code ${when}`,
        on: { change: () => { this.hooks[name].el.hidden = !this.hookChecks[name].checked; this._renumber(); this._note(); } } });
    }
    this.hookNote = h("div", { class: "help" });
    this.sweepLegend = h("legend");
    this.recLegend = h("legend");
    this.preLabel = h("label");
    this.postLabel = h("label");
    this.el = h("div", { class: "dim-editor" },
      bar(h("label", { text: "Points" }), this.points, h("label", { text: "Label" }), this.label,
        h("label", { class: "check" }, this.snake, " snake scan"),
        h("label", { class: "check" }, this.after, " record after inner dims")),
      bar(h("span", { text: "Actions:", title: "Python code run at every point of this dimension; tick to add one" }),
        ...HOOKS.map(([name, title]) => h("label", { class: "check" }, this.hookChecks[name], " " + title.replace(" action", "").toLowerCase()))),
      this.hookNote,
      h("div", { class: "help", text: "Every point, in this order:" }),
      this.hooks.pre_sweep.el,
      h("fieldset", { class: "group" }, this.sweepLegend, this.table.el, bar(this.addBtn)),
      this.hooks.post_sweep.el,
      bar(this.preLabel, this.pre),
      this.hooks.pre_record.el,
      h("fieldset", { class: "group" }, this.recLegend, this.record.el),
      this.hooks.post_record.el,
      bar(this.postLabel, this.post));
    this._renumber();
  }
  _renumber() {
    let n = 0;
    const step = (hook, set) => {
      if (hook) { if (this.hookChecks[hook].checked) { n += 1; this.hooks[hook].legend.textContent = `${n}. ${HOOKS.find((x) => x[0] === hook)[1]}`; } return; }
      n += 1; set(n);
    };
    step("pre_sweep");
    step(null, (k) => { this.sweepLegend.textContent = `${k}. Set swept channels`; });
    step("post_sweep");
    step(null, (k) => { this.preLabel.textContent = `${k}. Pre-sample delay (s)`; });
    step("pre_record");
    step(null, (k) => { this.recLegend.textContent = `${k}. Record`; });
    step("post_record");
    step(null, (k) => { this.postLabel.textContent = `${k}. Post-sample delay (s)`; });
  }
  setChannels(writable, readable) { this.table.setChannels(writable); this.record.setNames(readable.concat(SPECIAL_RECORD)); }
  setActions(actions) { for (const ed of Object.values(this.hooks)) ed.setActions(actions); }
  toDict() {
    const d = { points: Math.max(1, Math.round(Number(this.points.value) || 1)), sweep: this.table.entries(),
                pre_delay: Number(this.pre.value) || 0, record: this.record.checked(), post_delay: Number(this.post.value) || 0,
                snake: this.snake.checked, record_after_inner: this.after.checked, label: this.label.value };
    const hooks = {};
    for (const [name] of HOOKS) {
      const code = this.hooks[name].code.value, on = this.hookChecks[name].checked;
      if (on || code.trim()) hooks[name] = { enabled: on, code };   // unticked code is kept, not run
    }
    if (Object.keys(hooks).length) d.hooks = hooks;
    return d;
  }
  load(d) {
    this.points.value = d.points ?? 1;
    this.pre.value = d.pre_delay || 0;
    this.post.value = d.post_delay || 0;
    this.snake.checked = !!d.snake;
    this.after.checked = !!d.record_after_inner;
    this.label.value = d.label || "";
    this.table.clear();
    for (const e of d.sweep || []) this.table.addRow(e);
    this.record.setChecked(d.record || []);
    const hooks = d.hooks || {};
    for (const [name] of HOOKS) {
      let hk = hooks[name] || {};
      if (typeof hk === "string") hk = { enabled: !!hk.trim(), code: hk };
      this.hooks[name].code.value = hk.code || "";
      this.hookChecks[name].checked = !!hk.enabled;
      this.hooks[name].el.hidden = !hk.enabled;
    }
    this._renumber();
  }
  setReadOnly(ro) {
    for (const w of [this.points, this.pre, this.post, this.label]) w.readOnly = ro;
    this.snake.disabled = ro; this.after.disabled = ro; this.addBtn.disabled = ro;
    this.addBtn.dataset.lockedRo = ro ? "1" : "";
    for (const cb of Object.values(this.hookChecks)) cb.disabled = ro;
    for (const ed of Object.values(this.hooks)) ed.setReadOnly(ro);
    this.table.setReadOnly(ro); this.record.setReadOnly(ro);
    this.ro = ro;
    this._note();
  }
  _note() {
    const used = Object.values(this.hookChecks).some((cb) => cb.checked);
    this.hookNote.textContent = !this.ro && used && !this.canHook()
      ? "Actions run Python on the backend and need the admin role (sweeps with action code are refused for your role)." : "";
  }
}

class PhaseEditor {
  constructor(phase, canHook) {
    this.phase = phase;
    this.channels = [];
    this.choices = {};
    this.listId = "ph-" + phase + Math.random().toString(36).slice(2);
    this.list = h("datalist", { id: this.listId });
    this.tbody = h("tbody");
    this.consts = [];
    this.record = new CheckList();
    this.hook = h("textarea", { class: "code", rows: 7, spellcheck: false,
      placeholder: `# Python executed ${phase === "pre" ? "before" : "after"} the sweep, e.g.\n# action('dmm.reset')\n# set('Tset', 4.2)\n# sleep(1)\n# log('ready, T = %g' % get('T'))` });
    this.actionBox = h("select", { title: "instrument actions (commands without arguments)" });
    this.addAction = btn("+ action", () => this.appendAction(), { title: "append the selected action to the hook code" });
    this.canHook = canHook;
    this.hookNote = h("div", { class: "help" });
    this.addConstBtn = btn("+ constant", () => this.addConst());
    this.el = h("div", { class: "phase" },
      h("div", { class: "help", text: phase === "pre" ? "Order: hook → constants → record" : "Order: record → constants → hook (always runs, also after errors/abort)" }),
      h("div", { class: "row2" },
        group("Constants", this.list, h("div", { class: "table-wrap" }, h("table", { class: "grid edit" },
          h("thead", {}, h("tr", {}, ["Channel", "Value (number or text)", ""].map((t) => h("th", { text: t })))), this.tbody)),
          bar(this.addConstBtn)),
        group("Record once", this.record.el)),
      group("Hook code", this.hook, bar(h("span", { text: "Action" }), this.actionBox, this.addAction), this.hookNote));
  }
  setChannels(writable, readable, choices = {}) {
    this.channels = writable;
    this.choices = choices;
    this.list.replaceChildren(...writable.map((n) => h("option", { value: n })));
    this.record.setNames(readable);
    for (const c of this.consts) c.sync();
  }
  setActions(actions) {
    const cur = this.actionBox.value;
    this.actionBox.replaceChildren(...actions.map((a) => h("option", { text: a, value: a })));
    if (actions.includes(cur)) this.actionBox.value = cur;
  }
  appendAction() {
    const ref = this.actionBox.value;
    if (!ref) return;
    let t = this.hook.value;
    if (t && !t.endsWith("\n")) t += "\n";
    this.hook.value = t + `action('${ref}')\n`;
  }
  addConst(e = {}) {
    const ch = h("input", { value: e.channel || "", class: "w-chan" });
    ch.setAttribute("list", this.listId);
    const val = h("input", { value: e.value ?? "", class: "w-expr" });
    // channels that only accept certain values suggest them (datalist drop-down)
    const opts = h("datalist", { id: this.listId + "-v" + Math.random().toString(36).slice(2) });
    val.setAttribute("list", opts.id);
    const c = { ch, val };
    c.sync = () => {
      const ch_ = this.choices[ch.value.trim()] || [];
      opts.replaceChildren(...ch_.map((x) => h("option", { value: String(x) })));
      val.title = ch_.length ? "one of: " + ch_.join(", ") : "";
    };
    ch.addEventListener("input", c.sync);
    c.sync();
    const rm = h("button", { text: "−", on: { click: () => { this.consts = this.consts.filter((x) => x !== c); tr.remove(); } } });
    c.rm = rm;
    const tr = h("tr", {}, h("td", {}, ch), h("td", {}, val, opts), h("td", {}, rm));
    ch.readOnly = val.readOnly = this.ro;
    rm.disabled = !!this.ro;
    this.consts.push(c);
    this.tbody.append(tr);
  }
  toDict() {
    const constants = [];
    for (const c of this.consts) {
      const ch = c.ch.value.trim(), t = c.val.value.trim();
      if (!ch) continue;
      const n = Number(t);
      constants.push({ channel: ch, value: t !== "" && Number.isFinite(n) ? n : t });
    }
    return { constants, record: this.record.checked(), hook: this.hook.value };
  }
  load(d = {}) {
    this.consts = [];
    this.tbody.replaceChildren();
    for (const c of d.constants || []) this.addConst(c);
    this.record.setChecked(d.record || []);
    this.hook.value = d.hook || "";
  }
  setReadOnly(ro) {
    this.ro = ro;
    for (const c of this.consts) { c.ch.readOnly = c.val.readOnly = ro; c.rm.disabled = ro; }
    this.addConstBtn.disabled = ro;
    this.record.setReadOnly(ro);
    const noHook = !this.canHook();
    this.hook.readOnly = ro;              // editable (so it can be cleared); the gateway enforces
    this.actionBox.disabled = ro || noHook;
    this.addAction.disabled = ro || noHook;
    this.hookNote.textContent = !ro && noHook ? "Hook code runs Python on the backend and needs the admin role; leave it empty (sweeps with hooks are refused for your role)." : "";
  }
}

export class SweepEditor {
  constructor(app, { readOnly = false } = {}) {
    this.app = app;
    this.conn = app.conn;
    this.model = app.model;
    this.readOnly = readOnly;
    this.name = h("input", { value: "sweep" });
    this.comment = h("input", { placeholder: "comment / sample (saved as metadata)" });
    this.onRead = h("select", { title: "what to do when a recording fails after all retries" }, ["nan", "abort"].map((v) => h("option", { text: v, value: v })));
    this.onSet = h("select", {}, ["abort", "skip"].map((v) => h("option", { text: v, value: v })));
    this.postOnAbort = h("input", { type: "checkbox", checked: true, title: "run the post-sweep record/constants/hook also when aborted" });
    this.ppt = h("input", { type: "checkbox", checked: true, title: "append a summary slide to the configured PowerPoint file" });
    this.dimList = h("div", { class: "dimlist" });
    this.dimStack = h("div", { class: "dimstack" });
    this.dims = [];
    this.cur = 0;
    this.status = h("div", { class: "msg" });
    const canHook = () => this.conn.can("run_script_now");
    this.pre = new PhaseEditor("pre", canHook);
    this.post = new PhaseEditor("post", canHook);
    this.dimButtons = [btn("+ dimension", () => this.addDimension()), btn("− dimension", () => this.removeDimension()),
                       btn("▲ inner", () => this.moveDimension(-1)), btn("▼ outer", () => this.moveDimension(1))];
    this.tabBtns = {};
    const tabbody = h("div", { class: "subtabbody" });
    const dimsHost = h("div", { class: "dims-host" },
      h("div", { class: "dimside" }, h("div", { class: "help", text: "Dim 1 = innermost (fastest)" }), this.dimList, ...this.dimButtons),
      this.dimStack);
    const pages = { Dimensions: dimsHost, "Pre-sweep": this.pre.el, "Post-sweep": this.post.el };
    const tabbar = h("div", { class: "subtabbar" });
    for (const [n, el] of Object.entries(pages)) {
      const b = h("button", { text: n, class: "subtab", on: { click: () => { for (const [m, e] of Object.entries(pages)) { e.hidden = m !== n; this.tabBtns[m].classList.toggle("active", m === n); } } } });
      this.tabBtns[n] = b;
      tabbar.append(b);
      tabbody.append(el);
      el.hidden = n !== "Dimensions";
    }
    this.tabBtns.Dimensions.classList.add("active");
    this.actionsBar = bar(
      btn("New", () => this.newSweep()), btn("Load…", () => this.loadFile(), { title: "load a sweep JSON file from this computer" }),
      btn("Load from data file…", () => this.loadFromData(), { cmd: "read_run_header list_runs" }),
      btn("Save…", () => this.saveFile(), { title: "download the sweep definition as JSON" }),
      btn("Validate", () => this.validate(), { cmd: "validate_sweep" }),
      btn("Add to queue", () => this.queue(false), { cmd: "queue_add" }),
      btn("Add & start", () => this.queue(true), { cmd: "queue_add", cls: "primary" }));
    this.el = h("div", { class: "panel sweep" },
      h("div", { class: "form head" }, h("label", { text: "Name" }), this.name, h("label", { text: "Comment" }), this.comment),
      bar(h("label", {}, "on read error ", this.onRead), h("label", {}, "on set error ", this.onSet),
        h("label", { class: "check" }, this.postOnAbort, " post actions on abort"), h("label", { class: "check" }, this.ppt, " PPT slide when done")),
      tabbar, tabbody, this.status, readOnly ? null : this.actionsBar);
    this.model.on("state", () => this.refreshChannels());
    this.newSweep();
    this.setReadOnly(readOnly);
  }

  _lists() {
    return [this.model.writableChannels(true), this.model.readableChannels()];
  }

  refreshChannels() {
    const [w, r] = this._lists();
    for (const d of this.dims) d.setChannels(w, r);
    const wall = this.model.writableChannels().concat(this.model.configChannels(true));
    const rall = r.concat(this.model.configChannels());
    const ch = this.model.channelChoices();
    this.pre.setChannels(wall, rall, ch); this.post.setChannels(wall, rall, ch);
    const acts = this.model.actions();
    this.pre.setActions(acts); this.post.setActions(acts);
    for (const d of this.dims) d.setActions(acts);
    this.setReadOnly(this.readOnly);
  }

  _relabel() {
    this.dimList.replaceChildren(...this.dims.map((d, i) => {
      let dd = {};
      try { dd = d.toDict(); } catch (e) { dd = { sweep: [], points: d.points.value }; }
      const sw = (dd.sweep || []).map((e) => e.channel).join(", ") || "—";
      return h("div", { class: "dimitem" + (i === this.cur ? " active" : ""), text: `Dim ${i + 1}: ${sw} ×${dd.points}`,
        on: { click: () => this.showDim(i) } });
    }));
  }

  showDim(i) {
    this.cur = Math.max(0, Math.min(i, this.dims.length - 1));
    this.dims.forEach((d, j) => { d.el.hidden = j !== this.cur; });
    this._relabel();
  }

  addDimension(d = null) {
    const ed = new DimensionEditor(() => this._relabel(), () => this.conn.can("run_script_now"));
    const [w, r] = this._lists();
    ed.setChannels(w, r);
    ed.setActions(this.model.actions());
    if (d) ed.load(d);
    this.dims.push(ed);
    this.dimStack.append(ed.el);
    this.showDim(this.dims.length - 1);
    if (this.readOnly) ed.setReadOnly(true);
    return ed;
  }

  removeDimension() {
    if (this.dims.length <= 1) return;
    const [ed] = this.dims.splice(this.cur, 1);
    ed.el.remove();
    this.showDim(this.cur);
  }

  moveDimension(delta) {
    const i = this.cur, j = i + delta;
    if (j < 0 || j >= this.dims.length) return;
    const defs = this.dims.map((d) => d.toDict());
    [defs[i], defs[j]] = [defs[j], defs[i]];
    this._loadDims(defs);
    this.showDim(j);
  }

  _loadDims(defs) {
    for (const d of this.dims) d.el.remove();
    this.dims = [];
    for (const d of defs) this.addDimension(d);
    this.showDim(0);
  }

  definition() {
    const meta = {};
    if (this.comment.value.trim()) meta.comment = this.comment.value.trim();
    return { name: this.name.value.trim() || "sweep", dimensions: this.dims.map((d) => d.toDict()),
             pre: this.pre.toDict(), post: this.post.toDict(), on_read_error: this.onRead.value,
             on_set_error: this.onSet.value, run_post_on_abort: this.postOnAbort.checked,
             ppt: this.ppt.checked, metadata: meta };
  }

  loadDefinition(d) {
    if (!d) { this.newSweep(); return; }
    d = toJSONable(d);
    this.name.value = String(d.name ?? "sweep");
    this.comment.value = String((d.metadata || {}).comment || "");
    this.onRead.value = d.on_read_error || "nan";
    this.onSet.value = d.on_set_error || "abort";
    this.postOnAbort.checked = d.run_post_on_abort !== false;
    this.ppt.checked = d.ppt !== false;
    this._loadDims((d.dimensions && d.dimensions.length) ? d.dimensions : [{ points: 11 }]);
    this.pre.load(d.pre || {});
    this.post.load(d.post || {});
    this.setReadOnly(this.readOnly);
  }

  newSweep() { this.loadDefinition({ name: "sweep", dimensions: [{ points: 11, sweep: [], record: [] }] }); }

  setReadOnly(ro) {
    this.readOnly = ro;
    this.name.readOnly = this.comment.readOnly = ro;
    for (const w of [this.onRead, this.onSet, this.postOnAbort, this.ppt]) w.disabled = ro;
    for (const b of this.dimButtons) b.hidden = ro;
    for (const d of this.dims) d.setReadOnly(ro);
    this.pre.setReadOnly(ro);
    this.post.setReadOnly(ro);
  }

  say(text, err = false) { this.status.textContent = text; this.status.className = "msg" + (err ? " err" : ""); }

  _collect() {
    try { return this.definition(); } catch (e) { this.say(e.message, true); return null; }
  }

  async loadFile() {
    const f = await pickFile();
    if (!f) return;
    try {
      let d = JSON.parse(f.text);
      if (d.definition && !d.dimensions) d = d.definition;
      this.loadDefinition(d);
      this.say(`loaded ${f.name}`);
    } catch (e) { this.say(`cannot load ${f.name}: ${e.message}`, true); }
  }

  saveFile() {
    const d = this._collect();
    if (!d) return;
    download((this.name.value.trim() || "sweep") + ".json", JSON.stringify(d, null, 2));
    this.say("sweep downloaded");
  }

  async loadFromData() {
    const run = await this.app.pickRun("Load sweep from data file");
    if (!run) return;
    try {
      const hdr = await this.conn.request("read_run_header", { path: run.path });
      this.loadDefinition(hdr.definition);
      this.say(`sweep loaded from ${run.name}`);
    } catch (e) { this.say(e.message, true); }
  }

  async validate() {
    const d = this._collect();
    if (!d) return null;
    try {
      const r = await this.conn.request("validate_sweep", { definition: d });
      const w = (r.warnings || []).length ? `  ⚠ ${r.warnings.join("; ")}` : "";
      this.say(`✔ ${r.total_points} points, shape ${r.shape.join("×")}, ≥ ${fmt(r.min_duration, 3)} s of delays${w}`);
      return r;
    } catch (e) { this.say(`✘ ${e.message}`, true); return null; }
  }

  async queue(start) {
    const d = this._collect();
    if (!d) return;
    try {
      const id = await this.conn.request("queue_add", { item: { kind: "sweep", definition: d }, start });
      this.say(`queued as item #${id}${start ? " (queue started)" : ""}`);
    } catch (e) { this.say(`✘ ${e.message}`, true); }
  }
}

/** Read-only view of the sweep that is running now. */
export class RunningSweepView {
  constructor(app) {
    this.app = app;
    this.editor = new SweepEditor(app, { readOnly: true });
    this.info = h("div", { class: "msg" });
    this.el = h("div", { class: "panel" }, bar(this.info,
      btn("Copy to editor", () => this.app.copyToEditor(this.editor.definition()))), this.editor.el);
    app.conn.on("event", (k) => { if (k === "sweep_start" || k === "sweep_end") this.refresh(); });
    app.conn.on("connected", () => this.refresh());
  }
  async refresh() {
    try {
      const cur = await this.app.conn.request("current_run");
      if (cur) { this.editor.loadDefinition(cur.definition); this.info.textContent = `run ${cur.run} – ${cur.name} [${cur.status}]`; }
      else this.info.textContent = "no sweep running";
    } catch (e) { this.info.textContent = e.message; }
  }
}
