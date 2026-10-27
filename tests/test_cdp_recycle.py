"""A Chrome whose page stops answering is replaced, not reused (t_d2b88168, slow-mode run 4).

Run 4: one test's page wedged its renderer, and every later goto() in that Chrome failed with "Page.navigate: no
answer by its deadline" (85 tests of test_budget_editor_sheet, 50 extra minutes). A goto() that fails now still fails
its own test, but leaves behind a fresh Chrome (same object, same slot) with the setup the test gave the old one.
"""
import functools
import http.server
import threading
import time

import pytest

import threading

import cdp
from cdp import Chrome, _exited

STUCK = ('<html><body>stuck<script>addEventListener("load", () => setTimeout(() => { for (;;) {} }, 0));'
         '</script></body></html>')


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    d = tmp_path_factory.mktemp("recycle")
    (d / "stuck.html").write_text(STUCK)
    (d / "ok.html").write_text('<html><head><meta name="viewport" content="width=device-width"></head><body>ok</body></html>')
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(d)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_a_wedged_page_fails_its_goto_and_the_next_goto_gets_a_fresh_chrome(site, monkeypatch):
    # This Chrome wedges on purpose: with HF_CHROME_LOG on (the Nightly) it was kept and counted as a wedge every run
    # (Nightly 40572130051, 40580322138), so check.py's "Chromes that wedged" counts real ones only.
    monkeypatch.delenv(cdp.LOG_ENV, raising=False)
    with Chrome() as b:
        b.call("Page.addScriptToEvaluateOnNewDocument", source="window.__mark = 'kept';")
        b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
        b.size(390, 844)
        old, held = b.proc, cdp._HELD.get(threading.get_ident())
        with pytest.raises(RuntimeError):
            b.goto(site + "/stuck.html")
        assert _exited(old), "the wedged Chrome is still running"
        # replaced, holding a slot, and this thread holds as many as before: none leaked, none taken twice
        # (this thread's count, not slots_in_use(): that's machine-wide, and other workers come and go: Nightly 40451942425)
        assert b.proc is not old and b.slot is not None and cdp._HELD.get(threading.get_ident()) == held == 1
        t = time.monotonic()
        b.goto(site + "/ok.html", ready=False)
        assert time.monotonic() - t < 10
        got = b.js("[document.body.textContent, window.__mark, matchMedia('(prefers-color-scheme: dark)').matches, "
                   "innerWidth]", time.monotonic() + 10)
        assert got == ["ok", "kept", True, 390], got   # the test's own setup came along
        assert (b.width, b.height, b.mobile) == (390, 844, True)
    assert _exited(b.proc)


def test_a_page_that_loads_is_not_replaced(site):
    with Chrome() as b:
        old = b.proc
        b.goto(site + "/ok.html", ready=False)
        b.goto(site + "/ok.html?again", ready=False)
        assert b.proc is old and not _exited(old)
