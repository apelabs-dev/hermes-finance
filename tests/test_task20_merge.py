"""TASK-20: TASK-19's routes behind TASK-18's gate after the merge. The Alpaca key routes (and every other route that
changes something) go through the access modes: with the built-in login a write needs a session and its CSRF token
(POST, DELETE and anything else that isn't a read), behind the proxy login an allowed identity, and "This computer
only" refuses anything proxied. Settings' Alpaca row reads the key file alpaca.py writes."""

from __future__ import annotations

import inspect
import json
import re

import pytest

from hermes_finance import admin, alpaca, auth, config, db, features, prefs, secretstore
from hermes_finance.web import MANUAL_PATH, App

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
KEY_ID, SECRET = "AKTESTKEY0001", "s3cretS3cretS3cretS3cret0001"
ACCOUNT = {"status": "ACTIVE", "cash": "125.34", "equity": "1230.00", "currency": "USD", "account_number": "PA0012344321"}
POSITIONS = [{"symbol": "AAPL", "qty": "3", "current_price": "227.52", "market_value": "682.56", "cost_basis": "601.20",
              "asset_class": "us_equity"}]
# every table a write could touch: a refused request leaves each one as it was
TABLES = ("transaction_splits", "transaction_notes", "tags", "transaction_tags", "saved_filters", "category_rollover", "goals",
          "goal_entries", "alpaca_snapshots", "categories", "classifications", "rules", "manual_accounts", "settings",
          "mcp_tokens", "auth_sessions", "auth_users", "imports", "transactions",
          "tracked_bills", "pending_actions")   # (TASK-21: its writes too)


@pytest.fixture(autouse=True)
def quick_hashes(monkeypatch):
    from argon2 import PasswordHasher
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "hermes-finance" / "alpaca-live.env"
    monkeypatch.setenv("HERMES_ALPACA_KEY_FILE", str(path))
    return path


@pytest.fixture
def live(monkeypatch, keyfile):
    """Alpaca switched on for the test, answered by a fake transport (never the network)."""
    calls = []

    def fake(host, target, headers):
        calls.append((host, target))
        return 200, json.dumps(ACCOUNT if target.startswith("/v2/account") else POSITIONS).encode()
    monkeypatch.delenv("HERMES_ALPACA", raising=False)
    monkeypatch.setattr(alpaca, "transport", fake)
    monkeypatch.setattr(alpaca, "_store_logo", lambda conn, fetch=None: None)
    return calls


def call(app, method, path, headers, body=None):
    status, ctype, out, _ = app.respond(method, path, headers, json.dumps(body).encode() if body is not None else b"")
    return status, (json.loads(out) if ctype.startswith("application/json") else out.decode())


@pytest.fixture
def builtin(conn):
    """The built-in login, set up, with one signed-in session: (app, its cookie header, its CSRF token)."""
    with conn:
        conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                     "VALUES ('owner', ?, ?, ?, ?)", (auth.hash_secret("pw"), auth.new_totp_secret(), db.now(), db.now()))
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    token, csrf = auth.create_session(conn, 1)
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    return app, {"Cookie": f"{auth.COOKIE}={token}"}, csrf


def counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def writes() -> list[tuple[str, str]]:
    """Every route that changes something, found in the code rather than listed by hand: TASK-19's (features.py), TASK-18's
    (admin.ROUTES), and web.App._post's own paths (with a manual account's id for its /api/manual/<id>... paths)."""
    out = [("POST", p) for p in features.POST_ROUTES] + [("DELETE", p) for p in features.DELETE_ROUTES]
    out += [(m, p) for m, p in admin.ROUTES if m != "GET"]
    core = re.findall(r'path == "(/api/[^"]+)"', inspect.getsource(App._post))
    out += [("POST", p) for p in core]
    out += [("POST", f"/api/manual/1{sfx}") for sfx in ("", "/archive", "/refresh", "/use-estimate")]
    assert all(MANUAL_PATH.fullmatch(p) for m, p in out if p.startswith("/api/manual/"))
    # and a method nothing answers to yet: the gate refuses it before any routing
    out += [("PUT", "/api/goals/edit"), ("PATCH", features.ALPACA_KEY)]
    return out


WRITES = writes()


def test_the_walk_covers_every_task19_write():
    """Splits, notes / tags, saved filters, goals (entries, earmarks, dismiss), rollover and the Alpaca key are all in it."""
    paths = {p for _, p in WRITES}
    assert {"/api/transactions/split", "/api/transactions/meta", "/api/saved-filters", "/api/saved-filters/delete",
            "/api/goals", "/api/goals/edit", "/api/goals/entry", "/api/goals/dismiss", "/api/budgets/rollover",
            features.ALPACA_KEY} <= paths
    assert ("DELETE", features.ALPACA_KEY) in WRITES and len(WRITES) >= 40


