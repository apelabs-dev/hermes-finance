"""TASK-18: the layout gate's ported checks (the reviewer's independent audit + shoot_017.py's row check, in
scripts/audit_layout.py) are discriminating: their self-test catches a planted bug each, and on real pages each one fails
when the fix it guards is taken back out (a stylesheet injected after the page's own), then passes with the fix."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")

# The escape case guards a Settings row's sub-line that wraps (settings.css: white-space: normal; overflow-wrap:
# anywhere). Taking that out alone stopped sticking out when Settings became one full-width column (hotfix deb0246: a
# 688px card at 1024): the line's box (app.css .grow > span, a block cut with an ellipsis) keeps its column's width
# however long its text, so no element leaves the card. So the plant also puts a long unbroken path (199 characters) at
# the end of each row's first sub-line and sizes that line to its text (fit-content: its whole length when it can't
# wrap, the column when it can): with the fix it wraps inside its card (test_the_long_line_alone_stays_in_its_card),
# without it the line runs hundreds of px past the card's edge.
NO_WRAP = ".srow .grow > span { overflow-wrap: normal !important; white-space: nowrap !important; }"
LONG_LINE = (".srow .grow > b + span { width: fit-content !important; } .srow .grow > b + span::after { content: ' "
             "/home/owner/.config/application-support/hermes-finance/" + "a-folder-name-that-does-not-wrap/" * 4
             + "secrets.env'; }")

# (state, width, the old CSS, the check that must catch it)
CASES = [
    ("overview", 1440, ".nwtile h3 { flex-wrap: wrap !important; } .nwtile h3 .nwseg { flex-basis: 100% !important; }", "rows"),
    ("overview", 390, ".tabbar a { font-size: 12px !important; }", "text"),
    # (baseline K1) the narrow Subscriptions rows' name column is at least its longest word now (app.css
    # .li.wrap[data-bill]), so the badge alone moves the amount under the name instead of cutting "Subscriptio": the old
    # CSS takes both fixes out (the badge back, the name column free to shrink). (integration) TASK-24's 6em basis
    # (app.css, the same rows) stays in: with it the badge alone still cuts the category (test_task24_layout_fix)
    ("recurring", 1280, ".li.wrap .grow > span > .cb { display: inline-grid !important; } "
                        ".li.wrap[data-bill] > .grow { min-width: 0 !important; }", "cliptext"),
    ("accounts", 1440, ".cbtn { white-space: normal !important; width: 70px !important; }", "wrapped"),
    ("settings", 1024, "#page { min-width: 1800px; }", "overflow"),
    pytest.param("settings", 1024, f"{NO_WRAP} {LONG_LINE}", "escape", id="settings-1024-no-wrap-long-line-escape"),
    # (baseline K3) Settings is one full-width column since deb0246, so at 1024 the unwrapped row text fits its card and
    # escapes nothing; on a phone it still does (probed at every gate width: escapes at 430, 390 and 360 only, "Auto-apply
    # confidence" out of AI provider by 174px at 390; the page clean at all 7). (integration) Both kept: the unwrapped
    # text alone at 390, and with the long line (above) at 1024.
    # P3b debloat shortened every Settings row note below 390, so the plant also gives them a long one to not wrap
    ("settings", 390, ".srow .grow > span { overflow-wrap: normal !important; white-space: nowrap !important; } "
     ".srow .grow > span::after { content: ' and a long note that a phone has to wrap onto the next line'; }", "escape"),
]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    srv = Server(gate_dbs.build("full", tmp_path_factory.mktemp("g18") / "finance.db"))
    yield srv
    srv.close()


def test_the_self_test_passes_on_the_real_overview(site):
    lines = []
    with Chrome() as b:
        audit_layout.selftest(b, site.url, lines.append)
    assert lines and "each caught their planted bug" in lines[0]


def test_a_blind_check_stops_the_run(site, monkeypatch):
    """If a ported check stops seeing its plant (here: the orphan plant made harmless), the self-test raises."""
    monkeypatch.setattr(audit_layout, "PLANT_JS", audit_layout.PLANT_JS.replace("aaaaaaaa 29)", "aaaaaaaa bbbbbb"))
    with Chrome() as b, pytest.raises(AssertionError, match="orphans"):
        audit_layout.selftest(b, site.url, lambda line: None)


@pytest.mark.parametrize("state,width,css,check", CASES)
def test_each_ported_check_fails_without_its_fix_and_passes_with_it(site, tmp_path, state, width, css, check):
    old = audit_layout.audit(site.url, tmp_path / "old", "old", (width,), log=lambda line: None, states={state},
                             inject=css, themes=("dark",), self_test=False)
    got = [c for e in old["widths"][str(width)].values() for c, v in e["checks"].items() if v["violations"]]
    assert check in got, got
    fixed = audit_layout.audit(site.url, tmp_path / "fixed", "fixed", (width,), log=lambda line: None, states={state},
                               themes=("dark",), self_test=False)
    assert fixed["fail"] == 0


def test_the_long_line_alone_stays_in_its_card(site, tmp_path):
    """The escape case's long path, with the fix still there, wraps inside its card: what sticks out is the fix taken
    out, not the plant."""
    res = audit_layout.audit(site.url, tmp_path, "long", (1024,), log=lambda line: None, states={"settings"},
                             inject=LONG_LINE, themes=("dark",), self_test=False)
    got = [c for e in res["widths"]["1024"].values() for c, v in e["checks"].items() if v["violations"]]
    assert "escape" not in got, got


def test_the_gate_plan_covers_every_page_at_five_widths_in_both_themes():
    plan = list(audit_layout.plan("full", audit_layout.WIDTHS, None))
    for page in ("overview", "transactions", "budgets", "recurring", "accounts", "investments", "debt", "settings", "setup-1",
                 "import-csv", "goals"):
        runs = {(w, t) for w, t, name, *_ in plan if name == page}
        # (TASK-20: and at the tap check's other phones, 430 and 360, in the dark theme)
        assert runs == {(w, t) for w in (1440, 1280, 1024, 768, 390) for t in ("dark", "light")} | {(430, "dark"), (360, "dark")}, page
    editor = {(w, t) for w, t, name, *_ in plan if name == "budgets-editor"}
    assert editor == {(w, "dark") for w in (1440, 1024, 390)} | {(1440, "light"), (390, "light")}   # TASK-11's widths
    # (HOTFIX-1: and light at 1440 and 390, where its Save button was black on black)
    signin = {name for *_, name, _p, _s, _sc in [(w, t, n, p, s, sc) for w, t, n, p, s, sc in audit_layout.plan("signin", audit_layout.WIDTHS, None)]}
    assert signin == {"signin", "signin-code", "signin-recovery"}
