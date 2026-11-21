"""t_73b74e6f: the evidence for `goto ...: not loaded in NAV_TIMEOUT s`, the Nightly signature of run 43721612422 (four
Chromes in one process, the last one's first goto failing 15.0 s after launch on an idle page; the hypothesis is that
the document loaded but PEND was never injected into it).

What a failed goto() now reports, and the tests that pin each field:
1. a page with no injected script reads pend=undefined while the document itself is complete and loaded, and the
   evaluation counters show the wait really happened (a falsy answer);
2. a navigation that never commits a new document reports loaders=nav=(none) with the old document's frame loaderId
   next to it, and href still the old document (Page.navigate answered without a loader: Chrome answers a server that
   never responds by not answering the command, so the response is the one stand-in here and the rest is real);
3. _until() counts falsy answers, swallowed RuntimeErrors and the last one's text, with no change to its wait;
4. every Chrome this process started is listed, in start order, with its launch -> DevTools seconds (and a _recycled
   Chrome keeps the old entry);
5. a goto() still waiting at 2/3 of NAV_TIMEOUT starts its `sample`s then (before the deadline), and a wedge keeps
   them; one that loads after the mark kills them and returns without waiting, those of a presample still starting
   when the next goto() arms included;
6. a goto() that finishes first starts nothing, writes nothing, and a clean stop deletes its log and samples.

The browser cases use a tiny loopback server (plain files, and a route that accepts a connection and never answers).
HF_CHROMES keeps the run to the machine's budget; the file starts a real Chrome, so scripts/check.py files it with
the browser tests (BROWSER_RE: the literal below).
"""
from __future__ import annotations

import contextlib
import functools
import http.server
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

import cdp
from cdp import Chrome

pytestmark = pytest.mark.skipif(cdp.find_chrome() is None, reason="Chrome is not installed")

OK = '<html><head><meta name="viewport" content="width=device-width"></head><body>ok</body></html>'
# the renderer this page leaves busy: its load listener starts a loop that never yields
STUCK = ('<html><body>stuck<script>addEventListener("load", () => setTimeout(() => { for (;;) {} }, 0));'
         '</script></body></html>')
SLOW_LOAD = 2.4   # s: /slow.html answers this late, after the presample mark (2/3 of a 3 s NAV_TIMEOUT in its test)


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.partition("?")[0] == "/slow.html":
            time.sleep(SLOW_LOAD)   # (the commit comes after the presample mark)
        super().do_GET()


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    d = tmp_path_factory.mktemp("evidence")
    (d / "ok.html").write_text(OK)
    (d / "slow.html").write_text(OK)
    (d / "stuck.html").write_text(STUCK)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=str(d)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def without_loader(b: Chrome) -> None:
    """Page.navigate answering without a loaderId: a navigation that commits no new document, which is the case the
    loaders field is for. (Chrome 157 answers a server that never responds by not answering Page.navigate at all, so a
    real route cannot produce this raise: its Page.navigate deadline fires instead.) Every other call -- getFrameTree,
    Runtime.evaluate -- still goes to the real browser, on the real old document."""
    real = b.call

    def call(method: str, _deadline: float | None = None, **params):
        if method == "Page.navigate":
            return {"frameId": "stand-in"}
        return real(method, _deadline, **params)

    b.call = call


def evidence_fields(message: str) -> dict[str, str]:
    """The fields of a "not loaded" raise's `; evidence: ...` line, in order (values may contain spaces and commas)."""
    line = message.split("; evidence: ", 1)[1]
    keys = ("pend", "readyState", "href", "loadEventEnd", "evals", "loaders", "devtools", "chromes")
    fields, rest = {}, line
    for i, key in enumerate(keys):
        assert rest.startswith(key + "="), (key, rest)
        rest = rest[len(key) + 1:]
        if i + 1 == len(keys):
            fields[key] = rest
            break
        nxt = ", " + keys[i + 1] + "="
        cut = rest.find(nxt)
        assert cut > 0, (key, rest)
        fields[key], rest = rest[:cut], rest[cut + len(", "):]
    return fields


