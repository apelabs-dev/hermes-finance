"""Release 0.4.0 migration proof (now schema v25: v24 category colour plus the PDF import step). A clean 0.3.0 database (schema v20) and the two v21 shapes the other
branches made both come through the app's own startup path (db.connect) to the current schema with every row of every
table kept, exactly 36 built-in categories, v22's currency fill touching no amount, and one schema at the end: the same
tables, columns, indexes, views and triggers as a database created new. Connecting again changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_finance import db

FIXTURES = Path(__file__).parent / "fixtures"
TS = "2026-08-01T00:00:00Z"
START = {"v20": "schema_v20.sql", "v21_currency": "schema_v21_currency.sql",
         "v21_categories": "schema_v21_categories.sql"}
# Columns the migration is meant to fill (v22): compared apart, never as "unchanged".
FILLED = {("transactions", "iso_currency"), ("balance_snapshots", "iso_currency")}


def counts(conn) -> dict[str, int]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def columns(conn, table) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def snapshot(conn, only: dict[str, list[str]] | None = None, skip=FILLED) -> dict[str, list[tuple]]:
    """Every row of every table (or of `only`'s columns), sorted so a rebuilt table's new rowids don't matter."""
    out = {}
    for table in (only or counts(conn)):
        cols = [c for c in (only[table] if only else columns(conn, table)) if (table, c) not in skip]
        if cols:
            out[table] = sorted((tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table}")), key=repr)
    return out


def schema_of(conn) -> dict:
    cols = sorted((t, c[1], c[2]) for t in counts(conn) for c in conn.execute(f"PRAGMA table_info({t})"))
    objects = {kind: sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)))
        for kind in ("index", "view", "trigger")}
    return {"columns": cols, **objects}


