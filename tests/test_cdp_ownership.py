"""TEST-SPEED review corrections: tests/cdp.py's ownership of what a headless Chrome start leaves, without a browser and
without the constructor (no sweep of the machine's leftovers, no machine-wide slot). A stand-in is what a Chrome object
has once its browser is started: a private slot lock (passed down to the browser, as the real one is), a private
profile, and a small Python script as the browser, leading a process group of its own. Each test marks its processes
(HF_TEST_MARK in their environment) and its cleanup kills only those.

N1  a starting Chrome whose main process exits before DevTools answers, leaving a helper that holds its slot: the start
    fails, and no helper, profile or slot is left (its leader isn't reaped until its group is gone, so the group can
    still be signalled); a reaped leader's group id is never signalled (it may be another process's by now).
N2  a sweep running while a profile is being made (before its owner is written) keeps it.
N3  two stops of one Chrome at once stop it once; a real SIGTERM while a Chrome is connecting or waiting for a slot
    ends its start, its thread, its browser, its profile and its slot.
F2  a test's own ledger doesn't take the run's away (check.py's watchdog still stops those Chromes).
F5  goto() fails, within its deadline, on a navigation that fails, never commits, or never answers.
Round 2 (review f3e5e96):
B1  on the real WebSocket (DevTools: a loopback RFC 6455 stand-in): a deadline bounds every frame of every message and
    the sending, a connection torn by it is closed, one that isn't stays in use; a page never drawn fails READY's.
B2  a signal inside a stop, or inside close_all(), on the same thread: the Chrome finished once, no deadlock; a stop
    interrupted part-way is taken over and finished once; a wait for another thread's stop that runs out fails.
B3  a group still there after SIGKILL is not recorded stopped (and all of it is kept); once it goes, it is, once.
B4  the sweep signals an orphan's pid only while it is still that Chrome's.
B6  the child tests' handshake: said from the boundary itself, bounded reads, the signal checked to land there.
Round 3 (review 1301882):
B1  a message whose fragments are all buffered still ends by its deadline; one begun with an empty fragment is known
    open (torn on a deadline); frames out of order close the connection; many fragments are put together in linear time.
B2  a finishing interrupted part-way stays in _LIVE and is finished once by a later stop, close_all()'s included (no
    stop waiting to take it over), without a second socket close; one Chrome's failure leaves the others finished.
B3  a failed process listing is never taken for a Chrome gone.
B4  the sweep signals only the pids it verified, never a group.
B6  tests/test_cdp_budget.py's child harness ends its child and reports its own failure (stderr to a file: no
    Popen.stderr); the handshake's `landed` is where the start's thread is (its stack), not what it last said; a
    SIGTERM while a start talks to a stand-in DevTools (its WebSocket handshake, its first command) stops it.
Round 4 (review 7bf4d35):
C1  the sweep deletes a reaped Chrome's profile only once a fresh, whole look after its SIGKILLs finds nothing naming
    it (a helper it didn't signal, a refused or unverified one, a last look that failed: kept)."""

from __future__ import annotations

import fcntl
import importlib
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import cdp
from hermes_finance.cdp_client import WebSocket, WebSocketTimeout

ROOT = Path(__file__).resolve().parents[1]


def _mark() -> str:
    return f"own-{os.getpid()}-{time.time_ns()}"


def _procs(pgid: int, mark: str) -> list[int]:
    """The live (not zombie) processes of group `pgid` whose environment or command line names `mark`."""
    out = subprocess.run(["ps", cdp.PS_ENV, "-ww", cdp.PS_ALL, "-o", "pid=,pgid=,stat=,command="], capture_output=True, text=True).stdout
    return [int(f[0]) for f in (ln.split(None, 3) for ln in out.splitlines())
            if len(f) == 4 and f[1] == str(pgid) and not f[2].startswith("Z") and mark in f[3]]


def _kill_marked(pgid: int, mark: str) -> None:
    for pid in _procs(pgid, mark):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _gone(pgid: int, mark: str, secs: float = 5) -> bool:
    deadline = time.monotonic() + secs
    while _procs(pgid, mark) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _procs(pgid, mark)


