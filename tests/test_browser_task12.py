"""The vehicle / property forms in headless Chrome (tests/cdp.py; https and plaid.com blocked) against a throwaway
loopback server (TASK-12, after TASK-13b): the car form with a VIN (the page's check digit, the decode filling the car,
the Carfax trade-in on save), "Your value" and "Use estimate again", and the note when a source is switched off. The
server runs in this process, so valuation.transport is conftest.FakeHttp and valuation.browser a FakeBrowser: no
request ever leaves the machine and the real Chrome is never reached."""

from __future__ import annotations

import json
import time
from datetime import date

import pytest

from cdp import Chrome, Server, find_chrome
from conftest import VIN, FakeBrowser, FakeHttp
from hermes_finance import db, valuation

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
WAIT = "new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 120))))"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("browser12") / "finance.db"
    db.connect(path).close()
    srv = Server(path)
    srv.db_path = path
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


@pytest.fixture
def http(monkeypatch, tmp_path):
    """Carfax and Redfin on, the fake providers (NHTSA over FakeHttp, Carfax in a FakeBrowser), for this test."""
    env = tmp_path / "sources.env"
    env.write_text("CARFAX_ENABLED=1\nREDFIN_ENABLED=1\n")
    monkeypatch.setenv("HERMES_ENV_FILE", str(env))
    f = FakeHttp()
    monkeypatch.setattr(valuation, "transport", f)
    b = FakeBrowser()
    monkeypatch.setattr(valuation, "browser", b)
    f.tabs = b
    return f


def click(sel, text=None):
    return (f"(() => {{ const el = [...document.querySelectorAll({json.dumps(sel)})].filter((e) => e.getClientRects().length)"
            f".find((e) => {json.dumps(text)} === null || e.textContent.trim().startsWith({json.dumps(text)}));"
            f" if (!el) throw new Error('nothing to click: ' + {json.dumps(sel)}); el.click(); return 1; }})()")


def fill(sel, value):
    return (f"(() => {{ const i = document.querySelector({json.dumps(sel)}); i.value = {json.dumps(value)};"
            f" i.dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()")


