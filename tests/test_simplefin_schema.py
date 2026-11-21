"""v20 -> v21 (SimpleFIN): items.provider, accounts.conn_id and the transactions.source CHECK widened to
('plaid', 'import', 'simplefin'). SQLite can't alter a CHECK, so the transactions table is rebuilt with every row, its
five indexes, its triggers and the views kept. A v20 dump (tests/fixtures/schema_v20.sql) is filled, upgraded, and
everything is compared; the upgrade is idempotent and finished after an interrupted run.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_finance import config, db

FIXTURES = Path(__file__).parent / "fixtures"
V20 = (FIXTURES / "schema_v20.sql").read_text()
INDEXES = {"idx_transactions_date", "idx_transactions_item", "idx_transactions_account", "idx_transactions_merchant",
           "idx_transactions_fitid"}
COLUMNS = ["transaction_id", "account_id", "item_id", "date", "authorized_date", "name", "merchant_name", "amount",
           "iso_currency", "pending", "pending_transaction_id", "payment_channel", "pfc_primary", "pfc_detailed",
           "pfc_confidence", "logo_url", "website", "location_json", "counterparties_json", "raw_json", "removed",
           "first_seen_at", "updated_at", "source", "import_id", "fitid"]


def v20_db(path: Path) -> None:
    """A real v20 database with one item, two accounts, every source in use, a classification, a rule, a split and a
    note, so the rebuild has to keep rows other tables point at."""
    raw = sqlite3.connect(path)
    raw.executescript(V20)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 20
    ts = "2026-11-01T00:00:00Z"
    raw.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, products, "
                "created_at, last_sync_at) VALUES ('item_a', 'ins_1', 'Chase', 'access-production-v20', 'active', "
                "'[\"transactions\"]', ?, ?)", (ts, ts))
    for aid, name, typ, sub, bal in (("acc_chk", "Checking", "depository", "checking", 1200.0),
                                       ("acc_cc", "Card", "credit", "credit card", 450.0)):
        raw.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                    "available_balance, iso_currency, updated_at) VALUES (?, 'item_a', ?, '4410', ?, ?, ?, ?, 'USD', ?)",
                    (aid, name, typ, sub, bal, bal, ts))
    rows = (("t_plaid", "acc_chk", "Coffee", 12.5, "plaid", None, None),
            ("t_import", "acc_chk", "Imported row", 30.0, "import", 1, "FIT-1"),
            ("t_pending", "acc_cc", "Pending charge", 40.0, "plaid", None, None))
    for tid, aid, name, amount, source, import_id, fitid in rows:
        raw.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                    "pending, pfc_primary, raw_json, source, import_id, fitid, first_seen_at, updated_at) "
                    "VALUES (?, ?, 'item_a', '2026-11-02', ?, ?, ?, ?, 'FOOD_AND_DRINK', '{}', ?, ?, ?, ?, ?)",
                    (tid, aid, name, name.title(), amount, 1 if tid == "t_pending" else 0, source, import_id, fitid,
                     ts, ts))
    raw.execute("INSERT INTO imports (id, created_at, filename, kind, manual_account_id, rows_new, rows_skipped) "
                "VALUES (1, ?, 'bank.csv', 'csv', 1, 1, 0)", (ts,))
    raw.execute("INSERT INTO manual_accounts (id, kind, name, balance, balance_as_of, created_at, updated_at) "
                "VALUES (1, 'bank', 'Imported account', 30, '2026-11-01', ?, ?)", (ts, ts))
    raw.execute("DELETE FROM transactions WHERE transaction_id = 't_import'")  # the FK-less import row is rebuilt too
    raw.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, pending, raw_json, "
                "source, import_id, fitid, first_seen_at, updated_at) VALUES ('t_import', 'manual:1', 'import', "
                "'2026-11-02', 'Imported row', 30.0, 0, '{}', 'import', 1, 'FIT-1', ?, ?)", (ts, ts))
    raw.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                "SELECT 't_plaid', id, 'user', ? FROM categories WHERE name = 'Coffee'", (ts,))
    raw.execute("INSERT INTO rules (match_field, pattern, category_id, created_from, created_at) "
                "SELECT 'name', 'Coffee', id, 'user', ? FROM categories WHERE name = 'Coffee'", (ts,))
    raw.execute("INSERT INTO transaction_splits (transaction_id, line, category_id, amount, created_at) "
                "SELECT 't_plaid', 1, id, 12.5, ? FROM categories WHERE name = 'Coffee'", (ts,))
    raw.execute("INSERT INTO transaction_notes (transaction_id, note, updated_at) VALUES ('t_pending', 'a note', ?)",
                (ts,))
    raw.execute("CREATE VIEW v_custom AS SELECT transaction_id, source FROM transactions")
    raw.execute("CREATE TRIGGER trg_txn_keep BEFORE DELETE ON transactions BEGIN SELECT 1; END")
    raw.commit()
    raw.close()


def rows(conn, sql):
    return [tuple(r) for r in conn.execute(sql)]


V20_COLUMNS = {"items": [r for r in ["item_id", "institution_id", "institution_name", "access_token", "cursor", "status",
                                     "error_code", "error_message", "products", "txn_update_status", "created_at",
                                     "last_sync_at", "removed_at"]],
               "accounts": [r for r in ["account_id", "item_id", "name", "official_name", "mask", "type", "subtype",
                                        "current_balance", "available_balance", "credit_limit", "iso_currency",
                                        "updated_at"]]}


# v22 (multi-currency) adds manual_accounts.currency and fills a missing transactions.iso_currency from the account;
# test_currency_v22 proves that fill exactly, so the v20 comparison ignores those two columns.
V22_ADDED = {"transactions": {"iso_currency"}, "manual_accounts": {"currency"}}


def dump(conn):
    out = {}
    for t in ("items", "accounts", "transactions", "imports", "manual_accounts", "classifications", "rules",
              "transaction_splits", "transaction_notes", "balance_snapshots", "settings"):
        cols = V20_COLUMNS.get(t)
        names = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] if not cols else cols
        keep = [i for i, c in enumerate(names) if c not in V22_ADDED.get(t, set())]
        out[t] = [tuple(row[i] for i in keep)
                  for row in rows(conn, f"SELECT {', '.join(names)} FROM {t} ORDER BY rowid")]
    return out


def test_v20_upgrade_keeps_every_row_and_proves_the_new_constraints(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    v20_db(path)
    raw = sqlite3.connect(path)
    before = dump(raw)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 20
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert dump(conn) == before                         # every column, every row, byte for byte
    # items.provider: existing Items are Plaid's; only the two providers are allowed
    assert rows(conn, "SELECT item_id, provider FROM items") == [("item_a", "plaid")]
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("UPDATE items SET provider = 'junk' WHERE item_id = 'item_a'")
    with conn:
        conn.execute("INSERT INTO items (item_id, access_token, provider, created_at) "
                     "VALUES ('simplefin:CON-1', '', 'simplefin', 'now')")
    assert rows(conn, "SELECT provider FROM items WHERE item_id = 'simplefin:CON-1'") == [("simplefin",)]
    # accounts.conn_id: NULL for Plaid, a bridge id for SimpleFIN
    assert rows(conn, "SELECT DISTINCT conn_id FROM accounts") == [(None,)]
    with conn:
        conn.execute("UPDATE accounts SET conn_id = 'CON-1' WHERE account_id = 'acc_chk'")
    assert rows(conn, "SELECT conn_id FROM accounts WHERE account_id = 'acc_chk'") == [("CON-1",)]
    # the rebuilt transactions table has every column in order
    assert [r["name"] for r in conn.execute("PRAGMA table_info(transactions)")] == COLUMNS
    # the widened CHECK accepts simplefin and still rejects junk, for both source and the old import rows
    with conn:
        conn.execute("UPDATE transactions SET source = 'simplefin', import_id = NULL, fitid = NULL "
                     "WHERE transaction_id = 't_pending'")
    assert rows(conn, "SELECT source FROM transactions WHERE transaction_id = 't_pending'") == [("simplefin",)]
    for bad in ("UPDATE transactions SET source = 'plaidish' WHERE transaction_id = 't_plaid'",
                "INSERT INTO transactions (transaction_id, account_id, item_id, date, amount, source, first_seen_at, "
                "updated_at) VALUES ('x', 'acc_chk', 'item_a', '2026-11-03', 1, 'guess', 'now', 'now')"):
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(bad)
    # all five indexes are back: the unique fitid one still guards imports
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                                       "AND tbl_name = 'transactions'")} >= INDEXES
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, amount, source, fitid, "
                         "first_seen_at, updated_at) VALUES ('t_dup', 'manual:1', 'import', '2026-11-02', 1, "
                         "'import', 'FIT-1', 'now', 'now')")
    # the triggers and views by name are still there and still work
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_txn_keep'").fetchone()
    assert rows(conn, "SELECT transaction_id, source FROM v_custom ORDER BY transaction_id") == [
        ("t_import", "import"), ("t_pending", "simplefin"), ("t_plaid", "plaid")]
    assert rows(conn, "SELECT transaction_id, category, category_source FROM v_transactions "
                      "WHERE transaction_id = 't_plaid'") == [("t_plaid", "Coffee", "user")]
    assert rows(conn, "SELECT transaction_id FROM v_spend_lines WHERE transaction_id = 't_plaid'") == [("t_plaid",)]
    # and a fresh database defines the same three things
    fresh_path = path.parent / "fresh.db"
    fresh = db.connect(fresh_path)
    try:
        assert fresh.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert "provider" in {r["name"] for r in fresh.execute("PRAGMA table_info(items)")}
        assert "conn_id" in {r["name"] for r in fresh.execute("PRAGMA table_info(accounts)")}
        assert "simplefin" in fresh.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'").fetchone()[0]
        assert [r["name"] for r in fresh.execute("PRAGMA table_info(transactions)")] == COLUMNS
    finally:
        fresh.close()
    # idempotent: reconnecting changes nothing
    after = dump(conn)
    conn.close()
    again = db.connect(path)
    assert dump(again) == after
    assert again.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    again.close()


def test_a_finished_migration_is_a_noop_and_a_leftover_temp_table_is_dropped(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    v20_db(path)
    conn = db.connect(path)
    conn.close()
    # an interrupted rebuild left its temp table behind (the version was never stamped): the next run drops it and
    # does not copy from it
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 20")
    raw.execute("CREATE TABLE transactions_v21 (transaction_id TEXT)")
    raw.execute("INSERT INTO transactions_v21 VALUES ('junk')")
    raw.commit()
    raw.close()
    again = db.connect(path)
    assert again.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3
    assert not again.execute("SELECT 1 FROM sqlite_master WHERE name = 'transactions_v21'").fetchone()
    before = dump(again)
    db.init_schema(again)
    assert dump(again) == before
    again.close()
