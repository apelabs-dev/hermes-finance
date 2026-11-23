"""The one domain layer for categories (v23, 2026-11-15): create, rename, hide / unhide, re-icon, move and move-and-delete,
shared by the Settings card (api.py), `finance categories` (cli.py) and the finance_* agent tools (tooldefs.py).

User categories are spending-only and live in exactly one of the eight groups db.CATEGORY_GROUPS names (Misc by default).
Names are unique case-insensitively. The 36 seeded categories are built-in (db._v23_schema): a built-in can be given a
new icon or hidden / unhidden, never renamed, deleted or moved between groups. Income and Transfers are structural
outside the taxonomy: they can be re-iconed, never hidden (every income read would break).

Hidden is a selection-surface flag only. NOTHING IN THE MONEY PATH FILTERS ON is_hidden: insights.py, reads.py, budgets,
bills, cash flow, v_spend_lines, report.py and every total keep counting a hidden category exactly as before. Hidden
leaves the new-selection surfaces (the pickers, the filter choices, the AI categorizer's list) and the "Where it went"
donut (kept in settings.chart_excluded_categories, which set_hidden maintains: unhide removes only an exclusion hide
added); past totals remain.

Delete is move-and-delete: every referencing store is reassigned to an explicit, existing, visible spending destination
inside one transaction (an explicit SAVEPOINT), then the source row is removed. The eight stores are db.py's
REFERENCES categories rows: classifications, rules, plaid_category_map, ai_reviews, transaction_splits,
category_rollover (a destination's own row wins), rule_deletions and the JSON arguments of pending_actions. Foreign keys
are off in this app, so none of this relies on them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from . import caticons, insights
from .db import CATEGORY_GROUPS, now

GROUPS = tuple(group for group, _, _ in CATEGORY_GROUPS)          # the eight, in display order (never a second copy)
GROUP_RGB = {group: rgb for group, rgb, _ in CATEGORY_GROUPS}
GROUP_ICON = {group: cats[0][1] for group, _, cats in CATEGORY_GROUPS}   # a new category's icon until one is picked
NAME_MAX = 60
CHART_SETTING = "chart_excluded_categories"
HIDE_ADDED_SETTING = "categories.hide_added_chart"   # the chart exclusions hiding added (unhide removes only these)

COLOURS = caticons.COLOURS                          # the eight swatches the goal picker uses (categories.colour)
UNSET = object()                                    # update(): "colour" not given, as against None (clear it)

CATEGORY_ROWS = ("SELECT c.id, c.name, c.is_income, c.is_transfer, c.monthly_cap, c.icon, c.colour, c.is_builtin, "
                 "c.is_hidden, g.name AS grp, g.rgb FROM categories c LEFT JOIN category_groups g ON g.id = c.group_id")


class CategoryError(RuntimeError):
    """A refused category change; the message is the user-facing sentence (status / field suit api.ApiError)."""

    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.field = field


# --- validation ------------------------------------------------------------------------------------------------------

def _name(value: Any) -> str:
    if not isinstance(value, str):
        raise CategoryError("Give the category a name.", field="name")
    clean = " ".join(value.split())
    if not clean:
        raise CategoryError("Give the category a name.", field="name")
    if len(clean) > NAME_MAX:
        raise CategoryError(f"A category name is up to {NAME_MAX} characters.", field="name")
    if not clean.isprintable():
        raise CategoryError("A category name can't contain control characters.", field="name")
    return clean


def _group(value: Any, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise CategoryError(f"Pick a group: {', '.join(GROUPS)}.", field="group")
    for group in GROUPS:
        if group.casefold() == value.strip().casefold():
            return group
    raise CategoryError(f"Pick a group: {', '.join(GROUPS)}.", field="group")


def _icon(value: Any) -> str:
    if not isinstance(value, str) or value not in caticons.ALL:
        raise CategoryError("Pick an icon from the list.", field="icon")
    return value


def _colour(value: Any) -> str | None:
    """None (follow the group) or one of the eight swatches, matched case-insensitively and kept as the swatch."""
    if value is None:
        return None
    if isinstance(value, str):
        for swatch in COLOURS:
            if swatch == value.casefold():
                return swatch
    raise CategoryError("Pick a colour from the list.", field="colour")


def effective_colour(colour: str | None, rgb: str | None) -> str | None:
    """What the icon is drawn in: the override, else its group's colour as #rrggbb; None for Income and Transfers
    (no group) with no override."""
    if colour:
        return colour
    try:
        r, g, b = (int(part) for part in (rgb or "").split(","))
    except ValueError:
        return None
    return f"#{r:02x}{g:02x}{b:02x}"


def _row(conn: sqlite3.Connection, name: Any) -> sqlite3.Row:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX:
        raise CategoryError("category is required", field="name")
    row = conn.execute(f"{CATEGORY_ROWS} WHERE c.name = ? COLLATE NOCASE", (name.strip(),)).fetchone()
    if row is None:
        raise CategoryError(f"unknown category {name.strip()!r}")
    return row


def _json(r: sqlite3.Row) -> dict[str, Any]:
    return {"name": r["name"], "is_income": bool(r["is_income"]), "is_transfer": bool(r["is_transfer"]),
            "spending": not (r["is_income"] or r["is_transfer"]), "monthly_cap": r["monthly_cap"],
            "group": r["grp"], "icon": r["icon"], "rgb": r["rgb"], "built_in": bool(r["is_builtin"]),
            "hidden": bool(r["is_hidden"]), "colour": r["colour"],
            "effective_colour": effective_colour(r["colour"], r["rgb"])}


def _builtin(row: sqlite3.Row, verb: str) -> None:
    if row["is_builtin"]:
        raise CategoryError(f"{row['name']} is a built-in category: you can change its icon or hide it, not {verb}.")


def _unique(conn: sqlite3.Connection, name: str, exclude_id: int | None = None) -> None:
    """casefold() in Python, not COLLATE NOCASE (ASCII only): École and école are the same name."""
    key = name.casefold()
    for r in conn.execute("SELECT id, name FROM categories"):
        if r["id"] != exclude_id and r["name"].casefold() == key:
            raise CategoryError(f"There's already a category called {name}.", field="name")


# --- writes (the *_locked helpers run inside their caller's transaction) ----------------------------------------------

def _create_locked(conn: sqlite3.Connection, name: Any, group: Any, icon: Any, colour: Any = None) -> sqlite3.Row:
    clean = _name(name)
    group = _group(group, "Misc")
    icon = _icon(icon) if icon is not None else GROUP_ICON[group]
    colour = _colour(colour)
    _unique(conn, clean)
    try:
        cur = conn.execute("INSERT INTO categories (name, is_income, is_transfer, group_id, icon, colour, sort) "
                           "VALUES (?, 0, 0, (SELECT id FROM category_groups WHERE name = ?), ?, ?, NULL)",
                           (clean, group, icon, colour))
    except sqlite3.IntegrityError:
        raise CategoryError(f"There's already a category called {clean}.", field="name") from None
    conn.execute("UPDATE categories SET sort = ? WHERE id = ?", (1000 + cur.lastrowid, cur.lastrowid))
    return conn.execute(f"{CATEGORY_ROWS} WHERE c.id = ?", (cur.lastrowid,)).fetchone()


def _rename_locked(conn: sqlite3.Connection, row: sqlite3.Row, new_name: Any) -> sqlite3.Row:
    _builtin(row, "rename it")
    clean = _name(new_name)
    if clean == row["name"]:
        return row
    _unique(conn, clean, row["id"])
    try:
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (clean, row["id"]))
    except sqlite3.IntegrityError:
        raise CategoryError(f"There's already a category called {clean}.", field="name") from None
    _rename_name_refs(conn, row["name"], clean)
    return conn.execute(f"{CATEGORY_ROWS} WHERE c.id = ?", (row["id"],)).fetchone()


def _icon_locked(conn: sqlite3.Connection, row: sqlite3.Row, icon: Any) -> sqlite3.Row:
    conn.execute("UPDATE categories SET icon = ? WHERE id = ?", (_icon(icon), row["id"]))
    return conn.execute(f"{CATEGORY_ROWS} WHERE c.id = ?", (row["id"],)).fetchone()


def _colour_locked(conn: sqlite3.Connection, row: sqlite3.Row, colour: Any) -> sqlite3.Row:
    conn.execute("UPDATE categories SET colour = ? WHERE id = ?", (_colour(colour), row["id"]))
    return conn.execute(f"{CATEGORY_ROWS} WHERE c.id = ?", (row["id"],)).fetchone()


def _hide_locked(conn: sqlite3.Connection, row: sqlite3.Row, hidden: Any) -> sqlite3.Row:
    if not isinstance(hidden, bool):
        raise CategoryError("hidden must be true or false", field="hidden")
    if row["is_income"] or row["is_transfer"]:
        raise CategoryError(f"{row['name']} is an income / transfer category: hiding it would break every income and "
                            f"transfer read, so it always stays visible (its icon can still change).")
    if bool(row["is_hidden"]) != hidden:
        conn.execute("UPDATE categories SET is_hidden = ? WHERE id = ?", (1 if hidden else 0, row["id"]))
        _chart_set_hidden(conn, row["name"], hidden)
    return conn.execute(f"{CATEGORY_ROWS} WHERE c.id = ?", (row["id"],)).fetchone()


def _move_locked(conn: sqlite3.Connection, row: sqlite3.Row, group: Any) -> sqlite3.Row:
    _builtin(row, "move it between groups")
    target = _group(group)
    conn.execute("UPDATE categories SET group_id = (SELECT id FROM category_groups WHERE name = ?) WHERE id = ?",
                 (target, row["id"]))
    return conn.execute(f"{CATEGORY_ROWS} WHERE c.id = ?", (row["id"],)).fetchone()


def create(conn: sqlite3.Connection, name: Any, group: Any = None, icon: Any = None,
           colour: Any = None) -> dict[str, Any]:
    """A new spending category (is_income / is_transfer 0), in `group` (Misc by default) and with `icon` (its group's
    fallback by default); sort 1000 + id, after the taxonomy, as the picker's hand-made ones always were. `colour`: one
    of the eight swatches, or None to follow the group."""
    with conn:
        return _json(_create_locked(conn, name, group, icon, colour))


def rename(conn: sqlite3.Connection, name: Any, new_name: Any) -> dict[str, Any]:
    """A user category's new name. The same transaction updates the two name-keyed stores (saved_filters.filters and
    settings.chart_excluded_categories) and any pending action that names it."""
    with conn:
        return _json(_rename_locked(conn, _row(conn, name), new_name))


def set_icon(conn: sqlite3.Connection, name: Any, icon: Any) -> dict[str, Any]:
    """A new icon for any category, built-in or user (Income and Transfers included)."""
    with conn:
        return _json(_icon_locked(conn, _row(conn, name), icon))


def set_colour(conn: sqlite3.Connection, name: Any, colour: Any) -> dict[str, Any]:
    """A swatch for any category's icon (built-in or user), or None to follow its group again."""
    with conn:
        return _json(_colour_locked(conn, _row(conn, name), colour))


