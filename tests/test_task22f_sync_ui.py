"""TASK-22f: the TASK-22 build made to match the approved MOCK-022 (sketches/022-sync, NOTES.md), with the owner's five
decisions and the Allocation % fix. MOCK-022's own scene on synthetic data (tests/sync_scene.py: the 6:31 AM sync of 8
Plaid banks and Alpaca), served in this process with a fake Plaid (no network), in headless Chrome (tests/cdp.py).

  header     "● Synced …" then a labelled "Sync now" button (a phone: "Sync", 44px, the status under the title);
             Syncing…: disabled; HOTFIX-1: a light runs round its border (the page's one animation; still and lit with
             reduced motion); the status keeps the last good sync
  panel      what changed: counts, each balance before → after (largest first, the bank's logo and the account's type),
             the new spending with the rest counted, "Ask banks for fresh data"; desktop two columns under the button,
             phone a bottom sheet in the viewport with the footer below its scrolling body
  states     cooldown, Plaid out of reach, one bank to reconnect (Reconnect in the panel, the other 7's changes), fresh
             data asked of one bank recently / of every bank
  decisions  (a) the Loans & debt tile counts a payment in transit once; (b) the figure is the counted-once one, the line
             doesn't repeat it; (c) "Nothing new" closes by itself after 4 s, changes stay; (d) no per-bank "fresh data"
             button on Accounts; (e) "8 banks" for a sync, "9 connections" for health (Alpaca too), never "9 banks"
  settings   Trial / Paid and "Allow on-demand bank refresh" with the plan's line; Alpaca keeps its logo
  transit    the line under Own / Owe (sidebar, tiles, Accounts with where it left from) and the card's own row note
  allocation a negative "Unpriced / pending" residual shows its signed share (−0.7%), as audit_numbers expects
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import sync_scene
from cdp import NO_REDUCE, Chrome, Server, find_chrome
from hermes_finance import api, db, insights, prefs, syncnow
from hermes_finance.web import App

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_numbers  # noqa: E402

TODAY = date.today()
# The scene's morning, a quarter of an hour after its 6:31 sync (13:31:20Z, sync_scene.build): a run before 13:31Z on
# its day (midnight to 6:31 AM, Pacific) would otherwise sync before the scene's own last sync and see it in the page's
# future ("Synced just now")
MORNING = datetime(TODAY.year, TODAY.month, TODAY.day, 13, 45, tzinfo=timezone.utc)
JSONH = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
chrome_only = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
T = "((el) => (el ? el.textContent.replace(/\\s+/g, ' ').trim() : null))"
OPEN = ("new Promise((r, j) => { const t0 = Date.now(); const k = () => { const p = document.getElementById('syncp');"
        " if (p && !p.hidden && !document.getElementById('syncnow').disabled) r(1); else if (Date.now() - t0 > 15000)"
        " j(new Error('no panel')); else setTimeout(k, 40); }; k(); })")
HOLD = ("(() => { const f = window.fetch; let go; const held = new Promise((r) => { go = r; }); window.__go = go;"
        " window.fetch = (u, o) => (String(u).endsWith('/api/sync') ? held.then(() => f(u, o)) : f(u, o)); return 1; })()")
PANEL = f"""(() => {{ const T = {T}; const p = document.getElementById('syncp');
  const nm = (b) => (b ? [...b.childNodes].map((n) => n.textContent.trim()).filter(Boolean).join(' ') : null);   // "Chase ••3591"
  const rows = (label) => {{ const sec = [...p.querySelectorAll('.spsec')].find((s) => T(s.querySelector('.gh')) === label);
    return sec ? [...sec.querySelectorAll('.sprow')].map((r) => ({{name: nm(r.querySelector('.grow > b')), sub: T(r.querySelector('.grow > span')),
      right: nm(r.lastElementChild), logo: Boolean(r.querySelector('.ia.logo img, .av img')), initials: T(r.querySelector('.av'))}})) : null; }};
  return {{hidden: p.hidden, title: T(p.querySelector('#syncp-title')), sub: T(p.querySelector('.sphd .grow p')),
    tone: (p.querySelector('.spic') || {{className: ''}}).className, sums: [...p.querySelectorAll('.spsum')].map(T),
    moved: rows('Balances that moved'), spend: rows('New spending'), more: [...p.querySelectorAll('.spmore')].map(T),
    ask: p.querySelector('.spask') && {{text: T(p.querySelector('.spask')), disabled: p.querySelector('.spask').disabled}},
    hint: T(p.querySelector('.spfoot .hint')), waits: [...p.querySelectorAll('.spwait')].map(T),
    relog: [...p.querySelectorAll('.sprow.relog')].map((r) => ({{name: T(r.querySelector('.grow > b')), sub: T(r.querySelector('.grow > span')),
      button: T(r.querySelector('button')), item: (r.querySelector('button[data-item]') || {{dataset: {{}}}}).dataset.item || null}})),
    tall: p.classList.contains('tall'), text: T(p)}}; }})()"""


@pytest.fixture(scope="module")
def scene_dbs(tmp_path_factory):
    """The scene (before the 6:31 sync) and its day-before variant with the payment in transit, built once."""
    d = tmp_path_factory.mktemp("scene22f")
    return {"scene": sync_scene.build(d / "scene.db", TODAY), "transit": sync_scene.build(d / "transit.db", TODAY, transit=True)}


class Site:
    """A fresh copy of a scene DB, served in this process with the scene's fake Plaid."""

    def __init__(self, src: Path, tmp: Path, mode: str = "sync"):
        tmp.mkdir(parents=True, exist_ok=True)
        self.path = tmp / "finance.db"
        shutil.copyfile(src, self.path)
        self.conn = db.connect(self.path)
        self.fake = sync_scene.SceneFake(self.conn, TODAY, mode)
        self.srv = Server(self.path, today=TODAY)
        self.srv.app.client_factory = self.fake.client
        self.url = self.srv.url

    def close(self):
        self.srv.close()
        self.conn.close()


