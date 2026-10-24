"""Goals (TASK-19): the Goals page, its Overview tile, `finance goals` and the finance_goals tool.

Three kinds, all made from a template and only when you tap Create (nothing is made on its own):
  save     Save up: a target and an optional month to reach it by. Progress comes from one of
             account  a whole account's balance (a savings account, a vault);
             earmark  part of an account: an amount set aside inside it. The earmarks in one account can never add up to
                      more than its balance when they're set ("Unassigned in <account>: $X"); if the balance drops below
                      them later, the goals it no longer covers (newest first) are flagged short, never shrunk;
             manual   entries you log (+ add / withdraw), with their history.
           It shows %, what's left, "on pace for <month>" from the last 90 days (or "save $X a month to hit <month>"
           when a month is set), and when it's reached, "100% of $X saved" with archive / raise target.
  payoff   Pay off: a loan or a card, paid down from its balance when the goal was made (the loan's payoff estimate
           and payment come from the Loans & debt data).
  monthly  Monthly savings: income - spending this month (insights.cashflow's numbers) vs a target.
First run: savings-looking accounts (savings / vault / money market subtypes, or such names) are suggested with "Set a
target"; otherwise the page shows the three templates.

Goals never change net worth, budgets, alerts or any other total: nothing else reads these tables.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import insights, portfolio
from .money import money
from .db import now

_r = insights._r
ICONS = ("piggy-bank", "target", "plane", "car", "house", "shield-check", "graduation-cap", "gift",
         "heart", "briefcase", "rocket", "sprout")
COLOURS = ("#4fd1a5", "#8ea8ff", "#5fc6e8", "#c792ea", "#f5b454", "#ff8fb1", "#ff7b7b", "#9ba2b6")
KINDS = ("save", "payoff", "monthly")
TRACKS = ("account", "earmark", "manual")
MAX_NAME = 60
MAX_TARGET = 10_000_000
MAX_NOTE = 200
PACE_DAYS = 90
DISMISSED = "goals_dismissed_suggestions"   # settings key: account ids answered "Not a goal"
SAVINGS_SUBTYPES = {"savings", "cash management", "money market", "cd", "money_market"}
SAVINGS_NAME_RE = re.compile(r"savings|vault|emergency|rainy|fund|goal|travel|vacation|splurge", re.IGNORECASE)
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


class GoalError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.field = field


# --- accounts a goal can follow ---------------------------------------------------------------------------------------

def accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every current account (net worth's list: connected Plaid accounts, manual accounts, Alpaca) with its group."""
    return insights.networth_accounts(conn)


def _asset(a: dict[str, Any]) -> bool:
    return a["group"] in insights.ASSET_GROUPS and a["group"] not in ("vehicles", "property")


def _debt(a: dict[str, Any]) -> bool:
    return a["group"] not in insights.ASSET_GROUPS


def _label(a: dict[str, Any]) -> str:
    return f"{a['name'] or 'Account'} ••{a['mask']}" if a.get("mask") else (a["name"] or "Account")


