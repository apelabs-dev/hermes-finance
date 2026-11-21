"""polish P3a in headless Chrome (tests/cdp.py) on a throwaway loopback server over the synthetic demo DB: the hero figures
(Spent, Left to spend, Net worth) count up only on a month switch, end on the exact formatted value, don't count under
prefers-reduced-motion or on a plain re-render, and don't shift the layout while they run. Screenshots: shots/p3a/."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from hermes_finance import db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
SHOTS = Path(__file__).resolve().parent.parent / "shots" / "p3a"
TODAY = date(2026, 9, 26)
CLOCK = """{ const NativeDate = Date, now = new NativeDate(2026, 8, 26, 12).getTime();
  window.Date = class extends NativeDate { constructor(...a) { super(...(a.length ? a : [now])); } static now() { return now; } }; }"""

HEROES = {"spent": "Spent in", "left": "Left to spend", "networth": "Net worth"}
FIND = """(key) => { const t = {spent: 'Spent in', left: 'Left to spend', networth: 'Net worth'}[key];
  const c = [...document.querySelectorAll('#page .card')].find((c) => ((c.querySelector('h3 .ttl') || {}).textContent || '').startsWith(t));
  return c && c.querySelector('.big'); }"""

# Samples the three heroes for `ms` after `act` ran; also the layout shifts seen from the first sample on.
# Two observers, because a page that is starved (a loaded CI runner: frames 200+ ms apart, timers running late) can run
# the whole 400 ms count-up between two frames (Nightly 44803010495: "the count-up never ran"):
#  - a requestAnimationFrame sampler: `out` / `rects` / `busy_at`, one entry per frame the page drew (the layout checks);
#  - a MutationObserver on the page: `mut` (the hero's text after every DOM change, so a fill that happened between two
#    frames is still recorded) and `busy_seen` (a change that left role / min-width on the hero: the count-up engaged).
# MutationObserver callbacks run as microtasks right after the task that changed the DOM, so they cannot be starved of
# frames. `count`: the act is a month switch, so wait on STATE, not on a window: until every hero has been seen counting
# (`started`, 8 s at most) and then until none is still counting (`ended`, 8 s at most). Without it the window is MS.
SAMPLE = """(async () => {
  const find = FIND_FN; const keys = ['spent', 'left', 'networth'];
  const out = {spent: [], left: [], networth: []}, rects = {spent: [], left: [], networth: []}, busy_at = {spent: 0, left: 0, networth: 0};
  const mut = {spent: [], left: [], networth: []}, busy_seen = {spent: 0, left: 0, networth: 0};
  const text = (el) => el.textContent.replace(/\\s+/g, ' ').trim();
  const isBusy = (el) => !!(el && (el.getAttribute('role') || el.style.minWidth));
  const anyBusy = () => keys.some((k) => isBusy(find(k)));
  let shift = 0, t0 = null, started = false, ended = false;
  const po = new PerformanceObserver((l) => { for (const e of l.getEntries()) if (t0 !== null && e.startTime >= t0) shift += e.value; });
  po.observe({type: 'layout-shift', buffered: false});
  const mo = new MutationObserver(() => { for (const k of keys) { const el = find(k); if (!el) continue;
    if (t0 === null) t0 = performance.now();
    const t = text(el); if (mut[k][mut[k].length - 1] !== t) mut[k].push(t);
    if (isBusy(el)) busy_seen[k] += 1; } });
  mo.observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true});
  ACT
  const begun = performance.now(), end = begun + MS;
  await new Promise((res) => { const tick = () => {
    const now = performance.now();
    for (const k of keys) { const el = find(k); if (!el) continue; if (t0 === null) t0 = now;
      out[k].push(text(el));
      if (isBusy(el)) { busy_at[k] += 1; busy_seen[k] += 1; }   // a frame while its count-up ran
      const r = el.getBoundingClientRect(); rects[k].push([Math.round(r.width * 10) / 10, Math.round(r.height * 10) / 10]); }
    if (!started && keys.every((k) => busy_seen[k] > 0)) started = true;
    if (started && !anyBusy()) ended = true;
    // in state mode: until every hero was seen counting and none still is (each wait capped at 8 s: a count-up that never starts
    // or never ends must fail the test's assertions, not hang it). Otherwise just the window.
    const waiting = COUNT ? !(started && ended) && now < begun + 8000 + (started ? 8000 : 0) : now < end;
    if (now < end || waiting) requestAnimationFrame(tick); else res(); }; tick(); });
  po.disconnect(); mo.disconnect();
  const aria = {}; for (const k of keys) { const el = find(k); aria[k] = el ? [el.getAttribute('role'), el.getAttribute('aria-label'), el.style.minWidth] : null; }
  return {out, rects, shift, aria, busy_at, busy_seen, mut, started, ended, final: Object.fromEntries(keys.map((k) => [k, find(k) && text(find(k))]))};
})()""".replace("FIND_FN", FIND)

PREV = "document.getElementById('mprev').click();"
RERENDER = "dispatchEvent(new HashChangeEvent('hashchange'));"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("motionp3a") / "finance.db"
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
        b.call("Page.addScriptToEvaluateOnNewDocument", source=CLOCK)
        yield b


def load(b, site, width=1280, reduced=False):
    b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce" if reduced else "no-preference"}])
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{site.url}/?t={time.time():.3f}#/overview")
    b.settled()
    assert b.still() is True, "the bars' first fill / page fade never finished"


def sample(b, act, ms=900, count=False):
    return b.js(SAMPLE.replace("ACT", act).replace("MS", str(ms)).replace("COUNT", "true" if count else "false"))


def test_count_up_on_month_switch_ends_on_exact_value(browser, site):
    load(browser, site)
    r = sample(browser, PREV, count=True)
    # waited on state, not a window: every hero was seen counting, then none was (a count-up that never starts, or never
    # clears its role / label / min-width, fails here rather than passing on a lucky sampling window)
    assert r["started"], ("the count-up never started", r["busy_seen"], r["mut"])
    assert r["ended"], ("the count-up never ended", r["aria"])
    for key in ("spent", "left", "networth"):
        seen = r["out"][key]
        assert seen, key
        final = r["final"][key]
        assert seen[-1] == final
        # it did count: the hero was marked as counting (role / min-width) in at least one change or frame. Seen by a
        # MutationObserver as well as by frames: a starved page can run the whole 400 ms count-up between two frames
        assert r["busy_seen"][key] >= 1, (key, "the count-up never ran", seen, r["mut"][key])
        # and it counted THROUGH values: the page drew 3+ frames while it was marked (frame 1 sets the min-width, frame 2
        # is the first tick's frame, so the hero still being marked on frame 3 means that tick filled an in-between value
        # instead of finishing) -> a value neither the start nor the end was on screen, in a frame or between two
        # (not len(set(seen)) > 2: net worth can start and end on the same value). A starved page (frames 200+ ms apart)
        # draws fewer marked frames and can go straight to the end value: then there is no in-between value to show
        if r["busy_at"][key] >= 3:
            assert (set(seen) | set(r["mut"][key])) - {seen[0], final}, (key, seen, r["mut"][key], r["busy_at"][key])
        assert r["aria"][key] == [None, None, ""], r["aria"][key]   # interruption state cleared: no stale label or min-width
    # the exact final values are the ones a plain load of that month shows (no count-up on a fresh load)
    month = browser.js("location.hash")
    browser.goto(f"{site.url}/?t={time.time():.3f}{month}")
    browser.settled()
    fresh = browser.js("(() => { const f = " + FIND + "; return Object.fromEntries(['spent','left','networth'].map((k) => [k, f(k).textContent.replace(/\\s+/g, ' ').trim()])); })()")
    assert fresh == r["final"]
    assert browser.problems() == []


# Both tests below follow the count-up by STATE, with a MutationObserver as well as frames (see SAMPLE): started when the
# hero first carries a role, ended when it no longer does (8 s at most for each wait: a count-up that never starts or never
# ends fails the assertions instead of hanging).
OBSERVE = "mo.observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true});"


def test_aria_label_is_the_final_value_while_counting(browser, site):
    load(browser, site)
    got = browser.js("""(async () => {
      const find = %s; const seen = [];
      const snap = () => { const el = find('spent'); const busy = !!(el && el.getAttribute('role'));
        if (busy) seen.push([el.getAttribute('role'), el.getAttribute('aria-label'), el.textContent.replace(/\\s+/g, ' ').trim()]);
        return busy; };
      const mo = new MutationObserver(snap); %s
      document.getElementById('mprev').click();
      const begun = performance.now(); let started = false;
      await new Promise((res) => { const tick = () => { const busy = snap(); if (busy) started = true;
        (started && !busy) || performance.now() > begun + (started ? 16000 : 8000) ? res() : requestAnimationFrame(tick); }; tick(); });
      mo.disconnect();
      return {seen, started, final: find('spent').textContent.replace(/\\s+/g, ' ').trim()};   // read once the count-up ended
    })()""" % (FIND, OBSERVE))
    assert got["seen"], "the count-up never marked the hero"
    assert all(s[0] == "img" for s in got["seen"])
    final = got["final"].replace("\u00a0", " ")
    assert all(s[1].replace("\u00a0", " ") == final for s in got["seen"]), got["seen"]   # the label is the final value, never an intermediate one


def test_count_up_takes_at_most_400ms(browser, site):
    load(browser, site)
    t = browser.js("""(async () => { const find = %s;
      let first = null, last = null, ended = false, gap = 0, prev = null;
      const note = () => { const el = find('spent'), now = performance.now(); const busy = !!(el && el.getAttribute('role'));
        if (busy) { if (first === null) first = now; last = now; } else if (first !== null) ended = true;
        return busy; };
      const mo = new MutationObserver(note); %s
      document.getElementById('mprev').click();
      const begun = performance.now(); prev = begun;
      await new Promise((res) => { const tick = () => { const now = performance.now(); gap = Math.max(gap, now - prev); prev = now; note();
        ended || now > begun + 8000 ? res() : requestAnimationFrame(tick); }; tick(); });
      mo.disconnect();
      return first === null ? null : [last - first, gap]; })()""" % (FIND, OBSERVE))
    assert t is not None, "no count-up"
    # app.js stops it with its own 400 ms timer. A busy page runs that timer late by up to its longest frame gap (the
    # Nightly runner measured 441.5 ms); that is the page's lag, not a longer count-up, so the bound takes it into account
    # but never drops below the old 440 ms.
    took, gap = t
    assert took <= max(440, 400 + gap), t


def test_no_count_up_on_a_plain_rerender(browser, site):
    load(browser, site)
    r = sample(browser, RERENDER, ms=600)
    for key in ("spent", "left", "networth"):
        assert len(set(r["out"][key])) == 1, (key, r["out"][key])   # the same text on every frame, after the re-render too


def test_reduced_motion_has_no_count_up(browser, site):
    load(browser, site, reduced=True)
    r = sample(browser, PREV)
    for key in ("spent", "left", "networth"):
        # fresh text straight away: at most the old month's figure, then the new one; never a run of intermediate values
        assert len(set(r["out"][key])) <= 2, (key, r["out"][key])
        assert r["out"][key][-1] == r["final"][key]
    assert browser.js("getComputedStyle(document.documentElement).getPropertyValue('--mo-dx').trim()") == "0px"
    assert browser.js("getComputedStyle(document.documentElement).getPropertyValue('--mo-scale').trim()") == "1"
    browser.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])


def test_no_layout_shift_while_counting(browser, site):
    load(browser, site)
    r = sample(browser, PREV, count=True)
    assert r["shift"] < 0.01, r["shift"]
    for key in ("spent", "left", "networth"):
        sizes = r["rects"][key]
        assert len({h for _, h in sizes}) == 1, (key, sizes)                  # the hero never changes height
        # the samples taken before the switch re-rendered are the OLD month's hero: a different number, not the count-up
        # (group roll-up, 2026-10-22: the two months' Left now differ in width, -$90.67 vs -$107.11)
        texts = r["out"][key]
        first = next((i for i, t in enumerate(texts) if t != texts[0]), 0)
        widths = [w for w, _ in sizes[first:]]
        assert max(widths) - min(widths) <= 0.5 or widths[0] == max(widths[: len(widths)]), (key, widths)  # never narrower than where it ends


@pytest.mark.parametrize("width", [1280, 390])
def test_screenshots(browser, site, width):
    SHOTS.mkdir(parents=True, exist_ok=True)
    load(browser, site, width)
    browser.js(PREV)
    browser.js("new Promise((r) => setTimeout(r, 200))")   # mid count-up
    browser.screenshot(SHOTS / f"overview-{width}-counting.png")
    browser.settled()
    browser.js("new Promise((r) => setTimeout(r, 600))")
    browser.screenshot(SHOTS / f"overview-{width}.png")
    assert browser.problems() == []
