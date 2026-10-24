"""JSON payloads for the dashboard (`finance serve`). HTTP-agnostic: web.py routes requests here.

Every GET handler has the signature `(conn, params, today) -> dict`, where `params` holds the query
string (first value per key). All SQL is parameterized; filters never reach SQL as text.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

from . import (alpaca, bills, classify, goals, groupbudget, insights, logos, manual, mcp_state, portfolio, prefs, report, txmeta,
               valuation)
from .db import now

# Only these image hosts are ever handed to the browser (they're also the CSP img-src).
IMAGE_HOSTS = frozenset({"plaid-merchant-logos.plaid.com", "plaid-category-icons.plaid.com"})
# Needs review: what nobody has decided. An 'Other' row counts only while undecided (not confirmed or changed by the
# AI, not filed there by the user or a rule); a row with no category at all (source 'default') and a row the AI
# flagged with no user decision since always count.
DECIDED_VERDICTS = ("confirmed", "changed")
DECIDED_SOURCES = ("user", "rule")
OTHER_UNDECIDED_SQL = ("(v.category = 'Other' AND NOT (COALESCE(v.ai_verdict, '') IN ('confirmed', 'changed') "
                       "OR v.category_source IN ('user', 'rule')))")
UNCATEGORIZED_SQL = f"({OTHER_UNDECIDED_SQL} OR v.category_source = 'default')"
FLAGGED_SQL = "v.ai_flagged = 1"
REVIEW_SQL = f"({UNCATEGORIZED_SQL} OR {FLAGGED_SQL})"


def needs_review(category: str | None, source: str | None, verdict: str | None, flagged: Any) -> bool:
    """REVIEW_SQL for one row."""
    return is_uncategorized(category, source, verdict) or bool(flagged)


def is_uncategorized(category: str | None, source: str | None, verdict: str | None) -> bool:
    """UNCATEGORIZED_SQL for one row: what ?uncategorized=1 matches, and what the page shows in amber (polish)."""
    other_undecided = category == "Other" and not (verdict in DECIDED_VERDICTS or source in DECIDED_SOURCES)
    return other_undecided or source == "default"


KINDS = {
    "all": "1 = 1",
    "review": REVIEW_SQL,
    "spend": "(v.is_income = 0 AND v.is_transfer = 0)",
    "income": "v.is_income = 1",
    "transfers": "v.is_transfer = 1",
}
DEFAULT_LIMIT, MAX_LIMIT = 50, 500
MAX_TEXT = 200
MAX_CAP = 100_000
MAX_PICKS = 60   # values of one repeated parameter (?category=A&category=B ...)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.field = field  # the form field an error belongs to, so the page can show it inline


# --- parameters ----------------------------------------------------------------------------------

def month_param(value: str | None, today: date) -> str:
    if not value:
        return insights.month_of(today)
    try:
        insights.parse_month(value)
    except ValueError:
        raise ApiError("month must be YYYY-MM") from None
    return value


def int_param(value: str | None, default: int, lo: int, hi: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except ValueError:
        raise ApiError(f"{name} must be an integer") from None
    if not lo <= n <= hi:
        raise ApiError(f"{name} must be between {lo} and {hi}")
    return n


def text_param(value: str | None, name: str) -> str | None:
    if value in (None, ""):
        return None
    if len(value) > MAX_TEXT:
        raise ApiError(f"{name} is too long")
    return value


def list_param(value: str | list[str] | None, name: str) -> list[str]:
    """A parameter that may repeat (web.py passes every value of one in LIST_PARAMS as a list; a saved filter may hold
    one name or a list): each value as text_param takes it, blanks dropped, each once, in order."""
    values = value if isinstance(value, list) else [value]
    if len(values) > MAX_PICKS:
        raise ApiError(f"{name} takes at most {MAX_PICKS} values")
    out: list[str] = []
    for v in values:
        if v is not None and not isinstance(v, str):
            raise ApiError(f"{name} must be text")
        v = text_param(v, name)
        if v and v not in out:
            out.append(v)
    return out


def safe_image_url(url: Any) -> str | None:
    """The URL if it's https on an allowed image host (no credentials, default port), else None."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or (parts.hostname or "") not in IMAGE_HOSTS or port is not None
            or parts.username is not None or parts.password is not None):
        return None
    return url


def _clean_images(item: dict[str, Any]) -> dict[str, Any]:
    return {**item, "logo_url": safe_image_url(item.get("logo_url")), "icon_url": safe_image_url(item.get("icon_url"))}


