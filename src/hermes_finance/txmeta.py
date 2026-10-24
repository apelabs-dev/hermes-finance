"""Splits, notes, tags and saved filters on transactions (TASK-19).

Split: a purchase in two or more lines, each a spending category and an amount; the lines add up to the transaction's
amount to the cent (all the same sign as it). Spending by category, budgets, rings, the chart, cash flow, alerts and the
read commands use the lines (v_spend_lines) in place of the transaction's category, so a split never changes a total.
Saving one makes the transaction the user's decision (classifications.source = 'user', its category kept): rules and
the daily AI run never touch it, and set-category refuses it until it's unsplit. Unsplitting removes the lines.

Notes are free text; tags are made the first time they're used. `finance txns --search` and finance_transactions match
both (SEARCH_SQL); the Transactions page filters by one tag (TAG_SQL), and since polish P1 by any of several
(any_tag_sql). A saved filter is a named set of the page's filters (search, categories, groups, Uncategorized, bank,
kind, tags), shown as a chip with how many transactions match in the month shown.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .db import now
from .money import money

MAX_LINES = 20
MAX_NOTE = 2000
MAX_TAG = 40
MAX_TAGS = 20
MAX_FILTER_NAME = 60
MAX_FILTERS = 50
FILTER_KEYS = ("q", "category", "group", "uncategorized", "bank", "kind", "tag")
LIST_KEYS = ("category", "group", "tag")   # polish P1: one name, or a list of them (any matches)
MAX_PICKS = 60
KIND_VALUES = ("all", "review", "spend", "income", "transfers")
TAG_RE = re.compile(r"[^\s#,][^,#]{0,39}")

# SQL over `v_transactions v` (a column-like expression each, for LIKE): the note, and the tags joined by spaces
NOTE_SQL = "(SELECT n.note FROM transaction_notes n WHERE n.transaction_id = v.transaction_id)"
TAGS_SQL = ("(SELECT group_concat(tg.name, ' ') FROM transaction_tags tt JOIN tags tg ON tg.id = tt.tag_id "
            "WHERE tt.transaction_id = v.transaction_id)")
SEARCH_SQL = (NOTE_SQL, TAGS_SQL)
# one tag, by name (case-insensitive)
TAG_SQL = ("EXISTS (SELECT 1 FROM transaction_tags tt JOIN tags tg ON tg.id = tt.tag_id "
           "WHERE tt.transaction_id = v.transaction_id AND tg.name = ? COLLATE NOCASE)")


def any_tag_sql(n: int) -> str:
    """polish P1: any of `n` tags, by name (case-insensitive); n parameters."""
    return ("EXISTS (SELECT 1 FROM transaction_tags tt JOIN tags tg ON tg.id = tt.tag_id "
            f"WHERE tt.transaction_id = v.transaction_id AND tg.name COLLATE NOCASE IN ({', '.join('?' * n)}))")


# a category filter over transactions: a split one matches by its lines (two parameters: the name twice)
CATEGORY_SQL = ("((v.category = ? AND NOT EXISTS (SELECT 1 FROM transaction_splits s WHERE s.transaction_id = v.transaction_id)) "
                "OR EXISTS (SELECT 1 FROM transaction_splits s JOIN categories sc ON sc.id = s.category_id "
                "WHERE s.transaction_id = v.transaction_id AND sc.name = ?))")


class TxMetaError(Exception):
    def __init__(self, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.field = field


def _cents(x: float) -> int:
    return round(x * 100)


def is_split(conn: sqlite3.Connection, transaction_id: str) -> bool:
    return conn.execute("SELECT 1 FROM transaction_splits WHERE transaction_id = ? LIMIT 1", (transaction_id,)).fetchone() is not None


def _transaction(conn: sqlite3.Connection, transaction_id: Any) -> sqlite3.Row:
    if not isinstance(transaction_id, str) or not transaction_id.strip():
        raise TxMetaError("transaction_id is required", field="transaction_id")
    row = conn.execute("SELECT transaction_id, amount, pending, category, category_id, is_income, is_transfer, merchant_name, "
                       "name FROM v_transactions WHERE transaction_id = ?", (transaction_id,)).fetchone()
    if row is None:
        raise TxMetaError(f"unknown transaction {transaction_id[:80]!r}", 404, "transaction_id")
    return row


def _spending_category(conn: sqlite3.Connection, name: Any, i: int) -> sqlite3.Row:
    if not isinstance(name, str) or not name.strip():
        raise TxMetaError(f"line {i}: a category is required", field="lines")
    row = conn.execute("SELECT id, name, is_income, is_transfer FROM categories WHERE name = ? COLLATE NOCASE",
                       (name.strip(),)).fetchone()
    if row is None:
        raise TxMetaError(f"line {i}: unknown category {name[:80]!r}", field="lines")
    if row["is_income"] or row["is_transfer"]:
        raise TxMetaError(f"line {i}: {row['name']} is not a spending category (a split only moves spending between "
                          "categories, so no total changes)", field="lines")
    return row


def split(conn: sqlite3.Connection, transaction_id: Any, lines: Any) -> dict[str, Any]:
    """Save a split (replacing any before), or remove it with an empty list / null. Returns lines()."""
    t = _transaction(conn, transaction_id)
    tid = t["transaction_id"]
    if lines is None or lines == []:
        with conn:
            conn.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (tid,))
        return {"transaction_id": tid, "split": None, "category": t["category"]}
    if not isinstance(lines, list):
        raise TxMetaError("lines must be a list of {category, amount}", field="lines")
    if t["pending"]:
        raise TxMetaError("this purchase is still pending: split it once it posts (a pending charge can change)", field="lines")
    if t["is_income"] or t["is_transfer"]:
        raise TxMetaError(f"only spending can be split (this is under {t['category']})", field="lines")
    if not 2 <= len(lines) <= MAX_LINES:
        raise TxMetaError(f"a split needs 2 to {MAX_LINES} lines", field="lines")
    total = _cents(t["amount"])
    if total == 0:
        raise TxMetaError(f"a {money(0)} transaction can't be split", field="lines")
    seen, rows = set(), []
    for i, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            raise TxMetaError(f"line {i} must be an object {{category, amount}}", field="lines")
        cat = _spending_category(conn, line.get("category"), i)
        if cat["id"] in seen:
            raise TxMetaError(f"line {i}: {cat['name']} is on another line already", field="lines")
        seen.add(cat["id"])
        amount = line.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount != amount or abs(amount) > 1e9:
            raise TxMetaError(f"line {i}: the amount must be a number", field="lines")
        c = _cents(amount)
        if c == 0 or (c > 0) != (total > 0):
            raise TxMetaError(f"line {i}: each line must be {'more than $0' if total > 0 else 'under $0 (a refund)'}",
                              field="lines")
        rows.append((i, cat["id"], c / 100))
    got = sum(_cents(a) for _, _, a in rows)
    if got != total:
        diff = (total - got) / 100
        left = f"{money(diff)} left to split" if diff > 0 else f"{money(-diff)} too much"
        raise TxMetaError(f"the lines add up to {money(got / 100)}, not {money(total / 100)}: {left}", field="lines")
    ts = now()
    with conn:
        conn.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (tid,))
        conn.executemany("INSERT INTO transaction_splits (transaction_id, line, category_id, amount, created_at) "
                         "VALUES (?,?,?,?,?)", [(tid, i, cid, amt, ts) for i, cid, amt in rows])
        # the transaction becomes the user's decision (its category kept): no rule or AI run touches it again
        conn.execute("""INSERT INTO classifications (transaction_id, category_id, source, confidence, note, rule_id, updated_at)
                        VALUES (?, ?, 'user', NULL, NULL, NULL, ?)
                        ON CONFLICT(transaction_id) DO UPDATE SET category_id = excluded.category_id, source = 'user',
                            confidence = NULL, rule_id = NULL, updated_at = excluded.updated_at""",
                     (tid, t["category_id"], ts))
    return {"transaction_id": tid, "split": split_lines(conn, [tid]).get(tid), "category": t["category"]}


def split_lines(conn: sqlite3.Connection, tids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for chunk in _chunks(tids):
        for r in conn.execute(f"SELECT s.transaction_id, s.line, c.name, s.amount FROM transaction_splits s "
                              f"JOIN categories c ON c.id = s.category_id WHERE s.transaction_id IN "
                              f"({', '.join('?' * len(chunk))}) ORDER BY s.transaction_id, s.line", chunk):
            out.setdefault(r[0], []).append({"category": r[2], "amount": r[3]})
    return out


def _chunks(xs: list[str], n: int = 400):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def decorate(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add split (lines or null), note and tags to transaction rows (the Transactions page's, `finance txns`')."""
    tids = [r["transaction_id"] for r in rows]
    splits = split_lines(conn, tids)
    notes: dict[str, str] = {}
    tags: dict[str, list[str]] = {}
    for chunk in _chunks(tids):
        marks = ", ".join("?" * len(chunk))
        notes.update(conn.execute(f"SELECT transaction_id, note FROM transaction_notes WHERE transaction_id IN ({marks})", chunk))
        for tid, name in conn.execute(f"SELECT tt.transaction_id, tg.name FROM transaction_tags tt JOIN tags tg ON tg.id = tt.tag_id "
                                      f"WHERE tt.transaction_id IN ({marks}) ORDER BY tg.name COLLATE NOCASE", chunk):
            tags.setdefault(tid, []).append(name)
    for r in rows:
        r["split"] = splits.get(r["transaction_id"])
        r["note"] = notes.get(r["transaction_id"])
        r["tags"] = tags.get(r["transaction_id"], [])
    return rows


