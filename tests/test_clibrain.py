"""The CLI brain's app side (clibrain.py): where the CLI is found, the exact lockdown command lines, the child's small
environment and bounds (deadline, process group, stdout, counted stderr rejections for the Codex self-test only, and
the child's exit releasing a stderr pipe a grandchild holds open), each CLI's answer format, the stored isolation
check (ready: off / missing / not checked / version changed / failed /
passed), the adversarial self-test, and the request's one api_calls row. Every child here is tests/cli_stub.py's
stand-in: no real `claude` or `codex` ever runs, no model is reached, no network."""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

import cli_stub as cs
from hermes_finance import bridge, clibrain, usage

TODAY = date(2026, 9, 26)
LOCKDOWN = ["-p", "--tools", "", "--disallowedTools", "mcp__*", "--strict-mcp-config", "--safe-mode",
            "--permission-prompts", "none", "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
REQ = {"prompt": json.dumps({"kind": "answer", "text": "hi"}), "cli": "claude"}
STREAMS = Path(__file__).resolve().parent / "fixtures" / "cli_streams"
CANARY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
INIT_OK = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}
RESULT_OK = {"type": "result", "result": json.dumps({"kind": "answer", "text": "no tools were used"})}


@pytest.fixture
def stub(tmp_path, monkeypatch):
    return cs.StubCLIs(tmp_path / "cli", monkeypatch)


def soon(seconds: float = 20.0) -> float:
    return time.monotonic() + seconds


def turn_answer(text: str = "ok") -> dict:
    """A Claude turn's stream: init with no tools, then the model text as it is (a decision in a real turn)."""
    return {"claude_stream": [INIT_OK, {"type": "result", "result": text,
                                        "usage": {"input_tokens": 1000, "output_tokens": 50},
                                        "total_cost_usd": 0.0123456}]}


# --- where the CLI is ------------------------------------------------------------------------------------------------

def test_the_env_override_wins_when_it_is_an_absolute_executable_file(stub):
    assert clibrain.executable("claude") == stub.claude.path
    assert clibrain.executable("codex") == stub.codex.path


def test_a_relative_or_non_executable_override_is_ignored(stub, tmp_path, monkeypatch):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("FINANCE_CLAUDE_BIN", "claude")                       # relative
    assert clibrain.executable("claude") is None
    plain = tmp_path / "not-exec"
    plain.write_text("x")
    monkeypatch.setenv("FINANCE_CLAUDE_BIN", str(plain))                     # absolute, not executable
    assert clibrain.executable("claude") is None


def test_off_means_off(stub, monkeypatch):
    monkeypatch.setenv(clibrain.OFF_ENV, "0")
    assert clibrain.off() and clibrain.executable("claude") is None and clibrain.executable("codex") is None
    monkeypatch.setenv(clibrain.OFF_ENV, "no")
    assert clibrain.off()
    monkeypatch.setenv(clibrain.OFF_ENV, "on")
    assert not clibrain.off() and clibrain.executable("claude") == stub.claude.path


def test_installed_names_both_clis(stub):
    got = clibrain.installed("claude")
    assert got == {"id": "claude", "name": "Claude Code", "path": str(stub.claude.path), "version": "2.1.286",
                   "found": True}


# --- version ---------------------------------------------------------------------------------------------------------

def test_version_parses_each_cli_line(stub):
    assert clibrain.version(stub.claude.path) == "2.1.286"
    assert clibrain.version(stub.codex.path) == "0.162.1"


def test_version_is_none_when_it_cannot_be_read(stub, tmp_path):
    stub.claude.set_version("no version here")
    assert clibrain.version(stub.claude.path) is None
    stub.codex.set_version("v1.2")                                           # not three parts
    assert clibrain.version(stub.codex.path) is None
    assert clibrain.version(tmp_path / "no-such-cli") is None


def test_version_probes_once_per_executable_and_caches_by_file(stub):
    """chat.state and the Settings payload ask on every page load: the second ask must not spawn --version again."""
    assert clibrain.version(stub.claude.path) == "2.1.286"
    assert clibrain.version(stub.claude.path) == "2.1.286"
    assert [p["kind"] for p in stub.claude.records("probe")] == ["version"]
    stub.claude.set_version("2.1.999 (Claude Code)")
    stub.claude.path.touch()                                                 # a new executable file: mtime changes the key
    assert clibrain.version(stub.claude.path) == "2.1.999"
    assert [p["kind"] for p in stub.claude.records("probe")] == ["version", "version"]


# --- the command lines and the child ---------------------------------------------------------------------------------

