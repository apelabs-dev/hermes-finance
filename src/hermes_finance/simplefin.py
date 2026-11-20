"""SimpleFIN Bridge client (protocol version 2), on the standard library only.

The access URL is a secret. It carries HTTP Basic credentials in its user info, so this module splits it into a
credential-free base URL and an Authorization header: the credentials never appear in a request URL, a log line, an
exception or a repr. `secretstore` keeps it (0600, never in the database); everything read or written here is
registered with `redact`.

The setup token is a one-time base64 URL: it is shape-checked before it is decoded, decoded only to an https claim URL
with no query or fragment, and the claim POST follows no redirects and caps the answer. Every host is resolved at
connect time and every address must be global (the SSRF guard); injections let tests run without a resolver or a
socket.

`/accounts?version=2` is read with Basic auth, no redirects, a 10 s timeout and a response cap. 402 (subscription) and
403 (revoked) are `Disconnected`, never empty data; v2's `errlist` is parsed on both, sanitized, and `gen.api` entries
are marked as developer errors so a page never reports them as a broken connection.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable

from . import redact

log = logging.getLogger(__name__)

OFF_ENV = "HERMES_SIMPLEFIN"          # "off": the real transport refuses (tests and every process a test starts)
DEFAULT_TIMEOUT = 10.0
TIMEOUT = DEFAULT_TIMEOUT
MAX_TOKEN_CHARS = 4096
MAX_CLAIM_BYTES = 4096
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ERROR_CHARS = 240
USER_AGENT = "hermes-finance/0.1 (SimpleFIN; one request at a time)"
TOKEN_RE = re.compile(r"[A-Za-z0-9+/_-]+={0,2}\Z")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
URL_RE = re.compile(r"(?:https?|ftp)://\S+|www\.\S+", re.IGNORECASE)
CLAIM_403 = "This token was already used. Create a new one at SimpleFIN Bridge."
REVOKED = ("SimpleFIN stopped accepting this access link, so it was revoked or expired. Balances stay as of the last "
           "sync. Paste a new setup token to resume.")
SUBSCRIPTION = "SimpleFIN needs an active subscription. Balances stay as of the last sync."
QUOTA = "SimpleFIN's request limit is used up for now. Nothing was changed; the next daily sync tries again."


class SimpleFinError(Exception):
    """A SimpleFIN answer we can't use. The message is safe to show: never a URL, a token or a password."""


class InvalidToken(SimpleFinError):
    """The pasted setup token, or the access URL it decodes to, is not acceptable."""


class TransportError(SimpleFinError):
    """The network itself failed (no DNS, refused, a timeout). Nothing was read."""


class Disconnected(SimpleFinError):
    """The connection is over until the owner acts: 402 subscription, 403 revoked, or the provider's request quota.

    `reason` is "subscription", "revoked" or "quota". No balance is ever replaced by zeros on this path; the caller
    keeps the last sync's rows and says when they are from.
    """

    def __init__(self, reason: str, message: str, errors: list["Error"] | None = None):
        self.reason = reason
        self.errors = list(errors or [])
        super().__init__(redact.scrub(message))


@dataclass(frozen=True)
class Error:
    code: str
    message: str
    user: bool = True
    conn_id: str | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


# transport(method, url, headers, body, timeout) -> Response
Transport = Callable[[str, str, dict, bytes | None, float], Response]
# resolver(host, port) -> getaddrinfo-shaped list; tests inject one so nothing resolves a name
Resolver = Callable[..., list]


# --- secrets -------------------------------------------------------------------------------------------

def scrub_message(value: Any) -> str:
    """A SimpleFIN errlist message as a person may read it: control characters gone, links stripped, in one line, capped."""
    text = CONTROL_RE.sub(" ", str(value or ""))
    text = URL_RE.sub("[link removed]", text)
    text = " ".join(text.split())
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 1].rstrip() + "…"
    return text or "SimpleFIN reported a problem."


def _scrub_id(value: Any) -> str | None:
    """An errlist conn_id/account_id, scrubbed like a message (control characters, links, one line) and capped."""
    text = CONTROL_RE.sub(" ", str(value or ""))
    text = URL_RE.sub("[link removed]", text)
    text = " ".join(text.split())
    return text[:80].rstrip() or None


