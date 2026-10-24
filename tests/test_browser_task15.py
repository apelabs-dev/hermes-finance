"""TASK-15's Custom net worth in headless Chrome (tests/cdp.py; https and plaid.com blocked) against a throwaway loopback
server on the gate's "full" DB (tests/gate_dbs.py: its saved selection leaves out the Nova vault, both Nova investment
accounts, the 401(k), the Honda Fit, the three homes and the Sapphire card). The Accounts card's Edit sheet: every
account with a switch and a group switch (on / off / mixed), a preview that moves by exactly the account flipped, the
presets, Save (the server keeps it: the sidebar, the Overview tile, the Accounts figure, its groups and its chart show
it, and a fresh browser profile gets the same selection) and Cancel (nothing saved). TASK-14's remembered "cash" mode
becomes "custom"."""

from __future__ import annotations

import json
import re
import time

import pytest

import gate_dbs
from cdp import READY, Chrome, Server, find_chrome
from hermes_finance import db, insights

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
MONEY = re.compile(r"([−-]?)\$([0-9][0-9,]*(?:\.[0-9]+)?)")

PAGE = r"""(() => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
  const card = [...document.querySelectorAll('#page .card')].find((c) => T(c.querySelector('h3 .ttl')) === 'Net worth');
  const cv = card && card.querySelector('.ch-nw canvas');
  const ch = cv && Chart.getChart(cv);
  const note = card && card.querySelector('.nwnote');
  const shown = (el) => getComputedStyle(el).display !== 'none';
  return {hash: location.hash, stored: localStorage.getItem('hf.networth.mode'),
    side: T(document.getElementById('snw')), sideOwnOwe: [T(document.getElementById('sas')), T(document.getElementById('sli'))],
    big: card && T(card.querySelector('.big')), own: card && T(card.querySelector('.ac, .tl2')),
    pressed: card && [...card.querySelectorAll('.nwseg button[aria-pressed=true]')].map(T),
    note: note ? (note.hidden ? null : T(note)) : null, chart: ch ? ch.data.datasets[0].data : null,
    groups: card ? [...card.querySelectorAll('.grp')].map((g) => ({label: T(g.querySelector('.grow > b')).split(' ·')[0],
      state: g.dataset.nw, sub: T(g.querySelector('.grow > span')), total: T(g.querySelector('.gtot'))})) : [],
    // the "Not counted" shown: the Accounts cards' rows (by name) and the sidebar's bank lists. Approved density mock D4 B:
    // rows say Not counted on their details line (.meta2 .nc2, shown in Custom only) instead of a wrapping .tag.nct pill.
    tagged: [...document.querySelectorAll('#page .card .li .nc2')].filter(shown).map((t) => t.closest('.li').querySelector('.nmrow .nm').textContent.trim()),
    sideTagged: [...document.querySelectorAll('#side .ac .tag.nct')].filter(shown).length};
})()"""

SHEET = r"""(() => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
  const s = document.getElementById('sheet');
  return {open: document.getElementById('modal').classList.contains('on') && s.classList.contains('nwsheet'),
    title: T(s.querySelector('h2')), preview: T(s.querySelector('.nwprev .big')), own: T(s.querySelector('.nwprev .ac')),
    count: T(s.querySelector('.nwcnt')),
    presets: Object.fromEntries([...s.querySelectorAll('.nwpre .fchip')].map((b) => [T(b), b.getAttribute('aria-pressed')])),
    groups: Object.fromEntries([...s.querySelectorAll('.nwg')].map((g) => [g.dataset.group, {
      state: g.querySelector('.nwgh .tgl').getAttribute('aria-checked'), role: g.querySelector('.nwgh .tgl').getAttribute('role'),
      tally: T(g.querySelector('.nwgt .grow > span')), total: T(g.querySelector('.nwgh .amt')), shut: g.classList.contains('shut'),
      expanded: g.querySelector('.nwgt').getAttribute('aria-expanded'),
      rows: [...g.querySelectorAll('.nwr')].map((r) => ({id: r.dataset.id, on: r.querySelector('.tgl').getAttribute('aria-checked'),
        role: r.querySelector('.tgl').getAttribute('role'), amt: T(r.querySelector('.amt')), visible: r.getClientRects().length > 0}))}]))};
})()"""


