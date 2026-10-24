"""Schema-15 acceptance (rules-integration review of 04f33a1, gaps G1-G3): a database either lane stamped 14 opens at 15
with every original row and value, exactly a new database's schema definition, and both lanes' data still working.

G2  The version-14 inputs are pinned from the parents' immutable Git objects (tests/schema14_parents.py): each parent's
    new database, and the v13 fixture as that parent upgraded it, with its own views, defaults, keys, indexes and
    trigger. Nothing here rebuilds a 14 from the db module under test.
G1  Each input carries its lane's own data (chat: Ask-origin actions, cards an Ask turn still holds; rules: deletion
    receipts, one restored, rows still detached, a row changed since its delete) and rows in the other tables. Every row
    of every table is snapshotted with typed values, by rowid, before the upgrade and compared after it and after each
    reopen; the columns 15 adds are named and checked at their defaults, apart from the originals. Then receipts
    restore and protect, and held cards stay held, on the upgraded database.
G3  Definitions are compared (declarations, defaults, CHECKs, foreign keys, indexes, views, triggers), not names. An
    ordinary reopen (init_schema not run), a complete schema restamped 14 (init_schema replayed over it) and genuine
    partial installs (the real init_schema stopped by an SQLite authorizer refusing one schema statement, as a crash
    just before it would) are separate tests. The sweep covers every schema statement of the 14 -> 15 run on these
    inputs; it claims nothing about a write torn inside a statement, disk or WAL faults, or another writer.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

import schema14_parents as parents
from hermes_finance import classify, config, db, mcp_state, rulebook

VARIANTS = [("chat", False), ("chat", True), ("rules", False), ("rules", True)]
IDS = ["chat-new", "chat-from-v13", "rules-new", "rules-from-v13"]
# The committed fixtures, byte for byte (test_the_pinned_fixtures_are_what_the_parent_commits_give rebuilds them from
# the Git objects; this holds them where there are none, e.g. a git archive of the tree)
FIXTURE_SHA256 = {
    "schema_v14_chat.sql": "c4dd0fd05311d031e2810e56aedba122552febd87e3046e82560ef0bfd3ada10",
    "schema_v14_chat_from_v13.sql": "6d286d0fbeff56876e4ec4a56e5e7f49ba45f6773c27214d8316f4b2001dd0d6",
    "schema_v14_rules.sql": "277c17b07104b0c3ac28dbad7b746ca2318100c65bfea90fb0c1568794d5d656",
    "schema_v14_rules_from_v13.sql": "3ed369a1daea20109875b0dcc1ced06ef6f2eddc23777b13b2cf8a885f61c964",
}

TS, TS_DECIDED, TS_DELETED, TS_RESTORED = ("2026-08-01T09:00:00Z", "2026-08-02T10:30:00Z", "2026-08-03T11:00:00Z",
                                           "2026-08-04T12:00:00Z")
TURN = "a1b2c3d4e5f60718"          # an Ask turn still running (chat.py: secrets.token_hex(8)), holding its cards
RECEIPT_USER = "SynthReceiptUserRule-000000000001"       # the Undo of a deleted user rule (rows t2, t3; t3 changed since)
RECEIPT_AI = "SynthReceiptAiRule-00000000000002"         # of a deleted AI rule (row t4)
RECEIPT_RESTORED = "SynthReceiptRestored-000000000003"   # of a delete already undone (rule 5 is back)

# What 15 adds to each lane's 14. Every other object and column is an original, compared as it was.
ADDED_COLUMNS = {   # (table, column): (type, notnull, default as declared, the value every existing row gets)
    "chat": {("classifications", "detached_by"): ("INTEGER", 0, None, None)},
    "rules": {("pending_actions", "origin"): ("TEXT", 1, "'mcp'", "mcp"),
              ("pending_actions", "held_by"): ("TEXT", 0, None, None)},
}
ADDED_FOREIGN_KEYS = {"chat": {("rule_deletions", "detached_by", "id", "NO ACTION", "NO ACTION", "NONE")}, "rules": set()}
ADDED_OBJECTS = {"chat": {("table", "rule_deletions"), ("trigger", "trg_classifications_detached_changed")},
                 "rules": set()}
CHANGED_OBJECTS = {"chat": {("table", "classifications"), ("view", "v_transactions")},   # v_transactions: reads receipts
                   "rules": {("table", "pending_actions")}}
NONE = (set(), set(), set())
# connect() now goes on to 16 (polish P0), the same on either lane: category_groups (filled), categories' group_id / icon /
# sort (filled by the step, not left at a default), the 23 new categories and the new Plaid codes, and the car-payment
# code moved from Transport to Car Payment. Apart from those, every row and value is compared as it was.
V16_OBJECTS = {("table", "category_groups")}
V16_COLUMNS = {("categories", "group_id"): ("INTEGER", 0, None), ("categories", "icon"): ("TEXT", 0, None),
               ("categories", "sort"): ("INTEGER", 0, None)}
V17_COLUMNS = {("goals", "icon"): ("TEXT", 0, None), ("goals", "colour"): ("TEXT", 0, None)}
V16_FOREIGN_KEYS = {("category_groups", "group_id", "id", "NO ACTION", "NO ACTION", "NONE")}
# and on to 18 (polish P2e): two new tables, empty (no group budget, no kept group)
V18_OBJECTS = {("table", "group_budgets"), ("table", "group_budget_kept")}
V16_NEW_ROWS = {"categories": ("name", {name for name, _, _ in db.DEFAULT_CATEGORIES[13:]}),
                "plaid_category_map": ("pfc_code", set(db.DEFAULT_PFC_MAP))}
V16_MOVED = {("plaid_category_map", "LOAN_PAYMENTS_CAR_PAYMENT"): "category_id"}
# and on to 19 (category names): three categories renamed in place, by id (the same rows, so every rule, classification
# and map row follows); the group Other -> Misc is not seen here, as a 14 has no groups. No row is filed as 'plaid'.
V19_RENAMED = {"Transport": "Other Transport", "Shopping": "Other Shopping", "Health": "Other Health"}

# The rules lane's 14 as its commit declares it (562beb6's db.py; see the parent diff): spelled out, not read from db
RULE_DELETIONS_COLUMNS = [
    ("id", "INTEGER", 0, None, 1), ("receipt_hash", "TEXT", 1, None, 0), ("rule_id", "INTEGER", 1, None, 0),
    ("match_field", "TEXT", 1, None, 0), ("match_type", "TEXT", 1, None, 0), ("pattern", "TEXT", 1, None, 0),
    ("category_id", "INTEGER", 1, None, 0), ("priority", "INTEGER", 1, None, 0), ("created_from", "TEXT", 0, None, 0),
    ("created_at", "TEXT", 1, None, 0), ("filed", "INTEGER", 1, None, 0), ("deleted_at", "TEXT", 1, None, 0),
    ("restored_at", "TEXT", 0, None, 0), ("restored_rule_id", "INTEGER", 0, None, 0)]
DETACHED_TRIGGER = """CREATE TRIGGER trg_classifications_detached_changed
AFTER UPDATE OF category_id, source, rule_id ON classifications
WHEN NEW.detached_by IS NOT NULL AND NEW.detached_by IS OLD.detached_by
BEGIN
    UPDATE classifications SET detached_by = NULL WHERE transaction_id = NEW.transaction_id;
