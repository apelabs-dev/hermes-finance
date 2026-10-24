"""TASK-17: the finance_* tools (hermes_finance/tooldefs.py) through both adapters, the Hermes plugin (hermes_plugin/)
and the MCP server (mcp_server.py).

Every read tool answers exactly what `finance <command> --json` prints, through both adapters, on two synthetic DBs;
no read writes a byte; the three writes go through Hermes's approval gate (the plugin's pre_tool_call hook) and only
those; the plugin is a clean Hermes plugin (manifest = registrations, no core tool name, no network module)."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from agent_tools import (PLUGIN_DIR, ROOT, TODAY, FakeCtx, build, cli_argv, load_plugin, mcp_session, plugin_ctx,
                         rows, run_cli, sha, table_hashes)
from hermes_finance import api, cli, config, db, reads, tooldefs

HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"
HERMES_PYTHON = HERMES_AGENT / "venv" / "bin" / "python"


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)


@pytest.fixture
def demo(data_dir):
    build("demo", config.db_path())
    return config.db_path()


@pytest.fixture
def plugin(data_dir):
    return plugin_ctx()


# --- parity: plugin == MCP == `finance <command> --json` ---------------------------------------------------------

READ_CALLS = [
    ("finance_spend", {}), ("finance_spend", {"by": "category"}), ("finance_spend", {"by": "merchant", "since": "90d"}),
    ("finance_spend", {"by": "month", "since": "2026-01-01", "until": "2026-09-30"}),
    ("finance_spend", {"by": "account", "month": "2026-08", "category": "other shopping"}),
    ("finance_spend", {"merchant": "amazon", "account": "checking"}),
    ("finance_spend", {"category": "Eating Out", "by": "merchant", "limit": 3}),
    ("finance_spend", {"until": "2026-08-15"}),
    ("finance_budget_status", {}), ("finance_budget_status", {"month": "2026-08"}),
    ("finance_transactions", {}), ("finance_transactions", {"search": "zenith", "limit": 500}),
    ("finance_transactions", {"flagged": True}), ("finance_transactions", {"uncategorized": True, "limit": 500}),
    ("finance_transactions", {"month": "2026-08", "category": "eating out", "merchant": "a"}),
    ("finance_transactions", {"account": "checking", "since": "30d", "limit": 5}),
    ("finance_transactions", {"since": "2026-08-01", "until": "2026-08-31", "search": "a"}),
    ("finance_networth", {}), ("finance_networth", {"history": True}), ("finance_networth", {"custom": True}),
    ("finance_networth", {"custom": True, "history": True}),
    ("finance_bills", {}), ("finance_bills", {"next": "60d"}), ("finance_bills", {"next": "0"}),
    ("finance_loans", {}), ("finance_holdings", {}),
    ("finance_cashflow", {}), ("finance_cashflow", {"months": 24}),
    ("finance_status", {}),
    ("finance_goals", {}), ("finance_transactions", {"tag": "trip"}),  # TASK-19
    ("finance_rules", {}),  # TASK-24
]
# bad arguments: the same message everywhere (the CLI: "error: <it>" on stderr, exit 2)
READ_ERRORS = [
    ("finance_spend", {"month": "2026-13"}), ("finance_spend", {"month": "2026-08", "since": "30d"}),
    ("finance_spend", {"category": "Income"}), ("finance_spend", {"category": "Food"}),
    ("finance_transactions", {"account": "nope-0000"}), ("finance_transactions", {"since": "2026-10-01", "until": "2026-09-01"}),
    ("finance_budget_status", {"month": "1899-12"}), ("finance_bills", {"next": "800d"}),
    ("finance_cashflow", {"months": 0}), ("finance_transactions", {"limit": 501}),
]


def test_the_calls_cover_every_read_tool_and_every_argument():
    called = {name for name, _ in READ_CALLS}
    assert called == set(tooldefs.READS)
    used = {(name, key) for name, args in READ_CALLS for key in args}
    assert used == {(n, k) for n in tooldefs.READS for k in tooldefs.TOOLS[n].properties}


@pytest.mark.parametrize("kind", ["demo", "full"])
def test_every_read_tool_equals_the_cli_through_both_adapters(data_dir, capsys, kind):
    path = config.db_path()
    build(kind, path)
    cli_out = []
    for name, args in READ_CALLS + READ_ERRORS:
        code, out, err = run_cli(cli_argv(name, args), capsys)
        cli_out.append(json.loads(out) if code == 0 else {"error": err})
        assert code in (0, 2) and (code == 0) == ((name, args) in READ_CALLS), (name, args, err)
    ctx = plugin_ctx()
    plugin_out = [ctx.call(name, args) for name, args in READ_CALLS + READ_ERRORS]
    listed, mcp_out = mcp_session(path, READ_CALLS + READ_ERRORS)
    assert listed == list(tooldefs.UNGATED)                              # (TASK-22: + finance_sync)
    for (name, args), want, got, (is_error, text) in zip(READ_CALLS + READ_ERRORS, cli_out, plugin_out, mcp_out):
        if "error" in want:
            assert want["error"] == f"error: {got['error']}\n" and is_error and text == got["error"], (name, args)
        else:
            assert got == want and not is_error and json.loads(text) == want, (name, args)
    totals = {name: out for (name, args), out in zip(READ_CALLS, cli_out) if not args}
    assert totals["finance_spend"]["total"] > 0 and totals["finance_networth"]["net"] and totals["finance_status"]["items"]


def test_the_tools_defaults_and_arguments_are_the_cli_flags(capsys):
    parser = cli.build_parser()
    assert parser.parse_args(["bills"]).next == tooldefs.DEFAULT_NEXT
    assert parser.parse_args(["cashflow"]).months == tooldefs.DEFAULT_MONTHS
    assert parser.parse_args(["spend"]).limit == parser.parse_args(["txns"]).limit == api.DEFAULT_LIMIT
    subs = next(a for a in parser._actions if a.dest == "command").choices
    for name in tooldefs.READS:
        t = tooldefs.TOOLS[name]
        flags = {o.lstrip("-").replace("-", "_") for a in subs[t.command]._actions for o in a.option_strings}
        assert set(t.properties) <= flags, name                           # every argument is one of its command's flags
        assert name.startswith("finance_") and t.schema()["additionalProperties"] is False
    assert {n: t.command for n, t in tooldefs.TOOLS.items() if t.command} == {
        "finance_spend": "spend", "finance_budget_status": "budget-status", "finance_transactions": "txns",
        "finance_networth": "networth", "finance_bills": "bills", "finance_loans": "loans",
        "finance_holdings": "holdings", "finance_cashflow": "cashflow", "finance_status": "status",
        "finance_goals": "goals", "finance_rules": "rules"}
    assert all(len(t.description) <= 260 for t in tooldefs.TOOLS.values())          # sent on every API call


@pytest.mark.parametrize("args, message", [
    ({"monthh": "2026-09"}, "unknown argument 'monthh' for finance_spend (it takes: month, since, until, category, "
                            "merchant, account, limit, by)"),
    ({"limit": "10"}, "limit must be an integer"), ({"limit": True}, "limit must be an integer"),
    ({"by": "week"}, "by must be one of category, merchant, month, account"),
    ({"month": 202609}, "month must be a string"),
])
def test_arguments_are_checked_against_the_schema(demo, plugin, args, message):
    assert plugin.call("finance_spend", args) == {"error": message}
    listed, [(is_error, text)] = mcp_session(demo, [("finance_spend", args)])
    assert is_error and text == message


def test_null_means_not_given_and_the_database_must_exist_at_the_current_schema(data_dir, plugin):
    missing = plugin.call("finance_spend", {})
    assert missing["error"].startswith("database not found") and not data_dir.exists()   # never created
    build("demo", config.db_path())
    assert plugin.call("finance_spend", {"month": None, "by": None}) == plugin.call("finance_spend", {})
    assert plugin.call("finance_loans", None) == plugin.call("finance_loans", {})


# --- read-only -----------------------------------------------------------------------------------------------------

def test_no_read_tool_changes_a_byte_through_either_adapter(demo, plugin, monkeypatch):
    before, tables, mtime = sha(demo), table_hashes(demo), demo.stat().st_mtime_ns
    wal = demo.with_name(demo.name + "-wal")

    def no_writer(*a, **k):
        raise AssertionError("a read tool opened the database read-write")

    real_connect = db.connect
    monkeypatch.setattr(db, "connect", no_writer)
    for name, args in READ_CALLS:
        assert "error" not in plugin.call(name, args), name
    assert sha(demo) == before and demo.stat().st_mtime_ns == mtime and (not wal.exists() or wal.stat().st_size == 0)

    # MCP: opening the session records the client (mcp.last_used, the one write, in `settings`); from then on,
    # every read tool leaves the file byte-identical
    monkeypatch.setattr(db, "connect", real_connect)
    opened = {}

    def after_handshake():
        opened["sha"], opened["mtime"] = sha(demo), demo.stat().st_mtime_ns
        monkeypatch.setattr(db, "connect", no_writer)

    listed, out = mcp_session(demo, READ_CALLS, between=after_handshake)
    assert not any(is_error for is_error, _ in out)
    assert sha(demo) == opened["sha"] and demo.stat().st_mtime_ns == opened["mtime"]
    assert not wal.exists() or wal.stat().st_size == 0
    changed = {t for t, h in table_hashes(demo).items() if tables.get(t) != h}
    assert changed == {"settings"}
    assert [r[0] for r in rows(demo, "SELECT key FROM settings ORDER BY key")] == ["mcp.last_used"]


# --- writes: Hermes's approval gate ------------------------------------------------------------------------------

def _costco(path: Path) -> str:
    return rows(path, "SELECT transaction_id FROM v_transactions WHERE merchant_name = 'Costco' AND category_source <> 'user' "
                      "ORDER BY date LIMIT 1")[0][0]


def test_the_approval_hook_asks_for_exactly_the_four_writes(demo, plugin):
    """(TASK-21: finance_track_bill is the fourth write.)"""
    [hook] = plugin.hooks["pre_tool_call"]
    before = sha(demo)
    for name in [*tooldefs.READS, "terminal", "write_file", "memory", "execute_code", "finance_nope", ""]:
        assert hook(tool_name=name, args={"month": "2026-09"}, task_id="t") is None, name
    writes = {"finance_set_category": {"transaction_id": _costco(demo), "category": "other shopping"},
              "finance_add_rule": {"pattern": "costco", "category": "Other Shopping"},
              "finance_set_cap": {"category": "eating out", "monthly_cap": 400},
              "finance_track_bill": {"action": "track", "merchant": "netflix", "every": "1m"}}
    assert set(writes) == set(tooldefs.WRITES)
    messages = {}
    for name, args in writes.items():
        directive = hook(tool_name=name, args=args, task_id="t", session_id="s", tool_call_id="c")   # extra kwargs
        assert set(directive) == {"action", "message"} and directive["action"] == "approve", name
        messages[name] = directive["message"]
    assert re.fullmatch(r"File the transaction \d{4}-\d\d-\d\d · Costco · \$[\d,.]+ under Other Shopping \(now Groceries, set by "
                        r"plaid\)\. It becomes your decision: rules and the AI won't change it\.",
                        messages["finance_set_category"])
    assert messages["finance_add_rule"].startswith("Add a rule: file every transaction whose merchant name matches the "
                                                   "regex 'costco' (searched anywhere in the merchant name, "
                                                   "case-insensitive) under Other Shopping, now and on every sync. It matches ")
    assert "the 1 you filed yourself stay as they are." in messages["finance_add_rule"]       # the demo's user decision
    assert messages["finance_set_cap"] == "Set the monthly budget cap on Eating Out to $400 (now $250)."
    assert re.fullmatch(r"Track Netflix as a bill every month, the same amount each time \(next ~\w{3} \d{1,2}, \$15\.49\)\.",
                        messages["finance_track_bill"]), messages["finance_track_bill"]
    assert hook(tool_name="finance_set_cap", args={"category": "Eating Out", "monthly_cap": None}, task_id="t") == \
        {"action": "approve", "message": "Clear the monthly budget cap on Eating Out (now $250)."}
    assert sha(demo) == before                                                        # asking changes nothing


@pytest.mark.parametrize("name, args, reason", [
    ("finance_set_category", {"transaction_id": "t-nope", "category": "Other Shopping"},
     "unknown transaction 't-nope'"),
    ("finance_set_category", {"transaction_id": "x", "category": "Food"}, "unknown transaction 'x'"),
    ("finance_add_rule", {"pattern": "Costco Wholesale", "category": "Other Shopping", "match": "exact"},  # the pitfall
     "'Costco Wholesale' (the whole merchant name, case-insensitive) matches no transaction, so no rule was saved"),
    ("finance_add_rule", {"pattern": "zz-no-such-shop", "category": "Other Shopping"},
     "the regex 'zz-no-such-shop' (searched anywhere in the merchant name, case-insensitive) matches no transaction"),
    ("finance_add_rule", {"pattern": "costco(", "category": "Other Shopping"}, "pattern is not a valid regex"),
    ("finance_add_rule", {"pattern": "costco", "category": "Food"}, "unknown category 'Food'; categories: "),
    ("finance_set_cap", {"category": "Income", "monthly_cap": 5}, "Income is not a spending category"),
    ("finance_set_cap", {"category": "Travel", "monthly_cap": -1}, "monthly_cap must be between 0 and 100,000"),
    ("finance_set_cap", {"category": "Travel", "monthly_cap": 10 ** 400}, "monthly_cap must be between 0 and 100,000"),
    ("finance_set_cap", {"category": "Travel"}, "monthly_cap is required"),
    ("finance_set_cap", {"category": "Travel", "monthly_cap": "10"}, "monthly_cap must be a number or null"),
])
def test_a_write_that_cant_be_done_is_blocked_without_asking(demo, plugin, name, args, reason):
    before = table_hashes(demo)
    directive = plugin.call(name, args)["directive"]
    assert directive["action"] == "block" and reason in directive["message"], directive
    assert directive["message"].startswith(f"{name}: ") and directive["message"].endswith("(nothing was changed)")
    assert json.loads(plugin.tools[name](args))["error"]                     # the handler refuses it on its own too
    assert table_hashes(demo) == before


def test_approved_writes_apply_with_the_cli_rules(demo, plugin, capsys):
    tid = _costco(demo)
    out = plugin.approved("finance_set_category",
                          {"transaction_id": tid, "category": "other shopping", "note": "bulk run"})
    assert out == {"transaction_id": tid, "category": "Other Shopping", "category_source": "user", "note": "bulk run",
                   "previous": {"category": "Groceries", "category_source": "plaid"}}
    assert rows(demo, "SELECT source, note FROM classifications WHERE transaction_id = ?", (tid,)) == [("user", "bulk run")]

    state = ("SELECT t.transaction_id, t.merchant_name, v.category, c.source, c.rule_id FROM transactions t JOIN "
             "v_transactions v USING (transaction_id) LEFT JOIN classifications c USING (transaction_id) ORDER BY 1")
    before = [r for r in rows(demo, state) if re.match("cost", r[1] or "", re.I)]
    rule = plugin.approved("finance_add_rule", {"pattern": "^cost", "category": "Other Shopping"})
    after = [r for r in rows(demo, state) if re.match("cost", r[1] or "", re.I)]
    moved = [a for b, a in zip(before, after) if a != b]
    assert rule["field"] == "merchant_name" and rule["match"] == "regex" and rule["pattern"] == "^cost"
    assert rule["matched"] == len(before) and rule["refiled"] == len(moved) > 0          # the re-filed count is real
    assert all(a[2:] == ("Other Shopping", "rule", rule["rule_id"]) for a in moved)
    assert {a[3] for a in after} == {"user", "rule"}                                     # the user's rows stay theirs
    costco = [r for r in after if r[1] == "Costco"]
    assert rows(demo, "SELECT match_field, match_type, pattern, created_from, priority FROM rules WHERE id = ?",
                (rule["rule_id"],)) == [("merchant_name", "regex", "^cost", "user", 200)]
    exact = plugin.approved("finance_add_rule", {"pattern": "COSTCO", "category": "Other Shopping", "match": "exact"})
    assert exact["match"] == "exact" and exact["matched"] == len(costco)                 # exact: the whole name

    assert plugin.approved("finance_set_cap", {"category": "travel", "monthly_cap": 123.456}) == \
        {"category": "Travel", "monthly_cap": 123.46, "previous": None}
    code, text, _ = run_cli(["budget-status", "--json"], capsys)
    assert {c["category"]: c["cap"] for c in json.loads(text)["caps"]}["Travel"] == 123.46   # the CLI sees it
    assert plugin.approved("finance_set_cap", {"category": "Travel", "monthly_cap": None})["previous"] == 123.46
    assert rows(demo, "SELECT monthly_cap FROM categories WHERE name = 'Travel'") == [(None,)]


def test_the_hook_fails_closed(demo, plugin, monkeypatch):
    """A crash while describing the change still asks (Hermes skips a hook that raises: the write would run unasked)."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(tooldefs, "describe", boom)
    directive = plugin.hooks["pre_tool_call"][0](tool_name="finance_set_cap", args={"category": "Travel", "monthly_cap": 5},
                                                 task_id="t")
    assert directive == {"action": "approve",
                         "message": 'finance_set_cap with {"category": "Travel", "monthly_cap": 5}'}


