"""TEST-SPEED: the headless Chrome budget with real Chromes, and the run's own record of them. Under a budget of 2 (the
machine's slot files, only the first two of them) three Chromes asked for at once never run more than two at a time,
and two do run at once (unless another process is seen holding one of those slots); with HF_CHROME_LEDGER set each
one's start and stop is written there (in the run's ledger as well: TEST-SPEED review F2), and once the ledger says
stopped, its whole process group is gone. And (review N1 / N3) Chrome() itself, with a stand-in browser in a child
process of its own (a private slot, a private $TMPDIR, no sweep of the machine's leftovers): a start whose browser dies
leaving a helper that holds its slot, and a real SIGTERM while it waits for a slot, makes its profile, registers or
connects, leave nothing behind. Review B6: the child says it is at that point from the point itself, once (the same
handshake and bounded reads as tests/test_cdp_ownership.py's child tests, which run without a browser), and the
parent checks the signal landed there (review round 3: where the start's thread is when its handling begins, its
stack looked at, not what it last said). Review round 4: and that the stand-in browser was started without the start's
SIGTERM / SIGHUP block (the signal mask it was given, which it keeps, where Chrome clears its own), and ended by the
stop's SIGTERM, not by SIGKILL after the grace. Skipped without Chrome."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import cdp
from cdp import Chrome, find_chrome
from test_cdp_ownership import HANDSHAKE, Lines   # (the child handshake, shared with the tests that run without Chrome)

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):   # (macOS: EPERM when only a reaped leader's zombie is left)
        return True
    return False


def test_three_chromes_under_a_budget_of_two_and_the_ledger_of_them(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.txt"
    run = os.environ.get("HF_CHROME_LEDGER")                       # check.py's, when it runs this (its watchdog's)
    monkeypatch.setenv("HF_CHROME_LEDGER", cdp.ledger_env(ledger))  # (review F2) ours as well, not instead
    monkeypatch.setattr(cdp, "SLOTS", 2)
    live, peak, lock, errors, others, done = [0], [0], threading.Lock(), [], [0], threading.Event()

    def watch():   # another process holding one of the two slots: seen 3 looks in a row (not one of ours starting)
        seen = 0
        while not done.is_set():
            with cdp._HELD_LOCK:
                mine = sum(cdp._HELD.values())
            seen = seen + 1 if cdp.slots_in_use() > mine else 0
            others[0] = max(others[0], seen)
            time.sleep(0.1)

    def work():
        try:
            with Chrome() as b:
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                assert b.js("1 + 1") == 2
                time.sleep(1.0)
                with lock:
                    live[0] -= 1
        except BaseException as e:   # (reported below: a thread's assert doesn't fail the test by itself)
            errors.append(repr(e))
    threads = [threading.Thread(target=work) for _ in range(3)]
    watcher = threading.Thread(target=watch, daemon=True)
    try:
        watcher.start()
        for t in threads:
            t.start()
    finally:
        for t in threads:
            if t.ident:
                t.join(120)
        stuck = {t.ident for t in threads if t.is_alive()}
        for c in [c for c in list(cdp._LIVE) if c.thread in stuck]:   # (review B6: its own Chromes closed, whatever happened)
            with contextlib.suppress(RuntimeError):
                c.close()
        for t in threads:
            if t.is_alive():
                t.join(30)
        done.set()
        watcher.join(5)
    assert not [t for t in threads if t.is_alive()], "a thread still running 120 s on (its Chrome closed)"
    # never 3 at once (they all start together and each keeps its Chrome 1 s); two at once, really, unless another
    # process was seen holding one of the two slots meanwhile (the machine's slots: a concurrent test's Chrome)
    assert not errors and 1 <= peak[0] <= 2 and live[0] == 0, (errors, peak, live)
    assert peak[0] == 2 or others[0] >= 3, f"never two Chromes at once (peak {peak[0]}), and no other holder seen"
    lines = [ln.split(None, 5) for ln in ledger.read_text().splitlines()]
    starts = {int(f[3]) for f in lines if f[0] == "start"}
    stops = {int(f[3]) for f in lines if f[0] == "stop"}
    assert len(starts) == 3 and starts == stops and {int(f[4]) for f in lines} == {os.getpid()}
    assert all(_group_gone(pg) for pg in starts)
    assert all(not os.path.exists(f[5]) for f in lines)   # their profiles went with them
    if run:   # (F2) and the run's ledger has all three, started and stopped
        theirs = [ln.split(None, 5) for ln in Path(run.split(os.pathsep)[0]).read_text().splitlines()]
        assert starts <= {int(f[3]) for f in theirs if f[0] == "start"} and stops <= {int(f[3]) for f in theirs if f[0] == "stop"}


ROOT = Path(__file__).resolve().parents[1]


def _procs(pgid: int, mark: str) -> list[int]:
    """The live processes of group `pgid` whose environment or command line names `mark` (ps -E: ours only)."""
    out = subprocess.run(["ps", cdp.PS_ENV, "-ww", cdp.PS_ALL, "-o", "pid=,pgid=,stat=,command="], capture_output=True, text=True).stdout
    return [int(f[0]) for f in (ln.split(None, 3) for ln in out.splitlines())
            if len(f) == 4 and f[1] == str(pgid) and not f[2].startswith("Z") and mark in f[3]]


def _marked(mark: str) -> list[int]:
    out = subprocess.run(["ps", cdp.PS_ENV, "-ww", cdp.PS_ALL, "-o", "pid=,stat=,command="], capture_output=True, text=True).stdout
    return [int(f[0]) for f in (ln.split(None, 2) for ln in out.splitlines())
            if len(f) == 3 and not f[1].startswith("Z") and f"HF_TEST_MARK={mark}" in f[2]]


def _free(slot: Path) -> bool:
    import fcntl
    fd = os.open(slot, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


# (review round 4) first thing, the stand-in writes the signal mask it was started with (a child's is its parent thread's,
# kept through exec) to <script>.mask, whole or not at all. Unlike Chrome, which clears it as it starts (REPORT-D), it
# keeps it, as any program that doesn't touch its mask does: a SIGTERM blocked by whatever started it stays blocked.
MASK = ("import os, signal\nwith open(sys.argv[0] + '.mask.new', 'w') as _f:\n"
        "    _f.write(' '.join(sorted(getattr(s, 'name', str(s)) for s in signal.pthread_sigmask(signal.SIG_BLOCK, []))))\n"
        "os.replace(sys.argv[0] + '.mask.new', sys.argv[0] + '.mask')\n")


def _fake_chrome(tmp_path, body: str) -> str:
    """A Python script standing in for Chrome that never writes DevToolsActivePort (and writes its mask: MASK)."""
    f = tmp_path / "fake-chrome"
    f.write_text(f"#!{sys.executable}\nimport subprocess, sys, time\n{MASK}{body}\n")
    f.chmod(0o755)
    return str(f)


# Chrome() in a child process of its own, on a thread (-main: on its main thread, another thread running); a SIGTERM
# handler of its own (installed first: cdp's runs before it) joins that thread and says how Chrome() ended.
# `what`: where it is when the test sends SIGTERM. It says so from there, once (HANDSHAKE: `ready <what>`), and waits
# there until the signal's handling has begun (`landed <what>`, from close_all(), when the start's thread is then in the
# function named with it; `landed elsewhere` when not); -die: no handler of its own, the process dies of the signal (a
# pytest worker's). -main: the handler can't run in there (it runs once _REGISTER is let go, still in Chrome.__init__):
# `arrived` says the signal came while the start was paused there (set_wakeup_fd). registration-main pauses at the
# registry (the start's signal block on, holding _REGISTER); review round 4, spawn-main in the browser's Popen (the
# block off, the mask this thread had before: what the browser is given; _REGISTER still held). The registration cases
# pause once their stand-in is up (its mask written). `rc`: how the browser ended (its return code once finished).
CHILD = HANDSHAKE + r"""
import json, select, signal, tempfile, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
tmp, what, fake = Path(sys.argv[3]), sys.argv[4], sys.argv[5]
tempfile.tempdir = str(tmp)   # its profiles: here
dies, what = what.endswith("-die"), what.removesuffix("-die")
out, th = {}, None

