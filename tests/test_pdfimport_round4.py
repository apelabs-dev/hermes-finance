"""PR #28 round 4/5 (0.5): the AI mask guard is 768794f's verdict again (whitespace number words too) and linear.

Round 3 rewrote the guard as one pass over the view and lost a case: a statement word that holds whitespace (a
crafted ToUnicode glyph can print `1234 5678`) was blocked by `token in sent` and passed by the chunk loop (F1,
4,776 of 300,000 fuzz cases). The chunk loop was also still a product - every distinct number token against every
digit-bearing chunk - and on numcost.pdf (65 KB, under every cap) that is 11,998,272 pairs and 141 s per Ask AI
click (F2). Round 4 decided the tokens against an index of the view's 4-character windows but sorted every survivor
before charging any of that work, and the characters it charged were refused past a bound that 768794f never had.
Round 5 (the code review's Major) replaces both: the tokens are one Aho-Corasick automaton and the view goes through
it once, so the work is the view plus the tokens and no token set is sorted or searched for on its own.

Every PDF here is synthetic and built by `pdf_fixtures.py`. A step that could hang the suite on a regression runs in
a child process with a hard deadline: the parent-side work these tests bound holds the GIL, so an in-process timer
never fires on it. Test ids carry an explicit name (no bytes, no timestamps) so they are stable under xdist.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_finance
from hermes_finance import clibrain
from hermes_finance.pdfimport import PdfRefused, ai, templates, words
from hermes_finance.pdfimport.numbers import money_like

from pdf_fixtures import number_cost_statement, statement

SRC = str(Path(hermes_finance.__file__).resolve().parent.parent)


# ---------------------------------------------------------------- child processes

_PDF_CHILD = r'''
import json, sys, time
from datetime import date
from hermes_finance.pdfimport import PdfRefused, ai_outgoing, guard

# Every file here passes the content caps, and the read clock is not the step these tests bound: raise it and let
# this process's own deadline (the parent's kill) be the stop.
guard.LIMITS = guard.Limits(seconds=120.0)

raw = open(sys.argv[1], "rb").read()
started, cpu = time.monotonic(), time.process_time()
try:
    ai_outgoing(raw)
    outcome = "ok"
except PdfRefused as exc:
    outcome = exc.code
except Exception as exc:        # noqa: BLE001 - a test must see a crash, and it must not look like a refusal
    outcome = "error:" + type(exc).__name__
print(json.dumps({"outcome": outcome, "wall": time.monotonic() - started, "cpu": time.process_time() - cpu}))
'''

_GUARD_CHILD = r'''
import json, sys, time
from hermes_finance.pdfimport import ai, words


def once(view, tokens):
    started, cpu = time.monotonic(), time.process_time()
    try:
        ai.assert_masked(view, tokens)
        outcome = "pass"
    except PermissionError:
        outcome = "block"
    except words.TooComplex:
        outcome = "too_complex"
    except Exception as exc:    # noqa: BLE001 - a crash must not look like a verdict
        outcome = "error:" + type(exc).__name__
    return {"outcome": outcome, "wall": time.monotonic() - started, "cpu": time.process_time() - cpu}


out = []
for i in range(1, len(sys.argv), 2):
    with open(sys.argv[i], encoding="utf-8") as fh:
        view = fh.read()
    with open(sys.argv[i + 1], encoding="utf-8") as fh:
        tokens = fh.read().split("\n")
    out.append(once(view, tokens))
print(json.dumps(out))
'''


def _child(tmp_path, script: str, args: list[str], deadline: float):
    """The child's parsed JSON (a list, one entry a step), or None when it was still running at `deadline`."""
    path = tmp_path / "round4_child.py"
    path.write_text(script)
    env = {"PYTHONPATH": SRC, "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "PYTHONDONTWRITEBYTECODE": "1"}
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen([sys.executable, str(path), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, **kwargs)
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
        pytest.fail(f"the guard child failed: {err.decode()[-800:]}")
    return json.loads(out.decode().strip().splitlines()[-1])


def _guard_cases(tmp_path, cases: list[tuple[str, list[str], str]], deadline: float, name: str):
    """{name: {outcome, wall, cpu}} for each (view, tokens) guard case, each timed in one child process."""
    args: list[str] = []
    for view, tokens, label in cases:
        view_path = tmp_path / f"{name}-{label}.view"
        token_path = tmp_path / f"{name}-{label}.tokens"
        view_path.write_text(view, encoding="utf-8")
        token_path.write_text("\n".join(tokens), encoding="utf-8")
        args += [str(view_path), str(token_path)]
    out = _child(tmp_path, _GUARD_CHILD, args, deadline)
    assert out is not None, f"{name}: the guard was still running after {deadline} s"
    for label, answer in zip((c[2] for c in cases), out):
        assert not answer["outcome"].startswith("error:"), f"{name}/{label}: {answer['outcome']}"
    return {label: answer for label, answer in zip((c[2] for c in cases), out)}


def _pdf_case(tmp_path, raw: bytes, deadline: float, name: str):
    pdf = tmp_path / f"{name}.pdf"
    pdf.write_bytes(raw)
    answer = _child(tmp_path, _PDF_CHILD, [str(pdf)], deadline)
    assert answer is not None, f"{name}: the request was still running after {deadline} s"
    assert not answer["outcome"].startswith("error:"), answer["outcome"]
    return answer


# ---------------------------------------------------------------- the shapes

def number_word_columns(lines: int, tokens: int) -> tuple[str, list[str]]:
    """numcost.pdf's guard-level shape: a view of `lines` lines of 140 masked 200-character words of 9s, and
    `tokens` distinct `9`*a + d + `9`*b statement words (the report's adversarial shape)."""
    rows = []
    n = 0
    for li in range(lines):
        parts = []
        for c in range(140):
            n += 1
            parts.append(f"{n}@{10 + c * 3}-{210 + c * 3}:{'9' * 200}")
        rows.append(f"L{li} p1 y{760 - li * 10}: " + " ".join(parts))
    words_out = ["9" * a + d + "9" * b for d in "12345678" for a in range(92, 106) for b in range(2, 60)]
    random.Random(1).shuffle(words_out)
    return "\n".join(rows), words_out[:tokens]


# ---------------------------------------------------------------- 1. F1: a number word that holds whitespace

WHITESPACE_WORDS = [
    ("4012 8888 1881", "space"),
    ("1\u00a0234,56", "nbsp"),
    ("1\u202f234,56", "narrow-nbsp"),
    ("9999\u20089999", "punctuation-space"),
    ("4012\n8888", "newline"),
    ("DE89 3704 0044 0532 0130 00", "iban"),
]


@pytest.mark.parametrize("word,label", WHITESPACE_WORDS, ids=[c[1] for c in WHITESPACE_WORDS])
def test_a_number_word_that_holds_whitespace_is_blocked_again(word, label):
    """768794f tested `token in sent`, so a token holding whitespace was caught wherever it landed; round 3 only
    looked inside one whitespace-delimited chunk and passed every one of these (F1, a guard regression)."""
    with pytest.raises(PermissionError):
        ai.assert_masked(f"7@300-340:{word}", [word])


def test_a_token_split_across_two_chunks_is_still_not_a_match():
    """The whitespace fix restores the old substring test, not a whitespace-insensitive one (pins 768794f)."""
    for view in ("12 34", "12\t34", "12\u00a034"):
        ai.assert_masked(view, ["1234"])            # the word split by a space is not the word
    clean = "L4 p1 y40: 1@40-80:Opening 2@95-135:balance 3@500-540:99.99"
    ai.assert_masked(clean, ["Zelle", "Grocery", "1,234.56", "05/03/2024"])


# ---------------------------------------------------------------- 2. F2: one pass over the view, not a product

def test_the_number_half_does_not_compare_every_token_with_every_chunk(tmp_path):
    """11,998,272 token x chunk pairs (numcost.pdf's guard-level shape) answered by one automaton pass over the view."""
    view, tokens = number_word_columns(16, 5_244)
    chunks = 3 * 16 + 140 * 16
    assert len(tokens) * chunks == 11_998_272
    answer = _guard_cases(tmp_path, [(view, tokens, "numcost")], 25, "numcost")["numcost"]
    assert answer["outcome"] == "pass", answer["outcome"]
    assert answer["cpu"] < 1.0, f"the guard spent {answer['cpu']:.2f} s of CPU on the number half"


def test_the_number_half_grows_with_the_view_and_the_tokens_not_with_their_product(tmp_path):
    """Quadrupling both the view and the tokens (4 -> 16 lines, 1,300 -> 5,200 words) costs the guard about 4x:
    round 3's product spent 1.7 s and 26.4 s of CPU on these two shapes, 15x, which is what made F2."""
    small = number_word_columns(4, 1_300)
    big = number_word_columns(16, 5_200)
    answers = _guard_cases(tmp_path, [(*small, "small"), (*big, "big")], 120, "scaling")
    assert answers["small"]["outcome"] == answers["big"]["outcome"] == "pass"
    assert answers["big"]["cpu"] < 1.0, f"the big shape took {answers['big']['cpu']:.2f} s of CPU"
    ratio = answers["big"]["cpu"] / max(answers["small"]["cpu"], 0.005)
    assert ratio < 8.0, f"4x the view and 4x the tokens cost {ratio:.1f}x the time (the product would be ~16x)"


def test_a_view_of_many_distinct_digit_chunks_is_not_refused():
    """Round 3 refused on pairs alone: 16,008,000 pairs here, but the automaton scans the view once and matches none."""
    numbers = [str(50_000 + i) for i in range(4000)]     # "5000" is in no id, coordinate or 9-run of this view
    view = " ".join(f"{i}@40-80:9999" for i in range(4000))
    started = time.monotonic()
    ai.assert_masked(view, numbers)                  # nothing matched, and nothing was searched
    assert time.monotonic() - started < 1.0


def _near_miss_view(tokens: int = 4_000) -> tuple[str, list[str]]:
    """A 40,000-character view holding every 4-digit window, and `tokens` 8-digit words that are not in it.

    Every one of those words has all its 4-grams in the view, so round 4's 4-gram index ruled none of them out and
    its work bound charged every one; none of them occurs, so 768794f's answer is pass.
    """
    combos = [f"{i:04d}" for i in range(10_000)]
    rnd = random.Random(5)
    rnd.shuffle(combos)
    view = "".join(combos)
    absent: set[str] = set()
    while len(absent) < tokens:
        token = "".join(rnd.choice("0123456789") for _ in range(8))
        if token not in view:
            absent.add(token)
    return view, sorted(absent)


def test_a_number_heavy_view_is_never_refused_for_the_work_it_asks_for():
    """The old bound is gone: 4,000 near-miss words over a 40,000-character view are 160,032,000 characters of round
    4's work bound, and round 4 refused the file (`words.TooComplex`) where 768794f answered pass (review round 1:
    the guard must not add a refusal the reference cannot return)."""
    view, absent = _near_miss_view()
    assert sum(len(view) + len(t) for t in absent) == 160_032_000
    assert frozen_guard_768794f(view, absent) is None        # the reference verdict: pass
    ai.assert_masked(view, absent)                           # and so is the guard's, in well under a second


def test_a_twenty_thousand_word_set_is_screened_not_refused():
    """Twenty thousand absent words over that view is a pass, not a refusal: the guard builds one automaton from the
    words and scans the view once, so the word count is a pass over the words and nothing is sorted or refused."""
    view, absent = _near_miss_view()
    rnd = random.Random(9)
    many = list(absent)
    while len(many) < 20_000:
        token = "".join(rnd.choice("0123456789") for _ in range(8))
        if token not in view and token not in absent:
            many.append(token)
    started = time.monotonic()
    ai.assert_masked(view, many)
    assert time.monotonic() - started < 3.0                # 0.08 s unloaded; the bound is for a loaded box


def _trie_nodes(tokens: list[str]) -> int:
    """The node count an automaton builds for these tokens: the root plus every distinct prefix."""
    return 1 + len({t[:k] for t in tokens for k in range(1, len(t) + 1)})


def batch_tokens(count: int = 25_000) -> list[str]:
    """`count` distinct 16-digit words: about 285,000 trie nodes, so more than one automaton's 200,000 budget.

    Sorted, so the fixture is the same on every run; the guard's own order is the set's, which the test reads from
    the call it intercepts rather than assuming.
    """
    rnd = random.Random(17)
    out: set[str] = set()
    while len(out) < count:
        out.add("".join(rnd.choice("0123456789") for _ in range(16)))
    return sorted(out)


def test_a_token_set_bigger_than_one_automaton_still_finds_the_match(monkeypatch):
    """The real budget and the real batching: about 285,000 nodes are split into automatons of at least 200,000.

    Round 5's test built 110,964 nodes from 208,000 characters of 8-digit words, so it never reached a second batch:
    a mutant that scans only the first automaton (M03) or stops batching (M11) survived the whole suite. This test
    records the batches the guard actually scans, asserts a word absent pass, an early hit, and a hit that only the
    last batch can see - the case the mutants cannot answer.
    """
    view, _absent = _near_miss_view()
    token_words = batch_tokens()
    seen: list[tuple[int, int]] = []
    order: list[str] = []
    real = ai._scan_for_token

    def spy(sent, tokens, start, budget):
        if not order:
            order.extend(tokens)                     # the list the guard itself built (a set's order, not ours)
        found, stop = real(sent, tokens, start, budget)
        seen.append((start, stop))
        return found, stop

    monkeypatch.setattr(ai, "_scan_for_token", spy)
    ai.assert_masked(view, token_words)              # every word absent: a pass, and every batch is scanned
    assert len(seen) >= 2, f"the guard scanned {len(seen)} automaton(s): this set fits one"
    assert seen[0][0] == 0 and seen[-1][1] == len(order)
    nodes = [_trie_nodes(order[a:b]) for a, b in seen]
    assert nodes[0] >= ai._AUTOMATON_NODES, nodes           # the budget really was reached
    assert all(n >= ai._AUTOMATON_NODES for n in nodes[:-1]), nodes
    assert nodes[-1] < ai._AUTOMATON_NODES, nodes
    assert sum(nodes) > ai._AUTOMATON_NODES, nodes          # the set is more than one automaton's worth

    early = order[seen[0][0]]
    assert early not in view
    seen.clear()
    with pytest.raises(PermissionError):
        ai.assert_masked(view + " " + early, token_words)   # a word of the first batch
    assert len(seen) == 1 and seen[0][0] == 0, seen          # found without a second traversal

    seen.clear()
    ai.assert_masked(view, token_words)              # every batch again, to place the late word
    batch_starts = [start for start, _stop in seen]
    late_start = batch_starts[-1]
    late = order[late_start]
    assert not any(late in t for t in order[:late_start])    # no earlier batch can match it
    assert late not in view
    seen.clear()
    with pytest.raises(PermissionError):
        ai.assert_masked(view + " " + late, token_words)     # only the last automaton can find this one
    assert [start for start, _stop in seen] == batch_starts, seen


def test_a_match_near_the_end_of_a_supported_length_view_is_found():
    """Nothing pinned how much of the view is scanned: mutants that stop at the first 200,000 or 500,000 characters
    (M06, M07) survived the suite. A supported view is at most 512,000 characters, so a word in its last 200 must
    block exactly as 768794f's substring test blocks it, and the same view without the word must pass."""
    length = ai.MAX_VIEW_CHARS - 1_000
    token = "3141592653589793"
    offset = length - 200
    view = "A" * offset + token + "A" * (length - offset - len(token))
    assert len(view) == length and view.rfind(token) == offset > 500_000
    with pytest.raises(PermissionError):
        frozen_guard_768794f(view, [token])                  # 768794f's `token in sent`
    with pytest.raises(PermissionError):
        ai.assert_masked(view, [token])
    clean = "A" * length
    assert frozen_guard_768794f(clean, [token]) is None       # the no-match outcome of the same view
    ai.assert_masked(clean, [token])
    # a letter word at the same offset: the reference's lookarounds and the guard's run intersection both find it
    tail = "A" * offset + " Zelle " + "A" * (length - offset - len(" Zelle "))
    with pytest.raises(PermissionError):
        frozen_guard_768794f(tail, ["Zelle"])
    with pytest.raises(PermissionError):
        ai.assert_masked(tail, ["Zelle"])


@pytest.mark.parametrize("block", range(2), ids=["block-0", "block-1"])
def test_at_the_removed_work_boundary_the_guard_answers_what_768794f_answered(block):
    """Seeded and xdist-stable: word sets at and past the removed 24,000,000-character bound (300/600/900 words, so
    12 M, 24 M and 36 M charged characters), one real occurrence every fortieth case. 768794f's verdict both ways,
    with no refusal in between."""
    rnd = random.Random(f"boundary-{block}")
    view, absent = _near_miss_view()
    hits = 0
    for case in range(120):
        chosen = rnd.sample(absent, rnd.choice((300, 600, 900)))
        if case % 40 == 0:
            chosen[-1] = view[rnd.randrange(len(view) - 16):][:16]     # a real word of the view
            hits += 1
        old = _verdict(frozen_guard_768794f, view, chosen)
        try:
            ai.assert_masked(view, chosen)
            new = "pass"
        except PermissionError:
            new = "block"
        assert new == old, f"case {block}/{case}: 768794f said {old}, the guard said {new}"
    assert hits == 3                                    # the corpus is not all passes


def test_a_65_kb_number_heavy_statement_is_screened_in_budget(tmp_path):
    """numcost.pdf: 50 pages, 7,483 words, a 478,586-character view, 11,998,272 pairs - 141 s on 82cb0d1."""
    answer = _pdf_case(tmp_path, number_cost_statement(), 40, "numcost")
    assert answer["outcome"] == "ok", answer["outcome"]
    assert answer["cpu"] < 6.0, f"the ai_view path spent {answer['cpu']:.2f} s of parent CPU"


# ---------------------------------------------------------------- 3. the differential fuzz against 768794f

def test_the_automaton_answers_what_a_plain_search_answers():
    """The automaton on its own, over words the statement's own filters never let through (1-3 characters, prefixes of
    each other, repeats, words longer than the view), one automaton and one word an automaton: at every seeded case it
    says what `token in sent` says."""
    rnd = random.Random("automaton")
    for _ in range(3_000):
        alphabet = rnd.choice(("ab", "abc", "0123", "a9", "ab9/", "9"))
        sent = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 24)))
        tokens = ["".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 6)))
                  for _ in range(rnd.randint(1, 8))]
        for budget in (10_000, 1):                  # one automaton, then one automaton a word
            at, found = 0, False
            while at < len(tokens) and not found:
                found, at = ai._scan_for_token(sent, tokens, at, budget)
            assert found == any(t in sent for t in tokens), (sent, tokens, budget)


