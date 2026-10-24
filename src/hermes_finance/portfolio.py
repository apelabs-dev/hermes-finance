"""Investments and Loans & debt pages: read-only summaries over Plaid accounts + manual accounts.

Investments: account totals are Plaid's current balance (the same number net worth uses; the holdings
detail comes from the same Plaid response), plus manual investment accounts. The value-over-time line and
the change chips come only from real daily holding_snapshots (and manual balance history from the first
snapshot on); a change is null until there's a snapshot old enough, and it compares the same accounts at
both ends, so adding an account never shows up as a gain.

Debt: Plaid credit cards and loans plus manual credit cards, loans and other liabilities.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date
from typing import Any

from . import alpaca, insights, manual

_r = insights._r

RETIREMENT_RE = re.compile(r"401|403|457|\bira\b|roth|pension|retirement|keogh|thrift|profit sharing|rrsp|rrif|"
                           r"lira|\blif\b|sarsep|tsp", re.IGNORECASE)
CRYPTO_SUBTYPES = {"crypto exchange", "non-custodial wallet"}
ROBO_RE = re.compile(r"robo|automated invest", re.IGNORECASE)
TYPE_GROUP = {"Brokerage": "General", "Robo": "General", "Other": "General", "Retirement": "Retirement",
              "HSA": "Retirement", "Crypto": "Crypto"}
MANUAL_TYPE = {"brokerage": "Brokerage", "401k": "Retirement", "roth_ira": "Retirement",
               "traditional_ira": "Retirement", "hsa": "HSA", "crypto": "Crypto", "other": "Other"}
GROUP_ORDER = ("General", "Retirement", "Crypto")
TYPE_ORDER = ("Brokerage", "Robo", "Retirement", "HSA", "Crypto", "Other")


def plaid_type(subtype: str | None, name: str | None) -> str:
    sub = (subtype or "").lower()
    if sub in CRYPTO_SUBTYPES or "crypto" in sub:
        return "Crypto"
    if sub == "hsa" or sub == "health savings account":
        return "HSA"
    if RETIREMENT_RE.search(sub):
        return "Retirement"
    if ROBO_RE.search(name or ""):
        return "Robo"
    return "Brokerage"


def _is_cash(h: sqlite3.Row) -> bool:
    return (h["security_type"] or "").lower() == "cash" or (h["ticker_symbol"] or "").upper().startswith("CUR:")


def _latest_on_or_before(series: dict[str, float], day: str) -> float | None:
    best = None
    for d in sorted(series):
        if d > day:
            break
        best = series[d]
    return best


def _investment_series(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """name -> {"values": {date: value}, "until": archived date or None}: one per Plaid account (sum of its
    holding snapshots per day) and one per manual investment account (its balance history)."""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
            """SELECT s.account_id, s.date, SUM(COALESCE(s.value, 0)) FROM holding_snapshots s
               JOIN accounts a ON a.account_id = s.account_id JOIN items i ON i.item_id = a.item_id
               WHERE i.removed_at IS NULL GROUP BY s.account_id, s.date"""):
        out.setdefault(f"plaid:{r[0]}", {"values": {}, "until": None})["values"][r[1]] = r[2]
    for r in conn.execute(
            """SELECT h.manual_account_id, h.date, h.balance, m.archived_at FROM manual_balance_history h
               JOIN manual_accounts m ON m.id = h.manual_account_id WHERE m.kind = 'investment'"""):
        out.setdefault(f"manual:{r[0]}", {"values": {}, "until": insights.local_day(r[3])})["values"][r[1]] = r[2]
    values = alpaca.value_series(conn)  # TASK-19: the Alpaca live account's daily equity (None: not ACTIVE that day)
    if values:
        out[f"plaid:{alpaca.ACCOUNT_ID}"] = {"values": values, "until": None}
    return out


def _value_at(s: dict[str, Any], day: str) -> float | None:
    if s["until"] and day >= s["until"]:
        return None
    return _latest_on_or_before(s["values"], day)


def investment_history(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    series = _investment_series(conn)
    plaid_days = {d for k, s in series.items() if k.startswith("plaid:") for d in s["values"]}
    first = min(plaid_days) if plaid_days else None
    days = set(plaid_days) | {d for k, s in series.items() if k.startswith("manual:") for d in s["values"]
                              if first is None or d >= first}
    days = sorted(d for d in days if d <= today.isoformat())
    points = [{"date": d, "value": _r(sum(v for s in series.values() if (v := _value_at(s, d)) is not None))}
              for d in days]

    def change(target: str | None) -> dict[str, Any] | None:
        if len(points) < 2 or target is None:
            return None
        start = next((p["date"] for p in reversed(points) if p["date"] <= target), None)
        end = points[-1]["date"]
        if start is None or start == end:
            return None
        # the same accounts at both ends: one added or archived in between is neither a gain nor a loss
        same = [s for s in series.values() if _value_at(s, start) is not None and _value_at(s, end) is not None]
        a = sum(_value_at(s, start) for s in same)
        b = sum(_value_at(s, end) for s in same)
        return {"from": start, "to": end, "amount": _r(b - a), "pct": _r((b - a) / a * 100) if a else None}

    first_day = points[0]["date"] if points else None
    changes = {
        "7d": change(date.fromordinal(today.toordinal() - 7).isoformat()),
        "1m": change(insights._shift_months(today, -1).isoformat()),
        "ytd": change(date(today.year, 1, 1).isoformat()),
        "all": change(first_day),
    }
    seven = date.fromordinal(date.fromisoformat(first_day).toordinal() + 7).isoformat() if first_day else None
    return {"history": points, "tracking_since": first_day, "days": len(points), "changes": changes,
            "change_7d_from": seven}


RESIDUAL_MIN = 0.01 - 1e-9  # a balance/holdings gap under a cent isn't shown


def investments(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    accts = conn.execute(
        """SELECT a.account_id, a.name, a.official_name, a.mask, a.subtype, a.current_balance, a.updated_at,
                  i.institution_name, i.institution_id, i.item_id
           FROM accounts a JOIN items i ON i.item_id = a.item_id
           WHERE i.removed_at IS NULL AND a.type = 'investment' ORDER BY i.created_at, a.name""").fetchall()
    accts += alpaca.networth_rows(conn)  # TASK-19: Alpaca (read-only API) at its equity, while ACTIVE
    holdings = conn.execute(
        """SELECT h.*, a.name AS account_name, a.mask, sl.ticker IS NOT NULL AS has_logo FROM holdings h
           JOIN accounts a ON a.account_id = h.account_id JOIN items i ON i.item_id = a.item_id
           LEFT JOIN security_logos sl ON sl.ticker = UPPER(h.ticker_symbol) AND sl.png IS NOT NULL
           WHERE i.removed_at IS NULL AND a.type = 'investment'
           ORDER BY COALESCE(h.institution_value, 0) DESC, h.security_id""").fetchall()
    extra = alpaca.holding_rows(conn)  # its positions and cash, so its value = its holdings (no residual)
    if extra:
        holdings = sorted([*holdings, *extra], key=lambda h: (-(h["institution_value"] or 0.0), h["security_id"]))
    by_acct: dict[str, float] = defaultdict(float)
    for h in holdings:
        by_acct[h["account_id"]] += h["institution_value"] or 0.0
    accounts = []
    for a in accts:
        value = a["current_balance"] if a["current_balance"] is not None else by_acct.get(a["account_id"], 0.0)
        kind = plaid_type(a["subtype"], a["name"])
        count = sum(1 for h in holdings if h["account_id"] == a["account_id"])
        accounts.append({"id": a["account_id"], "name": a["name"], "official_name": a["official_name"],
                         "mask": a["mask"], "institution": a["institution_name"],
                         "institution_id": a["institution_id"], "subtype": a["subtype"],
                         "type": kind, "group": TYPE_GROUP[kind], "value": _r(value), "updated_at": a["updated_at"],
                         "manual": False, "stale": False, "holdings": count,
                         # Plaid snapshots the balance and the prices at slightly different times: what the balance
                         # has that its holdings + cash don't (negative when the holdings are worth more)
                         "residual": _r(value - by_acct[a["account_id"]]) if count else 0.0,
                         **(alpaca.account_extra(conn) if a["account_id"] == alpaca.ACCOUNT_ID else {})})
    manual_rows = [manual.describe(conn, r, today) for r in manual.rows(conn) if r["kind"] == "investment"]
    for m in manual_rows:
        kind = MANUAL_TYPE.get(m["subtype"] or "other", "Other")
        accounts.append({"id": f"manual:{m['id']}", "manual_id": m["id"], "name": m["name"], "official_name": None,
                         "mask": None, "institution": m["institution"], "subtype": m["subtype"], "type": kind,
                         "subtype_label": m["subtype_label"], "group": TYPE_GROUP[kind], "value": m["balance"],
                         "updated_at": m["updated_at"], "as_of": m["as_of"], "manual": True, "stale": m["stale"],
                         "holdings": 0, "residual": 0.0})
    total = _r(sum(a["value"] for a in accounts))
    hold_total = sum(h["institution_value"] or 0.0 for h in holdings)
    # hotfix 2026-10-05: what a holding's "% of portfolio" is of. Plaid can price the holdings after the balance (NVDA
    # $523.16 against a $511.55 balance), so a share of the balance total went past 100%. Each account counts at the
    # larger of its balance and its holdings (= the sum of the overview tile's positive slices), so no share passes
    # 100%. Equal to `total` whenever no account's holdings are priced above its balance.
    priced_total = _r(sum(max(a["value"], by_acct[a["id"]]) if not a["manual"] and a["holdings"] else a["value"]
                          for a in accounts))
    rows = [{"account_id": h["account_id"], "account_name": h["account_name"], "mask": h["mask"],
             "ticker": h["ticker_symbol"], "name": h["security_name"], "type": h["security_type"],
             "quantity": h["quantity"], "price": h["institution_price"], "value": _r(h["institution_value"] or 0.0),
             "cost_basis": h["cost_basis"], "is_cash": _is_cash(h), "logo": bool(h["has_logo"]) and not _is_cash(h),
             "pct": _r((h["institution_value"] or 0.0) / priced_total * 100) if priced_total else None}
            for h in holdings]
    # allocation by holding: securities across accounts (cash together), manual accounts as their own slice
    by_holding: dict[str, dict[str, Any]] = {}
    for h in rows:
        key = "Cash" if h["is_cash"] else (h["ticker"] or h["name"] or "Unknown")
        e = by_holding.setdefault(key, {"label": key, "name": "Uninvested cash" if h["is_cash"] else h["name"],
                                        "value": 0.0, "cash": h["is_cash"]})
        e["value"] += h["value"]
    plaid_without_holdings = [a for a in accounts if not a["manual"] and not a["holdings"] and a["value"]]
    for a in plaid_without_holdings + [a for a in accounts if a["manual"]]:
        by_holding[a["id"]] = {"label": a["name"], "name": f"{a['type']}{' · added by you' if a['manual'] else ''}",
                               "value": a["value"], "cash": False, "manual": a["manual"]}
    alloc_holding = sorted(({**e, "value": _r(e["value"])} for e in by_holding.values()),
                           key=lambda e: (e.get("cash", False), -e["value"]))
    # the residual, below Cash, so the allocation adds up to the total (hidden under a cent)
    residual = sum(a["value"] - by_acct[a["id"]] for a in accounts if not a["manual"] and a["holdings"])
    if abs(residual) >= RESIDUAL_MIN:
        alloc_holding.append({"label": "Unpriced / pending", "name": "In the balance, not priced in holdings yet",
                              "value": _r(residual), "cash": False, "residual": True})
    by_type = {t: {"label": t, "value": 0.0, "count": 0} for t in TYPE_ORDER}
    for a in accounts:
        by_type[a["type"]]["value"] += a["value"]
        by_type[a["type"]]["count"] += 1
    alloc_type = [{**e, "value": _r(e["value"])} for t, e in by_type.items()
                  if e["count"] or t in ("Retirement", "Crypto")]
    groups = {g: {"label": g, "value": 0.0, "count": 0} for g in GROUP_ORDER}
    for a in accounts:
        groups[a["group"]]["value"] += a["value"]
        groups[a["group"]]["count"] += 1
    prices_as_of = max((h["updated_at"] for h in holdings), default=None)
    return {
        "total": total,
        "holdings_total": _r(hold_total),
        "priced_total": priced_total,
        "accounts": accounts,
        "holdings": rows,
        "allocation": {"by_holding": alloc_holding, "by_type": alloc_type},
        "breakdown": [{**g, "value": _r(g["value"])} for g in groups.values()],
        "prices_as_of": prices_as_of,
        **investment_history(conn, today),
    }


# --- debt ---------------------------------------------------------------------------------------------

LOAN_LABELS = {"auto": "Auto loan", "student": "Student loan", "personal": "Personal loan", "mortgage": "Mortgage",
               "bnpl": "Buy now, pay later", "other": "Other"}


def _loan_key(subtype: str | None) -> str:
    sub = (subtype or "").lower()
    if sub in LOAN_LABELS:
        return sub
    return {"home equity": "mortgage", "line of credit": "personal", "consumer": "personal",
            "construction": "mortgage", "commercial": "personal"}.get(sub, "other")


def _matched(conn: sqlite3.Connection, row: sqlite3.Row, limit: int = 24) -> list[dict[str, Any]]:
    """Applied payments plus the pattern's other posted matches (the ones the entered balance already
    includes, or not applied yet), newest first."""
    applied = {p["transaction_id"]: p for p in manual.payments(conn, row["id"])}
    out = []
    for p in applied.values():
        live = p["date"] > row["balance_as_of"]  # on or before it: already in the balance entered since
        out.append({"transaction_id": p["transaction_id"], "date": p["date"], "name": p["name"],
                    "merchant_name": p["merchant_name"], "amount": p["amount"], "account_name": p["account_name"],
                    "account_mask": p["account_mask"], "applied": live, "interest": p["interest"] if live else None,
                    "principal": p["principal"] if live else None, "source": p["source"], "included": not live})
    if row["match_pattern"]:
        for t in manual.matching_transactions(conn, row["match_pattern"], limit=limit):
            if t["transaction_id"] in applied or t["applied_to"] is not None:
                continue
            out.append({**{k: t[k] for k in ("transaction_id", "date", "name", "merchant_name", "amount",
                                             "account_name", "account_mask")},
                        "applied": False, "interest": None, "principal": None, "source": None,
                        "included": t["date"] <= row["balance_as_of"]})
    out.sort(key=lambda x: (x["date"], x["transaction_id"]), reverse=True)
    return out[:limit]


def debt(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    liab = {(r["account_id"]): r for r in conn.execute(
        "SELECT * FROM liabilities ORDER BY CASE kind WHEN 'credit' THEN 0 ELSE 1 END")}
    plaid = conn.execute(
        """SELECT a.account_id, a.name, a.official_name, a.mask, a.type, a.subtype, a.current_balance, a.credit_limit,
                  a.updated_at, i.institution_name, i.institution_id FROM accounts a JOIN items i ON i.item_id = a.item_id
           WHERE i.removed_at IS NULL AND a.type IN ('credit', 'loan') ORDER BY i.created_at, a.name""").fetchall()
    cards, loans, others = [], [], []
    for a in plaid:
        l = liab.get(a["account_id"])
        bal = _r(a["current_balance"] or 0.0)
        base = {"id": a["account_id"], "manual": False, "name": a["name"], "official_name": a["official_name"],
                "institution": a["institution_name"], "institution_id": a["institution_id"], "mask": a["mask"],
                "balance": bal,
                "apr": l["interest_rate"] if l else None,
                "due_date": l["next_payment_due_date"] if l else None, "updated_at": a["updated_at"]}
        if a["type"] == "credit":
            lim = a["credit_limit"]
            cards.append({**base, "limit": lim, "util": _r(bal / lim * 100) if lim else None,
                          "min_payment": l["minimum_payment_amount"] if l else None})
        else:
            monthly = l["minimum_payment_amount"] if l else None
            key = _loan_key(a["subtype"])
            loans.append({**base, "subtype": key, "type_label": LOAN_LABELS[key], "monthly_payment": monthly,
                          "original_amount": None, "paid_pct": None, "paid_off": round(bal * 100) <= 0,
                          "payoff": manual.payoff(bal, base["apr"], monthly,
                                                  date.fromisoformat(base["due_date"]) if base["due_date"]
                                                  else manual.add_one_month(today)),
                          "next_payment": base["due_date"], "last_payment": None, "payments": [], "history": [],
                          "match_pattern": None, "balance_as_of": None, "stale": False})
    for r in manual.rows(conn):
        if r["kind"] not in manual.LIABILITY_KINDS:
            continue
        m = manual.describe(conn, r, today)
        base = {"id": m["id"], "manual": True, "name": m["name"], "official_name": None, "institution": m["institution"],
                "mask": None, "balance": m["balance"], "apr": m["interest_rate"], "updated_at": m["updated_at"],
                "stale": m["stale"], "as_of": m["as_of"], "balance_as_of": m["balance_as_of"]}
        if r["kind"] == "credit_card":
            lim = m["credit_limit"]
            cards.append({**base, "limit": lim, "util": _r(m["balance"] / lim * 100) if lim else None,
                          "min_payment": None, "due_date": None, "match_pattern": m["match_pattern"],
                          "auto_updates": m["auto_updates"], "approx": m["approx"], "entered_balance": m["entered_balance"],
                          "matched_total": m["matched_total"], "matched_count": m["matched_count"],
                          "last_payment": m["last_payment"], "nudge": m["nudge"]})
        elif r["kind"] == "loan":
            key = _loan_key(m["subtype"])
            loans.append({**base, "subtype": key, "type_label": LOAN_LABELS[key],
                          "monthly_payment": m["monthly_payment"], "original_amount": m["original_amount"],
                          "paid_pct": m["paid_pct"], "paid_off": m["paid_off"], "payoff": m["payoff"],
                          "next_payment": m["next_payment"], "last_payment": m["last_payment"],
                          "payments": _matched(conn, r), "history": manual.history(conn, r["id"]),
                          "match_pattern": m["match_pattern"], "auto_updates": m["auto_updates"],
                          "applied_count": m["applied_count"]})
        else:
            others.append({**base, "subtype": m["subtype"]})
    cards_total = _r(sum(c["balance"] for c in cards))
    limit_total = _r(sum(c["limit"] or 0 for c in cards if c["limit"]))
    groups = []
    if cards:
        util = _r(sum(c["balance"] for c in cards if c["limit"]) / limit_total * 100) if limit_total else None
        groups.append({"key": "cards", "label": "Credit cards", "count": len(cards), "total": cards_total,
                       "limit": limit_total, "util": util})
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for l in loans:
        by_type[l["subtype"]].append(l)
    for key in LOAN_LABELS:
        ls = by_type.get(key)
        if not ls:
            continue
        orig = sum(l["original_amount"] for l in ls if l["original_amount"])
        left = sum(l["balance"] for l in ls if l["original_amount"])
        groups.append({"key": f"loan:{key}", "label": LOAN_LABELS[key], "count": len(ls),
                       "total": _r(sum(l["balance"] for l in ls)), "original": _r(orig) if orig else None,
                       "paid_pct": _r(min(max((orig - left) / orig, 0), 1) * 100) if orig else None,
                       "loans": [l["id"] for l in ls], "all_paid_off": all(l["paid_off"] for l in ls)})
    if others:
        groups.append({"key": "other", "label": "Other debts", "count": len(others),
                       "total": _r(sum(o["balance"] for o in others))})
    total = _r(cards_total + sum(l["balance"] for l in loans) + sum(o["balance"] for o in others))
    loan_monthly = _r(sum(l["monthly_payment"] or 0 for l in loans if not l["paid_off"]))
    card_minimums = _r(sum(c["min_payment"] or 0 for c in cards))
    upcoming = [{"date": c["due_date"], "name": c["name"], "institution": c["institution"], "kind": "card",
                 "amount": c["min_payment"], "id": c["id"]}
                for c in cards if c["min_payment"] and c["due_date"] and c["due_date"] >= today.isoformat()]
    upcoming += [{"date": l["next_payment"], "name": l["name"], "institution": l["institution"], "kind": "loan",
                  "amount": l["monthly_payment"], "id": l["id"], "type_label": l["type_label"]}
                 for l in loans if l["next_payment"] and not l["paid_off"] and l["monthly_payment"]]
    upcoming.sort(key=lambda u: (u["date"], u["name"] or ""))
    year = str(today.year)
    this_year = [p for l in loans for p in l["payments"] if p["date"].startswith(year)]
    paid_off = [l for l in loans if l["paid_off"]]
    return {
        "total": total,
        "cards": cards,
        "loans": loans,
        "other": others,
        "groups": groups,
        "cards_total": cards_total,
        "cards_limit": limit_total,
        "monthly": {"total": _r(loan_monthly + card_minimums), "loans": loan_monthly, "card_minimums": card_minimums},
        "upcoming": upcoming,
        "stats": {
            "paid_so_far": _r(sum(l["original_amount"] - l["balance"] for l in loans
                                  if l["original_amount"] and l["original_amount"] > l["balance"])),
            "matched_this_year": len(this_year), "matched_this_year_total": _r(sum(p["amount"] for p in this_year)),
            "paid_off_count": len(paid_off), "paid_off_total": _r(sum(l["original_amount"] or 0 for l in paid_off)),
        },
        "today": today.isoformat(),
    }
