/* Minimal DOM shim to execute the real static/index.html script in node,
   click each preset and capture the JSON payload handed to fetch(). */
const fs = require("fs");
const path = require("path");

function makeMatcher(sel) {
  const tag = (sel.match(/^[a-z]+/i) || [])[0] || null;
  const classes = [...sel.matchAll(/\.([\w-]+)/g)].map(m => m[1]);
  const attrM = sel.match(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/);
  return (el) => {
    if (tag && el.tag.toLowerCase() !== tag.toLowerCase()) return false;
    const cls = (el.className || "").split(/\s+/);
    if (!classes.every(c => cls.includes(c))) return false;
    if (attrM) {
      const [, name, val] = attrM;
      if (!(name in el)) return false;
      if (val !== undefined && String(el[name]) !== val) return false;
    }
    return true;
  };
}

class El {
  constructor(tag) {
    this.tag = tag;
    this.className = "";
    this.children = [];
    this.parent = null;
    this.style = {};
    this.value = "";
    this._inner = "";
    this.textContent = "";
    this.title = "";
    this.type = "";
    this.dataset = {};
    this.handlers = {};
  }
  get innerHTML() { return this._inner; }
  set innerHTML(v) {
    this._inner = v;
    if (v === "") this.children = [];
  }  append(...kids) { kids.forEach(k => this.appendChild(k)); }
  appendChild(k) { k.parent = this; this.children.push(k); return k; }
  insertBefore(n, ref) {
    n.parent = this;
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, n);
    return n;
  }
  remove() {
    if (this.parent) {
      const i = this.parent.children.indexOf(this);
      if (i >= 0) this.parent.children.splice(i, 1);
      this.parent = null;
    }
  }
  addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); }
  dispatch(ev) { (this.handlers[ev] || []).forEach(fn => fn({ target: this })); }
  _descendants(out = []) {
    for (const c of this.children) { out.push(c); c._descendants(out); }
    return out;
  }
  querySelectorAll(sel) {
    const m = makeMatcher(sel);
    return this._descendants().filter(m);
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    const m = makeMatcher(sel);
    let n = this;
    while (n) { if (m(n)) return n; n = n.parent; }
    return null;
  }
  get options() {
    return this.tag === "select" ? this.children.filter(c => c.tag === "option") : [];
  }
}

const ids = ["auditId", "initialRows", "txnList", "txnCount", "verdict",
  "rawJson", "replayBadge", "submitBtn", "fetchBtn", "addInitRow", "addTxn", "editor",
  "resolutionSection", "resolutionId", "resolveBtn", "resolutionVerdict",
  "resolutionRaw", "resolutionRawWrap", "resolutionReplayBadge"];
const byId = {};
ids.forEach(id => { byId[id] = new El("div"); byId[id].id = id; });

const presetButtons = ["stale", "skew", "serial"].map(p => {
  const b = new El("button"); b.dataset.preset = p; return b;
});

const captured = [];
const resolutions = [];
global.Option = class extends El {
  constructor(text, value) { super("option"); this.textContent = text; this.value = value; }
};
global.document = {
  getElementById: id => byId[id],
  querySelectorAll: sel => sel === "button[data-preset]" ? presetButtons : [],
  createElement: tag => new El(tag),
};
global.fetch = async (url, opts) => {
  if (url === "/api/resolutions") {
    const req = JSON.parse(opts.body);
    resolutions.push(req);
    const body = {
      status: "RESOLVED", audit_id: req.audit_id, resolution_id: req.resolution_id,
      revoked_transactions: ["T1"], revocation_count: 1,
      criterion: "minimum revoked-transaction count; ties broken by the lexicographically smallest ascending transaction-id set",
      residual_vertices: ["T2"], serial_order: ["T2"],
      residual_edges: [],
      recomputation: { final_state: { x: -100, y: 100 }, reads_in_order: { T2: [] } },
    };
    return {
      ok: true, status: 201,
      headers: { get: h => h === "X-Resolution-Replayed" ? "false" : null },
      json: async () => body,
    };
  }
  captured.push({ url, body: JSON.parse(opts.body) });
  const p = JSON.parse(opts.body);
  const body = p.audit_id === "case-write-skew"
    ? {
        status: "NOT_SERIALIZABLE", audit_id: p.audit_id, read_checks: [],
        cycle: { length: 2, vertices: ["T1", "T2"], edges: [] },
        edges: [
          { from: "T1", to: "T2", type: "rw", key: "x", from_step: 0, to_step: 1, reason: "" },
          { from: "T2", to: "T1", type: "rw", key: "y", from_step: 0, to_step: 1, reason: "" },
        ],
      }
    : { status: "SERIALIZABLE", serial_order: [], read_checks: [], edges: [] };
  return {
    ok: true,
    status: 201,
    headers: { get: () => "false" },
    json: async () => body,
  };
};

