"""TASK-19 (Batch C): the schema upgrade that adds its tables and the v_spend_lines view (db._task19_schema). A database
at the version before it keeps every row and gains the new tables; running it again changes nothing; the read-only
commands ask for an upgrade on an old database instead of guessing."""

from __future__ import annotations

import sqlite3

from hermes_finance import cli, config, db

NEW_TABLES = ("transaction_splits", "transaction_notes", "tags", "transaction_tags", "saved_filters", "category_rollover",
              "goals", "goal_entries", "alpaca_snapshots")


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


def make_previous(path) -> None:
    """A database as the version before TASK-19 left it: today's schema without TASK-19's tables and view, one row of
    data, and its user_version."""
    conn = db.connect(path)
    with conn:
        conn.execute("DROP VIEW v_spend_lines")
        for t in NEW_TABLES:
            conn.execute(f"DROP TABLE {t}")
        conn.execute("INSERT INTO items (item_id, institution_name, access_token, created_at) VALUES ('it', 'Chase', 'x', 'now')")
        conn.execute("INSERT INTO accounts (account_id, item_id, name, type, current_balance) VALUES ('a', 'it', 'Checking', 'depository', 50)")
        conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, amount, first_seen_at, updated_at) "
                     "VALUES ('t1', 'a', 'it', '2026-09-01', 12.5, 'now', 'now')")
        conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    conn.close()


def test_the_upgrade_adds_the_tables_and_keeps_every_row(data_dir):
    path = config.db_path()
    make_previous(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1
    assert not tables(raw) & set(NEW_TABLES)
    raw.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert set(NEW_TABLES) | {"v_spend_lines"} <= tables(conn)
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, amount FROM transactions")] == [("t1", 12.5)]
    assert [tuple(r)[:3] for r in conn.execute("SELECT transaction_id, split_line, spend FROM v_spend_lines")] == [("t1", None, 12.5)]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(alpaca_snapshots)")]
    assert cols[-2:] == ["positions_json", "counted"]
    before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    db.init_schema(conn)                                             # again: nothing changes
    assert conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before
    conn.close()


def test_the_read_commands_ask_for_the_upgrade_on_an_old_database(data_dir, capsys):
    make_previous(config.db_path())
    assert cli.main(["goals", "--json"]) == 1
    assert f"these commands need v{db.SCHEMA_VERSION}" in capsys.readouterr().err
