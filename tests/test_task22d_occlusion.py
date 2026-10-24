"""TASK-22d: on a phone a control inside another tappable element takes its own taps. The Overview's Net worth tile is a
link to Accounts, and TASK-20's hit box (static/tap.css: the link's ::after, inset 0 on a card this big) was painted over
its All / Custom switch: a real tap on Custom opened Accounts and left the mode on All. Real taps here (CDP
Input.dispatchMouseEvent, not el.click()) at 390 on the gate's features DB, in headless Chrome (tests/cdp.py). The layout
gate's new tap-covered check (scripts/audit_layout.py) self-tests on a planted covered button and fails on the tile without
the fix."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
CUSTOM = "#page .nwtile .nwseg button[data-nw=custom]"
# tap.css's TASK-22d rule undone for the tile: its switch back under the tile's hit box
UNFIX = "@media (max-width: 1023.98px) { a[href] :is(button, [role=group]) { z-index: auto !important; } }"
# the element's centre (in the viewport, the page scrolled to put it 300px down) and what's on top there
AIM = """((sel) => { const el = document.querySelector(sel); scrollTo({top: scrollY + el.getBoundingClientRect().top - 300, behavior: 'instant'});
  const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2, top = document.elementFromPoint(x, y);
  return {x, y, top: top ? top.tagName + '.' + top.className : null, own: el === top || el.contains(top),
          inSwitch: Boolean(top && top.closest('.nwseg')), inTile: Boolean(top && top.closest('.nwtile'))}; })"""


def until(cond: str) -> str:
    """JS: wait (at most 3 s) until `cond` holds."""
    return (f"new Promise((r) => {{ const t0 = Date.now(); const tick = () => ({cond}) || Date.now() - t0 > 3000 ? r(1)"
            " : setTimeout(tick, 30); tick(); })")


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    srv = Server(gate_dbs.build("features", tmp_path_factory.mktemp("t22d") / "finance.db"))
    yield srv
    srv.close()


def tap(b, x, y):
    for kind in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)


def overview(b, srv):
    b.size(390, 844)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/overview")
    b.settle()


def test_a_real_tap_on_custom_switches_the_mode_and_stays_on_the_overview(site):
    with Chrome() as b:
        overview(b, site)
        assert b.js("document.documentElement.dataset.nw") == "all"
        aim = b.js(f"{AIM}({CUSTOM!r})")
        assert aim["own"], aim                                   # the switch is on top at its centre, not the tile's hit box
        tap(b, aim["x"], aim["y"])
        b.js(until("document.getElementById('modal').classList.contains('on')"))
        assert b.js("location.hash") == "#/overview"
        assert b.js("document.documentElement.dataset.nw") == "custom"
        assert b.js("localStorage.getItem('hf.networth.mode')") == "custom"
        assert b.js(f"document.querySelector({CUSTOM!r}).getAttribute('aria-pressed')") == "true"
        assert b.js("document.getElementById('modal').classList.contains('on')")   # a tap on Custom opens its sheet
        assert b.problems() == []


def test_a_real_tap_on_the_tile_away_from_the_switch_opens_accounts(site):
    with Chrome() as b:
        overview(b, site)
        aim = b.js(f"{AIM}('#page .nwtile .big')")
        assert aim["inTile"] and not aim["inSwitch"], aim
        tap(b, aim["x"], aim["y"])
        b.js(until("location.hash === '#/accounts'"))
        assert b.js("location.hash") == "#/accounts"
        assert b.js("document.documentElement.dataset.nw") == "all"                # the mode didn't change
        assert b.problems() == []


def test_a_blind_tap_covered_check_stops_the_run(site, monkeypatch):
    """The self-test's box moved off its button: the check no longer sees a covered one, so the self-test raises."""
    box = "add(w, 'div', {style: 'position:absolute;left:0;top:0;"
    assert audit_layout.TAP_PLANT_JS.count(box) == 1
    monkeypatch.setattr(audit_layout, "TAP_PLANT_JS", audit_layout.TAP_PLANT_JS.replace(box, box.replace("left:0", "left:200px")))
    with Chrome() as b, pytest.raises(AssertionError, match="tap-covered"):
        audit_layout.selftest(b, site.url, lambda line: None)


def test_tap_covered_fails_on_the_tile_without_the_fix_and_passes_with_it(site, tmp_path):
    for inject, want in ((UNFIX, {"All", "Custom"}), (None, set())):
        rep = audit_layout.audit(site.url, tmp_path / ("old" if inject else "fixed"), "t22d", (390,), log=lambda line: None,
                                 states={"overview"}, inject=inject, themes=("dark",), self_test=False, workers=1)
        entry = rep["widths"]["390"]["overview"]
        vs = entry["checks"]["tap-covered"]["violations"]
        assert {v["what"][0].split('"')[1] for v in vs if v["what"][1].startswith("a.card.tile.nwtile")} == want, vs
        assert len(vs) == len(want) and entry["seen"]["tap-covered"] == entry["seen"]["tap"] > 20