END"""


# --- the inputs: a pinned 14 with synthetic rows ----------------------------------------------------------------------

def _insert(raw: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    for row in rows:
        raw.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def _populate(raw: sqlite3.Connection, lane: str) -> None:
    """Rows a user of that lane's build could have: the lane's own (Ask cards and holds / receipts and detached rows)
    and the rest of the data, with REAL, negative, BLOB, NULL, JSON and non-ASCII values. Written with plain SQL, as
    that parent's code left them (classify.delete_rule and mcp_state.record_pending are unchanged since the parents)."""
    raw.execute("INSERT INTO categories (name, monthly_cap) VALUES ('Pets', 55.5)")
    raw.execute("UPDATE categories SET monthly_cap = 400 WHERE name = 'Groceries'")
    cat = dict(raw.execute("SELECT name, id FROM categories"))
    chat = lane == "chat"
    _insert(raw, "items", [
        dict(item_id="item_a", institution_id="ins_synth_1", institution_name="First Synthetic Bank",
             access_token="access-sandbox-synthetic-item-a", cursor="cursor-a-0001", status="active",
             products='["transactions", "liabilities"]', txn_update_status="HISTORICAL_UPDATE_COMPLETE", created_at=TS,
             last_sync_at=TS_DECIDED),
        dict(item_id="item_b", institution_id="ins_synth_2", institution_name="Second Synthetic CU",
             access_token="access-sandbox-synthetic-item-b", status="login_required", error_code="ITEM_LOGIN_REQUIRED",
             error_message="the login details changed", created_at=TS, removed_at=TS_DELETED)])
    _insert(raw, "accounts", [
        dict(account_id="acc_chk", item_id="item_a", name="Synthetic Checking", official_name="Everyday Checking",
             mask="0001", type="depository", subtype="checking", current_balance=1000.25, available_balance=900.0,
             iso_currency="USD", updated_at=TS),
        dict(account_id="acc_card", item_id="item_a", name="Synthetic Card", mask="0002", type="credit",
             subtype="credit card", current_balance=250.0, credit_limit=5000.0, iso_currency="USD", updated_at=TS)])
    _insert(raw, "manual_accounts", [
        dict(id=1, kind="loan", name="Synthetic Car Loan", institution="Synthetic Lender", subtype="auto", balance=7000.0,
             interest_rate=4.9, original_amount=12000.0, monthly_payment=310.5, match_pattern="SYNTH LENDER",
             balance_as_of="2026-08-01", created_at=TS, updated_at=TS),
        dict(id=2, kind="vehicle", name="Synthmobile S1", balance=15000.0, vehicle_make="Synthmobile", vehicle_model="S1",
             vehicle_year=2020, balance_as_of="2026-08-01", created_at=TS, updated_at=TS, value_source="user",
             vehicle_condition="good", linked_loan_id=1),
        dict(id=3, kind="bank", name="Synthetic Credit Union", institution="Synthetic CU", subtype="checking",
             balance=250.0, balance_as_of="2026-08-01", created_at=TS, updated_at=TS)])
    _insert(raw, "imports", [dict(id=1, created_at=TS, filename="synthetic.ofx", kind="ofx", manual_account_id=3,
                                  rows_new=1, rows_skipped=0)])
    txn = dict(account_id="acc_chk", item_id="item_a", date="2026-08-02", first_seen_at=TS, updated_at=TS)
    _insert(raw, "transactions", [
        {**txn, "transaction_id": "t1", "name": "SYNTH GROCER", "amount": 42.5, "pfc_primary": "FOOD_AND_DRINK",
         "pfc_detailed": "FOOD_AND_DRINK_GROCERIES", "raw_json": '{"synthetic": true}'},
        {**txn, "transaction_id": "t2", "account_id": "acc_card", "name": "SYNTH COFFEE 0012",
         "merchant_name": "Synth Coffee", "amount": 4.75, "pfc_primary": "FOOD_AND_DRINK"},
        {**txn, "transaction_id": "t3", "account_id": "acc_card", "date": "2026-08-03", "name": "SYNTH COFFEE 0013",
         "merchant_name": "Synth Coffee", "amount": 5.25, "pfc_primary": "FOOD_AND_DRINK"},
        {**txn, "transaction_id": "t4", "name": "SYNTH TAXI", "amount": 18.0, "pfc_primary": "TRANSPORTATION"},
        {**txn, "transaction_id": "t5", "name": "PAYROLL SYNTH", "amount": -3000.0, "pfc_primary": "INCOME"},
        {**txn, "transaction_id": "t6", "name": "O'Brien's Café ☕", "merchant_name": "O'Brien's", "amount": 12.34,
         "pending": 1, "location_json": '{"city": "Nowhere"}'},
        {**txn, "transaction_id": "t7", "name": "SYNTH GYM", "amount": 30.0, "pfc_primary": "PERSONAL_CARE"},
        {**txn, "transaction_id": "t8", "name": "SYNTH STREAM 42", "amount": 9.99, "pfc_primary": "ENTERTAINMENT"},
        {**txn, "transaction_id": "t9", "name": "SYNTH REMOVED", "amount": 1.0, "removed": 1},
        {**txn, "transaction_id": "imp1", "account_id": "manual:3", "item_id": "import", "name": "CORNER SYNTH",
         "amount": 8.75, "source": "import", "import_id": 1, "fitid": "FIT-0001"}])
    rule = dict(match_type="exact", created_at=TS)
    _insert(raw, "rules", [
        {**rule, "id": 1, "match_field": "name", "pattern": "SYNTH GROCER", "category_id": cat["Groceries"],
         "priority": 200, "created_from": "user"},
        {**rule, "id": 2, "match_field": "name", "match_type": "regex", "pattern": "^SYNTH STREAM",
         "category_id": cat["Subscriptions"], "priority": 100, "created_from": "ai"},
        {**rule, "id": 5, "match_field": "name", "pattern": "SYNTH GYM", "category_id": cat["Health"], "priority": 200,
         "created_from": "user"}]
        + ([{**rule, "id": 3, "match_field": "merchant_name", "pattern": "Synth Coffee", "category_id": cat["Eating Out"],
             "priority": 200, "created_from": "user"},
            {**rule, "id": 4, "match_field": "name", "pattern": "SYNTH TAXI", "category_id": cat["Transport"],
             "priority": 100, "created_from": "ai"}] if chat else []))
    if not chat:   # rules 3 and 4 deleted with receipts; rule 5 deleted and restored
        deleted = dict(match_type="exact", created_at=TS, deleted_at=TS_DELETED)
        _insert(raw, "rule_deletions", [
            {**deleted, "id": 1, "receipt_hash": _receipt_hash(RECEIPT_USER), "rule_id": 3,
             "match_field": "merchant_name", "pattern": "Synth Coffee", "category_id": cat["Eating Out"], "priority": 200, "created_from": "user",
             "filed": 2},
            {**deleted, "id": 2, "receipt_hash": _receipt_hash(RECEIPT_AI), "rule_id": 4, "match_field": "name",
             "pattern": "SYNTH TAXI", "category_id": cat["Transport"], "priority": 100, "created_from": "ai", "filed": 1},
            {**deleted, "id": 3, "receipt_hash": _receipt_hash(RECEIPT_RESTORED), "rule_id": 5, "match_field": "name",
             "pattern": "SYNTH GYM", "category_id": cat["Health"], "priority": 200, "created_from": "user", "filed": 1,
             "restored_at": TS_RESTORED, "restored_rule_id": 5}])
    filed = dict(source="rule", updated_at=TS)
    by_deleted = ([{**filed, "transaction_id": "t2", "category_id": cat["Eating Out"], "note": "rule #3",
                    "detached_by": 1, "updated_at": TS_DECIDED},
                   {"transaction_id": "t3", "category_id": cat["Shopping"], "source": "user", "updated_at": TS_RESTORED},
                   {**filed, "transaction_id": "t4", "category_id": cat["Transport"], "note": "rule #4", "detached_by": 2,
                    "updated_at": TS_DECIDED}] if not chat else
                  [{**filed, "transaction_id": t, "category_id": cat["Eating Out"], "rule_id": 3, "note": "rule #3",
                    "updated_at": TS_DECIDED} for t in ("t2", "t3")]
                  + [{**filed, "transaction_id": "t4", "category_id": cat["Transport"], "rule_id": 4, "note": "rule #4",
                      "updated_at": TS_DECIDED}])
    _insert(raw, "classifications", [
        {**filed, "transaction_id": "t1", "category_id": cat["Groceries"], "rule_id": 1, "note": "rule #1"},
        {"transaction_id": "t5", "category_id": cat["Income"], "source": "user", "note": "payday", "updated_at": TS},
        {"transaction_id": "t6", "category_id": cat["Eating Out"], "source": "ai", "confidence": 0.87, "note": "a cafe",
         "updated_at": TS},
        {**filed, "transaction_id": "t7", "category_id": cat["Health"], "rule_id": 5, "note": "rule #5"},
        {**filed, "transaction_id": "t8", "category_id": cat["Subscriptions"], "rule_id": 2, "note": "rule #2"},
        *by_deleted])
    _insert(raw, "ai_runs", [dict(run_id="ai-20260801-090000", created_at=TS, applied_at=TS, queued_groups=2,
                                  queued_txns=2, summary_json='{"changed": 0, "flagged": 2}')])
    _insert(raw, "ai_reviews", [
        dict(transaction_id="t2", run_id="ai-20260801-090000", reviewed_at=TS, verdict="flagged",
             category_id=cat["Eating Out"], note="coffee or groceries?"),
        dict(transaction_id="t4", run_id="ai-20260801-090000", reviewed_at=TS, verdict="flagged",
             category_id=cat["Transport"], note="taxi or transfer?")])
    mcp = dict(origin="mcp", held_by=None) if chat else {}
    _insert(raw, "pending_actions", [
        dict(id=1, tool="finance_set_cap", arguments='{"category": "Groceries", "monthly_cap": 450}',
             summary="Groceries cap to $450", client="synthetic-cli", status="pending", created_at=TS, **mcp),
        dict(id=2, tool="finance_add_rule", arguments='{"match_field": "name", "pattern": "SYNTH GROCER"}',
             summary="Rule: SYNTH GROCER -> Groceries", status="applied", created_at=TS, decided_at=TS_DECIDED,
             result='{"rule_id": 1}', **mcp),
        dict(id=3, tool="finance_set_category", arguments='{"transaction_id": "t8", "category": "Subscriptions"}',
             summary="SYNTH STREAM 42 to Subscriptions", client="synthetic-cli", status="rejected", created_at=TS,
             decided_at=TS_DECIDED, **mcp)]
        + ([dict(id=4, tool="finance_set_category", arguments='{"transaction_id": "t6", "category": "Eating Out"}',
                 summary="O'Brien's Café ☕ to Eating Out", client="Ask", status="pending", created_at=TS_DECIDED,
                 origin="ask", held_by=TURN),
            dict(id=5, tool="finance_track_bill", arguments='{"transaction_id": "t8"}',
                 summary="Track SYNTH STREAM as a bill", client="Ask", status="pending", created_at=TS_DECIDED,
                 origin="ask", held_by=TURN),
            dict(id=6, tool="finance_set_cap", arguments='{"category": "Eating Out", "monthly_cap": 120}',
                 summary="Eating Out cap to $120", client="Ask", status="applied", created_at=TS, decided_at=TS_DECIDED,
                 result='{"monthly_cap": 120}', origin="ask", held_by=None),
            dict(id=7, tool="finance_add_rule", arguments='{"match_field": "name", "pattern": "SYNTH TAXI"}',
                 summary="Rule: SYNTH TAXI -> Transport", client="Ask", status="rejected", created_at=TS,
                 decided_at=TS_DECIDED, result='{"withdrawn": "the Ask answer that suggested it failed"}', origin="ask",
                 held_by=None),
            dict(id=8, tool="finance_set_cap", arguments='{"category": "Pets", "monthly_cap": 60}',
                 summary="Pets cap to $60", client="Ask", status="pending", created_at=TS, origin="mcp",   # a client
                 held_by=None)] if chat else []))                                                     # named Ask
    _insert(raw, "settings", [dict(key="networth.mode", value='"custom"', updated_at=TS),
                              dict(key="mcp.allow_writes", value="true", updated_at=TS),
                              dict(key="chart_excluded_categories", value='["Transfers"]', updated_at=TS)])
    _insert(raw, "institutions", [dict(institution_id="ins_synth_1", name="First Synthetic Bank",
                                       logo_png=b"\x89PNG\r\n\x1a\n" + bytes(range(32)), logo_source="site",
                                       primary_color="#123456", url="https://bank.invalid", fetched_at=TS)])
    _insert(raw, "security_logos", [dict(ticker="SYNTH", png=None, source="none", fetched_at=TS)])
    _insert(raw, "balance_snapshots", [dict(account_id="acc_chk", date="2026-08-01", current=1000.25, available=900.0,
                                            iso_currency="USD")])
    _insert(raw, "tracked_bills", [dict(id=1, merchant_key="synth stream", merchant="SYNTH STREAM", cadence="monthly",
                                        variable=0, usual_low=9.99, usual_high=9.99, anchor_date="2026-08-02",
                                        anchor_amount=9.99, anchor_transaction_id="t8", created_at=TS, updated_at=TS)])
    _insert(raw, "transaction_notes", [dict(transaction_id="t1", note="weekly shop", updated_at=TS)])
    _insert(raw, "tags", [dict(id=1, name="household", created_at=TS)])
    _insert(raw, "transaction_tags", [dict(transaction_id="t1", tag_id=1)])
    _insert(raw, "goals", [dict(id=1, kind="save", name="Synthetic trip", target=1500.0, target_month="2027-06",
                                track="manual", created_at=TS, updated_at=TS)])
    _insert(raw, "goal_entries", [dict(id=1, goal_id=1, date="2026-08-02", amount=200.0, note="first", created_at=TS)])


