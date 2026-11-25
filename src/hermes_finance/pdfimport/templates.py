"""Teach-once layout templates: a masked layout fingerprint plus a validated, number-free parser config.

A saved template is the parser's overrides for one layout. Its fingerprint holds generic header roles and words,
the page size, the masked producer and the masked shape of the first lines: nothing a statement printed as a
merchant, a person or a number.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3

from . import layout
from ..db import now
from .words import KEEP_WORDS, build_lines, mask_text

ROLES = ("date", "desc", "debit", "credit", "amount", "balance")
CONFIG_KEYS = {"header_words", "columns", "date_order", "amount_sign", "row_start", "x_min", "year_placeholder",
               "skip_pattern", "opening_pattern", "closing_pattern", "table_end_pattern", "table_end_y",
               "table_start_pattern", "period_from_rows"}
SIGNS = ("explicit", "unsigned_is_debit", "unsigned_is_credit", "debit_positive", "credit_positive")
MAX_CONFIG_BYTES = 8192
MAX_PATTERN = 200

_MONEYISH = re.compile(r"\d[.,]\d{2}\b|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}")


def contains_data(obj) -> bool:
    """True when a config/AI reply carries an amount or a date anywhere (x positions and small integers are fine)."""
    if isinstance(obj, dict):
        return any(contains_data(k) or contains_data(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(contains_data(v) for v in obj)
    if isinstance(obj, str):
        return bool(_MONEYISH.search(obj))
    return False


def _number(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number")
    value = float(value)
    if not math.isfinite(value) or abs(value) > 10000:
        raise ValueError(f"{what} is out of range")
    return value


_PLAIN_META = set("\\.^$*+?{}[]|()")


def _plain_pattern(value: str, what: str) -> str:
    """One pattern from escaped plain text. `^` and a trailing `$` stay anchors; everything else is literal.

    `layout.parse` runs every pattern with `re.search` on every line, in-process, where `re` has no clock: the review's
    `^(\\s*\\S*)*Z$` kept a preview past 30 s. A plain-text grammar (escaped characters plus optional anchors) cannot
    backtrack, so the client and the teaching path can't hand the parser a pathological expression at all."""
    body = value
    start = body.startswith("^")
    if start:
        body = body[1:]
    end = False
    if body.endswith("$"):
        backslashes = 0
        while backslashes < len(body) - 1 and body[-2 - backslashes] == "\\":
            backslashes += 1
        if backslashes % 2 == 0:
            end = True
            body = body[:-1]
    chars: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\":
            if i + 1 >= len(body):
                raise ValueError(f"{what} isn't a valid pattern: a trailing backslash")
            nxt = body[i + 1]
            if nxt.isalnum():
                # `\d`, `\w`, `\s` are a regex class, not escaped plain text; reading them as the literal letter (the
                # old behaviour) silently changed the pattern the client or the model asked for. A digit after the
                # backslash (`\1`) is a backreference to the caller, not an escape either.
                raise ValueError(f"{what} isn't a valid pattern: only plain text (escape \\ . * + ? etc.) is accepted")
            chars.append(nxt)
            i += 2
            continue
        if char in _PLAIN_META:
            raise ValueError(f"{what} isn't a valid pattern: only plain text (escape \\ . * + ? etc.) is accepted")
        chars.append(char)
        i += 1
    return ("^" if start else "") + re.escape("".join(chars)) + ("$" if end else "")


