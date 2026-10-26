"""Shared fixtures. Every test runs against a mocked Plaid HTTP layer; real network is blocked
(except tests marked `live`)."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import tempfile
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

# A single-query worker flag must not leak into this session or any child process.
os.environ.pop("HERMES_SINGLE_QUERY_SESSION", None)

# No test ever reads the real ~/.hermes/.env (Plaid credentials, valuation keys): unless a test points HERMES_ENV_FILE
# somewhere itself (the data_dir fixture does), it names a file that doesn't exist, so every key is absent.
NO_ENV_FILE = Path(tempfile.gettempdir()) / "hermes-finance-tests-no-such.env"
os.environ["HERMES_ENV_FILE"] = str(NO_ENV_FILE)
# Nor the real secrets file (TASK-18: ~/.config/hermes-finance/secrets.env): a test that writes keys gets its own
# (the data_dir fixture's); every other one, and every process a test starts, names a file that doesn't exist.
NO_SECRETS_FILE = Path(tempfile.gettempdir()) / "hermes-finance-tests-no-such-secrets.env"
os.environ["FINANCE_SECRETS_FILE"] = str(NO_SECRETS_FILE)
# Hermes is "not installed" unless a test says otherwise (admin.hermes_detected only checks a directory exists)
os.environ["HERMES_HOME"] = str(Path(tempfile.gettempdir()) / "hermes-finance-tests-no-hermes-home")
# No test, and no process a test starts (they inherit this), can reach the real Chrome on 127.0.0.1:9222: the DevTools
# client is switched off (cdp_client never connects). A CDP test points a Chrome() at its own headless Chrome by port.
os.environ["HERMES_CHROME_DEVTOOLS"] = "off"
# TASK-19: no test, and no process a test starts, reads the real Alpaca key file (~/.config/hermes-finance/alpaca-live.env)
# or reaches Alpaca: the key file is a path that doesn't exist, and every Alpaca request (and its logo fetch) is off.
# An Alpaca test points HERMES_ALPACA_KEY_FILE at its own temp file and lifts the switch with a mocked transport.
NO_ALPACA_KEYS = Path(tempfile.gettempdir()) / "hermes-finance-tests-no-alpaca.env"
os.environ["HERMES_ALPACA_KEY_FILE"] = str(NO_ALPACA_KEYS)
os.environ["HERMES_ALPACA"] = "off"
# TASK-22: nor Plaid (Sync now and finance_sync reach it from the app, the plugin and `finance mcp`): the real transport
# refuses while this is set, in this process and every one a test starts. Tests hand PlaidClient a FakePlaid.
os.environ["HERMES_PLAID"] = "off"
# TASK-23: nor Hermes's API server (the Ask panel's default brain, 127.0.0.1:8642: loopback, so the socket guard below
# lets it through): every process names a closed port instead. A Hermes-mode test points it at its own fake server.
os.environ["FINANCE_HERMES_URL"] = "http://127.0.0.1:9/v1"
# Nor the real Hermes through the Ask bridge (bridge.executable): the hermes executable is a path that doesn't exist, in
# every process a test starts too. A bridge test points FINANCE_HERMES_BIN at its own stub or sandboxed runtime.
os.environ["FINANCE_HERMES_BIN"] = str(Path(tempfile.gettempdir()) / "hermes-finance-tests-no-hermes-bin")

from hermes_finance import alpaca, cdp_client, config, db, photos, valuation  # noqa: E402
from hermes_finance.config import Settings  # noqa: E402
from hermes_finance.plaid import PlaidClient  # noqa: E402

# No private word in any test report (CI's log is public): see tests/redact.py.
import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import pytest_make_collect_report, pytest_runtest_makereport  # noqa: E402,F401

FAKE_CLIENT_ID = "fakeclientid000000000001"
FAKE_SECRET = "fakesecret0000000000000000abcd"
TOKEN_A = "access-production-aaaaaaaa-1111-2222-3333-444444444444"
TOKEN_B = "access-production-bbbbbbbb-1111-2222-3333-444444444444"
TOKEN_NEW = "access-production-cccccccc-1111-2222-3333-444444444444"


# What production runs with (before any test patches it): Carfax and Redfin on.
PRODUCTION_FREE_SOURCES_DEFAULT = dict(config.FREE_SOURCES_DEFAULT)
REAL_CHROME_PORT = cdp_client.PORT          # the always-on Hermes Chrome: never reached from a test


def _real_chrome(address) -> bool:
    return isinstance(address, tuple) and len(address) >= 2 and address[1] == REAL_CHROME_PORT


SCHEDULED: list[int] = []   # accounts whose picture a test's page view / save asked for


def _no_cutter(data):
    raise AssertionError("a test reached the real background remover (set photos.cutter)")


def _loopback(address) -> bool:
    if not isinstance(address, tuple):          # AF_UNIX: a path
        return True
    host = str(address[0])
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:                          # any other host name would be resolved and reached: refuse
        return False


_REAL_CONNECT, _REAL_CONNECT_EX, _REAL_GETADDRINFO = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo


@pytest.fixture(autouse=True)
def hermes_credentials_reset():
    """Loading the Hermes plugin (register()) makes the app take its keys from Hermes, process-wide: undone after
    every test, so it never reaches the next one."""
    yield
    from hermes_finance import config
    config.use_hermes_credentials(None)


@pytest.fixture(autouse=True)
def environment_requirements(request):
    marker = request.node.get_closest_marker("env_dependent")
    if marker is not None:
        import hermes_runtime
        reason = hermes_runtime.environment_reason(marker.args[0] if marker.args else "runtime")
        if reason:
            pytest.skip(f"env_dependent: {reason}")


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Every test (but `live` ones, deselected by default) runs with the network off: urllib's urlopen and every opener
    (logos.py, valuation.py) raise, valuation.transport raises until a test installs a FakeHttp, and below all of that
    a socket may only connect to (and a name may only resolve to) loopback: the test servers, headless Chrome's
    DevTools, or a Unix socket."""
    if request.node.get_closest_marker("live"):
        monkeypatch.delenv("HERMES_PLAID", raising=False)
        return

    def blocked(*args, **kwargs):
        raise AssertionError("real network call attempted from a mocked test")

    def connect(sock, address):
        if not _loopback(address):
            raise AssertionError(f"real network connection attempted from a test: {address!r}")
        if _real_chrome(address):
            raise AssertionError(f"the real Chrome (DevTools on port {REAL_CHROME_PORT}) reached from a test: {address!r}")
        return _REAL_CONNECT(sock, address)

    def connect_ex(sock, address):
        if not _loopback(address):
            raise AssertionError(f"real network connection attempted from a test: {address!r}")
        if _real_chrome(address):
            raise AssertionError(f"the real Chrome (DevTools on port {REAL_CHROME_PORT}) reached from a test: {address!r}")
        return _REAL_CONNECT_EX(sock, address)

    def getaddrinfo(host, *args, **kwargs):  # no DNS lookup of an outside name either
        if host is not None and not _loopback((host.decode() if isinstance(host, bytes) else host, 0)):
            raise AssertionError(f"real network lookup attempted from a test: {host!r}")
        return _REAL_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", blocked)  # custom openers too (logos.py, valuation.py)
    monkeypatch.setattr(valuation, "transport", lambda *a, **k: blocked())  # a valuation test sets a FakeHttp
    monkeypatch.setattr(valuation, "browser", OfflineBrowser())            # a Carfax / Redfin test sets a FakeBrowser
    # Carfax, Redfin and the photos are off unless a test turns them on (the `free_sources` fixture writes =1 in the env
    # file); production has them on (PRODUCTION_FREE_SOURCES_DEFAULT, checked in a fresh process).
    monkeypatch.setattr(config, "FREE_SOURCES_DEFAULT", {"carfax": False, "redfin": False, "photos": False})
    # the background photo fetch never starts a thread in a test (SCHEDULED records the ask); no real pause, no rembg
    SCHEDULED.clear()
    monkeypatch.setattr(photos, "schedule", lambda db_path, account_id: SCHEDULED.append(account_id))
    monkeypatch.setattr(photos, "sleep", lambda seconds: None)
    monkeypatch.setattr(photos, "cutter", _no_cutter)
    monkeypatch.setenv("HERMES_ENV_FILE", str(NO_ENV_FILE))
    monkeypatch.setenv("FINANCE_SECRETS_FILE", str(NO_SECRETS_FILE))
    monkeypatch.setenv("HERMES_CHROME_DEVTOOLS", "off")
    monkeypatch.setenv("HERMES_ALPACA_KEY_FILE", str(NO_ALPACA_KEYS))
    monkeypatch.setenv("HERMES_ALPACA", "off")
    monkeypatch.setenv("HERMES_PLAID", "off")
    monkeypatch.setattr(alpaca, "transport", lambda *a, **k: blocked())  # an Alpaca test sets its own
    assert not NO_ENV_FILE.exists() and not NO_SECRETS_FILE.exists() and not NO_ALPACA_KEYS.exists()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("FINANCE_DATA_DIR", str(d))
    env = tmp_path / "hermes.env"
    env.write_text(f"PLAID_CLIENT_ID={FAKE_CLIENT_ID}\nPLAID_SECRET={FAKE_SECRET}\nPLAID_ENV=production\n")
    monkeypatch.setenv("HERMES_ENV_FILE", str(env))
    monkeypatch.setenv("FINANCE_SECRETS_FILE", str(tmp_path / "secrets" / "secrets.env"))  # written only by a test
    return d


