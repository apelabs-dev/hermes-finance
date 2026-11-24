"""The Reports numbers (0.5): one function per number, shared by the Reports pages, `reads.py` and the agent tools.

The owner's decisions (2026-11-20) are the rules here:

  * q1 refunds net against their category. `category_totals` reports Gross / Refunds / Net per category; a category
    whose net is below zero becomes its own "Refunds over spending" source (`spending_groups`) instead of a negative
    spending width. Refunds are never income; a tax refund stays income (the 0.4.0 rule), so it is an income source.
  * q2 `remaining_after_spending` = income - net spending, exact for any range (no averaging, no projection). Money
    moved between accounts is `moves`: listed, never drawn as income and never counted as spending.
  * q4 the forecast uses the chosen cash accounts, checking first (a savings account only when it is ticked in), the
    bank's available balance; loans, cards and investments are out. An account in another currency is its own line
    and is never added to the display currency.
  * q5 a detected paycheck is not in the line until the owner adds it: `detected_paychecks` lists the candidates and
    one answer per source (`add` / `omit`, kept in the `reports_paychecks` setting) decides it.
  * q6 the everyday-spending estimate is two named lines, "Lower everyday spending" and "Higher everyday spending",
    each with its end balance. Nothing here says "confidence" or shows a percentage: the band is a scenario, not a
    calibrated interval.

The Reports pages (0.5, B2) add one more number set here, not in the web layer: `spending_by_day` and
`year_spending` (the Spending page's year heatmap, its four KPIs and the drill-down, `category_activity`), and the two
preferences the pages save: which accounts the forecast line uses (`reports_forecast_accounts`) and the owner's answer
per detected paycheck (`reports_paychecks`). The pages only draw what these functions return.

Every total is in the display currency (`db.display_currency`), like every other number in the app; another
currency is reported per currency and never summed with it. Spend sign follows the app: money out is positive, a
refund negative; income is the opposite sign and is reported as a positive amount.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from . import billcal, insights, money
from .db import account_policy_sql, display_currency, now

# The everyday-spending band: a lower and a higher scenario around the typical daily rate. Not a confidence
# interval, and never shown as a percentage (q6).
LOWER_FACTOR = 0.75
HIGHER_FACTOR = 1.25
# The typical rate comes from this many complete calendar months before today (the current month is left out: it is
# partial). Fixed bill categories and recurring bills are left out, and they are drawn as their own events.
LOOKBACK_MONTHS = 3
FORECAST_DAYS = 30
MAX_FORECAST_DAYS = 90
# An opening bank balance older than this is called out as stale (the "balance from Nov 16 (3 days ago)" state).
STALE_DAYS = 3
# Detected paychecks: income deposits from this many months back, at a weekly / fortnightly / monthly cadence.
PAYCHECK_LOOKBACK_MONTHS = 6
PAYCHECK_CADENCES = ("weekly", "biweekly", "monthly", "bimonthly")
# One answer per paycheck source: {source key: "add" | "omit"}, a JSON object under this settings key. Nothing here
# writes it: the Reports pages save the owner's answer (no write tools in this card). The reader is bounded so a
# malformed or enormous row can never stall the forecast (security t_cb6c3189).
PAYCHECK_SETTING = "reports_paychecks"
PAYCHECK_SETTING_MAX_BYTES = 20_000
PAYCHECK_SETTING_MAX_ENTRIES = 200
# The accounts the forecast line uses (q4): a JSON list of account ids under this setting. A page tick writes the whole
# list; `forecast()` reads it when `accounts` is None, so the page and the agent tool show the same line.
FORECAST_ACCOUNTS_SETTING = "reports_forecast_accounts"
FORECAST_ACCOUNTS_MAX_BYTES = 5_000
MAX_FORECAST_ACCOUNTS = 20
# The Spending page's year view: 12 calendar months ending with the selected one (the mock's "Oct 2025 - Sep 2026").
YEAR_MONTHS = 12
MAX_YEAR_MONTHS = 24
# The transfers listed by `moves` (and so by `finance_reports_cashflow`): the newest rows in the range, oldest first.
# The count and the total always cover every row; a long range would otherwise send every transfer to the model.
MOVES_LIST_LIMIT = 50

INCOME_LABELS = {"paychecks": "Paychecks", "interest_tax_refunds": "Interest and tax refunds", "other": "Other income"}
# Plaid's income codes: a wage is a paycheck; a tax refund and interest are their own source (q1); the rest is other.
PAYCHECK_CODES = ("INCOME_WAGES",)
INTEREST_CODES = ("INCOME_TAX_REFUND", "INCOME_INTEREST_EARNED")


class ReportError(Exception):
    """A bad argument to a Reports number: `reads.py` turns it into a ReadError (the CLI exits 2)."""


def _round_to(x: float, decimals: int) -> float:
    return round(x + 0.0, decimals)  # + 0.0 turns -0.0 into 0.0


def _r(x: float) -> float:
    """An amount in the display currency: the app-wide 2-decimal payload convention."""
    return _round_to(x, 2)


def _range(start: str | None, end: str | None) -> tuple[list[str], list[Any]]:
    """The inclusive date bounds as SQL over `v.date` (either end may be open), validated as ISO dates."""
    where: list[str] = []
    args: list[Any] = []
    for value, op, flag in ((start, ">=", "start"), (end, "<=", "end")):
        if value is None:
            continue
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ReportError(f"{flag} must be a date as YYYY-MM-DD, got {value!r}") from None
        where.append(f"v.date {op} ?")
        args.append(value)
    if start is not None and end is not None and start > end:
        raise ReportError(f"start {start} is after end {end}")
    return where, args


def _where(where: list[str]) -> str:
    return " AND ".join(where) or "1 = 1"


# --- 1. Gross / Refunds / Net per category (q1) ----------------------------------------------------------------

def _part_columns(line: str) -> str:
    """The count / gross / refunds of each grouped row, each spending line expressed as `line` first."""
    return f"""COUNT(DISTINCT v.transaction_id) AS n,
                          COALESCE(SUM(CASE WHEN v.spend > 0 THEN {line} END), 0) AS gross,
                          COALESCE(SUM(CASE WHEN v.spend < 0 THEN {line} END), 0) AS refunds"""


# The display currency keeps the app-wide convention: every line is rounded to cents before it is summed, so a split's
# rounding never changes a total (and it matches the dashboard's own numbers line for line).
_PART_COLUMNS = _part_columns("ROUND(v.spend, 2)")
# A foreign line is summed raw and rounded once to its own ISO exponent (KWD / BHD / JOD 3, JPY 0, most 2), so a
# 3-decimal currency keeps its third decimal and a 4-fil charge is not dropped as a sub-cent row.
_FOREIGN_PART_COLUMNS = _part_columns("v.spend")


def _part_rows(rows: Iterable[sqlite3.Row], decimals: int = 2) -> list[dict[str, Any]]:
    """The gross / refunds / net of each grouped row (other selected columns are kept), skipping rows that are zero
    in `decimals` places: cents in the display currency, the currency's own minor unit in a foreign table."""
    scale = 10 ** decimals
    out = []
    for r in rows:
        gross, refunds = _round_to(r["gross"], decimals), _round_to(r["refunds"], decimals)
        if not round(gross * scale) and not round(refunds * scale):
            continue
        out.append({**dict(r), "gross": gross, "refunds": refunds, "net": _round_to(gross + refunds, decimals),
                    "count": r["n"]})
    return out


