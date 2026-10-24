"""TASK-23 review corrections (chat-review-e5a0939.md C1, C2, C3 and the server half of C5), against a fake
OpenAI-compatible server on a loopback port (no real LLM, no Plaid, nothing outside 127.0.0.1) and synthetic DBs only.

C1  a pending action's origin is set by the code path that records it (pending_actions.origin), never by what a client
    calls itself: an MCP client named exactly "Ask" gets no Ask card, and Settings doesn't say "Ask suggested it".
C2  an Ask card can't be decided (not by the panel, not by Settings: mcp_state.decide's claim) until its turn has
    answered; a turn that fails withdraws its cards, and a withdrawal that can't be written leaves them undecidable
    until mcp_state.reconcile() withdraws them.
C3  ai.monthly_cap is a soft stop after settlement: Ask requests go out one at a time per database (a second turn waits
    for the first request to be settled, then meets the cap), each bounded to MAX_OUTPUT_TOKENS; a request the app
    never settled stops blocking after usage.AI_INFLIGHT_STALE; a timed-out one is settled at its worst case.
C5  (server) an unexpected error while applying an Approve is a final, honest "may be partly done" state, and an Approve
    the app never finished (it stopped mid-apply) is settled the same way by reconcile(), never reopened as pending.

Round 2 (chat-corrections-review-3be89c58.md): N1 mcp_state.HELD_STALE outlasts the longest a turn holds a card,
derived from chat's constants (that this is the turn's real wall-clock bound, not just its budget, is
test_chat_deadline.py's); N2 a turn's cards are released only together with its reply's cards; N11 the Settings page
reconciles (and `finance mcp status` stays read-only)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_tools import TODAY, build, mcp_session, rows, run_cli
from hermes_finance import chat, config, db, mcp_state, prefs, secretstore, tooldefs, usage
from hermes_finance.web import App
from test_chat_task23 import KEY, MODEL, answer, ask, call_msg, cap_of, http, pending_rows, wire

STATIC = Path(__file__).resolve().parent.parent / "src" / "hermes_finance" / "static"


class GatedLLM:
    """test_chat_task23.FakeLLM's scripted /chat/completions, plus: a step with "hold" (a threading.Event) is answered
    only once the test sets it, and the fake counts the requests it is answering at the same moment (`peak`)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.script: list[dict] = []
        self.inflight = self.peak = 0
        self.cond = threading.Condition()
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake.cond:
                    fake.requests.append(body)
                    fake.headers.append(dict(self.headers.items()))
                    step = fake.script.pop(0) if fake.script else {"message": answer("(no script)")}
                    fake.inflight += 1
                    fake.peak = max(fake.peak, fake.inflight)
                    fake.cond.notify_all()
                try:
                    if step.get("hold") is not None:
                        step["hold"].wait(30)
                    if step.get("delay"):
                        time.sleep(step["delay"])
                    out = step.get("raw") or json.dumps({
                        "id": "x", "object": "chat.completion", "model": body.get("model"),
                        "choices": [{"index": 0, "message": step.get("message"), "finish_reason": "stop"}],
                        **({"usage": step["usage"]} if "usage" in step else {})}).encode()
                    self.send_response(step.get("status", 200))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with fake.cond:
                        fake.inflight -= 1

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def wait_requests(self, n: int, timeout: float) -> bool:
        """True once `n` requests have arrived (False if they haven't within `timeout` seconds)."""
        with self.cond:
            return self.cond.wait_for(lambda: len(self.requests) >= n, timeout)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)


@pytest.fixture
def llm():
    f = GatedLLM()
    yield f
    f.close()


@pytest.fixture
def app(data_dir, llm):
    build("full", config.db_path())
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.mode": "api", "ai.preset": "custom", "ai.base_url": llm.url, "ai.model": MODEL, "ai.monthly_cap": 5.0})
    conn.close()
    secretstore.write({secretstore.AI_KEY: KEY})
    a = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


