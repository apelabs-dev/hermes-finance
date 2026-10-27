"""A small headless-Chrome driver for browser checks, stdlib only (no websocket package, no Selenium).

Chrome gets its own temp profile and a random DevTools port (--remote-debugging-port=0, read back from
DevToolsActivePort), so it never touches 9222 or a real profile. Every https:// request and anything on
plaid.com is blocked, so a page under test can only reach the local server it was pointed at.

    with Chrome() as b:
        b.size(1440, 900)
        b.goto("http://127.0.0.1:8811/#/budgets")
        print(b.js("document.title"))

FIX-CDP-LEAK: a Chrome never outlives the process that started it. Chrome runs in its own process group (with its ~8
helpers), and close() stops the whole group (SIGTERM, then SIGKILL after 5 s). Every live Chrome is in a registry that
an atexit hook and a SIGTERM / SIGHUP handler close, so a run that ends, raises, is Ctrl-C'd or is terminated takes its
Chromes with it. A run killed with SIGKILL can't clean up: the first Chrome() of the next run (once per process) kills
the Chromes such a run left behind (re-parented to launchd, no live owner, older than ORPHAN_AGE) and deletes the
hf-cdp-* profiles nothing uses any more.

TEST-SPEED: at most HF_CHROMES (4 by default, and at most) of these Chromes run at once machine-wide, whatever process started them (a
lock file per slot; a Chrome() over the budget waits for one, and a deadlock between waiters fails one of them instead of
hanging); with HF_CHROME_LEDGER set, each start and stop is written there (scripts/check.py: the run's own record of its
Chromes, and what its watchdog stops if the run is killed outright). Chrome.settled() and goto() wait on what the page
has in flight (PEND) instead of fixed pauses.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from hermes_finance.cdp_client import WebSocket, WebSocketTimeout  # the minimal RFC 6455 client the app's Carfax lookup uses

CHROME_PATHS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome",
                "/usr/bin/chromium", "/usr/bin/chromium-browser")
NO_REDUCE = [{"name": "prefers-reduced-motion", "value": "no-preference"}]   # (Chrome() pins it, see there)
BLOCKED = ["https://*", "*plaid.com*"]
# Installed before any page script runs: JS errors, unhandled rejections and CSP violations land here.
WATCH = """window.__errs = []; window.__csp = [];
addEventListener('error', (e) => { if (e.target && e.target.tagName) return; __errs.push(String(e.message)); }, true);
addEventListener('unhandledrejection', (e) => __errs.push('rejection: ' + String(e.reason)));
document.addEventListener('securitypolicyviolation', (e) => __csp.push(e.violatedDirective + ' ' + e.blockedURI));"""
READY_POLL = """new Promise((res) => { const t0 = Date.now(); const tick = () => {
  const busy = document.querySelector('#page .skel, #page [aria-busy=true]');
  if ((!busy && document.fonts.status === 'loaded' && document.querySelector('#page > *')) || Date.now() - t0 > 8000)
    requestAnimationFrame(() => requestAnimationFrame(() => res(true)));
  else setTimeout(tick, 50); }; tick(); })"""
# The page is drawn: no skeleton, fonts in, something on #page (8 s at most), then two frames. TEST-SPEED: watched with
# PEND's __appear (woken by the page's own events) rather than polled every 50 ms (a 50 ms timer fires 60-190 ms late in
# this process tree); it also stops waiting once the page is settled undrawn with nothing left that could draw it.
DRAWN = "!document.querySelector('#page .skel, #page [aria-busy=true]') && document.fonts.status === 'loaded' && document.querySelector('#page > *')"
READY = (f"(window.__appear ? window.__appear(() => {DRAWN}, 8000, 'the page drawn').catch(() => true)"
         f".then(() => new Promise((res) => requestAnimationFrame(() => requestAnimationFrame(() => res(true))))) : {READY_POLL})")
SETTLE = "new Promise((res) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(() => res(true), 60))))"
# TEST-SPEED: what the page still has in flight, installed (like WATCH) before any page script runs, so a check can wait
# for the page to be settled instead of for a fixed time: fetch() calls and the reading of their bodies, timers of 1 s or
# less (a debounce, a deferred redraw; a longer one is later behaviour: a toast closing itself, a poll), animation frames
# asked for, finite CSS animations and transitions, fonts, images, and DOM mutations. The waits are driven by those events
# (each one that ends wakes them), not by polling: headless Chrome's timers fire ~30-40 ms late here and its frames come
# every ~40-80 ms, so a 10 ms poll is really a 40 ms one. They use the originals of setTimeout / requestAnimationFrame,
# so they don't count themselves.
#   __settled(cap, force): resolves '' once none of that is pending and a whole frame (its animation-frame callbacks,
#                   layout, resize observers: a card packer's re-lay) went by without a DOM change or anything new pending
#                   (at once when nothing at all happened since the last such frame, unless `force`: after a resize);
#                   after `cap` ms it resolves anyway, with what was still going on.
#   __appear(test, ms, what): resolves when test() is truthy; rejects "never appeared: <what>" at `ms`, or as soon as the
#                   page is settled with nothing left that could still change it before then (no timer due, no interval).
PEND = """(() => {
  const P = {fetch: 0, body: 0, raf: new Set(), timers: new Map(), intervals: new Map(), mut: 0, hash: 0, doc: Math.random(),
    ev: 0, quiet: -1, calm: -1};   // ev: every event below; quiet: its value at the last quiet frame; calm: see the rAF wrapper
  const O = {setTimeout: setTimeout.bind(window), clearTimeout: clearTimeout.bind(window), setInterval: setInterval.bind(window),
    clearInterval: clearInterval.bind(window), raf: requestAnimationFrame.bind(window), caf: cancelAnimationFrame.bind(window)};
  Object.defineProperty(window, '__pend', {value: P});
  // wake-ups: every waiter re-checks in a task of its own (a MessageChannel message: no timer delay) once something ended
  const waiters = new Set(), later = [];
  const ch = new MessageChannel();
  let queued = false;
  ch.port1.onmessage = (e) => { if (e.data === 'wake') { queued = false; for (const w of [...waiters]) w(); } else later.shift()(); };
  const wake = () => { P.ev += 1; if (waiters.size && !queued) { queued = true; ch.port2.postMessage('wake'); } };
  const task = (fn) => { later.push(fn); ch.port2.postMessage('task'); };
  const count = (p, k) => { P[k] += 1; P.ev += 1; const done = () => { P[k] -= 1; wake(); }; p.then(done, done); return p; };
  const f = window.fetch;
  window.fetch = function (...a) { return count(f.apply(this, a), 'fetch'); };
  for (const m of ['json', 'text', 'arrayBuffer', 'blob', 'formData']) {
    const o = Response.prototype[m];
    if (o) Response.prototype[m] = function (...a) { return count(o.apply(this, a), 'body'); };
  }
  window.setTimeout = function (fn, ms, ...a) {
    if (typeof fn !== 'function') return O.setTimeout(fn, ms, ...a);
    const id = O.setTimeout(function () { P.timers.delete(id); try { return fn.apply(this, a); } finally { wake(); } }, ms);
    P.timers.set(id, {due: Date.now() + (Number(ms) || 0), short: !(Number(ms) > 1000)});
    P.ev += 1;
    return id;
  };
  window.clearTimeout = (id) => { P.timers.delete(id); P.intervals.delete(id); wake(); return O.clearTimeout(id); };
  window.setInterval = function (fn, ms, ...a) { const id = O.setInterval(fn, ms, ...a); P.intervals.set(id, Number(ms) || 0); P.ev += 1; return id; };
  window.clearInterval = (id) => { P.intervals.delete(id); P.timers.delete(id); wake(); return O.clearInterval(id); };
  // calm: P.ev once a page's animation-frame callback (and the microtasks it started: its DOM changes seen) is done; that
  // frame's layout and resize observers then run before any task, so a task that still finds P.ev at `calm` knows a
  // whole frame went by after the page's last work without anything happening (a card packer's re-lay: no extra frame)
  window.requestAnimationFrame = (fn) => { const id = O.raf((t) => { P.raf.delete(id); try { fn(t); } finally { wake(); queueMicrotask(() => { P.calm = P.ev; }); } }); P.raf.add(id); P.ev += 1; return id; };
  window.cancelAnimationFrame = (id) => { P.raf.delete(id); wake(); return O.caf(id); };
  new MutationObserver(() => { P.mut += 1; wake(); }).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  addEventListener('hashchange', () => { P.hash += 1; wake(); });   // before the page's own listener: it has run when this is seen
  addEventListener('load', wake, true);    // the document, and (capturing) every image
  addEventListener('error', wake, true);
  document.addEventListener('readystatechange', wake);
  const hooked = new WeakSet();
  const busy = () => {
    if (document.readyState !== 'complete') return 'loading';
    if (P.fetch > 0) return 'fetch';
    if (P.body > 0) return 'body';
    for (const t of P.timers.values()) if (t.short) return 'timer';
    if (P.raf.size) return 'animation frame';
    if (document.fonts && document.fonts.status !== 'loaded') { document.fonts.ready.then(wake); return 'fonts'; }
    for (const i of document.images) if (!i.complete) return 'image';
    for (const a of document.getAnimations()) {
      if (a.playState === 'running' && a.effect && a.effect.getComputedTiming().iterations !== Infinity) {
        if (!hooked.has(a)) { hooked.add(a); a.finished.then(wake, wake); }
        return 'css animation';
      }
    }
    return '';
  };
  const idle = (until) => { if (busy()) return false; const now = Date.now();
    for (const t of P.timers.values()) if (t.due <= until) return false;
    for (const ms of P.intervals.values()) if (now + ms <= until) return false;
    return true; };
  // wait(check, ms): check() runs now, on every wake-up, and every 250 ms (a net under the events), until it returns true;
  // check(true) is its last call, at `ms`
  const wait = (check, ms) => {
    let over = false;
    const w = () => { if (!over && check(false)) stop(); };
    const net = O.setInterval(w, 250);
    const cap = O.setTimeout(() => { if (!over) { stop(); check(true); } }, ms);
    const stop = () => { over = true; waiters.delete(w); O.clearInterval(net); O.clearTimeout(cap); };
    waiters.add(w);
    w();
  };
  // quiet(then): after a whole frame, then(changed): whether anything happened since (a DOM change, anything new pending)
  const quiet = (then) => { const e = P.ev; O.raf(() => task(() => { const changed = P.ev !== e || busy() !== ''; if (!changed) P.quiet = e; then(changed); })); };
  const calm = () => P.ev === P.quiet || P.ev === P.calm;   // (only ever asked in a task, never inside a frame)
  Object.defineProperty(window, '__settled', {value: (cap = 10000, force = false) => new Promise((res) => {
    let framing = false, done = false;
    if (!force && calm() && !busy()) return res('');   // nothing at all has happened since the last quiet frame
    wait((last) => {
      if (done) return true;
      if (last) { done = true; res(`not settled after ${cap} ms: ${busy() || 'the page still changing'}`); return true; }
      if (framing || busy()) return false;
      if (!force && calm()) { done = true; P.quiet = P.ev; res(''); return true; }
      framing = true;
      quiet((changed) => { framing = false; if (done) return; if (!changed) { done = true; res(''); } wake(); });
      return false;
    }, cap);
  })});
  Object.defineProperty(window, '__appear', {value: (test, ms, what) => new Promise((res, rej) => {
    const end = Date.now() + ms;
    let framing = false, done = false;
    const finish = (ok) => { if (done) return; done = true; if (ok) res(true); else rej(new Error('never appeared: ' + what)); };
    wait((last) => {
      if (done) return true;
      if (test()) { finish(true); return true; }
      if (last) { finish(false); return true; }
      if (framing || !idle(end)) return false;
      if (calm()) { finish(false); return true; }   // settled, and nothing left that could bring it in time
      framing = true;
      quiet((changed) => { framing = false; if (done) return; if (test()) finish(true); else if (!changed && idle(end)) finish(false); wake(); });
      return false;
    }, ms);
  })});
})();"""
READY_NOW = f"(window.__appear ? window.__appear(() => {DRAWN}, 8000, 'the page drawn').catch(() => true) : {READY_POLL})"
SETTLED = "(window.__settled ? window.__settled() : " + SETTLE.replace("res(true)", "res('')") + ")"


# what a test sets up in its Chrome that a replacement one (Chrome._recycle) gets too; every Emulation.* override as well
STICKY = {"Page.addScriptToEvaluateOnNewDocument", "Network.setBlockedURLs", "Network.setExtraHTTPHeaders",
          "Network.emulateNetworkConditions", "Network.setCacheDisabled", "Page.setBypassCSP"}
NAV_TIMEOUT = 15      # s: (TEST-SPEED review F5) a goto() whose page hasn't committed and loaded by then fails
READY_TIMEOUT = 15    # s: (review B1) and then one whose READY hasn't answered this long after (8 s of it in the page)
# t_37bf3e0b: HF_CHROME_LOG=<dir> gives each Chrome its own log there (Chrome's stderr, with LOG_ARGS), and a Chrome that
# wedges (Chrome._capture, before _recycle stops it) its process group, `sample`s and page there too; the logs of the
# Chromes that stopped cleanly are deleted. Unset: the same Chrome as before (stderr to /dev/null), nothing written.
LOG_ENV = "HF_CHROME_LOG"
LOG_ARGS = ["--enable-logging=stderr", "--v=0"]
SAMPLE_SECONDS = 2    # each `sample` of a wedged Chrome's process ...
SAMPLE_WAIT = 30      # s: ... ends by then (symbolicating included), or is killed
SAMPLE_MAX = 4        # processes sampled at most: the browser, the GPU process, then renderers

PROFILE_PREFIX = "hf-cdp-"
OWNER_FILE = "hf-owner"   # (TEST-SPEED) in each profile, written as soon as it's made: the pid of the process it's for
PUBLISH_LOCK = "hf-cdp.lock"   # (TEST-SPEED review N2) next to the profiles; not hf-cdp-*, so never swept
PUBLISH_WAIT = 10              # s: a Chrome() waits this long at most for a sweep's listing to end
GRACE = 5             # s: close() gives Chrome's process group this long to exit on SIGTERM before SIGKILL
ORPHAN_AGE = 600      # s: a left-behind Chrome or profile younger than this is never reaped (it may be a run starting up)
_PROFILE_ARG = re.compile(r"--user-data-dir=(\S*/" + PROFILE_PREFIX + r"[A-Za-z0-9_]+)")
_PROFILE_NAME = re.compile(PROFILE_PREFIX + r"[A-Za-z0-9_]+")


CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")   # on PATH (Linux, CI)


def find_chrome() -> str | None:
    """HF_CHROME when set (it must exist: a wrong one is no Chrome, never another), else the first of CHROME_PATHS, else
    the first of CHROME_NAMES on PATH."""
    named = os.environ.get("HF_CHROME")
    if named:
        return named if os.path.exists(named) else None
    found = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    return found or next((w for w in map(shutil.which, CHROME_NAMES) if w), None)


# --- every Chrome this process started, stopped however the process ends ---------------------------------------------

_LIVE: set[Chrome] = set()          # started and not yet stopped
_PREV: dict[int, object] = {}       # the SIGTERM / SIGHUP handler that was there before ours (chained to)


KINFO_PROC_SIZE = 648   # macOS: sizeof(struct kinfo_proc)
SZOMB = 5               # its kp_proc.p_stat for a zombie
UNKNOWN = -1            # (review round 3) _proc_stat(): neither sysctl nor ps could tell
# `ps` showing each process's environment after its command line (the environments are this user's processes' only), and
# every process: macOS -E / -ax, Linux procps the BSD-style e / -e (procps has no -E, and takes no e with -ax).
PS_ENV = "-E" if sys.platform == "darwin" else "e"
PS_ALL = "-ax" if sys.platform == "darwin" else "-e"
PS_WAIT = 30            # s: (review round 3) any `ps` here; one that hasn't answered by then has failed
_SYSCTL = None


def _ps(cmd: list[str]) -> str | None:
    """TEST-SPEED review round 3: the output of `cmd`, a ps that lists this process among others (pid first on each
    line), or None when it can't be taken for a whole answer: ps failed, didn't answer in PS_WAIT s, exited nonzero,
    or didn't list this process (a listing without its own caller isn't one; a query by pid names this process too,
    so its exit status can't mean just "none of those"). A failed one used to read as "nothing running"."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=PS_WAIT)
    except (OSError, subprocess.SubprocessError):
        return None
    me = str(os.getpid())
    if res.returncode != 0 or not any(ln.split(None, 1)[:1] == [me] for ln in res.stdout.splitlines()):
        return None
    return res.stdout


