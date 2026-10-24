"""TASK-18: the three access modes (access.py, the gate in web.py, `finance serve` / `finance access`): the one-time move
of today's Tailscale gate into "Behind your proxy login", the refusal to start when the app would be reachable with no
login, "This computer only" refusing every proxied or foreign-Host request, the proxy mode's settings, and the MCP
bearer tokens (valid, revoked, wrong, missing, never accepted on /api/*)."""

from __future__ import annotations

import json
import logging

import pytest

from hermes_finance import access, auth, cli, config, db, prefs, web
from hermes_finance.web import App

TS_HOST = "box.tail0000.ts.net"
PUBLIC_URL = f"https://{TS_HOST}"
USER = "owner@example.com"
LOCAL = {"Host": "127.0.0.1:8765"}
TAILNET = {"Host": TS_HOST, "Tailscale-User-Login": USER, "X-Forwarded-For": "100.64.0.7", "X-Forwarded-Proto": "https"}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def call(app, method, path, headers, body=None):
    status, ctype, raw = app.handle(method, path, headers, json.dumps(body).encode() if body is not None else b"")
    return status, (json.loads(raw) if ctype.startswith("application/json") else raw.decode())


# --- the one-time migration ----------------------------------------------------------------------------------------

def test_an_install_started_with_public_urls_becomes_the_proxy_mode_with_its_logins(conn):
    assert access.migrate(conn, [PUBLIC_URL], ["Owner@Example.com"]) == "proxy"
    assert prefs.get_many(conn, ["access.mode", "access.allowed_identities", "access.public_urls"]) == {
        "access.mode": "proxy", "access.allowed_identities": [USER], "access.public_urls": [PUBLIC_URL]}
    # one time: a later start with other flags changes nothing saved (the flags still count, on top)
    assert access.migrate(conn, ["https://other.ts.net"], ["someone@else.example"]) is None
    assert prefs.get(conn, "access.allowed_identities") == [USER]
    acc = access.load(conn, ["https://other.ts.net"], ["someone@else.example"])
    assert acc.identities == {USER, "someone@else.example"} and acc.mode == "proxy"


def test_a_first_start_without_allow_user_is_seeded_by_the_next_that_has_one(conn):
    assert access.migrate(conn, [PUBLIC_URL], []) == "proxy"
    assert prefs.get(conn, "access.allowed_identities") == []
    assert access.migrate(conn, [PUBLIC_URL], [USER]) is None
    assert prefs.get(conn, "access.allowed_identities") == [USER]


def test_an_identity_allowed_before_the_first_start_survives_the_migration(conn):
    # the upgrade path the refusal message recommends: `finance access allow <login>`, then the unchanged
    # `finance serve --public-url ...` line. The first start must keep that identity, not overwrite it with [].
    prefs.save(conn, {"access.allowed_identities": [USER]}, allowed=["access.allowed_identities"])
    assert prefs.get(conn, "access.mode") is None
    assert access.migrate(conn, [PUBLIC_URL], []) == "proxy"
    assert prefs.get(conn, "access.allowed_identities") == [USER]
    assert access.startup_problems(access.load(conn, [PUBLIC_URL], []), "127.0.0.1", False) == []


def test_an_install_without_public_urls_is_this_computer_only(conn):
    assert access.migrate(conn, [], []) == "local"
    assert access.load(conn).mode == "local"


def test_identities_are_masked_everywhere_the_page_sees_them(conn):
    access.migrate(conn, [PUBLIC_URL], [USER])
    s = access.summary(conn, access.load(conn), "127.0.0.1", 8765)
    assert s["identities"] == [{"id": access.identity_id(USER), "masked": "ow•••@example.com"}]
    assert USER not in json.dumps(s)


# --- refusing to start ------------------------------------------------------------------------------------------------

@pytest.fixture
def no_run(monkeypatch):
    """The server is never actually started: make_server records the call instead."""
    started = []

    class Fake:
        server_address = ("127.0.0.1", 8799)

        def serve_forever(self):
            started.append(True)

        def server_close(self):
            pass

    monkeypatch.setattr(web, "make_server", lambda app, host, port: (setattr(app, "port", 8799), Fake())[1])
    return started


def test_serve_refuses_a_proxy_mode_with_no_identity_and_logs_it(data_dir, cli_client, no_run, capsys, caplog):
    caplog.set_level(logging.ERROR)
    assert cli.main(["serve", "--port", "8799", "--public-url", PUBLIC_URL]) == 2
    err = capsys.readouterr().err
    assert "no allowed identity" in err and "--allow-user" in err and not no_run
    assert "refused to start" in caplog.text
    # the next start with the login works (and saves it)
    assert cli.main(["serve", "--port", "8799", "--public-url", PUBLIC_URL, "--allow-user", USER]) == 0
    out = capsys.readouterr().out
    assert no_run == [True] and "ow•••@example.com" in out and USER not in out


