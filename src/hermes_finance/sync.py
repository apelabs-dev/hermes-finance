"""`finance sync`: pull transactions (cursor-based), balances, liabilities and holdings per Item."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import classify, logos
from .db import now
from .plaid import PlaidClient, PlaidError

log = logging.getLogger(__name__)

MUTATION_ERROR = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
MAX_PAGINATION_RESTARTS = 3
# Optional-product errors that just mean "not available for this Item" -> skip quietly.
TOLERATED_PRODUCT_ERRORS = {
    "PRODUCTS_NOT_SUPPORTED", "PRODUCT_NOT_ENABLED", "PRODUCT_NOT_READY", "INVALID_PRODUCT",
    "ADDITIONAL_CONSENT_REQUIRED", "NO_LIABILITY_ACCOUNTS", "NO_INVESTMENT_ACCOUNTS",
    "NO_INVESTMENT_AUTH_ACCOUNTS", "NO_ACCOUNTS", "INSTITUTION_NOT_SUPPORTED",
}
# TASK-22c: passing trouble (no network, Plaid's own error or maintenance, a rate limit, the bank down), not a problem
# with the Item: the failed sync is logged, but the Item's saved status and error stay as they were
TRANSIENT_ERRORS = {"NETWORK_ERROR", "INTERNAL_SERVER_ERROR", "PLANNED_MAINTENANCE", "INSTITUTION_DOWN",
                    "INSTITUTION_NOT_RESPONDING", "RATE_LIMIT_EXCEEDED", "RATE_LIMIT"}


def is_transient(error_code: str | None, error_type: str | None = None) -> bool:
    """A NETWORK_ERROR and the like; every RATE_LIMIT_EXCEEDED type (ACCOUNTS_LIMIT, TRANSACTIONS_LIMIT, ...); an HTTP
    429 or 5xx that came without Plaid's error body."""
    code = error_code or ""
    return (code in TRANSIENT_ERRORS or error_type == "RATE_LIMIT_EXCEEDED" or code.startswith("RATE_LIMIT")
            or code == "HTTP_429" or code.startswith("HTTP_5"))


@dataclass
class ItemSyncResult:
    item_id: str
    institution_name: str | None
    added: int = 0
    modified: int = 0
    removed: int = 0
    ok: bool = True
    error_code: str | None = None
    error_message: str | None = None
    error_type: str | None = None
    update_status: str | None = None
    extras: dict[str, str] = field(default_factory=dict)  # balances/liabilities/investments -> outcome

    def summary(self) -> str:
        name = self.institution_name or self.item_id
        if not self.ok:
            hint = " (open `finance serve` and click Reconnect)" if self.error_code == "ITEM_LOGIN_REQUIRED" else ""
            return f"{name}: ERROR {self.error_code}{hint}"
        parts = [f"{name}: +{self.added} ~{self.modified} -{self.removed}"]
        parts += [f"{k} {v}" for k, v in self.extras.items()]
        if self.update_status and self.update_status != "HISTORICAL_UPDATE_COMPLETE":
            parts.append(f"plaid status {self.update_status}")
        return " | ".join(parts)


# --- DB writes ---------------------------------------------------------------------------------

def upsert_accounts(conn: sqlite3.Connection, item_id: str, accounts: list[dict[str, Any]]) -> None:
    ts = now()
    for a in accounts:
        bal = a.get("balances") or {}
        conn.execute(
            """INSERT INTO accounts (account_id, item_id, name, official_name, mask, type, subtype,
                   current_balance, available_balance, credit_limit, iso_currency, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET
                   item_id=excluded.item_id, name=excluded.name, official_name=excluded.official_name,
                   mask=excluded.mask, type=excluded.type, subtype=excluded.subtype,
                   current_balance=excluded.current_balance, available_balance=excluded.available_balance,
                   credit_limit=excluded.credit_limit, iso_currency=excluded.iso_currency,
                   updated_at=excluded.updated_at""",
            (a["account_id"], item_id, a.get("name"), a.get("official_name"), a.get("mask"),
             a.get("type"), a.get("subtype"), bal.get("current"), bal.get("available"),
             bal.get("limit"), bal.get("iso_currency_code") or bal.get("unofficial_currency_code"), ts),
        )


