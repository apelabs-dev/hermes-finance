"""Budget insights: pure functions over a sqlite3 connection, returning plain dicts/lists.

Shared by the web dashboard, the agent read commands (reads.py) and (next) the weekly Discord summary +
alerts, so nothing in here knows about HTTP or HTML.

Spend semantics (all from `v_spend_lines`: `v_transactions` with a split transaction's lines in its place, TASK-19):
  * `spend > 0` is money out; a refund is negative spend and nets against its category.
  * A split transaction counts under each line's category; it's one transaction in every count.
  * Spending = categories that are neither income (`is_income`) nor transfers (`is_transfer`).
  * Income = `-spend` of income categories.
  * Pending transactions count (they're reported separately where it matters).
  * Dates are Plaid's local dates; "today" is the machine's local date unless a `today` is passed.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator, Sequence

from . import alpaca, groupbudget, rollover
from .money import money
from .db import now

log = logging.getLogger(__name__)

SPENDING = "v.is_income = 0 AND v.is_transfer = 0"
LINES = "v_spend_lines"   # TASK-19: spending by category reads a split's lines (a split never changes a total)
# Fixed bills: their spend is not extrapolated at the daily pace in pace_projection(). (v16: Internet & Phone came out
# of Rent & Utilities, and stays fixed.)
FIXED_CATEGORIES = ("Rent & Utilities", "Subscriptions", "Internet & Phone")
# A one-time purchase (RAM off Reddit, a USCIS fee, a big Amazon order) counts once in the pace projection instead of
# being stretched over every day left: a charge of at least ONE_OFF_MIN from a merchant that isn't a regular in the
# ONE_OFF_LOOKBACK_MONTHS before this month (fewer than ONE_OFF_REGULAR_CHARGES charges there). Costco and Trader
# Joe's runs stay in the pace.
ONE_OFF_MIN = 300.0
ONE_OFF_LOOKBACK_MONTHS = 6
ONE_OFF_REGULAR_CHARGES = 3

# recurring(): what qualifies
RECURRING_LOOKBACK_MONTHS = 7         # >= 3 charges in this window (weekly / every 2 weeks / monthly)
YEARLY_LOOKBACK_MONTHS = 25           # the longest window (every 6 months, yearly): >= 2 charges, one interval
# TASK-21: every schedule a bill can have: (id, the shortest and longest median interval between charges, in days). The
# labels are what every page, the CLI and the tools show.
CADENCES = (("weekly", 6, 8), ("biweekly", 13, 16), ("monthly", 25, 35), ("bimonthly", 55, 70),
            ("quarterly", 85, 100), ("semiannual", 170, 195), ("yearly", 350, 380))
CADENCE_RANGE = {c: (lo, hi) for c, lo, hi in CADENCES}
CADENCE_LABELS = {"weekly": "weekly", "biweekly": "every 2 weeks", "monthly": "monthly", "bimonthly": "every 2 months",
                  "quarterly": "every 3 months", "semiannual": "every 6 months", "yearly": "yearly"}
PER_MONTH = {"weekly": 52 / 12, "biweekly": 26 / 12, "monthly": 1.0, "bimonthly": 1 / 2, "quarterly": 1 / 3,
             "semiannual": 1 / 6, "yearly": 1 / 12}
# Detection tries these in order: (window in months, fewest core charges, the cadences it may find there). Each window
# holds enough history for its cadences' fewest charges. Two charges (one interval) count only for a bill-like merchant
# (_kind: a utility, rent, a loan, insurance), except yearly, which has always found a yearly subscription from two.
DETECT_STEPS = ((RECURRING_LOOKBACK_MONTHS, 3, ("weekly", "biweekly", "monthly")),
                (13, 2, ("bimonthly", "quarterly")),
                (YEARLY_LOOKBACK_MONTHS, 2, ("semiannual", "yearly")))
LOOKBACK_MONTHS = {c: months for months, _, cadences in DETECT_STEPS for c in cadences}
# Charged in lumps: a bill still due counts in the month it's expected and its charges aren't paced. Weekly and
# every-2-weeks charges are spread over the month like any other spending.
PERIODIC = ("monthly", "bimonthly", "quarterly", "semiannual", "yearly")
# A tracked bill with no second charge yet (or charges that don't fit its schedule) is expected this long after its last.
NOMINAL_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30, "bimonthly": 60, "quarterly": 91, "semiannual": 182,
                "yearly": 365}
CORE_BAND = 0.25                      # core charges: within ±25% of the merchant's median amount...
CORE_MIN_SHARE = 0.6                  # ...and at least 60% of its charges (the rest are one-off extras)
RECURRING_EXCLUDED = {"Groceries", "Eating Out", "Other Shopping", "Income", "Transfers",
                      "Coffee", "Alcohol & Bars", "Clothing", "Electronics", "Home Goods"}   # v16: split off them
TRANSFER_NAME_RE = re.compile(r"^(to|from) |\bvault\b", re.IGNORECASE)
BILL_PFC_PREFIXES = ("RENT_AND_UTILITIES", "LOAN_PAYMENTS", "GENERAL_SERVICES_INSURANCE")
# TASK-21: a utility's amount changes with the season (SoCalGas $4-$40 a month), so it's found by its schedule alone when
# its amounts don't fit the band: Plaid's RENT_AND_UTILITIES_* but rent, or a name BILL_NAME_RE counts as a utility.
UTILITY_NAME_RE = re.compile(r"at&t|verizon|t-mobile|comcast|xfinity|spectrum|internet|wireless|\butilit", re.IGNORECASE)
UTILITY_PFC_NOT = ("RENT_AND_UTILITIES_RENT",)
BILL_NAME_RE = re.compile(
    r"insurance|geico|state farm|progressive|allstate|financial services|\bloan\b|mortgage|" + UTILITY_NAME_RE.pattern,
    re.IGNORECASE)
ESTIMATE_CHARGES = 3                  # a bill whose amount changes is expected at the median of its last 3 charges
USUAL_MONTHS = 13                     # ...and "usually $4–$40" is the range of its charges over a year of seasons
OUTLIER_HIGH, OUTLIER_LOW = 1.5, 0.5  # "higher than usual": > 1.5x the usual's top; "lower": < 0.5x its bottom
DUE_SOON_DAYS = 3
LATE_AFTER_DAYS = 5

# suggest_caps(): never suggested (a cap can still be set by hand). Reasons are shown under "Not budgeted".
NO_SUGGEST = {"Rent & Utilities", "Internet & Phone", "Other", "Fees"}
NO_SUGGEST_REASONS = {
    "Rent & Utilities": "Fixed bills. Tracked on Recurring",
    "Internet & Phone": "Fixed bills. Tracked on Recurring",
    "Other": "Needs review. Categorize these first",
    "Fees": "Rare. No cap needed",
}


# --- months and days -----------------------------------------------------------------------------

def _today(today: date | None) -> date:
    return today or date.today()


def month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def parse_month(month: str) -> tuple[int, int]:
    """'YYYY-MM' -> (year, month). Raises ValueError otherwise."""
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError(f"expected a month as YYYY-MM, got {month!r}")
    y, m = int(month[:4]), int(month[5:])
    if not (1 <= m <= 12 and 1900 <= y <= 9999):
        raise ValueError(f"expected a month as YYYY-MM, got {month!r}")
    return y, m


def add_months(month: str, n: int) -> str:
    y, m = parse_month(month)
    k = y * 12 + (m - 1) + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def days_in_month(month: str) -> int:
    return calendar.monthrange(*parse_month(month))[1]


def days_elapsed(month: str, today: date | None = None) -> int:
    """Days of `month` that have happened: today's day this month, all of a past month, 0 for a future one."""
    today = _today(today)
    current = month_of(today)
    if month == current:
        return today.day
    return days_in_month(month) if month < current else 0


def _shift_months(d: date, n: int) -> date:
    y, m = parse_month(add_months(month_of(d), n))
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _r(x: float) -> float:
    return round(x + 0.0, 2)  # + 0.0 turns -0.0 into 0.0


