"""`finance query` is read-only and never shows tokens; export works."""

from __future__ import annotations

import csv
import json

import pytest

from conftest import TOKEN_A, add_item, page, txn
from hermes_finance import cli, db
from hermes_finance.sync import sync_all


@pytest.fixture
def populated(conn, client, fake):
    add_item(conn)
    fake.on("/transactions/sync", page(added=[txn("t1", 12.5, date="2026-08-01"),
                                              txn("t2", 7.0, date="2026-09-01")], next_cursor="c1"))
    sync_all(conn, client)
    return conn


def snapshot(conn):
    return [tuple(r) for r in conn.execute("SELECT transaction_id, amount, removed FROM transactions ORDER BY 1")] + \
        [tuple(r) for r in conn.execute("SELECT name, monthly_cap FROM categories ORDER BY 1")]


@pytest.mark.parametrize("sql", [
    "DELETE FROM transactions",
    "UPDATE categories SET monthly_cap = 1",
    "INSERT INTO categories (name) VALUES ('Hacked')",
    "DROP TABLE transactions",
    "CREATE TABLE evil (x)",
    "PRAGMA user_version = 99",
    "ATTACH DATABASE '/tmp/hermes-finance-evil.db' AS evil",
    "PRAGMA query_only = OFF",
])
def test_query_is_read_only(populated, capsys, sql):
    before = snapshot(populated)
    assert cli.main(["query", sql]) == 1
    assert "error:" in capsys.readouterr().err
    assert snapshot(populated) == before
    assert populated.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_readonly_connection_is_ro_even_without_authorizer(populated):
    ro = db.connect_readonly()
    ro.set_authorizer(None)
    ro.execute("PRAGMA query_only = OFF")
    with pytest.raises(Exception, match="readonly"):
        ro.execute("DELETE FROM transactions")


def test_query_select_table_and_json(populated, capsys):
    assert cli.main(["query", "SELECT transaction_id, spend, category FROM v_transactions ORDER BY date"]) == 0
    out = capsys.readouterr().out
    assert "t1" in out and "Coffee" in out and "(2 rows)" in out   # Blue Bottle: Coffee from v16
    assert cli.main(["query", "SELECT month, SUM(spend) AS s FROM v_transactions GROUP BY month", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"month": "2026-08", "s": 12.5}, {"month": "2026-09", "s": 7.0}]


def test_query_hides_access_tokens(populated, capsys):
    assert cli.main(["query", "SELECT * FROM items", "--json"]) == 0
    out = capsys.readouterr().out
    assert TOKEN_A not in out
    assert json.loads(out)[0]["access_token"] is None
    assert cli.main(["query", "SELECT group_concat(access_token) FROM items"]) == 0
    assert TOKEN_A not in capsys.readouterr().out


def test_query_introspection_allowed(populated, capsys):
    assert cli.main(["query", "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"]) == 0
    out = capsys.readouterr().out
    assert "v_transactions" in out and "v_monthly_category_spend" in out
    assert cli.main(["query", "PRAGMA table_info(transactions)"]) == 0


def test_export_csv_and_json(populated, capsys, tmp_path):
    path = tmp_path / "out.csv"
    assert cli.main(["export", "--since", "2026-08-15", "--csv", str(path)]) == 0
    rows = list(csv.DictReader(path.open()))
    assert [r["transaction_id"] for r in rows] == ["t2"]
    assert rows[0]["category"] == "Coffee" and rows[0]["amount"] == "7.0"
    capsys.readouterr()
    assert cli.main(["export", "--since", "2026-01-01"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert cli.main(["export", "--since", "08/01/2026"]) == 2
