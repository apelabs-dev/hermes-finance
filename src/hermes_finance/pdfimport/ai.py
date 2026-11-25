"""The opt-in AI template proposal: a masked word-id view in, a validated number-free config out.

The model sees positions and word shapes only (digits -> 9, names/merchants -> x/X) and answers with word ids and
column roles. The config it implies runs locally and must reconcile; the model never writes an amount or a date.
"""

from __future__ import annotations

import json
import os
import re
import time
from array import array

from .. import clibrain, prefs
from . import layout, replay as replay_mod, templates
from .numbers import money_like
from .words import KEEP_WORDS, Line, TooComplex, build_lines, mask_text

ENV = "FINANCE_PDF_AI"
ROLES = {"date", "desc", "debit", "credit", "amount", "balance", "ignore"}
DATE_CODES = {"unreadable_date", "no_period"}   # refusals about the dates themselves, not the mapping
SIGNS = {"explicit", "unsigned_is_debit", "unsigned_is_credit", "debit_positive", "credit_positive"}
# The AI view's ceiling: two pages of 160 lines is ~6.6 KB on a 40-page statement, and even a dense table at 1,000
# characters a line stays under 320 KB. Past this the view is not a statement's, so it is refused (`pdf_too_complex`)
# instead of screened: the guard is linear now, but a prompt that size is not something a model should be sent.
MAX_VIEW_CHARS = 512_000
# The guard's number half is one Aho-Corasick automaton per batch of about `_AUTOMATON_NODES` nodes, built from the
# tokens (a step a character) with the view scanned through it once (a step a character), so the work is
# O(len(view) + sum(len(token))) and a token is found wherever it occurs - whitespace and all, which is exactly
# 768794f's `token in sent`. No token is searched for on its own, no token set is sorted, and there is no bound to hit
# and so no verdict 768794f never returned. The node count is checked after a token has been added, so a batch can
# overshoot the budget by less than one supported word (200 characters); a crafted token set past it is screened by the
# next automaton over the same view, which is one more traversal - at most 11 under the 2,000,000-character read cap. A
# statement's own words share prefixes and fit one automaton - numcost.pdf's worst case is 5,244 tokens, 7 thousand
# nodes, one batch and 0.03 s for a 478,586-character view, five times a click.
_AUTOMATON_NODES = 200_000
HINTS = [
    "",
    "\nHint: look for the line whose words name the columns (date, description, amounts, balance).",
    "\nHint: x positions matter most: a column's words share x ranges down the page.",
    "\nHint: check whether money in and money out are separate columns or one signed column.",
    "\nHint: if the table has no header line, pick the line just above the first dated transaction as header_ids.",
]
INSTR = """You read a bank statement as positioned words and propose a parsing template. Reply with ONE JSON object only.

Each line: L<line> p<page> y<top>: then words as <id>@<x0>-<x1>:<word>.
Letters of uncommon words are shown as x/X and digits as 9 (shape only).

JSON keys:
- "header_ids": ids of the words of the line that starts the transaction table (its column header line; if the
  table has no header, a line just above the first transaction).
- "columns": list of {{"role": R, "ids": [word ids]}} with R one of date, desc, debit, credit, amount, balance.
  Give for each column the ids of its header word(s) and/or its words in ONE transaction line. Use debit/credit
  when money out and money in are separate columns, amount when one column holds both. balance = running balance.
- "end_ids": ids of the words of the first line after the last transaction, or [] if none.
- "amount_sign": for an amount column: "explicit", "unsigned_is_debit", "unsigned_is_credit", or null.
- "year_missing": true if dates and period print no year.
Never write any amount, date or description text in the JSON: word ids, roles and the keys above only.{hint}

{view}
"""


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in ("off", "0", "false", "no")


def _parse_json(text: str):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):]
    start = t.find("{")
    if start < 0:
        raise ValueError("no_json")
    obj, _ = json.JSONDecoder().raw_decode(t[start:])
    return obj


def _structural(ln: Line) -> bool:
    """A header or balance line: the two places a generic word is the statement's own vocabulary, not a name."""
    return (layout.header_columns(ln) is not None
            or any(rx.search(ln.text) for rx in (layout.OPEN_RE, layout.CLOSE_RE, layout.FIRST_LAST_RE)))


