"""TASK-21 in headless Chrome (tests/cdp.py; https and plaid.com blocked) on the gate's "features" DB (tests/gate_dbs.py
add_bills: a power bill whose amount changes, LADWP tracked by hand every 2 months with usually $100–150, a pending agent
change): Recurring rows show "~$ est." and the usual range, a big charge its "higher than usual" tag; the calendar's
every-2-months chip (with its logo) and a phone's agenda row; Track as a bill from a transaction (docked at 1440, the
phone's sheet with Back at 390) saves the row the server shows; Edit schedule and Stop tracking from a bill's sheet;
Settings' Approve applies a pending agent change. Every page stays free of JS errors and sideways scroll."""

from __future__ import annotations

import functools
import json
import sqlite3
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import api, db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()   # the page reads its month from the browser clock: the server uses the same day
LADWP = gate_dbs.LADWP
SCE = "Southern California Edison"

T = "var T = (el) => (el ? el.textContent.replace(/\\s+/g, ' ').trim() : null);"


def wait(ms: int) -> str:
    return f"new Promise((r) => setTimeout(r, {ms}))"


def until(sel: str, ms: int = 5000) -> str:
    return (f"new Promise((res, rej) => {{ const t0 = Date.now(); const tick = () => {{ const e = [...document.querySelectorAll("
            f"{json.dumps(sel)})].find((x) => x.getClientRects().length); if (e) res(true); else if (Date.now() - t0 > {ms}) "
            f"rej(new Error('never appeared: ' + {json.dumps(sel)})); else setTimeout(tick, 50); }}; tick(); }})")


def click(sel: str, text: str | None = None) -> str:
    return (f"(() => {{ const els = [...document.querySelectorAll({json.dumps(sel)})].filter((e) => e.getClientRects().length); "
            f"const el = els.find((e) => {json.dumps(text)} === null || e.textContent.includes({json.dumps(text)})); "
            f"if (!el) throw new Error('nothing to click: ' + {json.dumps(sel)} + ' ' + {json.dumps(text)}); el.click(); return 1; }})()")


def fill(sel: str, value: str, event: str = "input") -> str:
    return (f"(() => {{ const i = document.querySelector({json.dumps(sel)}); i.value = {json.dumps(value)}; "
            f"i.dispatchEvent(new Event({json.dumps(event)}, {{bubbles: true}})); return 1; }})()")


def serve(tmp_path):
    path = tmp_path / "finance.db"
    gate_dbs.build("features", path, TODAY)
    srv = Server(path, today=TODAY)
    srv.path = path
    return srv


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    srv = serve(tmp_path_factory.mktemp("browser21"))
    yield srv
    srv.close()


@pytest.fixture
def fresh(tmp_path):
    srv = serve(tmp_path)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def go(b, site, page, width=1440):
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{site.url}/?t={time.time():.3f}#/{page}")
    b.settle()
    b.js(wait(250))