def clean_tag(value: Any) -> str:
    if not isinstance(value, str):
        raise TxMetaError("a tag must be text", field="tags")
    tag = " ".join(value.strip().lstrip("#").split())
    if not tag:
        raise TxMetaError("a tag can't be empty", field="tags")
    if len(tag) > MAX_TAG or not TAG_RE.fullmatch(tag):
        raise TxMetaError(f"a tag is up to {MAX_TAG} characters, without commas or #", field="tags")
    return tag


def set_meta(conn: sqlite3.Connection, transaction_id: Any, data: dict[str, Any]) -> dict[str, Any]:
    """{note: text | "" | null} and / or {tags: [name, ...]} (the whole set; new names are made)."""
    t = _transaction(conn, transaction_id)
    tid = t["transaction_id"]
    if "note" not in data and "tags" not in data:
        raise TxMetaError("note or tags is required", field="note")
    note = data.get("note")
    if "note" in data and note is not None:
        if not isinstance(note, str):
            raise TxMetaError("note must be text", field="note")
        note = note.strip()
        if len(note) > MAX_NOTE:
            raise TxMetaError(f"a note is up to {MAX_NOTE} characters", field="note")
    tags = None
    if "tags" in data:
        raw = data["tags"]
        if not isinstance(raw, list):
            raise TxMetaError("tags must be a list of names", field="tags")
        tags = []
        for x in raw:
            tag = clean_tag(x)
            if tag.casefold() not in {y.casefold() for y in tags}:
                tags.append(tag)
        if len(tags) > MAX_TAGS:
            raise TxMetaError(f"at most {MAX_TAGS} tags on one transaction", field="tags")
    ts = now()
    with conn:
        if "note" in data:
            if note:
                conn.execute("INSERT INTO transaction_notes (transaction_id, note, updated_at) VALUES (?,?,?) "
                             "ON CONFLICT(transaction_id) DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at",
                             (tid, note, ts))
            else:
                conn.execute("DELETE FROM transaction_notes WHERE transaction_id = ?", (tid,))
        if tags is not None:
            conn.execute("DELETE FROM transaction_tags WHERE transaction_id = ?", (tid,))
            for tag in tags:
                conn.execute("INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)", (tag, ts))
                tag_id = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag,)).fetchone()[0]
                conn.execute("INSERT OR IGNORE INTO transaction_tags (transaction_id, tag_id) VALUES (?, ?)", (tid, tag_id))
    row = decorate(conn, [{"transaction_id": tid}])[0]
    return {"transaction_id": tid, "note": row["note"], "tags": row["tags"]}


