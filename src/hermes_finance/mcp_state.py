"""What the MCP server keeps in the database (TASK-17): two settings and the pending_actions table.

  mcp.allow_writes  true: the MCP server lists the write tools (finance_set_category, finance_add_rule,
                    finance_set_cap, finance_track_bill), and a call only records a pending action, which the owner
                    approves or declines in Settings (decide()). Off unless set (`finance mcp allow-writes on`).
  mcp.last_used     {client, title, version, transport, at}: the last MCP client that connected (its clientInfo), for
                    the Settings page. `at` is a UTC timestamp.

A pending action's `origin` (ORIGINS) is who recorded it, set by the code path that records it: `client` is only the
name a client gave itself, which anyone can make "Ask" (TASK-23 review C1). "ask" is an Ask turn answered with the app's
API key, "ask_hermes" one answered through Hermes's completion bridge (chat.py, bridge.py); both are the Ask panel's own. One recorded `held_by` a running Ask turn
is neither listed nor decidable until the turn release()s it with its answer, or withdraw()s it when it fails (C2).
reconcile() settles what a process that stopped left behind: a held row whose turn can't be running any more is
withdrawn, and an Approve still being applied long after it was claimed is a final "may be partly done" failure,
never pending again (C5).

Stdlib only, so `/api/settings` and `finance mcp status` read it without the optional mcp package.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Callable

from .db import now

log = logging.getLogger(__name__)

ALLOW_WRITES = "mcp.allow_writes"
LAST_USED = "mcp.last_used"
MAX_NAME = 200   # a client's name / title / version, as stored
ASK_OWNER_PREFIX = "Ask owner:"       # internal client value; _row exposes only client=Ask
ORIGINS = ("mcp", "ask", "ask_hermes")   # pending_actions.origin: the MCP server (any client), the Ask panel's own
                                         # turn (API key), the Ask panel's own turn through the Hermes bridge
HELD_STALE = 300           # seconds: a held row older than this has no turn that can still publish it (an Ask turn
                           # releases its cards before its deadline, chat.TIMEOUT, or not at all, and tries to
                           # withdraw them until chat.TIMEOUT + chat.SETTLE: 90 s, pinned by a test). One it couldn't
                           # withdraw (the database stayed locked), or whose turn a finance_sync held longer, is
                           # withdrawn here; that turn then can't publish (its release counts its cards). created_at is
                           # wall-clock time
APPLY_STALE = 600          # seconds: an Approve claimed longer ago than this and still "approved": its applier stopped
WITHDRAWN = "the Ask answer that suggested it failed"
ABANDONED = "its Ask answer never finished (the app stopped)"


def _setting(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        return None


def _save(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                     "SET value = excluded.value, updated_at = excluded.updated_at", (key, json.dumps(value), now()))


def allow_writes(conn: sqlite3.Connection) -> bool:
    return _setting(conn, ALLOW_WRITES) is True


def set_allow_writes(conn: sqlite3.Connection, on: bool) -> None:
    _save(conn, ALLOW_WRITES, bool(on))


def last_used(conn: sqlite3.Connection) -> dict[str, Any] | None:
    value = _setting(conn, LAST_USED)
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:MAX_NAME] or None


def record_last_used(conn: sqlite3.Connection, client: dict[str, Any], transport: str) -> dict[str, Any]:
    """Save who connected: `client` is the MCP clientInfo ({name, title?, version})."""
    value = {"client": _text(client.get("name")) or "unknown", "title": _text(client.get("title")),
             "version": _text(client.get("version")), "transport": transport, "at": now()}
    _save(conn, LAST_USED, value)
    return value


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM pending_actions WHERE status = 'pending' AND held_by IS NULL").fetchone()[0]


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """The MCP block of `/api/settings`."""
    return {"allow_writes": allow_writes(conn), "last_used": last_used(conn), "pending": pending_count(conn)}


def record_pending(conn: sqlite3.Connection, tool: str, arguments: dict[str, Any], summary_text: str,
                   client: str | None, origin: str = "mcp", held_by: str | None = None) -> dict[str, Any]:
    """A change an MCP client (or, origin ask, the Ask panel) asked for, waiting for the owner (never applied here).
    `origin` comes from the caller's own code path, never from the client; `held_by` holds it back until release()."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown pending action origin {origin!r}")
    with conn:   # (read back inside the transaction: a failure anywhere records nothing, so a try again is safe)
        cur = conn.execute("INSERT INTO pending_actions (tool, arguments, summary, client, created_at, origin, held_by) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", (tool, json.dumps(arguments, sort_keys=True), summary_text,
                                                            _text(client), now(), origin, held_by))
        return pending(conn, cur.lastrowid)[0]


