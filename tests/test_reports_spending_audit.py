"""Independent Spending reconciliation on the synthetic demo ledger and planted boundary rows."""
from collections import defaultdict
from datetime import date, timedelta

import pytest

import demo_data
from hermes_finance import api, reads, reports, reports_page, txmeta

TODAY = date(2026, 11, 15)


@pytest.fixture
def ledger(conn):
    demo_data.build(conn, today=TODAY)
    return conn


def plant(conn, ident, day, amount, pfc=None, currency="USD", removed=0):
    # The demo checking account already exists; don't reuse a demo row's classification.
    conn.execute("""INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount,
                    iso_currency, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at)
                    SELECT ?, account_id, item_id, ?, ?, ?, ?, ?, ?, ?, updated_at, updated_at
                    FROM accounts WHERE type = 'depository' AND subtype = 'checking' LIMIT 1""",
                 (ident, day, ident, amount, currency, "INCOME" if (pfc or "").startswith("INCOME_") else None,
                  pfc, removed))


# No v_transactions, v_spend_lines, or app predicates: start from raw rows, expand splits and independently resolve
# the demo's user/PFC/default classification. All accounts in this fixture are active, included and denominated in USD.
RAW_LINES = """WITH lines AS (
    SELECT t.transaction_id AS id, t.date AS day, t.name, t.merchant_name, t.amount,
           UPPER(COALESCE(NULLIF(TRIM(t.iso_currency), ''), 'USD')) AS currency,
           COALESCE(s.category_id, f.category_id, d.category_id, p.category_id,
                    (SELECT id FROM categories WHERE name = 'Other')) AS cid,
           COALESCE(s.amount, t.amount) AS value
    FROM transactions t LEFT JOIN transaction_splits s USING (transaction_id)
    LEFT JOIN classifications f USING (transaction_id)
    LEFT JOIN plaid_category_map d ON t.pfc_detailed = d.pfc_code
    LEFT JOIN plaid_category_map p ON t.pfc_primary = p.pfc_code
    WHERE t.removed = 0 AND t.date BETWEEN ? AND ?
) SELECT l.*, c.name AS category, c.is_income, c.is_transfer, c.group_id, g.name AS grp,
         CAST(ROUND(value * 100) AS INTEGER) AS cents
  FROM lines l JOIN categories c ON c.id = l.cid LEFT JOIN category_groups g ON g.id = c.group_id
  ORDER BY day, id"""


def raw(conn, start, end):
    return list(conn.execute(RAW_LINES, (start, end)))


def spend(rows, currency="USD"):
    return [r for r in rows if not (r["is_income"] or r["is_transfer"]) and r["currency"] == currency]


def by_key(rows, key):
    out = defaultdict(list)
    for r in rows:
        out[key(r)].append(r)
    return out


def net(rows):
    return sum(r["cents"] for r in rows) / 100


def amounts(rows):
    return (sum(max(0, r["cents"]) for r in rows) / 100, sum(min(0, r["cents"]) for r in rows) / 100,
            net(rows), len({r["id"] for r in rows}))


def check_amounts(got, rows):
    assert tuple(got[k] for k in ("gross", "refunds", "net", "count")) == amounts(rows)


