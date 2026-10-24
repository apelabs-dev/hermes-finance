"""Daily AI classification loop. The app never calls an LLM: Hermes (the agent, in a daily cron) is the
classifier, and this module is its interface:

  * `queue()` saves a run: the transactions that still need an AI look, grouped by merchant, oldest first;
  * `apply_decisions()` applies the agent's decisions for that run, validated, in one SQLite transaction;
  * `ai_reviews` records every transaction the AI has looked at, so each one is queued once.

Precedence stays user > user rule > ai > ai rule > plaid (see classify): nothing here changes a user row
or a row filed by a user rule, and an AI rule never replaces a user rule.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from . import classify, insights, prefs
from .db import now

MAX_GROUPS = 250
MIN_CONFIDENCE = 0.7                 # a rule / set / split item below this is downgraded to a flag (the default of
                                     # Settings -> AI provider -> Auto-apply confidence, prefs ai.min_confidence)
MAX_NAMES, MAX_ACCOUNTS, MAX_SAMPLES = 5, 3, 8
ACTIONS = ("rule", "set", "split", "confirm", "flag")
NO_PFC = "(none)"

QUEUE_SQL = """
    SELECT v.transaction_id, v.date, v.name, v.merchant_name, v.amount, v.pfc_detailed, v.category,
           v.account_name, v.institution_name
    FROM v_transactions v
    WHERE v.pending = 0 AND v.category_source IN ('plaid', 'default') AND v.ai_verdict IS NULL
    ORDER BY v.date, v.transaction_id"""

STATE_SQL = """
    SELECT t.transaction_id, t.removed, v.category_id, v.category, v.category_source AS source,
           c.rule_id, COALESCE(ru.created_from, dl.created_from) AS rule_from, r.run_id AS reviewed_by,
           EXISTS (SELECT 1 FROM transaction_splits s WHERE s.transaction_id = t.transaction_id) AS split
    FROM transactions t
    LEFT JOIN v_transactions v  ON v.transaction_id = t.transaction_id
    LEFT JOIN classifications c ON c.transaction_id = t.transaction_id
    LEFT JOIN rules ru          ON ru.id = c.rule_id
    LEFT JOIN rule_deletions dl ON dl.id = c.detached_by AND c.rule_id IS NULL  -- filed by a rule since deleted (TASK-24)
    LEFT JOIN ai_reviews r      ON r.transaction_id = t.transaction_id
    WHERE t.transaction_id IN (SELECT value FROM json_each(?))"""


class AiError(Exception):
    """The apply is refused as a whole and nothing changes (malformed file, unknown or applied run)."""


class _Invalid(Exception):
    """One decision (or split item) is invalid: reported in `errors`, the rest still apply."""


# --- queue -------------------------------------------------------------------------------------------

def _money(x: float) -> float:
    return round(x + 0.0, 2)


def _counts(values) -> dict[str, int]:
    """{value: n}, most common first (ties alphabetical)."""
    return dict(sorted(Counter(values).items(), key=lambda kv: (-kv[1], kv[0])))


def _display(r: sqlite3.Row) -> str:
    return (r["merchant_name"] or r["name"] or "").strip()


def _account(r: sqlite3.Row) -> str:
    return f"{r['institution_name'] or '?'} / {r['account_name'] or '?'}"


def _norm(pattern: str) -> str:
    return pattern.strip().casefold()  # how an exact rule compares


def _same_key_rules(conn: sqlite3.Connection, field: str, pattern: str) -> list[sqlite3.Row]:
    """Exact rules on `field` that match exactly what `pattern` matches, in apply() order."""
    rows = conn.execute(
        """SELECT r.id, r.pattern, r.created_from, c.name AS category FROM rules r
           JOIN categories c ON c.id = r.category_id
           WHERE r.match_type = 'exact' AND r.match_field = ? ORDER BY r.priority DESC, r.id""", (field,))
    return [r for r in rows if _norm(r["pattern"]) == _norm(pattern)]


def _rule_key(rows: list[sqlite3.Row]) -> tuple[str | None, str | None]:
    """classify.rule_from_transaction's (field, pattern), using the group's most common one."""
    keys = Counter(classify.rule_key(r["merchant_name"], r["name"]) for r in rows)
    field, pattern = min(keys, key=lambda k: (-keys[k], k[0] != "merchant_name", k[1] or ""))
    return (field, pattern) if pattern else (None, None)


def _samples(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Up to 8 rows: the cheapest and dearest (refunds, outliers), the latest of each raw name, then the
    most recent. Newest first."""
    recent = sorted(rows, key=lambda r: (r["date"], r["transaction_id"]), reverse=True)
    per_name: dict[str, sqlite3.Row] = {}
    for r in recent:
        per_name.setdefault(r["name"], r)
    chosen: dict[str, sqlite3.Row] = {}
    for r in (min(rows, key=lambda r: r["amount"]), max(rows, key=lambda r: r["amount"]), *per_name.values(), *recent):
        chosen.setdefault(r["transaction_id"], r)
        if len(chosen) == MAX_SAMPLES:
            break
    return [{"transaction_id": r["transaction_id"], "date": r["date"], "name": r["name"], "spend": r["amount"],
             "account": _account(r), "pfc_detailed": r["pfc_detailed"], "category": r["category"]}
            for r in sorted(chosen.values(), key=lambda r: (r["date"], r["transaction_id"]), reverse=True)]


