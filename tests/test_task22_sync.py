"""TASK-22: Sync now (POST /api/sync: one at a time, a minute's cooldown per bank, behind the gate and CSRF), asking the
banks for fresh data (Plaid's refresh endpoints: the setting, the plan, eligible Items, 30 minutes' cooldown), the
finance_sync tool (plugin + MCP: no card, except a billed refresh), data_age, and the payment in transit (a card payment
that left the bank but the card hasn't posted yet, counted as paid). Plaid is always the FakePlaid; every endpoint
sequence is asserted."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

import test_task20_merge as walk
from agent_tools import mcp_session, plugin_ctx, run_cli
from conftest import TOKEN_A, TOKEN_B, TOKEN_NEW, account, add_item, err, page, txn
from factory import acct, bank, snapshot, tx
from hermes_finance import api, auth, cli, config, db, insights, mcp_state, prefs, reads, report, syncnow, tooldefs, transit
from hermes_finance.web import App

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
SYNC = ["/item/get", "/transactions/sync", "/accounts/get"]          # one Item's sync, in order
CARD = account("acc_cc", "CREDIT CARD", "1234", "credit", "credit card", current=450.00, available=4550.00, limit=5000.0)
PAID = account("acc_cc", "CREDIT CARD", "1234", "credit", "credit card", current=0.0, available=5000.0, limit=5000.0)
TODAY = date(2026, 9, 28)


def post(app, body, headers=JSONH):
    status, _, out = app.handle("POST", "/api/sync", headers, json.dumps(body).encode())
    return status, json.loads(out)


@pytest.fixture
def chase(conn, fake):
    """One bank (Chase: checking + the ••1234 card owing $450.00); its next sync brings 3 transactions and the card at
    $0.00."""
    add_item(conn)
    acct(conn, "acc_chk", "item_a", "Checking", current=1000.0)
    acct(conn, "acc_cc", "item_a", "CREDIT CARD", type_="credit", subtype="credit card", current=450.00, limit=5000.0,
         mask="1234")
    fake.on("/transactions/sync", page(added=[txn("t1"), txn("t2", 20), txn("t3", 30)], accounts=[account(), CARD]))
    fake.on("/accounts/get", {"accounts": [account(), PAID]})
    fake.on("/transactions/refresh", {"request_id": "req-refresh"})
    fake.on("/investments/refresh", {"request_id": "req-refresh"})
    return conn


@pytest.fixture
def app(client):
    a = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def later(monkeypatch, seconds):
    at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    monkeypatch.setattr(syncnow, "clock", lambda: at)


def set_prefs(conn, **values):
    prefs.save(conn, {f"plaid.{k}": v for k, v in values.items()}, allowed=list(prefs.SPEC))


# --- 1. Sync now ------------------------------------------------------------------------------------------------------

def test_sync_now_says_what_changed(chase, app, fake):
    status, r = post(app, {})
    assert status == 200 and fake.paths() == SYNC
    c = r["changes"]
    assert (c["new"], c["updated"], c["removed"]) == (3, 0, 0)
    assert c["balances"] == [{"account_id": "acc_cc", "label": "Chase ••1234", "before": 450.00, "after": 0.0,   # (TASK-22f:
                              "item_id": "item_a", "bank": "Chase", "institution_id": "ins_1", "name": "CREDIT CARD",  # + the
                              "mask": "1234", "type": "credit", "subtype": "credit card"}]                            # panel's)
    assert c["text"] == "3 new transactions · Chase ••1234 $450 → $0"
    assert r["ok"] is True and r["errors"] == [] and r["data_age"] == "synced just now" and r["last_sync"]
    assert [(i["item_id"], i["added"]) for i in r["items"]] == [("item_a", 3)]
    assert "refresh_requested" not in r and r["refresh"] == {"allowed": True, "billed": False, "plan": "trial", "sandbox": False,
                                                             "banks": [{"item_id": "item_a", "institution_name": "Chase",
                                                                        "institution_id": "ins_1", "asked_ago": None,
                                                                        "retry_after": 0}]}   # (TASK-22f: + per bank)
    assert chase.execute("SELECT current_balance FROM accounts WHERE account_id = 'acc_cc'").fetchone()[0] == 0.0


def test_nothing_new_and_a_broken_bank(chase, app, fake, monkeypatch):
    fake.on("/transactions/sync", page(accounts=[account(), CARD]))
    fake.on("/accounts/get", {"accounts": [account(), CARD]})
    status, r = post(app, {})
    assert status == 200 and r["changes"]["text"] == "Nothing new" and r["changes"]["balances"] == []
    later(monkeypatch, 61)
    fake.on("/transactions/sync", err("ITEM_LOGIN_REQUIRED"))
    status, r = post(app, {})
    assert status == 200 and r["ok"] is False and r["items"][0]["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert r["errors"] == ["Couldn't sync Chase.", "Reconnect Chase on Accounts."]     # (TASK-22c's wording)
    assert r["items"][0]["error"] == "Chase: ERROR ITEM_LOGIN_REQUIRED (open `finance serve` and click Reconnect)"


def test_a_second_sync_while_one_runs_gets_409(chase, app, fake):
    started, go, first = threading.Event(), threading.Event(), []

    def slow(body):
        started.set()
        assert go.wait(10)
        return page(accounts=[account(), CARD])

    fake.on("/transactions/sync", slow)
    t = threading.Thread(target=lambda: first.append(post(app, {})))
    t.start()
    try:
        assert started.wait(10)
        calls = len(fake.calls)
        assert post(app, {}) == (409, {"error": "A sync is already running; try again when it's done.", "busy": True})
        assert post(app, {"refresh": True})[0] == 409
        assert len(fake.calls) == calls                                  # the refused ones called nothing
    finally:
        go.set()
        t.join(10)
    assert first[0][0] == 200 and not syncnow.LOCK.locked()


def test_the_cooldown_is_60_seconds_per_bank(chase, app, fake, monkeypatch):
    assert post(app, {})[0] == 200
    status, r = post(app, {})
    assert status == 429 and 55 <= r["retry_after"] <= 60 and r["error"].startswith("Synced a moment ago. Try again in ")
    assert fake.paths() == SYNC                                          # the second one called nothing
    later(monkeypatch, 61)
    assert post(app, {})[0] == 200 and fake.paths() == SYNC * 2


def test_the_cooldown_leaves_out_only_the_bank_just_synced(chase, app, fake):
    add_item(chase, "item_b", "ins_2", name="Amex", token=TOKEN_B)
    status, r = post(app, {"item_id": "item_a"})
    assert status == 200 and [b["access_token"] for b in fake.bodies("/item/get")] == [TOKEN_A]
    status, r = post(app, {})                                             # every bank: Chase waits, Amex syncs
    assert status == 200 and [i["item_id"] for i in r["items"]] == ["item_b"]
    assert [w["item_id"] for w in r["waiting"]] == ["item_a"] and 1 <= r["waiting"][0]["retry_after"] <= 61
    assert [b["access_token"] for b in fake.bodies("/item/get")] == [TOKEN_A, TOKEN_B]
    assert post(app, {})[0] == 429
    assert post(app, {"item_id": "nope"}) == (404, {"error": "no connected bank with item_id 'nope'"})
    assert post(app, {"item_id": 5})[0] == 400 and post(app, {"refresh": "yes"})[0] == 400


def test_the_cooldown_holds_across_processes(chase, app, fake, capsys, cli_client):
    """`finance sync` (the cron, another process) writes sync_log: a tap right after it waits too. The CLI itself has no
    cooldown and prints the same summary as the toast."""
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "what changed: 3 new transactions · Chase ••1234 $450 → $0"
    assert post(app, {})[0] == 429


def test_sync_now_is_behind_the_gate_and_csrf(chase, fake, client, monkeypatch):
    from argon2 import PasswordHasher
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    assert ("POST", "/api/sync") in walk.WRITES                                    # TASK-20's route walk has it
    local = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    assert post(local, {}, {**JSONH, "Origin": "https://evil.example"})[0] == 403
    assert post(local, {}, {**JSONH, "Host": "evil.example"})[0] == 403
    assert post(local, {}, {**JSONH, "X-Forwarded-For": "1.2.3.4"})[0] == 403     # "This computer only": nothing proxied
    with chase:
        chase.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                      "VALUES ('owner', ?, ?, ?, ?)", (auth.hash_secret("pw"), auth.new_totp_secret(), db.now(), db.now()))
    prefs.save(chase, {"access.mode": "builtin"}, allowed=["access.mode"])
    token, csrf = auth.create_session(chase, 1)
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    cookie = {"Cookie": f"{auth.COOKIE}={token}"}
    status, r = post(app, {})
    assert status == 401 and r["signin"] is True
    for bad in ({}, {"X-CSRF-Token": "nope"}):
        status, r = post(app, {"refresh": True}, {**JSONH, **cookie, **bad})
        assert status == 403 and "CSRF" in r["error"]
    assert fake.calls == []
    status, r = post(app, {}, {**JSONH, **cookie, "X-CSRF-Token": csrf})
    assert status == 200 and fake.paths() == SYNC


# --- 2. Ask banks for fresh data --------------------------------------------------------------------------------------

def test_refresh_asks_then_syncs_right_away(chase, app, fake):
    status, r = post(app, {"refresh": True})
    assert status == 200 and fake.paths() == ["/transactions/refresh", *SYNC]
    assert fake.bodies("/transactions/refresh") == [{"access_token": TOKEN_A}]
    assert r["refresh_requested"] == [{"item_id": "item_a", "institution_name": "Chase", "products": ["transactions"],
                                       "ok": True}]
    assert r["note"] == ("Banks asked for fresh data — new items usually land within a few minutes; tap Sync again "
                         "then.")
    assert r["changes"]["new"] == 3


def test_refresh_only_on_eligible_items(chase, app, fake):
    """Transactions -> /transactions/refresh, Investments -> /investments/refresh too; an Item that needs reconnecting
    (or a removed one) isn't asked. Every refresh comes before the syncs."""
    with chase:
        chase.execute("UPDATE items SET products = '[\"investments\", \"transactions\"]' WHERE item_id = 'item_a'")
    add_item(chase, "item_b", "ins_2", name="Amex", token=TOKEN_B, status="login_required")
    add_item(chase, "item_c", "ins_3", name="Citi", token=TOKEN_NEW)
    add_item(chase, "item_d", "ins_4", name="Gone", token="access-production-dddd", removed=True)
    with chase:   # (the order they were connected in)
        for i, item in enumerate(("item_a", "item_b", "item_c", "item_d")):
            chase.execute("UPDATE items SET created_at = ? WHERE item_id = ?", (f"2026-01-0{i + 1}T00:00:00Z", item))
    products = {TOKEN_A: ["investments", "transactions"], TOKEN_B: ["transactions"], TOKEN_NEW: ["transactions"]}
    fake.on("/item/get", lambda b: {"item": {"products": products[b["access_token"]]}})
    fake.on("/investments/holdings/get", {"accounts": [], "holdings": [], "securities": []})
    status, r = post(app, {"refresh": True})
    assert status == 200
    asked = sorted((p, b["access_token"]) for p, b in fake.calls if p.endswith("/refresh"))   # (HOTFIX-1: all at once)
    assert asked == sorted([("/transactions/refresh", TOKEN_A), ("/investments/refresh", TOKEN_A),
                            ("/transactions/refresh", TOKEN_NEW)])
    assert all(p.endswith("/refresh") for p in fake.paths()[:3])
    assert "/refresh" not in "".join(fake.paths()[3:])
    assert [a["item_id"] for a in r["refresh_requested"]] == ["item_a", "item_c"]
    assert [b["access_token"] for b in fake.bodies("/item/get")] == [TOKEN_A, TOKEN_B, TOKEN_NEW]   # all three synced


