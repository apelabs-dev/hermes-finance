"""TASK-17: the MCP server (mcp_server.py) beyond the read parity in test_tools_task17.py.

Read-only by default (the write tools aren't even listed); with mcp.allow_writes a write only becomes a pending action
and changes nothing else; each client's clientInfo is saved as mcp.last_used and shown by /api/settings; /mcp on
`finance serve` sits behind exactly the gate of every /api route; `finance mcp` serves stdio in a child process;
schema v10 (pending_actions) upgrades a v9 database."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
from datetime import date
from pathlib import Path

import anyio
import mcp_types as types
import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from agent_tools import (TODAY, build, cli_argv, mcp_error, mcp_session, rows, run_cli, sha, table_hashes)
from hermes_finance import cli, config, db, mcp_state, tooldefs
from hermes_finance.web import App, make_server

V9_SQL = (Path(__file__).parent / "fixtures" / "schema_v9.sql").read_text()
TS_HOST = "box.tail0000.ts.net"
PUBLIC_URL = f"https://{TS_HOST}"
USER = "owner@example.com"
LOCAL = {"Host": "127.0.0.1:8765"}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
TAILNET = {"Host": TS_HOST, "Tailscale-User-Login": USER, "X-Forwarded-For": "100.64.0.7",
           "X-Forwarded-Proto": "https", "X-Forwarded-Host": TS_HOST}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "Claude Desktop", "version": "0.14.1"}}}


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)


@pytest.fixture
def demo(data_dir):
    build("demo", config.db_path())
    return config.db_path()


def allow_writes(path: Path, on: bool = True) -> None:
    conn = db.connect(path)
    mcp_state.set_allow_writes(conn, on)
    conn.close()


def last_used(path: Path) -> dict | None:
    conn = db.connect_readonly(path)
    try:
        return mcp_state.last_used(conn)
    finally:
        conn.close()


def costco(path: Path) -> str:
    return rows(path, "SELECT transaction_id FROM v_transactions WHERE merchant_name = 'Costco' AND category_source <> 'user' "
                      "ORDER BY date LIMIT 1")[0][0]


# --- read-only by default; writes only become pending actions ------------------------------------------------------

def test_write_tools_are_not_listed_or_callable_without_allow_writes(demo):
    tables = table_hashes(demo)
    listed, _ = mcp_session(demo, [])
    assert listed == list(tooldefs.UNGATED)
    for mode in ("legacy", "auto"):
        assert mcp_error(demo, "finance_set_cap", {"category": "Travel", "monthly_cap": 5}, mode=mode) == \
            "MCPError: Unknown tool: finance_set_cap"
    assert mcp_error(demo, "terminal", {}) == "MCPError: Unknown tool: terminal"
    allow_writes(demo, False)                                             # explicitly off: the same
    assert mcp_session(demo, [])[0] == list(tooldefs.UNGATED)
    assert rows(demo, "SELECT COUNT(*) FROM pending_actions") == [(0,)]
    assert {t for t, h in table_hashes(demo).items() if tables[t] != h} == {"settings"}   # last_used + the switch


def test_with_allow_writes_a_write_is_only_recorded_as_pending(demo):
    allow_writes(demo)
    tid = costco(demo)
    before = table_hashes(demo)
    calls = [("finance_set_category", {"transaction_id": tid, "category": "other shopping"}),
             ("finance_add_rule", {"pattern": "costco", "category": "Other Shopping"}),
             ("finance_set_cap", {"category": "eating out", "monthly_cap": 400}),
             ("finance_add_rule", {"pattern": "Costco Wholesale", "category": "Other Shopping", "match": "exact"}),
             ("finance_set_cap", {"category": "Income", "monthly_cap": 1})]
    listed, out = mcp_session(demo, calls, client="Cursor")
    assert listed == list(tooldefs.TOOLS)
    answers = [json.loads(text) for _, text in out[:3]]
    assert [is_error for is_error, _ in out] == [False, False, False, True, True]
    assert "matches no transaction, so no rule was saved" in out[3][1] and "not a spending category" in out[4][1]
    for (name, args), a in zip(calls, answers):
        assert a["status"] == "pending_approval" and a["applied"] is False and a["note"].startswith("Nothing was changed")
        p = a["pending_action"]
        assert (p["tool"], p["client"], p["status"], p["decided_at"]) == (name, "Cursor", "pending", None)
        assert p["arguments"] == args and p["summary"] == tooldefs.describe(name, args, demo)[1]
    assert answers[2]["pending_action"]["summary"] == "Set the monthly budget cap on Eating Out to $400 (now $250)."
    assert rows(demo, "SELECT id, tool, status FROM pending_actions ORDER BY id") == [
        (1, "finance_set_category", "pending"), (2, "finance_add_rule", "pending"), (3, "finance_set_cap", "pending")]
    after = table_hashes(demo)
    assert {t for t in after if after[t] != before[t]} == {"settings", "pending_actions"}   # nothing else changed
    assert rows(demo, "SELECT key FROM settings ORDER BY key") == [("mcp.allow_writes",), ("mcp.last_used",)]
    conn = db.connect(demo)
    assert mcp_state.summary(conn)["pending"] == 3 and [a["id"] for a in mcp_state.pending(conn)] == [1, 2, 3]
    conn.close()


def test_the_client_is_recorded_from_initialize_and_from_discover(demo):
    assert last_used(demo) is None
    mcp_session(demo, [], mode="legacy", client="Claude Desktop", transport="stdio")          # initialize
    first = rows(demo, "SELECT value FROM settings WHERE key = 'mcp.last_used'")[0][0]
    assert {k: v for k, v in json.loads(first).items() if k != "at"} == \
        {"client": "Claude Desktop", "title": None, "version": "1.2.3", "transport": "stdio"}
    mcp_session(demo, [], mode="auto", client="codex-cli", transport="http")                  # 2026: server/discover
    last = json.loads(rows(demo, "SELECT value FROM settings WHERE key = 'mcp.last_used'")[0][0])
    assert (last["client"], last["transport"]) == ("codex-cli", "http")
    assert re.fullmatch(r"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ", last["at"])


def test_api_settings_shows_the_mcp_block_read_only(demo):
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    status, _, body = app.handle("GET", "/api/settings", LOCAL, b"")
    assert status == 200 and json.loads(body)["mcp"] == {"allow_writes": False, "last_used": None, "pending": 0}
    mcp_session(demo, [], client="Claude Desktop", transport="http")
    got = json.loads(app.handle("GET", "/api/settings", LOCAL, b"")[2])["mcp"]
    assert got["last_used"]["client"] == "Claude Desktop" and got["last_used"]["transport"] == "http"
    assert got["last_used"]["at"] and got["allow_writes"] is False
    for payload in ({"mcp.allow_writes": True}, {"mcp": {"allow_writes": True}}):
        status, _, body = app.handle("POST", "/api/settings", {**LOCAL, "Content-Type": "application/json"},
                                     json.dumps(payload).encode())
        assert status == 400 and "unknown setting" in json.loads(body)["error"]
    assert rows(demo, "SELECT COUNT(*) FROM settings WHERE key = 'mcp.allow_writes'") == [(0,)]


# --- /mcp on `finance serve`: the same gate as every other route --------------------------------------------------

@pytest.fixture
def gated(demo):
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765, [PUBLIC_URL], [USER])
    app.today = lambda: TODAY
    yield app
    if app._mcp_bridge is not None:
        app._mcp_bridge.close()


def _post(app, path, headers, payload):
    return app.handle("POST", path, headers, json.dumps(payload).encode())


def test_mcp_answers_through_the_gate(gated):
    status, ctype, body = _post(gated, "/mcp", {**LOCAL, **MCP_HEADERS}, INITIALIZE)
    result = json.loads(body)["result"]
    assert status == 200 and ctype.startswith("application/json")
    assert result["serverInfo"]["name"] == "hermes-finance" and "tools" in result["capabilities"]
    assert "never guess one" in result["instructions"]
    status, _, body = _post(gated, "/mcp", {**TAILNET, **MCP_HEADERS}, INITIALIZE)          # an allowed tailnet user
    assert status == 200 and json.loads(body)["result"]["serverInfo"]["name"] == "hermes-finance"
    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    status, _, body = _post(gated, "/mcp", {**LOCAL, **MCP_HEADERS, "MCP-Protocol-Version": "2025-06-18"}, listing)
    assert status == 200 and [t["name"] for t in json.loads(body)["result"]["tools"]] == list(tooldefs.UNGATED)
    status, _, body = gated.handle("GET", "/mcp", LOCAL, b"")
    assert status == 405 and "POST" in json.loads(body)["error"]                 # no server-sent stream (stateless)


@pytest.mark.parametrize("headers", [
    {k: v for k, v in TAILNET.items() if k != "Tailscale-User-Login"},                       # no login
    {**TAILNET, "Tailscale-User-Login": "someone@else.example"},                             # not an allowed user
    {**LOCAL, "X-Forwarded-For": "100.64.0.7"},                                              # proxied "localhost"
    {**LOCAL, "Tailscale-User-Login": USER},
    {"Host": "evil.example:8765"},                                                           # rebinding
    {**LOCAL, "Origin": "https://evil.example"},                                             # a foreign page
])
def test_mcp_is_refused_exactly_like_the_api(gated, headers):
    for method, api_path in (("POST", "/api/transactions/category"), ("GET", "/api/status")):
        body = json.dumps(INITIALIZE).encode() if method == "POST" else b""
        refused = gated.handle(method, "/mcp", {**headers, **MCP_HEADERS}, body)
        assert refused[0] == 403 and refused == gated.handle(method, api_path, {**headers, **MCP_HEADERS}, body)
    assert gated._mcp_bridge is None                                                 # never reached the MCP server
    assert rows(config.db_path(), "SELECT COUNT(*) FROM settings WHERE key = 'mcp.last_used'") == [(0,)]


def test_mcp_without_the_extra_answers_501(demo, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_finance.mcp_server", None)             # import fails
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    status, _, body = _post(app, "/mcp", {**LOCAL, **MCP_HEADERS}, INITIALIZE)
    assert status == 501 and "uv sync --extra mcp" in json.loads(body)["error"]
    assert app.handle("GET", "/api/settings", LOCAL, b"")[0] == 200


def test_streamable_http_end_to_end_equals_the_cli(demo, capsys):
    """A real `finance serve` socket on loopback and the SDK's own streamable-HTTP client, both protocol eras."""
    app = App(config.db_path(), lambda: None, "127.0.0.1", 0)
    app.today = lambda: TODAY
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = [("finance_spend", {"by": "category"}), ("finance_networth", {"custom": True}), ("finance_bills", {"next": "30d"})]
    want = []
    for name, args in calls:
        code, out, err = run_cli(cli_argv(name, args), capsys)
        assert code == 0, err
        want.append(json.loads(out))

    async def session(mode):
        async with Client(f"http://127.0.0.1:{app.port}/mcp", mode=mode,
                          client_info=types.Implementation(name=f"http-{mode}", version="1")) as c:
            listed = [t.name for t in (await c.list_tools()).tools]
            return listed, [json.loads((await c.call_tool(n, a)).content[0].text) for n, a in calls]

    try:
        for mode in ("legacy", "auto"):
            listed, got = anyio.run(session, mode)
            assert listed == list(tooldefs.UNGATED) and got == want, mode
            last = last_used(demo)
            assert (last["client"], last["transport"]) == (f"http-{mode}", "http")
    finally:
        server.shutdown()
        server.server_close()
        app._mcp_bridge.close()