def _free(slot: Path) -> bool:
    """Can the slot's lock be taken (nobody holds it: no process, and no inherited descriptor in a leftover helper)?"""
    fd = os.open(slot, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _fake(tmp_path: Path, name: str, body: str) -> str:
    """A Python script standing in for Chrome (it never writes DevToolsActivePort)."""
    f = tmp_path / name
    f.write_text(f"#!{sys.executable}\nimport os, subprocess, sys, time\n{body}\n")
    f.chmod(0o755)
    return str(f)


SLEEP = f"[{sys.executable!r}, '-c', 'import time; time.sleep(120)']"
HELPER_THEN_EXIT = f"subprocess.Popen({SLEEP}, close_fds=False)\nsys.exit(3)"   # a helper left running, the slot's descriptor with it
SLEEPS = "time.sleep(120)"


@pytest.fixture
def private(tmp_path, monkeypatch):
    """A private slot directory and held-slot table, no ledger unless a test names one."""
    monkeypatch.setattr(cdp, "SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(cdp, "SLOTS", 1)
    monkeypatch.setattr(cdp, "_HELD", {})
    monkeypatch.delenv("HF_CHROME_LEDGER", raising=False)
    (tmp_path / "slots").mkdir()
    return tmp_path


def standin(tmp: Path, fake: str, mark: str, slot: int = 0, args: tuple[str, ...] = ()):
    """What the constructor has made by the time it connects: its slot (held, and passed to the browser), its profile, the
    browser started in a session of its own, registered like the constructor registers it. `args`: the browser's, with
    {profile} in them filled in."""
    fd = os.open(tmp / "slots" / f"slot-{slot}", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    c = cdp.Chrome.__new__(cdp.Chrome)
    c.path, c._stopped, c.thread, c.slot = fake, False, threading.get_ident(), fd
    with cdp._HELD_LOCK:
        cdp._HELD[c.thread] = cdp._HELD.get(c.thread, 0) + 1
    c.profile = tempfile.mkdtemp(prefix=cdp.PROFILE_PREFIX, dir=tmp)
    c.proc = subprocess.Popen([fake, *(a.format(profile=c.profile) for a in args)], start_new_session=True, pass_fds=(fd,),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "HF_TEST_MARK": mark})
    c.pgid = c.proc.pid
    with cdp._REGISTER:
        cdp._LIVE.add(c)
        cdp._ledger("start", c)
    return c


def _cleanup(c, mark: str) -> None:
    _kill_marked(c.pgid, mark)
    cdp._LIVE.discard(c)
    if c.proc.returncode is None:
        c.proc.wait(10)
    if getattr(c, "slot", None) is not None:
        os.close(c.slot)
        c.slot = None


# --- N1 ------------------------------------------------------------------------------------------------------------------

def test_a_starting_chrome_whose_leader_exits_leaves_no_helper_profile_or_slot(private):
    mark = _mark()
    c = standin(private, _fake(private, "fake-chrome", HELPER_THEN_EXIT), mark)
    try:
        deadline = time.monotonic() + 10
        while len(_procs(c.pgid, mark)) < 1 and time.monotonic() < deadline:   # the helper is up
            time.sleep(0.05)
        with pytest.raises(RuntimeError, match="exited"):
            c._connect()
        c.close()   # (what the constructor does when _connect raises)
        assert _gone(c.pgid, mark, 2), f"a helper of the failed start is still running: {_procs(c.pgid, mark)}"
        assert _free(private / "slots" / "slot-0"), "the failed start's slot is still held (by its helper)"
        assert not Path(c.profile).exists() and c.proc.returncode == 3 and c not in cdp._LIVE
    finally:
        _cleanup(c, mark)


def test_a_reaped_leaders_group_id_is_never_signalled(private):
    """Once the leader is reaped its pid, the group's id, may belong to another process: a stranger leading a group of
    that id (stood in for here) is left alone, and the stop still finishes (profile, slot)."""
    mark = _mark()
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
                                env={**os.environ, "HF_TEST_MARK": mark})
    c = standin(private, _fake(private, "fake-chrome", "sys.exit(0)"), mark)
    c.proc.wait(10)                 # reaped (returncode set) ...
    c.pgid = stranger.pid           # ... and its id someone else's now
    try:
        cdp._stop([c], grace=0.5)
        assert stranger.poll() is None and _procs(stranger.pid, mark) == [stranger.pid]
        assert not Path(c.profile).exists() and _free(private / "slots" / "slot-0")
    finally:
        stranger.kill()
        stranger.wait(10)
        _cleanup(c, mark)


# --- N3 ------------------------------------------------------------------------------------------------------------------

def test_two_stops_at_once_stop_a_chrome_once(private, monkeypatch):
    """close_all() on a signal and a failed start's own close() at the same moment: each returns only once the Chrome is
    stopped, its slot's descriptor is closed once (a second close could hit a descriptor reused meanwhile), and the
    ledger says stopped once."""
    ledger = private / "ledger.txt"
    monkeypatch.setenv("HF_CHROME_LEDGER", str(ledger))
    mark = _mark()
    c = standin(private, _fake(private, "fake-chrome", SLEEPS), mark)
    fd, closes, real_close = c.slot, [], os.close

    def counting_close(n):
        # only the slot's own release: once it is closed its number is free, and a pipe or lock opened meanwhile (the
        # group-alive `ps`, the ledger's lock) can get it and be closed elsewhere: not a second close of the slot (CI
        # saw exactly that, both from one thread)
        if n == fd and sys._getframe(1).f_code is cdp._release_slot.__code__:
            closes.append(threading.get_ident())
        return real_close(n)
    monkeypatch.setattr(os, "close", counting_close)
    go, out = threading.Barrier(2), {}

    def stop(name):
        go.wait()
        try:
            cdp._stop([c], grace=2)
            out[name] = ("ok", _procs(c.pgid, mark), Path(c.profile).exists())
        except BaseException as e:   # (reported below)
            out[name] = ("raised", repr(e), None)
    threads = [threading.Thread(target=stop, args=(n,)) for n in ("a", "b")]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert not any(t.is_alive() for t in threads)
        assert out == {"a": ("ok", [], False), "b": ("ok", [], False)}, out
        assert len(closes) == 1, f"the slot descriptor was closed {len(closes)} times"
        stops = [ln for ln in ledger.read_text().splitlines() if ln.startswith("stop ")]
        assert len(stops) == 1, stops
    finally:
        monkeypatch.setattr(os, "close", real_close)
        _cleanup(c, mark)


class Lines:
    """A child's stdout, read a line at a time within a bound (review B6: a readline() that blocks for ever, before any
    timeout of the test's, bounds nothing)."""

    def __init__(self, proc: subprocess.Popen):
        self.proc, self.fd, self.buf = proc, proc.stdout.fileno(), b""

    def line(self, secs: float) -> str:
        deadline = time.monotonic() + secs
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                raise AssertionError(f"no line from the child in {secs} s (it hangs); so far {self.buf[-300:]!r}")
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise AssertionError(f"the child ended (exit {self.proc.poll()}) without a line; so far {self.buf[-300:]!r}")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode()


# The child's side of the handshake (review B6): the one line the parent waits for comes from the boundary itself,
# once (os.write: whole, unbuffered), and nothing else writes before the result; `landed` says where the start was
# when the SIGTERM's handling began (cdp's handler calls close_all() by name: a wrapper sees it first). Review round 3:
# where it *is* then: the start's thread's stack is looked at for its boundary (a function's qualified name), and
# "elsewhere" when it isn't in it (it said where the start last said it was: a signal that came once the start had
# left, or ended, still "landed" there).
HANDSHAKE = r"""
import os, sys, threading
at, landed, begun = {"where": None, "boundary": None, "ident": None}, [None], threading.Event()
def expect(where, boundary=None):   # this thread's start is to be in `boundary` when the signal's handling begins
    at.update(where=where, boundary=boundary, ident=threading.get_ident())
def ready(where, boundary=None):
    expect(where, boundary)
    os.write(1, f"ready {where}\n".encode())
def inside():
    if at["boundary"] is None:
        return True
    f = sys._current_frames().get(at["ident"])
    while f is not None:
        if f.f_code.co_qualname == at["boundary"]:
            return True
        f = f.f_back
    return False
def hook_close_all(cdp):
    real = cdp.close_all
    def close_all():
        landed[0] = at["where"] if at["where"] is not None and inside() else "elsewhere"
        os.write(1, f"landed {landed[0]}\n".encode())
        begun.set()
        return real()
    cdp.close_all = close_all
class Stderr:   # cdp's slot wait says it waits (SLOT_WARN): the thread has tried every slot, and waits
    def __init__(self, real): self.real = real
    def write(self, s):
        if "waiting for a headless Chrome slot" in s and at["where"] is None:
            ready("slot", "_acquire_slot")
        return self.real.write(s)
    def flush(self): self.real.flush()
"""

CHILD = HANDSHAKE + r"""
import fcntl, json, signal, subprocess, tempfile, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
tmp, what, mark = Path(sys.argv[3]), sys.argv[4], sys.argv[5]
out, th = {}, None

def prev(signum, frame):   # installed before cdp: its handler (close_all) runs first, then this one
    th.join(10)
    out["joined"], out["landed"] = not th.is_alive(), landed[0]
    os.write(1, (json.dumps(out) + "\n").encode())
    os._exit(0)
signal.signal(signal.SIGTERM, prev)
import cdp
cdp.SLOT_DIR, cdp.SLOTS = tmp / "slots", 1
hook_close_all(cdp)

def run(fn):
    global th
    def body():
        try:
            fn()
        except BaseException as e:
            out["err"] = str(e)
    th = threading.Thread(target=body)
    th.start()

if what.startswith("connect") or what in ("handshake", "enable"):
    fd = os.open(tmp / "slots" / "slot-0", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    c = cdp.Chrome.__new__(cdp.Chrome)
    c.path, c._stopped, c.thread, c.slot = sys.argv[6], False, threading.get_ident(), fd
    cdp._HELD[c.thread] = 1
    c.profile = tempfile.mkdtemp(prefix=cdp.PROFILE_PREFIX, dir=tmp)
    c.proc = subprocess.Popen([c.path, f"--user-data-dir={c.profile}", what], start_new_session=True, pass_fds=(fd,),
                              env={**os.environ, "HF_TEST_MARK": mark})   # (its stdout: this one's, the parent's pipe)
    c.pgid = c.proc.pid
    with cdp._REGISTER:
        cdp._LIVE.add(c)
        cdp._ledger("start", c)
    if what not in ("handshake", "enable"):   # (those: the stand-in's DevTools says when the start is there)
        real_exited = cdp._exited
        def exited(proc):   # _connect's wait for DevTools asks this each round: the first time, the start is in it
            if at["where"] is None:
                ready("connect", "Chrome._connect")
            return real_exited(proc)
        cdp._exited = exited
    def connect():
        if what in ("handshake", "enable"):
            expect(what, {"handshake": "WebSocket.__init__", "enable": "Chrome.call"}[what])
        if what == "connect-late":
            time.sleep(1.5)   # (a start slow to get there: the signal must still land in its connect)
        try:
            c._connect()
        except BaseException:
            c.close()   # (as the constructor does)
            raise
    run(connect)
else:   # "slot": the only slot is the parent's; this waits for it on a thread
    cdp.SLOT_WARN = -1   # (it says it waits at once)
    sys.stderr = Stderr(sys.stderr)
    run(cdp._acquire_slot)
while True:
    time.sleep(1)
"""


def _child(tmp: Path, what: str, mark: str, *extra: str, script: str = CHILD) -> subprocess.Popen:
    """(Review round 3: its stderr file closed here once the child has it; Popen.stderr is None for a file.)"""
    with (tmp / f"child-{what}.err").open("w") as err:
        return subprocess.Popen([sys.executable, "-c", script, str(ROOT / "tests"), str(ROOT / "src"), str(tmp), what,
                                 mark, *extra], stdout=subprocess.PIPE, stderr=err, start_new_session=True,
                                env={**os.environ, "HF_CHROME_LEDGER": str(tmp / "ledger.txt"), "HF_TEST_MARK": mark})


def _end_child(child: subprocess.Popen, mark: str) -> None:
    """Whatever happened: the test's own marked processes killed (the child's stand-in browser leads a group of its
    own), then the child, waited for within a bound; its pipes closed."""
    for pid in _marked(mark):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if child.poll() is None:
        child.kill()
    try:
        child.wait(10)
    finally:
        child.stdout.close()
        if child.stderr is not None:   # (a PIPE; None when it went to a file)
            child.stderr.close()


def _marked(mark: str) -> list[int]:
    """This test's live processes, wherever they are (HF_TEST_MARK in their environment: ps -E, this user's only)."""
    out = subprocess.run(["ps", cdp.PS_ENV, "-ww", cdp.PS_ALL, "-o", "pid=,stat=,command="], capture_output=True, text=True).stdout
    return [int(f[0]) for f in (ln.split(None, 2) for ln in out.splitlines())
            if len(f) == 3 and not f[1].startswith("Z") and f"HF_TEST_MARK={mark}" in f[2]]


@pytest.mark.parametrize("what", ["connect", "connect-late"])
def test_a_sigterm_while_a_chrome_is_connecting_stops_it_and_its_start_and_thread_end(what, private):
    """A real SIGTERM to a process whose Chrome start (on a thread) is waiting for DevTools: cdp's handler stops the browser
    (its whole group), deletes the profile and gives the slot back; the start fails at once, its thread ends; the
    ledger says started and stopped, once each. Review B6: the signal is sent once the start is in its wait (it says
    so from there), and lands there, even for a start slow to get there (connect-late)."""
    mark = _mark()
    child = _child(private, what, mark, _fake(private, "fake-chrome", SLEEPS))
    out = Lines(child)
    try:
        assert out.line(20) == "ready connect"
        pgid = int(next(f[3] for f in (ln.split() for ln in (private / "ledger.txt").read_text().splitlines())))
        assert _procs(pgid, mark) and not _free(private / "slots" / "slot-0")
        t0 = time.monotonic()
        child.send_signal(signal.SIGTERM)
        assert out.line(10) == "landed connect"
        res = json.loads(out.line(20))
        assert child.wait(20) == 0 and time.monotonic() - t0 < 10
        assert res.get("joined") is True and "did not start" in res.get("err", "") and res["landed"] == "connect", res
        assert _gone(pgid, mark, 2) and _free(private / "slots" / "slot-0")
        assert not list(private.glob(cdp.PROFILE_PREFIX + "*"))
        lines = [ln.split() for ln in (private / "ledger.txt").read_text().splitlines()]
        assert sorted(f[0] for f in lines) == ["start", "stop"] and {f[3] for f in lines} == {str(pgid)}, lines
    finally:
        _end_child(child, mark)


def test_a_sigterm_while_a_thread_waits_for_a_slot_ends_its_wait(private):
    mark = _mark()
    mine = os.open(private / "slots" / "slot-0", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(mine, fcntl.LOCK_EX | fcntl.LOCK_NB)   # the only slot: this test's
    child = _child(private, "slot", mark)
    out = Lines(child)
    try:
        assert out.line(20) == "ready slot"
        t0 = time.monotonic()
        child.send_signal(signal.SIGTERM)
        assert out.line(10) == "landed slot"
        res = json.loads(out.line(20))
        assert child.wait(20) == 0 and time.monotonic() - t0 < 5
        assert res.get("joined") is True and "stopping" in res.get("err", "") and res["landed"] == "slot", res
    finally:
        os.close(mine)
        _end_child(child, mark)
    assert _free(private / "slots" / "slot-0")


# --- B2: a signal inside a stop, or inside close_all(), on the main thread --------------------------------------------

# A stand-in in a child's main thread (a private slot, profile and ledger, a DevTools socket of its own), and handlers of
# the process's own for SIGTERM and SIGHUP that return (cdp's run first). Its browser ignores SIGTERM until the Nth.
# stop-*: the main thread is stopping it (a with-block's close()) when the parent's SIGTERM (then, -HUP, a SIGHUP inside
# the handler's own stop) comes. closing: the main thread is in close_all(), holding _REGISTER, when it comes.
STOP_CHILD = HANDSHAKE + r"""
import fcntl, json, signal, socket, subprocess, tempfile, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
tmp, what, mark, fake = Path(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6]
out = {"prev": []}
def prev(signum, frame):
    out["prev"].append(signal.Signals(signum).name)
signal.signal(signal.SIGTERM, prev)
signal.signal(signal.SIGHUP, prev)
import cdp
from hermes_finance.cdp_client import WebSocket
cdp.SLOT_DIR, cdp.SLOTS = tmp / "slots", 1
fd = os.open(tmp / "slots" / "slot-0", os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
c = cdp.Chrome.__new__(cdp.Chrome)
c.path, c._stopped, c.thread, c.slot = fake, False, threading.get_ident(), fd
cdp._HELD[c.thread] = 1
c.profile = tempfile.mkdtemp(prefix=cdp.PROFILE_PREFIX, dir=tmp)
up = tmp / "up"
c.proc = subprocess.Popen([fake, sys.argv[7], str(up)], start_new_session=True, pass_fds=(fd,),
                          env={**os.environ, "HF_TEST_MARK": mark})
c.pgid = c.proc.pid
t0 = time.monotonic()
while not up.exists() and time.monotonic() - t0 < 20:   # (its SIGTERM handler is in)
    time.sleep(0.02)
closes = {"ws": 0, "slot": 0}
ws = WebSocket.__new__(WebSocket)
ws.sock, peer = socket.socketpair()
ws.buf, ws.timeout, ws.broken = bytearray(), 60, None
ws_close = ws.close
def counted_ws_close():
    closes["ws"] += 1
    return ws_close()
ws.close = counted_ws_close
c.ws = ws
os_close, slot_ino = os.close, os.fstat(fd).st_ino
def counted_close(n):   # (the slot's file, not whatever has its descriptor's number since)
    try:
        if n == fd and os.fstat(n).st_ino == slot_ino:
            closes["slot"] += 1
    except OSError:
        pass
    return os_close(n)
os.close = counted_close
with cdp._REGISTER:
    cdp._LIVE.add(c)
    cdp._ledger("start", c)
if what == "closing":
    class Live(set):   # close_all() looking at _LIVE (holding _REGISTER): the parent's SIGTERM comes meanwhile
        def __iter__(self):
            if cdp._CLOSING and at["where"] is None:
                ready("closing")
                time.sleep(1.0)
            return super().__iter__()
    cdp._LIVE = Live(cdp._LIVE)
    cdp.close_all()   # (on the main thread: as atexit does)
    out["outer"] = "returned"
else:
    depth, said = [0], set()
    real_stop, real_alive = cdp._stop, cdp._group_alive
    def stop(chromes, grace=cdp.GRACE):
        depth[0] += 1
        try:
            return real_stop(chromes, grace)
        finally:
            depth[0] -= 1
    def alive(ch):   # a stop waiting for the group to go: says so, once per depth of stops
        if depth[0] not in said:
            said.add(depth[0])
            ready(f"stopping {depth[0]}")
        return real_alive(ch)
    cdp._stop, cdp._group_alive = stop, alive
    try:
        cdp._stop([c], grace=10)   # the stop the signal comes into (a with-block's close())
        out["outer"] = "returned"
    except Exception as e:
        out["outer"] = f"raised: {e}"
kinds = [ln.split()[0] for ln in (tmp / "ledger.txt").read_text().splitlines()]
out.update(closes, stops=kinds.count("stop"), starts=kinds.count("start"), stuck=kinds.count("stuck"),
           stopped=c._stopped, live=c in cdp._LIVE, profile=Path(c.profile).exists())
os.write(1, (json.dumps(out) + "\n").encode())
"""

COUNTING = ("import signal, sys, time\nn = []\n"
            "def term(*a):\n    n.append(1)\n    if len(n) >= int(sys.argv[1]):\n        sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, term)\nopen(sys.argv[2], 'w').close()\nwhile True:\n    time.sleep(0.05)")


@pytest.mark.parametrize("what, signals", [("stop", ("SIGTERM",)), ("stop", ("SIGTERM", "SIGHUP")),
                                           ("closing", ("SIGTERM",))])
def test_a_signal_inside_a_stop_on_the_same_thread_finishes_the_chrome_once(what, signals, private):
    """B2: a real SIGTERM while the main thread is stopping a Chrome (and, then, a SIGHUP inside that signal's own stop),
    the process's previous handlers returning (so the interrupted stop resumes): cdp's handler stops the Chrome, and the
    interrupted stop finds it stopped: one `stop` in the ledger, the DevTools socket closed once, the slot's descriptor
    closed once, nothing of the browser left. It finished it again (two stops, two closes). And a SIGTERM while the
    main thread is in close_all() (holding _REGISTER): handled once that's let go (it waited for _REGISTER there, for
    ever)."""
    mark = _mark()
    nth = {"stop": len(signals) + 1, "closing": 1}[what]   # (the browser ends at the SIGTERM of the innermost stop)
    child = _child(private, what, mark, _fake(private, "fake-chrome", COUNTING), str(nth), script=STOP_CHILD)
    out = Lines(child)
    try:
        for i, sig in enumerate(signals, start=1):
            want = f"ready stopping {i}" if what == "stop" else "ready closing"
            got = out.line(20)
            while got.startswith("ready stopping") and got != want:   # (a deeper stop's, not waited for here)
                got = out.line(20)
            assert got == want, got
            child.send_signal(getattr(signal, sig))
        got = out.line(30)
        while got.startswith(("ready ", "landed ")):
            got = out.line(30)
        res = json.loads(got)
        assert child.wait(20) == 0
        assert res["stops"] == 1 and res["starts"] == 1, f"the Chrome was recorded stopped {res['stops']} times: {res}"
        assert res["ws"] == 1 and res["slot"] == 1, f"its socket / slot descriptor closed {res['ws']} / {res['slot']} times"
        assert res["stopped"] and not res["live"] and not res["profile"] and res["outer"] == "returned", res
        assert sorted(res["prev"]) == sorted(signals), res   # (each signal still did what the process had it do)
        assert _marked(mark) == [], f"left running: {_marked(mark)}"
        assert _free(private / "slots" / "slot-0")
    finally:
        _end_child(child, mark)


def _waiting(ident: int, where: str = "_stop") -> bool:
    """Is thread `ident` blocked in an Event.wait() called from `where`?"""
    f = sys._current_frames().get(ident)
    names = []
    while f is not None:
        names.append(f.f_code.co_name)
        f = f.f_back
    if "wait" in names and where in names[names.index("wait"):]:
        return True
    # _finish holds _STOPPING while deleting the profile. A concurrent stop
    # blocks on that RLock before it can reach _done.wait(). Observe that
    # real blocking seam too, rather than waiting ten seconds for an
    # unreachable Event.wait and then calling the successful takeover slow.
    f = sys._current_frames().get(ident)
    while f is not None:
        if f.f_code is cdp._stop.__code__:
            import linecache
            if "_STOPPING" in linecache.getline(f.f_code.co_filename, f.f_lineno):
                return True
        f = f.f_back
    return False


def test_a_stop_interrupted_part_way_through_finishing_is_taken_over_and_finished_once(private, monkeypatch):
    """B2: thread A's stop is interrupted (an exception) part-way through finishing the Chrome, while thread B waits for
    it: B takes the stop over and finishes it, once (the stopped flag was set before the profile, the ledger's `stop`
    and the slot, so A's interruption left them undone, and B waited out its bound and returned as if it were done)."""
    ledger = private / "ledger.txt"
    monkeypatch.setenv("HF_CHROME_LEDGER", str(ledger))
    mark = _mark()
    c = standin(private, _fake(private, "fake-chrome", SLEEPS), mark)
    a, b, claimed, res = [], [], threading.Event(), {}
    real_signal, real_rmtree = cdp._signal_group, shutil.rmtree

    def signal_group(ch, sig):
        if a and threading.get_ident() == a[0]:
            claimed.set()
        return real_signal(ch, sig)

    def rmtree(path, *args, **kw):
        if a and threading.get_ident() == a[0] and "broke" not in res:
            deadline = time.monotonic() + 10
            while not (b and _waiting(b[0])) and time.monotonic() < deadline:   # B is waiting for this stop
                time.sleep(0.01)
            res["broke"] = True
            raise OSError("interrupted here (the test's)")
        return real_rmtree(path, *args, **kw)
    monkeypatch.setattr(cdp, "_signal_group", signal_group)
    monkeypatch.setattr(shutil, "rmtree", rmtree)

    def stop(me, name):
        me.append(threading.get_ident())
        t0 = time.monotonic()
        try:
            cdp._stop([c], grace=2)
            res[name] = "returned"
        except BaseException as e:
            res[name] = f"raised: {e!r}"
        res[name + "-secs"] = time.monotonic() - t0
    ta = threading.Thread(target=stop, args=(a, "a"))
    tb = threading.Thread(target=stop, args=(b, "b"))
    try:
        ta.start()
        assert claimed.wait(10)
        tb.start()
        ta.join(30)
        tb.join(30)
        assert not ta.is_alive() and not tb.is_alive()
        assert res.get("broke") and res["a"].startswith("raised") and "interrupted here" in res["a"], res
        stops = [ln for ln in ledger.read_text().splitlines() if ln.startswith("stop ")]
        assert len(stops) == 1, f"recorded stopped {len(stops)} times (B returned after {res['b-secs']:.1f} s): {res}"
        assert res["b"] == "returned" and res["b-secs"] < 10, res
        assert not Path(c.profile).exists() and _free(private / "slots" / "slot-0") and c.slot is None
        assert c._stopped and c not in cdp._LIVE and _gone(c.pgid, mark, 2)
    finally:
        monkeypatch.setattr(shutil, "rmtree", real_rmtree)
        _cleanup(c, mark)


def test_a_wait_for_another_threads_stop_that_runs_out_fails(private, monkeypatch):
    """B2: thread A's stop of a Chrome is stuck (held here in its wait for the group); B's stop of the same Chrome waits
    grace + STOP_WAIT for it, then fails (it returned as if the Chrome were stopped)."""
    monkeypatch.setattr(cdp, "STOP_WAIT", 1.0, raising=False)
    mark = _mark()
    c = standin(private, _fake(private, "fake-chrome", SLEEPS), mark)
    a, hold, held, res = [], threading.Event(), threading.Event(), {}
    real_alive = cdp._group_alive

    def alive(ch):
        if a and threading.get_ident() == a[0]:
            held.set()
            hold.wait(60)
        return real_alive(ch)
    monkeypatch.setattr(cdp, "_group_alive", alive)

    def stop_a():
        a.append(threading.get_ident())
        try:
            cdp._stop([c], grace=0.2)
            res["a"] = "returned"
        except BaseException as e:
            res["a"] = f"raised: {e!r}"
    ta = threading.Thread(target=stop_a)
    try:
        ta.start()
        assert held.wait(10)
        t0 = time.monotonic()
        try:
            cdp._stop([c], grace=0.2)
            res["b"] = "returned"
        except RuntimeError as e:
            res["b"] = f"raised: {e}"
        took = time.monotonic() - t0
        assert res["b"].startswith("raised") and "another thread" in res["b"], \
            f"B's stop {res['b']} after {took:.1f} s while A's stop still had the Chrome (stopped: {c._stopped})"
        assert took < 5 and not c._stopped
    finally:
        hold.set()
        ta.join(30)
        _cleanup(c, mark)
    assert res.get("a") == "returned" and c._stopped, res


# --- B3: a stop records the Chrome stopped only once its group is seen gone -------------------------------------------

DEAF = ("import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nopen(sys.argv[1], 'w').close()\n"
        "while True:\n    time.sleep(0.05)")


def test_a_group_still_there_after_sigkill_is_not_recorded_stopped(private, monkeypatch):
    """B3: a browser deaf to SIGTERM whose SIGKILL doesn't take within the bound (as for a process stuck in the kernel:
    withheld here): the stop records no `stop`, says it failed, and keeps all of it (the leader unreaped, the profile,
    the slot, the ledger's open start, its place in _LIVE). Once the SIGKILL takes, an ordinary stop finishes it, once.
    (It waited 5 s, then recorded it stopped, deleted the profile and gave its slot descriptor back.)"""
    ledger = private / "ledger.txt"
    monkeypatch.setenv("HF_CHROME_LEDGER", str(ledger))
    monkeypatch.setattr(cdp, "KILL_WAIT", 0.5, raising=False)
    mark, up = _mark(), private / "up"
    fd = os.open(private / "slots" / "slot-0", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    c = cdp.Chrome.__new__(cdp.Chrome)
    c.path, c._stopped, c.thread, c.slot = "deaf", False, threading.get_ident(), fd
    cdp._HELD[c.thread] = 1
    c.profile = tempfile.mkdtemp(prefix=cdp.PROFILE_PREFIX, dir=private)
    c.proc = subprocess.Popen([_fake(private, "fake-chrome", DEAF), str(up)], start_new_session=True, pass_fds=(fd,),
                              env={**os.environ, "HF_TEST_MARK": mark})
    c.pgid = c.proc.pid
    with cdp._REGISTER:
        cdp._LIVE.add(c)
        cdp._ledger("start", c)
    real_killpg, withheld = os.killpg, []

    def killpg(pg, sig):
        if pg == c.pgid and sig == signal.SIGKILL and not withheld[-1:] == ["released"]:
            withheld.append(sig)
            return
        return real_killpg(pg, sig)
    try:
        deadline = time.monotonic() + 20
        while not up.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        monkeypatch.setattr(os, "killpg", killpg)
        try:
            cdp._stop([c], grace=0.3)
            outcome = "returned"
        except RuntimeError as e:
            outcome = f"raised: {e}"
        kinds = [ln.split()[0] for ln in ledger.read_text().splitlines()]
        assert withheld, "(the test never saw the stop's SIGKILL)"
        assert "stop" not in kinds, f"recorded stopped while its group still runs ({_procs(c.pgid, mark)}): {kinds}"
        assert outcome.startswith("raised") and "not stopped" in outcome, outcome
        assert kinds.count("stuck") == 1 and _procs(c.pgid, mark), kinds
        assert Path(c.profile).exists() and c.slot is not None and c in cdp._LIVE and c.proc.returncode is None
        assert not c._stopped
        withheld.append("released")   # the SIGKILL takes from now on
        cdp._stop([c], grace=0.3)
        kinds = [ln.split()[0] for ln in ledger.read_text().splitlines()]
        assert kinds.count("stop") == 1 and _gone(c.pgid, mark, 2), kinds
        assert not Path(c.profile).exists() and c.slot is None and c not in cdp._LIVE and c._stopped
        assert _free(private / "slots" / "slot-0")
    finally:
        monkeypatch.setattr(os, "killpg", real_killpg)
        _cleanup(c, mark)


# --- N2 ------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def no_ps(monkeypatch):
    """reap_orphans() without its `ps`: no process of the machine is looked at (or killed) here, only the profiles. (Review
    round 3: a listing of this process alone, as a ps seeing nothing else would give; an empty one is a failed ps.)"""
    real = subprocess.run

    def run(cmd, *a, **k):
        if cmd[:1] == ["ps"]:
            return subprocess.CompletedProcess(cmd, 0, f"{os.getpid()}\n", "")
        return real(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize("sweep_waits", [False, True])
def test_a_sweep_while_a_profile_is_being_made_keeps_it(sweep_waits, tmp_path, monkeypatch, no_ps):
    """The profile is made (mkdtemp) and paused there, before its owner is written; an age-0 sweep of the same temp dir
    runs meanwhile (and either gives up on the pause or waits it out); then the start resumes: the profile is still
    there, with its owner, and later sweeps keep it while its owner lives."""
    made, resume, real = threading.Event(), threading.Event(), tempfile.mkdtemp

    def paused(*a, **k):
        d = real(*a, **k)
        made.set()
        resume.wait(10)
        return d
    monkeypatch.setattr(tempfile, "mkdtemp", paused)
    got: dict = {}
    maker = threading.Thread(target=lambda: got.setdefault("profile", cdp._new_profile(tmp_path)))
    maker.start()
    try:
        assert made.wait(10)
        sweep = threading.Thread(target=lambda: got.setdefault("swept", cdp.reap_orphans(age=0, tmp=tmp_path)))
        sweep.start()
        if sweep_waits:
            time.sleep(0.3)
            resume.set()
        sweep.join(20)
        assert not sweep.is_alive()
        resume.set()
        maker.join(20)
        profile = Path(got["profile"])
        assert profile.is_dir() and (profile / cdp.OWNER_FILE).read_text() == str(os.getpid()), got
        cdp.reap_orphans(age=0, tmp=tmp_path)
        assert profile.is_dir()
    finally:
        resume.set()
        maker.join(20)


def test_a_profile_left_without_an_owner_is_still_swept(tmp_path, no_ps):
    """(The control: a leftover hf-cdp-* dir nobody is making, no owner in it, is deleted by an age-0 sweep.)"""
    left = tmp_path / "hf-cdp-left0001"
    left.mkdir()
    cdp.reap_orphans(age=0, tmp=tmp_path)
    assert not left.exists()


def test_the_sweep_signals_an_orphans_pid_only_while_it_is_still_that_chrome(tmp_path, monkeypatch):
    """B4: the sweep's `ps` sees two orphaned Chromes (no parent but launchd, old enough). One is real (a process named
    like Chrome, its parent gone, the profile on its command line): reaped. The other's pid is, by the time it
    signals, another process's (a stranger's: its own group, another parent, no profile), stood in for by a `ps` naming
    the stranger's pid as the Chrome's was: left alone (it was SIGKILLed, by group). The sweep sees only this test's
    processes (its `ps` filtered to them), so nothing of the machine is looked at or signalled."""
    mark = _mark()
    env = {**os.environ, "HF_TEST_MARK": mark}
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True, env=env)
    exe = tmp_path / "fake-chrome.py"   # (its command line says chrom, as the sweep looks for)
    exe.write_text("import time\ntime.sleep(120)\n")
    real_prof, stale_prof = tmp_path / "hf-cdp-orphan01", tmp_path / "hf-cdp-stale001"
    real_prof.mkdir()
    stale_prof.mkdir()
    spawn = ("import subprocess, sys\n"
             f"p = subprocess.Popen([sys.executable, {str(exe)!r}, '--user-data-dir={real_prof}'], "
             "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\nprint(p.pid, flush=True)")
    real_run = subprocess.run

    def ppid(pid):
        return real_run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()

    def run(cmd, *a, **k):
        if cmd[:2] == ["ps", "-axww"]:   # the sweep's one ps: the orphan as it is, the stranger's pid as the Chrome's was
            res = real_run(cmd, *a, **k)
            keep = [ln for ln in res.stdout.splitlines() if ln.split()[:1] in ([str(orphan)], [str(os.getpid())])]
            fmt = cmd[cmd.index("-o") + 1]
            row = [str(stranger.pid), "1", str(stranger.pid), "20:00"] + (["Thu", "Jan", "1", "00:00:00", "2026"]
                                                                        if "lstart" in fmt else []) + \
                [f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --headless=new --user-data-dir={stale_prof}"]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(keep + [" ".join(row)]) + "\n", "")
        return real_run(cmd, *a, **k)
    try:
        orphan = int(real_run([sys.executable, "-c", spawn], capture_output=True, text=True, env=env, timeout=30).stdout)
        deadline = time.monotonic() + 10
        while ppid(orphan) != "1" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ppid(orphan) == "1", "(the stand-in orphan wasn't re-parented to launchd)"
        monkeypatch.setattr(subprocess, "run", run)
        cdp.reap_orphans(age=0, tmp=tmp_path)
        monkeypatch.setattr(subprocess, "run", real_run)
        assert stranger.poll() is None, "the sweep SIGKILLed a pid that was no longer the orphaned Chrome's"
        deadline = time.monotonic() + 5
        while ppid(orphan) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not ppid(orphan), "the real orphan wasn't reaped"
        assert not real_prof.exists()
    finally:
        monkeypatch.setattr(subprocess, "run", real_run)
        for pid in _marked(mark):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        stranger.wait(10)


# --- F2 ------------------------------------------------------------------------------------------------------------------

def test_a_tests_own_ledger_keeps_the_runs(tmp_path, monkeypatch):
    """tests/test_cdp_budget.py records its Chromes in a ledger of its own: they must stay in the one check.py gave its
    run, so that the run's watchdog, when the run is killed outright, still stops them (a stand-in Chrome here: a
    process leading its own group with the profile on its command line) and deletes their profiles."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import check
    run_ledger = tmp_path / "hf-check-9-x" / "ledger.txt"
    run_ledger.parent.mkdir()
    run_ledger.touch()
    monkeypatch.setenv("HF_CHROME_LEDGER", str(run_ledger))
    mine = tmp_path / "mine.txt"
    monkeypatch.setenv("HF_CHROME_LEDGER", cdp.ledger_env(mine))   # what the budget test sets
    profile = tempfile.mkdtemp(prefix=cdp.PROFILE_PREFIX, dir=tmp_path)
    mark = _mark()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", f"--user-data-dir={profile}"],
                            start_new_session=True, env={**os.environ, "HF_TEST_MARK": mark})
    c = cdp.Chrome.__new__(cdp.Chrome)
    c.proc, c.pgid, c.profile = proc, proc.pid, profile
    try:
        cdp._ledger("start", c)
        for path in (mine, run_ledger):
            assert [ln.split()[0] for ln in path.read_text().splitlines()] == ["start"], path
        dog = subprocess.run([sys.executable, str(ROOT / "scripts" / "check.py"), "--watchdog", str(run_ledger)],
                             input=b"", timeout=60, env={**check.safe_env(), "CHECK_WATCHDOG_GRACE": "2"})
        assert dog.returncode == 0
        assert proc.wait(10) == -signal.SIGTERM and not Path(profile).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


# --- F5, B1: goto()'s deadlines, on the real WebSocket -----------------------------------------------------------------

class DevTools:
    """TEST-SPEED review B1: a page's DevTools endpoint on a real loopback socket, spoken to by the real
    hermes_finance.cdp_client.WebSocket (not a queue standing in for it): the HTTP upgrade, then RFC 6455 frames.
    `serve(self)` (on a thread) is what the page does: read() takes the client's next message (noting pongs), send()
    writes raw bytes, frame() makes one, answer() answers a command."""

    def __init__(self, serve, rcvbuf: int | None = None):
        self.lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(1)
        self.port = self.lsock.getsockname()[1]
        self.conn, self.buf, self.evaluated, self.pongs = None, bytearray(), [], []
        self.eof = threading.Event()   # the client closed the connection
        self.th = threading.Thread(target=self._run, args=(serve,), daemon=True)
        self.th.start()

    def _run(self, serve) -> None:
        try:
            self.lsock.settimeout(10)
            self.conn, _ = self.lsock.accept()
            self.conn.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            self.conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            serve(self)
        except OSError:
            pass

    def _take(self, n: int) -> bytes | None:
        while len(self.buf) < n:
            chunk = self.conn.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read(self) -> dict | None:
        """The client's next text message; None once it has closed the connection (eof set)."""
        while True:
            head = self._take(2)
            n = head and head[1] & 0x7F
            if head and n == 126:
                n = struct.unpack("!H", self._take(2) or b"\0\0")[0]
            elif head and n == 127:
                n = struct.unpack("!Q", self._take(8) or b"\0" * 8)[0]
            mask = head and self._take(4)
            data = mask and self._take(n)
            if data is None or not head or (head[0] & 0x0F) == 0x8:
                self.eof.set()
                return None
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if head[0] & 0x0F == 0xA:
                self.pongs.append(payload)
                continue
            m = json.loads(payload)
            if m.get("method") == "Runtime.evaluate":
                self.evaluated.append(m["params"]["expression"])
            return m

    @staticmethod
    def frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
        n = len(payload)
        size = bytes([n]) if n < 126 else struct.pack("!BH", 126, n) if n < 65536 else struct.pack("!BQ", 127, n)
        return bytes([(0x80 if fin else 0) | opcode]) + size + payload

    def send(self, raw: bytes) -> None:
        self.conn.sendall(raw)

    def answer(self, i: int, result: dict) -> None:
        self.send(self.frame(json.dumps({"id": i, "result": result}).encode()))

    def until_closed(self) -> None:
        while self.read() is not None:
            pass

    def close(self) -> None:
        for s in (self.conn, self.lsock):
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
        self.th.join(5)


def _connected(serve, **kw):
    """A stand-in Chrome whose DevTools socket is the real WebSocket, connected to DevTools(serve)."""
    dt = DevTools(serve, **kw)
    c = cdp.Chrome.__new__(cdp.Chrome)
    c.n, c.events = 0, []
    c.ws = WebSocket(f"ws://127.0.0.1:{dt.port}/devtools/page/1")
    return c, dt


def _in_time(fn, bound: float) -> dict:
    """fn() on a thread, given `bound` s: {"result": "returned" | "raised: ..." | "unwound: ...", "secs"}, or
    {"result": "still waiting"} (the test's cleanup then closes the connection under it)."""
    out: dict = {}

    def go():
        t0 = time.monotonic()
        try:
            fn()
            out["result"] = "returned"
        except RuntimeError as e:
            out["result"] = f"raised: {e}"
        except BaseException as e:   # (the test's own cleanup closing the connection under a call still waiting)
            out["result"] = f"unwound: {e!r}"
        out.setdefault("secs", time.monotonic() - t0)
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(bound)
    out.setdefault("result", "still waiting")
    out["thread"] = th
    return out


def _join(out: dict) -> None:
    if "thread" in out:
        out["thread"].join(5)


EVENT = json.dumps({"method": "Network.dataReceived", "params": {"requestId": "1", "dataLength": 1}}).encode()


def _flood(dt: DevTools) -> None:
    """Events, never the answer, each write finishing one frame and starting the next (one byte of it): whatever the
    client has read, a frame of it is half there (its buffer never drains)."""
    stop, i, rest = time.monotonic() + 10, 0, dt.frame(EVENT)
    while time.monotonic() < stop and not dt.eof.is_set():
        i += 1
        nxt = dt.frame(EVENT + b" " * (i % 7))
        try:
            dt.send(rest + nxt[:1])
        except OSError:
            return
        rest = nxt[1:]


@pytest.mark.parametrize("how", ["partial-header", "partial-payload", "fragment", "fragment-then-ping", "send-stall"])
def test_a_deadline_bounds_every_frame_and_a_torn_connection_is_closed(how):
    """B1: call(deadline) on the real WebSocket, the page stopping part-way through a message (a frame's header, its
    payload, the first fragment of a message with no continuation, that and a ping), or not reading at all while a
    large command is sent: it fails by its deadline (it used to go on waiting the socket's 60 s per read), says the
    connection is closed, and closes it (the page sees it go); a later call fails at once instead of parsing the rest
    of a torn stream."""
    reading = threading.Event()

    def serve(dt):
        if how == "send-stall":   # doesn't read until the test says (after the call): the command's sending can't finish
            reading.wait(20)
            dt.until_closed()
            return
        dt.read()
        dt.send({"partial-header": b"\x81",
                 "partial-payload": b"\x81\x7e" + struct.pack("!H", 300) + b'{"id": 1, "result": {"x": "' + b"a" * 40,
                 "fragment": dt.frame(b'{"id": 1, "result": ', fin=False),
                 "fragment-then-ping": dt.frame(b'{"id": 1, "result": ', fin=False) + dt.frame(b"hi", opcode=0x9)}[how])
        dt.until_closed()
    c, dt = _connected(serve, rcvbuf=4096 if how == "send-stall" else None)
    if how == "send-stall":
        c.ws.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    expr, out = "x" * 8_000_000 if how == "send-stall" else "1", {}
    try:
        out = _in_time(lambda: c.call("Runtime.evaluate", time.monotonic() + 0.5, expression=expr), 4)
        reading.set()
        assert out["result"] != "still waiting", f"call() still waiting 4 s after its deadline of 0.5 s ({how})"
        assert out["result"].startswith("raised") and out["secs"] < 2, out
        assert "closed" in out["result"], out
        assert dt.eof.wait(5), "the torn connection was left open"
        t0 = time.monotonic()
        with pytest.raises(ConnectionError):
            c.call("Runtime.evaluate", time.monotonic() + 5, expression="2")
        assert time.monotonic() - t0 < 0.5
        if how == "fragment-then-ping":
            assert dt.pongs == [b"hi"], dt.pongs   # (the ping was answered on the way)
    finally:
        reading.set()
        dt.close()   # (unblocks a call still reading)
        _join(out)


def test_a_deadline_bounds_a_stream_of_events_that_never_drains():
    """B1: the page sends events, never the answer, the client's buffer never empty between them (the deadline was only
    looked at with nothing buffered): it fails by its deadline."""
    def serve(dt):
        dt.read()
        _flood(dt)
        dt.until_closed()
    c, dt = _connected(serve)
    out: dict = {}
    try:
        out = _in_time(lambda: c.call("Runtime.evaluate", time.monotonic() + 0.5, expression="1"), 4)
        assert out["result"] != "still waiting", "call() still reading events 4 s after its deadline of 0.5 s"
        assert out["result"].startswith("raised") and out["secs"] < 2, out
        assert len(c.events) > 10, len(c.events)   # (it did read the events meanwhile)
    finally:
        dt.close()
        _join(out)


def test_a_deadline_with_nothing_of_a_message_in_hand_leaves_the_connection_in_use():
    """B1: a ping, then nothing: the ping is answered, the call fails by its deadline, and the connection, with nothing
    of a message half-read, stays in use: the late answer is dropped (another id) and the next call gets its own."""
    def serve(dt):
        first = dt.read()
        dt.send(dt.frame(b"hi", opcode=0x9))
        second = dt.read()   # (after the first call has given up)
        dt.answer(first["id"], {"result": {"type": "number", "value": 1}})
        dt.answer(second["id"], {"result": {"type": "number", "value": 2}})
        dt.until_closed()
    c, dt = _connected(serve)
    out: dict = {}
    try:
        out = _in_time(lambda: c.call("Runtime.evaluate", time.monotonic() + 0.5, expression="1"), 4)
        assert out["result"] != "still waiting", "call() still waiting 4 s after its deadline of 0.5 s (a ping, then nothing)"
        assert out["result"].startswith("raised") and "closed" not in out["result"] and out["secs"] < 2, out
        assert dt.pongs == [b"hi"], dt.pongs
        assert c.call("Runtime.evaluate", time.monotonic() + 5, expression="2") == {"result": {"type": "number", "value": 2}}
        assert not dt.eof.is_set()
    finally:
        dt.close()
        _join(out)


def _page(behaves: str):
    """A stand-in Chrome on DevTools answering goto()'s commands as a page would that `behaves` so."""
    def serve(dt):
        while (m := dt.read()) is not None:
            method, expr = m["method"], m["params"].get("expression", "")
            if method == "Page.navigate":
                r = {"frameId": "f", "loaderId": "" if behaves == "hash-hang" else "L1"}
                if behaves == "fails":
                    r["errorText"] = "net::ERR_CONNECTION_REFUSED"
                dt.answer(m["id"], r)
                continue
            if expr.startswith("window.__pend ?"):          # goto()'s look at the page it leaves
                value = [0.25, 3, "#/a"]
            elif "the page drawn" in expr:                   # READY
                if behaves == "never-drawn":                 # (its frames never come)
                    continue
                value = True
            elif behaves in ("hangs", "hash-hang"):          # the page never answers (a load / hashchange that never comes)
                continue
            elif behaves == "no-commit":
                time.sleep(0.05)                             # (the page's own short wait, then false: still the old page)
                value = False
            else:
                value = True
            dt.answer(m["id"], {"result": {"type": "boolean" if value in (True, False) else "object", "value": value}})
    return _connected(serve)


@pytest.mark.parametrize("behaves, url", [("fails", "http://127.0.0.1:9/#/a"), ("no-commit", "http://127.0.0.1:9/#/b"),
                                          ("hangs", "http://127.0.0.1:9/?t=2#/a"), ("hash-hang", "http://127.0.0.1:9/#/b")])
def test_a_navigation_that_fails_or_never_completes_raises_within_its_deadline(behaves, url, monkeypatch):
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 1.0, raising=False)
    c, dt = _page(behaves)
    out: dict = {}
    try:
        out = _in_time(lambda: c.goto(url), 8)
        assert out["result"] != "still waiting", "goto() still waiting after 8 s (its deadline: 1 s)"
        assert out["result"].startswith("raised") and out["secs"] < 4, out
        assert not any("the page drawn" in e for e in dt.evaluated), "goto() went on to READY after a failed navigation"
    finally:
        dt.close()
        _join(out)


def test_a_navigation_that_commits_but_is_never_drawn_raises_within_its_ready_deadline(monkeypatch):
    """B1: the navigation commits and loads, then READY never answers (the page's frames never come): goto() fails
    READY_TIMEOUT after (READY had no deadline at all: the socket's 60 s per read)."""
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 1.0, raising=False)
    monkeypatch.setattr(cdp, "READY_TIMEOUT", 1.0, raising=False)
    c, dt = _page("never-drawn")
    out: dict = {}
    try:
        out = _in_time(lambda: c.goto("http://127.0.0.1:9/?t=1#/a"), 8)
        assert out["result"] != "still waiting", "goto() still waiting for READY 8 s on (NAV_TIMEOUT 1 s, READY_TIMEOUT 1 s)"
        assert out["result"].startswith("raised") and "READY" in out["result"] and out["secs"] < 4, out
        assert any("the page drawn" in e for e in dt.evaluated)
    finally:
        dt.close()
        _join(out)


def test_a_navigation_that_completes_returns_and_waits_for_the_page(monkeypatch):
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 1.0, raising=False)
    c, dt = _page("commits")
    try:
        c.goto("http://127.0.0.1:9/?t=1#/a")
        assert any("the page drawn" in e for e in dt.evaluated)
    finally:
        dt.close()


NODE_PAGE = r"""
// a page where the navigation committed but its load event (or the new #hash's hashchange) never comes
const listeners = {};
globalThis.window = globalThis;
globalThis.addEventListener = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
globalThis.document = {readyState: 'loading'};
globalThis.__pend = {doc: 0.9, hash: 3};
globalThis.location = {hash: '#/a'};
const t0 = Date.now();
const guard = setTimeout(() => { console.log(JSON.stringify({pending: true})); process.exit(0); }, 4000);
Promise.resolve(eval(process.argv[1])).then((value) => {
  clearTimeout(guard); console.log(JSON.stringify({value, ms: Date.now() - t0})); process.exit(0); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed (the in-page waits are run in it)")
@pytest.mark.parametrize("behaves", ["hangs", "hash-hang"])
def test_the_in_page_waits_of_goto_are_bounded(behaves, monkeypatch):
    """The very expression goto() has the page wait on, run (in node) on a page whose load / hashchange never comes: it
    answers false within goto()'s deadline, instead of never (then only the DevTools socket bounded the wait)."""
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 1.0, raising=False)
    c, dt = _page(behaves)
    try:
        out = _in_time(lambda: c.goto("http://127.0.0.1:9/?t=3#/b" if behaves == "hangs" else "http://127.0.0.1:9/#/b"), 5)
    finally:
        dt.close()
    out["thread"].join(5)
    wait = dt.evaluated[1]
    assert ("addEventListener('load'" if behaves == "hangs" else "hashchange") in wait, wait
    res = subprocess.run([shutil.which("node"), "-e", NODE_PAGE, wait], capture_output=True, text=True, timeout=30)
    got = json.loads(res.stdout or "{}")
    assert got.get("value") is False and got["ms"] < 2500, (got, res.stderr[-500:])


