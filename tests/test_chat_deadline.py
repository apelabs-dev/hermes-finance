"""The Ask turn's one real deadline (chat-deadline correction, owner reproduction chat-post-deadline-b337416.log).

chat._post gave `timeout` to http.client as the socket's timeout, which bounds each read, not the request: a reply
trickling in a byte at a time never timed out, and the turn then published its answer (and cards) however late. Now a
turn has one monotonic deadline: every read and write of a request waits only what's left of it, a name is looked up
in a child process killed at it, an answer is at most MAX_RESPONSE bytes, the cards are released under the write lock
only before it, and the turn's database waits end by it (its bookkeeping is tried until SETTLE after it).

The provider is a synthetic HTTP server in its own process (SERVER below, on 127.0.0.1 only: no real provider, no
network), answering per path: at once, never, headers or body one byte every DRIP seconds, 4 MiB, cut off, garbled,
never reading the request, a tool call then a trickle, chunked (at once, trickled, cut off, 4 MiB); the same server with
TLS on a throwaway certificate made here. The turn tests that need a held answer use test_chat_corrections.GatedLLM.
Deadlines are scaled down (chat.TIMEOUT / SETTLE monkeypatched, or _post's own `timeout`); the algorithm is
production's. Synthetic DBs only.

The follow-up (chat-deadline-review-f413562.md R1-R3):
R1  a tool step that only reads (a read, a write's plan, finance_sync's approval check) runs in a child process killed
    at the deadline: a model's rule regex that backtracks for hours over a synthetic merchant ends the turn at its
    deadline. That case runs in a child process of its own with a timebox, so code without the worker can't hang pytest.
R2  no step starts after the deadline: each database try (the first included, and one after an oversleep), each
    reservation try, each Hermes read, the card's record, finance_sync after its approval check. These tests count the
    step's calls (never invoked), not only the final 504.
R3  the turn's connections wait for no lock during their setup either (the schema check, db.connect's pragmas): with
    another process holding the file so that even opening it waits (states the app never makes itself: a rollback
    journal, or locking_mode=EXCLUSIVE), the card's record and the withdrawal end by their budget."""

from __future__ import annotations

import contextlib
import datetime
import ipaddress
import json
import os
import select
import ssl
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import bridge_stub
from agent_tools import TODAY, build, rows
from hermes_finance import chat, config, db, mcp_state, prefs, secretstore, tooldefs, usage
from hermes_finance.web import App
from test_chat_corrections import GatedLLM, result_of, sql, utc_ago
from test_chat_task23 import KEY, MODEL, answer, ask, call_msg, cap_of, http, pending_rows

DRIP = 0.04        # seconds between two bytes of a trickled answer (the owner's probe's)
DEADLINE = 1.0     # _post's timeout here: production's old 1 s floor, so the old code got exactly this per read
SLACK = 1.0        # what a loaded machine may add to a deadline before the test calls it outlasted
UNREADABLE = "The AI provider sent an answer the app couldn't read."

SERVER = r'''
import json, os, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _end_with_parent():
    parent, born = os.getppid(), time.monotonic()
    while True:
        time.sleep(1.0)
        if os.getppid() != parent or time.monotonic() - born > 900:
            os._exit(0)                                    # (the pytest that started us is gone, or 15 min: never stray)

threading.Thread(target=_end_with_parent, daemon=True).start()

DRIP, HOLD = float(sys.argv[1]), float(sys.argv[2])
seen, lock = {}, threading.Lock()

def completion(message):
    return json.dumps({"id": "x", "object": "chat.completion",
                       "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}).encode()

ANSWER = completion({"role": "assistant", "content": "synthetic answer"})
TOOL = completion({"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
    "name": "finance_set_cap", "arguments": json.dumps({"category": "Other Shopping", "monthly_cap": 900})}}]})

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        tag = self.path.split("/")[1]                      # /<mode>-<tag>/chat/completions
        with lock:
            n = seen[tag] = seen.get(tag, 0) + 1
        mode = tag.split("-")[0]
        if mode != "noread":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.close_connection = True
        try:
            getattr(self, mode)(n)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def head(self, body, length=True, extra=b""):
        return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + extra
                + (b"Content-Length: %d\r\n" % len(body) if length else b"") + b"Connection: close\r\n\r\n")

    def out(self, data):
        self.wfile.write(data)
        self.wfile.flush()

    def trickle(self, data):
        end = time.monotonic() + HOLD
        for i in range(len(data)):
            if time.monotonic() > end:
                return
            self.out(data[i:i + 1])
            time.sleep(DRIP)

    def ok(self, n):
        self.out(self.head(ANSWER) + ANSWER)

    def idle(self, n):                                     # nothing at all, until the client hangs up
        end = time.monotonic() + HOLD
        self.connection.settimeout(0.1)
        while time.monotonic() < end:
            try:
                if not self.connection.recv(65536):
                    return
            except socket.timeout:
                pass

    def noread(self, n):                                   # never reads the request (the client's send blocks)
        time.sleep(HOLD)

    def slowhead(self, n):                                 # the status line and headers, a byte at a time
        self.trickle(self.head(ANSWER, extra=b"X-Pad: " + b"p" * 16 + b"\r\n"))
        self.out(ANSWER)

    def drip(self, n):                                     # the headers at once, the body a byte at a time
        self.out(self.head(ANSWER))
        self.trickle(ANSWER)

    def huge(self, n):                                     # a valid answer of 4 MiB, at once
        body = completion({"role": "assistant", "content": "x" * (4 << 20)})
        self.out(self.head(body) + body)

    def hugeclose(self, n):                                # the same with no length: read until the close
        body = completion({"role": "assistant", "content": "x" * (4 << 20)})
        self.out(self.head(body, length=False) + body)

    def short(self, n):                                    # less than its Content-Length, then the close
        self.out(self.head(ANSWER) + ANSWER[:len(ANSWER) // 2])

    def garbage(self, n):
        self.out(b"SPAM SPAM SPAM\r\n\r\n")

    def toolok(self, n):
        body = TOOL if n == 1 else ANSWER
        self.out(self.head(body) + body)

    def tooldrip(self, n):                                 # a tool call at once, then the answer trickling in
        if n == 1:
            self.out(self.head(TOOL) + TOOL)
        else:
            self.drip(n)

    def chunks(self, body, size, drip=False, cut=False):   # Transfer-Encoding: chunked, `size` bytes a chunk
        self.out(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
                 b"Connection: close\r\n\r\n")
        pieces = [body[i:i + size] for i in range(0, len(body), size)]
        for piece in pieces[:len(pieces) // 2] if cut else pieces:
            chunk = b"%x\r\n" % len(piece) + piece + b"\r\n"
            self.trickle(chunk) if drip else self.out(chunk)
        if not cut:
            self.out(b"0\r\n\r\n")

    def chunked(self, n):                                  # a valid chunked answer, at once
        self.chunks(ANSWER, 16)

    def chunkdrip(self, n):                                # chunked, each chunk a byte at a time
        self.chunks(ANSWER, 8, drip=True)

    def chunkcut(self, n):                                 # half the chunks, then the close (no last chunk)
        self.chunks(ANSWER, 8, cut=True)

    def chunkhuge(self, n):                                # a valid chunked answer of 4 MiB, at once
        self.chunks(completion({"role": "assistant", "content": "x" * (4 << 20)}), 1 << 16)

class Srv(ThreadingHTTPServer):
    def server_bind(self):   # no getfqdn(): its reverse lookup of 127.0.0.1 can stall past first_line()'s 20 s on CI
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

srv = Srv(("127.0.0.1", 0), H)
srv.daemon_threads = True
if len(sys.argv) > 3:                                      # TLS, with the test's throwaway certificate and key
    import ssl
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(sys.argv[3], sys.argv[4])
    srv.socket = tls.wrap_socket(srv.socket, server_side=True)
print("PORT", srv.server_address[1], flush=True)
srv.serve_forever()
'''


