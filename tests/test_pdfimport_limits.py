"""The PDF read's resource limits and malformed-file refusals (0.5, PR #28 review).

Every file here is synthetic, built by tests/pdf_fixtures.py. The refusals are the product's own
(`PdfRefused`) and every limit is checked at the seam the web routes call (parse_statement / ai_outgoing),
not only in the guard.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

import hermes_finance
from hermes_finance.pdfimport import (PdfRefused, ai, ai_outgoing, guard, layout, parse_statement, templates,
                                      words)

from pdf_fixtures import (deep_page_tree, double_flate_pdf, infinite_mediabox_statement, infinite_position_statement,
                          long_word_pdf, many_pages_pdf, one_line_words, rotate_string_page, statement)

GOOD = [("05/03/2024", "Coffee", "10.00", "", "90.00"),
        ("14/03/2024", "Salary", "", "50.00", "140.00"),
        ("20/03/2024", "Rent", "40.00", "", "100.00")]
NAME_IS_A_LIST_WORD = [("05/03/2024", "Money New", "10.00", "", "90.00"),
                       ("14/03/2024", "March", "", "50.00", "140.00"),
                       ("20/03/2024", "Rent", "40.00", "", "100.00")]
CFG = {"header_words": ["date", "description", "debit", "credit", "balance"],
       "columns": [{"role": "date", "x0": 40, "x1": 70}, {"role": "desc", "x0": 120, "x1": 200},
                   {"role": "debit", "x0": 300, "x1": 330}, {"role": "credit", "x0": 380, "x1": 410},
                   {"role": "balance", "x0": 460, "x1": 495}]}


def refused(conn, raw, code, **kw):
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, raw, today=date(2026, 9, 28), **kw)
    assert caught.value.code == code, caught.value.code
    assert caught.value.message
    return caught.value


# ---------------------------------------------------------------- 1. the caps

def test_a_statement_over_the_page_cap_is_refused_before_any_page_is_read(conn):
    """12,000 pages in 1.2 MB: refused on the page count, not after walking (and keeping) every page."""
    raw = many_pages_pdf(12_000)
    started = time.monotonic()
    refused(conn, raw, "pdf_too_complex")
    assert time.monotonic() - started < 5          # old code walked all 12,000 pages before refusing


def test_the_page_cap_is_read_from_the_limits_and_checked_before_extraction():
    raw = many_pages_pdf(200)
    with pytest.raises(words.TooComplex):
        words.read(raw, max_pages=10)              # 200 pages is fine by default, 10 is the limit here


def test_the_word_cap_is_refused():
    with pytest.raises(words.TooComplex):
        words.read(statement(GOOD), max_words=5)   # a normal statement has more than five words


def test_a_slow_pdf_is_stopped_by_the_wall_clock_limit():
    """64 MB of NULs through two Flate layers took 3.9 s / 1.0 GB in the review's probe and 10.9 s here for 32 MB."""
    raw = double_flate_pdf(32 * 1024 * 1024)
    limits = dataclasses.replace(guard.LIMITS, seconds=0.5, memory_mb=2048)
    started = time.monotonic()
    with pytest.raises(words.TooComplex):
        guard.read(raw, limits=limits)
    assert time.monotonic() - started < 5


def test_a_flate_bomb_is_stopped_by_the_memory_limit():
    """The same 247-byte file peaks at 170 MB on the old read; this guard kills it at 48 MB."""
    raw = double_flate_pdf(64 * 1024 * 1024)
    limits = dataclasses.replace(guard.LIMITS, seconds=30.0, memory_mb=48)
    started = time.monotonic()
    with pytest.raises(words.TooComplex):
        guard.read(raw, limits=limits)
    assert time.monotonic() - started < 15


def test_a_limit_hit_through_parse_statement_is_a_refusal_not_a_crash(conn, monkeypatch):
    monkeypatch.setattr(guard, "LIMITS", dataclasses.replace(guard.LIMITS, seconds=1.0, memory_mb=2048))
    refused(conn, double_flate_pdf(32 * 1024 * 1024), "pdf_too_complex")


def test_the_default_page_cap_is_the_documented_number():
    assert (guard.LIMITS.pages, guard.LIMITS.words) == (50, 40_000)
    assert words.MAX_PAGES == guard.LIMITS.pages and words.MAX_WORDS == guard.LIMITS.words


