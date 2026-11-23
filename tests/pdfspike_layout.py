"""The PDF import spike's seam: a generic, profile-driven layout parser over pdfplumber.

This is measurement code, not an importer. It answers one question: can a PDF text layer be
turned into `hermes_finance.importfile.Row` objects, in row order, reconciling to the
statement's own printed opening and closing balances?

The public seam is `rows(pdf_bytes, password=None, *, profile)`; `parse()` is the same work
with the printed balances exposed for the out-of-sample runner. Country profiles carry
CONVENTIONS ONLY (date order, decimal and thousands separators, header words, sign style,
the words that mark an opening, closing or summary line). A profile never carries a row
count, an amount, a bank name, a page count, or anything else file-specific.

pdfplumber is imported lazily, inside the call: the repository's own venv doesn't have it,
and the test module must skip cleanly there.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from hermes_finance.importfile import Row, _cents, _parse_amount, _parse_date

__all__ = [
    "PROFILES", "PdfSpikeError", "PdfEngineMissing", "PasswordRequired", "NoTextLayer",
    "MultiAccountUnsupported", "NoRows", "ReconcileMismatch", "NoBalances", "ParseResult", "parse", "rows",
]


class PdfSpikeError(Exception):
    """A PDF that can't become rows, in plain words for the user. Subclasses carry a stable `.code`."""

    code = "pdf_spike_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PdfEngineMissing(PdfSpikeError):
    code = "no_engine"


class PasswordRequired(PdfSpikeError):
    code = "password_required_or_wrong"


class NoTextLayer(PdfSpikeError):
    code = "no_text_layer"


class MultiAccountUnsupported(PdfSpikeError):
    code = "multi_account"


class NoRows(PdfSpikeError):
    code = "no_rows"


class ReconcileMismatch(PdfSpikeError):
    code = "does_not_reconcile"


class NoBalances(PdfSpikeError):
    code = "no_balances"


_ENGINE_HELP = ("The PDF parser (pdfplumber) isn't installed. Install it in a disposable venv: "
                "UV_LINK_MODE=copy uv pip install --python .dispatch/venv/bin/python pdfplumber")


@dataclass(frozen=True)
class Profile:
    """One country's conventions. Shared by every statement of that country; never file-specific."""

    country: str
    date_formats: tuple[str, ...]
    decimal_comma: bool
    headers: dict[str, tuple[str, ...]]      # field -> normalized header phrases
    positive: str | None = None              # signed amount column: "out" or "in" for a positive number
    opening_words: tuple[str, ...] = ("opening balance",)
    closing_words: tuple[str, ...] = ("closing balance",)
    summary_words: tuple[str, ...] = ("total", "summary", "subtotal")
    number_words: tuple[str, ...] = ("dr", "cr")   # DR/CR marker or suffix words: DR out, CR in


def _p(country: str, date_formats: tuple[str, ...], decimal_comma: bool, headers: dict[str, tuple[str, ...]],
       **kw) -> Profile:
    return Profile(country, date_formats, decimal_comma, headers, **kw)


