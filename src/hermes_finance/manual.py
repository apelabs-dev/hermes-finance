"""Manual accounts (what Plaid can't see) and the bank payments matched to manual loans and credit cards.

A manual account's `balance` is always >= 0; liabilities (credit_card, loan, other_liability) are
subtracted by kind. `balance_as_of` is the day the entered balance was true.

Loans: a bank transaction can be applied to one loan, once (`loan_payments`). Only transactions dated
after the loan's `balance_as_of` move the balance, because the entered balance already includes earlier
ones. The balance, each payment's interest/principal split and the history rows after `balance_as_of` are
rebuilt from the entered balance (the history row on `balance_as_of`) and those payments in date order, so
applying and unapplying in any order ends at the same exact balance. A payment whose transaction the bank
later removed stops counting at the next rebuild (every `loan-match` run rebuilds the loans it looks at).

Split per payment: with an APR, interest = round(balance_before x APR / 100 / 12, 2) (never more than the
payment) and principal = amount - interest; without one, the whole payment is principal. The balance never
goes below 0 (at payoff the principal is what was left).

Credit cards (TASK-11) take a "Match payments from" pattern too, and their payments live in the same table
(`loan_payments.loan_id` is the manual account). A card payment is all principal. Only payments are seen, never
purchases, so a matched card's balance only goes down: it's shown as approximate ("≈ $X, last set $Y on DATE, minus
$Z matched payments") and nudges for a statement balance after NUDGE_DAYS. A payment label can be shared with cards
Plaid does see ("CITI CARD ONLINE" pays the connected Citi cards too), so an outflow that pairs with a same-amount
inflow on a connected credit card within ±PAIR_DAYS days is that card's payment and never applied (card_pair()); a
card payment is applied only once it's PAIR_DAYS old, when that inflow would have posted, and an automatic match that
pairs later is taken back.

HTTP- and CLI-agnostic: both call these functions and turn ManualError into a 400 / exit 2. Nothing is
written when validation fails.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import date
from typing import Any

from . import insights
from .db import now

KINDS = ("bank", "credit_card", "investment", "loan", "vehicle", "property", "other_asset", "other_liability")
LIABILITY_KINDS = frozenset({"credit_card", "loan", "other_liability"})
MATCH_KINDS = ("loan", "credit_card")  # their balance follows the bank payments matched to them
VALUE_KINDS = ("vehicle", "property")  # their value can follow an estimate (valuation.py; value_source)
# What Carfax prices a car in: carfax.com/value's own list (its JS bundle, 2026-09-26: {fair, good, excellent}).
VEHICLE_CONDITIONS = ("fair", "good", "excellent")
DEFAULT_CONDITION = "good"
SUBTYPES = {
    "bank": ("checking", "savings", "money_market", "cd"),
    "investment": ("brokerage", "401k", "roth_ira", "traditional_ira", "hsa", "crypto", "other"),
    "loan": ("auto", "student", "personal", "mortgage", "bnpl", "other"),
}
SUBTYPE_LABELS = {
    "checking": "Checking", "savings": "Savings", "money_market": "Money market", "cd": "CD",
    "brokerage": "Brokerage", "401k": "401(k)", "roth_ira": "Roth IRA", "traditional_ira": "Traditional IRA",
    "hsa": "HSA", "crypto": "Crypto", "other": "Other",
    "auto": "Auto loan", "student": "Student loan", "personal": "Personal loan", "mortgage": "Mortgage",
    "bnpl": "Buy now, pay later",
}
# The fields each kind's form has (brief section 2), and which of them are required.
ALLOWED = {
    "bank": {"name", "institution", "subtype", "balance", "balance_as_of"},
    "credit_card": {"name", "institution", "credit_limit", "balance", "interest_rate", "match_pattern", "balance_as_of"},
    "investment": {"name", "institution", "subtype", "balance", "balance_as_of"},
    "loan": {"name", "institution", "subtype", "interest_rate", "original_amount", "balance", "monthly_payment",
             "match_pattern", "balance_as_of"},
    "vehicle": {"name", "vin", "vehicle_make", "vehicle_model", "vehicle_year", "vehicle_trim", "mileage", "zip_code",
                "vehicle_condition", "linked_loan_id", "balance", "balance_as_of"},
    "property": {"name", "address", "linked_loan_id", "balance", "balance_as_of"},
    "other_asset": {"name", "subtype", "balance", "balance_as_of"},
    "other_liability": {"name", "subtype", "balance", "balance_as_of"},
}
REQUIRED = {
    "bank": {"name", "institution", "subtype", "balance"},
    "credit_card": {"name", "institution", "credit_limit", "balance"},
    "investment": {"name", "institution", "subtype", "balance"},
    "loan": {"name", "institution", "subtype", "original_amount", "balance", "monthly_payment"},
    "vehicle": {"vehicle_make", "vehicle_model", "vehicle_year", "balance"},
    "property": {"name", "balance"},
    "other_asset": {"name", "subtype", "balance"},
    "other_liability": {"name", "subtype", "balance"},
}
# Form labels, so errors read like the form ("Provider is required").
LABELS = {  # in form order, which is also the order missing fields are reported in
    "name": "Nickname", "institution": "Institution", "subtype": "Type", "address": "Address", "vin": "VIN",
    "vehicle_make": "Make", "vehicle_model": "Model", "vehicle_year": "Year", "vehicle_trim": "Trim",
    "mileage": "Mileage", "zip_code": "ZIP code", "vehicle_condition": "Condition", "interest_rate": "Interest rate",
    "original_amount": "Original amount", "credit_limit": "Credit limit", "balance": "Current balance",
    "monthly_payment": "Monthly payment", "match_pattern": "Match payments from", "balance_as_of": "Balance date",
    "linked_loan_id": "Loan",
}
KIND_LABELS = {
    "institution": {"bank": "Bank name", "credit_card": "Card company", "investment": "Institution", "loan": "Provider"},
    "balance": {"loan": "Amount remaining", "vehicle": "Your value", "property": "Your value",
                "other_asset": "Value", "other_liability": "Amount left"},
    "subtype": {"bank": "Account type", "investment": "Account type", "loan": "Loan type"},
    "name": {"other_asset": "Name", "other_liability": "Name"},
}
TEXT_MAX = {"name": 80, "institution": 80, "subtype": 40, "match_pattern": 60, "vehicle_make": 40, "vehicle_model": 40,
            "vehicle_trim": 40, "address": 160}
MAX_MILES = 2_000_000
ADDRESS_MIN = 8              # "1 Main St" is about the shortest address a lookup can use
ZIP_RE = re.compile(r"\d{5}(-\d{4})?")
MATCH_MIN = 3                # a shorter pattern would match far too much
MAX_AMOUNT = 100_000_000     # dollars; covers a house
MAX_RATE = 100.0             # APR percent
STALE_DAYS = 45
NUDGE_DAYS = 35              # a matched card: "update from statement" after this many days (purchases aren't seen)
PAIR_DAYS = 5                # an outflow and a same-amount inflow on a connected card this many days apart are a pair
PREVIEW_DAYS = 90            # the form's "This would have matched N payments ($T) in the last 90 days"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
COLUMNS = ("kind", "name", "institution", "subtype", "balance", "credit_limit", "interest_rate", "original_amount",
           "monthly_payment", "match_pattern", "vehicle_make", "vehicle_model", "vehicle_year", "balance_as_of",
           "vin", "vehicle_trim", "mileage", "mileage_as_of", "zip_code", "address", "vin_decoded", "value_source",
           "vehicle_condition", "linked_loan_id")
MAX_MONTHS = 1200            # payoff projections stop here ("never" at this payment)


class ManualError(Exception):
    def __init__(self, message: str, field: str | None = None, status: int = 400):
        super().__init__(message)
        self.field = field
        self.status = status


def _r(x: float) -> float:
    return round(x + 0.0, 2)


def label(field: str, kind: str) -> str:
    return KIND_LABELS.get(field, {}).get(kind) or LABELS.get(field, field)


# --- validation ---------------------------------------------------------------------------------------

def _number(field: str, kind: str, value: Any, lo: float, hi: float, lo_open: bool = False, places: int = 2) -> float:
    name = label(field, kind)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManualError(f"{name} must be a number", field)
    try:
        f = float(value)  # a huge JSON integer doesn't fit a float
    except OverflowError:
        raise ManualError(f"{name} must be at most {hi:,.0f}", field) from None
    if not math.isfinite(f):
        raise ManualError(f"{name} must be a finite number", field)
    if f < lo or (lo_open and f <= lo) or f > hi:
        low = f"more than {lo:,.0f}" if lo_open else f"at least {lo:,.0f}"
        raise ManualError(f"{name} must be {low} and at most {hi:,.0f}", field)
    return round(f + 0.0, places)


def _text(field: str, kind: str, value: Any) -> str:
    name = label(field, kind)
    if not isinstance(value, str):
        raise ManualError(f"{name} must be text", field)
    value = " ".join(value.split())  # trimmed, inner whitespace collapsed
    if not value:
        raise ManualError(f"{name} can't be empty", field)
    if len(value) > TEXT_MAX[field]:
        raise ManualError(f"{name} must be at most {TEXT_MAX[field]} characters", field)
    if any(ord(c) < 32 for c in value):
        raise ManualError(f"{name} has a control character", field)
    return value


def parse_day(value: Any, field: str = "balance_as_of", kind: str = "", today: date | None = None) -> str:
    name = label(field, kind)
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ManualError(f"{name} must be a date as YYYY-MM-DD", field)
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ManualError(f"{name} must be a real date (YYYY-MM-DD)", field) from None
    if d.year < 1900 or (today and d > today):
        raise ManualError(f"{name} can't be in the future" if today and d > today else f"{name} is too far back", field)
    return value


def _clean(kind: str, field: str, value: Any, today: date) -> Any:
    """One field's cleaned value. None clears an optional field."""
    if value is None:
        if field in REQUIRED[kind] or field == "balance_as_of":
            raise ManualError(f"{label(field, kind)} is required", field)
        return None
    if field in ("balance",):
        return _number(field, kind, value, 0, MAX_AMOUNT)
    if field in ("credit_limit", "original_amount", "monthly_payment"):
        return _number(field, kind, value, 0, MAX_AMOUNT, lo_open=True)
    if field == "interest_rate":
        return _number(field, kind, value, 0, MAX_RATE, places=4)  # 6.125% stays 6.125%
    if field == "vehicle_year":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise ManualError("Year must be a whole number", field)
        if not 1900 <= value <= today.year + 1:
            raise ManualError(f"Year must be between 1900 and {today.year + 1}", field)
        return value
    if field == "balance_as_of":
        return parse_day(value, field, kind, today)
    if field == "mileage":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ManualError("Mileage must be a whole number of miles", field)
        if not 0 <= value <= MAX_MILES:
            raise ManualError(f"Mileage must be between 0 and {MAX_MILES:,}", field)
        return value
    if field == "vin":
        from .valuation import vin_problem  # valuation imports this module
        if not isinstance(value, str):
            raise ManualError("VIN must be text", field)
        vin = "".join(value.split()).upper()
        problem = vin_problem(vin)
        if problem:
            raise ManualError(problem, field)
        return vin
    if field == "linked_loan_id":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ManualError("Loan must be one of your loans (or none)", field)
        return value
    if field == "vehicle_condition":
        if not isinstance(value, str) or value.strip().lower() not in VEHICLE_CONDITIONS:
            raise ManualError(f"Condition must be one of {', '.join(VEHICLE_CONDITIONS)}", field)
        return value.strip().lower()
    if field == "zip_code":
        if not isinstance(value, str) or not ZIP_RE.fullmatch(value.strip()):
            raise ManualError("ZIP code must be 5 digits (or ZIP+4, like 94110-1234)", field)
        return value.strip()
    text = _text(field, kind, value)
    if field == "address" and len(text) < ADDRESS_MIN:
        raise ManualError("Address needs the street, city and state (or ZIP) to be looked up", field)
    if field == "subtype" and kind in SUBTYPES:
        key = text.lower().replace(" ", "_").replace("(", "").replace(")", "")  # "Roth IRA" -> roth_ira, "401(k)" -> 401k
        if key not in SUBTYPES[kind]:
            raise ManualError(f"{label(field, kind)} must be one of {', '.join(SUBTYPES[kind])}", field)
        return key
    if field == "match_pattern" and len(text) < MATCH_MIN:
        raise ManualError(f"Match payments from needs at least {MATCH_MIN} characters", field)
    return text