def parse_errors(payload: Any) -> list[Error]:
    """v2 `errlist` (structured) first, then the deprecated `errors` (strings). Unknown subcodes read as their prefix;
    `gen.api` is a developer error (`user=False`), never shown as a broken connection. A `con.*` entry's `conn_id` and
    an `act.*` entry's `account_id` are kept so an answer that names a connection is known to be partial."""
    out: list[Error] = []
    if not isinstance(payload, dict):
        return out
    raw = payload.get("errlist")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "gen").strip()[:80] or "gen"
            out.append(Error(code, scrub_message(item.get("msg")), user=code != "gen.api",
                             conn_id=_scrub_id(item.get("conn_id")),
                             account_id=_scrub_id(item.get("account_id"))))
    legacy = payload.get("errors")
    if isinstance(legacy, list):
        for item in legacy:
            out.append(Error("gen", scrub_message(item), user=True))
    return out


def _is_quota(errors: list[Error]) -> bool:
    """The provider says the request quota is used up / the token is throttled (never the 90-day `gen.api` notice)."""
    for e in errors:
        if not e.user:
            continue
        text = e.message.lower()
        if ("rate limit" in text or "quota" in text or "too many requests" in text
                or ("request limit" in text) or ("limit exceeded" in text)) and "date range" not in text:
            return True
    return False


# --- URLs and the SSRF guard --------------------------------------------------------------------------

