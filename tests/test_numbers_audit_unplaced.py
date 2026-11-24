"""The numbers audit (scripts/audit_numbers.py) has to place every percentage the pages show, and each rule it uses
must fail on a wrong figure. Three standing FAILs on the live DB (t_5a3e1a78's run, 2026-11-20) came from here:

  * the Overview Goals tile's "Splurge 20% of $500" (a Save up goal's line, no monthly goal) was placed as the
    *monthly* line, which has no data when there is no monthly goal: "a percentage the audit can't place";
  * the Investments "-$335.00 (33.5%) past month" pill sat in a card titled "Total investments", but the placing
    rule only knew a card with no title: kind unknown. (The synthetic gate DBs never have a month of snapshots, so no
    gate ever drew the pill.) The UI's base is the portfolio value at the start of the range (portfolio.py change()),
    which this recomputes from the history points instead of echoing the API's own pct;
  * "slots = every Item ever exchanged (the file)" counted items rows only; the app's slots_used (db.py) also counts
    a blocked duplicate Plaid confirmed it counted (link_attempts.outcome = 'duplicate_counted').

The first group drives the real app in headless Chrome on a synthetic DB (no real data); the rest feed the audit's own
functions made-up API answers.
"""

from __future__ import annotations

import copy
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_numbers as an  # noqa: E402
import gate_dbs  # noqa: E402
from cdp import Chrome, Server, find_chrome  # noqa: E402
from hermes_finance import db, goals  # noqa: E402

TODAY = date(2026, 11, 20)


# --- placing (the DOM reading, in the real app) ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("unplaced") / "finance.db"
    gate_dbs.build("full", path, TODAY)
    conn = db.connect(path)
    with conn:   # a month and more of snapshots, so the Investments pill has a "past month" to show
        for back in range(22, 45):
            day = (TODAY - timedelta(days=back)).isoformat()
            conn.execute("INSERT OR REPLACE INTO holding_snapshots VALUES (?, 'acc_nova_inv', 'sec_nvda', 'NVDA', 2.18176, ?, ?)",
                         (day, 200.0, 436.0))
            conn.execute("INSERT OR REPLACE INTO holding_snapshots VALUES (?, 'acc_nova_inv', 'sec_cash', 'CUR:USD', 0.57, 1, 0.57)",
                         (day,))
    # a Save up goal and no monthly one: the Goals tile shows "Splurge 20% of $500" and nothing about the month
    goals.create(conn, {"kind": "save", "name": "Splurge", "target": 500, "track": "earmark", "account_id": "acc_nova_chk",
                        "earmark": 100}, TODAY)
    conn.close()
    srv = Server(path, today=TODAY)
    srv.path = path
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    if find_chrome() is None:
        pytest.skip("Chrome is not installed")
    with Chrome() as b:
        b.size(1440, 900)
        yield b


def placed(chrome, site, page):
    chrome.goto(f"{site.url}/?t={time.time():.3f}#/{page}")
    chrome.settle()
    return chrome.js(an.DOM_COMMON + "\n(() => pcts())()")


def test_a_goals_tile_with_only_a_save_up_goal_is_the_top_goal_line(site, chrome):
    tile = [p for p in placed(chrome, site, "overview") if p["kind"] == "goals_tile"]
    assert [(p["node"], p["key"]) for p in tile] == [("20% of $500", "top")], tile


def test_the_monthly_savings_line_is_still_the_monthly_one(site, chrome):
    conn = db.connect(site.path)
    goals.create(conn, {"kind": "monthly", "target": 800}, TODAY)
    conn.close()
    tile = [p for p in placed(chrome, site, "overview") if p["kind"] == "goals_tile"]
    keys = {p["node"]: p["key"] for p in tile}
    assert [k for n, k in keys.items() if n.endswith("of $800")] == ["monthly"], tile
    assert all(k == "top" for n, k in keys.items() if not n.endswith("of $800")), tile


def test_the_investments_past_month_pill_is_a_change(site, chrome):
    pill = [p for p in placed(chrome, site, "investments") if "past month" in p["node"]]
    assert [(p["kind"], p["range"]) for p in pill] == [("change", "1m")], pill


# --- the rules (made-up API answers, no Chrome) ---------------------------------------------------------------------

# Illustrative numbers (1 month): the portfolio went from $1000.00 to $665.00, -$335.00, which is 33.5% of the
# value at the START. Not of today's header total ($680.00: 49.3%) and not of the value at the end ($665.00: 50.4%).
HISTORY = [{"date": "2026-10-20", "value": 1000.00}, {"date": "2026-11-19", "value": 760.00}, {"date": "2026-11-20", "value": 665.00}]
INV = {"total": 680.00, "history": HISTORY,
       "changes": {"1m": {"from": "2026-10-20", "to": "2026-11-20", "amount": -335.00, "pct": -33.5}}}
BP = {"budgets": {}, "categories": [], "suggestions": {"categories": []}}
DEBT = {"loans": [], "cards": [], "groups": []}
PILL = "-$335.00 (33.5%) past month"


def run(dom, inv=INV, ov=None, goals_=None):
    A = an.Audit(lambda line: None)
    an.check_percentages(A, {"investments": dom} if "pcts" in dom else dom, ov or {}, BP, DEBT, inv, {}, goals_)
    return A