def _row(r: sqlite3.Row) -> dict[str, Any]:
    internal = r["client"] if r["origin"] in ("ask", "ask_hermes") else None
    return {"id": r["id"], "tool": r["tool"], "arguments": json.loads(r["arguments"]), "summary": r["summary"],
            "client": "Ask" if internal and internal.startswith(ASK_OWNER_PREFIX) else r["client"], "origin": r["origin"],
            "held": r["held_by"] is not None, "status": r["status"],
            "created_at": r["created_at"], "decided_at": r["decided_at"],
            **({"result": json.loads(r["result"])} if r["result"] else {})}


def ask_owner(conn: sqlite3.Connection, action_id: int) -> str | None:
    """An Ask row's opaque owner binding, kept out of every serialized action/card response."""
    row = conn.execute("SELECT client, origin FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
    internal = row["client"] if row and row["origin"] in ("ask", "ask_hermes") else None
    return internal[len(ASK_OWNER_PREFIX):] if internal and internal.startswith(ASK_OWNER_PREFIX) else None


def pending(conn: sqlite3.Connection, action_id: int | None = None, status: str | None = "pending") -> list[dict[str, Any]]:
    """Pending actions (all of them with status=None; one with action_id, held or not), oldest first. A list never
    has a held one (its Ask turn is still running)."""
    where, args = [], []
    if action_id is not None:
        where.append("id = ?")
        args.append(action_id)
    else:
        where.append("held_by IS NULL")
        if status is not None:
            where.append("status = ?")
            args.append(status)
    rows = conn.execute(f"SELECT * FROM pending_actions WHERE {' AND '.join(where)} ORDER BY id", args).fetchall()
    return [_row(r) for r in rows]


def pending_for_owner(conn: sqlite3.Connection, owner: str | None) -> list[dict[str, Any]]:
    """Released pending rows visible in Settings: every genuine MCP row, plus only this originating client's Ask
    rows. `None` deliberately retains the existing shared local/legacy behavior; authenticated owners do not inherit
    those unbound rows. The opaque binding remains internal to the database."""
    rows = pending(conn)
    return [row for row in rows if row["origin"] == "mcp" or ask_owner(conn, row["id"]) == owner]


def summary_for_owner(conn: sqlite3.Connection, owner: str | None) -> dict[str, Any]:
    """The legacy Settings summary, with a count consistent with this request's visible rows."""
    out = summary(conn)
    out["pending"] = len(pending_for_owner(conn, owner))
    return out


def release(conn: sqlite3.Connection, held_by: str, build: Callable[[dict[str, Any]], Any] = lambda row: row,
            check: Callable[[int], Any] = lambda released: None) -> list[Any]:
    """The turn `held_by` answered: its rows become listed and decidable. Returns `build` of each (as they are now: one
    that reconcile() withdrew meanwhile stays withdrawn), made before the release is committed. Then `check(n)`, n the
    rows still held that were released, runs with the write lock held (no reconcile() can come between it and the
    commit): if `build` or `check` raises, nothing is released (the rows stay held, so the turn's withdraw() still
    finds them)."""
    with conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM pending_actions WHERE held_by = ? ORDER BY id", (held_by,))]
        released = conn.execute("UPDATE pending_actions SET held_by = NULL WHERE held_by = ?", (held_by,)).rowcount
        out = [build(_row(r)) for r in conn.execute(f"SELECT * FROM pending_actions WHERE id IN "
                                                    f"({','.join('?' * len(ids))}) ORDER BY id", ids)] if ids else []
        check(released)
        return out


def withdraw(conn: sqlite3.Connection, held_by: str, reason: str = WITHDRAWN) -> int:
    """The turn `held_by` failed: its rows are withdrawn (status rejected, with why), never decided by anyone."""
    with conn:
        return conn.execute("UPDATE pending_actions SET status = 'rejected', decided_at = ?, result = ?, held_by = NULL "
                            "WHERE held_by = ? AND status = 'pending'",
                            (now(), json.dumps({"withdrawn": reason}), held_by)).rowcount


def _ago(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def reconcile(conn: sqlite3.Connection) -> dict[str, int]:
    """What a process that stopped left behind: a held row older than HELD_STALE (no turn runs that long) is withdrawn;
    an "approved" one claimed more than APPLY_STALE ago was never finished, so it's failed with `uncertain` (the change
    may be partly made: it's never reopened as pending, never applied again). If its applier is alive after all, its
    real outcome replaces this when it finishes (decide()). Writes only when there's one to settle: a plain look (the
    Settings page, the Ask panel's poll) never waits on another writer's lock (review N11)."""
    held, applying = _ago(HELD_STALE), _ago(APPLY_STALE)
    if not conn.execute("SELECT 1 FROM pending_actions WHERE (held_by IS NOT NULL AND status = 'pending' AND created_at < ?) "
                        "OR (status = 'approved' AND decided_at < ?) LIMIT 1", (held, applying)).fetchone():
        return {"withdrawn": 0, "interrupted": 0}
    with conn:
        withdrawn = conn.execute("UPDATE pending_actions SET status = 'rejected', decided_at = ?, result = ?, held_by = NULL "
                                 "WHERE held_by IS NOT NULL AND status = 'pending' AND created_at < ?",
                                 (now(), json.dumps({"withdrawn": ABANDONED}), held)).rowcount
        interrupted = conn.execute("UPDATE pending_actions SET status = 'failed', result = ? WHERE status = 'approved' "
                                   "AND decided_at < ?", (json.dumps({"error": "the app stopped", "uncertain": True},
                                                                     sort_keys=True), applying)).rowcount
    return {"withdrawn": withdrawn, "interrupted": interrupted}


class DecideError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.field = "id"


def decide(conn: sqlite3.Connection, action_id: Any, approve: Any, apply_fn: Callable[[str, dict[str, Any]], Any],
           errors: tuple[type[Exception], ...] = ()) -> dict[str, Any]:
    """TASK-21: the owner's answer to a pending action (Settings -> Connect other agents). Declined: status rejected,
    nothing changes. Approved: claimed (status approved, so it's never applied twice), then `apply_fn(tool, arguments)`
    makes the change exactly as the Hermes plugin would after its approval card (tooldefs.apply: the same checks again,
    then the same functions as the dashboard): applied, with what it did in `result`, or failed with the reason (one of
    `errors`: nothing was changed). Anything else raised while applying is failed too, `uncertain` (the change may be
    partly made), so the record never stays "approved". Only a pending row no running Ask turn holds can be claimed."""
    if isinstance(action_id, bool) or not isinstance(action_id, int):
        raise DecideError("id must be a pending action's number")
    if not isinstance(approve, bool):
        raise DecideError("approve must be true or false")
    reconcile(conn)
    with conn:
        claimed = conn.execute("UPDATE pending_actions SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending' "
                               "AND held_by IS NULL", ("approved" if approve else "rejected", now(), action_id)).rowcount
    if not claimed:
        found = pending(conn, action_id)
        if found and found[0]["held"] and found[0]["status"] == "pending":
            raise DecideError(f"pending action #{action_id} can't be decided yet: the Ask answer that suggested it is "
                              "still running", 409)
        raise DecideError(f"pending action #{action_id} was already {found[0]['status']}" if found
                          else f"no pending action #{action_id}", 409 if found else 404)
    row = pending(conn, action_id)[0]
    if approve:
        try:
            result, status = apply_fn(row["tool"], row["arguments"]), "applied"
        except errors as e:
            result, status = {"error": str(e)}, "failed"
        except Exception as e:   # not one of the expected refusals: what was done is unknown (logged, not shown)
            log.exception("pending action #%s: applying it failed", action_id)
            result, status = {"error": type(e).__name__, "uncertain": True}, "failed"
        with conn:
            conn.execute("UPDATE pending_actions SET status = ?, result = ? WHERE id = ?",
                         (status, json.dumps(result, default=str, sort_keys=True), action_id))
        row = pending(conn, action_id)[0]
    return row
