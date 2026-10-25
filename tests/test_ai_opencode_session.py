"""OpenCode's gateway (opencode.ai) refuses an Ask request without an `x-opencode-session` header (400
MissingSessionID). An AI base URL whose host is opencode.ai or a subdomain (parsed, never a substring match) gets one on
every request: a random per-install id made once, kept in the settings table (ai.opencode_session), never derived from
the API key, and left out of backups (aisession.py). Any other host gets nothing, and no id is made for it.

Against a fake OpenAI-compatible server on a loopback port that answers 400 MissingSessionID without the header and 200
with it, reached through the real route (App.respond POST /api/chat). The opencode.ai names "resolve" to 127.0.0.1
through a patched chat._lookup: no real name is ever looked up (conftest's no_network)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_tools import TODAY
from hermes_finance import backup, chat, config, db, prefs, redact, secretstore, tooldefs
from hermes_finance.web import App

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
KEY = "oc-test-not-a-real-key-for-ask"   # invented: no digits, so it isn't shaped like any provider's key
MODEL = "deepseek-v4.1-flash"
SETTING = "ai.opencode_session"           # (pinned here too: the old code has no aisession module to read it from)
HEADER = "x-opencode-session"
MISSING = json.dumps({"type": "error", "error": {"type": "MissingSessionID",
                                                 "message": "x-opencode-session header is required"}}).encode()
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
AT = "@"                                  # (URLs with userinfo are built: the release scan reads them as e-mails)
RESOLVED = {"opencode.ai", "api.opencode.ai", "opencode.ai.evil.com", "evilopencode.ai"}


def aisession():
    from hermes_finance import aisession as mod   # (imported per test: the end-to-end ones also run on the old code)
    return mod


class FakeOpenCode:
    """An OpenAI-compatible /chat/completions that, like OpenCode's gateway, answers 400 MissingSessionID to a request
    without the x-opencode-session header; with it, the next scripted step (a message, or a status and raw body)."""

    def __init__(self):
        self.headers: list[dict[str, str]] = []
        self.paths: list[str] = []
        self.key_registered: list[bool] = []   # was the key already scrubbed by redact when the request arrived
        self.script: list[dict] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                got = {k.lower(): v for k, v in self.headers.items()}
                fake.headers.append(got)
                fake.paths.append(self.path)
                fake.key_registered.append(redact.scrub(KEY) != KEY)
                if not got.get(HEADER):
                    status, out = 400, MISSING
                else:
                    step = fake.script.pop(0) if fake.script else {"message": answer("(no script)")}
                    status = step.get("status", 200)
                    out = step.get("raw") or json.dumps({
                        "id": "x", "object": "chat.completion", "model": body.get("model"),
                        "choices": [{"index": 0, "message": step.get("message"), "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 10}}).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self, host: str = "opencode.ai", path: str = "/zen/go/v1") -> str:
        return f"http://{host}:{self.port}{path}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def call_msg(name: str, args: dict, cid: str = "c1") -> dict:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def answer(text: str) -> dict:
    return {"role": "assistant", "content": text}


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)
    monkeypatch.setattr(redact, "_secrets", set())   # so "registered before the request" is this test's doing


@pytest.fixture(autouse=True)
def names(no_network, monkeypatch):
    """The test names resolve to the loopback address (as getaddrinfo's child would answer); any other name is unknown.
    chat._open's own is-it-an-IP-address check (AI_NUMERICHOST, which looks nothing up) says "not one" for them, as the
    real getaddrinfo does, instead of tripping conftest's guard."""
    guard = socket.getaddrinfo

    def numeric_only(host, *args, flags=0, **kwargs):
        if host in RESOLVED and flags & socket.AI_NUMERICHOST:
            raise socket.gaierror(socket.EAI_NONAME, "not a numeric host")
        return guard(host, *args, flags=flags, **kwargs)

    def lookup(host, port, deadline):
        if host not in RESOLVED:
            raise OSError(f"no address for {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", numeric_only)
    monkeypatch.setattr(chat, "_lookup", lookup)


@pytest.fixture
def fake():
    f = FakeOpenCode()
    yield f
    f.close()


def make_db(path, url: str):
    """A fresh database at `path` in the api mode at `url` (the key is the secrets file's, the same for every one)."""
    config.ensure_data_dir(path.parent)
    conn = db.connect(path)
    prefs.save(conn, {"ai.mode": "api", "ai.preset": "custom", "ai.base_url": url, "ai.model": MODEL,
                      "ai.monthly_cap": 5.0})
    conn.close()
    return path


def make_app(path) -> App:
    a = App(path, lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


@pytest.fixture
def setup(data_dir):
    secretstore.write({secretstore.AI_KEY: KEY})

    def at(url: str, path=None):
        p = make_db(path or config.db_path(), url)
        return make_app(p), p
    return at


def ask(app, text="How am I doing?"):
    status, ctype, out, _ = app.respond("POST", "/api/chat", JSONH,
                                        json.dumps({"messages": [{"role": "user", "content": text}]}).encode())
    return status, out.decode()


def stored(path) -> str | None:
    c = sqlite3.connect(path)
    try:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (SETTING,)).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def set_stored(path, raw: str) -> None:
    c = sqlite3.connect(path)
    try:
        with c:
            c.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, 'z')", (SETTING, raw))
    finally:
        c.close()