def category_totals(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
                    display: str | None = None) -> dict[str, Any]:
    """Gross / Refunds / Net spending per category for the range (spending = not income, not transfers; refunds net
    against their category). Categories are ordered by net, largest first; `refunds_over` names every category whose
    net is below zero, which the flow shows as its own source. `foreign` is the same table per other currency: it is
    never added to the display currency's totals.

    `display` is the currency already chosen by a caller that reads several aggregates in one report (`money_flow`);
    with None it is read here."""
    display = display or display_currency(conn)
    where, args = _range(start, end)
    rows = conn.execute(
        f"""SELECT v.category_id, v.category, c.group_id, g.name AS group_name, g.rgb AS group_rgb,
                   g.sort AS group_sort, c.icon, {_PART_COLUMNS}
            FROM {insights.LINES} v
            JOIN categories c ON c.id = v.category_id
            LEFT JOIN category_groups g ON g.id = c.group_id
            WHERE {insights.SPENDING} AND {insights.currency_is_sql(display, "v.iso_currency")}
              AND ({_where(where)})
            GROUP BY v.category_id, v.category, c.group_id, g.name, g.rgb, g.sort, c.icon""", args).fetchall()
    out_categories = [{
        "category_id": r["category_id"], "category": r["category"],
        "group": r["group_name"], "group_id": r["group_id"], "group_rgb": r["group_rgb"],
        "group_sort": r["group_sort"], "icon": r["icon"],
        "gross": r["gross"], "refunds": r["refunds"], "net": r["net"], "count": r["count"]}
        for r in _part_rows(rows)]
    total = _r(sum(c["net"] for c in out_categories))
    for c in out_categories:
        c["share"] = _r(c["net"] / total * 100) if total > 0 else None
    out_categories.sort(key=lambda c: (-c["net"], c["category"].casefold()))
    count = conn.execute(
        f"""SELECT COUNT(DISTINCT v.transaction_id) FROM {insights.LINES} v
            WHERE {insights.SPENDING} AND {insights.currency_is_sql(display, "v.iso_currency")}
              AND ({_where(where)})""", args).fetchone()[0]
    totals = {"gross": _r(sum(c["gross"] for c in out_categories)),
              "refunds": _r(sum(c["refunds"] for c in out_categories)),
              "net": total, "count": count}
    return {"currency": display, "start": start, "end": end,
            "categories": out_categories,
            "refunds_over": [{"category": c["category"], "category_id": c["category_id"], "group": c["group"],
                              "label": f"Refunds over spending · {c['category']}", "amount": _r(-c["net"]),
                              "count": c["count"]} for c in out_categories if c["net"] < 0],
            "totals": totals, "foreign": _foreign_totals(conn, where, args, display)}


def _foreign_totals(conn: sqlite3.Connection, where: list[str], args: list[Any], display: str) -> list[dict[str, Any]]:
    """The same Gross / Refunds / Net per category, for each currency that is not the display one, grouped by
    currency. Nothing here is converted or added to the display currency. Each currency's amounts are rounded to its
    own ISO minor unit (`money.exponent`), not to cents: KWD / BHD / JOD keep their third decimal."""
    rows = conn.execute(
        f"""SELECT UPPER(TRIM(v.iso_currency)) AS currency, v.category_id, v.category, {_FOREIGN_PART_COLUMNS}
            FROM {insights.LINES} v
            WHERE {insights.SPENDING} AND {insights.currency_is_sql(display, "v.iso_currency", other=True)}
              AND ({_where(where)})
            GROUP BY currency, v.category_id, v.category ORDER BY currency, v.category""", args).fetchall()
    by_currency_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_currency_rows[r["currency"]].append(r)
    out = []
    for code, currency_rows in by_currency_rows.items():
        decimals = money.exponent(code)
        cats = [{"category": r["category"], "category_id": r["category_id"],
                 "gross": r["gross"], "refunds": r["refunds"], "net": r["net"], "count": r["count"]}
                for r in _part_rows(currency_rows, decimals)]
        cats.sort(key=lambda c: (-c["net"], c["category"].casefold()))
        out.append({"currency": code, "categories": cats,
                    "totals": {"gross": _round_to(sum(c["gross"] for c in cats), decimals),
                               "refunds": _round_to(sum(c["refunds"] for c in cats), decimals),
                               "net": _round_to(sum(c["net"] for c in cats), decimals),
                               "count": sum(c["count"] for c in cats)}})
    return out


# --- 2. Spending groups (the flow's uses) ----------------------------------------------------------------------

def _groups(totals: dict[str, Any]) -> dict[str, Any]:
    """`spending_groups` for an already-read `category_totals`: the positive categories summed per group, and the
    negative ones as "Refunds over spending" sources. Deriving both sides from the one snapshot is what keeps
    `money_in - money_out == remaining` in `money_flow`."""
    groups: dict[int, dict[str, Any]] = {}
    refunds_over: list[dict[str, Any]] = []
    for c in totals["categories"]:
        if c["net"] < 0:
            refunds_over.append({"category": c["category"], "category_id": c["category_id"], "group": c["group"],
                                 "label": f"Refunds over spending · {c['category']}", "amount": _r(-c["net"]),
                                 "count": c["count"]})
        elif c["net"] > 0:
            key = c["group_id"] or 0
            g = groups.setdefault(key, {"group": c["group"] or "Other", "group_id": c["group_id"],
                                        "rgb": c["group_rgb"], "sort": c["group_sort"] or 99,
                                        "net": 0.0, "count": 0, "categories": []})
            g["net"] = _r(g["net"] + c["net"])
            g["count"] += c["count"]
            g["categories"].append(c["category"])
    out = sorted(groups.values(), key=lambda g: (-g["net"], g["sort"], g["group"].casefold()))
    total = _r(sum(g["net"] for g in out))
    for g in out:
        g["share"] = _r(g["net"] / total * 100) if total > 0 else None
    return {"groups": out, "refunds_over": refunds_over, "net": totals["totals"]["net"]}


def spending_groups(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
                    display: str | None = None) -> dict[str, Any]:
    """The categories that net positive, summed per category group (the flow's spending side), and the categories
    that net below zero as separate "Refunds over spending" sources. A group's net is the sum of its positive
    categories only, so `net` - the sum of the refunds -over sources equals the range's net spending exactly."""
    return _groups(category_totals(conn, start, end, display))


# --- 3. Income sources and moves (q1, q2) ----------------------------------------------------------------------