_FROZEN_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)
_FROZEN_DIGITS = re.compile(r"\d{4,}")
_FROZEN_PATTERNS: dict[str, re.Pattern] = {}


def _frozen_pattern(token: str) -> re.Pattern:
    pattern = _FROZEN_PATTERNS.get(token)
    if pattern is None:
        pattern = _FROZEN_PATTERNS[token] = re.compile(rf"(?<![^\W\d_]){re.escape(token)}(?![^\W\d_])")
    return pattern


def _frozen_sensitive(real_words: list[str]) -> tuple[set[str], set[str]]:
    """768794f's `ai._sensitive`, frozen: letter runs of 3+ characters, whole digit or money words of 4+."""
    letters, numbers = set(), set()
    for w in real_words:
        for m in _FROZEN_LETTERS.finditer(w):
            t = m.group(0)
            if len(t) >= 3:
                letters.add(t)
        if _FROZEN_DIGITS.search(w) or money_like(w):
            if len(w) >= 4:
                numbers.add(w)
    return letters, numbers


def frozen_guard_768794f(sent: str, real_words: list[str]) -> None:
    """768794f's `ai.assert_masked`, frozen: a statement letter run must not survive as a maximal run of the view,
    and a statement number/money/date token must not occur in the view at all - `token in sent`, whitespace and all.

    This is the reference the new guard's verdicts are compared against below. `_frozen_sensitive` is 768794f's
    `_sensitive` word for word, and the lookarounds are 768794f's own: the only change is that the compiled pattern
    is reused per token (the fuzz runs 300,000 cases).
    """
    letters, numbers = _frozen_sensitive(real_words)
    for t in letters:
        if _frozen_pattern(t).search(sent):
            raise PermissionError("unmasked word from a statement found in a model prompt")
    for t in numbers:
        if t in sent:
            raise PermissionError("unmasked number from a statement found in a model prompt")


