"""Group budgets (polish P2e): a monthly budget on a whole category group (db.CATEGORY_GROUPS: Food, Transport, ...),
and the one-time review that moves category budgets to their groups.

There is no mode: each group has a budget of its own, category caps, or both.
  - A group WITH a budget is one unit on every page, in every total and in budget-status: Planned = the budget, Spent =
    every spending category in the group (split-aware: category_spend's spend, summed), due = the bills still due this
    month in those categories, left = budget - spent - due, status = cap_status(spent + due, budget). The caps inside it
    are still shown, with their own numbers and alerts, but they are not counted in any total (`counted: false`,
    `in_group_budget`).
  - A group WITHOUT one but with caps (polish P2a): its caps count one by one and its Planned is their sum. Since the
    group roll-up (2026-10-22, `rest`) its uncapped categories' spending counts too: in its Spent / Left and the totals.
Group budgets never roll over.

The move is offered, never automatic. A group is eligible when it has no budget, isn't kept, and holds at least one
capped spending category. Applying the review (apply_move) is one transaction: "Group budget" sets the group's budget and
removes the caps it replaces (their rollover goes off with them); "Keep category budgets" records the group as kept, so
it's never offered again. Eligibility is checked again inside the transaction, so a second run changes nothing.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from .db import now

MAX_BUDGET = 100_000   # the same bound as a category cap (api.MAX_CAP)
CHOICES = ("group", "keep")


class GroupBudgetError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.field = field


def amounts(conn: sqlite3.Connection) -> dict[str, float]:
    """{group name: its monthly budget}; {} when none is set."""
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT g.name, b.amount FROM group_budgets b JOIN category_groups g ON g.id = b.group_id")}
    except sqlite3.OperationalError:  # a database from before v18 (read-only, not upgraded yet)
        return {}


def kept(conn: sqlite3.Connection) -> set[str]:
    """The groups whose category budgets were kept in the review."""
    try:
        return {r[0] for r in conn.execute(
            "SELECT g.name FROM group_budget_kept k JOIN category_groups g ON g.id = k.group_id")}
    except sqlite3.OperationalError:
        return set()


def layout(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every group in the taxonomy's display order, each with its spending categories in their order. A spending
    category with no group (made by hand) files under Misc, like the rest of the app (api.budget_groups)."""
    groups = {r["name"]: {"id": r["id"], "name": r["name"], "rgb": r["rgb"], "categories": []}
              for r in conn.execute("SELECT id, name, rgb FROM category_groups ORDER BY sort, id")}
    if not groups:
        return []
    fallback = groups.get("Misc") or next(iter(groups.values()))
    for name, grp in conn.execute("SELECT c.name, g.name FROM categories c LEFT JOIN category_groups g ON g.id = c.group_id "
                                  "WHERE c.is_income = 0 AND c.is_transfer = 0 ORDER BY c.sort, c.name"):
        groups.get(grp, fallback)["categories"].append(name)
    return list(groups.values())