@pytest.fixture
def served(app):
    from hermes_finance.web import make_server
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


class Turn(threading.Thread):
    """One POST /api/chat over real HTTP in its own thread; `.result` is (status, answer)."""

    def __init__(self, port: int, text: str):
        super().__init__(daemon=True)
        self.port, self.text, self.result = port, text, None

    def run(self):
        self.result = wire(self.port, "POST", "/api/chat", {"messages": [{"role": "user", "content": self.text}]})


def save_prefs(values: dict) -> None:
    conn = db.connect(config.db_path())
    prefs.save(conn, values)
    conn.close()


def sql(statement: str, args: tuple = ()) -> None:
    conn = db.connect(config.db_path())
    with conn:
        conn.execute(statement, args)
    conn.close()


def settings_pending(port: int | None, app=None) -> list[tuple]:
    """(id, client, origin) of what Settings lists as waiting for the owner."""
    s, page = wire(port, "GET", "/api/settings/page") if port else http(app, "GET", "/api/settings/page")
    assert s == 200
    return [(a["id"], a["client"], a.get("origin")) for a in page["mcp"]["pending_actions"]]


def result_of(action_id: int):
    raw = rows(config.db_path(), "SELECT result FROM pending_actions WHERE id = ?", (action_id,))[0][0]
    return json.loads(raw) if raw else None


def utc_ago(seconds: float) -> str:
    """A pending_actions timestamp `seconds` ago (db.now()'s format)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def longest_turn() -> float:
    """The longest an Ask turn holds a card: it releases it before its deadline (TIMEOUT from its start, checked under
    the write lock) or not at all, and tries to withdraw it until SETTLE after that; +1 s for created_at's whole
    seconds. Derived from chat's constants, so it follows them; test_chat_deadline.py shows the turn really keeps them
    (a reply trickling in, a late publish, a database lock). A card it couldn't withdraw (the database stayed locked),
    or whose turn a finance_sync held longer, is left to reconcile(): that turn can't publish it any more."""
    return chat.TIMEOUT + chat.SETTLE + 1


# --- C1: provenance is the server's, not the client's name ----------------------------------------------------------