def validate(kind: Any, data: dict[str, Any], today: date, partial: bool = False,
             estimate: bool = False) -> dict[str, Any]:
    """Cleaned values for the fields in `data`. Unknown fields, fields another kind's form has, and (unless
    `partial`) missing required fields are errors. `estimate`: a vehicle / property whose value will come from its
    estimate (valuation.py), so no value is needed."""
    if not isinstance(kind, str) or kind not in KINDS:
        raise ManualError(f"kind must be one of {', '.join(KINDS)}", "kind")
    if not isinstance(data, dict):
        raise ManualError("expected an object of fields")
    out = {}
    for field, value in data.items():
        if field == "kind":
            continue
        if field not in LABELS:
            raise ManualError(f"unknown field {field!r}", field)
        if field not in ALLOWED[kind]:
            raise ManualError(f"{label(field, kind)} doesn't apply to {kind.replace('_', ' ')} accounts", field)
        if estimate and field == "balance" and value is None and kind in VALUE_KINDS:
            out[field] = None
            continue
        out[field] = _clean(kind, field, value, today)
    if not partial:
        for field in sorted(REQUIRED[kind] - ({"balance"} if estimate and kind in VALUE_KINDS else set()),
                            key=list(LABELS).index):
            if out.get(field) is None:
                raise ManualError(f"{label(field, kind)} is required", field)
    return out