def _history(conn: sqlite3.Connection, keys: set[str]) -> dict[str, tuple[Counter, Counter]]:
    """How the already-decided (ai/user/rule) rows of these merchants are filed: (categories, sources)."""
    out: dict[str, tuple[Counter, Counter]] = {}
    for r in conn.execute("SELECT merchant_name, name, category, category_source FROM v_transactions "
                          "WHERE category_source IN ('ai', 'user', 'rule')"):
        key = insights.merchant_key(r["merchant_name"], r["name"])
        if key in keys:
            cats, sources = out.setdefault(key, (Counter(), Counter()))
            cats[r["category"]] += 1
            sources[r["category_source"]] += 1
    return out


def _group(conn: sqlite3.Connection, group_id: str, rows: list[sqlite3.Row],
           history: tuple[Counter, Counter]) -> dict[str, Any]:
    field, pattern = _rule_key(rows)
    existing = _same_key_rules(conn, field, pattern) if pattern else []
    amounts = [r["amount"] for r in rows]
    return {
        "group_id": group_id,
        "merchant": min(Counter(map(_display, rows)).items(), key=lambda kv: (-kv[1], kv[0]))[0],
        "rule_field": field,
        "rule_pattern": pattern,
        "names": list(_counts(r["name"] for r in rows if r["name"]))[:MAX_NAMES],
        "count": len(rows),
        "spend": {"min": _money(min(amounts)), "max": _money(max(amounts)), "sum": _money(sum(amounts))},
        "current": _counts(r["category"] for r in rows),
        "pfc": _counts(r["pfc_detailed"] or NO_PFC for r in rows),
        "accounts": list(_counts(map(_account, rows)))[:MAX_ACCOUNTS],
        "dates": [rows[0]["date"], rows[-1]["date"]],
        "samples": _samples(rows),
        "existing_rule": ({"category": existing[0]["category"], "created_from": existing[0]["created_from"]}
                          if existing else None),
        "history": _counts(history[0].elements()),
        "history_sources": _counts(history[1].elements()),
    }


def _new_run_id(conn: sqlite3.Connection, created_at: str) -> str:
    base = "ai-" + created_at[:19].replace("-", "").replace(":", "").replace("T", "-")
    run_id, n = base, 1
    while conn.execute("SELECT 1 FROM ai_runs WHERE run_id = ?", (run_id,)).fetchone():
        n += 1
        run_id = f"{base}-{n}"
    return run_id