def _receipt_hash(receipt: str) -> str:
    """What classify.receipt_hash stores for a receipt (sha256 hex), spelled out: the inputs never use the module."""
    return hashlib.sha256(receipt.encode()).hexdigest()


def _v14(path: Path, lane: str, from_v13: bool) -> sqlite3.Connection:
    """A database at 14 as that parent left it (its pinned fixture), with the synthetic rows. The lane's own data is
    really there: Ask cards and live holds (chat), receipts and rows still detached (rules)."""
    config.ensure_data_dir(path.parent)
    raw = sqlite3.connect(path)
    raw.executescript(parents.fixture(lane, from_v13).read_text())
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 14
    _populate(raw, lane)
    raw.commit()
    if lane == "chat":
        assert raw.execute("SELECT id, origin, held_by FROM pending_actions WHERE origin = 'ask' OR held_by IS NOT NULL "
                           "ORDER BY id").fetchall() == [(4, "ask", TURN), (5, "ask", TURN), (6, "ask", None),
                                                         (7, "ask", None)]
    else:
        assert raw.execute("SELECT COUNT(*), COUNT(restored_at) FROM rule_deletions").fetchone() == (3, 1)
        assert raw.execute("SELECT transaction_id, detached_by FROM classifications WHERE detached_by IS NOT NULL "
                           "ORDER BY 1").fetchall() == [("t2", 1), ("t4", 2)]
    return raw


