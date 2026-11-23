"""v23 move-and-delete (catops.delete): every one of the eight stores is reassigned in one transaction, the two
name-keyed stores (saved filters, the chart's hidden list) follow, the source row is removed last, a failure injected at
the last step rolls all of it back, and every invalid destination is refused before anything moves. Decided approval
history keeps its arguments."""

from __future__ import annotations

import json

import pytest

from hermes_finance import api, catops, insights

TS = "2026-08-01T00:00:00Z"


def rows(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args)]


def seed(conn, source="Kids", dest="Children"):
    """A destination and a source user category, and one row in every store that references the source."""
    catops.create(conn, dest)
    catops.create(conn, source)
    src = rows(conn, "SELECT id FROM categories WHERE name = ?", source)[0][0]
    dest_id = rows(conn, "SELECT id FROM categories WHERE name = ?", dest)[0][0]
    conn.execute("INSERT INTO items (item_id, institution_name, access_token, status, created_at) "
                 "VALUES ('item_a', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, type, subtype, current_balance, updated_at) "
                 "VALUES ('acc_chk', 'item_a', 'Checking', 'depository', 'checking', 100, ?)", (TS,))
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, first_seen_at, "
                 "updated_at) VALUES ('t1', 'acc_chk', 'item_a', '2026-08-02', 'MART', 10, ?, ?)", (TS, TS))
    conn.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
                 "VALUES ('merchant_name', 'exact', 'Mart', ?, 200, 'user', ?)", (src, TS))
    rule_id = rows(conn, "SELECT id FROM rules")[0][0]
    conn.execute("INSERT INTO classifications (transaction_id, category_id, source, rule_id, updated_at) "
                 "VALUES ('t1', ?, 'rule', ?, ?)", (src, rule_id, TS))
    conn.execute("INSERT INTO plaid_category_map (pfc_code, category_id) VALUES ('USER_KIDS', ?)", (src,))
    conn.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, category_id) "
                 "VALUES ('t1', 'run_1', ?, 'confirmed', ?)", (TS, src))
    conn.execute("INSERT INTO transaction_splits (transaction_id, line, category_id, amount, created_at) "
                 "VALUES ('t1', 1, ?, 10, ?)", (src, TS))
    conn.execute("INSERT INTO category_rollover (category_id, enabled_at) VALUES (?, ?)", (src, TS))
    conn.execute("INSERT INTO rule_deletions (receipt_hash, rule_id, match_field, match_type, pattern, category_id, "
                 "priority, created_from, created_at, filed, deleted_at) VALUES "
                 "('r1', 1, 'merchant_name', 'exact', 'Mart', ?, 200, 'user', ?, 1, ?)", (src, TS, TS))
    conn.execute("INSERT INTO pending_actions (tool, arguments, summary, client, status, created_at) VALUES "
                 "('finance_set_category', ?, 'File it under Kids', 'test', 'pending', ?)",
                 (json.dumps({"transaction_id": "t1", "category": source}), TS))
    conn.execute("INSERT INTO saved_filters (name, filters, created_at) VALUES ('Kids filter', ?, ?)",
                 (json.dumps({"category": source}), TS))
    conn.commit()
    return src, dest_id


def refs(conn):
    return {
        "classifications": rows(conn, "SELECT category_id FROM classifications"),
        "rules": rows(conn, "SELECT category_id FROM rules"),
        "plaid_category_map": rows(conn, "SELECT category_id FROM plaid_category_map WHERE pfc_code = 'USER_KIDS'"),
        "ai_reviews": rows(conn, "SELECT category_id FROM ai_reviews"),
        "transaction_splits": rows(conn, "SELECT category_id FROM transaction_splits"),
        "category_rollover": rows(conn, "SELECT category_id FROM category_rollover"),
        "rule_deletions": rows(conn, "SELECT category_id FROM rule_deletions"),
        "pending_actions": [json.loads(r[0]).get("category") for r in rows(conn, "SELECT arguments FROM pending_actions")],
    }