def _income_source(detailed: str | None) -> str:
    code = (detailed or "").strip().upper()
    if code in PAYCHECK_CODES:
        return "paychecks"
    if code in INTEREST_CODES:
        return "interest_tax_refunds"
    return "other"


def income_sources(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
                   display: str | None = None) -> dict[str, Any]:
    """Money in, by source (Paychecks, Interest and tax refunds, Other income), as positive amounts. Refunds of
    spending are not here: they net against their category (q1); a tax refund is income and is here. `display` is as
    `category_totals`'s: read here unless a report already chose it."""
    display = display or display_currency(conn)
    where, args = _range(start, end)
    rows = conn.execute(
        f"""SELECT v.transaction_id, v.amount, v.pfc_detailed
            FROM v_transactions v
            WHERE v.is_income = 1 AND {insights.currency_is_sql(display, "v.iso_currency")}
              AND ({_where(where)})""", args).fetchall()
    buckets: dict[str, dict[str, Any]] = {k: {"amount": 0.0, "count": 0} for k in INCOME_LABELS}
    for r in rows:
        bucket = buckets[_income_source(r["pfc_detailed"])]
        bucket["amount"] = _r(bucket["amount"] - r["amount"])
        bucket["count"] += 1
    total = _r(sum(b["amount"] for b in buckets.values()))
    sources = [{"source": k, "label": INCOME_LABELS[k], "amount": b["amount"], "count": b["count"],
                "share": _r(b["amount"] / total * 100) if total > 0 else None}
               for k, b in buckets.items() if round(b["amount"] * 100) or b["count"]]
    sources.sort(key=lambda s: (-s["amount"], s["label"].casefold()))
    return {"currency": display, "start": start, "end": end, "total": total, "sources": sources}


def moves(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
          display: str | None = None) -> dict[str, Any]:
    """Money moved between the owner's own accounts (transfers and card payments), as money-out rows. It is neither
    income nor spending, so the flow lists it under the diagram instead of drawing it; the counterpart inflow of a
    transfer is not repeated here.

    So a long range can never send every transfer to a model, `rows` is only the newest `MOVES_LIST_LIMIT`, oldest
    first (the order the cards and the CLI have always used). `count` and `total` cover every row in the range,
    `shown` is how many of them are listed, `truncated` says the list is not all of them, and `note` is the sentence
    to show when it is. The count, the total and the rows come from one statement, and each amount is rounded once
    (`ROUND(v.amount, 2)`, the same value the window SUM adds), so the listed rows always add up to the total
    (security t_b521d761 L2)."""
    display = display or display_currency(conn)
    where, args = _range(start, end)
    rows = conn.execute(
        f"""SELECT v.date, COALESCE(NULLIF(TRIM(v.merchant_name), ''), TRIM(v.name), '') AS label,
                   ROUND(v.amount, 2) AS amount, v.account_id,
                   COUNT(*) OVER () AS n, COALESCE(SUM(ROUND(v.amount, 2)) OVER (), 0) AS total
            FROM v_transactions v
            WHERE v.is_transfer = 1 AND v.amount > 0 AND {insights.currency_is_sql(display, "v.iso_currency")}
              AND ({_where(where)})
            ORDER BY v.date DESC, v.transaction_id DESC LIMIT ?""", [*args, MOVES_LIST_LIMIT]).fetchall()
    out = [{"date": r["date"], "label": r["label"].strip() or "(no name)", "amount": _r(r["amount"]),
            "account_id": r["account_id"]} for r in reversed(rows)]     # newest first from SQL, oldest first out
    count = rows[0]["n"] if rows else 0
    total = _r(rows[0]["total"]) if rows else 0.0
    truncated = count > len(out)
    return {"currency": display, "rows": out, "shown": len(out), "count": count,
            "total": total, "limit": MOVES_LIST_LIMIT, "truncated": truncated,
            "note": (f"the newest {len(out)} of {count} moves are listed; the count and the total cover all "
                     f"{count}") if truncated else None}


def remaining_after_spending(conn: sqlite3.Connection, start: str | None = None,
                             end: str | None = None) -> dict[str, Any]:
    """Income minus net spending for the range, exactly (q2). Negative is a shortfall (`short`), never clamped to
    zero; `share` is how much of the income is left, a percentage (income 0: null). Both aggregates are read in the
    one display currency this report chose."""
    display = display_currency(conn)
    income = income_sources(conn, start, end, display)
    spending = category_totals(conn, start, end, display)["totals"]
    remaining = _r(income["total"] - spending["net"])
    return {"currency": display, "start": start, "end": end,
            "income": income["total"], "spending": spending["net"], "remaining": remaining,
            "short": remaining < 0,
            "share": _r(remaining / income["total"] * 100) if income["total"] > 0 else None}


