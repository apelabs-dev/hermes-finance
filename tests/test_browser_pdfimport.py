"""The PDF import screens in headless Chrome (epic 0.5, tests/cdp.py): the check screen over a statement that adds
up, a statement with no balances taught in four steps (and the number that must match), a wrong typed balance,
a locked PDF, the AI opt-in as a second tap (AI is off in tests, so it ends as a refusal), the missing add-on, and a
file that only claims to be a PDF. Everything is synthetic: tests/pdf_fixtures.py builds the bytes in memory."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import clibrain, db, pdfimport
from hermes_finance.pdfimport import ai, words
from pdf_fixtures import (HEADER, LOCKED_PASSWORD, good_statement, no_balances_statement, off_statement,
                          prose_statement, scanned_statement, split_row_statement, statement)

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
MINUS = "\u2212"
# The engine's own AI success fixture (tests/test_pdfimport_api.py): no header line, so the generic parser refuses
# it with no_header, and only a layout suggestion (stubbed here) can read it. Day-first dates: 14/03 can't be a month.
MARCH = [("05/03/2024", "Coffee", "10.00", "", "90.00"),
         ("14/03/2024", "Salary", "", "50.00", "140.00"),
         ("20/03/2024", "Rent", "40.00", "", "100.00")]


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


@pytest.fixture
def full(tmp_path):
    srv = Server(gate_dbs.build("full", tmp_path / "finance.db"))
    yield srv
    srv.close()


@pytest.fixture
def stub_ai(monkeypatch):
    """The AI path with the model stubbed out, for one test: FINANCE_PDF_AI on, the CLI seam ready, and ai._ask
    answering with a reply the test builds. No model is called (clibrain.complete raises if anything tries)."""
    monkeypatch.setenv("FINANCE_PDF_AI", "on")
    monkeypatch.setattr(clibrain, "off", lambda: False)
    monkeypatch.setattr(clibrain, "ready", lambda conn, cli_id: (Path("/bin/true"), ""))

    def no_model(*args, **kwargs):
        raise AssertionError("a model was called")

    monkeypatch.setattr(clibrain, "complete", no_model)
    sent: list[str] = []

    def use(reply):
        monkeypatch.setattr(ai, "_ask", lambda cli_id, exe, prompt, deadline: sent.append(prompt) or reply)

    return SimpleNamespace(use=use, sent=sent)


def ai_reply(raw: bytes, header: list[str], columns: list[tuple[str, list[str]]]) -> clibrain.Reply:
    """A stubbed model reply: the word ids of the header words and of each column's words, picked by their text."""
    pages, _ = words.read(raw)
    _view, gid = ai.word_view(pages)
    ids: dict[str, list[int]] = {}
    for wid, w in gid.items():
        ids.setdefault(w.text, []).append(wid)
    text = json.dumps({"header_ids": [ids[t][0] for t in header],
                       "columns": [{"role": role, "ids": [ids[t][0] for t in texts]} for role, texts in columns],
                       "end_ids": [], "amount_sign": "explicit", "year_missing": False})
    return clibrain.Reply(text, "claude-code", "test", 1, 1, None, 0.0)


def send_to_ai(b):
    """Refusal -> Ask AI -> the privacy screen -> Send layout shape (once the screen shows what it sends)."""
    b.js("[...document.querySelectorAll('#sheet .btn.ghost')].find((x) => x.textContent.includes('Ask AI')).click()")
    waitfor(b, "!!document.querySelector('#sheet .pi-mask')", "the AI privacy screen")
    waitfor(b, "(() => { const s = [...document.querySelectorAll('#sheet .btn.w')].find((x) =>"
               " x.textContent === 'Send layout shape'); return !!s && !s.disabled; })()", "Send to be ready")
    b.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Send layout shape').click()")


def columns(b):
    """Teach step 1: each printed column's name and the role picked for it."""
    return b.js("[...document.querySelectorAll('#sheet .pi-col:not(.pi-rowstart)')].map((c) =>"
                " [c.querySelector('b').textContent, c.querySelector('select').value])")


def goto(b, srv, page="accounts", width=1280):
    b.size(width, 900)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/{page}")
    b.settle()


def waitfor(b, cond, what=""):
    """Poll in the page until cond is true; fail loudly instead of hanging the suite."""
    b.js("new Promise((ok, no) => { const end = Date.now() + 20000;"
         f" const t = () => {{ if ({cond}) return ok(1);"
         f" if (Date.now() > end) return no(new Error('timed out waiting for ' + {json.dumps(what or cond)}));"
         " setTimeout(t, 50); }; t(); })")


def open_drop(b):
    b.js("document.querySelector('[data-add=home]').click()")
    waitfor(b, "!!document.querySelector('#sheet .opt')", "the add sheet")
    b.js("[...document.querySelectorAll('#sheet .opt')].find((o) => o.textContent.startsWith('Import a file')).click()")
    waitfor(b, "!!document.querySelector('#sheet .drop')", "the drop sheet")


def drop(b, data: bytes, name="statement.pdf"):
    payload = base64.b64encode(data).decode()
    b.js(f"""(() => {{ const bin = atob({json.dumps(payload)}); const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
      const dt = new DataTransfer(); dt.items.add(new File([bytes], {json.dumps(name)}, {{type: 'application/pdf'}}));
      const i = document.querySelector('#sheet input[type=file]'); i.files = dt.files;
      i.dispatchEvent(new Event('change')); return 1; }})()""")


