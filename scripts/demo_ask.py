#!/usr/bin/env python3
"""Ask v2 for the static demo (t_d31fda2a): the preset questions and their answers, built at build time by the real
read tools (tooldefs.run_read) on the frozen demo database. No model is ever called here.

    build_answers(db_path, today) -> the ask.json payload (before the build's renaming)

Each preset carries:
- question: the chip label (<= 50 characters, one line in the Ask panel),
- need / terms: what demo/shim.js's matcher looks for (concepts from SYNONYMS; `need` is a list of alternatives, each a
  list of concepts that must all be present),
- reply / sources / stats: what the app's own chat renderer draws (chat.js entry()),
- blocks: the structured extras demo/ask.js draws under the answer (tiles, bars, line, columns, cards, progress),
- next: three follow-up preset ids, traces: the tool calls the answer came from, context: the same facts, small, for the
  optional live-AI worker (demo/worker).

Dates in any text are tokens the shim renders after its visitor-relative date shift: {{d:YYYY-MM-DD}} "Oct 26",
{{D:YYYY-MM-DD}} "Oct 26, 2026" (chat.source_line's day label), {{m:YYYY-MM}} "October 2026", {{M:YYYY-MM}} "Oct 2026"
(chat.source_line's month label). render() is the Python twin of the shim's renderer (tests and demo_parity use it).
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta
from typing import Any

WHATIF_EXTRA = 200
MERCHANT_DAYS = 180
MAX_MERCHANTS = 12
STARTERS = ["spending", "budgets", "bills_week"]
UNMATCHED = ["dining", "networth", "goals"]
MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
MONTHS_SHORT = tuple(m[:3] for m in MONTHS_LONG)

# Words -> concepts. Plain words only: renaming (build_demo.RenameMap) must leave every one of them alone, which
# tests/test_demo_ask.py checks against the demo's original names.
SYNONYMS = {
    "spend": ["spend", "spent", "spending", "cost", "costs", "paid", "pay", "expense", "expenses"],
    "month": ["month", "monthly"],
    "last": ["last", "previous", "prior"],
    "week": ["week", "weekly", "7", "seven", "days"],
    "year": ["year", "yearly", "annual", "ytd"],
    "category": ["category", "categories", "breakdown", "type", "kind", "groceries", "grocery", "shopping", "transport",
                 "health", "entertainment", "coffee", "gas", "fuel", "rideshare"],
    "dining": ["dining", "dine", "restaurant", "restaurants", "eating", "eat", "takeout", "dinner", "lunch", "food",
               "meal", "meals", "delivery"],
    "budget": ["budget", "budgets", "budgeting", "cap", "caps", "limit", "limits", "overspend", "overspending"],
    "track": ["track", "pace", "ok", "doing"],
    "over": ["over", "exceed", "exceeded", "blown"],
    "bill": ["bill", "bills", "due", "owe", "upcoming", "rent", "utilities", "utility", "insurance"],
    "subscription": ["subscription", "subscriptions", "subscribed", "streaming", "recurring", "membership",
                     "memberships", "renewal", "renewals"],
    "networth": ["networth", "worth", "wealth", "assets", "rich", "richer", "debts"],
    "change": ["change", "changed", "grow", "grown", "growth", "trend", "history"],
    "goal": ["goal", "goals", "saving", "savings", "progress"],
    "save": ["save", "saved", "saving", "put", "set", "aside"],
    "extra": ["more", "extra", "additional", "increase", "bump", "faster", "sooner"],
    "whatif": ["if", "whatif", "suppose", "imagine", "hypothetically"],
    "trip": ["trip", "travel", "vacation", "holiday", "lisbon", "flight"],
    "merchant": ["merchant", "merchants", "store", "stores", "shop", "shops", "places", "vendors", "brands"],
    "where": ["where", "go", "goes", "going"],
    "top": ["top", "most", "biggest", "largest", "main"],
    "money": ["money", "cash", "dollars"],
    "cashflow": ["cashflow", "flow", "income", "earn", "earned", "earning", "salary", "paycheck", "paychecks"],
    "invest": ["invest", "investing", "investment", "investments", "portfolio", "stocks", "stock", "holdings",
               "shares", "etf", "etfs", "brokerage", "market"],
}
# Whole phrases first, before the words are split ("net worth" -> networth).
PHRASES = {"net worth": "networth", "what if": "whatif", "cash flow": "cashflow", "eating out": "dining",
           "eat out": "dining", "set aside": "save", "money go": "where"}
STOPWORDS = ["a", "an", "the", "i", "me", "my", "mine", "we", "our", "you", "your", "is", "are", "am", "was", "were",
             "be", "been", "do", "does", "did", "have", "has", "had", "how", "what", "which", "who", "when", "why",
             "to", "of", "in", "at", "for", "on", "by", "with", "and", "or", "it", "its", "this", "that", "these",
             "those", "there", "can", "could", "would", "should", "will", "please", "tell", "show", "about", "much",
             "many", "so", "far", "s"]
THRESHOLD = 2


# --- text tokens --------------------------------------------------------------------------------------------------------

def d(iso: str) -> str:
    return "{{d:%s}}" % iso


def m(month: str) -> str:
    return "{{m:%s}}" % month


def add_months(month: str, n: int) -> str:
    y, mo = map(int, month.split("-"))
    k = y * 12 + (mo - 1) + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def render(text: str, days: int = 0, months: int = 0) -> str:
    """The tokens as the visitor reads them, shifted by `days` (dates) and `months` (months): demo/shim.js's render()."""
    def day(iso: str) -> date:
        return date.fromisoformat(iso) + timedelta(days=days)

    def sub(mt: re.Match) -> str:
        kind, val = mt.group(1), mt.group(2)
        if kind in "dD":
            x = day(val)
            return f"{MONTHS_SHORT[x.month - 1]} {x.day}" + (f", {x.year}" if kind == "D" else "")
        mo = add_months(val, months)
        names = MONTHS_LONG if kind == "m" else MONTHS_SHORT
        return f"{names[int(mo[5:]) - 1]} {mo[:4]}"
    return re.sub(r"\{\{([dDmM]):(\d{4}-\d{2}(?:-\d{2})?)\}\}", sub, text)