# --- Round 3 (review 1301882) ---------------------------------------------------------------------------------------------

def _ws_pair():
    """The real WebSocket on one end of a socket pair, as if its handshake were done (the other end: the page's)."""
    ws = WebSocket.__new__(WebSocket)
    ws.sock, peer = socket.socketpair()
    ws.buf, ws.timeout, ws.broken = bytearray(), 60, None
    return ws, peer


def test_a_deadline_bounds_a_message_whose_fragments_are_all_buffered():
    """Round 3 B1: a message of 602 fragments, every one already in the buffer (no socket read left, where the time was
    looked at). Taking each frame costs time (a big frame's copy, a loaded machine: 2 ms each here), so taking them all
    runs past a 0.2 s deadline: recv() fails by it, the next frame not taken, and closes the connection (a message
    open). It looked at the time once, on entry, and returned the whole message 1 s late."""
    ws, peer = _ws_pair()
    n = 600
    ws.buf += DevTools.frame(b'{"id": 1, "result": {"x": "', fin=False)
    for _ in range(n):
        ws.buf += DevTools.frame(b"a" * 10, opcode=0x0, fin=False)
    ws.buf += DevTools.frame(b'"}}', opcode=0x0)
    real, taken = ws._frame_in, [0]

    def slow():
        f = real()
        if not isinstance(f, int):
            taken[0] += 1
            time.sleep(0.002)
        return f
    ws._frame_in = slow
    t0 = time.monotonic()
    try:
        got = ws.recv(t0 + 0.2)
        outcome = f"returned a message of {len(got)} characters"
    except WebSocketTimeout as e:
        outcome = f"timed out (torn: {e.torn})"
    took = time.monotonic() - t0
    try:
        assert outcome.startswith("timed out") and took < 0.4, \
            f"recv() {outcome} {took - 0.2:.2f} s after its deadline of 0.2 s ({taken[0]} of {n + 2} frames taken)"
        assert outcome == "timed out (torn: True)" and ws.broken, outcome
        peer.settimeout(5)
        assert peer.recv(1) == b""   # (the page sees the connection go)
        with pytest.raises(ConnectionError):
            ws.recv(time.monotonic() + 5)
    finally:
        peer.close()
        ws.sock.close()


