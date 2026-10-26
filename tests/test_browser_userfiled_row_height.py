"""A transaction whose category the owner filed (button.catb.done) is a row like any other.

The bug (t_d67b1986, 2026-10-26): settings.css styled the import's finish panel with a bare `.done` class (flex, 18px
margins, grey spans). The category chip of a user-filed row is `button.catb.done`, so it picked the margins up: its
.catc box grew from 33px to 69px and the row from 52px to 88px (144px on a phone), and the chip's label turned grey
instead of the "done" green. This pins: every row on the page is one height, at 1440 and 390, dark and light, and the
user-filed chip carries no margin.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import factory as f
from cdp import Chrome, Server, find_chrome
from hermes_finance import classify, db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()

MEASURE = r"""(() => {
  const rows = [...document.querySelectorAll('#page table.tx tbody tr[data-tid]')];
  const h = (e) => Math.round(e.getBoundingClientRect().height);
  return rows.map((r) => {
    const b = r.querySelector('button.catb');
    const s = b && getComputedStyle(b);
    return {tid: r.dataset.tid, h: h(r), done: !!(b && b.classList.contains('done')),
            margin: s && [s.marginTop, s.marginBottom], catc: h(r.querySelector('.catc') || r)};
  });
})()"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("userfiled") / "finance.db"
    conn = db.connect(path)
    try:
        f.bank(conn, "item_a", "Chase")
        f.acct(conn, "acc_chk", current=1000.0)
        day = (TODAY - timedelta(days=1)).isoformat()
        f.tx(conn, day, 12.40, "Plain Merchant", cat="Other Shopping", tid="uf_plain")
        tid = f.tx(conn, day, 1.34, "Filed Merchant", cat="Other Shopping", tid="uf_filed")
        classify.set_category(conn, tid, "Other Shopping", source="user")
        f.tx(conn, day, 7.10, "Another Merchant", cat="Other Shopping", tid="uf_plain2")
    finally:
        conn.close()
    srv = Server(path, today=TODAY)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


@pytest.mark.parametrize("width,theme", [(1440, "dark"), (1440, "light"), (390, "dark"), (390, "light")])
def test_a_user_filed_row_is_as_tall_as_the_others(site, chrome, width, theme):
    chrome.size(width, 900 if width > 500 else 844)
    chrome.goto(f"{site.url}/#/transactions")
    chrome.js(f"document.documentElement.dataset.theme = '{theme}'")
    chrome.settled()
    rows = chrome.js(MEASURE)
    filed = [r for r in rows if r["done"]]
    assert filed, f"the user-filed row lost its done chip: {rows}"
    assert all(m == "0px" for r in filed for m in r["margin"]), f"the done chip has margins: {filed}"
    hs = [r["h"] for r in rows]
    # 1px slack: the last row has no bottom hairline (51 vs 52); the bug added 36px (88 vs 52)
    assert max(hs) - min(hs) <= 1, f"rows differ in height: {rows}"
