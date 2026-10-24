"""Tailnet access through `tailscale serve`: Host / identity / Origin checks, loopback-only bind."""

from __future__ import annotations

import http.client
import json
import re
import threading

import pytest

from hermes_finance import cli, config, web
from hermes_finance.web import App, make_server

TS_HOST = "my-machine.tail0a1b2c.ts.net"
PUBLIC_URL = f"https://{TS_HOST}:8765"
USER = "owner@example.com"
LOCAL = {"Host": "127.0.0.1:8765"}
# what Tailscale Serve forwards (verified live): Host with the port, the identity header, X-Forwarded-*
TAILNET = {"Host": f"{TS_HOST}:8765", "Tailscale-User-Login": USER,
           "X-Forwarded-Proto": "https", "X-Forwarded-Host": f"{TS_HOST}:8765", "X-Forwarded-For": "100.64.0.7"}
JSON_CT = "application/json"
TAILNET_POST = {**TAILNET, "Origin": PUBLIC_URL, "Content-Type": JSON_CT}


@pytest.fixture
def app(conn, client, fake):
    # TASK-18: there's no built-in default login any more; the allowed identity is given (as --allow-user would)
    fake.on("/link/token/create", {"link_token": "link-production-tailnet", "expiration": "x"})
    return App(config.db_path(), lambda: client, "127.0.0.1", 8765, [PUBLIC_URL], [USER])


def call(app, method, path, headers, body=None):
    status, ctype, raw = app.handle(method, path, headers, json.dumps(body).encode() if body is not None else b"")
    return status, ctype, raw.decode()


def without(headers, *names):
    return {k: v for k, v in headers.items() if k not in names}


# --- allowed -----------------------------------------------------------------------------------

# TASK-10: the "Open this page in Safari or Chrome ..." note is gone everywhere (tailnet or not).
BROWSER_NOTE = ("Safari or Chrome", "built-in browser", "allow pop-ups", "tailnet-note", "Discord")


def has_browser_note(text):
    return any(bit in text for bit in BROWSER_NOTE)


def test_tailnet_page_and_link_token(app, fake):
    status, _, html = call(app, "GET", "/", TAILNET)
    assert status == 200 and "Add account" in html
    assert not has_browser_note(html) and not hasattr(web, "TAILNET_NOTE")

    status, _, body = call(app, "POST", "/api/link_token", TAILNET_POST, {})
    assert status == 200 and json.loads(body)["link_token"] == "link-production-tailnet"
    assert fake.paths() == ["/link/token/create"]
    (sent,) = fake.bodies("/link/token/create")
    assert "redirect_uri" not in sent


@pytest.mark.parametrize("host", [TS_HOST, f"{TS_HOST}:8765", f"{TS_HOST.upper()}:8765"])
def test_public_host_with_or_without_port(app, host):
    assert call(app, "GET", "/", {**TAILNET, "Host": host})[0] == 200


def test_allowed_user_is_case_insensitive(app):
    assert call(app, "GET", "/", {**TAILNET, "Tailscale-User-Login": "Owner@Example.COM"})[0] == 200


def test_no_default_allowed_user_identities_come_from_the_flags_or_the_setting(conn, client):
    # TASK-18: no login is allowed by default (config.DEFAULT_ALLOWED_USERS is gone): with a public URL and no
    # identity at all, every proxied request is refused and says how to add one
    bare = App(config.db_path(), lambda: client, public_urls=[PUBLIC_URL])
    assert bare.allowed_users == set() and not hasattr(config, "DEFAULT_ALLOWED_USERS")
    status, _, html = call(bare, "GET", "/", TAILNET)
    assert status == 403 and "no allowed identity is set" in html and "Access &amp; security" in html
    app = App(config.db_path(), lambda: client, public_urls=[PUBLIC_URL], allowed_users=["Friend@Example.com"])
    assert call(app, "GET", "/", {**TAILNET, "Tailscale-User-Login": "friend@example.com"})[0] == 200
    assert call(app, "GET", "/", TAILNET)[0] == 403
    assert App(config.db_path(), lambda: client).allowed_users == set()
    # the saved setting (seeded once from --allow-user, or added in Settings) lets the same login in, no flag needed
    from hermes_finance import access
    access.add_identity(conn, USER)
    assert call(bare, "GET", "/", TAILNET)[0] == 200


# --- refused -----------------------------------------------------------------------------------