def set_hidden(conn: sqlite3.Connection, name: Any, hidden: bool) -> dict[str, Any]:
    """Hide / unhide any spending category, built-in or user. Keeps settings.chart_excluded_categories in step (the one
    place the donut reads), so the chart drops it while every total keeps it. Unhide takes it off the chart's list only
    when hide put it there: an exclusion the user set before hiding stays."""
    with conn:
        return _json(_hide_locked(conn, _row(conn, name), hidden))


def move(conn: sqlite3.Connection, name: Any, group: Any) -> dict[str, Any]:
    """Move a user category to another one of the eight groups. A built-in refuses: its group is the taxonomy's."""
    with conn:
        return _json(_move_locked(conn, _row(conn, name), group))


def update(conn: sqlite3.Connection, name: Any, rename_to: Any = None, icon: Any = None, hidden: Any = None,
           group: Any = None, colour: Any = UNSET) -> dict[str, Any]:
    """Only what is given changes, in one transaction (rename first, so the other steps use the new name). `colour`
    is the one field where None is a value: it clears the override (the icon follows its group)."""
    with conn:
        row = _row(conn, name)
        if rename_to is not None:
            row = _rename_locked(conn, row, rename_to)
        if icon is not None:
            row = _icon_locked(conn, row, icon)
        if colour is not UNSET:
            row = _colour_locked(conn, row, colour)
        if hidden is not None:
            row = _hide_locked(conn, row, hidden)
        if group is not None:
            row = _move_locked(conn, row, group)
        return _json(conn.execute(f"{CATEGORY_ROWS} WHERE c.id = ?", (row["id"],)).fetchone())