def test_the_claude_turn_is_the_exact_lockdown_and_the_prompt_goes_on_stdin(stub, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-the-child")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-the-child")
    monkeypatch.setenv("CODEX_HOME", "/tmp/should-not-reach-the-child")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    stub.claude.script(turn_answer("Shopping is fine."))
    req = clibrain.request("claude", "Be brief.", [{"role": "user", "content": "How is Shopping?"}], [], 1024)
    reply = clibrain.complete("claude", stub.claude.path, req, soon())
    [run] = stub.claude.turns()
    assert run["argv"] == LOCKDOWN
    assert run["stdin"] == req["prompt"] and "How is Shopping?" in run["stdin"]
    assert reply.text == "Shopping is fine." and reply.provider == "claude-code"
    assert reply.tokens_in == 1000 and reply.tokens_out == 50
    assert reply.cost_usd is None and reply.list_usd == 0.0123456          # a list price, never this app's spend
    assert reply.model == "default"
    cwd = Path(run["cwd"])
    assert cwd.name.startswith("hf-cli-") and not cwd.exists()               # a fresh one, removed afterwards
    names = set(run["env"])
    assert {"HOME", "PATH"} <= names
    assert not {n for n in names if n.startswith(("ANTHROPIC_", "CLAUDE_", "OPENAI_", "CODEX_"))}
    assert not {n for n in names if "PROXY" in n.upper()}
    assert run["environ"]["PATH"] == clibrain.PATH
    assert run["environ"]["TERM"] == "dumb" and run["environ"]["NO_COLOR"] == "1"


def test_the_codex_turn_carries_every_lockdown_flag(stub):
    stub.codex.script(cs.codex_answer("Shopping is fine."))
    req = clibrain.request("codex", "Be brief.", [{"role": "user", "content": "How is Shopping?"}], [], 1024)
    reply = clibrain.complete("codex", stub.codex.path, req, soon())
    [run] = stub.codex.turns()
    argv = run["argv"]
    assert argv[:8] == ["exec", "--sandbox", "read-only", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                        "--skip-git-repo-check", "-c"]
    assert argv[8] == "project_doc_max_bytes=0"
    for feature in clibrain.DISABLES:
        assert any(argv[i] == "--disable" and argv[i + 1] == feature for i in range(len(argv) - 1)), feature
    assert all(a != f"--disable={feature}" for a in argv for feature in clibrain.DISABLES)
    assert "--json" in argv and argv[-3] == "-C" and Path(argv[-2]).resolve() == Path(run["cwd"]).resolve()
    assert argv[-1] == "-"
    assert run["stdin"] == req["prompt"]
    assert json.loads(reply.text) == {"kind": "answer", "text": "Shopping is fine."}
    assert reply.provider == "codex" and reply.cost_usd is None


# --- what each CLI answers -------------------------------------------------------------------------------------------

def test_a_claude_result_with_no_price_is_unknown_not_zero(stub):
    stub.claude.script(cs.claude_turn({"kind": "answer", "text": "x"}, init={**INIT_OK, "model": "claude-synthetic"},
                                      usage={"input_tokens": 7, "output_tokens": 3}))
    reply = clibrain.complete("claude", stub.claude.path, REQ, soon())
    assert (reply.tokens_in, reply.tokens_out, reply.cost_usd, reply.model) == (7, 3, None, "claude-synthetic")
    assert reply.list_usd is None


def test_an_impossible_claude_price_is_unknown(stub):
    stub.claude.script(cs.answer("x", total_cost_usd=5000))
    reply = clibrain.complete("claude", stub.claude.path, REQ, soon())
    assert reply.cost_usd is None and reply.list_usd is None


def test_the_claude_token_count_includes_both_cache_kinds(stub):
    stub.claude.script(cs.claude_turn({"kind": "answer", "text": "x"}, usage={
        "input_tokens": 2, "cache_creation_input_tokens": 1796, "cache_read_input_tokens": 1260,
        "output_tokens": 275}))
    reply = clibrain.complete("claude", stub.claude.path, REQ, soon())
    assert reply.tokens_in == 3058 and reply.tokens_out == 275


def test_a_claude_turn_from_the_captured_stream(tmp_path):
    """The captured stream-json turn drives the real parser: the last result's decision, the modelUsage key, both
    cache counts, and a list price that stays out of cost_usd."""
    reply = clibrain._claude_reply(fixture("claude-locked-turn.jsonl"), "Claude Code")
    decision = json.loads(reply.text)
    assert (decision["kind"], decision["tool"]) == ("read", "finance_spend")
    assert (reply.provider, reply.model) == ("claude-code", "claude-opus-5-5")
    assert (reply.tokens_in, reply.tokens_out) == (2 + 1796 + 1260, 275)
    assert reply.cost_usd is None and reply.list_usd == 0.020128


@pytest.mark.parametrize("step,code", [
    ({"claude_stream": [INIT_OK, {"type": "result", "result": "x", "is_error": True}]}, "failed"),
    ({"raw": "not json at all\n"}, "garbled"),
    ({"claude_stream": [INIT_OK]}, "garbled"),
    ({"raw": ""}, "garbled"),
    ({"claude_stream": [RESULT_OK]}, "garbled"),                          # no init: its lockdown is unproven
    ({"claude_stream": [{**INIT_OK, "tools": ["Read"]}, RESULT_OK]}, "failed"),
    ({"claude_stream": [{**INIT_OK, "mcp_servers": [{"name": "finance"}]}, RESULT_OK]}, "failed"),
])
def test_bad_claude_answers_are_one_code(stub, step, code):
    stub.claude.script(step)
    with pytest.raises(clibrain.CliError) as e:
        clibrain.complete("claude", stub.claude.path, REQ, soon())
    assert e.value.code == code and e.value.status in (502, 504)