# --- what is compared -------------------------------------------------------------------------------------------------

def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                       "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]


def _snapshot(conn: sqlite3.Connection) -> dict[str, dict[int, dict[str, tuple[str, object]]]]:
    """Every row of every table, by rowid: {column: (its type, its value)}, so 1 and 1.0, or '1' and 1, differ."""
    out = {}
    for table in _tables(conn):
        cur = conn.execute(f'SELECT rowid, * FROM "{table}" ORDER BY rowid')
        cols = [d[0] for d in cur.description[1:]]
        out[table] = {r[0]: {c: (type(v).__name__, v) for c, v in zip(cols, tuple(r)[1:])} for r in cur}
    return out


def _assert_kept(before: dict, after: dict, lane: str, where: str = "after the upgrade") -> None:
    """Every original table keeps exactly its rows (same rowids, none added) with every original value; each column 15
    adds holds its default on every one of them; a table 15 adds is empty."""
    new_tables = {name for kind, name in ADDED_OBJECTS[lane] if kind == "table"}
    v16_tables = {name for _, name in V16_OBJECTS}
    v18_tables = {name for _, name in V18_OBJECTS}
    assert set(after) == set(before) | new_tables | v16_tables | v18_tables, f"{where}: tables lost or unexpected"
    assert all(after[t] == {} for t in new_tables), f"{where}: a table 15 adds has rows"
    assert all(after[t] == {} for t in v18_tables), f"{where}: a table 18 adds has rows"
    assert all(after[t] for t in v16_tables), f"{where}: a table 16 adds and fills is empty"
    for table, rows in before.items():
        if table in V16_NEW_ROWS:                # 16's own rows, by their key; every original row is still there
            key, allowed = V16_NEW_ROWS[table]
            assert after[table].keys() >= rows.keys(), f"{where}: {table} lost rows"
            assert {after[table][r][key][1] for r in after[table].keys() - rows.keys()} <= allowed, \
                f"{where}: {table} gained rows 16 doesn't add"
        else:
            assert after[table].keys() == rows.keys(), f"{where}: {table} lost or gained rows"
        added = {col: (type(value).__name__, value)
                 for (t, col), (*_, value) in ADDED_COLUMNS[lane].items() if t == table}
        for rowid, row in rows.items():
            now = after[table][rowid]
            if table == "goals":
                assert now["icon"] == ("str", "pencil")
                assert now["colour"] == ("str", "#4fd1a5")
            moved = {c for (t, k), c in V16_MOVED.items() if t == table and k in {v for _, v in row.values()}}
            kept = {c: v for c, v in row.items() if c not in moved}
            if table == "categories":
                kept["name"] = ("str", V19_RENAMED.get(row["name"][1], row["name"][1]))
            assert {c: now[c] for c in row if c not in moved} == kept, \
                f"{where}: {table} rowid {rowid}: an original value changed"
            assert {c: v for c, v in now.items() if c not in row and (table, c) not in V16_COLUMNS | V17_COLUMNS} == added, \
                f"{where}: {table} rowid {rowid}: the columns 15 adds are not at their defaults"