@pytest.mark.parametrize("method, path", WRITES, ids=[f"{m} {p}" for m, p in WRITES])
def test_every_write_without_the_csrf_token_is_refused_with_the_built_in_login(builtin, conn, live, method, path):
    app, cookie, csrf = builtin
    before = counts(conn)
    for headers in ({**JSONH, **cookie}, {**JSONH, **cookie, "X-CSRF-Token": "not-the-token"},
                    {**JSONH, **cookie, "X-CSRF-Token": ""}):
        status, body = call(app, method, path, headers, {"key_id": KEY_ID, "secret": SECRET})
        assert status == 403 and "CSRF" in body["error"], (status, body)
    assert counts(conn) == before and live == []                    # nothing changed, Alpaca never asked


@pytest.mark.parametrize("method, path", [(m, p) for m, p in WRITES if p in features.POST_ROUTES or p in features.DELETE_ROUTES])
def test_with_the_token_the_same_task19_writes_get_past_the_gate(builtin, live, method, path):
    """The control: the 403 above is the CSRF check, not the route (with the token each answers for itself)."""
    app, cookie, csrf = builtin
    status, body = call(app, method, path, {**JSONH, **cookie, "X-CSRF-Token": csrf},
                        {"key_id": KEY_ID, "secret": SECRET} if path == features.ALPACA_KEY else {})
    assert status not in (401, 403), (status, body)


def test_alpaca_key_with_the_built_in_login(builtin, conn, live, keyfile):
    app, cookie, csrf = builtin
    body = {"key_id": KEY_ID, "secret": SECRET}
    # no session: 401 (sign in first), for the POST and the DELETE
    for method in ("POST", "DELETE"):
        status, r = call(app, method, features.ALPACA_KEY, JSONH, body)
        assert status == 401 and r.get("signin") is True, (method, status, r)
    # a session but no CSRF token: 403
    assert call(app, "POST", features.ALPACA_KEY, {**JSONH, **cookie}, body)[0] == 403
    assert live == [] and not keyfile.exists()
    # a session and its token: checked with one read-only call, then saved in its own 0600 file
    status, r = call(app, "POST", features.ALPACA_KEY, {**JSONH, **cookie, "X-CSRF-Token": csrf}, body)
    assert status == 200 and r["connected"] is True and SECRET not in json.dumps(r), r
    assert live[0] == (alpaca.LIVE_HOST, "/v2/account") and keyfile.stat().st_mode & 0o777 == 0o600
    assert call(app, "DELETE", features.ALPACA_KEY, {**JSONH, **cookie}, {})[0] == 403          # DELETE needs it too
    assert keyfile.exists()
    status, r = call(app, "DELETE", features.ALPACA_KEY, {**JSONH, **cookie, "X-CSRF-Token": csrf}, {})
    assert status == 200 and r["removed_file"] is True and not keyfile.exists()
    # a signed-out session (sign out everywhere) is a 401 again
    auth.end_all_sessions(conn)
    assert call(app, "POST", features.ALPACA_KEY, {**JSONH, **cookie, "X-CSRF-Token": csrf}, body)[0] == 401


def test_alpaca_key_behind_the_proxy_login(conn, client, live, keyfile):
    """Behind the proxy login: a wrong identity is refused before anything; an allowed one works. "This computer only"
    (the app exposed with no login) refuses a proxied request even with an identity header."""
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765, ["https://box.tail0.ts.net:8765"], ["owner@example.com"])
    tailnet = {"Host": "box.tail0.ts.net:8765", "Tailscale-User-Login": "owner@example.com", "X-Forwarded-For": "100.64.0.7",
               "Origin": "https://box.tail0.ts.net:8765", "Content-Type": "application/json"}
    body = {"key_id": KEY_ID, "secret": SECRET}
    for method in ("POST", "DELETE"):
        status, r = call(app, method, features.ALPACA_KEY, {**tailnet, "Tailscale-User-Login": "intruder@example.com"}, body)
        assert status == 403 and "not allowed" in r["error"], (method, r)
    assert live == [] and not keyfile.exists()
    status, r = call(app, "POST", features.ALPACA_KEY, tailnet, body)
    assert status == 200 and r["connected"] is True and keyfile.exists()
    assert call(app, "DELETE", features.ALPACA_KEY, tailnet, {})[0] == 200 and not keyfile.exists()
    n = len(live)
    local_only = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    assert local_only.access(conn).mode == "local"
    for method in ("POST", "DELETE"):
        assert call(local_only, method, features.ALPACA_KEY, tailnet, body)[0] == 403
    assert len(live) == n and not keyfile.exists()