def old_db(path: Path, start: str) -> sqlite3.Connection:
    """A fixture plus one real row in every table the migration reads or could damage: a hand-made category, a bank,
    a Plaid USD and GBP account, a manual account, transactions with and without iso_currency (plain, GBP, imported),
    a classification, rule, map row, AI review, split, rollover, receipt, pending action, filter, setting, goal,
    balance snapshots and a manual balance. Ids are chosen not to collide with the fixtures' own rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path)
    raw.executescript((FIXTURES / START[start]).read_text())
    x = raw.execute
    x("INSERT INTO categories (name, is_income, is_transfer) VALUES ('Kids', 0, 0)")
    kids = x("SELECT id FROM categories WHERE name = 'Kids'").fetchone()[0]
    x("UPDATE categories SET group_id = (SELECT id FROM category_groups WHERE name = 'Misc'), sort = ? WHERE id = ?",
      (1000 + kids, kids))
    groceries = x("SELECT id FROM categories WHERE name = 'Groceries'").fetchone()[0]
    x("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
      "VALUES ('item_seed', 'ins_s', 'Seed Bank', 'access-fake-x', 'active', ?)", (TS,))
    for acc, name, cur in (("acc_seed_usd", "Seed checking", "USD"), ("acc_seed_gbp", "Seed pounds", "GBP")):
        x("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
          "updated_at) VALUES (?, 'item_seed', ?, '1234', 'depository', 'checking', 1234.56, ?, ?)", (acc, name, cur, TS))
    x("INSERT INTO manual_accounts (kind, name, institution, subtype, balance, balance_as_of, created_at, updated_at) "
      "VALUES ('bank', 'Cash jar', 'Home', 'checking', 321.09, '2026-08-01', ?, ?)", (TS, TS))
    man = x("SELECT id FROM manual_accounts WHERE name = 'Cash jar'").fetchone()[0]
    x("INSERT INTO manual_balance_history (manual_account_id, date, balance) VALUES (?, '2026-08-01', 321.09)", (man,))
    for tid, acc, item, amount, iso, source in (
            ("seed_usd", "acc_seed_usd", "item_seed", 10.01, "USD", "plaid"),
            ("seed_nocode", "acc_seed_usd", "item_seed", 12.34, None, "plaid"),
            ("seed_gbp_nocode", "acc_seed_gbp", "item_seed", 56.78, None, "plaid"),
            ("seed_import_nocode", f"manual:{man}", "import", 9.99, None, "import")):
        x("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, iso_currency, source, "
          "first_seen_at, updated_at) VALUES (?, ?, ?, '2026-08-02', 'MART', ?, ?, ?, ?, ?)",
          (tid, acc, item, amount, iso, source, TS, TS))
    x("INSERT INTO balance_snapshots (account_id, date, current, available, iso_currency) VALUES "
      "('acc_seed_gbp', '2026-08-01', 1234.56, 1200, NULL), ('acc_seed_usd', '2026-08-01', 1234.56, 1200, 'USD')")
    x("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
      "VALUES ('merchant_name', 'exact', 'Mart', ?, 200, 'user', ?)", (kids, TS))
    rule = x("SELECT id FROM rules WHERE pattern = 'Mart'").fetchone()[0]
    x("INSERT INTO classifications (transaction_id, category_id, source, rule_id, updated_at) "
      "VALUES ('seed_usd', ?, 'rule', ?, ?)", (kids, rule, TS))
    x("INSERT INTO plaid_category_map (pfc_code, category_id) VALUES ('USER_KIDS', ?)", (kids,))
    x("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, category_id) "
      "VALUES ('seed_usd', 'run_seed', ?, 'confirmed', ?)", (TS, groceries))
    x("INSERT INTO transaction_splits (transaction_id, line, category_id, amount, created_at) "
      "VALUES ('seed_usd', 1, ?, 10.01, ?)", (kids, TS))
    x("INSERT INTO category_rollover (category_id, enabled_at) VALUES (?, ?)", (kids, TS))
    x("INSERT INTO rule_deletions (receipt_hash, rule_id, match_field, match_type, pattern, category_id, priority, "
      "created_from, created_at, filed, deleted_at) VALUES ('r_seed', 99, 'merchant_name', 'exact', 'Mart', ?, 200, "
      "'user', ?, 1, ?)", (kids, TS, TS))
    x("INSERT INTO pending_actions (tool, arguments, summary, client, status, created_at) VALUES "
      "('finance_set_category', ?, 'File it under Kids', 'test', 'pending', ?)",
      ('{"transaction_id": "seed_usd", "category": "Kids"}', TS))
    x("INSERT INTO saved_filters (name, filters, created_at) VALUES ('Kids', ?, ?)", ('{"category": "Kids"}', TS))
    x("INSERT INTO settings (key, value, updated_at) VALUES ('seed_setting', '1', ?)", (TS,))
    x("INSERT INTO goals (kind, name, target, created_at, updated_at, icon, colour) "   # icon/colour NULL = v17's "unfilled"
      "VALUES ('save', 'Rainy day', 500.5, ?, ?, 'piggy-bank', '#4fd1a5')", (TS, TS))
    raw.commit()
    return raw


def migrate(tmp_path: Path, start: str):
    """-> (the old database's row snapshot and counts and column lists, a connection the app opened on it)."""
    path = tmp_path / start / "finance.db"
    raw = old_db(path, start)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == (20 if start == "v20" else 21)
    old = {"counts": counts(raw), "cols": {t: columns(raw, t) for t in counts(raw)}}
    old["rows"] = snapshot(raw)
    old["iso"] = {(t, r[0]): r[1] for t, sql in (("transactions", "SELECT transaction_id, iso_currency FROM transactions"),
                                                 ("balance_snapshots", "SELECT account_id || date, iso_currency FROM balance_snapshots"))
                  for r in raw.execute(sql) if (r[1] or "").strip()}
    raw.close()
    return old, db.connect(path)   # the app's own startup path


@pytest.fixture(params=list(START))
def migrated(request, data_dir, tmp_path):
    old, conn = migrate(tmp_path, request.param)
    yield request.param, old, conn
    conn.close()


def test_schema_version_is_25():
    assert db.SCHEMA_VERSION == 25, "this proof follows the current schema; update it with the next migration"


def test_every_start_keeps_every_row_and_ends_with_the_36_built_ins(migrated):
    start, old, conn = migrated
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 25, start
    after = counts(conn)
    for table, n in old["counts"].items():
        assert after.get(table) == n, f"{start}: table {table} had {n} rows, has {after.get(table)}"
    assert {"category_groups", "group_budgets", "rule_deletions"} <= set(after), f"{start}: tables missing after migrating"
    if start != "v20":
        assert "simplefin_requests" in after
    # every pre-existing column of every row is byte-identical (the v22 fill columns are checked below)
    assert snapshot(conn, old["cols"]) == old["rows"], f"{start}: a row changed in the migration"
    builtin = {r[0] for r in conn.execute("SELECT name FROM categories WHERE is_builtin = 1")}
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE is_builtin = 1").fetchone()[0] == 36, start
    assert builtin == {c[0] for c in db.DEFAULT_CATEGORIES}, start
    assert [tuple(r) for r in conn.execute("SELECT is_builtin, is_hidden FROM categories WHERE name = 'Kids'")] == [(0, 0)]
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE is_builtin = 0").fetchone()[0] == after["categories"] - 36


def test_v22_fills_the_currency_and_never_an_amount(migrated):
    start, old, conn = migrated
    iso = dict(conn.execute("SELECT transaction_id, iso_currency FROM transactions WHERE transaction_id LIKE 'seed_%'"))
    assert iso == {"seed_usd": "USD", "seed_nocode": "USD", "seed_gbp_nocode": "GBP", "seed_import_nocode": "USD"}, \
        f"{start}: {iso}"
    man = conn.execute("SELECT currency FROM manual_accounts WHERE name = 'Cash jar'").fetchone()[0]
    assert man == "USD", f"{start}: the manual account's currency is {man!r}"
    assert conn.execute("SELECT COUNT(*) FROM manual_accounts WHERE currency IS NULL OR TRIM(currency) = ''").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE iso_currency IS NULL OR TRIM(iso_currency) = ''"
                        ).fetchone()[0] == 0, start
    snaps = dict(conn.execute("SELECT account_id, iso_currency FROM balance_snapshots WHERE account_id LIKE 'acc_seed_%'"))
    assert snaps == {"acc_seed_usd": "USD", "acc_seed_gbp": "GBP"}, f"{start}: {snaps}"
    # a code that was already there is never replaced (the amounts are in the unchanged-rows snapshot above)
    now = {("transactions", k): v for k, v in conn.execute("SELECT transaction_id, iso_currency FROM transactions")}
    now.update({("balance_snapshots", k): v for k, v in conn.execute(
        "SELECT account_id || date, iso_currency FROM balance_snapshots")})
    assert old["iso"] and all(now[k] == v for k, v in old["iso"].items()), f"{start}: a non-empty code changed"
    assert conn.execute("SELECT SUM(amount) FROM transactions WHERE transaction_id LIKE 'seed_%'").fetchone()[0] == \
        pytest.approx(10.01 + 12.34 + 56.78 + 9.99, abs=1e-9)