def _sensitive_letters(lines: list[Line]) -> set[str]:
    """The letter runs `_sensitive` charges against the view, from the lines a view is built from.

    Exactly `_sensitive_words`'s non-exempt runs: one definition of sensitivity, so a view can keep the structural
    words the guard exempts and mask the ones it does not, and the two cannot drift.
    """
    return _sensitive(_sensitive_words_lines(lines))[0]


def _leaks(text: str, sensitive: set[str]) -> bool:
    """Whether `mask_text(text, keep=True)` would print one of `sensitive` back into the view.

    Only a run `mask_text` keeps can leak: a KEEP_WORDS run, compared as the guard compares it (exact case).
    """
    return any(m.group(0) in sensitive
               for m in _SENSITIVE_WORD.finditer(text) if m.group(0).lower() in KEEP_WORDS)


def word_view(pages: list[list[dict]], *, variant: int = 0, max_lines: int = 160,
              sensitive: set[str] | None = None):
    """(the masked id view text, {id: Word}) for the pages with money-like words.

    A structural line keeps its lexicon words, but not one that is sensitive somewhere else in the statement: a
    description "ATM Deposit Balance Credit" makes "Balance" and "Credit" sensitive, and the guard checks the whole
    view for them, so keeping the header's copies refused every try and the privacy screen showed nothing to send.
    The word is masked wherever it is kept, which is what the guard demands and never what it allows (round 6).

    `sensitive` is the guard's own letter set (`_sensitive_letters`); a caller that builds several views passes it so
    the statement is screened once, and one that does not gets it computed here.

    A view over `MAX_VIEW_CHARS` raises `words.TooComplex` (`pdf_too_complex` at the route): the AI view is a prompt,
    and a statement's own two pages never come near it.
    """
    lines = build_lines(pages)
    if sensitive is None:
        sensitive = _sensitive_letters(lines)
    by_page: dict[int, list[Line]] = {}
    for ln in lines:
        by_page.setdefault(ln.page, []).append(ln)
    money_pages = [p for p, lns in by_page.items()
                   if sum(1 for ln in lns for w in ln.words if money_like(w.text)) >= 3]
    chosen = money_pages[:2] if variant != 3 or len(money_pages) < 2 else money_pages[1:3]
    if not chosen:
        chosen = sorted(by_page)[:1]
    gid: dict[int, object] = {}
    out: list[str] = []
    chars = 0
    n = 0
    for li, ln in enumerate(lines):
        if ln.page not in chosen:
            continue
        keep = _structural(ln)
        parts = []
        for w in ln.words:
            n += 1
            gid[n] = w
            shape = mask_text(w.text, keep=keep and not _leaks(w.text, sensitive))
            parts.append(f"{n}@{round(w.x0)}-{round(w.x1)}:{shape}")
        row = f"L{li} p{ln.page + 1} y{round(ln.top)}: " + " ".join(parts)
        out.append(row)
        chars += len(row) + 1
        if chars > MAX_VIEW_CHARS:
            raise TooComplex()
        if len(out) >= max_lines * len(chosen):
            break
    return "\n".join(out), gid


def _ids(gid: dict, ids) -> list:
    return [gid[i] for i in ids if isinstance(i, int) and i in gid]


