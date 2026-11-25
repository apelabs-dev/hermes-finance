"""Reports numbers (0.5, B1): Gross / Refunds / Net per category, remaining after spending, income sources, money
moved between accounts, and the forecast lines.

Owner decisions (2026-11-20): q1 refunds net against their category (a category below zero is its own
"Refunds over spending" source; refunds are never income, a tax refund stays income); q2 remaining after spending =
income - net spending, exact for any range, with moves between accounts listed separately; q4 the forecast uses the
chosen cash accounts (checking first, savings only when chosen) at the bank's available balance, loans and
investments out, another currency on its own line; q5 a detected paycheck is not in the line until the owner adds
it, one answer per paycheck source; q6 two named everyday-spending lines with their end balances, never
"confidence", never a percentage.

Synthetic data only. The numbers that overlap the dashboard are checked against it to ±$0.01 (interpreter:
insights.month_summary, insights.category_spend, insights.cashflow, insights.spend_by and reads.spend).
"""

from __future__ import annotations

import json
import sqlite3
import tracemalloc
from datetime import date, timedelta

import pytest

from hermes_finance import insights, reads, reports

TODAY = date(2026, 11, 15)
TS = "2026-11-15T20:00:00Z"          # mid-day UTC: the local date is 2026-11-15 in any US time zone
PFC = {
    "groceries": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
    "eating": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),
    "coffee": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"),
    "rent": ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT"),
    "wages": ("INCOME", "INCOME_WAGES"),
    "interest": ("INCOME", "INCOME_INTEREST_EARNED"),
    "tax_refund": ("INCOME", "INCOME_TAX_REFUND"),
    "other_income": ("INCOME", "INCOME_OTHER_INCOME"),
    "transfer_out": ("TRANSFER_OUT", "TRANSFER_OUT_SAVINGS"),
    "card_payment": ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
}


def _account(conn, account_id, name, type_, subtype, current, available, currency="USD"):
    conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                 "available_balance, iso_currency, updated_at) VALUES (?, 'item_a', ?, ?, ?, ?, ?, ?, ?, ?)",
                 (account_id, name, account_id[-4:], type_, subtype, current, available, currency, TS))


def _txn(conn, tid, day, amount, name, pfc, account="acc_chk", currency="USD", merchant=None):
    primary, detailed = PFC[pfc]
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                 "iso_currency, pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at) "
                 "VALUES (?, ?, 'item_a', ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?)",
                 (tid, account, day, name, merchant, amount, currency, primary, detailed, TS, TS))


@pytest.fixture
def books(conn):
    """A small, fixed book: two paychecks' worth of history, a refund bigger than its category's spending, a month
    boundary, one EUR account, a monthly bill, and two moves between accounts."""
    with conn:
        conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                     "VALUES ('item_a', 'ins_1', 'Bank', 'tok', 'active', ?)", (TS,))
        _account(conn, "acc_chk", "Everyday checking", "depository", "checking", 2100.00, 2000.00)
        _account(conn, "acc_sav", "Rainy-day savings", "depository", "savings", 5000.00, 5000.00)
        _account(conn, "acc_eur", "Lisbon account", "depository", "checking", 700.00, 700.00, "EUR")
        _account(conn, "acc_loan", "Car loan", "loan", "auto", 3000.00, None)
        _account(conn, "acc_inv", "Brokerage", "investment", "brokerage", 900.00, None)
        # everyday spending, Aug-Oct (the forecast lookback) and November
        _txn(conn, "g1", "2026-08-05", 120.00, "Market", "groceries")
        _txn(conn, "g2", "2026-09-05", 110.00, "Market", "groceries")
        _txn(conn, "g3", "2026-10-05", 100.00, "Market", "groceries")
        _txn(conn, "g4", "2026-11-02", 120.00, "Market", "groceries")
        _txn(conn, "g5", "2026-11-05", -30.00, "Market refund", "groceries")
        _txn(conn, "e1", "2026-09-20", 60.00, "Diner", "eating")
        _txn(conn, "e2", "2026-11-10", 60.00, "Diner", "eating")
        _txn(conn, "e3", "2026-11-12", -80.00, "Diner refund", "eating")
        # the month boundary: an October charge, a November refund
        _txn(conn, "c1", "2026-10-30", 40.00, "Coffee bar", "coffee")
        _txn(conn, "c2", "2026-11-01", -10.00, "Coffee bar refund", "coffee")
        # a monthly bill
        for i, day in enumerate(("2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01")):
            _txn(conn, f"r{i}", day, 1000.00, "Rent Co", "rent", merchant="Rent Co")
        # income: three monthly paychecks, interest, and other income
        for i, day in enumerate(("2026-09-01", "2026-10-01", "2026-11-01")):
            _txn(conn, f"p{i}", day, -1500.00, "ACME PAYROLL", "wages")
        _txn(conn, "i1", "2026-11-08", -12.00, "Interest paid", "interest")
        _txn(conn, "o1", "2026-11-12", -100.00, "Side gig", "other_income")
        # moves between accounts: never income, never spending
        _txn(conn, "m1", "2026-11-06", 500.00, "To Savings - 7731", "transfer_out")
        _txn(conn, "m2", "2026-11-09", 300.00, "Chase Credit Card Payment", "card_payment")
        # another currency: reported on its own line, never in the dollar sums
        _txn(conn, "x1", "2026-11-03", 25.00, "Mercado", "groceries", account="acc_eur", currency="EUR")
        # the opening balance is 5 days old (the stale state)
        conn.execute("INSERT INTO balance_snapshots (account_id, date, current, available, iso_currency) "
                     "VALUES ('acc_chk', '2026-11-10', 2100.00, 2000.00, 'USD')")
    return conn


