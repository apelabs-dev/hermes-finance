"""The opt-in AI template proposal: a masked word-id view in, a validated number-free config out.

The model sees positions and word shapes only (digits -> 9, names/merchants -> x/X) and answers with word ids and
column roles. The config it implies runs locally and must reconcile; the model never writes an amount or a date.
"""

from __future__ import annotations

import json
import os
import re
import time

from .. import clibrain, prefs
from . import layout, replay as replay_mod, templates
from .numbers import money_like
from .words import KEEP_WORDS, Line, build_lines, mask_text

ENV = "FINANCE_PDF_AI"
ROLES = {"date", "desc", "debit", "credit", "amount", "balance", "ignore"}
DATE_CODES = {"unreadable_date", "no_period"}   # refusals about the dates themselves, not the mapping
SIGNS = {"explicit", "unsigned_is_debit", "unsigned_is_credit", "debit_positive", "credit_positive"}
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


def word_view(pages: list[list[dict]], *, variant: int = 0, max_lines: int = 160):
    """(the masked id view text, {id: Word}) for the pages with money-like words."""
    lines = build_lines(pages)
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
    n = 0
    for li, ln in enumerate(lines):
        if ln.page not in chosen:
            continue
        parts = []
        for w in ln.words:
            n += 1
            gid[n] = w
            parts.append(f"{n}@{round(w.x0)}-{round(w.x1)}:{mask_text(w.text)}")
        out.append(f"L{li} p{ln.page + 1} y{round(ln.top)}: " + " ".join(parts))
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
            if len(t) >= 3 and t.lower() not in KEEP_WORDS:
                letters.add(t)
        if _DIGITS.search(w) or money_like(w):
            if len(w) >= 4:
                numbers.add(w)
    return letters, numbers


def assert_masked(sent: str, real_words: list[str]) -> None:
    """Raise if an original statement word, number, money or date token survived into the text sent."""
    letters, numbers = _sensitive(real_words)
    for t in letters:
        if re.search(rf"(?<![^\W\d_]){re.escape(t)}(?![^\W\d_])", sent):
            raise PermissionError("unmasked word from a statement found in a model prompt")
    for t in numbers:
        if t in sent:
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
    real_words = [w["text"] for page in pages for w in page]
    out = []
    for k in range(TRIES):
        view, _gid = word_view(pages, variant=k)
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
    real_words = [w["text"] for page in pages for w in page]
    outcomes: list[str] = []
    proposal: dict | None = None
    gated: tuple[layout.Parsed, replay_mod.Replay] | None = None
    for k in range(TRIES):
        if deadline - time.monotonic() < clibrain.MIN_CALL:
            outcomes.append("no_time")
            break
        view, gid = word_view(pages, variant=k)
        prompt = _prompt(view, k)
        try:
            assert_masked(view, real_words)
        except PermissionError:
            outcomes.append("guard_block")
            continue
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