def all_tags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every tag in use, most used first."""
    return [{"name": r[0], "count": r[1]} for r in conn.execute(
        "SELECT tg.name, COUNT(t.transaction_id) AS n FROM tags tg JOIN transaction_tags tt ON tt.tag_id = tg.id "
        "JOIN transactions t ON t.transaction_id = tt.transaction_id AND t.removed = 0 "
        "GROUP BY tg.id ORDER BY n DESC, tg.name COLLATE NOCASE")]


# --- saved filters ---------------------------------------------------------------------------------------------------

def clean_filters(conn: sqlite3.Connection, value: Any) -> dict[str, str | list[str]]:
    """{q, category, group, uncategorized, bank, kind, tag}: each text; category, group and tag may be lists (polish
    P1: any of them), kept as one name when there's one; uncategorized is "1" or left out."""
    if not isinstance(value, dict):
        raise TxMetaError("filters must be an object {q, category, group, uncategorized, bank, kind, tag}", field="filters")
    unknown = sorted(set(value) - set(FILTER_KEYS))
    if unknown:
        raise TxMetaError(f"unknown filter {str(unknown[0])[:40]!r}", field="filters")
    out: dict[str, str | list[str]] = {}
    for k in FILTER_KEYS:
        raw = value.get(k)
        if raw in (None, "", []):
            continue
        if isinstance(raw, list) and (k not in LIST_KEYS or len(raw) > MAX_PICKS):
            raise TxMetaError(f"{k} takes at most {MAX_PICKS} names" if k in LIST_KEYS else f"{k} must be text", field="filters")
        if k == "uncategorized" and raw in (True, "1", "true"):
            out[k] = "1"
            continue
        elif k == "uncategorized" and raw in (False, "0", "false"):
            continue
        raw = raw if isinstance(raw, list) else [raw]
        vals: list[str] = []
        for v in raw:
            if not isinstance(v, str) or len(v) > 200:
                raise TxMetaError(f"{k} must be text (up to 200 characters)", field="filters")
            v = v.strip()
            if k == "kind":
                if v not in KIND_VALUES:
                    raise TxMetaError(f"kind must be one of {', '.join(KIND_VALUES)}", field="filters")
                if v == "all":
                    continue
            if k == "uncategorized":
                raise TxMetaError("uncategorized must be 1 or left out", field="filters")
            if k == "category" and not conn.execute("SELECT 1 FROM categories WHERE name = ?", (v,)).fetchone():
                raise TxMetaError(f"unknown category {v[:80]!r}", field="filters")
            if k == "group" and not conn.execute("SELECT 1 FROM category_groups WHERE name = ?", (v,)).fetchone():
                raise TxMetaError(f"unknown category group {v[:80]!r}", field="filters")
            if k == "tag":
                v = clean_tag(v)
            if v and v.casefold() not in {x.casefold() for x in vals}:
                vals.append(v)
        if vals:
            out[k] = vals if len(vals) > 1 else vals[0]
    if not out:
        raise TxMetaError("there's nothing to save: search, pick a category, a bank, a kind or a tag first", field="filters")
    return out


