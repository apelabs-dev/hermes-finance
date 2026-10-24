"""HOTFIX-1: the owner's first-day bugs on the live app (2026-09-29).

  1 sync      "Ask banks for fresh data" ended in "Couldn't sync · Load failed" on his iPhone: the request held 13 Plaid
              refresh calls one after another (40 s or more) and the phone dropped it. Now the page's sync runs in the
              background (POST /api/sync answers within syncnow.ANSWER_WITHIN, else 202; GET /api/sync?rid= looks it up),
              the refresh calls go at once with a timeout each, a dropped answer is looked up and shown as it really
              went, asking inside the minute after a sync says nothing was synced again, and serve.log gets one line per
              sync (and per hung-up client) even without --verbose.
  2 border    while a sync runs, a light runs round the Sync button's border (tests/test_task22f_sync_ui.py)
  3 nav       Goals under Budgets in the phone drawer too (the desktop sidebar had it)
  4 tabs      Settings, the phone tab bar's fifth tab: every tab 44px or more, the labels whole at 13px, at 360/390/430
  5 light     the light theme's selects (a `background` shorthand drew their arrows full size: slate with white
              triangles), and the layout gate's darkbg check that fails on that CSS

Plaid is always a fake (conftest's FakePlaid, or tests/sync_scene.SceneFake in the browser); no test reads the live DB.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import struct
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

import gate_dbs
import sync_scene
from cdp import Chrome, Server, find_chrome
from conftest import FAKE_CLIENT_ID, FAKE_SECRET, TOKEN_A, add_item, page
from factory import acct
from hermes_finance import config, prefs, syncnow
from hermes_finance.config import Settings
from hermes_finance.plaid import PlaidClient
from hermes_finance.web import App, make_server
from test_task22f_sync_ui import HOLD, OPEN, PANEL, chrome, go, later, scene_dbs, site, sync  # noqa: F401  (fixtures)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout  # noqa: E402

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
chrome_only = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
SKIPPED = ("Banks asked for fresh data. Not synced again: the last sync was {} s ago. New items usually land within a few "
           "minutes; tap Sync then.")


@pytest.fixture(autouse=True)
def lock_free():
    """Each test starts with no sync running in this process (a failed test's background sync may still hold LOCK)."""
    t0 = time.monotonic()
    while syncnow.LOCK.locked() and time.monotonic() - t0 < 10:
        time.sleep(0.05)
    yield


def post(app, body, headers=JSONH):
    status, _, out = app.handle("POST", "/api/sync", headers, json.dumps(body).encode())
    return status, json.loads(out)


def lookup(app, rid):
    status, _, out = app.handle("GET", f"/api/sync?rid={rid}", LOCAL, b"")
    assert status == 200
    return json.loads(out)


def settle(app, rid, limit=10.0):
    """Look the sync up, as the page does, until it's done: its state."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        s = lookup(app, rid)
        if not s.get("running"):
            return s
        time.sleep(0.05)
    raise AssertionError(f"sync {rid} still running after {limit} s")


class Recording:
    """A Plaid transport around the FakePlaid that records each call's timeout, and can hold one path for a while."""

    def __init__(self, fake, slow: dict | None = None):
        self.fake, self.slow, self.timeouts = fake, slow or {}, []

    def __call__(self, url, data, headers, timeout):
        path = url.split("plaid.com", 1)[1]
        self.timeouts.append((path, timeout))
        hold = self.slow.get(path)
        if callable(hold):
            hold = hold(json.loads(data))
        if hold:
            if hold > timeout:              # a call slower than its timeout: urllib gives up at the timeout
                time.sleep(min(timeout, 3.0))
                raise TimeoutError("timed out")
            time.sleep(hold)
        return self.fake(url, data, headers, timeout)

    def client(self):
        return PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=self)


def banks(conn, fake, n):
    """n connected banks, Transactions only, each with a checking account; Plaid answers each refresh at once."""
    for i in range(n):
        add_item(conn, f"item_{i}", f"ins_{i}", name=f"Bank {i}", token=f"access-production-{i:08d}-1111-2222-3333-4444")
        acct(conn, f"acc_{i}", f"item_{i}", "Checking", current=100.0 + i)
    fake.on("/transactions/refresh", {"request_id": "req-refresh"})
    fake.on("/investments/refresh", {"request_id": "req-refresh"})
    return [f"item_{i}" for i in range(n)]