def test_an_mcp_client_named_exactly_ask_gets_no_ask_card(app, llm):
    conn = db.connect(config.db_path())
    mcp_state.set_allow_writes(conn, True)
    conn.close()
    _, out = mcp_session(config.db_path(), [("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 999})], client="Ask")
    [(is_error, text)] = out
    assert not is_error, text
    spoof = json.loads(text)["pending_action"]
    assert spoof["client"] == "Ask" and spoof["status"] == "pending"      # the name it gave is kept, as its name only
    # the panel neither reads nor decides it
    s, got = http(app, "GET", f"/api/chat/cards?ids={spoof['id']}")
    assert s == 200 and got == {"cards": []}
    for approve in (True, False):
        s, r = http(app, "POST", "/api/chat/decide", {"id": spoof["id"], "approve": approve})
        assert s == 404 and r == {"error": f"no Ask card #{spoof['id']}"}
    assert pending_rows() == [(spoof["id"], "finance_set_cap", "Ask", "pending")] and cap_of("Other Shopping") == 850.0
    # Settings lists it as an MCP agent's (origin mcp), which is what its label is drawn from
    assert settings_pending(None, app) == [(spoof["id"], "Ask", "mcp")]
    # a card the panel really made is still the panel's own
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Health", "monthly_cap": 300})},
                  {"message": answer("Approve the card if you want it.")}]
    s, r = ask(app, "Raise Health?")
    assert s == 200, r
    [c] = r["cards"]
    s, got = http(app, "GET", f"/api/chat/cards?ids={spoof['id']},{c['id']}")
    assert [x["id"] for x in got["cards"]] == [c["id"]]
    assert sorted(settings_pending(None, app)) == [(spoof["id"], "Ask", "mcp"), (c["id"], "Ask", "ask")]
    s, d = http(app, "POST", "/api/chat/decide", {"id": c["id"], "approve": True})
    assert s == 200 and d["card"]["status"] == "applied" and cap_of("Other Health") == 300.0


def test_record_pending_with_the_name_ask_is_not_an_ask_card_owner_repro(app):
    """The owner's reproduction (chat-repro-362613a.py), whose assertion passed on the reviewed code: a row recorded
    with the client string "Ask" and no origin passed chat._own(). It must not."""
    conn = db.connect(config.db_path())
    try:
        a = mcp_state.record_pending(conn, "finance_set_cap", {"category": "Dining", "monthly_cap": 123},
                                     "Synthetic proposed cap", "Ask")
        assert chat._own(conn, a["id"]) is None and a["origin"] == "mcp"
    finally:
        conn.close()


def test_origin_is_one_the_server_knows(app):
    conn = db.connect(config.db_path())
    try:
        with pytest.raises(ValueError, match="origin"):
            mcp_state.record_pending(conn, "finance_set_cap", {"category": "Dining", "monthly_cap": 1}, "x", "Ask",
                                     origin="Ask")
        assert conn.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 0
    finally:
        conn.close()


def test_settings_labels_ask_from_the_origin_not_the_client_name():
    js = (STATIC / "settings.js").read_text()
    assert "a.client === 'Ask'" not in js and "a.origin === 'ask'" in js


def test_upgrade_to_v14_marks_every_earlier_pending_action_as_an_mcp_agents(data_dir):
    """A v13 database's pending actions (one of them from a client that called itself "Ask") all become origin mcp:
    nothing recorded before the origin existed can be an Ask card."""
    path = config.db_path()
    build("full", path)
    conn = db.connect(path)
    with conn:
        conn.execute("ALTER TABLE pending_actions DROP COLUMN origin")
        conn.execute("ALTER TABLE pending_actions DROP COLUMN held_by")
        for client in ("Ask", "Claude Desktop", None):
            conn.execute("INSERT INTO pending_actions (tool, arguments, summary, client, created_at) VALUES "
                         "('finance_set_cap', '{\"category\": \"Other Shopping\", \"monthly_cap\": 1}', 'x', ?, ?)",
                         (client, db.now()))
        conn.execute("PRAGMA user_version = 13")
    conn.close()
    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
        assert [(a["client"], a["origin"], a["held"]) for a in mcp_state.pending(conn)] == [
            ("Ask", "mcp", False), ("Claude Desktop", "mcp", False), (None, "mcp", False)]
        assert all(chat._own(conn, a["id"]) is None for a in mcp_state.pending(conn))
    finally:
        conn.close()


# --- C2: a card is decidable only once its turn has answered ----------------------------------------------------------

def test_settings_cannot_approve_a_card_while_its_turn_runs_and_a_failed_turn_withdraws_it(app, llm, served):
    hold = threading.Event()
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})},
                  {"status": 500, "raw": b'{"error": {"message": "boom"}}', "hold": hold}]
    t = Turn(served, "Raise Shopping?")
    t.start()
    try:
        assert llm.wait_requests(2, 20)            # the card is recorded and the turn's next round is out
        [(aid, _, client, status)] = pending_rows()
        assert (client, status) == ("Ask", "pending")
        s, r = wire(served, "POST", "/api/mcp/pending/approve", {"id": aid})        # Settings can't decide it
        assert s == 409 and "still running" in r["error"], (r, pending_rows(), cap_of("Other Shopping"))
        assert settings_pending(served) == []                                        # nor lists it while the turn runs
        s, r = wire(served, "POST", "/api/mcp/pending/decline", {"id": aid})
        assert s == 409, r
        s, r = wire(served, "POST", "/api/chat/decide", {"id": aid, "approve": True})   # nor by the panel
        assert s == 404, r
        s, got = wire(served, "GET", f"/api/chat/cards?ids={aid}")
        assert got == {"cards": []}
    finally:
        hold.set()
        t.join(30)
    assert t.result[0] == 502 and "answered with an error (500)" in t.result[1]["error"]
    assert pending_rows() == [(aid, "finance_set_cap", "Ask", "rejected")] and cap_of("Other Shopping") == 850.0
    assert result_of(aid) == {"withdrawn": "the Ask answer that suggested it failed"}
    s, r = wire(served, "POST", "/api/mcp/pending/approve", {"id": aid})              # a later Approve is refused
    assert s == 409 and "already rejected" in r["error"] and cap_of("Other Shopping") == 850.0