_FUZZ_LETTERS = list("abcXYZxXéÉßẞǅİıΣσςДж漢カअआकि्ًعرب")
_FUZZ_DIGITS = (list("0123456789") + list("٠١٢٣٤٥٦٧٨٩") + list("०१२३") + list("０１２３９")
                + list("²³①⑨Ⅻ½"))
_FUZZ_PUNCT = list(".,-/$€()'’:@_%#+*")
_FUZZ_SPACES = [" ", "\u00a0", "\u2009", "\u3000", "\x1c", "\x85", "\u2028", "\t", "\n", "\u200b", "\u202f"]
_FUZZ_COMBINING = ["\u0301", "\u093f", "\u094d", "\u0651"]


def _fuzz_word(rnd: random.Random) -> str:
    kind = rnd.random()
    n = rnd.randint(1, 14)
    if kind < 0.30:
        pool = _FUZZ_LETTERS
    elif kind < 0.55:
        pool = _FUZZ_DIGITS[:10] + _FUZZ_PUNCT[:4]
    elif kind < 0.75:
        pool = _FUZZ_DIGITS + _FUZZ_PUNCT
    elif kind < 0.90:
        pool = _FUZZ_LETTERS + _FUZZ_DIGITS + _FUZZ_PUNCT + _FUZZ_COMBINING
    else:
        pool = _FUZZ_LETTERS + _FUZZ_DIGITS + _FUZZ_PUNCT + _FUZZ_SPACES + _FUZZ_COMBINING
    word = "".join(rnd.choice(pool) for _ in range(n))
    if rnd.random() < 0.15:                      # in-word whitespace: only a multi-character glyph prints this
        k = rnd.randint(0, len(word))
        word = word[:k] + rnd.choice(_FUZZ_SPACES) + word[k:]
    return word


