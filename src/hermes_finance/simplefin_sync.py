"""SimpleFIN as a second, read-only bank-sync provider: claim and confirm, the request budget, the one-year backfill,
pending matching, duplicate review and disconnect/reconnect.

State lives where the other numbers do: `items` (one per v2 `connections[]` entry, `provider='simplefin'`, an empty
`access_token` because that column is NOT NULL), `accounts` (with `conn_id`), `transactions` (`source='simplefin'`) and
the `settings` rows for the install's coverage, last success, daily minute and duplicate pairs. The access URL never
appears in any of them: it lives only in `secretstore` (0600) and everything read from it is registered with redact.

The provider allows 24 /accounts requests a local day and expects at most 6 manual syncs. `simplefin_requests` records
every request before it is made (the database triggers are the hard backstop), so the app, the CLI and the Hermes jobs
share one budget. The first sync asks for up to one year in 89-day chunks (at most five requests); later ones ask from
`max(coverage_start, last_success - 5 days)` and overlap by five days. Coverage is what the Bridge actually returned:
the earliest transaction date, never a window that came back empty.
"""

from __future__ import annotations

import contextvars
import difflib
import functools
import inspect
import json
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from . import classify, config, insights, secretstore, simplefin
from . import sync as bank_sync
from .db import now


# --- the clock ----------------------------------------------------------------------------------------
# One seam for "today" and "now". A public entry point that takes `today` runs inside `_follows_today`, which sets
# `_CLOCK` for the call, so every helper under it (the budget, the stored timestamps) follows that day and never reads
# the real clock. Called without `today`, `_today()` / `_now()` are exactly `date.today()` / `db.now()`.
_CLOCK: contextvars.ContextVar[date | None] = contextvars.ContextVar("simplefin_clock", default=None)


def _today() -> date:
    return _CLOCK.get() or date.today()


def _now() -> str:
    """`db.now()`'s format. Under a given `today` the date is that day, at a fixed 12:00:00Z (so a run's timestamps
    never go backwards)."""
    day = _CLOCK.get()
    return now() if day is None else f"{day.isoformat()}T12:00:00Z"


def _follows_today(fn: Callable) -> Callable:
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        day = sig.bind(*args, **kwargs).arguments.get("today")
        if day is None:                       # no `today`: keep the caller's (a nested call) or the real clock
            return fn(*args, **kwargs)
        token = _CLOCK.set(day)
        try:
            return fn(*args, **kwargs)
        finally:
            _CLOCK.reset(token)
    return wrapper

log = logging.getLogger(__name__)

PROVIDER = "simplefin"
DAILY_CAP = 24
MANUAL_CAP = 6
CHUNK_DAYS = 89
BACKFILL_DAYS = 365
OVERLAP_DAYS = 5
PENDING_MATCH_DAYS = 5
PENDING_STALE_DAYS = 10
TIP = 5 / 4                         # +25%: compare as integers (4*q <= 5*p), never floats
TYPE_CHOICES = {
    "checking": ("depository", "checking"),
    "savings": ("depository", "savings"),
    "credit": ("credit", "credit card"),
    "loan": ("loan", None),
    "investment": ("investment", "brokerage"),
}
TYPE_LABELS = {"checking": "Checking", "savings": "Savings", "credit": "Credit card", "loan": "Loan",
               "investment": "Investment"}
STOP_WORDS = {"the", "and", "for", "account", "acct", "bank", "credit", "card", "checking", "savings", "online"}

PENDING_KEY = "simplefin.pending_confirm"
COVERAGE_KEY = "simplefin.coverage_start"
LAST_SUCCESS_KEY = "simplefin.last_success"
DAILY_MINUTE_KEY = "simplefin.daily_minute"
DISCONNECTED_KEY = "simplefin.disconnected"
DUPLICATES_KEY = "simplefin.duplicate_pairs"
LAST_ERRORS_KEY = "simplefin.last_errors"
LAST_RESULT_KEY = "simplefin.last_result"
STATE_VERSION = 1


class SimpleFinRefused(Exception):
    """A request the app refuses before calling the Bridge. `status` is the HTTP answer."""
    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status

    def payload(self) -> dict[str, Any]:
        return {"error": str(self)}


class ManualLimit(SimpleFinRefused):
    status = 429


class BudgetExhausted(SimpleFinRefused):
    status = 429


class NeedsConfirm(SimpleFinRefused):
    status = 409


# --- small state helpers ------------------------------------------------------------------------------

def _get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def _set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                     "SET value = excluded.value, updated_at = excluded.updated_at", (key, json.dumps(value), _now()))


def _del(conn: sqlite3.Connection, key: str) -> None:
    with conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def access_url() -> str | None:
    return secretstore.simplefin_url()


def _items(conn: sqlite3.Connection, include_removed: bool = False) -> list[sqlite3.Row]:
    where = "provider = ?" if include_removed else "provider = ? AND removed_at IS NULL"
    return conn.execute(f"SELECT * FROM items WHERE {where} ORDER BY created_at, item_id", (PROVIDER,)).fetchall()