def queue(conn: sqlite3.Connection, max_groups: int = MAX_GROUPS) -> dict[str, Any]:
    """Save a run of the (up to `max_groups`, oldest first) merchant groups that need an AI look: posted,
    live, never reviewed, and filed only by Plaid (or not at all). Returns the agent's JSON. With AI switched off in
    Settings (prefs ai.mode "off") nothing is queued and no run is saved."""
    if prefs.get(conn, "ai.mode") == "off":
        return {"run_id": None, "created_at": now(), "categories": [], "groups": [], "queued_txns": 0,
                "truncated": False, "ai_off": True,
                "note": "AI is off (Settings \u2192 AI provider): nothing is queued for classification."}
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute(QUEUE_SQL):
        groups[insights.merchant_key(r["merchant_name"], r["name"])].append(r)
    ordered = sorted(groups.items(), key=lambda kv: (kv[1][0]["date"], kv[0]))  # each group is date-sorted
    picked = ordered[:max_groups]
    history = _history(conn, {key for key, _ in picked})
    out = [_group(conn, f"g{i}", rows, history.get(key, (Counter(), Counter())))
           for i, (key, rows) in enumerate(picked, 1)]
    queued_txns = sum(g["count"] for g in out)
    created_at = now()
    with conn:
        run_id = _new_run_id(conn, created_at)
        conn.execute("INSERT INTO ai_runs (run_id, created_at, queued_groups, queued_txns) VALUES (?,?,?,?)",
                     (run_id, created_at, len(out), queued_txns))
        conn.executemany(
            "INSERT INTO ai_run_groups (run_id, group_id, merchant, rule_field, rule_pattern, transaction_ids_json) "
            "VALUES (?,?,?,?,?,?)",
            [(run_id, g["group_id"], g["merchant"], g["rule_field"], g["rule_pattern"],
              json.dumps([r["transaction_id"] for r in rows])) for g, (_, rows) in zip(out, picked)])
    categories = [{"name": r["name"], "is_income": bool(r["is_income"]), "is_transfer": bool(r["is_transfer"])}
                  for r in conn.execute("SELECT name, is_income, is_transfer FROM categories ORDER BY name")]
    return {"run_id": run_id, "created_at": created_at, "categories": categories, "groups": out,
            "queued_txns": queued_txns, "truncated": len(ordered) > max_groups}


# --- validation --------------------------------------------------------------------------------------

def _category(obj: dict, categories: dict[str, str]) -> str:
    value = obj.get("category")
    if not isinstance(value, str) or not value.strip():
        raise _Invalid("category is required")
    name = categories.get(value.strip().casefold())
    if name is None:
        raise _Invalid(f"unknown category {value!r}")
    return name


def _confidence(obj: dict, required: bool) -> float | None:
    value = obj.get("confidence")
    if value is None:
        if required:
            raise _Invalid("confidence is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= 1:
        raise _Invalid("confidence must be a number from 0 to 1")
    return float(value)


def _note(obj: dict) -> str | None:
    value = obj.get("note")
    if value is not None and not isinstance(value, str):
        raise _Invalid("note must be a string")
    return value


def _split_items(i: int, gid: str, items: list, tids: set[str], categories: dict[str, str],
                 errors: list[dict]) -> tuple[list[dict], set[str]]:
    """(valid items, transactions held back by an invalid item: left unreviewed so they come back)."""
    ok, taken, held_back = [], set(), set()
    for j, item in enumerate(items):
        tid = item.get("transaction_id") if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict):
                raise _Invalid("split item must be an object")
            if not isinstance(tid, str) or tid not in tids:
                raise _Invalid(f"transaction {tid!r} is not in group {gid}")
            if tid in taken:
                raise _Invalid(f"duplicate split item for {tid}")
            taken.add(tid)
            try:
                ok.append({"transaction_id": tid, "category": _category(item, categories),
                           "confidence": _confidence(item, True), "note": _note(item)})
            except _Invalid:
                held_back.add(tid)
                raise
        except _Invalid as e:
            errors.append({"index": i, "group_id": gid, "item": j, "transaction_id": tid, "error": str(e)})
    return ok, held_back


