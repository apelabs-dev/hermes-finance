"""SimpleFIN in headless Chrome (tests/cdp.py; https and plaid.com blocked) on copies of the gate's "full" database:
the chooser inside Add account at 390 and 1280 in dark and light, the setup-token error, the confirm sheet, the Settings
row (connected and revoked) and the Bank connections tag. Screenshots go to .dispatch/shots/, never into the repo."""

from __future__ import annotations

import base64
import json
import shutil
import socket
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import db, secretstore, simplefin, simplefin_sync

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()
SHOTS = Path(__file__).resolve().parents[1] / ".dispatch" / "shots"
ACCESS = "https://bridge-user:bridge-pass@bridge.example/simplefin"
TOKEN = base64.urlsafe_b64encode(b"https://bridge.example/simplefin/claim/synthetic").decode().rstrip("=")
PUBLIC = lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]  # noqa: E731


def payload():
    return {"errlist": [], "connections": [{"conn_id": "CON-1", "name": "Ally Bank"}],
            "accounts": [{"id": "ACT-1", "name": "Ally Savings 8841", "conn_id": "CON-1", "currency": "USD",
                          "balance": "1150.25", "available-balance": "1150.25", "balance-date": 1794787200,
                          "transactions": [{"id": "T1", "posted": 1794700800, "amount": "-40.00",
                                            "description": "Grocery store", "transacted_at": 1794700800}]},
                         {"id": "ACT-2", "name": "Smartly Checking 5108", "conn_id": "CON-1", "currency": "USD",
                          "balance": "25951.11", "available-balance": "25951.11", "balance-date": 1794787200,
                          "transactions": []}]}


class Bridge:
    def __init__(self, claim_status=403):
        self.claim_status = claim_status

    def __call__(self, method, url, headers, body, timeout):
        if method == "POST":
            return simplefin.Response(self.claim_status, ACCESS.encode() if self.claim_status == 200
                                      else b"Forbidden (was it already claimed?)")
        return simplefin.Response(200, json.dumps(payload()).encode())


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    path = tmp_path_factory.mktemp("browser_simplefin_base") / "finance.db"
    gate_dbs.build("full", path, TODAY)
    return path


@pytest.fixture
def site(tmp_path, base, monkeypatch):
    monkeypatch.setenv("FINANCE_SECRETS_FILE", str(tmp_path / "secrets.env"))
    path = tmp_path / "finance.db"
    shutil.copy(base, path)
    srv = Server(path, today=TODAY)
    srv.path = path
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def go(b, srv, width: int, theme: str, page: str = "accounts") -> None:
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/{page}")
    b.js(f"window.HFTheme.set({json.dumps(theme)})")
    b.settle()
    assert b.js("document.documentElement.dataset.theme") == theme
    b.problems()


def shot(b, name: str) -> Path:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / name
    b.screenshot(path, full=True)
    return path


def open_chooser(b) -> None:
    b.js("""(() => { const e = [...document.querySelectorAll('[data-add="home"]')].find((x) => x.getClientRects().length);
      if (!e) throw new Error('no Add account control'); e.click(); return 1; })()""")
    b.js("""new Promise((res, rej) => { const t0 = Date.now(); const tick = () => {
      const e = [...document.querySelectorAll('#sheet .opt')].find((x) => x.textContent.includes('Link a bank or card'));
      if (e) { e.click(); res(1); } else if (Date.now() - t0 > 5000) rej(new Error('no Link a bank or card row'));
      else setTimeout(tick, 50); }; tick(); })""")
    b.js("""new Promise((res, rej) => { const t0 = Date.now(); const tick = () => {
      const e = document.querySelector('#sheet .ways'); if (e) res(1); else if (Date.now() - t0 > 5000)
      rej(new Error('no chooser')); else setTimeout(tick, 50); }; tick(); })""")
    b.settle()