def _accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.*, i.institution_name, i.removed_at AS item_removed
           FROM accounts a JOIN items i ON i.item_id = a.item_id
           WHERE i.provider = ? ORDER BY i.created_at, a.type, a.name""", (PROVIDER,)).fetchall()


def item_of(conn_id: str) -> str:
    return f"simplefin:{conn_id}"


def account_of(conn_id: str, account_id: str) -> str:
    return f"simplefin:{conn_id}:{account_id}"


def txn_of(conn_id: str, transaction_id: str) -> str:
    return f"simplefin:{conn_id}:{transaction_id}"


def connected(conn: sqlite3.Connection) -> bool:
    """A confirmed SimpleFIN connection (an item row, not removed). A claimed but unconfirmed link is not connected."""
    return bool(_items(conn))


def claim_pending(conn: sqlite3.Connection) -> bool:
    return _get(conn, PENDING_KEY) is not None


# --- the request budget -------------------------------------------------------------------------------

def counts(conn: sqlite3.Connection, day: str | None = None) -> tuple[int, int]:
    """(all /accounts requests today, manual syncs today), from the database, so every process sees the same ones."""
    day = day or _today().isoformat()
    total = conn.execute("SELECT COUNT(*) FROM simplefin_requests WHERE day = ?", (day,)).fetchone()[0]
    manual = conn.execute("SELECT COUNT(*) FROM simplefin_requests WHERE day = ? AND trigger = 'manual'",
                          (day,)).fetchone()[0]
    return total, manual


def budget(conn: sqlite3.Connection) -> dict[str, Any]:
    total, manual = counts(conn)
    return {"requests_today": total, "request_limit": DAILY_CAP, "manual_today": manual,
            "manual_limit": MANUAL_CAP, "manual_left": max(0, MANUAL_CAP - manual)}


def _check(conn: sqlite3.Connection, trigger: str, day: str | None = None) -> None:
    total, manual = counts(conn, day)
    if trigger == "manual" and manual >= MANUAL_CAP:
        raise ManualLimit(f"SimpleFIN allows {MANUAL_CAP} manual syncs a day. Try again tomorrow, or let the daily sync "
                          "run.")
    if total >= DAILY_CAP:
        raise BudgetExhausted(f"SimpleFIN's {DAILY_CAP} requests a day are used up. Nothing was changed; try again "
                              "tomorrow.")


def reserve_request(conn: sqlite3.Connection, trigger: str, start: date | None = None,
                    end: date | None = None) -> int:
    """Count one /accounts request before it is made. The triggers on simplefin_requests refuse past the caps even if
    a second process raced us here; a refusal means no request is made."""
    if trigger not in ("daily", "manual", "confirm", "backfill"):
        raise ValueError(f"unknown SimpleFIN trigger {trigger!r}")
    day = _today().isoformat()
    _check(conn, trigger, day)
    try:
        with conn:
            return conn.execute(
                "INSERT INTO simplefin_requests (at, day, trigger, start_date, end_date, outcome) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (_now(), day, trigger, start.isoformat() if start else None, end.isoformat() if end else None)).lastrowid
    except sqlite3.IntegrityError as e:      # the database's own cap (a race): nothing is called
        text = str(e)
        if "manual" in text:
            raise ManualLimit(f"SimpleFIN allows {MANUAL_CAP} manual syncs a day. Try again tomorrow, or let the daily "
                              "sync run.") from None
        raise BudgetExhausted(f"SimpleFIN's {DAILY_CAP} requests a day are used up. Nothing was changed; try again "
                              "tomorrow.") from None


def finish_request(conn: sqlite3.Connection, request_id: int, outcome: str, note: str | None = None) -> None:
    with conn:
        conn.execute("UPDATE simplefin_requests SET outcome = ?, note = ? WHERE id = ?",
                     (outcome, simplefin.scrub_message(note)[:simplefin.MAX_ERROR_CHARS] if note else None, request_id))


# --- the install's daily minute ------------------------------------------------------------------------

def _new_minute(rng: Callable[[int], int] = secrets.randbelow) -> int:
    return int(rng(60))


def ensure_daily_minute(conn: sqlite3.Connection, rng: Callable[[int], int] = secrets.randbelow) -> int:
    minute = _get(conn, DAILY_MINUTE_KEY)
    if isinstance(minute, int) and 0 <= minute <= 59:
        return minute
    minute = _new_minute(rng)
    _set(conn, DAILY_MINUTE_KEY, minute)
    return minute


def daily_minute(conn: sqlite3.Connection) -> int:
    """Read-only: 0 when no connection has been claimed yet (the claim picks one, once)."""
    minute = _get(conn, DAILY_MINUTE_KEY)
    return minute if isinstance(minute, int) and 0 <= minute <= 59 else 0


# --- duplicates with Plaid -----------------------------------------------------------------------------

def _tokens(value: Any) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(w) > 2 and w not in STOP_WORDS}


def _plaid_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.account_id, a.name, a.mask, a.type, a.subtype, a.current_balance, i.institution_name,
                  i.institution_id
           FROM accounts a JOIN items i ON i.item_id = a.item_id
           WHERE i.provider = 'plaid' AND i.removed_at IS NULL ORDER BY i.created_at, a.name""").fetchall()


def _looks_like(account: simplefin.Account, connection_name: str, plaid: sqlite3.Row) -> bool:
    """A conservative mixed-provider duplicate test: a matching last 4, or the same balance, together with a name or
    institution overlap. A coin-toss never hides an account; the owner sees the flag and decides."""
    tokens = _tokens(account.name) | _tokens(connection_name)
    their = _tokens(plaid["name"]) | _tokens(plaid["institution_name"])
    overlap = bool(tokens & their)
    if account.mask and plaid["mask"] and account.mask == plaid["mask"] and overlap:
        return True
    balance = simplefin.units(account.balance_cents)
    same_money = plaid["current_balance"] is not None and abs(balance - plaid["current_balance"]) < 0.005
    if same_money and overlap:
        return True
    return bool(_tokens(account.name) & _tokens(plaid["name"])) and bool(_tokens(connection_name)
                                                                         & _tokens(plaid["institution_name"]))


def _duplicate_of(conn: sqlite3.Connection, account: simplefin.Account, connection_name: str) -> dict | None:
    for row in _plaid_accounts(conn):
        if _looks_like(account, connection_name, row):
            return {"account_id": row["account_id"], "name": row["name"], "mask": row["mask"],
                    "institution": row["institution_name"], "balance": row["current_balance"]}
    return None


def _duplicate_pairs(conn: sqlite3.Connection) -> list[dict]:
    pairs = _get(conn, DUPLICATES_KEY, [])
    return [p for p in pairs if isinstance(p, dict) and p.get("simplefin") and p.get("plaid")]


def _save_pairs(conn: sqlite3.Connection, pairs: list[dict]) -> None:
    if pairs:
        _set(conn, DUPLICATES_KEY, pairs)
    else:
        _del(conn, DUPLICATES_KEY)


# --- claim ---------------------------------------------------------------------------------------------

def _window(start: date, end: date) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


def _confirm_list(conn: sqlite3.Connection, payload: simplefin.Payload, window: dict[str, str] | None = None) -> dict:
    """What the confirm sheet shows. Guesses are suggestions; nothing is stored until the owner picks every type."""
    existing = {r["account_id"]: r for r in _accounts(conn)}
    items: list[dict] = []
    by_conn: dict[str, dict] = {}
    for a in payload.accounts:
        connection = payload.connections.get(a.conn_id)
        name = (connection.name if connection else "") or a.connection_name or ""
        stored = existing.get(account_of(a.conn_id, a.id))
        if stored is not None:
            kind, subtype, confidence = stored["type"], stored["subtype"], "stored"
        else:
            kind, subtype, confidence = simplefin.guess_type(a.name, a.has_holdings, a.balance_cents, a.available_cents)
        dup = None if stored is not None else _duplicate_of(conn, a, name)
        entry = {
            "key": f"{a.conn_id}:{a.id}",
            "id": a.id, "conn_id": a.conn_id, "connection": name,
            "name": a.name, "mask": a.mask,
            "balance": simplefin.units(a.balance_cents),
            "available": None if a.available_cents is None else simplefin.units(a.available_cents),
            "currency": a.currency,
            "guess": {"type": kind, "subtype": subtype, "confidence": confidence,
                      "label": TYPE_LABELS.get("credit" if kind == "credit" else kind, "Check type")},
            "existing": stored is not None,
            "plaid": dup,
        }
        items.append(entry)
        bank = by_conn.setdefault(a.conn_id, {"conn_id": a.conn_id, "name": name, "accounts": []})
        bank["accounts"].append(entry)
    return {"banks": list(by_conn.values()), "counts": {"banks": len(by_conn), "accounts": len(items)},
            "accounts": items, "window": window, "fetched_at": _now()}


