"""`finance serve`: the dashboard + Plaid Link, on a stdlib http.server bound to loopback.

The access token never leaves the server: no endpoint or page ever includes it.

Who may open it is one of three access modes (access.py, TASK-18). "This computer only" and "Behind your proxy login"
only ever bind loopback. Other devices reach the proxy mode through e.g. `tailscale serve`, which proxies from
127.0.0.1 with the public Host and a `Tailscale-User-Login` identity header (Serve strips client-sent copies; the
header's name is a setting). Requests for a configured public host must carry an allowed identity. Serve forwards the
client's Host unchanged, so a request with proxy headers is never treated as localhost. "Built-in login" needs a
session (auth.py) on every request but the sign-in page, and a bearer token on /mcp.
"""

from __future__ import annotations

import html
import json
import logging
import re
import shutil
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit

from . import access, admin, api, auth, config, db, link, logos, manual, photos, prefs, redact, report, syncnow
from . import features  # TASK-19 (Batch C): goals, splits / notes / tags / saved filters, bills calendar, rollover, Alpaca
from . import chat      # TASK-23: the Ask panel (/api/chat*)
from .plaid import PlaidClient, PlaidError

log = logging.getLogger(__name__)

MAX_BODY = 1_000_000
MAX_IMPORT_BODY = 15_000_000  # /api/import/*: a 10 MB file, base64 in JSON
JSON = "application/json"
HTML = "text/html; charset=utf-8"
PLAID_LINK_JS = "https://cdn.plaid.com/link/v2/stable/link-initialize.js"
# Sent on every response. Plaid Link's documented needs (plaid.com/docs/link/web): cdn.plaid.com for the
# script, frame and default-src; production.plaid.com for connect-src (covered by *.plaid.com);
# 'unsafe-inline' styles. Its 'unsafe-inline' script-src is only for an inline init snippet, which this
# page doesn't have (all JS is in /static/app.js), so it's left out.
CSP = "; ".join([
    "default-src 'self' https://cdn.plaid.com",
    "script-src 'self' https://cdn.plaid.com",
    "frame-src https://cdn.plaid.com",
    "connect-src 'self' https://*.plaid.com",
    "img-src 'self' data: https://plaid-merchant-logos.plaid.com https://plaid-category-icons.plaid.com",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self'",
    "manifest-src 'self'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
])
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
STATIC_DIR = Path(__file__).resolve().parent / "static"
# GET /static/<name> serves exactly these files (no path handling at all, so no traversal).
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    # TASK-18: the theme before first paint, the Settings / first-run / import / sign-in screens, the light theme
    "theme.js": "text/javascript; charset=utf-8",
    "settings.js": "text/javascript; charset=utf-8",
    "setup.js": "text/javascript; charset=utf-8",
    "import.js": "text/javascript; charset=utf-8",
    "login.js": "text/javascript; charset=utf-8",
    "settings.css": "text/css; charset=utf-8",
    "light.css": "text/css; charset=utf-8",
    "tap.css": "text/css; charset=utf-8",      # TASK-20: 44px tap targets on a phone
    "vendor/chart.umd.min.js": "text/javascript; charset=utf-8",
    "fonts/InterVariable.woff2": "font/woff2",
    "fonts/Inter-LICENSE.txt": "text/plain; charset=utf-8",
    "manifest.webmanifest": "application/manifest+json",
    "icons/icon.svg": "image/svg+xml",
    "icons/favicon.svg": "image/svg+xml",
    "icons/favicon.ico": "image/x-icon",
    "icons/favicon-16.png": "image/png",
    "icons/favicon-32.png": "image/png",
    "icons/apple-touch-icon.png": "image/png",
    "icons/icon-192.png": "image/png",
    "icons/icon-512.png": "image/png",
    "icons/icon-maskable-512.png": "image/png",
}
# TASK-19 (Batch C): each feature's script and styles, loaded by the page shell before app.js (they plug into it)
FEATURE_FILES = ("goals", "txmeta", "billcal", "rollover", "alpaca", "bills",   # + TASK-21's bills (tracked by hand)
                 "chat",                                                          # + TASK-23's Ask panel
                 "catpick",                                                       # + polish P0's category picker
                 "txfilter",                                                       # + polish P1's Transactions filters
                 "wherewent",                                                      # + polish P2b's grouped spending card
                 "budgets")                                                        # + polish P2A's grouped Budgets
STATIC_FILES.update({f"{n}.js": "text/javascript; charset=utf-8" for n in FEATURE_FILES})
STATIC_FILES.update({f"{n}.css": "text/css; charset=utf-8" for n in FEATURE_FILES})
# Root paths browsers ask for on their own -> the static file they get.
ROOT_FILES = {"/favicon.ico": "icons/favicon.ico", "/apple-touch-icon.png": "icons/apple-touch-icon.png"}
MANUAL_PATH = re.compile(r"/api/manual/([1-9][0-9]{0,8})(/archive|/refresh|/use-estimate)?")
DETAIL_PATH = re.compile(r"/api/manual/([1-9][0-9]{0,8})/detail")
# The car / property pictures (photos.py fetched them once; served from data/images/, never hotlinked).
IMAGE_PATH = re.compile(r"/api/images/([0-9a-f]{16}\.(?:png|jpg))")
RID = re.compile(r"[A-Za-z0-9_-]{1,64}")   # HOTFIX-1: the page's id for one of its syncs
# Bank and holding logos, served from the DB (logos.py fetched them once; the page never hotlinks an image).
LOGO_PATH = re.compile(r"/api/(institutions|securities)/([^/]{1,64})/logo")
LOGO_CACHE = "private, max-age=86400"
MCP_PATH = "/mcp"   # TASK-17: the MCP server (streamable HTTP), behind the same access gate as every other route
DEFAULT_PORTS = {"http": 80, "https": 443}
# any of these (lowercased) means the request came through a proxy such as `tailscale serve`
PROXY_HEADERS = frozenset({"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded",
                           "tailscale-user-login", "tailscale-user-name", "tailscale-headers-info"})


