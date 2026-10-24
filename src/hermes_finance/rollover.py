"""Budget rollover (TASK-19): a per-category switch in the cap editor, off by default.

When it's on, last month's unused cap (cap - spent, never below 0: overspending does not roll over in v1) is added to
this month's cap. That effective cap is what left-to-spend, the rings, the alerts and budget-status use
(insights.category_spend: `cap` is the effective cap, `base_cap` the one you set, `rollover` = {amount, from}). Only last
month's own cap and spending count: a rolled-over amount doesn't roll again. With every switch off, nothing changes
anywhere (no extra key in any payload).
"""

from __future__ import annotations

import sqlite3

from .db import now


class RolloverError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.field = field


def enabled(conn: sqlite3.Connection) -> set[int]:
    """The category ids with rollover on."""
    try:
        return {r[0] for r in conn.execute("SELECT category_id FROM category_rollover")}
    except sqlite3.OperationalError:  # a database from before TASK-19 (read-only, not upgraded yet)
        return set()


def carry(cap: float, last_month_spend: float) -> float:
    """What rolls into this month: last month's cap minus what was spent in it, never below 0 (in cents)."""
    return max(round(cap * 100) - round(last_month_spend * 100), 0) / 100


def set_enabled(conn: sqlite3.Connection, category: str, on: bool) -> dict:
    """Turn a spending category's rollover on or off. It needs a cap to roll over (off always works)."""
    if not isinstance(category, str) or not category.strip():
        raise RolloverError("category is required", field="category")
    if not isinstance(on, bool):
        raise RolloverError("enabled must be true or false", field="enabled")
    row = conn.execute("SELECT id, name, monthly_cap, is_income, is_transfer FROM categories WHERE name = ? COLLATE NOCASE",
                       (category.strip(),)).fetchone()
    if row is None:
        raise RolloverError(f"unknown category {category[:80]!r}", 404, "category")
    if row["is_income"] or row["is_transfer"]:
        raise RolloverError(f"{row['name']} is not a spending category", field="category")
    if on and row["monthly_cap"] is None:
        raise RolloverError(f"{row['name']} has no budget: set one first, then its unused part can roll over", field="enabled")
    with conn:
        if on:
            conn.execute("INSERT OR IGNORE INTO category_rollover (category_id, enabled_at) VALUES (?, ?)", (row["id"], now()))
        else:
            conn.execute("DELETE FROM category_rollover WHERE category_id = ?", (row["id"],))
    return {"category": row["name"], "enabled": on}
