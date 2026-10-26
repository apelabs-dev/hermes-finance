"""The Alpaca live brokerage account, read-only through its API (TASK-19).

What it may ask, and nothing else, ever: GET https://api.alpaca.markets/v2/account, /v2/positions and
/v2/account/portfolio/history. `_get()` is the only place a request is made; it checks the (method, path) pair against
ALLOWED before anything else and refuses every other call. The host is fixed to the live one, so a paper key can't
count (paper money never enters net worth): a key file whose APCA_ENV isn't "live", whose base URL isn't the live host,
or whose key id is a paper one (PK...) is refused before any request.

The key file (config.alpaca_key_path(): ~/.config/hermes-finance/alpaca-live.env unless HERMES_ALPACA_KEY_FILE names
another) holds APCA_API_KEY_ID, APCA_API_SECRET_KEY and APCA_ENV. The dashboard's Connect form writes it (connect():
one GET /v2/account on the live host first; only a 200 for an ACTIVE account is accepted, then the file is written
0600 in a 0700 folder, atomically) and its Remove button deletes it (disconnect()). Without it nothing is shown,
counted or asked. The secret is never sent back, logged or exported. HERMES_ALPACA=off switches every request off (the
tests and the gates set it; a process they start inherits it).

Hermes mode (config.hermes_credentials(), catalog rule 11): the same three values come from Hermes (hermes config set
APCA_API_KEY_ID / APCA_API_SECRET_KEY / APCA_ENV) and the key file is never opened or looked at. Connect and Remove are
refused there (the keys are Hermes's), and a sync without keys does nothing: it never leaves saved snapshots out.

`finance alpaca-sync` (the same daily run as `finance revalue`) saves one snapshot a day: the account's status, cash,
equity = cash + the positions' market value, and each position (quantity, price, value). Net worth counts the latest
snapshot, and only while Alpaca says the account is ACTIVE: any other status is shown as an error and counts nothing.
Once the key is removed (or its file is gone at the next sync) every snapshot is marked not counted, and the account
leaves every page, total and history point.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

from . import config, redact
from .money import money
from .db import now

log = logging.getLogger(__name__)

LIVE_HOST = "api.alpaca.markets"
ALLOWED = frozenset({("GET", "/v2/account"), ("GET", "/v2/positions"), ("GET", "/v2/account/portfolio/history")})
TIMEOUT = 15.0
MAX_BYTES = 2_000_000
KILL_ENV = "HERMES_ALPACA"          # "off": no request, no logo fetch
KEY_NAMES = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_ENV")
ACCOUNT_ID = "alpaca:live"          # its id in net worth and the Custom selection
ITEM_ID = "alpaca"                  # how the pages group it, like a bank (it is not a Plaid Item: no slot)
INSTITUTION_ID = "ins_hf_alpaca"    # its logo row in `institutions` (never a Plaid id: those are ins_<digits>)
NAME = "Alpaca"
SITE = "https://alpaca.markets"     # where its logo comes from (logos.site_icon, like a bank without a Plaid logo)
ACTIVE = "ACTIVE"
PAPER_MESSAGE = "That's a paper-trading key. Use your live account's key, or skip Alpaca"
ALPACA_NOT_CONFIGURED = ("Alpaca not configured in Hermes (hermes config set APCA_API_KEY_ID <key id>, hermes config set "
                         "APCA_API_SECRET_KEY <secret> and hermes config set APCA_ENV live); nothing to do")
IN_HERMES = ("Alpaca's key is kept in Hermes here: set or remove it with hermes config set (APCA_API_KEY_ID, "
             "APCA_API_SECRET_KEY, APCA_ENV live) or the Hermes Desktop's plugin settings")
KEY_ID_RE = re.compile(r"[A-Za-z0-9]{8,64}")
SECRET_RE = re.compile(r"[A-Za-z0-9/+=_-]{16,128}")


class AlpacaError(Exception):
    """The sync couldn't finish (network, refused key, a malformed answer): nothing is saved."""


class Refused(AlpacaError):
    """A call that isn't allowed, or a key that isn't a live one: refused before any request is made."""


@dataclass(frozen=True)
class Keys:
    key_id: str
    secret: str = field(repr=False)