def first_line(proc: subprocess.Popen, seconds: float) -> str:
    """The child's first stdout line, waiting `seconds` at most (never a readline that could block)."""
    fd, buf, end = proc.stdout.fileno(), b"", time.monotonic() + seconds
    while b"\n" not in buf:
        left = end - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            break
        chunk = os.read(fd, 256)
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace").split("\n")[0]


def serve(scheme: str, *tls: str):
    """The synthetic provider (its own process group, killed and waited for at the end): its base address."""
    proc = subprocess.Popen([sys.executable, "-I", "-c", SERVER, str(DRIP), "30", *tls], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        line = first_line(proc, 20)
        assert line.startswith("PORT "), (line, proc.poll())
        yield f"{scheme}://127.0.0.1:{int(line.split()[1])}"
    finally:
        if proc.poll() is None:   # (never reaped: its group is still its own)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(10)
        proc.stdout.close()


@pytest.fixture(scope="module")
def provider():
    yield from serve("http")


SPAWN_PROVIDER = r'''
import subprocess, sys, time
server, drip = sys.argv[1], sys.argv[2]
p = subprocess.Popen([sys.executable, "-I", "-c", server, drip, "30"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
print("SERVER", p.stdout.readline().decode().split()[1], p.pid, flush=True)
while True:
    time.sleep(60)
'''


def test_the_provider_ends_when_its_parent_dies():
    """The provider is not left serving when the process that started it is killed (as a SIGTERM'd pytest is): it
    watches its parent and ends itself within a couple of seconds instead of living on, reparented to PID 1."""
    parent = subprocess.Popen([sys.executable, "-I", "-c", SPAWN_PROVIDER, SERVER, str(DRIP)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    server, gone = None, False
    try:
        line = first_line(parent, 20)
        assert line.startswith("SERVER "), (line, parent.poll())
        _, port, pid = line.split()
        server = int(pid)
        assert port.isdigit() and os.kill(server, 0) is None
        os.killpg(parent.pid, signal.SIGKILL)              # the parent gone: the provider is reparented to PID 1
        parent.wait(10)
        end = time.monotonic() + 8
        while time.monotonic() < end:
            try:
                os.kill(server, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.1)
        assert gone, f"the provider (pid {server}) outlived its parent"
    finally:
        if parent.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(parent.pid, signal.SIGKILL)
            parent.wait(10)
        if server is not None and not gone:
            with contextlib.suppress(ProcessLookupError):
                os.kill(server, signal.SIGKILL)
        parent.stdout.close()


def outcome(url: str, timeout: float, body: dict | None = None) -> tuple[tuple, float]:
    """(what chat._post did: ("answer", its first 40 characters, its length) or ("error", status, line); seconds)."""
    start = time.monotonic()
    try:
        content = chat._post(url, None, body or {"messages": []}, timeout)["choices"][0]["message"].get("content") or ""
        got = ("answer", content[:40], len(content))
    except chat.ChatError as e:
        got = ("error", e.status, str(e))
    return got, round(time.monotonic() - start, 3)


def processes() -> str:
    return subprocess.run(["/bin/ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=10).stdout


# --- one request: all of it by its deadline ----------------------------------------------------------------------------

def test_an_answer_that_comes_at_once_is_read(provider):
    """Positive control: an ordinary answer is read, well inside the deadline."""
    got, took = outcome(f"{provider}/ok-a/chat/completions", DEADLINE)
    assert got == ("answer", "synthetic answer", 16) and took < DEADLINE, (got, took)


def test_an_endpoint_that_never_answers_times_out_at_the_deadline(provider):
    """Control (a single socket read never outlasted its timeout before either)."""
    got, took = outcome(f"{provider}/idle-a/chat/completions", DEADLINE)
    assert got[:2] == ("error", 504) and DEADLINE - 0.05 <= took < DEADLINE + SLACK, (got, took)


@pytest.mark.parametrize("mode", ["slowhead", "drip"])
def test_an_answer_trickling_in_never_outlasts_the_deadline(provider, mode):
    """The owner's reproduction: each byte comes well inside a socket timeout (DRIP), the whole answer takes over 4 s
    (slowhead: its headers; drip: its body). The request ends at the deadline, 504; the late answer is never used."""
    got, took = outcome(f"{provider}/{mode}-a/chat/completions", DEADLINE)
    assert (got[:2], took < DEADLINE + SLACK) == (("error", 504), True), (got, took)


def test_sending_to_an_endpoint_that_never_reads_ends_by_the_deadline(provider):
    """Control: a request body (8 MB) the endpoint never reads stops at the deadline (the old sendall had a total
    timeout on a plain socket too; TLS's didn't, _Wire's send is per write)."""
    got, took = outcome(f"{provider}/noread-a/chat/completions", DEADLINE,
                        {"messages": [{"role": "user", "content": "x" * (8 << 20)}]})
    assert got[:2] == ("error", 504) and took < DEADLINE + SLACK, (got, took)


@pytest.mark.parametrize("mode", ["huge", "hugeclose"])
def test_an_answer_over_max_response_is_not_read(provider, mode):
    """A valid 4 MiB answer (with its Content-Length, and without one: read until the close) is refused once
    MAX_RESPONSE bytes have come, not read whole into memory and used."""
    got, took = outcome(f"{provider}/{mode}-a/chat/completions", 10.0)
    assert got == ("error", 502, UNREADABLE), (got, took)
    assert chat.MAX_RESPONSE < 4 << 20


@pytest.mark.parametrize("mode", ["short", "garbage"])
def test_a_cut_off_or_garbled_answer_fails_closed(provider, mode):
    """Control: less than its Content-Length, or no HTTP at all: an error, never an answer (the old code failed these
    closed too, worded as "Couldn't reach")."""
    got, took = outcome(f"{provider}/{mode}-a/chat/completions", 10.0)
    assert got[:2] == ("error", 502), (got, took)


def test_a_stalled_name_lookup_in_this_process_cannot_hold_the_request(provider, monkeypatch):
    """A name (localhost) is never looked up with a blocking getaddrinfo in this process: here any lookup that isn't a
    plain IP address stalls 3 s, and the request still answers inside its deadline (the child process looks it up)."""
    real = socket.getaddrinfo   # (conftest's loopback-only guard)

    def stalling(host, port, family=0, type=0, proto=0, flags=0):
        if not flags & socket.AI_NUMERICHOST:
            time.sleep(3)
        return real(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket, "getaddrinfo", stalling)
    port = provider.rsplit(":", 1)[1]
    got, took = outcome(f"http://localhost:{port}/ok-dns/chat/completions", DEADLINE)
    assert (got, took < DEADLINE) == (("answer", "synthetic answer", 16), True), (got, took)


def test_a_lookup_that_hangs_is_killed_at_the_deadline_and_leaves_nothing_running(provider, monkeypatch):
    """_lookup's child ignoring SIGTERM and never answering: the request ends at its deadline (504), and the child is
    gone (killed with its group and reaped), not left running."""
    marker = f"hf-chat-deadline-lookup-{os.getpid()}-{time.monotonic_ns()}"
    monkeypatch.setattr(chat, "LOOKUP", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                        f"time.sleep(60)  # {marker}")
    port = provider.rsplit(":", 1)[1]
    got, took = outcome(f"http://localhost:{port}/ok-hang/chat/completions", DEADLINE)
    assert got[:2] == ("error", 504) and took < DEADLINE + SLACK, (got, took)
    assert marker not in processes()


def test_a_lookup_group_macos_refuses_to_kill_still_ends_as_a_timeout(monkeypatch):
    """_lookup's cleanup, like _isolated's: a child still running at the deadline is killed with its group, and on macOS
    a group holding only an unreaped zombie refuses killpg with EPERM (errno 1), not ESRCH. That refusal must not
    escape the cleanup as a PermissionError (a 500): the lookup still ends as its TimeoutError, and the child is gone
    when the test ends, whether the cleanup succeeded or not."""
    marker = f"hf-chat-deadline-lookup-eperm-{os.getpid()}-{time.monotonic_ns()}"
    # (killpg is faked below, so the cleanup's wait() reaps the child when it ends by itself at 3 s)
    monkeypatch.setattr(chat, "LOOKUP", f"import time; time.sleep(3)  # {marker}")
    refused, procs = [], []
    real_killpg, real_popen = os.killpg, subprocess.Popen   # (so this test can still kill the child it started)

    def eperm(pgid, sig):
        refused.append((pgid, sig))      # what macOS does when the group's only member is a zombie
        raise PermissionError(1, "Operation not permitted")

    def popen(*a, **k):
        proc = real_popen(*a, **k)
        procs.append(proc)
        return proc

    monkeypatch.setattr(os, "killpg", eperm)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(chat.subprocess, "Popen", popen)
    try:
        with pytest.raises(TimeoutError):
            chat._lookup("localhost", 80, time.monotonic() + 0.5)
    finally:
        for proc in procs:               # only what this test started, killed and reaped however it ended
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    real_killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
    assert len(procs) == 1 and refused == [(procs[0].pid, signal.SIGKILL)], (procs, refused)
    assert marker not in processes()


def test_an_outside_name_is_refused_by_the_test_guard_before_any_lookup(monkeypatch):
    """No test reaches a real resolver through the lookup child: the in-process getaddrinfo (conftest's guard) sees the
    name first and refuses it, and no child is started."""
    def no_child(*a, **k):
        raise AssertionError("a lookup child was started")

    monkeypatch.setattr(subprocess, "Popen", no_child)
    with pytest.raises(AssertionError, match="real network lookup attempted"):
        chat._post("https://provider.example/v1/chat/completions", "k", {"messages": []}, DEADLINE)


# --- the turn: nothing published after its deadline --------------------------------------------------------------------

@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)


@pytest.fixture
def app(data_dir, provider):
    build("full", config.db_path())
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.mode": "api", "ai.preset": "custom", "ai.base_url": f"{provider}/ok-unset", "ai.model": MODEL,
                      "ai.monthly_cap": 5.0})
    conn.close()
    secretstore.write({secretstore.AI_KEY: KEY})
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


@pytest.fixture
def gated(app):
    f = GatedLLM()
    base_url(f.url)
    yield f
    f.close()


def base_url(url: str) -> None:
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.base_url": url})
    conn.close()


def ai_calls() -> list[tuple]:
    return rows(config.db_path(), "SELECT outcome, note FROM api_calls WHERE provider = 'ai' ORDER BY id")


class Asking(threading.Thread):
    """One in-process POST /api/chat in its own thread; `.result` is (status, answer)."""

    def __init__(self, app, text: str):
        super().__init__(daemon=True)
        self.app, self.text, self.result = app, text, None

    def run(self):
        self.result = ask(self.app, self.text)


def test_a_card_and_an_answer_in_time_are_published(app, provider, monkeypatch):
    """Positive control for the turn tests below: a tool call, then an answer at once, inside the deadline. (10 s, not 2:
    a slow CI runner took over 2 s for this one turn and got a 504; what's checked is that an answer in time is
    published, and the turn tests below keep their 2 s.)"""
    monkeypatch.setattr(chat, "TIMEOUT", 10.0)
    base_url(f"{provider}/toolok-a")
    start = time.monotonic()
    s, r = ask(app, "Raise Shopping?")
    assert s == 200 and [c["status"] for c in r["cards"]] == ["pending"] and time.monotonic() - start < 10.0, r
    assert [x[3] for x in pending_rows()] == ["pending"] and [o for o, _ in ai_calls()] == ["ok", "ok"]


def test_a_card_then_an_answer_trickling_in_ends_at_the_deadline_and_withdraws_the_card(app, provider, monkeypatch):
    """The turn form of the reproduction: the first answer records a card, the second trickles in past the deadline. The
    turn answers 504 at its deadline, the card is withdrawn (never listed, never decidable), and the request that timed
    out is charged its worst case. (5 s, not 2: a slow CI runner hadn't recorded the card by 2 s, so there was none to
    withdraw. The trickled answer alone takes 157 bytes x DRIP = 6.3 s at least, so the deadline still always hits.)"""
    monkeypatch.setattr(chat, "TIMEOUT", 5.0)
    base_url(f"{provider}/tooldrip-a")
    start = time.monotonic()
    s, r = ask(app, "Raise Shopping?")
    took = time.monotonic() - start
    assert (s, took < chat.TIMEOUT + SLACK) == (504, True), (s, r, round(took, 3), pending_rows())
    assert "took too long" in r["error"]
    [(aid, _, _, status)] = pending_rows()
    assert (status, result_of(aid)) == ("rejected", {"withdrawn": mcp_state.WITHDRAWN})
    s, d = http(app, "POST", "/api/mcp/pending/approve", {"id": aid})
    assert s == 409 and "already rejected" in d["error"] and cap_of("Other Shopping") == 850.0
    [(first, _), (second, note)] = ai_calls()
    assert (first, second) == ("ok", "timeout") and note.endswith(f"out={chat.MAX_OUTPUT_TOKENS} estimated")


def held_past_the_deadline(app, gated, script: list[dict], hold: threading.Event) -> Asking:
    """Run a turn whose last request is answered at once but whose next write waits on another writer until 0.5 s past
    the turn's deadline (chat.TIMEOUT from now); the finished Asking."""
    start = time.monotonic()
    gated.script = script
    t = Asking(app, "Shopping?")
    t.start()
    holder = sqlite3.connect(config.db_path(), timeout=5)
    try:
        assert gated.wait_requests(len(script), 20)
        holder.execute("BEGIN IMMEDIATE")
        hold.set()
        time.sleep(max(0.0, start + chat.TIMEOUT + 0.5 - time.monotonic()))
    finally:
        hold.set()
        holder.rollback()
        holder.close()
        t.join(40)
    return t


def test_an_answer_in_time_whose_publish_would_come_after_the_deadline_publishes_nothing(app, gated, monkeypatch):
    """Deadline exhausted before the publish: the answer comes at once, but another writer holds the database until
    past the deadline, so the turn only gets to its publish late. It publishes nothing: 504, the card withdrawn."""
    monkeypatch.setattr(chat, "TIMEOUT", 2.0)
    hold = threading.Event()
    t = held_past_the_deadline(app, gated, [
        {"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})},
        {"message": answer("Raising it to $900 would cover it. Approve the card if you want it."), "hold": hold}], hold)
    assert t.result[0] == 504 and t.result[1] == {"error": chat.LATE}, (t.result, pending_rows())
    [(aid, _, _, status)] = pending_rows()
    assert (status, result_of(aid)) == ("rejected", {"withdrawn": mcp_state.WITHDRAWN})
    s, d = http(app, "POST", "/api/mcp/pending/approve", {"id": aid})
    assert s == 409 and "already rejected" in d["error"] and cap_of("Other Shopping") == 850.0
    assert [o for o, _ in ai_calls()] == ["ok", "ok"]      # the answer's cost is still recorded (it came)


def test_an_answer_without_cards_that_would_come_after_the_deadline_is_not_given(app, gated, monkeypatch):
    """A card-less answer comes in time, but the turn gets past recording its cost only after the deadline: it's not
    given late (504), though its cost is recorded."""
    monkeypatch.setattr(chat, "TIMEOUT", 2.0)
    hold = threading.Event()
    t = held_past_the_deadline(app, gated, [{"message": answer("You spent $120 on Shopping."), "hold": hold}], hold)
    assert t.result[0] == 504 and t.result[1] == {"error": chat.LATE}, t.result
    assert [o for o, _ in ai_calls()] == ["ok"]


def test_no_tool_runs_after_the_deadline(app, gated, monkeypatch):
    """The answer asking for a write tool comes in time, but the turn only gets to its tools after the deadline: none
    runs (not even a card recorded and withdrawn), 504."""
    monkeypatch.setattr(chat, "TIMEOUT", 2.0)
    hold = threading.Event()
    t = held_past_the_deadline(app, gated, [{"message": call_msg("finance_set_cap", {"category": "Other Shopping",
                                                                                    "monthly_cap": 900}), "hold": hold}], hold)
    assert t.result[0] == 504 and "took too long" in t.result[1].get("error", ""), t.result
    assert pending_rows() == [], pending_rows()


def test_a_card_reconcile_withdrew_while_its_turn_ran_is_never_silently_missing(app, gated):
    """The review's card loss: while the turn's last request is out, its card's wall-clock age says abandoned (a clock
    step, or HELD_STALE mis-set), and a look at the cards reconciles it away. The answer then comes in time, but its
    card is gone: the turn fails (504) rather than answer "approve the card" with no card."""
    hold = threading.Event()
    gated.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})},
                    {"message": answer("Raising it to $900 would cover it. Approve the card if you want it."), "hold": hold}]
    t = Asking(app, "Raise Shopping?")
    t.start()
    try:
        assert gated.wait_requests(2, 20)
        [(aid, *_)] = pending_rows()
        sql("UPDATE pending_actions SET created_at = ? WHERE id = ?", (utc_ago(mcp_state.HELD_STALE + 60), aid))
        s, _ = http(app, "GET", f"/api/chat/cards?ids={aid}")                    # reconciles: withdrawn as abandoned
        assert s == 200 and pending_rows()[0][3] == "rejected"
    finally:
        hold.set()
        t.join(30)
    s, r = t.result
    assert s == 504 and r == {"error": chat.LATE}, (s, r.get("reply"), r.get("cards"), r.get("error"))
    assert result_of(aid) == {"withdrawn": mcp_state.ABANDONED}


