"""Settings' loading skeleton, in headless Chrome (tests/cdp.py) on a copy of the gate's "full" database.

HF.RENDER.settings fills the page with settingsskel.js's skeleton while GET /api/settings/page is in flight: the real
page's eleven cards, in the real order, at the real width and left. The test freezes that fetch in the page, measures the
skeleton, lets the page finish, then compares each card's box with the real one at 1280 and 390 px."""

from __future__ import annotations

import json
import time

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
CARDS = ["connections", "access", "ai", "mcp", "sync", "valuations", "region", "look", "data", "categories", "rules"]
# Installed before any page script runs: the settings payload takes 2.5 s, so the skeleton can be measured.
FREEZE = """window.fetch = ((orig) => async (u, o) => {
  if (String(u).includes('/api/settings/page')) await new Promise((r) => setTimeout(r, 2500));
  return orig(u, o);
})(window.fetch);"""
RECTS = """[...document.querySelectorAll(%s)].map((c) => { const r = c.getBoundingClientRect();
  return [r.width, r.left]; })"""


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        b.call("Page.addScriptToEvaluateOnNewDocument", source=FREEZE)
        yield b


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("settings_skel") / "finance.db"
    gate_dbs.build("full", path)
    srv = Server(path)
    yield srv
    srv.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_the_loading_skeleton_mirrors_the_settings_cards(browser, site, width):
    browser.size(width, 900 if width > 500 else 844)
    browser.goto(f"{site.url}/?t={time.time():.6f}#/settings", ready=False)
    assert browser.until("document.querySelectorAll('[data-skel-card]').length === 11", 6000)
    names = browser.js("[...document.querySelectorAll('[data-skel-card]')].map((c) => c.dataset.skelCard)")
    assert names == CARDS, names
    busy = browser.js("document.querySelector('#page [aria-busy=true]')?.getAttribute('aria-label')")
    assert busy == "Loading settings", busy
    skeleton = browser.js(RECTS % json.dumps("[data-skel-card]"))
    assert len(skeleton) == 11
    assert browser.until("document.querySelectorAll('#page .sgrid > .card').length === 11"
                         " && !document.querySelector('#page [aria-busy=true]')", 15000)
    real = browser.js(RECTS % json.dumps("#page .sgrid > .card"))
    assert len(real) == 11
    for name, (sw, sl), (rw, rl) in zip(CARDS, skeleton, real):
        assert abs(sw - rw) <= 2, (width, name, "width", sw, rw)
        assert abs(sl - rl) <= 2, (width, name, "left", sl, rl)
    assert browser.problems() == []


def test_reduced_motion_turns_the_shimmer_off(browser, site):
    """settingsskel.css stops the .skel shimmer under reduced motion; the cards still draw."""
    browser.size(1280, 900)
    browser.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
    try:
        browser.goto(f"{site.url}/?t={time.time():.6f}#/settings", ready=False)
        assert browser.until("document.querySelectorAll('[data-skel-card]').length === 11", 6000)
        got = browser.js("""(() => { const s = document.querySelector('[data-skel-card] .skel');
          return {reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
                  name: getComputedStyle(s).animationName}; })()""")
        assert got == {"reduced": True, "name": "none"}, got
        assert browser.until("document.querySelectorAll('#page .sgrid > .card').length === 11"
                             " && !document.querySelector('#page [aria-busy=true]')", 15000)
    finally:
        browser.call("Emulation.setEmulatedMedia", features=[])
    assert browser.problems() == []
