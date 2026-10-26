"""Runs the demo AI worker's node:test suite (demo/worker/test/)."""

import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_demo_worker_node_tests():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.Popen(
        [node, "--test", "demo/worker/test/"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        pytest.fail("node --test timed out after 120 s")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert proc.returncode == 0, out[-4000:]


def test_worker_renders_date_tokens_like_the_build(tmp_path):
    """The worker gives the model the same plain dates demo_ask.render (and the shim) put on the page, shift included."""
    import sys
    import json
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    sys.path.insert(0, str(ROOT / "scripts"))
    import demo_ask
    texts = ["{{d:2026-10-26}} {{D:2026-02-28}} {{m:2026-01}} {{M:2026-12}}", "{{D:2026-12-31}} and {{m:2025-12}}"]
    shifts = [(0, 0), (1, 0), (-31, -1), (31, 2), (5, 1)]
    script = ("import('" + (ROOT / "demo/worker/src/index.js").as_uri() + "').then((w) => process.stdout.write(JSON.stringify("
              + json.dumps([[t, d, m] for t in texts for d, m in shifts])
              + ".map(([t, d, m]) => w.renderDates(t, {days: d, months: m})))))")
    r = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(r.stdout) == [demo_ask.render(t, d, m) for t in texts for d, m in shifts]