def money_flow(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """The Cash flow page's numbers: money in (income sources plus every "Refunds over spending" source), the
    spending groups, and what is left after spending. `money_in - money_out` equals `remaining`, and `remaining`
    equals `remaining_after_spending` for the same range. The moves between accounts are listed in `moves`.

    The whole report is one snapshot: the display currency is read once, and income and spending are each read once,
    with the groups, the refunds-over total and the remaining all derived from those reads. Reading the same
    aggregate several times let a sync landing between two SELECTs make the flow's own numbers disagree."""
    display = display_currency(conn)
    totals = category_totals(conn, start, end, display)
    income = income_sources(conn, start, end, display)
    groups = _groups(totals)
    over_total = _r(sum(r["amount"] for r in groups["refunds_over"]))
    money_out = _r(sum(g["net"] for g in groups["groups"]))
    money_in = _r(income["total"] + over_total)
    remaining = _r(income["total"] - totals["totals"]["net"])
    return {"currency": display, "start": start, "end": end,
            "income": income, "spending": totals["totals"],
            "groups": groups["groups"], "refunds_over": groups["refunds_over"],
            "money_in": money_in, "money_out": money_out,
            "remaining": remaining,
            "remaining_share": _r(remaining / income["total"] * 100) if income["total"] > 0 else None,
            "short": remaining < 0,
            "moves": moves(conn, start, end, display)}


# --- 4. The forecast's accounts (q4) ---------------------------------------------------------------------------

def _kind(subtype: str | None) -> str:
    s = (subtype or "").strip().lower()
    if "check" in s:
        return "checking"
    if "saving" in s:
        return "savings"
    return s or "depository"


def forecast_accounts(conn: sqlite3.Connection) -> dict[str, Any]:
    """The accounts the forecast can use: the live depository accounts in the display currency that report an
    available balance, checking first then savings. Checking is ticked by default; savings can be ticked in (q4).
    Savings without an available balance are listed as unavailable; loans, cards and investments are out; another
    currency is its own line and is never part of the display-currency opening balance."""
    display = display_currency(conn)
    rows = conn.execute(
        f"""SELECT a.account_id, a.name, a.mask, a.type, a.subtype, a.current_balance, a.available_balance,
                   a.updated_at, UPPER(COALESCE(NULLIF(TRIM(a.iso_currency), ''), ?)) AS currency,
                   i.institution_name
            FROM accounts a JOIN items i ON i.item_id = a.item_id
            WHERE i.removed_at IS NULL AND {account_policy_sql("a")}
            ORDER BY a.name, a.account_id""", (display,)).fetchall()
    accounts, unavailable, excluded = [], [], []
    other: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        row = {"account_id": r["account_id"], "name": r["name"], "mask": r["mask"],
               "institution": r["institution_name"], "currency": r["currency"],
               "available": None if r["available_balance"] is None else _r(r["available_balance"]),
               "current": None if r["current_balance"] is None else _r(r["current_balance"]),
               "updated_at": r["updated_at"]}
        if (r["type"] or "").lower() != "depository":
            excluded.append({"account_id": r["account_id"], "type": r["type"],
                             "reason": "loans and investments are out" if (r["type"] or "").lower() in ("loan", "investment")
                             else "cards and debts are out"})
            continue
        if r["currency"] != display:
            other[r["currency"]].append({k: row[k] for k in ("account_id", "name", "mask", "institution", "available")})
            continue
        if row["available"] is None:
            unavailable.append({k: row[k] for k in ("account_id", "name", "mask", "institution")}
                               | {"reason": "no available balance from the bank"})
            continue
        accounts.append({**row, "kind": _kind(r["subtype"]), "ticked": _kind(r["subtype"]) == "checking"})
    accounts.sort(key=lambda a: (not a["ticked"], {"checking": 0, "savings": 1}.get(a["kind"], 2),
                                 (a["name"] or "").casefold(), a["account_id"]))
    return {
        "currency": display,
        "accounts": accounts,
        "default": [a["account_id"] for a in accounts if a["ticked"]],
        "opening": _r(sum(a["available"] for a in accounts if a["ticked"])),
        "unavailable": unavailable,
        "excluded": excluded,
        "other_currencies": [{"currency": code, "available": _r(sum(a["available"] or 0.0 for a in accts)),
                              "accounts": accts} for code, accts in sorted(other.items())],
    }


def selected_accounts(table: dict[str, Any], accounts: Iterable[str] | None) -> list[dict[str, Any]]:
    """The forecast's chosen accounts: the ids given (only cash accounts with an available balance qualify) or, with
    `accounts` None, the default checking ones. Savings accounts are in the table but are only chosen explicitly."""
    eligible = {a["account_id"]: a for a in table["accounts"]}
    ids = list(table["default"]) if accounts is None else [x for x in accounts if x in eligible]
    return [eligible[i] for i in dict.fromkeys(ids)]


# --- 5. Detected paychecks (q5) --------------------------------------------------------------------------------

def _paycheck_key(name: str | None, merchant: str | None) -> str:
    """A description without dates and reference numbers, lowercased: the label part of a paycheck source key. Two
    deposits of the same paycheck share it from month to month ("Electronic Deposit 11/10 246810 Orbitex L" ->
    "electronic deposit orbitex l")."""
    base = (merchant or name or "").strip().casefold()
    base = re.sub(r"[0-9]+", " ", base)
    base = re.sub(r"[^a-z& ]+", " ", base)
    return " ".join(base.split())


def _paycheck_source_key(key: str, account_id: str, currency: str) -> str:
    """The saved answer's key: the normalized label bound to the account and currency it was seen on. A source that
    moves to another account, or a different account paying the same label, is a new candidate and waits again
    (`answer` is None) instead of inheriting an old Add (security t_cb6c3189)."""
    return f"{account_id}|{currency}|{key}"


def _paycheck_answers(conn: sqlite3.Connection) -> tuple[dict[str, str], int]:
    """(the owner's answers, how many valid answers were over the entry cap) from the `reports_paychecks` setting. A
    missing, malformed or non-object value reads as no answers (every detected source is still waiting). The raw
    value is capped in size by SQLite itself (`CASE`/`LENGTH`), so an oversized row never becomes a Python string: at
    most `PAYCHECK_SETTING_MAX_BYTES` characters (the writer's `json.dumps` is ASCII, so that is the same byte count
    the writer measures; a stored size in bytes would read every answer of a UTF-16 database as none: security
    t_b521d761 L1) and at most four times that in bytes, because `LENGTH` on TEXT stops at the first NUL and counts an
    invalid `\\xC0` + continuation run as one character, so the character bound alone lets a crafted row through
    (security t_cec35e94 L1). A value too deep for the JSON decoder is refused, so a bad row can never stall the
    forecast. Entries are validated before the 200-entry cap applies: junk entries must not push a real Add (or Omit)
    out of the map."""
    try:
        row = conn.execute(
            "SELECT CASE WHEN LENGTH(value) <= ? AND LENGTH(CAST(value AS BLOB)) <= 4 * ? THEN value END AS value "
            "FROM settings WHERE key = ?",
            (PAYCHECK_SETTING_MAX_BYTES, PAYCHECK_SETTING_MAX_BYTES, PAYCHECK_SETTING)).fetchone()
    except sqlite3.Error:
        return {}, 0
    if row is None or not isinstance(row[0], str):
        return {}, 0
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError, RecursionError):
        return {}, 0
    if not isinstance(value, dict):
        return {}, 0
    valid: dict[str, str] = {}
    for k, v in value.items():
        if isinstance(k, str) and isinstance(v, str):
            key, answer = k.strip(), v.strip()
            if key and answer in ("add", "omit"):
                valid[key] = answer
    answers = dict(list(valid.items())[:PAYCHECK_SETTING_MAX_ENTRIES])
    return answers, len(valid) - len(answers)


def paycheck_answers(conn: sqlite3.Connection) -> dict[str, str]:
    """The owner's answers per paycheck source: {source key: "add" | "omit"}. See `_paycheck_answers` for the bounds;
    the overflow count (valid answers past the cap, not read) is on `_paycheck_answers`, which the forecast surfaces."""
    return _paycheck_answers(conn)[0]


def forecast_account_choice(conn: sqlite3.Connection) -> list[str] | None:
    """The account ids the owner ticked for the forecast line (q4), from `reports_forecast_accounts`. None when nothing
    is saved yet (the default: the checking accounts), and `[]` when the owner unticked everything (an empty line, not
    the default coming back). A malformed, oversized or non-list value reads as None; the size check is SQLite's: at
    most `FORECAST_ACCOUNTS_MAX_BYTES` characters (the writer's `json.dumps` is ASCII, so that is the byte count it
    measures) and at most four times that in bytes, since `LENGTH` on TEXT stops at the first NUL and undercounts an
    invalid byte run, so an oversized row never becomes a Python string (security t_cec35e94 L1)."""
    try:
        row = conn.execute(
            "SELECT CASE WHEN LENGTH(value) <= ? AND LENGTH(CAST(value AS BLOB)) <= 4 * ? THEN value END AS value "
            "FROM settings WHERE key = ?",
            (FORECAST_ACCOUNTS_MAX_BYTES, FORECAST_ACCOUNTS_MAX_BYTES, FORECAST_ACCOUNTS_SETTING)).fetchone()
    except sqlite3.Error:
        return None
    if row is None or not isinstance(row[0], str):
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for x in value[:MAX_FORECAST_ACCOUNTS]:
        if isinstance(x, str) and x.strip() and x.strip() not in out:
            out.append(x.strip())
    return out