def test_refresh_has_a_30_minute_cooldown(chase, app, fake, monkeypatch):
    assert post(app, {"refresh": True})[0] == 200
    later(monkeypatch, 61)                                                 # the sync's minute has passed, not the 30
    status, r = post(app, {"refresh": True})
    assert status == 200 and r["refresh_requested"] == [] and r["refresh_waiting"][0]["item_id"] == "item_a"
    assert 1700 <= r["refresh_waiting"][0]["retry_after"] <= 1801 and "last 30 minutes" in r["note"]
    assert fake.paths() == ["/transactions/refresh", *SYNC, *SYNC]
    later(monkeypatch, 31 * 60)
    assert post(app, {"refresh": True})[1]["refresh_requested"][0]["item_id"] == "item_a"
    assert fake.paths().count("/transactions/refresh") == 2


def test_a_refused_refresh_still_syncs(chase, app, fake):
    fake.on("/transactions/refresh", err("PRODUCTS_NOT_SUPPORTED"))
    status, r = post(app, {"refresh": True})
    assert status == 200 and fake.paths() == ["/transactions/refresh", *SYNC]
    assert r["refresh_requested"] == [] and r["refresh_failed"][0]["error_code"] == "PRODUCTS_NOT_SUPPORTED"


@pytest.mark.parametrize("plan, chosen, env, allowed, billed", [
    ("trial", None, "production", True, False),        # the default: Trial, on
    ("paid", None, "production", False, True),         # paid: off unless turned on
    ("paid", True, "production", True, True),
    ("trial", False, "production", False, False),      # turned off on Trial
    ("paid", None, "sandbox", True, False),            # Sandbox is free: on
])
def test_refresh_allowed_or_blocked_by_the_setting_and_the_plan(chase, app, fake, plan, chosen, env, allowed, billed):
    set_prefs(chase, plan=plan, **({} if chosen is None else {"on_demand_refresh": chosen}))
    policy = syncnow.refresh_policy(chase, env)
    assert (policy["allowed"], policy["billed"], policy["set"]) == (allowed, billed, chosen is not None)
    if env != "production":
        return
    status, r = post(app, {"refresh": True})
    if allowed:
        assert status == 200 and fake.paths()[0] == "/transactions/refresh"
    else:
        assert status == 403 and fake.calls == []
        assert r["error"].startswith("On-demand bank refresh is off: Settings → Connections → Allow on-demand")
        assert ("Plaid bills each refresh" in r["error"]) == billed
        assert post(app, {})[0] == 200 and fake.paths() == SYNC            # a plain sync is always fine


