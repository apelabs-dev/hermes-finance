"""The SimpleFIN HTTP surface: claim -> confirm -> status, the confirm list, the type requirement, disconnect and the
duplicate source pick. No response ever carries the access URL; the settings and accounts payloads carry the status the
pages draw.
"""

from __future__ import annotations

import base64
import json
import socket
from datetime import datetime, timezone

import pytest

from conftest import FAKE_CLIENT_ID, FAKE_SECRET
from hermes_finance import config, db, secretstore, simplefin, simplefin_sync
from hermes_finance.config import Settings
from hermes_finance.plaid import PlaidClient
from hermes_finance.web import App

TODAY = __import__("datetime").date(2026, 11, 15)
ACCESS = "https://bridge-user:bridge-pass@bridge.example/simplefin"
TOKEN = base64.urlsafe_b64encode(b"https://bridge.example/simplefin/claim/synthetic").decode().rstrip("=")
HEADERS = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
PUBLIC = lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]  # noqa: E731


def payload():
    return {"errlist": [], "connections": [{"conn_id": "CON-1", "name": "Synthetic Bank"}],
            "accounts": [{"id": "ACT-1", "name": "Everyday Checking", "conn_id": "CON-1", "currency": "USD",
                          "balance": "1200.25", "available-balance": "1150.25",
                          "balance-date": 1794787200,
                          "transactions": [{"id": "T1", "posted": 1794700800, "amount": "-25.00",
                                            "description": "Grocery store", "transacted_at": 1794700800}]}]}


class Bridge:
    def __init__(self, claim_status=200, accounts_status=200, data=None):
        self.claim_status, self.accounts_status = claim_status, accounts_status
        self.data = data or payload()
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        if method == "POST":
            self.calls.append(("claim", url))
            return simplefin.Response(self.claim_status, ACCESS.encode() if self.claim_status == 200
                                      else b"Forbidden (was it already claimed?)")
        self.calls.append(("accounts", url))
        return simplefin.Response(self.accounts_status, json.dumps(self.data).encode())


@pytest.fixture
def bridge(monkeypatch):
    b = Bridge()
    monkeypatch.setattr(simplefin, "urllib_transport", b)
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    return b


@pytest.fixture
def app(conn, monkeypatch):
    monkeypatch.setattr(App, "today", property()) if False else None
    client = PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=lambda *a: (500, b"{}"))
    a = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def call(app, method, path, data=None):
    status, _, body = app.handle(method, path, HEADERS, json.dumps(data or {}).encode())
    return status, json.loads(body or b"{}"), body.decode()


def claimed(app):
    status, body, raw = call(app, "POST", "/api/simplefin/claim", {"token": TOKEN})
    assert status == 200, body
    assert "bridge-pass" not in raw and ACCESS not in raw
    return body


def test_claim_returns_the_confirm_list_and_never_the_url(app, bridge, data_dir):
    simplefin_sync._set(db.connect(config.db_path()), simplefin_sync.DAILY_MINUTE_KEY, 7)
    body = claimed(app)
    assert body["counts"] == {"banks": 1, "accounts": 1}
    assert body["accounts"][0]["guess"]["type"] == "depository"
    assert body["status"]["state"] == "needs_confirm"
    status, got, raw = call(app, "GET", "/api/simplefin/confirm")
    assert status == 200 and got["accounts"][0]["id"] == "ACT-1"
    assert "bridge-pass" not in raw
    st, status_body, raw = call(app, "GET", "/api/simplefin/status")
    assert st == 200 and status_body["needs_confirm"] is True
    assert status_body["requests_today"] == 1 and status_body["manual_left"] == 6
    assert status_body["daily_minute"] == 7
    assert "bridge-pass" not in raw
    assert secretstore.simplefin_url() == ACCESS


def test_confirm_requires_a_type_for_every_account_before_the_first_sync(app, bridge):
    claimed(app)
    st, body, _ = call(app, "POST", "/api/simplefin/confirm", {"accounts": {"CON-1:ACT-1": {}}})
    assert st == 400 and "Pick a type" in body["error"]
    st, body, _ = call(app, "POST", "/api/simplefin/confirm", {})
    assert st == 400 and body["error"].startswith("accounts is required")
    assert simplefin_sync.connected(db.connect(config.db_path())) is False