def _account_json(a: dict[str, Any] | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {"id": a["id"], "name": a["name"], "mask": a["mask"], "institution": a["institution"], "kind": a["kind"],
            "group": a["group"], "balance": _r(a["balance"]), "label": _label(a)}


def _growth(conn: sqlite3.Connection, account_id: str, today: date) -> float:
    """Money into the account over the last 90 days: a Plaid account's posted transactions (Plaid sign flipped), a manual
    account's balance change, Alpaca's equity change."""
    start = (today - timedelta(days=PACE_DAYS)).isoformat()
    if account_id.startswith("manual:"):
        mid = int(account_id.split(":", 1)[1])
        now_row = conn.execute("SELECT balance FROM manual_accounts WHERE id = ?", (mid,)).fetchone()
        then = conn.execute("SELECT balance FROM manual_balance_history WHERE manual_account_id = ? AND date <= ? "
                            "ORDER BY date DESC LIMIT 1", (mid, start)).fetchone() or \
            conn.execute("SELECT balance FROM manual_balance_history WHERE manual_account_id = ? ORDER BY date LIMIT 1",
                         (mid,)).fetchone()
        return _r((now_row[0] if now_row else 0.0) - (then[0] if then else 0.0)) if now_row else 0.0
    if account_id.startswith("alpaca:"):
        rows = conn.execute("SELECT date, equity, status FROM alpaca_snapshots WHERE counted = 1 ORDER BY date").fetchall()
        live = [r for r in rows if r["status"] == "ACTIVE"]
        if not live:
            return 0.0
        then = next((r for r in reversed(live) if r["date"] <= start), live[0])
        return _r(live[-1]["equity"] - then["equity"])
    row = conn.execute("SELECT COALESCE(-SUM(amount), 0) FROM transactions WHERE account_id = ? AND removed = 0 "
                       "AND pending = 0 AND date > ? AND date <= ?", (account_id, start, today.isoformat())).fetchone()
    return _r(row[0])


# --- the goals -------------------------------------------------------------------------------------------------------

def _months_between(a: str, b: str) -> int:
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def _pace(progress: float, target: float, growth: float, target_month: str | None, today: date) -> dict[str, Any]:
    """Estimate growth and whole dollars needed monthly for a YYYY-MM deadline.

    A deadline lasts through the target month's final day. Months left is the calendar-month
    difference (day-of-month does not affect it); zero uses one saving period to avoid division
    by zero. Earlier months are past due, with no required pace. Reached goals and goals without
    a date have no required pace. Compute in cents, then round UP to whole dollars.
    """
    this = insights.month_of(today)
    left = max(target - progress, 0.0)
    per_month = _r(growth / 3)
    out: dict[str, Any] = {"growth_90d": _r(growth), "per_month": per_month, "on_pace_month": None, "months_to_go": None,
                           "need_per_month": None, "months_left": None, "past_due": False}
    if left <= 0:
        return out
    if per_month >= 0.5:
        n = math.ceil(left / per_month - 1e-9)
        if n <= 1200:
            out.update(on_pace_month=insights.add_months(this, n), months_to_go=n)
    if target_month:
        n = _months_between(this, target_month)
        if n < 0:
            out.update(past_due=True, months_left=0)
        else:
            n = max(n, 1)
            cents = max(round(target * 100) - round(progress * 100), 0)
            out.update(need_per_month=(cents + n * 100 - 1) // (n * 100), months_left=n)
    return out


def _entries(conn: sqlite3.Connection, goal_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT id, date, amount, note FROM goal_entries WHERE goal_id = ? "
                                          "ORDER BY date DESC, id DESC", (goal_id,))]


def _entries_growth(entries: list[dict[str, Any]], today: date) -> float:
    start = (today - timedelta(days=PACE_DAYS)).isoformat()
    return _r(sum(e["amount"] for e in entries if start < e["date"] <= today.isoformat()))


def _earmark_cover(goals: list[sqlite3.Row], by_id: dict[str, dict[str, Any]]) -> dict[int, float]:
    """goal id -> how much of its earmark the account's balance covers now: the oldest goals first."""
    cover: dict[int, float] = {}
    per_account: dict[str, list[sqlite3.Row]] = {}
    for g in goals:
        if g["kind"] == "save" and g["track"] == "earmark" and g["archived_at"] is None:
            per_account.setdefault(g["account_id"], []).append(g)
    for aid, gs in per_account.items():
        left = max(by_id[aid]["balance"], 0.0) if aid in by_id else 0.0
        for g in sorted(gs, key=lambda x: x["id"]):
            take = min(g["earmark"] or 0.0, left)
            cover[g["id"]] = _r(take)
            left -= take
    return cover


def _debts(conn: sqlite3.Connection, today: date) -> dict[str, dict[str, Any]]:
    """Loans & debt rows by the id a goal stores (Plaid account_id or manual:<id>)."""
    d = portfolio.debt(conn, today)
    out = {}
    for x in d["loans"] + d["cards"] + d["other"]:
        key = f"manual:{x['id']}" if x["manual"] else x["id"]
        out[key] = x
    return out


def _goal_json(conn: sqlite3.Connection, g: sqlite3.Row, today: date, by_id: dict[str, dict[str, Any]],
               cover: dict[int, float], flow: dict[str, Any], debts: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    acct = by_id.get(g["account_id"]) if g["account_id"] else None
    out: dict[str, Any] = {"id": g["id"], "kind": g["kind"], "name": g["name"], "target": g["target"],
                           "icon": g["icon"], "colour": g["colour"],
                           "target_month": g["target_month"], "track": g["track"], "account_id": g["account_id"],
                           "account": _account_json(acct), "earmark": g["earmark"], "start_balance": g["start_balance"],
                           "created_at": g["created_at"], "archived_at": g["archived_at"], "missing_account": False,
                           "short": None, "entries": [], "pace": None, "payoff": None}
    if g["kind"] == "monthly":
        cur = flow["months"][-1]
        progress = cur["net"]
        out.update(progress=progress, month=cur["month"], income=cur["income"], spending=cur["spending"],
                   months=flow["months"])
    elif g["kind"] == "payoff":
        start = g["start_balance"] or 0.0
        if acct is None:
            out["missing_account"] = True
            progress = 0.0
        else:
            progress = _r(max(start - acct["balance"], 0.0))
        out["target"] = start
        d = (debts or {}).get(g["account_id"])
        if d is not None and d.get("payoff"):
            po = d["payoff"]
            out["payoff"] = {"payoff_month": po.get("payoff_month"), "payments_left": po.get("payments_left"),
                             "method": po.get("method"), "monthly_payment": d.get("monthly_payment")}
        out["remaining"] = _r(acct["balance"]) if acct else None
    elif g["track"] == "account":
        if acct is None:
            out["missing_account"] = True
        progress = _r(acct["balance"]) if acct else 0.0
        out["pace"] = _pace(progress, g["target"], _growth(conn, g["account_id"], today) if acct else 0.0, g["target_month"], today)
    else:  # earmark or manual: what was set aside / logged
        entries = _entries(conn, g["id"])
        out["entries"] = entries[:20]
        out["entry_count"] = len(entries)
        if g["track"] == "earmark":
            progress = _r(g["earmark"] or 0.0)
            if acct is None:
                out["missing_account"] = True
            covered = cover.get(g["id"], 0.0)
            if covered + 0.005 < progress:
                out["short"] = {"amount": _r(progress - covered), "covered": covered}
        else:
            progress = _r(sum(e["amount"] for e in entries))
        out["pace"] = _pace(progress, g["target"], _entries_growth(entries, today), g["target_month"], today)
    target = out["target"]
    out["progress"] = progress
    out["pct"] = _r(progress / target * 100) if target else None
    out["left"] = _r(max(target - progress, 0.0)) if target else None
    out["done"] = bool(target) and round(progress * 100) >= round(target * 100)
    return out


def _rows(conn: sqlite3.Connection, archived: bool = False) -> list[sqlite3.Row]:
    try:
        return conn.execute("SELECT * FROM goals" + ("" if archived else " WHERE archived_at IS NULL") + " ORDER BY id").fetchall()
    except sqlite3.OperationalError:  # a database from before TASK-19 (read-only)
        return []


def _dismissed(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (DISMISSED,)).fetchone()
    try:
        ids = json.loads(row["value"]) if row else []
    except ValueError:
        return []
    return [x for x in ids if isinstance(x, str)] if isinstance(ids, list) else []


def suggestions(conn: sqlite3.Connection, rows: list[sqlite3.Row], by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Savings-looking accounts no goal follows yet (and not answered "Not a goal")."""
    used = {g["account_id"] for g in rows if g["archived_at"] is None and g["account_id"]}
    skip = used | set(_dismissed(conn))
    out = []
    for r in conn.execute("""SELECT a.account_id, a.subtype, a.name FROM accounts a JOIN items i ON i.item_id = a.item_id
                             WHERE i.removed_at IS NULL AND a.type = 'depository' ORDER BY i.created_at, a.name"""):
        if r["account_id"] in skip or r["account_id"] not in by_id:
            continue
        if (r["subtype"] or "").lower() in SAVINGS_SUBTYPES or SAVINGS_NAME_RE.search(r["name"] or ""):
            out.append({**_account_json(by_id[r["account_id"]]), "subtype": r["subtype"],
                        "vault": (r["subtype"] or "").lower() == "cash management"})
    for m in conn.execute("SELECT id, subtype, name FROM manual_accounts WHERE archived_at IS NULL AND kind = 'bank' ORDER BY id"):
        mid = insights.manual_nw_id(m["id"])
        if mid in skip or mid not in by_id:
            continue
        if (m["subtype"] or "").lower() in SAVINGS_SUBTYPES or SAVINGS_NAME_RE.search(m["name"] or ""):
            out.append({**_account_json(by_id[mid]), "subtype": m["subtype"], "vault": False})
    return out


def payload(conn: sqlite3.Connection, today: date, archived: bool = False) -> dict[str, Any]:
    """The Goals page: the goals (with progress), the accounts they can follow (unassigned per account), suggestions."""
    rows = _rows(conn, archived=True)
    accts = accounts(conn)
    by_id = {a["id"]: a for a in accts}
    cover = _earmark_cover(rows, by_id)
    flow = insights.cashflow(conn, 6, today)
    needs_debts = any(g["kind"] == "payoff" for g in rows)
    debts = _debts(conn, today) if needs_debts else None
    live = [g for g in rows if g["archived_at"] is None]
    goals = [_goal_json(conn, g, today, by_id, cover, flow, debts) for g in live]
    earmarked: dict[str, float] = {}
    whole: dict[str, str] = {}
    for g in live:
        if g["kind"] == "save" and g["track"] == "earmark":
            earmarked[g["account_id"]] = earmarked.get(g["account_id"], 0.0) + (g["earmark"] or 0.0)
        elif g["kind"] == "save" and g["track"] == "account":
            whole[g["account_id"]] = g["name"]
    assets = [{**_account_json(a), "earmarked": _r(earmarked.get(a["id"], 0.0)),
               "unassigned": _r(max(a["balance"] - earmarked.get(a["id"], 0.0), 0.0)), "whole_goal": whole.get(a["id"])}
              for a in accts if _asset(a)]
    saves = [g for g in goals if g["kind"] == "save" and g["target"]]
    monthly = next((g for g in goals if g["kind"] == "monthly"), None)
    return {
        "appearance": {"icons": list(ICONS), "colours": list(COLOURS)},
        "today": today.isoformat(),
        "month": insights.month_of(today),
        "goals": goals,
        "archived": [_goal_json(conn, g, today, by_id, cover, flow, debts) for g in rows if g["archived_at"]] if archived else None,
        "archived_count": sum(1 for g in rows if g["archived_at"]),
        "monthly": monthly,
        "cashflow": flow["months"],
        "suggestions": suggestions(conn, rows, by_id),
        "accounts": assets,
        "debts": [{**_account_json(a)} for a in accts if _debt(a) and a["balance"] > 0],
        "totals": {"count": len(goals), "save_count": len(saves), "saving_target": _r(sum(g["target"] for g in saves)),
                   "saved": _r(sum(min(g["progress"], g["target"]) for g in saves)),
                   "pct": _r(sum(min(g["progress"], g["target"]) for g in saves) / sum(g["target"] for g in saves) * 100)
                   if saves else None},
    }


def tile(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    """The Overview's Goals tile: the Monthly savings target (the tile shows the month's income - spending against it) and
    the Save up goal closest to done."""
    rows = _rows(conn)
    if not rows:
        return {"count": 0, "monthly": None, "top": None}
    p = payload(conn, today)
    saves = [g for g in p["goals"] if g["kind"] == "save" and g["target"] and not g["missing_account"]]
    open_ = [g for g in saves if not g["done"]]
    top = max(open_ or saves, key=lambda g: (g["pct"] or 0, -g["id"]), default=None)
    m = p["monthly"]
    return {"count": len(p["goals"]),
            "monthly": {k: m[k] for k in ("target", "progress", "icon", "colour")} if m else None,
            "top": {k: top[k] for k in ("name", "progress", "target", "pct", "icon", "colour")} if top else None}


# --- writes ----------------------------------------------------------------------------------------------------------

def _appearance(value: Any, field: str) -> str:
    choices = ICONS if field == "icon" else COLOURS
    if not isinstance(value, str) or value not in choices:
        raise GoalError(f"pick a {field} from the available choices", field=field)
    return value


def _name(value: Any, default: str | None = None) -> str:
    if value is None and default:
        return default
    if not isinstance(value, str) or not " ".join(value.split()):
        raise GoalError("give the goal a name", field="name")
    value = " ".join(value.split())
    if len(value) > MAX_NAME:
        raise GoalError(f"a name is up to {MAX_NAME} characters", field="name")
    return value


def _amount(value: Any, field: str, what: str, lo: float = 0.0, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GoalError(f"{what} must be a number", field=field)
    v = round(float(value), 2)
    if v < lo or (v == 0 and not allow_zero) or v > MAX_TARGET:
        raise GoalError(f"{what} must be {'at least $0' if allow_zero else 'more than $0'} and at most {money(MAX_TARGET)}",
                        field=field)
    return v


def _month(value: Any, today: date) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        raise GoalError("the month must be YYYY-MM", field="target_month")
    if value < insights.month_of(today):
        raise GoalError("pick this month or a later one", field="target_month")
    return value


def unassigned(conn: sqlite3.Connection, account_id: str, balance: float, except_goal: int | None = None) -> float:
    """What's left in an account after the other goals' earmarks in it."""
    taken = conn.execute("SELECT COALESCE(SUM(earmark), 0) FROM goals WHERE kind = 'save' AND track = 'earmark' "
                         "AND archived_at IS NULL AND account_id = ? AND id IS NOT ?", (account_id, except_goal)).fetchone()[0]
    return _r(max(balance - taken, 0.0))


def _check_earmark(conn: sqlite3.Connection, a: dict[str, Any], amount: float, except_goal: int | None = None) -> None:
    free = unassigned(conn, a["id"], a["balance"], except_goal)
    if round(amount * 100) > round(free * 100):
        raise GoalError(f"only {money(free)} is unassigned in {_label(a)}: set aside that much or less", field="earmark")


def _whole_conflict(conn: sqlite3.Connection, account_id: str, track: str, except_goal: int | None = None) -> None:
    rows = conn.execute("SELECT name, track FROM goals WHERE kind = 'save' AND archived_at IS NULL AND account_id = ? "
                        "AND id IS NOT ?", (account_id, except_goal)).fetchall()
    for r in rows:
        if r["track"] == "account":
            raise GoalError(f"the goal {r['name']!r} already follows this whole account", field="account_id")
        if track == "account" and r["track"] == "earmark":
            raise GoalError(f"part of this account is set aside for {r['name']!r}: follow part of it too, or change that goal",
                            field="account_id")


def create(conn: sqlite3.Connection, data: dict[str, Any], today: date) -> dict[str, Any]:
    kind = data.get("kind")
    if kind not in KINDS:
        raise GoalError("kind must be save (Save up), payoff (Pay off) or monthly (Monthly savings)", field="kind")
    by_id = {a["id"]: a for a in accounts(conn)}
    ts = now()
    fields: dict[str, Any] = {"kind": kind, "created_at": ts, "updated_at": ts}
    entry = None
    if kind == "monthly":
        if conn.execute("SELECT 1 FROM goals WHERE kind = 'monthly' AND archived_at IS NULL").fetchone():
            raise GoalError("there's a Monthly savings goal already: change its target instead", field="kind")
        fields.update(name=_name(data.get("name"), "Monthly savings"),
                      target=_amount(data.get("target"), "target", "The monthly target"))
    elif kind == "payoff":
        a = by_id.get(data.get("account_id"))
        if a is None or not _debt(a):
            raise GoalError("pick a loan or a card to pay off", field="account_id")
        if a["balance"] <= 0:
            raise GoalError(f"{_label(a)} has nothing owed", field="account_id")
        if conn.execute("SELECT 1 FROM goals WHERE kind = 'payoff' AND archived_at IS NULL AND account_id = ?", (a["id"],)).fetchone():
            raise GoalError(f"there's a goal to pay off {_label(a)} already", field="account_id")
        fields.update(name=_name(data.get("name"), f"Pay off {a['name'] or 'the loan'}"), account_id=a["id"],
                      start_balance=_r(a["balance"]))
    else:
        track = data.get("track")
        if track not in TRACKS:
            raise GoalError("track must be account (a whole account), earmark (part of one) or manual (entries you log)",
                            field="track")
        fields.update(name=_name(data.get("name")), target=_amount(data.get("target"), "target", "The target"),
                      target_month=_month(data.get("target_month"), today), track=track)
        if track in ("account", "earmark"):
            a = by_id.get(data.get("account_id"))
            if a is None or not _asset(a):
                raise GoalError("pick an account (cash or investments)", field="account_id")
            _whole_conflict(conn, a["id"], track)
            fields["account_id"] = a["id"]
            if track == "earmark":
                amount = _amount(data.get("earmark", 0), "earmark", "The amount set aside", allow_zero=True)
                _check_earmark(conn, a, amount)
                fields["earmark"] = amount
                if amount:
                    entry = (amount, "Set aside")
    # Preserve the existing defaults when an API caller omits appearance.
    if kind == "payoff":
        default_icon = "card" if by_id[fields["account_id"]]["group"] == "cards" else "handcoins"
    else:
        default_icon = {"manual": "pencil", "earmark": "split"}.get(fields.get("track"), "piggy-bank")
    fields["icon"] = _appearance(data["icon"], "icon") if "icon" in data else default_icon
    fields["colour"] = _appearance(data["colour"], "colour") if "colour" in data else COLOURS[4 if kind == "payoff" else 0]
    cols = ", ".join(fields)
    with conn:
        cur = conn.execute(f"INSERT INTO goals ({cols}) VALUES ({', '.join('?' * len(fields))})", list(fields.values()))
        gid = cur.lastrowid
        if entry:
            conn.execute("INSERT INTO goal_entries (goal_id, date, amount, note, created_at) VALUES (?,?,?,?,?)",
                         (gid, today.isoformat(), entry[0], entry[1], ts))
    return one(conn, gid, today)


def _get(conn: sqlite3.Connection, goal_id: Any) -> sqlite3.Row:
    if isinstance(goal_id, bool) or not isinstance(goal_id, int):
        raise GoalError("id must be a goal's id", field="id")
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if row is None:
        raise GoalError("no such goal", 404, "id")
    return row


def one(conn: sqlite3.Connection, goal_id: int, today: date) -> dict[str, Any]:
    return next(g for g in payload(conn, today, archived=True)["goals"] + (payload(conn, today, archived=True)["archived"] or [])
                if g["id"] == goal_id)


def edit(conn: sqlite3.Connection, goal_id: Any, data: dict[str, Any], today: date) -> dict[str, Any]:
    """Change a goal's appearance, name, target, month or earmark (what its sheet saves when it closes); archive / unarchive it."""
    g = _get(conn, goal_id)
    allowed = {"save": {"name", "target", "target_month", "earmark", "archived"}, "payoff": {"name", "archived"},
               "monthly": {"name", "target", "archived"}}[g["kind"]] | {"icon", "colour"}
    unknown = sorted(set(data) - allowed - {"id"})
    if unknown:
        raise GoalError(f"{unknown[0]!r} can't be changed on this goal", field=str(unknown[0])[:40])
    sets: dict[str, Any] = {}
    entry = None
    for field in ("icon", "colour"):
        if field in data:
            sets[field] = _appearance(data[field], field)
    if "name" in data:
        sets["name"] = _name(data["name"])
    if "target" in data:
        sets["target"] = _amount(data["target"], "target", "The target")
    if "target_month" in data:
        sets["target_month"] = _month(data["target_month"], today)
    if "earmark" in data:
        if g["track"] != "earmark":
            raise GoalError("only a goal that follows part of an account has an amount set aside", field="earmark")
        a = {x["id"]: x for x in accounts(conn)}.get(g["account_id"])
        if a is None:
            raise GoalError("its account isn't there any more", field="earmark")
        amount = _amount(data["earmark"], "earmark", "The amount set aside", allow_zero=True)
        if round(amount * 100) > round((g["earmark"] or 0) * 100):  # more set aside: it must still fit
            _check_earmark(conn, a, amount, g["id"])
        sets["earmark"] = amount
        delta = _r(amount - (g["earmark"] or 0.0))
        if delta:
            entry = (delta, "Set aside more" if delta > 0 else "Took some back")
    if "archived" in data:
        if not isinstance(data["archived"], bool):
            raise GoalError("archived must be true or false", field="archived")
        if not data["archived"] and g["archived_at"]:
            if g["kind"] == "monthly" and conn.execute("SELECT 1 FROM goals WHERE kind = 'monthly' AND archived_at IS NULL").fetchone():
                raise GoalError("there's a Monthly savings goal already", field="archived")
            if g["track"] in ("account", "earmark"):
                _whole_conflict(conn, g["account_id"], g["track"], g["id"])
                if g["track"] == "earmark":
                    a = {x["id"]: x for x in accounts(conn)}.get(g["account_id"])
                    if a is not None:
                        _check_earmark(conn, a, g["earmark"] or 0.0, g["id"])
        sets["archived_at"] = (g["archived_at"] or now()) if data["archived"] else None
    if sets:
        sets["updated_at"] = now()
        with conn:
            conn.execute(f"UPDATE goals SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", [*sets.values(), g["id"]])
            if entry:
                conn.execute("INSERT INTO goal_entries (goal_id, date, amount, note, created_at) VALUES (?,?,?,?,?)",
                             (g["id"], today.isoformat(), entry[0], entry[1], now()))
    return one(conn, g["id"], today)


def add_entry(conn: sqlite3.Connection, goal_id: Any, data: dict[str, Any], today: date) -> dict[str, Any]:
    """+ add / withdraw on a goal you log yourself: {amount (negative to take out), note?, date?}."""
    g = _get(conn, goal_id)
    if g["kind"] != "save" or g["track"] != "manual":
        raise GoalError("entries are logged on a Save up goal you track yourself", field="amount")
    amount = data.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or round(amount * 100) == 0:
        raise GoalError("the amount must be a number (negative to take money out)", field="amount")
    amount = round(float(amount), 2)
    if abs(amount) > MAX_TARGET:
        raise GoalError(f"the amount is at most {money(MAX_TARGET)}", field="amount")
    note = data.get("note")
    if note is not None:
        if not isinstance(note, str) or len(note.strip()) > MAX_NOTE:
            raise GoalError(f"a note is text, up to {MAX_NOTE} characters", field="note")
        note = note.strip() or None
    day = data.get("date") or today.isoformat()
    try:
        if not isinstance(day, str) or date.fromisoformat(day) > today:
            raise ValueError
    except ValueError:
        raise GoalError("the date must be YYYY-MM-DD, today or earlier", field="date") from None
    held = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM goal_entries WHERE goal_id = ?", (g["id"],)).fetchone()[0]
    if round((held + amount) * 100) < 0:
        raise GoalError(f"you can take out at most {money(max(held, 0))}", field="amount")
    with conn:
        conn.execute("INSERT INTO goal_entries (goal_id, date, amount, note, created_at) VALUES (?,?,?,?,?)",
                     (g["id"], day, amount, note, now()))
    return one(conn, g["id"], today)


def dismiss(conn: sqlite3.Connection, account_id: Any, today: date) -> dict[str, Any]:
    """"Not a goal" on a suggestion: that account isn't suggested again."""
    if not isinstance(account_id, str) or account_id not in {a["id"] for a in accounts(conn)}:
        raise GoalError("not a current account", field="account_id")
    ids = _dismissed(conn)
    if account_id not in ids:
        ids.append(account_id)
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value, updated_at = excluded.updated_at", (DISMISSED, json.dumps(ids), now()))
    return {"dismissed": ids}


# --- text (finance goals) --------------------------------------------------------------------------------------------

def text(p: dict[str, Any]) -> str:
    def m(x: float | None) -> str:
        return money(x)
    lines = []
    for g in p["goals"]:
        head = {"save": "Save up", "payoff": "Pay off", "monthly": "Monthly savings"}[g["kind"]]
        src = (f" · {g['account']['label']}" if g["account"] else "") + (
            {"account": " (whole account)", "earmark": " (part of it)", "manual": " (logged by hand)"}.get(g["track"] or "", ""))
        pct = "-" if g["pct"] is None else f"{g['pct']:.0f}%"
        line = f"#{g['id']} {g['name']} [{head}{src}]: {m(g['progress'])} of {m(g['target'])} ({pct})"
        if g["kind"] == "monthly":
            line += f" saved in {g['month']} (income {m(g['income'])} - spending {m(g['spending'])})"
        elif g["done"]:
            line += " · reached"
        else:
            line += f" · {m(g['left'])} left"
            pace = g.get("pace") or {}
            if pace.get("past_due"):
                line += " · Past due"
            elif pace.get("need_per_month") is not None:
                line += f" · save {m(pace['need_per_month'])}/mo to hit {g['target_month']}"
            elif pace.get("on_pace_month"):
                line += f" · on pace for {pace['on_pace_month']}"
            if g.get("payoff") and g["payoff"].get("payoff_month"):
                line += f" · paid off {g['payoff']['payoff_month']}"
        if g.get("short"):
            line += f" · SHORT by {m(g['short']['amount'])} (the account holds less than its earmarks)"
        if g.get("missing_account"):
            line += " · its account isn't there any more"
        lines.append(line)
    if not p["goals"]:
        lines.append("No goals yet.")
    if p["suggestions"]:
        lines.append("Suggested (savings accounts without a goal): " + ", ".join(
            f"{s['label']} {m(s['balance'])}" for s in p["suggestions"]))
    t = p["totals"]
    if t["save_count"]:
        lines.append(f"Saving toward {m(t['saving_target'])}: {m(t['saved'])} saved ({t['pct']:.0f}%)")
    lines.append("(goals never change net worth, budgets or any other total)")
    return "\n".join(lines)
