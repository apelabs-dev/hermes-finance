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
# The Item's stored products are missing/empty/unparseable: fail closed, never assume transactions.
ITEM_PRODUCTS_UNKNOWN = "ITEM_PRODUCTS_UNKNOWN"
# The ledger's internal marker for an Item that simply does not grant transactions: never a Plaid error code.
TRANSACTIONS_NOT_GRANTED_MARKER = "not in the Item's products"
# /transactions/sync rejections about the product or its consent: the Item's OTHER data is unaffected, so balances,
# holdings and liabilities still refresh while the Item reports the transactions failure (decided 2026-11-05).
# Any other fatal transactions error (ITEM_LOGIN_REQUIRED, a broken token) leaves the whole Item unfresh: no snapshot.
TRANSACTIONS_PRODUCT_REJECTIONS = {
    "PRODUCT_NOT_ENABLED", "PRODUCTS_NOT_SUPPORTED", "INVALID_PRODUCT", "ADDITIONAL_CONSENT_REQUIRED", "ACCESS_NOT_GRANTED",
}
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
class Phase:
    """One data phase of an Item's sync: what was attempted, what it returned, whether it delivered account rows."""
    attempted: bool = False
    ok: bool = False
    skipped: str | None = None          # a tolerated product error: skipped, not a failure
    error_code: str | None = None
    error_message: str | None = None
    error_type: str | None = None
    accounts: bool = False              # delivered fresh account rows this run
    rows: int = 0                       # holdings / liability rows written
    fatal: bool = False                 # a non-tolerated error here makes the Item's sync fail
    detail: str | None = None           # what a consumer shows for a crash (the exception type)

    @property
    def outcome(self) -> str | None:
        if not self.attempted and self.skipped is None:
            return None
        if self.skipped is not None:
            return f"skipped ({self.skipped})"
        if self.ok:
            return "ok"
        return f"err ({self.detail or self.error_code})"


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
    phases: dict[str, Phase] = field(default_factory=dict)   # the per-Item data-phase ledger
    blocked_code: str | None = None     # no ledger could run (unknown products, /item/get failed)
    blocked_message: str | None = None
    blocked_type: str | None = None

    def block(self, code: str, message: str | None = None, error_type: str | None = None) -> None:
        self.blocked_code, self.blocked_message, self.blocked_type = code, message, error_type

    @property
    def transactions_skipped(self) -> str | None:
        phase = self.phases.get("transactions")
        return phase.skipped if phase else None

    @property
    def transactions_ok(self) -> bool:
        phase = self.phases.get("transactions")
        return bool(phase and phase.ok)

    @property
    def extras(self) -> dict[str, str]:
        return {name: phase.outcome for name, phase in self.phases.items()
                if name != "transactions" and phase.outcome is not None}

    @property
    def data_ok(self) -> bool:
        return any(phase.ok for phase in self.phases.values())

    @property
    def landed(self) -> bool:
        return self.data_ok

    @property
    def accounts_delivered(self) -> bool:
        return any(phase.ok and phase.accounts for phase in self.phases.values())

    @property
    def fresh(self) -> bool:
        return self.transactions_ok or (self.ok and self.data_ok and self.transactions_skipped is not None)

    def phase_ok(self, name: str) -> bool:
        phase = self.phases.get(name)
        return bool(phase and phase.ok)

    def derive(self) -> None:
        """The one place the ledger becomes the Item's verdict and what its consumers report."""
        if self.blocked_code is not None:
            self.ok = False
            self.error_code, self.error_message, self.error_type = (
                self.blocked_code, self.blocked_message, self.blocked_type)
            return
        failures = [p for p in self.phases.values() if p.attempted and not p.ok and p.skipped is None]
        fatal = next((p for p in failures if p.fatal), None)
        if fatal is not None or not self.landed:
            failure = fatal or (failures[0] if failures else None)
            self.ok = False
            if failure is not None:
                self.error_code = failure.error_code or ("INTERNAL_ERROR" if failure.detail else None)
                self.error_message = failure.error_message or failure.detail or failure.error_code
                self.error_type = failure.error_type
            else:
                skipped = next((p for p in self.phases.values()
                                if p.attempted and p.skipped is not None
                                and p.skipped != TRANSACTIONS_NOT_GRANTED_MARKER), None)
                if skipped is not None:
                    self.error_code = skipped.skipped
                    self.error_message = f"no data was delivered ({skipped.skipped})"
                    self.error_type = skipped.error_type
                else:
                    self.error_code = "NO_DATA"
                    self.error_message = "no data was delivered"
            return
        self.ok = True
        self.error_code = self.error_message = self.error_type = None

    def summary(self) -> str:
        name = self.institution_name or self.item_id
        if not self.ok:
            hint = " (open `finance serve` and click Reconnect)" if self.error_code == "ITEM_LOGIN_REQUIRED" else ""
            return f"{name}: ERROR {self.error_code}{hint}"
        if self.transactions_skipped:
            parts = [f"{name}: no transactions ({self.transactions_skipped})"]
        else:
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


