"""TASK-14's net worth toggle, All · Custom since TASK-15, in headless Chrome (tests/cdp.py; https and plaid.com blocked)
against a throwaway loopback server on the gate's "full" DB (tests/gate_dbs.py) without its saved Custom selection, so
the first read saves TASK-14's "Cash & debts" one and Custom shows exactly the cash & debts numbers. Flipping it on
Accounts changes the sidebar, the Overview tile, the Accounts figure and its chart to them without a reload; a reload
keeps the mode (localStorage hf.networth.mode); flipping back gives the original numbers exactly; a tap on the tile's
toggle never navigates (the tile is a link to Accounts). The selection itself: tests/test_browser_task15.py."""

from __future__ import annotations

import re
import time

import pytest

import gate_dbs
from cdp import READY, Chrome, Server, find_chrome
from hermes_finance import db, insights

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
MONEY = re.compile(r"([−-]?)\$([0-9][0-9,]*(?:\.[0-9]+)?)")

# Everything the net worth views show, as text, plus the chart's data and the toggle's state.
READ = r"""(() => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
  const card = [...document.querySelectorAll('#page .card')].find((c) => T(c.querySelector('h3 .ttl')) === 'Net worth');
  const cv = card && card.querySelector('.ch-nw canvas');
  const ch = cv && Chart.getChart(cv);
  const note = card && card.querySelector('.nwnote');
  return {hash: location.hash, stored: localStorage.getItem('hf.networth.mode'),
    side: {net: T(document.getElementById('snw')), own: T(document.getElementById('sas')), owe: T(document.getElementById('sli')),
      note: T(document.getElementById('nwn'))},
    big: card && T(card.querySelector('.big')), own: card && T(card.querySelector('.ac, .tl2')),
    pressed: card && [...card.querySelectorAll('.nwseg button[aria-pressed=true]')].map(T),
    note: note ? (note.hidden ? null : T(note)) : null,
    chart: ch ? ch.data.datasets[0].data : null,
    groups: card ? [...card.querySelectorAll('.grp')].map((g) => ({label: T(g.querySelector('.grow > b')),
      off: g.classList.contains('off'), sub: T(g.querySelector('.grow > span')), total: T(g.querySelector('.gtot')),
      share: g.querySelector('.share i') ? g.querySelector('.share i').style.width : null})) : []};
})()"""


def money(text: str) -> float:
    m = MONEY.search(text)
    v = float(m.group(2).replace(",", ""))
    return -v if m.group(1) else v


def monies(text: str) -> list[float]:
    return [(-1 if s else 1) * float(v.replace(",", "")) for s, v in MONEY.findall(text)]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("browser14") / "finance.db"
    gate_dbs.build("full", path)
    conn = db.connect(path)
    with conn:   # no saved selection: the first read saves the "Cash & debts" one
        conn.execute("DELETE FROM settings WHERE key = 'networth_excluded'")
    nw = insights.net_worth(conn)
    assert {k: nw["custom"][k] for k in ("assets", "debts", "net")} == \
        {k: insights.net_worth(conn, excluded=insights.cash_preset(conn))["custom"][k] for k in ("assets", "debts", "net")}
    conn.close()
    srv = Server(path)
    srv.nw = nw
    yield srv
    srv.close()


@pytest.fixture
def browser(site):
    with Chrome() as b:   # a fresh profile each time: nothing remembered from another test
        b.size(1440, 900)
        yield b


def load(b, site, page):  # a real page load (a reload of the app)
    b.goto(f"{site.url}/?t={time.time():.3f}#/{page}")
    b.settle()


def nav(b, page, title):  # in-app navigation: the hash only, no reload
    b.js(f"location.hash = '#/{page}'")
    b.js("new Promise((r) => { const t0 = Date.now(); const tick = () => document.title.startsWith(" + repr(title) +
         ") || Date.now() - t0 > 5000 ? r(1) : setTimeout(tick, 30); tick(); })")
    b.js(READY)
    b.settle()


def flip(b, mode):
    b.js(f"document.querySelector('#page .nwseg button[data-nw={mode}]').click()")
    b.settle()