@pytest.fixture
def site(scene_dbs, tmp_path):
    made = []

    def make(kind="scene", mode="sync"):
        s = Site(scene_dbs[kind], tmp_path / f"s{len(made)}", mode)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


@pytest.fixture(scope="module")
def chrome():
    if find_chrome() is None:
        pytest.skip("Chrome is not installed")
    with Chrome() as b:
        yield b


# When #syncp opens and closes, stamped in the page (performance.now()), so a 4 s timer is measured from its own start.
# HOVER: dispatch the pointerenter in the same turn it opens (a microtask after the timer is set), as a pointer resting there.
WATCH_PANEL = """(() => {
  const p = document.getElementById('syncp');
  window.__sp = {open: null, close: null};
  if (window.__spObs) window.__spObs.disconnect();
  window.__spObs = new MutationObserver(() => {
    const t = performance.now();
    if (!p.hidden && __sp.open === null) { __sp.open = t; if (HOVER) p.dispatchEvent(new Event('pointerenter')); }
    else if (p.hidden && __sp.open !== null && __sp.close === null) __sp.close = t;
  });
  window.__spObs.observe(p, {attributes: true, attributeFilter: ['hidden']});
  return 1;
})()"""
OPEN_TO_CLOSE = """new Promise((r) => {   // ms it stayed open (waits up to 10 s for it to close)
  const t0 = performance.now();
  const f = () => (__sp.close !== null || performance.now() - t0 > 10000) ? r(__sp.close === null ? -1 : __sp.close - __sp.open) : setTimeout(f, 25);
  f();
})"""


def later(monkeypatch, seconds):
    at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    monkeypatch.setattr(syncnow, "clock", lambda: at)


def at_morning(monkeypatch):
    """The server's wall clock stopped at MORNING: what a sync stamps (items.last_sync_at, sync_log) and what its
    cooldowns and refresh waits count from."""
    assert MORNING.astimezone().date() == TODAY   # still the scene's day here
    stamp = MORNING.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr("hermes_finance.sync.now", lambda: stamp)
    monkeypatch.setattr(syncnow, "now", lambda: stamp)
    monkeypatch.setattr(syncnow, "clock", lambda: MORNING)


# the page's clock from an instant on (epoch ms), still ticking (test_browser_task14b's)
CLOCK = """(() => { const fixed = %d, t0 = Date.now(), Real = Date;
  class D extends Real { constructor(...a) { if (a.length) super(...a); else super(fixed + Real.now() - t0); }
    static now() { return fixed + Real.now() - t0; } }
  window.Date = D; })();"""


@pytest.fixture
def morning(chrome):
    """The page's clock at MORNING for one test (every page it loads); the module's other tests keep the real one."""
    assert MORNING.astimezone().date() == TODAY   # the browser's local day, like this process's
    ident = chrome.call("Page.addScriptToEvaluateOnNewDocument", source=CLOCK % round(MORNING.timestamp() * 1000))["identifier"]
    yield MORNING
    chrome.call("Page.removeScriptToEvaluateOnNewDocument", identifier=ident)


def go(b, s, page="overview", width=1440, theme=None):
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{s.url}/?t={time.time():.3f}#/{page}")
    if theme:
        b.js(f"window.HFTheme.set({theme!r})")
    b.settled(force=bool(theme))   # fetches, redraws and the theme's transition done (was a fixed 0.3 s; t_d2b88168)
    b.js("localStorage.removeItem('hf.theme')")


def sync(b):
    b.js("document.getElementById('syncnow').click()")
    b.js(OPEN)
    b.settle()
    assert b.still("#syncp") is True, "the panel's pop-in never finished"   # measured at its end, not mid pop-in
    return b.js(PANEL)


def post(app, body=None):
    status, _, out = app.handle("POST", "/api/sync", JSONH, json.dumps(body or {}).encode())
    return status, json.loads(out)