def test_confirm_creates_the_rows_and_status_reports_the_shape(app, bridge):
    simplefin_sync._set(db.connect(config.db_path()), simplefin_sync.DAILY_MINUTE_KEY, 7)
    claimed(app)
    st, body, raw = call(app, "POST", "/api/simplefin/confirm",
                         {"accounts": [{"key": "CON-1:ACT-1", "type": "checking"}]})
    assert st == 200, body
    assert body["connected"] is True and body["banks"] == 1 and body["accounts"] == 1
    assert body["state"] == "connected"
    assert body["requests_today"] == 5                  # the claim + four backfill windows, never more
    assert body["coverage_start"] == "2026-11-15"       # what came back, not the year we asked for
    assert body["as_of"] == TODAY.isoformat()           # the app's given day, not the real clock
    assert body["duplicate_reviews"] == 0 and body["errors"] == []
    assert "bridge-pass" not in raw
    conn = db.connect(config.db_path())
    try:
        assert conn.execute("SELECT provider, access_token FROM items").fetchone()[:] == ("simplefin", "")
        assert conn.execute("SELECT source FROM transactions").fetchone()[0] == "simplefin"
    finally:
        conn.close()


@pytest.fixture
def real_clock_app(conn):
    """The app as `finance serve` builds it: its `today` is the real one, nothing pinned."""
    client = PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=lambda *a: (500, b"{}"))
    return App(config.db_path(), lambda: client, "127.0.0.1", 8765)