def _fresh(directory: Path) -> dict:
    """The definition of a new database at 15."""
    conn = db.connect(directory / "fresh.db")
    try:
        return parents.definition(conn)
    finally:
        conn.close()


def _assert_converged(conn: sqlite3.Connection, lane: str, before: dict, fresh: dict) -> None:
    """Exactly a new database's schema, reached from that lane's 14 by additions only: the objects and columns 15 adds,
    each as 15 declares it, after the original columns (kept in order, as they were declared)."""
    after = parents.definition(conn)
    assert parents.changes(fresh, after) == NONE, "not a new database's schema: (extra, missing, different) objects"
    assert after == fresh
    assert parents.changes(before, after) == (ADDED_OBJECTS[lane] | V16_OBJECTS | V18_OBJECTS, set(),
                                              CHANGED_OBJECTS[lane] | {("table", "categories"), ("table", "goals")})
    columns = {**{k: v[:3] for k, v in ADDED_COLUMNS[lane].items()}, **V16_COLUMNS, **V17_COLUMNS}
    for kind, table in CHANGED_OBJECTS[lane] | {("table", "categories"), ("table", "goals")}:
        if kind != "table":
            continue
        old, new = before[(kind, table)], after[(kind, table)]
        assert new["columns"][:len(old["columns"])] == old["columns"]
        assert [c[1:6] for c in new["columns"][len(old["columns"]):]] == \
            [(col, typ, notnull, default, 0) for (t, col), (typ, notnull, default) in columns.items() if t == table]
        assert {fk[2:] for fk in new["foreign_keys"]} - {fk[2:] for fk in old["foreign_keys"]} == \
            {fk for fk in ADDED_FOREIGN_KEYS[lane] | V16_FOREIGN_KEYS if (table, fk[1]) in columns}
        assert {fk[2:] for fk in old["foreign_keys"]} <= {fk[2:] for fk in new["foreign_keys"]}
        assert new["indexes"] == old["indexes"]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def _assert_lane_data_works(conn: sqlite3.Connection, path: Path, lane: str) -> None:
    """On the upgraded database: held cards stay held until their turn releases them; the rules lane's receipts protect
    what a deleted user rule filed, restore once (only rows still as the delete left them), and stay hidden from
    read-only connections. A chat-lane database gets working receipts of its own."""
    flagged = dict(conn.execute("SELECT transaction_id, ai_flagged FROM v_transactions "
                                "WHERE transaction_id IN ('t2', 't4')"))
    assert flagged == {"t2": 0, "t4": 1}               # a (deleted) user rule's row isn't flagged; an AI rule's still is
    if lane == "chat":
        assert [a["id"] for a in mcp_state.pending(conn)] == [1, 8] and mcp_state.pending_count(conn) == 2
        assert [(a["origin"], a["held"], a["status"]) for i in (4, 5) for a in mcp_state.pending(conn, i)] == \
            [("ask", True, "pending")] * 2
        assert [(a["id"], a["held"]) for a in mcp_state.release(conn, TURN)] == [(4, False), (5, False)]
        assert [a["id"] for a in mcp_state.pending(conn)] == [1, 4, 5, 8]
        out = rulebook.delete(conn, 3)
        assert out["filed"] == ["t2", "t3"]
        assert classify.set_category(conn, "t2", "Travel", "ai") == "kept-user-rule"
        back = rulebook.restore(conn, out["undo"])
        assert (back["rule"]["id"], back["rule"]["source"], back["relinked"]) == (3, "user", 2)
    else:
        assert [(a["id"], a["origin"], a["held"]) for a in mcp_state.pending(conn, status=None)] == \
            [(1, "mcp", False), (2, "mcp", False), (3, "mcp", False)]
        assert classify.set_category(conn, "t2", "Travel", "ai") == "kept-user-rule"   # the deleted user rule's row
        assert classify.set_category(conn, "t4", "Travel", "ai") == "set"              # the deleted AI rule's: open
        assert conn.execute("SELECT detached_by FROM classifications WHERE transaction_id = 't4'").fetchone()[0] is None
        back = rulebook.restore(conn, {"receipt": RECEIPT_USER})
        assert (back["rule"]["id"], back["rule"]["source"], back["relinked"]) == (3, "user", 1)   # t2; t3 changed since
        assert [tuple(r) for r in conn.execute("SELECT transaction_id, source, rule_id, detached_by, note FROM "
                                               "classifications WHERE transaction_id IN ('t2', 't3') ORDER BY 1")] == \
            [("t2", "rule", 3, None, "rule #3"), ("t3", "user", None, None, None)]
        for receipt, status in ((RECEIPT_USER, 409), (RECEIPT_RESTORED, 409), ("SynthReceiptNeverIssued", 404)):
            with pytest.raises(rulebook.RuleError) as refused:
                rulebook.restore(conn, {"receipt": receipt})
            assert refused.value.status == status, receipt
        back = rulebook.restore(conn, {"receipt": RECEIPT_AI})
        assert (back["rule"]["id"], back["rule"]["source"], back["relinked"]) == (4, "ai", 0)     # t4 changed since
    ro = db.connect_readonly(path)
    try:
        assert {r[0] for r in ro.execute("SELECT receipt_hash FROM rule_deletions")} == {None}
    finally:
        ro.close()