# --- the host rule ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://opencode.ai/zen/go/v1",
    "https://OpenCode.AI/zen/go/v1",
    "https://api.opencode.ai/v1",
    "http://opencode.ai:8080/x",
    "https://opencode.ai./zen/go/v1",
    f"https://user{AT}opencode.ai/v1",
])
def test_an_opencode_host_matches(url):
    assert aisession().is_opencode(url) is True


@pytest.mark.parametrize("url", [
    "https://opencode.ai.evil.com/zen/go/v1",
    "https://evilopencode.ai/v1",
    "https://notopencode.ai/v1",
    "https://opencode.aix/v1",
    "evil.com/opencode.ai",
    "https://evil.com/opencode.ai",
    "https://evil.com?h=opencode.ai",
    f"https://opencode.ai{AT}evil.com/v1",   # userinfo: the host is evil.com
    "https://evil.com#opencode.ai",
    "https://evil.com/?u=https://opencode.ai/v1",
    "opencode.ai",                       # no scheme: no host to speak of
    "https://.opencode.ai/v1",
    "https://openrouter.ai/api/v1",
    "https://api.openai.com/v1",
    "http://127.0.0.1:11434/v1",
    "http://[::1/v1",                    # malformed (urllib raises)
    "",
    None,
])
def test_anything_else_does_not(url):
    assert aisession().is_opencode(url) is False


def test_no_preset_is_an_opencode_host():
    assert not any(aisession().is_opencode(u) for u in prefs.AI_PRESETS.values())


# --- end to end ------------------------------------------------------------------------------------------------------

def test_an_opencode_turn_sends_the_header_on_every_round(setup, fake):
    app, path = setup(fake.url())
    fake.script = [{"message": call_msg("finance_budget_status", {"month": "2026-09"})},
                   {"message": answer("You're within your budgets this month.")}]
    status, out = ask(app)
    assert status == 200, out
    assert json.loads(out)["reply"] == "You're within your budgets this month."
    assert len(fake.headers) == 2 and fake.paths == ["/zen/go/v1/chat/completions"] * 2
    sent = [h.get(HEADER) for h in fake.headers]
    assert sent[0] and sent[0] == sent[1]
    assert sent[0] == json.loads(stored(path)) and UUID4.fullmatch(sent[0])
    assert all(h.get("authorization") == f"Bearer {KEY}" for h in fake.headers)


@pytest.mark.parametrize("host", ["127.0.0.1", "opencode.ai.evil.com", "evilopencode.ai"])
def test_another_host_gets_no_header_and_no_id_is_made(setup, fake, host):
    app, path = setup(fake.url(host, "/v1"))
    status, out = ask(app)
    assert status == 502, out
    assert json.loads(out) == {"error": "The AI provider answered with an error (400). Try again in a minute."}
    assert len(fake.headers) == 1 and HEADER not in fake.headers[0]
    assert stored(path) is None


def test_a_subdomain_gets_it_too(setup, fake):
    app, path = setup(fake.url("api.opencode.ai", "/v1"))
    fake.script = [{"message": answer("Fine.")}]
    status, out = ask(app)
    assert status == 200, out
    assert fake.headers[0][HEADER] == json.loads(stored(path))


