"""A stand-in for `hermes finance completion` (bridge.py's child), for the fast tests of the Ask bridge's app side: an
executable script that speaks the bridge's protocol and answers from a script of steps, so the chat loop, its bounds
and its cleanup can be driven without Hermes. NOT Hermes: the real runtime (its plugin loader, PluginContext and
ctx.llm.complete_structured) is tests/test_bridge_runtime.py's.

The app runs it exactly as it runs Hermes (FINANCE_HERMES_BIN, the fixed argv, bridge.child_env's environment), so all
it knows comes from $HERMES_HOME, the one directory the test points it at: stub/script.json (the steps, taken one per
request, in order) and stub/records.jsonl (what each run got: its argv, pid, process group, environment names, request,
and when it started and ended). A step (a dict):

  text          an ok envelope with this model text (the decision), with `usage` if given
  envelope      this envelope (protocol added)
  raw           these stdout bytes, no envelope
  sleep         seconds to wait before answering
  exit          the exit code (default 0)
  flood         this many bytes to stdout, then the step's answer
  stderr        this many bytes to stderr
  grandchild    start a `sleep 300` in the same process group (its pid recorded)
  linger        the same, holding this process's stdout open
  no_stdin      don't read the request (it sleeps `sleep` seconds, then answers)
Without a step left it answers {"kind": "answer", "text": "(no script)"}."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from hermes_finance import bridge

STUB = r'''#!{python} -I
import fcntl, json, os, subprocess, sys, time
PROTOCOL = {protocol!r}
base = os.path.join(os.environ.get("HERMES_HOME", "/nonexistent"), "stub")
def record(event, **kw):
    with open(os.path.join(base, "records.jsonl"), "a") as f:
        f.write(json.dumps(dict(event=event, pid=os.getpid(), pgid=os.getpgid(0), t=time.monotonic(), **kw)) + "\n")
if sys.argv[1:] != ["finance", "completion"]:
    sys.stderr.write("hermes: error: argument command: invalid choice: %r\n" % sys.argv[1:2])
    sys.exit(2)
with open(os.path.join(base, "lock"), "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = os.path.join(base, "script.json")
    steps = json.load(open(path)) if os.path.exists(path) else []
    step = steps.pop(0) if steps else {{"text": json.dumps({{"kind": "answer", "text": "(no script)"}})}}
    json.dump(steps, open(path, "w"))
if step.get("unavailable"):
    sys.stderr.write("hermes: error: argument command: invalid choice: 'finance'\n")
    sys.exit(2)
raw = b"" if step.get("no_stdin") else sys.stdin.buffer.read()
try:
    req = json.loads(raw) if raw else None
except ValueError:
    req = "not json"
record("start", argv=sys.argv[1:], env=sorted(os.environ), environ={{k: os.environ[k] for k in os.environ}}, request=req)
if step.get("grandchild") or step.get("linger"):
    out = None if step.get("linger") else subprocess.DEVNULL
    g = subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL)
    record("grandchild", child=g.pid)
if step.get("stderr"):
    sys.stderr.buffer.write(b"e" * step["stderr"]); sys.stderr.flush()
time.sleep(step.get("sleep", 0))
if step.get("flood"):
    left = step["flood"]
    while left > 0:
        n = min(left, 65536); sys.stdout.buffer.write(b"x" * n); left -= n
    sys.stdout.buffer.write(b"\n")
if "raw" in step:
    sys.stdout.buffer.write(step["raw"].encode())
elif "envelope" in step:
    sys.stdout.write(json.dumps(dict(protocol=PROTOCOL, **step["envelope"])) + "\n")
elif "text" in step:
    usage = step.get("usage", {{"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050, "cost_usd": None}})
    sys.stdout.write(json.dumps(dict(protocol=PROTOCOL, ok=True, text=step["text"], provider="stub", model="stub-model",
                                     usage=usage)) + "\n")
sys.stdout.flush()
record("end")
sys.exit(step.get("exit", 0))
'''


def answer(text: str, **step: Any) -> dict[str, Any]:
    return {"text": json.dumps({"kind": "answer", "text": text}), **step}


def read(tool: str, arguments: dict[str, Any] | None = None, **step: Any) -> dict[str, Any]:
    return {"text": json.dumps({"kind": "read", "tool": tool, "arguments": arguments or {}}), **step}


def propose(tool: str, arguments: dict[str, Any], **step: Any) -> dict[str, Any]:
    return {"text": json.dumps({"kind": "propose", "tool": tool, "arguments": arguments}), **step}


def error(code: str, **step: Any) -> dict[str, Any]:
    return {"envelope": {"ok": False, "error": code}, **step}


class StubHermes:
    """The stand-in, installed for one test: FINANCE_HERMES_BIN and HERMES_HOME point at it (monkeypatch)."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.home = root / "hermes-home"
        self.base = self.home / "stub"
        self.base.mkdir(parents=True)
        self.exe = root / "hermes"
        self.exe.write_text(STUB.format(python=sys.executable, protocol=bridge.PROTOCOL))
        self.exe.chmod(self.exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("FINANCE_HERMES_BIN", str(self.exe))
        monkeypatch.setenv("HERMES_HOME", str(self.home))

    def script(self, *steps: dict[str, Any]) -> None:
        (self.base / "script.json").write_text(json.dumps(list(steps)))

    def left(self) -> list[dict[str, Any]]:
        path = self.base / "script.json"
        return json.loads(path.read_text()) if path.exists() else []

    def records(self, event: str | None = None) -> list[dict[str, Any]]:
        path = self.base / "records.jsonl"
        got = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [r for r in got if event is None or r["event"] == event]

    def requests(self) -> list[dict[str, Any]]:
        return [r["request"] for r in self.records("start")]

    def pids(self) -> list[int]:
        return [r["pid"] for r in self.records("start")] + [r["child"] for r in self.records("grandchild")]


def alive(pid: int) -> bool:
    """Whether `pid` is a live process (a zombie is not), asked of /bin/ps itself (this test's PATH may not be real)."""
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
