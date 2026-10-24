"""Update on healthy banks + the 'Already connected' list before Plaid opens (2026-10-06, MOCK-023 pick B).

Plaid creates (and counts) an Item the moment its login finishes, before the app's duplicate guard can refuse the
exchange. So the guard that saves a slot is the Link sheet: it lists every connected bank with Update (update-mode Link
on the existing Item, no slot) before 'Connect a different bank' opens Plaid at all.
"""
from __future__ import annotations

from datetime import date

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from hermes_finance import db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")

# Plaid's script never loads in tests: stub Link and record which link-token endpoint each button asks for.
STUB = """
window.__calls = [];
window.Plaid = {create: (o) => ({open: () => window.__calls.push(['open', o.token]), exit() {}, destroy() {}})};
const _fetch = window.fetch;
window.fetch = (url, opts) => {
  if (String(url).startsWith('/api/link_token')) {
    window.__calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);
    return Promise.resolve(new Response(JSON.stringify({link_token: 'link-test'}), {status: 200,
      headers: {'Content-Type': 'application/json'}}));
  }
  return _fetch(url, opts);
};
"""
SETTLE = "new Promise((r) => setTimeout(r, 400))"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("relink") / "finance.db"
    conn = db.connect(path)
    today = date(2026, 9, 26)
    demo_data.build(conn, today=today)
    conn.close()
    srv = Server(path, today=today)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        b.call("Page.addScriptToEvaluateOnNewDocument", source=STUB)
        yield b


def accounts(browser, site, width=1440, height=900):
    browser.size(width, height)
    browser.goto(f"{site.url}/#/accounts")
    browser.settle()
    browser.js(f"(async () => {{ await {SETTLE}; }})()")


ROWS = """[...document.querySelectorAll('#page .conn')].map((r) => ({name: r.querySelector('.grow b').textContent,
  btn: (r.querySelector('button[data-item]') || {}).textContent || null,
  item: (r.querySelector('button[data-item]') || {dataset: {}}).dataset.item || null}))"""


@pytest.mark.parametrize("width", [1440, 390])
def test_every_healthy_bank_gets_update_and_a_broken_one_keeps_reconnect(browser, site, width):
    accounts(browser, site, width)
    rows = {r["name"]: r for r in browser.js(ROWS)}
    assert rows["Nova"]["btn"] == "Update" and rows["Chase"]["btn"] == "Update"
    assert rows["U.S. Bank"]["btn"] == "Reconnect"
    # the button never pushes the row wider than the card (phone included)
    over = browser.js("""[...document.querySelectorAll('#page .conn')].filter((r) =>
      r.scrollWidth > r.clientWidth + 1 || r.querySelector('button').getBoundingClientRect().right >
      r.getBoundingClientRect().right + 1).length""")
    assert over == 0
    assert browser.problems() == []


def test_update_opens_update_mode_link_for_that_item_never_a_new_one(browser, site):
    accounts(browser, site)
    browser.js("window.__calls = []; [...document.querySelectorAll('#page .conn')].find((r) => "
               "r.querySelector('.grow b').textContent === 'Chase').querySelector('button[data-item]').click()")
    browser.js(f"(async () => {{ await {SETTLE}; }})()")
    calls = browser.js("window.__calls")
    assert calls[0] == ["/api/link_token/update", {"item_id": "demo_chase"}]
    assert ["open", "link-test"] in calls
    assert not any(c[0] == "/api/link_token" for c in calls)


OPEN_LINK_SHEET = """(async () => {
  document.querySelector('[data-add=home]').click();
  await new Promise((r) => setTimeout(r, 150));
  [...document.querySelectorAll('#sheet button')].find((b) => /Link a bank or card/.test(b.textContent)).click();
  await new Promise((r) => setTimeout(r, 150));
})()"""


@pytest.mark.parametrize("width", [1440, 390])
def test_link_sheet_lists_connected_banks_before_plaid_opens(browser, site, width):
    accounts(browser, site, width)
    browser.js("window.__calls = []")
    browser.js(OPEN_LINK_SHEET)
    sheet = browser.js("document.querySelector('#sheet').textContent")
    assert "Already connected" in sheet
    names = browser.js("[...document.querySelectorAll('#sheet .duplist .conn .grow b')].map((b) => b.textContent)")
    assert sorted(names) == ["Chase", "Nova", "U.S. Bank"]
    assert browser.js("[...document.querySelectorAll('#sheet .duplist button')].every((b) => b.textContent === 'Update')")
    go = browser.js("(() => { const b = [...document.querySelectorAll('#sheet button')].find((x) => "
                    "x.textContent === 'Connect a different bank'); return b && !b.disabled; })()")
    assert go is True
    assert browser.js("window.__calls") == []  # merely opening the sheet never touches Plaid
    over = browser.js("(() => { const s = document.querySelector('#sheet'); return s.scrollWidth > s.clientWidth + 1; })()")
    assert over is False
    assert browser.problems() == []


def test_update_in_the_sheet_reuses_the_item(browser, site):
    accounts(browser, site)
    browser.js("window.__calls = []")
    browser.js(OPEN_LINK_SHEET)
    browser.js("[...document.querySelectorAll('#sheet .duplist .conn')].find((r) => "
               "r.querySelector('.grow b').textContent === 'Nova').querySelector('button').click()")
    browser.js(f"(async () => {{ await {SETTLE}; }})()")
    calls = browser.js("window.__calls")
    assert calls[0] == ["/api/link_token/update", {"item_id": "demo_nova"}]
    assert not any(c[0] == "/api/link_token" for c in calls)
    assert browser.js("document.querySelector('#modal').classList.contains('on')") is False


def test_connect_a_different_bank_is_the_only_way_to_a_new_item(browser, site):
    accounts(browser, site)
    browser.js("window.__calls = []")
    browser.js(OPEN_LINK_SHEET)
    browser.js("[...document.querySelectorAll('#sheet button')].find((b) => b.textContent === 'Connect a different bank').click()")
    browser.js(f"(async () => {{ await {SETTLE}; await {SETTLE}; }})()")
    calls = browser.js("window.__calls")
    assert calls and calls[0][0] == "/api/link_token"