def tokenize_source(line: str, payload: dict) -> str:
    """chat.source_line's day / month labels for the payload's own dates, as tokens (so the shim can shift them)."""
    from hermes_finance import chat
    dates, months = set(), set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
            dates.add(x)
        elif isinstance(x, str) and re.fullmatch(r"\d{4}-\d{2}", x):
            months.add(x)
    walk({k: payload.get(k) for k in ("period", "month", "today", "months") if k in payload})
    for x in sorted(dates, key=len, reverse=True):
        line = line.replace(chat._day_label(x), "{{D:%s}}" % x)
    for x in sorted(months):
        line = line.replace(chat._month_label(x), "{{M:%s}}" % x)
    return line


def usd(x: float) -> str:
    return ("-" if x < 0 else "") + f"${abs(x):,.2f}"


def joined(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --- presets ------------------------------------------------------------------------------------------------------------

class Builder:
    def __init__(self, db_path, today: date):
        from hermes_finance import tooldefs
        self.tooldefs, self.db_path, self.today = tooldefs, db_path, today
        self.month = today.strftime("%Y-%m")
        self.last = add_months(self.month, -1)

    def read(self, traces: list, name: str, arguments: dict) -> dict:
        result = self.tooldefs.run_read(name, arguments, self.db_path, self.today)
        traces.append({"tool": name, "arguments": arguments, "_result": result})
        return result

    def finish(self, pid: str, question: str, need, terms: dict, reply: str, traces: list, blocks: list,
               nxt: list, stats: list | None = None, merchant: str | None = None) -> dict:
        from hermes_finance import chat
        assert len(question) <= 50, question
        sources = [tokenize_source(chat.source_line(t["tool"], t["_result"]), t["_result"]) for t in traces]
        out = {"id": pid, "question": question, "need": need, "terms": terms, "reply": reply,
               "sources": list(dict.fromkeys(sources)), "stats": stats or [], "cards": [], "blocks": blocks,
               "next": nxt, "traces": [{"tool": t["tool"], "arguments": t["arguments"]} for t in traces]}
        if merchant:
            out["merchant"] = merchant
        out["context"] = {"question": question, "answer": reply, "data": blocks, "stats": stats or []}
        return out

    # How much have I spent this month?
    def spending(self) -> dict:
        tr = []
        p = self.read(tr, "finance_spend", {"month": self.month, "by": "category", "limit": 6})
        top = p["groups"][0] if p["groups"] else None
        ch = p["change"]
        trend = ""
        if p["previous"] and ch["pct"] is not None:
            same = f"the first {plural(p['previous']['days'], 'day')} of {m(self.last)} ({usd(p['previous']['total'])})"
            if abs(ch["pct"]) < 1:
                trend = f", about the same as {same}"
            else:
                trend = f", {'up' if ch['amount'] > 0 else 'down'} {abs(ch['pct']):.0f}% on {same}"
        reply = f"You've spent {usd(p['total'])} so far in {m(self.month)}, across {plural(p['count'], 'payment')}{trend}."
        if top:
            reply += f"\n\n{top['label']} is the biggest share: {usd(top['total'])}, {top['share']:.0f}% of the month."
        tiles = [{"label": "Spent so far", "value": p["total"], "fmt": "money"},
                 {"label": "Payments", "value": p["count"], "fmt": "count"}]
        if p["previous"] and ch["pct"] is not None:
            tiles.append({"label": "Same days last month", "value": ch["pct"], "fmt": "pct",
                          "tone": "warn" if ch["amount"] > 0 else "ok"})
        bars = {"type": "bars", "title": "By category", "fmt": "money",
                "items": [{"label": g["label"], "value": g["total"], "category": g["label"],
                           "note": f"{g['share']:.0f}%"} for g in p["groups"]]}
        return self.finish("spending", "How much have I spent this month?", [["spend"], ["category"]],
                           {"spend": 2, "month": 1, "category": 2}, reply, tr,
                           [{"type": "tiles", "items": tiles}, bars], ["dining", "top_merchants", "budgets"])

    # How much did I spend on dining last month?
    def dining(self) -> dict:
        tr = []
        cat = "Eating Out"
        p = self.read(tr, "finance_spend", {"month": self.last, "category": cat, "by": "merchant", "limit": 5})
        t = self.read(tr, "finance_transactions", {"month": self.last, "category": cat, "limit": 3})
        reply = (f"You spent {usd(p['total'])} on dining out in {m(self.last)}, over "
                 f"{plural(p['count'], 'payment')}.")
        if p["previous"]:
            ch = p["change"]
            way = "more" if ch["amount"] > 0 else "less"
            reply += f" That's {usd(abs(ch['amount']))} {way} than {m(add_months(self.last, -1))}."
        if p["groups"]:
            g = p["groups"][0]
            reply += f"\n\n{g['label']} was your top spot: {usd(g['total'])} across {plural(g['count'], 'visit')}."
        tiles = [{"label": f"Dining, {m(self.last)}", "value": p["total"], "fmt": "money"},
                 {"label": "Payments", "value": p["count"], "fmt": "count"},
                 {"label": "Average", "value": round(p["total"] / p["count"], 2) if p["count"] else 0, "fmt": "money"}]
        bars = {"type": "bars", "title": "Where", "fmt": "money",
                "items": [{"label": g["label"], "value": g["total"], "merchant": g["label"], "logo_url": None,
                           "note": plural(g["count"], "visit")} for g in p["groups"]]}
        cards = {"type": "cards", "title": "Latest", "items": [self.tx_card(r) for r in t["rows"]]}
        return self.finish("dining", "How much did I spend on dining last month?", [["dining"]],
                           {"dining": 3, "spend": 1, "last": 1, "month": 1}, reply, tr,
                           [{"type": "tiles", "items": tiles}, bars, cards], ["spending", "budgets", "top_merchants"])

    @staticmethod
    def tx_card(r: dict) -> dict:
        return {"title": r["merchant"] or r["name"], "merchant": r["merchant"] or r["name"], "logo_url": None,
                "date": r["date"], "amount": r["amount"], "note": r["category"]}

    # Am I on track with my budgets this month?
    def budgets(self) -> dict:
        tr = []
        p = self.read(tr, "finance_budget_status", {"month": self.month})
        t = p["totals"]
        caps = p["caps"]
        over = [c for c in caps if c["status"] == "over"]
        warn = [c for c in caps if c["status"] == "warn"]
        days = plural(p["days_left"], "day")
        if t["left"] >= 0 and (over or t["left"] < 0.1 * t["budgeted"]):
            head = f"Only just: {usd(t['left'])} of your {usd(t['budgeted'])} budget is left, with {days} to go."
        elif t["left"] >= 0:
            head = f"Yes: {usd(t['left'])} of your {usd(t['budgeted'])} budget is left, with {days} to go."
        else:
            head = f"Not quite: you're {usd(-t['left'])} over your {usd(t['budgeted'])} budget, with {days} to go."
        bits = []
        if over:
            bits.append(f"{joined([c['category'] for c in over])} {'is' if len(over) == 1 else 'are'} over")
        if warn:
            bits.append(f"{joined([c['category'] for c in warn])} {'is' if len(warn) == 1 else 'are'} past 80%")
        reply = head + ("\n\n" + "; ".join(bits) + "." if bits else "\n\nEvery capped category is under 80%.")
        rank = {"over": 0, "warn": 1, "ok": 2}
        pick = sorted(caps, key=lambda c: (rank.get(c["status"], 3), -(c["pct"] or 0)))[:3]
        stats = [{k: c[k] for k in ("category", "cap", "spent", "due", "count", "status")} for c in pick]
        tiles = [{"label": "Budgeted", "value": t["budgeted"], "fmt": "money"},
                 {"label": "Spent", "value": t["spent"], "fmt": "money"},
                 {"label": "Left" if t["left"] >= 0 else "Over", "value": abs(t["left"]), "fmt": "money",
                  "tone": "ok" if t["left"] >= 0 else "bad"},
                 {"label": "Days left", "value": p["days_left"], "fmt": "count"}]
        return self.finish("budgets", "Am I on track with my budgets this month?", [["budget"]],
                           {"budget": 3, "track": 1, "month": 1, "over": 1}, reply, tr,
                           [{"type": "tiles", "items": tiles}], ["spending", "bills_week", "goals"], stats=stats)

    def bill_card(self, b: dict) -> dict:
        return {"title": b["merchant"], "merchant": b["merchant"], "logo_url": None, "date": b["next_expected"],
                "amount": b["amount"], "days": b["days_until"],
                "badge": "Subscription" if b["kind"] == "subscription" else "Bill", "note": b["cadence_label"]}

    # What bills are due in the next 7 days?
    def bills_week(self) -> dict:
        tr = []
        p = self.read(tr, "finance_bills", {"next": "7d"})
        items = p["items"]
        total = round(sum(b["amount"] for b in items), 2)
        if items:
            first = items[0]
            reply = (f"{plural(len(items), 'charge')} {'is' if len(items) == 1 else 'are'} due in the next 7 days, "
                     f"{usd(total)} in all. The first is {first['merchant']} ({usd(first['amount'])}) on "
                     f"{d(first['next_expected'])}.")
        else:
            reply = "Nothing is due in the next 7 days."
        reply += (f"\n\nAll your bills come to about {usd(p['monthly_totals']['bill'])} a month, plus "
                  f"{usd(p['monthly_totals']['subscription'])} in subscriptions.")
        tiles = [{"label": "Due this week", "value": total, "fmt": "money"},
                 {"label": "Charges", "value": len(items), "fmt": "count"},
                 {"label": "Bills a month", "value": p["monthly_totals"]["bill"], "fmt": "money"}]
        cards = {"type": "cards", "title": "Coming up", "items": [self.bill_card(b) for b in items]}
        return self.finish("bills_week", "What bills are due in the next 7 days?", [["bill"]],
                           {"bill": 3, "week": 2}, reply, tr, [{"type": "tiles", "items": tiles}, cards],
                           ["subscriptions", "budgets", "cashflow"])

    # Which subscriptions am I paying for?
    def subscriptions(self) -> dict:
        tr = []
        p = self.read(tr, "finance_bills", {"next": "35d"})
        subs = [b for b in p["items"] if b["kind"] == "subscription"]
        monthly = p["monthly_totals"]["subscription"]
        priciest = max(subs, key=lambda b: b["monthly_amount"] or 0) if subs else None
        reply = f"You have {plural(len(subs), 'subscription')}, about {usd(monthly)} a month ({usd(monthly * 12)} a year)."
        if priciest:
            reply += f"\n\nThe priciest is {priciest['merchant']}, about {usd(priciest['monthly_amount'])} a month."
        tiles = [{"label": "A month", "value": monthly, "fmt": "money"},
                 {"label": "A year", "value": round(monthly * 12, 2), "fmt": "money"},
                 {"label": "Subscriptions", "value": len(subs), "fmt": "count"}]
        cards = {"type": "cards", "title": "Next charges", "items": [self.bill_card(b) for b in subs]}
        return self.finish("subscriptions", "Which subscriptions am I paying for?", [["subscription"]],
                           {"subscription": 3, "month": 1, "spend": 1}, reply, tr,
                           [{"type": "tiles", "items": tiles}, cards], ["bills_week", "spending", "cashflow"])

    # How has my net worth changed this year?
    def networth(self) -> dict:
        tr = []
        p = self.read(tr, "finance_networth", {"history": True})
        start = f"{self.today.year}-01-01"
        points = [h for h in p["history"] if start <= h["date"] <= self.today.isoformat()]
        first, final = points[0], points[-1]
        change = round(final["net"] - first["net"], 2)
        way = "up" if change >= 0 else "down"
        reply = (f"Your net worth is {usd(p['net'])}: you own {usd(p['assets'])} and owe {usd(p['debts'])}.\n\n"
                 f"Since {d(first['date'])} it's {way} {usd(abs(change))}, from {usd(first['net'])}. "
                 "Cars and properties count at their latest valuation.")
        groups = [g for g in p["groups"] if g["signed"]]
        tiles = [{"label": "Net worth", "value": p["net"], "fmt": "money"},
                 {"label": "This year", "value": change, "fmt": "money", "signed": True,
                  "tone": "ok" if change >= 0 else "bad"},
                 {"label": "Owe", "value": p["debts"], "fmt": "money"}]
        line = {"type": "line", "title": "Net worth this year", "fmt": "money",
                "points": [{"date": h["date"], "value": h["net"]} for h in points]}
        bars = {"type": "bars", "title": "What it's made of", "fmt": "money",
                "items": [{"label": g["label"], "value": abs(g["signed"]), "negative": g["signed"] < 0}
                          for g in groups]}
        return self.finish("networth", "How has my net worth changed this year?", [["networth"]],
                           {"networth": 3, "change": 1, "year": 1}, reply, tr,
                           [{"type": "tiles", "items": tiles}, line, bars], ["investments", "goals", "cashflow"])

    def goal_item(self, g: dict) -> dict:
        pace = g.get("pace") or {}
        when = pace.get("on_pace_month")
        return {"name": g["name"], "icon": g["icon"], "colour": g["colour"], "progress": g["progress"],
                "goal": g["target"], "pct": g["pct"], "note": f"on pace for {m(when)}" if when else None}

    # How are my savings goals doing?
    def goals(self) -> dict:
        tr = []
        p = self.read(tr, "finance_goals", {})
        goals = [g for g in p["goals"] if not g["done"] and g["kind"] == "save"]
        best = max(goals, key=lambda g: g["pct"]) if goals else None
        tot = p.get("totals") or {}
        reply = f"You have {plural(len(goals), 'savings goal')} going"
        if tot.get("saved") is not None and tot.get("saving_target"):
            reply += f", {usd(tot['saved'])} saved of {usd(tot['saving_target'])} ({tot['pct']:.0f}%)"
        reply += "."
        if best:
            pace = best["pace"] or {}
            reply += f"\n\n{best['name']} is closest: {best['pct']:.0f}% there, {usd(best['left'])} to go"
            reply += f", on pace for {m(pace['on_pace_month'])}." if pace.get("on_pace_month") else "."
        prog = {"type": "progress", "items": [self.goal_item(g) for g in goals]}
        return self.finish("goals", "How are my savings goals doing?", [["goal"]],
                           {"goal": 3, "track": 1, "save": 1}, reply, tr, [prog], ["whatif", "networth", "budgets"])

    # What if I saved $200 more a month for my trip?
    def whatif(self) -> dict:
        tr = []
        p = self.read(tr, "finance_goals", {})
        cands = [g for g in p["goals"] if g["kind"] == "save" and not g["done"] and (g.get("pace") or {}).get("per_month")]
        g = next((x for x in cands if "trip" in x["name"].lower()), None) or max(cands, key=lambda x: x["left"])
        pace = g["pace"]
        per, left = pace["per_month"], g["left"]
        now_n = math.ceil(left / per)
        new_n = math.ceil(left / (per + WHATIF_EXTRA))
        now_m, new_m = add_months(self.month, now_n), add_months(self.month, new_n)
        sooner = now_n - new_n
        reply = (f"{g['name']} has {usd(left)} to go. At your current pace of {usd(per)} a month you'd get there in "
                 f"{m(now_m)}.\n\nAdding ${WHATIF_EXTRA} a month ({usd(per + WHATIF_EXTRA)} in all) gets you there in "
                 f"{m(new_m)}" + (f", {plural(sooner, 'month')} sooner." if sooner else ", the same month."))
        tiles = [{"label": "Left to save", "value": left, "fmt": "money"},
                 {"label": "Now", "value": m(now_m), "fmt": "text"},
                 {"label": f"With ${WHATIF_EXTRA} more", "value": m(new_m), "fmt": "text", "tone": "ok"}]
        prog = {"type": "progress", "items": [self.goal_item(g)]}
        out = self.finish("whatif", f"What if I saved ${WHATIF_EXTRA} more a month for my trip?",
                          [["whatif"], ["extra", "save"]], {"whatif": 3, "extra": 2, "save": 2, "trip": 2, "goal": 1},
                          reply, tr, [{"type": "tiles", "items": tiles}, prog], ["goals", "cashflow", "spending"])
        out["context"]["math"] = {"goal": g["name"], "left": left, "per_month": per, "extra": WHATIF_EXTRA,
                                  "months_now": now_n, "months_with_extra": new_n}
        return out

    # Where does most of my money go?
    def top_merchants(self) -> dict:
        tr = []
        p = self.read(tr, "finance_spend", {"since": "90d", "by": "merchant", "limit": 6})
        g = p["groups"]
        reply = (f"Over the last 90 days you spent {usd(p['total'])} at {plural(p['group_count'], 'place')}. "
                 f"The top {len(g)} take {sum(x['share'] for x in g):.0f}% of it.")
        if g:
            reply += f"\n\n{g[0]['label']} leads with {usd(g[0]['total'])} ({g[0]['share']:.0f}%)."
        bars = {"type": "bars", "title": "Top places, 90 days", "fmt": "money",
                "items": [{"label": x["label"], "value": x["total"], "merchant": x["label"], "logo_url": None,
                           "note": f"{x['share']:.0f}%"} for x in g]}
        tiles = [{"label": "Spent, 90 days", "value": p["total"], "fmt": "money"},
                 {"label": "Places", "value": p["group_count"], "fmt": "count"}]
        return self.finish("top_merchants", "Where does most of my money go?", [["where"], ["merchant"], ["top", "spend"]],
                           {"where": 2, "merchant": 3, "top": 2, "money": 1, "spend": 1}, reply, tr,
                           [{"type": "tiles", "items": tiles}, bars], ["merchant", "spending", "subscriptions"])

    def merchant_names(self) -> list[str]:
        p = self.tooldefs.run_read("finance_spend", {"since": f"{MERCHANT_DAYS}d", "by": "merchant", "limit": 60},
                                   self.db_path, self.today)
        bills = self.tooldefs.run_read("finance_bills", {"next": "400d"}, self.db_path, self.today)
        recurring = {b["merchant"] for b in bills["items"]}
        groups = sorted(p["groups"], key=lambda g: (-g["count"], -g["total"], g["label"]))
        fits = lambda n: len(f"How much do I spend at {n}?") <= 50   # (a preset question stays short)
        return [g["label"] for g in groups if g["label"] not in recurring and g["count"] >= 3 and fits(g["label"])][:MAX_MERCHANTS]

    # How much do I spend at <merchant>?
    def merchant(self, name: str, pid: str) -> dict:
        tr = []
        p = self.read(tr, "finance_spend", {"since": f"{MERCHANT_DAYS}d", "merchant": name, "by": "month"})
        t = self.read(tr, "finance_transactions", {"since": f"{MERCHANT_DAYS}d", "merchant": name, "limit": 3})
        avg = round(p["total"] / p["count"], 2) if p["count"] else 0
        months = sorted(p["groups"], key=lambda g: g["key"])
        reply = (f"Over the last {MERCHANT_DAYS} days you spent {usd(p['total'])} at {name}, "
                 f"{plural(p['count'], 'payment')} averaging {usd(avg)}.")
        if months:
            hi = max(months, key=lambda g: g["total"])
            reply += f"\n\nYour biggest month there was {m(hi['key'])} at {usd(hi['total'])}."
        tiles = [{"label": f"{MERCHANT_DAYS} days", "value": p["total"], "fmt": "money"},
                 {"label": "Payments", "value": p["count"], "fmt": "count"},
                 {"label": "Average", "value": avg, "fmt": "money"}]
        cols = {"type": "columns", "title": "By month", "fmt": "money", "series": [{"key": "value", "label": name}],
                "items": [{"month": g["key"], "value": g["total"]} for g in months]}
        cards = {"type": "cards", "title": "Latest", "items": [self.tx_card(r) for r in t["rows"]]}
        return self.finish(pid, f"How much do I spend at {name}?", [["merchant_name"]], {"merchant_name": 5, "spend": 1},
                           reply, tr, [{"type": "tiles", "items": tiles}, cols, cards],
                           ["top_merchants", "spending", "dining"], merchant=name)

    # How does my income compare with spending?
    def cashflow(self) -> dict:
        tr = []
        p = self.read(tr, "finance_cashflow", {"months": 6})
        full = [x for x in p["months"] if not x["is_current"]]
        inc = round(sum(x["income"] for x in full) / len(full), 2) if full else 0
        spend = round(sum(x["spending"] for x in full) / len(full), 2) if full else 0
        net = round(sum(x["net"] for x in full), 2)
        reply = (f"Over the last {plural(len(full), 'full month')} you brought in about {usd(inc)} a month and spent about "
                 f"{usd(spend)}, so you kept {usd(net)} in all.")
        cur = next((x for x in p["months"] if x["is_current"]), None)
        if cur:
            reply += f"\n\nSo far in {m(cur['month'])}: {usd(cur['income'])} in, {usd(cur['spending'])} out."
        tiles = [{"label": "Income a month", "value": inc, "fmt": "money"},
                 {"label": "Spending a month", "value": spend, "fmt": "money"},
                 {"label": "Kept", "value": net, "fmt": "money", "tone": "ok" if net >= 0 else "bad"}]
        cols = {"type": "columns", "title": "In and out", "fmt": "money",
                "series": [{"key": "income", "label": "Income"}, {"key": "spending", "label": "Spending"}],
                "items": [{"month": x["month"], "income": x["income"], "spending": x["spending"]} for x in p["months"]]}
        return self.finish("cashflow", "How does my income compare with spending?", [["cashflow"]],
                           {"cashflow": 3, "spend": 1, "month": 1}, reply, tr, [{"type": "tiles", "items": tiles}, cols],
                           ["spending", "goals", "networth"])

    # How are my investments doing?
    def investments(self) -> dict:
        tr = []
        p = self.read(tr, "finance_holdings", {})
        ch = (p.get("changes") or {}).get("7d")
        reply = f"Your investments are worth {usd(p['total'])} across {plural(len(p['accounts']), 'account')}."
        if ch:
            way = "up" if ch["amount"] >= 0 else "down"
            reply += f" Over the last week they're {way} {usd(abs(ch['amount']))}."
        top = sorted(p["holdings"], key=lambda h: -h["value"])[:5]
        if top:
            reply += f"\n\nYour largest holding is {top[0]['ticker'] or top[0]['name']} at {usd(top[0]['value'])}."
        tiles = [{"label": "Portfolio", "value": p["total"], "fmt": "money"}]
        if ch:
            tiles.append({"label": "Last 7 days", "value": ch["amount"], "fmt": "money", "signed": True,
                          "tone": "ok" if ch["amount"] >= 0 else "bad"})
        bars = {"type": "bars", "title": "By account type", "fmt": "money",
                "items": [{"label": b["label"], "value": b["value"]} for b in p["breakdown"] if b["value"]]}
        hold = {"type": "bars", "title": "Largest holdings", "fmt": "money",
                "items": [{"label": h["ticker"] or h["name"], "value": h["value"], "note": h["account_name"]} for h in top]}
        return self.finish("investments", "How are my investments doing?", [["invest"]], {"invest": 3, "change": 1},
                           reply, tr, [{"type": "tiles", "items": tiles}, bars, hold], ["networth", "cashflow", "goals"])


def build_answers(db_path, today: date) -> dict:
    """The whole ask.json payload (names not yet renamed)."""
    b = Builder(db_path, today)
    presets = [b.spending(), b.dining(), b.budgets(), b.bills_week(), b.subscriptions(), b.networth(), b.goals(),
               b.whatif(), b.top_merchants()]
    names = b.merchant_names()
    merchants = [b.merchant(n, "merchant" if i == 0 else f"merchant-{i:02d}") for i, n in enumerate(names)]
    presets += [merchants[0], b.cashflow(), b.investments()]
    for t in [*presets, *merchants[1:]]:
        for x in t["traces"]:
            x.pop("_result", None)
    out = {"version": 2,
           "core": [p["id"] for p in presets],
           "presets": {p["id"]: p for p in [*presets, *merchants[1:]]},
           "starters": STARTERS,
           "unmatched": {"reply": "I can only answer a few questions in this demo: it runs on made-up numbers and no "
                                  "model reads what you type. Try one of these:",
                         "sources": ["Demo answers are built from the real tools ahead of time"],
                         "suggestions": UNMATCHED},
           "matcher": {"synonyms": SYNONYMS, "phrases": PHRASES, "stopwords": STOPWORDS, "threshold": THRESHOLD},
           "live": {"note": "Live AI busy, showing the demo answer", "source": "Live AI · a free model on OpenRouter, "
                    "reading only this answer's numbers"}}
    for p in out["presets"].values():
        assert all(n in out["presets"] for n in p["next"]), p["id"]
        assert len(json.dumps(p["context"])) <= 6000, p["id"]
    return out


if __name__ == "__main__":   # a look at the answers: python scripts/demo_ask.py DB YYYY-MM-DD
    import sys
    data = build_answers(sys.argv[1], date.fromisoformat(sys.argv[2]))
    for pid in data["core"]:
        p = data["presets"][pid]
        print(f"== {pid}: {p['question']}\n{render(p['reply'])}\n   {[render(s) for s in p['sources']]}\n")
