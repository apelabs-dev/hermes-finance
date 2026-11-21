"""Settings' side of the CLI brains (admin.py): the settings-page payload, saving ai.mode/ai.cli, the isolation check's
route (busy, off, unknown id) and the stored result. The CLIs are tests/cli_stub.py's stand-ins: no real `claude` or
`codex` ever runs, no model is reached, no network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import cli_stub as cs
from hermes_finance import clibrain, config, db, prefs
from hermes_finance.web import App

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json"}
TODAY = date(2026, 9, 26)
PASSING = cs.claude_stream({"type": "system", "subtype": "init", "tools": [], "mcp_servers": []},
                           {"type": "result", "result": json.dumps(
                               {"kind": "answer", "text": "Nothing was readable, writable or reachable."})})


@pytest.fixture
def stub(tmp_path, monkeypatch):
    return cs.StubCLIs(tmp_path / "cli", monkeypatch)


@pytest.fixture
def app(conn, stub):
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


def call(app, method, path, body=None):
    status, ctype, out, extra = app.respond(method, path, JSONH if method == "POST" else LOCAL,
                                            json.dumps(body).encode() if body is not None else b"")
    return status, (json.loads(out) if ctype.startswith("application/json") else out.decode()), dict(extra)


# --- the page and the settings ---------------------------------------------------------------------------------------

def test_the_settings_page_lists_both_clis_and_the_chosen_one(app, stub):
    s, d, _ = call(app, "GET", "/api/settings/page")
    assert s == 200
    cli = d["ai"]["cli"]
    assert cli["chosen"] == "claude" and cli["off"] is False
    assert [i["id"] for i in cli["items"]] == ["claude", "codex"]
    assert [(i["name"], i["found"], i["version"]) for i in cli["items"]] == [
        ("Claude Code", True, "2.1.286"), ("Codex", True, "0.162.1")]
    for item in cli["items"]:
        assert item["path"] == str(stub[item["id"]].path) and item["display"] == cs.shown(item["path"])
        assert item["passed"] is False and item["checked_version"] is None and item["reason"] == ""


def test_cli_mode_and_the_chosen_cli_save_and_an_unknown_one_is_refused(app):
    s, d, _ = call(app, "POST", "/api/prefs", {"ai.mode": "cli", "ai.cli": "codex"})
    assert s == 200 and d["saved"] == {"ai.mode": "cli", "ai.cli": "codex"}
    with db.connect(config.db_path()) as conn:
        assert prefs.get(conn, "ai.mode") == "cli" and prefs.get(conn, "ai.cli") == "codex"
    s, d, _ = call(app, "POST", "/api/prefs", {"ai.cli": "gpt"})
    assert s == 400 and d["field"] == "ai.cli"
    with db.connect(config.db_path()) as conn:
        assert prefs.get(conn, "ai.cli") == "codex"          # nothing written


# --- the isolation check's route -------------------------------------------------------------------------------------

def test_the_check_saves_a_passing_result_and_a_running_check_is_busy(app, stub):
    assert clibrain.LOCKS.acquire(blocking=False)
    try:
        s, d, _ = call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
        assert s == 409 and d == {"busy": True} and stub.claude.turns() == []
    finally:
        clibrain.LOCKS.release()
    stub.claude.script(PASSING)
    s, d, _ = call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    assert s == 200 and d["item"]["id"] == "claude"
    assert (d["item"]["passed"], d["item"]["checked_version"], d["item"]["version"]) == (True, "2.1.286", "2.1.286")
    assert d["item"]["checked_at"] and d["item"]["reason"] == ""
    with db.connect(config.db_path()) as conn:
        stored = clibrain.check_state(conn)["claude"]
    assert stored["passed"] is True and stored["version"] == "2.1.286" and stored["at"] == d["item"]["checked_at"]
    assert [t["argv"][:5] for t in stub.claude.turns()] == [["-p", "--tools", "", "--disallowedTools", "mcp__*"]]


def test_the_off_switch_stops_the_cli_everywhere(app, monkeypatch):
    monkeypatch.setenv(clibrain.OFF_ENV, "off")
    assert clibrain.executable("claude") is None
    s, d, _ = call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    assert s == 409 and d == {"error": clibrain.line("off")}
    with db.connect(config.db_path()) as conn:
        prefs.save(conn, {"ai.mode": "cli"})
    s, d, _ = call(app, "POST", "/api/chat", {"messages": [{"role": "user", "content": "Hi"}]})
    assert s == 503 and d["error"] == clibrain.line("off")


def test_an_unknown_cli_id_is_refused(app):
    s, d, _ = call(app, "POST", "/api/ai/cli/check", {"cli": "gpt"})
    assert s == 400 and d["field"] == "cli" and "claude" in d["error"]


def test_a_codex_that_can_still_run_tools_fails_with_a_reason_that_names_the_problem_and_the_next_step(app, stub):
    """The stream has a real tool item: the check fails (Ask stays refused) and Settings shows plain copy, not
    "it has a tool". Checks saved with the old wording read the new way too."""
    events = [json.loads(line) for line in (Path(__file__).parent / "fixtures" / "cli_streams"
                                            / "codex-command-execution.jsonl").read_text().splitlines() if line.strip()]
    stub.codex.script(cs.codex(*events))
    s, d, _ = call(app, "POST", "/api/ai/cli/check", {"cli": "codex"})
    assert s == 200 and d["item"]["passed"] is False
    reason = d["item"]["reason"]
    assert reason == clibrain.TOOL_REASON == ("Codex can still run tools in this mode, so it isn't safe to use here. "
                                              "Update Codex, or use Claude Code.")
    assert "it has a tool" not in reason and len(reason) <= 160   # settings.js shows a reason up to 160 characters
    with db.connect(config.db_path()) as conn:
        assert clibrain.ready(conn, "codex")[1] == "failed_check"   # the 503 refusal on /api/chat keys on this code
        clibrain.save_check(conn, "codex", clibrain.version(stub.codex.path), False, "it has a tool", {})
    s, d, _ = call(app, "GET", "/api/settings/page")
    assert next(i for i in d["ai"]["cli"]["items"] if i["id"] == "codex")["reason"] == clibrain.TOOL_REASON