def seed_connected(path: Path, revoked: bool = False) -> None:
    secretstore.write({secretstore.SIMPLEFIN: ACCESS})   # the access link only ever lives in the secrets file
    conn = db.connect(path)
    try:
        with conn:
            conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, products, "
                         "created_at, last_sync_at, provider) VALUES ('simplefin:CON-1', NULL, 'Ally Bank', '', ?, "
                         "'[\"transactions\"]', '2026-11-01T00:00:00Z', '2026-11-15T07:11:00Z', 'simplefin')",
                         ("login_required" if revoked else "active",))
            conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                         "available_balance, iso_currency, updated_at, conn_id) VALUES ('simplefin:CON-1:ACT-1', "
                         "'simplefin:CON-1', 'Ally Savings', '8841', 'depository', 'savings', 1150.25, 1150.25, 'USD', "
                         "?, 'CON-1')", ("2026-11-12T07:11:00Z" if revoked else "2026-11-15T07:11:00Z",))
            conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, "
                         "amount, pending, removed, first_seen_at, updated_at, source) VALUES ('simplefin:CON-1:T1', "
                         "'simplefin:CON-1:ACT-1', 'simplefin:CON-1', '2026-11-02', 'Grocery store', 'Grocery store', "
                         "40, 0, 0, '2026-11-15T07:11:00Z', '2026-11-15T07:11:00Z', 'simplefin')")
            if revoked:
                conn.execute("UPDATE items SET error_code = 'SIMPLEFIN_REVOKED', error_message = ? WHERE item_id = "
                             "'simplefin:CON-1'", (simplefin.REVOKED,))
        simplefin_sync._set(conn, simplefin_sync.LAST_SUCCESS_KEY, "2026-11-15T07:11:00Z")
        simplefin_sync._set(conn, simplefin_sync.COVERAGE_KEY, "2026-09-17")
        simplefin_sync._set(conn, simplefin_sync.DAILY_MINUTE_KEY, 7)
        with conn:
            for n in range(3):
                conn.execute("INSERT INTO simplefin_requests (at, day, trigger, outcome) VALUES ('x', ?, 'daily', 'ok')",
                             (date.today().isoformat(),))
    finally:
        conn.close()


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [390, 1280])
def test_the_chooser_at_both_widths_and_both_themes(chrome, site, theme, width):
    go(chrome, site, width, theme)
    open_chooser(chrome)
    got = chrome.js("""(() => { const T = (e) => (e ? e.textContent.replace(/\\s+/g, ' ').trim() : null);
      const sheet = document.getElementById('sheet');
      return {title: T(sheet.querySelector('h2')), lead: T(sheet.querySelector('p')),
        ways: [...sheet.querySelectorAll('.way')].map((w) => ({name: T(w.querySelector('.way-h b')),
          text: T(w.querySelector('.way-h .grow > span')), strengths: [...w.querySelectorAll('.way-pro li')].map(T),
          drawbacks: [...w.querySelectorAll('.way-con li')].map(T), go: T(w.querySelector('.btn.w')),
          logo: Boolean(w.querySelector('.ia img')) || T(w.querySelector('.ia'))})),
        slot: T(sheet.querySelector('.way-slot .slotm'))}; })()""")
    assert got["title"] == "Link a bank or card"
    assert got["lead"].startswith("Both end in the same place")
    assert [w["name"] for w in got["ways"]] == ["Plaid", "SimpleFIN"]
    assert got["ways"][0]["go"] == "Continue with Plaid" and got["ways"][1]["go"] == "Continue with SimpleFIN"
    assert "Covers most US banks" in got["ways"][0]["strengths"][0]
    assert "Bridge allows 24 requests a day" in got["ways"][1]["drawbacks"][1]
    assert got["ways"][0]["logo"] and got["ways"][1]["logo"]       # an initials tile until the vendor logo is cached
    assert "of 10 used" in got["slot"]
    path = shot(chrome, f"simplefin-chooser-{width}-{theme}.png")
    assert path.exists()


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_already_used_token_error(chrome, site, theme, monkeypatch):
    monkeypatch.setattr(simplefin, "urllib_transport", Bridge(403))
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    go(chrome, site, 390, theme)
    open_chooser(chrome)
    chrome.js("""(() => { const e = [...document.querySelectorAll('#sheet .btn.w')]
      .find((x) => x.textContent.includes('Continue with SimpleFIN')); e.click(); return 1; })()""")
    chrome.js("""new Promise((res, rej) => { const t0 = Date.now(); const tick = () => {
      const i = document.querySelector('#sheet input'); if (i) res(1); else if (Date.now() - t0 > 5000)
      rej(new Error('no token field')); else setTimeout(tick, 50); }; tick(); })""")
    chrome.js("""(() => { const i = document.querySelector('#sheet input'); i.value = %s;
      i.dispatchEvent(new Event('input', {bubbles: true}));
      [...document.querySelectorAll('#sheet .btn.w')].find((b) => b.textContent.trim() === 'Connect').click(); return 1; })()"""
              % json.dumps(TOKEN))
    chrome.js("""new Promise((res, rej) => { const t0 = Date.now(); const tick = () => {
      const e = document.querySelector('#sheet .formerr'); if (e && e.textContent.trim()) res(1);
      else if (Date.now() - t0 > 8000) rej(new Error('no error')); else setTimeout(tick, 50); }; tick(); })""")
    assert chrome.js("document.querySelector('#sheet .formerr').textContent.trim()") == \
        "This token was already used. Create a new one at SimpleFIN Bridge."
    assert chrome.js("document.querySelector('#sheet h2').textContent") == "Connect with SimpleFIN"
    assert "bridge-pass" not in chrome.js("document.body.innerHTML")
    path = shot(chrome, f"simplefin-token-error-390-{theme}.png")
    assert path.exists()


