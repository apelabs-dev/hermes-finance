"""v23 rename propagation (catops.rename): the two name-keyed stores (saved_filters.filters and the chart's hidden list)
and pending actions follow the new name in the same transaction, case-insensitively, order kept; a filter that names
another category is untouched."""

from __future__ import annotations

import json

import pytest

from hermes_finance import catops

TS = "2026-08-01T00:00:00Z"


def rows(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args)]


def seed(conn):
    catops.create(conn, "Kids")
    conn.execute("INSERT INTO saved_filters (name, filters, created_at) VALUES ('one', ?, ?), ('two', ?, ?), ('three', ?, ?)",
                 (json.dumps({"category": "kids", "q": "mart"}), TS,
                  json.dumps({"category": ["Kids", "Coffee"]}), TS,
                  json.dumps({"category": "Coffee"}), TS))
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('chart_excluded_categories', ?, ?)",
                 (json.dumps(["Kids", "Coffee"]), TS))
    conn.execute("INSERT INTO pending_actions (tool, arguments, summary, client, status, created_at) VALUES "
                 "('finance_set_category', ?, 'x', 'test', 'pending', ?)",
                 (json.dumps({"transaction_id": "t1", "category": "kids"}), TS))
    conn.commit()


def filters(conn, name):
    return json.loads(rows(conn, "SELECT filters FROM saved_filters WHERE name = ?", name)[0][0])


def test_rename_updates_saved_filters_case_insensitively_and_keeps_order(conn):
    seed(conn)
    catops.rename(conn, "Kids", "Children")
    assert filters(conn, "one") == {"category": "Children", "q": "mart"}
    assert filters(conn, "two") == {"category": ["Children", "Coffee"]}
    assert filters(conn, "three") == {"category": "Coffee"}   # untouched
    assert rows(conn, "SELECT name FROM categories WHERE name = 'Children'") == [("Children",)]


def test_rename_updates_the_chart_hidden_list_in_order_once(conn):
    seed(conn)
    conn.execute("UPDATE settings SET value = ? WHERE key = 'chart_excluded_categories'",
                 (json.dumps(["Kids", "Coffee", "kids"]),))
    conn.commit()
    catops.rename(conn, "Kids", "Children")
    assert json.loads(rows(conn, "SELECT value FROM settings WHERE key = 'chart_excluded_categories'")[0][0]) == \
        ["Children", "Coffee"]


def test_rename_rewrites_pending_actions_that_name_the_category(conn):
    seed(conn)
    catops.rename(conn, "Kids", "Children")
    args = json.loads(rows(conn, "SELECT arguments FROM pending_actions")[0][0])
    assert args == {"transaction_id": "t1", "category": "Children"}


def test_a_case_only_rename_keeps_every_reference(conn):
    seed(conn)
    out = catops.rename(conn, "Kids", "KIDS")
    assert out["name"] == "KIDS"
    assert filters(conn, "one")["category"] == "KIDS"
    assert json.loads(rows(conn, "SELECT value FROM settings WHERE key = 'chart_excluded_categories'")[0][0]) == \
        ["KIDS", "Coffee"]


def test_rename_refuses_a_case_insensitive_clash_and_writes_nothing(conn):
    seed(conn)
    conn.execute("INSERT INTO saved_filters (name, filters, created_at) VALUES ('keep', ?, ?)",
                 (json.dumps({"category": "Kids"}), TS))
    conn.commit()
    with pytest.raises(catops.CategoryError) as e:
        catops.rename(conn, "Kids", "coffee")
    assert e.value.field == "name"
    assert rows(conn, "SELECT name FROM categories WHERE name = 'Kids'") == [("Kids",)]
    assert filters(conn, "keep") == {"category": "Kids"}


def test_rename_refuses_built_ins_and_unknown_names(conn):
    with pytest.raises(catops.CategoryError) as e:
        catops.rename(conn, "Coffee", "Espresso")
    assert "built-in" in str(e.value)
    with pytest.raises(catops.CategoryError):
        catops.rename(conn, "Nope", "Something")


@pytest.mark.parametrize("status", ["applied", "rejected"])
def test_rename_leaves_a_decided_pending_action_as_it_was(conn, status):
    seed(conn)
    args = {"transaction_id": "t2", "category": "Kids"}
    conn.execute("INSERT INTO pending_actions (tool, arguments, summary, client, status, created_at) VALUES "
                 "('finance_set_category', ?, 'File it under Kids', 'test', ?, ?)", (json.dumps(args), status, TS))
    conn.commit()
    catops.rename(conn, "Kids", "Children")
    got = [(st, json.loads(a)) for st, a in rows(conn, "SELECT status, arguments FROM pending_actions ORDER BY id")]
    assert got == [("pending", {"transaction_id": "t1", "category": "Children"}), (status, args)]


def test_rename_and_create_compare_names_with_casefold_not_ascii_nocase(conn):
    catops.create(conn, "École")
    with pytest.raises(catops.CategoryError) as e:
        catops.create(conn, "école")
    assert e.value.field == "name"
    catops.create(conn, "Straße")
    with pytest.raises(catops.CategoryError):
        catops.create(conn, "STRASSE")
    catops.create(conn, "Kids")
    with pytest.raises(catops.CategoryError):
        catops.rename(conn, "Kids", "ÉCOLE")
    assert rows(conn, "SELECT COUNT(*) FROM categories WHERE name IN ('école', 'STRASSE', 'ÉCOLE')") == [(0,)]
    assert catops.rename(conn, "École", "ÉCOLE")["name"] == "ÉCOLE"      # its own name in another case is fine