def test_a_page_without_the_injected_script_reports_the_document_but_no_tracker(site, monkeypatch):
    """The hypothesis's discriminator: the document is there (complete, loaded) and `window.__pend` is not. A page with
    no PEND must read `pend=undefined`; the wait's own counters say whether it polled a falsy answer or was swallowed."""
    monkeypatch.setattr(cdp, "PEND", "")   # before the Chrome is made: no script is injected into any document
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 1.5)
    with Chrome() as b:
        # the wait's own page-side timer, 40 ms instead of the deadline: every poll answers (false counts), and the last
        # one is still unanswered when the deadline passes, so no answer is half-read when the evidence is asked for
        b.call("Page.addScriptToEvaluateOnNewDocument",
               source="(() => { const real = window.setTimeout; window.setTimeout = (fn, ms, ...a) => real(fn, 40, ...a); })()")
        with pytest.raises(RuntimeError) as err:
            b.goto(f"{site}/ok.html", ready=False)
    message = str(err.value)
    assert ("not loaded in 1.5 s (the navigation never committed, or its load / hashchange never came); evidence: "
            in message), message
    fields = evidence_fields(message)
    assert fields["pend"] == "undefined", fields
    assert fields["readyState"] == "complete", fields
    assert fields["href"] == f"{site}/ok.html", fields
    load = re.fullmatch(r"([\d.]+) now=([\d.]+)", fields["loadEventEnd"])
    assert load and float(load.group(1)) > 0, fields
    evals = re.fullmatch(r"false (\d+), raised (\d+), last=(\".*\"|\(none\))", fields["evals"])
    assert evals and int(evals.group(1)) >= 1, fields
    assert re.fullmatch(r"nav \S+ frame \S+", fields["loaders"]), fields
    assert re.fullmatch(r"[\d.]+s", fields["devtools"]), fields
    assert re.fullmatch(r"\d+ \[[^\]]*\]s", fields["chromes"]), fields


def test_a_navigation_that_never_commits_reports_the_old_document(site, monkeypatch):
    """Nothing commits. Page.navigate hands back no loaderId, so the evidence says nav=(none) -- and Page.getFrameTree
    still names the frame's own loaderId: the old document's, whose href stays too. (It is answered by the renderer,
    so `frame ?` means its main thread didn't answer -- and the page's own fields are then "?" too.)"""
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 1.5)
    with Chrome() as b:
        b.goto(f"{site}/ok.html", ready=False)
        assert b.js("location.href", time.monotonic() + 5) == f"{site}/ok.html"
        # (the wait's page-side timer 100 ms late: its answer is still unanswered when the deadline passes, not half-read,
        # and the evidence's next call skips the late one)
        b.js("(() => { const real = window.setTimeout;"
             " window.setTimeout = (fn, ms, ...a) => real(fn, ms + 100, ...a); })()")
        without_loader(b)
        with pytest.raises(RuntimeError) as err:
            b.goto(f"{site}/ok.html#/never", ready=False)   # a #hash the page will never hear about
    message = str(err.value)
    assert "not loaded in 1.5 s" in message
    fields = evidence_fields(message)
    assert re.fullmatch(r"nav=\(none\) frame \S+", fields["loaders"]), fields
    assert fields["href"] == f"{site}/ok.html", fields
    assert fields["pend"].startswith("object doc="), fields   # (the old document still has its tracker)


class _Clock:
    """cdp's time, for _until: every call to js() or sleep() moves it on, so a deadline is reached in a set number of
    polls however fast the machine is."""

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Js:
    """A stand-in page: js() raises for its first `raises` calls (with `text`, or the texts in it in turn), then answers
    False (both swallowing cases _until has)."""

    def __init__(self, clock: _Clock, raises: int, text: str | tuple[str, ...]) -> None:
        self.clock, self.left, self.text, self.calls = clock, raises, text, 0

    def js(self, expr: str, deadline: float | None = None) -> bool:
        self.calls += 1
        self.clock.now += 0.001
        if self.calls <= self.left:
            if isinstance(self.text, tuple):
                raise RuntimeError(self.text[min(self.calls, len(self.text)) - 1])
            raise RuntimeError(self.text)
        return False


