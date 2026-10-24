"""TASK-13b's car / property page in headless Chrome (tests/cdp.py; https and plaid.com blocked) against a throwaway
loopback server on the gate's "full" DB (tests/gate_dbs.py: the Mesa with its cut-out photo and linked loan, the Fit
with the drawn silhouette, a condo with its listing photo, a cabin on a map, a unit with no estimate). The server runs in
this process: Carfax is a FakeBrowser, the photo fetch is recorded, never started (conftest)."""

from __future__ import annotations

import json
import time
from datetime import date

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from conftest import SCHEDULED, FakeBrowser, FakeHttp
from hermes_finance import db, manual, photos, valuation

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
WAIT = "new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 120))))"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("browser13b") / "finance.db"
    gate_dbs.build("full", path)
    srv = Server(path)
    srv.db_path = path
    conn = db.connect(path)
    srv.ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM manual_accounts WHERE kind IN ('vehicle', 'property')")}
    conn.close()
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


@pytest.fixture
def on(monkeypatch, tmp_path):
    env = tmp_path / "sources.env"
    env.write_text("CARFAX_ENABLED=1\nREDFIN_ENABLED=1\nPHOTOS_ENABLED=1\n")
    monkeypatch.setenv("HERMES_ENV_FILE", str(env))
    monkeypatch.setattr(valuation, "transport", FakeHttp())
    b = FakeBrowser()
    monkeypatch.setattr(valuation, "browser", b)
    return b


def click(sel, text=None):
    return (f"(() => {{ const el = [...document.querySelectorAll({json.dumps(sel)})].filter((e) => e.getClientRects().length)"
            f".find((e) => {json.dumps(text)} === null || e.textContent.trim().startsWith({json.dumps(text)}));"
            f" if (!el) throw new Error('nothing to click: ' + {json.dumps(sel)}); el.click(); return 1; }})()")


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


def open_page(browser, site, name, width=1440):
    browser.size(width, 900)
    browser.goto(f"{site.url}/?t={time.time():.3f}#/asset/{site.ids[name]}")
    until(browser, "document.querySelector('#page .vd-hero') && 1")
    browser.settle()


PAGE = """(() => ({hash: location.hash, title: document.getElementById('title').textContent,
  back: !document.getElementById('back').hidden, nav: document.querySelector('#nav a[aria-current=page]').dataset.pg,
  big: document.querySelector('#page .vd-head .big').textContent, line: document.querySelector('#page .vd-head .sub').textContent,
  table: [...document.querySelectorAll('#page .vd-vt tr')].map((r) => [...r.cells].map((c) => c.textContent.trim())),
  equity: (document.querySelector('#page .vd-eq .big') || {}).textContent || null,
  legend: [...document.querySelectorAll('#page .vd-legend b')].map((b) => b.textContent),
  specs: [...document.querySelectorAll('#page dl.vd-kv dt')].map((d) => d.textContent),
  pic: (() => { const w = document.querySelector('#page .vd-picw'); const img = w && w.querySelector('img');
    return w && {kind: w.dataset.kind, status: w.dataset.photo, loaded: img ? img.complete && img.naturalWidth > 0 : null,
      pin: Boolean(w.querySelector('.vd-pin')), drawn: Boolean(w.querySelector('svg[data-art]')),
      credit: (w.querySelector('.vd-credit') || {}).textContent || null,
      href: (w.querySelector('.vd-credit a') || {}).href || null}; })(),
  notes: [...document.querySelectorAll('#page .vd-quiet')].map((n) => n.textContent.trim()),
  text: document.getElementById('page').textContent,
  wide: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1}))()"""


def test_a_car_row_opens_its_page_with_a_real_url_and_back_works(site, browser, on):
    browser.size(1440, 900)
    browser.goto(f"{site.url}/?t={time.time():.3f}#/accounts")
    browser.settle()
    mesa = site.ids["2019 Zenith Mesa"]
    run(browser, click(f"#page a[data-asset='{mesa}']"))
    until(browser, "document.querySelector('#page .vd-hero') && 1")
    p = browser.js(PAGE)
    assert p["hash"] == f"#/asset/{mesa}" and p["title"] == "2019 Zenith Mesa LE" and p["back"] and p["nav"] == "accounts"
    assert p["table"] == [["Trade-in (Carfax)", "$20,875"], ["Private sale (Carfax)", "$22,963"],
                          ["Dealer retail (Carfax)", "$25,050"]]
    assert p["big"].replace(" ", "").startswith("$20,875") and p["line"].startswith("Trade-in · Carfax · ")
    assert p["equity"].replace(" ", "").startswith(f"${20875 - 5907.88:,.0f}".split(".")[0])
    assert p["legend"] == ["$5,907.88", f"${20875 - 5907.88:,.2f}"]         # the linked Car Loan, the equity
    run(browser, "document.getElementById('back').click()")
    until(browser, "location.hash === '#/accounts' && document.querySelector('#page .grp') && 1")
    assert browser.problems() == []
    # straight in (a bookmark): the back arrow is a plain link to Accounts
    open_page(browser, site, "2019 Zenith Mesa")
    assert browser.js("document.getElementById('back').getAttribute('href')") == "#/accounts"


