"""Sync now, and ask the banks for fresh data (TASK-22): what POST /api/sync, the finance_sync tool and `finance sync`'s
summary share.

  run()             begin() then finish(), in the caller's thread (finance_sync, the CLI). begin(): the checks, nothing
                    called; finish(): the calls. HOTFIX-1: start() runs finish() in a thread of its own for the page
                    (POST /api/sync answers within ANSWER_WITHIN seconds: the result, or 202 and the page asks lookup(),
                    GET /api/sync?rid=, so a dropped connection loses nothing), with one line per sync in serve.log;
                    the refresh calls go REFRESH_AT_ONCE at a time, REFRESH_TIMEOUT seconds each; asked inside an
                    Item's minute, nothing is synced again and the answer says so (sync_skipped).
                    sync.sync_items for every connected Item (or one): one sync at a time in this process (LOCK: a second
                    one is Busy, 409), and an Item synced in the last COOLDOWN seconds is left out (every Item: Cooldown,
                    429, with the seconds left). The cooldown reads sync_log, so it holds across processes (the app, the
                    Hermes plugin, `finance mcp`). With refresh=True, first Plaid's refresh endpoints for each eligible
                    Item (POST /transactions/refresh for the Transactions product, /investments/refresh for Investments),
                    if refresh_policy allows it (else RefreshRefused, 403), an Item asked in the last REFRESH_COOLDOWN
                    seconds left out; then the sync right away. Plaid answers a refresh at once and fetches in the
                    background (there is no webhook receiver here), so nothing waits for it.
  changes()         what a sync changed: new / updated / removed transactions and every balance that moved (largest move
                    first, each with its bank, name, ••last 4 and type: TASK-22f's panel), with the line finance_sync and
                    the CLI print ("3 new transactions · Chase ••1234 $450 → $0").
  new_transactions() TASK-22f: the new spending a sync brought in (money out, not a transfer), largest first, and how
                    many other new rows it brought (transfers and card payments, money in): the panel's right column.
  failure()         TASK-22c: what went wrong, in the words the toast, finance_sync and `finance sync` share: "Couldn't
                    reach Plaid. …" when every bank failed with the same passing error, else the banks that failed in
                    one line and those to reconnect in another. A failed sync starts no cooldown.
  refresh_state()   TASK-22f: for the panel's "Ask banks for fresh data": allowed, billed, the plan, and each bank that can
                    be asked with how long ago it was and the seconds until it can be again (REFRESH_COOLDOWN).
  refresh_policy()  the plaid.on_demand_refresh setting; unset, it's on in Sandbox or on the Trial plan (Plaid bills each
                    refresh on paid plans: https://plaid.com/docs/account/billing/). There is no API for the plan, so
                    it's a setting too (plaid.plan, Trial unless changed). TASK-22g: changing the plan unsets the switch
                    (prefs.save), so the new plan's default applies until it's set again.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config, db, prefs, report
from .money import money
from .db import now
from .plaid import PlaidClient, PlaidError
from .sync import ItemSyncResult, is_transient, sync_items

log = logging.getLogger(__name__)
# HOTFIX-1: one line per sync the page starts (and per dropped answer: web.py), in serve.log even without --verbose
requests_log = logging.getLogger("hermes_finance.requests")
requests_log.setLevel(logging.INFO)

LOCK = threading.Lock()
COOLDOWN = 60                   # seconds between two syncs of one Item
REFRESH_COOLDOWN = 30 * 60      # seconds between two refresh requests for one Item
REFRESH_LOG = "plaid.refresh_log"   # settings row: {item_id: when its banks were last asked}, UTC
REFRESH_NOTE = ("Banks asked for fresh data — new items usually land within a few minutes; tap Sync again then.")
REFRESH_OFF = "On-demand bank refresh is off: Settings → Connections → Allow on-demand bank refresh."
BILLED = "On your paid Plaid plan, Plaid bills each refresh."
UNREACHABLE = "Couldn't reach Plaid. Nothing changed; try again in a minute."   # TASK-22c (the panel: title + line)
TOAST_ROOM = 76                 # characters: two lines of the toast at 360px (TASK-22c, measured in shots/022c)
NEW_ROWS = 5                    # TASK-22f: new spending rows the panel lists (the rest are counted)
NOT_SYNCED = ("Banks asked for fresh data. Not synced again: the last sync was {ago} s ago. New items usually land "
              "within a few minutes; tap Sync then.")   # HOTFIX-1: asked inside the minute after a sync
REFRESH_AT_ONCE = 8             # HOTFIX-1: refresh calls made at the same time
REFRESH_TIMEOUT = 10.0          # seconds a refresh call may take (Plaid answers at once; slower counts as not reached)
ANSWER_WITHIN = 4.0             # seconds POST /api/sync waits for its sync; then 202 and the page asks GET /api/sync
JOBS_KEPT = 16                  # the page's last syncs kept for GET /api/sync


def clock() -> datetime:
    return datetime.now(timezone.utc)   # tests pin this


def make_client() -> PlaidClient:
    """The Plaid client of a process that has none of its own (the tools): the saved keys (tests replace this)."""
    return PlaidClient(config.load_settings())


class SyncRefused(Exception):
    status = 400

    def payload(self) -> dict[str, Any]:
        return {"error": str(self)}


class Busy(SyncRefused):
    status = 409

    def __init__(self) -> None:
        super().__init__("A sync is already running; try again when it's done.")

    def payload(self) -> dict[str, Any]:
        return {"error": str(self), "busy": True}


class Cooldown(SyncRefused):
    status = 429

    def __init__(self, seconds: int) -> None:
        self.retry_after = max(1, int(seconds))
        super().__init__(f"Synced a moment ago. Try again in {self.retry_after} s.")

    def payload(self) -> dict[str, Any]:
        return {"error": str(self), "retry_after": self.retry_after}


class RefreshRefused(SyncRefused):
    status = 403


class UnknownItem(SyncRefused):
    status = 404


# --- the setting -------------------------------------------------------------------------------------------------

def plaid_env() -> str:
    return config.key_values().get("PLAID_ENV") or "production"


def refresh_policy(conn: sqlite3.Connection, env: str | None = None) -> dict[str, Any]:
    """{allowed, set (the owner chose), plan, sandbox, billed (Plaid bills each refresh), reason (when not allowed)}."""
    v = prefs.get_many(conn, ["plaid.plan", "plaid.on_demand_refresh"])
    sandbox = (env or plaid_env()) == "sandbox"
    plan, chosen = v["plaid.plan"], v["plaid.on_demand_refresh"]
    allowed = (sandbox or plan == "trial") if chosen is None else chosen
    billed = plan == "paid" and not sandbox
    return {"allowed": allowed, "set": chosen is not None, "plan": plan, "sandbox": sandbox, "billed": billed,
            "reason": None if allowed else REFRESH_OFF + (f" {BILLED}" if billed else "")}


# --- cooldowns ----------------------------------------------------------------------------------------------------

def _age(ts: str | None, at: datetime) -> float:
    if not ts:
        return float("inf")
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return (at - then).total_seconds()


def _refresh_log(conn: sqlite3.Connection) -> dict[str, str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (REFRESH_LOG,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _log_refresh(conn: sqlite3.Connection, item_ids: list[str]) -> None:
    log = _refresh_log(conn) | {i: now() for i in item_ids}
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                     "SET value = excluded.value, updated_at = excluded.updated_at", (REFRESH_LOG, json.dumps(log), now()))


def _items(conn: sqlite3.Connection, item_id: str | None) -> list[sqlite3.Row]:
    q, args = "SELECT * FROM items WHERE removed_at IS NULL", ()
    if item_id is not None:
        q, args = q + " AND item_id = ?", (item_id,)
    rows = conn.execute(q + " ORDER BY created_at", args).fetchall()
    if item_id is not None and not rows:
        raise UnknownItem(f"no connected bank with item_id {str(item_id)[:80]!r}")
    return rows


def _products(item: sqlite3.Row) -> set[str]:
    try:
        return set(json.loads(item["products"] or "[]"))
    except ValueError:
        return set()


def refresh_products(item: sqlite3.Row) -> list[str]:
    """The refresh endpoints an Item takes: transactions and / or investments (only while it's connected fine)."""
    if item["status"] != "active" or item["error_code"]:
        return []
    return [p for p in ("transactions", "investments") if p in _products(item)]


def plan(conn: sqlite3.Connection, item_id: str | None = None, refresh: bool = False) -> dict[str, Any]:
    """Which Items a run would sync and ask for fresh data, and which wait (with the seconds left). Changes nothing."""
    at = clock()
    items = _items(conn, item_id)
    # (TASK-22c: a sync that failed starts no cooldown: only one that worked, or one still running, counts)
    last = dict(conn.execute("SELECT item_id, MAX(started_at) FROM sync_log WHERE error IS NULL GROUP BY item_id").fetchall())
    asked = _refresh_log(conn)
    sync_now, sync_wait, refresh_now, refresh_wait = [], {}, [], {}
    for it in items:
        age = _age(last.get(it["item_id"]), at)
        if age >= COOLDOWN:
            sync_now.append(it)
        else:
            sync_wait[it["item_id"]] = COOLDOWN - age
        if refresh and refresh_products(it):
            age = _age(asked.get(it["item_id"]), at)
            if age >= REFRESH_COOLDOWN:
                refresh_now.append(it)
            else:
                refresh_wait[it["item_id"]] = REFRESH_COOLDOWN - age
    return {"items": items, "sync": sync_now, "sync_wait": sync_wait, "refresh": refresh_now, "refresh_wait": refresh_wait}


# --- what changed -------------------------------------------------------------------------------------------------

def balances(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every connected account's current balance, with the label a change is shown with ("Chase ••1234") and what
    TASK-22f's panel draws its row with (the bank and its logo, the account's name, ••last 4 and type)."""
    out = {}
    for a in conn.execute("""SELECT a.account_id, a.name, a.mask, a.type, a.subtype, a.current_balance, i.item_id,
                                    i.institution_name, i.institution_id FROM accounts a
                             JOIN items i ON i.item_id = a.item_id WHERE i.removed_at IS NULL"""):
        bank = a["institution_name"] or a["name"] or "Account"
        out[a["account_id"]] = {"label": f"{bank} ••{a['mask']}" if a["mask"] else f"{bank} {a['name'] or ''}".strip(),
                                "balance": a["current_balance"], "item_id": a["item_id"], "bank": bank,
                                "institution_id": a["institution_id"], "name": a["name"], "mask": a["mask"],
                                "type": a["type"], "subtype": a["subtype"]}
    return out


def usd(value: float | None) -> str:
    return money(value, none="—")


def changes(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]],
            results: list[ItemSyncResult]) -> dict[str, Any]:
    """{new, updated, removed, balances: [{account_id, label, before, after, item_id, bank, institution_id, name, mask,
    type, subtype}], text}: the balances largest move first (TASK-22f). The text is None when no bank synced (TASK-22c:
    never "Nothing new" then)."""
    ok = [r for r in results if r.ok]
    new, updated, removed = (sum(getattr(r, k) for r in ok) for k in ("added", "modified", "removed"))
    moved = []
    for aid, a in after.items():   # (an account the sync just added has no balance that moved)
        was = before[aid]["balance"] if aid in before else a["balance"]
        if a["balance"] != was and not (a["balance"] is not None and was is not None and abs(a["balance"] - was) < 0.005):
            moved.append({"account_id": aid, "label": a["label"], "before": was, "after": a["balance"],
                          **{k: a[k] for k in ("item_id", "bank", "institution_id", "name", "mask", "type", "subtype")}})
    moved.sort(key=lambda m: -abs((m["after"] or 0.0) - (m["before"] or 0.0)))   # (stable: DB order within a tie)
    parts = [f"{new} new transaction{'s' if new != 1 else ''}"] if new else []
    parts += [f"{updated} updated"] if updated else []
    parts += [f"{removed} removed"] if removed else []
    parts += [f"{m['label']} {usd(m['before']) if m['before'] is not None else 'new'} → {usd(m['after'])}" for m in moved]
    return {"new": new, "updated": updated, "removed": removed, "balances": moved,
            "text": " · ".join(parts) if parts else "Nothing new" if ok else None}


def new_transactions(conn: sqlite3.Connection, mark: int, item_ids: list[str]) -> dict[str, Any]:
    """TASK-22f: the transactions these Items' sync brought in (the rows stored after rowid `mark`: a sync inserts each new
    one and only updates the others; first_seen_at's whole seconds can't tell a row stored a moment before): the new
    spending (money out, not a transfer or a card payment), largest first, NEW_ROWS of them, each with its merchant's logo
    and the account it's on; and how many others: more spending, transfers and card payments, money in."""
    out = {"new_spend": [], "new_more": {"spend": 0, "transfers": 0, "money_in": 0}}
    if not item_ids:
        return out
    rows = conn.execute(
        f"""SELECT v.transaction_id, v.date, v.name, v.merchant_name, v.amount, v.pending, v.logo_url, v.is_transfer,
                   v.account_name, v.account_mask, v.item_id, v.institution_name, i.institution_id
            FROM v_transactions v JOIN transactions t ON t.transaction_id = v.transaction_id
            JOIN items i ON i.item_id = v.item_id
            WHERE t.rowid > ? AND v.item_id IN ({",".join("?" * len(item_ids))})
            ORDER BY v.amount DESC, v.date DESC, v.transaction_id""", (mark, *item_ids)).fetchall()
    spend = [r for r in rows if not r["is_transfer"] and r["amount"] > 0]
    out["new_spend"] = [{"transaction_id": r["transaction_id"], "date": r["date"], "name": r["name"],
                         "merchant_name": r["merchant_name"], "amount": r["amount"], "pending": bool(r["pending"]),
                         "logo_url": r["logo_url"], "bank": r["institution_name"], "institution_id": r["institution_id"],
                         "account_name": r["account_name"], "mask": r["account_mask"]} for r in spend[:NEW_ROWS]]
    out["new_more"] = {"spend": max(0, len(spend) - NEW_ROWS), "transfers": sum(bool(r["is_transfer"]) for r in rows),
                       "money_in": sum(not r["is_transfer"] and r["amount"] <= 0 for r in rows)}
    return out


