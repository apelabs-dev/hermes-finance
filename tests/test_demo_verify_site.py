"""scripts/demo_verify.Site: its http.server never outlives the process that started it (retro, 2026-10-26: one did,
for hours, after its demo_verify was killed before its finally could close it)."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHILD = """import sys, time
sys.path.insert(0, sys.argv[1])
import demo_verify
site = demo_verify.Site(__import__('pathlib').Path(sys.argv[2]))
print(site.port, site.proc.pid, flush=True)
time.sleep(600)
"""


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _alive(pid: int) -> bool:
    """Running, not merely a zombie: once the killed parent's children are reparented, a PID 1 that doesn't reap (a
    CI container without an init) leaves the dead server in the process table, and os.kill(pid, 0) still succeeds."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_the_server_goes_when_its_demo_verify_is_killed(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<p>demo</p>")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    parent = subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT / "scripts"), str(dist)], cwd=ROOT, env=env,
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        port, server = map(int, parent.stdout.readline().split())
        assert _listening(port)
        os.killpg(parent.pid, signal.SIGKILL)   # no finally, no close(): what a timeout or a crashed runner does
        parent.wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _listening(port):
            time.sleep(0.2)
        assert not _listening(port), "the http.server outlived its demo_verify"
        assert not _alive(server), "the http.server process is still running"
    finally:
        if parent.poll() is None:
            os.killpg(parent.pid, signal.SIGKILL)
        if "server" in locals():   # (this test never leaves one behind either)
            try:
                os.killpg(server, signal.SIGKILL)
            except ProcessLookupError:
                pass