# --- `finance mcp` ------------------------------------------------------------------------------------------------

def test_finance_mcp_serves_stdio_in_a_child_process(data_dir, capsys, monkeypatch):
    """`finance mcp` spawned the way an MCP client does it (the child has no conftest: only the environment it's given,
    with conftest's kill-switches and no Plaid key), then list tools and call finance_spend against the CLI."""
    today = date.today()                          # the child's clock is the real one
    monkeypatch.setattr(cli, "_today", lambda: today)
    build("demo", config.db_path(), today)
    finance = Path(sys.executable).with_name("finance")
    assert finance.exists()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLAID_") and k not in ("MARKETCHECK_API_KEY", "RENTCAST_API_KEY")}
    assert env["FINANCE_DATA_DIR"] == str(data_dir) and env["HERMES_CHROME_DEVTOOLS"] == "off"
    code, out, err = run_cli(["spend", "--by", "category", "--json"], capsys)
    want = json.loads(out)

    async def session():
        params = StdioServerParameters(command=str(finance), args=["mcp"], env=env, cwd=str(data_dir.parent))
        async with Client(params, mode="legacy", client_info=types.Implementation(name="stdio-smoke", version="1")) as c:
            listed = [t.name for t in (await c.list_tools()).tools]
            r = await c.call_tool("finance_spend", {"by": "category"})
            bad = await c.call_tool("finance_spend", {"month": "2026-13"})
            return listed, r, bad

    listed, r, bad = anyio.run(session)
    assert listed == list(tooldefs.UNGATED)
    assert not r.is_error and json.loads(r.content[0].text) == want and want["total"] > 0
    assert bad.is_error and bad.content[0].text.startswith("--month must be YYYY-MM")
    last = last_used(config.db_path())
    assert (last["client"], last["transport"]) == ("stdio-smoke", "stdio")


