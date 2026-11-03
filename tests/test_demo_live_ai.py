"""The demo's live-AI switch: on by default, any question answered, and a real quota lock with its tooltip.

Serves a built dist/demo from a stdlib http.server that stubs POST /ai/chat per scenario (no worker, no
network), then drives one real headless Chrome through the states the page ships in.
"""
from __future__ import annotations

import functools
import json
import shutil
import socketserver
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cdp import Chrome, find_chrome

ROOT = Path(__file__).resolve().parents[1]
STUB = "A synthetic answer about your coffee spending, read from this demo's fake data."
SCENARIO = {"mode": "ok", "resets_at": None, "hits": 0}


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 (http.server's spelling)
        if self.path.split("?", 1)[0] != "/ai/chat":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        SCENARIO["hits"] += 1
        mode = SCENARIO["mode"]
        if mode == "ok":
            status, body = 200, {"reply": STUB, "model": "stub/free"}
        elif mode == "quota":
            status, body = 429, {"error": "quota", "scope": "visitor", "resets_at": SCENARIO["resets_at"]}
        else:
            status, body = 503, {"error": "busy"}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # keep the test output clean
        pass


class QuietServer(ThreadingHTTPServer):
    def server_bind(self):  # skip HTTPServer.server_bind's reverse DNS lookup
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@pytest.fixture(scope="module")
def demo_dist(tmp_path_factory):
    existing = ROOT / "dist" / "demo"
    if (existing / "index.html").is_file():
        return existing
    out = tmp_path_factory.mktemp("demo-live-ai") / "demo"
    subprocess.run([sys.executable, str(ROOT / "scripts/build_demo.py"), "--no-canary", "--synthetic-only",
                    "--out", str(out)], check=True, capture_output=True)
    return out


@pytest.fixture(scope="module")
def site(demo_dist):
    srv = QuietServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(demo_dist)))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    if find_chrome() is None:
        pytest.skip("Chrome is not installed")
    with Chrome() as b:
        b.size(1440, 900)
        yield b


def fresh(b, site, mode="ok", resets_at=None):
    SCENARIO.update(mode=mode, resets_at=resets_at, hits=0)
    b.goto(site + "/?demo-today=build")
    b.js("localStorage.clear(); sessionStorage.clear(); true")
    b.goto(site + "/?demo-today=build")
    open_ask(b)


def open_ask(b):
    b.js("document.getElementById('askb').click()")
    b.until("!document.getElementById('chat').hidden")
    b.until("document.getElementById('hfd-live') !== null")


def ask(b, question):
    q = json.dumps(question)
    b.js("(() => { const e = document.querySelector('#chat .chin'); e.value = " + q +
         "; e.dispatchEvent(new Event('input', {bubbles: true})); "
         "document.getElementById('chsend').click(); return true; })()")
    b.until("document.querySelectorAll('#chat .msg.bot').length > 0")
    b.settled(cap_ms=8000)


def last_bot(b):
    return ("[...document.querySelectorAll('#chat .msg.bot')].at(-1)")


def test_fresh_visitor_gets_live_ai_on_with_the_header_badge(browser, site):
    fresh(browser, site)
    assert browser.js("document.getElementById('hfd-live').getAttribute('aria-checked')") == "true"
    assert browser.js("document.getElementById('hfd-live').getAttribute('aria-disabled')") == "false"
    assert browser.js("document.querySelector('#chat .chh .brain').textContent") == "Live AI"
    assert browser.js("document.querySelectorAll('#chat .chfoot #hfd-live').length") == 0


def test_any_question_gets_the_model_answer(browser, site):
    fresh(browser, site)
    question = "What is a good recipe for banana bread?"
    ask(browser, question)
    browser.until(f"{last_bot(browser)}.textContent.includes({json.dumps(STUB[:24])})")
    assert SCENARIO["hits"] == 1
    assert browser.js(f"{last_bot(browser)}.classList.contains('hfd-live-answer')") is True
    assert browser.js(f"{last_bot(browser)}.querySelectorAll('.hfd-blocks').length") == 0
    assert browser.js("document.querySelector('#chat .chh .brain').textContent") == "Live AI"
    assert browser.js("document.getElementById('hfd-live').getAttribute('aria-checked')") == "true"


