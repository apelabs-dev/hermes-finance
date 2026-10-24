"""TASK-19 (Batch C): the Alpaca live account, read-only through its API.

The single call site allows GET /v2/account, /v2/positions and /v2/account/portfolio/history on the live host and
nothing else (refused before a request is made); the module never names anything that could trade or move money; a
paper key can never count; the daily snapshot counts in net worth exactly once and only while the account is ACTIVE;
with no key file everything is exactly as before. The dashboard's Connect form (POST / DELETE /api/alpaca/key) checks a
key with one call, writes the key file 0600 only after that, never sends the secret back or logs it, and only answers
on this computer. Every HTTP answer is a fake (the real transport is blocked by conftest)."""

from __future__ import annotations

import json
import logging
import re
import stat
from datetime import date
from pathlib import Path

import pytest

import factory as f
from hermes_finance import alpaca, api, cli, config, db, features, insights, logos, portfolio, reads, redact
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
MODULE = Path(alpaca.__file__)
KEY_ID, SECRET = "AKTESTKEY0000000001", "sEcReT0000000000000000000000000000000000ab"
ACCOUNT = {"status": "ACTIVE", "cash": "125.34", "equity": "1230.00", "currency": "USD", "account_number": "PA0012344321"}
POSITIONS = [{"symbol": "AAPL", "qty": "3", "current_price": "227.52", "market_value": "682.56", "cost_basis": "601.20",
              "asset_class": "us_equity"},
             {"symbol": "VTI", "qty": "1.5", "current_price": "281.40", "market_value": "422.10", "cost_basis": "400.00",
              "asset_class": "us_equity"}]
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json"}


class FakeAlpaca:
    """alpaca.transport stand-in: answers by path, records every call (host, target, headers)."""

    def __init__(self, account=ACCOUNT, positions=POSITIONS, status=200):
        self.calls: list[tuple[str, str, dict]] = []
        self.answers = {"/v2/account": (status, account), "/v2/positions": (status, positions),
                        "/v2/account/portfolio/history": (status, {"equity": [1200.0, 1230.0], "timestamp": [1, 2]})}

    def __call__(self, host, target, headers):
        self.calls.append((host, target, dict(headers)))
        st, body = self.answers[target.split("?")[0]]
        return st, json.dumps(body).encode()


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "hermes-finance" / "alpaca-live.env"
    monkeypatch.setenv("HERMES_ALPACA_KEY_FILE", str(path))
    return path


@pytest.fixture
def live(monkeypatch, keyfile):
    """Alpaca calls switched on, answered by a fake."""
    fake = FakeAlpaca()
    monkeypatch.delenv("HERMES_ALPACA", raising=False)
    monkeypatch.setattr(alpaca, "transport", fake)
    # its site icon: no site answers here (a 404), so the page draws initials
    monkeypatch.setattr(logos, "http_get", lambda url, *a, **k: logos.Response(404, "text/html", b"", url))
    return fake


def write_keys(path: Path, key_id=KEY_ID, secret=SECRET, env="live", extra="") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"APCA_API_KEY_ID={key_id}\nAPCA_API_SECRET_KEY={secret}\nAPCA_ENV={env}\n{extra}")


def base_db(conn) -> None:
    f.bank(conn, "item_a", "Chase")
    f.acct(conn, "acc_chk", current=1000.0)
    f.acct(conn, "acc_cc", name="Card", type_="credit", subtype="credit card", current=200.0, limit=2000.0)


# --- (a) the allow-list, at the one call site ------------------------------------------------------------------------

@pytest.mark.parametrize("method, path", [
    ("POST", "/v2/account"), ("PATCH", "/v2/account"), ("DELETE", "/v2/positions"), ("PUT", "/v2/account"),
    ("get", "/v2/account"), ("GET", "/v2/orders"), ("GET", "/v2/positions/AAPL"), ("DELETE", "/v2/positions/AAPL"),
    ("GET", "/v2/account/configurations"), ("GET", "/v2/wallets"), ("GET", "/v1/transfers"), ("GET", "/v2/account/"),
    ("GET", "/v2/account?x=1"), ("GET", "https://api.alpaca.markets/v2/account"), ("GET", "")])