def test_until_counts_falsy_answers_and_the_exception_it_swallowed(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(cdp, "time", clock)
    stats: dict = {}
    page = _Js(clock, raises=1, text="js failed: between two documents")
    assert cdp.Chrome._until(page, lambda ms: "x", clock.now + 0.006, stats) is False
    assert stats == {"false": 3, "raised": 1, "last": "js failed: between two documents",
                     "first": "js failed: between two documents"}, stats


def test_until_counts_an_evaluation_that_raises_the_whole_time(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(cdp, "time", clock)
    stats: dict = {}
    page = _Js(clock, raises=10 ** 9, text="js failed: no page to evaluate on")
    assert cdp.Chrome._until(page, lambda ms: "x", clock.now + 0.005, stats) is False
    assert stats == {"false": 0, "raised": 2, "last": "js failed: no page to evaluate on",
                     "first": "js failed: no page to evaluate on"}, stats


def test_until_keeps_the_first_and_last_swallowed_exceptions_and_evidence_shows_both(monkeypatch):
    """Two distinct exceptions, then false answers: `last` is the final poll's own text, while `first` keeps the
    informative earlier one, and the evidence's evals field carries both when they differ."""
    clock = _Clock()
    monkeypatch.setattr(cdp, "time", clock)
    stats: dict = {}
    page = _Js(clock, raises=2, text=("js failed: between two documents",
                                      "Runtime.evaluate: no answer by its deadline"))
    assert cdp.Chrome._until(page, lambda ms: "x", clock.now + 0.008, stats) is False
    assert stats["first"] == "js failed: between two documents", stats
    assert stats["last"] == "Runtime.evaluate: no answer by its deadline", stats
    c = Chrome.__new__(Chrome)
    c._navstats = stats

    def unavailable(*a, **k):
        raise RuntimeError("no page")
    c.call = c.js = unavailable
    assert c._evidence(0.5)["evals"] == ('false 2, raised 2, last="Runtime.evaluate: no answer by its deadline" '
                                         'first="js failed: between two documents"')


def test_every_chrome_this_process_started_is_listed_with_its_devtools_seconds(site, tmp_path, monkeypatch):
    """Two Chromes in one process: chromes=2 with both launch -> DevTools seconds (0 < s < 30), and the wedge of the
    second one's failure names both profiles. The first is stopped before the second fails: a clean stop loses no
    entry (and a _recycle's old Chrome keeps its own)."""
    logs = tmp_path / "logs"
    monkeypatch.setenv(cdp.LOG_ENV, str(logs))
    monkeypatch.setattr(cdp, "_STARTED", [])
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 1.5)
    with Chrome() as first:
        first.goto(f"{site}/ok.html", ready=False)
        first_name = Path(first.profile).name
    with Chrome() as second:
        second_name, old_log = Path(second.profile).name, second.log
        without_loader(second)
        with pytest.raises(RuntimeError) as err:
            second.goto(f"{site}/ok.html#/never", ready=False)
    fields = evidence_fields(str(err.value))
    count = re.fullmatch(r"(\d+) \[([^\]]*)\]s", fields["chromes"])
    assert count and int(count.group(1)) == 2, fields
    seconds = [float(x) for x in count.group(2).split()]
    assert len(seconds) == 2 and all(0 < s < 30 for s in seconds), seconds
    wedge = Path(old_log[:-len(".log")] + ".wedge.txt").read_text()
    assert "chromes started in this process (launch -> DevTools ready, start order):" in wedge, wedge
    assert re.search(rf"^\s+{re.escape(first_name)} [\d.]+ s$", wedge, re.M), wedge
    assert re.search(rf"^\s+{re.escape(second_name)} [\d.]+ s$", wedge, re.M), wedge


@pytest.mark.skipif(shutil.which("sample") is None, reason="macOS `sample` is needed for the presamples")
def test_a_goto_still_waiting_starts_its_presamples_before_the_deadline(site, tmp_path, monkeypatch):
    """The busy-loop page: at 2/3 of NAV_TIMEOUT the goto is still waiting, so `sample` starts then (not after the
    failure, when the process is idle again). The hook (Chrome.presampled_at) and the files are both asserted, and the
    wedge names them."""
    logs = tmp_path / "logs"
    monkeypatch.setenv(cdp.LOG_ENV, str(logs))
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 3.0)
    monkeypatch.setattr(cdp, "READY_TIMEOUT", 4.0)
    with Chrome() as b:
        old_log, old_pid = b.log, b.proc.pid
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="stuck.html: not (loaded|drawn)"):
            b.goto(f"{site}/stuck.html")
        failure = time.monotonic() - started
        # (the failed Chrome's own flags go with it: goto() recycles it. _capture records what it saw here.)
        presampled = cdp.LAST_WEDGE.get("presampled_at")
        assert cdp.LAST_WEDGE.get("pid") == old_pid, cdp.LAST_WEDGE
        assert presampled is not None, "the presample hook never fired"
        assert cdp.PRESAMPLE_AT * 3.0 - 0.05 <= presampled < failure, (presampled, failure)
    base = Path(old_log[:-len(".log")])
    samples = sorted(p.name for p in logs.glob(base.name + ".presample-*.txt"))
    assert f"{base.name}.presample-{old_pid}.txt" in samples, samples
    wedge = Path(f"{base}.wedge.txt").read_text()
    assert f"presamples (started {presampled:.1f} s into the goto, 2/3 of NAV_TIMEOUT):" in wedge, wedge
    assert f"{base.name}.presample-{old_pid}.txt (exit" in wedge, wedge