def _validate(i: int, d: Any, groups: dict[str, dict], categories: dict[str, str], seen: set[str],
              errors: list[dict]) -> dict | None:
    """The normalized decision, or None after recording why it's invalid in `errors`."""
    gid = d.get("group_id") if isinstance(d, dict) else None
    try:
        if not isinstance(d, dict):
            raise _Invalid("decision must be an object")
        if not isinstance(gid, str) or gid not in groups:
            raise _Invalid(f"unknown group_id {gid!r} for this run")
        if gid in seen:
            raise _Invalid(f"more than one decision for {gid}")
        seen.add(gid)
        action = d.get("action")
        if action not in ACTIONS:
            raise _Invalid(f"action must be one of: {', '.join(ACTIONS)}")
        dec = {"group_id": gid, "action": action, "note": _note(d),
               "confidence": _confidence(d, required=action in ("rule", "set"))}
        if action in ("rule", "set"):
            dec["category"] = _category(d, categories)
        if action == "rule" and not groups[gid]["rule_pattern"]:
            raise _Invalid("this group has neither merchant_name nor name to build a rule from")
        if action == "split":
            dec["rest"] = d.get("rest", "confirm")
            if dec["rest"] not in ("confirm", "flag"):
                raise _Invalid("rest must be confirm or flag")
            if not isinstance(d.get("items"), list) or not d["items"]:
                raise _Invalid("split needs a non-empty items list")
            dec["items"], dec["held_back"] = _split_items(i, gid, d["items"], set(groups[gid]["tids"]),
                                                          categories, errors)
        return dec
    except _Invalid as e:
        errors.append({"index": i, "group_id": gid, "error": str(e)})
        return None


# --- apply -------------------------------------------------------------------------------------------

def _held(st: sqlite3.Row) -> str | None:
    """'user' / 'user-rule' when a user decision files this row (a user rule, or one deleted since: TASK-24): the AI
    never changes it."""
    if st["source"] == "user":
        return "user"
    if st["source"] == "rule" and st["rule_from"] == "user":
        return "user-rule"
    return None