# --- the plugin: manifest, registration, settings, skill -------------------------------------------------------------

def _manifest_list(key: str) -> list[str]:
    lines = (PLUGIN_DIR / "plugin.yaml").read_text().splitlines()
    start = lines.index(f"{key}:")
    out = []
    for line in lines[start + 1:]:
        if not line.startswith("  - "):
            break
        out.append(line[4:].strip())
    return out


def test_the_manifest_lists_exactly_what_registers(plugin):
    text = (PLUGIN_DIR / "plugin.yaml").read_text()
    assert re.search(r"^name: hermes-finance$", text, re.M)
    import tomllib
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert re.search(rf"^version: {re.escape(version)}$", text, re.M)
    assert "requires_env" not in text
    assert _manifest_list("provides_tools") == list(plugin.tools) == list(tooldefs.TOOLS)
    assert _manifest_list("provides_hooks") == list(plugin.hooks) == ["pre_tool_call"]
    assert plugin.toolsets == {"hermes-finance"}
    for name, schema in plugin.schemas.items():
        t = tooldefs.TOOLS[name]
        assert schema == {"name": name, "description": t.description, "parameters": t.schema()}
    assert plugin.skills == {"usage": PLUGIN_DIR / "skills" / "usage" / "SKILL.md"}


def test_the_skill_is_generic():
    text = (PLUGIN_DIR / "skills" / "usage" / "SKILL.md").read_text()
    assert text.startswith("---\nname: usage\ndescription: ") and "\n---\n" in text
    for needle in ("Sign convention", "Never guess a category", "Approval rules", "finance_add_rule"):
        assert needle in text
    for name in tooldefs.TOOLS:
        assert f"`{name}`" in text
    assert not re.search(r"/(?:Users|home)/|@[a-z0-9-]+\.[a-z]{2,}|\$\d", text, re.I)
    # no private word either (scripts/dehardcode.py's untracked sources: the runner's own names, the env, the word file)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import dehardcode
    assert dehardcode.Matcher(dehardcode.private_words()).scan("SKILL.md", text) == []


