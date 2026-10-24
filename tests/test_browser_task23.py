"""TASK-23 in headless Chrome (tests/cdp.py): the Ask panel on the gate's "features" DB, its brain pointed at a fake
OpenAI-compatible server on a loopback port (test_chat_task23.FakeLLM; no real LLM), the app's real backend behind it
(cdp.Server: chat.py, mcp_state, tooldefs, the DB). Every button is pressed with a real pointer tap
(Input.dispatchMouseEvent at its centre, after checking it is what's on top there) and every question typed with real
key events (Input.insertText, then Enter), not el.click() or a synthetic KeyboardEvent.

Desktop 1440: the Ask button bottom-right opens the right-side panel with its brain chip; Shift+Enter is a new line,
Enter sends; the answer comes back with its source line and a "Suggested change" card; Approve applies it once (the DB's
cap changes) and the card shows "Saved: ..."; Esc closes and focus returns to the Ask button. Phone 390: the button sits
above the tab bar, the panel is the whole screen, Deny changes nothing. A card declined in Settings while the panel shows
it: Approve there changes nothing and the card shows its final state. An Approve that fails (the server errors) leaves the
card pending with live buttons and the reason; the next Approve applies it once. The brain down: one friendly line, no
card. With AI off there is no Ask button and every /api/chat route is 404. No JS errors anywhere; the only failed
requests the browser logs are the ones a test caused on purpose."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import chat, db, prefs, tooldefs
from test_chat_task23 import FakeLLM, answer, call_msg

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()
MODEL = "anthropic/claude-haiku-4.5"


def until(sel: str, ms: int = 8000) -> str:
    return (f"new Promise((res, rej) => {{ const t0 = Date.now(); const tick = () => {{ const e = [...document.querySelectorAll("
            f"{json.dumps(sel)})].find((x) => x.getClientRects().length); if (e) res(true); else if (Date.now() - t0 > {ms}) "
            f"rej(new Error('never appeared: ' + {json.dumps(sel)})); else setTimeout(tick, 50); }}; tick(); }})")


def rect(b, sel: str):
    return b.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e || !e.getClientRects().length) return null; "
                f"const r = e.getBoundingClientRect(); return {{l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height}}; }})()")


def text(b, sel: str):
    return b.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); return e ? e.textContent.replace(/\\s+/g, ' ').trim() : null; }})()")


# JS: the centre of the first visible `sel` (whose text starts with `text`) once it has stopped moving (the panel slides
# in) and is what a pointer there would hit; throws if it never is (covered, or not there)
AIM = """(async (sel, text) => {
  const pick = () => [...document.querySelectorAll(sel)].find((e) => e.getClientRects().length
    && (text === null || e.textContent.trim().startsWith(text)));
  const t0 = Date.now(); let last = null, why = 'not there';
  while (Date.now() - t0 < 5000) {
    const e = pick();
    if (e) {
      const r = e.getBoundingClientRect(), key = [r.left, r.top, r.width, r.height].map(Math.round).join(',');
      const x = r.left + r.width / 2, y = r.top + r.height / 2, hit = document.elementFromPoint(x, y);
      why = hit ? 'covered by ' + (hit.id || hit.className || hit.tagName) : 'off screen';
      if (key === last && hit && e.contains(hit)) return {x, y};
      last = key;
    }
    await new Promise((res) => requestAnimationFrame(() => setTimeout(res, 40)));
  }
  throw new Error(`nothing to tap at ${sel} ${text}: ${why}`);
})"""


def tap(b, sel: str, label: str | None = None) -> None:
    """A real pointer tap (press + release) at the element's centre."""
    at = b.js(f"{AIM}({json.dumps(sel)}, {json.dumps(label)})")
    for kind in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=kind, x=at["x"], y=at["y"], button="left", clickCount=1)


def key(b, name: str, shift: bool = False) -> None:
    code = {"Enter": 13, "Escape": 27}[name]
    for kind in ("keyDown", "keyUp"):
        b.call("Input.dispatchKeyEvent", type=kind, key=name, code=name, windowsVirtualKeyCode=code,
               nativeVirtualKeyCode=code, modifiers=8 if shift else 0, **({"text": "\r"} if kind == "keyDown" and name == "Enter" else {}))
    if name == "Escape":   # headless Chrome hides the tab on a real Escape (visibilityState "hidden": no more rAF, so
        b.call("Page.bringToFront")   # every later goto's READY hangs); the page has had the key by now


