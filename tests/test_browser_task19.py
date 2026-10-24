"""TASK-19 (Batch C) in headless Chrome (tests/cdp.py; https and plaid.com blocked) against a throwaway loopback server
on the gate's "features" DB (tests/gate_dbs.py): every new screen loads cleanly at 1440 and 390; the Goals tile and
page; the transaction sheet docks beside the table from 1400px and is the usual sheet below, a split made in it saves
when it closes (only once it adds up), with its note and tags; the New goal sheet blocks an earmark above what's
unassigned; the bills calendar (a grid on desktop, an agenda on a phone); the rollover switch in the cap editor; the
Alpaca row and its Connect form (the server's kill switch refuses the check: nothing is written)."""

from __future__ import annotations

import json
import math
import re
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import classify, config, db, goals, insights

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()   # the page reads its month from the browser clock: the server uses the same day

T = "var T = (el) => (el ? el.textContent.replace(/\\s+/g, ' ').trim() : null);"
WAIT = "new Promise((r) => setTimeout(r, {ms}))"


MINUS = "-"


def signed_money(x: float) -> str:
    """app.js splitMoney (a tile's big figure) in en-US, worked out here: an ASCII minus before the "$" below zero
    ("-$201.50"), none at zero ("$0", as for anything under half a cent), whole dollars grouped ("$1,234.56")."""
    a = Decimal(abs(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)   # amounts are rounded to cents before suppressing a zero fraction
    number = f"{a:,.0f}" if a == a.to_integral_value() else f"{a:,.2f}"
    return f"{MINUS if x < 0 and a else ''}${number}"


def ring_pct(committed: float, cap: float) -> int:
    """app.js ringPctOf: the whole %, half up, never 100 unless it is exactly 100 (99 or 101)."""
    raw = (committed / cap if cap > 0 else 1 if committed > 0 else 0) * 100
    whole = math.floor(raw + 0.5)
    return (99 if raw < 100 else 101) if whole == 100 and raw != 100 else whole


def month_net(path, month: str | None = None) -> float:
    """The Overview's month (this one unless given): its income − spending, from the DB (insights, not the page)."""
    conn = db.connect(path)
    net = insights.cashflow(conn, 6, TODAY, end=month)["months"][-1]["net"]
    conn.close()
    return net


def fill(sel: str, value: str) -> str:
    return (f"(() => {{ const i = document.querySelector({json.dumps(sel)}); i.value = {json.dumps(value)}; "
            f"i.dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()")


def click(sel: str, text: str | None = None) -> str:
    return (f"(() => {{ const els = [...document.querySelectorAll({json.dumps(sel)})].filter((e) => e.getClientRects().length); "
            f"const el = els.find((e) => {json.dumps(text)} === null || e.textContent.trim().startsWith({json.dumps(text)})); "
            f"if (!el) throw new Error('nothing to click: ' + {json.dumps(sel)}); el.click(); return 1; }})()")


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("browser19") / "finance.db"
    gate_dbs.build("features", path, TODAY)
    srv = Server(path, today=TODAY)
    srv.path = path
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
    b.js(WAIT.format(ms=250))


def test_every_new_screen_loads_cleanly(site, chrome):
    for width in (1440, 390):
        for page in ("overview", "goals", "transactions", "budgets", "recurring", "investments", "accounts"):
            go(chrome, site, page, width)
            assert chrome.problems() == [], (width, page)
            assert chrome.js("document.documentElement.scrollWidth <= innerWidth + 1"), (width, page)


def test_the_goals_tile_and_page(site, chrome):
    go(chrome, site, "overview")
    tile = chrome.js(T + """(() => { const c = [...document.querySelectorAll('#page .tile')].find((x) => T(x.querySelector('h3 .ttl')) === 'Goals');
      return c && {href: c.getAttribute('href'), big: T(c.querySelector('.big')), tl2: T(c.querySelector('.tl2')),
        tiles: document.querySelectorAll('#page .tiles > .tile').length}; })()""")
    assert tile["href"] == "#/goals" and tile["tiles"] == 4 and "of $800" in tile["tl2"]
    # the month's net saved (bigMoney): signed like all money, with ASCII hyphen-minus ("-$201.50" late in a month that spent more)
    assert re.fullmatch(r"-?\$\d{1,3}(,\d{3})*(\.\d{2})?", tile["big"]), tile["big"]
    # (baseline K4) the month's savings, signed: on Sept 30 it was -$201.50 (the test wanted a "$" first); worked out
    # from the DB and written as the page writes money, whatever its sign
    net = month_net(site.path)
    assert tile["big"] == signed_money(net), (net, tile)
    assert tile["tl2"].startswith(f"Saved in {TODAY:%b} · {ring_pct(max(net, 0), 800)}% of $800"), (net, tile)
    go(chrome, site, "goals")
    page = chrome.js(T + """(() => ({cards: [...document.querySelectorAll('#page [data-goal]')].map((c) => T(c.querySelector('.hd b, h3 .ttl'))),
      short: [...document.querySelectorAll('#page .gshort .pill')].map(T), nav: T(document.querySelector('#nav [aria-current=page]')),
      suggest: document.querySelectorAll('#page [data-suggest]').length}))()""")
    assert page["nav"] == "Goals" and page["suggest"] == 1
    assert page["cards"] == ["Monthly savings", "Rainy day", "New laptop", "Concert tickets", "Trip to Lisbon", "Pay off Car Loan"]
    assert page["short"] == ["Short by $179.63"]


def set_month_net(path, target: float, n: int) -> float:
    """(baseline K4) One more charge today (spending) or deposit (income) so this month's income − spending is `target`;
    returns what the DB now says it is."""
    gap = round(month_net(path) - target, 2)
    if gap:
        conn = db.connect(path)
        with conn:   # Plaid's sign: > 0 is money out
            gate_dbs._txn(conn, f"k4_ctl_{n}", "acc_nova_chk", "demo_nova", TODAY.isoformat(), "Tile control", "Tile control", gap,
                          ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE") if gap > 0 else ("INCOME", "INCOME_WAGES"))
        classify.apply(conn)
        conn.close()
    return month_net(path)


@pytest.fixture(scope="module")
def tile_site(tmp_path_factory):
    """(baseline K4) the gate's "full" DB with a Monthly savings goal of $800 and no Save up goal: the tile's figure is the
    month's savings alone. Its own DB: the controls below add charges to this month."""
    path = gate_dbs.build("full", tmp_path_factory.mktemp("browser19k4") / "finance.db", TODAY)
    conn = db.connect(path)
    goals.create(conn, {"kind": "monthly", "target": 800}, TODAY)
    conn.close()
    srv = Server(path, today=TODAY)
    srv.path = path
    yield srv
    srv.close()


def test_the_goals_tile_shows_the_months_savings_with_its_sign(tile_site, chrome):
    """(baseline K4) the tile's figure is this month's income − spending with its sign, whatever the day: made positive,
    zero and negative here (the Sept 30 figure among them), and last month's (a whole month) when the Overview shows it."""
    tile_js = T + """(() => { const c = [...document.querySelectorAll('#page .tile')].find((x) => T(x.querySelector('h3 .ttl')) === 'Goals');
      return c && {big: T(c.querySelector('.big')), tl2: T(c.querySelector('.tl2'))}; })()"""
    for n, target in enumerate((1234.56, 0.0, -201.50)):
        net = set_month_net(tile_site.path, target, n)
        assert net == target, (target, net)
        go(chrome, tile_site, "overview")
        tile = chrome.js(tile_js)
        pct = ring_pct(max(net, 0), 800)
        assert tile == {"big": signed_money(net), "tl2": f"Saved in {TODAY:%b} · {pct}% of $800"}, (net, tile)
        assert chrome.problems() == []
    assert [signed_money(x) for x in (1234.56, 0.0, -201.50)] == ["$1,234.56", "$0", "-$201.50"]
    assert [ring_pct(max(x, 0), 800) for x in (1234.56, 0.0, -201.50)] == [154, 0, 0]
    last = (TODAY.replace(day=1) - timedelta(days=1)).isoformat()[:7]   # the month before, across the month's first day
    net = month_net(tile_site.path, last)
    go(chrome, tile_site, f"overview?month={last}")
    tile = chrome.js(tile_js)
    mon = date.fromisoformat(f"{last}-01")
    assert tile == {"big": signed_money(net), "tl2": f"Saved in {mon:%b} · {ring_pct(max(net, 0), 800)}% of $800"}, (last, net, tile)
    assert chrome.problems() == []


def test_the_new_goal_sheet_blocks_an_earmark_above_whats_unassigned(site, chrome):
    go(chrome, site, "goals")
    chrome.js(click("#page .gsum [data-add=goal]"))
    chrome.js(click("#sheet .choice", "Part of an account"))
    chrome.js(fill("#g-name", "Bike"))
    chrome.js(fill("#g-target", "900"))
    chrome.js("(() => { const s = document.getElementById('g-acct'); s.value = 'acc_usb_chk'; s.dispatchEvent(new Event('change')); return 1; })()")
    chrome.js(fill("#g-earmark", "5,000"))
    state = chrome.js(T + """(() => ({create: [...document.querySelectorAll('#sheet .row .btn')].pop().disabled,
      err: T(document.querySelector('#sheet .gleft')), unas: T(document.querySelector('#sheet .unas'))}))()""")
    assert state["create"] is True and state["err"] == "Only $1,840 is unassigned in Smartly Checking. Set aside that much or less."
    assert state["unas"] == "Unassigned in Smartly Checking$1,840"
    chrome.js(fill("#g-earmark", "250"))
    assert chrome.js("[...document.querySelectorAll('#sheet .row .btn')].pop().disabled") is False
    assert chrome.js(T + "T(document.querySelector('#sheet .gleft'))") == "After this, $1,590 stays unassigned."
    chrome.js(click("#sheet .row .btn", "Create goal"))
    chrome.js(WAIT.format(ms=500))
    conn = db.connect(site.path)
    row = conn.execute("SELECT kind, name, target, track, account_id, earmark FROM goals WHERE name = 'Bike'").fetchone()
    conn.close()
    assert tuple(row) == ("save", "Bike", 900.0, "earmark", "acc_usb_chk", 250.0)
    assert chrome.js("document.getElementById('modal').classList.contains('on')") is False
    assert chrome.problems() == []


def test_the_add_a_new_goal_row_replaces_the_new_goal_button(site, chrome):
    """The Saving toward card ends with one always-on "Add a new goal" row: no full-width New goal button, even when a
    Monthly savings goal already exists (the gate's "features" DB has one); the row opens the New goal sheet on Save up."""
    go(chrome, site, "goals")
    row = chrome.js(T + """(() => { const sum = document.querySelector('#page .gsum');
      const adds = [...sum.querySelectorAll('[data-add=goal]')];
      const r = adds[0];
      return {adds: adds.length,
        old: [...sum.querySelectorAll('.btn')].filter((b) => T(b).startsWith('New goal')).length,
        tag: r ? r.tagName : null,
        title: r ? T(r.querySelector('.grow > b')) : null,
        text: r ? T(r.querySelector('.grow > span')) : null,
        icon: r && r.querySelector('[data-icon]') ? r.querySelector('[data-icon]').getAttribute('data-icon') : null,
        label: r ? r.getAttribute('aria-label') : null,
        clipped: r ? (() => { const d = r.querySelector('.grow > span'); return d.scrollWidth > d.clientWidth + 1; })() : null,
        width: innerWidth,
        monthly: !!document.querySelector('#page .goalm'),
        monthlyTitle: T(document.querySelector('#page .goalm h3 .ttl'))}; })()""")
    assert row["old"] == 0
    assert row["adds"] == 1
    assert row["tag"] == "BUTTON"
    assert row["title"] == "Add a new goal"
    assert row["text"] == "Save up, pay off, or monthly"
    assert row["icon"] == "plus"
    assert row["label"] == "Add a new goal"
    assert row["clipped"] is False, row   # the description wraps, never "Save up, pay off, or m…" (3-column desktop)
    # still there beside the Monthly savings card: the row is not conditional on having no monthly goal
    assert row["monthly"] is True and row["monthlyTitle"] == "Monthly savings", row
    chrome.js(click("#page .gsum [data-add=goal]"))
    chrome.js(WAIT.format(ms=300))
    opened = chrome.js("""(() => ({on: document.getElementById('modal').classList.contains('on'),
      save: document.querySelector('#sheet [data-k=save]') ? document.querySelector('#sheet [data-k=save]').getAttribute('aria-pressed') : null,
      monthly: document.querySelector('#sheet [data-k=monthly]') ? document.querySelector('#sheet [data-k=monthly]').getAttribute('aria-pressed') : null}))()""")
    assert opened == {"on": True, "save": "true", "monthly": "false"}, opened
    chrome.js("document.querySelector('#sheet .shd .ib[aria-label=Close]').click()")
    for width in (1280, 1024, 768, 390):   # the 3-column card is narrowest at 1280: the description must wrap, not end in "…"
        go(chrome, site, "goals", width)
        clip = chrome.js("""(() => { const d = document.querySelector('#page .gsum [data-add=goal] .grow > span');
          return d.scrollWidth > d.clientWidth + 1; })()""")
        assert clip is False, width
    assert chrome.problems() == []


def test_the_transaction_sheet_docks_from_1400_and_is_a_sheet_below(site, chrome):
    go(chrome, site, "transactions")
    chrome.js(click("#page table.tx tbody tr[data-tid]:not(:has(.pend)) td.num"))
    chrome.js(WAIT.format(ms=300))
    docked = chrome.js("""(() => ({panel: !document.getElementById('txpanel').hidden, open: document.body.classList.contains('txopen'),
      modal: document.getElementById('modal').classList.contains('on'), sel: document.querySelectorAll('#page tr.sel').length,
      acct: getComputedStyle(document.querySelector('#page .tx th.c-acct')).display,
      right: document.querySelector('main').getBoundingClientRect().right}))()""")
    assert docked == {"panel": True, "open": True, "modal": False, "sel": 1, "acct": "none", "right": 1000.0}
    chrome.js(click("#txpanel .shd .ib"))
    assert chrome.js("document.getElementById('txpanel').hidden && !document.body.classList.contains('txopen')")
    go(chrome, site, "transactions", 1280)
    chrome.js(click("#page table.tx tbody tr[data-tid]:not(:has(.pend)) td.num"))
    chrome.js(WAIT.format(ms=300))
    assert chrome.js("document.getElementById('modal').classList.contains('on') && document.getElementById('sheet').classList.contains('txsheet') "
                     "&& document.getElementById('txpanel').hidden")
    chrome.js("document.querySelector('#sheet .shd .ib[aria-label=Close]').click()")


def test_a_split_made_in_the_sheet_saves_when_it_closes_once_it_adds_up(site, chrome):
    conn = db.connect(site.path)
    tid, amount, category = conn.execute(
        "SELECT transaction_id, amount, category FROM v_transactions WHERE month = ? AND category = 'Eating Out' AND pending = 0 "
        "AND amount > 10 AND transaction_id NOT IN (SELECT transaction_id FROM transaction_splits) ORDER BY date DESC LIMIT 1",
        (TODAY.isoformat()[:7],)).fetchone()
    conn.close()
    go(chrome, site, "transactions", 1280)
    chrome.js(f"document.querySelector('#page tr[data-tid=\"{tid}\"] td.num').click()")
    chrome.js(WAIT.format(ms=300))
    chrome.js(click("#sheet .spl.one .btn", "Split"))
    first = round(amount - 5.25, 2)
    chrome.js(f"(() => {{ const i = document.querySelectorAll('#sheet .spl input'); i[0].value = '{first:.2f}'; "
              f"i[0].dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()")
    assert chrome.js(T + "T(document.querySelector('#sheet .remain'))") == f"Left to split${5.25:.2f}"
    # closing before it adds up saves nothing, and says so
    chrome.js("document.querySelector('#sheet .shd .ib[aria-label=Close]').click()")
    chrome.js(WAIT.format(ms=400))
    assert "Not saved" in chrome.js(T + "T(document.getElementById('toast'))")
    conn = db.connect(site.path)
    assert conn.execute("SELECT COUNT(*) FROM transaction_splits WHERE transaction_id = ?", (tid,)).fetchone()[0] == 0
    conn.close()
    chrome.js(f"document.querySelector('#page tr[data-tid=\"{tid}\"] td.num').click()")
    chrome.js(WAIT.format(ms=300))
    chrome.js(click("#sheet .spl.one .btn", "Split"))
    chrome.js(f"(() => {{ const i = document.querySelectorAll('#sheet .spl input'); i[0].value = '{first:.2f}'; i[1].value = '5.25'; "
              f"i.forEach((x) => x.dispatchEvent(new Event('input', {{bubbles: true}}))); return 1; }})()")
    assert chrome.js(T + "T(document.querySelector('#sheet .remain'))") == f"Adds up to {signed_money(amount)}"
    chrome.js(fill("#sheet textarea.txta", "the tip went on a card"))
    chrome.js(click("#sheet .addtag"))
    chrome.js("(() => { const i = document.querySelector('#sheet .taginp'); i.value = 'date night'; "
              "i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); return 1; })()")
    chrome.js("document.querySelector('#sheet .shd .ib[aria-label=Close]').click()")
    chrome.js(WAIT.format(ms=700))
    conn = db.connect(site.path)
    lines = [tuple(r) for r in conn.execute("SELECT c.name, s.amount FROM transaction_splits s JOIN categories c ON c.id = s.category_id "
                                            "WHERE s.transaction_id = ? ORDER BY s.line", (tid,))]
    note = conn.execute("SELECT note FROM transaction_notes WHERE transaction_id = ?", (tid,)).fetchone()[0]
    tags = [r[0] for r in conn.execute("SELECT tg.name FROM transaction_tags tt JOIN tags tg ON tg.id = tt.tag_id WHERE tt.transaction_id = ?", (tid,))]
    conn.close()
    assert lines[0] == (category, first) and lines[1][1] == 5.25 and len(lines) == 2
    assert note == "the tip went on a card" and "date night" in tags and len(tags) == len(set(tags))
    chrome.js(WAIT.format(ms=400))
    row = chrome.js(T + f"T(document.querySelector('#page tr[data-tid=\"{tid}\"]'))")
    assert "Split" in row and "date night" in row
    assert chrome.problems() == []


def test_the_bills_calendar_grid_and_phone_agenda(site, chrome):
    go(chrome, site, "recurring")
    d = json.loads(chrome.js("fetch('/api/bills-calendar').then((r) => r.text())"))
    grid = chrome.js(T + """(() => ({chips: document.querySelectorAll('#page .calg .bchip').length, agenda: getComputedStyle(document.querySelector('#page .agenda')).display,
      total: T(document.querySelector('#page .calsum b')), next: !!document.querySelector('#page .calnav button[aria-label="Next month"]:not([disabled])')}))()""")
    assert grid["chips"] == d["count"] and grid["agenda"] == "none" and grid["total"] == signed_money(d["total"]) and grid["next"]
    chrome.js(click("#page .calnav button[aria-label='Next month']"))
    chrome.js(WAIT.format(ms=400))
    assert chrome.js(T + "T(document.querySelector('#page .calnav span'))") != "" and chrome.problems() == []
    go(chrome, site, "recurring", 390)
    phone = chrome.js("""(() => ({grid: getComputedStyle(document.querySelector('#page .calg')).display, rows: document.querySelectorAll('#page .agenda .agi').length}))()""")
    assert phone == {"grid": "none", "rows": d["count"]}


def last_month_spend(path, category: str) -> float:
    """(baseline K6) What `category` spent last month, summed from the tables (a split charge by its lines)."""
    month = (TODAY.replace(day=1) - timedelta(days=1)).isoformat()[:7]
    conn = db.connect(path)
    whole = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM v_transactions WHERE month = ? AND category = ? AND "
                         "transaction_id NOT IN (SELECT transaction_id FROM transaction_splits)", (month, category)).fetchone()[0]
    lines = conn.execute("SELECT COALESCE(SUM(s.amount), 0) FROM transaction_splits s JOIN transactions t USING (transaction_id) "
                         "JOIN categories c ON c.id = s.category_id WHERE substr(t.date, 1, 7) = ? AND c.name = ?",
                         (month, category)).fetchone()[0]
    conn.close()
    return round(whole + lines, 2)


def test_the_rollover_switch_in_the_cap_editor(site, chrome):
    # (baseline K6) last month's figures from the DB, whatever the day (on Sept 30: "+$64.44 from Aug"): Eating Out
    # under its $250 cap, so what's left rolls in; Other Transport over its $600, so nothing does
    prev = TODAY.replace(day=1) - timedelta(days=1)
    carry, transport = round(250 - last_month_spend(site.path, "Eating Out"), 2), last_month_spend(site.path, "Other Transport")
    assert carry > 0 and transport > 600, (carry, transport)
    go(chrome, site, "budgets")
    # P2A: a category is a table row; Planned (`.plan`) is the edit button and shows the cap in force, with a repeat icon
    # when something rolled in; the rollover and the bills due are in the row's Details
    cards = chrome.js(T + """[...document.querySelectorAll('#page tr.c')].map((c) => ({name: c.dataset.category, slot: T(c.querySelector('.plan')),
      aria: c.querySelector('.plan').getAttribute('aria-label'), icon: !!c.querySelector('.plan .roll svg'),
      rollAmt: c.dataset.rollover, hasDet: !!c.querySelector('[data-help]')}))""")
    eat = next(c for c in cards if c["name"] == "Eating Out")
    # 2026-10-02: the rollover is a DISCLOSURE in the Details panel, not a `.roll` pill under the figure and not a second
    # number glued onto the label. It is published as `data-rollover` so the value can be asserted whether or not the
    # panel is open. P2A: Planned reads the cap in force ($250 + what rolled in), the $250 you set is in its label.
    assert float(eat["rollAmt"]) == carry, (eat, carry)
    assert eat["slot"] == f"${250 + carry:,.0f}", (eat, carry)
    assert eat["icon"] and "$250 set and" in eat["aria"], (eat, carry)
    assert "rolled over" not in eat["slot"], (eat, carry)
    assert eat["hasDet"], (eat, "a row with a rollover carries a Details disclosure")
    assert float(next(c for c in cards if c["name"] == "Other Transport")["rollAmt"]) == 0.0
    chrome.js("(() => { const c = [...document.querySelectorAll('#page tr.c')].find((x) => x.dataset.category === 'Other Health'); "
              "c.querySelector('.plan').click(); return 1; })()")
    sw = chrome.js("(() => { const s = document.querySelector('#sheet .bx-roll .tgl'); return {checked: s.getAttribute('aria-checked'), role: s.getAttribute('role'), "
                   "text: document.querySelector('#sheet .bx-roll .grow').textContent}; })()")
    assert sw["checked"] == "false" and sw["role"] == "switch" and sw["text"].startswith("Roll over what’s left") and "Off" in sw["text"]
    chrome.js("document.querySelector('#sheet .bx-roll .tgl').click()")
    chrome.js(WAIT.format(ms=600))
    conn = db.connect(site.path)
    on = [r[0] for r in conn.execute("SELECT c.name FROM category_rollover r JOIN categories c ON c.id = r.category_id ORDER BY c.name")]
    conn.close()
    assert on == ["Eating Out", "Other Health", "Other Transport"]
    # the sheet stays open and shows the new state in place; then Cancel closes it
    assert chrome.js("document.querySelector('#sheet .bx-roll .tgl').getAttribute('aria-checked')") == "true"
    chrome.js("[...document.querySelectorAll('#sheet .bx-foot button')].find((x) => x.textContent === 'Cancel').click()")
    chrome.js(WAIT.format(ms=400))
    assert chrome.js("document.getElementById('modal').classList.contains('on')") is False
    # the toggle saved, and the card now discloses it: the header chip text plus `data-rollover` both move. The
    # disclosure's own wording lives in the Details panel, which this checks by opening it.
    health = chrome.js(T + """(() => { const c = [...document.querySelectorAll('#page tr.c')]
      .find((x) => x.dataset.category === 'Other Health');
      return {text: T(c), rollAmt: c.dataset.rollover}; })()""")
    assert health["rollAmt"] is not None, health
    chrome.js("(() => { const c = [...document.querySelectorAll('#page tr.c')]"
              ".find((x) => x.dataset.category === 'Other Health');"
              "c.querySelector('[data-help]').click(); return 1; })()")
    chrome.js(WAIT.format(ms=250))
    panel = chrome.js(T + "T(document.querySelector('.help-panel'))")
    assert "Rolled over" in panel or "rolled over" in panel, panel


def test_the_alpaca_row_and_its_connect_form(site, chrome):
    go(chrome, site, "investments")
    row = chrome.js(T + "T(document.querySelector('#page .apirow'))")
    assert row.startswith("AlpacaAPI") and "Live · read-only via API · 2 positions" in row and "Counted in net worth" in row
    assert "Replace key" in row and "Remove" in row
    go(chrome, site, "accounts")
    chrome.js(click("[data-add=home]"))
    chrome.js(click("#sheet .opt", "Alpaca"))
    form = chrome.js(T + "({title: T(document.querySelector('#sheet h2')), note: T(document.querySelector('#sheet .apnote')), "
                         "secret: document.getElementById('ap-secret').type})")
    assert form == {"title": "Connect Alpaca", "note": "The app only reads your account. Alpaca keys can also trade, so keep this key private.",
                    "secret": "password"}
    chrome.js(fill("#ap-key", "AKTESTKEY0000000001"))
    chrome.js(fill("#ap-secret", "sEcReT0000000000000000000000000000000000ab"))
    chrome.js(click("#sheet .row .btn", "Connect"))
    chrome.js(WAIT.format(ms=500))
    err = chrome.js(T + "T(document.querySelector('#sheet .ferr'))")
    assert "switched off" in err                                   # the server's kill switch (conftest): no call, no file
    assert not config.alpaca_key_path().exists()
    # the refused check is a 400 (the browser logs that one request); nothing else went wrong
    assert [p for p in chrome.problems() if "/api/alpaca/key" not in p] == []
