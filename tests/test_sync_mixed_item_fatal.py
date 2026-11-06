"""A fatal transactions error on a mixed Item must not refresh (or snapshot) its sibling products.

Plaid's ITEM_LOGIN_REQUIRED / ITEM_LOCKED / INVALID_ACCESS_TOKEN say the Item itself is not usable
this run. So a transactions+investments Item must stop after /transactions/sync: no /accounts/get,
no /investments/holdings/get, no balance or holding snapshot, and the cursor stays where it was.
Only the product/consent rejections (PRODUCT_NOT_ENABLED and friends) keep the sibling phases going
(test_sync_products.py pins that half).

Regression for f665076: before the guard every fatal transactions error still ran the data phases
and wrote a snapshot from data the Item had just been refused.
"""

from __future__ import annotations

import pytest

from conftest import account, add_item, err, item_row
from hermes_finance import syncnow
from hermes_finance.sync import sync_all

SYNC = "/transactions/sync"

# "the Item is unusable", as opposed to "this product was rejected"
FATAL_ITEM_CODES = ("ITEM_LOGIN_REQUIRED", "ITEM_LOCKED", "INVALID_ACCESS_TOKEN")
# what the ledger shows for each: only a login error gets its own status
EXPECTED_STATUS = {"ITEM_LOGIN_REQUIRED": "login_required", "ITEM_LOCKED": "error",
                   "INVALID_ACCESS_TOKEN": "error"}


def holdings_route(value=1000.0):
    return {
        "accounts": [account("acc_inv", "Brokerage", "5555", "investment", "brokerage", 5000.0, None)],
        "holdings": [{"account_id": "acc_inv", "security_id": "sec_1", "quantity": 10,
                      "institution_price": value / 10, "institution_value": value, "cost_basis": 800.0,
                      "iso_currency_code": "USD"}],
        "securities": [{"security_id": "sec_1", "ticker_symbol": "VTI", "name": "Vanguard Total", "type": "etf"}],
    }


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.mark.parametrize("code", FATAL_ITEM_CODES)
def test_mixed_item_fatal_transactions_error_blocks_every_sibling_phase(conn, client, fake, code):
    add_item(conn, products=("transactions", "investments"), cursor="c0")
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on(SYNC, err(code))
    fake.on("/investments/holdings/get", holdings_route())

    (r,) = sync_all(conn, client)

    # the Item is reported as failed, with Plaid's own code
    assert r.ok is False and r.error_code == code
    assert r.summary().startswith("Chase: ERROR " + code)
    assert set(r.phases) == {"transactions"}
    assert r.phases["transactions"].attempted is True and r.phases["transactions"].fatal is True
    assert r.extras == {}

    # ... and nothing else ran: no balances, no holdings, no snapshot from stale data
    assert fake.paths() == ["/item/get", SYNC]
    assert count(conn, "holdings") == 0
    assert count(conn, "holding_snapshots") == 0
    assert count(conn, "balance_snapshots") == 0

    # the cursor is untouched, so the next sync resumes where this one stopped
    row = item_row(conn)
    assert row["cursor"] == "c0"
    assert row["status"] == EXPECTED_STATUS[code] and row["error_code"] == code
    assert row["last_sync_at"] is None
    log = conn.execute("SELECT finished_at, error FROM sync_log").fetchone()
    assert log["finished_at"] is not None and log["error"].startswith(code + ": ")


@pytest.mark.parametrize("code", FATAL_ITEM_CODES)
def test_fatal_transactions_error_on_a_transactions_only_item_changes_nothing_else(conn, client, fake, code):
    """The transactions-only half of the same rule: no data phase, no cursor move, no snapshot."""
    add_item(conn, products=("transactions",), cursor="c0")
    fake.on("/item/get", {"item": {"products": ["transactions"]}})
    fake.on(SYNC, err(code))

    (r,) = sync_all(conn, client)

    assert r.ok is False and r.error_code == code
    assert fake.paths() == ["/item/get", SYNC]
    assert count(conn, "balance_snapshots") == 0
    assert item_row(conn)["cursor"] == "c0"


