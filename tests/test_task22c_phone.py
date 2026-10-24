"""TASK-22c: the phone pass on Sync now. A passing Plaid error (NETWORK_ERROR and the like) is logged but never saved on
the Item; a sync where every bank failed starts no cooldown and says "Couldn't reach Plaid" once (the page, finance_sync
over the plugin and MCP, `finance sync`), mixed failures are one line plus one for who to reconnect, and the minute's
cooldown reads "Synced a moment ago. Try again in N s.". Plaid off is the real client with HERMES_PLAID=off (conftest):
every call fails as on a Wi-Fi drop."""

from __future__ import annotations

import json

import pytest

from agent_tools import mcp_session, plugin_ctx, run_cli
from conftest import FAKE_CLIENT_ID, FAKE_SECRET, TOKEN_A, TOKEN_B, add_item, err, item_row, page, txn
from hermes_finance import config, syncnow
from hermes_finance.config import Settings
from hermes_finance.plaid import PlaidClient
from hermes_finance.sync import is_transient, sync_all
from hermes_finance.web import App

JSONH = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
UNREACHABLE = "Couldn't reach Plaid. Nothing changed; try again in a minute."
BANKS = [("item_a", "Chase", TOKEN_A), ("item_b", "Harbor", TOKEN_B), ("item_c", "Amex", "access-c"),
         ("item_d", "Juniper", "access-d")]


def post(app, body=None):
    status, _, out = app.handle("POST", "/api/sync", JSONH, json.dumps(body or {}).encode())
    return status, json.loads(out)


@pytest.fixture
def offline():
    """The real Plaid client, switched off (HERMES_PLAID=off): each call raises NETWORK_ERROR."""
    return PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"))


@pytest.fixture
def banks(conn):
    for item_id, name, token in BANKS:
        add_item(conn, item_id, f"ins_{item_id}", name=name, token=token)
    return conn


def statuses(conn):
    return [tuple(r) for r in conn.execute("SELECT item_id, status, error_code FROM items ORDER BY item_id")]


# --- 2. one network blip doesn't mark every bank as broken ------------------------------------------------------------

def test_a_network_error_leaves_a_healthy_item_as_it_was(conn, offline):
    add_item(conn)
    (r,) = sync_all(conn, offline)
    assert not r.ok and r.error_code == "NETWORK_ERROR"
    assert item_row(conn)["status"] == "active" and item_row(conn)["error_code"] is None
    assert item_row(conn)["error_message"] is None
    assert conn.execute("SELECT error FROM sync_log").fetchone()[0].startswith("NETWORK_ERROR: ")   # still logged


def test_an_item_that_needs_a_login_stays_that_way(conn, offline):
    add_item(conn, status="login_required")
    with conn:
        conn.execute("UPDATE items SET error_code = 'ITEM_LOGIN_REQUIRED', error_message = 'the login changed'")
    sync_all(conn, offline)
    row = item_row(conn)
    assert (row["status"], row["error_code"], row["error_message"]) == ("login_required", "ITEM_LOGIN_REQUIRED",
                                                                        "the login changed")


@pytest.mark.parametrize("code, error_type", [
    ("INTERNAL_SERVER_ERROR", "API_ERROR"), ("PLANNED_MAINTENANCE", "API_ERROR"),
    ("RATE_LIMIT_EXCEEDED", "RATE_LIMIT_EXCEEDED"), ("TRANSACTIONS_SYNC_LIMIT", "RATE_LIMIT_EXCEEDED"),
    ("ACCOUNTS_LIMIT", "RATE_LIMIT_EXCEEDED"), ("RATE_LIMIT", "API_ERROR"),
    ("INSTITUTION_DOWN", "INSTITUTION_ERROR"), ("INSTITUTION_NOT_RESPONDING", "INSTITUTION_ERROR"),
])
def test_passing_errors_are_logged_not_saved(conn, client, fake, code, error_type):
    add_item(conn)
    fake.on("/transactions/sync", err(code, error_type))
    (r,) = sync_all(conn, client)
    assert not r.ok and r.error_code == code and is_transient(code, error_type)
    assert (item_row(conn)["status"], item_row(conn)["error_code"]) == ("active", None)
    assert conn.execute("SELECT error FROM sync_log").fetchone()[0].startswith(f"{code}: ")


@pytest.mark.parametrize("code, status", [("ITEM_LOGIN_REQUIRED", "login_required"), ("ITEM_NOT_FOUND", "error"),
                                          ("INVALID_ACCESS_TOKEN", "error")])
def test_real_item_errors_are_still_saved(conn, client, fake, code, status):
    add_item(conn)
    fake.on("/transactions/sync", err(code, "ITEM_ERROR" if code.startswith("ITEM") else "INVALID_INPUT"))
    sync_all(conn, client)
    assert (item_row(conn)["status"], item_row(conn)["error_code"]) == (status, code)


