"""SimpleFIN money in on a bank account is Income, not negative spending. SimpleFIN sends no category, so before this a
payroll deposit landed in Other and netted against the month's spending ("Spent in November -$1,280.24").
classify.inflow_hint files it at import time: income, or a transfer when the description says so, and a card's own
payment line is a transfer. Refunds are never Income, from any source (owner decision): on a card or loan every other
inflow gets no hint and nets against spending as a refund, and on any account a line that says REFUND, RETURN,
REVERSAL or CREDIT VOUCHER gets none either. A sync reaches rows imported earlier. A category the user set always
wins; Plaid rows are untouched."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

import demo_data
from hermes_finance import api, classify, cli, config, simplefin_sync, tooldefs
from test_simplefin_sync import PUBLIC, TODAY, Bridge, account_set, card, claimed, tx

MONTH = "2026-11"
OUT = [("T1", date(2026, 11, 2), "-100.00", "Grocery Mart"),
       ("T2", date(2026, 11, 3), "-45.50", "Gas Depot"),
       ("T3", date(2026, 11, 5), "-80.25", "Corner Cafe"),
       ("T4", date(2026, 11, 7), "-12.00", "Streaming Plus")]
OUTFLOWS = 237.75
PAY = [("P1", date(2026, 11, 1), "2564.48", "Pay day!"), ("P2", date(2026, 11, 8), "2564.48", "Pay day!")]
FAST = {"sleep": lambda s: None, "jitter": 0}


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)


def connect(conn, rows, kind="checking", name="Everyday Checking"):
    """Claim and confirm one SimpleFIN account of this type holding these (id, day, amount, description) rows."""
    bridge = Bridge(account_set([card(name=name, txs=[tx(*r) for r in rows])]))
    claimed(conn, bridge)
    key = simplefin_sync.pending(conn)["accounts"][0]["key"]
    simplefin_sync.confirm(conn, {key: {"type": kind}}, transport=bridge, resolve=PUBLIC, today=TODAY, **FAST)
    return bridge


def resync(conn, bridge):
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, **FAST)


def tid(conn, description):
    return conn.execute("SELECT transaction_id FROM transactions WHERE name = ? ORDER BY date", (description,)).fetchone()[0]


def category(conn, description, n=0):
    return conn.execute("SELECT category FROM v_transactions WHERE name = ? ORDER BY date LIMIT 1 OFFSET ?",
                        (description, n)).fetchone()[0]


def cli_json(capsys, *argv):
    capsys.readouterr()
    assert cli.main([*argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_a_payroll_inflow_is_income_and_the_months_spending_is_the_outflows_only(conn, capsys):
    connect(conn, [*OUT, *PAY])
    # the CLI
    spend = cli_json(capsys, "spend", "--month", MONTH, "--by", "category")
    assert spend["total"] == OUTFLOWS and spend["count"] == 4
    assert {g["label"]: g["total"] for g in spend["groups"]} == {"Other": OUTFLOWS}
    # the Overview payload: summary, pace, categories and the cash-flow bars agree with it
    ov = api.overview(conn, {"month": MONTH}, TODAY)
    assert ov["summary"]["mtd"] == ov["pace"]["mtd"] == OUTFLOWS
    assert ov["cashflow"]["months"][-1]["spending"] == OUTFLOWS
    assert ov["cashflow"]["months"][-1]["income"] == 5128.96
    assert {c["category"]: c["spend"] for c in ov["categories"] if c["count"]} == {"Other": OUTFLOWS}   # no Income card
    # budget status counts the same
    status = cli_json(capsys, "budget-status", "--month", MONTH)
    assert status["uncapped"]["spent"] == OUTFLOWS and status["uncapped"]["count"] == 4
    # the agent tools (and so MCP) call the same reads
    assert tooldefs.run_read("finance_spend", {"month": MONTH}, config.db_path(), TODAY)["total"] == OUTFLOWS
    tool_status = tooldefs.run_read("finance_budget_status", {"month": MONTH}, config.db_path(), TODAY)
    assert tool_status["uncapped"]["spent"] == OUTFLOWS
    # and the inflow is filed as Income, money in with no category from the provider
    pay = conn.execute("SELECT pfc_primary, pfc_detailed, amount FROM transactions WHERE transaction_id = ?",
                       (tid(conn, "Pay day!"),)).fetchone()
    assert (pay["pfc_primary"], pay["amount"]) == ("INCOME", -2564.48)
    assert category(conn, "Pay day!") == "Income" and category(conn, "Grocery Mart") == "Other"


def test_a_category_the_user_set_wins_and_a_resync_keeps_it(conn, capsys):
    bridge = connect(conn, [*OUT, ("R1", date(2026, 11, 9), "30.00", "Grocery Mart refund")])
    assert category(conn, "Grocery Mart refund") == "Other"      # a refund is never Income
    classify.set_category(conn, tid(conn, "Grocery Mart refund"), "Groceries", "user")
    classify.set_category(conn, tid(conn, "Grocery Mart"), "Groceries", "user")
    resync(conn, bridge)
    resync(conn, bridge)
    row = conn.execute("SELECT category, category_source FROM v_transactions WHERE name = 'Grocery Mart refund'").fetchone()
    assert tuple(row) == ("Groceries", "user")
    spend = cli_json(capsys, "spend", "--month", MONTH, "--by", "category")
    assert {g["label"]: g["total"] for g in spend["groups"]}["Groceries"] == 70.0      # the refund nets in its category
    assert spend["total"] == round(OUTFLOWS - 30.0, 2)
    ov = api.overview(conn, {"month": MONTH}, TODAY)
    assert ov["summary"]["mtd"] == round(OUTFLOWS - 30.0, 2)


def test_a_user_rule_wins_over_the_income_hint(conn):
    connect(conn, [*OUT, ("R1", date(2026, 11, 9), "30.00", "Grocery Mart refund")])
    classify.add_rule(conn, "name", "Grocery Mart refund", "Groceries")
    classify.apply(conn)
    assert category(conn, "Grocery Mart refund") == "Groceries"


def test_transfers_stay_transfers_not_income_and_not_spending(conn, capsys):
    connect(conn, [*OUT, *PAY, ("X1", date(2026, 11, 4), "200.00", "Transfer from Savings"),
                   ("X2", date(2026, 11, 6), "75.00", "ACH XFER IN 4411"),
                   ("X3", date(2026, 11, 6), "50.00", "Payment from a friend")])
    assert category(conn, "Transfer from Savings") == "Transfers" and category(conn, "ACH XFER IN 4411") == "Transfers"
    assert category(conn, "Payment from a friend") == "Income"       # a payment word only counts on a card or loan
    ov = api.overview(conn, {"month": MONTH}, TODAY)
    assert ov["summary"]["mtd"] == OUTFLOWS
    assert ov["cashflow"]["months"][-1]["income"] == round(5128.96 + 50.0, 2)


def test_a_cards_own_payment_line_is_a_transfer(conn, capsys):
    connect(conn, [("C1", date(2026, 11, 2), "-60.00", "Hardware Store"),
                   ("C2", date(2026, 11, 8), "300.00", "ONLINE PAYMENT THANK YOU")], kind="credit", name="Synthetic Card")
    row = conn.execute("SELECT pfc_primary, pfc_detailed FROM transactions WHERE name = 'ONLINE PAYMENT THANK YOU'").fetchone()
    assert tuple(row) == ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    assert category(conn, "ONLINE PAYMENT THANK YOU") == "Transfers"
    assert cli_json(capsys, "spend", "--month", MONTH)["total"] == 60.0


def test_a_card_inflow_that_is_not_a_payment_is_a_refund_that_nets_against_spending(conn, capsys):
    connect(conn, [("C1", date(2026, 11, 2), "-60.00", "Hardware Store"),
                   ("C2", date(2026, 11, 8), "25.00", "AMAZON MKTPLACE")], kind="credit", name="Synthetic Card")
    row = conn.execute("SELECT pfc_primary, pfc_detailed FROM transactions WHERE name = 'AMAZON MKTPLACE'").fetchone()
    assert tuple(row) == (None, None)
    assert conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 0
    assert category(conn, "AMAZON MKTPLACE") == "Other"
    assert cli_json(capsys, "spend", "--month", MONTH)["total"] == 35.0     # 60 out, 25 back


def test_a_cards_payment_thank_you_line_stays_a_transfer(conn, capsys):
    connect(conn, [("C1", date(2026, 11, 2), "-60.00", "Hardware Store"),
                   ("C2", date(2026, 11, 8), "300.00", "PAYMENT THANK YOU")], kind="credit", name="Synthetic Card")
    assert category(conn, "PAYMENT THANK YOU") == "Transfers"
    assert cli_json(capsys, "spend", "--month", MONTH)["total"] == 60.0


def test_a_checking_refund_is_not_income(conn, capsys):
    connect(conn, [*OUT, ("R1", date(2026, 11, 9), "30.00", "Grocery Mart REFUND")])
    row = conn.execute("SELECT pfc_primary, pfc_detailed FROM transactions WHERE name = 'Grocery Mart REFUND'").fetchone()
    assert tuple(row) == (None, None)
    assert category(conn, "Grocery Mart REFUND") == "Other"
    ov = api.overview(conn, {"month": MONTH}, TODAY)
    assert ov["cashflow"]["months"][-1]["income"] == 0
    assert cli_json(capsys, "spend", "--month", MONTH)["total"] == round(OUTFLOWS - 30.0, 2)


def test_a_checking_payroll_inflow_is_still_income(conn):
    connect(conn, [*OUT, ("P1", date(2026, 11, 1), "2564.48", "ACME CORP PAYROLL")])
    assert category(conn, "ACME CORP PAYROLL") == "Income"
    ov = api.overview(conn, {"month": MONTH}, TODAY)
    assert ov["cashflow"]["months"][-1]["income"] == 2564.48


@pytest.mark.parametrize("description, kind, want", [
    ("Refund from the shop", "checking", (None, None)),
    ("RETURN CREDIT", "checking", (None, None)),
    ("Payment reversal", "checking", (None, None)),
    ("Credit Voucher 5521", "checking", (None, None)),
    ("REFUND TRANSFER", "checking", (None, None)),             # never Income, and not a transfer in either
    ("Refund", "credit", (None, None)),
    ("Refund payment", "credit", classify.CARD_PAYMENT_HINT),  # a card line naming a payment is the card's payment
    ("Anything at all", "loan", (None, None)),
    ("Transfer from Savings", "checking", classify.TRANSFER_IN_HINT),
    ("Transfer from Savings", "credit", (None, None)),
    ("Payroll", None, classify.INCOME_HINT),                   # an unknown type behaves as a bank account
    # whole words only (the word-boundary rule): RETURNS and REFUNDED are other words, so they are not matched
    ("Returns Mart deposit", "checking", classify.INCOME_HINT),
    ("Refunded deposit", "checking", classify.INCOME_HINT),
])
def test_inflow_hint_rules(description, kind, want):
    assert classify.inflow_hint(description, kind, -10.0) == want
    assert classify.inflow_hint(description, kind, 10.0) == (None, None)       # money out never gets one


def test_a_resync_clears_a_card_inflows_old_income_hint_and_filing(conn):
    connect(conn, [("C1", date(2026, 11, 2), "-60.00", "Hardware Store"),
                   ("C2", date(2026, 11, 8), "25.00", "AMAZON MKTPLACE")], kind="credit", name="Synthetic Card")
    t = tid(conn, "AMAZON MKTPLACE")
    # as the previous code left it: hinted Income and filed there by the plaid default
    with conn:
        conn.execute("UPDATE transactions SET pfc_primary = ?, pfc_detailed = ? WHERE transaction_id = ?",
                     (*classify.INCOME_HINT, t))
        conn.execute("INSERT OR REPLACE INTO classifications (transaction_id, category_id, source, updated_at) "
                     "SELECT ?, id, 'plaid', '2026-11-01T00:00:00Z' FROM categories WHERE name = 'Income'", (t,))
    assert category(conn, "AMAZON MKTPLACE") == "Income"
    later = Bridge(account_set([card(txs=[tx("N1", date(2026, 11, 14), "-5.00", "Newsstand")])]))
    resync(conn, later)
    row = conn.execute("SELECT pfc_primary, pfc_detailed FROM transactions WHERE transaction_id = ?", (t,)).fetchone()
    assert tuple(row) == (None, None)
    assert conn.execute("SELECT COUNT(*) FROM classifications WHERE transaction_id = ?", (t,)).fetchone()[0] == 0
    assert category(conn, "AMAZON MKTPLACE") == "Other"


def test_money_out_gets_no_hint(conn):
    connect(conn, OUT)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE pfc_primary IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 0   # still undecided for the AI / a person


def test_a_resync_fixes_rows_imported_before_the_hint_existed(conn, capsys):
    connect(conn, [*OUT, *PAY])
    # as the old code left them: no hint, no classification (P1); one filed Other by a stale plaid default (P2)
    p1, p2 = [r[0] for r in conn.execute("SELECT transaction_id FROM transactions WHERE name = 'Pay day!' ORDER BY date")]
    with conn:
        conn.execute("UPDATE transactions SET pfc_primary = NULL, pfc_detailed = NULL")
        conn.execute("DELETE FROM classifications")
        conn.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                     "SELECT ?, id, 'plaid', '2026-11-01T00:00:00Z' FROM categories WHERE name = 'Other'", (p2,))
    assert cli_json(capsys, "spend", "--month", MONTH)["total"] == round(OUTFLOWS - 5128.96, 2)    # the bug
    # the next sync returns none of these rows (the daily window is short), yet it still files them
    later = Bridge(account_set([card(txs=[tx("N1", date(2026, 11, 14), "-5.00", "Newsstand")])]))
    resync(conn, later)
    assert category(conn, "Pay day!", 0) == category(conn, "Pay day!", 1) == "Income"
    assert cli_json(capsys, "spend", "--month", MONTH)["total"] == OUTFLOWS + 5.0
    assert conn.execute("SELECT source FROM classifications WHERE transaction_id = ?", (p1,)).fetchone()[0] == "plaid"


def test_a_row_that_stops_being_money_in_loses_its_income_filing(conn):
    bridge = connect(conn, [("P1", date(2026, 11, 1), "100.00", "Odd entry")])
    assert category(conn, "Odd entry") == "Income"
    flipped = Bridge(account_set([card(txs=[tx("P1", date(2026, 11, 1), "-100.00", "Odd entry")])]))
    resync(conn, flipped)
    assert category(conn, "Odd entry") == "Other"
    assert conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 0


# --- Plaid parity (a control: green before and after the change) ----------------------------------------------

PLAID_TODAY = date(2026, 9, 26)
# sha256 of the sorted-key JSON of the numbers below on the synthetic Plaid demo database
PLAID_DIGEST = "a39c2a92d85b19f9f9e78ef9a300ab3a1f4b6559b9bd657fd5df67482f33d513"


def plaid_numbers(conn, capsys):
    ov = api.overview(conn, {"month": "2026-09"}, PLAID_TODAY)
    keys = ("summary", "pace", "budgets", "categories", "spend_groups", "cashflow", "category_history")
    return {"overview": {k: ov[k] for k in keys},
            "spend": cli_json(capsys, "spend", "--by", "category"),
            "spend_prev": cli_json(capsys, "spend", "--month", "2026-08", "--by", "category"),
            "budget": cli_json(capsys, "budget-status")}


def test_plaid_numbers_are_unchanged(conn, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: PLAID_TODAY)
    demo_data.build(conn, PLAID_TODAY)
    demo_data.extras(conn, PLAID_TODAY, holding_days=30)
    got = plaid_numbers(conn, capsys)
    assert got["spend"]["total"] > 0 and got["overview"]["summary"]["mtd"] == got["spend"]["total"]
    blob = json.dumps(got, sort_keys=True, default=str)
    assert hashlib.sha256(blob.encode()).hexdigest() == PLAID_DIGEST
    # nothing here is SimpleFIN, so nothing was hinted or refiled
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'simplefin'").fetchone()[0] == 0