def to_config(obj, gid: dict) -> dict:
    """Model JSON -> a validated parser config. Raises ValueError (short code) on a bad candidate."""
    if not isinstance(obj, dict):
        raise ValueError("not_object")
    if templates.contains_data(obj):
        raise ValueError("json_has_data")
    cols = []
    for c in obj.get("columns") or []:
        role = c.get("role") if isinstance(c, dict) else None
        ids = [i for i in (c.get("ids") or []) if isinstance(i, int)] if isinstance(c, dict) else []
        if role not in ROLES or not ids:
            raise ValueError("bad_column")
        if any(i not in gid for i in ids):
            raise ValueError("unknown_id")
        if role == "ignore":
            continue
        ws = _ids(gid, ids)
        cols.append({"role": role, "x0": round(min(w.x0 for w in ws), 1), "x1": round(max(w.x1 for w in ws), 1)})
    roles = [c["role"] for c in cols]
    if "date" not in roles or not any(r in ("debit", "credit", "amount") for r in roles):
        raise ValueError("missing_roles")
    if roles.count("balance") > 1:
        raise ValueError("two_balances")
    hdr_ids = [i for i in (obj.get("header_ids") or []) if isinstance(i, int)]
    if not hdr_ids or any(i not in gid for i in hdr_ids):
        raise ValueError("bad_header")
    hws = _ids(gid, hdr_ids)
    words_out = []
    for w in hws:
        text = w.text.lower()
        if len(text) >= 2 and text in KEEP_WORDS and not re.search(r"\d", text):
            words_out.append(text)
        if len(words_out) >= 6:
            break
    if not words_out:
        raise ValueError("header_without_words")
    cfg: dict = {"columns": cols, "header_words": words_out}
    end_ids = [i for i in (obj.get("end_ids") or []) if isinstance(i, int) and i in gid]
    if end_ids:
        end_words = _ids(gid, end_ids)
        cfg["table_end_y"] = round(min(w.top for w in end_words), 1)
        ew = [w.text.lower() for w in end_words][:2]
        if ew and all(len(t) >= 2 and t in KEEP_WORDS and not re.search(r"\d", t) for t in ew):
            cfg["table_end_pattern"] = "^" + re.escape(" ".join(ew))[:190]
    sign = obj.get("amount_sign")
    if sign in SIGNS:
        cfg["amount_sign"] = sign
    # A "date_order" in the reply is ignored: the model sees dates as 99/99/9999, so it can't know the order. The local
    # resolver, which reads the digits, decides it (and refuses an ambiguous statement) on every path.
    if obj.get("year_missing") is True:
        cfg["year_placeholder"] = True
    return templates.clean_config(cfg)


# ---------------------------------------------------------------- privacy guard

_SENSITIVE_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_DIGITS = re.compile(r"\d{4,}")


def _sensitive(real_words: list[str]) -> tuple[set[str], set[str]]:
    """(letter words that must not survive, number/date/money tokens that must not survive)."""
    letters, numbers = set(), set()
    for w in real_words:
        for m in _SENSITIVE_WORD.finditer(w):
            t = m.group(0)
            if len(t) >= 3:
                letters.add(t)
        if _DIGITS.search(w) or money_like(w):
            if len(w) >= 4:
                numbers.add(w)
    return letters, numbers


def _sensitive_words(pages: list[list[dict]]) -> list[str]:
    """The words a prompt must not contain.

    Outside the header and balance lines every word is sensitive: a generic word ("New", "Money", a month) can be a
    merchant's or a person's name there, which is why the view masks the lexicon too. On those two structural lines
    the view keeps the lexicon on purpose, so exactly those tokens are exempt from the guard: a name or a number
    printed on an opening-balance line is still checked (the review's fix round exempted the whole line).
    """
    return _sensitive_words_lines(build_lines(pages))


def _sensitive_words_lines(lines: list[Line]) -> list[str]:
    """`_sensitive_words` for lines already built (a view builds the same ones)."""
    out: list[str] = []
    for ln in lines:
        keep = _structural(ln)
        out.extend(w.text for w in ln.words if not (keep and _kept_by_mask(w.text)))
    return out


def _kept_by_mask(text: str) -> bool:
    """Whether the masked view keeps this token on a structural line: every letter run in it is a KEEP_WORDS word.

    A token with no letters ("100.00"), or with one outside the lexicon ("Zelle"), is not exempt.
    """
    runs = [m.group(0).lower() for m in _SENSITIVE_WORD.finditer(text)]
    return bool(runs) and all(run in KEEP_WORDS for run in runs)


