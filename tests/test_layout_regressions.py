"""Regression tests for the layout bugs the TASK-11 layout gate (scripts/audit_layout.py) found, one per bug, each
written to fail on the code before its fix (REPORT-11.md has the failing runs). Headless Chrome (tests/cdp.py) over the
gates' synthetic DBs (tests/gate_dbs.py); the packer cases run packLayout() in node.

HF_TEST_STATIC=<dir> serves another copy of static/ (used to show a test failing with its fix taken out)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import web

if os.environ.get("HF_TEST_STATIC"):
    web.STATIC_DIR = Path(os.environ["HF_TEST_STATIC"])
JS = (web.STATIC_DIR / "app.js").read_text()
NODE = shutil.which("node")
chrome = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
U, G = 4, 16
SIZES = {1440: 900, 1024: 768, 390: 844}
PAGES = ("overview", "transactions", "budgets", "recurring", "accounts", "investments", "debt")


# --- the packer: no near-miss tops, no holes under a card (Budgets at 1440, measured heights) --------------------

def run_pack(cases):
    fn = re.search(r"\n  function packLayout\(.*?\n  }\n", JS, re.S).group(0)
    code = re.search(r"\n  const PACK = \{.*?\};\n", JS, re.S).group(0) + fn
    prog = code + f"\nconst cases = {json.dumps(cases)};\nconsole.log(JSON.stringify(cases.map((c) => packLayout(c.items, c.cols))));"
    return json.loads(subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=60, check=True).stdout)


def card(h, chart=True, k=1):
    return {"spans": [{"k": k, "h": h}], "chart": chart, "pin": None}


NB = {"spans": [{"k": 1, "h": 806.5}, {"k": 2, "h": 484}, {"k": 3, "h": 484}], "chart": False, "pin": None}
# natural heights of the Budgets cards at 1440 on the gates' full DB (Other Transport has a "Still due" line: 1px taller)
BUDGETS = [card(391.25), card(392.3), card(391.25), card(391.25), card(391.25), NB]
BUDGETS_EDITOR = [card(431.75), card(392.3), card(391.25), card(391.25), card(391.25), NB]   # Other Health's editor open
# "See suggestions" opened: No budget yet grows to 849.5px in one column (527px two or three wide)
NB_OPEN = {"spans": [{"k": 1, "h": 849.5}, {"k": 2, "h": 527}, {"k": 3, "h": 527}], "chart": False, "pin": None}
BUDGETS_SUGGESTIONS = [card(391.25), card(392.3), card(391.25), card(391.25), card(391.25), NB_OPEN]


def problems(items, cols, layout):
    """Side-by-side cards whose tops are 4-12px apart, and cards that don't reach the next card below them (a gap
    bigger than the gutter)."""
    out = []
    for i, a in enumerate(layout):
        for b in layout[i + 1:]:
            side = a["c"] + a["k"] <= b["c"] or b["c"] + b["k"] <= a["c"]
            if side and 0 < abs(a["top"] - b["top"]) * U <= 12:
                out.append(("near-miss tops", a, b))
        for col in range(a["c"], a["c"] + a["k"]):
            below = [b["top"] for b in layout if b is not a and b["c"] <= col < b["c"] + b["k"] and b["top"] > a["top"]]
            if below and min(below) != a["top"] + a["rows"]:
                out.append(("gap under a card", a, (min(below) - a["top"] - a["rows"]) * U + G))
    for it, a in zip(items, layout):  # a chart card's canvas doesn't grow: stretched more than 40px it's an empty card
        if it["chart"] and a["rows"] * U - G - it["spans"][0]["h"] > 40 + U:
            out.append(("chart card stretched", a, round(a["rows"] * U - G - it["spans"][0]["h"])))
    return out


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_packed_cards_line_up_and_every_gap_is_the_gutter():
    for items in (BUDGETS, BUDGETS_EDITOR, BUDGETS_SUGGESTIONS):
        (layout,) = run_pack([{"items": items, "cols": 3}])
        assert problems(items, 3, layout) == [], layout


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_opening_see_suggestions_only_grows_its_card():
    """Opening "See suggestions" made the packer move No budget yet under everything, full width, and stretch
    Transport ~400px down beside it (a ragged bottom edge counted like a hole in the middle). Now the card stays in
    its column and grows."""
    before, after = run_pack([{"items": BUDGETS, "cols": 3}, {"items": BUDGETS_SUGGESTIONS, "cols": 3}])
    assert [(p["c"], p["k"], p["top"]) for p in after] == [(p["c"], p["k"], p["top"]) for p in before]
    assert after[-1]["rows"] > before[-1]["rows"]


# --- browser checks ----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sites(tmp_path_factory):
    out = {}
    for kind in gate_dbs.KINDS:
        path = gate_dbs.build(kind, tmp_path_factory.mktemp(f"reg-{kind}") / "finance.db")
        out[kind] = Server(path)
    yield out
    for s in out.values():
        s.close()


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


def open_page(b, srv, page, width):
    b.size(width, SIZES[width])
    b.goto(f"{srv.url}/?t={width}{time.time():.3f}#/{page}")
    b.settle()


MONEY_CUT = r"""(() => {
  const out = [];
  for (const el of document.querySelectorAll('#page *')) {
    if (!el.getClientRects().length || el.closest('[hidden]')) continue;
    const cs = getComputedStyle(el);
    const clip = (v) => v === 'hidden' || v === 'clip';
    if (!clip(cs.overflowX) || !/\$\s?[0-9]/.test(el.textContent)) continue;
    if (el.scrollWidth > el.clientWidth + 1 && !el.matches('.card, .grid, table')) out.push({text: el.textContent.trim().slice(0, 60), sw: el.scrollWidth, cw: el.clientWidth});
  }
  return out;
})()"""


@chrome
@pytest.mark.parametrize("width", (1440, 1024, 390))
def test_no_money_is_cut_off_in_a_row(browser, sites, width):
    """"$1.08 a month" (Recurring subscriptions), "$465.10 a month" (a manual loan beside its sparkline at 1024),
    "Suggested $500 · Use" (No budget yet at 1024), "2.18176 shares · $224.82 each" (Holdings at 1024): a figure in a
    row's subline wraps, it's never cut off."""
    cut = {}
    for kind in ("full", "nocaps"):
        for page in PAGES:
            open_page(browser, sites[kind], page, width)
            if page == "budgets" and browser.js("!!document.querySelector('#page .nb button.lnk')"):
                browser.js("[...document.querySelectorAll('#page .nb button.lnk')].find((b) => b.textContent === 'See suggestions').click()")
                browser.settle()
            found = browser.js(MONEY_CUT)
            if found:
                cut[f"{kind}/{page}"] = found
    assert not cut, cut


@chrome
def test_no_card_is_wider_than_its_box_at_1024(browser, sites):
    """At 1024 the content area is 688px: three columns of 217px cut off a budget card's figures, the Zenith payoff
    ring and an Investments tile's pill. Cards get two columns there, and a tile's pill wraps."""
    wide = {}
    for kind in gate_dbs.KINDS:
        for page in PAGES:
            open_page(browser, sites[kind], page, 1024)
            found = browser.js("[...document.querySelectorAll('#page .card')].filter((c) => c.scrollWidth > c.clientWidth + 1)"
                               ".map((c) => ({card: (c.querySelector('h3 .ttl, .hd b') || c).textContent.trim().slice(0, 30), sw: c.scrollWidth, cw: c.clientWidth}))")
            if found:
                wide[f"{kind}/{page}"] = found
    assert not wide, wide


@chrome
@pytest.mark.parametrize("width", (1440, 390))
def test_the_utilization_tick_sticks_out_of_its_bar(browser, sites, width):
    """The 30% tick on a card's utilization bar (Accounts) stands 3px above and below the bar; the row's subline
    style (overflow: hidden) used to cut it to the bar's height."""
    open_page(browser, sites["full"], "accounts", width)
    ticks = browser.js("""[...document.querySelectorAll('#page .li.acct .track.thin > s')].map((s) => {
      const t = s.getBoundingClientRect(); let cut = false;
      for (let a = s.parentElement; a && a !== document.body; a = a.parentElement) {
        const cs = getComputedStyle(a); if (cs.overflowY === 'visible' && cs.overflowX === 'visible') continue;
        const q = a.getBoundingClientRect(); if (t.top < q.top - 0.5 || t.bottom > q.bottom + 0.5) { cut = a.className; break; } }
      return {h: t.height, cut}; })""")
    assert len(ticks) >= 3 and all(t["cut"] is False and t["h"] >= 11.5 for t in ticks), ticks