@pytest.fixture
def conn(data_dir):
    c = db.connect()
    yield c
    c.close()


class FakePlaid:
    """Mocked Plaid HTTP transport. Route handlers get the request body (minus credentials) and
    return a dict (HTTP 200) or a (status, dict) tuple. Every call is recorded."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.routes: dict = {}

    def on(self, path, handler):
        self.routes[path] = handler
        return self

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    def bodies(self, path) -> list[dict]:
        return [b for p, b in self.calls if p == path]

    def __call__(self, url, data, headers, timeout):
        path = urlparse(url).path
        body = json.loads(data)
        assert url.startswith("https://production.plaid.com/")
        assert body.pop("secret") == FAKE_SECRET and body.pop("client_id") == FAKE_CLIENT_ID
        assert headers["Plaid-Version"]
        self.calls.append((path, body))
        handler = self.routes.get(path)
        if handler is None:
            return 400, json.dumps(err("UNMOCKED_ENDPOINT", "INVALID_REQUEST")[1]).encode()
        resp = handler(body) if callable(handler) else handler
        status, payload = resp if isinstance(resp, tuple) else (200, resp)
        return status, json.dumps(payload).encode()


def err(code, error_type="ITEM_ERROR", status=400):
    return status, {"error_type": error_type, "error_code": code, "error_message": f"mock {code}",
                    "display_message": None, "request_id": "req-mock"}


def account(account_id="acc_chk", name="Checking", mask="1234", type_="depository", subtype="checking",
            current=1000.0, available=900.0, limit=None):
    return {"account_id": account_id, "name": name, "official_name": None, "mask": mask, "type": type_,
            "subtype": subtype, "balances": {"current": current, "available": available, "limit": limit,
                                             "iso_currency_code": "USD"}}


def txn(tid, amount=10.0, name="BLUE BOTTLE COFFEE", merchant="Blue Bottle", date="2026-09-01",
        pfc=("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"), account_id="acc_chk", pending=False, pending_id=None):
    return {"transaction_id": tid, "account_id": account_id, "amount": amount, "iso_currency_code": "USD",
            "date": date, "authorized_date": date, "name": name, "merchant_name": merchant,
            "pending": pending, "pending_transaction_id": pending_id, "payment_channel": "in store",
            "personal_finance_category": {"primary": pfc[0], "detailed": pfc[1], "confidence_level": "HIGH"},
            "logo_url": None, "website": None, "location": {"city": "SF"}, "counterparties": []}


def page(added=(), modified=(), removed=(), next_cursor="c1", has_more=False, accounts=None):
    return {"added": list(added), "modified": list(modified),
            "removed": [{"transaction_id": r, "account_id": "acc_chk"} for r in removed],
            "next_cursor": next_cursor, "has_more": has_more,
            "accounts": accounts if accounts is not None else [account()],
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE", "request_id": "req"}


def add_item(conn, item_id="item_a", institution_id="ins_1", name="Chase", token=TOKEN_A,
             removed=False, status="active", cursor=None, products=("transactions",)):
    with conn:
        conn.execute(
            """INSERT INTO items (item_id, institution_id, institution_name, access_token, cursor, status,
                   products, created_at, removed_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (item_id, institution_id, name, token, cursor, status, json.dumps(list(products)),
             db.now(), db.now() if removed else None))