class _Apply:
    """One run's decisions, applied on a connection that already holds the transaction."""

    def __init__(self, conn: sqlite3.Connection, run_id: str, dry_run: bool):
        run = conn.execute("SELECT applied_at FROM ai_runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise AiError(f"unknown run_id {run_id!r}")
        if run["applied_at"]:
            raise AiError(f"run {run_id} was already applied at {run['applied_at']}")
        self.conn, self.run_id, self.ts = conn, run_id, now()
        self.min_confidence = prefs.min_confidence(conn, MIN_CONFIDENCE)
        self.groups = {g["group_id"]: {"merchant": g["merchant"], "rule_field": g["rule_field"],
                                       "rule_pattern": g["rule_pattern"], "tids": json.loads(g["transaction_ids_json"])}
                       for g in conn.execute("SELECT * FROM ai_run_groups WHERE run_id = ? ORDER BY rowid", (run_id,))}
        self.categories = {r["name"].casefold(): r["name"] for r in conn.execute("SELECT name FROM categories")}
        self.out: dict[str, Any] = {
            "run_id": run_id, "dry_run": dry_run, "groups_decided": 0, "txns_changed": 0,
            "by_action": dict.fromkeys(ACTIONS, 0), "changes": [], "flagged": [], "kept_user": 0,
            "kept_user_rule": [], "downgraded": [], "errors": [], "skipped": [], "unreviewed_groups": []}

    def run(self, decisions: list) -> dict[str, Any]:
        seen: set[str] = set()
        valid = [dec for i, d in enumerate(decisions)
                 if (dec := _validate(i, d, self.groups, self.categories, seen, self.out["errors"]))]
        for dec in valid:
            self.decide(dec)
        decided = {dec["group_id"] for dec in valid}
        self.out["groups_decided"] = len(decided)
        self.out["unreviewed_groups"] = [gid for gid in self.groups if gid not in decided]
        return self.out

    def state(self, tids: list[str]) -> dict[str, sqlite3.Row]:
        return {r["transaction_id"]: r for r in self.conn.execute(STATE_SQL, (json.dumps(tids),))}

    def live(self, gid: str, tids: list[str], before: dict[str, sqlite3.Row]) -> list[str]:
        """The group's rows still in play: not removed by Plaid, not reviewed by another run since."""
        out = []
        for t in tids:
            st = before.get(t)
            if st is None or st["removed"]:
                self.out["skipped"].append({"group_id": gid, "transaction_id": t, "reason": "removed"})
            elif st["reviewed_by"]:
                self.out["skipped"].append({"group_id": gid, "transaction_id": t,
                                            "reason": f"already reviewed by {st['reviewed_by']}"})
            elif st["split"]:  # TASK-19: split by the user since the queue was made: never touched, never reviewed
                self.out["skipped"].append({"group_id": gid, "transaction_id": t, "reason": "split by you"})
            else:
                out.append(t)
        return out

    def downgrade(self, gid: str, action: str, category: str, confidence: float, note: str | None,
                  tid: str | None = None) -> str:
        """Record a below-threshold rule / set / split item; returns the flag note that replaces it."""
        self.out["downgraded"].append({"group_id": gid, "action": action, "category": category,
                                       "confidence": confidence, "transaction_id": tid})
        return f"low confidence ({confidence:.2f}) for {category}" + (f": {note}" if note else "")

    def kept_rule(self, gid: str, rule_id: int, category: str, n: int) -> None:
        self.out["kept_user_rule"].append({"group_id": gid, "merchant": self.groups[gid]["merchant"],
                                           "rule_id": rule_id, "category": category, "n": n})

    def ai_set(self, tid: str, category: str, confidence: float, note: str | None) -> str:
        try:
            return classify.set_category(self.conn, tid, category, "ai", confidence, note)
        except sqlite3.DatabaseError as e:  # the trigger guarding user rows: same outcome as the check
            if "never overwritten" not in str(e):
                raise
            return "kept-user"

    def rule(self, gid: str, dec: dict, live: list[str], before: dict[str, sqlite3.Row]) -> dict[str, tuple]:
        """Save the AI rule and apply it; rows it doesn't file get a per-transaction ai set. A user rule
        on the same key is never replaced: then nothing changes (kept-user-rule)."""
        group, category, conf, note = self.groups[gid], dec["category"], dec["confidence"], dec["note"]
        same = _same_key_rules(self.conn, group["rule_field"], group["rule_pattern"])
        user_rule = next((r for r in same if r["created_from"] == "user"), None)
        if user_rule is None:
            saved = classify.add_rule(self.conn, group["rule_field"], same[0]["pattern"] if same else group["rule_pattern"],
                                      category, "exact", priority=classify.AI_RULE_PRIORITY, created_from="ai")
            if not saved.kept_user_rule:
                classify.apply(self.conn)
                filed = self.state(live)
                return {t: ("confirm", conf, note) if filed[t]["source"] == "rule" and not _held(before[t])
                        else ("set", category, conf, note) for t in live}
            user_rule = self.conn.execute("SELECT r.id, c.name AS category FROM rules r JOIN categories c "
                                          "ON c.id = r.category_id WHERE r.id = ?", (saved.id,)).fetchone()
        self.kept_rule(gid, user_rule["id"], user_rule["category"], len(live))
        return {t: ("confirm", conf, note) for t in live}

    def decide(self, dec: dict) -> None:
        gid, action, conf, note = dec["group_id"], dec["action"], dec["confidence"], dec["note"]
        tids = [t for t in self.groups[gid]["tids"] if t not in dec.get("held_back", ())]
        before = self.state(tids)
        live = self.live(gid, tids, before)
        if action in ("rule", "set") and conf < self.min_confidence:
            note = self.downgrade(gid, action, dec["category"], conf, note)
            action = "flag"
        self.out["by_action"][action] += 1

        # per row: ("set", category, confidence, note) or ("confirm" | "flag", confidence, note)
        plan: dict[str, tuple] = {}
        if action == "rule":
            plan = self.rule(gid, dec, live, before)
        elif action == "set":
            plan = {t: ("set", dec["category"], conf, note) for t in live}
        elif action == "split":
            for item in dec["items"]:
                tid, c, cat = item["transaction_id"], item["confidence"], item["category"]
                if c < self.min_confidence:
                    plan[tid] = ("flag", c, self.downgrade(gid, "split", cat, c, item["note"], tid))
                else:
                    plan[tid] = ("set", cat, c, item["note"])
        rest = ("flag" if action == "flag" or dec.get("rest") == "flag" else "confirm", conf, note)

        verdicts: dict[str, tuple] = {}
        kept_rules: Counter = Counter()
        for t in live:
            kind, *args = plan.get(t, rest)
            held = _held(before[t])
            if held is None and kind == "set" and (outcome := self.ai_set(t, *args)) != "set":
                held = outcome.removeprefix("kept-")
            if held and kind != "confirm":  # a user decision files this row: the AI leaves it alone
                if held == "user":
                    self.out["kept_user"] += 1
                else:
                    kept_rules[before[t]["rule_id"], before[t]["category"]] += 1
            verdicts[t] = ("flagged" if kind == "flag" and not held else None, *args[-2:])
        for (rule_id, category), n in kept_rules.items():
            self.kept_rule(gid, rule_id, category, n)
        self.record(gid, action, before, verdicts)

    def record(self, gid: str, action: str, before: dict[str, sqlite3.Row], verdicts: dict[str, tuple]) -> None:
        """One ai_reviews row per reviewed transaction, plus the summary's changes / flagged entries."""
        after = self.state(list(verdicts))
        moved: dict[str, Counter] = defaultdict(Counter)  # to category -> {from category: n}
        flagged: Counter = Counter()                     # note -> n
        rows = []
        for t, (verdict, confidence, note) in verdicts.items():
            b, a = before[t], after[t]
            if verdict is None:
                verdict = "changed" if a["category_id"] != b["category_id"] else "confirmed"
            if verdict == "changed":
                moved[a["category"]][b["category"]] += 1
            elif verdict == "flagged":
                flagged[note] += 1
            rows.append((t, self.run_id, self.ts, verdict, a["category_id"], confidence, note))
        self.conn.executemany("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, category_id, "
                              "confidence, note) VALUES (?,?,?,?,?,?,?)", rows)
        merchant = self.groups[gid]["merchant"]
        for to, frm in moved.items():
            n = sum(frm.values())
            self.out["txns_changed"] += n
            self.out["changes"].append({"group_id": gid, "merchant": merchant, "from": _counts(frm.elements()),
                                        "to": to, "n": n, "action": action})
        for note, n in flagged.items():
            self.out["flagged"].append({"group_id": gid, "merchant": merchant, "n": n, "note": note})


def apply_decisions(conn: sqlite3.Connection, payload: Any, dry_run: bool = False) -> dict[str, Any]:
    """Apply one run's decisions in a single SQLite transaction, rolled back on any exception (re-raised)
    and on dry_run. Raises AiError, changing nothing, for a malformed payload or an unknown/applied run."""
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        raise AiError('malformed decisions: expected a JSON object with a "decisions" list')
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise AiError("malformed decisions: run_id is required")
    conn.execute("BEGIN IMMEDIATE")  # classify's writes join this transaction instead of committing
    try:
        applier = _Apply(conn, run_id, dry_run)
        summary = applier.run(payload["decisions"])
        if dry_run:
            conn.rollback()
        else:
            conn.execute("UPDATE ai_runs SET applied_at = ?, summary_json = ? WHERE run_id = ?",
                         (applier.ts, json.dumps(summary), run_id))
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return summary