def _cat(payload, name):
    return next(c for c in payload["categories"] if c["category"] == name)


def _october():
    return "2026-10-01", "2026-10-31"


# --- 1. Gross / Refunds / Net per category (q1) --------------------------------------------------------------

def test_gross_refunds_and_net_per_category(books):
    t = reports.category_totals(books, "2026-11-01", "2026-11-30")
    groceries, eating, coffee = _cat(t, "Groceries"), _cat(t, "Eating Out"), _cat(t, "Coffee")
    assert (groceries["gross"], groceries["refunds"], groceries["net"]) == (120.00, -30.00, 90.00)
    assert (eating["gross"], eating["refunds"], eating["net"]) == (60.00, -80.00, -20.00)
    assert (coffee["gross"], coffee["refunds"], coffee["net"]) == (0.0, -10.00, -10.00)
    assert t["totals"] == {"gross": 1180.00, "refunds": -120.00, "net": 1060.00, "count": 6}
    assert t["currency"] == "USD"


def test_a_category_below_zero_is_its_own_refunds_over_spending_source(books):
    flow = reports.money_flow(books, "2026-11-01", "2026-11-30")
    over = {r["category"]: r for r in flow["refunds_over"]}
    assert set(over) == {"Eating Out", "Coffee"}
    assert over["Eating Out"]["amount"] == 20.00 and over["Coffee"]["amount"] == 10.00
    assert over["Eating Out"]["label"] == "Refunds over spending · Eating Out"
    # they are sources, not spending groups
    assert [g["group"] for g in flow["groups"]] == ["Home", "Food"]
    assert {c for g in flow["groups"] for c in g["categories"]} == {"Rent & Utilities", "Groceries"}


def test_the_flow_adds_up_with_refunds_larger_than_their_category(books):
    flow = reports.money_flow(books, "2026-11-01", "2026-11-30")
    # 1,612 income + 30 refunded over spending - (1,000 rent + 90 groceries + 0) = 552
    assert flow["income"]["total"] == 1612.00
    assert flow["money_in"] == 1642.00
    assert flow["money_out"] == 1090.00
    assert flow["remaining"] == 552.00
    assert flow["remaining"] == pytest.approx(round(flow["money_in"] - flow["money_out"], 2), abs=0.01)
    assert flow["spending"]["net"] == 1060.00


# --- 2. Remaining after spending, exact for any range (q2) ----------------------------------------------------

def test_month_boundary_splits_a_charge_from_its_refund(books):
    october = reports.category_totals(books, *_october())
    assert _cat(october, "Coffee")["net"] == 40.00
    november = reports.category_totals(books, "2026-11-01", "2026-11-30")
    assert _cat(november, "Coffee")["net"] == -10.00
    # a range that ends before the refund never sees it; one that includes it nets it
    assert reports.category_totals(books, "2026-10-30", "2026-10-31")["totals"]["net"] == 40.00
    assert _cat(reports.category_totals(books, "2026-10-30", "2026-11-01"), "Coffee")["net"] == 30.00


@pytest.mark.parametrize("start, end, income, spending, remaining, short", [
    ("2026-11-01", "2026-11-30", 1612.00, 1060.00, 552.00, False),
    ("2026-11-02", "2026-11-05", 0.0, 90.00, -90.00, True),          # the 120 charge and its 30 refund
    (None, "2026-11-01", 4500.00, 4420.00, 80.00, False),            # open start: everything through Nov 1
    ("2026-12-01", "2026-12-31", 0.0, 0.0, 0.0, False),              # an empty future range
])
def test_remaining_after_spending_is_exact_for_any_range(books, start, end, income, spending, remaining, short):
    got = reports.remaining_after_spending(books, start, end)
    assert got["income"] == income and got["spending"] == spending and got["remaining"] == remaining
    assert got["short"] is short
    # "at any amount" never mind the calendar month: it is the two sums, exactly
    assert got["remaining"] == pytest.approx(round(got["income"] - got["spending"], 2), abs=0.01)


def test_income_sources_are_paychecks_interest_and_other(books):
    inc = reports.income_sources(books, "2026-11-01", "2026-11-30")
    assert [(s["source"], s["label"], s["amount"]) for s in inc["sources"]] == [
        ("paychecks", "Paychecks", 1500.00), ("other", "Other income", 100.00),
        ("interest_tax_refunds", "Interest and tax refunds", 12.00)]
    assert inc["total"] == 1612.00
    assert [s["share"] for s in inc["sources"]] == [93.05, 6.2, 0.74]


def test_moves_between_accounts_are_listed_not_drawn(books):
    flow = reports.money_flow(books, "2026-11-01", "2026-11-30")
    assert [(m["date"], m["label"], m["amount"]) for m in flow["moves"]["rows"]] == [
        ("2026-11-06", "To Savings - 7731", 500.00), ("2026-11-09", "Chase Credit Card Payment", 300.00)]
    assert flow["moves"]["total"] == 800.00
    names = {c for g in flow["groups"] for c in g["categories"]}
    assert "Transfers" not in names and flow["spending"]["net"] == 1060.00


# --- 3. Multi-currency: its own line, never added (q4) -------------------------------------------------------

def test_multi_currency_is_reported_separately_and_never_added(books):
    t = reports.category_totals(books, "2026-11-01", "2026-11-30")
    assert [f["currency"] for f in t["foreign"]] == ["EUR"]
    eur = t["foreign"][0]
    assert [(c["category"], c["gross"], c["net"]) for c in eur["categories"]] == [("Groceries", 25.00, 25.00)]
    assert eur["totals"]["net"] == 25.00
    assert _cat(t, "Groceries")["gross"] == 120.00        # the EUR charge is not in the dollar Groceries
    flow = reports.money_flow(books, "2026-11-01", "2026-11-30")
    assert flow["money_out"] == 1090.00 and flow["remaining"] == 552.00