# --- delete: move every reference, then the source row ----------------------------------------------------------------

STORE_WORDS = {
    "classifications": "transaction",
    "rules": "rule",
    "plaid_category_map": "bank category mapping",
    "ai_reviews": "AI review",
    "transaction_splits": "split line",
    "category_rollover": "budget rollover",
    "rule_deletions": "deleted-rule receipt",
    "pending_actions": "pending action",
}


def _destination(conn: sqlite3.Connection, src: sqlite3.Row, move_to: Any) -> sqlite3.Row:
    if not isinstance(move_to, str) or not move_to.strip():
        raise CategoryError("Choose where its transactions go (move_to).", field="move_to")
    dest = conn.execute(f"{CATEGORY_ROWS} WHERE c.name = ? COLLATE NOCASE", (move_to.strip(),)).fetchone()
    if dest is None:
        raise CategoryError(f"Where its transactions go must be an existing category; {move_to.strip()!r} isn't one.",
                            field="move_to")
    if dest["name"].casefold() == src["name"].casefold():
        raise CategoryError("Choose a different category: where its transactions go can't be the one being deleted.",
                            field="move_to")
    if dest["is_income"] or dest["is_transfer"]:
        raise CategoryError(f"{dest['name']} is an income / transfer category, not a spending one; choose a spending "
                            f"category.", field="move_to")
    if dest["is_hidden"]:
        raise CategoryError(f"{dest['name']} is hidden; unhide it first, or choose a visible category: money must not "
                            f"move where nobody can see it.", field="move_to")
    return dest


