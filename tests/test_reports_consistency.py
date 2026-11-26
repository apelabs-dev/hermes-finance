"""The same figure, the same answer, on every surface (0.5 numbers audit).

One synthetic book (tests/demo_data.py with today pinned to 2026-11-26, plus a refund that beats its category's
spending and a pending charge) is read through every door that carries a Reports number:

  * the HTTP payloads  /api/reports/spending | cashflow | forecast | activity
  * reads.py            spending_report / cashflow_report / forecast_report
  * the read tools      finance_reports_*, finance_spend, finance_budget_status (tooldefs, the plugin handlers)
  * the CLI             finance reports-* --json   (a tool answers exactly what its command prints)
  * Overview and Budgets (/api/overview, /api/budgets)

Any difference beyond $0.005 is a failure. A surface is never edited to copy another: the comparisons name the shared
function (reports.py) and fail where the surfaces disagree. Synthetic data only; no network, no real keys.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from urllib.parse import urlencode

import pytest

import agent_tools
import demo_data
from hermes_finance import config, db, insights, reads, reports, tooldefs, web

TODAY = date(2026, 11, 26)
TOL = 0.005
LOCAL = {"Host": "127.0.0.1:8765"}
# The periods every surface is asked for: three months (the current one, a closed one, one that ends in the past), the
# rolling windows the page offers, and a typed range.
PERIODS = [{"month": "2026-11"}, {"month": "2026-10"}, {"month": "2026-07"},
           {"since": "7d"}, {"since": "30d"}, {"since": "90d"},
           {"since": "2026-10-10", "until": "2026-11-15"}]


def _diffs(a, b, path=""):
    """Every place two JSON values differ: numbers beyond TOL, anything else by equality."""
    if isinstance(a, bool) or isinstance(b, bool):
        if a != b:
            yield path, a, b
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if abs(a - b) > TOL:
            yield path, a, b
    elif isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                yield f"{path}.{k}", a.get(k, "<missing>"), b.get(k, "<missing>")
            else:
                yield from _diffs(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield f"{path}[len]", len(a), len(b)
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                yield from _diffs(x, y, f"{path}[{i}]")
    elif a != b:
        yield path, a, b


def same(a, b, what):
    found = list(_diffs(a, b))
    assert not found, f"{what}: {found[:5]}"


@pytest.fixture
def books(data_dir, monkeypatch):
    """demo_data's book at 2026-11-26 plus two edge rows the demo has no room for: a refund in a category with no
    spending (it nets below zero, so it is a \"Refunds over spending\" source) and a pending charge."""
    conn = db.connect()
    demo_data.build(conn, TODAY)
    demo_data.extras(conn, TODAY, holding_days=3)
    acct = conn.execute("SELECT account_id, item_id FROM accounts WHERE subtype = 'checking' "
                        "ORDER BY account_id LIMIT 1").fetchone()
    stamp = "2026-11-26T20:00:00Z"
    with conn:
        for tid, day, amount, name, primary, detailed, pending in (
                ("edge_refund", "2026-11-20", -35.00, "Streaming refund", "ENTERTAINMENT",
                 "ENTERTAINMENT_MUSIC_AND_AUDIO", 0),
                ("edge_pending", "2026-11-25", 33.33, "Pending Market", "FOOD_AND_DRINK",
                 "FOOD_AND_DRINK_GROCERIES", 1)):
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                "iso_currency, pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, 'USD', ?, ?, ?, 0, ?, ?)",
                (tid, acct["account_id"], acct["item_id"], day, name, amount, pending, primary, detailed,
                 stamp, stamp))
    conn.close()
    monkeypatch.setattr(tooldefs, "today", lambda: TODAY)
    return config.db_path()


@pytest.fixture
def ro(books):
    conn = db.connect_readonly(books)
    yield conn
    conn.close()


@pytest.fixture
def app(books):
    a = web.App(books, lambda: None, "127.0.0.1", 8765, (), ())
    a.today = lambda: TODAY
    return a


def _get(app, path, **params):
    status, ctype, body = app.handle("GET", path + (f"?{urlencode(params)}" if params else ""), LOCAL, b"")
    assert status == 200 and ctype == "application/json", (path, params, status, body[:200])
    return json.loads(body)


def _tool(books, name, args):
    return json.loads(tooldefs.render(tooldefs.run_read(name, args, books, TODAY)))