def vehicle_name(year: Any, make: Any, model: Any) -> str:
    return " ".join(str(x) for x in (year, make, model) if x not in (None, ""))[:TEXT_MAX["name"]]


# --- reads ----------------------------------------------------------------------------------------------

def get(conn: sqlite3.Connection, account_id: Any, include_archived: bool = False) -> sqlite3.Row:
    if isinstance(account_id, bool) or not isinstance(account_id, int):
        raise ManualError("id must be a whole number", "id")
    row = conn.execute("SELECT * FROM manual_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None or (row["archived_at"] and not include_archived):
        raise ManualError(f"no manual account #{account_id}" + (" (archived)" if row else ""), "id", 404)
    return row


def rows(conn: sqlite3.Connection, include_archived: bool = False) -> list[sqlite3.Row]:
    where = "" if include_archived else "WHERE archived_at IS NULL"
    return conn.execute(f"SELECT * FROM manual_accounts {where} ORDER BY kind, name COLLATE NOCASE, id").fetchall()


def signed(row: sqlite3.Row | dict) -> float:
    return _r(-row["balance"] if row["kind"] in LIABILITY_KINDS else row["balance"])


def history(conn: sqlite3.Connection, account_id: int) -> list[dict[str, Any]]:
    return [{"date": r["date"], "balance": r["balance"]} for r in conn.execute(
        "SELECT date, balance FROM manual_balance_history WHERE manual_account_id = ? ORDER BY date", (account_id,))]


def payments(conn: sqlite3.Connection, loan_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """SELECT p.transaction_id, p.amount, p.interest, p.principal, p.applied_at, p.source, t.date, t.name,
                  t.merchant_name, a.name AS account_name, a.mask AS account_mask
           FROM loan_payments p JOIN transactions t USING (transaction_id)
           LEFT JOIN accounts a ON a.account_id = t.account_id
           WHERE p.loan_id = ? AND t.removed = 0 ORDER BY t.date DESC, p.transaction_id DESC""", (loan_id,))]


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def matching_transactions(conn: sqlite3.Connection, pattern: str, after: str | None = None,
                          limit: int = 500, since: str | None = None) -> list[dict[str, Any]]:
    """Posted outflows whose name or merchant_name contains `pattern` (case-insensitive), newest first, dated after
    `after` / on or after `since`. `applied_to` is the manual account a row is already applied to (or None)."""
    args: list[Any] = [_like(pattern), _like(pattern)]
    extra = ""
    if after:
        extra = "AND t.date > ?"
        args.append(after)
    if since:
        extra += " AND t.date >= ?"
        args.append(since)
    return [dict(r) for r in conn.execute(
        f"""SELECT t.transaction_id, t.date, t.name, t.merchant_name, t.amount, a.name AS account_name,
                   a.mask AS account_mask, a.type AS account_type, i.institution_name, p.loan_id AS applied_to
            FROM transactions t LEFT JOIN accounts a ON a.account_id = t.account_id
            LEFT JOIN items i ON i.item_id = t.item_id
            LEFT JOIN loan_payments p ON p.transaction_id = t.transaction_id
            WHERE t.removed = 0 AND t.pending = 0 AND t.amount > 0
              AND (t.name LIKE ? ESCAPE '\\' OR t.merchant_name LIKE ? ESCAPE '\\') {extra}
            ORDER BY t.date DESC, t.transaction_id DESC LIMIT ?""", [*args, limit])]


def _card_inflows(conn: sqlite3.Connection, cents: int, day: str) -> list[sqlite3.Row]:
    """Inflows of exactly `cents` on connected credit cards (Plaid, not removed) within ±PAIR_DAYS days of `day`,
    nearest first."""
    return conn.execute(
        """SELECT u.transaction_id, u.date, a.name, a.mask FROM transactions u
           JOIN accounts a ON a.account_id = u.account_id JOIN items i ON i.item_id = u.item_id
           WHERE u.removed = 0 AND i.removed_at IS NULL AND a.type = 'credit'
             AND CAST(ROUND(-u.amount * 100) AS INTEGER) = ? AND ABS(julianday(u.date) - julianday(?)) <= ?
           ORDER BY ABS(julianday(u.date) - julianday(?)), u.date, u.transaction_id""",
        (cents, day, PAIR_DAYS, day)).fetchall()


def card_pair(conn: sqlite3.Connection, txn_id: str) -> dict[str, Any] | None:
    """The connected-card payment a bank outflow is, or None: a same-amount inflow on a connected credit card within
    ±PAIR_DAYS days. One to one: the outflows of that amount around it (on non-card accounts) take those inflows in
    date order, each the nearest one still free, so two $250 payments and one $250 card credit make one pair."""
    t = conn.execute("SELECT transaction_id, date, amount FROM transactions WHERE transaction_id = ?", (txn_id,)).fetchone()
    if t is None or t["amount"] <= 0:
        return None
    cents = round(t["amount"] * 100)
    rivals = conn.execute(
        """SELECT t.transaction_id, t.date FROM transactions t JOIN accounts a ON a.account_id = t.account_id
           WHERE t.removed = 0 AND a.type <> 'credit' AND CAST(ROUND(t.amount * 100) AS INTEGER) = ?
             AND ABS(julianday(t.date) - julianday(?)) <= ? ORDER BY t.date, t.transaction_id""",
        (cents, t["date"], 2 * PAIR_DAYS)).fetchall()
    taken: set[str] = set()
    for r in rivals:
        u = next((u for u in _card_inflows(conn, cents, r["date"]) if u["transaction_id"] not in taken), None)
        if r["transaction_id"] == txn_id:
            return {"transaction_id": u["transaction_id"], "date": u["date"], "card": u["name"], "mask": u["mask"]} if u else None
        if u:
            taken.add(u["transaction_id"])
    return None