def test_a_message_begun_with_an_empty_fragment_is_open_on_a_deadline():
    """Round 3 B1: the page begins a message with a zero-length, non-final frame, then nothing: the call fails by its
    deadline with a message open, so the connection is closed, and its continuation is never read as a message of its
    own. With no byte of it in hand, it was taken as intact: the next call read the continuation as its answer."""
    def serve(dt):
        dt.read()
        dt.send(dt.frame(b"", fin=False))   # a text message begun, nothing of it yet
        second = dt.read()                  # (only when the connection was kept)
        if second is not None:
            dt.send(dt.frame(json.dumps({"id": second["id"], "result": {"the continuation": True}}).encode(), opcode=0x0))
        dt.until_closed()
    c, dt = _connected(serve)
    out, after = {}, {}
    try:
        out = _in_time(lambda: c.call("Runtime.evaluate", time.monotonic() + 0.5, expression="1"), 4)
        assert out["result"].startswith("raised") and out["secs"] < 2, out
        after = _in_time(lambda: c.call("Runtime.evaluate", time.monotonic() + 2, expression="2"), 4)
        assert "closed" in out["result"] and after["result"].startswith("unwound: ConnectionError"), \
            (f"the first call {out['result']!r}; the next {after['result']!r} (the continuation of an unfinished message "
             f"taken for a message of its own)")
        assert dt.eof.wait(5), "the connection, a message open in it, was left open"
    finally:
        dt.close()
        _join(out)
        _join(after)