def _cli(capsys, command, args):
    argv = [command] + [x for k, v in args.items() for x in (f"--{k}", str(v))] + ["--json"]
    code, out, err = agent_tools.run_cli(argv, capsys)
    assert code in (0, None), (argv, code, err)
    return json.loads(out)


def _period(ro, p):
    return reads.parse_period(TODAY, p.get("month"), p.get("since"), p.get("until"))


def test_the_fixture_has_the_edge_cases(ro):
    """The book must contain what the comparisons are about: a category below zero, and a pending charge."""
    totals = reports.category_totals(ro, "2026-11-01", "2026-11-30")
    assert [r["category"] for r in totals["refunds_over"]], "no category nets below zero in the fixture"
    assert ro.execute("SELECT COUNT(*) FROM transactions WHERE pending = 1 AND transaction_id = 'edge_pending'"
                      ).fetchone()[0] == 1


@pytest.mark.parametrize("p", PERIODS, ids=lambda p: "-".join(p.values()))
def test_cash_flow_is_one_answer_on_every_surface(books, ro, app, capsys, p):
    ref = reads.cashflow_report(ro, _period(ro, p), TODAY)
    for name, got in (("tool", _tool(books, "finance_reports_cashflow", p)),
                      ("cli", _cli(capsys, "reports-cashflow", p))):
        same(got, ref, f"{name} finance_reports_cashflow {p}")
    page = _get(app, "/api/reports/cashflow", **p)
    skip = {"view", "window", "month", "period"}       # the page's own envelope, not figures
    same({k: v for k, v in page.items() if k not in skip}, {k: v for k, v in ref.items() if k not in skip},
         f"/api/reports/cashflow {p}")
    assert (page["period"]["start"], page["period"]["end"]) == (ref["period"]["start"], ref["period"]["end"])
    # the payload's own arithmetic
    assert page["money_in"] == pytest.approx(page["income"]["total"] + sum(r["amount"] for r in page["refunds_over"]),
                                             abs=TOL)
    assert page["remaining"] == pytest.approx(page["money_in"] - page["money_out"], abs=TOL)


@pytest.mark.parametrize("p", PERIODS, ids=lambda p: "-".join(p.values()))
def test_spending_is_one_answer_on_every_surface(books, ro, app, capsys, p):
    ref = reads.spending_report(ro, _period(ro, p), TODAY)
    for name, got in (("tool", _tool(books, "finance_reports_spending", p)),
                      ("cli", _cli(capsys, "reports-spending", p))):
        same(got, ref, f"{name} finance_reports_spending {p}")
    # the page's heatmap: each month's cell is that month's category_totals, never a second calculation
    year = _get(app, "/api/reports/spending", **p)["year"]
    for m in year["months"]:
        per = reads.Period.of_month(m["month"])
        want = {c["category_id"]: c["net"] for c in reports.category_totals(ro, per.start, per.end)["categories"]}
        for c in year["categories"]:
            cell = next(x for x in c["months"] if x["month"] == m["month"])
            assert cell["net"] == pytest.approx(want.get(c["category_id"], 0.0), abs=TOL), (p, c["category"], m["month"])
        assert m["spending"] == pytest.approx(sum(want.values()), abs=TOL), (p, m["month"])
        assert m["income"] == pytest.approx(reports.money_flow(ro, per.start, per.end)["income"]["total"], abs=TOL)


@pytest.mark.parametrize("days", [30, 60, 90])
def test_forecast_is_one_answer_on_every_surface(books, ro, app, capsys, days):
    ref = reads.forecast_report(ro, TODAY, days)
    page = _get(app, "/api/reports/forecast", days=days)
    same({k: v for k, v in page.items() if k not in ("view", "account_options")}, ref, f"/api/reports/forecast {days}")
    same(_tool(books, "finance_reports_forecast", {"days": days}), ref, f"tool forecast {days}")
    same(_cli(capsys, "reports-forecast", {"days": days}), ref, f"cli forecast {days}")
    # the lines end where the headline says, and the bills in it are the ones the Overview lists
    assert ref["available"] == pytest.approx(ref["lines"]["expected"]["end"], abs=TOL)
    assert ref["bills_total"] == pytest.approx(-sum(e["amount"] for e in ref["events"] if e["amount"] < 0
                                                    and e["kind"] in ("bill", "subscription")), abs=TOL)


