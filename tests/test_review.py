"""TASK-11b (3): "Needs review" holds what nobody has decided. An 'Other' row someone decided (the AI confirmed or
changed it, or a user / rule filed it there) leaves Review; an undecided 'Other' row, a row with no category at all
(source 'default') and a row the AI flagged stay. The same rule everywhere: the Transactions filter and each row's
`review`, the counts badge, and `finance txns --uncategorized` / `--flagged`."""

from __future__ import annotations

import json
from datetime import date

import pytest

from factory import acct, bank, tx
from hermes_finance import classify, cli, config, db
from hermes_finance.web import App

TODAY = date(2026, 9, 26)
LOCAL = {"Host": "127.0.0.1:8765"}


def review_row(conn, tid: str, verdict: str) -> None:
    with conn:
        conn.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, note) "
                     "VALUES (?, 'ai-test', ?, ?, 'looked at it')", (tid, db.now(), verdict))


@pytest.fixture
def ledger(conn, client, monkeypatch):
    bank(conn)
    acct(conn)
    # 'Other' rows (GENERAL_SERVICES_OTHER maps to Other, source plaid)
    tx(conn, "2026-09-02", 12.0, "Mystery Co", cat="Other", tid="other_new")          # nobody decided: Review
    tx(conn, "2026-09-03", 535.0, "USCIS", cat="Other", tid="other_confirmed")          # the AI confirmed Other
    tx(conn, "2026-09-04", 2.0, "IRS", cat="Other", tid="other_user")                   # the user filed it under Other
    tx(conn, "2026-09-05", 9.5, "USPS", cat="Other", tid="other_rule")                  # a rule files it under Other
    tx(conn, "2026-09-06", 80.0, "Attorney", cat="Other Shopping", tid="other_changed")      # the AI changed it to Other
    tx(conn, "2026-09-07", 40.0, "Court fees", cat="Other", tid="other_flagged")       # the AI flagged it: Review
    tx(conn, "2026-09-08", 30.0, "Amazon", cat="Other Shopping", tid="shop_flagged")         # flagged, not Other: Review
    tx(conn, "2026-09-09", 25.0, "Chipotle", cat="Eating Out", tid="plain")            # a normal row: never
    tx(conn, "2026-09-10", 7.0, "Donation", cat="Other", tid="default_confirmed")      # no category at all (below)
    review_row(conn, "other_confirmed", "confirmed")
    classify.set_category(conn, "other_user", "Other", "user")
    classify.add_rule(conn, "merchant_name", "USPS", "Other", created_from="user")
    classify.apply(conn)
    with conn:  # no Plaid category and no classification: source 'default' (filed under Other by the view)
        conn.execute("UPDATE transactions SET pfc_primary = 'NOPE', pfc_detailed = 'NOPE_X' WHERE transaction_id = "
                     "'default_confirmed'")
        conn.execute("DELETE FROM classifications WHERE transaction_id = 'default_confirmed'")
    classify.set_category(conn, "other_changed", "Other", "ai", 0.9)
    review_row(conn, "other_changed", "changed")
    review_row(conn, "other_flagged", "flagged")
    review_row(conn, "shop_flagged", "flagged")
    review_row(conn, "default_confirmed", "confirmed")      # default-source rows stay as they are
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    app.today = lambda: TODAY
    return app


IN_REVIEW = ["default_confirmed", "shop_flagged", "other_flagged", "other_new"]   # newest first


def get(app, path):
    status, _, body = app.handle("GET", path, LOCAL, b"")
    assert status == 200
    return json.loads(body)


def test_the_sources_are_what_the_rows_say(ledger, conn):
    src = {r["transaction_id"]: (r["category"], r["category_source"], r["ai_verdict"])
           for r in conn.execute("SELECT transaction_id, category, category_source, ai_verdict FROM v_transactions")}
    assert src["other_confirmed"] == ("Other", "plaid", "confirmed")
    assert src["other_user"][:2] == ("Other", "user") and src["other_rule"][:2] == ("Other", "rule")
    assert src["other_changed"] == ("Other", "ai", "changed")
    assert src["default_confirmed"] == ("Other", "default", "confirmed")


def test_review_holds_only_what_nobody_decided(ledger):
    page = get(ledger, "/api/transactions?month=2026-09&kind=review")
    assert [r["transaction_id"] for r in page["rows"]] == IN_REVIEW
    rows = {r["transaction_id"]: r["review"] for r in get(ledger, "/api/transactions?month=2026-09&limit=500")["rows"]}
    assert sorted(t for t, v in rows.items() if v) == sorted(IN_REVIEW)            # each row's flag agrees
    assert page["counts"]["review"] == 4 and get(ledger, "/api/counts?month=2026-09")["review"] == 4


def test_the_read_command_uses_the_same_rule(ledger, capsys):
    cli.main(["txns", "--uncategorized", "--json"])
    unc = json.loads(capsys.readouterr().out)
    assert [r["transaction_id"] for r in unc["rows"]] == ["default_confirmed", "other_flagged", "other_new"]
    cli.main(["txns", "--flagged", "--json"])
    flagged = json.loads(capsys.readouterr().out)
    assert [r["transaction_id"] for r in flagged["rows"]] == ["shop_flagged", "other_flagged"]
    assert unc["counts"]["review"] == 3 and all(r["review"] for r in unc["rows"] + flagged["rows"])