def until(browser, cond, timeout=6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        got = browser.js(cond)
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError(f"never true: {cond}")


def run(browser, *steps):
    for js in steps:
        browser.js(js)
        browser.js(WAIT)


def open_accounts(browser, site, width=1440):
    browser.size(width, 900)
    browser.goto(f"{site.url}/?t={time.time():.3f}#/accounts")
    browser.settle()
    browser.problems()


# Approved density mock D1 B: a car's row is a two-line .li.r2; its name link carries data-asset / data-kind and the
# href (no longer the row), line two is short ("Estimated value · updated today") and the estimate's own line
# ("Trade-in $X · Carfax · <date>") is the Details fact in the row's panel, read here the way a reader opens it.
ROW = ("(() => [...document.querySelectorAll('#page .li.r2 .account-open[data-kind=vehicle]')].map((a) => { "
       "const r = a.closest('.li'), T = (e) => e ? e.textContent.replace(/\\s+/g, ' ').trim() : null; "
       "const b = r.querySelector('.nmrow > .help-trigger'); b.click(); "
       "const dt = [...document.querySelectorAll('.help-panel .help-facts dt')].find((t) => T(t) === 'Details'); "
       "const sub = dt ? T(dt.nextElementSibling) : null; b.click(); "
       "return {id: Number(a.dataset.asset), href: a.getAttribute('href'), name: T(a.querySelector('.nm')), sub, "
       "line: T(r.querySelector('.meta2 > .mt')), amount: T(r.querySelector('.amt'))}; }))()")


def test_a_car_with_a_vin_is_filled_in_and_valued_on_save(site, browser, http):
    open_accounts(browser, site)
    run(browser, click("[data-add=home]"), click("#sheet .opt", "Add a vehicle"))
    note = browser.js("[...document.querySelectorAll('#sheet form > .hint')].map((h) => h.textContent).join(' | ')")
    assert note == ("Your VIN, mileage and ZIP code are sent to NHTSA (the free VIN lookup) and to Carfax (through your "
                    "Chrome) to get an estimate.")
    run(browser, fill("#f-vin", VIN.lower()), fill("#f-mileage", "120,000"), fill("#f-zip_code", "94110"))
    hints = browser.js("({value: document.querySelector('[data-key=balance] .hint:not([hidden])').textContent, "
                       "optional: [...document.querySelectorAll('[data-key=vehicle_make] label em, [data-key=vehicle_year] label em')]"
                       ".map((e) => !e.hidden)})")
    assert hints == {"value": "Leave blank to use the Carfax estimate.", "optional": [True, True]}
    run(browser, "document.querySelector('#sheet form').requestSubmit()")
    toast = until(browser, "document.querySelector('#toast.on') && document.querySelector('#toast').textContent")
    assert toast.startswith("Added 2003 Honda Accord. Trade-in $14,090 by Carfax (private sale $17,160, dealer retail $19,360).")
    # one row: approved density mock D2 B lists a manual item in its group card only ("Added by you" is gone)
    rows = until(browser, f"(() => {{ const r = {ROW}; return r.length >= 1 && r; }})()")
    assert [r["name"] for r in rows] == ["2003 Honda Accord"]
    for r in rows:
        assert r["amount"] == "$14,090" and r["sub"].startswith("Trade-in $14,090 · Carfax ·")
        assert r["line"].startswith("Estimated value · updated")
        assert r["href"] == f"#/asset/{r['id']}"                     # the row opens the car's page
    assert http.count("nhtsa") == 1 and len(http.tabs.opened) == 1
    assert json.loads(http.tabs.fetches[-1]["body"])["odometer"] == 120000
    assert browser.problems() == []


def test_a_bad_vin_is_caught_on_the_page_before_anything_is_sent(site, browser, http):
    open_accounts(browser, site, 390)
    run(browser, click("[data-add=home]"), click("#sheet .opt", "Add a vehicle"),
        fill("#f-vin", "1HGCM82643A004352"), "document.querySelector('#sheet form').requestSubmit()")
    err = until(browser, "(document.querySelector('[data-key=vin].bad .ferr') || {}).textContent")
    assert err == "That VIN doesn't check out (its 9th character, the check digit, doesn't match): look for a typo"
    # without a valid VIN the make, model and year are required again
    assert browser.js("document.querySelector('[data-key=vehicle_make] .ferr').textContent") == "Make is required"
    assert http.calls == [] and http.tabs.opened == [] and browser.problems() == []


def test_your_value_wins_until_use_estimate_again(site, browser, http):
    conn = db.connect(site.db_path)
    car = valuation.create(conn, {"kind": "vehicle", "vin": "1M8GDM9AXKP042788", "mileage": 50000, "zip_code": "94110",
                                  "vehicle_make": "Ford", "vehicle_model": "Transit", "vehicle_year": 2019},
                           date.today())
    conn.close()
    assert car["value_source"] == "estimate" and car["balance"] == 14090.0
    browser.size(1440, 900)
    browser.goto(f"{site.url}/?t={time.time():.3f}#/asset/{car['id']}")
    browser.settle()
    run(browser, click("#page .vd-actions .btn", "Edit"))
    until(browser, "document.querySelector('#sheet form') && 1")
    box = browser.js("document.querySelector('#sheet .valbox').textContent.replace(/\\s+/g, ' ')")
    assert box.startswith("Trade-in $14,090 · Carfax ·") and "Your net worth uses this estimate." in box
    run(browser, fill("#f-balance", "16,500"), "document.querySelector('#sheet form').requestSubmit()")
    until(browser, "document.querySelector('#toast.on') && 1")
    head = until(browser, "(() => { const s = document.querySelector('#page .vd-head .sub'); "
                          "return s && s.textContent.startsWith('Your value') && s.textContent; })()")
    assert "Carfax trade-in $14,090" in head
    run(browser, click("#page .vd-quiet .lnk", "Use the estimate"))
    toast = until(browser, "document.querySelector('#toast.on') && document.querySelector('#toast').textContent")
    assert toast.startswith("Now using the estimate: $14,090.")
    until(browser, "(() => { const s = document.querySelector('#page .vd-head .sub'); "
                   "return s && s.textContent.startsWith('Trade-in · Carfax'); })()")
    assert len(http.tabs.opened) == 1 and browser.problems() == []     # switching back never calls


def test_a_source_switched_off_is_said_quietly(site, browser, monkeypatch, tmp_path):
    env = tmp_path / "off.env"
    env.write_text("CARFAX_ENABLED=0\nREDFIN_ENABLED=0\n")
    monkeypatch.setenv("HERMES_ENV_FILE", str(env))
    open_accounts(browser, site)
    run(browser, click("[data-add=home]"), click("#sheet .opt", "Add a vehicle"), fill("#f-vin", VIN))
    note = browser.js("[...document.querySelectorAll('#sheet form > .hint')].map((h) => h.textContent).join(' | ')")
    assert note == ("Carfax is switched off (CARFAX_ENABLED=0 in the .env). The VIN is only sent to NHTSA (the free VIN "
                    "lookup) to fill in the car.")
    assert browser.js("document.querySelector('[data-key=balance] .hint').hidden") is True
    run(browser, click("#sheet .btn", "Back"), click("#sheet .opt", "Add a property"))
    note = browser.js("[...document.querySelectorAll('#sheet form > .hint')].map((h) => h.textContent).join(' | ')")
    assert note == "Redfin is switched off (REDFIN_ENABLED=0 in the .env)."
    assert browser.problems() == []