def _valid_products(value: Any) -> list[str] | None:
    """The product metadata, only when it is a list of nonempty strings; anything else is not trustworthy."""
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(p, str) and p for p in value):
        return None
    return value


def _granted_products(item: sqlite3.Row) -> set[str] | None:
    """The Item's granted products, or None when the stored value is missing/empty/malformed (genuinely unknown)."""
    raw = item["products"]
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    products = _valid_products(parsed)
    return set(products) if products is not None else None


def _refresh_item_meta(conn, client, item) -> sqlite3.Row:
    """/item/get: learn which optional products were granted (free call). Only a well-formed list is saved."""
    data = client.item_get(item["access_token"])
    it = data.get("item") or {}
    products = _valid_products(it.get("products"))
    if products is None:  # malformed/missing: keep the stored value for _granted_products to validate
        return item
    with conn:
        conn.execute("UPDATE items SET products=?, institution_id=COALESCE(institution_id, ?) WHERE item_id=?",
                     (json.dumps(sorted(products)), it.get("institution_id"), item["item_id"]))
    return conn.execute("SELECT * FROM items WHERE item_id=?", (item["item_id"],)).fetchone()


def _sync_liabilities(conn, client, item) -> tuple[bool, int]:
    data = client.liabilities_get(item["access_token"])
    accounts = data.get("accounts", [])
    upsert_accounts(conn, item["item_id"], accounts)
    ts, liab, rows = now(), data.get("liabilities") or {}, 0
    with conn:
        for kind in ("credit", "mortgage", "student"):
            for l in liab.get(kind) or []:
                rows += 1
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
    return bool(accounts), rows


def _sync_holdings(conn, client, item) -> tuple[bool, int]:
    data = client.investments_holdings_get(item["access_token"])
    accounts = data.get("accounts", [])
    upsert_accounts(conn, item["item_id"], accounts)
    securities = {s["security_id"]: s for s in data.get("securities", [])}
    rows = data.get("holdings", [])
    ts = now()
    with conn:
        # Holdings are a snapshot: positions that disappeared were sold.
        conn.execute("DELETE FROM holdings WHERE item_id=?", (item["item_id"],))
        for h in rows:
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
    return bool(accounts), len(rows)


def _first_fatal_failure(result: ItemSyncResult) -> Phase | None:
    """derive()'s own rule: the first fatal phase that failed (in phase order)."""
    return next((p for p in result.phases.values()
                 if p.attempted and not p.ok and p.skipped is None and p.fatal), None)


def _run_data_phase(result: ItemSyncResult, conn: sqlite3.Connection, label: str, fn,
                    fatal: bool = False) -> None:
    """Run one optional data phase and write its real outcome into the Item's ledger."""
    phase = Phase(attempted=True, fatal=fatal)
    result.phases[label] = phase
    try:
        accounts, rows = fn()
    except PlaidError as e:
        tolerated = e.error_code in TOLERATED_PRODUCT_ERRORS
        if tolerated:
            phase.skipped = e.error_code
        else:
            phase.error_code, phase.error_message, phase.error_type = e.error_code, e.error_message, e.error_type
            if fatal:  # a granted product we asked for failed: say so, but keep everything that did land
                # Only the first fatal phase writes: the row must agree with derive(), sync_log.error and
                # _item_json(), so a later phase's failure must not overwrite the verdict already reported.
                if _first_fatal_failure(result) is phase:
                    _set_item_error(conn, result.item_id, e)
            log.warning("%s failed for item %s: %s", label, result.item_id, e.error_code)
    except Exception as e:  # a parsing bug in an optional product must not sink the whole sync
        phase.error_code, phase.error_message, phase.detail = "INTERNAL_ERROR", type(e).__name__, type(e).__name__
        log.exception("%s failed for item %s", label, result.item_id)
    else:
        phase.ok, phase.accounts, phase.rows = True, bool(accounts), rows