def test_a_card_is_published_only_when_its_turn_answers(app, llm, served):
    hold = threading.Event()
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})},
                  {"message": answer("Raising it to $900 would cover it. Approve the card if you want it."), "hold": hold}]
    t = Turn(served, "Raise Shopping?")
    t.start()
    try:
        assert llm.wait_requests(2, 20)
        [(aid, *_)] = pending_rows()
        assert settings_pending(served) == []
        s, r = wire(served, "POST", "/api/mcp/pending/approve", {"id": aid})
        assert s == 409 and cap_of("Other Shopping") == 850.0
    finally:
        hold.set()
        t.join(30)
    s, r = t.result
    assert s == 200 and [(c["id"], c["status"]) for c in r["cards"]] == [(aid, "pending")]
    assert settings_pending(served) == [(aid, "Ask", "ask")]                       # published with the answer
    s, d = wire(served, "POST", "/api/chat/decide", {"id": aid, "approve": True})
    assert s == 200 and d["card"]["status"] == "applied" and cap_of("Other Shopping") == 900.0


def test_a_failed_turn_withdraws_its_card_in_order_ordinary_path(app, llm):
    """The ordinary (sequential) failure: the card is withdrawn with the reason recorded, so Settings (and the
    history) tell a withdrawn card from one the owner declined."""
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 999})},
                  {"status": 500, "raw": b'{"error": {"message": "boom"}}'}]
    s, r = ask(app, "Raise it")
    assert s == 502 and "answered with an error (500)" in r["error"]
    [(aid, _, _, status)] = pending_rows()
    assert status == "rejected" and result_of(aid) == {"withdrawn": "the Ask answer that suggested it failed"}
    assert settings_pending(None, app) == []
    s, d = http(app, "POST", "/api/mcp/pending/approve", {"id": aid})
    assert s == 409 and cap_of("Other Shopping") == 850.0


def test_a_failed_withdrawal_keeps_the_turns_own_error_and_nothing_becomes_decidable(app, llm, monkeypatch):
    real = tooldefs.write_connection
    opened = []

    def flaky(path, **kw):   # the card's own record opens fine; the withdrawal after the failure hits a broken disk
        opened.append(path)
        if len(opened) > 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(path, **kw)

    monkeypatch.setattr(tooldefs, "write_connection", flaky)
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 999})},
                  {"status": 500, "raw": b'{"error": {"message": "boom"}}'}]
    s, r = ask(app, "Raise it")
    assert s == 502 and r == {"error": "The AI provider answered with an error (500). Try again in a minute."}
    assert len(opened) == 2
    monkeypatch.setattr(tooldefs, "write_connection", real)
    [(aid, _, _, status)] = pending_rows()
    assert status == "pending"                                   # not withdrawn (the disk said no), but held:
    assert settings_pending(None, app) == []
    s, r = http(app, "POST", "/api/mcp/pending/approve", {"id": aid})
    assert s == 409 and "still running" in r["error"]
    s, r = http(app, "POST", "/api/chat/decide", {"id": aid, "approve": True})
    assert s == 404 and cap_of("Other Shopping") == 850.0
    # once its turn can't be running any more, the next look at the cards withdraws it
    sql("UPDATE pending_actions SET created_at = '2026-01-01T00:00:00Z' WHERE id = ?", (aid,))
    s, got = http(app, "GET", f"/api/chat/cards?ids={aid}")
    assert s == 200 and pending_rows()[0][3] == "rejected"
    assert result_of(aid) == {"withdrawn": "its Ask answer never finished (the app stopped)"}
    s, r = http(app, "POST", "/api/mcp/pending/approve", {"id": aid})
    assert s == 409 and "already rejected" in r["error"] and cap_of("Other Shopping") == 850.0


