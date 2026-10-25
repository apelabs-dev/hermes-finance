"""Import a bank's file (TASK-18, Add account -> Import a file): CSV, OFX or QFX into a manual account.

importfile.py reads the file (type sniffing, CSV columns / dates / sign, OFX statements); this module shows what an
import would do (preview) and does it (commit):

  * every group of rows (a CSV's Account column, an OFX statement, else the whole file) goes to one manual account:
    a new one ("add as account: <name>", a bank account or a card) or an existing manual bank account / card. Never a
    Plaid account: an imported row's account_id is 'manual:<id>' and its item_id 'import' (no items row, no slot).
  * a row already there is skipped: the same FITID (OFX), else the same (date, amount, normalized description) in
    that account. Rows are matched one for one, so two identical coffees on one day in the file and one already
    saved import one.
  * imported rows get source 'import' and go through the same rules and classification as synced ones
    (classify.apply; the AI queue picks up what no rule files). A file has no Plaid category, so a row no rule
    matches is filed under Other, except money coming in on an OFX row whose TRNTYPE says what it is (OFX_INCOME_HINTS:
    a direct deposit, payroll or interest; not a generic CREDIT, which is also card payments and refunds): it is saved with the Plaid income code that type means, so it
    files under Income like a synced paycheck, on every later classify.apply too. A rule still wins over the hint, and
    an AI or user decision is never touched.
  * the account's balance is what you give it (an OFX file's ledger balance is offered): transactions never move a
    manual account's balance, so no total changes until rows are imported, and then only spending does.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import PurePath
from typing import Any

from . import classify, importfile, manual
from .db import IMPORT_ITEM, now

TARGET_KINDS = ("bank", "credit_card")
MAX_ROWS = 20_000

# OFX TRNTYPE -> the Plaid personal_finance_category (primary, detailed) a money-in row of that type is saved with.
# classify.apply() files it like a synced row: a matching rule first, else plaid_category_map (INCOME -> Income).
# The detailed codes keep pay, interest and other income apart, so a finer income category later is one map row each.
OFX_INCOME_HINTS = {
    "DIRECTDEP": ("INCOME", "INCOME_WAGES"),
    "DEP": ("INCOME", "INCOME_WAGES"),
    "PAYROLL": ("INCOME", "INCOME_WAGES"),
    "INT": ("INCOME", "INCOME_INTEREST_EARNED"),
}
# Not CREDIT: OFX uses it for any money in, so a card payment ("PAYMENT THANK YOU") or a
# refund would count as income. A CREDIT row stays unhinted: Other (Needs review), or what a rule says.


class ImportRefused(ValueError):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


def _parse(filename: str, raw: bytes, data: dict[str, Any], today: date) -> dict[str, Any]:
    """The file read with the page's choices (a CSV's column mapping, date format and sign; guesses when absent)."""
    try:
        kind = importfile.sniff(filename, raw)
        if kind == "csv":
            pv = importfile.csv_preview(raw)
            mapping = data.get("mapping") or pv.guess
            if not isinstance(mapping, dict):
                raise ImportRefused("mapping must name the columns", "mapping")
            mapping = {k: (int(v) if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < len(pv.headers) else None)
                       for k, v in mapping.items() if k in pv.guess}
            date_format = data.get("date_format") or pv.date_format
            if date_format not in (pv.date_formats or [date_format]):
                raise ImportRefused("that date format doesn't read every date in the file", "date_format")
            sign = data.get("sign") or pv.sign
            if sign not in ("minus_out", "minus_in", "split"):
                raise ImportRefused("sign must be minus_out, minus_in or split", "sign")
            if not date_format:
                raise ImportRefused("The date column couldn't be read: pick the column with the dates.", "date")
            rows = importfile.csv_rows(raw, mapping, date_format, sign, today=today)
            label = _stem(filename)
            groups: dict[str, list] = defaultdict(list)
            for r in rows:
                groups[r.account or label].append(r)
            out_groups = [{"label": k, "kind": "bank", "subtype": None, "balance": None, "balance_date": None,
                           "institution": None, "rows": v} for k, v in groups.items()]
            info = {"headers": pv.headers, "sample": pv.sample, "mapping": mapping, "guess": pv.guess,
                    "date_format": date_format, "date_formats": pv.date_formats, "sign": sign, "delimiter": pv.delimiter}
        else:
            stmts = importfile.ofx_statements(raw)
            out_groups = [{"label": s.account_label, "kind": s.kind, "subtype": s.subtype, "balance": s.balance,
                           "balance_date": s.balance_date, "institution": getattr(s, "institution", None), "rows": s.rows}
                          for s in stmts]
            info = {}
    except importfile.ImportFileError as e:
        raise ImportRefused(str(e), getattr(e, "field", None) or "file") from None
    total = sum(len(g["rows"]) for g in out_groups)
    if not total:
        raise ImportRefused("There are no transactions in this file.", "file")
    if total > MAX_ROWS:
        raise ImportRefused(f"This file has {total:,} rows; import at most {MAX_ROWS:,} at a time.", "file")
    return {"kind": kind, "groups": out_groups, **info}


def _stem(filename: str) -> str:
    stem = PurePath(filename or "file").stem.replace("_", " ").replace("-", " ").strip()
    return (stem[:1].upper() + stem[1:])[:60] if stem else "Imported account"


def targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The manual accounts an import may go into: bank accounts and cards, not archived."""
    return [{"id": r["id"], "name": r["name"], "kind": r["kind"], "institution": r["institution"]}
            for r in conn.execute("SELECT id, name, kind, institution FROM manual_accounts WHERE archived_at IS NULL "
                                  "AND kind IN ('bank', 'credit_card') ORDER BY name COLLATE NOCASE")]


