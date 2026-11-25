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
import math
import re
import unicodedata
from dataclasses import dataclass
from statistics import median

log = logging.getLogger(__name__)

_DEVICE = {"DeviceGray", "DeviceRGB", "DeviceCMYK"}

# A real statement is a few dozen pages: 40 pages measured 127 MB peak and 0.9 s here. The review's probes were
# 12,000 pages in 1.1 MB (10.2 s, 1.28 GB) and 0.9 KB that unpacked to 333 MB, so the caps leave room for a long
# statement while refusing those. The page count is checked before any page is read; the word count as pages come in.
MAX_PAGES = 50
MAX_WORDS = 40_000

# Characters, not words: pdfplumber has no length limit of its own, and a ToUnicode CMap can map one glyph to 10,000
# characters, so a 1.1 KB file prints a 1,000,000-character word. The word and page caps counted words only, so the
# read passed it and the parent's role_of / _phrases / build_lines then spent minutes on it on the request thread.
# A statement word is a few dozen characters; a dense 50-page statement at the word cap is ~320,000 characters.
MAX_WORD_CHARS = 200
MAX_CHARS = 2_000_000

# A text-drawn rule or dot leader (".......", "________", "========"): layout, never data. A statement prints one
# across the page, and at 6-7 pt a full-width rule passes 200 characters, so refusing every over-cap word refused a
# whole normal statement that carries one (21ba046 parsed it, the cap refused it as pdf_too_complex).
RULE_CHARS = frozenset(".-_=*~·•…‥—–‐‑‒―─━▁")


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
_SPACE = re.compile(r"\s+", re.UNICODE)


class WordReadError(Exception):
    """The bytes could not be read as a PDF (damaged, or an encryption this build does not support)."""


class PasswordRequired(WordReadError):
    """The PDF is encrypted and no password was supplied."""


class PasswordWrong(WordReadError):
    """The PDF is encrypted and the supplied password did not open it."""


class TooComplex(WordReadError):
    """The PDF has more pages or words than a statement can, or the read used more time or memory than it may."""


def _failed(exc: BaseException) -> None:
    """One log line for a read that failed: the exception's TYPE only. Its message can quote the PDF's own bytes."""
    log_failure(type(exc).__name__)


def log_failure(name: str) -> None:
    """The guard's child reports the exception type it caught; this keeps the parent's log line the same shape."""
    log.warning("pdf read failed: %s", name)


def _read_error(exc: BaseException) -> WordReadError:
    """The public WordReadError for a raw failure, carrying the cause's type for the parent's one log line."""
    err = WordReadError()
    err.cause = type(exc).__name__
    _failed(exc)
    return err


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
            raise _read_error(exc) from None
        raise _read_error(exc) from None