def load_keys(path=None) -> Keys | None:
    """The live keys from the key file, or None when there is no key file. Refused: a paper key, or keys missing.
    Hermes mode: from Hermes (None when neither key is set there), never from a file, with the same checks."""
    if config.hermes_credentials():
        values = {k: config.hermes_value(k) or "" for k in KEY_NAMES}
        if not values["APCA_API_KEY_ID"] and not values["APCA_API_SECRET_KEY"]:
            return None
        where = "Hermes"
    else:
        path = path or config.alpaca_key_path()
        if not path.exists():
            return None
        values = config.parse_env_file(path)
        where = str(path)
    key_id, secret = (values.get("APCA_API_KEY_ID") or "").strip(), (values.get("APCA_API_SECRET_KEY") or "").strip()
    if not key_id or not secret:
        raise Refused(f"{where} needs APCA_API_KEY_ID and APCA_API_SECRET_KEY")
    redact.register(secret)
    env = (values.get("APCA_ENV") or "").strip().lower()
    if env != "live":
        raise Refused(f"only a live key is used (APCA_ENV is {env or 'not set'}): paper money never counts")
    base = (values.get("APCA_API_BASE_URL") or values.get("APCA_BASE_URL") or "").strip()
    if base and (urlsplit(base).hostname or "").lower() != LIVE_HOST:
        raise Refused(f"only the live host {LIVE_HOST} is used, not {urlsplit(base).hostname or base!r}")
    if key_id.upper().startswith("PK"):
        raise Refused("that is a paper key (PK...): only a live key is used")
    return Keys(key_id, secret)


def _connect(host: str, timeout: float) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, timeout=timeout)


# Tests replace this: (host, path with query, headers) -> (status, body bytes). It never follows a redirect.
def _transport(host: str, target: str, headers: dict[str, str]) -> tuple[int, bytes]:
    conn = _connect(host, TIMEOUT)
    try:
        conn.request("GET", target, headers=headers)
        resp = conn.getresponse()
        body = resp.read(MAX_BYTES + 1)
        return resp.status, body
    finally:
        conn.close()


transport: Callable[[str, str, dict[str, str]], tuple[int, bytes]] = _transport


def _get(method: str, path: str, keys: Keys, query: dict[str, str] | None = None) -> Any:
    """The one call site. The allow-list comes first: any other method or path is refused before anything is sent."""
    if (method, path) not in ALLOWED:
        raise Refused(f"not an allowed Alpaca call: {method} {path} (only GET {', '.join(sorted(p for _, p in ALLOWED))})")
    if (os.environ.get(KILL_ENV) or "").strip().lower() == "off":
        raise Refused(f"Alpaca calls are switched off ({KILL_ENV}=off)")
    target = path + ("?" + urlencode(query) if query else "")
    headers = {"APCA-API-KEY-ID": keys.key_id, "APCA-API-SECRET-KEY": keys.secret, "Accept": "application/json",
               "User-Agent": "hermes-finance/0.1 (read-only)"}
    try:
        status, body = transport(LIVE_HOST, target, headers)
    except (OSError, http.client.HTTPException) as e:
        raise AlpacaError(f"{LIVE_HOST}: {type(e).__name__}") from None
    if status in (401, 403):
        raise AlpacaError(f"Alpaca refused the key (HTTP {status})")
    if status != 200:
        raise AlpacaError(f"Alpaca answered HTTP {status} for {path}")
    if len(body) > MAX_BYTES:
        raise AlpacaError(f"Alpaca's answer for {path} is too large")
    try:
        return json.loads(body)
    except ValueError:
        raise AlpacaError(f"Alpaca's answer for {path} is not JSON") from None