def summary_line(c: dict[str, Any]) -> str:
    return f"what changed: {c['text']}"


def _banks(rs: list[ItemSyncResult], room: int) -> str:
    """"Chase", or "3 banks (Chase, Harbor, Amex)": as many names as keep "Couldn't sync …." within `room` characters,
    then "+N" ("8 banks" when not even one fits)."""
    names = [r.institution_name or "a bank" for r in rs]
    if len(names) == 1:
        return names[0]
    best = f"{len(names)} banks"
    for k in range(1, len(names) + 1):
        more = len(names) - k
        said = f"{len(names)} banks ({', '.join(names[:k])}{f' +{more}' if more else ''})"
        if len(f"Couldn't sync {said}.") > room:
            break
        best = said
    return best


def failure(results: list[ItemSyncResult]) -> dict[str, Any] | None:
    """TASK-22c: None when every bank synced. Every bank failed with the same passing error (sync.is_transient: the
    network, Plaid, a rate limit, the bank down): {unreachable: True, lines: [UNREACHABLE]}. Otherwise the banks that
    failed in one line, and those to reconnect (ITEM_LOGIN_REQUIRED only) in another: TOAST_ROOM characters in all (two
    lines of the toast on a 360px phone)."""
    bad = [r for r in results if not r.ok]
    if not bad:
        return None
    if len(bad) == len(results) and len({r.error_code for r in bad}) == 1 and is_transient(bad[0].error_code, bad[0].error_type):
        return {"unreachable": True, "lines": [UNREACHABLE]}
    login, fix = [r for r in bad if r.error_code == "ITEM_LOGIN_REQUIRED"], []
    if login:
        name = login[0].institution_name or ""
        who = name if len(login) == 1 and 0 < len(name) <= 24 else f"{len(login)} bank{'s' if len(login) > 1 else ''}"
        fix = [f"Reconnect {who} on Accounts."]
    room = TOAST_ROOM - sum(len(x) + 1 for x in fix)
    return {"unreachable": False, "lines": [f"Couldn't sync {_banks(bad, room)}.", *fix]}