def money(text: str) -> float:
    m = MONEY.search(text)
    v = float(m.group(2).replace(",", ""))
    return -v if m.group(1) else v


def monies(text: str) -> list[float]:
    return [(-1 if s else 1) * float(v.replace(",", "")) for s, v in MONEY.findall(text)]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("browser15") / "finance.db"
    gate_dbs.build("full", path)
    conn = db.connect(path)
    site_saved = insights.networth_excluded(conn)
    conn.close()
    srv = Server(path)
    srv.path, srv.saved = path, site_saved
    yield srv
    srv.close()


def server_nw(site) -> dict:
    conn = db.connect(site.path)
    try:
        return insights.net_worth(conn)
    finally:
        conn.close()


def saved(site) -> list[str]:
    conn = db.connect(site.path)
    try:
        return json.loads(conn.execute("SELECT value FROM settings WHERE key = 'networth_excluded'").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def browser(site):
    conn = db.connect(site.path)
    insights.save_networth_excluded(conn, site.saved)   # every test starts from full's saved selection
    conn.close()
    with Chrome() as b:   # a fresh profile each time: nothing remembered from another test
        b.size(1440, 900)
        yield b


def load(b, site, page):
    b.goto(f"{site.url}/?t={time.time():.3f}#/{page}")
    b.settle()


def nav(b, page, title):  # in-app navigation: the hash only, no reload
    b.js(f"location.hash = '#/{page}'")
    b.js("new Promise((r) => { const t0 = Date.now(); const tick = () => document.title.startsWith(" + repr(title) +
         ") || Date.now() - t0 > 5000 ? r(1) : setTimeout(tick, 30); tick(); })")
    b.js(READY)
    b.settle()


def open_sheet(b, via="#page .nwseg button[data-nw=custom]"):
    b.js(f"document.querySelector({via!r}).click()")
    b.settled()   # P3a motion: the sheet slides in for up to 250 ms, longer than settle()'s two frames
    return b.js(SHEET)


def click(b, sel):
    b.js(f"document.querySelector({sel!r}).click()")
    b.settle()


def rows(sheet) -> dict:
    return {r["id"]: r for g in sheet["groups"].values() for r in g["rows"]}


def test_edit_flip_save_shows_everywhere_and_persists(browser, site):
    b = browser
    nw = server_nw(site)
    load(b, site, "accounts")
    before = b.js(PAGE)
    assert before["stored"] is None and before["pressed"] == ["All"] and money(before["big"]) == nw["net"]
    assert before["tagged"] == [] and before["sideTagged"] == 0     # All: no tag shows
    sh = open_sheet(b)
    assert sh["open"] and sh["title"] == "Custom net worth" and money(sh["preview"]) == nw["custom"]["net"]
    assert monies(sh["own"]) == [nw["custom"]["assets"], nw["custom"]["debts"]]
    everyone = insights.networth_accounts(db.connect(site.path))
    assert set(rows(sh)) == {a["id"] for a in everyone} and len(rows(sh)) == 19   # every account, no archived one
    out = {x["id"] for x in nw["custom"]["excluded"]}
    assert all(r["on"] == ("false" if i in out else "true") and r["role"] == "switch" for i, r in rows(sh).items())
    signed = {a["id"]: a["signed"] for a in everyone}
    assert all(money(r["amt"]) == signed[i] for i, r in rows(sh).items())
    assert {k: (g["state"], g["tally"]) for k, g in sh["groups"].items()} == {
        "cash": ("mixed", "3 of 4 counted"), "investments": ("false", "0 of 3 counted"), "vehicles": ("mixed", "1 of 2 counted"),
        "property": ("false", "0 of 3 counted"), "cards": ("mixed", "4 of 5 counted"), "loans": ("true", "2 of 2 counted")}
    assert all(g["role"] == "checkbox" for g in sh["groups"].values())
    assert sh["count"] == "9 of 19 accounts not counted" and sh["presets"] == {"Everything": "false", "Cash & debts": "false"}

    # flip one Plaid account off (a row tap) and one manual debt off (its switch): the preview moves by exactly them
    click(b, "#sheet .nwr[data-id=acc_nova_sav]")
    one = b.js(SHEET)
    assert money(one["preview"]) == round(nw["custom"]["net"] - signed["acc_nova_sav"], 2)
    assert one["groups"]["cash"]["tally"] == "2 of 4 counted" and rows(one)["acc_nova_sav"]["on"] == "false"
    loan = next(a["id"] for a in everyone if a["name"] == "Car Loan")
    click(b, f"#sheet .nwr[data-id='{loan}'] .tgl")
    two = b.js(SHEET)
    assert money(two["preview"]) == round(nw["custom"]["net"] - signed["acc_nova_sav"] - signed[loan], 2)
    assert two["groups"]["loans"]["state"] == "mixed" and two["count"] == "11 of 19 accounts not counted"
    assert saved(site) == site.saved                                # nothing saved yet

    b.js("window.__same = 1")
    click(b, "#sheet .shd .ib[aria-label=Close]")                  # closing saves (TASK-16: no Save button)
    b.js(READY)
    b.settle()
    time.sleep(0.3)
    b.settle()
    after = b.js(PAGE)
    assert b.js("window.__same") == 1 and not b.js("document.getElementById('modal').classList.contains('on')")
    assert set(saved(site)) == set(site.saved) | {"acc_nova_sav", loan}
    nw2 = server_nw(site)
    assert money(two["preview"]) == nw2["custom"]["net"]            # the preview was the saved figure
    assert after["stored"] == "custom" and after["pressed"] == ["Custom"] and money(after["big"]) == nw2["custom"]["net"]
    assert monies(after["own"]) == [nw2["custom"]["assets"], nw2["custom"]["debts"]]
    assert money(after["side"]) == round(nw2["custom"]["net"]) and after["sideOwnOwe"][0].startswith("Own ")
    assert after["chart"] == [p["custom_net"] for p in nw2["history"]]
    assert after["note"] == "Custom · 11 accounts not counted"
    states = {g["label"]: (g["state"], g["sub"], money(g["total"]) if g["total"] else None) for g in after["groups"]}
    cash = next(g for g in nw2["breakdown"] if g["key"] == "cash")
    assert states["Cash"] == ("part", "2 of 4 counted", cash["custom"]["signed"])
    assert states["Investments"][:2] == ("off", "Not counted") and states["Property"][:2] == ("off", "Not counted")
    assert states["Loans"] == ("part", "1 of 2 counted", 0.0)
    assert sum(t for s, _, t in states.values() if s != "off" and t is not None) == pytest.approx(nw2["custom"]["net"], abs=0.005)
    assert set(after["tagged"]) == {x["name"] for x in nw2["custom"]["excluded"]}       # every left-out account, only them
    # the sidebar's bank lists (accounts under $1 wait behind "show all") and "Added by you" too
    shown = [x for x in nw2["custom"]["excluded"] if x["source"] == "manual" or abs(x["balance"]) >= 1]
    assert after["sideTagged"] == len(shown) == 9
    click(b, "#side .bank .showall")
    assert b.js(PAGE)["sideTagged"] == 11
    assert "toast on" in b.js("document.getElementById('toast').className")
    assert "11 accounts not counted" in b.js("document.getElementById('toast').textContent")
    nav(b, "overview", "Overview")
    tile = b.js(PAGE)
    assert tile["pressed"] == ["Custom"] and money(tile["big"]) == nw2["custom"]["net"]
    assert b.problems() == []

    # another device: a fresh profile starts in All, and its Custom is the same saved selection
    with Chrome() as other:
        other.size(1440, 900)
        load(other, site, "accounts")
        first = other.js(PAGE)
        assert first["stored"] is None and first["pressed"] == ["All"] and money(first["big"]) == nw2["net"]
        click(other, "#page .nwseg button[data-nw=custom]")
        assert money(other.js(PAGE)["big"]) == nw2["custom"]["net"]
        sh = open_sheet(other)
        assert {i for i, r in rows(sh).items() if r["on"] == "false"} == set(saved(site))
        assert other.problems() == []


def test_presets_group_switches_and_close_saves(browser, site):
    b = browser
    nw = server_nw(site)
    load(b, site, "accounts")
    open_sheet(b)
    click(b, "#sheet .nwpre .fchip:first-of-type")                  # Everything
    sh = b.js(SHEET)
    assert money(sh["preview"]) == nw["net"] and all(r["on"] == "true" for r in rows(sh).values())
    assert sh["presets"] == {"Everything": "true", "Cash & debts": "false"} and sh["count"] == "all 19 accounts counted"
    click(b, "#sheet .nwpre .fchip:last-of-type")                   # Cash & debts
    sh = b.js(SHEET)
    conn = db.connect(site.path)
    cash = insights.net_worth(conn, excluded=insights.cash_preset(conn))["custom"]
    preset = set(insights.cash_preset(conn))
    conn.close()
    assert money(sh["preview"]) == cash["net"] and sh["presets"] == {"Everything": "false", "Cash & debts": "true"}
    assert {i for i, r in rows(sh).items() if r["on"] == "false"} == preset
    # a group switch: mixed or off -> all on, all on -> all off
    click(b, "#sheet .nwg[data-group=vehicles] .nwgh .tgl")
    sh = b.js(SHEET)
    assert sh["groups"]["vehicles"]["state"] == "true" and all(r["on"] == "true" for r in sh["groups"]["vehicles"]["rows"])
    click(b, "#sheet .nwg[data-group=cards] .nwgh .tgl")
    sh = b.js(SHEET)
    assert sh["groups"]["cards"]["state"] == "false" and money(sh["groups"]["cards"]["total"]) == 0.0
    # folding a group away hides its rows only
    click(b, "#sheet .nwg[data-group=cash] .nwgt")
    sh = b.js(SHEET)
    assert sh["groups"]["cash"]["shut"] and sh["groups"]["cash"]["expanded"] == "false"
    assert not any(r["visible"] for r in sh["groups"]["cash"]["rows"]) and all(r["visible"] for r in sh["groups"]["loans"]["rows"])
    # there's no Save or Cancel: the sheet has no footer, only ×
    assert b.js("document.querySelectorAll('#sheet .nwfoot, #sheet .btn').length") == 0
    # Escape closes it and saves what changed (Cash & debts preset, then vehicles on, cards off)
    want = (preset - {r["id"] for r in sh["groups"]["vehicles"]["rows"]}) | {r["id"] for r in sh["groups"]["cards"]["rows"]}
    b.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    b.settle()
    time.sleep(0.3)
    b.settle()
    assert not b.js("document.getElementById('modal').classList.contains('on')")
    assert set(saved(site)) == want and b.js("localStorage.getItem('hf.networth.mode')") == "custom"
    nw2 = server_nw(site)
    assert money(b.js(PAGE)["big"]) == nw2["custom"]["net"] == money(sh["preview"])
    assert "Custom net worth saved" in b.js("document.getElementById('toast').textContent")
    # reopen, change nothing, tap outside: nothing is sent (the saved selection is untouched)
    sh = open_sheet(b)
    assert {i for i, r in rows(sh).items() if r["on"] == "false"} == want
    b.js("window.__posts = 0; const f = window.fetch; window.fetch = (u, o) => { if (o && o.method === 'POST') window.__posts++; return f(u, o); }")
    b.js("document.getElementById('modal').dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    b.settle()
    time.sleep(0.3)
    assert not b.js("document.getElementById('modal').classList.contains('on')") and b.js("window.__posts") == 0
    assert set(saved(site)) == want
    assert b.problems() == []


def test_the_quiet_line_opens_the_sheet(browser, site):
    b = browser
    load(b, site, "accounts")
    click(b, "#page .nwseg button[data-nw=custom]")
    page = b.js(PAGE)
    assert page["note"] == "Custom · 9 accounts not counted"
    sh = open_sheet(b, "#page .nwnote button")
    assert sh["open"] and money(sh["preview"]) == money(page["big"])
    assert b.problems() == []


def test_task14_cash_mode_becomes_custom(browser, site):
    b = browser
    nw = server_nw(site)
    load(b, site, "overview")
    b.js("localStorage.setItem('hf.networth.mode', 'cash')")
    load(b, site, "overview")
    r = b.js(PAGE)
    assert r["stored"] == "custom" and r["pressed"] == ["Custom"] and money(r["big"]) == nw["custom"]["net"]
    assert b.js("document.documentElement.dataset.nw") == "custom"
    assert b.problems() == []


@pytest.mark.parametrize("width,height", [(1440, 900), (1024, 768), (390, 844)])
def test_sheet_fits_and_scrolls_its_list(browser, site, width, height):
    b = browser
    b.size(width, height)
    load(b, site, "accounts")
    open_sheet(b)
    r = b.js("""(() => { const s = document.getElementById('sheet').getBoundingClientRect();
      const list = document.querySelector('#sheet .nwlist'), save = document.querySelector('#sheet .shd .ib[aria-label=Close]').getBoundingClientRect();
      const prev = document.querySelector('#sheet .nwprev').getBoundingClientRect();
      return {inView: s.top >= 0 && s.bottom <= innerHeight + 0.5 && s.left >= 0 && s.right <= innerWidth + 0.5,
        saveVisible: save.top >= s.top && save.bottom <= s.bottom, previewVisible: prev.top >= s.top,
        scrolls: list.scrollHeight > list.clientHeight, wide: list.scrollWidth > list.clientWidth + 1,
        pageWide: document.documentElement.scrollWidth > innerWidth}; })()""")
    assert r == {"inView": True, "saveVisible": True, "previewVisible": True, "scrolls": True, "wide": False,
                 "pageWide": False}, (width, r)
    # scrolled to the bottom of the list: the preview and Save stay where they are
    b.js("document.querySelector('#sheet .nwlist').scrollTop = 99999")
    b.settle()
    assert b.js("document.querySelector('#sheet .nwprev').getBoundingClientRect().top >= "
                "document.getElementById('sheet').getBoundingClientRect().top")
    assert b.problems() == []


def test_custom_chip_opens_the_sheet_everywhere_and_edit_is_gone(browser, site):
    """TASK-16: no Edit button; a tap on Custom (on the Accounts card or the Overview tile, already on or not) opens the
    sheet. The Overview tile is a link to Accounts: the tap must not navigate."""
    b = browser
    nw = server_nw(site)
    load(b, site, "accounts")
    assert b.js("document.querySelectorAll('#page .nwedit').length") == 0
    assert "Edit" not in [b2 for b2 in b.js("[...document.querySelectorAll('#page .card button')].map((x) => x.textContent.trim())")]
    sh = open_sheet(b)                                              # from All
    assert sh["open"] and money(sh["preview"]) == nw["custom"]["net"]
    b.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    b.settle()
    assert b.js("document.documentElement.dataset.nw") == "custom"   # the page stays in Custom
    sh = open_sheet(b)                                              # already in Custom: opens again
    assert sh["open"]
    b.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    b.settle()
    load(b, site, "overview")
    sh = open_sheet(b, "#page .nwtile .nwseg button[data-nw=custom]")
    assert sh["open"] and money(sh["preview"]) == nw["custom"]["net"]
    assert b.js("location.hash") == "#/overview"                     # the tile's link didn't fire
    b.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    b.settle()
    assert saved(site) == site.saved                                # nothing changed, nothing sent
    assert b.problems() == []
