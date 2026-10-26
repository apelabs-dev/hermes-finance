"""TASK-18 in headless Chrome (tests/cdp.py: https and plaid.com blocked) against throwaway loopback servers on synthetic
DBs: the Settings page (every card, a switch that saves, the theme that repaints charts and is kept by the server),
the currency and locale, first-run setup (the redirect, the keys step), importing a CSV through the Add account
sheet, and signing in with the built-in login (password, then a code from the authenticator)."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest

import gate_dbs
from cdp import READY, Chrome, Server, find_chrome
from hermes_finance import auth, db, prefs

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
FIX = Path(__file__).parent / "fixtures" / "import"


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


@pytest.fixture
def full(tmp_path):
    srv = Server(gate_dbs.build("full", tmp_path / "finance.db"))
    yield srv
    srv.close()


def goto(b, srv, page, width=1440):
    b.size(width, 900 if width > 500 else 844)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/{page}")
    b.settle()


def test_settings_shows_every_card_and_saves_as_it_changes(browser, full):
    goto(browser, full, "settings")
    titles = browser.js("[...document.querySelectorAll('#page .card h3 .ttl')].map((t) => t.textContent)")
    assert titles == ["Connections", "Access & security", "AI provider", "Connect other agents (MCP)", "Sync & alerts",
                      "Valuations", "Region", "Appearance", "Data", "Categorization rules"]   # (TASK-24: the rules card last)
    text = browser.js("document.getElementById('page').textContent")
    assert "Behind your proxy login" in text and "ow•••@example.com" in text and "owner@example.com" not in text
    assert "Needs Chrome with remote debugging on port 9222" in text and "free public pages, not official APIs" not in text
    assert "hfm_" in text and "Coming in the next update" in text
    # a switch saves on the spot
    browser.js("[...document.querySelectorAll('#page .srow')].find((r) => r.textContent.startsWith('A budget reaches 80%'))"
               ".querySelector('.tgl').click()")
    browser.js("new Promise((r) => setTimeout(r, 400))")
    conn = db.connect(full.app.db_path)
    assert prefs.get(conn, "alerts.cap_80") is False
    assert browser.problems() == []


def test_the_theme_follows_the_setting_charts_included(browser, full):
    goto(browser, full, "settings")
    browser.js("[...document.querySelectorAll('#page .seg button')].find((b) => b.textContent === 'Light').click()")
    browser.js("new Promise((r) => setTimeout(r, 500))")
    assert browser.js("document.documentElement.dataset.theme") == "light"
    conn = db.connect(full.app.db_path)
    assert prefs.get(conn, "appearance.theme") == "light"
    goto(browser, full, "overview")          # a fresh load: the server's choice, before the first paint
    got = browser.js("({theme: document.documentElement.dataset.theme, pref: document.documentElement.dataset.themePref,"
                     " ink: Chart.defaults.color, bg: getComputedStyle(document.body).backgroundColor,"
                     " logo: getComputedStyle(document.querySelector('.brand img')).boxShadow})")
    assert got["theme"] == got["pref"] == "light" and got["ink"] == "#5a6275" and got["bg"] == "rgb(243, 244, 247)"
    assert "rgba(12, 14, 19" in got["logo"]                                  # the icon keeps its dark tile, with a hairline
    browser.js("localStorage.removeItem('hf.theme')")


def test_currency_and_locale_format_every_amount(browser, full):
    conn = db.connect(full.app.db_path)
    prefs.save(conn, {"region.currency": "EUR", "region.locale": "de-DE"})
    goto(browser, full, "overview")
    side = browser.js("document.getElementById('snw').textContent")
    assert side.endswith("€") and "." in side and "$" not in side        # 1.508.632 € (de-DE, symbol after)
    big = browser.js("document.querySelector('#page .big').textContent")
    assert "€" in big and "," in big
    prefs.save(conn, {"region.currency": "USD", "region.locale": "en-US"})


def test_first_run_opens_setup_without_the_app_chrome(browser, tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_SECRETS_FILE", str(tmp_path / "keys" / "secrets.env"))
    path = tmp_path / "fresh" / "finance.db"
    db.connect(path).close()
    srv = Server(path)
    try:
        browser.size(1440, 900)
        browser.goto(f"{srv.url}/?t={time.time():.3f}")
        browser.settle()
        state = browser.js("({hash: location.hash, board: document.body.classList.contains('board'),"
                           " side: getComputedStyle(document.getElementById('side')).display,"
                           " title: document.querySelector('#page h2').textContent})")
        assert state == {"hash": "#/setup", "board": True, "side": "none", "title": "Welcome"}
        browser.js("(() => { const [a, b] = document.querySelectorAll('#page input'); a.value = 'clientid000001'; b.value = 'secret00000000000000001';"
                   " [...document.querySelectorAll('#page .btn')].find((x) => x.textContent === 'Continue').click(); return 1; })()")
        browser.js("new Promise((r) => { const t = () => location.hash === '#/setup?step=2' ? r(1) : setTimeout(t, 50); t(); })")
        browser.settle()
        assert browser.js("document.querySelector('#page h2').textContent") == "Who can open this app?"
        assert "PLAID_SECRET=secret00000000000000001" in Path(os.environ["FINANCE_SECRETS_FILE"]).read_text()
        assert browser.problems() == []
    finally:
        srv.close()


def test_import_a_csv_through_add_account(browser, full):
    goto(browser, full, "accounts")
    data = base64.b64encode((FIX / "everyday-export.csv").read_bytes()).decode()
    browser.js("document.querySelector('[data-add=home]').click()")
    browser.js("[...document.querySelectorAll('#sheet .opt')].find((o) => o.textContent.startsWith('Import a file')).click()")
    browser.js(f"""(() => {{ const bin = atob({json.dumps(data)}); const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
      const dt = new DataTransfer(); dt.items.add(new File([bytes], 'everyday-export.csv'));
      const i = document.querySelector('#sheet input[type=file]'); i.files = dt.files; i.dispatchEvent(new Event('change')); return 1; }})()""")
    browser.js("new Promise((r) => { const t = () => document.querySelector('#sheet table.map') ? r(1) : setTimeout(t, 50); t(); })")
    sheet = browser.js("({cols: [...document.querySelectorAll('#sheet .map th')].map((t) => t.textContent), rows: document.querySelectorAll('#sheet .map tbody tr').length,"
                       " go: [...document.querySelectorAll('#sheet .btn.w')].map((b) => b.textContent)})")
    assert sheet["cols"] == ["Posted Date", "Description", "Amount", "Account"] and sheet["rows"] == 6 and sheet["go"] == ["Import 37 rows"]
    browser.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
    browser.js("new Promise((r) => { const t = () => document.querySelector('#sheet .impdone') ? r(1) : setTimeout(t, 50); t(); })")
    assert browser.js("document.querySelector('#sheet .impdone b').textContent") == "37 new rows"
    conn = db.connect(full.app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 37
    assert browser.problems() == []


def test_signing_in_with_the_built_in_login(browser, tmp_path):
    path = gate_dbs.build("signin", tmp_path / "finance.db")
    srv = Server(path)
    try:
        conn = db.connect(path)
        secret = conn.execute("SELECT totp_secret FROM auth_users").fetchone()[0]
        conn.close()
        browser.size(390, 844)
        browser.goto(f"{srv.url}/?t={time.time():.3f}", ready=False)
        browser.js("new Promise((r) => { const t = () => document.querySelector('#page form') ? r(1) : setTimeout(t, 50); t(); })")
        assert browser.js("document.querySelector('#page h2').textContent") == "Sign in"
        browser.js(f"""(() => {{ document.querySelector('#page input[autocomplete=username]').value = {json.dumps(gate_dbs.SIGNIN_USER)};
          document.querySelector('#page input[type=password]').value = {json.dumps(gate_dbs.SIGNIN_PASSWORD)};
          document.querySelector('#page form').requestSubmit(); return 1; }})()""")
        browser.js("new Promise((r) => { const t = () => document.querySelector('#page .codeb') ? r(1) : setTimeout(t, 50); t(); })")
        code = auth.hotp(secret, auth.totp_step() + 1)
        browser.js(f"""(() => {{ const bs = [...document.querySelectorAll('#page .codeb input')]; bs.forEach((b, i) => {{ b.value = {json.dumps(code)}[i]; }});
          bs[5].dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()""")
        for _ in range(100):  # the page reloads itself, signed in, as the app
            time.sleep(0.1)
            try:
                if browser.js("!!document.getElementById('tabbar') && !!document.querySelector('#page > *')"):
                    break
            except RuntimeError:  # evaluated while the page was navigating
                continue
        browser.js(READY)
        assert browser.js("location.hash") == "#/overview" and browser.js("!!document.querySelector('meta[name=csrf-token]')")
        # the app works signed in: a write carries the CSRF token
        s = browser.js("fetch('/api/prefs', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token':"
                       " document.querySelector('meta[name=csrf-token]').content}, body: JSON.stringify({'sync.daily_time': '07:00'})}).then((r) => r.status)")
        assert s == 200
        assert browser.problems() == []
    finally:
        browser.call("Network.clearBrowserCookies")
        srv.close()


def test_phone_five_tabs_and_the_drawer_order_and_highlight(browser, full):
    """TASK-18 addendum: under 1024px the tab bar has Overview, Transactions, Budgets, Accounts (13px+, one line, whole),
    HOTFIX-1: and Settings; the drawer lists Overview, Transactions, Budgets, then Goals (HOTFIX-1: under Budgets, as in
    the desktop sidebar), then Accounts, Recurring, Investments, Loans & debt, with Settings at the bottom, and marks the
    page you're on even when it has no tab."""
    for width in (390, 768):
        for page in ("recurring", "investments", "debt", "goals", "settings"):
            goto(browser, full, page, width)
            got = browser.js("""(() => {
              const tabs = [...document.querySelectorAll('#tabbar a')].map((a) => ({t: a.textContent.trim(), fs: parseFloat(getComputedStyle(a.querySelector('span')).fontSize),
                one: a.querySelector('span').getClientRects().length === 1, whole: a.querySelector('span').scrollWidth <= a.querySelector('span').clientWidth}));
              const order = [...document.querySelectorAll('#nav a')].sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top).map((a) => a.dataset.pg);
              const current = [...document.querySelectorAll('#side [aria-current=page]')].map((a) => a.dataset.pg);
              return {tabs, order, current, gear: document.querySelector('#side .sfoot a').getBoundingClientRect().top >
                Math.max(...[...document.querySelectorAll('#nav a')].map((a) => a.getBoundingClientRect().top))};
            })()""")
            assert [t["t"] for t in got["tabs"]] == ["Overview", "Transactions", "Budgets", "Accounts", "Settings"]
            assert all(t["fs"] >= 13 and t["one"] and t["whole"] for t in got["tabs"]), got["tabs"]
            assert got["order"] == ["overview", "transactions", "budgets", "goals", "accounts", "recurring", "investments", "debt"]
            assert got["current"] == [page] and got["gear"]
