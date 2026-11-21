"""Refunds are never Income, from any source (owner decision). A Plaid row whose default lands in an income category
(Plaid sometimes sends INCOME_* for a merchant credit) is filed Other instead when it is money in on a credit or loan
account, or when its name says REFUND, RETURN, REVERSAL or CREDIT VOUCHER, so it nets against spending. The guard
touches only the default (source 'plaid' or no filing): a rule, the AI's pick or the user's own choice stays, and a
Plaid merchant category (a refund Plaid files under General merchandise) keeps the merchant's category."""

from __future__ import annotations

import json
from datetime import date

import pytest

import factory
from hermes_finance import classify, cli, db

TODAY = date(2026, 11, 15)
MONTH = "2026-11"
INCOME_OTHER = ("INCOME", "INCOME_OTHER_INCOME")
SALARY = ("INCOME", "INCOME_WAGES")
MERCH = ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES")


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)


@pytest.fixture
def accounts(conn):
    factory.bank(conn)
    factory.acct(conn, "acc_chk", type_="depository", subtype="checking")
    factory.acct(conn, "acc_card", name="Card", type_="credit", subtype="credit card", mask="9999")
    factory.acct(conn, "acc_loan", name="Loan", type_="loan", subtype="auto", mask="7777")
    return conn


def add(conn, tid, amount, name, pfc, account="acc_card", day="2026-11-08", merchant=None):
    raw = {"transaction_id": tid, "amount": amount, "date": day, "name": name}
    with conn:
        conn.execute(
            """INSERT INTO transactions (transaction_id, account_id, item_id, date, authorized_date, name, merchant_name,
                   amount, iso_currency, pending, pfc_primary, pfc_detailed, pfc_confidence, raw_json, removed,
                   first_seen_at, updated_at, source)
               VALUES (?,?,?,?,?,?,?,?, 'USD', 0, ?,?, 'HIGH', ?, 0, ?, ?, 'plaid')""",
            (tid, account, "item_a", day, day, name, merchant, amount, pfc[0], pfc[1], json.dumps(raw), db.now(), db.now()))


def cat(conn, tid):
    return tuple(conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id = ?",
                              (tid,)).fetchone())