def test_settings_shows_and_saves_the_plan_and_the_switch(chase, app, tmp_path):
    status, _, out = app.handle("GET", "/api/settings/page", LOCAL, b"")
    assert status == 200 and json.loads(out)["plaid"]["refresh"] == {
        "allowed": True, "set": False, "plan": "trial", "sandbox": False, "billed": False, "reason": None}
    status, _, out = app.handle("POST", "/api/prefs", JSONH, json.dumps({"plaid.plan": "paid"}).encode())
    assert status == 200
    refresh = json.loads(app.handle("GET", "/api/settings/page", LOCAL, b"")[2])["plaid"]["refresh"]
    assert (refresh["allowed"], refresh["billed"]) == (False, True)
    assert app.handle("POST", "/api/prefs", JSONH, json.dumps({"plaid.on_demand_refresh": True}).encode())[0] == 200
    assert json.loads(app.handle("GET", "/api/settings/page", LOCAL, b"")[2])["plaid"]["refresh"]["allowed"] is True
    assert app.handle("POST", "/api/prefs", JSONH, json.dumps({"plaid.plan": "free"}).encode())[0] == 400
    assert app.handle("POST", "/api/prefs", JSONH, json.dumps({"plaid.demo": True}).encode())[0] == 400   # still not a page key