def test_a_turn_stuck_behind_another_writer_gives_up_by_its_deadline_and_settle(app, gated, monkeypatch):
    """The turn's database waits keep its clock: the provider answers an error while another writer holds the
    database for good. Settling the request's cost and withdrawing the card wait until SETTLE after the deadline at
    most (not the database's 30 s each); the turn has ended while the writer still holds on, and its card stays held,
    undecidable, for reconcile()."""
    monkeypatch.setattr(chat, "TIMEOUT", 1.0)
    monkeypatch.setattr(chat, "SETTLE", 0.5, raising=False)   # (so this runs, and fails on its timing, on code without it)
    hold = threading.Event()
    gated.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})},
                    {"status": 500, "raw": b'{"error": {"message": "boom"}}', "hold": hold}]
    start = time.monotonic()
    t = Asking(app, "Raise Shopping?")
    t.start()
    holder = sqlite3.connect(config.db_path(), timeout=5)
    try:
        assert gated.wait_requests(2, 20)
        holder.execute("BEGIN IMMEDIATE")
        hold.set()
        t.join(chat.TIMEOUT + 0.5 + SLACK)
        took, running = round(time.monotonic() - start, 3), t.is_alive()
    finally:
        hold.set()
        holder.rollback()
        holder.close()
        t.join(40)
    assert (running, took < chat.TIMEOUT + 0.5 + SLACK) == (False, True), (took, t.result)
    assert t.result == (500, {"error": "internal error (OperationalError)"}), t.result
    [(aid, _, _, status)] = pending_rows()
    assert status == "pending" and settings_list(app) == []                  # held: not listed, not decidable
    s, d = http(app, "POST", "/api/mcp/pending/approve", {"id": aid})
    assert s == 409 and "still running" in d["error"] and cap_of("Other Shopping") == 850.0
    assert [o for o, _ in ai_calls()] == ["ok", "pending"]   # unsettled (no database): AI_INFLIGHT_STALE settles it


