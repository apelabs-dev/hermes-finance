"""The bills calendar on Recurring (TASK-19): the recurring charges /api/recurring finds (insights.recurring, not a copy
of it), laid out on a month.

For a month, each recurring item puts a chip on:
  * its last charge's day, if that falls in the month: paid (the amount is that charge, /api/recurring's last_amount);
  * its next expected day and the ones after it at its cadence (weekly / every 2 weeks: every 7 / 14 days; monthly,
    every 2 / 3 / 6 months, yearly: the same day 1 / 2 / 3 / 6 / 12 months on, clamped to the month's length), for the
    days in the month: due (late while the expected day has passed without a charge), at its amount (TASK-21: a bill
    whose amount changes, its estimate: variable). Days after the next one are projections.
The month total is the sum of the chips; the numbers gate checks it against /api/recurring worked out independently.
The calendar shows this month and the next 11 (earlier months would only know each item's last charge).

The week starts on Sunday unless the Region setting (TASK-18) says Monday; it's read defensively (settings key `region`,
{"week_start": "monday" | "sunday" | 0 | 1}, or a `week_start` key), since that setting isn't in this build yet.
"""

from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import insights

AHEAD_MONTHS = 11
STEP_DAYS = {"weekly": 7, "biweekly": 14}
STEP_MONTHS = {"monthly": 1, "bimonthly": 2, "quarterly": 3, "semiannual": 6, "yearly": 12}
WEEK_KEYS = ("week_start", "week_starts", "week_starts_on", "first_day_of_week", "weekStart")


class CalendarError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.field = field


def _week_value(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and v in (0, 1, 7):
        return 1 if v == 1 else 0
    if isinstance(v, str):
        w = v.strip().lower()
        if w.startswith("mon"):
            return 1
        if w.startswith("sun"):
            return 0
    return None


def week_start(conn: sqlite3.Connection) -> tuple[int, str]:
    """(0 = Sunday, 1 = Monday), and where it came from. Anything unreadable: Sunday."""
    try:
        rows = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings WHERE key IN ('region', 'week_start')")}
    except sqlite3.Error:
        return 0, "default"
    for key in ("region", "week_start"):
        if key not in rows:
            continue
        try:
            value = json.loads(rows[key])
        except (TypeError, ValueError):
            value = rows[key]
        if isinstance(value, dict):
            for k in WEEK_KEYS:
                w = _week_value(value.get(k))
                if w is not None:
                    return w, f"settings:{key}.{k}"
        else:
            w = _week_value(value)
            if w is not None:
                return w, f"settings:{key}"
    return 0, "default"


def _add_months_day(d: date, n: int, anchor_day: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    m += 1
    return date(y, m, min(anchor_day, calendar.monthrange(y, m)[1]))


def _cycle(nxt: date, cadence: str, k: int) -> date:
    """The date of the k-th cycle after `next_expected` at `cadence` (month steps keep the anchor day, clamped)."""
    if cadence in STEP_DAYS:
        return nxt + timedelta(days=STEP_DAYS[cadence] * k)
    return _add_months_day(nxt, STEP_MONTHS[cadence] * k, nxt.day)


def _first_cycle_at_or_after(nxt: date, cadence: str, start: date) -> int:
    """The smallest k whose cycle is on or after `start`. The walk starts there instead of at `next_expected`, so a
    years-stale bill does not spend the whole 400-step budget before it reaches the window."""
    if cadence in STEP_DAYS:
        days = STEP_DAYS[cadence]
        return max(0, -((start - nxt).days // -days))       # ceil((start - nxt) / days)
    months = STEP_MONTHS[cadence]
    ahead = (start.year - nxt.year) * 12 + (start.month - nxt.month)
    k = max(0, ahead // months - 1)          # one cycle early; the loop below lands on the first one at or after it
    while _add_months_day(nxt, months * k, nxt.day) < start:
        k += 1
    return k


def occurrences(item: dict[str, Any], start: date, end: date, today: date) -> list[dict[str, Any]]:
    """One recurring item's chips from `start` to `end` (inclusive)."""
    out = []
    last = date.fromisoformat(item["last_date"])
    if start <= last <= end:
        out.append({"date": last.isoformat(), "state": "paid", "projected": False})
    nxt = date.fromisoformat(item["next_expected"])
    step = item["cadence"]
    k = _first_cycle_at_or_after(nxt, step, start) if nxt < start else 0
    d = _cycle(nxt, step, k)
    limit = k + 400       # at most 400 cycles drawn per window; the jump is what keeps a stale bill inside it
    while d <= end and k < limit:
        if d >= start:
            state = "late" if item["status"] == "late" and k == 0 else "due"
            out.append({"date": d.isoformat(), "state": state, "projected": k > 0})
        k += 1
        d = _cycle(nxt, step, k)
    return out


def months(today: date) -> tuple[str, str]:
    first = insights.month_of(today)
    return first, insights.add_months(first, AHEAD_MONTHS)


def calendar_month(conn: sqlite3.Connection, month: str, today: date, rec: dict[str, Any] | None = None) -> dict[str, Any]:
    first, last = months(today)
    if month < first or month > last:
        raise CalendarError(f"the calendar shows {first} to {last}", field="month")
    rec = rec if rec is not None else insights.recurring(conn, today)
    y, m = insights.parse_month(month)
    start, end = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    bills = []
    for item in rec["items"]:
        for occ in occurrences(item, start, end, today):
            paid = occ["state"] == "paid"
            bills.append({**occ, "merchant": item["merchant"], "key": item["key"],
                          "amount": item["last_amount"] if paid else item["amount"],
                          "variable": item["variable"] and not paid,   # due at its estimate (TASK-21)
                          "category": item["category"], "kind": item["kind"], "cadence": item["cadence"],
                          "cadence_label": item["cadence_label"], "source": item["source"],
                          "logo_url": item.get("logo_url"), "icon_url": item.get("icon_url")})
    bills.sort(key=lambda b: (b["date"], b["merchant"].casefold()))
    paid = [b for b in bills if b["state"] == "paid"]
    ws, src = week_start(conn)
    total = insights._r(sum(b["amount"] for b in bills))
    paid_total = insights._r(sum(b["amount"] for b in paid))
    return {"month": month, "today": today.isoformat(), "week_start": ws, "week_start_source": src,
            "bills": bills, "count": len(bills), "paid_count": len(paid), "total": total, "paid_total": paid_total,
            "due_total": insights._r(total - paid_total), "first_month": first, "last_month": last,
            "prev": insights.add_months(month, -1) if month > first else None,
            "next": insights.add_months(month, 1) if month < last else None}
