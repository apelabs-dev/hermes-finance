"""Reconcile-or-refuse: the rows must replay to the printed balances, or nothing is imported.

`replay()` never repairs: no sign flip, no decimal shift, no dropped row. Statements printed newest first replay
bottom-up; indices in the result are still printed-order indices.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Replay:
    accepted: bool
    reason: str | None            # a public reason code, or None when accepted
    first_bad_row_index: int | None
    balance_mode: str | None      # "running" | "totals"

    @property
    def status(self) -> str:
        return "accepted" if self.accepted else "refused"


def _refuse(reason: str, first_bad: int | None = None, mode: str | None = None) -> Replay:
    return Replay(False, reason, first_bad, mode)


def replay(rows, opening: Decimal | None, closing: Decimal | None, totals: dict | None = None,
           carry=()) -> Replay:
    """rows: objects with .amount (statement sign, + = money in), .balance (Decimal | None), .date.

    `totals`: {"debit": Decimal, "credit": Decimal} printed totals, checked when present.
    `carry`: (printed-order index, printed balance) page carry lines, checked at that point.
    """
    if not rows:
        return _refuse("no_rows")
    for i, r in enumerate(rows):
        if r.amount is None:
            return _refuse("does_not_reconcile", i)
        if getattr(r, "date", None) is None:
            return _refuse("unreadable_date", i)
    has_bal = any(r.balance is not None for r in rows)
    dates = [r.date for r in rows]
    reverse = dates[0] > dates[-1] and all(a >= b for a, b in zip(dates, dates[1:]))
    if reverse:
        flipped = [(len(rows) - 2 - i, val) for i, val in carry]
        v = replay(list(reversed(rows)), opening, closing, totals, flipped)
        if v.first_bad_row_index is not None:
            v.first_bad_row_index = len(rows) - 1 - v.first_bad_row_index
        return v
    if has_bal:
        if opening is None:
            return _refuse("no_balances", 0, "running")
        prev = opening
        last_checked = -1
        for i, r in enumerate(rows):
            prev = prev + r.amount
            if r.balance is not None:
                if prev != r.balance:
                    return _refuse("does_not_reconcile", i, "running")
                last_checked = i
        if closing is not None:
            if prev != closing:
                return _refuse("does_not_reconcile", len(rows) - 1, "running")
        elif last_checked != len(rows) - 1:
            return _refuse("does_not_reconcile", last_checked + 1, "running")
        mode = "running"
    else:
        if opening is None or closing is None:
            return _refuse("no_balances", None, "totals")
        if opening + sum(r.amount for r in rows) != closing:
            return _refuse("does_not_reconcile", len(rows) - 1, "totals")
        mode = "totals"
    run, pos = opening, {}
    for i, r in enumerate(rows):
        run += r.amount
        pos[i] = run
    for idx, val in carry:
        if idx in pos and pos[idx] != val:
            return _refuse("does_not_reconcile", idx + 1, mode)
    if totals:
        debit, credit = totals.get("debit"), totals.get("credit")
        if debit is not None and sum(-r.amount for r in rows if r.amount < 0) != debit:
            return _refuse("does_not_reconcile", None, mode)
        if credit is not None and sum(r.amount for r in rows if r.amount > 0) != credit:
            return _refuse("does_not_reconcile", None, mode)
    return Replay(True, None, None, mode)