def test_delete_moves_every_one_of_the_eight_stores_then_removes_the_source(conn):
    src, dest_id = seed(conn)
    out = catops.delete(conn, "Kids", "Children")
    assert out["name"] == "Children" and out["deleted"] == "Kids"
    assert out["moved"] == {"classifications": 1, "rules": 1, "plaid_category_map": 1, "ai_reviews": 1,
                            "transaction_splits": 1, "category_rollover": 1, "rule_deletions": 1,
                            "pending_actions": 1}
    assert refs(conn) == {
        "classifications": [(dest_id,)],
        "rules": [(dest_id,)],
        "plaid_category_map": [(dest_id,)],
        "ai_reviews": [(dest_id,)],
        "transaction_splits": [(dest_id,)],
        "category_rollover": [(dest_id,)],
        "rule_deletions": [(dest_id,)],
        "pending_actions": ["Children"],
    }
    assert rows(conn, "SELECT COUNT(*) FROM categories WHERE name = 'Kids'") == [(0,)]
    assert rows(conn, "SELECT COUNT(*) FROM categories WHERE name = 'Children'") == [(1,)]
    # the month's spend follows the transaction: still money, under the destination now
    month = insights.month_of(__import__("datetime").date(2026, 8, 15))
    spend = insights.spend_by(conn, "v.month = ? AND v.category = 'Children'", (month,))
    assert spend[None][0] == 10.0


def test_a_destination_that_already_rolls_over_keeps_its_own_row_and_the_dropped_one_is_not_counted_as_moved(conn):
    src, dest_id = seed(conn)
    conn.execute("INSERT INTO category_rollover (category_id, enabled_at) VALUES (?, '2026-07-01T00:00:00Z')", (dest_id,))
    conn.commit()
    _, preview = catops.delete_preview(conn, "Kids", "Children")
    assert preview["category_rollover"] == 0
    out = catops.delete(conn, "Kids", "Children")
    assert out["moved"]["category_rollover"] == 0
    assert rows(conn, "SELECT category_id FROM category_rollover") == [(dest_id,)]


def chart(conn):
    got = rows(conn, "SELECT value FROM settings WHERE key = 'chart_excluded_categories'")
    return json.loads(got[0][0]) if got else []


def saved(conn, name):
    return json.loads(rows(conn, "SELECT filters FROM saved_filters WHERE name = ?", name)[0][0])


def test_hide_then_delete_leaves_the_chart_list_saveable_and_does_not_hide_the_destination(conn):
    seed(conn)
    catops.set_hidden(conn, "Kids", True)
    assert chart(conn) == ["Kids"]
    catops.delete(conn, "Kids", "Children")
    assert chart(conn) == []
    # what wherewent.js posts on its next toggle: the stored list plus one more category
    out = api.save_settings(conn, {"chart_excluded_categories": api.chart_excluded(conn) + ["Coffee"]})
    assert out["chart_excluded_categories"] == ["Coffee"]


def test_delete_keeps_a_destination_already_on_the_chart_list_once(conn):
    seed(conn)
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('chart_excluded_categories', ?, ?)",
                 (json.dumps(["Coffee", "kids", "Children"]), TS))
    conn.commit()
    catops.delete(conn, "Kids", "Children")
    assert chart(conn) == ["Coffee", "Children"]


def test_a_saved_filter_naming_the_deleted_category_names_the_destination(conn):
    seed(conn)
    conn.execute("INSERT INTO saved_filters (name, filters, created_at) VALUES ('both', ?, ?), ('other', ?, ?)",
                 (json.dumps({"category": ["KIDS", "Coffee", "Children"], "q": "mart"}), TS,
                  json.dumps({"category": "Coffee"}), TS))
    conn.commit()
    catops.delete(conn, "Kids", "Children")
    assert saved(conn, "Kids filter") == {"category": "Children"}
    assert saved(conn, "both") == {"category": ["Children", "Coffee"], "q": "mart"}   # once, order kept
    assert saved(conn, "other") == {"category": "Coffee"}


