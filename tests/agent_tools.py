"""Helpers for the TASK-17 tests (the Hermes plugin and the MCP server): a stand-in for Hermes's plugin context, the
plugin loaded from hermes_plugin/, one MCP client session over the SDK, `finance <command> --json` in process, and the
two synthetic databases (tests/demo_data.py, tests/gate_dbs.py full). No network, no real data."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.client.client import Client

import demo_data
import gate_dbs
from hermes_finance import classify, cli, config, db, mcp_server, tooldefs

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes_plugin"
TODAY = date(2026, 9, 26)


class FakeCtx:
    """The parts of Hermes's PluginContext the plugin uses (register_tool / register_hook / register_skill /
    get_config), recording what it registers. `settings` is its plugins.entries.<id>.settings."""

    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = settings or {}
        self.tools: dict[str, Any] = {}
        self.schemas: dict[str, dict] = {}
        self.toolsets: set[str] = set()
        self.hooks: dict[str, list] = defaultdict(list)
        self.skills: dict[str, Path] = {}

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False,
                      description="", emoji="", override=False):
        assert name not in self.tools and not override and not requires_env
        self.tools[name], self.schemas[name] = handler, schema
        self.toolsets.add(toolset)

    def register_hook(self, hook_name, callback):
        self.hooks[hook_name].append(callback)

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = path

    def call(self, name: str, args: dict | None = None) -> dict:
        """A tool call the way Hermes makes it: the pre_tool_call hooks first (a block or an approval request stops
        here and is returned), then the handler; its JSON string is parsed."""
        for hook in self.hooks["pre_tool_call"]:
            directive = hook(tool_name=name, args=args or {}, task_id="test")
            if directive:
                return {"directive": directive}
        return json.loads(self.tools[name](args or {}))

    def approved(self, name: str, args: dict) -> dict:
        """A write the user approved: the hook asked, then the handler runs."""
        directive = self.hooks["pre_tool_call"][0](tool_name=name, args=args, task_id="test")
        assert directive and directive["action"] == "approve", directive
        return json.loads(self.tools[name](args))


def load_plugin():
    spec = importlib.util.spec_from_file_location("hermes_finance_plugin", PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plugin_ctx(settings: dict[str, Any] | None = None) -> FakeCtx:
    ctx = FakeCtx(settings)
    load_plugin().register(ctx)
    return ctx


def mcp_session(path: Path, calls: list[tuple[str, dict]], *, mode: str = "legacy", client: str = "pytest-client",
                transport: str = "stdio", between=None) -> tuple[list[str], list[tuple[bool, str]]]:
    """One MCP client session (the SDK's Client, in process) against build_server(path): the tools it lists, then
    (is_error, text) per call. `between()` runs once the session is open, before the calls."""
    server = mcp_server.build_server(path, transport)

    async def main():
        async with Client(server, mode=mode, client_info=types.Implementation(name=client, version="1.2.3")) as c:
            if between:
                between()
            listed = [t.name for t in (await c.list_tools()).tools]
            out = []
            for name, args in calls:
                r = await c.call_tool(name, args)
                out.append((bool(r.is_error), r.content[0].text))
            return listed, out

    return anyio.run(main)


def mcp_error(path: Path, name: str, args: dict, **kw) -> str:
    """The protocol error a call gets (an unknown tool), as text."""
    server = mcp_server.build_server(path, "stdio")

    async def main():
        async with Client(server, mode=kw.get("mode", "legacy")) as c:
            try:
                await c.call_tool(name, args)
            except Exception as e:  # MCPError
                return f"{type(e).__name__}: {e}"
            return "no error"

    return anyio.run(main)


def run_cli(argv: list[str], capsys) -> tuple[int, str, str]:
    try:
        code = cli.main(argv)
    except SystemExit as e:
        code = e.code
    out, err = capsys.readouterr()
    return code, out, err


def cli_argv(name: str, args: dict) -> list[str]:
    """`finance <command> [--flag value]... --json` for a read tool's arguments (the names are the CLI's flags)."""
    out = [tooldefs.TOOLS[name].command]
    for key, value in args.items():
        out += [f"--{key}"] if value is True else [f"--{key}", str(value)]
    return out + ["--json"]


def build_demo(path: Path, today: date = TODAY) -> None:
    """test_reads.py's demo: demo_data build + extras (30 days of holding snapshots), an AI flag, a user decision."""
    conn = db.connect(path)
    demo_data.build(conn, today)
    demo_data.extras(conn, today, holding_days=30)
    tid = conn.execute("SELECT transaction_id FROM transactions WHERE name = 'Smile Dental'").fetchone()[0]
    with conn:
        conn.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, note) "
                     "VALUES (?, 'ai-test', ?, 'flagged', 'is this a gym reimbursement?')", (tid, db.now()))
    costco = conn.execute("SELECT transaction_id FROM transactions WHERE merchant_name = 'Costco' "
                          "ORDER BY date DESC LIMIT 1").fetchone()[0]
    classify.set_category(conn, costco, "Other Shopping", "user")
    conn.close()


def build(kind: str, path: Path, today: date = TODAY) -> None:
    config.ensure_data_dir(path.parent)
    if kind == "demo":
        build_demo(path, today)
    else:
        gate_dbs.build(kind, path, today)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table_hashes(path: Path) -> dict[str, str]:
    """Each table's rows, hashed (read through a read-only connection)."""
    conn = db.connect_readonly(path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {n: hashlib.sha256(repr([tuple(r) for r in conn.execute(f"SELECT * FROM {n} ORDER BY rowid")]).encode())
                .hexdigest() for n in names}
    finally:
        conn.close()


def rows(path: Path, sql: str, args: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()
