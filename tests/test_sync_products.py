"""The products an Item actually granted decide which sync calls run (issue #11)."""

from __future__ import annotations

import json

import pytest

from conftest import TOKEN_A, TOKEN_B, account, add_item, err, item_row, page, txn
from hermes_finance import syncnow
from hermes_finance.sync import sync_all

SYNC = "/transactions/sync"


def holdings_route(value=1000.0):
    return {
        "accounts": [account("acc_inv", "Brokerage", "5555", "investment", "brokerage", 5000.0, None)],
        "holdings": [{"account_id": "acc_inv", "security_id": "sec_1", "quantity": 10,
                      "institution_price": value / 10, "institution_value": value, "cost_basis": 800.0,
                      "iso_currency_code": "USD"}],
        "securities": [{"security_id": "sec_1", "ticker_symbol": "VTI", "name": "Vanguard Total", "type": "etf"}],
    }


def test_investments_only_never_calls_transactions(conn, client, fake):
    add_item(conn, products=("investments",))
    fake.on("/item/get", {"item": {"products": ["investments"]}})
    fake.on("/investments/holdings/get", holdings_route())

    def boom(body):
        raise AssertionError("/transactions/sync called for an investments-only Item")

    fake.on(SYNC, boom)

    (r,) = sync_all(conn, client)

    assert r.ok
    holding = conn.execute("SELECT * FROM holdings").fetchone()
    assert (holding["ticker_symbol"], holding["quantity"], holding["institution_value"]) == ("VTI", 10, 1000.0)
    snapshot = conn.execute("SELECT * FROM holding_snapshots").fetchone()
    assert (snapshot["account_id"], snapshot["security_id"], snapshot["ticker"], snapshot["quantity"],
            snapshot["value"]) == ("acc_inv", "sec_1", "VTI", 10, 1000.0)
    balances = {row["account_id"] for row in conn.execute("SELECT * FROM balance_snapshots")}
    assert balances == {"acc_chk", "acc_inv"}
    assert fake.paths() == ["/item/get", "/accounts/get", "/investments/holdings/get"]
    assert SYNC not in fake.paths()
    row = item_row(conn)
    assert row["cursor"] is None
    assert row["last_sync_at"] is not None
    assert row["status"] == "active" and row["error_code"] is None
    assert r.extras == {"balances": "ok", "investments": "ok"}
    assert r.summary() == "Chase: no transactions (not in the Item's products) | balances ok | investments ok"
    assert "+0 ~0 -0" not in r.summary()
    assert "/link/token/create" not in fake.paths()
    assert "/item/public_token/exchange" not in fake.paths()


def test_existing_cursor_is_preserved(conn, client, fake):
    add_item(conn, products=("investments",), cursor="c0")
    fake.on("/item/get", {"item": {"products": ["investments"]}})
    fake.on("/investments/holdings/get", holdings_route())

    (r,) = sync_all(conn, client)

    assert r.ok and item_row(conn)["cursor"] == "c0"
    assert SYNC not in fake.paths()


def test_transactions_only_call_order_is_pinned(conn, client, fake):
    add_item(conn)
    fake.on(SYNC, page(added=[txn("t1")]))

    (r,) = sync_all(conn, client)

    assert fake.paths() == ["/item/get", SYNC, "/accounts/get"]
    assert (r.added, r.modified, r.removed) == (1, 0, 0)
    assert r.transactions_skipped is None
    assert r.summary() == "Chase: +1 ~0 -0 | balances ok"
    assert "/liabilities/get" not in fake.paths()
    assert "/investments/holdings/get" not in fake.paths()


def test_transactions_and_investments_both_run(conn, client, fake):
    add_item(conn, products=("transactions", "investments"))
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on(SYNC, page(added=[txn("t1")]))
    fake.on("/investments/holdings/get", holdings_route())

    (r,) = sync_all(conn, client)

    assert r.ok and r.added == 1 and r.transactions_skipped is None
    assert SYNC in fake.paths() and "/investments/holdings/get" in fake.paths()
    assert r.extras == {"balances": "ok", "investments": "ok"}
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 1


