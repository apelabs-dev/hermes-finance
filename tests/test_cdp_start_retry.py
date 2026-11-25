"""Round 9: a Chrome that dies before it answers — early, or SIGKILLed (exit -9) — is started again, up to twice.

A stand-in "Chrome" (a Python script, as tests/test_cdp_ownership.py uses) stands in for the browser, and
`Chrome._connect` is replaced by one that reports what the stand-in did, so the retry's own launch and stop path runs
for real: the slot, the fresh profile and the process group. A Chrome that starts but misbehaves (its process alive) is
never retried, and three dead launches raise.
"""
from __future__ import annotations

import fcntl
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

import cdp


@pytest.fixture
def private(tmp_path, monkeypatch):
    """A private slot dir, held-slot table and temp dir, no ledger, no sweep of the machine, no signal handlers."""
    monkeypatch.setattr(cdp, "SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(cdp, "SLOTS", 1)
    monkeypatch.setattr(cdp, "_HELD", {})
    monkeypatch.setattr(cdp, "_sweep_once", lambda: None)
    monkeypatch.setattr(cdp, "_install_handlers", lambda: None)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("HF_CHROME_LEDGER", raising=False)
    monkeypatch.delenv("HF_NO_CHROME", raising=False)
    monkeypatch.delenv("HF_CHROME_LOG", raising=False)
    (tmp_path / "slots").mkdir()
    return tmp_path


def stand_in(tmp_path: Path, body: str) -> tuple[str, Path]:
    """A Python script standing in for Chrome; it counts its launches in `tmp_path/launches`, then runs `body`."""
    script, count = tmp_path / "fake-chrome", tmp_path / "launches"
    script.write_text(f"""#!{sys.executable}
import os, signal, sys, time
COUNT = {str(count)!r}
n = int(open(COUNT).read()) if os.path.exists(COUNT) else 0
with open(COUNT, "w") as fh:
    fh.write(str(n + 1))
{body}
""")
    script.chmod(0o755)
    return str(script), count


def connect_expecting(tmp_path: Path, launches: int):
    """`_connect` for a stand-in: return once it has started `launches` times; an early exit is the real message."""
    count = tmp_path / "launches"

    def connect(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cdp._exited(self.proc):
                raise RuntimeError("Chrome did not start (it exited)")
            if count.exists() and int(count.read_text()) >= launches:
                return
            time.sleep(0.01)
        raise AssertionError(f"the stand-in never launched {launches} time(s)")
    return connect


def connect_after_start(tmp_path: Path):
    """`_connect` for a stand-in that is to misbehave: return after its first launch (the process alive), so the test
    is about a started browser, then raise is the caller's."""
    count = tmp_path / "launches"

    def connect(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cdp._exited(self.proc):
                raise RuntimeError("Chrome did not start (it exited)")
            if count.exists() and int(count.read_text()) >= 1:
                return
            time.sleep(0.01)
        raise AssertionError("the stand-in never launched")
    return connect


def slot_free(private: Path) -> bool:
    """Can the slot's lock be taken (held by no process, and no inherited descriptor)?"""
    fd = os.open(private / "slots" / "slot-0", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def nothing_left(private: Path, profile: str) -> bool:
    return (not Path(profile).exists() and list(private.glob(cdp.PROFILE_PREFIX + "*")) == []
            and cdp._naming(profile) == [] and slot_free(private))


def test_a_first_launch_killed_with_sigkill_is_started_again(private, monkeypatch):
    """Nightly 45648085672's "Chrome did not start (exited -9)": the second launch answers, and nothing of the first
    is left (its profile, its slot, its process group)."""
    fake, count = stand_in(private, "if n == 0:\n    os.kill(os.getpid(), signal.SIGKILL)\ntime.sleep(120)")
    monkeypatch.setattr(cdp.Chrome, "_connect", connect_expecting(private, 2))
    b = cdp.Chrome(fake)
    try:
        assert count.read_text() == "2"
        assert b.proc.poll() is None, "the second launch is not running"
        profile = b.profile
    finally:
        b.close()
    assert nothing_left(private, profile)


def test_three_dead_launches_raise_and_leave_nothing(private, monkeypatch):
    fake, count = stand_in(private, "os.kill(os.getpid(), signal.SIGKILL)")
    monkeypatch.setattr(cdp.Chrome, "_connect", connect_expecting(private, 99))
    profiles = set(private.glob(cdp.PROFILE_PREFIX + "*"))
    with pytest.raises(RuntimeError, match=r"Chrome did not start \(it exited: -9\)"):
        cdp.Chrome(fake)
    assert count.read_text() == "3", "the start was tried three times, no more"
    assert set(private.glob(cdp.PROFILE_PREFIX + "*")) == profiles == set(), "a failed launch left its profile"
    assert slot_free(private)


def test_a_chrome_that_starts_but_misbehaves_is_not_retried(private, monkeypatch):
    fake, count = stand_in(private, "time.sleep(120)")
    after_start = connect_after_start(private)

    def misbehaves(self):
        after_start(self)
        raise RuntimeError("DevTools answered garbage")

    monkeypatch.setattr(cdp.Chrome, "_connect", misbehaves)
    with pytest.raises(RuntimeError, match="garbage"):
        cdp.Chrome(fake)
    assert count.read_text() == "1", "a browser whose process is alive was launched again"
    profile = next(iter(private.glob(cdp.PROFILE_PREFIX + "*")), None)
    assert profile is None and slot_free(private)