def test_mixed_item_login_required_makes_no_sibling_calls_and_keeps_the_reconnect_line(conn, client, fake):
    add_item(conn, products=("transactions", "investments"), cursor="c0")
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on(SYNC, err("ITEM_LOGIN_REQUIRED"))
    fake.on("/investments/holdings/get", holdings_route())

    (r,) = sync_all(conn, client)

    assert fake.paths() == ["/item/get", SYNC]
    assert r.ok is False and r.error_code == "ITEM_LOGIN_REQUIRED"
    assert "Reconnect Chase on Accounts." in syncnow.failure([r])["lines"]
    row = item_row(conn)
    assert row["status"] == "login_required" and row["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert row["last_sync_at"] is None and row["cursor"] == "c0"
    assert count(conn, "holdings") == 0
    assert count(conn, "holding_snapshots") == 0
    assert count(conn, "balance_snapshots") == 0


def test_mixed_item_network_error_everywhere_is_unreachable_with_no_sibling_calls(conn, client, fake):
    add_item(conn, products=("transactions", "investments"), cursor="c0")
    # a real NETWORK_ERROR is an API error, not an Item error: /item/get leaves the stored products in play
    net = err("NETWORK_ERROR", "NETWORK_ERROR")
    fake.on("/item/get", net)
    fake.on(SYNC, net)
    fake.on("/accounts/get", net)
    fake.on("/investments/holdings/get", net)
    fake.on("/liabilities/get", net)

    (r,) = sync_all(conn, client)

    assert syncnow.failure([r]) == {"unreachable": True, "lines": [syncnow.UNREACHABLE]}
    assert fake.paths() == ["/item/get", SYNC]
    assert count(conn, "holding_snapshots") == 0
    assert count(conn, "balance_snapshots") == 0
    assert item_row(conn)["cursor"] == "c0"
    assert item_row(conn)["last_sync_at"] is None


def test_rejection_then_different_investment_error_keeps_the_first_fatal_code(conn, client, fake):
    add_item(conn, products=("transactions", "investments"), cursor="c0")
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on(SYNC, err("PRODUCT_NOT_ENABLED"))
    fake.on("/investments/holdings/get", err("ITEM_LOCKED"))

    (r,) = sync_all(conn, client)

    row = item_row(conn)
    log = conn.execute("SELECT error FROM sync_log").fetchone()["error"]
    summary = syncnow._item_json(r)["error"]
    assert row["status"] == "error" and row["error_code"] == "PRODUCT_NOT_ENABLED"
    assert r.error_code == "PRODUCT_NOT_ENABLED"
    assert log.startswith("PRODUCT_NOT_ENABLED: ")
    assert "PRODUCT_NOT_ENABLED" in summary
    assert all("ITEM_LOCKED" not in v for v in (row["error_code"], r.error_code, log, summary))
    assert r.extras["investments"] == "err (ITEM_LOCKED)"


def test_liabilities_then_investments_failures_keep_the_first_fatal_code(conn, client, fake):
    add_item(conn, products=("liabilities", "investments"))
    fake.on("/item/get", {"item": {"products": ["liabilities", "investments"]}})
    fake.on("/liabilities/get", err("ITEM_LOCKED"))
    fake.on("/investments/holdings/get", err("INVALID_ACCESS_TOKEN"))

    (r,) = sync_all(conn, client)

    row = item_row(conn)
    log = conn.execute("SELECT error FROM sync_log").fetchone()["error"]
    summary = syncnow._item_json(r)["error"]
    assert fake.paths() == ["/item/get", "/accounts/get", "/liabilities/get", "/investments/holdings/get"]
    assert row["status"] == "error" and row["error_code"] == "ITEM_LOCKED"
    assert r.error_code == "ITEM_LOCKED"
    assert log.startswith("ITEM_LOCKED: ")
    assert "ITEM_LOCKED" in summary
    assert all("INVALID_ACCESS_TOKEN" not in v for v in (row["error_code"], r.error_code, log, summary))
    assert count(conn, "holdings") == 0