def test_finance_mcp_status_and_allow_writes(demo, capsys):
    code, out, _ = run_cli(["mcp", "status"], capsys)
    assert code == 0 and out.splitlines() == ["write tools: off", "last used: never", "0 pending action(s)"]
    code, out, _ = run_cli(["mcp", "allow-writes", "on"], capsys)
    assert code == 0 and "each call is saved as a pending action, nothing is applied" in out
    mcp_session(demo, [("finance_set_cap", {"category": "Travel", "monthly_cap": 99})], client="Cursor")
    code, out, _ = run_cli(["mcp", "status", "--json"], capsys)
    s = json.loads(out)
    assert s["allow_writes"] is True and s["pending"] == 1 and s["last_used"]["client"] == "Cursor"
    assert [(a["tool"], a["summary"]) for a in s["pending_actions"]] == \
        [("finance_set_cap", "Set the monthly budget cap on Travel to $99 (now no cap).")]
    code, out, _ = run_cli(["mcp", "status"], capsys)
    assert out.splitlines()[0] == "write tools: listed (each call is saved as a pending action)"
    assert re.match(r"last used: Cursor 1\.2\.3 over stdio at 20", out.splitlines()[1])
    assert "Travel to $99" in out.splitlines()[2]
    assert run_cli(["mcp", "allow-writes", "off"], capsys)[0] == 0
    assert mcp_session(demo, [])[0] == list(tooldefs.UNGATED)
    assert rows(demo, "SELECT monthly_cap FROM categories WHERE name = 'Travel'") == [(None,)]   # never applied
    code, out, _ = run_cli(["mcp", "--help"], capsys)
    assert code == 0 and all(name in out for name in tooldefs.TOOLS)


