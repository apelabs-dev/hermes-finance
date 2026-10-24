"""Gate 1 (TASK-11 final review): the numbers audit.

    uv run python scripts/audit_numbers.py <base_url> [--db PATH/finance.db] [--json OUT.json]

Pulls every API payload of a running dashboard (`finance serve`, or tests/cdp.Server over a synthetic DB), the rendered
DOM text of every page (headless Chrome through tests/cdp.py: temp profile, random port, every https:// request and
plaid.com blocked), and with --db the read-only agent CLI (`finance budget-status`, `finance spend`, `finance cashflow`
on that DB, opened read-only; FINANCE_DATA_DIR is pointed at its folder and HERMES_ENV_FILE at nothing, so the CLI
can't read any real configuration). It checks every invariant below to ±$0.01 and prints PASS/FAIL with both values.
Exit status 1 on any FAIL. It never writes: the only requests are GETs.

Net worth, budgets (left = budgeted - spent - bills still due), spending, recurring (rename-aware lateness, bills due),
investments, debt (payoff dates re-amortized independently), cash flow, vehicle and property values (TASK-12, TASK-13b:
each row's value, "Trade-in $X · Carfax · Sep 26" / "Estimated $X · Redfin · Sep 26" or "Your value", against the API,
the asset_valuations table and `finance networth`), each car / property page (TASK-13b: its value, its value table =
detail() = the stored Carfax / Redfin figures, equity = value - the LINKED loan, the history), the net worth switch
(TASK-14 / TASK-15: in both modes, All and Custom flipped on the Accounts page, the sidebar = the Overview tile = the
Accounts figure = `finance networth [--custom] --json`; Custom = every account but the saved selection, its net = the sum
of the counted accounts' signed balances; the groups it leaves out entirely marked "Not counted", the ones in part "N of
M counted" with their counted total; the "Not counted" tags; the chart on custom_net; the Edit sheet: every account
with its switch, the preview = Custom, one switch moves it by exactly that account, Cancel saves nothing; the hidden
--cash-only = the Cash & debts preset), every budget ring (TASK-14b: Overview "Left to spend" and its Budgets rows, the
Budgets summary, each category card: data-ring-pct = round((spent + bills still due) / cap x 100) and data-ring-status =
the status rule on spent + due, against `finance budget-status --json`; the %, label, arcs and aria-label from the same
numbers; the Overview and Budgets summary rings the same), every percentage the DOM shows (recomputed from the numbers
it's shown with, within 0.5 point) and every chart axis (max tick >= data max, labels at the tick's magnitude: the 10x
bug class).

TASK-19 (Batch C), on every DB and in full on the `features` one: the bills calendar on Recurring (its month total = that
month's bills from /api/recurring, worked out again here: the last charge's day if it falls in the month, the next
expected day and the ones after it at the item's cadence; this month and next; the page's total, paid and still-due
figures and its chips), goals (a whole-account goal's progress = that account's balance; per account the earmarks the
balance covers = min(earmarks, balance) and the shortfall flagged = the rest; Monthly savings = the month's cash flow
net; each card's % and the Overview tile), split transactions (the lines add up to the amount to the cent, each
category's spend = the tables summed again with split lines in place of their transaction, and the month total is the
same as without the splits), budget rollover (effective cap = cap + max(cap - last month's spend, 0), to the cent; the
card's "Budget $X + $Y rolled over") and the Alpaca live account (equity = its positions + cash; counted in net worth
exactly once; its Investments value and row).

TASK-21 (check_bills_task21), in full on the `features` one: bill totals, bills still due and Left to spend agree between
the API, the CLI (`finance bills`, `finance budget-status`) and the Hermes plugin (finance_bills, finance_budget_status),
every bill's fields too; the monthly totals re-normalized here (amount x how often it comes a month); a bill every 2 / 3 /
6 months in this month's still due only when it's expected this month; a changing bill's estimate (the median of its last
3 charges, or with fewer than 3 the middle of the usual amount the owner gave), its usual range and its outlier tag
worked out again from the database; "last paid from" = the account of its last charge; each Recurring row's "~$X est."
and "usually $a–b" (exact amounts otherwise), its schedule and outlier tag; calendar chips of a changing bill in whole
dollars.
"""
from __future__ import annotations

import argparse
import calendar
import http.client
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from cdp import Chrome  # noqa: E402

TOL = 0.01          # dollars
PCT_TOL = 0.5       # percentage points, for a percentage shown next to the numbers it's made of
# A budget ring never shows "100%" unless it's exactly at the cap (99.8% shows 99%, 100.3% shows 101%), so its %
# can sit up to 1 point from the true ratio in that band; the exact-value ring check (data-ring-pct) pins it precisely.
def ring_tol(true_pct: float) -> float:
    return 1.0 if 99.5 <= true_pct < 100.5 and true_pct != 100 else PCT_TOL
PAGES = ("overview", "transactions", "budgets", "recurring", "accounts", "investments", "debt", "goals")
# TASK-21, written again here (not imported from insights): the cadences charged in lumps (a bill still due counts in its
# month), how often each comes a month, their longest interval, and how many days / months a calendar step is
PERIODIC = ("monthly", "bimonthly", "quarterly", "semiannual", "yearly")
PER_MONTH = {"weekly": 52 / 12, "biweekly": 26 / 12, "monthly": 1, "bimonthly": 1 / 2, "quarterly": 1 / 3, "semiannual": 1 / 6,
             "yearly": 1 / 12}
