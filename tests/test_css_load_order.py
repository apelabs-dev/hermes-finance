"""The stylesheet order of the pages `finance serve` sends (docs/css-load-order.md): the app page, the sign-in page and
the first-run (setup) page, each fetched over HTTP. A rule wins over another of the same specificity by coming later,
so this order is part of the styling: budgets.css's "No budget yet" rows, for one, are written for a sheet loaded
after app.css."""

from __future__ import annotations

import http.client
import re
import threading
from datetime import date
from pathlib import Path

import pytest

import gate_dbs
from hermes_finance import web

ROOT = Path(__file__).resolve().parents[1]
APP_ORDER = ["app.css", "goals.css", "txmeta.css", "billcal.css", "rollover.css", "alpaca.css", "bills.css", "chat.css",
             "catpick.css", "txfilter.css", "wherewent.css", "budgets.css", "settings.css", "light.css", "tap.css"]
LOGIN_ORDER = ["app.css", "settings.css", "light.css", "tap.css"]
LINK = re.compile(r"""<link\b[^>]*\brel=["']?stylesheet["']?[^>]*>""", re.I)
HREF = re.compile(r"""\bhref=["']?/static/([^"' >]+)""")


def sheets(html: str) -> list[str]:
    return [HREF.search(tag).group(1) for tag in LINK.findall(html)]


@pytest.fixture
def serve(tmp_path):
    servers = []

    def start(kind):
        path = gate_dbs.build(kind, tmp_path / kind / "finance.db", date(2026, 9, 25))
        srv = web.make_server(web.App(path, lambda: None, "127.0.0.1", 0), "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv.server_address[1]

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def get(port: int, path: str) -> tuple[int, str, str]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.getheader("Content-Type", ""), r.read().decode()
    finally:
        c.close()


@pytest.mark.parametrize("kind, order, marker", [
    ("empty", APP_ORDER, "data-slots-used"),             # the app page (local mode)
    ("signin", LOGIN_ORDER, 'data-mode="signin"'),       # the sign-in page
    ("signin-setup", LOGIN_ORDER, 'data-mode="setup"'),  # the first-run page that sets the login up
])
def test_stylesheet_order(serve, kind, order, marker):
    port = serve(kind)
    status, _, html = get(port, "/")
    assert status == 200 and marker in html
    assert sheets(html) == order
    for name in order:   # and each one is served, as CSS
        status, ctype, _ = get(port, f"/static/{name}")
        assert (status, ctype.split(";")[0]) == (200, "text/css"), name


def test_feature_sheets_come_from_feature_files():
    """The app page's feature sheets are FEATURE_FILES in order; budgets.css stays the last of them."""
    assert APP_ORDER[1:-3] == [f"{n}.css" for n in web.FEATURE_FILES]
    assert web.FEATURE_FILES[-1] == "budgets"


def test_the_doc_and_the_header_state_the_same_order():
    doc = (ROOT / "docs" / "css-load-order.md").read_text()
    listed = re.findall(r"^\d+\. `([a-z]+\.css)`", doc, re.M)
    assert listed == APP_ORDER, "docs/css-load-order.md's numbered list is the app page's order"
    login = re.search(r"^The sign-in and first-run page: (.+)$", doc, re.M)
    assert login and re.findall(r"`([a-z]+\.css)`", login.group(1)) == LOGIN_ORDER
    head = (ROOT / "src" / "hermes_finance" / "static" / "budgets.css").read_text().split("*/", 1)[0]
    assert "app.css, then the feature sheets in FEATURE_FILES order" in head
    assert "then settings.css, light.css, tap.css" in head
