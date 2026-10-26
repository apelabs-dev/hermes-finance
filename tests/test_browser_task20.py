"""TASK-20 in headless Chrome (tests/cdp.py: https and plaid.com blocked) on throwaway loopback servers: after the merge
Goals and the bills calendar are reachable from the phone drawer and the desktop sidebar, every extension file runs once
(the Add account chooser lists both batches' rows once each), the desktop calendar chips show the merchant's logo like
the transaction rows, the first-run "Add Alpaca?" step (Skip picked; a key checked once, read-only, through alpaca.py),
and account names: never a U+FFFD, and the ••last-4 never cut or pushed out."""

from __future__ import annotations

import json
import sqlite3
import stat
import time

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import alpaca, db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


@pytest.fixture(scope="module")
def features(tmp_path_factory):
    srv = Server(gate_dbs.build("features", tmp_path_factory.mktemp("f20") / "finance.db"))
    yield srv
    srv.close()


def goto(b, srv, page, width=1440):
    b.size(width, 900 if width > 500 else 844, width < 500)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/{page}")
    b.settle()


def test_goals_and_the_bills_calendar_are_reachable_from_the_sidebar_and_the_phone_drawer(browser, features):
    goto(browser, features, "overview", 1280)
    side = browser.js("""[...document.querySelectorAll('#nav a')].filter((a) => a.getClientRects().length)
      .map((a) => a.dataset.pg)""")
    assert side == ["overview", "transactions", "budgets", "goals", "recurring", "accounts", "investments", "debt"]
    browser.js("document.querySelector('#nav a[data-pg=goals]').click()")
    browser.js("new Promise((r) => setTimeout(r, 500))")
    assert browser.js("location.hash") == "#/goals" and browser.js("Boolean(document.querySelector('#page .card.goal'))")
    browser.js("document.querySelector('#nav a[data-pg=recurring]').click()")
    browser.js("new Promise((r) => setTimeout(r, 700))")
    assert browser.js("Boolean(document.querySelector('#page .calcard .calg'))")      # the grid on a desktop
    for page, probe in (("goals", "#page .card.goal"), ("recurring", "#page .calcard .agenda")):
        goto(browser, features, "overview", 390)
        browser.js("document.getElementById('menu').click()")
        # until the drawer is open (a slow CI runner hadn't finished its slide-in at a fixed 400 ms: visible False)
        browser.js("""new Promise((r) => { const t0 = Date.now(); (function look() {
          if (getComputedStyle(document.getElementById('side')).visibility === 'visible' || Date.now() - t0 > 8000) r();
          else setTimeout(look, 50); })(); })""")
        link = browser.js(f"""(() => {{ const a = document.querySelector('#side #nav a[data-pg={page}]'); const r = a.getBoundingClientRect();
          return {{visible: getComputedStyle(document.getElementById('side')).visibility === 'visible', w: r.width, h: r.height,
                   tab: Boolean(document.querySelector('#tabbar a[data-pg={page}]'))}}; }})()""")
        assert link["visible"] and link["w"] > 100 and link["h"] >= 40 and not link["tab"], link   # in the drawer, not a tab
        browser.js(f"document.querySelector('#side #nav a[data-pg={page}]').click()")
        browser.js("new Promise((r) => setTimeout(r, 700))")
        assert browser.js("location.hash") == f"#/{page}" and browser.js(f"Boolean(document.querySelector('{probe}'))"), page
    assert browser.problems() == []


def test_every_extension_runs_once(browser, features):
    """Both batches plug into app.js through window.HF_EXT; each file's function runs once (twice, the chooser would
    list Import a file and Alpaca twice)."""
    goto(browser, features, "accounts", 1280)
    browser.js("document.querySelector('[data-add=home]').click()")
    browser.js("new Promise((r) => setTimeout(r, 300))")
    rows = browser.js("[...document.querySelectorAll('#sheet .opt b')].map((b) => b.textContent)")
    assert rows == ["Link a bank or card", "Add a property", "Add a vehicle", "Add manually", "Import a file", "Alpaca (API key)"]
    # settings, setup, import + goals, txmeta, billcal, rollover, alpaca + (TASK-21) bills + (TASK-23) chat
    # + (polish P0) catpick + (polish P1) txfilter + (polish P2b) wherewent + (polish P2A) budgets
    assert browser.js("window.HF_EXT.length") == 14
    assert browser.problems() == []


# Recorded as each chip's picture is set: the test browser blocks every https request, so a logo that fails to load is
# removed again (the avatar falls back to its initials) before the page could be read.
RECORD_IMGS = """window.__chipImgs = []; new MutationObserver((ms) => { for (const m of ms) for (const n of m.addedNodes) {
  if (n.nodeType !== 1) continue;
  for (const chip of (n.matches('.bchip') ? [n] : [...n.querySelectorAll('.bchip')])) {
    const img = chip.querySelector('.av img');
    window.__chipImgs.push([chip.querySelector('.bct b').textContent, img ? img.getAttribute('src') : null,
      chip.querySelector('.av').firstChild.nodeType === 3 ? chip.querySelector('.av').firstChild.textContent : null]);
  } } }).observe(document, {childList: true, subtree: true});"""


