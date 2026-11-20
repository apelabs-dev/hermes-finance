"""Card t_0bff711d: a transaction row is reachable and openable from the keyboard, and its details close on navigation.

Before this card a row in #page table.tx was mouse-only (no tabindex, the click delegated in txmeta.js), the docked
details panel did not return focus to the row on Escape, and below 1400px the modal tx sheet survived a page change.
This pins, at 1440 (docked panel) and 390 (modal sheet):
  * Tab reaches a row; Enter or Space opens its details, focus moves inside, Escape puts focus back on that row;
  * Space does not scroll the page (no scroll-through);
  * a sidebar / plain hash / history.back() navigation closes the details either way;
  * a filter change that rebuilds the table closes them too, with no error and no focus left in a hidden sheet;
  * a nested control (the category chip) keeps its own Enter action;
  * a focused row keeps its siblings' height;
  * an edit saved by Escape leaves focus on that same row once the save's redraw has replaced it (1440 and 390);
  * a reload while the panel is open (the filter reload bills.js after() makes) returns Escape focus to the new row;
  * a real mouse click on another row, and the panel's ✕, never move the scroll position.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

import pytest

import factory as f
from cdp import Chrome, Server, find_chrome
from hermes_finance import db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()

VK = {"Tab": 9, "Enter": 13, "Escape": 27, " ": 32}
MEASURE = r"""(() => {
  const rows = [...document.querySelectorAll('#page table.tx tbody tr[data-tid]')];
  const h = (e) => Math.round(e.getBoundingClientRect().height);
  return rows.map((r) => ({tid: r.dataset.tid, h: h(r)}));
})()"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("txkeyboard") / "finance.db"
    conn = db.connect(path)
    try:
        f.bank(conn, "item_a", "Chase")
        f.acct(conn, "acc_chk", current=1000.0)
        for i in range(1, 31):   # enough rows that the page scrolls at 390, so a Space scroll-through would show
            f.tx(conn, (TODAY - timedelta(days=i % 5)).isoformat(), 10 + i * 1.25,
                 f"Keyboard Market {i:02d}", cat="Other Shopping", tid=f"kb_{i:02d}")
    finally:
        conn.close()
    srv = Server(path, today=TODAY)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def key(b, name, wait=True):
    code = "Space" if name == " " else name
    text = {"Enter": "\r", " ": " "}.get(name)
    b.call("Input.dispatchKeyEvent", type="keyDown", key=name, code=code, windowsVirtualKeyCode=VK[name],
           **({"text": text} if text else {}))
    b.call("Input.dispatchKeyEvent", type="keyUp", key=name, code=code, windowsVirtualKeyCode=VK[name])
    if name == "Escape":   # headless Chrome hides the tab on a real Escape (no more rAF for the next page)
        b.call("Page.bringToFront")
    if wait:
        b.settled()
    else:
        b.settle()


def go(b, site, width):
    b.size(width, 900 if width > 500 else 844)
    b.goto(f"{site.url}/?t={time.time():.3f}#/transactions")   # a fresh document, never the last test's state
    b.settled()
    assert b.js("!!document.querySelector('#page table.tx tbody tr[data-tid]')")


def tab_to_row(b):
    """Tab from the search box (a real control ahead of the table) until a transaction row has focus."""
    b.js("document.querySelector('#page .fbar .search').focus()")
    for _ in range(40):
        key(b, "Tab", wait=False)
        tid = b.js("(() => { const a = document.activeElement; "
                   "return a && a.matches && a.matches('#page table.tx tbody tr[data-tid]') ? a.dataset.tid : null; })()")
        if tid:
            return tid
    raise AssertionError("Tab never reached a transaction row")


def state(b):
    return {"modal": b.js("document.querySelector('#modal').classList.contains('on')"),
            "panel": b.js("!document.querySelector('#txpanel').hidden"),
            "txsheet": b.js("document.querySelector('#sheet').classList.contains('txsheet')")}