def _pending_rows(conn: sqlite3.Connection, old_name: str) -> list[tuple[int, dict[str, Any]]]:
    """(id, arguments) of every undecided pending action (pending, or approved and being applied) whose JSON `category`
    names old_name (case-insensitively). A decided row (applied, rejected, failed) is history: it keeps its arguments."""
    out = []
    for row in conn.execute("SELECT id, arguments FROM pending_actions WHERE status IN ('pending', 'approved') "
                            "ORDER BY id"):
        try:
            args = json.loads(row["arguments"])
        except (TypeError, ValueError):
            continue
        if isinstance(args, dict) and isinstance(args.get("category"), str) and \
                args["category"].strip().casefold() == old_name.strip().casefold():
            out.append((row["id"], args))
    return out


def _move_pending_actions(conn: sqlite3.Connection, old_name: str, new_name: str) -> int:
    moved = 0
    for action_id, args in _pending_rows(conn, old_name):
        args = {**args, "category": new_name}
        conn.execute("UPDATE pending_actions SET arguments = ? WHERE id = ?", (json.dumps(args), action_id))
        moved += 1
    return moved


def _counts(conn: sqlite3.Connection, src: sqlite3.Row, dest: sqlite3.Row) -> dict[str, int]:
    src_id = src["id"]
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE category_id = ?", (src_id,)).fetchone()[0]
              for t in STORE_WORDS if t != "pending_actions"}
    if conn.execute("SELECT 1 FROM category_rollover WHERE category_id = ?", (dest["id"],)).fetchone():
        counts["category_rollover"] = 0   # the destination's own row wins: the source's is dropped, nothing moves
    counts["pending_actions"] = len(_pending_rows(conn, src["name"]))
    return counts


def delete_preview(conn: sqlite3.Connection, name: Any, move_to: Any) -> tuple[dict[str, Any], dict[str, int]]:
    """(the destination category, how many rows of each store would move there): the tool's plain-words summary, with
    the same validation delete applies and nothing written."""
    src = _row(conn, name)
    _builtin(src, "delete it")
    dest = _destination(conn, src, move_to)
    return _json(dest), _counts(conn, src, dest)