# --- 1. the request never waits on Plaid for long --------------------------------------------------------------------

def test_a_slow_plaid_never_holds_the_request(conn, fake, monkeypatch):
    """8 banks whose refresh calls take 0.4 s each: 3.2 s one after another, as the live request did (13 calls, 40 s or
    more). The POST answers 202 within ANSWER_WITHIN, the calls go at once, and the look-up has the whole result."""
    ids = banks(conn, fake, 8)
    rec = Recording(fake, {"/transactions/refresh": 0.4})
    app = App(config.db_path(), rec.client, "127.0.0.1", 8765)
    monkeypatch.setattr(syncnow, "ANSWER_WITHIN", 0.2, raising=False)
    t0 = time.monotonic()
    status, r = post(app, {"refresh": True, "rid": "slow-1"})
    answered = time.monotonic() - t0
    assert status == 202 and answered < 1.0, (status, answered)            # the old code: 200 after 3.2 s or more
    assert r["running"] is True and r["rid"] == "slow-1" and r["refresh"] is True
    done = settle(app, "slow-1")
    total = time.monotonic() - t0
    assert total < 2.0, total                                              # at once: ~0.4 s, not 3.2
    res = done["result"]
    assert [a["item_id"] for a in res["refresh_requested"]] == ids and res["synced"] == 8
    assert sum(p == "/transactions/refresh" for p, _ in fake.calls) == 8
    assert not syncnow.LOCK.locked()


def test_each_refresh_call_has_its_own_timeout_and_a_hung_one_can_be_asked_again(conn, fake, monkeypatch):
    """A refresh call gets REFRESH_TIMEOUT (not the client's 60 s); one that doesn't answer in time counts as not reached
    (TASK-22c: asked again right away), and the others are asked all the same."""
    banks(conn, fake, 3)
    monkeypatch.setattr(syncnow, "REFRESH_TIMEOUT", 0.3, raising=False)
    rec = Recording(fake, {"/transactions/refresh": lambda b: 5.0 if b["access_token"].startswith("access-production-00000001")
                           else None})
    app = App(config.db_path(), rec.client, "127.0.0.1", 8765)
    t0 = time.monotonic()
    status, r = post(app, {"refresh": True})
    if status == 202:
        r = settle(app, r["rid"])["result"]
    assert time.monotonic() - t0 < 2.5
    assert {t for p, t in rec.timeouts if p.endswith("/refresh")} == {0.3}  # the old code: 60 s, the client's
    assert [a["item_id"] for a in r["refresh_requested"]] == ["item_0", "item_2"]
    assert [(a["item_id"], a["error_code"]) for a in r["refresh_failed"]] == [("item_1", "NETWORK_ERROR")]
    asked = json.loads(conn.execute("SELECT value FROM settings WHERE key = 'plaid.refresh_log'").fetchone()[0])
    assert sorted(asked) == ["item_0", "item_2"]                            # item_1 may be asked again right away


def test_asked_inside_the_minute_after_a_sync_says_nothing_was_synced_again(conn, fake, monkeypatch):
    """The live evening: a Sync now, then "Ask banks for fresh data" 30 s later. Every bank is asked; none is synced again
    (the minute between syncs holds), and the answer says so, with when a sync can run; never "synced", never an error.
    Sync now right after is refused with the seconds left; after the minute, asking again waits out the 30 minutes (and
    syncs); on a paid plan with the switch off, asking is refused."""
    ids = banks(conn, fake, 2)
    app = App(config.db_path(), lambda: PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=fake),
              "127.0.0.1", 8765)
    assert post(app, {})[0] == 200
    rows = conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0]
    later(monkeypatch, 30)
    status, r = post(app, {"refresh": True})
    assert status == 200 and [a["item_id"] for a in r["refresh_requested"]] == ids
    assert r["synced"] == 0 and r["items"] == [] and r["ok"] is True and r["errors"] == []
    assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == rows          # no sync ran
    skip = r["sync_skipped"]   # (sync_log's times are whole seconds: the last sync was 30 to 31 s ago)
    assert skip["ago"] in (30, 31) and skip["retry_after"] in (30, 31) and skip["ago"] + skip["retry_after"] in (60, 61)
    assert r["note"] == r["message"] == SKIPPED.format(skip["ago"])         # (the old code: message "", note "tap Sync")
    assert {w["retry_after"] for w in r["waiting"]} <= {30, 31}
    status, r = post(app, {})                                               # Sync now right after: the minute
    assert status == 429 and r["retry_after"] in (29, 30, 31)
    later(monkeypatch, 61)
    status, r = post(app, {"refresh": True})                                # the 30 minutes hold; the sync runs
    assert status == 200 and r["refresh_requested"] == [] and len(r["refresh_waiting"]) == 2 and r["synced"] == 2
    assert "sync_skipped" not in r and "last 30 minutes" in r["note"]
    prefs.save(conn, {"plaid.plan": "paid"}, allowed=list(prefs.SPEC))    # Trial / Paid: off on a paid plan
    assert post(app, {"refresh": True})[0] == 403