def test_is_transient_edges():
    assert is_transient("HTTP_503", "API_ERROR") and is_transient("HTTP_429", "API_ERROR")   # no error body
    assert not is_transient("ITEM_LOGIN_REQUIRED", "ITEM_ERROR") and not is_transient("HTTP_400", "API_ERROR")
    assert not is_transient(None) and not is_transient("INSTITUTION_NOT_AVAILABLE", "INSTITUTION_ERROR")


# --- 3. what a failed sync says, and no cooldown after one ------------------------------------------------------------

def test_plaid_out_of_reach_is_one_line_and_starts_no_cooldown(banks, offline):
    app = App(config.db_path(), lambda: offline, "127.0.0.1", 8765)
    before = statuses(banks)
    status, r = post(app)
    assert status == 200 and r["ok"] is False and r["synced"] == 0
    assert r["message"] == UNREACHABLE and r["errors"] == [UNREACHABLE] and r["items"] == []
    assert r["changes"]["text"] is None and "Nothing new" not in json.dumps(r)
    assert r["last_sync"] is None and r["data_age"] != "synced just now"               # no "Synced just now"
    assert statuses(banks) == before                                                    # all 4 still active
    assert banks.execute("SELECT COUNT(*) FROM sync_log WHERE error LIKE 'NETWORK_ERROR:%'").fetchone()[0] == 4
    status, again = post(app)                                                           # no cooldown: it runs again
    assert status == 200 and again["message"] == UNREACHABLE
    assert banks.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 8


def test_mixed_failures_are_grouped_with_a_reconnect_line(banks, app_for, fake):
    """Chase needs a login, Harbor's network dropped, Amex's bank is down, Juniper syncs one transaction."""
    by_token = {TOKEN_A: err("ITEM_LOGIN_REQUIRED"), "access-c": err("INSTITUTION_DOWN", "INSTITUTION_ERROR")}

    def sync(body):
        if body["access_token"] == TOKEN_B:
            raise OSError("connection reset")
        return by_token.get(body["access_token"]) or page(added=[txn("t1")])

    fake.on("/transactions/sync", sync)
    app = app_for()
    status, r = post(app)
    assert status == 200 and r["synced"] == 1 and len(r["items"]) == 4
    assert r["errors"] == ["Couldn't sync 3 banks (Chase, Harbor, Amex).", "Reconnect Chase on Accounts."]
    assert r["message"] == "Couldn't sync 3 banks (Chase, Harbor, Amex). Reconnect Chase on Accounts."
    assert r["changes"]["new"] == 1 and r["changes"]["text"] == "1 new transaction"            # Juniper's
    assert statuses(banks)[:3] == [("item_a", "login_required", "ITEM_LOGIN_REQUIRED"), ("item_b", "active", None),
                                   ("item_c", "active", None)]
    status, again = post(app)          # the failed three sync again at once; Juniper, which synced, waits its minute
    assert status == 200 and [i["item_id"] for i in again["items"]] == ["item_a", "item_b", "item_c"]
    assert [w["item_id"] for w in again["waiting"]] == ["item_d"]
    assert "new transaction" not in again["message"] and again["changes"]["text"] is None


def test_every_bank_failing_with_different_errors_is_mixed(banks, app_for, fake):
    fake.on("/transactions/sync", lambda b: err("INSTITUTION_DOWN", "INSTITUTION_ERROR") if b["access_token"] == TOKEN_A
            else err("INTERNAL_SERVER_ERROR", "API_ERROR"))
    status, r = post(app_for())
    assert r["synced"] == 0 and r["errors"] == ["Couldn't sync 4 banks (Chase, Harbor, Amex, Juniper)."]
    assert r["message"] == r["errors"][0]                                   # no "Nothing new", no reconnect line
    assert all(s[1:] == ("active", None) for s in statuses(banks))


def test_eight_banks_stay_within_two_short_lines(conn, app_for, fake):
    names = ["Wells Fargo", "Capital One", "Discover", "Chase", "Harbor", "Juniper", "Amex", "Citi"]
    for k, name in enumerate(names):
        add_item(conn, f"item_{k}", f"ins_{k}", name=name, token=f"access-{k}")
    fake.on("/transactions/sync", lambda b: err("ITEM_LOGIN_REQUIRED") if b["access_token"] in ("access-0", "access-3")
            else err("INSTITUTION_NOT_RESPONDING", "INSTITUTION_ERROR"))
    r = post(app_for())[1]
    assert r["message"] == "Couldn't sync 8 banks (Wells Fargo +7). Reconnect 2 banks on Accounts."
    assert len(syncnow.UNREACHABLE) <= syncnow.TOAST_ROOM                    # two lines of the toast at 360px


