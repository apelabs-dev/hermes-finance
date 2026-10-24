"""The two overrides on the Budgets page's "No budget yet" rows (budgets.css, OVERRIDE 1 and 2; docs/css-load-order.md),
in a browser, on rows 254px wide: the width the rows were measured at when the overrides were written.

  - The name stays on one line, so rows with the same reason are the same height. app.css's
    `.li.wrap .grow > b { white-space: normal }` matches these rows. Without `.li.wrap.nbrow > .grow > b`, "Doctor & Dentist"
    wraps under its "New" badge and its row grows taller than "Fees"'s.
  - The reason ("$124 spent in September") is never cut. The row's clamp `.nb .li > .grow > span` would keep it on one line
    and put an ellipsis through the figure. `.nb .li.wrap.nbrow > .grow > span` lets it wrap instead.

Two layouts give 254px rows: the desktop one (1440) with the card narrowed to that width, and a 320px phone, where the rows
are that wide on their own.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cdp import Chrome, Server, find_chrome
from factory import basic, cap, tx
from hermes_finance import classify, db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="needs a local Chrome")

TODAY = date(2026, 9, 25)
MONTH = "2026-09"
# short and long names, all new this month (no budget, no history): each row reads "$X spent in September", and the
# short ones ("Pets", "Gym") fit on one line however the CSS goes, so a long one that wraps makes its row taller than theirs
CATEGORIES = ("Pets", "Doctor & Dentist", "Gifts & Charity", "Gym", "Personal Care", "Home Repair")
ROW = 254

NARROW = f"""(() => {{ const c = document.querySelector('#page .card.nb'), r = c.querySelector('.nbrow');
  const pad = c.getBoundingClientRect().width - r.getBoundingClientRect().width;
  c.style.width = ({ROW} + pad) + 'px'; c.style.maxWidth = 'none'; return 1; }})()"""
# Per category row: its width and height, its shape (how many lines its reason takes and what else it holds: the same
# shape should mean the same height), how many lines the name's own text takes, and whether the reason shows all of its
# text (nothing past its box, no line past its right edge).
ROWS = r"""(() => [...document.querySelectorAll('#page .nb .nbrow:not(.nbgrp)')].map((r) => {
  const g = r.querySelector(':scope > .grow'), b = g.querySelector(':scope > b'), why = g.querySelector(':scope > span[title]');
  const lines = (node) => { const rg = document.createRange(); rg.selectNodeContents(node);
    return new Set([...rg.getClientRects()].filter((x) => x.width > 0).map((x) => Math.round(x.top))).size; };
  const text = [...b.childNodes].find((n) => n.nodeType === 3);
  const rg = document.createRange(); rg.selectNodeContents(why);
  const right = why.getBoundingClientRect().right;
  const box = r.getBoundingClientRect();
  const shape = [lines(why), ...[...r.querySelectorAll('button, .newc')].map((e) => e.className)].join(' ');
  return {name: text.textContent, why: why.textContent, width: Math.round(box.width), height: Math.round(box.height), shape,
          nameLines: lines(text), whyLines: lines(why),
          whyCut: why.scrollWidth > why.clientWidth + 1 || [...rg.getClientRects()].some((x) => x.right > right + 1)};
}))()"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("budget-rows") / "finance.db"
    conn = db.connect(path)
    basic(conn)
    cap(conn, "Groceries", 400)   # a capped category, so the page draws its table and the "No budget yet" card
    tx(conn, f"{MONTH}-03", 80.0, "Market", cat="Groceries")
    for i, name in enumerate(CATEGORIES):
        tid = tx(conn, f"{MONTH}-{10 + i:02d}", 120.0 + i, f"Shop {i}", cat="Other Shopping")
        classify.set_category(conn, tid, name)
    conn.commit()
    conn.close()
    srv = Server(path, today=TODAY)
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def rows(b: Chrome, url: str, width: int, narrow: bool) -> list[dict]:
    b.size(width, 900)
    b.goto("about:blank", ready=False)   # a fresh page: nothing left over from the last layout
    b.goto(f"{url}/#/budgets?month={MONTH}")
    b.settled()
    if narrow:
        b.js(NARROW)
        b.settled()
    out = [r for r in b.js(ROWS) if r["name"] in CATEGORIES]   # (the quiet rows, folded away, are 0px wide)
    assert sorted(r["name"] for r in out) == sorted(CATEGORIES), out
    assert all(r["width"] == ROW for r in out), [(r["name"], r["width"]) for r in out]
    assert all(r["why"].startswith(f"${120 + CATEGORIES.index(r['name'])} spent in September") for r in out), [r["why"] for r in out]
    return out


LAYOUTS = pytest.mark.parametrize("width, narrow", [(1440, True), (320, False)], ids=["1440-card-254", "320"])


@LAYOUTS
def test_name_stays_on_one_line(site, chrome, width, narrow):
    got = rows(chrome, site.url, width, narrow)
    wrapped = [r["name"] for r in got if r["nameLines"] != 1]
    shapes: dict[str, set[int]] = {}
    for r in got:
        shapes.setdefault(r["shape"], set()).add(r["height"])
    uneven = {k: sorted(h) for k, h in shapes.items() if len(h) > 1}
    assert not wrapped and not uneven, (
        f"at {ROW}px rows the name wrapped onto a second line: {wrapped}; rows of the same shape at different heights: "
        f"{uneven} ({[(r['name'], r['height']) for r in got]})")
    assert any(len([r for r in got if r["shape"] == k]) > 2 for k in shapes), "the rows to compare have the same shape"


@LAYOUTS
def test_reason_is_never_cut(site, chrome, width, narrow):
    got = rows(chrome, site.url, width, narrow)
    cut = [r["why"] for r in got if r["whyCut"]]
    assert not cut, f"the reason (it carries the amount) was cut at {ROW}px rows: {cut}"