def message(c: dict[str, Any], fail: dict[str, Any] | None) -> str:
    """The sync's one line for the toast, finance_sync and `finance sync` (TASK-22c): what changed, or what failed (the
    count of what came in from the banks that did sync is in `changes`: the toast has room for two lines)."""
    if fail is None:
        return c["text"] or ""
    return " ".join(fail["lines"])


# --- a run --------------------------------------------------------------------------------------------------------

def _item_json(r: ItemSyncResult, institution_id: str | None = None) -> dict[str, Any]:
    return {"item_id": r.item_id, "institution_name": r.institution_name, "institution_id": institution_id,
            "ok": r.ok, "added": r.added,
            "modified": r.modified, "removed": r.removed, "error_code": r.error_code,
            "error": None if r.ok else r.summary(), "extras": r.extras}


def _ask(plaid: PlaidClient, items: list[sqlite3.Row]) -> tuple[list[dict[str, Any]], set[str]]:
    """Ask these Items' banks for fresh data (each one's refresh endpoints): HOTFIX-1: REFRESH_AT_ONCE calls at a time,
    each given REFRESH_TIMEOUT seconds (13 of them one after another held the live request 40 s or more, and the phone
    dropped it). What each Item was asked for (in the Items' order), and the Items not reached at all for a passing error
    (TASK-22c: they may be asked again right away)."""
    calls = [(it, product) for it in items for product in refresh_products(it)]

    def one(call: tuple[sqlite3.Row, str]) -> PlaidError | None:
        it, product = call
        try:
            (plaid.transactions_refresh if product == "transactions" else plaid.investments_refresh)(
                it["access_token"], timeout=REFRESH_TIMEOUT)
        except PlaidError as e:
            return e
        return None

    with ThreadPoolExecutor(max_workers=max(1, min(REFRESH_AT_ONCE, len(calls))), thread_name_prefix="plaid-refresh") as pool:
        outcomes = dict(zip([(it["item_id"], product) for it, product in calls], pool.map(one, calls)))
    asked, passing = [], set()
    for it in items:
        done = {"item_id": it["item_id"], "institution_name": it["institution_name"], "products": [], "ok": True}
        for product in refresh_products(it):
            e = outcomes[(it["item_id"], product)]
            if e is None:
                done["products"].append(product)
                continue
            done.update(ok=False, error_code=e.error_code)
            if is_transient(e.error_code, e.error_type):
                passing.add(it["item_id"])
        if done["products"]:
            passing.discard(it["item_id"])
        asked.append(done)
    return asked, passing