def settings_list(app) -> list[int]:
    s, page = http(app, "GET", "/api/settings/page")
    assert s == 200
    return [a["id"] for a in page["mcp"]["pending_actions"]]


def test_the_stale_limits_outlast_what_a_turn_really_takes():
    """reconcile() and ai_reserve() settle what they think no turn can still own: a held card after HELD_STALE, a
    request left 'pending' after AI_INFLIGHT_STALE. The tests above show what a turn really takes: it publishes before
    TIMEOUT or not at all, a request's transport ends by TIMEOUT, and its bookkeeping is tried until TIMEOUT + SETTLE
    (+1 s: timestamps in whole seconds). A pin of the configuration, not a proof of a maximum: a finance_sync can hold
    its turn longer, and bookkeeping a database keeps refusing stays unwritten (the writer-stuck test above)."""
    bound = chat.TIMEOUT + chat.SETTLE + 1
    assert mcp_state.HELD_STALE > bound and usage.AI_INFLIGHT_STALE > bound


# --- R1: a tool step that only reads runs in a child process killed at the deadline -----------------------------------

SYNTHETIC = "Synthetic " + "a" * 40 + " Store"   # a merchant name "(a+)+$" backtracks over for hours (2^40 paths)
BACKTRACKS = "(a+)+$"                           # a valid regex the model may propose; re has no timeout
SRC = str(chat.CODE) if hasattr(chat, "CODE") else os.path.dirname(os.path.dirname(chat.__file__))

RUN_TOOL = r'''
import json, os, sys, threading, time
from datetime import date
from pathlib import Path

def _end_with_parent():
    parent, born = os.getppid(), time.monotonic()
    while True:
        time.sleep(1.0)
        if os.getppid() != parent or time.monotonic() - born > 900:
            os._exit(0)                                    # (the pytest that started us is gone, or 15 min: never stray)

threading.Thread(target=_end_with_parent, daemon=True).start()

src, db_path, pattern, deadline, today = sys.argv[1:6]
sys.path.insert(0, src)
from hermes_finance import chat, tooldefs
tooldefs.today = lambda: date.fromisoformat(today)
# the marker comes in the environment, so only the isolated step's own command line shows it (code without one: none)
chat.WORKER = getattr(chat, "WORKER", "") + "  # " + os.environ["HF_CHAT_DEADLINE_MARKER"]
start = time.monotonic()
try:
    result, payload, action = chat._run_tool(Path(db_path), date.fromisoformat(today), "finance_add_rule",
                                             json.dumps({"pattern": pattern, "category": "Other Shopping"}), "t-regex",
                                             start + float(deadline))
    got = {"result": json.loads(result), "card": action is not None}
except chat.ChatError as e:
    got = {"error": [e.status, str(e)]}
got["seconds"] = round(time.monotonic() - start, 3)
print(json.dumps(got), flush=True)
'''


def synthetic_merchant_db() -> str:
    """The full synthetic DB with one transaction's merchant renamed SYNTHETIC; its path."""
    path = config.db_path()
    build("full", path)
    conn = sqlite3.connect(path)
    with conn:
        [(tid,)] = conn.execute("SELECT transaction_id FROM transactions WHERE removed = 0 ORDER BY rowid LIMIT 1")
        conn.execute("UPDATE transactions SET merchant_name = ? WHERE transaction_id = ?", (SYNTHETIC, tid))
    conn.close()
    return str(path)


def run_tool_child(pattern: str, marker: str, deadline: float, timebox: float | None = None):
    """chat._run_tool(finance_add_rule, pattern) in a child process of its own (its own group, killed at `timebox`
    seconds: a regex running in this process could never be stopped): (its JSON line or None, its Popen)."""
    proc = subprocess.Popen([sys.executable, "-I", "-c", RUN_TOOL, SRC, synthetic_merchant_db(), pattern, str(deadline),
                             TODAY.isoformat()], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, "HF_CHAT_DEADLINE_MARKER": marker})
    if timebox is None:
        return None, proc
    try:
        line = first_line(proc, timebox)
    except BaseException:
        kill_group(proc)
        raise
    return (json.loads(line) if line.startswith("{") else None), proc


def kill_group(proc: subprocess.Popen) -> None:
    if proc.poll() is None:   # (never reaped: its group is still its own)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(10)
    proc.stdout.close()