def pair_reason(pair: dict[str, Any]) -> str:
    mask = f" ••{pair['mask']}" if pair["mask"] else ""
    return f"pairs with a payment on {pair['card'] or 'a connected card'}{mask} on {pair['date']} (that card's payment)"


def match_preview(conn: sqlite3.Connection, pattern: str, today: date, account_id: int | None = None,
                  days: int = PREVIEW_DAYS) -> dict[str, Any]:
    """The form's dry run: what `pattern` would have matched in the last `days` days, with the same guards as
    match_payments() (card payments that pair with a connected card are left out and counted in `paired`; the ones
    too new to tell are `waiting`; `elsewhere`: already applied to another account). Writes nothing."""
    since = date.fromordinal(today.toordinal() - days).isoformat()
    out = {"days": days, "since": since, "count": 0, "total": 0.0, "paired": 0, "paired_total": 0.0, "waiting": 0,
           "elsewhere": 0}
    for t in matching_transactions(conn, pattern, since=since):
        if t["applied_to"] is not None and t["applied_to"] != account_id:
            out["elsewhere"] += 1
        elif card_pair(conn, t["transaction_id"]):
            out["paired"] += 1
            out["paired_total"] += t["amount"]
        elif (today - date.fromisoformat(t["date"])).days < PAIR_DAYS:
            out["waiting"] += 1
        else:
            out["count"] += 1
            out["total"] += t["amount"]
    out["total"], out["paired_total"] = _r(out["total"]), _r(out["paired_total"])
    return out


# A bank outflow that looks like a card payment: Plaid's credit card payment category, or "card" / "autopay" in its
# name (not a purchase at the card's store, a Zelle payment or a loan payment).
CARD_PAYMENT_SQL = ("(COALESCE(t.pfc_detailed, '') LIKE 'LOAN_PAYMENTS_CREDIT_CARD%' OR t.name LIKE '%card%' "
                    "OR t.name LIKE '%autopay%' OR t.name LIKE '%auto pay%' OR t.name LIKE '%crd pmt%')")
# Card issuers and the other names their payments go by on a bank statement.
ISSUER_ALIASES = {"citi": ("cbna", "citibank", "citicard"), "best buy": ("bby",), "american express": ("amex",),
                  "capital one": ("cap one", "capone"), "synchrony": ("syf", "synchrony bank"), "chase": ("jpmorgan",),
                  "barclays": ("barclaycard",), "wells fargo": ("wf credit card",), "apple": ("gs bank", "goldman")}
SUGGEST_DAYS = 120


def unpaired_card_payments(conn: sqlite3.Connection, today: date, since: str) -> list[dict[str, Any]]:
    """Posted bank outflows since `since` that look like a card payment, pay no connected card (card_pair()), aren't
    applied, match no active account's pattern and are PAIR_DAYS old (so the connected side would have posted),
    newest first: the lines a manual card's payment label can be read from."""
    patterns = [r[0].casefold() for r in conn.execute(
        "SELECT match_pattern FROM manual_accounts WHERE archived_at IS NULL AND match_pattern IS NOT NULL")]
    settled = date.fromordinal(today.toordinal() - PAIR_DAYS).isoformat()
    out = []
    for t in conn.execute(
            f"""SELECT t.transaction_id, t.date, t.name, t.merchant_name, t.amount, a.name AS account_name, a.mask AS account_mask
                FROM transactions t JOIN accounts a ON a.account_id = t.account_id
                LEFT JOIN loan_payments p ON p.transaction_id = t.transaction_id
                WHERE t.removed = 0 AND t.pending = 0 AND t.amount > 0 AND a.type <> 'credit' AND p.transaction_id IS NULL
                  AND t.date >= ? AND t.date <= ? AND {CARD_PAYMENT_SQL}
                ORDER BY t.date DESC, t.transaction_id""", (since, settled)):
        text = f"{t['name'] or ''} {t['merchant_name'] or ''}".casefold()
        if any(pat in text for pat in patterns) or card_pair(conn, t["transaction_id"]):
            continue
        out.append(dict(t))
    return out


def card_hints(row: sqlite3.Row) -> list[str]:
    """What a manual card's payments may be called: its name, its institution, and the issuers' other names."""
    hints = {" ".join(str(row[k] or "").casefold().split()) for k in ("name", "institution")} - {""}
    for key, aliases in ISSUER_ALIASES.items():
        if any(key in h for h in hints):
            hints |= {key, *aliases}
    return sorted(hints)


