"""Slot safety: duplicate guard, slot counter, update mode, token backup."""

from __future__ import annotations

import json
import os
import stat

import pytest

from conftest import TOKEN_A, TOKEN_NEW, add_item, err, item_row, page
from hermes_finance import config, db, link, report
from hermes_finance.web import App

EXCHANGE = "/item/public_token/exchange"
HOST = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}


def make_app(client):
    return App(config.db_path(), lambda: client, "127.0.0.1", 8765)


def post(app, path, payload):
    status, ctype, body = app.handle("POST", path, HOST, json.dumps(payload).encode())
    return status, json.loads(body)


def get(app, path):
    status, ctype, body = app.handle("GET", path, {"Host": "127.0.0.1:8765"}, b"")
    return status, body.decode()


def exchange_payload(institution_id="ins_1", name="Chase"):
    return {"public_token": "public-production-11111111-2222-3333-4444-555555555555",
            "metadata": {"institution": {"institution_id": institution_id, "name": name},
                         "accounts": [{"id": "acc_chk", "mask": "1234"}]}}


# --- duplicate guard -------------------------------------------------------------------------

def test_duplicate_guard_blocks_exchange(conn, client, fake):
    add_item(conn, institution_id="ins_1", name="Chase")
    fake.on(EXCHANGE, {"access_token": TOKEN_NEW, "item_id": "item_dup"})
    app = make_app(client)

    status, body = post(app, "/api/exchange", exchange_payload("ins_1", "Chase"))

    assert status == 409
    assert "already connected — use Reconnect instead" in body["error"]
    assert EXCHANGE not in fake.paths()
    assert db.slots_used(conn) == 1
    assert conn.execute("SELECT outcome FROM link_attempts").fetchone()[0] == "duplicate_blocked"
    # the page warns that Plaid may have counted the blocked Link completion
    assert "duplicate Link completion" in get(app, "/")[1]


def test_confirmed_counted_duplicate_takes_a_slot_and_clears_the_warning(conn, client, fake):
    """Plaid confirmed it counted a blocked duplicate (an Item with no access_token): it uses a slot, the
    'may have counted' warning goes, and the slot cap includes it."""
    add_item(conn, institution_id="ins_1", name="Chase")
    fake.on(EXCHANGE, {"access_token": TOKEN_NEW, "item_id": "item_dup"})
    app = make_app(client)
    post(app, "/api/exchange", exchange_payload("ins_1", "Chase"))
    assert (db.slots_used(conn), db.blocked_duplicates(conn)) == (1, 1)

    conn.execute("UPDATE link_attempts SET outcome = 'duplicate_counted' WHERE outcome = 'duplicate_blocked'")
    conn.commit()

    assert (db.slots_used(conn), db.blocked_duplicates(conn)) == (2, 0)
    s = report.status(conn)
    assert (s["slots_used"], s["blocked_duplicate_links"]) == (2, 0)
    assert "duplicate Link completion" not in get(app, "/")[1]
    assert json.loads(get(app, "/api/status")[1])["slots_used"] == 2

def test_duplicate_guard_blocks_even_when_item_is_in_error(conn, client, fake):
    add_item(conn, institution_id="ins_1", status="login_required")
    with pytest.raises(link.LinkRefused, match="use Reconnect instead"):
        link.exchange(conn, client, "public-x", exchange_payload("ins_1")["metadata"])
    assert EXCHANGE not in fake.paths()


def test_exchange_refused_without_institution_id(conn, client, fake):
    status, body = post(make_app(client), "/api/exchange", {"public_token": "public-x", "metadata": {}})
    assert status == 400 and "refusing to exchange" in body["error"]
    assert EXCHANGE not in fake.paths()