def test_a_normal_statement_still_parses_exactly_as_before(conn):
    st = parse_statement(conn, statement(GOOD), today=date(2026, 9, 28))
    assert [(r.date, r.amount) for r in st.rows] == [("2024-03-05", 10.0), ("2024-03-14", -50.0),
                                                     ("2024-03-20", 40.0)]
    assert str(st.opening) == "100.00" and str(st.closing) == "100.00"


# ---------------------------------------------------------------- 2. malformed PDFs

@pytest.mark.parametrize("build", [deep_page_tree, rotate_string_page, infinite_mediabox_statement],
                         ids=["deep_page_tree", "rotate_string_page", "infinite_mediabox"])
def test_a_malformed_pdf_is_pdf_unreadable_not_a_500(conn, build):
    refused(conn, build(), "pdf_unreadable")


def test_an_infinite_text_position_is_dropped_not_rounded():
    pages, sizes = words.read(infinite_position_statement())
    coords = [v for page in pages for w in page for v in (w["x0"], w["x1"], w["top"], w["bottom"])]
    assert coords and all(math.isfinite(v) for v in coords)


def test_ai_outgoing_survives_an_infinite_position():
    """ai.word_view round()s every position; a non-finite one used to be a 500 on the ai_view route."""
    assert isinstance(ai_outgoing(infinite_position_statement()), list)


def test_the_read_logs_the_exception_type_only(conn, caplog):
    raw = rotate_string_page()
    with caplog.at_level(logging.WARNING, logger="hermes_finance.pdfimport.words"):
        refused(conn, raw, "pdf_unreadable")
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "TypeError" in logged                  # the type, for the operator
    assert "not all arguments" not in logged      # ...never the exception's message (it can carry PDF bytes)
    assert "ninety" not in logged and "MediaBox" not in logged


def test_a_broken_producer_is_empty_not_an_exception():
    assert words.producer(rotate_string_page()) == ""


# ---------------------------------------------------------------- 3. client regex patterns

def test_a_catastrophic_client_pattern_is_refused():
    with pytest.raises(ValueError, match="plain text"):
        templates.clean_config({**CFG, "skip_pattern": r"^(\s*\S*)*Z$"})


def test_a_catastrophic_taught_pattern_is_refused_before_it_is_saved(conn):
    fingerprint = {"header_roles": ["date"], "header_words": ["date"], "page_w": 612, "page_h": 792,
                   "top_shape": []}
    with pytest.raises(ValueError, match="plain text"):
        templates.save(conn, fingerprint, {**CFG, "table_start_pattern": r"^(\s*\S*)*Z$"}, "user")
    assert conn.execute("SELECT COUNT(*) FROM pdf_templates").fetchone()[0] == 0


def test_a_list_word_outside_the_header_and_balance_lines_is_masked():
    """A merchant or person called "Money" or "New" is masked; the same word in the header or a balance line is kept,
    because there it is the statement's own vocabulary and the columns need it."""
    header = [("Date", 40), ("Description", 120), ("Debit", 300), ("Credit", 400), ("Balance", 500)]
    pages = [[{"text": t, "x0": x, "x1": x + 50, "top": 10, "bottom": 20} for t, x in header]
             + [{"text": "Opening", "x0": 40, "x1": 90, "top": 30, "bottom": 40},
                {"text": "balance", "x0": 95, "x1": 140, "top": 30, "bottom": 40},
                {"text": "100.00", "x0": 500, "x1": 540, "top": 30, "bottom": 40}]
             + [{"text": "Money", "x0": 120, "x1": 170, "top": 60, "bottom": 70},
                {"text": "New", "x0": 120, "x1": 150, "top": 80, "bottom": 90},
                {"text": "10.00", "x0": 300, "x1": 340, "top": 60, "bottom": 70},
                {"text": "20.00", "x0": 300, "x1": 340, "top": 80, "bottom": 90},
                {"text": "30.00", "x0": 300, "x1": 340, "top": 100, "bottom": 110}]]
    view, _gid = ai.word_view(pages)
    assert "Money" not in view and "New" not in view
    assert "Date" in view and "Opening" in view and "balance" in view


def test_indic_vowel_signs_are_masked_with_their_letters():
    masked = words.mask_text("मार्च", keep=False)
    assert masked.isascii() and len(masked) == 5        # 3 letters + 2 combining marks, every one masked


