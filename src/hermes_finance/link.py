"""Plaid Link flows: new-Item link token, exchange (with duplicate guard), update-mode reconnect.

Slot safety (Plaid Trial = 10 Items for the life of the account):
  * new link tokens are refused once 10 Items have been exchanged (removed ones still count);
  * exchange is refused if the institution already has a connected Item ("use Reconnect");
  * Reconnect uses update mode (link token with access_token) and never calls exchange;
  * demo mode (first-run "Try demo", TASK-18) only ever talks to Plaid Sandbox: with a client for any other
    environment, no link token is made and nothing is exchanged, so no Production Item can appear.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading

from . import classify, config, logos, prefs
from .db import now, slots_used
from .plaid import PlaidClient, PlaidError
from .sync import sync_item

log = logging.getLogger(__name__)

_exchange_lock = threading.Lock()  # serialize exchanges: a double-click must not race the guard


class LinkRefused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _record_attempt(conn, outcome, inst_id, inst_name, item_id=None, error=None) -> None:
    with conn:
        conn.execute(
            """INSERT INTO link_attempts (created_at, institution_id, institution_name, outcome, item_id, error)
               VALUES (?,?,?,?,?,?)""", (now(), inst_id, inst_name, outcome, item_id, error))


def backup_token(record: dict) -> None:
    """Append-only, fsync'd, 0600. Written before anything else touches the new token."""
    path = config.token_backup_path()
    config.ensure_data_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def connected_item_for(conn: sqlite3.Connection, institution_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT item_id, institution_name FROM items WHERE institution_id=? AND removed_at IS NULL",
        (institution_id,)).fetchone()


def demo_guard(conn: sqlite3.Connection, client: PlaidClient) -> None:
    """Demo mode is Sandbox only: refuse before any Plaid call when the client would reach another environment."""
    if prefs.get(conn, "plaid.demo") and (client.settings.env != "sandbox"
                                          or not client.base_url.startswith("https://sandbox.plaid.com")):
        raise LinkRefused("Demo mode only uses Plaid Sandbox, and the saved keys are for Production. To connect real "
                          "banks, choose \u201cConnect real banks\u201d in Settings \u2192 Connections.", 409)


def new_link_token(conn: sqlite3.Connection, client: PlaidClient) -> dict:
    demo_guard(conn, client)
    used = slots_used(conn)
    if used >= config.SLOT_LIMIT:
        raise LinkRefused(f"All {config.SLOT_LIMIT} Plaid Trial slots are used ({used}/{config.SLOT_LIMIT}); "
                          "a new bank cannot be connected. Use Reconnect for existing banks.")
    resp = client.link_token_create()
    return {"link_token": resp["link_token"], "expiration": resp.get("expiration")}


def update_link_token(conn: sqlite3.Connection, client: PlaidClient, item_id: str) -> dict:
    item = conn.execute("SELECT access_token FROM items WHERE item_id=? AND removed_at IS NULL",
                        (item_id,)).fetchone()
    if not item:
        raise LinkRefused(f"unknown item {item_id!r}", 404)
    demo_guard(conn, client)
    resp = client.link_token_create_update(item["access_token"])
    return {"link_token": resp["link_token"], "expiration": resp.get("expiration")}


def exchange(conn: sqlite3.Connection, client: PlaidClient, public_token: str, metadata: dict | None) -> dict:
    """Duplicate guard -> exchange -> backup token -> store Item -> initial sync."""
    inst = (metadata or {}).get("institution") or {}
    inst_id, inst_name = inst.get("institution_id"), inst.get("name")
    demo_guard(conn, client)
    with _exchange_lock:
        if not public_token:
            raise LinkRefused("missing public_token", 400)
        if not inst_id:
            _record_attempt(conn, "rejected", None, inst_name, error="no institution_id in Link metadata")
            raise LinkRefused("Link did not report which institution this is, so the duplicate check "
                              "cannot run; refusing to exchange.", 400)
        existing = connected_item_for(conn, inst_id)
        if existing:
            _record_attempt(conn, "duplicate_blocked", inst_id, inst_name, existing["item_id"])
            raise LinkRefused(f"{existing['institution_name'] or inst_name} is already connected — "
                              "use Reconnect instead.")

        try:
            resp = client.item_public_token_exchange(public_token)
        except PlaidError as e:
            _record_attempt(conn, "error", inst_id, inst_name, error=e.error_code)
            raise
        access_token, item_id = resp["access_token"], resp["item_id"]
        created = now()
        backup_error = None
        try:
            backup_token({"item_id": item_id, "access_token": access_token, "institution_id": inst_id,
                          "institution_name": inst_name, "created_at": created})
        except OSError as e:  # still store it in the DB below; losing the token would waste a slot
            backup_error = e
            log.error("token backup write failed: %s", e)
        with conn:
            conn.execute(
                """INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at)
                   VALUES (?,?,?,?, 'active', ?)""", (item_id, inst_id, inst_name, access_token, created))
        _record_attempt(conn, "exchanged", inst_id, inst_name, item_id)
        log.info("exchanged new item %s (%s)", item_id, inst_name)

    item = conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
    result = sync_item(conn, client, item)
    classify.apply(conn)
    try:  # the bank's logo (free lookup, then its own site icon once): never fails the connection
        logos.refresh_institution(conn, client, inst_id)
    except Exception as e:
        log.warning("logo lookup failed for %s: %s", inst_id, e)
    out = {"item_id": item_id, "institution_name": inst_name, "sync": result.summary(), "sync_ok": result.ok}
    if backup_error:
        out["warning"] = "token backup file could not be written; token is stored in the DB only"
    return out


def reconnected(conn: sqlite3.Connection, client: PlaidClient, item_id: str) -> dict:
    """After update-mode Link succeeds: the access_token is unchanged, so just clear + re-sync."""
    item = conn.execute("SELECT * FROM items WHERE item_id=? AND removed_at IS NULL", (item_id,)).fetchone()
    if not item:
        raise LinkRefused(f"unknown item {item_id!r}", 404)
    with conn:
        conn.execute("UPDATE items SET status='active', error_code=NULL, error_message=NULL WHERE item_id=?",
                     (item_id,))
    result = sync_item(conn, client, item)
    classify.apply(conn)
    return {"item_id": item_id, "sync": result.summary(), "sync_ok": result.ok}
