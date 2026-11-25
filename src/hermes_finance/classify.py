"""Deterministic classification: rules + Plaid-mapped defaults. No LLM here.

Precedence on write: user > user rule > ai > ai rule > plaid. `apply()` only ever (re)writes rows
whose source is 'rule' or 'plaid' (or missing); 'ai' and 'user' rows are left alone, and a DB
trigger makes overwriting a 'user' row impossible. Saving a rule with created_from='user' re-files
the rows it matches right away, 'ai' rows included (an explicit "always" beats an AI guess); user
rules default to priority 200 and AI rules to 100, so the user's also win in `apply()`.

Writes commit on their own, or join the caller's open transaction (`ai.apply_decisions` runs a
whole day's decisions as one).

TASK-24 (the rules manager, rulebook.py): `edit_rule`, `keep_rule` and `delete_rule`. Editing or keeping a rule makes it
the user's (priority at least 200), so the next AI run can't change it back, and re-files the plaid/rule/ai rows it
matches at once, like saving a user rule. Deleting one leaves the rows it filed as they are: they lose their link to it
(rule_id NULL, category and source 'rule' kept) and `apply()` never touches a 'rule' row without a rule, so neither the
next classify-apply nor a later rule that reuses the id moves them. A user rule saved later still re-files them.

Deleting also writes a receipt (db.TASK24_SCHEMA rule_deletions) the detached rows point at (classifications.detached_by):
a row a deleted *user* rule filed keeps its protection from the AI, and `restore_rule` (Undo) takes only the receipt, so
the caller can't make up a rule, its owner or its rows. Undo links again only the rows still as the delete left them.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import NamedTuple

from .db import now

SOURCES = ("plaid", "rule", "ai", "user")
RANK = {s: i for i, s in enumerate(SOURCES)}
USER_RULE_PRIORITY, AI_RULE_PRIORITY = 200, 100


class ClassifyError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status  # the HTTP status rulebook answers with (TASK-24 fix: 404 / 409 for Undo)


class RuleSaved(NamedTuple):
    id: int
    kept_user_rule: bool = False  # a non-user save on a user rule's key: nothing changed
    refiled: int = 0              # rows a user rule re-filed as it was saved


def _writing(conn: sqlite3.Connection):
    """`with conn:` (commit on exit), unless the caller holds an open transaction: then join it."""
    return nullcontext() if conn.in_transaction else conn


@contextmanager
def atomic(conn: sqlite3.Connection):
    """TASK-24 fix: one all-or-nothing write, reads included (rulebook's writes: a suggestion's rule and its re-filing, a
    delete and its receipt, an Undo). BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any error, so nothing it read can change
    under it and a failure half way leaves nothing behind. Inside the caller's open transaction: a savepoint, undone on its
    own on an error (the caller's transaction goes on)."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT classify_atomic")
        try:
            yield
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK TO classify_atomic")
                    conn.execute("RELEASE classify_atomic")
                except sqlite3.Error:
                    pass  # SQLite already rolled the whole transaction back (disk full, I/O): the first error stands
            raise
        else:
            conn.execute("RELEASE classify_atomic")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@dataclass
class Rule:
    id: int
    match_field: str
    match_type: str
    pattern: str
    category_id: int
    regex: re.Pattern | None

    def matches(self, txn: sqlite3.Row) -> bool:
        value = txn[self.match_field]
        if not value:
            return False
        if self.match_type == "exact":
            return value.strip().casefold() == self.pattern.strip().casefold()
        return bool(self.regex and self.regex.search(value))


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    rules = []
    for r in conn.execute("SELECT * FROM rules ORDER BY priority DESC, id ASC"):
        regex = None
        if r["match_type"] == "regex":
            try:
                regex = re.compile(r["pattern"], re.IGNORECASE)
            except re.error:
                continue  # a broken rule never matches
        rules.append(Rule(r["id"], r["match_field"], r["match_type"], r["pattern"], r["category_id"], regex))
    return rules


def plaid_default(conn: sqlite3.Connection, pfc_detailed: str | None, pfc_primary: str | None) -> int:
    for code in (pfc_detailed, pfc_primary):
        if code:
            row = conn.execute("SELECT category_id FROM plaid_category_map WHERE pfc_code=?", (code,)).fetchone()
            if row:
                return row[0]
    return category_id(conn, "Other")


def default_filing(conn: sqlite3.Connection, t: sqlite3.Row) -> int:
    """The category a Plaid-default row (no rule, nothing from the AI or the user) is filed under: plaid_default(), except
    that a refund is never Income (owner decision). When the default lands in an income category and the row is money
    in (amount < 0) on a credit or loan account, or one whose name says REFUND, RETURN, REVERSAL or CREDIT VOUCHER, the
    row is filed Other and nets against spending. A merchant category (Plaid's own refund under General merchandise)
    is kept, and so is a bank deposit that is simply income. A tax refund on a bank account stays Income (owner
    decision): Plaid's INCOME_TAX_REFUND code, or a name that says so (TAX_REFUND_WORDS). `t` is a _candidates() row."""
    cat = plaid_default(conn, t["pfc_detailed"], t["pfc_primary"])
    if (t["amount"] is not None and t["amount"] < 0
            and (t["account_type"] in ("credit", "loan")
                 or (REFUND_WORDS.search(t["name"] or "") and not is_tax_refund(t["name"], t["pfc_detailed"])))
            and conn.execute("SELECT is_income FROM categories WHERE id = ?", (cat,)).fetchone()[0]):
        return category_id(conn, "Other")
    return cat


def category_id(conn: sqlite3.Connection, name_or_id: str | int) -> int:
    """Resolve a category by id or name, case-insensitively. A hidden category (v23) is still accepted by name: an
    existing rule or the user's own filing must keep working; hidden only leaves the new-selection lists."""
    row = conn.execute("SELECT id FROM categories WHERE id = ? OR name = ? COLLATE NOCASE",
                       (name_or_id, str(name_or_id))).fetchone()
    if not row:
        names = ", ".join(r[0] for r in conn.execute("SELECT name FROM categories ORDER BY name"))
        raise ClassifyError(f"unknown category {name_or_id!r}. Valid: {names}")
    return row[0]


def _candidates(conn: sqlite3.Connection, sources: tuple[str, ...], unlinked: bool = True) -> list[sqlite3.Row]:
    """Live transactions with no classification, or one whose source is in `sources`. unlinked=False leaves out the
    rows a deleted rule filed (source 'rule', rule_id NULL: TASK-24), which stay as they are. An imported row also
    carries its file's recorded kind (`imports.kind`: csv / ofx / pdf - the provenance the description hint needs, so
    a typeless OFX row is never read as a CSV one) and its manual account's kind (accounts has no row for it: the
    view joins it the same way)."""
    marks = ", ".join("?" * len(sources))
    keep = "" if unlinked else " AND NOT COALESCE(c.source = 'rule' AND c.rule_id IS NULL, 0)"  # no row: a candidate
    return conn.execute(
        f"""SELECT t.transaction_id, t.name, t.merchant_name, t.pfc_primary, t.pfc_detailed, t.source, t.amount,
                   a.type AS account_type, i.kind AS import_kind, ma.kind AS manual_kind,
                   c.category_id AS cur_category, c.source AS cur_source, c.rule_id AS cur_rule
            FROM transactions t LEFT JOIN classifications c USING (transaction_id)
                 LEFT JOIN accounts a ON a.account_id = t.account_id
                 LEFT JOIN imports i ON i.id = t.import_id
                 LEFT JOIN manual_accounts ma ON t.source = 'import'
                      AND ma.id = CAST(substr(t.account_id, 8) AS INTEGER)
            WHERE t.removed = 0 AND (c.source IS NULL OR c.source IN ({marks})){keep}""", sources).fetchall()


def _file(conn: sqlite3.Connection, transaction_id: str, new: tuple, ts: str, over: tuple[str, ...]) -> None:
    """Write a rule/plaid classification (category_id, source, rule_id, note) over a row whose
    source is in `over` (never 'user')."""
    marks = ", ".join("?" * len(over))
    conn.execute(
        f"""INSERT INTO classifications (transaction_id, category_id, source, rule_id, note,
                confidence, updated_at)
            VALUES (?,?,?,?,?,NULL,?)
            ON CONFLICT(transaction_id) DO UPDATE SET
                category_id=excluded.category_id, source=excluded.source,
                rule_id=excluded.rule_id, note=excluded.note, confidence=NULL,
                updated_at=excluded.updated_at
            WHERE classifications.source IN ({marks})""",
        (transaction_id, *new, ts, *over))


# What a row that is money IN is saved with when its provider sends no category (SimpleFIN; the importer shares the card
# payment hint): income or a transfer in on a bank account, a card payment on a card or loan. A refund is never Income
# (owner decision), so on a card or loan every inflow but a payment, and on any account a line that says REFUND, RETURN,
# REVERSAL or CREDIT VOUCHER, gets no hint: it stays undecided and nets against spending.
INCOME_HINT = ("INCOME", "INCOME_OTHER_INCOME")
TRANSFER_IN_HINT = ("TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")
CARD_PAYMENT_HINT = ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
CARD_PAYMENT_WORDS = re.compile(r"\b(?:PAYMENT|PAYMT|PMT)\b", re.IGNORECASE)
_TRANSFER_WORDS = re.compile(r"\b(?:TRANSFER|XFER)\b", re.IGNORECASE)
REFUND_WORDS = re.compile(r"\b(?:REFUND|RETURN|REVERSAL|CREDIT\s+VOUCHER)\b", re.IGNORECASE)   # whole words: not RETURNS
# A tax refund stays Income on both sides (owner decision): Plaid's INCOME_TAX_REFUND code, or a description that names
# one. Whole words, so the Plaid default and the SimpleFIN hint agree.
TAX_REFUND_WORDS = re.compile(r"\b(?:TAX\s*REF(?:UND)?|TAX\s*RFD|IRS\s+TREAS)\b", re.IGNORECASE)
INCOME_TAX_REFUND_HINT = ("INCOME", "INCOME_TAX_REFUND")


def is_tax_refund(description: str | None, pfc_detailed: str | None = None) -> bool:
    return pfc_detailed == "INCOME_TAX_REFUND" or bool(TAX_REFUND_WORDS.search(description or ""))


def inflow_hint(description: str | None, account_type: str | None, amount: float) -> tuple[str | None, str | None]:
    """(pfc_primary, pfc_detailed) for a row from a provider that sends no category: only money IN (the app's amount
    < 0) can get one, money out gets none. In this order:
      1. on a credit card or loan, a line whose description names a payment is that account's own payment
         (CARD_PAYMENT_HINT: a transfer);
      2. on a bank account, a description that names a tax refund (TAX_REFUND_WORDS) is INCOME_TAX_REFUND: it stays
         Income, ahead of the refund rule below;
      3. a description with the whole word REFUND, RETURN or REVERSAL, or the phrase CREDIT VOUCHER, gets none: a
         refund is never Income, so it stays undecided and nets against spending;
      4. every other inflow on a credit card or loan gets none either (a merchant credit is a refund);
      5. a description that says TRANSFER or XFER is a transfer in (TRANSFER_IN_HINT);
      6. any other inflow on a bank account (or an account of unknown type) is income (INCOME_HINT).
    A rule or the user's own filing still wins over all of these: apply() checks rules first and never touches a
    user's choice."""
    if amount >= 0:
        return None, None
    text = description or ""
    on_credit = account_type in ("credit", "loan")
    if on_credit and CARD_PAYMENT_WORDS.search(text):
        return CARD_PAYMENT_HINT
    if not on_credit and TAX_REFUND_WORDS.search(text):
        return INCOME_TAX_REFUND_HINT
    if REFUND_WORDS.search(text) or on_credit:
        return None, None
    if _TRANSFER_WORDS.search(text):
        return TRANSFER_IN_HINT
    return INCOME_HINT


_MANUAL_ACCOUNT_TYPES = {"credit_card": "credit"}   # accounts.type's vocabulary; an imported row's own kind


def _import_hint(t: sqlite3.Row) -> tuple[str | None, str | None]:
    """The (pfc_primary, pfc_detailed) code a CSV or PDF import's money-in row is saved with, or (None, None).

    A file has no Plaid category, and a CSV / PDF row has no TRNTYPE either (an OFX row does: importer's
    OFX_INCOME_HINTS, written at commit), so with nothing on the row it was filed under Other - a spending
    category - and a payroll deposit counted as *negative* spending (QA 0.5 F1/F2). This is the same rule a synced
    row with no provider category gets (SimpleFIN's inflow_hint): money out gets nothing; on a card or loan only
    the account's own payment line gets a code, and never one that names a refund; a tax refund stays Income; a
    refund, return or reversal stays undecided (a refund is never Income); a transfer in is a transfer; any other
    credit is income.

    The file's recorded kind decides (`imports.kind`), not the absence of a TRNTYPE: an OFX row whose type is
    missing or blank is still an OFX row, and only the OFX rule may file it (owner rule: a generic credit is never
    Income); a row with no recorded import (import_id NULL) is not re-filed either. A rule still wins over this in
    apply(), and an AI or user decision is never touched."""
    if (t["source"] != "import" or t["pfc_primary"] or t["import_kind"] not in ("csv", "pdf")
            or t["amount"] is None or t["amount"] >= 0):
        return None, None
    # an imported row's account is a manual one (importer only targets those): its kind, mapped to accounts.type's
    # vocabulary; a synced account's type is taken as it is, and anything else counts as a bank account
    kind = _MANUAL_ACCOUNT_TYPES.get(t["manual_kind"]) or t["account_type"] or "bank"
    if kind in ("credit", "loan") and REFUND_WORDS.search(t["name"] or ""):
        return None, None    # an explicit refund is not the account's own payment, and not income either
    return inflow_hint(t["name"], kind, t["amount"])


def apply(conn: sqlite3.Connection) -> dict[str, int]:
    """(a) apply rules, (b) fill Plaid-mapped defaults, for everything not decided by ai/user. A CSV / PDF
    import's money-in row first gets its code from _import_hint(), saved on the row the way an OFX / synced row's
    is; then a rule files it if one matches, else that code fills it in (Income, or Transfers for a transfer in)
    instead of Other - and a re-apply finds the code already on the row."""
    rules = load_rules(conn)
    counts = {"rule": 0, "plaid": 0, "unchanged": 0}
    ts = now()
    rows = _candidates(conn, ("rule", "plaid"), unlinked=False)
    with _writing(conn):
        for t in rows:
            hint = _import_hint(t)
            if hint != (None, None):
                conn.execute("UPDATE transactions SET pfc_primary = ?, pfc_detailed = ? WHERE transaction_id = ?",
                             (*hint, t["transaction_id"]))
            rule = next((r for r in rules if r.matches(t)), None)
            if rule:
                new = (rule.category_id, "rule", rule.id, f"rule #{rule.id}")
            elif hint != (None, None):
                new = (plaid_default(conn, hint[1], hint[0]), "plaid", None, None)
            elif t["source"] == "simplefin" and not t["pfc_primary"]:
                # SimpleFIN sends no category, and a money-out row has no hint (inflow_hint): it stays
                # undecided for the AI / a person. A 'plaid' filing left by an earlier hint (the row's sign changed) goes.
                if t["cur_source"] == "plaid":
                    conn.execute("DELETE FROM classifications WHERE transaction_id = ? AND source = 'plaid'",
                                 (t["transaction_id"],))
                continue
            else:
                new = (default_filing(conn, t), "plaid", None, None)
            if (t["cur_category"], t["cur_source"], t["cur_rule"]) == new[:3]:
                counts["unchanged"] += 1
                continue
            _file(conn, t["transaction_id"], new, ts, ("rule", "plaid"))
            counts[new[1]] += 1
    return counts


def _refile(conn: sqlite3.Connection, rule_id: int) -> int:
    """File every plaid/rule/ai row that rule `rule_id` matches under the rule that wins it (the
    same pick `apply()` makes, so the next apply leaves it there). Returns the rows written."""
    rules = load_rules(conn)
    saved = next((r for r in rules if r.id == rule_id), None)
    if saved is None:
        return 0
    n, ts = 0, now()
    for t in _candidates(conn, ("plaid", "rule", "ai")):
        if not saved.matches(t):
            continue
        rule = next(r for r in rules if r.matches(t))
        new = (rule.category_id, "rule", rule.id, f"rule #{rule.id}")
        if (t["cur_category"], t["cur_source"], t["cur_rule"]) != new[:3]:
            _file(conn, t["transaction_id"], new, ts, ("plaid", "rule", "ai"))
            n += 1
    return n


def set_category(conn: sqlite3.Connection, transaction_id: str, category: str, source: str = "user",
                 confidence: float | None = None, note: str | None = None) -> str:
    """Record an ai/user decision. Returns 'set', 'kept-user' or 'kept-user-rule' (ai never
    overrides a user classification, nor a row filed by a user rule, even one deleted since)."""
    if source not in ("ai", "user"):
        raise ClassifyError("source must be 'ai' or 'user'")
    if not conn.execute("SELECT 1 FROM transactions WHERE transaction_id=?", (transaction_id,)).fetchone():
        raise ClassifyError(f"unknown transaction {transaction_id!r}")
    cat = category_id(conn, category)
    # the rule that filed it: its rule, or (TASK-24 fix) the deleted one it points at
    cur = conn.execute("""SELECT c.source, COALESCE(r.created_from, d.created_from) AS rule_from
                          FROM classifications c LEFT JOIN rules r ON r.id = c.rule_id
                          LEFT JOIN rule_deletions d ON d.id = c.detached_by AND c.rule_id IS NULL
                          WHERE c.transaction_id=?""", (transaction_id,)).fetchone()
    if cur and source != "user":
        if cur["source"] == "user":
            return "kept-user"
        if cur["source"] == "rule" and cur["rule_from"] == "user":
            return "kept-user-rule"
    if conn.execute("SELECT 1 FROM transaction_splits WHERE transaction_id = ? LIMIT 1", (transaction_id,)).fetchone():
        raise ClassifyError("this purchase is split: change its lines, or unsplit it, to give it one category")  # TASK-19
    with _writing(conn):
        conn.execute(
            """INSERT INTO classifications (transaction_id, category_id, source, confidence, note, rule_id, updated_at)
               VALUES (?,?,?,?,?,NULL,?)
               ON CONFLICT(transaction_id) DO UPDATE SET
                   category_id=excluded.category_id, source=excluded.source,
                   confidence=excluded.confidence, note=excluded.note, rule_id=NULL,
                   updated_at=excluded.updated_at""",
            (transaction_id, cat, source, confidence, note, now()))
    return "set"


def add_rule(conn: sqlite3.Connection, match_field: str, pattern: str, category: str,
             match_type: str = "exact", priority: int | None = None, created_from: str = "user") -> RuleSaved:
    """Create or update a rule. A user rule (default priority 200) re-files the plaid/rule/ai rows it
    matches at once. Any other rule (default 100) never replaces a user rule: on a user rule's key it
    is a no-op that returns the user rule's id with kept_user_rule=True."""
    if match_field not in ("merchant_name", "name"):
        raise ClassifyError("match_field must be 'merchant_name' or 'name'")
    if match_type not in ("exact", "regex"):
        raise ClassifyError("match_type must be 'exact' or 'regex'")
    if match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            raise ClassifyError(f"invalid regex: {e}") from None
    cat = category_id(conn, category)
    user = created_from == "user"
    if priority is None:
        priority = USER_RULE_PRIORITY if user else AI_RULE_PRIORITY
    key = (match_field, match_type, pattern)
    found = conn.execute("SELECT id, created_from FROM rules WHERE match_field=? AND match_type=? AND pattern=?",
                         key).fetchone()
    if found and found["created_from"] == "user" and not user:
        return RuleSaved(found["id"], kept_user_rule=True)
    with _writing(conn):
        conn.execute(
            """INSERT INTO rules (match_field, match_type, pattern, category_id, priority, created_from, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(match_field, match_type, pattern) DO UPDATE SET
                   category_id=excluded.category_id, priority=excluded.priority,
                   created_from=excluded.created_from""",
            (*key, cat, priority, created_from, now()))
        rule_id = conn.execute("SELECT id FROM rules WHERE match_field=? AND match_type=? AND pattern=?",
                               key).fetchone()[0]
        refiled = _refile(conn, rule_id) if user else 0
    return RuleSaved(rule_id, refiled=refiled)


def _rule_row(conn: sqlite3.Connection, rule_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise ClassifyError(f"unknown rule {rule_id!r}")
    return row


def _release(conn: sqlite3.Connection, rule_id: int) -> int:
    """Rows still linked to `rule_id` that it no longer matches (its pattern changed): filed the way `apply()` would
    (another rule, else the Plaid default). Returns the rows written."""
    rules = load_rules(conn)
    mine = next((r for r in rules if r.id == rule_id), None)
    n, ts = 0, now()
    for t in _candidates(conn, ("rule",)):
        if t["cur_rule"] != rule_id or (mine and mine.matches(t)):
            continue
        rule = next((r for r in rules if r.matches(t)), None)
        new = ((rule.category_id, "rule", rule.id, f"rule #{rule.id}") if rule
               else (default_filing(conn, t), "plaid", None, None))
        _file(conn, t["transaction_id"], new, ts, ("rule",))
        n += 1
    return n


def edit_rule(conn: sqlite3.Connection, rule_id: int, category: str, pattern: str | None = None) -> int:
    """TASK-24: change a rule's category (and, for a user rule only, its pattern). The rule becomes the user's
    (priority at least 200), so an AI run can't change it back. Re-files the plaid/rule/ai rows it matches at once
    (and the rows it filed that it no longer matches); user rows are never touched. Returns the rows re-filed."""
    row = _rule_row(conn, rule_id)
    if pattern is not None and row["created_from"] != "user":
        raise ClassifyError("only your own rules' pattern can be changed (keep this rule first)")
    if pattern is not None and pattern != row["pattern"]:
        pattern = pattern.strip()
        if not pattern:
            raise ClassifyError("pattern can't be empty")
        if row["match_type"] == "regex":
            try:
                re.compile(pattern)
            except re.error as e:
                raise ClassifyError(f"invalid regex: {e}") from None
        clash = conn.execute("SELECT id FROM rules WHERE match_field=? AND match_type=? AND pattern=? AND id<>?",
                             (row["match_field"], row["match_type"], pattern, rule_id)).fetchone()
        if clash:
            raise ClassifyError(f"rule #{clash[0]} already has that pattern")
    else:
        pattern = row["pattern"]
    cat = category_id(conn, category)
    with _writing(conn):
        conn.execute("UPDATE rules SET category_id=?, pattern=?, created_from='user', priority=MAX(priority, ?) "
                     "WHERE id=?", (cat, pattern, USER_RULE_PRIORITY, rule_id))
        return _refile(conn, rule_id) + _release(conn, rule_id)


def keep_rule(conn: sqlite3.Connection, rule_id: int) -> int:
    """TASK-24: an AI rule becomes the user's (created_from 'user', priority at least 200), and re-files what it
    matches like saving a user rule. Returns the rows re-filed."""
    _rule_row(conn, rule_id)
    with _writing(conn):
        conn.execute("UPDATE rules SET created_from='user', priority=MAX(priority, ?) WHERE id=?",
                     (USER_RULE_PRIORITY, rule_id))
        return _refile(conn, rule_id)


def receipt_hash(receipt: str) -> str:
    return hashlib.sha256(receipt.encode()).hexdigest()


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> dict:
    """TASK-24: delete a rule. The rows it filed stay as they are (category and source 'rule' kept, rule_id NULL) and
    point at the delete's receipt row (rule_deletions): `apply()` leaves them alone, and the AI too when it was a user
    rule. Returns {"receipt": the Undo (a random string, only its hash is kept), "rule": the rule as it was, "category",
    "filed": the ids of the rows it had filed}."""
    row = _rule_row(conn, rule_id)
    cat = conn.execute("SELECT name FROM categories WHERE id=?", (row["category_id"],)).fetchone()
    receipt = secrets.token_urlsafe(24)
    with _writing(conn):
        filed = [r[0] for r in conn.execute("SELECT transaction_id FROM classifications WHERE rule_id=? AND source='rule' "
                                            "ORDER BY transaction_id", (rule_id,))]
        did = conn.execute(
            "INSERT INTO rule_deletions (receipt_hash, rule_id, match_field, match_type, pattern, category_id, priority, "
            "created_from, created_at, filed, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_hash(receipt), rule_id, row["match_field"], row["match_type"], row["pattern"], row["category_id"],
             row["priority"], row["created_from"], row["created_at"], len(filed), now())).lastrowid
        conn.execute("UPDATE classifications SET rule_id=NULL, detached_by=? WHERE rule_id=? AND source='rule'",
                     (did, rule_id))
        conn.execute("UPDATE classifications SET rule_id=NULL WHERE rule_id=?", (rule_id,))  # any other source: none
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    return {"receipt": receipt, "rule": {k: row[k] for k in row.keys()}, "category": cat[0] if cat else None,
            "filed": filed}


def restore_rule(conn: sqlite3.Connection, receipt: str) -> tuple[int, int]:
    """TASK-24: undo a delete_rule from its receipt alone: the rule comes back exactly as the delete recorded it (with its
    id when that's free), whatever else a caller sends, and the rows it filed are linked to it again: only the ones still
    as the delete left them (still pointing at this delete, which any change to a row ends) that it still matches. A
    receipt restores once; an unknown one restores nothing. Returns (the rule's id, the rows linked again)."""
    if not isinstance(receipt, str) or not receipt:
        raise ClassifyError("undo: receipt is required")
    d = conn.execute("SELECT * FROM rule_deletions WHERE receipt_hash = ?", (receipt_hash(receipt),)).fetchone()
    if d is None:
        raise ClassifyError("undo: unknown receipt (nothing was deleted with it)", 404)
    if d["restored_at"]:
        raise ClassifyError("undo: that delete was already undone", 409)
    key = (d["match_field"], d["match_type"], d["pattern"])
    if conn.execute("SELECT 1 FROM rules WHERE match_field=? AND match_type=? AND pattern=?", key).fetchone():
        raise ClassifyError("a rule with that pattern exists again; nothing to restore", 409)
    cat = category_id(conn, d["category_id"])
    rid = None if conn.execute("SELECT 1 FROM rules WHERE id=?", (d["rule_id"],)).fetchone() else d["rule_id"]
    with _writing(conn):
        rid = conn.execute("INSERT INTO rules (id, match_field, match_type, pattern, category_id, priority, created_from, "
                           "created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (rid, *key, cat, d["priority"], d["created_from"], d["created_at"])).lastrowid
        try:
            regex = re.compile(d["pattern"], re.IGNORECASE) if d["match_type"] == "regex" else None
        except re.error:
            regex = None  # a broken rule never matches: it links nothing
        rule = Rule(rid, d["match_field"], d["match_type"], d["pattern"], cat, regex)
        rows = conn.execute("""SELECT t.transaction_id, t.name, t.merchant_name FROM classifications c
                               JOIN transactions t USING (transaction_id)
                               WHERE c.detached_by = ? AND c.source = 'rule' AND c.rule_id IS NULL AND c.category_id = ?""",
                            (d["id"], cat)).fetchall()
        back = [r["transaction_id"] for r in rows if rule.matches(r)]
        conn.executemany("UPDATE classifications SET rule_id=?, note=?, detached_by=NULL WHERE transaction_id=? "
                         "AND detached_by=?", [(rid, f"rule #{rid}", t, d["id"]) for t in back])
        conn.execute("UPDATE rule_deletions SET restored_at=?, restored_rule_id=? WHERE id=?", (now(), rid, d["id"]))
    return rid, len(back)


def rule_key(merchant_name: str | None, name: str | None) -> tuple[str, str | None]:
    """(match_field, pattern) of the exact rule a transaction would get: its merchant_name, else its name."""
    field = "merchant_name" if merchant_name else "name"
    return field, merchant_name or name or None


def rule_from_transaction(conn: sqlite3.Connection, transaction_id: str, category: str,
                          created_from: str) -> RuleSaved:
    t = conn.execute("SELECT merchant_name, name FROM transactions WHERE transaction_id=?",
                     (transaction_id,)).fetchone()
    field, pattern = rule_key(t["merchant_name"], t["name"])
    if not pattern:
        raise ClassifyError("transaction has neither merchant_name nor name to build a rule from")
    return add_rule(conn, field, pattern, category, "exact", created_from=created_from)


def unclassified(conn: sqlite3.Connection, limit: int = 50, since: str | None = None) -> list[dict]:
    """Transactions with no ai/user/rule decision yet (only a Plaid default, or nothing)."""
    q = """SELECT v.transaction_id, v.date, v.name, v.merchant_name, v.amount, v.iso_currency, v.pending,
                  v.payment_channel, v.account_name, v.account_type, v.institution_name,
                  v.pfc_primary, v.pfc_detailed, v.pfc_confidence,
                  v.category AS suggested_category, v.category_source AS current_source,
                  v.website, json_extract(t.raw_json, '$.original_description') AS original_description,
                  t.counterparties_json
           FROM v_transactions v JOIN transactions t USING (transaction_id)
           WHERE v.category_source NOT IN ('user', 'ai', 'rule')"""
    args: list = []
    if since:
        q += " AND v.date >= ?"
        args.append(since)
    q += " ORDER BY v.date DESC, v.transaction_id LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]