@pytest.mark.parametrize("method, path", [("POST", p) for p in features.POST_ROUTES] + [("DELETE", p) for p in features.DELETE_ROUTES])
def test_every_task19_write_refuses_a_foreign_origin_on_this_computer(conn, client, live, method, path):
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    before = counts(conn)
    status, _ = call(app, method, path, {**JSONH, "Origin": "https://evil.example"}, {"key_id": KEY_ID, "secret": SECRET})
    assert status == 403 and counts(conn) == before and live == []


def test_settings_alpaca_row_reads_the_key_file_alpaca_writes(conn, client, live, keyfile):
    """Settings -> Connections -> Alpaca says Live only while alpaca.py's own key file is there, and where it is; the
    secrets file (Plaid, the AI key) no longer takes Alpaca keys at all."""
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    page = call(app, "GET", "/api/settings/page", LOCAL)[1]
    assert page["secrets"]["alpaca"] == {"set": False, "where": secretstore.shown_path(keyfile)}
    assert call(app, "POST", features.ALPACA_KEY, JSONH, {"key_id": KEY_ID, "secret": SECRET})[0] == 200
    page = call(app, "GET", "/api/settings/page", LOCAL)[1]
    assert page["secrets"]["alpaca"] == {"set": True, "where": secretstore.shown_path(keyfile)}
    assert SECRET not in json.dumps(page) and KEY_ID not in json.dumps(page)
    with pytest.raises(secretstore.SecretError):
        secretstore.write({"APCA_API_SECRET_KEY": SECRET})
    assert call(app, "DELETE", features.ALPACA_KEY, JSONH, {})[0] == 200
    assert call(app, "GET", "/api/settings/page", LOCAL)[1]["secrets"]["alpaca"]["set"] is False


def test_first_run_setup_adds_alpaca_through_the_same_read_only_check(conn, client, keyfile, monkeypatch):
    """The setup's optional "Add Alpaca?" step (static/setup.js) posts to the same route as Settings (POST
    /api/alpaca/key -> alpaca.connect): a paper key is refused before any call, an account that isn't ACTIVE after the one
    GET /v2/account, and in both cases nothing is written; a live ACTIVE one is saved in its own 0600 file, and /api/setup
    then says so. Only alpaca.py (its allow-listed GETs) ever talks to Alpaca."""
    answers = {"/v2/account": dict(ACCOUNT, status="ACCOUNT_CLOSED"), "/v2/positions": POSITIONS}
    calls = []

    def fake(host, target, headers):
        calls.append((host, target))
        return 200, json.dumps(answers[target]).encode()
    monkeypatch.delenv("HERMES_ALPACA", raising=False)
    monkeypatch.setattr(alpaca, "transport", fake)
    monkeypatch.setattr(alpaca, "_store_logo", lambda conn, fetch=None: None)
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    st = call(app, "GET", "/api/setup", LOCAL)[1]
    assert st["alpaca"] == {"set": False, "where": secretstore.shown_path(keyfile)}
    status, r = call(app, "POST", features.ALPACA_KEY, JSONH, {"key_id": "PKPAPERKEY0001", "secret": SECRET})
    assert status == 400 and "paper-trading key" in r["error"] and calls == [] and not keyfile.exists()
    status, r = call(app, "POST", features.ALPACA_KEY, JSONH, {"key_id": KEY_ID, "secret": SECRET})
    assert status == 400 and "isn't active" in r["error"] and calls == [(alpaca.LIVE_HOST, "/v2/account")]
    assert not keyfile.exists() and not keyfile.parent.exists()
    answers["/v2/account"] = ACCOUNT
    status, r = call(app, "POST", features.ALPACA_KEY, JSONH, {"key_id": KEY_ID, "secret": SECRET})
    assert status == 200 and r == {"connected": True, "status": "ACTIVE", "equity": 807.9, "cash": 125.34, "positions": 1}  # cash + positions
    assert keyfile.stat().st_mode & 0o777 == 0o600 and keyfile.parent.stat().st_mode & 0o777 == 0o700
    assert all(host == alpaca.LIVE_HOST and (("GET", t.split("?")[0]) in alpaca.ALLOWED) for host, t in calls)
    assert call(app, "GET", "/api/setup", LOCAL)[1]["alpaca"]["set"] is True
    # the setup step and Settings share one form that sends the key to that route only; nothing else talks to Alpaca
    static = config.project_root() / "src" / "hermes_finance" / "static"
    setup_js, settings_js = (static / "setup.js").read_text(), (static / "settings.js").read_text()
    assert "HF.alpacaForm(" in setup_js and "fetch(" not in setup_js and "alpaca.markets/" not in setup_js
    assert re.findall(r"post\('(/api/alpaca[^']*)'", settings_js) == [features.ALPACA_KEY]
    py = config.project_root() / "src" / "hermes_finance"
    assert [p.name for p in sorted(py.glob("*.py")) if "api.alpaca.markets" in p.read_text()] == ["alpaca.py"]