def _proc_stat(pid: int) -> int | None:
    """macOS: process `pid`'s state (sysctl kern.proc.pid: SZOMB for a zombie), None when there is no such process,
    UNKNOWN when neither sysctl (its answer not believed) nor ps (_ps: failed, or no answer in PS_WAIT s; it waited
    for ever, and a failed one said "no such process") can tell."""
    global _SYSCTL
    try:
        import ctypes
        _SYSCTL = _SYSCTL or ctypes.CDLL(None, use_errno=True).sysctl
        buf, size = ctypes.create_string_buffer(KINFO_PROC_SIZE), ctypes.c_size_t(KINFO_PROC_SIZE)
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)   # CTL_KERN, KERN_PROC, KERN_PROC_PID
        if _SYSCTL(mib, 4, buf, ctypes.byref(size), None, 0) == 0 and size.value in (0, KINFO_PROC_SIZE):
            if not size.value:
                return None
            # kp_proc.p_stat: after p_un (16), 2 pointers (16), p_flag (4); then kp_proc.p_pid at 40. (TEST-SPEED review
            # B4: the size alone doesn't show the layout is the one assumed: the pid read back at 40 is looked at too;
            # anything else, ps below. Still not a full check of the layout: the offsets of p_stat and p_pid only.)
            if int.from_bytes(buf.raw[40:44], sys.byteorder) == pid:
                return buf.raw[36]
    except (OSError, AttributeError, ImportError):
        pass
    out = _ps(["ps", "-o", "pid=,stat=", "-p", f"{pid},{os.getpid()}"])
    if out is None:
        return UNKNOWN
    st = next((f[1] for f in map(str.split, out.splitlines()) if len(f) >= 2 and f[0] == str(pid)), None)
    return None if st is None else SZOMB if st.startswith("Z") else 0