def item_row(conn, item_id="item_a"):
    return conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()


@pytest.fixture
def fake():
    f = FakePlaid()
    f.on("/item/get", lambda b: {"item": {"item_id": "x", "institution_id": "ins_1",
                                          "products": ["transactions"]}})
    f.on("/accounts/get", {"accounts": [account()]})
    f.on("/transactions/sync", page())
    return f


@pytest.fixture
def client(fake):
    return PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=fake)


@pytest.fixture
def cli_client(client, monkeypatch):
    """Make `finance` CLI commands use the mocked client."""
    from hermes_finance import cli
    monkeypatch.setattr(cli, "_client", lambda: client)
    return client


# --- valuation providers: response shapes as recorded in the TASK-12 / TASK-13 briefs ------------------------------

VIN = "1HGCM82633A004352"          # a valid check digit (the 9th character, 3)
NHTSA_OK = {"Count": 136, "Message": "Results returned successfully. NOTE: Any missing decoded values should be "
            "interpreted as NHTSA does not have data on the specific variable.", "SearchCriteria": f"VIN:{VIN}",
            "Results": [{"ModelYear": "2003", "Make": "HONDA", "Model": "Accord", "Trim": "EX-V6", "Series": "",
                         "ErrorCode": "0", "ErrorText": "0 - VIN decoded clean. Check Digit (9th position) is correct",
                         "VIN": VIN}]}