@pytest.mark.parametrize("width", [390, 1280])
def test_the_confirm_sheet(chrome, site, width, monkeypatch):
    # a Plaid account the second SimpleFIN row looks like (same last 4 and balance)
    conn = db.connect(site.path)
    try:
        with conn:
            conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                         "VALUES ('item_usb', 'ins_demo_usb', 'U.S. Bank', 'access-production-x', 'active', 'now')")
            conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance) "
                         "VALUES ('acc_usb', 'item_usb', 'Smartly Checking', '5108', 'depository', 'checking', 25951.11)")
    finally:
        conn.close()
    monkeypatch.setattr(simplefin, "urllib_transport", Bridge(200))
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    go(chrome, site, width, "dark")
    open_chooser(chrome)
    chrome.js("""(() => { const e = [...document.querySelectorAll('#sheet .btn.w')]
      .find((x) => x.textContent.includes('Continue with SimpleFIN')); e.click(); return 1; })()""")
    chrome.js("""new Promise((res, rej) => { const t0 = Date.now(); const tick = () => {
      const i = document.querySelector('#sheet input'); if (i) res(1); else if (Date.now() - t0 > 5000)
      rej(new Error('no token field')); else setTimeout(tick, 50); }; tick(); })""")
    chrome.js("""(() => { const i = document.querySelector('#sheet input'); i.value = %s;
      i.dispatchEvent(new Event('input', {bubbles: true}));
      [...document.querySelectorAll('#sheet .btn.w')].find((b) => b.textContent.trim() === 'Connect').click(); return 1; })()"""
              % json.dumps(TOKEN))
    chrome.js("""new Promise((res, rej) => { const t0 = Date.now(); const tick = () => {
      const e = document.querySelector('#sheet .sf-acct'); if (e) res(1); else if (Date.now() - t0 > 8000)
      rej(new Error('no confirm sheet')); else setTimeout(tick, 50); }; tick(); })""")
    got = chrome.js("""(() => { const T = (e) => (e ? e.textContent.replace(/\\s+/g, ' ').trim() : null);
      const s = document.getElementById('sheet');
      return {title: T(s.querySelector('h2')), sub: T(s.querySelector('.filec .grow > span')), banks: [...s.querySelectorAll('.gh')].map(T),
        rows: [...s.querySelectorAll('.sf-acct')].map((r) => ({name: T(r.querySelector('.nm')), meta: T(r.querySelector('.meta2')),
          tags: [...r.querySelectorAll('.tag')].map(T), options: [...r.querySelectorAll('select')].map((x) => [...x.options].map((o) => o.text)),
          add: T(s.querySelector('.btn.w'))}))}; })()""")
    assert got["title"] == "Confirm what it found"
    assert got["sub"] == "1 bank · 2 accounts found"
    assert got["banks"] == ["Ally Bank"]
    assert got["rows"][0]["name"] == "Ally Savings 8841"
    assert "looks like savings" in got["rows"][0]["meta"]
    assert "On Plaid" in got["rows"][1]["tags"] and "already synced through Plaid" in got["rows"][1]["meta"]
    assert "Skip" in got["rows"][1]["options"][0] and "Add as a second account" in got["rows"][1]["options"][0]
    assert got["rows"][0]["add"] == "Add 1 account"          # the duplicate starts skipped
    path = shot(chrome, f"simplefin-confirm-{width}-dark.png")
    assert path.exists()


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_settings_row_and_the_bank_connections_tag(chrome, site, theme):
    seed_connected(site.path)
    go(chrome, site, 1280, theme, page="settings")
    got = chrome.js("""(() => { const T = (e) => (e ? e.textContent.replace(/\\s+/g, ' ').trim() : null);
      const row = [...document.querySelectorAll('#page .srow')].find((r) => (T(r.querySelector('b')) || '').startsWith('SimpleFIN')
        && r.querySelector('.grow b').textContent.trim() === 'SimpleFIN');
      const budget = [...document.querySelectorAll('#page .srow')].find((r) => (T(r.querySelector('b')) || '').startsWith('SimpleFIN requests today'));
      return {row: T(row), budget: T(budget), buttons: row ? [...row.querySelectorAll('button')].map(T) : null}; })()""")
    assert "SimpleFIN" in got["row"] and "1 bank · 1 account" in got["row"] and "Connected" in got["row"]
    assert "Replace" in got["buttons"] and "Remove" in got["buttons"]
    assert "SimpleFIN requests today" in got["budget"] and "Bridge allows 24 a day" in got["budget"]
    assert "3/24" in got["budget"]
    path = shot(chrome, f"simplefin-settings-1280-{theme}.png")
    assert path.exists()
    go(chrome, site, 1280, theme, page="accounts")
    tag = chrome.js("""(() => { const T = (e) => (e ? e.textContent.replace(/\\s+/g, ' ').trim() : null);
      const r = [...document.querySelectorAll('#page .conn')].find((x) => x.textContent.includes('Ally Bank'));
      return r ? {text: T(r), tag: Boolean([...r.querySelectorAll('.tag')].find((t) => t.textContent.trim() === 'SimpleFIN')),
        update: Boolean([...r.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Update'))} : null; })()""")
    assert tag and tag["tag"] and tag["update"] and "Synced" in tag["text"]
    assert chrome.js("document.querySelector('#page').scrollWidth <= innerWidth + 1")
    shot(chrome, f"simplefin-connections-1280-{theme}.png")


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [390, 1280])
def test_the_needs_retry_row_offers_try_again(chrome, site, monkeypatch, theme, width):
    secretstore.write({secretstore.SIMPLEFIN: ACCESS})
    conn = db.connect(site.path)
    try:
        simplefin_sync._set(conn, simplefin_sync.DAILY_MINUTE_KEY, 7)
    finally:
        conn.close()
    monkeypatch.setattr(simplefin, "urllib_transport", Bridge(200))
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    go(chrome, site, width, theme, page="settings")
    got = chrome.js("""(() => { const T = (e) => (e ? e.textContent.replace(/\\s+/g, ' ').trim() : null);
      const row = [...document.querySelectorAll('#page .srow')].find((r) => r.querySelector('.grow b')
        && r.querySelector('.grow b').textContent.trim() === 'SimpleFIN');
      const warn = [...document.querySelectorAll('#page p.nc')].find((x) => T(x).startsWith('The access link is saved'));
      return {row: T(row), warn: T(warn), buttons: row ? [...row.querySelectorAll('button')].map(T) : null}; })()""")
    assert "Needs retry" in got["row"] and "Try again" in got["buttons"]
    assert got["warn"] and "no new token is needed" in got["warn"]
    path = shot(chrome, f"simplefin-needs-retry-{width}-{theme}.png")
    assert path.exists()
    chrome.js("""(() => { const row = [...document.querySelectorAll('#page .srow')].find((r) => r.querySelector('.grow b')
      && r.querySelector('.grow b').textContent.trim() === 'SimpleFIN');
      [...row.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Try again').click(); return 1; })()""")
    chrome.js("""new Promise((res, rej) => { const t0 = Date.now(); const tick = () => {
      const e = document.querySelector('#sheet .sf-acct'); if (e) res(1); else if (Date.now() - t0 > 8000)
      rej(new Error('no confirm sheet after Try again')); else setTimeout(tick, 50); }; tick(); })""")
    assert chrome.js("document.querySelector('#sheet h2').textContent") == "Confirm what it found"
    shot(chrome, f"simplefin-needs-retry-confirm-{width}-{theme}.png")


