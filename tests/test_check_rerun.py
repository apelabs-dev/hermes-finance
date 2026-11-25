"""Round 9: the browser tier's failed test is run once more, and the run names every test that needed it.

A browser test that fails once passes on the rerun and the run is green; one that fails twice keeps the run red; a
no-browser file is never rerun. Real pytest processes (no Chrome): the fixture files are plain pytest.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check  # noqa: E402

FAILS_ONCE = '''\
import os


def test_flaky_browser_test():
    count = os.environ["RERUN_COUNT"]
    seen = int(open(count).read()) if os.path.exists(count) else 0
    with open(count, "w") as fh:
        fh.write(str(seen + 1))
    assert seen >= 1, "first run only: the rerun sees its count"
'''

ALWAYS_FAILS = '''\
def test_broken_browser_test():
    assert False, "every run"
'''

PASSES = '''\
def test_a_no_browser_test():
    assert True
'''


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """The run's log dir; the marker a child pytest process counts its runs in; no inherited xdist worker name."""
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv("RERUN_COUNT", str(tmp_path / "runs"))
    (tmp_path / "logs").mkdir()
    return tmp_path / "logs"


def write(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body)
    return str(path)


def test_a_browser_test_that_fails_once_is_rerun_and_passes(tmp_path, logs):
    browser = write(tmp_path, "test_flaky.py", FAILS_ONCE)
    nodeid = f"{os.path.relpath(browser, ROOT)}::test_flaky_browser_test"
    run = check.Pytest("browser", [browser], logs, check.safe_env())
    result = run.wait()
    assert result["rc"] == 1 and result["failed"] == 1 and result["tests"] == 1, result
    assert check.failed_nodeids(run) == [nodeid]

    result = check.rerun_failed(run, result, logs, check.safe_env())
    assert result["failed"] == 0, result
    assert result["rerun"] == {"ran": [nodeid], "passed": [nodeid], "failed": [], "secs": result["rerun"]["secs"],
                               "complete": True}
    assert check.rerun_lines(result) == [f"rerun: passed on rerun: {nodeid}"]


def test_a_browser_test_that_fails_twice_stays_red(tmp_path, logs):
    browser = write(tmp_path, "test_broken.py", ALWAYS_FAILS)
    nodeid = f"{os.path.relpath(browser, ROOT)}::test_broken_browser_test"
    run = check.Pytest("browser", [browser], logs, check.safe_env())
    result = check.rerun_failed(run, run.wait(), logs, check.safe_env())
    assert result["failed"] == 1, result
    assert result["rerun"]["passed"] == [] and result["rerun"]["failed"] == [nodeid] and result["rerun"]["complete"]
    assert check.rerun_lines(result) == [f"rerun: failed twice: {nodeid}"]


def test_the_full_runner_counts_a_rerun_pass_as_passed_and_names_it(tmp_path, logs, capsys):
    cpu, browser = write(tmp_path, "test_plain.py", PASSES), write(tmp_path, "test_flaky.py", FAILS_ONCE)
    nodeid = f"{os.path.relpath(browser, ROOT)}::test_flaky_browser_test"
    args = check.parse_args(["full", "--part", "1"])
    assert check._run_existing(args, [], [cpu], [browser], logs, time.monotonic()) == 0
    out = capsys.readouterr().out
    assert f"rerun: passed on rerun: {nodeid}" in out, out
    assert f"rerun: 1 passed on rerun: {nodeid}" in out.splitlines()[-1], out
    assert "FULL (part 1): 2 tests" in out and ", 0 FAIL" in out.splitlines()[-1], out


def test_the_full_runner_keeps_a_rerun_failure_red_and_names_it(tmp_path, logs, capsys):
    cpu, browser = write(tmp_path, "test_plain.py", PASSES), write(tmp_path, "test_broken.py", ALWAYS_FAILS)
    nodeid = f"{os.path.relpath(browser, ROOT)}::test_broken_browser_test"
    args = check.parse_args(["full", "--part", "1"])
    assert check._run_existing(args, [], [cpu], [browser], logs, time.monotonic()) == 1
    out = capsys.readouterr().out
    assert f"rerun: failed twice: {nodeid}" in out, out
    assert f"rerun: 1 failed twice: {nodeid}" in out.splitlines()[-1], out
    assert ", 1 FAIL" in out.splitlines()[-1], out


def test_a_no_browser_failure_is_never_rerun(tmp_path, logs, capsys):
    flaky, passing = write(tmp_path, "test_flaky.py", FAILS_ONCE), write(tmp_path, "test_plain.py", PASSES)
    args = check.parse_args(["full", "--part", "1"])
    assert check._run_existing(args, [], [flaky], [passing], logs, time.monotonic()) == 1
    out = capsys.readouterr().out
    assert (tmp_path / "runs").read_text() == "1", "the no-browser file was run again"
    assert "passed on rerun" not in out and "failed twice" not in out, out