def test_masked_ai_view_of_a_name_that_is_a_list_word_passes_the_guard():
    pages, _sizes = words.read(statement(NAME_IS_A_LIST_WORD))
    assert ai.outgoing(pages)   # the first view survived the mask guard: nothing readable is left in it


@pytest.mark.parametrize("pattern,line", [(r"^transactions$", "Transactions"), (r"^0\.00$", "0.00"),
                                          ("total", "Subtotal 12.00"), (r"closing balance", "Closing balance 1.00")])
def test_plain_text_patterns_are_still_accepted(pattern, line):
    import re
    cleaned = templates.clean_config({**CFG, "skip_pattern": pattern})["skip_pattern"]
    assert re.search(cleaned, line, re.I)


def test_a_saved_pattern_from_before_the_grammar_is_ignored(conn):
    """A template row taught by the old code can hold a backtracking pattern; it is skipped on find and load."""
    pages, sizes = words.read(statement(GOOD))
    fp = templates.fingerprint(pages, sizes, "")
    legacy = {**CFG, "skip_pattern": r"^(\s*\S*)*Z$"}
    conn.execute("INSERT INTO pdf_templates (id, layout_id, fingerprint, config, source, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 ("legacy", templates.layout_id(fp), json.dumps(fp), json.dumps(legacy), "user", "2024-01-01T00:00:00Z"))
    assert templates.find(conn, fp) is None
    assert templates.load(conn, "legacy") is None


def test_a_taught_plain_pattern_still_starts_the_table(conn):
    """The teach-once fixture's own pattern keeps working through the validator and the parser."""
    assert templates.clean_config({**CFG, "table_start_pattern": r"^transactions$"})["table_start_pattern"] \
        == r"^transactions$"


# ---------------------------------------------------------------- 4. parent-side work on what the child returns

_PREVIEW_CHILD = r'''
import json, sys, time
from datetime import date
from hermes_finance.pdfimport import PdfRefused, ai_outgoing, parse_statement

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
except Exception as exc:            # noqa: BLE001 - the test must see it, and it must not look like a refusal
    outcome = "error:" + type(exc).__name__
print(json.dumps({"outcome": outcome, "wall": time.monotonic() - started,
                  "cpu": time.process_time() - cpu}))
'''

_SRC = str(Path(hermes_finance.__file__).resolve().parent.parent)


def _one_preview(tmp_path, raw: bytes, mode: str, deadline: float):
    """(wall seconds, outcome) for one preview in a child process, or None when it is still running at `deadline`.

    The parent-side parse is what the review froze the server with. It holds the GIL, so a storm-watch thread inside
    the same process never gets scheduled to time it out; a child process can be killed, so a regression fails the
    test in `deadline` seconds instead of hanging the suite.
    """
    pdf = tmp_path / f"{mode}.pdf"
    pdf.write_bytes(raw)
    script = tmp_path / "preview_child.py"
    script.write_text(_PREVIEW_CHILD)
    env = {"PYTHONPATH": _SRC, "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
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
        pytest.fail(f"the preview child failed: {err.decode()[-800:]}")
    answer = json.loads(out.decode().strip().splitlines()[-1])
    return answer["wall"], answer["outcome"]


_FREEZE_CASES = {"paren_1000000": lambda: long_word_pdf(1_000_000),
                 "line_paren_2000": lambda: one_line_words(2_000, "("),
                 "one_line_20k": lambda: one_line_words(20_000)}


@pytest.mark.parametrize("mode", ["preview", "ai_view"])
@pytest.mark.parametrize("name", ["paren_1000000", "line_paren_2000", "one_line_20k"])
def test_a_guard_approved_read_cannot_freeze_the_parent(tmp_path, name, mode):
    """The child's caps bound the read; the parent's work on the words it returns used to have no bound at all.

    Measured on 21ba046: the 1,000,000-character word never answered (about 37 minutes extrapolated), the 2,000-word
    "(" line took 25.2 s and 20,000 words on one line took 123 s, each holding the GIL so the server stopped answering.
    The files are built per call: fpdf stamps /CreationDate with the current second, so a PDF built at collection time
    gives a different test id (and a different id on every xdist worker).
    """
    raw = _FREEZE_CASES[name]()
    answer = _one_preview(tmp_path, raw, mode, deadline=20)
    assert answer is not None, f"{mode} was still running after 20 s: the request thread is frozen"
    wall, outcome = answer
    # These files are not statements, so a typed refusal is a correct answer (a bomb is refused, a headerless page
    # says no_header); what must never happen is an error, a freeze, or a request that takes longer than the budget.
    assert outcome in ("ok", "pdf_too_complex", "no_header"), outcome
    assert wall < 15, f"{mode} took {wall:.1f} s"


def test_role_of_is_linear_on_an_unclosed_parenthesis_run():
    """`\\(.*?\\)` scanned to the end of the phrase from every position: 40,000 "(" cost 3.64 s here and held the GIL."""
    started = time.monotonic()
    assert layout.role_of("(" * 40_000) is None
    assert time.monotonic() - started < 0.5


def test_a_line_over_the_header_word_cap_is_rejected_before_the_phrase_scan(monkeypatch):
    """`_phrases` re-runs role_of on the growing phrase for every word; a 20,000-word line must never reach it."""
    line = words.Line(0, [words.Word("x", 0.0, 1.0, 10.0, 20.0, 0, i) for i in range(15)])
    monkeypatch.setattr(layout, "_phrases", lambda ln: pytest.fail("_phrases ran on a 15-word line"))
    assert layout.header_columns(line) is None


def test_a_header_phrase_is_matched_on_a_bounded_prefix(monkeypatch):
    """Joining the whole growing phrase for every word was O(words x phrase); role_of reads at most 200 characters."""
    seen: list[str] = []
    real = layout.role_of
    monkeypatch.setattr(layout, "role_of", lambda phrase: seen.append(phrase) or real(phrase))
    line = words.Line(0, [words.Word("(" * 200, 0.0, 1.0, 10.0, 20.0, 0, i) for i in range(14)])
    assert layout._phrases(line)                                   # the words merge into one phrase
    assert seen and max(len(p) for p in seen) <= 200


def test_build_lines_keeps_a_running_mean_not_a_recomputed_one():
    """The mean of the line was recomputed for every word (O(words^2)); it runs on every parse and every AI try.

    CPU, not wall: 0.01 s here and 0.413 s on a CI runner for the same build, so wall time on a loaded runner is the
    runner's load, not this loop (same reason the round-3 money-words test bounds `process_time`).
    """
    page = [{"text": "a", "x0": float(i), "x1": float(i) + 1.0, "top": 10.0, "bottom": 20.0} for i in range(20_000)]
    started = time.process_time()
    lines = words.build_lines([page])
    assert len(lines) == 1 and len(lines[0].words) == 20_000
    assert time.process_time() - started < 1.0


def test_a_million_character_word_is_refused_by_the_child_read():
    """The caps counted words, never characters: this is a 1.1 KB file with one 1,000,000-character word."""
    started = time.monotonic()
    with pytest.raises(words.TooComplex):
        words.read(long_word_pdf(1_000_000))
    assert time.monotonic() - started < 5


def test_the_word_character_cap_is_a_read_argument():
    with pytest.raises(words.TooComplex):
        words.read(long_word_pdf(1_000, glyphs=1), max_word_chars=200)


def test_the_total_character_cap_is_a_read_argument():
    with pytest.raises(words.TooComplex):
        words.read(statement(GOOD), max_chars=50)


def test_the_default_character_caps_are_the_documented_numbers():
    assert (words.MAX_WORD_CHARS, words.MAX_CHARS) == (200, 2_000_000)


# ---------------------------------------------------------------- 5. the review's two small Lows (L1, L2)

def test_non_posix_reads_warn_once_about_the_missing_memory_ceiling(monkeypatch, caplog):
    """L2: on a platform without the RSS sampler nothing enforced the ceiling and nothing said so."""
    monkeypatch.setattr(guard, "_has_memory_sampler", lambda: False)
    monkeypatch.setattr(guard, "_warned_no_ceiling", False)
    with caplog.at_level(logging.WARNING, logger="hermes_finance.pdfimport.guard"):
        guard._warn_without_ceiling()
        guard._warn_without_ceiling()
        guard._warn_without_ceiling()
    assert len([r for r in caplog.records if "memory ceiling" in r.getMessage()]) == 1
    assert guard._warned_no_ceiling is True


def test_only_a_few_guard_children_run_at_once(monkeypatch):
    """L1: six uploads started six children at ~340 MB each; a read holds one of SLOTS slots."""
    live = 0
    peak = 0
    lock = threading.Lock()

    def fake_read_now(raw, password, limits):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.2)
        with lock:
            live -= 1
        raise words.WordReadError()

    monkeypatch.setattr(guard, "_read_now", fake_read_now)
    errors: list[BaseException] = []

    def worker():
        try:
            guard.read(b"%PDF-1.7")
        except words.WordReadError:
            pass
        except BaseException as exc:        # noqa: BLE001 - a slot bug fails the test instead of a dead thread
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    assert guard.SLOTS == 2
    assert peak == guard.SLOTS


# ---------------------------------------------------------------- 6. L3: the child's reply is read in chunks and
# killed at its process group once it passes MAX_OUT (round 6)

_OVERSIZED_REPLY_CHILD = r'''
import os, sys, time
sys.stdin.buffer.read()                      # the writer thread delivers the request; this stub ignores it
log = os.environ["GUARD_STUB_LOG"]
chunk = b"x" * 65536
sent = 0
with open(log, "a", encoding="ascii") as fh:
    while sent < 96 * 1024 * 1024:           # 1.5 x MAX_OUT
        try:
            os.write(1, chunk)
        except OSError:
            break                            # the parent killed us: that is what this test is about
        sent += len(chunk)
        if sent % (1024 * 1024) == 0:
            fh.write("x")
            fh.flush()
time.sleep(60)                               # hold the pipe open: the old read would wait for the clock
'''

_FOREVER_REPLY_CHILD = r'''
import os, subprocess, sys, time
sys.stdin.buffer.read()
helper = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
with open(os.environ["GUARD_STUB_PIDS"], "w", encoding="ascii") as fh:
    fh.write(f"{os.getpid()} {helper.pid}")
chunk = b"x" * 65536
while True:
    try:
        os.write(1, chunk)
    except OSError:
        break
    time.sleep(0.005)                        # ~13 MB/s: 3 s at the old read is ~40 MB buffered, not a GB
'''

_ECHO_LENGTH_CHILD = r'''
import json, sys
body = sys.stdin.buffer.read()
sys.stdout.write(json.dumps({"pages": [], "sizes": [], "producer": str(len(body))}))
sys.stdout.flush()
'''

_TAIL_REPLY_CHILD = r'''
import os, sys, time
sys.stdin.buffer.read()
view = memoryview(b"x" * (1024 * 1024 + 100))    # just past MAX_OUT, then silence: no EOF, no further byte
while view:
    view = view[os.write(1, view):]
time.sleep(60)                                   # hold the pipe open: only the cap can end this read
'''


def test_an_oversized_reply_is_killed_while_it_is_read(monkeypatch, tmp_path):
    """L3: `MAX_OUT` was checked only after `communicate()` had buffered the whole reply, so the child's own RSS was
    the only ceiling during the read. 96 MiB (1.5x MAX_OUT) is now refused in well under the clock, and the child
    never finishes writing: the parent stops it at the cap, not after the whole answer is in memory."""
    log = tmp_path / "oversized.log"
    monkeypatch.setenv("GUARD_STUB_LOG", str(log))
    monkeypatch.setattr(guard, "CHILD", _OVERSIZED_REPLY_CHILD)
    started = time.monotonic()
    with pytest.raises(words.TooComplex):
        guard.read(b"%PDF-1.7", limits=dataclasses.replace(guard.LIMITS, seconds=8.0))
    elapsed = time.monotonic() - started
    assert elapsed < 4.0, f"the read waited {elapsed:.1f} s for a reply it should have killed at MAX_OUT"
    written = len(log.read_text(encoding="ascii")) if log.exists() else 0
    assert written < 96, f"the child wrote the whole 96 MiB ({written} MiB) before it was stopped"


@pytest.mark.skipif(os.name != "posix", reason="process-group kill and os.kill are POSIX")
def test_a_child_that_writes_forever_leaves_nothing_behind(monkeypatch, tmp_path):
    """The card's L3 test: a stub that writes forever is refused with the typed error and no child is left behind.

    MAX_OUT is 1 MiB here so the kill is quick; the stub also starts a helper in the same process group, which the
    group kill has to take down with it. On the old read this ran to the whole 3 s clock (~40 MB buffered).
    """
    pids = tmp_path / "pids"
    monkeypatch.setenv("GUARD_STUB_PIDS", str(pids))
    monkeypatch.setattr(guard, "CHILD", _FOREVER_REPLY_CHILD)
    monkeypatch.setattr(guard, "MAX_OUT", 1 << 20)
    started = time.monotonic()
    with pytest.raises(words.TooComplex):
        guard.read(b"%PDF-1.7", limits=dataclasses.replace(guard.LIMITS, seconds=8.0))
    elapsed = time.monotonic() - started
    assert elapsed < 4.0, f"the read ran for {elapsed:.1f} s: it waited for the clock, not for MAX_OUT"
    assert pids.exists(), "the stub child never started"
    child, helper = (int(p) for p in pids.read_text(encoding="ascii").split())
    assert _gone(child) and _gone(helper), (child, helper)


def test_a_reply_that_passes_the_cap_and_goes_quiet_is_killed_at_the_cap(monkeypatch):
    """The cap is enforced on the bytes read, not on the chunks: a child that writes MAX_OUT + 100 bytes and then
    holds the pipe open without a further byte, or an EOF, must be killed when the cap is passed. A reader that
    waits for a full 64 KiB block would sit here until the clock instead (4 s here, vs the 8 s clock)."""
    monkeypatch.setattr(guard, "CHILD", _TAIL_REPLY_CHILD)
    monkeypatch.setattr(guard, "MAX_OUT", 1 << 20)
    started = time.monotonic()
    with pytest.raises(words.TooComplex):
        guard.read(b"%PDF-1.7", limits=dataclasses.replace(guard.LIMITS, seconds=8.0))
    elapsed = time.monotonic() - started
    assert elapsed < 4.0, f"the read ran for {elapsed:.1f} s: it waited on a full chunk or the clock, not on the cap"


def _gone(pid: int) -> bool:
    """Whether `pid` is gone, polling briefly: a killed grandchild can still be a zombie when it is reparented."""
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


def test_a_ten_megabyte_request_reaches_the_child(monkeypatch):
    """The request can be ~13 MB, so the chunked read needs its writer thread: the body goes out while the reply is
    read, and the child sees every byte instead of both sides deadlocking on full pipes."""
    monkeypatch.setattr(guard, "CHILD", _ECHO_LENGTH_CHILD)
    raw = b"x" * (10 * 1024 * 1024)
    pages, sizes, producer = guard.read(raw, limits=dataclasses.replace(guard.LIMITS, seconds=30.0))
    assert (pages, sizes) == ([], [])
    assert int(producer) > 13_000_000            # base64 of 10 MiB is 13,981,016 bytes, plus the JSON around it


def test_the_mask_guard_still_covers_a_name_on_an_opening_balance_line():
    """The fix round dropped header and balance lines from the guard's word list whole, so it went blind there.

    The view keeps only the generic lexicon on those lines, so that is all the guard may exempt; a merchant name or
    a number printed on the same line is still checked.
    """
    cells = [("Opening", 40), ("balance", 95), ("Zelle", 160), ("100.00", 500)]
    pages = [[{"text": t, "x0": x, "x1": x + 40, "top": 30, "bottom": 40} for t, x in cells]]
    sensitive = ai._sensitive_words(pages)
    assert "Zelle" in sensitive and "100.00" in sensitive
    assert "Opening" not in sensitive and "balance" not in sensitive
    with pytest.raises(PermissionError):
        ai.assert_masked("y30: 1@40-80:Opening 2@95-135:balance 3@160-200:Zelle 4@500-540:99.99", sensitive)


@pytest.mark.parametrize("pattern", [r"\d", r"\w", r"\S", r"^opening\sbalance$"])
def test_a_backslash_before_a_letter_is_refused(pattern):
    """`\\d` used to become the literal letter "d": the pattern silently meant something the caller never asked for."""
    with pytest.raises(ValueError, match="plain text"):
        templates.clean_config({**CFG, "skip_pattern": pattern})


def test_an_escaped_punctuation_character_is_still_plain_text():
    for pattern, line in ((r"total\ \(net\)", "Total (net)"), (r"1\+1", "1+1"), (r"a\.b", "a.b")):
        cleaned = templates.clean_config({**CFG, "skip_pattern": pattern})["skip_pattern"]
        assert re.search(cleaned, line, re.I), pattern
