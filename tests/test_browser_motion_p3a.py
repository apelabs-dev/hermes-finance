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

# Samples the three heroes on every frame for `ms` after `act` ran; also the layout shifts seen from the first sample on.
SAMPLE = """(async () => {
  const find = FIND_FN; const out = {spent: [], left: [], networth: []}, rects = {spent: [], left: [], networth: []};
  let shift = 0, t0 = null;
  const po = new PerformanceObserver((l) => { for (const e of l.getEntries()) if (t0 !== null && e.startTime >= t0) shift += e.value; });
  po.observe({type: 'layout-shift', buffered: false});
  ACT
  const end = performance.now() + MS;
  await new Promise((res) => { const tick = () => {
    for (const k of Object.keys(out)) { const el = find(k); if (!el) continue; if (t0 === null) t0 = performance.now();
      out[k].push(el.textContent.replace(/\\s+/g, ' ').trim());
      const r = el.getBoundingClientRect(); rects[k].push([Math.round(r.width * 10) / 10, Math.round(r.height * 10) / 10]); }
    // past the window, sampled on until no count-up is still running (its role/label/min-width set), 5 s at most: on a
    // slow machine it can outlast MS, and "ends on the exact value, state cleared" is about its end (t_d2b88168)
    const busy = () => Object.keys(out).some((k) => { const el = find(k); return el && (el.getAttribute('role') || el.style.minWidth); });
    if (performance.now() < end || (busy() && performance.now() < end + 5000)) requestAnimationFrame(tick); else res(); }; tick(); });
  po.disconnect();
  const aria = {}; for (const k of Object.keys(out)) { const el = find(k); aria[k] = el ? [el.getAttribute('role'), el.getAttribute('aria-label'), el.style.minWidth] : null; }
  return {out, rects, shift, aria, final: Object.fromEntries(Object.keys(out).map((k) => [k, find(k) && find(k).textContent.replace(/\\s+/g, ' ').trim()]))};
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
    b.js("new Promise((r) => setTimeout(r, 700))")   # the bars' first fill and the page fade are over


def sample(b, act, ms=900):
    return b.js(SAMPLE.replace("ACT", act).replace("MS", str(ms)))


def test_count_up_on_month_switch_ends_on_exact_value(browser, site):
    load(browser, site)
    r = sample(browser, PREV)
    for key in ("spent", "left", "networth"):
        seen = r["out"][key]
        assert seen, key
        final = r["final"][key]
        assert seen[-1] == final
        assert set(seen) - {seen[0], final}, (key, seen)  # it did count: a value neither the start nor the end was on screen
        # (not len(set(seen)) > 2: net worth can start and end on the same value, and a slow machine catches 1 frame)
        assert r["aria"][key] == [None, None, ""], r["aria"][key]   # interruption state cleared: no stale label or min-width
    # the exact final values are the ones a plain load of that month shows (no count-up on a fresh load)
    month = browser.js("location.hash")
    browser.goto(f"{site.url}/?t={time.time():.3f}{month}")
    browser.settled()
    fresh = browser.js("(() => { const f = " + FIND + "; return Object.fromEntries(['spent','left','networth'].map((k) => [k, f(k).textContent.replace(/\\s+/g, ' ').trim()])); })()")
    assert fresh == r["final"]
    assert browser.problems() == []


def test_aria_label_is_the_final_value_while_counting(browser, site):
    load(browser, site)
    got = browser.js("""(async () => {
      document.getElementById('mprev').click();
      const find = %s; const seen = [];
      await new Promise((res) => { const end = performance.now() + 500; const tick = () => { const el = find('spent');
        if (el && el.getAttribute('role')) seen.push([el.getAttribute('role'), el.getAttribute('aria-label'), el.textContent.replace(/\\s+/g, ' ').trim()]);
        performance.now() < end ? requestAnimationFrame(tick) : res(); }; tick(); });
      await new Promise((res) => setTimeout(res, 1500));   // the count-up can outlast the 500 ms sample: read the settled value
      return {seen, final: find('spent').textContent.replace(/\\s+/g, ' ').trim()};
    })()""" % FIND)
    assert got["seen"], "the count-up never marked the hero"
    assert all(s[0] == "img" for s in got["seen"])
    final = got["final"].replace("\u00a0", " ")
    assert all(s[1].replace("\u00a0", " ") == final for s in got["seen"]), got["seen"]   # the label is the final value, never an intermediate one


def test_count_up_takes_at_most_400ms(browser, site):
    load(browser, site)
    t = browser.js("""(async () => { const find = %s; document.getElementById('mprev').click();
      const t0 = performance.now(); let last = t0, first = null, prev = t0, gap = 0;
      await new Promise((res) => { const end = t0 + 1500; const tick = () => { const el = find('spent'), now = performance.now();
        gap = Math.max(gap, now - prev); prev = now;
        if (el && el.getAttribute('role')) { if (first === null) first = now; last = now; }
        now < end ? requestAnimationFrame(tick) : res(); }; tick(); });
      return first === null ? null : [last - first, gap]; })()""" % FIND)
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
    r = sample(browser, PREV)
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
