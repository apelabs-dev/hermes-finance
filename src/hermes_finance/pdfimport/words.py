"""The statement's text layer as positioned words, and the masked shape view a model may see.

Extraction fixes measured on real statements:
- `dedupe_chars` removes the doubled glyphs of fake-bold text ("MMeemmbbeerr").
- x_tolerance 1.5 (pdfplumber's default is 3) stops words without a space glyph from merging
  ("1DraftsClearedfor$1,894.61-").
- y_tolerance 2.5 keeps a line together when its glyphs sit a little off the baseline.
- White glyphs in DeviceGray / DeviceRGB / DeviceCMYK are dropped: some statements draw a white "-" after every
  figure, which read as text makes every amount negative. Other colour spaces report raw tints (DeviceN: 1.0 is
  ink, not white), so only these three are judged; a stray artifact there stays a recorded limitation.

No PyMuPDF, no OCR, no temp files: the bytes and the password stay in memory.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
from dataclasses import dataclass
from statistics import median

_DEVICE = {"DeviceGray", "DeviceRGB", "DeviceCMYK"}

# pdfminer logs every glyph and its position at DEBUG: statement text, descriptions and amounts in the app's log.
# The importer must never write those anywhere, so its loggers stay at WARNING (like ofxtools in importfile.py).
for _name in ("pdfminer", "pdfplumber"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# Generic words that may stay readable in a masked shape view: column names, balance phrases, month names and
# currency codes. Nothing here can be a merchant, a bank or a person.
KEEP_WORDS = {
    "date", "datum", "fecha", "data", "valuta", "value", "posted", "posting", "post", "trans", "transaction",
    "transactions", "booking", "buchung", "buchungstag", "wert", "description", "details", "detail", "particulars",
    "narration", "narrative", "memo", "reference", "ref", "verwendungszweck", "text", "beschreibung", "concepto",
    "descripcion", "libelle", "libellé", "opération", "operation", "type", "withdrawals", "withdrawal", "deposits",
    "deposit", "debit", "debits", "credit", "credits", "dr", "cr", "amount", "amounts", "betrag", "importe", "montant",
    "soll", "haben", "lastschrift", "gutschrift", "balance", "balances", "saldo", "solde", "kontostand", "running",
    "ledger", "available", "cheque", "check", "checks", "chq", "no", "no.", "number", "paid", "in", "out", "money",
    "fees", "fee", "charges", "interest", "total", "totals", "subtotal", "sum", "summe",
    "opening", "closing", "beginning", "ending", "previous", "new", "start", "end", "starting", "brought", "carried",
    "forward", "fwd", "b/f", "c/f", "continued", "page", "of", "statement", "period", "from", "to", "the", "on", "at",
    "as", "and", "for", "account", "summary", "anfangssaldo", "endsaldo", "alter", "neuer", "kontoauszug", "seite",
    "von", "bis", "ouverture", "fermeture", "inicial", "final", "anterior", "actual",
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "january", "february",
    "march", "april", "june", "july", "august", "september", "october", "november", "december", "januar", "februar",
    "märz", "mai", "juni", "juli", "oktober", "dezember", "januari", "februari", "maart", "mrt", "mei", "augustus",
    "usd", "eur", "cad", "aud", "gbp", "inr", "sgd", "chf", "hkd", "nzd", "jpy",
}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_DIGIT = re.compile(r"\d")


class WordReadError(Exception):
    """The bytes could not be read as a PDF (damaged, or an encryption this build does not support)."""


class PasswordRequired(WordReadError):
    """The PDF is encrypted and no password was supplied."""


class PasswordWrong(WordReadError):
    """The PDF is encrypted and the supplied password did not open it."""


def available() -> bool:
    """Whether the optional PDF extra (pdfplumber) can be imported."""
    try:
        import pdfplumber  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install is the same as a missing one to the user
        return False
    return True


def _white(color) -> bool:
    if color is None:
        return False
    if isinstance(color, (int, float)):
        color = (color,)
    try:
        vals = [float(v) for v in color]
    except (TypeError, ValueError):
        return False
    if len(vals) == 1:
        return vals[0] >= 0.99
    if len(vals) == 3:
        return min(vals) >= 0.99
    if len(vals) == 4:
        return max(vals) <= 0.01
    return False


def _open(raw: bytes, password: str | None):
    import pdfplumber

    try:
        return pdfplumber.open(io.BytesIO(raw), password=password)
    except Exception as exc:  # noqa: BLE001 - the pdfminer exception classes move between releases
        names = " ".join(type(a).__name__ for a in (exc, *getattr(exc, "args", ()))).lower()
        if "password" in names:
            if isinstance(password, str) and password:
                raise PasswordWrong() from None
            raise PasswordRequired() from None
        if "encryption" in names:
            raise WordReadError() from None
        raise WordReadError() from None


def read(raw: bytes, password: str | None = None) -> tuple[list[list[dict]], list[tuple[float, float]]]:
    """(pages of {text,x0,x1,top,bottom}, page sizes) for a statement PDF. Never writes a file."""
    pages: list[list[dict]] = []
    sizes: list[tuple[float, float]] = []
    pdf = _open(raw, password)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            for page in pdf.pages:
                try:
                    p = page.dedupe_chars(tolerance=1).filter(
                        lambda o: not (o.get("object_type") == "char" and o.get("ncs") in _DEVICE
                                       and _white(o.get("non_stroking_color"))))
                    words = p.extract_words(x_tolerance=1.5, y_tolerance=2.5, keep_blank_chars=False)
                except Exception:  # noqa: BLE001 - one broken page must not be a traceback
                    raise WordReadError() from None
                pages.append([{"text": w["text"], "x0": float(w["x0"]), "x1": float(w["x1"]),
                               "top": float(w["top"]), "bottom": float(w["bottom"])} for w in words])
                sizes.append((float(page.width), float(page.height)))
    finally:
        with contextlib.suppress(Exception):
            pdf.close()
    return pages, sizes


def producer(raw: bytes, password: str | None = None) -> str:
    """The PDF Producer metadata, masked (a secondary layout signal only)."""
    pdf = _open(raw, password)
    try:
        meta = pdf.metadata or {}
        return mask_text(str(meta.get("Producer") or ""))[:40]
    except Exception:  # noqa: BLE001
        return ""
    finally:
        with contextlib.suppress(Exception):
            pdf.close()


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int
    wid: int            # index into the page's word list

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def h(self) -> float:
        return max(self.bottom - self.top, 0.1)


@dataclass
class Line:
    page: int
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)


def build_lines(pages: list[list[dict]]) -> list[Line]:
    """Words grouped into visual lines, page by page, each line's words left to right."""
    out: list[Line] = []
    for pno, words in enumerate(pages):
        ws = [Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], pno, i)
              for i, w in enumerate(words) if w["text"].strip()]
        if not ws:
            continue
        tol = max(0.8, 0.45 * median(w.h for w in ws))
        ws.sort(key=lambda w: ((w.top + w.bottom) / 2, w.x0))
        cur: list[Word] = []
        cy = None
        for w in ws:
            c = (w.top + w.bottom) / 2
            if cur and abs(c - cy) > tol:
                out.append(Line(pno, sorted(cur, key=lambda v: v.x0)))
                cur = []
            if not cur:
                cy = c
            cur.append(w)
            cy = sum((v.top + v.bottom) / 2 for v in cur) / len(cur)
        if cur:
            out.append(Line(pno, sorted(cur, key=lambda v: v.x0)))
    return out


def _mask_word(m: re.Match) -> str:
    w = m.group(0)
    if w.lower() in KEEP_WORDS:
        return w
    return "".join("X" if c.isupper() else "x" for c in w)


def mask_text(text: str) -> str:
    """Shape only: digits -> 9, letters of words outside the generic lexicon -> x/X. Punctuation kept."""
    return _WORD.sub(_mask_word, _DIGIT.sub("9", text or ""))