STEP_DAYS, STEP_MONTHS = {"weekly": 7, "biweekly": 14}, {"monthly": 1, "bimonthly": 2, "quarterly": 3, "semiannual": 6, "yearly": 12}
MONEY_RE = re.compile(r"([−\-+]?)\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
# Percentages that are fixed thresholds, not computed numbers (the 30% utilization marker and its advice).
STATIC_PCT = {"30%", "Under 30% is great", "Well under 30%", "Over 30%"}


def money(text: str | None) -> float | None:
    """'−$1,234.56' / '≈ −$1,075.66' / '+$2,353.10' / '$4,011' -> number (None when there's no amount)."""
    if text is None:
        return None
    m = MONEY_RE.search(text.replace(" ", " "))
    if not m:
        return None
    v = float(m.group(2).replace(",", ""))
    return -v if m.group(1) in ("−", "-") else v


def monies(text: str) -> list[float]:
    return [(-1 if s in ("−", "-") else 1) * float(v.replace(",", "")) for s, v in MONEY_RE.findall(text.replace(" ", " "))]


def attr_num(raw: str | None) -> float | None:
    """A number the page published as a data attribute (2026-10-02).

    `Still due` and the rollover moved into the Details panel, so there is no visible money text to scrape any more.
    The renderer writes the raw value to `data-due` / `data-rollover` and this reads it back. Absent or unparseable
    is None — a FAIL the caller reports, never a crash and never a silent 0.0.
    """
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def pct_in(text: str | None) -> float | None:
    # nothing shown: None (a FAIL, never a crash); TASK-22f: a negative share is signed with U+2212, like money
    m = re.search(r"([-\u2212]?[0-9]+(?:\.[0-9]+)?)%", text or "")
    return float(m.group(1).replace("\u2212", "-")) if m else None


def whole(v: float) -> str:
    """The page's money() for a figure: whole dollars, half up, U+2212 for a negative ("−$1,076")."""
    n = math.floor(abs(v) + 0.5)
    return ("\u2212" if v < 0 and n else "") + f"${n:,}"


def money_text(v: float) -> str:
    """The shared money format (src/hermes_finance/money.py, static/app.js money2): cents unless whole once rounded."""
    from decimal import Decimal, ROUND_HALF_UP
    a = abs(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return ("-" if v < 0 and a else "") + f"${a:,.{0 if a == a.to_integral_value() else 2}f}"


def transit_ui(e: dict) -> str:
    """TASK-22f: a payment in transit's line under Own / Owe (static/app.js transitLine)."""
    inst = "Citi" if e.get("institution") == "Citibank Online" else e.get("institution") or "Card"
    return f"{money_text(e['amount'])} {inst} payment in transit \u00b7 counted once"


def r2(x: float) -> float:
    return round(x + 0.0, 2)


def nth(xs: list, i: int):
    """xs[i], or None when the page shows fewer numbers than expected (that's then a FAIL, never a crash)."""
    return xs[i] if -len(xs) <= i < len(xs) else None


class Audit:
    def __init__(self, log=None) -> None:
        self.results: list[dict] = []
        self.section = ""
        self.log = log or (lambda line: print(line, flush=True))

    def sect(self, name: str) -> None:
        self.section = name
        self.log(f"\n== {name} ==")

    def _add(self, ok: bool, name: str, got, want, note: str = "") -> bool:
        self.results.append({"section": self.section, "check": name, "ok": ok, "got": got, "want": want, "note": note})
        self.log(f"{'PASS' if ok else 'FAIL'}  {name}  [got {got!r}, want {want!r}]" + (f"  ({note})" if note else ""))
        return ok

    def eq(self, name: str, got, want, tol: float = TOL, note: str = "") -> bool:
        """Numbers (and lists of numbers, element by element) within `tol`; anything else exactly."""
        num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)  # noqa: E731
        if num(want) and num(got):
            return self._add(abs(got - want) <= tol + 1e-9, name, r2(got), r2(want), note)
        if isinstance(got, list) and isinstance(want, list) and want and all(num(x) for x in want) and all(num(x) for x in got):
            ok = len(got) == len(want) and all(abs(a - b) <= tol + 1e-9 for a, b in zip(got, want))
            return self._add(ok, name, [r2(x) for x in got], [r2(x) for x in want], note)
        return self._add(got == want, name, got, r2(want) if num(want) else want, note)

    def ok(self, name: str, cond: bool, got=None, want=None, note: str = "") -> bool:
        return self._add(bool(cond), name, got, want, note)

    def fails(self) -> list[dict]:
        return [r for r in self.results if not r["ok"]]


class Site:
    """GETs against the dashboard (Host header = the base URL's host, which the server accepts on loopback)."""

    def __init__(self, base: str) -> None:
        u = urlsplit(base)
        self.base, self.host, self.port = base.rstrip("/"), u.hostname, u.port or 80
        self.cache: dict[str, dict] = {}

    def get(self, path: str, **params) -> dict:
        url = path + ("?" + urlencode({k: v for k, v in params.items() if v is not None}) if params else "")
        if url not in self.cache:
            c = http.client.HTTPConnection(self.host, self.port, timeout=60)
            c.request("GET", url, headers={"Accept": "application/json"})
            r = c.getresponse()
            body = r.read()
            c.close()
            if r.status != 200:
                raise RuntimeError(f"GET {url}: {r.status} {body[:200]!r}")
            self.cache[url] = json.loads(body)
        return self.cache[url]

    def fresh(self, path: str) -> dict:
        self.cache.pop(path, None)
        return self.get(path)


# --- DOM extraction (one JS program per page; everything read is what the page shows) -----------------------------

DOM_COMMON = r"""
const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
const cardBy = (title) => [...document.querySelectorAll('#page .card')].find((c) => T(c.querySelector('h3 .ttl')) === title
  || T(c.querySelector('.hd b')) === title);
const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !el.closest('[hidden]'); };
// A figure that now lives behind an explanation disclosure (text-density: the long lists and the per-row breakdown
// moved out of the main reading path). Opens the trigger the way a reader would and reads the panel, so the same
// numbers are checked there instead of on the card. Returns null when the trigger isn't there.
const helpText = (scope, key) => {
  const b = scope && scope.querySelector(`[data-help=${key}]`);
  if (!b) return null;
  b.click();
  const p = document.querySelector('.help-panel');
  const out = p ? T(p) : null;
  b.click();
  return out;
};
// One fact of a row's own panel (its <dt> label's <dd>), or, with `sel`, the text of that element of the panel.
const helpFact = (row, label, sel = null) => {
  const b = row && row.querySelector('.nmrow > .help-trigger');
  if (!b) return null;
  b.click();
  const p = document.querySelector('.help-panel');
  const dd = p && (sel ? p.querySelector(sel) : [...p.querySelectorAll('.help-facts dt')].find((t) => T(t) === label));
  const out = dd ? T(sel ? dd : dd.nextElementSibling) : null;
  b.click();
  return out;
};
// Every visible text node with a percentage, and what it belongs to (`kind` + `key`, read from the component around
// it), so the audit can recompute it. A percentage in a place this doesn't know gets kind 'unknown' (a FAIL).
const first = (el) => (el && el.firstChild ? el.firstChild.textContent.trim() : null);
// "APR 3.81%", "at 3.81% APR", "Interest rate 3.81%": a rate, checked against the stored APR, never a "% paid". The
// words are in the text itself, or (a bare "3.81%") in its column label or right beside it ("APR <b>3.81%</b>"; not
// anywhere in the line: "· <b>20%</b> of $6,000 · 25.99% APR" has a utilization before the APR).
const RATE = /\bAPR\b|interest|\brate\b/i;
const beside = (n) => {
  const x = n.parentElement.childNodes.length === 1 ? n.parentElement : n;
  return `${x.previousSibling ? x.previousSibling.textContent : ''} ${x.nextSibling ? x.nextSibling.textContent : ''}`;
};
const isRate = (el, node, n) => RATE.test(node) || (/^\s*[−-]?[0-9.]+%\s*$/.test(node)
  && RATE.test(`${(el.closest('[data-l]') || {dataset: {}}).dataset.l || ''} ${n ? beside(n) : ''}`));
const pctCtx = (el, node, n) => {
  const c = place(el, node);
  return isRate(el, node, n) ? {kind: 'apr', key: c.key, of: c.kind} : c;
};
const place = (el, node) => {
  const card = el.closest('.card');
  const title = card ? T(card.querySelector('h3 .ttl')) : null;
  if (el.closest('.crow')) return {kind: 'where'};
  if (el.closest('.mchg')) return {kind: 'acct_month', key: title};   // polish: an Accounts group's change this month
  const bc = el.closest('#page tr.c');   // polish P2A: a budget is a row of the grouped table
  if (bc) return {kind: 'budget_card', key: bc.dataset.category};
  if (el.closest('.bsum')) return {kind: 'budget_summary'};
  if (title === 'Left to spend') return {kind: 'left_ring'};
  const al = el.closest('.al');
  if (al) return {kind: 'alert', key: first(al.querySelector('span > b')), group: al.dataset.group || null};   // polish P2E: a group budget's
  if (title === 'Investments' && el.closest('.tl2')) return {kind: 'invest_top'};
  if (title === 'Loans & debt' && el.closest('.tl2')) return node.includes('used') ? {kind: 'cards_util'} : {kind: 'loan_paid', key: first(el.closest('span'))};
  const dl = el.closest('.dl');
  if (dl) {
    const key = first(dl.querySelector('.grow > b'));
    return {kind: /paid off/.test(node) ? 'loan_paid' : 'card_util', key};
  }
  if (el.closest('.win-t')) return {kind: 'win', key: first(el.closest('.dg') && el.closest('.dg').querySelector('.hd b'))};
  const dg = el.closest('.dg');
  if (dg) {
    const li = el.closest('.xtra .li');
    if (li) return {kind: 'card_util', key: first(li.querySelector('.grow > b'))};
    if (/utilization/.test(node)) return {kind: 'cards_util'};
    return {kind: 'group_paid', key: first(dg.querySelector('.hd b'))};
  }
  if (title && title.endsWith(' payoff')) return {kind: 'loan_paid', key: title.slice(0, -7)};
  const acct = el.closest('.li.acct');
  if (acct) return {kind: 'plaid_util', key: first(acct.querySelector('.grow > b'))};
  if (el.closest('.note') && /in limits/.test(T(el.closest('.note')))) return {kind: 'accounts_cards_util'};
  const man = el.closest('[data-edit], [data-loan]');
  if (man && man.classList.contains('li')) return {kind: /paid off/.test(node) ? 'loan_paid' : 'manual_util', key: first(man.querySelector('.grow > b'))};
  // Approved density mock D1 B: a manual Accounts row is a .li.r2 (data-account "manual:<id>", its data-edit / data-loan
  // on the name's control) and its figure leads line two: "27% of $4,000 · updated …" (balance / limit), "81% paid off
  // · updated …" (the loan's progress)
  const mr = el.closest('.li.r2[data-account^="manual:"]');
  if (mr && el.closest('.meta2')) return {kind: /paid off/.test(node) ? 'loan_paid' : 'manual_util', key: T(mr.querySelector('.nmrow .nm'))};
  const lgr = el.closest('.lgr');
  if (lgr && title === 'Allocation') return {kind: 'alloc', key: first(lgr.querySelector('.grow > b')),
    mode: (card.querySelector('.seg button[aria-pressed=true]') || {dataset: {}}).dataset.k};
  if (lgr && title === 'Portfolio breakdown') return {kind: 'breakdown', key: first(lgr.querySelector('.grow > b'))};
  const li = el.closest('.li');
  if (li && title === 'Holdings') return {kind: 'holding', key: first(li.querySelector('.grow > b'))};
  if (el.closest('.herohead') && title === null && el.closest('.pill')) return {kind: 'change',
    range: (document.querySelector('#page .seg button[aria-pressed=true][data-r]') || {dataset: {}}).dataset.r};
  if (title === 'Goals' && el.closest('.tl2')) return {kind: 'goals_tile', key: /of \$/.test(node) ? 'monthly' : 'top'};
  const gcard = el.closest('[data-goal]');
  if (gcard) return {kind: 'goal', key: gcard.dataset.goal};
  if (el.closest('.gsum')) return {kind: 'goals_total'};
  return {kind: 'unknown'};
};
const pcts = (root) => {
  const out = [];
  const w = document.createTreeWalker(root || document.querySelector('#page'), NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    if (!/[0-9]%/.test(n.textContent) || !n.parentElement || !vis(n.parentElement)) continue;
    const el = n.parentElement;
    const row = el.closest('.li, .lgr, .brow, .crow, .dl, .dg, .al, .tile, .fig, .note, .bigring, .herohead, .tl2, .trackw, .bsum');
    out.push({node: n.textContent.trim(), row: row ? T(row) : T(el), ...pctCtx(el, n.textContent, n)});
  }
  return out;
};
const charts = () => [...document.querySelectorAll('#page canvas, #panel canvas')].map((cv) => {
  const ch = Chart.getChart(cv);
  if (!ch) return null;
  const y = ch.scales && ch.scales.y;
  const data = [];
  for (const ds of ch.data.datasets) for (const v of ds.data) if (typeof v === 'number') data.push(v);
  return {label: cv.getAttribute('aria-label'), type: ch.config.type,
    ticks: y ? y.ticks.map((t) => ({value: t.value, label: Array.isArray(t.label) ? t.label.join(' ') : String(t.label)})) : null,
    max: y ? y.max : null, data_max: data.length ? Math.max(...data) : null, data_sum: data.reduce((a, v) => a + v, 0)};
}).filter(Boolean);
// TASK-14b: every budget ring, found by where it sits (Overview "Left to spend" and its Budgets rows, the Budgets
// summary, each category card), so one drawn without the helper is still read (and FAILs): its data-ring-pct /
// data-ring-status, the % and the label shown, the aria-label, and each arc (colour, opacity, share of the ring).
const rings = () => {
  const out = [];
  const read = (r, kind, key) => {
    const svg = r.querySelector(':scope > svg');
    const arcs = r.classList.contains('hairline')   // an Overview Budgets row's bar (polish P2c): one <i> per shade
      ? [...r.querySelectorAll(':scope > i')].map((i) => ({stroke: i.dataset.c, opacity: i.dataset.o || null, share: parseFloat(i.style.width) / 100}))
      : svg ? [...svg.querySelectorAll(':scope > circle')].slice(1).map((c) => ({stroke: c.getAttribute('stroke'),
      opacity: c.getAttribute('stroke-opacity'),
      share: Number((c.getAttribute('stroke-dasharray') || '0').split(' ')[0]) / (2 * Math.PI * Number(c.getAttribute('r')))})) : [];
    out.push({kind, key, pct: r.dataset.ringPct == null ? null : Number(r.dataset.ringPct), status: r.dataset.ringStatus || null,
      text: T(r.querySelector('.rin > b')), label: T(r.querySelector('.rin > small')), aria: r.getAttribute('aria-label'),
      role: r.getAttribute('role'), arcs});
  };
  const left = cardBy('Left to spend'), bud = cardBy('Budgets');
  if (left) left.querySelectorAll('.ring').forEach((r) => read(r, 'left_ring', null));
  if (bud) bud.querySelectorAll('.brow').forEach((row) => {
    if (!row.querySelector('.ring, .hairline')) return;
    read(row.querySelector('.ring, .hairline'), 'overview_row', T(row.querySelector('.grow b')));
    out[out.length - 1].group = row.dataset.group || null;   // polish P2E: a group budget's row (a group can share a category's name)
  });
  document.querySelectorAll('#page .bsum .ring').forEach((r) => read(r, 'budget_summary', null));
  // polish P2A: a budget row's ring is the 3px progress line under its name (.hair): the same data-ring-pct / -status and
  // aria-label, its bars read back in the ring's shape (colour, opacity, share of the cap: the bills due, lighter, then spent)
  const hex = (c) => '#' + c.match(/\d+/g).slice(0, 3).map((n) => Number(n).toString(16).padStart(2, '0')).join('');
  document.querySelectorAll('#page tr.c .hair').forEach((r) => {
    const bars = [...r.querySelectorAll(':scope > i')], frac = (i) => parseFloat(i.style.width) / 100;
    const spent = bars.find((i) => !i.classList.contains('d')), due = bars.find((i) => i.classList.contains('d'));
    const arcs = [];
    if (due) arcs.push({stroke: hex(getComputedStyle(due).backgroundColor), opacity: String(getComputedStyle(due).opacity), share: frac(spent) + frac(due)});
    if (frac(spent) > 0) arcs.push({stroke: hex(getComputedStyle(spent).backgroundColor), opacity: null, share: frac(spent)});   // a ring draws no empty arc
    out.push({kind: 'budget_card', key: r.closest('tr.c').dataset.category, pct: Number(r.dataset.ringPct), status: r.dataset.ringStatus || null,
      text: r.dataset.ringPct + '%', label: due ? 'with bills' : null, aria: r.getAttribute('aria-label'), role: r.getAttribute('role'), arcs});
  });
  return out;
};
"""

DOM = {
    "overview": r"""(() => {
  const worth = cardBy('Net worth'), inv = cardBy('Investments'), debt = cardBy('Loans & debt'), left = cardBy('Left to spend');
  const where = cardBy('Where it went'), bud = cardBy('Budgets'), spent = [...document.querySelectorAll('#page .card')].find((c) => (T(c.querySelector('h3 .ttl')) || '').startsWith('Spent in'));
  const pace = cardBy('On pace for') || cardBy('Month total');
  return {
    snw: T(document.getElementById('snw')), sas: T(document.getElementById('sas')), sli: T(document.getElementById('sli')),
    nwn: T(document.getElementById('nwn')),
    worth: worth && {big: T(worth.querySelector('.big')), tl2: T(worth.querySelector('.tl2')), transit: [...worth.querySelectorAll('.transit')].map(T),
      mode: (worth.querySelector('.nwseg [aria-pressed=true]') || {dataset: {}}).dataset.nw || null},
    invest: inv && {big: T(inv.querySelector('.big')), tl2: T(inv.querySelector('.tl2'))},
    goals: cardBy('Goals') ? {big: T(cardBy('Goals').querySelector('.big')), tl2: T(cardBy('Goals').querySelector('.tl2'))} : null,
    debt: debt && {big: T(debt.querySelector('.big')), tl2: T(debt.querySelector('.tl2')), transit: [...debt.querySelectorAll('.transit')].map(T)},
    side_transit: [...document.querySelectorAll('#side .nwbox .transit')].map(T),
    spent: spent && T(spent.querySelector('.big')),
    pace: pace && {big: T(pace.querySelector('.big')), subs: [...pace.querySelectorAll('.sub')].map(T)},
    left: left && {big: T(left.querySelector('.big')), subs: [...left.querySelectorAll('.sub')].map(T), ring: T(left.querySelector('.ring b')),
      bad: left.querySelector('.big').classList.contains('bad'), split: helpText(left, 'left-split'),
      bills: helpText(left, 'left-bills')},
    budgets: bud && [...bud.querySelectorAll('.brow')].map((r) => ({name: T(r.querySelector('.grow b')), line: T(r.querySelector('.grow .amt') || r.querySelector('.grow > span')),
      right: T(r.querySelector('.lft') || r.lastElementChild), group: r.dataset.group || null})),
    bfoot: bud && [...bud.querySelectorAll('.bfoot .bc')].map(T),
    where: where && {label: (where.querySelector('canvas') || {getAttribute: () => null}).getAttribute('aria-label'), extra: T(where.querySelector('h3 .hx')), hid: T(where.querySelector('.wfoot .whid')),
      rows: [...where.querySelectorAll('.crow')].map((r) => ({name: T(r.querySelector('.grow > b')), sub: T(r.querySelector('.grow > span')),
        amount: T(r.querySelector('.amt')), spend: Number(r.dataset.spend), hidden: r.classList.contains('off')})),
      all_hidden: T(where.querySelector('.allhid'))},
    flow: T((cardBy('Income vs spending') || document).querySelector('.pill')),
    alerts: [...document.querySelectorAll('#page .alerts .al')].map(T),
    pcts: pcts(), charts: charts(), rings: rings(),
  };
})()""",
    "transactions": r"""(() => ({review: T([...document.querySelectorAll('.fbar .fchip')].find((c) => c.dataset.kind === 'review')),
  badge: T(document.querySelector('[data-count=transactions]')), rows: document.querySelectorAll('#page tbody tr').length,
  pcts: pcts(), charts: charts()}))()""",
    "budgets": r"""(() => {
  const sum = document.querySelector('#page .bsum');
  return {summary: sum && {text: T(sum), big: T(sum.querySelector('.big')), ring: T(sum.querySelector('.ring b')), subs: [...sum.querySelectorAll('.sub')].map(T)},
    // polish P2A: the page is a table grouped by category group. A budget is a `tr.c` row (Planned / Spent / Left); the bills
    // still due and the rollover are read from `data-due` / `data-rollover` on the row, as they were on the card, and a
    // group's totals from `data-*` on its header row.
    cards: [...document.querySelectorAll('#page tr.c')].map((c) => ({name: c.dataset.category, group: c.closest('tbody').dataset.group,
      spend: T(c.querySelector('.act')), plan: T(c.querySelector('.plan')), planLabel: c.querySelector('.plan').getAttribute('aria-label'),
      left: T(c.querySelector('.rp')), leftAmt: c.dataset.left ?? null, status: c.dataset.status ?? null,
      pill: c.querySelector('.rp') ? c.querySelector('.rp').className : null,
      ringPct: c.querySelector('.hair') ? c.querySelector('.hair').dataset.ringPct : null,
      dueAmt: c.dataset.due ?? null, rollAmt: c.dataset.rollover ?? null})),
    groups: [...document.querySelectorAll('#page tr.g')].map((g) => ({name: g.dataset.group, planned: g.dataset.planned,
      spent: g.dataset.spent, left: g.dataset.left, text: T(g), shut: g.closest('tbody').classList.contains('shut'),
      // polish P2E: a group budget (data-budget, '' when none), the caps' sum, the Planned shown, the over-note and the
      // "sum" marker (shown on a group without a budget of its own, if it's visible)
      budget: g.dataset.budget ?? null, caps: g.dataset.caps ?? null, plan: T(g.querySelector('.plan')), note: T(g.querySelector('.gover')),
      sum: (() => { const s = g.querySelector('.gsum'); return s && s.getClientRects().length ? T(s) : null; })()})),
    badge: T(document.querySelector('[data-count=budgets]')), pcts: pcts(), charts: charts(), rings: rings()};
})()""",
    "recurring": r"""(() => {
  const next = cardBy('Next 30 days'), bills = cardBy('Bills'), subs = cardBy('Subscriptions');
  const calc = document.querySelector('#page .calcard');
  return {next: next ? T(next.querySelector('h3 small')) : null, bills: bills && T(bills.querySelector('.recurring-total')),
    cal: calc ? {month: T(calc.querySelector('.calnav span')), total: T(calc.querySelector('.calsum > div:nth-child(1) b')),
      paid: T(calc.querySelector('.calsum > div:nth-child(2) b')), due: T(calc.querySelector('.calsum > div:nth-child(3) b')),
      chips: [...calc.querySelectorAll('.calg .bchip')].map((c) => ({day: T(c.closest('.cday').querySelector('.dn')),
        name: T(c.querySelector('.bct b')), amount: T(c.querySelector('.bct .v')), paid: c.classList.contains('paid')})),
      agenda: [...calc.querySelectorAll('.agenda .agi')].map((c) => T(c.querySelector('.amt')))} : null,
    subs: subs && T(subs.querySelector('.recurring-total')), late: [...document.querySelectorAll('#page .tlc.late b')].map(T),
    rows: [...document.querySelectorAll('#page .li[data-bill]')].map((li) => ({key: li.dataset.bill, amt: T(li.querySelector('.amt')),
      sub: T(li.querySelector('.grow > span')), tag: T(li.querySelector('.pend[data-outlier]')), est: li.hasAttribute('data-est')})),
    badge: T(document.querySelector('[data-count=recurring]')), pcts: pcts(), charts: charts()};
})()""",
    "accounts": r"""(() => {
  const worth = cardBy('Net worth');
  const note = worth && worth.querySelector('.nwnote');
  const cv = worth && worth.querySelector('.ch-nw canvas');
  const ch = cv && Chart.getChart(cv);
  return {big: worth && T(worth.querySelector('.big')), ownowe: worth && T(worth.querySelector('.ac')),
    mode: worth && ((worth.querySelector('.nwseg [aria-pressed=true]') || {dataset: {}}).dataset.nw || null),
    // TASK-22f: each payment in transit's box (its line, then where it left from) and its card's own row's note
    transit: worth ? [...worth.querySelectorAll('.trbox')].map((b) => [T(b.querySelector('.trl')), (() => { const hb = b.querySelector('.help-trigger'); if (!hb) return null; hb.click(); return T(document.querySelector('.help-panel')); })()]) : [],
    // (approved density mock D1 B: the row says it first on its details line, whole, ".meta2 .fx[data-transit]"; where it
    // left from is in the row's own panel, "… payment on its way from Nova Checking on Oct 19. …")
    card_notes: [...document.querySelectorAll('#page .li.acct .meta2 .fx[data-transit]')].map((f) => {
      const r = f.closest('.li'), id = f.dataset.transit;
      const p = helpFact(r, null, `p[data-transit="${id}"]`);
      return {id, text: T(f).replace(/ ·$/, ''), from: p && (/ payment on its way (from .+?) on [A-Z][a-z]{2} [0-9]/.exec(p) || [])[1],
        row: T(r.querySelector('.nmrow .nm'))};
    }),
    note: note && vis(note) ? T(note) : null, nwchart: ch ? ch.data.datasets[0].data : null,
    groups: [...document.querySelectorAll('#page .grp')].map((g) => ({label: T(g.querySelector('.grow > b')), total: T(g.querySelector('.gtot')),
      off: g.classList.contains('off'), part: g.classList.contains('part'), state: g.dataset.nw || null,
      sub: T(g.querySelector('.grow > span')), share: Boolean(g.querySelector('.share'))})),
    // TASK-15: the "Not counted" tags shown (Custom only): the Accounts cards' rows by name, and how many in the sidebar.
    // Approved density mock D4 B: an Accounts row says Not counted on its details line (.meta2 .nc2), not a .tag.nct pill.
    tagged: [...document.querySelectorAll('#page .card .li .nc2')].filter((t) => getComputedStyle(t).display !== 'none')
      .map((t) => T(t.closest('.li').querySelector('.nmrow .nm'))),
    side_tagged: [...document.querySelectorAll('#side .ac .tag.nct')].filter((t) => getComputedStyle(t).display !== 'none').length,
    // Approved density mock D1 B: a car's / property's row is a .li.r2 whose name link carries data-asset / data-kind
    // (no longer the row), line two is short ("Estimated value · updated 4 days ago") and the estimate's own line
    // ("Trade-in $X · Carfax · Sep 26 · Update mileage …") is the Details fact in the row's panel.
    values: [...document.querySelectorAll('#page .li.r2 .account-open:is([data-kind=vehicle], [data-kind=property])')].map((a) => {
      const r = a.closest('.li');
      return {id: Number(a.dataset.asset || a.dataset.edit), kind: a.dataset.kind, href: a.getAttribute('href'),
        name: T(a.querySelector('.nm')), line: T(r.querySelector('.meta2')), sub: helpFact(r, 'Details'), amount: T(r.querySelector('.amt')),
        card: (() => { const c = r.closest('.card'); return c ? T(c.querySelector('h3 .ttl')) : null; })()};
    }),
    snw: T(document.getElementById('snw')), sas: T(document.getElementById('sas')), sli: T(document.getElementById('sli')),
    nwn: T(document.getElementById('nwn')), pcts: pcts(), charts: charts()};
})()""",
    "investments": r"""(() => {
  const top = [...document.querySelectorAll('#page .card')].find((c) => T(c.querySelector('h3 .ttl')) === 'Total investments');
  const alloc = cardBy('Allocation');
  const api = document.querySelector('#page .apirow');
  const out = {total: top && T(top.querySelector('.big')), alloc: {}, pcts: pcts(), charts: charts(),
    api: api ? {amount: T(api.querySelector('.amt')), line: T(api.querySelector('.grow > span'))} : null};
  for (const k of ['holding', 'type']) {
    const b = alloc && alloc.querySelector(`.seg button[data-k="${k}"]`);
    if (!b) continue;
    b.click();
    out.alloc[k] = [...alloc.querySelectorAll('.lgr')].map((r) => ({label: T(r.querySelector('.grow > b')), amount: T(r.querySelector('.amt')),
      pct: T(r.querySelector('.rt > span:last-child'))}));
    out['pcts_' + k] = pcts();
    out['charts_' + k] = charts();
  }
  return out;
})()""",
    "goals": r"""(() => ({cards: [...document.querySelectorAll('#page [data-goal]')].map((c) => ({id: Number(c.dataset.goal),
    pct: T(c.querySelector('.ring .rin b')), big: T(c.querySelector('.big')), short: T(c.querySelector('.gshort .pill'))})),
  suggest: [...document.querySelectorAll('#page [data-suggest]')].map((c) => c.dataset.suggest),
  total: T((document.querySelector('#page .gsum') || document.createElement('i')).querySelector('.big')),
  pcts: pcts(), charts: charts()}))()""",
    "debt": r"""(() => {
  const total = cardBy('Total owed');
  return {total: total ? T(total.querySelector('.big')) : null, sub: total ? T(total.querySelector('.sub')) : null,
    util: T(document.querySelector('#page .dg .meta b')), rows: [...document.querySelectorAll('#page .dl:not(.hdr)')].map((r) => ({name: T(r.querySelector('.grow > b')),
      sub: T(r.querySelector('.grow > span')), bal: T(r.querySelector('.bal'))})), pcts: pcts(), charts: charts()};
})()""",
}


# TASK-15: the Custom net worth's Edit sheet, opened from the Accounts card: every account row (its switch, amount),
# each group (its switch, tally, total), the preview; then one switch flipped (the first row) and read again; then
# Cancel (nothing may be saved).
SHEET_DOM = r"""(() => {
  const edit = document.querySelector('#page .nwseg button[data-nw=custom]');
  if (!edit) return null;
  edit.click();
  const s = document.getElementById('sheet');
  const read = () => ({open: document.getElementById('modal').classList.contains('on') && s.classList.contains('nwsheet'),
    preview: T(s.querySelector('.nwprev .big')), own: T(s.querySelector('.nwprev .ac')), count: T(s.querySelector('.nwcnt')),
    transit: [...s.querySelectorAll('.nwprev .transit')].map(T),
    presets: [...s.querySelectorAll('.nwpre .fchip')].map((b) => ({label: T(b), pressed: b.getAttribute('aria-pressed')})),
    groups: [...s.querySelectorAll('.nwg')].map((g) => ({key: g.dataset.group, state: g.querySelector('.nwgh .tgl').getAttribute('aria-checked'),
      tally: T(g.querySelector('.nwgt .grow > span')), total: T(g.querySelector('.nwgh .amt')),
      rows: [...g.querySelectorAll('.nwr')].map((r) => ({id: r.dataset.id, on: r.querySelector('.tgl').getAttribute('aria-checked'),
        amt: T(r.querySelector('.amt')), name: first(r.querySelector('.grow > b'))}))}))});
  const before = read();
  if (!before.open) return {before, flipped: null, closed: null};  // it didn't open: every sheet check FAILs, none crashes
  const row = s.querySelector('.nwr');
  let flipped = null;
  if (row) {
    row.querySelector('.tgl').click();
    flipped = {id: row.dataset.id, ...read()};
  }
  // The sheet saves on close (TASK-16), and this check also runs on the LIVE app: flip the switch back first, so the
  // close finds nothing changed and sends nothing.
  if (row) row.querySelector('.tgl').click();
  const restored = read();
  const x = s.querySelector('.shd .ib[aria-label=Close]');
  if (x) x.click();
  return {before, flipped, restored, closed: !document.getElementById('modal').classList.contains('on')};
})()"""


# A car's / property's page (#/asset/<id>, TASK-13b)
ASSET_DOM = r"""(() => {
  const head = document.querySelector('#page .vd-head');
  const eq = document.querySelector('#page .vd-eq');
  const w = document.querySelector('#page .vd-picw');
  return {title: T(document.getElementById('title')), big: head && T(head.querySelector('.big')), line: head && T(head.querySelector('.sub')),
    table: [...document.querySelectorAll('#page .vd-vt tr')].map((r) => ({label: T(r.cells[0]), value: T(r.cells[1])})),
    equity: eq && T(eq.querySelector('.big')), legend: eq ? [...eq.querySelectorAll('.vd-legend b')].map(T) : [],
    no_loan: [...document.querySelectorAll('#page .card')].some((c) => /& equity$/.test(T(c.querySelector('h3 .ttl')) || '')
      && c.querySelector('.msgbox')),
    checks: T((cardBy('Value history') || document.createElement('i')).querySelector('h3 small')),
    pic: w && {kind: w.dataset.kind, status: w.dataset.photo},
    use: [...document.querySelectorAll('#page .vd-quiet button')].some((b) => T(b) === 'Use the estimate'),
    pcts: [], charts: charts()};
})()"""


# TASK-18: the Settings page's figures: the Plaid slots, each value source's calls this month (its meter too) and
# switch, and the export's transaction count
SETTINGS_DOM = DOM_COMMON + r"""
(() => {
  const card = (t) => [...document.querySelectorAll('#page .card')].find((c) => (c.querySelector('h3 .ttl') || {}).textContent === t);
  const conn = card('Connections'), val = card('Valuations'), data = card('Data');
  const bar = (m) => (m ? {now: m.getAttribute('aria-valuenow'), max: m.getAttribute('aria-valuemax'),
    fill: m.querySelector('i') ? m.querySelector('i').offsetWidth / m.offsetWidth : null} : null);
  const slots = conn && [...conn.querySelectorAll('.srow')].find((r) => (r.querySelector('.grow > b') || {}).textContent === 'Plaid connections');
  return {
    slotn: slots && slots.querySelector('.slotn') ? slots.querySelector('.slotn').textContent : null,
    slotline: slots ? [...slots.querySelectorAll('.grow > span')].map((x) => x.textContent).find((t) => / used/.test(t)) || null : null,
    slotbar: bar(slots && slots.querySelector('.meter')),
    sources: val ? [...val.querySelectorAll('.srow')].map((r) => ({name: (r.querySelector('.grow > b') || {}).textContent,
      use: r.querySelector('.use') ? r.querySelector('.use').textContent : null, bar: bar(r.querySelector('.use .meter')),
      on: r.querySelector('.tgl') ? r.querySelector('.tgl').getAttribute('aria-checked') : null,
      locked: r.querySelector('.tgl') ? r.querySelector('.tgl').disabled : null, text: r.textContent})) : null,
    exported: data ? (helpText(data, 'data') || [...data.querySelectorAll('.hint')].map((x) => x.textContent)
      .find((t) => t.startsWith('Export:'))) || null : null,
  };
})()"""


def read_dom(base: str, assets: list[int] = ()) -> dict:
    out = {}
    with Chrome() as b:
        b.size(1440, 900)
        for page in PAGES:
            b.goto(f"{base}/?audit={time.time():.3f}#/{page}")
            b.settle()
            out[page] = b.js(DOM_COMMON + "\n" + DOM[page])
            out[page]["problems"] = b.problems()
        b.goto(f"{base}/?audit={time.time():.3f}#/settings")
        b.settle()
        out["settings"] = b.js(SETTINGS_DOM)
        out["settings"]["problems"] = b.problems()
        for aid in assets:
            b.goto(f"{base}/?audit={time.time():.3f}#/asset/{aid}")
            b.js("new Promise((res) => { const t0 = Date.now(); const tick = () => document.querySelector('#page .vd-hero') "
                 "|| Date.now() - t0 > 6000 ? res(1) : setTimeout(tick, 50); tick(); })")
            b.settle()
            out[f"asset-{aid}"] = b.js(DOM_COMMON + "\n" + ASSET_DOM)
            out[f"asset-{aid}"]["problems"] = b.problems()
        # the loan panel (its chart's axis)
        b.goto(f"{base}/?audit={time.time():.3f}#/debt")
        opened = b.js("(() => { const l = document.querySelector('#page [data-loan]'); if (!l) return false; l.click(); return true; })()")
        if opened:
            b.js("new Promise((r) => setTimeout(r, 500))")
            out["panel"] = b.js(DOM_COMMON + "\n(() => ({title: first(document.querySelector('#panel h2')), pcts: pcts(document.getElementById('panel'))"
                                ".map((p) => ({...p, kind: p.kind === 'apr' ? 'apr' : 'loan_paid', key: first(document.querySelector('#panel h2'))})), "
                                "charts: charts()}))()")
        # TASK-14 / 15: Custom, flipped on the Accounts page (in place, no reload), its Edit sheet (opened, one switch
        # flipped, cancelled), then the Overview in it (a reload: the mode is remembered on the device)
        b.goto(f"{base}/?audit={time.time():.3f}#/accounts")
        b.settle()
        flipped = b.js("(() => { const x = document.querySelector('#page .nwseg button[data-nw=custom]'); if (!x) return false; "
                       "window.__audit_same = 1; x.click(); return true; })()")
        b.settle()
        out["accounts-custom"] = b.js(DOM_COMMON + "\n" + DOM["accounts"])
        out["accounts-custom"].update(flipped=flipped, in_place=b.js("window.__audit_same === 1"), problems=b.problems())
        out["sheet"] = b.js("(() => {\n" + DOM_COMMON + "\nreturn " + SHEET_DOM + ";\n})()") or {}  # its own scope
        out["sheet"]["problems"] = b.problems()
        b.goto(f"{base}/?audit={time.time():.3f}#/overview")
        b.settle()
        out["overview-custom"] = b.js(DOM_COMMON + "\n" + DOM["overview"])
        out["overview-custom"]["problems"] = b.problems()
        b.js("localStorage.removeItem('hf.networth.mode')")
    return out


# --- the CLI (read-only, on --db) -----------------------------------------------------------------------------------

def run_cli(db: Path, *argv: str) -> dict:
    env = {**os.environ, "FINANCE_DATA_DIR": str(db.parent), "HERMES_ENV_FILE": str(db.parent / "no-such.env"),
           "FINANCE_SECRETS_FILE": str(db.parent / "no-such-secrets.env")}
    if db.name != "finance.db":  # the CLI reads FINANCE_DATA_DIR/finance.db: point a temp dir at it
        tmp = Path(tempfile.mkdtemp(prefix="audit-cli-"))
        (tmp / "finance.db").symlink_to(db.resolve())
        env["FINANCE_DATA_DIR"] = str(tmp)
    r = subprocess.run([sys.executable, "-m", "hermes_finance.cli", *argv, "--json"], env=env, capture_output=True, text=True,
                       timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"finance {' '.join(argv)}: exit {r.returncode}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


# --- independent re-computations ----------------------------------------------------------------------------------

def amortize(balance: float, apr: float | None, monthly: float | None) -> tuple[int | None, str]:
    """Payments left, re-computed month by month in cents (the loan form's rule, written again here)."""
    cents = round(balance * 100)
    if cents <= 0:
        return 0, "paid"
    if not monthly or monthly <= 0:
        return None, "none"
    pay = round(monthly * 100)
    if apr:
        rate = apr / 1200
        if pay <= round(cents * rate):
            return None, "never"
        n = 0
        while cents > 0 and n < 1200:
            cents = cents + round(cents * rate) - pay
            n += 1
        return (n, "amortized") if cents <= 0 else (None, "never")
    return -(-cents // pay), "simple"


def add_months(month: str, n: int) -> str:
    k = int(month[:4]) * 12 + int(month[5:7]) - 1 + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def tokens(*names: str | None) -> set[str]:
    """Distinctive words of a merchant name, written independently of insights.name_tokens: 4+ letters, a domain-like
    word also joined ("ANOMA.LY" -> "anomaly") and by its first label."""
    generic = {"payment", "online", "purchase", "debit", "credit", "card", "autopay", "store", "shop", "services",
               "service", "the", "and", "inc", "llc", "www", "com", "subscription", "monthly", "recurring", "bill"}
    out = set()
    for n in names:
        for w in re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", (n or "").lower()):
            for x in (w.replace(".", ""), w.split(".")[0]):
                if len(x) >= 4 and not x.isdigit() and x not in generic:
                    out.add(x)
    return out


def parse_axis(label: str) -> float | None:
    """'$1.2k' -> 1200, '−$800' -> -800, '$12k' -> 12000."""
    m = re.fullmatch(r"([−\-]?)\$([0-9]+(?:\.[0-9]+)?)(k?)", label.strip())
    if not m:
        return None
    v = float(m.group(2)) * (1000 if m.group(3) else 1)
    return -v if m.group(1) else v


# --- the audit ------------------------------------------------------------------------------------------------------

def audit(base: str, db: Path | None = None, log=None) -> Audit:
    A = Audit(log)
    S = Site(base)
    ov = S.get("/api/overview")
    today = date.fromisoformat(ov["today"])
    month = ov["month"]
    acc = S.get("/api/accounts")
    debt = S.get("/api/debt")
    bp = S.get("/api/budgets")
    rec = S.get("/api/recurring")
    inv = S.get("/api/investments")
    settings = S.get("/api/settings")
    assets = [m["id"] for m in acc["manual"] if not m["archived_at"] and m["kind"] in ("vehicle", "property")]
    details = {i: S.get(f"/api/manual/{i}/detail") for i in assets}
    dom = read_dom(base, assets)
    for page, d in dom.items():
        if page != "panel":
            A.sect(f"page {page}: loads cleanly")
            A.eq(f"{page}: no JS error, console error or CSP violation", d["problems"], [])
    cli = {}
    if db:
        cli = {"budget": run_cli(db, "budget-status"), "spend": run_cli(db, "spend", "--month", month),
               "cashflow": run_cli(db, "cashflow"), "networth": run_cli(db, "networth"),
               "networth_custom": run_cli(db, "networth", "--custom"), "networth_cash": run_cli(db, "networth", "--cash-only")}

    # --- net worth ---
    A.sect("net worth")
    nw = acc["net_worth"]
    plaid_assets = sum(a["current"] or 0 for i in nw["institutions"] for a in i["accounts"] if a["type"] in ("depository", "investment"))
    plaid_debts = sum(a["current"] or 0 for i in nw["institutions"] for a in i["accounts"] if a["type"] in ("credit", "loan"))
    live = [m for m in acc["manual"] if not m["archived_at"]]
    own = plaid_assets + sum(m["balance"] for m in live if not m["liability"])
    owe_accounts = plaid_debts + sum(m["balance"] for m in live if m["liability"])
    A.eq("own = banks + manual assets + investments + vehicles/property (sum of every asset account)", nw["assets"], own)
    owe = debt["total"]
    # TASK-22: a card payment in transit counts as paid (the net's debts are lower by it; accounts keep the banks' balances)
    transit = r2(sum(e["amount"] for e in nw.get("in_transit") or []))
    if transit:
        A.eq("Overview API payments in transit = Accounts API (TASK-22)", ov["net_worth"].get("in_transit"), nw["in_transit"])
    A.eq("owe = Loans & debt total (less payments in transit)", nw["debts"], owe - transit)
    A.eq("Loans & debt total = cards + loans + other", owe,
         sum(c["balance"] for c in debt["cards"]) + sum(l["balance"] for l in debt["loans"]) + sum(o["balance"] for o in debt["other"]))
    A.eq("Loans & debt total = every liability account", owe, owe_accounts)
    A.eq("net worth = own - owe", nw["net"], own - owe + transit)
    A.eq("Overview API net worth = Accounts API", ov["net_worth"]["net"], nw["net"])
    # whole-dollar figures are compared with the exact value within $0.50 (the page rounds half up: $812.50 is $813)
    A.eq("sidebar net worth (whole dollars) = own - owe", money(dom["overview"]["snw"]), nw["net"], tol=0.5)
    A.eq("sidebar Own = own", money(dom["overview"]["sas"]), own, tol=0.5)
    A.eq("sidebar Owe = owe", money(dom["overview"]["sli"]), owe - transit, tol=0.5)
    A.eq("Overview tile net worth = own - owe", money(dom["overview"]["worth"]["big"]), nw["net"])
    own_t, owe_t = (monies(dom["overview"]["worth"]["tl2"]) + [None, None])[:2]
    A.eq("Overview tile Own = own", own_t, own, tol=0.5)
    A.eq("Overview tile Owe = owe", owe_t, owe - transit, tol=0.5)
    A.eq("Accounts net worth = own - owe", money(dom["accounts"]["big"]), nw["net"])
    ao = monies(dom["accounts"]["ownowe"])
    A.eq("Accounts Own = own", nth(ao, 0), own)
    A.eq("Accounts Owe = owe", nth(ao, 1), owe - transit)
    A.eq("Accounts groups add up to the net worth (plus payments in transit)",
         sum(money(g["total"]) for g in dom["accounts"]["groups"] if g["total"]) + transit, nw["net"])
    # TASK-22f (decision a): the tile counts each payment in transit as paid too, so it never contradicts net worth's Owe
    A.eq("Overview API Loans & debt tile total = Loans & debt total less payments in transit (TASK-22f)",
         [ov["debt"]["total"], ov["debt"].get("reported")], [owe - transit, owe])
    A.eq("Overview Loans & debt tile = Loans & debt total less payments in transit (= Owe)", money(dom["overview"]["debt"]["big"]),
         owe - transit)
    lines = [transit_ui(e) for e in nw.get("in_transit") or []]   # (the audit reads the pages in All)
    A.eq("sidebar: a 'payment in transit · counted once' line for each (TASK-22f)", dom["overview"].get("side_transit"), lines)
    A.eq("Overview Net worth tile: its payment in transit lines", (dom["overview"]["worth"] or {}).get("transit"), lines)
    A.eq("Overview Loans & debt tile: the same lines", (dom["overview"]["debt"] or {}).get("transit"), lines)
    A.eq("Accounts Net worth: each payment in transit's box: its line, where it left from and net worth counted twice",
         dom["accounts"].get("transit"),
         [[transit_ui(e), f"Left {e['from_account']['name']}{' ••' + e['from_account']['mask'] if e['from_account']['mask'] else ''} on "
           f"{date.fromisoformat(e['date']):%b} {date.fromisoformat(e['date']).day}; "
           f"{'Citi' if e['institution'] == 'Citibank Online' else e['institution']}{' ••' + e['mask'] if e['mask'] else ''} "
           f"still shows it owed. Counted twice, net worth would read {whole(nw['net'] - e['amount'])}."]
          for e in nw.get("in_transit") or []])
    # (D1 B: "$731.63 payment on its way" whole on the card row's details line, "from Nova Checking" in its panel)
    A.eq("Accounts: the card's own row says the payment is on its way (and keeps what the bank reports)",
         [(n["id"], f"{n['text']} {n['from']}") for n in dom["accounts"].get("card_notes") or []],
         [(e["transaction_id"], f"{money_text(e['amount'])} payment on its way from {e['from_account']['name']}")
          for e in nw.get("in_transit") or []])
    if dom["debt"].get("total"):
        A.eq("Loans & debt page Total owed = Loans & debt total", money(dom["debt"]["total"]), owe)
    else:
        A.eq("Loans & debt page says 'Nothing owed' only when nothing is owed", owe, 0.0)

    check_worth_modes(A, ov, acc, debt, dom, cli, settings, S.fresh("/api/settings"), db)
    check_values(A, acc, dom, db, cli)
    check_details(A, acc, details, dom, db)

    # --- budgets ---
    A.sect("budgets")
    b = bp["budgets"]
    capped = [c for c in bp["categories"] if c["cap"] is not None]
    end = f"{month}-{31:02d}"
    rec_due: dict[str, float] = {}
    if month == today.isoformat()[:7]:
        for r in rec["items"]:
            if r["cadence"] in PERIODIC and today.isoformat() <= r["next_expected"] <= end and not r["paid_this_month"]:
                rec_due[r["category"]] = rec_due.get(r["category"], 0) + r["amount"]
    # polish P2E: a group with a budget of its own is ONE budget: its amount (read from the database), and every spending
    # category in it, capped or not, for what it spent and its bills still due. The caps inside it keep their rows, but
    # the totals take the group once and leave them out. With no group budget, `counted` is every cap, as before.
    own = group_budget_amounts(db, bp)
    members: dict[str, list] = {}
    for c in bp["categories"]:
        members.setdefault(c.get("group"), []).append(c)
    units = {}
    for g, amt in own.items():
        u = {"group": g, "cap": amt, "spend": r2(sum(c["spend"] for c in members.get(g, []))),
             "due": r2(sum(rec_due.get(c["category"], 0) for c in members.get(g, [])))}
        units[g] = {**u, "left": r2(amt - u["spend"] - u["due"]), "status": cap_rule(u["spend"] + u["due"], amt)}
    counted = [c for c in capped if c.get("group") not in units]
    counted_units = counted + list(units.values())
    # group roll-up (2026-10-22): a group with a cap but no group budget also counts its UNCAPPED categories' spend and
    # bills still due (its rest) in the totals. A group with no cap at all counts nothing, as before.
    rest = {}
    for g in {c.get("group") for c in counted}:
        loose = [c for c in members.get(g, []) if c["cap"] is None]
        rest[g] = (r2(sum(c["spend"] for c in loose)), r2(sum(rec_due.get(c["category"], 0) for c in loose)))
    A.eq("total budgeted = sum of caps", b["total_cap"], sum(c["cap"] for c in counted_units))
    A.eq("spent = sum of capped spend + the uncapped spend in groups with a cap",
         b["total_spend"], sum(c["spend"] for c in counted_units) + sum(x[0] for x in rest.values()))
    A.eq("bills still due in budgets = the Recurring items expected today..month end, unpaid (capped groups whole)",
         b["total_due"], sum(rec_due.get(c["category"], 0) for c in counted) + sum(u["due"] for u in units.values())
         + sum(x[1] for x in rest.values()))
    A.eq("N budgets = the caps outside a group budget + the group budgets (each group once)", b["count"], len(counted_units))
    api_units = {u["group"]: u for u in b.get("group_budgets") or []}
    A.eq("group budgets: the API's = the database's", sorted(api_units), sorted(units))
    for g, u in units.items():
        a = api_units.get(g) or {}
        A.eq(f"group budget {g}: the API's budget = the database's", a.get("budget"), u["cap"])
        A.eq(f"group budget {g}: spent = every category in the group (capped or not), summed", a.get("spend"), u["spend"])
        A.eq(f"group budget {g}: bills still due = its categories' Recurring items due", a.get("due"), u["due"])
        A.eq(f"group budget {g}: left = budget - spent - bills still due", a.get("left"), u["left"])
        A.eq(f"group budget {g}: the status rule on spent + due (over at 100%, warn at 80%)", a.get("status"), u["status"])
        A.eq(f"group budget {g}: its categories = the group's", sorted(a.get("categories") or []),
             sorted(c["category"] for c in members.get(g, [])))
    A.eq("left = budgeted - spent - bills still due in capped categories", b["left"], b["total_cap"] - b["total_spend"] - b["total_due"])
    for c in capped:
        A.eq(f"{c['category']}: bills still due = its Recurring items due", c["due"], rec_due.get(c["category"], 0))
        A.eq(f"{c['category']}: left = cap - spend - due", c["left"], c["cap"] - c["spend"] - c["due"])
    is_over = lambda c: round((c["spend"] + c["due"]) * 100) >= round(c["cap"] * 100) and (c["cap"] > 0 or c["spend"] + c["due"] > 0)
    over_n = sum(is_over(c) for c in counted_units)
    # roll-up: a group none of whose caps is over, whose rest is more than the room its caps still have, is over once
    for g, (sp, du) in rest.items():
        caps = [c for c in counted if c.get("group") == g]
        room = sum(max(c["cap"] - c["spend"] - c["due"], 0) for c in caps if not is_over(c))
        over_n += not any(is_over(c) for c in caps) and round((sp + du) * 100) > round(room * 100)
    A.eq("N over = caps with spend + due at or over the cap (TASK-4 rule: over at 100%) + groups their rest pushes over",
         b["over"], over_n)
    A.eq("left = still free in the caps not used up - how far the others are over", b["left"], b["under"] - b["over_amount"])
    if b["per_day"] is not None:
        A.eq("per day = max(left, 0) / days left", b["per_day"], max(b["left"], 0) / max(b["days_left"], 1))
    ovb = ov["budgets"]
    for k in ("total_cap", "total_spend", "total_due", "left", "per_day", "over"):
        A.eq(f"Overview {k} = Budgets page {k}", ovb[k], b[k])
    if cli:
        t = cli["budget"]["totals"]
        for k, ck in (("total_cap", "budgeted"), ("total_spend", "spent"), ("total_due", "due"), ("left", "left"),
                      ("per_day", "per_day"), ("over", "over")):
            A.eq(f"finance budget-status totals.{ck} = Budgets page {k}", t[ck], b[k])
        for c in cli["budget"]["caps"]:
            A.eq(f"finance budget-status {c['category']}: left = cap - spent - due", c["left"], c["cap"] - c["spent"] - c["due"])
            # polish P2E: a cap inside a group budget is marked, and no other cap carries the keys
            grp = next((x.get("group") for x in capped if x["category"] == c["category"]), None)
            A.eq(f"finance budget-status {c['category']}: counted / in_group_budget",
                 [c.get("counted", "absent"), c.get("in_group_budget", "absent")], [False, grp] if grp in units else ["absent", "absent"])
        groups_cli = {g["group"]: g for g in cli["budget"].get("groups") or []}
        A.eq("finance budget-status groups = the group budgets", sorted(groups_cli), sorted(units))
        for g, u in units.items():
            x = groups_cli.get(g) or {}
            A.eq(f"finance budget-status group {g}: budget, spent, due, left, status = the Budgets page's",
                 [x.get("budget"), x.get("spent"), x.get("due"), x.get("left"), x.get("status")],
                 [api_units.get(g, {}).get(k) for k in ("budget", "spend", "due", "left", "status")])
            A.eq(f"finance budget-status group {g}: categories = the group's", sorted(x.get("categories") or []),
                 sorted(c["category"] for c in members.get(g, [])))
    else:
        A.ok("finance budget-status (skipped: no --db given)", True, note="SKIP")
    L = dom["overview"]["left"]
    if b["count"]:
        A.eq("Overview 'Left to spend' figure = left", money(L["big"]), b["left"])
        A.eq("Overview 'Left to spend' is red exactly when negative", L["bad"], b["left"] < 0)
        A.eq("Overview 'Left to spend' ring % = (spent + bills still due) / budgeted", pct_in(L["ring"]),
             (b["total_spend"] + b["total_due"]) / b["total_cap"] * 100 if b["total_cap"] else 0,
             tol=ring_tol((b["total_spend"] + b["total_due"]) / b["total_cap"] * 100 if b["total_cap"] else 0))
        head = L["subs"][0]
        if b["left"] < 0:
            A.eq("Overview: 'Over budget by $X' = -left", money(head), -b["left"], tol=0.5)
        elif b["per_day"] is not None:
            A.eq("Overview: 'About $X a day' = max(left, 0) / days left", money(head), b["per_day"], tol=0.5)
        if b["total_due"]:
            bills = next((s for s in L["subs"] if "in bills still due" in s), None)
            A.eq("Overview: bills still due line = total due", money(bills), b["total_due"], tol=0.5)
            listed = L["bills"] and [money(t) for t in re.findall(r"[-−$][\d,.]+", L["bills"])]
            A.ok("Overview: the bills behind 'in bills still due' are disclosed, not dropped", bool(listed),
                 got=(L["bills"] or "")[:80], want="a help panel listing them")
            if listed:
                A.eq("Overview: the disclosed bills sum to the total due", round(sum(listed), 2), round(b["total_due"], 2), tol=0.5)
        split = monies(L["split"])
        A.ok("Overview: the budget breakdown is disclosed, not dropped", L["split"] is not None, got=L["split"], want="a help panel")
        A.eq("Overview: '$X left in N budgets' = under", nth(split, 0), b["under"], tol=0.5)
        if b["over"]:
            A.eq("Overview: '$Y over in N' = over amount", nth(split, 1), b["over_amount"], tol=0.5)
        foot = dom["overview"]["bfoot"]
        A.eq("Overview Budgets footer: over / almost there / on track", [int(re.match(r"\d+", x).group(0)) for x in foot],
             [b["over"], b["warn"], b["count"] - b["over"] - b["warn"]])
        for row in dom["overview"]["budgets"]:
            # polish P2E: a row is a counted budget: a cap outside a group budget, or a group budget (data-group), never
            # a cap inside one
            if row.get("group"):
                c, nm = units.get(row["group"]), f"group {row['group']}"
            else:
                c, nm = next((x for x in counted if x["category"] == row["name"]), None), row["name"]
            if c is None:
                A.ok(f"Overview Budgets row {row['name']!r} is a capped category", False, got=row["name"],
                     want="a cap outside a group budget, or a group budget")
                continue
            nums = monies(row["line"])
            A.eq(f"Overview Budgets {nm}: spend, cap (and due)", nums, [c["spend"], c["cap"]] + ([c["due"]] if c["due"] else []), tol=0.5)
            if "left" in row["right"]:
                A.eq(f"Overview Budgets {nm}: '$X left' = cap - spend - due", money(row["right"]), c["left"], tol=0.5)
            elif "Overspent" in row["right"]:
                A.eq(f"Overview Budgets {nm}: overspent = spend - cap", money(row["right"]), c["spend"] - c["cap"], tol=0.5)
            else:
                A.eq(f"Overview Budgets {nm}: over = spend + due - cap", money(row["right"]), -c["left"], tol=0.5)
        s = dom["budgets"]["summary"]
        A.eq("Budgets summary: budgeted", money(s["big"]), b["total_cap"], tol=0.5)
        A.eq("Budgets summary ring % = (spent + bills still due) / budgeted", pct_in(s["ring"]),
             (b["total_spend"] + b["total_due"]) / b["total_cap"] * 100 if b["total_cap"] else 0,
             tol=ring_tol((b["total_spend"] + b["total_due"]) / b["total_cap"] * 100 if b["total_cap"] else 0))
        nums = monies(s["subs"][0])
        A.eq("Budgets summary: spent", nth(nums, 0), b["total_spend"], tol=0.5)
        if b["total_due"]:
            A.eq("Budgets summary: bills still due", nth(nums, 1), b["total_due"], tol=0.5)
        A.eq("Budgets summary: left (or over)", nth(nums, -2 if (b["left"] >= 0 and b["per_day"] is not None and b["days_left"]) else -1),
             abs(b["left"]), tol=0.5)
        for card in dom["budgets"]["cards"]:
            c = next((x for x in capped if x["category"] == card["name"]), None)
            if c is None:
                continue
            A.eq(f"Budgets row {c['category']}: Spent", money(card["spend"]), c["spend"], tol=0.5)
            A.eq(f"Budgets row {c['category']}: Planned = the cap in force (the one set + what rolled in)", money(card["plan"]), c["cap"], tol=0.5)
            left_c = round(c["cap"] - c["spend"] - c["due"], 2)
            A.eq(f"Budgets row {c['category']}: Left = cap - spent - bills still due (negative when over)", money(card["left"]), left_c, tol=0.5)
            A.eq(f"Budgets row {c['category']}: data-left = Left", attr_num(card.get("leftAmt")), left_c, tol=0.005)
            A.eq(f"Budgets row {c['category']}: Left = the API's left", c["left"], left_c, tol=0.005)
            committed = c["spend"] + c["due"]
            true_pct = committed / c["cap"] * 100 if c["cap"] else (100 if committed > 0 else 0)
            A.eq(f"Budgets row {c['category']}: progress line % = (spent + bills still due) / cap", attr_num(card["ringPct"]),
                 true_pct, tol=ring_tol(true_pct))
            A.eq(f"Budgets row {c['category']}: 'Still due' = its bills due", attr_num(card.get("dueAmt")),
                 c["due"], tol=0.5)
            A.eq(f"Budgets row {c['category']}: the status says over exactly when spend + due reaches the cap",
                 "over" if card["status"] == "over" else None, "over" if c["status"] == "over" else None)
            # the Left pill: gray at exactly $0, otherwise the line's colour (amber from 80%, red when over)
            want_pill = "z" if round(left_c * 100) == 0 else {"ok": "", "warn": "w", "over": "b"}[c["status"]]
            got_pill = next((x for x in (card["pill"] or "").split()[1:] if x in ("z", "w", "b")), "")
            A.eq(f"Budgets row {c['category']}: the Left pill's tint = the line's status (gray at $0)", got_pill, want_pill)
        # the groups: each header's totals are the sum of the capped categories in it, and the page's totals are unchanged
        by_group: dict[str, list] = {}
        for card in dom["budgets"]["cards"]:
            c = next((x for x in capped if x["category"] == card["name"]), None)
            if c is not None:
                by_group.setdefault(card["group"], []).append(c)
                A.eq(f"Budgets row {c['category']}: sits in its group", card["group"], c.get("group"))
        for g in dom["budgets"]["groups"]:
            cs = by_group.get(g["name"], [])
            caps_sum = round(sum(c["cap"] for c in cs), 2)
            u = units.get(g["name"])
            if u is None:
                A.eq(f"Budgets group {g['name']}: Planned = its categories' caps", attr_num(g["planned"]), caps_sum, tol=0.005)
                sp, du = rest.get(g["name"], (0, 0)) if cs else (0, 0)   # roll-up: a group with caps counts all of it
                A.eq(f"Budgets group {g['name']}: Spent = every category's spend in a group with caps", attr_num(g["spent"]),
                     round(sum(c["spend"] for c in cs) + sp, 2), tol=0.005)
                A.eq(f"Budgets group {g['name']}: Left = its caps - everything it spent and still owes", attr_num(g["left"]),
                     round(sum(c["cap"] - c["spend"] - c["due"] for c in cs) - sp - du, 2), tol=0.005)
                A.eq(f"Budgets group {g['name']}: no group budget (data-budget empty)", g.get("budget") or None, None)
                A.eq(f"Budgets group {g['name']}: the Planned shown = its categories' caps", money(g.get("plan")), caps_sum, tol=0.5)
                A.eq(f"Budgets group {g['name']}: no visible 'sum' marker (the owner, 2026-10-21: one Planned style)", g.get("sum"), None)
            else:   # polish P2E: a group budget: Planned is it, Spent / Left take every category in the group
                A.eq(f"Budgets group {g['name']}: no 'sum' marker on a group budget", g.get("sum"), None)
                A.eq(f"Budgets group {g['name']}: Planned = its group budget (not its caps)", attr_num(g["planned"]), u["cap"], tol=0.005)
                A.eq(f"Budgets group {g['name']}: the Planned shown = its group budget", money(g.get("plan")), u["cap"], tol=0.5)
                A.eq(f"Budgets group {g['name']}: data-budget = its group budget", attr_num(g.get("budget")), u["cap"], tol=0.005)
                A.eq(f"Budgets group {g['name']}: Spent = every category in it (capped or not), summed", attr_num(g["spent"]), u["spend"], tol=0.005)
                A.eq(f"Budgets group {g['name']}: Left = group budget - spent - bills still due in it", attr_num(g["left"]), u["left"], tol=0.005)
            A.eq(f"Budgets group {g['name']}: data-caps = its caps' Planned, summed", attr_num(g.get("caps")), caps_sum, tol=0.005)
            note = caps_sum if u is not None and round(caps_sum * 100) > round(u["cap"] * 100) else None
            A.eq(f"Budgets group {g['name']}: 'categories add up to $X' exactly when its caps add up to more than its group budget",
                 money(g.get("note")), note, tol=0.5)
        A.eq("Budgets groups: every group budget has its group's row", sorted(g["name"] for g in dom["budgets"]["groups"] if g["name"] in units),
             sorted(units))
        A.eq("Budgets groups: Planned adds up to the page's budgeted", round(sum(attr_num(g["planned"]) or 0 for g in dom["budgets"]["groups"]), 2),
             b["total_cap"], tol=0.005)
        A.eq("Budgets groups: Left adds up to the page's left", round(sum(attr_num(g["left"]) or 0 for g in dom["budgets"]["groups"]), 2),
             b["left"], tol=0.005)
    else:
        A.ok("no caps: nothing to budget", L is not None, note="the tile says so")
    check_rings(A, bp, dom, cli)

    # --- spending ---
    A.sect("spending")
    s = ov["summary"]
    cats = ov["categories"]
    A.eq("month total = sum of category spend", s["mtd"], sum(c["spend"] for c in cats))
    rows, offset = [], 0
    while True:
        page = S.get("/api/transactions", month=month, kind="spend", limit=500, offset=offset)
        rows += page["rows"]
        offset += 500
        if offset >= page["total"]:
            break
    A.eq("month total = sum of the Transactions list (not Income, not Transfers)", s["mtd"], sum(r["amount"] for r in rows))
    A.eq("Transactions list count = its total", len(rows), page["total"])
    if cli:
        A.eq("month total = finance spend --month", cli["spend"]["total"], s["mtd"])
    A.eq("Overview 'Spent in' figure = month total", money(dom["overview"]["spent"]), s["mtd"])
    p = ov["pace"]
    A.eq("pace projection = mtd + pace_add + bills_due", p["projection"], p["mtd"] + p["pace_add"] + p["bills_due_total"])
    A.eq("pace bills due = sum of its items", p["bills_due_total"], sum(x["amount"] for x in p["bills_due"]))
    if dom["overview"]["pace"]:
        A.eq("Overview 'On pace for' figure = projection", money(dom["overview"]["pace"]["big"]), p["projection"])
    W = dom["overview"]["where"]
    if W:
        groups = [g for g in ov["spend_groups"] if g["count"]]
        hidden = set(settings["chart_excluded_categories"])
        expected = {g["group"]: g for g in groups}
        total = sum(g["spend"] for g in groups)
        included = sum(c["spend"] for g in groups for c in g["categories"] if c["category"] not in hidden)
        A.eq("Where it went rows = taxonomy groups with payments", sorted(r["name"] for r in W["rows"]), sorted(expected))
        A.eq("Where it went group totals reconcile", sum(r["spend"] for r in W["rows"]), total)
        for r in W["rows"]:
            g = expected[r["name"]]
            A.eq(f"Where it went {r['name']}: full month total", r["spend"], g["spend"])
            part = sum(c["spend"] for c in g["categories"] if c["category"] not in hidden)
            partly = part > 0 and any(c["count"] and c["category"] in hidden for c in g["categories"])
            # (the owner, 2026-10-20) a partly hidden row's amount is what the chart shows, and it names what's hidden;
            # data-spend (r["spend"], checked above) stays the full month
            A.eq(f"Where it went {r['name']}: amount", money(r["amount"]), part if partly else g["spend"], tol=0.5)
            if partly:
                A.eq(f"Where it went {r['name']}: partly hidden row says so", r["sub"].endswith(" hidden"), True)
            if part > 0:   # (reviewer) the % is over what the chart shows (the owner, 2026-09-26); a fully hidden row says "hidden"
                A.eq(f"Where it went {r['name']}: share of what's shown", pct_in(r["sub"]),
                     part / included * 100 if included else 0, tol=PCT_TOL)
            else:
                A.eq(f"Where it went {r['name']}: hidden row says so", r["sub"].startswith("hidden · "), True)
            A.eq(f"Where it went {r['name']}: hidden when all categories excluded", r["hidden"],
                 all(c["category"] in hidden for c in g["categories"]))
        if included > 0:
            centre = monies(W["label"])
            A.eq("Where it went centre = included spending", nth(centre, 0), included, tol=0.5)
            if abs(included - total) > 0.005:
                A.eq("Where it went centre 2nd line = all spending", nth(centre, 1), total, tol=0.5)
        elif groups:
            A.eq("Where it went: everything hidden", W["all_hidden"], "All categories hidden · Show all")

    # --- recurring ---
    A.sect("recurring")
    for kind in ("bill", "subscription"):
        A.eq(f"monthly total ({kind}s) = sum of its items", rec["monthly_totals"][kind],
             sum(r["monthly_amount"] for r in rec["items"] if r["kind"] == kind))
    D = dom["recurring"]
    A.eq("Recurring 'Bills' visible monthly total", money(D["bills"]), rec["monthly_totals"]["bill"])
    A.eq("Recurring 'Subscriptions' visible monthly total", money(D["subs"]), rec["monthly_totals"]["subscription"])
    soon = [r for r in rec["items"] if r["status"] != "late" and 0 <= r["days_until"] <= 30]
    if D.get("next") is not None or not D.get("cal"):
        A.eq("Recurring 'Next 30 days' = the items expected in 30 days", money(D["next"]), sum(r["amount"] for r in soon))
    else:
        A.ok("Recurring: the bills calendar takes the 'Next 30 days' card's place (TASK-19; checked below)", True)
    lookback = (today - timedelta(days=62)).isoformat()
    recent, offset = [], 0
    while True:
        page = S.get("/api/transactions", limit=500, offset=offset)
        recent += [t for t in page["rows"] if t["date"] >= lookback]
        offset += 500
        if offset >= page["total"] or (page["rows"] and page["rows"][-1]["date"] < lookback):
            break
    for r in rec["items"]:
        if r["status"] != "late":
            continue
        tk = tokens(r["merchant"])
        paid = [t for t in recent if t["amount"] > 0 and t["date"] > r["last_date"] and not t["is_transfer"]
                and tokens(t["merchant_name"], t["name"]) & tk
                and (r.get("variable") or abs(t["amount"] - r["amount"]) <= 0.05 * r["amount"])]   # (TASK-21: any amount)
        A.eq(f"{r['merchant']} is late: no rename-aware charge of the same amount since {r['last_date']}",
             [f"{t['date']} {t['merchant']} {t['amount']}" for t in paid], [])
    late_ok = [r for r in rec["items"] if r["status"] == "late"]
    A.ok("rename-aware lateness checked for every late item", True, got=len(late_ok), want="all")
    due = sorted((r["merchant"], r["amount"]) for r in rec["items"] if r["cadence"] in PERIODIC
                 and today.isoformat() <= r["next_expected"] <= end and not r["paid_this_month"])
    A.eq("bills due = Recurring items expected today..month end and not paid", sorted((x["merchant"], x["amount"]) for x in p["bills_due"]), due)

    # --- investments ---
    A.sect("investments")
    A.eq("total = sum of accounts", inv["total"], sum(a["value"] for a in inv["accounts"]))
    by_acct: dict[str, float] = {}
    for h in inv["holdings"]:
        by_acct[h["account_id"]] = by_acct.get(h["account_id"], 0) + h["value"]
    # Plaid snapshots a balance and its holdings' prices at slightly different times: the account keeps the balance,
    # the gap (the residual) is its own "Unpriced / pending" allocation row, shown from a cent
    residual = 0.0
    for a in inv["accounts"]:
        if not a["manual"] and a["holdings"]:
            A.eq(f"{a['name']}: account value = its holdings + cash + residual", a["value"], by_acct.get(a["id"], 0) + a["residual"])
            residual += a["value"] - by_acct.get(a["id"], 0)
    no_holdings = sum(a["value"] for a in inv["accounts"] if a["manual"] or not a["holdings"])
    A.eq("total = sum of holdings + cash + residual (+ accounts without holdings)", inv["total"],
         sum(h["value"] for h in inv["holdings"]) + residual + no_holdings)
    unpriced = [x for x in inv["allocation"]["by_holding"] if x.get("residual")]
    A.eq("allocation 'Unpriced / pending' = sum of (account value - its holdings + cash), when a cent or more",
         [x["value"] for x in unpriced], [residual] if abs(residual) >= 0.01 else [])
    for k, rows_ in (("by_holding", inv["allocation"]["by_holding"]), ("by_type", inv["allocation"]["by_type"])):
        A.eq(f"allocation {k} = total", sum(x["value"] for x in rows_), inv["total"])
        if inv["total"]:
            A.eq(f"allocation {k} % sum to 100", sum(x["value"] / inv["total"] * 100 for x in rows_), 100.0, tol=0.1)
    A.eq("breakdown = total", sum(x["value"] for x in inv["breakdown"]), inv["total"])
    # hotfix 2026-10-05: the denominator of every Holdings "% of portfolio"
    A.eq("priced total = each account at max(balance, its holdings + cash)", inv.get("priced_total"),
         sum(max(a["value"], by_acct.get(a["id"], 0)) if not a["manual"] and a["holdings"] else a["value"]
             for a in inv["accounts"]))
    A.ok("no holding's share of the portfolio passes 100%", all((h.get("pct") or 0) <= 100.0 for h in inv["holdings"]),
         got=max((h.get("pct") or 0 for h in inv["holdings"]), default=0), want="<= 100")
    A.eq("Investments page total = total", money(dom["investments"]["total"]), inv["total"])
    A.eq("Overview Investments tile = total", money(dom["overview"]["invest"]["big"]), inv["total"])
    for k in ("holding", "type"):
        rows_ = dom["investments"]["alloc"].get(k, [])
        if rows_:
            A.eq(f"Allocation ({k}): the list adds up to the page total", sum(money(r["amount"]) or 0.0 for r in rows_),
                 money(dom["investments"]["total"]))
            ring = next((c for c in dom["investments"].get(f"charts_{k}", []) if c["label"] == "Allocation"), None)
            positive = sum(v for v in (money(r["amount"]) for r in rows_) if v and v > 0)
            if positive:
                A.eq(f"Allocation ({k}): the ring = the list's positive rows", ring["data_sum"] if ring else None, positive)
            else:  # every row $0 (no accounts yet): the list stays, the ring isn't drawn
                A.ok(f"Allocation ({k}): no positive rows, no ring", ring is None, got=ring and ring["data_sum"], want=None)
        unp = [r for r in rows_ if r["label"] == "Unpriced / pending"]
        if k == "holding":
            A.eq("Allocation (holding): the 'Unpriced / pending' row = the residual", [money(r["amount"]) for r in unp],
                 [x["value"] for x in unpriced])
            A.ok("Allocation (holding): 'Unpriced / pending' is the last row (below Cash)", not unp or rows_[-1] is unp[0],
                 got=[r["label"] for r in rows_][-2:], want="... Cash, Unpriced / pending" if unp else "(no residual row)")
        for row in rows_:
            v = money(row["amount"])
            if v is None or not inv["total"]:
                continue
            A.eq(f"Allocation ({k}) {row['label']}: shown % = value / total", pct_in(row["pct"]) if row["pct"] else 0.0,
                 v / inv["total"] * 100, tol=PCT_TOL)

    # --- debt ---
    A.sect("debt")
    for l in debt["loans"]:
        po = l["payoff"]
        if l["paid_off"]:
            A.eq(f"{l['name']}: paid off", po["method"], "paid")
            continue
        n, method = amortize(l["balance"], l["apr"], l["monthly_payment"])
        A.eq(f"{l['name']}: payments left re-amortized from balance / payment / APR", po["payments_left"], n)
        A.eq(f"{l['name']}: payoff method", po["method"], method)
        if n:
            first = (l["next_payment"] or (today.replace(day=1) + timedelta(days=32)).isoformat())[:7]
            A.eq(f"{l['name']}: payoff month = first payment month + payments - 1", po["payoff_month"], add_months(first, n - 1))
    for c in debt["cards"]:
        if c["limit"]:
            A.eq(f"{c['name']}: utilization = balance / limit", c["util"], c["balance"] / c["limit"] * 100)
    g = next((x for x in debt["groups"] if x["key"] == "cards"), None)
    if g and g["limit"]:
        A.eq("cards utilization = card balances / limits", g["util"], sum(c["balance"] for c in debt["cards"] if c["limit"]) / g["limit"] * 100)
        A.eq("Loans & debt page: 'N% utilization'", pct_in(dom["debt"]["util"]), g["util"], tol=PCT_TOL)
    for row in dom["debt"]["rows"]:
        item = next((x for x in debt["cards"] + debt["loans"] + debt["other"] if x["name"] == row["name"].replace("manual", "").strip()), None)
        if item:
            A.eq(f"All debts {item['name']}: balance", money(row["bal"]), item["balance"])

    # --- cash flow ---
    A.sect("cash flow")
    for m in ov["cashflow"]["months"]:
        A.eq(f"{m['month']}: net = income - spending", m["net"], m["income"] - m["spending"])
    if cli:
        A.eq("finance cashflow = the Overview's cash flow", [(m["month"], m["income"], m["spending"]) for m in cli["cashflow"]["months"]],
             [(m["month"], m["income"], m["spending"]) for m in ov["cashflow"]["months"]])
    cm = ov["cashflow"]["months"][-1]
    if dom["overview"]["flow"]:
        A.eq("Overview 'saved' / 'more out than in' = income - spending", money(dom["overview"]["flow"]), abs(cm["income"] - cm["spending"]), tol=0.5)

    check_batch_c(A, S, ov, acc, bp, rec, inv, dom, db)
    check_bills_task21(A, S, ov, bp, rec, dom, db, cli)  # TASK-19
    # --- TASK-18: the Settings page ---
    A.sect("Settings page figures")
    check_settings(A, S, dom["settings"], db)

    # --- every percentage shown ---
    A.sect("every percentage in the DOM")
    check_percentages(A, dom, ov, bp, debt, inv, acc, S.get("/api/goals"))

    # --- axes ---
    A.sect("chart axes")
    for page, d in dom.items():
        for key in [k for k in d if k.startswith("charts")]:
            for ch in d[key]:
                if not ch["ticks"]:
                    continue
                name = f"{page} {ch['label'] or ch['type']}"
                A.ok(f"{name}: max tick >= data max", ch["data_max"] is None or ch["ticks"][-1]["value"] >= ch["data_max"] - 1e-6,
                     got=ch["ticks"][-1]["value"], want=f">= {ch['data_max']}")
                step = abs(ch["ticks"][1]["value"] - ch["ticks"][0]["value"]) if len(ch["ticks"]) > 1 else 1
                bad = [(t["label"], t["value"]) for t in ch["ticks"] if t["label"] and (parse_axis(t["label"]) is None
                       or abs(parse_axis(t["label"]) - t["value"]) > max(step / 2, abs(t["value"]) * 0.05))]
                A.eq(f"{name}: every tick label is at its value's magnitude", bad, [])
    return A


def name_key(s: str | None) -> str:
    """An account name as the pages show it (app.js acctName: Plaid's U+FFFD junk dropped, one space between words; the
    no-break space it puts before a short trailing "(PHEV)" read as a space), for matching a row to the API (TASK-20)."""
    return re.sub(r"\s+", " ", (s or "").replace("\ufffd", "").replace("\u00a0", " ")).strip()


def pct_want(p: dict, ov: dict, bp: dict, debt: dict, inv: dict, acc: dict, goals: dict | None = None) -> tuple[float | None, str]:
    """What a percentage should be, from the API numbers it's made of: (value, how)."""
    kind, key = p["kind"], p.get("key")
    if kind in ("goal", "goals_tile", "goals_total"):
        return goal_pct(p, ov, goals or {})
    b = bp["budgets"]
    cats = {c["category"]: c for c in bp["categories"]}
    sug = {c["category"]: c for c in bp["suggestions"]["categories"]}
    loans = {name_key(l["name"]): l for l in debt["loans"]}
    cards = {name_key(c["name"]): c for c in debt["cards"]}
    total = inv["total"]
    if kind == "budget_card":  # TASK-14b: the ring counts the bills still due in it
        c = cats[key]
        ref = c["cap"] if c["cap"] is not None else sug[key]["suggested"]
        x = c["spend"] + c["due"]
        return (x / ref * 100 if ref else (100.0 if x > 0 else 0.0)), f"{key} (spent + due) / {'cap' if c['cap'] is not None else 'suggestion'}"
    if kind == "budget_summary":
        if bp["suggestion_hero"]:
            names = [c["category"] for c in bp["suggestions"]["categories"] if c["suggested"] is not None]
            spent = sum(cats[n]["spend"] for n in names)
            return (spent / bp["suggestions"]["total"] * 100 if bp["suggestions"]["total"] else 0.0), "spent / suggested total"
        return ((b["total_spend"] + b["total_due"]) / b["total_cap"] * 100 if b["total_cap"] else 0.0), "(spent + due) / budgeted"
    if kind == "left_ring":
        return ((b["total_spend"] + b["total_due"]) / b["total_cap"] * 100 if b["total_cap"] else 0.0), "(spent + due) / budgeted"
    if kind == "alert":
        a = (next((a for a in ov["alerts"] if a.get("group") == p["group"] and a.get("category") is None), None) if p.get("group")
             else next((a for a in ov["alerts"] if a.get("category") == key), None))   # polish P2E: a group budget's alert
        if a is None:
            return None, ""
        return (a["spend"] + a.get("due", 0)) / a["cap"] * 100, f"{key} (spent + due) / cap"
    if kind == "invest_top":
        top = next((x for x in ov["investments"]["slices"] if not x["cash"] and not x.get("residual")), None)
        if top is None:
            return None, ""
        # hotfix 2026-10-05: of the priced total (each account at the larger of its balance and its holdings)
        of = ov["investments"].get("priced_total") or ov["investments"]["total"]
        return top["value"] / of * 100, f"{top['label']} / investments priced total"
    if kind == "cards_util":
        g = next((x for x in debt["groups"] if x["key"] == "cards"), None)
        if g is None:
            return None, ""
        return g["util"], "card balances / card limits"
    if kind == "accounts_cards_util":
        plaid = [a for i in acc["net_worth"]["institutions"] for a in i["accounts"] if a["type"] == "credit" and a["limit"]]
        man = [m for m in acc["manual"] if m["kind"] == "credit_card" and m["credit_limit"] and not m["archived_at"]]
        lim = sum(a["limit"] for a in plaid) + sum(m["credit_limit"] for m in man)
        used = sum(a["current"] or 0 for a in plaid) + sum(m["balance"] for m in man)
        return used / lim * 100, "card balances / limits (Accounts)"
    if kind == "loan_paid":
        l = loans.get(name_key(key))
        return (l["paid_pct"], f"{key} (original - balance) / original") if l else (None, "")
    if kind == "group_paid":
        g = next((g for g in debt["groups"] if g["label"] == key), None)
        return (g["paid_pct"], f"{key} paid") if g else (None, "")
    if kind == "win":
        g = next((g for g in debt["groups"] if g["label"] == key), None)
        return (100.0, f"{key}: every loan paid off") if g and g["all_paid_off"] else (None, "")
    if kind in ("card_util", "manual_util"):
        c = cards.get(name_key(key))
        return (c["util"], f"{key} balance / limit") if c and c["util"] is not None else (None, "")
    if kind == "acct_month":   # polish (t_4c075dc4): "$575 (2.0%) this month" under an Accounts group, shown unsigned
        g = next((g for g in acc["net_worth"]["breakdown"] if g["label"] == key), None)
        c = g and g.get("month_change")
        return (abs(c["pct"]), f"{key}: change since the 1st / its start-of-month total") if c and c["pct"] is not None \
            else (None, "")
    if kind == "plaid_util":
        a = next((a for i in acc["net_worth"]["institutions"] for a in i["accounts"] if name_key(a["name"]) == name_key(key)), None)
        return (a["current"] / a["limit"] * 100, f"{key} balance / limit") if a and a["limit"] else (None, "")
    if kind == "apr":  # the stored rate: a card's (Plaid liabilities, or entered), a loan's, a one-loan group's loan
        if p.get("of") == "group_paid":
            g = next((g for g in debt["groups"] if g["label"] == key), None)
            ids = g.get("loans") or [] if g else []
            l = next((l for l in debt["loans"] if l["id"] == ids[0]), None) if len(ids) == 1 else None
            return (l["apr"], f"{key}: its loan's stored APR ({l['name']})") if l and l["apr"] is not None else (None, "")
        c, l = cards.get(name_key(key)), loans.get(name_key(key))
        if c and c["apr"] is not None:
            return c["apr"], f"{key} APR ({'entered' if c.get('manual') else 'Plaid liabilities'})"
        return (l["apr"], f"{key} stored APR") if l and l["apr"] is not None else (None, "")
    if kind == "alloc":
        rows = inv["allocation"]["by_type" if p.get("mode") == "type" else "by_holding"]
        x = next((x for x in rows if x["label"] == key), None)
        return (x["value"] / total * 100, f"{key} / total") if x and total else (None, "")
    if kind == "breakdown":
        x = next((x for x in inv["breakdown"] if x["label"] == key), None)
        return (x["value"] / total * 100, f"{key} / total") if x and total else (None, "")
    if kind == "holding":
        if key == "Cash":
            v = sum(h["value"] for h in inv["holdings"] if h["is_cash"])
        else:
            h = next((h for h in inv["holdings"] if (h["ticker"] or h["name"]) == key), None)
            m = next((a for a in inv["accounts"] if a["manual"] and name_key(a["name"]) == name_key(key)), None)
            v = h["value"] if h else m["value"] if m else None
        # hotfix 2026-10-05: "% of portfolio" is of the priced total, so a stale balance can't push a holding past 100%
        of = inv.get("priced_total") or total
        return (v / of * 100, f"{key} / portfolio priced total") if v is not None and of else (None, "")
    if kind == "change":
        ch = inv["changes"].get(p.get("range") or "1m")
        return (abs(ch["pct"]), "change / value at the start of the range") if ch and ch.get("pct") is not None else (None, "")
    return None, ""


def goal_pct(p: dict, ov: dict, goals: dict) -> tuple[float | None, str]:
    """TASK-19: a goal card's ring (its % floored, as shown), the Goals tile's two lines, the "Saving toward" total."""
    kind, key = p["kind"], p.get("key")
    gs = {str(g["id"]): g for g in goals.get("goals", [])}
    if kind == "goal":
        g = gs.get(key)
        if not g or g["pct"] is None:
            return None, ""
        return float(math.floor(min(max(g["pct"], 0), 999))), f"goal #{key} progress / target, floored"
    if kind == "goals_total":
        t = goals.get("totals") or {}
        return (float(round(t["pct"])), "saved / targets of the Save up goals") if t.get("pct") is not None else (None, "")
    tile = ov.get("goals") or {}
    if key == "monthly" and tile.get("monthly"):
        net = ov["cashflow"]["months"][-1]["net"]
        return max(net, 0) / tile["monthly"]["target"] * 100, "this month's income - spending / the monthly target"
    if key == "top" and tile.get("top"):
        return float(math.floor(tile["top"]["pct"])), f"{tile['top']['name']} progress / target, floored"
    return None, ""


def spend_with_splits(con: sqlite3.Connection, month: str) -> dict[str, float]:
    """Each spending category's spend in `month`, summed again straight from the tables: a split transaction's lines
    under their own categories, every other transaction under its own."""
    rows = con.execute(
        """SELECT category, SUM(spend) FROM (
             SELECT v.category AS category, v.spend AS spend FROM v_transactions v
             WHERE v.month = ? AND v.is_income = 0 AND v.is_transfer = 0
               AND v.transaction_id NOT IN (SELECT transaction_id FROM transaction_splits)
             UNION ALL
             SELECT c.name, s.amount FROM transaction_splits s
             JOIN transactions t ON t.transaction_id = s.transaction_id AND t.removed = 0
             JOIN categories c ON c.id = s.category_id
             WHERE substr(t.date, 1, 7) = ? AND c.is_income = 0 AND c.is_transfer = 0)
           GROUP BY category""", (month, month)).fetchall()
    return {r[0]: round(r[1], 2) for r in rows}


def month_bills(items: list[dict], month: str) -> list[tuple[str, str, float, str]]:
    """TASK-19: (day, merchant, amount, paid|due) of the recurring items in `month`, from /api/recurring's items, written
    again: the last charge's day when it's in the month (paid, at that charge: last_amount), and the next expected day and
    every one after it at the item's cadence (weekly / every 2 weeks +7 / +14 days; monthly, every 2 / 3 / 6 months,
    yearly: the same day 1 / 2 / 3 / 6 / 12 months on; a day past a month's end is its last day) that falls in the month
    (due, at its amount: TASK-21's estimate for a bill whose amount changes)."""
    y, m = int(month[:4]), int(month[5:7])
    first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    out = []

    def months_on(d: date, n: int) -> date:
        k = d.year * 12 + d.month - 1 + n
        yy, mm = divmod(k, 12)
        return date(yy, mm + 1, min(d.day, calendar.monthrange(yy, mm + 1)[1]))

    for r in items:
        paid = date.fromisoformat(r["last_date"])
        if first <= paid <= last:
            out.append((paid.isoformat(), r["merchant"], r.get("last_amount", r["amount"]), "paid"))
        nxt = date.fromisoformat(r["next_expected"])
        for k in range(0, 400):
            d = (nxt + timedelta(days=STEP_DAYS[r["cadence"]] * k) if r["cadence"] in STEP_DAYS
                 else months_on(nxt, STEP_MONTHS[r["cadence"]] * k))
            if d > last:
                break
            if d >= first:
                out.append((d.isoformat(), r["merchant"], r["amount"], "due"))
    # Same order the API lays the month out in (billcal.calendar_month: by day, then merchant case-folded), so the
    # recomputed list is compared in the page's order. A plain tuple sort here is byte order, which put "SpotHero"
    # before "iCloud" on the same day and failed against the page's "iCloud, SpotHero" for no real reason.
    return sorted(out, key=lambda b: (b[0], b[1].casefold()))


def half_up(x: float) -> int:
    """$296 for 295.50: how an estimate and a usual range are shown (whole dollars, half up)."""
    return math.floor(x + 0.5)


def plugin_call(db: Path, name: str, args: dict) -> dict:
    """The Hermes plugin (hermes_plugin/, loaded the way Hermes loads it, into a stand-in context) answering `name` on `db`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hermes_finance_plugin_audit", ROOT / "hermes_plugin" / "__init__.py",
                                                  submodule_search_locations=[str(ROOT / "hermes_plugin")])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data_dir = db.parent
    if db.name != "finance.db":
        data_dir = Path(tempfile.mkdtemp(prefix="audit-plugin-"))
        (data_dir / "finance.db").symlink_to(db.resolve())

    class Ctx:
        tools: dict = {}

        def get_config(self, key, default=None):
            return {"HERMES_FINANCE_DATA_DIR": str(data_dir), "HERMES_FINANCE_APP_DIR": str(ROOT)}.get(key, default)

        def register_tool(self, name, toolset, schema, handler, **kw):
            self.tools[name] = handler

        def register_hook(self, *a, **kw):
            pass

        def register_skill(self, *a, **kw):
            pass

    ctx = Ctx()
    mod.register(ctx)
    return json.loads(ctx.tools[name](args))


def check_bills_task21(A: Audit, S: "Site", ov: dict, bp: dict, rec: dict, dom: dict, db: Path | None, cli: dict) -> None:
    """TASK-21: bills with any schedule and amounts that change. Bill totals, bills still due and Left to spend agree between
    the API, the CLI (`finance bills`, `finance budget-status`) and the Hermes plugin (finance_bills, finance_budget_status);
    the monthly totals are re-normalized here (PER_MONTH written again); a changing bill's estimate (the median of its last
    3 charges, or while it has fewer than 3 the middle of the usual amount the owner gave), its usual range (its charges'
    lowest and highest over 13 months) and its outlier tag are worked out again from the database; each Recurring row shows
    "~$X est." and "usually $a–b" for a bill that changes and its exact amount otherwise."""
    A.sect("bills: any schedule, amounts that change (TASK-21)")
    today = date.fromisoformat(ov["today"])
    items = rec["items"]
    for kind in ("bill", "subscription"):
        A.eq(f"monthly total ({kind}s) = sum of amount x how often it comes a month (every 2 months: 0.5)",
             rec["monthly_totals"][kind], sum(r["amount"] * PER_MONTH[r["cadence"]] for r in items if r["kind"] == kind))
    for r in items:
        A.eq(f"{r['merchant']}: monthly amount = {r['amount']} x {r['cadence']}", r["monthly_amount"],
             r["amount"] * PER_MONTH[r["cadence"]])
        want_label = f"~${half_up(r['amount']):,} est." if r["variable"] else money_text(r['amount'])
        A.eq(f"{r['merchant']}: shown as {want_label}", r["amount_label"], want_label)
        A.eq(f"{r['merchant']}: an estimate only when it changes", r["estimate"], r["amount"] if r["variable"] else None)
    b = bp["budgets"]
    month_end = f"{ov['month']}-31"
    for r in items:  # a slower bill counts in still due only in the month it's expected
        if r["cadence"] in PERIODIC and r["cadence"] != "monthly":
            due_here = ov["month"] == today.isoformat()[:7] and today.isoformat() <= r["next_expected"] <= month_end and not r["paid_this_month"]
            A.eq(f"{r['merchant']} ({r['cadence']}): in this month's bills still due only when expected this month",
                 any(x["merchant"] == r["merchant"] for x in ov["pace"]["bills_due"]), due_here)
    if db:
        bills_cli = run_cli(db, "bills", "--next", "730d")
        bills_plug = plugin_call(db, "finance_bills", {"next": "730d"})
        bud_plug = plugin_call(db, "finance_budget_status", {})
        t_cli, t_plug = cli["budget"]["totals"], bud_plug["totals"]
        A.eq("bill totals: API = finance bills = the plugin's finance_bills", [rec["monthly_totals"], bills_cli["monthly_totals"]],
             [bills_plug["monthly_totals"], bills_plug["monthly_totals"]])
        A.eq("every bill: API = finance bills = finance_bills (amount, estimate, usual, cadence, source, last paid from, outlier)",
             [[{k: r[k] for k in x} for r, x in zip(items, bills_cli["items"])], bills_cli["items"]],
             [bills_plug["items"], bills_plug["items"]])
        if ov["month"] == today.isoformat()[:7]:
            A.eq("bills still due: Budgets API = finance budget-status = finance_budget_status", [b["total_due"], t_cli["due"]],
                 [t_plug["due"], t_plug["due"]])
            A.eq("Left to spend: Budgets API = finance budget-status = finance_budget_status", [b["left"], t_cli["left"]],
                 [t_plug["left"], t_plug["left"]])
            A.eq("the pace's bills still due: Overview API = finance budget-status = finance_budget_status",
                 [ov["pace"]["bills_due_total"], cli["budget"]["spending"]["bills_due_total"]],
                 [bud_plug["spending"]["bills_due_total"], bud_plug["spending"]["bills_due_total"]])
        # polish P2E: the Hermes tool lists the group budgets (and marks the caps inside) as finance budget-status does
        A.eq("group budgets and caps: finance budget-status = finance_budget_status", [cli["budget"].get("groups"), cli["budget"]["caps"]],
             [bud_plug.get("groups"), bud_plug["caps"]])
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            charges: dict[str, dict[str, float]] = {}
            accts: dict[tuple[str, str], set] = {}
            for d, mn, nm, amt, mask in con.execute(
                    "SELECT t.date, t.merchant_name, t.name, t.amount, a.mask FROM transactions t LEFT JOIN accounts a "
                    "ON a.account_id = t.account_id WHERE t.removed = 0 AND t.amount > 0"):
                key = (mn or nm or "").strip().casefold()
                charges.setdefault(key, {})
                charges[key][d] = charges[key].get(d, 0) + amt
                accts.setdefault((key, d), set()).add(mask)
            since13 = add_months(today.isoformat()[:7], -13) + today.isoformat()[7:]
            for r in (x for x in items if x["variable"]):
                days = sorted({d: 0 for k in r["keys"] for d in charges.get(k, {})})
                per_day = [round(sum(charges.get(k, {}).get(d, 0) for k in r["keys"]), 2) for d in days]
                u = r["usual"]
                if r["source"] == "tracked" and u and u["source"] == "you" and r["charges"] < 3:
                    want = (u["low"] + u["high"]) / 2
                    rule = "the middle of the usual amount you gave (fewer than 3 charges)"
                else:
                    want = statistics.median(per_day[-3:])
                    rule = "the median of its last 3 charges"
                A.eq(f"{r['merchant']}: estimate = {rule}", r["amount"], want)
                if u and u["source"] == "charges":
                    season = [a for d, a in zip(days, per_day) if d >= since13]
                    A.eq(f"{r['merchant']}: usual range = its lowest and highest charge in 13 months", [u["low"], u["high"]],
                         [min(season), max(season)])
                want_tag = None
                if u and u["source"] == "you":
                    want_tag = "higher" if r["last_amount"] > 1.5 * u["high"] else "lower" if r["last_amount"] < 0.5 * u["low"] else None
                A.eq(f"{r['merchant']}: outlier (> 1.5x the usual's top / < 0.5x its bottom)", r["outlier"], want_tag)
            for r in items:
                if r["last_account"]:
                    A.ok(f"{r['merchant']}: last paid from ••{r['last_account']['mask']} = its charge on {r['last_date']}",
                         any(r["last_account"]["mask"] in accts.get((k, r["last_date"]), set()) for k in r["keys"]))
        finally:
            con.close()
    D = dom["recurring"]
    shown = {x["key"]: x for x in D.get("rows") or []}
    for r in items:
        row = shown.get(r["key"])
        if row is None:
            A.ok(f"Recurring row for {r['merchant']}", False, got=None, want="a row")
            continue
        if r["variable"]:
            A.eq(f"Recurring {r['merchant']}: '~$X est.' in whole dollars", [row["amt"], row["est"]], [f"~${half_up(r['amount']):,} est.", True])
            if r["usual"]:
                lo, hi = half_up(r["usual"]["low"]), half_up(r["usual"]["high"])
                rng = f"${lo:,}" if lo == hi else f"${lo:,}–{hi:,}"
                A.ok(f"Recurring {r['merchant']}: 'usually {rng}'", f"usually {rng}" in (row["sub"] or ""), got=row["sub"])
        else:
            A.eq(f"Recurring {r['merchant']}: the exact amount, no est.", [money(row["amt"]), row["est"], "est." in row["amt"]],
                 [r["amount"], False, False])
        A.ok(f"Recurring {r['merchant']}: its schedule '{r['cadence_label']}'", r["cadence_label"] in (row["sub"] or ""), got=row["sub"])
        A.eq(f"Recurring {r['merchant']}: the outlier tag", row["tag"], f"{r['outlier']} than usual" if r["outlier"] else None)


def check_batch_c(A: Audit, S: "Site", ov: dict, acc: dict, bp: dict, rec: dict, inv: dict, dom: dict, db: Path | None) -> None:
    today = date.fromisoformat(ov["today"])
    month = ov["month"]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if db else None

    A.sect("bills calendar (TASK-19)")
    for m in (month, add_months(month, 1)):
        cal = S.get("/api/bills-calendar", month=m)
        want = month_bills(rec["items"], m)
        A.eq(f"calendar {m}: total = its bills from /api/recurring, worked out again", cal["total"], sum(b[2] for b in want))
        A.eq(f"calendar {m}: the bills (day, merchant, amount, paid or due)",
             [(b["date"], b["merchant"], b["amount"], "paid" if b["state"] == "paid" else "due") for b in cal["bills"]], want)
        A.eq(f"calendar {m}: paid + still due = total", cal["paid_total"] + cal["due_total"], cal["total"])
        A.eq(f"calendar {m}: paid = the paid bills", cal["paid_total"], sum(b[2] for b in want if b[3] == "paid"))
    cal = S.get("/api/bills-calendar", month=month)
    D = dom["recurring"].get("cal")
    if D:
        A.eq("Recurring calendar: the month shown is this month", D["month"], f"{date(int(month[:4]), int(month[5:]), 1):%B %Y}")
        A.eq("Recurring calendar: the page's total = the calendar's", money(D["total"]), cal["total"])
        A.eq("Recurring calendar: 'Paid N of M'", D["paid"], f"{cal['paid_count']} of {cal['count']}")
        A.eq("Recurring calendar: 'Still due' = total - paid", money(D["due"]), cal["due_total"])
        # (TASK-21) a bill whose amount changes is due at its estimate, shown in whole dollars: "~$125"
        A.eq("Recurring calendar: every chip on the grid (amounts, day by day; an estimate '~$' in whole dollars)",
             sorted((int(re.match(r"[0-9]+", c["day"]).group(0)), money(c["amount"]), c["amount"].startswith("~")) for c in D["chips"]),
             sorted((int(b["date"][8:]), float(half_up(b["amount"])) if b.get("variable") else b["amount"], bool(b.get("variable")))
                    for b in cal["bills"]))
    else:
        A.ok("Recurring calendar is on the page", False, got=None, want="the Bills calendar card")

    A.sect("split transactions (TASK-19)")
    rows, offset = [], 0
    while True:
        page = S.get("/api/transactions", month=month, limit=500, offset=offset)
        rows += page["rows"]
        offset += 500
        if offset >= page["total"]:
            break
    splits = [r for r in rows if r.get("split")]
    for r in splits:
        A.eq(f"split {r['merchant']} {r['date']}: its lines add up to the amount, to the cent",
             round(sum(l["amount"] for l in r["split"]) * 100), round(r["amount"] * 100), tol=0)
        A.ok(f"split {r['merchant']} {r['date']}: 2+ lines, each a spending category, same sign as the amount",
             len(r["split"]) >= 2 and all((l["amount"] > 0) == (r["amount"] > 0) for l in r["split"]), got=r["split"])
    if con:
        want = spend_with_splits(con, month)
        got = {c["category"]: c["spend"] for c in ov["categories"] if c["spend"] or c["category"] in want}
        A.eq("each category's spend = the tables summed again, a split's lines under their own categories",
             sorted((k, v) for k, v in got.items() if v), sorted((k, v) for k, v in want.items() if v))
        parents = con.execute("SELECT COALESCE(SUM(spend), 0) FROM v_transactions WHERE month = ? AND is_income = 0 "
                              "AND is_transfer = 0", (month,)).fetchone()[0]
        A.eq("a split never changes the month's total: spend without the splits = the month total", round(parents, 2),
             ov["summary"]["mtd"])
    A.ok(f"splits checked: {len(splits)} this month", True, got=len(splits))

    A.sect("budget rollover (TASK-19)")
    rolled = [c for c in bp["categories"] if c.get("rollover")]
    for c in bp["categories"]:
        if not c.get("rollover"):
            A.ok(f"{c['category']}: no rollover, the cap is the one set (no base_cap)", "base_cap" not in c, got=c.get("base_cap"))
    prev = add_months(month, -1)
    last_spend = spend_with_splits(con, prev) if con else {}
    for c in rolled:
        base, roll = c["base_cap"], c["rollover"]
        spent_before = last_spend.get(c["category"], 0.0) if con else c["last_month"]
        carry = max(round(base * 100) - round(spent_before * 100), 0) / 100
        A.eq(f"{c['category']}: rolled over = max(cap - {prev} spend, 0) (overspending never rolls)", roll["amount"], carry)
        A.eq(f"{c['category']}: effective cap = cap + rolled over", c["cap"], base + carry)
        A.eq(f"{c['category']}: rolled over from last month", roll["from"], prev)
        card = next((x for x in dom["budgets"]["cards"] if x["name"] == c["category"]), None)
        if card:
            # 2026-10-02: the rollover figure is not glued onto the amount; it is a disclosure in the Details panel, with
            # the value published as `data-rollover`. P2A: Planned shows the cap in force, its label names the amount set.
            A.eq(f"Budgets row {c['category']}: Planned = base + rolled over", monies(card["plan"])[:1], [base + carry], tol=0.5)
            A.eq(f"Budgets row {c['category']}: the Planned button's label names the $base that was set",
                 (monies(card["planLabel"])[1:2] if carry > 0 else monies(card["planLabel"])[:1]), [base], tol=0.5)
            if carry > 0:
                A.eq(f"Budgets row {c['category']}: the disclosed rollover = what carried over",
                     attr_num(card.get("rollAmt")), carry, tol=0.5)
            else:
                A.eq(f"Budgets row {c['category']}: nothing carried over, so none is disclosed",
                     attr_num(card.get("rollAmt")), 0.0, tol=0.5)
    A.ok(f"rollover checked: {len(rolled)} categor{'y' if len(rolled) == 1 else 'ies'} on", True, got=len(rolled))

    A.sect("goals (TASK-19)")
    g = S.get("/api/goals")
    balances = {a["account_id"]: a["current"] or 0.0 for i in acc["net_worth"]["institutions"] for a in i["accounts"]}
    balances.update({f"manual:{m['id']}": m["balance"] for m in acc["manual"] if not m["archived_at"]})
    for x in g["goals"]:
        if x["kind"] == "save" and x["track"] == "account" and not x["missing_account"]:
            A.eq(f"goal {x['name']}: progress = the account's balance ({x['account']['label']})", x["progress"],
                 balances.get(x["account_id"]))
        if x["kind"] == "monthly":
            cur = ov["cashflow"]["months"][-1]
            A.eq(f"goal {x['name']}: this month's income - spending = the Overview's cash flow net", x["progress"], cur["net"])
            A.eq(f"goal {x['name']}: its month", x["month"], cur["month"])
        if x["kind"] == "payoff" and not x["missing_account"]:
            A.eq(f"goal {x['name']}: paid down = start - what it owes now", x["progress"],
                 max(x["start_balance"] - balances.get(x["account_id"], 0), 0))
        if x["target"]:
            A.eq(f"goal {x['name']}: % = progress / target", x["pct"], x["progress"] / x["target"] * 100)
    per: dict[str, list[dict]] = {}
    for x in g["goals"]:
        if x["kind"] == "save" and x["track"] == "earmark":
            per.setdefault(x["account_id"], []).append(x)
    for aid, xs in per.items():
        bal = max(balances.get(aid, 0.0), 0.0)
        marks = sum(x["earmark"] or 0 for x in xs)
        covered = sum((x["short"] or {}).get("covered", x["earmark"] or 0) for x in xs)
        short = sum((x["short"] or {}).get("amount", 0) for x in xs)
        A.eq(f"earmarks in {aid}: covered = min(earmarks, balance)", covered, min(marks, bal))
        A.eq(f"earmarks in {aid}: flagged short = max(earmarks - balance, 0), never shrunk", short, max(marks - bal, 0))
        A.eq(f"earmarks in {aid}: each goal keeps its whole earmark as progress", [x["progress"] for x in xs],
             [x["earmark"] for x in xs])
    for a in g["accounts"]:
        A.eq(f"{a['label']}: unassigned = max(balance - its earmarks, 0)", a["unassigned"], max(a["balance"] - a["earmarked"], 0))
    G = dom.get("goals") or {}
    shown = {c["id"]: c for c in G.get("cards", [])}
    for x in g["goals"]:
        c = shown.get(x["id"])
        if c is None:
            A.ok(f"Goals page: goal {x['name']} has a card", False, got=None, want=x["id"])
            continue
        A.eq(f"Goals page {x['name']}: the figure = progress", money(c["big"]), x["progress"], tol=0.5)
        A.eq(f"Goals page {x['name']}: short flag shown exactly when short", bool(c.get("short")), bool(x["short"]))
    T = dom["overview"].get("goals")
    if T:
        tile = ov.get("goals") or {}
        want = ov["cashflow"]["months"][-1]["net"] if tile.get("monthly") else tile["top"]["progress"] if tile.get("top") \
            else ov["cashflow"]["months"][-1]["net"]
        A.eq("Overview Goals tile figure = the month's income - spending (or the Save up goal closest to done)", money(T["big"]), want)
    else:
        A.ok("Overview Goals tile is on the page", False, got=None, want="the Goals tile")

    A.sect("Alpaca live account (TASK-19)")
    snap = con.execute("SELECT * FROM alpaca_snapshots WHERE counted = 1 ORDER BY date DESC LIMIT 1").fetchone() if con else None
    ids = [a["account_id"] for i in acc["net_worth"]["institutions"] for a in i["accounts"] if a["account_id"] == "alpaca:live"]
    inv_acct = next((a for a in inv["accounts"] if a["id"] == "alpaca:live"), None)
    if snap is not None and snap[2] == "ACTIVE":
        cols = [d[0] for d in con.execute("SELECT * FROM alpaca_snapshots LIMIT 0").description]
        row = dict(zip(cols, snap))
        pos = json.loads(row["positions_json"])
        A.eq("Alpaca: equity = its positions + cash", row["equity"], sum(p_["value"] for p_ in pos) + row["cash"])
        A.eq("Alpaca: net worth All counts it exactly once", len(ids), 1)
        A.eq("Alpaca: its net worth balance = the snapshot's equity",
             next((a["current"] for i in acc["net_worth"]["institutions"] for a in i["accounts"] if a["account_id"] == "alpaca:live"), None),
             row["equity"])
        A.eq("Alpaca: its Investments value = the equity", inv_acct and inv_acct["value"], row["equity"])
        A.eq("Alpaca: its holdings (positions + cash) add up to its value",
             sum(h["value"] for h in inv["holdings"] if h["account_id"] == "alpaca:live"), row["equity"])
        A.ok("Alpaca: no Plaid slot (not a Plaid Item)", all(it["item_id"] != "alpaca" for it in acc["items"]),
             got=[it["item_id"] for it in acc["items"]])
        api_row = dom["investments"].get("api")
        A.eq("Investments page: the Alpaca row's amount = the equity", money(api_row and api_row["amount"]), row["equity"])
        A.ok("Investments page: the Alpaca row says live, read-only and its positions",
             bool(api_row) and f"{len(pos)} position" in api_row["line"] and "read-only" in api_row["line"], got=api_row)
    else:
        A.eq("Alpaca: not counted anywhere when there's no ACTIVE snapshot", (len(ids), inv_acct), (0, None))
    if con:
        con.close()


def short_date(iso: str) -> str:
    """app.js fmtDate(): 'Sep 23', with the year when it isn't this year ('Sep 23, 2025')."""
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}" + (f", {d.year}" if d.year != date.today().year else "")


def latest_valuations(db: Path) -> dict[int, dict]:
    """The newest Carfax / Redfin asset_valuations row per account, read straight from the DB file (read-only). Rows
    of the retired providers (MarketCheck, RentCast) are history the app never shows."""
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out = {}
        for r in conn.execute("SELECT * FROM asset_valuations WHERE source IN ('carfax', 'redfin') ORDER BY fetched_at, id"):
            out[r["account_id"]] = dict(r)
        return out
    finally:
        conn.close()


def valuation_counts(db: Path) -> dict[int, int]:
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        return dict(conn.execute("SELECT account_id, COUNT(*) FROM asset_valuations WHERE source IN ('carfax', 'redfin') "
                                 "GROUP BY account_id").fetchall())
    finally:
        conn.close()


def asked_accounts(db: Path) -> set[int]:
    """The accounts Carfax / Redfin were ever asked about (answered or not)."""
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM api_calls WHERE provider IN ('carfax', 'redfin') "
                                           "AND account_id IS NOT NULL")}
    finally:
        conn.close()