def test_a_client_that_hangs_up_loses_nothing(conn, fake, monkeypatch, caplog, capsys):
    """A real `finance serve` socket: the page's POST arrives and the phone hangs up (a reset, as a dropped connection
    through the proxy) before the answer. The sync goes on, GET /api/sync?rid= has its result, and the log has one line
    for the hang-up, not a traceback (the old code: a BrokenPipe traceback, and nothing to look up)."""
    caplog.set_level(logging.INFO, logger="hermes_finance.requests")
    # (only caplog's handler on the root: an earlier test's `finance` CLI run may have left one on a closed stream)
    monkeypatch.setattr(logging.getLogger(), "handlers", [caplog.handler])
    add_item(conn)
    monkeypatch.setattr(syncnow, "ANSWER_WITHIN", 0.3, raising=False)
    fake.on("/transactions/refresh", lambda b: (time.sleep(0.8), {"request_id": "req-refresh"})[1])
    app = App(config.db_path(), lambda: PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=fake),
              "127.0.0.1", 0)
    srv = make_server(app, "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"refresh": True, "rid": "hangup-1"}).encode()
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(f"POST /api/sync HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        time.sleep(0.1)                                                     # the server has it
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()                                                           # the phone hangs up (RST)
        state, t0 = None, time.monotonic()
        while time.monotonic() - t0 < 10:                                   # the page looks it up
            h = HTTPConnection("127.0.0.1", port, timeout=5)
            h.request("GET", "/api/sync?rid=hangup-1")
            resp = h.getresponse()
            status, state = resp.status, json.loads(resp.read() or b"{}")
            h.close()
            assert status == 200, f"nothing to look up: GET /api/sync -> {status}"
            if not state["running"]:
                break
            time.sleep(0.1)
        assert state["found"] and state["result"]["refresh_requested"][0]["item_id"] == "item_a"
        assert state["result"]["synced"] == 1                               # (the first sync: out of any minute)
        time.sleep(0.3)
    finally:
        srv.shutdown()
        srv.server_close()
    lines = [r.getMessage() for r in caplog.records if r.name == "hermes_finance.requests"]
    assert any(re.fullmatch(r"POST /api/sync: the client went away before the 202 answer \((BrokenPipe|ConnectionReset)Error\)", x)
               for x in lines), lines
    err = capsys.readouterr().err
    assert "Traceback" not in err and "Exception occurred during processing of request" not in err, err


def test_one_info_line_per_sync_in_serve_log_even_without_verbose(conn, fake, monkeypatch):
    """`finance serve` runs with the root logger at WARNING (no --verbose), as live: each /api/sync still writes one INFO
    line: sync or refresh, the banks, how long, what happened (counts only), and a refusal with its status."""
    add_item(conn)
    acct(conn, "acc_chk", "item_a", "Checking", current=1000.0)
    fake.on("/transactions/sync", page(added=[{**_txn("t1")}, {**_txn("t2")}]))
    fake.on("/transactions/refresh", {"request_id": "req-refresh"})
    app = App(config.db_path(), lambda: PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=fake),
              "127.0.0.1", 8765)
    root, records = logging.getLogger(), []
    handler = logging.Handler()
    handler.emit = records.append
    level = root.level
    root.setLevel(logging.WARNING)
    root.addHandler(handler)
    try:
        assert post(app, {"rid": "log-1"})[0] == 200
        later(monkeypatch, 20)
        assert post(app, {"refresh": True, "rid": "log-2"})[0] == 200
        assert post(app, {"refresh": True, "rid": "log-3"})[0] == 429
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    lines = [r.getMessage() for r in records if r.name == "hermes_finance.requests" and r.levelno == logging.INFO]
    assert len(lines) == 3, lines
    assert re.fullmatch(r"POST /api/sync log-1: sync, 1 bank\(s\), \d+\.\d s, answered in the request: synced 1 of 1, "
                        r"2 new, 0 updated, 0 balance\(s\) moved", lines[0]), lines[0]
    assert re.fullmatch(r"POST /api/sync log-2: refresh, 1 bank\(s\), \d+\.\d s, answered in the request: asked 1 bank\(s\); "
                        r"not synced: every bank synced under a minute ago \(the last 20 s ago\)", lines[1]), lines[1]
    assert re.fullmatch(r"POST /api/sync log-3: refresh, 1 bank\(s\), \d+\.\d s, refused 429: Synced a moment ago\. "
                        r"Try again in \d+ s\.", lines[2]), lines[2]
    assert all(TOKEN_A not in x and "$" not in x for x in lines)           # no token, no amount


def _txn(tid):
    from conftest import txn
    return txn(tid)


def test_the_look_up_the_rid_and_a_failed_sync(conn, fake, monkeypatch):
    """A rid the server doesn't know: found False with the last sync (the POST never reached it, or it restarted); a bad
    rid is refused; a sync that fails inside says so to the look-up and gives the lock back."""
    add_item(conn)
    app = App(config.db_path(), lambda: PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=fake),
              "127.0.0.1", 8765)
    s = lookup(app, "never-sent")
    assert (s["found"], s["running"], s["rid"]) == (False, False, "never-sent") and "last_sync" in s
    assert post(app, {"rid": "no spaces"})[0] == 400 and post(app, {"rid": "x" * 65})[0] == 400
    monkeypatch.setattr(syncnow, "sync_items", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    status, r = post(app, {"rid": "boom-1"})
    assert status == 500 and r["error"] == "internal error (RuntimeError)"
    assert lookup(app, "boom-1")["error"] == "internal error (RuntimeError)" and not syncnow.LOCK.locked()


# --- 1. the page: a dropped answer, a lost request, a slow sync, and the words --------------------------------------

DROP = """((reach) => { const f = window.fetch; window.fetch = (u, o) => {
  if (String(u).endsWith('/api/sync') && o && o.method === 'POST') { if (reach) f(u, o); return Promise.reject(new TypeError('Load failed')); }
  return f(u, o); }; return 1; })"""
COUNT = """(() => { const f = window.fetch; window.__looks = 0; window.fetch = (u, o) => {
  if (String(u).includes('/api/sync?')) window.__looks += 1; return f(u, o); }; return 1; })()"""


@chrome_only
def test_a_dropped_answer_shows_what_really_happened(chrome, site, monkeypatch):
    """The POST reaches the server but its answer never comes back (Safari: "Load failed"). The page looks the sync up
    and shows its real result; then the same for "Ask banks for fresh data" (the live case)."""
    b, s = chrome, site()
    go(b, s)
    b.js(f"{DROP}(true)")
    p = sync(b)
    assert (p["title"], p["sub"], p["sums"]) == ("Synced just now", "All 8 banks", ["12 new transactions · 10 updated"])
    assert "Load failed" not in p["text"] and "Couldn’t sync" not in p["text"]      # (the old page: exactly that)
    later(monkeypatch, 120)
    b.js("document.querySelector('#syncp .spask').click()")
    b.js(OPEN)
    b.settle()
    p = b.js(PANEL)
    assert (p["title"], p["sub"]) == ("Banks asked for fresh data", "All 8 banks") and "Load failed" not in p["text"]
    assert s.fake.calls.count(("/transactions/refresh", "demo_chase")) == 1
    assert b.problems() == []


@chrome_only
def test_a_request_that_never_reached_the_server_says_the_connection_was_lost(chrome, site):
    b, s = chrome, site()
    go(b, s)
    before = b.js("document.getElementById('sync').textContent")
    b.js(f"{DROP}(false)")
    p = sync(b)
    assert (p["title"], p["sub"]) == ("Lost the connection", "The server didn’t get it, so nothing ran. Tap Sync again.")
    assert "spic warn" in p["tone"] and s.fake.calls == []
    assert b.js("document.getElementById('sync').textContent") == before
    assert b.problems() == []


@chrome_only
def test_look_ups_that_fail_a_while_are_retried(chrome, site):
    """A phone back from the background: the answer was lost and the first two look-ups fail too; the third finds the
    sync done and the panel shows it."""
    b, s = chrome, site()
    go(b, s)
    b.js("""(() => { const f = window.fetch; let n = 0; window.fetch = (u, o) => {
      if (String(u).endsWith('/api/sync') && o && o.method === 'POST') { f(u, o); return Promise.reject(new TypeError('Load failed')); }
      if (String(u).includes('/api/sync?') && n++ < 2) return Promise.reject(new TypeError('Load failed'));
      return f(u, o); }; return 1; })()""")
    p = sync(b)
    assert (p["title"], p["sub"]) == ("Synced just now", "All 8 banks")
    assert b.problems() == []


@chrome_only
def test_no_answer_and_no_look_up_says_the_banks_may_still_have_been_asked(chrome, site, monkeypatch):
    """Ask banks for fresh data with the network gone: the POST reaches the server but its answer doesn't come back, and
    every look-up fails too (LOST_TRIES, for 20 s). The panel says the connection was lost and that the banks were still
    asked if the server got it (it did: Chase was asked), never "Couldn't sync · Load failed"."""
    b, s = chrome, site()
    sync_scene.presync(s.conn, s.fake)
    later(monkeypatch, 120)
    go(b, s)
    sync(b)
    b.js("""(() => { const f = window.fetch; window.fetch = (u, o) => {
      if (String(u).endsWith('/api/sync') && o && o.method === 'POST') { f(u, o); return Promise.reject(new TypeError('Load failed')); }
      if (String(u).includes('/api/sync?')) return Promise.reject(new TypeError('Load failed'));
      return f(u, o); }; return 1; })()""")
    b.js("document.querySelector('#syncp .spask').click()")
    time.sleep(1.0)
    assert b.js("document.getElementById('syncnow').innerText.trim()") == "Asking banks…"   # still looking it up
    b.js(OPEN.replace("15000", "40000"))
    b.settle()
    p = b.js(PANEL)
    assert (p["title"], p["sub"]) == ("Lost the connection", "If the server got it, your banks were still asked. Reload the "
                                                             "page in a minute to see it.")
    assert "spic warn" in p["tone"] and "Load failed" not in p["text"]
    assert s.fake.calls.count(("/transactions/refresh", "demo_chase")) == 1               # it did get it
    assert b.problems() == []


@chrome_only
def test_a_slow_sync_is_polled_to_its_end(chrome, site, monkeypatch):
    """Plaid slower than ANSWER_WITHIN: the POST answers 202, the page looks the sync up until it's done, the button
    says Syncing… (its border running) all along, and the panel shows the result."""
    monkeypatch.setattr(syncnow, "ANSWER_WITHIN", 0.2, raising=False)
    b, s = chrome, site()
    slow = lambda url, data, headers, timeout: (time.sleep(0.08), s.fake(url, data, headers, timeout))[1]   # noqa: E731
    s.srv.app.client_factory = lambda: PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=slow)
    go(b, s)
    b.js(COUNT)
    b.js("document.getElementById('syncnow').click()")
    time.sleep(0.8)
    assert b.js("document.getElementById('syncnow').innerText.trim()") == "Syncing…"
    b.js(OPEN)
    b.settle()
    p = b.js(PANEL)
    assert (p["title"], p["sums"]) == ("Synced just now", ["12 new transactions · 10 updated"])
    assert b.js("window.__looks") >= 1
    assert b.problems() == []


