"""Local PDF bank-statement import: the public API and the reason codes.

One deposit account per PDF, with a text layer and printed balances. The cascade is generic parser -> a saved local
template whose masked layout fingerprint matches -> an opt-in AI template proposal from a masked layout view. Rows
reach the preview only when they replay to the printed balances; otherwise a PdfRefused names why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from .. import importfile
from . import guard, layout, replay as replay_mod, templates, words

REASONS = {
    "pdf_unreadable": "This file couldn't be read as a PDF. It may be damaged; download it again from your bank.",
    "pdf_too_complex": "This PDF is too large or too complex to read safely. Download a shorter date range from your "
                       "bank, or import CSV, OFX or QFX.",
    "password_required": "This PDF is password-protected. Enter the password your bank uses for statements.",
    "password_wrong": "That password didn't open the PDF. Check it and try again.",
    "no_text_layer": "This PDF is a scanned image, so there's no text to read. Download the statement as CSV, OFX or "
                     "QFX instead.",
    "credit_card": "This looks like a credit-card statement. PDF import covers one bank account with printed "
                   "balances; download a card statement as CSV, OFX or QFX.",
    "multi_account": "This PDF has more than one account. Import a statement with one account, or download CSV, OFX "
                     "or QFX.",
    "no_header": "The transaction table in this PDF wasn't recognized.",
    "no_rows": "No transactions were found in this PDF.",
    "no_balances": "This PDF doesn't print balances, so the transactions can't be checked against the statement. "
                   "Nothing is imported.",
    "does_not_reconcile": "The transactions in this PDF don't add up to the balances it prints, so nothing is "
                          "imported.",
    "unreadable_date": "A date in this PDF couldn't be read, so its transactions can't be placed on the timeline.",
    "no_period": "This PDF doesn't print a statement period or a year, so its dates can't be placed on the timeline.",
    "template_invalid": "That saved layout doesn't fit this statement.",
    "ai_unavailable": "The AI layout proposal isn't available. Turn on a local Claude Code or Codex brain in "
                      "Settings → AI provider, or import without it.",
    "ai_failed": "The AI layout proposal didn't produce a mapping that adds up. Nothing was imported.",
    "missing_extra": "PDF import needs an optional part that isn't installed. Install it with "
                     "`pip install 'hermes-finance[pdf]'` (or `uv pip install 'hermes-finance[pdf]'`), then try again.",
}


class PdfRefused(ValueError):
    """A statement that can't be imported, with a public reason code and a plain message for the user."""

    def __init__(self, code: str, message: str | None = None, field: str = "file", extra: dict | None = None,
                 detail=None):
        self.code = code
        self.message = message or REASONS.get(code, "This PDF couldn't be imported.")
        self.field = field
        self.extra = dict(extra or {})
        self.detail = detail if detail is not None else self.extra.get("detail")
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


@dataclass
class Statement:
    rows: list[importfile.Row]              # app sign: > 0 money OUT
    period: tuple[date, date]
    opening: Decimal
    closing: Decimal
    balance_mode: str                       # "running" | "totals"
    template: dict | None                   # {"id", "source", "config"} used (validated, no numbers)
    proposal: dict | None                   # a candidate the user may confirm on commit
    ai_used: bool
    currency: str | None                    # an ISO code the statement prints (display only)
    subtype: str | None                     # "checking" | "savings" when obvious
    detail: dict


def available() -> bool:
    """Whether the optional PDF extra (pdfplumber) is installed."""
    return words.available()


# ---------------------------------------------------------------- helpers

_CURRENCY_CODE = re.compile(r"(?<![A-Za-z])(USD|EUR|CAD|AUD|GBP|CHF|INR|SGD|HKD|NZD|JPY)(?![A-Za-z])")
_CURRENCY_SYMBOL = (("€", "EUR"), ("£", "GBP"), ("₹", "INR"))
_SUBTYPE_CHECKING = re.compile(r"\b(checking|chequing|current account)\b", re.I)
_SUBTYPE_SAVINGS = re.compile(r"\b(savings?|sparen|épargne)\b", re.I)