@_follows_today
def claim(conn: sqlite3.Connection, token: str, transport: simplefin.Transport | None = None,
          resolve: simplefin.Resolver | None = None, minute: int | None = None,
          rng: Callable[[int], int] = secrets.randbelow, today: date | None = None) -> dict:
    """Trade a setup token, keep the access URL in secretstore, and fetch the newest 89-day window for the confirm
    sheet. One request is counted before it is made. A token that can't be claimed never touches the store."""
    if config.hermes_credentials():
        raise SimpleFinRefused("SimpleFIN keeps its access link in this app's secrets file, which isn't used while "
                               "the keys come from Hermes.")
    _check(conn, "confirm")
    url = simplefin.claim(token, transport=transport, resolve=resolve)
    secretstore.write({secretstore.SIMPLEFIN: url})
    if minute is None:
        ensure_daily_minute(conn, rng)
    else:
        if not isinstance(minute, int) or not 0 <= minute <= 59:
            raise ValueError("minute must be 0 to 59")
        _set(conn, DAILY_MINUTE_KEY, minute)
    _set(conn, DISCONNECTED_KEY, None)
    day = today or _today()
    end = day + timedelta(days=1)                     # SimpleFIN's end-date is exclusive: today is inside the window
    start = end - timedelta(days=CHUNK_DAYS)
    request_id = reserve_request(conn, "confirm", start, end)
    client = simplefin.Client(url, transport=transport, resolve=resolve)
    try:
        payload = client.accounts(start, end)
    except simplefin.Disconnected as e:
        finish_request(conn, request_id, "disconnected", str(e))
        _mark_disconnected(conn, e)
        raise
    except Exception:
        finish_request(conn, request_id, "error")
        raise
    finish_request(conn, request_id, "ok")
    window = _window(start, end)
    _set(conn, PENDING_KEY, {"version": STATE_VERSION, "payload": payload.raw, "window": window, "fetched_at": _now()})
    return _confirm_list(conn, payload, window)


def needs_retry(conn: sqlite3.Connection) -> bool:
    """An access link is saved but no connection is active: the first fetch never answered, also after a reconnect
    that followed an earlier disconnect. The claim can be retried without a new token."""
    return access_url() is not None and not _items(conn) and not claim_pending(conn)


@_follows_today
def retry(conn: sqlite3.Connection, transport: simplefin.Transport | None = None,
          resolve: simplefin.Resolver | None = None, today: date | None = None) -> dict:
    """Fetch from the saved access URL again after a claim whose first request failed. One request is counted before it is
    made, exactly like a claim; a failure leaves the URL stored, so the same retry can run again. Never asks for a new
    setup token."""
    if config.hermes_credentials():
        raise SimpleFinRefused("SimpleFIN keeps its access link in this app's secrets file, which isn't used while "
                               "the keys come from Hermes.")
    if connected(conn):
        raise SimpleFinRefused("SimpleFIN is already connected.")
    url = access_url()
    if url is None:
        raise SimpleFinRefused("Paste the SimpleFIN setup token from SimpleFIN Bridge first.")
    _check(conn, "confirm")
    day = today or _today()
    end = day + timedelta(days=1)
    start = end - timedelta(days=CHUNK_DAYS)
    request_id = reserve_request(conn, "confirm", start, end)
    client = simplefin.Client(url, transport=transport, resolve=resolve)
    try:
        payload = client.accounts(start, end)
    except simplefin.Disconnected as e:
        finish_request(conn, request_id, "disconnected", str(e))
        _mark_disconnected(conn, e)
        raise
    except Exception:
        finish_request(conn, request_id, "error")
        raise
    finish_request(conn, request_id, "ok")
    window = _window(start, end)
    _set(conn, PENDING_KEY, {"version": STATE_VERSION, "payload": payload.raw, "window": window, "fetched_at": _now()})
    _set(conn, DISCONNECTED_KEY, None)
    return _confirm_list(conn, payload, window)


def pending(conn: sqlite3.Connection) -> dict | None:
    state = _get(conn, PENDING_KEY)
    if not isinstance(state, dict) or not isinstance(state.get("payload"), dict):
        return None
    try:
        return _confirm_list(conn, simplefin.normalize(state["payload"]), state.get("window"))
    except simplefin.SimpleFinError:
        return None


# --- confirm -------------------------------------------------------------------------------------------

def _allocated(conn: sqlite3.Connection) -> set[str]:
    return set(insights.excluded_accounts(conn))


def _choose(choice: dict | None, account: simplefin.Account) -> tuple[bool, str, str]:
    """(add?, type, subtype) from the owner's choice. Missing choice on an account that isn't a Plaid duplicate is a
    refusal: every type is confirmed before the first sync."""
    if not isinstance(choice, dict):
        raise SimpleFinRefused("Choose what to do with every account before adding them.")
    if choice.get("add") is False:
        return False, "other", None
    kind = choice.get("type")
    if kind not in TYPE_CHOICES:
        raise SimpleFinRefused(f"Pick a type for {account.name or 'an account'}: "
                               + ", ".join(TYPE_LABELS.values()) + ".")
    typ, subtype = TYPE_CHOICES[kind]
    if kind == "loan":
        guess = simplefin.guess_type(account.name, account.has_holdings, account.balance_cents, account.available_cents)
        subtype = guess[1] if guess[0] == "loan" and guess[1] else "other"
    return True, typ, subtype


def _stored_balance(account: simplefin.Account, typ: str) -> float:
    value = simplefin.units(account.balance_cents)
    # Plaid's sign for a debt is positive owed, and SimpleFIN's is positive = money in: negate a card or loan balance.
    # A card owing 450.75 arrives as -450.75 and stores +450.75; an overpaid card at +25 stores -25 (an asset).
    return -value if typ in ("credit", "loan") else value


def _iso(currency: Any) -> str | None:
    text = str(currency or "").strip().upper()
    return text if re.fullmatch(r"[A-Z]{3}", text) else None


def _upsert_item(conn: sqlite3.Connection, conn_id: str, name: str) -> None:
    item_id = item_of(conn_id)
    with conn:
        conn.execute(
            """INSERT INTO items (item_id, institution_id, institution_name, access_token, status, products,
                   created_at, provider)
               VALUES (?, NULL, ?, '', 'active', '["transactions"]', ?, 'simplefin')
               ON CONFLICT(item_id) DO UPDATE SET institution_name=excluded.institution_name,
                   access_token='', status='active', error_code=NULL, error_message=NULL, removed_at=NULL,
                   provider='simplefin'""",
            (item_id, name, _now()))


