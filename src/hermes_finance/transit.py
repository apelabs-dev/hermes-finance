"""Payment in transit (TASK-22): a card payment that has left a bank account but that the card hasn't posted yet.

For a day or two after a card payment, the money is gone from checking while the card still shows the whole balance owed,
so net worth dips by the payment for no reason (the case that prompted this: a card payment left a checking account on day
D; the card still showed it owed on D+1). Each entry here is counted as reducing that card's balance, by
insights.net_worth, until the card posts the payment or DAYS pass. Nothing stored changes, and every view that counts one
shows its line.

An entry is a posted outflow on a connected depository account that
  * is a card payment: Plaid's credit card payment category, or the app's own rule (manual.CARD_PAYMENT_SQL);
  * is at most DAYS days old;
  * isn't a transfer to another of your own bank accounts (a same-amount inflow on one within DAYS days);
  * names exactly one connected credit card: its bank (or the issuer's other names, manual.ISSUER_ALIASES) is a word in
    the transaction's name and, when that bank has several connected cards, the card's last 4 digits too. Otherwise
    there's no entry: the card is never guessed;
  * pairs with no payment on a connected card yet (manual.card_pair), and the named card has no inflow of the same amount
    (±$0.01) within DAYS days of it (one inflow answers one payment);
  * and the card still owes at least that much, so a card whose balance already took the payment gets no entry.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import manual
from .money import money

DAYS = 5            # an entry lasts this many days after the payment left (and a card's inflow this close answers it)
CENTS = 0.01        # a card's inflow within a cent of the payment answers it


def _word(text: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(text)}(?![a-z0-9])")


def _hints(institution: str | None) -> list[re.Pattern]:
    """The bank's name and, for a card issuer, the other names its payments go by (manual.ISSUER_ALIASES)."""
    name = " ".join((institution or "").casefold().split())
    if not name:
        return []
    hints = {name}
    for key, aliases in manual.ISSUER_ALIASES.items():
        if key in name:
            hints |= {key, *aliases}
    return [_word(h) for h in sorted(hints)]


def _card(text: str, cards: list[sqlite3.Row]) -> sqlite3.Row | None:
    """The one connected card a payment's text names, else None."""
    named = [c for c in cards if any(h.search(text) for h in _hints(c["institution_name"]))]
    if len(named) > 1:
        named = [c for c in named if c["mask"] and re.search(rf"(?<!\d){re.escape(c['mask'])}(?!\d)", text)]
    return named[0] if len(named) == 1 else None


def _label(card: sqlite3.Row) -> str:
    bank = card["institution_name"] or card["name"] or "card"
    return f"{bank} ••{card['mask']}" if card["mask"] else f"{bank} {card['name'] or ''}".strip()


def entries(conn: sqlite3.Connection, today: date) -> list[dict[str, Any]]:
    """Today's payments in transit, oldest first: {transaction_id, date, amount, from_account, card_id, card, mask,
    institution, label, line}."""
    cards = conn.execute(
        """SELECT a.account_id, a.name, a.mask, a.current_balance, i.institution_name FROM accounts a
           JOIN items i ON i.item_id = a.item_id WHERE i.removed_at IS NULL AND a.type = 'credit'
           ORDER BY i.created_at, a.account_id""").fetchall()
    if not cards:
        return []
    since = (today - timedelta(days=DAYS)).isoformat()
    rows = conn.execute(
        f"""SELECT t.transaction_id, t.account_id, t.date, t.name, t.merchant_name, t.amount, a.name AS account_name,
                   a.mask AS account_mask
            FROM transactions t JOIN accounts a ON a.account_id = t.account_id JOIN items i ON i.item_id = t.item_id
            WHERE t.removed = 0 AND t.pending = 0 AND t.amount > 0 AND a.type = 'depository' AND i.removed_at IS NULL
              AND t.date >= ? AND t.date <= ? AND {manual.CARD_PAYMENT_SQL}
            ORDER BY t.date, t.transaction_id""", (since, today.isoformat())).fetchall()
    owed = {c["account_id"]: c["current_balance"] or 0.0 for c in cards}
    answered: set[str] = set()
    out = []
    for t in rows:
        card = _card(f"{t['name'] or ''} {t['merchant_name'] or ''}".casefold(), cards)
        if card is None:
            continue
        # a transfer to another of your own bank accounts, not a card payment
        if conn.execute(
                """SELECT 1 FROM transactions u JOIN accounts a ON a.account_id = u.account_id
                   JOIN items i ON i.item_id = u.item_id
                   WHERE u.removed = 0 AND i.removed_at IS NULL AND a.type = 'depository' AND u.account_id <> ?
                     AND ABS(-u.amount - ?) <= ? AND ABS(julianday(u.date) - julianday(?)) <= ?""",
                (t["account_id"], t["amount"], CENTS + 1e-9, t["date"], DAYS)).fetchone():
            continue
        if manual.card_pair(conn, t["transaction_id"]):
            continue
        inflow = next((r[0] for r in conn.execute(
            """SELECT transaction_id FROM transactions WHERE account_id = ? AND removed = 0 AND amount < 0
                 AND ABS(-amount - ?) <= ? AND ABS(julianday(date) - julianday(?)) <= ?
               ORDER BY ABS(julianday(date) - julianday(?)), date, transaction_id""",
            (card["account_id"], t["amount"], CENTS + 1e-9, t["date"], DAYS, t["date"]))
            if r[0] not in answered), None)
        if inflow is not None:          # the card posted it
            answered.add(inflow)
            continue
        if owed[card["account_id"]] < t["amount"] - CENTS:
            continue
        owed[card["account_id"]] -= t["amount"]
        label = _label(card)
        sent = date.fromisoformat(t["date"])
        out.append({"transaction_id": t["transaction_id"], "date": t["date"], "amount": round(t["amount"], 2),
                    "from_account": {"name": t["account_name"], "mask": t["account_mask"]},
                    "card_id": card["account_id"], "card": card["name"], "mask": card["mask"],
                    "institution": card["institution_name"], "label": label,
                    "line": f"Payment in transit: {money(t['amount'])} to {label} (sent {sent:%b} {sent.day})"})
    return out
