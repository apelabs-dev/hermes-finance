"""v23 (categories, 2026-11-15): a v21 database (tests/fixtures/schema_v21_categories.sql) upgrades through the app's
own startup path with every row kept. The 36 seeded categories become built-in once, by name; a hand-made one survives
and stays user-made; is_hidden starts 0 everywhere and is never touched by the migration; running it again changes
nothing; a read-only connection never upgrades a database; and a v15-era database comes through the whole chain to v23.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_finance import config, db, tooldefs

FIXTURES = Path(__file__).parent / "fixtures"
TS = "2026-08-01T00:00:00Z"


def rows(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args)]


def counts(conn) -> dict[str, int]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def v21_db(path: Path, fixture: str = "schema_v21_categories.sql") -> sqlite3.Connection:
    """The fixture, plus one row in every table v23 or its guard reads: a bank, an account, a transaction, a
    classification, a rule, a map row, an AI review, a split line, a rollover, a rule-deletion receipt, a pending action,
    a saved filter, a setting, a goal and a hand-made category ("Kids")."""
    config.ensure_data_dir(path.parent)
    raw = sqlite3.connect(path)
    raw.executescript((FIXTURES / fixture).read_text())
    cid = lambda name: raw.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]   # noqa: E731
    raw.execute("INSERT INTO categories (name, is_income, is_transfer) VALUES ('Kids', 0, 0)")
    kids = raw.execute("SELECT id FROM categories WHERE name = 'Kids'").fetchone()[0]
    if raw.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'category_groups'").fetchone():
        raw.execute("UPDATE categories SET group_id = (SELECT id FROM category_groups WHERE name = 'Misc'), sort = ? "
                    "WHERE id = ?", (1000 + kids, kids))
    raw.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                "VALUES ('item_a', 'ins_1', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    raw.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
                "updated_at) VALUES ('acc_chk', 'item_a', 'Checking', '1234', 'depository', 'checking', 1000, 'USD', ?)",
                (TS,))
    raw.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, first_seen_at, "
                "updated_at) VALUES ('t1', 'acc_chk', 'item_a', '2026-08-02', 'MART', 10, ?, ?)", (TS, TS))
    raw.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
                "VALUES ('merchant_name', 'exact', 'Mart', ?, 200, 'user', ?)", (kids, TS))
    rule_id = raw.execute("SELECT id FROM rules").fetchone()[0]
    raw.execute("INSERT INTO classifications (transaction_id, category_id, source, rule_id, updated_at) "
                "VALUES ('t1', ?, 'rule', ?, ?)", (kids, rule_id, TS))
    raw.execute("INSERT INTO plaid_category_map (pfc_code, category_id) VALUES ('USER_KIDS', ?)", (kids,))
    raw.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, category_id) "
                "VALUES ('t1', 'run_1', ?, 'confirmed', ?)", (TS, cid("Groceries")))
    raw.execute("INSERT INTO transaction_splits (transaction_id, line, category_id, amount, created_at) "
                "VALUES ('t1', 1, ?, 10, ?)", (kids, TS))
    raw.execute("INSERT INTO category_rollover (category_id, enabled_at) VALUES (?, ?)", (kids, TS))
    raw.execute("INSERT INTO rule_deletions (receipt_hash, rule_id, match_field, match_type, pattern, category_id, "
                "priority, created_from, created_at, filed, deleted_at) VALUES "
                "('r1', 1, 'merchant_name', 'exact', 'Mart', ?, 200, 'user', ?, 1, ?)", (kids, TS, TS))
    raw.execute("INSERT INTO pending_actions (tool, arguments, summary, client, status, created_at) VALUES "
                "('finance_set_category', ?, 'File it under Kids', 'test', 'pending', ?)",
                ('{"transaction_id": "t1", "category": "Kids"}', TS))
    raw.execute("INSERT INTO saved_filters (name, filters, created_at) VALUES ('Kids', ?, ?)",
                ('{"category": "Kids"}', TS))
    raw.execute("INSERT INTO settings (key, value, updated_at) VALUES ('chart_excluded_categories', '[]', ?)", (TS,))
    raw.execute("INSERT INTO goals (kind, name, target, created_at, updated_at) VALUES ('save', 'Rainy day', 500, ?, ?)",
                (TS, TS))
    raw.commit()
    return raw


def test_v21_to_v23_keeps_every_row_and_marks_the_36_built_ins(data_dir):
    path = config.db_path()
    raw = v21_db(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 21
    before_counts = counts(raw)
    before_categories = rows(raw, "SELECT id, name, is_income, is_transfer, group_id, icon, sort FROM categories ORDER BY id")
    before_users = rows(raw, "SELECT transaction_id, category_id, source, rule_id FROM classifications ORDER BY rowid")
    before_actions = rows(raw, "SELECT tool, arguments, status FROM pending_actions ORDER BY rowid")
    raw.close()

    conn = db.connect(path)   # the app's own startup path
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert counts(conn) == before_counts
    assert rows(conn, "SELECT id, name, is_income, is_transfer, group_id, icon, sort FROM categories ORDER BY id") == \
        before_categories
    assert rows(conn, "SELECT transaction_id, category_id, source, rule_id FROM classifications ORDER BY rowid") == before_users
    assert rows(conn, "SELECT tool, arguments, status FROM pending_actions ORDER BY rowid") == before_actions
    assert rows(conn, "SELECT is_builtin, is_hidden FROM categories WHERE name = 'Kids'") == [(0, 0)]
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE is_builtin = 1").fetchone()[0] == 36
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE is_builtin = 0").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE is_hidden <> 0").fetchone()[0] == 0
    # every seeded name is built-in, exactly once
    builtins = {r[0] for r in conn.execute("SELECT name FROM categories WHERE is_builtin = 1")}
    assert builtins == {c[0] for c in db.DEFAULT_CATEGORIES}
    assert len(db.DEFAULT_CATEGORIES) == len(db.BUILTIN_CATEGORY_NAMES) == 36
    conn.close()


def test_v23_schema_is_idempotent(data_dir):
    path = config.db_path()
    v21_db(path).close()
    conn = db.connect(path)
    snapshot = rows(conn, "SELECT id, name, is_builtin, is_hidden FROM categories ORDER BY id")
    db._v23_schema(conn)
    db._v23_schema(conn)
    assert rows(conn, "SELECT id, name, is_builtin, is_hidden FROM categories ORDER BY id") == snapshot
    conn.close()


def test_read_only_connection_never_upgrades_a_v21_database(data_dir):
    path = config.db_path()
    v21_db(path).close()
    ro = db.connect_readonly(path)
    try:
        assert ro.execute("PRAGMA user_version").fetchone()[0] == 21
        assert "is_builtin" not in {r[1] for r in ro.execute("PRAGMA table_info(categories)")}
    finally:
        ro.close()
    try:
        tooldefs.read_connection(path)
    except tooldefs.ToolError as e:
        assert "schema v21" in str(e) and f"v{db.SCHEMA_VERSION}" in str(e)
    else:
        raise AssertionError("a read-only tool connection must refuse an old database, never upgrade it")
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 21


def test_a_v15_era_database_upgrades_through_the_whole_chain_to_v23(data_dir):
    path = config.db_path()
    v21_db(path, "schema_v15.sql").close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE is_builtin = 1").fetchone()[0] == 36
    assert rows(conn, "SELECT is_builtin FROM categories WHERE name = 'Kids'") == [(0,)]
    assert {"is_builtin", "is_hidden"} <= {r[1] for r in conn.execute("PRAGMA table_info(categories)")}
    conn.close()


def test_the_categories_v21_fixture_has_its_own_name():
    """This branch's v21 fixture is schema_v21_categories.sql: a bare schema_v21.sql would collide with another branch's
    v21 fixture at merge (an add/add conflict)."""
    assert (FIXTURES / "schema_v21_categories.sql").is_file()
    assert not (FIXTURES / "schema_v21.sql").exists()