def marker_of(name: str) -> str:
    return f"hf-chat-deadline-{name}-{os.getpid()}-{time.monotonic_ns()}"


def test_a_rule_pattern_that_matches_quickly_still_makes_its_card_in_time(data_dir):
    """Positive control for the next test (same child process, same synthetic merchant): an ordinary pattern is
    planned in its isolated step and its card recorded (a 5 s deadline: this one is about the card, not the clock)."""
    got, proc = run_tool_child("Synthetic", marker_of("benign"), 5.0, 20)
    kill_group(proc)
    assert got is not None and got.get("card") is True and got["seconds"] < 5.0, got
    assert got["result"]["status"] == "pending_approval"
    assert "It matches 1 transaction today" in got["result"]["summary"]
    assert rows(config.db_path(), "SELECT tool, held_by, status FROM pending_actions") == [
        ("finance_add_rule", "t-regex", "pending")]


def test_a_rule_pattern_that_backtracks_for_hours_ends_the_turn_at_its_deadline(data_dir):
    """R1, the review's case: the model proposes a valid regex that backtracks catastrophically over a synthetic
    merchant name. Its plan (describe) runs in a child process killed at the deadline: LATE (504) by the deadline, no
    card, and the child is gone. Without that, the plan runs in the turn's own process until the timebox kills it."""
    marker = marker_of("regex")
    got, proc = run_tool_child(BACKTRACKS, marker, DEADLINE, DEADLINE + SLACK + 6)
    try:
        assert got is not None, "still planning the rule when the timebox ended (nothing could cut it short)"
        assert (got.get("error"), got["seconds"] < DEADLINE + SLACK) == ([504, chat.LATE], True), got
        assert marker not in processes()                                  # killed and waited for, not left running
        assert rows(config.db_path(), "SELECT COUNT(*) FROM pending_actions") == [(0,)]
    finally:
        kill_group(proc)


def test_the_isolated_step_ends_itself_by_the_deadline_even_when_the_app_is_killed(data_dir):
    """The child has its own alarm (the deadline + 1 s, rounded up): SIGKILL the process that started it (it can't
    clean up) while the child backtracks, and the child still ends by then, not hours later."""
    marker = marker_of("orphan")
    start = time.monotonic()
    _, proc = run_tool_child(BACKTRACKS, marker, 3.0)
    try:
        seen = None
        while seen is None and time.monotonic() - start < 10:
            if marker in processes():
                seen = time.monotonic()
            else:
                time.sleep(0.05)
        assert seen is not None, "no isolated child ever ran"
        time.sleep(0.5)                                                   # (it has its question: it's backtracking)
        os.killpg(proc.pid, signal.SIGKILL)                               # the app, gone (its child: its own group)
        proc.wait(10)
        assert marker in processes(), "the child ended with the app: this test shows nothing"
        while marker in processes() and time.monotonic() - seen < 3.0 + 1 + 1 + SLACK + 2:
            time.sleep(0.1)
        took = round(time.monotonic() - seen, 3)
        assert marker not in processes() and took < 3.0 + 1 + 1 + SLACK, took
    finally:
        kill_group(proc)
        for line in processes().splitlines():                            # never leave one behind, whatever failed
            if marker in line:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(line.split()[0]), signal.SIGKILL)


def outcome_of(fn) -> tuple:
    try:
        return "ok", json.loads(json.dumps(fn(), default=str))
    except tooldefs.ToolError as e:
        return "refused", str(e)


def test_an_isolated_step_answers_exactly_what_the_step_answers_in_process(data_dir):
    """Nothing about what a tool answers changes by running it in the child: every read tool, each write's plan
    (describe: the checked arguments and the card's text, what _record stores with origin ask and the turn's hold),
    finance_sync's approval check, and a refusal, compared with the same call in this process."""
    build("full", config.db_path())
    path, deadline = config.db_path(), time.monotonic() + 60
    for name in tooldefs.READS:
        assert outcome_of(lambda: chat._isolated("read", name, {}, path, TODAY, deadline)) == \
            outcome_of(lambda: tooldefs.run_read(name, {}, path, TODAY)), name
    by_category = {"by": "category", "month": "2026-09"}
    assert outcome_of(lambda: chat._isolated("read", "finance_spend", by_category, path, TODAY, deadline)) == \
        outcome_of(lambda: tooldefs.run_read("finance_spend", by_category, path, TODAY))
    [(tid, merchant)] = rows(path, "SELECT transaction_id, merchant_name FROM transactions WHERE removed = 0 AND "
                                   "merchant_name IS NOT NULL ORDER BY rowid LIMIT 1")
    for name, args in [("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900}),
                       ("finance_set_cap", {"category": "Nope", "monthly_cap": 900}),
                       ("finance_add_rule", {"pattern": merchant[:6], "category": "Other Shopping"}),
                       ("finance_set_category", {"transaction_id": tid, "category": "Other Shopping"}),
                       ("finance_track_bill", {"action": "track", "merchant": merchant})]:
        got = outcome_of(lambda: chat._isolated("describe", name, args, path, TODAY, deadline))
        assert got == outcome_of(lambda: list(tooldefs.describe(name, args, path))), (name, args, got)
    for args in ({}, {"refresh": True}):
        assert outcome_of(lambda: chat._isolated("approval", "finance_sync", args, path, TODAY, deadline)) == \
            outcome_of(lambda: list(tooldefs.approval_needed("finance_sync", args, path)))
    assert outcome_of(lambda: chat._isolated("read", "finance_spend", {"by": "planet"}, path, TODAY, deadline))[0] == \
        "refused"


ZOMBIE_WORKER = r'''
import json, os, signal, sys, time
from datetime import date
from pathlib import Path

signal.alarm(int(sys.argv[2]))                      # (the same own-alarm bound a real worker has: never left behind)
sys.path.insert(0, sys.argv[1])
from hermes_finance import tooldefs

ask = json.loads(sys.stdin.buffer.read())
tooldefs.today = lambda: date.fromisoformat(ask["today"])
answer = {"ok": tooldefs.run_read(ask["name"], ask["args"], Path(ask["path"]), date.fromisoformat(ask["day"]))}
sys.stdout.write(json.dumps(answer, default=str))
sys.stdout.flush()
os.close(1)                                         # EOF: _worker_exchange returns with the answer
time.sleep(30)                                      # still running at the cleanup: killpg is the branch under test
'''


def test_a_group_macos_refuses_to_kill_still_answers_the_step(data_dir, monkeypatch):
    """The child has answered and closed stdout but not exited, so the turn's cleanup calls killpg. On macOS a group
    holding only an unreaped zombie refuses that with EPERM (errno 1), not ESRCH; the step's answer must still come
    back (the group is the child's own, so a refusal means nothing is left to kill there) and the child must be gone
    when the test ends, whether the cleanup succeeded or not."""
    build("full", config.db_path())
    path, deadline = config.db_path(), time.monotonic() + 2.5
    args = {"by": "category", "month": "2026-09"}
    expected = json.loads(json.dumps(tooldefs.run_read("finance_spend", args, path, TODAY), default=str))

    monkeypatch.setattr(chat, "WORKER", ZOMBIE_WORKER)
    refused = []
    real_killpg = os.killpg              # (so this test can still kill the child it started)

    def eperm(pgid, sig):
        refused.append((pgid, sig))      # what macOS does when the group's only member is a zombie
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", eperm)
    procs, real_popen = [], subprocess.Popen

    def popen(*a, **k):
        proc = real_popen(*a, **k)
        procs.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    try:
        got = chat._isolated("read", "finance_spend", args, path, TODAY, deadline)
    finally:
        for proc in procs:               # only what this test started, killed and reaped however it ended
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    real_killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(10)
            proc.stdin.close()
            proc.stdout.close()
    assert got == expected
    assert [pgid for pgid, _ in refused] == [procs[0].pid]
    assert procs[0].poll() is not None


def test_an_isolated_step_that_crashes_is_an_internal_error_that_names_only_its_type(data_dir):
    """Anything the child raises that isn't a ToolError (here a real one: the plan's table is missing) is a
    RuntimeError here naming its type, never its message; a child that ends without an answer, the same."""
    build("full", config.db_path())
    path = config.db_path()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE categories RENAME TO categories_gone")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match=r"\(OperationalError\)$"):
        chat._isolated("describe", "finance_set_cap", {"category": "Other Shopping", "monthly_cap": 9}, path, TODAY,
                       time.monotonic() + 30)


def test_a_turn_whose_isolated_step_dies_answers_500_and_leaves_no_card(app, gated, monkeypatch):
    """The child ends without an answer (killed, or its interpreter failed): the turn is an internal error (500), and
    the card it had recorded is withdrawn, never decidable."""
    gated.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})},
                    {"message": call_msg("finance_bills", {"next": "7d"}, "c2")}]
    real = chat.WORKER
    calls = []

    def popen(argv, *a, **k):
        calls.append(argv)
        if len(calls) > 1:   # the read (the card's plan went through): a child that dies at once
            argv = [x if x != real else "import os; os._exit(3)" for x in argv]
        return real_popen(argv, *a, **k)

    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", popen)
    s, r = ask(app, "Raise Shopping, and my bills?")
    assert (s, r) == (500, {"error": "internal error (RuntimeError)"}), (s, r)
    [(aid, _, _, status)] = pending_rows()
    assert (status, result_of(aid)) == ("rejected", {"withdrawn": mcp_state.WITHDRAWN})


