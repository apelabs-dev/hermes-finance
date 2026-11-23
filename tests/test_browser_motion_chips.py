"""t_acbcd9f4 (motion A) and t_3d65332a (alert chips grid), in headless Chrome (tests/cdp.py). Synthetic demo data only,
on a throwaway loopback server.

Motion A: the bars refill and the charts grow (400ms) on a page switch and a month switch, not on a refresh in place,
and never under prefers-reduced-motion. Before it, the bars filled once per session and the charts never animated.
Alert chips: an even grid (2 columns on a desktop, 1 on a phone) instead of content-width chips in ragged rows, with the
broken connection (the one with a Reconnect button) first.

The demo DB is built for the fixed day below and the browser's clock is pinned to it (Chrome.pin_clock): the Overview takes
"this month" from `new Date()`, so unpinned the page asked for the month the gate itself ran in and, from 2026-11-01 on,
drew two chips from an October fixture (its 4 alerts include the two cap alerts that went missing).
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
MONTH = TODAY.isoformat()[:7]

# what animated in the MS after ACT: mo-bar CSS animations started, and whether any Chart.js chart was animating.
# A positive sample runs on, up to CAP, until the bars and the charts have both animated: on a loaded runner the
# hash change's fetch and render can outlast a fixed window, which is how Nightly 43245845336 sampled bars 0 from a
# page that had redrawn. The in-place controls keep CAP = MS: their proof is a fixed window that nothing animates in.
SAMPLE = """(async () => {
  let bars = 0, charts = false;
  const inBars = (el) => IN_BARS;   // which bars this sample counts: the page's, or any
  const live = new Map();   // bar element -> its mo-bar animations counted and not yet ended
  const onstart = (e) => {
    if (e.animationName !== 'mo-bar' || !inBars(e.target)) return;
    if (window.__stall_hook && !window.__stall_ran) return;   // the stall hook owns this one
    bars++; live.set(e.target, (live.get(e.target) || 0) + 1);
  };
  const onend = (e) => {
    if (e.animationName !== 'mo-bar') return;
    live.delete(e.target);   // a finished animation counts; a later cancel for it must not take it back
  };
  const oncancel = (e) => {
    if (e.animationName !== 'mo-bar' || !inBars(e.target)) return;
    if (!live.has(e.target)) return;
    live.delete(e.target); bars--;   // cancelled before it finished: an animation nobody saw
  };
  document.addEventListener('animationstart', onstart, true);
  document.addEventListener('animationend', onend, true);
  document.addEventListener('animationcancel', oncancel, true);
  ACT
  const end = performance.now() + MS;
  const cap = performance.now() + CAP;
  // This bound on the post-cap state wait is anchored at the first frame past the cap, not at the cap: a main-thread
  // stall blocks every frame, so a bound measured from before the stall can expire without a single frame given to the
  // state it bounds (measured: the stall ended 0.9 s past the cap and the sample resolved with four page-bar mo-bar
  // animations pending, currentTime 0). A counted animation still gets to reach its cancel inside it.
  let settle = cap + SETTLE;
  let observing = false;
  // Past the cap the sample cannot end while a page bar animation can still start: HOLD is that state (false for the
  // samples that are over at the cap). It is not a frame allowance: the tick that decides runs before the style recalc
  // that creates the animation, so any fixed number of frames can end one frame before its animationstart.
  const holds = () => (HOLD);
  await new Promise((res) => { const tick = () => {
    const C = window.Chart;
    if (C && Object.values(C.instances || {}).some((c) => C.animator.running(c))) charts = true;
    const now = performance.now();
    if (!observing && now > cap) { observing = true; settle = now + SETTLE; }
    if (now < end || (now < cap && !(bars > 0 && charts)) || (live.size > 0 && now < settle)
        || (now < settle && holds()))
      requestAnimationFrame(tick);
    else res(); }; tick(); });
  document.removeEventListener('animationstart', onstart, true);
  document.removeEventListener('animationend', onend, true);
  document.removeEventListener('animationcancel', oncancel, true);
  return {bars, charts, hash: location.hash};
})()"""
PREV = "document.getElementById('mprev').click();"
RERENDER = "dispatchEvent(new HashChangeEvent('hashchange'));"
CAP = 4000   # a positive's liveness bound: the sample ends when the page animates, this only bounds a broken one
# (measured ready times on this fixture with the fix: 42-270 ms, 20 runs; 4 s is ~15x the slowest)
SETTLE = 1000   # past the cap: the bound on a settled dict — a counted bar reaches its cancel, a pending one its start

# The stall test's post-cap state wait, injected as HOLD. Past the cap the sample keeps ticking while no bar animation
# has been counted and one can still arrive: either an mo-bar animation already applies to a #page bar but no start has
# counted it yet, or the app's bars-done window is still open (the stylesheet rule that creates mo-bar is on, so the
# next style recalc can add the animation after this frame's tick). On base e066a26 bars-done lands before the first
# frame, the rule never applies, no animation is created, and the wait ends at once with CI's dict.
HOLD = """(bars === 0 && (
    [...document.getAnimations()].some((a) => a.animationName === 'mo-bar' && a.effect && inBars(a.effect.target)
      && a.playState !== 'finished' && !live.has(a.effect.target))
    || (!document.body.classList.contains('bars-done')
        && !!document.querySelector('#page .meter i, #page .pbar i, #page .pbar b, #page .share i, #page .split i'))))"""

# The Nightly 43245845336 regression: a real main-thread stall, started the moment the first bar element lands in #page
# (the bars are drawn, no frame has painted them yet) and blocking until past the sample's own cap. A chart on the page
# being left is set animating, so `charts` is what CI saw (True), and the stall is a busy-wait with no timers inside. On
# base the overdue 400 ms bars-done timer fires before the first frame after the stall, so the bars never animate; with
# the fix that timer is not scheduled yet, so they animate once the stall ends. The hook is a microtask, not a
# MessageChannel task: a task lets a rendering opportunity run first, so on base the bars' animation starts before the
# stall and the test passes on base (measured: 8 of 9 runs). A microtask from the observer's callback is in the same
# task as the draw, before any rendering opportunity, so the stall always sits between the draw and the first frame.
STALL = """(() => {
  const C = window.Chart;   // the page being left has a chart: the sample's charts latch, as CI's dict has it
  const ch = Object.values(C.instances || {})[0];
  if (ch) { ch.options.animation = {...(ch.options.animation || {}), duration: 3000}; ch.reset(); ch.update(); }
  window.__stall_ran = false;
  window.__stall_hook = true;   // the sample can tell an installed hook from no hook at all
  const page = document.getElementById('page');
  const mo = new MutationObserver(() => {   // the DOM, not app.js's scheduling: the hook must not move with the fix
    if (!page.querySelector('.meter i, .pbar i, .pbar b, .share i, .split i')) return;   // the bars aren't in yet
    mo.disconnect();
    queueMicrotask(() => {   // same task as the draw: no rendering opportunity can come between
      window.__stall_ran = true;
      const t0 = performance.now();
      while (performance.now() - t0 < %d) {}   // no timers in the stall: a starved renderer, not a sleep
    });
  });
  mo.observe(page, {childList: true, subtree: true});
})();"""


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
        b.pin_clock(TODAY)   # the page's "today" is the fixture's, whatever day this gate runs on
        yield b


def load(b, site, width=1440, reduced=False, page="overview"):
    b.call("Emulation.setEmulatedMedia",
           features=[{"name": "prefers-reduced-motion", "value": "reduce" if reduced else "no-preference"}])
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{site.url}/?t={time.time():.3f}#/{page}")
    b.settled()
    b.js("new Promise((r) => setTimeout(r, 700))")   # the first load's bars, charts and fade are over


def sample(b, act, ms=700, cap=700, in_bars="true", hold="false"):
    return b.js(SAMPLE.replace("ACT", act).replace("MS", str(ms)).replace("CAP", str(cap))
                .replace("SETTLE", str(SETTLE)).replace("IN_BARS", in_bars).replace("(HOLD)", hold))


def test_returning_to_a_page_refills_its_bars_and_grows_its_charts(browser, site):
    load(browser, site, page="budgets")
    r = sample(browser, go("overview"), cap=CAP)
    assert r["bars"] > 0, r          # was 0: bars filled once per session (body.bars-done stayed on)
    assert r["charts"], r            # was never: Chart.defaults.animation = false for every chart
    assert browser.problems() == []


def test_a_main_thread_stall_after_the_bars_are_drawn_does_not_skip_their_animation(browser, site):
    # CI's dict: {'bars': 0, 'charts': True, 'hash': '#/overview'}. The stall starts when the first bar element lands in
    # #page, i.e. as soon as the rendered bars are in the DOM (no frame has drawn them yet), and outlasts the sample's
    # cap. Past the cap the sample waits on HOLD (a page bar's animation still to start, or the bars-done window still
    # open) instead of a frame count; on base bars-done lands before the first frame, so the Overview's bars never
    # animate (the page's, not the sidebar's).
    load(browser, site, page="investments")   # a page with a chart: the sample's charts latch while it is left
    r = sample(browser, STALL % (CAP + 500) + go("overview"), cap=CAP, hold=HOLD,
               in_bars="document.getElementById('page').contains(el)")
    assert browser.js("window.__stall_ran") is True, "the stall hook did not run"   # else this would pass on nothing
    assert r["bars"] > 0, r          # was 0: bars-done was added before their first frame, so they never filled
    assert r["charts"], r
    assert browser.problems() == []


def test_a_month_switch_refills_the_bars_and_grows_the_charts(browser, site):
    load(browser, site)
    r = sample(browser, PREV, cap=CAP)
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


def strip_count(d: dict) -> int:
    """How many chips the Overview's strip holds for this month's payload (app.js alertsStrip): its cap and connection
    alerts, the cards with a minimum due, and the one "all healthy" line when nothing is broken."""
    return len(d["alerts"]) + len(d["cards_due"] or []) + (0 if d["connections"]["broken"] else 1)


def test_desktop_alert_chips_are_an_even_two_column_grid_broken_connection_first(browser, site):
    load(browser, site)
    s = browser.js(CHIPS)
    chips = s["chips"]
    want = strip_count(site.api("/api/overview", month=MONTH))   # the fixture's month: what the page must be showing
    assert s["display"] == "grid" and len(chips) == want, (len(chips), want, s)
    assert want >= 3, want                                        # the two cap alerts are in there, not just a connection
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