def assert_open(b, width):
    s = state(b)
    if width >= 1400:
        assert s["panel"] and not s["modal"], s
        root = "#txpanel"
    else:
        assert s["modal"] and s["txsheet"] and not s["panel"], s
        root = "#sheet"
    assert b.js(f"!!document.activeElement.closest({root!r})"), f"focus did not move into {root}"


def assert_closed(b):
    s = state(b)
    assert not s["modal"] and not s["panel"], s


def open_first_row(b):
    b.js("document.querySelector('#page table.tx tbody tr[data-tid]').focus()")
    key(b, "Enter")


def first_row_tid(b):
    return b.js("document.querySelector('#page table.tx tbody tr[data-tid]').dataset.tid")


def mouse_click(b, at):
    """A real pointer click at {x, y}, then wait for the page to settle."""
    for kind in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=kind, x=at["x"], y=at["y"], button="left", clickCount=1)
    b.settled()


def row_point(b, tid):
    """A plain-click point in a row's merchant cell, checked to be what a pointer there hits (never a nested control:
    the category picker is a button and Enter/click on it keeps its own action)."""
    return b.js(f"""(() => {{
      const row = document.querySelector('#page tr[data-tid={json.dumps(tid)}]');
      if (!row || !row.getClientRects().length) throw new Error('no row on screen: ' + {json.dumps(tid)});
      const cell = row.querySelector('td.c-m'), r = cell.getBoundingClientRect();
      const x = r.left + Math.min(r.width / 2, 160), y = r.top + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      if (!hit || !row.contains(hit) || hit.closest('select, button, a, input'))
        throw new Error('not a plain click on ' + {json.dumps(tid)} + ': ' + (hit && (hit.id || hit.className || hit.tagName)));
      return {{x, y}};
    }})()""")


def click_row(b, tid):
    mouse_click(b, row_point(b, tid))


def element_point(b, selector):
    return b.js(f"""(() => {{
      const el = document.querySelector({json.dumps(selector)});
      if (!el || !el.getClientRects().length) throw new Error('nothing to click: ' + {json.dumps(selector)});
      const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      if (!hit || !(hit === el || el.contains(hit))) throw new Error('covered: ' + {json.dumps(selector)});
      return {{x, y}};
    }})()""")


def visible_row_tid(b, skip):
    """The first transaction row fully on screen at the current scroll position and not hidden by the docked panel."""
    return b.js(f"""(() => {{
      for (const row of document.querySelectorAll('#page table.tx tbody tr[data-tid]')) {{
        if (row.dataset.tid === {json.dumps(skip)}) continue;
        const r = row.getBoundingClientRect();
        if (r.top < 40 || r.bottom > innerHeight - 4) continue;
        return row.dataset.tid;
      }}
      return null;
    }})()""")


@pytest.mark.parametrize("width", [1440, 390])
def test_tab_enter_opens_and_escape_returns_focus_to_the_row(site, chrome, width):
    go(chrome, site, width)
    tid = tab_to_row(chrome)
    key(chrome, "Enter")
    assert_open(chrome, width)
    key(chrome, "Escape")
    assert_closed(chrome)
    back = chrome.js("(() => { const a = document.activeElement; "
                     "return a && a.dataset ? a.dataset.tid || null : null; })()")
    assert back == tid, f"Escape did not put focus back on {tid}: {back}"


@pytest.mark.parametrize("width", [1440, 390])
def test_space_opens_without_scrolling_the_page(site, chrome, width):
    go(chrome, site, width)
    tab_to_row(chrome)
    chrome.js("window.scrollTo(0, 240)")
    chrome.settled()
    before = chrome.js("window.scrollY")
    assert before > 0, "the fixture page cannot scroll here, so a scroll-through would not show"
    key(chrome, " ")
    assert_open(chrome, width)
    assert chrome.js("window.scrollY") == before, "Space scrolled the page under the sheet"