def test_any_other_method_or_path_is_refused_before_a_request(live, method, path):
    with pytest.raises(alpaca.Refused, match="not an allowed Alpaca call"):
        alpaca._get(method, path, alpaca.Keys(KEY_ID, SECRET))
    assert live.calls == []


def test_the_allowed_calls_go_to_the_live_host_with_the_keys_in_headers_only(live):
    keys = alpaca.Keys(KEY_ID, SECRET)
    alpaca._get("GET", "/v2/account", keys)
    alpaca._get("GET", "/v2/positions", keys)
    alpaca._get("GET", "/v2/account/portfolio/history", keys, {"period": "1M", "timeframe": "1D"})
    assert [(h, t) for h, t, _ in live.calls] == [("api.alpaca.markets", "/v2/account"), ("api.alpaca.markets", "/v2/positions"),
                                                  ("api.alpaca.markets", "/v2/account/portfolio/history?period=1M&timeframe=1D")]
    assert all(hd["APCA-API-KEY-ID"] == KEY_ID and hd["APCA-API-SECRET-KEY"] == SECRET for _, _, hd in live.calls)
    assert alpaca.ALLOWED == {("GET", "/v2/account"), ("GET", "/v2/positions"), ("GET", "/v2/account/portfolio/history")}


def test_the_kill_switch_refuses_even_an_allowed_call(monkeypatch, keyfile):
    fake = FakeAlpaca()
    monkeypatch.setattr(alpaca, "transport", fake)
    monkeypatch.setenv("HERMES_ALPACA", "off")                   # what conftest (and the gates) set
    with pytest.raises(alpaca.Refused, match="switched off"):
        alpaca._get("GET", "/v2/account", alpaca.Keys(KEY_ID, SECRET))
    assert fake.calls == []


def test_the_real_transport_is_one_https_get_that_never_follows_a_redirect(monkeypatch):
    seen = []

    class Conn:
        def __init__(self, host, timeout):
            seen.append(("open", host, timeout))

        def request(self, method, target, headers):
            seen.append(("request", method, target))

        def getresponse(self):
            class R:
                status = 301
                def read(self, n):
                    return b""
            return R()

        def close(self):
            seen.append(("close",))

    monkeypatch.setattr(alpaca, "_connect", lambda host, timeout: Conn(host, timeout))
    assert alpaca._transport("api.alpaca.markets", "/v2/account", {}) == (301, b"")
    assert seen == [("open", "api.alpaca.markets", alpaca.TIMEOUT), ("request", "GET", "/v2/account"), ("close",)]


# --- (b) the static scan ---------------------------------------------------------------------------------------------

def test_the_module_never_names_anything_that_trades_or_moves_money():
    text = MODULE.read_text()
    for word in ("orders", "wallets", "transfers", "whitelists", "positions/"):
        assert word not in text.lower(), word
    for method in ("POST", "PATCH", "DELETE"):
        assert not re.search(rf"\b{method}\b", text, re.IGNORECASE), method
    # one place opens a connection, and only for GET
    assert text.count("HTTPSConnection(") == 1 and text.count('.request("GET"') == 1 and ".request(" not in text.replace('.request("GET"', "")


# --- (c) a paper key never counts ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("env, extra, key_id, why", [
    ("paper", "", KEY_ID, "only a live key"), ("", "", KEY_ID, "only a live key"), ("PAPER", "", KEY_ID, "only a live key"),
    ("live", "APCA_API_BASE_URL=https://paper-api.alpaca.markets\n", KEY_ID, "only the live host"),
    ("live", "APCA_BASE_URL=https://paper-api.alpaca.markets/v2\n", KEY_ID, "only the live host"),
    ("live", "", "PKTESTKEY0000000001", "paper key")])