def test_the_car_page_has_one_engine_row_its_photo_credit_and_no_retired_provider(site, browser, on):
    open_page(browser, site, "2019 Zenith Mesa")
    p = browser.js(PAGE)
    assert p["specs"].count("Engine") == 1 and "Engine (Carfax)" not in p["specs"]
    assert p["pic"] == {**p["pic"], "kind": "cutout", "status": "ready", "loaded": True, "drawn": False,
                        "credit": "Photo: Kevauto, CC BY-SA 4.0 · Wikimedia Commons",
                        "href": "https://commons.wikimedia.org/wiki/File:2019_Zenith_MESA_LE_front.jpg"}
    assert "MarketCheck" not in p["text"] and "RentCast" not in p["text"] and "Add a photo" not in p["text"]
    assert "Blind spot" not in p["text"].split("Also:")[0] or "Optional" in p["text"]   # only what NHTSA lists (here: optional)
    assert browser.problems() == []


def test_the_condition_control_asks_carfax_within_the_guard(site, browser, on):
    open_page(browser, site, "2019 Zenith Mesa")
    run(browser, click("#page .vd-seg .seg button", "Excellent"))
    toast = until(browser, "document.querySelector('#toast.on') && document.querySelector('#toast').textContent")
    assert toast.startswith("Condition: Excellent. Trade-in $14,090 by Carfax")
    assert len(on.opened) == 1 and json.loads(on.fetches[-1]["body"])["condition"] == "excellent"
    until(browser, "document.querySelector('#page .vd-seg button[aria-pressed=true]').textContent === 'Excellent'")
    run(browser, click("#page .vd-seg .seg button", "Fair"))                 # the same day: saved, Carfax not asked
    toast = until(browser, "(() => { const t = document.querySelector('#toast.on'); "
                           "return t && t.textContent.startsWith('Condition: Fair') && t.textContent; })()")
    assert "Carfax: already asked Carfax today for this account; again tomorrow." in toast and len(on.opened) == 1
    conn = db.connect(site.db_path)
    assert manual.get(conn, site.ids["2019 Zenith Mesa"])["vehicle_condition"] == "fair"
    conn.close()
    assert browser.problems() == []


def test_the_loan_picker_links_the_loan_and_the_equity_follows(site, browser, on):
    open_page(browser, site, "2015 Honda Fit")
    assert browser.js(PAGE)["equity"] is None and "No loan linked" in browser.js(PAGE)["text"]
    run(browser, click("#page .vd-actions .btn", "Edit"))
    until(browser, "document.querySelector('#f-linked_loan_id') && 1")
    options = browser.js("[...document.querySelectorAll('#f-linked_loan_id option')].map((o) => o.textContent)")
    assert options[0] == "None" and any(o.startswith("Car Loan · Zenith Financial Services") for o in options)
    run(browser, "(() => { const s = document.getElementById('f-linked_loan_id'); "
                 "s.value = [...s.options].find((o) => o.textContent.startsWith('Car Loan')).value; "
                 "s.dispatchEvent(new Event('change', {bubbles: true})); return 1; })()",
        "document.querySelector('#sheet form').requestSubmit()")
    until(browser, "document.querySelector('#page .vd-eq .big') && 1")
    p = browser.js(PAGE)
    assert p["legend"][0] == "$5,907.88" and p["legend"][1] == f"${7200 - 5907.88:,.2f}"   # its value is "Your value"
    assert browser.problems() == []


def test_property_pages_show_the_photo_the_map_and_no_estimate(site, browser, on):
    open_page(browser, site, "Condo", 390)
    p = browser.js(PAGE)
    assert p["pic"]["kind"] == "photo" and p["pic"]["loaded"] and p["pic"]["credit"] == "Photo: listing via Redfin"
    assert p["table"] == [["Estimate (Redfin)", "$432,000"], ["Last sold price (Redfin)", "$185,000"]] and not p["wide"]
    open_page(browser, site, "Cabin", 390)
    p = browser.js(PAGE)
    assert p["pic"] == {**p["pic"], "kind": "map", "pin": True, "loaded": True, "credit": "© OpenStreetMap contributors"}
    open_page(browser, site, "Studio", 390)
    p = browser.js(PAGE)
    assert p["pic"]["drawn"] and p["pic"]["kind"] == "outline" and p["table"] == [["Estimate (Redfin)", "no estimate"]]
    assert p["notes"] == ["Redfin has no record for unit 303 (only the building or other units): no estimate for this unit."]
    assert p["line"].startswith("Your value") and not p["wide"] and browser.problems() == []


def test_the_picture_appears_when_the_background_fetch_finishes(site, browser, on):
    conn = db.connect(site.db_path)
    m = manual.create(conn, {"kind": "vehicle", "vehicle_year": 2021, "vehicle_make": "Tesla", "vehicle_model": "Model 3",
                             "balance": 28000}, date.today())
    open_page(browser, site, "2019 Zenith Mesa")                                # (for its ids / warm)
    browser.goto(f"{site.url}/?t={time.time():.3f}#/asset/{m['id']}")
    until(browser, "document.querySelector('#page .vd-picw[data-photo=pending] svg[data-art=car]') && 1")
    assert m["id"] in SCHEDULED
    assert browser.js("document.querySelector('#page .vd-credit').textContent") == "Finding a photo…"
    row = manual.get(conn, m["id"])                                              # the background job finishes
    photos._write(photos.images_dir(site.db_path), photos.file_name(photos.key_for(row), "png"), gate_dbs._car_png())
    photos._save(conn, photos.key_for(row), "vehicle", "ready", source="commons", file=photos.file_name(photos.key_for(row), "png"),
                 cutout=True, credit="Photo: X, CC0 · Wikimedia Commons", credit_url="https://commons.wikimedia.org/wiki/File:X.jpg")
    conn.close()
    until(browser, "(() => { const w = document.querySelector('#page .vd-picw'); const i = w && w.querySelector('img');"
                   " return w && w.dataset.photo === 'ready' && i && i.complete && i.naturalWidth > 0; })()", timeout=8)
    assert browser.problems() == []