def test_page_has_early_duplicate_guard(conn, client):
    add_item(conn, institution_id="ins_1")
    app = make_app(client)
    html = get(app, "/")[1]
    # TASK-4: the page's JS lives in /static/app.js (the CSP allows no inline script) and gets the
    # connected institutions from /api/accounts instead of an inline literal
    js = get(app, "/static/app.js")[1]
    assert '<script src="/static/app.js"' in html
    assert json.loads(get(app, "/api/accounts")[1])["connected_institution_ids"] == ["ins_1"]
    assert "CONNECTED_INSTITUTIONS = new Set(d.connected_institution_ids)" in js
    assert "SELECT_INSTITUTION" in js and "handler.exit" in js
    assert "link-initialize.js" in html


def test_new_institution_is_exchanged_and_synced(conn, client, fake):
    add_item(conn, institution_id="ins_1")
    fake.on(EXCHANGE, {"access_token": TOKEN_NEW, "item_id": "item_new"})
    status, body = post(make_app(client), "/api/exchange", exchange_payload("ins_2", "Amex"))
    assert status == 200, body
    assert body["item_id"] == "item_new" and body["sync_ok"]
    assert fake.paths().count(EXCHANGE) == 1
    new = item_row(conn, "item_new")
    assert new["institution_id"] == "ins_2" and new["access_token"] == TOKEN_NEW
    assert new["last_sync_at"] and new["cursor"] == "c1"
    assert db.slots_used(conn) == 2


# --- slot counter ----------------------------------------------------------------------------

def test_slot_counter_counts_removed_items(conn, client):
    add_item(conn, "item_a", "ins_1")
    add_item(conn, "item_b", "ins_2", token="access-production-b")
    add_item(conn, "item_c", "ins_3", token="access-production-c", removed=True)

    assert db.slots_used(conn) == 3
    assert report.status(conn)["slots_used"] == 3
    app = make_app(client)
    assert "slots used: <b>3 / 10</b>" in get(app, "/")[1]
    assert json.loads(get(app, "/api/status")[1])["slots_used"] == 3


def test_link_token_refused_when_all_slots_used(conn, client, fake):
    for i in range(7):
        add_item(conn, f"item_{i}", f"ins_{i}", token=f"access-production-{i}")
    for i in range(7, 10):
        add_item(conn, f"item_{i}", f"ins_{i}", token=f"access-production-{i}", removed=True)
    app = make_app(client)

    status, body = post(app, "/api/link_token", {})

    assert status == 409 and "10/10" in body["error"]
    assert "/link/token/create" not in fake.paths()
    assert "disabled" in get(app, "/")[1]


def test_link_token_for_new_bank_uses_spec_products(conn, client, fake):
    fake.on("/link/token/create", {"link_token": "link-production-abc", "expiration": "x"})
    status, body = post(make_app(client), "/api/link_token", {})
    assert status == 200 and body["link_token"] == "link-production-abc"
    (sent,) = fake.bodies("/link/token/create")
    assert sent["products"] == ["transactions"]
    assert sent["optional_products"] == ["liabilities", "investments"]
    assert sent["transactions"] == {"days_requested": 730}
    assert sent["country_codes"] == ["US"] and sent["language"] == "en"
    assert sent["client_name"] == "Hermes Budget"
    assert "redirect_uri" not in sent and "access_token" not in sent


# --- update mode -----------------------------------------------------------------------------

def test_reconnect_uses_update_mode_and_never_exchanges(conn, client, fake):
    add_item(conn, status="login_required")
    with conn:
        conn.execute("UPDATE items SET error_code='ITEM_LOGIN_REQUIRED' WHERE item_id='item_a'")
    fake.on("/link/token/create", {"link_token": "link-production-upd", "expiration": "x"})
    fake.on(EXCHANGE, {"access_token": TOKEN_NEW, "item_id": "should-never-happen"})
    app = make_app(client)

    status, body = post(app, "/api/link_token/update", {"item_id": "item_a"})
    assert status == 200 and body == {"link_token": "link-production-upd", "expiration": "x"}
    (sent,) = fake.bodies("/link/token/create")
    assert sent["access_token"] == TOKEN_A
    assert "products" not in sent and "optional_products" not in sent

    status, body = post(app, "/api/reconnected", {"item_id": "item_a"})
    assert status == 200 and body["sync_ok"]

    assert EXCHANGE not in fake.paths()
    row = item_row(conn)
    assert row["status"] == "active" and row["error_code"] is None
    assert db.slots_used(conn) == 1
    html = get(app, "/")[1]
    js = get(app, "/static/app.js")[1]  # TASK-4: Reconnect buttons are wired in app.js (no inline handlers)
    assert "reconnect(rc.dataset.item)" in js and "post('/api/link_token/update'" in js
    assert TOKEN_A not in html
    assert TOKEN_A not in get(app, "/api/accounts")[1]