def _fuzz_variant(rnd: random.Random, word: str) -> str:
    """A leak of `word` into a view: as is, case-flipped, split by other whitespace, or glued into a bigger word."""
    c = rnd.random()
    if c < 0.2:
        return word.upper()
    if c < 0.4:
        return word.lower()
    if c < 0.5:
        return word.swapcase()
    if c < 0.6 and len(word) > 2:
        k = rnd.randint(1, len(word) - 1)
        return word[:k] + rnd.choice(_FUZZ_SPACES) + word[k:]
    if c < 0.7:
        return rnd.choice(["x", "9", "_", "1", "é", ""]) + word + rnd.choice(["x", "9", "_", "1", "é", ""])
    return word


def fuzz_case(rnd: random.Random) -> tuple[str, list[str]]:
    real = [_fuzz_word(rnd) for _ in range(rnd.randint(1, 12))]
    parts = []
    for i in range(rnd.randint(1, 15)):
        c = rnd.random()
        if c < 0.35:
            from_word = rnd.choice(real)
            parts.append(f"{i}@{rnd.randint(0, 9999)}-{rnd.randint(0, 9999)}:" + _fuzz_variant(rnd, from_word))
        elif c < 0.5:
            parts.append(rnd.choice(real))                          # a raw leak
        elif c < 0.7:
            parts.append(f"L{rnd.randint(0, 999)} p{rnd.randint(1, 50)} y{rnd.randint(0, 9999)}:")
        else:
            parts.append(_fuzz_word(rnd))
    return rnd.choice([" ", " ", " ", "\n", "\u00a0", ""]).join(parts), real