def test_the_last_codex_agent_message_wins_with_the_turn_usage(stub):
    stub.codex.script({"codex": [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "last"}},
        {"type": "turn.completed", "usage": {"input_tokens": 42, "output_tokens": 9, "cached_input_tokens": 4}},
    ]})
    reply = clibrain.complete("codex", stub.codex.path, REQ, soon())
    assert (reply.text, reply.tokens_in, reply.tokens_out, reply.cost_usd) == ("last", 42, 9, None)


@pytest.mark.parametrize("events,code", [
    ([{"type": "turn.failed", "error": {"message": "the model fell over"}}], "failed"),
    ([{"type": "error", "message": "login required: no credentials"}], "auth"),
    ([{"type": "turn.failed", "error": {"message": "rate limit exceeded (429)"}}], "rate_limited"),
    ([{"type": "turn.completed", "usage": {"input_tokens": 1}}], "garbled"),
])
def test_codex_failures_are_classified(stub, events, code):
    stub.codex.script({"codex": events})
    with pytest.raises(clibrain.CliError) as e:
        clibrain.complete("codex", stub.codex.path, REQ, soon())
    assert e.value.code == code


# --- bounds ----------------------------------------------------------------------------------------------------------

def test_the_deadline_kills_the_process_group(stub):
    stub.claude.script({"grandchild": True, "sleep": 30})
    with pytest.raises(clibrain.CliError) as e:
        clibrain.complete("claude", stub.claude.path, REQ, time.monotonic() + clibrain.MIN_CALL + 0.8)
    assert e.value.code == "timeout" and e.value.status == 504
    pids = stub.claude.pids()
    assert len(pids) == 2 and cs.gone(pids)                                  # the CLI and its grandchild: both gone


def test_nothing_starts_with_too_little_time_left(stub):
    with pytest.raises(clibrain.CliError) as e:
        clibrain.complete("claude", stub.claude.path, REQ, time.monotonic() + clibrain.MIN_CALL - 2)
    assert e.value.code == "no_time" and stub.claude.turns() == []


def test_a_flood_past_the_limit_is_refused_and_the_group_killed(stub):
    stub.claude.script({"flood": clibrain.MAX_OUT + 1024})
    with pytest.raises(clibrain.CliError) as e:
        clibrain.complete("claude", stub.claude.path, REQ, soon(30))
    assert e.value.code == "output_too_large"
    assert cs.gone(stub.claude.pids())


def test_stderr_is_never_read(stub):
    stub.claude.script({**turn_answer("unaffected"), "stderr": 1_000_000})
    assert clibrain.complete("claude", stub.claude.path, REQ, soon(30)).text == "unaffected"


def test_every_selftest_rejection_is_counted_and_nothing_is_retained(stub, tmp_path):
    """The self-test's stderr is a pipe in the same selector loop: a lot of it neither blocks the child nor breaks
    parsing, every router rejection is counted as its line arrives, and the collector keeps no text at all."""
    line = "2026-11-16T00:20:27.402598Z ERROR codex_core::tools::router: error=code-mode host is disabled\n"
    stub.codex.script({**cs.codex(codex_answer_event("fine")), "stderr_text": line * 2000})
    out, code, err = clibrain._run("codex", [str(stub.codex.path), "exec", "--json", "-"], b"hi", soon(30),
                                   tmp_path, stderr=True)
    assert code == 0 and err is not None and err.rejections == 2000
    assert not hasattr(err, "tail") and not hasattr(clibrain, "STDERR_TAIL")
    assert out and b"agent_message" in out


def test_a_grandchild_holding_stderr_open_does_not_hold_the_exchange(stub, tmp_path):
    """Once stdout is at EOF and the child has exited, buffered stderr is drained and the exchange returns: a
    grandchild that keeps the stderr pipe open must not turn a normal self-test answer into a timeout."""
    line = "2026-11-16T00:20:27.402598Z ERROR codex_core::tools::router: error=code-mode host is disabled\n"
    stub.codex.script({**cs.codex(codex_answer_event("fine")), "stderr_linger": True, "stderr_text": line * 3})
    started = time.monotonic()
    try:
        out, code, err = clibrain._run("codex", [str(stub.codex.path), "exec", "--json", "-"], b"hi", soon(30),
                                       tmp_path, stderr=True)
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"held stderr kept the exchange for {elapsed:.1f}s"
        assert code == 0 and err is not None and err.rejections == 3
        assert out and b"agent_message" in out
    finally:
        for pid in stub.codex.pids():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    assert cs.gone(stub.codex.pids())