def _exited(proc: subprocess.Popen) -> bool:
    """Has our child `proc` exited? Without reaping it, unlike Popen.poll() (TEST-SPEED review N1: a starting Chrome
    polled that way was reaped the moment it died, and its helpers, left running, were then never signalled: the guards
    below won't signal a reaped leader's group, whose id may be another process's by then). Until it's reaped, an exited
    leader's pid, its group's id, can't be given to anyone else. (scripts/check.py has the same for its pytest runs.)
    Review round 3: a state that can't be told (_proc_stat UNKNOWN) is not an exit."""
    if proc.returncode is not None:
        return True
    if hasattr(os, "waitid"):
        try:
            return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    return _proc_stat(proc.pid) in (None, SZOMB)


def _group_alive(c: Chrome) -> bool:
    """Is any process of c's group still running? Only asked while the leader (our child) is unreaped, so the pgid can't
    have been reused (it may have exited: its helpers still count). macOS answers EPERM, not ESRCH, when all that's left
    is the leader's zombie; Linux succeeds, so there /proc is asked whether anything but a zombie is left."""
    if c.proc.returncode is not None:
        return False
    try:
        os.killpg(c.pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return _proc_group_running(c.pgid) is not False


def _proc_group_running(pgid: int) -> bool | None:
    """Linux: does a process of group `pgid` other than a zombie run (from /proc/<pid>/stat)? None where there's no
    /proc to ask (macOS), which callers take for "yes": the answer killpg gave."""
    try:
        pids = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()   # state, ppid, pgrp, ...
        except OSError:
            continue   # gone since
        if len(fields) >= 3 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
            return True
    return False


def _signal_group(c: Chrome, sig: int) -> None:
    if c.proc.returncode is not None:   # reaped: the pgid may be someone else's by now
        return
    try:
        os.killpg(c.pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


_STOPPING = threading.RLock()   # (TEST-SPEED review N3) who stops which Chrome, and its finishing (reentrant: a signal handler can run in it)
KILL_WAIT = 5    # s: (review B3) how long a stop waits for a group to be gone after SIGKILL; still there, it isn't stopped
STOP_WAIT = 15   # s: (review B2) past the grace, how long a stop waits for another thread's stop of the same Chrome
_HOLD: dict = {"depth": 0, "signals": []}   # (review B2) the main thread's depth in _holding(), the signals held meanwhile


@contextlib.contextmanager
def _holding():
    """TEST-SPEED review B2: a section no SIGTERM / SIGHUP handler of ours runs inside. Python runs every handler on the
    main thread, whichever thread the signal came to, so blocking SIGTERM for the main thread alone doesn't keep it out
    once any other thread runs (seen: the handler ran while it was blocked). One that comes meanwhile is held, and
    raised again as the (outermost) section is left. Inside one, close_all() would take _REGISTER a second time (it
    isn't reentrant: a deadlock), or finish a Chrome that the stop it interrupted is part-way through finishing."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    _HOLD["depth"] += 1
    try:
        yield
    finally:
        _HOLD["depth"] -= 1
        while not _HOLD["depth"] and _HOLD["signals"]:
            signal.raise_signal(_HOLD["signals"].pop(0))


def _stop(chromes: list[Chrome], grace: float = GRACE) -> None:
    """SIGTERM each Chrome's whole process group, give them `grace` seconds together, SIGKILL whatever is left, and
    finish each one whose group is gone (_finish: reap its leader, delete its profile, record it stopped). Safe to call
    twice, from a signal handler or from another thread. TEST-SPEED review N3: at the same time too (close_all() on a
    signal while a failed start closes itself): each Chrome is stopped by the first caller only, and the others wait
    for it to be stopped (its slot's descriptor was closed twice, and the second close could hit one opened
    meanwhile). Review B2: a signal handler's stop inside a stop on the same thread (it can't wait for the one it
    interrupted: that may never resume) stops the Chrome itself, and the interrupted one then finds it stopped: each
    Chrome is finished once, in a section no handler runs inside (_holding). A wait for another thread's stop that
    runs out (grace + STOP_WAIT) fails instead of returning as if it were done. Review B3: a Chrome whose group is still
    there KILL_WAIT s after SIGKILL isn't recorded stopped (_finish). Review round 3: nor one whose finishing raised
    part-way; it stays in _LIVE for a later stop (close_all()'s too), and the others are still finished (one error
    left them all). Each raises RuntimeError, once every Chrome has been seen to."""
    me = threading.get_ident()
    with _holding(), _STOPPING:
        todo = [c for c in chromes if not c._stopped and c.__dict__.setdefault("_stopper", me) == me]
        theirs = [c for c in chromes if not c._stopped and c._stopper != me]
        for c in todo + theirs:
            if c.__dict__.setdefault("_done", threading.Event()).is_set() and c in todo:
                c._done = threading.Event()   # (an interrupted or failed stop's, taken over)
        for c in todo:
            c.__dict__.pop("_stuck", None)
    chromes, failed, first = todo, [], None
    try:
        woken = False
        for c in chromes:
            c._stopping = True   # (TEST-SPEED) a Chrome() still in _connect() gives up on seeing this
            if getattr(c, "ws", None):
                # (TEST-SPEED) shut the DevTools socket down first: that wakes a thread blocked reading it (its call fails at
                # once). Closing it here as well, before that thread has run again, left it polling the closed (-1)
                # descriptor until the socket's 60 s timeout: a stopped run took a minute to unwind. Closed below instead.
                try:
                    c.ws.sock.shutdown(socket.SHUT_RDWR)
                    woken = True
                except OSError:
                    pass
            _signal_group(c, signal.SIGTERM)
        if woken:
            time.sleep(0.02)   # (the GIL: let those readers see the shutdown)
        left, deadline = chromes, time.monotonic() + grace
        while left := [c for c in left if _group_alive(c)]:
            if time.monotonic() >= deadline:
                for c in left:
                    _signal_group(c, signal.SIGKILL)
                killed = time.monotonic() + KILL_WAIT   # (N1: the whole group gone before its leader is reaped)
                while (left := [c for c in left if _group_alive(c)]) and time.monotonic() < killed:
                    time.sleep(0.05)
                break
            time.sleep(0.05)
        for c in chromes:
            with _holding(), _STOPPING:
                if c._stopped:   # (stopped meanwhile: by a signal handler's stop inside this one)
                    continue
                try:
                    why = _finish(c)
                except Exception as e:   # (round 3: the rest of it left to a later stop, the next Chrome still finished)
                    why, first = f"cdp: Chrome {c.proc.pid} (group {c.pgid}) not finished: {e!r}", first or e
                if why:
                    failed.append(why)
    finally:
        with _holding(), _STOPPING:
            for c in chromes:
                if not c._stopped:   # interrupted (an exception out of it) or not stopped: its claim goes, a later stop does it
                    c.__dict__.pop("_stopper", None)
                    c._done.set()
    bound = grace + STOP_WAIT
    for c in theirs:   # another thread is stopping these: stopped when it's done
        if not c._done.wait(bound):
            failed.append(f"cdp: Chrome {c.proc.pid} (group {c.pgid}): another thread's stop of it not done in {bound:.0f} s")
        elif not c._stopped and c.__dict__.get("_stuck"):
            failed.append(c._stuck)
    if lost := [c for c in theirs if c._done.is_set() and not c._stopped and "_stopper" not in c.__dict__
                and not c.__dict__.get("_stuck")]:
        try:
            _stop(lost, grace)   # (the stop that had them was interrupted)
        except RuntimeError as e:
            failed.append(str(e))
    if failed:
        raise RuntimeError("; ".join(failed)) from first


def _finish(c: Chrome) -> str:
    """The end of c's stop, by its caller holding _STOPPING (no signal handler inside). TEST-SPEED review B3: only once
    c's whole group has been seen gone are its leader reaped, the DevTools socket closed, the profile deleted, `stop`
    written in the ledger and the slot given back, then c marked stopped. '' then. Otherwise why not, a `stuck` line in
    the ledger, and all of it kept for a later stop and the ledger's watchdog: the leader unreaped (so its group can
    still be signalled by its id), the profile, the slot, the ledger's open `start`, its place in _LIVE (for close_all()).
    Review round 3: in stages, each marked done (c._finished) once it has been, never before: an exception part-way
    (no signal handler of ours runs in here, but an error, a Ctrl-C, can come) leaves c in _LIVE, and a later stop
    (close_all()'s included) does only what is left. It was out of _LIVE before its profile, `stop` and slot were done
    (close_all() never saw it again), its socket closed a second time by the next stop, and its `stop` marked written
    before it was (a stop interrupted there never wrote it). Not a transaction: an asynchronous exception (Ctrl-C)
    between a stage and its mark has that stage done again (a second socket close, or `stop` line: the ledger's readers
    take a Chrome stopped once whatever the count)."""
    done = c.__dict__.setdefault("_finished", set())
    if "gone" not in done:
        why = _left(c)
        if why:
            _ledger("stuck", c)
            c._stuck = f"cdp: Chrome {c.proc.pid} (group {c.pgid}) not stopped: {why}"
            return c._stuck
        done.add("gone")   # (and its leader reaped: _left)
    if "ws" not in done:
        if getattr(c, "ws", None):
            c.ws.close()
        done.add("ws")
    if "profile" not in done:
        shutil.rmtree(c.profile, ignore_errors=True)
        if c.__dict__.get("log") and not c.__dict__.get("wedged"):   # (HF_CHROME_LOG: a clean stop's log isn't kept)
            with contextlib.suppress(OSError):
                os.unlink(c.log)
        done.add("profile")
    if "stop" not in done:
        _ledger("stop", c)
        done.add("stop")
    _release_slot(c)   # (once, however often it runs: the descriptor is taken off c, then closed)
    _LIVE.discard(c)
    c._stopped = True
    c._done.set()
    return ""


def _left(c: Chrome) -> str:
    """What of c's browser still runs, '' when nothing does: seen, not assumed (review B3)."""
    if c.proc.returncode is None:   # our child, unreaped: its pid, the group's id, is still its own
        if _group_alive(c):
            return f"its process group is still there {KILL_WAIT} s after SIGKILL"
        try:
            c.proc.wait(KILL_WAIT)   # (a zombie by now: at once)
        except subprocess.TimeoutExpired:
            return "its main process didn't end"
        return ""
    # reaped before this stop could look (not by cdp: it reaps only above, and _finish doesn't ask again once it has):
    # its group's id may be another process's by now, so it isn't looked at; what still names its profile on its
    # command line (as Chrome's processes do) is
    pids = _naming(c.profile)
    return "" if pids == [] else "ps failed: its processes can't be looked for" if pids is None else \
        f"process(es) {pids} naming its profile still running"


def _naming(profile: str) -> list[int] | None:
    """The live processes (not this one) whose command line names `profile`; None when ps fails (review round 3: _ps;
    a nonzero exit with nothing on stdout read as "none", and the profile of a Chrome still running was deleted)."""
    out = _ps(["ps", "-axww", "-o", "pid=,stat=,command="])
    if out is None:
        return None
    return [int(f[0]) for f in (ln.split(None, 2) for ln in out.splitlines())
            if len(f) == 3 and f[0].isdigit() and int(f[0]) != os.getpid() and not f[1].startswith("Z") and profile in f[2]]


_CLOSING = False                     # close_all() has run: a Chrome registered after it is stopped by its own __init__
_REGISTER = threading.Lock()         # a Chrome's registration and close_all()'s look at _LIVE never interleave


def close_all() -> None:
    """Stop every Chrome this process still has (atexit, and SIGTERM / SIGHUP)."""
    global _CLOSING
    with _holding(), _REGISTER:   # (review B2: a signal meanwhile is handled after, not inside, holding _REGISTER)
        _CLOSING = True
        live = list(_LIVE)
    _stop(live)


def _on_signal(signum, frame):
    if _HOLD["depth"] and threading.current_thread() is threading.main_thread():
        _HOLD["signals"].append(signum)   # (review B2: inside a section it mustn't run in: raised again as that's left)
        return
    try:
        close_all()
    except Exception as e:   # (review B3: a Chrome that couldn't be stopped is said; the signal still does what it did)
        print(f"cdp: {e}", file=sys.stderr, flush=True)
    prev = _PREV.get(signum)
    if callable(prev):
        return prev(signum, frame)
    signal.signal(signum, signal.SIG_DFL)   # then die of the signal as if it had never been caught (exit 128 + signum)
    os.kill(os.getpid(), signum)
    os._exit(128 + signum)


def _install_handlers() -> None:
    """SIGTERM / SIGHUP close the Chromes first, then do what they did before: kill the process (the default) or run the
    handler someone installed. Left alone when ignored (nohup) or set outside Python. SIGINT is Python's own: Ctrl-C
    raises KeyboardInterrupt, which unwinds the `with Chrome()` blocks, and atexit closes the rest."""
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGTERM, signal.SIGHUP):
        cur = signal.getsignal(sig)
        if sig not in _PREV and (cur is signal.SIG_DFL or (callable(cur) and cur is not _on_signal)):
            _PREV[sig] = cur
            signal.signal(sig, _on_signal)