@chrome_only
def test_asked_inside_the_minute_the_panel_says_not_synced_again(chrome, site, monkeypatch):
    b, s = chrome, site()
    sync_scene.presync(s.conn, s.fake)
    later(monkeypatch, 120)
    go(b, s)
    sync(b)                                                                # nothing new; the sync is now
    later(monkeypatch, 20)                                                 # 20 s later: Ask banks
    b.js("document.querySelector('#syncp .spask').click()")
    b.js(OPEN)
    b.settle()
    p = b.js(PANEL)
    assert (p["title"], p["sub"]) == ("Banks asked for fresh data", "All 8 banks")
    assert len(p["more"]) == 1 and re.fullmatch(r"Not synced again: the last sync was 2[01] s ago\.", p["more"][0]), p["more"]
    assert not any(m.startswith("This sync") for m in p["more"]) and "Synced just now" not in p["text"]
    assert b.problems() == []


@chrome_only
def test_a_sync_that_left_banks_in_their_minute_never_claims_all_of_them(chrome, site, monkeypatch):
    """7 banks out of their minute and Nova synced 20 s ago: the panel says 7 of 8 banks (the old one: "All 8 banks")."""
    b, s = chrome, site()
    sync_scene.presync(s.conn, s.fake)
    with s.conn:
        s.conn.execute("UPDATE sync_log SET started_at = '2020-01-01T00:00:00Z' WHERE item_id != 'demo_nova'")
    later(monkeypatch, 20)
    go(b, s)
    p = sync(b)
    assert (p["title"], p["sub"]) == ("Synced just now", "7 of 8 banks; the rest synced a moment ago")
    assert b.problems() == []