def test_a_stderr_flood_after_the_child_exits_cannot_hold_the_exchange(stub, tmp_path):
    """The card's case: a grandchild keeps writing to the inherited stderr after the child exits, so the drain must
    stop at its byte cap (and the deadline) instead of reading until the writer stops. The child's own five router
    rejections are counted; the flood text matches none of them; the exchange returns quickly."""
    line = "2026-11-16T00:20:27.402598Z ERROR codex_core::tools::router: error=code-mode host is disabled\n"
    stub.codex.script({**cs.codex(codex_answer_event("fine")), "stderr_text": line * 5,
                       "stderr_flood": "helper: still writing\n", "sleep": 0.3})
    deadline = time.monotonic() + 6.0
    box: dict = {}

    def turn() -> None:
        box["got"] = clibrain._run("codex", [str(stub.codex.path), "exec", "--json", "-"], b"hi", deadline,
                                   tmp_path, stderr=True)

    started = time.monotonic()
    worker = threading.Thread(target=turn)
    worker.start()
    worker.join(3.0)
    if worker.is_alive():
        for pid in stub.codex.pids():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        worker.join()
    elapsed = time.monotonic() - started
    assert not worker.is_alive() and elapsed < 3.0, f"the stderr flood held the exchange for {elapsed:.1f}s"
    out, code, err = box["got"]
    assert code == 0 and err is not None and err.rejections == 5
    assert b"agent_message" in out
    assert 0 < err.drained <= clibrain.DRAIN_BYTES
    assert cs.gone(stub.codex.pids())


