// Run queue: state, queued items, history (rerun, open data), scripts.

import { h, Table, btn, bar, group, clock, confirmDialog, qcan } from "../ui.js";

const SCRIPT_HELP = `# Script API: get(name), set(name, value), action('inst.act'), sleep(s),
# log(msg), run_sweep(definition), instrument(name) ...
`;

const info = (r) => r.kind === "sweep" ? (r.shape || []).join("×") : r.kind === "script" ? `${(r.code || "").split("\n").length} lines` : "";

export class QueuePanel {
  constructor(app) {
    this.app = app;
    this.conn = app.conn;
    this.state = h("b", { text: "state: ?" });
    this.stopOnError = h("input", { type: "checkbox", on: { change: () => this._cmd("queue_options", { stop_on_error: this.stopOnError.checked }) } });
    this.stopOnError.dataset.cmd = "queue_options";
    this.current = h("div", { class: "msg", text: "current: —" });
    this.msg = h("div", { class: "msg" });
    this.queued = new Table([{ key: "id", label: "#" }, { key: "kind", label: "Kind" }, { key: "name", label: "Name" },
                             { key: "info", label: "Shape / info", fmt: (_, r) => info(r) }],
                            { empty: "queue is empty", onSelect: () => this._copyLabel(this.copyQueued, this.queued.current()) });
    this.queued.keyOf = (r) => r.id;
    this.history = new Table([{ key: "id", label: "#" }, { key: "kind", label: "Kind" }, { key: "name", label: "Name" },
      { key: "status", label: "Status", cls: (r) => ({ finished: "ok", terminated: "ok", error: "err", aborted: "warn" })[r.status] || "" },
      { key: "ended", label: "Ended", fmt: (v) => clock(v) },
      { key: "run", label: "Run / error", fmt: (_, r) => (r.error ? r.error : r.run !== undefined && r.run !== null ? `run ${r.run}` : "") }],
      { onActivate: (r) => this._openData(r), empty: "no history yet",
        onSelect: () => this._copyLabel(this.copyHist, this.history.current()) });
    this.copyQueued = btn("Copy to sweep editor", () => this._copy(this.queued.current()),
      { title: "copy the selected item into the sweep (or script) editor" });
    this.copyHist = btn("Copy to sweep editor", () => this._copy(this.history.current()),
      { title: "copy the selected item into the sweep (or script) editor" });
    this.copyQueued.disabled = this.copyHist.disabled = true;
    this.history.keyOf = (r) => r.id;
    this.scriptName = h("input", { value: "script", class: "w-chan" });
    this.script = h("textarea", { class: "code", rows: 8, spellcheck: false });
    this.script.value = SCRIPT_HELP + "set('Vg', 0.1)\nsleep(1)\nlog('I = %g' % get('I'))\n";
    this.el = h("div", { class: "panel queue" },
      bar(this.state,
        qcan(btn("▶ Start queue", () => this._cmd("queue_start"), { cmd: "queue_start", title: "run the queued items" }), "start"),
        qcan(btn("Stop after current", () => this._cmd("queue_stop"), { cmd: "queue_stop", title: "finish the running item, then stay idle" }), "stop"),
        qcan(btn("⏸ Pause", () => this._cmd("pause"), { cmd: "pause" }), "pause"),
        qcan(btn("Resume", () => this._cmd("resume"), { cmd: "resume" }), "resume"),
        qcan(btn("■ Abort current", () => this._cmd("abort"), { cmd: "abort", cls: "danger", title: "abort the running item; the queue continues" }), "abort"),
        qcan(btn("Abort all", () => this._cmd("abort_all"), { cmd: "abort_all", cls: "danger", title: "abort the running item and stop the queue" }), "abort_all"),
        h("label", { class: "check" }, this.stopOnError, " stop queue on error")),
      group("Queued", this.current, this.queued.el, bar(
        btn("▲ up", () => this._move(-1), { cmd: "queue_move" }), btn("▼ down", () => this._move(1), { cmd: "queue_move" }),
        btn("Remove", () => this._remove(), { cmd: "queue_remove" }),
        btn("Clear queue", async () => { if (await confirmDialog("Clear queue", "Remove all queued items?")) this._cmd("queue_clear"); }, { cmd: "queue_clear" }),
        this.copyQueued)),
      group("History", this.history.el, bar(
        btn("⟳ Rerun selected", () => this._rerun(), { cmd: "queue_add", title: "queue the selected sweep or script again and start" }),
        this.copyHist,
        btn("Open data", () => this._openData(this.history.current()), { cmd: "load_run" }))),
      group("Script (admin)", this.script, bar(h("span", { text: "Name" }), this.scriptName,
        btn("Add script to queue", () => this._script(false), { cmd: "queue_add run_script_now" }),
        btn("Run now (front of queue)", () => this._script(true), { cmd: "run_script_now" }))),
      this.msg);
    app.model.on("queue", (q) => this._queue(q));
  }

