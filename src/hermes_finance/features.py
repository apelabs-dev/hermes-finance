"""HTTP routes for TASK-19 (Batch C): goals, splits / notes / tags / saved filters, the bills calendar, budget rollover
and the Alpaca key; and TASK-21's bills tracked by hand (Track as a bill, Edit schedule, Stop tracking). web.py dispatches
here after its own gate, exactly like api.py's routes; each handler turns its module's error into api.ApiError (status +
the form field it belongs to).

The gate (web.App.handle, TASK-18's access modes) comes first for every one of them, the Alpaca key included: "This
computer only" refuses anything proxied or not for localhost, "Behind your proxy login" needs an allowed identity, and
"Built-in login" needs a session plus its CSRF token on every POST / DELETE. The server refuses to start when it would be
reachable from other devices with no login mode set (access.startup_problems), so these routes are never exposed
without one.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Callable

from . import alpaca, api, billcal, bills, goals, rollover, rulebook, txmeta

ALPACA_KEY = "/api/alpaca/key"


def _call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except (txmeta.TxMetaError, goals.GoalError, rollover.RolloverError, billcal.CalendarError, bills.BillError) as e:
        raise api.ApiError(str(e), e.status, e.field) from None
    except rulebook.RuleError as e:   # TASK-24
        raise api.ApiError(str(e), e.status) from None
    except alpaca.Refused as e:
        raise api.ApiError(str(e), 400, "secret") from None
    except alpaca.AlpacaError as e:
        raise api.ApiError(f"Couldn't reach Alpaca: {e}", 502, "secret") from None


def _id(data: dict[str, Any]) -> Any:
    return data.get("id")


# --- GET -------------------------------------------------------------------------------------------------------------

def goals_page(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    return goals.payload(conn, today, archived=params.get("archived") in ("1", "true"))


def tags(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    return {"tags": txmeta.all_tags(conn)}


def saved_filters(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """The saved filters, each with how many transactions it matches in ?month= (the month the page shows)."""
    month = api.month_param(params.get("month"), today)
    period = {k: params[k] for k in ("month", "since", "until") if params.get(k)} if params.get("window") else {"month": month}
    out = []
    for f in txmeta.saved_filters(conn):
        try:
            n = api.transactions(conn, {**f["filters"], **period, "limit": "1"}, today)["total"]
        except api.ApiError:  # a filter that no longer applies (a category renamed): shown with no count
            n = None
        out.append({**f, "count": n})
    return {"month": month, "filters": out}


def bills_calendar(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    month = api.month_param(params.get("month"), today)
    rec = api.recurring_page(conn, {}, today)  # the /api/recurring payload itself (logos cleaned)
    return _call(billcal.calendar_month, conn, month, today, rec)


def bill_lookup(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """TASK-21: a transaction's merchant as a bill (?transaction_id=, the transaction sheet) or a bill's own sheet (?key=,
    from Recurring): its bill or none, what Track as a bill starts with, its latest charges."""
    bill = _call(bills.lookup, conn, today, params.get("transaction_id") or None, params.get("key"))
    if bill["bill"]:
        bill["bill"] = api._clean_images(bill["bill"])
    return bill


def _bill_args(d: dict[str, Any]) -> dict[str, Any]:
    """The Track as a bill sheet's fields: the merchant (key, or the transaction it was opened from), every, varies,
    usually (a number, [low, high] or text such as "100-150"; empty: none)."""
    unknown = sorted(set(d) - {"action", "key", "transaction_id", "every", "varies", "usually"})
    if unknown:
        raise api.ApiError(f"unknown field {str(unknown[0])[:40]!r}")
    out = {k: d[k] for k in ("key", "transaction_id", "every", "varies") if d.get(k) is not None}
    if "usually" in d:
        out["usually"] = d["usually"]
    if "key" not in out and "transaction_id" not in out:
        raise api.ApiError("key or transaction_id is required (the merchant to track)", field="merchant")
    return out


def bill_preview(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """What Track as a bill would save, before it does: the bill as it would show, and the change in plain words."""
    args = _bill_args({k: v for k, v in params.items() if k != "action"})
    if "varies" in args:
        args["varies"] = args["varies"] in ("1", "true")
    p = _call(bills.plan, conn, today, params.get("action") or "track", **args)
    return {"summary": p["summary"], "bill": p["bill"] and api._clean_images(p["bill"])}


def _bill_save(conn: sqlite3.Connection, d: dict[str, Any], today: date) -> dict[str, Any]:
    action = d.get("action", "track")
    if action not in ("track", "update"):
        raise api.ApiError("action must be track or update (Stop tracking is /api/bills/stop)", field="action")
    p = _call(bills.plan, conn, today, action, **_bill_args(d))
    out = _call(bills.apply, conn, p, today)
    return {**out, "bill": out["bill"] and api._clean_images(out["bill"])}


def _bill_stop(conn: sqlite3.Connection, d: dict[str, Any], today: date) -> dict[str, Any]:
    p = _call(bills.plan, conn, today, "stop", **_bill_args({k: v for k, v in d.items() if k in ("key", "transaction_id")}))
    return _call(bills.apply, conn, p, today)


def alpaca_state(conn: sqlite3.Connection, params: dict[str, str], today: date) -> dict[str, Any]:
    """Whether a key is saved and what the last snapshot says (never the key itself)."""
    return {**alpaca.key_state(), "items": alpaca.items_payload(conn), "account": alpaca.account_extra(conn) or None}


GET_ROUTES: dict[str, Callable[[sqlite3.Connection, dict[str, str], date], Any]] = {
    "/api/goals": goals_page,
    "/api/tags": tags,
    "/api/saved-filters": saved_filters,
    "/api/bills-calendar": bills_calendar,
    "/api/alpaca": alpaca_state,
    "/api/bills/lookup": bill_lookup,      # TASK-21
    "/api/bills/preview": bill_preview,
    "/api/rules": lambda conn, p, t: rulebook.payload(conn),   # TASK-24: Settings -> Categorization rules
}


# --- POST ------------------------------------------------------------------------------------------------------------

def _alpaca_connect(conn: sqlite3.Connection, data: dict[str, Any], today: date) -> dict[str, Any]:
    unknown = sorted(set(data) - {"key_id", "secret"})
    if unknown:
        raise api.ApiError(f"unknown field {str(unknown[0])[:40]!r}")
    return _call(alpaca.connect, conn, data.get("key_id"), data.get("secret"), today)


POST_ROUTES: dict[str, Callable[[sqlite3.Connection, dict[str, Any], date], Any]] = {
    "/api/transactions/split": lambda conn, d, t: _call(txmeta.split, conn, d.get("transaction_id"), d.get("lines")),
    "/api/transactions/meta": lambda conn, d, t: _call(txmeta.set_meta, conn, d.get("transaction_id"),
                                                       {k: v for k, v in d.items() if k in ("note", "tags")}),
    "/api/saved-filters": lambda conn, d, t: _call(txmeta.save_filter, conn, d.get("name"), d.get("filters")),
    "/api/saved-filters/delete": lambda conn, d, t: _call(txmeta.delete_filter, conn, _id(d)),
    "/api/budgets/rollover": lambda conn, d, t: _call(rollover.set_enabled, conn, d.get("category"), d.get("enabled")),
    "/api/goals": lambda conn, d, t: _call(goals.create, conn, d, t),
    "/api/goals/edit": lambda conn, d, t: _call(goals.edit, conn, _id(d), {k: v for k, v in d.items() if k != "id"}, t),
    "/api/goals/entry": lambda conn, d, t: _call(goals.add_entry, conn, _id(d), {k: v for k, v in d.items() if k != "id"}, t),
    "/api/goals/dismiss": lambda conn, d, t: _call(goals.dismiss, conn, d.get("account_id"), t),
    ALPACA_KEY: _alpaca_connect,
    "/api/bills/track": _bill_save,        # TASK-21: Track as a bill / Edit schedule
    "/api/bills/stop": _bill_stop,         # TASK-21: Stop tracking
    # TASK-24: the rules manager (rulebook.py)
    "/api/rules/update": lambda conn, d, t: _call(rulebook.update, conn, _id(d), d.get("category"), d.get("pattern")),
    "/api/rules/keep": lambda conn, d, t: _call(rulebook.keep, conn, _id(d)),
    "/api/rules/delete": lambda conn, d, t: _call(rulebook.delete, conn, _id(d)),
    "/api/rules/restore": lambda conn, d, t: _call(rulebook.restore, conn, d.get("undo")),
    "/api/rules/suggestions/accept": lambda conn, d, t: _call(rulebook.accept, conn, d.get("key")),
    "/api/rules/suggestions/dismiss": lambda conn, d, t: _call(rulebook.dismiss, conn, d.get("key")),
}

# --- DELETE ----------------------------------------------------------------------------------------------------------

DELETE_ROUTES: dict[str, Callable[[sqlite3.Connection, date], Any]] = {
    ALPACA_KEY: lambda conn, t: _call(alpaca.disconnect, conn),
}