def test_reconcile_leaves_a_running_turns_card_alone(app):
    conn = db.connect(config.db_path())
    try:
        a = mcp_state.record_pending(conn, "finance_set_cap", {"category": "Other Shopping", "monthly_cap": 1}, "x",
                                     "Ask", origin="ask", held_by="turn-1")
        assert mcp_state.reconcile(conn) == {"withdrawn": 0, "interrupted": 0}
        [row] = [mcp_state.pending(conn, a["id"])[0]]
        assert row["status"] == "pending" and row["held"] is True
        assert [x["id"] for x in mcp_state.release(conn, "turn-1")] == [a["id"]]
        assert mcp_state.pending(conn, a["id"])[0]["held"] is False and mcp_state.pending_count(conn) == 1
    finally:
        conn.close()


def test_a_held_card_outlasts_the_longest_turn():
    """Review N1, the C2 counterpart of AI_INFLIGHT_STALE > TIMEOUT (C3, below): reconcile() withdraws a held card older
    than HELD_STALE as abandoned, so HELD_STALE must be longer than any turn can run. Else a live turn's card would be
    withdrawn under it, release() would return nothing for it, and the panel would lose a card the model was told the
    owner sees."""
    assert mcp_state.HELD_STALE > longest_turn()


def test_a_card_as_old_as_the_longest_turn_is_still_its_turn_to_publish(app, llm, served):
    """N1 through the routes: the turn's card is made as old as a card of a live turn can be (recorded as the turn
    began, the turn at its longest), a look at the cards reconciles meanwhile, and the answer still publishes it."""
    hold = threading.Event()
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})},
                  {"message": answer("Raising it to $900 would cover it. Approve the card if you want it."), "hold": hold}]
    t = Turn(served, "Raise Shopping?")
    t.start()
    try:
        assert llm.wait_requests(2, 20)            # the card is recorded (held) and the turn's last request is out
        [(aid, *_)] = pending_rows()
        sql("UPDATE pending_actions SET created_at = ? WHERE id = ?", (utc_ago(longest_turn()), aid))
        s, _ = wire(served, "GET", f"/api/chat/cards?ids={aid}")                     # reconciles
        assert s == 200 and pending_rows()[0][3] == "pending", result_of(aid)
    finally:
        hold.set()
        t.join(30)
    s, r = t.result
    assert s == 200 and [(c["id"], c["status"]) for c in r["cards"]] == [(aid, "pending")], r
    assert settings_pending(served) == [(aid, "Ask", "ask")]


def test_a_failure_making_the_reply_leaves_no_card_released_that_the_owner_never_saw(app, llm, monkeypatch):
    """Review N2: the turn's cards are released in the same transaction that makes its reply's cards, so a failure
    there (after the answer came) leaves them held, and the turn's withdraw() still finds them: none is left listed and
    decidable in Settings while the turn answered 500 without it."""
    def broken(action):
        raise RuntimeError("no card")

    monkeypatch.setattr(chat, "card", broken)
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})},
                  {"message": answer("Raising it to $900 would cover it. Approve the card if you want it.")}]
    s, r = ask(app, "Raise Shopping?")
    assert s == 500 and r == {"error": "internal error (RuntimeError)"}
    [(aid, _, _, status)] = pending_rows()
    assert (status, result_of(aid)) == ("rejected", {"withdrawn": "the Ask answer that suggested it failed"})
    assert settings_pending(None, app) == []
    s, d = http(app, "POST", "/api/mcp/pending/approve", {"id": aid})
    assert s == 409 and "already rejected" in d["error"] and cap_of("Other Shopping") == 850.0


