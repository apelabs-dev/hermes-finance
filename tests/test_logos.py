"""TASK-10: real bank logos and holding logos. Both are fetched server-side once, cached in the DB, served from
/api/.../logo, and never hotlinked (the CSP stays first-party). Every HTTP call here is mocked."""

from __future__ import annotations

import base64
import http.client
import io
import json
import socket
import struct
import threading
import urllib.error
import urllib.request
from datetime import date

import pytest

from conftest import TOKEN_NEW, add_item, account
from factory import acct, bank
from pngtools import logo_png, make_png
from hermes_finance import cli, config, link, logos, portfolio, web
from hermes_finance.logos import FetchError, LogoError, Response
from hermes_finance.sync import sync_all
from hermes_finance.web import App, make_server

LOCAL = {"Host": "127.0.0.1:8765"}
PNG = logo_png(64)
GET_BY_ID = "/institutions/get_by_id"
INSTITUTIONS = {  # as measured live 2026-09-26 (logo bytes replaced by a synthetic PNG)
    "ins_126339": {"name": "Nova", "logo": base64.b64encode(PNG).decode(), "primary_color": "#00a9ce",
                   "url": "https://www.novabank.example"},
    "ins_56": {"name": "Chase", "logo": None, "primary_color": "#095aa6", "url": "https://www.chase.com"},
    "ins_5": {"name": "Citibank Online", "logo": None, "primary_color": "#204081", "url": "https://www.citi.com"},
    "ins_127991": {"name": "Wells Fargo", "logo": None, "primary_color": "#d11f37", "url": "https://www.wellsfargo.com"},
    "ins_nourl": {"name": "Tiny Credit Union", "logo": None, "primary_color": "#123456", "url": None},
}
FMP = "https://financialmodelingprep.com/image-stock/{}.png"