def test_tailnet_without_identity_header_is_403(app, fake):
    status, ctype, html = call(app, "GET", "/", without(TAILNET, "Tailscale-User-Login"))
    assert status == 403 and ctype.startswith("text/html")
    assert "Tailscale-User-Login" in html and "tailscale serve" in html

    status, ctype, body = call(app, "POST", "/api/link_token", without(TAILNET_POST, "Tailscale-User-Login"), {})
    assert status == 403 and ctype == "application/json"
    assert "Tailscale-User-Login header is missing" in json.loads(body)["error"]

    assert call(app, "GET", "/", {**TAILNET, "Tailscale-User-Login": "  "})[0] == 403
    assert fake.paths() == []


def test_tailnet_user_not_allowed_is_403(app, fake):
    status, _, html = call(app, "GET", "/", {**TAILNET, "Tailscale-User-Login": "spoof@evil.example"})
    assert status == 403 and "spoof@evil.example is not allowed" in html
    status, _, body = call(app, "POST", "/api/link_token", {**TAILNET_POST, "Tailscale-User-Login": "spoof@evil.example"}, {})
    assert status == 403 and "not allowed" in json.loads(body)["error"]
    assert fake.paths() == []


@pytest.mark.parametrize("host", ["evil.com", "100.64.0.99:8765", f"{TS_HOST}:9999", f"{TS_HOST}.evil.com",
                                  "127.0.0.1:9999", ""])
def test_foreign_host_is_403_even_with_identity(app, fake, host):
    status, _, body = call(app, "GET", "/", {**TAILNET, "Host": host})
    assert status == 403 and json.loads(body)["error"] == "forbidden host"
    status, _, _ = call(app, "POST", "/api/link_token", {**TAILNET_POST, "Host": host}, {})
    assert status == 403
    assert fake.paths() == []


def test_missing_host_header_is_403(app):
    assert call(app, "GET", "/", without(TAILNET, "Host"))[0] == 403


@pytest.mark.parametrize("origin", [
    "https://evil.com",
    f"http://{TS_HOST}:8765",        # wrong scheme
    f"https://{TS_HOST}",            # wrong port (443)
    f"https://{TS_HOST}:8765.evil.com",
    f"https://{TS_HOST}:8765@evil.example",
    f"https://{TS_HOST}:8765/path",
    "https://127.0.0.1:8765",        # https on the localhost port
    "null",
])
def test_foreign_origin_is_403(app, fake, origin):
    status, _, body = call(app, "POST", "/api/link_token", {**TAILNET_POST, "Origin": origin}, {})
    assert status == 403 and json.loads(body)["error"] == "forbidden origin"
    assert fake.paths() == []


def test_x_forwarded_headers_never_grant_access(app):
    # proxy headers can't grant a foreign Host (and with a localhost Host they refuse, see below)
    assert call(app, "GET", "/", {**TAILNET, "Host": "evil.com", "X-Forwarded-Host": f"{TS_HOST}:8765"})[0] == 403


# --- Host spoof through Serve: it forwards the client's Host unchanged ----------------------------

# what a tailnet client sending `Host: 127.0.0.1:8765` to the Serve URL reaches the backend with (verified live)
SPOOFED = {**TAILNET, "Host": "127.0.0.1:8765", "X-Forwarded-Host": "127.0.0.1:8765"}


@pytest.mark.parametrize("proxy", [
    {"X-Forwarded-For": "100.64.0.99"},
    {"Tailscale-User-Login": USER},
    {"X-Forwarded-Proto": "https"},
    {"X-Forwarded-Host": "127.0.0.1:8765"},
    {"Forwarded": "for=100.64.0.99;proto=https"},
    {"Tailscale-User-Name": "Alex Example"},
    {"Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers"},
    {"x-forwarded-for": ""},  # any case, even empty
    {"FORWARDED": "for=100.64.0.99"},
])
@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_localhost_host_with_proxy_header_is_403(app, fake, host, proxy):
    status, _, body = call(app, "GET", "/", {"Host": host, **proxy})
    assert status == 403 and json.loads(body)["error"] == "forbidden host"
    status, _, body = call(app, "POST", "/api/link_token",
                           {"Host": host, **proxy, "Content-Type": "application/json"}, {})
    assert status == 403 and json.loads(body)["error"] == "forbidden host"
    assert call(app, "GET", "/api/status", {"Host": host, **proxy})[0] == 403
    assert fake.paths() == []


