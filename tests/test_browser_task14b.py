"""TASK-14b: every budget ring counts the bills still due, with one colour rule, in headless Chrome (tests/cdp.py; https
and plaid.com blocked) over throwaway loopback servers on small fixture DBs. The server's today and the page's clock
are both pinned to 2026-09-27 (the page reads its month from the browser's clock), so the fixtures don't move with the
day the suite runs.

- September's real shape (caps $1,960, spent $1,746.85, Zenith $465.10 still due in Other Transport): the Overview "Left to
  spend" ring and the Budgets summary ring both read 113%, red, "with bills", the bills as a lighter second arc; the
  category cards 344 / 134 / 99 (Other Transport, amber: 99.8%, never "100%" under the cap) / 75 / 74 / 66 / 0; the cards run fullest first.
- The same month with no bill due, and a past month: "used", no second shade, the two summary rings the same colour.
- 79% / 80% / 99.8% / 99.9% / 100% of a single cap (so every ring kind shows the same ratio), with and without a bill
  due: green / amber / amber / amber / red on the Overview ring, its Budgets row, the Budgets summary and the card.
- "with bills" stays inside the 70px Overview ring's hole at 1440 / 1024 / 390, and nothing scrolls sideways."""

from __future__ import annotations

import math
import time
from datetime import date

import pytest

from cdp import Chrome, Server, find_chrome
from factory import PFC, basic, cap, tx
from hermes_finance import classify, db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")

TODAY = date(2026, 9, 27)
GREEN, AMBER, RED = "#4fd1a5", "#f5b454", "#ff7b7b"
COLOUR = {"ok": GREEN, "warn": AMBER, "over": RED}
ZENITH = 465.10
# category: (cap, spent in September), as on the real DB on 2026-09-27
SEPT = {"Other Shopping": (200, 688.15), "Eating Out": (150, 201.71), "Other Health": (300, 224.06), "Subscriptions": (50, 36.77),
        "Groceries": (400, 262.79), "Other Transport": (800, 333.37), "Entertainment": (60, 0.0)}

# the page's clock at noon on 2026-09-27 (still ticking, so timers and Date.now() differences behave)
CLOCK = """(() => { const fixed = new Date(2026, 8, 27, 12, 0, 0).getTime(), t0 = Date.now(), Real = Date;
  class D extends Real { constructor(...a) { if (a.length) super(...a); else super(fixed + Real.now() - t0); }
    static now() { return fixed + Real.now() - t0; } }
  window.Date = D; })();"""

RINGS = r"""(() => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
  const where = (r) => {
    if (r.closest('.bsum')) return 'summary';
    const row = r.closest('.brow');
    if (row) return 'row:' + T(row.querySelector('.grow > b'));
    const card = r.closest('.card');
    return card && T(card.querySelector('h3 .ttl')) === 'Left to spend' ? 'left' : null;
  };
  const out = {};
  for (const r of document.querySelectorAll('#page .ring')) {
    const k = where(r);
    if (!k) continue;
    out[k] = {pct: r.dataset.ringPct, status: r.dataset.ringStatus, text: T(r.querySelector('.rin > b')),
      label: T(r.querySelector('.rin > small')), aria: r.getAttribute('aria-label'), role: r.getAttribute('role'),
      arcs: [...r.querySelector(':scope > svg').querySelectorAll(':scope > circle')].slice(1).map((c) => [c.getAttribute('stroke'),
        c.getAttribute('stroke-opacity'), Number(c.getAttribute('stroke-dasharray').split(' ')[0]) / (2 * Math.PI * Number(c.getAttribute('r')))])};
  }
  for (const h of document.querySelectorAll('#page .hairline[data-ring-pct]')) {
    const k = 'row:' + T(h.closest('.brow').querySelector('.grow b'));
    out[k] = {pct: h.dataset.ringPct, status: h.dataset.ringStatus, text: null, label: null, aria: h.getAttribute('aria-label'), role: h.getAttribute('role'),
      arcs: [...h.querySelectorAll(':scope > i')].map((i) => [i.dataset.c, i.dataset.o || null, parseFloat(i.style.width) / 100]), type: 'hairline'};
  }
  // P2A: a budget is a table row whose progress line (.hair) carries the same data-ring-pct / status / aria as the ring did.
  // Its bars are read back in the ring's shape: [colour, opacity, fraction of the cap], the lighter bills-due bar (the whole
  // committed amount) first, then the solid spent bar.
  const hex = (c) => '#' + c.match(/\d+/g).slice(0, 3).map((n) => Number(n).toString(16).padStart(2, '0')).join('');
  for (const r of document.querySelectorAll('#page tr.c .hair')) {
    const bars = [...r.querySelectorAll(':scope > i')], frac = (i) => parseFloat(i.style.width) / 100;
    const spent = bars.find((i) => !i.classList.contains('d')), due = bars.find((i) => i.classList.contains('d'));
    const arcs = [];
    if (due) arcs.push([hex(getComputedStyle(due).backgroundColor), '0.45', frac(spent) + frac(due)]);
    if (frac(spent) > 0) arcs.push([hex(getComputedStyle(spent).backgroundColor), null, frac(spent)]);
    out['card:' + r.closest('tr.c').dataset.category] = {pct: r.dataset.ringPct, status: r.dataset.ringStatus,
      text: r.dataset.ringPct + '%', label: due ? 'with bills' : null, aria: r.getAttribute('aria-label'),
      role: r.getAttribute('role'), arcs};
  }
  out._order = [...document.querySelectorAll('#page tr.c')].map((r) => r.dataset.category);
  out._subs = [...document.querySelectorAll('#page .card .sub')].map(T);
  return out;
})()"""

