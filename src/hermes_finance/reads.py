"""Read-only answers for Hermes: `finance spend`, `budget-status`, `txns`, `networth`, `bills`, `loans`,
`holdings` and `cashflow`.

Every payload is built from the functions the dashboard API uses (api.py, insights.py, portfolio.py, manual.py),
so a number here is the number on the page. The CLI opens the DB read-only for these commands and nothing here
writes or calls Plaid. Money is signed as in the app: spend and money out are positive, refunds and income
negative. Dates are the local dates Plaid reports; "today" is the machine's local date.

Bad arguments raise ReadError (the CLI prints it and exits 2).
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import api, insights, manual, portfolio, report, txmeta, usage, valuation
from .money import money2

_r = insights._r

# --by: SQL group expression over `v_transactions v` (merchant_key is insights.merchant_key, registered by spend())
SPEND_GROUPS = {
    "category": "v.category",
    "merchant": "merchant_key(v.merchant_name, v.name)",
    "month": "v.month",
    "account": "v.account_id",
}
DAYS_RE = re.compile(r"([0-9]{1,5})d")
MONTH_RE = re.compile(r"[0-9]{4}-[0-9]{2}")            # ASCII digits only (\d also matches full-width ones)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FIRST_YEAR = 1901         # --month: the dashboard's month math looks back up to 3 months and stops at 1900
UNNAMED = "Account"       # how an account without a name is shown (and matched)
MAX_SINCE_DAYS = 3660     # --since Nd
MAX_NEXT_DAYS = 730       # bills --next Nd
MAX_MONTHS = 120          # cashflow --months
# The Accounts page groups Plaid accounts by type the way net worth's BREAKDOWN does.
TYPE_GROUP = {t: key for key, _, _, types, _ in insights.BREAKDOWN for t in types}


class ReadError(Exception):
    """A bad argument: the CLI prints it and exits 2."""


# --- periods and filters -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    """A month (filtered on `v.month`, as the dashboard does), a date range (either end may be open), or all."""
    kind: str                   # month | range | all
    start: str | None = None    # inclusive YYYY-MM-DD
    end: str | None = None      # inclusive YYYY-MM-DD
    month: str | None = None

    @classmethod
    def of_month(cls, month: str) -> Period:
        return cls("month", f"{month}-01", f"{month}-{insights.days_in_month(month):02d}", month)

    def sql(self) -> tuple[list[str], list[Any]]:
        if self.month:
            return ["v.month = ?"], [self.month]
        where, args = [], []
        for bound, op in ((self.start, ">="), (self.end, "<=")):
            if bound:
                where.append(f"v.date {op} ?")
                args.append(bound)
        return where, args

    def days(self) -> int | None:
        if not (self.start and self.end):
            return None
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days + 1

    def json(self) -> dict[str, Any]:
        return {"kind": self.kind, "month": self.month, "start": self.start, "end": self.end, "days": self.days()}

    def label(self) -> str:
        if self.month:
            return self.month
        if self.start:
            return f"{self.start}..{self.end}"
        return f"through {self.end}" if self.end else "all history"

    def previous(self, today: date) -> Period | None:
        """The previous equal period. A month compares like the Overview's "vs last month": the previous month up
        to the same day (all of it once the month is over; none before the month starts). A date range compares with
        the same number of days just before it. An open-ended range or all history has none."""
        if self.month:
            day = insights.days_elapsed(self.month, today)
            if not day:
                return None
            prev = insights.add_months(self.month, -1)
            dim = insights.days_in_month(prev)
            last = dim if self.month < insights.month_of(today) else min(day, dim)
            return Period("range", f"{prev}-01", f"{prev}-{last:02d}")
        n = self.days()
        if n is None:
            return None
        start = date.fromisoformat(self.start)
        try:
            return Period("range", (start - timedelta(days=n)).isoformat(), (start - timedelta(days=1)).isoformat())
        except OverflowError:  # a range so long that the one before it would start before year 1
            return None


@dataclass(frozen=True)
class Filters:
    period: Period
    category: str | None = None                 # the category's stored name
    merchant: str | None = None
    account: str | None = None                  # --account as given
    accounts: tuple[dict[str, Any], ...] = ()   # the accounts it matched
    limit: int = api.DEFAULT_LIMIT

    def sql(self, period: Period | None = None, split_aware: bool = False) -> tuple[list[str], list[Any]]:
        """Over `v_spend_lines v` (spend: a split line has its own category), or with split_aware over `v_transactions v`
        (txns: a split transaction matches the category of any of its lines)."""
        where, args = (period or self.period).sql()
        if self.category and split_aware:
            where.append(txmeta.CATEGORY_SQL)
            args += [self.category, self.category]
        elif self.category:
            where.append("v.category = ?")
            args.append(self.category)
        if self.merchant:
            where.append("(v.merchant_name LIKE ? ESCAPE '\\' OR v.name LIKE ? ESCAPE '\\')")
            args += [api._like(self.merchant)] * 2
        if self.accounts:
            where.append(f"v.account_id IN ({', '.join('?' * len(self.accounts))})")
            args += [a["account_id"] for a in self.accounts]
        return where, args

    def json(self) -> dict[str, Any]:
        return {"category": self.category, "merchant": self.merchant,
                "account": {"query": self.account, "matched": list(self.accounts)} if self.account else None}


def parse_month(value: str | None, today: date, flag: str = "--month") -> str:
    if value is None:
        return insights.month_of(today)
    try:
        if not MONTH_RE.fullmatch(value) or int(value[:4]) < FIRST_YEAR:
            raise ValueError(value)
        insights.parse_month(value)
    except ValueError:
        raise ReadError(f"{flag} must be YYYY-MM from {FIRST_YEAR} on (e.g. {insights.month_of(today)}), "
                        f"got {value!r}") from None
    return value


def _day(value: str, flag: str) -> date:
    if not DATE_RE.fullmatch(value):
        raise ReadError(f"{flag} must be a date as YYYY-MM-DD, got {value!r}")
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ReadError(f"{flag} must be a real date (YYYY-MM-DD), got {value!r}") from None
    if d.year < 1900:
        raise ReadError(f"{flag} is too far back (from 1900 on), got {value!r}")
    return d


def parse_days(value: str, flag: str, lo: int, hi: int, bare: bool = False) -> int:
    """'14d' (or '14' when `bare`) -> 14, within lo..hi."""
    m = DAYS_RE.fullmatch(value) or (re.fullmatch(r"([0-9]{1,5})", value) if bare else None)
    if not m:
        raise ReadError(f"{flag} must be a number of days like 14d, got {value!r}")
    n = int(m[1])
    if not lo <= n <= hi:
        raise ReadError(f"{flag} must be between {lo}d and {hi}d, got {value!r}")
    return n


def parse_period(today: date, month: str | None = None, since: str | None = None, until: str | None = None,
                 default: str = "month") -> Period:
    """--month, or --since (Nd = the last N days, today included; or YYYY-MM-DD) and --until (YYYY-MM-DD, default
    today). With none of them: this month (`default="month"`) or all history (`default="all"`)."""
    if month is not None and (since is not None or until is not None):
        raise ReadError("--month can't be combined with --since/--until")
    if month is not None:
        return Period.of_month(parse_month(month, today))
    start = None
    if since is not None:
        if DAYS_RE.fullmatch(since):
            start = today - timedelta(days=parse_days(since, "--since", 1, MAX_SINCE_DAYS) - 1)
        elif DATE_RE.fullmatch(since):
            start = _day(since, "--since")
        else:
            raise ReadError(f"--since must be Nd (e.g. 30d: the last 30 days, today included) or YYYY-MM-DD, "
                            f"got {since!r}")
    end = _day(until, "--until") if until is not None else None
    if start is None and end is None:
        return Period.of_month(insights.month_of(today)) if default == "month" else Period("all")
    if start is not None and end is None:
        end = today
    if start is not None and start > end:
        raise ReadError(f"--since {start} is after {'--until' if until is not None else 'today'} {end}")
    return Period("range", start.isoformat() if start else None, end.isoformat())


def _category(conn: sqlite3.Connection, name: str, spending: bool) -> str:
    row = conn.execute("SELECT name, is_income, is_transfer FROM categories WHERE name = ? COLLATE NOCASE",
                       (name.strip(),)).fetchone()
    if row is None:
        names = ", ".join(r[0] for r in conn.execute("SELECT name FROM categories ORDER BY name"))
        raise ReadError(f"unknown category {name!r}; categories: {names}")
    if spending and (row["is_income"] or row["is_transfer"]):
        raise ReadError(f"{row['name']} is not a spending category, so it has no spend; "
                        f"see `finance txns --category {row['name']!r}` or `finance cashflow`")
    return row["name"]


def _text(value: str, flag: str) -> str:
    value = value.strip()
    if not value:
        raise ReadError(f"{flag} can't be empty")
    if len(value) > api.MAX_TEXT:
        raise ReadError(f"{flag} is too long (at most {api.MAX_TEXT} characters)")
    return value


def account_label(name: str | None, mask: str | None) -> str:
    """How the CLI names an account: its cleaned name (report.account_name, TASK-22b) and ••last-4."""
    name = report.account_name(name, mask) or UNNAMED
    return f"{name} ••{mask}" if mask else name


def _accounts(conn: sqlite3.Connection, query: str) -> tuple[dict[str, Any], ...]:
    """Accounts matching --account: by id, then last digits, then name (case-insensitive), then part of the name."""
    q = _text(query, "--account")
    fold = q.casefold()
    rows = conn.execute(
        """SELECT a.account_id, a.name, a.official_name, a.mask, a.type, i.institution_name
           FROM accounts a LEFT JOIN items i ON i.item_id = a.item_id
           ORDER BY i.created_at, a.name, a.account_id""").fetchall()
    tiers = (lambda a: a["account_id"] == q,
             lambda a: a["mask"] == q,
             lambda a: (a["name"] or UNNAMED).casefold() == fold,
             lambda a: fold in (a["name"] or "").casefold() or fold in (a["official_name"] or "").casefold())
    for match in tiers:
        hit = [a for a in rows if match(a)]
        if hit:
            return tuple({"account_id": a["account_id"], "name": a["name"], "mask": a["mask"], "type": a["type"],
                          "institution": a["institution_name"]} for a in hit)
    known = ", ".join(account_label(a["name"], a["mask"]) + ("" if a["name"] else f" ({a['account_id']})")
                      for a in rows) or "none yet"
    raise ReadError(f"no account matches {query!r} (by last digits or name); accounts: {known}")


def parse_filters(conn: sqlite3.Connection, today: date, *, month: str | None = None, since: str | None = None,
                  until: str | None = None, category: str | None = None, merchant: str | None = None,
                  account: str | None = None, limit: int = api.DEFAULT_LIMIT, default_period: str = "month",
                  spending: bool = False) -> Filters:
    """The shared filters, validated. `spending`: the category must be a spending one (for spend totals)."""
    period = parse_period(today, month, since, until, default_period)
    if not 1 <= limit <= api.MAX_LIMIT:
        raise ReadError(f"--limit must be between 1 and {api.MAX_LIMIT}, got {limit}")
    return Filters(period,
                   category=_category(conn, category, spending) if category is not None else None,
                   merchant=_text(merchant, "--merchant") if merchant is not None else None,
                   account=account.strip() if account is not None else None,
                   accounts=_accounts(conn, account) if account is not None else (),
                   limit=limit)


# --- spend ----------------------------------------------------------------------------------------------

def _spend(conn: sqlite3.Connection, where: list[str], args: list[Any],
           group: str | None = None) -> dict[Any, tuple[float, int]]:
    return insights.spend_by(conn, " AND ".join(where) or "1 = 1", args, group)


def _group_labels(conn: sqlite3.Connection, by: str, where: list[str], args: list[Any]) -> dict[Any, str]:
    if by == "account":
        return {r["account_id"]: account_label(r["name"], r["mask"])
                for r in conn.execute("SELECT account_id, name, mask FROM accounts")}
    if by == "merchant":  # the latest spelling, as the Recurring page names a merchant
        labels = {}
        for r in conn.execute(f"SELECT v.merchant_name, v.name FROM v_transactions v WHERE {insights.SPENDING} "
                              f"AND ({' AND '.join(where) or '1 = 1'}) ORDER BY v.date, v.transaction_id", args):
            labels[insights.merchant_key(r["merchant_name"], r["name"])] = \
                (r["merchant_name"] or r["name"] or "").strip() or "(no name)"
        return labels
    return {}


def spend(conn: sqlite3.Connection, f: Filters, today: date, by: str | None = None) -> dict[str, Any]:
    """Real spending (not income, not transfers: the dashboard's rule; refunds net) for the filters, with the
    previous equal period, and optionally grouped (--by; groups are cut to --limit, the totals are not)."""
    if by is not None and by not in SPEND_GROUPS:
        raise ReadError(f"--by must be one of {', '.join(SPEND_GROUPS)}")
    conn.create_function("merchant_key", 2, insights.merchant_key, deterministic=True)
    where, args = f.sql()
    total, count = _spend(conn, where, args)[None]
    pending_total, pending_count = _spend(conn, [*where, "v.pending = 1"], args)[None]
    prev = f.period.previous(today)
    before = _spend(conn, *f.sql(prev))[None] if prev else None
    groups: list[dict[str, Any]] = []
    if by:
        expr = SPEND_GROUPS[by]
        prev_groups = _spend(conn, *f.sql(prev), expr) if prev and by != "month" else None
        labels = _group_labels(conn, by, where, args)
        groups = [{"key": k, "label": labels.get(k, k), "total": t, "count": n,
                   "share": _r(t / total * 100) if total > 0 else None,
                   "previous": prev_groups.get(k, (0.0, 0))[0] if prev_groups is not None else None}
                  for k, (t, n) in _spend(conn, where, args, expr).items()]
        groups.sort(key=(lambda g: g["key"]) if by == "month" else (lambda g: (-g["total"], g["label"].casefold())))
    return {
        "today": today.isoformat(),
        "period": f.period.json(),
        "filters": f.json(),
        "total": total,
        "count": count,
        "pending": {"total": pending_total, "count": pending_count},
        "previous": {**prev.json(), "total": before[0], "count": before[1]} if prev else None,
        "change": {"amount": _r(total - before[0]),
                   "pct": _r((total - before[0]) / before[0] * 100) if before[0] > 0 else None} if prev else None,
        "by": by,
        "groups": groups[:f.limit],
        "group_count": len(groups),
    }


# --- budget status --------------------------------------------------------------------------------------

def budget_status(conn: sqlite3.Connection, month: str, today: date) -> dict[str, Any]:
    """Each cap (the Budgets page's numbers) with a per-category pace projection, uncapped spend, and totals.
    Polish P2e: `groups` are the group budgets, each counted once in the totals; a cap inside one is still listed, marked
    `counted: false` with `in_group_budget`, and the uncapped spend leaves out the categories a group budget covers.
    Group roll-up (2026-10-22): `group_rest` is each capped group's uncapped spending; it's in the totals, so those
    categories aren't in `uncapped` either."""
    rec = insights.recurring(conn, today)
    cats = insights.category_spend(conn, month, today, rec)
    b = insights.budgets(conn, month, today, cats)
    pace = insights.pace_projection(conn, month, today, rec)
    projected = insights.category_pace(conn, month, today, rec, cats)
    covered = {n for g in [*b["group_budgets"], *b["group_rest"]] for n in g["categories"]}
    uncapped = [c for c in cats if c["cap"] is None and c["category"] not in covered]
    groups = [{"group": g["group"], "budget": g["budget"], "spent": g["spend"], "due": g["due"],
               "bills_due": [x["merchant"] for x in g["bills_due"]], "left": g["left"], "pct": g["pct"],
               "status": g["status"], "count": sum(c["count"] for c in cats if c["category"] in g["categories"]),
               "days_left": b["days_left"], "projection": _r(sum(projected[n] for n in g["categories"])),
               "projection_status": insights.cap_status(_r(sum(projected[n] for n in g["categories"])), g["budget"]),
               "categories": g["categories"], "caps": g["caps"], "caps_total": g["caps_total"],
               "over_note": g["over_note"]} for g in b["group_budgets"]]
    return {
        "today": today.isoformat(),
        "month": month,
        "is_current": month == insights.month_of(today),
        "days_in_month": insights.days_in_month(month),
        "days_left": b["days_left"],
        "caps": [{"category": c["category"], "cap": c["cap"], "spent": c["spend"], "due": c["due"],
                  "bills_due": [x["merchant"] for x in c["bills_due"]], "left": c["left"],
                  "pct": c["pct"], "status": c["status"], "count": c["count"], "days_left": b["days_left"],
                  "projection": projected[c["category"]],
                  "projection_status": insights.cap_status(projected[c["category"]], c["cap"]),
                  "last_month": c["last_month"], "avg3": c["avg3"],
                  **({k: c[k] for k in ("base_cap", "rollover")} if "rollover" in c else {}),
                  **({k: c[k] for k in ("counted", "in_group_budget")} if "in_group_budget" in c else {})}
                 for c in b["categories"]],
        "groups": groups,
        # group roll-up (2026-10-22): a capped group's uncapped spending, counted in that group and in the totals
        "group_rest": [{"group": r["group"], "categories": r["categories"], "spent": r["spend"], "due": r["due"]}
                       for r in b["group_rest"]],
        "totals": {"budgeted": b["total_cap"], "spent": b["total_spend"], "due": b["total_due"], "left": b["left"],
                   "per_day": b["per_day"], "count": b["count"], "over": b["over"], "warn": b["warn"],
                   "under": b["under"], "under_count": b["under_count"], "over_amount": b["over_amount"],
                   "bills_due": [{k: x[k] for k in ("merchant", "amount", "next_expected", "category")}
                                 for x in b["bills_due"]]},
        "uncapped": {"spent": _r(sum(c["spend"] for c in uncapped)), "count": sum(c["count"] for c in uncapped),
                     "categories": [{"category": c["category"], "spent": c["spend"], "count": c["count"]}
                                    for c in uncapped]},
        "spending": {"total": pace["mtd"], "projection": pace["projection"], "daily_pace": pace["daily_pace"],
                     "bills_due_total": pace["bills_due_total"], "prev_month_total": pace["prev_total"]},
    }


# --- transactions ---------------------------------------------------------------------------------------

SEARCH_COLUMNS = ("v.name", "v.merchant_name", "v.category", "v.account_name", "v.institution_name", "v.ai_note",
                  "printf('%.2f', ABS(v.amount))", *txmeta.SEARCH_SQL)  # TASK-19: its note and tags too


def _txn_row(t: dict[str, Any]) -> dict[str, Any]:
    return {"transaction_id": t["transaction_id"], "date": t["date"], "name": t["name"], "merchant": t["merchant"],
            "merchant_name": t["merchant_name"], "amount": t["amount"], "category": t["category"],
            "category_source": t["category_source"], "account": account_label(t["account_name"], t["account_mask"]),
            "account_name": t["account_name"], "account_mask": t["account_mask"],
            "institution_name": t["institution_name"], "pending": t["pending"], "ai_flagged": t["ai_flagged"],
            "review": t["review"], "ai_note": t["ai_note"], "is_income": t["is_income"], "is_transfer": t["is_transfer"],
            "split": t.get("split"), "note": t.get("note"), "tags": t.get("tags", [])}


def _group(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute("SELECT name FROM category_groups WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()
    if row is None:
        names = ", ".join(r[0] for r in conn.execute("SELECT name FROM category_groups ORDER BY sort, name"))
        raise ReadError(f"unknown category group {name!r}; groups: {names}")
    return row["name"]


def txns(conn: sqlite3.Connection, f: Filters, today: date, search: str | None = None, flagged: bool = False,
         uncategorized: bool = False, tag: str | list[str] | None = None, group: str | None = None,
         sort: str = api.DEFAULT_SORT) -> dict[str, Any]:
    """Transactions newest first, or in `sort`'s order (the Transactions page's query and rows). Every filter narrows;
    with several tags (polish P1), any of them matches."""
    where, args = f.sql(split_aware=True)
    if sort not in api.SORTS:
        raise ReadError(f"--sort must be one of {', '.join(api.SORTS)}")
    tags: list[str] = []
    for x in [tag] if isinstance(tag, str) else (tag or []):
        try:
            x = txmeta.clean_tag(x)
        except txmeta.TxMetaError as e:
            raise ReadError(f"--tag: {e}") from None
        if x.casefold() not in {y.casefold() for y in tags}:
            tags.append(x)
    if tags:
        where.append(txmeta.any_tag_sql(len(tags)))
        args += tags
    if group is not None:  # polish P1: a whole category group, a split purchase by its lines
        group = _group(conn, group)
        sql, gargs = api.category_filter([], [group])
        where.append(sql)
        args += gargs
    if search is not None:
        search = _text(search, "--search")
        where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in SEARCH_COLUMNS) + ")")
        args += [api._like(search)] * len(SEARCH_COLUMNS)
    if flagged:
        where.append(api.FLAGGED_SQL)
    if uncategorized:
        where.append(api.UNCATEGORIZED_SQL)
    page = api.transaction_page(conn, where, args, "all", f.limit, 0, sort)
    rows = [_txn_row(t) for t in page["rows"]]
    return {"today": today.isoformat(), "period": f.period.json(),
            "filters": {**f.json(), "search": search, "flagged": flagged, "uncategorized": uncategorized,
                        **({"tag": tags[0] if len(tags) == 1 else tags} if tags else {}),
                        **({"group": group} if group is not None else {}), "sort": sort},
            "total": page["total"], "count": len(rows), "limit": f.limit, "counts": page["counts"], "rows": rows}


# --- net worth ------------------------------------------------------------------------------------------

def networth(conn: sqlite3.Connection, today: date, history: bool = False, custom: bool = False,
             cash_only: bool = False) -> dict[str, Any]:
    """Net worth as the Accounts page shows it: the groups with their Plaid accounts (largest first) then the
    manual ones, and (with `history`) the daily points. Every account carries its nw_id (the id the Custom selection
    uses) and whether Custom counts it. `custom` is the page's Custom mode: assets, debts and net are Custom's, and each
    group lists only the accounts it counts, with their total (a group left out entirely isn't listed). The custom block
    is there either way. `cash_only` (the hidden --cash-only, kept for one release): TASK-14's "Cash & debts" output,
    the preset in place of the saved selection (and a cash_only block, cash_assets / cash_net in each point)."""
    nw = insights.net_worth(conn, excluded=insights.cash_preset(conn) if cash_only else None, today=today)
    left = {a["id"] for a in nw["custom"]["excluded"]}
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for inst in nw["institutions"]:
        for a in inst["accounts"]:
            if a["type"] in TYPE_GROUP:
                by_group[TYPE_GROUP[a["type"]]].append(
                    {"source": "plaid", "id": a["account_id"], "nw_id": a["account_id"],
                     "counted": a["account_id"] not in left, "name": a["name"], "mask": a["mask"],
                     "institution": inst["institution_name"], "type": a["type"], "subtype": a["subtype"],
                     "balance": a["current"], "signed": a["signed"], "limit": a["limit"], "as_of": None,
                     "stale": False, "approx": False})
    for accts in by_group.values():
        accts.sort(key=lambda a: -(a["balance"] or 0))
    for m in manual.summary(conn, today)["accounts"]:
        nw_id = insights.manual_nw_id(m["id"])
        by_group[m["group"]].append(
            {"source": "manual", "id": m["id"], "nw_id": nw_id, "counted": nw_id not in left, "name": m["name"],
             "mask": None, "institution": m["institution"], "type": m["kind"], "subtype": m["subtype_label"],
             "balance": m["balance"], "signed": m["signed"], "limit": m["credit_limit"], "as_of": m["as_of"],
             "stale": m["stale"], "approx": m.get("approx", False),
             **({"valuation": valuation_json(m)} if m.get("valuation") else {})})
    narrowed = custom or cash_only
    groups = []
    for g in nw["breakdown"]:
        c = g["custom"]
        if (cash_only and g["key"] not in insights.CASH_ONLY_GROUPS) or (custom and g["count"] and not c["count"]):
            continue
        if narrowed:
            groups.append({**{k: g[k] for k in ("key", "label", "asset")}, **c,
                           # the month change is only for the all-accounts view (a narrowed one would need it recomputed
                           # per selection), so it's None here
                           "month_start": None, "month_change": None,
                           "accounts": [a for a in by_group[g["key"]] if a["counted"]]})
        else:
            groups.append({**{k: g[k] for k in ("key", "label", "asset", "count", "total", "signed", "custom",
                                                "month_start", "month_change")},
                           "accounts": by_group[g["key"]]})
    top = nw["custom"] if narrowed else nw
    points = nw["history"]
    if cash_only:
        points = [{**p, "cash_assets": p["custom_assets"], "cash_net": p["custom_net"]} for p in points]
    return {
        "today": today.isoformat(),
        "mode": "cash_only" if cash_only else "custom" if custom else "all",
        "assets": top["assets"], "debts": top["debts"], "net": top["net"],
        "custom": nw["custom"],
        **({"cash_only": {k: nw["custom"][k] for k in ("assets", "debts", "net")}} if cash_only else {}),
        "groups": groups,
        # TASK-22: card payments in transit, counted in the net above (in Custom, only those to a card it counts)
        "in_transit": [e for e in nw["in_transit"] if e["counted_custom"] or not narrowed],
        "manual": nw["manual"],
        "tracking_since": nw["tracking_since"],
        "history": points if history else None,
    }


def networth_custom(conn: sqlite3.Connection, today: date, changed: bool | None = None) -> dict[str, Any]:
    """`finance networth-custom`: every account with its id and whether the Custom net worth counts it, and Custom's
    totals. changed: whether this run changed the saved selection (None: nothing was asked to change)."""
    nw = insights.net_worth(conn, history=False, today=today)
    left = {a["id"] for a in nw["custom"]["excluded"]}
    order = {key: i for i, (key, *_) in enumerate(insights.BREAKDOWN)}
    labels = {key: label for key, label, *_ in insights.BREAKDOWN}
    accounts = sorted(insights.networth_accounts(conn), key=lambda a: order[a["group"]])
    return {"today": today.isoformat(), "changed": changed, "excluded": [a["id"] for a in nw["custom"]["excluded"]],
            "custom": {k: nw["custom"][k] for k in ("assets", "debts", "net")}, "all": {k: nw[k] for k in ("assets", "debts", "net")},
            "accounts": [{**{k: a[k] for k in ("id", "source", "name", "mask", "institution", "kind", "group")},
                          "group_label": labels[a["group"]], "balance": _r(a["balance"]), "signed": a["signed"],
                          "counted": a["id"] not in left} for a in accounts]}


def valuation_json(m: dict[str, Any]) -> dict[str, Any]:
    """A vehicle / property's value: where it comes from (the estimate or the user) and as of when. basis: trade_in
    (a car's Carfax trade-in) | estimate (a home's Redfin Estimate)."""
    v = m["valuation"]
    est = v["estimate"]
    following = v["source"] == "estimate"
    return {"value": m["balance"], "source": v["source"], "label": est["source_label"] if following and est else
            ("Your value" if not following else "No estimate yet"),
            "provider": est["source"] if following and est else None,
            "basis": est["basis"] if following and est else None,
            "as_of": est["date"] if following and est else m["balance_as_of"],
            "estimate": {k: est[k] for k in ("value", "source", "date", "miles", "basis")} if est else None}


BASIS_WORDS = valuation.BASIS_WORDS


# --- bills, loans, holdings, cash flow ------------------------------------------------------------------

BILL_FIELDS = ("merchant", "key", "kind", "category", "amount", "cadence", "interval_days", "charges", "last_date",
               "next_expected", "days_until", "status", "paid_this_month", "monthly_amount",
               # TASK-21: the schedule's words, a changing amount's estimate and usual range, tracked by hand or found,
               # the account the last charge came from, and whether it was far from what the owner said is usual
               "cadence_label", "variable", "estimate", "amount_label", "usual", "source", "last_amount", "last_account",
               "outlier")


def bills(conn: sqlite3.Connection, today: date, next_days: int = 14) -> dict[str, Any]:
    """Recurring bills and subscriptions (the Recurring page's detector, and the bills tracked by hand) expected within
    `next_days`, including any whose expected date has already passed (status due_soon / late)."""
    rec = insights.recurring(conn, today)
    items = [{k: r[k] for k in BILL_FIELDS} for r in rec["items"] if r["days_until"] <= next_days]
    return {"today": today.isoformat(), "next_days": next_days,
            "until": (today + timedelta(days=next_days)).isoformat(), "count": len(items), "items": items,
            "recurring_count": rec["count"], "monthly_totals": rec["monthly_totals"]}


def loans(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    """Loans from the Loans & debt page: manual loans (TASK-8), and any loan a bank reports through Plaid."""
    d = portfolio.debt(conn, today)
    out = [{"id": l["id"], "manual": l["manual"], "name": l["name"], "institution": l["institution"],
            "type": l["subtype"], "type_label": l["type_label"], "remaining": l["balance"],
            "original_amount": l["original_amount"], "paid_pct": l["paid_pct"], "paid_off": l["paid_off"],
            "apr": l["apr"], "monthly_payment": l["monthly_payment"], "payoff": l["payoff"],
            "next_payment": l["next_payment"], "last_payment": l["last_payment"], "match_pattern": l["match_pattern"],
            "auto_updates": l.get("auto_updates", False), "applied_count": l.get("applied_count"),
            "balance_as_of": l["balance_as_of"], "stale": l["stale"]} for l in d["loans"]]
    return {"today": today.isoformat(), "count": len(out), "loans": out,
            "total": _r(sum(g["total"] for g in d["groups"] if g["key"].startswith("loan:"))),
            "monthly": d["monthly"]["loans"]}


def holdings(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    """The Investments page: accounts (Plaid + manual) and holdings, each with its % of the portfolio."""
    inv = portfolio.investments(conn, today)
    total = inv["total"]
    accounts = [{"id": a["id"], "name": a["name"], "institution": a["institution"], "mask": a["mask"],
                 "type": a["type"], "group": a["group"], "manual": a["manual"], "value": a["value"],
                 "pct": _r(a["value"] / total * 100) if total else None, "holdings": a["holdings"],
                 "as_of": a.get("as_of"), "stale": a["stale"]} for a in inv["accounts"]]
    rows = [{k: h[k] for k in ("account_id", "account_name", "mask", "ticker", "name", "type", "quantity", "price",
                               "value", "cost_basis", "is_cash", "pct")} for h in inv["holdings"]]
    return {"today": today.isoformat(), "total": total, "holdings_total": inv["holdings_total"],
            "priced_total": inv["priced_total"],
            "prices_as_of": inv["prices_as_of"], "accounts": accounts, "holdings": rows,
            "breakdown": inv["breakdown"], "changes": inv["changes"], "tracking_since": inv["tracking_since"]}


def cashflow(conn: sqlite3.Connection, today: date, months: int = 6) -> dict[str, Any]:
    """Income vs spending per month, oldest first (the Overview's cash flow)."""
    if not 1 <= months <= MAX_MONTHS:
        raise ReadError(f"--months must be between 1 and {MAX_MONTHS}, got {months}")
    flow = insights.cashflow(conn, months, today)
    return {"today": today.isoformat(), "months": flow["months"], "first_dates": flow["first_dates"]}


# --- text -----------------------------------------------------------------------------------------------

def _m(value: float | None) -> str:
    return report.money(value)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}%"


def _table(columns: list[str], rows: list[list[Any]]) -> str:
    return report.table(columns, [["-" if v is None else v for v in r] for r in rows], 40)


def _filters_text(f: dict[str, Any]) -> str:
    tags = f.get("tag") or []
    bits = [f"category {f['category']}" if f.get("category") else None,
            f"group {f['group']}" if f.get("group") else None,
            "tag " + " or ".join([tags] if isinstance(tags, str) else tags) if tags else None,
            f"merchant ~{f['merchant']!r}" if f.get("merchant") else None,
            "account " + ", ".join(account_label(a["name"], a["mask"]) for a in f["account"]["matched"])
            if f.get("account") else None,
            f"search {f['search']!r}" if f.get("search") else None,
            "flagged" if f.get("flagged") else None, "uncategorized" if f.get("uncategorized") else None]
    return "".join(f", {b}" for b in bits if b)


def _period_text(p: dict[str, Any]) -> str:
    return Period(p["kind"], p["start"], p["end"], p["month"]).label()


def spend_text(p: dict[str, Any]) -> str:
    lines = []
    if p["groups"]:
        lines.append(_table([p["by"], "spend", "count", "share", "previous"],
                            [[g["label"], _m(g["total"]), g["count"], _pct(g["share"]), _m(g["previous"])]
                             for g in p["groups"]]))
        if p["group_count"] > len(p["groups"]):
            lines.append(f"(top {len(p['groups'])} of {p['group_count']}; raise --limit for more)")
    elif p["by"]:
        lines.append("(no spending)")
    pend = p["pending"]
    lines.append(f"Spending {_period_text(p['period'])}{_filters_text(p['filters'])}: {_m(p['total'])} in "
                 f"{p['count']} transaction(s)" + (f", {_m(pend['total'])} of it pending" if pend["count"] else ""))
    if p["previous"]:
        c = p["change"]
        pct = f", {c['pct']:+.1f}%" if c["pct"] is not None else ""
        change = money2(c["amount"])
        change = change if change.startswith("-") else "+" + change
        lines.append(f"Previous {_period_text(p['previous'])}: {_m(p['previous']['total'])} "
                     f"({change}{pct})")
    lines.append("(income and transfers are not spending; refunds net against their category)")
    return "\n".join(lines)


STATUS_TEXT = {"ok": "ok", "warn": "80%+", "over": "OVER", "nocap": "-"}


def budget_status_text(p: dict[str, Any]) -> str:
    lines = []
    groups = p.get("groups") or []
    if groups:   # polish P2e: a group budget is one budget; the caps inside it are listed below but not counted
        lines.append(_table(["group budget", "budget", "spent", "still due", "left", "used", "status", "projected"],
                            [[g["group"], _m(g["budget"]), _m(g["spent"]), _m(g["due"]) if g["due"] else "-",
                              _m(g["left"]), _pct(g["pct"]), STATUS_TEXT[g["status"]],
                              _m(g["projection"]) + (" (over)" if g["projection_status"] == "over" else "")]
                             for g in groups]))
        for g in groups:
            if g["over_note"] is not None:
                lines.append(f"{g['group']}: its category caps add up to {_m(g['over_note'])}, "
                             f"more than the group budget {_m(g['budget'])}")
    if p["caps"]:
        lines.append(_table(["category", "cap", "spent", "still due", "left", "used", "status", "projected"],
                            [[c["category"] + (f" (in {c['in_group_budget']})" if c.get("in_group_budget") else ""),
                              _m(c["cap"]), _m(c["spent"]), _m(c["due"]) if c["due"] else "-",
                              _m(c["left"]), _pct(c["pct"]), STATUS_TEXT[c["status"]],
                              _m(c["projection"]) + (" (over)" if c["projection_status"] == "over" else "")]
                             for c in p["caps"]]))
        if any(c.get("counted") is False for c in p["caps"]):
            lines.append("A cap marked (in <group>) is inside a group budget: it isn't counted again in the totals.")
    elif not groups:
        lines.append("No caps set.")
    for r in p.get("group_rest") or []:
        what = " and ".join([*([f"{_m(r['spent'])} spent"] if r["spent"] or not r["due"] else []),
                             *([f"{_m(r['due'])} in bills still due"] if r["due"] else [])])
        lines.append(f"{r['group']}: also counts {what} in categories without a cap ({', '.join(r['categories'])})")
    t, u, s = p["totals"], p["uncapped"], p["spending"]
    due = (f" and {_m(t['due'])} in bills still due ({', '.join(b['merchant'] for b in t['bills_due'])})"
           if t["due"] else "")
    if t["left"] < 0:
        left = f"over budget by {_m(-t['left'])}" + (" once bills clear" if t["due"] else "")
    else:
        left = f"{_m(t['left'])} left"
    per_day = f", about {_m(t['per_day'])} a day" if t["per_day"] is not None and p["days_left"] and t["left"] > 0 else ""
    where = "budgets" if groups or p.get("group_rest") else "capped categories"
    lines.append(f"{p['month']}: budgeted {_m(t['budgeted'])}, spent {_m(t['spent'])} in {where}{due}: "
                 f"{left} · {p['days_left']} day(s) left{per_day}")
    if t["count"]:
        lines.append(f"({_m(t['under'])} left in {t['under_count']} budget(s) · {_m(t['over_amount'])} over in "
                     f"{t['over']})")
    top = ", ".join(f"{c['category']} {_m(c['spent'])}" for c in u["categories"] if c["spent"])
    lines.append(f"Uncapped: {_m(u['spent'])}" + (f" ({top})" if top else ""))
    lines.append(f"All spending {_m(s['total'])}, on pace for {_m(s['projection'])} "
                 f"(last month {_m(s['prev_month_total'])})")
    return "\n".join(lines)


def txns_text(p: dict[str, Any]) -> str:
    rows = [[t["date"], _m(t["amount"]), t["merchant"], t["category"], t["category_source"], t["account"],
             ",".join(x for x, on in (("pending", t["pending"]), ("flagged", t["ai_flagged"])) if on)]
            for t in p["rows"]]
    head = f"Transactions {_period_text(p['period'])}{_filters_text(p['filters'])}: {p['total']} match"
    first = "newest" if p["filters"].get("sort", api.DEFAULT_SORT) == api.DEFAULT_SORT else "first"
    more = f", showing the {first} {p['count']} (raise --limit for more)" if p["total"] > p["count"] else ""
    return "\n".join([_table(["date", "amount", "merchant", "category", "source", "account", "flags"], rows),
                      head + more])


def networth_text(p: dict[str, Any]) -> str:
    mode = p.get("mode")
    head = {"cash_only": f"Net worth (cash & debts only) {_m(p['net'])}: cash {_m(p['assets'])}, owe {_m(p['debts'])}",
            "custom": f"Net worth (custom) {_m(p['net'])}: own {_m(p['assets'])}, owe {_m(p['debts'])}"}
    lines = [head.get(mode, f"Net worth {_m(p['net'])}: own {_m(p['assets'])}, owe {_m(p['debts'])}")]
    for g in p["groups"]:
        lines.append(f"  {g['label']} ({g['count']}): {_m(g['signed'])}")
        for a in g["accounts"]:
            tag = " [manual]" if a["source"] == "manual" else ""
            if mode == "all" and not a["counted"]:
                tag += " [not counted in Custom]"
            stale = f" (stale, as of {a['as_of']})" if a["stale"] else ""
            val = ""
            if a.get("valuation"):
                v = a["valuation"]
                est = v["estimate"]
                other = (f"; {usage.LABELS[est['source']]} {BASIS_WORDS[est['basis']]} {_m(est['value'])} on {est['date']}"
                         if est and v["source"] == "user" else "")
                basis = (valuation.BASIS_LABELS[v["basis"]] + " · ") if v["provider"] else ""
                val = f" ({basis}{v['label']} · as of {v['as_of']}{other})"
            lines.append(f"    {account_label(a['name'], a['mask'])}{tag}  {_m(a['signed'])}{val}{stale}")
    for e in p.get("in_transit") or ():
        lines.append(f"  {e['line']}, counted as paid")
    left = p["custom"]["excluded"]
    if mode == "cash_only":
        lines.append("(investments, vehicles, property and other assets not counted; without --cash-only for all of it)")
    elif mode == "custom":
        lines.append(f"(not counted: {', '.join(account_label(a['name'], a['mask']) for a in left)}; without --custom for "
                     "all of it)" if left else "(every account counted)")
    elif left:
        lines.append(f"(Custom, finance networth --custom: {_m(p['custom']['net'])}, "
                     f"{len(left)} account{'s' if len(left) != 1 else ''} not counted)")
    if p["history"] is not None:
        if mode == "cash_only":
            lines.append(_table(["date", "cash", "debts", "net"],
                                [[h["date"], _m(h["cash_assets"]), _m(h["custom_debts"]), _m(h["cash_net"])] for h in p["history"]]))
        else:
            pre = "custom_" if mode == "custom" else ""
            lines.append(_table(["date", "assets", "debts", "net"],
                                [[h["date"], _m(h[pre + "assets"]), _m(h[pre + "debts"]), _m(h[pre + "net"])] for h in p["history"]]))
    elif p["tracking_since"]:
        lines.append(f"(tracking since {p['tracking_since']}; --history for the daily points)")
    return "\n".join(lines)


def networth_custom_text(p: dict[str, Any]) -> str:
    c, n = p["custom"], len(p["excluded"])
    state = {True: "Saved. ", False: "No change. ", None: ""}[p["changed"]]
    rows = [["yes" if a["counted"] else "no", a["group_label"], account_label(a["name"], a["mask"]), _m(a["signed"]), a["id"]]
            for a in p["accounts"]]
    return "\n".join([f"{state}Custom net worth {_m(c['net'])}: own {_m(c['assets'])}, owe {_m(c['debts'])} "
                      f"({n} of {len(p['accounts'])} account{'s' if len(p['accounts']) != 1 else ''} not counted; "
                      f"All is {_m(p['all']['net'])})",
                      _table(["counted", "group", "account", "balance", "id"], rows)])


def bill_amount_text(b: dict[str, Any]) -> str:
    """$465.10 for a fixed amount; ~$125 est. (usually $100–150) for one that changes (TASK-21)."""
    if not b["variable"]:
        return _m(b["amount"])
    u = b["usual"]
    return b["amount_label"] + (f" (usually {insights.usual_text(u['low'], u['high'])})" if u else "")


def bills_text(p: dict[str, Any]) -> str:
    rows = [[b["next_expected"], b["days_until"], b["merchant"], b["kind"], bill_amount_text(b), b["cadence_label"],
             b["status"], "yes" if b["paid_this_month"] else "no",
             account_label(b["last_account"]["name"], b["last_account"]["mask"]) if b["last_account"] else "-",
             ", ".join(x for x in ("tracked" if b["source"] == "tracked" else "",
                                   f"last charge {b['outlier']} than usual" if b["outlier"] else "") if x) or "-"]
            for b in p["items"]]
    return "\n".join([_table(["expected", "days", "merchant", "kind", "amount", "cadence", "status",
                              "paid this month", "last paid from", "notes"], rows),
                      f"Recurring charges expected by {p['until']} (next {p['next_days']} days, overdue ones "
                      f"included). Monthly totals: bills {_m(p['monthly_totals']['bill'])}, subscriptions "
                      f"{_m(p['monthly_totals']['subscription'])}"])


def loans_text(p: dict[str, Any]) -> str:
    def payoff(po: dict[str, Any]) -> str:
        return f"{po['payoff_month']} ({po['payments_left']} left)" if po["payoff_month"] else po["method"]

    def last(lp: dict[str, Any] | None) -> str:
        return f"{lp['date']} {_m(lp['amount'])}" if lp else "-"

    rows = [[l["name"] + ("" if l["manual"] else " [plaid]"), _m(l["remaining"]),
             "-" if l["apr"] is None else f"{l['apr']}%", _m(l["monthly_payment"]),
             "paid off" if l["paid_off"] else payoff(l["payoff"]), last(l["last_payment"])] for l in p["loans"]]
    return "\n".join([_table(["loan", "remaining", "apr", "payment", "payoff", "last payment"], rows),
                      f"Total remaining {_m(p['total'])}, {_m(p['monthly'])} a month"])


def holdings_text(p: dict[str, Any]) -> str:
    accts = _table(["account", "type", "value", "share"],
                   [[account_label(a["name"], a["mask"]) + (" [manual]" if a["manual"] else ""), a["type"],
                     _m(a["value"]), _pct(a["pct"])] for a in p["accounts"]])
    rows = _table(["holding", "account", "quantity", "price", "value", "share"],
                  [[h["ticker"] or h["name"], account_label(h["account_name"], h["mask"]), h["quantity"],
                    _m(h["price"]), _m(h["value"]), _pct(h["pct"])] for h in p["holdings"]])
    return "\n".join([accts, rows, f"Portfolio {_m(p['total'])} (prices as of {p['prices_as_of'] or '-'})"])


def cashflow_text(p: dict[str, Any]) -> str:
    rows = [[m["month"] + (" (so far)" if m["is_current"] else ""), _m(m["income"]), _m(m["spending"]), _m(m["net"]),
             m["coverage"] + (f" (no history yet from {', '.join(m['missing'])})" if m["missing"] else "")]
            for m in p["months"]]
    return _table(["month", "income", "spending", "net", "coverage"], rows)
