"""Status snapshot + plain-text table rendering (shared by the CLI and the web page)."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from . import config, insights
from .money import money
from .db import blocked_duplicates, slots_used


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    policy = set(insights.excluded_accounts(conn))
    items = []
    for it in conn.execute("SELECT * FROM v_items ORDER BY created_at"):
        item = dict(it)
        item["products"] = json.loads(item["products"]) if item["products"] else []
        item["accounts"] = [dict(a) for a in conn.execute(
            """SELECT account_id, name, official_name, mask, type, subtype, current_balance,
                      available_balance, credit_limit, iso_currency, updated_at
               FROM accounts WHERE item_id=? ORDER BY type, name""", (it["item_id"],))]
        item["excluded_account_count"] = 0
        for a in item["accounts"]:
            a["excluded"] = a["account_id"] in policy
            item["excluded_account_count"] += 1 if a["excluded"] else 0
        total = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE item_id=? AND removed=0", (it["item_id"],)).fetchone()[0]
        item["transaction_count"] = conn.execute(
            "SELECT COUNT(*) FROM v_transactions WHERE item_id=?", (it["item_id"],)).fetchone()[0]
        item["excluded_transaction_count"] = total - item["transaction_count"]
        items.append(item)
    pol = insights.account_policy_status(conn)
    return {
        "slots_used": slots_used(conn),
        "slot_limit": config.SLOT_LIMIT,
        "blocked_duplicate_links": blocked_duplicates(conn),
        "items": items,
        "account_policy": {
            "configured_count": pol["configured_count"],
            "resolved_count": pol["resolved_count"],
            "unresolved_count": pol["unresolved_count"],
            "coverage": pol["coverage"],
            "accounts": [{"item": a["institution"], "name": a["name"], "mask": a["mask"]}
                         for a in pol["accounts"]],
        },
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
    pol = s.get("account_policy") or {}
    if pol.get("configured_count") or pol.get("coverage") == "invalid":
        if pol.get("coverage") == "invalid":
            lines.append(f"Account policy: the saved setting is invalid; {pol.get('configured_count', 0)} excluded, "
                         f"{pol.get('unresolved_count', 0)} unresolved (invalid)")
        else:
            lines.append(f"Account policy: {pol['configured_count']} excluded, {pol['unresolved_count']} unresolved "
                         f"({pol['coverage']})")
        for a in pol["accounts"]:
            lines.append(f"  left out: {a['item'] or '?'} · {account_name(a['name'], a['mask']) or '?'} "
                         f"••{a['mask'] or '----'}")
    if not s["items"]:
        lines.append("No Items connected yet. Run `finance serve` and click 'Connect a bank'.")
    for it in s["items"]:
        state = "removed" if it["removed_at"] else it["status"]
        err = f"  error={it['error_code']}" if it["error_code"] else ""
        lines.append("")
        lines.append(f"[{state}] {it['institution_name'] or it['institution_id']}  item={it['item_id']}{err}")
        lines.append(f"  last sync: {it['last_sync_at'] or 'never'}  transactions: {it['transaction_count']}"
                     + (f" ({it['excluded_transaction_count']} hidden by the account policy)"
                        if it.get("excluded_transaction_count") else "")
                     + f"  products: {','.join(it['products']) or '-'}"
                     + (f"  plaid: {it['txn_update_status']}" if it["txn_update_status"] else ""))
        for a in it["accounts"]:
            lines.append(
                f"    {account_name(a['name'], a['mask']) or '?'} ••{a['mask'] or '----'}  {a['type']}/{a['subtype']}  "
                f"current {money(a['current_balance'])}  available {money(a['available_balance'])}"
                + (f"  limit {money(a['credit_limit'])}" if a["credit_limit"] is not None else "")
                + ("  left out by the account policy" if a.get("excluded") else "")
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