def test_flip_on_accounts_changes_everything_persists_and_flips_back(browser, site):
    b, nw = browser, site.nw
    co = nw["custom"]
    b.js("0")
    load(b, site, "overview")
    tile_all = b.js(READ)
    assert tile_all["stored"] is None and tile_all["pressed"] == ["All"] and tile_all["note"] is None
    assert money(tile_all["big"]) == nw["net"] and monies(tile_all["own"]) == [round(nw["assets"]), round(nw["debts"])]
    nav(b, "accounts", "Accounts")
    acc_all = b.js(READ)
    assert acc_all["hash"] == "#/accounts" and acc_all["pressed"] == ["All"] and acc_all["note"] is None
    assert money(acc_all["big"]) == nw["net"] and acc_all["own"].startswith("Own ")
    assert monies(acc_all["own"]) == [nw["assets"], nw["debts"]]
    assert acc_all["chart"] == [p["net"] for p in nw["history"]]
    assert acc_all["side"]["own"].startswith("Own ") and money(acc_all["side"]["net"]) == round(nw["net"])
    assert not any(g["off"] for g in acc_all["groups"])

    # flip on Accounts: the figure, Own / Owe, the chart and the sidebar change in place
    b.js("window.__same = 1")
    flip(b, "custom")
    cash = b.js(READ)
    assert b.js("window.__same") == 1                                   # no reload
    assert cash["stored"] == "custom" and cash["pressed"] == ["Custom"]
    assert money(cash["big"]) == co["net"] and cash["own"].startswith("Own ")
    assert monies(cash["own"]) == [co["assets"], co["debts"]]
    assert cash["note"] == "Custom · 8 accounts not counted"         # 3 investments, 2 cars, 3 homes
    assert cash["chart"] == [p["custom_net"] for p in nw["history"]] != acc_all["chart"]
    assert money(cash["side"]["net"]) == round(co["net"]) and cash["side"]["own"] == f"Own ${round(co['assets']):,}"
    assert monies(cash["side"]["owe"]) == [round(co["debts"])]
    assert cash["side"]["note"] == "Custom" and acc_all["side"]["note"] == "incl. manual"   # the note names the mode
    off = [g for g in cash["groups"] if g["off"]]
    assert [g["label"].split(" ·")[0] for g in off] == ["Investments", "Vehicles", "Property"]
    assert all(g["sub"] == "Not counted" and g["share"] is None and g["total"] for g in off)     # totals still shown
    assert [g["total"] for g in cash["groups"]] == [g["total"] for g in acc_all["groups"]]
    counted = {g["label"].split(" ·")[0]: g["share"] for g in cash["groups"] if not g["off"]}
    assert counted["Cash"] == "100%"                                    # of the mode's own assets
    cards = next(g for g in nw["breakdown"] if g["key"] == "cards")
    assert float(counted["Credit cards"].rstrip("%")) == pytest.approx(cards["total"] / co["debts"] * 100, abs=0.01)

    # the tile, reached without a reload, is in the mode too
    nav(b, "overview", "Overview")
    tile = b.js(READ)
    assert b.js("window.__same") == 1
    assert tile["pressed"] == ["Custom"] and money(tile["big"]) == co["net"]
    assert monies(tile["own"]) == [round(co["assets"]), round(co["debts"])] and tile["own"].startswith("Own ")

    # a reload keeps it
    load(b, site, "overview")
    again = b.js(READ)
    assert b.js("window.__same") is None
    assert again["stored"] == "custom" and again["pressed"] == ["Custom"] and again["big"] == tile["big"]
    assert again["side"] == cash["side"]
    load(b, site, "accounts")
    assert b.js(READ)["big"] == cash["big"]

    # flip back: the original numbers exactly
    flip(b, "all")
    back = b.js(READ)
    for k in ("big", "own", "chart", "groups", "side", "note", "pressed"):
        assert back[k] == acc_all[k], k
    assert back["stored"] == "all"
    nav(b, "overview", "Overview")
    tile_back = b.js(READ)
    for k in ("big", "own", "side", "pressed"):
        assert tile_back[k] == tile_all[k], k


def test_tapping_the_toggle_on_the_tile_does_not_navigate(browser, site):
    b, co = browser, site.nw["custom"]
    load(b, site, "overview")
    b.js("window.__same = 1")
    # a real mouse tap on the button (Input.dispatchMouseEvent), not only el.click()
    box = b.js("(() => { const r = document.querySelector('#page .nwtile .nwseg button[data-nw=custom]').getBoundingClientRect();"
               " scrollTo(0, scrollY + r.top - 200); const q = document.querySelector('#page .nwtile .nwseg button[data-nw=custom]')"
               ".getBoundingClientRect(); return [q.left + q.width / 2, q.top + q.height / 2]; })()")
    for kind in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=kind, x=box[0], y=box[1], button="left", clickCount=1)
    b.settle()
    time.sleep(0.3)
    r = b.js(READ)
    assert r["hash"] == "#/overview" and b.js("window.__same") == 1
    assert r["pressed"] == ["Custom"] and money(r["big"]) == co["net"]
    # the space between / around the buttons is part of the control: no navigation either
    b.js("document.querySelector('#page .nwtile .nwseg').dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))")
    time.sleep(0.3)
    assert b.js("location.hash") == "#/overview"
    b.js("document.querySelector('#page .nwtile .nwseg button[data-nw=all]').click()")
    time.sleep(0.2)
    r = b.js(READ)
    assert r["hash"] == "#/overview" and r["pressed"] == ["All"] and money(r["big"]) == site.nw["net"]
    # the rest of the tile is still the link to Accounts
    b.js("document.querySelector('#page .nwtile .big').click()")
    b.js("new Promise((r) => { const t0 = Date.now(); const tick = () => location.hash === '#/accounts' || Date.now() - t0 > 3000"
         " ? r(1) : setTimeout(tick, 30); tick(); })")
    assert b.js("location.hash") == "#/accounts"
    assert b.problems() == []


@pytest.mark.parametrize("width,height", [(1440, 900), (1024, 768), (390, 844)])
def test_toggle_on_one_line_at_every_width(browser, site, width, height):
    b = browser
    b.size(width, height)
    for mode in ("all", "custom"):
        for page in ("overview", "accounts"):
            load(b, site, page)
            b.js(f"localStorage.setItem('hf.networth.mode', '{mode}')")
            load(b, site, page)
            r = b.js("""(() => { const seg = document.querySelector('#page .nwseg');
              const [a, c] = [...seg.querySelectorAll('button')].map((x) => x.getBoundingClientRect());
              const h3 = seg.closest('h3').getBoundingClientRect(), s = seg.getBoundingClientRect();
              const card = seg.closest('.card').getBoundingClientRect();
              const chev = seg.closest('h3').querySelector('.end');
              return {oneLine: Math.abs(a.top - c.top) < 1 && a.height < 40 && c.height < 40, inCard: s.left >= card.left && s.right <= card.right,
                chevronTop: chev ? Math.abs(chev.getBoundingClientRect().top - h3.top) < 12 : true,
                wide: document.documentElement.scrollWidth > innerWidth,
                pressed: seg.querySelector('[aria-pressed=true]').dataset.nw}; })()""")
            assert r == {"oneLine": True, "inCard": True, "chevronTop": True, "wide": False, "pressed": mode}, (width, mode, page, r)
    assert b.problems() == []