def prev(signum, frame):
    if what.endswith("-main"):   # (the start is this thread's: it goes on, and says how it ended)
        out["prev"] = True
        return
    th.join(15)
    out["joined"], out["landed"] = not th.is_alive(), landed[0]
    os.write(1, (json.dumps(out) + "\n").encode())
    os._exit(0)
if not dies:
    signal.signal(signal.SIGTERM, prev)
import cdp
cdp._swept = True             # no sweep of the machine's leftovers from a test
cdp.SLOT_DIR, cdp.SLOTS = tmp / "slots", 1
hook_close_all(cdp)
real_finish = cdp._finish
def finish(c):                # (round 4) how its browser ended: the stop's SIGTERM (-15), or SIGKILL after the grace (-9)
    why = real_finish(c)
    if not why:
        out["rc"] = c.proc.returncode
    return why
cdp._finish = finish
def browser_up():             # (round 4) its stand-in has started, and written the mask it was given
    deadline = time.monotonic() + 10
    while not os.path.exists(fake + ".mask") and time.monotonic() < deadline:
        time.sleep(0.01)
if what == "slot":            # its wait says it waits (at once)
    cdp.SLOT_WARN = -1
    sys.stderr = Stderr(sys.stderr)
elif what == "publication":   # just after making the profile, before its owner is written
    made = tempfile.mkdtemp
    def publishing(*a, **k):
        d = made(*a, **k)
        if at["where"] is None:
            ready("publication", "publishing")
            begun.wait(10)
        return d
    tempfile.mkdtemp = publishing