# --- 4. The forecast: accounts, opening balance, lines (q4, q6) ------------------------------------------------

def test_forecast_accounts_are_checking_first_loans_and_investments_out(books):
    table = reports.forecast_accounts(books)
    assert [(a["account_id"], a["kind"], a["ticked"], a["available"]) for a in table["accounts"]] == [
        ("acc_chk", "checking", True, 2000.00), ("acc_sav", "savings", False, 5000.00)]
    assert table["default"] == ["acc_chk"] and table["opening"] == 2000.00
    assert {a["account_id"] for a in table["unavailable"]} == set()
    assert {e["type"] for e in table["excluded"]} == {"loan", "investment"}
    assert [f["currency"] for f in table["other_currencies"]] == ["EUR"]
    assert table["other_currencies"][0]["available"] == 700.00


def test_forecast_opens_on_the_selected_available_balance(books):
    assert reports.forecast(books, TODAY)["opening"]["balance"] == 2000.00
    both = reports.forecast(books, TODAY, accounts=["acc_chk", "acc_sav"])
    assert both["opening"]["balance"] == 7000.00
    assert {a["account_id"] for a in both["accounts"]} == {"acc_chk", "acc_sav"}
    # another currency is its own line, never part of the dollar opening
    assert both["other_currencies"][0]["available"] == 700.00


def test_forecast_has_two_named_everyday_spending_lines_with_end_balances(books):
    f = reports.forecast(books, TODAY)
    lines = f["lines"]
    assert set(lines) == {"lower", "expected", "higher"}
    assert lines["lower"]["label"] == "Lower everyday spending"
    assert lines["higher"]["label"] == "Higher everyday spending"
    assert lines["lower"]["daily"] == 3.50 and lines["expected"]["daily"] == 4.67 and lines["higher"]["daily"] == 5.84
    assert lines["lower"]["end"] == 895.00 and lines["expected"]["end"] == 859.90 and lines["higher"]["end"] == 824.80
    assert lines["lower"]["end"] > lines["expected"]["end"] > lines["higher"]["end"]
    assert f["available"] == lines["expected"]["end"] and f["available_date"] == "2026-12-14"
    text = json.dumps(f).lower()
    assert "confidence" not in text and "%" not in text


def test_forecast_horizon_events_and_lowest_point(books):
    f = reports.forecast(books, TODAY)
    assert f["horizon"] == {"days": 30, "start": "2026-11-15", "end": "2026-12-14"}
    assert [p["date"] for p in f["lines"]["expected"]["points"]][0] == "2026-11-15"
    assert len(f["lines"]["expected"]["points"]) == 30
    rent = [e for e in f["events"] if e["label"] == "Rent Co"]
    assert len(rent) == 1 and rent[0]["amount"] == -1000.00 and rent[0]["kind"] == "bill"
    assert rent[0]["date"] >= "2026-11-15"
    balances = [p["balance"] for p in f["lines"]["expected"]["points"]]
    assert f["lowest"]["balance"] == min(balances + [f["opening"]["balance"]])
    assert f["lowest"]["balance"] < f["opening"]["balance"]
    assert f["expected"] == {"in": 0.0, "out": 1140.10, "net": -1140.10}


def test_forecast_flags_a_stale_opening_balance(books):
    stale = reports.forecast(books, TODAY)
    assert stale["opening"]["as_of"] == "2026-11-10"
    assert stale["opening"]["stale_days"] == 5 and stale["opening"]["stale"] is True
    assert any("bank balance from 2026-11-10" in w for w in stale["warnings"])
    with books:
        books.execute("INSERT INTO balance_snapshots (account_id, date, current, available, iso_currency) "
                      "VALUES ('acc_chk', '2026-11-15', 2100.00, 2000.00, 'USD')")
    fresh = reports.forecast(books, TODAY)["opening"]
    assert fresh["as_of"] == "2026-11-15" and fresh["stale"] is False
    assert not any("bank balance from" in w for w in reports.forecast(books, TODAY)["warnings"])


def test_the_stale_boundary_is_exactly_stale_days(books):
    """A balance STALE_DAYS old is stale; one day younger is not (round 1: >= vs >, unpinned)."""
    with books:
        books.execute("UPDATE balance_snapshots SET date = '2026-11-12' WHERE account_id = 'acc_chk'")
    at_boundary = reports.forecast(books, TODAY)["opening"]
    assert at_boundary["stale_days"] == reports.STALE_DAYS and at_boundary["stale"] is True
    with books:
        books.execute("UPDATE balance_snapshots SET date = '2026-11-13' WHERE account_id = 'acc_chk'")
    below = reports.forecast(books, TODAY)["opening"]
    assert below["stale_days"] == reports.STALE_DAYS - 1 and below["stale"] is False


def test_a_stale_tracked_bill_is_charged_once_today_not_once_per_missed_cycle(books):
    """A tracked bill never stops. Its missed cycles are history: one late charge today, then the cycles from the
    window start (round 1: it was charged once for every missed cycle, all on day 1)."""
    with books:
        books.execute("INSERT INTO tracked_bills (merchant_key, merchant, cadence, variable, anchor_date, "
                      "anchor_amount, created_at, updated_at) VALUES "
                      "('gym co', 'Gym Co', 'monthly', 0, '2026-05-01', 50.00, ?, ?)", (TS, TS))
        for i, day in enumerate(("2026-05-01", "2026-06-01", "2026-07-01")):
            _txn(books, f"gym{i}", day, 50.00, "Gym Co", "rent", merchant="Gym Co")
    item = next(i for i in insights.recurring(books, TODAY)["items"] if i["merchant"] == "Gym Co")
    assert item["next_expected"] == "2026-08-01" and item["status"] == "late"
    gym = [e for e in reports.forecast(books, TODAY)["events"] if e["label"] == "Gym Co"]
    assert [(e["date"], e["amount"], e["overdue"]) for e in gym] == [
        ("2026-11-15", -50.00, True),       # one late charge, today
        ("2026-12-01", -50.00, False),      # the next cycle, from the window start
    ]


