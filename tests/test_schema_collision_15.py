"""The two v14s: the chat lane and the rules lane each stamped 14 with different DDL.

Chat's 14 is `pending_actions.origin` / `held_by`; the rules lane's 14 is `rule_deletions`, its trigger and
`classifications.detached_by`. Integration keeps chat's 14 and makes TASK-24's step 15, so a database stamped 14 by
EITHER branch has to converge on 15 with both sets of objects and every row kept — the case a single shared version
number silently skipped: `connect()` only calls `init_schema` when `user_version < SCHEMA_VERSION`, so a database
already at 14 would never have seen the other branch's DDL.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_finance import config, db

FIXTURES = Path(__file__).parent / "fixtures"
# what each branch's 14 added, exactly as db.ADDED_COLUMNS / db.TASK24_SCHEMA declare it
CHAT_V14 = (("pending_actions", "origin", "TEXT NOT NULL DEFAULT 'mcp'"),
            ("pending_actions", "held_by", "TEXT"))
RULES_V14 = (("classifications", "detached_by", "INTEGER REFERENCES rule_deletions(id)"),)
TS = "2026-08-01T00:00:00Z"


def _shape(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(type, name) for every table, view, index and trigger, so 'exactly a new one's shape' is comparable."""
    return sorted((r[0], r[1]) for r in conn.execute("SELECT type, name FROM sqlite_master "
                                                     "WHERE name NOT LIKE 'sqlite_%'"))


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _v14(path: Path, flavor: str) -> sqlite3.Connection:
    """A database as one branch left it: pinned v13 fixture, that branch's own DDL, rows, then `user_version = 14`."""
    config.ensure_data_dir(path.parent)
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "schema_v13.sql").read_text())
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    for table, column, decl in (CHAT_V14 if flavor == "chat" else RULES_V14):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    if flavor == "rules":                       # TASK-24's table and trigger, the rules lane's own v14
        conn.executescript(db.TASK24_SCHEMA)
    conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                 "VALUES ('item_a', 'ins_1', 'Chase', 'access-production-x', 'active', ?)", (TS,))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency,"
                 " updated_at) VALUES ('acc_chk', 'item_a', 'Checking', '1234', 'depository', 'checking', 1000, 'USD',"
                 " ?)", (TS,))
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, pfc_primary, "
                 "pfc_detailed, first_seen_at, updated_at) VALUES ('t1', 'acc_chk', 'item_a', '2026-08-02', 'X', 42.5, "
                 "'FOOD_AND_DRINK_GROCERIES', 'FOOD_AND_DRINK_GROCERIES', ?, ?)", (TS, TS))
    conn.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                 "SELECT 't1', id, 'user', ? FROM categories WHERE name = 'Groceries'", (TS,))
    conn.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at)"
                 " SELECT 'name', 'exact', 'X', id, 5, 'user', ? FROM categories WHERE name = 'Groceries'", (TS,))
    conn.execute("INSERT INTO pending_actions (tool, arguments, summary, status, created_at) "
                 "VALUES ('finance_set_cap', '{}', 's', 'pending', ?)", (TS,))
    with conn:
        conn.execute("PRAGMA user_version = 14")
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
    return conn


@pytest.mark.parametrize("flavor", ["chat", "rules"])
def test_a_v14_database_from_either_branch_converges_on_both_lanes_ddl(data_dir, flavor):
    """A 14 left by either branch opens once at 15 with both lanes' objects, every row kept, and a new one's shape."""
    path = config.db_path()
    raw = _v14(path, flavor)
    had_rule_deletions = "rule_deletions" in {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert had_rule_deletions is (flavor == "rules")
    assert ("detached_by" in _columns(raw, "classifications")) is (flavor == "rules")
    assert ({"origin", "held_by"} <= _columns(raw, "pending_actions")) is (flavor == "chat")
    raw.close()

    conn = db.connect(path)                      # 14 < 15, so the cumulative step runs (and v16 after it)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert "rule_deletions" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "detached_by" in _columns(conn, "classifications")
    assert {"origin", "held_by"} <= _columns(conn, "pending_actions")
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")} >= {
        "trg_classifications_detached_changed", "trg_classifications_protect_user"}
    # every row it had is still there, with its values
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, amount FROM transactions ORDER BY transaction_id")] \
        == [("t1", 42.5)]
    assert [(r[0], r[2]) for r in conn.execute("SELECT transaction_id, category_id, source FROM classifications")] \
        == [("t1", "user")]
    assert [tuple(r) for r in conn.execute("SELECT tool, status, origin, held_by FROM pending_actions")] \
        == [("finance_set_cap", "pending", "mcp", None)]
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 1
    # the view reads the old rows, and it is the one that joins rule_deletions
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, category FROM v_transactions WHERE item_id = 'item_a'")] \
        == [("t1", "Groceries")]
    assert "rule_deletions" in (conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'v_transactions'")
                               .fetchone()[0])
    # exactly a fresh database's shape, and connecting again changes nothing
    fresh = db.connect(path.parent / "fresh.db")
    assert _shape(conn) == _shape(fresh)
    fresh.close()
    before = _shape(conn)
    conn.close()
    again = db.connect(path)
    assert _shape(again) == before and again.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    again.close()
