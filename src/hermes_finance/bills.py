"""Bills tracked by hand (TASK-21): "Track as a bill" on a transaction (and "Edit schedule" / "Stop tracking" on a Recurring
row or bill sheet), `finance bills track / untrack`, and the finance_track_bill agent tool. They all go through plan()
(check the request and say in plain words what it would change, changing nothing: the Hermes approval card and the MCP
pending action show that sentence) and apply() (save it), so each way to track a bill leaves the same tracked_bills row.

A bill is the merchant (insights.merchant_key), not the account: every charge from it, on any account, confirms it. One row
per merchant (db.py tracked_bills). insights.recurring() reads the rows: an active one makes its merchant a bill on its
own schedule; a stopped one hides the merchant from bills (tracked by hand: it stops; detected: it's hidden). Tracking
it again clears the stop.

  track   make the merchant a bill with these settings (a new row, or the old one's settings replaced). Not given: the
          schedule it was detected on (else every month), and "changes each time" for a utility; the charge it starts
          from is the merchant's latest (the transaction sheet: that transaction).
  update  change what's given on a bill that is tracked or detected (a detected one becomes tracked); the rest stays.
  stop    a bill that is tracked or detected only.

Usually (a bill that changes): one amount or a range, low <= high, > 0. While there are fewer than 3 real charges its
middle is the estimate; after that the median of the last 3, the range still shown. Giving it means the amount changes.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import date
from typing import Any

from . import insights
from .money import money
from .db import now

EVERY = {"1w": "weekly", "2w": "biweekly", "1m": "monthly", "2m": "bimonthly", "3m": "quarterly", "6m": "semiannual",
         "1y": "yearly"}
EVERY_OF = {v: k for k, v in EVERY.items()}
SCHEDULE_WORDS = {"weekly": "every week", "biweekly": "every 2 weeks", "monthly": "every month",
                  "bimonthly": "every 2 months", "quarterly": "every 3 months", "semiannual": "every 6 months",
                  "yearly": "every year"}
ACTIONS = ("track", "update", "stop")
MAX_USUAL = 100_000
MAX_TEXT = 200
MAX_CANDIDATES = 8
UNSET = object()   # "usually" not given (None clears it)


class BillError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.field = field


# --- arguments ------------------------------------------------------------------------------------------------------

def parse_every(value: Any) -> str:
    """'2m' (or 'every 2 months', or a cadence id such as 'bimonthly') -> the cadence id."""
    if isinstance(value, str):
        v = " ".join(value.strip().lower().split())
        if v in EVERY:
            return EVERY[v]
        if v in insights.CADENCE_LABELS:
            return v
        for cadence, words in SCHEDULE_WORDS.items():
            if v in (words, insights.CADENCE_LABELS[cadence]):
                return cadence
    raise BillError(f"every must be one of {', '.join(EVERY)} (every week, 2 weeks, month, 2 months, 3 months, "
                    "6 months, year)", field="every")


def _amount(x: Any) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise BillError("usually must be an amount or a range of two amounts, e.g. 120 or [100, 150]", field="usually")
    if isinstance(x, int) and abs(x) > MAX_USUAL:
        raise BillError(f"usually must be between 0 and {MAX_USUAL:,}", field="usually")
    if not math.isfinite(x) or not 0 < x <= MAX_USUAL:
        raise BillError(f"usually must be more than 0 and at most {MAX_USUAL:,}", field="usually")
    return round(float(x), 2)


def parse_usually(value: Any) -> tuple[float, float] | None:
    """What the bill usually is: an amount (120), a range ([100, 150], "100-150", "$100–150"), or None / "" for none."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        text = value.replace("$", "").replace(",", "").strip()
        if text[:1] in ("-", "–", "—", "−"):
            raise BillError("usually must be more than 0", field="usually")
        parts = [p for p in re.split(r"\s*(?:-|–|—|to)\s*", text) if p]
        try:
            value = [float(p) for p in parts]
        except ValueError:
            raise BillError(f"usually must be an amount or a range such as 100-150, not {value[:40]!r}",
                            field="usually") from None
    if isinstance(value, (list, tuple)):
        if not 1 <= len(value) <= 2:
            raise BillError("usually takes one amount or two (low and high)", field="usually")
        low, high = _amount(value[0]), _amount(value[-1])
    else:
        low = high = _amount(value)
    if low > high:
        raise BillError(f"usually: the low end ({money(low)}) is more than the high end ({money(high)})", field="usually")
    return low, high


