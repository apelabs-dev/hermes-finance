"""Transactions `?source=`: one income source of the Cash flow report, so a Cash flow row can open the
rows it counts. The filter is the report's own bucketing (`reports.income_source_sql`, the SQL twin of
`reports._income_source`): the rows, their count and their total are the report's, for every window.

Synthetic data only."""

from __future__ import annotations

from datetime import date

import pytest

import demo_data
from hermes_finance import api, db, reads, reports

TODAY = date(2026, 11, 15)
EXTRA = [("is_int", "2026-11-03", -5.37, "INCOME_INTEREST_EARNED"),
         ("is_tax", "2026-11-04", -61.40, "INCOME_TAX_REFUND"),
         ("is_gift", "2026-11-05", -20.00, "INCOME_OTHER_INCOME"),
         ("is_blank", "2026-11-07", -9.00, None)]       # an income row with no detailed code is Other income


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "income-source.sqlite")
    demo_data.build(c, today=TODAY)
    account_id, item_id, currency = c.execute(
        "SELECT account_id, item_id, iso_currency FROM accounts WHERE type = 'depository' LIMIT 1").fetchone()
    with c:
        for tid, day, amt, detailed in EXTRA:
            c.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                      "iso_currency, pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'INCOME', ?, 0, '2026-11-15T20:00:00Z', '2026-11-15T20:00:00Z')",
                      (tid, account_id, item_id, day, tid, tid, amt, currency, detailed))
    yield c
    c.close()


@pytest.mark.parametrize("window", [{"month": "2026-11"}, {"since": "30d"}, {"since": "2026-09-01", "until": "2026-11-15"}, {}])
def test_every_source_lists_exactly_the_rows_and_total_the_report_counts(conn, window):
    period = reads.parse_period(TODAY, window.get("month"), window.get("since"), window.get("until"), default="all")
    flow = reports.income_sources(conn, period.start, period.end)
    seen = {s["source"]: s for s in flow["sources"]}
    assert set(seen) == {"paychecks", "interest_tax_refunds", "other"}
    rows_by_source = {}
    for source in reports.INCOME_LABELS:
        page = api.transactions(conn, {**window, "source": source, "kind": "income", "limit": "500"}, TODAY)
        rows_by_source[source] = {r["transaction_id"] for r in page["rows"]}
        want = seen.get(source, {"count": 0, "amount": 0.0})
        assert page["total"] == want["count"], (source, window)
        assert abs(-page["sum"] - want["amount"]) < 0.01, (source, window)
    everything = api.transactions(conn, {**window, "kind": "income", "limit": "500"}, TODAY)
    ids = [r["transaction_id"] for r in everything["rows"]]
    # the three sources split every income row: each in one, none in two
    assert sorted(ids) == sorted(i for rows in rows_by_source.values() for i in rows)


def test_the_codes_decide_the_source(conn):
    got = {s: {r["transaction_id"] for r in api.transactions(
        conn, {"month": "2026-11", "source": s, "limit": "500"}, TODAY)["rows"]} for s in reports.INCOME_LABELS}
    assert got["interest_tax_refunds"] == {"is_int", "is_tax"}
    assert {"is_gift", "is_blank"} <= got["other"] and not ({"is_int", "is_tax"} & got["other"])
    assert not {"is_int", "is_tax", "is_gift", "is_blank"} & got["paychecks"]


def test_a_source_never_lists_spending(conn):
    page = api.transactions(conn, {"month": "2026-11", "source": "other", "limit": "500"}, TODAY)
    assert page["rows"] and all(r["is_income"] for r in page["rows"])


def test_an_unknown_source_is_refused(conn):
    with pytest.raises(api.ApiError, match="source must be one of paychecks, interest_tax_refunds, other"):
        api.transactions(conn, {"source": "salary"}, TODAY)