def stale_rows(app, llm) -> tuple[int, int]:
    """What a stopped app leaves: (an Approve claimed long ago and never finished, a held card whose turn is gone)."""
    stuck = ask_card(app, llm)["id"]
    sql("UPDATE pending_actions SET status = 'approved', decided_at = '2026-01-01T00:00:00Z' WHERE id = ?", (stuck,))
    conn = db.connect(config.db_path())
    try:
        held = mcp_state.record_pending(conn, "finance_set_cap", {"category": "Other Health", "monthly_cap": 1}, "x",
                                        "Ask", origin="ask", held_by="turn-gone")["id"]
    finally:
        conn.close()
    sql("UPDATE pending_actions SET created_at = '2026-01-01T00:00:00Z' WHERE id = ?", (held,))
    return stuck, held


def test_opening_settings_settles_what_a_stopped_app_left(app, llm):
    """Review N11: the Settings page reconciles itself, so an Approve the app never finished and a card whose turn can't
    be running reach their final state when the owner merely opens Settings, not only once an Ask route or a decide runs
    (with Ask off, every Ask route answers 404 before it would)."""
    stuck, held = stale_rows(app, llm)
    save_prefs({"ai.mode": "off"})                            # a Settings-only owner: no Ask route runs any more
    assert settings_pending(None, app) == []
    assert [(r[0], r[3]) for r in pending_rows()] == [(stuck, "failed"), (held, "rejected")]
    assert result_of(stuck) == {"error": "the app stopped", "uncertain": True}
    assert result_of(held) == {"withdrawn": "its Ask answer never finished (the app stopped)"}


def test_opening_settings_never_waits_on_another_writer_when_nothing_is_stale(app):
    """N11's reconcile() on the Settings page writes only when there's a row to settle: while another writer (a sync,
    say) holds the database, the page answers at once, as it did before (a plain reconcile there waited as long as the
    lock was held)."""
    holder = sqlite3.connect(config.db_path(), timeout=1)
    holder.execute("BEGIN IMMEDIATE")
    got: dict = {}
    look = threading.Thread(target=lambda: got.update(status=http(app, "GET", "/api/settings/page")[0]), daemon=True)
    try:
        look.start()
        look.join(10)
        assert got == {"status": 200}
    finally:
        holder.rollback()
        holder.close()
        look.join(60)


def test_finance_mcp_status_stays_read_only_and_shows_nothing_reconcile_would_settle(app, llm, capsys):
    """N11, the CLI half, left read-only on purpose: `finance mcp status` opens the database read-only (it can't
    reconcile), and lists only pending rows no turn holds, so neither a stale held card nor an unfinished Approve is
    ever shown as waiting, before reconcile() or after it."""
    stuck, held = stale_rows(app, llm)
    before = pending_rows()
    code, out, _ = run_cli(["mcp", "status", "--json"], capsys)
    assert code == 0 and pending_rows() == before                                   # nothing written
    shown = json.loads(out)
    assert shown["pending"] == 0 and shown["pending_actions"] == []
    conn = db.connect(config.db_path())
    try:
        assert mcp_state.reconcile(conn) == {"withdrawn": 1, "interrupted": 1}
    finally:
        conn.close()
    assert json.loads(run_cli(["mcp", "status", "--json"], capsys)[1]) == shown


# --- C3: the monthly cap -----------------------------------------------------------------------------------------------