def test_a_paper_key_is_refused_before_any_request(live, keyfile, conn, env, extra, key_id, why):
    write_keys(keyfile, key_id=key_id, env=env, extra=extra)
    out = alpaca.sync(conn, TODAY)
    assert out["ok"] is False and out["action"] == "refused" and why in out["message"]
    assert live.calls == [] and conn.execute("SELECT COUNT(*) FROM alpaca_snapshots").fetchone()[0] == 0


def test_a_live_key_with_the_live_base_url_is_used(live, keyfile, conn):
    write_keys(keyfile, extra="APCA_API_BASE_URL=https://api.alpaca.markets\n")
    assert alpaca.sync(conn, TODAY)["action"] == "saved"


# --- the daily snapshot -------------------------------------------------------------------------------------------------

def test_sync_saves_equity_as_positions_plus_cash_and_counts_it_once(live, keyfile, conn):
    base_db(conn)
    before = insights.net_worth(conn, history=False)
    write_keys(keyfile)
    out = alpaca.sync(conn, TODAY)
    assert out["ok"] and out["snapshot"] == {"status": "ACTIVE", "cash": 125.34, "equity": 1230.0, "reported_equity": 1230.0,
                                             "currency": "USD", "positions": 2}
    row = conn.execute("SELECT * FROM alpaca_snapshots").fetchone()
    pos = json.loads(row["positions_json"])
    assert row["equity"] == round(row["cash"] + sum(p["value"] for p in pos), 2) == 1230.0 and row["account_mask"] == "4321"
    nw = insights.net_worth(conn, history=False)
    assert nw["assets"] == round(before["assets"] + 1230.0, 2) and nw["net"] == round(before["net"] + 1230.0, 2)
    ids = [a["account_id"] for b in nw["institutions"] for a in b["accounts"]]
    assert ids.count("alpaca:live") == 1
    bank = next(b for b in nw["institutions"] if b["item_id"] == "alpaca")
    assert bank["institution_name"] == "Alpaca" and bank["api"] and bank["positions"] == 2 and bank["counted"]
    assert db.slots_used(conn) == 1                                     # no Plaid slot
    inv = portfolio.investments(conn, TODAY)
    acct = next(a for a in inv["accounts"] if a["id"] == "alpaca:live")
    assert acct["value"] == 1230.0 and acct["residual"] == 0.0 and acct["api"] and acct["holdings"] == 3
    assert sum(h["value"] for h in inv["holdings"] if h["account_id"] == "alpaca:live") == 1230.0
    assert {h["ticker"] for h in inv["holdings"] if h["account_id"] == "alpaca:live"} == {"AAPL", "VTI", "CUR:USD"}
    # the Custom picker lists it like any account, and leaving it out takes exactly its equity off
    assert "alpaca:live" in {a["id"] for a in insights.networth_accounts(conn)}
    custom = insights.net_worth(conn, history=False, excluded=["alpaca:live"])["custom"]
    assert custom["net"] == round(nw["net"] - 1230.0, 2)


def test_a_second_sync_the_same_day_replaces_its_snapshot(live, keyfile, conn):
    write_keys(keyfile)
    alpaca.sync(conn, TODAY)
    live.answers["/v2/account"] = (200, {**ACCOUNT, "cash": "10.00"})
    alpaca.sync(conn, TODAY)
    rows = conn.execute("SELECT date, cash, equity FROM alpaca_snapshots").fetchall()
    assert [tuple(r) for r in rows] == [("2026-09-28", 10.0, 1114.66)]