def _verdict(guard, sent: str, real: list[str]) -> str:
    try:
        guard(sent, real)
        return "pass"
    except PermissionError:
        return "block"


FUZZ_SEED = 20261124
FUZZ_BLOCKS = 6
FUZZ_PER_BLOCK = 50_000


@pytest.mark.parametrize("block", range(FUZZ_BLOCKS), ids=[f"block-{i}" for i in range(FUZZ_BLOCKS)])
def test_the_guard_answers_exactly_what_768794f_answered(block):
    """300,000 seeded cases (12 statement words and 15 view parts each, letters, every digit script, whitespace,
    combining marks, case variants, glue and chunk splits): ZERO verdict differences, in either direction."""
    rnd = random.Random(f"{FUZZ_SEED}-{block}")
    for i in range(FUZZ_PER_BLOCK):
        sent, real = fuzz_case(rnd)
        old = _verdict(frozen_guard_768794f, sent, real)
        try:
            ai.assert_masked(sent, real)
            new = "pass"
        except PermissionError:
            new = "block"
        except words.TooComplex:
            pytest.fail(f"case {block}/{i}: the guard refused a small view as too complex: "
                        f"sent={sent!r} real={real!r}")
        assert new == old, f"case {block}/{i}: 768794f said {old}, the guard said {new}: sent={sent!r} real={real!r}"


