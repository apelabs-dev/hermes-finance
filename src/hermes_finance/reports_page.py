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

from . import api, insights, reports


def _month(params: dict[str, str], today: date) -> str:
    month = api.month_param(params.get("month"), today)
    if month > insights.month_of(today):
        raise api.ApiError("month is in the future")
    return month


def _bounds(month: str) -> tuple[str, str]:
    return f"{month}-01", f"{month}-{insights.days_in_month(month):02d}"


def spending(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """The Spending page: the selected month plus the 12-month heatmap, its category rows and the four KPIs."""
    month = _month(params, today)
    start, end = _bounds(month)
    try:
        year = reports.year_spending(conn, today, end_month=month)
    except reports.ReportError as e:
        raise api.ApiError(str(e)) from None
    return {"view": "spending", "today": today.isoformat(), "month": month, "period": {"start": start, "end": end},
            "year": year}


def cashflow(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """The Cash flow page: income sources, spending groups, what is left after spending, and the moves between
    accounts (listed, never drawn)."""
    month = _month(params, today)
    start, end = _bounds(month)
    try:
        flow = reports.money_flow(conn, start, end)
    except reports.ReportError as e:
        raise api.ApiError(str(e)) from None
    return {"view": "cashflow", "today": today.isoformat(), "month": month, "period": {"start": start, "end": end},
            **flow}


def forecast(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """The Forecast page: the next `days` days on the chosen accounts, the account ticks (q4), the detected paychecks
    that are waiting for an answer (q5) and the two named everyday-spending lines (q6)."""
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