@pytest.mark.parametrize("status", ["ACCOUNT_CLOSED", "ACCOUNT_UPDATED", "SUBMITTED", "INACTIVE"])
def test_an_account_that_isnt_active_is_an_error_and_counts_nothing(live, keyfile, conn, status):
    base_db(conn)
    before = insights.net_worth(conn, history=False)
    write_keys(keyfile)
    live.answers["/v2/account"] = (200, {**ACCOUNT, "status": status})
    out = alpaca.sync(conn, TODAY)
    assert out["ok"] is False and out["action"] == "saved" and "not counted" in out["message"]
    nw = insights.net_worth(conn, history=False)
    assert (nw["assets"], nw["net"]) == (before["assets"], before["net"])
    assert not any(b["item_id"] == "alpaca" for b in nw["institutions"])
    assert "alpaca:live" not in {a["id"] for a in portfolio.investments(conn, TODAY)["accounts"]}
    (item,) = alpaca.items_payload(conn)                                   # the sidebar shows the error
    assert item["status"] == "error" and item["error_code"] == f"ACCOUNT_{status}" and item["counted"] is False


def test_history_counts_its_active_days_and_drops_when_not_active(live, keyfile, conn):
    base_db(conn)
    f.snapshot(conn, "acc_chk", "2026-09-26", 1000.0)
    f.snapshot(conn, "acc_chk", "2026-09-27", 1000.0)
    f.snapshot(conn, "acc_chk", "2026-09-28", 1000.0)
    write_keys(keyfile)
    alpaca.sync(conn, date(2026, 9, 26))
    live.answers["/v2/account"] = (200, {**ACCOUNT, "status": "ACCOUNT_CLOSED"})
    alpaca.sync(conn, date(2026, 9, 28))
    pts = {p["date"]: p["assets"] for p in insights.net_worth(conn)["history"]}
    assert pts == {"2026-09-26": 2230.0, "2026-09-27": 2230.0, "2026-09-28": 1000.0}


@pytest.mark.parametrize("answer, message", [
    ((401, {"message": "unauthorized"}), "refused the key"), ((403, {}), "refused the key"), ((500, {}), "HTTP 500"),
    ((200, {"status": "ACTIVE", "cash": "NaN"}), "not a number"), ((200, ["x"]), "expected shape")])
def test_a_failed_sync_saves_nothing(live, keyfile, conn, answer, message):
    write_keys(keyfile)
    live.answers["/v2/account"] = answer
    out = alpaca.sync(conn, TODAY)
    assert out["ok"] is False and out["action"] == "failed" and message in out["message"]
    assert conn.execute("SELECT COUNT(*) FROM alpaca_snapshots").fetchone()[0] == 0


def test_network_trouble_is_a_failure_not_a_crash(keyfile, conn, monkeypatch):
    monkeypatch.delenv("HERMES_ALPACA", raising=False)
    monkeypatch.setattr(alpaca, "transport", lambda *a: (_ for _ in ()).throw(OSError("down")))
    write_keys(keyfile)
    assert alpaca.sync(conn, TODAY) == {"ok": False, "action": "failed", "message": "api.alpaca.markets: OSError"}


def test_the_logo_is_fetched_once_through_site_icon(live, keyfile, conn, monkeypatch):
    from pngtools import logo_png
    asked = []

    def fetch(url):
        asked.append(url)
        if url == "https://alpaca.markets":
            return logos.Response(200, "text/html", b'<link rel="apple-touch-icon" href="/touch.png">', url)
        return logos.Response(200, "image/png", logo_png(64), url)

    write_keys(keyfile)
    alpaca.sync(conn, TODAY, fetch_logo=fetch)
    alpaca.sync(conn, TODAY, fetch_logo=fetch)
    assert asked == ["https://alpaca.markets", "https://alpaca.markets/touch.png"]
    row = conn.execute("SELECT name, logo_source, url FROM institutions WHERE institution_id = ?", (alpaca.INSTITUTION_ID,)).fetchone()
    assert tuple(row) == ("Alpaca", "site", "https://alpaca.markets")
    assert logos.institution_logo(conn, alpaca.INSTITUTION_ID)[0] == "image/png"


# --- without a key file: exactly as before ---------------------------------------------------------------------------