def is_loopback(host: str) -> bool:
    return access.is_loopback(host)


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _split_origin(url: str, *, allow_root_path: bool = False) -> tuple[str, str, int | None]:
    """(scheme, host, port) of an http(s) origin; port is None when it's the scheme's default.
    Raises ValueError for anything that isn't a bare origin."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    port = parts.port  # ValueError on a malformed / out-of-range port
    if (scheme not in DEFAULT_PORTS or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment
            or parts.path not in (("", "/") if allow_root_path else ("",))):
        raise ValueError(f"not an http(s) origin: {url!r}")
    return scheme, _bracket(parts.hostname), None if port == DEFAULT_PORTS[scheme] else port


def _join_origin(scheme: str, host: str, port: int | None) -> str:
    return f"{scheme}://{host}" + ("" if port is None else f":{port}")


def canonical_origin(value: str) -> str | None:
    try:
        return _join_origin(*_split_origin(value))
    except ValueError:
        return None


def parse_public_url(url: str) -> tuple[str, set[str]]:
    """A `--public-url` → (its origin, the Host header values it accepts: host with or without port)."""
    try:
        scheme, host, port = _split_origin(url, allow_root_path=True)
    except ValueError:
        raise ValueError(f"invalid public URL {url!r}: expected http(s)://host[:port] with no path") from None
    return _join_origin(scheme, host, port), {host, f"{host}:{port or DEFAULT_PORTS[scheme]}"}


class App:
    """HTTP-agnostic request handling so tests can drive it without sockets."""

    def __init__(self, db_path: Path, client_factory: Callable[[], PlaidClient],
                 host: str = "127.0.0.1", port: int = 8765,
                 public_urls: Iterable[str] = (), allowed_users: Iterable[str] = ()):
        self.db_path = db_path
        self.client_factory = client_factory
        self.host = host
        self.port = port
        # --public-url / --allow-user (and their env vars): on top of the saved access settings, never saved here
        # (`finance serve` saves them once, access.migrate)
        self.flag_urls = list(public_urls)
        self.public_origins: set[str] = set()
        self.public_hosts: set[str] = set()
        for url in self.flag_urls:
            origin, hosts = parse_public_url(url)
            self.public_origins.add(origin)
            self.public_hosts |= hosts
        self.allowed_users = {access.canonical_identity(u) for u in allowed_users if u.strip()}
        self._client: PlaidClient | None = None
        self.today: Callable[[], date] = date.today  # tests pin this
        self._mcp_bridge = None  # mcp_server.HttpBridge, started by the first /mcp request
        self._mcp_lock = threading.Lock()
        self._out = threading.local()  # extra response headers (Set-Cookie) for respond()

    @property
    def client(self) -> PlaidClient:
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    def reset_client(self) -> None:
        """New Plaid keys were saved: the next Plaid call builds its client from them."""
        self._client = None

    def access(self, conn) -> access.Access:
        return access.load(conn, self.flag_urls, self.allowed_users)

    def allowed_hosts(self) -> set[str]:
        return {f"{h}:{self.port}" for h in ("127.0.0.1", "localhost", "[::1]", self.host)}

    def allowed_origins(self, acc: access.Access | None = None) -> set[str]:
        local = {canonical_origin(f"http://{h}:{self.port}")
                 for h in ("127.0.0.1", "localhost", "[::1]", _bracket(self.host))}
        return (local - {None}) | (acc.public_origins if acc is not None else self.public_origins)

    @staticmethod
    def proxy_headers(acc: access.Access) -> frozenset[str]:
        return PROXY_HEADERS | {acc.header_key}

    def identity_error(self, login: str | None, acc: access.Access | None = None) -> str | None:
        """Why a request on a public (proxied) host is refused, or None if its signed-in identity is allowed."""
        header = acc.header if acc else access.DEFAULT_HEADER
        users = acc.identities if acc else self.allowed_users
        tailscale = header.lower() == access.DEFAULT_HEADER.lower()
        login = (login or "").strip()
        if not login:
            if tailscale:
                return ("no Tailscale identity: this address only works through `tailscale serve` for a "
                        "logged-in tailnet user (the Tailscale-User-Login header is missing)")
            return f"no signed-in identity: this address only works through your proxy login (the {header} header is missing)"
        if not users:
            return ("no allowed identity is set: add one in Settings \u2192 Access & security on this computer "
                    "(or finance serve --allow-user)")
        if access.canonical_identity(login) not in users:
            return (f"{'tailnet user' if tailscale else 'user'} {login} is not allowed to use this page "
                    "(allowed identities are in Settings \u2192 Access & security, or finance serve --allow-user)")
        return None

    def may_bind(self, host: str) -> bool:
        """A non-loopback address only with the built-in login set up (access.startup_problems)."""
        if is_loopback(host):
            return True
        with self._conn() as conn:
            return self.access(conn).mode == "builtin" and auth.configured(conn)

    def respond(self, method: str, path: str, headers: dict[str, str], body: bytes,
                client_ip: str | None = None) -> tuple[int, str, Any, list[tuple[str, str]]]:
        """handle(), plus the extra response headers it set (a session cookie, a download's file name)."""
        self._out.headers = []
        status, ctype, out = self.handle(method, path, headers, body, client_ip)
        extra, self._out.headers = self._out.headers, []
        return status, ctype, out, extra

    def add_header(self, name: str, value: str) -> None:
        if getattr(self._out, "headers", None) is None:
            self._out.headers = []
        self._out.headers.append((name, value))

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes,
               client_ip: str | None = None) -> tuple[int, str, bytes]:
        headers = {k.lower(): v for k, v in headers.items()}
        path, _, query = path.partition("?")
        with self._conn() as conn:
            acc = self.access(conn)
        host = headers.get("host")
        proxied = bool(self.proxy_headers(acc).intersection(headers))
        req = admin.Request(method, path, query, headers, body, self._client_ip(client_ip, headers), acc,
                            plain_local=host in self.allowed_hosts() and not proxied)
        if acc.mode == "builtin":
            gate = self._builtin_gate(req)
            if gate is not None:
                return gate
        else:
            # DNS-rebinding / CSRF protection: localhost Host, or a configured public Host (via the proxy) from an
            # allowed identity; any Origin must be one of ours. Serve forwards the client's Host unchanged, so a
            # proxied request never takes the localhost path. Proxy headers only ever refuse.
            public = host not in self.allowed_hosts()
            if not public and proxied:
                if acc.mode == "local":
                    access.log_refused_proxy(acc, f"Host {host}")
                return self._json(403, {"error": "forbidden host"})
            if public:
                if acc.mode == "local" or (host or "").lower() not in acc.public_hosts:
                    if acc.mode == "local" and proxied:
                        access.log_refused_proxy(acc, f"Host {host}")
                    return self._json(403, {"error": "forbidden host"})
                # /mcp also takes a bearer token instead of the proxy's identity (an agent with no browser login)
                if not (path == MCP_PATH and self._bearer(headers) is not None):
                    login = headers.get(acc.header_key)
                    denied = self.identity_error(login, acc)
                    if denied:
                        return self._forbidden(method, path, denied)
                    with self._conn() as conn:
                        access.record_proxy_request(conn, acc, login.strip(), host or "")
            origin = headers.get("origin")
            if origin and canonical_origin(origin) not in self.allowed_origins(acc):
                return self._json(403, {"error": "forbidden origin"})
        try:
            if path == MCP_PATH:
                return self._mcp(method, query, headers, body)
            if admin.handles(path):
                return admin.route(self, req)
            if chat.handles(path):   # TASK-23: past the same gate (and, with the built-in login, its CSRF check)
                return chat.route(self, req)
            if method == "GET":
                if path == "/":
                    return 200, HTML, self.page(tailnet=host not in self.allowed_hosts(),
                                                csrf=req.session["csrf"] if req.session else None).encode()
                return self._get(path, query, tailnet=host not in self.allowed_hosts(), req=req)
            # TASK-19: DELETE /api/alpaca/key. It went through the same gate as every POST above (the access mode, the
            # proxy identity, the Origin and, with the built-in login, the session and its CSRF token).
            if method == "DELETE" and path in features.DELETE_ROUTES:
                if not headers.get("content-type", "").startswith(JSON):
                    return self._json(415, {"error": "expected application/json"})
                with self._conn() as conn:
                    return self._json(200, features.DELETE_ROUTES[path](conn, self.today()))
            if method != "POST":
                return self._json(404, {"error": "not found"})
            if not headers.get("content-type", "").startswith(JSON):
                return self._json(415, {"error": "expected application/json"})
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            if not isinstance(data, dict):
                return self._json(400, {"error": "expected a JSON object"})
            return self._post(path, data, req)
        except api.ApiError as e:
            return self._json(e.status, {"error": str(e), **({"field": e.field} if e.field else {})})
        except link.LinkRefused as e:
            return self._json(e.status, {"error": str(e)})
        except PlaidError as e:
            return self._json(502, {"error": f"Plaid error {e.error_code}: {e.display_message or e.error_message}"})
        except Exception as e:  # never leak a traceback (or anything in it) to the browser
            log.exception("request failed: %s %s", method, path)
            return self._json(500, {"error": f"internal error ({type(e).__name__})"})

    # --- the built-in login's gate (TASK-18) ---------------------------------------------------------------------

    @staticmethod
    def _client_ip(peer: str | None, headers: dict[str, str]) -> str | None:
        """The address a lockout counts against: the peer, or behind a proxy on this computer the address it saw (the
        last X-Forwarded-For entry, the one that proxy added)."""
        fwd = headers.get("x-forwarded-for", "")
        if fwd.strip() and (peer is None or is_loopback(peer)):
            return fwd.split(",")[-1].strip()[:64] or peer
        return peer

    @staticmethod
    def _cookie(headers: dict[str, str]) -> str | None:
        for part in headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == auth.COOKIE and value:
                return value
        return None

    def _bearer(self, headers: dict[str, str]) -> int | None:
        """The live MCP token an `Authorization: Bearer hfm_...` header carries (its id), else None."""
        kind, _, token = headers.get("authorization", "").partition(" ")
        if kind.lower() != "bearer" or not token.strip():
            return None
        with self._conn() as conn:
            return auth.mcp_token_check(conn, token.strip())

    def _builtin_gate(self, req: "admin.Request") -> tuple[int, str, bytes] | None:
        """Built-in login: an allowed Origin, then a session for everything but the sign-in page, its assets and its
        own endpoints (and, while no login exists yet, its set-up from this computer); a CSRF token on every state-changing
        request (POST, DELETE) with a session; a bearer token on /mcp. None: go on and answer."""
        headers, path, method = req.headers, req.path, req.method
        origin = headers.get("origin")
        if origin:
            scheme = "https" if headers.get("x-forwarded-proto", "").strip().lower() == "https" else "http"
            own = canonical_origin(f"{scheme}://{headers.get('host', '')}")
            if canonical_origin(origin) not in self.allowed_origins(req.acc) | ({own} if own else set()):
                return self._json(403, {"error": "forbidden origin"})
        if path == MCP_PATH:
            if self._bearer(headers) is None:
                return self._json(401, {"error": "this address needs a bearer token: Settings \u2192 Connect other "
                                                 "agents \u2192 New token, sent as Authorization: Bearer hfm_..."})
            return None
        with self._conn() as conn:
            req.session = auth.session(conn, self._cookie(headers))
            req.login_ready = auth.configured(conn)
        if req.session is None:
            if path in ROOT_FILES or path.startswith("/static/") or admin.open_without_session(req):
                return None
            if method == "GET" and path == "/":
                over_https = req.plain_local or headers.get("x-forwarded-proto", "").strip().lower() == "https"
                return 200, HTML, redact.scrub(render_login_page(setup=not req.login_ready and req.plain_local,
                                                                  https=over_https)).encode()
            return self._json(401, {"error": "sign in first", "signin": True})
        if method not in ("GET", "HEAD") and not auth.csrf_ok(req.session, headers.get("x-csrf-token")):
            return self._json(403, {"error": "missing or wrong CSRF token: reload the page"})
        return None

    def _get(self, path: str, query: str, tailnet: bool, req: admin.Request | None = None) -> tuple[int, str, bytes]:
        if path == "/":
            return 200, HTML, self.page(tailnet=tailnet).encode()
        if path in ROOT_FILES:
            return self._static(ROOT_FILES[path])
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/status":
            with self._conn() as conn:
                return self._json(200, report.status(conn))
        if path == "/api/sync":   # HOTFIX-1: how the page's sync went (syncnow.lookup)
            rid = parse_qs(query).get("rid", [None])[0]
            with self._conn() as conn:
                return self._json(200, syncnow.lookup(conn, rid if rid and RID.fullmatch(rid) else None))
        m = LOGO_PATH.fullmatch(path)
        if m:
            with self._conn() as conn:
                found = (logos.institution_logo if m[1] == "institutions" else logos.security_logo)(conn, m[2])
            return (200, *found) if found else self._json(404, {"error": "not found"})
        m = DETAIL_PATH.fullmatch(path)
        if m:
            with self._conn() as conn:
                d = api.manual_detail(conn, int(m[1]), self.today())
            if d["photo"]["status"] == "pending":   # the first view fetches the picture, in the background
                photos.schedule(self.db_path, d["id"])
            return self._json(200, d)
        m = IMAGE_PATH.fullmatch(path.partition("?")[0])
        if m:
            found = photos.file_bytes(self.db_path, m[1])
            return (200, *found) if found else self._json(404, {"error": "not found"})
        route = api.GET_ROUTES.get(path) or features.GET_ROUTES.get(path)  # TASK-19: its routes
        if route is None:
            return self._json(404, {"error": "not found"})
        # polish P1: the Transactions page's picks may repeat (?category=A&category=B), each as a list
        params = {k: v if path == "/api/transactions" and k in api.LIST_PARAMS else v[0]
                  for k, v in parse_qs(query, keep_blank_values=True).items()}
        with self._conn() as conn:
            if path == "/api/settings" and req is not None:
                return self._json(200, api.settings_for_owner(conn, params, self.today(), chat._request_owner(req)))
            return self._json(200, route(conn, params, self.today()))

    @staticmethod
    def cache_control(path: str, status: int) -> str:
        """Logos and pictures may be cached by the browser for a day (their URLs carry a version); everything else:
        no-store."""
        bare = path.partition("?")[0]
        return LOGO_CACHE if status == 200 and (LOGO_PATH.fullmatch(bare) or IMAGE_PATH.fullmatch(bare)) else "no-store"

    @staticmethod
    def _static(name: str) -> tuple[int, str, bytes]:
        ctype = STATIC_FILES.get(name)
        if ctype is None:
            return App._json(404, {"error": "not found"})
        return 200, ctype, (STATIC_DIR / name).read_bytes()

    def _post(self, path: str, data: dict[str, Any], req: admin.Request) -> tuple[int, str, bytes]:
        with self._conn() as conn:
            if path in features.POST_ROUTES:  # TASK-19
                return self._json(200, features.POST_ROUTES[path](conn, data, self.today()))
            if path == "/api/transactions/category":
                return self._json(200, api.set_transaction_category(conn, data))
            if path == "/api/budgets/cap":
                return self._json(200, api.set_cap(conn, data))
            if path == "/api/budgets/group":     # polish P2e: a group's own budget, and the one-time move to group budgets
                return self._json(200, api.set_group_budget(conn, data))
            if path == "/api/budgets/move":
                return self._json(200, api.move_budgets(conn, data))
            if path == "/api/settings":
                return self._json(200, api.save_settings_for_owner(conn, data, chat._request_owner(req)))
            if path == "/api/manual":
                return self._json(200, self._with_photo(conn, api.manual_create(conn, data, self.today())))
            m = MANUAL_PATH.fullmatch(path)
            if m:
                action = {"/archive": api.manual_archive, "/refresh": api.manual_refresh,
                          "/use-estimate": api.manual_use_estimate}.get(m[2])
                if action:
                    return self._json(200, action(conn, int(m[1]), self.today()))
                return self._json(200, self._with_photo(conn, api.manual_edit(conn, int(m[1]), data, self.today())))
            if path == "/api/link_token":
                return self._json(200, link.new_link_token(conn, self.client))
            if path == "/api/link_token/update":
                return self._json(200, link.update_link_token(conn, self.client, str(data.get("item_id", ""))))
            if path == "/api/exchange":
                return self._json(200, link.exchange(conn, self.client, data.get("public_token", ""),
                                                     data.get("metadata")))
            if path == "/api/reconnected":
                return self._json(200, link.reconnected(conn, self.client, str(data.get("item_id", ""))))
            if path == "/api/sync":
                return self._sync(conn, data)
        return self._json(404, {"error": "not found"})

    def _sync(self, conn, data: dict[str, Any]) -> tuple[int, str, bytes]:
        """TASK-22: Sync now {item_id?, refresh?, rid?}: 409 while one runs, 429 in an Item's cooldown, 403 when asking
        the banks for fresh data isn't allowed. HOTFIX-1: the sync runs in the background (syncnow.start); this answers
        with its result when it's done within syncnow.ANSWER_WITHIN seconds, else 202 {rid, running} and the page asks
        GET /api/sync?rid= (the page's own id for it, so it can also find a sync whose answer never reached it)."""
        item_id, refresh, rid = data.get("item_id"), data.get("refresh", False), data.get("rid")
        if item_id is not None and not isinstance(item_id, str):
            raise api.ApiError("item_id must be a string", field="item_id")
        if not isinstance(refresh, bool):
            raise api.ApiError("refresh must be true or false", field="refresh")
        if rid is not None and not (isinstance(rid, str) and RID.fullmatch(rid)):
            raise api.ApiError("rid must be 1 to 64 letters, digits, - or _", field="rid")
        t0 = time.monotonic()
        try:
            job = syncnow.start(self.db_path, conn, lambda: self.client, item_id, refresh, rid)
        except (syncnow.SyncRefused, config.ConfigError) as e:
            status, payload = ((e.status, e.payload()) if isinstance(e, syncnow.SyncRefused) else
                               (400, {"error": "Plaid keys are missing: Settings \u2192 Connections \u2192 Replace keys"}))
            banks = conn.execute("SELECT COUNT(*) FROM items WHERE removed_at IS NULL").fetchone()[0]
            syncnow.requests_log.info("POST /api/sync %s: %s, %d bank(s), %.1f s, refused %d: %s", (rid or "-")[:12],
                                      "refresh" if refresh else "sync", banks, time.monotonic() - t0, status, payload["error"])
            return self._json(status, payload)
        if job.done.wait(syncnow.ANSWER_WITHIN):
            return self._json(200, job.result) if job.error is None else self._json(500, {"error": job.error, "rid": job.rid})
        job.answered_202 = True
        return self._json(202, job.state())

    def _mcp(self, method: str, query: str, headers: dict[str, str], body: bytes) -> tuple[int, str, bytes]:
        """/mcp: the finance_* tools over MCP (mcp_server.py), for a request that passed the gate above. Off while
        Settings -> Connect other agents -> MCP server is switched off (prefs mcp.enabled)."""
        with self._conn() as conn:
            if not prefs.get(conn, "mcp.enabled"):
                return self._json(404, {"error": "the MCP server is switched off (Settings \u2192 Connect other agents)"})
        with self._mcp_lock:
            if self._mcp_bridge is None:
                try:
                    from .mcp_server import HttpBridge
                except ImportError:  # the optional extra isn't installed
                    return self._json(501, {"error": "the MCP server needs the mcp extra: uv sync --extra mcp"})
                self._mcp_bridge = HttpBridge(self.db_path)
        status, ctype, out = self._mcp_bridge.handle(method, query, headers, body)
        return status, ctype, redact.scrub(out.decode("utf-8", "replace")).encode()

    def _with_photo(self, conn, m: dict[str, Any]) -> dict[str, Any]:
        """After a vehicle / property is saved, its picture is fetched in the background if it's due."""
        if m.get("kind") in ("vehicle", "property") and photos.due(conn, manual.get(conn, m["id"], include_archived=True)):
            photos.schedule(self.db_path, m["id"])
        return m

    def _conn(self):
        return _Closing(db.connect(self.db_path))

    @staticmethod
    def _json(status: int, payload: Any) -> tuple[int, str, bytes]:
        return status, JSON, redact.scrub(json.dumps(payload)).encode()

    @classmethod
    def _forbidden(cls, method: str, path: str, reason: str) -> tuple[int, str, bytes]:
        if method == "GET" and not path.startswith("/api/") and path != MCP_PATH:  # a browser navigation: explain
            page = (f"<!doctype html><html lang=en><head><meta charset=utf-8><title>403 Forbidden</title>"
                    f"<meta name=viewport content=\"width=device-width, initial-scale=1\"></head>"
                    f"<body><h1>403 Forbidden</h1><p>{_e(reason)}</p></body></html>")
            return 403, HTML, redact.scrub(page).encode()
        return cls._json(403, {"error": f"forbidden: {reason}"})

    def page(self, tailnet: bool = False, csrf: str | None = None) -> str:
        with self._conn() as conn:
            s = report.status(conn)
            v = prefs.get_many(conn, ["appearance.theme", "region.currency", "region.locale"])
            shell = {"csrf": csrf, "theme": v["appearance.theme"], "setup": admin.needs_setup(conn), "signed_in": csrf is not None,
                     "currency": v["region.currency"], "locale": v["region.locale"],
                     "ask": chat.brain(conn)}   # TASK-23: the Ask button (none with AI off) and its brain chip
        return redact.scrub(render_page(s, tailnet, shell))


