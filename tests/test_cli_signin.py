"""Settings' side of finding a CLI and knowing whether it is signed in: the saved-path prefs (ai.cli_path.<cli>), and
the three sign-in states the app can derive from stored evidence only (signed_in / signed_out / unknown). The app never
runs a status command and never calls a model on a page load. The CLIs are tests/cli_stub.py's stand-ins."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import cli_stub as cs
from hermes_finance import admin, clibrain, config, db, prefs
from hermes_finance.web import App

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json"}
TODAY = date(2026, 9, 26)
PASSING = cs.claude_stream({"type": "system", "subtype": "init", "tools": [], "mcp_servers": []},
                           {"type": "result", "result": json.dumps(
                               {"kind": "answer", "text": "Nothing was readable, writable or reachable."})})
SIGNED_OUT = cs.claude_stream({"type": "result", "is_error": True, "result": "Not logged in. Please run /login."})
NOT_FOUND_DIRS = ["~/.local/bin", "~/.claude/local"]


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
    return status, (json.loads(out) if ctype.startswith("application/json") else out.decode())


def item(app, cli_id="claude"):
    s, d = call(app, "GET", "/api/settings/page")
    assert s == 200
    return next(i for i in d["ai"]["cli"]["items"] if i["id"] == cli_id)


def executable_file(path: Path, name: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path if name is None else path.parent / name
    target.write_text("#!/bin/sh\necho 'tool 1.2.3'\n")
    target.chmod(0o755)
    return target


# --- the saved path --------------------------------------------------------------------------------------------------

def test_a_saved_path_is_validated_and_stored_absolute(app, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    good = executable_file(tmp_path / "bin" / "claude")
    s, d = call(app, "POST", "/api/prefs", {"ai.cli_path.claude": str(good)})
    assert s == 200 and d["saved"] == {"ai.cli_path.claude": str(good)}
    s, d = call(app, "POST", "/api/prefs", {"ai.cli_path.claude": "~/bin/claude"})          # ~ expands
    assert s == 200 and d["saved"] == {"ai.cli_path.claude": str(good)}
    s, d = call(app, "POST", "/api/prefs", {"ai.cli_path.claude": ""})                      # empty = unset
    assert s == 200 and d["saved"] == {"ai.cli_path.claude": ""}
    with db.connect(config.db_path()) as conn:
        assert prefs.get(conn, "ai.cli_path.claude") == "" and prefs.get(conn, "ai.cli_path.codex") == ""


@pytest.mark.parametrize("make", ["relative", "directory", "missing", "not_executable", "wrong_name", "not_text"])
def test_a_bad_saved_path_is_refused_with_plain_words_and_nothing_is_written(app, tmp_path, make):
    base = tmp_path / "bin"
    base.mkdir()
    value = {"relative": "claude", "directory": str(base), "missing": str(base / "claude"),
             "not_executable": str(base / "claude"), "wrong_name": str(executable_file(base / "other")),
             "not_text": 5}[make]
    if make == "not_executable":
        (base / "claude").write_text("x")
        (base / "claude").chmod(0o644)
    s, d = call(app, "POST", "/api/prefs", {"ai.cli_path.claude": value})
    assert s == 400 and d["field"] == "ai.cli_path.claude"
    assert d["error"] == "That isn't an executable file named claude."
    assert "ai.cli_path" not in d["error"]
    with db.connect(config.db_path()) as conn:
        assert prefs.get(conn, "ai.cli_path.claude") == ""


def test_the_name_must_be_the_clis_own_binary(app, tmp_path):
    codex = executable_file(tmp_path / "bin" / "codex")
    s, d = call(app, "POST", "/api/prefs", {"ai.cli_path.claude": str(codex)})
    assert s == 400 and d["error"] == "That isn't an executable file named claude."
    s, d = call(app, "POST", "/api/prefs", {"ai.cli_path.codex": str(codex)})
    assert s == 200


def test_a_saved_path_that_stops_being_valid_is_ignored_at_use(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.delenv("FINANCE_CLAUDE_BIN", raising=False)
    monkeypatch.delenv(clibrain.OFF_ENV, raising=False)
    monkeypatch.setattr(clibrain, "SYSTEM_DIRS", (), raising=False)
    saved = executable_file(tmp_path / "bin" / "claude")
    prefs.save(conn, {"ai.cli_path.claude": str(saved)})
    assert clibrain.executable("claude", conn) == saved
    saved.chmod(0o644)                                          # no longer executable
    assert clibrain.executable("claude", conn) is None
    saved.chmod(0o755)
    with conn:                                                  # a hand-edited row naming some other program
        conn.execute("UPDATE settings SET value = ? WHERE key = 'ai.cli_path.claude'",
                     (json.dumps(str(executable_file(tmp_path / "bin" / "other"))),))
    assert clibrain.executable("claude", conn) is None
    assert prefs.get(conn, "ai.cli_path.claude") == ""


def test_the_saved_path_is_what_the_page_check_and_chat_gate_use(app, conn, stub, tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCE_CLAUDE_BIN")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setattr(clibrain, "SYSTEM_DIRS", (), raising=False)
    assert item(app)["found"] is False
    s, d = call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    assert s == 503 and d["field"] == "cli"
    assert call(app, "POST", "/api/prefs", {"ai.cli_path.claude": str(stub.claude.path)})[0] == 200
    got = item(app)
    assert got["found"] is True and got["path"] == str(stub.claude.path) and got["saved_path"] == got["display"]
    with db.connect(config.db_path()) as c:
        assert clibrain.ready(c, "claude") == (stub.claude.path, "not_checked")   # the gate is unchanged
    stub.claude.script(PASSING)
    s, d = call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    assert s == 200 and d["item"]["passed"] is True
    with db.connect(config.db_path()) as c:
        assert clibrain.ready(c, "claude") == (stub.claude.path, "")


# --- sign-in: three states from stored evidence ----------------------------------------------------------------------

def test_a_cli_with_no_check_yet_is_unknown_and_no_model_is_called(app, stub):
    got = item(app)
    assert (got["signin"], got["signin_at"]) == ("unknown", None)
    assert got["login_command"] == "claude auth login" and item(app, "codex")["login_command"] == "codex login"
    assert stub.claude.turns() == [] and stub.codex.turns() == []      # a page load starts no model call
    assert [r["kind"] for r in stub.claude.records("probe")] == ["version"]   # --version at most (never `auth status`)


def test_a_passing_check_for_the_current_version_is_signed_in_with_its_date(app, stub):
    stub.claude.script(PASSING)
    s, d = call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    assert s == 200
    assert (d["item"]["signin"], d["item"]["signin_at"]) == ("signed_in", d["item"]["checked_at"])
    assert item(app)["signin"] == "signed_in" and item(app)["signin_at"] == d["item"]["checked_at"]


def test_a_changed_version_makes_the_stored_sign_in_unknown(app, stub):
    stub.claude.script(PASSING)
    call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    stub.claude.set_version("2.1.999 (Claude Code)")
    stub.claude.path.touch()                                  # a new executable: the version cache keys on the file
    got = item(app)
    assert (got["signin"], got["signin_at"]) == ("unknown", None)


def test_an_auth_failure_in_the_check_is_stored_as_signed_out_and_survives_a_reload(app, stub):
    stub.claude.script(SIGNED_OUT)
    s, d = call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    assert s == 502 and d["field"] == "cli" and "isn't signed in" in d["error"]   # the route's contract is unchanged
    got = item(app)                                                               # a fresh request, a fresh read
    assert got["signin"] == "signed_out" and got["signin_at"] and got["passed"] is False
    with db.connect(config.db_path()) as c:
        assert clibrain.ready(c, "claude")[1] == "failed_check"                   # chat stays refused
    assert item(app, "codex")["signin"] == "unknown"                              # only that CLI


def test_the_next_pass_clears_signed_out_and_a_non_auth_failure_is_unknown(app, stub):
    stub.claude.script(SIGNED_OUT, PASSING)
    call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    assert item(app)["signin"] == "signed_out"
    s, d = call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    assert s == 200 and d["item"]["signin"] == "signed_in"
    stub.claude.script(SIGNED_OUT, cs.claude_stream({"type": "system", "subtype": "init", "tools": ["Bash"],
                                                    "mcp_servers": []}, {"type": "result", "result": "x"}))
    call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    assert item(app)["signin"] == "signed_out"
    s, d = call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})              # failed isolation, not an auth error
    assert s == 200 and d["item"]["passed"] is False and d["item"]["signin"] == "unknown"


def test_a_signed_out_mark_for_an_older_version_is_unknown(app, stub):
    stub.claude.script(SIGNED_OUT)
    call(app, "POST", "/api/ai/cli/check", {"cli": "claude"})
    stub.claude.set_version("2.2.0 (Claude Code)")
    stub.claude.path.touch()
    assert item(app)["signin"] == "unknown"


def test_the_stored_check_row_keeps_working_for_old_rows(conn):
    clibrain.save_check(conn, "claude", "1.0.0", True, "", {})
    got = clibrain.check_state(conn)["claude"]
    assert got["passed"] is True and got["signed_out"] is False
    with conn:
        conn.execute("UPDATE settings SET value = ? WHERE key = ?",
                     (json.dumps({"claude": {"version": "1.0.0", "passed": True, "at": "2026-01-01T00:00:00Z"}}),
                      clibrain.STATE_KEY))
    assert clibrain.check_state(conn)["claude"]["signed_out"] is False


# --- not found --------------------------------------------------------------------------------------------------------

def test_a_cli_that_isnt_found_says_so_and_lists_where_it_looked(app, tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCE_CLAUDE_BIN")
    monkeypatch.delenv("FINANCE_CODEX_BIN")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(clibrain, "SYSTEM_DIRS", (Path("/opt/hf-nowhere/bin"),), raising=False)
    for cli_id in ("claude", "codex"):
        got = item(app, cli_id)
        assert got["found"] is False and got["path"] is None and got["display"] is None and got["version"] is None
        assert got["searched"] == ["~/.local/bin", "~/.claude/local", "/opt/hf-nowhere/bin", "~/.npm-global/bin"]
        assert (got["signin"], got["signin_at"], got["saved_path"]) == ("unknown", None, "")


def test_the_searched_dirs_come_from_clibrains_single_list(app):
    shown = [admin._display(str(d)) for d in clibrain.search_dirs()]
    assert item(app)["searched"] == shown