def test_a_non_empty_currency_survives_the_fill(data_dir, tmp_path):
    old, conn = migrate(tmp_path, "v21_currency")
    try:
        iso = dict(conn.execute("SELECT transaction_id, iso_currency FROM transactions"))
        assert iso["t_jpy"] == "JPY" and iso["t_custom"] == "USDF" and iso["t_eur_null"] == "EUR", iso
        assert iso["t_usd_null"] == "USD" and iso["t_import"] == "USD", iso
        assert conn.execute("SELECT amount FROM transactions WHERE transaction_id = 't_jpy'").fetchone()[0] == 1200.0
    finally:
        conn.close()


def test_every_migrated_schema_equals_a_new_database(data_dir, tmp_path):
    fresh = db.connect(tmp_path / "fresh" / "finance.db")
    want = schema_of(fresh)
    fresh.close()
    assert len(want["columns"]) > 300 and len(want["index"]) > 10 and len(want["view"]) > 3, "a new database looks empty"
    for start in START:
        _old, conn = migrate(tmp_path, start)
        got = schema_of(conn)
        conn.close()
        for key in want:
            extra, missing = [x for x in got[key] if x not in want[key]], [x for x in want[key] if x not in got[key]]
            assert not extra and not missing, f"{start} vs new database, {key}: only migrated {extra}, only new {missing}"


def test_connecting_again_changes_nothing(migrated, tmp_path):
    start, old, conn = migrated
    path = tmp_path / start / "finance.db"
    first, first_counts = snapshot(conn, skip=set()), counts(conn)
    conn.close()
    again = db.connect(path)
    try:
        assert again.execute("PRAGMA user_version").fetchone()[0] == 25
        assert counts(again) == first_counts, f"{start}: row counts changed on the second connect"
        assert snapshot(again, skip=set()) == first, f"{start}: rows changed on the second connect"
        assert again.execute("SELECT COUNT(*) FROM categories WHERE is_builtin = 1").fetchone()[0] == 36
    finally:
        again.close()