def check_values(A: Audit, acc: dict, dom: dict, db: Path | None, cli: dict) -> None:
    """Every vehicle / property row on the Accounts page, against the API, the DB and `finance networth`."""
    A.sect("vehicles and property: estimates (TASK-12, TASK-13b)")
    live = [m for m in acc["manual"] if not m["archived_at"] and m["kind"] in ("vehicle", "property")]
    for label, kind in (("Vehicles", "vehicle"), ("Property", "property")):
        g = next((g for g in dom["accounts"]["groups"] if (g["label"] or "").startswith(label)), None)
        items = [m for m in live if m["kind"] == kind]
        if g and g["total"]:
            A.eq(f"Accounts '{label}' group = the sum of its values", money(g["total"]), sum(m["balance"] for m in items))
    if not live:
        A.ok("no vehicle or property on this DB (nothing to check)", not dom["accounts"]["values"], got=0, want=0, note="SKIP")
        return
    stored = latest_valuations(db) if db else {}
    nw = {a["id"]: a for g in cli["networth"]["groups"] for a in g["accounts"] if a["source"] == "manual"} if cli else {}
    for m in live:
        v, e, name = m["valuation"], m["valuation"]["estimate"], m["name"]
        A.eq(f"{name}: valuation source is 'estimate' or 'user' (value_source)", v["source"], m["value_source"] or "user")
        if v["source"] == "estimate" and e:
            A.eq(f"{name}: its value = its latest estimate", m["balance"], e["value"])
        if db:
            row = stored.get(m["id"])
            A.eq(f"{name}: the API's estimate = asset_valuations' newest Carfax / Redfin row (value, source)",
                 [e["value"], e["source"]] if e else None, [row["value"], row["source"]] if row else None)
        rows = [r for r in dom["accounts"]["values"] if r["id"] == m["id"]]
        # approved density mock D2 B: "Added by you" is gone, a manual item is listed once, in its group card
        A.ok(f"{name}: its row is on the Accounts page (once, in its group card)", len(rows) == 1, got=len(rows), want="1")
        for r in rows:
            where = f"{name} [{r['card']}]"
            A.eq(f"{where}: amount = its value", money(r["amount"]), m["signed"])
            A.eq(f"{where}: the row opens its page", r["href"], f"#/asset/{m['id']}")
            if v["source"] == "estimate" and e:
                head = f"{e['basis_label']} ${e['value']:,.0f} · {e['source_label']} · {short_date(e['date'])}"
                A.ok(f"{where}: reads 'Trade-in / Estimated $X · source · date'", r["sub"].startswith(head), got=r["sub"], want=head)
                A.eq(f"{where}: its $X = the estimate", money(r["sub"]), e["value"], tol=0.5)
            elif v["source"] == "estimate":
                A.ok(f"{where}: says 'No estimate yet' (it counts $0)", r["sub"].startswith("No estimate yet") and m["balance"] == 0,
                     got=r["sub"], want="No estimate yet ...")
            else:
                A.ok(f"{where}: labelled 'Your value'", r["sub"].startswith("Your value"), got=r["sub"], want="Your value ...")
                shown = monies(r["sub"])
                A.eq(f"{where}: the estimate shown beside your value", shown, [e["value"]] if e else [], tol=0.5)
            A.eq(f"{where}: the mileage nudge exactly when due", "Update mileage for a better estimate" in r["sub"], v["mileage_nudge"])
        if cli:
            n = nw.get(m["id"])
            if not n:
                A.ok(f"finance networth lists {name}", False, got=None, want=name)
                continue
            nv = n["valuation"]
            A.eq(f"finance networth {name}: value = the API's", nv["value"], m["balance"])
            A.eq(f"finance networth {name}: source = the API's", nv["source"], v["source"])
            A.eq(f"finance networth {name}: as of = the estimate's date (or when you set your value)", nv["as_of"],
                 e["date"] if v["source"] == "estimate" and e else m["balance_as_of"])
            A.eq(f"finance networth {name}: label", nv["label"],
                 e["source_label"] if v["source"] == "estimate" and e else "Your value" if v["source"] == "user" else "No estimate yet")
            A.eq(f"finance networth {name}: the estimate = the API's",
                 [nv["estimate"][k] for k in ("value", "date", "basis")] if nv["estimate"] else None,
                 [e[k] for k in ("value", "date", "basis")] if e else None)


