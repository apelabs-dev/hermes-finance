"""A stand-in for the Claude Code and Codex CLIs (clibrain.py's children), for the fast tests of the CLI brain's app
side: one executable script installed twice, once as `claude` and once as `codex`, selected by FINANCE_CLAUDE_BIN /
FINANCE_CODEX_BIN. It answers from a script of steps, one per model request, so a turn, the isolation self-test and
every bound can be driven without either real CLI (never run: the real login is expired and the quota is low).

Each install has its own directory (root/<name>/stub) with script.json (the steps, taken one per model request),
records.jsonl (what each run got: its argv, environment names and values, cwd, pid, process group, the stdin it
received, and when it started and ended), and the help/version/features text its probes answer from. A step (a dict):

  claude_stream   these events, one JSON line each (a turn's or the isolation self-test's stream-json)
  codex           these events, one JSON line each (the turn's / self-test's JSONL)
  raw             these stdout bytes, nothing else
  sleep           seconds to wait before answering
  exit            the exit code (default 0)
  flood           this many bytes to stdout, then the step's answer
  stderr          this many bytes to stderr
  stderr_text     this text to stderr (the Codex self-test's runtime rejection lines)
  stderr_flood    start a grandchild that floods this process's inherited stderr with this text (`yes <text> >&2`),
                  recorded like `grandchild`: the helper holds the pipe open and keeps writing
  grandchild      start a `sleep 300` in the same process group (its pid recorded)
  linger          the same, holding this process's stdout open
  stderr_linger   the same, with stderr inherited: the grandchild holds this process's stderr open
  no_stdin        don't read the request (it sleeps `sleep` seconds, then answers)
  canary          print the working directory's canary.txt first (a leak the self-test must catch)
  pwn             create pwned.txt in the working directory (a write the self-test must catch)

Without a step it answers one valid decision, {"kind": "answer", "text": "(no script)"}, in this CLI's own envelope.
`--version`, `--help`, `exec --help` and `features list` are probes, not step-taking model requests: they answer from
version.txt / help.txt / exec-help.txt / features.txt, and are recorded with event="probe"."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from hermes_finance import clibrain

STUB = r'''#!{python} -I
import fcntl, json, os, subprocess, sys, time
BASE = {base!r}
NAME = {name!r}
DEFAULT_VERSION = {version!r}
DEFAULT_HELP = {help_text!r}
DEFAULT_EXEC_HELP = {exec_help_text!r}
DEFAULT_FEATURES = {features_text!r}

def record(event, **kw):
    with open(os.path.join(BASE, "records.jsonl"), "a") as f:
        f.write(json.dumps(dict(event=event, name=NAME, pid=os.getpid(), pgid=os.getpgid(0), t=time.monotonic(),
                                **kw)) + "\n")

def text(name, default):
    path = os.path.join(BASE, name)
    return open(path).read() if os.path.exists(path) else default

def out(s):
    sys.stdout.write(s)
    sys.stdout.flush()

argv = sys.argv[1:]
if argv == ["--version"]:
    record("probe", kind="version", argv=argv)
    out(text("version.txt", DEFAULT_VERSION) + "\n")
    sys.exit(0)
if argv == ["--help"]:
    record("probe", kind="help", argv=argv)
    out(text("help.txt", DEFAULT_HELP) + "\n")
    sys.exit(0)
if argv == ["exec", "--help"]:
    record("probe", kind="exec-help", argv=argv)
    out(text("exec-help.txt", DEFAULT_EXEC_HELP) + "\n")
    sys.exit(0)
if argv[:2] == ["features", "list"]:
    record("probe", kind="features", argv=argv)
    out(text("features.txt", DEFAULT_FEATURES) + "\n")
    sys.exit(0)

with open(os.path.join(BASE, "lock"), "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = os.path.join(BASE, "script.json")
    steps = json.load(open(path)) if os.path.exists(path) else []
    step = steps.pop(0) if steps else {{}}
    json.dump(steps, open(path, "w"))
raw = b"" if step.get("no_stdin") else sys.stdin.buffer.read()
try:
    prompt = raw.decode("utf-8", "replace")
except Exception:
    prompt = repr(raw)
record("start", argv=argv, env=sorted(os.environ), environ={{k: os.environ[k] for k in os.environ}},
       cwd=os.getcwd(), stdin=prompt)
if step.get("grandchild") or step.get("linger") or step.get("stderr_linger"):
    sink = None if step.get("linger") else subprocess.DEVNULL
    err_sink = None if step.get("stderr_linger") else subprocess.DEVNULL
    g = subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL, stdout=sink, stderr=err_sink)
    record("grandchild", child=g.pid)
if step.get("stderr_text"):
    sys.stderr.buffer.write(step["stderr_text"].encode())
    sys.stderr.flush()
if step.get("stderr"):
    sys.stderr.buffer.write(b"e" * step["stderr"])
    sys.stderr.flush()
if step.get("stderr_flood"):
    g = subprocess.Popen(["/bin/sh", "-c", 'exec /usr/bin/yes "$0" >&2', step["stderr_flood"]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    record("grandchild", child=g.pid)
time.sleep(step.get("sleep", 0))
if step.get("canary"):
    try:
        out(open(os.path.join(os.getcwd(), "canary.txt")).read() + "\n")
    except OSError:
        pass
if step.get("pwn"):
    open(os.path.join(os.getcwd(), "pwned.txt"), "w").write("pwned\n")
if step.get("flood"):
    left = step["flood"]
    while left > 0:
        n = min(left, 65536)
        sys.stdout.buffer.write(b"x" * n)
        left -= n
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
decision = json.dumps({{"kind": "answer", "text": "(no script)"}})
if "raw" in step:
    out(step["raw"])
elif "claude_stream" in step:
    out("".join(json.dumps(e) + "\n" for e in step["claude_stream"]))
elif "codex" in step:
    out("".join(json.dumps(e) + "\n" for e in step["codex"]))
elif NAME == "codex":
    out(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": decision}}}}) + "\n")
    out(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}) + "\n")
else:
    out(json.dumps({{"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}}) + "\n")
    out(json.dumps({{"type": "result", "result": decision, "is_error": False, "usage": {{"input_tokens": 10,
                                                                                        "output_tokens": 5}}}}) + "\n")
record("end")
sys.exit(step.get("exit", 0))
'''

HELP_DIR = Path(__file__).resolve().parent / "fixtures" / "cli_help"
HELP_CLAUDE = (HELP_DIR / "claude-help.txt").read_text()
HELP_CODEX = (HELP_DIR / "codex-help.txt").read_text()
HELP_CODEX_EXEC = (HELP_DIR / "codex-exec-help.txt").read_text()
FEATURES_CODEX = (HELP_DIR / "codex-features.txt").read_text()
VERSION_CLAUDE = "2.1.286 (Claude Code)"
VERSION_CODEX = "codex-cli 0.162.1"


class StubCLI:
    """One installed stand-in (claude or codex): its path, its step queue and its records."""

    def __init__(self, root: Path, cli_id: str, version: str, help_text: str, exec_help_text: str = "",
                 features_text: str = ""):
        self.id = cli_id
        self.base = root / cli_id / "stub"
        self.base.mkdir(parents=True)
        self.path = root / cli_id / cli_id
        self.path.write_text(STUB.format(python=sys.executable, base=str(self.base), name=cli_id, version=version,
                                         help_text=help_text, exec_help_text=exec_help_text,
                                         features_text=features_text))
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.version_line = version

    def script(self, *steps: dict[str, Any]) -> None:
        (self.base / "script.json").write_text(json.dumps(list(steps)))

    def left(self) -> list[dict[str, Any]]:
        path = self.base / "script.json"
        return json.loads(path.read_text()) if path.exists() else []

    def set_version(self, text: str) -> None:
        (self.base / "version.txt").write_text(text)

    def set_help(self, text: str) -> None:
        (self.base / "help.txt").write_text(text)

    def set_exec_help(self, text: str) -> None:
        (self.base / "exec-help.txt").write_text(text)

    def set_features(self, text: str) -> None:
        (self.base / "features.txt").write_text(text)

    def records(self, event: str | None = None) -> list[dict[str, Any]]:
        path = self.base / "records.jsonl"
        got = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [r for r in got if event is None or r["event"] == event]

    def turns(self) -> list[dict[str, Any]]:
        """The model requests (a probe such as --version is not one)."""
        return self.records("start")

    def prompts(self) -> list[str]:
        return [r["stdin"] for r in self.turns()]

    def pids(self) -> list[int]:
        return [r["pid"] for r in self.turns()] + [r["child"] for r in self.records("grandchild")]


class StubCLIs:
    """Both stand-ins, installed for one test: FINANCE_CLAUDE_BIN and FINANCE_CODEX_BIN point at them."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.claude = StubCLI(root, "claude", VERSION_CLAUDE, HELP_CLAUDE)
        self.codex = StubCLI(root, "codex", VERSION_CODEX, HELP_CODEX, HELP_CODEX_EXEC, FEATURES_CODEX)
        monkeypatch.setenv("FINANCE_CLAUDE_BIN", str(self.claude.path))
        monkeypatch.setenv("FINANCE_CODEX_BIN", str(self.codex.path))
        monkeypatch.delenv(clibrain.OFF_ENV, raising=False)

    def __getitem__(self, cli_id: str) -> StubCLI:
        return self.claude if cli_id == "claude" else self.codex