def test_names_fit_the_toast():
    from hermes_finance.sync import ItemSyncResult

    def failed(names, login=()):
        codes = [("ITEM_LOGIN_REQUIRED", "ITEM_ERROR") if n in login else ("INSTITUTION_DOWN", "INSTITUTION_ERROR") if k % 2
                 else ("INTERNAL_SERVER_ERROR", "API_ERROR") for k, n in enumerate(names)]         # mixed, never one code
        return [ItemSyncResult(f"i{k}", n, ok=False, error_code=c, error_type=t) for k, (n, (c, t)) in enumerate(zip(names, codes))]

    eight = ["Wells Fargo", "Capital One", "Discover", "Chase", "Harbor", "Juniper", "Amex", "Citi"]
    assert syncnow.failure(failed(eight))["lines"] == ["Couldn't sync 8 banks (Wells Fargo, Capital One, Discover, Chase +4)."]
    long = "Navy Federal Credit Union of the North"
    lines = syncnow.failure(failed([long, *eight], login=(long,)))["lines"]
    assert lines == ["Couldn't sync 9 banks.", "Reconnect 1 bank on Accounts."]           # no name fits
    lines = syncnow.failure(failed(["Chase", long], login=("Chase",)))["lines"]
    assert lines == ["Couldn't sync 2 banks (Chase +1).", "Reconnect Chase on Accounts."]
    for case in (failed(eight, ("Chase",)), failed(eight * 2, eight), failed([long] * 3, (long,)), failed(eight[:3], ("Harbor",))):
        assert len(" ".join(syncnow.failure(case)["lines"])) <= syncnow.TOAST_ROOM


def test_the_cooldown_message(banks, app_for, fake):
    app = app_for()
    assert post(app)[0] == 200
    status, r = post(app)
    assert status == 429 and r["error"] == f"Synced a moment ago. Try again in {r['retry_after']} s."
    assert 55 <= r["retry_after"] <= 60


def test_a_refresh_that_never_reached_the_bank_can_be_asked_again(conn, offline):
    add_item(conn)
    app = App(config.db_path(), lambda: offline, "127.0.0.1", 8765)
    status, r = post(app, {"refresh": True})
    assert status == 200 and r["refresh_failed"][0]["error_code"] == "NETWORK_ERROR" and r["message"] == UNREACHABLE
    assert syncnow._refresh_log(conn) == {}                                  # no 30-minute wait was started
    assert r["note"] == UNREACHABLE
    assert "last 30 minutes" not in post(app, {"refresh": True})[1]["note"]


# --- the same words for finance_sync (plugin + MCP) and `finance sync` ------------------------------------------------

def test_finance_sync_says_it_once(banks, offline, monkeypatch):
    monkeypatch.setattr(syncnow, "make_client", lambda: offline)
    out = plugin_ctx().call("finance_sync")
    assert out["message"] == UNREACHABLE and out["errors"] == [UNREACHABLE] and out["items"] == []
    _, calls = mcp_session(config.db_path(), [("finance_sync", {})])
    got = json.loads(calls[0][1])
    assert got["message"] == UNREACHABLE and got["errors"] == [UNREACHABLE] and got["items"] == []
    assert "NETWORK_ERROR" not in calls[0][1]
    assert all(s[1:] == ("active", None) for s in statuses(banks))


def test_finance_sync_cli_says_it_once(banks, offline, monkeypatch, capsys):
    from hermes_finance import cli
    monkeypatch.setattr(cli, "_client", lambda: offline)
    code, out, _ = run_cli(["sync"], capsys)
    assert code == 1 and out.splitlines() == [UNREACHABLE]


def test_cli_mixed_and_nothing_synced(conn, fake, capsys, cli_client):
    add_item(conn)
    add_item(conn, "item_b", "ins_2", name="Harbor", token=TOKEN_B)
    fake.on("/transactions/sync", lambda b: err("ITEM_LOGIN_REQUIRED") if b["access_token"] == TOKEN_A
            else page(added=[txn("tb1", 5)], next_cursor="cb"))
    code, out, _ = run_cli(["sync"], capsys)
    assert code == 0 and out.splitlines()[-3:] == ["synced 1/2 item(s)", "what changed: 1 new transaction",
                                                   "Couldn't sync Chase. Reconnect Chase on Accounts."]
    fake.on("/transactions/sync", err("ITEM_LOGIN_REQUIRED"))
    code, out, _ = run_cli(["sync"], capsys)
    assert code == 1 and "what changed" not in out and "Nothing new" not in out
    assert out.splitlines()[-2:] == ["synced 0/2 item(s)", "Couldn't sync 2 banks (Chase, Harbor). Reconnect 2 banks on Accounts."]


@pytest.fixture
def app_for(client):
    return lambda: App(config.db_path(), lambda: client, "127.0.0.1", 8765)