class _Closing:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


# (route, sidebar label, icon, phone tab label or None). The phone's tab bar has four of these pages (TASK-18 addendum)
# and, HOTFIX-1, Settings as its fifth tab; the rest are in the drawer (app.css orders it: Overview, Transactions, Budgets,
# Goals, then Accounts, Recurring, Investments, Loans & debt). The desktop sidebar keeps this order: Goals (TASK-19)
# under Budgets (HOTFIX-1: the drawer's too); the bills calendar is on Recurring.
PAGES = (("overview", "Overview", "grid", "Overview"), ("transactions", "Transactions", "swap", "Transactions"),
         ("budgets", "Budgets", "target", "Budgets"), ("goals", "Goals", "flag", None),
         ("recurring", "Recurring", "repeat", None),
         ("accounts", "Accounts", "landmark", "Accounts"), ("investments", "Investments", "linechart", None),
         ("debt", "Loans & debt", "handcoins", None))
TABS = ("overview", "transactions", "budgets", "accounts", "settings")
SETTINGS_TAB = ("settings", "Settings", "gear", "Settings")   # HOTFIX-1: in the sidebar it's the gear at the bottom
# 24x24 Lucide-style stroke icons. They live in the HTML (a <template>), so the script clones them and never
# parses SVG markup itself; rings and sparklines are drawn with createElementNS from numbers only.
ICONS = {
    'alert': '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4M12 17h.01"/>',
    'archive': '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
    'arrowl': '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    'bag': '<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z"/><path d="M3 6h18"/><path d="M16 10a4 4 0 0 1-8 0"/>',
    'bars': '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
    'bell': '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    'bitcoin': '<path d="M11.767 19.089c4.924.868 6.14-6.025 1.216-6.894m-1.216 6.894L5.86 18.047m5.908 1.042-.347 1.97m1.563-8.864c4.924.869 6.14-6.025 1.215-6.893m-1.215 6.893-3.94-.694m5.155-6.2L8.29 4.26m5.908 1.042.348-1.97M7.48 20.364l3.126-17.727"/>',
    'briefcase': '<path d="M16 20V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/><rect width="20" height="14" x="2" y="6" rx="2"/>',
    'building': '<rect width="16" height="20" x="4" y="2" rx="2"/><path d="M9 22v-4h6v4"/><path d="M8 6h.01M16 6h.01M12 6h.01M12 10h.01M12 14h.01M16 10h.01M16 14h.01M8 10h.01M8 14h.01"/>',
    'bulb': '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6M10 22h4"/>',
    'calendar': '<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    'car': '<path d="M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2"/><circle cx="7" cy="17" r="2"/><path d="M9 17h6"/><circle cx="17" cy="17" r="2"/>',
    'card': '<rect width="20" height="14" x="2" y="5" rx="2"/><path d="M2 10h20"/>',
    'cart': '<circle cx="8" cy="21" r="1"/><circle cx="19" cy="21" r="1"/><path d="M2.05 2.05h2l2.66 12.42a2 2 0 0 0 2 1.58h9.78a2 2 0 0 0 1.95-1.57l1.65-7.43H5.12"/>',
    'check': '<path d="M20 6 9 17l-5-5"/>',
    'checkc': '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    'chevd': '<path d="m6 9 6 6 6-6"/>',
    'chevl': '<path d="m15 18-6-6 6-6"/>',
    'chevr': '<path d="m9 18 6-6-6-6"/>',
    'clock': '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    'coins': '<circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18"/><path d="M7 6h1v4"/><path d="m16.71 13.88.7.71-2.82 2.82"/>',
    'dollar': '<path d="M12 2v20"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
    'fileminus': '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M9 15h6"/>',
    'flag': '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><path d="M4 22v-7"/>',
    # TASK-18: Settings, sign-in, export / backup / import, the theme switch
    'gear': '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    'upload': '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
    'download': '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    'filetext': '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8M16 13H8M16 17H8"/>',
    'globe': '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2m-7.07-14.07 1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    'moon': '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    'monitor': '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8M12 17v4"/>',
    'key': '<path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4"/><path d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/>',
    'database': '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
    'user': '<circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/>',
    'palette': '<path d="M12 22a1 1 0 0 1 0-20 10 9 0 0 1 10 9 5 5 0 0 1-5 5h-2.25a1.75 1.75 0 0 0-1.4 2.8l.3.4a1.75 1.75 0 0 1-1.4 2.8z"/><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/>',
    'lock': '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    'copy': '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    'plug': '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
    'logout': '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    'gauge': '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    'grid': '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    'handcoins': '<path d="M11 15h2a2 2 0 1 0 0-4h-3c-.6 0-1.1.2-1.4.6L3 17"/><path d="m7 21 1.6-1.4c.3-.4.8-.6 1.4-.6h4c1.1 0 2.1-.4 2.8-1.2l4.6-4.4a2 2 0 0 0-2.75-2.91l-4.2 3.9"/><path d="m2 16 6 6"/><circle cx="16" cy="9" r="2.9"/><circle cx="6" cy="5" r="3"/>',
    'heart': '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
    'house': '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',   # polish P0: Lucide's house in place of the hand-drawn home
    'income': '<path d="M17 7 7 17"/><path d="M17 17H7V7"/>',
    'info': '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    'landmark': '<path d="M3 22h18"/><path d="M6 18v-7M10 18v-7M14 18v-7M18 18v-7"/><path d="M12 2 20 7H4z"/>',
    'layers': '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    'linechart': '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="m19 9-5 5-4-4-3 3"/>',
    'link': '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    'menu': '<path d="M4 7h16M4 12h16M4 17h16"/>',
    'package': '<path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z"/><path d="M12 22V12"/><path d="m3.3 7 7.7 5 7.7-5"/>',
    'party': '<path d="M5.8 11.3 2 22l10.7-3.79"/><path d="M4 3h.01M22 8h.01M15 2h.01M22 20h.01"/><path d="m22 2-2.24.75a2.9 2.9 0 0 0-1.96 3.12c.1.86-.57 1.63-1.45 1.63h-.38c-.86 0-1.6.6-1.76 1.44L14 10"/><path d="m22 13-.82-.33c-.86-.34-1.82.2-1.98 1.11-.11.7-.72 1.22-1.43 1.22H17"/><path d="m11 2 .33.82c.34.86-.2 1.82-1.11 1.98C9.52 4.9 9 5.52 9 6.23V7"/><path d="M11 13c1.93 1.93 2.83 4.17 2 5-.83.83-3.07-.07-5-2-1.93-1.93-2.83-4.17-2-5 .83-.83 3.07.07 5 2Z"/>',
    'pencil': '<path d="M21.17 6.81a1 1 0 0 0-3.99-3.99L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32a2 2 0 0 0 .83-.5z"/><path d="m15 5 4 4"/>',
    'percent': '<path d="M19 5 5 19"/><circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/>',
    'pie': '<path d="M21.21 15.89A10 10 0 1 1 8 2.83"/><path d="M22 12A10 10 0 0 0 12 2v10z"/>',
    'piggy': '<path d="M11 17h3v2a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-3a3.16 3.16 0 0 0 2-2h1a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1h-1a5 5 0 0 0-2-4V3a4 4 0 0 0-3.2 1.6l-.3.4H11a6 6 0 0 0-6 6v1a5 5 0 0 0 2 4v3a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1z"/><path d="M16 10h.01"/><path d="M2 8v1a2 2 0 0 0 2 2h1"/>',
    'plane': '<path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"/>',
    'plus': '<path d="M12 5v14M5 12h14"/>',
    'receipt': '<path d="M12 17V7"/><path d="M16 8h-6a2 2 0 0 0 0 4h4a2 2 0 0 1 0 4H8"/><path d="M4 3a1 1 0 0 1 1-1 1.3 1.3 0 0 1 .7.2l.933.6a1.3 1.3 0 0 0 1.4 0l.934-.6a1.3 1.3 0 0 1 1.4 0l.933.6a1.3 1.3 0 0 0 1.4 0l.933-.6a1.3 1.3 0 0 1 1.4 0l.934.6a1.3 1.3 0 0 0 1.4 0l.933-.6A1.3 1.3 0 0 1 19 2a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1 1.3 1.3 0 0 1-.7-.2l-.933-.6a1.3 1.3 0 0 0-1.4 0l-.934.6a1.3 1.3 0 0 1-1.4 0l-.933-.6a1.3 1.3 0 0 0-1.4 0l-.933.6a1.3 1.3 0 0 1-1.4 0l-.934-.6a1.3 1.3 0 0 0-1.4 0l-.933.6a1.3 1.3 0 0 1-.7.2 1 1 0 0 1-1-1z"/>',
    'refresh': '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>',
    'repeat': '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
    'search': '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    'shield': '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    'sort': '<path d="m21 16-4 4-4-4"/><path d="M17 20V4"/><path d="m3 8 4-4 4 4"/><path d="M7 4v16"/>',
    'sparkles': '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4M22 5h-4"/>',
    'sprout': '<path d="M7 20h10"/><path d="M10 20c5.5-2.5.8-6.4 3-10"/><path d="M9.5 9.4c1.1.8 1.8 2.2 2.3 3.7-2 .4-3.5.4-4.8-.3-1.2-.6-2.3-1.9-3-4.2 2.8-.5 4.4 0 5.5.8z"/><path d="M14.1 6a7 7 0 0 0-1.1 4c1.9-.1 3.3-.6 4.3-1.4 1-1 1.6-2.3 1.7-4.6-2.7.1-4 1-4.9 2z"/>',
    'swap': '<path d="M8 3 4 7l4 4"/><path d="M4 7h16"/><path d="m16 21 4-4-4-4"/><path d="M20 17H4"/>',
    'tag': '<path d="M12.6 2.6A2 2 0 0 0 11.2 2H4a2 2 0 0 0-2 2v7.2a2 2 0 0 0 .6 1.4l8.7 8.7a2.4 2.4 0 0 0 3.4 0l6.6-6.6a2.4 2.4 0 0 0 0-3.4z"/><circle cx="7.5" cy="7.5" r="1"/>',
    'target': '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    'ticket': '<path d="M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/><path d="M13 5v2M13 17v2M13 11v2"/>',
    'trend': '<path d="M22 7 13.5 15.5 8.5 10.5 2 17"/><path d="M16 7h6v6"/>',
    'tv': '<rect width="20" height="15" x="2" y="7" rx="2"/><path d="m17 2-5 5-5-5"/>',
    'utensils': '<path d="M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2"/><path d="M7 2v20"/><path d="M21 15V2a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3Zm0 0v7"/>',
    'wallet': '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
    'x': '<path d="M18 6 6 18M6 6l12 12"/>',
}
ICONS.update({  # TASK-19
    'bookmark': '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>',
    'key': '<path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4"/><path d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/>',
    'split': '<path d="M16 3h5v5"/><path d="M8 3H3v5"/><path d="M12 22v-8.3a4 4 0 0 0-1.172-2.872L3 3"/><path d="m15 9 6-6"/>',
    'trash': '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
})
ICONS.update({  # TASK-23: the Ask button, Send, the API-key brain chip
    'chat': '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
    'send': '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>',
    'cpu': '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/>',
})
ICONS.update({  # the Accounts group header's month-change arrows
    'arrowdownright': '<path d="m7 7 10 10"/><path d="M17 7v10H7"/>',
    'arrowupright': '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
})