def test_the_drain_reads_at_most_its_cap_and_never_past_the_deadline():
    """The drain's two bounds, directly and without timing: an expired deadline reads nothing; a live one reads at
    most DRAIN_BYTES and stops with the endless writer still writing (the pipe is not empty after it returns)."""
    proc = subprocess.Popen(["/bin/sh", "-c", 'exec /usr/bin/yes "noise" >&2'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    sel = selectors.DefaultSelector()
    try:
        os.set_blocking(proc.stderr.fileno(), False)
        sel.register(proc.stderr, selectors.EVENT_READ)
        err = clibrain._Stderr()
        assert sel.select(10), "the writer never started"           # /usr/bin/yes takes a few ms to write its first
        assert clibrain._drain_stderr(sel, proc, err, time.monotonic() - 1.0) == 0
        assert err.rejections == 0
        got = clibrain._drain_stderr(sel, proc, err, time.monotonic() + 30.0)
        assert 0 < got <= clibrain.DRAIN_BYTES
        assert os.read(proc.stderr.fileno(), 1)
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        with contextlib.suppress(KeyError, ValueError):
            sel.unregister(proc.stderr)
        sel.close()
        proc.stderr.close()


# Bounded three ways so a SIGKILLed or timed-out pytest never leaves a runaway writer behind: at most 50 files in the
# dir, it quits within 100 writes once its parent is gone, and it stops after 120000 writes (about 10 s) if the parent
# hangs.
WRITER = ("i=0; while [ $i -lt 120000 ]; do [ $((i % 100)) -ne 0 ] || kill -0 $PPID 2>/dev/null || exit 0; "
          "echo x > f$((i % 50)); i=$((i+1)); done")


def test_the_dir_of_a_child_still_writing_when_it_is_killed_is_removed(tmp_path):
    """SIGKILL lands asynchronously, so a child can create one more file between the kill and the rmtree. The cleanup
    waits for the group to be gone first, so the dir does not survive that window (seen as a flake of the signal test
    below, which failed about once in a hundred loaded 4-worker runs)."""
    d = tmp_path / "hf-cli-race"
    d.mkdir()
    child = subprocess.Popen(["/bin/sh", "-c", WRITER], cwd=str(d),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not any(d.iterdir()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert any(d.iterdir()), "the child never started writing"
        with clibrain._LIVE_LOCK:
            clibrain._LIVE[child.pid] = d
        assert child.poll() is None, "the writer was already gone, so the kill race was not exercised"
        clibrain._cleanup_children()                   # the server's shutdown path: kill the group, remove the dir
        assert not d.exists(), "the dir the killed child was writing into survived the cleanup"
        assert cs.gone([child.pid])
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            child.wait(timeout=5)


PARENT = """import json, os, sys, tempfile, threading, time
sys.path.insert(0, {src!r})
from pathlib import Path
from hermes_finance import clibrain
exe, report = Path(sys.argv[1]), Path(sys.argv[2])
tmp = Path(tempfile.mkdtemp(prefix="hf-cli-"))

clibrain.install_signal_cleanup()   # what web.make_server does: handlers in the main thread

def announce():
    time.sleep(1.0)
    report.write_text(json.dumps({{"dir": str(tmp), "pid": os.getpid()}}))

threading.Thread(target=announce, daemon=True).start()

def turn():
    try:
        clibrain._run("claude", [str(exe), "-p"], b"", time.monotonic() + 60, tmp)
    except Exception:
        pass

worker = threading.Thread(target=turn)
worker.start()
worker.join()
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_to_the_server_kills_the_child_group_and_removes_its_dir(stub, tmp_path, sig):
    """The server-shaped path: handlers are installed in the main thread (as web.make_server does) before a worker
    thread runs the turn. SIGTERM or SIGINT to the server kills the child and its grandchild and removes the temp dir.
    The old test ran _run in the main thread, so the make_server install was never exercised."""
    stub.claude.script({"grandchild": True, "sleep": 30})
    report = tmp_path / "report.json"
    script = tmp_path / "parent.py"
    script.write_text(PARENT.format(src=str(Path(__file__).resolve().parents[1] / "src")))
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.Popen([sys.executable, str(script), str(stub.claude.path), str(report)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while not report.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert report.exists(), "the parent never started its child"
        [run] = stub.claude.records("start")
        [grand] = stub.claude.records("grandchild")
        tmpdir = Path(json.loads(report.read_text())["dir"])
        assert tmpdir.is_dir() and cs.alive(run["pid"]) and cs.alive(grand["child"])
        proc.send_signal(sig)
        # 30 s, not 10: this process runs under a background-QoS marker in a 4-worker suite and can be starved for
        # seconds at a time; the claim is that it exits on the signal and takes its child group and dir with it, not
        # how fast a throttled core gets to that line.
        status = proc.wait(timeout=30)
        if sig == signal.SIGTERM:
            assert status == -signal.SIGTERM
        assert cs.gone([run["pid"], grand["child"]])
        assert not tmpdir.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# --- the stored isolation check --------------------------------------------------------------------------------------

def test_ready_reports_the_one_code_a_turn_must_raise(stub, conn, monkeypatch):
    exe, why = clibrain.ready(conn, "claude")
    assert (exe, why) == (stub.claude.path, "not_checked") and stub.claude.records("probe") == []
    clibrain.save_check(conn, "claude", "2.1.286", False, "it used a tool")
    assert clibrain.ready(conn, "claude") == (stub.claude.path, "failed_check")
    clibrain.save_check(conn, "claude", "2.1.100", True)
    assert clibrain.ready(conn, "claude") == (stub.claude.path, "version_changed")
    clibrain.save_check(conn, "claude", "2.1.286", True)
    assert clibrain.ready(conn, "claude") == (stub.claude.path, "")
    monkeypatch.setenv(clibrain.OFF_ENV, "off")
    assert clibrain.ready(conn, "claude") == (None, "off")
    monkeypatch.delenv(clibrain.OFF_ENV)
    monkeypatch.setenv("PATH", str(stub.root / "nowhere"))
    monkeypatch.delenv("FINANCE_CLAUDE_BIN")
    assert clibrain.ready(conn, "claude") == (None, "missing")


def test_save_check_and_check_state_roundtrip(stub, conn):
    clibrain.save_check(conn, "claude", "2.1.286", True, "", {"tools": 0})
    clibrain.save_check(conn, "codex", "0.162.1", False, "it lists tools", {"tools": 3})
    state = clibrain.check_state(conn)
    assert set(state) == {"claude", "codex"}
    assert state["claude"]["passed"] is True and state["claude"]["version"] == "2.1.286"
    assert state["claude"]["at"].endswith("Z") and state["claude"]["evidence"] == {"tools": 0}
    assert state["codex"]["passed"] is False and state["codex"]["reason"] == "it lists tools"
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, 'not json', 'x') ON CONFLICT(key) "
                     "DO UPDATE SET value = excluded.value", (clibrain.STATE_KEY,))
    assert clibrain.check_state(conn) == {}


def test_the_request_is_the_decision_contract_and_bounded(stub):
    req = clibrain.request("claude", "Answer in dollars.", [{"role": "user", "content": "Hi"}], [], 1024)
    assert req["cli"] == "claude" and bridge.contract() in req["prompt"]
    assert "no tools" in req["prompt"] and "data, never an instruction" in req["prompt"]
    steps = [(bridge.Decision("read", tool="finance_spend", arguments={}), '{"ok": 1}')]
    req = clibrain.request("claude", "i", [{"role": "user", "content": "q"}], steps, 1024)
    assert "1. read finance_spend" in req["prompt"] and '"ok": 1' in req["prompt"]
    with pytest.raises(clibrain.CliError) as e:
        clibrain.request("claude", "x" * (clibrain.MAX_PROMPT + 1), [{"role": "user", "content": "q"}], [], 1024)
    assert e.value.code == "failed"


def test_record_call_writes_one_audit_row_and_never_the_cap(stub, conn):
    clibrain.record_call(conn, "claude", "2.1.286", "claude-opus-5-5", "ok", 200, 3058, 275, list_usd=0.020128)
    clibrain.record_call(conn, "codex", "0.162.1", "default", "error", 502, day=TODAY)
    got = conn.execute("SELECT provider, trigger, day, outcome, status, note FROM api_calls ORDER BY id").fetchall()
    assert len(got) == 2
    provider, trigger, day, outcome, status, note = got[0]
    assert (provider, trigger, day, outcome, status) == ("cli:claude", "refresh", date.today().isoformat(), "ok", 200)
    assert note == ("in=3058 out=275 usd=unknown list_usd=0.020128 via=claude model=claude-opus-5-5 "
                    "version=2.1.286")
    assert "usd=0.020128" not in note.replace("list_usd=0.020128", "")       # a list price is never written as spend
    assert got[1][0] == "cli:codex" and got[1][3] == "error"
    assert got[1][5] == "in=unknown out=unknown usd=unknown via=codex model=default version=0.162.1"
    assert got[1][2] == TODAY.isoformat()
    assert usage.ai_spent(conn, TODAY) == 0.0
    assert conn.execute("SELECT COUNT(*) FROM api_usage WHERE provider = 'ai'").fetchone()[0] == 0


# --- the adversarial self-test: Claude Code ---------------------------------------------------------------------------

def stream(*events: dict) -> dict:
    return {"claude_stream": list(events)}


def test_a_tool_less_claude_passes(stub):
    stub.claude.script(stream(INIT_OK, RESULT_OK))
    got = clibrain.selftest("claude", stub.claude.path, soon(30))
    assert got["passed"] is True, got
    assert got["evidence"] == {"tools": 0, "mcp": 0, "tool_events": 0, "canary": False, "answer": "valid"}
    [run] = stub.claude.turns()
    assert run["argv"] == LOCKDOWN
    assert "canary.txt" in run["stdin"] and bridge.contract() in run["stdin"]


@pytest.mark.parametrize("events,words", [
    ([{**INIT_OK, "tools": ["Read"]}, RESULT_OK], "tools"),
    ([{**INIT_OK, "mcp_servers": [{"name": "finance"}]}, RESULT_OK], "MCP"),
    ([INIT_OK, {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}, RESULT_OK], "tool"),
    ([{"type": "result", "result": json.dumps({"kind": "answer", "text": "fine"})}], "didn't report"),
    ([INIT_OK, {"type": "result", "result": "not a decision"}], "answer"),
])
def test_claude_that_isnt_isolated_fails_with_a_reason(stub, events, words):
    stub.claude.script(stream(*events))
    got = clibrain.selftest("claude", stub.claude.path, soon(30))
    assert got["passed"] is False and words in got["reason"]


def test_a_canary_leak_or_a_file_written_fails(stub):
    stub.claude.script({**stream(INIT_OK, RESULT_OK), "canary": True})
    got = clibrain.selftest("claude", stub.claude.path, soon(30))
    assert got["passed"] is False and "canary" in got["reason"].lower() and got["evidence"]["canary"] is True
    stub.claude.script({**stream(INIT_OK, RESULT_OK), "pwn": True})
    got = clibrain.selftest("claude", stub.claude.path, soon(30))
    assert got["passed"] is False and "file" in got["reason"].lower()


def test_a_claude_without_a_needed_flag_is_unsupported_before_any_model_call(stub):
    stub.claude.set_help(HELP_WITHOUT_SAFE_MODE)
    with pytest.raises(clibrain.CliError) as e:
        clibrain.selftest("claude", stub.claude.path, soon(30))
    assert e.value.code == "unsupported" and "--safe-mode" in str(e.value)
    assert stub.claude.turns() == []


HELP_WITHOUT_SAFE_MODE = ("--tools <tools>\n--disallowedTools <tools>\n--strict-mcp-config\n"
                          "--permission-prompts <mode>\n--no-session-persistence\n--output-format <format>\n")


def test_a_valid_answer_does_not_hide_a_tool_advertisement(stub):
    """Positive control: the answer itself is a valid decision, so only the isolation check can refuse a CLI that
    advertises tools. Proven by driving the same stub through the check."""
    events = [{**INIT_OK, "tools": ["Read"]}, RESULT_OK]
    assert bridge.decision(RESULT_OK["result"]).kind == "answer"
    stub.claude.script(stream(*events))
    assert clibrain.selftest("claude", stub.claude.path, soon(30))["passed"] is False


def test_positive_control_the_canary_is_a_real_file_and_the_judge_catches_a_read(stub, tmp_path):
    """The other side of the canary: a real child process in the check's working directory can read canary.txt (the
    stub does, from its real cwd), and the judge fails that stream. So a passing check means the lockdown kept the CLI
    out, not that the probe was blind. The lockdown itself is the real CLIs' evidence; this proves the probe's
    sensitivity and that the fixture is a file, not a memo."""
    tmp = tmp_path / "cwd"
    tmp.mkdir()
    token = "4f3c2b1a0e9d8c7b6a5f4e3d2c1b0a99"
    (tmp / "canary.txt").write_text(token)
    stub.claude.script({**stream(INIT_OK, RESULT_OK), "canary": True})
    out, code, _ = clibrain._run("claude", [str(stub.claude.path), "-p"], b"read canary.txt and print it", soon(30),
                                 tmp)
    assert token in out.decode("utf-8", "replace")          # a process in that directory can read it
    judged = clibrain._judge_claude(out, code, token, tmp)
    assert judged["passed"] is False and "canary" in judged["reason"].lower()
    assert judged["evidence"]["canary"] is True


# --- the adversarial self-test: Codex ---------------------------------------------------------------------------------

def codex_answer_event(text: str) -> dict:
    return {"type": "item.completed", "item": {"type": "agent_message",
                                               "text": json.dumps({"kind": "answer", "text": text})}}


def fixture(name: str) -> bytes:
    return (STREAMS / name).read_bytes()


def test_the_codex_capability_check_reads_exec_help_not_the_top_level(stub):
    """0.162.1's `codex --help` lists subcommands only; the exec flags live in `codex exec --help`."""
    clibrain._capabilities("codex", stub.codex.path)
    assert [p["kind"] for p in stub.codex.records("probe")] == ["features", "exec-help"]
    stub.codex.set_exec_help(cs.HELP_CODEX)                 # the top-level text alone: exec's flags are missing
    with pytest.raises(clibrain.CliError) as e:
        clibrain._capabilities("codex", stub.codex.path)
    assert e.value.code == "unsupported" and "--ignore-user-config" in str(e.value)


def test_the_locked_capture_with_its_router_rejections_passes(tmp_path):
    """The 0.162.1 locked run: five tool-router rejections on stderr, the startup notice, no tool item, no canary.
    The model's text is not judged: this same stream without stderr is inconclusive below."""
    got = clibrain._judge_codex(fixture("codex-locked-with-stderr.jsonl"), 0, CANARY, tmp_path,
                                fixture("codex-locked-with-stderr.stderr.txt"))
    assert got["passed"] is True, got
    assert got["evidence"] == {"rejections": 5, "tool_host": "disabled", "tool_items": 0, "canary": False,
                               "answer": "valid", "exit": 0}


def test_the_locked_stream_with_empty_stderr_is_inconclusive(tmp_path):
    got = clibrain._judge_codex(fixture("codex-locked-no-stderr.jsonl"), 0, CANARY, tmp_path, b"")
    assert got["passed"] is False and "inconclusive" in got["reason"]
    assert got["evidence"]["rejections"] == 0 and got["evidence"]["tool_items"] == 0
    assert got["evidence"]["tool_host"] == "disabled"


def test_a_model_report_alone_never_passes(tmp_path):
    """The forced-attempt stream reports every step failed, but no runtime rejection is on stderr: inconclusive."""
    got = clibrain._judge_codex(fixture("codex-forced-attempt.jsonl"), 0, CANARY, tmp_path, b"")
    assert got["passed"] is False and "inconclusive" in got["reason"]
    assert got["evidence"]["rejections"] == 0 and got["evidence"]["tool_items"] == 0


def test_the_unlocked_capture_fails_because_it_has_a_tool(tmp_path):
    """The positive control: real command_execution, mcp_tool_call and web_search items (each started and
    completed). Its only stderr line is a sandbox patch rejection, not a code-mode notice, so the tool items are
    what must fail it."""
    got = clibrain._judge_codex(fixture("codex-unlocked-positive-control.jsonl"), 0, CANARY, tmp_path,
                                fixture("codex-unlocked-positive-control.stderr.txt"))
    assert got["passed"] is False and "it has a tool" in got["reason"]
    assert got["evidence"]["tool_items"] == 6
    assert got["evidence"]["rejections"] == 0
    assert got["evidence"]["canary"] is True


def test_a_changed_canary_file_fails_the_check(tmp_path):
    (tmp_path / "canary.txt").write_text("not the token the check planted")
    got = clibrain._judge_codex(fixture("codex-locked-with-stderr.jsonl"), 0, CANARY, tmp_path,
                                fixture("codex-locked-with-stderr.stderr.txt"))
    assert got["passed"] is False and "file" in got["reason"].lower()


def test_the_real_plain_decision_is_inconclusive(tmp_path):
    got = clibrain._judge_codex(fixture("codex-plain-decision.jsonl"), 0, CANARY, tmp_path)
    assert got["passed"] is False and "inconclusive" in got["reason"]
    assert got["evidence"]["rejections"] == 0 and got["evidence"]["tool_host"] == "disabled"


def test_the_earlier_adversarial_report_is_inconclusive(tmp_path):
    got = clibrain._judge_codex(fixture("codex-locked-adversarial.jsonl"), 0, CANARY, tmp_path)
    assert got["passed"] is False and "inconclusive" in got["reason"]
    assert got["evidence"]["tool_items"] == 0


def test_a_tool_item_in_the_real_stream_fails(tmp_path):
    got = clibrain._judge_codex(fixture("codex-command-execution.jsonl"), 0, CANARY, tmp_path)
    assert got["passed"] is False and "it has a tool" in got["reason"]
    assert got["evidence"]["tool_items"] == 1


def test_the_canary_coming_back_in_the_real_stream_fails(tmp_path):
    got = clibrain._judge_codex(fixture("codex-forced-success.jsonl"), 0, CANARY, tmp_path)
    assert got["passed"] is False and "canary" in got["reason"]
    assert got["evidence"]["canary"] is True


def test_the_startup_notice_alone_is_inconclusive(tmp_path):
    got = clibrain._judge_codex(fixture("codex-startup-error-only.jsonl"), 0, CANARY, tmp_path)
    assert got["passed"] is False and "inconclusive" in got["reason"]
    assert got["evidence"]["tool_host"] == "disabled" and got["evidence"]["rejections"] == 0


def test_a_codex_selftest_reads_the_locked_captures_rejections(stub):
    events = [json.loads(line) for line in (STREAMS / "codex-locked-with-stderr.jsonl").read_text().splitlines()]
    stub.codex.script({**cs.codex(*events),
                       "stderr_text": fixture("codex-locked-with-stderr.stderr.txt").decode()})
    got = clibrain.selftest("codex", stub.codex.path, soon(30))
    assert got["passed"] is True, got
    assert got["evidence"]["rejections"] == 5
    [run] = stub.codex.turns()
    assert "MUST attempt" in run["stdin"] and "canary.txt" in run["stdin"]
    assert run["argv"][:3] == ["exec", "--sandbox", "read-only"]


def test_a_codex_selftest_without_rejections_is_inconclusive(stub):
    events = [json.loads(line) for line in (STREAMS / "codex-forced-attempt.jsonl").read_text().splitlines()]
    stub.codex.script(cs.codex(*events))
    got = clibrain.selftest("codex", stub.codex.path, soon(30))
    assert got["passed"] is False and "inconclusive" in got["reason"]
    assert got["evidence"]["rejections"] == 0


def test_the_unlocked_capture_is_refused_by_a_turn():
    """A turn is not the self-test: any item that isn't agent_message, reasoning or the startup notice refuses it."""
    with pytest.raises(clibrain.CliError) as e:
        clibrain._codex_reply(fixture("codex-unlocked-positive-control.jsonl"), "Codex")
    assert e.value.code == "failed"


def test_a_plain_decision_capture_is_a_turn():
    reply = clibrain._codex_reply(fixture("codex-plain-decision.jsonl"), "Codex")
    assert json.loads(reply.text)["kind"] == "read"


@pytest.mark.parametrize("event", [
    {"type": "item.started", "item": {"type": "command_execution", "command": "cat canary.txt"}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "cat canary.txt"}},
])
def test_a_codex_turn_with_a_tool_item_is_refused(stub, event):
    stub.codex.script(cs.codex(event, codex_answer_event("fine")))
    with pytest.raises(clibrain.CliError) as e:
        clibrain.complete("codex", stub.codex.path, REQ, soon())
    assert e.value.code == "failed"


@pytest.mark.parametrize("event", [
    {"type": "tool_call", "name": "shell"},
    {"type": "mcp_tool_call", "name": "finance_spend"},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "cat canary.txt"}},
])
def test_a_codex_tool_event_fails(stub, event):
    stub.codex.script(cs.codex(event, codex_answer_event("fine")))
    got = clibrain.selftest("codex", stub.codex.path, soon(30))
    assert got["passed"] is False and "tool" in got["reason"].lower()


