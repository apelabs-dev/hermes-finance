"""Multi-currency totals (t_3f83e261): every aggregate is one currency, and nothing is converted.

Net worth and Cash are one number in the display currency; foreign accounts sit in their own card and
are never added. Spending/budgets/cash flow are display currency too, with foreign spend shown beside a
category. Transaction rows and day totals carry each currency separately.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from hermes_finance import api, config, db, goals, insights, money, portfolio, prefs, reads, tooldefs

TODAY = date(2026, 11, 15)
TS = "2026-11-01T00:00:00Z"


def _account(conn, account_id, name, currency, current, type_="depository", subtype="checking"):
    conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                 "available_balance, iso_currency, updated_at) VALUES (?, 'item_a', ?, ?, ?, ?, ?, ?, ?, ?)",
                 (account_id, name, account_id[-4:], type_, subtype, current, current, currency, TS))


def _txn(conn, tid, account_id, day, amount, currency, name, pfc=("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")):
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                 "iso_currency, pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at) "
                 "VALUES (?, ?, 'item_a', ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?)",
                 (tid, account_id, day, name, name, amount, currency, pfc[0], pfc[1], TS, TS))


@pytest.fixture
def mixed(conn):
    with conn:
        conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                     "VALUES ('item_a', 'ins_1', 'Bank', 'tok', 'active', ?)", (TS,))
        _account(conn, "acc_usd", "Dollar checking", "USD", 1000)
        _account(conn, "acc_krw", "Won checking", "KRW", 500000)
        _account(conn, "acc_eur", "Euro checking", "EUR", 500)
        # Groceries in three currencies; Coffee in dollars
        _txn(conn, "t_usd", "acc_usd", "2026-11-02", 100.0, "USD", "Market")
        _txn(conn, "t_usd2", "acc_usd", "2026-11-02", 50.0, "USD", "Cafe",
             ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"))
        _txn(conn, "t_krw", "acc_krw", "2026-11-02", 12000.0, "KRW", "Seoul market")
        _txn(conn, "t_eur", "acc_eur", "2026-11-03", 25.0, "EUR", "Berlin market")
        for acct, day, cur, bal in (("acc_usd", "2026-11-01", "USD", 990), ("acc_usd", "2026-11-02", "USD", 1000),
                                    ("acc_krw", "2026-11-01", "KRW", 512000), ("acc_krw", "2026-11-02", "KRW", 500000),
                                    ("acc_eur", "2026-11-01", "EUR", 525), ("acc_eur", "2026-11-02", "EUR", 500)):
            conn.execute("INSERT INTO balance_snapshots (account_id, date, current, available, iso_currency) "
                         "VALUES (?, ?, ?, ?, ?)", (acct, day, bal, bal, cur))
        conn.execute("UPDATE categories SET monthly_cap = 200 WHERE name = 'Groceries'")
    return conn


def test_spending_and_cashflow_are_the_display_currency_only(mixed):
    assert insights._spend(mixed, "2026-11") == 150.0
    assert insights._income(mixed, "2026-11") == 0.0
    s = insights.month_summary(mixed, "2026-11", TODAY)
    assert s["mtd"] == 150.0
    flow = insights.cashflow(mixed, 6, TODAY)
    nov = next(m for m in flow["months"] if m["month"] == "2026-11")
    assert (nov["spending"], nov["income"], nov["net"]) == (150.0, 0.0, -150.0)


def test_net_worth_is_one_number_and_foreign_accounts_are_not_counted(mixed):
    nw = insights.net_worth(mixed, history=True, today=TODAY)
    assert nw["assets"] == 1000.0 and nw["debts"] == 0.0 and nw["net"] == 1000.0
    assert {a["currency"] for a in nw["foreign"]} == {"KRW", "EUR"}
    assert {a["currency"] for a in nw["institutions"][0]["accounts"]} == {"USD"}
    assert nw["history"][-1]["net"] == 1000.0      # foreign snapshots are not in the display-currency line
    assert nw["custom"]["net"] == 1000.0           # Custom cannot resurrect a foreign account either
    r = reads.networth(mixed, TODAY, history=True)
    assert r["currency"] == "USD" and r["net"] == 1000.0
    assert {a["currency"] for a in r["other_currencies"]} == {"KRW", "EUR"}
    text = reads.networth_text(r)
    assert "Not counted (other currencies): €500.00, KRW 500,000" in text
    assert "$1,000" in text


def test_transactions_keep_the_row_currency_and_each_day_has_one_total_per_currency(mixed):
    p = api.transactions(mixed, {"month": "2026-11"})
    assert p["sums"] == {"USD": 150.0, "KRW": 12000.0, "EUR": 25.0}
    assert p["sum"] == 150.0                       # the display-currency figure the page has always shown
    assert p["days"]["2026-11-02"]["totals"] == {"USD": 150.0, "KRW": 12000.0}
    assert p["days"]["2026-11-02"]["total"] == 150.0
    assert {(t["transaction_id"], t["iso_currency"]) for t in p["rows"]} == \
        {("t_usd", "USD"), ("t_usd2", "USD"), ("t_krw", "KRW"), ("t_eur", "EUR")}
    # no code path added the raw values: 150 + 12,000 + 25 would be 12,175
    assert p["sums"]["USD"] + p["sums"]["KRW"] + p["sums"]["EUR"] == 12175.0
    assert reads.spend(mixed, reads.parse_filters(mixed, TODAY, month="2026-11", spending=True), TODAY)["total"] == 150.0


def test_budget_bar_ignores_foreign_spend_but_shows_it(mixed):
    cats = insights.category_spend(mixed, "2026-11", TODAY)
    groc = next(c for c in cats if c["category"] == "Groceries")
    assert groc["spend"] == 100.0 and groc["status"] == "ok"
    assert groc["foreign"] == [{"currency": "EUR", "spend": 25.0, "count": 1},
                               {"currency": "KRW", "spend": 12000.0, "count": 1}]
    b = insights.budgets(mixed, "2026-11", TODAY, cats)
    assert b["total_spend"] == 150.0 and b["over"] == 0
    status = reads.budget_status(mixed, "2026-11", TODAY)
    cap = next(c for c in status["caps"] if c["category"] == "Groceries")
    assert cap["spent"] == 100.0 and cap["cap"] == 200.0 and cap["status"] == "ok"
    assert {f["currency"] for f in cap["foreign"]} == {"KRW", "EUR"}
    text = reads.budget_status_text(status)
    assert "Groceries: also €25.00, KRW 12,000" in text


def test_goals_follow_their_account_and_unlinked_use_the_display_currency(mixed):
    with mixed:
        mixed.execute("INSERT INTO goals (kind, name, target, track, account_id, created_at, updated_at) VALUES "
                      "('save', 'Won goal', 5000000, 'account', 'acc_krw', ?, ?)", (TS, TS))
        mixed.execute("INSERT INTO goals (kind, name, target, track, account_id, created_at, updated_at) VALUES "
                      "('save', 'Dollar goal', 2000, 'manual', NULL, ?, ?)", (TS, TS))
    p = goals.payload(mixed, TODAY)
    by = {g["name"]: g for g in p["goals"]}
    assert by["Won goal"]["currency"] == "KRW" and by["Dollar goal"]["currency"] == "USD"
    assert p["totals"]["saving_target"] == 2000.0            # one currency, not 5,002,000
    assert {t["currency"]: t["saving_target"] for t in p["totals_by_currency"]} == {"KRW": 5000000.0, "USD": 2000.0}
    text = goals.text(p)
    assert "KRW 5,000,000" in text and "$2,000" in text


def test_a_display_currency_change_reformats_and_never_converts(mixed):
    prefs.save(mixed, {"region.currency": "EUR"})
    nw = insights.net_worth(mixed, history=True, today=TODAY)
    assert nw["assets"] == 500.0 and nw["net"] == 500.0       # only the euro account counts now
    assert {a["currency"] for a in nw["foreign"]} == {"USD", "KRW"}
    assert insights._spend(mixed, "2026-11") == 25.0
    cats = insights.category_spend(mixed, "2026-11", TODAY)
    groc = next(c for c in cats if c["category"] == "Groceries")
    assert groc["spend"] == 25.0 and {f["currency"] for f in groc["foreign"]} == {"USD", "KRW"}
    assert money.money2(groc["spend"], currency="EUR") == "€25.00"
    assert money.money(12000, currency="KRW") == "KRW 12,000"


def test_investments_and_debt_pages_drop_foreign_accounts_from_their_totals(mixed):
    with mixed:
        conn = mixed
        conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                     "available_balance, iso_currency, updated_at) VALUES ('acc_eur_inv', 'item_a', 'Euro brokerage', "
                     "'9999', 'investment', 'brokerage', 777.0, 777.0, 'EUR', ?)", (TS,))
        conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                     "available_balance, iso_currency, updated_at) VALUES ('acc_krw_loan', 'item_a', 'Won loan', "
                     "'8888', 'loan', 'auto', 100000.0, 100000.0, 'KRW', ?)", (TS,))
    inv = portfolio.investments(mixed, TODAY)
    assert inv["total"] == 0.0 and inv["holdings_total"] == 0.0
    assert [(a["id"], a["currency"], a["counted"]) for a in inv["accounts"]] == [("acc_eur_inv", "EUR", False)]
    debt = portfolio.debt(mixed, TODAY)
    assert debt["total"] == 0.0 and debt["monthly"]["total"] == 0.0
    assert [(l["id"], l["currency"], l["counted"]) for l in debt["loans"]] == [("acc_krw_loan", "KRW", False)]


def test_the_mcp_and_ask_tools_carry_the_codes(mixed, data_dir):
    payload = tooldefs.run_read("finance_networth", {}, config.db_path(), TODAY)
    assert payload["currency"] == "USD" and payload["net"] == 1000.0
    assert {a["currency"] for a in payload["other_currencies"]} == {"KRW", "EUR"}
    txs = tooldefs.run_read("finance_transactions", {"month": "2026-11"}, config.db_path(), TODAY)
    assert {t["iso_currency"] for t in txs["rows"]} == {"USD", "KRW", "EUR"}
    assert txs["sums"] == {"USD": 150.0, "KRW": 12000.0, "EUR": 25.0}
    rendered = tooldefs.render(txs)
    assert "KRW" in rendered and "EUR" in rendered and "12175" not in rendered
    assert "dollar" not in tooldefs.instructions().lower()
