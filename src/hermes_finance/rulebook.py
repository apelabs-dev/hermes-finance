"""The rules manager (TASK-24): Settings -> Categorization rules, `finance rules list|delete|suggestions` and the
finance_rules tool. Reads work on a read-only connection; the writes go through classify.py (edit_rule, keep_rule,
delete_rule, restore_rule, rule_from_transaction), the same paths as every other rule save.

Suggestions: a merchant (the exact rule key a transaction would get, classify.rule_key) the user filed by hand
(category_source 'user') 2+ times under the same category, where no rule already does that: no user rule matches those
rows, and the rule that wins them (if any) files them elsewhere. An AI guess the user overrode counts the same way (the
row is 'user' now), and says so. "Dismiss" is remembered in the settings table (key DISMISSED, a JSON list of keys), like
goals' "Not a goal": no schema change. A suggestion disappears once a rule covers it.

Every write here is one transaction, reads included (classify.atomic): a failure half way leaves no rule and no re-filed
row behind, from the web routes and from a direct caller alike. Accepting a suggestion writes only its merchant's rule and
re-files only the rows that rule matches (no classify-apply over everything). Undo takes only the delete's receipt
({"receipt": str}, the server-held record in rule_deletions): nothing else a caller sends is used.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import classify
from .db import now

DISMISSED = "rules_dismissed_suggestions"   # settings key: suggestion keys answered "Dismiss"
MIN_TIMES = 2
FIELD_WORD = {"merchant_name": "Merchant", "name": "Name"}


class RuleError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def label(field: str, match_type: str, pattern: str) -> str:
    return f"{FIELD_WORD.get(field, field)} {'is' if match_type == 'exact' else 'contains'} {pattern}"


def _txns(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT t.transaction_id, t.name, t.merchant_name, c.source, c.category_id, c.rule_id
                           FROM transactions t LEFT JOIN classifications c USING (transaction_id)
                           WHERE t.removed = 0""").fetchall()


def _rule_json(r: sqlite3.Row, matches: int) -> dict[str, Any]:
    return {"id": r["id"], "field": r["match_field"], "type": r["match_type"], "pattern": r["pattern"],
            "label": label(r["match_field"], r["match_type"], r["pattern"]), "category": r["category"],
            "source": "user" if r["created_from"] == "user" else "ai", "matches": matches, "created_at": r["created_at"]}


RULES_SQL = """SELECT r.*, c.name AS category FROM rules r JOIN categories c ON c.id = r.category_id"""


def _compiled(r: sqlite3.Row) -> classify.Rule:
    import re
    regex = None
    if r["match_type"] == "regex":
        try:
            regex = re.compile(r["pattern"], re.IGNORECASE)
        except re.error:
            regex = None
    return classify.Rule(r["id"], r["match_field"], r["match_type"], r["pattern"], r["category_id"], regex)


def rules(conn: sqlite3.Connection, txns: list[sqlite3.Row] | None = None) -> list[dict[str, Any]]:
    """Every rule, newest first, with how many live transactions it matches now."""
    txns = _txns(conn) if txns is None else txns
    out = []
    for r in conn.execute(RULES_SQL + " ORDER BY r.created_at DESC, r.id DESC"):
        rule = _compiled(r)
        out.append(_rule_json(r, sum(1 for t in txns if rule.matches(t))))
    return out


def rule(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any]:
    r = conn.execute(RULES_SQL + " WHERE r.id = ?", (rule_id,)).fetchone()
    if r is None:
        raise RuleError(f"unknown rule {rule_id!r}", 404)
    compiled = _compiled(r)
    return _rule_json(r, sum(1 for t in _txns(conn) if compiled.matches(t)))


def dismissed(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (DISMISSED,)).fetchone()
    try:
        keys = json.loads(row["value"]) if row else []
    except ValueError:
        return []
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def _key(field: str, pattern: str, category_id: int) -> str:
    return f"{field}|{pattern.strip().casefold()}|{category_id}"