def test_a_second_turn_waits_for_the_first_request_then_meets_the_cap(app, llm, served):
    save_prefs({"ai.monthly_cap": 0.05})
    hold = threading.Event()
    llm.script = [{"message": answer("First."), "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.06},
                   "hold": hold},
                  {"message": answer("Second."), "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.06}}]
    first, second = Turn(served, "Hi"), Turn(served, "Again")
    first.start()
    try:
        assert llm.wait_requests(1, 20)                        # the first request is out, its answer held open
        second.start()
        overlapped = llm.wait_requests(2, 1.5)                 # the second turn's request, while the first is open?
    finally:
        hold.set()
        first.join(30)
        second.join(30)
    assert not overlapped and llm.peak == 1 and len(llm.requests) == 1
    assert first.result[0] == 200 and first.result[1]["reply"] == "First."
    assert second.result[0] == 429 and "spend cap is reached ($0.06 of $0.05)" in second.result[1]["error"]
    conn = db.connect(config.db_path())
    assert usage.ai_spent(conn, TODAY) == 0.06                 # over by at most the one request that was running
    conn.close()


def test_turns_under_the_cap_are_answered_one_request_at_a_time(app, llm, served):
    hold = threading.Event()
    llm.script = [{"message": answer("First."), "hold": hold}, {"message": answer("Second.")}]
    first, second = Turn(served, "Hi"), Turn(served, "Again")
    first.start()
    try:
        assert llm.wait_requests(1, 20)
        second.start()
        overlapped = llm.wait_requests(2, 1.0)
    finally:
        hold.set()
        first.join(30)
        second.join(30)
    assert not overlapped and llm.peak == 1
    assert (first.result[0], first.result[1]["reply"]) == (200, "First.")
    assert (second.result[0], second.result[1]["reply"]) == (200, "Second.")
    assert rows(config.db_path(), "SELECT outcome FROM api_calls WHERE provider = 'ai' ORDER BY id") == [("ok",), ("ok",)]


@pytest.mark.parametrize("preset,field,other", [("custom", "max_tokens", "max_completion_tokens"),
                                                ("openrouter", "max_tokens", "max_completion_tokens"),
                                                ("local", "max_tokens", "max_completion_tokens"),
                                                ("openai", "max_completion_tokens", "max_tokens")])
def test_every_request_bounds_the_models_output(app, llm, preset, field, other):
    save_prefs({"ai.preset": preset, "ai.base_url": llm.url})
    llm.script = [{"message": call_msg("finance_status", {})}, {"message": answer("Ok.")}]
    s, r = ask(app, "Hi")
    assert s == 200, r
    assert [(q[field], other in q) for q in llm.requests] == [(chat.MAX_OUTPUT_TOKENS, False)] * 2
    assert 0 < chat.MAX_OUTPUT_TOKENS <= 2048


def test_a_request_the_app_never_settled_stops_blocking_after_a_while(app, llm, monkeypatch):
    stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))   # a day old: long past settling
    sql("INSERT INTO api_calls (provider, account_id, trigger, at, day, note) VALUES ('ai', NULL, 'refresh', ?, ?, 'usd=0')",
        (stale, TODAY.isoformat()))
    llm.script = [{"message": answer("Ok.")}]
    s, r = ask(app, "Hi")
    assert s == 200, r
    assert rows(config.db_path(), "SELECT outcome FROM api_calls WHERE provider = 'ai' ORDER BY id") == [
        ("interrupted",), ("ok",)]
    # one still running (a fresh unsettled request) makes a new turn wait, and past its deadline say so
    sql("INSERT INTO api_calls (provider, account_id, trigger, at, day, note) VALUES ('ai', NULL, 'refresh', ?, ?, 'usd=0')",
        (db.now(), TODAY.isoformat()))
    monkeypatch.setattr(chat, "TIMEOUT", 1.0)
    s, r = ask(app, "Again")
    assert s == 429 and r == {"error": "Ask is still answering another question. Try again in a moment."}
    assert len(llm.requests) == 1