def app_on(path):
    a = App(path, lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


# --- the server's answer: what the panel draws -------------------------------------------------------------------------

def test_the_sync_answer_has_each_balance_before_and_after_and_the_new_spending(scene_dbs, tmp_path, monkeypatch):
    at_morning(monkeypatch)
    shutil.copyfile(scene_dbs["scene"], tmp_path / "finance.db")
    conn = db.connect(tmp_path / "finance.db")
    fake = sync_scene.SceneFake(conn, TODAY)
    a = app_on(tmp_path / "finance.db")
    a.client_factory = fake.client
    status, r = post(a)
    assert status == 200 and r["ok"] and (r["synced"], r["banks"]) == (8, 8)
    c = r["changes"]
    assert (c["new"], c["updated"], c["removed"]) == (12, 10, 0)
    # one row per account that moved, largest move first, with its bank's logo id, name, ••last 4 and type
    assert [m["account_id"] for m in c["balances"]] == sync_scene.MOVED
    chase = c["balances"][0]
    assert {k: chase[k] for k in ("label", "before", "after", "bank", "institution_id", "mask", "type", "subtype")} == {
        "label": "Chase ••3591", "before": 994.42, "after": 22.29, "bank": "Chase", "institution_id": "ins_56", "mask": "3591",
        "type": "credit", "subtype": "credit card"}
    # the new spending (money out, not a transfer), largest first; the 8 transfers and card payments only counted
    assert [(t["merchant_name"], t["amount"], t["bank"], t["mask"], t["pending"]) for t in c["new_spend"]] == [
        ("Zenith Financial Services", 465.10, "Nova", "7385", False), ("Costco", 269.14, "PayPal", None, False),
        ("Whole Foods", 10.09, "Chase", "3591", True), ("Xai", 5.00, "Wells Fargo", "1618", False)]
    assert c["new_more"] == {"spend": 0, "transfers": 8, "money_in": 0}
    assert r["previous_sync"] == f"{TODAY.isoformat()}T13:31:20Z" and r["last_sync"] > r["previous_sync"]
    assert r["unreachable"] is False and all(it["institution_id"] for it in r["items"])
    assert [b["item_id"] for b in r["refresh"]["banks"]] == [i for i, _, _ in sync_scene.BANKS]   # the Plaid banks only
    assert all(b["retry_after"] == 0 and b["asked_ago"] is None for b in r["refresh"]["banks"])
    assert (r["refresh"]["plan"], r["refresh"]["sandbox"]) == ("trial", False)
    conn.close()


def test_only_the_rows_this_sync_stored_are_new(scene_dbs, tmp_path):
    """A row stored a moment before the sync, in the same second (first_seen_at has whole seconds), isn't new."""
    shutil.copyfile(scene_dbs["scene"], tmp_path / "finance.db")
    conn = db.connect(tmp_path / "finance.db")
    with conn:
        conn.execute("""INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount,
                            pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at)
                        VALUES ('just_before', 'acc_nova_chk', 'demo_nova', ?, 'BIG STORE', 'Big Store', 999.0, 0,
                                'GENERAL_MERCHANDISE', 'GENERAL_MERCHANDISE_SUPERSTORES', 0, ?, ?)""",
                     (TODAY.isoformat(), db.now(), db.now()))
    fake = sync_scene.SceneFake(conn, TODAY)
    r = syncnow.run(conn, fake.client)
    assert [t["merchant_name"] for t in r["changes"]["new_spend"]] == sync_scene.SPEND
    conn.close()


def test_the_refresh_state_says_when_each_bank_can_be_asked_again(scene_dbs, tmp_path, monkeypatch):
    shutil.copyfile(scene_dbs["scene"], tmp_path / "finance.db")
    conn = db.connect(tmp_path / "finance.db")
    sync_scene.ask_log(conn, ["demo_chase"], 12)
    st = syncnow.refresh_state(conn)
    chase = next(b for b in st["banks"] if b["item_id"] == "demo_chase")
    assert 715 <= chase["asked_ago"] <= 725 and 1075 <= chase["retry_after"] <= 1085
    assert all(b["retry_after"] == 0 for b in st["banks"] if b["item_id"] != "demo_chase") and len(st["banks"]) == 8
    later(monkeypatch, 31 * 60)
    assert syncnow.refresh_state(conn)["banks"][2]["retry_after"] == 0   # (Chase is the third bank)
    conn.close()


def test_the_loans_and_debt_tile_counts_a_payment_in_transit_once(scene_dbs, tmp_path):
    """Decision (a): the tile says what net worth's Owe says, with the payment counted once; `reported` is the banks'."""
    shutil.copyfile(scene_dbs["transit"], tmp_path / "finance.db")
    conn = db.connect(tmp_path / "finance.db")
    ov = api.overview(conn, {}, TODAY)
    [e] = ov["net_worth"]["in_transit"]
    assert (e["amount"], e["label"], e["from_account"]["name"]) == (994.42, "Chase ••3591", "Nova Checking")
    nw = insights.net_worth(conn, history=False, today=TODAY)
    assert ov["debt"]["total"] == nw["debts"] == round(ov["debt"]["reported"] - 994.42, 2)
    assert [x["transaction_id"] for x in ov["debt"]["in_transit"]] == [e["transaction_id"]]
    conn.close()


@pytest.mark.parametrize("day", [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 26)])
def test_the_transit_scene_has_one_payment_on_every_day(tmp_path, day):
    """Screen 5 on both sides of the day the gate DB's own Chase card payment appears (the 2026-10-21 boundary): the
    fixture's monthly payment must not compete with the scene's one for the card's single owed balance, at either layer."""
    path = sync_scene.build(tmp_path / f"transit-{day}.db", day, transit=True)
    conn = db.connect(path)
    try:
        ov = api.overview(conn, {}, day)
        [e] = ov["net_worth"]["in_transit"]
        assert (e["transaction_id"], e["amount"], e["card"], e["mask"], e["label"], e["from_account"]["name"]) == (
            "s22_transit", 994.42, "CREDIT CARD", "3591", "Chase ••3591", "Nova Checking")
        nw = insights.net_worth(conn, history=False, today=day)
        assert [x["transaction_id"] for x in nw["in_transit"]] == ["s22_transit"]
        assert ov["debt"]["total"] == nw["debts"] == round(ov["debt"]["reported"] - 994.42, 2)
        assert [x["transaction_id"] for x in ov["debt"]["in_transit"]] == ["s22_transit"]
    finally:
        conn.close()