def test_the_database_comes_from_the_setting_else_the_apps_own(data_dir, tmp_path):
    other = tmp_path / "elsewhere"
    build("demo", other / "finance.db")
    assert plugin_ctx().call("finance_loans")["error"].startswith(f"database not found: {config.db_path()}")
    configured = plugin_ctx({"HERMES_FINANCE_DATA_DIR": str(other)})
    assert configured.call("finance_loans")["count"] == 2
    assert not data_dir.exists()                                             # the default dir was never created


def test_the_app_is_found_from_the_setting_or_the_environment(data_dir, tmp_path, monkeypatch):
    module = load_plugin()
    with pytest.raises(ImportError, match=r"HERMES_FINANCE_APP_DIR=.*nowhere has no src/hermes_finance"):
        module.register(FakeCtx({"HERMES_FINANCE_APP_DIR": str(tmp_path / "nowhere")}))
    monkeypatch.setenv("HERMES_FINANCE_APP_DIR", str(tmp_path / "nowhere-either"))
    with pytest.raises(ImportError, match="nowhere-either has no src/hermes_finance"):
        module.register(FakeCtx())
    monkeypatch.setenv("HERMES_FINANCE_APP_DIR", str(ROOT))
    ctx = FakeCtx()
    module.register(ctx)
    assert list(ctx.tools) == list(tooldefs.TOOLS)