atexit.register(close_all)
_install_handlers()


# --- what a run killed with SIGKILL left behind ------------------------------------------------------------------------

_swept = False
_SWEEP_LOCK = threading.Lock()


def _seconds(etime: str) -> int:
    """ps's elapsed time, [[dd-]hh:]mm:ss, in seconds."""
    days, _, hms = etime.rpartition("-")
    s = 0
    for part in hms.split(":"):
        s = s * 60 + int(part)
    return s + int(days or 0) * 86400


def reap_orphans(age: float | None = None, tmp: str | Path | None = None) -> tuple[int, int]:
    """Kill the headless Chromes whose run died without closing them, and delete the hf-cdp-* profiles in $TMPDIR that
    no process uses. One `ps`. A Chrome (all the processes sharing one hf-cdp-* profile) is reaped only when none of
    them has a live parent outside it (the main process was re-parented to launchd, PPID 1) and it is older than `age`
    seconds (ORPHAN_AGE): a Chrome with a live owner, a concurrent run's, is never touched. TEST-SPEED review B4: each
    process of it looked at again just before (_still), and signalled only if it is still the one the `ps` saw (the
    same start time, group and profile, still orphaned): a pid given to another process since is left alone. Review
    round 3: process by process, its main process first (then nothing restarts a helper), never by group: a group id
    read off a listing is a snapshot's, not a leader this process holds (only a parent's unreaped child pins its
    group's id: _signal_group), and a group signal reached members nothing had looked at. So a process of the group
    that doesn't carry --user-data-dir=<profile> is left (and, naming the profile, keeps it in use: not deleted). A
    `ps` that fails (_ps) touches nothing. Review round 4: a reaped Chrome's profile is deleted only once every pid it
    was sent SIGKILL for is seen gone *and* a fresh, whole look after that (_users: every user's processes, one `ps`)
    finds nothing naming it. Seeing only the signalled ones gone deleted it while a process it hadn't signalled still
    named it: a helper without --user-data-dir (left by PID-only reaping), one whose SIGKILL was refused
    (PermissionError), one _still didn't verify; and a Chrome none of whose SIGKILLs was sent counted as gone (nothing
    to see). Now such a profile is kept, and so is every reaped one when that last look fails or is cut short. What
    stays open: the moment between _still's look and each signal (macOS has no pid handle; a pid reused within it is
    signalled), ps's start time is to the second, and a process that starts naming the profile between the last look
    and the deletion. Returns (processes signalled, profile dirs deleted). `tmp`: where the profiles are (tests),
    $TMPDIR by default."""
    age = ORPHAN_AGE if age is None else age
    # TEST-SPEED: the leftover profiles are listed before the `ps` below, and one whose owner (OWNER_FILE) is alive is
    # never deleted. A profile made after the `ps` (another process's Chrome(), its Chrome not on the list yet) used to be
    # swept with the leftovers when `age` was 0 (the cleanup tests'): with the tests on 4 workers that Chrome then never
    # found its profile ("Chrome did not start" in whatever test was starting one). TEST-SPEED review N2: and they are
    # listed holding PUBLISH_LOCK exclusively, so no profile is listed between being made and having its owner written
    # (_new_profile() holds it shared meanwhile). Not had in 2 s (a start paused in between): a profile without an
    # owner is kept this time.
    tmp = Path(tmp or tempfile.gettempdir())
    lock = _publishing(tmp, exclusive=True, wait=2)
    try:
        listed = list(tmp.glob(PROFILE_PREFIX + "*"))
    finally:
        if lock is not None:
            os.close(lock)
    out = _ps(["ps", "-axww", "-o", "pid=,ppid=,pgid=,etime=,lstart=,command="])
    if out is None:   # (round 3: nothing known orphaned, nor any profile unused)
        return 0, 0
    chromes: dict[str, list[tuple[int, int, int, int, str]]] = {}   # profile -> (pid, ppid, pgid, age, start) of its processes
    in_use: set[str] = set()                                           # every hf-cdp-* name any command line mentions
    for line in out.splitlines():
        f = line.split(None, 9)   # (lstart: 5 words)
        if len(f) < 10:
            continue
        in_use.update(_PROFILE_NAME.findall(f[9]))
        m = _PROFILE_ARG.search(f[9])
        if m and "chrom" in f[9].split(" --", 1)[0].lower():
            try:
                chromes.setdefault(m.group(1), []).append((int(f[0]), int(f[1]), int(f[2]), _seconds(f[3]),
                                                           " ".join(f[4:9])))
            except ValueError:
                continue
    killed: list[int] = []
    reaped: dict[str, list[int]] = {}   # profile -> the pids of it signalled (round 4: one at least)
    for profile, procs in chromes.items():
        pids = {p[0] for p in procs}
        owners = {p[1] for p in procs} - pids
        if owners != {1} or max(p[3] for p in procs) < age:
            continue
        still = _still(procs, profile)
        if still is None:   # (couldn't look again: left for the next sweep)
            continue
        sent = []
        for pid, _ppid, _pgid, _age, _start in sorted(procs, key=lambda p: p[0] != p[2]):   # (its main process first)
            if pid not in still:   # (gone since, or its pid someone else's now)
                continue
            try:
                os.kill(pid, signal.SIGKILL)   # (round 3: by its pid only, never its group)
                sent.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        killed += sent
        if sent:   # (round 4: none sent, it isn't reaped: nothing it signalled to see gone)
            reaped[profile] = sent
    deadline, live = time.monotonic() + 3, set(killed)
    while live and time.monotonic() < deadline:   # launchd reaps them; let them go before deleting their profiles
        for pid in list(live):
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):   # (gone; or its pid is another user's process now)
                live.discard(pid)
        if live:
            time.sleep(0.05)
    gone = [p for p, sent in reaped.items() if not live & set(sent)]   # (round 3: the others' are kept, still in use)
    users = _users() if gone else {}   # (round 4: and then nothing names it, in a whole look; failed: all kept)
    gone = [] if users is None else [p for p in gone if Path(p).name not in users]
    in_use -= {Path(p).name for p in gone}
    dirs = {Path(p) for p in gone}
    now = time.time()
    for d in listed:
        try:
            if (d.name not in in_use and d.is_dir() and not d.is_symlink() and now - d.stat().st_mtime >= age
                    and not _owner_alive(d) and (lock is not None or (d / OWNER_FILE).exists())):
                dirs.add(d)
        except OSError:
            continue
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
    if killed or dirs:
        print(f"cdp: SIGKILLed {len(killed)} process(es) of {len(reaped)} orphaned headless Chrome(s) ({len(gone)} seen "
              f"gone, nothing naming its profile), deleted {len(dirs)} leftover {PROFILE_PREFIX}* profile(s)",
              file=sys.stderr, flush=True)
    return len(killed), len(dirs)