def type_ask(b, q: str) -> None:
    """Tap the input, type the question with real key input, press Enter."""
    tap(b, "#chat .chin")
    assert b.js("document.activeElement && document.activeElement.classList.contains('chin')")
    b.call("Input.insertText", text=q)
    key(b, "Enter")


def failed_requests(b, *expected: tuple[int, str]) -> list[str]:
    """b.problems() less the failed requests this test caused on purpose ((status, path), each asserted to be there):
    Chrome logs a fetch's 4xx / 5xx answer as an error."""
    got = b.problems()
    rest = list(got)
    for status, path in expected:
        mine = [p for p in rest if p.startswith("log: Failed to load resource") and f"status of {status} " in p and p.endswith(path)]
        assert mine, (status, path, got)
        rest = [p for p in rest if p not in mine]
    return rest


def capped(path) -> tuple[str, float]:
    with sqlite3.connect(path) as c:
        return c.execute("SELECT name, monthly_cap FROM categories WHERE monthly_cap IS NOT NULL ORDER BY name LIMIT 1").fetchone()


def cap_of(path, name: str) -> float:
    with sqlite3.connect(path) as c:
        return c.execute("SELECT monthly_cap FROM categories WHERE name = ?", (name,)).fetchone()[0]


def ask_rows(path) -> list[tuple]:
    with sqlite3.connect(path) as c:
        return c.execute("SELECT client, status FROM pending_actions WHERE client = 'Ask' ORDER BY id").fetchall()


@pytest.fixture
def llm():
    f = FakeLLM()
    yield f
    f.close()


def serve(tmp_path, kind: str, llm=None, base_url=None):
    path = tmp_path / "finance.db"
    gate_dbs.build(kind, path, TODAY)
    if llm is not None or base_url:   # the "local" preset needs no key: the brain is the fake server
        conn = db.connect(path)
        prefs.save(conn, {"ai.mode": "api", "ai.preset": "local", "ai.base_url": base_url or llm.url, "ai.model": MODEL,
                          "ai.monthly_cap": 5.0})
        conn.close()
    srv = Server(path, today=TODAY)
    srv.path = path
    return srv


def settings(srv, path: str, body: dict):
    """A Settings request to the same server (what its Approve / Decline buttons send), from outside the page."""
    s, _, out, _ = srv.app.respond("POST", path, {"Host": f"127.0.0.1:{srv.port}", "Content-Type": "application/json"},
                                   json.dumps(body).encode())
    return s, json.loads(out)


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def go(b, srv, page="overview", width=1440):
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/{page}")
    b.settle()
    b.js("sessionStorage.removeItem('hf.chat.v1'); 1")
    b.problems()


def script(llm, cat: str, new_cap: float):
    llm.script = [{"message": call_msg("finance_budget_status", {}, "r1")},
                  {"message": call_msg("finance_set_cap", {"category": cat, "monthly_cap": new_cap}, "w1")},
                  {"message": answer(f"You're close to your {cat} cap. Raising it to ${new_cap:.0f} would cover a month like this.")}]


def open_panel(b):
    tap(b, "#askb")
    b.js(until("#chat:not([hidden]) .chin"))


