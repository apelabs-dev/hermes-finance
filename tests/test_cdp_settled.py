"""TEST-SPEED: tests/cdp.py's readiness waits, which replaced the gates' fixed pauses. Chrome.settled() returns only once
nothing the page started is still going (a fetch and its body, a timer of 1 s or less, an animation frame, a finite CSS
transition, and a frame with no DOM change after them: a ResizeObserver -> requestAnimationFrame re-lay included), and
doesn't wait for what a fixed pause never waited for either (a timer over 1 s, an endless animation); __appear (the layout
gate's wait_for) gives up at once when nothing left could bring its element in time, never while something still could;
goto() returns only on the new page (a new document loaded, a new #hash handled by the page's own listener). Headless
Chrome on a tiny local page; skipped without Chrome."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cdp import Chrome, find_chrome

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")

PAGE = b"""<!doctype html><html><head><style>
.slide { width: 50px; height: 50px; transition: transform .5s linear; } .slide.on { transform: translateX(200px); }
.spin { width: 10px; height: 10px; animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }
</style></head><body><main id="main"><div id="page"><p>page</p></div></main>
<script>addEventListener('hashchange', () => { document.body.dataset.seen = location.hash; });</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(int(self.path.split("ms=")[1]) / 1000)
            body, ctype = json.dumps({"ok": 1}).encode(), "application/json"
        else:
            body, ctype = PAGE, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        b.size(800, 600)
        yield b


@pytest.fixture
def page(chrome, site):
    chrome.goto(f"{site}/?t={time.time()}#/a", ready=False)
    assert chrome.settled() == ""
    return chrome


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def test_settled_waits_for_a_fetch_its_body_and_the_redraw(page):
    page.js("fetch('/slow?ms=400').then((r) => r.json()).then((d) => { document.getElementById('page').append("
            "Object.assign(document.createElement('b'), {id: 'got', textContent: d.ok})); }); 1")
    why, dt = timed(page.settled)
    assert why == "" and dt >= 0.35
    assert page.js("document.getElementById('got').textContent") == "1"


def test_settled_waits_for_a_short_timer_a_debounce(page):
    page.js("setTimeout(() => document.getElementById('page').append(Object.assign(document.createElement('i'), {id: 'late'})), 300); 1")
    why, dt = timed(page.settled)
    assert why == "" and dt >= 0.25 and page.js("!!document.getElementById('late')")


def test_settled_does_not_wait_for_a_timer_over_a_second(page):
    page.js("setTimeout(() => document.getElementById('page').append(Object.assign(document.createElement('i'), {id: 'later'})), 3000); 1")
    why, dt = timed(page.settled)
    assert why == "" and dt < 1.5 and not page.js("!!document.getElementById('later')")


def test_settled_waits_for_a_css_transition_and_ignores_an_endless_animation(page):
    page.js("document.getElementById('page').append(Object.assign(document.createElement('div'), {className: 'spin'})); 1")
    assert timed(page.settled)[1] < 1.5   # the spinner never ends: not waited for
    page.js("(() => { const d = Object.assign(document.createElement('div'), {className: 'slide', id: 'sl'});"
            " document.getElementById('page').append(d); d.getBoundingClientRect(); d.classList.add('on'); return 1; })()")
    why, dt = timed(page.settled)
    assert why == "" and dt >= 0.4
    assert page.js("document.getAnimations().filter((a) => a.effect.target.id === 'sl' && a.playState === 'running').length") == 0


def test_settled_waits_for_a_resize_observer_then_an_animation_frame_re_lay(page):
    """The card packer's pattern (app.js): a ResizeObserver asks for a frame, whose callback moves things."""
    page.js("""(() => { const box = Object.assign(document.createElement('div'), {id: 'box'}); document.getElementById('page').append(box);
      new ResizeObserver(() => requestAnimationFrame(() => requestAnimationFrame(() => { box.dataset.packed = box.offsetHeight; })))
        .observe(box); box.style.height = '80px'; return 1; })()""")
    assert page.settled() == ""
    assert page.js("document.getElementById('box').dataset.packed") == "80"


def test_settled_gives_up_at_its_cap_and_says_why(page):
    page.js("(() => { let n = 0; const tick = () => { if (++n < 200) setTimeout(tick, 20); }; tick(); return 1; })()")
    why, dt = timed(lambda: page.settled(1500))
    assert why.startswith("not settled after 1500 ms") and "timer" in why and 1.4 < dt < 3.5   # (the next test's page
                                                                                                  # is a new document)


def test_appear_waits_while_something_could_still_bring_it(page):
    page.js("setTimeout(() => document.getElementById('page').append(Object.assign(document.createElement('u'), {id: 'u1'})), 300); 1")
    assert page.js("window.__appear(() => document.getElementById('u1'), 4000, '#u1')") is True
    # a 1.5 s timer (not short, so settled() ignores it) is still due before the deadline: waited for
    page.js("setTimeout(() => document.getElementById('page').append(Object.assign(document.createElement('u'), {id: 'u2'})), 1500); 1")
    ok, dt = timed(lambda: page.js("window.__appear(() => document.getElementById('u2'), 4000, '#u2')"))
    assert ok is True and dt >= 1.3


def test_appear_gives_up_at_once_when_nothing_could_bring_it(page):
    with pytest.raises(RuntimeError, match="never appeared: #nothing"):
        page.js("window.__appear(() => document.getElementById('nothing'), 4000, '#nothing')")
    t = time.perf_counter()
    page.js("setTimeout(() => {}, 6000); 1")   # due after the deadline: can't bring it in time
    with pytest.raises(RuntimeError, match="never appeared"):
        page.js("window.__appear(() => document.getElementById('nothing'), 3000, '#nothing')")
    assert time.perf_counter() - t < 1.5


def test_appear_waits_for_a_fetch_in_flight(page):
    page.js("fetch('/slow?ms=600').then((r) => r.json()).then(() => document.getElementById('page').append("
            "Object.assign(document.createElement('s'), {id: 'fetched'}))); 1")
    ok, dt = timed(lambda: page.js("window.__appear(() => document.getElementById('fetched'), 4000, '#fetched')"))
    assert ok is True and dt >= 0.5


def test_goto_returns_on_the_new_document_and_after_a_hash_change_is_handled(chrome, site):
    chrome.goto(f"{site}/?t=one#/a", ready=False)
    first = chrome.js("window.__pend.doc")
    chrome.goto(f"{site}/?t=two#/a", ready=False)   # a new document: loaded, with a new tracker
    assert chrome.js("[location.search, document.readyState, window.__pend.doc !== %r]" % first) == ["?t=two", "complete", True]
    chrome.goto(f"{site}/?t=two#/b", ready=False)   # the same document: the page's hashchange listener has run
    assert chrome.js("document.body.dataset.seen") == "#/b"
    chrome.goto(f"{site}/?t=two#/b", ready=False)   # the same URL again: nothing to wait for (no hashchange comes)
    assert chrome.js("location.hash") == "#/b"