# --- static: no network module, no Hermes core tool name ------------------------------------------------------------

FORBIDDEN = {"plaid", "urllib", "requests", "httpx", "socket"}
ADAPTERS = [*sorted(PLUGIN_DIR.rglob("*.py")), ROOT / "src" / "hermes_finance" / "mcp_server.py",
            ROOT / "src" / "hermes_finance" / "tooldefs.py", ROOT / "src" / "hermes_finance" / "mcp_state.py"]


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            found |= {(node.module or "").split(".")[-1 if node.level else 0]} | {a.name for a in node.names}
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                "import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant):
            found |= set(str(node.args[0].value).split("."))
    return found


def test_no_adapter_module_imports_a_network_module():
    # every plugin module (the crons/ package too) plus the app's three adapter modules
    assert len(ADAPTERS) == len(list(PLUGIN_DIR.rglob("*.py"))) + 3 and ADAPTERS[0] == PLUGIN_DIR / "__init__.py"
    assert PLUGIN_DIR / "crons" / "reconcile.py" in ADAPTERS
    for path in ADAPTERS:
        assert not _imports(path) & FORBIDDEN, (path, _imports(path) & FORBIDDEN)
    assert "urllib" in _imports(ROOT / "src" / "hermes_finance" / "plaid.py")          # the check does see them


