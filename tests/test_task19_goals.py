"""TASK-19 (Batch C): goals. Save up (a whole account, part of one, entries logged by hand), Pay off (a loan or card
from its balance when the goal was made) and Monthly savings (the month's income - spending, cash flow's numbers).

Earmarks never add up to more than the account's balance when they're set, and when the balance drops later the goals
it no longer covers are flagged short, never shrunk. Pace: "on pace for <month>" from the last 90 days, or what to save
a month to hit a month. Nothing is made without a tap, and goals never change net worth, budgets or any other total."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import factory as f
import gate_dbs
from hermes_finance import api, cli, config, db, goals, insights, manual, tooldefs
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json"}


@pytest.fixture
def home(conn):
    f.bank(conn, "item_a", "Nova")
    f.acct(conn, "acc_chk", name="Checking", mask="7385", current=3000.0)
    f.acct(conn, "acc_sav", name="Nova Savings", mask="7731", subtype="savings", current=8986.80)
    f.acct(conn, "acc_vault", name="Travel Vault", mask="4502", subtype="cash management", current=0.12)
    f.acct(conn, "acc_cc", name="Card", mask="2417", type_="credit", subtype="credit card", current=600.0, limit=5000.0)
    for back, amount in ((80, -3000.0), (40, -2000.0), (10, -1482.59), (100, -9999.0)):   # money into savings (the last: too old)
        f.tx(conn, (TODAY - timedelta(days=back)).isoformat(), amount, name="From Checking", cat="Transfers", account_id="acc_sav")
    f.tx(conn, "2026-09-03", -4800.0, "Payroll", cat="Income")
    f.tx(conn, "2026-09-05", 1200.0, "Landlord", cat="Rent & Utilities")
    f.tx(conn, "2026-09-12", 250.55, "Costco", cat="Groceries")
    loan = manual.create(conn, {"kind": "loan", "name": "Car loan", "institution": "Zenith", "subtype": "auto",
                                "balance": 5907.88, "original_amount": 30567.0, "monthly_payment": 465.10, "interest_rate": 3.81}, TODAY)
    return {"loan": f"manual:{loan['id']}"}


def by_name(conn, name):
    return next(g for g in goals.payload(conn, TODAY)["goals"] if g["name"] == name)


def test_nothing_is_made_on_its_own_and_savings_accounts_are_suggested(home, conn):
    p = goals.payload(conn, TODAY)
    assert p["goals"] == [] and p["monthly"] is None and conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0
    assert [(s["id"], s["vault"]) for s in p["suggestions"]] == [("acc_sav", False), ("acc_vault", True)]
    goals.dismiss(conn, "acc_vault", TODAY)
    assert [s["id"] for s in goals.payload(conn, TODAY)["suggestions"]] == ["acc_sav"]
    goals.create(conn, {"kind": "save", "name": "Rainy day", "target": 12000, "track": "account", "account_id": "acc_sav"}, TODAY)
    assert goals.payload(conn, TODAY)["suggestions"] == []                 # it has a goal now


def test_a_whole_account_goal_follows_its_balance_and_paces_from_90_days(home, conn):
    g = goals.create(conn, {"kind": "save", "name": "Rainy day", "target": 12000, "track": "account", "account_id": "acc_sav"}, TODAY)
    assert (g["progress"], g["pct"], g["left"], g["done"]) == (8986.80, 74.89, 3013.2, False)
    # +6,482.59 in the last 90 days (the 100-day-old move doesn't count): ~2,160.86 a month, 2 months to go
    assert g["pace"]["growth_90d"] == 6482.59 and g["pace"]["per_month"] == 2160.86
    assert (g["pace"]["on_pace_month"], g["pace"]["months_to_go"], g["pace"]["need_per_month"]) == ("2026-11", 2, None)
    with conn:
        conn.execute("UPDATE accounts SET current_balance = 9500.0 WHERE account_id = 'acc_sav'")
    assert by_name(conn, "Rainy day")["progress"] == 9500.0


def test_a_target_month_says_what_to_save_a_month(home, conn):
    g = goals.create(conn, {"kind": "save", "name": "Laptop", "target": 2000, "target_month": "2027-03", "track": "manual"}, TODAY)
    assert g["pace"]["need_per_month"] == 334 and g["pace"]["months_left"] == 6   # $2,000 over Oct..Mar, rounded up
    with pytest.raises(goals.GoalError, match="this month or a later one"):
        goals.create(conn, {"kind": "save", "name": "Late", "target": 10, "target_month": "2026-08", "track": "manual"}, TODAY)


def test_entries_add_up_withdraw_and_never_go_below_zero(home, conn):
    g = goals.create(conn, {"kind": "save", "name": "Trip", "target": 1800, "track": "manual"}, TODAY)
    goals.add_entry(conn, g["id"], {"amount": 300, "note": "Tax refund", "date": "2026-07-01"}, TODAY)
    goals.add_entry(conn, g["id"], {"amount": 250}, TODAY)
    g = goals.add_entry(conn, g["id"], {"amount": -50, "note": "Deposit back"}, TODAY)
    assert g["progress"] == 500.0 and [e["amount"] for e in g["entries"]] == [-50.0, 250.0, 300.0]
    assert g["pace"]["growth_90d"] == 500.0                                  # 2026-07-01 is within 90 days
    with pytest.raises(goals.GoalError, match="at most \\$500"):
        goals.add_entry(conn, g["id"], {"amount": -500.01}, TODAY)
    for bad, message in (({"amount": 0}, "must be a number"), ({"amount": "5"}, "must be a number"),
                         ({"amount": 5, "date": "2026-10-01"}, "today or earlier"), ({"amount": 5, "note": "x" * 201}, "up to 200")):
        with pytest.raises(goals.GoalError, match=message):
            goals.add_entry(conn, g["id"], bad, TODAY)
    other = goals.create(conn, {"kind": "save", "name": "Whole", "target": 10, "track": "account", "account_id": "acc_chk"}, TODAY)
    with pytest.raises(goals.GoalError, match="track yourself"):
        goals.add_entry(conn, other["id"], {"amount": 5}, TODAY)


def test_earmarks_never_exceed_the_balance_and_a_drop_flags_the_newest_short(home, conn):
    a = goals.create(conn, {"kind": "save", "name": "Laptop", "target": 2000, "track": "earmark", "account_id": "acc_chk",
                            "earmark": 1500}, TODAY)
    assert a["progress"] == 1500.0 and a["short"] is None and a["entries"][0]["amount"] == 1500.0
    acc = next(x for x in goals.payload(conn, TODAY)["accounts"] if x["id"] == "acc_chk")
    assert (acc["earmarked"], acc["unassigned"]) == (1500.0, 1500.0)
    with pytest.raises(goals.GoalError, match="only \\$1,500 is unassigned in Checking ••7385"):
        goals.create(conn, {"kind": "save", "name": "Too much", "target": 5000, "track": "earmark", "account_id": "acc_chk",
                            "earmark": 1500.01}, TODAY)
    b = goals.create(conn, {"kind": "save", "name": "Concert", "target": 600, "track": "earmark", "account_id": "acc_chk",
                            "earmark": 1500}, TODAY)
    with pytest.raises(goals.GoalError, match="only \\$1,500"):           # its own 1,500 plus nothing left over
        goals.edit(conn, a["id"], {"earmark": 1600}, TODAY)
    goals.edit(conn, a["id"], {"earmark": 1200}, TODAY)                      # less is always fine
    with conn:  # the balance drops under the earmarks: the newer goal is short, neither shrinks
        conn.execute("UPDATE accounts SET current_balance = 2000.0 WHERE account_id = 'acc_chk'")
    a, b = by_name(conn, "Laptop"), by_name(conn, "Concert")
    assert (a["progress"], a["short"]) == (1200.0, None)
    assert b["progress"] == 1500.0 and b["short"] == {"amount": 700.0, "covered": 800.0}
    # a whole-account goal can't share an account with earmarks, nor two whole ones one account
    with pytest.raises(goals.GoalError, match="part of this account is set aside"):
        goals.create(conn, {"kind": "save", "name": "W", "target": 10, "track": "account", "account_id": "acc_chk"}, TODAY)
    goals.create(conn, {"kind": "save", "name": "S", "target": 10, "track": "account", "account_id": "acc_sav"}, TODAY)
    for track in ("account", "earmark"):
        with pytest.raises(goals.GoalError, match="already follows this whole account"):
            goals.create(conn, {"kind": "save", "name": "T", "target": 10, "track": track, "account_id": "acc_sav", "earmark": 1}, TODAY)


def test_pay_off_counts_down_from_the_balance_it_was_made_with(home, conn):
    g = goals.create(conn, {"kind": "payoff", "account_id": home["loan"]}, TODAY)
    assert (g["name"], g["start_balance"], g["target"], g["progress"], g["remaining"]) == ("Pay off Car loan", 5907.88, 5907.88, 0.0, 5907.88)
    assert g["payoff"]["payoff_month"] and g["payoff"]["monthly_payment"] == 465.10
    mid = int(home["loan"].split(":")[1])
    manual.edit(conn, mid, {"balance": 5442.78, "balance_as_of": TODAY.isoformat()}, TODAY)
    g = by_name(conn, "Pay off Car loan")
    assert (g["progress"], g["pct"], g["remaining"]) == (465.10, 7.87, 5442.78)
    with pytest.raises(goals.GoalError, match="already"):
        goals.create(conn, {"kind": "payoff", "account_id": home["loan"]}, TODAY)
    with pytest.raises(goals.GoalError, match="loan or a card"):
        goals.create(conn, {"kind": "payoff", "account_id": "acc_sav"}, TODAY)
    card = goals.create(conn, {"kind": "payoff", "account_id": "acc_cc"}, TODAY)
    assert card["start_balance"] == 600.0 and card["account"]["group"] == "cards"


def test_monthly_savings_is_cash_flows_net_for_the_month(home, conn):
    g = goals.create(conn, {"kind": "monthly", "target": 880}, TODAY)
    cur = insights.cashflow(conn, 6, TODAY)["months"][-1]
    assert g["progress"] == cur["net"] == round(4800.0 - 1200.0 - 250.55, 2) and g["month"] == "2026-09"
    assert g["pct"] == round(g["progress"] / 880 * 100, 2) and g["done"] is True
    with pytest.raises(goals.GoalError, match="Monthly savings goal already"):
        goals.create(conn, {"kind": "monthly", "target": 100}, TODAY)
    goals.edit(conn, g["id"], {"target": 5000}, TODAY)
    assert goals.payload(conn, TODAY)["monthly"]["done"] is False


def test_the_done_state_archive_and_bring_back(home, conn):
    g = goals.create(conn, {"kind": "save", "name": "Small", "target": 500, "track": "account", "account_id": "acc_sav"}, TODAY)
    assert g["done"] and g["pct"] > 100 and g["left"] == 0.0
    goals.edit(conn, g["id"], {"archived": True}, TODAY)
    p = goals.payload(conn, TODAY, archived=True)
    assert p["goals"] == [] and [x["id"] for x in p["archived"]] == [g["id"]] and p["archived_count"] == 1
    goals.edit(conn, g["id"], {"archived": False, "target": 20000}, TODAY)
    assert by_name(conn, "Small")["done"] is False


@pytest.mark.parametrize("data, field", [
    ({"kind": "wish"}, "kind"), ({"kind": "save", "name": "", "target": 5, "track": "manual"}, "name"),
    ({"kind": "save", "name": "x" * 61, "target": 5, "track": "manual"}, "name"), ({"kind": "save", "name": "A", "target": 0, "track": "manual"}, "target"),
    ({"kind": "save", "name": "A", "target": -5, "track": "manual"}, "target"), ({"kind": "save", "name": "A", "target": "5", "track": "manual"}, "target"),
    ({"kind": "save", "name": "A", "target": 5, "track": "vault"}, "track"), ({"kind": "save", "name": "A", "target": 5, "track": "account"}, "account_id"),
    ({"kind": "save", "name": "A", "target": 5, "track": "account", "account_id": "acc_cc"}, "account_id"),
    ({"kind": "save", "name": "A", "target": 5, "track": "earmark", "account_id": "acc_chk", "earmark": -1}, "earmark"),
    ({"kind": "save", "name": "A", "target": 5, "track": "manual", "target_month": "2027-13"}, "target_month"),
    ({"kind": "payoff"}, "account_id"), ({"kind": "monthly"}, "target"), ({"kind": "monthly", "target": 20_000_000}, "target")])
def test_bad_goals_are_refused_with_the_field(home, conn, data, field):
    with pytest.raises(goals.GoalError) as e:
        goals.create(conn, data, TODAY)
    assert e.value.field == field and conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0


def test_goals_never_change_net_worth_budgets_or_any_total(data_dir):
    path = config.data_dir() / "full.db"
    gate_dbs.build("full", path, TODAY)
    conn = db.connect(path)
    pages = lambda: json.dumps([{k: v for k, v in api.overview(conn, {}, TODAY).items() if k != "goals"},  # noqa: E731
                                api.budgets_page(conn, {}, TODAY), api.accounts(conn, {}, TODAY), api.investments_page(conn, {}, TODAY),
                                api.debt_page(conn, {}, TODAY), api.recurring_page(conn, {}, TODAY)], sort_keys=True, default=str)
    before = pages()
    loan = conn.execute("SELECT id FROM manual_accounts WHERE name = 'Car Loan'").fetchone()[0]
    goals.create(conn, {"kind": "save", "name": "Rainy day", "target": 12000, "track": "account", "account_id": "acc_nova_sav"}, TODAY)
    goals.create(conn, {"kind": "save", "name": "Laptop", "target": 2000, "track": "earmark", "account_id": "acc_nova_chk", "earmark": 900}, TODAY)
    g = goals.create(conn, {"kind": "save", "name": "Trip", "target": 900, "track": "manual"}, TODAY)
    goals.add_entry(conn, g["id"], {"amount": 120}, TODAY)
    goals.create(conn, {"kind": "payoff", "account_id": f"manual:{loan}"}, TODAY)
    goals.create(conn, {"kind": "monthly", "target": 800}, TODAY)
    assert pages() == before
    tile = api.overview(conn, {}, TODAY)["goals"]
    assert tile["count"] == 5 and tile["monthly"]["target"] == 800 and tile["top"]["name"] in ("Rainy day", "Laptop", "Trip")
    conn.close()


def test_finance_goals_and_the_tool_say_the_same(home, conn, data_dir, capsys, monkeypatch):
    goals.create(conn, {"kind": "save", "name": "Rainy day", "target": 12000, "track": "account", "account_id": "acc_sav"}, TODAY)
    goals.create(conn, {"kind": "monthly", "target": 880}, TODAY)
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    assert cli.main(["goals", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [g["name"] for g in out["goals"]] == ["Rainy day", "Monthly savings"]
    assert tooldefs.run_read("finance_goals", {}, config.db_path(), TODAY) == out
    assert cli.main(["goals"]) == 0
    text = capsys.readouterr().out
    assert "#1 Rainy day [Save up · Nova Savings ••7731 (whole account)]: $8,986.80 of $12,000 (75%)" in text
    assert "on pace for 2026-11" in text and "never change net worth" in text
    with pytest.raises(tooldefs.ToolError, match="unknown argument"):
        tooldefs.run_read("finance_goals", {"month": "2026-09"}, config.db_path(), TODAY)


def test_the_routes(home, conn, client):
    app = App(config.db_path(), lambda: client)
    app.today = lambda: TODAY

    def call(method, path, body=None):
        status, _, raw = app.handle(method, path, POST if method == "POST" else LOCAL,
                                    json.dumps(body).encode() if body is not None else b"")
        return status, json.loads(raw)

    s, g = call("POST", "/api/goals", {"kind": "save", "name": "Laptop", "target": 2000, "track": "earmark", "account_id": "acc_chk",
                                       "earmark": 3500})
    assert s == 400 and g == {"error": "only $3,000 is unassigned in Checking ••7385: set aside that much or less", "field": "earmark"}
    s, g = call("POST", "/api/goals", {"kind": "save", "name": "Laptop", "target": 2000, "track": "manual"})
    assert s == 200 and g["name"] == "Laptop"
    assert call("POST", "/api/goals/entry", {"id": g["id"], "amount": 125.5})[1]["progress"] == 125.5
    assert call("POST", "/api/goals/edit", {"id": g["id"], "name": "New laptop", "target": 2200})[1]["target"] == 2200.0
    assert call("POST", "/api/goals/edit", {"id": g["id"], "track": "account"})[0] == 400
    assert call("POST", "/api/goals/edit", {"id": 999, "name": "x"})[0] == 404
    assert call("POST", "/api/goals/dismiss", {"account_id": "acc_vault"})[0] == 200
    assert call("POST", "/api/goals/dismiss", {"account_id": "nope"})[0] == 400
    page = call("GET", "/api/goals")[1]
    assert [x["name"] for x in page["goals"]] == ["New laptop"] and [s_["id"] for s_ in page["suggestions"]] == ["acc_sav"]
    assert call("GET", "/api/overview")[1]["goals"]["top"]["name"] == "New laptop"