def read(raw: bytes, password: str | None = None, *, max_pages: int = MAX_PAGES,
         max_words: int = MAX_WORDS, max_word_chars: int = MAX_WORD_CHARS,
         max_chars: int = MAX_CHARS) -> tuple[list[list[dict]], list[tuple[float, float]]]:
    """(pages of {text,x0,x1,top,bottom}, page sizes) for a statement PDF. Never writes a file.

    The whole read is one guard: a page tree that raises, a `/Rotate` that is a string, a MediaBox that is infinite
    all become WordReadError (never a raw pdfminer exception on the request thread), and coordinates that are not
    finite are dropped rather than carried to `round()` later. Over the page, word, word-length or total-character
    cap raises TooComplex before (pages) or while (words) the pages are extracted: the characters are what the
    parent's parser walks, so bounding only words bounds nothing.
    """
    pages: list[list[dict]] = []
    sizes: list[tuple[float, float]] = []
    count = 0
    chars = 0
    try:
        pdf = _open(raw, password)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                page_list = pdf.pages
                if len(page_list) > max_pages:
                    raise TooComplex()
                for page in page_list:
                    try:
                        p = page.dedupe_chars(tolerance=1).filter(
                            lambda o: not (o.get("object_type") == "char" and o.get("ncs") in _DEVICE
                                           and _white(o.get("non_stroking_color"))))
                        found = p.extract_words(x_tolerance=1.5, y_tolerance=2.5, keep_blank_chars=False)
                        width, height = float(page.width), float(page.height)
                        if not (math.isfinite(width) and math.isfinite(height)):
                            raise ValueError("non-finite page size")
                    except TooComplex:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one broken page must not be a traceback
                        raise _read_error(exc) from None
                    kept = []
                    for w in found:
                        text = w["text"]
                        if len(text) > max_word_chars:
                            if set(text) <= RULE_CHARS:      # a drawn rule or dot leader, not a word of the statement
                                continue
                            raise TooComplex()
                        try:
                            box = (float(w["x0"]), float(w["x1"]), float(w["top"]), float(w["bottom"]))
                        except (KeyError, TypeError, ValueError, OverflowError):
                            continue
                        if all(math.isfinite(v) for v in box):
                            kept.append({"text": text, "x0": box[0], "x1": box[1], "top": box[2],
                                         "bottom": box[3]})
                            chars += len(text)
                            if chars > max_chars:
                                raise TooComplex()
                    count += len(kept)
                    if count > max_words:
                        raise TooComplex()
                    pages.append(kept)
                    sizes.append((width, height))
        finally:
            with contextlib.suppress(Exception):
                pdf.close()
    except (TooComplex, PasswordRequired, PasswordWrong):
        raise
    except WordReadError:
        raise
    except Exception as exc:  # noqa: BLE001 - the page tree, metadata and geometry all live here
        raise _read_error(exc) from None
    return pages, sizes


def producer(raw: bytes, password: str | None = None) -> str:
    """The PDF Producer metadata, masked (a secondary layout signal only). A malformed file answers ""."""
    try:
        pdf = _open(raw, password)
        try:
            meta = pdf.metadata or {}
            return mask_text(str(meta.get("Producer") or ""))[:40]
        finally:
            with contextlib.suppress(Exception):
                pdf.close()
    except (PasswordRequired, PasswordWrong):
        raise
    except Exception as exc:  # noqa: BLE001 - metadata is a signal, never a reason to refuse the statement
        _failed(exc)
        return ""


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
        ysum = 0.0
        for w in ws:
            c = (w.top + w.bottom) / 2
            if cur and abs(c - cy) > tol:
                out.append(Line(pno, sorted(cur, key=lambda v: v.x0)))
                cur = []
                ysum = 0.0
            ysum += c                       # running sum: recomputing the mean for every word was O(words^2) a line
            cur.append(w)
            cy = ysum / len(cur)
        if cur:
            out.append(Line(pno, sorted(cur, key=lambda v: v.x0)))
    return out


def _mask_word(m: re.Match, keep: bool) -> str:
    w = m.group(0)
    if keep and w.lower() in KEEP_WORDS:
        return w
    return "".join("X" if c.isupper() else "x" for c in w)


def mask_text(text: str, *, keep: bool = True) -> str:
    """Shape only: digits -> 9, letters of words outside the generic lexicon -> x/X. Punctuation kept.

    `keep` is for the two places a list word is structural (a column header, a balance line). Everywhere else a
    merchant or a person can be called "New" or "Money", so the AI view masks the lexicon too; combining marks
    (Indic vowel signs, which `\\w` leaves out) are masked with them.

    Whitespace inside one word (a crafted ToUnicode CMap maps one glyph to `9999\\n9999`, reachable only as one
    word) becomes a single space: a view is one line per `Line`, and a word's own line break split it (L2).
    """
    shaped = _WORD.sub(lambda m: _mask_word(m, keep), _DIGIT.sub("9", text or ""))
    if any(unicodedata.category(c).startswith("M") for c in shaped):
        shaped = "".join("x" if unicodedata.category(c).startswith("M") else c for c in shaped)
    if _SPACE.search(shaped):                   # rare: a statement's own words never hold whitespace
        shaped = _SPACE.sub(" ", shaped)
    return shaped
