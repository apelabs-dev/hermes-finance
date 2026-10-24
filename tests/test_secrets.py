"""The Plaid secret and access tokens never reach stdout, stderr, logs, or HTTP responses."""

from __future__ import annotations

import json
import logging

from conftest import FAKE_SECRET, TOKEN_A, TOKEN_NEW, add_item, err, page, txn
from hermes_finance import cli, config, link
from hermes_finance.plaid import PlaidClient, PlaidError
from hermes_finance.web import App

HEADERS = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}


def test_secrets_never_in_output(conn, client, fake, capsys, caplog, cli_client):
    caplog.set_level(logging.DEBUG)
    add_item(conn, "item_a", "ins_1", token=TOKEN_A)
    fake.on("/item/public_token/exchange", {"access_token": TOKEN_NEW, "item_id": "item_new"})
    fake.on("/link/token/create", {"link_token": "link-production-x", "expiration": "x"})
    fake.on("/transactions/sync", lambda b: page(added=[txn("t_" + b["access_token"][-4:])], next_cursor="c1")
            if b["access_token"] == TOKEN_NEW else err("ITEM_LOGIN_REQUIRED"))
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    bodies = []

    def call(method, path, payload=None):
        status, _, body = app.handle(method, path, HEADERS, json.dumps(payload or {}).encode())
        bodies.append(body.decode())
        return status

    # web flows: page, status, new link token, exchange, duplicate, update mode, reconnect
    assert call("GET", "/") == 200
    assert call("GET", "/api/status") == 200
    assert call("POST", "/api/link_token") == 200
    meta = {"institution": {"institution_id": "ins_2", "name": "Amex"}}
    assert call("POST", "/api/exchange", {"public_token": "public-production-p", "metadata": meta}) == 200
    assert call("POST", "/api/exchange", {"public_token": "public-production-p", "metadata": meta}) == 409
    assert call("POST", "/api/link_token/update", {"item_id": "item_a"}) == 200
    assert call("POST", "/api/reconnected", {"item_id": "item_a"}) == 200
    fake.on("/link/token/create", err("INVALID_FIELD", "INVALID_REQUEST"))
    assert call("POST", "/api/link_token/update", {"item_id": "item_a"}) == 502

    # CLI flows, including error paths
    cli.main(["-v", "sync"])
    cli.main(["status"])
    cli.main(["status", "--json"])
    cli.main(["query", "SELECT * FROM items"])
    cli.main(["query", "SELECT access_token FROM items", "--json"])
    cli.main(["unclassified", "--json"])
    cli.main(["export", "--since", "2000-01-01"])

    captured = capsys.readouterr()
    haystacks = {"stdout": captured.out, "stderr": captured.err, "logs": caplog.text,
                 "http": "\n".join(bodies)}
    for where, text in haystacks.items():
        for secret in (FAKE_SECRET, TOKEN_A, TOKEN_NEW):
            assert secret not in text, f"{secret[:12]}… leaked to {where}"
    assert "item_new" in captured.out  # sanity: we did capture real output


def test_plaid_error_and_client_repr_do_not_contain_secret(fake):
    settings = config.Settings("fakeclientid000000000001", FAKE_SECRET, "production")
    assert FAKE_SECRET not in repr(settings)
    client = PlaidClient(settings, transport=lambda *a: (400, json.dumps({
        "error_type": "INVALID_INPUT", "error_code": "INVALID_ACCESS_TOKEN",
        "error_message": f"bad token {TOKEN_A}"}).encode()))
    try:
        client.accounts_get(TOKEN_A)
    except PlaidError as e:
        assert TOKEN_A not in str(e) and TOKEN_A not in e.error_message
    else:
        raise AssertionError("expected PlaidError")


def test_link_exchange_logs_contain_no_token(conn, client, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.on("/item/public_token/exchange", {"access_token": TOKEN_NEW, "item_id": "item_new"})
    link.exchange(conn, client, "public-production-p", {"institution": {"institution_id": "ins_3", "name": "X"}})
    assert "item_new" in caplog.text
    assert TOKEN_NEW not in caplog.text and FAKE_SECRET not in caplog.text