def _hermes_core_tools() -> set[str]:
    """Every tool name Hermes itself defines, read (never imported) from ~/.hermes/hermes-agent: toolsets.py's
    _HERMES_CORE_TOOLS and every toolset's tools, and each tools/*.py registry.register(name=...)."""
    names: set[str] = set()
    tree = ast.parse((HERMES_AGENT / "toolsets.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_HERMES_CORE_TOOLS" for t in node.targets):
            names |= set(ast.literal_eval(node.value))
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "tools" and isinstance(value, ast.List):
                    names |= {e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    for path in sorted((HERMES_AGENT / "tools").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "register":
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        names.add(kw.value.value)
    return names


@pytest.mark.skipif(not (HERMES_AGENT / "toolsets.py").exists(), reason="no Hermes checkout at ~/.hermes/hermes-agent")
def test_no_tool_collides_with_a_hermes_core_tool():
    core = _hermes_core_tools()
    assert {"terminal", "read_file", "web_search", "memory", "delegate_task"} <= core and len(core) > 50
    assert not core & set(tooldefs.TOOLS)
    assert all(name.startswith("finance_") for name in tooldefs.TOOLS)


@pytest.mark.skipif(not HERMES_PYTHON.exists(), reason="no Hermes interpreter at ~/.hermes/hermes-agent/venv")
def test_the_plugin_answers_the_same_under_hermess_own_python(data_dir, monkeypatch):
    """Hermes runs plugins in its own interpreter (3.11 today, while the app is built on 3.12): load the plugin there
    (a copy elsewhere, found through HERMES_FINANCE_APP_DIR, as `hermes plugins doctor` loads it) and compare."""
    today = date.today()
    build("demo", config.db_path(), today)
    script = (
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('p', {str(PLUGIN_DIR / '__init__.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "class Ctx:\n"
        "    tools = {}; hooks = []; skills = {}\n"
        "    def get_config(self, key, default=None): return default\n"
        "    def register_tool(self, name, toolset, schema, handler, **kw): self.tools[name] = handler\n"
        "    def register_hook(self, name, cb): self.hooks.append(cb)\n"
        "    def register_skill(self, name, path, description=''): self.skills[name] = str(path)\n"
        "ctx = Ctx(); m.register(ctx)\n"
        "calls = json.loads(sys.argv[1])\n"
        "print(json.dumps({'version': list(sys.version_info[:2]), 'hook': ctx.hooks[0](tool_name='finance_spend', args={}),\n"
        "                  'out': [json.loads(ctx.tools[n](a)) for n, a in calls]}))\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PLAID_", "PYTHON"))
           and k not in ("MARKETCHECK_API_KEY", "RENTCAST_API_KEY", "VIRTUAL_ENV")}
    env["HERMES_FINANCE_APP_DIR"] = str(ROOT)
    proc = subprocess.run([str(HERMES_PYTHON), "-I", "-c", script, json.dumps(READ_CALLS)], env=env, cwd=data_dir.parent,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    there = json.loads(proc.stdout)
    assert there["hook"] is None and there["version"][0] == 3
    monkeypatch.setattr(tooldefs, "today", lambda: today)
    here = plugin_ctx()
    assert there["out"] == [here.call(n, a) for n, a in READ_CALLS]