def test_the_revoked_settings_copy(chrome, site):
    seed_connected(site.path, revoked=True)
    go(chrome, site, 1280, "dark", page="settings")
    got = chrome.js("""(() => { const T = (e) => (e ? e.textContent.replace(/\\s+/g, ' ').trim() : null);
      const p = [...document.querySelectorAll('#page p.nc')].find((x) => T(x).startsWith('SimpleFIN stopped'));
      return {p: T(p)}; })()""")
    assert got["p"] == ("SimpleFIN stopped accepting this access link, so it was revoked or expired. Balances stay as of "
                        "Nov 12. Paste a new setup token to resume.")
    shot(chrome, "simplefin-revoked-settings-1280-dark.png")
    go(chrome, site, 1280, "dark", page="accounts")
    got = chrome.js("""(() => { const T = (e) => (e ? e.textContent.replace(/\\s+/g, ' ').trim() : null);
      const row = [...document.querySelectorAll('#page .conn')].find((x) => x.textContent.includes('Ally Bank'));
      return {row: row ? T(row) : null, fix: Boolean(document.querySelector('#page button.rc[data-fixbridge]'))}; })()""")
    assert got["row"] and "Bridge lost its login on Nov 12" in got["row"] and "Fix at Bridge" in got["row"]
    assert got["fix"]
    shot(chrome, "simplefin-revoked-connections-1280-dark.png")