def test_busy_stays_on_canned_and_keeps_the_suggestions_visible(browser, site):
    fresh(browser, site, mode="busy")
    ask(browser, "What is a good recipe for banana bread?")
    canned = browser.js("HFDemo.ask.data().then((a) => HFDemo.ask.render(a.unmatched.reply))")
    note = browser.js("HFDemo.ask.data().then((a) => a.live.note)")
    browser.until(f"{last_bot(browser)}.textContent.includes({json.dumps(canned[:40])})")
    assert browser.js(f"{last_bot(browser)}.classList.contains('hfd-live-answer')") is False
    assert browser.js(f"{last_bot(browser)}.textContent.includes({json.dumps(note)})") is True
    assert browser.js("document.getElementById('hfd-live').getAttribute('aria-checked')") == "true"
    assert browser.js("document.getElementById('hfd-live').classList.contains('is-locked')") is False
    assert browser.js(f"{last_bot(browser)}.querySelectorAll('.hfd-try .fchip').length") == 3


def test_quota_locks_the_switch_with_a_tooltip_until_it_resets(browser, site):
    resets = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    fresh(browser, site, mode="quota", resets_at=resets)
    ask(browser, "What is a good recipe for banana bread?")
    browser.until("document.getElementById('hfd-live').classList.contains('is-locked')")
    sw = "document.getElementById('hfd-live')"
    assert browser.js(f"{sw}.getAttribute('aria-checked')") == "false"
    assert browser.js(f"{sw}.getAttribute('aria-disabled')") == "true"
    assert browser.js(f"{sw}.dataset.locked") == "1"
    assert browser.js("document.querySelector('#chat .chh .brain').textContent") == "canned demo answers"
    # The reply says live AI ran out, not the canned "no model reads what you type", and keeps its suggestions.
    browser.until(f"{last_bot(browser)}.textContent.includes('Live AI is used up for today')")
    assert browser.js(f"{last_bot(browser)}.textContent.includes('no model reads')") is False
    assert browser.js(f"{last_bot(browser)}.querySelectorAll('.hfd-try .fchip').length") == 3

    # A click is ignored; both a hover and a keyboard focus show the lock reason.
    browser.js(f"{sw}.click()")
    assert browser.js(f"{sw}.getAttribute('aria-checked')") == "false"
    assert browser.js(f"{sw}.classList.contains('is-locked')") is True
    browser.js(f"{sw}.dispatchEvent(new PointerEvent('pointerenter', {{pointerType: 'mouse'}}))")
    assert browser.js("document.getElementById('hfd-live-tip').hidden") is False
    tip = browser.js("document.getElementById('hfd-live-tip').textContent")
    assert "Free live AI is used up for today." in tip and "canned demo answers" in tip
    browser.js(f"{sw}.dispatchEvent(new PointerEvent('pointerleave', {{pointerType: 'mouse'}}))")
    assert browser.js("document.getElementById('hfd-live-tip').hidden") is True
    browser.call("Emulation.setFocusEmulationEnabled", enabled=True)
    browser.js(f"{sw}.focus()")
    assert browser.js("document.activeElement === " + sw) is True
    assert browser.js("document.getElementById('hfd-live-tip').hidden") is False
    browser.js(f"{sw}.blur()")

    # A reload stays locked; an expired stored lock unlocks without a reload on the next visit.
    browser.goto(site + "/?demo-today=build")
    open_ask(browser)
    assert browser.js(f"{sw}.classList.contains('is-locked')") is True
    browser.js("localStorage.setItem('hf-demo.live-ai.lock', JSON.stringify({until: Date.now() - 86400000})); true")
    browser.goto(site + "/?demo-today=build")
    open_ask(browser)
    assert browser.js(f"{sw}.classList.contains('is-locked')") is False
    assert browser.js(f"{sw}.getAttribute('aria-disabled')") == "false"
    browser.js(f"{sw}.click()")
    assert browser.js(f"{sw}.getAttribute('aria-checked')") == "true"
    assert browser.js("document.querySelector('#chat .chh .brain').textContent") == "Live AI"
    # The 429 itself is the scenario; nothing else may go wrong.
    assert [p for p in browser.problems() if "/ai/chat" not in p] == []