def _money(value: Any, what: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise AlpacaError(f"Alpaca's {what} is not a number ({str(value)[:20]!r})") from None
    if not math.isfinite(x):
        raise AlpacaError(f"Alpaca's {what} is not a number")
    return x


def _r(x: float) -> float:
    return round(x + 0.0, 2)


def parse(account: Any, positions: Any) -> dict[str, Any]:
    """The snapshot to save from the two answers: status, cash, equity (cash + the positions' value) and the positions."""
    if not isinstance(account, dict) or not isinstance(positions, list):
        raise AlpacaError("Alpaca's answer is not the expected shape")
    status = str(account.get("status") or "UNKNOWN").strip().upper()
    cash = _r(_money(account.get("cash", 0), "cash"))
    rows = []
    for p in positions:
        if not isinstance(p, dict) or not isinstance(p.get("symbol"), str) or not p["symbol"].strip():
            raise AlpacaError("an Alpaca position without a symbol")
        rows.append({"symbol": p["symbol"].strip().upper()[:24], "asset_class": str(p.get("asset_class") or "")[:24] or None,
                     "qty": _money(p.get("qty", 0), "quantity"),
                     "price": _money(p["current_price"], "price") if p.get("current_price") is not None else None,
                     "value": _r(_money(p.get("market_value", 0), "market value")),
                     "cost_basis": _r(_money(p["cost_basis"], "cost basis")) if p.get("cost_basis") is not None else None})
    number = str(account.get("account_number") or "")
    reported = account.get("equity")
    return {"status": status, "account_mask": number[-4:] or None, "currency": str(account.get("currency") or "USD")[:8],
            "cash": cash, "equity": _r(cash + sum(r["value"] for r in rows)),
            "reported_equity": _r(_money(reported, "equity")) if reported is not None else None,
            "positions": rows}


def save(conn: sqlite3.Connection, snap: dict[str, Any], day: date) -> None:
    """Today's snapshot with its positions (a second sync the same day replaces the row)."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO alpaca_snapshots (date, fetched_at, status, account_mask, currency, cash, equity, "
                     "reported_equity, positions, positions_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (day.isoformat(), now(), snap["status"], snap.get("account_mask"), snap.get("currency"), snap["cash"],
                      snap["equity"], snap.get("reported_equity"), len(snap["positions"]), json.dumps(snap["positions"])))


def positions(row: sqlite3.Row) -> list[dict[str, Any]]:
    """A snapshot's positions, largest first."""
    try:
        items = json.loads(row["positions_json"] or "[]")
    except ValueError:
        return []
    return sorted((p for p in items if isinstance(p, dict)), key=lambda p: (-(p.get("value") or 0), p.get("symbol") or ""))


def _store_logo(conn: sqlite3.Connection, fetch=None) -> None:
    """Alpaca's own site icon (logos.site_icon), cached like a bank's; any failure leaves initials. Once found it's kept;
    a failed try is tried again once it's logos.INSTITUTION_RETRY_DAYS old (the next day's sync), never sooner."""
    if (os.environ.get(KILL_ENV) or "").strip().lower() == "off":
        return
    from . import logos
    stamp = now()
    row = conn.execute("SELECT logo_png IS NOT NULL, fetched_at FROM institutions WHERE institution_id = ?",
                       (INSTITUTION_ID,)).fetchone()
    if row and (row[0] or not logos.retry_due(row[1], logos.INSTITUTION_RETRY_DAYS, date.fromisoformat(stamp[:10]))):
        return
    try:
        png, source = logos.site_icon(fetch or logos.http_get, SITE), "site"
    except logos.LogoError as e:
        log.info("no Alpaca logo: %s", e)
        png, source = None, "none"
    with conn:
        conn.execute("INSERT INTO institutions (institution_id, name, logo_png, logo_source, primary_color, url, fetched_at) "
                     "VALUES (?,?,?,?,NULL,?,?) ON CONFLICT(institution_id) DO UPDATE SET name=excluded.name, "
                     "logo_png=excluded.logo_png, logo_source=excluded.logo_source, url=excluded.url, "
                     "fetched_at=excluded.fetched_at", (INSTITUTION_ID, NAME, png, source, SITE, stamp))


def sync(conn: sqlite3.Connection, day: date, key_path=None, fetch_logo=None) -> dict[str, Any]:
    """Read the account and its positions, save today's snapshot. {ok, action, message, snapshot?}. Without a key file:
    nothing is asked (and snapshots saved before are marked not counted, so the account leaves every page)."""
    try:
        keys = load_keys(key_path)
    except Refused as e:
        return {"ok": False, "action": "refused", "message": str(e)}
    if keys is None:
        if config.hermes_credentials():   # where the keys come from changed: saved snapshots stay as they are
            return {"ok": True, "action": "none", "message": ALPACA_NOT_CONFIGURED}
        if forget(conn):
            return {"ok": True, "action": "removed", "message": "no key file any more: Alpaca is no longer shown or counted"}
        return {"ok": True, "action": "none", "message": f"no key file ({config.alpaca_key_path()}): nothing to do"}
    try:
        snap = parse(_get("GET", "/v2/account", keys), _get("GET", "/v2/positions", keys))
    except AlpacaError as e:
        return {"ok": False, "action": "failed", "message": str(e)}
    save(conn, snap, day)
    _store_logo(conn, fetch_logo)
    counted = snap["status"] == ACTIVE
    msg = (f"{snap['status'].lower()} · equity {money(snap['equity'])} (cash {money(snap['cash'])} + {len(snap['positions'])} "
           f"position{'s' if len(snap['positions']) != 1 else ''})" + ("" if counted else ": not counted (the account isn't active)"))
    return {"ok": counted, "action": "saved", "message": msg,
            "snapshot": {k: snap[k] for k in ("status", "cash", "equity", "reported_equity", "currency")} | {"positions": len(snap["positions"])}}


def forget(conn: sqlite3.Connection) -> int:
    """Every snapshot left out of every page, total and history point (the key was removed). Returns the rows changed."""
    with conn:
        return conn.execute("UPDATE alpaca_snapshots SET counted = 0 WHERE counted = 1").rowcount


# --- the dashboard's Connect form (/api/alpaca/key) and Remove ---------------------------------------------------------

def _clean_key(key_id: Any, secret: Any) -> Keys:
    if not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id.strip()):
        raise Refused("That Key ID doesn't look right: it's the letters and numbers Alpaca shows as API Key ID")
    if not isinstance(secret, str) or not SECRET_RE.fullmatch(secret.strip()):
        raise Refused("That Secret doesn't look right: paste the whole Secret Key Alpaca showed you")
    keys = Keys(key_id.strip(), secret.strip())
    redact.register(keys.secret)
    if keys.key_id.upper().startswith("PK"):
        raise Refused(PAPER_MESSAGE)
    return keys