def test_the_id_is_stable_across_restarts_and_per_database(setup, fake, tmp_path):
    app, path = setup(fake.url())
    fake.script = [{"message": answer("One.")}]
    assert ask(app)[0] == 200
    first = fake.headers[-1][HEADER]
    # a restart: a new App (and its own connections) on the same database
    fake.script = [{"message": answer("Two.")}]
    assert ask(make_app(path))[0] == 200
    assert fake.headers[-1][HEADER] == first == json.loads(stored(path))
    # another install (database) with the SAME key: its own id
    other = make_db(tmp_path / "other" / "finance.db", fake.url())
    fake.script = [{"message": answer("Three.")}]
    assert ask(make_app(other))[0] == 200
    second = fake.headers[-1][HEADER]
    assert second != first and second == json.loads(stored(other))
    for sid in (first, second):
        assert UUID4.fullmatch(sid) and uuid.UUID(sid).version == 4
        assert KEY not in sid and sid not in KEY
        assert not any(KEY[i:i + 6] in sid for i in range(len(KEY) - 5))   # no part of the key
        digests = {getattr(hashlib, n)(KEY.encode()).hexdigest() for n in ("md5", "sha1", "sha256", "sha512")}
        bare = sid.replace("-", "")
        assert not any(bare in d or d.startswith(bare[:8]) for d in digests)
        assert str(uuid.uuid5(uuid.NAMESPACE_DNS, KEY)) != sid and str(uuid.uuid3(uuid.NAMESPACE_DNS, KEY)) != sid


@pytest.mark.parametrize("raw", ["not json", "42", "null", '"short"', json.dumps("g" * 36), json.dumps(["x"]),
                                 "UPPER", "evil\r\nx-injected: 1"])
def test_a_malformed_stored_value_is_replaced_never_sent(setup, fake, raw):
    app, path = setup(fake.url())
    set_stored(path, raw if raw != "UPPER" else json.dumps(str(uuid.uuid4()).upper()))
    before = stored(path)
    fake.script = [{"message": answer("Fine.")}]
    status, out = ask(app)
    assert status == 200, out
    sent = fake.headers[0][HEADER]
    assert UUID4.fullmatch(sent) and sent != before and json.dumps(sent) != before
    assert stored(path) == json.dumps(sent)
    assert "x-injected" not in fake.headers[0]


def test_first_turns_at_once_agree_on_one_id(data_dir):
    path = make_db(config.db_path(), "https://opencode.ai/zen/go/v1")
    mod, got, errors = aisession(), [], []
    start = threading.Barrier(8)

    def one():
        c = chat._writer(path, time.monotonic() + 10)   # busy_timeout 0, as a turn's: chat._write waits by the clock
        try:
            start.wait()
            got.append(chat._write(time.monotonic() + 10, c, mod.session_id))
        except Exception as e:   # (reported below)
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(got) == 8 and len(set(got)) == 1 and json.loads(stored(path)) == got[0]


def test_a_locked_database_cannot_hold_the_turn_past_its_deadline(setup, fake, monkeypatch):
    """The id is made with the turn's clock (chat._write: busy_timeout 0, tried by the clock), so a database another
    connection locks once the turn has started ends the turn by its deadline, and no request is sent without the
    header."""
    app, path = setup(fake.url())
    monkeypatch.setattr(chat, "TIMEOUT", 1.0)
    holder = sqlite3.connect(path, isolation_level=None)
    real = chat.mcp_state.reconcile

    def then_locked(conn):   # the route's own first write done, another writer takes the database
        out = real(conn)
        holder.execute("BEGIN IMMEDIATE")
        return out

    monkeypatch.setattr(chat.mcp_state, "reconcile", then_locked)
    try:
        t0 = time.monotonic()
        status, _ = ask(app)
        took = time.monotonic() - t0
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()
    assert status >= 500 and took < chat.TIMEOUT + 1.0, (status, took)
    assert fake.headers == [] and stored(path) is None


