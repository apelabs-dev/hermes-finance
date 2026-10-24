"""Who can open the app (TASK-18, Settings -> Access & security): three modes, one at a time.

  local    "This computer only" (a new install's default): the server binds 127.0.0.1 and answers only requests for
           a localhost Host that came through no proxy. No login.
  proxy    "Behind your proxy login": a proxy on this computer (Tailscale Serve, Cloudflare Access, Authelia,
           oauth2-proxy) signs people in and forwards the request with the signed-in identity in a trusted header
           (default Tailscale-User-Login). Requests for one of the public URLs must carry an allowed identity; the
           localhost rules are as in `local`. This was the only mode before TASK-18: an install started with
           --public-url becomes this mode on its first start, its --allow-user / FINANCE_ALLOWED_USERS logins seeding
           the allowed identities (there is no built-in default login any more).
  builtin  "Built-in login": for public hosting. Every request needs a session from the built-in login (auth.py:
           password + authenticator app), whatever its Host; /mcp needs a bearer token.

The server refuses to start when it would be reachable from other devices (a non-loopback --host, or public URLs that
proxied requests arrive on) and no mode protects it: the proxy mode needs at least one allowed identity, the built-in
login a finished set-up. The proxy mode also refuses a non-loopback bind: it trusts a header that anyone reaching the
port directly could send.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import prefs

log = logging.getLogger(__name__)

MODES = prefs.ACCESS_MODES
MODE_NAMES = {"local": "This computer only", "proxy": "Behind your proxy login", "builtin": "Built-in login"}
DEFAULT_HEADER = "Tailscale-User-Login"
# the proxies the Settings page names, by the header they add (lowercase)
VIA = {"tailscale-user-login": "Tailscale", "cf-access-authenticated-user-email": "Cloudflare Access",
       "remote-user": "Authelia", "remote-email": "Authelia", "x-forwarded-email": "oauth2-proxy",
       "x-forwarded-user": "oauth2-proxy", "x-auth-request-email": "oauth2-proxy"}
LAST_PROXY_EVERY = 60     # seconds between writes of "last request via ..."
REFUSAL_LOG_EVERY = 60    # seconds between log lines about refused proxied requests in `local` mode


class AccessError(ValueError):
    def __init__(self, message: str, field: str | None = None, status: int = 400):
        super().__init__(message)
        self.field = field
        self.status = status


class StartRefused(Exception):
    """`finance serve` must not start: the message says why and what to do."""


@dataclass
class Access:
    mode: str
    header: str
    identities: set[str]
    public_urls: list[str]
    public_origins: set[str] = field(default_factory=set)
    public_hosts: set[str] = field(default_factory=set)
    saved_mode: str | None = None       # None: never chosen (a new install, or before the first start)

    @property
    def header_key(self) -> str:
        return self.header.lower()


def canonical_identity(identity: str) -> str:
    """The exact identity admitted by the proxy policy: surrounding whitespace removed and Unicode lowercased.
    Do not broaden this to casefolding: separately admitted identities such as straße and strasse stay distinct."""
    return identity.strip().lower()


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.removeprefix("[").removesuffix("]")).is_loopback
    except ValueError:
        return False


def mask(identity: str) -> str:
    """yo•••@example.com: the first two characters, then dots up to the domain (a bare login keeps two letters too)."""
    name, at, domain = identity.partition("@")
    return f"{name[:2]}•••{at}{domain}"


def identity_id(identity: str) -> str:
    """A stable handle for one identity, so the page can remove it without ever holding the whole value."""
    return hashlib.sha256(canonical_identity(identity).encode()).hexdigest()[:12]


def via(header: str) -> str:
    return VIA.get(header.lower(), header)


def load(conn: sqlite3.Connection, flag_urls: Iterable[str] = (), flag_users: Iterable[str] = ()) -> Access:
    """The access settings with the command line's --public-url / --allow-user on top (flags are never saved here;
    `startup` saves them once). A mode never chosen is `proxy` when there are public URLs, else `local`."""
    from .web import parse_public_url  # the URL rules live with the rest of the HTTP gate
    v = prefs.get_many(conn, ["access.mode", "access.proxy_header", "access.allowed_identities", "access.public_urls"])
    urls = list(dict.fromkeys([*v["access.public_urls"], *flag_urls]))
    users = {canonical_identity(u) for u in [*v["access.allowed_identities"], *flag_users] if u.strip()}
    saved = v["access.mode"]
    mode = saved or ("proxy" if urls else "local")
    acc = Access(mode, v["access.proxy_header"], users, urls, saved_mode=saved)
    for url in urls:
        origin, hosts = parse_public_url(url)
        acc.public_origins.add(origin)
        acc.public_hosts |= hosts
    return acc


def _save(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    prefs.save(conn, values, allowed=[k for k in prefs.SPEC if k.startswith("access.")])


def migrate(conn: sqlite3.Connection, flag_urls: list[str], flag_users: list[str]) -> str | None:
    """The one-time move of today's gate into a mode, on a start where no mode was chosen yet: public URLs (a
    --public-url or FINANCE_PUBLIC_URLS) make it `proxy`, seeded with them and the --allow-user / FINANCE_ALLOWED_USERS
    logins; otherwise `local`. Returns the mode it chose (None: a mode was already chosen). A proxy mode whose list is
    still empty (a first start without --allow-user, refused) is seeded by the next start that has them."""
    saved = prefs.get_many(conn, ["access.mode", "access.allowed_identities", "access.public_urls"])
    if saved["access.mode"] is not None:
        seed = {}
        if saved["access.mode"] == "proxy" and not saved["access.allowed_identities"] and flag_users:
            seed["access.allowed_identities"] = [canonical_identity(u) for u in flag_users if u.strip()]
        if saved["access.mode"] == "proxy" and not saved["access.public_urls"] and flag_urls:
            seed["access.public_urls"] = list(dict.fromkeys(flag_urls))
        if seed:
            _save(conn, seed)
        return None
    if flag_urls:
        # keep identities saved before the first start (`finance access allow <login>`), then add the flag's
        users = [canonical_identity(u) for u in [*saved["access.allowed_identities"], *flag_users] if u.strip()]
        _save(conn, {"access.mode": "proxy", "access.public_urls": list(dict.fromkeys(flag_urls)),
                     "access.allowed_identities": list(dict.fromkeys(users))})
        return "proxy"
    _save(conn, {"access.mode": "local"})
    return "local"


def startup_problems(acc: Access, host: str, login_ready: bool) -> list[str]:
    """Why `finance serve --host <host>` with these settings must not start (empty: it may)."""
    exposed = not is_loopback(host)
    out = []
    if acc.mode == "local":
        if exposed:
            out.append(f"refusing to listen on {host}: Access is “This computer only”, so the app only binds a "
                       f"loopback address (127.0.0.1). To open it from other devices, choose “Behind your proxy "
                       f"login” (e.g. tailscale serve) or “Built-in login” in Settings → Access & "
                       f"security first:\n  tailscale serve --bg --https=8765 http://127.0.0.1:8765\n"
                       f"  finance serve --public-url https://<machine>.<tailnet>.ts.net:8765 --allow-user <your login>")
        if acc.public_urls:
            out.append("public URLs are set (--public-url / FINANCE_PUBLIC_URLS), so requests would arrive through a "
                       "proxy, but Access is “This computer only”. Choose “Behind your proxy login” "
                       "(with at least one allowed identity) or “Built-in login” in Settings → Access & "
                       "security, or start without --public-url.")
    elif acc.mode == "proxy":
        if exposed:
            out.append(f"refusing to listen on {host}: “Behind your proxy login” trusts the "
                       f"{acc.header} header, which anyone reaching the port directly could send. Bind 127.0.0.1 and let "
                       f"the proxy on this computer forward to it (e.g. tailscale serve).")
        if not acc.identities:
            out.append("“Behind your proxy login” has no allowed identity, so no proxied request could be "
                       "let in. Pass --allow-user <your login> once (it's saved), run `finance access allow <login>`, "
                       "or add one in Settings → Access & security on this computer.")
    elif acc.mode == "builtin" and not login_ready and (exposed or acc.public_urls):
        out.append("“Built-in login” is chosen but its set-up isn't finished (no password and "
                   "authenticator yet), and the app would be reachable from other devices. Finish it on this computer "
                   "(start without --host / --public-url and open http://127.0.0.1:8765) first, or run "
                   "`finance access mode local`.")
    return out


# --- runtime helpers -----------------------------------------------------------------------------------------------

_last_proxy: dict[str, float] = {}
_last_refusal = [0.0]
_state_lock = threading.Lock()


def record_proxy_request(conn: sqlite3.Connection, acc: Access, identity: str, host: str) -> None:
    """"Last request via Tailscale · yo•••@example.com" for Settings: written at most once a minute per identity."""
    key = canonical_identity(identity)
    t = time.time()
    with _state_lock:
        if _last_proxy.get(key, 0) + LAST_PROXY_EVERY > t:
            return
        _last_proxy[key] = t
    _save(conn, {"access.last_proxy": {"identity": key, "via": via(acc.header), "host": host,
                                       "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))}})


def log_refused_proxy(acc: Access, why: str) -> None:
    """In `local` mode a proxied request is refused; say so in the log (at most once a minute) so the owner sees why
    a phone gets 403."""
    t = time.time()
    with _state_lock:
        if _last_refusal[0] + REFUSAL_LOG_EVERY > t:
            return
        _last_refusal[0] = t
    log.error("refused a proxied request (%s): Access is “This computer only”. To open the app from other "
              "devices choose “Behind your proxy login” or “Built-in login” in Settings → "
              "Access & security.", why)


def summary(conn: sqlite3.Connection, acc: Access, host: str, port: int) -> dict[str, Any]:
    """The Access & security card's data. Identities only masked (with an id to remove one by)."""
    last = prefs.get(conn, "access.last_proxy")
    from . import auth
    return {"mode": acc.mode, "chosen": acc.saved_mode is not None, "proxy_header": acc.header,
            # what the next `finance serve` with these settings would refuse (the red line under the choice)
            "problems": startup_problems(acc, host, auth.configured(conn)),
            "identities": [{"id": identity_id(u), "masked": mask(u)} for u in sorted(acc.identities)],
            "public_urls": acc.public_urls, "bind": f"{host}:{port}", "exposed": not is_loopback(host),
            "last_proxy": {"via": last.get("via"), "masked": mask(last.get("identity", "")), "at": last.get("at"),
                           "host": last.get("host")} if isinstance(last, dict) and last.get("identity") else None}