# --- 5. A detected paycheck waits for the owner (q5) ----------------------------------------------------------

ACME_KEY = "acc_chk|USD|acme payroll"       # the answer key: account | currency | normalized description


def test_a_detected_paycheck_is_not_in_the_line_until_it_is_added(books):
    found = reports.detected_paychecks(books, TODAY)
    assert [(s["key"], s["label"], s["amount"], s["cadence"]) for s in found["sources"]] == [
        (ACME_KEY, "ACME PAYROLL", 1500.00, "monthly")]
    src = found["sources"][0]
    assert src["last_date"] == "2026-11-01" and src["next_expected"] == "2026-12-01"
    assert src["answer"] is None
    # not answered: the line has no income
    assert reports.forecast(books, TODAY)["expected"]["in"] == 0.0
    assert not [e for e in reports.forecast(books, TODAY)["events"] if e["kind"] == "paycheck"]
    # one answer per source: "add" puts it in, "omit" keeps it out and the question stops
    assert reports.detected_paychecks(books, TODAY, answers={ACME_KEY: "omit"})["sources"][0]["answer"] == "omit"
    added = reports.forecast(books, TODAY, paychecks={ACME_KEY: "add"})
    assert added["expected"]["in"] == 1500.00
    paychecks = [e for e in added["events"] if e["kind"] == "paycheck"]
    assert len(paychecks) == 1 and paychecks[0]["amount"] == 1500.00 and paychecks[0]["confirmed"] is True
    assert added["available"] == 1500.00 + 859.90


def test_the_owners_paycheck_answer_is_read_from_settings(books):
    with books:
        books.execute("INSERT INTO settings (key, value, updated_at) VALUES ('reports_paychecks', ?, ?)",
                      (json.dumps({ACME_KEY: "add"}), TS))
    assert reports.paycheck_answers(books) == {ACME_KEY: "add"}
    assert reports.forecast(books, TODAY)["expected"]["in"] == 1500.00
    with books:
        books.execute("UPDATE settings SET value = ? WHERE key = 'reports_paychecks'",
                      (json.dumps({ACME_KEY: "omit"}),))
    assert reports.forecast(books, TODAY)["expected"]["in"] == 0.0


def test_a_paycheck_answer_is_bound_to_its_account_and_currency(books):
    """Two payroll streams with the same description on two accounts are two sources: an Add for one never puts the
    other in the line (round 1 security: an answer followed a colliding normalized label)."""
    with books:
        for i, day in enumerate(("2026-09-15", "2026-10-15", "2026-11-15")):
            _txn(books, f"split{i}", day, -800.00, "ACME PAYROLL", "wages", account="acc_sav")
    found = reports.detected_paychecks(books, TODAY)
    assert sorted((s["key"], s["amount"]) for s in found["sources"]) == [
        (ACME_KEY, 1500.00), ("acc_sav|USD|acme payroll", 800.00)]
    assert all(s["answer"] is None for s in found["sources"])
    only_checking = reports.forecast(books, TODAY, paychecks={ACME_KEY: "add"})
    pays = [e for e in only_checking["events"] if e["kind"] == "paycheck"]
    assert [(e["source"], e["amount"]) for e in pays] == [(ACME_KEY, 1500.00)]


def test_a_paycheck_needs_three_deposits(books):
    """3+ deposits on a steady cadence, like insights.recurring (round 1: two raised the question)."""
    with books:
        _txn(books, "tt1", "2026-09-20", -900.00, "Two Timer Payroll", "wages", merchant="Two Timer Payroll")
        _txn(books, "tt2", "2026-10-20", -900.00, "Two Timer Payroll", "wages", merchant="Two Timer Payroll")
    assert "two timer payroll" not in [s["key_label"] for s in reports.detected_paychecks(books, TODAY)["sources"]]
    with books:
        _txn(books, "tt3", "2026-11-14", -900.00, "Two Timer Payroll", "wages", merchant="Two Timer Payroll")
    found = [s for s in reports.detected_paychecks(books, TODAY)["sources"] if s["key_label"] == "two timer payroll"]
    assert len(found) == 1 and (found[0]["cadence"], found[0]["count"]) == ("monthly", 3)


def test_a_padded_paycheck_answer_counts_and_bad_settings_are_bounded(books, monkeypatch):
    """The reader strips a padded token (it used to hide the question without drawing the paycheck) and refuses an
    enormous value, or one the JSON decoder chokes on, instead of raising (round 1 security Lows)."""
    with books:
        books.execute("INSERT INTO settings (key, value, updated_at) VALUES ('reports_paychecks', ?, ?)",
                      (json.dumps({ACME_KEY: " add "}), TS))
    assert reports.paycheck_answers(books) == {ACME_KEY: "add"}
    assert reports.forecast(books, TODAY)["expected"]["in"] == 1500.00
    with books:
        books.execute("UPDATE settings SET value = ? WHERE key = 'reports_paychecks'",
                      (json.dumps({ACME_KEY: "add"}) + " " * reports.PAYCHECK_SETTING_MAX_BYTES,))
    assert reports.paycheck_answers(books) == {}

    class TooDeep:
        @staticmethod
        def loads(*args, **kwargs):
            raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(reports, "json", TooDeep)
    with books:
        books.execute("UPDATE settings SET value = ? WHERE key = 'reports_paychecks'",
                      (json.dumps({ACME_KEY: "add"}),))
    assert reports.paycheck_answers(books) == {}
    assert reports.forecast(books, TODAY)["expected"]["in"] == 0.0