def _users() -> dict[str, list[int]] | None:
    """TEST-SPEED review round 4: {hf-cdp-* name: the live processes (not this one) whose command line names it}, from
    one whole `ps` of every user's processes (the sweep's own `in_use`, looked at again); None when it can't be taken
    for a whole one (_ps). A line whose pid can't be read still counts for the names on it (as pid -1): what can't be
    read isn't taken for nothing."""
    out = _ps(["ps", "-axww", "-o", "pid=,stat=,command="])
    if out is None:
        return None
    users: dict[str, list[int]] = {}
    for line in out.splitlines():
        f = line.split(None, 2)
        if f[:1] == [str(os.getpid())] or (len(f) > 1 and f[1].startswith("Z")):
            continue
        for name in _PROFILE_NAME.findall(line):
            users.setdefault(name, []).append(int(f[0]) if f[0].isdigit() else -1)
    return users


def _still(procs: list[tuple[int, int, int, int, str]], profile: str) -> set[int] | None:
    """TEST-SPEED review B4: those of an orphaned Chrome's processes (pid, ppid, pgid, age, start time: reap_orphans'
    `ps`) that are, looked at again now (one `ps -p` of them), still the same: the same start time and group, the same
    profile on the command line, and still no parent but launchd or one of them. What is left is the moment between
    this look and the signal (not closed: macOS has no pid handle; one look for all of them, so a later signal is a
    little further from it), and a start time to the second (ps's lstart): a pid reused within the same second, in
    the same group, naming the same profile, would pass. None when ps fails (_ps)."""
    pids = {p[0] for p in procs}
    out = _ps(["ps", "-ww", "-o", "pid=,ppid=,pgid=,lstart=,command=", "-p", ",".join(map(str, sorted(pids | {os.getpid()})))])
    if out is None:
        return None
    was = {p[0]: p for p in procs}
    same = set()
    for line in out.splitlines():
        f = line.split(None, 8)
        try:
            pid, ppid, pgid, start, cmd = int(f[0]), int(f[1]), int(f[2]), " ".join(f[3:8]), f[8]
        except (IndexError, ValueError):
            continue
        m = _PROFILE_ARG.search(cmd)
        if pid in was and pgid == was[pid][2] and start == was[pid][4] and (ppid == 1 or ppid in pids) \
                and m and m.group(1) == profile:
            same.add(pid)
    return same


def _publishing(tmp: Path, exclusive: bool, wait: float) -> int | None:
    """TEST-SPEED review N2: flock `tmp`/PUBLISH_LOCK, shared while a Chrome() makes its profile and writes its owner
    in it, exclusive while a sweep lists the profiles; the locked descriptor (closing it unlocks), or None when not had
    within `wait` s."""
    import fcntl
    fd = os.open(tmp / PUBLISH_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.01)


def _new_profile(tmp: str | Path | None = None) -> str:
    """A new hf-cdp-* profile in `tmp` ($TMPDIR), its OWNER_FILE (this process) written before any sweep can list it."""
    tmp = Path(tmp or tempfile.gettempdir())
    lock = _publishing(tmp, exclusive=False, wait=PUBLISH_WAIT)
    if lock is None:
        raise RuntimeError(f"cdp: no profile made: {tmp / PUBLISH_LOCK} held for {PUBLISH_WAIT} s by a sweep")
    try:
        d = tempfile.mkdtemp(prefix=PROFILE_PREFIX, dir=tmp)
        try:
            (Path(d) / f"{OWNER_FILE}.new").write_text(str(os.getpid()))
            os.replace(Path(d) / f"{OWNER_FILE}.new", Path(d) / OWNER_FILE)   # (whole, or not there)
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return d
    finally:
        os.close(lock)


def _owner_alive(profile: Path) -> bool:
    """Is the process a profile was made for (its OWNER_FILE) still running? (A reused pid keeps it: kept, not lost.)"""
    try:
        pid = int((profile / OWNER_FILE).read_text())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _sweep_once() -> None:
    """reap_orphans() before this process's first Chrome (the other threads starting one wait for it)."""
    global _swept
    with _SWEEP_LOCK:
        if not _swept:
            _swept = True
            reap_orphans()


# --- TEST-SPEED: at most SLOTS headless Chromes at once, machine-wide -----------------------------------------------------
# Every Chrome() first takes one of SLOTS lock files (flock) in SLOT_DIR and holds it until it has stopped; Chrome itself
# holds it too (the descriptor is passed down), so a Chrome whose run was SIGKILLed keeps its slot until it's reaped.
# The locks are the kernel's: they go with the processes holding them, whatever way those end. Any number of processes
# (pytest-xdist workers, the gates, another worktree's run) share the budget; a Chrome() over it waits for a slot.
# HF_CHROMES sets the budget (default and at most 4: this Mac mini's rule; set it the same for every process, or lower).
# SLOT_DIR is one fixed path, not under $TMPDIR: a cron job or a launchd agent has another $TMPDIR (or none), and the
# budget is the machine's (HF_CHROME_SLOT_DIR: tests of the slots themselves); not hf-cdp-*: reap_orphans() deletes those.
MAX_CHROMES = 4   # this Mac mini's rule: a ceiling (TEST-SPEED review), not a default an HF_CHROMES=8 could lift
SLOTS = max(1, min(MAX_CHROMES, int(os.environ.get("HF_CHROMES") or MAX_CHROMES)))
SLOT_DIR = Path(os.environ.get("HF_CHROME_SLOT_DIR") or "/tmp/hermes-finance-chrome-slots")
SLOT_WARN = 60        # s: waiting longer than this for a slot prints one line (who holds them: `lsof +D <SLOT_DIR>`)
# A thread that already has a Chrome and starts another (a module's Chrome kept for its tests, a second "device") waits
# while holding a slot. When every slot is held by such a thread, in any process, none can ever get one: a deadlock
# (two check.py runs at once, each with such tests on its workers). It is seen by the waiters themselves (each holds a
# flock on SLOT_DIR/wait-<pid>-<thread> while it waits, the file saying how many slots it holds) and one of them, the
# same whichever looks, gives up with a RuntimeError: a failed test, not a run that never ends.
_HELD: dict[int, int] = {}           # thread ident -> slots it holds (its Chromes not yet stopped)
_HELD_LOCK = threading.RLock()   # (reentrant: _release_slot() can run in a signal handler)
_PARKED = 0   # threads of this process inside parked(): its pool threads look for a deadlock too, holding no slot