def _utc(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def test_claim_and_confirm_through_the_server_stamp_the_real_time(real_clock_app, bridge):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    claimed(real_clock_app)
    st, body, _ = call(real_clock_app, "POST", "/api/simplefin/confirm",
                       {"accounts": [{"key": "CON-1:ACT-1", "type": "checking"}]})
    assert st == 200, body
    after = datetime.now(timezone.utc)
    conn = db.connect(config.db_path())
    try:
        stamps = [r["at"] for r in conn.execute("SELECT at FROM simplefin_requests ORDER BY id")]
        last_success = simplefin_sync._get(conn, simplefin_sync.LAST_SUCCESS_KEY)
        last_result_at = simplefin_sync._get(conn, simplefin_sync.LAST_RESULT_KEY)["at"]
    finally:
        conn.close()
    assert len(stamps) == 5                                  # the claim, then confirm's four backfill windows
    for stamp in [*stamps, last_success, last_result_at]:    # the time of day it happened, not a fixed noon
        assert not stamp.endswith("T12:00:00Z"), stamp
        assert before <= _utc(stamp) <= after, (stamp, before, after)
    assert stamps == sorted(stamps)                          # the request log never runs backwards


def test_a_pinned_day_that_is_not_today_still_stamps_its_fixed_noon(app, bridge):
    claimed(app)                                             # the app's given day is 2026-11-15, not the real one
    st, body, _ = call(app, "POST", "/api/simplefin/confirm", {"accounts": [{"key": "CON-1:ACT-1", "type": "checking"}]})
    assert st == 200, body
    conn = db.connect(config.db_path())
    try:
        assert {r["at"] for r in conn.execute("SELECT at FROM simplefin_requests")} == {"2026-11-15T12:00:00Z"}
        assert simplefin_sync._get(conn, simplefin_sync.LAST_SUCCESS_KEY) == "2026-11-15T12:00:00Z"
    finally:
        conn.close()


def test_the_sidebar_counts_equal_the_transactions_page_before_and_after_a_remove(app, bridge):
    claimed(app)
    st, body, _ = call(app, "POST", "/api/simplefin/confirm", {"accounts": [{"key": "CON-1:ACT-1", "type": "checking"}]})
    assert st == 200, body

    def both():
        _, badge, _ = call(app, "GET", "/api/counts?month=2026-11")
        _, page, _ = call(app, "GET", "/api/transactions?month=2026-11")
        return (badge["transactions"], badge["review"]), (page["counts"]["all"], page["counts"]["review"])

    shown, listed = both()
    assert shown == listed == (1, 1)                    # one uncategorised row, counted the same by both
    st, body, _ = call(app, "POST", "/api/simplefin/disconnect", {})
    assert st == 200, body
    shown, listed = both()
    assert shown == listed == (0, 0)                    # the removed connection's rows are out of both, not just the page


def test_claim_403_returns_the_not_accepted_wording_under_the_token_field(app, monkeypatch):
    b = Bridge(claim_status=403)
    monkeypatch.setattr(simplefin, "urllib_transport", b)
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    st, body, raw = call(app, "POST", "/api/simplefin/claim", {"token": TOKEN})
    assert st == 400
    assert body["field"] == "token"
    assert body["error"] == ("SimpleFIN Bridge didn't accept this token. It may be mistyped or already used: create a "
                             "new one at SimpleFIN Bridge.")
    assert "bridge-pass" not in raw
    assert secretstore.simplefin_url() is None


def test_the_claim_route_without_a_token_retries_the_saved_link(app, bridge, data_dir):
    bridge.accounts_status = 500
    st, body, raw = call(app, "POST", "/api/simplefin/claim", {"token": TOKEN})
    assert st == 502
    assert "bridge-pass" not in raw
    assert secretstore.simplefin_url() == ACCESS
    conn = db.connect(config.db_path())
    try:
        assert simplefin_sync.status(conn, TODAY)["state"] == "needs_retry"
    finally:
        conn.close()
    bridge.accounts_status = 200
    st, body, raw = call(app, "POST", "/api/simplefin/claim", {})
    assert st == 200 and body["accounts"][0]["id"] == "ACT-1"
    assert body["status"]["needs_confirm"] is True
    assert "bridge-pass" not in raw
    st, body, _ = call(app, "POST", "/api/simplefin/confirm", {"accounts": [{"key": "CON-1:ACT-1", "type": "checking"}]})
    assert st == 200 and body["connected"] is True and body["accounts"] == 1


def test_disconnect_and_the_settings_and_accounts_payloads(app, bridge, data_dir):
    claimed(app)
    call(app, "POST", "/api/simplefin/confirm", {"accounts": {"CON-1:ACT-1": {"type": "checking"}}})
    st, settings, raw = call(app, "GET", "/api/settings/page")
    assert st == 200
    assert settings["simplefin"]["connected"] is True and settings["simplefin"]["accounts"] == 1
    assert settings["simplefin"]["requests_today"] == 5
    assert "bridge-pass" not in raw
    st, accounts, raw = call(app, "GET", "/api/accounts")
    assert st == 200
    assert accounts["simplefin"]["connected"] is True
    assert [it["provider"] for it in accounts["items"]] == ["simplefin"]
    assert "bridge-pass" not in raw
    st, out, raw = call(app, "POST", "/api/simplefin/disconnect")
    assert st == 200 and out["connected"] is False
    assert secretstore.simplefin_url() is None
    assert db.connect(config.db_path()).execute("SELECT removed_at FROM items").fetchone()[0] is not None
    assert "bridge-pass" not in raw


def test_the_sync_trigger_shares_syncnow(app, bridge, monkeypatch):
    claimed(app)
    call(app, "POST", "/api/simplefin/confirm", {"accounts": {"CON-1:ACT-1": {"type": "checking"}}})
    st, out, raw = call(app, "POST", "/api/simplefin/sync", {})
    assert st in (200, 202)
    assert out.get("simplefin") or out.get("result") or out.get("rid")
    # the sync's outcome, when the answer came back directly
    result = out.get("simplefin") or (out.get("result") or {}).get("simplefin")
    if result is not None:
        assert result["connected"] is True and result["ok"] is True
    assert "bridge-pass" not in raw


def test_the_duplicate_source_pick_endpoint(app, bridge):
    conn = db.connect(config.db_path())
    try:
        with conn:
            conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                         "VALUES ('item_plaid', 'ins_usb', 'U.S. Bank', 'access-production-x', 'active', '2026-01-01')")
            conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance) "
                         "VALUES ('acc_plaid', 'item_plaid', 'Smartly Checking', '5108', 'depository', 'checking', 100)")
    finally:
        conn.close()
    data = payload()
    data["accounts"][0].update(name="Smartly Checking 5108", balance="100.00", **{"available-balance": "100.00"})
    bridge.data = data
    claimed(app)
    st, body, _ = call(app, "POST", "/api/simplefin/confirm", {"accounts": {"CON-1:ACT-1": {"type": "checking"}}})
    assert st == 200 and body["duplicate_reviews"] == 1
    st, body, _ = call(app, "POST", "/api/simplefin/duplicate",
                       {"simplefin_account_id": "simplefin:CON-1:ACT-1", "plaid_account_id": "acc_plaid",
                        "choice": "simplefin"})
    assert st == 200 and body["duplicate_reviews"] == 0
    policy = json.loads(db.connect(config.db_path()).execute(
        "SELECT value FROM settings WHERE key = 'excluded_accounts'").fetchone()[0])
    assert "acc_plaid" in policy and "simplefin:CON-1:ACT-1" not in policy


def test_the_cli_status_and_claim(conn, data_dir, monkeypatch, capsys):
    from hermes_finance import cli
    b = Bridge()
    monkeypatch.setattr(simplefin, "urllib_transport", b)
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    assert cli.main(["simplefin", "status"]) == 0
    assert "SimpleFIN: not connected" in capsys.readouterr().out
    assert cli.main(["simplefin", "claim", TOKEN, "--type", "CON-1:ACT-1=checking"]) == 0
    out = capsys.readouterr().out
    assert "SimpleFIN connected: 1 bank(s) · 1 account(s)" in out
    assert cli.main(["simplefin", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "connected" and payload["requests_today"] == 5
