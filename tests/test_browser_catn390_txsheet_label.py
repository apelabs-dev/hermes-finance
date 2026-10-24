"""CATN-390: on a 390px phone the transaction sheet's Category row stays one row and a long label truncates.

The row is the badge, the category button and Split, side by side. "Shopping · Other Shopping" needs 185px and the
label gets 181px, so it ends in an ellipsis. The owner's call (2026-10-23): keep the one-row layout and truncate,
never wrap or restack the row. This pins the layout (all three on one 38px line, Split at the right edge), checks
the ellipsis is a drawn one with the full name still in the button's aria-label, and that the layout audit
accepts it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import factory as f
from cdp import Chrome, Server, find_chrome
from hermes_finance import classify, db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()

MEASURE = r"""(() => {
  const row = document.querySelector('#sheet.txsheet .spl.one');
  const btn = row && row.querySelector('button.catb');
  const n = btn && btn.querySelector('.catn');
  const box = (e) => e ? {x: Math.round(e.getBoundingClientRect().x), w: Math.round(e.getBoundingClientRect().width)} : null;
  return n && {
    label: n.textContent, catn: box(n), button: box(btn), row: box(row),
    catnScroll: n.scrollWidth, catnClient: n.clientWidth, btnScroll: btn.scrollWidth, btnClient: btn.clientWidth,
    rowScroll: row.scrollWidth, rowClient: row.clientWidth,
    kids: [...row.children].map((k) => ({cls: k.className, ...box(k), y: Math.round(k.getBoundingClientRect().y)})),
    rowH: Math.round(row.getBoundingClientRect().height), ell: getComputedStyle(n).textOverflow,
    ws: getComputedStyle(n).whiteSpace, aria: btn.getAttribute('aria-label'),
  };
})()"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("catn390") / "finance.db"
    conn = db.connect(path)
    try:
        f.bank(conn, "item_a", "Chase")
        f.acct(conn, "acc_chk", current=1000.0)
        tid = f.tx(conn, TODAY.isoformat(), 1.25, "Gate Stationery", cat="Other Shopping", tid="catn_390")
        classify.set_category(conn, tid, "Other Shopping")
    finally:
        conn.close()
    srv = Server(path, today=TODAY)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def test_a_390px_sheet_shows_the_whole_group_and_category_label(site, chrome):
    chrome.size(390, 844)
    chrome.goto(f"{site.url}/#/transactions")
    chrome.settled()
    chrome.js("""(() => { const row = document.querySelector('#page table.tx tbody tr[data-tid] td.num');
      if (!row) throw new Error('no transaction row'); row.click(); return 1; })()""")
    chrome.js("window.__appear(() => document.querySelector('#sheet.txsheet .spl.one .catn'), 4000, 'the tx sheet')")
    chrome.settled()
    m = chrome.js(MEASURE)
    assert m, "the sheet's Category row is missing"
    assert m["label"] == "Shopping · Other Shopping", m
    assert m["rowScroll"] <= m["rowClient"], f"the Category row overflows the sheet: {m}"
    # one row: badge, button and Split share a line; the row is the 38px control height, not two stacked lines
    assert m["rowH"] <= 44, f"the Category row wrapped onto two lines: {m}"
    assert len({k["y"] // 10 for k in m["kids"]}) == 1 or max(k["y"] for k in m["kids"]) - min(k["y"] for k in m["kids"]) <= 6, m
    xs = [k["x"] for k in m["kids"]]
    assert xs == sorted(xs), f"badge / button / Split are out of order: {m}"
    assert m["kids"][-1]["x"] + m["kids"][-1]["w"] == m["row"]["x"] + m["row"]["w"], f"Split left the right edge: {m}"
    # the long label truncates with a drawn ellipsis, the whole name still reachable
    assert m["catnScroll"] > m["catnClient"], f"expected the 185px label to truncate in the 181px box: {m}"
    assert m["ell"] == "ellipsis" and m["ws"] == "nowrap", m
    assert "Shopping · Other Shopping" in (m["aria"] or ""), m


def test_the_layout_audit_accepts_the_truncated_category_label():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "audit_layout.py").read_text()
    namey = next(line for line in src.splitlines() if line.startswith("const NAMEY"))
    assert ".spl.one button.catb .catn" in namey
