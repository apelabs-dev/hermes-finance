"""Always-on wiring smoke: each audit_layout state must reach real synthetic UI.

One dark-theme width per distinct state/step sequence, including phone, desktop,
docked and sign-in-only states. Unlike the aggregate acceptance driver, this
doesn't turn a missing selector into an allowed skip or run the full width/DB grid.
"""
from datetime import date
from pathlib import Path
import sys

import pytest

from cdp import Chrome, Server, find_chrome
import gate_dbs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout
import run_gates

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")


def wiring_states():
    for kind in ("features", *gate_dbs.AUTH_KINDS):
        seen = set()
        for item in audit_layout.plan(kind, (390, 1440), None):
            width, theme, name, page, steps, scope = item
            key = (name, tuple(steps))
            if theme == "dark" and key not in seen:
                seen.add(key)
                yield pytest.param(kind, item, id=f"{kind}-{width}-{name}")


    # These states remain required on nocaps: exercise Set and its validation
    # at every planned width/theme, without the aggregate gate's skip conversion.
    for item in audit_layout.plan("nocaps", audit_layout.WIDTHS, {"budgets-editor", "budgets-editor-error"}):
        width, theme, name, *_ = item
        yield pytest.param("nocaps", item, id=f"nocaps-{width}-{theme}-{name}")


@pytest.fixture(scope="module")
def sites(tmp_path_factory):
    root = tmp_path_factory.mktemp("tier-wiring")
    servers = {}
    with run_gates.offline_valuation(root):
        try:
            for kind in ("features", "nocaps", "history-empty", *gate_dbs.AUTH_KINDS):
                servers[kind] = Server(gate_dbs.build(kind, root / f"{kind}.db", date.today()), today=date.today())
            yield servers
        finally:
            for server in servers.values():
                server.close()


@pytest.fixture(scope="module")
def browser():
    with Chrome() as chrome:
        yield chrome


@pytest.mark.parametrize("kind,item", list(wiring_states()))
def test_each_layout_state_reaches_its_selectors(sites, browser, kind, item):
    width, theme, name, page, steps, scope = item
    # run_state raises on a missing click/wait selector. No skip conversion.
    result = audit_layout.run_state(browser, sites[kind].url, width, name, page, steps, scope, theme=theme)
    assert not result["problems"], (kind, name, result["problems"])
    assert not result["violations"], (kind, name, result["violations"][:5])

    if kind == "nocaps":
        assert browser.js("Boolean(document.querySelector('#modal.on #sheet .bx input'))")
        if name == "budgets-editor-error":
            assert browser.js("document.querySelector('#sheet .bx input').getAttribute('aria-invalid')") == "true"
            assert browser.js("document.querySelector('#sheet .bx [role=alert]').textContent") == "Enter an amount from $0 to $100,000."


def test_sync_asked_survives_slow_settling(sites, browser, monkeypatch):
    """A busy runner may pause beyond the brief panel's four-second close timer."""
    original = browser.settled
    delayed = False

    def slow_settled():
        nonlocal delayed
        if not delayed and browser.js("Boolean(document.querySelector('#syncp:not([hidden]) .spask'))"):
            delayed = True
            browser.js("new Promise(resolve => setTimeout(resolve, 4200))")
        return original()

    monkeypatch.setattr(browser, "settled", slow_settled)
    item = next(item for item in audit_layout.plan("history-empty", (390,), {"sync-asked"}) if item[1] == "light")
    width, theme, name, page, steps, scope = item
    result = audit_layout.run_state(browser, sites["history-empty"].url, width, name, page, steps, scope, theme=theme)
    assert delayed
    assert not result["problems"]
    assert not result["violations"]
    assert browser.js("Boolean(document.querySelector('#syncp:not([hidden]) .spwait'))")


# The controls gate (scripts/audit_controls.py, deadctl): its self-test (two planted dead buttons caught, a live one
# not), and one whole page clicked through as the gate does it, on the features DB. The gate itself (every page, two
# DBs, two widths) is tests/test_gates.py::test_gate_3_controls.
def test_controls_gate_catches_its_plants(sites):
    import audit_controls
    with Chrome() as b:
        audit_controls.prepare(b)
        assert "the 2 planted dead controls were caught" in audit_controls.selftest(b, sites["features"].url)


def test_controls_gate_clicks_a_page_through(sites):
    import audit_controls
    assert not audit_controls.ALLOW, "the controls gate's allowlist is normally empty: say why each entry is right"
    with Chrome() as b:
        audit_controls.prepare(b)
        r = audit_controls.probe(audit_controls._Page(b, sites["features"].url, 1440, "goals"), "page")
    assert r["probed"] >= 3 and r["alive"] == r["probed"], r
    assert not r["dead"], r["dead"]