def _key(row) -> tuple[str, float, str]:
    return row.date, round(row.amount, 2), importfile.normalize_description(row.description)


def _split(conn: sqlite3.Connection, account_id: int | None, rows: list) -> tuple[list, int]:
    """(rows to add, how many are already there) for rows going into manual account `account_id` (None: a new one)."""
    if account_id is None:
        return list(rows), 0
    have: Counter = Counter()
    fitids = set()
    for r in conn.execute("SELECT date, amount, name, fitid FROM transactions WHERE account_id = ? AND removed = 0",
                          (f"manual:{account_id}",)):
        have[(r["date"], round(r["amount"], 2), importfile.normalize_description(r["name"] or ""))] += 1
        if r["fitid"]:
            fitids.add(r["fitid"])
    new, skipped = [], 0
    for row in rows:
        k = _key(row)
        if row.fitid and row.fitid in fitids:
            skipped += 1
            if have[k] > 0:
                have[k] -= 1
        elif have[k] > 0:
            have[k] -= 1
            skipped += 1
        else:
            new.append(row)
            if row.fitid:
                fitids.add(row.fitid)
    return new, skipped


def _target(conn: sqlite3.Connection, choice: Any, group: dict[str, Any]) -> dict[str, Any]:
    """{"account_id": N} (an existing manual bank account / card) or {"new": {name, kind, balance?, credit_limit?,
    institution?}}; the default is a new account named after the group, unless a manual account already has that
    name (then that one)."""
    if choice is None:
        same = next((t for t in targets(conn) if t["name"].casefold() == group["label"].casefold()), None)
        return {"account_id": same["id"]} if same else {"new": {"name": group["label"], "kind": group["kind"]}}
    if not isinstance(choice, dict):
        raise ImportRefused("each account needs a target", "targets")
    if "account_id" in choice:
        ids = {t["id"]: t for t in targets(conn)}
        aid = choice["account_id"]
        if isinstance(aid, bool) or not isinstance(aid, int) or aid not in ids:
            raise ImportRefused("Import only goes into a manual bank account or card that isn't archived.", "targets")
        return {"account_id": aid}
    new = choice.get("new")
    if not isinstance(new, dict) or new.get("kind") not in TARGET_KINDS:
        raise ImportRefused("a new account is a bank account or a card", "targets")
    return {"new": new}


def preview(conn: sqlite3.Connection, filename: str, raw: bytes, data: dict[str, Any], today: date) -> dict[str, Any]:
    """What the import would do with these choices, changing nothing."""
    p = _parse(filename, raw, data, today)
    chosen = data.get("targets") or {}
    groups = []
    for g in p["groups"]:
        t = _target(conn, chosen.get(g["label"]) if isinstance(chosen, dict) else None, g)
        new, skipped = _split(conn, t.get("account_id"), g["rows"])
        dates = sorted(r.date for r in g["rows"])
        groups.append({"label": g["label"], "kind": g["kind"], "subtype": g["subtype"], "rows": len(g["rows"]),
                       "new": len(new), "skipped": skipped, "first": dates[0], "last": dates[-1], "target": t,
                       "balance": g["balance"], "balance_date": g["balance_date"], "institution": g["institution"]})
    rows = [r for g in p["groups"] for r in g["rows"]]
    dates = sorted(r.date for r in rows)
    out = {k: v for k, v in p.items() if k != "groups"}
    return {**out, "filename": filename, "rows": len(rows), "first": dates[0], "last": dates[-1], "groups": groups,
            "accounts": targets(conn), "new": sum(g["new"] for g in groups), "skipped": sum(g["skipped"] for g in groups)}