# --- 3 / 4. Goals under Budgets; Settings the fifth tab ---------------------------------------------------------------

ORDER = """[...document.querySelectorAll('#nav a')].filter((a) => a.getClientRects().length)
  .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top).map((a) => a.dataset.pg)"""
TABS = """(() => { const acc = getComputedStyle(document.documentElement).getPropertyValue('--acc').trim();
  return [...document.querySelectorAll('#tabbar a')].map((a) => { const r = a.getBoundingClientRect(), s = a.querySelector('span');
  const hit = getComputedStyle(a, '::after');
  return {pg: a.dataset.pg, label: s.textContent, left: r.left, right: r.right, w: r.width, h: r.height,
    fs: parseFloat(getComputedStyle(s).fontSize), lines: s.getClientRects().length, whole: s.scrollWidth <= s.clientWidth,
    labelIn: s.getBoundingClientRect().left >= r.left - 0.5 && s.getBoundingClientRect().right <= r.right + 0.5,
    icon: Boolean(a.querySelector('svg')), current: a.getAttribute('aria-current'),
    iconColor: getComputedStyle(a.querySelector('svg')).color, hitW: parseFloat(hit.width) || r.width,
    hitH: parseFloat(hit.height) || r.height}; }); })()"""


@chrome_only
def test_goals_sits_under_budgets_in_the_sidebar_and_the_drawer(chrome, site):
    b, s = chrome, site()
    go(b, s, width=1440)
    assert b.js(ORDER) == ["overview", "transactions", "budgets", "goals", "recurring", "accounts", "investments", "debt"]
    for width in (390, 768):
        go(b, s, width=width)
        b.js("document.getElementById('menu').click()")
        time.sleep(0.4)
        assert b.js(ORDER) == ["overview", "transactions", "budgets", "goals", "accounts", "recurring", "investments", "debt"]
    assert b.problems() == []


