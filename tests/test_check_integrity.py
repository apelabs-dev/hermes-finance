"""Stale verdicts and real cross-process check contention, without running a suite."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check


@pytest.fixture
def tree(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init", "-b", "fixture")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "user.name", "Synthetic")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/a.py").write_text("original\n")
    git("add", ".")
    git("commit", "-m", "fixture")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    return tmp_path, git


@pytest.mark.parametrize("mutation", ["tracked", "untracked", "head", "delete"])
def test_midrun_mutation_fails_even_after_passing_tests(tree, monkeypatch, capsys, mutation):
    root, git = tree
    def run(args):
        if mutation == "tracked":
            (root / "src/a.py").write_text("changed\n")
        elif mutation == "untracked":
            (root / "tests/new.py").write_text("new\n")
        elif mutation == "delete":
            (root / "src/a.py").unlink()
        else:
            git("commit", "--allow-empty", "-m", "new HEAD")
        return 0
    monkeypatch.setattr(check, "_main", run)
    assert check.main(["--tier", "fast"]) == 1
    output = capsys.readouterr().out
    assert "STALE: tree changed mid-run (" in output
    assert output.rstrip().endswith("TIER fast (NOT a ship verdict)")


def test_fingerprint_detects_staged_and_untracked_content_but_ignores_evidence(tree):
    root, git = tree
    start = check.tree_fingerprint()
    (root / "evidence.log").write_text("evidence")
    assert check.tree_fingerprint() == start
    (root / "tests/new file.py").write_text("first")
    first = check.tree_fingerprint()
    assert first != start
    (root / "tests/new file.py").write_text("other")  # same length
    assert check.tree_fingerprint() != first
    (root / "tests/new file.py").unlink()
    (root / "src/a.py").write_text("staged")
    git("add", "src/a.py")
    assert check.tree_fingerprint() != start


@pytest.mark.parametrize("rc", [0, 7])
def test_stable_run_preserves_exit_status(tree, monkeypatch, capsys, rc):
    monkeypatch.setattr(check, "_main", lambda args: rc)
    assert check.main(["fast"]) == rc
    assert "STALE" not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["full"], ["--tier", "ship"], ["fast"], ["--tier", "fast"]])
def test_only_explicit_full_and_ship_lock(tree, monkeypatch, argv):
    entered = []
    @contextmanager
    def guard():
        entered.append("lock")
        yield
        entered.append("release")
    monkeypatch.setattr(check, "check_lock", guard)
    monkeypatch.setattr(check, "_main", lambda args: 0)
    assert check.main(argv) == 0
    assert entered == ([] if "fast" in argv else ["lock", "release"])


def test_fast_full_fallback_never_locks(tree, monkeypatch):
    monkeypatch.setattr(check, "check_lock", lambda: pytest.fail("fast must never lock"))
    monkeypatch.setattr(check, "tier_selection", lambda *args: ({}, ["infrastructure"]))
    monkeypatch.setattr(check, "_run_existing", lambda *args: 0)
    monkeypatch.setattr(check, "_main", lambda args: check._run_tier_fast(args, [], [], tree[0], 0))
    assert check.main(["--tier", "fast", "--files", "scripts/check.py"]) == 0


def test_real_lock_serializes_processes_and_prints_owner_immediately(tmp_path):
    path = tmp_path / "check.lock"
    prefix = f"import sys; sys.path.insert(0, {str(Path(check.__file__).parent)!r}); import check; from pathlib import Path\n"
    holder_code = prefix + f"with check.check_lock(Path({str(path)!r})):\n print('held', flush=True)\n sys.stdin.readline()\n"
    waiter_code = prefix + f"with check.check_lock(Path({str(path)!r})):\n print('entered', flush=True)\n"
    owned = []
    def line(proc):
        assert select.select([proc.stdout], [], [], 10)[0], "child produced no output"
        return proc.stdout.readline().strip()
    try:
        holder = subprocess.Popen([sys.executable, "-c", holder_code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        owned.append(holder)
        assert line(holder) == "held"
        owner = json.loads(path.read_text())
        assert owner == {"pid": holder.pid, "branch": subprocess.check_output(["git", "-C", str(check.ROOT), "branch", "--show-current"], text=True).strip(), "worktree": str(check.ROOT)}
        waiter = subprocess.Popen([sys.executable, "-c", waiter_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        owned.append(waiter)
        assert line(waiter) == f"waiting for check lock held by pid {holder.pid} ({owner['branch']}, {owner['worktree']})"
        assert waiter.poll() is None
        assert holder.communicate("release\n", timeout=10)[1] == ""
        out, err = waiter.communicate(timeout=10)
        assert waiter.returncode == 0 and out.strip() == "entered" and not err
        assert json.loads(path.read_text())["pid"] == waiter.pid
    finally:
        for proc in owned:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)


@pytest.mark.parametrize("metadata", ["incomplete metadata", "[]", "null"])
def test_wait_notice_repeats_after_60_seconds_and_bad_metadata_is_safe(tmp_path, monkeypatch, capsys, metadata):
    path = tmp_path / "lock"
    path.write_text(metadata)
    times = iter([1, 30, 61])
    attempts = iter([False, False, False, True])
    def flock(fd, operation):
        if operation != check.fcntl.LOCK_UN and not next(attempts):
            raise BlockingIOError
    monkeypatch.setattr(check.fcntl, "flock", flock)
    monkeypatch.setattr(check.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(check.time, "sleep", lambda _: None)
    with check.check_lock(path):
        pass
    assert capsys.readouterr().out.count("waiting for check lock held by pid unknown") == 2


def test_lock_releases_on_exception(tmp_path):
    path = tmp_path / "lock"
    with pytest.raises(RuntimeError), check.check_lock(path):
        raise RuntimeError("planted")
    with check.check_lock(path):
        assert json.loads(path.read_text())["pid"] == os.getpid()