# ---------------------------------------------------------------- 4. the two Lows from the report

@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setenv("FINANCE_PDF_AI", "on")
    monkeypatch.setattr(clibrain, "off", lambda: False)
    monkeypatch.setattr(clibrain, "ready", lambda conn, cli: (Path("/bin/true"), ""))


def test_the_send_path_builds_and_checks_every_view_before_the_first_call(conn, ai_on, monkeypatch):
    """L1: only try 4's view was over the ceiling, and Send had already made 3 calls the screen never showed."""
    pages, sizes = words.read(statement([("05/03/2024", "Coffee", "10.00", "", "90.00"),
                                         ("14/03/2024", "Salary", "", "50.00", "140.00"),
                                         ("20/03/2024", "Rent", "40.00", "", "100.00")]))
    calls = []
    view_of = ai.word_view

    def word_view(pages, *, variant=0, max_lines=160, **kwargs):
        if variant == 3:
            raise words.TooComplex()            # only the fourth try's view is past the ceiling
        return view_of(pages, variant=variant, max_lines=max_lines, **kwargs)

    monkeypatch.setattr(ai, "word_view", word_view)
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: calls.append(1))
    with pytest.raises(PdfRefused) as caught:
        ai.propose(conn, pages, sizes, anchors={}, year=None, totals={}, carry=())
    assert caught.value.code == "pdf_too_complex"
    assert calls == [], f"{len(calls)} prompt(s) left for a view the privacy screen never showed"


