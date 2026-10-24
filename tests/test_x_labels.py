"""polish x-labels: category names show with group prefix in selects, chips, and picker buttons
(e.g., 'Food · Coffee' for categories with groups, bare for Income/Transfers and when group=category)."""

from __future__ import annotations

import json
import time
from datetime import date
from urllib.parse import parse_qs

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()
WAIT = "new Promise((r) => setTimeout(r, {ms}))"
T = "var T = (el) => (el ? el.textContent.replace(/\\s+/g, ' ').trim() : null);"

STATE = T + """({
  hash: location.hash,
  chips: [...document.querySelectorAll('#page .txchips > *')].filter((x) => x.getClientRects().length).map(T),
  catbtn: T(document.querySelector('#page .catb')),
  drawer: Boolean(document.querySelector('.tfd')),
  catOptions: [...document.querySelectorAll('.tfd .tftree .tfr:not(.tfgr) .tft')].map(T),
})"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("x_labels") / "finance.db"
    gate_dbs.build("features", path, TODAY)
    srv = Server(path, today=TODAY)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def go(b, site, page, width=1280):
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{site.url}/?t={time.time():.3f}#/{page}")
    b.settle()
    b.js(WAIT.format(ms=250))


def act(b, js, ms=450):
    b.js(f"(() => {{ {js}; return 1; }})()")
    b.js(WAIT.format(ms=ms))
    return b.js(STATE)


def test_category_chips_show_group_category_format(site, chrome):
    """Category chips in the filter bar show 'Group · Category' format."""
    go(chrome, site, "transactions")
    # Open the filters drawer
    s = act(chrome, "document.querySelector('#page .tfbtn').click()", 500)
    assert s["drawer"]
    # Pick a category from Food group (e.g., Coffee)
    act(chrome, "const q = document.querySelector('.tfd .tfq'); q.value = 'coffee'; q.dispatchEvent(new Event('input', {bubbles: true}))", 100)
    act(chrome, "[...document.querySelectorAll('.tfd .tftree .tfr')].find((r) => r.textContent.includes('Coffee') "
                "&& !r.classList.contains('tfgr')).querySelector('.tfc').click()", 100)
    s = act(chrome, "document.querySelector('.tfd .tfft .btn.w').click()", 700)
    # The chip should show "Food · Coffee"
    assert "Food · Coffee" in s["chips"]
    assert chrome.problems() == []


def test_group_chips_and_category_chips_together(site, chrome):
    """When both group and category are selected, they show correctly formatted."""
    go(chrome, site, "transactions")
    # Open the filters drawer
    s = act(chrome, "document.querySelector('#page .tfbtn').click()", 500)
    assert s["drawer"]
    # Pick a category from a group
    act(chrome, "const q = document.querySelector('.tfd .tfq'); q.value = 'coffee'; q.dispatchEvent(new Event('input', {bubbles: true}))", 100)
    act(chrome, "[...document.querySelectorAll('.tfd .tftree .tfr')].find((r) => r.textContent.includes('Coffee') "
                "&& !r.classList.contains('tfgr')).querySelector('.tfc').click()", 100)
    s = act(chrome, "document.querySelector('.tfd .tfft .btn.w').click()", 700)
    # The chip should show "Food · Coffee", not "Food › Coffee"
    assert "Food · Coffee" in s["chips"]
    assert not any("›" in chip for chip in s["chips"]), f"Found old format › in chips: {s['chips']}"
    assert chrome.problems() == []


def test_no_old_format_arrows_in_chips(site, chrome):
    """Verify that old format with › arrows is not present in chips."""
    go(chrome, site, "transactions")
    # Open the filters drawer
    s = act(chrome, "document.querySelector('#page .tfbtn').click()", 500)
    assert s["drawer"]
    # Pick a category
    act(chrome, "const q = document.querySelector('.tfd .tfq'); q.value = 'coffee'; q.dispatchEvent(new Event('input', {bubbles: true}))", 100)
    act(chrome, "[...document.querySelectorAll('.tfd .tftree .tfr')].find((r) => r.textContent.includes('Coffee') "
                "&& !r.classList.contains('tfgr')).querySelector('.tfc').click()", 100)
    s = act(chrome, "document.querySelector('.tfd .tfft .btn.w').click()", 700)
    # No chip should use the old "›" format
    assert not any("›" in chip for chip in s["chips"]), f"Found old format › in chips: {s['chips']}"
    # Should use the new "·" format
    assert any("·" in chip for chip in s["chips"] if "coffee" in chip.lower() or "food" in chip.lower()), \
        f"Expected · format in chips: {s['chips']}"
    assert chrome.problems() == []