def _acquire_slot() -> int:
    import fcntl
    SLOT_DIR.mkdir(exist_ok=True)
    me = threading.get_ident()
    with _HELD_LOCK:
        held = _HELD.get(me, 0)
    t0, warned, mark, looked, seen = time.monotonic(), False, None, 0.0, 0
    try:
        while True:
            if _CLOSING:   # close_all() has run (a SIGTERM): this process is stopping, not starting another Chrome
                raise RuntimeError("the process is stopping its Chromes")
            for i in range(SLOTS):
                fd = os.open(SLOT_DIR / f"slot-{i}", os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    continue
                with _HELD_LOCK:
                    _HELD[me] = held + 1
                return fd
            if (held or _PARKED) and time.monotonic() - looked >= 1:   # (looked for once a second, and must be seen twice)
                looked = time.monotonic()
                mark = mark or _mark_waiting(held)
                seen = seen + 1 if _deadlocked(mark[0]) else 0
                if seen >= 2:
                    raise RuntimeError(f"cdp: deadlock: all {SLOTS} headless Chrome slots are held by threads that each wait "
                                       f"for another one (a test holding a Chrome while it starts a second, in several "
                                       f"runs at once); this one gives up (HF_CHROMES; lsof +D {SLOT_DIR})")
            if not warned and time.monotonic() - t0 > SLOT_WARN:
                warned = True
                print(f"cdp: waiting for a headless Chrome slot: all {SLOTS} in use (HF_CHROMES; lsof +D {SLOT_DIR})",
                      file=sys.stderr, flush=True)
            time.sleep(0.05)
    finally:
        if mark:
            with contextlib.suppress(OSError):
                os.unlink(mark[0])
            os.close(mark[1])


@contextlib.contextmanager
def parked() -> Iterator[None]:
    """The calling thread now waits on threads it started (a ThreadPoolExecutor whose work starts Chromes) while it
    holds its own slots: the v0.2.4 Nightly hang. A worker kept its module's Chrome and ran a layout gate whose Chrome
    started on a pool thread; the pool threads hold no slot, so the waiters' count never added up to a deadlock and
    every run with HF_CHROMES=2 hung for good. While parked, SLOT_DIR/park-<pid>-<thread> (flocked) says how many slots
    it holds, _deadlocked() counts them, and this process's pool threads look for a deadlock although they hold none.
    A parked thread is never the one to give up (it isn't waiting for a slot); one of the waiters is. Holding no slot,
    it changes nothing."""
    global _PARKED
    with _HELD_LOCK:
        held = _HELD.get(threading.get_ident(), 0)
    if not held:
        yield
        return
    mark = _mark_waiting(held, "park")
    with _HELD_LOCK:
        _PARKED += 1
    try:
        yield
    finally:
        with _HELD_LOCK:
            _PARKED -= 1
        with contextlib.suppress(OSError):
            os.unlink(mark[0])
        os.close(mark[1])


def _mark_waiting(held: int, kind: str = "wait") -> tuple[Path, int]:
    """SLOT_DIR/<kind>-<pid>-<thread>, flocked while this thread waits (for a slot, or parked() on its pool) holding
    `held` of them."""
    import fcntl
    path = SLOT_DIR / f"{kind}-{os.getpid()}-{threading.get_ident()}"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, str(held).encode())
    return path, fd


def _deadlocked(mine: Path) -> bool:
    """Every slot is taken and the waiting threads (their wait-* files still flocked) and the parked ones (park-*: each
    waits on its pool) hold all of them; True for one of the slot waiters only (the last wait-* by name), so exactly
    one gives up."""
    import fcntl
    if slots_in_use() < SLOTS:
        return False
    waiting: list[tuple[str, int]] = []
    for p in [*SLOT_DIR.glob("wait-*"), *SLOT_DIR.glob("park-*")]:
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                continue   # nobody holds it: its waiter got its slot, or is gone
            except OSError:
                pass
            try:
                waiting.append((p.name, int(os.read(fd, 16) or 0)))
            except ValueError:
                continue   # being written
        finally:
            os.close(fd)
    waiters = [name for name, _ in waiting if name.startswith("wait-")]
    return sum(n for _, n in waiting) >= SLOTS and bool(waiters) and max(waiters) == mine.name


def _release_slot(c: Chrome) -> None:
    with _HELD_LOCK:   # (N3: taken from the Chrome and counted off at once, by one caller)
        fd, c.slot = getattr(c, "slot", None), None
        if fd is None:
            return
        t = getattr(c, "thread", None)
        if _HELD.get(t, 0) > 1:
            _HELD[t] -= 1
        else:
            _HELD.pop(t, None)
    os.close(fd)


# --- TEST-SPEED: the ledger: every Chrome's start and stop, for whoever started this process ------------------------------
# HF_CHROME_LEDGER=<file> (scripts/check.py sets it for everything it runs): each Chrome started and stopped here appends
# one line there: `start|stop <epoch> <chrome pid> <pgid> <this pid> <profile>`. It's the run's own record of which
# Chromes are its (a machine-wide count can't tell a concurrent run's live Chromes from a leak), and what check.py's
# watchdog stops when check.py itself was killed before it could.
# TEST-SPEED review F2: several ledgers (os.pathsep between them), each line in every one: a test keeping a ledger of its
# own (tests/test_cdp_budget.py) adds it with ledger_env(), and its Chromes stay in the run's too, for its watchdog.
def _ledger(event: str, c: Chrome) -> None:
    line = f"{event} {time.time():.3f} {c.proc.pid} {c.pgid} {os.getpid()} {c.profile}\n".encode()
    for path in filter(None, os.environ.get("HF_CHROME_LEDGER", "").split(os.pathsep)):
        with contextlib.suppress(OSError):
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)


def ledger_env(path: str | Path) -> str:
    """HF_CHROME_LEDGER's value that records this process's Chromes in `path` as well as where they're recorded now."""
    return os.pathsep.join(filter(None, [os.environ.get("HF_CHROME_LEDGER"), str(path)]))


def slots_in_use() -> int:
    """How many of the SLOTS are taken right now (by any process)."""
    import fcntl
    n = 0
    for i in range(SLOTS):
        try:
            fd = os.open(SLOT_DIR / f"slot-{i}", os.O_RDWR)
        except FileNotFoundError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            n += 1
        finally:
            os.close(fd)
    return n