class FakeHttp:
    """valuation.transport stand-in: route by host to a handler returning (status, dict|bytes) or raising
    TimeoutError / OSError. Records every call (url, headers)."""

    HOSTS = {"vpic.nhtsa.dot.gov": "nhtsa", "www.redfin.com": "redfin", "ssl.cdn-redfin.com": "redfin_photo",
             "commons.wikimedia.org": "commons", "upload.wikimedia.org": "commons_file",
             "tile.openstreetmap.org": "osm"}

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.routes = {"nhtsa": (200, NHTSA_OK), **{p: AssertionError(f"no {p} answer set for this test")
                                                   for p in ("redfin", "redfin_photo", "commons", "commons_file", "osm")}}

    def on(self, provider, response):
        self.routes[provider] = response
        return self

    def count(self, provider=None):
        return sum(1 for u, _ in self.calls if provider is None or self.HOSTS[urlparse(u).hostname] == provider)

    def params(self, i=-1):
        return {k: v[0] for k, v in parse_qs(urlparse(self.calls[i][0]).query).items()}

    def __call__(self, url, headers, timeout, max_bytes=None):
        host = urlparse(url).hostname
        assert url.startswith("https://") and host in self.HOSTS, url
        self.calls.append((url, dict(headers)))
        resp = self.routes[self.HOSTS[host]]
        if callable(resp):
            resp = resp(url, headers)
        if isinstance(resp, BaseException):
            raise resp
        status, body = resp
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()


@pytest.fixture
def fake_http(monkeypatch):
    f = FakeHttp()
    monkeypatch.setattr(valuation, "transport", f)
    return f


@pytest.fixture
def free_sources(data_dir, tmp_path):
    """Carfax, Redfin and the photos switched on in the (temporary) env file, as they are by default in production."""
    env = tmp_path / "hermes.env"
    env.write_text(env.read_text() + "CARFAX_ENABLED=1\nREDFIN_ENABLED=1\nPHOTOS_ENABLED=1\n")
    return env


# --- TASK-13: the local Chrome (valuation.browser) ------------------------------------------------------------------

class OfflineBrowser:
    """The default in every test: Chrome "isn't running", and opening a tab is a test failure (the real one on :9222 is
    refused at the socket level too)."""

    def available(self):
        return "Chrome not running (tests have no browser)"

    def tab(self, url):
        raise AssertionError(f"a test tried to open a real Chrome tab on {url}")


# Carfax, exactly as recorded from carfax.com/value on 2026-09-26 (TASK-13 brief). The brief elided the XLE / LE trims'
# options ("..."); they're filled in here with the same shape.
CARFAX_VIN = "SYNTHCARXL3012345"
_colour = lambda cid, name, default: {"componentId": cid, "component": name, "featureCategory": "EXTERIOR COLOR",  # noqa: E731
                                      "standardFrequency": "O", "componentDefault": default}