def snapshot_balances(conn: sqlite3.Connection, item_id: str, day: str | None = None) -> int:
    """Upsert today's (local date) balance row for each of the Item's accounts. Returns rows written."""
    day = day or date.today().isoformat()
    with conn:
        cur = conn.execute(
            """INSERT INTO balance_snapshots (account_id, date, current, available, iso_currency)
               SELECT account_id, ?, current_balance, available_balance, iso_currency
               FROM accounts WHERE item_id = ?
               ON CONFLICT(account_id, date) DO UPDATE SET
                   current=excluded.current, available=excluded.available,
                   iso_currency=excluded.iso_currency""",
            (day, item_id))
    return cur.rowcount


def snapshot_holdings(conn: sqlite3.Connection, item_id: str, day: str | None = None) -> int:
    """Upsert today's (local date) row per account/security from the Item's current holdings, the same way as
    snapshot_balances. Only called after a successful holdings call, so a stale position is never re-dated."""
    day = day or date.today().isoformat()
    with conn:
        # a second sync the same day replaces the day's rows, so a position sold in between doesn't linger
        conn.execute("DELETE FROM holding_snapshots WHERE date = ? AND account_id IN "
                     "(SELECT account_id FROM accounts WHERE item_id = ?)", (day, item_id))
        cur = conn.execute(
            """INSERT INTO holding_snapshots (date, account_id, security_id, ticker, quantity, price, value)
               SELECT ?, account_id, security_id, ticker_symbol, quantity, institution_price, institution_value
               FROM holdings WHERE item_id = ?
               ON CONFLICT(account_id, security_id, date) DO UPDATE SET
                   ticker=excluded.ticker, quantity=excluded.quantity, price=excluded.price, value=excluded.value""",
            (day, item_id))
    return cur.rowcount


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def upsert_transaction(conn: sqlite3.Connection, item_id: str, t: dict[str, Any], ts: str) -> None:
    pfc = t.get("personal_finance_category") or {}
    pending = bool(t.get("pending"))
    # A pending row that a posted row of the same Item and account already names (it posted before this copy
    # arrived, or Plaid sent an old copy again) is stored retired, so it never counts next to the posted one.
    superseded = pending and conn.execute(
        """SELECT 1 FROM transactions WHERE pending_transaction_id = ? AND transaction_id <> ? AND pending = 0
               AND item_id = ? AND account_id = ? AND source = 'plaid' LIMIT 1""",
        (t["transaction_id"], t["transaction_id"], item_id, t["account_id"])).fetchone() is not None
    conn.execute(
        """INSERT INTO transactions (transaction_id, account_id, item_id, date, authorized_date, name,
               merchant_name, amount, iso_currency, pending, pending_transaction_id, payment_channel,
               pfc_primary, pfc_detailed, pfc_confidence, logo_url, website, location_json,
               counterparties_json, raw_json, removed, first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(transaction_id) DO UPDATE SET
               account_id=excluded.account_id, item_id=excluded.item_id, date=excluded.date,
               authorized_date=excluded.authorized_date, name=excluded.name,
               merchant_name=excluded.merchant_name, amount=excluded.amount,
               iso_currency=excluded.iso_currency, pending=excluded.pending,
               pending_transaction_id=excluded.pending_transaction_id,
               payment_channel=excluded.payment_channel, pfc_primary=excluded.pfc_primary,
               pfc_detailed=excluded.pfc_detailed, pfc_confidence=excluded.pfc_confidence,
               logo_url=excluded.logo_url, website=excluded.website,
               location_json=excluded.location_json, counterparties_json=excluded.counterparties_json,
               raw_json=excluded.raw_json, removed=excluded.removed, updated_at=excluded.updated_at""",
        (t["transaction_id"], t["account_id"], item_id, t["date"], t.get("authorized_date"),
         t.get("name"), t.get("merchant_name"), t["amount"],
         t.get("iso_currency_code") or t.get("unofficial_currency_code"), int(pending),
         t.get("pending_transaction_id"), t.get("payment_channel"), pfc.get("primary"),
         pfc.get("detailed"), pfc.get("confidence_level"), t.get("logo_url"), t.get("website"),
         _dumps(t.get("location")), _dumps(t.get("counterparties")), _dumps(t), int(superseded), ts, ts),
    )
    # A posted row retires the pending one it names whether or not Plaid's removal of it ever comes: same Item and
    # account only, never by amount or name. Soft, like a removal: the row and its classification are kept.
    if not pending and t.get("pending_transaction_id") and t["pending_transaction_id"] != t["transaction_id"]:
        conn.execute(
            """UPDATE transactions SET removed=1, updated_at=?
               WHERE transaction_id=? AND removed=0 AND pending=1 AND item_id=? AND account_id=?
                   AND source='plaid'""",
            (ts, t["pending_transaction_id"], item_id, t["account_id"]))
    # A pending transaction the user/AI already categorized: carry that decision to the posted one.
    if t.get("pending_transaction_id"):
        conn.execute(
            """INSERT INTO classifications (transaction_id, category_id, source, confidence, note, updated_at)
               SELECT ?, category_id, source, confidence, note, ? FROM classifications
               WHERE transaction_id = ? AND source IN ('user', 'ai')
               ON CONFLICT(transaction_id) DO NOTHING""",
            (t["transaction_id"], ts, t["pending_transaction_id"]),
        )