def test_the_forecast_bills_are_the_overview_bills(books, app):
    ov = _get(app, "/api/overview", month="2026-11")
    fc = _get(app, "/api/reports/forecast", days=30)
    got = {(e["date"], e["label"]): e["amount"] for e in fc["events"]}
    due = {(i["next_expected"], i["merchant"]): i["amount"] for i in ov["bills"] + ov["upcoming"]
           if i["next_expected"] <= fc["horizon"]["end"]}
    assert due, "the fixture has no bill in the next 30 days"
    for key, amount in due.items():
        assert key in got, f"{key} is on the Overview but not in the forecast"
        assert got[key] == pytest.approx(-amount, abs=TOL), key


@pytest.mark.parametrize("month", ["2026-11", "2026-10", "2026-09"])
def test_overview_budgets_and_tools_agree_with_reports(books, ro, app, month):
    per = reads.Period.of_month(month)
    cats = reports.category_totals(ro, per.start, per.end)
    flow = reports.money_flow(ro, per.start, per.end)
    net = {c["category"]: c["net"] for c in cats["categories"]}
    ov = _get(app, "/api/overview", month=month)
    assert ov["summary"]["mtd"] == pytest.approx(cats["totals"]["net"], abs=TOL)
    assert ov["summary"]["income"] == pytest.approx(flow["income"]["total"], abs=TOL)
    for c in ov["categories"]:
        assert c["spend"] == pytest.approx(net.get(c["category"], 0.0), abs=TOL), c["category"]
    bu = _get(app, "/api/budgets", month=month)
    for c in bu["categories"]:
        assert c["spend"] == pytest.approx(net.get(c["category"], 0.0), abs=TOL), c["category"]
    status = {c["category"]: c for c in _tool(books, "finance_budget_status", {"month": month})["caps"]}
    assert status, "no budget caps in the fixture"
    capped = {c["category"]: c for c in bu["categories"]}
    for name, c in status.items():
        assert c["spent"] == pytest.approx(net.get(name, 0.0), abs=TOL), name
        assert c["cap"] == pytest.approx(capped[name]["cap"], abs=TOL), name
    spend = _tool(books, "finance_spend", {"month": month})
    assert spend["total"] == pytest.approx(cats["totals"]["net"], abs=TOL)


def test_every_drill_down_is_its_category_totals_row(ro, app):
    """The activity payload behind a heatmap cell: gross, refunds, net and count are category_totals' row for that
    category and month, and its rows add up to the net."""
    checked = 0
    for month in ("2026-11", "2026-10"):
        per = reads.Period.of_month(month)
        for c in reports.category_totals(ro, per.start, per.end)["categories"]:
            d = _get(app, "/api/reports/activity", month=month, category_id=c["category_id"])
            for key in ("gross", "refunds", "net"):
                assert d[key] == pytest.approx(c[key], abs=TOL), (month, c["category"], key)
            assert d["count"] == c["count"], (month, c["category"])
            assert sum(r["amount"] for r in d["rows"]) == pytest.approx(d["net"], abs=TOL)
            checked += 1
    assert checked >= 10


def test_a_closed_month_reads_the_same_whenever_it_is_asked(books, ro, monkeypatch):
    """Today moves; a closed month's numbers must not (no 'this month so far' rule leaking into the past)."""
    per = reads.Period.of_month("2026-10")
    before = reports.money_flow(ro, per.start, per.end)
    after = reads.cashflow_report(ro, per, date(2026, 12, 20))
    same({k: v for k, v in after.items() if k not in ("today", "period")},
         {k: v for k, v in before.items()}, "money_flow vs cashflow_report")
    assert insights.month_of(TODAY) == "2026-11"


def test_the_hermes_plugin_answers_like_the_cli(books, ro, capsys):
    """The plugin's handlers run the same read as the command; a Hermes tool call and `finance ... --json` agree."""
    ctx = agent_tools.plugin_ctx()
    for name, command, args in (("finance_reports_spending", "reports-spending", {"month": "2026-10"}),
                                ("finance_reports_cashflow", "reports-cashflow", {"since": "30d"}),
                                ("finance_reports_forecast", "reports-forecast", {"days": 60})):
        same(ctx.call(name, args), _cli(capsys, command, args), f"plugin {name} {args}")