# --- 6. The numbers equal the dashboard, ±$0.01 -------------------------------------------------------------


def test_a_tax_refund_is_income_not_a_spending_refund(books):
    """q1: a refund of spending nets against its category; a tax refund stays income (the 0.4.0 rule)."""
    with books:
        _txn(books, "tax1", "2026-11-13", -400.00, "IRS TREAS 310 TAX REF", "tax_refund", merchant="IRS")
    inc = reports.income_sources(books, "2026-11-01", "2026-11-30")
    row = next(s for s in inc["sources"] if s["source"] == "interest_tax_refunds")
    assert (row["label"], row["amount"], row["count"]) == ("Interest and tax refunds", 412.00, 2)
    assert inc["total"] == 2012.00
    totals = reports.category_totals(books, "2026-11-01", "2026-11-30")["totals"]
    assert totals == {"gross": 1180.00, "refunds": -120.00, "net": 1060.00, "count": 6}   # income never here
    assert reports.remaining_after_spending(books, "2026-11-01", "2026-11-30")["remaining"] == 952.00

def test_the_numbers_equal_the_dashboards(books):
    """Overview: insights.month_summary. Budgets: insights.category_spend. Cash flow: insights.cashflow.
    Spend: insights.spend_by and reads.spend."""
    summary = insights.month_summary(books, "2026-11", TODAY)
    totals = reports.category_totals(books, "2026-11-01", "2026-11-30")["totals"]
    assert totals["net"] == pytest.approx(summary["mtd"], abs=0.01)
    assert reports.remaining_after_spending(books, "2026-11-01", "2026-11-30")["income"] == \
        pytest.approx(summary["income"], abs=0.01)
    assert reports.category_totals(books, "2026-11-01", "2026-11-30")["totals"]["net"] == \
        pytest.approx(insights.spend_by(books, "v.month = ?", ("2026-11",))[None][0], abs=0.01)
    for row in insights.category_spend(books, "2026-11", TODAY):
        if round(row["spend"] * 100):
            assert _cat(reports.category_totals(books, "2026-11-01", "2026-11-30"), row["category"])["net"] == \
                pytest.approx(row["spend"], abs=0.01)
    cash = next(m for m in insights.cashflow(books, 2, TODAY)["months"] if m["month"] == "2026-11")
    assert cash["spending"] == pytest.approx(totals["net"], abs=0.01)
    assert cash["net"] == pytest.approx(reports.remaining_after_spending(books, "2026-11-01", "2026-11-30")["remaining"],
                                       abs=0.01)
    # a custom range: `finance spend --since 2026-09-01 --until 2026-09-30`
    period = reads.Period("range", "2026-09-01", "2026-09-30")
    assert reads.spend(books, reads.Filters(period), TODAY)["total"] == \
        pytest.approx(reports.category_totals(books, "2026-09-01", "2026-09-30")["totals"]["net"], abs=0.01)


def test_the_numbers_equal_the_dashboards_on_the_demo_data(conn):
    from demo_data import build
    build(conn, today=TODAY)
    totals = reports.category_totals(conn, "2026-11-01", "2026-11-30")["totals"]
    summary = insights.month_summary(conn, "2026-11", TODAY)
    assert totals["net"] == pytest.approx(summary["mtd"], abs=0.01)
    assert reports.remaining_after_spending(conn, "2026-11-01", "2026-11-30")["income"] == \
        pytest.approx(summary["income"], abs=0.01)
    for row in insights.category_spend(conn, "2026-11", TODAY):
        if round(row["spend"] * 100):
            assert _cat(reports.category_totals(conn, "2026-11-01", "2026-11-30"), row["category"])["net"] == \
                pytest.approx(row["spend"], abs=0.01)
    cash = next(m for m in insights.cashflow(conn, 2, TODAY)["months"] if m["month"] == "2026-11")
    assert cash["spending"] == pytest.approx(totals["net"], abs=0.01)


# --- 7. Arguments ---------------------------------------------------------------------------------------------

def test_bad_arguments_are_refused(books):
    with pytest.raises(reports.ReportError):
        reports.forecast(books, TODAY, days=0)
    with pytest.raises(reports.ReportError):
        reports.forecast(books, TODAY, days=reports.MAX_FORECAST_DAYS + 1)
    with pytest.raises(reads.ReadError):
        reads.forecast_report(books, TODAY, 0)
    with pytest.raises(reports.ReportError):
        reports.category_totals(books, "2026-11-30", "2026-11-01")


# --- 8. Hardening: the security Lows from t_cb6c3189 ---------------------------------------------------------