_PRECEDENCE = ("multi_account", "no_header", "no_rows", "no_balances", "does_not_reconcile", "no_period",
               "unreadable_date", "template_invalid")

_FIXABLE = {"no_header", "unreadable_date", "no_period", "does_not_reconcile", "no_balances"}
_HARD = {"missing_extra", "pdf_unreadable", "pdf_too_complex", "password_required", "password_wrong", "no_text_layer",
         "credit_card", "multi_account", "template_invalid", "too_big"}

_NO_YEAR_MESSAGE = ("This statement doesn't print a year, so its dates can't be placed on the timeline. "
                    "Type the year the statement covers to import it.")


def _decimal(value, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(name)
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError(name) from None


def _code(problems: list[str], parsed: layout.Parsed | None, verdict: replay_mod.Replay | None) -> str | None:
    codes: list[str] = []
    for p in problems:
        code = {
            "multiple_tables": "multi_account", "opening_conflict": "multi_account",
            "closing_conflict": "multi_account", "no_header": "no_header",
            "no_rows": "no_rows", "row_without_amount": "does_not_reconcile",
            "unassigned_money": "does_not_reconcile", "ambiguous_amounts": "does_not_reconcile",
            "ambiguous_balance": "does_not_reconcile", "ambiguous_sign": "does_not_reconcile",
            "first_row_without_date": "unreadable_date", "unreadable_date": "unreadable_date",
            "row_without_date": "unreadable_date", "ambiguous_date_order": "unreadable_date",
            "date_outside_period": "unreadable_date", "no_period": "no_period",
        }.get(p)
        if code:
            codes.append(code)
    if parsed is not None and not parsed.rows and "no_header" not in problems:
        codes.append("no_rows")
    if verdict is not None and not verdict.accepted and verdict.reason:
        if not (verdict.reason == "unreadable_date" and "no_period" in problems):
            codes.append(verdict.reason)
    for code in _PRECEDENCE:
        if code in codes:
            return code
    return codes[0] if codes else None


def _currency(lines) -> str | None:
    for ln in lines[:60]:
        m = _CURRENCY_CODE.search(ln.text)
        if m:
            return m.group(1)
    for ln in lines[:60]:
        for symbol, code in _CURRENCY_SYMBOL:
            if symbol in ln.text:
                return code
    return None


def _subtype(lines) -> str | None:
    for ln in lines[:60]:
        if _SUBTYPE_CHECKING.search(ln.text):
            return "checking"
        if _SUBTYPE_SAVINGS.search(ln.text):
            return "savings"
    return None


_SAMPLE_CELLS = 3      # printed values shown under a column on Teach this layout
_SAMPLE_CHARS = 40     # a printed cell longer than this is cut: it is an example, not the text


def _column_samples(parsed: layout.Parsed) -> list[list[str]]:
    """Example cell texts per column, from the first data lines: what Teach this layout shows under each printed
    column, so a person can tell which printed column holds what. Local only; never sent to a model."""
    if not parsed.columns or not parsed.lines or not parsed.rows:
        return []
    line_ids = sorted({r.line_index for r in parsed.rows[:_SAMPLE_CELLS + 3] if 0 <= r.line_index < len(parsed.lines)})
    out: list[list[str]] = []
    for col in parsed.columns:                     # parsed columns carry their bands (layout._bands), not just the header
        cells: list[str] = []
        for li in line_ids:
            words = [w.text for w in parsed.lines[li].words if col.x0 <= (w.x0 + w.x1) / 2 < col.x1]
            text = " ".join(words).strip()
            if text and text not in cells:
                cells.append(text[:_SAMPLE_CHARS])
            if len(cells) >= _SAMPLE_CELLS:
                break
        out.append(cells)
    return out


def _column_labels(parsed: layout.Parsed) -> list[str]:
    """The printed header cell of each column, so Teach this layout can name the columns the way the statement does."""
    if not parsed.columns or not parsed.lines:
        return []
    header = next((ln for ln in parsed.lines if layout.header_columns(ln) is not None), None)
    if header is None:
        return []
    return [" ".join(w.text for w in header.words if col.x0 <= (w.x0 + w.x1) / 2 < col.x1).strip()[:40]
            for col in parsed.columns]


def _detail(parsed: layout.Parsed | None, fp: dict | None) -> dict:
    if parsed is None:
        return {"header_words": [], "columns": [], "samples": [], "row_count": 0, "problems": [], "fingerprint": fp}
    labels = _column_labels(parsed)
    return {"header_words": parsed.header_words(),
            "columns": [{"role": c.role, "label": labels[i] if i < len(labels) else "",
                         "x0": round(c.hx0, 1), "x1": round(c.hx1, 1)} for i, c in enumerate(parsed.columns)],
            "samples": _column_samples(parsed), "row_count": len(parsed.rows),   # a count: a refusal never carries rows
            "problems": list(parsed.problems), "year_placeholder": parsed.year_placeholder,
            "date_order": parsed.date_order, "fingerprint": fp}               # the order the digits resolved, or None


def _has_placeholder_year(parsed: layout.Parsed) -> bool:
    """Whether a resolved date still sits in the no-year placeholder year (never trust Parsed.year_placeholder)."""
    years = {r.date.year for r in parsed.rows if r.date is not None}
    if parsed.period is not None:
        years.update((parsed.period[0].year, parsed.period[1].year))
    return layout.YEAR_PLACEHOLDER in years


def _build(conn, parsed: layout.Parsed, verdict: replay_mod.Replay, *, fp: dict, template: dict | None,
           proposal: dict | None, ai_used: bool, lines, sizes, year: int | None) -> Statement:
    if verdict.accepted is not True or parsed.period is None:
        raise PdfRefused("does_not_reconcile")
    if year is None and _has_placeholder_year(parsed):
        extra = {"needs_year": True, **_refusal_extra("no_period", parsed, fp)}
        raise PdfRefused("no_period", _NO_YEAR_MESSAGE, extra=extra)
    opening, closing = parsed.opening, parsed.closing
    if opening is None or closing is None:
        # totals mode has both by definition; running mode may have a missing closing only when every row balanced
        opening = opening if opening is not None else Decimal("0")
        closing = closing if closing is not None else (opening + sum(r.amount for r in parsed.rows))
    rows = []
    for i, r in enumerate(parsed.rows, 1):
        amount = (-r.amount).quantize(Decimal("0.01")) + Decimal("0.00")
        rows.append(importfile.Row(date=r.date.isoformat(), description=" ".join(r.desc).strip(),
                                   amount=float(amount), account=None, fitid=None, line=i, trntype=None,
                                   currency=None))
    return Statement(rows=rows, period=parsed.period, opening=opening, closing=closing,
                     balance_mode="totals" if verdict.balance_mode == "totals" else "running",
                     template=template, proposal=proposal, ai_used=ai_used, currency=_currency(lines),
                     subtype=_subtype(lines), detail=_detail(parsed, fp))


def _template_ref(tpl: dict | None, config: dict | None, source: str) -> dict | None:
    if tpl is not None:
        return {"id": tpl["id"], "source": tpl["source"], "config": tpl["config"]}
    if config is not None:
        return {"id": None, "source": source, "config": config}
    return None


def _check(parsed: layout.Parsed, opening: Decimal | None, closing: Decimal | None) -> replay_mod.Replay:
    totals = None
    if parsed.debit_total is not None or parsed.credit_total is not None:
        totals = {"debit": parsed.debit_total, "credit": parsed.credit_total}
    return replay_mod.replay(parsed.rows, opening, closing, totals, parsed.carry_checks)


# ---------------------------------------------------------------- public entry point

def _check_raw(raw) -> None:
    """The refusals for a file before a word of it is read (shared by parse_statement and ai_outgoing)."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise PdfRefused("pdf_unreadable")
    if len(raw) > importfile.MAX_BYTES:
        raise PdfRefused("too_big", "This file is too big: an import can be up to 10 MB. Download a shorter date "
                                    "range from your bank.")
    if not importfile.is_pdf(raw):
        raise PdfRefused("pdf_unreadable")
    if not available():
        raise PdfRefused("missing_extra")


def _read_pages(raw: bytes, password):
    """(pages, sizes, producer) through the guard's child process: its page/word caps, wall clock and memory ceiling.
    Any raw pdfminer failure is already a WordReadError there; a limit hit is TooComplex."""
    try:
        return guard.read(raw, password)
    except words.PasswordRequired:
        raise PdfRefused("password_required", field="password") from None
    except words.PasswordWrong:
        raise PdfRefused("password_wrong", field="password") from None
    except words.TooComplex:
        raise PdfRefused("pdf_too_complex") from None
    except words.WordReadError:
        raise PdfRefused("pdf_unreadable") from None


def ai_outgoing(raw: bytes, *, password=None) -> list[str]:
    """Every prompt an AI layout proposal for this PDF could send (ai.outgoing), worked out here and sent nowhere: the
    privacy screen shows them word for word before Send. A file it can't read is refused as parse_statement would."""
    from . import ai
    _check_raw(raw)
    pages, _sizes, _producer = _read_pages(raw, password)
    try:
        return ai.outgoing(pages)
    except words.TooComplex:
        raise PdfRefused("pdf_too_complex") from None


def parse_statement(conn, raw: bytes, *, password=None, ai=False, template=None, template_source="user", opening=None,
                    closing=None, year=None, today=None) -> Statement:
    """Read one statement PDF. Raises PdfRefused (with a public code) when nothing may be imported. `template_source`
    ('user' or 'ai'): where a supplied template came from (an AI proposal the preview returned, confirmed as is)."""
    _check_raw(raw)
    try:
        open_anchor = _decimal(opening, "opening")
        close_anchor = _decimal(closing, "closing")
    except ValueError:
        raise PdfRefused("template_invalid", "The opening or closing balance you typed isn't a number.") from None

    pages, sizes, producer = _read_pages(raw, password)

    text = " ".join(w["text"] for page in pages for w in page).strip()
    if len(text) < 10:
        raise PdfRefused("no_text_layer")
    lines = words.build_lines(pages)
    if layout.credit_card_lines(lines):
        raise PdfRefused("credit_card")
    if layout.account_count(lines) > 1:
        raise PdfRefused("multi_account")

    fp = templates.fingerprint(pages, sizes, producer)
    anchors = {}
    if open_anchor is not None:
        anchors["user_opening"] = str(open_anchor)
    if close_anchor is not None:
        anchors["user_closing"] = str(close_anchor)

    supplied_cfg = None
    if template is not None:
        try:
            supplied_cfg = templates.clean_config(template)
        except ValueError as exc:
            raise PdfRefused("template_invalid", extra={"detail": {"reason": str(exc)}}) from None

    parsed: layout.Parsed | None = None
    verdict: replay_mod.Replay | None = None
    used: dict | None = None
    if supplied_cfg is not None:
        cfg = {**supplied_cfg, **anchors}
        parsed = layout.parse(pages, sizes, cfg, year=year)
        if not parsed.columns:
            raise PdfRefused("template_invalid", extra={"detail": _detail(parsed, fp)})
        verdict = _check(parsed, parsed.opening, parsed.closing)
        code = _code(parsed.problems, parsed, verdict)
        if code:
            raise PdfRefused(code, extra=_refusal_extra(code, parsed, fp, verdict))
        found = templates.find(conn, fp) if conn is not None else None
        source = "ai" if template_source == "ai" else "user"
        used = _template_ref(found if found and found["config"] == supplied_cfg else None, supplied_cfg, source)
        return _build(conn, parsed, verdict, fp=fp, template=used, proposal=None, ai_used=False,
                      lines=lines, sizes=sizes, year=year)

    generic = layout.parse(pages, sizes, anchors or None, year=year)
    parsed = generic
    verdict = _check(generic, generic.opening, generic.closing)
    code = _code(generic.problems, generic, verdict)
    if code is None and generic.rows:
        return _build(conn, generic, verdict, fp=fp, template=None, proposal=None, ai_used=False,
                      lines=lines, sizes=sizes, year=year)

    if code is not None and code not in _HARD and conn is not None:
        found = templates.find(conn, fp)
        if found is not None:
            cfg = {**found["config"], **anchors}
            saved = layout.parse(pages, sizes, cfg, year=year)
            saved_verdict = _check(saved, saved.opening, saved.closing)
            saved_code = _code(saved.problems, saved, saved_verdict)
            if saved_code is None and saved.rows:
                return _build(conn, saved, saved_verdict, fp=fp,
                              template=_template_ref(found, None, found["source"]), proposal=None, ai_used=False,
                              lines=lines, sizes=sizes, year=year)
            parsed, verdict, code = saved, saved_verdict, saved_code

    if ai and code is not None and code not in _HARD:
        from . import ai as ai_mod

        try:
            cfg, ai_parsed, ai_verdict, outcomes = ai_mod.propose(
                conn, pages, sizes, anchors=anchors, year=year,
                totals={"debit": generic.debit_total, "credit": generic.credit_total},
                carry=generic.carry_checks)
        except PdfRefused:
            raise
        except Exception:  # noqa: BLE001 - a proposal bug is one refusal, never a crash or a 500
            raise PdfRefused("ai_failed", extra={"detail": {"reason": "proposal_error"}}) from None
        # The AI candidate passes the same gate as the generic and template paths. When it is refused for the same date
        # or period problem the local parser hit (an ambiguous date order: the model only sees 99/99/9999), the
        # refusal is that code, exactly as without AI.
        ai_code = (_code(ai_parsed.problems, ai_parsed, ai_verdict)
                   if ai_parsed is not None and ai_verdict is not None else "ai_failed")
        if ai_code in ai_mod.DATE_CODES and ai_code == code:
            extra = _refusal_extra(ai_code, ai_parsed, fp, ai_verdict)
            extra.setdefault("detail", {})["candidates"] = len(outcomes)
            raise PdfRefused(ai_code, extra=extra)
        if cfg is not None and ai_code is None and ai_verdict.accepted and ai_parsed.period is not None:
            try:
                return _build(conn, ai_parsed, ai_verdict, fp=fp,
                              template=_template_ref(None, cfg, "ai"), proposal=cfg, ai_used=True, lines=lines,
                              sizes=sizes, year=year)
            except PdfRefused as exc:
                if exc.extra.get("needs_year"):
                    exc.extra["proposal"] = cfg
                    exc.extra["ai_used"] = True
                raise
        extra = _refusal_extra("ai_failed", parsed or generic, fp, verdict)
        extra["detail"] = {**extra.get("detail", {}), "candidates": len(outcomes),
                           "outcomes": [o for o in outcomes if re.fullmatch(r"[a-z_]+", o or "")][:8]}
        if cfg is not None:
            extra["proposal"] = cfg
        raise PdfRefused("ai_failed", extra=extra)

    if code is None:
        code = "no_rows" if not generic.rows else "does_not_reconcile"
    raise PdfRefused(code, extra=_refusal_extra(code, parsed or generic, fp, verdict))


def _refusal_extra(code: str, parsed: layout.Parsed | None, fp: dict | None,
                   verdict: replay_mod.Replay | None = None) -> dict:
    extra: dict = {}
    if code in _FIXABLE:
        extra["detail"] = _detail(parsed, fp)
        if verdict is not None and not verdict.accepted:
            extra["detail"]["replay"] = verdict.reason
            if verdict.first_bad_row_index is not None:
                extra["detail"]["first_bad_row"] = verdict.first_bad_row_index
    return extra