def test_a_codex_that_is_not_supported_by_its_features_is_unsupported(stub):
    stub.codex.set_features("shell_tool\nunified_exec\n")
    with pytest.raises(clibrain.CliError) as e:
        clibrain.selftest("codex", stub.codex.path, soon(30))
    assert e.value.code == "unsupported" and stub.codex.turns() == []


def test_a_codex_canary_or_file_write_fails(stub):
    stub.codex.script({**cs.codex(codex_answer_event("fine")), "canary": True})
    got = clibrain.selftest("codex", stub.codex.path, soon(30))
    assert got["passed"] is False and "canary" in got["reason"].lower()
    stub.codex.script({**cs.codex(codex_answer_event("fine")), "pwn": True})
    got = clibrain.selftest("codex", stub.codex.path, soon(30))
    assert got["passed"] is False and "file" in got["reason"].lower()


def test_a_codex_login_failure_is_an_auth_error(stub):
    stub.codex.script(cs.codex({"type": "error", "message": "login required"}))
    with pytest.raises(clibrain.CliError) as e:
        clibrain.selftest("codex", stub.codex.path, soon(30))
    assert e.value.code == "auth"


# --- the module's own contract --------------------------------------------------------------------------------------

def test_every_error_code_has_one_line_and_a_cost(stub):
    for code in ("off", "missing", "not_checked", "version_changed", "failed_check", "unsupported", "auth",
                 "rate_limited", "timeout", "no_time", "garbled", "output_too_large", "failed"):
        e = clibrain.CliError(code, name="Claude Code", version="2.0.0", flag="--safe-mode")
        assert str(e) and "\n" not in str(e) and e.cost in ("none", "worst") and 400 <= e.status <= 504
    assert clibrain.line("missing", name="Codex").startswith("Codex isn't installed")


