"""v23 hidden categories: a hidden category leaves the selection surfaces (the picker's payload, the AI categorizer's
list, the assistant's category list) and the Where-it-went chart, while every historical total keeps counting it:
month spend, budgets, cash flow. Unhide puts the chart entry back."""

from __future__ import annotations

import json
from datetime import date, timedelta

from hermes_finance import ai, api, catops, config, insights, reads, tooldefs

TS = "2026-08-01T00:00:00Z"


def rows(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args)]


def seed_today(conn):
    today = date.today()
    month = insights.month_of(today)
    catops.create(conn, "Kids", group="Home", icon="tent")
    conn.execute("INSERT INTO items (item_id, institution_name, access_token, status, created_at) "
                 "VALUES ('item_a', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, type, subtype, current_balance, updated_at) "
                 "VALUES ('acc_chk', 'item_a', 'Checking', 'depository', 'checking', 1000, ?)", (TS,))
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                 "first_seen_at, updated_at) VALUES ('t1', 'acc_chk', 'item_a', ?, 'KIDS STORE', 'Kids Store', 40, ?, ?)",
                 (today.isoformat(), TS, TS))
    conn.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                 "SELECT 't1', id, 'user', ? FROM categories WHERE name = 'Kids'", (TS,))
    conn.execute("UPDATE categories SET monthly_cap = 100 WHERE name = 'Kids'")
    conn.commit()
    return today, month


def total(conn, month):
    return insights.spend_by(conn, "v.month = ?", (month,))[None][0]


def test_a_hidden_category_still_counts_in_every_money_total(conn):
    today, month = seed_today(conn)
    before = total(conn, month)
    assert before == 40.0
    catops.set_hidden(conn, "Kids", True)
    assert total(conn, month) == before
    cat = next(c for c in insights.category_spend(conn, month, today) if c["category"] == "Kids")
    assert (cat["spend"], cat["cap"], cat["status"]) == (40.0, 100.0, "ok")
    assert reads.budget_status(conn, month, today)["totals"]["spent"] == before
    flow = reads.cashflow(conn, today, 1)["months"][-1]
    assert flow["spending"] == before
    assert api.overview(conn, {}, today)["summary"]["mtd"] == before


def test_a_hidden_category_leaves_the_new_selection_surfaces_but_stays_in_the_full_reads(conn):
    today, month = seed_today(conn)
    catops.set_hidden(conn, "Kids", True)
    assert next(c for c in api.categories(conn) if c["name"] == "Kids")["hidden"] is True
    groups = api.category_groups(conn)
    assert any(c["name"] == "Kids" and c["hidden"] for g in groups["groups"] for c in g["categories"])
    queued = ai.queue(conn, 0)
    assert "Kids" not in {c["name"] for c in queued["categories"]}
    assert "Coffee" in {c["name"] for c in queued["categories"]}
    payload = tooldefs.run_read("finance_categories", {}, config.db_path(), today)
    assert "Kids" in payload["hidden"]
    assert next(c for c in payload["categories"] if c["name"] == "Kids")["hidden"] is True
    assert next(c for c in payload["categories"] if c["name"] == "Kids")["spend"] == 40.0


def test_the_chart_drops_a_hidden_category_while_the_total_keeps_it_and_unhide_restores_it(conn):
    today, month = seed_today(conn)
    assert "Kids" not in api.overview(conn, {}, today)["chart"]["excluded"]
    catops.set_hidden(conn, "Kids", True)
    overview = api.overview(conn, {}, today)
    assert "Kids" in overview["chart"]["excluded"]
    assert overview["summary"]["mtd"] == 40.0
    assert json.loads(rows(conn, "SELECT value FROM settings WHERE key = 'chart_excluded_categories'")[0][0]) == ["Kids"]
    catops.set_hidden(conn, "Kids", False)
    assert "Kids" not in api.overview(conn, {}, today)["chart"]["excluded"]
    assert total(conn, month) == 40.0


def test_hiding_never_erases_rules_or_filings_and_classify_keeps_accepting_the_name(conn):
    today, month = seed_today(conn)
    conn.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
                 "SELECT 'merchant_name', 'exact', 'Kids Store', id, 200, 'user', ? FROM categories WHERE name = 'Kids'",
                 (TS,))
    conn.commit()
    catops.set_hidden(conn, "Kids", True)
    assert rows(conn, "SELECT category_id FROM classifications") == [(rows(conn, "SELECT id FROM categories WHERE name = 'Kids'")[0][0],)]
    # classify.category_id by name still resolves a hidden target (an existing rule must keep filing there)
    from hermes_finance import classify
    assert classify.category_id(conn, "kids") == rows(conn, "SELECT id FROM categories WHERE name = 'Kids'")[0][0]
    assert total(conn, month) == 40.0


def test_unhide_keeps_a_chart_exclusion_the_user_set_before_hiding(conn):
    today, month = seed_today(conn)
    api.save_settings(conn, {"chart_excluded_categories": ["Kids", "Coffee"]})
    catops.set_hidden(conn, "Kids", True)
    catops.set_hidden(conn, "Kids", False)
    assert api.chart_excluded(conn) == ["Kids", "Coffee"]
    # one hide added (Coffee's was the user's): unhiding both takes off only what hide put there
    catops.set_hidden(conn, "Coffee", True)
    catops.set_hidden(conn, "Groceries", True)
    catops.set_hidden(conn, "Groceries", False)
    catops.set_hidden(conn, "Coffee", False)
    assert api.chart_excluded(conn) == ["Kids", "Coffee"]


def test_the_hide_record_follows_a_rename(conn):
    seed_today(conn)
    catops.set_hidden(conn, "Kids", True)
    catops.rename(conn, "Kids", "Children")
    assert api.chart_excluded(conn) == ["Children"]
    catops.set_hidden(conn, "Children", False)
    assert api.chart_excluded(conn) == []