def units(conn: sqlite3.Connection, cats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The groups with a budget, each as one budget (see the module docstring), in the taxonomy's order. `cats` are
    insights.category_spend() rows. `cap` repeats `budget` so a unit reads like a capped category in the totals; `caps`
    are the category caps inside (the Planned each row shows: rollover included), `caps_total` their sum, and
    `over_note` that sum when it's more than the budget (the page's "categories add up to $X"), else None."""
    from . import insights
    have = amounts(conn)
    if not have:
        return []
    by_name = {c["category"]: c for c in cats}
    out = []
    for g in layout(conn):
        if g["name"] not in have:
            continue
        budget = have[g["name"]]
        rows = [by_name[n] for n in g["categories"] if n in by_name]
        spend = insights._r(sum(c["spend"] for c in rows))
        due = insights._r(sum(c["due"] for c in rows))
        caps = [{"category": c["category"], "cap": c["cap"]} for c in rows if c["cap"] is not None]
        caps_total = insights._r(sum(c["cap"] for c in caps))
        out.append({
            "group": g["name"], "rgb": g["rgb"], "budget": budget, "cap": budget,
            "spend": spend, "due": due, "left": insights._r(budget - spend - due),
            "pct": insights._r(spend / budget * 100) if budget else None,
            "status": insights.cap_status(spend + due, budget),
            "categories": [c["category"] for c in rows],
            "caps": caps, "caps_total": caps_total,
            "over_note": caps_total if round(caps_total * 100) > round(budget * 100) else None,
            "bills_due": sorted((b for c in rows for b in c["bills_due"]), key=lambda b: (b["next_expected"], b["merchant"])),
        })
    return out


def rest(conn: sqlite3.Connection, cats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group roll-up (2026-10-22): a group WITHOUT a budget of its own but with at least one capped category counts all
    of its spending, not only its capped categories'. Its Planned is still its caps' sum; the spending (and bills
    still due) of its UNCAPPED categories is its `rest`, which counts in the group's Spent / Left and in the page's
    totals. One entry per such group whose uncapped categories have spending or bills due this month: `caps` (its
    capped categories), `categories` (the uncapped ones with spend != 0 or a bill due: the ones shown under the group,
    never in No budget yet), `spend` / `due` (summed over every uncapped category in it) and `bills_due`. A group with
    no cap and no budget has no rest: its categories are in No budget yet, as before."""
    from . import insights
    have = amounts(conn)
    by_name = {c["category"]: c for c in cats}
    out = []
    for g in layout(conn):
        if g["name"] in have:
            continue
        rows = [by_name[n] for n in g["categories"] if n in by_name]
        caps = [c["category"] for c in rows if c["cap"] is not None]
        loose = [c for c in rows if c["cap"] is None]
        shown = [c["category"] for c in loose if c["spend"] != 0 or c["due"] > 0]
        if not caps or not shown:
            continue
        out.append({
            "group": g["name"], "caps": caps, "categories": shown,
            "spend": insights._r(sum(c["spend"] for c in loose)), "due": insights._r(sum(c["due"] for c in loose)),
            "bills_due": sorted((b for c in loose for b in c["bills_due"]), key=lambda b: (b["next_expected"], b["merchant"])),
        })
    return out


def move_plan(conn: sqlite3.Connection, cats: list[dict[str, Any]]) -> dict[str, Any]:
    """The one-time review: each eligible group with its caps (the ones you set, never a rolled-over amount), the
    proposed budget (their sum) and what the whole group spent in the month of `cats`. `count` is the number of caps in
    those groups: the banner's "Move N budgets to groups"."""
    from . import insights
    have, gone = amounts(conn), kept(conn)
    by_name = {c["category"]: c for c in cats}
    groups = []
    for g in layout(conn):
        if g["name"] in have or g["name"] in gone:
            continue
        rows = [by_name[n] for n in g["categories"] if n in by_name]
        caps = [{"category": c["category"], "cap": c.get("base_cap", c["cap"])} for c in rows if c["cap"] is not None]
        if caps:
            groups.append({"group": g["name"], "rgb": g["rgb"], "caps": caps,
                           "amount": insights._r(sum(c["cap"] for c in caps)),
                           "spent": insights._r(sum(c["spend"] for c in rows))})
    return {"count": sum(len(g["caps"]) for g in groups), "groups": groups}


def eligible(conn: sqlite3.Connection) -> dict[str, list[tuple[int, str]]]:
    """{group name: [(category id, name), ...] its capped categories} for every eligible group, read from the database."""
    have, gone = amounts(conn), kept(conn)
    capped = {r[1]: r[0] for r in conn.execute("SELECT id, name FROM categories WHERE monthly_cap IS NOT NULL "
                                               "AND is_income = 0 AND is_transfer = 0")}
    out = {}
    for g in layout(conn):
        caps = [(capped[n], n) for n in g["categories"] if n in capped]
        if caps and g["name"] not in have and g["name"] not in gone:
            out[g["name"]] = caps
    return out


def amount_of(value: Any, field: str) -> float:
    """A budget amount, checked like a category cap (api.set_cap): a number from 0 to 100,000, kept to the cent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GroupBudgetError(f"{field} must be a number or null", field=field)
    if isinstance(value, int) and abs(value) > MAX_BUDGET:  # also a huge int, which math.isfinite can't take
        raise GroupBudgetError(f"{field} must be between 0 and {MAX_BUDGET:,}", field=field)
    if not math.isfinite(value) or not 0 <= value <= MAX_BUDGET:
        raise GroupBudgetError(f"{field} must be between 0 and {MAX_BUDGET:,}", field=field)
    return round(float(value), 2)


def _group(conn: sqlite3.Connection, name: Any, field: str) -> sqlite3.Row:
    if not isinstance(name, str) or not name.strip():
        raise GroupBudgetError("group is required", field=field)
    row = conn.execute("SELECT id, name FROM category_groups WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()
    if row is None:
        raise GroupBudgetError(f"unknown group {name[:80]!r}", field=field)
    return row


def set_budget(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """Set (0..100,000) or clear (null) a group's monthly budget: POST /api/budgets/group {group, monthly_budget}.
    Cleared, the group's Planned is the sum of its caps again. The caps inside are never touched."""
    row = _group(conn, data.get("group"), "group")
    if "monthly_budget" not in data:
        raise GroupBudgetError("monthly_budget is required: a number, or null to clear the group budget", field="monthly_budget")
    amount = data["monthly_budget"]
    if amount is not None:
        amount = amount_of(amount, "monthly_budget")
    stamp = now()
    with conn:
        if amount is None:
            conn.execute("DELETE FROM group_budgets WHERE group_id = ?", (row["id"],))
        else:
            conn.execute("INSERT INTO group_budgets (group_id, amount, created_at, updated_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT (group_id) DO UPDATE SET amount = excluded.amount, updated_at = excluded.updated_at",
                         (row["id"], amount, stamp, stamp))
    return {"group": row["name"], "monthly_budget": amount}