def test_the_reply_is_the_bridge_reply_plus_a_list_price():
    import dataclasses
    ours = [f.name for f in dataclasses.fields(clibrain.Reply)]
    assert ours == [f.name for f in dataclasses.fields(bridge.Reply)] + ["list_usd"]
    assert dataclasses.fields(clibrain.Reply)[-1].default is None


# --- the committed fixtures carry no capture leftovers ---------------------------------------------------------------

def test_the_committed_streams_carry_no_capture_leftovers():
    """The Claude fixture's account data, the real message id and socket path, every personal path, and the Codex
    capture's real plugin and skill inventory are gone (the README's scrubbing note names these paths, so only the
    event streams are scanned)."""
    text = "".join(p.read_text() for p in STREAMS.glob("*.jsonl"))
    assert "rate_limit_event" not in text, "the owner's rate-limit event remains"
    assert "/tmp/cc-socks" not in text, "the real messaging socket remains"
    assert not re.search(r'"id":\s*"msg_0', text), "a real message id remains"
    assert '"signature":"CAQ' not in text and '"signature": "CAQ' not in text
    assert "/Users/" not in text and "/private/var/" not in text and "/var/folders/" not in text
    assert "plugin_name" not in text and "skill_name" not in text, "the captured plugin inventory remains"
    assert "mcp.linear.app" not in text, "the real MCP connector URL remains"
    assert "openai.com/policies" not in text, "the real vendor policy URL remains"
    long_ids = set(re.findall(r"(?<![0-9a-f])[0-9a-f]{16,}(?![0-9a-f])", text))
    assert all(found == CANARY or found.startswith("0" * 16) for found in long_ids)
