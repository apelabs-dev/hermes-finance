"""TASK-23: the Ask panel's server side (chat.py, /api/chat*), against a fake OpenAI-compatible server started here on a
loopback port (no real LLM, no Plaid, nothing outside 127.0.0.1).

The app owns the tool loop: a read is answered with a source line ("from budget-status · Sep 2026"), and what the model
got back is exactly tooldefs.render (scrubbed, as MCP and the Hermes plugin send it); a write becomes one pending action
(client "Ask", the table Settings reads) that is applied exactly once on Approve, never on Deny or a second Approve, and a
card decided in Settings shows its final state; the monthly spend cap pauses Ask before any request is sent; an endpoint
that's down, refuses the key or times out is one friendly line, with a card recorded mid-turn declined; Off is 404
everywhere; with the built-in login every POST needs the CSRF token. Hermes mode (the plugin's completion bridge) sends
no tools and answers from reads the app ran itself."""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_tools import TODAY, build, rows
from hermes_finance import auth, config, db, mcp_state, prefs, redact, secretstore, tooldefs
from hermes_finance.web import App

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
KEY = "sk-or-test-0123456789abcdef"
MODEL = "anthropic/claude-haiku-4.5"


class FakeLLM:
    """An OpenAI-compatible /chat/completions: each request is recorded and answered by the next scripted reply (a dict:
    the message, plus optional usage / status / delay)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.script: list[dict] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                fake.headers.append(dict(self.headers.items()))
                step = fake.script.pop(0) if fake.script else {"message": {"role": "assistant", "content": "(no script)"}}
                if step.get("delay"):
                    time.sleep(step["delay"])
                status = step.get("status", 200)
                out = step.get("raw") or json.dumps({"id": "x", "object": "chat.completion", "model": body.get("model"),
                                                     "choices": [{"index": 0, "message": step.get("message"),
                                                                  "finish_reason": "stop"}],
                                                     **({"usage": step["usage"]} if "usage" in step else {})}).encode()
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
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

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


@pytest.fixture
def llm():
    f = FakeLLM()
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


def http(app, method, path, body=None, headers=JSONH):
    status, ctype, out, _ = app.respond(method, path, headers if method != "GET" else LOCAL,
                                        json.dumps(body).encode() if body is not None else b"")
    return status, (json.loads(out) if ctype.startswith("application/json") else out.decode())


def ask(app, text, history=()):
    return http(app, "POST", "/api/chat", {"messages": [*history, {"role": "user", "content": text}]})


def cap_of(category: str) -> float | None:
    return rows(config.db_path(), "SELECT monthly_cap FROM categories WHERE name = ?", (category,))[0][0]


def pending_rows() -> list[tuple]:
    return rows(config.db_path(), "SELECT id, tool, client, status FROM pending_actions ORDER BY id")


# --- reads ---------------------------------------------------------------------------------------------------------

def test_a_read_is_answered_with_its_source_line_and_the_model_gets_the_tool_json(app, llm):
    llm.script = [{"message": call_msg("finance_budget_status", {"month": "2026-09"}), "usage": {"prompt_tokens": 900, "completion_tokens": 20}},
                  {"message": answer("You've spent $311.61 on Other Shopping in September. The cap is $850."),
                   "usage": {"prompt_tokens": 2400, "completion_tokens": 30}}]
    s, r = ask(app, "Why am I over on Shopping?")
    assert s == 200, r
    assert r["reply"].startswith("You've spent $311.61 on Other Shopping")
    assert r["sources"] == ["from budget-status · Sep 2026"]
    assert r["cards"] == [] and r["brain"] == "via Custom · claude-haiku-4.5"
    assert [x["category"] for x in r["stats"]] == ["Other Shopping"] and r["stats"][0]["cap"] == 850.0
    # the brain got every finance_* tool's schema, the key as a bearer, and the read's JSON exactly as MCP sends it
    first, second = llm.requests
    assert [t["function"]["name"] for t in first["tools"]] == list(tooldefs.TOOLS)
    assert first["tools"][0]["function"]["parameters"] == tooldefs.TOOLS["finance_spend"].schema()
    assert first["model"] == MODEL and llm.headers[0]["Authorization"] == f"Bearer {KEY}"
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    want = tooldefs.render(tooldefs.run_read("finance_budget_status", {"month": "2026-09"}, config.db_path(), TODAY))
    assert tool_msg["content"] == want
    assert pending_rows() == []                      # a read records nothing


def test_tool_results_sent_to_the_model_are_scrubbed_like_mcp(app, llm):
    token = "access-production-deadbeef-1111-2222-3333-444444444444"
    tid = rows(config.db_path(), "SELECT transaction_id FROM transactions ORDER BY date DESC LIMIT 1")[0][0]
    conn = db.connect(config.db_path())
    with conn:
        conn.execute("UPDATE transactions SET name = ? WHERE transaction_id = ?", (f"LEAK {token}", tid))
    conn.close()
    llm.script = [{"message": call_msg("finance_transactions", {"search": "LEAK"})}, {"message": answer("One row.")}]
    s, r = ask(app, "Anything odd?")
    assert s == 200, r
    sent = llm.requests[1]["messages"][-1]["content"]
    assert token not in sent and "access-production-[REDACTED]" in sent
    assert r["sources"] == ["from txns · all time"]


def test_a_bad_tool_call_is_an_error_the_model_sees_not_a_crash(app, llm):
    llm.script = [{"message": call_msg("finance_spend", {"category": "Nope"})},
                  {"message": call_msg("finance_nothing", {}, "c2")}, {"message": answer("I couldn't find that category.")}]
    s, r = ask(app, "Spend on Nope?")
    assert s == 200 and r["reply"] == "I couldn't find that category." and r["sources"] == []
    assert "unknown category" in llm.requests[1]["messages"][-1]["content"]
    assert "unknown tool" in llm.requests[2]["messages"][-1]["content"]


# --- writes: approval cards ----------------------------------------------------------------------------------------

def test_a_write_becomes_one_pending_action_applied_exactly_once_on_approve(app, llm):
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})},
                  {"message": answer("Raising the Shopping cap to $900 would cover it. Approve the card if you want it.")}]
    s, r = ask(app, "Raise my Shopping cap?")
    assert s == 200, r
    [c] = r["cards"]
    assert c["title"] == "Raise Other Shopping cap to $900?" and c["status"] == "pending" and c["result_line"] is None
    assert c["summary"] == "Set the monthly budget cap on Other Shopping to $900 (now $850)."
    assert cap_of("Other Shopping") == 850.0                                   # nothing applied yet
    assert pending_rows() == [(c["id"], "finance_set_cap", "Ask", "pending")]
    # the model was told it's only pending
    told = json.loads(llm.requests[1]["messages"][-1]["content"])
    assert told["status"] == "pending_approval" and told["applied"] is False
    # Settings lists it (the same table), labelled Ask
    s, page = http(app, "GET", "/api/settings/page")
    assert [(a["id"], a["client"]) for a in page["mcp"]["pending_actions"]] == [(c["id"], "Ask")]
    # Approve: applied once
    s, d = http(app, "POST", "/api/chat/decide", {"id": c["id"], "approve": True})
    assert s == 200 and d["card"]["status"] == "applied"
    assert d["card"]["result_line"] == "Saved: Other Shopping cap is now $900"
    assert cap_of("Other Shopping") == 900.0
    # a second Approve (here or in Settings) changes nothing: 409 with the final state
    conn = db.connect(config.db_path())
    with conn:
        conn.execute("UPDATE categories SET monthly_cap = 850 WHERE name = 'Other Shopping'")  # would show a second apply
    conn.close()
    s, d = http(app, "POST", "/api/chat/decide", {"id": c["id"], "approve": True})
    assert s == 409 and d["card"]["status"] == "applied" and "already applied" in d["error"]
    s, d = http(app, "POST", "/api/mcp/pending/approve", {"id": c["id"]})
    assert s == 409
    assert cap_of("Other Shopping") == 850.0
    assert pending_rows() == [(c["id"], "finance_set_cap", "Ask", "applied")]


def test_deny_changes_nothing_and_a_card_decided_in_settings_shows_its_final_state(app, llm):
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 250})},
                  {"message": answer("Want a lower cap?")},
                  {"message": call_msg("finance_set_cap", {"category": "Other Health", "monthly_cap": 300}, "c2")},
                  {"message": answer("Or raise Health?")}]
    _, r1 = ask(app, "Lower Shopping?")
    _, r2 = ask(app, "Health?")
    a, b = r1["cards"][0], r2["cards"][0]
    assert a["title"] == "Lower Other Shopping cap to $250?" and b["title"] == "Raise Other Health cap to $300?"
    s, d = http(app, "POST", "/api/chat/decide", {"id": a["id"], "approve": False})
    assert s == 200 and d["card"]["status"] == "rejected" and d["card"]["result_line"] == "Declined, nothing changed"
    s, d = http(app, "POST", "/api/chat/decide", {"id": a["id"], "approve": True})     # deny is final
    assert s == 409 and d["card"]["status"] == "rejected"
    assert cap_of("Other Shopping") == 850.0
    # the other card is approved in Settings; the panel, drawing its cards again, gets the final state
    s, _ = http(app, "POST", "/api/mcp/pending/approve", {"id": b["id"]})
    assert s == 200 and cap_of("Other Health") == 300.0
    s, got = http(app, "GET", f"/api/chat/cards?ids={a['id']},{b['id']},999")
    assert s == 200
    assert [(c["id"], c["status"], c["result_line"]) for c in got["cards"]] == [
        (a["id"], "rejected", "Declined, nothing changed"), (b["id"], "applied", "Saved: Other Health cap is now $300")]
    s, d = http(app, "POST", "/api/chat/decide", {"id": b["id"], "approve": True})
    assert s == 409 and cap_of("Other Health") == 300.0


def test_a_refused_write_records_no_card(app, llm):
    llm.script = [{"message": call_msg("finance_add_rule", {"pattern": "zzz-no-such-merchant", "category": "Other Shopping"})},
                  {"message": answer("That pattern matches nothing.")}]
    s, r = ask(app, "Rule for zzz?")
    assert s == 200 and r["cards"] == [] and pending_rows() == []
    assert "matches no transaction" in llm.requests[1]["messages"][-1]["content"]


# --- the spend cap -------------------------------------------------------------------------------------------------

def test_the_monthly_spend_cap_pauses_ask_before_any_request(app, llm):
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.monthly_cap": 0.05})
    conn.close()
    llm.script = [{"message": answer("First."), "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.06}}]
    s, r = ask(app, "Hi")
    assert s == 200 and r["reply"] == "First."
    s, st = http(app, "GET", "/api/chat")
    assert st["spent"] == 0.06 and st["cap"] == 0.05 and st["paused"] is True
    s, r = ask(app, "Again")
    assert s == 429 and "spend cap is reached ($0.06 of $0.05)" in r["error"] and "Traceback" not in r["error"]
    assert len(llm.requests) == 1                                            # the capped turn sent nothing
    assert rows(config.db_path(), "SELECT calls FROM api_usage WHERE provider = 'ai'") == [(1,)]
    s, page = http(app, "GET", "/api/settings/page")
    assert page["ai"]["spent"] == 0.06


def test_without_a_reported_cost_the_spend_is_estimated_from_tokens(app, llm):
    llm.script = [{"message": answer("Ok."), "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}}]
    s, _ = ask(app, "Hi")
    assert s == 200
    note = rows(config.db_path(), "SELECT note FROM api_calls WHERE provider = 'ai'")[0][0]
    assert note == "usd=4.500000 in=1000000 out=100000 estimated"          # 1M x $3 + 0.1M x $15
    s, st = http(app, "GET", "/api/chat")
    assert st["spent"] == 4.5 and st["paused"] is False


# --- failures: one friendly line, nothing half-applied ----------------------------------------------------------------

def test_endpoint_down_is_a_friendly_line(app, llm):
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.base_url": "http://127.0.0.1:9/v1"})
    conn.close()
    s, r = ask(app, "Hi")
    assert s == 502 and r == {"error": "Couldn't reach the AI provider. Is it running, and is the address in Settings right?"}


@pytest.mark.parametrize("status,words", [(401, "refused the key (401)"), (404, "doesn't know that model"),
                                          (500, "answered with an error (500)"), (429, "too many requests")])
def test_an_http_error_is_a_friendly_line(app, llm, status, words):
    llm.script = [{"status": status, "raw": b'{"error": {"message": "secret internals"}}'}]
    s, r = ask(app, "Hi")
    assert s in (502, 429) and words in r["error"] and "secret internals" not in r["error"]


def test_a_garbled_answer_is_a_friendly_line(app, llm):
    llm.script = [{"raw": b"<html>oops</html>"}]
    s, r = ask(app, "Hi")
    assert s == 502 and r["error"] == "The AI provider sent an answer the app couldn't read."


def test_a_timeout_is_a_friendly_line_and_declines_a_card_it_recorded(app, llm, monkeypatch):
    monkeypatch.setattr("hermes_finance.chat.TIMEOUT", 1.0)
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 999})},
                  {"message": answer("late"), "delay": 2.5}]
    s, r = ask(app, "Raise it")
    assert s == 504 and "took too long" in r["error"]
    assert [x[3] for x in pending_rows()] == ["rejected"]                  # nothing left waiting from a failed turn
    assert cap_of("Other Shopping") == 850.0


def test_the_loop_is_capped_at_six_rounds(app, llm):
    llm.script = [{"message": call_msg("finance_status", {}, f"c{i}")} for i in range(8)]
    s, r = ask(app, "Loop")
    assert s == 502 and "more than 6 look-ups" in r["error"] and len(llm.requests) == 6


def test_no_key_or_model_says_what_to_set(app, llm):
    secretstore.write({secretstore.AI_KEY: None})
    s, r = ask(app, "Hi")
    assert s == 400 and r["error"] == "Add your API key in Settings \u2192 AI provider first."
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.model": ""})
    conn.close()
    s, r = ask(app, "Hi")
    assert s == 400 and "Pick a model" in r["error"] and llm.requests == []


@pytest.mark.parametrize("body", [{}, {"messages": []}, {"messages": [{"role": "system", "content": "x"}]},
                                  {"messages": [{"role": "user", "content": "x" * 4001}]},
                                  {"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]}])
def test_a_bad_history_is_refused(app, llm, body):
    s, r = http(app, "POST", "/api/chat", body)
    assert s == 400 and r["field"] == "messages" and llm.requests == []


# --- Off, the page, CSRF ---------------------------------------------------------------------------------------------

def test_off_means_no_ask_at_all(app, llm):
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.mode": "off"})
    conn.close()
    for method, path, body in (("GET", "/api/chat", None), ("POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}]}),
                               ("GET", "/api/chat/cards?ids=1", None), ("POST", "/api/chat/decide", {"id": 1, "approve": True})):
        s, r = http(app, method, path, body)
        assert s == 404 and "Ask is off" in r["error"], (method, path)
    s, html = http(app, "GET", "/")
    assert "data-ask" not in html and "/static/chat.js" in html     # the script is there; with no flag it draws nothing
    assert llm.requests == []


def test_the_page_carries_the_brain_for_the_header_chip(app):
    s, html = http(app, "GET", "/")
    assert 'data-ask="api" data-ask-brain="via Custom · claude-haiku-4.5"' in html
    assert '<link rel="stylesheet" href="/static/chat.css">' in html and '<script src="/static/chat.js" defer></script>' in html
    assert all(f'data-icon="{n}"' in html for n in ("chat", "send", "cpu"))
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.mode": "hermes"})
    conn.close()
    s, html = http(app, "GET", "/")
    assert 'data-ask="hermes" data-ask-brain="via Hermes"' in html
    for name in ("chat.js", "chat.css"):
        s, _, body, _ = app.respond("GET", f"/static/{name}", LOCAL, b"")
        assert s == 200 and len(body) > 1000


def test_every_chat_post_needs_the_csrf_token_with_the_built_in_login(app, llm, monkeypatch):
    from argon2 import PasswordHasher
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    monkeypatch.setattr(auth, "_dummy_hash", None)
    auth._tickets.clear()
    auth._enrollments.clear()
    pw = "correct horse battery staple"
    s, _, r, _ = app.respond("POST", "/api/auth/enroll/start", JSONH, json.dumps({"username": "owner", "password": pw, "confirm": pw}).encode())
    r = json.loads(r)
    s, _, v, _ = app.respond("POST", "/api/auth/enroll/verify", JSONH,
                             json.dumps({"enroll_id": r["enroll_id"], "code": auth.totp(r["key"].replace(" ", ""))}).encode())
    s, _, f, extra = app.respond("POST", "/api/auth/enroll/finish", JSONH, json.dumps({"enroll_id": r["enroll_id"], "saved": True}).encode())
    assert s == 200, f
    cookie = {"Cookie": f"{auth.COOKIE}={dict(extra)['Set-Cookie'].split(';')[0].split('=', 1)[1]}"}
    headers = {**JSONH, **cookie}
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900})}, {"message": answer("ok")}]
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert http(app, "POST", "/api/chat", body, headers)[0] == 403
    assert http(app, "POST", "/api/chat", body, {**headers, "X-CSRF-Token": "nope"})[0] == 403
    assert llm.requests == []
    _, _, html, _ = app.respond("GET", "/", {**LOCAL, **cookie}, b"")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', html.decode()).group(1)
    s, r = http(app, "POST", "/api/chat", body, {**headers, "X-CSRF-Token": token})
    assert s == 200 and len(r["cards"]) == 1
    cid = r["cards"][0]["id"]
    assert http(app, "POST", "/api/chat/decide", {"id": cid, "approve": True}, headers)[0] == 403
    assert cap_of("Other Shopping") == 850.0
    assert http(app, "POST", "/api/chat/decide", {"id": cid, "approve": True},
                {**headers, "X-CSRF-Token": token, "Origin": "https://evil.example"})[0] == 403
    s, d = http(app, "POST", "/api/chat/decide", {"id": cid, "approve": True}, {**headers, "X-CSRF-Token": token})
    assert s == 200 and cap_of("Other Shopping") == 900.0
    assert app.respond("GET", "/api/chat", {"Host": "127.0.0.1:8765"}, b"")[0] == 401   # no session: sign in first


def test_a_foreign_origin_is_refused_without_a_login(app, llm):
    s, r = http(app, "POST", "/api/chat", {"messages": [{"role": "user", "content": "hi"}]},
                {**JSONH, "Origin": "https://evil.example"})
    assert s == 403 and llm.requests == []


# --- Hermes mode (through the plugin's completion bridge: bridge.py; the child here is tests/bridge_stub.py's
# stand-in, not Hermes; tests/test_bridge.py and tests/test_bridge_runtime.py cover the bridge itself) ---------------

def hermes_mode(tmp_path, monkeypatch):
    import bridge_stub
    stub = bridge_stub.StubHermes(tmp_path / "stub", monkeypatch)
    conn = db.connect(config.db_path())
    prefs.save(conn, {"ai.mode": "hermes"})
    conn.close()
    return stub


def test_hermes_mode_sends_no_tools_and_answers_from_the_apps_own_reads(app, llm, tmp_path, monkeypatch):
    import bridge_stub as bs
    stub = hermes_mode(tmp_path, monkeypatch)
    stub.script(bs.read("finance_budget_status"), bs.answer("Shopping is at $311.61 of its $850 cap."))
    s, r = ask(app, "Why am I over on Shopping?")
    assert s == 200, r
    assert r["brain"] == "via Hermes" and r["cards"] == []
    assert r["sources"] == ["from budget-status · Sep 2026"]          # the read the app ran, not one the model named
    first, second = stub.requests()
    assert "tools" not in first and sorted(first) == ["input", "instructions", "max_tokens", "protocol", "schema",
                                                      "schema_name", "timeout"]
    assert tooldefs.render(tooldefs.run_read("finance_budget_status", {}, config.db_path(), TODAY)) in second["input"]
    assert "Why am I over on Shopping?" in first["input"]
    assert llm.requests == []                                         # nothing went to the API key's endpoint
    s, st = http(app, "GET", "/api/chat")
    assert st == {"mode": "hermes", "enabled": True, "label": "via Hermes", "cap": 5.0, "spent": st["spent"], "paused": False}
    assert rows(config.db_path(), "SELECT COUNT(*) FROM api_calls WHERE provider = 'ai'") == [(2,)]   # capped, counted


def test_hermes_down_is_a_friendly_line(app, tmp_path, monkeypatch):
    stub = hermes_mode(tmp_path, monkeypatch)
    stub.script({"unavailable": True})                                # Hermes without the plugin's bridge command
    s, r = ask(app, "Hi")
    assert s == 503 and r["error"].startswith("Hermes doesn't have the hermes-finance plugin's Ask bridge")
    monkeypatch.setenv("FINANCE_HERMES_BIN", str(tmp_path / "no-hermes"))   # no Hermes at all
    s, r = ask(app, "Hi")
    assert s == 503 and r["error"].startswith("Ask can't find Hermes on this computer")


def test_hermes_mode_needs_no_api_server_key_and_sends_none(app, llm, tmp_path, monkeypatch, data_dir):
    """The bridge borrows Hermes's own auth: without API_SERVER_KEY a turn still works, and with one in the env file
    the app neither reads nor sends it (the child's environment and request carry no key)."""
    import bridge_stub as bs
    stub = hermes_mode(tmp_path, monkeypatch)
    stub.script(bs.answer("Hello."), bs.answer("Hello again."))
    assert ask(app, "Hi") == (200, {"reply": "Hello.", "sources": [], "stats": [], "brain": "via Hermes", "cards": []})
    env = config.env_file_path()
    env.write_text(env.read_text() + "API_SERVER_KEY=hermes-api-key-for-tests-000000\n")
    assert ask(app, "Hi")[0] == 200
    assert "hermes-api-key-for-tests" not in json.dumps(stub.records()) and llm.requests == []


def test_hermes_refusing_the_key_names_hermes_not_settings(app, tmp_path, monkeypatch):
    import bridge_stub as bs
    stub = hermes_mode(tmp_path, monkeypatch)
    stub.script(bs.error("auth_refused"))
    s, r = ask(app, "Hi")
    assert s == 502 and r["error"] == "Hermes's model provider refused its key. Check Hermes's own setup (hermes model)."
    assert "Settings" not in r["error"]


def test_the_ai_key_never_reaches_an_answer(app, llm):
    llm.script = [{"message": answer(f"your key is {KEY}")}]
    s, r = ask(app, "What's my key?")
    assert s == 200 and KEY not in json.dumps(r) and redact.MASK in r["reply"]


# --- approvals under real HTTP: concurrency, stale replays, every route's gate ------------------------------------------
# (the app behind web.make_server, as `finance serve` runs it: a ThreadingHTTPServer, one thread per request)

@pytest.fixture
def served(app):
    from hermes_finance.web import make_server
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def wire(port, method, path, body=None, headers=None, raw=None):
    """One real HTTP request to the served app: (status, JSON answer)."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        h = {"Host": f"127.0.0.1:{port}", **({"Content-Type": "application/json"} if method != "GET" else {}), **(headers or {})}
        c.request(method, path, raw if raw is not None else (json.dumps(body) if body is not None else None), h)
        r = c.getresponse()
        out = r.read()
        return r.status, (json.loads(out) if out else {})
    finally:
        c.close()


class Applies:
    """tooldefs.apply (what both Approve paths call), counted, and slow enough that concurrent Approves overlap it."""

    def __init__(self, monkeypatch, delay: float = 0.3):
        self.calls: list[tuple[str, dict]] = []
        real = tooldefs.apply

        def spy(name, args, path, day):
            self.calls.append((name, args))
            time.sleep(delay)
            return real(name, args, path, day)

        monkeypatch.setattr(tooldefs, "apply", spy)


def card_over(port, llm, category="Other Shopping", new=900.0, headers=None):
    llm.script = [{"message": call_msg("finance_set_cap", {"category": category, "monthly_cap": new})},
                  {"message": answer("Approve the card if you want it.")}]
    s, r = wire(port, "POST", "/api/chat", {"messages": [{"role": "user", "content": f"Raise {category}?"}]}, headers)
    assert s == 200, r
    [c] = r["cards"]
    return c


def race(port, jobs):
    """Every (path, body) at the same moment, each in its own thread: [(path, status, answer)] in job order."""
    go = threading.Barrier(len(jobs))
    out: list = [None] * len(jobs)

    def one(i, path, body):
        go.wait()
        out[i] = (path, *wire(port, "POST", path, body))

    ts = [threading.Thread(target=one, args=(i, *j)) for i, j in enumerate(jobs)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert all(o is not None for o in out), out
    return out


def test_concurrent_approves_in_chat_and_settings_apply_exactly_once(app, llm, monkeypatch, served):
    c = card_over(served, llm)
    spy = Applies(monkeypatch)
    out = race(served, [("/api/chat/decide", {"id": c["id"], "approve": True})] * 5
               + [("/api/mcp/pending/approve", {"id": c["id"]})] * 3)
    assert sorted(s for _, s, _ in out) == [200] + [409] * 7, out
    assert spy.calls == [("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 900.0})]
    assert cap_of("Other Shopping") == 900.0
    assert pending_rows() == [(c["id"], "finance_set_cap", "Ask", "applied")]
    # a losing Approve in the panel gets the card's real state: approved while the winner applies it, then applied
    for path, s, r in out:
        if path == "/api/chat/decide" and s == 409:
            assert r["card"]["status"] in ("approved", "applied") and not r["card"]["status"] == "pending", r
            if r["card"]["status"] == "approved":
                assert r["card"]["result_line"] == "Approved, applying it\u2026"
    s, got = wire(served, "GET", f"/api/chat/cards?ids={c['id']}")
    assert s == 200 and got["cards"][0]["result_line"] == "Saved: Other Shopping cap is now $900"


def test_an_approve_and_a_deny_racing_leave_exactly_one_answer(app, llm, monkeypatch, served):
    spy = Applies(monkeypatch, delay=0.05)
    seen = set()
    for i in range(6):
        new = 900.0 + i
        before = cap_of("Other Shopping")
        c = card_over(served, llm, new=new)
        applied_before = len(spy.calls)
        out = race(served, [("/api/chat/decide", {"id": c["id"], "approve": True}),
                            ("/api/chat/decide", {"id": c["id"], "approve": False}),
                            ("/api/mcp/pending/approve", {"id": c["id"]}), ("/api/mcp/pending/decline", {"id": c["id"]})])
        wins = [(path, r) for path, s, r in out if s == 200]
        assert len(wins) == 1 and sorted(s for _, s, _ in out) == [200, 409, 409, 409], out
        status = pending_rows()[-1][3]
        path, r = wins[0]
        approved = (path.endswith("approve") or (path == "/api/chat/decide" and r["card"]["status"] == "applied"))
        seen.add(status)
        if status == "applied":
            assert approved and cap_of("Other Shopping") == new and len(spy.calls) == applied_before + 1
        else:
            assert status == "rejected" and not approved and cap_of("Other Shopping") == before and len(spy.calls) == applied_before
    assert pending_rows()[-1][3] in ("applied", "rejected") and seen   # (which one wins is up to the scheduler)


def sign_in(app, monkeypatch):
    """The built-in login set up and signed in once: (the session cookie header, its CSRF token)."""
    from argon2 import PasswordHasher
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    monkeypatch.setattr(auth, "_dummy_hash", None)
    auth._tickets.clear()
    auth._enrollments.clear()
    pw = "correct horse battery staple"
    host = {"Host": f"127.0.0.1:{app.port}", "Content-Type": "application/json"}
    _, _, r, _ = app.respond("POST", "/api/auth/enroll/start", host, json.dumps({"username": "owner", "password": pw, "confirm": pw}).encode())
    r = json.loads(r)
    app.respond("POST", "/api/auth/enroll/verify", host,
                json.dumps({"enroll_id": r["enroll_id"], "code": auth.totp(r["key"].replace(" ", ""))}).encode())
    s, _, f, extra = app.respond("POST", "/api/auth/enroll/finish", host, json.dumps({"enroll_id": r["enroll_id"], "saved": True}).encode())
    assert s == 200, f
    cookie = {"Cookie": f"{auth.COOKIE}={dict(extra)['Set-Cookie'].split(';')[0].split('=', 1)[1]}"}
    _, _, html, _ = app.respond("GET", "/", {"Host": host["Host"], **cookie}, b"")
    return cookie, re.search(r'<meta name="csrf-token" content="([^"]+)">', html.decode()).group(1)


def test_every_chat_route_needs_a_session_and_every_post_its_csrf_token(app, llm, monkeypatch, served):
    cookie, token = sign_in(app, monkeypatch)
    ok = {**cookie, "X-CSRF-Token": token}
    c = card_over(served, llm, headers=ok)
    ask_body = {"messages": [{"role": "user", "content": "hi"}]}
    routes = [("GET", "/api/chat", None), ("GET", f"/api/chat/cards?ids={c['id']}", None),
              ("POST", "/api/chat", ask_body), ("POST", "/api/chat/decide", {"id": c["id"], "approve": True})]
    for method, path, body in routes:   # no session (no cookie, or a made-up one): sign in first
        for h in ({}, {"Cookie": f"{auth.COOKIE}=not-a-session"}, {"X-CSRF-Token": token}):
            s, r = wire(served, method, path, body, h)
            assert s == 401 and r.get("signin") is True, (method, path, h, s, r)
    for path, body in (("/api/chat", ask_body), ("/api/chat/decide", {"id": c["id"], "approve": True})):
        for h in (cookie, {**cookie, "X-CSRF-Token": "nope"}, {**cookie, "X-CSRF-Token": ""}):   # a session, no good token
            s, r = wire(served, "POST", path, body, h)
            assert s == 403 and "CSRF" in r["error"], (path, h, s, r)
        s, r = wire(served, "POST", path, body, {**ok, "Origin": "https://evil.example"})     # a good token, a foreign page
        assert s == 403 and r["error"] == "forbidden origin"
    assert len(llm.requests) == 2 and cap_of("Other Shopping") == 850.0 and pending_rows()[0][3] == "pending"
    for method, path, _ in routes[:2]:   # with the session, GETs need no token (they change nothing)
        assert wire(served, method, path, None, cookie)[0] == 200
    s, d = wire(served, "POST", "/api/chat/decide", {"id": c["id"], "approve": True}, ok)
    assert s == 200 and d["card"]["status"] == "applied" and cap_of("Other Shopping") == 900.0


def test_a_stale_approve_replayed_changes_nothing(app, llm, monkeypatch, served):
    """The exact Approve request sent again (same cookie, same token): 409 with the final state, never a second apply;
    after sign-out the same request is 401; another session's token doesn't pass for this one."""
    cookie, token = sign_in(app, monkeypatch)
    ok = {**cookie, "X-CSRF-Token": token}
    spy = Applies(monkeypatch, delay=0)
    health = cap_of("Other Health")
    c = card_over(served, llm, headers=ok)
    body = {"id": c["id"], "approve": True}
    assert wire(served, "POST", "/api/chat/decide", body, ok)[0] == 200
    conn = db.connect(config.db_path())
    with conn:
        conn.execute("UPDATE categories SET monthly_cap = 850 WHERE name = 'Other Shopping'")  # a second apply would show
        user_id = conn.execute("SELECT id FROM auth_users").fetchone()[0]
    other_token, other_csrf = auth.create_session(conn, user_id)
    conn.close()
    for _ in range(3):
        s, d = wire(served, "POST", "/api/chat/decide", body, ok)
        assert s == 409 and d["card"]["status"] == "applied" and d["card"]["result_line"] == "Saved: Other Shopping cap is now $900"
    # a Deny replayed after the Approve doesn't undo or re-decide it either
    s, d = wire(served, "POST", "/api/chat/decide", {"id": c["id"], "approve": False}, ok)
    assert s == 409 and d["card"]["status"] == "applied"
    # a second, live session's CSRF token is not this session's
    c2 = card_over(served, llm, "Other Health", 300.0, headers=ok)
    s, r = wire(served, "POST", "/api/chat/decide", {"id": c2["id"], "approve": True}, {**cookie, "X-CSRF-Token": other_csrf})
    assert s == 403
    # signed out: the same cookie and token are refused before anything is looked at
    assert wire(served, "POST", "/api/auth/logout", {}, ok)[0] == 200
    s, r = wire(served, "POST", "/api/chat/decide", {"id": c2["id"], "approve": True}, ok)
    assert s == 401 and r.get("signin") is True
    assert wire(served, "GET", f"/api/chat/cards?ids={c2['id']}", None, cookie)[0] == 401
    assert len(spy.calls) == 1
    assert cap_of("Other Shopping") == 850.0 and cap_of("Other Health") == health != 300.0
    assert [r[3] for r in pending_rows()] == ["applied", "pending"]


def test_without_a_login_a_form_post_or_a_foreign_page_is_refused(app, llm, served):
    """Local mode (no built-in login): the CSRF defence is JSON only (a form can't send it without a CORS preflight) and
    an Origin that isn't this app's."""
    c = card_over(served, llm)
    n = len(llm.requests)
    for path, raw in (("/api/chat", "messages=hi"), ("/api/chat/decide", f"id={c['id']}&approve=true")):
        for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"):
            s, r = wire(served, "POST", path, raw=raw, headers={"Content-Type": ctype})
            assert s == 415 and r["error"] == "expected application/json", (path, ctype, s, r)
        s, r = wire(served, "POST", path, raw=json.dumps({"id": c["id"], "approve": True, "messages": []}),
                    headers={"Origin": "https://evil.example"})
        assert s == 403 and r["error"] == "forbidden origin"
    assert len(llm.requests) == n and cap_of("Other Shopping") == 850.0 and pending_rows()[0][3] == "pending"


def test_the_panel_decides_and_reads_only_its_own_cards(app, llm):
    """An MCP client's pending action is decided in Settings: /api/chat/decide says 404 and changes nothing, and
    /api/chat/cards doesn't return it."""
    conn = db.connect(config.db_path())
    checked, summary = tooldefs.describe("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 999}, config.db_path())
    other = mcp_state.record_pending(conn, "finance_set_cap", checked, summary, "Claude Desktop")
    conn.close()
    for approve in (True, False):
        s, r = http(app, "POST", "/api/chat/decide", {"id": other["id"], "approve": approve})
        assert s == 404 and r == {"error": f"no Ask card #{other['id']}"}
    s, got = http(app, "GET", f"/api/chat/cards?ids={other['id']}")
    assert s == 200 and got == {"cards": []}
    assert pending_rows() == [(other["id"], "finance_set_cap", "Claude Desktop", "pending")] and cap_of("Other Shopping") == 850.0
    s, _ = http(app, "POST", "/api/mcp/pending/decline", {"id": other["id"]})   # Settings still decides it
    assert s == 200 and pending_rows()[0][3] == "rejected"


@pytest.mark.parametrize("calls", [["garbage"], [{"id": "c9", "type": "function", "function": "finance_status"}], [None]])
def test_malformed_tool_calls_are_a_friendly_line_and_decline_the_turns_card(app, llm, calls):
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 999})},
                  {"message": {"role": "assistant", "content": None, "tool_calls": calls}}]
    s, r = ask(app, "Raise it")
    assert s == 502 and r == {"error": "The AI provider sent an answer the app couldn't read."}
    assert [x[3] for x in pending_rows()] == ["rejected"] and cap_of("Other Shopping") == 850.0


def test_an_unexpected_crash_mid_turn_declines_its_card_and_leaks_nothing(app, llm, monkeypatch):
    from hermes_finance import chat
    real = chat._isolated   # (a read runs in a child process, chat-deadline follow-up: its crash is raised from here)

    def boom(step, name, *args):
        if name == "finance_bills":
            raise RuntimeError("disk I/O error at /secret/path")
        return real(step, name, *args)

    monkeypatch.setattr(chat, "_isolated", boom)
    llm.script = [{"message": call_msg("finance_set_cap", {"category": "Other Shopping", "monthly_cap": 999})},
                  {"message": call_msg("finance_bills", {"next": "7d"}, "c2")}]
    s, r = ask(app, "Raise it, and my bills?")
    assert s == 500 and r == {"error": "internal error (RuntimeError)"}
    assert [x[3] for x in pending_rows()] == ["rejected"] and cap_of("Other Shopping") == 850.0