def test_update_token_for_unknown_item_is_404(conn, client, fake):
    status, _ = post(make_app(client), "/api/link_token/update", {"item_id": "nope"})
    assert status == 404
    assert fake.paths() == []


# --- token persistence -----------------------------------------------------------------------

def test_token_backup_written_before_sync(conn, client, fake, data_dir):
    fake.on(EXCHANGE, {"access_token": TOKEN_NEW, "item_id": "item_new"})
    seen = {}

    def sync_handler(body):
        backup = config.token_backup_path()
        lines = [json.loads(l) for l in backup.read_text().splitlines()]
        seen["backup"] = lines
        other = db.connect()
        seen["db_token"] = other.execute("SELECT access_token FROM items WHERE item_id='item_new'").fetchone()[0]
        other.close()
        return page(next_cursor="c1")

    fake.on("/transactions/sync", sync_handler)
    result = link.exchange(conn, client, "public-x", exchange_payload("ins_9", "Bank9")["metadata"])

    assert result["sync_ok"]
    assert seen["backup"] == [{"item_id": "item_new", "access_token": TOKEN_NEW, "institution_id": "ins_9",
                               "institution_name": "Bank9", "created_at": seen["backup"][0]["created_at"]}]
    assert seen["db_token"] == TOKEN_NEW
    order = fake.paths()
    assert order.index(EXCHANGE) < order.index("/transactions/sync")

    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)
    assert mode(config.token_backup_path()) == 0o600
    assert mode(config.db_path()) == 0o600
    assert mode(data_dir) == 0o700


def test_backup_is_append_only(conn, client, fake):
    tokens = iter([("access-production-one", "item_1"), ("access-production-two", "item_2")])

    def exch(body):
        tok, iid = next(tokens)
        return {"access_token": tok, "item_id": iid}

    fake.on(EXCHANGE, exch)
    link.exchange(conn, client, "public-1", exchange_payload("ins_1")["metadata"])
    link.exchange(conn, client, "public-2", exchange_payload("ins_2")["metadata"])
    lines = config.token_backup_path().read_text().splitlines()
    assert [json.loads(l)["item_id"] for l in lines] == ["item_1", "item_2"]


def test_initial_sync_failure_still_keeps_item(conn, client, fake):
    fake.on(EXCHANGE, {"access_token": TOKEN_NEW, "item_id": "item_new"})
    fake.on("/transactions/sync", err("INTERNAL_SERVER_ERROR", "API_ERROR", 500))
    result = link.exchange(conn, client, "public-x", exchange_payload("ins_5")["metadata"])
    assert result["sync_ok"] is False
    assert item_row(conn, "item_new")["access_token"] == TOKEN_NEW


def test_failed_exchange_is_recorded_and_stores_nothing(conn, client, fake):
    fake.on(EXCHANGE, err("INVALID_PUBLIC_TOKEN", "INVALID_INPUT"))
    status, body = post(make_app(client), "/api/exchange", exchange_payload("ins_7", "Bank7"))
    assert status == 502 and "INVALID_PUBLIC_TOKEN" in body["error"]
    assert db.slots_used(conn) == 0
    assert conn.execute("SELECT outcome, error FROM link_attempts").fetchone()[:] == ("error", "INVALID_PUBLIC_TOKEN")