def test_health_counts_connections_with_alpaca_and_a_sync_counts_the_plaid_banks(scene_dbs, tmp_path):
    """Decision (e): 9 connections (8 Plaid banks + Alpaca) for health; the sync's 8 banks."""
    shutil.copyfile(scene_dbs["scene"], tmp_path / "finance.db")
    conn = db.connect(tmp_path / "finance.db")
    assert api.overview(conn, {}, TODAY)["connections"] == {"total": 9, "banks": 8, "broken": 0}
    conn.close()


def test_the_numbers_gate_reads_a_negative_share():
    """The page writes a negative share with U+2212; the gate's reader must keep its sign."""
    assert audit_numbers.pct_in("−0.7%") == -0.7
    assert audit_numbers.pct_in("-0.7%") == -0.7 and audit_numbers.pct_in("12.5%") == 12.5 and audit_numbers.pct_in("") is None


# --- in the browser ----------------------------------------------------------------------------------------------------

@chrome_only
def test_the_header_is_a_status_then_a_labelled_sync_now_button(chrome, site, morning):
    b, s = chrome, site()
    go(b, s)
    h = b.js(f"""(() => {{ const T = {T}; const btn = document.getElementById('syncnow'), r = btn.getBoundingClientRect();
      const st = document.getElementById('syncbox').getBoundingClientRect();
      return {{label: btn.innerText.trim(), aria: btn.getAttribute('aria-label'), h: r.height, status: T(document.getElementById('sync')),
        statusLeftOfButton: st.right <= r.left, statusShown: getComputedStyle(document.getElementById('syncbox')).display !== 'none',
        under: getComputedStyle(document.getElementById('syncst')).display, isButton: btn.tagName}}; }})()""")
    assert h["label"] == "Sync now" and h["aria"] == "Sync now" and h["isButton"] == "BUTTON" and h["h"] == 38
    assert re.fullmatch(r"Synced \d{1,2}:\d{2}\s?[AP]M", h["status"]) and h["statusShown"] and h["statusLeftOfButton"]
    assert h["under"] == "none"
    # a phone: "Sync", 44px tall, the status under the page title (the header's status hidden)
    go(b, s, width=390)
    p = b.js(f"""(() => {{ const T = {T}; const btn = document.getElementById('syncnow'), r = btn.getBoundingClientRect();
      const t = document.getElementById('title').getBoundingClientRect(), st = document.getElementById('syncst').getBoundingClientRect();
      return {{label: btn.innerText.trim(), h: r.height, right: r.right, under: T(document.getElementById('syncst')), below: st.top >= t.bottom - 1,
        box: getComputedStyle(document.getElementById('syncbox')).display}}; }})()""")
    assert p["label"] == "Sync" and p["h"] >= 44 and p["right"] <= 390 and p["box"] == "none"
    assert re.fullmatch(r"Synced \d{1,2}:\d{2}\s?[AP]M", p["under"]) and p["below"]
    # the light theme: the same button, on white
    go(b, s, theme="light")
    assert b.js("getComputedStyle(document.getElementById('syncnow')).backgroundColor") == "rgb(255, 255, 255)"
    assert b.problems() == []


# HOTFIX-1 (the owner's request, overriding TASK-22f's "nothing moves" for this one control): while a sync runs, a light
# runs round the Sync button's border (its ::before, the "syncring" animation), and that is the one animation on the page
# (P3a's colour transitions and finite mo-* entrance fades/pops are the motion tokens, not decoration: left out of the count)
MOVING = """(() => { const a = document.getAnimations().filter((x) => x instanceof CSSAnimation && !x.animationName.startsWith('mo-')).map((x) => ({name: x.animationName, id: x.effect.target.id,
  pseudo: x.effect.pseudoElement || null, running: x.playState === 'running'}));
  const others = [...document.querySelectorAll('body *')].filter((e) => !/^(none|mo-)/.test(getComputedStyle(e).animationName)).map((e) => e.id || e.className);
  const ring = getComputedStyle(document.getElementById('syncnow'), '::before');
  return {a, others, ring: {content: ring.content, bg: ring.backgroundColor, image: ring.backgroundImage.slice(0, 16),
    name: ring.animationName}}; })()"""


@chrome_only
def test_syncing_runs_a_light_round_the_border_and_nothing_else_moves(chrome, site):
    b, s = chrome, site()
    go(b, s)
    before = b.js("document.getElementById('sync').textContent")
    assert b.js(MOVING)["a"] == [] and b.js(MOVING)["ring"]["content"] == "none"          # idle: nothing moves
    b.js(HOLD)
    b.js("document.getElementById('syncnow').click()")
    b.settle()
    st = b.js(f"""(() => {{ const btn = document.getElementById('syncnow'), cs = getComputedStyle(btn);
      return {{label: btn.innerText.trim(), disabled: btn.disabled, busy: btn.hasAttribute('aria-busy'), border: cs.borderTopStyle,
        status: document.getElementById('sync').textContent}}; }})()""")
    assert st["label"] == "Syncing…" and st["disabled"] and st["busy"] and st["border"] == "solid"   # (no more dashes)
    assert st["status"] == before                                                        # the last good sync
    m = b.js(MOVING)
    assert m["a"] == [{"name": "syncring", "id": "syncnow", "pseudo": "::before", "running": True}]   # exactly this one
    assert m["others"] == [] and m["ring"]["image"].startswith("conic-gradient")         # no element itself animates
    b.js("window.__go()")
    b.js(OPEN)
    assert b.js("document.getElementById('syncnow').innerText.trim()") == "Sync now"
    assert not [a for a in b.js(MOVING)["a"] if a["name"] == "syncring"]                 # done: it stops (the page
    # reloads its data, whose loading skeletons shimmer a moment: TASK-18's, not the button's)
    b.js("document.querySelector('#syncp .spx').click()")
    b.settle()
    assert b.js("document.getElementById('sync').textContent") == "Synced just now"
    assert b.problems() == []