def _upsert_account(conn: sqlite3.Connection, item_id: str, account: simplefin.Account, typ: str,
                    subtype: str | None) -> None:
    with conn:
        conn.execute(
            """INSERT INTO accounts (account_id, item_id, name, official_name, mask, type, subtype, current_balance,
                   available_balance, credit_limit, iso_currency, updated_at, conn_id)
               VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET item_id=excluded.item_id, name=excluded.name,
                   mask=excluded.mask, type=excluded.type, subtype=excluded.subtype,
                   current_balance=excluded.current_balance, available_balance=excluded.available_balance,
                   iso_currency=excluded.iso_currency, updated_at=excluded.updated_at, conn_id=excluded.conn_id""",
            (account_of(account.conn_id, account.id), item_id, account.name, account.mask, typ, subtype,
             _stored_balance(account, typ),
             None if account.available_cents is None else simplefin.units(account.available_cents),
             _iso(account.currency), _now(), account.conn_id))


@_follows_today
def confirm(conn: sqlite3.Connection, choices: dict[str, dict], transport: simplefin.Transport | None = None,
            resolve: simplefin.Resolver | None = None, today: date | None = None,
            sleep: Callable[[float], None] | None = None, jitter: int | None = None) -> dict:
    """Save the owner's type choices, create (or revive) the items and accounts, import the claim's window, and
    backfill older windows. Deterministic ids mean a reconnect never orphans or doubles a row."""
    state = _get(conn, PENDING_KEY)
    if not isinstance(state, dict) or not isinstance(state.get("payload"), dict):
        raise NeedsConfirm("Nothing to confirm: paste a SimpleFIN setup token first.")
    payload = simplefin.normalize(state["payload"])
    previous = {r["account_id"]: r for r in _accounts(conn)}
    policy = _allocated(conn)
    pairs = _duplicate_pairs(conn)
    added = 0
    touched_items: dict[str, str] = {}
    for account in payload.accounts:
        choice = None
        if isinstance(choices, dict):
            key = f"{account.conn_id}:{account.id}"
            choice = choices[key] if key in choices else choices.get(account.id)
        is_dup = (account_of(account.conn_id, account.id) not in previous
                  and _duplicate_of(conn, account,
                                    (payload.connections[account.conn_id].name
                                     if account.conn_id in payload.connections else "")) is not None)
        if choice is None:
            if is_dup:
                continue                                  # a likely Plaid duplicate defaults to Skip
            raise SimpleFinRefused("Choose what to do with every account before adding them.")
        add, typ, subtype = _choose(choice, account)
        aid = account_of(account.conn_id, account.id)
        if not add:
            if aid in previous:                           # a reconnect that skips a stored account: keep, don't count
                policy.add(aid)
            continue
        connection = payload.connections.get(account.conn_id)
        name = (connection.name if connection else "") or account.connection_name or "SimpleFIN"
        item_id = item_of(account.conn_id)
        _upsert_item(conn, account.conn_id, name)
        _upsert_account(conn, item_id, account, typ, subtype)
        touched_items[item_id] = name
        added += 1
        plaid = _duplicate_of(conn, account, name)
        pair = next((p for p in pairs if p["simplefin"] == aid), None)
        if pair is not None and pair.get("picked"):
            # a reconnect keeps the owner's earlier pick instead of excluding the copy again
            picked = pair["picked"]
            if picked == "simplefin":
                policy.discard(aid)
                policy.add(pair["plaid"])
            elif picked == "plaid":
                policy.add(aid)
                policy.discard(pair["plaid"])
            else:                                          # not_duplicate: both copies count
                policy.discard(aid)
                policy.discard(pair["plaid"])
        elif pair is not None or plaid is not None:
            policy.add(aid)                               # a second copy never counts until the owner picks a source
            if pair is None:
                pairs.append({"simplefin": aid, "plaid": plaid["account_id"],
                              "simplefin_name": account.name, "plaid_name": plaid["name"],
                              "mask": account.mask or plaid["mask"], "created_at": _now()})
        else:
            policy.discard(aid)
    if not added:
        raise SimpleFinRefused("Add at least one account, or close this and paste a new setup token later.")
    insights.save_excluded_accounts(conn, sorted(policy))
    _save_pairs(conn, pairs)
    _del(conn, PENDING_KEY)
    _set(conn, DISCONNECTED_KEY, None)
    result = import_window(conn, payload, state.get("window") or _window(_today() - timedelta(days=CHUNK_DAYS),
                                                                       _today()), today or _today())
    # older windows, up to the one-year backfill and the provider's request budget
    older = _backfill_windows(state.get("window"), today or _today(), keep=4)
    for start, end in older:
        try:
            request_id = reserve_request(conn, "backfill", start, end)
        except SimpleFinRefused as e:
            result["limited"] = str(e)
            break
        try:
            client = simplefin.Client(access_url() or "", transport=transport, resolve=resolve)
            got = client.accounts(start, end)
        except simplefin.Disconnected as e:
            finish_request(conn, request_id, "disconnected", str(e))
            _mark_disconnected(conn, e)
            result.update(ok=False, state=_state_of(conn), message=str(e))
            break
        except Exception as e:
            finish_request(conn, request_id, "error", type(e).__name__)
            log.warning("SimpleFIN backfill failed: %s", type(e).__name__)
            continue
        finish_request(conn, request_id, "ok")
        result = _merge(result, import_window(conn, got, _window(start, end), today or _today()))
    _after_sync(conn, result)
    return status(conn, today=today) | {"added": added}


def _backfill_windows(window: dict | None, today: date, keep: int = 4) -> list[tuple[date, date]]:
    """The older 89-day chunks, newest first, down to one year before today, with exclusive ends (an older chunk's end
    is the newer chunk's start). The claim's fetched window is not asked for again."""
    if not isinstance(window, dict):
        return []
    try:
        newest = date.fromisoformat(str(window.get("start"))[:10])
    except (TypeError, ValueError):
        return []
    floor = today - timedelta(days=BACKFILL_DAYS)
    out, cursor = [], newest
    while cursor > floor and len(out) < keep:
        start = max(floor, cursor - timedelta(days=CHUNK_DAYS))
        out.append((start, cursor))
        cursor = start
    return out


# --- import and pending matching -----------------------------------------------------------------------

def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for key in ("added", "modified", "removed", "imported"):
        out[key] = out.get(key, 0) + b.get(key, 0)
    for key in ("ambiguous", "stale_removed", "matched"):
        out[key] = out.get(key, 0) + b.get(key, 0)
    return out