def _must_not_run(conn):
    raise AssertionError("init_schema ran on an ordinary reopen of a database at 15")


# --- G2: the pinned inputs --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("lane", "from_v13"), VARIANTS, ids=IDS)
def test_each_pinned_v14_is_that_parents_schema(lane, from_v13):
    """No Git needed: the fixture is byte for byte the pinned one, and its difference from the v13 fixture is exactly
    that parent's 14 as its commit declares it (spelled out above), with that parent's own views. A new database and
    the v13 upgrade reach the same definition."""
    path = parents.fixture(lane, from_v13)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == FIXTURE_SHA256[path.name]
    v14 = parents.loaded(path)
    assert v14.execute("PRAGMA user_version").fetchone()[0] == 14
    before, after = parents.definition(parents.loaded(parents.V13_FIXTURE)), parents.definition(v14)
    if lane == "chat":            # pending_actions.origin, held_by; the v13 views as they were
        assert parents.changes(before, after) == (set(), set(), {("table", "pending_actions")})
        old, new = before[("table", "pending_actions")], after[("table", "pending_actions")]
        assert new["columns"] == old["columns"] + [(9, "origin", "TEXT", 1, "'mcp'", 0, 0),
                                                   (10, "held_by", "TEXT", 0, None, 0, 0)]
        assert (new["foreign_keys"], new["indexes"]) == (old["foreign_keys"], old["indexes"])
    else:                         # rule_deletions, its trigger, classifications.detached_by, receipt-aware views
        assert parents.changes(before, after) == (
            {("table", "rule_deletions"), ("trigger", "trg_classifications_detached_changed")}, set(),
            {("table", "classifications"), ("view", "v_transactions")})
        old, new = before[("table", "classifications")], after[("table", "classifications")]
        assert new["columns"] == old["columns"] + [(7, "detached_by", "INTEGER", 0, None, 0, 0)]
        assert {fk[2:] for fk in new["foreign_keys"]} == {fk[2:] for fk in old["foreign_keys"]} | \
            {("rule_deletions", "detached_by", "id", "NO ACTION", "NO ACTION", "NONE")}
        receipts = after[("table", "rule_deletions")]
        assert [c[1:6] for c in receipts["columns"]] == RULE_DELETIONS_COLUMNS
        assert [fk[2:5] for fk in receipts["foreign_keys"]] == [("categories", "category_id", "id")]
        assert [(unique, origin, [c[2] for c in cols if c[5]]) for unique, origin, _, cols in
                receipts["indexes"].values()] == [(1, "u", ["receipt_hash"])]
        assert after[("trigger", "trg_classifications_detached_changed")]["sql"] == parents.normalized(DETACHED_TRIGGER)
        view = after[("view", "v_transactions")]["sql"]
        assert parents.normalized("LEFT JOIN rule_deletions dl ON dl.id = c.detached_by AND c.rule_id IS NULL") in view
        assert parents.normalized("COALESCE(ru.created_from, dl.created_from) = 'user'") in view
    assert after == parents.definition(parents.loaded(parents.fixture(lane, not from_v13)))


def test_the_pinned_fixtures_are_what_the_parent_commits_give():
    """Rebuilt from the Git objects alone (each parent's db.py by blob id, its SQL literals replayed in its init_schema's
    order), every fixture has exactly the stored SQL (comments included) and rows; the same replay of 6b89dd5 gives the
    v13 fixture made before any of this. (`tests/schema14_parents.py --check` compares the bytes too.)"""
    if not parents.objects_available():
        pytest.skip("the parents' Git objects are not in this checkout (a git archive); the sha256 pins still hold")
    for lane, from_v13 in VARIANTS:
        path = parents.fixture(lane, from_v13)
        assert parents.exact(parents.loaded(path)) == parents.exact(parents.replayed(parents.PARENTS[lane], from_v13)), \
            path.name
    assert parents.v13_fixture_matches_its_commit()


