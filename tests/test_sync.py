"""/transactions/sync handling: pagination, modified/removed, mutation restart, item errors."""

from __future__ import annotations

import json

from conftest import TOKEN_A, TOKEN_B, account, add_item, err, item_row, page, txn
from hermes_finance import cli
from hermes_finance.sync import sync_all

SYNC = "/transactions/sync"


def txn_row(conn, tid):
    return conn.execute("SELECT * FROM transactions WHERE transaction_id=?", (tid,)).fetchone()


def test_pagination_added_modified_removed(conn, client, fake):
    add_item(conn)
    pages = {
        None: page(added=[txn("t1", 10), txn("t2", 20), txn("t3", 30)], next_cursor="c1", has_more=True),
        "c1": page(added=[txn("t4", 40)], modified=[txn("t1", 11.5)], removed=["t2"], next_cursor="c2"),
        "c2": page(next_cursor="c2"),
    }
    fake.on(SYNC, lambda b: pages[b.get("cursor")])

    (r,) = sync_all(conn, client)

    assert r.ok and (r.added, r.modified, r.removed) == (4, 1, 1)
    assert [b.get("cursor") for b in fake.bodies(SYNC)] == [None, "c1"]
    assert txn_row(conn, "t1")["amount"] == 11.5
    assert txn_row(conn, "t2")["removed"] == 1          # kept, just flagged
    assert txn_row(conn, "t4")["removed"] == 0
    assert item_row(conn)["cursor"] == "c2"
    visible = {row[0] for row in conn.execute("SELECT transaction_id FROM v_transactions")}
    assert visible == {"t1", "t3", "t4"}
    log = conn.execute("SELECT added, modified, removed, error FROM sync_log").fetchone()
    assert tuple(log) == (4, 1, 1, None)

    # next run resumes from the stored cursor
    (r2,) = sync_all(conn, client)
    assert r2.ok and (r2.added, r2.modified, r2.removed) == (0, 0, 0)
    assert fake.bodies(SYNC)[-1]["cursor"] == "c2"
    assert fake.bodies(SYNC)[-1]["access_token"] == TOKEN_A


