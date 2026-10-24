"""The guard around every outbound valuation / photo call (TASK-12's free-tier guard, kept for the free sources).

Every outbound call is reserved first by reserve(), in ONE `BEGIN IMMEDIATE` transaction, so two runs at the same time
(the daily run and a Refresh click, two dashboard threads) can't both take the last call:

  * a provider blocked after a refusal stays blocked until its `blocked_until` (NHTSA: the 1st of the next month;
    Carfax / Redfin, whose refusal is bot protection, and the photo sources: config.BLOCK_DAYS days), persisted in
    api_usage, so a restart doesn't forget it;
  * `api_usage.calls` for this local month must be under the cap (config.api_caps(): NHTSA 300, Carfax 6 and Redfin
    20 lookups, each lookup counted once, Wikimedia Commons 60 and OpenStreetMap 20 for the photos);
  * the calls in the last 32 local days (api_calls) must be under the cap too, so a provider whose billing month starts on
    another day than the 1st still sees at most `cap` calls in any of its billing months;
  * per account (the value lookups, Carfax and Redfin): one call per local day at most (a save or a Refresh is refused
    on a day it was already looked up, by anything), and the daily run only 30+ days after the last call for that
    account. NHTSA's VIN decoder (once per VIN) and the photo fetches (once per car / home) have no per-account limit.

The count goes up before the request is sent, so a failed call, a 429 and a timeout all count. finish() records how
it went; block() stops the provider until block_end(). Nothing here ever retries.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config
from .money import money
from .db import now

PROVIDERS = ("nhtsa", "carfax", "redfin", "commons", "osm")
LABELS = {"nhtsa": "NHTSA", "carfax": "Carfax", "redfin": "Redfin", "commons": "Wikimedia Commons",
          "osm": "OpenStreetMap"}
PER_ACCOUNT = frozenset({"carfax", "redfin"})   # the value lookups: one a day per account, monthly in the daily run
KINDS = {"nhtsa": "free, no key", "carfax": "free page, no key; each lookup counts once",
         "redfin": "free page, no key; each lookup counts once", "commons": "free API, no key (car photos)",
         "osm": "free map tiles, no key (home maps)"}
WINDOW_DAYS = 32        # the rolling window: any billing month fits in it (31 UTC days span 32 local ones)
SCHEDULED_DAYS = 30     # the daily run: one call per account per 30 days
ON_DEMAND = ("save", "refresh")
TRIGGERS = ("save", "refresh", "daily")


class Refused(Exception):
    """No call was made (and none was counted): blocked, over the cap, or not allowed for this account yet."""

    def __init__(self, provider: str, reason: str, message: str):
        super().__init__(message)
        self.provider = provider
        self.reason = reason  # blocked | cap | window | today | not_due | config | busy


@dataclass
class Reservation:
    call_id: int
    provider: str
    account_id: int | None
    trigger: str
    month: str
    calls: int      # this month's count, this call included
    cap: int


def month_of(day: date) -> str:
    return day.isoformat()[:7]


def next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def local_day(ts: str | None) -> str | None:
    """The local YYYY-MM-DD of a UTC timestamp (as db.now() writes them)."""
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone().date().isoformat()


def _blocked(conn: sqlite3.Connection, provider: str, today: date) -> sqlite3.Row | None:
    return conn.execute("SELECT blocked_until, blocked_reason FROM api_usage WHERE provider = ? AND blocked_until > ? "
                        "ORDER BY blocked_until DESC LIMIT 1", (provider, today.isoformat())).fetchone()


blocked = _blocked  # the block in force today (blocked_until, blocked_reason), or None


def _month_calls(conn: sqlite3.Connection, provider: str, month: str) -> int:
    r = conn.execute("SELECT calls FROM api_usage WHERE provider = ? AND month = ?", (provider, month)).fetchone()
    return r["calls"] if r else 0


def _window_calls(conn: sqlite3.Connection, provider: str, today: date) -> int:
    since = (today - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    return conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = ? AND day >= ?", (provider, since)).fetchone()[0]


def last_call(conn: sqlite3.Connection, provider: str, account_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM api_calls WHERE provider = ? AND account_id = ? ORDER BY id DESC LIMIT 1",
                        (provider, account_id)).fetchone()


def check(conn: sqlite3.Connection, provider: str, today: date, account_id: int | None = None,
          trigger: str = "refresh", caps: dict[str, int] | None = None) -> int:
    """Raise Refused if a call now isn't allowed; else return the provider's cap. Reads only (reserve() runs it inside
    its write lock; dry runs and the dashboard's "can I refresh?" use it as is)."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")
    name = LABELS[provider]
    try:
        cap = (caps or config.api_caps())[provider]
    except config.ConfigError as e:
        raise Refused(provider, "config", f"{name} is off: {e}") from None
    b = _blocked(conn, provider, today)
    if b:
        raise Refused(provider, "blocked", f"{name} is paused until {b['blocked_until']}: {b['blocked_reason']}")
    month = month_of(today)
    used = _month_calls(conn, provider, month)
    if used >= cap:
        raise Refused(provider, "cap", f"{name}'s monthly limit is reached ({used} of {cap} calls this month); "
                                       f"it resumes on {next_month(today).isoformat()}")
    window = _window_calls(conn, provider, today)
    if window >= cap:
        raise Refused(provider, "window", f"{name} made {window} calls in the last {WINDOW_DAYS} days (limit {cap}); "
                                          "it resumes as older calls age out")
    if account_id is None:
        return cap
    last = last_call(conn, provider, account_id)
    if provider not in PER_ACCOUNT:
        return cap  # the VIN decoder (once per VIN) and the photos (once per car / home): the monthly cap only
    if trigger in ON_DEMAND:
        if last and last["day"] >= today.isoformat():
            raise Refused(provider, "today", f"already asked {name} today for this account; again tomorrow")
    elif last:
        due = date.fromisoformat(last["day"]) + timedelta(days=SCHEDULED_DAYS)
        if today < due:
            raise Refused(provider, "not_due", f"{name} was asked on {last['day']}; next monthly check on "
                                               f"{due.isoformat()}")
    return cap


def reserve(conn: sqlite3.Connection, provider: str, today: date, account_id: int | None = None,
            trigger: str = "refresh") -> Reservation:
    """Check and count one call atomically (BEGIN IMMEDIATE ... COMMIT), before it's made. Raises Refused (nothing
    counted) when it isn't allowed. The connection must not be inside a transaction."""
    if conn.in_transaction:
        raise RuntimeError("usage.reserve() needs a connection with no open transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:  # the DB stayed locked past busy_timeout: don't call
        raise Refused(provider, "busy", f"{LABELS.get(provider, provider)} skipped: the database is busy ({e})") from None
    try:
        cap = check(conn, provider, today, account_id, trigger)  # a bad cap config: Refused(config), nothing counted
        month = month_of(today)
        ts = now()
        conn.execute("INSERT INTO api_usage (provider, month, calls, updated_at) VALUES (?, ?, 1, ?) "
                     "ON CONFLICT(provider, month) DO UPDATE SET calls = calls + 1, updated_at = excluded.updated_at",
                     (provider, month, ts))
        cur = conn.execute("INSERT INTO api_calls (provider, account_id, trigger, at, day) VALUES (?, ?, ?, ?, ?)",
                           (provider, account_id, trigger, ts, today.isoformat()))
        calls = _month_calls(conn, provider, month)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return Reservation(cur.lastrowid, provider, account_id, trigger, month, calls, cap)


def finish(conn: sqlite3.Connection, res: Reservation, outcome: str, status: int | None = None,
           note: str | None = None, block_reason: str | None = None, today: date | None = None) -> str | None:
    """Record how the call went; with `block_reason`, block the provider in the same transaction (so the outcome and
    the block are saved together or not at all). Returns the day a block lasts until."""
    with conn:
        conn.execute("UPDATE api_calls SET outcome = ?, status = ?, note = ? WHERE id = ?",
                     (outcome, status, note[:300] if note else None, res.call_id))
        return _block(conn, res.provider, today or date.today(), block_reason) if block_reason else None


def block_end(provider: str, today: date) -> date:
    """The day a block set today ends: the 1st of next month, or today + BLOCK_DAYS (Carfax, Redfin)."""
    days = config.BLOCK_DAYS.get(provider)
    return today + timedelta(days=days) if days else next_month(today)


def _block(conn: sqlite3.Connection, provider: str, today: date, reason: str) -> str:
    until = block_end(provider, today).isoformat()
    conn.execute("INSERT INTO api_usage (provider, month, calls, blocked_until, blocked_reason, updated_at) "
                 "VALUES (?, ?, 0, ?, ?, ?) ON CONFLICT(provider, month) DO UPDATE SET "
                 "blocked_until = excluded.blocked_until, blocked_reason = excluded.blocked_reason, "
                 "updated_at = excluded.updated_at", (provider, month_of(today), until, reason[:300], now()))
    return until


def block(conn: sqlite3.Connection, provider: str, today: date, reason: str) -> str:
    """Stop `provider` until block_end() (persisted). Returns that day."""
    with conn:
        return _block(conn, provider, today, reason)


def unblock(conn: sqlite3.Connection, provider: str) -> int:
    """`finance api-usage --unblock`: lift a block by hand (e.g. once Carfax lets the browser through again). The caps still apply."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    with conn:
        return conn.execute("UPDATE api_usage SET blocked_until = NULL, blocked_reason = NULL, updated_at = ? "
                            "WHERE provider = ? AND blocked_until IS NOT NULL", (now(), provider)).rowcount


# --- TASK-23: the Ask chat's monthly spend cap (prefs ai.monthly_cap, in dollars) --------------------------------------
# The same two tables, provider "ai": api_usage.calls counts the brain requests of the local month, and each request is
# one api_calls row (reserved before it's sent, like every other outbound call) whose note records what it cost
# ("usd=0.004200 in=1000 out=100 reported|estimated"). It isn't one of PROVIDERS (those have call caps, this one a
# dollar cap), so `finance api-usage` and its caps are unchanged.
# A soft cap, a stop after settlement (TASK-23 review C3): what a request costs is only known once it's answered, so
# only one is out at a time per database (a row still 'pending' is one that's out: ai_reserve refuses another,
# reason "inflight"), and none is sent once the settled spend reached the cap. The month ends over the cap by at most
# the request that crossed it. A row left 'pending' past AI_INFLIGHT_STALE (the app stopped mid-request, or the database
# stayed locked past its settlement's last try) is settled 'interrupted' (its cost unknown: none recorded, not even a
# timed-out request's worst case, so such a month can end over the cap by that much more) and blocks nothing. `at` is
# wall-clock time: a clock stepped forward past the margin can settle a request that is still out.
AI = "ai"
AI_NOTE = re.compile(r"usd=(\d+(?:\.\d+)?)")
AI_INFLIGHT_STALE = 180   # seconds: longer than any Ask request can be out (all of it ends by its turn's deadline,
                          # chat.TIMEOUT) and its settlement tried (until chat.TIMEOUT + chat.SETTLE: 90 s, pinned by a
                          # test; tried, not sure to be written: see above)


class CapReached(Refused):
    """This month's Ask spend reached ai.monthly_cap: no request was sent (and none was counted)."""


def ai_spent(conn: sqlite3.Connection, today: date) -> float:
    """What the Ask requests of `today`'s local month cost, in dollars (reported by the endpoint, or estimated)."""
    month = month_of(today)
    total = 0.0
    for (note,) in conn.execute("SELECT note FROM api_calls WHERE provider = ? AND substr(day, 1, 7) = ?", (AI, month)):
        m = AI_NOTE.search(note or "")
        if m:
            total += float(m.group(1))
    return round(total, 6)


def ai_reserve(conn: sqlite3.Connection, today: date, cap_usd: float) -> Reservation:
    """Count one Ask request before it's sent, in one BEGIN IMMEDIATE transaction; Refused "inflight" (nothing counted)
    while another Ask request is out, CapReached when the month's settled spend is at the cap already."""
    if conn.in_transaction:
        raise RuntimeError("usage.ai_reserve() needs a connection with no open transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        raise Refused(AI, "busy", f"Ask skipped: the database is busy ({e})") from None
    refused: Refused | None = None
    try:
        stale = (datetime.now(timezone.utc) - timedelta(seconds=AI_INFLIGHT_STALE)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("UPDATE api_calls SET outcome = 'interrupted' WHERE provider = ? AND outcome = 'pending' AND at < ?",
                     (AI, stale))
        if conn.execute("SELECT 1 FROM api_calls WHERE provider = ? AND outcome = 'pending' LIMIT 1", (AI,)).fetchone():
            refused = Refused(AI, "inflight", "Ask is still answering another question. Try again in a moment.")
        elif (spent := ai_spent(conn, today)) >= cap_usd:
            refused = CapReached(AI, "cap", f"This month's AI spend cap is reached ({money(spent)} of {money(cap_usd)}). Ask "
                                            f"is paused until {next_month(today).isoformat()}, or raise the cap in "
                                            "Settings \u2192 AI provider.")
        else:
            month, ts = month_of(today), now()
            conn.execute("INSERT INTO api_usage (provider, month, calls, updated_at) VALUES (?, ?, 1, ?) "
                         "ON CONFLICT(provider, month) DO UPDATE SET calls = calls + 1, updated_at = excluded.updated_at",
                         (AI, month, ts))
            cur = conn.execute("INSERT INTO api_calls (provider, account_id, trigger, at, day, note) VALUES (?, NULL, "
                               "'refresh', ?, ?, ?)", (AI, ts, today.isoformat(), "usd=0"))
            calls = _month_calls(conn, AI, month)
        conn.execute("COMMIT")   # (a refusal counts nothing; the stale rows settled above are kept)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if refused is not None:
        raise refused
    return Reservation(cur.lastrowid, AI, None, "refresh", month, calls, 0)


def ai_finish(conn: sqlite3.Connection, res: Reservation, outcome: str, status: int | None, usd: float,
              tokens_in: int = 0, tokens_out: int = 0, estimated: bool = True, via: str | None = None) -> None:
    """Record how an Ask request went and what it cost (this is what ai_spent adds up). `via`: the brain that isn't
    the API key's (chat.VIA_HERMES: the request went through Hermes's completion bridge), named in the note."""
    note = f"usd={max(usd, 0.0):.6f} in={int(tokens_in)} out={int(tokens_out)} {'estimated' if estimated else 'reported'}"
    if via:
        note += f" via={via}"
    with conn:
        conn.execute("UPDATE api_calls SET outcome = ?, status = ?, note = ? WHERE id = ?", (outcome, status, note, res.call_id))


def report(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    """`finance api-usage`: calls used / cap per provider this month (and the rolling window). Only the providers in
    use: rows older ones left in api_usage are history."""
    month = month_of(today)
    try:
        caps, cap_error = config.api_caps(), None
    except config.ConfigError as e:
        caps, cap_error = dict(config.API_CAPS), str(e)
    out = []
    for p in PROVIDERS:
        b = _blocked(conn, p, today)
        out.append({"provider": p, "label": LABELS[p], "month": month, "calls": _month_calls(conn, p, month),
                    "cap": caps[p], "what": KINDS[p], "last_31_days": _window_calls(conn, p, today),
                    "blocked_until": b["blocked_until"] if b else None,
                    "blocked_reason": b["blocked_reason"] if b else None})
    return {"today": today.isoformat(), "month": month, "resets": next_month(today).isoformat(),
            "config_error": cap_error, "providers": out}


def report_text(p: dict[str, Any]) -> str:
    lines = [f"API usage for {p['month']} (resets {p['resets']}; every call counts, failed ones too):"]
    for x in p["providers"]:
        blocked = f"  BLOCKED until {x['blocked_until']}: {x['blocked_reason']}" if x["blocked_until"] else ""
        lines.append(f"  {x['label']:<17} {x['calls']:>4} used / cap {x['cap']} / {x['what']}  "
                     f"({x['last_31_days']} in the last {WINDOW_DAYS} days){blocked}")
    if p["config_error"]:
        lines.append(f"config error, no calls are made: {p['config_error']}")
    return "\n".join(lines)