# --- 3. finance_sync --------------------------------------------------------------------------------------------------

@pytest.fixture
def agent(chase, client, monkeypatch):
    monkeypatch.setattr(syncnow, "make_client", lambda: client)
    return plugin_ctx()


def test_finance_sync_runs_without_a_card(agent, fake):
    [hook] = agent.hooks["pre_tool_call"]
    assert hook(tool_name="finance_sync", args={}, task_id="t") is None
    assert fake.calls == []
    out = agent.call("finance_sync")
    assert fake.paths() == SYNC and out["changes"]["text"].startswith("3 new transactions")
    assert out["data_age"] == "synced just now"
    again = agent.call("finance_sync")                                   # the same cooldown as the page
    assert again["error"].startswith("Synced a moment ago. Try again in ") and fake.paths() == SYNC


def test_finance_sync_refresh_on_trial_runs_directly(agent, fake):
    assert agent.hooks["pre_tool_call"][0](tool_name="finance_sync", args={"refresh": True}, task_id="t") is None
    out = agent.call("finance_sync", {"refresh": True})
    assert fake.paths() == ["/transactions/refresh", *SYNC] and out["refresh_requested"][0]["item_id"] == "item_a"


def test_finance_sync_refresh_on_a_paid_plan_asks_first(agent, chase, fake):
    set_prefs(chase, plan="paid", on_demand_refresh=True)
    add_item(chase, "item_b", "ins_2", name="Amex", token=TOKEN_B)
    directive = agent.call("finance_sync", {"refresh": True})["directive"]
    assert directive == {"action": "approve",
                         "message": "Ask your banks for fresh data (Plaid bills each refresh on your plan: 2 connections)"}
    assert fake.calls == []                                               # asking called nothing
    out = agent.approved("finance_sync", {"refresh": True})
    assert [p for p in fake.paths() if p.endswith("/refresh")] == ["/transactions/refresh"] * 2
    assert len(out["refresh_requested"]) == 2
    assert agent.call("finance_sync").get("error", "").startswith("Synced a moment ago")    # no card for a plain sync