def test_a_simplefin_investment_says_holdings_are_not_imported(chrome, site):
    seed_connected(site.path)
    conn = db.connect(site.path)
    try:
        with conn:
            conn.execute("UPDATE accounts SET type = 'investment', subtype = 'brokerage' WHERE account_id = "
                         "'simplefin:CON-1:ACT-1'")
    finally:
        conn.close()
    go(chrome, site, 1280, "dark", page="investments")
    got = chrome.js("""(() => { const T = (e) => (e ? e.textContent.replace(/\\s+/g, ' ').trim() : null);
      const row = [...document.querySelectorAll('#page .li')].find((x) => x.textContent.includes('Ally Savings'));
      return row ? {text: T(row), tag: [...row.querySelectorAll('.tag')].map(T)} : null; })()""")
    assert got and "holdings not imported" in got["tag"]
    assert "holdings not imported" in got["text"]
    shot(chrome, "simplefin-investment-badge-1280-dark.png")


def test_sync_now_reports_the_simplefin_provider(chrome, site, monkeypatch):
    seed_connected(site.path)
    conn = db.connect(site.path)   # the Plaid banks just synced: this test is about SimpleFIN's line
    try:
        with conn:
            conn.execute("INSERT INTO sync_log (item_id, started_at, finished_at, added) "
                         "SELECT item_id, ?, ?, 0 FROM items WHERE provider = 'plaid'", (db.now(), db.now()))
    finally:
        conn.close()
    monkeypatch.setattr(simplefin, "urllib_transport", Bridge(200))
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    go(chrome, site, 1280, "dark")
    chrome.js("document.getElementById('syncnow').click()")
    chrome.js("""new Promise((res, rej) => { const t0 = Date.now(); const tick = () => {
      const p = document.querySelector('#syncp'); if (p && !p.hidden && p.textContent.includes('SimpleFIN:')) res(1);
      else if (Date.now() - t0 > 10000) rej(new Error('no SimpleFIN line in the sync panel'));
      else setTimeout(tick, 100); }; tick(); })""")
    text = chrome.js("document.querySelector('#syncp').textContent")
    assert "SimpleFIN: +" in text
    shot(chrome, "simplefin-sync-panel-1280-dark.png")
