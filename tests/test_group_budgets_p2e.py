"""Polish P2e: group budgets (groupbudget.py) and the one-time "Move N budgets to groups" review. API side, no browser.

A synthetic DB shaped like the owner's real caps (Groceries 400, Eating Out 150, Other Transport 800, Other Health 300,
Other Shopping 200, Entertainment 60, Subscriptions 50) with a little spending, a Netflix bill still due this month
in Subscriptions and an uncapped Gas charge in the Transport group:
  - a group budget is set, edited and removed (POST /api/budgets/group), checked like a category cap;
  - a group with a budget is ONE budget: Planned = it, Spent = every category in the group, left = budget - spent - due,
    status on spent + due; the caps inside are shown, marked `counted: false`, and never counted again in the totals;
  - "categories add up to $X" (over_note) when the caps inside add up to more than the group budget;
  - (addendum 3) a group with no budget of its own counts only its capped categories (Groceries $400, not Coffee's $80
    or its bill); once it has one, every category counts; the page and budget-status agree both ways;
  - the review (POST /api/budgets/move) is one transaction: a validation or database error writes nothing, a rerun is a
    no-op, Keep is recorded for good, the moved caps go with their rollover; all 7 caps move to 6 group budgets = $1,960;
  - finance budget-status, the Hermes tool and the MCP tool list the group budgets with the page's totals;
  - the routes keep the Host / Origin / content-type / CSRF gates; v17 and v16 databases upgrade to v18.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from agent_tools import mcp_session, plugin_ctx
from factory import basic, cap, tx
from hermes_finance import api, classify, cli, config, db, groupbudget, insights, reads, rollover, tooldefs
from hermes_finance.web import App
from test_auth_task18 import cookie_of, csrf_of, enroll, quick_hashes  # noqa: F401 (quick_hashes: the autouse fixture)

TODAY = date(2026, 10, 20)
MONTH = "2026-10"
CAPS = {"Groceries": 400, "Eating Out": 150, "Other Transport": 800, "Other Health": 300, "Other Shopping": 200,
        "Entertainment": 60, "Subscriptions": 50}
PROPOSED = {"Food": 550, "Transport": 800, "Shopping": 200, "Bills": 50, "Health": 300, "Leisure": 60}
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
NETFLIX = 15.49


def owner_caps(conn) -> None:
    """The 7 caps, this month's spending, a Netflix bill (Subscriptions) due on the 25th, Gas (uncapped, Transport)."""
    basic(conn)
    for name, value in CAPS.items():
        cap(conn, name, value)
    tx(conn, "2026-10-03", 120.0, "Trader Joe's", cat="Groceries")
    tx(conn, "2026-10-05", 60.0, "Chipotle", cat="Eating Out")
    tx(conn, "2026-10-06", 300.0, "Uber", cat="Other Transport")
    tx(conn, "2026-10-08", 90.0, "CVS", cat="Other Health")
    tx(conn, "2026-10-09", 180.0, "Amazon", cat="Other Shopping")
    tx(conn, "2026-10-10", 25.0, "AMC", cat="Entertainment")
    classify.set_category(conn, tx(conn, "2026-10-11", 40.0, "Shell", cat="Other Transport"), "Gas")
    for day in ("2026-06-25", "2026-07-25", "2026-08-25", "2026-09-25"):
        classify.set_category(conn, tx(conn, day, NETFLIX, "Netflix", cat="Entertainment"), "Subscriptions")