def test_finance_sync_refresh_blocked_when_not_allowed(agent, chase, fake):
    set_prefs(chase, plan="paid")
    directive = agent.call("finance_sync", {"refresh": True})["directive"]
    assert directive["action"] == "block" and "On-demand bank refresh is off" in directive["message"]
    assert "Plaid bills each refresh" in directive["message"]
    assert "On-demand bank refresh is off" in json.loads(agent.tools["finance_sync"]({"refresh": True}))["error"]
    assert agent.call("finance_sync", {"refresh": "yes"})["directive"]["message"] == \
        "finance_sync: refresh must be true or false (nothing was changed)"
    assert fake.calls == []


def test_finance_sync_over_mcp(chase, client, fake, monkeypatch):
    """Listed without allow_writes and run directly (no pending action); a billed refresh is a pending action, applied
    once the owner approves it in Settings."""
    monkeypatch.setattr(syncnow, "make_client", lambda: client)
    path = config.db_path()
    listed, out = mcp_session(path, [("finance_sync", {})])
    assert "finance_sync" in listed and "finance_set_cap" not in listed and listed == list(tooldefs.UNGATED)
    assert not out[0][0] and json.loads(out[0][1])["changes"]["new"] == 3 and fake.paths() == SYNC
    assert chase.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 0
    set_prefs(chase, plan="paid", on_demand_refresh=True)
    _, out = mcp_session(path, [("finance_sync", {"refresh": True})], client="Cursor")
    pending = json.loads(out[0][1])
    assert pending["status"] == "pending_approval" and pending["applied"] is False
    assert pending["pending_action"]["summary"] == ("Ask your banks for fresh data (Plaid bills each refresh on your "
                                                    "plan: 1 connection)")
    assert fake.paths() == SYNC                                            # nothing asked yet
    app = App(path, lambda: client, "127.0.0.1", 8765)
    app.today = lambda: TODAY
    monkeypatch.setattr(syncnow, "clock", lambda: datetime.now(timezone.utc) + timedelta(seconds=61))
    status, _, body = app.handle("POST", "/api/mcp/pending/approve", JSONH,
                                 json.dumps({"id": pending["pending_action"]["id"]}).encode())
    action = json.loads(body)["action"]
    assert status == 200 and action["status"] == "applied" and action["result"]["refresh_requested"][0]["item_id"] == "item_a"
    assert fake.paths() == [*SYNC, "/transactions/refresh", *SYNC]
    set_prefs(chase, plan="trial")                                           # on Trial: directly, within the cooldown
    _, out = mcp_session(path, [("finance_sync", {"refresh": True})])
    assert json.loads(out[0][1])["refresh_waiting"][0]["item_id"] == "item_a"
    assert mcp_state.pending_count(chase) == 0