def claude_turn(decision: dict[str, Any], init: dict[str, Any] | None = None, usage: dict[str, Any] | None = None,
                **fields: Any) -> dict[str, Any]:
    """A claude turn's stream: a system/init event (no tools, no MCP servers) and one result event."""
    return {"claude_stream": [
        init or {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []},
        {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(decision),
         "usage": usage or {"input_tokens": 1000, "output_tokens": 50}, **fields}]}


def answer(text: str, **fields: Any) -> dict[str, Any]:
    """A claude turn's decision with this answer text."""
    return claude_turn({"kind": "answer", "text": text}, **fields)


def read(tool: str, arguments: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    return claude_turn({"kind": "read", "tool": tool, "arguments": arguments or {}}, **fields)


def propose(tool: str, arguments: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return claude_turn({"kind": "propose", "tool": tool, "arguments": arguments}, **fields)


def claude_stream(*events: dict[str, Any]) -> dict[str, Any]:
    return {"claude_stream": list(events)}


def codex(*events: dict[str, Any]) -> dict[str, Any]:
    return {"codex": list(events)}


def codex_answer(text: str, **usage: Any) -> dict[str, Any]:
    return {"codex": [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(
        {"kind": "answer", "text": text})}}, {"type": "turn.completed", "usage": usage or {"input_tokens": 10,
                                                                                         "output_tokens": 5}}]}


def alive(pid: int) -> bool:
    """Whether `pid` is a live process (a zombie is not), asked of /bin/ps itself."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat_ = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat_) and not stat_.startswith("Z")


def gone(pids: list[int], within: float = 3.0) -> bool:
    """Every pid ends within `within` seconds (a SIGKILLed orphan is reaped by launchd, not at once)."""
    end = time.monotonic() + within
    while time.monotonic() < end:
        if not any(alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return not any(alive(p) for p in pids)