PROFILES: dict[str, Profile] = {
    "AU": _p(
        "AU", ("%d/%m/%Y", "%d/%m/%y", "%d %b %Y", "%d %b %y", "%b %d, %Y", "%B %d, %Y"), False,
        {
            "date": ("date", "transaction date", "txn date", "posting date", "value date"),
            "description": ("description", "transaction", "details", "particulars"),
            "debit": ("debit", "debit amount", "withdrawal", "withdrawals", "money out"),
            "credit": ("credit", "credit amount", "deposit", "deposits", "money in"),
            "balance": ("balance", "closing balance", "running balance"),
        },
        opening_words=("opening balance",),
        closing_words=("closing balance",),
        summary_words=("total", "summary", "subtotal", "closing balance", "brought forward", "carried forward"),
    ),
    "IN": _p(
        "IN", ("%d-%m-%y", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y"), False,
        {
            "date": ("date", "txn date", "transaction date", "posting date", "value date"),
            "description": ("narration", "description", "particulars", "details", "transaction"),
            "reference": ("chq ref no", "chq ref", "reference", "ref", "cheque no", "chq no"),
            "debit": ("withdrawal amt", "withdrawal", "withdrawals", "debit", "debit amount"),
            "credit": ("deposit amt", "deposit", "deposits", "credit", "credit amount"),
            "balance": ("closing balance", "balance", "running balance"),
        },
        opening_words=("opening balance",),
        closing_words=("closing balance", "closing bal"),
        summary_words=("total", "summary", "subtotal", "closing balance"),
    ),
    "SG": _p(
        "SG", ("%d %b %Y", "%d %b %y", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"), False,
        {
            "date": ("date", "transaction date", "posting date", "value date"),
            "description": ("transaction", "description", "details", "particulars"),
            "debit": ("withdrawal", "withdrawals", "debit"),
            "credit": ("deposit", "deposits", "credit"),
            "marker": ("dr cr", "cr dr", "type", "d c"),
            "amount": ("amount", "transaction amount"),
            "balance": ("balance", "closing balance", "running balance"),
        },
        opening_words=("opening balance",),
        closing_words=("closing balance",),
        summary_words=("total", "summary", "subtotal", "statement summary", "closing balance"),
    ),
    "US": _p(
        "US", ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%b %d %Y"), False,
        {
            "date": ("date", "transaction date", "posting date", "value date"),
            "description": ("description", "details", "transaction", "memo"),
            "debit": ("debit", "debits", "withdrawal", "withdrawals"),
            "credit": ("credit", "credits", "deposit", "deposits"),
            "amount": ("amount", "transaction amount"),
            "balance": ("balance", "closing balance", "running balance"),
        },
        positive="in",
        opening_words=("opening balance", "beginning balance", "previous balance"),
        closing_words=("closing balance", "ending balance", "new balance"),
        summary_words=("total", "summary", "subtotal", "deposits other credits", "withdrawals other debits",
                       "closing balance", "ending balance", "new balance", "previous balance"),
    ),
    "BR": _p(
        "BR", ("%d/%m/%Y", "%d/%m/%y"), True,
        {
            "date": ("data", "data transacao", "data valor"),
            "description": ("historico", "descricao", "detalhes"),
            "sign": ("tipo", "d c", "sign"),
            "amount": ("valor", "valor lancamento"),
            "balance": ("saldo", "saldo final"),
        },
        opening_words=("saldo anterior", "opening balance"),
        closing_words=("saldo final", "saldo atual", "closing balance"),
        summary_words=("total", "resumo", "saldo final", "saldo atual"),
    ),
    "CL": _p(
        "CL", ("%d-%m-%Y", "%d-%m-%y"), True,
        {
            "date": ("fecha", "fecha transaccion", "fecha valor"),
            "description": ("descripcion", "detalle", "detalles"),
            "debit": ("cargo", "cargos", "giro", "debito"),
            "credit": ("abono", "abonos", "deposito", "credito"),
            "balance": ("saldo", "saldo final"),
        },
        opening_words=("saldo inicial", "opening balance"),
        closing_words=("saldo final", "closing balance"),
        summary_words=("total", "resumen", "saldo final"),
    ),
    "CO": _p(
        "CO", ("%d/%m/%Y", "%d/%m/%y"), False,
        {
            "date": ("fecha", "fecha transaccion", "fecha valor"),
            "description": ("descripcion", "detalle", "detalles"),
            "amount": ("valor", "valor transaccion"),
            "balance": ("saldo", "saldo final"),
        },
        positive="in",
        opening_words=("saldo inicial", "opening balance"),
        closing_words=("saldo final", "closing balance"),
        summary_words=("total", "resumen", "saldo final"),
    ),
    "DE": _p(
        "DE", ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"), True,
        {
            "date": ("datum", "buchung", "buchungstag", "wertstellung", "valuta"),
            "description": ("verwendungszweck", "vorgang", "beschreibung", "buchungstext", "text"),
            "debit": ("soll", "lastschrift", "ausgabe", "auszahlung", "belastung"),
            "credit": ("haben", "gutschrift", "einnahme", "einzahlung"),
            "amount": ("betrag", "umsatz", "betrag eur"),
            "balance": ("saldo", "kontostand", "saldo eur"),
        },
        positive="in",
        opening_words=("anfangssaldo", "anfangsbestand", "saldovortrag", "saldo vortrag", "alter saldo",
                       "uebertrag", "opening balance"),
        closing_words=("endsaldo", "endbestand", "neuer saldo", "kontostand per", "kontostand", "schlussaldo",
                       "closing balance"),
        summary_words=("summe", "gesamt", "zwischensumme", "endsaldo", "kontostand", "saldovortrag", "uebertrag"),
    ),
    "CA": _p(
        "CA", ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y"), False,
        {
            "date": ("date", "transaction date", "posting date", "value date", "activity date"),
            "description": ("description", "details", "transaction", "memo", "activity"),
            "debit": ("debit", "debits", "withdrawal", "withdrawals"),
            "credit": ("credit", "credits", "deposit", "deposits"),
            "amount": ("amount", "transaction amount"),
            "balance": ("balance", "closing balance", "running balance"),
        },
        positive="in",
        opening_words=("opening balance", "beginning balance", "previous balance"),
        closing_words=("closing balance", "ending balance", "new balance"),
        summary_words=("total", "summary", "subtotal", "closing balance", "ending balance", "new balance"),
    ),
    "CH": _p(
        "CH", ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%Y-%m-%d"), False,
        {
            "date": ("datum", "date", "data", "buchungsdatum"),
            "description": ("beschreibung", "verwendungszweck", "description", "libelle", "descrizione", "text"),
            "debit": ("soll", "debit", "belastung", "ausgabe"),
            "credit": ("haben", "credit", "gutschrift", "einnahme"),
            "amount": ("betrag", "montant", "importo", "amount"),
            "balance": ("saldo", "solde", "balance", "saldo conto"),
        },
        positive="in",
        opening_words=("anfangssaldo", "sold initial", "saldo iniziale", "opening balance", "alter saldo"),
        closing_words=("endsaldo", "solde final", "saldo finale", "closing balance", "neuer saldo"),
        summary_words=("total", "summe", "saldo final", "endsaldo", "solde final"),
    ),
}


# --- text normalization -------------------------------------------------------------------------------------------

_ACCENT = re.compile(r"[\u0300-\u036f]")
_KEEP = re.compile(r"[^a-z0-9+()$-]+")


def _norm(text: str) -> str:
    """Lower-case, unaccented, punctuation-light text for matching headers and marker words."""
    s = unicodedata.normalize("NFKD", text or "")
    s = _ACCENT.sub("", s).casefold()
    s = _KEEP.sub(" ", s)
    return " ".join(s.split())


def _phrase_in(norm: str, phrases: tuple[str, ...]) -> bool:
    for phrase in phrases:
        if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", norm):
            return True
    return False


# --- pdfplumber extraction ----------------------------------------------------------------------------------------

@dataclass
class _Token:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass
class _Line:
    tokens: list[_Token] = field(default_factory=list)
    page: int = 0

    @property
    def text(self) -> str:
        return " ".join(t.text for t in self.tokens)

    @property
    def norm(self) -> str:
        return _norm(self.text)


def _lines_of_page(page, page_number: int = 0) -> list[_Line]:
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False, extra_attrs=["size"])
    tokens: list[_Token] = []
    for w in words:
        text = (w.get("text") or "").strip()
        if not text:
            continue
        tokens.append(_Token(text, float(w["x0"]), float(w["x1"]), float(w["top"]), float(w["bottom"])))
    tokens.sort(key=lambda t: (t.top, t.x0))
    lines: list[_Line] = []
    for tok in tokens:
        center = (tok.top + tok.bottom) / 2
        for line in reversed(lines[-4:]):
            tops = [t.top for t in line.tokens]
            bottoms = [t.bottom for t in line.tokens]
            if min(tops) - 2.0 <= center <= max(bottoms) + 2.0:
                line.tokens.append(tok)
                break
        else:
            lines.append(_Line([tok], page_number))
    for line in lines:
        line.tokens.sort(key=lambda t: t.x0)
    lines.sort(key=lambda line: (min(t.top for t in line.tokens), min(t.x0 for t in line.tokens)))
    return lines


@dataclass
class _Span:
    field: str | None            # None: a header column with no parsing role (a reference, a value date)
    x0: float
    x1: float
    center: float


def _header_cells(line: _Line, gap: float = 3.0) -> list[list[_Token]]:
    """The header line split into visual cells: a gap wider than `gap` points starts a new cell."""
    cells: list[list[_Token]] = []
    for tok in line.tokens:
        if cells and tok.x0 - cells[-1][-1].x1 <= gap:
            cells[-1].append(tok)
        else:
            cells.append([tok])
    return cells


def _match_header(line: _Line, profile: Profile) -> list[_Span] | None:
    """The header's column spans with each cell matched to a field, or None when the line isn't a header.

    A cell may hold a longer label ("Transaction Date", "Transaction Description"): an exact phrase beats a
    suffix match, which beats a prefix match, and a longer phrase beats a shorter one. A header can name its
    columns in any order; the common labels are conventions, not file-specific strings.
    """
    cells = _header_cells(line)
    texts = [_norm(" ".join(t.text for t in cell)) for cell in cells]
    candidates: list[tuple[int, int, int, int, str]] = []
    for ci, text in enumerate(texts):
        for fi, (name, phrases) in enumerate(profile.headers.items()):
            for phrase in phrases:
                if text == phrase:
                    score = 3 + len(phrase.split())
                elif text.startswith(phrase + " "):
                    score = 1 + len(phrase.split())
                elif text.endswith(" " + phrase):
                    score = 2 + len(phrase.split())
                else:
                    continue
                candidates.append((score, -ci, -fi, ci, name))
    candidates.sort(reverse=True)
    assigned: dict[int, str] = {}
    used: set[str] = set()
    for _score, _nci, _nfi, ci, name in candidates:
        if ci in assigned or name in used:
            continue
        assigned[ci] = name
        used.add(name)
    spans: list[_Span] = []
    for ci, cell in enumerate(cells):
        x0, x1 = min(t.x0 for t in cell), max(t.x1 for t in cell)
        spans.append(_Span(assigned.get(ci), x0, x1, (x0 + x1) / 2))
    if "description" not in used and "date" in used:
        # A description column whose label is missing or outside the vocabulary: the widest unnamed
        # text cell between the date and the first money column is it.
        date_span = next(span for span in spans if span.field == "date")
        money = [span.center for span in spans if span.field in {"debit", "credit", "amount", "balance", "marker",
                                                                 "sign"}]
        if money:
            right = min(money)
            between = [(span.x1 - span.x0, i) for i, span in enumerate(spans)
                       if span.field is None and date_span.x1 <= span.x0 and span.center < right]
            if between:
                _width, index = max(between)
                spans[index].field = "description"
                used.add("description")
    if not {"date", "description"} <= used:
        return None
    if not ({"debit", "credit"} & used or {"amount"} & used or {"marker"} & used or {"sign"} & used):
        return None
    return spans


def _merge_lines(first: _Line, second: _Line) -> _Line:
    """A two-row header read as one line: tokens ordered left to right, first row first on a tie."""
    return _Line(sorted(first.tokens + second.tokens, key=lambda t: (t.x0, t.top)), first.page)


def _line_gap(first: _Line, second: _Line) -> float:
    return min(t.top for t in second.tokens) - max(t.bottom for t in first.tokens)


def _has_money_token(line: _Line, profile: Profile) -> bool:
    """A line with a printed amount (not just a year or a date) can't be part of a header block."""
    for tok in line.tokens:
        if _is_date_token(tok.text):
            continue
        try:
            value, _ = _parse_amount(tok.text, profile.decimal_comma)
        except ValueError:
            continue
        if value is None:
            continue
        if "$" in tok.text or re.search(r"[.,]\d{2}$", tok.text):
            return True
    return False


def _header_block(lines: list[_Line], index: int, profile: Profile) -> tuple[int, list[_Span]] | None:
    """The header starting at `index`: one line, or two adjacent lines forming one header block."""
    found = _match_header(lines[index], profile)
    if found is not None:
        return index, found
    if index + 1 < len(lines) and _line_gap(lines[index], lines[index + 1]) <= 14.0:
        if _has_money_token(lines[index], profile) or _has_money_token(lines[index + 1], profile):
            return None
        found = _match_header(_merge_lines(lines[index], lines[index + 1]), profile)
        if found is not None:
            return index + 1, found
    return None


def _assign(line: _Line, spans: list[_Span], page_width: float) -> dict[str, str]:
    """Each word to its header column; the text of every named field, joined in reading order."""
    ordered = sorted(spans, key=lambda s: s.center)
    bounds: list[tuple[float, float, _Span]] = []
    left = 0.0
    for i, span in enumerate(ordered):
        right = (span.center + ordered[i + 1].center) / 2 if i + 1 < len(ordered) else page_width
        bounds.append((left, right, span))
        left = right
    cells: dict[str, list[str]] = {}
    for tok in line.tokens:
        cx = (tok.x0 + tok.x1) / 2
        for lo, hi, span in bounds:
            if lo <= cx < hi or (hi == page_width and cx >= lo):
                if span.field:
                    cells.setdefault(span.field, []).append(tok.text)
                break
    return {k: " ".join(v) for k, v in cells.items()}


_NUMERIC_FIELDS = frozenset({"debit", "credit", "amount", "balance"})
_DATE_TOKEN = re.compile(r"\d{1,4}[./-]\d{1,2}(?:[./-]\d{2,4})?\.?$|"
                         r"\d{1,2}\s*[A-Za-z]{3,9}\s*\d{0,4}$|"
                         r"[A-Za-z]{3,9}\.?\s*\d{1,2},?$")


def _is_date_token(text: str) -> bool:
    text = text.strip()
    if not _DATE_TOKEN.match(text):
        return False
    groups = [int(group) for group in re.findall(r"\d+", text)]
    if "." in text and "/" not in text and "-" not in text and len(groups) >= 2:
        # "976.22" is an amount, not the 976th day of the 22nd month.
        if groups[0] > 31 or groups[1] > 12:
            return False
    return True


def _looks_like_money(text: str) -> bool:
    """A printed amount, as opposed to a bare date fragment or a reference number."""
    return bool(re.search(r"[.,]\d{2}(?!\d)", text) or "$" in text or "\u20ac" in text or "\u00a3" in text
                or text.startswith("("))


def _numeric_clusters(lines: list[_Line], start: int, profile: Profile, left_limit: float = 0.0) -> list[dict]:
    """Right-edge clusters of numeric tokens below the header: where the money columns actually sit."""
    clusters: list[dict] = []
    for line in lines[start:]:
        for tok in line.tokens:
            if (tok.x0 + tok.x1) / 2 < left_limit:
                continue
            if _is_date_token(tok.text) or not _looks_like_money(tok.text):
                continue
            try:
                value, _ = _parse_amount(tok.text, profile.decimal_comma)
            except ValueError:
                continue
            if value is None:
                continue
            for cluster in clusters:
                if abs(tok.x1 - cluster["x1"]) <= 3.0:
                    cluster["tokens"].append(tok)
                    cluster["x0"] = min(cluster["x0"], tok.x0)
                    cluster["x1"] = sum(t.x1 for t in cluster["tokens"]) / len(cluster["tokens"])
                    cluster["anchor"] = cluster["x1"]
                    break
            else:
                clusters.append({"tokens": [tok], "x0": tok.x0, "x1": tok.x1, "anchor": tok.x1})
    return clusters


def _matched_pairs(fields: list[_Span], clusters: list[dict], offset: float,
                   limit: float) -> list[tuple[float, int, int]]:
    pairs = sorted((abs(span.center + offset - cluster["anchor"]), si, ci)
                   for si, span in enumerate(fields) for ci, cluster in enumerate(clusters))
    used_fields: set[int] = set()
    used_clusters: set[int] = set()
    matched: list[tuple[float, int, int]] = []
    for distance, si, ci in pairs:
        if si in used_fields or ci in used_clusters or distance > limit:
            continue
        used_fields.add(si)
        used_clusters.add(ci)
        matched.append((distance, si, ci))
    return matched


def _refine_numeric_spans(lines: list[_Line], start: int, spans: list[_Span],
                          profile: Profile, page_width: float) -> list[_Span]:
    """Move money columns onto the values' geometry, so a left-aligned header label can't misassign them.

    Header labels and their values can sit at different offsets inside the same columns, so the best
    constant shift between the two is found first (a two-row block, or a statement whose money columns
    are narrower than their labels, shifts every column together). A field whose column has no values
    is left on its header geometry rather than being forced onto a stray number.
    """
    fields = sorted((span for span in spans if span.field in _NUMERIC_FIELDS), key=lambda span: span.center)
    if not fields:
        return spans
    limit = page_width * 0.25
    left_limit = min(span.center for span in fields) - limit
    clusters = sorted(_numeric_clusters(lines, start, profile, left_limit), key=lambda cluster: cluster["anchor"])
    if not clusters:
        return spans
    offsets = {round(cluster["anchor"] - span.center, 3) for span in fields for cluster in clusters}
    best_offset, best_key = 0.0, (-1, 0.0)
    for offset in offsets:
        matched = _matched_pairs(fields, clusters, offset, limit)
        key = (len(matched), -sum(distance for distance, *_ in matched))
        if key > best_key:
            best_offset, best_key = offset, key
    for distance, si, ci in _matched_pairs(fields, clusters, best_offset, limit):
        span, cluster = fields[si], clusters[ci]
        span.x0, span.x1, span.center = cluster["x0"], cluster["x1"], cluster["anchor"]
    return spans


def _line_amount(line: _Line, spans: list[_Span], profile: Profile, page_width: float) -> Decimal | None:
    cells = _assign(line, spans, page_width)
    for name in ("balance", "amount", "credit", "debit"):
        text = cells.get(name, "")
        if text:
            try:
                value, _ = _parse_amount(text, profile.decimal_comma)
            except ValueError:
                continue
            if value is not None:
                return value
    for tok in line.tokens:
        try:
            value, _ = _parse_amount(tok.text, profile.decimal_comma)
        except ValueError:
            continue
        if value is not None:
            return value
    return None


_DATE_TEXT = re.compile(r"\d{1,4}[/.\-]\d{1,2}[/.\-]\d{2,4}|"
                        r"\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{2,4}|"
                        r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{2,4}")


def _profile_date(text: str, profile: Profile):
    text = " ".join(text.split())
    if not text:
        return None
    candidates = [text]
    m = _DATE_TEXT.search(text)
    if m and m.group(0) != text:
        candidates.append(m.group(0))
    for candidate in candidates:
        for fmt in profile.date_formats:
            parsed = _parse_date(candidate, fmt)
            if parsed is not None:
                return parsed
    return None


def _yearless_formats(profile: Profile) -> list[str]:
    """The profile's date formats with the year (and its separator) removed."""
    formats: list[str] = []
    for fmt in profile.date_formats:
        if "%Y" not in fmt and "%y" not in fmt:
            continue
        base = re.sub(r"[ ,./\-]*%[Yy]$", "", fmt).rstrip(" ,./-")
        if base and base not in formats:
            formats.append(base)
    return formats


def _partial_date(text: str, profile: Profile):
    """A date printed without its year ("02 Jun", "12.03.", "05/14"), or None."""
    text = " ".join(text.split())
    if not text:
        return None
    candidates = [text, text.rstrip(" ,./-")]
    for m in re.finditer(r"\d{1,4}[./-]\d{1,2}\.?|\d{1,2}\s+[A-Za-z]{3,9}\.?|"
                         r"[A-Za-z]{3,9}\.?\s+\d{1,2}", text):
        candidate = m.group(0).rstrip(" ,./-")
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        for fmt in _yearless_formats(profile):
            parsed = _parse_date(candidate, fmt)
            if parsed is not None:
                return parsed
    return None


def _infer_year(month: int, day: int, reference_dates: list[date]) -> int | None:
    """The year that puts `month`/`day` inside the statement period printed on the page."""
    if not reference_dates:
        return None
    first, last = min(reference_dates), max(reference_dates)
    if first.year == last.year:
        return first.year
    fits: list[int] = []
    for year in range(first.year, last.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if first <= candidate <= last:
            fits.append(year)
    return fits[0] if len(fits) == 1 else None


def _row_date(text: str, profile: Profile, reference_dates: list[date]) -> date | None:
    """A row's date: the full date when printed, otherwise its month/day with the printed period's year."""
    full = _profile_date(text, profile)
    if full is not None:
        return full
    partial = _partial_date(text, profile)
    if partial is None:
        return None
    year = _infer_year(partial.month, partial.day, reference_dates)
    if year is None:
        return None
    try:
        return date(year, partial.month, partial.day)
    except ValueError:
        return None


def _preamble_dates(lines: list[_Line], header_index: int, profile: Profile) -> list[date]:
    found: list[date] = []
    for line in lines[:header_index + 1]:
        for match in _DATE_TEXT.finditer(line.text):
            parsed = _profile_date(match.group(0), profile)
            if parsed is not None:
                found.append(parsed)
    return found


@dataclass
class _RowAcc:
    date: str
    description: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    balance: Decimal | None = None


def _app_amount(values: dict[str, str], profile: Profile) -> Decimal | None:
    """The row's amount in the app's sign: > 0 money out, < 0 money in."""
    if "debit" in values or "credit" in values:
        total, found = Decimal(0), False
        for name, direction in (("debit", 1), ("credit", -1)):
            text = values.get(name, "")
            if not text:
                continue
            try:
                value, override = _parse_amount(text, profile.decimal_comma)
            except ValueError:
                continue
            if value is None:
                continue
            found = True
            if override:
                direction = 1 if override == "out" else -1
            total += direction * abs(value)
        return total if found else None
    if "marker" in values and "amount" in values:
        try:
            value, _ = _parse_amount(values.get("amount", ""), profile.decimal_comma)
        except ValueError:
            return None
        if value is None:
            return None
        marker = _norm(values.get("marker", ""))
        if marker in profile.number_words and marker == "dr":
            return abs(value)
        if marker in profile.number_words and marker == "cr":
            return -abs(value)
        return None
    if "sign" in values and "amount" in values:
        try:
            value, override = _parse_amount(values.get("amount", ""), profile.decimal_comma)
        except ValueError:
            return None
        if value is None:
            return None
        if override:
            return abs(value) if override == "out" else -abs(value)
        token = _norm(values.get("sign", ""))
        if token == "(+)":                 # added to the balance: money in
            return -abs(value)
        if token == "(-)":                 # taken from the balance: money out
            return abs(value)
        return None
    text = values.get("amount", "")
    if not text:
        return None
    try:
        value, override = _parse_amount(text, profile.decimal_comma)
    except ValueError:
        return None
    if value is None:
        return None
    if override:
        return abs(value) if override == "out" else -abs(value)
    return value if profile.positive == "out" else -value


_MISMATCH = ("This statement's transactions don't add up to the balance it prints, so the import can't trust "
             "what it read. Nothing was imported. Download the transactions as CSV or OFX instead.")
_NO_OPENING = ("This statement doesn't print an opening balance, so the import can't check that the first "
               "transaction was read correctly. Nothing was imported. Download the transactions as CSV or OFX "
               "instead.")
_NO_BALANCES = ("This statement doesn't print an opening and closing balance or a running balance, so the import "
                "can't check that it read the transactions correctly. Nothing was imported. Download the "
                "transactions as CSV or OFX instead.")


def _parse_lines(lines: list[_Line], profile: Profile, page_width: float) -> tuple[list[Row], Decimal | None,
                                                                                   Decimal | None]:
    header_start = header_end = None
    spans: list[_Span] = []
    for i in range(len(lines)):
        block = _header_block(lines, i, profile)
        if block is not None:
            header_start, header_end = i, block[0]
            spans = block[1]
            break
    if header_start is None:
        raise NoRows("This PDF has text but no transaction table in it (no date, description and amount columns "
                     "were found). Download the transactions as CSV or OFX instead.")
    assert header_end is not None

    spans = _refine_numeric_spans(lines, header_end + 1, spans, profile, page_width)
    reference_dates = _preamble_dates(lines, header_end, profile)

    opening_candidates: list[tuple[int, Decimal]] = []
    closing_candidates: list[tuple[int, Decimal]] = []
    for i, line in enumerate(lines):
        norm = line.norm
        is_open = _phrase_in(norm, profile.opening_words)
        is_close = False if is_open else _phrase_in(norm, profile.closing_words)
        if not (is_open or is_close):
            continue
        value = _line_amount(line, spans, profile, page_width)
        if value is None and i + 1 < len(lines):
            nxt = lines[i + 1].norm
            if re.fullmatch(r"[()+\-\d.,\s$r]+", nxt):
                value = _line_amount(lines[i + 1], spans, profile, page_width)
        if value is not None:
            (opening_candidates if is_open else closing_candidates).append((i, value))

    after_open = [(i, value) for i, value in opening_candidates if i > header_end]
    before_open = [(i, value) for i, value in opening_candidates if i < header_start]
    if len({value for _i, value in after_open}) > 1:
        raise MultiAccountUnsupported("This PDF holds more than one account or balance stream. This import "
                                      "supports one account per file; download each account separately.")
    if len({value for _i, value in before_open}) > 1 and not after_open:
        raise MultiAccountUnsupported("This PDF holds more than one account or balance stream. This import "
                                      "supports one account per file; download each account separately.")
    opening = after_open[0][1] if after_open else (before_open[-1][1] if before_open else None)

    after_close = [(i, value) for i, value in closing_candidates if i > header_end]
    before_close = [(i, value) for i, value in closing_candidates if i < header_start]
    closing = after_close[-1][1] if after_close else (before_close[-1][1] if before_close else None)

    header_fields = frozenset(f for f in (s.field for s in spans) if f)
    header_page = lines[header_start].page
    repeated: set[int] = set()
    page_start = 0
    while page_start < len(lines):
        page = lines[page_start].page
        page_end = page_start
        while page_end < len(lines) and lines[page_end].page == page:
            page_end += 1
        for i in range(page_start, page_end):
            block = _header_block(lines, i, profile)
            if block is None:
                continue
            end, found = block
            if frozenset(f for f in (s.field for s in found) if f) != header_fields:
                continue
            repeated.update(range(i, end + 1))
            if page != header_page:
                repeated.update(range(page_start, i))
            break
        page_start = page_end

    rows: list[Row] = []
    current: _RowAcc | None = None
    printed_balances: list[Decimal | None] = []

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        amount = _app_amount(current.values, profile)
        if amount is not None:
            description = " ".join(" ".join(current.description).split())
            if len(description) > 240 or len(description.split()) > 40:
                raise NoRows("This PDF's text doesn't form a clean transaction table, so the import can't read it "
                             "reliably. Download the transactions as CSV or OFX instead.")
            rows.append(Row(current.date, description, _cents(amount), None, None, len(rows) + 1))
            printed_balances.append(current.balance)
        current = None

    for i, line in enumerate(lines):
        if i <= header_end or i in repeated:
            continue
        cells = _assign(line, spans, page_width)
        norm = line.norm
        if not norm:
            continue
        if re.fullmatch(r"page \d+( of \d+)?", norm) or re.fullmatch(r"\d{1,3}", norm):
            continue
        if _phrase_in(norm, profile.opening_words) or _phrase_in(norm, profile.closing_words):
            continue
        if any(norm.startswith(word) for word in profile.summary_words):
            continue

        date_text = cells.get("date", "")
        day = _row_date(date_text, profile, reference_dates) if date_text else None
        if day is not None:
            flush()
            current = _RowAcc(day.isoformat())
        elif current is None:
            continue
        if current is None:
            continue
        for name in ("description", "debit", "credit", "amount", "marker", "sign", "reference"):
            text = cells.get(name, "")
            if not text:
                continue
            if name == "description":
                current.description.append(text)
            elif name not in current.values:
                current.values[name] = text
        balance_text = cells.get("balance", "")
        if balance_text:
            try:
                value, _ = _parse_amount(balance_text, profile.decimal_comma)
            except ValueError:
                value = None
            if value is not None:
                current.balance = value
    flush()

    if closing is None:
        for value in reversed(printed_balances):
            if value is not None:
                closing = value
                break

    if not rows:
        raise NoRows("This PDF has text but no transaction rows in it. Download the transactions as CSV or OFX "
                     "instead.")
    if opening is None:
        raise NoBalances(_NO_OPENING)

    tolerance = Decimal("0.01")
    balanced = sum(1 for value in printed_balances if value is not None)
    running_available = bool(balanced) and (balanced == len(rows) or balanced >= max(2, int(len(rows) * 0.8)))
    total_available = opening is not None and closing is not None
    if not running_available and not total_available:
        raise NoBalances(_NO_BALANCES)

    def reconciles(values: list[Decimal]) -> bool:
        if total_available and abs(opening - sum(values) - closing) > tolerance:
            return False
        if running_available:
            prior = opening
            checked = 0
            for amount, balance in zip(values, printed_balances):
                if balance is None:
                    continue
                if prior is not None:
                    if abs(prior - amount - balance) > tolerance:
                        return False
                    checked += 1
                prior = balance
            if checked == 0:
                return False
        return True

    amounts = [Decimal(str(row.amount)) for row in rows]
    if not reconciles(amounts):
        flipped = [-amount for amount in amounts]
        if profile.positive is not None and reconciles(flipped):
            rows = [Row(r.date, r.description, _cents(-Decimal(str(r.amount))), r.account, r.fitid, r.line,
                        r.trntype) for r in rows]
        else:
            raise ReconcileMismatch(_MISMATCH)

    return rows, opening, closing


@dataclass
class ParseResult:
    rows: list[Row]
    opening: float | None
    closing: float | None
    pages: int
    text_chars: int


def _is_password_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    todo: list[BaseException | None] = [exc]
    while todo:
        cur = todo.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if type(cur).__name__ == "PDFPasswordIncorrect":
            return True
        args = getattr(cur, "args", ())
        if args and type(args[0]).__name__ == "PDFPasswordIncorrect":
            return True
        todo.extend([getattr(cur, "__cause__", None), getattr(cur, "__context__", None)])
        if args:
            todo.extend(a for a in args if isinstance(a, BaseException))
    return False


def parse(pdf_bytes: bytes, password: str | None = None, *, profile: str) -> ParseResult:
    """Parse a statement's text layer. Raises PdfSpikeError subclasses for the documented cases."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose one of {', '.join(sorted(PROFILES))}")
    try:
        import pdfplumber
    except ImportError:
        raise PdfEngineMissing(_ENGINE_HELP) from None

    conf = PROFILES[profile]
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
            pages = list(pdf.pages)
            page_width = float(pages[0].width) if pages else 0.0
            lines: list[_Line] = []
            text_chars = 0
            for page_number, page in enumerate(pages):
                page_lines = _lines_of_page(page, page_number)
                text_chars += sum(len(line.text) for line in page_lines)
                lines.extend(page_lines)
    except PdfSpikeError:
        raise
    except Exception as exc:
        if _is_password_error(exc):
            raise PasswordRequired("This PDF is password-protected, and the password is missing or wrong. Enter "
                                   "the password your bank uses, or download the transactions as CSV or OFX "
                                   "instead.") from None
        raise

    if text_chars < 10 or not lines:
        raise NoTextLayer("This PDF has no text layer: it looks scanned, export CSV/OFX instead. The import reads "
                          "text, not pictures, so a scan needs OCR first.")

    parsed_rows, opening, closing = _parse_lines(lines, conf, page_width)
    return ParseResult(parsed_rows, None if opening is None else _cents(opening),
                       None if closing is None else _cents(closing), len(pages), text_chars)


def rows(pdf_bytes: bytes, password: str | None = None, *, profile: str) -> list[Row]:
    """The statement's transactions as importfile.Row objects (app sign: > 0 money out), in row order."""
    return parse(pdf_bytes, password, profile=profile).rows