def test_in_word_whitespace_becomes_a_space_in_the_view():
    """L2: a crafted glyph's own line break used to split a line of the AI view (a pre-existing 768794f behaviour)."""
    assert words.mask_text("9999\n9999") == "9999 9999"
    assert words.mask_text("9999\t9999") == "9999 9999"
    assert words.mask_text("Zelle\u2028Smith") == "Xxxxx Xxxxx"
    assert words.mask_text("13/03/2024 Coffee 10.00") == "99/99/9999 Xxxxxx 99.99"
    page = [{"text": "9999\n9999", "x0": 40.0, "x1": 300.0, "top": 20.0, "bottom": 28.0}]
    view, _gid = ai.word_view([page])
    assert len(view.splitlines()) == 1, view


# ---------------------------------------------------------------- 5. the review's template nit

def test_a_numeric_escape_is_refused_not_read_as_a_literal():
    """`\\1` and `\\d` are regex syntax, not escaped plain text: `isalpha()` let the digit through as a literal, so
    `^Page \\1$` was saved as a pattern that matches the text "Page 1" and nothing else (review round 1)."""
    for pattern in (r"^Page \1$", r"^Page \d$", r"^Page \9$"):
        with pytest.raises(ValueError, match="plain text"):
            templates._plain_pattern(pattern, "table_start_pattern")
    # punctuation is still escapable plain text, and an unescaped digit needs no escape at all
    assert templates._plain_pattern(r"^Total 1\.00$", "skip_pattern") == r"^Total\ 1\.00$"
    assert templates._plain_pattern(r"^Page 1$", "table_start_pattern") == r"^Page\ 1$"