def save_forecast_accounts(conn: sqlite3.Connection, accounts: Iterable[str]) -> dict[str, Any]:
    """Save the forecast's chosen accounts, keeping only eligible ones (a live cash account with an available balance in
    the display currency) and at most `MAX_FORECAST_ACCOUNTS`. The ids that were dropped come back in `dropped`."""
    table = forecast_accounts(conn)
    eligible = [a["account_id"] for a in table["accounts"]]
    given = [x.strip() for x in accounts if isinstance(x, str) and x.strip()]
    clean = [x for x in dict.fromkeys(given) if x in eligible][:MAX_FORECAST_ACCOUNTS]
    dropped = [x for x in dict.fromkeys(given) if x not in eligible]
    _write_setting(conn, FORECAST_ACCOUNTS_SETTING, json.dumps(clean))
    return {"accounts": clean, "dropped": dropped}


def _paycheck_answers_size(answers: Mapping[str, str]) -> int:
    """The stored value's size in bytes, from the same `json.dumps(answers, sort_keys=True)` the writer stores, so it
    is the row's `LENGTH(CAST(value AS BLOB))` (the writer must refuse a map the reader would refuse, or a save
    silently wipes every answer: security t_b8a0089f L1). The reader also bounds the row at four times the character
    cap in bytes; this writer's ASCII stops at the character cap, so a saved map is always inside both bounds."""
    return len(json.dumps(answers, sort_keys=True).encode("utf-8"))


def clean_paycheck_answers(answers: Mapping[str, Any]) -> dict[str, str]:
    """The canonical answer map: source keys as given (stripped) and exactly `add` / `omit`, at most
    `PAYCHECK_SETTING_MAX_ENTRIES` entries and at most `PAYCHECK_SETTING_MAX_BYTES` once serialized (the reader's cap).
    Raises ReportError for anything else."""
    if not isinstance(answers, Mapping):
        raise ReportError("reports_paychecks must be an object of source key to add / omit")
    clean: dict[str, str] = {}
    for k, v in answers.items():
        if not isinstance(k, str) or not k.strip() or len(k) > 200:
            raise ReportError("a reports_paychecks key must be a paycheck source key")
        if v not in ("add", "omit"):
            raise ReportError("a reports_paychecks answer must be add or omit")
        if k.strip() not in clean and len(clean) >= PAYCHECK_SETTING_MAX_ENTRIES:
            raise ReportError(f"at most {PAYCHECK_SETTING_MAX_ENTRIES} paycheck answers are kept")
        clean[k.strip()] = v
    if _paycheck_answers_size(clean) > PAYCHECK_SETTING_MAX_BYTES:
        raise ReportError(f"at most {PAYCHECK_SETTING_MAX_BYTES} bytes of paycheck answers are kept; "
                          "leave an old answer out first")
    return clean


def save_paycheck_answers(conn: sqlite3.Connection, answers: Mapping[str, Any]) -> dict[str, str]:
    """Save the whole answer map (the Settings write). `clean_paycheck_answers` does the checks, so a bad page can never
    write a value the forecast reader has to guess about."""
    clean = clean_paycheck_answers(answers)
    _write_setting(conn, PAYCHECK_SETTING, json.dumps(clean, sort_keys=True))
    return clean


def save_paycheck_answer(conn: sqlite3.Connection, key: str, answer: str | None) -> dict[str, str]:
    """Save the owner's answer for one detected paycheck source (q5): `add` / `omit`, or None to clear it (the source
    waits again). Keys are the source keys `detected_paychecks` returns (account | currency | label), never a bare
    label; the stored tokens are exactly `add` / `omit`."""
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ReportError("key must be a paycheck source key")
    if answer not in ("add", "omit", None):
        raise ReportError("answer must be add, omit or null")
    answers = paycheck_answers(conn)
    if answer is None:
        answers.pop(key.strip(), None)
    else:
        if key.strip() not in answers and len(answers) >= PAYCHECK_SETTING_MAX_ENTRIES:
            raise ReportError(f"at most {PAYCHECK_SETTING_MAX_ENTRIES} paycheck answers are kept")
        answers[key.strip()] = answer
    clean = clean_paycheck_answers(answers)      # the same entry and byte caps the reader enforces
    _write_setting(conn, PAYCHECK_SETTING, json.dumps(clean, sort_keys=True))
    return clean


def _write_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (key, value, now()))