def test_15_tools_one_hook_and_the_manifest():
    p = plugin_ctx()
    assert len(p.tools) == 16 and len(p.hooks["pre_tool_call"]) == 1 and "finance_sync" in p.tools  # TASK-24: + finance_rules
    assert tooldefs.ACTIONS == ("finance_sync",) and "finance_sync" not in tooldefs.READS + tooldefs.WRITES
    assert tooldefs.TOOLS["finance_sync"].schema() == {
        "type": "object", "additionalProperties": False,
        "properties": {"refresh": tooldefs.TOOLS["finance_sync"].properties["refresh"]}}


def test_data_age(conn):
    at = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    assert report.data_age(conn, at) == "never synced"
    bank(conn, last_sync="2026-09-28T11:59:40Z")
    assert report.data_age(conn, at) == "synced just now"
    for last, want in (("2026-09-28T11:36:00Z", "synced 24 min ago"), ("2026-09-27T22:00:00Z", "synced 14 h ago"),
                       ("2026-09-25T12:00:00Z", "synced 3 d ago")):
        with conn:
            conn.execute("UPDATE items SET last_sync_at = ?", (last,))
        assert report.data_age(conn, at) == want
    assert report.status(conn)["data_age"].startswith("synced ")


def test_finance_status_carries_data_age(chase, monkeypatch):
    with chase:
        chase.execute("UPDATE items SET last_sync_at = '2026-09-27T22:00:00Z'")
    out = plugin_ctx().call("finance_status")
    assert out["data_age"] == report.data_age(chase) and out["data_age"].startswith("synced ")


# --- 4. Payment in transit --------------------------------------------------------------------------------------------

def real_shape(conn, card_owes=450.00, pay_name="CHASE CREDIT CRD AUTOPAY", sav_name="Transfer to Harbor Checking"):
    """Sep 27: $450.00 Harbor Savings -> Harbor Checking -> "CHASE CREDIT CRD"; on Sep 28 the Chase ••1234 card still owes
    it (synthetic)."""
    bank(conn, "item_harbor", "Harbor")
    bank(conn, "item_chase", "Chase")
    acct(conn, "acc_sav", "item_harbor", "Harbor Savings", subtype="savings", current=5000.0, mask="7781")
    acct(conn, "acc_chk", "item_harbor", "Harbor Checking", current=1200.0, mask="4410")
    acct(conn, "acc_cc", "item_chase", "CREDIT CARD", type_="credit", subtype="credit card", current=card_owes,
         limit=5000.0, mask="1234")
    tx(conn, "2026-09-27", 450.00, name=sav_name, cat="Transfers", account_id="acc_sav", item_id="item_harbor", tid="sav-out")
    tx(conn, "2026-09-27", -450.00, name="Transfer from Harbor Savings", cat="Transfers", account_id="acc_chk",
       item_id="item_harbor", tid="chk-in")
    tx(conn, "2026-09-27", 450.00, name=pay_name, cat="Card payment", account_id="acc_chk", item_id="item_harbor",
       tid="chk-pay")