def write_key_file(keys: Keys, path: Path | None = None) -> Path:
    """The key file, 0600 in a 0700 folder, written atomically (a temp file in the same folder, then renamed over)."""
    if config.hermes_credentials():
        raise Refused(IN_HERMES)
    path = Path(path or config.alpaca_key_path())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".alpaca-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"APCA_API_KEY_ID={keys.key_id}\nAPCA_API_SECRET_KEY={keys.secret}\nAPCA_ENV=live\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.chmod(path, 0o600)
    return path


def connect(conn: sqlite3.Connection, key_id: Any, secret: Any, day: date, path: Path | None = None,
            fetch_logo=None) -> dict[str, Any]:
    """Check a live key with ONE call (GET /v2/account): accepted only for a 200 from an ACTIVE account. Then the key file
    is written and today's snapshot saved (its positions asked once). Returns {equity, cash, positions, status}; the
    secret is never in it. Refused (nothing written): a paper key (PK..., or a 401 from the live host), a key Alpaca
    doesn't accept, an account not ACTIVE."""
    if config.hermes_credentials():
        raise Refused(IN_HERMES)
    keys = _clean_key(key_id, secret)
    try:
        account = _get("GET", "/v2/account", keys)
    except Refused:
        raise
    except AlpacaError as e:
        if "(HTTP 401)" in str(e):  # what the live host says to a paper key (and to a mistyped live one)
            raise Refused(f"{PAPER_MESSAGE}. If it's a live key, check the Key ID and Secret") from None
        if "refused the key" in str(e):
            raise Refused("Alpaca didn't accept that key. Check the Key ID and Secret (a live account's key)") from None
        raise
    if not isinstance(account, dict):
        raise AlpacaError("Alpaca's answer is not the expected shape")
    status = str(account.get("status") or "UNKNOWN").strip().upper()
    if status != ACTIVE:
        raise Refused(f"That Alpaca account isn't active (Alpaca says {status}), so it isn't connected")
    write_key_file(keys, path)
    try:
        snap = parse(account, _get("GET", "/v2/positions", keys))
    except AlpacaError as e:  # connected; the daily sync fills the positions in
        log.info("Alpaca positions not read yet: %s", e)
        snap = parse(account, [])
        snap["positions_unread"] = True
    save(conn, snap, day)
    _store_logo(conn, fetch_logo)
    return {"connected": True, "status": status, "equity": snap["equity"], "cash": snap["cash"],
            "positions": None if snap.get("positions_unread") else len(snap["positions"])}


def disconnect(conn: sqlite3.Connection, path: Path | None = None) -> dict[str, Any]:
    """Remove the key file and leave every snapshot out of every page and total."""
    if config.hermes_credentials():
        raise Refused(IN_HERMES)
    path = Path(path or config.alpaca_key_path())
    existed = path.exists()
    if existed:
        path.unlink()
    rows = forget(conn)
    return {"connected": False, "removed_file": existed, "snapshots_left_out": rows}


def key_state(path: Path | None = None) -> dict[str, Any]:
    """Whether a key file is there (never its contents). Hermes mode: whether Hermes has both keys (no file looked at)."""
    if config.hermes_credentials():
        return {"key_file": bool(config.hermes_value("APCA_API_KEY_ID") and config.hermes_value("APCA_API_SECRET_KEY"))}
    path = Path(path or config.alpaca_key_path())
    return {"key_file": path.exists()}


# --- reads (net worth, Investments, Accounts, the sidebar) -----------------------------------------------------------

