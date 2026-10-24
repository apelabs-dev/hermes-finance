"""TASK-20: the layout gate's `tap` check (scripts/audit_layout.py): on a phone (under 1024px) every control is 44 x 44 px
or more to hit. It self-tests on a planted 30px button (caught), the same button with a transparent 44px ::before hit box
and an inline link in a sentence (not caught), and it fails on the real pages without static/tap.css's hit boxes, then
passes with them. The plan runs it at 360, 390, 430 and 768."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
# tap.css taken back out: no transparent hit boxes, the inputs and selects at their drawn size
NO_TAP_CSS = ("*::after { content: none !important; } .inp input, .edin input, .search { min-height: 0 !important; } "
              "select.cat, .bsel { min-height: 0 !important; border-width: 1px !important; margin: 0 !important; }")


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    srv = Server(gate_dbs.build("features", tmp_path_factory.mktemp("g20") / "finance.db"))
    yield srv
    srv.close()


def test_the_self_test_catches_a_planted_30px_button(site):
    lines = []
    with Chrome() as b:
        audit_layout.selftest(b, site.url, lines.append)
    assert "tap" in lines[0] and "each caught their planted bug" in lines[0]


def test_a_blind_tap_check_stops_the_run(site, monkeypatch):
    """The planted button made 50px: the check no longer sees a small one, so the self-test raises."""
    small = "textContent: 'tapsmall', style: 'width:30px;height:30px"
    assert small in audit_layout.TAP_PLANT_JS
    monkeypatch.setattr(audit_layout, "TAP_PLANT_JS", audit_layout.TAP_PLANT_JS.replace(small, small.replace("30px", "50px")))
    with Chrome() as b, pytest.raises(AssertionError, match="tap"):
        audit_layout.selftest(b, site.url, lambda line: None)


# (state, width, controls the review measured under 44px: a piece of their description and their height then)
@pytest.mark.parametrize("state,width,small", [
    ("overview", 390, [("button.menu", 40), ('button ""', 30)]),                  # the menu button, the month arrows
    ("transactions", 360, [("button.fchip", 37), ("button.catb", 33)]),          # filter chips, category pickers
    ("recurring", 430, [('button "" in Bills calendar', 30)]),                    # the calendar's month arrows
    ("tx-save-search", 390, [("button.ib", 38)]),                                  # a sheet's close
])
def test_tap_fails_without_the_hit_boxes_and_passes_with_them(site, tmp_path, state, width, small):
    old = audit_layout.audit(site.url, tmp_path / "old", "old", (width,), log=lambda line: None, states={state},
                             inject=NO_TAP_CSS, themes=("dark",), self_test=False, workers=1)
    found = [(v["what"][0], v["detail"]["h"]) for e in old["widths"][str(width)].values() for v in e["checks"]["tap"]["violations"]]
    for piece, height in small:
        assert any(piece in what and h == height for what, h in found), (piece, height, found)
    fixed = audit_layout.audit(site.url, tmp_path / "fixed", "fixed", (width,), log=lambda line: None, states={state},
                               themes=("dark",), self_test=False, workers=1)
    entry = next(iter(fixed["widths"][str(width)].values()))
    assert entry["checks"]["tap"]["examined"] >= len(small) and entry["checks"]["tap"]["violations"] == []


def test_the_plan_runs_tap_at_four_phone_widths():
    plan = list(audit_layout.plan("features", audit_layout.WIDTHS, None))
    assert {w for w, *_ in plan if w < audit_layout.PHONE} == {768, 430, 390, 360}
    extra = {(w, t, n) for w, t, n, *_ in plan if w in audit_layout.TAP_WIDTHS}
    assert {(430, "dark", "phone-drawer"), (360, "dark", "goals"), (360, "dark", "settings")} <= extra
    # TASK-20's own states at the extra phone widths: dark only; TASK-21's (EVERY_WIDTH) run there in both themes
    assert all(t == "dark" for _, t, n in extra if n not in audit_layout.EVERY_WIDTH)
    assert not any(n == "tx-save-search" for *_, n in extra)
    assert {(w, t, n) for w in audit_layout.TAP_WIDTHS for t in ("dark", "light") for n in audit_layout.EVERY_WIDTH} <= extra
    assert "tap" in audit_layout.CHECKS


def test_single_worker_audit_keeps_the_callers_chrome_slot_ownership(tmp_path, monkeypatch):
    """A nested audit must not hide the caller's held slot behind a new thread.

    The slot deadlock detector is thread-owned. With workers=1, moving Chrome
    acquisition to a pool thread made its held count zero and could wait forever.
    """
    import threading
    owner = threading.get_ident()
    seen = []

    class Browser:
        def __enter__(self):
            assert threading.get_ident() == owner, "nested Chrome ownership lost to a pool thread"
            seen.append(threading.get_ident())
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(audit_layout, "Chrome", Browser)
    monkeypatch.setattr(audit_layout, "plan", lambda *args: [])
    monkeypatch.setattr(audit_layout, "selftest", lambda *args: None)
    reports = audit_layout.audit_many({"nested": "http://127.0.0.1"}, tmp_path,
                                      workers=1, self_test=True)
    assert seen == [owner] and reports["nested"]["selftest"] == "ok"