@pytest.mark.parametrize("how", ["continuation-first", "new-message-inside-one"])
def test_frames_out_of_order_close_the_connection(how):
    """Round 3 B1: a continuation frame with no message begun, or a new message's first frame inside an unfinished one,
    is a protocol error: the connection is closed (ConnectionError). The continuation was read as a message of its
    own; the new message's text was glued onto the unfinished one's (a JSON error)."""
    def serve(dt):
        m = dt.read()
        answer = json.dumps({"id": m["id"], "result": {}}).encode()
        dt.send(dt.frame(answer, opcode=0x0) if how == "continuation-first" else
                dt.frame(b'{"id": ', fin=False) + dt.frame(answer))
        dt.until_closed()
    c, dt = _connected(serve)
    out: dict = {}
    try:
        out = _in_time(lambda: c.call("Runtime.evaluate", time.monotonic() + 2, expression="1"), 4)
        assert out["result"].startswith("unwound: ConnectionError") and "protocol error" in out["result"], out
        assert dt.eof.wait(5), "the connection was left open"
    finally:
        dt.close()
        _join(out)


LINEAR = r"""
import json, socket, struct, sys, time
sys.path.insert(0, sys.argv[1])
from hermes_finance.cdp_client import WebSocket
def frame(p, op, fin):
    return bytes([(0x80 if fin else 0) | op, len(p)]) + p
n = int(sys.argv[2])
ws = WebSocket.__new__(WebSocket)
ws.sock, peer = socket.socketpair()
ws.timeout, ws.broken = 60, None
ws.buf = bytearray(frame(b"a" * 100, 0x1, False) + frame(b"a" * 100, 0x0, False) * (n - 2) + frame(b"a" * 100, 0x0, True))
t0 = time.monotonic()
got = ws.recv()
print(json.dumps({"chars": len(got), "secs": time.monotonic() - t0}))
"""