@chrome_only
@pytest.mark.parametrize("width, theme, ask", [(1440, "light", False), (390, "dark", False), (390, "light", True)])
def test_the_running_border_on_a_phone_in_the_light_theme_and_for_asking_banks(chrome, site, width, theme, ask, monkeypatch):
    b, s = chrome, site()
    if ask:   # the 6:31 sync already ran (2 minutes ago): Sync now, then "Ask banks for fresh data" is held
        sync_scene.presync(s.conn, s.fake)
        later(monkeypatch, 120)
    go(b, s, width=width, theme=theme)
    if ask:
        sync(b)
        b.js(HOLD)
        b.js("document.querySelector('#syncp .spask').click()")
    else:
        b.js(HOLD)
        b.js("document.getElementById('syncnow').click()")
    b.settle()
    label = b.js("document.getElementById('syncnow').innerText.trim()")
    assert label == ("Asking banks…" if ask else "Syncing…" if width > 1000 else "Syncing…")
    m = b.js(MOVING)
    assert m["a"] == [{"name": "syncring", "id": "syncnow", "pseudo": "::before", "running": True}] and m["others"] == []
    # the light is the theme's accent: a conic gradient ending in it
    acc = "rgb(53, 82, 212)" if theme == "light" else "rgb(142, 168, 255)"
    assert acc in b.js("getComputedStyle(document.getElementById('syncnow'), '::before').backgroundImage")
    b.js("window.__go()")
    b.js(OPEN)
    assert b.problems() == []


@chrome_only
def test_with_reduced_motion_the_border_is_lit_and_still(chrome, site):
    b = chrome
    try:
        b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
        for theme, acc in (("dark", "rgb(142, 168, 255)"), ("light", "rgb(53, 82, 212)")):
            go(b, site(), theme=theme)   # (a fresh site each: the minute between two syncs)
            b.js(HOLD)
            b.js("document.getElementById('syncnow').click()")
            b.settle()
            m = b.js(MOVING)
            assert m["a"] == [] and m["others"] == []                            # nothing moves
            assert m["ring"]["content"] == '""' and m["ring"]["bg"] == acc and m["ring"]["image"] == "none"   # all lit
            assert b.js("document.getElementById('syncnow').innerText.trim()") == "Syncing…"
            b.js("window.__go()")
            b.js(OPEN)
    finally:
        b.call("Emulation.setEmulatedMedia", features=NO_REDUCE)
    assert b.problems() == []


@chrome_only
def test_the_panel_says_what_changed_in_two_columns_under_the_button(chrome, site):
    b, s = chrome, site()
    go(b, s)
    p = sync(b)
    assert (p["title"], p["sub"], p["sums"]) == ("Synced just now", "All 8 banks", ["12 new transactions · 10 updated"])
    assert "spic ok" in p["tone"] and p["tall"]
    assert [(m["name"], m["sub"], m["right"]) for m in p["moved"]] == [
        ("Chase ••3591", "Credit card · owed", "$994.42 → $22.29"), ("Nova ••7731", "Savings", "$8,986.80 → $8,370.44"),
        ("U.S. Bank ••7266", "Credit card · owed", "$390.97 → $560.50"), ("Nova ••6820", "Investments", "$491.62 → $499.89"),
        ("Wells Fargo ••1618", "Credit card · owed", "$296.92 → $304.91")]
    assert all(m["logo"] for m in p["moved"])                           # each bank's own logo
    assert [(t["name"], t["sub"], t["right"]) for t in p["spend"]] == [
        ("Zenith Financial Services", "Nova ••7385", "$465.10"), ("Costco", "PayPal", "$269.14"),
        ("Whole Foods", "Chase ••3591 · pending", "$10.09"), ("Xai", "Wells Fargo ••1618", "$5")]
    assert [t["initials"] for t in p["spend"]] == ["Z", "C", "W", "X"]   # no logo_url: initials
    assert p["more"] == ["+ 8 transfers and card payments · See all in Transactions"]
    assert p["ask"] == {"text": "Ask banks for fresh data", "disabled": False}
    assert p["hint"] == "Banks can send new items hours late. This asks each bank to check now; free on your Trial plan."
    g = b.js("""(() => { const p = document.getElementById('syncp').getBoundingClientRect(), btn = document.getElementById('syncnow')
      .getBoundingClientRect(); const [a, c] = [...document.querySelectorAll('#syncp .spcols > .spsec')].map((x) => x.getBoundingClientRect());
      return {w: p.width, top: p.top - btn.bottom, right: Math.abs(p.right - btn.right), cols: c.left > a.right && Math.abs(c.top - a.top) < 2,
        bottom: p.bottom, vh: innerHeight}; })()""")
    assert abs(g["w"] - 760) <= 2 and 8 <= g["top"] <= 12 and g["right"] < 1 and g["cols"] and g["bottom"] <= g["vh"]
    assert b.js("document.activeElement === document.getElementById('syncp')")   # focus moved into it
    # it stays (changes): still open after the 4 s a "Nothing new" gets; Escape closes it, focus back on the button
    b.js("new Promise((r) => setTimeout(r, 4500))")
    assert b.js("document.getElementById('syncp').hidden") is False
    b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
    assert b.js("document.getElementById('syncp').hidden") is True
    assert b.js("document.activeElement === document.getElementById('syncnow')")
    assert b.js("document.getElementById('syncnow').getAttribute('aria-expanded')") == "false"
    assert b.problems() == []