def spend_total(capsys):
    capsys.readouterr()
    assert cli.main(["spend", "--month", MONTH, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["total"]


def test_a_credit_account_inflow_with_an_income_code_defaults_to_other_and_nets_against_spending(accounts, capsys):
    conn = accounts
    add(conn, "buy", 60.0, "HARDWARE STORE", MERCH)
    add(conn, "back", -20.0, "AMAZON MKTPLACE", INCOME_OTHER)
    classify.apply(conn)
    assert cat(conn, "back") == ("Other", "plaid")
    assert spend_total(capsys) == 40.0


def test_a_loan_account_inflow_with_an_income_code_defaults_to_other(accounts):
    add(accounts, "back", -20.0, "SOMETHING CREDITED", INCOME_OTHER, account="acc_loan")
    classify.apply(accounts)
    assert cat(accounts, "back") == ("Other", "plaid")


def test_a_bank_refund_with_an_income_code_defaults_to_other(accounts):
    add(accounts, "r1", -15.0, "Grocery Mart REFUND", INCOME_OTHER, account="acc_chk")
    add(accounts, "r2", -15.0, "Mart return posted", INCOME_OTHER, account="acc_chk")
    add(accounts, "r3", -15.0, "Reversal of fee", INCOME_OTHER, account="acc_chk")
    add(accounts, "r4", -15.0, "CREDIT VOUCHER 4411", INCOME_OTHER, account="acc_chk")
    classify.apply(accounts)
    assert [cat(accounts, t) for t in ("r1", "r2", "r3", "r4")] == [("Other", "plaid")] * 4


def test_a_bank_payroll_inflow_stays_income(accounts):
    add(accounts, "pay", -3000.0, "ACME PAYROLL", SALARY, account="acc_chk")
    add(accounts, "int", -2.0, "Interest paid", INCOME_OTHER, account="acc_chk")
    # RETURNS and REFUNDED are other words: the whole-word rule does not match them
    add(accounts, "rm", -9.0, "Returns Mart deposit", INCOME_OTHER, account="acc_chk")
    add(accounts, "rd", -9.0, "Refunded deposit", INCOME_OTHER, account="acc_chk")
    classify.apply(accounts)
    assert [cat(accounts, t) for t in ("pay", "int", "rm", "rd")] == [("Income", "plaid")] * 4


def test_an_ai_or_user_income_filing_on_a_card_inflow_is_kept(accounts):
    add(accounts, "by_ai", -20.0, "AMAZON MKTPLACE", INCOME_OTHER)
    add(accounts, "by_user", -20.0, "AMAZON MKTPLACE 2", INCOME_OTHER)
    add(accounts, "by_rule", -20.0, "CASHBACK BONUS", INCOME_OTHER)
    classify.set_category(accounts, "by_ai", "Income", "ai")
    classify.set_category(accounts, "by_user", "Income", "user")
    classify.add_rule(accounts, "name", "CASHBACK BONUS", "Income")
    classify.apply(accounts)
    assert cat(accounts, "by_ai") == ("Income", "ai")
    assert cat(accounts, "by_user") == ("Income", "user")
    assert cat(accounts, "by_rule") == ("Income", "rule")


def test_a_merchant_category_refund_keeps_the_merchants_category(accounts):
    add(accounts, "back", -20.0, "AMAZON MKTPLACE REFUND", MERCH)
    add(accounts, "bank_back", -20.0, "Shop return", MERCH, account="acc_chk")
    classify.apply(accounts)
    assert cat(accounts, "back")[0] == cat(accounts, "bank_back")[0] == "Other Shopping"


def test_money_out_with_an_income_code_is_not_touched(accounts):
    add(accounts, "odd", 20.0, "Odd debit", INCOME_OTHER, account="acc_card")
    classify.apply(accounts)
    assert cat(accounts, "odd") == ("Income", "plaid")        # only money in is a refund


def test_releasing_a_rule_files_the_row_the_way_apply_does(accounts):
    add(accounts, "back", -20.0, "CASHBACK BONUS", INCOME_OTHER)
    rule = classify.add_rule(accounts, "name", "CASHBACK BONUS", "Groceries")
    assert cat(accounts, "back") == ("Groceries", "rule")
    classify.edit_rule(accounts, rule.id, "Groceries", pattern="NO SUCH NAME")     # it no longer matches
    assert cat(accounts, "back") == ("Other", "plaid")


def test_default_filing_names_the_same_words_as_the_simplefin_hint():
    for text in ("REFUND", "return", "Reversal", "credit voucher", "A CREDIT  VOUCHER"):
        assert classify.REFUND_WORDS.search(text), text
    for text in ("Returns", "Refunded", "voucher credit", "Credit"):
        assert not classify.REFUND_WORDS.search(text), text


# --- a tax refund stays Income (owner decision) ----------------------------------------------------------------

TAX = ("INCOME", "INCOME_TAX_REFUND")


def test_a_plaid_tax_refund_with_its_own_code_stays_income_and_spending_is_unchanged(accounts, capsys):
    add(accounts, "buy", 60.0, "HARDWARE STORE", MERCH, account="acc_chk")
    classify.apply(accounts)
    before = spend_total(capsys)
    add(accounts, "tax", -1840.0, "NYS DTF PIT TAX REFUND", TAX, account="acc_chk")
    classify.apply(accounts)
    assert cat(accounts, "tax") == ("Income", "plaid")
    assert spend_total(capsys) == before == 60.0


def test_a_tax_refund_named_only_in_words_with_a_plain_income_code_stays_income(accounts):
    add(accounts, "irs", -900.0, "IRS TREAS 310 TAX REF", ("INCOME", None), account="acc_chk")
    add(accounts, "irs2", -900.0, "IRS TREAS 310 TAX REFUND", INCOME_OTHER, account="acc_chk")
    classify.apply(accounts)
    assert cat(accounts, "irs") == ("Income", "plaid")
    assert cat(accounts, "irs2") == ("Income", "plaid")


def test_a_tax_refund_on_a_card_is_still_not_income_and_an_amazon_refund_on_checking_is_not_income(accounts):
    add(accounts, "card", -20.0, "TAX REFUND CREDIT", TAX)
    add(accounts, "amz", -20.0, "AMAZON REFUND", INCOME_OTHER, account="acc_chk")
    classify.apply(accounts)
    assert cat(accounts, "card") == ("Other", "plaid")
    assert cat(accounts, "amz") == ("Other", "plaid")