def test_desktop_calendar_chips_show_the_merchant_logo_like_the_transaction_rows(features):
    with Chrome() as b:
        b.call("Page.addScriptToEvaluateOnNewDocument", source=RECORD_IMGS)
        goto(b, features, "recurring", 1440)
        b.js("new Promise((r) => setTimeout(r, 800))")
        cal = json.loads(b.js("fetch('/api/bills-calendar').then((r) => r.text())"))
        got = {name: (src, text) for name, src, text in b.js("window.__chipImgs")}
        assert b.js("document.querySelectorAll('#page .calg .bchip .av').length") == len(cal["bills"]) > 5
        kinds = set()
        for bill in cal["bills"]:
            src, text = got[bill["merchant"]]
            want = bill.get("logo_url") or bill.get("icon_url")    # the transaction rows' order: the logo, else the icon
            assert src == want, (bill["merchant"], src, want)
            assert text and 1 <= len(text) <= 2                   # the initials under it, shown without a picture
            kinds.add("logo" if bill.get("logo_url") else "icon" if want else "initials")
        assert kinds == {"logo", "icon", "initials"}
        # the same merchant's transaction row asks for the same picture
        tx = json.loads(b.js("fetch('/api/transactions?kind=all&limit=500').then((r) => r.text())"))
        logo = next(bl for bl in cal["bills"] if bl.get("logo_url"))
        assert logo["logo_url"] in {t["logo_url"] for t in tx["rows"] if t["merchant"] == logo["merchant"]}
        assert b.problems() == []


ACCOUNT = {"status": "ACTIVE", "cash": "125.34", "equity": "1230.00", "currency": "USD", "account_number": "PA0012344321"}
POSITIONS = [{"symbol": "AAPL", "qty": "3", "current_price": "227.52", "market_value": "682.56", "cost_basis": "601.20",
              "asset_class": "us_equity"},
             {"symbol": "VTI", "qty": "1.5", "current_price": "281.40", "market_value": "422.10", "cost_basis": "400.00",
              "asset_class": "us_equity"}]


def test_setup_add_alpaca_step_skip_is_the_default_and_a_key_is_checked_once(tmp_path, monkeypatch):
    keyfile = tmp_path / "cfg" / "alpaca-live.env"
    monkeypatch.setenv("HERMES_ALPACA_KEY_FILE", str(keyfile))
    monkeypatch.delenv("HERMES_ALPACA", raising=False)
    calls = []

    def fake(host, target, headers):                      # the server runs in this process: its one transport
        calls.append(target)
        return 200, json.dumps(ACCOUNT if target == "/v2/account" else POSITIONS).encode()
    monkeypatch.setattr(alpaca, "transport", fake)
    monkeypatch.setattr(alpaca, "_store_logo", lambda conn, fetch=None: None)
    path = tmp_path / "finance.db"
    db.connect(path).close()                               # a new install: first-run setup is due
    srv = Server(path)
    try:
        with Chrome() as b:
            for width in (390, 1280):
                goto(b, srv, "setup?step=4", width)
                st = b.js("""(() => ({title: document.querySelector('#page h2').textContent,
                  choices: [...document.querySelectorAll('#page .choice')].map((c) => [c.querySelector('b').textContent, c.getAttribute('aria-checked')]),
                  focused: document.activeElement.classList.contains('choice') && document.activeElement.textContent.startsWith('Skip'),
                  inputs: document.querySelectorAll('#page input').length}))()""")
                assert st == {"title": "Add Alpaca?", "choices": [["Skip", "true"], ["Add my Alpaca key", "false"]],
                              "focused": True, "inputs": 0}, st
            b.js("[...document.querySelectorAll('#page .btn.w')].find((x) => x.textContent === 'Continue').click()")
            b.js("new Promise((r) => setTimeout(r, 500))")
            assert b.js("location.hash") == "#/setup?step=5" and calls == [] and not keyfile.exists()   # skipped: nothing asked
            goto(b, srv, "setup?step=4", 390)
            b.js("document.querySelectorAll('#page .choice')[1].click()")
            b.js("new Promise((r) => setTimeout(r, 200))")
            fill = """((k, s) => { const [id, sec] = document.querySelectorAll('#page input'); id.value = k; sec.value = s;
              [...document.querySelectorAll('#page .btn.w')].find((x) => x.textContent === 'Check and add').click(); return 1; })"""
            b.js(f"{fill}('PKPAPERKEY0001', 's3cretS3cretS3cretS3cret0001')")             # a paper key: refused, no call
            b.js("new Promise((r) => setTimeout(r, 500))")
            assert "paper-trading key" in b.js("document.querySelector('#page .formerr').textContent")
            assert calls == [] and not keyfile.exists()
            assert [p for p in b.problems() if "status of 400" not in p or "/api/alpaca/key" not in p] == []  # the refusal only
            b.js(f"{fill}('AKLIVEKEY0001', 's3cretS3cretS3cretS3cret0001')")
            b.js("new Promise((r) => setTimeout(r, 800))")
            text = b.js("document.getElementById('page').innerText")
            assert "Connected" in text and "Equity $1,230 · 2 positions" in text, text
            assert calls == ["/v2/account", "/v2/positions"]                               # one check, then the positions
            assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
            assert b.js("document.querySelectorAll('#page input').length") == 0            # the secret is gone from the page
            assert "s3cret" not in b.js("document.documentElement.outerHTML")
            b.js("[...document.querySelectorAll('#page .btn.w')].find((x) => x.textContent === 'Continue').click()")
            b.js("new Promise((r) => setTimeout(r, 400))")
            assert b.js("location.hash") == "#/setup?step=5"
            goto(b, srv, "setup?step=4", 390)                                              # back again: it says so
            assert "Alpaca is connected" in b.js("document.getElementById('page').innerText")
            assert b.problems() == []
    finally:
        srv.close()