# --- G1: every row and value, the upgrade and an ordinary reopen ------------------------------------------------------

@pytest.mark.parametrize(("lane", "from_v13"), VARIANTS, ids=IDS)
def test_a_v14_from_either_lane_converges_on_15_with_every_row_and_value(data_dir, monkeypatch, lane, from_v13):
    """One connect() takes that lane's 14 to 15: a new database's definition, reached by additions only, every original
    row and value kept, 15's columns at their defaults. Reopened (an ordinary reopen: no init_schema), nothing changes,
    and the held cards and receipts do what they are for."""
    path = config.db_path()
    raw = _v14(path, lane, from_v13)
    before, before_def = _snapshot(raw), parents.definition(raw)
    raw.close()
    fresh = _fresh(data_dir)

    conn = db.connect(path)                     # 14 < 15: the cumulative step runs
    _assert_converged(conn, lane, before_def, fresh)
    upgraded = _snapshot(conn)
    _assert_kept(before, upgraded, lane)
    conn.close()

    with monkeypatch.context() as m:            # an ordinary reopen: at 15, connect() runs no init_schema at all
        m.setattr(db, "init_schema", _must_not_run)
        conn = db.connect(path)
    assert parents.definition(conn) == fresh and _snapshot(conn) == upgraded
    _assert_lane_data_works(conn, path, lane)
    conn.close()


# --- G3: a complete schema restamped, and genuine partial installs ----------------------------------------------------

@pytest.mark.parametrize(("lane", "from_v13"), VARIANTS, ids=IDS)
def test_a_complete_15_restamped_14_replays_init_schema_and_changes_nothing(data_dir, monkeypatch, lane, from_v13):
    """Not a partial install: every object is there and only the stamp says 14 (the number both lanes used), so connect()
    runs init_schema over a complete schema, and so does a direct call at 15. Definitions and rows stay as they were."""
    path = config.db_path()
    _v14(path, lane, from_v13).close()
    conn = db.connect(path)
    upgraded, upgraded_def = _snapshot(conn), parents.definition(conn)
    with conn:
        conn.execute("PRAGMA user_version = 14")
    conn.close()
    runs, real = [], db.init_schema

    def counted(c):
        runs.append(c)
        return real(c)

    with monkeypatch.context() as m:
        m.setattr(db, "init_schema", counted)
        conn = db.connect(path)
    assert len(runs) == 1, "connect() did not run init_schema on a database stamped 14"
    assert parents.definition(conn) == upgraded_def and _snapshot(conn) == upgraded
    db.init_schema(conn)
    assert parents.definition(conn) == upgraded_def and _snapshot(conn) == upgraded
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


_SCHEMA_ACTIONS = {sqlite3.SQLITE_CREATE_TABLE: "create table", sqlite3.SQLITE_CREATE_INDEX: "create index",
                   sqlite3.SQLITE_CREATE_TRIGGER: "create trigger", sqlite3.SQLITE_CREATE_VIEW: "create view",
                   sqlite3.SQLITE_DROP_TABLE: "drop table", sqlite3.SQLITE_DROP_VIEW: "drop view",
                   sqlite3.SQLITE_DROP_INDEX: "drop index", sqlite3.SQLITE_DROP_TRIGGER: "drop trigger"}


def _schema_statement(action: int, arg1: str | None, arg2: str | None) -> tuple[str, str] | None:
    """A statement that changes the schema or the stamp, as SQLite's authorizer sees it: ('create trigger', name),
    ('alter table', table), ('stamp', '15'); None for anything else (reads, row writes, SQLite's own nested steps)."""
    if action in _SCHEMA_ACTIONS:
        return _SCHEMA_ACTIONS[action], arg1
    if action == sqlite3.SQLITE_ALTER_TABLE:
        return "alter table", arg2
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "user_version" and arg2 is not None:
        return "stamp", arg2
    return None


def _run_init_schema(path: Path, stop_at: int | None) -> list[tuple[str, str]]:
    """The real db.init_schema on `path`, handed a connection as connect() hands it one (WAL, Row), with SQLite refusing
    its `stop_at`-th schema statement (0-based; None: none): a crash just before that statement. Each earlier one stays
    done (DDL commits as it runs; the row writes and the stamp roll back together). Returns the statements it reached."""
    reached: list[tuple[str, str]] = []

    def authorizer(action, arg1, arg2, _db, _trigger):
        step = _schema_statement(action, arg1, arg2)
        if step is None:
            return sqlite3.SQLITE_OK
        reached.append(step)
        return sqlite3.SQLITE_DENY if len(reached) - 1 == stop_at else sqlite3.SQLITE_OK

    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.set_authorizer(authorizer)
        if stop_at is None:
            db.init_schema(conn)
        else:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                db.init_schema(conn)
    finally:
        conn.close()
    return reached


def _left(conn: sqlite3.Connection) -> dict:
    """What an interrupted run left, in the terms that matter for 14 -> 15."""
    objects = {(r[0], r[1]): r[2] for r in conn.execute("SELECT type, name, sql FROM sqlite_master")}
    cols = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in ("classifications", "pending_actions")}
    view = objects.get(("view", "v_transactions"))
    try:
        conn.execute("SELECT COUNT(*) FROM v_spend_lines").fetchone()
        spend_lines = True
    except sqlite3.OperationalError:            # it reads v_transactions, dropped and not made again yet
        spend_lines = False
    return {"stamp": conn.execute("PRAGMA user_version").fetchone()[0],
            "detached_by": "detached_by" in cols["classifications"],
            "origin": "origin" in cols["pending_actions"], "held_by": "held_by" in cols["pending_actions"],
            "rule_deletions": ("table", "rule_deletions") in objects,
            "trigger": ("trigger", "trg_classifications_detached_changed") in objects,
            "v_transactions": None if view is None else ("reads receipts" if "rule_deletions" in view else "no receipts"),
            "v_monthly_category_spend": ("view", "v_monthly_category_spend") in objects,
            "v_spend_lines_readable": spend_lines}