@_follows_today
def import_window(conn: sqlite3.Connection, payload: simplefin.Payload, window: dict | None = None,
                  today: date | None = None) -> dict:
    """Upsert the payload's accounts and transactions (source 'simplefin'), then reconcile pending rows. Idempotent: the
    ids are deterministic, an existing row is updated in place. A matched pending row is soft-removed, never deleted."""
    ts = _now()
    accounts = {r["account_id"]: r for r in _accounts(conn)}
    added = modified = 0
    earliest: str | None = None
    for account in payload.accounts:
        aid = account_of(account.conn_id, account.id)
        stored = accounts.get(aid)
        if stored is None or stored["item_removed"]:
            continue                                      # never import an account the owner didn't confirm
        typ = stored["type"] or "other"
        _upsert_account(conn, stored["item_id"], account, typ, stored["subtype"])
        for t in account.transactions:
            if earliest is None or t.date < earliest:
                earliest = t.date
            tid = txn_of(account.conn_id, t.id)
            row = conn.execute("SELECT pending, removed FROM transactions WHERE transaction_id = ?", (tid,)).fetchone()
            # a row the Bridge returns again counts again, except a pending merged into a live posted twin: recounting
            # it would double the charge. A row returned posted under its own id is a second real charge instead: it
            # counts, and the twin drops the link so the pair is never re-matched. Rows of a removed item never reach
            # this loop.
            twin = None
            if row is not None and row["removed"]:
                twin = conn.execute(
                    "SELECT transaction_id FROM transactions WHERE pending_transaction_id = ? AND removed = 0 LIMIT 1",
                    (tid,)).fetchone()
            keep_removed = bool(twin is not None and t.pending)
            with conn:
                conn.execute(
                    """INSERT INTO transactions (transaction_id, account_id, item_id, date, authorized_date, name,
                           merchant_name, amount, iso_currency, pending, pending_transaction_id, payment_channel,
                           pfc_primary, pfc_detailed, pfc_confidence, logo_url, website, location_json,
                           counterparties_json, raw_json, removed, first_seen_at, updated_at, source)
                       VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                           ?, 0, ?, ?, 'simplefin')
                       ON CONFLICT(transaction_id) DO UPDATE SET account_id=excluded.account_id,
                           item_id=excluded.item_id, date=excluded.date, name=excluded.name,
                           merchant_name=excluded.merchant_name, amount=excluded.amount,
                           iso_currency=excluded.iso_currency, pending=excluded.pending,
                           raw_json=excluded.raw_json, removed=?, updated_at=excluded.updated_at""",
                    (tid, aid, stored["item_id"], t.date, t.description,
                     (t.raw.get("payee") or t.description or None), simplefin.units(t.amount_cents),
                     _iso(account.currency), int(t.pending), json.dumps(t.raw, sort_keys=True), ts, ts,
                     1 if keep_removed else 0))
                if twin is not None and not t.pending:
                    conn.execute("UPDATE transactions SET pending_transaction_id = NULL, updated_at = ? "
                                 "WHERE transaction_id = ?", (ts, twin["transaction_id"]))
            if row is None:
                added += 1
            else:
                modified += 1
        try:
            bank_sync.snapshot_balances(conn, stored["item_id"])
        except sqlite3.Error:
            log.exception("SimpleFIN balance snapshot failed for %s", stored["item_id"])
    matched = reconcile_pending(conn, today, payload=payload, window=window)
    # coverage is what was actually returned, never a window that came back empty
    if earliest is not None:
        current = _get(conn, COVERAGE_KEY)
        if not isinstance(current, str) or earliest < current:
            _set(conn, COVERAGE_KEY, earliest)
    return {"added": added, "modified": modified, "removed": 0, "imported": added,
            "matched": matched["matched"], "ambiguous": matched["ambiguous"],
            "stale_removed": matched["stale_removed"], "coverage_start": _get(conn, COVERAGE_KEY),
            "window": window}


def _description(row: sqlite3.Row | dict) -> str:
    value = (row["merchant_name"] if "merchant_name" in row.keys() else None) or row["name"] or ""
    return " ".join(str(value).lower().split())