def page_errors(b):
    """Console errors that are not a refusal: every PDF refusal is an expected 400 from /api/import/preview."""
    return [p for p in b.problems() if not (p.startswith("log: Failed to load resource") and "400 (Bad Request)" in p
                                            and "/api/import/" in p)]


def proof(b):
    """The four rows of the add-up proof: label and amount, in order."""
    return b.js("[...document.querySelectorAll('#sheet .srow')].map((r) =>"
                " [r.querySelector('.grow b').textContent, (r.querySelector('.v') || {}).textContent])")


def next_step(b, n):
    """Tap Next until the sheet says step n."""
    b.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Next').click()")
    waitfor(b, f"document.querySelector('#sheet .pi-steps span').textContent === 'Step {n} of 4'", f"step {n}")


def teach_to_balance(b):
    """Walk the four teach steps to step 4 (the balance question)."""
    b.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Teach it').click()")
    waitfor(b, "!!document.querySelector('#sheet .pi-cols')", "teach step 1")
    next_step(b, 2)
    next_step(b, 3)
    next_step(b, 4)
    waitfor(b, "!!document.querySelector('#sheet .pi-typed')", "teach step 4")


def type_balances(b, opening, closing):
    b.js("(() => { const i = [...document.querySelectorAll('#sheet .pi-typed input')];"
         f" i[0].value = {json.dumps(opening)}; i[0].dispatchEvent(new Event('input', {{bubbles: true}}));"
         f" i[1].value = {json.dumps(closing)}; i[1].dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()")