def local_day(ts: str | None) -> str | None:
    """A stored UTC timestamp ('2026-09-27T00:20:00Z', db.now()) as the local date it happened on ('2026-09-26' in
    California), for comparing with the local dates everything else uses."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


# --- small shared queries --------------------------------------------------------------------------

def spend_by(conn: sqlite3.Connection, where: str = "1 = 1", args: Sequence[Any] = (),
             group: str | None = None) -> dict[Any, tuple[float, int]]:
    """Spending (SPENDING rows of `v_spend_lines v`) matching `where` (SQL, parameterized by `args`) ->
    {group value: (spend, count)}, or {None: (spend, count)} without a `group` expression. The month and category
    totals, pending spend, cash flow and `finance spend` all sum with this. count is transactions (a split one is one
    in each of its categories, and one in a total)."""
    sql = (f"SELECT {group or 'NULL'}, COALESCE(SUM(v.spend), 0), COUNT(DISTINCT v.transaction_id) FROM {LINES} v "
           f"WHERE {SPENDING} AND ({where})")
    if group:
        sql += f" GROUP BY {group}"
    return {r[0]: (_r(r[1]), r[2]) for r in conn.execute(sql, args)}


def _spend(conn: sqlite3.Connection, month: str) -> float:
    return spend_by(conn, "v.month = ?", (month,))[None][0]


def _income(conn: sqlite3.Connection, month: str) -> float:
    row = conn.execute(f"SELECT COALESCE(-SUM(v.spend), 0) FROM {LINES} v WHERE v.month = ? AND v.is_income = 1",
                       (month,)).fetchone()
    return _r(row[0])


def _daily_spend(conn: sqlite3.Connection, month: str) -> dict[int, float]:
    rows = conn.execute(
        f"SELECT CAST(substr(v.date, 9, 2) AS INTEGER) AS day, SUM(v.spend) FROM {LINES} v "
        f"WHERE v.month = ? AND {SPENDING} GROUP BY day", (month,))
    return {r[0]: r[1] for r in rows}


def _cumulative(daily: dict[int, float], n: int) -> list[float]:
    """Running total for days 1..n (anything dated after day n lands on day n)."""
    out, total = [], 0.0
    for d in range(1, n + 1):
        total += daily.get(d, 0.0)
        out.append(total)
    if out:
        out[-1] += sum(v for d, v in daily.items() if d > n)
    return [_r(x) for x in out]


def _category_month_spend(conn: sqlite3.Connection, month: str) -> dict[int, tuple[float, int]]:
    return spend_by(conn, "v.month = ?", (month,), "v.category_id")


def _spending_categories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name, monthly_cap FROM categories "
                        "WHERE is_income = 0 AND is_transfer = 0 ORDER BY name").fetchall()


def cap_status(spend: float, cap: float | None) -> str:
    """ok (< 80%), warn (>= 80%), over (>= 100%), or nocap. Compared in cents, so exactly 80% is warn."""
    if cap is None:
        return "nocap"
    spend_c, cap_c = round(spend * 100), round(cap * 100)
    if cap_c <= 0:
        return "over" if spend_c > 0 else "ok"
    if spend_c >= cap_c:
        return "over"
    return "warn" if spend_c * 10 >= cap_c * 8 else "ok"


# --- 1. month summary ------------------------------------------------------------------------------

def month_summary(conn: sqlite3.Connection, month: str, today: date | None = None) -> dict[str, Any]:
    today = _today(today)
    prev = add_months(month, -1)
    dim, dim_prev = days_in_month(month), days_in_month(prev)
    day = days_elapsed(month, today)
    cum = _cumulative(_daily_spend(conn, month), day)
    cum_prev = _cumulative(_daily_spend(conn, prev), dim_prev)
    mtd = cum[-1] if cum else _spend(conn, month)
    past = month < month_of(today)
    # same day of last month; for a finished month, last month's total
    prev_same_day = (cum_prev[-1] if past else cum_prev[min(day, dim_prev) - 1]) if day else 0.0
    pending = spend_by(conn, "v.month = ? AND v.pending = 1", (month,))[None]
    return {
        "month": month,
        "prev_month": prev,
        "is_current": month == month_of(today),
        "days_in_month": dim,
        "days_in_prev_month": dim_prev,
        "day": day,                               # today's day (whole month for a past month)
        "days_left": dim - day,
        "mtd": mtd,
        "prev_same_day": prev_same_day,
        "delta": _r(mtd - prev_same_day),
        "prev_total": cum_prev[-1] if cum_prev else 0.0,
        "cum": cum,                               # length = day
        "cum_prev": cum_prev,                     # length = days in the previous month
        "income": _income(conn, month),
        "income_prev": _income(conn, prev),
        "pending_spend": pending[0],
        "pending_count": pending[1],
    }


# --- 2. pace projection ----------------------------------------------------------------------------

def pace_projection(conn: sqlite3.Connection, month: str, today: date | None = None,
                    rec: dict[str, Any] | None = None) -> dict[str, Any]:
    """projection = mtd + (discretionary_mtd / days_elapsed) * days_left + bills_still_due.

    discretionary_mtd leaves out fixed spend AND one-time purchases (_one_offs): those count once, at their amount.

    Fixed spend (not extrapolated) = the FIXED_CATEGORIES plus this month's charges from the PERIODIC recurring
    merchants (monthly, every 2 / 3 / 6 months, yearly). Bills still due = those recurring items expected from today to
    the end of this month that haven't been seen this month (current month only), at their amount (a bill whose amount
    changes: its estimate). An every-2-months bill is due only in the month it's expected.
    """
    today = _today(today)
    s = month_summary(conn, month, today)
    rec = rec if rec is not None else recurring(conn, today)
    periodic = _periodic(rec)
    fixed = 0.0
    for _, spend in _fixed_rows(conn, month, _keys(periodic)):
        fixed += spend
    fixed = _r(fixed)
    one_offs = _one_offs(conn, month, _keys(periodic))
    one_off_total = _r(sum(o["amount"] for o in one_offs))
    discretionary = _r(s["mtd"] - fixed - one_off_total)
    elapsed, left = s["day"], s["days_left"]
    daily = _r(discretionary / elapsed) if elapsed else 0.0
    pace_add = _r(max(discretionary, 0.0) / elapsed * left) if elapsed else 0.0
    due = [{"merchant": r["merchant"], "amount": r["amount"], "next_expected": r["next_expected"], "kind": r["kind"],
            "variable": r["variable"]} for r in _bills_due(periodic, month, today)]
    due_total = _r(sum(d["amount"] for d in due))
    return {
        "month": month,
        "mtd": s["mtd"],
        "fixed_mtd": fixed,
        "one_offs": one_offs,
        "one_off_total": one_off_total,
        "discretionary_mtd": discretionary,
        "fixed_categories": list(FIXED_CATEGORIES),
        "days_elapsed": elapsed,
        "days_left": left,
        "daily_pace": daily,
        "pace_add": pace_add,
        "bills_due": sorted(due, key=lambda d: d["next_expected"]),
        "bills_due_total": due_total,
        "projection": _r(s["mtd"] + pace_add + due_total),
        "prev_total": s["prev_total"],
    }


def category_pace(conn: sqlite3.Connection, month: str, today: date | None = None,
                  rec: dict[str, Any] | None = None, cats: list[dict[str, Any]] | None = None) -> dict[str, float]:
    """pace_projection() for each spending category on its own (same fixed spend, pace and bills-due rules):
    category -> its spend + its discretionary spend at its daily pace for the days left + its bills still due."""
    today = _today(today)
    rec = rec if rec is not None else recurring(conn, today)
    cats = cats if cats is not None else category_spend(conn, month, today)
    periodic = _periodic(rec)
    fixed: dict[str, float] = defaultdict(float)
    for category, spend in _fixed_rows(conn, month, _keys(periodic)):
        fixed[category] += spend
    for o in _one_offs(conn, month, _keys(periodic)):
        fixed[o["category"]] += o["amount"]
    due: dict[str, float] = defaultdict(float)
    for r in _bills_due(periodic, month, today):
        due[r["category"]] += r["amount"]
    elapsed = days_elapsed(month, today)
    left = days_in_month(month) - elapsed
    out = {}
    for c in cats:
        discretionary = _r(c["spend"] - _r(fixed[c["category"]]))
        pace_add = _r(max(discretionary, 0.0) / elapsed * left) if elapsed else 0.0
        out[c["category"]] = _r(c["spend"] + pace_add + _r(due[c["category"]]))
    return out


def _periodic(rec: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in rec["items"] if r["cadence"] in PERIODIC]


def _keys(items: list[dict[str, Any]]) -> set[str]:
    return {k for r in items for k in r.get("keys", [r["key"]])}


def _fixed_rows(conn: sqlite3.Connection, month: str, keys: set[str]) -> Iterator[tuple[str, float]]:
    """(category, spend) of the month's fixed spending, which the pace doesn't extrapolate: the FIXED_CATEGORIES
    plus charges from the PERIODIC recurring merchants in `keys`."""
    for row in conn.execute(
            f"SELECT v.category, v.merchant_name, v.name, v.spend FROM {LINES} v "
            f"WHERE v.month = ? AND {SPENDING}", (month,)):
        if row["category"] in FIXED_CATEGORIES or merchant_key(row["merchant_name"], row["name"]) in keys:
            yield row["category"], row["spend"]


def _one_offs(conn: sqlite3.Connection, month: str, periodic_keys: set[str]) -> list[dict[str, Any]]:
    """The month's one-time purchases, biggest first: spending lines of at least ONE_OFF_MIN, outside the fixed spend
    (_fixed_rows), from a merchant with fewer than ONE_OFF_REGULAR_CHARGES charges in the ONE_OFF_LOOKBACK_MONTHS
    before this month. The pace projection counts them once instead of stretching them over the days left."""
    start = add_months(month, -ONE_OFF_LOOKBACK_MONTHS)
    seen: dict[str, int] = defaultdict(int)
    for row in conn.execute(
            f"SELECT v.merchant_name, v.name FROM {LINES} v WHERE v.month >= ? AND v.month < ? AND {SPENDING} "
            f"AND v.spend > 0", (start, month)):
        seen[merchant_key(row["merchant_name"], row["name"])] += 1
    out = []
    for row in conn.execute(
            f"SELECT v.date, v.category, v.merchant_name, v.name, v.spend FROM {LINES} v "
            f"WHERE v.month = ? AND {SPENDING} AND v.spend >= ?", (month, ONE_OFF_MIN)):
        key = merchant_key(row["merchant_name"], row["name"])
        if row["category"] in FIXED_CATEGORIES or key in periodic_keys or seen[key] >= ONE_OFF_REGULAR_CHARGES:
            continue
        out.append({"date": row["date"], "merchant": row["merchant_name"] or row["name"], "category": row["category"],
                    "amount": _r(row["spend"])})
    return sorted(out, key=lambda o: -o["amount"])


def _bills_due(periodic: list[dict[str, Any]], month: str, today: date) -> list[dict[str, Any]]:
    """Recurring items expected from today to the end of the month and not seen this month (current month only)."""
    if month != month_of(today):
        return []
    end = date(*parse_month(month), days_in_month(month)).isoformat()
    return [r for r in periodic if today.isoformat() <= r["next_expected"] <= end and not r["paid_this_month"]]


def bills_due_by_category(conn: sqlite3.Connection, month: str, today: date | None = None,
                          rec: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """The month's recurring bills still due (the pace projection's bills_due), per category, soonest first."""
    today = _today(today)
    if month != month_of(today):
        return {}
    rec = rec if rec is not None else recurring(conn, today)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in sorted(_bills_due(_periodic(rec), month, today), key=lambda r: (r["next_expected"], r["merchant"])):
        out[r["category"]].append({"merchant": r["merchant"], "amount": r["amount"],
                                   "next_expected": r["next_expected"], "category": r["category"],
                                   "variable": r["variable"]})
    return out


# --- 3. category spend -----------------------------------------------------------------------------

def category_spend(conn: sqlite3.Connection, month: str, today: date | None = None,
                   rec: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every spending category: spend, count, last month, 3-month average, cap, pct of cap (spend / cap), its
    recurring bills still due this month (`due`, `bills_due`; current month only), left (cap - spend - due, negative
    when over; None without a cap) and status. The status comes from spend + due: a bill that will land this month
    counts before it posts. With rollover on for a category (TASK-19, rollover.py), `cap` is the effective cap (the cap
    you set, `base_cap`, plus last month's unused part, `rollover`): pct, left and status use it."""
    today = _today(today)
    cur = _category_month_spend(conn, month)
    prev = [_category_month_spend(conn, add_months(month, -i)) for i in (1, 2, 3)]
    due_by = bills_due_by_category(conn, month, today, rec)
    rolls = rollover.enabled(conn)
    out = []
    for c in _spending_categories(conn):
        spend, count = cur.get(c["id"], (0.0, 0))
        cap = c["monthly_cap"]
        history = [p.get(c["id"], (0.0, 0))[0] for p in prev]
        bills = due_by.get(c["name"], [])
        due = _r(sum(b["amount"] for b in bills))
        roll = rollover.carry(cap, history[0]) if cap is not None and c["id"] in rolls else None
        if roll is not None:
            cap = _r(cap + roll)
        out.append({
            "category": c["name"],
            "spend": spend,
            "count": count,
            "last_month": history[0],
            "avg3": _r(sum(history) / 3),
            "cap": cap,
            "pct": _r(spend / cap * 100) if cap else None,
            "due": due,
            "bills_due": bills,
            "left": _r(cap - spend - due) if cap is not None else None,
            "status": cap_status(spend + due, cap),
            **({"base_cap": c["monthly_cap"], "rollover": {"amount": roll, "from": add_months(month, -1)}}
               if roll is not None else {}),
        })
    out.sort(key=lambda c: (-c["spend"], c["category"]))
    return out