def test_spoofed_localhost_host_via_serve_is_403(app, fake):
    status, _, body = call(app, "GET", "/", SPOOFED)
    assert status == 403 and json.loads(body)["error"] == "forbidden host"
    for origin in ("http://127.0.0.1:8765", PUBLIC_URL):
        headers = {**SPOOFED, "Origin": origin, "Content-Type": "application/json"}
        assert call(app, "POST", "/api/link_token", headers, {})[0] == 403
    assert fake.paths() == []


def test_spoof_refused_without_public_urls_too(conn, client, fake):
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    assert call(app, "GET", "/", {**LOCAL, "X-Forwarded-For": "100.64.0.99"})[0] == 403
    assert call(app, "GET", "/", LOCAL)[0] == 200
    assert fake.paths() == []


def test_full_serve_headers_on_public_host_still_allowed(app, fake):
    full = {**TAILNET, "Tailscale-User-Name": "Alex Example", "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers",
            "Forwarded": "for=100.64.0.7;proto=https"}
    status, _, html = call(app, "GET", "/", full)
    assert status == 200 and "Add account" in html
    status, _, body = call(app, "POST", "/api/link_token", {**full, "Origin": PUBLIC_URL, "Content-Type": JSON_CT}, {})
    assert status == 200 and json.loads(body)["link_token"] == "link-production-tailnet"
    assert fake.paths() == ["/link/token/create"]


def test_plain_localhost_without_proxy_headers_still_allowed(app, fake):
    status, _, html = call(app, "GET", "/", LOCAL)
    assert status == 200 and not has_browser_note(html)
    headers = {**LOCAL, "Origin": "http://127.0.0.1:8765", "Content-Type": JSON_CT}
    status, _, body = call(app, "POST", "/api/link_token", headers, {})
    assert status == 200 and json.loads(body)["link_token"] == "link-production-tailnet"
    assert fake.paths() == ["/link/token/create"]


# --- localhost unchanged -----------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_localhost_needs_no_identity_and_has_no_banner(app, host):
    status, _, html = call(app, "GET", "/", {"Host": host})
    assert status == 200 and not has_browser_note(html)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"])
def test_localhost_origins_still_allowed(app, origin):
    headers = {**LOCAL, "Origin": origin, "Content-Type": "application/json"}
    assert call(app, "POST", "/api/link_token", headers, {})[0] == 200


def test_localhost_without_public_urls_is_as_before(conn, client, fake):
    fake.on("/link/token/create", {"link_token": "link-production-x", "expiration": "x"})
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    assert call(app, "GET", "/", LOCAL)[0] == 200
    assert call(app, "GET", "/", TAILNET)[0] == 403  # tailnet host isn't accepted unless configured
    post = {**LOCAL, "Content-Type": "application/json"}
    assert call(app, "POST", "/api/link_token", {**post, "Origin": "http://127.0.0.1:8765"}, {})[0] == 200
    assert call(app, "POST", "/api/link_token", {**post, "Origin": "http://127.0.0.1:9999"}, {})[0] == 403
    assert call(app, "POST", "/api/link_token", {**post, "Origin": PUBLIC_URL}, {})[0] == 403


# --- page works over https ---------------------------------------------------------------------