CARFAX_ROWS = (("Trade-in", "trade_in"), ("Private sale", "private_sale"), ("Dealer retail", "dealer_retail"))


def check_details(A: Audit, acc: dict, details: dict, dom: dict, db: Path | None) -> None:
    """TASK-13b: each car / property page: its value, its value table (= detail() = the stored Carfax / Redfin figures),
    equity = value - the loan LINKED to it (no link: no equity), and its history."""
    A.sect("car / property pages (TASK-13b)")
    if not details:
        A.ok("no vehicle or property on this DB (nothing to check)", True, got=0, want=0, note="SKIP")
        return
    manual_by_id = {m["id"]: m for m in acc["manual"]}
    stored = latest_valuations(db) if db else {}
    counts = valuation_counts(db) if db else {}
    asked = asked_accounts(db) if db else set()
    for aid, d in details.items():
        m, page = manual_by_id[aid], dom.get(f"asset-{aid}") or {}
        name = m["name"]
        A.eq(f"{name} page: value = its balance (the API's)", d["value"]["amount"], m["balance"])
        A.eq(f"{name} page: the big number = its value (whole dollars)", money(page.get("big")), m["balance"], tol=0.5)
        # the value table: the page shows detail()'s rows, and those are the stored figures of its one source
        shown = [(r["label"], money(r["value"]))   # "no estimate" / "not checked yet": None
                 for r in page.get("table", [])]
        A.eq(f"{name} page: the value table on the page = detail()'s",
             shown, [(f"{r['label']} ({r['source']})", r["value"]) for r in d["table"]], tol=0.5)
        row = stored.get(aid)
        det = json.loads(row["details_json"]) if row and row["details_json"] else {}
        if m["kind"] == "vehicle":
            want = [(f"{label} (Carfax)", det.get(key) if row else None) for label, key in CARFAX_ROWS]
        else:
            want = [("Estimate (Redfin)", row["value"] if row else None)] + (
                [(f"{det['last_sold']['label'][:1] + det['last_sold']['label'][1:].lower()} (Redfin)", det["last_sold"]["amount"])]
                if det.get("last_sold") else [])
        if db:
            A.eq(f"{name} page: detail()'s table = the newest Carfax / Redfin row (and nothing else)",
                 [(f"{r['label']} ({r['source']})", r["value"]) for r in d["table"]], want, tol=0.01)
            for r, cell in zip(d["table"], page.get("table", [])):
                if r["value"] is None:
                    A.eq(f"{name} page: the empty '{r['label']}' cell says why", cell["value"],
                         "no estimate" if row or aid in asked else "not checked yet")
            # "Use the estimate" only when there's one to use: a typed value with a stored estimate beside it
            A.eq(f"{name} page: 'Use the estimate' offered only with an estimate to use", page.get("use"),
                 bool(row) and (m.get("valuation") or {}).get("source") == "user")
        # equity: only the linked loan counts
        link = m.get("linked_loan_id")
        loan = manual_by_id.get(link) if link else None
        live_loan = loan if loan and loan["kind"] == "loan" and not loan["archived_at"] else None
        A.eq(f"{name} page: its loan is the one linked to it", d["loan"]["id"] if d["loan"] else None,
             live_loan["id"] if live_loan else None)
        if live_loan:
            equity = m["balance"] - live_loan["balance"]
            A.eq(f"{name} page: equity = value - linked loan (API)", d["loan"]["equity"], equity)
            A.eq(f"{name} page: equity on the page (whole dollars) = value - linked loan", money(page.get("equity")), equity, tol=0.5)
            A.eq(f"{name} page: the legend = the loan's balance and the equity", [money(x) for x in page.get("legend", [])],
                 [live_loan["balance"], equity])
        else:
            A.ok(f"{name} page: no linked loan, no equity shown", page.get("equity") is None and page.get("no_loan") is True,
                 got=page.get("equity"), want="no equity, 'No loan linked'")
        if db:
            A.eq(f"{name} page: the history = its Carfax / Redfin estimates", len(d["history"]), counts.get(aid, 0))
        A.eq(f"{name} page: 'N checks' = the history", int((page.get("checks") or "0").split()[0]), len(d["history"]))
        A.eq(f"{name} page: the picture shown = detail()'s", (page.get("pic") or {}).get("kind"), d["photo"]["kind"])


