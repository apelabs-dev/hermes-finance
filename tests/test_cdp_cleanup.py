"""FIX-CDP-LEAK: a headless Chrome (tests/cdp.py) never outlives the run that started it. On 2026-09-29, 52 Chrome trees
(356 processes, ~21 GB) from killed test / gate runs were found re-parented to launchd, some 9 days old.

Each test starts a real Chrome, in this process or in a child Python process that is then ended the hard way, and counts
the processes that still run on that Chrome's profile (every Chrome helper carries --user-data-dir). A test's own
Chrome is always killed in a `finally`, pass or fail, so a failing run can't leak one either.

CDP_UNDER_TEST=<path to a cdp.py> runs the same tests against another copy (the pre-fix one: `git show
969d1f7:tests/cdp.py`), which fails the SIGKILL and SIGTERM / SIGHUP ones."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cdp

HERE = Path(__file__).resolve().parent
CDP_FILE = Path(os.environ.get("CDP_UNDER_TEST") or HERE / "cdp.py").resolve()

pytestmark = pytest.mark.skipif(cdp.find_chrome() is None, reason="Chrome is not installed")

# A child that starts one Chrome, says where, and then waits to be signalled (or, with "exit", ends without close()).
# It starts from the dispositions of an interactive run, whatever this pytest inherited (nohup, a background job).
CHILD = """import importlib.util, json, signal, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
spec = importlib.util.spec_from_file_location("cdp_child", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
b = mod.Chrome()
print(json.dumps({"profile": b.profile, "pid": b.proc.pid}), flush=True)
if sys.argv[2] == "exit":
    sys.exit(0)
time.sleep(600)
"""


@pytest.fixture(scope="module")
def mod():
    if CDP_FILE == (HERE / "cdp.py").resolve():
        return cdp
    spec = importlib.util.spec_from_file_location("cdp_under_test", CDP_FILE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def on_profile(profile: str) -> list[tuple[int, int]]:
    """(pid, ppid) of every Chrome process running on `profile`."""
    flag = re.compile(r"--user-data-dir=" + re.escape(profile) + r"(\s|$)")
    out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True, check=True).stdout
    rows = [line.split(None, 2) for line in out.splitlines()]
    return [(int(r[0]), int(r[1])) for r in rows if len(r) == 3 and flag.search(r[2]) and "chrom" in r[2].split(" --")[0].lower()]


def settle(profile: str, seconds: float = 5) -> list[tuple[int, int]]:
    """on_profile() once it's empty, or after `seconds` (a SIGKILLed process takes a moment to be reaped)."""
    deadline = time.monotonic() + seconds
    while (left := on_profile(profile)) and time.monotonic() < deadline:
        time.sleep(0.1)
    return left


def kill_profile(profile: str | None) -> None:
    """The test's own clean-up, whatever cdp did: SIGKILL every process on the profile, then delete it."""
    if not profile:
        return
    for pid, _ in on_profile(profile):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    settle(profile)
    shutil.rmtree(profile, ignore_errors=True)


def start_child(mode: str = "wait") -> tuple[subprocess.Popen, dict]:
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(CDP_FILE), mode], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    line = child.stdout.readline()
    if not line:
        child.kill()
        child.wait()
        pytest.fail(f"the child didn't start a Chrome (exit {child.returncode}): {child.stderr.read()[-2000:]}")
    return child, json.loads(line)


def stop_child(child: subprocess.Popen | None) -> None:
    if child and child.poll() is None:
        child.kill()
    if child:
        child.wait()
        child.stdout.close()
        child.stderr.close()


def test_a_closed_chrome_leaves_no_process_and_no_profile(mod):
    with mod.Chrome() as b:
        profile = b.profile
        try:
            during = on_profile(profile)
            assert b.js("1 + 1") == 2
        except BaseException:
            kill_profile(profile)
            raise
    try:
        after = on_profile(profile)
        print(f"\nPROOF close: {len(during)} Chrome processes on {Path(profile).name} inside `with Chrome()`, "
              f"{len(after)} right after it")
        assert len(during) >= 2 and all(ppid == os.getpid() or ppid in {p for p, _ in during} for _, ppid in during)
        assert after == [], after
        assert not Path(profile).exists()
        b.close()   # idempotent: a second close is a no-op
        b.close()
    finally:
        kill_profile(profile)