def sync_item(conn: sqlite3.Connection, client: PlaidClient, item: sqlite3.Row) -> ItemSyncResult:
    item_id = item["item_id"]
    result = ItemSyncResult(item_id, item["institution_name"])
    started = now()
    log_id = conn.execute("INSERT INTO sync_log (item_id, started_at) VALUES (?, ?)",
                          (item_id, started)).lastrowid
    conn.commit()
    try:
        _sync_item_body(conn, client, item, result)
    except Exception as e:  # a bug here must never leave the Item without a finished sync_log row
        result.block("INTERNAL_ERROR", type(e).__name__)
        result.derive()
        log.exception("item sync crashed for %s", item_id)
    finally:
        details = dict(result.extras)
        if result.transactions_skipped is not None:
            details["transactions"] = f"skipped ({result.transactions_skipped})"
        with conn:
            conn.execute(
                """UPDATE sync_log SET finished_at=?, added=?, modified=?, removed=?, error=?, details=?
                   WHERE id=?""",
                (now(), result.added, result.modified, result.removed,
                 None if result.ok else f"{result.error_code}: {result.error_message}",
                 json.dumps(details), log_id))
    return result


def _sync_transactions(conn: sqlite3.Connection, client: PlaidClient, item: sqlite3.Row,
                       result: ItemSyncResult) -> None:
    item_id = result.item_id
    phase = Phase(attempted=True, fatal=True)
    result.phases["transactions"] = phase
    try:
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
                """UPDATE items SET cursor=?, txn_update_status=?, status='active',
                       error_code=NULL, error_message=NULL WHERE item_id=?""",
                (cursor, status, item_id))
        result.added, result.modified, result.removed = len(added), len(modified), len(removed)
        result.update_status = status
        phase.ok, phase.accounts = True, bool(accounts)
    except PlaidError as e:
        if e.error_code == "PRODUCT_NOT_READY":  # Plaid is still pulling history: not an Item problem
            result.update_status = "NOT_READY"
            phase.error_code, phase.error_message, phase.error_type = e.error_code, e.error_message, e.error_type
            phase.fatal = False
        else:
            phase.error_code, phase.error_message, phase.error_type = e.error_code, e.error_message, e.error_type
            _set_item_error(conn, item_id, e)
            log.warning("transactions sync failed for %s: %s", item_id, e.error_code)
    except Exception as e:  # unexpected: keep going with the other Items, cursor untouched
        phase.error_code, phase.error_message, phase.detail = "INTERNAL_ERROR", type(e).__name__, type(e).__name__
        log.exception("transactions sync crashed for %s", item_id)


def _sync_data_phases(conn: sqlite3.Connection, client: PlaidClient, item: sqlite3.Row,
                      result: ItemSyncResult, products: set[str]) -> None:
    item_id = result.item_id

    def balances():
        accounts = client.accounts_get(item["access_token"]).get("accounts", [])
        upsert_accounts(conn, item_id, accounts)
        conn.commit()
        return bool(accounts), 0

    # balances is the Item's only data source only when nothing else was granted.
    balance_is_only = not ({"transactions", "investments", "liabilities"} & products)
    _run_data_phase(result, conn, "balances", balances, fatal=balance_is_only)
    if "liabilities" in products:
        _run_data_phase(result, conn, "liabilities", lambda: _sync_liabilities(conn, client, item), fatal=True)
    if "investments" in products:
        _run_data_phase(result, conn, "investments", lambda: _sync_holdings(conn, client, item), fatal=True)