def _fetch_all_pages(client: PlaidClient, access_token: str, cursor: str | None):
    """Accumulate every page from `cursor`, restarting from it on mutation-during-pagination."""
    for attempt in range(MAX_PAGINATION_RESTARTS + 1):
        added, modified, removed, accounts = [], [], [], {}
        next_cursor, status = cursor, None
        try:
            while True:
                page = client.transactions_sync(access_token, next_cursor)
                added += page.get("added", [])
                modified += page.get("modified", [])
                removed += page.get("removed", [])
                for a in page.get("accounts", []):
                    accounts[a["account_id"]] = a
                next_cursor = page.get("next_cursor", next_cursor)
                status = page.get("transactions_update_status", status)
                if not page.get("has_more"):
                    return added, modified, removed, list(accounts.values()), next_cursor, status
        except PlaidError as e:
            if e.error_code != MUTATION_ERROR or attempt == MAX_PAGINATION_RESTARTS:
                raise
            log.info("data changed during pagination; restarting from saved cursor")
    raise AssertionError("unreachable")


def _set_item_error(conn: sqlite3.Connection, item_id: str, e: PlaidError) -> None:
    if is_transient(e.error_code, e.error_type):   # TASK-22c: one network blip doesn't mark every bank as broken
        return
    status = "login_required" if e.error_code == "ITEM_LOGIN_REQUIRED" else "error"
    with conn:
        conn.execute("UPDATE items SET status=?, error_code=?, error_message=? WHERE item_id=?",
                     (status, e.error_code, e.error_message, item_id))


def _products(item: sqlite3.Row) -> set[str]:
    try:
        return set(json.loads(item["products"] or "[]"))
    except ValueError:
        return set()


def _refresh_item_meta(conn, client, item) -> sqlite3.Row:
    """/item/get: learn which optional products were granted (free call)."""
    data = client.item_get(item["access_token"])
    it = data.get("item") or {}
    products = sorted(it.get("products") or [])
    with conn:
        conn.execute("UPDATE items SET products=?, institution_id=COALESCE(institution_id, ?) WHERE item_id=?",
                     (json.dumps(products), it.get("institution_id"), item["item_id"]))
    return conn.execute("SELECT * FROM items WHERE item_id=?", (item["item_id"],)).fetchone()


