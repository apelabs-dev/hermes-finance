"""Connect a bank with no working Plaid keys: the message shows where the user is looking, in headless Chrome.

#msg is hidden while the set-up wizard or the Link sheet is open, so the refusal from /api/link_token used to vanish.
Plaid's script doesn't load headless (cdn.plaid.com is blocked), so a stub stands in for it. Synthetic data only.
"""

from __future__ import annotations

import time

import pytest

from cdp import Chrome, Server, find_chrome
from hermes_finance import config, db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
SAID = "Plaid isn't set up yet"
STUB = "window.Plaid = {create: () => { throw new Error('Plaid.create must not run'); }};"


def no_keys():
    raise config.ConfigError("missing PLAID_CLIENT_ID")


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


@pytest.fixture
def srv(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.env"   # fake keys, so the wizard enables its button; the client factory below still fails
    secrets.write_text("PLAID_CLIENT_ID=fake-client\nPLAID_SECRET=fake-secret\n")
    secrets.chmod(0o600)
    monkeypatch.setenv("FINANCE_SECRETS_FILE", str(secrets))
    path = tmp_path / "finance.db"
    db.connect(path).close()
    s = Server(path)
    s.app.client_factory = no_keys
    yield s
    s.close()


def open_page(b, srv, frag):
    b.size(1440, 900)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/{frag}")
    b.settle()
    b.js(STUB)


def wait_js(b, pick: str) -> None:
    """Click the first element `pick` finds (waiting up to 5 s for it)."""
    b.js("new Promise((res, rej) => { const t0 = Date.now(); const tick = () => { const e = (" + pick + ")();"
         " if (e) { e.click(); res(1); } else if (Date.now() - t0 > 5000) rej(new Error('missing control'));"
         " else setTimeout(tick, 50); }; tick(); })")


def button(scope: str, text: str) -> str:
    return f"() => [...document.querySelectorAll('{scope}')].find((x) => x.textContent.includes('{text}') && x.getClientRects().length)"


def visible_note(b) -> str:
    """The note's text once it's on screen (waits up to 5 s for the failed request)."""
    return b.js("new Promise((res) => { const t0 = Date.now(); const tick = () => { const n = document.querySelector('.linkerr');"
                " const r = n && n.getBoundingClientRect();"
                " if (n && r.width > 0 && r.height > 0 && getComputedStyle(n).display !== 'none') res(n.textContent);"
                " else if (Date.now() - t0 > 5000) res(''); else setTimeout(tick, 50); }; tick(); })")


def test_the_wizard_shows_the_message(browser, srv):
    open_page(browser, srv, "setup?step=5")
    wait_js(browser, button("#page .sheet button", "Connect a bank"))
    got = visible_note(browser)
    assert SAID in got


def test_the_link_sheet_shows_the_message(browser, srv):
    open_page(browser, srv, "accounts")
    wait_js(browser, "() => [...document.querySelectorAll('[data-add=\"home\"]')].find((x) => x.getClientRects().length)")
    wait_js(browser, button("#sheet .opt", "Link a bank or card"))
    wait_js(browser, button("#sheet button", "Continue with Plaid"))
    wait_js(browser, button("#sheet button", "Continue to Plaid"))
    assert SAID in visible_note(browser)
    assert browser.js("document.getElementById('modal').classList.contains('on')")   # the sheet stayed open


# --- the message doesn't outlive the Link sheet -------------------------------------------------------------------

def msg_state(b) -> dict:
    """The page banner (#msg): its text and whether it shows at all."""
    return b.js("(() => { const m = document.getElementById('msg'); const r = m.getBoundingClientRect();"
                " return {text: m.textContent.trim(), shown: getComputedStyle(m).display !== 'none' && r.height > 0}; })()")


def plaid_fails_in_the_link_sheet(b, srv) -> None:
    open_page(b, srv, "accounts")
    wait_js(b, "() => [...document.querySelectorAll('[data-add=\"home\"]')].find((x) => x.getClientRects().length)")
    wait_js(b, button("#sheet .opt", "Link a bank or card"))
    wait_js(b, button("#sheet button", "Continue with Plaid"))
    wait_js(b, button("#sheet button", "Continue to Plaid"))
    assert SAID in visible_note(b)


def test_closing_the_link_sheet_clears_the_message(browser, srv):
    plaid_fails_in_the_link_sheet(browser, srv)
    wait_js(browser, "() => document.querySelector('#sheet button[aria-label=\"Close\"]')")
    browser.settled()
    assert not browser.js("document.getElementById('modal').classList.contains('on')")
    assert msg_state(browser) == {"text": "", "shown": False}          # not pinned to the Accounts page
    assert browser.js("document.querySelectorAll('.linkerr').length") == 0


def test_the_message_is_not_under_the_simplefin_sheets_and_a_connect_leaves_none(browser, srv, monkeypatch):
    from test_browser_simplefin import PUBLIC, TOKEN, Bridge, payload_without_a_plaid_twin
    from hermes_finance import simplefin
    monkeypatch.setattr(simplefin, "urllib_transport", Bridge(200, payload_without_a_plaid_twin()))
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    plaid_fails_in_the_link_sheet(browser, srv)
    wait_js(browser, "() => document.querySelector('#sheet button[aria-label=\"Back\"]')")           # Plaid step -> Add account
    wait_js(browser, button("#sheet .opt", "Link a bank or card"))
    wait_js(browser, button("#sheet button", "Continue with SimpleFIN"))                              # the token sheet
    browser.js("new Promise((res, rej) => { const t0 = Date.now(); const tick = () => { if (document.querySelector('#sheet input')) res(1);"
               " else if (Date.now() - t0 > 5000) rej(new Error('no token field')); else setTimeout(tick, 50); }; tick(); })")
    assert msg_state(browser)["shown"] is False and browser.js("document.querySelectorAll('.linkerr').length") == 0
    browser.js("(() => { const i = document.querySelector('#sheet input'); i.value = " + repr(TOKEN) + ";"
               " i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()")
    wait_js(browser, button("#sheet .btn.w", "Connect"))                                              # the confirm sheet
    browser.js("new Promise((res, rej) => { const t0 = Date.now(); const tick = () => { if (document.querySelector('#sheet .sf-acct')) res(1);"
               " else if (Date.now() - t0 > 8000) rej(new Error('no confirm sheet')); else setTimeout(tick, 50); }; tick(); })")
    assert msg_state(browser)["shown"] is False and browser.js("document.querySelectorAll('.linkerr').length") == 0
    wait_js(browser, button("#sheet .btn.w", "Add "))
    browser.js("new Promise((res, rej) => { const t0 = Date.now(); const tick = () => {"
               " if (!document.getElementById('modal').classList.contains('on')) res(1);"
               " else if (Date.now() - t0 > 8000) rej(new Error('sheet still open')); else setTimeout(tick, 50); }; tick(); })")
    browser.settled()
    assert msg_state(browser) == {"text": "", "shown": False}
    assert not browser.js("document.body.textContent.includes(" + repr(SAID) + ")")