ICONS.update({  # polish P0: a Lucide icon per category (db.CATEGORY_GROUPS names them), and the filter button's funnel
    'wifi': '<path d="M12 20h.01"/><path d="M2 8.82a15 15 0 0 1 20 0"/><path d="M5 12.859a10 10 0 0 1 14 0"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/>',
    'armchair': '<path d="M19 9V6a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v3"/><path d="M3 16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5a2 2 0 0 0-4 0v1.5a.5.5 0 0 1-.5.5h-9a.5.5 0 0 1-.5-.5V11a2 2 0 0 0-4 0z"/><path d="M5 18v2"/><path d="M19 18v2"/>',
    'paint-roller': '<rect width="16" height="6" x="2" y="2" rx="2"/><path d="M10 16v-2a2 2 0 0 1 2-2h8a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect width="4" height="6" x="8" y="16" rx="1"/>',
    'shopping-cart': '<path d="m2.05 2.05 1.099-.028a1 1 0 0 1 1.008.815l2.69 14.347A1 1 0 0 0 7.83 18H18"/><path d="M4.563 5h16.435a1 1 0 0 1 .981 1.204l-1.026 6.226A2 2 0 0 1 18.962 14H6.25"/><circle cx="18" cy="20" r="2"/><circle cx="8" cy="20" r="2"/>',
    'coffee': '<path d="M10 2v2"/><path d="M14 2v2"/><path d="M16 8a1 1 0 0 1 1 1v8a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1h14a4 4 0 1 1 0 8h-1"/><path d="M6 2v2"/>',
    'wine': '<path d="M8 22h8"/><path d="M7 10h10"/><path d="M12 15v7"/><path d="M12 15a5 5 0 0 0 5-5c0-2-.5-4-2-8H9c-1.5 4-2 6-2 8a5 5 0 0 0 5 5Z"/>',
    'car-front': '<path d="m21 8-2 2-1.5-3.7A2 2 0 0 0 15.646 5H8.4a2 2 0 0 0-1.903 1.257L5 10 3 8"/><path d="M7 14h.01"/><path d="M17 14h.01"/><rect width="18" height="8" x="3" y="10" rx="2"/><path d="M5 18v2"/><path d="M19 18v2"/>',
    'fuel': '<path d="M14 13h2a2 2 0 0 1 2 2v2a2 2 0 0 0 4 0v-6.998a2 2 0 0 0-.59-1.42L18 5"/><path d="M14 21V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v16"/><path d="M2 21h13"/><path d="M3 9h11"/>',
    'square-parking': '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 17V7h4a3 3 0 0 1 0 6H9"/>',
    'car-taxi-front': '<path d="M10 2h4"/><path d="m21 8-2 2-1.5-3.7A2 2 0 0 0 15.646 5H8.4a2 2 0 0 0-1.903 1.257L5 10 3 8"/><path d="M7 14h.01"/><path d="M17 14h.01"/><rect width="18" height="8" x="3" y="10" rx="2"/><path d="M5 18v2"/><path d="M19 18v2"/>',
    'wrench': '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/>',
    'train-front': '<path d="M8 3.1V7a4 4 0 0 0 8 0V3.1"/><path d="m9 15-1-1"/><path d="m15 15 1-1"/><path d="M9 19c-2.8 0-5-2.2-5-5v-4a8 8 0 0 1 16 0v4c0 2.8-2.2 5-5 5Z"/><path d="m8 19-2 3"/><path d="m16 19 2 3"/>',
    'route': '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
    'shopping-bag': '<path d="M16 10a4 4 0 0 1-8 0"/><path d="M3.103 6.034h17.794"/><path d="M3.4 5.467a2 2 0 0 0-.4 1.2V20a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6.667a2 2 0 0 0-.4-1.2l-2-2.667A2 2 0 0 0 17 2H7a2 2 0 0 0-1.6.8z"/>',
    'shirt': '<path d="M20.38 3.46 16 2a4 4 0 0 1-8 0L3.62 3.46a2 2 0 0 0-1.34 2.23l.58 3.47a1 1 0 0 0 .99.84H6v10c0 1.1.9 2 2 2h8a2 2 0 0 0 2-2V10h2.15a1 1 0 0 0 .99-.84l.58-3.47a2 2 0 0 0-1.34-2.23z"/>',
    'laptop': '<path d="M18 5a2 2 0 0 1 2 2v8.526a2 2 0 0 0 .212.897l1.068 2.127a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45l1.068-2.127A2 2 0 0 0 4 15.526V7a2 2 0 0 1 2-2z"/><path d="M20.054 15.987H3.946"/>',
    'heart-pulse': '<path d="M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5"/><path d="M3.22 13H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"/>',
    'scissors': '<circle cx="6" cy="6" r="3"/><path d="M8.12 8.12 12 12"/><path d="M20 4 8.12 15.88"/><circle cx="6" cy="18" r="3"/><path d="M14.8 14.8 20 20"/>',
    'dumbbell': '<path d="M17.596 12.768a2 2 0 1 0 2.829-2.829l-1.768-1.767a2 2 0 0 0 2.828-2.829l-2.828-2.828a2 2 0 0 0-2.829 2.828l-1.767-1.768a2 2 0 1 0-2.829 2.829z"/><path d="m2.5 21.5 1.4-1.4"/><path d="m20.1 3.9 1.4-1.4"/><path d="M5.343 21.485a2 2 0 1 0 2.829-2.828l1.767 1.768a2 2 0 1 0 2.829-2.829l-6.364-6.364a2 2 0 1 0-2.829 2.829l1.768 1.767a2 2 0 0 0-2.828 2.829z"/><path d="m9.6 14.4 4.8-4.8"/>',
    'pill': '<path d="m10.5 20.5 10-10a4.95 4.95 0 1 0-7-7l-10 10a4.95 4.95 0 1 0 7 7Z"/><path d="m8.5 8.5 7 7"/>',
    'stethoscope': '<path d="M11 2v2"/><path d="M5 2v2"/><path d="M5 3H4a2 2 0 0 0-2 2v4a6 6 0 0 0 12 0V5a2 2 0 0 0-2-2h-1"/><path d="M8 15a6 6 0 0 0 12 0v-3"/><circle cx="20" cy="10" r="2"/>',
    'clapperboard': '<path d="m12.296 3.464 3.02 3.956"/><path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3z"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="m6.18 5.276 3.1 3.899"/>',
    'bed-double': '<path d="M2 20v-8a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v8"/><path d="M4 10V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v4"/><path d="M12 4v6"/><path d="M2 18h20"/>',
    'ellipsis': '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    'gift': '<path d="M12 7v14"/><path d="M20 11v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8"/><path d="M7.5 7a1 1 0 0 1 0-5A4.8 8 0 0 1 12 7a4.8 8 0 0 1 4.5-5 1 1 0 0 1 0 5"/><rect x="3" y="7" width="18" height="4" rx="1"/>',
    'paw-print': '<circle cx="11" cy="4" r="2"/><circle cx="18" cy="8" r="2"/><circle cx="20" cy="16" r="2"/><path d="M9 10a5 5 0 0 1 5 5v3.5a3.5 3.5 0 0 1-6.84 1.045Q6.52 17.48 4.46 16.84A3.5 3.5 0 0 1 5.5 10Z"/>',
    'graduation-cap': '<path d="M21.42 10.922a1 1 0 0 0-.019-1.838L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08a1 1 0 0 0 0 1.832l8.57 3.908a2 2 0 0 0 1.66 0z"/><path d="M22 10v6"/><path d="M6 12.5V16a6 3 0 0 0 12 0v-3.5"/>',
    'rocket': '<path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />\n  <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09" />\n  <path d="M9 12a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.4 22.4 0 0 1-4 2z" />\n  <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 .05 5 .05" />',
    'funnel': '<path d="M10 20a1 1 0 0 0 .553.895l2 1A1 1 0 0 0 14 21v-7a2 2 0 0 1 .517-1.341L21.74 4.67A1 1 0 0 0 21 3H3a1 1 0 0 0-.742 1.67l7.225 7.989A2 2 0 0 1 10 14z"/>',
    # polish P1 (Lucide): the Transactions table's sort indicators, a group's partly-picked box
    'arrow-up': '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    'arrow-down': '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
    'chevrons-up-down': '<path d="m7 15 5 5 5-5"/><path d="m7 9 5-5 5 5"/>',
    'minus': '<path d="M5 12h14"/>',
})
ICONS.update({name: ICONS[same] for name, same in (('piggy-bank', 'piggy'), ('hand-coins', 'handcoins'), ('shield-check', 'shield'),
                                                   ('arrow-down-left', 'income'), ('arrow-left-right', 'swap'))})