def test_a_message_of_many_fragments_is_put_together_in_linear_time():
    """Round 3 B1: 120000 fragments of 100 bytes (12 MB), all buffered, put together in well under a second. Each
    fragment was appended to a bytes object, copying the message so far: quadratic (seen: 1.2 s for 40000, 6 s for
    80000). In a child process, killed at its bound (so a slow one isn't left running)."""
    child = subprocess.Popen([sys.executable, "-c", LINEAR, str(ROOT / "src"), "120000"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        out, err = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        pytest.fail("a 12 MB message of 120000 buffered fragments not put together in 10 s")
    res = json.loads(out or "{}")
    assert res.get("chars") == 12_000_000 and res["secs"] < 3, (res, err[-500:])


class Closes:
    """How often a stand-in's DevTools socket, and its slot's descriptor, are closed (the slot's by its inode, not whatever
    has had its number since). Read them before anything else opens the slot's file (_free())."""

    def __init__(self, c, monkeypatch):
        self.ws = self.slot = 0
        ws_close = c.ws.close

        def counted_ws_close():
            self.ws += 1
            return ws_close()
        c.ws.close = counted_ws_close
        fd, ino, os_close = c.slot, os.fstat(c.slot).st_ino, os.close

        def counted_close(n):
            try:
                if n == fd and os.fstat(n).st_ino == ino:
                    self.slot += 1
            except OSError:
                pass
            return os_close(n)
        monkeypatch.setattr(os, "close", counted_close)


def _interrupt(point: str, c, monkeypatch) -> list:
    """An exception out of c's finishing, once: at its profile's deletion, or at its `stop` line."""
    broke: list = []
    if point == "profile":
        real = shutil.rmtree

        def rmtree(path, *a, **k):
            if str(path) == c.profile and not broke:
                broke.append(point)
                raise OSError("interrupted here (the test's)")
            return real(path, *a, **k)
        monkeypatch.setattr(shutil, "rmtree", rmtree)
    else:
        real_ledger = cdp._ledger

        def ledger(event, ch):
            if event == "stop" and ch is c and not broke:
                broke.append(point)
                raise OSError("interrupted here (the test's)")
            return real_ledger(event, ch)
        monkeypatch.setattr(cdp, "_ledger", ledger)
    return broke


@pytest.mark.parametrize("point, then", [("profile", "close_all"), ("stop-line", "close_all"), ("profile", "close"),
                                         ("stop-line", "close")])
def test_a_finishing_interrupted_part_way_is_kept_and_finished_once_by_a_later_stop(point, then, private, monkeypatch):
    """Round 3 B2: a stop (the only one: nobody waits to take it over) is interrupted part-way through finishing the
    Chrome, after its group was seen gone and its socket closed: at the profile's deletion, or at its `stop` line. The
    Chrome stays in _LIVE, and a later stop, close_all() or close(), finishes it: one `stop` line, the socket and the
    slot's descriptor closed once each. It left _LIVE first (close_all() never saw it: no `stop`, profile and slot kept
    for good); a second close() closed the socket again, and skipped the `stop` it had marked written before writing."""
    ledger = private / "ledger.txt"
    monkeypatch.setenv("HF_CHROME_LEDGER", str(ledger))
    monkeypatch.setattr(cdp, "_LIVE", set())
    monkeypatch.setattr(cdp, "_CLOSING", False)   # (close_all() sets it: this process goes on)
    mark = _mark()
    c = standin(private, _fake(private, "fake-chrome", SLEEPS), mark)
    c.ws, peer = _ws_pair()
    n = Closes(c, monkeypatch)
    broke = _interrupt(point, c, monkeypatch)
    try:
        with pytest.raises(Exception) as first:
            c.close()
        mid = {"in _LIVE": c in cdp._LIVE, "stopped": c._stopped}
        (cdp.close_all if then == "close_all" else c.close)()
        stops = [ln for ln in ledger.read_text().splitlines() if ln.startswith("stop ")]
        got = {**mid, "stops": len(stops), "ws closes": n.ws, "slot closes": n.slot, "stopped at the end": c._stopped,
               "in _LIVE at the end": c in cdp._LIVE, "profile": Path(c.profile).exists()}
        assert broke == [point] and "interrupted here" in str(first.value), (broke, first.value)
        want = {"in _LIVE": True, "stopped": False, "stops": 1, "ws closes": 1, "slot closes": 1, "stopped at the end": True,
                "in _LIVE at the end": False, "profile": False}
        assert got == want, f"interrupted at its {point}, then {then}(): {got}"
        assert _free(private / "slots" / "slot-0") and _gone(c.pgid, mark, 2)
    finally:
        peer.close()
        _cleanup(c, mark)


def test_one_chromes_failed_finishing_leaves_the_others_finished(private, monkeypatch):
    """Round 3 B2: close_all() with two Chromes, the finishing of one raising part-way: the other is still finished, the
    failed one stays in _LIVE (close_all() says it failed), and the next close_all() finishes it. The exception went
    straight out: whichever Chrome came after it was left unfinished, and the failed one had left _LIVE."""
    ledger = private / "ledger.txt"
    monkeypatch.setenv("HF_CHROME_LEDGER", str(ledger))
    monkeypatch.setattr(cdp, "_LIVE", set())
    monkeypatch.setattr(cdp, "_CLOSING", False)
    mark = _mark()
    fake = _fake(private, "fake-chrome", SLEEPS)
    bad, good = standin(private, fake, mark, slot=0), standin(private, fake, mark, slot=1)
    _interrupt("profile", bad, monkeypatch)
    try:
        with pytest.raises(Exception) as first:
            cdp.close_all()
        mid = {"good stopped": good._stopped, "good's profile": Path(good.profile).exists(), "good in _LIVE": good in cdp._LIVE,
               "bad stopped": bad._stopped, "bad in _LIVE": bad in cdp._LIVE}
        cdp.close_all()
        stops = sorted(int(ln.split()[3]) for ln in ledger.read_text().splitlines() if ln.startswith("stop "))
        assert mid == {"good stopped": True, "good's profile": False, "good in _LIVE": False, "bad stopped": False,
                       "bad in _LIVE": True}, (mid, repr(first.value))
        assert bad._stopped and not Path(bad.profile).exists() and bad not in cdp._LIVE, "the failed one not finished later"
        assert stops == sorted([bad.pgid, good.pgid]), stops
        assert _free(private / "slots" / "slot-0") and _free(private / "slots" / "slot-1")
    finally:
        _cleanup(bad, mark)
        _cleanup(good, mark)


HELPER_NAMING = ("subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', sys.argv[1]])\n"
                 "sys.exit(0)")   # its main process exits by itself; a helper naming the profile (as Chrome's do) goes on


@pytest.mark.parametrize("ps", ["nonzero", "no-self-row"])
def test_a_failed_process_listing_is_not_taken_for_a_chrome_gone(ps, private, monkeypatch):
    """Round 3 B3: the Chrome's main process was reaped by someone else (not cdp), a helper of it still running and
    naming the profile. Its stop can only look for what names the profile, and that `ps` fails: exit 1 with nothing on
    stdout, or exit 0 with a listing that doesn't even have this process in it. The Chrome is not recorded stopped and
    all of it is kept (the stop raises). The failed ps read as "nothing running": `stop` written, the profile of a
    Chrome still running deleted, its slot given back. With ps answering, a stop still sees the helper; once it's
    gone, a stop finishes the Chrome, once."""
    ledger = private / "ledger.txt"
    monkeypatch.setenv("HF_CHROME_LEDGER", str(ledger))
    monkeypatch.setattr(cdp, "_LIVE", set())
    mark = _mark()
    c = standin(private, _fake(private, "fake-chrome", HELPER_NAMING), mark, args=("--user-data-dir={profile}",))
    real_run = subprocess.run

    def run(cmd, *a, **k):
        if cmd[:2] == ["ps", "-axww"]:
            if ps == "nonzero":
                return subprocess.CompletedProcess(cmd, 1, "", "ps: (the test's) failure\n")
            res = real_run(cmd, *a, **k)   # cut short: neither this process nor the helper in it
            rows = [ln for ln in res.stdout.splitlines() if ln.split()[:1] != [str(os.getpid())] and c.profile not in ln]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(rows) + "\n", "")
        return real_run(cmd, *a, **k)
    try:
        c.proc.wait(10)   # (reaped by someone else)
        deadline = time.monotonic() + 10
        while not _procs(c.pgid, mark) and time.monotonic() < deadline:
            time.sleep(0.05)
        helper = _procs(c.pgid, mark)
        assert helper, "(the helper never came up)"
        monkeypatch.setattr(subprocess, "run", run)
        try:
            cdp._stop([c], grace=0.3)
            outcome = "returned"
        except RuntimeError as e:
            outcome = f"raised: {e}"
        monkeypatch.setattr(subprocess, "run", real_run)
        kinds = [ln.split()[0] for ln in ledger.read_text().splitlines()]
        assert "stop" not in kinds, f"recorded stopped on a failed ps, its helper {helper} still running: {kinds} ({outcome})"
        assert outcome.startswith("raised") and "can't be looked for" in outcome, outcome
        assert Path(c.profile).exists() and c.slot is not None and c in cdp._LIVE and not c._stopped
        with pytest.raises(RuntimeError, match="naming its profile still running"):
            cdp._stop([c], grace=0.3)
        _kill_marked(c.pgid, mark)
        assert _gone(c.pgid, mark, 5)
        cdp._stop([c], grace=0.3)
        kinds = [ln.split()[0] for ln in ledger.read_text().splitlines()]
        assert kinds.count("stop") == 1 and not Path(c.profile).exists() and c._stopped and c not in cdp._LIVE, kinds
        assert _free(private / "slots" / "slot-0")
    finally:
        monkeypatch.setattr(subprocess, "run", real_run)
        _cleanup(c, mark)


def test_the_sweep_signals_only_the_pids_it_verified_never_their_group(tmp_path, monkeypatch):
    """Round 3 B4: an orphaned Chrome (its main process re-parented to launchd, leading its group, the profile on its
    command line) with a process in its group that doesn't name the profile, so nothing the sweep took for that Chrome
    or looked at again. The sweep SIGKILLs the pids it verified, by pid (the main process first), and nothing by group:
    that other process goes on. It SIGKILLed the group (its id read off the sweep's own `ps`), that process with it.
    The sweep sees only this test's processes (its `ps` filtered to them)."""
    mark = _mark()
    env = {**os.environ, "HF_TEST_MARK": mark}
    exe = tmp_path / "fake-chrome.py"   # (its command line says chrom, as the sweep looks for)
    exe.write_text("import subprocess, sys, time\n"
                   "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"   # its group, no profile
                   "time.sleep(120)\n")
    prof = tmp_path / "hf-cdp-orphan02"
    prof.mkdir()
    spawn = ("import subprocess, sys\n"
             f"p = subprocess.Popen([sys.executable, {str(exe)!r}, '--user-data-dir={prof}'], "
             "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\nprint(p.pid, flush=True)")
    real_run, real_kill, real_killpg, signals = subprocess.run, os.kill, os.killpg, []

    def ppid(pid):
        return real_run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()

    def run(cmd, *a, **k):
        res = real_run(cmd, *a, **k)
        if cmd[:2] == ["ps", "-axww"]:   # the sweep's one ps: this test's processes, and this one
            keep = {str(orphan), str(os.getpid()), *map(str, member)}
            return subprocess.CompletedProcess(cmd, res.returncode, "\n".join(
                ln for ln in res.stdout.splitlines() if ln.split()[:1] and ln.split()[0] in keep) + "\n", res.stderr)
        return res

    def kill(pid, sig):
        if sig:
            signals.append(("kill", pid, signal.Signals(sig).name))
        return real_kill(pid, sig)

    def killpg(pg, sig):
        if sig:
            signals.append(("killpg", pg, signal.Signals(sig).name))
        return real_killpg(pg, sig)
    orphan, member = None, []
    try:
        orphan = int(real_run([sys.executable, "-c", spawn], capture_output=True, text=True, env=env, timeout=30).stdout)
        deadline = time.monotonic() + 10
        while (ppid(orphan) != "1" or not member) and time.monotonic() < deadline:
            member = [p for p in _procs(orphan, mark) if p != orphan]
            time.sleep(0.05)
        assert ppid(orphan) == "1" and member, "(the stand-in orphan and its group's other process didn't come up)"
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(os, "kill", kill)
        monkeypatch.setattr(os, "killpg", killpg)
        cdp.reap_orphans(age=0, tmp=tmp_path)
        monkeypatch.setattr(os, "kill", real_kill)
        monkeypatch.setattr(os, "killpg", real_killpg)
        monkeypatch.setattr(subprocess, "run", real_run)
        assert not [s for s in signals if s[0] == "killpg"], f"the sweep signalled a group: {signals}"
        assert ("kill", orphan, "SIGKILL") in signals, signals
        assert _procs(orphan, mark) == member, \
            f"the process of its group it never verified ({member}) was killed: left {_procs(orphan, mark)}; {signals}"
        assert not ppid(orphan) and not prof.exists(), "the orphan wasn't reaped"
    finally:
        monkeypatch.setattr(os, "kill", real_kill)
        monkeypatch.setattr(os, "killpg", real_killpg)
        monkeypatch.setattr(subprocess, "run", real_run)
        for pid in _marked(mark):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.parametrize("child_says", ["its result", "a wrong result"])
def test_the_constructor_tests_harness_ends_its_child_and_reports_its_own_failure(child_says, tmp_path, monkeypatch):
    """Round 3 B6: tests/test_cdp_budget.py's child harness (_run_child) itself, here with a child that starts nothing
    (a stand-in saying what that file's child would: no browser, no constructor). It ends its child and closes what it
    opened, and when the child's result is wrong, its own assertion is what the test sees. Its cleanup closed
    child.stderr, None for a child whose stderr goes to a file: AttributeError at every end, in place of the result or
    of the harness's own failure. (Imported by name: an import statement of it would make this file one of check.py's
    browser files, and nothing here starts a browser: HF_NO_CHROME would fail it loudly if anything did.)"""
    budget = importlib.import_module("test_cdp_budget")
    said = {"its result": {"joined": True, "err": "Chrome did not start (it exited: 3)", "secs": 0.1},
            "a wrong result": {"joined": False, "secs": 0.1}}[child_says]
    monkeypatch.setattr(budget, "CHILD", f"import os\nos.write(1, {(json.dumps(said) + chr(10)).encode()!r})\n")
    if child_says == "its result":
        assert budget._run_child(tmp_path, "exited", "sys.exit(3)") == said
    else:
        with pytest.raises(AssertionError) as e:
            budget._run_child(tmp_path, "exited", "sys.exit(3)")
        assert "'joined': False" in str(e.value), e.value


def test_the_handshake_says_where_the_start_is_when_the_signal_lands_not_where_it_was(private):
    """Round 3 B6: `landed` was what the start last said (sticky), not where it was: a SIGTERM that came once the start
    had left its boundary, here once it had ended altogether (its browser exited at once; the SIGTERM sent after the
    ledger's `stop`), still "landed connect". The handshake now looks at the start's thread: "elsewhere"."""
    mark = _mark()
    child = _child(private, "connect-over", mark, _fake(private, "fake-chrome", "sys.exit(0)"))
    out = Lines(child)
    try:
        assert out.line(20) == "ready connect"
        deadline = time.monotonic() + 10
        while "stop" not in [ln.split()[0] for ln in (private / "ledger.txt").read_text().splitlines()] \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)   # (and its thread gone by now)
        child.send_signal(signal.SIGTERM)
        got = out.line(10)
        assert got == "landed elsewhere", f"the child said {got!r} though the start had ended before the SIGTERM was sent"
        res = json.loads(out.line(20))
        assert child.wait(20) == 0 and res["landed"] == "elsewhere" and "did not start" in res.get("err", ""), res
    finally:
        _end_child(child, mark)