# The schema statements each lane's 14 is missing, in the order a 14 -> 15 run reaches them (among the others it replays)
LANE_STEPS = {"chat": [("alter table", "classifications"), ("create table", "rule_deletions"),
                       ("create trigger", "trg_classifications_detached_changed"), ("create view", "v_transactions")],
              "rules": [("alter table", "pending_actions"), ("alter table", "pending_actions"),
                        ("create view", "v_transactions")]}
_WHOLE = dict(stamp=14, detached_by=True, origin=True, held_by=True, rule_deletions=True, trigger=True,
              v_transactions="reads receipts", v_monthly_category_spend=True, v_spend_lines_readable=True)
# (lane, the schema statement SQLite refuses (its n-th time), what the interrupted run must have left)
INTERRUPTIONS = {
    "chat-after-detached_by-before-rule_deletions": ("chat", ("create table", "rule_deletions"), 1,
                                                     {**_WHOLE, "rule_deletions": False, "trigger": False,
                                                      "v_transactions": "no receipts"}),
    "chat-after-rule_deletions-before-its-trigger": ("chat", ("create trigger", "trg_classifications_detached_changed"),
                                                     1, {**_WHOLE, "trigger": False, "v_transactions": "no receipts"}),
    "chat-views-dropped-not-made-again": ("chat", ("create view", "v_transactions"), 1,
                                          {**_WHOLE, "v_transactions": None, "v_monthly_category_spend": False,
                                           "v_spend_lines_readable": False}),
    "chat-every-step-but-the-stamp": ("chat", ("stamp", None), 1, _WHOLE),
    "rules-after-origin-before-held_by": ("rules", ("alter table", "pending_actions"), 2, {**_WHOLE, "held_by": False}),
    "rules-views-dropped-not-made-again": ("rules", ("create view", "v_transactions"), 1,
                                           {**_WHOLE, "v_transactions": None, "v_monthly_category_spend": False,
                                            "v_spend_lines_readable": False}),
    "rules-every-step-but-the-stamp": ("rules", ("stamp", None), 1, _WHOLE),
}


@pytest.mark.parametrize("case", list(INTERRUPTIONS))
def test_an_interrupted_upgrade_is_finished_by_the_next_connect(data_dir, tmp_path, case):
    """A 14 -> 15 run (on each lane's new-database 14) stopped at a chosen schema statement leaves a genuine partial
    install, checked and still stamped 14; the next connect() finishes it: a new database's definition, every original
    row and value, both lanes' data working."""
    lane, (action, name), nth, expected = INTERRUPTIONS[case]
    name = str(db.SCHEMA_VERSION) if action == "stamp" else name
    path = config.db_path()
    raw = _v14(path, lane, False)
    before, before_def = _snapshot(raw), parents.definition(raw)
    raw.close()
    fresh = _fresh(data_dir)
    shutil.copyfile(path, tmp_path / "probe.db")
    steps = _run_init_schema(tmp_path / "probe.db", None)           # the whole run on a copy: where that statement is
    stop_at = [i for i, s in enumerate(steps) if s == (action, name)][nth - 1]

    assert _run_init_schema(path, stop_at) == steps[:stop_at + 1]
    raw = sqlite3.connect(path)
    assert _left(raw) == expected
    raw.close()

    conn = db.connect(path)
    _assert_converged(conn, lane, before_def, fresh)
    _assert_kept(before, _snapshot(conn), lane)
    _assert_lane_data_works(conn, path, lane)
    conn.close()


@pytest.mark.parametrize(("lane", "from_v13"), VARIANTS, ids=IDS)
def test_every_schema_statement_boundary_of_the_upgrade_converges(data_dir, tmp_path, lane, from_v13):
    """Stopped just before each schema statement of the 14 -> 15 run in turn (every CREATE / DROP / ALTER it reaches, and
    the stamp), the database is still at 14 and the next connect() gives a new database's definition with every original
    row and value. Only statement boundaries: nothing torn inside a statement, no disk, WAL or concurrency fault."""
    pristine = tmp_path / "v14.db"
    raw = _v14(pristine, lane, from_v13)
    before = _snapshot(raw)
    raw.close()
    fresh = _fresh(data_dir)
    shutil.copyfile(pristine, tmp_path / "whole.db")
    steps = _run_init_schema(tmp_path / "whole.db", None)
    remaining = iter(steps)                                  # the lane's missing DDL is among them, in this order
    assert all(step in remaining for step in LANE_STEPS[lane]) and steps[-1] == ("stamp", str(db.SCHEMA_VERSION))
    cut, states = tmp_path / "cut.db", set()
    for k, step in enumerate(steps):
        for leftover in (cut, cut.with_name("cut.db-wal"), cut.with_name("cut.db-shm")):
            leftover.unlink(missing_ok=True)
        shutil.copyfile(pristine, cut)
        assert _run_init_schema(cut, k) == steps[:k + 1], f"#{k} {step}: the run is not the same each time"
        raw = sqlite3.connect(cut)
        left = _left(raw)
        raw.close()
        where = f"stopped before #{k} {step}"
        assert left["stamp"] == 14, where
        states.add(tuple(sorted(left.items())))
        conn = db.connect(cut)
        assert parents.changes(fresh, parents.definition(conn)) == NONE, f"{where}: the next connect() did not converge"
        _assert_kept(before, _snapshot(conn), lane, where)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION, where
        conn.close()
    assert len(states) >= (4 if lane == "chat" else 3)   # distinct partial installs, not one state over and over
