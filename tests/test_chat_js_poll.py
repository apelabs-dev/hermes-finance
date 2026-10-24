"""TASK-23 review correction C5 (client): an Ask card left "approved" (claimed by an Approve that is still being applied,
maybe from another tab or Settings) is asked for again until it reaches a final state, and never drawn back as pending.

No browser: static/chat.js runs in node (vm) with app.js's building blocks replaced by a minimal element tree, the
/api/chat/cards answers scripted, and setTimeout under the test's control (so the backoff costs no wall time)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

CHAT_JS = Path(__file__).resolve().parent.parent / "src" / "hermes_finance" / "static" / "chat.js"

HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const plan = JSON.parse(process.argv[3]);   // {answers: ['approved' | 'applied' | 'failed' | 'error', ...], closeAfter,
                                            //  reopenDuring: the loop step whose ask is held while the panel closes and opens}

function el(tag) {
  return {tag, kids: [], dataset: {}, style: {setProperty() {}}, hidden: false, disabled: false, value: '',
    scrollTop: 0, scrollHeight: 0, attrs: {}, parentElement: null,
    classList: {add() {}, remove() {}, contains() { return false; }},
    setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {}, focus() {},
    append(...k) { for (const x of k) { if (x && typeof x === 'object') x.parentElement = this; this.kids.push(x); } },
    querySelectorAll() { return []; }};
}
function flat(kids) { return kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ''); }
function h(tag, props, ...kids) {
  const e = el(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'dataset') Object.assign(e.dataset, v);
    else if (k === 'hidden') e.hidden = true;
    else e.attrs[k] = v;
  }
  e.append(...flat(kids));
  return e;
}
function fill(e, ...kids) { e.kids = []; e.append(...flat(kids)); return e; }

const timers = [];
let held = null;   // {promise, release}: the ask being held (reopenDuring)
let tid = 0;
const calls = [];
let refreshed = 0;
const answers = plan.answers.slice();
const card = (status) => ({id: 7, tool: 'finance_set_cap', title: 'Raise Shopping cap to $900?', summary: 's', status,
  result_line: status === 'applied' ? 'Saved: Shopping cap is now $900' : status === 'approved' ? 'Approved, applying it…' : 'x',
  client: 'Ask'});
const HF = {h, fill, icon: () => el('i'), glyph: () => el('i'), money: String, money2: String, plural: (n, w) => `${n} ${w}`,
  budgetRing: () => el('svg'), ibadge: () => el('i'), RGB: {acc: '0', warn: '0'},
  refresh() { refreshed++; }, loadCounts() {},
  async api(path, q) {
    calls.push({path, ids: q && q.ids});
    const a = answers.length > 1 ? answers.shift() : answers[0];
    if (held) await held.promise;
    if (a === 'error') throw new Error('unreachable');
    return {cards: [card(a)]};
  },
  async post() { throw new Error('not in this test'); }};
const store = {'hf.chat.v1': JSON.stringify([{role: 'user', content: 'Raise it?'},
  {role: 'assistant', content: 'Approve the card if you want it.', cards: [7]}])};
const body = el('body');
body.dataset.ask = 'api';
const sandbox = {
  document: {body, addEventListener() {}, getElementById: () => el('div')},
  sessionStorage: {getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = v; }},
  setTimeout: (fn, ms) => { const id = ++tid; timers.push({id, fn, ms}); return id; },
  clearTimeout: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
  console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
for (const ext of sandbox.HF_EXT) ext(HF);
const panel = body.kids.find((k) => k.attrs.id === 'chat');

function statusOf(e) {
  if (e && e.dataset && e.dataset.card === '7') return e.dataset.status;
  for (const k of (e && e.kids) || []) { const s = statusOf(k); if (s) return s; }
  return null;
}
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };

(async () => {
  const seen = [];
  const waits = [];
  await HF.chat.open();
  await settle();
  seen.push(statusOf(panel));
  for (let i = 0; i < 12 && timers.length; i++) {
    if (plan.closeAfter != null && i === plan.closeAfter) { HF.chat.close(); await settle(); continue; }
    const t = timers.shift();
    waits.push(t.ms);
    if (plan.reopenDuring === i) {   // the ask is out; meanwhile the panel is closed and opened again
      let release;
      held = {promise: new Promise((r) => { release = r; })};
      const out = t.fn();
      await settle();
      held = null;
      HF.chat.close();
      await settle();
      await HF.chat.open();
      await settle();
      release();
      await out;
    } else {
      await t.fn();
    }
    await settle();
    seen.push(statusOf(panel));
    if (plan.reopenDuring === i) break;
  }
  process.stdout.write(JSON.stringify({calls, seen, waits, pending_timers: timers.length, refreshed}));
})().catch((e) => { process.stdout.write(JSON.stringify({crash: String(e && e.stack || e)})); });
"""


def run(answers: list[str], close_after: int | None = None, reopen_during: int | None = None) -> dict:
    node = shutil.which("node")
    assert node, "node is needed for the chat.js poll test (it runs chat.js without a browser)"
    p = subprocess.run([node, "-e", HARNESS, "-", str(CHAT_JS), json.dumps({"answers": answers, "closeAfter": close_after, "reopenDuring": reopen_during})],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert "crash" not in out, out
    return out


def test_an_approved_card_is_asked_for_again_until_it_is_final():
    out = run(["approved", "approved", "applied"])
    assert out["seen"] == ["approved", "approved", "applied"]
    assert [c["ids"] for c in out["calls"]] == ["7", "7", "7"] and len(out["calls"]) == 3
    assert out["pending_timers"] == 0                             # final: it stops asking
    assert out["refreshed"] == 1                                  # and the page shows the change
    assert out["waits"] == sorted(out["waits"]) and out["waits"][0] >= 500   # a backoff, not a busy loop


def test_a_failed_poll_keeps_the_card_approved_and_asks_again():
    out = run(["approved", "error", "failed"])
    assert out["seen"] == ["approved", "approved", "failed"]     # never drawn back as pending (no live buttons)
    assert "pending" not in out["seen"] and out["pending_timers"] == 0 and out["refreshed"] == 0


def test_closing_the_panel_stops_asking_and_the_backoff_is_capped():
    out = run(["approved"], close_after=6)
    assert out["seen"] == ["approved"] * 7 and len(out["calls"]) == 7
    assert out["pending_timers"] == 0                             # closed: no timer left behind
    assert out["waits"] == [1000, 2000, 4000, 8000, 15000, 15000]


def test_closing_and_opening_while_an_ask_is_out_keeps_one_chain():
    out = run(["approved"], reopen_during=1)
    assert out["pending_timers"] == 1                             # one timer asks next, not two
    assert "pending" not in out["seen"]
