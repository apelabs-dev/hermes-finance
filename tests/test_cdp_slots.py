"""TEST-SPEED: tests/cdp.py's machine-wide headless Chrome budget, without starting a browser. The slot locks (one flocked
file per slot) bound how many are held at once across threads and processes; a slot goes with the process holding it
however that process ends; and a deadlock (every slot held by a thread that waits for another: a test keeping a browser
while it opens a second one, in several runs at once) fails one waiter within seconds instead of hanging, while an
ordinary wait (the slot's holder will give it back) is never taken for one."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import cdp

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def slots(tmp_path, monkeypatch):
    monkeypatch.setattr(cdp, "SLOT_DIR", tmp_path / "slots")   # (no browser here: a private budget of 2)
    monkeypatch.setattr(cdp, "SLOTS", 2)
    monkeypatch.setattr(cdp, "_HELD", {})
    return tmp_path / "slots"


class Holder:
    """What a browser does with its slot, without the browser: take one (waiting if need be), give it back."""

    def __init__(self):
        self.thread = threading.get_ident()
        self.slot = cdp._acquire_slot()

    def release(self):
        cdp._release_slot(self)


def test_at_most_slots_held_at_once_across_threads(slots):
    held, peak, lock = [0], [0], threading.Lock()

    def work():
        h = Holder()
        with lock:
            held[0] += 1
            peak[0] = max(peak[0], held[0])
        time.sleep(0.3)
        with lock:
            held[0] -= 1
        h.release()
    threads = [threading.Thread(target=work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert peak[0] == 2 and held[0] == 0 and cdp.slots_in_use() == 0


def test_a_slot_held_by_another_process_counts_and_goes_with_it_when_it_is_killed(slots):
    """The kernel's lock: a process killed outright (SIGKILL: nothing of its own runs) frees its slot all the same."""
    code = ("import sys, time; sys.path[:0] = [sys.argv[1], sys.argv[2]]; import cdp; fd = cdp._acquire_slot(); "
            "print('held', flush=True); time.sleep(60)")
    env = {**os.environ, "HF_CHROME_SLOT_DIR": str(slots), "HF_CHROMES": "2"}
    child = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "tests"), str(ROOT / "src")], env=env,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        assert cdp.slots_in_use() == 1
        mine = Holder()            # the other slot is free: taken at once
        assert cdp.slots_in_use() == 2
        child.send_signal(signal.SIGKILL)
        child.wait(10)
        deadline = time.monotonic() + 5
        while cdp.slots_in_use() != 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert cdp.slots_in_use() == 1   # only ours left
        mine.release()
        assert cdp.slots_in_use() == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)
        child.stdout.close()


def test_a_deadlock_fails_one_waiter_and_the_other_goes_on(slots):
    """Two threads each hold one of the 2 slots and each want a second: neither can ever get one. One of them raises
    within seconds; once it gives its slot back, the other gets it."""
    ready = threading.Barrier(2)
    out: dict[str, object] = {}

    def work(name):
        first = Holder()
        ready.wait()
        t0 = time.monotonic()
        try:
            second = Holder()
        except RuntimeError as e:
            out[name] = ("gave up", str(e), time.monotonic() - t0)
            first.release()
            return
        out[name] = ("got it", "", time.monotonic() - t0)
        second.release()
        first.release()
    threads = [threading.Thread(target=work, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), "the waiters hung"
    results = sorted(v[0] for v in out.values())
    assert results == ["gave up", "got it"], out
    gave_up = next(v for v in out.values() if v[0] == "gave up")
    assert "deadlock" in gave_up[1] and gave_up[2] < 10
    assert cdp.slots_in_use() == 0 and not list(slots.glob("wait-*"))


def test_an_ordinary_wait_is_not_a_deadlock(slots):
    """A thread holding a slot waits for a second while the other slot's holder isn't waiting (it will give it back in
    3 s): it waits, then gets it. A wait-* file left by a dead waiter (not flocked) changes nothing."""
    (slots).mkdir(exist_ok=True)
    (slots / "wait-999999-1").write_text("2")   # stale: nobody holds its lock
    other = Holder()
    got: list[float] = []

    def work():
        first = Holder()
        t0 = time.monotonic()
        second = Holder()
        got.append(time.monotonic() - t0)
        second.release()
        first.release()
    t = threading.Thread(target=work)
    t.start()
    time.sleep(3)
    other.release()
    t.join(20)
    assert got and 2.5 < got[0] < 10
    assert cdp.slots_in_use() == 0