def group_spend(conn: sqlite3.Connection, month: str, cats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Where it went: taxonomy groups, with distinct payments across their split lines."""
    totals = spend_by(conn, "v.month = ?", (month,),
                      "(SELECT c.group_id FROM categories c WHERE c.id = v.category_id)")
    names = dict(conn.execute("SELECT name, group_id FROM categories WHERE is_income = 0 AND is_transfer = 0"))
    out = []
    for g in conn.execute("SELECT id, name, rgb FROM category_groups ORDER BY sort, id"):
        spend, count = totals.get(g["id"], (0.0, 0))
        out.append({"group": g["name"], "rgb": g["rgb"], "spend": spend, "count": count,
                    "categories": [c for c in cats if names[c["category"]] == g["id"]]})
    return sorted(out, key=lambda g: -g["spend"])


def category_history(conn: sqlite3.Connection, months: list[str]) -> dict[str, list[float]]:
    """Spend per spending category for each of `months` (for the budget mini-charts)."""
    per_month = [_category_month_spend(conn, m) for m in months]
    return {c["name"]: [pm.get(c["id"], (0.0, 0))[0] for pm in per_month] for c in _spending_categories(conn)}


# --- 4. budgets --------------------------------------------------------------------------------------

def budgets(conn: sqlite3.Connection, month: str, today: date | None = None,
            cats: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Capped categories only (category_spend() rows: each has due, left = cap - spend - due and its status).

    left = total_cap - total_spend - total_due: the net. An overspent cap counts against it, and so do the capped
    categories' recurring bills still due this month (current month only), so it goes negative when the month is
    over. It splits into `under` (what's still free in the `under_count` caps not used up) minus `over_amount` (how
    far the `over` caps are past theirs). per_day spreads max(left, 0) over the days left (days after today; on the
    last day it's all for today): 0 once the net is used up, None for a past month.

    polish P2e: a group with a budget of its own (groupbudget.py) is ONE budget here, in `group_budgets`: the totals,
    count, over / warn / under and bills_due take it once (its budget, all its categories' spend and bills) and leave
    out the caps inside it, which stay in `categories` marked `counted: false` / `in_group_budget`. With no group
    budget, every key and value is as before.

    Group roll-up (2026-10-22): a group with caps but no budget of its own counts its uncapped categories too
    (groupbudget.rest, listed in `group_rest`): their spend and bills due are in total_spend / total_due / left and
    bills_due. That rest is paid from its group's free cap room first (`under`), the remainder is `over_amount`, and a
    group the rest alone pushes over adds one to `over`. `count` / `under_count` / `warn` still count the caps."""
    today = _today(today)
    cats = cats if cats is not None else category_spend(conn, month, today)
    capped = sorted((c for c in cats if c["cap"] is not None),
                    key=lambda c: (-(c["pct"] if c["pct"] is not None else math.inf), c["category"]))
    units = groupbudget.units(conn, cats)
    inside = {n: u["group"] for u in units for n in u["categories"]}
    capped = [{**c, "counted": False, "in_group_budget": inside[c["category"]]} if c["category"] in inside else c
              for c in capped]
    counted = [c for c in capped if c["category"] not in inside] + units   # each budget once
    rest = groupbudget.rest(conn, cats)   # group roll-up: a capped group's uncapped spending counts too
    total_cap = _r(sum(c["cap"] for c in counted))
    total_spend = _r(sum(c["spend"] for c in counted) + sum(r["spend"] for r in rest))
    total_due = _r(sum(c["due"] for c in counted) + sum(r["due"] for r in rest))
    left = _r(total_cap - total_spend - total_due)
    over = [c for c in counted if c["status"] == "over"]
    under = [c for c in counted if c["status"] != "over"]
    # A group's rest is paid from the room still free in its caps first; what's past that is over. So left stays
    # under - over_amount, and a group pushed over only by its rest counts once in `over`.
    under_amt = sum(max(c["left"], 0.0) for c in under)
    over_amt = sum(max(-c["left"], 0.0) for c in over)
    by_cat = {c["category"]: c for c in counted if "category" in c}   # the caps (a group budget's unit has none)
    spilled = 0
    for r in rest:
        mine = [by_cat[n] for n in r["caps"] if n in by_cat]
        amt = r["spend"] + r["due"]
        if amt <= 0:   # refunds beat spending: room back
            under_amt -= amt
            continue
        room = sum(max(c["left"], 0.0) for c in mine if c["status"] != "over")
        take = min(amt, room)
        under_amt -= take
        over_amt += amt - take
        if amt - take > 0.005 and not any(c["status"] == "over" for c in mine):
            spilled += 1
    current = month_of(today)
    dim = days_in_month(month)
    days_left = dim - today.day if month == current else (0 if month < current else dim)
    per_day = None if month < current else _r(max(left, 0.0) / max(days_left, 1))
    return {
        "month": month,
        "categories": capped,
        "group_budgets": units,
        "group_rest": rest,
        "count": len(counted),
        "total_cap": total_cap,
        "total_spend": total_spend,
        "total_due": total_due,
        "bills_due": sorted((b for c in [*counted, *rest] for b in c["bills_due"]),
                            key=lambda b: (b["next_expected"], b["merchant"])),
        "left": left,
        "under": _r(under_amt),
        "under_count": len(under),
        "over_amount": _r(over_amt),
        "days_left": days_left,
        "per_day": per_day,
        "over": len(over) + spilled,
        "warn": sum(c["status"] == "warn" for c in counted),
    }


# --- 5. suggested caps -------------------------------------------------------------------------------

def round_cap(value: float) -> float:
    """Round up to a sensible step: $25 under $300, $50 from $300."""
    cents = round(value * 100)
    step = 2500 if cents < 30000 else 5000
    return float(-(-cents // step) * step // 100)


def suggest_caps(conn: sqlite3.Connection, months: int = 3, today: date | None = None) -> dict[str, Any]:
    """Median of the last `months` complete months per spending category, rounded up. Read-only.
    NO_SUGGEST categories (fixed bills, the needs-review bucket, rare fees) are left out."""
    today = _today(today)
    ms = [add_months(month_of(today), -i) for i in range(months, 0, -1)]  # oldest first
    per_month = [_category_month_spend(conn, m) for m in ms]
    out = []
    for c in _spending_categories(conn):
        if c["name"] in NO_SUGGEST:
            continue
        values = [pm.get(c["id"], (0.0, 0))[0] for pm in per_month]
        median = _r(statistics.median(values)) if values else 0.0
        out.append({
            "category": c["name"],
            "values": values,
            "median": median,
            "suggested": round_cap(median) if median > 0 else None,
            "cap": c["monthly_cap"],
        })
    return {"months": ms, "categories": out,
            "total": _r(sum(c["suggested"] or 0 for c in out))}


# --- 6. recurring ------------------------------------------------------------------------------------

def merchant_key(merchant_name: str | None, name: str | None) -> str:
    return (merchant_name or name or "").strip().casefold()


# Words too common in bank descriptions to say two names are the same merchant.
NAME_STOPWORDS = frozenset(
    "the and inc llc ltd corp company www http https com net org payment payments online purchase debit credit card "
    "autopay bill billing recurring subscription monthly annual store shop services service pos ach web mobile".split())
RENAME_AMOUNT_BAND = 0.05  # a renamed merchant's first charge is within ±5% of its last one under the old name


def name_tokens(*names: str | None) -> set[str]:
    """The distinctive words of a merchant's names: 4+ letters, not generic. A domain-like word counts joined and by
    its first label, so "OPENCODE ANOMA.LY" gives {"opencode", "anomaly", "anoma"} and matches plain "ANOMALY"."""
    out: set[str] = set()
    for n in names:
        for word in re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", (n or "").casefold()):
            for w in {word.replace(".", ""), word.split(".")[0]}:
                if len(w) >= 4 and not w.isdigit() and w not in NAME_STOPWORDS:
                    out.add(w)
    return out


def _merge_renames(groups: dict[str, list[sqlite3.Row]]) -> list[tuple[list[str], list[sqlite3.Row]]]:
    """Merchant groups -> [(keys, rows)], where a group that starts after another one ended is appended to it when
    the bank renamed the merchant: they share a distinctive name token, the new group's first charge is within ±5% of
    the old group's last one, and the gap between them fits a cadence window (weekly, monthly or yearly)."""
    chains: list[dict[str, Any]] = []

    def day_amounts(rows: list[sqlite3.Row]) -> list[tuple[date, float]]:
        per_day: dict[str, float] = defaultdict(float)
        for r in rows:
            per_day[r["date"]] += r["spend"]
        return [(date.fromisoformat(d), a) for d, a in sorted(per_day.items())]

    for key, rows in sorted(groups.items(), key=lambda kv: (kv[1][0]["date"], kv[0])):
        charges = day_amounts(rows)
        tokens = name_tokens(*(n for r in rows for n in (r["merchant_name"], r["name"])))
        first_day, first_amount = charges[0]
        best = None
        for ch in chains:
            last_day, last_amount = ch["charges"][-1]
            gap = (first_day - last_day).days
            if (gap > 0 and ch["tokens"] & tokens and _cadence(gap)
                    and abs(first_amount - last_amount) <= RENAME_AMOUNT_BAND * last_amount
                    and (best is None or last_day > best["charges"][-1][0])):
                best = ch
        if best:
            best["keys"].append(key)
            best["rows"].extend(rows)
            best["charges"].extend(charges)
            best["tokens"] |= tokens
        else:
            chains.append({"keys": [key], "rows": list(rows), "charges": charges, "tokens": tokens})
    return [(ch["keys"], ch["rows"]) for ch in chains]


def _cadence(interval: float) -> str | None:
    return next((label for label, lo, hi in CADENCES if lo <= interval <= hi), None)


def _stable(amounts: list[float]) -> bool:
    lo, hi = min(amounts), max(amounts)
    if lo <= 0:
        return False
    if hi / lo <= 1.35:
        return True
    mean = statistics.fmean(amounts)
    return statistics.pstdev(amounts) / mean < 0.2


def _detect(charges: list[tuple[date, float]],
            min_core: int) -> tuple[str, float, list[tuple[date, float]]] | None:
    """(cadence, median interval, core charges) if the per-day charges look recurring, else None.

    Core charges are within ±25% of the median amount, so an occasional one-off extra from the same
    merchant doesn't break detection: there must be `min_core` of them, at least 60% of all charges,
    and the median interval between them must fit a cadence with stable amounts.
    """
    if not charges:
        return None
    mid = statistics.median(a for _, a in charges)
    core = [c for c in charges if abs(c[1] - mid) <= CORE_BAND * mid]
    if len(core) < min_core or len(core) / len(charges) < CORE_MIN_SHARE:
        return None
    intervals = [(b[0] - a[0]).days for a, b in zip(core, core[1:])]
    med = statistics.median(intervals)
    cadence = _cadence(med)
    if not cadence or not _stable([a for _, a in core]):
        return None
    return cadence, med, core


def _detect_schedule(charges: list[tuple[date, float]],
                     min_core: int) -> tuple[str, float, list[tuple[date, float]]] | None:
    """TASK-21, a utility whose amount changes: _detect with every charge counted (no amount band, no stable amounts).
    The median interval must fit a cadence of a month or longer (a utility isn't billed weekly), and most intervals must
    fit it too (_regular)."""
    if len(charges) < max(min_core, 2):
        return None
    intervals = [(b[0] - a[0]).days for a, b in zip(charges, charges[1:])]
    med = statistics.median(intervals)
    cadence = _cadence(med)
    if cadence not in PERIODIC or not _regular(charges, cadence):
        return None
    return cadence, med, charges


def _regular(charges: list[tuple[date, float]], cadence: str) -> bool:
    """Most (CORE_MIN_SHARE) of the intervals between the charges fit the cadence, not just their median: a merchant
    visited at odd times (19, 86 and 57 days apart) isn't a bill every 2 months."""
    lo, hi = CADENCE_RANGE[cadence]
    intervals = [(b[0] - a[0]).days for a, b in zip(charges, charges[1:])]
    return sum(lo <= i <= hi for i in intervals) >= CORE_MIN_SHARE * len(intervals)


# weekly / monthly / yearly keep TASK-4's rule exactly (their median interval alone); the cadences TASK-21 added (and every
# schedule-only find) also need most intervals to fit (_regular)
KEPT_CADENCES = ("weekly", "monthly", "yearly")


def _find(charges: list[tuple[date, float]], today: date, bill_like: bool,
          utility: bool) -> tuple[str, float, list[tuple[date, float]], bool] | None:
    """(cadence, median interval, core charges, variable) or None. DETECT_STEPS with the amount band first (every
    merchant); then, for a utility only, the same steps by schedule alone: a bill whose amount changes (variable). A
    cadence TASK-21 added needs most of its intervals to fit it, not only the median."""
    for variable, detect in ((False, _detect), (True, _detect_schedule)):
        if variable and not utility:
            break
        for months, fewest, cadences in DETECT_STEPS:
            since = _shift_months(today, -months).isoformat()
            found = detect([c for c in charges if c[0].isoformat() >= since], fewest)
            if (found and found[0] in cadences and (len(found[2]) >= 3 or bill_like or found[0] == "yearly")
                    and (found[0] in KEPT_CADENCES or _regular(found[2], found[0]))):
                return (*found, variable)
    return None


def is_utility(pfc_detailed: str | None, merchant: str | None) -> bool:
    """TASK-21: gas & electricity, water, sewage / waste, telephone, internet & cable, other utilities (Plaid's
    RENT_AND_UTILITIES_* but rent), or a name BILL_NAME_RE counts as a utility (AT&T, Comcast, "... Utility Company")."""
    pfc = pfc_detailed or ""
    return (pfc.startswith("RENT_AND_UTILITIES") and pfc not in UTILITY_PFC_NOT) or bool(UTILITY_NAME_RE.search(merchant or ""))


def _kind(category: str, pfc_detailed: str | None, merchant: str) -> str:
    if (category == "Rent & Utilities" or (pfc_detailed or "").startswith(BILL_PFC_PREFIXES)
            or BILL_NAME_RE.search(merchant)):
        return "bill"
    return "subscription"


def money_whole(x: float) -> str:
    """$296 for 295.50 (half up), for estimates and "usually" ranges: an estimate is never shown to the cent (TASK-21)."""
    v = math.floor(abs(x) + 0.5)
    return f"{'-' if x < 0 and v else ''}${v:,}"


def usual_text(low: float, high: float) -> str:
    """$100–150, or $120 for one amount."""
    lo, hi = math.floor(low + 0.5), math.floor(high + 0.5)
    return f"${lo:,}" if lo == hi else f"${lo:,}–{hi:,}"


def amount_label(amount: float, variable: bool) -> str:
    return f"~{money_whole(amount)} est." if variable else money(amount)


def outlier(amount: float, usual: dict[str, Any] | None) -> str | None:
    """higher / lower than the usual amount the owner typed (> 1.5x its top, < 0.5x its bottom), else None."""
    if not usual or usual.get("source") != "you":
        return None
    if amount > OUTLIER_HIGH * usual["high"]:
        return "higher"
    if amount < OUTLIER_LOW * usual["low"]:
        return "lower"
    return None


def _status(last_date: date, nxt: date, cadence: str, today: date) -> tuple[int, str]:
    """(days until the next charge, status). TASK-21: late only once the cadence's longest interval plus LATE_AFTER_DAYS
    has passed since the last charge (every 2 months: 75 days), so a slower schedule is never late early."""
    until = (nxt - today).days
    if (today - last_date).days > CADENCE_RANGE[cadence][1] + LATE_AFTER_DAYS:
        return until, "late"
    return until, "due_soon" if until <= DUE_SOON_DAYS else "upcoming"


def _account(txns: Sequence[Any], day: date) -> dict[str, Any] | None:
    """The account the charge on `day` came from (the last of that day's rows): "last paid from"."""
    row = next((t for t in reversed(txns) if t["date"] == day.isoformat()), None)
    if row is None or not (row["account_name"] or row["account_mask"]):
        return None
    return {"name": row["account_name"], "mask": row["account_mask"], "institution": row["institution_name"]}


def _per_day(txns: Iterable[Any]) -> list[tuple[date, float]]:
    per_day: dict[str, float] = defaultdict(float)
    for t in txns:
        per_day[t["date"]] += t["spend"]
    return [(date.fromisoformat(d), a) for d, a in sorted(per_day.items())]


MONTH_STEPS = {"monthly": 1, "bimonthly": 2, "quarterly": 3, "semiannual": 6, "yearly": 12}


def _next_charge(last_date: date, cadence: str, step: int, charges: list[tuple[date, float]]) -> date:
    """hotfix 2026-09-29: a bill billed on a fixed day of the month comes back on that day, not a fixed number of days
    later (the gym, charged Aug 1 and Sep 1, was put on Oct 2: Sep 1 + the 31-day Aug→Sep gap). It counts as billed on
    a day of the month when it has 2+ charges and its recent ones (up to 3) all fell on the same day (month ends lined
    up: Jul 31 = Jun 30); then next = that day in the cadence's month (the month's last day if it has no such day). A bill whose day
    drifts (every 55 days, say), and weekly / every-2-weeks bills, keep last + the median interval."""
    months = MONTH_STEPS.get(cadence)
    if not months:
        return last_date + timedelta(days=step)

    def off(d: date) -> int:  # days between d's day of the month and the last charge's, month ends lined up
        end_d = calendar.monthrange(d.year, d.month)[1]
        end_l = calendar.monthrange(last_date.year, last_date.month)[1]
        return min(abs(d.day - last_date.day), abs(min(d.day, end_l) - min(last_date.day, end_d)))

    if len(charges) < 2 or any(off(d) > 0 for d, _ in charges[-3:]):
        return last_date + timedelta(days=step)
    y, m = parse_month(add_months(month_of(last_date), months))
    return date(y, m, min(last_date.day, calendar.monthrange(y, m)[1]))


def _item(*, merchant: str, key: str, keys: list[str], kind: str, category: str, cadence: str, amount: float,
          variable: bool, step: int, charges: list[tuple[date, float]], today: date, txns: Sequence[Any],
          usual: dict[str, Any] | None, source: str, logo: str | None, icon: str | None) -> dict[str, Any]:
    last_date, last_amount = charges[-1]
    nxt = _next_charge(last_date, cadence, step, charges)
    until, status = _status(last_date, nxt, cadence, today)
    amount = _r(amount)
    return {
        "merchant": merchant,
        "key": key,
        "keys": keys,                              # every merchant key it went by (a rename keeps its history)
        "kind": kind,
        "category": category,
        "amount": amount,                          # what's expected: exact, or (variable) the estimate
        "cadence": cadence,
        "cadence_label": CADENCE_LABELS[cadence],
        "interval_days": step,
        "charges": len(charges),
        "last_date": last_date.isoformat(),
        "last_amount": _r(last_amount),
        "next_expected": nxt.isoformat(),
        "days_until": until,
        "status": status,
        "paid_this_month": month_of(last_date) == month_of(today),
        "monthly_amount": _r(amount * PER_MONTH[cadence]),
        "variable": variable,                      # TASK-21: the amount changes each time; amount is an estimate
        "estimate": amount if variable else None,
        "amount_label": amount_label(amount, variable),
        "usual": usual,                            # {low, high, source: charges | you} or None
        "source": source,                          # detected | tracked
        "last_account": _account(txns, last_date), # "last paid from"
        "outlier": outlier(last_amount, usual),    # the last charge: higher / lower than usual, or None
        "logo_url": logo,
        "icon_url": icon,
    }


def _logo(txns: Sequence[Any]) -> tuple[str | None, str | None]:
    return (next((t["logo_url"] for t in reversed(txns) if t["logo_url"]), None),
            next((t["icon_url"] for t in reversed(txns) if t["icon_url"]), None))


def _season(charges: list[tuple[date, float]], today: date) -> dict[str, Any] | None:
    """A changing bill's usual range: its lowest and highest charge over the last USUAL_MONTHS (summer and winter)."""
    since = _shift_months(today, -USUAL_MONTHS)
    amounts = [a for d, a in charges if d >= since]
    return {"low": _r(min(amounts)), "high": _r(max(amounts)), "source": "charges"} if len(amounts) >= 2 else None


def tracked_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """tracked_bills (TASK-21), [] on a database from before it."""
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM tracked_bills ORDER BY id")]
    except sqlite3.OperationalError:
        return []


def usual_of(t: dict[str, Any]) -> dict[str, Any] | None:
    return {"low": t["usual_low"], "high": t["usual_high"], "source": "you"} if t.get("usual_low") else None


def _tracked_item(conn: sqlite3.Connection, t: dict[str, Any], txns: list[Any], keys: list[str],
                  today: date) -> dict[str, Any]:
    """A bill tracked by hand: every charge from its merchant (any account) in its cadence's window, and the charge it
    started from. Next = the last charge + the median interval (when it fits the cadence) or the cadence's nominal days.
    Amount: the last charge; changing each time, the median of the last 3, or while there are fewer than 3 charges and
    the owner typed what it usually is, the middle of that (a single charge is never the estimate then)."""
    cadence = t["cadence"]
    if not txns:  # nothing from it in the last 25 months: its anchor's row, if Plaid still has it
        txns = [r for r in conn.execute(_REC_SELECT.format(where="v.transaction_id = ?"), (t["anchor_transaction_id"],))]
    since = _shift_months(today, -LOOKBACK_MONTHS[cadence]).isoformat()
    per_day = dict((d.isoformat(), a) for d, a in _per_day(r for r in txns if r["date"] >= since))
    if t["anchor_date"] not in per_day and (t["anchor_date"] >= since or not per_day):
        per_day[t["anchor_date"]] = t["anchor_amount"]  # the charge it starts from (older, or gone from Plaid)
    charges = [(date.fromisoformat(d), a) for d, a in sorted(per_day.items())]
    intervals = [(b[0] - a[0]).days for a, b in zip(charges, charges[1:])]
    lo, hi = CADENCE_RANGE[cadence]
    med = statistics.median(intervals) if intervals else None
    step = math.floor(med + 0.5) if med is not None and lo <= med <= hi else NOMINAL_DAYS[cadence]
    usual = usual_of(t)
    variable = bool(t["variable"])
    if not variable:
        amount = charges[-1][1]
    elif usual and len(charges) < ESTIMATE_CHARGES:
        amount = (usual["low"] + usual["high"]) / 2
    else:
        amount = statistics.median(a for _, a in charges[-ESTIMATE_CHARGES:])
    if variable and not usual and len(charges) >= 2:
        usual = _season(_per_day(txns), today) or _season(charges, today)
    last = txns[-1] if txns else None
    logo, icon = _logo(txns)
    merchant = (last["merchant_name"] or last["name"]).strip() if last else t["merchant"]
    # hotfix 2026-09-29: a tracked merchant is a bill or a subscription by the same rule as a detected one (a gym is a
    # subscription, LADWP a bill); before, every tracked row was a bill
    kind = _kind(last["category"], last["pfc_detailed"], merchant) if last else "bill"
    return _item(merchant=merchant, key=t["merchant_key"],
                 keys=keys, kind=kind, category=last["category"] if last else "Other", cadence=cadence, amount=amount,
                 variable=variable, step=step, charges=charges, today=today, txns=txns, usual=usual, source="tracked",
                 logo=logo, icon=icon)


_REC_SELECT = """SELECT v.transaction_id, v.date, v.name, v.merchant_name, v.spend, v.category, v.is_income, v.is_transfer,
                  v.pfc_detailed, v.logo_url, v.account_name, v.account_mask, v.institution_name,
                  json_extract(t.raw_json, '$.personal_finance_category_icon_url') AS icon_url
           FROM v_transactions v JOIN transactions t USING (transaction_id)
           WHERE {where}
           ORDER BY v.date, v.transaction_id"""


def recurring(conn: sqlite3.Connection, today: date | None = None,
              tracked: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Detect recurring charges, and add the bills tracked by hand (TASK-21).

    Only core charges count: those within ±25% of the merchant's median amount in the window, which
    must be at least 60% of its charges there (so an occasional one-off extra is ignored). Core amounts must be stable
    (max/min <= 1.35 or CV < 0.2). A merchant qualifies (DETECT_STEPS) with >= 3 core charges in the last 7 months whose
    median interval is 6-8 days (weekly), 13-16 (every 2 weeks) or 25-35 (monthly); or >= 2 in the last 13 months 55-70
    (every 2 months) or 85-100 (every 3 months) apart; or >= 2 in the last 25 months 170-195 (every 6 months) or 350-380
    (yearly) apart. Two charges are enough only for a bill-like merchant, and for yearly. A utility whose amounts don't
    fit the band is found by the same schedules alone (variable: its amount is the median of its last 3 charges, its
    usual range theirs). Amount, last date, next expected and paid_this_month come from core charges only. Same-day
    charges are summed. A bill is the merchant, not the account: its charges on any account count ("last paid from" is
    its last one's). Groceries/Eating Out/Other Shopping/Income/Transfers and transfer-looking names are excluded, as are
    merchants that skipped a whole extra cycle (stopped). Late: more than the cadence's longest interval + 5 days since
    the last charge.

    Tracked bills (tracked_bills, or `tracked` in its place for a preview): an active row makes its merchant a bill
    on its own schedule (source tracked; never excluded, never stopped); a stopped row hides it.
    """
    today = _today(today)
    since_yearly = _shift_months(today, -YEARLY_LOOKBACK_MONTHS).isoformat()
    rows = conn.execute(_REC_SELECT.format(where="v.date >= ? AND v.spend > 0"), (since_yearly,)).fetchall()
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key = merchant_key(r["merchant_name"], r["name"])
        if key:
            groups[key].append(r)
    marks = tracked_rows(conn) if tracked is None else tracked
    active = {t["merchant_key"]: t for t in marks if not t["stopped_at"]}
    stopped = {t["merchant_key"] for t in marks if t["stopped_at"]}

    items = []
    done: set[str] = set()
    for keys, txns in _merge_renames(groups):  # a merchant the bank renamed stays one item, under its latest name
        txns.sort(key=lambda t: t["date"])
        mine = [k for k in keys if k in active]
        if mine:
            items.append(_tracked_item(conn, active[mine[-1]], txns, keys, today))
            done.update(mine)
            continue
        if stopped.intersection(keys):
            continue
        last = txns[-1]
        key = keys[-1]
        merchant = (last["merchant_name"] or last["name"]).strip()
        if (last["category"] in RECURRING_EXCLUDED or last["is_income"] or last["is_transfer"]
                or TRANSFER_NAME_RE.search(merchant) or TRANSFER_NAME_RE.search(last["name"] or "")):
            continue
        kind = _kind(last["category"], last["pfc_detailed"], merchant)
        found = _find(_per_day(txns), today, kind == "bill", is_utility(last["pfc_detailed"], merchant))
        if not found:
            continue
        cadence, interval, core, variable = found
        step = math.floor(interval + 0.5)  # half up: a 30.5-day median is 31 days
        if (today - (core[-1][0] + timedelta(days=step))).days > step:  # missed a whole extra cycle: it stopped
            continue
        amounts = [a for _, a in core]
        logo, icon = _logo(txns)
        items.append(_item(
            merchant=merchant, key=key, keys=keys, kind=kind, category=last["category"], cadence=cadence,
            amount=statistics.median(amounts[-ESTIMATE_CHARGES:]) if variable else amounts[-1], variable=variable,
            step=step, charges=core, today=today, txns=txns, source="detected", logo=logo, icon=icon,
            usual=(_season(_per_day(txns), today) or _season(core, today)) if variable else None))
    for key, t in active.items():  # tracked, with no charge in the last 25 months (its anchor may still be there)
        if key not in done:
            items.append(_tracked_item(conn, t, [], [key], today))
    items.sort(key=lambda r: (r["next_expected"], r["merchant"].casefold()))
    totals = {"bill": 0.0, "subscription": 0.0}
    for r in items:
        totals[r["kind"]] += r["monthly_amount"]
    return {"today": today.isoformat(), "items": items, "count": len(items),
            "monthly_totals": {k: _r(v) for k, v in totals.items()}}


# --- 7. alerts ---------------------------------------------------------------------------------------

def alerts(conn: sqlite3.Connection, month: str, today: date | None = None,
           cats: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Cap alerts (warn / over, from spend + the category's bills still due this month) and broken connections.
    `key` is stable, for deduping notifications. polish P2e: a group budget alerts at the same thresholds (kind "cap",
    so the same Settings switches apply, with `group` instead of `category`); the caps inside it still alert on their own."""
    cats = cats if cats is not None else category_spend(conn, month, today)
    out = []
    for c in cats:
        if c["status"] not in ("warn", "over"):
            continue
        due = c.get("due", 0.0)
        of = f"of {money(c['cap'])}"
        amounts = f"{money(c['spend'])} spent + {money(due)} still due {of}" if due else f"{money(c['spend'])} {of}"
        when = " once bills clear" if due else ""
        if c["status"] == "over":
            msg = f"{c['category']} is over its cap{when}: {amounts}"
        else:
            pct = (c["spend"] + due) / c["cap"] * 100 if c["cap"] else 0.0
            msg = f"{c['category']} is at {pct:.0f}% of its cap{when}: {amounts}"
        out.append({"key": f"cap:{c['category']}:{month}:{c['status']}", "kind": "cap", "level": c["status"],
                    "category": c["category"], "spend": c["spend"], "due": due, "cap": c["cap"], "pct": c["pct"],
                    "left": c.get("left"), "message": msg})
    for g in groupbudget.units(conn, cats):
        if g["status"] not in ("warn", "over"):
            continue
        of = f"of {money(g['budget'])}"
        amounts = f"{money(g['spend'])} spent + {money(g['due'])} still due {of}" if g["due"] else f"{money(g['spend'])} {of}"
        when = " once bills clear" if g["due"] else ""
        if g["status"] == "over":
            msg = f"{g['group']} is over its group budget{when}: {amounts}"
        else:
            pct = (g["spend"] + g["due"]) / g["budget"] * 100 if g["budget"] else 0.0
            msg = f"{g['group']} is at {pct:.0f}% of its group budget{when}: {amounts}"
        out.append({"key": f"group:{g['group']}:{month}:{g['status']}", "kind": "cap", "level": g["status"],
                    "group": g["group"], "spend": g["spend"], "due": g["due"], "cap": g["budget"], "pct": g["pct"],
                    "left": g["left"], "message": msg})
    for it in conn.execute(
            """SELECT item_id, institution_name, institution_id, status, error_code, error_message FROM items
               WHERE removed_at IS NULL AND (status <> 'active' OR error_code IS NOT NULL)
               ORDER BY created_at"""):
        code = it["error_code"] or it["status"].upper()
        name = it["institution_name"] or it["institution_id"] or it["item_id"]
        relogin = code == "ITEM_LOGIN_REQUIRED" or it["status"] == "login_required"
        msg = f"{name} needs a re-login" if relogin else f"{name} has a connection problem ({code})"
        out.append({"key": f"item:{it['item_id']}:{code}", "kind": "connection", "level": "error",
                    "item_id": it["item_id"], "institution_name": name, "error_code": code,
                    "error_message": it["error_message"], "message": msg})
    return out


# --- 8. cash flow ------------------------------------------------------------------------------------

def first_transaction_dates(conn: sqlite3.Connection) -> dict[str, str]:
    """Earliest transaction date per connected bank (history depth differs a lot between banks)."""
    rows = conn.execute(
        """SELECT COALESCE(i.institution_name, i.institution_id, i.item_id) AS bank, MIN(t.date)
           FROM transactions t JOIN items i ON i.item_id = t.item_id
           WHERE t.removed = 0 AND i.removed_at IS NULL GROUP BY i.item_id ORDER BY bank""")
    out: dict[str, str] = {}
    for bank, first in rows:
        out[bank] = min(first, out.get(bank, first))
    return out


def cashflow(conn: sqlite3.Connection, months: int = 6, today: date | None = None,
             end: str | None = None) -> dict[str, Any]:
    """Income / spending / net per month, oldest first. A month is `partial` when it starts before a
    connected bank's first transaction; `missing` names those banks."""
    today = _today(today)
    end = end or month_of(today)
    first = first_transaction_dates(conn)
    out = []
    for m in (add_months(end, -i) for i in range(months - 1, -1, -1)):
        income, spending = _income(conn, m), _spend(conn, m)
        missing = sorted(bank for bank, f in first.items() if f > f"{m}-01")
        out.append({"month": m, "income": income, "spending": spending, "net": _r(income - spending),
                    "coverage": "partial" if missing else "full", "missing": missing,
                    "is_current": m == month_of(today)})
    return {"months": out, "first_dates": first}


# --- 9. net worth ------------------------------------------------------------------------------------

ASSET_TYPES = ("depository", "investment")
DEBT_TYPES = ("credit", "loan")
# Manual accounts (manual.py): balance is always >= 0, liabilities are subtracted by kind.
MANUAL_ASSET_KINDS = ("bank", "investment", "vehicle", "property", "other_asset")
MANUAL_DEBT_KINDS = ("credit_card", "loan", "other_liability")
CASH_TYPES = ("depository",)
MANUAL_CASH_KINDS = ("bank",)
# The Accounts page groups: (key, label, is_asset, Plaid account types, manual kinds). The two "other" groups
# only show when something is in them.
# Order (the owner's call, 2026-09-26): everything you own first, then everything you owe, so the list reads top to bottom
# like the Own / Owe bar above it.
BREAKDOWN = (
    ("cash", "Cash", True, CASH_TYPES, MANUAL_CASH_KINDS),
    ("investments", "Investments", True, ("investment",), ("investment",)),
    ("vehicles", "Vehicles", True, (), ("vehicle",)),
    ("property", "Property", True, (), ("property",)),
    ("other_assets", "Other assets", True, (), ("other_asset",)),
    ("cards", "Credit cards", False, ("credit",), ("credit_card",)),
    ("loans", "Loans", False, ("loan",), ("loan",)),
    ("other_debts", "Other debts", False, (), ("other_liability",)),
)
GROUP_OF_TYPE = {t: key for key, _, _, types, _ in BREAKDOWN for t in types}
GROUP_OF_KIND = {k: key for key, _, _, _, kinds in BREAKDOWN for k in kinds}
ASSET_GROUPS = frozenset(key for key, _, is_asset, _, _ in BREAKDOWN if is_asset)
# The "Cash & debts" selection (TASK-14's mode; since TASK-15 a preset of Custom and its first saved selection): Cash
# and every debt group count, Investments, Vehicles, Property and Other assets don't.
CASH_ONLY_GROUPS = tuple(key for key, _, is_asset, types, _ in BREAKDOWN if not is_asset or types == CASH_TYPES)
# The Custom net worth (TASK-15): every account counts unless its id is in the `networth_excluded` setting, a JSON list
# of ids (a Plaid account_id, or "manual:<id>"). What's stored is what's left out, so an account connected or added
# later counts by default.
NETWORTH_EXCLUDED = "networth_excluded"
MAX_NETWORTH_EXCLUDED = 1000


def manual_nw_id(account_id: int) -> str:
    return f"manual:{account_id}"


def _manual_live(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, kind, balance FROM manual_accounts WHERE archived_at IS NULL").fetchall()


def networth_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every account net worth counts (and Custom can leave out): the Plaid accounts of connected Items with an asset
    or debt type, then the non-archived manual accounts. id is the one networth_excluded stores; kind is the Plaid type
    or the manual kind; group the Accounts page group; balance is positive for debts too, signed negative."""
    out = []
    for a in [*conn.execute(
            """SELECT a.account_id, a.name, a.mask, a.type, a.current_balance, i.institution_name, i.institution_id
               FROM accounts a JOIN items i ON i.item_id = a.item_id
               WHERE i.removed_at IS NULL AND a.type IN ('depository', 'investment', 'credit', 'loan')
               ORDER BY i.created_at, a.type, a.name"""), *alpaca.networth_rows(conn)]:  # TASK-19: Alpaca, when ACTIVE
        bal = a["current_balance"] or 0.0
        out.append({"id": a["account_id"], "source": "plaid", "name": a["name"], "mask": a["mask"],
                    "institution": a["institution_name"] or a["institution_id"], "kind": a["type"],
                    "group": GROUP_OF_TYPE[a["type"]], "balance": bal,
                    "signed": _r(bal if a["type"] in ASSET_TYPES else -bal)})
    for m in conn.execute("SELECT id, name, institution, kind, balance FROM manual_accounts WHERE archived_at IS NULL "
                          "ORDER BY id"):
        out.append({"id": manual_nw_id(m["id"]), "source": "manual", "name": m["name"], "mask": None,
                    "institution": m["institution"], "kind": m["kind"], "group": GROUP_OF_KIND[m["kind"]],
                    "balance": m["balance"], "signed": _r(m["balance"] if m["kind"] in MANUAL_ASSET_KINDS else -m["balance"])})
    return out


def cash_preset(conn: sqlite3.Connection) -> list[str]:
    """The "Cash & debts" selection as a Custom one: the ids of every investment, vehicle, property and other-asset
    account (all cash and all debts count)."""
    return [a["id"] for a in networth_accounts(conn) if a["group"] not in CASH_ONLY_GROUPS]


def _id_list(value: str) -> list[str]:
    try:
        ids = json.loads(value)
    except ValueError:
        return []
    return [x for x in ids if isinstance(x, str)] if isinstance(ids, list) else []


def networth_excluded(conn: sqlite3.Connection) -> list[str]:
    """The saved Custom selection: the ids left out. On a database without one, the first read saves the "Cash & debts"
    selection as it stands (so Custom starts out as exactly TASK-14's "Cash & debts") and logs it, once; a read-only
    connection (the agent read commands) gets the same selection without saving it."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (NETWORTH_EXCLUDED,)).fetchone()
    if row is not None:
        return _id_list(row["value"])
    ids = cash_preset(conn)
    try:
        with conn:
            saved = conn.execute("INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                                 (NETWORTH_EXCLUDED, json.dumps(ids), now())).rowcount
    except sqlite3.DatabaseError:  # read-only (or busy): the same selection, saved by the next read that can write
        return ids
    if not saved:  # another connection saved one in between
        return networth_excluded(conn)
    log.warning("net worth: Custom starts as the Cash & debts selection (%d account(s) not counted: every investment, "
                "vehicle, property and other asset); saved once as the %s setting", len(ids), NETWORTH_EXCLUDED)
    return ids


def save_networth_excluded(conn: sqlite3.Connection, ids: Iterable[str]) -> tuple[list[str], list[str]]:
    """Save the Custom selection: the ids left out, once each, in the order given. An id that isn't one of
    networth_accounts() (unknown, an archived manual account, a removed bank's) is dropped. Returns (saved, dropped)."""
    known = {a["id"] for a in networth_accounts(conn)}
    kept: list[str] = []
    dropped: list[str] = []
    for x in ids:
        if x not in kept and x not in dropped:
            (kept if x in known else dropped).append(x)
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                     "SET value = excluded.value, updated_at = excluded.updated_at", (NETWORTH_EXCLUDED, json.dumps(kept), now()))
    return kept, dropped


def _month_start(conn: sqlite3.Connection, today: date) -> dict[str, float | None]:
    """Each Accounts group's total at the start of the current month (positive, like `total` in net_worth()), from each
    account's latest balance on or before the first of the month: balance_snapshots for Plaid accounts, Alpaca's daily
    equity (TASK-19) and manual_balance_history for manual accounts — the same sources as net_worth_history(). None for a
    group with no live accounts, or where any live account in it has no balance that early (a bank connected after it, a
    manual account added this month)."""
    first = date(today.year, today.month, 1).isoformat()
    groups: dict[str, list[float | None]] = {key: [] for key, *_ in BREAKDOWN}
    for r in conn.execute(
            """SELECT a.type AS type, (
                       SELECT s.current FROM balance_snapshots s
                       WHERE s.account_id = a.account_id AND s.current IS NOT NULL AND s.date <= ?
                       ORDER BY s.date DESC LIMIT 1) AS baseline
               FROM accounts a JOIN items i ON i.item_id = a.item_id
               WHERE i.removed_at IS NULL AND a.type IN ('depository', 'investment', 'credit', 'loan')""",
            (first,)).fetchall():
        groups[GROUP_OF_TYPE[r["type"]]].append(r["baseline"])
    alp = alpaca.history_rows(conn) if alpaca.networth_rows(conn) else []   # only while today's total counts it
    if alp:  # TASK-19: the Alpaca live account's equity on the last snapshot day on or before the first
        prior = [r for r in alp if r["date"] <= first]
        groups[GROUP_OF_TYPE["investment"]].append(prior[-1]["current"] if prior else None)
    for r in conn.execute(
            """SELECT m.kind AS kind, (
                       SELECT h.balance FROM manual_balance_history h
                       WHERE h.manual_account_id = m.id AND h.date <= ?
                       ORDER BY h.date DESC LIMIT 1) AS baseline
               FROM manual_accounts m WHERE m.archived_at IS NULL""",
            (first,)).fetchall():
        groups[GROUP_OF_KIND[r["kind"]]].append(r["baseline"])
    return {key: (None if not vals or any(v is None for v in vals) else _r(sum(vals)))
            for key, vals in groups.items()}


def net_worth(conn: sqlite3.Connection, history: bool = True, excluded: Iterable[str] | None = None,
              today: date | None = None) -> dict[str, Any]:
    """Assets (depository + investment, plus manual assets) minus debts (credit + loan, plus manual
    liabilities), from current balances of accounts on connected Items and non-archived manual accounts.
    `groups` is the Plaid part by account type, `manual` the manual part, `breakdown` the Accounts page groups
    (count + total, and each group's `custom` part), `institutions` the Plaid accounts by bank. History comes only from
    balance_snapshots and manual_balance_history. `custom` is the Custom net worth: every account except the ones
    `excluded` (default: the saved selection, networth_excluded()), with the accounts it leaves out.
    TASK-22: with `today`, `in_transit` lists the card payments in transit (transit.py), each counted as reducing its
    card's balance: debts (and Custom's, while the card counts there) are that much lower, and so is today's history
    point's. The groups and the accounts keep the balances the banks report."""
    skip = set(networth_excluded(conn) if excluded is None else excluded)
    groups = {t: 0.0 for t in ASSET_TYPES + DEBT_TYPES}
    counts: dict[str, int] = defaultdict(int)
    banks: dict[str, dict[str, Any]] = {}
    for a in [*conn.execute(
            """SELECT a.account_id, a.name, a.official_name, a.mask, a.type, a.subtype, a.current_balance,
                      a.available_balance, a.credit_limit, a.iso_currency, i.item_id, i.institution_name,
                      i.institution_id, i.status, i.error_code
               FROM accounts a JOIN items i ON i.item_id = a.item_id
               WHERE i.removed_at IS NULL ORDER BY i.created_at, a.type, a.name"""),
              *alpaca.networth_rows(conn)]:  # TASK-19: the Alpaca live account (read-only API), counted while ACTIVE
        bal = a["current_balance"] or 0.0
        signed = bal if a["type"] in ASSET_TYPES else -bal if a["type"] in DEBT_TYPES else None
        if a["type"] in groups:
            groups[a["type"]] += bal
            counts[a["type"]] += 1
        bank = banks.setdefault(a["item_id"], {
            "item_id": a["item_id"], "institution_name": a["institution_name"] or a["institution_id"],
            "institution_id": a["institution_id"],
            "status": a["status"], "error_code": a["error_code"], "net": 0.0, "accounts": []})
        bank["net"] += signed or 0.0
        bank["accounts"].append({
            "account_id": a["account_id"], "name": a["name"], "official_name": a["official_name"],
            "mask": a["mask"], "type": a["type"], "subtype": a["subtype"],
            "current": a["current_balance"], "available": a["available_balance"], "limit": a["credit_limit"],
            "iso_currency": a["iso_currency"], "signed": None if signed is None else _r(signed)})
    manual_by_kind: dict[str, float] = defaultdict(float)
    manual_counts: dict[str, int] = defaultdict(int)
    for m in _manual_live(conn):
        manual_by_kind[m["kind"]] += m["balance"]
        manual_counts[m["kind"]] += 1
    m_assets = _r(sum(manual_by_kind[k] for k in MANUAL_ASSET_KINDS))
    m_debts = _r(sum(manual_by_kind[k] for k in MANUAL_DEBT_KINDS))
    assets = _r(sum(groups[t] for t in ASSET_TYPES) + m_assets)
    debts = _r(sum(groups[t] for t in DEBT_TYPES) + m_debts)
    for b in banks.values():
        b["net"] = _r(b["net"])
        if b["item_id"] == alpaca.ITEM_ID:  # TASK-19: connected by API, not Plaid (no slot); its status and positions
            b.update(alpaca.account_extra(conn))
            for acct in b["accounts"]:
                acct.update(api=True, positions=b.get("positions"))
    c_total: dict[str, float] = defaultdict(float)   # Custom: the counted accounts, by group
    c_count: dict[str, int] = defaultdict(int)
    left_out = []
    for a in networth_accounts(conn):
        if a["id"] in skip:
            left_out.append({k: a[k] for k in ("id", "source", "name", "mask", "institution", "kind", "group", "signed")}
                            | {"balance": _r(a["balance"])})
        else:
            c_total[a["group"]] += a["balance"]
            c_count[a["group"]] += 1
    c_assets = _r(sum(v for g, v in c_total.items() if g in ASSET_GROUPS))
    c_debts = _r(sum(v for g, v in c_total.items() if g not in ASSET_GROUPS))
    ms = _month_start(conn, _today(today))  # each group's total at the start of the current month
    breakdown = []
    for key, label, is_asset, types, kinds in BREAKDOWN:
        total = _r(sum(groups[t] for t in types) + sum(manual_by_kind[k] for k in kinds))
        count = sum(counts[t] for t in types) + sum(manual_counts[k] for k in kinds)
        if key.startswith("other_") and not count:
            continue
        ctot = _r(c_total[key])
        signed = total if is_asset else _r(-total)
        start = ms[key]
        if start is None:
            month_start = month_change = None
        else:
            signed_start = start if is_asset else -start
            amount = _r(signed - signed_start)  # the growth of this group's net-worth contribution this month
            month_start = start
            month_change = {"amount": amount, "pct": _r(amount / abs(signed_start) * 100) if signed_start else None}
        breakdown.append({"key": key, "label": label, "asset": is_asset, "count": count, "total": total,
                          "signed": signed,
                          "month_start": month_start, "month_change": month_change,
                          "manual_count": sum(manual_counts[k] for k in kinds),
                          "custom": {"count": c_count[key], "total": ctot, "signed": ctot if is_asset else _r(-ctot)}})
    in_transit = transit_entries(conn, today, skip) if today else []
    debts = _r(debts - sum(e["amount"] for e in in_transit))
    c_debts = _r(c_debts - sum(e["amount"] for e in in_transit if e["counted_custom"]))
    out = {"assets": assets, "debts": debts, "net": _r(assets - debts),
           "custom": {"assets": c_assets, "debts": c_debts, "net": _r(c_assets - c_debts), "excluded": left_out},
           "in_transit": in_transit,
           "groups": {k: _r(v) for k, v in groups.items()},
           "manual": {"assets": m_assets, "debts": m_debts, "count": sum(manual_counts.values())},
           "breakdown": breakdown, "institutions": list(banks.values())}
    if history:
        out.update(net_worth_history(conn, skip))
        point = next((p for p in out["history"] if p["date"] == today.isoformat()), None) if in_transit else None
        if point:   # today's point, as the figure above
            moved = sum(e["amount"] for e in in_transit)
            c_moved = sum(e["amount"] for e in in_transit if e["counted_custom"])
            point.update(debts=_r(point["debts"] - moved), net=_r(point["net"] + moved),
                         custom_debts=_r(point["custom_debts"] - c_moved), custom_net=_r(point["custom_net"] + c_moved))
    return out


def transit_entries(conn: sqlite3.Connection, today: date, excluded: Iterable[str] = ()) -> list[dict[str, Any]]:
    """TASK-22: transit.entries, each with whether Custom counts its card (counted_custom)."""
    from . import transit   # (it imports manual, which imports this module)
    skip = set(excluded)
    return [e | {"counted_custom": e["card_id"] not in skip} for e in transit.entries(conn, today)]


def net_worth_history(conn: sqlite3.Connection, excluded: Iterable[str] = ()) -> dict[str, Any]:
    """One point per snapshot date (and per manual balance change from the first snapshot on). Each Plaid
    account contributes its latest real snapshot on or before that date, and nothing before its first one;
    each manual account its latest history row on or before that date, until the day it was archived.
    custom_assets / custom_debts / custom_net: the same point counting only the accounts Custom counts today (the Custom
    line: today's selection applied to every day, so changing it never makes the line jump). An archived manual account
    never counts in Custom (it isn't in the selection at all), so it's in no point of that line."""
    skip = set(excluded)
    rows = conn.execute(
        """SELECT s.date, s.account_id, s.current, a.type FROM balance_snapshots s
           JOIN accounts a ON a.account_id = s.account_id JOIN items i ON i.item_id = a.item_id
           WHERE i.removed_at IS NULL AND a.type IN ('depository', 'investment', 'credit', 'loan')
           ORDER BY s.date""").fetchall() + alpaca.history_rows(conn)  # TASK-19: Alpaca's daily equity (0 if not ACTIVE)
    manual = conn.execute(
        """SELECT h.manual_account_id AS id, h.date, h.balance, m.kind, m.archived_at AS archived
           FROM manual_balance_history h JOIN manual_accounts m ON m.id = h.manual_account_id
           ORDER BY h.date, h.manual_account_id""").fetchall()
    by_day: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    days = set(by_day)
    first = min(days) if days else None
    days |= {m["date"] for m in manual if first is None or m["date"] >= first}
    known: dict[str, tuple[str, float]] = {}
    known_m: dict[int, tuple[str, float, str | None]] = {}
    points = []
    i = 0
    for day in sorted(days):
        for r in by_day.get(day, ()):
            if r["current"] is not None:
                known[r["account_id"]] = (r["type"], r["current"])
        while i < len(manual) and manual[i]["date"] <= day:
            m = manual[i]
            known_m[m["id"]] = (m["kind"], m["balance"], local_day(m["archived"]))  # archived that local day
            i += 1
        plaid = [(aid in skip, t, v) for aid, (t, v) in known.items()]
        live = [(arch is not None or manual_nw_id(mid) in skip, k, v) for mid, (k, v, arch) in known_m.items()
                if not (arch and day >= arch)]
        assets = debts = c_assets = c_debts = 0.0
        for out_, t, v in plaid:
            if t in ASSET_TYPES:
                assets += v
                c_assets += 0.0 if out_ else v
            elif t in DEBT_TYPES:
                debts += v
                c_debts += 0.0 if out_ else v
        for out_, k, v in live:
            if k in MANUAL_ASSET_KINDS:
                assets += v
                c_assets += 0.0 if out_ else v
            elif k in MANUAL_DEBT_KINDS:
                debts += v
                c_debts += 0.0 if out_ else v
        points.append({"date": day, "assets": _r(assets), "debts": _r(debts), "net": _r(assets - debts),
                       "custom_assets": _r(c_assets), "custom_debts": _r(c_debts), "custom_net": _r(c_assets - c_debts)})
    return {"history": points, "tracking_since": points[0]["date"] if points else None}