@chrome
@pytest.mark.parametrize("width", (1440, 390))
def test_a_confirm_opened_from_the_loan_panel_is_on_top(browser, sites, width):
    """"Mark as paid off?" opened under the loan panel and its scrim (z-index 80 < 85 / 90): the dialog was dimmed and
    a click on it hit the scrim, which closed the panel."""
    open_page(browser, sites["full"], "debt", width)
    browser.js("document.querySelector('#page [data-loan]').click()")
    browser.js("new Promise((r) => setTimeout(r, 400))")
    browser.js("[...document.querySelectorAll('#panel .btn')].find((b) => b.textContent === 'Mark as paid off').click()")
    browser.settle()
    browser.js("new Promise((r) => setTimeout(r, 300))")   # the dialog's 220ms entrance (--mo-sheet) is over
    top = browser.js("""(() => { const s = document.getElementById('sheet').getBoundingClientRect();
      return [[s.left + s.width / 2, s.top + s.height / 2], [s.left + 20, s.top + 20], [s.right - 20, s.bottom - 20]]
        .map(([x, y]) => { const e = document.elementFromPoint(x, y); return e && e.closest('#sheet') ? 'sheet' : (e ? e.id || e.className : null); }); })()""")
    assert top == ["sheet"] * 3, top
    browser.js("[...document.querySelectorAll('#sheet button')].find((b) => b.textContent === 'Cancel').click()")


