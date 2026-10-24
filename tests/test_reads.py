"""TASK-8b agent read commands (`finance spend`, `budget-status`, `txns`, `networth`, `bills`, `loans`, `holdings`,
`cashflow`): every --json equals the dashboard API on the same DB, filters are validated (exit 2), and running
them leaves the DB byte-identical."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import demo_data
from factory import acct, bank, cap, tx
from hermes_finance import classify, cli, config, db, insights, reads
from hermes_finance.web import App

TODAY = date(2026, 9, 26)
LOCAL = {"Host": "127.0.0.1:8765"}
V3_SQL = (Path(__file__).parent / "fixtures" / "schema_v3.sql").read_text()


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    try:
        code = cli.main(argv)
    except SystemExit as e:  # argparse's own errors (bad int, bad choice, unknown flag)
        code = e.code
    out, err = capsys.readouterr()
    return code, out, err


def cli_json(capsys, *argv: str) -> dict:
    code, out, err = run([*argv, "--json"], capsys)
    assert code == 0, err
    return json.loads(out)


def flag(conn, tid: str, note: str = "is this a gym reimbursement?") -> None:
    with conn:
        conn.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, note) "
                     "VALUES (?, 'ai-test', ?, 'flagged', ?)", (tid, db.now(), note))


@pytest.fixture
def demo(conn, client):
    """The synthetic demo DB (three banks, caps, rules, recurring bills, holdings with 30 days of snapshots, card
    liabilities, a matched Zenith loan, a paid-off loan, a car), plus an AI flag and a user decision."""
    demo_data.build(conn, TODAY)
    demo_data.extras(conn, TODAY, holding_days=30)
    flag(conn, conn.execute("SELECT transaction_id FROM transactions WHERE name = 'Smile Dental'").fetchone()[0])
    classify.set_category(conn, conn.execute("SELECT transaction_id FROM transactions WHERE merchant_name = 'Costco' "
                                             "ORDER BY date DESC LIMIT 1").fetchone()[0], "Other Shopping", "user")
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    app.today = lambda: TODAY
    return app


def api(app, path: str) -> dict:
    status, _, body = app.handle("GET", path, LOCAL, b"")
    assert status == 200, body[:200]
    return json.loads(body)


# --- parity: each command's --json equals the dashboard API ------------------------------------------------

def test_spend_equals_overview_summary_categories_and_cashflow(demo, capsys):
    ov = api(demo, "/api/overview")
    out = cli_json(capsys, "spend", "--by", "category")
    s = ov["summary"]
    assert out["period"] == {"kind": "month", "month": "2026-09", "start": "2026-09-01", "end": "2026-09-30",
                             "days": 30}
    assert out["total"] == s["mtd"] == ov["pace"]["mtd"] == ov["cashflow"]["months"][-1]["spending"]
    assert out["total"] > 0 and out["count"] > 0
    assert out["previous"]["start"] == "2026-08-01" and out["previous"]["end"] == "2026-08-26"
    assert out["previous"]["total"] == s["prev_same_day"]                         # "vs same day last month"
    assert out["change"]["amount"] == s["delta"]
    assert out["pending"] == {"total": s["pending_spend"], "count": s["pending_count"]}
    page = {c["category"]: (c["spend"], c["count"]) for c in ov["categories"] if c["count"]}
    assert {g["key"]: (g["total"], g["count"]) for g in out["groups"]} == page
    assert out["group_count"] == len(page) and [g["total"] for g in out["groups"]] == sorted(
        (g["total"] for g in out["groups"]), reverse=True)


def test_spend_for_a_past_month_equals_that_months_page(demo, capsys):
    ov = api(demo, "/api/overview?month=2026-08")
    out = cli_json(capsys, "spend", "--month", "2026-08", "--by", "category")
    assert out["total"] == ov["summary"]["mtd"]
    assert out["previous"]["total"] == ov["summary"]["prev_same_day"] == ov["summary"]["prev_total"]  # all of July
    cats = {c["category"]: c for c in ov["categories"]}
    for g in out["groups"]:
        assert (g["total"], g["count"], g["previous"]) == (cats[g["key"]]["spend"], cats[g["key"]]["count"],
                                                           cats[g["key"]]["last_month"])


def test_spend_by_month_equals_cashflow_and_category_filter_equals_its_card(demo, capsys):
    flow = api(demo, "/api/overview")["cashflow"]["months"]
    out = cli_json(capsys, "spend", "--by", "month", "--since", "2026-04-01", "--until", "2026-09-30")
    assert [(g["key"], g["total"]) for g in out["groups"]] == [(m["month"], m["spending"]) for m in flow]
    assert out["total"] == round(sum(m["spending"] for m in flow), 2)
    cats = {c["category"]: c for c in api(demo, "/api/budgets")["categories"]}
    for name in ("eating out", "OTHER HEALTH", "Rent & Utilities"):
        one = cli_json(capsys, "spend", "--category", name)
        c = cats[one["filters"]["category"]]
        assert (one["total"], one["count"]) == (c["spend"], c["count"])
    by_account = cli_json(capsys, "spend", "--by", "account")
    assert round(sum(g["total"] for g in by_account["groups"]), 2) == by_account["total"]
    assert all("••" in g["label"] for g in by_account["groups"])


def test_budget_status_equals_budgets_page_and_pace(demo, capsys):
    bp, ov = api(demo, "/api/budgets"), api(demo, "/api/overview")
    out = cli_json(capsys, "budget-status")
    b = bp["budgets"]
    assert [(c["category"], c["cap"], c["spent"], c["pct"], c["status"], c["count"]) for c in out["caps"]] == \
        [(c["category"], c["cap"], c["spend"], c["pct"], c["status"], c["count"]) for c in b["categories"]]
    assert out["totals"] == {"budgeted": b["total_cap"], "spent": b["total_spend"], "due": b["total_due"],
                             "left": b["left"], "per_day": b["per_day"], "count": b["count"], "over": b["over"],
                             "warn": b["warn"], "under": b["under"], "under_count": b["under_count"],
                             "over_amount": b["over_amount"],
                             "bills_due": [{k: x[k] for k in ("merchant", "amount", "next_expected", "category")}
                                           for x in b["bills_due"]]}
    assert out["totals"]["left"] == ov["budgets"]["left"]                              # the Overview tile
    # TASK-11 (c, d): the net, with overspending and the capped categories' bills still due counted against it
    t = out["totals"]
    assert t["left"] == round(t["budgeted"] - t["spent"] - t["due"], 2) == round(t["under"] - t["over_amount"], 2)
    assert t["per_day"] == round(max(t["left"], 0) / 4, 2)
    assert t["due"] == 465.10 and [x["merchant"] for x in t["bills_due"]] == ["Zenith Financial Services"]
    assert out["days_left"] == b["days_left"] == ov["summary"]["days_left"] == 4
    assert all(c["left"] == round(c["cap"] - c["spent"] - c["due"], 2) and c["days_left"] == 4 for c in out["caps"])
    assert {c["status"] for c in out["caps"]} >= {"over", "ok"}                       # Other Health is over in the demo
    assert out["spending"] == {"total": ov["pace"]["mtd"], "projection": ov["pace"]["projection"],
                               "daily_pace": ov["pace"]["daily_pace"], "bills_due_total": ov["pace"]["bills_due_total"],
                               "prev_month_total": ov["pace"]["prev_total"]}
    rolled = {n for r in b["group_rest"] for n in r["categories"]}   # group roll-up: counted in their group
    uncapped = [c for c in bp["categories"] if c["cap"] is None and c["category"] not in rolled]
    assert out["uncapped"]["categories"] == [{"category": c["category"], "spent": c["spend"], "count": c["count"]}
                                             for c in uncapped]
    assert out["uncapped"]["spent"] == round(sum(c["spend"] for c in uncapped), 2)
    assert round(out["uncapped"]["spent"] + out["totals"]["spent"], 2) == out["spending"]["total"]
    assert all(c["projection"] >= c["spent"] for c in out["caps"])  # no refunds in capped categories this month
    past = cli_json(capsys, "budget-status", "--month", "2026-08")
    assert past["days_left"] == 0 and past["totals"]["per_day"] is None
    assert all(c["projection"] == c["spent"] for c in past["caps"])                  # a finished month is its total


@pytest.mark.parametrize("cli_args,query", [
    ((), ""),
    (("--month", "2026-09"), "month=2026-09"),
    (("--month", "2026-08", "--category", "eating out"), "month=2026-08&category=Eating%20Out"),
    (("--merchant", "amazon"), "q=amazon"),
    (("--merchant", "orbitex", "--limit", "500"), "q=orbitex&limit=500"),
    (("--limit", "3"), "limit=3"),
])
def test_txns_equal_transactions_page(demo, capsys, cli_args, query):
    page = api(demo, f"/api/transactions?{query}")
    out = cli_json(capsys, "txns", *cli_args)
    assert out["total"] == page["total"] > 0 and out["counts"] == page["counts"]
    keys = ("transaction_id", "date", "name", "merchant", "amount", "category", "category_source", "pending",
            "ai_flagged", "account_name", "account_mask", "institution_name")
    assert [tuple(r[k] for k in keys) for r in out["rows"]] == [tuple(r[k] for k in keys) for r in page["rows"]]
    assert all(r["account"].endswith(f"••{r['account_mask']}") for r in out["rows"])


def test_txns_flagged_and_uncategorized(demo, capsys):
    review = api(demo, "/api/transactions?kind=review&limit=500")["rows"]
    flagged = cli_json(capsys, "txns", "--flagged", "--limit", "500")
    assert [r["transaction_id"] for r in flagged["rows"]] == [r["transaction_id"] for r in review if r["ai_flagged"]]
    assert [r["merchant"] for r in flagged["rows"]] == ["Smile Dental"]
    assert flagged["rows"][0]["ai_note"] == "is this a gym reimbursement?" and flagged["rows"][0]["review"] is True
    unc = cli_json(capsys, "txns", "--uncategorized", "--limit", "500")
    unc_ids = [r["transaction_id"] for r in unc["rows"]]
    assert unc_ids == [r["transaction_id"] for r in review if r["transaction_id"] in unc_ids]   # the same rule
    assert all(r["ai_flagged"] for r in review if r["transaction_id"] not in unc_ids)           # the rest: flagged
    assert unc["total"] > 0 and all(r["category"] == "Other" for r in unc["rows"])
    both = cli_json(capsys, "txns", "--uncategorized", "--flagged")
    assert both["total"] == 0                                         # the flagged row is Other Health: filters narrow


def test_networth_equals_accounts_page(demo, capsys):
    acc = api(demo, "/api/accounts")
    nw = acc["net_worth"]
    out = cli_json(capsys, "networth", "--history")
    assert (out["assets"], out["debts"], out["net"]) == (nw["assets"], nw["debts"], nw["net"])
    assert [(g["key"], g["label"], g["count"], g["total"], g["signed"]) for g in out["groups"]] == \
        [(g["key"], g["label"], g["count"], g["total"], g["signed"]) for g in nw["breakdown"]]
    assert out["history"] == nw["history"] and len(out["history"]) == 14
    assert out["manual"] == nw["manual"] and out["tracking_since"] == nw["tracking_since"]
    for g in out["groups"]:
        assert len(g["accounts"]) == g["count"]
        assert round(sum(a["balance"] for a in g["accounts"]), 2) == g["total"]
        assert round(sum(a["signed"] for a in g["accounts"]), 2) == g["signed"]
    manual = [a for g in out["groups"] for a in g["accounts"] if a["source"] == "manual"]
    assert sorted(a["id"] for a in manual) == sorted(m["id"] for m in acc["manual"])
    vehicles = next(g for g in out["groups"] if g["key"] == "vehicles")["accounts"]
    assert [(a["name"], a["stale"]) for a in vehicles] == [("2019 Zenith Mesa", True)]
    cash = next(g for g in out["groups"] if g["key"] == "cash")["accounts"]
    assert [a["balance"] for a in cash] == sorted((a["balance"] for a in cash), reverse=True)  # as the page sorts
    assert cli_json(capsys, "networth")["history"] is None


def test_bills_equal_recurring_page_and_overview_upcoming(demo, capsys):
    rec, ov = api(demo, "/api/recurring"), api(demo, "/api/overview")
    out = cli_json(capsys, "bills", "--next", "30d")
    fields = ("merchant", "key", "kind", "category", "amount", "cadence", "next_expected", "days_until", "status",
              "paid_this_month", "monthly_amount")
    assert [tuple(b[k] for k in fields) for b in out["items"]] == \
        [tuple(r[k] for k in fields) for r in rec["items"] if r["days_until"] <= 30]
    assert [b["key"] for b in out["items"] if b["days_until"] >= 0] == [u["key"] for u in ov["upcoming"]]
    assert out["monthly_totals"] == rec["monthly_totals"] and out["recurring_count"] == rec["count"]
    assert out["until"] == "2026-10-26"
    soon = cli_json(capsys, "bills")                                                    # default: next 14 days
    assert soon["next_days"] == 14 and all(b["days_until"] <= 14 for b in soon["items"])
    assert {"Zenith Financial Services", "OpenAI"} <= {b["merchant"] for b in soon["items"]}
    assert "logo_url" not in soon["items"][0]


def test_loans_equal_debt_page(demo, capsys):
    d = api(demo, "/api/debt")
    out = cli_json(capsys, "loans")
    assert [(l["id"], l["name"], l["remaining"], l["apr"], l["monthly_payment"], l["payoff"], l["last_payment"],
             l["paid_pct"], l["paid_off"], l["next_payment"]) for l in out["loans"]] == \
        [(l["id"], l["name"], l["balance"], l["apr"], l["monthly_payment"], l["payoff"], l["last_payment"],
          l["paid_pct"], l["paid_off"], l["next_payment"]) for l in d["loans"]]
    assert out["monthly"] == d["monthly"]["loans"]
    zenith = next(l for l in out["loans"] if l["name"] == "Zenith")
    assert zenith["remaining"] == round(5910.0 - 465.10, 2) and zenith["manual"] and zenith["auto_updates"]
    assert zenith["last_payment"] == {"date": "2026-08-29", "amount": 465.10, "name": "Zenith Financial Services"}
    assert zenith["payoff"]["method"] == "simple" and zenith["payoff"]["payoff_month"]
    assert out["total"] == zenith["remaining"]                                          # the other one is paid off
    assert next(l for l in out["loans"] if l["name"] == "Other loan")["paid_off"] is True


def test_loans_include_a_loan_the_bank_reports(ledger, capsys):
    acct(ledger, "plaid_loan", "item_a", name="Student Loan", type_="loan", subtype="student", current=8000.0,
         mask="7788")
    with ledger:
        ledger.execute("INSERT INTO liabilities (account_id, kind, item_id, minimum_payment_amount, "
                       "next_payment_due_date, interest_rate, updated_at) VALUES "
                       "('plaid_loan', 'student', 'item_a', 120.0, '2026-10-15', 5.5, ?)", (db.now(),))
    out = cli_json(capsys, "loans")
    (l,) = out["loans"]
    assert (l["manual"], l["name"], l["type"], l["remaining"], l["apr"], l["monthly_payment"]) == \
        (False, "Student Loan", "student", 8000.0, 5.5, 120.0)
    assert l["payoff"]["method"] == "amortized" and l["last_payment"] is None and l["auto_updates"] is False
    assert (out["total"], out["monthly"]) == (8000.0, 120.0)
    code, text, _ = run(["loans"], capsys)
    assert code == 0 and "Student Loan [plaid]" in text


def test_holdings_equal_investments_page(demo, capsys):
    inv = api(demo, "/api/investments")
    out = cli_json(capsys, "holdings")
    assert out["total"] == inv["total"] == 491.08 and out["holdings_total"] == inv["holdings_total"]
    assert [(a["id"], a["value"]) for a in out["accounts"]] == [(a["id"], a["value"]) for a in inv["accounts"]]
    assert [(h["ticker"], h["value"], h["pct"]) for h in out["holdings"]] == \
        [(h["ticker"], h["value"], h["pct"]) for h in inv["holdings"]]
    assert out["changes"] == inv["changes"] and out["changes"]["7d"] is not None
    assert out["breakdown"] == inv["breakdown"] and out["prices_as_of"] == inv["prices_as_of"]
    assert [a["pct"] for a in out["accounts"]] == [100.0]


def test_cashflow_equals_overview(demo, capsys):
    flow = api(demo, "/api/overview")["cashflow"]
    out = cli_json(capsys, "cashflow")
    assert out["months"] == flow["months"] and out["first_dates"] == flow["first_dates"]
    year = cli_json(capsys, "cashflow", "--months", "12")
    assert len(year["months"]) == 12 and year["months"][-6:] == flow["months"]


# --- non-vacuity: a transfer and an income row never count as spend ---------------------------------------

@pytest.fixture
def ledger(conn):
    bank(conn, "item_a", "Nova")
    acct(conn, "chk", "item_a", name="Nova Checking", mask="7385", current=2400.0)
    acct(conn, "cc", "item_a", name="Freedom Unlimited", type_="credit", subtype="credit card", current=300.0,
         limit=5000.0, mask="3591")
    tx(conn, "2026-09-02", 71.01, "Amazon", cat="Other Shopping", account_id="chk", tid="s_amz")
    tx(conn, "2026-09-05", 55.0, "Chipotle", cat="Eating Out", account_id="cc", pending=True, tid="s_chip")
    tx(conn, "2026-09-10", -20.0, "Amazon", name="AMAZON REFUND", cat="Other Shopping", account_id="cc", tid="s_refund")
    tx(conn, "2026-09-07", 500.0, "To Savings - 7731", cat="Transfers", account_id="chk", tid="s_xfer")
    tx(conn, "2026-09-15", -2353.10, None, name="Electronic Deposit Orbitex", cat="Income", account_id="chk",
       tid="s_pay")
    tx(conn, "2026-09-20", 600.0, None, name="Chase Credit Card Payment", cat="Card payment", account_id="chk",
       tid="s_ccpay")
    tx(conn, "2026-09-21", 9.0, None, name="246810 ORBITEX L", cat="Other", account_id="chk", tid="s_other")
    tx(conn, "2026-08-20", 300.0, "Amazon", cat="Other Shopping", account_id="chk", tid="s_aug")
    tx(conn, "2026-08-25", 40.0, "Chipotle", cat="Eating Out", account_id="cc", tid="s_aug2")
    flag(conn, "s_other")
    return conn


def test_transfer_and_income_rows_are_not_spend(ledger, capsys):
    rows = {r["transaction_id"]: r for r in cli_json(capsys, "txns", "--month", "2026-09")["rows"]}
    assert rows["s_xfer"]["is_transfer"] and rows["s_ccpay"]["is_transfer"] and rows["s_pay"]["is_income"]  # in period
    everything = round(sum(r["amount"] for r in rows.values()), 2)
    out = cli_json(capsys, "spend", "--by", "category")
    assert out["total"] == round(71.01 + 55.0 - 20.0 + 9.0, 2) == 115.01 != everything
    assert out["count"] == 4
    assert {g["key"] for g in out["groups"]} == {"Other Shopping", "Eating Out", "Other"}
    assert next(g for g in out["groups"] if g["key"] == "Other Shopping")["total"] == 51.01   # the refund nets
    for merchant in ("To Savings", "Electronic Deposit", "Card Payment"):
        m = cli_json(capsys, "spend", "--merchant", merchant)
        assert (m["total"], m["count"]) == (0.0, 0)
    assert cli_json(capsys, "txns", "--merchant", "To Savings")["total"] == 1
    by_merchant = cli_json(capsys, "spend", "--by", "merchant")
    assert {g["label"] for g in by_merchant["groups"]} == {"Amazon", "Chipotle", "246810 ORBITEX L"}
    code, _, err = run(["spend", "--category", "transfers"], capsys)
    assert code == 2 and "not a spending category" in err


# --- filter semantics ------------------------------------------------------------------------------------

def test_period_filters_and_previous_period(ledger, capsys):
    month = cli_json(capsys, "spend")                   # no period: this month vs Aug 1-26
    assert month["previous"]["total"] == 340.0 and month["change"] == {"amount": -224.99, "pct": -66.17}
    week = cli_json(capsys, "spend", "--since", "7d")
    assert week["period"] == {"kind": "range", "month": None, "start": "2026-09-20", "end": "2026-09-26", "days": 7}
    assert (week["total"], week["count"]) == (9.0, 1)
    assert week["previous"] == {"kind": "range", "month": None, "start": "2026-09-13", "end": "2026-09-19",
                                "days": 7, "total": 0.0, "count": 0}
    assert week["change"] == {"amount": 9.0, "pct": None}
    rng = cli_json(capsys, "spend", "--since", "2026-09-01", "--until", "2026-09-05")
    assert (rng["total"], rng["previous"]["start"], rng["previous"]["end"]) == (126.01, "2026-08-27", "2026-08-31")
    open_start = cli_json(capsys, "spend", "--until", "2026-08-31")
    assert open_start["total"] == 340.0 and open_start["previous"] is None and open_start["change"] is None
    assert cli_json(capsys, "txns")["total"] == 9                                          # txns: all history
    assert cli_json(capsys, "txns", "--since", "2026-09-20")["total"] == 2


def test_account_merchant_category_search_filters(ledger, capsys):
    card = cli_json(capsys, "spend", "--account", "3591")
    assert card["total"] == 35.0 and [a["name"] for a in card["filters"]["account"]["matched"]] == ["Freedom Unlimited"]
    assert cli_json(capsys, "spend", "--account", "freedom unlimited")["total"] == 35.0      # by name
    assert cli_json(capsys, "spend", "--account", "nova")["total"] == 80.01                 # part of the name
    assert {r["transaction_id"] for r in cli_json(capsys, "txns", "--account", "chk")["rows"]} == \
        {"s_amz", "s_xfer", "s_pay", "s_ccpay", "s_other", "s_aug"}                          # by id
    assert {r["transaction_id"] for r in cli_json(capsys, "txns", "--merchant", "orbitex")["rows"]} == \
        {"s_pay", "s_other"}                                                                 # name, no merchant_name
    assert cli_json(capsys, "spend", "--category", "EATING OUT")["total"] == 55.0
    assert cli_json(capsys, "txns", "--category", "eating out")["total"] == 2
    assert {r["transaction_id"] for r in cli_json(capsys, "txns", "--search", "eating")["rows"]} == \
        {"s_chip", "s_aug2"}                                                                 # category
    assert [r["transaction_id"] for r in cli_json(capsys, "txns", "--search", "2353.10")["rows"]] == ["s_pay"]
    assert [r["transaction_id"] for r in cli_json(capsys, "txns", "--flagged")["rows"]] == ["s_other"]
    classify.set_category(ledger, "s_other", "Fees", "user")          # a user decision clears flag and bucket
    assert cli_json(capsys, "txns", "--flagged")["total"] == 0
    assert cli_json(capsys, "txns", "--uncategorized")["total"] == 0
    two = cli_json(capsys, "spend", "--by", "merchant", "--limit", "1")
    assert len(two["groups"]) == 1 and two["group_count"] == 3 and two["groups"][0]["label"] == "Chipotle"  # Amazon nets to 51.01


def test_text_output(ledger, capsys):
    cap(ledger, "Other Shopping", 40)
    code, out, _ = run(["spend"], capsys)
    assert code == 0 and "Spending 2026-09: $115.01 in 4 transaction(s), $55 of it pending" in out
    code, out, _ = run(["budget-status"], capsys)
    assert code == 0 and "Other Shopping" in out and "OVER" in out
    code, out, _ = run(["txns", "--flagged"], capsys)
    assert code == 0 and "246810 ORBITEX L" in out and "flagged" in out
    for argv in (["networth"], ["bills"], ["loans"], ["holdings"], ["cashflow"]):
        code, out, err = run(argv, capsys)
        assert code == 0 and out and not err


# --- validation: exit 2 with a clear message, nothing printed to stdout ------------------------------------

@pytest.mark.parametrize("argv,message", [
    (["spend", "--category", "Nope"], "unknown category 'Nope'; categories: "),
    (["txns", "--category", "yachts"], "unknown category 'yachts'"),
    (["spend", "--category", "income"], "Income is not a spending category"),
    (["spend", "--month", "2026-9"], "--month must be YYYY-MM"),
    (["spend", "--month", "2026-13"], "--month must be YYYY-MM"),
    (["txns", "--month", "Sept"], "--month must be YYYY-MM"),
    (["budget-status", "--month", "2026/09"], "--month must be YYYY-MM"),
    (["txns", "--limit", "0"], "--limit must be between 1 and 500"),
    (["txns", "--limit", "501"], "--limit must be between 1 and 500"),
    (["spend", "--by", "merchant", "--limit", "-5"], "--limit must be between 1 and 500"),
    (["spend", "--since", "30"], "--since must be Nd"),
    (["spend", "--since", "0d"], "--since must be between 1d and 3660d"),
    (["spend", "--since", "3661d"], "--since must be between 1d and 3660d"),
    (["spend", "--since", "2026-02-30"], "--since must be a real date"),
    (["txns", "--until", "yesterday"], "--until must be a date as YYYY-MM-DD"),
    (["spend", "--since", "2026-09-10", "--until", "2026-09-01"], "--since 2026-09-10 is after --until 2026-09-01"),
    (["spend", "--since", "2026-10-01"], "--since 2026-10-01 is after today 2026-09-26"),
    (["txns", "--month", "2026-09", "--since", "7d"], "--month can't be combined with --since/--until"),
    (["txns", "--account", "0000"], "no account matches '0000'"),
    (["txns", "--account", " "], "--account can't be empty"),
    (["spend", "--merchant", "  "], "--merchant can't be empty"),
    (["txns", "--search", "x" * 201], "--search is too long"),
    (["bills", "--next", "2w"], "--next must be a number of days"),
    (["bills", "--next", "731d"], "--next must be between 0d and 730d"),
    (["cashflow", "--months", "0"], "--months must be between 1 and 120"),
    (["cashflow", "--months", "121"], "--months must be between 1 and 120"),
    # review: these passed validation, then crashed with a traceback (exit 1)
    (["spend", "--month", "1900-01"], "--month must be YYYY-MM"),                 # its previous month is 1899-12
    (["budget-status", "--month", "1900-01"], "--month must be YYYY-MM"),
    (["txns", "--month", "２０２６-01"], "--month must be YYYY-MM"),                 # full-width digits match \d
    (["budget-status", "--month", "２０２６-01"], "--month must be YYYY-MM"),        # exited 0 with made-up data
    (["spend", "--since", "0001-01-01"], "--since is too far back"),                 # previous period overflowed
    (["spend", "--since", "0500-01-01", "--until", "0500-01-05"], "--since is too far back"),
    (["txns", "--until", "２０２６-09-01"], "--until must be a date as YYYY-MM-DD"),
])
def test_bad_arguments_exit_2(ledger, capsys, argv, message):
    for extra in ([], ["--json"]):
        code, out, err = run(argv + extra, capsys)
        assert code == 2 and out == ""
        assert err.startswith("error: ") and message in err, err


@pytest.mark.parametrize("argv", [["txns", "--limit", "abc"], ["spend", "--by", "day"],
                                  ["networth", "--month", "2026-09"], ["loans", "--category", "Health"]])
def test_argparse_errors_exit_2(ledger, capsys, argv):
    code, out, err = run(argv, capsys)
    assert code == 2 and out == "" and "error:" in err


def test_widest_range_has_no_previous_period_instead_of_crashing(ledger, capsys):
    out = cli_json(capsys, "spend", "--since", "1900-01-01", "--until", "9999-12-31")
    assert out["total"] == 455.01 and out["previous"] is None and out["change"] is None


def test_an_unnamed_account_matches_the_name_it_is_shown_under(ledger, capsys):
    acct(ledger, "acc_null", "item_a", name=None, mask=None)
    tx(ledger, "2026-09-03", 12.0, "Kiosk", cat="Other Shopping", account_id="acc_null", tid="s_null")
    out = cli_json(capsys, "txns", "--account", "account")
    assert [r["transaction_id"] for r in out["rows"]] == ["s_null"] and out["rows"][0]["account"] == "Account"
    code, _, err = run(["txns", "--account", "nope"], capsys)
    assert code == 2 and "Account (acc_null)" in err                       # listed with an id that works


def test_limit_bounds_are_inclusive(ledger, capsys):
    assert cli_json(capsys, "txns", "--limit", "500")["count"] == 9
    one = cli_json(capsys, "txns", "--limit", "1")
    assert one["count"] == 1 and one["total"] == 9 and one["limit"] == 1


def test_every_command_documents_its_json(capsys):
    for name in ("spend", "budget-status", "txns", "networth", "bills", "loans", "holdings", "cashflow"):
        code, out, _ = run([name, "--help"], capsys)
        assert code == 0 and "JSON: {today" in out and "Read-only" in out and "%%" not in out


# --- read-only ---------------------------------------------------------------------------------------------

ALL_COMMANDS = [
    ["spend"], ["spend", "--by", "category"], ["spend", "--by", "merchant", "--since", "90d"],
    ["spend", "--by", "month", "--since", "2026-01-01", "--until", "2026-09-30"],
    ["spend", "--by", "account", "--month", "2026-08", "--category", "other shopping"],
    ["spend", "--merchant", "amazon", "--account", "3591"],
    ["budget-status"], ["budget-status", "--month", "2026-08"],
    ["txns"], ["txns", "--search", "zenith", "--limit", "500"], ["txns", "--flagged"], ["txns", "--uncategorized"],
    ["networth"], ["networth", "--history"], ["networth", "--custom"], ["networth", "--custom", "--history"],
    ["networth", "--cash-only"], ["networth", "--cash-only", "--history"], ["bills"], ["bills", "--next", "60d"], ["loans"], ["holdings"],
    ["cashflow"], ["cashflow", "--months", "24"],
]


def test_every_command_leaves_the_db_byte_identical(demo, conn, client, capsys, monkeypatch):
    conn.close()                                    # checkpoint: everything is in finance.db itself now
    path = config.db_path()
    wal = path.with_name(path.name + "-wal")
    assert not wal.exists()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    mtime = path.stat().st_mtime_ns

    def no_writer(*a, **k):
        raise AssertionError("a read command opened the database read-write")

    def no_plaid(*a, **k):
        raise AssertionError("a read command built a Plaid client")

    monkeypatch.setattr(db, "connect", no_writer)
    monkeypatch.setattr(cli, "_client", no_plaid)
    for argv in ALL_COMMANDS:
        for extra in ([], ["--json"]):
            code, out, err = run(argv + extra, capsys)
            assert code == 0 and out and not err, (argv, err)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert path.stat().st_mtime_ns == mtime
    assert not wal.exists() or wal.stat().st_size == 0                      # nothing written, not even to the WAL


def test_missing_database_is_not_created(data_dir, capsys):
    code, out, err = run(["spend"], capsys)
    assert code == 1 and out == "" and "database not found" in err
    assert not data_dir.exists()


def test_old_schema_is_not_upgraded(data_dir, capsys):
    data_dir.mkdir()
    path = config.db_path()
    old = sqlite3.connect(path)
    old.executescript(V3_SQL)
    old.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    code, out, err = run(["networth"], capsys)
    assert code == 1 and out == "" and "schema v3" in err and "finance sync" in err
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


# --- the per-category pace (insights.category_pace) ----------------------------------------------------------

def test_category_pace_uses_the_overview_pace_rules(conn):
    today = date(2026, 9, 25)                                      # 25 days in, 5 left
    bank(conn)
    acct(conn)
    tx(conn, "2026-09-03", 40.0, "Chipotle", cat="Eating Out")
    tx(conn, "2026-09-20", 60.0, "Sweetgreen", cat="Eating Out")                          # discretionary: extrapolated
    tx(conn, "2026-09-01", 2000.0, "Innago", cat="Rent & Utilities")                       # a fixed category
    for d in ("2026-06-12", "2026-07-12", "2026-08-12", "2026-09-12"):
        tx(conn, d, 15.49, "Netflix", cat="Entertainment")                                # recurring: fixed
    tx(conn, "2026-09-05", 20.0, "AMC", cat="Entertainment")
    for d in ("2026-06-28", "2026-07-28", "2026-08-28"):
        tx(conn, d, 165.03, "GEICO", cat="Insurance")                                     # due on the 28th
    tx(conn, "2026-09-04", 50.0, "Amazon", cat="Other Shopping")
    tx(conn, "2026-09-06", -80.0, "Amazon", name="AMAZON REFUND", cat="Other Shopping")   # net refund: no pace
    proj = insights.category_pace(conn, "2026-09", today)
    assert proj["Eating Out"] == 120.0                                                     # 100 + 100 / 25 * 5
    assert proj["Rent & Utilities"] == 2000.0
    assert proj["Entertainment"] == 39.49                                                  # 35.49 + 20 / 25 * 5
    assert proj["Insurance"] == 165.03                                                     # nothing yet + bill due
    assert proj["Other Shopping"] == -30.0 and proj["Groceries"] == 0.0
    pace = insights.pace_projection(conn, "2026-09", today)
    assert pace["bills_due_total"] == 165.03 and pace["fixed_mtd"] == 2015.49
    past = insights.category_pace(conn, "2026-08", today)
    assert past == {c["category"]: c["spend"] for c in insights.category_spend(conn, "2026-08", today)}
    cap(conn, "Insurance", 100)
    status = reads.budget_status(conn, "2026-09", today)
    other = next(c for c in status["caps"] if c["category"] == "Insurance")
    # TASK-11 (d): the status counts the bill still due (it used to say "ok" with $0 spent of a $100 cap)
    assert (other["spent"], other["due"], other["left"], other["status"], other["projection"],
            other["projection_status"]) == (0.0, 165.03, -65.03, "over", 165.03, "over")
