"""t_acbcd9f4 (motion A) and t_3d65332a (alert chips grid), in headless Chrome (tests/cdp.py). Synthetic demo data only,
on a throwaway loopback server.

Motion A: the bars refill and the charts grow (400ms) on a page switch and a month switch, not on a refresh in place,
and never under prefers-reduced-motion. Before it, the bars filled once per session and the charts never animated.
Alert chips: an even grid (2 columns on a desktop, 1 on a phone) instead of content-width chips in ragged rows, with the
broken connection (the one with a Reconnect button) first.
"""

from __future__ import annotations

import time
from datetime import date

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from hermes_finance import db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date(2026, 10, 20)

# what animated in the MS after ACT: mo-bar CSS animations started, and whether any Chart.js chart was animating
SAMPLE = """(async () => {
  let bars = 0, charts = false;
  const on = (e) => { if (e.animationName === 'mo-bar') bars++; };
  document.addEventListener('animationstart', on, true);
  ACT
  const end = performance.now() + MS;
  await new Promise((res) => { const tick = () => {
    const C = window.Chart;
    if (C && Object.values(C.instances || {}).some((c) => C.animator.running(c))) charts = true;
    if (performance.now() < end) requestAnimationFrame(tick); else res(); }; tick(); });
  document.removeEventListener('animationstart', on, true);
  return {bars, charts, hash: location.hash};
})()"""
PREV = "document.getElementById('mprev').click();"
RERENDER = "dispatchEvent(new HashChangeEvent('hashchange'));"


def go(page: str) -> str:
    return f"location.hash = '#/{page}';"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("motionchips") / "finance.db"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    demo_data.extras(conn, today=TODAY)
    conn.close()
    srv = Server(path, today=TODAY)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


def load(b, site, width=1440, reduced=False, page="overview"):
    b.call("Emulation.setEmulatedMedia",
           features=[{"name": "prefers-reduced-motion", "value": "reduce" if reduced else "no-preference"}])
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{site.url}/?t={time.time():.3f}#/{page}")
    b.settled()
    b.js("new Promise((r) => setTimeout(r, 700))")   # the first load's bars, charts and fade are over


def sample(b, act, ms=700):
    return b.js(SAMPLE.replace("ACT", act).replace("MS", str(ms)))


def test_returning_to_a_page_refills_its_bars_and_grows_its_charts(browser, site):
    load(browser, site, page="budgets")
    r = sample(browser, go("overview"))
    assert r["bars"] > 0, r          # was 0: bars filled once per session (body.bars-done stayed on)
    assert r["charts"], r            # was never: Chart.defaults.animation = false for every chart
    assert browser.problems() == []


def test_a_month_switch_refills_the_bars_and_grows_the_charts(browser, site):
    load(browser, site)
    r = sample(browser, PREV)
    assert "month=" in r["hash"]
    assert r["bars"] > 0 and r["charts"], r


def test_a_refresh_in_place_draws_still(browser, site):
    load(browser, site)
    r = sample(browser, RERENDER)
    assert r == {"bars": 0, "charts": False, "hash": r["hash"]}


def test_a_toggle_redrawn_in_place_draws_still(browser, site):
    # the gate's doughnut and drill tests read slice geometry right after a toggle: a regrowing chart broke them
    load(browser, site, page="investments")
    r = sample(browser, "document.querySelector('#page .seg button[data-k=type]').click();")
    assert r["charts"] is False, r
    assert browser.problems() == []


def test_reduced_motion_never_animates_bars_or_charts(browser, site):
    load(browser, site, reduced=True, page="budgets")
    for act in (go("overview"), PREV):
        r = sample(browser, act)
        assert r["bars"] == 0 and not r["charts"], (act, r)
    browser.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])


CHIPS = """(() => { const s = document.querySelector('#page .alerts'); const sr = s.getBoundingClientRect();
  return {display: getComputedStyle(s).display, left: sr.left, right: sr.right,
    chips: [...s.children].map((c) => { const r = c.getBoundingClientRect();
      return {tag: c.tagName, cls: c.className, top: Math.round(r.top), left: r.left, right: r.right, w: r.width,
              clipped: c.scrollWidth > c.clientWidth + 1, reconnect: !!c.querySelector('button')}; })}; })()"""


def test_desktop_alert_chips_are_an_even_two_column_grid_broken_connection_first(browser, site):
    load(browser, site)
    s = browser.js(CHIPS)
    chips = s["chips"]
    assert s["display"] == "grid" and len(chips) >= 3, s
    assert chips[0]["tag"] == "DIV" and chips[0]["reconnect"], chips[0]   # the one that needs you comes first
    rows = {}
    for c in chips:
        rows.setdefault(c["top"], []).append(c)
    full = [r for r in rows.values() if len(r) == 2]
    assert full, rows                                                     # 2 columns at 1440
    for r in full:                                                        # equal widths, flush with both edges
        assert abs(r[0]["w"] - r[1]["w"]) <= 1, r
        assert abs(r[0]["left"] - s["left"]) <= 1 and abs(r[1]["right"] - s["right"]) <= 1, (r, s)
    assert not any(c["clipped"] for c in chips), chips


def test_phone_alert_chips_are_one_full_width_column(browser, site):
    load(browser, site, width=390)
    s = browser.js(CHIPS)
    assert len({c["top"] for c in s["chips"]}) == len(s["chips"])          # one per row
    for c in s["chips"]:
        assert abs(c["left"] - s["left"]) <= 1 and abs(c["right"] - s["right"]) <= 1, (c, s)
        assert not c["clipped"], c