const html = fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
new Function(script)();

let failures = 0;
function check(name, cond, extra) {
  console.log((cond ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : ""));
  if (!cond) failures++;
}

function submitAndCapture(preset) {
  captured.length = 0;
  presetButtons.find(b => b.dataset.preset === preset).onclick();
  return byId.submitBtn.onclick();
}

(async () => {
  // serial preset
  await submitAndCapture("serial");
  let p = captured[0].body;
  check("serial: audit_id", p.audit_id === "case-serializable");
  check("serial: initial", JSON.stringify(p.initial) === JSON.stringify({ x: 1 }));
  check("serial: 3 txns", p.transactions.length === 3);
  check("serial: T1 write", p.transactions[0].steps[0].op === "write" &&
    p.transactions[0].steps[0].value === 2);
  check("serial: T2 read observes writer T1",
    p.transactions[1].steps[0].observed.source === "txn" &&
    p.transactions[1].steps[0].observed.writer === "T1");
  check("serial: T3 read observes T2",
    p.transactions[2].steps[0].observed.writer === "T2");

  // stale preset
  await submitAndCapture("stale");
  p = captured[0].body;
  check("stale: audit_id", p.audit_id === "case-stale-read");
  check("stale: T2 reads initial after T1 commit",
    p.transactions[1].steps[0].op === "read" &&
    p.transactions[1].steps[0].observed === "initial");

  // skew preset
  await submitAndCapture("skew");
  p = captured[0].body;
  check("skew: audit_id", p.audit_id === "case-write-skew");
  check("skew: T1 reads x then writes y",
    p.transactions[0].steps[0].key === "x" && p.transactions[0].steps[1].key === "y");
  check("skew: T2 reads y then writes x",
    p.transactions[1].steps[0].key === "y" && p.transactions[1].steps[1].key === "x");

  // ---- stable resolution flow on the frozen cyclic source ----
  const sec = byId.resolutionSection;
  check("cyclic verdict reveals resolution section", sec.style.display === "block");
  check("resolution id prefilled from source",
    byId.resolutionId.value === "resolve-case-write-skew", byId.resolutionId.value);
  await byId.resolveBtn.onclick();
  check("resolution submitted to source",
    resolutions.length === 1 &&
    resolutions[0].audit_id === "case-write-skew" &&
    resolutions[0].resolution_id === "resolve-case-write-skew",
    JSON.stringify(resolutions[0]));
  check("resolution plan rendered (revoked + serial order)",
    byId.resolutionVerdict.innerHTML.includes("处置成功") &&
    byId.resolutionVerdict.innerHTML.includes("T1") &&
    byId.resolutionVerdict.innerHTML.includes("T2"));

  // changing the resolution marker must clear the old plan
  byId.resolutionId.value = "resolve-different-marker";
  byId.resolutionId.dispatch("input");
  check("marker change clears old plan",
    byId.resolutionVerdict.innerHTML.includes("处置标识已更换，旧方案已清除"),
    byId.resolutionVerdict.innerHTML.slice(0, 50));
  await byId.resolveBtn.onclick();
  check("resolution resubmitted under new marker",
    resolutions[1].resolution_id === "resolve-different-marker");

  // switching the source (loading another preset/submitting an acyclic one)
  // must clear the old plan and hide the section
  await submitAndCapture("serial");
  check("source change hides resolution section and clears old plan",
    sec.style.display === "none" &&
    byId.resolutionVerdict.innerHTML === "",
    `display=${sec.style.display}`);

  // back to skew: section reappears, plan starts empty
  await submitAndCapture("skew");
  check("cyclic source again reveals section", sec.style.display === "block");
  check("old plan did not resurface with the source",
    !byId.resolutionVerdict.innerHTML.includes("处置成功"));

  // dirty: after a successful submit renders a verdict (dirty reset),
  // any subsequent input on the editor must clear the old evidence.
  await new Promise(r => setTimeout(r, 0));
  const rendered = byId.verdict.innerHTML.includes("serializable") ||
    byId.verdict.innerHTML.includes("chip");
  byId.editor.dispatch("input");
  check("verdict rendered before dirtying (precondition)", rendered);
  check("dirty clears old evidence",
    byId.verdict.innerHTML.includes("旧证据已清除"), byId.verdict.innerHTML.slice(0, 60));
  check("dirty clears resolution plan too",
    sec.style.display === "none" && byId.resolutionVerdict.innerHTML === "");

  process.exit(failures ? 1 : 0);
})();
