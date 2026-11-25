"""PR #28 round 3 (0.5): the AI mask guard in one pass, the two super-linear `layout.parse` loops, drawn rules.

Every file here is synthetic and built by `pdf_fixtures.py`; each stays under the guard child's caps (50 pages,
40,000 words, 200 characters a word, 2,000,000 characters) unless a test says otherwise. A step that could hang the
suite on a regression runs in a child process with a hard deadline (as the round-2 freezes do): the parent-side work
these tests bound held the GIL, so an in-process timer never fires on it.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

import hermes_finance
from hermes_finance.pdfimport import PdfRefused, ai, layout, parse_statement, words

from pdf_fixtures import (irregular_pitch_statement, letter_run_words_pdf, long_statement, long_word_pdf,
                          rule_statement, statement)

SRC = str(Path(hermes_finance.__file__).resolve().parent.parent)

_CHILD = r'''
import json, sys, time
from datetime import date
from hermes_finance.pdfimport import PdfRefused, ai_outgoing, guard, parse_statement

# Every file here passes the content caps (pages, words, characters), and the read clock is not the step these tests
# bound: on a loaded machine a 1.2 M glyph read trips it and turns the outcome into pdf_too_complex before the parent
# step runs. Raise it, and let this process's own deadline (the parent's kill) be the stop.
guard.LIMITS = guard.Limits(seconds=120.0)

raw = open(sys.argv[1], "rb").read()
started, cpu = time.monotonic(), time.process_time()
try:
    if sys.argv[2] == "preview":
        parse_statement(None, raw, today=date(2026, 11, 24))
    else:
        ai_outgoing(raw)
    outcome = "ok"
except PdfRefused as exc:
    outcome = exc.code
except Exception as exc:        # noqa: BLE001 - a test must see a crash, and it must not look like a refusal
    outcome = "error:" + type(exc).__name__
print(json.dumps({"outcome": outcome, "wall": time.monotonic() - started,
                  "cpu": time.process_time() - cpu}))
'''


def _run(tmp_path, raw: bytes, mode: str, deadline: float, name: str = "case"):
    """{wall, cpu, outcome} for one preview / ai_view in a killable child process, or None past `deadline`.

    `cpu` is the parent-side time (`process_time` of this child process, not of the guard's own read child): that is
    the share these tests bound. The child raises the read clock (its files pass the content caps), so a loaded
    machine cannot turn the read into a refusal ahead of the step under test; a regression is killed at `deadline`
    instead of hanging the suite.
    """
    pdf = tmp_path / f"{name}.pdf"
    pdf.write_bytes(raw)
    script = tmp_path / "round3_child.py"
    script.write_text(_CHILD)
    env = {"PYTHONPATH": SRC, "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "PYTHONDONTWRITEBYTECODE": "1"}
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen([sys.executable, str(script), str(pdf), mode], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, **kwargs)
    try:
        out, err = proc.communicate(timeout=deadline)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.communicate()
        return None
    if proc.returncode != 0:
        pytest.fail(f"the {mode} child failed: {err.decode()[-800:]}")
    return json.loads(out.decode().strip().splitlines()[-1])


def _one(tmp_path, raw: bytes, mode: str, deadline: float, name: str):
    answer = _run(tmp_path, raw, mode, deadline, name)
    assert answer is not None, f"{name} {mode} was still running after {deadline} s: the request thread is stuck"
    assert not answer["outcome"].startswith("error:"), answer["outcome"]
    return answer


# ---------------------------------------------------------------- 1. the mask guard: one pass, same verdicts

def test_the_mask_guard_still_catches_a_name_a_word_and_a_number():
    """The linear rewrite must refuse exactly what the old per-token regex searches refused.

    Every case below is one the old `re.search`-per-token loop raised on: a merchant/name word, a number, and a
    number that sits inside a decorated word of the view. This is red if the guard's checks are removed.
    """
    real = ["Zelle", "Grocery", "1,234.56", "05/03/2024"]
    clean = "L4 p1 y40: 1@40-80:Opening 2@95-135:balance 3@500-540:99.99"
    ai.assert_masked(clean, real)                                  # nothing readable leaked: no raise
    with pytest.raises(PermissionError):
        ai.assert_masked(clean + " 7@160-200:Grocery", real)        # a merchant word survived
    with pytest.raises(PermissionError):
        ai.assert_masked(clean + " 8@300-340:1,234.56", real)       # an amount survived, in its own chunk
    with pytest.raises(PermissionError):
        ai.assert_masked(clean + " 9@40-90:05/03/2024", real)       # a date survived
    with pytest.raises(PermissionError):
        ai.assert_masked("3@500-540:99.99 1,234.56", real)          # ...and inside a decorated word
    ai.assert_masked("12 34", ["1234"])                             # a token split across two chunks is not a match


def test_the_mask_guard_does_not_rescan_the_view_per_token(tmp_path):
    """42 KB, 426 words, 15,604 distinct letter runs, a 41,636-character view: 8.0 s a pass on 768794f, 40.9 s five."""
    answer = _one(tmp_path, letter_run_words_pdf(400), "ai_view", 20, "runs_400")
    assert answer["outcome"] == "ok", answer["outcome"]
    assert answer["cpu"] < 4.0, f"the parent spent {answer['cpu']:.2f} s of CPU on the mask guard"


def test_a_worst_case_ai_view_under_every_cap_answers_in_the_budget(tmp_path):
    """481 KB, 4,826 words (194 characters the longest), 187,204 letter runs, a 498,106-character view: every cap passes."""
    answer = _one(tmp_path, letter_run_words_pdf(4_800), "ai_view", 45, "runs_4800")
    assert answer["outcome"] == "ok", answer["outcome"]
    assert answer["cpu"] < 6.0, f"the parent spent {answer['cpu']:.2f} s of CPU on the mask guard"


def test_an_ai_view_over_the_ceiling_is_a_typed_refusal(tmp_path):
    """6,000 runs make a view of about 620,000 characters: past `ai.MAX_VIEW_CHARS` the file is refused, not screened."""
    answer = _one(tmp_path, letter_run_words_pdf(6_000), "ai_view", 30, "runs_6000")
    assert answer["outcome"] == "pdf_too_complex", answer["outcome"]
    assert ai.MAX_VIEW_CHARS < 620_000


# ---------------------------------------------------------------- 2. a drawn rule is not a word (7-word regression)

def test_a_statement_with_a_long_drawn_rule_still_parses(conn):
    """A 300-dot rule at 9 pt is one 300-character "word": 21ba046 parsed this statement as 3 rows, 768794f refused it."""
    st = parse_statement(conn, rule_statement(300), today=date(2026, 11, 24))
    assert [(r.date, r.amount) for r in st.rows] == [("2024-03-05", 10.0), ("2024-03-14", -50.0),
                                                     ("2024-03-20", 40.0)]
    assert str(st.opening) == "100.00" and str(st.closing) == "100.00"


def test_an_over_cap_word_of_letters_is_still_refused(conn):
    """The rule skip is only for rule characters: a 300-character word of letters still refuses the file."""
    raw = statement([("05/03/2024", "Coffee", "10.00", "", "90.00")], extra=[(40, 200, "A" * 300)])
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, raw, today=date(2026, 11, 24))
    assert caught.value.code == "pdf_too_complex"
    with pytest.raises(words.TooComplex):
        words.read(long_word_pdf(1_000_000))


# ---------------------------------------------------------------- 3. layout.parse: the two super-linear loops

def test_parse_does_not_resort_row_pitch_on_every_line(tmp_path):
    """220 KB, 50 pages, 37,504 table lines: 48.2 s of parent CPU on 768794f, from a median() of a growing list."""
    answer = _one(tmp_path, irregular_pitch_statement(), "preview", 40, "pitch")
    assert answer["outcome"] == "no_balances", answer["outcome"]
    assert answer["cpu"] < 6.0, f"the parent spent {answer['cpu']:.2f} s of CPU on the parse"


def _money_line_pages(count: int = 20_000):
    """The pages a `gen2.py money <count>` file reads to: a header, one dated row, then `count` money words on it."""
    def w(text, x0, top, width=None):
        width = width if width is not None else max(1.0, 4.0 * len(text))
        return {"text": text, "x0": x0, "x1": x0 + width, "top": top, "bottom": top + 8.0}

    page = [w("Statement", 40, 20), w("period", 90, 20), w("01/03/2024", 130, 20), w("to", 190, 20),
            w("31/03/2024", 210, 20), w("Opening", 40, 32), w("balance", 80, 32), w("100.00", 130, 32),
            w("Date", 40, 44), w("Description", 120, 44), w("Debit", 300, 44), w("Credit", 380, 44),
            w("Balance", 460, 44), w("05/03/2024", 40, 56)]
    for i in range(count):
        page.append(w("1.00", 300 + i * 79.0 / count, 56, width=0.001))
    page.append(w("90.00", 460, 56))
    return [page], [(612.0, 792.0)]


def test_parse_does_not_scan_the_line_money_words_for_every_word():
    """`gen2.py money`'s shape, scaled up: one row line of 20,000 money words, 6.9 s of parent CPU on 768794f.

    Built as words, not read from a PDF: at 19,500 words the read itself is past the guard child's clock, and the
    quadratic step under test is the parent's per-word identity scan over the line's own money words.
    """
    pages, sizes = _money_line_pages(20_000)
    started = time.monotonic()
    parsed = layout.parse(pages, sizes)
    elapsed = time.monotonic() - started
    assert parsed is not None
    assert elapsed < 1.5, f"the parent spent {elapsed:.2f} s parsing one line of 20,000 money words"


def test_the_running_median_answers_exactly_what_statistics_median_did():
    """`parse` branched on `median(row_pitch)`: the replacement must return the same float, not an estimate."""
    for values in ([1.0], [3.0, 7.0], [12.0, 4.0, 9.0], [0.5, 0.25, 8.0, 2.0], [float(i % 7) for i in range(500)]):
        running = layout.RunningMedian()
        for i, v in enumerate(values):
            running.add(v)
            assert len(running) == i + 1
            assert running.value() == statistics.median(values[:i + 1]), values[:i + 1]


# ---------------------------------------------------------------- 4. the equivalence pin

def test_a_realistic_statement_still_parses_exactly(conn):
    """1,200 rows over 41 pages, opening 100,000.00, closing 88,000.00: the corpus the round-3 dump compares to 768794f."""
    raw = long_statement()
    st = parse_statement(conn, raw, today=date(2026, 11, 24))
    assert len(st.rows) == 1200
    assert (st.rows[0].date, st.rows[0].amount) == ("2024-03-01", 10.0)
    assert (st.rows[599].date, st.rows[599].amount) == ("2024-03-15", 10.0)
    assert (st.rows[-1].date, st.rows[-1].amount) == ("2024-03-30", 10.0)
    assert st.rows[0].description == "Payment 0001"
    assert str(st.opening) == "100000.00" and str(st.closing) == "88000.00"
    assert len(ai.outgoing(words.read(raw)[0])) == 5


def test_a_plain_statement_is_unchanged(conn):
    """The smallest statement the fixture builds: three rows, and the AI view still passes the guard."""
    st = parse_statement(conn, statement([("05/03/2024", "Coffee", "10.00", "", "90.00"),
                                          ("14/03/2024", "Salary", "", "50.00", "140.00"),
                                          ("20/03/2024", "Rent", "40.00", "", "100.00")]),
                         today=date(2026, 11, 24))
    assert [(r.date, r.amount) for r in st.rows] == [("2024-03-05", 10.0), ("2024-03-14", -50.0),
                                                     ("2024-03-20", 40.0)]
