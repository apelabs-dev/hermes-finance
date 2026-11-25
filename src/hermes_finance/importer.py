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
    matches is filed under Other, with two exceptions for money coming in. An OFX row whose TRNTYPE says what it is
    (OFX_INCOME_HINTS: a direct deposit, payroll or interest) is saved with the Plaid income code that type means,
    and a credit-card statement's own payment line (a CREDIT or PAYMENT row naming a payment) as
    LOAN_PAYMENTS / LOAN_PAYMENTS_CREDIT_CARD_PAYMENT, so it files under Transfers. A CSV or PDF row has no
    TRNTYPE at all, so its description decides by the same rule a synced row with no provider category gets
    (classify.inflow_hint: a card payment, a refund / return / reversal, a tax refund, a transfer in, else income),
    which is why a payroll deposit from a statement files under Income rather than counting as negative spending
    (QA 0.5 F1/F2). Only a row whose import is recorded as csv or pdf gets that hint: an OFX row whose TRNTYPE is
    missing or blank is still an OFX row and stays Other. A bank statement's generic OFX CREDIT and a refund stay
    Other. A rule still wins over every hint, and an AI or user decision is never touched.
  * the account's balance is what you give it (an OFX file's ledger balance is offered): transactions never move a
    manual account's balance, so no total changes until rows are imported, and then only spending does.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import PurePath
from typing import Any

from . import classify, importfile, manual
from .db import IMPORT_ITEM, display_currency, now

TARGET_KINDS = ("bank", "credit_card")
MAX_ROWS = 20_000
PREVIEW_ROWS = 200    # a PDF preview's row list: the check screen shows these, the total is counted separately

# OFX TRNTYPE -> the Plaid personal_finance_category (primary, detailed) a money-in row of that type is saved with.
# classify.apply() files it like a synced row: a matching rule first, else plaid_category_map (INCOME -> Income).
# The detailed codes keep pay, interest and other income apart, so a finer income category later is one map row each.
OFX_INCOME_HINTS = {
    "DIRECTDEP": ("INCOME", "INCOME_WAGES"),
    "DEP": ("INCOME", "INCOME_WAGES"),
    "PAYROLL": ("INCOME", "INCOME_WAGES"),
    "INT": ("INCOME", "INCOME_INTEREST_EARNED"),
}
# Not CREDIT: OFX uses it for any money in, so a card payment or a refund would count as income. A CREDIT row stays
# unhinted: Other (Needs review), or what a rule says. The exception is a card statement's own payment line
# (_pfc_hint): it is a transfer, not spending or income.
CARD_PAYMENT_HINT = classify.CARD_PAYMENT_HINT   # classify holds the three hints: simplefin_sync can't import this module
_CARD_PAYMENT_WORDS = classify.CARD_PAYMENT_WORDS  # (importfile needs ofxtools, which Hermes's own Python doesn't have)


class ImportRefused(ValueError):
    def __init__(self, message: str, field: str | None = None, code: str | None = None, extra: dict | None = None):
        super().__init__(message)
        self.field = field
        self.code = code
        self.extra = extra or {}


def _chosen_currency(conn: sqlite3.Connection, data: dict[str, Any]) -> str:
    """The picker's default: the target manual account's currency when one is chosen, else the display currency."""
    chosen = data.get("targets") or {}
    if isinstance(chosen, dict):
        for choice in chosen.values():
            if isinstance(choice, dict) and isinstance(choice.get("account_id"), int):
                row = conn.execute("SELECT currency FROM manual_accounts WHERE id = ?",
                                   (choice["account_id"],)).fetchone()
                if row is not None and (row["currency"] or "").strip():
                    return row["currency"].strip().upper()
    return display_currency(conn)


def _pdf_info(st) -> dict[str, Any]:
    return {"reason": None, "balance_mode": st.balance_mode, "period": [st.period[0].isoformat(),
                                                                        st.period[1].isoformat()],
            "opening": str(st.opening), "closing": str(st.closing), "currency": st.currency,
            "template": st.template, "proposal": st.proposal, "ai_used": st.ai_used, "detail": st.detail}