def test_mutation_during_pagination_restarts_from_saved_cursor(conn, client, fake):
    add_item(conn, cursor="c0")
    calls = []

    def handler(body):
        cursor = body.get("cursor")
        calls.append(cursor)
        n = len(calls)
        if n == 1:
            return page(added=[txn("ghost", 99)], next_cursor="c1", has_more=True)
        if n == 2:
            return err("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "TRANSACTIONS_ERROR")
        if n == 3:
            return page(added=[txn("t1", 10)], next_cursor="c1b", has_more=True)
        return page(added=[txn("t2", 20)], next_cursor="c2")

    fake.on(SYNC, handler)
    (r,) = sync_all(conn, client)

    assert r.ok
    assert calls == ["c0", "c1", "c0", "c1b"]
    ids = {row[0] for row in conn.execute("SELECT transaction_id FROM transactions")}
    assert ids == {"t1", "t2"}  # pages from the aborted pass were discarded
    assert item_row(conn)["cursor"] == "c2"


def test_mutation_retries_are_bounded(conn, client, fake):
    add_item(conn, cursor="c0")
    fake.on(SYNC, lambda b: err("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "TRANSACTIONS_ERROR"))
    (r,) = sync_all(conn, client)
    assert not r.ok and r.error_code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
    assert item_row(conn)["cursor"] == "c0"  # untouched


def test_item_login_required_recorded_not_raised(conn, client, fake, capsys, cli_client):
    add_item(conn, "item_a", "ins_1", token=TOKEN_A, cursor="c0")
    fake.on(SYNC, err("ITEM_LOGIN_REQUIRED"))

    (r,) = sync_all(conn, client)

    assert not r.ok and r.error_code == "ITEM_LOGIN_REQUIRED"
    row = item_row(conn)
    assert row["status"] == "login_required" and row["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert row["cursor"] == "c0"
    assert "ITEM_LOGIN_REQUIRED" in conn.execute("SELECT error FROM sync_log").fetchone()[0]

    # CLI: the only item failed -> total failure -> non-zero, but no traceback
    assert cli.main(["sync"]) == 1
    out = capsys.readouterr().out
    assert "ERROR ITEM_LOGIN_REQUIRED" in out and "Reconnect" in out


def test_one_failing_item_does_not_fail_the_run(conn, fake, capsys, cli_client):
    add_item(conn, "item_a", "ins_1", token=TOKEN_A)
    add_item(conn, "item_b", "ins_2", name="Amex", token=TOKEN_B)
    fake.on(SYNC, lambda b: err("ITEM_LOGIN_REQUIRED") if b["access_token"] == TOKEN_A
            else page(added=[txn("tb1", 5)], next_cursor="cb"))

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "Chase: ERROR ITEM_LOGIN_REQUIRED" in out
    assert "Amex: +1 ~0 -0 | balances ok" in out
    assert "synced 1/2" in out
    assert item_row(conn, "item_b")["status"] == "active"


def test_successful_sync_clears_previous_error(conn, client):
    add_item(conn, status="login_required")
    with conn:
        conn.execute("UPDATE items SET error_code='ITEM_LOGIN_REQUIRED'")
    (r,) = sync_all(conn, client)
    assert r.ok
    assert item_row(conn)["status"] == "active" and item_row(conn)["error_code"] is None


def test_removed_items_are_not_synced(conn, client, fake):
    add_item(conn, removed=True)
    assert sync_all(conn, client) == []
    assert fake.paths() == []


def test_balances_refreshed_from_accounts_get(conn, client, fake):
    add_item(conn)
    fake.on("/accounts/get", {"accounts": [account(current=1234.56, available=1200.0),
                                           account("acc_cc", "Card", "9999", "credit", "credit card",
                                                   current=250.0, available=4750.0, limit=5000.0)]})
    (r,) = sync_all(conn, client)
    assert r.extras["balances"] == "ok"
    rows = {a["account_id"]: a for a in conn.execute("SELECT * FROM accounts")}
    assert rows["acc_chk"]["current_balance"] == 1234.56
    assert rows["acc_cc"]["credit_limit"] == 5000.0 and rows["acc_cc"]["mask"] == "9999"


def test_optional_products_pulled_and_errors_tolerated(conn, client, fake):
    add_item(conn)
    fake.on("/item/get", {"item": {"products": ["transactions", "liabilities", "investments"]}})
    fake.on("/liabilities/get", {
        "accounts": [account("acc_cc", "Card", "9999", "credit", "credit card", 250.0, 4750.0, 5000.0)],
        "liabilities": {"credit": [{"account_id": "acc_cc", "last_statement_balance": 200.0,
                                    "minimum_payment_amount": 25.0, "next_payment_due_date": "2026-10-10",
                                    "is_overdue": False, "aprs": [{"apr_type": "purchase_apr",
                                                                   "apr_percentage": 24.99}]}],
                        "mortgage": None, "student": None}})
    fake.on("/investments/holdings/get", err("PRODUCTS_NOT_SUPPORTED", "ITEM_ERROR"))

    (r,) = sync_all(conn, client)

    assert r.ok
    assert r.extras == {"balances": "ok", "liabilities": "ok", "investments": "skipped (PRODUCTS_NOT_SUPPORTED)"}
    liab = conn.execute("SELECT * FROM liabilities").fetchone()
    assert (liab["kind"], liab["last_statement_balance"], liab["interest_rate"]) == ("credit", 200.0, 24.99)
    assert item_row(conn)["status"] == "active"
    assert json.loads(item_row(conn)["products"]) == ["investments", "liabilities", "transactions"]


def test_holdings_snapshot(conn, client, fake):
    add_item(conn)
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on("/investments/holdings/get", {
        "accounts": [account("acc_inv", "Brokerage", "5555", "investment", "brokerage", 5000.0, None)],
        "holdings": [{"account_id": "acc_inv", "security_id": "sec_1", "quantity": 10,
                      "institution_price": 100.0, "institution_value": 1000.0, "cost_basis": 800.0,
                      "iso_currency_code": "USD"}],
        "securities": [{"security_id": "sec_1", "ticker_symbol": "VTI", "name": "Vanguard Total", "type": "etf"}]})
    (r,) = sync_all(conn, client)
    assert r.extras["investments"] == "ok"
    h = conn.execute("SELECT * FROM holdings").fetchone()
    assert (h["ticker_symbol"], h["quantity"], h["institution_value"]) == ("VTI", 10, 1000.0)


def test_optional_products_not_called_when_not_granted(conn, client, fake):
    add_item(conn)
    sync_all(conn, client)
    assert "/liabilities/get" not in fake.paths()
    assert "/investments/holdings/get" not in fake.paths()


def test_product_not_ready_is_not_an_item_error(conn, client, fake):
    add_item(conn)
    fake.on(SYNC, err("PRODUCT_NOT_READY"))
    (r,) = sync_all(conn, client)
    assert r.ok and "NOT_READY" in r.summary()
    assert item_row(conn)["status"] == "active"


def test_network_error_recorded(conn, client, fake):
    add_item(conn)

    def boom(body):
        raise OSError("connection reset")

    fake.on(SYNC, boom)
    (r,) = sync_all(conn, client)
    assert not r.ok and r.error_code == "NETWORK_ERROR"
    # TASK-22c: logged, but a passing error leaves the Item's saved status alone (it was "error")
    assert item_row(conn)["status"] == "active" and item_row(conn)["error_code"] is None
    assert conn.execute("SELECT error FROM sync_log").fetchone()[0].startswith("NETWORK_ERROR: ")


def test_pending_classification_carries_over_to_posted(conn, client, fake):
    from hermes_finance import classify
    add_item(conn)
    fake.on(SYNC, page(added=[txn("p1", 42, pending=True)], next_cursor="c1"))
    sync_all(conn, client)
    classify.set_category(conn, "p1", "Other Health", "user")

    fake.on(SYNC, page(added=[txn("t1", 42, pending_id="p1")], removed=["p1"], next_cursor="c2"))
    sync_all(conn, client)

    c = conn.execute("SELECT c.source, cat.name FROM classifications c JOIN categories cat ON cat.id=c.category_id "
                     "WHERE transaction_id='t1'").fetchone()
    assert tuple(c) == ("user", "Other Health")


def test_sync_output_is_concise(conn, fake, capsys, cli_client):
    add_item(conn)
    fake.on(SYNC, page(added=[txn("t1")], next_cursor="c1"))
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["Chase: +1 ~0 -0 | balances ok", "synced 1/1 item(s)",
                   "what changed: 1 new transaction"]                   # (TASK-22; a new account's balance didn't move)