elif what in ("registration", "registration-main", "spawn-main"):   # the browser started, not yet in cdp's registry
    started = cdp.subprocess.Popen
    if what.endswith("-main"):   # (the signal's arrival, seen: the handler can't, or mustn't, run in there)
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
    def registering(*a, **k):   # registration, spawn-main: in the browser's Popen, once it is up
        p = started(*a, **k)
        if what != "registration-main" and a and a[0] and a[0][0] == fake and at["where"] is None:
            browser_up()
            if what == "spawn-main":
                ready(what, "Chrome.__init__")
                out["arrived"] = bool(select.select([wake_r], [], [], 10)[0])
            else:
                ready(what, "registering")
                begun.wait(10)
        return p
    cdp.subprocess.Popen = registering
    class Registry(set):        # registration-main: at the registry, the start's signal block back on
        def add(self, c):
            if what == "registration-main" and getattr(c, "path", None) == fake and at["where"] is None:
                browser_up()
                ready(what, "Chrome.__init__")
                out["arrived"] = bool(select.select([wake_r], [], [], 10)[0])
            return super().add(c)
    cdp._LIVE = Registry(cdp._LIVE)
elif what == "connect":       # in _connect's wait for DevTools (it asks _exited() each round)
    real_exited = cdp._exited
    def exited(proc):
        if at["where"] is None:
            ready("connect", "Chrome._connect")
        return real_exited(proc)
    cdp._exited = exited

def start():
    t = time.monotonic()
    try:
        with cdp.Chrome(chrome=fake):
            out["started"] = True
    except BaseException as e:
        out["err"], out["secs"] = str(e), time.monotonic() - t
if what.endswith("-main"):
    threading.Thread(target=threading.Event().wait, daemon=True).start()
    start()
    out["landed"] = landed[0]
    os.write(1, (json.dumps(out) + "\n").encode())
    sys.exit(0)
th = threading.Thread(target=start)
th.start()
if what == "exited":          # no signal: the browser dies by itself
    th.join(30)
    out["joined"] = not th.is_alive()
    os.write(1, (json.dumps(out) + "\n").encode())
    sys.exit(0)
while True:
    time.sleep(1)