def test_page_uses_only_relative_urls(app):
    html = call(app, "GET", "/", TAILNET)[2]
    assert "http://" not in html
    assert re.findall(r"https?://[^\s\"'<>]+", html) == ["https://cdn.plaid.com/link/v2/stable/link-initialize.js"]
    assert "redirect_uri" not in html
    # TASK-4: the page's own code is /static/app.js (the CSP allows no inline script); same checks there
    assets = re.findall(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"', html)
    assert [a for a in assets if not a.startswith("/static/")] == ["https://cdn.plaid.com/link/v2/stable/link-initialize.js"]
    js = call(app, "GET", "/static/app.js", TAILNET)[2]
    assert re.findall(r"https?://", js) == []
    posts = re.findall(r"post\('([^']*)'", js)
    assert posts and all(p.startswith("/api/") for p in posts)
    gets = re.findall(r"api\('([^']*)'", js)
    assert gets and all(p.startswith("/api/") for p in gets)
    assert "redirect_uri" not in js


# --- public URL parsing ------------------------------------------------------------------------

@pytest.mark.parametrize("url,origin,hosts", [
    (PUBLIC_URL, PUBLIC_URL, {TS_HOST, f"{TS_HOST}:8765"}),
    (f"HTTPS://{TS_HOST.upper()}:8765/", PUBLIC_URL, {TS_HOST, f"{TS_HOST}:8765"}),
    (f"https://{TS_HOST}:443", f"https://{TS_HOST}", {TS_HOST, f"{TS_HOST}:443"}),
    (f"https://{TS_HOST}", f"https://{TS_HOST}", {TS_HOST, f"{TS_HOST}:443"}),
    ("http://box.local:80", "http://box.local", {"box.local", "box.local:80"}),
])
def test_parse_public_url(url, origin, hosts):
    assert web.parse_public_url(url) == (origin, hosts)


@pytest.mark.parametrize("url", [TS_HOST, f"ftp://{TS_HOST}", f"https://{TS_HOST}:8765/finance",
                                 f"https://{TS_HOST}:99999", f"https://u@{TS_HOST}", "https://", ""])
def test_invalid_public_url_rejected(url):
    with pytest.raises(ValueError, match="invalid public URL"):
        web.parse_public_url(url)


# --- CLI: flags + env, loopback-only bind ------------------------------------------------------

def test_flags_and_env_are_merged(monkeypatch):
    monkeypatch.setenv("FINANCE_PUBLIC_URLS", f" https://other.ts.net , {PUBLIC_URL},")
    monkeypatch.setenv("FINANCE_ALLOWED_USERS", "b@example.com,,a@example.com")
    args = cli.build_parser().parse_args(["serve", "--public-url", PUBLIC_URL,
                                          "--allow-user", "a@example.com", "--allow-user", "c@example.com"])
    assert cli.serve_access(args) == ([PUBLIC_URL, "https://other.ts.net"],
                                      ["a@example.com", "c@example.com", "b@example.com"])


def test_no_flags_no_env(monkeypatch):
    monkeypatch.delenv("FINANCE_PUBLIC_URLS", raising=False)
    monkeypatch.delenv("FINANCE_ALLOWED_USERS", raising=False)
    assert cli.serve_access(cli.build_parser().parse_args(["serve"])) == ([], [])


@pytest.fixture
def no_bind(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("server must not start")
    monkeypatch.setattr(cli, "_client", refuse)
    monkeypatch.setattr(web, "make_server", refuse)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "100.64.0.99", "192.168.1.20", TS_HOST, ""])
def test_non_loopback_host_is_refused(no_bind, capsys, host):
    assert cli.main(["serve", "--host", host, "--port", "8799"]) == 2
    err = capsys.readouterr().err
    assert "refusing to listen" in err and "tailscale serve" in err


def test_invalid_public_url_exits_2(cli_client, data_dir, monkeypatch, capsys):
    monkeypatch.setattr(web, "make_server", lambda *a, **kw: pytest.fail("server must not start"))
    assert cli.main(["serve", "--port", "8799", "--public-url", TS_HOST]) == 2
    assert "invalid public URL" in capsys.readouterr().err


@pytest.mark.parametrize("host,ok", [("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True),
                                     ("127.0.0.2", True), ("0.0.0.0", False), ("::", False), ("", False),
                                     ("100.64.0.99", False), (TS_HOST, False)])
def test_is_loopback(host, ok):
    assert web.is_loopback(host) is ok


def test_make_server_refuses_non_loopback(conn, client):
    with pytest.raises(ValueError, match="only loopback"):
        make_server(App(config.db_path(), lambda: client), "0.0.0.0", 0)


# --- real HTTP round-trip, as Tailscale Serve would send it ------------------------------------

def test_tailnet_over_real_socket(conn, client, fake):
    fake.on("/link/token/create", {"link_token": "link-production-sock", "expiration": "x"})
    app = App(config.db_path(), lambda: client, "127.0.0.1", 0, [PUBLIC_URL], [USER])
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def request(method, path, headers, body=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            r = c.getresponse()
            return r.status, r.read().decode()

        assert request("GET", "/", TAILNET)[0] == 200
        status, body = request("POST", "/api/link_token", TAILNET_POST, {})
        assert status == 200 and json.loads(body)["link_token"] == "link-production-sock"
        assert request("GET", "/", without(TAILNET, "Tailscale-User-Login"))[0] == 403
        assert request("POST", "/api/link_token", {**TAILNET_POST, "Origin": f"http://{TS_HOST}:8765"}, {})[0] == 403
        spoofed = {**SPOOFED, "Host": f"127.0.0.1:{srv.server_address[1]}"}
        assert request("GET", "/", spoofed)[0] == 403
        assert request("POST", "/api/link_token", {**spoofed, "Content-Type": JSON_CT}, {})[0] == 403
        assert request("GET", "/", {"Host": f"127.0.0.1:{srv.server_address[1]}"})[0] == 200
        assert fake.paths() == ["/link/token/create"]
    finally:
        srv.shutdown()
        srv.server_close()
