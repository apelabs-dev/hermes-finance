"""scripts/check.py ci: what CI runs. The same no-browser file list as fast and full (never a second selection), plus the
release scan; it can't be narrowed."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check  # noqa: E402


def test_ci_is_a_mode_and_cant_be_narrowed():
    assert check.parse_args(["ci"]).mode == "ci"
    for extra in (["--part", "1"], ["--files", "src/x.py"], ["--kinds", "full"]):
        with pytest.raises(SystemExit):
            check.parse_args(["ci", *extra])
    with pytest.raises(SystemExit):
        check.parse_args(["ci", "--tier", "fast"])


def test_ci_plans_the_no_browser_list_and_the_release_scan():
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_XDIST_WORKER"}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check.py"), "ci", "--dry-run"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    cpu, _ = check.test_files()
    assert f"2) {len(cpu)} no-browser files" in r.stdout and "release scan" in r.stdout
    assert "tests/test_check_ci.py" in cpu and "tests/test_release_scan.py" in cpu