@pytest.mark.parametrize("choice", ["month", "7d", "30d", "90d", "all", "custom"])
def test_selected_window_counts_exactly_its_inclusive_dates(ledger, choice):
    end = TODAY.isoformat()
    if choice == "all":
        params, start = {"window": "all"}, "2023-01-08"
        plant(ledger, "old", start, 111.11)
        assert ledger.execute("SELECT MIN(date) FROM transactions").fetchone()[0] == start
    elif choice == "month":
        params, start, end = {"month": "2026-10"}, "2026-10-01", "2026-10-31"
    elif choice == "custom":
        start, end = "2026-10-04", "2026-11-08"
        params = {"since": start, "until": end}
    else:
        params = {"since": choice}
        start = (TODAY - timedelta(days=int(choice[:-1]) - 1)).isoformat()
    for delta, edge, ident, amount in ((0, start, "start", 19.31), (0, end, "end", 27.11),
                                        (1, end, "after", 9000), (-1, start, "before", 8000)):
        if ident == "before" and choice == "all":
            continue
        day = (date.fromisoformat(edge) + timedelta(days=delta)).isoformat()
        plant(ledger, ident, day, amount)
    rows = spend(raw(ledger, start, end))
    got = reports_page.spending(ledger, params, TODAY)
    assert got["period"] == {"start": start, "end": end}
    assert got["window"]["days"] == (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    assert net(rows) != 0
    assert got.get("totals", {}).get("net") == net(rows)
    check_amounts(got["totals"], rows)


def test_no_completed_month_is_not_a_zero_or_partial_average(ledger):
    ledger.execute("DELETE FROM transactions")
    plant(ledger, "first", "2026-11-14", 35.00)
    plant(ledger, "income", "2026-11-14", -100.00, "INCOME_WAGES")
    k = reports.year_spending(ledger, TODAY)["kpis"]
    assert (k["months"], k["average"]) == (0, None)
    assert k["savings_rate"] == {"now": 65.0, "average": None, "delta": None}


@pytest.mark.parametrize("currency", ["USD", "EUR"])
def test_split_count_is_one_per_group_or_foreign_currency(ledger, currency):
    ledger.execute("DELETE FROM transactions")
    plant(ledger, "split", "2026-11-06", 30, currency=currency)
    txmeta.split(ledger, "split", [{"category": "Groceries", "amount": 20}, {"category": "Eating Out", "amount": 10}])
    start, end = "2026-11-01", "2026-11-30"
    rows = spend(raw(ledger, start, end), currency)
    assert len({r["group_id"] for r in rows}) == 1
    if currency == "USD":
        groups = reports.spending_groups(ledger, start, end)["groups"]
        assert [(g["count"], g["net"], g["share"]) for g in groups] == [(amounts(rows)[3], net(rows), 100)]
    else:
        foreign = reports.category_totals(ledger, start, end)["foreign"]
        check_amounts(foreign[0]["totals"], rows)


@pytest.mark.parametrize("bound", ["20261101", "2026-W44-7"])
def test_direct_reports_dont_accept_dates_that_cannot_compare_lexically(ledger, bound):
    with pytest.raises(reports.ReportError, match="YYYY-MM-DD"):
        reports.spending_by_day(ledger, bound)


def test_all_time_doesnt_start_from_a_future_transaction(ledger):
    ledger.execute("DELETE FROM transactions")
    plant(ledger, "future", "2026-11-16", 800)
    got = reports_page.spending(ledger, {"window": "all"}, TODAY)
    assert got["period"] == {"start": TODAY.isoformat(), "end": TODAY.isoformat()}
    assert got["totals"]["net"] == 0


@pytest.mark.parametrize("measure", ["spending", "income"])
def test_subcent_rows_follow_one_rule_in_reports_overview_and_budgets(ledger, measure):
    ledger.execute("DELETE FROM transactions")
    for n in range(2):
        plant(ledger, f"charge{n}", "2026-10-31", 1.006, "FOOD_AND_DRINK_GROCERIES")
        plant(ledger, f"income{n}", "2026-10-31", -10.006, "INCOME_WAGES")
    start, end = "2026-10-01", "2026-10-31"
    basic = raw(ledger, start, end)
    expected = net(spend(basic))
    income = -net([r for r in basic if r["is_income"]])
    params = {"month": "2026-10"}
    ov, bu = api.overview(ledger, params, TODAY), api.budgets_page(ledger, params, TODAY)
    if measure == "income":
        assert reports.remaining_after_spending(ledger, start, end)["income"] == income
        assert ov["cashflow"]["months"][-1]["income"] == income
        return
    rpt = reports.category_totals(ledger, start, end)
    check_amounts(rpt["totals"], spend(basic))
    assert ov["summary"]["mtd"] == expected
    for page in (ov, bu):
        assert next(c for c in page["categories"] if c["category"] == "Groceries")["spend"] == expected
    assert reads.spend(ledger, reads.Filters(reads.Period.of_month("2026-10")), TODAY)["total"] == expected