@chrome_only
@pytest.mark.parametrize("width", [360, 390, 430])
def test_settings_is_the_fifth_tab_and_every_tab_fits(chrome, site, width):
    b, s = chrome, site()
    for pg in ("overview", "settings"):
        go(b, s, page=pg, width=width)
        tabs = b.js(TABS)
        assert [t["label"] for t in tabs] == ["Overview", "Transactions", "Budgets", "Accounts", "Settings"]
        assert [t["pg"] for t in tabs][-1] == "settings" and all(t["icon"] for t in tabs)
        assert len({t["fs"] for t in tabs}) == 1 and tabs[0]["fs"] >= 13                 # one size, 13px (TASK-18)
        for t in tabs:
            assert t["w"] >= 44 and t["h"] >= 44 and t["hitW"] >= 44 and t["hitH"] >= 44, t
            assert t["lines"] == 1 and t["whole"] and t["labelIn"], t                     # whole, on one line
            assert t["left"] >= -0.5 and t["right"] <= width + 0.5, t
        assert all(a["right"] <= c["left"] + 0.5 for a, c in zip(tabs, tabs[1:]))          # side by side, no overlap
        current = [t["pg"] for t in tabs if t["current"] == "page"]
        assert current == [pg], current
    lit = b.js(TABS)[-1]
    assert lit["iconColor"] == "rgb(142, 168, 255)"                                        # the accent, on #/settings
    assert b.problems() == []


# --- 5. the light theme's selects, and the gate check --------------------------------------------------------------