def test_desktop_ask_answer_card_approve_and_close(chrome, tmp_path, llm):
    srv = serve(tmp_path, "features", llm)
    try:
        b = chrome
        cat, cap = capped(srv.path)
        new = float(int(cap) + 100)
        script(llm, cat, new)
        go(b, srv)
        btn = rect(b, "#askb")
        assert btn and 1440 - btn["r"] <= 40 and 900 - btn["b"] <= 40, btn        # bottom-right
        open_panel(b)
        panel = rect(b, "#chat")
        assert panel["r"] == 1440 and panel["t"] == 0 and panel["h"] == 900 and 380 <= panel["w"] <= 480, panel
        assert text(b, "#chat .brain") == "via Local · claude-haiku-4.5"
        assert b.js("document.querySelectorAll('#chat .chchips .fchip').length") == 3
        # Shift+Enter is a new line in the question, nothing is sent
        tap(b, "#chat .chin")
        b.call("Input.insertText", text="first line")
        key(b, "Enter", shift=True)
        assert b.js("document.querySelector('#chat .chin').value") == "first line\n" and llm.requests == []
        b.js("(() => { const i = document.querySelector('#chat .chin'); i.value = ''; return 1; })()")
        type_ask(b, f"Should I raise my {cat} cap?")
        b.js(until("#chat .appr[data-status=pending]"))
        assert [m["content"] for m in llm.requests[0]["messages"] if m["role"] == "user"] == [f"Should I raise my {cat} cap?"]
        assert text(b, "#chat .src").startswith("from budget-status")
        assert "Nothing changes until you approve." in text(b, "#chat .appr")
        assert text(b, "#chat .appr .apt") == f"Raise {cat} cap to ${new:.0f}?"
        assert cap_of(srv.path, cat) == cap                                            # nothing applied yet
        tools = [t["function"]["name"] for t in llm.requests[0]["tools"]]
        assert "finance_budget_status" in tools and "finance_set_cap" in tools
        tap(b, "#chat .appr .btn", "Approve")
        b.js(until("#chat .appr[data-status=applied]"))
        assert text(b, "#chat .appr .apres") == f"Saved: {cat} cap is now ${new:.0f}"
        assert b.js("document.querySelectorAll('#chat .appr button').length") == 0     # no live buttons once decided
        assert cap_of(srv.path, cat) == new
        assert ask_rows(srv.path) == [("Ask", "applied")]
        key(b, "Escape")
        assert b.js("document.getElementById('chat').hidden") is True
        assert b.js("document.activeElement && document.activeElement.id") == "askb"
        assert b.problems() == []
    finally:
        srv.close()


def test_phone_button_above_the_tab_bar_full_screen_sheet_and_deny(chrome, tmp_path, llm):
    srv = serve(tmp_path, "features", llm)
    try:
        b = chrome
        cat, cap = capped(srv.path)
        script(llm, cat, float(int(cap) + 50))
        go(b, srv, width=390)
        btn, tab = rect(b, "#askb"), rect(b, "#tabbar")
        assert btn and tab and btn["b"] <= tab["t"], (btn, tab)                        # never covers the tab bar
        open_panel(b)
        panel = rect(b, "#chat")
        assert panel["l"] == 0 and panel["t"] == 0 and panel["w"] == 390 and panel["h"] == 844, panel
        assert b.js("document.documentElement.scrollWidth") <= 390
        type_ask(b, "Raise it?")
        b.js(until("#chat .appr[data-status=pending]"))
        tap(b, "#chat .appr .btn", "Deny")
        b.js(until("#chat .appr[data-status=rejected]"))
        assert text(b, "#chat .appr .apres") == "Declined, nothing changed"
        assert cap_of(srv.path, cat) == cap and ask_rows(srv.path) == [("Ask", "rejected")]
        tap(b, "#chat .chh .ib")                                                       # the ✕
        assert b.js("document.getElementById('chat').hidden") is True
        assert b.js("document.activeElement && document.activeElement.id") == "askb"
        # the conversation is kept for the browser session: reopening shows the decided card, not live buttons
        open_panel(b)
        b.js(until("#chat .appr[data-status=rejected]"))
        assert b.js("document.querySelectorAll('#chat .appr button').length") == 0
        assert b.problems() == []
    finally:
        srv.close()


def test_a_card_declined_in_settings_meanwhile_cannot_be_approved_in_the_panel(chrome, tmp_path, llm):
    srv = serve(tmp_path, "features", llm)
    try:
        b = chrome
        cat, cap = capped(srv.path)
        script(llm, cat, float(int(cap) + 75))
        go(b, srv, width=390)
        open_panel(b)
        type_ask(b, "Raise it?")
        b.js(until("#chat .appr[data-status=pending]"))
        cid = int(b.js("document.querySelector('#chat .appr').dataset.card"))
        s, _ = settings(srv, "/api/mcp/pending/decline", {"id": cid})                 # Settings' Decline, meanwhile
        assert s == 200
        tap(b, "#chat .appr .btn", "Approve")                                          # the panel's stale Approve
        b.js(until("#chat .appr[data-status=rejected]"))
        assert text(b, "#chat .appr .apres") == "Declined, nothing changed"
        assert b.js("document.querySelectorAll('#chat .appr button').length") == 0
        assert cap_of(srv.path, cat) == cap and ask_rows(srv.path) == [("Ask", "rejected")]
        assert failed_requests(b, (409, "/api/chat/decide")) == []
    finally:
        srv.close()