@pytest.mark.parametrize("width", [1440, 390])
def test_navigation_closes_the_details(site, chrome, width):
    go(chrome, site, width)
    # a plain hash change (setMonth, the browser's back/forward path) closes it
    open_first_row(chrome)
    assert_open(chrome, width)
    chrome.js("location.hash = '#/budgets'")
    chrome.settled()
    assert_closed(chrome)
    # a sidebar link goes through the same hashchange
    chrome.js("location.hash = '#/transactions'")
    chrome.settled()
    chrome.until("!!document.querySelector('#page table.tx tbody tr[data-tid]')")
    open_first_row(chrome)
    assert_open(chrome, width)
    chrome.js("document.querySelector('#side a[href=\"#/budgets\"]').click()")
    chrome.settled()
    assert_closed(chrome)
    # history.back() too
    chrome.js("history.back()")
    chrome.settled()
    chrome.until("!!document.querySelector('#page table.tx tbody tr[data-tid]')")
    open_first_row(chrome)
    assert_open(chrome, width)
    chrome.js("history.back()")
    chrome.settled()
    assert_closed(chrome)


@pytest.mark.parametrize("width", [1440, 390])
def test_a_filter_rebuild_closes_then_closing_again_is_safe(site, chrome, width):
    go(chrome, site, width)
    open_first_row(chrome)
    assert_open(chrome, width)
    chrome.js("""(() => { const i = document.querySelector('#page .fbar .search');
      i.value = 'no-such-merchant'; i.dispatchEvent(new Event('input', {bubbles: true})); })()""")
    chrome.settled()
    assert chrome.js("document.querySelectorAll('#page table.tx tbody tr[data-tid]').length") == 0
    assert_closed(chrome)
    key(chrome, "Escape")   # closing something already closed must not throw
    assert_closed(chrome)
    assert chrome.problems() == []
    assert not chrome.js("(() => { const a = document.activeElement; "
                         "return !!a && !!(a.closest('#txpanel') || a.closest('#modal')); })()"), \
        "focus was left inside a hidden sheet"


@pytest.mark.parametrize("width", [1440, 390])
def test_a_nested_control_keeps_its_own_enter_action(site, chrome, width):
    go(chrome, site, width)
    chrome.js("document.querySelector('#page table.tx tbody tr[data-tid] button.catb').focus()")
    key(chrome, "Enter")
    assert chrome.js("!!document.querySelector('.cpk')"), "the category picker did not open"
    s = state(chrome)
    assert not s["modal"] and not s["panel"], f"the transaction sheet opened from a nested control: {s}"
    key(chrome, "Escape")


@pytest.mark.parametrize("width", [1440, 390])
def test_a_focused_row_keeps_its_siblings_height(site, chrome, width):
    go(chrome, site, width)
    chrome.js("document.querySelector('#page table.tx tbody tr[data-tid]').focus()")
    assert chrome.js("document.activeElement === document.querySelector('#page table.tx tbody tr[data-tid]')")
    rows = chrome.js(MEASURE)
    assert len(rows) >= 10, rows
    hs = [r["h"] for r in rows]
    # 1px slack: the last row has no bottom hairline (51 vs 52)
    assert max(hs) - min(hs) <= 1, f"rows differ in height: {rows}"


NOTE = "Written from the keyboard, then saved by Escape."


@pytest.mark.parametrize("width", [1440, 390])
def test_edit_escape_keeps_focus_on_the_row_after_the_save_redraw(site, chrome, width):
    go(chrome, site, width)
    tid = tab_to_row(chrome)
    key(chrome, "Enter")
    assert_open(chrome, width)
    note_id = f"txnote-{tid}"
    before = chrome.js(f"(() => {{ const e = document.getElementById({json.dumps(note_id)}); e.focus(); "
                       f"e.setSelectionRange(e.value.length, e.value.length); return e.value; }})()")
    chrome.call("Input.insertText", text=NOTE)
    assert chrome.js(f"document.getElementById({json.dumps(note_id)}).value") == before + NOTE
    # the element the close will focus: after the save's redraw it is gone, so the focus return has to find the new row
    assert chrome.js(f"(() => {{ window.__kbRow = document.querySelector('#page tr[data-tid={json.dumps(tid)}]'); "
                     f"return !!window.__kbRow; }})()")
    key(chrome, "Escape")
    chrome.until(f"""(() => {{ const a = document.activeElement;
      return !!(a && a.dataset && a.dataset.tid === {json.dumps(tid)} && a.isConnected && a !== window.__kbRow); }})()""")
    assert_closed(chrome)
    back = chrome.js("(() => { const a = document.activeElement; return a && a.dataset ? a.dataset.tid || null : null; })()")
    assert back == tid, f"focus after the save redraw was {back!r}, not {tid}"
    assert chrome.js(f"document.activeElement === document.querySelector('#page tr[data-tid={json.dumps(tid)}]')")
    # the redraw happened because the note was saved: re-open the row and read it back through the sheet
    key(chrome, "Enter")
    assert_open(chrome, width)
    assert chrome.js(f"document.getElementById({json.dumps(note_id)}).value") == before + NOTE, "the note was not saved"
    key(chrome, "Escape")
    assert_closed(chrome)