class Web:
    """Mocked HTTP for logos.fetch: url -> Response (or an exception to raise); 404 for anything else."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        r = self.routes.get(url)
        if r is None:
            return Response(404, "text/html", b"<h1>not found</h1>", url)
        if isinstance(r, Exception):
            raise r
        return r


def img(body=PNG, ctype="image/png"):
    return Response(200, ctype, body, "")


def html(text):
    return Response(200, "text/html; charset=utf-8", text.encode(), "")


def ico_with_png(png):
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 64, 64, 0, 0, 1, 32, len(png), 22) + png


@pytest.fixture
def plaid_insts(fake):
    fake.on(GET_BY_ID, lambda b: {"institution": {"institution_id": b["institution_id"], **INSTITUTIONS[b["institution_id"]]}}
            if b["institution_id"] in INSTITUTIONS else (400, {"error_type": "INVALID_INPUT",
                                                             "error_code": "INVALID_INSTITUTION", "error_message": "x"}))
    return fake


def row(conn, institution_id):
    return conn.execute("SELECT * FROM institutions WHERE institution_id = ?", (institution_id,)).fetchone()


# --- the institutions cache: Plaid logo, then the bank's own icon, then initials ----------------------

def test_plaid_logo_first_and_nothing_else_fetched(conn, client, plaid_insts):
    bank(conn, "item_nova", "Nova", institution_id="ins_126339")
    net = Web()
    out = logos.refresh_institutions(conn, client, fetch=net)
    r = row(conn, "ins_126339")
    assert (r["name"], r["logo_source"], r["primary_color"], r["url"]) == ("Nova", "plaid", "#00a9ce", "https://www.novabank.example")
    assert r["logo_png"] == PNG and r["fetched_at"]
    assert net.calls == []                                                # Plaid had one: no site fetch at all
    assert plaid_insts.bodies(GET_BY_ID) == [{"institution_id": "ins_126339", "country_codes": ["US"],
                                              "options": {"include_optional_metadata": True}}]
    assert plaid_insts.paths() == [GET_BY_ID]                             # the free lookup only: never an Item
    assert [(o["institution_id"], o["source"]) for o in out] == [("ins_126339", "plaid")]


def test_the_banks_own_icon_when_plaid_has_none(conn, client, plaid_insts):
    bank(conn, "item_chase", "Chase", institution_id="ins_56")
    net = Web({"https://www.chase.com": html('<html><head><link rel="icon" href="/fav16.png" sizes="16x16">'
                                             '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">'
                                             '</head><body>Chase</body></html>'),
               "https://www.chase.com/apple-touch-icon.png": img()})
    logos.refresh_institutions(conn, client, fetch=net)
    r = row(conn, "ins_56")
    assert (r["logo_source"], r["logo_png"], r["primary_color"]) == ("site", PNG, "#095aa6")
    assert net.calls == ["https://www.chase.com", "https://www.chase.com/apple-touch-icon.png"]   # once each


def test_largest_link_icon_then_favicon_ico_and_junk_is_skipped(conn, client, plaid_insts):
    bank(conn, "item_wf", "Wells Fargo", institution_id="ins_127991")
    bank(conn, "item_citi", "Citibank Online", institution_id="ins_5")
    big = logo_png(96)
    net = Web({
        "https://www.wellsfargo.com": html('<link rel="icon" sizes="16x16" href="/s.png">'
                                           '<link rel="shortcut icon" sizes="192x192" href="https://www.wellsfargo.com/big.png">'
                                           '<link rel="icon" sizes="96x96" href="//www.wellsfargo.com/mid.png">'),
        "https://www.wellsfargo.com/big.png": Response(200, "text/html", b"<html>oops</html>", ""),   # junk: skipped
        "https://www.wellsfargo.com/mid.png": img(big),
        "https://www.citi.com": html("<html><head><title>Citi</title></head></html>"),              # no <link> icons
        "https://www.citi.com/favicon.ico": Response(200, "image/x-icon", ico_with_png(PNG), ""),
    })
    logos.refresh_institutions(conn, client, fetch=net)
    assert (row(conn, "ins_127991")["logo_source"], row(conn, "ins_127991")["logo_png"]) == ("site", big)
    assert net.calls[:3] == ["https://www.wellsfargo.com", "https://www.wellsfargo.com/big.png",
                             "https://www.wellsfargo.com/mid.png"]
    citi = row(conn, "ins_5")
    assert citi["logo_source"] == "site" and citi["logo_png"] == PNG      # the PNG inside the .ico
    assert "https://www.citi.com/favicon.ico" in net.calls


def test_initials_with_the_real_colour_when_nothing_is_usable(conn, client, plaid_insts):
    bank(conn, "item_citi", "Citibank Online", institution_id="ins_5")
    bank(conn, "item_tiny", "Tiny Credit Union", institution_id="ins_nourl")
    net = Web()                                                           # the site and /favicon.ico: 404
    logos.refresh_institutions(conn, client, fetch=net)
    citi, tiny = row(conn, "ins_5"), row(conn, "ins_nourl")
    assert (citi["logo_source"], citi["logo_png"], citi["primary_color"]) == ("none", None, "#204081")
    assert (tiny["logo_source"], tiny["logo_png"], tiny["primary_color"]) == ("none", None, "#123456")
    assert not any("tiny" in u for u in net.calls)                        # no url: nothing fetched


def test_a_plaid_logo_that_is_junk_falls_through_to_the_site(conn, client, fake):
    fake.on(GET_BY_ID, {"institution": {"institution_id": "ins_x", "name": "X Bank", "primary_color": "#ffffff",
                                        "logo": base64.b64encode(b"<svg onload=alert(1)>").decode(), "url": "https://x.example"}})
    bank(conn, "item_x", "X Bank", institution_id="ins_x")
    logos.refresh_institutions(conn, client, fetch=Web({"https://x.example/favicon.ico": img()}))
    assert (row(conn, "ins_x")["logo_source"], row(conn, "ins_x")["logo_png"]) == ("site", PNG)


def test_a_plaid_error_is_reported_and_keeps_the_old_row(conn, client, plaid_insts):
    bank(conn, "item_nova", "Nova", institution_id="ins_126339")
    logos.refresh_institutions(conn, client, fetch=Web())
    plaid_insts.on(GET_BY_ID, (500, {"error_type": "API_ERROR", "error_code": "INTERNAL_SERVER_ERROR", "error_message": "x"}))
    out = logos.refresh_institutions(conn, client, fetch=Web())
    assert out[0]["error"] and row(conn, "ins_126339")["logo_png"] == PNG


# --- fetch guards: https only, 5 s, 256 KB, an image type that matches its bytes, no tiny or blank images ---

class FakeResp(io.BytesIO):
    def __init__(self, body, ctype="image/png", url="https://x.example/i.png", status=200, length=None):
        super().__init__(body)
        self.status = status
        self.headers = http.client.HTTPMessage()
        self.headers["Content-Type"] = ctype
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self._url = url
        self.asked = []

    def read(self, n=-1):
        self.asked.append(n)
        return super().read(n)

    def geturl(self):
        return self._url


def test_http_get_guards(monkeypatch):
    seen = {}

    def opener(resp):
        def _open(req, timeout):
            seen["timeout"], seen["url"] = timeout, req.full_url
            return resp
        return _open

    for bad in ("http://www.chase.com/favicon.ico", "ftp://x.example/a.png", "data:image/png;base64,AAAA",
                "file:///etc/passwd", "//x.example/a.png", "javascript:alert(1)"):
        monkeypatch.setattr(logos, "_open", lambda req, timeout: pytest.fail("must not open " + req.full_url))
        with pytest.raises(LogoError):
            logos.http_get(bad)
    monkeypatch.setattr(logos, "_open", opener(FakeResp(PNG)))
    r = logos.http_get("https://x.example/i.png")
    assert (r.status, r.content_type, r.body) == (200, "image/png", PNG) and seen["timeout"] == 5
    with pytest.raises(LogoError, match="256"):                           # declared too big: not even read
        monkeypatch.setattr(logos, "_open", opener(FakeResp(PNG, length=300_000)))
        logos.http_get("https://x.example/i.png")
    big = FakeResp(b"\x89PNG" + b"0" * 300_000)                           # no length header: read is capped
    monkeypatch.setattr(logos, "_open", opener(big))
    with pytest.raises(LogoError, match="256"):
        logos.http_get("https://x.example/i.png")
    assert big.asked == [256 * 1024 + 1]
    monkeypatch.setattr(logos, "_open", opener(FakeResp(PNG, url="http://x.example/i.png")))   # ended up off https
    with pytest.raises(LogoError):
        logos.http_get("https://x.example/i.png")
    for exc in (socket.timeout("timed out"), urllib.error.URLError("dns"), ConnectionRefusedError()):
        def boom(req, timeout, exc=exc):
            raise exc
        monkeypatch.setattr(logos, "_open", boom)
        with pytest.raises(FetchError):
            logos.http_get("https://x.example/i.png")
    def not_found(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", http.client.HTTPMessage(), io.BytesIO(b"no"))
    monkeypatch.setattr(logos, "_open", not_found)
    assert logos.http_get("https://x.example/i.png").status == 404
    with pytest.raises(LogoError):                                         # a redirect may not leave https
        logos.HttpsOnlyRedirect().redirect_request(urllib.request.Request("https://x.example/"), None, 302, "Found",
                                                   http.client.HTTPMessage(), "http://x.example/i.png")


def test_image_checks_reject_junk():
    ok = logos.check_image("image/png", PNG)
    assert ok == "image/png"
    assert logos.check_image("image/x-icon", ico_with_png(PNG)) == "image/x-icon"
    jpeg = (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 64, 64, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\xff\xd9")
    assert logos.check_image("image/jpeg", jpeg) == "image/jpeg"
    with pytest.raises(LogoError):
        logos.check_image("image/jpeg", jpeg.replace(struct.pack(">HH", 64, 64), struct.pack(">HH", 8, 8)))
    junk = [
        ("text/html", PNG),                                               # not an image content type
        ("image/png", b"<html><body>404</body></html>"),                  # says PNG, isn't
        ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>"),   # no SVG, ever
        ("image/png", make_png(1, 1)),                                    # a tracking pixel
        ("image/png", make_png(8, 8, lambda x, y: (20, 40, 160, 255))),   # too small to be a logo
        ("image/png", make_png(64, 64)),                                  # blank white
        ("image/png", make_png(64, 64, lambda x, y: (0, 0, 0, 0))),       # blank transparent
        ("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40),               # truncated
        ("image/png", b""),
        ("application/octet-stream", PNG),
    ]
    for ctype, body in junk:
        with pytest.raises(LogoError):
            logos.check_image(ctype, body)
    with pytest.raises(LogoError):
        logos.check_image("image/png", PNG + b"0" * (256 * 1024))


# --- serving: from the DB only, with cache headers, through the usual gate ---------------------------

@pytest.fixture
def served(conn, client, plaid_insts):
    bank(conn, "item_nova", "Nova", institution_id="ins_126339")
    bank(conn, "item_citi", "Citibank Online", institution_id="ins_5")
    acct(conn, "chk", "item_nova")
    acct(conn, "inv", "item_nova", type_="investment", subtype="brokerage", mask="6820")
    logos.refresh_institutions(conn, client, fetch=Web())
    with conn:
        conn.execute("INSERT INTO holdings (account_id, security_id, item_id, ticker_symbol, security_name, security_type, "
                     "quantity, institution_price, institution_value, updated_at) VALUES "
                     "('inv', 'sec_nvda', 'item_nova', 'NVDA', 'NVIDIA Corp', 'equity', 1, 10, 10, 'now')")
    logos.refresh_security_logos(conn, fetch=Web({FMP.format("NVDA"): img()}), today=date(2026, 9, 26))
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    return app


def test_logo_routes_serve_from_the_db_and_404_otherwise(served):
    status, ctype, body = served.handle("GET", "/api/institutions/ins_126339/logo", LOCAL, b"")
    assert (status, ctype, body) == (200, "image/png", PNG)
    status, ctype, body = served.handle("GET", "/api/securities/NVDA/logo", LOCAL, b"")
    assert (status, ctype, body) == (200, "image/png", PNG)
    for path in ("/api/institutions/ins_999/logo", "/api/institutions/ins_5/logo",          # unknown; initials only
                 "/api/institutions/../logo", "/api/institutions/ins_1/../../logo", "/api/institutions//logo",
                 "/api/institutions/ins_126339/logo/", "/api/institutions/ins_126339", "/api/institutions/%2e%2e/logo",
                 "/api/institutions/" + "ins_" + "a" * 60 + "/logo", "/api/securities/AAPL/logo",
                 "/api/securities/nvda/logo", "/api/securities/../logo", "/api/securities/CUR:USD/logo",
                 "/api/securities//logo", "/api/securities/NVDA/logo.png"):
        status, ctype, _ = served.handle("GET", path, LOCAL, b"")
        assert (status, ctype) == (404, "application/json"), path
    assert served.handle("GET", "/api/institutions/ins_126339/logo", {"Host": "evil.example"}, b"")[0] == 403
    assert served.handle("POST", "/api/institutions/ins_126339/logo", {**LOCAL, "Content-Type": "application/json"},
                         b"{}")[0] == 404


def test_logo_responses_are_cacheable_and_keep_the_security_headers(served):
    srv = make_server(served, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for path, cache in (("/api/institutions/ins_126339/logo", "private, max-age=86400"),
                            ("/api/securities/NVDA/logo", "private, max-age=86400"),
                            ("/api/institutions/ins_999/logo", "no-store"), ("/api/accounts", "no-store")):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{srv.server_address[1]}"})
            r = c.getresponse()
            r.read()
            assert r.getheader("Cache-Control") == cache, path
            assert r.getheader("Content-Security-Policy") == web.CSP and r.getheader("X-Content-Type-Options") == "nosniff"
    finally:
        srv.shutdown()
        srv.server_close()


def test_csp_adds_no_third_party_image_host():
    d = dict(part.strip().split(" ", 1) for part in web.CSP.split(";"))
    assert d["img-src"] == "'self' data: https://plaid-merchant-logos.plaid.com https://plaid-category-icons.plaid.com"
    for host in ("financialmodelingprep", "chase.com", "citi.com", "wellsfargo.com", "novabank.example", "*"):
        assert host not in d["img-src"]
    js = (web.STATIC_DIR / "app.js").read_text()
    assert "financialmodelingprep" not in js and "/api/institutions/" in js and "/api/securities/" in js


def test_accounts_and_investments_payloads_say_which_logos_exist(served, conn):
    with conn:
        conn.execute("UPDATE institutions SET primary_color = 'red;background:url(x)' WHERE institution_id = 'ins_5'")
    d = json.loads(served.handle("GET", "/api/accounts", LOCAL, b"")[2])
    by = {i["institution_id"]: i for i in d["institutions"]}
    assert by["ins_126339"]["logo"] is True and by["ins_126339"]["color"] == "#00a9ce" and by["ins_126339"]["v"]
    assert by["ins_5"]["logo"] is False and by["ins_5"]["color"] is None          # a bad colour never reaches CSS
    assert by["ins_126339"]["name"] == "Nova" and "logo_png" not in by["ins_126339"]
    assert {b["institution_id"] for b in d["net_worth"]["institutions"]} == {"ins_126339"}
    inv = portfolio.investments(conn, date(2026, 9, 26))
    assert [(h["ticker"], h["logo"]) for h in inv["holdings"]] == [("NVDA", True)]
    tx = json.loads(served.handle("GET", "/api/transactions", LOCAL, b"")[2])
    assert "institution_id" in json.dumps(tx) or tx["rows"] == []


# --- connect time and the commands --------------------------------------------------------------------

def test_connecting_a_bank_fills_its_logo(conn, client, plaid_insts, monkeypatch):
    plaid_insts.on("/item/public_token/exchange", {"access_token": TOKEN_NEW, "item_id": "item_new"})
    monkeypatch.setattr(logos, "http_get", Web())
    out = link.exchange(conn, client, "public-production-x", {"institution": {"institution_id": "ins_126339", "name": "Nova"}})
    assert out["sync_ok"] and row(conn, "ins_126339")["logo_source"] == "plaid"
    assert "/link/token/create" not in plaid_insts.paths()


def test_a_logo_failure_never_fails_the_connection(conn, client, fake, monkeypatch):
    fake.on("/item/public_token/exchange", {"access_token": TOKEN_NEW, "item_id": "item_new"})
    fake.on(GET_BY_ID, (500, {"error_type": "API_ERROR", "error_code": "INTERNAL_SERVER_ERROR", "error_message": "x"}))
    out = link.exchange(conn, client, "public-production-x", {"institution": {"institution_id": "ins_56", "name": "Chase"}})
    assert out["sync_ok"] and out["item_id"] == "item_new" and row(conn, "ins_56") is None


def test_refresh_institutions_command(cli_client, conn, plaid_insts, monkeypatch, capsys):
    bank(conn, "item_nova", "Nova", institution_id="ins_126339")
    bank(conn, "item_citi", "Citibank Online", institution_id="ins_5")
    monkeypatch.setattr(logos, "http_get", Web())
    assert cli.main(["refresh-institutions"]) == 0
    out = capsys.readouterr().out
    assert "Nova" in out and "plaid logo" in out and "Citibank Online" in out and "initials" in out
    assert plaid_insts.paths() == [GET_BY_ID, GET_BY_ID]


def test_refresh_logos_command_covers_banks_and_holdings(cli_client, conn, plaid_insts, monkeypatch, capsys):
    bank(conn, "item_nova", "Nova", institution_id="ins_126339")
    acct(conn, "inv", "item_nova", type_="investment", subtype="brokerage")
    with conn:
        conn.execute("INSERT INTO holdings (account_id, security_id, item_id, ticker_symbol, security_name, security_type, "
                     "updated_at) VALUES ('inv', 's1', 'item_nova', 'VTI', 'Vanguard Total', 'etf', 'now')")
    net = Web({FMP.format("VTI"): img()})
    monkeypatch.setattr(logos, "http_get", net)
    assert cli.main(["refresh-logos"]) == 0
    out = capsys.readouterr().out
    assert "Nova" in out and "VTI" in out
    assert conn.execute("SELECT source FROM security_logos WHERE ticker = 'VTI'").fetchone()[0] == "fmp"
    assert row(conn, "ins_126339")["logo_source"] == "plaid"


# --- holding logos ------------------------------------------------------------------------------------

def holding(conn, sec, ticker, typ, item="item_a", account_id="inv"):
    with conn:
        conn.execute("INSERT INTO holdings (account_id, security_id, item_id, ticker_symbol, security_name, security_type, "
                     "quantity, institution_price, institution_value, updated_at) VALUES (?,?,?,?,?,?,1,1,1,'now')",
                     (account_id, sec, item, ticker, f"{ticker} name", typ))


@pytest.fixture
def portfolio_db(conn):
    bank(conn)
    acct(conn, "inv", type_="investment", subtype="brokerage")
    for sec, ticker, typ in (("s1", "NVDA", "equity"), ("s2", "CUR:USD", "cash"), ("s3", "BTC", "cryptocurrency"),
                             ("s4", None, "equity"), ("s5", "VTI", "etf"), ("s6", "", "etf"), ("s7", "WEIRD$", "equity"),
                             ("s8", "XYZ", None)):
        holding(conn, sec, ticker, typ)
    return conn


def logo_rows(conn):
    return {r["ticker"]: (r["source"], r["png"] is not None) for r in conn.execute("SELECT * FROM security_logos")}


def test_holding_logos_fetch_stocks_and_etfs_only(portfolio_db):
    net = Web({FMP.format("NVDA"): img(), FMP.format("VTI"): img()})
    out = logos.refresh_security_logos(portfolio_db, fetch=net, today=date(2026, 9, 26))
    assert net.calls == [FMP.format("NVDA"), FMP.format("VTI")]          # no cash, crypto, empty or odd tickers
    assert logo_rows(portfolio_db) == {"NVDA": ("fmp", True), "VTI": ("fmp", True)}
    assert out["found"] == ["NVDA", "VTI"] and out["missed"] == []


@pytest.mark.parametrize("answer", [
    Response(404, "text/html", b"nope", ""),
    Response(200, "text/html", b"<html>placeholder</html>", ""),                   # not an image
    Response(200, "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * (256 * 1024), ""),    # over 256 KB
    Response(200, "image/png", make_png(64, 64), ""),                              # blank placeholder
    Response(200, "image/png", make_png(2, 2, lambda x, y: (9, 9, 9, 255)), ""),   # tiny
])
def test_a_miss_is_remembered_for_30_days(portfolio_db, answer):
    net = Web({FMP.format("NVDA"): answer, FMP.format("VTI"): img()})
    out = logos.refresh_security_logos(portfolio_db, fetch=net, today=date(2026, 9, 26))
    assert logo_rows(portfolio_db) == {"NVDA": ("none", False), "VTI": ("fmp", True)} and out["missed"] == ["NVDA"]
    net.calls.clear()
    logos.refresh_security_logos(portfolio_db, fetch=net, today=date(2026, 10, 20))
    assert net.calls == []                                                # cached hit and a recent miss: no calls
    logos.refresh_security_logos(portfolio_db, fetch=net, today=date(2026, 10, 27))
    assert net.calls == [FMP.format("NVDA")]                              # 31 days later: the miss is retried
    net.calls.clear()
    logos.refresh_security_logos(portfolio_db, fetch=net, today=date(2026, 10, 27), force=True)
    assert net.calls == [FMP.format("NVDA"), FMP.format("VTI")]


def test_network_trouble_is_not_a_miss_and_stops_the_run(portfolio_db):
    net = Web({FMP.format("NVDA"): FetchError("timed out"), FMP.format("VTI"): img()})
    out = logos.refresh_security_logos(portfolio_db, fetch=net, today=date(2026, 9, 26))
    assert logo_rows(portfolio_db) == {} and net.calls == [FMP.format("NVDA")] and out["error"]


def test_sync_fills_holding_logos_and_a_failure_is_not_fatal(conn, client, fake, monkeypatch):
    add_item(conn, products=("transactions", "investments"))
    fake.on("/item/get", {"item": {"item_id": "item_a", "institution_id": "ins_1",
                                   "products": ["transactions", "investments"]}})
    fake.on("/investments/holdings/get", {
        "accounts": [account("acc_inv", "Nova Invest", "6820", "investment", "brokerage", 490.0, None)],
        "holdings": [{"account_id": "acc_inv", "security_id": "sec_nvda", "quantity": 2, "institution_price": 245,
                      "institution_value": 490.0}],
        "securities": [{"security_id": "sec_nvda", "ticker_symbol": "NVDA", "name": "NVIDIA Corp", "type": "equity"}]})
    monkeypatch.setattr(logos, "http_get", Web({FMP.format("NVDA"): img()}))
    (r,) = sync_all(conn, client)
    assert r.ok and r.extras["investments"] == "ok"
    assert logo_rows(conn) == {"NVDA": ("fmp", True)}

    def broken(url):
        raise RuntimeError("logo service exploded")
    with conn:
        conn.execute("DELETE FROM security_logos")
    monkeypatch.setattr(logos, "http_get", broken)
    (r,) = sync_all(conn, client)
    assert r.ok and logo_rows(conn) == {}


def test_holdings_fall_back_to_letters_and_cash_keeps_its_badge():
    js = (web.STATIC_DIR / "app.js").read_text()
    body = js[js.index("function holdingsCard("):js.index("// ---------- Loans & debt")]
    assert "x.logo" in body and "/api/securities/" in body                  # a cached logo: the image
    assert ".slice(0, 2)" in body                                           # otherwise: the letters bubble
    assert "icon('dollar')" in body or "glyph('dollar')" in body            # cash: the $ badge