def _scan_for_token(sent: str, tokens: list[str], start: int, budget: int) -> tuple[bool, int]:
    """(whether a token of `tokens[start:]` occurs in `sent`, the index this automaton covered).

    Aho-Corasick: the automaton is the tokens' trie plus a failure link for every node, and `sent` goes through it
    once, following a failure link when a character has no edge and stopping at the first node a token ends at. So a
    token is found wherever it occurs in `sent` - whitespace and all, exactly 768794f's `token in sent` - in one pass
    over the tokens and one over the view, whatever their number.

    `budget` bounds the trie's nodes, not its tokens: the tokens past it go to the next automaton (the caller loops),
    which is another pass over the view. The count is checked after a token has been added, so one batch holds at most
    about `budget` nodes and may overshoot by less than one supported word (200 characters). Only a crafted token set
    far past a statement's reaches a second automaton - an automaton a character of token is the ceiling, and
    `_AUTOMATON_NODES` is 200,000.
    """
    children: list[dict[str, int]] = [{}]           # node -> {character: child node}
    ends: bytearray = bytearray(b"\x00")            # node -> is a token's last character
    i, last = start, len(tokens)
    while i < last:
        token = tokens[i]
        if not token:                               # `"" in sent` is true; `_sensitive` never makes one
            return True, i
        node = 0
        for ch in token:
            nxt = children[node].get(ch)
            if nxt is None:
                nxt = len(children)
                children[node][ch] = nxt
                children.append({})
                ends.append(0)
            node = nxt
        ends[node] = 1
        i += 1
        if len(children) >= budget and i < last:
            break
    fail = array("i", bytes(4 * len(children)))     # node -> the longest proper suffix of it that is a node
    queue = list(children[0].values())              # breadth first, so a failure link always exists before its use
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        at = fail[node]
        for ch, nxt in children[node].items():
            fallback = at
            while fallback and ch not in children[fallback]:
                fallback = fail[fallback]
            fallback = children[fallback].get(ch, 0)
            fail[nxt] = fallback
            if ends[fallback]:
                ends[nxt] = 1                       # a token ends here too, through the failure link
            queue.append(nxt)
    node = 0
    for ch in sent:
        nxt = children[node].get(ch)
        if nxt is None:
            while node:
                node = fail[node]
                nxt = children[node].get(ch)
                if nxt is not None:
                    break
            if nxt is None:
                continue                            # at the root with no edge for this character
        node = nxt
        if ends[node]:
            return True, i
    return False, i


def assert_masked(sent: str, real_words: list[str]) -> None:
    """Raise if an original statement word, number, money or date token survived into the text sent.

    One pass over the view per automaton batch (at most 11 under the current read caps) and one over the words,
    whatever their number, and the verdict is exactly 768794f's.

    - A letter token is a maximal `[^\\W\\d_]+` run and 768794f's lookarounds only matched a maximal run of `sent`,
      so the letter half is the intersection of the statement's letter runs with the view's own runs: one pass over
      the view, whatever the number of runs. (One pass per distinct run over the whole view was the round-2 Medium:
      187 s of parent CPU on an 84 KB file, five tries a click.)
    - 768794f tested `token in sent` for every number token, so a statement word holding whitespace (a crafted
      ToUnicode glyph can print `1234 5678`) was caught wherever it landed. The round-3 rewrite could only see a
      token inside one whitespace-delimited chunk and passed those words; it also compared every token with every
      digit-bearing chunk, which on a 65 KB file is 11,998,272 pairs and 141 s a click. Round 4 indexed the view's
      4-character windows but sorted every survivor before charging any of that work, and charged it against a bound
      that refused a file 768794f screened. Here the tokens are the automaton's patterns and the view is one pass
      through it: a token is found wherever it occurs, whitespace and all, in one pass over each, and no input is
      refused.
    """
    letters, numbers = _sensitive(real_words)
    if letters and letters & set(_SENSITIVE_WORD.findall(sent)):
        raise PermissionError("unmasked word from a statement found in a model prompt")
    if not numbers:
        return
    tokens = list(numbers)
    i, last = 0, len(tokens)
    while i < last:
        found, i = _scan_for_token(sent, tokens, i, _AUTOMATON_NODES)
        if found:
            raise PermissionError("unmasked number from a statement found in a model prompt")


# ---------------------------------------------------------------- one call

def _ask(cli_id: str, exe, prompt: str, deadline: float):
    """One model turn through the app's CLI seam. The eval script replaces this with its pinned-model call."""
    return clibrain.complete(cli_id, exe, {"prompt": prompt, "cli": cli_id}, deadline)


TRIES = 5


def _prompt(view: str, k: int) -> str:
    """The exact text of try k: what propose() sends and what outgoing() shows, from the one place."""
    return INSTR.format(hint=HINTS[k], view=view)


