"""TASK-24: the layout gate (scripts/audit_layout.py) on the Categorization rules card's five states (rules-list,
rules-search, rules-edit, rules-delete, rules-suggestions) with real suggestions: tests/rules_scene.py's synthetic DB
(the six gate DBs have no hand filings, so their rules-suggestions state is skipped there - here it runs). Every width,
both themes, every check (tap included on phones), 0 FAIL and nothing skipped."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import rules_scene
from cdp import Server, find_chrome
from hermes_finance import db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
RULES_STATES = {"rules-list", "rules-search", "rules-edit", "rules-delete", "rules-suggestions"}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("g24") / "finance.db"
    conn = db.connect(path)
    rules_scene.build(conn)
    conn.close()
    srv = Server(path)
    yield srv
    srv.close()


def test_the_five_rules_states_are_in_the_plan_at_every_width_in_both_themes():
    assert RULES_STATES <= audit_layout.EVERY_WIDTH
    plan = {(w, t, n) for w, t, n, *_ in audit_layout.plan("full", audit_layout.WIDTHS, RULES_STATES)}
    assert plan == {(w, t, n) for w in audit_layout.WIDTHS for t in audit_layout.THEMES for n in RULES_STATES}


def test_the_rules_states_pass_every_check_with_real_suggestions(site, tmp_path):
    lines = []
    rep = audit_layout.audit(site.url, tmp_path / "shots", "rules", audit_layout.WIDTHS, log=lines.append,
                             states=RULES_STATES, self_test=False, workers=2)
    assert not [ln for ln in lines if ln.startswith("SKIP")], [ln for ln in lines if ln.startswith("SKIP")]
    assert rep["fail"] == 0, "\n".join(ln for ln in lines if ln.startswith(("FAIL", "  ")))
    runs = sum(len(v) for v in rep["widths"].values())
    assert runs == len(audit_layout.WIDTHS) * len(audit_layout.THEMES) * len(RULES_STATES)
    assert rep["pass"] >= runs * 10
    print(f"rules states: {runs} runs, {rep['pass']} PASS, {rep['fail']} FAIL")
