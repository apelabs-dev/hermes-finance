"""Schema upgrades on DBs with data (v1 -> v2 balance_snapshots, v2 -> v3 AI review tables, v3 -> v4 manual
accounts, loan payments and holding snapshots, v4 -> v5 the institutions and security_logos caches, ..., and TASK-20's
merged order: v9 -> v10 pending_actions -> v11 TASK-18's login / token / import tables -> v12 TASK-19's tables), and the
snapshots written after each sync."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from conftest import account, add_item, err, page, txn
from hermes_finance import ai, classify, config, db, insights, manual, rulebook
from hermes_finance.sync import sync_all

V1_SQL = (Path(__file__).parent / "fixtures" / "schema_v1.sql").read_text()
V2_SQL = (Path(__file__).parent / "fixtures" / "schema_v2.sql").read_text()
V3_SQL = (Path(__file__).parent / "fixtures" / "schema_v3.sql").read_text()
V4_SQL = (Path(__file__).parent / "fixtures" / "schema_v4.sql").read_text()
V1_TABLES = ["items", "accounts", "transactions", "categories", "plaid_category_map", "rules", "classifications",
             "holdings", "liabilities", "sync_log", "link_attempts"]
V2_TABLES = V1_TABLES + ["balance_snapshots"]
V3_TABLES = V2_TABLES + ["ai_runs", "ai_run_groups", "ai_reviews"]
V4_NEW = ["manual_accounts", "manual_balance_history", "loan_payments", "holding_snapshots"]
V4_TABLES = V3_TABLES + V4_NEW
V5_NEW = ["institutions", "security_logos"]


# v16 (polish P0) adds 23 categories and finer Plaid defaults, and moves the car payment's default to Car Payment
# (test_taxonomy_p0 checks those): every other row is as it was
V16_CATEGORIES = [name for name, _, _ in db.DEFAULT_CATEGORIES[13:]]
V16_PFC = (set(db.DEFAULT_PFC_MAP) - set(re.findall(r"INSERT INTO \"plaid_category_map\" VALUES\('([A-Z_]+)'",
                                                     (Path(__file__).parent / "fixtures" / "schema_v15.sql").read_text()))
           | {"LOAN_PAYMENTS_CAR_PAYMENT"})
V16_ROWS = {"categories": ("name", V16_CATEGORIES), "plaid_category_map": ("pfc_code", sorted(V16_PFC))}
# v19 (category names) renames three categories in place, by id: a database stamped before 19 is dumped as it will read
# once upgraded, so the upgraded one is compared against the new names themselves
V19_RENAMED = {"Transport": "Other Transport", "Shopping": "Other Shopping", "Health": "Other Health"}


def dump(conn, tables=V1_TABLES):
    """Every row of `tables`, in the columns they had before v7 (v7 adds columns to manual_accounts; an old row keeps
    every value it had)."""
    added = {(t, c) for t, c, _ in db.ADDED_COLUMNS + db.TASK18_COLUMNS}  # TASK-18: transactions.source ... too
    added |= {("goals", "icon"), ("goals", "colour")}  # v17 appearance does not alter original goal values
    out = {}
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})") if (t, r[1]) not in added]
        # v11 saves an upgraded install's Carfax / Redfin switches (on, as they were: test_settings_task18)
        where = " WHERE key NOT LIKE 'valuations.%'" if t == "settings" else ""
        if t in V16_ROWS:
            col, values = V16_ROWS[t]
            where = f" WHERE {col} NOT IN ({', '.join(repr(v) for v in values)})"
        rows = [tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {t}{where} ORDER BY rowid")]
        if t == "categories" and conn.execute("PRAGMA user_version").fetchone()[0] < 19:
            name = cols.index("name")
            rows = [r[:name] + (V19_RENAMED.get(r[name], r[name]),) + r[name + 1:] for r in rows]
        out[t] = rows
    return out


def make_v1_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V1_SQL)
    ts = "2026-09-01T00:00:00Z"
    conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at, "
                 "last_sync_at) VALUES ('item_a', 'ins_1', 'Chase', 'access-production-v1', 'active', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                 "available_balance, iso_currency, updated_at) VALUES ('acc_chk', 'item_a', 'Checking', '1234', "
                 "'depository', 'checking', 1000, 900, 'USD', ?)", (ts,))
    for tid, amount, pfc in (("t1", 12.5, "FOOD_AND_DRINK"), ("t2", -3000, "INCOME")):
        conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, pfc_primary, "
                     "raw_json, first_seen_at, updated_at) VALUES (?, 'acc_chk', 'item_a', '2026-09-02', 'X', ?, ?, "
                     "'{}', ?, ?)", (tid, amount, pfc, ts, ts))
    conn.execute("INSERT INTO rules (match_field, pattern, category_id, created_at) "
                 "SELECT 'name', 'X', id, ? FROM categories WHERE name = 'Shopping'", (ts,))
    conn.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                 "SELECT 't1', id, 'user', ? FROM categories WHERE name = 'Health'", (ts,))
    conn.execute("UPDATE categories SET monthly_cap = 250 WHERE name = 'Groceries'")
    conn.execute("INSERT INTO sync_log (item_id, started_at, finished_at, added) VALUES ('item_a', ?, ?, 2)", (ts, ts))
    conn.commit()
    conn.close()


def test_upgrade_from_v1_keeps_every_row_and_adds_snapshots(data_dir, client, fake):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    make_v1_db(path)
    raw = sqlite3.connect(path)
    before = dump(raw)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'balance_snapshots'").fetchone()
    raw.close()

    conn = db.connect(path)  # the live DB is upgraded exactly like this on the next start

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert dump(conn) == before
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(balance_snapshots)")]
    assert cols == ["account_id", "date", "current", "available", "iso_currency"]
    pk = [r["name"] for r in sorted(conn.execute("PRAGMA table_info(balance_snapshots)"), key=lambda r: r["pk"]) if r["pk"]]
    assert pk == ["account_id", "date"]
    # the views still work over the old rows, and the user classification still wins
    assert conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id='t1'").fetchone()[:] \
        == ("Other Health", "user")   # v19: the same category row, by id, under its new name
    assert conn.execute("SELECT month, spend FROM v_monthly_category_spend "
                        "WHERE category='Other Health'").fetchone()[:] \
        == ("2026-09", 12.5)
    # a sync after the upgrade writes one snapshot per account for today
    fake.on("/accounts/get", {"accounts": [account(current=1234.56, available=1200.0)]})
    fake.on("/transactions/sync", page(added=[txn("t3", 5.0)], next_cursor="c1"))
    (r,) = sync_all(conn, client)
    assert r.ok
    rows = [tuple(x) for x in conn.execute("SELECT * FROM balance_snapshots")]
    assert rows == [("acc_chk", date.today().isoformat(), 1234.56, 1200.0, "USD")]
    after = dump(conn)
    assert after["items"][0][0] == "item_a" and len(after["transactions"]) == 3
    assert after["rules"] == before["rules"] and after["categories"] == before["categories"]
    # upgrading is idempotent: reconnecting changes nothing
    conn.close()
    again = db.connect(path)
    assert dump(again, V1_TABLES + ["balance_snapshots"]) == {**after, "balance_snapshots": rows}
    again.close()


def make_v2_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V2_SQL)
    ts = "2026-09-20T00:00:00Z"
    conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at, "
                 "last_sync_at) VALUES ('item_a', 'ins_1', 'Chase', 'access-production-v2', 'active', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                 "available_balance, iso_currency, updated_at) VALUES ('acc_chk', 'item_a', 'Checking', '1234', "
                 "'depository', 'checking', 1000, 900, 'USD', ?)", (ts,))
    for tid, name, amount, pfc in (("t_user", "DENTIST", 80, "MEDICAL"), ("t_ai", "OPENAI", 20, "GENERAL_MERCHANDISE"),
                                   ("t_plaid", "WHOLE FOODS", 55, "FOOD_AND_DRINK")):
        conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                     "pfc_primary, raw_json, first_seen_at, updated_at) VALUES (?, 'acc_chk', 'item_a', '2026-09-15', "
                     "?, ?, ?, ?, '{}', ?, ?)", (tid, name, name.title(), amount, pfc, ts, ts))
    conn.execute("INSERT INTO rules (match_field, pattern, category_id, priority, created_from, created_at) "
                 "SELECT 'merchant_name', 'Dentist', id, 100, 'user', ? FROM categories WHERE name = 'Health'", (ts,))
    for tid, category, source in (("t_user", "Health", "user"), ("t_ai", "Subscriptions", "ai"),
                                  ("t_plaid", "Eating Out", "plaid")):
        conn.execute("INSERT INTO classifications (transaction_id, category_id, source, confidence, updated_at) "
                     "SELECT ?, id, ?, 0.9, ? FROM categories WHERE name = ?", (tid, source, ts, category))
    conn.execute("UPDATE categories SET monthly_cap = 250 WHERE name = 'Groceries'")
    conn.execute("INSERT INTO balance_snapshots VALUES ('acc_chk', '2026-09-20', 1000, 900, 'USD')")
    conn.execute("INSERT INTO sync_log (item_id, started_at, finished_at, added) VALUES ('item_a', ?, ?, 3)", (ts, ts))
    conn.commit()
    conn.close()


def columns(conn, table):
    rows = list(conn.execute(f"PRAGMA table_info({table})"))
    return [r["name"] for r in rows], [r["name"] for r in sorted(rows, key=lambda r: r["pk"]) if r["pk"]]


def test_upgrade_from_v2_keeps_every_row_and_adds_ai_tables(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    make_v2_db(path)
    raw = sqlite3.connect(path)
    before = dump(raw, V2_TABLES)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'ai_%'").fetchone()
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert dump(conn, V2_TABLES) == before
    assert columns(conn, "ai_runs") == (["run_id", "created_at", "applied_at", "queued_groups", "queued_txns",
                                         "summary_json"], ["run_id"])
    assert columns(conn, "ai_run_groups") == (["run_id", "group_id", "merchant", "rule_field", "rule_pattern",
                                               "transaction_ids_json"], ["run_id", "group_id"])
    assert columns(conn, "ai_reviews") == (["transaction_id", "run_id", "reviewed_at", "verdict", "category_id",
                                            "confidence", "note"], ["transaction_id"])
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict) "
                         "VALUES ('t_plaid', 'r', 'now', 'maybe')")
    # the views still work over the old rows, with the new AI columns
    rows = {r["transaction_id"]: tuple(r) for r in conn.execute(
        "SELECT transaction_id, category, category_source, ai_verdict, ai_note, ai_flagged FROM v_transactions")}
    assert rows == {"t_user": ("t_user", "Other Health", "user", None, None, 0),
                    "t_ai": ("t_ai", "Subscriptions", "ai", None, None, 0),
                    "t_plaid": ("t_plaid", "Eating Out", "plaid", None, None, 0)}
    # and the AI loop runs on the upgraded DB: only the Plaid-filed row needs a look
    q = ai.queue(conn)
    assert [(g["merchant"], g["count"]) for g in q["groups"]] == [("Whole Foods", 1)]
    after = dump(conn, V2_TABLES)
    conn.close()
    again = db.connect(path)                                   # upgrading is idempotent
    assert dump(again, V2_TABLES) == after == before
    assert again.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0] == 1
    again.close()


def test_snapshot_upserts_one_row_per_account_per_day(conn, client, fake):
    add_item(conn)
    fake.on("/accounts/get", {"accounts": [account(current=100.0),
                                           account("acc_cc", "Card", "9999", "credit", "credit card", 50.0, 950.0, 1000.0)]})
    sync_all(conn, client)
    fake.on("/accounts/get", {"accounts": [account(current=175.0), account("acc_cc", "Card", "9999", "credit",
                                                                            "credit card", 80.0, 920.0, 1000.0)]})
    sync_all(conn, client)
    rows = {r["account_id"]: (r["date"], r["current"], r["available"]) for r in conn.execute("SELECT * FROM balance_snapshots")}
    today = date.today().isoformat()
    assert rows == {"acc_chk": (today, 175.0, 900.0), "acc_cc": (today, 80.0, 920.0)}


def test_failed_sync_writes_no_snapshot(conn, client, fake):
    add_item(conn)
    fake.on("/transactions/sync", err("ITEM_LOGIN_REQUIRED"))
    (r,) = sync_all(conn, client)
    assert not r.ok
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0] == 0


def test_snapshot_is_skipped_quietly_if_it_fails(conn, client, fake, monkeypatch):
    from hermes_finance import sync

    def boom(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sync, "snapshot_balances", boom)
    add_item(conn)
    (r,) = sync_all(conn, client)
    assert r.ok and r.extras == {"balances": "ok"}
    assert json.loads(conn.execute("SELECT details FROM sync_log").fetchone()[0]) == {"balances": "ok"}


def make_v3_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V3_SQL)
    ts = "2026-09-24T00:00:00Z"
    conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, products, "
                 "created_at, last_sync_at) VALUES ('item_a', 'ins_1', 'Nova', 'access-production-v3', 'active', "
                 "'[\"transactions\", \"investments\"]', ?, ?)", (ts, ts))
    for acc, name, typ, sub, cur in (("acc_chk", "Nova Checking", "depository", "checking", 2400.0),
                                     ("acc_inv", "Nova Invest", "investment", "brokerage", 491.47),
                                     ("acc_cc", "Card", "credit", "credit card", 300.0)):
        conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                     "iso_currency, updated_at) VALUES (?, 'item_a', ?, '7385', ?, ?, ?, 'USD', ?)",
                     (acc, name, typ, sub, cur, ts))
    for tid, name, amount, pfc, day in (("t_toy", "ZENITH ACH RTL", 465.10, "LOAN_PAYMENTS", "2026-09-25"),
                                        ("t_food", "WHOLE FOODS", 55, "FOOD_AND_DRINK", "2026-09-15")):
        conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, pfc_primary, "
                     "raw_json, first_seen_at, updated_at) VALUES (?, 'acc_chk', 'item_a', ?, ?, ?, ?, '{}', ?, ?)",
                     (tid, day, name, amount, pfc, ts, ts))
    conn.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                 "SELECT 't_food', id, 'user', ? FROM categories WHERE name = 'Groceries'", (ts,))
    conn.execute("INSERT INTO holdings (account_id, security_id, item_id, ticker_symbol, security_name, security_type, "
                 "quantity, institution_price, institution_value, updated_at) VALUES ('acc_inv', 'sec_nvda', 'item_a', "
                 "'NVDA', 'NVIDIA Corp', 'equity', 2.18176, 225.0, 490.90, ?)", (ts,))
    conn.execute("INSERT INTO ai_runs (run_id, created_at, applied_at, queued_groups, queued_txns) "
                 "VALUES ('ai-20260924-000000', ?, ?, 1, 1)", (ts, ts))
    conn.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, category_id, confidence) "
                 "SELECT 't_toy', 'ai-20260924-000000', ?, 'confirmed', id, 0.9 FROM categories WHERE name = 'Transport'",
                 (ts,))
    conn.execute("UPDATE categories SET monthly_cap = 200 WHERE name = 'Shopping'")
    conn.execute("INSERT INTO balance_snapshots VALUES ('acc_chk', '2026-09-24', 2400, NULL, 'USD')")
    conn.commit()
    conn.close()


def test_upgrade_from_v3_keeps_every_row_and_adds_manual_tables(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    make_v3_db(path)
    raw = sqlite3.connect(path)
    before = dump(raw, V3_TABLES)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name IN ('manual_accounts', 'loan_payments', "
                           "'holding_snapshots', 'manual_balance_history')").fetchone()
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert dump(conn, V3_TABLES) == before
    assert columns(conn, "manual_accounts") == (
        ["id", "kind", "name", "institution", "subtype", "balance", "credit_limit", "interest_rate", "original_amount",
         "monthly_payment", "match_pattern", "vehicle_make", "vehicle_model", "vehicle_year", "balance_as_of",
         "created_at", "updated_at", "archived_at",
         "vin", "vehicle_trim", "mileage", "mileage_as_of", "zip_code", "address", "vin_decoded", "value_source",  # v7
         "vehicle_condition",  # v8
         "linked_loan_id"],  # v9
        ["id"])
    assert columns(conn, "manual_balance_history") == (["manual_account_id", "date", "balance"],
                                                       ["manual_account_id", "date"])
    assert columns(conn, "loan_payments") == (["transaction_id", "loan_id", "amount", "interest", "principal",
                                               "applied_at", "source"], ["transaction_id"])
    assert columns(conn, "holding_snapshots") == (["date", "account_id", "security_id", "ticker", "quantity", "price",
                                                   "value"], ["account_id", "security_id", "date"])
    for bad in ("INSERT INTO manual_accounts (kind, name, balance, balance_as_of, created_at, updated_at) "
                "VALUES ('boat', 'x', 1, '2026-09-26', 'now', 'now')",
                "INSERT INTO manual_accounts (kind, name, balance, balance_as_of, created_at, updated_at) "
                "VALUES ('loan', 'x', -1, '2026-09-26', 'now', 'now')",
                "INSERT INTO loan_payments VALUES ('t_food', 1, 1, 0, 1, 'now', 'robot')"):
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(bad)
    # the old rows still read the same, and the new features work on the upgraded DB
    assert tuple(conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id = 't_food'")
                 .fetchone()) == ("Groceries", "user")
    loan = manual.create(conn, {"kind": "loan", "name": "Zenith", "institution": "Zenith Financial Services",
                                "subtype": "auto", "original_amount": 30947.0, "balance": 5910.0,
                                "monthly_payment": 465.10, "match_pattern": "ZENITH ACH RTL",
                                "balance_as_of": "2026-09-23"}, date(2026, 9, 26))
    assert [p["transaction_id"] for p in manual.match_loans(conn)["applied"]] == ["t_toy"]
    assert conn.execute("SELECT balance FROM manual_accounts").fetchone()[0] == 5444.90
    nw = insights.net_worth(conn)
    assert nw["net"] == round(2400.0 + 491.47 - 300.0 - 5444.90, 2)
    after = dump(conn, V3_TABLES + V4_NEW)
    conn.close()
    again = db.connect(path)                                   # upgrading is idempotent
    assert dump(again, V3_TABLES + V4_NEW) == after
    assert dump(again, V3_TABLES) == before
    assert again.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0] == 1 and loan["id"] == 1
    again.close()


def holdings_route(value):
    return {"accounts": [account("acc_inv", "Nova Invest", "6820", "investment", "brokerage", value, None)],
            "holdings": [{"account_id": "acc_inv", "security_id": "sec_nvda", "quantity": 2.18176,
                          "institution_price": round(value / 2.18176, 2), "institution_value": value},
                         {"account_id": "acc_inv", "security_id": "sec_cash", "quantity": 0.57,
                          "institution_price": 1.0, "institution_value": 0.57}],
            "securities": [{"security_id": "sec_nvda", "ticker_symbol": "NVDA", "name": "NVIDIA Corp", "type": "equity"},
                           {"security_id": "sec_cash", "ticker_symbol": "CUR:USD", "name": "U S Dollar",
                            "type": "cash"}]}


def test_holding_snapshots_are_one_row_per_security_per_day_and_idempotent(conn, client, fake):
    add_item(conn, products=("transactions", "investments"))
    fake.on("/item/get", {"item": {"item_id": "item_a", "institution_id": "ins_1",
                                   "products": ["transactions", "investments"]}})
    fake.on("/investments/holdings/get", holdings_route(490.90))
    sync_all(conn, client)
    fake.on("/investments/holdings/get", holdings_route(500.00))
    sync_all(conn, client)                                     # same day: updated in place, not duplicated
    today = date.today().isoformat()
    rows = sorted(tuple(r) for r in conn.execute("SELECT date, account_id, security_id, ticker, value FROM holding_snapshots"))
    assert rows == [(today, "acc_inv", "sec_cash", "CUR:USD", 0.57), (today, "acc_inv", "sec_nvda", "NVDA", 500.0)]


def test_failed_holdings_call_writes_no_holding_snapshot(conn, client, fake):
    add_item(conn, products=("transactions", "investments"))
    fake.on("/item/get", {"item": {"item_id": "item_a", "institution_id": "ins_1",
                                   "products": ["transactions", "investments"]}})
    fake.on("/investments/holdings/get", holdings_route(490.90))
    sync_all(conn, client)
    conn.execute("UPDATE holding_snapshots SET date = '2026-01-01'")
    conn.commit()
    fake.on("/investments/holdings/get", err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))
    (r,) = sync_all(conn, client)
    assert r.ok and r.extras["investments"].startswith("err")
    assert {x[0] for x in conn.execute("SELECT date FROM holding_snapshots")} == {"2026-01-01"}   # stale ones not re-dated


def test_a_second_sync_the_same_day_drops_a_sold_position(conn, client, fake):
    add_item(conn, products=("transactions", "investments"))
    fake.on("/item/get", {"item": {"item_id": "item_a", "institution_id": "ins_1",
                                   "products": ["transactions", "investments"]}})
    fake.on("/investments/holdings/get", holdings_route(490.90))
    sync_all(conn, client)
    sold = holdings_route(0)
    sold["holdings"] = [{"account_id": "acc_inv", "security_id": "sec_cash", "quantity": 491.47, "institution_price": 1.0,
                         "institution_value": 491.47}]
    fake.on("/investments/holdings/get", sold)
    sync_all(conn, client)
    rows = sorted(tuple(r) for r in conn.execute("SELECT security_id, value FROM holding_snapshots"))
    assert rows == [("sec_cash", 491.47)]                                  # NVDA sold: not counted twice


# --- v4 -> v5: the logo caches ------------------------------------------------------------------------

def make_v4_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V4_SQL)
    ts = "2026-09-25T00:00:00Z"
    conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, products, "
                 "created_at, last_sync_at) VALUES ('item_a', 'ins_126339', 'Nova', 'access-production-v4', 'active', "
                 "'[\"transactions\", \"investments\"]', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
                 "updated_at) VALUES ('acc_inv', 'item_a', 'Nova Invest', '6820', 'investment', 'brokerage', 491.47, "
                 "'USD', ?)", (ts,))
    conn.execute("INSERT INTO holdings (account_id, security_id, item_id, ticker_symbol, security_name, security_type, "
                 "quantity, institution_price, institution_value, updated_at) VALUES ('acc_inv', 'sec_nvda', 'item_a', "
                 "'NVDA', 'NVIDIA Corp', 'equity', 2.18176, 225.0, 490.9, ?)", (ts,))
    conn.execute("INSERT INTO manual_accounts (kind, name, institution, subtype, balance, balance_as_of, created_at, "
                 "updated_at) VALUES ('loan', 'Car', 'Ally Auto', 'auto', 5000, '2026-09-01', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO holding_snapshots VALUES ('2026-09-25', 'acc_inv', 'sec_nvda', 'NVDA', 2.18176, 225, 490.9)")
    conn.commit()
    conn.close()


def test_upgrade_from_v4_keeps_every_row_and_adds_the_logo_caches(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    make_v4_db(path)
    raw = sqlite3.connect(path)
    before = dump(raw, V4_TABLES)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 4
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name IN ('institutions', 'security_logos')").fetchone()
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert dump(conn, V4_TABLES) == before
    assert columns(conn, "institutions") == (["institution_id", "name", "logo_png", "logo_source", "primary_color", "url",
                                              "fetched_at"], ["institution_id"])
    assert columns(conn, "security_logos") == (["ticker", "png", "source", "fetched_at"], ["ticker"])
    for bad in ("INSERT INTO institutions (institution_id, logo_source, fetched_at) VALUES ('ins_1', 'hotlink', 'now')",
                "INSERT INTO security_logos (ticker, source, fetched_at) VALUES ('NVDA', 'guess', 'now')",
                "INSERT INTO security_logos (ticker, source) VALUES ('NVDA', 'fmp')"):
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(bad)
    with conn:
        conn.execute("INSERT INTO institutions VALUES ('ins_126339', 'Nova', x'89504e47', 'plaid', '#00a9ce', "
                     "'https://www.novabank.example', 'now')")
        conn.execute("INSERT INTO security_logos VALUES ('NVDA', NULL, 'none', '2026-09-26')")
    after = dump(conn, V4_TABLES + V5_NEW)
    conn.close()
    again = db.connect(path)                                   # upgrading is idempotent
    assert dump(again, V4_TABLES + V5_NEW) == after and dump(again, V4_TABLES) == before
    again.close()


V5_SQL = (Path(__file__).parent / "fixtures" / "schema_v5.sql").read_text()
V5_TABLES = V4_TABLES + V5_NEW


def make_v5_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V5_SQL)
    ts = "2026-09-01T00:00:00Z"
    conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                 "VALUES ('item_a', 'ins_1', 'Nova', 'access-production-v5', 'active', ?)", (ts,))
    conn.execute("INSERT INTO manual_accounts (kind, name, institution, balance, credit_limit, balance_as_of, created_at, "
                 "updated_at) VALUES ('credit_card', 'Best Buy', 'Citi', 1110.66, 4000, '2026-09-26', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO institutions VALUES ('ins_1', 'Nova', NULL, 'none', '#00a9ce', NULL, ?)", (ts,))
    conn.execute("UPDATE categories SET monthly_cap = 800 WHERE name = 'Transport'")
    conn.commit()
    conn.close()


def test_upgrade_from_v5_keeps_every_row_and_adds_settings(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    make_v5_db(path)
    raw = sqlite3.connect(path)
    before = dump(raw, V5_TABLES)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'settings'").fetchone()
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert dump(conn, V5_TABLES) == before
    assert columns(conn, "settings") == (["key", "value", "updated_at"], ["key"])
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO settings (key, updated_at) VALUES ('x', 'now')")   # a value is required
    with conn:
        conn.execute("INSERT INTO settings VALUES ('chart_excluded_categories', '[\"Rent & Utilities\"]', 'now')")
    after = dump(conn, V5_TABLES + ["settings"])
    conn.close()
    again = db.connect(path)                                   # upgrading is idempotent
    assert dump(again, V5_TABLES + ["settings"]) == after and dump(again, V5_TABLES) == before
    again.close()


V6_SQL = (Path(__file__).parent / "fixtures" / "schema_v6.sql").read_text()
V6_TABLES = V5_TABLES + ["settings"]
V7_NEW = ["asset_valuations", "vin_decodes", "api_usage", "api_calls"]


def make_v6_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V6_SQL)
    ts = "2026-09-01T00:00:00Z"
    conn.execute("INSERT INTO manual_accounts (kind, name, balance, vehicle_make, vehicle_model, vehicle_year, "
                 "balance_as_of, created_at, updated_at) VALUES ('vehicle', '2019 Zenith Mesa', 20000, 'Zenith', 'Mesa', "
                 "2019, '2026-07-28', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO manual_accounts (kind, name, balance, balance_as_of, created_at, updated_at) "
                 "VALUES ('property', 'Cabin', 150000, '2026-09-01', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO manual_accounts (kind, name, institution, subtype, balance, original_amount, monthly_payment, "
                 "balance_as_of, created_at, updated_at) VALUES ('loan', 'Zenith', 'TFS', 'auto', 5910, 30947, 465.1, "
                 "'2026-08-14', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO manual_balance_history VALUES (1, '2026-07-28', 20000)")
    conn.execute("INSERT INTO settings VALUES ('chart_excluded_categories', '[]', ?)", (ts,))
    conn.commit()
    conn.close()


def test_upgrade_from_v6_adds_valuation(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    make_v6_db(path)
    raw = sqlite3.connect(path)
    before = dump(raw, V6_TABLES)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 6
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'api_usage'").fetchone()
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert dump(conn, V6_TABLES) == before                          # every old value kept
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(manual_accounts)")]
    assert cols[-10:] == ["vin", "vehicle_trim", "mileage", "mileage_as_of", "zip_code", "address", "vin_decoded",
                          "value_source", "vehicle_condition", "linked_loan_id"]
    # an upgraded table is the same as a new one (the columns were added at the end of the CREATE TABLE too)
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(db.SCHEMA)
    assert cols == [r[1] for r in fresh.execute("PRAGMA table_info(manual_accounts)")]
    # the typed vehicle / property values are "Your value"; other kinds have no value source
    assert [tuple(r) for r in conn.execute("SELECT kind, value_source FROM manual_accounts ORDER BY id")] == \
        [("vehicle", "user"), ("property", "user"), ("loan", None)]
    assert columns(conn, "asset_valuations")[0] == ["id", "account_id", "value", "low", "high", "source", "miles",
                                                     "subject", "fetched_at", "raw_json", "details_json", "flag",
                                                     "confirmed_at"]
    assert columns(conn, "vin_decodes") == (["vin", "vehicle_year", "vehicle_make", "vehicle_model", "vehicle_trim",
                                             "fetched_at", "specs_json"], ["vin"])
    assert columns(conn, "api_usage") == (["provider", "month", "calls", "blocked_until", "blocked_reason", "updated_at"],
                                          ["provider", "month"])
    # TASK-13b: no usage is seeded any more (it was MarketCheck's / RentCast's, both gone); the v9 link: the only
    # vehicle -> the only auto loan
    assert conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 0
    assert [tuple(r) for r in conn.execute("SELECT kind, linked_loan_id FROM manual_accounts ORDER BY id")] == \
        [("vehicle", 3), ("property", None), ("loan", None)]
    for bad in ("UPDATE manual_accounts SET value_source = 'guess' WHERE id = 1",
                "INSERT INTO asset_valuations (account_id, value, source, fetched_at) VALUES (1, 0, 'marketcheck', 'x')",
                "INSERT INTO asset_valuations (account_id, value, source, fetched_at) VALUES (1, 5, 'kbb', 'x')",
                "INSERT INTO api_calls (provider, trigger, at, day) VALUES ('rentcast', 'loop', 'x', 'y')"):
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(bad)
    after = dump(conn, V6_TABLES + V7_NEW)
    conn.close()
    again = db.connect(path)                                   # upgrading is idempotent
    assert dump(again, V6_TABLES + V7_NEW) == after
    assert again.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 0
    again.close()


def test_an_interrupted_upgrade_seeds_nothing_and_links_once(data_dir):
    """Upgrading again after a stop before the version was written: still no seeded usage, and the car keeps its link."""
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    make_v6_db(path)
    conn = db.connect(path)
    with conn:
        conn.execute("PRAGMA user_version = 6")                 # as if it stopped before the version was written
    conn.close()
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 0
    assert conn.execute("SELECT linked_loan_id FROM manual_accounts WHERE kind = 'vehicle'").fetchone()[0] == 3
    conn.close()


# --- TASK-20: the merged order v10 (TASK-17) -> v11 (TASK-18, Batch A) -> v12 (TASK-19, Batch C) ---------------------

FIXTURES = Path(__file__).parent / "fixtures"
V9_UP = ["items", "accounts", "transactions", "categories", "classifications", "rules", "manual_accounts",
         "manual_balance_history", "holding_snapshots", "settings", "balance_snapshots"]
V11_NEW = ["auth_users", "auth_recovery_codes", "auth_sessions", "auth_failures", "mcp_tokens", "imports"]
V12_NEW = ["transaction_splits", "transaction_notes", "tags", "transaction_tags", "saved_filters", "category_rollover",
           "goals", "goal_entries", "alpaca_snapshots"]
V13_NEW = ["tracked_bills"]   # TASK-21
V14_NEW = ["rule_deletions"]  # TASK-24 fix (and classifications.detached_by)


def _names(conn, kind="table") -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def _shape(conn) -> dict[str, list[str]]:
    """Every table's columns in order, and every view's: what a new database has, whatever the path to it."""
    return {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            for t in sorted(_names(conn) | _names(conn, "view")) if not t.startswith("sqlite_")}


def _old_db(path: Path, fixture: str, version: int) -> sqlite3.Connection:
    config.ensure_data_dir(path.parent)
    raw = sqlite3.connect(path)
    raw.executescript((FIXTURES / fixture).read_text())
    assert raw.execute("PRAGMA user_version").fetchone()[0] == version
    ts = "2026-08-01T00:00:00Z"
    raw.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                "VALUES ('item_a', 'ins_1', 'Chase', 'access-production-x', 'active', ?)", (ts,))
    raw.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
                "updated_at) VALUES ('acc_chk', 'item_a', 'Checking', '1234', 'depository', 'checking', 1000, 'USD', ?)", (ts,))
    for tid, amount, pfc in (("t1", 42.5, "FOOD_AND_DRINK_GROCERIES"), ("t2", -3000, "INCOME"), ("t3", 19.99, "ENTERTAINMENT")):
        raw.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, pfc_primary, "
                    "pfc_detailed, first_seen_at, updated_at) VALUES (?, 'acc_chk', 'item_a', '2026-08-02', 'X', ?, ?, ?, ?, ?)",
                    (tid, amount, pfc.split("_GROCERIES")[0], pfc, ts, ts))
    raw.execute("INSERT INTO classifications (transaction_id, category_id, source, updated_at) "
                "SELECT 't3', id, 'user', ? FROM categories WHERE name = 'Subscriptions'", (ts,))
    raw.execute("UPDATE categories SET monthly_cap = 400 WHERE name = 'Groceries'")
    raw.execute("INSERT INTO manual_accounts (kind, name, subtype, balance, balance_as_of, created_at, updated_at) "
                "VALUES ('loan', 'Car Loan', 'auto', 7000, '2026-08-01', ?, ?)", (ts, ts))
    raw.execute("INSERT INTO settings (key, value, updated_at) VALUES ('networth.mode', '\"custom\"', ?)", (ts,))
    raw.commit()
    return raw


def _upgrade_and_check(path: Path, raw: sqlite3.Connection, tables: list[str]) -> sqlite3.Connection:
    """Upgrade to v16 (polish P0's category groups; the TASK-24 fix's v15, the chat lane's v14 and TASK-21's v13 before
    it): every old row keeps every value, the tables and view of each later step are there, and the database has exactly
    a new one's shape. Then again (idempotent), and after an interrupted run (the version not written yet): nothing
    changes."""
    before = dump(raw, tables)
    raw.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert dump(conn, tables) == before
    assert set(V11_NEW + V12_NEW + V13_NEW + V14_NEW + ["pending_actions"]) <= _names(conn) and "v_spend_lines" in _names(conn, "view")
    fresh = db.connect(path.parent / "fresh.db")
    assert _shape(conn) == _shape(fresh)                            # an upgraded database is a new one plus its rows
    fresh.close()
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, source, import_id, fitid FROM transactions "
                                           "WHERE item_id = 'item_a' ORDER BY transaction_id")] == \
        [("t1", "plaid", None, None), ("t2", "plaid", None, None), ("t3", "plaid", None, None)]
    # the views read the old rows as before: a synced row is 'plaid', and with no split its one spending line
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, category, source FROM v_transactions "
                                           "WHERE item_id = 'item_a' ORDER BY transaction_id")] == \
        [("t1", "Groceries", "plaid"), ("t2", "Income", "plaid"), ("t3", "Subscriptions", "plaid")]
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, split_line, spend, category FROM v_spend_lines "
                                           "WHERE item_id = 'item_a' ORDER BY transaction_id")] == \
        [("t1", None, 42.5, "Groceries"), ("t2", None, -3000.0, "Income"), ("t3", None, 19.99, "Subscriptions")]
    new_tables = [t for t in V12_NEW + V13_NEW + V14_NEW if t not in tables]
    assert all(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0 for t in new_tables)   # nothing made up
    later = [t for t in V11_NEW + V12_NEW + V13_NEW + V14_NEW if t not in tables]
    after = {**dump(conn, tables + later), "settings": dump(conn, ["settings"])["settings"]}
    settings = [tuple(r) for r in conn.execute("SELECT key, value FROM settings ORDER BY key")]
    db.init_schema(conn)                                            # again: nothing changes
    assert {**dump(conn, tables + later), "settings": dump(conn, ["settings"])["settings"]} == after
    for stopped in (11, 12, 13, 14, 15):                            # as if it stopped before v12 / ... / v16 was written
        with conn:
            conn.execute(f"PRAGMA user_version = {stopped}")
        conn.close()
        conn = db.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert {**dump(conn, tables + later), "settings": dump(conn, ["settings"])["settings"]} == after
    assert [tuple(r) for r in conn.execute("SELECT key, value FROM settings ORDER BY key")] == settings
    return conn


def test_upgrade_from_v9_runs_v10_v11_v12_v13_in_order_and_keeps_every_row(data_dir):
    """The live database is v9: one start takes it through TASK-17's pending_actions (v10), TASK-18's login / token /
    import tables and transactions columns (v11), TASK-19's tables and view (v12) and TASK-21's tracked_bills (v13), in
    that order."""
    path = config.db_path()
    raw = _old_db(path, "schema_v9.sql", 9)
    assert not {"pending_actions", *V11_NEW, *V12_NEW, *V13_NEW} & _names(raw)
    conn = _upgrade_and_check(path, raw, V9_UP)
    # an install from before v11 keeps Carfax and Redfin on (TASK-18's upgrade seeds them), once
    assert {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'valuations.%'")} == \
        {"valuations.carfax": "true", "valuations.redfin": "true"}
    conn.close()


def test_upgrade_from_v10_to_v13_keeps_every_row_and_the_pending_actions(data_dir):
    path = config.db_path()
    raw = _old_db(path, "schema_v10.sql", 10)
    raw.execute("INSERT INTO pending_actions (tool, arguments, summary, client, created_at) VALUES "
                "('finance_set_cap', '{\"category\": \"Groceries\", \"monthly_cap\": 450}', 'Groceries cap to $450', 'cli', "
                "'2026-08-03T00:00:00Z')")
    raw.commit()
    assert not {*V11_NEW, *V12_NEW} & _names(raw)
    conn = _upgrade_and_check(path, raw, V9_UP + ["pending_actions"])
    assert {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'valuations.%'")} == \
        {"valuations.carfax": "true", "valuations.redfin": "true"}
    conn.close()


def test_upgrade_from_v11_batch_a_to_v13_adds_only_batch_c_and_task21(data_dir):
    """A database TASK-18 (Batch A) alone made (v11: a login, an MCP token, an import, Carfax switched off by its owner):
    the upgrade adds TASK-19's tables and view (and TASK-21's table) and touches nothing of v11 (the valuation switches
    aren't seeded again)."""
    path = config.db_path()
    raw = _old_db(path, "schema_v11a.sql", 11)
    ts = "2026-08-05T00:00:00Z"
    raw.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                "VALUES ('owner', '$argon2id$v=19$m=65536,t=3,p=4$x$y', 'JBSWY3DPEHPK3PXP', ?, ?)", (ts, ts))
    raw.execute("INSERT INTO mcp_tokens (name, token_hash, last4, created_at) VALUES ('laptop', ?, 'abcd', ?)", ("0" * 64, ts))
    raw.execute("INSERT INTO manual_accounts (kind, name, institution, subtype, balance, balance_as_of, created_at, updated_at) "
                "VALUES ('bank', 'Credit union', 'Local CU', 'checking', 250, '2026-08-05', ?, ?)", (ts, ts))
    mid = raw.execute("SELECT id FROM manual_accounts WHERE name = 'Credit union'").fetchone()[0]
    raw.execute("INSERT INTO imports (created_at, filename, kind, manual_account_id, rows_new, rows_skipped) "
                "VALUES (?, 'cu.ofx', 'ofx', ?, 1, 0)", (ts, mid))
    raw.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, first_seen_at, updated_at, "
                "source, import_id, fitid) VALUES ('imp1', ?, 'import', '2026-08-04', 'CORNER STORE', 8.75, ?, ?, 'import', 1, 'F1')",
                (f"manual:{mid}", ts, ts))
    raw.executemany("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    [("valuations.carfax", "false", ts), ("valuations.redfin", "true", ts), ("access.mode", '"builtin"', ts)])
    raw.commit()
    assert not set(V12_NEW) & _names(raw) and set(V11_NEW) <= _names(raw)
    tables = V9_UP + ["pending_actions"] + V11_NEW
    before_imported = [tuple(r) for r in raw.execute("SELECT * FROM transactions WHERE source = 'import'")]
    conn = _upgrade_and_check(path, raw, tables)
    assert {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'valuations.%'")} == \
        {"valuations.carfax": "false", "valuations.redfin": "true"}   # the owner's choice, not re-seeded
    assert [tuple(r) for r in conn.execute("SELECT * FROM transactions WHERE source = 'import'")] == before_imported
    assert tuple(conn.execute("SELECT account_name, institution_name, category, source FROM v_transactions "
                              "WHERE transaction_id = 'imp1'").fetchone()) == ("Credit union", "Local CU", "Other", "import")
    assert tuple(conn.execute("SELECT account_name, institution_name, category, spend FROM v_spend_lines "
                              "WHERE transaction_id = 'imp1'").fetchone()) == ("Credit union", "Local CU", "Other", 8.75)
    conn.close()


def test_upgrade_from_v12_to_v13_adds_only_tracked_bills(data_dir):
    """TASK-21: a database TASK-20's merged build made (v12: a goal, a pending action, the built-in login) gets tracked_bills
    and nothing else changes; the bills it lists are the same as before (no tracked row, so detection alone)."""
    path = config.db_path()
    raw = _old_db(path, "schema_v12.sql", 12)
    ts = "2026-08-05T00:00:00Z"
    raw.execute("INSERT INTO goals (kind, name, target, track, created_at, updated_at) VALUES ('save', 'Trip', 1500, 'manual', ?, ?)",
                (ts, ts))
    raw.execute("INSERT INTO pending_actions (tool, arguments, summary, client, created_at) VALUES ('finance_set_cap', "
                "'{\"category\": \"Groceries\", \"monthly_cap\": 450}', 'Groceries cap to $450', 'cli', ?)", (ts,))
    raw.execute("INSERT INTO settings (key, value, updated_at) VALUES ('access.mode', '\"builtin\"', ?)", (ts,))
    raw.commit()
    assert set(V12_NEW) <= _names(raw) and not set(V13_NEW) & _names(raw)
    tables = V9_UP + ["pending_actions"] + V11_NEW + V12_NEW
    conn = _upgrade_and_check(path, raw, tables)
    assert [r[1] for r in conn.execute("PRAGMA table_info(tracked_bills)")] == [
        "id", "merchant_key", "merchant", "cadence", "variable", "usual_low", "usual_high", "anchor_date", "anchor_amount",
        "anchor_transaction_id", "created_at", "updated_at", "stopped_at"]
    with pytest.raises(sqlite3.IntegrityError):   # its checks hold: a schedule it doesn't know, a usual range upside down
        with conn:
            conn.execute("INSERT INTO tracked_bills (merchant_key, merchant, cadence, variable, anchor_date, anchor_amount, "
                         "created_at, updated_at) VALUES ('x', 'X', 'fortnightly', 0, '2026-08-01', 1, ?, ?)", (ts, ts))
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO tracked_bills (merchant_key, merchant, cadence, variable, usual_low, usual_high, anchor_date, "
                         "anchor_amount, created_at, updated_at) VALUES ('x', 'X', 'bimonthly', 1, 150, 100, '2026-08-01', 1, ?, ?)",
                         (ts, ts))
    assert conn.execute("SELECT COUNT(*) FROM tracked_bills").fetchone()[0] == 0
    assert insights.recurring(conn, date(2026, 9, 28))["items"] == []
    conn.close()


def test_upgrade_from_v13_to_v14_adds_rule_deletions_and_keeps_every_row(data_dir):
    """TASK-24 fix: a database TASK-21 made (v13, from db.py at 6b89dd5: a user rule filing a row, an AI rule filing
    another, a user row, a tracked bill) gets rule_deletions, classifications.detached_by (NULL on every old row) and its
    trigger, and nothing else changes. Then the upgraded database does what the fix is for: the user rule's delete gives a
    receipt, the AI can't overwrite the row it filed, and Undo links it again."""
    path = config.db_path()
    raw = _old_db(path, "schema_v13.sql", 13)
    ts = "2026-08-05T00:00:00Z"
    raw.execute("UPDATE transactions SET merchant_name = 'Corner Store' WHERE transaction_id = 't2'")
    for field, pattern, cat, prio, who in (("name", "X", "Groceries", 200, "user"),
                                          ("merchant_name", "Corner Store", "Income", 100, "ai")):  # as the views read them
        raw.execute("INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at) "
                    "SELECT ?, 'exact', ?, id, ?, ?, ? FROM categories WHERE name = ?", (field, pattern, prio, who, ts, cat))
    raw.execute("INSERT INTO classifications (transaction_id, category_id, source, rule_id, note, updated_at) "
                "SELECT 't1', category_id, 'rule', id, 'rule #' || id, ? FROM rules WHERE pattern = 'X'", (ts,))
    raw.execute("INSERT INTO classifications (transaction_id, category_id, source, rule_id, note, updated_at) "
                "SELECT 't2', category_id, 'rule', id, 'rule #' || id, ? FROM rules WHERE pattern = 'Corner Store'", (ts,))
    raw.execute("INSERT INTO tracked_bills (merchant_key, merchant, cadence, variable, anchor_date, anchor_amount, created_at, "
                "updated_at) VALUES ('netflix', 'Netflix', 'monthly', 0, '2026-08-01', 15.49, ?, ?)", (ts, ts))
    raw.commit()
    assert not set(V14_NEW) & _names(raw) and "detached_by" not in [r[1] for r in raw.execute("PRAGMA table_info(classifications)")]
    assert not _names(raw, "trigger") - {"trg_classifications_protect_user"}
    tables = V9_UP + ["pending_actions"] + V11_NEW + V12_NEW + V13_NEW
    conn = _upgrade_and_check(path, raw, tables)
    assert [r[1] for r in conn.execute("PRAGMA table_info(classifications)")][-1] == "detached_by"
    assert [r[0] for r in conn.execute("SELECT detached_by FROM classifications")] == [None] * 3
    assert _names(conn, "trigger") == {"trg_classifications_protect_user", "trg_classifications_detached_changed"}
    assert [r[1] for r in conn.execute("PRAGMA table_info(rule_deletions)")] == [
        "id", "receipt_hash", "rule_id", "match_field", "match_type", "pattern", "category_id", "priority", "created_from",
        "created_at", "filed", "deleted_at", "restored_at", "restored_rule_id"]
    user_rule = conn.execute("SELECT id FROM rules WHERE pattern = 'X'").fetchone()[0]
    out = rulebook.delete(conn, user_rule)
    assert out["filed"] == ["t1"] and set(out["undo"]) == {"receipt"}
    assert classify.set_category(conn, "t1", "Travel", "ai") == "kept-user-rule"          # still the user's
    assert classify.set_category(conn, "t2", "Travel", "ai") == "set"                    # an AI rule's row: open to the AI
    back = rulebook.restore(conn, out["undo"])
    assert back["relinked"] == 1 and back["rule"]["id"] == user_rule and back["rule"]["source"] == "user"
    assert tuple(conn.execute("SELECT category_id, source, rule_id, detached_by FROM classifications WHERE transaction_id = 't1'"
                              ).fetchone())[1:] == ("rule", user_rule, None)
    conn.close()
