"""The TASK-11 final-review gates, run on the synthetic DBs (tests/gate_dbs.py: full, nocaps, empty, history-empty, onepoint, features) at once:
Gate 1 (scripts/audit_numbers.py: every number and percentage the pages show, the API and the read-only CLI agree)
and Gate 2 (scripts/audit_layout.py: clipping, alignment, spacing, charts, text and overlays at 1440, 1024 and 390, on
every page and every modal / drawer / editor state; TASK-18: every page at 390 / 768 / 1024 / 1280 / 1440 in both
themes, the independent audit's checks after their self-test, and the built-in login's page on the two sign-in DBs)
and Gate 3 (scripts/audit_controls.py, deadctl: every visible, enabled control on every page does something you can see
when clicked; full and nocaps at 1440 and 390, after its self-test). Headless Chrome, skipped when it isn't installed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from cdp import find_chrome

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_controls  # noqa: E402
import gate_dbs  # noqa: E402
import run_gates  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")


# TASK-18: the two gates are two fixtures, so they can run as separate calls (the extended layout gate alone takes about
# 8 minutes): pytest tests/test_gates.py -k gate_1, then -k gate_2. TASK-19: GATE_KINDS=full,nocaps runs the gates on
# those kinds only (the others' tests skip), so the layout gate can be split further. Default: every kind, the sign-in
# DBs included.
ALL_KINDS = (*gate_dbs.KINDS, *gate_dbs.AUTH_KINDS)
KINDS_RUN = [k for k in os.environ.get("GATE_KINDS", ",".join(ALL_KINDS)).split(",") if k in ALL_KINDS]


def _only(kind):
    if kind not in KINDS_RUN:
        pytest.skip(f"not in GATE_KINDS ({','.join(KINDS_RUN)})")


@pytest.fixture(scope="module")
def numbers(tmp_path_factory):
    kinds = [k for k in KINDS_RUN if k in gate_dbs.KINDS]
    return {r["kind"]: r for r in run_gates.run(kinds, "numbers", tmp_path_factory.mktemp("numbers"))} if kinds else {}


@pytest.fixture(scope="module")
def gates(tmp_path_factory):
    return {r["kind"]: r for r in run_gates.run(KINDS_RUN, "layout", tmp_path_factory.mktemp("layout"))}


@pytest.fixture(scope="module")
def controls(tmp_path_factory):
    kinds = [k for k in KINDS_RUN if k in audit_controls.KINDS]
    return {r["kind"]: r for r in run_gates.run(kinds, "controls", tmp_path_factory.mktemp("controls"))} if kinds else {}


@pytest.mark.parametrize("kind", gate_dbs.KINDS)
def test_gate_1_numbers(numbers, kind):
    _only(kind)
    r = numbers[kind]
    assert r["numbers"] and len(r["numbers"]) > 80                       # it really checked
    assert not r["numbers_fail"], "\n".join(f"{f['check']}: got {f['got']!r}, want {f['want']!r}" for f in r["numbers_fail"])
    if kind == "full":  # TASK-11b: the $0.15 residual and the car loan's 3.81% APR are on screen, and checked
        got = {x["check"]: x["got"] for x in r["numbers"]}
        assert got["Nova Self-directed: account value = its holdings + cash + residual"] == 491.62
        assert got["Allocation (holding): the 'Unpriced / pending' row = the residual"] == [0.15]
        aprs = [(c, g) for c, g in got.items() if "stored APR" in c]
        assert len(aprs) >= 4 and all(g == 3.81 for _, g in aprs), aprs  # Debt group, table, payoff note, panel
        assert any(c.startswith("debt: '81% paid off'") and g == 81.0 for c, g in got.items())


@pytest.mark.parametrize("kind", (*gate_dbs.KINDS, *gate_dbs.AUTH_KINDS))
def test_gate_2_layout(gates, kind):
    _only(kind)
    rep = gates[kind]["layout"]
    assert rep["pass"] > (3000 if kind in gate_dbs.KINDS else 400)       # 5 widths x 2 themes x the pages x 14 checks
    assert rep["selftest"] == ("ok" if kind in gate_dbs.KINDS else None)  # the ported checks caught their planted bugs
    examined = {c: sum(e["checks"][c]["examined"] for states in rep["widths"].values() for e in states.values() if "checks" in e)
                for c in ("overflow", "escape", "cliptext", "wrapped", "rows", "orphans")}
    assert all(examined[c] > 0 for c in ("overflow", "escape", "cliptext")), examined   # they really looked
    assert {"1440", "1280", "1024", "768", "390"} <= set(rep["widths"])
    assert any(k.endswith("@light") for states in rep["widths"].values() for k in states)
    fails = [f"{w} {state} {check}: {v['rule']} {v['what']}" for w, states in rep["widths"].items()
             for state, e in states.items() for check, c in e.get("checks", {}).items() for v in c["violations"]]
    assert rep["fail"] == 0, "\n".join(fails[:40])


@pytest.mark.parametrize("kind", audit_controls.KINDS)
def test_gate_3_controls(controls, kind):
    """deadctl (docs/ui-invariants.md rule 21): every control clicked on every page of the plan did something."""
    _only(kind)
    rep = controls[kind]["controls"]
    assert rep["selftest"] == "ok"                                        # the planted dead controls were caught
    got = {(w, page) for w, pages in rep["widths"].items() for page in pages}
    assert got == {(str(w), page) for w in audit_controls.WIDTHS for page in audit_controls.ALL_PAGES}, got
    assert rep["probed"] > 150, rep["probed"]                             # it really clicked
    dead = [f"{w} {page}: {d['label']!r} ({d['sig']})" for w, pages in rep["widths"].items()
            for page, r in pages.items() for d in r["dead"]]
    assert rep["fail"] == 0 and not dead, "controls that did nothing you can see when clicked:\n" + "\n".join(dead)