SELECTS = """(() => [...document.querySelectorAll('#page select, #sheet select')].filter((e) => e.getClientRects().length)
  .map((e) => { const cs = getComputedStyle(e); return {cls: e.className, bg: cs.backgroundColor, size: cs.backgroundSize,
    image: cs.backgroundImage.includes('gradient')}; }))()"""


@chrome_only
def test_the_light_themes_selects_are_light_with_a_small_arrow(chrome, site):
    """Settings' selects (Weekly summary's day among them) on the light fill with their 5px arrow; the old light.css drew
    the arrow's two gradients over the whole select (background-size auto): slate, with white triangles."""
    b, s = chrome, site()
    go(b, s, page="settings", width=390, theme="light")
    sel = b.js(SELECTS)
    selv = [x for x in sel if "selv" in x["cls"]]
    assert len(selv) >= 5
    assert all(x["bg"] == "rgb(247, 248, 251)" and x["size"] == "5px 5px, 5px 5px" and x["image"] for x in selv), selv
    day = b.js("getComputedStyle(document.querySelector(\"select[aria-label='Summary day']\")).backgroundSize")
    assert day == "5px 5px, 5px 5px"
    go(b, s, page="transactions", width=390, theme="light")
    # polish P0: a row's category is the picker's button (catpick.css), drawn like the select it replaced
    cat = [x for x in b.js(SELECTS.replace("select", "button.catb")) if "catb" in x["cls"].split()]
    assert cat and all(x["size"] == "4px 4px, 4px 4px" and x["image"] for x in cat)
    assert b.problems() == []


@pytest.fixture(scope="module")
def gate_site(tmp_path_factory):
    srv = Server(gate_dbs.build("full", tmp_path_factory.mktemp("hot1") / "finance.db"))
    yield srv
    srv.close()


OLD_SELECT = ":root[data-theme=light] .selv { background: #f7f8fb; }"   # light.css before HOTFIX-1
OLD_CAR = ".vd-chip { background: #171c28 !important; } .vd-facts div, .vd-quiet { background: #11151e !important; }"


@chrome_only
@pytest.mark.parametrize("state, width, css, what", [
    ("settings", 390, OLD_SELECT, "select.selv"),
    ("settings", 1440, OLD_SELECT, "select.selv"),
    ("asset-car-photo", 1440, OLD_CAR, "span.vd-chip"),
])
def test_darkbg_fails_on_the_old_light_css_and_passes_with_the_fix(gate_site, tmp_path, state, width, css, what):
    old = audit_layout.audit(gate_site.url, tmp_path / "old", "old", (width,), log=lambda line: None, states={state},
                             inject=css, themes=("light",), self_test=False, workers=1)
    vs = [v for e in old["widths"][str(width)].values() for v in e["checks"]["darkbg"]["violations"]]
    assert vs and all(v["what"][0].startswith(what) for v in vs), vs
    fixed = audit_layout.audit(gate_site.url, tmp_path / "fixed", "fixed", (width,), log=lambda line: None, states={state},
                               themes=("light",), self_test=False, workers=1)
    entry = next(iter(fixed["widths"][str(width)].values()))
    assert fixed["fail"] == 0 and entry["checks"]["darkbg"]["examined"] > 20


@chrome_only
def test_a_blind_darkbg_check_stops_the_run(gate_site, monkeypatch):
    monkeypatch.setattr(audit_layout, "DARK_PLANT_JS", audit_layout.DARK_PLANT_JS.replace("add('plantdarkarrow'", "(() => 0)('x'"))
    with Chrome() as b, pytest.raises(AssertionError, match="darkbg"):
        audit_layout.selftest(b, gate_site.url, lambda line: None)


def test_the_plan_runs_the_sheets_and_the_sync_panel_in_the_light_theme_too():
    plan = {(w, t, n) for w, t, n, *_ in audit_layout.plan("full", audit_layout.WIDTHS, None)}
    for name in ("add-account", "form-loan-errors", "budgets-editor", "sync-result", "sync-lost", "sync-asked-skip",
                 "phone-drawer", "toast", "asset-car-photo"):
        runs = {(w, t) for w, t, n in plan if n == name}
        assert {(1440, "light"), (390, "light")} & runs == {(w, "light") for w in (1440, 390) if (w, "dark") in runs}, name
    assert "darkbg" in audit_layout.CHECKS