CARFAX_TRIMS = {"condition": "excellent", "make": "ZENITH", "model": "COMET PRIME", "numberOfRecords": 25,
                "odometer": 143210,
                "trimOptions": [
                    {"trim": "LIMITED", "subtrim": "", "trimDisplay": "LIMITED", "trimDefault": True,
                     "options": [_colour(1085, "Black", True), _colour(1086, "Blue Magnetism", False),
                                 {"componentId": 2001, "component": "Advanced Technology Package",
                                  "featureCategory": "PACKAGE", "standardFrequency": "O", "componentDefault": False}]},
                    {"trim": "XLE", "subtrim": "", "trimDisplay": "XLE", "trimDefault": False,
                     "options": [_colour(1185, "Classic Silver Metallic", True), _colour(1186, "Black", False)]},
                    {"trim": "LE", "subtrim": "", "trimDisplay": "LE", "trimDefault": False,
                     "options": [_colour(1285, "Wind Chill Pearl", True)]}],
                "year": 2020}
CARFAX_PRICES = {"atomInfo": {"bodyType": "4 DOOR HATCHBACK", "driveType": "FWD",
                              "engineName": "1.8L Plug-in Hybrid I4 121hp", "engineCylinders": 4, "engineSize": 1.8,
                              "fuelType": "PLUG-IN GAS/ELECTRIC HYBRID", "transType": "CONTINUOUSLY VARIABLE",
                              "segmentationDesc": "COMPACT CAR", "trim": "LIMITED", "year": 2020},
                 "displayStatus": "Displayable",
                 "vehiclePrice": {"consumerRetailPrice": 19360, "fsboPrice": 17160, "wholesalePrice": 14090}}
# DataDome's answer to a repeat call (a 403 with a captcha page)
CARFAX_CAPTCHA = (403, '<html><head><title>carfax.com</title></head><body><script>var dd={"rt":"c","cid":"x",'
                       '"host":"geo.captcha-delivery.com"}</script><script src="https://ct.captcha-delivery.com/c.js">'
                       '</script></body></html>')


class FakeTab:
    def __init__(self, browser, url):
        self.browser, self.url = browser, url

    def fetch(self, url, method="GET", headers=None, body=None, timeout=20.0):
        self.browser.fetches.append({"page": self.url, "method": method, "url": url, "headers": dict(headers or {}),
                                     "body": body})
        host, path = urlparse(url).hostname, urlparse(url).path
        resp = None
        for (m, prefix), r in self.browser.routes.items():
            if m == method and (host + path).startswith(prefix):
                resp = r
                break
        if resp is None:
            raise AssertionError(f"no FakeBrowser answer for {method} {url}")
        if callable(resp):
            resp = resp(url, method, headers, body)
        if isinstance(resp, BaseException):
            raise resp
        status, payload = resp
        return status, payload if isinstance(payload, str) else json.dumps(payload)


class FakeBrowser:
    """valuation.browser stand-in: available() is `down` (None = Chrome is there); tab(url) records the tab it opens
    and closes (closed on the way out whatever happens), and its fetch() answers from `routes`
    {(method, "host/path prefix"): (status, dict|str) | exception | callable}."""

    def __init__(self):
        self.down = None
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.fetches: list[dict] = []
        self.open_error = None
        self.routes = {("GET", "consumerapi.carfax.com/hbv/cow/trims/"): (200, CARFAX_TRIMS),
                       ("POST", "consumerapi.carfax.com/hbv/cow/prices"): (200, CARFAX_PRICES)}

    def on(self, method, prefix, response):
        """Answer `method` requests under `prefix` with `response` (checked before the other routes)."""
        rest = {k: v for k, v in self.routes.items() if k != (method, prefix)}
        self.routes = {(method, prefix): response, **rest}
        return self

    def available(self):
        return self.down

    @contextmanager
    def tab(self, url):
        if self.open_error:
            raise self.open_error
        self.opened.append(url)
        try:
            yield FakeTab(self, url)
        finally:
            self.closed.append(url)


@pytest.fixture
def fake_browser(monkeypatch):
    b = FakeBrowser()
    monkeypatch.setattr(valuation, "browser", b)
    return b


# --- Redfin (TASK-13 recorded shapes; the photo from TASK-13b) ------------------------------------------------------

def stingray(payload) -> bytes:
    return ("{}&&" + json.dumps({"version": 1, "errorMessage": "Success", "resultCode": 0, "payload": payload})).encode()


def redfin_row(pid, name, sub="Santa Rosa, CA, USA", type_="1"):
    return {"id": f"{type_}_{pid}", "type": type_, "name": name, "subName": sub,
            "url": f"/CA/Santa-Rosa/{name.replace(' ', '-')}/home/{pid}", "active": True}