def _sync_item_body(conn: sqlite3.Connection, client: PlaidClient, item: sqlite3.Row,
                    result: ItemSyncResult) -> None:
    item_id = result.item_id
    products: set[str] | None = None
    try:
        try:
            item = _refresh_item_meta(conn, client, item)
        except PlaidError as e:
            if e.is_item_error:
                raise
            log.warning("item/get failed for %s: %s", item_id, e.error_code)
        products = _granted_products(item)
    except PlaidError as e:
        if e.error_code == "PRODUCT_NOT_READY":  # Plaid is still pulling history: not an Item problem
            result.update_status = "NOT_READY"
        else:
            result.block(e.error_code, e.error_message, e.error_type)
            _set_item_error(conn, item_id, e)
        log.warning("item/get failed for %s: %s", item_id, e.error_code)
    except Exception as e:  # unexpected: keep going with the other Items, cursor untouched
        result.block("INTERNAL_ERROR", type(e).__name__)
        log.exception("item sync crashed for %s", item_id)

    if products is None:
        # /item/get itself failed with an Item error (PRODUCT_NOT_READY and the like): no product state, fail closed.
        if result.blocked_code is None:
            result.block(ITEM_PRODUCTS_UNKNOWN, "the Item's granted products are unknown")
            log.warning("products unknown for %s; no product was synced", item_id)
    else:
        if "transactions" not in products:
            result.phases["transactions"] = Phase(skipped=TRANSACTIONS_NOT_GRANTED_MARKER)
        else:
            _sync_transactions(conn, client, item, result)
        txn = result.phases.get("transactions")
        blocked = txn is not None and txn.attempted and not txn.ok and txn.skipped is None and txn.fatal
        # A product/consent rejection says nothing about the Item's other data: refresh it. Any other fatal
        # transactions error means the Item itself is not usable this run, so no phase runs and no snapshot is written.
        if not blocked or (txn is not None and txn.error_code in TRANSACTIONS_PRODUCT_REJECTIONS):
            _sync_data_phases(conn, client, item, result, products)

    result.derive()

    if result.accounts_delivered:  # never re-date a snapshot from balances that did not arrive this run
        try:  # net worth history; a failure here must not fail the sync
            snapshot_balances(conn, item_id)
        except sqlite3.Error:
            log.exception("balance snapshot failed for item %s", item_id)
    if result.phase_ok("investments"):
        try:  # portfolio history, same rules
            snapshot_holdings(conn, item_id)
        except sqlite3.Error:
            log.exception("holding snapshot failed for item %s", item_id)
    if result.fresh:
        ts = now()
        with conn:
            if result.ok:  # a clean sync: mark the Item fresh and clear any old error
                conn.execute(
                    """UPDATE items SET last_sync_at=?, status='active', error_code=NULL, error_message=NULL
                       WHERE item_id=?""",
                    (ts, item_id))
            else:  # transactions landed but a later phase failed: keep that failure, still record the sync
                conn.execute("UPDATE items SET last_sync_at=? WHERE item_id=?", (ts, item_id))


def sync_all(conn: sqlite3.Connection, client: PlaidClient, item_id: str | None = None) -> list[ItemSyncResult]:
    q = "SELECT * FROM items WHERE removed_at IS NULL"
    args: tuple = ()
    if item_id:
        q, args = q + " AND item_id = ?", (item_id,)
    return sync_items(conn, client, conn.execute(q + " ORDER BY created_at", args).fetchall())


def sync_items(conn: sqlite3.Connection, client: PlaidClient, items: list[sqlite3.Row]) -> list[ItemSyncResult]:
    """Sync these Items (rows of `items`), then what follows a sync (TASK-22: syncnow.run picks them)."""
    results = [sync_item(conn, client, item) for item in items]
    if any(r.transactions_ok for r in results):  # a partial Item still committed transactions: classify them
        classify.apply(conn)
    if any(r.phase_ok("investments") for r in results):
        try:  # holding logos not cached yet (one fetch per ticker, ever); a failure never fails the sync
            logos.refresh_security_logos(conn)
        except Exception:
            log.exception("holding logo refresh failed")
    return results