def _pattern(value, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    if len(value) > MAX_PATTERN:
        raise ValueError(f"{what} is too long")
    pattern = _plain_pattern(value, what)
    try:
        re.compile(pattern)
    except re.error:
        raise ValueError(f"{what} isn't a valid pattern") from None
    return pattern


def clean_config(cfg) -> dict:
    """Validate a client or model template config. Raises ValueError with a short reason on any invalid shape."""
    if not isinstance(cfg, dict):
        raise ValueError("a template must be an object")
    try:
        payload = json.dumps(cfg, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValueError("the template isn't plain JSON") from None
    if len(payload.encode()) > MAX_CONFIG_BYTES:
        raise ValueError("the template is too big")
    unknown = set(cfg) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown template setting {sorted(unknown)[0]!r}")
    if contains_data(cfg):
        raise ValueError("a template can't carry an amount or a date")

    out: dict = {}
    words = cfg.get("header_words", [])
    if not isinstance(words, list) or len(words) > 8:
        raise ValueError("header_words must be a list of at most 8 words")
    clean_words = []
    for word in words:
        if not isinstance(word, str) or not word or len(word) > 40 or re.search(r"\d", word):
            raise ValueError("each header word must be short text without digits")
        if contains_data(word):
            raise ValueError("a template can't carry an amount or a date")
        clean_words.append(word)
    out["header_words"] = clean_words

    columns = cfg.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError("a template needs columns")
    clean_cols = []
    roles = []
    for col in columns:
        if not isinstance(col, dict) or set(col) != {"role", "x0", "x1"}:
            raise ValueError("each column is {role, x0, x1}")
        role = col["role"]
        if role not in ROLES:
            raise ValueError(f"unknown column role {role!r}")
        x0, x1 = _number(col["x0"], "x0"), _number(col["x1"], "x1")
        if x0 >= x1:
            raise ValueError("a column needs x0 < x1")
        roles.append(role)
        clean_cols.append({"role": role, "x0": x0, "x1": x1})
    if "date" not in roles:
        raise ValueError("a template needs a date column")
    if not any(r in ("amount", "debit", "credit") for r in roles):
        raise ValueError("a template needs an amount, debit or credit column")
    if roles.count("balance") > 1:
        raise ValueError("a template can have at most one balance column")
    out["columns"] = clean_cols

    if "date_order" in cfg:
        if cfg["date_order"] not in ("MDY", "DMY"):
            raise ValueError("date_order must be MDY or DMY")
        out["date_order"] = cfg["date_order"]
    if "amount_sign" in cfg:
        if cfg["amount_sign"] not in SIGNS:
            raise ValueError("amount_sign is unknown")
        out["amount_sign"] = cfg["amount_sign"]
    if "row_start" in cfg:
        if cfg["row_start"] not in ("date", "date_or_amount"):
            raise ValueError("row_start must be date or date_or_amount")
        out["row_start"] = cfg["row_start"]
    if "x_min" in cfg:
        out["x_min"] = _number(cfg["x_min"], "x_min")
    if "table_end_y" in cfg:
        out["table_end_y"] = _number(cfg["table_end_y"], "table_end_y")
    for key in ("year_placeholder", "period_from_rows"):
        if key in cfg:
            if not isinstance(cfg[key], bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = cfg[key]
    for key in ("skip_pattern", "opening_pattern", "closing_pattern", "table_end_pattern", "table_start_pattern"):
        if key in cfg:
            out[key] = _pattern(cfg[key], key)
    return out


def fingerprint(pages: list[list[dict]], sizes: list[tuple[float, float]], producer: str = "") -> dict:
    """The masked layout fingerprint: generic roles/words, page size, masked producer and top-line shapes."""
    lines = build_lines(pages)
    header = next((ln for ln in lines if layout.header_columns(ln) is not None), None)
    columns = layout.header_columns(header) if header is not None else None
    roles = [c.role for c in columns] if columns else []
    hdr_words: list[str] = []
    if header is not None:
        for w in header.words:
            text = w.text.lower()
            if text in KEEP_WORDS and not re.search(r"\d", text) and text not in hdr_words:
                hdr_words.append(text)
    w, h = sizes[0] if sizes else (0, 0)
    top = [mask_text(ln.text)[:60] for ln in lines if ln.page == 0][:3]
    return {"header_roles": roles, "header_words": hdr_words, "page_w": round(w), "page_h": round(h),
            "producer": mask_text(producer)[:40], "top_shape": top}


def layout_id(fp: dict) -> str:
    key = json.dumps(fp, sort_keys=True, ensure_ascii=False)
    return "l" + hashlib.sha1(key.encode()).hexdigest()[:20]


def _shape_close(a: list[str], b: list[str]) -> bool:
    """At least two of the first three masked lines identical once x/X/9 runs are collapsed."""
    norm = [re.sub(r"[xX9]+", "x", s) for s in a], [re.sub(r"[xX9]+", "x", s) for s in b]
    return sum(1 for p, q in zip(*norm) if p == q) >= min(2, len(a), len(b))


def same_layout(a: dict, b: dict) -> bool:
    return a.get("header_roles") == b.get("header_roles") and a.get("header_words") == b.get("header_words") \
        and abs(a.get("page_w", 0) - b.get("page_w", 0)) <= 2 \
        and abs(a.get("page_h", 0) - b.get("page_h", 0)) <= 2 \
        and _shape_close(a.get("top_shape") or [], b.get("top_shape") or [])


def _usable(raw) -> dict | None:
    """A stored config, revalidated: a row taught by an older version can hold a regex the client grammar no longer
    accepts (a backtracking `^(\\s*\\S*)*Z$` among them). Such a row is ignored, so it never runs on a later statement."""
    try:
        return clean_config(json.loads(raw))
    except (TypeError, ValueError, KeyError):
        return None


def find(conn: sqlite3.Connection, fp: dict) -> dict | None:
    """The saved template whose fingerprint matches this statement's layout (None when unknown)."""
    for row in conn.execute("SELECT id, layout_id, fingerprint, config, source FROM pdf_templates ORDER BY created_at"):
        try:
            stored = json.loads(row["fingerprint"])
        except (TypeError, ValueError):
            continue
        cfg = _usable(row["config"])
        if cfg is None:
            continue
        if same_layout(stored, fp):
            return {"id": row["id"], "layout_id": row["layout_id"], "source": row["source"], "config": cfg,
                    "fingerprint": stored}
    return None


def load(conn: sqlite3.Connection, template_id: str) -> dict | None:
    row = conn.execute("SELECT id, layout_id, fingerprint, config, source FROM pdf_templates WHERE id = ?",
                       (template_id,)).fetchone()
    if row is None:
        return None
    cfg = _usable(row["config"])
    if cfg is None:
        return None
    return {"id": row["id"], "layout_id": row["layout_id"], "source": row["source"], "config": cfg,
            "fingerprint": json.loads(row["fingerprint"])}


def template_id(lid: str, canonical: str) -> str:
    """sha1 of the layout id and the canonical config: the same mapping taught for two layouts is two ids."""
    return hashlib.sha1(f"{lid}\n{canonical}".encode()).hexdigest()


def save(conn: sqlite3.Connection, fp: dict, config: dict, source: str) -> dict:
    """Validate and store a template: one row per layout. A statement whose layout matches a saved template (find:
    next month's statement has the same layout under another exact fingerprint) that already holds this config adds
    nothing and returns that template. Otherwise the matched row (if any) and any row with this exact layout_id are
    replaced by one new row. Writes only inside the caller's transaction."""
    if source not in ("user", "ai"):
        raise ValueError("source must be user or ai")
    cfg = clean_config(config)
    found = find(conn, fp)
    if found is not None and found["config"] == cfg:
        return {"id": found["id"], "layout_id": found["layout_id"], "source": found["source"], "config": cfg}
    canonical = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    lid = layout_id(fp)
    tid = template_id(lid, canonical)
    payload = json.dumps(fp, sort_keys=True, ensure_ascii=False)
    conn.execute("DELETE FROM pdf_templates WHERE layout_id = ? OR id IN (?, ?)",
                 (lid, tid, found["id"] if found is not None else tid))
    conn.execute("INSERT INTO pdf_templates (id, layout_id, fingerprint, config, source, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (tid, lid, payload, canonical, source, now()))
    return {"id": tid, "layout_id": lid, "source": source, "config": cfg}