def test_an_approve_that_fails_keeps_the_card_live_and_the_next_one_applies_once(chrome, tmp_path, llm, monkeypatch):
    srv = serve(tmp_path, "features", llm)
    real_decide, applied = chat.decide, []
    real_apply = tooldefs.apply

    def fails_once(app, conn, data, owner=None):
        if not getattr(fails_once, "done", False):
            fails_once.done = True
            raise RuntimeError("the database is locked")      # web.py answers a bare 500; nothing was claimed
        return real_decide(app, conn, data, owner)

    monkeypatch.setattr(chat, "decide", fails_once)
    monkeypatch.setattr(tooldefs, "apply", lambda *a: applied.append(a[0]) or real_apply(*a))
    try:
        b = chrome
        cat, cap = capped(srv.path)
        new = float(int(cap) + 25)
        script(llm, cat, new)
        go(b, srv)
        open_panel(b)
        type_ask(b, "Raise it?")
        b.js(until("#chat .appr[data-status=pending]"))
        tap(b, "#chat .appr .btn", "Approve")
        b.js(until("#chat .appr[data-status=pending] .apres.bad"))
        assert text(b, "#chat .appr .apres.bad") == "internal error (RuntimeError)"
        assert [t for t in b.js("[...document.querySelectorAll('#chat .appr button')].map((e) => e.textContent.trim())")] == ["Approve", "Deny"]
        assert b.js("[...document.querySelectorAll('#chat .appr button')].every((e) => !e.disabled)")
        assert cap_of(srv.path, cat) == cap and ask_rows(srv.path) == [("Ask", "pending")] and applied == []
        tap(b, "#chat .appr .btn", "Approve")
        b.js(until("#chat .appr[data-status=applied]"))
        assert text(b, "#chat .appr .apres") == f"Saved: {cat} cap is now ${new:.0f}"
        assert cap_of(srv.path, cat) == new and ask_rows(srv.path) == [("Ask", "applied")] and applied == ["finance_set_cap"]
        assert failed_requests(b, (500, "/api/chat/decide")) == []
    finally:
        srv.close()


def test_the_brain_down_is_one_friendly_line_and_no_card(chrome, tmp_path):
    srv = serve(tmp_path, "features", base_url="http://127.0.0.1:9/v1")     # nothing listens on port 9
    try:
        b = chrome
        go(b, srv, width=390)
        open_panel(b)
        type_ask(b, "What bills are due this week?")
        b.js(until("#chat .msg.bot.err"))
        assert text(b, "#chat .msg.bot.err") == "Couldn't reach the AI provider. Is it running, and is the address in Settings right?"
        assert b.js("document.querySelectorAll('#chat .appr').length") == 0 and ask_rows(srv.path) == []
        assert b.js("!document.querySelector('#chat .chin').disabled && !document.getElementById('chsend').disabled")
        assert "Traceback" not in b.js("document.getElementById('chat').textContent")
        assert failed_requests(b, (502, "/api/chat")) == []
    finally:
        srv.close()


def test_no_ask_button_when_ai_is_off(chrome, tmp_path):
    srv = serve(tmp_path, "empty")     # gate_dbs "empty": ai.mode off
    try:
        b = chrome
        for width in (1440, 390):
            go(b, srv, width=width)
            assert b.js("!!document.getElementById('askb') || !!document.getElementById('chat')") is False
            assert b.js("document.body.hasAttribute('data-ask')") is False
        assert b.problems() == []
        got = b.js("""Promise.all([['GET', '/api/chat'], ['GET', '/api/chat/cards?ids=1'],
            ['POST', '/api/chat', {messages: [{role: 'user', content: 'hi'}]}], ['POST', '/api/chat/decide', {id: 1, approve: true}]]
          .map(([m, u, body]) => fetch(u, m === 'GET' ? {} : {method: m, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
            .then((r) => r.status)))""")
        assert got == [404, 404, 404, 404]
        assert failed_requests(b, (404, "/api/chat"), (404, "/api/chat/cards?ids=1"), (404, "/api/chat/decide")) == []
    finally:
        srv.close()