def test_the_check_screen_proves_the_statement_and_imports_it(browser, full):
    goto(browser, full)
    open_drop(browser)
    drop(browser, good_statement(), "statement-2026-10.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .pi-pair')", "the check screen")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Check the statement"
    assert browser.js("document.querySelector('#sheet .filec b').textContent") == "statement-2026-10.pdf"
    assert "5 rows" in browser.js("document.querySelector('#sheet .filec span span').textContent")
    # the proof: the statement's own cents, never the app's dropped ".00"
    assert proof(browser) == [["Opening balance", "$2,410.37"], ["Money in · 1 row", "+$1,600.00"],
                              ["Money out · 4 rows", f"{MINUS}$268.51"], ["Closing balance", "$3,741.86"]]
    assert browser.js("document.querySelector('#sheet .pi-pair .pill').textContent") == "Adds up"
    assert browser.js("[...document.querySelectorAll('#sheet .srow')][3].querySelector('.grow span').textContent") \
        == "Same as the statement prints"
    # every row, in the app's date style
    assert browser.js("[...document.querySelectorAll('#sheet .pi-rows th')].map((t) => t.textContent)") == \
        ["Date", "Description", "Amount"]
    rows = browser.js("[...document.querySelectorAll('#sheet .pi-rows tbody tr')].map((tr) =>"
                      " [...tr.children].map((td) => td.textContent))")
    assert len(rows) == 5
    assert rows[0] == ["Oct 1", "ACH DEPOSIT PAYROLL", "+$1,600.00"]
    assert rows[-1] == ["Oct 5", "ZELLE PAYMENT SENT", f"{MINUS}$120.00"]
    # where it goes: a new account named after the file, and nothing else offered in a fresh DB
    assert browser.js("document.querySelector('#sheet .li b').textContent") == "Statement 2026 10"
    assert browser.js("document.querySelector('#sheet .li .selv').value") == "new:bank"
    assert browser.js("document.querySelector('#sheet .li .selv').selectedOptions[0].textContent") \
        == "Add as account: Statement 2026 10"
    # import: the rows land in a manual account, and the sheet says so
    go = browser.js("[...document.querySelectorAll('#sheet .btn.w')].map((b) => b.textContent)")
    assert go == ["Import 5 rows"]
    browser.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
    waitfor(browser, "!!document.querySelector('#sheet .impdone')", "the imported sheet")
    assert browser.js("document.querySelector('#sheet .impdone b').textContent") == "5 new rows"
    assert browser.js("document.querySelector('#sheet .fg b').textContent") == "5 new · 0 skipped"
    assert browser.js("document.querySelector('#sheet .hint').textContent") \
        == "They are in accounts added by you (manual); categories are filled in the usual way."
    conn = db.connect(full.app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 5
    # a PDF makes a manual account named after the file, opened at the statement's closing balance
    assert [float(r[0]) for r in conn.execute("SELECT balance FROM manual_accounts WHERE name = 'Statement 2026 10'")] \
        == [3741.86]
    # the app's sign: money in is negative, so -1,600.00 + 84.12 + 52.40 + 11.99 + 120.00
    assert conn.execute("SELECT ROUND(SUM(amount), 2) FROM transactions WHERE source = 'import'").fetchone()[0] == -1331.49
    conn.close()
    assert browser.problems() == []                      # the happy path is clean: no refusal, no console noise


def test_the_same_statement_twice_is_all_duplicates_and_imports_nothing(browser, full):
    goto(browser, full)
    open_drop(browser)
    drop(browser, good_statement(), "statement-2026-10.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .pi-pair')")
    browser.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
    waitfor(browser, "!!document.querySelector('#sheet .impdone')")
    open_drop(browser)
    drop(browser, good_statement(), "statement-2026-10.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .pi-pair')")
    assert browser.js("document.querySelector('#sheet .li span span').textContent") == "5 rows · 0 new · 5 already here"
    assert browser.js("[...document.querySelectorAll('#sheet .btn.w')].map((b) => [b.textContent, b.disabled])") \
        == [["Nothing new to import", True]]
    conn = db.connect(full.app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 5
    conn.close()
    assert browser.problems() == []


def test_a_statement_without_balances_is_taught_in_four_steps(browser, full):
    goto(browser, full)
    open_drop(browser)
    drop(browser, no_balances_statement(), "statement-nobal.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "a refusal")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Can\u2019t check this PDF"
    assert browser.js("document.querySelector('#sheet .filec span span').textContent") == "No balances printed"
    assert browser.js("document.querySelector('#sheet .formerr').textContent").startswith(
        "This statement does not print balances")
    # the refusal offers the two ways forward, and the AI only as a second, quieter button
    assert browser.js("[...document.querySelectorAll('#sheet .btn.w')].map((b) => b.textContent)") \
        == ["Teach it", "Choose another file"]
    assert browser.js("[...document.querySelectorAll('#sheet .btn.ghost')].map((b) => b.textContent.trim())") \
        == ["Ask AI to suggest the layout"]
    # step 1: the statement's own columns, and what is printed in them
    browser.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Teach it').click()")
    waitfor(browser, "!!document.querySelector('#sheet .pi-cols')", "teach step 1")
    assert browser.js("document.querySelector('#sheet .pi-steps span').textContent") == "Step 1 of 4"
    assert browser.js("[...document.querySelectorAll('#sheet .pi-col:not(.pi-rowstart)')].map((c) => [c.querySelector('b').textContent,"
                      " c.querySelector('select').value])") == [["Date", "date"], ["Description", "desc"],
                                                                ["Debit", "debit"], ["Credit", "credit"]]
    assert browser.js("document.querySelector('#sheet .pi-col:not(.pi-rowstart) span span').textContent") \
        == "10/01/2026 · 10/03/2026 · 10/04/2026"
    # step 2: the date order, both readings named
    next_step(browser, 2)
    assert browser.js("[...document.querySelectorAll('#sheet .pi-opt')].map((o) => [o.querySelector('b').textContent,"
                      " o.querySelector('.grow span').textContent])") == [["October 1, 2026", "Month first: MM/DD/YYYY"],
                                                                          ["January 10, 2026", "Day first: DD/MM/YYYY"]]
    assert browser.js("document.querySelector('#sheet .pi-opt.on b').textContent") == "October 1, 2026"
    # step 3: how money out is shown: the statement has two money columns, so "in its own column" is picked
    next_step(browser, 3)
    assert browser.js("[...document.querySelectorAll('#sheet .pi-opt b')].map((b) => b.textContent)") \
        == ["In its own column", "A minus sign", "A minus after the number", "Marked DR or CR"]
    assert browser.js("document.querySelector('#sheet .pi-opt.on b').textContent") == "In its own column"
    next_step(browser, 4)
    # step 4: no balance column, so it asks for the two numbers, and says it can't remember the layout
    assert browser.js("[...document.querySelectorAll('#sheet .pi-opt b')].map((b) => b.textContent)") \
        == ["A running balance column", "Opening and closing only", "I will type them"]
    assert browser.js("document.querySelector('#sheet .pi-opt.on b').textContent") == "Opening and closing only"
    assert browser.js("[...document.querySelectorAll('#sheet .srow b')].some((b) => b.textContent === 'Save this layout')") is False
    assert browser.js("document.querySelector('#sheet .pi-hint').textContent").startswith("This layout can\u2019t be saved")
    # the number that matters: opening + in - out == closing, or nothing is imported
    type_balances(browser, "300.00", "173.48")
    waitfor(browser, "!!document.querySelector('#sheet .pi-chk.ok')", "the add-up check")
    assert browser.js("document.querySelector('#sheet .pi-chk b').textContent") == "Adds up"
    assert browser.js("document.querySelector('#sheet .pi-chk .grow span').textContent") \
        == "$300.00 + $10.00 " + MINUS + " $136.52 = $173.48, as you typed"     # (security L4) not "as printed"
    # on a phone the proof wraps: "as you typed" never hides behind an ellipsis
    browser.size(390, 900)
    assert browser.js("(() => { const s = document.querySelector('#sheet .pi-chk .grow span');"
                      " return s.scrollWidth <= s.clientWidth; })()") is True
    browser.size(1280, 900)
    assert browser.js("[...document.querySelectorAll('#sheet .btn.w')].map((b) => [b.textContent, b.disabled])") \
        == [["Save and import 3 rows", False]]
    browser.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
    waitfor(browser, "!!document.querySelector('#sheet .impdone')", "the imported sheet")
    assert browser.js("document.querySelector('#sheet .impdone b').textContent") == "3 new rows"
    conn = db.connect(full.app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM pdf_templates").fetchone()[0] == 0     # typed balances: not remembered
    conn.close()
    assert page_errors(browser) == []


def test_a_typed_balance_that_does_not_add_up_imports_nothing(browser, full):
    goto(browser, full)
    open_drop(browser)
    drop(browser, no_balances_statement(), "statement-nobal.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')")
    teach_to_balance(browser)
    type_balances(browser, "300.00", "200.00")
    waitfor(browser, "!!document.querySelector('#sheet .pi-chk.bad')", "a failed check")
    assert browser.js("document.querySelector('#sheet .pi-chk b').textContent") == "Doesn\u2019t add up"
    assert browser.js("document.querySelector('#sheet .pi-chk .grow span').textContent") \
        == "The transactions in this PDF don't add up to the balances it prints, so nothing is imported."
    assert browser.js("[...document.querySelectorAll('#sheet .btn.w')].map((b) => [b.textContent, b.disabled])") \
        == [["Save and import", True]]
    conn = db.connect(full.app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 0
    conn.close()


def test_a_locked_pdf_asks_for_its_password_and_never_keeps_it(browser, full):
    goto(browser, full)
    open_drop(browser)
    drop(browser, good_statement(password=LOCKED_PASSWORD), "statement-locked.pdf")
    waitfor(browser, "!!document.querySelector('#sheet #pdf-password')", "the password prompt")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "This PDF is locked"
    assert browser.js("document.querySelector('#sheet .filec span span').textContent") == "Locked with a password"
    assert browser.js("document.querySelector('#sheet .pi-hint').textContent") \
        == "Used once to open this file on this computer. Never saved."
    # empty, then wrong, then right
    browser.js("document.querySelector('#sheet .btn.w').click()")
    assert browser.js("document.querySelector('#sheet .formerr').textContent") \
        == "Type the password the statement was saved with."
    browser.js("(() => { const i = document.querySelector('#sheet #pdf-password'); i.value = 'not-the-one';"
               " i.dispatchEvent(new Event('input', {bubbles: true})); document.querySelector('#sheet .btn.w').click(); return 1; })()")
    waitfor(browser, "document.querySelector('#sheet .formerr').textContent === 'That password did not open the file. Try again.'",
            "the wrong-password line")
    browser.js(f"(() => {{ const i = document.querySelector('#sheet #pdf-password'); i.value = {json.dumps(LOCKED_PASSWORD)};"
               " i.dispatchEvent(new Event('input', {bubbles: true})); document.querySelector('#sheet .btn.w').click(); return 1; })()")
    waitfor(browser, "!!document.querySelector('#sheet .pi-pair')", "the check screen")
    assert len(browser.js("[...document.querySelectorAll('#sheet .pi-rows tbody tr')]")) == 5
    # the password is gone from the page, and was never written to the database
    assert browser.js("!!document.querySelector('#sheet input[type=password]')") is False
    assert LOCKED_PASSWORD not in browser.js("document.querySelector('#sheet').textContent")
    assert LOCKED_PASSWORD.encode() not in Path(full.app.db_path).read_bytes()
    assert page_errors(browser) == []


def test_the_ai_is_a_deliberate_second_tap_and_is_off_in_tests(browser, full):
    from hermes_finance.pdfimport import ai
    assert ai.enabled() is False                      # the tests run with FINANCE_PDF_AI=off
    goto(browser, full)
    open_drop(browser)
    drop(browser, good_statement(), "statement-2026-10.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .pi-pair')")
    assert "Ask AI" not in browser.js("document.querySelector('#sheet').textContent")   # never on the way in
    # a statement that doesn't add up offers it, behind its own privacy screen
    open_drop(browser)
    drop(browser, off_statement(), "statement-off.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Can\u2019t check this PDF"
    browser.js("[...document.querySelectorAll('#sheet .btn.ghost')].find((b) => b.textContent.includes('Ask AI')).click()")
    waitfor(browser, "!!document.querySelector('#sheet .pi-mask')", "the AI privacy screen")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Ask AI to suggest the layout"
    assert browser.js("[...document.querySelectorAll('#sheet .pi-priv .gh')].map((g) => g.textContent.replace(/\\s+/g, ' ').trim())") \
        == ["Sent", "Never sent"]
    mask = browser.js("document.querySelector('#sheet .pi-mask').textContent")
    assert mask.startswith("You read a bank statement as positioned words")     # the request itself, word for word
    shapes = re.findall(r"\b\d+@\d+-\d+:(\S+)", mask)                           # <id>@<x0>-<x1>:<word shape>
    assert "99/99/9999" in shapes and set(ch for s in shapes for ch in s if ch.isdigit()) == {"9"}
    assert "3,806.86" not in mask and "2,410.37" not in mask and "statement-off" not in mask
    # sending is the second tap; with AI off it ends in a refusal that offers no second try
    browser.js("[...document.querySelectorAll('#sheet .btn.w')].find((b) => b.textContent === 'Send layout shape').click()")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "the AI refusal")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Can\u2019t check this PDF"
    assert "isn\u2019t available" in browser.js("document.querySelector('#sheet .formerr').textContent")
    assert browser.js("[...document.querySelectorAll('#sheet .btn.ghost')].some((b) => b.textContent.includes('Ask AI'))") is False
    conn = db.connect(full.app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 0
    conn.close()
    assert page_errors(browser) == []


def test_the_missing_addon_names_the_command_to_install(browser, full, monkeypatch):
    monkeypatch.setattr(pdfimport, "available", lambda: False)
    goto(browser, full)
    open_drop(browser)
    drop(browser, good_statement(), "statement-2026-10.pdf")
    waitfor(browser, "!!document.querySelector('#pdf-install')", "the add-on screen")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "PDF import isn\u2019t installed"
    assert browser.js("document.querySelector('#pdf-install').value") == "pip install 'hermes-finance[pdf]'"
    text = browser.js("document.querySelector('#sheet').textContent")
    assert "uv sync --extra pdf" in text and "Or download the statement as CSV, OFX or QFX" in text
    assert page_errors(browser) == []


def test_a_file_that_is_not_really_a_pdf_is_refused(browser, full):
    goto(browser, full)
    open_drop(browser)
    drop(browser, b"this is not a pdf at all", "statement.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "a refusal")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Can\u2019t read this PDF"
    assert browser.js("document.querySelector('#sheet .filec span span').textContent") == "Not a readable PDF"
    assert page_errors(browser) == []


def test_the_phone_sheet_does_not_overflow_sideways(browser, full):
    goto(browser, full, width=390)
    open_drop(browser)
    drop(browser, good_statement(), "statement-2026-10.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .pi-pair')")
    assert browser.js("({page: document.documentElement.scrollWidth <= innerWidth,"
                      " sheet: (() => { const s = document.querySelector('#sheet'); return s.scrollWidth <= s.clientWidth; })()})") \
        == {"page": True, "sheet": True}
    assert browser.js("[...document.querySelectorAll('#sheet .pi-rows tbody tr')].length") == 5
    assert browser.problems() == []


def test_the_screens_never_write_html(browser, full):
    """No innerHTML, no injected script, no inline handler: the sheet is built from elements only."""
    goto(browser, full)
    open_drop(browser)
    drop(browser, good_statement(), "statement-2026-10.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .pi-pair')")
    assert browser.js("document.querySelectorAll('#sheet script, #sheet [onclick], #sheet [oninput], #sheet [onchange]').length") == 0
    js = (Path(__file__).parent.parent / "src/hermes_finance/static/pdfimport.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js
    assert "eval(" not in js and "new Function" not in js


def test_a_scanned_pdf_is_refused_and_offers_no_layout_work(browser, full):
    """A page of ink with no text: nothing to teach and nothing to send, so only the file can be changed."""
    goto(browser, full)
    open_drop(browser)
    drop(browser, scanned_statement(), "statement-scan.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "a refusal")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Can\u2019t read this PDF"
    assert browser.js("document.querySelector('#sheet .filec span span').textContent") == "A picture of a page"
    assert "only a picture of a page" in browser.js("document.querySelector('#sheet .formerr').textContent")
    buttons = browser.js("[...document.querySelectorAll('#sheet .btn')].map((b) => b.textContent.trim())")
    assert buttons == ["Choose another file"]
    assert page_errors(browser) == []


def test_a_page_without_a_table_is_refused_but_the_ai_can_still_help(browser, full):
    goto(browser, full)
    open_drop(browser)
    drop(browser, prose_statement(), "statement-prose.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "a refusal")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Can\u2019t read this PDF"
    assert browser.js("document.querySelector('#sheet .filec span span').textContent") == "The table wasn\u2019t recognized"
    assert browser.js("[...document.querySelectorAll('#sheet .btn')].map((b) => b.textContent.trim())") \
        == ["Choose another file", "Ask AI to suggest the layout"]
    assert page_errors(browser) == []


def test_a_file_over_ten_megabytes_is_refused_before_it_is_read(browser, full):
    goto(browser, full)
    open_drop(browser)
    browser.requests()                                                   # drain
    browser.js("(() => { const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(10 * 1024 * 1024 + 1)],"
               " 'huge.pdf', {type: 'application/pdf'})); const i = document.querySelector('#sheet input[type=file]');"
               " i.files = dt.files; i.dispatchEvent(new Event('change')); return 1; })()")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "the size refusal")
    assert browser.js("document.querySelector('#sheet .formerr').textContent") \
        == "That file is over 10 MB. Download a shorter date range."
    assert [u for _, u in browser.requests() if "/api/import/" in u] == []     # never uploaded
    assert page_errors(browser) == []


def test_a_row_that_starts_at_its_date_is_found_and_remembered(browser, full):
    """The IN shape (docs/pdf-teach-once.md): the generic parser and the default row cut both refuse, and only
    row_start=date adds up. The check tries that cut itself, the control shows what it settled on, and the saved
    template carries it, so the same statement imports next time with no teach."""
    goto(browser, full)
    open_drop(browser)
    drop(browser, split_row_statement(), "statement-split.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "a refusal")
    assert browser.js("document.querySelector('#sheet .filec span span').textContent") == "The rows don\u2019t add up"
    browser.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Teach it').click()")
    waitfor(browser, "!!document.querySelector('#sheet .pi-cols')", "teach step 1")
    assert browser.js("document.querySelector('#sheet .pi-rowstart select').value") == "date_or_amount"
    assert browser.js("[...document.querySelectorAll('#sheet .pi-rowstart option')].map((o) => o.textContent)") \
        == ["A date or an amount", "A new date"]
    # nothing touched: the check itself finds the right row cut
    next_step(browser, 2)
    next_step(browser, 3)
    next_step(browser, 4)
    waitfor(browser, "!!document.querySelector('#sheet .pi-chk.ok')", "the add-up check")
    assert browser.js("document.querySelector('#sheet .pi-chk .grow span').textContent") \
        == "$1,000.00 + $250.00 " + MINUS + " $130.00 = $1,120.00, as printed"
    assert "Each row starts at its date" in browser.js("document.querySelector('#sheet').textContent")
    # the control on step 1 shows the cut the check settled on
    for _ in range(3):
        browser.js("[...document.querySelectorAll('#sheet .btn')].filter((x) => x.textContent === 'Back').pop().click()")
        browser.js("new Promise((r) => setTimeout(r, 120))")
    waitfor(browser, "document.querySelector('#sheet .pi-steps span').textContent === 'Step 1 of 4'")
    assert browser.js("document.querySelector('#sheet .pi-rowstart select').value") == "date"
    for n in (2, 3, 4):
        next_step(browser, n)
    waitfor(browser, "!!document.querySelector('#sheet .pi-chk.ok')", "the add-up check again")
    assert browser.js("[...document.querySelectorAll('#sheet .btn.w')].map((b) => [b.textContent, b.disabled])") \
        == [["Save and import 3 rows", False]]
    browser.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
    waitfor(browser, "!!document.querySelector('#sheet .impdone')", "the imported sheet")
    assert browser.js("document.querySelector('#sheet .impdone b').textContent") == "3 new rows"
    conn = db.connect(full.app.db_path)
    saved = conn.execute("SELECT config FROM pdf_templates").fetchall()
    assert len(saved) == 1 and json.loads(saved[0][0])["row_start"] == "date"     # the key survives the save
    conn.close()
    # the template round-trips: the same file again goes straight to the check screen, nothing new
    open_drop(browser)
    drop(browser, split_row_statement(), "statement-split.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .pi-pair')", "the check screen from the saved template")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Check the statement"
    assert browser.js("document.querySelector('#sheet .li span span').textContent") == "3 rows · 0 new · 3 already here"
    assert page_errors(browser) == []


def test_the_teach_step_lets_a_person_choose_where_a_row_starts(browser, full):
    goto(browser, full)
    open_drop(browser)
    drop(browser, split_row_statement(), "statement-split.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')")
    browser.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Teach it').click()")
    waitfor(browser, "!!document.querySelector('#sheet .pi-cols')")
    browser.js("(() => { const s = document.querySelector('#sheet .pi-rowstart select'); s.value = 'date';"
               " s.dispatchEvent(new Event('change', {bubbles: true})); return 1; })()")
    next_step(browser, 2)
    next_step(browser, 3)
    next_step(browser, 4)
    waitfor(browser, "!!document.querySelector('#sheet .pi-chk.ok')", "the add-up check")
    # chosen, not guessed: no "the check decided" line
    assert "Each row starts at its date" not in browser.js("document.querySelector('#sheet').textContent")
    assert browser.js("[...document.querySelectorAll('#sheet .btn.w')].map((b) => b.textContent)") \
        == ["Save and import 3 rows"]
    assert page_errors(browser) == []


@pytest.mark.parametrize("width,height", [(1280, 900), (390, 844)])
def test_the_teach_step_fits_across_at_every_width(browser, full, width, height):
    """The column list is a single-column grid: a nowrap sample must not widen it past the sheet (it did once, and the
    selects were clipped)."""
    goto(browser, full, width=width)
    browser.size(width, height)
    open_drop(browser)
    drop(browser, split_row_statement(), "statement-split.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')")
    browser.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Teach it').click()")
    waitfor(browser, "!!document.querySelector('#sheet .pi-cols')")
    assert browser.js("""(() => { const s = document.querySelector('#sheet');
      const sel = document.querySelector('#sheet .pi-rowstart select').getBoundingClientRect();
      const box = s.getBoundingClientRect();
      return {overflow: s.scrollWidth - s.clientWidth, inside: sel.right <= box.right && sel.left >= box.left}; })()""") \
        == {"overflow": 0, "inside": True}
    assert browser.js("document.querySelector('#sheet .pi-rowstart select').selectedOptions[0].textContent") \
        == "A date or an amount"


def test_an_ai_suggestion_that_adds_up_is_checked_here_and_imported_as_ai(browser, full, stub_ai):
    """Round 5, frames 2 and 3: the suggestion fills the four Teach steps (columns, the date order the statement's own
    digits give, the sign), the check passes on this computer, and Save and import keeps the AI's layout as source
    'ai'. The model is asked once, on the preview: neither the checks nor the commit call it again."""
    raw = statement(MARCH, header=None)
    stub_ai.use(ai_reply(raw, ["Statement", "period"], [("date", ["05/03/2024"]), ("desc", ["Coffee"]),
                                                        ("debit", ["10.00"]), ("credit", ["50.00"]),
                                                        ("balance", ["90.00"])]))
    goto(browser, full)
    open_drop(browser)
    drop(browser, raw, "statement-2024-03.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "a refusal")
    assert browser.js("document.querySelector('#sheet .filec span span').textContent") == "The table wasn\u2019t recognized"
    send_to_ai(browser)
    waitfor(browser, "!!document.querySelector('#sheet .pi-cols')", "the suggestion on teach step 1")
    assert browser.js("document.querySelector('#sheet .filec span span').textContent") == "Suggested by AI · 3 rows"
    assert browser.js("document.querySelector('#sheet .pi-tag').textContent") == "AI layout"
    assert columns(browser) == [["Date", "date"], ["Description", "desc"], ["Money out", "debit"],
                                ["Money in", "credit"], ["Balance", "balance"]]
    # the date order is the one the server read from the digits (14/03 is day first), not a month-first default
    next_step(browser, 2)
    assert browser.js("document.querySelector('#sheet .pi-opt.on b').textContent") == "March 5, 2024"
    next_step(browser, 3)
    assert browser.js("document.querySelector('#sheet .pi-opt.on b').textContent") == "In its own column"
    next_step(browser, 4)
    waitfor(browser, "!!document.querySelector('#sheet .pi-chk.ok, #sheet .pi-chk.bad')", "the add-up check")
    assert browser.js("document.querySelector('#sheet .pi-chk b').textContent") == "Adds up"
    assert browser.js("document.querySelector('#sheet .pi-chk .grow span').textContent") \
        == "$100.00 + $50.00 " + MINUS + " $50.00 = $100.00, as printed"
    assert browser.js("[...document.querySelectorAll('#sheet .btn.w')].map((b) => [b.textContent, b.disabled])") \
        == [["Save and import 3 rows", False]]
    browser.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
    waitfor(browser, "!!document.querySelector('#sheet .impdone')", "the imported sheet")
    assert browser.js("document.querySelector('#sheet .impdone b').textContent") == "3 new rows"
    conn = db.connect(full.app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 3
    saved = conn.execute("SELECT config, source FROM pdf_templates").fetchall()
    conn.close()
    assert len(saved) == 1 and saved[0][1] == "ai"
    cfg = json.loads(saved[0][0])
    assert cfg["header_words"] == ["statement", "period"]          # the proposal's own header, not the empty detail
    assert [c["role"] for c in cfg["columns"]] == ["date", "desc", "debit", "credit", "balance"]
    assert cfg["date_order"] == "DMY"
    assert len(stub_ai.sent) == 1                                  # one model turn, on the preview only
    assert page_errors(browser) == []


def test_an_ai_suggestion_that_does_not_add_up_offers_teach_it_from_the_suggestion(browser, full, stub_ai):
    """Round 5, frame 4: nothing is imported, and the next step is the same as any refusal: Teach it (opened with
    the AI's suggestion filled in, here its swapped money columns) or Choose another file. No second AI try."""
    raw = off_statement()
    stub_ai.use(ai_reply(raw, list(HEADER), [("date", ["Date"]), ("desc", ["Description"]), ("credit", ["Debit"]),
                                             ("debit", ["Credit"]), ("balance", ["Balance"])]))
    goto(browser, full)
    open_drop(browser)
    drop(browser, raw, "statement-off.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "a refusal")
    send_to_ai(browser)
    waitfor(browser, "document.querySelector('#sheet .filec span span').textContent === 'The AI\u2019s layout doesn\u2019t add up'",
            "the AI refusal")
    assert browser.js("document.querySelector('#sheet h2').textContent") == "Can\u2019t check this PDF"
    assert browser.js("document.querySelector('#sheet .formerr').textContent").startswith(
        "The AI\u2019s layout does not add up to the closing balance")
    assert browser.js("[...document.querySelectorAll('#sheet .btn')].map((b) => b.textContent.trim())"
                      ".filter((t) => t)") == ["Teach it", "Choose another file"]
    browser.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Teach it').click()")
    waitfor(browser, "!!document.querySelector('#sheet .pi-cols')", "teach step 1")
    assert browser.js("document.querySelector('#sheet .pi-steps span').textContent") == "Step 1 of 4"
    # the statement's own columns, with the roles the AI suggested (its money columns the wrong way round)
    assert columns(browser) == [["Date", "date"], ["Description", "desc"], ["Debit", "credit"], ["Credit", "debit"],
                                ["Balance", "balance"]]
    conn = db.connect(full.app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pdf_templates").fetchone()[0] == 0
    conn.close()
    assert len(stub_ai.sent) == 5                                  # the five candidates of one preview, no more
    assert page_errors(browser) == []


def test_the_ai_privacy_screen_shows_every_line_that_is_sent(browser, full, stub_ai):
    """(security L2) The privacy screen is what the server would send, word for word, before anything leaves: the
    first request in full, and what each later try changes. Every line of every prompt the model got was on it."""
    raw = off_statement()
    stub_ai.use(ai_reply(raw, list(HEADER), [("date", ["Date"]), ("desc", ["Description"]), ("credit", ["Debit"]),
                                             ("debit", ["Credit"]), ("balance", ["Balance"])]))   # all five tries go
    goto(browser, full)
    open_drop(browser)
    drop(browser, raw, "statement-off.pdf")
    waitfor(browser, "!!document.querySelector('#sheet .formerr')", "a refusal")
    browser.js("[...document.querySelectorAll('#sheet .btn.ghost')].find((x) => x.textContent.includes('Ask AI')).click()")
    waitfor(browser, "!!document.querySelector('#sheet .pi-mask')", "the AI privacy screen")
    assert stub_ai.sent == []                                     # the screen is built here: nothing has left yet
    shown = browser.js("[...document.querySelectorAll('#sheet .pi-mask')].map((m) => m.textContent).join('\\n')")
    sent_line = browser.js("document.querySelector('#sheet .pi-priv .yes li span').textContent")
    assert sent_line.startswith("Up to 5 requests, word for word below")
    browser.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Send layout shape').click()")
    waitfor(browser, "document.querySelector('#sheet .filec span span').textContent === 'The AI\u2019s layout doesn\u2019t add up'",
            "the AI refusal")
    assert len(stub_ai.sent) == 5
    assert stub_ai.sent[0] in shown                               # the first request, whole and in order
    lines = set(shown.split("\n"))
    assert [ln for p in stub_ai.sent for ln in p.split("\n") if ln.strip() and ln not in lines] == []
    assert "3,806.86" not in shown and "2,410.37" not in shown and "statement-off" not in shown
    assert page_errors(browser) == []


GRAB_HF = "(window.HF_EXT = window.HF_EXT || []).push((HF) => { window.__hf = HF; });"


@pytest.fixture
def hf_browser():
    """A Chrome of its own that keeps a handle on the app's HF object (the module's Chrome stays untouched)."""
    with Chrome() as b:
        b.call("Page.addScriptToEvaluateOnNewDocument", source=GRAB_HF)
        yield b


def test_the_password_and_the_pdf_are_let_go_when_the_flow_ends(hf_browser, full):
    """(security L3) The password and the file are held only while the flow needs them: an import, closing the sheet
    on a refusal, and Choose another file each let both go."""
    b = hf_browser
    held = "JSON.stringify(window.__hf.pdf.held())"
    gone = {"file": False, "data": False, "password": False}
    goto(b, full)
    open_drop(b)
    drop(b, good_statement(password=LOCKED_PASSWORD), "statement-locked.pdf")
    waitfor(b, "!!document.querySelector('#sheet #pdf-password')", "the password prompt")
    b.js(f"(() => {{ const i = document.querySelector('#sheet #pdf-password'); i.value = {json.dumps(LOCKED_PASSWORD)};"
         " i.dispatchEvent(new Event('input', {bubbles: true})); document.querySelector('#sheet .btn.w').click(); return 1; })()")
    waitfor(b, "!!document.querySelector('#sheet .pi-pair')", "the check screen")
    assert json.loads(b.js(held)) == {"file": True, "data": True, "password": True}    # while the flow needs them
    b.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
    waitfor(b, "!!document.querySelector('#sheet .impdone')", "the imported sheet")
    assert json.loads(b.js(held)) == gone                                             # imported: both let go
    assert b.js("document.querySelector('#sheet .impdone b + span').textContent").startswith("statement-locked.pdf")
    # a refusal closed with the x
    open_drop(b)
    drop(b, off_statement(), "statement-off.pdf")
    waitfor(b, "!!document.querySelector('#sheet .formerr')", "a refusal")
    assert json.loads(b.js(held))["data"] is True
    b.js("document.querySelector('#sheet .shd [aria-label=Close]').click()")
    assert json.loads(b.js(held)) == gone
    # a refusal left for another file
    open_drop(b)
    drop(b, off_statement(), "statement-off.pdf")
    waitfor(b, "!!document.querySelector('#sheet .formerr')", "a refusal")
    b.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Choose another file').click()")
    waitfor(b, "!!document.querySelector('#sheet .drop')", "the drop sheet")
    assert json.loads(b.js(held)) == gone
    assert page_errors(b) == []


def test_teaching_over_a_saved_layout_says_it_replaces_it(browser, full):
    """(security L1) A layout saved before is only replaced in the open: step 4 says so under Save this layout, and
    the import sends replace_template (the server refuses a silent replace). Here the saved layout is the split-row
    statement's default row cut, which can't add up; the teach finds the date cut and replaces it."""
    raw = split_row_statement()
    conn = db.connect(full.app.db_path)
    with pytest.raises(pdfimport.PdfRefused) as refused:
        pdfimport.parse_statement(conn, raw)
    d = refused.value.extra["detail"]
    wrong = {"header_words": d["header_words"][:8], "amount_sign": "explicit",
             "columns": [{"role": c["role"], "x0": c["x0"], "x1": c["x1"]} for c in d["columns"] if c["role"]],
             **({"date_order": d["date_order"]} if d.get("date_order") else {})}
    with conn:
        pdfimport.templates.save(conn, d["fingerprint"], wrong, "user")
        # saved at 03:00 UTC on Mar 15, which is the evening of Mar 14 in Los Angeles: the screen says the local day
        conn.execute("UPDATE pdf_templates SET created_at = '2026-03-15T03:00:00Z'")
    conn.close()
    browser.call("Emulation.setTimezoneOverride", timezoneId="America/Los_Angeles")
    try:
        goto(browser, full, width=390)
        open_drop(browser)
        drop(browser, raw, "statement-split.pdf")
        waitfor(browser, "!!document.querySelector('#sheet .formerr')", "a refusal under the saved layout")
        browser.js("[...document.querySelectorAll('#sheet .btn.w')].find((x) => x.textContent === 'Teach it').click()")
        waitfor(browser, "!!document.querySelector('#sheet .pi-cols')", "teach step 1")
        for n in (2, 3, 4):
            next_step(browser, n)
        waitfor(browser, "!!document.querySelector('#sheet .pi-chk.ok')", "the add-up check")
        hint = "[...document.querySelectorAll('#sheet .srow')].find((r) => r.textContent.includes('Save this layout'))" \
               ".querySelector('.grow span')"
        assert browser.js(f"{hint}.textContent") == "Replaces the layout saved on Mar 14, 2026 for statements like this."
        # on a phone the sentence wraps: none of it hides behind an ellipsis
        assert browser.js(f"(() => {{ const s = {hint}; return s.scrollWidth <= s.clientWidth; }})()") is True
        browser.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
        waitfor(browser, "!!document.querySelector('#sheet .impdone')", "the imported sheet")
    finally:
        browser.call("Emulation.setTimezoneOverride", timezoneId="")
    conn = db.connect(full.app.db_path)
    saved = conn.execute("SELECT config FROM pdf_templates").fetchall()
    conn.close()
    assert len(saved) == 1 and json.loads(saved[0][0])["row_start"] == "date"      # replaced, as the screen said
    assert page_errors(browser) == []