class Chrome:
    """One headless Chrome with one page. js() evaluates an expression (awaiting promises) and returns its
    JSON value; problems() drains JS errors, console errors and CSP violations seen since the last call."""

    def __init__(self, chrome: str | None = None):
        self.path = chrome or find_chrome()
        if not self.path:
            raise RuntimeError("Chrome is not installed")
        if os.environ.get("HF_NO_CHROME"):   # TEST-SPEED: scripts/check.py's no-browser phase (a misfiled test fails loudly)
            raise RuntimeError(f"this test starts a headless Chrome but runs in check.py's no-browser phase "
                               f"(HF_NO_CHROME): add its file to scripts/check.py BROWSER")
        _sweep_once()
        _install_handlers()   # a no-op unless this module was first imported off the main thread
        self._stopped = False
        self._done = threading.Event()
        self.thread = threading.get_ident()
        self.slot = _acquire_slot()   # TEST-SPEED: waits while HF_CHROMES Chromes are already running
        # TEST-SPEED: from the moment Chrome is started until it's in _LIVE, a SIGTERM / SIGHUP waits (on the main thread,
        # where Python runs its handlers), and close_all() on another thread can't look at _LIVE in between: a Chrome
        # can't be orphaned by a signal landing between the two (seen once: a run's pytest worker, stopped mid-start).
        # TEST-SPEED review N3: the profile is made in there too (a signal between making it and starting Chrome left it
        # behind), and not at all once close_all() has run. Review B2: the mask alone doesn't keep the handler out (any
        # other thread takes the signal, and Python runs the handler here all the same: close_all() then waited for
        # the _REGISTER this thread holds, for ever); _holding() does. Review round 4: the browser itself isn't given
        # that block. A child starts with the mask of the thread that forked it and keeps it through exec, so a
        # browser started here (on the main thread) had SIGTERM / SIGHUP blocked: a SIGTERM to its group stayed
        # pending, and every stop of it waited out GRACE for SIGKILL (measured with a stand-in; Chrome clears its mask
        # as it starts, so it hid this). Its Popen runs with the mask this thread had before (no preexec_fn: not in a
        # threaded process): a signal meanwhile is still held by _holding(), and the block is back on after it.
        masked = {signal.SIGTERM, signal.SIGHUP} if threading.current_thread() is threading.main_thread() else set()
        if masked:
            was = signal.pthread_sigmask(signal.SIG_BLOCK, masked)
        try:
            with _holding(), _REGISTER:
                try:
                    if _CLOSING:
                        raise RuntimeError("the process is stopping its Chromes")
                    self.profile = _new_profile()   # (N2: its owner in it before a concurrent sweep can list it)
                    argv = [self.path, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                            "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                            "--disable-component-update", "--disable-sync", "--disable-extensions",
                            "--disable-default-apps", "--hide-scrollbars", "--force-color-profile=srgb", "--mute-audio",
                            "--password-store=basic", "--use-mock-keychain", "about:blank"]
                    err, self.log = subprocess.DEVNULL, None
                    if os.environ.get(LOG_ENV):   # (t_37bf3e0b) its own log, kept only if it wedges
                        os.makedirs(os.environ[LOG_ENV], exist_ok=True)
                        self.log = os.path.join(os.environ[LOG_ENV], f"chrome-{Path(self.profile).name}.log")
                        argv[-1:-1] = LOG_ARGS
                        err = open(self.log, "wb")
                    if masked:
                        signal.pthread_sigmask(signal.SIG_SETMASK, was)
                    try:
                        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=err, start_new_session=True,
                                                     pass_fds=(self.slot,))
                    finally:
                        if err is not subprocess.DEVNULL:
                            err.close()   # (Chrome has its own copy)
                        if masked:
                            signal.pthread_sigmask(signal.SIG_BLOCK, masked)
                except BaseException:
                    if getattr(self, "profile", None):
                        shutil.rmtree(self.profile, ignore_errors=True)
                    if getattr(self, "log", None):
                        with contextlib.suppress(OSError):
                            os.unlink(self.log)
                    _release_slot(self)
                    raise
                self.pgid = self.proc.pid   # start_new_session: Chrome leads a new session and process group (pgid == its pid)
                _LIVE.add(self)
                _ledger("start", self)
        finally:
            if masked:
                signal.pthread_sigmask(signal.SIG_SETMASK, was)   # a SIGTERM that came meanwhile is handled here
        try:
            self._connect()
        except BaseException as e:
            self.close()
            if "(it exited)" in str(e):   # (reaped by close() only now: N1)
                raise RuntimeError(f"Chrome did not start (it exited: {self.proc.returncode})") from None
            raise

    def _connect(self) -> None:
        self.n = 0
        self.events: list[dict] = []
        self._sticky: list[tuple[str, dict]] = []   # the test's own setup, for a replacement Chrome (_recycle)
        self._recording = False
        port_file = Path(self.profile) / "DevToolsActivePort"
        deadline = time.time() + 30
        page = None
        while time.time() < deadline:
            if getattr(self, "_stopping", False) or _exited(self.proc):
                # (TEST-SPEED) stopped meanwhile (close_all() on SIGTERM) or exited: no port will come. Waiting out the
                # 30 s here held a stopping run up for 30 s (its gate threads were each starting a Chrome). (Review N1:
                # exited, not reaped: its group, helpers and all, is stopped by close() before that)
                break
            try:
                port = int(port_file.read_text().split()[0])
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)  # not urlopen: tests block that
                c.request("GET", "/json")
                tabs = json.loads(c.getresponse().read())
                c.close()
                page = next(t for t in tabs if t["type"] == "page")
                break
            except (OSError, ValueError, StopIteration, IndexError):
                time.sleep(0.1)
        if page is None or getattr(self, "_stopping", False):   # (review N3: stopped just as DevTools answered, too)
            raise RuntimeError("Chrome did not start" + (" (stopped while starting)" if getattr(self, "_stopping", False) else
                                                         " (it exited)" if _exited(self.proc) else ""))
        self.ws = WebSocket(page["webSocketDebuggerUrl"])
        if getattr(self, "_stopping", False):
            self.ws.close()
            raise RuntimeError("Chrome did not start (stopped while starting)")
        for method in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
            self.call(method)
        self.call("Network.setBlockedURLs", urls=BLOCKED)
        # the host's own Reduce motion never decides a test (the macOS CI runner has it on: the sync ring and the
        # count-up never ran there); a test about reduced motion asks for it with Emulation.setEmulatedMedia
        self.call("Emulation.setEmulatedMedia", features=NO_REDUCE)
        self.call("Page.addScriptToEvaluateOnNewDocument", source=WATCH)
        self.call("Page.addScriptToEvaluateOnNewDocument", source=PEND)   # TEST-SPEED: settled() / goto()
        self.width = self.height = 0
        self.mobile = False
        self._recording = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        """Stop Chrome and all its helpers (its process group) and delete the profile. Idempotent."""
        _stop([self])

    def call(self, method: str, _deadline: float | None = None, **params):
        """One DevTools command, its result. `_deadline` (time.monotonic()): fail (RuntimeError) if no answer by then.
        TEST-SPEED review B1: sending the command and every frame of every message read meanwhile count (it was only
        looked at between messages, with nothing buffered: a frame half-read, a fragment, a ping, a stream of events
        went on past it). After a failed one the connection is as WebSocket leaves it: still in use when nothing of a
        message was in hand (a late answer is dropped with the other ids'), otherwise closed (every later call fails
        at once). Without a deadline, each read waits the socket's 60 s, as it always did. Review round 3: the deadline
        is looked at before every frame, buffered ones too; the work on one frame or on the finished message (its
        decoding, json.loads) is CPU time it doesn't interrupt, so a call can end that much after it."""
        if self.__dict__.get("_recording") and (method.startswith("Emulation.") or method in STICKY):
            if method.startswith("Emulation."):   # an override: the latest one is what's in force
                self._sticky = [s for s in self._sticky if s[0] != method]
            self._sticky.append((method, params))
        self.n += 1
        mine = self.n
        try:
            self.ws.send(json.dumps({"id": mine, "method": method, "params": params}), _deadline)
            while True:
                m = json.loads(self.ws.recv(_deadline))
                if "method" in m:
                    self.events.append(m)
                elif m.get("id") == mine:
                    if "error" in m:
                        raise RuntimeError(f"{method}: {m['error']}")
                    return m.get("result", {})
        except WebSocketTimeout as e:
            if _deadline is None:
                raise
            raise RuntimeError(f"{method}: no answer by its deadline"
                               + ("; the DevTools connection is closed (part of a message was in hand)" if e.torn else "")) from None

    def js(self, expr: str, _deadline: float | None = None):
        r = self.call("Runtime.evaluate", _deadline, expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError(f"js failed: {d.get('text')} {d.get('exception', {}).get('description', '')[:300]}")
        return r.get("result", {}).get("value")

    def size(self, width: int, height: int, mobile: bool | None = None, scale: float | None = None) -> None:
        mobile = width < 760 if mobile is None else mobile
        self.width, self.height, self.mobile = width, height, mobile
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=scale or (2 if mobile else 1), mobile=mobile)

    def goto(self, url: str, ready: bool = True) -> None:
        """_goto(), and if it fails (the page didn't load, wasn't drawn, or DevTools stopped answering), this Chrome is
        replaced before the error goes on: slow-mode run 4 (t_d2b88168) had one page wedge its renderer, and every later
        goto() in that Chrome failed with "Page.navigate: no answer by its deadline" (85 tests, 50 minutes). A
        navigation the page itself refused (Page.navigate's errorText) leaves it as it is."""
        try:
            self._goto(url, ready)
        except RuntimeError as e:
            # (a Chrome started here; tests of goto()'s own deadlines drive a stand-in made without __init__)
            if "the navigation failed:" not in str(e) and "_sticky" in self.__dict__:
                self._recycle(str(e))
            raise

    def _recycle(self, why: str) -> None:
        """Stop this Chrome and go on as a fresh one: the same object (the test's fixture holds it), the slot it frees,
        and the setup the test gave the old one (its Emulation overrides, scripts for new documents, size)."""
        sticky, size = list(self._sticky), (self.width, self.height, self.mobile)
        self._capture(why)   # (HF_CHROME_LOG: what it was doing, looked at before it's stopped)
        self.close()
        fresh = Chrome(self.path)
        with _REGISTER:   # (a stop of every Chrome looks at _LIVE under it: never sees neither, or both)
            _LIVE.discard(fresh)
            self.__dict__ = fresh.__dict__
            _LIVE.add(self)
        self._recording = False
        try:
            for method, params in sticky:
                self.call(method, **params)
        finally:
            self._sticky, self._recording = sticky, True
        self.width, self.height, self.mobile = size
        sys.stderr.write(f"cdp: a Chrome was replaced after: {why[:200]}\n")

    def _capture(self, why: str) -> None:
        """HF_CHROME_LOG only (t_37bf3e0b): what this Chrome was doing when it stopped answering, written next to its
        log, which is then kept: <log>.wedge.txt (why, the test, its pages as the browser's own /json lists them, and
        its process group from ps) and <log>.sample-<pid>.txt (macOS `sample`, SAMPLE_SECONDS each, SAMPLE_WAIT at
        most: the browser, its GPU process, its renderers). Every step is bounded, and none raises."""
        log = self.__dict__.get("log")
        if not log or self.__dict__.get("_stopped", True):
            return
        self.wedged = True
        base = log[:-len(".log")]
        out = [f"why: {why}", f"at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
               f"test: {os.environ.get('PYTEST_CURRENT_TEST')}", f"chrome: pid {self.proc.pid}, group {self.pgid}"]
        try:   # (the browser's HTTP endpoint, not the page's DevTools session: that's what stopped answering)
            port = int((Path(self.profile) / "DevToolsActivePort").read_text().split()[0])
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c.request("GET", "/json/list")
            out += [f"page: {t.get('type')} {t.get('url')}" for t in json.loads(c.getresponse().read())]
            c.close()
        except Exception as e:
            out.append(f"pages: not listed ({e!r})")
        group: list[tuple[int, float, str]] = []
        try:
            ps = subprocess.run(["ps", PS_ALL, "-o", "pid=,ppid=,pgid=,stat=,%cpu=,rss=,etime=,command="],
                                capture_output=True, text=True, timeout=PS_WAIT).stdout
            out.append("ps (pid ppid pgid stat %cpu rss etime command):")
            for ln in ps.splitlines():
                f = ln.split(None, 7)
                if len(f) == 8 and f[2] == str(self.pgid):
                    out.append("  " + ln.strip())
                    group.append((int(f[0]), float(f[4]), f[7]))
        except Exception as e:
            out.append(f"ps: failed ({e!r})")
        # the browser, its GPU process, then the pages' renderers busiest first (not Chrome's own UI's or extensions')
        order = lambda p: (0 if p[0] == self.pgid else 1 if "--type=gpu-process" in p[2] else
                           2 if "--type=renderer" in p[2] and "--top-chrome-webui" not in p[2]
                           and "--extension-process" not in p[2] else 3, -p[1])
        picked = [p for p in sorted(group, key=order) if order(p)[0] < 3][:SAMPLE_MAX] if shutil.which("sample") else []
        samplers = []
        for pid, *_ in picked:
            try:
                samplers.append((pid, subprocess.Popen(["sample", str(pid), str(SAMPLE_SECONDS), "-file",
                                                        f"{base}.sample-{pid}.txt"], stdin=subprocess.DEVNULL,
                                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                                       start_new_session=True)))
            except OSError as e:
                out.append(f"sample {pid}: not started ({e!r})")
        end = time.monotonic() + SAMPLE_WAIT
        for pid, s in samplers:
            try:
                _, err = s.communicate(timeout=max(0.1, end - time.monotonic()))
                out.append(f"sample {pid}: exit {s.returncode} {err.decode(errors='replace').strip()[-300:]}")
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(s.pid, signal.SIGKILL)
                s.communicate()
                out.append(f"sample {pid}: killed after {SAMPLE_WAIT} s")
        try:
            Path(f"{base}.wedge.txt").write_text("\n".join(out) + "\n")
        except OSError:
            pass
        sys.stderr.write(f"cdp: a wedged Chrome's log and samples: {base}.*\n")

    def _goto(self, url: str, ready: bool = True) -> None:
        """Open `url` and wait until it's there, then (ready) until the page is drawn (READY). TEST-SPEED: no fixed pause
        (it was 0.15 s, which a busy machine can outlast): a new document is waited for until it has loaded (its PEND
        tracker is a new one), a new #hash in the same document until the page's own hashchange listener has run.
        TEST-SPEED review F5: the navigation by NAV_TIMEOUT, or it raises: one that fails (Page.navigate's errorText),
        never commits, or whose load / hashchange never comes (the page's waits race a timer, and an evaluation that
        doesn't answer in time fails too), instead of going on to READY on whatever page is there. Review B1: every
        DevTools message waited for counts against it (call()), and READY has READY_TIMEOUT more (it waits 8 s at
        most in the page, then two frames: a page whose frames never come no longer holds goto() up). So a goto()
        ends within NAV_TIMEOUT + READY_TIMEOUT, plus the CPU time of the frame or message in hand then (call()). The
        other calls (js(), settled(), screenshot()) have no deadline: only the socket's 60 s per read."""
        deadline = time.monotonic() + NAV_TIMEOUT
        try:
            old = self.js("window.__pend ? [window.__pend.doc, window.__pend.hash, location.hash] : null", deadline)
        except RuntimeError:   # the page is still navigating from an earlier call
            old = None
        nav = self.call("Page.navigate", deadline, url=url)
        if nav.get("errorText"):
            raise RuntimeError(f"goto {url}: the navigation failed: {nav['errorText']}")
        if nav.get("loaderId") or not old:   # a new document (a same-document navigation has no loader)
            # on the old page this never answers: the evaluation fails when the page goes away, and runs again on the new
            # one (2 s at most: a navigation that never commits)
            done = self._until(lambda ms: "(window.__pend && window.__pend.doc !== %s) ? (document.readyState === 'complete' || "
                               "new Promise((r) => { const t = setTimeout(() => r(false), %d); addEventListener('load', () => "
                               "{ clearTimeout(t); r(true); }, {once: true}); })) : new Promise((r) => setTimeout(() => r(false), %d))"
                               % (json.dumps(old[0] if old else None), ms, min(ms, 2000)), deadline)
        elif ("#" + url.partition("#")[2] if "#" in url else "") != old[2]:   # same document, another hash: handled
            # (a listener added now runs after the page's own)
            done = self._until(lambda ms: f"window.__pend.hash !== {old[1]} || new Promise((r) => {{ const t = setTimeout(() => "
                               f"r(false), {ms}); addEventListener('hashchange', () => {{ clearTimeout(t); r(true); }}, "
                               "{once: true}); })", deadline)
        else:
            done = True
        if not done:
            raise RuntimeError(f"goto {url}: not loaded in {NAV_TIMEOUT} s (the navigation never committed, or its load / "
                               f"hashchange never came)")
        if ready:
            try:
                self.js(READY, time.monotonic() + READY_TIMEOUT)
            except RuntimeError as e:
                raise RuntimeError(f"goto {url}: loaded, but not drawn (READY) in {READY_TIMEOUT} s: {e}") from None

    def _until(self, cond, deadline: float) -> bool:
        """Evaluate cond(ms) (an expression that waits in the page ms at most) until it's true; False once `deadline`
        (time.monotonic()) has passed. An evaluation that lands on a page going away fails at once and is tried again;
        one that doesn't answer by the deadline fails too."""
        while (left := deadline - time.monotonic()) > 0:
            try:
                if self.js(cond(max(1, int(left * 1000))), deadline):
                    return True
            except RuntimeError:
                time.sleep(0.002)   # between two documents (no page to evaluate on yet), or out of time
        return False

    def until(self, expr: str, timeout: float = 8.0):
        """expr's value once it's truthy, polled in the page every 50 ms for up to `timeout` s, else its last value (so
        an assert on it shows what was there). For reads after a click or a transition: a fixed wait is a guess that a
        slow machine loses (the slow-mode sweep, t_d2b88168), a wait for the state the test expects is not."""
        return self.js(f"""new Promise((r) => {{ const end = performance.now() + {timeout * 1000:.0f};
          const f = () => {{ let v = null; try {{ v = ({expr}); }} catch (e) {{ v = null; }}
            if (v || performance.now() >= end) r(v); else setTimeout(f, 50); }};
          f(); }})""")

    def still(self, sel: str = "", timeout: float = 8.0):
        """Wait until no finite animation or transition is running in `sel`'s subtree (the whole page if empty), so a
        measurement after a pop-in reads the end state, not a frame of it. Endless ones (spinners) don't count. True
        once still, else the running animations' names (for the assert message)."""
        root = f"document.querySelector({json.dumps(sel)})" if sel else "document"
        return self.until(f"""(() => {{ const r = {root}; if (!r) return true;
          const run = r.getAnimations({{subtree: true}}).filter((a) => a.playState === 'running'
            && (a.effect?.getTiming?.().iterations ?? 1) !== Infinity);
          return run.length === 0 || null; }})()""", timeout) or "still running"

    def settle(self) -> None:
        self.js(SETTLE)

    def settled(self, cap_ms: int = 10000, force: bool = False) -> str:
        """TEST-SPEED: wait until the page is settled (PEND: nothing in flight, no timer of 1 s or less, no animation, and
        a frame without a DOM change; `force`: that frame even when nothing happened since the last one, e.g. after a
        resize). Returns '' then, or what was still going on after `cap_ms`."""
        return self.js(f"(window.__settled ? window.__settled({int(cap_ms)}, {'true' if force else 'false'}) : {SETTLED})")

    def screenshot(self, path: str | Path, full: bool = False, cap: int = 6000) -> None:
        if full:
            h = min(int(self.js("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)")), cap)
            self.call("Emulation.setDeviceMetricsOverride", width=self.width, height=max(h, self.height),
                      deviceScaleFactor=2 if self.mobile else 1, mobile=self.mobile)
            self.settled(force=True)   # (TEST-SPEED: was a 0.5 s pause) the page laid out again at the full height
        data = self.call("Page.captureScreenshot", format="png")["data"]
        Path(path).write_bytes(base64.b64decode(data))
        if full:
            self.size(self.width, self.height, self.mobile)
            self.settled(force=True)   # (was 0.2 s)

    def problems(self) -> list[str]:
        out = [f"js: {e}" for e in (self.js("window.__errs.splice(0)") or [])]
        out += [f"csp: {e}" for e in (self.js("window.__csp.splice(0)") or [])]
        for m in self.events:
            if m["method"] == "Runtime.exceptionThrown":
                d = m["params"]["exceptionDetails"]
                out.append(f"exception: {d.get('text', '')} {str(d.get('exception', {}).get('description', ''))[:200]}")
            elif m["method"] == "Runtime.consoleAPICalled" and m["params"]["type"] in ("error", "assert"):
                out.append("console.error: " + " ".join(str(a.get("value", a.get("description", "")))
                                                        for a in m["params"]["args"])[:200])
            elif m["method"] == "Log.entryAdded" and m["params"]["entry"]["level"] == "error":
                e = m["params"]["entry"]
                if "ERR_BLOCKED_BY_CLIENT" not in e.get("text", "") and "plaid.com" not in (e.get("url") or ""):
                    out.append(f"log: {e.get('text', '')[:160]} {e.get('url', '')}")
        self.events.clear()
        return out

    def requests(self) -> list[tuple[str, str]]:
        """(method, url) of the requests the page sent since the last problems()/requests() call, oldest first."""
        out = [(m["params"]["request"]["method"], m["params"]["request"]["url"]) for m in self.events
               if m["method"] == "Network.requestWillBeSent"]
        self.events = [m for m in self.events if m["method"] != "Network.requestWillBeSent"]
        return out


class Server:
    """`finance serve`'s App on a free loopback port, in a thread (for browser checks)."""

    def __init__(self, db_path, today=None):
        from hermes_finance.web import App, make_server

        def no_plaid():
            raise AssertionError("the browser check must never reach Plaid")

        self.app = App(db_path, no_plaid, "127.0.0.1", 0)
        if today:
            self.app.today = lambda: today
        self.srv = make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