# --- R2: no step starts after the deadline (each one's calls counted, not only the final 504) -------------------------

class Oversleeping:
    """chat's view of time on a loaded machine: each sleep() overshoots by `extra` seconds."""

    monotonic = staticmethod(time.monotonic)

    def __init__(self, extra: float):
        self.extra = extra

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds + self.extra)


def late(fn) -> object:
    """fn()'s value, or (status, line) of the ChatError it raised."""
    try:
        return fn()
    except chat.ChatError as e:
        return e.status, str(e)


def test_no_database_try_starts_once_its_budget_is_spent(data_dir):
    """_write with its budget already spent runs no try at all (not even the first): LATE."""
    build("full", config.db_path())
    conn = sqlite3.connect(config.db_path(), timeout=0, isolation_level=None)
    calls = []

    def fn(c):
        calls.append(time.monotonic())
        return "written"

    try:
        assert (late(lambda: chat._write(time.monotonic() - 0.01, conn, fn)), calls) == ((504, chat.LATE), [])
    finally:
        conn.close()


def test_a_locked_database_is_not_tried_again_once_a_sleep_overshot_the_budget(data_dir, monkeypatch):
    """_write: a try that found the database locked is not tried again after a sleep that overshot `until` (a loaded
    machine): its "database is locked" is raised instead, and every try started before `until`."""
    build("full", config.db_path())
    conn = sqlite3.connect(config.db_path(), timeout=0, isolation_level=None)
    holder = sqlite3.connect(config.db_path(), timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    calls = []

    def busy(c):
        calls.append(time.monotonic())
        c.execute("BEGIN IMMEDIATE")   # another writer holds it: SQLITE_BUSY at once (busy_timeout 0)

    try:
        until = time.monotonic() + 0.3
        monkeypatch.setattr(chat, "time", Oversleeping(0.5))
        try:
            got = chat._write(until, conn, busy)
        except sqlite3.OperationalError as e:
            got = str(e)
        monkeypatch.setattr(chat, "time", time)
        assert ([round(t - until, 3) for t in calls if t >= until], len(calls), got) == ([], 1, "database is locked")
    finally:
        holder.rollback()
        holder.close()
        conn.close()


def test_no_reservation_try_starts_once_the_deadline_passed(data_dir, monkeypatch):
    """_reserve with the deadline already passed reserves nothing: usage.ai_reserve is never called and no api_calls row
    is written (LATE)."""
    build("full", config.db_path())
    conn = db.connect(config.db_path())
    real = usage.ai_reserve
    calls = []

    def counting(*a, **k):
        calls.append(time.monotonic())
        return real(*a, **k)

    monkeypatch.setattr(usage, "ai_reserve", counting)
    try:
        got = late(lambda: chat._reserve(conn, TODAY, 5.0, time.monotonic() - 0.01))
    finally:
        conn.close()
    assert (got, calls) == ((504, chat.LATE), []), (got, calls)
    assert rows(config.db_path(), "SELECT COUNT(*) FROM api_calls WHERE provider = 'ai'") == [(0,)]


def test_a_refused_reservation_is_not_tried_again_once_a_sleep_overshot_the_deadline(data_dir, monkeypatch):
    """_reserve: a refusal (another turn's request is out) is not tried again after a sleep that overshot the deadline:
    429 with that refusal, and every try started before the deadline."""
    build("full", config.db_path())
    conn = db.connect(config.db_path())
    calls = []

    def inflight(*a, **k):
        calls.append(time.monotonic())
        raise usage.Refused(usage.AI, "inflight", "Ask is still answering another question. Try again in a moment.")

    monkeypatch.setattr(usage, "ai_reserve", inflight)
    deadline = time.monotonic() + 0.5
    monkeypatch.setattr(chat, "time", Oversleeping(0.6))
    try:
        got = late(lambda: chat._reserve(conn, TODAY, 5.0, deadline))
    finally:
        monkeypatch.setattr(chat, "time", time)
        conn.close()
    assert ([round(t - deadline, 3) for t in calls if t >= deadline], len(calls), got[0]) == ([], 1, 429), (got, calls)


def test_no_card_is_recorded_after_the_deadline(data_dir, monkeypatch):
    """_record with the deadline spent: no connection is even opened, record_pending never runs, no row (LATE)."""
    build("full", config.db_path())
    opened, recorded = [], []
    real_open, real_record = tooldefs.write_connection, mcp_state.record_pending
    monkeypatch.setattr(tooldefs, "write_connection", lambda *a, **k: opened.append(a) or real_open(*a, **k))
    monkeypatch.setattr(mcp_state, "record_pending", lambda *a, **k: recorded.append(a) or real_record(*a, **k))
    got = late(lambda: chat._record(config.db_path(), "finance_set_cap",
                                    {"category": "Other Shopping", "monthly_cap": 900},
                                    "Set the monthly budget cap on Other Shopping to $900 (now $850).", "t-late",
                                    time.monotonic() - 0.01))
    assert (got, len(opened), len(recorded)) == ((504, chat.LATE), 0, 0), (got, opened, recorded)
    assert rows(config.db_path(), "SELECT COUNT(*) FROM pending_actions") == [(0,)]


def hermes_on(tmp_path, monkeypatch):
    """Hermes mode through the bridge, its child the scripted stand-in (tests/bridge_stub.py: not Hermes)."""
    stub = bridge_stub.StubHermes(tmp_path / "stub", monkeypatch)
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.mode": "hermes"})
    b = chat.brain(conn)
    conn.close()
    return stub, b


def count_reads(monkeypatch, first_takes: float = 0.0) -> list[str]:
    """Every read that starts, however it runs: tooldefs.run_read in this process, or a child process started for one
    (_isolated). With `first_takes`, the first one takes that long before it starts its work."""
    started: list[str] = []
    real_read, real_popen = tooldefs.run_read, subprocess.Popen

    def read(name, *a, **k):
        started.append(name)
        if len(started) == 1:
            time.sleep(first_takes)
        return real_read(name, *a, **k)

    def popen(argv, *a, **k):
        if any("_worker()" in str(x) for x in argv):
            started.append("child")
            if len(started) == 1:
                time.sleep(first_takes)
        return real_popen(argv, *a, **k)

    monkeypatch.setattr(tooldefs, "run_read", read)
    monkeypatch.setattr(subprocess, "Popen", popen)
    return started


def hermes_turn(b, deadline: float):
    conn = db.connect(config.db_path())
    try:
        return chat._hermes_turn(conn, b, [{"role": "user", "content": "Shopping?"}], config.db_path(), TODAY, deadline)
    finally:
        conn.close()


def test_no_hermes_read_starts_after_the_deadline(app, tmp_path, monkeypatch):
    """_hermes_turn with its deadline spent reads nothing and starts no bridge request: 504."""
    stub, b = hermes_on(tmp_path, monkeypatch)
    stub.script(bridge_stub.read("finance_budget_status"))
    started = count_reads(monkeypatch)
    got = late(lambda: hermes_turn(b, time.monotonic() - 0.01))
    assert (started, got[0] if isinstance(got, tuple) else got, stub.records()) == ([], 504, []), (started, got)


def test_a_hermes_read_that_takes_until_the_deadline_starts_no_other(app, tmp_path, monkeypatch):
    """The model's first decision is a read that takes past the deadline: no other request or read starts, 504."""
    stub, b = hermes_on(tmp_path, monkeypatch)
    stub.script(bridge_stub.read("finance_budget_status"), bridge_stub.read("finance_bills"), bridge_stub.answer("x"))
    started = count_reads(monkeypatch, first_takes=3.0)
    got = late(lambda: hermes_turn(b, time.monotonic() + chat.bridge.MIN_CALL + 0.5))
    assert (len(started), got[0] if isinstance(got, tuple) else got, len(stub.requests())) == (1, 504, 1), (started, got)


def test_finance_sync_never_starts_when_its_approval_check_took_until_the_deadline(data_dir, monkeypatch):
    """finance_sync (in this process, not cut short once started) runs only if the deadline hasn't passed after its
    approval check: a check that answers "no card needed" past the deadline starts no sync (LATE)."""
    build("full", config.db_path())
    synced = []
    monkeypatch.setattr(tooldefs, "run_action", lambda *a, **k: synced.append(a) or {"synced": 0})

    def slow_check(name, args, *rest):
        time.sleep(0.5)
        return args, None

    def slow_step(step, name, args, *rest):
        assert step == "approval"
        time.sleep(0.5)
        return [args, None]

    monkeypatch.setattr(tooldefs, "approval_needed", slow_check)            # (in this process: code without _isolated)
    monkeypatch.setattr(chat, "_isolated", slow_step, raising=False)        # (in its child)
    got = late(lambda: chat._run_tool(config.db_path(), TODAY, "finance_sync", "{}", "t-sync", time.monotonic() + 0.3))
    assert (synced, got) == ([], (504, chat.LATE)), (synced, got)


# --- R3: connection setup waits for no lock by SQLite's clock ---------------------------------------------------------

HOLDER = r'''
import select, sqlite3, sys, time
path, state, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
c = sqlite3.connect(path, timeout=5, isolation_level=None)
if state.startswith("rollback"):     # (the app always opens in WAL: another program would have to switch it)
    c.execute("PRAGMA journal_mode=DELETE")
if state == "rollback-reader":       # a reader: the schema check passes, db.connect's journal_mode=WAL can't
    c.execute("BEGIN")
    c.execute("SELECT COUNT(*) FROM categories").fetchone()
elif state == "rollback-journal":    # a writer: even reading waits
    c.execute("BEGIN EXCLUSIVE")
    c.execute("CREATE TABLE IF NOT EXISTS lock_probe (x)")
    c.execute("INSERT INTO lock_probe VALUES (1)")
else:                                # "exclusive-locking": locking_mode=EXCLUSIVE keeps its lock after a write
    c.execute("PRAGMA locking_mode=EXCLUSIVE")
    c.execute("BEGIN IMMEDIATE")
    c.execute("CREATE TABLE IF NOT EXISTS lock_probe (x)")
    c.execute("INSERT INTO lock_probe VALUES (1)")
    c.execute("COMMIT")
print("HELD", flush=True)
select.select([sys.stdin], [], [], hold)   # until `hold` seconds have passed or the test closes stdin
if c.in_transaction:
    c.rollback()                           # (no hot journal left behind)
c.close()
'''


@contextlib.contextmanager
def held_file(state: str, hold: float):
    """Another process holding the database so that even opening it waits (see HOLDER), for `hold` seconds at most."""
    proc = subprocess.Popen([sys.executable, "-I", "-c", HOLDER, str(config.db_path()), state, str(hold)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        assert first_line(proc, 10) == "HELD"
        yield proc
    finally:
        proc.stdin.close()                 # it lets go (rolled back) and ends; killed only if it doesn't
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(5)
        kill_group(proc)


SETUP_LOCKS = ["rollback-journal", "exclusive-locking", "rollback-reader"]


def test_the_setup_lock_is_real_an_ordinary_open_waits_on_it(data_dir):
    """Control for the tests below: in these states an ordinary open for writing (tooldefs.write_connection with
    SQLite's own busy timeout: its schema check, or db.connect's journal_mode) waits as long as the holder holds (here
    1.5 s), where a WAL writer holding BEGIN IMMEDIATE wouldn't stop it at all."""
    build("full", config.db_path())
    for state in SETUP_LOCKS:
        with held_file(state, 1.5):
            start = time.monotonic()
            try:
                tooldefs.write_connection(config.db_path()).close()
            except (tooldefs.ToolError, sqlite3.OperationalError):
                pass
            took = time.monotonic() - start
        assert took > 1.0, (state, took)


@pytest.mark.parametrize("state", SETUP_LOCKS)
def test_recording_a_card_ends_by_the_deadline_even_while_the_file_cannot_be_opened(data_dir, state):
    """R3: _record's connection setup (the schema check, db.connect's pragmas) waits for no lock by SQLite's clock:
    with the file held for 5 s, it gives up by its 1 s deadline and records nothing (the old setup waited for the
    holder, up to 30 s of SQLite's sleeps)."""
    build("full", config.db_path())
    with held_file(state, 5.0):
        start = time.monotonic()
        try:
            got = chat._record(config.db_path(), "finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900},
                               "Set the monthly budget cap on Other Shopping to $900 (now $850).", "t-lock",
                               start + DEADLINE)
        except (chat.ChatError, tooldefs.ToolError, sqlite3.OperationalError) as e:
            got = f"{type(e).__name__}: {e}"
        took = round(time.monotonic() - start, 3)
    assert (took < DEADLINE + SLACK, isinstance(got, str)) == (True, True), (took, got)
    assert rows(config.db_path(), "SELECT COUNT(*) FROM pending_actions") == [(0,)]


@pytest.mark.parametrize("name, args", [("finance_spend", {"by": "category"}),
                                        ("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900}),
                                        ("finance_sync", {})])
def test_a_tool_step_waiting_on_a_lock_ends_the_turn_at_its_deadline(data_dir, monkeypatch, name, args):
    """R1/R3: a read, a write's plan, or finance_sync's approval check whose read-only connection waits on another
    process's lock (SQLite's 30 s there) ends the turn at its deadline: its child is killed (LATE), no card, no sync."""
    build("full", config.db_path())
    synced = []
    monkeypatch.setattr(tooldefs, "run_action", lambda *a, **k: synced.append(a) or {"synced": 0})
    with held_file("rollback-journal", 5.0):
        start = time.monotonic()
        got = late(lambda: chat._run_tool(config.db_path(), TODAY, name, json.dumps(args), "t-wait", start + DEADLINE))
        took = round(time.monotonic() - start, 3)
    assert (got, took < DEADLINE + SLACK, synced) == ((504, chat.LATE), True, []), (got, took, synced)
    assert rows(config.db_path(), "SELECT COUNT(*) FROM pending_actions") == [(0,)]


def test_withdrawing_ends_by_its_budget_even_while_the_file_cannot_be_opened(data_dir):
    """R3 for the failure path: _withdraw gives up by `until` (the card stays held for reconcile()), instead of
    waiting for the holder."""
    build("full", config.db_path())
    conn = db.connect(config.db_path())
    mcp_state.record_pending(conn, "finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900}, "s", "Ask",
                             origin="ask", held_by="t-held")
    conn.close()
    with held_file("rollback-journal", 5.0):
        start = time.monotonic()
        chat._withdraw(config.db_path(), "t-held", start + DEADLINE)
        took = round(time.monotonic() - start, 3)
    assert took < DEADLINE + SLACK, took
    assert rows(config.db_path(), "SELECT status, held_by FROM pending_actions") == [("pending", "t-held")]


def test_the_turns_connection_waits_by_the_clock_and_opens_once_the_file_is_free(data_dir, monkeypatch):
    """Positive control for the two above: _writer tries again (by the clock, DB_STEP apart) while the file can't be
    opened, and opens it once the holder lets go (0.5 s), well before its budget: busy_timeout 0 from its first
    statement to the end (its writes wait by _write's clock instead)."""
    build("full", config.db_path())
    tries = []
    real = tooldefs.write_connection
    monkeypatch.setattr(tooldefs, "write_connection", lambda *a, **k: tries.append(k) or real(*a, **k))
    with held_file("rollback-journal", 0.5) as holder:
        start = time.monotonic()
        conn = chat._writer(config.db_path(), start + 5.0)
        took = round(time.monotonic() - start, 3)
        holder.wait(5)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"      # (db.connect's own setup, as ever)
    finally:
        conn.close()
    assert len(tries) > 1 and set(map(str, tries)) == {"{'timeout': 0}"} and took < 5.0, (len(tries), took)


def test_the_timeout_parameter_changes_nothing_else_about_opening_a_database(data_dir, tmp_path):
    """db.connect / connect_readonly and tooldefs' connections keep their 30 s default (and create, open and upgrade
    exactly as before); timeout=0 only changes the busy timeout. The turn's connection never creates or upgrades."""
    fresh = tmp_path / "fresh" / "finance.db"
    conn = db.connect(fresh)
    assert (conn.execute("PRAGMA busy_timeout").fetchone()[0], conn.execute("PRAGMA user_version").fetchone()[0],
            conn.execute("PRAGMA journal_mode").fetchone()[0]) == (30000, db.SCHEMA_VERSION, "wal")
    conn.close()
    conn = tooldefs.write_connection(fresh)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    conn.close()
    conn = tooldefs.write_connection(fresh, timeout=0)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    conn.close()
    tooldefs.read_connection(fresh, timeout=0).close()
    missing = tmp_path / "none" / "finance.db"
    with pytest.raises(tooldefs.ToolError, match="database not found"):
        chat._writer(missing, time.monotonic() + 5)
    assert not missing.exists()
    old = sqlite3.connect(fresh)
    old.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    old.close()
    start = time.monotonic()
    with pytest.raises(tooldefs.ToolError, match=f"schema v{db.SCHEMA_VERSION - 1}"):
        chat._writer(fresh, start + 5)
    assert time.monotonic() - start < 1.0                                   # refused at once, not retried
    assert rows(fresh, "PRAGMA user_version") == [(db.SCHEMA_VERSION - 1,)]  # and not upgraded
    conn = db.connect(fresh)                                                # the app's own open still upgrades
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


# --- transport coverage the review listed as untested: chunked, TLS, connect, address fallback ------------------------

@pytest.mark.parametrize("mode, want", [("chunked", ("answer", "synthetic answer", 16)),
                                        ("chunkcut", ("error", 502, UNREADABLE)),
                                        ("chunkhuge", ("error", 502, UNREADABLE))])
def test_a_chunked_answer_is_read_and_a_cut_off_or_huge_one_is_not(provider, mode, want):
    """Transfer-Encoding: chunked (http.client decodes it): a valid one is read; one cut off before its last chunk,
    or one over MAX_RESPONSE, fails closed."""
    got, took = outcome(f"{provider}/{mode}-a/chat/completions", 10.0)
    assert got == want, (got, took)


def test_a_chunked_answer_trickling_in_never_outlasts_the_deadline(provider):
    got, took = outcome(f"{provider}/chunkdrip-a/chat/completions", DEADLINE)
    assert (got[:2], took < DEADLINE + SLACK) == (("error", 504), True), (got, took)


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    """A throwaway self-signed certificate for 127.0.0.1 (made here, an hour's validity; nothing in the repo)."""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hf-chat-deadline test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    d = tmp_path_factory.mktemp("tls")
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    (d / "key.pem").chmod(0o600)
    yield d
    for f in ("cert.pem", "key.pem"):
        (d / f).unlink()


@pytest.fixture(scope="module")
def tls_provider(certificate):
    yield from serve("https", str(certificate / "cert.pem"), str(certificate / "key.pem"))


@pytest.fixture
def trusted(certificate, monkeypatch):
    """chat's TLS context trusts the throwaway certificate (and nothing else); verification stays as it is."""
    real = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: real(cafile=str(certificate / "cert.pem")))


def test_tls_an_answer_at_once_is_read(tls_provider, trusted):
    got, took = outcome(f"{tls_provider}/ok-tls/chat/completions", DEADLINE)
    assert got == ("answer", "synthetic answer", 16) and took < DEADLINE, (got, took)


def test_tls_an_answer_trickling_in_never_outlasts_the_deadline(tls_provider, trusted):
    got, took = outcome(f"{tls_provider}/drip-tls/chat/completions", DEADLINE)
    assert (got[:2], took < DEADLINE + SLACK) == (("error", 504), True), (got, took)


def test_tls_a_handshake_that_never_comes_ends_by_the_deadline(trusted):
    """The server takes the connection and never speaks TLS: the handshake gets what's left of the deadline."""
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(4)                                     # (the kernel accepts the TCP connection; nobody answers)
    try:
        got, took = outcome(f"https://127.0.0.1:{silent.getsockname()[1]}/v1/chat/completions", DEADLINE)
    finally:
        silent.close()
    assert (got[:2], took < DEADLINE + SLACK) == (("error", 504), True), (got, took)


def test_tls_a_certificate_nobody_trusts_is_refused(tls_provider):
    """Verification isn't loosened: without the test's trust, the same server is refused (nothing is read)."""
    got, took = outcome(f"{tls_provider}/ok-untrusted/chat/completions", DEADLINE)
    assert got[:2] == ("error", 502) and "Couldn't reach" in got[2], (got, took)


def test_an_address_that_refuses_is_passed_over_for_the_next(provider, monkeypatch):
    """A name with two addresses, the first refusing: the second answers (each tried gets what's left)."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    refused = closed.getsockname()[1]
    closed.close()
    good = int(provider.rsplit(":", 1)[1])
    tried = []

    def lookup(host, port, deadline):
        tried.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", refused)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", good))]

    monkeypatch.setattr(chat, "_lookup", lookup)
    got, took = outcome(f"http://localhost:{good}/ok-fallback/chat/completions", DEADLINE)
    assert (got, tried) == (("answer", "synthetic answer", 16), ["localhost"]), (got, took)


def test_a_connect_that_stalls_ends_by_the_deadline_and_tries_nothing_after(provider, monkeypatch):
    """A connect that's never answered (on this Mac a loopback one always is, so it's played by a socket that waits
    out its own timeout as an unanswered SYN would): the request ends by the deadline, and no later address is tried
    (there's no time left for it)."""
    good = int(provider.rsplit(":", 1)[1])
    stall = ("127.0.0.1", 1)
    tried = []

    class Unanswered(socket.socket):
        def connect(self, address):
            tried.append(address)
            if address == stall:
                time.sleep(self.gettimeout())
                raise TimeoutError("timed out")
            return super().connect(address)

    monkeypatch.setattr(chat, "_lookup", lambda host, port, deadline: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", stall),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", good))])
    monkeypatch.setattr(socket, "socket", Unanswered)
    got, took = outcome(f"http://localhost:{good}/ok-stall/chat/completions", DEADLINE)
    assert (got[:2], took < DEADLINE + SLACK, tried) == (("error", 504), True, [stall]), (got, took, tried)