def pill(text, rng="1m"):
    return {"pcts": [{"node": text, "row": "$680.00" + text, "kind": "change", "range": rng}]}


def test_the_change_pill_passes_on_the_start_of_range_base():
    A = run(pill(PILL))
    assert A.fails() == [], A.fails()
    assert any("value at the start" in r["check"] for r in A.results)


@pytest.mark.parametrize("shown", ["-$335.00 (49.3%) past month",    # divided by today's header total
                                   "-$335.00 (50.4%) past month",    # divided by the end value
                                   "-$335.00 (33.0%) past month"])   # a plain wrong percentage, matching no base
def test_the_change_pill_fails_on_another_base(shown):
    fails = [f["check"] for f in run(pill(shown)).fails()]
    assert any("past month" in f and "value at the start" in f for f in fails), fails


def test_the_change_pill_fails_when_the_api_pct_itself_is_off_the_history():
    """The UI shows the API's pct, so echoing it can't catch an API that divides by the wrong base: the start of the
    range comes from the history points. Here the API says 33.0% (and the pill shows it); the history says 33.5%."""
    inv = copy.deepcopy(INV)
    inv["changes"]["1m"]["pct"] = -33.0
    fails = [f["check"] for f in run(pill("-$335.00 (33.0%) past month"), inv).fails()]
    assert any("value at the start" in f for f in fails), fails


def test_the_change_pill_amount_is_the_api_amount():
    fails = [f["check"] for f in run(pill("-$300.00 (33.5%) past month")).fails()]
    assert any("amount" in f for f in fails), fails


def test_the_change_pill_with_other_accounts_at_the_ends_uses_the_api_pct():
    """An account added since the start moves the total by more than the change: then the base isn't in the history
    points (portfolio.py compares the same accounts at both ends), so only the API's own pct can be the reference."""
    inv = copy.deepcopy(INV)
    inv["history"][-1]["value"] = 320.00   # +6.77 from an account that wasn't there at the start
    A = run(pill(PILL), inv)
    assert A.fails() == [], A.fails()
    assert any("same accounts" in r["check"] for r in A.results)


def tile_ov(top_pct=20.02, target=500):
    return {"goals": {"top": {"name": "Splurge", "pct": top_pct, "target": target, "progress": 100.08}, "monthly": None},
            "cashflow": {"months": [{"net": 100.08}]}}


def goal_line(text, key):
    return {"pcts": [{"node": text, "row": "Splurge " + text, "kind": "goals_tile", "key": key}]}


def test_the_top_goal_line_passes_on_the_floored_progress():
    A = run({"overview": goal_line("20% of $500", "top")}, ov=tile_ov())
    assert A.fails() == [], A.fails()


def test_the_top_goal_line_fails_on_a_wrong_percentage():
    fails = [f["check"] for f in run({"overview": goal_line("21% of $500", "top")}, ov=tile_ov()).fails()]
    assert any("floored" in f for f in fails), fails


def test_a_monthly_line_without_a_monthly_goal_still_can_not_be_placed():
    """The old misplacement stays loud: a figure on a line the API has no data for is a FAIL, not a pass."""
    fails = [f["check"] for f in run({"overview": goal_line("20% of $500", "monthly")}, ov=tile_ov()).fails()]
    assert any("can't place" in f for f in fails), fails


# --- the slots rule --------------------------------------------------------------------------------------------------

def _items(conn, n, provider="plaid", removed=None):
    with conn:
        for i in range(n):
            conn.execute("INSERT INTO items (item_id, institution_name, access_token, provider, removed_at, created_at) "
                         "VALUES (?, 'Bank', 'tok', ?, ?, '2026-09-25T00:00:00Z')", (f"{provider}{i}{removed}", provider, removed))


def _attempt(conn, outcome):
    with conn:
        conn.execute("INSERT INTO link_attempts (created_at, institution_name, outcome) VALUES ('2026-10-06T00:00:00Z', 'PayPal', ?)",
                     (outcome,))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "finance.db")
    yield c
    c.close()


def test_slots_count_every_item_ever_exchanged_and_a_duplicate_plaid_counted(conn):
    _items(conn, 7)
    _items(conn, 1, removed="2026-10-01T00:00:00Z")           # removed: still a slot (Plaid Trial)
    _items(conn, 2, provider="simplefin")                      # not Plaid: no slot
    _attempt(conn, "exchanged")                                # an attempt that made an Item: counted by the Item
    _attempt(conn, "duplicate_blocked")                        # not confirmed either way: no slot
    _attempt(conn, "duplicate_counted")                        # confirmed counted by Plaid: a slot with no Item row
    assert an.file_slots(conn) == 9
    assert an.file_slots(conn) == db.slots_used(conn)           # what the app shows


def test_slots_fail_when_the_page_counts_the_items_alone(conn):
    """The standing FAIL: 8 Item rows + 1 confirmed duplicate = 9 shown. Item rows alone (the old rule) read 8."""
    _items(conn, 8)
    _attempt(conn, "duplicate_counted")
    A = an.Audit(lambda line: None)
    an.check_slots(A, 8, conn)                                  # a page that forgot the duplicate
    assert [f["check"] for f in A.fails()] == [an.SLOTS_CHECK]
    B = an.Audit(lambda line: None)
    an.check_slots(B, 9, conn)
    assert B.fails() == []