# A stand-in browser that answers DevTools (its port in the profile, /json, the WebSocket upgrade), then holds the start at
# one step of it, and says so (its stdout is the child's: the parent's pipe) at that step: `handshake`, the upgrade
# asked for and its 101 withheld; `enable`, the upgrade done and the first command (Page.enable) never answered.
DEVTOOLS = r"""
import json, socket
prof, stage = sys.argv[1].split("=", 1)[1], sys.argv[2]
ls = socket.socket()
ls.bind(("127.0.0.1", 0))
ls.listen(8)
port = ls.getsockname()[1]
with open(prof + "/DevToolsActivePort.new", "w") as f:
    f.write(f"{port}\n/devtools/browser/stand-in\n")
os.replace(prof + "/DevToolsActivePort.new", prof + "/DevToolsActivePort")
while True:
    conn, _ = ls.accept()
    req = b""
    while b"\r\n\r\n" not in req and (chunk := conn.recv(4096)):
        req += chunk
    if req.startswith(b"GET /json"):
        body = json.dumps([{"type": "page", "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/1"}]).encode()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     % len(body) + body)
        conn.close()
        continue
    if stage == "handshake":
        os.write(1, b"ready handshake\n")
        time.sleep(120)
    conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
    conn.recv(65536)
    os.write(1, b"ready enable\n")
    time.sleep(120)
"""