@pytest.mark.skipif(shutil.which("sample") is None, reason="macOS `sample` is needed for the presamples")
def test_a_goto_that_loads_after_the_presample_mark_returns_without_waiting_for_it(site, tmp_path, monkeypatch):
    """The page commits at SLOW_LOAD (2.4 s), after the presample mark (2/3 of a 3 s NAV_TIMEOUT: 2.0 s). A recovered
    goto() must return as soon as its page has loaded: it kills the samplers (6 s each here) and deletes their files,
    instead of waiting for them (which would return at ~8 s, five seconds after the load)."""
    logs = tmp_path / "logs"
    monkeypatch.setenv(cdp.LOG_ENV, str(logs))
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 3.0)
    monkeypatch.setattr(cdp, "READY_TIMEOUT", 4.0)
    monkeypatch.setattr(cdp, "SAMPLE_SECONDS", 6)
    with Chrome() as b:
        started = time.monotonic()
        b.goto(f"{site}/slow.html", ready=False)
        elapsed = time.monotonic() - started
        assert b.presampled_at is not None, "the presample hook never fired"
        assert elapsed - SLOW_LOAD < 2.0, (elapsed, b.presampled_at)
        end = time.monotonic() + 3.0

        def sampling() -> bool:
            out = subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True).stdout
            return any(Path(b.profile).name in ln and ln.split(None, 1)[0].endswith("sample")
                       for ln in out.splitlines() if ln.strip())

        while time.monotonic() < end:   # (a just-SIGKILLed sampler may be a zombie until its thread reaps it)
            files = list(logs.glob("*.presample-*"))
            if not files and not sampling():
                break
            time.sleep(0.05)
        assert not files, files
        assert not sampling(), "a `sample` for this Chrome's profile was still alive 3 s after goto() returned"


@pytest.mark.skipif(shutil.which("sample") is None, reason="macOS `sample` is needed for the presamples")
def test_a_presample_still_starting_when_the_next_goto_arms_is_still_killed(site, tmp_path, monkeypatch):
    """t_a4781960: the first goto's presample is still in its `ps` (slowed by 1 s, its first call only) when that goto
    has loaded and the second one has armed. Its samplers belong to the first goto, which recovered: they are killed.
    The state was the Chrome's, reset at every goto: the late timer read the second goto's fresh flags, stored its
    samplers as the second goto's, whose own presample overwrote them, and nothing killed them (6 s each here)."""
    logs = tmp_path / "logs"
    monkeypatch.setenv(cdp.LOG_ENV, str(logs))
    monkeypatch.setattr(cdp, "NAV_TIMEOUT", 3.0)
    monkeypatch.setattr(cdp, "READY_TIMEOUT", 4.0)
    monkeypatch.setattr(cdp, "SAMPLE_SECONDS", 6)
    real, slowed = Chrome._sample_pids, []

    def sample_pids(self):
        if not slowed:   # (the first goto's: past its page's load at SLOW_LOAD, into the second goto)
            slowed.append(True)
            time.sleep(1.0)
        return real(self)
    monkeypatch.setattr(Chrome, "_sample_pids", sample_pids)

    def samplers(name: str) -> list[int]:
        out = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True).stdout
        return [int(f[0]) for f in (ln.split(None, 1) for ln in out.splitlines())
                if len(f) == 2 and name in f[1] and f[1].split(None, 1)[0].endswith("sample")]

    with Chrome() as b:
        name = Path(b.profile).name
        try:
            b.goto(f"{site}/slow.html", ready=False)
            b.goto(f"{site}/slow.html?again", ready=False)
            assert slowed, "the first goto's presample never ran (the slowed `ps` never fired)"
            end = time.monotonic() + 3.0
            while time.monotonic() < end:   # (a just-SIGKILLed sampler may be a zombie until its thread reaps it)
                files = list(logs.glob("*.presample-*"))
                if not files and not samplers(name):
                    break
                time.sleep(0.05)
            assert not files, files
            assert not samplers(name), "a `sample` of the first goto was still alive 3 s after the second goto returned"
        finally:
            for pid in samplers(name):   # (this test's leak, if any: by pid)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)


def test_a_healthy_goto_starts_nothing_and_leaves_nothing(site, tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setenv(cdp.LOG_ENV, str(logs))
    with Chrome() as b:
        b.goto(f"{site}/ok.html", ready=False)
        assert b.presampled_at is None
        assert not list(logs.glob("*.presample-*")), list(logs.iterdir())
        assert not list(logs.glob("*.wedge.txt")), list(logs.iterdir())
    assert list(logs.iterdir()) == [], list(logs.iterdir())   # (the clean Chrome's log went with it)