def q(site, sql, args=()):
    conn = sqlite3.connect(f"file:{site.path}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def clean(b):
    assert b.problems() == []
    assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")


ROWS = T + """[...document.querySelectorAll('#page .li[data-bill]')].map((li) => ({name: li.querySelector('.grow > b').firstChild.textContent,
  sub: T(li.querySelector('.grow > span')), amt: T(li.querySelector('.amt')), tag: T(li.querySelector('.pend[data-outlier]')),
  est: li.hasAttribute('data-est')}))"""

# (baseline K8) demo_data's rent, Innago on the 3rd: $2,252, but $2,150 in January and July (its "rent change
# mid-history"). A fixed bill is expected at its last charge's amount, so Recurring reads $2,150 from Jan 3 to Feb 2
# and Jul 3 to Aug 2: "$2,252" held on the other days only. These days pin both amounts, either side of a change.
RENT = {date(2026, 9, 30): "$2,252", date(2027, 1, 2): "$2,252", date(2027, 1, 15): "$2,150",
        date(2027, 2, 2): "$2,150", date(2027, 2, 3): "$2,252", date(2027, 7, 3): "$2,150"}


def rent_on(day: date) -> str:
    """The rent Recurring shows on `day`, from demo_data's charges (worked out here, not read from the app)."""
    last = day if day.day >= 3 else day.replace(day=1) - timedelta(days=1)   # the month of the last charge
    return "$2,150" if last.month in (1, 7) else "$2,252"


@functools.cache
def api_rent_on(day: date) -> str:
    """Innago's amount_label from /api/recurring's own reader, on the features DB built for `day`."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(gate_dbs.build("features", Path(tmp) / "finance.db", day))
        try:
            return {r["merchant"]: r for r in api.recurring_page(conn, {}, day)["items"]}["Innago"]["amount_label"]
        finally:
            conn.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_recurring_rows_show_the_estimate_the_range_and_the_tag(site, chrome, width):
    go(chrome, site, "recurring", width)
    rows = {r["name"]: r for r in chrome.js(ROWS)}
    lad, sce, rent = rows[LADWP], rows[SCE], rows["Innago"]
    assert (lad["amt"], lad["tag"], lad["est"]) == ("~$125 est.", "higher than usual", True)
    assert "every 2 months" in lad["sub"] and "$62.50 a month" in lad["sub"] and "usually $100–150" in lad["sub"]
    assert (sce["amt"], sce["est"]) == ("~$5 est.", True) and "monthly" in sce["sub"] and "usually $4–40" in sce["sub"]
    # the rent: the API's label, which is what demo_data charged last; on the pinned days the API gives both amounts
    items = json.loads(chrome.js("fetch('/api/recurring').then((r) => r.text())"))["items"]
    label = next(r["amount_label"] for r in items if r["merchant"] == "Innago")
    assert label == rent_on(TODAY) and {d: api_rent_on(d) for d in RENT} == {d: rent_on(d) for d in RENT} == RENT
    assert (rent["amt"], rent["est"], rent["tag"]) == (label.removesuffix(".00"), False, None) and "usually" not in rent["sub"]
    clean(chrome)


@pytest.mark.parametrize("width", [1440, 390])
def test_the_calendar_shows_the_every_2_months_bill_with_its_logo(site, chrome, width):
    go(chrome, site, "recurring", width)
    nxt = (TODAY + timedelta(days=31))
    if nxt.strftime("%Y-%m") != TODAY.strftime("%Y-%m"):
        chrome.js(click("#page .calnav button[aria-label='Next month']"))
        chrome.js(wait(500))
    sel = ".bchip[data-cadence=bimonthly]" if width >= 1200 else ".agi[data-cadence=bimonthly]"
    chrome.js(until(f"#page {sel}"))
    got = chrome.js(T + f"""(() => {{ const c = [...document.querySelectorAll('#page {sel}')].find((x) => x.getClientRects().length);
      return {{text: T(c), title: c.getAttribute('title'), avatar: Boolean(c.querySelector('.av, img'))}}; }})()""")
    assert LADWP in got["text"] and "~$125" in got["text"] and got["avatar"]
    if width >= 1200:
        assert "~$125 est." in got["title"] and "every 2 months" in got["title"]
    else:
        assert "every 2 months" in got["text"] and "~$125 est." in got["text"]
    clean(chrome)


def _open_tx(b, site, merchant, width, month=None):
    go(b, site, f"transactions?q={merchant.split()[0]}" + (f"&month={month}" if month else ""), width)
    b.js(until("#page table.tx tbody tr[data-tid]"))
    b.js(click("#page table.tx tbody tr[data-tid] td.num"))
    root = "#txpanel:not([hidden])" if width >= 1400 else "#sheet.txsheet"
    b.js(until(f"{root} .billsec .btn"))
    return root


def test_the_big_ladwp_charge_is_tagged_on_its_row_and_in_its_sheet(site, chrome):
    month = (TODAY - timedelta(days=29)).strftime("%Y-%m")
    root = _open_tx(chrome, site, LADWP, 1440, month)
    tag = chrome.js(T + "T(document.querySelector('#page table.tx tbody tr[data-tid] .pend[data-outlier]'))")
    sec = chrome.js(T + f"T(document.querySelector('{root} .billsec'))")
    assert tag == "higher than usual"
    assert "Every 2 months" in sec and "higher than usual" in sec and "Tracked by hand" in sec and "~$125 est." in sec
    assert "Edit schedule" in sec and "Stop tracking" in sec
    clean(chrome)


@pytest.mark.parametrize("width", [1440, 390])
def test_track_as_a_bill_from_a_transaction(fresh, chrome, width):
    root = _open_tx(chrome, fresh, "Amazon", width)
    assert chrome.js(T + f"T(document.querySelector('{root} .billsec .btn'))") == "Track as a bill"
    chrome.js(click(f"{root} .billsec .btn"))
    chrome.js(until("#sheet.billsheet .trackf"))
    if width < 1400:                                              # the phone: Back to the transaction, then again
        chrome.js(click("#sheet .shd .ib[aria-label=Back]"))
        chrome.js(until("#sheet.txsheet .billsec .btn"))
        chrome.js(click("#sheet.txsheet .billsec .btn"))
        chrome.js(until("#sheet.billsheet .trackf"))
    sheet = chrome.js(T + """({title: T(document.querySelector('#sheet h2')), sub: T(document.querySelector('#sheet .shd + p')),
      every: document.querySelector('#bt-every').value, pressed: T(document.querySelector('#sheet .bseg [aria-pressed=true]')),
      usual: !document.querySelector('#sheet [data-f=usually]').hidden})""")
    assert sheet["title"] == "Track as a bill" and sheet["sub"].startswith("Amazon · Starting from this charge: $")
    assert (sheet["every"], sheet["pressed"], sheet["usual"]) == ("1m", "Same each time", False)  # every month, not a utility
    chrome.js(fill("#bt-every", "2m", "change"))
    chrome.js(click("#sheet .bseg button", "Changes each time"))
    chrome.js(fill("#bt-usual", "100-150"))
    chrome.js(wait(700))
    preview = chrome.js(T + "T(document.querySelector('#sheet .bprev'))")
    assert preview.startswith("Next ~") and "est." in preview and "usually $100–150" in preview
    assert q(fresh, "SELECT COUNT(*) FROM tracked_bills WHERE merchant_key = 'amazon'") == [(0,)]   # nothing saved yet
    chrome.js(click("#sheet .row .btn", "Track as a bill"))
    chrome.js(until("#toast.on"))
    assert "Tracking Amazon as a bill: every 2 months" in chrome.js(T + "T(document.querySelector('#toast'))")
    assert q(fresh, "SELECT cadence, variable, usual_low, usual_high, stopped_at FROM tracked_bills WHERE merchant_key = 'amazon'") == \
        [("bimonthly", 1, 100.0, 150.0, None)]
    if width >= 1400:                                             # the docked sheet shows it straight away
        chrome.js(until(f"{root} .billsec .bline"))
        assert "Every 2 months" in chrome.js(T + f"T(document.querySelector('{root} .billsec'))")
    clean(chrome)


def test_edit_schedule_and_stop_tracking_from_a_bills_sheet(fresh, chrome):
    go(chrome, fresh, "recurring")
    chrome.js(click("#page .li[data-bill]", SCE))
    chrome.js(until("#sheet.billsheet .tsec"))
    sheet = chrome.js(T + "T(document.querySelector('#sheet'))")
    assert "~$5 est." in sheet and "Usually $4–40" in sheet and "Found from 7 charges" in sheet and "from Nova Checking" in sheet
    chrome.js(click("#sheet .row .btn", "Edit schedule"))
    chrome.js(until("#sheet .trackf"))
    assert chrome.js("document.querySelector('#sheet h2').textContent") == "Edit schedule"
    assert chrome.js(T + "T(document.querySelector('#sheet .bseg [aria-pressed=true]'))") == "Changes each time"
    chrome.js(fill("#bt-every", "2m", "change"))
    chrome.js(wait(400))
    chrome.js(click("#sheet .row .btn", "Save"))
    chrome.js(until("#toast.on"))
    assert q(fresh, "SELECT cadence, variable, stopped_at FROM tracked_bills WHERE merchant_key = 'southern california edison'") == \
        [("bimonthly", 1, None)]
    chrome.js(wait(600))
    rows = {r["name"]: r for r in chrome.js(ROWS)}
    assert "every 2 months" in rows[SCE]["sub"]
    chrome.js(click("#page .li[data-bill]", SCE))
    chrome.js(until("#sheet.billsheet .tsec"))
    chrome.js(click("#sheet .row .btn", "Stop tracking"))
    chrome.js(until("#sheet .row .btn"))
    assert chrome.js("document.querySelector('#sheet h2').textContent") == f"Stop tracking {SCE}?"
    chrome.js(click("#sheet .row .btn", "Stop tracking"))
    chrome.js(until("#toast.on"))
    chrome.js(wait(600))
    assert SCE not in {r["name"] for r in chrome.js(ROWS)}
    assert q(fresh, "SELECT stopped_at IS NOT NULL FROM tracked_bills WHERE merchant_key = 'southern california edison'") == [(1,)]
    clean(chrome)


def test_settings_approve_applies_the_pending_agent_change(fresh, chrome):
    go(chrome, fresh, "settings")
    chrome.js(until("#page [data-pending]"))
    row = chrome.js(T + "[...document.querySelectorAll('#page [data-pending] .grow > *')].map(T)")
    assert row[0] == "Track a bill" and row[1].startswith("Track Netflix as a bill every month, the same amount each time (next ~")
    assert row[2].startswith("Claude Desktop asked")
    chrome.js(click("#page [data-pending] .btn", "Approve"))
    chrome.js(until("#toast.on"))
    assert q(fresh, "SELECT status FROM pending_actions") == [("applied",)]
    assert q(fresh, "SELECT cadence, variable, stopped_at FROM tracked_bills WHERE merchant_key = 'netflix'") == [("monthly", 0, None)]
    chrome.js(wait(600))
    assert chrome.js("document.querySelectorAll('#page [data-pending]').length") == 0
    clean(chrome)