@pytest.fixture
def app(conn):
    owner_caps(conn)
    a = App(config.db_path(), lambda: pytest.fail("a provider was called"), "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def get(app, path):
    status, ctype, body = app.handle("GET", path, LOCAL, b"")
    assert status == 200, body
    return json.loads(body)


def post(app, path, payload, headers=POST, raw=None):
    status, _, body = app.handle("POST", path, headers, raw if raw is not None else json.dumps(payload).encode())
    return status, json.loads(body)


def page(app):
    return get(app, f"/api/budgets?month={MONTH}")


def group(d, name):
    return next(g for g in d["groups"] if g["name"] == name)


def caps_now(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT name, monthly_cap FROM categories WHERE monthly_cap IS NOT NULL")}


def state(conn):
    """Everything the move may write: the group budgets, the kept groups, the caps and the rollover switches."""
    return (sorted(map(tuple, conn.execute("SELECT group_id, amount FROM group_budgets"))),
            sorted(r[0] for r in conn.execute("SELECT group_id FROM group_budget_kept")),
            caps_now(conn), sorted(r[0] for r in conn.execute("SELECT category_id FROM category_rollover")))


# --- set / edit / remove -----------------------------------------------------------------------------------------

def test_a_group_budget_is_set_edited_and_removed(app, conn):
    before = group(page(app), "Food")
    assert (before["budget"], before["planned"], before["caps_total"], before["caps_counted"]) == (None, 550.0, 550.0, True)
    assert before["eligible"] and not before["kept"]
    status, d = post(app, "/api/budgets/group", {"group": "food", "monthly_budget": 500})
    assert status == 200 and d == {"group": "Food", "monthly_budget": 500.0}
    food = group(page(app), "Food")
    assert (food["budget"], food["planned"], food["status"]) == (500.0, 500.0, "ok")
    assert food["spent"] == 180.0                                       # Groceries 120 + Eating Out 60
    assert food["caps_total"] == 550.0 and food["caps_counted"] is False and not food["eligible"]
    status, d = post(app, "/api/budgets/group", {"group": "Food", "monthly_budget": 450.555})
    assert status == 200 and d["monthly_budget"] == 450.56 and group(page(app), "Food")["planned"] == 450.56
    status, d = post(app, "/api/budgets/group", {"group": "Food", "monthly_budget": None})
    assert status == 200 and d == {"group": "Food", "monthly_budget": None}
    food = group(page(app), "Food")
    assert (food["budget"], food["planned"], food["status"], food["caps_counted"]) == (None, 550.0, None, True)
    assert caps_now(conn) == {k: float(v) for k, v in CAPS.items()}    # the caps inside were never touched
    groupbudget.set_budget(conn, {"group": "Leisure", "monthly_budget": 0})
    created, updated = conn.execute("SELECT created_at, updated_at FROM group_budgets").fetchone()
    assert created and updated


@pytest.mark.parametrize("raw", [
    b'{"group": "Nope", "monthly_budget": 100}',
    b'{"group": "", "monthly_budget": 100}',
    b'{"group": 5, "monthly_budget": 100}',
    b'{"monthly_budget": 100}',
    b'{"group": "Food"}',
    b'{"group": "Food", "monthly_budget": -1}',
    b'{"group": "Food", "monthly_budget": NaN}',
    b'{"group": "Food", "monthly_budget": Infinity}',
    b'{"group": "Food", "monthly_budget": 100000.01}',
    b'{"group": "Food", "monthly_budget": 1e400}',
    b'{"group": "Food", "monthly_budget": 99999999999999999999999}',
    b'{"group": "Food", "monthly_budget": "250"}',
    b'{"group": "Food", "monthly_budget": true}',
    b'{"group": "Food", "monthly_budget": [250]}',
])
def test_a_bad_group_budget_writes_nothing(app, conn, raw):
    status, d = post(app, "/api/budgets/group", None, raw=raw)
    assert status == 400 and d["error"]
    assert conn.execute("SELECT COUNT(*) FROM group_budgets").fetchone()[0] == 0


def test_the_bounds_are_the_caps(app, conn):
    for value in (0, 100000):
        assert post(app, "/api/budgets/group", {"group": "Food", "monthly_budget": value}) == \
            (200, {"group": "Food", "monthly_budget": float(value)})


def test_the_routes_keep_their_gates(app, conn):
    body = {"group": "Food", "monthly_budget": 5}
    move = {"groups": [{"group": "Food", "choice": "keep"}]}
    for path, payload in (("/api/budgets/group", body), ("/api/budgets/move", move)):
        assert post(app, path, payload, headers={**POST, "Origin": "https://evil.example"})[0] == 403
        assert post(app, path, payload, headers={**POST, "Content-Type": "text/plain"})[0] == 415
        assert post(app, path, payload, headers={"Host": "evil.example:8765", "Content-Type": "application/json"})[0] == 403
        assert app.handle("GET", path, LOCAL, b"")[0] in (404, 405)
    assert state(conn)[:2] == ([], [])


def test_with_a_session_the_routes_need_the_csrf_token(conn):
    owner_caps(conn)
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    _, _, ck = enroll(a)
    headers = {**POST, **cookie_of(ck)}
    body = json.dumps({"group": "Food", "monthly_budget": 500}).encode()
    assert a.respond("POST", "/api/budgets/group", headers, body, None)[0] == 403
    assert a.respond("POST", "/api/budgets/group", {**headers, "X-CSRF-Token": "nope"}, body, None)[0] == 403
    move = json.dumps({"groups": [{"group": "Transport", "choice": "group", "amount": 800}]}).encode()
    assert a.respond("POST", "/api/budgets/move", headers, move, None)[0] == 403
    assert state(conn)[:2] == ([], [])
    token = csrf_of(a, headers)
    assert a.respond("POST", "/api/budgets/group", {**headers, "X-CSRF-Token": token}, body, None)[0] == 200
    assert a.respond("POST", "/api/budgets/move", {**headers, "X-CSRF-Token": token}, move, None)[0] == 200
    assert groupbudget.amounts(conn) == {"Food": 500.0, "Transport": 800.0}


# --- the numbers ---------------------------------------------------------------------------------------------------

def test_mixed_totals_count_each_group_once(app, conn):
    groupbudget.set_budget(conn, {"group": "Food", "monthly_budget": 500})
    d = page(app)
    b = d["budgets"]
    assert b["total_cap"] == 500 + 800 + 300 + 200 + 60 + 50 == 1910    # Food once, not Groceries + Eating Out too
    assert b["count"] == 6                                              # Food's budget + the 5 caps outside it
    spend = {c["category"]: c["spend"] for c in d["categories"]}
    due = {c["category"]: c["due"] for c in d["categories"]}
    food = ("Groceries", "Eating Out", "Coffee", "Alcohol & Bars")
    # group roll-up (2026-10-22): a group with caps and no group budget counts every category in it, so "outside" is
    # every category of the groups holding those 5 caps
    caps_out = ("Other Transport", "Other Health", "Other Shopping", "Entertainment", "Subscriptions")
    grp = {c["category"]: c["group"] for c in d["categories"]}
    outside = [n for n in grp if grp[n] in {grp[x] for x in caps_out}]
    assert set(caps_out) < set(outside)
    assert b["total_spend"] == round(sum(spend[n] for n in food) + sum(spend[n] for n in outside), 2)
    assert b["total_due"] == round(sum(due[n] for n in food) + sum(due[n] for n in outside), 2) == NETFLIX
    assert sum(spend[n] for n in outside if n not in caps_out) > 0, "the fixture has uncapped spending outside Food"
    assert b["left"] == round(b["total_cap"] - b["total_spend"] - b["total_due"], 2)
    marked = {c["category"]: (c.get("counted"), c.get("in_group_budget")) for c in b["categories"]}
    assert marked["Groceries"] == marked["Eating Out"] == (False, "Food")
    assert all(marked[n] == (None, None) for n in caps_out)
    [unit] = b["group_budgets"]
    assert (unit["group"], unit["budget"], unit["spend"], unit["caps_total"], unit["over_note"]) == ("Food", 500.0, 180.0, 550.0, 550.0)
    shown = [g for g in d["groups"] if g["budget"] is not None or g["count"]]
    assert round(sum(g["planned"] for g in shown), 2) == b["total_cap"]
    assert round(sum(g["spent"] for g in shown), 2) == b["total_spend"]
    assert round(sum(g["left"] for g in shown), 2) == b["left"]
    assert b["under"] - b["over_amount"] == pytest.approx(b["left"])
    ov = get(app, "/api/overview")["budgets"]
    assert {k: ov[k] for k in ("total_cap", "total_spend", "total_due", "left", "count", "over", "warn")} == \
        {k: b[k] for k in ("total_cap", "total_spend", "total_due", "left", "count", "over", "warn")}


def test_a_group_budget_counts_every_category_in_it_and_its_bills(app, conn):
    groupbudget.set_budget(conn, {"group": "Transport", "monthly_budget": 400})
    groupbudget.set_budget(conn, {"group": "Bills", "monthly_budget": 20})
    d = page(app)
    t, bills = group(d, "Transport"), group(d, "Bills")
    assert t["spent"] == 340.0                                          # Transport 300 + Gas 40 (uncapped)
    assert (t["left"], t["status"]) == (60.0, "warn")                   # 340 / 400 = 85%
    assert bills["due"] == NETFLIX and bills["spent"] == 0
    assert bills["left"] == round(20 - NETFLIX, 2) and bills["status"] == insights.cap_status(NETFLIX, 20) == "ok"
    groupbudget.set_budget(conn, {"group": "Bills", "monthly_budget": 15})
    bills = group(page(app), "Bills")
    assert bills["left"] == round(15 - NETFLIX, 2) and bills["status"] == "over"    # over once the bill clears
    # alerts: the group at the same thresholds, the cap inside on its own
    al = {a["key"]: a for a in insights.alerts(conn, MONTH, TODAY)}
    assert al[f"group:Bills:{MONTH}:over"]["group"] == "Bills" and "category" not in al[f"group:Bills:{MONTH}:over"]
    assert al[f"group:Bills:{MONTH}:over"]["kind"] == "cap" and al[f"group:Bills:{MONTH}:over"]["cap"] == 15.0
    assert al[f"group:Transport:{MONTH}:warn"]["left"] == 60.0
    assert f"cap:Other Transport:{MONTH}:ok" not in al                  # Other Transport's own cap: 300 / 800, no alert


def test_the_over_note(app, conn):
    for budget, note in ((500, 550.0), (550, None), (600, None), (549.99, 550.0)):
        groupbudget.set_budget(conn, {"group": "Food", "monthly_budget": budget})
        food = group(page(app), "Food")
        assert (food["over_note"], food["caps_total"]) == (note, 550.0), budget
    assert group(page(app), "Transport")["over_note"] is None           # no group budget: never a note


def test_rollover_shows_in_the_caps_sum_but_the_move_proposes_the_caps_set(app, conn):
    for day in ("2026-09-03", "2026-09-12"):
        tx(conn, day, 50.0, "Chipotle", cat="Eating Out")
    rollover.set_enabled(conn, "Eating Out", True)
    d = page(app)
    eo = next(c for c in d["categories"] if c["category"] == "Eating Out")
    assert (eo["base_cap"], eo["cap"]) == (150.0, 200.0)                # $50 unused last month rolled in
    assert group(d, "Food")["caps_total"] == 600.0                      # the Planned each row shows
    food = next(g for g in d["move"]["groups"] if g["group"] == "Food")
    assert food["amount"] == 550.0 and food["caps"] == [{"category": "Groceries", "cap": 400.0}, {"category": "Eating Out", "cap": 150.0}]


def test_with_no_group_budget_nothing_changes(app, conn):
    d = page(app)
    b = d["budgets"]
    assert b["group_budgets"] == [] and b["count"] == 7 and b["total_cap"] == 1960.0
    assert not any("counted" in c or "in_group_budget" in c for c in b["categories"])
    assert all(g["budget"] is None and g["status"] is None and g["caps_counted"] and g["over_note"] is None for g in d["groups"])
    assert all(g["planned"] == g["caps_total"] for g in d["groups"])
    s = reads.budget_status(conn, MONTH, TODAY)
    assert s["groups"] == [] and not any("counted" in c for c in s["caps"])


# --- the one-time move ---------------------------------------------------------------------------------------------

def test_the_banner_offers_the_seven_caps(app, conn):
    m = page(app)["move"]
    assert m["count"] == 7
    assert {g["group"]: g["amount"] for g in m["groups"]} == PROPOSED and sum(PROPOSED.values()) == 1960
    assert [g["group"] for g in m["groups"]] == ["Food", "Transport", "Shopping", "Bills", "Health", "Leisure"]   # taxonomy order
    food = m["groups"][0]
    assert food["caps"] == [{"category": "Groceries", "cap": 400.0}, {"category": "Eating Out", "cap": 150.0}]
    assert food["spent"] == 180.0 and next(g for g in m["groups"] if g["group"] == "Transport")["spent"] == 340.0
    assert food["rgb"]


def test_moving_everything_gives_six_group_budgets_of_1960(app, conn):
    status, r = post(app, "/api/budgets/move", {"groups": [{"group": g, "choice": "group", "amount": a} for g, a in PROPOSED.items()]})
    assert status == 200 and r["kept"] == [] and r["skipped"] == [] and r["remaining"] == 0
    assert {x["group"]: x["monthly_budget"] for x in r["moved"]} == PROPOSED
    assert groupbudget.amounts(conn) == {g: float(a) for g, a in PROPOSED.items()}
    assert caps_now(conn) == {}
    b = page(app)["budgets"]
    assert (b["count"], b["total_cap"], len(b["group_budgets"])) == (6, 1960.0, 6)
    assert page(app)["move"] == {"count": 0, "groups": []}


def test_transport_to_a_group_budget_and_food_kept(app, conn):
    rollover.set_enabled(conn, "Other Transport", True)
    choice = {"groups": [{"group": "Transport", "choice": "group", "amount": 800}, {"group": "Food", "choice": "keep"}]}
    status, r = post(app, "/api/budgets/move", choice)
    assert status == 200
    assert r["moved"] == [{"group": "Transport", "monthly_budget": 800.0, "categories": ["Other Transport"],
                         "rollover_off": ["Other Transport"]}]
    assert r["kept"] == [{"group": "Food", "categories": ["Groceries", "Eating Out"]}]
    assert r["skipped"] == [] and r["remaining"] == 4                  # Other Shopping, Subscriptions, Other Health, Entertainment
    assert groupbudget.amounts(conn) == {"Transport": 800.0} and groupbudget.kept(conn) == {"Food"}
    assert "Other Transport" not in caps_now(conn) and caps_now(conn)["Groceries"] == 400 and caps_now(conn)["Eating Out"] == 150
    assert rollover.enabled(conn) == set()                             # the moved cap's rollover is off
    d = page(app)
    assert d["move"]["count"] == 4 and [g["group"] for g in d["move"]["groups"]] == ["Shopping", "Bills", "Health", "Leisure"]
    assert group(d, "Food")["kept"] and not group(d, "Food")["eligible"]
    # a rerun is a no-op: both are skipped, nothing changes
    before = state(conn)
    status, again = post(app, "/api/budgets/move", choice)
    assert status == 200 and again["moved"] == [] and again["kept"] == []
    assert [s["group"] for s in again["skipped"]] == ["Transport", "Food"] and again["remaining"] == 4
    assert state(conn) == before


def test_keep_all_is_for_good(app, conn):
    status, r = post(app, "/api/budgets/move", {"groups": [{"group": g, "choice": "keep"} for g in PROPOSED]})
    assert status == 200 and r["remaining"] == 0 and len(r["kept"]) == 6 and r["moved"] == []
    assert caps_now(conn) == {k: float(v) for k, v in CAPS.items()}    # every cap stays
    assert page(app)["move"]["count"] == 0
    cap(conn, "Coffee", 40)                                             # a new cap in a kept group
    cap(conn, "Other Transport", 900)                                   # a cap changed in one
    cap(conn, "Other Shopping", None)
    assert page(app)["move"]["count"] == 0
    assert groupbudget.kept(conn) == set(PROPOSED)


def test_a_group_with_a_budget_is_never_offered(app, conn):
    groupbudget.set_budget(conn, {"group": "Food", "monthly_budget": 500})
    m = page(app)["move"]
    assert m["count"] == 5 and "Food" not in [g["group"] for g in m["groups"]]
    status, r = post(app, "/api/budgets/move", {"groups": [{"group": "Food", "choice": "group", "amount": 700}]})
    assert status == 200 and r["moved"] == [] and r["skipped"][0]["group"] == "Food"
    assert groupbudget.amounts(conn) == {"Food": 500.0} and caps_now(conn)["Groceries"] == 400


@pytest.mark.parametrize("payload", [
    {},
    {"groups": []},
    {"groups": "Food"},
    {"groups": [{"group": "Transport", "choice": "group", "amount": 800}, {"group": "Food", "choice": "group", "amount": -1}]},
    {"groups": [{"group": "Transport", "choice": "group", "amount": 800}, {"group": "Food", "choice": "group"}]},
    {"groups": [{"group": "Transport", "choice": "group", "amount": 800}, {"group": "Food", "choice": "group", "amount": 100000.01}]},
    {"groups": [{"group": "Transport", "choice": "group", "amount": 800}, {"group": "Food", "choice": "group", "amount": "550"}]},
    {"groups": [{"group": "Transport", "choice": "group", "amount": 800}, {"group": "Food", "choice": "maybe"}]},
    {"groups": [{"group": "Transport", "choice": "group", "amount": 800}, {"group": "Nope", "choice": "keep"}]},
    {"groups": [{"group": "Transport", "choice": "group", "amount": 800}, {"group": "transport", "choice": "keep"}]},
    {"groups": [{"group": "Transport", "choice": "group", "amount": 800}, "Food"]},
    {"groups": [{"group": "Food", "choice": "keep"}] * 101},
])
def test_any_bad_answer_writes_nothing(app, conn, payload):
    before = state(conn)
    status, d = post(app, "/api/budgets/move", payload)
    assert status == 400 and d["error"]
    assert state(conn) == before


def test_a_database_error_half_way_writes_nothing(app, conn):
    rollover.set_enabled(conn, "Other Transport", True)
    before = state(conn)
    with conn:   # Other Health's cap can't be removed: the move fails after Transport's part was written
        conn.execute("CREATE TEMP TRIGGER boom BEFORE UPDATE OF monthly_cap ON categories WHEN OLD.name = 'Other Health' "
                     "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(Exception, match="boom"):
        groupbudget.apply_move(conn, {"groups": [{"group": "Transport", "choice": "group", "amount": 800},
                                                 {"group": "Food", "choice": "keep"},
                                                 {"group": "Health", "choice": "group", "amount": 300}]})
    assert not conn.in_transaction and state(conn) == before
    conn.execute("DROP TRIGGER boom")
    # inside a caller's transaction: a savepoint, undone the same way
    with conn:
        conn.execute("CREATE TEMP TRIGGER boom BEFORE UPDATE OF monthly_cap ON categories WHEN OLD.name = 'Other Health' "
                     "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    conn.execute("BEGIN")
    with pytest.raises(Exception, match="boom"):
        groupbudget.apply_move(conn, {"groups": [{"group": "Transport", "choice": "group", "amount": 800},
                                                 {"group": "Health", "choice": "group", "amount": 300}]})
    conn.rollback()
    conn.execute("DROP TRIGGER boom")
    assert state(conn) == before


# --- budget-status, the Hermes tool, MCP ---------------------------------------------------------------------------

@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)


def test_budget_status_and_the_tools_match_the_page(app, conn, pinned, data_dir):
    groupbudget.set_budget(conn, {"group": "Food", "monthly_budget": 500})
    groupbudget.set_budget(conn, {"group": "Bills", "monthly_budget": 20})
    d = page(app)
    b = d["budgets"]
    s = reads.budget_status(conn, MONTH, TODAY)
    t = s["totals"]
    assert (t["budgeted"], t["spent"], t["due"], t["left"], t["over"]) == \
        (b["total_cap"], b["total_spend"], b["total_due"], b["left"], b["over"])
    by_group = {g["group"]: g for g in s["groups"]}
    assert list(by_group) == ["Food", "Bills"]
    for name, g in by_group.items():
        p = group(d, name)
        assert (g["budget"], g["spent"], g["due"], g["left"], g["status"]) == (p["budget"], p["spent"], p["due"], p["left"], p["status"])
        assert g["categories"] == p["categories"]
    assert by_group["Food"]["over_note"] == 550.0 and by_group["Food"]["caps"] == \
        [{"category": "Groceries", "cap": 400.0}, {"category": "Eating Out", "cap": 150.0}]
    assert by_group["Bills"]["bills_due"] == ["Netflix"]
    caps = {c["category"]: c for c in s["caps"]}
    assert (caps["Groceries"]["counted"], caps["Groceries"]["in_group_budget"]) == (False, "Food")
    assert (caps["Subscriptions"]["counted"], caps["Subscriptions"]["in_group_budget"]) == (False, "Bills")
    assert "counted" not in caps["Other Transport"] and "in_group_budget" not in caps["Other Transport"]
    uncapped = {u["category"] for u in s["uncapped"]["categories"]}
    # group roll-up: Gas is in Transport, which has a cap (Other Transport): counted there, not in uncapped
    rest = {r["group"]: r for r in s["group_rest"]}
    assert "Gas" in rest["Transport"]["categories"] and "Gas" not in uncapped
    assert not {"Groceries", "Eating Out", "Coffee", "Subscriptions"} & uncapped
    assert "Food" not in rest, "a group budget already counts every category in it"
    text = reads.budget_status_text(s)
    assert "group budget" in text and "Food: its category caps add up to $550" in text and "(in Food)" in text
    assert "Transport: also counts $" in text and " spent in categories without a cap (" in text and "Gas" in text
    # the Hermes tool and MCP give the same JSON as `finance budget-status --json`
    conn.commit()
    hermes = plugin_ctx().call("finance_budget_status", {})
    [(err, raw)] = mcp_session(config.db_path(), [("finance_budget_status", {})])[1]
    assert not err
    assert hermes == json.loads(raw) == json.loads(json.dumps(s))


# --- addendum 3: an auto group counts only its budgeted categories; a group budget counts them all ------------------

GROCERIES, COFFEE, BEANS = 150.0, 80.0, 18.0


def groceries_and_coffee(conn) -> str:
    """Only Groceries is capped ($400). Coffee, in the same Food group, has $80 spent this month and no budget, and a
    coffee-bean subscription ($18 on the 25th; returned: its last charge, to track it as a bill)."""
    basic(conn)
    cap(conn, "Groceries", 400)
    tx(conn, "2026-10-03", GROCERIES, "Trader Joe's", cat="Groceries")
    for day, amount in (("2026-10-07", 30.0), ("2026-10-14", 50.0)):
        classify.set_category(conn, tx(conn, day, amount, "Blue Bottle", cat="Eating Out"), "Coffee")
    for day in ("2026-06-25", "2026-07-25", "2026-08-25", "2026-09-25"):
        last = tx(conn, day, BEANS, "Trade Coffee", cat="Eating Out")
        classify.set_category(conn, last, "Coffee")
    return last


@pytest.fixture
def coffee_app(conn):
    beans = groceries_and_coffee(conn)
    a = App(config.db_path(), lambda: pytest.fail("a provider was called"), "127.0.0.1", 8765)
    a.today = lambda: TODAY
    # Food's categories are never detected as recurring (insights.RECURRING_EXCLUDED), so the beans are tracked by hand
    # (TASK-21): a bill still due on the 25th in Coffee
    assert post(a, "/api/bills/track", {"transaction_id": beans, "every": "1m"})[0] == 200
    return a


def _status_matches_the_page(s, d):
    """budget-status's totals are the page's, and its Food is the page's Food group: its group budget when it has one,
    else its caps (here Groceries alone) summed."""
    b, food = d["budgets"], group(d, "Food")
    t = s["totals"]
    assert (t["budgeted"], t["spent"], t["due"], t["left"], t["count"]) == \
        (b["total_cap"], b["total_spend"], b["total_due"], b["left"], b["count"])
    own = [g for g in s["groups"] if g["group"] == "Food"]
    if own:
        [g] = own
        got = (g["budget"], g["spent"], g["due"], g["left"])
    else:   # group roll-up: its caps plus its uncapped categories' spending and bills (group_rest)
        caps = [c for c in s["caps"] if c["category"] in food["categories"]]
        rest = [r for r in s["group_rest"] if r["group"] == "Food"]
        cap, spent, due = (round(sum(c[k] for c in caps), 2) for k in ("cap", "spent", "due"))
        spent, due = round(spent + sum(r["spent"] for r in rest), 2), round(due + sum(r["due"] for r in rest), 2)
        got = (cap, spent, due, round(cap - spent - due, 2))
    assert got == (food["planned"], food["spent"], food["due"], food["left"])


def test_an_auto_group_and_a_group_budget_both_count_every_category_in_them(coffee_app, conn):
    # no group budget ("auto"): Planned = the caps (Groceries 400). Group roll-up (the owner, 2026-10-22: "my unset
    # subcategories no longer count towards the group budget"): Spent / Left count Coffee's $80 and its bill too
    d = page(coffee_app)
    food, b = group(d, "Food"), d["budgets"]
    spent, due = GROCERIES + COFFEE, BEANS
    assert (food["budget"], food["planned"], food["spent"], food["due"], food["left"]) == (None, 400.0, spent, due, 152.0)
    assert (b["total_cap"], b["total_spend"], b["total_due"], b["left"], b["count"]) == (400.0, spent, due, 152.0, 1)
    assert (b["under"], b["over_amount"], b["over"]) == (152.0, 0.0, 0)   # Coffee is paid from Groceries' free room
    assert [x["merchant"] for x in b["bills_due"]] == ["Trade Coffee"]
    coffee = next(c for c in d["categories"] if c["category"] == "Coffee")
    assert (coffee["cap"], coffee["spend"], coffee["due"]) == (None, COFFEE, BEANS)   # its own numbers are unchanged
    assert "Coffee" not in {x["category"] for x in d["not_budgeted"]}
    s = reads.budget_status(conn, MONTH, TODAY)
    assert s["groups"] == [] and [c["category"] for c in s["caps"]] == ["Groceries"]
    assert s["group_rest"] == [{"group": "Food", "categories": ["Coffee"], "spent": COFFEE, "due": BEANS}]
    assert "Coffee" not in {u["category"] for u in s["uncapped"]["categories"]}       # counted in Food, not twice
    _status_matches_the_page(s, d)

    # Food's own budget, $700: every category in the group counts, Coffee's $80 and its bill still due included
    assert post(coffee_app, "/api/budgets/group", {"group": "Food", "monthly_budget": 700})[0] == 200
    d = page(coffee_app)
    food, b = group(d, "Food"), d["budgets"]
    spent, due = GROCERIES + COFFEE, BEANS
    assert (food["budget"], food["planned"], food["spent"], food["due"]) == (700.0, 700.0, spent, due)
    assert food["left"] == 700 - spent - due == 452.0
    assert (b["total_cap"], b["total_spend"], b["total_due"], b["left"], b["count"]) == (700.0, spent, due, 452.0, 1)
    s = reads.budget_status(conn, MONTH, TODAY)
    [g] = s["groups"]
    assert (g["group"], g["budget"], g["spent"], g["due"], g["left"]) == ("Food", 700.0, spent, due, 452.0)
    assert "Coffee" not in {u["category"] for u in s["uncapped"]["categories"]}       # counted in Food now, not twice
    _status_matches_the_page(s, d)


# --- group roll-up (2026-10-22): a capped group's uncapped spending, in the page's under / over split -------------------

def _coffee(conn, day, amount):
    classify.set_category(conn, tx(conn, day, amount, "Blue Bottle", cat="Eating Out"), "Coffee")


def _split(b):
    return b["left"], b["under"], b["over_amount"], b["over"]


def test_a_group_pushed_over_only_by_its_uncapped_spending_counts_once_in_over(coffee_app, conn):
    # Groceries $150 of $400 is fine on its own; Coffee $380 + its $18 bill eats the $250 free and goes $148 past it
    _coffee(conn, "2026-10-15", 300.0)
    d = page(coffee_app)
    b, food = d["budgets"], group(d, "Food")
    assert _split(b) == (-148.0, 0.0, 148.0, 1)
    assert (food["spent"], food["due"], food["left"], food["over"]) == (530.0, BEANS, -148.0, 0)   # no cap is over
    assert next(c for c in b["categories"] if c["category"] == "Groceries")["status"] == "ok"
    s = reads.budget_status(conn, MONTH, TODAY)
    assert (s["totals"]["over"], s["totals"]["over_amount"]) == (1, 148.0)
    assert "($0 left in 1 budget(s) · $148 over in 1)" in reads.budget_status_text(s)
    # now Groceries is over too ($150 of $100): the group is over once, not twice, and the rest is all past the room
    cap(conn, "Groceries", 100)
    b = page(coffee_app)["budgets"]
    assert _split(b) == (-448.0, 0.0, 448.0, 1)


def test_a_refund_in_an_uncapped_category_gives_room_back(coffee_app, conn):
    _coffee(conn, "2026-10-16", -200.0)   # Coffee nets to -$120: $102 back after its $18 bill
    b = page(coffee_app)["budgets"]
    assert (b["total_spend"], b["total_due"]) == (GROCERIES - 120.0, BEANS)
    assert _split(b) == (352.0, 352.0, 0.0, 0)


def test_a_group_with_no_cap_keeps_its_spending_in_no_budget_yet(coffee_app, conn):
    cap(conn, "Groceries", None)   # Food has no cap and no group budget: nothing rolls up, as before
    d = page(coffee_app)
    food = group(d, "Food")
    assert (food["count"], food["planned"], food["spent"], food["due"], food["left"]) == (0, 0.0, 0.0, 0.0, 0.0)
    assert d["budgets"]["group_rest"] == [] and d["budgets"]["total_spend"] == 0.0
    s = reads.budget_status(conn, MONTH, TODAY)
    assert s["group_rest"] == [] and "Coffee" in {u["category"] for u in s["uncapped"]["categories"]}


# --- v18 ------------------------------------------------------------------------------------------------------------

def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@pytest.mark.parametrize("start", [17, 16])
def test_an_older_database_upgrades_to_v18(tmp_path, monkeypatch, start):
    path = tmp_path / "synthetic.sqlite"
    with monkeypatch.context() as m:
        m.setattr(db, "_v18_schema", lambda conn: None)
        if start == 16:
            m.setattr(db, "_v17_schema", lambda conn: None)
        m.setattr(db, "SCHEMA_VERSION", start)
        conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == start
    assert not {"group_budgets", "group_budget_kept"} & _tables(conn)
    assert groupbudget.amounts(conn) == {} and groupbudget.kept(conn) == set()   # read before the upgrade: none
    owner_caps(conn)
    caps = caps_now(conn)
    conn.close()
    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
        assert {"group_budgets", "group_budget_kept"} <= _tables(conn)
        assert caps_now(conn) == caps                                   # no cap touched
        cols = {r[1] for r in conn.execute("PRAGMA table_info(group_budgets)")}
        assert cols == {"group_id", "amount", "created_at", "updated_at"}
        assert {r[1] for r in conn.execute("PRAGMA table_info(group_budget_kept)")} == {"group_id", "kept_at"}
        groupbudget.set_budget(conn, {"group": "Food", "monthly_budget": 500})
        groupbudget.apply_move(conn, {"groups": [{"group": "Health", "choice": "keep"}]})
        snap = state(conn)
        db.init_schema(conn)                                            # a rerun changes nothing
        assert state(conn) == snap and conn.execute("PRAGMA user_version").fetchone()[0] == 19
    finally:
        conn.close()


def test_a_partly_applied_v18_resumes(conn):
    owner_caps(conn)
    groupbudget.set_budget(conn, {"group": "Food", "monthly_budget": 500})
    with conn:   # stopped after the first table, before the second and the version stamp
        conn.execute("DROP TABLE group_budget_kept")
        conn.execute("PRAGMA user_version = 17")
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
    assert groupbudget.amounts(conn) == {"Food": 500.0}                 # kept as it was
    assert "group_budget_kept" in _tables(conn) and groupbudget.kept(conn) == set()


def test_a_group_budget_must_name_a_real_group(conn):
    owner_caps(conn)
    with pytest.raises(Exception):
        with conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("INSERT INTO group_budgets (group_id, amount, created_at, updated_at) VALUES (999, 5, 'now', 'now')")
    with pytest.raises(Exception):
        with conn:
            conn.execute("INSERT INTO group_budgets (group_id, amount, created_at, updated_at) "
                         "SELECT id, -1, 'now', 'now' FROM category_groups WHERE name = 'Food'")
    assert groupbudget.amounts(conn) == {}