def saved_filters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT id, name, filters, created_at FROM saved_filters ORDER BY id"):
        try:
            f = json.loads(r["filters"])
        except ValueError:
            f = {}
        out.append({"id": r["id"], "name": r["name"], "filters": f if isinstance(f, dict) else {}, "created_at": r["created_at"]})
    return out


def save_filter(conn: sqlite3.Connection, name: Any, filters: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not " ".join(name.split()):
        raise TxMetaError("give the filter a name", field="name")
    name = " ".join(name.split())
    if len(name) > MAX_FILTER_NAME:
        raise TxMetaError(f"a name is up to {MAX_FILTER_NAME} characters", field="name")
    f = clean_filters(conn, filters)
    if conn.execute("SELECT COUNT(*) FROM saved_filters").fetchone()[0] >= MAX_FILTERS:
        raise TxMetaError(f"at most {MAX_FILTERS} saved filters: delete one first", field="name")
    if conn.execute("SELECT 1 FROM saved_filters WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        raise TxMetaError(f"there's a saved filter called {name!r} already", field="name")
    with conn:
        cur = conn.execute("INSERT INTO saved_filters (name, filters, created_at) VALUES (?,?,?)", (name, json.dumps(f), now()))
    return {"id": cur.lastrowid, "name": name, "filters": f}


def delete_filter(conn: sqlite3.Connection, filter_id: Any) -> dict[str, Any]:
    if isinstance(filter_id, bool) or not isinstance(filter_id, int):
        raise TxMetaError("id must be a saved filter's id", field="id")
    with conn:
        n = conn.execute("DELETE FROM saved_filters WHERE id = ?", (filter_id,)).rowcount
    if not n:
        raise TxMetaError("no such saved filter", 404, "id")
    return {"id": filter_id, "deleted": True}