def test_with_no_key_file_nothing_is_asked_shown_or_counted(keyfile, conn, monkeypatch):
    fake = FakeAlpaca()
    monkeypatch.delenv("HERMES_ALPACA", raising=False)
    monkeypatch.setattr(alpaca, "transport", fake)
    base_db(conn)
    before = json.dumps(insights.net_worth(conn), sort_keys=True)
    out = alpaca.sync(conn, TODAY)
    assert out == {"ok": True, "action": "none", "message": f"no key file ({keyfile}): nothing to do"} and fake.calls == []
    assert json.dumps(insights.net_worth(conn), sort_keys=True) == before
    assert alpaca.items_payload(conn) == [] and alpaca.account_extra(conn) == {}
    assert api.accounts(conn, {}, TODAY)["api_items"] == []


def test_when_the_key_file_goes_the_next_sync_leaves_every_snapshot_out(live, keyfile, conn):
    base_db(conn)
    before = insights.net_worth(conn)
    write_keys(keyfile)
    alpaca.sync(conn, TODAY)
    assert insights.net_worth(conn, history=False)["net"] == round(before["net"] + 1230.0, 2)
    keyfile.unlink()
    out = alpaca.sync(conn, TODAY)
    assert out["action"] == "removed" and len(live.calls) == 2           # nothing asked without the file
    after = insights.net_worth(conn)
    assert (after["net"], after["history"]) == (before["net"], before["history"])
    assert alpaca.items_payload(conn) == [] and conn.execute("SELECT SUM(counted) FROM alpaca_snapshots").fetchone()[0] == 0