# --- merchants ------------------------------------------------------------------------------------------------------

def merchants(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every merchant money went out to (and every tracked one): key -> {name (as shown, the latest), names (the
    statement names too, casefolded)}."""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT merchant_name, name FROM v_transactions WHERE spend > 0 ORDER BY date, transaction_id"):
        key = insights.merchant_key(r["merchant_name"], r["name"])
        if not key:
            continue
        m = out.setdefault(key, {"name": key, "names": set()})
        m["name"] = (r["merchant_name"] or r["name"]).strip()
        m["names"] |= {key, (r["name"] or "").strip().casefold()} - {""}
    for t in insights.tracked_rows(conn):
        out.setdefault(t["merchant_key"], {"name": t["merchant"], "names": {t["merchant_key"]}})
    return out


def find_merchant(conn: sqlite3.Connection, text: Any) -> tuple[str, str]:
    """(key, name) of the merchant `text` names: its exact name, else the one merchant whose name (or statement name)
    matches it case-insensitively, else the one that contains it. Unknown or ambiguous: BillError listing candidates."""
    if not isinstance(text, str) or not text.strip():
        raise BillError("merchant is required: the merchant's name as the transactions show it", field="merchant")
    text = " ".join(text.split())
    if len(text) > MAX_TEXT:
        raise BillError(f"merchant is too long (at most {MAX_TEXT} characters)", field="merchant")
    ms = merchants(conn)
    exact = [k for k, m in ms.items() if m["name"] == text]
    if len(exact) == 1:
        return exact[0], ms[exact[0]]["name"]
    q = text.casefold()
    for found in ([k for k, m in ms.items() if q in m["names"]], [k for k, m in ms.items() if any(q in n for n in m["names"])]):
        if len(found) == 1:
            return found[0], ms[found[0]]["name"]
        if found:
            names = sorted(ms[k]["name"] for k in found)
            more = f" and {len(names) - MAX_CANDIDATES} more" if len(names) > MAX_CANDIDATES else ""
            raise BillError(f"{text!r} matches {len(names)} merchants: {', '.join(names[:MAX_CANDIDATES])}{more}. "
                            "Use one of these names exactly.", field="merchant")
    words = insights.name_tokens(text)
    near = sorted(m["name"] for m in ms.values() if words & insights.name_tokens(*m["names"]))
    hint = f" Close ones: {', '.join(near[:MAX_CANDIDATES])}." if near else ""
    raise BillError(f"no merchant matches {text!r} in your transactions.{hint}", status=404, field="merchant")


def latest_charge(conn: sqlite3.Connection, keys: list[str] | set[str]) -> sqlite3.Row | None:
    """The newest charge (money out) from any of the merchant's keys, on any account."""
    for r in conn.execute("SELECT transaction_id, date, spend, merchant_name, name, category, pfc_detailed FROM v_transactions "
                          "WHERE spend > 0 ORDER BY date DESC, transaction_id DESC"):
        if insights.merchant_key(r["merchant_name"], r["name"]) in keys:
            return r
    return None


def _row(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    return next((t for t in insights.tracked_rows(conn) if t["merchant_key"] == key), None)


def bill_of(rec: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The Recurring item (tracked or detected) this merchant key is, or None."""
    return next((r for r in rec["items"] if key in r["keys"]), None)


# --- words ------------------------------------------------------------------------------------------------------------

def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


def amount_words(bill: dict[str, Any]) -> str:
    """'~$125 est. (usually $100–150)', '~$296 est.' or '$465.10'."""
    out = bill["amount_label"]
    u = bill.get("usual")
    if bill["variable"] and u and u["source"] == "you":
        out += f" (usually {insights.usual_text(u['low'], u['high'])})"
    return out


def _settings_words(cadence: str, variable: bool, usual: tuple[float, float] | None) -> str:
    what = SCHEDULE_WORDS[cadence]
    if not variable:
        return f"{what}, the same amount each time"
    return f"{what}, usually {insights.usual_text(*usual)}" if usual else f"{what}, the amount changing each time"


def _next_words(bill: dict[str, Any]) -> str:
    return f"next ~{_day(bill['next_expected'])}, {bill['amount_label']}"


# --- plan / apply -----------------------------------------------------------------------------------------------------

def _anchor(conn: sqlite3.Connection, keys: list[str], transaction_id: str | None) -> dict[str, Any]:
    if transaction_id is not None:
        t = conn.execute("SELECT transaction_id, date, spend FROM v_transactions WHERE transaction_id = ?",
                         (transaction_id,)).fetchone()
    else:
        t = latest_charge(conn, keys)
    if t is None or t["spend"] <= 0:
        raise BillError("a bill starts from a charge (money out) from the merchant", field="transaction_id")
    return {"anchor_date": t["date"], "anchor_amount": insights._r(t["spend"]), "anchor_transaction_id": t["transaction_id"]}


def suggest(conn: sqlite3.Connection, key: str, rec: dict[str, Any]) -> dict[str, Any]:
    """What the Track as a bill sheet starts with: the bill's own schedule and kind of amount when it's one already
    (detected or tracked) or was (stopped), else every month, and "changes each time" for a utility."""
    bill = bill_of(rec, key)
    row = _row(conn, key)
    if bill:
        usual = (row["usual_low"], row["usual_high"]) if row and not row["stopped_at"] and row["usual_low"] else None
        return {"every": EVERY_OF[bill["cadence"]], "varies": bill["variable"], "usually": usual}
    if row:  # stopped: tracking it again starts from how it was
        return {"every": EVERY_OF[row["cadence"]], "varies": bool(row["variable"]),
                "usually": (row["usual_low"], row["usual_high"]) if row["usual_low"] else None}
    last = latest_charge(conn, {key})
    utility = bool(last) and insights.is_utility(last["pfc_detailed"], (last["merchant_name"] or last["name"] or ""))
    return {"every": "1m", "varies": utility, "usually": None}


def plan(conn: sqlite3.Connection, today: date, action: Any, *, merchant: Any = None, key: str | None = None,
         transaction_id: str | None = None, every: Any = None, varies: Any = None, usually: Any = UNSET,
         rec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check a request and work out what it would change, without changing anything. The merchant is named by `key`
    (a merchant key, from a page), `transaction_id` (its charge: the transaction sheet) or `merchant` (text: the CLI and
    the tool). {action, key, merchant, row (what apply saves), before (the bill now, or None), bill (the bill after, or
    None when it stops), summary (the change in plain words)}. BillError when it can't be done."""
    if action not in ACTIONS:
        raise BillError(f"action must be one of {', '.join(ACTIONS)}", field="action")
    if varies is not None and not isinstance(varies, bool):
        raise BillError("varies must be true (the amount changes each time) or false", field="varies")
    rec = rec if rec is not None else insights.recurring(conn, today)
    if transaction_id is not None:
        t = conn.execute("SELECT merchant_name, name, spend FROM v_transactions WHERE transaction_id = ?",
                         (str(transaction_id),)).fetchone()
        if t is None:
            raise BillError("unknown transaction", status=404, field="transaction_id")
        key, name = insights.merchant_key(t["merchant_name"], t["name"]), (t["merchant_name"] or t["name"] or "").strip()
        if not key:
            raise BillError("this transaction has no merchant name to track", field="transaction_id")
    elif key is not None:
        ms = merchants(conn)
        if not isinstance(key, str) or key not in ms:
            raise BillError("unknown merchant", status=404, field="merchant")
        name = ms[key]["name"]
    else:
        key, name = find_merchant(conn, merchant)
    before = bill_of(rec, key)
    row = _row(conn, key)
    if before:
        name = before["merchant"]
    keys = before["keys"] if before else [key]
    tracked = bool(row and not row["stopped_at"])

    if action == "stop":
        if not before:
            raise BillError(f"{name} isn't a bill right now (not tracked or detected), so there's nothing to stop",
                            field="merchant")
        if tracked:
            summary = (f"Stop tracking {name} as a bill (tracked {SCHEDULE_WORDS[row['cadence']]} since "
                       f"{_day(insights.local_day(row['created_at']))}). Its charges stay as they are.")
            new = {**row, "stopped_at": "(now)"}
        else:
            summary = (f"Stop tracking {name} as a bill: it's no longer listed with your bills (found "
                       f"{SCHEDULE_WORDS[before['cadence']]}, {before['amount_label']}). Its charges stay as they are.")
            new = {"merchant_key": key, "merchant": name, "cadence": before["cadence"], "variable": int(before["variable"]),
                   "usual_low": None, "usual_high": None, **_anchor(conn, keys, None), "stopped_at": "(now)"}
        return {"action": action, "key": key, "merchant": name, "row": new, "before": before, "bill": None,
                "summary": summary}

    if action == "update" and not before:
        raise BillError(f"{name} isn't tracked or detected as a bill yet: track it first (action track)", field="merchant")
    base = suggest(conn, key, rec) if action == "track" or not tracked else {
        "every": EVERY_OF[row["cadence"]], "varies": bool(row["variable"]),
        "usually": (row["usual_low"], row["usual_high"]) if row["usual_low"] else None}
    cadence = parse_every(every) if every is not None else EVERY[base["every"]]
    usual = base["usually"] if action == "update" else None
    if usually is not UNSET:
        usual = parse_usually(usually)
    if varies is None:
        varies = True if (usually is not UNSET and usual) else base["varies"]
    if not varies and usually is not UNSET and usual:
        raise BillError("usually is for a bill whose amount changes: leave it out, or say the amount changes (varies)",
                        field="usually")
    if not varies:
        usual = None
    anchor = ({k: row[k] for k in ("anchor_date", "anchor_amount", "anchor_transaction_id")}
              if tracked and action == "update" else _anchor(conn, keys, transaction_id))
    new = {"merchant_key": key, "merchant": name, "cadence": cadence, "variable": int(varies),
           "usual_low": usual[0] if usual else None, "usual_high": usual[1] if usual else None, **anchor,
           "created_at": row["created_at"] if row else now(), "updated_at": now(), "stopped_at": None}
    others = [t for t in insights.tracked_rows(conn) if t["merchant_key"] != key]
    after = bill_of(insights.recurring(conn, today, tracked=[*others, new]), key)
    words = _settings_words(cadence, bool(varies), usual)
    if action == "track" and not tracked:
        summary = f"Track {name} as a bill {words} ({_next_words(after)})."
    else:
        was = _settings_words(before["cadence"], before["variable"],
                              base["usually"] if tracked else None)
        summary = (f"Change {name}'s bill to {words} (now {was}; {_next_words(after)})." if words != was else
                   f"Keep {name}'s bill {words} ({_next_words(after)}); nothing changes.")
    return {"action": action, "key": key, "merchant": name, "row": new, "before": before, "bill": after,
            "summary": summary}


COLUMNS = ("merchant_key", "merchant", "cadence", "variable", "usual_low", "usual_high", "anchor_date", "anchor_amount",
           "anchor_transaction_id")


def apply(conn: sqlite3.Connection, p: dict[str, Any], today: date) -> dict[str, Any]:
    """Save a plan(): {action, key, merchant, summary, bill (as Recurring shows it now, None once stopped), row}."""
    r, ts = p["row"], now()
    with conn:
        if p["action"] == "stop" and "id" in r:
            conn.execute("UPDATE tracked_bills SET stopped_at = ?, updated_at = ? WHERE id = ?", (ts, ts, r["id"]))
        else:
            values = [r[c] for c in COLUMNS]
            stopped = ts if p["action"] == "stop" else None
            conn.execute(f"INSERT INTO tracked_bills ({', '.join(COLUMNS)}, created_at, updated_at, stopped_at) "
                         f"VALUES ({', '.join('?' * len(COLUMNS))}, ?, ?, ?) ON CONFLICT(merchant_key) DO UPDATE SET "
                         + ", ".join(f"{c} = excluded.{c}" for c in COLUMNS[1:])
                         + ", updated_at = excluded.updated_at, stopped_at = excluded.stopped_at",
                         (*values, ts, ts, stopped))
    saved = _row(conn, p["key"])
    bill = bill_of(insights.recurring(conn, today), p["key"]) if p["action"] != "stop" else None
    return {"action": p["action"], "key": p["key"], "merchant": p["merchant"], "summary": p["summary"], "bill": bill,
            "row": saved}


# --- the pages --------------------------------------------------------------------------------------------------------

def charges(conn: sqlite3.Connection, keys: list[str], usual: dict[str, Any] | None, n: int = 6) -> list[dict[str, Any]]:
    """The merchant's latest charges (any account): the bill sheet's list, each with higher / lower than usual."""
    out = []
    for r in conn.execute("SELECT transaction_id, date, spend, merchant_name, name, account_name, account_mask, "
                          "institution_name FROM v_transactions WHERE spend > 0 ORDER BY date DESC, transaction_id DESC"):
        if insights.merchant_key(r["merchant_name"], r["name"]) in keys:
            out.append({"transaction_id": r["transaction_id"], "date": r["date"], "amount": insights._r(r["spend"]),
                        "account": {"name": r["account_name"], "mask": r["account_mask"], "institution": r["institution_name"]},
                        "outlier": insights.outlier(r["spend"], usual)})
            if len(out) >= n:
                break
    return out


SCHEDULES = [{"every": e, "cadence": c, "label": SCHEDULE_WORDS[c].capitalize()} for e, c in EVERY.items()]


def lookup(conn: sqlite3.Connection, today: date, transaction_id: str | None = None,
           key: str | None = None) -> dict[str, Any]:
    """The transaction sheet's and the bill sheet's data: the merchant, its bill (or None: hidden says whether it was
    stopped), what Track as a bill starts with, the charge it would start from, the latest charges and the schedules."""
    rec = insights.recurring(conn, today)
    tx = None
    if transaction_id is not None:
        tx = conn.execute("SELECT transaction_id, date, spend, merchant_name, name FROM v_transactions "
                          "WHERE transaction_id = ?", (str(transaction_id),)).fetchone()
        if tx is None:
            raise BillError("unknown transaction", status=404, field="transaction_id")
        key = insights.merchant_key(tx["merchant_name"], tx["name"])
    elif not isinstance(key, str) or key not in merchants(conn):
        raise BillError("unknown merchant", status=404, field="merchant")
    bill = bill_of(rec, key) if key else None
    row = _row(conn, key) if key else None
    usual = bill["usual"] if bill else None
    last = latest_charge(conn, bill["keys"] if bill else {key}) if key else None
    start = tx if tx is not None and tx["spend"] > 0 else last
    return {"key": key or None, "merchant": bill["merchant"] if bill else merchants(conn).get(key, {}).get("name") if key else None,
            "trackable": bool(key) and start is not None,
            "bill": bill, "hidden": bool(row and row["stopped_at"]), "tracked": bool(row and not row["stopped_at"]),
            "suggest": suggest(conn, key, rec) if key else None,
            "anchor": {"transaction_id": start["transaction_id"], "date": start["date"],
                       "amount": insights._r(start["spend"])} if start is not None else None,
            "outlier": insights.outlier(tx["spend"], usual) if tx is not None and tx["spend"] > 0 else None,
            "charges": charges(conn, bill["keys"] if bill else [key], usual) if key else [],
            "schedules": SCHEDULES}


def decorate(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Transactions page's rows: `bill_outlier` (higher / lower, or None) on a charge from a tracked bill whose
    usual amount the owner typed, when it's far outside it. Informational: nothing else reads it."""
    usual = {t["merchant_key"]: insights.usual_of(t) for t in insights.tracked_rows(conn)
             if not t["stopped_at"] and t["variable"] and t["usual_low"]}
    for r in rows:
        u = usual.get(insights.merchant_key(r.get("merchant_name"), r.get("name"))) if usual else None
        r["bill_outlier"] = insights.outlier(r["amount"], u) if u and r["amount"] > 0 else None
    return rows