# The drawn car and house a car / property page shows until (or instead of) its photo: never a broken image.
ART = ("""<svg data-art="car" viewBox="0 0 640 300" aria-hidden="true"><defs>"""
       """<linearGradient id="art-bd" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3a4357"/>"""
       """<stop offset="1" stop-color="#262d3c"/></linearGradient><linearGradient id="art-gl" x1="0" y1="0" x2="0" y2="1">"""
       """<stop offset="0" stop-color="#6c7a99" stop-opacity=".55"/><stop offset="1" stop-color="#3b4560" stop-opacity=".35"/>"""
       """</linearGradient></defs><ellipse cx="320" cy="256" rx="262" ry="13" fill="#070910" opacity=".85"/>"""
       """<path d="M58 206c-6-26 4-48 30-56l70-18c30-34 70-58 124-64 64-7 128 2 178 26 26 12 50 28 70 44l40 8c22 5 36 22 36 44"""
       """v12c0 10-8 18-18 18h-38a58 58 0 0 0-116 0H222a58 58 0 0 0-116 0H74c-9 0-16-6-16-14z" fill="url(#art-bd)"/>"""
       """<path d="M178 132c30-32 66-50 112-55 54-6 110 2 154 22 18 8 34 19 48 31z" fill="url(#art-gl)"/>"""
       """<path d="M300 78l-8 56M430 94l18 38" stroke="#262d3c" stroke-width="7"/>"""
       """<path d="M86 180h40M552 178h32" stroke="#8a97b3" stroke-width="5" stroke-linecap="round" opacity=".7"/>"""
       """<g><circle cx="164" cy="226" r="42" fill="#0c0f16"/><circle cx="164" cy="226" r="27" fill="#2b3346"/>"""
       """<circle cx="164" cy="226" r="8" fill="#5d6882"/></g><g><circle cx="480" cy="226" r="42" fill="#0c0f16"/>"""
       """<circle cx="480" cy="226" r="27" fill="#2b3346"/><circle cx="480" cy="226" r="8" fill="#5d6882"/></g></svg>"""
       """<svg data-art="house" viewBox="0 0 640 300" aria-hidden="true">"""
       """<ellipse cx="320" cy="262" rx="250" ry="12" fill="#070910" opacity=".85"/>"""
       """<path d="M150 150 320 50l170 100v108H150z" fill="#2a3244"/><path d="M130 158 320 44l190 114" stroke="#3d475e" """
       """stroke-width="14" stroke-linejoin="round" fill="none"/><rect x="286" y="180" width="54" height="78" rx="4" """
       """fill="#161b26"/><rect x="190" y="170" width="62" height="48" rx="4" fill="#56637f" opacity=".6"/>"""
       """<rect x="380" y="170" width="62" height="48" rx="4" fill="#56637f" opacity=".6"/></svg>""")