def begin(conn: sqlite3.Connection, client: Callable[[], PlaidClient], item_id: str | None = None,
          refresh: bool = False) -> dict[str, Any]:
    """The checks before a sync, with nothing called yet: raises a SyncRefused (Busy, Cooldown, RefreshRefused,
    UnknownItem), or config.ConfigError when the Plaid keys are missing. Otherwise LOCK is held until finish() and this
    is the run: the plan, the Plaid client (made only when something is to be called) and `refresh`."""
    if refresh:
        policy = refresh_policy(conn)
        if not policy["allowed"]:
            raise RefreshRefused(policy["reason"])
    if not LOCK.acquire(blocking=False):
        raise Busy()
    try:
        p = plan(conn, item_id, refresh)
        if p["items"] and not p["sync"] and not p["refresh"]:
            raise Cooldown(min([*p["sync_wait"].values(), *p["refresh_wait"].values()]))
        p.update(plaid=client() if p["sync"] or p["refresh"] else None, refresh_run=refresh, locked=True)
    except BaseException:
        LOCK.release()
        raise
    return p


def _release(p: dict[str, Any]) -> None:
    if p.pop("locked", False):
        LOCK.release()


def finish(conn: sqlite3.Connection, p: dict[str, Any]) -> dict[str, Any]:
    """The run begin() let through: ask the banks (with refresh), then sync the Items out of their minute; LOCK is
    released at the end. What happened, for the page, finance_sync and the CLI."""
    refresh, plaid = p["refresh_run"], p["plaid"]
    try:
        asked, passing = _ask(plaid, p["refresh"]) if p["refresh"] else ([], set())
        if asked:   # (TASK-22c: a bank not reached, for the network or the like, may be asked again right away)
            _log_refresh(conn, [a["item_id"] for a in asked if a["item_id"] not in passing])
        previous = _last_sync(conn)   # TASK-22f: "Nothing new since 6:31 AM."
        mark, before = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM transactions").fetchone()[0], balances(conn)
        results = sync_items(conn, plaid, p["sync"]) if p["sync"] else []
        c = changes(before, balances(conn), results) | new_transactions(conn, mark, [r.item_id for r in results if r.ok])
    finally:
        _release(p)
    fail = failure(results)   # (TASK-22c: Plaid out of reach is one line, not one error per bank)
    inst = {it["item_id"]: it["institution_id"] for it in p["items"]}
    # (TASK-22f: banks = the Plaid banks a sync covers, the panel's "All 8 banks"; previous_sync = the last one before)
    out = {"ok": all(r.ok for r in results), "synced": sum(r.ok for r in results), "banks": len(p["items"]),
           "message": message(c, fail) if p["items"] else "No bank is connected yet.",
           "unreachable": bool(fail and fail["unreachable"]),
           "items": [] if fail and fail["unreachable"] else [_item_json(r, inst.get(r.item_id)) for r in results],
           "waiting": [{"item_id": i, "retry_after": int(s) + 1} for i, s in p["sync_wait"].items()],
           "changes": c, "errors": fail["lines"] if fail else [],
           "last_sync": _last_sync(conn), "previous_sync": previous,
           "data_age": report.data_age(conn), "refresh": refresh_state(conn)}
    if refresh:
        requested = [a for a in asked if a["products"]]
        out.update(refresh_requested=requested, refresh_failed=[a for a in asked if not a["ok"]],
                   refresh_waiting=[{"item_id": i, "retry_after": int(s) + 1} for i, s in p["refresh_wait"].items()],
                   note=REFRESH_NOTE if requested else UNREACHABLE if passing else   # (TASK-22c)
                   "Banks were asked for fresh data in the last 30 minutes; tap Sync in a few minutes." if p["refresh_wait"]
                   else "No connected bank can be asked for fresh data right now.")
        if not results and p["sync_wait"]:
            # HOTFIX-1: every bank synced under a minute ago, so asking them didn't sync them again: say so, and when a
            # sync can run (never "synced"; the live evening's "Ask banks" came 40 s after a sync)
            ago, wait = COOLDOWN - max(p["sync_wait"].values()), min(p["sync_wait"].values())
            out["sync_skipped"] = {"ago": max(0, int(ago)), "retry_after": int(wait) + 1}
            if requested:
                out["note"] = NOT_SYNCED.format(ago=max(1, int(ago)))
            out["message"] = out["note"]
    return out