def test_liabilities_only_skips_transactions_and_investments(conn, client, fake):
    add_item(conn, products=("liabilities",))
    fake.on("/item/get", {"item": {"products": ["liabilities"]}})
    fake.on("/liabilities/get", {
        "accounts": [account("acc_cc", "Card", "9999", "credit", "credit card", 250.0, 4750.0, 5000.0)],
        "liabilities": {"credit": [{"account_id": "acc_cc", "last_statement_balance": 200.0,
                                    "minimum_payment_amount": 25.0, "next_payment_due_date": "2026-10-10",
                                    "is_overdue": False, "aprs": [{"apr_type": "purchase_apr",
                                                                   "apr_percentage": 24.99}]}],
                        "mortgage": None, "student": None}})

    (r,) = sync_all(conn, client)

    assert r.ok
    assert SYNC not in fake.paths()
    assert "/liabilities/get" in fake.paths()
    assert r.extras == {"balances": "ok", "liabilities": "ok"}
    assert "investments" not in r.extras
    liabilities = conn.execute("SELECT * FROM liabilities").fetchone()
    assert liabilities["kind"] == "credit" and liabilities["last_statement_balance"] == 200.0


def test_item_get_failure_keeps_the_stored_products(conn, client, fake):
    add_item(conn, products=("investments",))
    fake.on("/item/get", err("API_ERROR", "API_ERROR"))
    fake.on("/investments/holdings/get", holdings_route())

    (r,) = sync_all(conn, client)

    assert r.ok
    assert SYNC not in fake.paths()
    assert "/investments/holdings/get" in fake.paths()
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 1
    assert json.loads(item_row(conn)["products"]) == ["investments"]


def test_item_get_failure_with_unknown_products_fails_closed(conn, client, fake):
    add_item(conn, products=(), status="error")
    fake.on("/item/get", err("API_ERROR", "API_ERROR"))

    (r,) = sync_all(conn, client)

    assert not r.ok
    assert r.error_code == "ITEM_PRODUCTS_UNKNOWN"
    assert "products" in r.error_message
    assert fake.paths() == ["/item/get"]
    row = item_row(conn)
    assert row["status"] == "error"
    assert row["last_sync_at"] is None


def test_investments_error_is_not_hidden(conn, client, fake):
    add_item(conn, products=("transactions", "investments"))
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on(SYNC, page(added=[txn("t1")]))
    fake.on("/investments/holdings/get", err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))

    (r,) = sync_all(conn, client)

    assert not r.ok
    assert r.error_code == "INTERNAL_SERVER_ERROR"
    assert r.extras["investments"] == "err (INTERNAL_SERVER_ERROR)"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    log = conn.execute("SELECT error FROM sync_log").fetchone()
    assert log["error"].startswith("INTERNAL_SERVER_ERROR: ")


def test_transactions_rejected_by_plaid_fails_the_item(conn, client, fake):
    add_item(conn, products=("transactions",), cursor="c0")
    fake.on("/item/get", {"item": {"products": ["transactions"]}})
    fake.on(SYNC, err("PRODUCT_NOT_ENABLED"))

    (r,) = sync_all(conn, client)

    assert not r.ok and r.error_code == "PRODUCT_NOT_ENABLED"
    assert r.transactions_skipped is None
    assert r.summary() == "Chase: ERROR PRODUCT_NOT_ENABLED"
    row = item_row(conn)
    assert row["status"] == "error" and row["error_code"] == "PRODUCT_NOT_ENABLED"
    assert row["cursor"] == "c0" and row["last_sync_at"] is None