def card_in(conn, amount, day="2026-09-28", tid=None):
    tx(conn, day, -amount, name="Payment Thank You", cat="Card payment", account_id="acc_cc", item_id="item_chase", tid=tid)


def test_the_real_shape_is_a_payment_in_transit(conn):
    real_shape(conn)
    [e] = transit.entries(conn, TODAY)
    assert (e["transaction_id"], e["date"], e["amount"], e["card_id"], e["label"]) == \
        ("chk-pay", "2026-09-27", 450.00, "acc_cc", "Chase ••1234")
    assert e["line"] == "Payment in transit: $450 to Chase ••1234 (sent Sep 27)"
    assert e["from_account"] == {"name": "Harbor Checking", "mask": "4410"}


def test_net_worth_adds_it_back_everywhere_and_touches_nothing_stored(conn, monkeypatch, capsys):
    real_shape(conn)
    for aid, before, today_ in (("acc_sav", 5450.00, 5000.0), ("acc_chk", 1200.0, 1200.0), ("acc_cc", 450.00, 450.00)):
        snapshot(conn, aid, "2026-09-26", before)
        snapshot(conn, aid, "2026-09-28", today_)
    stored = [tuple(r) for r in conn.execute("SELECT account_id, current_balance FROM accounts ORDER BY 1")]
    snaps = [tuple(r) for r in conn.execute("SELECT * FROM balance_snapshots ORDER BY 1, 2")]
    plain = insights.net_worth(conn)                                       # (no today: as the banks report it)
    nw = insights.net_worth(conn, today=TODAY)
    assert plain["in_transit"] == [] and (plain["net"], plain["debts"]) == (5750.00, 450.00)
    assert (nw["assets"], nw["debts"], nw["net"]) == (6200.0, 0.0, 6200.0)
    assert (nw["custom"]["debts"], nw["custom"]["net"]) == (0.0, 6200.0)   # the card counts in Custom (the default)
    assert nw["in_transit"][0]["counted_custom"] is True
    assert [g["total"] for g in nw["breakdown"] if g["key"] == "cards"] == [450.00]   # the card as the bank reports it
    points = {p["date"]: p for p in nw["history"]}
    assert (points["2026-09-28"]["net"], points["2026-09-28"]["debts"], points["2026-09-28"]["custom_net"]) == (6200.0, 0.0, 6200.0)
    assert points["2026-09-26"] == {p["date"]: p for p in plain["history"]}["2026-09-26"]      # only today's point moves
    # Overview, Accounts (and the sidebar, drawn from it), finance networth, finance_networth
    ov = api.overview(conn, {}, TODAY)["net_worth"]
    assert ov["net"] == 6200.0 and ov["in_transit"] == nw["in_transit"]
    assert api.accounts(conn, {}, TODAY)["net_worth"]["net"] == 6200.0
    cli_nw = reads.networth(conn, TODAY, history=True)
    assert cli_nw["net"] == 6200.0 and cli_nw["in_transit"] == nw["in_transit"] and cli_nw["history"][-1]["net"] == 6200.0
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    code, out, _ = run_cli(["networth"], capsys)
    assert code == 0 and out.splitlines()[0].startswith("Net worth $6,200")
    assert "  Payment in transit: $450 to Chase ••1234 (sent Sep 27), counted as paid" in out.splitlines()
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)
    tool = plugin_ctx().call("finance_networth", {"history": True})
    assert tool == json.loads(json.dumps(cli_nw)) and tool["net"] == 6200.0
    assert [tuple(r) for r in conn.execute("SELECT account_id, current_balance FROM accounts ORDER BY 1")] == stored
    assert [tuple(r) for r in conn.execute("SELECT * FROM balance_snapshots ORDER BY 1, 2")] == snaps