def latest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The latest snapshot still counted (a removed key's are not)."""
    try:
        return conn.execute("SELECT * FROM alpaca_snapshots WHERE counted = 1 ORDER BY date DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:  # a database from before TASK-19 (read-only, not upgraded yet)
        return None


def counted(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The snapshot net worth counts: the latest, while the account is ACTIVE."""
    s = latest(conn)
    return s if s is not None and s["status"] == ACTIVE else None


def networth_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The account as net worth's account query returns a Plaid one (an investment account at its equity), or []."""
    s = counted(conn)
    if s is None:
        return []
    return [{"account_id": ACCOUNT_ID, "name": NAME, "official_name": "Alpaca brokerage (live)", "mask": s["account_mask"],
             "type": "investment", "subtype": "brokerage", "current_balance": s["equity"], "available_balance": s["cash"],
             "credit_limit": None, "iso_currency": s["currency"] or "USD", "updated_at": s["fetched_at"], "item_id": ITEM_ID,
             "institution_name": NAME, "institution_id": INSTITUTION_ID, "status": "active", "error_code": None}]


def history_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Net worth history's rows: the equity on each snapshot day (0 once the account isn't ACTIVE)."""
    try:
        rows = conn.execute("SELECT date, status, equity FROM alpaca_snapshots WHERE counted = 1 ORDER BY date").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"date": r["date"], "account_id": ACCOUNT_ID, "current": r["equity"] if r["status"] == ACTIVE else 0.0,
             "type": "investment"} for r in rows]


def value_series(conn: sqlite3.Connection) -> dict[str, float | None]:
    """{date: equity} for the Investments value line (None on a day the account wasn't ACTIVE: it counts nothing)."""
    try:
        rows = conn.execute("SELECT date, status, equity FROM alpaca_snapshots WHERE counted = 1 ORDER BY date").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["date"]: (r["equity"] if r["status"] == ACTIVE else None) for r in rows}


def holding_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The counted snapshot's positions (and its cash, as a CUR:USD holding) in the shape of a Plaid holdings row."""
    s = counted(conn)
    if s is None:
        return []
    out = []
    logos = {r[0] for r in conn.execute("SELECT ticker FROM security_logos WHERE png IS NOT NULL")}
    for p in positions(s):
        out.append({"account_id": ACCOUNT_ID, "security_id": f"alpaca:{p['symbol']}", "item_id": ITEM_ID,
                    "ticker_symbol": p["symbol"], "security_name": None,
                    "security_type": "cryptocurrency" if p["asset_class"] == "crypto" else "equity",
                    "quantity": p["qty"], "institution_price": p["price"], "institution_value": p["value"],
                    "cost_basis": p["cost_basis"], "iso_currency": s["currency"] or "USD", "raw_json": None,
                    "updated_at": s["fetched_at"], "account_name": NAME, "mask": s["account_mask"],
                    "has_logo": 1 if p["symbol"] in logos else 0})
    if s["cash"]:
        out.append({"account_id": ACCOUNT_ID, "security_id": "alpaca:CUR:USD", "item_id": ITEM_ID, "ticker_symbol": "CUR:USD",
                    "security_name": "US Dollar", "security_type": "cash", "quantity": s["cash"], "institution_price": 1.0,
                    "institution_value": s["cash"], "cost_basis": None, "iso_currency": s["currency"] or "USD",
                    "raw_json": None, "updated_at": s["fetched_at"], "account_name": NAME, "mask": s["account_mask"],
                    "has_logo": 0})
    return out


def account_extra(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the pages add to its account row: connected by API (no Plaid slot), its status, positions, as of when."""
    s = latest(conn)
    if s is None:
        return {}
    return {"api": True, "provider": NAME, "account_status": s["status"], "counted": s["status"] == ACTIVE,
            "positions": s["positions"], "as_of": s["date"], "fetched_at": s["fetched_at"]}


def items_payload(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The sidebar / Accounts entry, like a bank's item (status error when the account isn't ACTIVE), or []."""
    s = latest(conn)
    if s is None:
        return []
    ok = s["status"] == ACTIVE
    return [{"item_id": ITEM_ID, "institution_id": INSTITUTION_ID, "institution_name": NAME, "status": "active" if ok else "error",
             "error_code": None if ok else f"ACCOUNT_{s['status']}", "removed_at": None, "last_sync_at": s["fetched_at"],
             "api": True, "account_status": s["status"], "counted": ok, "positions": s["positions"], "equity": s["equity"],
             "cash": s["cash"], "as_of": s["date"]}]