def _delete_locked(conn: sqlite3.Connection, src: sqlite3.Row, dest: sqlite3.Row) -> dict[str, int]:
    src_id, dest_id, ts = src["id"], dest["id"], now()
    moved: dict[str, int] = {}
    conn.execute("SAVEPOINT catops_delete")
    try:
        moved["classifications"] = conn.execute(
            "UPDATE classifications SET category_id = ?, updated_at = ? WHERE category_id = ?",
            (dest_id, ts, src_id)).rowcount
        moved["rules"] = conn.execute("UPDATE rules SET category_id = ? WHERE category_id = ?",
                                      (dest_id, src_id)).rowcount
        moved["plaid_category_map"] = conn.execute(
            "UPDATE plaid_category_map SET category_id = ? WHERE category_id = ?", (dest_id, src_id)).rowcount
        moved["ai_reviews"] = conn.execute("UPDATE ai_reviews SET category_id = ? WHERE category_id = ?",
                                           (dest_id, src_id)).rowcount
        moved["transaction_splits"] = conn.execute(
            "UPDATE transaction_splits SET category_id = ? WHERE category_id = ?", (dest_id, src_id)).rowcount
        if conn.execute("SELECT 1 FROM category_rollover WHERE category_id = ?", (dest_id,)).fetchone():
            conn.execute("DELETE FROM category_rollover WHERE category_id = ?", (src_id,))   # the destination's row wins
            moved["category_rollover"] = 0                                                   # dropped, not moved
        else:
            moved["category_rollover"] = conn.execute(
                "UPDATE category_rollover SET category_id = ? WHERE category_id = ?", (dest_id, src_id)).rowcount
        moved["rule_deletions"] = conn.execute(
            "UPDATE rule_deletions SET category_id = ? WHERE category_id = ?", (dest_id, src_id)).rowcount
        moved["pending_actions"] = _move_pending_actions(conn, src["name"], dest["name"])
        _swap_saved_filters(conn, src["name"], dest["name"])
        _chart_forget(conn, src["name"])
        conn.execute("DELETE FROM categories WHERE id = ?", (src_id,))
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT catops_delete")
        conn.execute("RELEASE SAVEPOINT catops_delete")
        raise
    conn.execute("RELEASE SAVEPOINT catops_delete")
    return moved


def delete(conn: sqlite3.Connection, name: Any, move_to: Any) -> dict[str, Any]:
    """User categories only: reassign all eight stores to `move_to` in one transaction, then delete the source row.
    The two name-keyed stores follow in the same transaction: a saved filter naming the source names the destination,
    and the source leaves the chart's hidden list (the destination is never added: deleting a hidden category doesn't
    hide where its money went). Returns the destination category, the deleted name and how many rows moved from each
    of the eight stores."""
    with conn:
        src = _row(conn, name)
        _builtin(src, "delete it")
        dest = _destination(conn, src, move_to)
        moved = _delete_locked(conn, src, dest)
        dest_row = conn.execute(f"{CATEGORY_ROWS} WHERE c.id = ?", (dest["id"],)).fetchone()
        return {**_json(dest_row), "deleted": src["name"], "moved": moved}


# --- name-keyed stores ------------------------------------------------------------------------------------------------

def _chart_names(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (CHART_SETTING,)).fetchone()
    try:
        value = json.loads(row["value"]) if row else []
    except ValueError:
        return []
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _save_chart_names(conn: sqlite3.Connection, names: list[str]) -> None:
    out: list[str] = []
    for name in names:
        if name not in out:
            out.append(name)
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (CHART_SETTING, json.dumps(out), now()))


def _hide_added(conn: sqlite3.Connection) -> list[str]:
    """The names hiding put on the chart's list (settings HIDE_ADDED_SETTING): unhide takes off only these, so an
    exclusion the user had set before hiding stays."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (HIDE_ADDED_SETTING,)).fetchone()
    try:
        value = json.loads(row["value"]) if row else []
    except ValueError:
        return []
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _save_hide_added(conn: sqlite3.Connection, names: list[str]) -> None:
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (HIDE_ADDED_SETTING, json.dumps(names), now()))


def _chart_set_hidden(conn: sqlite3.Connection, name: str, hidden: bool) -> None:
    key = name.casefold()
    names, added = _chart_names(conn), _hide_added(conn)
    was_added = any(x.casefold() == key for x in added)
    others = [x for x in added if x.casefold() != key]
    if hidden and not any(x.casefold() == key for x in names):
        _save_chart_names(conn, names + [name])
        _save_hide_added(conn, others + [name])
    elif not hidden and was_added:
        _save_chart_names(conn, [x for x in names if x.casefold() != key])
        _save_hide_added(conn, others)


def _chart_forget(conn: sqlite3.Connection, name: str) -> None:
    """A deleted category leaves the chart's list and the hide record (nothing is added in its place)."""
    key = name.casefold()
    names, added = _chart_names(conn), _hide_added(conn)
    if any(x.casefold() == key for x in names):
        _save_chart_names(conn, [x for x in names if x.casefold() != key])
    if any(x.casefold() == key for x in added):
        _save_hide_added(conn, [x for x in added if x.casefold() != key])