def test_moves_lists_only_the_newest_rows_and_keeps_the_exact_count_and_total(books):
    """A long range materialised and returned every transfer row, and `finance_reports_cashflow` sent all of them to
    the model. The list is capped to the newest `MOVES_LIST_LIMIT` (same oldest-first order); `count` and `total` still
    cover every row in the range, and the payload says the list is truncated."""
    extra = [(f"mv{i:02d}", f"Move {i}", 10.0 + i) for i in range(60)]
    with books:
        for i, (tid, label, amount) in enumerate(extra):
            _txn(books, tid, (date(2026, 9, 1) + timedelta(days=i)).isoformat(), amount, label, "transfer_out")
    m = reports.moves(books, "2026-09-01", "2026-10-31")
    assert m["count"] == 60                                              # every row in the range
    assert m["total"] == sum(amount for _, _, amount in extra)           # every row, not just the listed ones
    assert m["limit"] == reports.MOVES_LIST_LIMIT == 50
    assert m["shown"] == 50 and m["truncated"] is True
    assert "50" in m["note"] and "60" in m["note"]
    assert [r["label"] for r in m["rows"]] == [label for _, label, _ in extra][-50:]   # the newest 50, oldest first
    assert [r["date"] for r in m["rows"]] == sorted(r["date"] for r in m["rows"])
    assert m["rows"] == sorted(m["rows"], key=lambda r: (r["date"], r["label"]))


def test_moves_under_the_cap_are_all_listed_and_not_marked_truncated(books):
    short = reports.moves(books, "2026-11-01", "2026-11-30")
    assert (short["shown"], short["count"], short["truncated"], short["note"]) == (2, 2, False, None)
    assert [r["label"] for r in short["rows"]] == ["To Savings - 7731", "Chase Credit Card Payment"]
    empty = reports.moves(books, "2026-12-01", "2026-12-31")
    assert (empty["rows"], empty["shown"], empty["count"], empty["total"], empty["truncated"]) == ([], 0, 0, 0.0, False)


def test_money_flow_reads_each_aggregate_once(books, monkeypatch):
    """`money_flow` ran `category_totals` three times and `income_sources` twice; a sync committing between those reads
    could make `money_in - money_out` disagree with `remaining`. One read each, everything else derived."""
    calls = {"category_totals": 0, "income_sources": 0}
    real_categories, real_income = reports.category_totals, reports.income_sources

    def counting_categories(*args, **kwargs):
        calls["category_totals"] += 1
        return real_categories(*args, **kwargs)

    def counting_income(*args, **kwargs):
        calls["income_sources"] += 1
        return real_income(*args, **kwargs)

    monkeypatch.setattr(reports, "category_totals", counting_categories)
    monkeypatch.setattr(reports, "income_sources", counting_income)
    flow = reports.money_flow(books, "2026-11-01", "2026-11-30")
    assert calls == {"category_totals": 1, "income_sources": 1}
    assert (flow["money_in"], flow["money_out"], flow["remaining"]) == (1642.00, 1090.00, 552.00)


def test_money_flow_derives_every_number_from_one_read_when_a_sync_commits_between_reads(books, monkeypatch):
    """The Sync-now commit lands after the spending aggregate and before anything else reads. Every number in the
    payload must still come from that one aggregate, so the flow adds up and the late row waits for the next report."""
    real = reports.category_totals
    reads = {"n": 0}

    def syncing(*args, **kwargs):
        out = real(*args, **kwargs)
        reads["n"] += 1
        if reads["n"] == 1:
            with books:
                _txn(books, "sync1", "2026-11-14", 25.00, "Sync Mart", "groceries")
        return out

    monkeypatch.setattr(reports, "category_totals", syncing)
    flow = reports.money_flow(books, "2026-11-01", "2026-11-30")
    assert flow["spending"]["net"] == 1060.00                 # the pre-commit number, read once
    assert flow["spending"]["count"] == 6                     # the committed row waits for the next report
    assert flow["money_in"] - flow["money_out"] == flow["remaining"]


def test_money_flow_reads_the_display_currency_once(books, monkeypatch):
    """Income and spending were each read in their own display currency: a currency change between the reads labelled
    the flow with one code and summed the other. The whole report is one currency, read once."""
    real = reports.display_currency
    reads = []

    def counting(conn):
        reads.append(1)
        return real(conn)

    monkeypatch.setattr(reports, "display_currency", counting)
    flow = reports.money_flow(books, "2026-11-01", "2026-11-30")
    assert flow["currency"] == "USD"
    assert len(reads) == 1


def test_a_three_decimal_currency_keeps_its_third_decimal_in_the_foreign_table(books):
    """`_PART_COLUMNS` rounded every line to 2 decimals and dropped sub-cent rows, so a KWD line lost its third decimal
    and a 4-fil charge vanished from `foreign`. A foreign line is rounded once, to its own ISO exponent."""
    with books:
        _account(books, "acc_kwd", "Kuwait account", "depository", "checking", 700.00, 700.00, "KWD")
        _account(books, "acc_jpy", "Tokyo account", "depository", "checking", 90000.00, 90000.00, "JPY")
        _txn(books, "kw1", "2026-11-04", 1.234, "Souk", "groceries", account="acc_kwd", currency="KWD")
        _txn(books, "kw2", "2026-11-05", 0.004, "Parking meter", "groceries", account="acc_kwd", currency="KWD")
        _txn(books, "jp1", "2026-11-06", 1200.40, "Konbini", "groceries", account="acc_jpy", currency="JPY")
    t = reports.category_totals(books, "2026-11-01", "2026-11-30")
    foreign = {f["currency"]: f for f in t["foreign"]}
    assert [(c["category"], c["gross"], c["net"]) for c in foreign["KWD"]["categories"]] == [
        ("Groceries", 1.238, 1.238)]
    assert foreign["KWD"]["totals"] == {"gross": 1.238, "refunds": 0.0, "net": 1.238, "count": 2}
    assert [(c["category"], c["gross"]) for c in foreign["JPY"]["categories"]] == [("Groceries", 1200.0)]
    assert foreign["JPY"]["totals"]["net"] == 1200.0
    # the display currency is untouched: USD still rounds to cents, line by line
    assert _cat(t, "Groceries")["gross"] == 120.00 and t["totals"]["net"] == 1060.00