def _new_account(conn: sqlite3.Connection, spec: dict[str, Any], group: dict[str, Any], today: date) -> int:
    kind = spec["kind"]
    balance = spec.get("balance", group["balance"] if group["balance"] is not None else 0.0)
    data = {"kind": kind, "name": spec.get("name") or group["label"],
            "institution": spec.get("institution") or group["institution"] or "Imported file",
            "balance": abs(balance) if isinstance(balance, (int, float)) and not isinstance(balance, bool) else balance,
            "balance_as_of": spec.get("balance_as_of") or group["balance_date"] or today.isoformat()}
    if kind == "bank":
        data["subtype"] = spec.get("subtype") or group["subtype"] or "checking"
    else:
        data["credit_limit"] = spec.get("credit_limit")
    if data["balance_as_of"] > today.isoformat():
        data["balance_as_of"] = today.isoformat()
    try:
        return manual.create(conn, data, today)["id"]
    except manual.ManualError as e:
        raise ImportRefused(f"{group['label']}: {e}", "targets") from None


def _tid(account_id: int, row, n: int) -> str:
    basis = f"{account_id}|{row.fitid or ''}|{row.date}|{row.amount:.2f}|{importfile.normalize_description(row.description)}|{n}"
    return "imp_" + hashlib.sha256(basis.encode()).hexdigest()[:24]


def _pfc_hint(row) -> tuple[str | None, str | None]:
    """(pfc_primary, pfc_detailed) for an OFX row whose TRNTYPE is in OFX_INCOME_HINTS and that is money IN: the app's
    amount < 0, i.e. the file's TRNAMT > 0. A DEP / INT ... row with TRNAMT <= 0 (money out) gets none, nor
    does a CSV row (no TRNTYPE)."""
    if row.amount >= 0:
        return None, None
    return OFX_INCOME_HINTS.get(row.trntype or "", (None, None))


def _raw_json(filename: str, row) -> str:
    raw = {"file": filename[:200], "line": row.line, "fitid": row.fitid}
    if row.trntype:
        raw["trntype"] = row.trntype
    return json.dumps(raw)


def commit(conn: sqlite3.Connection, filename: str, raw: bytes, data: dict[str, Any], today: date) -> dict[str, Any]:
    """Import the file with these choices: new accounts first, then every new row (one transaction; an OFX money-in row
    with its TRNTYPE's income code, _pfc_hint), then the rules and the Plaid-category defaults (classify.apply). Returns
    what was added and skipped, per account."""
    p = _parse(filename, raw, data, today)
    chosen = data.get("targets") or {}
    plan = [(g, _target(conn, chosen.get(g["label"]) if isinstance(chosen, dict) else None, g)) for g in p["groups"]]
    for g, t in plan:  # everything checked before anything is written
        if "new" in t and t["new"]["kind"] == "credit_card" and not t["new"].get("credit_limit"):
            raise ImportRefused(f"{g['label']}: a card needs its credit limit", "credit_limit")
    ts = now()
    results = []
    for g, t in plan:
        account_id = t.get("account_id") or _new_account(conn, t["new"], g, today)
        new, skipped = _split(conn, t.get("account_id"), g["rows"])
        name = conn.execute("SELECT name FROM manual_accounts WHERE id = ?", (account_id,)).fetchone()["name"]
        with conn:
            cur = conn.execute("INSERT INTO imports (created_at, filename, kind, manual_account_id, rows_new, rows_skipped) "
                               "VALUES (?, ?, ?, ?, ?, ?)", (ts, filename[:200], p["kind"], account_id, len(new), skipped))
            import_id = cur.lastrowid
            seen: Counter = Counter()
            for row in new:
                k = (row.fitid,) + _key(row)
                seen[k] += 1
                conn.execute(
                    "INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, iso_currency, pending, "
                    "pfc_primary, pfc_detailed, raw_json, removed, first_seen_at, updated_at, source, import_id, fitid) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, 'import', ?, ?)",
                    (_tid(account_id, row, seen[k]), f"manual:{account_id}", IMPORT_ITEM, row.date, row.description[:300],
                     round(row.amount, 2), None, *_pfc_hint(row), _raw_json(filename, row), ts, ts, import_id, row.fitid))
        results.append({"label": g["label"], "account": {"id": account_id, "name": name}, "new": len(new), "skipped": skipped})
    classify.apply(conn)
    dates = sorted(r.date for g, _ in plan for r in g["rows"])
    return {"filename": filename, "kind": p["kind"], "new": sum(r["new"] for r in results),
            "skipped": sum(r["skipped"] for r in results), "accounts": results, "first": dates[0], "last": dates[-1]}