@pytest.mark.parametrize("code", [
    "PRODUCT_NOT_ENABLED", "PRODUCTS_NOT_SUPPORTED", "INVALID_PRODUCT", "ADDITIONAL_CONSENT_REQUIRED",
    "ACCESS_NOT_GRANTED",
])
def test_transactions_product_rejection_is_a_visible_item_error(conn, client, fake, code):
    add_item(conn, products=("transactions",), cursor="c0")
    fake.on("/item/get", {"item": {"products": ["transactions"]}})
    fake.on(SYNC, err(code))

    (r,) = sync_all(conn, client)

    assert r.ok is False
    assert r.error_code == code
    row = item_row(conn)
    assert row["status"] == "error" and row["error_code"] == code
    assert row["error_message"]
    assert row["last_sync_at"] is None
    log = conn.execute("SELECT error FROM sync_log").fetchone()
    assert log["error"].startswith(code + ": ")
    assert syncnow._item_json(r)["error"] is not None
    failure = syncnow.failure([r])
    assert failure is not None and failure["lines"][0].startswith("Couldn't sync ")
    assert "ERROR " in r.summary()
    assert "no transactions (" not in r.summary()


@pytest.mark.parametrize("invalid", [
    {"transactions": True}, "transactions", [1], ["transactions", 1], ["transactions", ""],
    [], None, {"transactions": ["transactions"]},
])
def test_malformed_product_metadata_fails_closed(conn, client, fake, invalid):
    add_item(conn, products=(), cursor="saved")
    fake.on("/item/get", {"item": {"products": invalid}})

    def boom(body):
        raise AssertionError("/transactions/sync called without a trustworthy product grant")

    fake.on(SYNC, boom)

    (r,) = sync_all(conn, client)

    assert not r.ok and r.error_code == "ITEM_PRODUCTS_UNKNOWN"
    assert fake.paths() == ["/item/get"]
    row = item_row(conn)
    assert row["cursor"] == "saved" and row["last_sync_at"] is None
    assert json.loads(row["products"]) == []          # the malformed value was not saved as a grant


def test_missing_product_metadata_fails_closed(conn, client, fake):
    add_item(conn, products=(), cursor="saved")
    fake.on("/item/get", {"item": {"item_id": "item_a"}})

    (r,) = sync_all(conn, client)

    assert not r.ok and r.error_code == "ITEM_PRODUCTS_UNKNOWN"
    assert SYNC not in fake.paths()


def test_malformed_metadata_falls_back_to_valid_stored_products(conn, client, fake):
    add_item(conn, products=("investments",))
    fake.on("/item/get", {"item": {"products": {"transactions": True}}})
    fake.on("/investments/holdings/get", holdings_route())

    (r,) = sync_all(conn, client)

    assert r.ok and SYNC not in fake.paths()
    assert "/investments/holdings/get" in fake.paths()
    assert json.loads(item_row(conn)["products"]) == ["investments"]


@pytest.mark.parametrize("stored", ['{"transactions": true}', '"transactions"', "[1]", "[]", "null", "not json"])
def test_malformed_stored_products_fail_closed(conn, client, fake, stored):
    add_item(conn, products=())
    with conn:
        conn.execute("UPDATE items SET products=? WHERE item_id='item_a'", (stored,))
    fake.on("/item/get", err("API_ERROR", "API_ERROR"))   # keep the stored fallback in play

    (r,) = sync_all(conn, client)

    assert not r.ok and r.error_code == "ITEM_PRODUCTS_UNKNOWN"
    assert SYNC not in fake.paths()
    assert item_row(conn)["last_sync_at"] is None


def test_item_get_product_not_ready_is_fail_closed_and_logged(conn, client, fake):
    add_item(conn, products=("investments",))
    fake.on("/item/get", err("PRODUCT_NOT_READY"))

    (r,) = sync_all(conn, client)

    assert not r.ok and r.error_code == "ITEM_PRODUCTS_UNKNOWN"
    log = conn.execute("SELECT finished_at, error FROM sync_log").fetchone()
    assert log["finished_at"] is not None and log["error"] is not None
    assert item_row(conn)["last_sync_at"] is None