def run(conn: sqlite3.Connection, client: Callable[[], PlaidClient], item_id: str | None = None,
        refresh: bool = False) -> dict[str, Any]:
    """Sync now (and first ask the banks for fresh data, with refresh), in this thread: finance_sync and the CLI (the
    page's POST /api/sync runs it in the background: start()). `client` makes the Plaid client, only once something is
    to be called. Raises a SyncRefused (Busy, Cooldown, RefreshRefused, UnknownItem): nothing was called."""
    return finish(conn, begin(conn, client, item_id, refresh))


# --- the page's syncs, in the background (HOTFIX-1) ---------------------------------------------------------------

class Job:
    """A sync the page started (POST /api/sync), run in its own thread on its own connection: the request that started
    it may drop (a phone through a proxy: the browser's "Load failed") and nothing is lost; GET /api/sync?rid= says how it
    went. The last JOBS_KEPT are kept, in memory."""

    def __init__(self, rid: str, refresh: bool, banks: int):
        self.rid, self.refresh, self.banks = rid, refresh, banks
        self.started_at, self.t0, self.secs = now(), time.monotonic(), None
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.answered_202 = False   # the POST stopped waiting: the page looks it up
        self.fetched = False        # a look-up returned it finished

    def state(self) -> dict[str, Any]:
        s = {"rid": self.rid, "found": True, "running": not self.done.is_set(), "refresh": self.refresh,
             "started_at": self.started_at, "elapsed": round(time.monotonic() - self.t0, 1)}
        if self.done.is_set():
            s.update(result=self.result, error=self.error)
        return s

    def line(self) -> str:
        """serve.log's one line for it: sync or refresh, the banks, how long, what happened (counts only)."""
        how = "answered 202, the page looks it up" if self.answered_202 else "answered in the request"
        return f"POST /api/sync {self.rid[:12]}: {'refresh' if self.refresh else 'sync'}, {self.banks} bank(s), " \
               f"{self.secs:.1f} s, {how}: {outcome(self.result, self.error)}"


JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def outcome(r: dict[str, Any] | None, error: str | None = None) -> str:
    """A sync's result in a few words for the log: counts, no amounts or names."""
    if error or r is None:
        return f"failed: {error}"
    parts = []
    if "refresh_requested" in r:
        parts.append(f"asked {len(r['refresh_requested'])} bank(s)" + (f", {len(r['refresh_failed'])} refused or not reached"
                                                                       if r["refresh_failed"] else "")
                     + (f", {len(r['refresh_waiting'])} in their 30 min" if r["refresh_waiting"] else ""))
    if r.get("sync_skipped"):
        parts.append(f"not synced: every bank synced under a minute ago (the last {r['sync_skipped']['ago']} s ago)")
    elif r["banks"]:
        c = r["changes"]
        parts.append(f"synced {r['synced']} of {r['banks']}" + (f" ({len(r['waiting'])} in their minute)" if r["waiting"] else "")
                     + f", {c['new']} new, {c['updated']} updated, {len(c['balances'])} balance(s) moved"
                     + (", Plaid unreachable" if r["unreachable"] else f", {len(r['errors'])} error line(s)" if r["errors"] else ""))
    else:
        parts.append("no bank connected")
    return "; ".join(parts)


def start(db_path: Path, conn: sqlite3.Connection, client: Callable[[], PlaidClient], item_id: str | None = None,
          refresh: bool = False, rid: str | None = None) -> Job:
    """POST /api/sync: begin() here (a refusal raises at once, nothing called), then finish() in a thread of its own."""
    p = begin(conn, client, item_id, refresh)
    try:
        job = Job(rid or uuid.uuid4().hex, refresh, len(p["items"]))
        with _JOBS_LOCK:
            JOBS[job.rid] = job
            while len(JOBS) > JOBS_KEPT:
                JOBS.pop(next(iter(JOBS)))
        threading.Thread(target=_work, args=(job, db_path, p), name=f"sync-{job.rid[:8]}", daemon=True).start()
    except BaseException:   # (no thread: the lock goes back)
        _release(p)
        raise
    return job


