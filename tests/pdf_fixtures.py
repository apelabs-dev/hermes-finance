"""Synthetic statement PDFs for the PDF import tests (fpdf2). Fictional names and figures only; no real statement."""

from __future__ import annotations

from fpdf import FPDF

PAGE = (612, 792)
COLUMNS = (40, 120, 300, 380, 460)      # date, description, debit, credit, balance
HEADER = ("Date", "Description", "Debit", "Credit", "Balance")


def pdf(pages: list[list[tuple[float, float, str]]], *, page=PAGE, password: str | None = None,
        font_size: float = 9.0) -> bytes:
    """One or more pages of text placed at (x, baseline y). Encrypted when `password` is given."""
    doc = FPDF(unit="pt", format=page)
    doc.set_auto_page_break(False)
    doc.set_margins(0, 0, 0)
    for lines in pages:
        doc.add_page()
        doc.set_font("Helvetica", size=font_size)
        for x, y, text in lines:
            doc.text(x, y, text)
    if password is not None:
        doc.set_encryption(owner_password="synthetic-owner", user_password=password)
    return bytes(doc.output())


def _row(x0: float, y: float, cells: list[tuple[float, str]]) -> list[tuple[float, float, str]]:
    return [(x, y, text) for x, text in cells if text]


def statement(rows, *, period="Statement period 01/03/2024 to 31/03/2024", opening="Opening balance 100.00",
              closing="Closing balance {last}", header=HEADER, header_y: float = 60,
              columns=COLUMNS,
              extra: list[tuple[float, float, str]] | None = None,
              after: list[list[tuple[float, float, str]]] | None = None,
              page=PAGE, password: str | None = None, row_y: float | None = None) -> bytes:
    """A one-account statement. Each row has one cell per header word; '' leaves a cell empty.

    A description cell with a newline continues on the following lines at the same x. `after` adds pages after
    page one. The default rows are (date, description, debit, credit, balance).
    """
    lines: list[tuple[float, float, str]] = []
    if period:
        lines.append((40, 20, period))
    if opening:
        lines.append((40, 34, opening))
    top = header_y
    width = len(header) if header else 5
    xs = tuple(columns)[:width]
    if header:
        lines += _row(40, top, [(x, text) for x, text in zip(xs, header)])
    top += 14
    last_balance = ""
    for row in rows:
        first = True
        desc = str(row[1]) if width > 1 else ""
        for part in desc.split("\n"):
            cells = []
            for i, (x, cell) in enumerate(zip(xs, row)):
                if not first and i != 1:
                    continue
                if i == 1:
                    cells.append((x, part))
                elif cell:
                    cells.append((x, str(cell)))
            lines += _row(40, top, cells)
            if first and row and row[-1]:
                last_balance = str(row[-1])
            top += 14 if row_y is None else row_y
            first = False
    if extra:
        for x, y, text in extra:
            lines.append((x, y, text))
    if closing:
        top += 16
        lines.append((40, top, closing.format(last=last_balance or "0.00")))
    pages = [lines] + list(after or [])
    return pdf(pages, page=page, password=password)


def two_accounts() -> bytes:
    """Two balance tables on one page: not one account per PDF."""
    lines: list[tuple[float, float, str]] = [(40, 20, "Statement period 01/03/2024 to 31/03/2024")]
    lines.append((40, 34, "Opening balance 100.00"))
    lines += _row(40, 60, [(x, text) for x, text in zip(COLUMNS, HEADER)])
    lines += _row(40, 74, [(40, "05/03/2024"), (120, "Coffee"), (300, "10.00"), (460, "90.00")])
    lines += _row(40, 100, [(x, text) for x, text in zip(COLUMNS, HEADER)])
    lines += _row(40, 114, [(40, "06/03/2024"), (120, "Other"), (300, "5.00"), (460, "20.00")])
    lines.append((40, 150, "Closing balance 20.00"))
    return pdf([lines])


def blank_page() -> bytes:
    """A scan with no text layer."""
    return pdf([[]])


def credit_card() -> bytes:
    lines = [(40, 20, "Credit Card Statement"), (40, 34, "Statement period 01/03/2024 to 31/03/2024"),
             (40, 48, "Opening balance 500.00")]
    lines += _row(40, 70, [(x, text) for x, text in zip(COLUMNS, HEADER)])
    lines += _row(40, 84, [(40, "05/03/2024"), (120, "Shop"), (300, "10.00"), (460, "490.00")])
    lines.append((40, 120, "Closing balance 490.00"))
    return pdf([lines])