def outgoing(pages: list[list[dict]]) -> list[str]:
    """Every prompt propose() could send for these pages, word for word and in order, without sending anything: the
    privacy screen shows these before the person presses Send. A try whose view fails the mask guard is never sent,
    so it isn't listed; propose() stops early on an accepted candidate, so this is the most that can leave."""
    real_words = _sensitive_words(pages)
    sensitive = _sensitive(real_words)[0]
    out = []
    for k in range(TRIES):
        view, _gid = word_view(pages, variant=k, sensitive=sensitive)
        try:
            assert_masked(view, real_words)
        except PermissionError:
            continue
        out.append(_prompt(view, k))
    return out


def propose(conn, pages: list[list[dict]], sizes, *, anchors: dict, year: int | None, totals: dict,
            carry=()) -> tuple[dict | None, layout.Parsed | None, replay_mod.Replay | None, list[str]]:
    """Try up to five candidates; return the first that reconciles and passes the same problem gate as the generic and
    template paths (pdfimport._code). Otherwise (the first valid proposal, the first candidate the gate refused for a
    date or period problem (DATE_CODES: the model never sees digits, so it can't fix those) or None, its replay or None,
    the outcomes): the caller gates that candidate again and refuses with its code when the local parser hit the same."""
    from . import _code
    if not enabled() or clibrain.off():
        from . import PdfRefused
        raise PdfRefused("ai_unavailable")
    cli_id = prefs.get(conn, "ai.cli")
    exe, why = clibrain.ready(conn, cli_id)
    if why:
        from . import PdfRefused
        raise PdfRefused("ai_unavailable")
    deadline = time.monotonic() + 90.0
    real_words = _sensitive_words(pages)
    sensitive = _sensitive(real_words)[0]
    # Build and screen every try's view before the first call: a try whose view is past `MAX_VIEW_CHARS` refuses the
    # whole file (`pdf_too_complex`), and it must do so before a prompt the privacy screen never showed leaves (L1:
    # with the views built lazily, a file whose only over-cap try was the fourth made three calls first). `outgoing`
    # builds the same five views, so the screen and Send send the same text.
    views: list[tuple[str, dict] | None] = []
    for k in range(TRIES):
        try:
            view, gid = word_view(pages, variant=k, sensitive=sensitive)
            assert_masked(view, real_words)
        except TooComplex:
            from . import PdfRefused
            raise PdfRefused("pdf_too_complex") from None
        except PermissionError:
            views.append(None)                  # guard_block at its turn, as before
            continue
        views.append((view, gid))
    outcomes: list[str] = []
    proposal: dict | None = None
    gated: tuple[layout.Parsed, replay_mod.Replay] | None = None
    for k in range(TRIES):
        if deadline - time.monotonic() < clibrain.MIN_CALL:
            outcomes.append("no_time")
            break
        if views[k] is None:
            outcomes.append("guard_block")
            continue
        view, gid = views[k]
        prompt = _prompt(view, k)
        try:
            reply = _ask(cli_id, exe, prompt, deadline)
            text = reply.text
        except clibrain.CliError:
            outcomes.append("call_failed")
            break
        except Exception:  # noqa: BLE001 - a broken child or reply is one failed candidate, never a crash
            outcomes.append("call_failed")
            break
        try:
            cfg = to_config(_parse_json(text), gid)
        except (ValueError, TypeError, AttributeError) as exc:
            outcomes.append(str(exc) if re.fullmatch(r"[a-z_]+", str(exc)) else "bad_json")
            continue
        if proposal is None:
            proposal = cfg
        try:
            parsed = layout.parse(pages, sizes, {**cfg, **anchors}, year=year)
            verdict = replay_mod.replay(parsed.rows, parsed.opening, parsed.closing, totals or None,
                                        parsed.carry_checks)
        except Exception:  # noqa: BLE001 - one bad candidate must not end the proposal
            outcomes.append("parse_failed")
            continue
        code = _code(parsed.problems, parsed, verdict)
        if verdict.accepted and code is None and parsed.period is not None:
            outcomes.append("accepted")
            return cfg, parsed, verdict, outcomes
        outcomes.append(code or verdict.reason or "no_period")
        if code in DATE_CODES and gated is None:
            gated = (parsed, verdict)
    if gated is not None:
        return proposal, gated[0], gated[1], outcomes
    return proposal, None, None, outcomes

