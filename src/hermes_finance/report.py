"""Status snapshot + plain-text table rendering (shared by the CLI and the web page)."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from . import config
from .money import money
from .db import blocked_duplicates, slots_used


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    items = []
    for it in conn.execute("SELECT * FROM v_items ORDER BY created_at"):
        item = dict(it)
        item["products"] = json.loads(item["products"]) if item["products"] else []
        item["accounts"] = [dict(a) for a in conn.execute(
            """SELECT account_id, name, official_name, mask, type, subtype, current_balance,
                      available_balance, credit_limit, iso_currency, updated_at
               FROM accounts WHERE item_id=? ORDER BY type, name""", (it["item_id"],))]
        item["transaction_count"] = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE item_id=? AND removed=0", (it["item_id"],)).fetchone()[0]
        items.append(item)
    return {
        "slots_used": slots_used(conn),
        "slot_limit": config.SLOT_LIMIT,
        "blocked_duplicate_links": blocked_duplicates(conn),
        "items": items,
        "data_age": data_age(conn),   # TASK-22: so an agent can decide to sync first
    }


def data_age(conn: sqlite3.Connection, at: datetime | None = None) -> str:
    """How old the bank data is: "synced 14 h ago" (the newest sync of a connected bank), or "never synced"."""
    last = conn.execute("SELECT MAX(last_sync_at) FROM items WHERE removed_at IS NULL").fetchone()[0]
    try:
        then = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "never synced"
    m = max(0, round(((at or datetime.now(timezone.utc)) - then).total_seconds() / 60))
    return ("synced just now" if m < 1 else f"synced {m} min ago" if m < 60
            else f"synced {round(m / 60)} h ago" if m < 1440 else f"synced {round(m / 1440)} d ago")


def account_name(name: str | None, mask: str | None = None) -> str | None:
    """An account's name as the CLI shows it (TASK-22b), cleaned as the pages' acctName does: Plaid sometimes sends junk
    ("WELLS FARGO ACTIVE CASH VISA\ufffd\ufffd CARD ...4321"). Replacement characters (U+FFFD, U+FFFC) and control /
    format characters go, spaces collapse, and a trailing "...4321" that repeats the mask goes too (the label adds its
    own ••4321). None when nothing is left."""
    text = "".join(" " if c.isspace() else "" if c in "\ufffd\ufffc" or unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co")
                   else c for c in name or "")
    text = " ".join(text.split())
    if mask:
        text = re.sub(rf"\s*(?:\.{{2,}}|\u2026|\u2022+|\*+)\s*{re.escape(mask)}$", "", text).strip()
    return text or None


def status_text(s: dict[str, Any]) -> str:
    lines = [f"Slots used: {s['slots_used']} / {s['slot_limit']}  (Plaid Trial: removed Items still count)"]
    if s["blocked_duplicate_links"]:
        n = s["blocked_duplicate_links"]
        lines.append(f"  note: {n} duplicate Link completion{'' if n == 1 else 's'} {'was' if n == 1 else 'were'} "
                     f"blocked before exchange; Plaid may have counted {'it' if n == 1 else 'them'}.")
    if not s["items"]:
        lines.append("No Items connected yet. Run `finance serve` and click 'Connect a bank'.")
    for it in s["items"]:
        state = "removed" if it["removed_at"] else it["status"]
        err = f"  error={it['error_code']}" if it["error_code"] else ""
        lines.append("")
        lines.append(f"[{state}] {it['institution_name'] or it['institution_id']}  item={it['item_id']}{err}")
        lines.append(f"  last sync: {it['last_sync_at'] or 'never'}  transactions: {it['transaction_count']}"
                     f"  products: {','.join(it['products']) or '-'}"
                     + (f"  plaid: {it['txn_update_status']}" if it["txn_update_status"] else ""))
        for a in it["accounts"]:
            lines.append(
                f"    {account_name(a['name'], a['mask']) or '?'} ••{a['mask'] or '----'}  {a['type']}/{a['subtype']}  "
                f"current {money(a['current_balance'])}  available {money(a['available_balance'])}"
                + (f"  limit {money(a['credit_limit'])}" if a["credit_limit"] is not None else "")
                + f" {a['iso_currency'] or ''}")
    return "\n".join(lines)


def table(columns: Sequence[str], rows: Iterable[Sequence[Any]], max_width: int = 60) -> str:
    def cell(v: Any) -> str:
        s = "NULL" if v is None else str(v)
        s = s.replace("\n", " ")
        return s if len(s) <= max_width else s[: max_width - 1] + "…"

    body = [[cell(v) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in body]) for i, c in enumerate(columns)]
    fmt = lambda r: " | ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip()
    out = [fmt(columns), "-+-".join("-" * w for w in widths)] + [fmt(r) for r in body]
    out.append(f"({len(body)} row{'s' if len(body) != 1 else ''})")
    return "\n".join(out)
