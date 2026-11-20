"""Multi-currency v22 (t_3f83e261): schema migration, currency codes, and money formatting.

No FX anywhere: the migration fills only a missing code (from the account, else the display currency),
keeps every known code and every amount, and the display currency only changes how a number is written.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from hermes_finance import db, goals, insights, money, prefs

FIX = Path(__file__).parent / "fixtures"
V21_SQL = (FIX / "schema_v21_currency.sql").read_text()   # v21 as the Currency step (v22) found it


def _v21_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(V21_SQL)
    return conn


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _rows(conn) -> dict[str, list[tuple]]:
    return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid')] for t in _tables(conn)}


def test_v21_to_v22_keeps_every_row_and_fills_only_missing_codes(tmp_path):
    path = tmp_path / "finance.db"
    raw = _v21_db(path)
    before = _rows(raw)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 21
    raw.close()

    conn = db.connect(path)
    assert db.SCHEMA_VERSION >= 22   # a later step (v23 and on) upgrades through this one
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    after = _rows(conn)
    # A migration never loses a row, and ids stay put.
    assert {t: len(rows) for t, rows in after.items()} == {t: len(rows) for t, rows in before.items()}
    assert {t: [r[0] for r in after[t]] for t in after} == {t: [r[0] for r in before[t]] for t in before}

    # manual_accounts.currency is the last column, added at the end so an upgraded table matches a fresh one
    assert [r[1] for r in conn.execute("PRAGMA table_info(manual_accounts)")][-1] == "currency"
    assert [r[0] for r in conn.execute("SELECT currency FROM manual_accounts")] == ["USD"]

    codes = dict(conn.execute("SELECT transaction_id, iso_currency FROM transactions"))
    assert codes == {"t_usd_null": "USD",       # inherits the account
                     "t_eur_null": "EUR",       # inherits the account
                     "t_jpy": "JPY",            # a known code is never overwritten
                     "t_custom": "USDF",        # a provider's custom id stays exactly as given
                     "t_import": "USD",         # an imported row inherits its manual account
                     "t_split": "USD"}
    assert dict(conn.execute("SELECT transaction_id, amount FROM transactions"))["t_jpy"] == 1200.0
    assert [r[0] for r in conn.execute("SELECT iso_currency FROM balance_snapshots ORDER BY account_id, date")] == \
        ["EUR", "EUR", "USD", "USD", "USD"]
    # v_spend_lines exposes the code in both branches (plain rows and split lines)
    assert {r[0] for r in conn.execute("SELECT DISTINCT iso_currency FROM v_spend_lines")} == {"USD", "EUR", "JPY", "USDF"}
    assert [tuple(r) for r in conn.execute("SELECT iso_currency FROM v_spend_lines WHERE transaction_id = 't_split'")] == \
        [("USD",), ("USD",)]
    conn.close()


def test_v22_falls_back_to_the_display_currency_and_is_idempotent(tmp_path):
    path = tmp_path / "finance.db"
    raw = _v21_db(path)
    raw.execute("INSERT INTO settings (key, value, updated_at) VALUES ('region.currency', ?, '2026-11-01')",
                (json.dumps("EUR"),))
    # an account with no currency: its null rows fall back to the display currency
    raw.execute("UPDATE accounts SET iso_currency = NULL WHERE account_id = 'acc_usd'")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert dict(conn.execute("SELECT transaction_id, iso_currency FROM transactions"))["t_usd_null"] == "EUR"
    assert conn.execute("SELECT currency FROM manual_accounts").fetchone()[0] == "EUR"
    before = _rows(conn)
    db._v22_currency(conn)
    db._v22_currency(conn)
    assert _rows(conn) == before
    conn.execute("UPDATE transactions SET iso_currency = NULL WHERE transaction_id = 't_usd_null'")
    db._v22_currency(conn)
    assert conn.execute("SELECT iso_currency FROM transactions WHERE transaction_id = 't_usd_null'").fetchone()[0] == "EUR"
    conn.close()


def test_goals_derive_currency_from_their_account_unlinked_use_display(tmp_path):
    path = tmp_path / "finance.db"
    raw = _v21_db(path)
    raw.close()
    conn = db.connect(path)
    conn.execute("INSERT INTO accounts (account_id, item_id, name, type, subtype, current_balance, iso_currency, updated_at) "
                 "VALUES ('acc_krw', 'item_eur', 'Won', 'depository', 'checking', 1200000, 'KRW', '2026-11-01')")
    conn.execute("INSERT INTO goals (kind, name, target, track, account_id, created_at, updated_at) "
                 "VALUES ('save', 'Won goal', 5000000, 'account', 'acc_krw', '2026-11-01', '2026-11-01')")
    conn.execute("INSERT INTO goals (kind, name, target, track, account_id, created_at, updated_at) "
                 "VALUES ('save', 'Free goal', 5000, 'manual', NULL, '2026-11-01', '2026-11-01')")
    p = goals.payload(conn, __import__("datetime").date(2026, 11, 15))
    by = {g["name"]: g for g in p["goals"]}
    assert by["Won goal"]["currency"] == "KRW"
    assert by["Free goal"]["currency"] == "USD"
    assert "currency" not in {r[1] for r in conn.execute("PRAGMA table_info(goals)")}
    conn.close()


def test_money_uses_official_exponents_and_never_converts():
    assert money.money(1234) == "$1,234"
    assert money.money(1234.5) == "$1,234.50"
    assert money.money(1.005) == "$1.01"                       # half up on the magnitude
    assert money.money(-1.005) == "-$1.01"
    assert money.money(-23.8, currency="EUR") == "-€23.80"
    assert money.money(1234, currency="JPY") == "JPY 1,234"    # 0 decimals
    assert money.money(1234.4, currency="KWD") == "KWD 1,234.400"  # 3 decimals
    assert money.money(1200, currency="KRW") == "KRW 1,200"    # no symbol: the code is shown
    assert money.money(23.8, currency="EUR") == "€23.80"
    assert money.money(None, currency="EUR") == "-"
    assert money.money(-0.004) == "$0"
    assert money.money2(2, currency="JPY") == "JPY 2"
    assert money.money2(2, currency="KWD") == "KWD 2.000"
    assert money.exponent("JPY") == 0 and money.exponent("USD") == 2 and money.exponent("KWD") == 3
    assert money.is_iso("usd") and not money.is_iso("USDF") and not money.is_iso("")
    # a display-currency change is only a rewrite: the number is untouched
    assert money.money(1234.56, currency="USD") == "$1,234.56"
    assert money.money(1234.56, currency="EUR") == "€1,234.56"


def test_display_currency_comes_from_region(conn):
    assert db.display_currency(conn) == "USD"
    prefs.save(conn, {"region.currency": "EUR"})
    assert db.display_currency(conn) == "EUR"
    assert insights._spend(conn, "2026-11") == 0.0


def test_no_agent_facing_prompt_still_mandates_dollars():
    """The card's rule 6: no prompt or tool note may tell the model to write dollars. The plugin's usage skill and its
    weekly cron prompt are read at runtime (`register_skill`, `crons/roles.py`), so they carry the same rule as a tool
    description: a foreign amount written as "$" is a wrong number, not a formatting nit."""
    from hermes_finance import tooldefs
    plugin = Path(__file__).resolve().parents[1] / "hermes_plugin"
    prompts = ((plugin / "skills" / "usage" / "SKILL.md").read_text()
               + (plugin / "crons" / "prompts" / "weekly.md").read_text()
               + (plugin / "crons" / "prompts" / "daily.md").read_text()
               + tooldefs.instructions()).lower()
    assert "dollar symbol" not in prompts and "dollar amounts" not in prompts
    assert "in dollars" not in prompts and "use $" not in prompts
    assert "currency" in prompts          # the replacement rule is there, not just the old one deleted


def test_the_next_schema_step_merges_without_renumbering_this_one():
    """The Currency fixture is named for its card, so the next card's own v21 fixture (Categories, v23) can't collide
    with it, and no other test pins the version at 22: a later bump only touches its own files."""
    tests = Path(__file__).resolve().parent
    assert (FIX / "schema_v21_currency.sql").is_file() and not (FIX / "schema_v21.sql").exists()
    pin = re.compile(r"SCHEMA_VERSION\s*==\s*22\b|user_version[^\n]*==\s*22\b")
    pinned = [f"{p.name}:{n}" for p in sorted(tests.glob("test_*.py")) if p.name != Path(__file__).name
              for n, line in enumerate(p.read_text().splitlines(), 1) if pin.search(line)]
    assert pinned == []