def test_a_timed_out_request_is_settled_at_its_worst_case(app, llm, monkeypatch):
    monkeypatch.setattr(chat, "TIMEOUT", 1.0)
    llm.script = [{"message": answer("late"), "delay": 2.5}]
    s, r = ask(app, "Hi")
    assert s == 504 and "took too long" in r["error"]
    [(outcome, note)] = rows(config.db_path(), "SELECT outcome, note FROM api_calls WHERE provider = 'ai'")
    assert (outcome, note.startswith("usd=0.000000")) == ("timeout", False), note
    sent = llm.requests[0]
    worst = len(json.dumps(sent)) // 3 * chat.EST_IN / 1e6 + chat.MAX_OUTPUT_TOKENS * chat.EST_OUT / 1e6
    assert note.endswith(f"out={chat.MAX_OUTPUT_TOKENS} estimated") and usage.AI_INFLIGHT_STALE > chat.TIMEOUT
    assert note.startswith(f"usd={worst:.6f} ") and worst > 0


def test_settings_calls_the_cap_what_it_is():
    js = (STATIC / "settings.js").read_text()
    assert "pause at the cap" not in js
    assert "A soft cap" in js and "one request at a time" in js


# --- C5 (server): an Approve always reaches a final state ---------------------------------------------------------------

def ask_card(app, llm, category="Other Shopping", new=900.0) -> dict:
    llm.script = [{"message": call_msg("finance_set_cap", {"category": category, "monthly_cap": new})},
                  {"message": answer("Approve the card if you want it.")}]
    s, r = ask(app, f"Raise {category}?")
    assert s == 200, r
    [c] = r["cards"]
    return c


def test_an_unexpected_error_while_applying_is_final_and_says_it_may_be_partly_done(app, llm, monkeypatch):
    c = ask_card(app, llm)

    def boom(name, args, path, day):
        raise RuntimeError("disk gone at /secret/path")

    monkeypatch.setattr(tooldefs, "apply", boom)
    s, d = http(app, "POST", "/api/chat/decide", {"id": c["id"], "approve": True})
    assert s == 200 and d["card"]["status"] == "failed", d
    assert d["card"]["result_line"] == ("Interrupted while applying it (RuntimeError). It may be partly done: "
                                        "check before asking again")
    assert "/secret/path" not in json.dumps(d)
    assert result_of(c["id"]) == {"error": "RuntimeError", "uncertain": True}
    s, d = http(app, "POST", "/api/chat/decide", {"id": c["id"], "approve": True})    # final: never applied again
    assert s == 409 and d["card"]["status"] == "failed"


def test_an_approve_the_app_never_finished_is_settled_not_reopened(app, llm):
    c = ask_card(app, llm)
    fresh = ask_card(app, llm, "Other Health", 300.0)
    sql("UPDATE pending_actions SET status = 'approved', decided_at = '2026-01-01T00:00:00Z' WHERE id = ?", (c["id"],))
    sql("UPDATE pending_actions SET status = 'approved', decided_at = ? WHERE id = ?", (db.now(), fresh["id"]))
    s, got = http(app, "GET", f"/api/chat/cards?ids={c['id']},{fresh['id']}")
    assert s == 200
    assert [(x["id"], x["status"], x["result_line"]) for x in got["cards"]] == [
        (c["id"], "failed", "Interrupted while applying it (the app stopped). It may be partly done: check before asking again"),
        (fresh["id"], "approved", "Approved, applying it…")]               # one being applied now is left alone
    assert result_of(c["id"]) == {"error": "the app stopped", "uncertain": True}
    for path, body in (("/api/chat/decide", {"id": c["id"], "approve": True}), ("/api/mcp/pending/approve", {"id": c["id"]})):
        s, _ = http(app, "POST", path, body)
        assert s == 409
    assert cap_of("Other Shopping") == 850.0 and "pending" not in [r[3] for r in pending_rows()]