def test_custom_counts_it_only_while_the_card_counts(conn):
    real_shape(conn)
    insights.save_networth_excluded(conn, ["acc_cc"])
    nw = insights.net_worth(conn, today=TODAY)
    assert nw["net"] == 6200.0 and nw["in_transit"][0]["counted_custom"] is False
    assert (nw["custom"]["debts"], nw["custom"]["net"]) == (0.0, 6200.0)   # the card isn't counted: nothing to add back
    insights.save_networth_excluded(conn, ["acc_sav"])
    nw = insights.net_worth(conn, today=TODAY)
    assert (nw["custom"]["assets"], nw["custom"]["debts"], nw["custom"]["net"]) == (1200.0, 0.0, 1200.0)
    assert reads.networth(conn, TODAY, custom=True)["in_transit"][0]["transaction_id"] == "chk-pay"
    insights.save_networth_excluded(conn, ["acc_cc"])
    assert reads.networth(conn, TODAY, custom=True)["in_transit"] == []
    assert len(reads.networth(conn, TODAY)["in_transit"]) == 1


def test_gone_once_the_card_posts_it(conn):
    real_shape(conn)
    card_in(conn, 450.00)
    assert transit.entries(conn, TODAY) == []
    assert insights.net_worth(conn, today=TODAY)["net"] == 5750.00


def test_gone_after_5_days(conn):
    real_shape(conn)
    assert len(transit.entries(conn, date(2026, 10, 2))) == 1               # 5 days after
    assert transit.entries(conn, date(2026, 10, 3)) == []                   # 6 days: gone by itself


@pytest.mark.parametrize("name", ["ONLINE PAYMENT THANK YOU", "CREDIT CARD AUTOPAY", "CITI CARD ONLINE PAYMENT"])
def test_a_card_it_cant_name_gets_nothing(conn, name):
    real_shape(conn, pay_name=name)
    assert transit.entries(conn, TODAY) == []


def test_two_cards_at_the_bank_need_the_last_4_digits(conn):
    real_shape(conn)
    acct(conn, "acc_cc2", "item_chase", "SAPPHIRE", type_="credit", subtype="credit card", current=2000.0, mask="1111")
    assert transit.entries(conn, TODAY) == []                               # which Chase card? no guess
    with conn:
        conn.execute("UPDATE transactions SET name = 'CHASE CREDIT CRD EPAY ...1234' WHERE transaction_id = 'chk-pay'")
    assert [e["card_id"] for e in transit.entries(conn, TODAY)] == ["acc_cc"]


def test_a_transfer_between_your_own_bank_accounts_is_nothing(conn):
    real_shape(conn, sav_name="Move to checking for the Chase card")          # card-payment words, but own transfer
    assert [e["transaction_id"] for e in transit.entries(conn, TODAY)] == ["chk-pay"]
    with conn:
        conn.execute("DELETE FROM transactions WHERE transaction_id = 'chk-pay'")
    assert transit.entries(conn, TODAY) == []


@pytest.mark.parametrize("inflow, gone", [(500.0, False), (449.98, False), (450.01, True), (450.00, True)])
def test_only_an_exact_amount_answers_it(conn, inflow, gone):
    real_shape(conn)
    card_in(conn, inflow)
    assert (transit.entries(conn, TODAY) == []) == gone


def test_one_inflow_answers_one_payment_and_a_card_owing_less_gets_nothing(conn):
    real_shape(conn, card_owes=900.00)
    tx(conn, "2026-09-28", 450.00, name="CHASE CREDIT CRD AUTOPAY", cat="Card payment", account_id="acc_chk",
       item_id="item_harbor", tid="chk-pay-2")
    card_in(conn, 450.01, tid="one-inflow")                                  # a cent off: answers one of the two
    assert [e["transaction_id"] for e in transit.entries(conn, TODAY)] == ["chk-pay-2"]
    with conn:
        conn.execute("UPDATE accounts SET current_balance = 0 WHERE account_id = 'acc_cc'")
    assert transit.entries(conn, TODAY) == []                                # the balance already took it