LEFT_OUT = ("investments", "vehicles", "property", "other_assets")   # the groups the Cash & debts preset leaves out
TYPE_GROUP = {"depository": "cash", "investment": "investments", "credit": "cards", "loan": "loans"}


def custom_points(db: Path, days: list[str], excluded: set[str]) -> dict[str, tuple[float, float]]:
    """Custom's (assets, debts) on each history day, from the DB file (read-only), written again: each connected Plaid
    account's latest real snapshot on or before the day, each manual account's latest balance row on or before it; none
    left out, and no archived manual account (it never counts in Custom)."""
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        plaid = conn.execute("SELECT a.account_id, a.type FROM accounts a JOIN items i ON i.item_id = a.item_id WHERE "
                             "i.removed_at IS NULL AND a.type IN ('depository', 'investment', 'credit', 'loan')").fetchall()
        mine = conn.execute("SELECT id, kind FROM manual_accounts WHERE archived_at IS NULL").fetchall()
        out = {}
        for day in days:
            a = d = 0.0
            for aid, typ in plaid:
                if aid in excluded:
                    continue
                v = conn.execute("SELECT current FROM balance_snapshots WHERE account_id = ? AND date <= ? AND current IS NOT "
                                 "NULL ORDER BY date DESC LIMIT 1", (aid, day)).fetchone()
                if v:
                    a, d = (a + v[0], d) if typ in ("depository", "investment") else (a, d + v[0])
            for mid, kind in mine:
                if f"manual:{mid}" in excluded:
                    continue
                v = conn.execute("SELECT balance FROM manual_balance_history WHERE manual_account_id = ? AND date <= ? "
                                 "ORDER BY date DESC LIMIT 1", (mid, day)).fetchone()
                if v:
                    a, d = (a + v[0], d) if kind in ("bank", "investment", "vehicle", "property", "other_asset") else (a, d + v[0])
            if "alpaca:live" not in excluded:  # TASK-19: Alpaca's latest counted snapshot on or before the day, while ACTIVE
                v = conn.execute("SELECT status, equity FROM alpaca_snapshots WHERE counted = 1 AND date <= ? ORDER BY date "
                                 "DESC LIMIT 1", (day,)).fetchone()
                a += v[1] if v and v[0] == "ACTIVE" else 0.0
            out[day] = (a, d)
        return out
    finally:
        conn.close()