def _sync_liabilities(conn, client, item) -> None:
    data = client.liabilities_get(item["access_token"])
    upsert_accounts(conn, item["item_id"], data.get("accounts", []))
    ts, liab = now(), data.get("liabilities") or {}
    with conn:
        for kind in ("credit", "mortgage", "student"):
            for l in liab.get(kind) or []:
                if kind == "credit":
                    aprs = l.get("aprs") or []
                    rate = next((a.get("apr_percentage") for a in aprs if a.get("apr_type") == "purchase_apr"),
                                aprs[0].get("apr_percentage") if aprs else None)
                    min_pay = l.get("minimum_payment_amount")
                else:
                    rate = (l.get("interest_rate") or {}).get("percentage") if kind == "mortgage" \
                        else l.get("interest_rate_percentage")
                    min_pay = l.get("minimum_payment_amount") if kind == "student" else l.get("next_monthly_payment")
                conn.execute(
                    """INSERT OR REPLACE INTO liabilities (account_id, kind, item_id, last_statement_balance,
                           minimum_payment_amount, next_payment_due_date, last_payment_amount,
                           last_payment_date, is_overdue, interest_rate, raw_json, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (l.get("account_id"), kind, item["item_id"],
                     l.get("last_statement_balance"),
                     min_pay, l.get("next_payment_due_date"), l.get("last_payment_amount"),
                     l.get("last_payment_date"),
                     None if l.get("is_overdue") is None else int(bool(l.get("is_overdue"))),
                     rate, _dumps(l), ts),
                )


def _sync_holdings(conn, client, item) -> None:
    data = client.investments_holdings_get(item["access_token"])
    upsert_accounts(conn, item["item_id"], data.get("accounts", []))
    securities = {s["security_id"]: s for s in data.get("securities", [])}
    ts = now()
    with conn:
        # Holdings are a snapshot: positions that disappeared were sold.
        conn.execute("DELETE FROM holdings WHERE item_id=?", (item["item_id"],))
        for h in data.get("holdings", []):
            sec = securities.get(h.get("security_id"), {})
            conn.execute(
                """INSERT OR REPLACE INTO holdings (account_id, security_id, item_id, ticker_symbol,
                       security_name, security_type, quantity, institution_price, institution_value,
                       cost_basis, iso_currency, raw_json, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (h.get("account_id"), h.get("security_id"), item["item_id"], sec.get("ticker_symbol"),
                 sec.get("name"), sec.get("type"), h.get("quantity"), h.get("institution_price"),
                 h.get("institution_value"), h.get("cost_basis"),
                 h.get("iso_currency_code") or h.get("unofficial_currency_code"),
                 _dumps({"holding": h, "security": sec}), ts),
            )


def _optional(label: str, fn, result: ItemSyncResult) -> None:
    try:
        fn()
        result.extras[label] = "ok"
    except PlaidError as e:
        tolerated = e.error_code in TOLERATED_PRODUCT_ERRORS
        result.extras[label] = f"skipped ({e.error_code})" if tolerated else f"err ({e.error_code})"
        if not tolerated:
            log.warning("%s failed for item %s: %s", label, result.item_id, e.error_code)
    except Exception as e:  # a parsing bug in an optional product must not sink the whole sync
        result.extras[label] = f"err ({type(e).__name__})"
        log.exception("%s failed for item %s", label, result.item_id)


def sync_item(conn: sqlite3.Connection, client: PlaidClient, item: sqlite3.Row) -> ItemSyncResult:
    item_id = item["item_id"]
    result = ItemSyncResult(item_id, item["institution_name"])
    started = now()
    log_id = conn.execute("INSERT INTO sync_log (item_id, started_at) VALUES (?, ?)",
                          (item_id, started)).lastrowid
    conn.commit()

    try:
        try:
            item = _refresh_item_meta(conn, client, item)
        except PlaidError as e:
            if e.is_item_error:
                raise
            log.warning("item/get failed for %s: %s", item_id, e.error_code)

        added, modified, removed, accounts, cursor, status = _fetch_all_pages(
            client, item["access_token"], item["cursor"])
        ts = now()
        with conn:  # one DB transaction: data + cursor land together or not at all
            upsert_accounts(conn, item_id, accounts)
            for t in added + modified:
                upsert_transaction(conn, item_id, t, ts)
            for r in removed:
                conn.execute("UPDATE transactions SET removed=1, updated_at=? WHERE transaction_id=?",
                             (ts, r["transaction_id"]))
            conn.execute(
                """UPDATE items SET cursor=?, txn_update_status=?, last_sync_at=?, status='active',
                       error_code=NULL, error_message=NULL WHERE item_id=?""",
                (cursor, status, ts, item_id))
        result.added, result.modified, result.removed = len(added), len(modified), len(removed)
        result.update_status = status
    except PlaidError as e:
        if e.error_code == "PRODUCT_NOT_READY":  # Plaid is still pulling history: not an Item problem
            result.update_status = "NOT_READY"
        else:
            result.ok, result.error_code, result.error_message = False, e.error_code, e.error_message
            result.error_type = e.error_type
            _set_item_error(conn, item_id, e)
            log.warning("transactions sync failed for %s: %s", item_id, e.error_code)
    except Exception as e:  # unexpected: keep going with the other Items, cursor untouched
        result.ok, result.error_code, result.error_message = False, "INTERNAL_ERROR", type(e).__name__
        log.exception("transactions sync crashed for %s", item_id)

    if result.ok:
        def balances():
            upsert_accounts(conn, item_id, client.accounts_get(item["access_token"]).get("accounts", []))
            conn.commit()
        _optional("balances", balances, result)
        products = _products(item)
        if "liabilities" in products:
            _optional("liabilities", lambda: _sync_liabilities(conn, client, item), result)
        if "investments" in products:
            _optional("investments", lambda: _sync_holdings(conn, client, item), result)
        try:  # net worth history; a failure here must not fail the sync
            snapshot_balances(conn, item_id)
        except sqlite3.Error:
            log.exception("balance snapshot failed for item %s", item_id)
        if result.extras.get("investments") == "ok":
            try:  # portfolio history, same rules
                snapshot_holdings(conn, item_id)
            except sqlite3.Error:
                log.exception("holding snapshot failed for item %s", item_id)

    with conn:
        conn.execute(
            """UPDATE sync_log SET finished_at=?, added=?, modified=?, removed=?, error=?, details=?
               WHERE id=?""",
            (now(), result.added, result.modified, result.removed,
             None if result.ok else f"{result.error_code}: {result.error_message}",
             json.dumps(result.extras), log_id))
    return result


def sync_all(conn: sqlite3.Connection, client: PlaidClient, item_id: str | None = None) -> list[ItemSyncResult]:
    q = "SELECT * FROM items WHERE removed_at IS NULL"
    args: tuple = ()
    if item_id:
        q, args = q + " AND item_id = ?", (item_id,)
    return sync_items(conn, client, conn.execute(q + " ORDER BY created_at", args).fetchall())


def sync_items(conn: sqlite3.Connection, client: PlaidClient, items: list[sqlite3.Row]) -> list[ItemSyncResult]:
    """Sync these Items (rows of `items`), then what follows a sync (TASK-22: syncnow.run picks them)."""
    results = [sync_item(conn, client, item) for item in items]
    if any(r.ok for r in results):
        classify.apply(conn)
    if any(r.extras.get("investments") == "ok" for r in results):
        try:  # holding logos not cached yet (one fetch per ticker, ever); a failure never fails the sync
            logos.refresh_security_logos(conn)
        except Exception:
            log.exception("holding logo refresh failed")
    return results