def _work(job: Job, db_path: Path, p: dict[str, Any]) -> None:
    conn = None
    try:
        conn = db.connect(db_path)
        job.result = finish(conn, p)
    except Exception as e:   # (never a traceback to the page; the log has it)
        log.exception("sync %s failed", job.rid[:12])
        job.error = f"internal error ({type(e).__name__})"
    finally:
        _release(p)
        if conn is not None:
            conn.close()
        job.secs = time.monotonic() - job.t0
        requests_log.info(job.line())   # (before done: the POST still waiting on it answers after the line)
        job.done.set()


def lookup(conn: sqlite3.Connection, rid: str | None) -> dict[str, Any]:
    """GET /api/sync?rid=: that sync (running, or its result), else {found: False} with the last sync that worked (the
    server never got it, or restarted since); without rid, the one running now, if any."""
    with _JOBS_LOCK:
        job = JOBS.get(rid) if rid else next((j for j in reversed(JOBS.values()) if not j.done.is_set()), None)
    if job is None:
        return {"rid": rid, "found": False, "running": False, "last_sync": _last_sync(conn), "data_age": report.data_age(conn)}
    state = job.state()
    if not state["running"] and not job.fetched and job.answered_202:
        job.fetched = True
        requests_log.info("GET /api/sync %s: the page picked up the result %.1f s after the start", job.rid[:12],
                          time.monotonic() - job.t0)
    return state


def _last_sync(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MAX(last_sync_at) FROM items WHERE removed_at IS NULL").fetchone()[0]


def refresh_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """For the page: may it offer "Ask banks for fresh data", does Plaid bill it, the plan (and Sandbox), and (TASK-22f)
    each bank that can be asked: how many seconds ago it was last asked (None: never) and how many until it can be again
    (0: now), so the panel can offer "Ask 7 banks for fresh data" and say when the rest are available again."""
    policy = refresh_policy(conn)
    at, asked = clock(), _refresh_log(conn)
    banks = []
    for it in _items(conn, None):
        if not refresh_products(it):
            continue
        age = _age(asked.get(it["item_id"]), at)
        banks.append({"item_id": it["item_id"], "institution_name": it["institution_name"],
                      "institution_id": it["institution_id"], "asked_ago": None if age == float("inf") else max(0, int(age)),
                      "retry_after": math.ceil(REFRESH_COOLDOWN - age) if age < REFRESH_COOLDOWN else 0})
    return {"allowed": policy["allowed"], "billed": policy["billed"], "plan": policy["plan"], "sandbox": policy["sandbox"],
            "banks": banks}