@chrome
def test_bank_initials_are_readable(browser, sites):
    """A bank without a cached logo shows its initials in white on its colour: Nova's #1d9fd0 gave 3.0:1. Too-light
    colours are darkened (same hue) to 4.5:1."""
    open_page(browser, sites["nocaps"], "accounts", 1440)
    bubbles = browser.js("""[...document.querySelectorAll('.ia:not(.logo)')].filter((e) => e.textContent.trim()).map((e) => {
      const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      const [r, g, b] = getComputedStyle(e).backgroundColor.match(/[0-9.]+/g).map(Number);
      return {text: e.textContent.trim(), ratio: 1.05 / (0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b) + 0.05)}; })""")
    assert len(bubbles) >= 3 and all(x["ratio"] >= 4.5 for x in bubbles), bubbles


@chrome
def test_a_callout_icon_sits_on_its_first_line(browser, sites):
    """The form's "Match payments from" callout: its check icon was 2.5px below the middle of the first line."""
    open_page(browser, sites["full"], "accounts", 1440)
    browser.js("[...document.querySelectorAll('#page [data-edit]')].find((b) => b.textContent.includes('Best Buy')).click()")
    browser.js("new Promise((res) => { const t = () => document.querySelector('#sheet .matchbox .ck') ? res(1) : setTimeout(t, 50); t(); })")
    dy = browser.js("""(() => { const box = document.querySelector('#sheet .matchbox'); const ck = box.querySelector('.ck').getBoundingClientRect();
      const rg = document.createRange(); rg.selectNodeContents(box.querySelector(':scope > span:not(.ck)'));
      const line = [...rg.getClientRects()].find((q) => q.height > 0);
      return (ck.top + ck.bottom) / 2 - (line.top + line.bottom) / 2; })()""")
    assert abs(dy) <= 1, dy
    browser.js("document.querySelector('#sheet [aria-label=Close]').click()")