def _candidates(conn: sqlite3.Connection, txns: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Every suggestion, dismissed ones included (each with its key and a transaction to build the rule from)."""
    loaded = classify.load_rules(conn)
    user_ids = {r[0] for r in conn.execute("SELECT id FROM rules WHERE created_from = 'user'")}
    ai_cat = {r[0]: r[1] for r in conn.execute("SELECT transaction_id, category_id FROM ai_reviews")}
    groups: dict[str, dict[str, Any]] = {}
    for t in txns:
        if t["source"] != "user":
            continue
        field, pattern = classify.rule_key(t["merchant_name"], t["name"])
        if not pattern or not pattern.strip():
            continue
        g = groups.setdefault(_key(field, pattern, t["category_id"]), {
            "field": field, "pattern": pattern.strip(), "category_id": t["category_id"], "rows": []})
        g["rows"].append(t)
    out = []
    for key, g in groups.items():
        if len(g["rows"]) < MIN_TIMES:
            continue
        covered = False
        for t in g["rows"]:
            hits = [r for r in loaded if r.matches(t)]
            if any(r.id in user_ids for r in hits) or (hits and hits[0].category_id == g["category_id"]):
                covered = True
                break
        if covered:
            continue
        cat = conn.execute("SELECT name FROM categories WHERE id = ?", (g["category_id"],)).fetchone()[0]
        overrode = sum(1 for t in g["rows"] if t["transaction_id"] in ai_cat and ai_cat[t["transaction_id"]] != g["category_id"])
        times = len(g["rows"])
        text = f"You filed {g['pattern']} under {cat} {times} times. Always do that?"
        if overrode:
            text = (f"You moved {g['pattern']} from the AI's pick to {cat} {overrode} time{'s' if overrode != 1 else ''}"
                    + (f" ({times} filed there in all)" if times != overrode else "") + ". Always do that?")
        out.append({"key": key, "merchant": g["pattern"], "category": cat, "times": times, "text": text,
                    "_tid": sorted(t["transaction_id"] for t in g["rows"])[0]})
    out.sort(key=lambda s: (-s["times"], s["merchant"].casefold(), s["category"]))
    return out


def suggestions(conn: sqlite3.Connection, txns: list[sqlite3.Row] | None = None) -> list[dict[str, Any]]:
    skip = set(dismissed(conn))
    return [{k: v for k, v in s.items() if k != "_tid"} for s in _candidates(conn, _txns(conn) if txns is None else txns)
            if s["key"] not in skip]


def payload(conn: sqlite3.Connection) -> dict[str, Any]:
    """GET /api/rules, `finance rules list --json`, finance_rules: the rules, the counts and the suggestions."""
    txns = _txns(conn)
    listed = rules(conn, txns)
    user = sum(1 for r in listed if r["source"] == "user")
    return {"rules": listed, "counts": {"total": len(listed), "user": user, "ai": len(listed) - user},
            "suggestions": suggestions(conn, txns)}


# --- writes ------------------------------------------------------------------------------------------------------------

def _int_id(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuleError("id must be an integer")
    return value


def _call(fn, *args):
    try:
        return fn(*args)
    except classify.ClassifyError as e:
        msg = str(e)
        raise RuleError(msg, 404 if msg.startswith("unknown rule") else e.status) from None


def update(conn: sqlite3.Connection, rule_id: Any, category: Any, pattern: Any = None) -> dict[str, Any]:
    rid = _int_id(rule_id)
    if not isinstance(category, str) or not category.strip():
        raise RuleError("category is required")
    if pattern is not None and not isinstance(pattern, str):
        raise RuleError("pattern must be a string")
    with classify.atomic(conn):
        refiled = _call(classify.edit_rule, conn, rid, category.strip(), pattern)
        return {"rule": rule(conn, rid), "refiled": refiled}


def keep(conn: sqlite3.Connection, rule_id: Any) -> dict[str, Any]:
    rid = _int_id(rule_id)
    with classify.atomic(conn):
        _call(classify.keep_rule, conn, rid)
        return {"rule": rule(conn, rid)}


def delete(conn: sqlite3.Connection, rule_id: Any) -> dict[str, Any]:
    """{deleted, rule (as it was), filed (the ids of the rows it had filed; they stay as they are), undo}. `undo` is
    {"receipt": str}: the only thing /api/rules/restore reads."""
    rid = _int_id(rule_id)
    with classify.atomic(conn):
        out = _call(classify.delete_rule, conn, rid)
    r = out["rule"]
    shown = {"id": rid, "field": r["match_field"], "type": r["match_type"], "pattern": r["pattern"],
             "label": label(r["match_field"], r["match_type"], r["pattern"]), "category": out["category"],
             "source": "user" if r["created_from"] == "user" else "ai"}
    return {"deleted": rid, "rule": shown, "filed": out["filed"], "undo": {"receipt": out["receipt"]}}


def _receipt(undo: Any) -> str:
    if (not isinstance(undo, dict) or set(undo) != {"receipt"} or not isinstance(undo["receipt"], str)
            or not 0 < len(undo["receipt"]) <= 200):
        raise RuleError('undo must be the {"receipt": ...} the delete returned, unchanged')
    return undo["receipt"]


def restore(conn: sqlite3.Connection, undo: Any) -> dict[str, Any]:
    """Undo a delete from its receipt: {rule, relinked (the rows it filed that are linked to it again)}. 404: an unknown
    receipt; 409: already undone, or a rule with the same pattern exists again."""
    receipt = _receipt(undo)
    with classify.atomic(conn):
        rid, relinked = _call(classify.restore_rule, conn, receipt)
        return {"rule": rule(conn, rid), "relinked": relinked}


def _find(conn: sqlite3.Connection, key: Any) -> dict[str, Any]:
    if not isinstance(key, str) or not key:
        raise RuleError("key is required")
    found = next((s for s in _candidates(conn, _txns(conn)) if s["key"] == key), None)
    if found is None:
        raise RuleError("no such suggestion (a rule may cover it already)", 404)
    return found


def accept(conn: sqlite3.Connection, key: Any) -> dict[str, Any]:
    """Add rule: the rule save the Transactions toast uses (rule_from_transaction, a user rule), which re-files the rows
    that rule matches, the approved merchant's, and nothing else (no classify-apply after it, unlike the toast). One
    transaction: the rule and its re-filing, or neither. `refiled` counts only those rows."""
    with classify.atomic(conn):
        s = _find(conn, key)
        saved = _call(classify.rule_from_transaction, conn, s["_tid"], s["category"], "user")
        return {"rule": rule(conn, saved.id), "refiled": saved.refiled}


def dismiss(conn: sqlite3.Connection, key: Any) -> dict[str, Any]:
    with classify.atomic(conn):
        _find(conn, key)
        keys = dismissed(conn)
        if key not in keys:
            keys.append(key)
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value, updated_at = excluded.updated_at", (DISMISSED, json.dumps(keys), now()))
    return {"dismissed": True}


def text(p: dict[str, Any]) -> str:
    """`finance rules list` without --json."""
    c = p["counts"]
    lines = [f"{c['total']} rules · {c['user']} yours · {len(p['suggestions'])} suggestions"]
    for r in p["rules"]:
        lines.append(f"#{r['id']:<5} {r['source']:<4} {r['label']} -> {r['category']} ({r['matches']} match"
                     f"{'es' if r['matches'] != 1 else ''})")
    return "\n".join(lines)


def suggestions_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "no rule suggestions"
    return "\n".join(f"{s['text']}  [key {s['key']}]" for s in items)