@chrome_only
def test_on_a_phone_the_panel_is_a_bottom_sheet_with_its_footer_in_view(chrome, site):
    b, s = chrome, site()
    go(b, s, width=390)
    p = sync(b)
    assert p["title"] == "Synced just now" and len(p["moved"]) == 5 and len(p["spend"]) == 4
    g = b.js("""(() => { const p = document.getElementById('syncp'), r = p.getBoundingClientRect(), body = p.querySelector('.spbody');
      const f = p.querySelector('.spfoot').getBoundingClientRect(), sc = document.getElementById('syncscrim');
      const tab = document.getElementById('tabbar').getBoundingClientRect();
      return {left: r.left, right: r.right, bottom: r.bottom, top: r.top, vh: innerHeight, footBottom: f.bottom, footTop: f.top,
        scrolls: body.scrollHeight > body.clientHeight, scrim: getComputedStyle(sc).display, fixed: getComputedStyle(p).position,
        overTabs: Number(getComputedStyle(p).zIndex) > Number(getComputedStyle(document.getElementById('tabbar')).zIndex),
        stacked: [...p.querySelectorAll('.spcols > .spsec')].map((x) => Math.round(x.getBoundingClientRect().left)), tabTop: tab.top}; })()""")
    assert (g["left"], g["right"], g["fixed"], g["scrim"]) == (0, 390, "fixed", "block")
    assert abs(g["bottom"] - g["vh"]) < 1 and g["top"] >= 0 and g["footBottom"] <= g["vh"] and g["overTabs"]
    assert g["scrolls"] and len(set(g["stacked"])) == 1                # one column; the list scrolls, the footer doesn't
    b.js("(() => { const x = document.querySelector('#syncp .spbody'); x.scrollTop = x.scrollHeight; return 1; })()")
    assert b.js("document.querySelector('#syncp .spfoot').getBoundingClientRect().top") == g["footTop"]
    tap = b.js("""(() => [...document.querySelectorAll('#syncp .spx, #syncp .spask')].map((e) => Math.round(e.getBoundingClientRect().height)))()""")
    assert all(x >= 44 for x in tap)
    b.js("document.getElementById('syncscrim').click()")               # a tap on the dimmed page closes it
    assert b.js("document.getElementById('syncp').hidden") is True
    assert b.problems() == []


@chrome_only
def test_nothing_new_closes_by_itself_after_4_seconds(chrome, site, monkeypatch):
    """Decision (c); the pointer on it keeps it."""
    b, s = chrome, site()
    go(b, s)
    sync(b)
    b.js("document.querySelector('#syncp .spx').click()")
    later(monkeypatch, 120)
    b.js(WATCH_PANEL.replace("HOVER", "false"))
    p = sync(b)
    assert (p["title"], p["sub"]) == ("Synced just now", "All 8 banks")
    assert re.fullmatch(r"Nothing new since \d{1,2}:\d{2}\s?[AP]M\.", p["sums"][0]) and p["ask"]["text"] == "Ask banks for fresh data"
    shown = b.js(OPEN_TO_CLOSE)                     # timed in the page, from the moment it opened (not from here:
    assert 3950 <= shown < 6000, shown              # on a slow machine sync() itself takes a second or more)
    assert b.js("document.getElementById('syncp').hidden") is True        # gone, no animation
    later(monkeypatch, 240)
    b.js(WATCH_PANEL.replace("HOVER", "true"))      # the pointer lands on it the moment it opens
    sync(b)
    b.js("new Promise((r) => setTimeout(r, 4600))")
    assert b.js("window.__sp.open") is not None and b.js("window.__sp.close") is None
    assert b.js("document.getElementById('syncp').hidden") is False
    # "Ask banks for fresh data": the banks asked (Trial), and when they can be asked again
    later(monkeypatch, 250)
    b.js("document.querySelector('#syncp .spask').click()")
    b.js(OPEN)
    p = b.js(PANEL)
    assert (p["title"], p["sub"]) == ("Banks asked for fresh data", "All 8 banks")
    assert p["sums"] == ["New items usually land within a few minutes. Tap Sync again then."]
    assert p["waits"] and re.fullmatch(r"You can ask again in \d+ min", p["waits"][0])
    assert s.fake.calls.count(("/transactions/refresh", "demo_chase")) == 1
    assert b.problems() == []


