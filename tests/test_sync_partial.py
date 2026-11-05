"""Partial product success keeps what actually landed (issue #11)."""

from __future__ import annotations

from conftest import account, add_item, err, item_row, page, txn
from hermes_finance import classify, cli, sync, syncnow

SYNC = "/transactions/sync"


def partial_item(conn, fake, cursor=None):
    add_item(conn, products=("transactions", "investments"), cursor=cursor)
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on(SYNC, page(added=[txn("new")], next_cursor="c1"))
    fake.on("/investments/holdings/get", err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))


def test_partial_transactions_commit_is_kept_and_cursor_advances(conn, client, fake):
    partial_item(conn, fake, cursor="c0")

    (r,) = sync.sync_all(conn, client)

    assert not r.ok and r.error_code == "INTERNAL_SERVER_ERROR"
    assert r.transactions_ok and (r.added, r.modified, r.removed) == (1, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE transaction_id='new'").fetchone()[0] == 1
    assert item_row(conn)["cursor"] == "c1"
    log = conn.execute("SELECT added, error FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    assert log["added"] == 1 and log["error"].startswith("INTERNAL_SERVER_ERROR: ")


def test_partial_transactions_still_apply_rules(conn, client, fake):
    classify.add_rule(conn, "merchant_name", "Blue Bottle", "Transfers")
    partial_item(conn, fake)

    (r,) = sync.sync_all(conn, client)

    row = conn.execute("SELECT category, category_source, is_transfer FROM v_transactions "
                       "WHERE transaction_id='new'").fetchone()
    assert tuple(row) == ("Transfers", "rule", 1)
    assert not r.ok


def test_partial_changes_count_what_landed(conn, client, fake):
    partial_item(conn, fake)

    (r,) = sync.sync_all(conn, client)

    c = syncnow.changes({}, {}, [r])
    assert (c["new"], c["updated"], c["removed"]) == (1, 0, 0)
    assert c["text"] == "1 new transaction"
    assert syncnow.failure([r]) == {"unreachable": False, "lines": ["Couldn't sync Chase."]}


def test_partial_page_payload_is_honest(conn, client, fake):
    classify.add_rule(conn, "merchant_name", "Blue Bottle", "Transfers")
    partial_item(conn, fake)

    out = syncnow.run(conn, lambda: client)

    assert out["unreachable"] is False
    assert out["ok"] is False
    assert out["changes"]["new"] == 1
    assert out["changes"]["text"] == "1 new transaction"
    assert out["items"][0]["added"] == 1 and out["items"][0]["error"] is not None
    assert "Nothing changed" not in out["message"]
    assert out["changes"]["new_more"] == {"spend": 0, "transfers": 1, "money_in": 0}


def test_partial_cli_reports_the_saved_transaction(conn, fake, cli_client, capsys):
    partial_item(conn, fake)

    rc = cli.main(["sync"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "Chase: ERROR INTERNAL_SERVER_ERROR" in out
    assert "what changed: 1 new transaction" in out
    assert "Couldn't sync Chase." in out
    assert "Nothing changed" not in out
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_fully_successful_transactions_and_investments_is_the_control(conn, client, fake):
    add_item(conn, products=("transactions", "investments"))
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on(SYNC, page(added=[txn("new")], next_cursor="c1"))
    fake.on("/investments/holdings/get", {
        "accounts": [account("acc_inv", "Brokerage", "5555", "investment", "brokerage", 5000.0, None)],
        "holdings": [], "securities": []})

    (r,) = sync.sync_all(conn, client)

    assert r.ok and r.transactions_ok and r.added == 1
    assert item_row(conn)["cursor"] == "c1"
    assert syncnow.failure([r]) is None
    c = syncnow.changes({}, {}, [r])
    assert (c["new"], c["text"]) == (1, "1 new transaction")


def test_holdings_parser_crash_is_not_a_success(conn, client, fake):
    add_item(conn, products=("investments",), cursor="saved")
    fake.on("/item/get", {"item": {"products": ["investments"]}})
    fake.on("/investments/holdings/get", {"accounts": [], "securities": [{}], "holdings": []})

    (r,) = sync.sync_all(conn, client)

    assert not r.ok and r.error_code == "INTERNAL_ERROR"
    assert r.extras["investments"] == "err (KeyError)"
    assert conn.execute("SELECT error FROM sync_log").fetchone()[0].startswith("INTERNAL_ERROR: KeyError")
    row = item_row(conn)
    assert row["last_sync_at"] is None and row["cursor"] == "saved"


def test_failed_balance_only_sync_never_refreshes_stale_history(conn, client, fake):
    add_item(conn, products=("balance",), cursor="saved", status="error")
    sync.upsert_accounts(conn, "item_a", [account(current=123.0)])
    sync.snapshot_balances(conn, "item_a", "2000-01-01")
    fake.on("/item/get", {"item": {"products": ["balance"]}})
    fake.on("/accounts/get", err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))

    (r,) = sync.sync_all(conn, client)

    assert not r.ok and r.error_code == "INTERNAL_SERVER_ERROR"
    assert [row[0] for row in conn.execute("SELECT date FROM balance_snapshots ORDER BY date")] == ["2000-01-01"]
    row = item_row(conn)
    assert row["last_sync_at"] is None and row["status"] == "error"
    assert conn.execute("SELECT error FROM sync_log").fetchone()[0].startswith("INTERNAL_SERVER_ERROR: ")


def unavailable_investments(conn, fake):
    add_item(conn, products=("investments",), cursor="saved", status="error")
    sync.upsert_accounts(conn, "item_a", [account(current=123.0)])
    sync.snapshot_balances(conn, "item_a", "2000-01-01")
    fake.on("/item/get", {"item": {"products": ["investments"]}})
    fake.on("/accounts/get", err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))
    fake.on("/investments/holdings/get", err("PRODUCT_NOT_READY"))


def test_no_data_investments_run_is_not_reported_successful(conn, client, fake):
    unavailable_investments(conn, fake)

    out = syncnow.run(conn, lambda: client)

    row, log = item_row(conn), conn.execute("SELECT error FROM sync_log").fetchone()
    assert out["ok"] is False and out["synced"] == 0
    assert log["error"] is not None
    assert row["cursor"] == "saved" and row["last_sync_at"] is None
    assert [r[0] for r in conn.execute("SELECT date FROM balance_snapshots")] == ["2000-01-01"]


def test_no_data_investments_cli_is_not_successful(conn, fake, cli_client, capsys):
    unavailable_investments(conn, fake)

    rc = cli.main(["sync"])
    out = capsys.readouterr().out

    assert rc != 0
    assert "synced 1/1" not in out
    assert "what changed: Nothing new" not in out


def not_ready_only_item(conn, fake):
    add_item(conn, products=("investments",), cursor="saved", status="error")
    fake.on("/item/get", {"item": {"products": ["investments"]}})
    fake.on("/accounts/get", err("PRODUCT_NOT_READY"))
    fake.on("/investments/holdings/get", err("PRODUCT_NOT_READY"))


def test_not_ready_only_item_reports_not_ready(conn, client, cli_client, fake, capsys):
    not_ready_only_item(conn, fake)

    (r,) = sync.sync_all(conn, client)

    assert not r.ok
    assert r.error_code == "PRODUCT_NOT_READY"
    log = conn.execute("SELECT error FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    assert log["error"].startswith("PRODUCT_NOT_READY")
    assert "not in the Item's products" not in log["error"]

    out = syncnow.run(conn, lambda: client)
    assert out["items"][0]["error_code"] == "PRODUCT_NOT_READY"
    assert "not in the Item's products" not in out["items"][0]["error"]

    rc = cli.main(["sync"])
    output = capsys.readouterr().out
    error_lines = [line for line in output.splitlines() if "ERROR" in line]
    assert rc == 1
    assert error_lines and "ERROR PRODUCT_NOT_READY" in error_lines[0]
    assert "not in the Item's products" not in error_lines[0]


def test_holdings_account_balance_is_snapshotted(conn, client, fake):
    add_item(conn, products=("investments",), cursor="saved")
    fake.on("/item/get", {"item": {"products": ["investments"]}})
    fake.on("/accounts/get", err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))
    fake.on("/investments/holdings/get", {
        "accounts": [account("acc_inv", "Brokerage", "5555", "investment", "brokerage", 5000.0, None)],
        "securities": [], "holdings": []})

    (r,) = sync.sync_all(conn, client)

    snapshots = [(row["account_id"], row["current"]) for row in conn.execute(
        "SELECT account_id, current FROM balance_snapshots")]
    assert r.ok and snapshots == [("acc_inv", 5000.0)]


def test_liabilities_account_balance_is_snapshotted(conn, client, fake):
    add_item(conn, products=("liabilities",), cursor="saved")
    fake.on("/item/get", {"item": {"products": ["liabilities"]}})
    fake.on("/accounts/get", err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))
    fake.on("/liabilities/get", {
        "accounts": [account("acc_cc", "Card", "9999", "credit", "credit card", 250.0, 4750.0, 5000.0)],
        "liabilities": {"credit": [], "mortgage": None, "student": None}})

    (r,) = sync.sync_all(conn, client)

    snapshots = [(row["account_id"], row["current"]) for row in conn.execute(
        "SELECT account_id, current FROM balance_snapshots")]
    assert r.ok and snapshots == [("acc_cc", 250.0)]


def test_committed_transactions_tolerate_an_unsupported_optional_product(conn, client, fake):
    add_item(conn, products=("transactions", "investments"))
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on(SYNC, page(added=[txn("new")], next_cursor="c1"))
    fake.on("/investments/holdings/get", err("PRODUCTS_NOT_SUPPORTED"))

    (r,) = sync.sync_all(conn, client)

    assert r.ok and r.transactions_ok


def test_transaction_accounts_still_snapshot_and_mark_fresh(conn, client, fake):
    add_item(conn)
    fake.on(SYNC, page(added=[txn("new")], accounts=[account(current=321.0)]))
    fake.on("/accounts/get", err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))

    (r,) = sync.sync_all(conn, client)

    assert r.ok and r.transactions_ok
    assert conn.execute("SELECT current FROM balance_snapshots WHERE account_id='acc_chk'").fetchone()[0] == 321.0
    row = item_row(conn)
    assert row["last_sync_at"] is not None and row["status"] == "active"