def _fuzzy(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb and len(ta & tb) / max(1, len(ta | tb)) >= 0.5:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def _tip_ok(pending_cents: int, posted_cents: int) -> bool:
    """posted >= pending, posted <= pending * 1.25, with both signs (money out and money in)."""
    if pending_cents >= 0:
        return pending_cents <= posted_cents and 4 * posted_cents <= 5 * pending_cents
    return posted_cents <= pending_cents and 4 * posted_cents >= 5 * pending_cents


def _merge_pair(conn: sqlite3.Connection, pending_id: str, posted_id: str, ts: str) -> None:
    """Link one pending row to its posted charge and soft-remove the pending one. The pending row's user/AI decision
    follows the posted charge (rules re-file on apply)."""
    with conn:
        conn.execute("UPDATE transactions SET pending_transaction_id = ?, updated_at = ? WHERE transaction_id = ?",
                     (pending_id, ts, posted_id))
        conn.execute("UPDATE transactions SET removed = 1, updated_at = ? WHERE transaction_id = ?",
                     (ts, pending_id))
        conn.execute(
            """INSERT INTO classifications (transaction_id, category_id, source, confidence, note, updated_at)
               SELECT ?, category_id, source, confidence, note, ? FROM classifications
               WHERE transaction_id = ? AND source IN ('user', 'ai')
               ON CONFLICT(transaction_id) DO NOTHING""", (posted_id, ts, pending_id))


def _candidate_blocks(candidates: dict[str, list[sqlite3.Row]],
                      reverse: dict[str, list[str]]) -> list[tuple[set[str], set[str]]]:
    """The connected components of the pending/posted candidate graph, as (pending ids, posted ids)."""
    adjacent: dict[str, set[str]] = {}
    for pid, qs in candidates.items():
        for q in qs:
            qid = q["transaction_id"]
            adjacent.setdefault(pid, set()).add(qid)
            adjacent.setdefault(qid, set()).add(pid)
    blocks: list[tuple[set[str], set[str]]] = []
    seen: set[str] = set()
    for start in sorted(adjacent):
        if start in seen:
            continue
        stack, pids, qids = [start], set(), set()
        seen.add(start)
        while stack:
            node = stack.pop()
            (pids if node in candidates else qids).add(node)
            for nxt in adjacent.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        blocks.append((pids, qids))
    return blocks


@_follows_today
def reconcile_pending(conn: sqlite3.Connection, today: date | None = None, payload: simplefin.Payload | None = None,
                      window: dict | None = None) -> dict[str, int]:
    """One-to-one, deterministic pending -> posted matching (same account, within five days, fuzzy description, an
    amount tip of up to +25%). A complete block of identical candidates pairs in id order (min(pending, posted)); an
    irregular graph keeps only a pairing unique in both directions, and ambiguity is preserved for review. Pending rows
    absent from an answer whose window covers their date are soft-removed, as are pendings older than ten days with no
    posted twin."""
    today = today or _today()
    matched = ambiguous = stale = 0
    accounts = [r["account_id"] for r in conn.execute(
        "SELECT DISTINCT a.account_id FROM accounts a JOIN items i ON i.item_id = a.item_id "
        "WHERE i.provider = ? AND i.removed_at IS NULL", (PROVIDER,))]
    response: dict[str, set[str]] = {}
    win_start = win_end = None
    if payload is not None and isinstance(window, dict):
        try:
            win_start = date.fromisoformat(str(window.get("start"))[:10])
            win_end = date.fromisoformat(str(window.get("end"))[:10])
        except (TypeError, ValueError):
            win_start = win_end = None
        if win_start is not None and win_end is not None:
            live = set(accounts)
            # an errlist entry that names a connection (con.*: conn_id) or an account (act.*: account_id) means the
            # answer is partial for it: its transactions may be absent without the account being gone. An entry that
            # names neither (legacy `errors`, a bare con.*) makes the whole answer partial, so no account is assumed
            # complete, but only when no entry names a connection or an account: a nameless entry next to a named one
            # (a legacy string mirroring a named errlist entry) does not suspend the rule for the other accounts.
            # `gen.api` is a developer notice (`user=False`) and never suspends the rule.
            named_conns = {e.conn_id for e in payload.errors if e.conn_id}
            named_accounts = {e.account_id for e in payload.errors if e.account_id}
            partial = (not named_conns and not named_accounts
                       and any(e.user and not e.conn_id and not e.account_id for e in payload.errors))
            if not partial:
                for account in payload.accounts:
                    aid = account_of(account.conn_id, account.id)
                    if aid in live and account.conn_id not in named_conns and account.id not in named_accounts:
                        response[aid] = {txn_of(account.conn_id, t.id) for t in account.transactions}
    ts = _now()
    for aid in accounts:
        pend = conn.execute(
            """SELECT transaction_id, date, amount, name, merchant_name FROM transactions
               WHERE account_id = ? AND source = 'simplefin' AND pending = 1 AND removed = 0""", (aid,)).fetchall()
        posted = conn.execute(
            """SELECT transaction_id, date, amount, name, merchant_name FROM transactions
               WHERE account_id = ? AND source = 'simplefin' AND pending = 0 AND removed = 0
                 AND (pending_transaction_id IS NULL OR pending_transaction_id = '')""", (aid,)).fetchall()
        candidates: dict[str, list[sqlite3.Row]] = {}
        reverse: dict[str, list[str]] = {}
        for p in pend:
            try:
                p_day = date.fromisoformat(p["date"][:10])
            except ValueError:
                continue
            p_cents = int(round(p["amount"] * 100))
            for q in posted:
                try:
                    q_day = date.fromisoformat(q["date"][:10])
                except ValueError:
                    continue
                if not 0 <= (q_day - p_day).days <= PENDING_MATCH_DAYS:
                    continue
                if not _tip_ok(p_cents, int(round(q["amount"] * 100))):
                    continue
                if not _fuzzy(_description(p), _description(q)):
                    continue
                candidates.setdefault(p["transaction_id"], []).append(q)
                reverse.setdefault(q["transaction_id"], []).append(p["transaction_id"])
        merged_pending: set[str] = set()
        for pids, qids in _candidate_blocks(candidates, reverse):
            complete = all({q["transaction_id"] for q in candidates[pid]} == qids for pid in pids) \
                and all(set(reverse[qid]) == pids for qid in qids)
            if complete:
                pairs = list(zip(sorted(pids), sorted(qids)))
                for pid, qid in pairs:
                    _merge_pair(conn, pid, qid, ts)
                    merged_pending.add(pid)
                matched += len(pairs)
                ambiguous += abs(len(pids) - len(qids))
                continue
            for pid in sorted(pids):
                qs = candidates[pid]
                if len(qs) == 1 and len(reverse.get(qs[0]["transaction_id"], [])) == 1:
                    _merge_pair(conn, pid, qs[0]["transaction_id"], ts)
                    matched += 1
                    merged_pending.add(pid)
                elif len(qs) > 1 or len(reverse.get(qs[0]["transaction_id"], [])) > 1:
                    ambiguous += 1
        cutoff = today - timedelta(days=PENDING_STALE_DAYS)
        for p in pend:
            pid = p["transaction_id"]
            if pid in merged_pending:
                continue
            try:
                day = date.fromisoformat(p["date"][:10])
            except ValueError:
                continue
            if (win_start is not None and aid in response and win_start <= day < win_end
                    and pid not in response[aid]):
                with conn:
                    conn.execute("UPDATE transactions SET removed = 1, updated_at = ? WHERE transaction_id = ?",
                                 (ts, pid))
                stale += 1
                continue
            if day < cutoff and not conn.execute(
                    "SELECT 1 FROM transactions WHERE pending_transaction_id = ? AND removed = 0 LIMIT 1",
                    (pid,)).fetchone():
                with conn:
                    conn.execute("UPDATE transactions SET removed = 1, updated_at = ? WHERE transaction_id = ?",
                                 (ts, pid))
                stale += 1
    return {"matched": matched, "ambiguous": ambiguous, "stale_removed": stale}


# --- sync ----------------------------------------------------------------------------------------------

def _state_of(conn: sqlite3.Connection) -> str:
    if claim_pending(conn):
        return "needs_confirm"
    items = _items(conn)
    if not items:
        if access_url() is not None:
            return "needs_retry"                     # also after a reconnect whose first fetch failed
        if _items(conn, include_removed=True):
            return "disconnected"
        return "not_connected"
    codes = {it["error_code"] for it in items}
    if "SIMPLEFIN_REVOKED" in codes:
        return "needs_login"
    if "SIMPLEFIN_SUBSCRIPTION" in codes:
        return "subscription"
    if "SIMPLEFIN_QUOTA" in codes:
        return "quota"
    if all(it["removed_at"] for it in items):
        return "disconnected"
    return "connected"


def _all_items_error(conn: sqlite3.Connection, code: str | None, message: str | None, status: str = "error") -> None:
    with conn:
        conn.execute("UPDATE items SET status = ?, error_code = ?, error_message = ? WHERE provider = ? "
                     "AND removed_at IS NULL", (status, code, message, PROVIDER))


def _mark_disconnected(conn: sqlite3.Connection, exc: simplefin.Disconnected) -> None:
    code = {"revoked": "SIMPLEFIN_REVOKED", "subscription": "SIMPLEFIN_SUBSCRIPTION",
            "quota": "SIMPLEFIN_QUOTA"}.get(exc.reason, "SIMPLEFIN_ERROR")
    status = "login_required" if exc.reason == "revoked" else "error"
    _all_items_error(conn, code, str(exc), status)
    _set(conn, DISCONNECTED_KEY, {"reason": exc.reason, "at": _now(), "message": str(exc)})
    # the last answer's errlist, even an empty one: status never keeps a stale error from an older answer
    _set(conn, LAST_ERRORS_KEY, [{"code": e.code, "message": e.message, "user": e.user} for e in exc.errors])


def _after_sync(conn: sqlite3.Connection, result: dict) -> None:
    _set(conn, LAST_RESULT_KEY, {**{k: result.get(k) for k in
                                    ("added", "modified", "removed", "matched", "ambiguous", "stale_removed",
                                     "coverage_start")}, "at": _now()})
    if result.get("ok", True):
        _set(conn, LAST_SUCCESS_KEY, _now())
        _set(conn, DISCONNECTED_KEY, None)
        with conn:
            conn.execute("UPDATE items SET last_sync_at = ?, status = 'active', error_code = NULL, error_message = NULL "
                         "WHERE provider = ? AND removed_at IS NULL", (_now(), PROVIDER))
    classify.apply(conn)


@dataclass
class SyncResult:
    ok: bool = True
    connected: bool = True
    state: str = "connected"
    added: int = 0
    modified: int = 0
    removed: int = 0
    matched: int = 0
    ambiguous: int = 0
    stale_removed: int = 0
    requests: int = 0
    coverage_start: str | None = None
    as_of: str | None = None
    message: str = ""
    errors: list[dict] = field(default_factory=list)
    limited: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def summary(self) -> str:
        name = "SimpleFIN"
        if not self.connected:
            return "SimpleFIN: not connected"
        if not self.ok:
            return f"{name}: ERROR {self.message}"
        parts = [f"{name}: +{self.added} ~{self.modified} -{self.removed}"]
        if self.matched:
            parts.append(f"{self.matched} pending matched")
        if self.ambiguous:
            parts.append(f"{self.ambiguous} to review")
        if self.stale_removed:
            parts.append(f"{self.stale_removed} stale pending")
        return " | ".join(parts)


@_follows_today
def sync(conn: sqlite3.Connection, trigger: str = "manual", transport: simplefin.Transport | None = None,
         resolve: simplefin.Resolver | None = None, today: date | None = None,
         sleep: Callable[[float], None] | None = None, jitter: int | None = None,
         rng: Callable[[int], int] = secrets.randbelow) -> dict[str, Any]:
    """Fetch SimpleFIN once (chunked only when a backfill is due), import it, and report. A manual run counts against
    six a day; a daily run is not one of the six, but both count against the provider's 24. Disconnected never zeroes
    a balance."""
    today = today or _today()
    if claim_pending(conn):
        return SyncResult(ok=False, connected=False, state="needs_confirm",
                          message="SimpleFIN is claimed but no account type is confirmed yet.").as_dict()
    url = access_url()
    items = _items(conn)
    if url is None or not items:
        return SyncResult(ok=True, connected=False, state="not_connected", message="SimpleFIN is not connected.").as_dict()
    if trigger not in ("manual", "daily"):
        raise ValueError("trigger must be manual or daily")
    if trigger == "daily":
        delay = jitter if jitter is not None else rng(60)
        if delay:
            (sleep or time.sleep)(delay)
    total_before, _ = counts(conn, today.isoformat())
    last_success = _get(conn, LAST_SUCCESS_KEY)
    coverage = _get(conn, COVERAGE_KEY)
    floor = today - timedelta(days=BACKFILL_DAYS)
    end = today + timedelta(days=1)        # SimpleFIN's end-date is exclusive: today's transactions are inside it
    if not isinstance(last_success, str):
        start = floor
    else:
        try:
            last_day = date.fromisoformat(last_success[:10])
        except ValueError:
            last_day = today
        start = max(last_day - timedelta(days=OVERLAP_DAYS), floor)
        if isinstance(coverage, str):
            try:
                start = max(start, date.fromisoformat(coverage[:10]))
            except ValueError:
                pass
        if start >= end:                   # defensive only: never shrink a normal window away from today
            start = end - timedelta(days=CHUNK_DAYS)
    windows = _chunks(start, end) if not isinstance(last_success, str) else [(start, end)]
    if trigger == "manual":
        windows = [(max(w[0], w[1] - timedelta(days=CHUNK_DAYS)), w[1]) for w in windows[:1]]
    result = SyncResult(coverage_start=coverage if isinstance(coverage, str) else None)
    client = simplefin.Client(url, transport=transport, resolve=resolve)
    answered = False                       # a transport failure must not count as a fresh sync
    first_trigger = "manual" if trigger == "manual" else "daily"
    for index, (win_start, win_end) in enumerate(windows):
        kind = first_trigger if index == 0 else "backfill"
        try:
            request_id = reserve_request(conn, kind, win_start, win_end)
        except SimpleFinRefused as e:
            result.ok, result.limited, result.message = False, str(e), str(e)
            break
        try:
            payload = client.accounts(win_start, win_end)
        except simplefin.Disconnected as e:
            finish_request(conn, request_id, "disconnected", str(e))
            _mark_disconnected(conn, e)
            answered = True
            result.ok, result.state, result.message = False, _state_of(conn), str(e)
            result.errors = [{"code": x.code, "message": x.message, "user": x.user} for x in e.errors]
            break
        except simplefin.TransportError as e:
            finish_request(conn, request_id, "error", type(e).__name__)
            result.ok, result.message = False, "Couldn't reach SimpleFIN. Nothing changed; try again in a minute."
            log.warning("SimpleFIN fetch failed: %s", type(e).__name__)
            break
        except Exception as e:
            finish_request(conn, request_id, "error", type(e).__name__)
            result.ok, result.message = False, "SimpleFIN returned an answer the app couldn't use. Nothing changed."
            log.warning("SimpleFIN fetch failed: %s", type(e).__name__)
            break
        finish_request(conn, request_id, "ok")
        answered = True
        result.requests += 1
        got = import_window(conn, payload, _window(win_start, win_end), today)
        result.added += got["added"]
        result.modified += got["modified"]
        result.matched += got["matched"]
        result.ambiguous += got["ambiguous"]
        result.stale_removed += got["stale_removed"]
        result.coverage_start = got["coverage_start"]
        result.errors = [{"code": e.code, "message": e.message, "user": e.user} for e in payload.errors]
        _set(conn, LAST_ERRORS_KEY, result.errors)       # the last answer's errlist, a clean one included
    if answered:
        _after_sync(conn, result.as_dict())
    result.state = _state_of(conn)
    result.as_of = as_of(conn)
    if result.ok and not result.message:
        result.message = f"SimpleFIN synced: {result.added} new"
    return result.as_dict()


def _chunks(start: date, end: date, limit: int = 5) -> list[tuple[date, date]]:
    """Newest first, at most 89 days each, with exclusive ends: an older chunk's end is the newer chunk's start."""
    out, cursor = [], end
    floor = max(start, end - timedelta(days=BACKFILL_DAYS))
    while cursor > floor and len(out) < limit:
        chunk_start = max(floor, cursor - timedelta(days=CHUNK_DAYS))
        out.append((chunk_start, cursor))
        cursor = chunk_start
    return out


def summary_text(result: dict[str, Any]) -> str:
    """The one line `finance sync`, the page and the CLI share."""
    if not result.get("connected"):
        return "SimpleFIN: not connected"
    if not result.get("ok"):
        return f"SimpleFIN: ERROR {result.get('message') or 'sync failed'}"
    parts = [f"SimpleFIN: +{result.get('added', 0)} ~{result.get('modified', 0)} -{result.get('removed', 0)}"]
    if result.get("matched"):
        parts.append(f"{result['matched']} pending matched")
    if result.get("ambiguous"):
        parts.append(f"{result['ambiguous']} to review")
    if result.get("stale_removed"):
        parts.append(f"{result['stale_removed']} stale pending")
    return " | ".join(parts)


def as_of(conn: sqlite3.Connection) -> str | None:
    """The date the stored SimpleFIN balances are from (the last sync that delivered accounts)."""
    row = conn.execute(
        """SELECT MAX(a.updated_at) FROM accounts a JOIN items i ON i.item_id = a.item_id
           WHERE i.provider = ? AND i.removed_at IS NULL""", (PROVIDER,)).fetchone()
    return row[0][:10] if row and row[0] else None


# --- status, disconnect, duplicate source pick ----------------------------------------------------------

@_follows_today
def status(conn: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    """What Settings, the Accounts tag and the CLI may know. Never the access URL."""
    today = today or _today()
    items = _items(conn, include_removed=True)
    active = [it for it in items if not it["removed_at"]]
    accounts = _accounts(conn)
    active_ids = {it["item_id"] for it in active}
    live_accounts = [a for a in accounts if a["item_id"] in active_ids]
    pairs = _duplicate_pairs(conn)
    live_ids = {a["account_id"] for a in live_accounts}
    need_review = [p for p in pairs if not p.get("picked") and p["simplefin"] in live_ids]
    policy = _allocated(conn)
    state = _state_of(conn)
    errors = _get(conn, LAST_ERRORS_KEY, [])
    disconnected = _get(conn, DISCONNECTED_KEY)
    b = budget(conn)
    banks = len({it["item_id"] for it in active})
    return {
        "connected": bool(active),
        "state": state,
        "needs_confirm": claim_pending(conn),
        "needs_retry": needs_retry(conn),
        "banks": banks,
        "accounts": len(live_accounts),
        "institution": "SimpleFIN",
        "requests_today": b["requests_today"], "request_limit": b["request_limit"],
        "manual_today": b["manual_today"], "manual_limit": b["manual_limit"], "manual_left": b["manual_left"],
        "daily_minute": daily_minute(conn),
        "coverage_start": _get(conn, COVERAGE_KEY),
        "last_success": _get(conn, LAST_SUCCESS_KEY),
        "as_of": as_of(conn),
        "duplicate_reviews": len(need_review),
        "last_result": _get(conn, LAST_RESULT_KEY),
        "duplicates": [{"simplefin": p["simplefin"], "plaid": p["plaid"],
                        "simplefin_name": p.get("simplefin_name"), "plaid_name": p.get("plaid_name"),
                        "mask": p.get("mask"), "picked": p.get("picked")} for p in need_review],
        "errors": [e for e in errors if isinstance(e, dict)],
        "message": (disconnected or {}).get("message") if isinstance(disconnected, dict) else None,
        "items": [{"item_id": it["item_id"], "name": it["institution_name"], "status": it["status"],
                   "error_code": it["error_code"], "error_message": it["error_message"],
                   "last_sync_at": it["last_sync_at"], "removed_at": it["removed_at"],
                   "accounts": [{"account_id": a["account_id"], "name": a["name"], "mask": a["mask"],
                                 "type": a["type"], "subtype": a["subtype"], "balance": a["current_balance"],
                                 "available": a["available_balance"], "currency": a["iso_currency"],
                                 "conn_id": a["conn_id"], "excluded": a["account_id"] in policy}
                                for a in accounts if a["item_id"] == it["item_id"]]} for it in items],
        "today": today.isoformat(),
    }


def disconnect(conn: sqlite3.Connection) -> dict[str, Any]:
    """Forget the access URL and stop counting the connection's accounts, keeping every row. Local only: nothing is
    revoked at SimpleFIN Bridge (the page says so). A new setup token reuses the deterministic ids."""
    ids = [a["account_id"] for a in _accounts(conn)]
    policy = _allocated(conn) | set(ids)
    # hand counting back to the Plaid copy of every pair picked as 'simplefin', and reset the pick (a reconnect then
    # shows the review again); a 'plaid' or 'not_duplicate' pick is kept so a reconnect does not re-open it
    pairs = _duplicate_pairs(conn)
    for pair in pairs:
        if pair.get("picked") == "simplefin":
            policy.discard(pair["plaid"])
            pair.pop("picked", None)
        elif pair.get("picked") == "plaid":
            policy.discard(pair["plaid"])
        elif pair.get("picked") == "not_duplicate":
            policy.discard(pair["plaid"])                 # the SimpleFIN copy stays out until a reconnect confirms it
    _save_pairs(conn, pairs)
    insights.save_excluded_accounts(conn, sorted(policy))
    with conn:
        conn.execute("UPDATE items SET removed_at = ?, status = 'error', error_code = 'SIMPLEFIN_DISCONNECTED', "
                     "error_message = 'Removed on this computer.' WHERE provider = ? AND removed_at IS NULL",
                     (_now(), PROVIDER))
    secretstore.write({secretstore.SIMPLEFIN: None})
    _del(conn, PENDING_KEY)
    _set(conn, DISCONNECTED_KEY, {"reason": "disconnected", "at": _now(),
                                  "message": "Removed on this computer. Remove the app at SimpleFIN Bridge too."})
    return status(conn)


SOURCE_CHOICES = ("plaid", "simplefin", "not_duplicate")


def pick_duplicate_source(conn: sqlite3.Connection, simplefin_account_id: str, plaid_account_id: str,
                          choice: str) -> dict[str, Any]:
    """The explicit source pick for one Plaid/SimpleFIN duplicate pair. Until it happens the SimpleFIN copy is in the
    account policy, so every total is Plaid-only. Use Plaid keeps that; Use SimpleFIN moves the policy to Plaid;
    Not a duplicate counts both."""
    if choice not in SOURCE_CHOICES:
        raise SimpleFinRefused("choice must be plaid, simplefin or not_duplicate.")
    pairs = _duplicate_pairs(conn)
    pair = next((p for p in pairs if p["simplefin"] == simplefin_account_id
                 and p["plaid"] == plaid_account_id), None)
    if pair is None:
        raise SimpleFinRefused("no SimpleFIN/Plaid duplicate with those accounts", status=404)
    policy = _allocated(conn)
    if choice == "simplefin":
        policy.discard(simplefin_account_id)
        policy.add(plaid_account_id)
    else:
        policy.add(simplefin_account_id)
        if choice == "plaid":
            policy.discard(plaid_account_id)
        else:
            policy.discard(simplefin_account_id)
            policy.discard(plaid_account_id)
    pair["picked"] = choice                               # kept: a reconnect honours it and it leaves the review list
    _save_pairs(conn, pairs)
    insights.save_excluded_accounts(conn, sorted(policy))
    return status(conn)
