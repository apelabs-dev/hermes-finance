"""HTTP routes for the Reports pages (0.5, B2): the Spending year view, Cash flow and Forecast payloads.

`web.py` dispatches here after its own access gate, exactly like `api.py`'s and `features.py`'s routes. The numbers
come only from `reports.py` (one function per number); this module validates the query string, picks the month's
bounds and adds the few labels a page needs. Writes are settings, saved through `POST /api/settings` (api.SETTING_KEYS
carries `reports_paychecks` and `reports_forecast_accounts`), so there is nothing to write here.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Callable

from . import api, insights, reads, reports


def _month(params: dict[str, str], today: date) -> str:
    month = api.month_param(params.get("month"), today)
    if month > insights.month_of(today):
        raise api.ApiError("month is in the future")
    return month


def _bounds(month: str) -> tuple[str, str]:
    return f"{month}-01", f"{month}-{insights.days_in_month(month):02d}"


def _window(conn: sqlite3.Connection, params: dict[str, str], today: date) -> tuple[reads.Period, bool]:
    """The period a Spending or Cash flow request asks for, from the same parser the CLI and the agent tools use
    (`reads.parse_period`): `month`, or `since` (Nd or a date) with `until`, or `window=all`. The dates come from the
    URL, so each is checked here: a real date from 1900 on, a range that begins somewhere and never ends after today.
    Returns the period and whether it was `window=all`."""
    month, since, until = (params.get(k) or None for k in ("month", "since", "until"))
    window = params.get("window") or None
    if window is not None:
        if window != "all":
            raise api.ApiError("window must be all")
        if month or since or until:
            raise api.ApiError("window can't be combined with month, since or until")
        first = conn.execute("SELECT MIN(v.date) FROM v_transactions v").fetchone()[0]
        return reads.Period("range", first if isinstance(first, str) else today.isoformat(), today.isoformat()), True
    if month is not None and (since or until):
        raise api.ApiError("month can't be combined with since or until")
    if since is None and until is None:       # a month (the default): the page's own check, unchanged
        return reads.Period.of_month(_month(params, today)), False
    try:
        period = reads.parse_period(today, None, since, until, default="month")
    except reads.ReadError as e:
        raise api.ApiError(str(e).replace("--", "")) from None
    if not period.start:
        raise api.ApiError("until needs a since")
    if period.end > today.isoformat():
        raise api.ApiError("the range ends in the future")
    return period, False


def _kind(period: reads.Period, every: bool) -> dict[str, Any]:
    """What the page's time-window control shows: a month, a range of days (7d, 30d, a custom range) or all time."""
    return {"kind": "all" if every else period.kind, "days": period.days()}


def spending(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """The Spending page: the 12-month heatmap ending with the selected month, or, for a date range, the whole
    months (2 to 24) the range touches; its category rows and the four KPIs."""
    period, every = _window(conn, params, today)
    if period.kind == "month":
        end_month, months = period.month, reports.YEAR_MONTHS
    else:
        end_month = period.end[:7]
        months = (int(period.end[:4]) - int(period.start[:4])) * 12 + int(period.end[5:7]) - int(period.start[5:7]) + 1
        if every:
            months = reports.MAX_YEAR_MONTHS
        elif months > reports.MAX_YEAR_MONTHS:
            raise api.ApiError(f"Spending shows at most {reports.MAX_YEAR_MONTHS} months")
        months = max(months, 2)
    try:
        year = reports.year_spending(conn, today, months=months, end_month=end_month)
    except reports.ReportError as e:
        raise api.ApiError(str(e)) from None
    return {"view": "spending", "today": today.isoformat(), "month": end_month,
            "period": {"start": period.start, "end": period.end}, "window": _kind(period, every), "year": year}


def cashflow(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """The Cash flow page: income sources, spending groups, what is left after spending, and the moves between
    accounts (listed, never drawn), for a month or a date range."""
    period, every = _window(conn, params, today)
    try:
        flow = reports.money_flow(conn, period.start, period.end)
    except reports.ReportError as e:
        raise api.ApiError(str(e)) from None
    return {"view": "cashflow", "today": today.isoformat(), "month": period.month,
            "period": {"start": period.start, "end": period.end}, "window": _kind(period, every), **flow}


def forecast(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """The Forecast page: the next `days` days (or up to the date `until`) on the chosen accounts, the account ticks
    (q4), the detected paychecks that are waiting for an answer (q5) and the two named everyday-spending lines (q6)."""
    until = params.get("until") or None
    if until is not None and params.get("days"):
        raise api.ApiError("days can't be combined with until")
    if until is not None:
        try:
            days = (reads.parse_day(until, "until") - today).days
        except reads.ReadError as e:
            raise api.ApiError(str(e)) from None
        if not 1 <= days <= reports.MAX_FORECAST_DAYS:
            raise api.ApiError(f"until must be within the next {reports.MAX_FORECAST_DAYS} days")
    else:
        days = api.int_param(params.get("days"), reports.FORECAST_DAYS, 1, reports.MAX_FORECAST_DAYS, "days")
    try:
        payload = reports.forecast(conn, today, days)
    except reports.ReportError as e:
        raise api.ApiError(str(e)) from None
    table = reports.forecast_accounts(conn)
    saved = reports.forecast_account_choice(conn)
    chosen = {a["account_id"] for a in reports.selected_accounts(table, saved)}
    payload["account_options"] = {
        "currency": table["currency"],
        "accounts": [{**a, "ticked": a["account_id"] in chosen} for a in table["accounts"]],
        "default": table["default"], "unavailable": table["unavailable"], "excluded": table["excluded"],
        "other_currencies": table["other_currencies"], "saved": saved,
    }
    payload["view"] = "forecast"
    return payload


def activity(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """The drill-down that opens in place (q3): one category in one month, Gross / Refunds / Net and its rows."""
    month = _month(params, today)
    category_id = api.int_param(params.get("category_id"), 0, 1, 10**9, "category_id")
    start, end = _bounds(month)
    try:
        out = reports.category_activity(conn, category_id, start, end)
    except reports.ReportError as e:
        raise api.ApiError(str(e), 404 if "no category" in str(e) else 400) from None
    return {"view": "activity", "today": today.isoformat(), "month": month, "period": {"start": start, "end": end},
            **out}


GET_ROUTES: dict[str, Callable[[sqlite3.Connection, dict[str, str], date], Any]] = {
    "/api/reports/spending": spending,
    "/api/reports/cashflow": cashflow,
    "/api/reports/forecast": forecast,
    "/api/reports/activity": activity,
}