# the glyph box of each line of text in a ring (canvas measure in its own font, on its baseline) vs the arc's inner edge
FIT = r"""(() => { const ctx = document.createElement('canvas').getContext('2d');
  return [...document.querySelectorAll('#page .ring[data-ring-pct]')].flatMap((r) => {
    const R = r.getBoundingClientRect(), cx = R.left + R.width / 2, cy = R.top + R.height / 2;
    const track = r.querySelector(':scope > svg > circle');
    const hole = Number(track.getAttribute('r')) - Number(track.getAttribute('stroke-width')) / 2;
    return [...r.querySelectorAll('.rin > b, .rin > small')].map((e) => {
      const cs = getComputedStyle(e);
      ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
      ctx.letterSpacing = cs.letterSpacing === 'normal' ? '0px' : cs.letterSpacing;
      const m = ctx.measureText(e.textContent), rg = document.createRange();
      rg.selectNodeContents(e);
      const lines = [...rg.getClientRects()], q = lines[0], base = q.bottom - m.fontBoundingBoxDescent;
      const far = Math.max(...[q.left - m.actualBoundingBoxLeft, q.left + m.actualBoundingBoxRight].flatMap((x) =>
        [base - m.actualBoundingBoxAscent, base + m.actualBoundingBoxDescent].map((y) => Math.hypot(x - cx, y - cy))));
      return {text: e.textContent, size: R.width, hole, far, lines: lines.length, font: cs.fontSize};
    });
  });
})()"""


