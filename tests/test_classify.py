"""Classification precedence, rules, views, and the AI seam."""

from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import add_item, page, txn
from hermes_finance import classify, cli
from hermes_finance.sync import sync_all


@pytest.fixture
def synced(conn, client, fake):
    add_item(conn)
    fake.on("/transactions/sync", page(added=[
        txn("t_grocery", 55.0, "WHOLE FOODS", "Whole Foods", "2026-08-03", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")),
        # a detailed code the map doesn't list (v16 maps FOOD_AND_DRINK_COFFEE to Coffee): the primary decides
        txn("t_coffee", 4.5, "BLUE BOTTLE", "Blue Bottle", "2026-08-05", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD")),
        txn("t_coffee2", 5.5, "BLUE BOTTLE #2", "Blue Bottle", "2026-09-02", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD")),
        txn("t_netflix", 15.49, "NETFLIX.COM", "Netflix", "2026-08-10", ("ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES")),
        txn("t_spotify", 11.99, "SPOTIFY USA", None, "2026-08-11", ("ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO")),
        txn("t_pay", -3000.0, "ACME PAYROLL", None, "2026-08-15", ("INCOME", "INCOME_WAGES")),
        txn("t_ccpay", 500.0, "CARD PAYMENT", None, "2026-08-20", ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")),
        txn("t_weird", 9.0, "MYSTERY", None, "2026-08-21", ("SOMETHING_NEW", "SOMETHING_NEW_X")),
        txn("t_refund", -20.0, "WHOLE FOODS REFUND", "Whole Foods", "2026-08-25", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")),
    ], next_cursor="c1"))
    sync_all(conn, client)  # runs classify.apply at the end
    return conn


def cat_of(conn, tid):
    return tuple(conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id=?",
                              (tid,)).fetchone())


def test_plaid_defaults(synced):
    assert cat_of(synced, "t_grocery") == ("Groceries", "plaid")        # detailed mapping wins
    assert cat_of(synced, "t_coffee") == ("Eating Out", "plaid")         # primary fallback
    assert cat_of(synced, "t_pay") == ("Income", "plaid")
    assert cat_of(synced, "t_ccpay") == ("Transfers", "plaid")
    assert cat_of(synced, "t_weird") == ("Other", "plaid")


def test_view_falls_back_to_plaid_map_without_classification_rows(synced):
    with synced:
        synced.execute("DELETE FROM classifications")
    assert cat_of(synced, "t_grocery") == ("Groceries", "plaid")
    assert cat_of(synced, "t_weird") == ("Other", "default")


def test_rules_exact_regex_and_priority(synced):
    # ai rules: a user rule would re-file its rows as it's saved, leaving apply() nothing to count
    classify.add_rule(synced, "merchant_name", "netflix", "Subscriptions", created_from="ai")  # exact, case-insensitive
    classify.add_rule(synced, "name", r"spotify|hulu", "Subscriptions", "regex", created_from="ai")
    classify.add_rule(synced, "name", r"^BLUE BOTTLE$", "Groceries", "regex", priority=10, created_from="ai")
    classify.add_rule(synced, "name", r"^blue bottle", "Entertainment", "regex", priority=200, created_from="ai")
    counts = classify.apply(synced)
    assert counts["rule"] == 4
    assert cat_of(synced, "t_netflix") == ("Subscriptions", "rule")
    assert cat_of(synced, "t_spotify") == ("Subscriptions", "rule")
    assert cat_of(synced, "t_coffee") == ("Entertainment", "rule")   # priority 200 beats 10
    # removing the rule reverts to the Plaid default on the next apply
    with synced:
        synced.execute("DELETE FROM rules WHERE pattern='netflix'")
    classify.apply(synced)
    assert cat_of(synced, "t_netflix") == ("Entertainment", "plaid")


def test_user_classifications_never_overwritten(synced, client, fake):
    assert classify.set_category(synced, "t_coffee", "Other Shopping", "user") == "set"
    classify.apply(synced)
    assert classify.set_category(synced, "t_coffee", "Other Health", "ai", 0.9) == "kept-user"
    classify.add_rule(synced, "merchant_name", "Blue Bottle", "Travel", priority=999)
    classify.apply(synced)
    # a later sync modifying the transaction keeps the user decision
    fake.on("/transactions/sync", page(modified=[txn("t_coffee", 4.75)], next_cursor="c2"))
    sync_all(synced, client)
    assert cat_of(synced, "t_coffee") == ("Other Shopping", "user")
    assert cat_of(synced, "t_coffee2") == ("Travel", "rule")  # the rule still applies elsewhere
    # and the database itself refuses a non-user overwrite
    with pytest.raises(sqlite3.DatabaseError, match="never overwritten"):
        with synced:
            synced.execute("UPDATE classifications SET source='ai' WHERE transaction_id='t_coffee'")
    # user may change their own mind
    assert classify.set_category(synced, "t_coffee", "Other Health", "user") == "set"
    assert cat_of(synced, "t_coffee") == ("Other Health", "user")


def test_ai_classification_beats_rules_and_plaid(synced):
    classify.set_category(synced, "t_netflix", "Subscriptions", "ai", 0.8, "streaming")
    classify.add_rule(synced, "merchant_name", "Netflix", "Other", created_from="ai")  # a user rule would win
    classify.apply(synced)
    row = synced.execute("SELECT category, category_source, category_confidence, category_note FROM v_transactions "
                         "WHERE transaction_id='t_netflix'").fetchone()
    assert tuple(row) == ("Subscriptions", "ai", 0.8, "streaming")


def test_cli_set_category_and_make_rule(synced, capsys):
    assert cli.main(["set-category", "t_coffee", "eating out", "--source", "ai", "--confidence", "0.7",
                     "--make-rule"]) == 0
    assert cat_of(synced, "t_coffee") == ("Eating Out", "ai")
    assert cat_of(synced, "t_coffee2") == ("Eating Out", "rule")
    rule = synced.execute("SELECT match_field, match_type, pattern, created_from FROM rules").fetchone()
    assert tuple(rule) == ("merchant_name", "exact", "Blue Bottle", "ai")

    assert cli.main(["set-category", "t_coffee", "Other Health"]) == 0            # default source = user
    assert cli.main(["set-category", "t_coffee", "Travel", "--source", "ai"]) == 1
    assert "kept existing user classification" in capsys.readouterr().out
    assert cat_of(synced, "t_coffee") == ("Other Health", "user")

    assert cli.main(["set-category", "t_coffee", "NotACategory"]) == 2
    assert "Valid:" in capsys.readouterr().err


def test_unclassified_seam(synced, capsys):
    assert cli.main(["unclassified", "--limit", "100", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    ids = {t["transaction_id"] for t in out["transactions"]}
    assert "t_coffee" in ids and out["count"] == 9
    assert "Subscriptions" in [c["name"] for c in out["categories"]]
    coffee = next(t for t in out["transactions"] if t["transaction_id"] == "t_coffee")
    assert coffee["suggested_category"] == "Eating Out" and coffee["pfc_confidence"] == "HIGH"

    classify.set_category(synced, "t_coffee", "Eating Out", "ai", 0.95)
    classify.add_rule(synced, "merchant_name", "Netflix", "Subscriptions")
    classify.apply(synced)
    remaining = {t["transaction_id"] for t in classify.unclassified(synced, 100)}
    assert "t_coffee" not in remaining and "t_netflix" not in remaining
    assert [t["transaction_id"] for t in classify.unclassified(synced, 2)] == ["t_coffee2", "t_refund"]


def test_views_spend_month_and_caps(synced, capsys):
    row = synced.execute("SELECT month, amount, spend FROM v_transactions WHERE transaction_id='t_pay'").fetchone()
    assert tuple(row) == ("2026-08", -3000.0, -3000.0)  # negative spend = money in
    with synced:
        synced.execute("UPDATE categories SET monthly_cap=30 WHERE name='Groceries'")
        synced.execute("UPDATE categories SET monthly_cap=100 WHERE name='Eating Out'")
    rows = {(r["month"], r["category"]): dict(r) for r in synced.execute("SELECT * FROM v_monthly_category_spend")}
    g = rows[("2026-08", "Groceries")]
    assert (g["spend"], g["cap"], g["over_cap"], g["txn_count"]) == (35.0, 30.0, 1, 2)  # 55 - 20 refund
    e = rows[("2026-08", "Eating Out")]
    assert (e["spend"], e["over_cap"]) == (4.5, 0)
    assert rows[("2026-08", "Transfers")]["is_transfer"] == 1
    assert rows[("2026-08", "Income")]["is_income"] == 1


def test_removed_transactions_excluded_from_views(synced, client, fake):
    fake.on("/transactions/sync", page(removed=["t_grocery"], next_cursor="c2"))
    sync_all(synced, client)
    assert synced.execute("SELECT COUNT(*) FROM v_transactions WHERE transaction_id='t_grocery'").fetchone()[0] == 0
    g = synced.execute("SELECT spend FROM v_monthly_category_spend WHERE month='2026-08' AND category='Groceries'")
    assert g.fetchone()[0] == -20.0


def test_classify_apply_cli_is_idempotent(synced, capsys):
    assert cli.main(["classify-apply"]) == 0
    assert "0 set by rules, 0 set to Plaid defaults, 9 unchanged" in capsys.readouterr().out


def test_invalid_regex_rejected(synced):
    with pytest.raises(classify.ClassifyError, match="invalid regex"):
        classify.add_rule(synced, "name", "(unclosed", "Other", "regex")