@chrome_only
def test_the_other_outcomes_use_the_same_panel(chrome, site):
    b = chrome
    s = site()
    go(b, s)
    sync(b)
    b.js("document.querySelector('#syncp .spx').click()")
    p = sync(b)                                                          # tapped again within the minute
    assert re.fullmatch(r"Synced a moment ago\. Try again in \d+ s\.", b.js("document.getElementById('syncp-title').textContent"))
    assert "spic mut" in p["tone"] and not p["moved"] and not p["ask"]
    b.problems()   # (a real 429 logs "Failed to load resource": expected here)
    s = site(mode="unreachable")                                         # Plaid out of reach
    go(b, s)
    status = b.js("document.getElementById('sync').textContent")
    p = sync(b)
    assert (p["title"], p["sub"]) == ("Couldn’t reach Plaid", "Nothing changed; try again in a minute.") and "spic bad" in p["tone"]
    assert b.js("document.getElementById('sync').textContent") == status   # the header keeps the last good sync
    assert b.problems() == []
    s = site(mode="relogin")                                             # Wells Fargo needs a re-login
    go(b, s)
    p = sync(b)
    assert p["title"] == "Synced 7 of 8 banks" and "spic warn" in p["tone"]
    assert p["relog"] == [{"name": "Wells Fargo", "sub": "Needs you to sign in again", "button": "Reconnect", "item": "scene_wf"}]
    assert p["sums"] == ["From the other 7: 11 new transactions · 10 updated"]
    assert [m["name"] for m in p["moved"]] == ["Chase ••3591", "Nova ••7731", "U.S. Bank ••7266", "Nova ••6820"]
    b.js("new Promise((r) => setTimeout(r, 400))")
    head = b.js("({dot: document.getElementById('syncdot').className, title: document.getElementById('syncbox').title})")
    assert head == {"dot": "dot bad", "title": "1 connection needs attention"}
    assert b.problems() == []


@chrome_only
def test_fresh_data_waits_per_bank_and_for_every_bank(chrome, site, monkeypatch):
    b = chrome
    for asked, want in ((["demo_chase"], "one"), ([i for i, _, _ in sync_scene.BANKS], "all")):
        s = site()
        sync_scene.presync(s.conn, s.fake)
        later(monkeypatch, 120)
        sync_scene.ask_log(s.conn, asked, 12)                            # 12 min before the server's clock
        go(b, s)
        p = sync(b)
        assert re.fullmatch(r"Nothing new since \d{1,2}:31\s?[AP]M\.", p["sums"][0])   # (the 6:31 sync, local time)
        if want == "one":
            assert p["ask"] == {"text": "Ask 7 banks for fresh data", "disabled": False}
            assert p["waits"] == ["Chase · asked 12 min ago · available again in 18 min"]
            assert p["hint"] == "Free on your Trial plan."
        else:
            assert p["ask"] == {"text": "Ask banks for fresh data", "disabled": True}
            assert p["waits"] == ["Asked 12 min ago · available again in 18 min"] and p["hint"] is None
    assert b.problems() == []


@chrome_only
def test_settings_has_the_plan_and_the_refresh_switch_as_the_mock(chrome, site):
    b = chrome
    for plan, on, line in (("trial", "true", "Adds Refresh"),
                           ("paid", "false", "Plaid bills each refresh request on paid plans. Off unless you turn it on.")):
        s = site()
        prefs.save(s.conn, {"plaid.plan": plan}, allowed=list(prefs.SPEC))
        go(b, s, "settings")
        st = b.js(f"""(() => {{ const T = {T}; const row = (t) => [...document.querySelectorAll('#page .srow')].find((r) => T(r.querySelector('.grow > b')) === t);
          const pl = row('Plaid plan'), rf = row('Allow on-demand bank refresh'), al = row('Alpaca');
          return {{planSub: T(pl.querySelector('.grow > span')), segs: [...pl.querySelectorAll('.seg button')].map((x) => [T(x), x.getAttribute('aria-pressed')]),
            col: pl.classList.contains('col'), line: T(rf.querySelector('.grow > span')), on: rf.querySelector('[role=switch]').getAttribute('aria-checked'),
            links: [...rf.querySelectorAll('button:not([role=switch]), a')].length, alpacaLogo: Boolean(al.querySelector('.ia.logo img')),
            anywhere: [...document.querySelectorAll('#page button, #page a')].filter((x) => /fresh data/.test(x.textContent)).length,
            above: pl.compareDocumentPosition(al) & Node.DOCUMENT_POSITION_FOLLOWING}}; }})()""")
        assert st["planSub"] == (None if plan == "trial" else "Plaid bills each linked bank monthly on paid plans.")
        assert st["segs"] == [["Trial", str(plan == "trial").lower()], ["Paid", str(plan == "paid").lower()]] and not st["col"]
        assert (st["line"], st["on"], st["links"], st["anywhere"]) == (line, on, 0, 0)   # the action lives in the Sync panel
        assert st["alpacaLogo"] and st["above"]
    assert b.problems() == []