@pytest.mark.parametrize("what", ["handshake", "enable"])
def test_a_sigterm_while_a_chrome_talks_to_its_devtools_stops_it(what, private):
    """Round 3 B6 (the review's uncovered boundary, with a stand-in: no real Chrome's DevTools): the start has found
    DevTools and is in its WebSocket handshake (its 101 withheld), or has connected and waits for its first command's
    answer, when a real SIGTERM comes: it lands there (the start's stack), the start fails at once, its thread ends,
    and nothing is left (browser, profile, slot; the ledger's start and stop once each)."""
    mark = _mark()
    child = _child(private, what, mark, _fake(private, "fake-chrome", DEVTOOLS))
    out = Lines(child)
    try:
        assert out.line(20) == f"ready {what}"
        pgid = int(next(f[3] for f in (ln.split() for ln in (private / "ledger.txt").read_text().splitlines())))
        t0 = time.monotonic()
        child.send_signal(signal.SIGTERM)
        assert out.line(10) == f"landed {what}"
        res = json.loads(out.line(20))
        assert child.wait(20) == 0 and time.monotonic() - t0 < 10
        assert res.get("joined") is True and res["landed"] == what and res.get("err"), res
        assert _gone(pgid, mark, 2) and _free(private / "slots" / "slot-0")
        assert not list(private.glob(cdp.PROFILE_PREFIX + "*"))
        lines = [ln.split() for ln in (private / "ledger.txt").read_text().splitlines()]
        assert sorted(f[0] for f in lines) == ["start", "stop"] and {f[3] for f in lines} == {str(pgid)}, lines
    finally:
        _end_child(child, mark)


# --- Round 4 (review 7bf4d35) ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["all gone", "a helper naming it", "a denied helper", "an unverified helper",
                                  "every signal denied", "the last look fails", "the last look cut short"])
def test_the_sweep_deletes_a_reaped_chromes_profile_only_once_nothing_names_it(case, tmp_path, monkeypatch):
    """Round 4 C1: an orphaned Chrome (its main process re-parented to launchd, leading its group, the profile on its
    command line), alone or with a process of its own: another of its processes (--user-data-dir=<profile>, a Chrome's
    name), or a helper naming the profile without that flag (as crashpad's --database=<profile>/Crashpad would), which
    PID-only reaping leaves alone. The sweep deletes the profile only once a fresh, whole look after its SIGKILLs finds
    nothing naming it. It deleted it as soon as the pids it had signalled were gone: with that helper still running;
    with a SIGKILL refused (PermissionError, as for another user's process: stood in for here), that process running on;
    with one _still didn't verify (not the process the sweep's `ps` saw: its start time differs), never signalled;
    with every SIGKILL refused (nothing sent: "all it signalled gone" held vacuously); and with no look at all, so a
    last look that fails, or is cut short (no line for the sweep's own process), couldn't keep it. All of it gone and
    seen gone, it is deleted (the control). The sweep sees only this test's processes (its `ps` filtered to them)."""
    mark = _mark()
    env = {**os.environ, "HF_TEST_MARK": mark}
    prof = tmp_path / "hf-cdp-orphan03"
    prof.mkdir()
    exe = tmp_path / "fake-chrome.py"   # (its command line says chrom, as the sweep looks for)
    helper = {"all gone": f"[sys.executable, {str(exe)!r}, '--user-data-dir={prof}', '--type=renderer']",
              "a denied helper": f"[sys.executable, {str(exe)!r}, '--user-data-dir={prof}', '--type=renderer']",
              "an unverified helper": f"[sys.executable, {str(exe)!r}, '--user-data-dir={prof}', '--type=renderer']",
              "a helper naming it": f"[sys.executable, '-c', 'import time; time.sleep(120)', '--database={prof}/Crashpad']",
              }.get(case)
    exe.write_text("import subprocess, sys, time\n"
                   + (f"if '--type=renderer' not in sys.argv:\n    subprocess.Popen({helper})\n" if helper else "")
                   + "time.sleep(120)\n")
    spawn = ("import subprocess, sys\n"
             f"p = subprocess.Popen([sys.executable, {str(exe)!r}, '--user-data-dir={prof}'], "
             "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\nprint(p.pid, flush=True)")
    real_run, real_kill, signals, looks = subprocess.run, os.kill, [], []

    def ppid(pid):
        return real_run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()

    def run(cmd, *a, **k):
        res = real_run(cmd, *a, **k)
        if cmd[:2] == ["ps", "-axww"]:   # a whole look: this test's processes, and this one
            looks.append(cmd)
            keep = {str(orphan), str(os.getpid()), *map(str, others)}
            rows = [ln for ln in res.stdout.splitlines() if ln.split()[:1] and ln.split()[0] in keep]
            if len(looks) == 1 and case == "an unverified helper":   # (the sweep's ps saw another start time for it)
                rows = [" ".join(ln.split(None, 9)[:4] + ["Thu", "Jan", "1", "00:00:00", "2026", ln.split(None, 9)[9]])
                        if ln.split()[0] in map(str, others) else ln for ln in rows]
            if len(looks) > 1 and case == "the last look fails":
                return subprocess.CompletedProcess(cmd, 1, "", "ps: (the test's) failure\n")
            if len(looks) > 1 and case == "the last look cut short":
                rows = [ln for ln in rows if ln.split()[0] != str(os.getpid())]
            return subprocess.CompletedProcess(cmd, res.returncode, "\n".join(rows) + "\n", res.stderr)
        return res

    def kill(pid, sig):
        if sig == signal.SIGKILL:
            signals.append(pid)
            if pid in denied:
                raise PermissionError(1, "Operation not permitted (the test's)")
        return real_kill(pid, sig)
    orphan, others, denied = None, [], set()
    try:
        orphan = int(real_run([sys.executable, "-c", spawn], capture_output=True, text=True, env=env, timeout=30).stdout)
        deadline = time.monotonic() + 10
        while (ppid(orphan) != "1" or (helper and not others)) and time.monotonic() < deadline:
            others = [p for p in _procs(orphan, mark) if p != orphan]
            time.sleep(0.05)
        assert ppid(orphan) == "1" and (not helper or others), "(the stand-in orphan, or its other process, didn't come up)"
        denied = {"a denied helper": set(others), "every signal denied": {orphan}}.get(case, set())
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(os, "kill", kill)
        swept = cdp.reap_orphans(age=0, tmp=tmp_path)
        monkeypatch.setattr(os, "kill", real_kill)
        monkeypatch.setattr(subprocess, "run", real_run)
        deadline = time.monotonic() + 5
        while case != "every signal denied" and ppid(orphan) and time.monotonic() < deadline:
            time.sleep(0.05)
        running = _procs(orphan, mark)
        facts = f"{case}: swept {swept}, SIGKILLs {signals}, still running {running}, looks {len(looks)}"
        if case == "every signal denied":
            assert running == [orphan] and orphan in signals, facts
        else:
            assert orphan not in running and orphan in signals, f"the orphan wasn't reaped: {facts}"
        if case == "all gone":
            assert running == [] and not prof.exists() and swept == (2, 1), facts
        else:
            assert running == ([] if case.startswith("the last look") else [orphan] if case == "every signal denied"
                               else others), facts
            if case in ("a helper naming it", "an unverified helper"):
                assert not set(others) & set(signals), f"PID-only: a process not verified was signalled: {facts}"
            assert prof.is_dir(), f"the profile was deleted though {'a process still names it' if running else 'nothing whole was seen after the SIGKILLs'}: {facts}"
            assert swept[1] == 0, facts
    finally:
        monkeypatch.setattr(os, "kill", real_kill)
        monkeypatch.setattr(subprocess, "run", real_run)
        for pid in _marked(mark):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