def plain(rows, *, period="Statement period 01/03/2024 to 31/03/2024", opening="Opening balance 100.00",
          closing="Closing balance {last}"):
    return statement(rows, period=period, opening=opening, closing=closing)


# ---- the four statements the import screens are built and gated on (epic 0.5; synthetic, and the mock's own facts) ----
LOCKED_PASSWORD = "synthetic-pass"   # a made-up password for the locked fixture: never a real one

OCT_2026 = "Statement period 10/01/2026 to 10/31/2026"   # MM/DD, like the mock: 10/31 can only be month first
STATEMENT_ROWS = [("10/01/2026", "ACH DEPOSIT PAYROLL", "", "1,600.00", "4,010.37"),
                  ("10/02/2026", "TRADER JOE S #122", "84.12", "", "3,926.25"),
                  ("10/03/2026", "SHELL OIL 57444", "52.40", "", "3,873.85"),
                  ("10/03/2026", "SPOTIFY USA", "11.99", "", "3,861.86"),
                  ("10/05/2026", "ZELLE PAYMENT SENT", "120.00", "", "3,741.86")]


def good_statement(password: str | None = None) -> bytes:
    """The check screen's own statement: 5 rows, opening 2,410.37, closing 3,741.86, and it adds up."""
    return statement(STATEMENT_ROWS, period=OCT_2026, opening="Opening balance 2,410.37", password=password)


def no_balances_statement() -> bytes:
    """A statement with no balance column and no printed totals: refused with no_balances, teachable (opening 300.00,
    closing 173.48) and the one the gate types those two numbers into."""
    return statement([("10/01/2026", "TRADER JOE S #122", "84.12", ""),
                      ("10/03/2026", "SHELL OIL 57444", "52.40", ""),
                      ("10/04/2026", "REFUND", "", "10.00")],
                     period=OCT_2026, opening=None, closing=None,
                     header=("Date", "Description", "Debit", "Credit"), columns=(40, 120, 300, 380))


def off_statement() -> bytes:
    """The same rows as good_statement, with a closing balance $65.00 out: refused with does_not_reconcile."""
    rows = STATEMENT_ROWS[:-1] + [("10/05/2026", "ZELLE PAYMENT SENT", "120.00", "", "3,806.86")]
    return statement(rows, period=OCT_2026, opening="Opening balance 2,410.37")


def scanned_statement() -> bytes:
    """A page with no text at all, only ink: refused with no_text_layer, and nothing to teach."""
    doc = FPDF()
    doc.add_page()
    doc.set_fill_color(200, 200, 200)
    doc.rect(20, 20, 170, 250, "F")
    return _raw(doc)


def prose_statement() -> bytes:
    """A page of letter prose with no transaction table: refused with no_header."""
    doc = FPDF()
    doc.add_page()
    doc.set_font("helvetica", size=11)
    for i, line in enumerate(["Nova Federal Credit Union", "Statement of account", "Dear member,",
                              "This page explains the terms of your account.", "The annual percentage rate is 4.5%.",
                              "Nothing here is a table of transactions."]):
        doc.text(20, 30 + i * 10, line)
    return _raw(doc)


def split_row_statement() -> bytes:
    """The IN shape (docs/pdf-teach-once.md): each row's date is on one line and its money on the next, so the
    default row cut (a date OR an amount starts a row) splits every row in two. Taught with row_start=date it adds
    up: opening 1,000.00 + in 250.00 - out 130.00 = closing 1,120.00."""
    doc = FPDF()
    doc.add_page()
    doc.set_font("helvetica", size=9)
    doc.text(40, 28, OCT_2026)
    doc.text(40, 40, "Opening balance 1,000.00")
    cols = (40, 120, 300, 380, 460)
    for i, name in enumerate(("Date", "Description", "Debit", "Credit", "Balance")):
        doc.text(cols[i], 54, name)
    rows = [("10/01/2026", "ACH DEPOSIT PAYROLL", "", "250.00", "1,250.00"),
            ("10/02/2026", "TRADER JOE S #122", "84.12", "", "1,165.88"),
            ("10/03/2026", "SHELL OIL 57444", "45.88", "", "1,120.00")]
    y = 66
    for date, desc, debit, credit, balance in rows:
        doc.text(cols[0], y, date)                      # the date line: date and description only
        doc.text(cols[1], y, desc)
        y += 10
        for i, value in ((2, debit), (3, credit), (4, balance)):   # the money line: no date on it
            if value:
                doc.text(cols[i], y, value)
        y += 14
    return _raw(doc)