def _pdf_year(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1000 <= value <= 9999:
        raise ImportRefused("The year must be four digits, such as 2024.", "year")
    return value


def _pdf_password(data: dict[str, Any]) -> str | None:
    password = data.get("password")
    if password is not None and not isinstance(password, str):
        raise ImportRefused("The password must be text.", "password")
    return password or None


def _ai_view(filename: str, raw: bytes, data: dict[str, Any]) -> dict[str, Any]:
    """ai_view on a PDF preview: every prompt an AI layout proposal could send, word for word, for the privacy screen
    to show before the person presses Send. Worked out here; nothing is parsed further, written or sent."""
    from . import pdfimport
    try:
        prompts = pdfimport.ai_outgoing(raw, password=_pdf_password(data))
    except pdfimport.PdfRefused as e:
        raise ImportRefused(e.message, e.field, e.code, e.extra) from None
    return {"kind": "pdf", "filename": filename, "ai_view": {"prompts": prompts}}


def _parse_pdf(conn: sqlite3.Connection, filename: str, raw: bytes, data: dict[str, Any], today: date,
               commit: bool = False) -> dict[str, Any]:
    """One statement PDF through pdfimport (before sniff: a PDF is not a CSV/OFX file). Only a preview may ask the
    model (ai_template); a commit sends the previewed proposal back as `template` with template_source 'ai'."""
    from . import pdfimport

    password = _pdf_password(data)
    if not isinstance(data.get("ai_template", False), bool):
        raise ImportRefused("ai_template must be true or false", "ai_template")
    template = data.get("template")
    if template is not None and not isinstance(template, dict):
        raise ImportRefused(pdfimport.REASONS["template_invalid"], "file", "template_invalid")
    template_source = data.get("template_source") or "user"
    if template_source not in ("user", "ai"):
        raise ImportRefused("template_source must be user or ai", "template_source")
    year = _pdf_year(data.get("year"))
    try:
        st = pdfimport.parse_statement(conn, raw, password=password,
                                       ai=not commit and data.get("ai_template") is True,
                                       template=template, template_source=template_source,
                                       opening=data.get("opening"), closing=data.get("closing"), year=year,
                                       today=today)
    except pdfimport.PdfRefused as e:
        raise ImportRefused(e.message, e.field, e.code, e.extra) from None
    label = _stem(filename)
    group = {"label": label, "kind": "bank", "subtype": st.subtype, "balance": float(st.closing),
             "balance_date": st.period[1].isoformat(), "institution": None, "rows": st.rows, "card": False,
             "currency": None}
    return {"kind": "pdf", "groups": [group], "pdf": _pdf_info(st)}


def _parse(conn: sqlite3.Connection, filename: str, raw: bytes, data: dict[str, Any], today: date,
           commit: bool = False) -> dict[str, Any]:
    """The file read with the page's choices (a CSV's column mapping, date format and sign; guesses when absent)."""
    if importfile.is_pdf(raw):
        parsed = _parse_pdf(conn, filename, raw, data, today, commit)
        _check_max_rows(sum(len(g["rows"]) for g in parsed["groups"]))
        return parsed
    try:
        kind = importfile.sniff(filename, raw)
        if kind == "csv":
            pv = importfile.csv_preview(raw)
            mapping = data.get("mapping") or pv.guess
            if not isinstance(mapping, dict):
                raise ImportRefused("mapping must name the columns", "mapping")
            mapping = {k: (int(v) if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < len(pv.headers) else None)
                       for k, v in mapping.items() if k in pv.guess}
            currency = data.get("currency") or _chosen_currency(conn, data)
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z0-9]{1,12}", currency.strip()):
                raise ImportRefused("currency must be a 3-letter code such as USD or EUR", "currency")
            currency = currency.strip().upper()
            date_format = data.get("date_format") or pv.date_format
            if date_format not in (pv.date_formats or [date_format]):
                raise ImportRefused("that date format doesn't read every date in the file", "date_format")
            sign = data.get("sign") or pv.sign
            if sign not in ("minus_out", "minus_in", "split"):
                raise ImportRefused("sign must be minus_out, minus_in or split", "sign")
            if not date_format:
                raise ImportRefused("The date column couldn't be read: pick the column with the dates.", "date")
            rows = importfile.csv_rows(raw, mapping, date_format, sign, today=today, currency=currency)
            label = _stem(filename)
            groups: dict[str, list] = defaultdict(list)
            for r in rows:
                groups[r.account or label].append(r)
            out_groups = [{"label": k, "kind": "bank", "subtype": None, "balance": None, "balance_date": None,
                           "institution": None, "rows": v, "card": False, "currency": currency}
                          for k, v in groups.items()]
            info = {"headers": pv.headers, "sample": pv.sample, "mapping": mapping, "guess": pv.guess,
                    "date_format": date_format, "date_formats": pv.date_formats, "sign": sign, "delimiter": pv.delimiter,
                    "currency": currency, "currencies": pv.currencies}
        else:
            stmts = importfile.ofx_statements(raw)
            out_groups = [{"label": s.account_label, "kind": s.kind, "subtype": s.subtype, "balance": s.balance,
                           "balance_date": s.balance_date, "institution": getattr(s, "institution", None),
                           "rows": s.rows, "card": s.card, "currency": s.currency}
                          for s in stmts]
            info = {"currency": next((s.currency for s in stmts if s.currency), display_currency(conn)),
                    "currencies": sorted({r.currency for s in stmts for r in s.rows if r.currency})}
    except importfile.ImportFileError as e:
        raise ImportRefused(str(e), getattr(e, "field", None) or "file") from None
    total = sum(len(g["rows"]) for g in out_groups)
    if not total:
        raise ImportRefused("There are no transactions in this file.", "file")
    _check_max_rows(total)
    return {"kind": kind, "groups": out_groups, **info}