"""


def _run_child(tmp_path, what: str, body: str) -> dict:
    """Run CHILD for `what` (SIGTERM once it says it's there, checked to land there), and check that nothing of it is
    left: its thread joined (or, -die, the process dead of the signal: resources checked, no join to see), no process
    of its stand-in browser (marked), no profile, its slot free, its ledger's starts all stopped. Every read is
    bounded; cleanup, whatever happened, kills only this test's own marked processes and the child."""
    import fcntl
    mark = f"budget-{os.getpid()}-{time.time_ns()}"
    tmp = tmp_path / "child"
    (tmp / "slots").mkdir(parents=True)
    held, child = None, None
    dies, what = what.endswith("-die"), what.removesuffix("-die")
    try:
        if what == "slot":   # the only slot is this test's: the child's Chrome() waits for it
            held = os.open(tmp / "slots" / "slot-0", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fake = _fake_chrome(tmp_path, body)
        with (tmp / "child.err").open("w") as err:   # (review round 3: closed here once the child has it)
            child = subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT / "tests"), str(ROOT / "src"), str(tmp),
                                      what + ("-die" if dies else ""), fake],
                                     stdout=subprocess.PIPE, stderr=err, start_new_session=True,
                                     env={**os.environ, "HF_CHROME_LEDGER": str(tmp / "ledger.txt"), "HF_TEST_MARK": mark})
        out = Lines(child)
        if what != "exited":
            assert out.line(30) == f"ready {what}"
            child.send_signal(signal.SIGTERM)
            assert out.line(10) == f"landed {what}"
        t0 = time.monotonic()
        if dies:   # no word from it: it died of the signal (once cdp's handler had stopped what it had)
            assert child.wait(30) == -signal.SIGTERM and time.monotonic() - t0 < 5
            res = {"died": True}
        else:
            res = json.loads(out.line(30))
            took = time.monotonic() - t0
            assert child.wait(30) == 0, res
        # (review round 4) the browser, where it was started, without the start's SIGTERM / SIGHUP block, and (a
        # SIGTERM case) ended by the stop's SIGTERM: not held off, SIGKILLed after the grace
        mask = Path(fake + ".mask")
        assert mask.exists() or what not in ("registration", "registration-main", "spawn-main"), "(its stand-in never came up)"
        blocked = set(mask.read_text().split()) if mask.exists() else set()
        assert not blocked & {"SIGTERM", "SIGHUP"}, f"({what}) its browser was started with {sorted(blocked)} blocked"
        if not dies:
            assert what == "exited" or "rc" not in res or res["rc"] == -signal.SIGTERM, \
                f"({what}) its browser ended by {res['rc']} (-9: SIGKILL, after the grace), not the stop's SIGTERM: {res}"
            assert what not in ("registration", "registration-main", "spawn-main", "connect") or "rc" in res, res
            assert (res.get("secs", 99) if what == "exited" else took) < 5, (what, took, res)   # at once, not in 30 s
            assert res.get("joined", True) is True and "started" not in res, res
            assert what == "exited" or res.get("landed") == what, res
            assert not what.endswith("-main") or res.get("arrived") is True, res
        deadline = time.monotonic() + 3
        while _marked(mark) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _marked(mark) == [], f"processes of the stand-in browser left: {_marked(mark)}"
        assert not list(tmp.glob("hf-cdp-*")), list(tmp.glob("hf-cdp-*"))
        if held is not None:
            os.close(held)
            held = None
        assert _free(tmp / "slots" / "slot-0")
        lines = [ln.split() for ln in (tmp / "ledger.txt").read_text().splitlines()] if (tmp / "ledger.txt").exists() else []
        assert {f[3] for f in lines if f[0] == "start"} == {f[3] for f in lines if f[0] == "stop"}, lines
        return res
    finally:
        for pid in _marked(mark):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(10)
            finally:
                child.stdout.close()   # (review round 3: its stderr went to a file, closed above: child.stderr is None)
        if held is not None:
            os.close(held)


def test_a_chrome_whose_browser_dies_leaving_a_helper_with_its_slot_fails_and_leaves_nothing(tmp_path):
    """Review N1: the browser starts a helper (which inherits the slot's descriptor) and exits before DevTools answers:
    Chrome() fails at once, and its helper, profile and slot don't outlive it."""
    helper = f"subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(120)'], close_fds=False)\nsys.exit(3)"
    res = _run_child(tmp_path, "exited", helper)
    assert "it exited: 3" in res.get("err", ""), res


@pytest.mark.parametrize("what", ["slot", "publication", "registration", "registration-main", "spawn-main", "connect",
                                  "publication-die", "connect-die"])
def test_a_sigterm_while_a_chrome_starts_leaves_nothing(what, tmp_path):
    """Review N3: a real SIGTERM while Chrome() waits for a slot, makes its profile, starts and registers its browser, or
    waits for DevTools: its start fails promptly, its thread ends, and nothing of it is left; and (-die) when the process
    then dies of the signal (a pytest worker's): its profile, made a moment before, isn't left behind either. Review B2
    (registration-main): the start on the main thread, holding _REGISTER, another thread taking the signal: its handler
    runs once _REGISTER is let go (it ran in there, and waited for _REGISTER for ever). Review round 4: its browser,
    started on the main thread, had the start's SIGTERM / SIGHUP block (a child's mask is its parent thread's): the
    stop's SIGTERM stayed pending in it, and it went only with SIGKILL, after the 5 s grace (5.2 s). spawn-main: the
    signal while the browser is being started, the mask this thread had before in place (what the browser is given)."""
    res = _run_child(tmp_path, what, "time.sleep(120)")
    if not what.endswith("-die"):
        want = "the process is stopping its Chromes" if what == "slot" else "Chrome did not start (stopped while starting)"
        assert want in res.get("err", ""), res