def _raw(doc) -> bytes:
    out = doc.output()
    return out.encode("latin-1") if isinstance(out, str) else out


# ---- resource limits and malformed files (epic 0.5, PR #28 review): synthetic, never a real document ----

def _assemble(objects: list[bytes], root: int = 1) -> bytes:
    """A minimal PDF from object bodies, with an xref table (only what pdfminer needs)."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root {root} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _stream(data: bytes, filters: str = "") -> bytes:
    return f"<< /Length {len(data)}{filters} >>\nstream\n".encode() + data + b"\nendstream"


def many_pages_pdf(count: int = 12_000) -> bytes:
    """`count` empty pages: a real statement has a few dozen. 12,000 pages is ~1.2 MB of page-tree pointers."""
    kids = b" ".join(f"{i} 0 R".encode() for i in range(3, 3 + count))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{kids.decode()}] /Count {count} >>".encode()]
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"] * count
    return _assemble(objects)


def double_flate_pdf(decoded_bytes: int = 64 * 1024 * 1024) -> bytes:
    """One content stream Flate-compressed twice: 247 bytes unpack to 64 MB (an amplifier, not a statement)."""
    import zlib
    once = zlib.compress(b"\x00" * decoded_bytes, 9)
    twice = zlib.compress(once, 9)
    return _assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        _stream(twice, " /Filter [/FlateDecode /FlateDecode]"),
    ])


def infinite_mediabox_statement() -> bytes:
    """A readable statement whose page width parses as float infinity: round(width) used to raise OverflowError."""
    raw = statement([("05/03/2024", "Coffee", "10.00", "", "90.00"),
                     ("14/03/2024", "Salary", "", "50.00", "140.00"),
                     ("20/03/2024", "Rent", "40.00", "", "100.00")])
    huge = b"9" * 400 + b".0"
    return raw.replace(b"/MediaBox [0 0 612 792]", b"/MediaBox [0 0 " + huge + b" 792]")


def infinite_position_statement() -> bytes:
    """A page with words, one of them at a text position that parses as float infinity (a huge decimal Td)."""
    huge = b"9" * 400 + b".0"
    content = (b"BT /F1 9 Tf 40 34 Td (Opening balance 100.00) Tj ET\n"
               b"BT /F1 9 Tf 40 60 Td (Date) Tj 80 0 Td (Description) Tj ET\n"
               b"BT /F1 9 Tf " + huge + b" 74 Td (Coffee) Tj ET")
    return _assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 /Resources << /Font << /F1 4 0 R >> >> >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(content),
    ])


def rotate_string_page() -> bytes:
    """`/Rotate` as a string instead of a number: pdfminer raises inside the page tree."""
    return _assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Rotate (ninety) >>",
    ])


def deep_page_tree(depth: int = 3000) -> bytes:
    """A page tree nested 3,000 levels deep: pdfminer raises RecursionError while walking it."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"]
    for i in range(3, 3 + depth - 1):
        objects.append(f"<< /Type /Pages /Kids [{i + 1} 0 R] /Count 1 >>".encode())
    objects.append(b"<< /Type /Page /Parent " + f"{2 + depth} 0 R".encode() + b" /MediaBox [0 0 612 792] >>")
    return _assemble(objects)


def long_word_pdf(chars: int = 1_000_000, *, glyphs: int = 100) -> bytes:
    """One word: `glyphs` 'A' glyphs, each mapped by a ToUnicode CMap to `chars // glyphs` copies of '('.

    A 1.1 KB file prints a 1,000,000-character word this way, and every cap counted words, never characters. The
    second line is ordinary text, so the page is not refused for having no text layer.
    """
    per = max(1, chars // max(1, glyphs))
    dst = ("(" * per).encode("utf-16-be").hex().upper().encode()
    cmap = (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CMapName /Probe def\n"
            b"1 begincodespacerange <00> <FF> endcodespacerange\n1 beginbfchar\n<41> <" + dst + b">\nendbfchar\n"
            b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
    content = (b"BT /F1 9 Tf 40 400 Td (" + b"A" * glyphs + b") Tj ET\n"
               b"BT /F1 9 Tf 40 300 Td (Some ordinary text here) Tj ET")
    import zlib
    return _assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 /Resources << /Font << /F1 4 0 R >> >> >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /ProbeFont /FirstChar 65 /LastChar 65 /Widths [500] "
        b"/ToUnicode 6 0 R >>",
        _stream(content),
        _stream(zlib.compress(cmap, 9), " /Filter /FlateDecode"),
    ])