def test_detected_paychecks_reads_the_display_currency_once(books, monkeypatch):
    """The answer key's currency came from one read and the SQL filter from another: a currency change between them
    tagged a deposit with the old currency's Add. One read decides both."""
    with books:
        for i, day in enumerate(("2026-09-01", "2026-10-01", "2026-11-01")):
            _txn(books, f"eurpay{i}", day, -1500.00, "ACME PAYROLL", "wages", account="acc_eur", currency="EUR")
    monkeypatch.setattr(reports, "display_currency", lambda conn: "EUR")
    found = reports.detected_paychecks(books, TODAY)
    assert found["currency"] == "EUR"
    assert [(s["key"], s["account_id"]) for s in found["sources"]] == [("acc_eur|EUR|acme payroll", "acc_eur")]


def test_the_paycheck_answer_size_cap_is_applied_before_the_value_reaches_python(books):
    """The 20 KB cap ran after SQLite had handed the whole row to Python. The reader asks SQLite for the size first,
    so an oversized setting never becomes a Python string at all; a value exactly at the cap still reads."""
    oversized = json.dumps({ACME_KEY: "add"}) + " " * reports.PAYCHECK_SETTING_MAX_BYTES
    with books:
        books.execute("INSERT INTO settings (key, value, updated_at) VALUES ('reports_paychecks', ?, ?)",
                      (oversized, TS))
    seen = []

    def watch(cursor, row):
        seen.extend(len(value) for value in row if isinstance(value, str))
        return row

    books.row_factory = watch
    try:
        assert reports.paycheck_answers(books) == {}
    finally:
        books.row_factory = sqlite3.Row
    assert max(seen, default=0) <= reports.PAYCHECK_SETTING_MAX_BYTES

    base = json.dumps({ACME_KEY: "add"})
    at_cap = base + " " * (reports.PAYCHECK_SETTING_MAX_BYTES - len(base))
    assert len(at_cap.encode()) == reports.PAYCHECK_SETTING_MAX_BYTES
    with books:
        books.execute("UPDATE settings SET value = ? WHERE key = 'reports_paychecks'", (at_cap,))
    assert reports.paycheck_answers(books) == {ACME_KEY: "add"}


# A row too large for the readers' byte bound but not for their character bound: SQLite's LENGTH(TEXT) stops at the
# first NUL and counts an invalid UTF-8 lead byte plus its continuation bytes as one character each, so the character
# cap alone let these rows through (security t_cec35e94 L1).
_TOO_MANY_BYTES = 4 * reports.PAYCHECK_SETTING_MAX_BYTES + 1     # past both readers' four-times-the-cap byte bound
_READERS = [
    (reports.PAYCHECK_SETTING, reports.paycheck_answers, {}),
    (reports.FORECAST_ACCOUNTS_SETTING, reports.forecast_account_choice, None),
]


@pytest.mark.parametrize("setting, read, empty", _READERS, ids=["paychecks", "forecast-accounts"])
def test_a_nul_prefixed_setting_never_reaches_python(books, setting, read, empty):
    """Security t_cec35e94 L1: LENGTH(TEXT) stops at the first NUL, so "\\x00" + 50 MB read as a 0-character value and
    the reader built the whole row into a Python string (the 20 KB cap never applied). The reader now bounds the row's
    bytes too, so the CASE hands Python NULL and no part of the value is decoded."""
    payload = b"\x00" + b"x" * _TOO_MANY_BYTES
    with books:
        books.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, CAST(? AS TEXT), ?)",
                      (setting, payload, TS))
    # the trap is real in this row: SQLite counts it as empty
    assert books.execute("SELECT LENGTH(value) FROM settings WHERE key = ?", (setting,)).fetchone()[0] == 0
    seen = []

    def watch(cursor, row):
        seen.extend(len(value) for value in row if isinstance(value, str))
        return row

    old = books.row_factory
    books.row_factory = watch
    try:
        assert read(books) == empty
    finally:
        books.row_factory = old
    assert seen == [], f"a {max(seen, default=0)}-character value reached Python"


@pytest.mark.parametrize("setting, read, empty", _READERS, ids=["paychecks", "forecast-accounts"])
def test_an_invalid_utf8_run_never_becomes_a_python_string(books, setting, read, empty):
    """Security t_cec35e94 L1: LENGTH(TEXT) counts a "\\xC0" + continuation-byte run as one character each, so an 8 MB
    row of them read as a 1-character value and the driver decoded the whole row into a Python string (a 16 MB peak)
    before the reader could refuse it. The byte bound refuses the row inside SQLite."""
    payload = b"\xc0" + b"\x80" * 8_000_000
    with books:
        books.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, CAST(? AS TEXT), ?)",
                      (setting, payload, TS))
    assert books.execute("SELECT LENGTH(value) FROM settings WHERE key = ?", (setting,)).fetchone()[0] == 1
    tracemalloc.start()
    try:
        assert read(books) == empty
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000, f"the reader allocated {peak / 1e6:.1f} MB for a row it should refuse"


def test_invalid_entries_do_not_push_a_real_answer_past_the_entry_cap(books):
    """The 200-entry cap was applied to the raw map before validation: junk entries early in it raised the cap before
    the real Add was reached, and that source quietly went back to waiting. Validate first, then cap."""
    answers = {f"junk-{i}": "maybe" for i in range(reports.PAYCHECK_SETTING_MAX_ENTRIES)}
    answers[ACME_KEY] = "add"
    with books:
        books.execute("INSERT INTO settings (key, value, updated_at) VALUES ('reports_paychecks', ?, ?)",
                      (json.dumps(answers), TS))
    assert reports.paycheck_answers(books) == {ACME_KEY: "add"}
    assert reports.forecast(books, TODAY)["expected"]["in"] == 1500.00


