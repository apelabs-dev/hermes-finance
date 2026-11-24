"""Schema v25: pdf_templates, imports.kind gains 'pdf', a v24 database upgrades with every row kept."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_finance import db, tooldefs

V24 = (Path(__file__).parent / "fixtures" / "schema_v24.sql").read_text()


def _rows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    out = {}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for table in tables:
        if table == "pdf_templates":
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        out[table] = [tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid")]
    return out


def _master(conn: sqlite3.Connection) -> list[tuple]:
    return sorted(tuple(r) for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))


def _v24_db(path: Path) -> None:
    raw = sqlite3.connect(path)
    raw.executescript(V24)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 24
    raw.close()


def test_v24_upgrades_to_v25_and_keeps_every_row(tmp_path):
    path = tmp_path / "v24.db"
    _v24_db(path)
    raw = sqlite3.connect(path)
    before = _rows(raw)
    assert before["auth_users"] and before["accounts"] and before["transactions"] and before["settings"]
    assert before["imports"] and before["manual_accounts"]
    assert before["imports"] == [(1, "2026-11-01T00:00:00Z", "fictional.csv", "csv", 1, 2, 0)]
    assert raw.execute("SELECT colour FROM categories WHERE name = 'Groceries'").fetchone()[0] == "#4fd1a5"
    raw.close()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 25
    after = _rows(conn)
    assert after == before                       # every table, every row, every id: the rebuild keeps them
    assert conn.execute("SELECT COUNT(*) FROM pdf_templates").fetchone()[0] == 0
    assert conn.execute("SELECT kind FROM imports").fetchone()[0] == "csv"
    assert conn.execute("SELECT import_id FROM transactions WHERE transaction_id = 't1'").fetchone()[0] == 1
    # v24's category colour is not the PDF step's business: it is still there
    assert conn.execute("SELECT colour FROM categories WHERE name = 'Groceries'").fetchone()[0] == "#4fd1a5"


def test_pdf_kind_is_allowed_and_init_schema_twice_changes_nothing(tmp_path):
    path = tmp_path / "v24b.db"
    _v24_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    conn.execute("INSERT INTO imports (created_at, filename, kind, manual_account_id, rows_new, rows_skipped) "
                 "VALUES ('2026-11-01T00:00:00Z', 'statement.pdf', 'pdf', 1, 3, 0)")
    conn.commit()
    before = _rows(conn)
    master = _master(conn)
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 25
    assert _rows(conn) == before
    assert _master(conn) == master
    assert conn.execute("SELECT COUNT(*) FROM imports WHERE kind = 'pdf'").fetchone()[0] == 1


def test_new_database_has_the_table_and_accepts_pdf_imports(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 25
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pdf_templates)")}
        assert cols == {"id", "layout_id", "fingerprint", "config", "source", "created_at"}
        conn.execute("INSERT INTO manual_accounts (kind, name, balance, balance_as_of, created_at, updated_at) "
                     "VALUES ('bank', 'Checking', 0, '2026-11-01', '2026-11-01T00:00:00Z', "
                     "'2026-11-01T00:00:00Z')")
        conn.execute("INSERT INTO imports (created_at, filename, kind, manual_account_id, rows_new, rows_skipped) "
                     "VALUES ('2026-11-01T00:00:00Z', 'statement.pdf', 'pdf', 1, 3, 0)")
        conn.commit()
    finally:
        conn.close()


def test_an_upgraded_v24_database_has_exactly_a_fresh_v25_schema(tmp_path):
    """sqlite_master after v24 -> v25 is byte-for-byte a new database's: the rebuilt imports keeps the fresh CREATE text
    (no quoted name left by a RENAME), every object is there, and transactions.import_id still points at imports."""
    path = tmp_path / "v24.db"
    _v24_db(path)
    upgraded = db.connect(path)
    fresh = db.connect(tmp_path / "fresh.db")
    try:
        assert _master(upgraded) == _master(fresh)
        assert upgraded.execute("SELECT sql FROM sqlite_master WHERE name = 'imports'").fetchone()[0].startswith(
            "CREATE TABLE imports (")
        fks = [(r[2], r[3], r[4]) for r in upgraded.execute("PRAGMA foreign_key_list(transactions)")]
        assert ("imports", "import_id", "id") in fks
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
        db.init_schema(upgraded)   # a second run changes nothing
        assert _master(upgraded) == _master(fresh)
    finally:
        upgraded.close()
        fresh.close()


def test_a_read_only_connection_never_upgrades_a_v24_database(tmp_path):
    path = tmp_path / "v24.db"
    _v24_db(path)
    with pytest.raises(tooldefs.ToolError) as refused:
        tooldefs.read_connection(path)
    assert "schema v24" in str(refused.value) and f"need v{db.SCHEMA_VERSION}" in str(refused.value)
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 24
        assert raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'pdf_templates'").fetchone()[0] == 0
        assert "'pdf'" not in raw.execute("SELECT sql FROM sqlite_master WHERE name = 'imports'").fetchone()[0]
    finally:
        raw.close()


def test_a_failed_rebuild_leaves_the_old_imports_as_it_was(tmp_path, monkeypatch):
    """An interrupted v25 step: the rebuild fails inside its savepoint, so imports is the v24 table with its row, and a
    later run finishes the job."""
    path = tmp_path / "v24.db"
    _v24_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(db, "IMPORTS_TABLE", "CREATE TABLE imports (broken")
    with pytest.raises(sqlite3.OperationalError):
        db._v25_pdf(conn)
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
    assert "'pdf'" not in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'imports'").fetchone()[0]
    monkeypatch.undo()
    db._v25_pdf(conn)
    assert "'pdf'" in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'imports'").fetchone()[0]
    assert [tuple(r) for r in conn.execute("SELECT * FROM imports")] == \
        [(1, "2026-11-01T00:00:00Z", "fictional.csv", "csv", 1, 2, 0)]
    conn.close()