def custom_note(n: int) -> str:
    return f"Custom · {n} account{'' if n == 1 else 's'} not counted" if n else "Custom · every account counted"


def check_worth_modes(A: Audit, ov: dict, acc: dict, debt: dict, dom: dict, cli: dict, settings: dict,
                      settings_after: dict, db: Path | None = None) -> None:
    """TASK-14 / TASK-15: the net worth switch, All · Custom. Custom = every account except the saved selection (GET
    /api/settings networth_excluded), recomputed from the accounts: its assets / debts / net are the counted accounts'
    balances, its net their signed sum, each group's counted part. Then in each mode (All as loaded, Custom flipped on the
    Accounts page) the sidebar = the Overview tile = the Accounts figure = `finance networth [--custom] --json`, Own / Owe
    everywhere, the groups (left out entirely: "Not counted" with its total; in part: "N of M counted" and the counted
    total; the counted ones adding up to the mode's net), the "Not counted" tags, the chart on the mode's line (with --db,
    each of its points written again from the snapshots); the Edit
    sheet (every account and its switch, the preview = Custom, one switch moves it by exactly that account, Cancel saves
    nothing); and the hidden --cash-only (the Cash & debts preset, whatever is saved)."""
    A.sect("net worth: All and Custom (TASK-15)")
    nw = acc["net_worth"]
    c = nw.get("custom") or {}
    sel = set(settings.get("networth_excluded") or [])
    live = [m for m in acc["manual"] if not m["archived_at"]]
    # (id, group, balance, asset?, name, shown in the sidebar without "show all")
    accts = [(a["account_id"], TYPE_GROUP[a["type"]], a["current"] or 0, a["type"] in ("depository", "investment"), a["name"],
              abs(a["current"] or 0) >= 1) for i in nw["institutions"] for a in i["accounts"] if a["type"] in TYPE_GROUP]
    accts += [(f"manual:{m['id']}", m["group"], m["balance"], not m["liability"], m["name"], True) for m in live]
    counted = [x for x in accts if x[0] not in sel]
    left = [x for x in accts if x[0] in sel]
    c_assets = sum(x[2] for x in counted if x[3])
    c_debts = sum(x[2] for x in counted if not x[3])
    # TASK-22: payments in transit to a card Custom counts are counted as paid
    in_transit = nw.get("in_transit") or []
    c_transit = r2(sum(e["amount"] for e in in_transit if e.get("counted_custom")))
    A.eq("Custom assets = the counted asset accounts (every account but the saved selection)", c.get("assets"), c_assets)
    A.eq("Custom debts = the counted debt accounts (less payments in transit)", c.get("debts"), c_debts - c_transit)
    A.eq("Custom net = counted assets - counted debts", c.get("net"), c_assets - c_debts + c_transit)
    A.eq("Custom net = the sum of the counted accounts' signed balances", c.get("net"),
         sum(x[2] if x[3] else -x[2] for x in counted) + c_transit)
    A.eq("Custom's accounts left out = the saved selection's current accounts",
         sorted(x["id"] for x in c.get("excluded", [])), sorted(x[0] for x in left))
    A.eq("Custom's accounts left out: name and balance", sorted((x["name"], x["balance"]) for x in c.get("excluded", [])),
         sorted((x[4], round(x[2], 2)) for x in left))
    for g in nw["breakdown"]:
        mine = [x for x in counted if x[1] == g["key"]]
        A.eq(f"{g['label']}: Custom counts {len(mine)} of its accounts, their total",
             [(g.get("custom") or {}).get("count"), (g.get("custom") or {}).get("total")], [len(mine), sum(x[2] for x in mine)])
    A.eq("Custom's groups add up to Custom's net", sum((g.get("custom") or {}).get("signed", 0) for g in nw["breakdown"])
         + c_transit, c.get("net"))
    brief = lambda b: b and [b.get("assets"), b.get("debts"), b.get("net"), [x["id"] for x in b.get("excluded", [])]]  # noqa: E731
    A.ok("Overview API custom = Accounts API custom (the whole block)", ov["net_worth"].get("custom") == c,
         got=brief(ov["net_worth"].get("custom")), want=brief(c))
    want_points = custom_points(db, [p["date"] for p in nw["history"]], sel) if db else {}
    for p in nw["history"]:
        A.eq(f"history {p['date']}: custom_net = custom_assets - custom_debts", p.get("custom_net"),
             (p.get("custom_assets") or 0) - (p.get("custom_debts") or 0))
        if db:
            want = want_points[p["date"]]
            if p["date"] == acc.get("today"):   # TASK-22: today's point counts the payments in transit as paid
                want = (want[0], want[1] - c_transit)
            A.eq(f"history {p['date']}: custom_assets / custom_debts = the counted accounts' balances that day (from the DB)",
                 [p.get("custom_assets"), p.get("custom_debts")], [r2(x) for x in want])
    # the hidden --cash-only (TASK-14's flag, one more release): the Cash & debts preset, whatever is saved
    out = cli.get("networth_cash")
    if out is not None:
        cash = sum(x[2] for x in accts if x[1] == "cash")
        owe = debt["total"] - r2(sum(e["amount"] for e in nw.get("in_transit") or []))   # (every card counts in the preset)
        A.eq("finance networth --cash-only --json: cash_only = cash - every debt (the preset, not the saved selection)",
             out.get("cash_only"), {"assets": r2(cash), "debts": r2(owe), "net": r2(cash - owe)})
        A.eq("finance networth --cash-only --json: top net / mode", [out.get("net"), out.get("mode")], [r2(cash - owe), "cash_only"])
        A.eq("finance networth --cash-only --json lists the Cash & debts groups",
             [g["key"] for g in out.get("groups", [])], [g["key"] for g in nw["breakdown"] if g["key"] not in LEFT_OUT])
    history = nw["history"]
    keys = {g["label"]: g["key"] for g in nw["breakdown"]}
    gkey = lambda g: keys.get((g["label"] or "").split(" · ")[0])  # noqa: E731
    bgroup = {g["key"]: g for g in nw["breakdown"]}
    for mode, key, w, ov_d, acc_d, out in (
            ("All", "all", nw, dom["overview"], dom["accounts"], cli.get("networth")),
            ("Custom", "custom", c, dom.get("overview-custom") or {}, dom.get("accounts-custom") or {}, cli.get("networth_custom"))):
        tag, flag = f"[{mode}]", "" if key == "all" else " --custom"
        net, assets, debts = w.get("net"), w.get("assets"), w.get("debts")
        m_transit = r2(sum(e["amount"] for e in in_transit)) if key == "all" else c_transit   # TASK-22b: counted as paid
        tile_d = ov_d.get("worth") or {}
        if key == "custom":
            A.ok(f"{tag} flipped on the Accounts page, in place (no reload)", acc_d.get("flipped") and acc_d.get("in_place"),
                 got={k: acc_d.get(k) for k in ("flipped", "in_place")}, want="flipped in place")
            A.eq(f"{tag} the sidebar's note under Net worth reads 'Custom'", ov_d.get("nwn"), "Custom")
        A.eq(f"{tag} the switch on the Overview tile shows it", tile_d.get("mode"), key)
        A.eq(f"{tag} the switch on the Accounts card shows it", acc_d.get("mode"), key)
        page, tile = money(acc_d.get("big")), money(tile_d.get("big"))
        A.eq(f"{tag} Accounts figure = the API's net", page, net)
        A.eq(f"{tag} Overview tile = Accounts figure", tile, page)
        A.eq(f"{tag} sidebar on the Overview (whole dollars) = Accounts figure", money(ov_d.get("snw")), page, tol=0.5)
        A.eq(f"{tag} sidebar on the Accounts page (whole dollars) = Accounts figure", money(acc_d.get("snw")), page, tol=0.5)
        if out is not None:
            A.eq(f"{tag} finance networth{flag} --json net = Accounts figure", out.get("net"), page)
            A.eq(f"{tag} finance networth{flag} --json assets / debts = the API's",
                 [out.get("assets"), out.get("debts")], [assets, debts])
            A.eq(f"{tag} finance networth{flag} --json mode", out.get("mode"), key)
            A.ok(f"{tag} finance networth{flag} --json custom block = the API's (the whole block)", out.get("custom") == c,
                 got=brief(out.get("custom")), want=brief(c))
            groups_out = out.get("groups", [])
            if key == "all":
                A.eq(f"{tag} finance networth --json lists every group", [g["key"] for g in groups_out], [g["key"] for g in nw["breakdown"]])
                A.eq(f"{tag} finance networth --json: each account's nw_id / counted = the saved selection",
                     sorted((a["nw_id"], a["counted"]) for g in groups_out for a in g["accounts"]),
                     sorted((x[0], x[0] not in sel) for x in accts))
            else:
                want = [g["key"] for g in nw["breakdown"] if not (g["count"] and not g["custom"]["count"])]
                A.eq(f"{tag} finance networth --custom --json lists the groups not left out entirely", [g["key"] for g in groups_out], want)
                A.eq(f"{tag} finance networth --custom --json: each group's total = its counted accounts'",
                     [(g["key"], g["count"], g["total"]) for g in groups_out],
                     [(k, bgroup[k]["custom"]["count"], bgroup[k]["custom"]["total"]) for k in want])
                A.eq(f"{tag} finance networth --custom --json lists only the counted accounts",
                     sorted(a["nw_id"] for g in groups_out for a in g["accounts"]), sorted(x[0] for x in counted))
            A.eq(f"{tag} finance networth{flag} --json groups add up to its net (plus the payments in transit it counts)",
                 sum(g["signed"] for g in groups_out) + m_transit, out.get("net"))
        # Own / Owe: the sidebar and the tile in whole dollars, the Accounts card to the cent
        for where, text, tol in (("sidebar", f"{ov_d.get('sas') or ''} {ov_d.get('sli') or ''}", 0.5),
                                 ("sidebar on the Accounts page", f"{acc_d.get('sas') or ''} {acc_d.get('sli') or ''}", 0.5),
                                 ("Overview tile", tile_d.get("tl2") or "", 0.5), ("Accounts card", acc_d.get("ownowe") or "", TOL)):
            A.eq(f"{tag} {where}: Own / Owe = assets / debts", monies(text)[:2], [assets, debts], tol=tol)
            A.eq(f"{tag} {where}: the first label reads 'Own'", text.strip().split(" ")[0] if text.strip() else None, "Own")
        A.eq(f"{tag} the quiet line under the Accounts figure", acc_d.get("note"), None if key == "all" else custom_note(len(left)))
        groups = acc_d.get("groups", [])
        want_state = {g["key"]: "on" if key == "all" or not g["count"] else "off" if not g["custom"]["count"]
                      else "part" if g["custom"]["count"] < g["count"] else "on" for g in nw["breakdown"]}
        A.eq(f"{tag} Accounts groups: counted / in part / left out", [(gkey(g), g.get("state")) for g in groups],
             [(g["key"], want_state[g["key"]]) for g in nw["breakdown"]])
        A.ok(f"{tag} a group left out entirely shows 'Not counted' in place of its share, its total still there",
             all(g["off"] and g["sub"] == "Not counted" and not g["share"] and (g["total"] or not bgroup[gkey(g)]["count"])
                 and (not g["total"] or money(g["total"]) == bgroup[gkey(g)]["signed"]) for g in groups if g.get("state") == "off"),
             got=[(g["label"], g["sub"], g["total"]) for g in groups if g.get("state") == "off"], want="Not counted, total shown")
        for g in (g for g in groups if g.get("state") == "part"):
            bg = bgroup[gkey(g)]
            A.eq(f"{tag} {bg['label']} counted in part: 'N of M counted' and the counted total",
                 [g["sub"], money(g["total"]), g["share"]], [f"{bg['custom']['count']} of {bg['count']} counted", bg["custom"]["signed"], True])
        A.eq(f"{tag} Accounts groups counted (whole or in part) add up to the net (plus the payments in transit it counts)",
             sum(money(g["total"]) for g in groups if g["total"] and g.get("state") != "off") + m_transit, net)
        A.eq(f"{tag} 'Not counted' tags on the Accounts cards: exactly the accounts left out",
             sorted({name_key(t) for t in acc_d.get("tagged") or []}), [] if key == "all" else sorted({name_key(x[4]) for x in left}))
        A.eq(f"{tag} 'Not counted' tags in the sidebar (accounts under $1 wait behind 'show all')", acc_d.get("side_tagged"),
             0 if key == "all" else sum(x[5] for x in left))
        line = [p["net" if key == "all" else "custom_net"] for p in history]
        A.eq(f"{tag} the net worth chart plots {'net' if key == 'all' else 'custom_net'}", acc_d.get("nwchart"),
             line if len(history) >= 2 else None)
    # the Edit sheet
    sh = dom.get("sheet") or {}
    before = sh.get("before") or {}
    rows = [r for g in before.get("groups", []) for r in g["rows"]]
    A.ok("Edit sheet: opens from the Accounts card", before.get("open"), got=before.get("open"), want=True)
    A.eq("Edit sheet: lists every account (none archived), once", sorted(r["id"] for r in rows), sorted(x[0] for x in accts))
    A.eq("Edit sheet: each switch = counted or not", sorted((r["id"], r["on"]) for r in rows),
         sorted((x[0], "false" if x[0] in sel else "true") for x in accts))
    A.eq("Edit sheet: each row's amount = its signed balance", sorted((r["id"], money(r["amt"])) for r in rows),
         sorted((x[0], r2(x[2] if x[3] else -x[2])) for x in accts))
    A.eq("Edit sheet: groups in the Accounts page's order", [g["key"] for g in before.get("groups", [])],
         [g["key"] for g in nw["breakdown"] if g["count"]])
    for g in before.get("groups", []):
        n = sum(r["on"] == "true" for r in g["rows"])
        A.eq(f"Edit sheet: {g['key']} group switch / tally / total", [g["state"], g["tally"], money(g["total"])],
             ["true" if n == len(g["rows"]) else "mixed" if n else "false", f"{n} of {len(g['rows'])} counted",
              r2(sum(money(r["amt"]) for r in g["rows"] if r["on"] == "true"))])
    A.eq("Edit sheet: the preview = Custom's net", money(before.get("preview")), c.get("net"))
    A.eq("Edit sheet: the preview's Own / Owe = Custom's", monies(before.get("own") or "")[:2], [c.get("assets"), c.get("debts")])
    # TASK-22b: a payment in transit to a card switched on in the sheet counts as paid there, with its line
    on = {r["id"] for r in rows if r["on"] == "true"}
    paid = lambda i: r2(sum(e["amount"] for e in in_transit if e["card_id"] == i))  # noqa: E731
    A.eq("Edit sheet: the preview = the sum of the switched-on rows, their cards' payments in transit counted as paid",
         money(before.get("preview")), r2(sum(money(r["amt"]) for r in rows if r["on"] == "true") + sum(paid(i) for i in on)))
    A.eq("Edit sheet: a 'Payment in transit' line under the preview for each one to a card switched on",
         before.get("transit"), [transit_ui(e) for e in in_transit if e["card_id"] in on])   # (TASK-22f's line)
    fl = sh.get("flipped") or {}
    was = next((r for r in rows if r["id"] == fl.get("id")), None)
    now_on = next((r["on"] for g in fl.get("groups", []) for r in g["rows"] if r["id"] == fl.get("id")), None)
    if rows and (was is None or now_on is None):
        A.ok("Edit sheet: a switch flipped (the first row)", False, got=fl.get("id"), want="a row flipped")
    elif rows:
        step = money(was["amt"]) + paid(was["id"])
        step = -step if was["on"] == "true" else step
        A.eq(f"Edit sheet: flipping {was['name']} moves the preview by exactly its signed balance (and its payment in transit)",
             [money(fl.get("preview")), now_on], [r2(money(before.get("preview")) + step), "false" if was["on"] == "true" else "true"])
    A.eq("Edit sheet: flipping it back restores the preview", money((sh.get("restored") or {}).get("preview")),
         money(before.get("preview")))
    A.ok("Edit sheet: × closes it", sh.get("closed"), got=sh.get("closed"), want=True)
    A.eq("Edit sheet: closing with nothing changed saved nothing", settings_after.get("networth_excluded"),
         settings.get("networth_excluded"))