def test_more_valid_answers_than_the_entry_cap_are_trimmed_and_the_overflow_is_surfaced(books):
    """Over 200 valid answers are trimmed (the reader's cap), and the forecast says so instead of dropping them
    silently."""
    answers = {f"src-{i:03d}": "add" for i in range(reports.PAYCHECK_SETTING_MAX_ENTRIES + 5)}
    with books:
        books.execute("INSERT INTO settings (key, value, updated_at) VALUES ('reports_paychecks', ?, ?)",
                      (json.dumps(answers), TS))
    assert len(reports.paycheck_answers(books)) == reports.PAYCHECK_SETTING_MAX_ENTRIES
    f = reports.forecast(books, TODAY)
    assert f["paychecks"]["answers_dropped"] == 5
    assert any(f"5 saved paycheck answers" in w for w in f["warnings"])


# --- 9. The moves cap's follow-ups (t_589d9c8e) ---------------------------------------------------------------

def test_the_moves_total_is_the_sum_of_the_rows_it_lists(books):
    """Security t_b521d761 L2: the window SUM rounded in SQLite (half away from zero) while each row was rounded in
    Python, so sub-cent transfers gave a total the listed rows did not add up to (16.82 against 16.81). One ROUND per
    row in the statement feeds both the list and the total, so they always agree."""
    amounts = [2.675, 1.005, 0.125, 10.015, 3.00]
    with books:
        for i, amount in enumerate(amounts):
            _txn(books, f"frac{i}", f"2026-10-{10 + i:02d}", amount, f"Fraction {i}", "transfer_out")
    m = reports.moves(books, "2026-10-01", "2026-10-31")
    assert (m["count"], m["truncated"]) == (len(amounts), False)
    assert [r["amount"] for r in m["rows"]] == pytest.approx([2.67, 1.00, 0.13, 10.02, 3.00], abs=0.001)
    assert m["total"] == pytest.approx(sum(r["amount"] for r in m["rows"]), abs=0.001)
    assert m["total"] == pytest.approx(16.82, abs=0.001)          # the rounded rows add up to the total


def test_the_dropped_answers_warning_names_advice_that_works_and_reads_right_for_one(books):
    """t_209138b7 review nit 4: the warning said "1 saved paycheck answers are ... answer a source again to keep it",
    and following that advice raises ReportError (the map is already at its 200-entry cap). It now says to clear an
    old answer first, and a single dropped answer reads as one."""
    answers = {f"src-{i:03d}": "add" for i in range(reports.PAYCHECK_SETTING_MAX_ENTRIES + 1)}
    with books:
        books.execute("INSERT INTO settings (key, value, updated_at) VALUES ('reports_paychecks', ?, ?)",
                      (json.dumps(answers), TS))
    f = reports.forecast(books, TODAY)
    assert f["paychecks"]["answers_dropped"] == 1
    warn = next(w for w in f["warnings"] if "saved paycheck answer" in w)
    assert warn.startswith("1 saved paycheck answer is over the 200-answer limit")
    assert "was not read" in warn and "clear an old answer first" in warn
    assert "answer a source again" not in warn
    with pytest.raises(reports.ReportError):                     # the old advice: refused at the cap
        reports.save_paycheck_answer(books, "src-200", "add")
    reports.save_paycheck_answer(books, "src-000", None)         # what the warning says: make room first
    reports.save_paycheck_answer(books, "src-200", "add")
    saved = reports.paycheck_answers(books)
    assert saved["src-200"] == "add" and len(saved) == reports.PAYCHECK_SETTING_MAX_ENTRIES
    assert reports.forecast(books, TODAY)["paychecks"]["answers_dropped"] == 0
    # more than one dropped answer keeps the plural
    with books:
        books.execute("UPDATE settings SET value = ? WHERE key = 'reports_paychecks'",
                      (json.dumps({f"src-{i:03d}": "add"
                                   for i in range(reports.PAYCHECK_SETTING_MAX_ENTRIES + 4)}),))
    warn = next(w for w in reports.forecast(books, TODAY)["warnings"] if "saved paycheck answer" in w)
    assert warn.startswith("4 saved paycheck answers are over the 200-answer limit")
    assert "were not read" in warn and "clear an old answer first" in warn


def test_saved_answers_survive_a_utf16_database(tmp_path):
    """Security t_b521d761 L1: the reader capped on LENGTH(CAST(value AS BLOB)) — the row's size as stored — while the
    writer counts UTF-8 bytes; on a UTF-16 database 80 valid answers read back as none and the next save wiped 79 of
    them. The reader caps on characters now, and the writer's json.dumps is ASCII, so the two agree again (the app
    never creates such a database; this is the belt)."""
    path = tmp_path / "utf16.sqlite"
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA encoding = 'UTF-16le'")
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")
        answers = {f"{'k' * 186}{i:04d}": "add" for i in range(80)}
        value = json.dumps(answers)
        assert len(value) < reports.PAYCHECK_SETTING_MAX_BYTES < len(value.encode("utf-16-le"))
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (reports.PAYCHECK_SETTING, value))
        conn.commit()
        assert conn.execute("PRAGMA encoding").fetchone()[0].lower() == "utf-16le"
        assert reports.paycheck_answers(conn) == answers          # all 80 read whole
        reports.save_paycheck_answer(conn, "src-new", "add")      # the next save keeps them, and adds one
        saved = reports.paycheck_answers(conn)
        assert len(saved) == 81 and saved["src-new"] == "add"
    finally:
        conn.close()
