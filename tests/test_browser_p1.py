"""polish P1 in headless Chrome (tests/cdp.py) against a throwaway loopback server on the gate's "features" DB
(tests/gate_dbs.py): every header of the Transactions table sorts it (aria-sort, day headers only in date order); the
Filters drawer (a dialog on the right; a sheet from the bottom on a phone) counts what the draft would show, applies
on "Show N payments", Escape closes it without applying; what's picked shows as chips under the bar that clear
themselves; on a phone the order (no headers there) is a chip too."""

from __future__ import annotations

import json
import time
from datetime import date
from urllib.parse import parse_qs

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()   # the page reads its month from the browser clock: the server uses the same day
WAIT = "new Promise((r) => setTimeout(r, {ms}))"
T = "var T = (el) => (el ? el.textContent.replace(/\\s+/g, ' ').trim() : null);"

STATE = T + """({
  hash: location.hash,
  sort: [...document.querySelectorAll('#page .tx thead th')].map((th) => th.getAttribute('aria-sort')),
  days: document.querySelectorAll('#page .tx tr.day').length,
  first: document.querySelector('#page .tx tbody tr') ? document.querySelector('#page .tx tbody tr').className : null,
  amounts: [...document.querySelectorAll('#page .tx tr[data-tid] td.num')].map((td) =>
    (T(td).startsWith('+') ? -1 : 1) * Number(T(td).replace(/[^0-9.]/g, ''))),
  chips: [...document.querySelectorAll('#page .txchips > *')].filter((x) => x.getClientRects().length).map(T),
  badge: T(document.querySelector('#page .tfbtn .tfn:not([hidden])')),
  drawer: Boolean(document.querySelector('.tfd')),
})"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("browserp1") / "finance.db"
    gate_dbs.build("features", path, TODAY)
    srv = Server(path, today=TODAY)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def go(b, site, page, width=1280):
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{site.url}/?t={time.time():.3f}#/{page}")
    b.settle()
    b.js(WAIT.format(ms=250))


def act(b, js, ms=450):
    b.js(f"(() => {{ {js}; return 1; }})()")
    b.js(WAIT.format(ms=ms))
    return b.js(STATE)


def total(b, **params) -> int:
    qs = "&".join(f"{k}={v}" for k, v in {"month": TODAY.isoformat()[:7], "limit": 1, **params}.items())
    return b.js(f"fetch('/api/transactions?{qs}').then((r) => r.json()).then((d) => d.total)")


def params(hash_: str) -> dict:
    return parse_qs(hash_.partition("?")[2])


def test_every_header_sorts_and_days_show_only_in_date_order(site, chrome):
    go(chrome, site, "transactions")
    s = chrome.js(STATE)
    assert s["sort"] == ["descending", "none", "none", "none", "none"]
    assert s["days"] > 0 and s["first"] == "day"
    header = "[...document.querySelectorAll('#page .tx th .sortb')].find((x) => x.textContent.startsWith({}))"
    s = act(chrome, header.format(json.dumps("Amount")) + ".click()")
    assert params(s["hash"])["sort"] == ["amount_desc"] and s["sort"][-1] == "descending" and s["days"] == 0
    assert s["amounts"] == sorted(s["amounts"], reverse=True)
    s = act(chrome, header.format(json.dumps("Amount")) + ".click()")
    assert params(s["hash"])["sort"] == ["amount_asc"] and s["sort"][-1] == "ascending"
    assert s["amounts"] == sorted(s["amounts"])
    s = act(chrome, header.format(json.dumps("Merchant")) + ".click()")
    assert s["sort"] == ["none", "ascending", "none", "none", "none"] and s["days"] == 0
    s = act(chrome, header.format(json.dumps("Date")) + ".click()")
    assert "sort" not in params(s["hash"]) and s["sort"][0] == "descending" and s["days"] > 0
    assert chrome.problems() == []


def test_the_drawer_counts_the_draft_applies_it_and_the_chips_clear_it(site, chrome):
    go(chrome, site, "transactions")
    s = act(chrome, "document.querySelector('#page .tfbtn').click()", 500)
    assert s["drawer"]
    assert chrome.js("[document.querySelector('.tfd').getAttribute('role'), document.querySelector('.tfd').getAttribute('aria-modal'), "
                     "document.querySelector('#page .tfbtn').getAttribute('aria-expanded'), document.activeElement.className]") \
        == ["dialog", "true", "true", "tfq"]
    # type to filter the tree; pick one category: the button counts what it would show
    act(chrome, "const q = document.querySelector('.tfd .tfq'); q.value = 'groc'; q.dispatchEvent(new Event('input', {bubbles: true}))", 100)
    shown = chrome.js(T + "[...document.querySelectorAll('.tfd .tftree .tfr .tft')].map(T)")
    assert "Groceries" in shown and "Food" in shown and "Shopping" not in shown
    act(chrome, "[...document.querySelectorAll('.tfd .tftree .tfr')].find((r) => r.textContent.includes('Groceries') "
                "&& !r.classList.contains('tfgr')).querySelector('.tfc').click()", 600)
    n = total(chrome, category="Groceries")
    assert n > 0
    assert chrome.js(T + "T(document.querySelector('.tfd .tfft .btn.w'))") == f"Show {n} payment{'s' if n != 1 else ''}"
    # Escape: nothing applied, focus back on the button
    s = act(chrome, "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))", 300)
    assert not s["drawer"] and "category" not in params(s["hash"]) and s["chips"] == []
    assert chrome.js("document.activeElement.classList.contains('tfbtn')")
    # the whole Food group and a tag, applied
    act(chrome, "document.querySelector('#page .tfbtn').click()", 500)
    act(chrome, "[...document.querySelectorAll('.tfd .tfg')].find((g) => g.textContent.includes('Food')).querySelector('.tfc').click()", 100)
    act(chrome, "document.querySelector('.tfd .tfdd').click()", 200)
    act(chrome, "[...document.querySelectorAll('.tfd .tftl .tfr')].find((r) => r.textContent.includes('#trip')).querySelector('.tfc').click()",
        600)
    s = act(chrome, "document.querySelector('.tfd .tfft .btn.w').click()", 700)
    p = params(s["hash"])
    assert p["group"] == ["Food"] and p["tag"] == ["trip"] and not s["drawer"] and s["badge"] == "2"
    n = total(chrome, group="Food", tag="trip")
    assert s["chips"][:2] == ["Food · all", "#trip"] and s["chips"][2].startswith(f"{n} payment") and s["chips"][-1] == "Clear all"
    assert len(s["amounts"]) == n
    # a chip clears itself; Clear all the rest
    s = act(chrome, "document.querySelector('#page .txchips .tfchip[aria-label=\"Remove tag trip\"]').click()", 600)
    assert "tag" not in params(s["hash"]) and s["chips"][0] == "Food · all" and s["badge"] == "1"
    assert chrome.js("document.activeElement.classList.contains('tfbtn')")   # not lost with the chip
    s = act(chrome, "document.querySelector('#page .txchips .tfclr').click()", 600)
    assert "group" not in params(s["hash"]) and s["chips"] == [] and s["badge"] is None
    assert chrome.problems() == []


def test_on_a_phone_the_drawer_is_a_sheet_and_the_order_is_a_chip(site, chrome):
    go(chrome, site, "transactions", 390)
    act(chrome, "document.querySelector('#page .tfbtn').click()", 500)
    chrome.until("(() => { const d = document.querySelector('.tfd'); return d && Math.round(d.getBoundingClientRect().bottom) === "
                 "innerHeight && !d.getAnimations({subtree: true}).some((a) => a.playState === 'running'); })()")   # slid in
    box = chrome.js("(() => { const r = document.querySelector('.tfd').getBoundingClientRect(); "
                    "return [r.left, Math.round(r.bottom) === innerHeight, r.width === innerWidth, "
                    "Boolean(document.querySelector('.tfd .tfsorts').getClientRects().length), "
                    "getComputedStyle(document.querySelector('.tfd .tfq')).fontSize]; })()")
    assert box == [0, True, True, True, "16px"]
    act(chrome, "const s = document.querySelector('.tfd .tfsort'); s.value = 'amount_desc'; s.dispatchEvent(new Event('change'))", 50)
    s = act(chrome, "document.querySelector('.tfd .tfft .btn.w').click()", 700)
    assert params(s["hash"])["sort"] == ["amount_desc"] and s["chips"] == ["Biggest amount"] and s["days"] == 0
    s = act(chrome, "document.querySelector('#page .txchips .tfsortc').click()", 600)
    assert "sort" not in params(s["hash"]) and s["chips"] == [] and s["days"] > 0
    assert chrome.js("document.documentElement.scrollWidth <= innerWidth + 1")
    assert chrome.problems() == []


def test_uncategorized_rows_show_their_category_label_in_amber(site, chrome):
    """A row nobody has filed yet (Other, from Plaid's map or no category): its label is amber, on desktop and phone."""
    read = """(() => {
      const rows = [...document.querySelectorAll('#page .tx tr[data-tid]')];
      const label = (b) => getComputedStyle(b.querySelector('.catn')).color;
      return {
        uncat: rows.map((tr) => tr.querySelector('button.catb.uncat')).filter(Boolean).map((b) => [b.value, label(b)]),
        amber: rows.filter((tr) => tr.querySelector('button.catb.uncat')).map((tr) => tr.dataset.tid).sort(),
        other: rows.map((tr) => tr.querySelector('button.catb:not(.uncat)')).filter(Boolean).map(label),
      };
    })()"""
    for width in (1280, 390):
        go(chrome, site, "transactions", width)
        s = chrome.js(read)
        assert s["uncat"] and all(c == "rgb(245, 180, 84)" for _, c in s["uncat"]), (width, s["uncat"])
        # amber marks exactly the rows the Uncategorized filter finds (reviewer: one rule, the server's)
        want = chrome.js("fetch('/api/transactions?uncategorized=1').then((r) => r.json())"
                         ".then((d) => d.rows.map((r) => r.transaction_id).sort())")
        shown = set(chrome.js("[...document.querySelectorAll('#page .tx tr[data-tid]')].map((tr) => tr.dataset.tid)"))
        assert s["amber"] == sorted(t for t in want if t in shown), (width, s["amber"], want)
        assert "rgb(245, 180, 84)" not in s["other"]
        assert chrome.problems() == []