def build(path, caps: dict, zenith: bool = True):
    """caps: category -> (cap, spent this month). Zenith: a $465.10 car payment (Other Transport) on the 29th of June, July
    and August, so on Sep 27 it's a monthly bill still due on the 29th."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    basic(conn)
    for category, (value, spent) in caps.items():
        if spent:
            tid = tx(conn, "2026-09-05", spent, f"{category} shop", cat=category if category in PFC else "Other Shopping")
            if category not in PFC:  # Subscriptions: Plaid has none, set by hand as on the real DB
                classify.set_category(conn, tid, category)
        cap(conn, category, value)
    if zenith:
        for d in ("2026-06-29", "2026-07-29", "2026-08-29"):
            tid = tx(conn, d, ZENITH, "Zenith Financial Services", cat="Car payment")
            classify.set_category(conn, tid, "Other Transport")   # by hand: from v16 the car-payment code is Car Payment
    conn.close()
    return path


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        b.call("Page.addScriptToEvaluateOnNewDocument", source=CLOCK)
        b.size(1440, 900)
        yield b


@pytest.fixture(scope="module")
def serve(tmp_path_factory):
    servers = []

    def make(name, caps, zenith=True):
        srv = Server(build(tmp_path_factory.mktemp("rings14b") / name / "finance.db", caps, zenith), today=TODAY)
        servers.append(srv)
        return srv
    yield make
    for s in servers:
        s.close()


def load(b, srv, page, month=None):
    b.goto(f"{srv.url}/?t={time.time():.3f}#/{page}" + (f"?month={month}" if month else ""))
    b.settle()
    return b.js(RINGS)


def api(b, path):
    return b.js(f"fetch({path!r}).then((r) => r.json())")


def pct_of(committed, capv):  # the page's Math.round (half up), never "100" unless exactly at the cap
    raw = committed / capv * 100
    pct = math.floor(raw + 0.5)
    return (99 if raw < 100 else 101) if pct == 100 and raw != 100 else pct


def arcs_ok(got, want):
    return len(got) == len(want) and all(g[0] == w[0] and g[1] == w[1] and abs(g[2] - w[2]) < 0.002 for g, w in zip(got, want))


def test_september_real_shape(browser, serve):
    """The bug: 89% used, green on the Overview and amber on Budgets, while the line beside them says over budget."""
    srv = serve("sept", SEPT)
    ov = load(browser, srv, "overview")
    assert browser.problems() == []
    t = api(browser, "/api/budgets")["budgets"]
    assert (t["total_cap"], t["total_spend"], t["total_due"], t["left"]) == (1960.0, 1746.85, 465.1, -251.95)
    left = ov["left"]
    assert (left["pct"], left["status"], left["text"], left["label"], left["role"]) == ("113", "over", "113%", "with bills", "img")
    assert arcs_ok(left["arcs"], [(RED, "0.45", 1.0), (RED, None, 1746.85 / 1960)])      # spent, then the bill lighter
    assert left["aria"] == "113% of budget with bills still due: $1,747 spent, $465 due, of $1,960"
    assert any(s.startswith("Over budget by $252 once bills clear") for s in ov["_subs"])  # the line beside it
    # the Overview Budgets rows (top 5 by spent / cap): hairline, colour from spent + due, no label
    for name, status in (("Other Shopping", "over"), ("Eating Out", "over"), ("Other Health", "ok"), ("Subscriptions", "ok"), ("Groceries", "ok")):
        r = ov[f"row:{name}"]
        capv, spent = SEPT[name]
        assert (r["pct"], r["status"], r["label"], r["text"]) == (str(pct_of(spent, capv)), status, None, None), name
        assert r["type"] == "hairline" and r["role"] == "img", name
        assert arcs_ok(r["arcs"], [(COLOUR[status], None, min(spent / capv, 1))]), (name, r["arcs"])

    bp = load(browser, srv, "budgets")
    assert browser.problems() == []
    s = bp["summary"]
    assert (s["pct"], s["status"], s["text"], s["label"], s["aria"]) == (left["pct"], left["status"], left["text"], left["label"], left["aria"])
    assert arcs_ok(s["arcs"], [(RED, "0.45", 1.0), (RED, None, 1746.85 / 1960)])
    want = {"Other Shopping": ("344", "over"), "Eating Out": ("134", "over"), "Other Transport": ("99", "warn"), "Other Health": ("75", "ok"),
            "Subscriptions": ("74", "ok"), "Groceries": ("66", "ok"), "Entertainment": ("0", "ok")}
    status = {c["category"]: c["status"] for c in api(browser, "/api/budgets")["categories"] if c["cap"] is not None}
    for name, (pct, st) in want.items():
        r = bp[f"card:{name}"]
        assert (r["pct"], r["status"], r["text"]) == (pct, st, f"{pct}%"), (name, r)
        assert r["status"] == status[name], name                                          # = capStatus / the chip's status
    # Other Transport: 333.37 + 465.10 of 800 = 99.8% -> "99%", amber exactly as capStatus says; the bill after the spend
    tr = bp["card:Other Transport"]
    assert tr["label"] == "with bills"
    assert arcs_ok(tr["arcs"], [(AMBER, "0.45", 798.47 / 800), (AMBER, None, 333.37 / 800)])
    assert tr["aria"] == "99% of budget with bills still due: $333 spent, $465 due, of $800"
    assert bp["card:Other Shopping"]["label"] is None and arcs_ok(bp["card:Other Shopping"]["arcs"], [(RED, None, 1.0)])
    assert bp["card:Other Health"]["aria"] == "75% of budget used: $224 spent of $300"
    assert bp["card:Entertainment"]["arcs"] == []
    # every capped category is a row (P2A: in the taxonomy's order within its group, no longer fullest ring first)
    assert sorted(bp["_order"]) == sorted(want)


def test_no_bills_due_looks_like_before_with_one_colour(browser, serve):
    """No bill due (the same month without Zenith, and a past month): "used", no second shade; both summary rings amber
    at 89% (the Overview's was green)."""
    srv = serve("nobill", SEPT, zenith=False)
    ov, bp = load(browser, srv, "overview"), load(browser, srv, "budgets")
    for r in (ov["left"], bp["summary"]):
        assert (r["pct"], r["status"], r["text"], r["label"]) == ("89", "warn", "89%", "used")
        assert arcs_ok(r["arcs"], [(AMBER, None, 1746.85 / 1960)])
        assert r["aria"] == "89% of budget used: $1,747 spent of $1,960"
    tr = bp["card:Other Transport"]
    assert (tr["pct"], tr["status"], tr["label"]) == ("42", "ok", None) and arcs_ok(tr["arcs"], [(GREEN, None, 333.37 / 800)])
    assert browser.problems() == []

    past = serve("past", SEPT)                                        # August: only the Zenith payment, nothing due
    ov, bp = load(browser, past, "overview", "2026-08"), load(browser, past, "budgets", "2026-08")
    for r in (ov["left"], bp["summary"]):
        assert (r["pct"], r["status"], r["label"]) == (str(pct_of(ZENITH, 1960)), "ok", "used") and len(r["arcs"]) == 1
    assert (bp["card:Other Transport"]["pct"], bp["card:Other Transport"]["label"], len(bp["card:Other Transport"]["arcs"])) == ("58", None, 1)
    assert browser.problems() == []


# (cap, committed, shown %, status): 79% / 80% / 99.8% (Transport-style) / 99.9% / 100%
BOUNDARIES = [(1000, 790.00, 79, "ok"), (1000, 800.00, 80, "warn"), (800, 798.47, 99, "warn"), (1000, 999.00, 99, "warn"),
              (1000, 1000.00, 100, "over")]


@pytest.mark.parametrize("bill", [True, False], ids=["with-bill", "no-bill"])
@pytest.mark.parametrize("capv,committed,pct,status", BOUNDARIES, ids=["79", "80", "99.8", "99.9", "100"])
def test_boundaries_on_every_ring_kind(browser, serve, capv, committed, pct, status, bill):
    """One cap (Other Transport), so the Overview ring, its Budgets row, the Budgets summary and the card all show the same
    ratio: each one the same %, status and colour. With the bill, the spend is committed - $465.10."""
    spent = round(committed - (ZENITH if bill else 0), 2)
    srv = serve(f"b{capv}-{committed}-{bill}", {"Other Transport": (capv, spent)}, zenith=bill)
    ov, bp = load(browser, srv, "overview"), load(browser, srv, "budgets")
    rings = {"Overview ring": ov["left"], "Overview row": ov["row:Other Transport"], "Budgets summary": bp["summary"],
             "Budgets card": bp["card:Other Transport"]}
    for kind, r in rings.items():
        assert (r["pct"], r["status"]) == (str(pct), status), (kind, r)
        assert {a[0] for a in r["arcs"]} == {COLOUR[status]}, (kind, r["arcs"])          # ring arcs or bar shades alike
        assert [a[1] for a in r["arcs"]] == (["0.45", None] if bill else [None]), (kind, r["arcs"])
        if kind != "Overview row":
            assert r["text"] == f"{pct}%", kind
    assert rings["Overview ring"]["label"] == rings["Budgets summary"]["label"] == ("with bills" if bill else "used")
    assert rings["Budgets card"]["label"] == ("with bills" if bill else None)
    tr = next(c for c in api(browser, "/api/budgets")["categories"] if c["category"] == "Other Transport")
    assert tr["status"] == status                                     # the backend's cap_status: one rule everywhere
    assert browser.problems() == []


@pytest.mark.parametrize("width,height", [(1440, 900), (1024, 768), (390, 844)])
def test_with_bills_fits_the_ring(browser, serve, width, height):
    """ "with bills" in the 70px Overview ring (and the 84px ones on Budgets): the glyphs clear the arc's inner edge, on
    one line, and the page doesn't scroll sideways."""
    srv = serve(f"fit{width}", SEPT)
    browser.size(width, height)
    try:
        for page in ("overview", "budgets"):
            load(browser, srv, page)
            texts = browser.js(FIT)
            assert any(t["text"] == "with bills" and t["size"] == (70 if page == "overview" else 84) for t in texts), texts
            for t in texts:
                assert t["lines"] == 1 and t["far"] <= t["hole"], (page, t)
            assert browser.js("document.documentElement.scrollWidth <= innerWidth"), page
        assert browser.problems() == []
    finally:
        browser.size(1440, 900)
