"""Spike 3's judge: the merged spike-2 reconcile-or-refuse check as a standalone gate.

Arm D (Docling) and arm A (a text layer through a model) both produce a Candidate; judge()
either accepts its rows or raises the same PdfSpikeError the merged seam raises. No row is
returned that this function has not accepted.

The rules are lifted from `pdfspike_layout._parse_lines` (spike 2's merged seam), not
re-derived:

* tolerance 0.01;
* total check `|opening - sum(amounts) - closing| <= 0.01`, when opening and closing are both
  printed (a missing closing is derived from the last printed running balance, exactly as the
  seam derives it);
* running-balance check `balance[i-1] - amount[i] == balance[i]` when a printed balance is on
  every row or on at least 80% of them (`>= max(2, int(len(rows) * 0.8))`);
* `NoBalances` when no opening balance is printed, or when there is neither a total pair nor a
  running balance;
* the sign-flip fallback only when the profile defines `positive` and the flipped reading
  satisfies every available check;
* `ReconcileMismatch` otherwise;
* `MultiAccountUnsupported` when the extractor saw more than one account or balance stream.

A Candidate is already assembled: rows are `importfile.Row` objects in the app's sign (money
out > 0, ISO dates), balances are the per-row printed balances in row order (or all None), and
`opening` / `closing` are the printed (or seam-style derived) balances. `truncated` on a
candidate's source does not belong here: the judge checks what it is given.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from hermes_finance.importfile import Row, _cents
from pdfspike_layout import (
    PROFILES,
    MultiAccountUnsupported,
    NoBalances,
    NoRows,
    ReconcileMismatch,
    _MISMATCH,
    _NO_BALANCES,
    _NO_OPENING,
)

__all__ = ["Candidate", "judge"]

# The merged seam keeps these two messages inline in _parse_lines; copied verbatim here and
# pinned by the parity test.
_MULTI_ACCOUNT = ("This PDF holds more than one account or balance stream. This import supports one account per "
                  "file; download each account separately.")
_NO_ROWS = ("This PDF has text but no transaction rows in it. Download the transactions as CSV or OFX instead.")


@dataclass(frozen=True)
class Candidate:
    """One extractor's reading of a statement, before the judge accepts or refuses it."""

    rows: list[Row]                  # importfile.Row, app sign (> 0 money out), ISO dates
    opening: Decimal | None
    closing: Decimal | None
    balances: list[Decimal | None]   # per-row printed balance, or all None
    multi_account: bool = False


def _same(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= Decimal("0.01")


def judge(candidate: Candidate, profile: str) -> list[Row]:
    """Accept the candidate's rows or raise. Reuses layout's exception classes and messages."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose one of {', '.join(sorted(PROFILES))}")
    if candidate.multi_account:
        raise MultiAccountUnsupported(_MULTI_ACCOUNT)
    rows = list(candidate.rows)
    if not rows:
        raise NoRows(_NO_ROWS)
    balances = list(candidate.balances)
    if not balances:
        balances = [None] * len(rows)
    elif len(balances) != len(rows):
        raise ValueError("balances must have one entry per row, or be empty")

    opening = candidate.opening
    closing = candidate.closing
    if closing is None:
        for value in reversed(balances):
            if value is not None:
                closing = value
                break
    if opening is None:
        raise NoBalances(_NO_OPENING)

    tolerance = Decimal("0.01")
    balanced = sum(1 for value in balances if value is not None)
    running_available = bool(balanced) and (balanced == len(rows) or balanced >= max(2, int(len(rows) * 0.8)))
    total_available = opening is not None and closing is not None
    if not running_available and not total_available:
        raise NoBalances(_NO_BALANCES)

    def reconciles(values: list[Decimal]) -> bool:
        if total_available and abs(opening - sum(values) - closing) > tolerance:
            return False
        if running_available:
            prior = opening
            checked = 0
            for amount, balance in zip(values, balances):
                if balance is None:
                    continue
                if prior is not None:
                    if abs(prior - amount - balance) > tolerance:
                        return False
                    checked += 1
                prior = balance
            if checked == 0:
                return False
        return True

    amounts = [Decimal(str(row.amount)) for row in rows]
    if not reconciles(amounts):
        flipped = [-amount for amount in amounts]
        if PROFILES[profile].positive is not None and reconciles(flipped):
            rows = [Row(row.date, row.description, _cents(-Decimal(str(row.amount))), row.account, row.fitid,
                        row.line, row.trntype) for row in rows]
        else:
            raise ReconcileMismatch(_MISMATCH)
    return rows
