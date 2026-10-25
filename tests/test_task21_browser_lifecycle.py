"""TASK-21's resource lifetime and error checks, without launching a browser.

The real browser checks share a document across tests but replace their servers.
These contracts must also hold when a request finishes late or a test fails.
"""

import inspect

import pytest

import test_browser_task21 as browser21


class PendingPage:
    def __init__(self, problems=(), pending=""):
        self.done = False
        self.late_problems = list(problems)
        self.pending = pending

    def settled(self):
        self.done = True
        return self.pending

    def problems(self):
        return self.late_problems if self.done else []

    def js(self, expression):
        return True


def test_clean_waits_for_background_reads():
    page = PendingPage()
    browser21.clean(page)
    assert page.done, "the error check returned while a background read was still pending"


def test_clean_reports_errors_from_a_late_background_read():
    page = PendingPage(problems=["log: Failed to load resource: net::ERR_CONNECTION_RESET /api/saved-filters?window=1"])
    with pytest.raises(AssertionError, match="ERR_CONNECTION_RESET"):
        browser21.clean(page)


def test_clean_does_not_accept_a_settle_timeout():
    page = PendingPage(pending="not settled after 10000 ms: fetch")
    with pytest.raises(AssertionError, match="not settled"):
        browser21.clean(page)


@pytest.mark.parametrize("test_fails", [False, True])
def test_fresh_unloads_its_page_before_stopping_the_server(tmp_path, monkeypatch, test_fails):
    events = []

    class Page:
        def goto(self, url, ready=True):
            assert url == "about:blank" and ready is False
            events.append("document unloaded")

    class Site:
        def close(self):
            events.append("server stopped")

    monkeypatch.setattr(browser21, "serve", lambda path: Site())
    fixture = browser21.fresh.__wrapped__
    # Also run against the previous fixture, which did not declare its browser dependency.
    args = {"tmp_path": tmp_path, "chrome": Page()}
    lifetime = fixture(**{k: v for k, v in args.items() if k in inspect.signature(fixture).parameters})
    next(lifetime)
    if test_fails:
        lifetime.close()  # pytest tears down the fixture even when the test assertion fails
    else:
        with pytest.raises(StopIteration):
            next(lifetime)
    assert events == ["document unloaded", "server stopped"]


def test_fresh_closes_the_server_even_if_unloading_fails(tmp_path, monkeypatch):
    events = []

    class Page:
        def goto(self, url, ready=True):
            events.append("unload failed")
            raise RuntimeError("navigation failed")

    class Site:
        def close(self):
            events.append("server stopped")

    monkeypatch.setattr(browser21, "serve", lambda path: Site())
    fixture = browser21.fresh.__wrapped__
    args = {"tmp_path": tmp_path, "chrome": Page()}
    lifetime = fixture(**{k: v for k, v in args.items() if k in inspect.signature(fixture).parameters})
    next(lifetime)
    with pytest.raises(RuntimeError, match="navigation failed"):
        next(lifetime)
    assert events == ["unload failed", "server stopped"]