def _check_max_rows(total: int) -> None:
    """One refusal for every file kind (CSV, OFX, PDF) over MAX_ROWS."""
    if total > MAX_ROWS:
        raise ImportRefused(f"This file has {total:,} rows; import at most {MAX_ROWS:,} at a time.", "file")


def _stem(filename: str) -> str:
    stem = PurePath(filename or "file").stem.replace("_", " ").replace("-", " ").strip()
    return (stem[:1].upper() + stem[1:])[:60] if stem else "Imported account"


def targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The manual accounts an import may go into: bank accounts and cards, not archived."""
    return [{"id": r["id"], "name": r["name"], "kind": r["kind"], "institution": r["institution"],
             "currency": r["currency"]}
            for r in conn.execute("SELECT id, name, kind, institution, currency FROM manual_accounts WHERE archived_at "
                                  "IS NULL AND kind IN ('bank', 'credit_card') ORDER BY name COLLATE NOCASE")]


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
    if data.get("ai_view") is True and importfile.is_pdf(raw):
        return _ai_view(filename, raw, data)
    p = _parse(conn, filename, raw, data, today)
    chosen = data.get("targets") or {}
    groups = []
    for g in p["groups"]:
        t = _target(conn, chosen.get(g["label"]) if isinstance(chosen, dict) else None, g)
        new, skipped = _split(conn, t.get("account_id"), g["rows"])
        dates = sorted(r.date for r in g["rows"])
        groups.append({"label": g["label"], "kind": g["kind"], "subtype": g["subtype"], "rows": len(g["rows"]),
                       "new": len(new), "skipped": skipped, "first": dates[0], "last": dates[-1], "target": t,
                       "balance": g["balance"], "balance_date": g["balance_date"], "institution": g["institution"],
                       "currency": g["currency"]})
    rows = [r for g in p["groups"] for r in g["rows"]]
    dates = sorted(r.date for r in rows)
    out = {k: v for k, v in p.items() if k != "groups"}
    res = {**out, "filename": filename, "rows": len(rows), "first": dates[0], "last": dates[-1], "groups": groups,
           "accounts": targets(conn), "new": sum(g["new"] for g in groups), "skipped": sum(g["skipped"] for g in groups)}
    if p["kind"] == "pdf":
        # What the check screen lists under the proof: every row the replay accepted (the PDF branch has one group),
        # and the two totals across all of them (the list may be capped, the sums never are).
        res["preview_rows"] = [{"date": r.date, "description": r.description, "amount": r.amount}
                               for r in rows[:PREVIEW_ROWS]]
        res["preview_rows_total"] = len(rows)
        res["preview_in"] = float(sum(-r.amount for r in rows if r.amount < 0))
        res["preview_out"] = float(sum(r.amount for r in rows if r.amount > 0))
        # A mapping sent to be checked: the saved layout teaching it would replace (None: nothing would be replaced),
        # so the screen can say so before Save and import; the commit then has to send replace_template: true.
        fp = ((p.get("pdf") or {}).get("detail") or {}).get("fingerprint")
        res["pdf"] = {**(p.get("pdf") or {}), "replaces": _replaces(conn, fp, data.get("template"))}
    return res


def _new_account(conn: sqlite3.Connection, spec: dict[str, Any], group: dict[str, Any], today: date) -> int:
    kind = spec["kind"]
    balance = spec.get("balance", group["balance"] if group["balance"] is not None else 0.0)
    data = {"kind": kind, "name": spec.get("name") or group["label"],
            "institution": spec.get("institution") or group["institution"] or "Imported file",
            "balance": abs(balance) if isinstance(balance, (int, float)) and not isinstance(balance, bool) else balance,
            "balance_as_of": spec.get("balance_as_of") or group["balance_date"] or today.isoformat(),
            "currency": spec.get("currency") or group.get("currency") or display_currency(conn)}
    if kind == "bank":
        data["subtype"] = spec.get("subtype") or group["subtype"] or "checking"
    else:
        data["credit_limit"] = spec.get("credit_limit")
    if data["balance_as_of"] > today.isoformat():
        data["balance_as_of"] = today.isoformat()
    try:
        return manual.insert(conn, data, today)   # no commit: commit() writes the account with its rows
    except manual.ManualError as e:
        raise ImportRefused(f"{group['label']}: {e}", "targets") from None


def _tid(account_id: int, row, n: int) -> str:
    basis = f"{account_id}|{row.fitid or ''}|{row.date}|{row.amount:.2f}|{importfile.normalize_description(row.description)}|{n}"
    return "imp_" + hashlib.sha256(basis.encode()).hexdigest()[:24]


def _pfc_hint(row, card: bool) -> tuple[str | None, str | None]:
    """(pfc_primary, pfc_detailed) for an OFX row that is money IN: the app's amount < 0, i.e. the file's TRNAMT > 0.
    First its TRNTYPE's income code (OFX_INCOME_HINTS); then, on a card statement only, a CREDIT / PAYMENT row whose
    description names a payment ("PAYMENT THANK YOU"): the card's own payment is a transfer, not income or spending.
    A DEP / INT ... row with TRNAMT <= 0 (money out) gets none, nor does a CSV row (no TRNTYPE)."""
    if row.amount >= 0:
        return None, None
    hint = OFX_INCOME_HINTS.get(row.trntype or "")
    if hint:
        return hint
    if card and row.trntype in ("CREDIT", "PAYMENT") and _CARD_PAYMENT_WORDS.search(row.description or ""):
        return CARD_PAYMENT_HINT
    return None, None


def _raw_json(filename: str, row) -> str:
    raw = {"file": filename[:200], "line": row.line, "fitid": row.fitid}
    if row.trntype:
        raw["trntype"] = row.trntype
    return json.dumps(raw)


def commit(conn: sqlite3.Connection, filename: str, raw: bytes, data: dict[str, Any], today: date) -> dict[str, Any]:
    """Import the file with these choices: new accounts first, then every new row (one transaction; an OFX money-in row
    with its TRNTYPE's income code, or a card payment's transfer code, _pfc_hint), then the rules and the
    Plaid-category defaults (classify.apply, which also gives a CSV / PDF money-in row its code from its
    description). Returns what was added and skipped, per account. A commit never calls the
    model: ai_template is refused (ai_on_commit); a previewed AI proposal comes back as `template` with
    template_source 'ai', is validated and must reconcile again locally, and is taught with source 'ai'."""
    if data.get("ai_template") is True:
        raise ImportRefused("A commit doesn't ask the AI again. Send the proposal the preview returned as the template "
                            "(template_source \"ai\").", "ai_template", "ai_on_commit")
    p = _parse(conn, filename, raw, data, today, commit=True)
    chosen = data.get("targets") or {}
    plan = [(g, _target(conn, chosen.get(g["label"]) if isinstance(chosen, dict) else None, g)) for g in p["groups"]]
    for g, t in plan:  # everything checked before anything is written
        if "new" in t and t["new"]["kind"] == "credit_card" and not t["new"].get("credit_limit"):
            raise ImportRefused(f"{g['label']}: a card needs its credit limit", "credit_limit")
    taught = _teach_plan(conn, p, data) if p["kind"] == "pdf" and data.get("teach") is True else None
    ts = now()
    results = []
    # One transaction for every write: new accounts, the imports rows, the transactions and a taught PDF template
    # land together, so an error anywhere (a template save included) leaves no account or row behind.
    with conn:
        for g, t in plan:
            account_id = t.get("account_id") or _new_account(conn, t["new"], g, today)
            new, skipped = _split(conn, t.get("account_id"), g["rows"])
            account = conn.execute("SELECT name, currency FROM manual_accounts WHERE id = ?", (account_id,)).fetchone()
            name = account["name"]
            account_currency = (account["currency"] or "").strip().upper() or display_currency(conn)
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
                     round(row.amount, 2), (row.currency or g.get("currency") or account_currency).strip().upper(),
                     *_pfc_hint(row, g["card"]), _raw_json(filename, row), ts, ts, import_id, row.fitid))
            if taught is not None and taught["saved"]:
                # the template is saved in the same transaction as the rows, only after the parse/replay that preview ran
                from .pdfimport import templates
                templates.save(conn, taught["fp"], taught["ref"]["config"], taught["ref"]["source"])
            results.append({"label": g["label"], "account": {"id": account_id, "name": name, "currency": account_currency},
                            "new": len(new), "skipped": skipped})
    classify.apply(conn)
    dates = sorted(r.date for g, _ in plan for r in g["rows"])
    out = {"filename": filename, "kind": p["kind"], "new": sum(r["new"] for r in results),
           "skipped": sum(r["skipped"] for r in results), "accounts": results, "first": dates[0], "last": dates[-1]}
    if taught is not None:   # what teach=true did with the layout: saved (replacing one or not), or why not
        out["template"] = {k: v for k, v in taught.items() if k not in ("fp", "ref")}
    return out


def _typed_balances(data: dict[str, Any]) -> bool:
    return any(data.get(k) not in (None, "") for k in ("opening", "closing"))


def _teach_plan(conn: sqlite3.Connection, p: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """What teach=true will do with the layout, decided before anything is written. Balances typed by hand save
    nothing (next month's statement can't be read without typing them again); a different layout already saved for
    these statements is replaced only when the request says replace_template: true, never silently."""
    pdf = p.get("pdf") or {}
    fp = (pdf.get("detail") or {}).get("fingerprint")
    ref = pdf.get("template") or ({"source": "ai", "config": pdf.get("proposal")} if pdf.get("proposal") else None)
    if not ref or not ref.get("config") or not fp:
        return {"saved": False, "reason": "nothing_to_save"}
    if _typed_balances(data):
        return {"saved": False, "reason": "typed_balances"}
    replaced = _replaces(conn, fp, ref["config"]) is not None
    if replaced and data.get("replace_template") is not True:
        raise ImportRefused("A layout is already saved for statements like this one. To replace it, teach it again "
                            "with replace_template set to true.", "teach", "template_exists")
    return {"saved": True, "replaced": replaced, "source": ref["source"], "fp": fp, "ref": ref}


def _replaces(conn: sqlite3.Connection, fp: dict | None, config: Any) -> dict[str, Any] | None:
    """The saved layout a teach with this config would replace ({source, created_at}), or None (none saved, the
    same config is saved, or the config doesn't validate: then nothing would be saved at all)."""
    from .pdfimport import templates
    if not fp or not isinstance(config, dict):
        return None
    found = templates.find(conn, fp)
    try:
        if found is None or found["config"] == templates.clean_config(config):
            return None
    except ValueError:
        return None
    row = conn.execute("SELECT source, created_at FROM pdf_templates WHERE id = ?", (found["id"],)).fetchone()
    return {"source": row[0], "created_at": row[1]} if row else None