def _swap_saved_filters(conn: sqlite3.Connection, old: str, new: str) -> None:
    """saved_filters.filters.category (one name or a list) names `new` where it named `old`, case-insensitively, order
    kept, once each."""
    old_key = old.casefold()

    def swap(value: Any) -> Any:
        if isinstance(value, str):
            return new if value.casefold() == old_key else value
        if isinstance(value, list):
            out: list[Any] = []
            for v in (swap(v) for v in value):
                if not (isinstance(v, str) and any(isinstance(o, str) and o.casefold() == v.casefold() for o in out)):
                    out.append(v)
            return out
        return value

    for row in conn.execute("SELECT id, filters FROM saved_filters ORDER BY id").fetchall():
        try:
            filters = json.loads(row["filters"])
        except (TypeError, ValueError):
            continue
        if not isinstance(filters, dict) or "category" not in filters:
            continue
        changed = swap(filters["category"])
        if changed != filters["category"]:
            conn.execute("UPDATE saved_filters SET filters = ? WHERE id = ?",
                         (json.dumps({**filters, "category": changed}), row["id"]))


def _rename_name_refs(conn: sqlite3.Connection, old: str, new: str) -> None:
    """saved_filters.filters.category, the chart's hidden list (and the hide record), case-insensitively, order kept."""
    old_key = old.casefold()
    _swap_saved_filters(conn, old, new)
    for names, save in ((_chart_names(conn), _save_chart_names), (_hide_added(conn), _save_hide_added)):
        swapped = [new if x.casefold() == old_key else x for x in names]
        if swapped != names:
            save(conn, swapped)
    _move_pending_actions(conn, old, new)


# --- the Settings card's read -----------------------------------------------------------------------------------------

def payload(conn: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    """Groups (display order) with their categories, Income & Transfers last, each category with its month spend and
    its transaction count (the transactions filed under it: what a delete would move); the flat list; the hidden names;
    and the counts. Every category is here (hidden ones flagged): the card filters. The spend is the Budgets page's
    number (insights.spend_by: a split's lines, the display currency only), keyed by category id."""
    month = insights.month_of(today or date.today())
    spend = {cid: s for cid, (s, _n) in insights.spend_by(conn, "v.month = ?", (month,), "v.category_id").items()}
    filed = {r[0]: r[1] for r in conn.execute(
        "SELECT category_id, COUNT(*) FROM classifications GROUP BY category_id")}
    groups = {name: {"name": name, "rgb": rgb, "categories": []} for name, rgb, _ in CATEGORY_GROUPS}
    ungrouped = []
    flat = []
    for r in conn.execute(f"{CATEGORY_ROWS} ORDER BY c.sort, c.name"):
        item = _json(r)
        item["spend"] = round(float(spend.get(r["id"], 0.0)), 2)
        item["transactions"] = filed.get(r["id"], 0)
        flat.append(item)
        (groups[item["group"]]["categories"] if item["group"] in groups else ungrouped).append(item)
    hidden = [c["name"] for c in flat if c["hidden"]]
    return {"month": month,
            "colours": list(COLOURS),
            "groups": [{"name": g["name"], "rgb": g["rgb"], "categories": g["categories"]} for g in groups.values()]
                      + ([{"name": "Income & transfers", "rgb": None, "categories": ungrouped}] if ungrouped else []),
            "ungrouped": ungrouped,
            "categories": flat,
            "hidden": hidden,
            "counts": {"total": len(flat), "builtin": sum(1 for c in flat if c["built_in"]),
                       "user": sum(1 for c in flat if not c["built_in"]), "hidden": len(hidden),
                       "spending": sum(1 for c in flat if c["spending"])}}


def text(p: dict[str, Any]) -> str:
    """`finance categories`' plain lines: group, name, icon, tags and this month's spend."""
    out = []
    for group in p["groups"]:
        for c in group["categories"]:
            tags = ["built-in" if c["built_in"] else "yours"] + (["hidden"] if c["hidden"] else [])
            cap = f" cap {c['monthly_cap']:.2f}" if c["monthly_cap"] is not None else ""
            out.append(f"{group['name']:<18} {c['name']:<24} {c['icon'] or '-':<20} "
                       f"{' '.join(tags):<18} spend {c['spend']:.2f}{cap}")
    return "\n".join(out)