def test_an_unknown_product_item_does_not_stop_later_items(conn, client, fake):
    add_item(conn, "item_a", "ins_1", token=TOKEN_A, products=("investments",))
    add_item(conn, "item_b", "ins_2", name="Harbor", token=TOKEN_B)
    fake.on("/item/get", lambda b: err("PRODUCT_NOT_READY") if b["access_token"] == TOKEN_A
            else {"item": {"products": ["transactions"]}})
    fake.on(SYNC, page(added=[txn("tb1")], next_cursor="cb"))

    first, second = sync_all(conn, client)

    assert not first.ok and first.error_code == "ITEM_PRODUCTS_UNKNOWN"
    assert second.ok and second.transactions_ok and second.added == 1
    assert item_row(conn, "item_b")["cursor"] == "cb"


@pytest.mark.parametrize("code", [
    "PRODUCT_NOT_ENABLED", "PRODUCTS_NOT_SUPPORTED", "INVALID_PRODUCT", "ADDITIONAL_CONSENT_REQUIRED",
    "ACCESS_NOT_GRANTED",
])
def test_transactions_rejection_does_not_skip_mixed_item_data_phases(conn, client, fake, code):
    add_item(conn, products=("transactions", "investments"), cursor="c0")
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on(SYNC, err(code))
    fake.on("/investments/holdings/get", holdings_route())

    (r,) = sync_all(conn, client)

    assert r.ok is False and r.error_code == code
    assert f"ERROR {code}" in r.summary()
    assert "no transactions (" not in r.summary()
    row = item_row(conn)
    assert row["status"] == "error" and row["error_code"] == code
    assert row["error_message"]
    assert row["last_sync_at"] is None and row["cursor"] == "c0"
    log = conn.execute("SELECT error FROM sync_log").fetchone()
    assert log["error"].startswith(code + ": ")
    assert syncnow._item_json(r)["error"] is not None
    assert syncnow.failure([r])["lines"][0].startswith("Couldn't sync ")
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM holding_snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0] == 2
    assert fake.paths() == ["/item/get", SYNC, "/accounts/get", "/investments/holdings/get"]
    assert r.phases["transactions"].ok is False
    assert r.extras == {"balances": "ok", "investments": "ok"}
    c = syncnow.changes({}, {}, [r])
    assert c["text"] is None
    assert "Nothing new" not in syncnow.summary_line(c)


def test_transactions_rejection_does_not_skip_liabilities_phase(conn, client, fake):
    add_item(conn, products=("transactions", "investments", "liabilities"), cursor="c0")
    fake.on("/item/get", {"item": {"products": ["transactions", "investments", "liabilities"]}})
    fake.on(SYNC, err("PRODUCT_NOT_ENABLED"))
    fake.on("/investments/holdings/get", holdings_route())
    fake.on("/liabilities/get", {
        "accounts": [account("acc_cc", "Card", "9999", "credit", "credit card", 250.0, 4750.0, 5000.0)],
        "liabilities": {"credit": [{"account_id": "acc_cc", "last_statement_balance": 200.0,
                                    "minimum_payment_amount": 25.0, "next_payment_due_date": "2026-10-10",
                                    "is_overdue": False, "aprs": [{"apr_type": "purchase_apr",
                                                                   "apr_percentage": 24.99}]}],
                        "mortgage": None, "student": None}})

    (r,) = sync_all(conn, client)

    paths = fake.paths()
    assert paths == ["/item/get", SYNC, "/accounts/get", "/liabilities/get", "/investments/holdings/get"]
    assert paths.index("/liabilities/get") > paths.index(SYNC)
    liabilities = conn.execute("SELECT * FROM liabilities").fetchone()
    assert liabilities["kind"] == "credit" and liabilities["last_statement_balance"] == 200.0
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 1
    assert r.phases["transactions"].ok is False
    assert r.extras == {"balances": "ok", "liabilities": "ok", "investments": "ok"}
    assert item_row(conn)["error_code"] == "PRODUCT_NOT_ENABLED"
    assert r.ok is False and r.error_code == "PRODUCT_NOT_ENABLED"