def _resolve(resolve: Resolver | None) -> Resolver:
    return resolve or socket.getaddrinfo


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _public(host: str, port: int | None, resolve: Resolver | None = None) -> None:
    """Every address the host resolves to must be global. Runs at claim time and again before every fetch, so a
    DNS answer that changed between the two cannot slip past. localhost, private, link-local, metadata, reserved and
    unspecified addresses are all refused."""
    host = (host or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise InvalidToken("SimpleFIN's address is not a public host.")
    try:
        literal = ipaddress.ip_address(host.strip("[]").split("%")[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise InvalidToken("SimpleFIN's address is not a public host.")
        return
    try:
        infos = _resolve(resolve)(host, port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError, ValueError):
        raise InvalidToken("SimpleFIN's address can't be resolved.") from None
    if not infos:
        raise InvalidToken("SimpleFIN's address can't be resolved.")
    for info in infos:
        try:
            address = str(info[4][0]).split("%")[0]
            ip = ipaddress.ip_address(address)
        except (IndexError, TypeError, ValueError):
            raise InvalidToken("SimpleFIN's address can't be resolved.") from None
        if not ip.is_global:
            raise InvalidToken("SimpleFIN's address is not a public host.")


@dataclass(frozen=True)
class Access:
    """A parsed access URL: the credential-free base and the Basic header built from its user info."""
    base: str
    auth: str

    def __repr__(self) -> str:
        return f"Access({urllib.parse.urlsplit(self.base).hostname!r})"


def parse_access_url(url: Any, resolve: Resolver | None = None) -> Access:
    """https://user:password@host/path (no query, no fragment, a global host) -> Access. The URL is registered with
    redact before it is split, and the credentials never enter `base`."""
    if not isinstance(url, str) or not url.strip():
        raise InvalidToken("SimpleFIN didn't return an access link.")
    text = url.strip()
    redact.register(text)
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise InvalidToken("SimpleFIN returned an invalid access link.") from None
    if (parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or not parts.path
            or parts.username is None or parts.password is None):
        raise InvalidToken("SimpleFIN returned an invalid access link.")
    _public(parts.hostname, port, resolve)
    user = urllib.parse.unquote(parts.username)
    password = urllib.parse.unquote(parts.password)
    if not user or not password:
        raise InvalidToken("SimpleFIN returned an invalid access link.")
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    base = f"https://{host}{f':{port}' if port is not None else ''}{parts.path}"
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    return Access(base, auth)


def _validate_token(token: Any) -> str:
    if not isinstance(token, str):
        raise InvalidToken("Paste the setup token from SimpleFIN Bridge.")
    text = token.strip()
    if not text or len(text) > MAX_TOKEN_CHARS or not TOKEN_RE.fullmatch(text):
        raise InvalidToken("That doesn't look like a SimpleFIN setup token. Paste it exactly as SimpleFIN Bridge "
                           "shows it.")
    return text


def _decode_token(token: str) -> str:
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(padded.encode())
        except (ValueError, TypeError):
            continue
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
    raise InvalidToken("That doesn't look like a SimpleFIN setup token. Paste it exactly as SimpleFIN Bridge shows it.")


def _claim_url(token: str, resolve: Resolver | None) -> str:
    decoded = _decode_token(token).strip()
    try:
        parts = urllib.parse.urlsplit(decoded)
        port = parts.port
    except ValueError:
        raise InvalidToken("That setup token doesn't contain a SimpleFIN claim link.") from None
    if (parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None):
        raise InvalidToken("That setup token doesn't contain a SimpleFIN claim link.")
    _public(parts.hostname, port, resolve)
    return decoded


# --- the real transport -------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, never a hop: the access URL's credentials must not travel to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> Response:
    """The one real network call. Refuses while HERMES_SIMPLEFIN=off (tests and every process a test starts)."""
    if os.environ.get(OFF_ENV) == "off":
        raise TransportError("SimpleFIN is switched off (HERMES_SIMPLEFIN=off).")
    host = _host(url)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(MAX_CLAIM_BYTES + 1) if e.code in (402, 403, 429) else b""
        except (OSError, ValueError):
            raw = b""
        finally:
            try:
                e.close()
            except Exception:
                pass
        if len(raw) > MAX_CLAIM_BYTES:
            raw = raw[:MAX_CLAIM_BYTES]
        return Response(e.code, raw)
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError) as e:
        raise TransportError(f"Couldn't reach SimpleFIN ({type(e).__name__}).") from None
    with resp:
        status = getattr(resp, "status", 0) or 0
        length = (resp.headers.get("Content-Length") or "").strip()
        if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
            raise SimpleFinError("SimpleFIN returned too much data.")
        try:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError) as e:
            raise TransportError(f"Couldn't read SimpleFIN's answer ({type(e).__name__}).") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SimpleFinError("SimpleFIN returned too much data.")
        return Response(status, raw)


# --- claim --------------------------------------------------------------------------------------------

def claim(token: Any, transport: Transport | None = None, timeout: float = DEFAULT_TIMEOUT,
          resolve: Resolver | None = None) -> str:
    """Trade a one-time setup token for the access URL. 403 means it was already used or revoked."""
    text = _validate_token(token)
    redact.register(text)
    url = _claim_url(text, resolve)
    transport = transport or urllib_transport
    try:
        response = transport("POST", url, {"Accept": "text/plain", "User-Agent": USER_AGENT}, None, timeout)
    except SimpleFinError:
        raise
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError) as e:
        raise TransportError(f"Couldn't reach SimpleFIN Bridge ({type(e).__name__}).") from None
    status = getattr(response, "status", 0)
    if status in (301, 302, 303, 307, 308):
        raise SimpleFinError("SimpleFIN's claim endpoint redirected; refusing to follow it.")
    if status == 403:
        raise InvalidToken(CLAIM_403)
    if status != 200:
        raise SimpleFinError(f"Couldn't claim the setup token (HTTP {status}).")
    body = getattr(response, "body", b"") or b""
    if len(body) > MAX_CLAIM_BYTES:
        raise InvalidToken("SimpleFIN's answer was too large to be an access link.")
    answer = body.decode("utf-8", "replace").strip()
    if "\n" in answer or CONTROL_RE.search(answer):
        raise InvalidToken("SimpleFIN didn't return an access link.")
    parse_access_url(answer, resolve)   # validate (and register) before it is stored
    return answer                       # the original, credentialed access URL; the caller keeps it in secretstore


# --- /accounts ----------------------------------------------------------------------------------------

