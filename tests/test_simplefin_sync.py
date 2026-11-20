"""SimpleFIN sync: claim and confirm create namespaced rows; a repeat sync is idempotent; the budget is shared by every
process; the first sync chunks a year; the daily run overlaps five days; pending rows are matched one-to-one; mixed
Plaid/SimpleFIN duplicates keep Plaid's totals until an explicit pick; disconnect/reconnect never orphans or doubles a
row. The access URL never reaches the database, a log or an exception.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from hermes_finance import config, db, secretstore, simplefin, simplefin_sync

TODAY = date(2026, 11, 15)
ACCESS = "https://bridge-user:bridge-pass@bridge.example/simplefin"
TOKEN = base64.urlsafe_b64encode(b"https://bridge.example/simplefin/claim/synthetic").decode().rstrip("=")
PUBLIC = lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))]  # noqa: E731


def epoch(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())


def tx(tid, day, amount, desc, pending=False, payee=None):
    return {"id": tid, "posted": 0 if pending else epoch(day), "amount": amount, "description": desc,
            "transacted_at": epoch(day), "pending": int(pending), **({"payee": payee} if payee else {})}


def day(seconds) -> date:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).date()


def card(balance="100.00", available="900.00", txs=(), conn="CON-1", name="Everyday Checking", holdings=False):
    return {"id": "ACT-1", "name": name, "conn_id": conn, "currency": "USD", "balance": balance,
            "available-balance": available, "balance-date": epoch(TODAY),
            "transactions": list(txs), **({"holdings": [{"id": "H1", "symbol": "SYNTH", "market_value": "10.00"}]}
                                           if holdings else {})}


def account_set(accounts, connections=None, errlist=None):
    return {"errlist": errlist or [],
            "connections": connections or [{"conn_id": "CON-1", "name": "Synthetic Bank", "org_name": "Synthetic"}],
            "accounts": accounts}


class Bridge:
    """simplefin.Transport: a one-time claim POST and /accounts GETs with a per-window payload."""

    def __init__(self, payload=None, claim_status=200, accounts_status=200):
        self.payload = payload if payload is not None else account_set([card()])
        self.claim_status, self.accounts_status = claim_status, accounts_status
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        from urllib.parse import parse_qs, urlsplit
        parts = urlsplit(url)
        if method == "POST":
            self.calls.append(("claim", parts.path, {}))
            return simplefin.Response(self.claim_status, ACCESS.encode() if self.claim_status == 200
                                      else b"Forbidden (was it already claimed?)")
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append(("accounts", q))
        payload = self.payload(q) if callable(self.payload) else self.payload
        return simplefin.Response(self.accounts_status, json.dumps(payload).encode())

    def windows(self):
        return [(c[1]["start-date"], c[1].get("end-date")) for c in self.calls if c[0] == "accounts"]


def claimed(conn, bridge, today=TODAY, minute=7):
    return simplefin_sync.claim(conn, TOKEN, transport=bridge, resolve=PUBLIC, minute=minute, today=today)


def choices(*accounts, **overrides):
    out = {}
    for a in accounts or ({"id": "ACT-1"},):
        out[a["id"]] = {"type": overrides.get(a["id"], "checking")}
    return out


def confirmed(conn, bridge, today=TODAY, types=None, minute=7):
    claimed(conn, bridge, today=today, minute=minute)
    got = simplefin_sync.pending(conn)
    wanted = {}
    for a in got["accounts"]:
        key = a.get("key", a["id"])
        wanted[key] = {"type": (types or {}).get(key, "checking")}
    return simplefin_sync.confirm(conn, wanted, transport=bridge, resolve=PUBLIC, today=today,
                                  sleep=lambda s: None, jitter=0)


# --- claim and confirm --------------------------------------------------------------------------------

def test_claim_stores_the_access_url_only_in_the_secret_file_and_never_returns_it(conn, data_dir, caplog):
    caplog.set_level(logging.DEBUG)
    bridge = Bridge()
    got = claimed(conn, bridge)
    assert "bridge-pass" not in json.dumps(got)
    assert "bridge-pass" not in caplog.text and "bridge-pass" not in str(got)
    assert secretstore.simplefin_url() == ACCESS
    # nowhere in the database: every text value of every table
    dump = []
    for table in ("items", "accounts", "transactions", "settings", "simplefin_requests", "sync_log"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            dump.append(" ".join(str(v) for v in row if v is not None))
    haystack = "\n".join(dump)
    assert "bridge-pass" not in haystack and ACCESS not in haystack
    assert got["counts"] == {"banks": 1, "accounts": 1}
    assert got["accounts"][0]["guess"]["type"] == "depository"
    assert got["accounts"][0]["plaid"] is None


def test_confirm_creates_namespaced_items_accounts_and_transactions(conn):
    bridge = Bridge(account_set([
        card(txs=[tx("T1", date(2026, 11, 2), "-25.00", "Grocery store"),
                  tx("T2", date(2026, 11, 3), "100.00", "Refund")]),
        card(name="Synthetic Savings", balance="5000.00", available="5000.00", conn="CON-2")],
        connections=[{"conn_id": "CON-1", "name": "Synthetic Bank"},
                     {"conn_id": "CON-2", "name": "Synthetic Credit Union"}]))
    got = claimed(conn, bridge)
    wanted = {a["key"]: {"type": "investment" if a["name"] == "Synthetic Savings" else "checking"}
              for a in got["accounts"]}
    out = simplefin_sync.confirm(conn, wanted, transport=bridge, resolve=PUBLIC, today=TODAY,
                                 sleep=lambda s: None, jitter=0)
    assert out["connected"] and out["accounts"] == 2
    items = conn.execute("SELECT item_id, provider, access_token, institution_name FROM items ORDER BY item_id").fetchall()
    assert [tuple(r) for r in items] == [
        ("simplefin:CON-1", "simplefin", "", "Synthetic Bank"),
        ("simplefin:CON-2", "simplefin", "", "Synthetic Credit Union")]
    accounts = conn.execute("SELECT account_id, item_id, conn_id, type, subtype, current_balance FROM accounts "
                            "ORDER BY account_id").fetchall()
    assert [tuple(r) for r in accounts] == [
        ("simplefin:CON-1:ACT-1", "simplefin:CON-1", "CON-1", "depository", "checking", 100.0),
        # the savings card reuses ACT-1 in the fixture: namespaced by its own connection, still a distinct row
        ("simplefin:CON-2:ACT-1", "simplefin:CON-2", "CON-2", "investment", "brokerage", 5000.0)]
    txs = conn.execute("SELECT transaction_id, source, amount, pending, removed, name, merchant_name FROM transactions "
                       "ORDER BY transaction_id").fetchall()
    assert [tuple(r) for r in txs] == [
        ("simplefin:CON-1:T1", "simplefin", 25.0, 0, 0, "Grocery store", "Grocery store"),
        ("simplefin:CON-1:T2", "simplefin", -100.0, 0, 0, "Refund", "Refund")]
    # a repeat sync changes no id and adds no duplicate
    before = [tuple(r) for r in conn.execute("SELECT transaction_id, amount FROM transactions ORDER BY transaction_id")]
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    after = [tuple(r) for r in conn.execute("SELECT transaction_id, amount FROM transactions ORDER BY transaction_id")]
    assert after == before
    assert conn.execute("SELECT COUNT(*) FROM simplefin_requests").fetchone()[0] == len(bridge.windows())


def test_a_repeat_sync_does_not_resurrect_a_matched_pending_or_its_link(conn):
    pending = tx("P1", date(2026, 11, 2), "40.00", "Grocery store", pending=True)
    posted = tx("B1", date(2026, 11, 3), "48.00", "Grocery store")
    bridge = Bridge(account_set([card(txs=[pending, posted])]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE removed = 1").fetchone()[0] == 1
    link = conn.execute("SELECT pending_transaction_id FROM transactions WHERE transaction_id = 'simplefin:CON-1:B1'").fetchone()[0]
    assert link == "simplefin:CON-1:P1"
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 1
    assert conn.execute("SELECT pending_transaction_id FROM transactions WHERE transaction_id = 'simplefin:CON-1:B1'"
                        ).fetchone()[0] == "simplefin:CON-1:P1"
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 1


def test_plaid_ids_are_untouched_and_simplefin_ids_never_collide(conn):
    with conn:
        conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                     "VALUES ('item_plaid', 'ins_1', 'Chase', 'access-production-x', 'active', 'now')")
        conn.execute("INSERT INTO accounts (account_id, item_id, name, type, subtype, current_balance) "
                     "VALUES ('acc_plaid', 'item_plaid', 'Checking', 'depository', 'checking', 10)")
        conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, source, "
                     "first_seen_at, updated_at) VALUES ('T1', 'acc_plaid', 'item_plaid', '2026-11-02', 'Coffee', 5, "
                     "'plaid', 'now', 'now')")
    bridge = Bridge(account_set([card(txs=[tx("T1", date(2026, 11, 2), "25.00", "Coffee")])]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE transaction_id = 'T1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE transaction_id = 'simplefin:CON-1:T1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    assert conn.execute("SELECT provider FROM items WHERE item_id = 'item_plaid'").fetchone()[0] == "plaid"


# --- categorization -----------------------------------------------------------------------------------

def test_rules_apply_but_no_fake_plaid_category_is_filed(conn):
    from hermes_finance import classify
    bridge = Bridge(account_set([card(txs=[tx("T1", date(2026, 11, 2), "25.00", "Synthetic Grocer"),
                                          tx("T2", date(2026, 11, 3), "9.00", "Synthetic Coffee")])]))
    confirmed(conn, bridge)
    rows = {r["transaction_id"]: (r["category"], r["category_source"]) for r in conn.execute(
        "SELECT transaction_id, category, category_source FROM v_transactions ORDER BY transaction_id")}
    assert rows == {"simplefin:CON-1:T1": ("Other", "default"), "simplefin:CON-1:T2": ("Other", "default")}
    assert conn.execute("SELECT COUNT(*) FROM classifications WHERE source = 'plaid'").fetchone()[0] == 0
    classify.add_rule(conn, "merchant_name", "Synthetic Grocer", "Groceries", created_from="user")
    rows = {r["transaction_id"]: (r["category"], r["category_source"]) for r in conn.execute(
        "SELECT transaction_id, category, category_source FROM v_transactions WHERE transaction_id LIKE 'simplefin:%' "
        "ORDER BY transaction_id")}
    assert rows["simplefin:CON-1:T1"] == ("Groceries", "rule")
    assert rows["simplefin:CON-1:T2"] == ("Other", "default")


# --- windows, backfill and coverage -------------------------------------------------------------------

def test_the_first_sync_backfills_a_year_in_at_most_five_requests_with_exclusive_ends(conn):
    bridge = Bridge(account_set([card(txs=[tx("T1", TODAY - timedelta(days=10), "1.00", "Recent")])]))
    confirmed(conn, bridge)
    windows = bridge.windows()
    assert len(windows) == 5                         # the claim + four older chunks, never more
    starts = [day(w[0]) for w in windows]
    ends = [day(w[1]) for w in windows]
    assert ends[0] == TODAY + timedelta(days=1)      # exclusive: today's transactions are inside the window
    assert starts[0] == ends[0] - timedelta(days=simplefin_sync.CHUNK_DAYS)
    for i in range(len(windows) - 1):
        assert ends[i + 1] == starts[i]              # the older chunk ends where the newer one starts
        assert ends[i] - starts[i] == timedelta(days=simplefin_sync.CHUNK_DAYS)
    assert starts[-1] == TODAY - timedelta(days=simplefin_sync.BACKFILL_DAYS)
    # the Bridge only had ten days of data: coverage is what it returned, not the year we asked for
    assert simplefin_sync.status(conn)["coverage_start"] == (TODAY - timedelta(days=10)).isoformat()


def test_the_daily_sync_asks_from_last_success_minus_five_days(conn):
    bridge = Bridge(account_set([card(txs=[tx("T1", date(2026, 11, 10), "1.00", "Old")])]))
    confirmed(conn, bridge)
    simplefin_sync._set(conn, simplefin_sync.LAST_SUCCESS_KEY, f"{TODAY - timedelta(days=2)}T12:00:00Z")
    simplefin_sync._set(conn, simplefin_sync.COVERAGE_KEY, (TODAY - timedelta(days=30)).isoformat())
    before = len(bridge.windows())
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    (start, end), = bridge.windows()[before:]
    assert day(start) == TODAY - timedelta(days=7)     # last success - 2 days - 5 overlap
    assert day(end) == TODAY + timedelta(days=1)       # exclusive end: today is inside the window


def test_the_claim_and_first_sync_windows_end_today_plus_one(conn):
    bridge = Bridge(account_set([card()]))
    claimed(conn, bridge)
    start, end = bridge.windows()[0]                     # the claim's fetch is the first window
    assert int(end) == epoch(TODAY + timedelta(days=1))
    assert int(start) == epoch(TODAY + timedelta(days=1)) - simplefin_sync.CHUNK_DAYS * 86400
    simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                           today=TODAY, sleep=lambda s: None, jitter=0)
    simplefin_sync._del(conn, simplefin_sync.LAST_SUCCESS_KEY)   # the next run is a first sync again
    before = len(bridge.windows())
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    window = bridge.windows()[before:]
    assert window and int(window[0][1]) == epoch(TODAY + timedelta(days=1))
    assert day(window[0][0]) == TODAY + timedelta(days=1) - timedelta(days=simplefin_sync.CHUNK_DAYS)


def test_a_manual_sync_asks_for_today(conn):
    bridge = Bridge(account_set([card()]))
    confirmed(conn, bridge)
    simplefin_sync._set(conn, simplefin_sync.LAST_SUCCESS_KEY, f"{TODAY - timedelta(days=2)}T12:00:00Z")
    simplefin_sync._set(conn, simplefin_sync.COVERAGE_KEY, (TODAY - timedelta(days=30)).isoformat())
    stamp = epoch(TODAY) + 15 * 3600                     # today at 15:00 UTC
    bridge.payload = account_set([card(txs=[{"id": "T15", "posted": stamp, "amount": "-5.00",
                                             "description": "Coffee shop", "transacted_at": stamp}])])
    before = len(bridge.windows())
    simplefin_sync.sync(conn, "manual", transport=bridge, resolve=PUBLIC, today=TODAY)
    (start, end), = bridge.windows()[before:]
    assert int(end) == epoch(TODAY + timedelta(days=1))   # exclusive end, so today is inside the window
    assert day(start) == TODAY - timedelta(days=7)       # at most five days before the last success
    assert int(start) <= stamp < int(end)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE transaction_id = 'simplefin:CON-1:T15'"
                        ).fetchone()[0] == 1


def test_a_manual_sync_is_one_request_and_counts_against_six(conn, data_dir):
    bridge = Bridge(account_set([card(txs=[])]) )
    confirmed(conn, bridge)
    for n in range(6):
        out = simplefin_sync.sync(conn, "manual", transport=bridge, resolve=PUBLIC, today=TODAY)
        assert out["ok"] and out["requests"] == 1
    rows = conn.execute("SELECT COUNT(*) FROM simplefin_requests WHERE trigger = 'manual'").fetchone()[0]
    assert rows == 6
    calls_before = len(bridge.calls)
    out = simplefin_sync.sync(conn, "manual", transport=bridge, resolve=PUBLIC, today=TODAY)
    assert not out["ok"] and "6 manual syncs" in out["limited"]
    assert len(bridge.calls) == calls_before                 # no request was made
    assert conn.execute("SELECT COUNT(*) FROM simplefin_requests WHERE trigger = 'manual'").fetchone()[0] == 6


def test_the_six_manual_syncs_are_shared_across_connections_and_processes(conn, data_dir):
    bridge = Bridge(account_set([card(txs=[])]) )
    confirmed(conn, bridge)
    other = db.connect(config.db_path())                    # the CLI / the plugin: another process's connection
    try:
        for _ in range(6):
            assert simplefin_sync.sync(other, "manual", transport=bridge, resolve=PUBLIC, today=TODAY)["ok"]
        refused = simplefin_sync.sync(conn, "manual", transport=bridge, resolve=PUBLIC, today=TODAY)
        assert not refused["ok"] and "6 manual syncs" in refused["limited"]
    finally:
        other.close()


def test_the_database_refuses_the_twenty_fifth_request_and_the_seventh_manual_one(conn):
    day = TODAY.isoformat()
    with conn:
        for n in range(24):
            conn.execute("INSERT INTO simplefin_requests (at, day, trigger, outcome) VALUES ('x', ?, 'daily', 'ok')",
                         (day,))
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO simplefin_requests (at, day, trigger) VALUES ('x', ?, 'daily')", (day,))
    with conn:
        conn.execute("DELETE FROM simplefin_requests")
        for n in range(6):
            conn.execute("INSERT INTO simplefin_requests (at, day, trigger, outcome) VALUES ('x', ?, 'manual', 'ok')", (day,))
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO simplefin_requests (at, day, trigger) VALUES ('x', ?, 'manual')", (day,))


def test_a_full_budget_stops_a_daily_sync_without_a_request(conn):
    bridge = Bridge(account_set([card(txs=[])]) )
    confirmed(conn, bridge)
    day = TODAY.isoformat()
    with conn:
        conn.execute("DELETE FROM simplefin_requests")
        for n in range(24):
            conn.execute("INSERT INTO simplefin_requests (at, day, trigger, outcome) VALUES ('x', ?, 'backfill', 'ok')",
                         (day,))
    calls = len(bridge.calls)
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert not out["ok"] and "24 requests" in out["limited"]
    assert len(bridge.calls) == calls


def _real_clock_far_away(monkeypatch):
    """The real clock reads 2027-06-01 everywhere this module could read it."""
    class FarDate(date):
        @classmethod
        def today(cls):
            return cls(2027, 6, 1)
    monkeypatch.setattr(simplefin_sync, "date", FarDate)
    monkeypatch.setattr(simplefin_sync, "now", lambda: "2027-06-01T09:30:00Z")
    monkeypatch.setattr(db, "now", lambda: "2027-06-01T09:30:00Z")


def test_a_given_today_is_the_only_clock_a_sync_reads(conn, monkeypatch):
    _real_clock_far_away(monkeypatch)
    bridge = Bridge(account_set([card(txs=[tx("T1", TODAY - timedelta(days=1), "-5.00", "Grocery store"),
                                          tx("P1", TODAY, "-9.00", "Coffee shop", pending=True)])]))
    claimed(conn, bridge)
    state = simplefin_sync.pending(conn)
    assert simplefin_sync._get(conn, simplefin_sync.PENDING_KEY)["fetched_at"].startswith("2026-11-15")
    got = simplefin_sync.confirm(conn, {a["key"]: {"type": "checking"} for a in state["accounts"]}, transport=bridge,
                                 resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None,
                              jitter=0)
    assert out["ok"], out
    assert conn.execute("SELECT COUNT(*) FROM simplefin_requests").fetchone()[0] >= 2
    for r in conn.execute("SELECT day, at FROM simplefin_requests"):      # the budget day and its stamp
        assert r["day"] == "2026-11-15" and r["at"].startswith("2026-11-15")
    # the daily window ends the day after TODAY and starts within days of it, not around 2027-06-01
    start, end = bridge.windows()[-1]
    assert day(end) == TODAY + timedelta(days=1)
    assert TODAY - timedelta(days=10) <= day(start) <= TODAY
    assert simplefin_sync._get(conn, simplefin_sync.LAST_SUCCESS_KEY).startswith("2026-11-15")
    assert simplefin_sync._get(conn, simplefin_sync.LAST_RESULT_KEY)["at"].startswith("2026-11-15")
    assert simplefin_sync.status(conn, today=TODAY)["requests_today"] == conn.execute(
        "SELECT COUNT(*) FROM simplefin_requests").fetchone()[0]
    for sql in ("SELECT last_sync_at FROM items WHERE provider = 'simplefin'",
                "SELECT updated_at FROM settings WHERE key LIKE 'simplefin%'",
                "SELECT created_at FROM items WHERE provider = 'simplefin'"):
        stamps = [r[0] for r in conn.execute(sql) if r[0]]
        assert stamps and all(t.startswith("2026-11-15") for t in stamps), sql
    # a second daily run the same (given) day follows the same budget day, not the real one
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert {r[0] for r in conn.execute("SELECT day FROM simplefin_requests")} == {"2026-11-15"}


def test_without_today_the_seam_is_the_real_clock(conn, monkeypatch):
    _real_clock_far_away(monkeypatch)
    assert simplefin_sync._today() == date(2027, 6, 1)
    assert simplefin_sync._now() == "2027-06-01T09:30:00Z"
    assert simplefin_sync.budget(conn)["requests_today"] == 0
    assert simplefin_sync.status(conn)["today"] == "2027-06-01"


# --- pending matching ---------------------------------------------------------------------------------

def test_a_40_pending_then_48_posted_is_one_charge(conn):
    pending = tx("P1", TODAY - timedelta(days=2), "-40.00", "Grocery store", pending=True)
    posted = tx("B1", TODAY - timedelta(days=1), "-48.00", "Grocery store")
    bridge = Bridge(account_set([card(txs=[pending, posted])]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 1
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 1
    assert conn.execute("SELECT pending_transaction_id FROM transactions WHERE transaction_id = 'simplefin:CON-1:B1'"
                        ).fetchone()[0] == "simplefin:CON-1:P1"
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 48.0


def test_ambiguous_pending_is_never_merged_and_both_rows_stay_for_review(conn):
    # not a complete 2x2 block: P1 can only pair with B1, while P2 fits both B1 and B2 (P1 is too old for B2)
    txs = [tx("P1", TODAY - timedelta(days=7), "-40.00", "Grocery store", pending=True),
           tx("P2", TODAY - timedelta(days=3), "-40.00", "Grocery store", pending=True),
           tx("B1", TODAY - timedelta(days=2), "-40.00", "Grocery store"),
           tx("B2", TODAY - timedelta(days=1), "-40.00", "Grocery store")]
    bridge = Bridge(account_set([card(txs=txs)]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE pending = 1 AND removed = 0").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE pending_transaction_id IS NOT NULL").fetchone()[0] == 0
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert out["ambiguous"] >= 2


def test_a_pending_row_older_than_ten_days_with_no_twin_is_soft_removed(conn):
    old = tx("P1", TODAY - timedelta(days=11), "-40.00", "Grocery store", pending=True)
    bridge = Bridge(account_set([card(txs=[old])]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 0


def test_a_pending_without_a_twin_never_matches_an_unrelated_charge(conn):
    pending = tx("P1", TODAY - timedelta(days=2), "-40.00", "Coffee shop", pending=True)
    posted = tx("B1", TODAY - timedelta(days=1), "-40.00", "Hardware store")
    bridge = Bridge(account_set([card(txs=[pending, posted])]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 0
    assert conn.execute("SELECT pending_transaction_id FROM transactions WHERE transaction_id = 'simplefin:CON-1:B1'"
                        ).fetchone()[0] is None


def test_two_independent_identical_charges_both_count(conn):
    bridge = Bridge(account_set([card(txs=[tx("B1", TODAY - timedelta(days=1), "-40.00", "Grocery store"),
                                          tx("B2", TODAY - timedelta(days=1), "-40.00", "Grocery store")])]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 2
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 80.0


def test_two_identical_pendings_and_two_identical_posted_pair_in_id_order(conn):
    txs = [tx("P1", TODAY - timedelta(days=2), "-4.50", "Coffee shop", pending=True),
           tx("P2", TODAY - timedelta(days=2), "-4.50", "Coffee shop", pending=True),
           tx("B1", TODAY - timedelta(days=1), "-4.50", "Coffee shop"),
           tx("B2", TODAY - timedelta(days=1), "-4.50", "Coffee shop")]
    bridge = Bridge(account_set([card(txs=txs)]))
    out = confirmed(conn, bridge)
    assert out["last_result"]["matched"] == 2 and out["last_result"]["ambiguous"] == 0
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 2
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 9.0
    links = dict(conn.execute("SELECT transaction_id, pending_transaction_id FROM transactions "
                              "WHERE pending_transaction_id IS NOT NULL ORDER BY transaction_id"))
    assert links == {"simplefin:CON-1:B1": "simplefin:CON-1:P1",
                     "simplefin:CON-1:B2": "simplefin:CON-1:P2"}


def test_unequal_identical_counts_stay_ambiguous(conn):
    txs = [tx("P1", TODAY - timedelta(days=2), "-4.50", "Coffee shop", pending=True),
           tx("P2", TODAY - timedelta(days=2), "-4.50", "Coffee shop", pending=True),
           tx("B1", TODAY - timedelta(days=1), "-4.50", "Coffee shop")]
    bridge = Bridge(account_set([card(txs=txs)]))
    out = confirmed(conn, bridge)
    assert out["last_result"]["matched"] == 1 and out["last_result"]["ambiguous"] == 1
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 2
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 9.0
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 1
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P2'").fetchone()[0] == 0
    assert conn.execute("SELECT pending_transaction_id FROM transactions WHERE transaction_id = 'simplefin:CON-1:B1'"
                        ).fetchone()[0] == "simplefin:CON-1:P1"


def test_a_pending_the_bridge_drops_is_soft_removed(conn):
    inside = tx("P75", TODAY - timedelta(days=3), "-75.00", "Released hold", pending=True)
    outside = tx("POUT", TODAY - timedelta(days=8), "-10.00", "Older hold", pending=True)
    bridge = Bridge(account_set([card(txs=[inside, outside])]))
    confirmed(conn, bridge)
    before = conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0]
    assert before == 85.0
    simplefin_sync._set(conn, simplefin_sync.LAST_SUCCESS_KEY, f"{TODAY - timedelta(days=1)}T12:00:00Z")
    simplefin_sync._set(conn, simplefin_sync.COVERAGE_KEY, (TODAY - timedelta(days=30)).isoformat())
    bridge.payload = account_set([card(txs=[outside])])  # the Bridge released the 75.00 hold
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert out["stale_removed"] == 1
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P75'").fetchone()[0] == 1
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:POUT'").fetchone()[0] == 0
    after = conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0]
    assert after == 10.0                                  # 75.00 of spending no longer counts


def test_a_pending_the_bridge_drops_and_returns_as_pending_is_counted_again(conn):
    hold = tx("P75", TODAY - timedelta(days=3), "-75.00", "Released hold", pending=True)
    bridge = Bridge(account_set([card(txs=[hold])]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 75.0
    bridge.payload = account_set([card(txs=[])])          # one answer without it: soft-removed
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert out["stale_removed"] == 1
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P75'").fetchone()[0] == 1
    bridge.payload = account_set([card(txs=[hold])])      # the Bridge returns it again as pending
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P75'").fetchone()[0] == 0
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 75.0
    assert out["modified"] == 1 and out["added"] == 0     # restored rows are updates, never new rows


def test_a_pending_the_bridge_drops_and_returns_posted_under_the_same_id_is_counted_once(conn):
    hold = tx("X1", TODAY - timedelta(days=3), "-75.00", "Grocery store", pending=True)
    bridge = Bridge(account_set([card(txs=[hold])]))
    confirmed(conn, bridge)
    bridge.payload = account_set([card(txs=[])])          # the hold disappears for one answer
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:X1'").fetchone()[0] == 1
    posted = tx("X1", TODAY - timedelta(days=2), "-75.00", "Grocery store")   # same id, now posted
    bridge.payload = account_set([card(txs=[posted])])
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    row = conn.execute("SELECT pending, removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:X1'").fetchone()
    assert tuple(row) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 1
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 75.0
    assert out["modified"] == 1 and out["added"] == 0


def test_a_stale_pending_returned_posted_under_the_same_id_is_not_lost(conn):
    old = tx("P1", TODAY - timedelta(days=11), "-40.00", "Grocery store", pending=True)
    bridge = Bridge(account_set([card(txs=[old])]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 0
    bridge.payload = account_set([card(txs=[tx("P1", TODAY - timedelta(days=10), "-40.00", "Grocery store")])])
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    row = conn.execute("SELECT pending, removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()
    assert tuple(row) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 1
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 40.0
    assert out["modified"] == 1 and out["added"] == 0


def test_an_account_named_in_the_errlist_keeps_its_pending_transactions(conn):
    first = tx("P1", TODAY - timedelta(days=3), "-25.00", "Grocery store", pending=True)
    second = tx("P2", TODAY - timedelta(days=3), "-10.00", "Coffee shop", pending=True)
    connections = [{"conn_id": "CON-1", "name": "Synthetic Bank"},
                   {"conn_id": "CON-2", "name": "Synthetic Credit Union"}]
    bridge = Bridge(account_set([card(txs=[first], conn="CON-1"),
                                 card(name="Synthetic Savings", balance="500.00", available="500.00",
                                      txs=[second], conn="CON-2")], connections=connections))
    confirmed(conn, bridge)
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 2
    bridge.payload = account_set([card(txs=[], conn="CON-1"),
                                  card(name="Synthetic Savings", balance="500.00", available="500.00",
                                       txs=[], conn="CON-2")], connections=connections,
                                 errlist=[{"code": "con.auth", "msg": "Synthetic Bank needs attention",
                                           "conn_id": "CON-1"}])
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert out["stale_removed"] == 1                      # only the healthy connection's pending is gone
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 0
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-2:P2'").fetchone()[0] == 1
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 25.0
    assert [e["code"] for e in simplefin_sync.status(conn)["errors"]] == ["con.auth"]


@pytest.mark.parametrize("error", [
    {"errlist": [{"code": "con.auth", "msg": "Synthetic Bank needs attention"}]},
    {"errors": ["Synthetic Bank needs attention"]},
])
def test_an_error_that_names_no_connection_keeps_every_pending(conn, error):
    first = tx("P1", TODAY - timedelta(days=3), "-25.00", "Grocery store", pending=True)
    second = tx("P2", TODAY - timedelta(days=3), "-10.00", "Coffee shop", pending=True)
    connections = [{"conn_id": "CON-1", "name": "Synthetic Bank"},
                   {"conn_id": "CON-2", "name": "Synthetic Credit Union"}]
    bridge = Bridge(account_set([card(txs=[first], conn="CON-1"),
                                 card(name="Synthetic Savings", balance="500.00", available="500.00",
                                      txs=[second], conn="CON-2")], connections=connections))
    confirmed(conn, bridge)
    bridge.payload = account_set([card(txs=[], conn="CON-1"),
                                  card(name="Synthetic Savings", balance="500.00", available="500.00",
                                       txs=[], conn="CON-2")], connections=connections) | error
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert out["stale_removed"] == 0                      # the whole answer is partial, not one account gone
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 0
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-2:P2'").fetchone()[0] == 0


def test_a_legacy_error_string_mirroring_a_named_connection_does_not_suspend_the_rule_for_the_others(conn):
    first = tx("P1", TODAY - timedelta(days=3), "-25.00", "Grocery store", pending=True)
    second = tx("P2", TODAY - timedelta(days=3), "-10.00", "Coffee shop", pending=True)
    connections = [{"conn_id": "CON-1", "name": "Synthetic Bank"},
                   {"conn_id": "CON-2", "name": "Synthetic Credit Union"}]
    bridge = Bridge(account_set([card(txs=[first], conn="CON-1"),
                                 card(name="Synthetic Savings", balance="500.00", available="500.00",
                                      txs=[second], conn="CON-2")], connections=connections))
    confirmed(conn, bridge)
    bridge.payload = account_set([card(txs=[], conn="CON-1"),
                                  card(name="Synthetic Savings", balance="500.00", available="500.00",
                                       txs=[], conn="CON-2")], connections=connections,
                                 errlist=[{"code": "con.auth", "msg": "Synthetic Bank needs attention",
                                           "conn_id": "CON-1"}]) | {"errors": ["Synthetic Bank needs attention"]}
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert out["stale_removed"] == 1                      # the mirrored string names no one, CON-2 is still complete
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 0
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-2:P2'").fetchone()[0] == 1
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 25.0


def test_a_merged_pending_still_returned_as_pending_is_not_double_counted(conn):
    pending = tx("P1", TODAY - timedelta(days=2), "-40.00", "Grocery store", pending=True)
    posted = tx("B1", TODAY - timedelta(days=1), "-48.00", "Grocery store")
    bridge = Bridge(account_set([card(txs=[pending, posted])]))
    confirmed(conn, bridge)
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 1
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:P1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 1
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 48.0


def test_a_merged_pending_the_bridge_returns_posted_under_its_own_id_counts_again(conn):
    pending = tx("X1", TODAY - timedelta(days=2), "-40.00", "Grocery store", pending=True)
    bridge = Bridge(account_set([card(txs=[pending])]))
    confirmed(conn, bridge)
    posted = tx("B1", TODAY - timedelta(days=1), "-48.00", "Grocery store")
    bridge.payload = account_set([card(txs=[pending, posted])])       # the fuzzy match merges X1 into B1
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert out["matched"] == 1
    assert conn.execute("SELECT removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:X1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 1
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 48.0
    bridge.payload = account_set([card(txs=[tx("X1", TODAY - timedelta(days=2), "-40.00", "Grocery store"), posted])])
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    row = conn.execute("SELECT pending, removed FROM transactions WHERE transaction_id = 'simplefin:CON-1:X1'").fetchone()
    assert tuple(row) == (0, 0)                           # the bank posted it under its own id: a second real charge
    assert conn.execute("SELECT pending_transaction_id FROM transactions WHERE transaction_id = 'simplefin:CON-1:B1'"
                        ).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 2
    assert conn.execute("SELECT SUM(amount) FROM v_transactions WHERE source = 'simplefin'").fetchone()[0] == 88.0


def test_a_pending_users_category_follows_the_posted_charge(conn):
    from hermes_finance import classify
    pending = tx("P1", TODAY - timedelta(days=2), "-40.00", "Grocery store", pending=True)
    bridge = Bridge(account_set([card(txs=[pending])]))
    confirmed(conn, bridge)
    classify.set_category(conn, "simplefin:CON-1:P1", "Groceries", "user")
    bridge.payload = account_set([card(txs=[pending, tx("B1", TODAY - timedelta(days=1), "-48.00", "Grocery store")])])
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    row = conn.execute("SELECT c.source, cat.name FROM classifications c JOIN categories cat ON cat.id = c.category_id "
                       "WHERE c.transaction_id = 'simplefin:CON-1:B1'").fetchone()
    assert tuple(row) == ("user", "Groceries")


# --- mixed Plaid / SimpleFIN duplicates and reconnect --------------------------------------------------

def plaid_account(conn, balance=100.0):
    with conn:
        conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                     "VALUES ('item_plaid', 'ins_usb', 'U.S. Bank', 'access-production-x', 'active', '2026-01-01')")
        conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                     "iso_currency) VALUES ('acc_plaid', 'item_plaid', 'Smartly Checking', '5108', 'depository', "
                     "'checking', ?, 'USD')", (balance,))


def duplicate_bridge(balance="200.00"):
    return Bridge(account_set([card(name="Smartly Checking 5108", balance=balance, available=balance)]))


def test_a_plaid_duplicate_does_not_count_until_the_owner_picks_a_source(conn):
    plaid_account(conn, 100.0)
    bridge = duplicate_bridge("200.00")
    got = claimed(conn, bridge)
    assert got["accounts"][0]["plaid"]["account_id"] == "acc_plaid"
    out = simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                                 today=TODAY, sleep=lambda s: None, jitter=0)
    policy = json.loads(conn.execute("SELECT value FROM settings WHERE key = 'excluded_accounts'").fetchone()[0])
    assert "simplefin:CON-1:ACT-1" in policy
    assert out["duplicate_reviews"] == 1
    assert net(conn) == 100.0                                   # Plaid keeps counting
    simplefin_sync.pick_duplicate_source(conn, "simplefin:CON-1:ACT-1", "acc_plaid", "simplefin")
    assert net(conn) == 200.0                                   # the explicit pick moves the count
    assert "acc_plaid" in json.loads(conn.execute(
        "SELECT value FROM settings WHERE key = 'excluded_accounts'").fetchone()[0])


def test_use_plaid_and_not_a_duplicate_are_explicit_too(conn):
    plaid_account(conn, 100.0)
    bridge = duplicate_bridge("200.00")
    claimed(conn, bridge)
    simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                           today=TODAY, sleep=lambda s: None, jitter=0)
    simplefin_sync.pick_duplicate_source(conn, "simplefin:CON-1:ACT-1", "acc_plaid", "not_duplicate")
    assert net(conn) == 300.0
    pairs = json.loads(conn.execute("SELECT value FROM settings WHERE key = 'simplefin.duplicate_pairs'").fetchone()[0])
    assert [(p["simplefin"], p["picked"]) for p in pairs] == [("simplefin:CON-1:ACT-1", "not_duplicate")]
    assert simplefin_sync.status(conn)["duplicates"] == []


def policy(conn) -> set[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = 'excluded_accounts'").fetchone()
    return set(json.loads(row[0])) if row else set()


def reconnect(conn, bridge):
    token = base64.urlsafe_b64encode(b"https://bridge.example/simplefin/claim/again").decode().rstrip("=")
    simplefin_sync.claim(conn, token, transport=bridge, resolve=PUBLIC, minute=7, today=TODAY)
    return simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                                  today=TODAY, sleep=lambda s: None, jitter=0)


def test_picking_simplefin_then_disconnecting_keeps_the_account_in_net_worth(conn, data_dir):
    plaid_account(conn, 100.0)
    bridge = duplicate_bridge("200.00")
    claimed(conn, bridge)
    simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                           today=TODAY, sleep=lambda s: None, jitter=0)
    assert net(conn) == 100.0
    simplefin_sync.pick_duplicate_source(conn, "simplefin:CON-1:ACT-1", "acc_plaid", "simplefin")
    assert net(conn) == 200.0
    simplefin_sync.disconnect(conn)
    assert net(conn) == 100.0                            # counting is handed back to the Plaid copy
    assert "acc_plaid" not in policy(conn) and "simplefin:CON-1:ACT-1" in policy(conn)
    pairs = json.loads(conn.execute(
        "SELECT value FROM settings WHERE key = 'simplefin.duplicate_pairs'").fetchone()[0])
    assert pairs[0].get("picked") is None                # the pair is a review again
    assert simplefin_sync.status(conn)["duplicates"] == []   # a disconnected connection shows no review row
    reconnect(conn, bridge)
    assert net(conn) == 100.0                            # unpicked after the disconnect: Plaid-only
    assert simplefin_sync.status(conn)["duplicates"] and \
        simplefin_sync.status(conn)["duplicates"][0]["simplefin"] == "simplefin:CON-1:ACT-1"
    simplefin_sync.pick_duplicate_source(conn, "simplefin:CON-1:ACT-1", "acc_plaid", "simplefin")
    assert net(conn) == 200.0                            # the second explicit pick still works


def test_a_not_duplicate_pick_survives_disconnect_and_reconnect(conn, data_dir):
    plaid_account(conn, 100.0)
    bridge = duplicate_bridge("200.00")
    claimed(conn, bridge)
    simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                           today=TODAY, sleep=lambda s: None, jitter=0)
    simplefin_sync.pick_duplicate_source(conn, "simplefin:CON-1:ACT-1", "acc_plaid", "not_duplicate")
    assert net(conn) == 300.0
    simplefin_sync.disconnect(conn)
    pairs = json.loads(conn.execute(
        "SELECT value FROM settings WHERE key = 'simplefin.duplicate_pairs'").fetchone()[0])
    assert pairs[0]["picked"] == "not_duplicate"
    assert simplefin_sync.status(conn)["duplicates"] == []
    reconnect(conn, bridge)
    assert net(conn) == 300.0                            # both copies count again, and the pick is not reopened
    assert simplefin_sync.status(conn)["duplicates"] == []


def test_a_not_duplicate_account_stops_counting_spending_after_disconnect(conn, data_dir):
    from hermes_finance import insights
    plaid_account(conn, 100.0)
    with conn:
        conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, source, "
                     "first_seen_at, updated_at) VALUES ('plaid-coffee', 'acc_plaid', 'item_plaid', '2026-11-02', "
                     "'Synthetic Coffee', 10.0, 'plaid', 'now', 'now')")
    shopping = tx("T15", date(2026, 11, 3), "-15.00", "Grocery store")
    bridge = Bridge(account_set([card(name="Smartly Checking 5108", balance="200.00", available="200.00",
                                      txs=[shopping])]))
    claimed(conn, bridge)
    simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                           today=TODAY, sleep=lambda s: None, jitter=0)
    assert insights.month_summary(conn, "2026-11", today=TODAY)["mtd"] == 10.0   # the duplicate is Plaid-only
    simplefin_sync.pick_duplicate_source(conn, "simplefin:CON-1:ACT-1", "acc_plaid", "not_duplicate")
    assert insights.month_summary(conn, "2026-11", today=TODAY)["mtd"] == 25.0
    simplefin_sync.disconnect(conn)
    assert insights.month_summary(conn, "2026-11", today=TODAY)["mtd"] == 10.0   # the SimpleFIN copy stops counting
    assert insights.spend_by(conn, "v.account_id = ?", ("acc_plaid",))[None][0] == 10.0   # the Plaid copy still does
    assert "simplefin:CON-1:ACT-1" in policy(conn)


def test_a_plaid_pick_survives_disconnect_and_reconnect_without_a_new_review(conn, data_dir):
    plaid_account(conn, 100.0)
    bridge = duplicate_bridge("200.00")
    claimed(conn, bridge)
    simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                           today=TODAY, sleep=lambda s: None, jitter=0)
    simplefin_sync.pick_duplicate_source(conn, "simplefin:CON-1:ACT-1", "acc_plaid", "plaid")
    assert net(conn) == 100.0
    simplefin_sync.disconnect(conn)
    assert net(conn) == 100.0
    reconnect(conn, bridge)
    assert net(conn) == 100.0
    assert simplefin_sync.status(conn)["duplicates"] == []   # the settled pick is not reopened


def net(conn):
    from hermes_finance import insights
    return insights.net_worth(conn, today=TODAY)["net"]


def test_disconnect_keeps_every_row_and_reconnect_reuses_the_ids(conn, data_dir):
    bridge = Bridge(account_set([card(txs=[tx("T1", date(2026, 11, 2), "-25.00", "Grocery store")])]))
    confirmed(conn, bridge)
    transaction_ids = [r[0] for r in conn.execute("SELECT transaction_id FROM transactions")]
    assert simplefin_sync.disconnect(conn)["connected"] is False
    assert secretstore.simplefin_url() is None
    item = conn.execute("SELECT removed_at, error_code FROM items WHERE item_id = 'simplefin:CON-1'").fetchone()
    assert item["removed_at"] and item["error_code"] == "SIMPLEFIN_DISCONNECTED"
    assert [r[0] for r in conn.execute("SELECT transaction_id FROM transactions")] == transaction_ids
    assert "simplefin:CON-1:ACT-1" in json.loads(
        conn.execute("SELECT value FROM settings WHERE key = 'excluded_accounts'").fetchone()[0])
    assert net(conn) == 0.0
    # a new one-time token for the same connection: deterministic ids, counting back on, no double count
    new_token = base64.urlsafe_b64encode(b"https://bridge.example/simplefin/claim/again").decode().rstrip("=")
    simplefin_sync.claim(conn, new_token, transport=bridge, resolve=PUBLIC, minute=7, today=TODAY)
    simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                           today=TODAY, sleep=lambda s: None, jitter=0)
    item = conn.execute("SELECT removed_at, status FROM items WHERE item_id = 'simplefin:CON-1'").fetchone()
    assert item["removed_at"] is None and item["status"] == "active"
    assert [r[0] for r in conn.execute("SELECT transaction_id FROM transactions")] == transaction_ids
    assert "simplefin:CON-1:ACT-1" not in json.loads(
        conn.execute("SELECT value FROM settings WHERE key = 'excluded_accounts'").fetchone()[0])
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_a_balance_only_investment_imports_no_holdings(conn):
    bridge = Bridge(account_set([card(name="Synthetic Brokerage", balance="5000.00", holdings=True)]))
    claimed(conn, bridge)
    simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "investment"}}, transport=bridge, resolve=PUBLIC,
                           today=TODAY, sleep=lambda s: None, jitter=0)
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 0
    from hermes_finance import portfolio
    inv = portfolio.investments(conn, TODAY)
    assert len(inv["accounts"]) == 1
    assert inv["accounts"][0]["holdings_imported"] is False
    assert inv["accounts"][0]["holdings"] == 0
    assert inv["total"] == 5000.0


# --- disconnected never zeroes a balance --------------------------------------------------------------

@pytest.mark.parametrize("status,state", [(402, "subscription"), (403, "needs_login")])
def test_402_and_403_keep_the_last_balances_and_never_write_zeros(conn, status, state):
    bridge = Bridge(account_set([card(balance="1234.56", txs=[tx("T1", date(2026, 11, 2), "-25.00", "Store")])]))
    confirmed(conn, bridge)
    before = conn.execute("SELECT current_balance, updated_at FROM accounts WHERE account_id = 'simplefin:CON-1:ACT-1'"
                          ).fetchone()
    sync_before = conn.execute("SELECT last_sync_at FROM items WHERE item_id = 'simplefin:CON-1'").fetchone()[0]
    bridge.accounts_status = status
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert not out["ok"] and out["state"] == state
    after = conn.execute("SELECT current_balance, updated_at FROM accounts WHERE account_id = 'simplefin:CON-1:ACT-1'"
                         ).fetchone()
    assert tuple(after) == tuple(before)
    assert conn.execute("SELECT last_sync_at FROM items WHERE item_id = 'simplefin:CON-1'").fetchone()[0] == sync_before
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE source = 'simplefin' AND amount = 0").fetchone()[0] == 0
    assert simplefin_sync.status(conn)["as_of"] == before["updated_at"][:10]


def test_bridge_errors_reach_status_and_clear_on_a_clean_sync(conn):
    bridge = Bridge(account_set([card()]))
    confirmed(conn, bridge)
    bridge.payload = account_set([card()], errlist=[{"code": "con.auth",
                                                     "msg": "Account needs a look at https://bridge.example/x\nagain"}])
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    errors = simplefin_sync.status(conn)["errors"]
    assert [e["code"] for e in errors] == ["con.auth"]
    assert "https://" not in errors[0]["message"]
    assert "\n" not in errors[0]["message"] and "\x07" not in errors[0]["message"]
    bridge.payload = account_set([card()])                # a clean answer clears the last errors
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert simplefin_sync.status(conn)["errors"] == []
    bridge.accounts_status = 403
    bridge.payload = account_set([], errlist=[{"code": "gen.auth", "msg": "revoked"}])
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert [e["code"] for e in simplefin_sync.status(conn)["errors"]] == ["gen.auth"]
    bridge.accounts_status = 200
    bridge.payload = account_set([card()])
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert simplefin_sync.status(conn)["errors"] == []


def test_an_overpaid_card_is_an_asset_and_a_balance_owed_is_a_debt(conn):
    owed = card(name="Synthetic Visa", balance="-450.75", available="4549.25", conn="CON-1")
    overpaid = card(name="Synthetic Card", balance="25.00", available="0.00", conn="CON-2")
    payload = account_set([owed, overpaid], connections=[{"conn_id": "CON-1", "name": "Synthetic Bank"},
                                                          {"conn_id": "CON-2", "name": "Synthetic Credit Union"}])
    bridge = Bridge(payload)
    claimed(conn, bridge)
    simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "credit"}, "CON-2:ACT-1": {"type": "credit"}},
                           transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    balances = dict(conn.execute("SELECT account_id, current_balance FROM accounts ORDER BY account_id"))
    assert balances == {"simplefin:CON-1:ACT-1": 450.75, "simplefin:CON-2:ACT-1": -25.0}
    from hermes_finance import insights
    nw = insights.net_worth(conn, today=TODAY)
    assert nw["debts"] == 425.75                          # the overpayment reduces the debt by 25.00
    assert nw["net"] == -425.75                           # -450.75 owed + 25.00 overpaid


def test_a_bad_claim_never_stores_a_url(conn, data_dir):
    bridge = Bridge(claim_status=403)
    with pytest.raises(simplefin.InvalidToken):
        claimed(conn, bridge)
    assert secretstore.simplefin_url() is None
    assert conn.execute("SELECT COUNT(*) FROM simplefin_requests").fetchone()[0] == 0


def test_a_claim_whose_first_fetch_fails_can_be_retried_without_a_new_token(conn, data_dir):
    bridge = Bridge(account_set([card()]), accounts_status=500)
    with pytest.raises(simplefin.SimpleFinError):
        claimed(conn, bridge)
    assert secretstore.simplefin_url() == ACCESS          # the spent token's link is kept, ready to retry
    st = simplefin_sync.status(conn)
    assert st["state"] == "needs_retry" and st["needs_retry"] is True
    assert st["needs_confirm"] is False
    assert simplefin_sync.pending(conn) is None
    with pytest.raises(simplefin.SimpleFinError):         # a retry that fails again stays retryable
        simplefin_sync.retry(conn, transport=bridge, resolve=PUBLIC, today=TODAY)
    assert secretstore.simplefin_url() == ACCESS
    assert simplefin_sync.status(conn)["state"] == "needs_retry"
    bridge.accounts_status = 200
    got = simplefin_sync.retry(conn, transport=bridge, resolve=PUBLIC, today=TODAY)
    assert got["accounts"][0]["id"] == "ACT-1"            # no token was passed: the saved link was reused
    assert conn.execute("SELECT COUNT(*) FROM simplefin_requests WHERE trigger = 'confirm'").fetchone()[0] == 3
    out = simplefin_sync.confirm(conn, {"CON-1:ACT-1": {"type": "checking"}}, transport=bridge, resolve=PUBLIC,
                                 today=TODAY, sleep=lambda s: None, jitter=0)
    assert out["connected"] is True and out["accounts"] == 1
    assert "bridge-pass" not in json.dumps(out)


def test_a_reconnect_whose_first_fetch_fails_offers_the_retry(conn, data_dir, monkeypatch):
    from hermes_finance import features
    bridge = Bridge(account_set([card()]))
    confirmed(conn, bridge)
    assert simplefin_sync.status(conn, TODAY)["state"] == "connected"
    simplefin_sync.disconnect(conn)
    st = simplefin_sync.status(conn, TODAY)
    assert st["state"] == "disconnected" and st["needs_retry"] is False
    bridge.accounts_status = 500                          # the new token is claimed, but its first fetch fails
    token = base64.urlsafe_b64encode(b"https://bridge.example/simplefin/claim/again").decode().rstrip("=")
    with pytest.raises(simplefin.SimpleFinError):
        simplefin_sync.claim(conn, token, transport=bridge, resolve=PUBLIC, minute=7, today=TODAY)
    st = simplefin_sync.status(conn, TODAY)
    assert st["needs_retry"] is True and st["state"] == "needs_retry"
    assert st["connected"] is False and st["needs_confirm"] is False
    bridge.accounts_status = 200
    monkeypatch.setattr(simplefin, "urllib_transport", bridge)
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    out = features.simplefin_claim(conn, {}, TODAY)       # no token: the saved link is retried, not asked for again
    assert out["accounts"][0]["id"] == "ACT-1"
    assert out["status"]["needs_confirm"] is True
    assert len([c for c in bridge.calls if c[0] == "claim"]) == 2   # the first claim and the reconnect's, no third
    out = features.simplefin_confirm(conn, {"accounts": [{"key": "CON-1:ACT-1", "type": "checking"}]}, TODAY)
    assert out["connected"] is True and out["state"] == "connected" and out["needs_retry"] is False
    assert len([c for c in bridge.calls if c[0] == "claim"]) == 2


def test_the_daily_run_waits_a_jitter_and_the_manual_one_does_not(conn):
    bridge = Bridge(account_set([card(txs=[])]))
    confirmed(conn, bridge)
    sleeps = []
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=sleeps.append, jitter=59)
    assert out["ok"] and sleeps == [59]
    sleeps.clear()
    out = simplefin_sync.sync(conn, "manual", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=sleeps.append, jitter=59)
    assert out["ok"] and sleeps == []


def test_the_daily_sync_runs_without_an_injected_jitter(conn):
    bridge = Bridge(account_set([card()]))
    confirmed(conn, bridge)
    bridge.payload = account_set([card(txs=[tx("T1", TODAY, "-5.00", "Coffee shop")])])
    sleeps = []
    before = conn.execute("SELECT COUNT(*) FROM simplefin_requests").fetchone()[0]
    out = simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=sleeps.append)
    assert out["ok"] is True and out["added"] == 1 and out["requests"] == 1
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= 59
    assert conn.execute("SELECT COUNT(*) FROM simplefin_requests").fetchone()[0] == before + 1


def test_the_scheduled_cli_path_runs_the_daily_sync(conn, data_dir, monkeypatch, capsys):
    from hermes_finance import cli
    bridge = Bridge(account_set([card(txs=[tx("T1", TODAY, "-5.00", "Coffee shop")])]))
    confirmed(conn, bridge)
    real = simplefin_sync.sync

    def wrapped(connection, trigger="manual", **kwargs):
        assert trigger == "daily"
        assert "jitter" not in kwargs and "rng" not in kwargs
        return real(connection, trigger, transport=bridge, resolve=PUBLIC, today=TODAY,
                    sleep=lambda seconds: None, **kwargs)

    monkeypatch.setattr(cli.simplefin_sync, "sync", wrapped)
    assert cli.main(["sync", "--scheduled"]) == 0
    out = capsys.readouterr().out
    assert "SimpleFIN: +" in out


def test_a_plaid_failure_does_not_stop_simplefin(conn, monkeypatch):
    from conftest import FakePlaid, add_item, err
    from hermes_finance import config, syncnow
    from hermes_finance.plaid import PlaidClient
    from hermes_finance.config import Settings
    add_item(conn)
    fake = FakePlaid()
    fake.on("/item/get", err("ITEM_LOGIN_REQUIRED"))
    plaid = PlaidClient(Settings("fakeclientid000000000001", "fakesecret0000000000000000abcd", "production"),
                        transport=fake)
    bridge = Bridge(account_set([card(txs=[tx("T1", TODAY - timedelta(days=1), "-25.00", "Grocery store")])]))
    confirmed(conn, bridge)
    monkeypatch.setattr(simplefin, "urllib_transport", bridge)
    monkeypatch.setattr(simplefin.socket, "getaddrinfo", PUBLIC)
    out = syncnow.run(conn, lambda: plaid)
    assert out["ok"] is False and out["items"] and not out["items"][0]["ok"]
    assert out["simplefin"]["ok"] is True and out["simplefin"]["connected"] is True
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'simplefin'").fetchone()[0] == 1


def test_the_status_surfaces_the_last_ambiguous_count(conn):
    txs = [tx("P1", TODAY - timedelta(days=7), "-40.00", "Grocery store", pending=True),
           tx("P2", TODAY - timedelta(days=3), "-40.00", "Grocery store", pending=True),
           tx("B1", TODAY - timedelta(days=2), "-40.00", "Grocery store"),
           tx("B2", TODAY - timedelta(days=1), "-40.00", "Grocery store")]
    bridge = Bridge(account_set([card(txs=txs)]))
    confirmed(conn, bridge)
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    st = simplefin_sync.status(conn)
    assert st["last_result"]["ambiguous"] >= 2
