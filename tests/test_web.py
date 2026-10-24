"""Real HTTP round-trips against `finance serve`'s server (Plaid still mocked)."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from hermes_finance import config
from hermes_finance.web import App, make_server


@pytest.fixture
def server(conn, client, fake):
    fake.on("/link/token/create", {"link_token": "link-production-e2e", "expiration": "x"})
    app = App(config.db_path(), lambda: client, "127.0.0.1", 0)
    srv = make_server(app, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def request(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, r.read().decode()


def test_page_renders(server):
    status, html = request(server, "GET", "/")
    assert status == 200
    assert "Add account" in html and "slots used: <b>0 / 10</b>" in html
    assert "https://cdn.plaid.com/link/v2/stable/link-initialize.js" in html


def test_link_token_endpoint(server):
    status, body = request(server, "POST", "/api/link_token", {})
    assert status == 200 and json.loads(body)["link_token"] == "link-production-e2e"


def test_rejects_non_json_post_and_foreign_origin(server):
    status, _ = request(server, "POST", "/api/link_token", headers={"Content-Type": "text/plain"})
    assert status == 415
    status, _ = request(server, "POST", "/api/link_token", {}, {"Origin": "https://evil.example"})
    assert status == 403
    status, _ = request(server, "GET", "/", headers={"Host": "evil.example:80"})
    assert status == 403