  say(t, err = false) { this.msg.textContent = t; this.msg.className = "msg" + (err ? " err" : ""); }

  async _cmd(cmd, args = {}) {
    try { await this.conn.request(cmd, args); this.say(`${cmd}: ok`); } catch (e) { this.say(`${cmd}: ${e.message}`, true); }
  }

  _queue(q) {
    this.state.textContent = `state: ${q.state}${q.processing ? " (running)" : ""}`;
    this.stopOnError.checked = !!q.stop_on_error;
    const c = q.current;
    this.current.textContent = c ? `current: #${c.id} ${c.kind} ${c.name || ""} ${c.run !== undefined ? `(run ${c.run})` : ""}` : "current: —";
    this.queued.set(q.queued || []);
    this.history.set((q.history || []).slice().reverse());
    this._copyLabel(this.copyQueued, this.queued.current());
    this._copyLabel(this.copyHist, this.history.current());
  }

  /** The copy button says where the selected item goes (sweep or script editor). */
  _copyLabel(b, r) {
    const script = !!r && r.kind === "script";
    b.textContent = script ? "Copy to script editor" : "Copy to sweep editor";
    b.disabled = !r || !((r.kind === "sweep" && r.definition) || (script && r.code !== undefined));
  }

  _copy(r) {
    if (!r) return;
    if (r.kind === "script") {
      this.script.value = r.code || "";
      this.scriptName.value = r.name || "script";
      this.script.focus();
      this.say(`script #${r.id} copied to the script editor`);
    } else if (r.definition) {
      this.app.copyToEditor(r.definition);
      this.say(`sweep #${r.id} copied to the sweep editor`);
    }
  }

  async _move(d) {
    const r = this.queued.current();
    if (!r) return;
    const ids = this.queued.rows.map((x) => x.id);
    const pos = Math.max(0, Math.min(ids.length - 1, ids.indexOf(r.id) + d));
    await this._cmd("queue_move", { item_id: r.id, position: pos });
  }

  async _remove() {
    const r = this.queued.current();
    if (r) await this._cmd("queue_remove", { item_id: r.id });
  }

  async _rerun() {
    const r = this.history.current();
    if (!r) { this.say("select a history item", true); return; }
    let item;
    if (r.kind === "sweep" && r.definition) item = { kind: "sweep", definition: r.definition };
    else if (r.kind === "script" && r.code !== undefined) item = { kind: "script", code: r.code, name: r.name || "script" };
    else { this.say("this history item cannot be rerun", true); return; }
    try { const id = await this.conn.request("queue_add", { item, start: true }); this.say(`rerun queued as item #${id}`); }
    catch (e) { this.say(e.message, true); }
  }

  _openData(r) {
    if (!r || !r.path) { this.say("no data file for this item", true); return; }
    this.app.openRun(r.path);
  }

  async _script(now) {
    const code = this.script.value, name = this.scriptName.value.trim() || "script";
    try {
      const id = now ? await this.conn.request("run_script_now", { code, name })
        : await this.conn.request("queue_add", { item: { kind: "script", code, name } });
      this.say(`script queued as item #${id}`);
    } catch (e) { this.say(e.message, true); }
  }
}
