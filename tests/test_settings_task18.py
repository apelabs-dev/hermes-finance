"""TASK-18: Settings' server side: the typed settings (prefs.py) and who reads them, the secrets file, /api/settings and
`finance settings --json`, the valuation switches (a new install starts with Carfax and Redfin off, an upgraded one
keeps them on, the env file still wins), the AI switches, export and backup, first-run setup and demo mode."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import stat
from datetime import date
from pathlib import Path

import pytest

from conftest import FAKE_CLIENT_ID, FAKE_SECRET, TOKEN_A, add_item
from hermes_finance import admin, ai, api, auth, cli, config, db, prefs, redact, secretstore, web
from hermes_finance.plaid import PlaidClient
from hermes_finance.web import App

import demo_data

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json"}
TODAY = date(2026, 9, 25)


@pytest.fixture
def app(conn):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def call(app, method, path, body=None, headers=None):
    h = headers or (JSONH if method == "POST" else LOCAL)
    status, ctype, out, extra = app.respond(method, path, h, json.dumps(body).encode() if body is not None else b"")
    if hasattr(out, "open"):
        with out.open() as f:
            return status, ctype, f.read(), dict(extra)
    return status, ctype, json.loads(out) if ctype.startswith("application/json") else out.decode(), dict(extra)


# --- the typed settings ---------------------------------------------------------------------------------------------

def test_every_setting_has_a_valid_default_and_bad_rows_read_as_it(conn):
    for key, spec in prefs.SPEC.items():
        if spec.default is not None:
            assert spec.check(spec.default) == spec.default, key
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('sync.daily_time', '\"25:99\"', 'x'), "
                 "('ai.min_confidence', 'not json', 'x')")
    assert prefs.get(conn, "sync.daily_time") == "06:30" and prefs.get(conn, "ai.min_confidence") == 0.7


@pytest.mark.parametrize("values,field", [
    ({"sync.daily_time": "7:5"}, "sync.daily_time"), ({"sync.weekly_day": "funday"}, "sync.weekly_day"),
    ({"alerts.cap_80": "yes"}, "alerts.cap_80"), ({"ai.min_confidence": 0.72}, "ai.min_confidence"),
    ({"ai.min_confidence": 0.99}, "ai.min_confidence"), ({"ai.base_url": "ftp://x"}, "ai.base_url"),
    ({"region.currency": "XXX"}, "region.currency"), ({"region.budget_month_start": 15}, "region.budget_month_start"),
    ({"appearance.theme": "neon"}, "appearance.theme"), ({"nope": 1}, "nope"),
    ({"access.mode": "local"}, "access.mode"),    # not a page key: it has its own route
])
def test_bad_values_are_refused_whole(app, conn, values, field):
    s, _, e, _ = call(app, "POST", "/api/prefs", {**values, "sync.weekly_time": "19:00"})
    assert s == 400 and e["field"] == field
    assert prefs.get(conn, "sync.weekly_time") == "18:00"                  # nothing written


def test_saved_settings_reach_api_settings_and_the_cli(app, conn, capsys):
    s, _, r, _ = call(app, "POST", "/api/prefs", {"sync.daily_time": "07:15", "sync.weekly_day": "mon", "alerts.over_cap": False,
                                                  "alerts.destination": "  Family  chat ", "region.week_start": "mon"})
    assert s == 200 and r["saved"]["alerts.destination"] == "Family chat"
    got = call(app, "GET", "/api/settings")[2]["prefs"]
    assert got["sync"] == {"daily_time": "07:15", "weekly_day": "mon", "weekly_time": "18:00"}
    assert got["alerts"] == {"cap_80": True, "over_cap": False, "broken_connection": True, "destination": "Family chat"}
    assert got["region"]["week_start"] == "mon" and "access" not in got
    assert cli.main(["settings", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sync"]["daily_time"] == "07:15" and out["alerts"]["over_cap"] is False
    assert out["access"] == {"mode": "local", "proxy_header": "Tailscale-User-Login", "identities": 0}
    assert out["mcp"] == {"enabled": True, "allow_writes": False}


def test_finance_settings_never_writes(data_dir, capsys):
    assert cli.main(["settings", "--json"]) == 1 and "database not found" in capsys.readouterr().err
    assert not data_dir.exists()


def test_allow_writes_is_the_mcp_switch_of_task_17(app, conn):
    from hermes_finance import mcp_state
    assert call(app, "POST", "/api/prefs", {"mcp.allow_writes": True})[0] == 200
    assert mcp_state.allow_writes(conn) is True
    assert call(app, "GET", "/api/settings")[2]["mcp"]["allow_writes"] is True


# --- who reads them ---------------------------------------------------------------------------------------------

def test_ai_off_queues_nothing(conn):
    demo_data.build(conn, TODAY)
    prefs.save(conn, {"ai.mode": "off"})
    q = ai.queue(conn)
    assert q["groups"] == [] and q["run_id"] is None and q["ai_off"] is True
    assert conn.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0] == 0
    prefs.save(conn, {"ai.mode": "hermes"})
    assert ai.queue(conn)["run_id"]


def test_ai_apply_uses_the_confidence_setting(conn):
    demo_data.build(conn, TODAY)
    q = ai.queue(conn, 1)
    g = q["groups"][0]
    decision = {"run_id": q["run_id"], "decisions": [{"group_id": g["group_id"], "action": "set", "category": "Travel",
                                                      "confidence": 0.8, "note": "x"}]}
    prefs.save(conn, {"ai.min_confidence": 0.85})
    out = ai.apply_decisions(conn, decision, dry_run=True)
    assert out["by_action"]["flag"] == 1 and out["downgraded"]            # 0.8 < 0.85: only a flag
    prefs.save(conn, {"ai.min_confidence": 0.75})
    out = ai.apply_decisions(conn, decision, dry_run=True)
    assert out["by_action"]["set"] == 1 and not out["downgraded"]


def test_the_valuation_switches_env_file_wins(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FREE_SOURCES_DEFAULT", {"carfax": False, "redfin": False, "photos": True})
    assert config.free_sources(conn=conn) == {"carfax": False, "redfin": False, "photos": True}
    prefs.save(conn, {"valuations.carfax": True, "valuations.photos": False})
    assert config.free_sources(conn=conn) == {"carfax": True, "redfin": False, "photos": False}
    env = Path(os.environ["HERMES_ENV_FILE"])
    env.write_text(env.read_text() + "CARFAX_ENABLED=0\n")
    assert config.free_sources(conn=conn)["carfax"] is False
    assert config.free_source_overrides() == {"carfax": "CARFAX_ENABLED"}


def test_an_upgraded_install_keeps_carfax_and_redfin_on_a_new_one_starts_off(data_dir, monkeypatch):
    monkeypatch.setattr(config, "FREE_SOURCES_DEFAULT", {"carfax": False, "redfin": False, "photos": True})
    fresh = db.connect(data_dir / "new.db")
    assert config.free_sources(conn=fresh) == {"carfax": False, "redfin": False, "photos": True}
    old = data_dir / "old.db"
    raw = sqlite3.connect(old)
    raw.executescript((Path(__file__).parent / "fixtures" / "schema_v9.sql").read_text())
    raw.execute("PRAGMA user_version = 10")
    raw.commit()
    raw.close()
    up = db.connect(old)
    assert config.free_sources(conn=up) == {"carfax": True, "redfin": True, "photos": True}
    up.close()
    assert config.free_sources(conn=db.connect(old))["carfax"] is True        # once, and it stays


def test_settings_page_says_what_each_source_needs(app, conn):
    page = call(app, "GET", "/api/settings/page")[2]
    carfax = next(v for v in page["valuations"] if v["key"] == "carfax")
    assert carfax["chrome"] == {"ready": False, "why": "Chrome not running (tests have no browser)"}
    assert [v["key"] for v in page["valuations"]] == ["carfax", "redfin", "photos"]


# --- the secrets file ------------------------------------------------------------------------------------------------

def test_replace_keys_writes_the_secrets_file_0600_and_never_shows_them(app, conn):
    path = Path(os.environ["FINANCE_SECRETS_FILE"])
    s, _, r, _ = call(app, "POST", "/api/secrets/plaid", {"client_id": "clientnew0001", "secret": "secretnew000000000000001", "env": "sandbox"})
    assert s == 200 and r == {"plaid": {"set": True, "env": "sandbox", "where": secretstore.shown_path(path)}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    text = path.read_text()
    assert "PLAID_SECRET=secretnew000000000000001" in text and "PLAID_ENV=sandbox" in text
    # the secrets file wins over the env file for Plaid, and it's never in an answer
    assert config.load_settings().secret == "secretnew000000000000001" and config.load_settings().env == "sandbox"
    page = json.dumps(call(app, "GET", "/api/settings/page")[2])
    assert "secretnew000000000000001" not in page and "clientnew0001" not in page
    assert redact.scrub("x secretnew000000000000001 y") == f"x {redact.MASK} y"
    # replacing one key keeps the rest of the file
    secretstore.write({"AI_API_KEY": "sk-test-000000000001"})
    assert "PLAID_SECRET=secretnew000000000000001" in path.read_text() and secretstore.status()["ai_key"]["set"]
    secretstore.write({"AI_API_KEY": None})
    assert "AI_API_KEY" not in path.read_text()


@pytest.mark.parametrize("body,field", [({"client_id": "abcdefgh", "secret": "has space inside"}, "PLAID_SECRET"),
                                        ({"client_id": "abcdefgh", "secret": "x\ny"}, "PLAID_SECRET"),
                                        ({"client_id": "abcdefgh"}, "secret"),
                                        ({"client_id": "abcdefgh", "secret": "abcdefgh", "env": "development"}, "env")])
def test_bad_keys_are_refused(app, body, field):
    s, _, e, _ = call(app, "POST", "/api/secrets/plaid", body)
    assert s == 400 and e["field"] == field
    assert not Path(os.environ["FINANCE_SECRETS_FILE"]).exists()


def test_an_env_file_install_works_as_before(conn):
    # the data_dir fixture's HERMES_ENV_FILE has the keys and there's no secrets file: nothing changes
    assert not Path(os.environ["FINANCE_SECRETS_FILE"]).exists()
    s = config.load_settings()
    assert (s.client_id, s.secret, s.env) == (FAKE_CLIENT_ID, FAKE_SECRET, "production")
    assert secretstore.status()["plaid"]["where"] == secretstore.shown_path(config.env_file_path())


def test_new_keys_reach_the_running_app(app, conn, monkeypatch):
    built = []
    app.client_factory = lambda: built.append(1) or PlaidClient(config.load_settings(), transport=lambda *a: (200, b"{}"))
    assert app.client.settings.env == "production"
    call(app, "POST", "/api/secrets/plaid", {"client_id": "clientnew0001", "secret": "secretnew000000000000001", "env": "sandbox"})
    assert app.client.settings.env == "sandbox" and built == [1, 1]


# --- demo mode -----------------------------------------------------------------------------------------------------

def test_demo_mode_can_never_make_a_production_item(app, conn, fake, client):
    prefs.save(conn, {"plaid.demo": True}, allowed=["plaid.demo"])
    app.client_factory = lambda: client                                   # a Production client
    meta = {"institution": {"institution_id": "ins_9", "name": "Bank"}}
    for path, body in (("/api/link_token", {}), ("/api/exchange", {"public_token": "public-production-x", "metadata": meta})):
        s, _, e, _ = call(app, "POST", path, body)
        assert s == 409 and "Demo mode only uses Plaid Sandbox" in e["error"]
    assert fake.calls == [] and conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM link_attempts").fetchone()[0] == 0


def test_setup_plaid_demo_writes_sandbox_keys_and_real_banks_production(app, conn):
    s, _, r, _ = call(app, "POST", "/api/setup/plaid", {"client_id": "clientnew0001", "secret": "secretnew000000000000001", "demo": True})
    assert s == 200 and r["demo"] is True and config.load_settings().env == "sandbox"
    assert PlaidClient(config.load_settings()).base_url == "https://sandbox.plaid.com"
    s, _, r, _ = call(app, "POST", "/api/setup/plaid", {"demo": False})
    assert config.load_settings().env == "production" and prefs.get(conn, "plaid.demo") is False


def test_first_run_setup_is_due_only_with_no_keys_and_nothing_added(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path / "d"))
    conn = db.connect()
    assert admin.needs_setup(conn)                              # no keys (the conftest env file doesn't exist), no data
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    assert " data-setup" in app.page()
    prefs.save(conn, {"setup.done": True}, allowed=["setup.done"])
    assert not admin.needs_setup(conn) and " data-setup" not in app.page()
    prefs.save(conn, {"setup.done": False}, allowed=["setup.done"])
    add_item(conn, token=TOKEN_A)
    assert not admin.needs_setup(conn)


def test_setup_suggestions_from_the_last_60_days(app, conn):
    demo_data.build(conn, TODAY)
    s, _, r, _ = call(app, "GET", "/api/setup/suggestions")
    assert s == 200 and r["days"] == 60 and r["categories"]
    since = "2026-07-27"
    for c in r["categories"]:
        spent = conn.execute("SELECT ROUND(COALESCE(SUM(spend), 0), 2) FROM v_transactions WHERE category = ? AND date > ? AND date <= ?",
                             (c["category"], since, TODAY.isoformat())).fetchone()[0]
        assert c["spent"] == spent and c["suggested"] == api.insights.round_cap(spent / 2)
        assert c["category"] not in api.insights.NO_SUGGEST


# --- export and backup ------------------------------------------------------------------------------------------------

def test_export_csv_has_every_transaction_with_category_and_notes(app, conn):
    demo_data.build(conn, TODAY)
    conn.execute("UPDATE transactions SET merchant_name = '=HYPERLINK(\"x\")' WHERE transaction_id = "
                 "(SELECT transaction_id FROM transactions LIMIT 1)")
    conn.commit()
    s, ctype, raw, extra = call(app, "GET", "/api/export.csv")
    assert s == 200 and ctype.startswith("text/csv") and 'attachment; filename="hermes-finance-transactions-2026-09-25.csv"' == extra["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(raw.decode())))
    assert len(rows) == conn.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0]
    assert {"category", "category_source", "category_note", "ai_note"} <= set(rows[0])
    assert any(r["merchant_name"] == "'=HYPERLINK(\"x\")" for r in rows)          # a formula stays text
    assert all(not r["amount"].startswith("'") for r in rows)


def test_backup_is_a_consistent_copy_without_any_credential(app, conn):
    demo_data.build(conn, TODAY)
    add_item(conn, "item_z", "ins_z", token=TOKEN_A)
    conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                 "VALUES ('owner', '$argon2id$secret-hash', 'TOTPSECRETKEY', 'z', 'z')")
    auth.create_session(conn, 1)
    auth.mcp_token_create(conn, "Cursor")
    conn.commit()
    s, ctype, raw, extra = call(app, "GET", "/api/backup")
    assert s == 200 and ctype == "application/vnd.sqlite3" and raw[:16] == b"SQLite format 3\x00"
    assert "hermes-finance-backup-2026-09-25.db" in extra["Content-Disposition"]
    for secret in (TOKEN_A.encode(), b"TOTPSECRETKEY", b"secret-hash"):
        assert secret not in raw                                                # not even in free pages
    path = Path(os.environ["FINANCE_DATA_DIR"]) / "restored.db"
    path.write_bytes(raw)
    r = sqlite3.connect(path)
    assert r.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert {x[0] for x in r.execute("SELECT access_token FROM items")} == {"removed-from-backup"}
    for table in ("auth_users", "auth_sessions", "auth_recovery_codes", "mcp_tokens"):
        assert r.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert not list(Path(os.environ["FINANCE_DATA_DIR"]).parent.glob("**/secrets.env"))   # never the secrets file


def test_download_streams_the_file_and_removes_it(conn, tmp_path):
    import threading
    import http.client
    app = App(config.db_path(), lambda: None, "127.0.0.1", 0)
    srv = web.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", "/api/backup", headers={"Host": f"127.0.0.1:{srv.server_address[1]}"})
        r = c.getresponse()
        body = r.read()
        assert r.status == 200 and int(r.getheader("Content-Length")) == len(body) and body[:6] == b"SQLite"
        assert r.getheader("Cache-Control") == "no-store"
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_shell_carries_theme_currency_and_locale(app, conn):
    prefs.save(conn, {"appearance.theme": "light", "region.currency": "EUR", "region.locale": "de-DE"})
    html = call(app, "GET", "/")[2]
    assert '<html lang="en" data-theme-pref="light" data-currency="EUR" data-locale="de-DE">' in html
    assert html.index('<script src="/static/theme.js"></script>') < html.index('<link rel="stylesheet" href="/static/app.css">')
    for name in ("login.js", "settings.js", "setup.js", "import.js"):
        assert html.index(f"/static/{name}") < html.index("/static/app.js")