def test_finance_mcp_commands_never_create_or_upgrade_a_database(data_dir, capsys):
    for argv in (["mcp", "status"], ["mcp", "allow-writes", "on"]):
        code, out, err = run_cli(argv, capsys)
        assert code == 1 and "database not found" in err
    assert not data_dir.exists()
    data_dir.mkdir()
    raw = sqlite3.connect(config.db_path())
    raw.executescript(V9_SQL)
    raw.close()
    before = sha(config.db_path())
    code, _, err = run_cli(["mcp", "allow-writes", "on"], capsys)
    assert code == 1 and "schema v9" in err and "finance sync" in err
    assert sha(config.db_path()) == before


# --- schema v10 --------------------------------------------------------------------------------------------------

def test_upgrade_from_v9_adds_pending_actions_and_keeps_everything(data_dir):
    data_dir.mkdir()
    path = config.db_path()
    raw = sqlite3.connect(path)
    raw.executescript(V9_SQL)
    ts = "2026-09-27T12:00:00Z"
    raw.execute("INSERT INTO settings (key, value, updated_at) VALUES ('networth_excluded', '[\"manual:1\"]', ?)", (ts,))
    raw.execute("UPDATE categories SET monthly_cap = 321 WHERE name = 'Groceries'")
    raw.commit()
    cols = {t: ", ".join(r[1] for r in raw.execute(f"PRAGMA table_info({t})"))   # later versions add columns (v11)
            for (t,) in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    old = {t: [tuple(r) for r in raw.execute(f"SELECT {c} FROM {t} ORDER BY rowid")] for t, c in cols.items()}
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 9 and "pending_actions" not in old
    raw.close()

    before = sha(path)                                         # the tools refuse it as the CLI does, changing nothing
    assert mcp_session(path, [("finance_spend", {})])[1][0][1].startswith("the database is at schema v9, these commands "
                                                                          f"need v{db.SCHEMA_VERSION}; run `finance sync` once")
    assert sha(path) == before

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    for t, r in old.items():
        now_rows = [tuple(x) for x in conn.execute(f"SELECT {cols[t]} FROM {t} ORDER BY rowid")]
        if t == "settings":  # v11 (TASK-18) saves an upgraded install's Carfax / Redfin switches, on as they were
            assert [x[:2] for x in now_rows if x[0].startswith("valuations.")] == [("valuations.carfax", "true"),
                                                                                  ("valuations.redfin", "true")]
            now_rows = [x for x in now_rows if not x[0].startswith("valuations.")]
        if t == "categories":  # v16 (polish P0) adds the 23 new categories after the originals
            renamed = {"Transport": "Other Transport", "Shopping": "Other Shopping", "Health": "Other Health"}
            r = [(x[0], renamed.get(x[1], x[1]), *x[2:]) for x in r]    # ... and v19 renames three of them, by id
            now_rows = now_rows[:len(r)]
        if t == "plaid_category_map":  # ... and the new codes, and moves the car payment to Car Payment
            car = conn.execute("SELECT id FROM categories WHERE name = 'Car Payment'").fetchone()[0]
            transport = conn.execute("SELECT id FROM categories WHERE name = 'Other Transport'").fetchone()[0]
            now_rows = [(x[0], transport) if x == ("LOAN_PAYMENTS_CAR_PAYMENT", car) else x for x in now_rows[:len(r)]]
        assert now_rows == r, t
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(db.SCHEMA)
    assert [r[1:] for r in conn.execute("PRAGMA table_info(pending_actions)")] == \
        [r[1:] for r in fresh.execute("PRAGMA table_info(pending_actions)")]
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO pending_actions (tool, arguments, summary, status, created_at) "
                         "VALUES ('finance_set_cap', '{}', 'x', 'maybe', 'x')")
    conn.close()
    assert mcp_session(path, [("finance_spend", {})])[1][0][0] is False        # upgraded: the tools answer