@dataclass
class Connection:
    conn_id: str
    name: str = ""
    org_name: str = ""
    org_url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Tx:
    id: str
    date: str                 # YYYY-MM-DD (posted date, or the pending date)
    amount_cents: int         # OUR sign: positive = money out (SimpleFIN's amount negated)
    description: str = ""
    pending: bool = False
    posted: int | None = None
    transacted_at: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Account:
    id: str
    conn_id: str
    connection_name: str
    name: str
    currency: str | None
    balance_cents: int
    available_cents: int | None
    balance_date: str | None
    has_holdings: bool
    mask: str | None
    transactions: list[Tx] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class Payload:
    accounts: list[Account]
    connections: dict[str, Connection]
    errors: list[Error]
    raw: dict = field(default_factory=dict)


def cents(value: Any) -> int:
    """A SimpleFIN decimal string (or number) in whole cents, exactly: Decimal, never a float. Half a cent rounds up."""
    if isinstance(value, bool) or value is None:
        raise InvalidOperation("not an amount")
    text = str(value).strip()
    if not text:
        raise InvalidOperation("empty amount")
    d = Decimal(text)
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def units(cents_value: int) -> float:
    return cents_value / 100.0


def money(cents_value: int) -> str:
    sign = "-" if cents_value < 0 else ""
    value = abs(int(cents_value))
    return f"{sign}{value // 100}.{value % 100:02d}"


def _epoch_day(value: Any) -> str | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()


MASK_RE = re.compile(r"(?:^|\D)(\d{4,})\D*$")


def mask_of(name: Any) -> str | None:
    """The last four digits the name happens to end with (the protocol has no mask), else None."""
    m = MASK_RE.search(str(name or ""))
    return m[1][-4:] if m else None


def guess_type(name: str, holdings: bool, balance: int, available: int | None) -> tuple[str, str | None, str]:
    """A type suggestion for the confirm step. It is never stored without the owner's choice.

    holdings -> investment (high); loan / mortgage / HELOC -> loan; credit, card, visa, mastercard, amex, discover ->
    credit; checking / savings -> depository; a negative balance with money available -> credit (medium); anything else
    -> other, low, "Check type"."""
    low = " ".join(str(name or "").lower().split())
    if holdings:
        return "investment", "brokerage", "high"
    if any(w in low for w in ("heloc",)):
        return "loan", "mortgage", "high"
    if "mortgage" in low:
        return "loan", "mortgage", "high"
    if "student" in low and "loan" in low:
        return "loan", "student", "high"
    if any(w in low for w in ("auto loan", "car loan")) or ("auto" in low and "loan" in low):
        return "loan", "auto", "high"
    if "loan" in low:
        return "loan", "other", "high"
    if any(w in low for w in ("credit", "card", "visa", "mastercard", "master card", "amex", "american express",
                               "discover")):
        return "credit", "credit card", "high"
    if "savings" in low:
        return "depository", "savings", "high"
    if "checking" in low or "chequing" in low:
        return "depository", "checking", "high"
    if balance < 0 and available is not None and available > 0:
        return "credit", "credit card", "medium"
    return "other", None, "low"