def one_line_words(count: int = 20_000, text: str = "a") -> bytes:
    """`count` short words on ONE line of one page, packed close enough that the parser reads one long phrase."""
    ops = [b"BT /F1 10 Tf"]
    x = 0.0
    lit = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    for _ in range(count):
        ops.append(f"1 0 0 1 {x:.2f} 400 Tm ({lit}) Tj".encode())
        x += 8.6 if text != "(" else 5.0     # 10 pt: 'a' 5.56 wide / '(' 3.33: a gap > 1.5 splits words
    ops.append(b"ET")
    return _assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 /Resources << /Font << /F1 4 0 R >> >> >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400000 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(b"\n".join(ops)),
    ])


# ---- round 3 (PR #28): files that stay under every child cap (50 pages, 40,000 words, 200 chars a word,
# 2,000,000 characters) and still made a parent-side step quadratic; plus a statement with a drawn rule ----

_LETTERS4 = "abcdefghijklmnopqrstuvwxyz"


def _letters4(i: int) -> str:
    s = ""
    for _ in range(4):
        i, r = divmod(i, 26)
        s += _LETTERS4[r]
    return s


def _cmap_bfchar(entries: list[tuple[int, str]]) -> bytes:
    """A ToUnicode CMap mapping each two-byte code to one arbitrary string (the amplifier `letter_run_words_pdf` uses)."""
    out = [b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CMapName /Probe def\n"
           b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"]
    for k in range(0, len(entries), 100):
        chunk = entries[k:k + 100]
        out.append(b"%d beginbfchar\n" % len(chunk))
        for code, text in chunk:
            out.append(b"<" + b"%04X" % code + b"> <" + text.encode("utf-16-be").hex().upper().encode() + b">\n")
        out.append(b"endbfchar\n")
    out.append(b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
    return b"".join(out)


def _flate(data: bytes) -> bytes:
    import zlib
    return _stream(zlib.compress(data, 9), " /Filter /FlateDecode")


def _esc(s: str) -> bytes:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def _std_text(x: float, y: float, s: str, size: float = 9) -> bytes:
    return b"BT /F1 %g Tf 1 0 0 1 %.3f %.3f Tm (" % (size, x, y) + _esc(s) + b") Tj ET\n"


def letter_run_words_pdf(count: int = 1_600, *, pages: int = 2, per_line: int = 64) -> bytes:
    """`count` distinct 199-character words, each a run of ~39 four-letter tokens joined by digits.

    One CID glyph prints one whole word through /ToUnicode, so a 40 KB file carries 1,600 distinct words inside
    199 characters each - every cap passes. The mask guard used one regex per distinct letter run (about 39 a word)
    over the whole view, five times: the review measured 245.8 s of parent CPU on a 21.9 KB file of this shape.
    """
    entries = [(1, " ")]                     # code 1 = a space glyph: it splits words inside one Tj
    k = 0
    for i in range(count):
        parts = []
        while sum(len(p) + 1 for p in parts) + 5 <= 199:
            parts.append(_letters4(k))
            k += 1
        entries.append((2 + i, "1".join(parts)[:199]))
    cmap = _cmap_bfchar(entries)
    objs: list[bytes | None] = [b"<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = []
    per_page = -(-count // pages)
    for p in range(pages):
        ops = [_std_text(40, 760, "Statement period 01/03/2024 to 31/03/2024"),
               _std_text(40, 748, "Date      Description      Debit      Credit      Balance"),
               _std_text(40, 736, "05/03/2024 Coffee 10.00 90.00"),
               _std_text(40, 724, "06/03/2024 Tea 5.00 85.00")]
        words = list(range(p * per_page, min(count, (p + 1) * per_page)))
        y = 700.0
        for j in range(0, len(words), per_line):
            codes = []
            for w in words[j:j + per_line]:
                codes += [2 + w, 1]
            hexs = b"".join(b"%04X" % c for c in codes)
            ops.append(b"BT /F2 4 Tf 1 0 0 1 10 %.2f Tm <" % y + hexs + b"> Tj ET\n")
            y -= 6
        content = b"".join(ops)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 700 792] /Contents %d 0 R >>" % (len(objs) + 2))
        kids.append(len(objs))
        objs.append(_flate(content))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    f1 = len(objs)
    objs.append(b"<< /Type /Font /Subtype /Type0 /BaseFont /ProbeCID /Encoding /Identity-H "
                b"/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (len(objs) + 2, len(objs) + 3))
    f2 = len(objs)
    objs.append(b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ProbeCID /CIDSystemInfo << /Registry (Adobe) "
                b"/Ordering (Identity) /Supplement 0 >> /FontDescriptor %d 0 R /DW 1000 >>" % (len(objs) + 3))
    objs.append(_flate(cmap))
    objs.append(b"<< /Type /FontDescriptor /FontName /ProbeCID /Flags 32 /FontBBox [0 0 1000 1000] /ItalicAngle 0 "
                b"/Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>")
    objs[1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d" % pages
               + b" /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>" % (f1, f2))
    return _assemble(objs)


def _cid_run(codes: list[int], x: float, y: float, size: float = 4.0) -> bytes:
    """One Tj of two-byte CID codes in the probe font (each code prints its whole /ToUnicode string)."""
    return b"BT /F2 %g Tf 1 0 0 1 %.2f %.2f Tm <" % (size, x, y) + b"".join(b"%04X" % c for c in codes) + b"> Tj ET\n"


def number_cost_statement(*, token_chunk_pairs: int = 12_000_000, pages: int = 50) -> bytes:
    """numcost.pdf's shape: a 65 KB statement whose number words cost a per-chunk guard every token x chunk pair.

    Page 1 is 16 lines of 140 one-glyph words of 200 "1"s: the view masks each to a 200-character run of 9s, so with
    the three line-prefix chunks a line it carries 2,288 digit-bearing chunks. The other 49 pages carry distinct
    `9`*a + d + `9`*b words of about 100 characters - 5,243 of them, 11,998,272 token x chunk pairs in all. Every
    content cap passes (50 pages, 7,483 words, 1,129,973 characters, 200 a word), and round 3's guard compared every
    one of those pairs: the security review measured 141 s for one Ask AI click.
    """
    import random
    toks = ["9" * a + d + "9" * b for d in "12345678" for a in range(92, 106) for b in range(2, 60)]
    random.Random(1).shuffle(toks)
    chunks = 16 * 3 + 140 * 16
    toks = toks[:token_chunk_pairs // chunks - 1]
    entries = [(1, " "), (2, "1" * 200)] + [(3 + i, t) for i, t in enumerate(toks)]
    objs: list[bytes | None] = [b"<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = []
    page_ops = [b"".join(_cid_run([2, 1] * 140, 10, 760 - li * 10) for li in range(16))]
    per = -(-len(toks) // (pages - 1))
    for p in range(pages - 1):
        mine = list(range(p * per, min(len(toks), (p + 1) * per)))
        ops = []
        for j in range(0, len(mine), 6):
            codes: list[int] = []
            for i in mine[j:j + 6]:
                codes += [3 + i, 1]
            ops.append(_cid_run(codes, 10, 760 - (j // 6) * 6))
        page_ops.append(b"".join(ops))
    for content in page_ops:
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1300 792] /Contents %d 0 R >>" % (len(objs) + 2))
        kids.append(len(objs))
        objs.append(_flate(content))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    f1 = len(objs)
    objs.append(b"<< /Type /Font /Subtype /Type0 /BaseFont /ProbeCID /Encoding /Identity-H "
                b"/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (len(objs) + 2, len(objs) + 3))
    f2 = len(objs)
    objs.append(b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ProbeCID /CIDSystemInfo << /Registry (Adobe) "
                b"/Ordering (Identity) /Supplement 0 >> /FontDescriptor %d 0 R /DW 1000 >>" % (len(objs) + 3))
    objs.append(_flate(_cmap_bfchar(entries)))
    objs.append(b"<< /Type /FontDescriptor /FontName /ProbeCID /Flags 32 /FontBBox [0 0 1000 1000] /ItalicAngle 0 "
                b"/Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>")
    objs[1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d" % len(page_ops)
               + b" /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>" % (f1, f2))
    return _assemble(objs)


def irregular_pitch_statement(count: int = 39_500, *, pages: int = 50) -> bytes:
    """A statement whose table carries `count` one-word lines of irregular pitch (1-30 pt) over `pages` pages.

    Every page opens with a dated row so the table continues, and the parser kept every pitch in one list for the
    whole document and re-sorted it per line: O(n^2 log n), 48 s of parent CPU on a 220 KB file (the 39,500 drawn
    lines merge into 37,504 `Line`s, which is how often the median was asked for).
    """
    import random
    rnd = random.Random(7)
    per = count // pages
    objs: list[bytes | None] = [b"<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = []
    for p in range(pages):
        ops = [b"BT /F1 8 Tf\n"]
        top = 40.0
        if p == 0:
            ops.append(b"1 0 0 1 40 -20 Tm (Statement period 01/03/2024 to 31/03/2024) Tj\n")
            for t, x in (("Date", 40), ("Description", 120), ("Debit", 300), ("Credit", 380), ("Balance", 460)):
                ops.append(b"1 0 0 1 %d %.3f Tm (%s) Tj\n" % (x, -top, t.encode()))
            top += 12
        for t, x in (("05/03/2024", 40), ("Coffee", 120), ("10.00", 300), ("90.00", 460)):
            ops.append(b"1 0 0 1 %d %.3f Tm (%s) Tj\n" % (x, -top, t.encode()))
        ops.append(b"ET\nBT /F1 1 Tf\n")
        top += 8
        for _ in range(per):
            top += 1.0 + rnd.random() * 29.0
            ops.append(b"1 0 0 1 120 %.3f Tm (x) Tj\n" % -top)
        ops.append(b"ET\n")
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 %.0f 612 0] /Contents %d 0 R >>"
                    % (-(top + 100), len(objs) + 2))
        kids.append(len(objs))
        objs.append(_flate(b"".join(ops)))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs[1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d" % pages
               + b" /Resources << /Font << /F1 %d 0 R >> >> >>" % len(objs))
    return _assemble(objs)


def long_statement(pages: int = 40, rows_per_page: int = 30) -> bytes:
    """A realistic multi-page statement: `rows_per_page` dated rows a page over `pages` pages (40 x 30 = 1,200 rows).

    Every page repeats the column header, the balance runs down from 100,000.00 by 10.00 a row, and the printed
    closing balance matches, so the statement reconciles: the parse-equivalence corpus for the fix rounds.
    """
    rows = pages * rows_per_page
    opening = 100_000.00
    header = HEADER
    xs = COLUMNS
    first: list[tuple[float, float, str]] = [(40, 20, "Statement period 01/03/2024 to 31/03/2024"),
                                             (40, 34, f"Opening balance {opening:,.2f}")]
    first += _row(40, 60, [(x, text) for x, text in zip(xs, header)])
    lines = [first]
    for p in range(pages):
        page: list[tuple[float, float, str]] = []
        if p:
            page += _row(40, 60, [(x, text) for x, text in zip(xs, header)])
        top = 74
        for r in range(rows_per_page):
            i = p * rows_per_page + r + 1
            day = 1 + (i - 1) // 40                  # 40 rows a day over 30 days: dates never go backwards
            balance = opening - 10.0 * i
            page += _row(40, top, [(40, f"{day:02d}/03/2024"), (120, f"Payment {i:04d}"),
                                   (300, "10.00"), (460, f"{balance:,.2f}")])
            top += 14
        if p == pages - 1:
            top += 16
            page.append((40, top, f"Closing balance {opening - 10.0 * rows:,.2f}"))
        lines.append(page)
    return pdf(lines)


def rule_statement(chars: int = 300) -> bytes:
    """A normal 3-row statement with one long text-drawn rule (`chars` dots at 9 pt) under the table."""
    return statement([("05/03/2024", "Coffee", "10.00", "", "90.00"),
                      ("14/03/2024", "Salary", "", "50.00", "140.00"),
                      ("20/03/2024", "Rent", "40.00", "", "100.00")],
                     extra=[(40, 200, "." * chars)])