def test_a_sigkilled_runs_chrome_is_reaped_by_the_next_chrome(mod, monkeypatch):
    """No handler runs on SIGKILL, so the Chrome outlives its owner, re-parented to launchd: the next Chrome() (here: this
    process's, told that everything is old enough and that it hasn't swept yet) kills it and deletes its profile."""
    child = profile = mine = None
    try:
        child, info = start_child()
        profile = info["profile"]
        alive = on_profile(profile)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(10)
        orphans = on_profile(profile)
        assert child.returncode == -signal.SIGKILL
        assert len(orphans) >= 2 and (info["pid"], 1) in orphans, orphans   # the whole tree, its main process now PPID 1
        monkeypatch.setattr(mod, "ORPHAN_AGE", 0, raising=False)
        monkeypatch.setattr(mod, "_swept", False, raising=False)
        with mod.Chrome() as b:
            mine = b.profile
            assert b.js("1 + 1") == 2
        after = settle(profile)
        print(f"\nPROOF sigkill: {len(alive)} Chrome processes on {Path(profile).name} with the child alive, "
              f"{len(orphans)} after SIGKILL (main pid {info['pid']} now PPID 1), {len(after)} after the next Chrome()")
        assert after == [], f"{len(after)} processes of the orphaned Chrome still run after the next Chrome(): {after}"
        assert not Path(profile).exists()
    finally:
        stop_child(child)
        kill_profile(profile)
        kill_profile(mine)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT], ids=["SIGTERM", "SIGHUP", "SIGINT"])
def test_a_signalled_run_takes_its_chrome_with_it(sig):
    """SIGTERM / SIGHUP: the handler closes every Chrome, then the child dies of the signal as usual (-15 / -1). SIGINT
    (Ctrl-C): Python's KeyboardInterrupt, then atexit."""
    child = profile = None
    try:
        child, info = start_child()
        profile = info["profile"]
        alive = on_profile(profile)
        os.kill(child.pid, sig)
        child.wait(30)
        after = settle(profile, 1)
        print(f"\nPROOF {sig.name}: {len(alive)} Chrome processes on {Path(profile).name} with the child alive, "
              f"{len(after)} after {sig.name} (child exit {child.returncode})")
        assert len(alive) >= 2
        assert after == [], f"{len(after)} Chrome processes outlived the child: {after}"
        assert not Path(profile).exists()
        assert child.returncode == -sig   # still the conventional death by that signal (a shell shows 128 + signum)
    finally:
        stop_child(child)
        kill_profile(profile)


def test_a_run_that_ends_without_close_takes_its_chrome_with_it():
    """atexit: the child starts a Chrome and exits without closing it."""
    child = profile = None
    try:
        child, info = start_child("exit")
        profile = info["profile"]
        child.wait(30)
        after = settle(profile, 1)
        assert child.returncode == 0
        assert after == [], f"{len(after)} Chrome processes outlived the child: {after}"
        assert not Path(profile).exists()
    finally:
        stop_child(child)
        kill_profile(profile)


def test_a_chrome_with_a_live_owner_is_never_reaped(mod):
    """A concurrent run's Chrome (its owner alive) survives the sweep, however old, and so does its profile."""
    if not hasattr(mod, "reap_orphans"):
        pytest.skip("this cdp.py has no reaper")
    child = profile = None
    try:
        child, info = start_child()
        profile = info["profile"]
        alive = on_profile(profile)
        mod.reap_orphans(age=0)
        assert set(alive) <= set(on_profile(profile)) and Path(profile).is_dir()   # (it may have started another helper)
        os.kill(child.pid, signal.SIGTERM)
        child.wait(30)
        assert settle(profile, 1) == []
    finally:
        stop_child(child)
        kill_profile(profile)


def test_a_leftover_profile_is_deleted_only_when_unused_and_old(tmp_path):
    """The sweep deletes an hf-cdp-* dir no process mentions once it's older than ORPHAN_AGE, and keeps a fresh one."""
    old, fresh, other = tmp_path / "hf-cdp-old00001", tmp_path / "hf-cdp-new00001", tmp_path / "not-a-profile"
    for d in (old, fresh, other):
        d.mkdir()
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    os.utime(other, (time.time() - 3600, time.time() - 3600))
    cdp.reap_orphans(tmp=tmp_path)
    assert not old.exists() and fresh.is_dir() and other.is_dir()


def test_a_profile_whose_owner_is_alive_is_never_swept(tmp_path):
    """TEST-SPEED: a sweep with age 0 (the tests above use one) keeps an old-looking profile no process mentions yet while
    the process it was made for is alive (another worker's Chrome() between making its profile and starting Chrome), and
    deletes one whose owner is gone."""
    mine, gone = tmp_path / "hf-cdp-mine0001", tmp_path / "hf-cdp-gone0001"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(10)
    for d, pid in ((mine, os.getpid()), (gone, dead.pid)):
        d.mkdir()
        (d / cdp.OWNER_FILE).write_text(str(pid))
        os.utime(d, (time.time() - 3600, time.time() - 3600))
    cdp.reap_orphans(age=0, tmp=tmp_path)
    assert mine.is_dir() and not gone.exists()


def test_ps_elapsed_times():
    assert cdp._seconds("00:07") == 7
    assert cdp._seconds("10:00") == 600
    assert cdp._seconds("01:02:03") == 3723
    assert cdp._seconds("9-00:00:01") == 9 * 86400 + 1