def test_the_cli_command(live, keyfile, data_dir, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    assert cli.main(["alpaca-sync"]) == 0
    assert "no key file" in capsys.readouterr().out
    write_keys(keyfile)
    assert cli.main(["alpaca-sync", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "saved" and out["snapshot"]["equity"] == 1230.0
    live.answers["/v2/account"] = (200, {**ACCOUNT, "status": "ACCOUNT_CLOSED"})
    assert cli.main(["alpaca-sync"]) == 1                                   # non-fatal for the daily run, but not ok
    assert "not counted" in capsys.readouterr().out


def test_the_daily_prerun_runs_it_next_to_revalue():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "finance_daily_prerun.sh").read_text()
    assert "$F alpaca-sync" in script and script.index("$F revalue") < script.index("$F alpaca-sync")


def test_read_commands_show_it_like_any_investment_account(live, keyfile, conn):
    base_db(conn)
    write_keys(keyfile)
    alpaca.sync(conn, TODAY)
    nw = reads.networth(conn, TODAY)
    inv = next(g for g in nw["groups"] if g["key"] == "investments")
    assert [a["id"] for a in inv["accounts"]] == ["alpaca:live"] and inv["total"] == 1230.0
    h = reads.holdings(conn, TODAY)
    assert h["total"] == 1230.0 and {x["ticker"] for x in h["holdings"]} == {"AAPL", "VTI", "CUR:USD"}


# --- the Connect form: POST / DELETE /api/alpaca/key -----------------------------------------------------------------

@pytest.fixture
def web_app(conn, client):
    return App(config.db_path(), lambda: client, "127.0.0.1", 8765, ["https://box.tail0.ts.net:8765"])


def call(app, method, path, headers, body=None):
    status, _, raw = app.handle(method, path, headers, json.dumps(body).encode() if body is not None else b"")
    return status, raw.decode()


def test_connect_checks_with_one_call_then_writes_the_key_file_0600(live, keyfile, web_app, conn, caplog):
    caplog.set_level(logging.DEBUG)
    status, body = call(web_app, "POST", features.ALPACA_KEY, POST, {"key_id": KEY_ID, "secret": SECRET})
    assert status == 200, body
    assert json.loads(body) == {"connected": True, "status": "ACTIVE", "equity": 1230.0, "cash": 125.34, "positions": 2}
    assert [t for _, t, _ in live.calls] == ["/v2/account", "/v2/positions"]   # one check, then the positions once
    assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600 and stat.S_IMODE(keyfile.parent.stat().st_mode) == 0o700
    assert keyfile.read_text() == f"APCA_API_KEY_ID={KEY_ID}\nAPCA_API_SECRET_KEY={SECRET}\nAPCA_ENV=live\n"
    assert [p.name for p in keyfile.parent.iterdir()] == ["alpaca-live.env"]   # no temp file left behind
    assert SECRET not in body and SECRET not in caplog.text
    assert redact.scrub(f"x {SECRET} y") == "x [REDACTED] y"                   # registered, so nothing prints it
    for path in ("/api/alpaca", "/api/accounts", "/api/investments", "/api/overview"):
        assert SECRET not in call(web_app, "GET", path, LOCAL)[1], path
    assert conn.execute("SELECT status, equity FROM alpaca_snapshots").fetchone()[:] == ("ACTIVE", 1230.0)
    assert json.loads(call(web_app, "GET", "/api/alpaca", LOCAL)[1])["key_file"] is True


def test_connect_refuses_a_paper_key_without_a_call(live, keyfile, web_app):
    status, body = call(web_app, "POST", features.ALPACA_KEY, POST, {"key_id": "PKTESTKEY0000000001", "secret": SECRET})
    assert status == 400 and json.loads(body)["error"] == alpaca.PAPER_MESSAGE
    assert live.calls == [] and not keyfile.exists()


def test_connect_refuses_an_account_that_isnt_active(live, keyfile, web_app, conn):
    live.answers["/v2/account"] = (200, {**ACCOUNT, "status": "ACCOUNT_CLOSED"})
    status, body = call(web_app, "POST", features.ALPACA_KEY, POST, {"key_id": KEY_ID, "secret": SECRET})
    assert status == 400 and "isn't active" in json.loads(body)["error"] and "ACCOUNT_CLOSED" in json.loads(body)["error"]
    assert [t for _, t, _ in live.calls] == ["/v2/account"] and not keyfile.exists()
    assert conn.execute("SELECT COUNT(*) FROM alpaca_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("answer, status_code, message", [
    ((401, {"message": "unauthorized"}), 400, alpaca.PAPER_MESSAGE + ". If it's a live key"),  # a paper key on the live host
    ((403, {"message": "forbidden"}), 400, "didn't accept that key"),
    ((500, {}), 502, "Couldn't reach Alpaca")])
def test_connect_refuses_a_bad_key_with_no_file_written(live, keyfile, web_app, answer, status_code, message):
    live.answers["/v2/account"] = answer
    status, body = call(web_app, "POST", features.ALPACA_KEY, POST, {"key_id": KEY_ID, "secret": SECRET})
    assert status == status_code and message in json.loads(body)["error"] and SECRET not in body
    assert not keyfile.exists() and not keyfile.parent.exists()


@pytest.mark.parametrize("payload, message", [
    ({"key_id": "short", "secret": SECRET}, "Key ID"), ({"key_id": KEY_ID, "secret": "x"}, "Secret"),
    ({"key_id": KEY_ID}, "Secret"), ({"key_id": KEY_ID, "secret": SECRET, "base_url": "https://paper-api.alpaca.markets"}, "unknown field"),
    ({"key_id": ["x"], "secret": SECRET}, "Key ID")])
def test_connect_validates_before_any_call(live, keyfile, web_app, payload, message):
    status, body = call(web_app, "POST", features.ALPACA_KEY, POST, payload)
    assert status == 400 and message in json.loads(body)["error"]
    assert live.calls == [] and not keyfile.exists()


def test_remove_deletes_the_file_and_leaves_every_snapshot_out(live, keyfile, web_app, conn):
    base_db(conn)
    before = insights.net_worth(conn)
    assert call(web_app, "POST", features.ALPACA_KEY, POST, {"key_id": KEY_ID, "secret": SECRET})[0] == 200
    assert keyfile.exists() and insights.net_worth(conn, history=False)["net"] == round(before["net"] + 1230.0, 2)
    status, body = call(web_app, "DELETE", features.ALPACA_KEY, POST, {})
    assert status == 200 and json.loads(body) == {"connected": False, "removed_file": True, "snapshots_left_out": 1}
    assert not keyfile.exists()
    after = insights.net_worth(conn)
    assert (after["net"], after["history"]) == (before["net"], before["history"])
    assert json.loads(call(web_app, "GET", "/api/accounts", LOCAL)[1])["api_items"] == []
    assert call(web_app, "DELETE", features.ALPACA_KEY, POST, {})[0] == 200    # again: nothing left, still fine


def test_the_key_routes_go_through_the_access_gate(live, keyfile, conn, client):
    """TASK-20: the key routes sit behind TASK-18's gate like every other write. Behind the proxy login a wrong (or no)
    identity is refused before anything; an allowed one gets through. "This computer only" refuses anything proxied.
    (Before the merge they answered on this computer only: TASK-19 had no login mode to lean on.)"""
    proxied = App(config.db_path(), lambda: client, "127.0.0.1", 8765, ["https://box.tail0.ts.net:8765"],
                  ["owner@example.com"])
    tailnet = {"Host": "box.tail0.ts.net:8765", "Tailscale-User-Login": "owner@example.com", "X-Forwarded-For": "100.64.0.7",
               "Origin": "https://box.tail0.ts.net:8765", "Content-Type": "application/json"}
    for method in ("POST", "DELETE"):
        wrong = {**tailnet, "Tailscale-User-Login": "someone-else@example.com"}
        status, body = call(proxied, method, features.ALPACA_KEY, wrong, {"key_id": KEY_ID, "secret": SECRET})
        assert status == 403 and "not allowed" in json.loads(body)["error"], method
        anon = {k: v for k, v in tailnet.items() if k != "Tailscale-User-Login"}
        assert call(proxied, method, features.ALPACA_KEY, anon, {"key_id": KEY_ID, "secret": SECRET})[0] == 403
    assert live.calls == [] and not keyfile.exists()
    status, body = call(proxied, "POST", features.ALPACA_KEY, tailnet, {"key_id": KEY_ID, "secret": SECRET})
    assert status == 200 and json.loads(body)["connected"] is True and keyfile.exists(), body
    assert call(proxied, "DELETE", features.ALPACA_KEY, tailnet, {})[0] == 200 and not keyfile.exists()
    calls = len(live.calls)
    # "This computer only": a proxied request never gets in, even with a login header
    local_only = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    for method in ("POST", "DELETE"):
        assert call(local_only, method, features.ALPACA_KEY, tailnet, {"key_id": KEY_ID, "secret": SECRET})[0] == 403
    # a forged Origin (CSRF) and a form post are refused like every write
    assert call(local_only, "POST", features.ALPACA_KEY, {**POST, "Origin": "https://evil.example"}, {"key_id": KEY_ID})[0] == 403
    assert call(local_only, "POST", features.ALPACA_KEY, {**LOCAL, "Content-Type": "text/plain"}, {})[0] == 415
    assert call(local_only, "DELETE", features.ALPACA_KEY, {**LOCAL, "Content-Type": "text/plain"}, {})[0] == 415
    assert call(local_only, "DELETE", "/api/goals", POST, {})[0] == 404                # DELETE: that one route only
    assert len(live.calls) == calls and not keyfile.exists()


def test_the_secret_is_never_in_an_export(live, keyfile, web_app, data_dir, capsys, monkeypatch):
    base_db(db.connect())
    assert call(web_app, "POST", features.ALPACA_KEY, POST, {"key_id": KEY_ID, "secret": SECRET})[0] == 200
    assert cli.main(["export", "--since", "2000-01-01"]) == 0
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err and KEY_ID not in out.out
    assert keyfile.parent != config.data_dir() and not str(keyfile).startswith(str(config.data_dir()))