def test_account_names_never_show_u_fffd_and_the_last_4_is_never_cut(tmp_path):
    path = gate_dbs.build("features", tmp_path / "finance.db")
    c = sqlite3.connect(path)
    c.execute("UPDATE accounts SET name = 'Nova�� Checking Everyday Premier Rewards' WHERE account_id = 'acc_nova_chk'")
    c.execute("UPDATE accounts SET name = 'VISA�� SIGNATURE REWARDS CARD' WHERE rowid = "
              "(SELECT min(rowid) FROM accounts WHERE type = 'credit')")
    c.execute("UPDATE accounts SET name = 'Self�-directed Investing Brokerage Account Long' WHERE rowid = "
              "(SELECT min(rowid) FROM accounts WHERE type = 'investment')")
    c.commit()
    c.close()
    srv = Server(path)
    # every .mk (a ••last-4) whole and inside its row, its card (or the sidebar / sheet / cell) and the window
    MK = """[...document.querySelectorAll('.nmrow > .mk')].filter((e) => e.getClientRects().length && getComputedStyle(e).visibility === 'visible')
      .map((e) => { const r = e.getBoundingClientRect(); const box = e.closest('.card, aside, .sheet, td, .panel').getBoundingClientRect();
        const row = e.parentElement.getBoundingClientRect();
        return {t: e.textContent, whole: e.scrollWidth <= e.clientWidth + 1, inside: r.left >= box.left - 0.5 && r.right <= box.right + 0.5
          && r.right <= row.right + 0.5 && r.right <= innerWidth + 0.5}; })"""
    FFFD = """(() => { const out = []; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (w.nextNode()) if (w.currentNode.data.includes('\\ufffd')) out.push(w.currentNode.data.slice(0, 60));
      for (const e of document.querySelectorAll('[title], [aria-label]')) for (const a of ['title', 'aria-label'])
        if ((e.getAttribute(a) || '').includes('\\ufffd')) out.push(a + ': ' + e.getAttribute(a).slice(0, 60));
      return out; })()"""
    try:
        with Chrome() as b:
            seen = set()
            for width in (1440, 1280, 390):
                for page in ("overview", "accounts", "investments", "goals", "transactions"):
                    goto(b, srv, page, width)
                    if width < 1024:
                        b.js("document.getElementById('menu').click()")                    # the sidebar is the drawer
                        b.js("new Promise((r) => setTimeout(r, 300))")
                    assert b.js(FFFD) == [], (page, width)
                    for m in b.js(MK):
                        assert m["whole"] and m["inside"], (page, width, m)
                        seen.add((page, m["t"]))
            # the sidebar, the Accounts card, the Investments rows, a goal's line and the Account column all show theirs
            assert {("overview", "••7385"), ("accounts", "••7385"), ("investments", "••6820"), ("goals", "••7385"),
                    ("transactions", "••7385")} <= seen, sorted(seen)
            goto(b, srv, "goals", 1280)                                                  # the "track from" picker
            b.js("document.querySelector('#page .gsum [data-add=goal], #page [data-template=save]').click()")
            b.js("new Promise((r) => setTimeout(r, 300))")
            b.js("[...document.querySelectorAll('#sheet .choice')].find((c) => c.textContent.startsWith('Part of an account')).click()")
            b.js("new Promise((r) => setTimeout(r, 300))")
            opts = b.js("[...document.querySelectorAll('#g-acct option')].map((o) => o.textContent)")
            assert any(o.startswith("Nova Checking Everyday Premier Rewards ••7385") for o in opts), opts
            assert not any("�" in o for o in opts) and b.js(FFFD) == []
            assert b.problems() == []
    finally:
        srv.close()