ALDER = "1420 Alder Ave, Santa Rosa, CA 95405"
ALDER_PHOTO = "https://ssl.cdn-redfin.com/photo/40/bigphoto/975/31415975_0.jpg"
# aboveTheFold for 1420 Alder (the shape measured on 2026-09-26, trimmed): the listing photo on Redfin's CDN, and a Google
# Street View URL (with Redfin's key) that must never be used
ALDER_ABOVE = {"mediaBrowserInfo": {"photos": [{"photoUrls": {"fullScreenPhotoUrl": ALDER_PHOTO,
                                                               "nonFullScreenPhotoUrl": ALDER_PHOTO.replace("bigphoto", "mbpaddedwide")}}],
                                     "streetView": {"streetViewUrl": "https://maps.googleapis.com/maps/api/streetview?key=REDFINKEY"}},
                "addressSectionInfo": {"latLong": {"latitude": 38.4404091, "longitude": -122.7144731}}}
ALDER_AVM = {"predictedValue": 1014415.33, "sectionPreviewText": "$1,014,415 (+$194K since last sold)", "numBeds": 3,
              "numBaths": 2.0, "sqFt": {"display": "1,560", "value": 1560},
              "priceInfo": {"amount": 820000, "label": "Last Sold Price"},
              "latLong": {"latitude": 38.4411, "longitude": -122.7193},
              "streetAddress": {"assembledAddress": "1420 Alder Ave"}}
# TASK-13 2b, measured on 4120 Birch Ave (a condo building): "#303" / "Unit 303" only find a free-text row; the
# building search finds the building record, units 301 / 201 / 206 (no estimate) and "Unit 4120-301"
BIRCH_FREE_TEXT = {"sections": [{"rows": [{"id": "23_null", "type": "23", "name": "4120 Birch Ave #303",
                                              "subName": "Search for this address"}]}]}
BIRCH_BUILDING = {"sections": [{"name": "Addresses", "rows": [
    redfin_row(300004120, "4120 Birch Ave"), redfin_row(201000301, "4120 Birch Ave #301"), redfin_row(201000201, "4120 Birch Ave #201"),
    redfin_row(201000206, "4120 Birch Ave #206"), redfin_row(300004301, "4120 Birch Ave Unit 4120-301")]}]}
NOT_AVAILABLE = {"sectionPreviewText": "Estimate Not Available", "numBeds": 2, "numBaths": 2.0}
BIRCH_AVM = {"300004120": {"predictedValue": 1031933.0, "sectionPreviewText": "$1,031,933"},
               "300004301": {"predictedValue": 579436.0, "sectionPreviewText": "$579,436"},
               "201000301": NOT_AVAILABLE, "201000201": NOT_AVAILABLE, "201000206": NOT_AVAILABLE}


class Redfin:
    """Redfin's endpoints (either transport): autocomplete by the query's building (units go to a free-text row, as
    measured), the AVM and aboveTheFold (the listing photo) by propertyId. Records what was asked."""

    def __init__(self, homes=None, avm=None, above=None):
        self.homes = homes or {"1420 alder ave": {"sections": [{"rows": [redfin_row(5555501, "1420 Alder Ave")]}]},
                               "4120 birch ave": BIRCH_BUILDING}
        self.avm = avm or {"5555501": ALDER_AVM, **BIRCH_AVM}
        self.above = {"5555501": ALDER_ABOVE} if above is None else above
        self.asked: list[str] = []

    def answer(self, url):
        u = urlparse(url)
        q = parse_qs(u.query)
        if u.path == "/stingray/do/location-autocomplete":
            text = q["location"][0]
            self.asked.append(f"autocomplete {text}")
            a = valuation.parse_address(text)
            if a.unit:
                return 200, stingray(BIRCH_FREE_TEXT)
            key = a.building.split(",")[0].casefold()
            return 200, stingray(self.homes.get(key, {"sections": []}))
        if u.path == "/stingray/api/home/details/avm":
            pid = q["propertyId"][0]
            self.asked.append(f"avm {pid}")
            assert q["accessLevel"] == ["1"]
            return 200, stingray(self.avm[pid])
        if u.path == "/stingray/api/home/details/aboveTheFold":
            pid = q["propertyId"][0]
            self.asked.append(f"above {pid}")
            return 200, stingray(self.above.get(pid, {"mediaBrowserInfo": {"photos": []}}))
        raise AssertionError(url)

    def http(self, url, headers):
        return self.answer(url)

    def tab(self, url, method, headers, body):
        status, raw = self.answer(url)
        return status, raw.decode()