def test_serve_refuses_public_urls_in_this_computer_only(data_dir, cli_client, no_run, capsys):
    conn = db.connect()
    access.migrate(conn, [], [])                       # a mode was chosen: this computer only
    conn.close()
    assert cli.main(["serve", "--port", "8799", "--public-url", PUBLIC_URL, "--allow-user", USER]) == 2
    assert "This computer only" in capsys.readouterr().err and not no_run


@pytest.mark.parametrize("mode", ["local", "proxy"])
def test_serve_refuses_a_non_loopback_bind_without_the_built_in_login(data_dir, cli_client, no_run, capsys, mode):
    conn = db.connect()
    prefs.save(conn, {"access.mode": mode, "access.allowed_identities": [USER]}, allowed=list(prefs.SPEC))
    conn.close()
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8799"]) == 2
    err = capsys.readouterr().err
    assert "refusing to listen on 0.0.0.0" in err and not no_run
    assert ("tailscale serve" in err) if mode == "local" else ("trusts the Tailscale-User-Login header" in err)


def test_serve_with_the_built_in_login_set_up_may_bind_any_address(data_dir, cli_client, no_run, capsys, monkeypatch):
    conn = db.connect()
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8799"]) == 2          # not set up yet: refused
    assert "isn't finished" in capsys.readouterr().err
    conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                 "VALUES ('owner', 'x', 'y', 'z', 'z')")
    conn.commit()
    conn.close()
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8799"]) == 0 and no_run == [True]
    # and make_server itself agrees
    app = App(config.db_path(), lambda: None, "0.0.0.0", 0)
    assert app.may_bind("0.0.0.0") and App(config.db_path(), lambda: None).may_bind("127.0.0.1")


def test_serve_starts_without_plaid_keys_for_first_run_setup(tmp_path, monkeypatch, no_run, capsys):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["serve", "--port", "8799"]) == 0 and no_run == [True]
    assert "no Plaid keys yet" in capsys.readouterr().out


# --- this computer only --------------------------------------------------------------------------------------------

def test_this_computer_only_refuses_proxied_and_foreign_requests_and_says_why_in_the_log(conn, caplog, monkeypatch):
    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(access, "_last_refusal", [0.0])
    access.migrate(conn, [], [])
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    assert call(app, "GET", "/api/status", LOCAL)[0] == 200
    assert call(app, "GET", "/api/status", {**LOCAL, "X-Forwarded-For": "100.64.0.7"}) == (403, {"error": "forbidden host"})
    assert call(app, "GET", "/api/status", TAILNET) == (403, {"error": "forbidden host"})
    assert call(app, "GET", "/api/status", {"Host": "evil.example"}) == (403, {"error": "forbidden host"})
    assert "This computer only" in caplog.text


# --- behind your proxy login -----------------------------------------------------------------------------------------

def test_the_trusted_header_is_a_setting(conn):
    access.migrate(conn, [PUBLIC_URL], [USER])
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    assert call(app, "GET", "/api/status", TAILNET)[0] == 200
    access.set_proxy(conn, {"proxy_header": "Remote-User"})
    assert call(app, "GET", "/api/status", TAILNET)[0] == 403             # Tailscale's header no longer counts
    remote = {"Host": TS_HOST, "Remote-User": USER, "X-Forwarded-For": "100.64.0.7"}
    assert call(app, "GET", "/api/status", remote)[0] == 200
    s, body = call(app, "GET", "/api/status", {**remote, "Remote-User": ""})
    assert s == 403 and "Remote-User header is missing" in body["error"]
    # and on a localhost Host it's a proxy header like the others: refused
    assert call(app, "GET", "/api/status", {**LOCAL, "Remote-User": USER})[0] == 403
    with pytest.raises(access.AccessError):
        access.set_proxy(conn, {"proxy_header": "not a header!"})


def test_the_last_proxied_request_is_shown_masked(conn, monkeypatch):
    monkeypatch.setattr(access, "_last_proxy", {})
    access.migrate(conn, [PUBLIC_URL], [USER])
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    call(app, "GET", "/api/status", TAILNET)
    last = access.summary(conn, access.load(conn), "127.0.0.1", 8765)["last_proxy"]
    assert last["via"] == "Tailscale" and last["masked"] == "ow•••@example.com" and last["at"]


def test_choosing_the_proxy_mode_needs_an_identity_and_the_last_one_stays(conn):
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    h = {**LOCAL, "Content-Type": "application/json"}
    s, e = call(app, "POST", "/api/access", h, {"mode": "proxy"})
    assert s == 409 and e["field"] == "identity"
    assert call(app, "POST", "/api/access/identities", h, {"add": "Owner@Example.com"})[0] == 200
    s, a = call(app, "POST", "/api/access", h, {"mode": "proxy"})
    assert s == 200 and a["mode"] == "proxy" and a["identities"][0]["masked"] == "ow•••@example.com"
    s, e = call(app, "POST", "/api/access/identities", h, {"remove": a["identities"][0]["id"]})
    assert s == 409 and "last allowed identity" in e["error"]
    s, e = call(app, "POST", "/api/access", h, {"mode": "builtin"})
    assert s == 409 and "Set up the built-in login first" in e["error"]
    assert call(app, "POST", "/api/access", h, {"mode": "local"})[0] == 200


