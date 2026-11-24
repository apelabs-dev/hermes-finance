"""Generic statement layout: header roles, column bands, rows, balances and dates from word positions.

`parse()` never repairs. It reports what it read plus the reasons it could not read something (`problems`); the
caller maps those to a user-facing reason and replay.py checks the printed balances.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from .numbers import MONTHS, Money, RawDate, parse_date_prefix, parse_money, resolve
from .words import Line, Word, build_lines

# ------------------------------------------------------------------ lexicon

ROLE_WORDS = [
    ("balance", r"\b(balance|saldo|solde|kontostand|running bal)"),
    ("debit", r"\b(withdrawals?|debits?|débits?|money out|paid out|soll|lastschrift|payments?|charges|"
              r"retraits?|cargos?|abonos? no|out)\b"),
    ("credit", r"\b(deposits?|credits?|crédits?|money in|paid in|haben|gutschrift|receipts?|dépôts?|abonos?|in)\b"),
    ("amount", r"\b(amount|betrag|importe|montant|bedrag|valor|umsatz)\b"),
    ("date", r"\b(date|datum|fecha|data|buchungstag|buchung|valuta|wert|posted|post date|trans date|jour)\b"),
    ("desc", r"\b(description|details?|particulars|narration|narrative|transactions?|verwendungszweck|"
             r"erläuterung|erl.uterung|text|libellé|libelle|concepto|reference|memo|payee|omschrijving|"
             r"opération|operation|vorgang|beschreibung|descripción|descripcion|type)\b"),
]
DEBIT_HDR = re.compile(r"\b(withdrawals?|debits?|débits?|money out|paid out|soll|payments?|retraits?)\b")
CREDIT_HDR = re.compile(r"\b(deposits?|credits?|crédits?|money in|paid in|haben|dépôts?)\b")

OPEN_RE = re.compile(
    r"(opening|beginning|previous|starting|start|prior|last statement)\s+balance|balance\s+(brought\s+forward|b/f|"
    r"forward|bfwd|at start)|brought\s+forward|anfangssaldo|alter\s+(konto)?stand|saldo\s+(anterior|inicial|vortrag)|"
    r"solde\s+(d['’]\s?ouverture|précédent|precedent|initial|reporté|reporte|ancien)|ancien\s+solde|"
    r"vorheriger\s+kontostand|beginsaldo|vorsaldo", re.I)
CLOSE_RE = re.compile(
    r"(closing|ending|new|final|current)\s+balance|balance\s+(carried\s+forward|c/f|at end)|carried\s+forward|"
    r"endsaldo|neuer\s+(konto)?stand|saldo\s+(final|actual|neu)|solde\s+(de\s+fermeture|final|nouveau|actuel|"
    r"au\s)|nouveau\s+solde|eindsaldo", re.I)
FIRST_LAST_RE = re.compile(r"\bkontostand\s+am\b|\bbalance\s+(at|as\s+of|on)\b|\bsaldo\s+(per|am|al)\b", re.I)
SKIP_RE = re.compile(
    r"^(sub\s?total|total|totals|summe|gesamt|zwischensumme|totaux?|page\s+\d|seite\s+\d|continued|"
    r"\(continued|fortsetzung|übertrag|uebertrag|daily\s+(ending\s+)?balance)|\bcontinued\b|"
    r"\bpage\s+\d+\s+(of|von|de)\s+\d+|\bseite\s+\d+\s+von\s+\d+|^\s*total\b", re.I)
PERIOD_SEP = r"(?:to|through|thru|until|bis|au|al|tot|à|-|–|—)"

CREDIT_CARD_RE = re.compile(
    r"\b(credit\s+card|card\s+statement|card\s+account|visa|mastercard|american\s+express|amex)\b", re.I)
MULTI_ACCOUNT_RE = re.compile(r"\b(account\s*(number|no\.?|#|id)|acct\.?\s*(number|no\.?|#))\b", re.I)


@dataclass
class Column:
    role: str
    x0: float
    x1: float
    hx0: float = 0.0     # header phrase extent
    hx1: float = 0.0


@dataclass
class Row:
    raw_date: RawDate | None
    date_inherited: bool
    amount: Decimal | None = None
    explicit: str | None = None
    col: str | None = None          # debit | credit | amount
    balance: Decimal | None = None
    desc: list[str] = field(default_factory=list)
    page: int = 0
    line_index: int = 0
    word_ids: list[tuple[int, int]] = field(default_factory=list)
    date: date | None = None
    section_sign: int = 0           # +1 / -1 from a section title above the header


@dataclass
class Parsed:
    rows: list[Row]
    opening: Decimal | None
    closing: Decimal | None
    period: tuple[date, date] | None
    date_order: str | None
    problems: list[str]
    columns: list[Column]
    header_text: str = ""
    debit_total: Decimal | None = None
    credit_total: Decimal | None = None
    unassigned: list[tuple[int, int]] = field(default_factory=list)
    carry_checks: list[tuple[int, Decimal]] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    year_placeholder: bool = False
    header_line: Line | None = None

    @property
    def reason(self) -> str | None:
        return self.problems[0] if self.problems else None

    def header_words(self) -> list[str]:
        """The generic words of the header line, for a template fingerprint (never merchant or bank text)."""
        lexicon = _header_lexicon()
        words = self.header_line.words if self.header_line is not None else []
        return [w.text.lower() for w in words if w.text.lower() in lexicon][:8]


# ------------------------------------------------------------------ geometry

def _gap_small(a: Word, b: Word, factor: float = 0.6) -> bool:
    return b.x0 - a.x1 < factor * min(a.h, b.h)


def merge_money(words: list[Word]) -> list[tuple[list[Word], str]]:
    """Group tokens whose money figure is split across words ('$ 12.00', '450,00 -', '1 234,56', '12.00 CR')."""
    out: list[tuple[list[Word], str]] = []
    i, n = 0, len(words)
    while i < n:
        best = None
        for j in range(min(n, i + 3), i, -1):
            grp = words[i:j]
            if j - i > 1 and not all(_gap_small(grp[k], grp[k + 1], 0.9) for k in range(len(grp) - 1)):
                continue
            joined = grp[0].text
            for k in range(1, len(grp)):
                sep = " " if re.fullmatch(r"\d{3}[.,]\d{2}.*", grp[k].text) and \
                    re.fullmatch(r"[+\-−(]?\d{1,3}", grp[k - 1].text) else ""
                joined += (sep if sep else ("" if not re.fullmatch(r"[A-Za-z]{1,2}", grp[k].text) else " ")) + \
                    grp[k].text
            if parse_money(joined) and (j - i == 1 or any(ch.isdigit() for ch in grp[-1].text + grp[0].text)):
                best = (grp, joined)
                break
        if best and len(best[0]) > 1:
            out.append(best)
            i += len(best[0])
        else:
            out.append(([words[i]], words[i].text))
            i += 1
    return out


# ------------------------------------------------------------------ header / columns

def _phrases(line: Line) -> list[list[Word]]:
    out: list[list[Word]] = []
    for w in line.words:
        if out and _gap_small(out[-1][-1], w, 0.55):
            a, b = role_of(" ".join(x.text for x in out[-1])), role_of(w.text)
            if a is None or b is None or a == b or {a, b} in ({"amount", "debit"}, {"amount", "credit"},
                                                              {"amount", "balance"}):
                out[-1].append(w)
                continue
        out.append([w])
    return out


def role_of(phrase: str) -> str | None:
    p = re.sub(r"\(.*?\)|[$€£]|\b(eur|usd|cad|aud|gbp|chf)\b|\b\d{1,2}\b", " ", (phrase or "").lower()).strip()
    if not p or re.search(r"\d", p):
        return None
    for role, rx in ROLE_WORDS:
        if re.search(rx, p):
            return role
    return None


def header_columns(line: Line, header_words: list[str] | None = None) -> list[Column] | None:
    if header_words:
        low = line.text.lower()
        if not all(h.lower() in low for h in header_words):
            return None
    cols: list[Column] = []
    for ph in _phrases(line):
        text = " ".join(w.text for w in ph)
        role = role_of(text)
        if role is None:
            continue
        cols.append(Column(role, ph[0].x0, ph[-1].x1, ph[0].x0, ph[-1].x1))
    roles = [c.role for c in cols]
    money = [r for r in roles if r in ("debit", "credit", "amount", "balance")]
    if "date" not in roles and not header_words:
        return None
    if not any(r in ("debit", "credit", "amount") for r in roles):
        return None
    if len(cols) < 3 or len(money) > 4 or len(line.words) > 14:
        return None
    return _bands(cols)


def _bands(cols: list[Column]) -> list[Column]:
    cols = sorted(cols, key=lambda c: (c.hx0 + c.hx1) / 2)
    for i, c in enumerate(cols):
        left = -1e9 if i == 0 else ((cols[i - 1].hx0 + cols[i - 1].hx1) / 2 + (c.hx0 + c.hx1) / 2) / 2
        right = 1e9 if i == len(cols) - 1 else ((c.hx0 + c.hx1) / 2 + (cols[i + 1].hx0 + cols[i + 1].hx1) / 2) / 2
        c.x0, c.x1 = left, right
    return cols


_HEADER_LEXICON = set()

def _header_lexicon() -> set[str]:
    global _HEADER_LEXICON
    if not _HEADER_LEXICON:
        for _, pattern in ROLE_WORDS:
            for word in re.findall(r"[a-zà-ÿ]{2,}", pattern):
                _HEADER_LEXICON.add(word)
        _HEADER_LEXICON.update({"balance", "date", "description", "debit", "credit", "amount", "withdrawals",
                                "deposits", "withdrawal", "deposit", "details", "transaction", "transactions"})
    return _HEADER_LEXICON


def assign(cols: list[Column], x0: float, x1: float, money: bool) -> Column | None:
    """The column for a token: a header extent it overlaps (money tokens prefer money columns), else its band."""
    xc = (x0 + x1) / 2
    cands = [c for c in cols if not (x1 < c.hx0 or x0 > c.hx1)]
    if money:
        mc = [c for c in cands if c.role in ("debit", "credit", "amount", "balance")]
        if len(mc) == 1:
            return mc[0]
        if len(mc) > 1:
            return min(mc, key=lambda c: abs((c.hx0 + c.hx1) / 2 - xc))
    for c in cols:
        if c.x0 <= xc < c.x1:
            return c
    return None


# ------------------------------------------------------------------ period / balances

def _all_dates(text: str) -> list[tuple[RawDate, int, int]]:
    out = []
    i = 0
    while i < len(text):
        if (i == 0 or not text[i - 1].isalnum()) and (text[i].isdigit() or text[i].isalpha()):
            r = parse_date_prefix(text[i:])
            if r and (r[0].kind != "num" or r[0].year is not None or r[0].sep in "/."):
                raw, rest = r
                end = len(text) - len(rest)
                if raw.kind != "num" or raw.year is not None:
                    out.append((raw, i, end))
                    i = end
                    continue
        i += 1
    return out


def find_period(lines: list[Line], cfg: dict) -> list[tuple[RawDate, RawDate]]:
    """Candidate statement periods as raw date pairs (order-agnostic), most explicit first."""
    found: list[tuple[RawDate, RawDate]] = []
    for ln in lines[:400]:
        t = ln.text
        ds = _all_dates(t)
        for k in range(len(ds) - 1):
            (a, _, ea), (b, sb, _) = ds[k], ds[k + 1]
            between = t[ea:sb].strip().lower().strip(",")
            if re.fullmatch(PERIOD_SEP, between) or between in ("", "and"):
                if between == "" and not re.search(r"period|statement|from|zeitraum|du|vom|periode", t.lower()):
                    continue
                if a.year is None and b.year is not None:
                    a = RawDate(a.kind, a.a, a.b, b.year, a.sep, a.text)
                found.append((a, b))
        m = re.search(r"\b([A-Za-zÀ-ÿ]{3,9})\.?\s+(\d{1,2})\s*[-–]\s*(\d{1,2}),?\s+(\d{4})\b", t)
        m2 = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\.?\s+([A-Za-zÀ-ÿ]{3,9})\.?,?\s+(\d{4})\b", t)
        for mm, (mo_i, a_i, b_i, y_i) in ((m, (1, 2, 3, 4)), (m2, (3, 1, 2, 4))):
            if mm and mm.group(mo_i).lower() in MONTHS:
                mth, yr = MONTHS[mm.group(mo_i).lower()], int(mm.group(y_i))
                found.append((RawDate("text", int(mm.group(a_i)), mth, yr, " ", ""),
                              RawDate("text", int(mm.group(b_i)), mth, yr, " ", "")))
        # "Kontoauszug 10/2021", "Statement 10/2021"
        m = re.search(r"(kontoauszug|statement|auszug)\s+(\d{1,2})\s*/\s*(\d{4})\b", t, re.I)
        if m:
            mth, yr = int(m.group(2)), int(m.group(3))
            if 1 <= mth <= 12:
                end = (date(yr + (mth == 12), mth % 12 + 1, 1) - timedelta(days=1)).day
                found.append((RawDate("ymd", mth, 1, yr, "-", ""), RawDate("ymd", mth, end, yr, "-", "")))
        # "1 June - 30 June" with no year: two text dates with no year, joined by a dash
        m = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]{3,9})\.?\s*[-–]\s*(\d{1,2})\s+([A-Za-zÀ-ÿ]{3,9})\.?(?!\s*,?\s*\d{4})",
                      t)
        if m and m.group(2).lower() in MONTHS and m.group(4).lower() in MONTHS:
            found.append((RawDate("text", int(m.group(1)), MONTHS[m.group(2).lower()], None, " ", ""),
                          RawDate("text", int(m.group(3)), MONTHS[m.group(4).lower()], None, " ", "")))
    return found


def balance_phrases(lines: list[Line], cfg: dict) -> tuple[list[tuple[int, Decimal, RawDate | None]],
                                                          list[tuple[int, Decimal, RawDate | None]]]:
    """(opening candidates, closing candidates) as (line index, value, raw date on the line)."""
    o_re = re.compile(cfg["opening_pattern"], re.I) if cfg.get("opening_pattern") else OPEN_RE
    c_re = re.compile(cfg["closing_pattern"], re.I) if cfg.get("closing_pattern") else CLOSE_RE
    opens, closes, firstlast = [], [], []
    for li, ln in enumerate(lines):
        t = ln.text
        hit = False
        for rx, bucket in ((o_re, opens), (c_re, closes), (FIRST_LAST_RE, firstlast)):
            m = rx.search(t)
            if not m or (hit and rx is FIRST_LAST_RE):
                continue
            hit = True
            toks = merge_money(ln.words)
            pos, val = 0, None
            for grp, txt in toks:
                start = t.find(grp[0].text, pos)
                pos = max(pos, start)
                if start >= m.end() - 1 or grp[0].x0 > ln.words[0].x0 and start >= m.start():
                    mo = parse_money(txt)
                    if mo and start >= m.end() - 1:
                        val = mo.value
                        break
            if val is None and li + 1 < len(lines) and lines[li + 1].page == ln.page:
                offs, pos2 = [], 0
                for w in ln.words:
                    offs.append(pos2)
                    pos2 += len(w.text) + 1
                inside = [w for w, o in zip(ln.words, offs) if m.start() <= o < m.end()]
                ph_x0 = inside[0].x0 if inside else ln.words[0].x0
                ph_x1 = inside[-1].x1 if inside else ln.words[0].x1
                best_d = None
                for grp, txt in merge_money(lines[li + 1].words):
                    mo = parse_money(txt)
                    if mo and grp[0].x0 < ph_x1 + 20 and grp[-1].x1 > ph_x0 - 20:
                        d = abs((grp[0].x0 + grp[-1].x1) / 2 - (ph_x0 + ph_x1) / 2)
                        if best_d is None or d < best_d:
                            best_d, val = d, mo.value
                if val is not None and any(parse_money(tx) for _, tx in merge_money(ln.words)):
                    val = None
            if val is not None:
                ds = _all_dates(t[m.end():])
                bucket.append((li, val, ds[0][0] if ds else None))
    if firstlast and not opens and not closes and len(firstlast) >= 2:
        opens.append(firstlast[0])
        closes.append(firstlast[-1])
    return opens, closes


# ------------------------------------------------------------------ main

def parse(pages: list[list[dict]], sizes: list[tuple[float, float]] | None = None, config: dict | None = None,
          *, year: int | None = None) -> Parsed:
    cfg = dict(config or {})
    if cfg.get("x_min") is not None:
        pages = [[w for w in pg if w["x0"] >= cfg["x_min"]] for pg in pages]
    lines = build_lines(pages)
    problems: list[str] = []
    skip_extra = re.compile(cfg["skip_pattern"], re.I) if cfg.get("skip_pattern") else None
    rows: list[Row] = []
    unassigned: list[tuple[int, int]] = []
    carry: list[tuple[int, Decimal]] = []
    cols: list[Column] | None = None
    header_text = ""
    header_line: Line | None = None
    opens, closes = balance_phrases(lines, cfg)
    open_lines = {o[0] for o in opens}
    close_lines = {c[0] for c in closes}
    debit_total = credit_total = None
    in_table = False
    ended = False
    pending: Row | None = None
    last_header_page = -1
    rows_at_header = 0
    prev_line: Line | None = None
    row_pitch: list[float] = []
    awaiting_page = False
    section_sign = 0
    fixed_cols = None
    if cfg.get("columns"):
        fixed_cols = [Column(c["role"], -1e9, 1e9, c["x0"], c["x1"]) for c in cfg["columns"]]
        fixed_cols = _bands(fixed_cols)

    for li, ln in enumerate(lines):
        if ended:
            break
        if fixed_cols and cfg.get("table_start_pattern") and not in_table and not rows \
                and re.search(cfg["table_start_pattern"], ln.text, re.I):
            cols, in_table, last_header_page, prev_line, awaiting_page = fixed_cols, True, ln.page, ln, False
            header_text = header_text or "(table start)"
            header_line = header_line or ln
            continue
        if fixed_cols and cfg.get("header_words"):
            low_ln = ln.text.lower()
            hc = fixed_cols if (all(h.lower() in low_ln for h in cfg["header_words"]) and len(ln.words) <= 20
                                and (len(cfg["header_words"]) >= 2 or not any(parse_money(w.text) for w in ln.words))) \
                else None
        else:
            hc = header_columns(ln, cfg.get("header_words"))
        if hc is not None:
            section_sign = _section_sign(lines, li)
            if (rows and ln.page == last_header_page and rows[-1].page == ln.page and section_sign == 0
                    and any(c.role == "balance" for c in hc) and len(rows) > rows_at_header):
                problems.append("multiple_tables")
            rows_at_header = len(rows)
            cols = fixed_cols or hc
            header_text = header_text or ln.text
            header_line = header_line or ln
            in_table = True
            last_header_page = ln.page
            prev_line = ln
            awaiting_page = False
            continue
        if not in_table or cols is None:
            continue
        if cfg.get("table_end_y") is not None and ln.top >= float(cfg["table_end_y"]) - 0.5:
            ended = True
            continue
        if prev_line is not None and ln.page == prev_line.page and rows:
            pitch = median(row_pitch) if row_pitch else 12.0
            if ln.top - prev_line.top > max(4 * pitch, 40):
                in_table = False
                continue
        if prev_line is not None and ln.page != prev_line.page:
            awaiting_page = True
        if awaiting_page:
            lead = " ".join(w.text for w in ln.words[:4])
            if not parse_date_prefix(lead):
                continue
            awaiting_page = False
        if prev_line is not None and ln.page == prev_line.page:
            row_pitch.append(ln.top - prev_line.top)
        prev_line = ln
        text = ln.text
        low = text.lower().strip()
        is_open = li in open_lines
        is_close = li in close_lines
        toks = merge_money(ln.words)
        money: list[tuple[Column | None, Money, list[Word]]] = []
        for grp, txt in toks:
            mo = parse_money(txt)
            if mo is None:
                continue
            col = assign(cols, grp[0].x0, grp[-1].x1, True)
            if col is not None and col.role == "date" and len(grp) == 1 and \
                    re.fullmatch(r"\d{1,2}[./]\d{1,2}\.?", txt) and parse_date_prefix(txt) is not None:
                continue
            money.append((col, mo, grp))
        raw = None
        date_words: set[int] = set()
        first = ln.words[0]
        date_cols = [c for c in cols if c.role == "date"]
        dcol = date_cols[0] if date_cols else None
        in_date_band = dcol is None or dcol.x0 <= first.xc < dcol.x1 or first.x0 <= dcol.hx1
        if in_date_band:
            lead = " ".join(w.text for w in ln.words[:4])
            r = parse_date_prefix(lead)
            if r:
                raw = r[0]
                used = 0
                for w in ln.words[:4]:
                    if used >= len(raw.text):
                        break
                    date_words.add(id(w))
                    used += len(w.text) + 1
        if is_open or is_close or SKIP_RE.search(low) or (skip_extra and skip_extra.search(low)):
            if is_close and rows:
                if re.search(r"carried\s+forward|c/f|übertrag", low):
                    vals = [m for c, m, g in money]
                    if vals:
                        carry.append((len(rows) - 1, vals[-1].value))
                    continue
                ended = True
            elif is_open and rows:
                vals = [m for c, m, g in money]
                if vals:
                    carry.append((len(rows) - 1, vals[-1].value))
            if re.match(r"^\s*(total|totals|summe)\b", low):
                for c, m, g in money:
                    if c and c.role == "debit":
                        debit_total = abs(m.value)
                    elif c and c.role == "credit":
                        credit_total = abs(m.value)
            continue
        if header_columns(ln) is None and cfg.get("table_end_pattern") and \
                re.search(cfg["table_end_pattern"], low, re.I):
            ended = True
            continue
        if rows and raw is None and len(money) >= 3 and (re.search(r"[=+]\s", text) or "check" in low):
            in_table = False
            continue
        txn = [(c, m, g) for c, m, g in money if c and c.role in ("debit", "credit", "amount")]
        if not txn and (OPEN_RE.search(text) or CLOSE_RE.search(text)):
            continue
        bal = [(c, m, g) for c, m, g in money if c and c.role == "balance"]
        stray = [(c, m, g) for c, m, g in money if not c or c.role not in ("debit", "credit", "amount", "balance")]
        for c, m, g in stray:
            if c is not None and c.role == "desc":
                continue
            if raw is not None and any(w is ln.words[0] for w in g):
                continue
            unassigned.extend((w.page, w.wid) for w in g)
        money = [(c, m, g) for c, m, g in money if c is not None and c.role in ("debit", "credit", "amount",
                                                                                 "balance")]
        if len(txn) > 1:
            problems.append("ambiguous_amounts")
        if len(bal) > 1:
            problems.append("ambiguous_balance")
        row_start = raw is not None or (txn and cfg.get("row_start", "date_or_amount") == "date_or_amount")
        if raw is not None and pending is not None and pending.amount is None:
            problems.append("row_without_amount")
        if row_start:
            if raw is None and not rows and pending is None:
                problems.append("first_row_without_date")
            if raw is None and any(c.x0 <= w.xc < c.x1 for w in ln.words for c in date_cols
                                   if not any(w is x for _, _, g in money for x in g)):
                problems.append("unreadable_date")
            r = Row(raw_date=raw if raw is not None else (rows[-1].raw_date if rows else None),
                    date_inherited=raw is None, page=ln.page, line_index=li, section_sign=section_sign)
            pending = r
            rows.append(r)
            desc_words = [w.text for w in ln.words if id(w) not in date_words
                          and not any(w is x for _, _, g in money for x in g)]
            r.desc.append(" ".join(desc_words))
        elif pending is not None and (txn or bal):
            if txn and pending.amount is not None:
                problems.append("unassigned_money")
            r = pending
        else:
            if pending is not None and not money:
                pending.desc.append(text)
            elif money and pending is None:
                unassigned.extend((w.page, w.wid) for _, _, g in money for w in g)
            continue
        if txn:
            c, m, g = txn[0]
            if r.amount is not None:
                problems.append("unassigned_money")
            r.amount, r.explicit, r.col = m.value, m.explicit, c.role
            r.word_ids.extend((w.page, w.wid) for w in g)
        if bal:
            c, m, g = bal[0]
            if r.balance is not None:
                problems.append("unassigned_money")
            r.balance = m.value
            r.word_ids.extend((w.page, w.wid) for w in g)

    if cols is None:
        problems.append("no_header")
    if pending is not None and pending.amount is None:
        problems.append("row_without_amount")
    if unassigned:
        problems.append("unassigned_money")

    _apply_signs(rows, cfg, problems)
    order, period, placeholder = _resolve_dates(rows, lines, cfg, problems, opens, closes, year)

    opening = closing = None
    first_li = rows[0].line_index if rows else 10**9
    last_li = rows[-1].line_index if rows else -1
    ovals = sorted({v for li, v, _ in opens if li < first_li})
    cvals = sorted({v for li, v, _ in closes if li < first_li or li > last_li})
    if cfg.get("user_opening") is not None:
        ovals = [Decimal(str(cfg["user_opening"]))]
    if cfg.get("user_closing") is not None:
        cvals = [Decimal(str(cfg["user_closing"]))]
    if len(ovals) > 1:
        problems.append("opening_conflict")
    elif ovals:
        opening = ovals[0]
    if len(cvals) > 1:
        problems.append("closing_conflict")
    elif cvals:
        closing = cvals[0]
    return Parsed(rows, opening, closing, period, order, list(dict.fromkeys(problems)), cols or [], header_text,
                  debit_total, credit_total, unassigned, carry, lines, placeholder, header_line)


SECTION_POS = re.compile(r"\b(deposits?|credits?|additions?|incoming|money in|gutschriften|einzahlungen|"
                         r"dépôts|crédits)\b", re.I)
SECTION_NEG = re.compile(r"\b(withdrawals?|debits?|checks?|cheques?|fees?|charges|payments?|purchases?|outgoing|"
                         r"money out|subtractions?|lastschriften|retraits|débits)\b", re.I)


def _section_sign(lines: list[Line], li: int) -> int:
    """+1 / -1 when the 1-3 lines just above a header (same page) name a one-sided section; 0 otherwise."""
    for k in range(li - 1, max(-1, li - 4), -1):
        ln = lines[k]
        if ln.page != lines[li].page:
            break
        t = ln.text
        if any(parse_money(w.text) for w in ln.words):
            break
        pos, neg = bool(SECTION_POS.search(t)), bool(SECTION_NEG.search(t))
        if pos != neg:
            return 1 if pos else -1
        if pos and neg:
            return 0
    return 0


def _apply_signs(rows: list[Row], cfg: dict, problems: list[str]) -> None:
    conv = cfg.get("amount_sign")
    amt_rows = [r for r in rows if r.col == "amount" and not (r.explicit is None and r.section_sign)]
    if amt_rows and conv is None:
        neg = sum(1 for r in amt_rows if r.explicit == "neg")
        pos = sum(1 for r in amt_rows if r.explicit == "pos")
        uns = sum(1 for r in amt_rows if r.explicit is None)
        if uns == 0:
            conv = "explicit"
        elif neg and not pos:
            conv = "unsigned_is_credit"
        elif pos and not neg:
            conv = "unsigned_is_debit"
        else:
            problems.append("ambiguous_sign")
    for r in rows:
        if r.amount is None:
            continue
        if r.col == "debit":
            r.amount = -abs(r.amount)
        elif r.col == "credit":
            r.amount = abs(r.amount) if r.explicit != "neg" else r.amount
        elif r.col == "amount":
            if r.explicit is None and r.section_sign and cfg.get("amount_sign") is None:
                r.amount = abs(r.amount) * r.section_sign
            elif r.explicit is None:
                if conv in ("unsigned_is_debit", "debit_positive"):
                    r.amount = -abs(r.amount)
                elif conv in ("unsigned_is_credit", "explicit", "credit_positive"):
                    r.amount = abs(r.amount)
            elif conv == "debit_positive":
                r.amount = -r.amount


YEAR_PLACEHOLDER = 2000   # a leap year placeholder for a statement that prints no year at all


def _period_dates(pair: tuple[RawDate, RawDate], order: str, default_year: int | None = None) -> tuple[date, date] | None:
    a, b = pair
    db = resolve(b, order, default_year)
    if db is None:
        return None
    da = resolve(a, order, a.year if a.year is not None else db.year)
    if da is None:
        return None
    if da > db and a.year is None:
        da = resolve(a, order, db.year - 1)
    if da is None or da > db or (db - da).days > 400:
        return None
    return da, db


def _resolve_dates(rows, lines, cfg, problems, opens, closes, year):
    raws = [r.raw_date for r in rows if r.raw_date is not None]
    if any(r.raw_date is None for r in rows):
        problems.append("row_without_date")
    pairs = find_period(lines, cfg)
    orders = [cfg["date_order"]] if cfg.get("date_order") else ["DMY", "MDY"]
    od = [d for _, _, d in opens if d is not None]
    cd = [d for _, _, d in closes if d is not None]
    if od and cd:
        pairs.append((od[0], cd[-1]))
    placeholder = False
    year_for_tokens = year
    if year_for_tokens is None and not any(r.year is not None for r in raws):
        year_for_tokens = YEAR_PLACEHOLDER
        placeholder = True
    viable = []
    for order in orders:
        period = None
        for p in pairs:
            period = _period_dates(p, order, year_for_tokens if cfg.get("year_placeholder") or year is not None else None)
            if period is None and year_for_tokens == YEAR_PLACEHOLDER and (p[0].year is None or p[1].year is None):
                period = _period_dates(p, order, YEAR_PLACEHOLDER)
                placeholder = period is not None
            if period:
                break
        if period is None:
            continue
        ok = True
        dates = []
        for raw in raws:
            d = _date_in_period(raw, order, period)
            if d is None:
                ok = False
                break
            dates.append(d)
        if ok:
            viable.append((order, period, dates))
    if not viable and raws and year_for_tokens is not None and (pairs or not placeholder):
        # no printed period: an explicit year (or a year on every token) fixes the dates; the period is their span.
        # A placeholder year never invents a period: with no period at all the statement refuses no_period.
        for order in orders:
            ds = [resolve(r, order, year_for_tokens) for r in raws]
            if all(ds):
                ordered = all(a <= b for a, b in zip(ds, ds[1:])) or all(a >= b for a, b in zip(ds, ds[1:]))
                if ordered or cfg.get("period_from_rows"):
                    viable.append((order, (min(ds), max(ds)), ds))
    if not raws:
        return None, None, placeholder
    if not viable:
        problems.append("no_period" if not pairs else "date_outside_period")
        return None, None, placeholder
    if len(viable) > 1:
        if all(r.kind != "num" for r in raws):
            viable = viable[:1]
        elif all(r.sep == "." for r in raws if r.kind == "num"):
            viable = [v for v in viable if v[0] == "DMY"]
        else:
            problems.append("ambiguous_date_order")
            return None, None, placeholder
    order, period, dates = viable[0]
    it = iter(dates)
    for r in rows:
        if r.raw_date is not None:
            r.date = next(it)
    return order, period, placeholder


def _date_in_period(raw: RawDate, order: str, period: tuple[date, date]) -> date | None:
    start, end = period
    for y in ([raw.year] if raw.year is not None else sorted({start.year, end.year})):
        d = resolve(raw, order, y)
        if d is not None and start <= d <= end:
            return d
    return None


def credit_card_lines(lines: list[Line]) -> bool:
    """Whether the statement's own opening lines say it is a card statement (not a card payment row or a guide)."""
    for ln in lines[:8]:
        if any(parse_date_prefix(w.text) for w in ln.words) or any(parse_money(w.text) for w in ln.words):
            break
        if CREDIT_CARD_RE.search(ln.text) and not re.search(r"\b(checking|chequing|savings)\b", ln.text, re.I):
            return True
    return False


def account_count(lines: list[Line]) -> int:
    """Different account names/numbers printed near the top, or more than one balance chain (0 when unclear)."""
    seen: set[str] = set()
    for ln in lines[:80]:
        if MULTI_ACCOUNT_RE.search(ln.text):
            numbers = tuple(re.findall(r"\d{3,}", ln.text))
            if numbers:
                seen.add("|".join(numbers))
    return len(seen)