SOURCE_PROVIDER = {"Carfax": "carfax", "Redfin": "redfin", "Photos": "commons"}   # admin.VALUATIONS -> api_usage


def check_settings(A: Audit, S: Site, d: dict, db: Path | None) -> None:
    """The Settings page (TASK-18): the Plaid slots it shows = /api/status (= every Item ever exchanged, from the file);
    each value source's "Used this month N / cap" and its meter = `finance api-usage` (the calls this month and the cap
    that stops them); a switch = `finance settings` unless the env file sets it (then it's locked and says so); the
    export's count = every transaction the Transactions page counts (= the file's rows not removed)."""
    status = S.get("/api/status")
    used, limit = status["slots_used"], status["slot_limit"]
    A.eq("Settings 'N/10' slots = /api/status (the sidebar's)", d["slotn"], f"{used}/{limit}")
    A.ok("Settings 'N of 10 used' = /api/status", bool(d["slotline"]) and d["slotline"].startswith(f"{used} of {limit} used"),
         got=d["slotline"], want=f"{used} of {limit} used ...")
    bar = d["slotbar"] or {}
    A.eq("Settings slot meter (aria) = used / limit", (bar.get("now"), bar.get("max")), (str(used), str(limit)))
    A.eq("Settings slot meter fill = used / limit", bar.get("fill"), min(used / limit, 1), tol=0.01)
    usage, prefs_cli, rows = {}, {}, None
    if db:
        usage = {p["provider"]: p for p in run_cli(db, "api-usage")["providers"]}
        prefs_cli = run_cli(db, "settings")["valuations"]
        conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
        A.eq("slots = every Item ever exchanged (the file)", used, conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], tol=0)
        rows = conn.execute("SELECT COUNT(*) FROM transactions WHERE removed = 0").fetchone()[0]
        conn.close()
    page = S.get("/api/settings/page")
    sources = d["sources"] or []
    A.eq("Settings shows every value source", [x["name"] for x in sources], list(SOURCE_PROVIDER))
    for x, v in zip(sources, page["valuations"]):
        name = x["name"]
        m = re.fullmatch(r"Used this month ([0-9]+) / ([0-9]+)", (x["use"] or "").strip())
        A.ok(f"{name}: 'Used this month N / cap' is shown", bool(m), got=x["use"], want="Used this month N / cap")
        if not m:
            continue
        n, cap = int(m.group(1)), int(m.group(2))
        want = usage.get(SOURCE_PROVIDER.get(name, "")) or {"calls": v["used"], "cap": v["cap"]}
        how = "finance api-usage" if usage else "/api/settings/page"
        A.eq(f"{name}: calls this month = {how}", n, want["calls"], tol=0)
        A.eq(f"{name}: cap = {how}", cap, want["cap"], tol=0)
        b = x["bar"] or {}
        A.eq(f"{name}: meter (aria) = calls / cap", (b.get("now"), b.get("max")), (str(n), str(cap)))
        A.eq(f"{name}: meter fill = calls / cap", b.get("fill"), min(n / cap, 1) if cap else 0, tol=0.01)
        if v["env_flag"]:
            A.ok(f"{name}: set in the env file: the switch is locked and says where", x["locked"] is True
                 and f"Set by {v['env_flag']} in your env file" in x["text"], got=(x["locked"], x["text"][:120]), want=v["env_flag"])
            A.eq(f"{name}: switch = the env file's value", x["on"], str(v["on"]).lower())
        else:
            A.eq(f"{name}: switch = {'finance settings' if prefs_cli else '/api/settings/page'}", x["on"],
                 str(prefs_cli[v["key"]] if prefs_cli else v["on"]).lower())
    m = re.search(r"every transaction \(([0-9,]+)\)", (d["exported"] or "").lower())
    # the count now lives in the Export action's disclosure (text-density): read it there, not from a card paragraph
    A.ok("Settings Data: the export's count is shown (in its disclosure)", bool(m), got=(d["exported"] or "")[:80],
         want="every transaction (N)")
    if m:
        n = int(m.group(1).replace(",", ""))
        A.eq("export count = Transactions page 'All' count", n, S.get("/api/transactions", kind="all", limit=1)["total"], tol=0)
        if rows is not None:
            A.eq("export count = every transaction not removed (the file)", n, rows, tol=0)