def test_finance_access_cli(data_dir, capsys):
    assert cli.main(["access", "allow", USER]) == 0
    assert cli.main(["access", "mode", "proxy"]) == 0
    out = capsys.readouterr().out
    assert "Behind your proxy login" in out and "ow•••@example.com" in out and USER not in out
    assert cli.main(["access", "mode", "builtin"]) == 2
    assert "Set up the built-in login first" in capsys.readouterr().err
    conn = db.connect()
    conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                 "VALUES ('owner', 'x', 'y', 'z', 'z')")
    conn.commit()
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    conn.close()
    assert cli.main(["access", "reset-login"]) == 0
    out = capsys.readouterr().out
    assert "removed" in out and "This computer only" in out
    conn = db.connect()
    assert not auth.configured(conn) and prefs.get(conn, "access.mode") == "local"


# --- MCP tokens ------------------------------------------------------------------------------------------------------

@pytest.fixture
def token(conn):
    return auth.mcp_token_create(conn, "Claude Desktop")


def test_a_token_is_shown_once_and_stored_hashed(conn, token):
    assert token["token"].startswith("hfm_") and len(token["token"]) >= 4 + 43
    row = conn.execute("SELECT * FROM mcp_tokens").fetchone()
    assert row["token_hash"] == auth.sha256(token["token"]) and token["token"] not in json.dumps(dict(row))
    listed = auth.mcp_tokens(conn)
    assert listed == [{"id": token["id"], "name": "Claude Desktop", "masked": f"hfm_••••{token['token'][-4:]}",
                       "created_at": row["created_at"], "last_used_at": None}]


def test_a_token_opens_mcp_through_the_proxy_without_a_browser_login(conn, token):
    access.migrate(conn, [PUBLIC_URL], [USER])
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    agent = {"Host": TS_HOST, "X-Forwarded-For": "100.64.0.7", **MCP_HEADERS}
    bearer = {**agent, "Authorization": f"Bearer {token['token']}"}
    assert call(app, "POST", "/mcp", bearer, PING)[0] not in (401, 403)             # valid: through to the MCP server
    assert conn.execute("SELECT last_used_at FROM mcp_tokens").fetchone()[0]
    assert call(app, "POST", "/mcp", agent, PING)[0] == 403                          # missing
    assert call(app, "POST", "/mcp", {**agent, "Authorization": "Bearer hfm_wrong"}, PING)[0] == 403   # wrong
    assert call(app, "POST", "/mcp", {**agent, "Authorization": f"Basic {token['token']}"}, PING)[0] == 403
    # never on /api/*: the token is for agents on /mcp only
    assert call(app, "GET", "/api/overview", bearer)[0] == 403
    auth.mcp_token_revoke(conn, token["id"])
    assert call(app, "POST", "/mcp", bearer, PING)[0] == 403                         # revoked


def test_tokens_with_the_built_in_login(conn, token):
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    bearer = {**LOCAL, **MCP_HEADERS, "Authorization": f"Bearer {token['token']}"}
    assert call(app, "POST", "/mcp", bearer, PING)[0] not in (401, 403)
    assert call(app, "POST", "/mcp", {**LOCAL, **MCP_HEADERS}, PING)[0] == 401
    assert call(app, "POST", "/mcp", {**LOCAL, **MCP_HEADERS, "Authorization": "Bearer hfm_x"}, PING)[0] == 401
    assert call(app, "GET", "/api/overview", bearer)[0] == 401                       # not a session
    auth.mcp_token_revoke(conn, token["id"])
    assert call(app, "POST", "/mcp", bearer, PING)[0] == 401


def test_this_computer_only_keeps_mcp_open_on_localhost(conn):
    access.migrate(conn, [], [])
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    assert call(app, "POST", "/mcp", {**LOCAL, **MCP_HEADERS}, PING)[0] not in (401, 403)


def test_token_routes(conn):
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    h = {**LOCAL, "Content-Type": "application/json"}
    s, t = call(app, "POST", "/api/mcp/tokens", h, {"name": "Cursor"})
    assert s == 200 and t["token"].startswith("hfm_")
    s, lst = call(app, "GET", "/api/mcp/tokens", LOCAL)
    assert [x["name"] for x in lst["tokens"]] == ["Cursor"] and t["token"] not in json.dumps(lst)
    assert call(app, "POST", "/api/mcp/tokens", h, {"name": "  "})[0] == 400
    assert call(app, "POST", "/api/mcp/tokens/revoke", h, {"id": t["id"]})[1]["tokens"] == []
    assert call(app, "POST", "/api/mcp/tokens/revoke", h, {"id": t["id"]})[0] == 404


def test_the_mcp_switch_turns_mcp_off(conn, data_dir, capsys):
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    prefs.save(conn, {"mcp.enabled": False}, allowed=["mcp.enabled"])
    s, e = call(app, "POST", "/mcp", {**LOCAL, **MCP_HEADERS}, PING)
    assert s == 404 and "switched off" in e["error"] and app._mcp_bridge is None
    assert cli.main(["mcp"]) == 2 and "switched off" in capsys.readouterr().err