def suggest_patterns(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    """`finance match-payments --suggest` (for the daily run to post; nothing is saved): for each active manual
    credit card without a pattern, the unpaired card-payment outflows after its balance date that mention it (its
    name, its institution or the issuer's other names: "CITI", "CBNA", "BEST BUY"), each with the label to use."""
    out = []
    since = date.fromordinal(today.toordinal() - SUGGEST_DAYS).isoformat()
    lines = unpaired_card_payments(conn, today, since)
    for card in conn.execute("SELECT * FROM manual_accounts WHERE kind = 'credit_card' AND archived_at IS NULL "
                             "AND match_pattern IS NULL ORDER BY id").fetchall():
        hints = [re.compile(rf"(?<![a-z0-9]){re.escape(h)}(?![a-z0-9])") for h in card_hints(card)]
        for t in lines:
            text = f"{t['name'] or ''} {t['merchant_name'] or ''}".casefold()
            if t["date"] <= card["balance_as_of"] or not any(h.search(text) for h in hints):
                continue
            label = " ".join((t["name"] or t["merchant_name"] or "").split())[:TEXT_MAX["match_pattern"]]
            shared = conn.execute("SELECT transaction_id FROM transactions WHERE name = ? AND amount > 0 AND removed = 0 "
                                  "AND transaction_id <> ?", (t["name"], t["transaction_id"])).fetchall()
            out.append({"card_id": card["id"], "card": card["name"], "institution": card["institution"],
                        "transaction_id": t["transaction_id"], "date": t["date"], "name": t["name"],
                        "merchant_name": t["merchant_name"], "amount": t["amount"], "account_name": t["account_name"],
                        "account_mask": t["account_mask"], "pattern": label,
                        "shared_with_connected_cards": any(card_pair(conn, r[0]) for r in shared)})
    return {"today": today.isoformat(), "suggestions": out}


# --- payoff math ------------------------------------------------------------------------------------------

def payoff(balance: float, apr: float | None, monthly: float | None, first_payment: date) -> dict[str, Any]:
    """Payments left and the payoff month, starting with a payment in `first_payment`'s month.

    APR set: amortized month by month (interest rounded to cents each month). No APR: remaining / payment,
    rounded up, in cents. `method` is paid | amortized | simple | none (no payment) | never (the payment
    doesn't cover the interest)."""
    cents = round(balance * 100)
    if cents <= 0:
        return {"payments_left": 0, "payoff_month": None, "method": "paid"}
    if not monthly or monthly <= 0:
        return {"payments_left": None, "payoff_month": None, "method": "none"}
    pay = round(monthly * 100)
    if apr:
        rate = apr / 1200
        if pay <= round(cents * rate):
            return {"payments_left": None, "payoff_month": None, "method": "never"}
        n, left = 0, cents
        while left > 0 and n < MAX_MONTHS:
            left = left + round(left * rate) - pay
            n += 1
        if left > 0:
            return {"payments_left": None, "payoff_month": None, "method": "never"}
        method = "amortized"
    else:
        n = -(-cents // pay)
        method = "simple"
    start = insights.month_of(first_payment)
    return {"payments_left": n, "payoff_month": insights.add_months(start, n - 1), "method": method}


def add_one_month(d: date) -> date:
    return insights._shift_months(d, 1)


def next_payment(last_payment: str | None, today: date) -> date | None:
    """The next expected payment: a month after the last matched one, rolled forward to today or later."""
    if not last_payment:
        return None
    d = add_one_month(date.fromisoformat(last_payment))
    base = date.fromisoformat(last_payment)
    k = 1
    while d < today and k < 600:
        k += 1
        d = insights._shift_months(base, k)
    return d


# --- writes -------------------------------------------------------------------------------------------------

def _history_row(conn: sqlite3.Connection, account_id: int, day: str, balance: float) -> None:
    conn.execute("""INSERT INTO manual_balance_history (manual_account_id, date, balance) VALUES (?, ?, ?)
                    ON CONFLICT(manual_account_id, date) DO UPDATE SET balance = excluded.balance""",
                 (account_id, day, balance))


def _check_loan_link(conn: sqlite3.Connection, loan_id: int | None) -> None:
    """A vehicle / property may only be linked to an active manual loan."""
    if loan_id is None:
        return
    r = conn.execute("SELECT kind, archived_at FROM manual_accounts WHERE id = ?", (loan_id,)).fetchone()
    if r is None or r["kind"] != "loan":
        raise ManualError("Loan must be one of your loans (or none)", "linked_loan_id")
    if r["archived_at"]:
        raise ManualError("That loan is archived: pick an active one (or none)", "linked_loan_id")


def create(conn: sqlite3.Connection, data: dict[str, Any], today: date, estimate: bool = False) -> dict[str, Any]:
    """Add a manual account. `estimate` (valuation.create, when the source is on and the lookup inputs are there): a vehicle /
    property without a value follows its estimate (value_source 'estimate', $0 until the first one arrives)."""
    kind = data.get("kind") if isinstance(data, dict) else None
    clean = validate(kind, data, today, estimate=estimate)
    if kind == "vehicle" and not clean.get("name"):
        clean["name"] = vehicle_name(clean["vehicle_year"], clean["vehicle_make"], clean["vehicle_model"])
    clean.setdefault("balance_as_of", today.isoformat())
    if kind in VALUE_KINDS:
        clean["value_source"] = "user" if clean.get("balance") is not None else "estimate"
        if clean.get("balance") is None:
            clean["balance"] = 0.0
    if clean.get("mileage") is not None:
        clean["mileage_as_of"] = today.isoformat()
    if kind == "vehicle" and clean.get("vehicle_condition") is None:
        clean["vehicle_condition"] = DEFAULT_CONDITION
    _check_loan_link(conn, clean.get("linked_loan_id"))
    values = {c: clean.get(c) for c in COLUMNS}
    values["kind"] = kind
    ts = now()
    with conn:
        cur = conn.execute(
            f"INSERT INTO manual_accounts ({', '.join(COLUMNS)}, created_at, updated_at) "
            f"VALUES ({', '.join('?' * len(COLUMNS))}, ?, ?)", [values[c] for c in COLUMNS] + [ts, ts])
        account_id = cur.lastrowid
        _history_row(conn, account_id, values["balance_as_of"], values["balance"])
    return describe(conn, get(conn, account_id), today)


def edit(conn: sqlite3.Connection, account_id: Any, data: dict[str, Any], today: date) -> dict[str, Any]:
    """Change the fields in `data` (null clears an optional one). A new balance (or balance date) is a new
    entered balance, true on `balance_as_of` (default today); a loan's later payments are then re-applied."""
    row = get(conn, account_id)
    kind = row["kind"]
    if not isinstance(data, dict):
        raise ManualError("expected an object of fields")
    if "kind" in data and data["kind"] != kind:
        raise ManualError("kind can't be changed; archive this one and add a new one", "kind")
    clean = validate(kind, {k: v for k, v in data.items() if k != "kind"}, today, partial=True)
    merged = {c: row[c] for c in COLUMNS} | clean
    if "linked_loan_id" in clean:
        _check_loan_link(conn, clean["linked_loan_id"])
    for field in REQUIRED[kind]:
        if merged.get(field) is None:
            raise ManualError(f"{label(field, kind)} is required", field)
    if kind in VALUE_KINDS and clean.get("balance") is not None:
        merged["value_source"] = "user"  # a value typed by the user wins over the estimate ("Your value")
    if "mileage" in clean and clean["mileage"] != row["mileage"]:
        merged["mileage_as_of"] = today.isoformat() if clean["mileage"] is not None else None
    if "vin" in clean and clean["vin"] != row["vin"]:
        merged["vin_decoded"] = None
    if kind == "vehicle" and merged.get("vehicle_condition") is None:
        merged["vehicle_condition"] = DEFAULT_CONDITION  # cleared: back to the default
    if kind == "vehicle":  # no name, or the automatic one: keep it "YEAR MAKE MODEL"
        old_auto = vehicle_name(row["vehicle_year"], row["vehicle_make"], row["vehicle_model"])
        if not merged.get("name") or ("name" not in clean and row["name"] == old_auto):
            merged["name"] = vehicle_name(merged["vehicle_year"], merged["vehicle_make"], merged["vehicle_model"])
    new_base = ("balance" in clean and round(clean["balance"] * 100) != round(row["balance"] * 100)) or \
               ("balance_as_of" in clean and clean["balance_as_of"] != row["balance_as_of"])
    if new_base:
        merged["balance_as_of"] = clean.get("balance_as_of") or today.isoformat()
        if kind in MATCH_KINDS and "balance" not in clean:
            # only the date moved: the balance entered then (before any payment) is what was true on the new date
            merged["balance"] = entered_balance(conn, row)
    elif kind in MATCH_KINDS:
        merged["balance"] = row["balance"]  # the same entered balance; payments are re-applied below
    with conn:
        conn.execute(f"UPDATE manual_accounts SET {', '.join(f'{c} = ?' for c in COLUMNS[1:])}, updated_at = ? "
                     "WHERE id = ?", [merged[c] for c in COLUMNS[1:]] + [now(), row["id"]])
        if new_base:  # the new entered balance replaces anything recorded after its date
            conn.execute("DELETE FROM manual_balance_history WHERE manual_account_id = ? AND date > ?",
                         (row["id"], merged["balance_as_of"]))
            _history_row(conn, row["id"], merged["balance_as_of"], merged["balance"])
        if kind in MATCH_KINDS:
            rebuild_loan(conn, row["id"])
    return describe(conn, get(conn, row["id"]), today)


def archive(conn: sqlite3.Connection, account_id: Any, today: date) -> dict[str, Any]:
    row = get(conn, account_id)
    with conn:
        conn.execute("UPDATE manual_accounts SET archived_at = ?, updated_at = ? WHERE id = ?", (now(), now(), row["id"]))
    return describe(conn, get(conn, row["id"], include_archived=True), today)


def entered_balance(conn: sqlite3.Connection, row: sqlite3.Row) -> float:
    """The balance as entered for `balance_as_of` (a loan's current balance has later payments taken off)."""
    r = conn.execute("SELECT balance FROM manual_balance_history WHERE manual_account_id = ? AND date = ?",
                     (row["id"], row["balance_as_of"])).fetchone()
    return r["balance"] if r else row["balance"]


def rebuild_loan(conn: sqlite3.Connection, loan_id: int) -> float:
    """Recompute the loan's (or credit card's) balance, its payments' splits and its history after balance_as_of
    from the entered balance. Runs inside the caller's transaction. Returns the new balance. A card payment is all
    principal (its APR accrues on purchases this app can't see)."""
    loan = conn.execute("SELECT * FROM manual_accounts WHERE id = ?", (loan_id,)).fetchone()
    as_of = loan["balance_as_of"]
    base_row = conn.execute("SELECT balance FROM manual_balance_history WHERE manual_account_id = ? AND date = ?",
                            (loan_id, as_of)).fetchone()
    base = base_row["balance"] if base_row else loan["balance"]
    if not base_row:
        _history_row(conn, loan_id, as_of, base)
    apr = loan["interest_rate"] if loan["kind"] == "loan" else None
    bal = base
    days: dict[str, float] = {}
    for p in conn.execute(
            """SELECT p.transaction_id, p.amount, t.date FROM loan_payments p JOIN transactions t USING (transaction_id)
               WHERE p.loan_id = ? AND t.date > ? AND t.removed = 0 ORDER BY t.date, p.transaction_id""",
            (loan_id, as_of)).fetchall():
        interest = min(_r(bal * apr / 1200), p["amount"]) if apr else 0.0
        principal = _r(min(p["amount"] - interest, bal))
        bal = _r(bal - principal)
        conn.execute("UPDATE loan_payments SET interest = ?, principal = ? WHERE transaction_id = ?",
                     (interest, principal, p["transaction_id"]))
        days[p["date"]] = bal
    conn.execute("DELETE FROM manual_balance_history WHERE manual_account_id = ? AND date > ?", (loan_id, as_of))
    for day, value in days.items():
        _history_row(conn, loan_id, day, value)
    conn.execute("UPDATE manual_accounts SET balance = ? WHERE id = ?", (bal, loan_id))
    return bal


def _payment_view(conn: sqlite3.Connection, txn_id: str) -> dict[str, Any]:
    r = conn.execute("""SELECT p.*, t.date, t.name, t.merchant_name FROM loan_payments p
                        JOIN transactions t USING (transaction_id) WHERE p.transaction_id = ?""", (txn_id,)).fetchone()
    return dict(r)


def apply_payment(conn: sqlite3.Connection, txn_id: Any, loan_id: Any, source: str = "user",
                  dry_run: bool = False) -> dict[str, Any]:
    """Apply one bank transaction to a loan or credit card. Refused (nothing written): unknown/removed/pending/inflow
    transaction, one already applied, one dated on or before balance_as_of, an archived account or one of another
    kind, or one already at $0."""
    if source not in ("auto", "ai", "user"):
        raise ManualError("source must be auto, ai or user", "source")
    if not isinstance(txn_id, str) or not txn_id:
        raise ManualError("transaction id is required", "transaction_id")
    loan = get(conn, loan_id)
    if loan["kind"] not in MATCH_KINDS:
        raise ManualError(f"#{loan['id']} {loan['name']} is a {loan['kind'].replace('_', ' ')}, not a loan or credit card",
                          "loan_id")
    t = conn.execute("SELECT transaction_id, date, name, merchant_name, amount, pending, removed FROM transactions "
                     "WHERE transaction_id = ?", (txn_id,)).fetchone()
    if t is None:
        raise ManualError(f"unknown transaction {txn_id}", "transaction_id", 404)
    if t["removed"]:
        raise ManualError(f"transaction {txn_id} was removed by the bank", "transaction_id")
    if t["pending"]:
        raise ManualError(f"transaction {txn_id} is still pending; apply it once it posts", "transaction_id")
    if t["amount"] <= 0:
        raise ManualError(f"transaction {txn_id} is money in, not a payment out", "transaction_id")
    done = conn.execute("SELECT loan_id FROM loan_payments WHERE transaction_id = ?", (txn_id,)).fetchone()
    if done:
        where = "this loan" if done["loan_id"] == loan["id"] else f"loan #{done['loan_id']}"
        raise ManualError(f"transaction {txn_id} is already applied to {where}", "transaction_id", 409)
    if t["date"] <= loan["balance_as_of"]:
        raise ManualError(f"transaction {txn_id} is dated {t['date']}, on or before the "
                          f"{'loan' if loan['kind'] == 'loan' else 'card'}'s balance date {loan['balance_as_of']}; "
                          f"the entered balance already includes it", "transaction_id")
    if round(loan["balance"] * 100) <= 0:
        raise ManualError(f"#{loan['id']} {loan['name']} is already paid off", "loan_id")
    before = loan["balance"]
    conn.execute("SAVEPOINT apply_payment")
    try:
        conn.execute("INSERT INTO loan_payments (transaction_id, loan_id, amount, interest, principal, applied_at, "
                     "source) VALUES (?, ?, ?, 0, 0, ?, ?)", (txn_id, loan["id"], _r(t["amount"]), now(), source))
        after = rebuild_loan(conn, loan["id"])
        pay = _payment_view(conn, txn_id)
    except Exception:
        conn.execute("ROLLBACK TO apply_payment")
        conn.execute("RELEASE apply_payment")
        raise
    if dry_run:
        conn.execute("ROLLBACK TO apply_payment")
    conn.execute("RELEASE apply_payment")
    conn.commit()
    return {"transaction_id": txn_id, "loan_id": loan["id"], "loan": loan["name"], "date": t["date"],
            "name": t["merchant_name"] or t["name"], "amount": pay["amount"], "interest": pay["interest"],
            "principal": pay["principal"], "balance_before": before, "balance_after": after,
            "paid_off": round(after * 100) <= 0, "source": source, "dry_run": dry_run,
            "apr_set": bool(loan["interest_rate"]) and loan["kind"] == "loan", "kind": loan["kind"]}


def unapply_payment(conn: sqlite3.Connection, txn_id: Any, dry_run: bool = False) -> dict[str, Any]:
    """Take a transaction back off its loan: the payment row goes, and the balance and history are rebuilt
    without it (exactly the balance it would have had without that payment)."""
    if not isinstance(txn_id, str) or not txn_id:
        raise ManualError("transaction id is required", "transaction_id")
    p = conn.execute("SELECT * FROM loan_payments WHERE transaction_id = ?", (txn_id,)).fetchone()
    if p is None:
        raise ManualError(f"transaction {txn_id} isn't applied to any loan", "transaction_id", 404)
    loan = conn.execute("SELECT * FROM manual_accounts WHERE id = ?", (p["loan_id"],)).fetchone()
    before = loan["balance"]
    conn.execute("SAVEPOINT unapply_payment")
    try:
        conn.execute("DELETE FROM loan_payments WHERE transaction_id = ?", (txn_id,))
        after = rebuild_loan(conn, loan["id"])
    except Exception:
        conn.execute("ROLLBACK TO unapply_payment")
        conn.execute("RELEASE unapply_payment")
        raise
    if dry_run:
        conn.execute("ROLLBACK TO unapply_payment")
    conn.execute("RELEASE unapply_payment")
    conn.commit()
    return {"transaction_id": txn_id, "loan_id": loan["id"], "loan": loan["name"], "amount": p["amount"],
            "balance_before": before, "balance_after": after, "dry_run": dry_run}


def match_payments(conn: sqlite3.Connection, dry_run: bool = False, today: date | None = None) -> dict[str, Any]:
    """`finance match-payments` (and its alias `loan-match`): for every active loan and credit card with a
    match_pattern, apply each unapplied posted outflow after its balance_as_of whose name or merchant_name contains
    the pattern (source 'auto'), oldest first. Never one that pairs with a connected card's payment (card_pair()); a
    card payment waits until it's PAIR_DAYS old; an automatic match that pairs by now is taken back (`unapplied`).
    A dry run computes everything, then rolls it all back."""
    today = today or date.today()
    applied, skipped, unapplied = [], [], []
    conn.execute("SAVEPOINT loan_match")
    try:
        for acct in conn.execute("SELECT * FROM manual_accounts WHERE kind IN ('loan', 'credit_card') "
                                 "AND archived_at IS NULL AND match_pattern IS NOT NULL ORDER BY id").fetchall():
            for p in conn.execute("SELECT transaction_id, amount FROM loan_payments WHERE loan_id = ? AND source = 'auto'",
                                  (acct["id"],)).fetchall():
                pair = card_pair(conn, p["transaction_id"])
                if pair:
                    conn.execute("DELETE FROM loan_payments WHERE transaction_id = ?", (p["transaction_id"],))
                    unapplied.append({"transaction_id": p["transaction_id"], "loan_id": acct["id"], "loan": acct["name"],
                                      "amount": p["amount"], "reason": pair_reason(pair)})
            found = matching_transactions(conn, acct["match_pattern"], after=acct["balance_as_of"])
            rebuild_loan(conn, acct["id"])  # a payment the bank has since removed (or that pairs) stops counting
            for t in sorted(found, key=lambda x: (x["date"], x["transaction_id"])):
                if t["applied_to"] is not None:
                    continue
                pair = card_pair(conn, t["transaction_id"])
                age = (today - date.fromisoformat(t["date"])).days
                if pair:
                    skipped.append({"transaction_id": t["transaction_id"], "loan_id": acct["id"], "reason": pair_reason(pair)})
                elif acct["kind"] == "credit_card" and age < PAIR_DAYS:
                    skipped.append({"transaction_id": t["transaction_id"], "loan_id": acct["id"],
                                    "reason": f"waiting {PAIR_DAYS - age} more day(s): a connected card's payment "
                                              f"could still post for it"})
                else:
                    try:
                        applied.append(_apply_in_savepoint(conn, t["transaction_id"], acct["id"]))
                    except ManualError as e:
                        skipped.append({"transaction_id": t["transaction_id"], "loan_id": acct["id"], "reason": str(e)})
    except Exception:
        conn.execute("ROLLBACK TO loan_match")
        conn.execute("RELEASE loan_match")
        raise
    if dry_run:
        conn.execute("ROLLBACK TO loan_match")
    conn.execute("RELEASE loan_match")
    conn.commit()
    return {"applied": applied, "skipped": skipped, "unapplied": unapplied, "dry_run": dry_run}


match_loans = match_payments  # the TASK-8 name


def _apply_in_savepoint(conn: sqlite3.Connection, txn_id: str, loan_id: int) -> dict[str, Any]:
    """apply_payment without its own commit (match_loans commits or rolls back the whole run)."""
    loan = get(conn, loan_id)
    t = conn.execute("SELECT date, name, merchant_name, amount FROM transactions WHERE transaction_id = ?",
                     (txn_id,)).fetchone()
    if round(loan["balance"] * 100) <= 0:
        raise ManualError(f"#{loan['id']} {loan['name']} is already paid off", "loan_id")
    before = loan["balance"]
    conn.execute("INSERT INTO loan_payments (transaction_id, loan_id, amount, interest, principal, applied_at, source) "
                 "VALUES (?, ?, ?, 0, 0, ?, 'auto')", (txn_id, loan_id, _r(t["amount"]), now()))
    after = rebuild_loan(conn, loan_id)
    pay = _payment_view(conn, txn_id)
    return {"transaction_id": txn_id, "loan_id": loan_id, "loan": loan["name"], "date": t["date"],
            "name": t["merchant_name"] or t["name"], "amount": pay["amount"], "interest": pay["interest"],
            "principal": pay["principal"], "balance_before": before, "balance_after": after,
            "paid_off": round(after * 100) <= 0, "source": "auto",
            "apr_set": bool(loan["interest_rate"]) and loan["kind"] == "loan", "kind": loan["kind"]}


# --- views for the API / CLI ----------------------------------------------------------------------------------

GROUPS = {"bank": "cash", "credit_card": "cards", "loan": "loans", "investment": "investments", "property": "property",
          "vehicle": "vehicles", "other_asset": "other_assets", "other_liability": "other_debts"}


def describe(conn: sqlite3.Connection, row: sqlite3.Row, today: date, spark_days: int = 60,
             free: dict[str, bool] | None = None) -> dict[str, Any]:
    """A manual account as the dashboard shows it: signed value, freshness, loan progress, and (vehicle / property)
    its estimate. `free`: which keyless sources are on (read from the config when None)."""
    d = {k: row[k] for k in row.keys() if k != "vin_decoded"}
    kind = row["kind"]
    d["manual"] = True
    d["liability"] = kind in LIABILITY_KINDS
    d["signed"] = signed(row)
    d["group"] = GROUPS[kind]
    d["subtype_label"] = SUBTYPE_LABELS.get(row["subtype"] or "", row["subtype"]) if kind in SUBTYPES else row["subtype"]
    hist = history(conn, row["id"])
    since = date.fromordinal(today.toordinal() - spark_days).isoformat()
    d["spark"] = [h["balance"] for h in hist if h["date"] >= since]
    as_of = row["balance_as_of"]
    auto = kind in MATCH_KINDS and bool(row["match_pattern"])
    last_applied = None
    if kind == "loan":
        pays = payments(conn, row["id"])
        effective = [p for p in pays if p["date"] > row["balance_as_of"]]  # older ones are in the entered balance
        last_applied = effective[0]["date"] if effective else None
        as_of = max(as_of, last_applied or as_of)
        d["applied_count"] = len(effective)
        orig = row["original_amount"]
        d["paid_off"] = round(row["balance"] * 100) <= 0
        d["paid_pct"] = _r(min(max((orig - row["balance"]) / orig, 0.0), 1.0) * 100) if orig else None
        matched = matching_transactions(conn, row["match_pattern"], limit=1) if row["match_pattern"] else []
        last = pays[0] if pays else None
        if matched and (not last or matched[0]["date"] > last["date"]):
            last = matched[0]
        d["last_payment"] = {"date": last["date"], "amount": last["amount"],
                             "name": last["merchant_name"] or last["name"]} if last else None
        nxt = next_payment(last["date"] if last else None, today)
        d["next_payment"] = nxt.isoformat() if nxt else None
        first = nxt or add_one_month(today)
        d["payoff"] = payoff(row["balance"], row["interest_rate"], row["monthly_payment"], first)
    if kind == "credit_card":
        # Payments lower it, purchases aren't seen: "≈ $X (last set $Y on DATE, minus $Z matched payments)".
        effective = [p for p in payments(conn, row["id"]) if p["date"] > row["balance_as_of"]]
        d["entered_balance"] = entered_balance(conn, row)
        d["matched_count"] = d["applied_count"] = len(effective)
        d["matched_total"] = _r(sum(p["principal"] for p in effective))
        d["approx"] = auto or bool(effective)
        d["last_payment"] = ({"date": effective[0]["date"], "amount": effective[0]["amount"],
                              "name": effective[0]["merchant_name"] or effective[0]["name"]} if effective else None)
    if kind in VALUE_KINDS:
        from . import valuation  # it imports this module
        d["valuation"] = valuation.state(conn, row, today, free)
    d["as_of"] = as_of
    d["age_days"] = (today - date.fromisoformat(row["balance_as_of"])).days
    d["stale"] = d["age_days"] > STALE_DAYS and not auto
    d["nudge"] = kind == "credit_card" and auto and d["age_days"] > NUDGE_DAYS
    d["auto_updates"] = auto
    return d


def summary(conn: sqlite3.Connection, today: date, include_archived: bool = False) -> dict[str, Any]:
    all_rows = rows(conn, include_archived)
    free = None
    if any(r["kind"] in VALUE_KINDS for r in all_rows):
        from . import config
        free = config.free_sources(conn=conn)  # read once for the whole list (TASK-18: with the Settings switches)
    items = [describe(conn, r, today, free=free) for r in all_rows]
    live = [x for x in items if not x["archived_at"]]
    assets = _r(sum(x["balance"] for x in live if not x["liability"]))
    debts = _r(sum(x["balance"] for x in live if x["liability"]))
    return {"accounts": items, "assets": assets, "debts": debts, "net": _r(assets - debts), "count": len(live)}
