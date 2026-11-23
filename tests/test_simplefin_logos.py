"""The two chooser logos (owner decision 2026-11-15): fetched server-side from each vendor's own page, checked by
logos.py and cached in `institutions`, never a file in the repository and never the vendors' real bytes. A local stub
HTTP server answers with synthetic PNGs only; when the fetch fails or has not run, the page's initials tile stands in.
"""

from __future__ import annotations

import http.client
import http.server
import json
import threading
from datetime import date, timedelta

import pytest

from hermes_finance import logos
from pngtools import decode_png, logo_png

TODAY = logos.utc_today()
PNG = logo_png(64)


class Stub(http.server.BaseHTTPRequestHandler):
    icon_status = 200
    head = '<link rel="apple-touch-icon" href="/icon.png">'

    def do_GET(self):  # noqa: N802 - http.server's name
        if self.path == "/":
            body, kind = f"<html><head>{self.head}</head><body></body></html>".encode(), "text/html"
            status = 200
        elif self.path == "/icon.png":
            body, kind, status = PNG, "image/png", self.icon_status
        else:
            body, kind, status = b"not found", "text/plain", 404
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def fetch_to(stub, calls=None):
    """logos.Fetch that asks the local stub, whatever host the app thinks it is asking (https only in production)."""
    def fetch(url):
        if calls is not None:
            calls.append(url)
        path = "/icon.png" if url.rstrip("/").endswith("icon.png") else "/"
        conn = http.client.HTTPConnection("127.0.0.1", stub.server_address[1], timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            return logos.Response(resp.status, resp.getheader("Content-Type", ""), body, url)
        finally:
            conn.close()
    return fetch


@pytest.fixture(autouse=True)
def vendors_on(monkeypatch):
    monkeypatch.delenv("HERMES_SIMPLEFIN", raising=False)
    monkeypatch.delenv("HERMES_PLAID", raising=False)


def test_both_vendor_icons_are_fetched_checked_and_cached(conn, stub):
    calls = []
    out = logos.ensure_vendor_logos(conn, fetch=fetch_to(stub, calls), today=TODAY)
    assert {o["institution_id"] for o in out} == {"ins_hf_simplefin", "ins_hf_plaid"}
    assert {o["source"] for o in out} == {"site"} and all(o["error"] is None for o in out)
    rows = {r["institution_id"]: (r["name"], r["logo_source"], bytes(r["logo_png"])) for r in conn.execute(
        "SELECT institution_id, name, logo_png, logo_source FROM institutions")}
    assert rows["ins_hf_simplefin"] == ("SimpleFIN", "site", PNG)
    assert rows["ins_hf_plaid"] == ("Plaid", "site", PNG)
    assert decode_png(rows["ins_hf_simplefin"][2])[0] == 64
    payload = {p["institution_id"]: p for p in logos.institutions_payload(conn)}
    assert payload["ins_hf_simplefin"]["logo"] is True and payload["ins_hf_plaid"]["logo"] is True
    kind, body = logos.institution_logo(conn, "ins_hf_simplefin")
    assert kind == "image/png" and body == PNG
    assert calls and all(u.startswith("https://") for u in calls)   # the app thinks https; the stub is the transport


def test_a_failed_or_svg_fetch_leaves_the_initials_fallback(conn, stub, monkeypatch):
    monkeypatch.setattr(Stub, "head", '<link rel="icon" href="/logo.svg" type="image/svg+xml">')
    out = logos.ensure_vendor_logos(conn, fetch=fetch_to(stub), today=TODAY)
    assert {o["source"] for o in out} == {"none"}
    payload = {p["institution_id"]: p for p in logos.institutions_payload(conn)}
    assert payload["ins_hf_simplefin"]["logo"] is False and payload["ins_hf_plaid"]["logo"] is False
    assert logos.institution_logo(conn, "ins_hf_simplefin") is None


def test_a_miss_is_remembered_and_retried_only_after_a_day(conn, stub):
    calls = []
    b = Stub
    old = b.icon_status
    b.icon_status = 404
    try:
        first = logos.ensure_vendor_logos(conn, fetch=fetch_to(stub, calls), today=TODAY)
    finally:
        b.icon_status = old
    assert {o["source"] for o in first} == {"none"}
    n = len(calls)
    again = logos.ensure_vendor_logos(conn, fetch=fetch_to(stub, calls), today=TODAY)
    assert again == [] and len(calls) == n                     # the same day: no retry
    b.icon_status = 200
    later = logos.ensure_vendor_logos(conn, fetch=fetch_to(stub, calls), today=TODAY + timedelta(days=1))
    assert {o["source"] for o in later} == {"site"} and len(calls) > n


def test_the_test_switch_means_no_fetch_at_all(conn, stub, monkeypatch):
    monkeypatch.setenv("HERMES_SIMPLEFIN", "off")
    monkeypatch.setenv("HERMES_PLAID", "off")
    calls = []
    assert logos.ensure_vendor_logos(conn, fetch=fetch_to(stub, calls), today=TODAY) == []
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM institutions").fetchone()[0] == 0


def test_the_accounts_payload_triggers_the_fetch_in_the_background(conn, data_dir, stub, monkeypatch):
    from hermes_finance import api
    calls = []
    monkeypatch.setattr(logos, "http_get", fetch_to(stub, calls))
    got = api.accounts(conn)
    assert isinstance(got, dict)
    for _ in range(200):
        if conn.execute("SELECT COUNT(*) FROM institutions WHERE institution_id IN "
                        "('ins_hf_simplefin', 'ins_hf_plaid') AND logo_png IS NOT NULL").fetchone()[0] == 2:
            break
        threading.Event().wait(0.02)
    assert conn.execute("SELECT COUNT(*) FROM institutions WHERE institution_id IN "
                        "('ins_hf_simplefin', 'ins_hf_plaid') AND logo_png IS NOT NULL").fetchone()[0] == 2