def check_percentages(A: Audit, dom: dict, ov: dict, bp: dict, debt: dict, inv: dict, acc: dict,
                      goals: dict | None = None) -> None:
    """Each percentage the pages show, recomputed from the API numbers it's made of (the ones shown next to it are
    checked above), within 0.5 point (0.05 for one shown with a decimal). One the audit can't place is a FAIL, so
    none goes unchecked."""
    seen = unknown = 0
    for page, d in dom.items():
        lists = [d.get("pcts", [])] + [d[k] for k in d if k.startswith("pcts_")]
        for p in (x for lst in lists for x in lst):
            seen += 1
            m = re.search(r"([-\u2212]?[0-9]+(?:\.([0-9]+))?)%", p["node"])   # (TASK-22f: a U+2212 minus is a minus)
            shown, decimals = float(m.group(1).replace("\u2212", "-")), len(m.group(2) or "")
            where = f"{page}: '{p['node']}'" + (f" ({p.get('key')})" if p.get("key") else "")
            if p["node"] in STATIC_PCT:
                A.ok(f"{where} is a fixed threshold", True, got=p["node"], want="static")
                continue
            if p["kind"] == "where":
                A.ok(f"{where} (checked above, over the included total)", True, got=shown, want="row check")
                continue
            want, how = pct_want(p, ov, bp, debt, inv, acc, goals)
            if want is None:
                unknown += 1
                A.ok(f"{where}: a percentage the audit can't place (kind {p['kind']}) in '{p['row'][:90]}'", False,
                     got=p["node"], want="checked")
                continue
            ringish = p["kind"] in ("left_ring", "budget_summary", "budget_card", "alert", "budget_row")
            tol = (ring_tol(want) if ringish else PCT_TOL) if decimals == 0 else 0.05 + 1e-6
            A.eq(f"{where} = {how}", shown, want, tol=tol)
    A.ok("every percentage on every page was checked", unknown == 0, got=f"{seen - unknown} of {seen}", want=f"{seen} of {seen}")


RING_COLOUR = {"ok": "#4fd1a5", "warn": "#f5b454", "over": "#ff7b7b", "nocap": "#8ea8ff"}
RING_ARIA = re.compile(r"^(-?[0-9]+)% of (?:suggested )?budget (?:with bills still due: (\S+) spent, (\S+) due, of (\S+)|used: (\S+) spent of (\S+))$")
RING_NAMES = {"left_ring": "Overview 'Left to spend' ring", "overview_row": "Overview Budgets ring",
              "budget_summary": "Budgets summary ring", "budget_card": "Budgets row progress line"}


def cap_rule(committed: float, cap: float | None) -> str:
    """The status rule (TASK-4, insights.cap_status) written again: in cents, over at 100% of the cap, warn at 80%."""
    if cap is None:
        return "nocap"
    s, c = round(committed * 100), round(cap * 100)
    if c <= 0:
        return "over" if s > 0 else "ok"
    if s >= c:
        return "over"
    return "warn" if s * 10 >= c * 8 else "ok"


def fullness(committed: float, cap: float) -> float:
    """(spent + due) / cap, the way the page divides (a $0 cap: full once anything is spent)."""
    return committed / cap if cap > 0 else (1.0 if committed > 0 else 0.0)


def group_budget_amounts(db: Path | None, bp: dict) -> dict[str, float]:
    """polish P2E: {group: its own monthly budget}, read from the database (group_budgets) with --db, else the API's
    groups. A database from before v18 has none."""
    if db is None:
        return {g["name"]: g["budget"] for g in bp["groups"] if g.get("budget") is not None}
    con = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        return {n: a for n, a in con.execute("SELECT g.name, b.amount FROM group_budgets b JOIN category_groups g ON g.id = b.group_id")}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def check_rings(A: Audit, bp: dict, dom: dict, cli: dict) -> None:
    """TASK-14b: every budget ring on the Overview and Budgets pages counts the bills still due. data-ring-pct =
    round((spent + due) / cap x 100) (half up, as Math.round) and data-ring-status = the status rule on spent + due,
    against `finance budget-status --json` (the API's numbers without --db); the % and the label shown, the arcs (spent
    in the status colour, the bills after it at 45%) and the aria-label from the same numbers; the Overview and Budgets
    summary rings agree; (P2A) each budget row's progress line is its ring."""
    A.sect("budget rings: spent + bills still due (TASK-14b)")
    b = bp["budgets"]
    if cli:
        t = cli["budget"]["totals"]
        total, caps, src = (t["spent"], t["due"], t["budgeted"]), {c["category"]: c for c in cli["budget"]["caps"]}, "finance budget-status"
        gcaps = {g["group"]: {**g, "cap": g["budget"]} for g in cli["budget"].get("groups") or []}
    else:
        total, src = (b["total_spend"], b["total_due"], b["total_cap"]), "the API (no --db)"
        caps = {c["category"]: {"spent": c["spend"], "due": c["due"], "cap": c["cap"], "status": c["status"]}
                for c in bp["categories"] if c["cap"] is not None}
        gcaps = {u["group"]: {"spent": u["spend"], "due": u["due"], "cap": u["budget"], "status": u["status"]}
                 for u in b.get("group_budgets") or []}
    cats = {c["category"]: c for c in bp["categories"]}
    sug = {c["category"]: c["suggested"] for c in bp["suggestions"]["categories"] if c["suggested"] is not None}
    found: dict[str, list] = {"overview": [], "budgets": []}
    for page in found:
        for r in dom[page].get("rings", []):
            where = RING_NAMES[r["kind"]] + (f" {r['key']}" if r["key"] else "")
            c, frm = None, src
            if r["kind"] in ("left_ring", "budget_summary"):
                if bp["suggestion_hero"]:
                    continue  # no caps: the Budgets summary is the suggestions' ring, as before (checked with the %s)
                spend, due, cap = total
            elif r.get("group"):   # polish P2E: a group budget's Overview row
                if r["group"] not in gcaps:
                    A.ok(f"{where}: a group budget in {src}", False, got=r["group"], want="a group budget")
                    continue
                c = gcaps[r["group"]]
                spend, due, cap = c["spent"], c["due"], c["cap"]
            elif r["key"] in caps:
                c = caps[r["key"]]
                spend, due, cap = c["spent"], c["due"], c["cap"]
            elif bp["suggestion_hero"] and r["kind"] == "budget_card" and r["key"] in sug:
                spend, due, cap = cats[r["key"]]["spend"], cats[r["key"]]["due"], sug[r["key"]]
                where, frm = f"{where} (suggested)", "the API's suggestion"
            else:
                A.ok(f"{where}: a capped category in {src}", False, got=r["key"], want="a cap")
                continue
            found[page].append(r)
            committed = spend + due
            raw = fullness(committed, cap) * 100
            pct = math.floor(raw + 0.5)
            if pct == 100 and raw != 100:  # the page never shows 100% unless it's exactly at the cap
                pct = 99 if raw < 100 else 101
            status = cap_rule(committed, cap)
            A.eq(f"{where}: data-ring-pct = round((spent + due) / cap x 100), {frm}", r["pct"], pct, tol=0)
            A.eq(f"{where}: data-ring-status = the status rule on spent + due (red at 100%, amber at 80%)", r["status"], status)
            if c is not None:
                A.eq(f"{where}: data-ring-status = {src}'s status", r["status"], c["status"])
            if r["kind"] != "overview_row":  # the rows show the category's icon in the ring
                A.eq(f"{where}: the % shown = data-ring-pct", r["text"], f"{min(max(pct, 0), 999)}%")
            A.eq(f"{where}: the label under the %", r["label"],
                 "with bills" if due > 0 and r["kind"] != "overview_row" else "used" if r["kind"] in ("left_ring", "budget_summary") else None)
            share = lambda x: min(max(fullness(x, cap), 0.0), 1.0)  # noqa: E731
            colour = RING_COLOUR[status]
            want = [a for a in ([(colour, "0.45", share(committed))] if due > 0 else []) + [(colour, None, share(spend))] if a[2] > 0.003]
            got = [(a["stroke"], a["opacity"], a["share"]) for a in r["arcs"]]
            A.ok(f"{where}: the arcs: spent in the status colour" + (", the bills still due after it at 45%" if due > 0 else ", no second shade"),
                 len(got) == len(want) and all(g[:2] == w[:2] and abs(g[2] - w[2]) <= 0.002 for g, w in zip(got, want)),
                 got=[(x, o, round(v, 3)) for x, o, v in got], want=[(x, o, round(v, 3)) for x, o, v in want])
            m = RING_ARIA.match(r["aria"] or "")
            amounts = [x for x in (m.groups()[1:] if m else ()) if x is not None]
            of = "budget" if frm == src else "suggested budget"
            A.ok(f"{where}: aria-label spells it out" + (" (with bills still due)" if due > 0 else ""),
                 r["role"] == "img" and bool(m) and int(m.group(1)) == pct and r["aria"].startswith(f"{m.group(1)}% of {of} ")
                 and (m.group(2) is not None) == (due > 0)
                 and all(money(x) is not None and abs(money(x) - y) <= 0.5 for x, y in zip(amounts, (spend, due, cap) if due > 0 else (spend, cap))),
                 got=r["aria"], want=f"{pct}% of {of} " + (f"with bills still due: ${spend:,.0f} spent, ${due:,.0f} due, of ${cap:,.0f}"
                                                         if due > 0 else f"used: ${spend:,.0f} spent of ${cap:,.0f}"))
    left = [r for r in found["overview"] if r["kind"] == "left_ring"]
    summ = [r for r in found["budgets"] if r["kind"] == "budget_summary"]
    if b["count"]:
        A.eq("one 'Left to spend' ring on the Overview, one summary ring on Budgets", [len(left), len(summ)], [1, 1])
        if left and summ:
            A.eq("the Overview and Budgets summary rings show the same % and status", [left[0]["pct"], left[0]["status"]],
                 [summ[0]["pct"], summ[0]["status"]])
        A.eq(f"every cap in {src} has its ring on Budgets", sorted(r["key"] for r in found["budgets"] if r["kind"] == "budget_card"),
             sorted(caps))
        A.eq("a ring or bar on every Overview Budgets row", len([r for r in found["overview"] if r["kind"] == "overview_row"]),
             len(dom["overview"].get("budgets") or []))
    # P2A: the rows are grouped by category group, in the taxonomy's order, no longer fullest ring first. What replaces the
    # ordering check: every row sits under the header of its own group.
    groups_of = {r["name"]: r["group"] for r in dom["budgets"].get("cards", [])}
    A.ok("every Budgets row sits in its own category group", all(groups_of.get(k) == cats[k].get("group") for k in groups_of if k in cats),
         got=sorted((k, v) for k, v in groups_of.items() if k in cats and cats[k].get("group") != v), want="rows under their group")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base_url")
    ap.add_argument("--db", type=Path, help="the dashboard's SQLite file, for the read-only CLI checks")
    ap.add_argument("--json", type=Path, help="also write the results here")
    args = ap.parse_args(argv)
    A = audit(args.base_url, args.db)
    fails = A.fails()
    print(f"\n{len(A.results)} checks: {len(A.results) - len(fails)} PASS, {len(fails)} FAIL")
    if args.json:
        args.json.write_text(json.dumps({"base_url": args.base_url, "results": A.results}, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
