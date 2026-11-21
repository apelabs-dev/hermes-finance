"""Multi-currency text and SQL (t_3f83e261, review round 2): the transactions text writes each row in its own
currency, goals fall back to the display currency (never a literal USD), and the display code reaches SQL text only
through the one guarded helper."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from hermes_finance import goals, insights, prefs, reads

TODAY = date(2026, 11, 15)
TS = "2026-11-01T00:00:00Z"
SRC = Path(__file__).resolve().parents[1] / "src" / "hermes_finance"


def _seed(conn, currency_of_main="USD"):
    with conn:
        conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                     "VALUES ('item_a', 'ins_1', 'Bank', 'tok', 'active', ?)", (TS,))
        for aid, name, cur, bal in (("acc_main", "Main checking", currency_of_main, 1000),
                                    ("acc_krw", "Won checking", "KRW", 500000)):
            conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                         "available_balance, iso_currency, updated_at) VALUES (?, 'item_a', ?, ?, 'depository', "
                         "'savings', ?, ?, ?, ?)", (aid, name, aid[-4:], bal, bal, cur, TS))
        for tid, aid, amount, cur, name in (("t_usd", "acc_main", 100.0, "USD", "Market"),
                                            ("t_krw", "acc_krw", 12000.0, "KRW", "Seoul market")):
            conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, "
                         "amount, iso_currency, pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at) "
                         "VALUES (?, ?, 'item_a', '2026-11-02', ?, ?, ?, ?, 0, 'FOOD_AND_DRINK', "
                         "'FOOD_AND_DRINK_GROCERIES', 0, ?, ?)", (tid, aid, name, name, amount, cur, TS, TS))


def test_transactions_text_writes_a_won_row_in_won(conn):
    _seed(conn)
    p = reads.txns(conn, reads.Filters(reads.parse_period(TODAY, default="all")), TODAY)
    text = reads.txns_text(p)
    krw = next(line for line in text.splitlines() if "Seoul market" in line)
    usd = next(line for line in text.splitlines() if "Market" in line and "Seoul" not in line)
    assert "KRW 12,000" in krw and "$" not in krw
    assert "$12,000" not in text
    assert "$100" in usd                # a display-currency row is written as it always was


def test_goals_fall_back_to_the_display_currency_not_usd(conn, monkeypatch):
    prefs.save(conn, {"region.currency": "EUR"})
    _seed(conn, currency_of_main=None)       # no code of its own: it is in the display currency
    real = goals.accounts
    monkeypatch.setattr(goals, "accounts", lambda c: [{k: v for k, v in a.items() if k != "currency"} for a in real(c)])
    p = goals.payload(conn, TODAY)
    assert p["accounts"] and {a["currency"] for a in p["accounts"]} == {"EUR"}
    assert p["suggestions"] and {s["currency"] for s in p["suggestions"]} == {"EUR"}
    text = goals.text(p)
    line = next(x for x in text.splitlines() if x.startswith("Suggested"))
    assert "€1,000" in line and "$" not in line
    # a payload amount that carries no code is written in the payload's display currency
    assert "€50" in goals.text({"currency": "EUR", "goals": [], "suggestions": [{"label": "Pot", "balance": 50}],
                                "totals_by_currency": []})


def test_the_display_code_reaches_sql_only_through_the_guard(conn):
    assert insights.sql_code("EUR") == "EUR" and insights.sql_code("USDF") == "USDF"
    for bad in ("USD'; --", "eur", "", "E UR", None):
        assert insights.sql_code(bad) == "USD"
        assert "--" not in insights.currency_expr(bad, "v.iso_currency")
        assert "'; " not in insights.currency_is_sql(bad, "a.iso_currency", "v.iso_currency")
    rows = [("EUR",), ("eur",), ("",), (None,), ("USD",)]
    conn.execute("CREATE TEMP TABLE c (iso_currency TEXT)")
    conn.executemany("INSERT INTO c VALUES (?)", rows)
    def pick(sql):
        return [r[0] for r in conn.execute(f"SELECT iso_currency FROM c WHERE {sql} ORDER BY rowid")]
    assert pick(insights.currency_is_sql("EUR", "iso_currency")) == ["EUR", "eur", "", None]
    assert pick(insights.currency_is_sql("EUR", "iso_currency", other=True)) == ["USD"]
    assert pick(insights.currency_is_sql("USD'; --", "iso_currency")) == ["", None, "USD"]   # read as USD
    # no f-string inlines the display code into SQL any more: every site goes through currency_expr / currency_is_sql
    raw = re.compile(r"'\{(display|code|cur|ccy)\}'")
    hits = [f"{p.name}:{n}" for p in sorted(SRC.glob("*.py"))
            for n, line in enumerate(p.read_text().splitlines(), 1) if raw.search(line)]
    assert hits == []