@chrome_only
def test_a_payment_in_transit_is_counted_once_and_says_so(chrome, site):
    b, s = chrome, site("transit")
    go(b, s)
    ov = b.js(f"""(() => {{ const T = {T}; const card = (t) => [...document.querySelectorAll('#page .card')].find((c) => T(c.querySelector('h3 .ttl')) === t);
      const nw = card('Net worth'), debt = card('Loans & debt');
      return {{side: [...document.querySelectorAll('#side .nwbox .trl')].map(T), tile: [...nw.querySelectorAll('.trl')].map(T),
        owe: T(nw.querySelector('.tl2')), debt: T(debt.querySelector('.big')), debtLine: [...debt.querySelectorAll('.trl')].map(T),
        icon: Boolean(nw.querySelector('.trl svg'))}}; }})()""")
    line = "$994.42 Chase payment in transit · counted once"
    assert ov["side"] == [line] and ov["tile"] == [line] and ov["debtLine"] == [line] and ov["icon"]
    owe = audit_numbers.monies(ov["owe"])[1]
    assert audit_numbers.money(ov["debt"]) == pytest.approx(owe, abs=0.5)   # (a) the tile agrees with Owe
    nw = insights.net_worth(s.conn, history=False, today=TODAY)
    go(b, s, "accounts")
    ac = b.js(f"""(() => {{ const T = {T}; const box = document.querySelector('#page .trbox');
      const row = document.querySelector('#page .li.acct[data-account=acc_chase_cc]');
      const sub = (() => {{ const hb = box.querySelector('.help-trigger'); if (!hb) return null; hb.click();
        const t = T(document.querySelector('.help-panel')); hb.click(); return t; }})();
      const fact = (sel) => {{ const hb = row.querySelector('.nmrow > .help-trigger'); if (!hb) return null; hb.click();
        const p = document.querySelector('.help-panel'), el = p && p.querySelector(sel); const t = el ? T(el) : null; hb.click(); return t; }};
      return {{line: T(box.querySelector('.trl')), sub, big: T(document.querySelector('#page .big')),
        name: T(row.querySelector('.nmrow > .nm')), meta: T(row.querySelector('.meta2')), mask: T(row.querySelector('.meta2 > .mt')),
        transit: row.querySelectorAll('.fx[data-transit]').length, from: fact('p[data-transit=s22_transit]'),
        amt: T(row.querySelector('.rt > .amt')),
        fresh: [...document.querySelectorAll('#page button, #page a')].filter((x) => /fresh data/i.test(x.textContent)).length}}; }})()""")
    day = TODAY - timedelta(days=1)
    assert ac["line"] == line
    assert ac["sub"] == (f"Left Nova Checking ••7385 on {day:%b} {day.day}; Chase ••3591 still shows it owed. Counted twice, "
                         f"net worth would read {audit_numbers.whole(nw['net'] - 994.42)}.")
    assert audit_numbers.money(ac["big"]) == nw["net"]                     # (b) the figure is the counted-once one
    assert "counted once" in ac["line"] and audit_numbers.whole(nw["net"]) not in ac["line"]   # the line doesn't repeat it
    assert ac["name"] == "CREDIT CARD" and ac["mask"].endswith("••3591")   # name line, then the mask on line two
    assert ac["meta"] == "$994.42 payment on its way · 15% used · ••3591"
    assert ac["transit"] == 1 and ac["meta"].count("$994.42") == 1        # counted once on the row
    assert ac["from"] == (f"$994.42 payment on its way from Nova Checking on {day:%b} {day.day}. "
                          "The bank still shows it owed; net worth counts this payment once.")   # what Chase reports
    assert ac["amt"] == "-$994.42"
    assert ac["fresh"] == 0                                              # (d) no per-bank "Ask for fresh data" on Accounts
    assert b.problems() == []


@chrome_only
def test_health_says_connections_and_a_sync_says_the_plaid_banks(chrome, site):
    """Decision (e): never "9 banks"."""
    b, s = chrome, site()
    go(b, s)
    text = b.js("document.body.innerText")
    assert "All 9 connections healthy" in text and "9 banks" not in text
    go(b, s, "accounts")
    card = b.js(f"""(() => {{ const T = {T}; return T([...document.querySelectorAll('#page .card')].find((c) => T(c.querySelector('h3 .ttl')) === 'Bank connections')); }})()""")
    assert "All 9 connections healthy" in card and "9 banks" not in b.js("document.body.innerText")
    assert sync(b)["sub"] == "All 8 banks"
    with s.conn:   # no manual account: the sidebar's note counts connections too
        s.conn.execute("UPDATE manual_accounts SET archived_at = '2026-01-01T00:00:00Z'")
    go(b, s)
    assert b.js("document.getElementById('nwn').textContent") == "9 connections"
    assert b.problems() == []


@chrome_only
def test_a_negative_unpriced_residual_shows_its_signed_share(chrome, site):
    """The Allocation list showed 0.0% (blank: the page hid a negative share) for the live DB's -$3.50 on $499.89; the
    numbers gate wants value / total, signed."""
    b, s = chrome, site()
    with s.conn:   # the account is worth $3.50 less than its holdings + cash (491.47): a negative residual
        s.conn.execute("UPDATE accounts SET current_balance = 487.97 WHERE account_id = 'acc_nova_inv'")
        s.conn.execute("UPDATE manual_accounts SET archived_at = '2026-01-01T00:00:00Z' WHERE kind = 'investment'")
    go(b, s, "investments")
    rows = b.js(f"""(() => {{ const T = {T}; const card = [...document.querySelectorAll('#page .card')].find((c) => T(c.querySelector('h3 .ttl')) === 'Allocation');
      return [...card.querySelectorAll('.lgr')].map((r) => ({{label: T(r.querySelector('.grow > b')),
      amt: T(r.querySelector('.amt')), pct: T(r.querySelector('.rt > span:last-child'))}})); }})()""")
    unp = [r for r in rows if r["label"] == "Unpriced / pending"]
    assert len(unp) == 1 and unp[0]["amt"] == "-$3.50"
    total = audit_numbers.money(b.js("document.querySelector('#page .big').textContent"))
    want = -3.50 / total * 100
    assert unp[0]["pct"] == f"−{abs(want):.1f}%" and audit_numbers.pct_in(unp[0]["pct"]) == pytest.approx(want, abs=0.051)
    assert all(r["pct"] and not r["pct"].startswith("−") for r in rows if r["label"] != "Unpriced / pending" and audit_numbers.money(r["amt"]))
    assert b.problems() == []