def _choices(conn: sqlite3.Connection, data: dict[str, Any]) -> list[tuple[sqlite3.Row, str, float | None]]:
    """The review's answers, every one checked before anything is written."""
    items = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise GroupBudgetError("groups is required: a list of {group, choice, amount}", field="groups")
    if len(items) > 100:
        raise GroupBudgetError("too many groups", field="groups")
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise GroupBudgetError("each group must be an object: {group, choice, amount}", field="groups")
        row = _group(conn, item.get("group"), "groups")
        if row["id"] in seen:
            raise GroupBudgetError(f"{row['name']} is listed twice", field="groups")
        seen.add(row["id"])
        choice = item.get("choice")
        if choice not in CHOICES:
            raise GroupBudgetError(f"{row['name']}: choice must be 'group' or 'keep'", field="choice")
        amount = None
        if choice == "group":
            if item.get("amount") is None:
                raise GroupBudgetError(f"{row['name']}: amount is required for a group budget", field="amount")
            amount = amount_of(item["amount"], "amount")
        out.append((row, choice, amount))
    return out


def apply_move(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """Apply the review: POST /api/budgets/move {groups: [{group, choice: "group" | "keep", amount}]}. All or nothing:
    every answer is checked first (any error: nothing is written), then one transaction checks each group's eligibility
    again and skips one that isn't (already moved or kept, or no cap left), so running it twice changes nothing.
    "group": the group's budget is set to `amount` and the caps it replaces are removed, their rollover switched off.
    "keep": the group is recorded as kept and its caps stay. Returns what changed and the caps still to review."""
    choices = _choices(conn, data)
    moved, kept_now, skipped = [], [], []
    stamp = now()
    outer = conn.in_transaction   # inside a caller's transaction: a savepoint, undone on its own on an error
    conn.execute("SAVEPOINT group_move" if outer else "BEGIN IMMEDIATE")
    try:
        open_ = eligible(conn)
        rolls = {r[0] for r in conn.execute("SELECT category_id FROM category_rollover")}
        for row, choice, amount in choices:
            caps = open_.get(row["name"])
            if caps is None:
                skipped.append({"group": row["name"], "reason": "not eligible: it has a group budget, was kept, or has no category budget"})
                continue
            names = [n for _, n in caps]
            if choice == "keep":
                conn.execute("INSERT OR IGNORE INTO group_budget_kept (group_id, kept_at) VALUES (?, ?)", (row["id"], stamp))
                kept_now.append({"group": row["name"], "categories": names})
                continue
            ids = [i for i, _ in caps]
            marks = ", ".join("?" * len(ids))
            conn.execute("INSERT INTO group_budgets (group_id, amount, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         (row["id"], amount, stamp, stamp))
            conn.execute(f"UPDATE categories SET monthly_cap = NULL WHERE id IN ({marks})", ids)
            conn.execute(f"DELETE FROM category_rollover WHERE category_id IN ({marks})", ids)
            moved.append({"group": row["name"], "monthly_budget": amount, "categories": names,
                          "rollover_off": [n for i, n in caps if i in rolls]})
        if outer:
            conn.execute("RELEASE group_move")
        else:
            conn.commit()
    except BaseException:
        if not outer:
            conn.rollback()
        elif conn.in_transaction:
            conn.execute("ROLLBACK TO group_move")
            conn.execute("RELEASE group_move")
        raise
    return {"moved": moved, "kept": kept_now, "skipped": skipped,
            "remaining": sum(len(c) for c in eligible(conn).values())}