def test_the_id_is_not_a_setting_any_route_reads_or_writes(setup, fake):
    app, path = setup(fake.url())
    fake.script = [{"message": answer("Fine.")}]
    assert ask(app)[0] == 200
    sid = json.loads(stored(path))
    for route in ("/api/settings", "/api/prefs", "/api/chat"):
        status, _, out, _ = app.respond("GET", route, LOCAL, b"")
        assert sid not in out.decode(errors="replace"), route
    status, _, out, _ = app.respond("POST", "/api/prefs", JSONH, json.dumps({SETTING: "chosen"}).encode())
    assert status >= 400 and json.loads(stored(path)) == sid
    assert SETTING not in prefs.SPEC


def test_a_backup_leaves_the_id_out(setup, fake, tmp_path):
    app, path = setup(fake.url())
    fake.script = [{"message": answer("Fine.")}]
    assert ask(app)[0] == 200 and stored(path)
    conn = db.connect(path)
    try:
        out = backup.make_backup(conn, tmp_path / "copy.db")
    finally:
        conn.close()
    assert SETTING in backup.STRIPPED_SETTINGS and stored(out) is None


# --- the key stays secret --------------------------------------------------------------------------------------------

class RawLines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        self.lines.append(self.format(record))   # (with its traceback, when it has one)


def test_the_key_never_leaks_on_any_path(setup, fake, caplog, capsys, monkeypatch):
    """A successful turn, a MissingSessionID 400, a 401 and a transport failure: the key is in no log record (caplog
    at DEBUG, and the stderr log `finance serve` installs), in no JSON the panel gets, not in the stored id nor the
    header; and it was registered with redact before the first request went out."""
    caplog.set_level(logging.DEBUG)
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    # A redacting handler an earlier test left on root (install_logging in the same worker) would rewrite each
    # record before `raw` sees it, so the proof below couldn't tell the filter's work from a no-op: park it.
    parked = [h for h in handlers if any(isinstance(f, redact.RedactingFilter) for f in h.filters)]
    for h in parked:
        root.removeHandler(h)
    raw = RawLines()   # each record as it was emitted (the redacting filter installed next rewrites records in place)
    root.addHandler(raw)
    redact.install_logging(logging.DEBUG)
    try:
        app, path = setup(fake.url())
        replies = []
        fake.script = [{"message": call_msg("finance_budget_status", {"month": "2026-09"})},
                       {"message": answer("All good.")}]
        replies.append(ask(app))
        fake.script = [{"status": 400, "raw": MISSING}]
        replies.append(ask(app))
        fake.script = [{"status": 401, "raw": json.dumps({"error": {"message": f"bad key {KEY}"}}).encode()}]
        replies.append(ask(app))
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead = closed.getsockname()[1]
        closed.close()
        prefs_conn = db.connect(path)
        prefs.save(prefs_conn, {"ai.base_url": f"http://opencode.ai:{dead}/zen/go/v1"})
        prefs_conn.close()
        replies.append(ask(app))
        logging.getLogger("hermes_finance.chat").warning("chat: a key-bearing line %s", KEY)   # the filter's own proof
    finally:
        for h in root.handlers[:]:
            if h not in handlers:
                root.removeHandler(h)
        for h in parked:
            root.addHandler(h)
        root.setLevel(level)
    assert [s for s, _ in replies] == [200, 502, 502, 502]
    errors = [json.loads(o).get("error") for _, o in replies[1:]]
    assert errors == ["The AI provider answered with an error (400). Try again in a minute.",
                      "The AI provider refused the key (401). Check it in Settings → AI provider.",
                      "Couldn't reach the AI provider. Is it running, and is the address in Settings right?"]
    for _, o in replies:
        assert KEY not in o and "MissingSessionID" not in o and "header is required" not in o
    proof = f"chat: a key-bearing line {KEY}"
    assert proof in raw.lines and caplog.records
    assert all(KEY not in line for line in raw.lines if line != proof)
    err = capsys.readouterr().err
    assert "a key-bearing line [REDACTED]" in err and KEY not in err
    sid = json.loads(stored(path))
    assert KEY not in stored(path) and all(KEY not in h.get(HEADER, "") for h in fake.headers)
    assert all(h.get(HEADER) == sid for h in fake.headers)
    assert fake.key_registered and all(fake.key_registered)
    masked = redact.scrub(f"provider said: bad key {KEY}")
    assert KEY not in masked and redact.MASK in masked