def _icon(name: str, attrs: str = "") -> str:
    return f'<svg class="i" viewBox="0 0 24 24" aria-hidden="true"{attrs}>{ICONS[name]}</svg>'


def render_page(s: dict[str, Any], tailnet: bool = False, shell: dict[str, Any] | None = None) -> str:
    """The app shell. Pages are drawn by /static/app.js from the JSON API; the connection footer (slot
    counter, Add account button, full / blocked-duplicate warnings) is rendered here so the safety info is
    right even before the script runs. `shell` (TASK-18): the saved theme (applied before the first paint by
    /static/theme.js), the session's CSRF token (built-in login), whether first-run setup is due."""
    shell = shell or {}
    theme = shell.get("theme") if shell.get("theme") in ("dark", "light", "system") else "dark"
    region = (f' data-currency="{_e(shell["currency"])}"' if shell.get("currency") else "") + (
        f' data-locale="{_e(shell["locale"])}"' if shell.get("locale") else "")
    csrf = f'\n<meta name="csrf-token" content="{_e(shell["csrf"])}">' if shell.get("csrf") else ""
    body_flags = (" data-setup" if shell.get("setup") else "") + (" data-signed-in" if shell.get("signed_in") else "")
    ask = shell.get("ask") or {}
    if ask.get("mode") in ("api", "hermes"):   # TASK-23: chat.js draws the Ask button and panel from these
        body_flags += f' data-ask="{_e(ask["mode"])}" data-ask-brain="{_e(ask["label"])}"'
    used, limit = s["slots_used"], s["slot_limit"]
    full = used >= limit
    blocked = s["blocked_duplicate_links"]
    nav = "".join(f'<a href="#/{key}" data-pg="{key}">{_icon(ic)}<span class="lbl">{_e(label)}</span>'
                  f'<span class="n" data-count="{key}"></span></a>' for key, label, ic, _ in PAGES)
    by_key = {p[0]: p for p in (*PAGES, SETTINGS_TAB)}
    tabs = "".join(f'<a href="#/{key}" data-pg="{key}">{_icon(by_key[key][2])}<span>{by_key[key][3]}</span></a>'
                   for key in TABS)
    icons = "".join(_icon(name, f' data-icon="{name}"') for name in ICONS)
    notes = []
    if full:
        notes.append(f'<p class="nc warn">All {limit} Plaid slots are used. Linking a new bank is disabled; '
                     f'use Reconnect for existing banks. You can still add accounts by hand.</p>')
    if blocked:
        one = blocked == 1
        notes.append(f'<p class="nc warn" id="blocked-note">{blocked} duplicate Link completion{"" if one else "s"} '
                     f'{"was" if one else "were"} blocked before exchange; Plaid may have counted {"it" if one else "them"} '
                     f'(worst case {used + blocked} / {limit}).</p>')
    return f"""<!doctype html>
<html lang="en" data-theme-pref="{theme}"{region}><head><meta charset="utf-8">
<title>Hermes Finances</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0c0e13">{csrf}
<script src="/static/theme.js"></script>
<meta name="apple-mobile-web-app-title" content="Finances">
<meta name="application-name" content="Hermes Finances">
<link rel="icon" href="/static/icons/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/static/icons/favicon-32.png" sizes="32x32" type="image/png">
<link rel="icon" href="/static/icons/favicon-16.png" sizes="16x16" type="image/png">
<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
<link rel="manifest" href="/static/manifest.webmanifest">
<link rel="preload" href="/static/fonts/InterVariable.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/static/app.css">
{''.join(f'<link rel="stylesheet" href="/static/{n}.css">' for n in FEATURE_FILES)}
<link rel="stylesheet" href="/static/settings.css">
<link rel="stylesheet" href="/static/light.css">
<link rel="stylesheet" href="/static/tap.css">
<script src="{PLAID_LINK_JS}" async></script>
<script src="/static/vendor/chart.umd.min.js" defer></script>
<script src="/static/login.js" defer></script>
<script src="/static/settings.js" defer></script>
<script src="/static/setup.js" defer></script>
<script src="/static/import.js" defer></script>
{''.join(f'<script src="/static/{n}.js" defer></script>' for n in FEATURE_FILES)}
<script src="/static/app.js" defer></script>
</head>
<body data-slots-used="{used}" data-slot-limit="{limit}"{' data-slots-full' if full else ''}{body_flags}>
<a class="skip" href="#page">Skip to content</a>
<aside id="side" aria-label="Sidebar">
 <div class="brand"><img src="/static/icons/icon.svg" alt="" width="30" height="30">Hermes Finances</div>
 <nav class="snav" id="nav" aria-label="Pages">{nav}</nav>
 <section class="sbody" aria-label="Net worth and accounts">
  <div class="sh"><span>Net worth</span><span id="nwn"></span></div>
  <div class="nwbox"><div class="n v" id="snw"><span class="skel sk-num"></span></div>
   <div class="split" id="ssplit"></div><div class="ac"><span id="sas"></span><span id="sli"></span></div></div>
  <div class="sh"><span>Accounts</span><span>by bank</span></div>
  <div id="banks"><span class="skel sk-row"></span><span class="skel sk-row"></span><span class="skel sk-row"></span></div>
 </section>
 <div class="foot">
  <div class="ac slots"><span>Connections</span><span class="v" id="slots">slots used: <b>{used} / {limit}</b></span></div>
  <div class="meter" id="meter" role="progressbar" aria-label="Plaid slots used" aria-valuemin="0"
   aria-valuemax="{limit}" aria-valuenow="{used}"><i style="width:{min(used / limit, 1) * 100:.0f}%"></i></div>
  <button class="cbtn" type="button" id="addacct" data-add="home" aria-haspopup="dialog">{_icon('plus')}Add account</button>
  {''.join(notes)}
  <nav class="snav sfoot" aria-label="Settings"><a href="#/settings" data-pg="settings">{_icon('gear')}<span class="lbl">Settings</span></a></nav>
 </div>
</aside>
<div class="scrim" id="scrim"></div>
<main id="main">
 <header class="top">
  <button class="menu" id="menu" type="button" aria-label="Menu" aria-controls="side" aria-expanded="false">{_icon('menu')}</button>
  <a class="back" id="back" href="#/accounts" hidden aria-label="Back">{_icon('arrowl')}</a>
  <div class="ttlw"><h1 id="title">Overview</h1><p class="syncst" id="syncst"><b class="dot" id="syncdot2"></b><span id="sync2"></span></p></div>
  <div class="mon" id="mon" role="group" aria-label="Month">
   <button type="button" id="mprev" aria-label="Previous month">{_icon('chevl')}</button>
   <span id="monl" aria-live="polite"></span>
   <button type="button" id="mnext" aria-label="Next month">{_icon('chevr')}</button>
  </div>
  <span class="sp"></span>
  <span class="sync" id="syncbox"><b class="dot" id="syncdot"></b><span id="sync"></span></span>
  <button class="syncbtn" type="button" id="syncnow" aria-label="Sync now" aria-haspopup="dialog" aria-expanded="false"
   aria-controls="syncp">{_icon('refresh')}<span class="lbl-d">Sync now</span><span class="lbl-m">Sync</span></button>
 </header>
 <div id="msg" role="status" aria-live="polite"></div>
 <div id="page" tabindex="-1"></div>
</main>
<nav class="tabbar" id="tabbar" aria-label="Pages">{tabs}</nav>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<div class="modal" id="modal"><div class="sheet" id="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title"></div></div>
<div class="dscrim" id="dscrim"></div>
<section class="panel" id="panel" role="dialog" aria-modal="true" aria-labelledby="panel-title" hidden></section>
<div class="syncscrim" id="syncscrim" hidden></div>
<section class="syncp" id="syncp" role="dialog" aria-labelledby="syncp-title" tabindex="-1" hidden></section>
<template id="icons">{icons}</template>
<template id="art">{ART}</template>
<noscript><p class="noscript">This dashboard needs JavaScript. Slots used: {used} / {limit}.</p></noscript>
</body></html>"""