def named_stores(conn):
    return (rows(conn, "SELECT name, filters FROM saved_filters ORDER BY id"),
            rows(conn, "SELECT key, value FROM settings ORDER BY key"))


@pytest.mark.parametrize("last", ["_move_pending_actions", "_chart_forget"])
def test_failure_at_the_last_store_rolls_every_store_back(conn, monkeypatch, last):
    src, dest_id = seed(conn)
    catops.set_hidden(conn, "Kids", True)
    before = refs(conn)
    before_named = named_stores(conn)
    before_categories = rows(conn, "SELECT name FROM categories ORDER BY id")

    def boom(*a, **k):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(catops, last, boom)
    with pytest.raises(RuntimeError, match="injected failure"):
        catops.delete(conn, "Kids", "Children")
    assert refs(conn) == before
    assert named_stores(conn) == before_named
    assert chart(conn) == ["Kids"] and saved(conn, "Kids filter") == {"category": "Kids"}
    assert rows(conn, "SELECT name FROM categories ORDER BY id") == before_categories


@pytest.mark.parametrize("status", ["applied", "rejected", "failed"])
def test_a_decided_pending_action_keeps_its_arguments_and_is_not_counted(conn, status):
    seed(conn)
    args = {"transaction_id": "t9", "category": "Kids"}
    conn.execute("INSERT INTO pending_actions (tool, arguments, summary, client, status, created_at) VALUES "
                 "('finance_set_category', ?, 'File it under Kids', 'test', ?, ?)", (json.dumps(args), status, TS))
    conn.commit()
    _, preview = catops.delete_preview(conn, "Kids", "Children")
    assert preview["pending_actions"] == 1          # only seed()'s pending one
    out = catops.delete(conn, "Kids", "Children")
    assert out["moved"]["pending_actions"] == 1
    got = rows(conn, "SELECT status, arguments FROM pending_actions ORDER BY id")
    assert [(st, json.loads(a)) for st, a in got] == [
        ("pending", {"transaction_id": "t1", "category": "Children"}), (status, args)]


@pytest.mark.parametrize("name,move_to,word", [
    ("Kids", None, "Choose where"),
    ("Kids", "", "Choose where"),
    ("Kids", "Nope", "isn't one"),
    ("kids", "KIDS", "different category"),
    ("Kids", "Income", "not a spending one"),
    ("Kids", "Groceries", "hidden"),
])
def test_a_missing_unknown_self_income_or_hidden_destination_is_refused_and_nothing_moves(conn, name, move_to, word):
    src, dest_id = seed(conn)
    if move_to == "Groceries":
        conn.execute("UPDATE categories SET is_hidden = 1 WHERE name = 'Groceries'")
        conn.commit()
    before = refs(conn)
    with pytest.raises(catops.CategoryError) as e:
        catops.delete(conn, name, move_to)
    assert word in str(e.value)
    assert e.value.field == "move_to"
    assert refs(conn) == before
    assert rows(conn, "SELECT COUNT(*) FROM categories WHERE name = 'Kids'") == [(1,)]


def test_only_user_categories_can_be_deleted(conn):
    with pytest.raises(catops.CategoryError) as e:
        catops.delete(conn, "Coffee", "Groceries")
    assert "built-in" in str(e.value)
    assert rows(conn, "SELECT COUNT(*) FROM categories WHERE name = 'Coffee'") == [(1,)]


def test_delete_preview_counts_without_writing(conn):
    src, dest_id = seed(conn)
    before = refs(conn)
    dest, moved = catops.delete_preview(conn, "Kids", "Children")
    assert dest["name"] == "Children"
    assert moved["classifications"] == 1 and moved["rules"] == 1 and moved["pending_actions"] == 1
    assert refs(conn) == before
    assert rows(conn, "SELECT COUNT(*) FROM categories WHERE name = 'Kids'") == [(1,)]