def test_a_mouse_click_on_another_row_keeps_the_scroll_position(site, chrome):
    go(chrome, site, 1440)
    first = first_row_tid(chrome)
    click_row(chrome, first)
    assert_open(chrome, 1440)
    chrome.js("window.scrollTo(0, document.documentElement.scrollHeight)")
    chrome.settled()
    before = chrome.js("window.scrollY")
    assert before > 0, "the page cannot scroll here"
    second = visible_row_tid(chrome, first)
    assert second and second != first, f"no other row on screen at scrollY {before}"
    click_row(chrome, second)
    assert chrome.js("window.scrollY") == before, "clicking another row moved the page"
    assert_open(chrome, 1440)
    assert chrome.js("document.querySelector('#page tr.sel').dataset.tid") == second


def test_the_panel_close_button_keeps_the_scroll_position(site, chrome):
    go(chrome, site, 1440)
    chrome.js("window.scrollTo(0, document.documentElement.scrollHeight)")
    chrome.settled()
    tid = visible_row_tid(chrome, "")
    assert tid
    click_row(chrome, tid)
    assert_open(chrome, 1440)
    chrome.js("window.scrollTo(0, 0)")
    chrome.settled()
    assert chrome.js("window.scrollY") == 0
    assert chrome.js(f"""(() => {{ const r = document.querySelector('#page tr[data-tid={json.dumps(tid)}]').getBoundingClientRect();
      return r.top > innerHeight; }})()"""), "the open row is still on screen, so a focus return would not scroll"
    at = element_point(chrome, '#txpanel .shd button[aria-label="Close"]')
    mouse_click(chrome, at)
    assert_closed(chrome)
    assert chrome.js("window.scrollY") == 0, "closing the panel moved the page"


def test_a_reload_while_the_panel_is_open_returns_focus_to_the_row(site, chrome):
    go(chrome, site, 1440)
    tid = "kb_05"
    click_row(chrome, tid)
    assert_open(chrome, 1440)
    chrome.js(f"window.__kbRow = document.querySelector('#page tr[data-tid={json.dumps(tid)}]')")
    # the same call bills.js after() makes while the panel is open: the list's own filter reload keeps the row,
    # so the panel stays open and only the row element is replaced
    chrome.js("""(() => { const i = document.querySelector('#page .fbar .search');
      i.value = 'Keyboard Market 05'; i.dispatchEvent(new Event('input', {bubbles: true})); })()""")
    chrome.until("document.querySelectorAll('#page table.tx tbody tr[data-tid]').length === 1")
    chrome.settled()
    assert chrome.js("!document.querySelector('#txpanel').hidden"), "the panel closed on the reload"
    assert chrome.js(f"window.__kbRow !== document.querySelector('#page tr[data-tid={json.dumps(tid)}]')"), \
        "the reload did not replace the row: the test would prove nothing"
    key(chrome, "Escape")
    assert_closed(chrome)
    back = chrome.js("(() => { const a = document.activeElement; return a && a.dataset ? a.dataset.tid || null : null; })()")
    assert back == tid, f"focus after the reload was {back!r}, not {tid}"
    assert chrome.js(f"document.activeElement === document.querySelector('#page tr[data-tid={json.dumps(tid)}]')")