def set_mode(conn: sqlite3.Connection, mode: Any, login_ready: bool, acc: Access) -> None:
    """Choose a mode from Settings. `proxy` needs at least one allowed identity; `builtin` a finished set-up (its
    set-up switches to it itself)."""
    if mode not in MODES:
        raise AccessError(f"mode must be one of {', '.join(MODES)}", "mode")
    if mode == "proxy" and not acc.identities:
        raise AccessError("Add at least one allowed identity first: the proxy's signed-in login that may open the app.",
                          "identity", 409)
    if mode == "builtin" and not login_ready:
        raise AccessError("Set up the built-in login first (a password and an authenticator app).", "mode", 409)
    _save(conn, {"access.mode": mode})


def set_proxy(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    """The proxy mode's fields: the trusted header, the public URLs."""
    from .web import parse_public_url
    values = {}
    if "proxy_header" in data:
        values["access.proxy_header"] = data["proxy_header"]
    if "public_urls" in data:
        urls = data["public_urls"]
        if not isinstance(urls, list):
            raise AccessError("public_urls must be a list of URLs", "public_urls")
        for u in urls:
            try:
                parse_public_url(str(u))
            except ValueError as e:
                raise AccessError(str(e), "public_urls") from None
        values["access.public_urls"] = urls
    if not values:
        raise AccessError("nothing to change (proxy_header, public_urls)")
    try:
        _save(conn, values)
    except prefs.PrefError as e:
        raise AccessError(str(e), e.field.split(".")[-1] if e.field else None) from None


def add_identity(conn: sqlite3.Connection, identity: Any) -> None:
    try:
        clean = prefs.SPEC["access.allowed_identities"].check([identity])[0]
    except (prefs.PrefError, IndexError) as e:
        raise AccessError(f"That isn't an identity: {e}", "identity") from None
    now_ = prefs.get(conn, "access.allowed_identities")
    if clean in now_:
        return
    if len(now_) >= prefs.MAX_LIST:
        raise AccessError(f"At most {prefs.MAX_LIST} identities.", "identity")
    _save(conn, {"access.allowed_identities": [*now_, clean]})


def remove_identity(conn: sqlite3.Connection, ident: Any, acc: Access) -> None:
    """By id (identity_id). The last one can't go while the proxy mode is on: that would lock everyone out."""
    now_ = prefs.get(conn, "access.allowed_identities")
    keep = [u for u in now_ if identity_id(u) != ident]
    if len(keep) == len(now_):
        raise AccessError("No such identity (a --allow-user one can only be removed from the command line).", "identity",
                          404)
    if acc.mode == "proxy" and not keep and not (acc.identities - set(now_)):
        raise AccessError("That's the last allowed identity. Add another one, or choose another mode first.",
                          "identity", 409)
    _save(conn, {"access.allowed_identities": keep})