def last_sync(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MAX(last_sync_at) FROM items WHERE removed_at IS NULL").fetchone()[0]


# --- transactions ----------------------------------------------------------------------------------

TXN_SELECT = """
    SELECT v.transaction_id, v.date, v.name, v.merchant_name, v.amount, v.pending, v.category,
           v.category_source, v.is_income, v.is_transfer, v.account_name, v.account_mask,
           v.institution_name, it.institution_id, v.logo_url, v.ai_note, v.ai_flagged, v.ai_verdict,
           json_extract(t.raw_json, '$.personal_finance_category_icon_url') AS icon_url
    FROM v_transactions v JOIN transactions t USING (transaction_id) LEFT JOIN items it ON it.item_id = v.item_id"""


def _txn(r: sqlite3.Row) -> dict[str, Any]:
    merchant = (r["merchant_name"] or r["name"] or "").strip()
    return {
        "transaction_id": r["transaction_id"],
        "date": r["date"],
        "merchant": merchant,
        "name": r["name"],
        "merchant_name": r["merchant_name"],
        "amount": r["amount"],
        "pending": bool(r["pending"]),
        "category": r["category"],
        "category_source": r["category_source"],
        "review": needs_review(r["category"], r["category_source"], r["ai_verdict"], r["ai_flagged"]),
        "uncategorized": is_uncategorized(r["category"], r["category_source"], r["ai_verdict"]),
        "ai_flagged": bool(r["ai_flagged"]),
        "ai_note": r["ai_note"],
        "is_income": bool(r["is_income"]),
        "is_transfer": bool(r["is_transfer"]),
        "account_name": r["account_name"],
        "account_mask": r["account_mask"],
        "institution_name": r["institution_name"],
        "institution_id": r["institution_id"],  # its logo, if cached: /api/institutions/<id>/logo
        "logo_url": safe_image_url(r["logo_url"]),
        "icon_url": safe_image_url(r["icon_url"]),
        "seed": merchant,  # avatar initials + hue come from this
    }


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# polish P1: the orders the Transactions page sorts by (?sort=, a column and a direction). Every one ends with the same
# tiebreaks, so two loads list the same rows in the same order: within a day the biggest amount first (Plaid sign: the
# biggest spend; money in last), then the latest authorization (Plaid's authorized_datetime or datetime when the bank
# gives one, else the authorized date; none last), then the id. Over TXN_SELECT (`t` is the transactions row).
AUTHORIZED_SQL = ("datetime(COALESCE(json_extract(t.raw_json, '$.authorized_datetime'), "
                  "json_extract(t.raw_json, '$.datetime'), v.authorized_date))")
WITHIN_DAY = f"v.amount DESC, {AUTHORIZED_SQL} DESC, v.transaction_id"
MERCHANT_SQL = "COALESCE(NULLIF(TRIM(v.merchant_name), ''), TRIM(v.name), '') COLLATE NOCASE"
SORTS = {
    "date_desc": f"v.date DESC, {WITHIN_DAY}",
    "date_asc": f"v.date, {WITHIN_DAY}",
    "amount_desc": f"v.amount DESC, v.date DESC, {AUTHORIZED_SQL} DESC, v.transaction_id",
    "amount_asc": f"v.amount, v.date DESC, {AUTHORIZED_SQL} DESC, v.transaction_id",
    "merchant_asc": f"{MERCHANT_SQL}, v.date DESC, {WITHIN_DAY}",
    "merchant_desc": f"{MERCHANT_SQL} DESC, v.date DESC, {WITHIN_DAY}",
    "category_asc": f"v.category COLLATE NOCASE, v.date DESC, {WITHIN_DAY}",
    "category_desc": f"v.category COLLATE NOCASE DESC, v.date DESC, {WITHIN_DAY}",
    "account_asc": ("COALESCE(v.institution_name, '') COLLATE NOCASE, COALESCE(v.account_name, '') COLLATE NOCASE, "
                    f"COALESCE(v.account_mask, ''), v.date DESC, {WITHIN_DAY}"),
    "account_desc": ("COALESCE(v.institution_name, '') COLLATE NOCASE DESC, COALESCE(v.account_name, '') COLLATE NOCASE DESC, "
                     f"COALESCE(v.account_mask, '') DESC, v.date DESC, {WITHIN_DAY}"),
}
DEFAULT_SORT = "date_desc"
LIST_PARAMS = ("category", "group", "tag")   # may repeat: any of them matches
SPLIT_SQL = "EXISTS (SELECT 1 FROM transaction_splits s WHERE s.transaction_id = v.transaction_id)"


def _picked(cats: list[str], groups: list[str]) -> tuple[str, list[Any]]:
    """SQL over a categories row `c`: it is one of `cats` (by name) or files under one of `groups`."""
    parts, args = [], []
    if cats:
        parts.append(f"c.name IN ({', '.join('?' * len(cats))})")
        args += cats
    if groups:
        parts.append(f"c.group_id IN (SELECT g.id FROM category_groups g WHERE g.name IN ({', '.join('?' * len(groups))}))")
        args += groups
    return "(" + " OR ".join(parts) + ")", args


def category_filter(cats: list[str], groups: list[str], uncategorized: bool = False) -> tuple[str, list[Any]] | None:
    """polish P1: the Transactions page's category picks, any of them: categories, whole groups, Uncategorized (no
    decided category: UNCATEGORIZED_SQL). Split-aware like txmeta.CATEGORY_SQL: a split transaction matches by its
    lines, never by the category it keeps. None with nothing picked."""
    parts, args = [], []
    if cats or groups:
        pick, pargs = _picked(cats, groups)
        parts += [f"(NOT {SPLIT_SQL} AND EXISTS (SELECT 1 FROM categories c WHERE c.id = v.category_id AND {pick}))",
                  "EXISTS (SELECT 1 FROM transaction_splits s JOIN categories c ON c.id = s.category_id "
                  f"WHERE s.transaction_id = v.transaction_id AND {pick})"]
        args += pargs * 2
    if uncategorized:
        parts.append(UNCATEGORIZED_SQL)
    return ("(" + " OR ".join(parts) + ")", args) if parts else None


def _amount_sql(cats: list[str], groups: list[str]) -> tuple[str, list[Any]]:
    """What a matching transaction adds to a total: its amount; with categories picked, a split one adds only its lines
    in them (so a total reconciles with Where it went's)."""
    if not (cats or groups):
        return "v.amount", []
    pick, pargs = _picked(cats, groups)
    return (f"(CASE WHEN {SPLIT_SQL} THEN (SELECT COALESCE(SUM(s.amount), 0) FROM transaction_splits s JOIN categories c "
            f"ON c.id = s.category_id WHERE s.transaction_id = v.transaction_id AND {pick}) ELSE v.amount END)", pargs)


def transactions(conn: sqlite3.Connection, params: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """The Transactions page: ?month, q, bank, kind, and (polish P1) any of the picked categories (?category=, may
    repeat), groups (?group=) or ?uncategorized=1, any of the picked tags (?tag=), and ?sort= (SORTS). Every filter
    narrows; the counts, the total and `sum` follow all of them. `days`: each date on this page with its count and total
    over every matching row (not only this page's). With ?facets=1, `facets` (the filter drawer's counts)."""
    kind = params.get("kind") or "all"
    if kind not in KINDS:
        raise ApiError(f"kind must be one of {', '.join(KINDS)}")
    sort = params.get("sort") or DEFAULT_SORT
    if sort not in SORTS:
        raise ApiError(f"sort must be one of {', '.join(SORTS)}")
    limit = int_param(params.get("limit"), DEFAULT_LIMIT, 1, MAX_LIMIT, "limit")
    offset = int_param(params.get("offset"), 0, 0, 10_000_000, "offset")
    # Local import: reads shares transaction_page with the API; keep one period parser.
    from . import reads
    try:
        period = reads.parse_period(today or date.today(), month=params.get("month") or None,
                                    since=params.get("since"), until=params.get("until"), default="all")
    except reads.ReadError as e:
        raise ApiError(str(e)) from None
    where, args = period.sql()
    q = text_param(params.get("q"), "q")
    if q:  # TASK-19: a note or a tag matches too
        where.append("(v.merchant_name LIKE ? ESCAPE '\\' OR v.name LIKE ? ESCAPE '\\'"
                     + "".join(f" OR {x} LIKE ? ESCAPE '\\'" for x in txmeta.SEARCH_SQL) + ")")
        args += [_like(q)] * (2 + len(txmeta.SEARCH_SQL))
    bank = text_param(params.get("bank"), "bank")
    if bank:
        where.append("v.institution_name = ?")
        args.append(bank)
    cats, groups, tags = (list_param(params.get(k), k) for k in LIST_PARAMS)
    picked = category_filter(cats, groups, params.get("uncategorized") in ("1", "true"))
    tagged = (txmeta.any_tag_sql(len(tags)), tags) if tags else None   # TASK-19: one tag; P1: any of several
    full_where = where + [p[0] for p in (picked, tagged) if p]
    full_args = args + [a for p in (picked, tagged) if p for a in p[1]]
    page = transaction_page(conn, full_where, full_args, kind, limit, offset, sort)
    bills.decorate(conn, page["rows"])  # TASK-21: "higher / lower than usual" on a tracked bill's charge (the page only)
    amount = _amount_sql(cats, groups)
    page.update(sort=sort, **_totals(conn, full_where, full_args, kind, amount, sorted({r["date"] for r in page["rows"]})))
    page.update(period=period.json(), coverage_month=transaction_coverage(conn))
    if params.get("facets") in ("1", "true"):
        page["facets"] = facets(conn, kind, (where + ([tagged[0]] if tagged else []), args + (tagged[1] if tagged else [])),
                                (where + ([picked[0]] if picked else []), args + (picked[1] if picked else [])))
    return page


def transaction_coverage(conn: sqlite3.Connection) -> str | None:
    """Latest first observed month among connected items with transactions.

    Empty items have no observed boundary; ignore them rather than inventing one.
    Removed items and removed transactions do not establish current coverage.
    """
    return conn.execute("SELECT MAX(first_month) FROM (SELECT MIN(substr(t.date, 1, 7)) AS first_month "
                        "FROM items i JOIN transactions t ON t.item_id = i.item_id AND t.removed = 0 "
                        "WHERE i.removed_at IS NULL GROUP BY i.item_id)").fetchone()[0]


def _totals(conn: sqlite3.Connection, where: list[str], args: list[Any], kind: str, amount: tuple[str, list[Any]],
            dates: list[str]) -> dict[str, Any]:
    """`sum`: what every matching row adds up to (Plaid sign: > 0 is money out), and `days`: {date: {count, total}} for
    `dates`, over every matching row on each. A transfer moves money between your own accounts, so no total counts it
    (the table greys its amount), unless transfers are all that match."""
    base = f"{' AND '.join(where) or '1 = 1'} AND {KINDS[kind]}"
    expr, eargs = amount
    if conn.execute(f"SELECT 1 FROM v_transactions v WHERE {base} AND v.is_transfer = 0 LIMIT 1", args).fetchone():
        expr = f"(CASE WHEN v.is_transfer = 1 THEN 0 ELSE {expr} END)"
    total = conn.execute(f"SELECT COALESCE(SUM({expr}), 0) FROM v_transactions v WHERE {base}", [*eargs, *args]).fetchone()[0]
    months = {m: {"count": n, "total": insights._r(s)} for m, n, s in conn.execute(
        f"SELECT v.month, COUNT(*), COALESCE(SUM({expr}), 0) FROM v_transactions v WHERE {base} GROUP BY v.month",
        [*eargs, *args])}
    days = {}
    if dates:
        for d, n, s in conn.execute(f"SELECT v.date, COUNT(*), COALESCE(SUM({expr}), 0) FROM v_transactions v WHERE {base} "
                                    f"AND v.date IN ({', '.join('?' * len(dates))}) GROUP BY v.date", [*eargs, *args, *dates]):
            days[d] = {"count": n, "total": insights._r(s)}
    return {"sum": insights._r(total), "days": days, "months": months}


def facets(conn: sqlite3.Connection, kind: str, for_categories: tuple[list[str], list[Any]],
           for_tags: tuple[list[str], list[Any]]) -> dict[str, Any]:
    """polish P1: the filter drawer's counts, each for the kind shown and every other filter (`for_categories`: all but
    the category picks; `for_tags`: all but the tag picks). categories {name: n} and groups {name: n}: the transactions
    each would match (a split one counts once in each category its lines are in, and once in a group), so with no other
    filter a spending category's count is Where it went's; uncategorized: n; tags [{name, count}]: every tag in use,
    most used first."""
    def base(w: list[str]) -> str:
        return f"{' AND '.join(w) or '1 = 1'} AND {KINDS[kind]}"
    lines = ("FROM v_transactions v LEFT JOIN transaction_splits s ON s.transaction_id = v.transaction_id "
             "JOIN categories c ON c.id = COALESCE(s.category_id, v.category_id)")
    w, a = for_categories
    cats = dict(conn.execute(f"SELECT c.name, COUNT(DISTINCT v.transaction_id) {lines} WHERE {base(w)} GROUP BY c.id", a).fetchall())
    groups = dict(conn.execute(f"SELECT g.name, COUNT(DISTINCT v.transaction_id) {lines} JOIN category_groups g ON g.id = c.group_id "
                               f"WHERE {base(w)} GROUP BY g.id", a).fetchall())
    uncategorized = conn.execute(f"SELECT COUNT(*) FROM v_transactions v WHERE {base(w)} AND {UNCATEGORIZED_SQL}", a).fetchone()[0]
    w, a = for_tags
    tagged = dict(conn.execute(f"SELECT tt.tag_id, COUNT(DISTINCT v.transaction_id) FROM v_transactions v JOIN transaction_tags tt "
                               f"ON tt.transaction_id = v.transaction_id WHERE {base(w)} GROUP BY tt.tag_id", a).fetchall())
    tags = [{"name": r["name"], "count": tagged.get(r["id"], 0)} for r in conn.execute(
        "SELECT tg.id, tg.name, COUNT(t.transaction_id) AS n FROM tags tg JOIN transaction_tags tt ON tt.tag_id = tg.id "
        "JOIN transactions t ON t.transaction_id = tt.transaction_id AND t.removed = 0 "
        "GROUP BY tg.id ORDER BY n DESC, tg.name COLLATE NOCASE")]
    return {"categories": cats, "groups": groups, "uncategorized": uncategorized, "tags": tags}


def transaction_page(conn: sqlite3.Connection, where: list[str], args: list[Any], kind: str = "all",
                     limit: int = DEFAULT_LIMIT, offset: int = 0, sort: str = DEFAULT_SORT) -> dict[str, Any]:
    """Counts per kind and one page of rows (newest first, or as `sort`) for filters already turned into SQL over
    `v_transactions v` (parameterized by `args`). Shared by the Transactions page and `finance txns`."""
    base = " AND ".join(where) or "1 = 1"
    sums = ", ".join(f"COALESCE(SUM(CASE WHEN {sql} THEN 1 ELSE 0 END), 0)" for sql in KINDS.values())
    row = conn.execute(f"SELECT {sums} FROM v_transactions v WHERE {base}", args).fetchone()
    counts = dict(zip(KINDS, row))
    rows = conn.execute(
        f"{TXN_SELECT} WHERE {base} AND {KINDS[kind]} ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?",
        [*args, limit, offset]).fetchall()
    return {"total": counts[kind], "counts": counts, "limit": limit, "offset": offset,
            "rows": txmeta.decorate(conn, [_txn(r) for r in rows])}  # TASK-19: split, note, tags


def recent(conn: sqlite3.Connection, month: str, n: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"{TXN_SELECT} WHERE v.month = ? AND v.is_transfer = 0 ORDER BY {SORTS[DEFAULT_SORT]} LIMIT ?", (month, n)).fetchall()
    return [_txn(r) for r in rows]


# --- pages -------------------------------------------------------------------------------------------

def overview(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    month = month_param(params.get("month"), today)
    rec = insights.recurring(conn, today)
    items = [_clean_images(r) for r in rec["items"]]
    cats = insights.category_spend(conn, month, today, rec)
    alerts = insights.alerts(conn, month, today, cats)
    nw = insights.net_worth(conn, history=False, today=today)
    banks = conn.execute("SELECT COUNT(*) FROM items WHERE removed_at IS NULL").fetchone()[0]
    api_items = alpaca.items_payload(conn)
    horizon = (today + timedelta(days=30)).isoformat()
    flow = insights.cashflow(conn, 6, today, end=month)
    return {
        "month": month,
        "today": today.isoformat(),
        "summary": insights.month_summary(conn, month, today),
        "pace": insights.pace_projection(conn, month, today, rec),
        "budgets": insights.budgets(conn, month, today, cats),
        "categories": cats,
        "spend_groups": insights.group_spend(conn, month, cats),
        "alerts": alerts,
        # TASK-22f: every connection, the Plaid banks and Alpaca (the health chip: "All 9 connections healthy"; banks is
        # the Plaid part, what a sync covers)
        "connections": {
            "total": banks + len(api_items), "banks": banks,
            "broken": sum(a["kind"] == "connection" for a in alerts)
                      + sum(it["status"] != "active" or bool(it["error_code"]) for it in api_items)},
        "bills": [r for r in items if r["kind"] == "bill"],
        "upcoming": [r for r in items if today.isoformat() <= r["next_expected"] <= horizon],
        "recurring_totals": rec["monthly_totals"],
        "cashflow": flow,
        "net_worth": {k: nw[k] for k in ("assets", "debts", "net", "custom")}
                     | ({"in_transit": nw["in_transit"]} if nw["in_transit"] else {}),   # TASK-22: counted in the net
        "investments": investments_tile(conn, today),
        "debt": debt_tile(conn, today, nw["in_transit"]),
        "cards_due": cards_due(conn, today),
        "category_history": insights.category_history(conn, [m["month"] for m in flow["months"]]),
        "recent": recent(conn, month),
        "last_sync": last_sync(conn),
        "chart": {"excluded": chart_excluded(conn), "fixed_categories": list(insights.FIXED_CATEGORIES)},
        "goals": goals.tile(conn, today),  # TASK-19: the Overview's Goals tile
    }


def investments_tile(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    inv = portfolio.investments(conn, today)
    return {"total": inv["total"], "priced_total": inv["priced_total"], "slices": [{"label": s["label"], "value": s["value"], "cash": s.get("cash", False),
                                               **({"residual": True} if s.get("residual") else {})}
                                              for s in inv["allocation"]["by_holding"] if s["value"] > 0],
            "days": inv["days"], "tracking_since": inv["tracking_since"], "change_7d": inv["changes"]["7d"]}


def debt_tile(conn: sqlite3.Connection, today: date, in_transit: list[dict[str, Any]] = ()) -> dict[str, Any]:
    """The Overview's Loans & debt tile. TASK-22f: each payment in transit (insights.net_worth's) counts as paid here too,
    so the tile never contradicts net worth's Owe: `total` is what you owe counted once, `reported` what the banks say,
    `in_transit` the payments (the tile's line)."""
    d = portfolio.debt(conn, today)
    loans = sorted((l for l in d["loans"] if l["paid_pct"] is not None and not l["paid_off"]),
                   key=lambda l: -l["balance"])
    cards = next((g for g in d["groups"] if g["key"] == "cards"), None)
    moved = sum(e["amount"] for e in in_transit)
    return {"total": round(d["total"] - moved, 2) if moved else d["total"], "reported": d["total"], "in_transit": list(in_transit),
            "cards_util": cards["util"] if cards else None,
            "loan": {"name": loans[0]["name"], "paid_pct": loans[0]["paid_pct"]} if loans else None,
            "count": len(d["cards"]) + len(d["loans"]) + len(d["other"])}


def cards_due(conn: sqlite3.Connection, today: date, days: int = insights.DUE_SOON_DAYS) -> list[dict[str, Any]]:
    """Plaid credit cards with a minimum payment due in the next few days (for the overview alerts)."""
    end = date.fromordinal(today.toordinal() + days).isoformat()
    return [{"account_id": r["account_id"], "name": r["name"], "institution_name": r["institution_name"],
             "mask": r["mask"], "amount": r["minimum_payment_amount"], "due": r["next_payment_due_date"]}
            for r in conn.execute(
                """SELECT a.account_id, a.name, a.mask, i.institution_name, l.minimum_payment_amount,
                          l.next_payment_due_date
                   FROM liabilities l JOIN accounts a ON a.account_id = l.account_id
                   JOIN items i ON i.item_id = a.item_id
                   WHERE i.removed_at IS NULL AND l.kind = 'credit' AND l.minimum_payment_amount > 0
                     AND l.next_payment_due_date BETWEEN ? AND ? ORDER BY l.next_payment_due_date""",
                (today.isoformat(), end))]


def counts(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """Small numbers for the sidebar badges."""
    month = month_param(params.get("month"), today)
    total, review = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN {REVIEW_SQL} THEN 1 ELSE 0 END), 0) "
        "FROM v_transactions v WHERE v.month = ?", (month,)).fetchone()
    b = insights.budgets(conn, month, today)
    return {"month": month, "transactions": total, "review": review, "over": b["over"], "warn": b["warn"],
            "recurring": insights.recurring(conn, today)["count"]}


def not_budgeted(cats: list[dict[str, Any]], suggestions: dict[str, Any], covered: set[str] = frozenset()) -> list[dict[str, Any]]:
    """Spending categories with neither a cap nor a suggestion, each with the reason why. `covered`: the categories a
    group budget already counts (polish P2e), never listed."""
    sug = {s["category"]: s for s in suggestions["categories"]}
    out = []
    for c in cats:
        s = sug.get(c["category"])
        if c["cap"] is not None or (s and s["suggested"] is not None) or c["category"] in covered:
            continue
        if c["category"] in insights.NO_SUGGEST:
            reason = insights.NO_SUGGEST_REASONS[c["category"]]
        elif s and all(v <= 0 for v in s["values"]):
            reason = "No spending in the last 3 full months"
        else:
            reason = "Not enough history to suggest a cap"
        out.append({"category": c["category"], "spend": c["spend"], "reason": reason})
    return out


def quiet_suggestions(s: dict[str, Any]) -> dict[str, Any]:
    """Suggestions only for categories without a cap: a capped category carries no suggestion."""
    cats = [c for c in s["categories"] if c["cap"] is None]
    return {"months": s["months"], "categories": cats, "count": sum(c["suggested"] is not None for c in cats),
            "total": insights._r(sum(c["suggested"] or 0 for c in cats))}


def budget_groups(conn: sqlite3.Connection, cats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """polish P2A: the Budgets table's groups, in the taxonomy's display order (db.CATEGORY_GROUPS), each with every
    spending category in it (`categories`, their order) and the totals of its CAPPED ones: planned (the effective caps),
    spent, due (bills still due), left = planned - spent - due, the same net `insights.budgets` takes over the page, so
    the groups' figures add up to `budgets.total_*` / `left`. A group with no cap has count 0 and zeros. A spending
    category with no group (made by hand since v16) files under Misc, like the rest of the app.

    Group roll-up (2026-10-22): a group with caps and no budget of its own counts EVERY spending category in it, capped
    or not: spent / due are all of them, left = planned (its caps' sum) - spent - due (groupbudget.rest).

    polish P2e: a group with a budget of its own (groupbudget.py) has `budget` set, and then planned = the budget,
    spent / due = every spending category in it (capped or not), left = budget - spent - due and `status` its own; its
    caps are still listed but `caps_counted` is false (the page's totals take the group once). Every group also carries
    `caps_total` (the caps' Planned, summed), `over_note` (that sum when a budgeted group's caps add up to more than its
    budget, else null), `eligible` (offered by the one-time move: no budget, not kept, at least one cap) and `kept`."""
    by_name = {c["category"]: c for c in cats}
    units = {u["group"]: u for u in groupbudget.units(conn, cats)}
    gone = groupbudget.kept(conn)
    groups: dict[str, dict[str, Any]] = {
        r["name"]: {"name": r["name"], "rgb": r["rgb"], "categories": []}
        for r in conn.execute("SELECT name, rgb FROM category_groups ORDER BY sort, id")}
    for r in conn.execute("SELECT c.name, g.name AS grp FROM categories c LEFT JOIN category_groups g ON g.id = c.group_id "
                          "WHERE c.is_income = 0 AND c.is_transfer = 0 ORDER BY c.sort, c.name"):
        if r["name"] in by_name:
            groups.get(r["grp"], groups.get("Misc") or next(iter(groups.values())))["categories"].append(r["name"])
    out = []
    for g in groups.values():
        rows = [by_name[n] for n in g["categories"] if by_name[n]["cap"] is not None]
        # group roll-up (2026-10-22): a group with caps counts every category in it, capped or not
        every = [by_name[n] for n in g["categories"]] if rows else []
        unit = units.get(g["name"])
        caps_total = insights._r(sum(c["cap"] for c in rows))
        spent = insights._r(sum(c["spend"] for c in every))
        due = insights._r(sum(c["due"] for c in every))
        out.append({**g,
                    "count": len(rows),
                    "planned": unit["budget"] if unit else caps_total,
                    "spent": unit["spend"] if unit else spent,
                    "due": unit["due"] if unit else due,
                    "left": unit["left"] if unit else insights._r(caps_total - spent - due),
                    "over": sum(c["status"] == "over" for c in rows),
                    "warn": sum(c["status"] == "warn" for c in rows),
                    "budget": unit["budget"] if unit else None,
                    "status": unit["status"] if unit else None,
                    "caps_total": caps_total,
                    "caps_counted": unit is None,
                    "over_note": unit["over_note"] if unit else None,
                    "eligible": unit is None and g["name"] not in gone and bool(rows),
                    "kept": g["name"] in gone})
        for n in g["categories"]:
            by_name[n]["group"] = g["name"]
    return out


def budgets_page(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    month = month_param(params.get("month"), today)
    flow = insights.cashflow(conn, 6, today, end=month)
    months = [m["month"] for m in flow["months"]]
    raw = insights.suggest_caps(conn, 3, today)
    suggestions = quiet_suggestions(raw)
    # Every category's suggestion (capped or not; null when there's no usable one) for the budget editor's
    # "Suggested $X · Use". Only the editor shows it: capped rows stay quiet.
    editor = {c["category"]: c["suggested"] for c in raw["categories"]}
    cats = [{**c, "suggested": editor.get(c["category"])} for c in insights.category_spend(conn, month, today)]
    for c in cats:
        # polish P2A: a category with no budget that carries spending now but has no history to suggest from (the finer
        # Plaid mapping filed it there only lately) gets `starter`: what it has taken so far, rounded up like the rest.
        # `suggested` stays the history-based value the editor has always had; the page-wide suggestions (the hero's
        # total, "Use all", not_budgeted) are untouched.
        c["starter"] = (insights.round_cap(c["spend"]) if c["cap"] is None and c["suggested"] is None and c["spend"] > 0
                        and c["category"] not in insights.NO_SUGGEST else None)
        c["new"] = c["cap"] is None and c["spend"] > 0 and c["last_month"] == 0 and c["avg3"] == 0
    groups = budget_groups(conn, cats)
    b = insights.budgets(conn, month, today, cats)
    return {
        "month": month,
        "today": today.isoformat(),
        "categories": cats,
        "groups": groups,
        "budgets": b,
        "suggestions": suggestions,
        # The big "Suggested budgets / Use all" hero only when no cap is set at all; otherwise the page
        # summarizes the caps and keeps suggestions behind a small "See suggestions" link.
        "suggestion_hero": b["count"] == 0 and suggestions["count"] > 0,
        "not_budgeted": not_budgeted(cats, suggestions, {n for u in [*b["group_budgets"], *b["group_rest"]] for n in u["categories"]}),
        # polish P2e: the one-time "Move N budgets to groups" review (groupbudget.move_plan): count = N, the groups
        "move": groupbudget.move_plan(conn, cats),
        "history": {
            "months": months,
            "coverage": [{"month": m["month"], "coverage": m["coverage"], "missing": m["missing"],
                          "is_current": m["is_current"]} for m in flow["months"]],
            "series": insights.category_history(conn, months),
        },
    }


def recurring_page(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    rec = insights.recurring(conn, today)
    return {**rec, "items": [_clean_images(r) for r in rec["items"]]}


def sparks(conn: sqlite3.Connection, today: date, days: int = 60) -> dict[str, list[float]]:
    """Daily balance per Plaid account for the last `days` days, from real snapshots only."""
    since = date.fromordinal(today.toordinal() - days).isoformat()
    out: dict[str, list[float]] = {}
    for r in conn.execute("SELECT account_id, current FROM balance_snapshots WHERE date >= ? AND current IS NOT NULL "
                          "ORDER BY date", (since,)):
        out.setdefault(r["account_id"], []).append(r["current"])
    return out


def accounts(conn: sqlite3.Connection, params: dict[str, str] | None = None,
             today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    s = report.status(conn)  # built on v_items: never includes access tokens
    connected = sorted({it["institution_id"] for it in s["items"] if not it["removed_at"] and it["institution_id"]})
    liab = {r["account_id"]: {"apr": r["interest_rate"], "min_payment": r["minimum_payment_amount"],
                              "due_date": r["next_payment_due_date"]}
            for r in conn.execute("SELECT account_id, interest_rate, minimum_payment_amount, next_payment_due_date "
                                  "FROM liabilities WHERE kind = 'credit'")}
    return {**s, "net_worth": insights.net_worth(conn, today=today), "connected_institution_ids": connected,
            "manual": manual.summary(conn, today)["accounts"], "sparks": sparks(conn, today),
            "liabilities": liab, "institutions": logos.institutions_payload(conn),
            "valuation": valuation.providers(conn, today),
            "api_items": alpaca.items_payload(conn),  # TASK-19: Alpaca (read-only API; no Plaid slot)
            "today": today.isoformat(), "last_sync": last_sync(conn)}


def manual_list(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    archived = params.get("archived") in ("1", "true")
    return manual.summary(conn, today, include_archived=archived)


def investments_page(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    return {**portfolio.investments(conn, today), "today": today.isoformat(), "last_sync": last_sync(conn)}


def debt_page(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    return portfolio.debt(conn, today)


LOAN_PFC = "t.pfc_primary = 'LOAN_PAYMENTS' AND COALESCE(t.pfc_detailed, '') <> 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'"


def match_preview(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """The loan and credit card forms' "Match payments from" helper (?kind=loan, the default, or credit_card; ?id= the
    account being edited). With ?pattern=: the posted outflows it matches, and `preview`, the dry run of the last 90
    days with match-payments' guards (manual.match_preview). Without: names to offer, seen in the bank transactions
    (loans: loan payments seen twice; cards: card payments that pay no connected card and match no pattern yet)."""
    kind = params.get("kind") or "loan"
    if kind not in manual.MATCH_KINDS:
        raise ApiError(f"kind must be one of {', '.join(manual.MATCH_KINDS)}")
    account_id = int_param(params.get("id"), 0, 0, 999_999_999, "id") or None
    pattern = text_param(params.get("pattern"), "pattern")
    if not pattern and kind == "credit_card":
        since = date.fromordinal(today.toordinal() - manual.SUGGEST_DAYS).isoformat()
        labels = [" ".join((t["name"] or t["merchant_name"] or "").split())
                  for t in manual.unpaired_card_payments(conn, today, since)]
        return {"suggestions": list(dict.fromkeys(x for x in labels if x))[:5]}
    if not pattern:
        rows = conn.execute(
            f"""SELECT COALESCE(NULLIF(TRIM(t.merchant_name), ''), TRIM(t.name)) AS label, COUNT(*) AS n,
                       MAX(t.date) AS last, t.amount
                FROM transactions t WHERE t.removed = 0 AND t.pending = 0 AND t.amount > 0 AND {LOAN_PFC}
                GROUP BY label HAVING n >= 2 ORDER BY last DESC LIMIT 5""").fetchall()
        return {"suggestions": [r["label"] for r in rows if r["label"]]}
    pattern = " ".join(pattern.split())
    if len(pattern) < manual.MATCH_MIN:
        raise ApiError(f"pattern needs at least {manual.MATCH_MIN} characters", field="match_pattern")
    found = manual.matching_transactions(conn, pattern)
    amounts = sorted(t["amount"] for t in found)
    last = found[0] if found else None
    return {"pattern": pattern, "kind": kind, "preview": manual.match_preview(conn, pattern, today, account_id),
            "count": len(found),
            "typical": amounts[len(amounts) // 2] if amounts else None,
            "last": {k: last[k] for k in ("date", "amount", "name", "merchant_name", "account_name", "account_mask",
                                           "institution_name")} if last else None,
            "matches": [{k: t[k] for k in ("transaction_id", "date", "amount", "name", "account_name", "account_mask",
                                           "applied_to")} for t in found[:12]]}


CATEGORY_ROWS = ("SELECT c.name, c.is_income, c.is_transfer, c.monthly_cap, c.icon, g.name AS grp, g.rgb "
                 "FROM categories c LEFT JOIN category_groups g ON g.id = c.group_id")


def _category(r: sqlite3.Row) -> dict[str, Any]:
    return {"name": r["name"], "is_income": bool(r["is_income"]), "is_transfer": bool(r["is_transfer"]),
            "spending": not (r["is_income"] or r["is_transfer"]), "monthly_cap": r["monthly_cap"],
            "group": r["grp"], "icon": r["icon"], "rgb": r["rgb"]}   # v16: group / rgb None for Income and Transfers


def categories(conn: sqlite3.Connection, params: dict[str, str] | None = None,
               today: date | None = None) -> list[dict[str, Any]]:
    return [_category(r) for r in conn.execute(f"{CATEGORY_ROWS} ORDER BY c.name")]


def category_groups(conn: sqlite3.Connection, params: dict[str, str] | None = None,
                    today: date | None = None) -> dict[str, Any]:
    """v16 (polish P0): the category tree, in display order. {"groups": [{name, rgb, categories: [category, ...]}, ...],
    "ungrouped": [Income, Transfers]}, each category as /api/categories gives it."""
    groups = {r["name"]: {"name": r["name"], "rgb": r["rgb"], "categories": []}
              for r in conn.execute("SELECT name, rgb FROM category_groups ORDER BY sort, id")}
    ungrouped = []
    for r in conn.execute(f"{CATEGORY_ROWS} ORDER BY c.sort, c.name"):
        (groups[r["grp"]]["categories"] if r["grp"] in groups else ungrouped).append(_category(r))
    return {"groups": list(groups.values()), "ungrouped": ungrouped}


# --- settings ----------------------------------------------------------------------------------------
# Dashboard preferences every device shares (the `settings` table, one JSON value per key). Only the "Where it went"
# chart reads chart_excluded_categories: no total, budget, alert, pace or read command ever does. networth_excluded
# (TASK-15) is the Custom net worth's selection, the accounts it leaves out (insights.networth_excluded).
SETTING_KEYS = ("chart_excluded_categories", "networth_excluded")
MAX_EXCLUDED = 50


def chart_excluded(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = 'chart_excluded_categories'").fetchone()
    try:
        value = json.loads(row["value"]) if row else []
    except ValueError:
        return []
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def settings(conn: sqlite3.Connection, params: dict[str, str] | None = None, today: date | None = None) -> dict[str, Any]:
    return {"chart_excluded_categories": chart_excluded(conn), "fixed_categories": list(insights.FIXED_CATEGORIES),
            "networth_excluded": insights.networth_excluded(conn),
            "mcp": mcp_settings(conn),
            "prefs": prefs.public(conn)}   # TASK-18: the Settings page's values (sync & alerts, region, ...)


def settings_for_owner(conn: sqlite3.Connection, params: dict[str, str] | None, today: date,
                       owner: str | None) -> dict[str, Any]:
    """The HTTP legacy Settings response. Keep non-request callers' historic global `settings()` behavior while the
    authenticated route reports a count consistent with the pending rows that request may see in Settings."""
    out = settings(conn, params, today)
    out["mcp"] = mcp_state.summary_for_owner(conn, owner)
    return out


def mcp_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """TASK-17, read-only here: {allow_writes, last_used: {client, title, version, transport, at} | null, pending}, for the
    Settings page ("last used by <client> at <time>"). Changed with `finance mcp allow-writes on|off`, not POST."""
    return mcp_state.summary(conn)


def _chart_setting(conn: sqlite3.Connection, value: Any) -> list[str]:
    """chart_excluded_categories: spending categories (case-insensitive, saved as named, once each).
    A category with no spending stays in the list."""
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ApiError("chart_excluded_categories must be a list of category names", field="chart_excluded_categories")
    if len(value) > MAX_EXCLUDED:
        raise ApiError(f"chart_excluded_categories takes at most {MAX_EXCLUDED} names", field="chart_excluded_categories")
    names = {r["name"].casefold(): r["name"] for r in conn.execute(
        "SELECT name FROM categories WHERE is_income = 0 AND is_transfer = 0")}
    out: list[str] = []
    for x in value:
        name = names.get(x.strip().casefold())
        if name is None:
            raise ApiError(f"{x[:MAX_TEXT]!r} is not a spending category", field="chart_excluded_categories")
        if name not in out:
            out.append(name)
    return out


def save_settings(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """{"chart_excluded_categories": [category, ...], "networth_excluded": [account id, ...]}, either or both.
    networth_excluded: the accounts the Custom net worth leaves out, as `finance networth --json` shows their ids (a
    Plaid account_id, or "manual:<id>"). An id that isn't a current account (unknown, archived, a removed bank's) is
    dropped, and the answer says so in `note` (with `dropped`); the rest is saved."""
    unknown = sorted(set(data) - set(SETTING_KEYS))
    if unknown:
        raise ApiError(f"unknown setting {unknown[0]!r}")
    if not any(k in data for k in SETTING_KEYS):
        raise ApiError("a setting is required: chart_excluded_categories (a list of category names) or "
                       "networth_excluded (a list of account ids)", field="chart_excluded_categories")
    chart = _chart_setting(conn, data["chart_excluded_categories"]) if "chart_excluded_categories" in data else None
    ids = data.get("networth_excluded")
    if "networth_excluded" in data:
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise ApiError("networth_excluded must be a list of account ids", field="networth_excluded")
        if len(ids) > insights.MAX_NETWORTH_EXCLUDED:
            raise ApiError(f"networth_excluded takes at most {insights.MAX_NETWORTH_EXCLUDED} ids", field="networth_excluded")
    if chart is not None:  # everything is validated by now: both are saved, or (a 400 above) neither
        with conn:
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('chart_excluded_categories', ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (json.dumps(chart), now()))
    dropped = insights.save_networth_excluded(conn, ids)[1] if ids is not None else []
    out = settings(conn)
    if dropped:
        shown = ", ".join(repr(x[:MAX_TEXT]) for x in dropped[:5]) + (f" and {len(dropped) - 5} more" if len(dropped) > 5 else "")
        out.update(dropped=dropped, note=f"{len(dropped)} unknown account id{'s' if len(dropped) != 1 else ''} dropped "
                                         f"(not a current account): {shown}")
    return out


def save_settings_for_owner(conn: sqlite3.Connection, data: dict[str, Any], owner: str | None) -> dict[str, Any]:
    """Save legacy browser Settings, then scope its pending summary to the admitted request owner."""
    out = save_settings(conn, data)
    out["mcp"] = mcp_state.summary_for_owner(conn, owner)
    return out


GET_ROUTES: dict[str, Callable[[sqlite3.Connection, dict[str, str], date], Any]] = {
    "/api/overview": overview,
    "/api/transactions": transactions,
    "/api/budgets": budgets_page,
    "/api/recurring": recurring_page,
    "/api/accounts": accounts,
    "/api/categories": categories,
    "/api/category-groups": category_groups,
    "/api/counts": counts,
    "/api/manual": manual_list,
    "/api/manual/match": match_preview,
    "/api/investments": investments_page,
    "/api/debt": debt_page,
    "/api/settings": settings,
}


# --- writes ------------------------------------------------------------------------------------------

def set_transaction_category(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """User decision for one transaction; with make_rule, also an exact merchant (or name) user rule
    that re-files matching rows now (ai guesses included) and on future syncs (user rows are never
    touched by rules)."""
    tid, category, make_rule = data.get("transaction_id"), data.get("category"), data.get("make_rule", False)
    if not isinstance(tid, str) or not tid or not isinstance(category, str) or not category.strip():
        raise ApiError("transaction_id and category are required strings")
    if not isinstance(make_rule, bool):
        raise ApiError("make_rule must be true or false")
    try:
        classify.set_category(conn, tid, category.strip(), source="user")
        changed, rule_id = 1, None
        if make_rule:
            saved = classify.rule_from_transaction(conn, tid, category.strip(), "user")
            rule_id = saved.id
            changed += saved.refiled + classify.apply(conn)["rule"]
    except classify.ClassifyError as e:
        raise ApiError(str(e), 404 if str(e).startswith("unknown transaction") else 400) from None
    row = conn.execute("SELECT category, category_source FROM v_transactions WHERE transaction_id = ?",
                       (tid,)).fetchone()
    rule = conn.execute("SELECT match_field, pattern FROM rules WHERE id = ?", (rule_id,)).fetchone() if rule_id else None
    return {"transaction_id": tid, "category": row["category"], "category_source": row["category_source"],
            "changed": changed, "rule_id": rule_id,
            "rule": {"field": rule["match_field"], "pattern": rule["pattern"]} if rule else None}


def set_cap(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """Set (0..100000) or clear (null) a spending category's monthly cap."""
    category = data.get("category")
    if not isinstance(category, str) or not category.strip():
        raise ApiError("category is required")
    row = conn.execute("SELECT id, name, is_income, is_transfer FROM categories WHERE name = ? COLLATE NOCASE",
                       (category.strip(),)).fetchone()
    if not row:
        raise ApiError(f"unknown category {category!r}")
    if row["is_income"] or row["is_transfer"]:
        raise ApiError(f"{row['name']} is not a spending category; it can't have a cap")
    if "monthly_cap" not in data:
        raise ApiError("monthly_cap is required: a number, or null to clear the cap")
    cap = data["monthly_cap"]
    if cap is not None:
        if isinstance(cap, bool) or not isinstance(cap, (int, float)):
            raise ApiError("monthly_cap must be a number or null")
        if isinstance(cap, int) and abs(cap) > MAX_CAP:  # also a huge int, which math.isfinite can't take
            raise ApiError(f"monthly_cap must be between 0 and {MAX_CAP:,}")
        if not math.isfinite(cap) or not 0 <= cap <= MAX_CAP:
            raise ApiError(f"monthly_cap must be between 0 and {MAX_CAP:,}")
        cap = round(float(cap), 2)
    with conn:
        conn.execute("UPDATE categories SET monthly_cap = ? WHERE id = ?", (cap, row["id"]))
    return {"category": row["name"], "monthly_cap": cap}


def set_group_budget(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """polish P2e: set (0..100000) or clear (null) a category group's monthly budget, checked like set_cap."""
    try:
        return groupbudget.set_budget(conn, data)
    except groupbudget.GroupBudgetError as e:
        raise ApiError(str(e), e.status, e.field) from None


def move_budgets(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """polish P2e: apply the one-time "move budgets to groups" review, all or nothing (groupbudget.apply_move)."""
    try:
        return groupbudget.apply_move(conn, data)
    except groupbudget.GroupBudgetError as e:
        raise ApiError(str(e), e.status, e.field) from None


def _manual_call(fn, *args):
    try:
        return fn(*args)
    except manual.ManualError as e:
        raise ApiError(str(e), e.status, e.field) from None


def manual_create(conn: sqlite3.Connection, data: dict[str, Any], today: date) -> dict[str, Any]:
    """A vehicle / property also gets its VIN decoded and its first estimate here (valuation.create)."""
    return _manual_call(valuation.create, conn, data, today)


def manual_edit(conn: sqlite3.Connection, account_id: int, data: dict[str, Any], today: date) -> dict[str, Any]:
    return _manual_call(valuation.edit, conn, account_id, data, today)


def manual_refresh(conn: sqlite3.Connection, account_id: int, today: date) -> dict[str, Any]:
    """"Refresh estimate": one guarded call, at most one per account per day (it counts toward the monthly cap)."""
    return _manual_call(valuation.refresh, conn, account_id, today)


def manual_use_estimate(conn: sqlite3.Connection, account_id: int, today: date) -> dict[str, Any]:
    return _manual_call(valuation.use_estimate, conn, account_id, today)


def manual_detail(conn: sqlite3.Connection, account_id: int, today: date) -> dict[str, Any]:
    """The car / property detail page's data (TASK-13)."""
    return _manual_call(valuation.detail, conn, account_id, today)


def manual_archive(conn: sqlite3.Connection, account_id: int, today: date) -> dict[str, Any]:
    return _manual_call(manual.archive, conn, account_id, today)