def detected_paychecks(conn: sqlite3.Connection, today: date,
                       answers: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Recurring income deposits that look like a paycheck, one candidate per source. A paycheck is a candidate only
    for the forecast once the owner answers it (q5): `answer` is the saved "add" / "omit", else None. Until then it is
    not in the line. Amounts are positive medians of the last three deposits; `next_expected` is the last deposit
    plus the detected cadence."""
    display = display_currency(conn)
    answers = dict(answers or {})
    since = f"{insights.add_months(insights.month_of(today), -PAYCHECK_LOOKBACK_MONTHS)}-01"
    rows = conn.execute(
        f"""SELECT v.transaction_id, v.date, v.name, v.merchant_name, v.amount, v.account_id
            FROM v_transactions v
            WHERE v.is_income = 1 AND {insights.currency_is_sql(display, "v.iso_currency")}
              AND v.date >= ? AND v.date <= ?
            ORDER BY v.date, v.transaction_id""", (since, today.isoformat())).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key = _paycheck_key(r["name"], r["merchant_name"])
        if key:
            groups[(key, r["account_id"])].append(r)
    sources = []
    for (key, account_id), deposits in groups.items():
        if len(deposits) < 3:     # like insights.recurring: 3+ deposits for weekly / every 2 weeks / monthly
            continue
        amounts = [_r(-d["amount"]) for d in deposits]
        if any(a <= 0 for a in amounts) or not insights._stable(amounts):
            continue
        days = [date.fromisoformat(d["date"]) for d in deposits]
        gaps = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0]
        if not gaps:
            continue
        interval = statistics.median(gaps)
        cadence = next((c for c in PAYCHECK_CADENCES
                        if insights.CADENCE_RANGE[c][0] <= interval <= insights.CADENCE_RANGE[c][1]), None)
        if cadence is None:
            continue
        last = days[-1]
        nxt = last + timedelta(days=insights.NOMINAL_DAYS[cadence])
        if (today - nxt).days > insights.NOMINAL_DAYS[cadence]:   # missed a whole extra cycle: it stopped
            continue
        latest = deposits[-1]
        sources.append({
            "key": _paycheck_source_key(key, account_id, display),
            "key_label": key,
            "label": ((latest["merchant_name"] or latest["name"]) or "").strip() or key,
            "amount": _r(statistics.median(amounts[-3:])),
            "cadence": cadence, "cadence_label": insights.CADENCE_LABELS[cadence],
            "last_date": last.isoformat(), "next_expected": nxt.isoformat(),
            "days_until": (nxt - today).days, "count": len(deposits),
            "account_id": account_id,
            "answer": answers.get(_paycheck_source_key(key, account_id, display)),
        })
    sources.sort(key=lambda s: (s["next_expected"], -s["amount"], s["key"]))
    waiting = [s for s in sources if s["answer"] is None]
    return {"today": today.isoformat(), "currency": display, "count": len(sources),
            "waiting": len(waiting), "sources": sources}


# --- 6. The forecast (q4, q5, q6) ------------------------------------------------------------------------------

def _everyday(conn: sqlite3.Connection, today: date, rec: dict[str, Any]) -> dict[str, Any]:
    """The typical everyday spending: net spending over the last complete calendar months, leaving out the fixed
    bill categories and every recurring bill's charges (those are drawn as events), as a daily rate. `lower` and
    `higher` are the two named scenario rates (q6)."""
    display = display_currency(conn)
    months = [insights.add_months(insights.month_of(today), -i) for i in range(LOOKBACK_MONTHS, 0, -1)]
    start = date.fromisoformat(f"{months[0]}-01")
    end = date.fromisoformat(f"{months[-1]}-{insights.days_in_month(months[-1]):02d}")
    bills = {k for item in rec["items"] for k in item.get("keys", [item["key"]])}
    fixed = set(insights.FIXED_CATEGORIES)
    rows = conn.execute(
        f"""SELECT v.spend, v.category, v.merchant_name, v.name FROM {insights.LINES} v
            WHERE {insights.SPENDING} AND {insights.currency_sql(conn)} AND v.date >= ? AND v.date <= ?""",
        (start.isoformat(), end.isoformat())).fetchall()
    total, bill_charges = 0.0, 0
    for r in rows:
        if r["category"] in fixed:
            continue
        if insights.merchant_key(r["merchant_name"], r["name"]) in bills:
            bill_charges += 1
            continue
        total += r["spend"]
    days = (end - start).days + 1
    rate = _r(total / days) if days else 0.0
    first = conn.execute("SELECT MIN(v.date) FROM v_transactions v").fetchone()[0]
    return {"start": start.isoformat(), "end": end.isoformat(), "days": days,
            "total": _r(total), "rate": rate, "lower": _r(rate * LOWER_FACTOR), "higher": _r(rate * HIGHER_FACTOR),
            "coverage": "partial" if first and first > start.isoformat() else "full",
            "history_from": first,
            "excluded": {"fixed_categories": sorted(fixed), "bill_charges": bill_charges, "bills": len(bills)}}


def _line(opening: float, events: list[dict[str, Any]], rate: float, start: date, end: date) -> dict[str, Any]:
    """A daily balance path: the opening balance, plus each day's events, minus one day of everyday spending. The
    points are end-of-day balances."""
    by_day: dict[str, float] = defaultdict(float)
    for e in events:
        by_day[e["date"]] += e["amount"]
    days = (end - start).days + 1
    balance, points = opening, []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        balance += by_day.get(d, 0.0)
        balance -= rate
        points.append({"date": d, "balance": _r(balance)})
    lowest = min(points, key=lambda p: (p["balance"], p["date"]))
    return {"daily": rate, "points": points, "end": points[-1]["balance"], "lowest": dict(lowest)}


def _as_of(conn: sqlite3.Connection, account: dict[str, Any]) -> str | None:
    """When the bank's balance is from: the latest balance snapshot for the account, else the local date of the
    account row's last update."""
    row = conn.execute("SELECT MAX(date) FROM balance_snapshots WHERE account_id = ?",
                       (account["account_id"],)).fetchone()
    if row and row[0]:
        return row[0]
    return insights.local_day(account["updated_at"])


def _answers(paychecks: Mapping[str, str] | Iterable[str] | None,
             conn: sqlite3.Connection) -> tuple[dict[str, str], int]:
    """(the answer map to use, how many saved answers were over the reader's entry cap). The overflow is 0 unless the
    settings were read (`paychecks` None); it is surfaced in the forecast instead of those sources waiting silently."""
    if paychecks is None:
        return _paycheck_answers(conn)
    if isinstance(paychecks, Mapping):
        return {k: v for k, v in paychecks.items() if isinstance(k, str) and v in ("add", "omit")}, 0
    return {k: "add" for k in paychecks if isinstance(k, str)}, 0


def _in_window(item: dict[str, Any], start: date, end: date, today: date) -> list[dict[str, Any]]:
    """One recurring item's unpaid charges in the window, taken from the window itself (as the Bills calendar draws
    it). An item whose expected date is before the window is late: it is charged once, today, and its later cycles
    are projected from the window start — its missed cycles before today are history, not one charge per cycle
    landing on day one."""
    occs = billcal.occurrences(item, start, end, today)
    nxt = date.fromisoformat(item["next_expected"])
    if nxt < start:
        occs = [{"date": nxt.isoformat(), "state": "late", "projected": False}] + occs
    return [o for o in occs if o["state"] != "paid"]


def forecast(conn: sqlite3.Connection, today: date, days: int = FORECAST_DAYS,
             accounts: Iterable[str] | None = None, paychecks: Mapping[str, str] | Iterable[str] | None = None,
             rec: dict[str, Any] | None = None) -> dict[str, Any]:
    """The next `days` days of available balance on the chosen accounts.

    Opening = the bank's available balance of the chosen accounts (q4: checking by default, savings when ticked,
    loans / cards / investments out, another currency on its own line). Events = the bills expected in the window
    plus the paychecks the owner added (q5: a detected one is only listed, never drawn). Everyday spending is one
    daily rate from the last complete months, drawn three ways: the typical "expected" line and the two named
    "Lower everyday spending" / "Higher everyday spending" lines, each with its end balance (q6)."""
    if isinstance(days, bool) or not isinstance(days, int):
        raise ReportError("days must be a whole number")
    if not 1 <= days <= MAX_FORECAST_DAYS:
        raise ReportError(f"days must be between 1 and {MAX_FORECAST_DAYS}, got {days}")
    table = forecast_accounts(conn)
    chosen = selected_accounts(table, accounts if accounts is not None else forecast_account_choice(conn))
    opening = _r(sum(a["available"] for a in chosen))
    start, end = today, today + timedelta(days=days - 1)
    rec = rec if rec is not None else insights.recurring(conn, today)
    display = display_currency(conn)
    warnings: list[str] = []

    # bills: every expected charge in the window; a late bill (its expected date has passed and it hasn't been
    # charged) is counted once today, and the rest are projected from the window start
    events: list[dict[str, Any]] = []
    for item in rec["items"]:
        for occ in _in_window(item, start, end, today):
            when = date.fromisoformat(occ["date"])
            events.append({"date": max(when, start).isoformat(), "label": item["merchant"], "kind": item["kind"],
                           "amount": _r(-abs(item["amount"])), "category": item["category"],
                           "variable": bool(item["variable"]), "source": item["source"], "confirmed": True,
                           "overdue": when < start})

    # paychecks: only the sources the owner added (q5), and only when they land in an account this line covers: a
    # paycheck into an unticked savings account never reaches the checking balance, so drawing it would overstate the
    # line. It is listed with its account and a warning instead (reviewer question on t_4b666ab1).
    answers, answers_dropped = _answers(paychecks, conn)
    found = detected_paychecks(conn, today, answers)
    names = {a["account_id"]: a["name"] for a in table["accounts"]}
    names.update({a["account_id"]: a["name"] for a in table["unavailable"]})
    chosen_ids = {a["account_id"] for a in chosen}
    included, excluded = [], []
    for src in found["sources"]:
        src["account_name"] = names.get(src["account_id"])
        if answers.get(src["key"]) != "add":
            continue
        if src["account_id"] not in chosen_ids:
            excluded.append({k: src[k] for k in ("key", "label", "amount", "cadence", "cadence_label", "last_date",
                                                  "account_id", "account_name")})
            warnings.append(f"{src['label']} pays into {src['account_name'] or src['account_id']}, which is not in "
                            "this line; it stays out until you add the account")
            continue
        included.append(src["key"])
        item = {"last_date": src["last_date"], "next_expected": src["next_expected"], "cadence": src["cadence"],
                "status": "ok"}
        for occ in _in_window(item, start, end, today):
            when = date.fromisoformat(occ["date"])
            events.append({"date": max(when, start).isoformat(), "label": src["label"], "kind": "paycheck",
                           "amount": _r(src["amount"]), "category": None, "variable": False, "source": src["key"],
                           "confirmed": True, "overdue": when < start})
    events.sort(key=lambda e: (e["date"], e["label"].casefold()))

    everyday = _everyday(conn, today, rec)
    if not everyday["total"]:
        warnings.append("no everyday spending in the last "
                        f"{LOOKBACK_MONTHS} complete months, so the lines leave it at 0")
    elif everyday["coverage"] == "partial":
        warnings.append(f"everyday spending only has history from {everyday['history_from']}")

    # the opening balance's freshness
    dated = []
    for a in chosen:
        as_of = _as_of(conn, a)
        stale_days = (today - date.fromisoformat(as_of)).days if as_of else None
        dated.append({**{k: a[k] for k in ("account_id", "name", "mask", "available")},
                      "as_of": as_of, "stale_days": stale_days})
        if as_of and stale_days is not None and stale_days >= STALE_DAYS:
            warnings.append(f"{a['name'] or a['account_id']}: bank balance from {as_of} ({stale_days} days ago)")
    as_ofs = [d["as_of"] for d in dated if d["as_of"]]
    stale_days = max([d["stale_days"] or 0 for d in dated], default=0)
    opening_block = {"balance": opening, "as_of": min(as_ofs) if as_ofs else None, "stale_days": stale_days,
                     "stale": stale_days >= STALE_DAYS, "accounts": dated}
    if not chosen:
        warnings.append("no cash account is in the line yet")
    for a in table["unavailable"]:
        warnings.append(f"{a['name'] or a['account_id']}: no available balance from the bank, left out")
    for cur in table["other_currencies"]:
        warnings.append(f"{cur['currency']}: {len(cur['accounts'])} account(s) have their own line and are never "
                        "added to the " + display + " balance")
    for src in found["sources"]:
        if src["answer"] is None:
            warnings.append(f"possible paycheck: {src['label']} pays about {_r(src['amount'])} "
                            f"{src['cadence_label']} (last {src['last_date']}); it is not in the line until you add it")
    if answers_dropped:
        one = answers_dropped == 1
        warnings.append(f"{answers_dropped} saved paycheck {'answer is' if one else 'answers are'} over the "
                        f"{PAYCHECK_SETTING_MAX_ENTRIES}-answer limit and {'was' if one else 'were'} not read; "
                        "clear an old answer first")

    lines = {
        "lower": {"label": "Lower everyday spending", **_line(opening, events, everyday["lower"], start, end)},
        "expected": {"label": "Expected", **_line(opening, events, everyday["rate"], start, end)},
        "higher": {"label": "Higher everyday spending", **_line(opening, events, everyday["higher"], start, end)},
    }
    expected_in = _r(sum(e["amount"] for e in events if e["amount"] > 0))
    expected_out = _r(-sum(e["amount"] for e in events if e["amount"] < 0) + everyday["rate"] * days)
    expected = {"in": expected_in, "out": expected_out, "net": _r(expected_in - expected_out)}
    # the bill events' own total (the "Biggest upcoming bills" footer). `expected.out` is larger: it also counts the
    # everyday-spending rate over the horizon.
    bills_total = _r(-sum(e["amount"] for e in events if e["amount"] < 0))
    return {
        "today": today.isoformat(),
        "horizon": {"days": days, "start": start.isoformat(), "end": end.isoformat()},
        "currency": display,
        "accounts": chosen,
        "opening": opening_block,
        "everyday": everyday,
        "events": events,
        "paychecks": {"detected": found["sources"], "included": included, "excluded_by_account": excluded,
                      "answers_dropped": answers_dropped},
        "expected": expected,
        "bills_total": bills_total,
        "lines": lines,
        "available": lines["expected"]["end"],
        "available_date": end.isoformat(),
        "lowest": lines["expected"]["lowest"],
        "other_currencies": table["other_currencies"],
        "unavailable_accounts": table["unavailable"],
        "warnings": warnings,
    }


# --- 7. The Spending page's year view (B2) ---------------------------------------------------------------------

def spending_by_day(conn: sqlite3.Connection, start: str | None = None,
                    end: str | None = None) -> dict[str, Any]:
    """Net spending per day, per category, for the range. It is the same definition as `category_totals` (spending in
    the display currency, a refund negative), so every day cell sums exactly to that month's `totals.net`; the year view
    builds its month cells from this one series instead of re-running the category maths per cell."""
    display = display_currency(conn)
    where, args = _range(start, end)
    rows = conn.execute(
        f"""SELECT v.date, v.category_id, v.category, COUNT(DISTINCT v.transaction_id) AS n,
                   COALESCE(SUM(ROUND(v.spend, 2)), 0) AS net
            FROM {insights.LINES} v
            WHERE {insights.SPENDING} AND {insights.currency_sql(conn)} AND ({_where(where)})
            GROUP BY v.date, v.category_id, v.category
            ORDER BY v.date, v.category_id""", args).fetchall()
    days = [{"date": r["date"], "category_id": r["category_id"], "category": r["category"],
             "net": _r(r["net"]), "count": r["n"]} for r in rows]
    return {"currency": display, "start": start, "end": end, "days": days,
            "total": _r(sum(d["net"] for d in days))}


def _month_bounds(month: str) -> tuple[str, str]:
    return f"{month}-01", f"{month}-{insights.days_in_month(month):02d}"


def year_spending(conn: sqlite3.Connection, today: date, months: int = YEAR_MONTHS,
                  end_month: str | None = None) -> dict[str, Any]:
    """The Spending page's year view: the last `months` calendar months up to `end_month` (the selected month, default
    the current one) as category rows and month cells, plus the four KPIs.

    A cell is that category's net spending in that month, taken from `spending_by_day`, so a month's cells sum to the
    month's `category_totals` net. A month before the household's first transaction is `nodata` (drawn hatched, never
    $0). `kpis`: the 12-month average spent per month (complete months only: the selected month is left out while it is
    the current, partial one), the best month (highest remaining after spending), the most variable category (the widest
    month-to-month net), and the savings rate now against the average for the range. Nothing here is a forecast or a
    percentage of confidence (q6)."""
    if isinstance(months, bool) or not isinstance(months, int) or not 2 <= months <= MAX_YEAR_MONTHS:
        raise ReportError(f"months must be a whole number between 2 and {MAX_YEAR_MONTHS}")
    current = insights.month_of(today)
    end_month = end_month or current
    try:
        insights.parse_month(end_month)
    except (TypeError, ValueError):
        raise ReportError(f"end_month must be a month as YYYY-MM, got {end_month!r}") from None
    if end_month > current:
        raise ReportError(f"end_month {end_month} is after {current}")
    try:
        month_list = [insights.add_months(end_month, -(months - 1 - i)) for i in range(months)]
        start, end = _month_bounds(month_list[0])[0], _month_bounds(month_list[-1])[1]
    except (TypeError, ValueError):
        # e.g. month=1900-05 with a 12-month view: add_months walks back into 1899, which is not a month. A 400, not a 500.
        raise ReportError(f"{months} months ending {end_month} would start before 1900-01") from None
    totals = category_totals(conn, start, end)
    series = spending_by_day(conn, start, end)
    cells: dict[tuple[Any, str], float] = defaultdict(float)
    for d in series["days"]:
        cells[(d["category_id"], d["date"][:7])] = _r(cells[(d["category_id"], d["date"][:7])] + d["net"])
    first_row = conn.execute("SELECT MIN(v.date) FROM v_transactions v").fetchone()[0]
    data_from = first_row if isinstance(first_row, str) else None
    no_data_from = data_from[:7] if data_from else current  # with no data at all, every month is hatched
    month_rows = []
    for m in month_list:
        s, e = _month_bounds(m)
        remaining = remaining_after_spending(conn, s, e)
        spent = _r(sum(cells[(c["category_id"], m)] for c in totals["categories"]))
        month_rows.append({"month": m, "nodata": m < no_data_from, "spending": spent,
                           "income": remaining["income"], "remaining": remaining["remaining"],
                           "share": remaining["share"], "short": remaining["short"]})
    data_months = [r for r in month_rows if not r["nodata"]]
    complete = [r for r in data_months if r["month"] != current] or data_months
    average = _r(sum(r["spending"] for r in complete) / len(complete)) if complete else 0.0
    best = max(data_months, key=lambda r: (r["remaining"], r["month"]), default=None) if data_months else None
    variable = None
    for c in totals["categories"]:
        values = [cells[(c["category_id"], r["month"])] for r in data_months]
        if len(values) < 2:
            continue
        spread = _r(max(values) - min(values))
        if spread <= 0:
            continue
        if variable is None or (spread, c["net"]) > (variable["spread"], variable["net"]):
            variable = {"category_id": c["category_id"], "category": c["category"], "from": _r(min(values)),
                        "to": _r(max(values)), "spread": spread, "net": c["net"]}
    income = _r(sum(r["income"] for r in complete))
    saved = _r(sum(r["remaining"] for r in complete))
    rate_average = _r(saved / income * 100) if income > 0 else None
    now = month_rows[-1]
    rate_now = now["share"]
    kpis = {
        "average": average, "months": len(complete),
        "best": None if best is None else {k: best[k] for k in ("month", "remaining", "share", "short")},
        "most_variable": variable,
        "savings_rate": {"now": rate_now, "average": rate_average,
                         "delta": _r(rate_now - rate_average) if rate_now is not None and rate_average is not None else None},
    }
    rows = []
    for c in totals["categories"]:
        cells_out = [{"month": r["month"], "net": cells[(c["category_id"], r["month"])], "nodata": r["nodata"]}
                     for r in month_rows]
        peak = max((abs(x["net"]) for x in cells_out), default=0.0) or 1.0
        for x in cells_out:
            x["alpha"] = round(abs(x["net"]) / peak, 3)
        rows.append({**{k: c[k] for k in ("category_id", "category", "group", "group_id", "group_rgb", "icon",
                                          "gross", "refunds", "net", "count", "share")},
                     "months": cells_out})
    return {"currency": totals["currency"], "start": start, "end": end, "today": today.isoformat(),
            "months": month_rows, "categories": rows, "refunds_over": totals["refunds_over"],
            "foreign": totals["foreign"], "data_from": data_from, "kpis": kpis}


def category_activity(conn: sqlite3.Connection, category_id: int, start: str | None = None,
                      end: str | None = None) -> dict[str, Any]:
    """One category in one range, for the drill-down that opens in place (q3): Gross, Refunds and Net (the same numbers
    `category_totals` reports for it) and the transactions behind them. A refund is a negative row and reduces the net;
    nothing is dropped."""
    if isinstance(category_id, bool) or not isinstance(category_id, int) or category_id <= 0:
        raise ReportError("category_id must be a whole number")
    where, args = _range(start, end)
    rows = conn.execute(
        f"""SELECT v.transaction_id, MIN(v.date) AS date,
                   COALESCE(NULLIF(TRIM(MIN(v.merchant_name)), ''), TRIM(MIN(v.name)), '') AS label,
                   COALESCE(SUM(ROUND(v.spend, 2)), 0) AS net, COUNT(*) AS lines
            FROM {insights.LINES} v
            WHERE {insights.SPENDING} AND {insights.currency_sql(conn)} AND v.category_id = ?
              AND ({_where(where)})
            GROUP BY v.transaction_id ORDER BY date, v.transaction_id""", [category_id, *args]).fetchall()
    name = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    if name is None:
        raise ReportError(f"no category {category_id}")
    txns = [{"transaction_id": r["transaction_id"], "date": r["date"], "label": r["label"],
             "amount": _r(r["net"]), "lines": r["lines"]} for r in rows]
    gross = _r(sum(t["amount"] for t in txns if t["amount"] > 0))
    refunds = _r(sum(t["amount"] for t in txns if t["amount"] < 0))
    return {"category_id": category_id, "category": name[0], "start": start, "end": end,
            "gross": gross, "refunds": refunds, "net": _r(gross + refunds), "count": len(txns), "rows": txns}