def normalize(payload: Any) -> Payload:
    """The v2 Account Set: connections by conn_id, accounts joined to their connection, transactions in our Plaid
    sign. Unknown keys are ignored; `holdings` is kept only as the type hint (nothing is imported from it)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list):
        raise SimpleFinError("SimpleFIN returned data in an unexpected format.")
    errors = parse_errors(payload)
    connections: dict[str, Connection] = {}
    raw_connections = payload.get("connections")
    if isinstance(raw_connections, list):
        for item in raw_connections:
            if not isinstance(item, dict) or not item.get("conn_id"):
                continue
            conn_id = str(item["conn_id"])
            connections[conn_id] = Connection(conn_id, str(item.get("name") or ""), str(item.get("org_name") or ""),
                                              str(item.get("org_url") or ""), raw=item)
    accounts: list[Account] = []
    for item in payload["accounts"]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        conn_id = str(item.get("conn_id") or "")
        connection = connections.get(conn_id)
        txs: list[Tx] = []
        for t in item.get("transactions") or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            posted = t.get("posted")
            when = _epoch_day(t.get("transacted_at")) or _epoch_day(posted) or _epoch_day(item.get("balance-date"))
            if when is None:
                continue
            try:
                amount = -cents(t.get("amount"))
            except (InvalidOperation, ValueError):
                continue
            txs.append(Tx(str(t["id"]), when, amount, str(t.get("description") or ""),
                          bool(t.get("pending")) or not posted or int(posted or 0) <= 0,
                          int(posted) if posted else None, int(t["transacted_at"]) if t.get("transacted_at") else None,
                          raw=t))
        try:
            balance = cents(item.get("balance") or 0)
            available = cents(item["available-balance"]) if item.get("available-balance") not in (None, "") else None
        except (InvalidOperation, ValueError):
            raise SimpleFinError("SimpleFIN returned an account amount in an unexpected format.") from None
        accounts.append(Account(
            id=str(item["id"]), conn_id=conn_id, connection_name=(connection.name if connection else ""),
            name=str(item.get("name") or ""), currency=(str(item["currency"]) if item.get("currency") else None),
            balance_cents=balance, available_cents=available, balance_date=_epoch_day(item.get("balance-date")),
            has_holdings=bool(item.get("holdings")), mask=mask_of(item.get("name")), transactions=txs, raw=item))
    return Payload(accounts, connections, errors, raw=payload)


class Client:
    """The /accounts reader for one access URL. `transport` and `resolve` are injected in tests."""

    def __init__(self, access: Access | str, transport: Transport | None = None, timeout: float = DEFAULT_TIMEOUT,
                 resolve: Resolver | None = None):
        self.access = access if isinstance(access, Access) else parse_access_url(access, resolve)
        self.transport = transport or urllib_transport
        self.timeout = timeout
        self.resolve = resolve

    def __repr__(self) -> str:
        return "Client(<simplefin>)"

    @property
    def host(self) -> str:
        return _host(self.access.base)

    def accounts(self, start: date | str, end: date | str | None = None, pending: bool = True) -> Payload:
        """One /accounts?version=2 call. `start`/`end` are dates; end is optional. 402/403/a quota answer raise
        Disconnected, never a payload with zeroed balances."""
        params = {"version": "2", "pending": "1" if pending else "0",
                  "start-date": str(_epoch(start))}
        if end is not None:
            params["end-date"] = str(_epoch(end))
        url = self.access.base.rstrip("/") + "/accounts?" + urllib.parse.urlencode(params)
        host = _host(self.access.base)
        _public(host, urllib.parse.urlsplit(self.access.base).port, self.resolve)
        headers = {"Authorization": self.access.auth, "Accept": "application/json", "User-Agent": USER_AGENT}
        try:
            response = self.transport("GET", url, headers, None, self.timeout)
        except SimpleFinError:
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError) as e:
            raise TransportError(f"Couldn't reach SimpleFIN ({type(e).__name__}).") from None
        status = getattr(response, "status", 0)
        body = getattr(response, "body", b"") or b""
        if len(body) > MAX_RESPONSE_BYTES:
            raise SimpleFinError("SimpleFIN returned too much data.")
        if status in (402, 403, 429):
            errors = _json_errors(body)
            if status == 402:
                raise Disconnected("subscription", SUBSCRIPTION, errors)
            if status == 429:
                raise Disconnected("quota", QUOTA, errors)
            raise Disconnected("revoked", REVOKED, errors)
        if status != 200:
            raise SimpleFinError(f"SimpleFIN error (HTTP {status}).")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SimpleFinError("SimpleFIN returned an invalid response.") from None
        got = normalize(payload)
        if not got.accounts and not got.connections and _is_quota(got.errors):
            raise Disconnected("quota", QUOTA, got.errors)
        return got


def _json_errors(body: bytes) -> list[Error]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        text = scrub_message(body.decode("utf-8", "replace")[:MAX_ERROR_CHARS])
        return [Error("gen", text)] if text else []
    return parse_errors(payload)


def _epoch(value: date | str) -> int:
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return int(datetime.combine(d, time.min, tzinfo=timezone.utc).timestamp())


def fetch_accounts(client: Client, start: date | str, end: date | str | None = None,
                   pending: bool = True) -> Payload:
    """The module-level spelling of Client.accounts (one request)."""
    return client.accounts(start, end, pending)
