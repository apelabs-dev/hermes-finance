"""TASK-12b: the layout gate's new checks (scripts/audit_layout.py: `segments`, `empty`) are discriminating. With the old
CSS re-introduced (tests/fixtures/task12b_old_layout.css: the empty-history message at left:64px / bottom:40px, the phone
range chips spread by space-between with content-width buttons) they FAIL on the Investments page; with the fix they
PASS. On the gates' synthetic DBs: nocaps (one investment account, no holdings, no snapshot), history-empty (the live page
The owner reported: holdings in two accounts, no snapshot yet, a tall Allocation card beside it), onepoint (one snapshot: a
one-point chart under the message) and full (21 days: a real chart). Headless Chrome; no network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import gate_dbs
from cdp import Server, find_chrome

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
OLD_CSS = (Path(__file__).parent / "fixtures" / "task12b_old_layout.css").read_text()
STATES = {"investments"}
WIDTHS = (1440, 1024, 390)


@pytest.fixture(scope="module")
def sites(tmp_path_factory):
    out = {}
    for kind in ("nocaps", "history-empty", "onepoint", "full"):
        srv = Server(gate_dbs.build(kind, tmp_path_factory.mktemp(f"g12b-{kind}") / "finance.db"))
        out[kind] = srv
    yield out
    for srv in out.values():
        srv.close()


def violations(rep):
    return [(int(w), check, v["rule"]) for w, states in rep["widths"].items() for e in states.values()
            for check, c in e.get("checks", {}).items() for v in c["violations"]]


@pytest.fixture(scope="module")
def runs(sites, tmp_path_factory):
    out = {}
    for kind, srv in sites.items():
        for label, inject in (("old", OLD_CSS), ("fixed", None)):
            out[kind, label] = audit_layout.audit(srv.url, tmp_path_factory.mktemp(f"rep-{kind}-{label}"), f"{kind}-{label}",
                                                  WIDTHS, log=lambda line: None, states=STATES, inject=inject,
                                                  themes=("dark",), self_test=False)   # TASK-18 options: this test's scope
    return out


def test_the_old_empty_state_css_fails_the_empty_check(runs):
    for kind in ("nocaps", "history-empty"):
        old = violations(runs[kind, "old"])
        # no snapshot: bottom-left on desktop and tablet, not vertically centred anywhere (the phone keeps full width)
        for w in (1440, 1024):
            assert (w, "empty", "an empty-state message off the horizontal centre of its container") in old, kind
        for w in WIDTHS:
            assert (w, "empty", "an empty-state message off the vertical centre of its container (no chart drawn)") in old, kind
    # beside the tall Allocation card the old box also kept far more than 120px blank above the message
    assert (1440, "empty", "an empty container keeping more than 120px of blank space around its message") in \
        violations(runs["history-empty", "old"])
    one = violations(runs["onepoint", "old"])
    for w in (1440, 1024):  # one point: the message must still be centred across
        assert (w, "empty", "an empty-state message off the horizontal centre of its container") in one
    assert (390, "empty", "an empty-state message covers a point of the chart behind it") in one   # full width, over it


def test_the_old_phone_range_chips_fail_the_segments_check(runs):
    for kind in ("nocaps", "history-empty", "onepoint", "full"):
        old = violations(runs[kind, "old"])
        assert (390, "segments", "a stretched segmented control whose buttons have different widths") in old
        assert not [v for v in old if v[1] == "segments" and v[0] != 390]   # not stretched on wider screens


@pytest.mark.parametrize("kind", ("nocaps", "history-empty", "onepoint", "full"))
def test_the_fixed_layout_passes_every_check(runs, kind):
    rep = runs[kind, "fixed"]
    assert rep["fail"] == 0, violations(rep)
    seen = {c: sum(e["checks"][c]["examined"] for states in rep["widths"].values() for e in states.values() if "checks" in e)
            for c in ("segments", "empty")}
    assert seen["segments"] >= 2 * len(WIDTHS)                       # both .seg controls, at every width
    assert seen["empty"] >= (len(WIDTHS) if kind != "full" else 0)    # the message was there to check


PACKED = """(() => {
  const [total, alloc] = [...document.querySelectorAll('#page .grid .card')];
  const area = total.querySelector('[data-empty-chart]');
  const out = {packed: total.getBoundingClientRect().height, alloc: alloc.getBoundingClientRect().height,
    area: area ? area.getBoundingClientRect().height : null};
  total.classList.remove('fill');                       // the packer's stretch off: the card's own height
  out.natural = total.getBoundingClientRect().height;
  total.classList.add('fill');
  return out;
})()"""


def test_a_packed_empty_chart_card_keeps_its_own_height(sites):
    """history-empty at 1440 (the live page): Total investments shows no chart beside a much taller Allocation card. The
    packer stretches it at most 40px (packsLikeChart), so its empty chart area stays about 180-220px (the brief) instead
    of growing to the neighbour's height (measured without the rule: card 488px, area 307px)."""
    import time
    from cdp import Chrome
    with Chrome() as b:
        b.size(1440, 900)
        b.goto(f"{sites['history-empty'].url}/?t={time.time():.3f}#/investments")
        b.settle()
        time.sleep(0.4)
        m = b.js(PACKED)
    assert m["alloc"] - m["natural"] > 100                            # the case that matters: a much taller neighbour
    assert 0 <= m["packed"] - m["natural"] <= 40 + 1                  # stretched no more than a chart card
    assert m["packed"] < m["alloc"]
    assert 180 <= m["area"] <= 221                                    # the message plus its padding, not a tall box