def render_login_page(setup: bool = False, https: bool = True) -> str:
    """The built-in login's own page (no app data, no Plaid script): sign in, or on this computer while no login
    exists yet, set it up. Drawn by /static/login.js."""
    icons = "".join(_icon(name, f' data-icon="{name}"') for name in ICONS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Sign in · Hermes Finances</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0c0e13">
<meta name="robots" content="noindex">
<script src="/static/theme.js"></script>
<link rel="icon" href="/static/icons/favicon.svg" type="image/svg+xml">
<link rel="preload" href="/static/fonts/InterVariable.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/static/app.css">
<link rel="stylesheet" href="/static/settings.css">
<link rel="stylesheet" href="/static/light.css">
<link rel="stylesheet" href="/static/tap.css">
<script src="/static/login.js" defer></script>
</head>
<body class="board signin" data-mode="{'setup' if setup else 'signin'}"{'' if https else ' data-plain-http'}>
<main id="main"><div id="page" tabindex="-1"></div></main>
<template id="icons">{icons}</template>
<noscript><p class="noscript">Signing in needs JavaScript.</p></noscript>
</body></html>"""


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hermes-finance"

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            limit = MAX_IMPORT_BODY if self.path.startswith("/api/import/") else MAX_BODY
            extra: list[tuple[str, str]] = []
            if length > limit:
                status, ctype, body = App._json(413, {"error": "request too large"})
            else:
                raw = self.rfile.read(length) if length else b""
                status, ctype, body, extra = app.respond(method, self.path, dict(self.headers.items()), raw,
                                                         self.client_address[0] if self.client_address else None)
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                # a download (admin.FileBody: the backup, the CSV export) is streamed from its temp file
                size = body.size if isinstance(body, admin.FileBody) else len(body)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", App.cache_control(self.path, status))
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                if isinstance(body, admin.FileBody):
                    with body.open() as f:
                        shutil.copyfileobj(f, self.wfile, 256 * 1024)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError) as e:
                # HOTFIX-1: the client (a phone through the proxy) went away before the answer: one line, not a
                # traceback (a sync it started goes on; the page looks it up with GET /api/sync)
                self.close_connection = True
                syncnow.requests_log.info("%s %s: the client went away before the %d answer (%s)", method,
                                          redact.scrub(self.path.partition("?")[0]), status, type(e).__name__)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):  # TASK-19: DELETE /api/alpaca/key only (every other path: 404)
            self._dispatch("DELETE")

        def log_message(self, fmt, *args):  # request line only (no bodies), redacted anyway
            log.info("%s %s", self.address_string(), redact.scrub(fmt % args))

    return Handler


class _Server(ThreadingHTTPServer):
    # a page load asks for a couple of dozen files at once (TASK-18's and TASK-19's own scripts and styles);
    # socketserver's default backlog of 5 refused some of them (connection reset) while the first were answered
    request_queue_size = 64


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    if not app.may_bind(host):
        raise ValueError(f"refusing to listen on {host}: only loopback addresses are allowed "
                         "(a non-loopback address needs the built-in login set up)")
    server = _Server((host, port), make_handler(app))
    app.port = server.server_address[1]
    return server