HOLES = r"""(() => {
  const out = [];
  for (const grid of document.querySelectorAll('#page .grid')) {
    const cs = getComputedStyle(grid), gap = parseFloat(cs.columnGap);
    const cards = [...grid.children].filter((c) => c.getClientRects().length).map((c) => [c, c.getBoundingClientRect()]);
    let x = grid.getBoundingClientRect().left;
    const centres = cs.gridTemplateColumns.split(' ').map(parseFloat).map((w) => { const c = x + w / 2; x += w + gap; return c; });
    for (const [a, A] of cards) for (const cx of centres.filter((c) => c > A.left && c < A.right)) {
      const below = cards.filter(([b, B]) => b !== a && B.left < cx && B.right > cx && B.top >= A.bottom - 1).sort((p, q) => p[1].top - q[1].top)[0];
      const later = cards.find(([b, B]) => b !== a && B.top >= A.bottom - 1);
      const name = (el) => (el.querySelector('h3 .ttl, .hd b') || el).textContent.trim().slice(0, 24);
      if (below ? Math.abs(below[1].top - A.bottom - gap) > 1 : later)
        out.push({card: name(a), next: below ? name(below[0]) : 'nothing (a later card in another column)', gap: below ? Math.round(below[1].top - A.bottom) : null});
    }
  }
  return out;
})()"""


@chrome
def test_no_empty_column_on_a_two_column_grid(browser, sites):
    """At 1024 (two columns) a stack pinned beside a full-width card (Overview's Left to spend + On pace, Loans & debt's
    payoff + payments), or a lone card on its row (Coming up, Subscriptions, Accounts' Bank connections: approved density
    mock D2 B removed "Added by you", so it is no longer a stack), left a whole column empty (600-1128px). Stacks become
    a full row of their cards there; lone cards take the row."""
    holes = {}
    for page in ("overview", "recurring", "accounts", "debt"):
        open_page(browser, sites["full"], page, 1024)
        found = browser.js(HOLES)
        if found:
            holes[page] = found
    assert not holes, holes


@chrome
def test_a_suggestion_fits_a_one_column_no_budget_card(browser, sites):
    """A budget suggestion is never cut, in a one-column (336px, at 1024) "No budget yet" card.

    Rewritten 2026-10-02. This originally caught a real bug — `Suggested $500 · Use` was squeezed to 64px and clipped.
    The inline `.sg` line it measured is GONE: the suggestion moved into the row's own Details panel, so there is no
    inline text left to clip. The rule it protected still stands, so it is now checked on the elements that DO carry
    money and identity on that row — the name, the amount (when there is one), and the row itself — and the suggestion
    is checked inside the panel, where it now lives.
    """
    open_page(browser, sites["full"], "budgets", 1024)
    cut = browser.js("""(async () => {
      const copy = document.querySelector('#page .nb').cloneNode(true);
      copy.style.cssText = 'position:absolute;left:0;top:0;width:336px;min-height:0';
      document.getElementById('page').append(copy);
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      const out = [...copy.querySelectorAll('.li')].filter((e) => e.getClientRects().length).map((e) => {
        const name = e.querySelector('.grow > b, .grow > a');
        return {name: name.textContent, row: [e.scrollWidth, e.clientWidth],
          nameBox: [name.scrollWidth, name.clientWidth],
          amt: (() => { const a = e.querySelector('.amt'); return a ? [a.scrollWidth, a.clientWidth] : null; })()};
      });
      copy.remove();
      return out; })()""")
    assert cut, "the fixture has categories without a budget"
    for c in cut:
        assert c["row"][0] <= c["row"][1] + 1, (c, "the row overflows a one-column card")
        assert c["nameBox"][0] <= c["nameBox"][1] + 1, (c, "the category NAME is cut — never acceptable")
        if c["amt"]:
            assert c["amt"][0] <= c["amt"][1] + 1, (c, "money is cut")
    # and the suggestion itself, where it lives now, is whole
    panel = browser.js("""(() => { const t = document.querySelector('#page .nb .help-trigger');
      if (!t) return null; t.click();
      return new Promise((r) => setTimeout(() => {
        const p = document.querySelector('.help-panel'); const out = p ? p.innerText : null;
        if (p) p.remove(); r(out); }, 250)); })()""")
    if panel is not None:
        # the panel states the figure; it does not use the word "Suggested" (the row's whole point is that the card
        # has no budget, so it is phrased as what the app WOULD set)
        assert re.search(r"\$[\d,]+\.?\d*", panel), panel
        assert "budget" in panel.lower(), panel
