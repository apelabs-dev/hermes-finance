"""polish P1: the Transactions page's filters drawer and order.

Any of the picked categories, whole groups or Uncategorized (split-aware: a split purchase matches by its lines, once),
any of the picked tags, the bank, the search and the kind all compose; the counts, the kind counts, `sum`, the day
totals and the pages follow every filter. The drawer's counts (facets) are Where it went's for a spending category.
Every sort ends with the same tiebreaks (biggest amount, latest authorized, id), so two loads are identical. Saved
searches keep lists of categories, groups and tags. `finance txns` mirrors --group, --sort and a repeated --tag."""

from __future__ import annotations

import json
from datetime import date

import pytest

import factory as f
from hermes_finance import api, classify, cli, config, features, insights, reads, txmeta
from hermes_finance.web import App

TODAY = date(2026, 10, 20)
LOCAL = {"Host": "127.0.0.1:8765"}
# small categories remain individually filterable
SMALL_CATEGORIES = ["Other Transport", "Subscriptions", "Eating Out", "Entertainment"]


def authorized(conn, tid, when):
    """Plaid's authorized_datetime on a row (the factory gives only the authorized date)."""
    raw = json.loads(conn.execute("SELECT raw_json FROM transactions WHERE transaction_id = ?", (tid,)).fetchone()[0])
    with conn:
        conn.execute("UPDATE transactions SET raw_json = ? WHERE transaction_id = ?",
                     (json.dumps({**raw, "authorized_datetime": when}), tid))


@pytest.fixture
def october(conn):
    """2026-10: Other Transport 1, Subscriptions 2, Eating Out 3, Entertainment 1 (7 payments, $112.85: each under 3% of
    the month); Other Shopping 3 and Groceries 3; a paycheck. Five rows on Oct 6."""
    f.bank(conn, "item_a", "Chase")
    f.acct(conn, "acc_chk", current=1000.0)
    f.bank(conn, "item_b", "Nova")
    f.acct(conn, "acc_nova", item_id="item_b", name="Nova Checking", mask="9876")
    t = {
        "metro": f.tx(conn, "2026-10-06", 9.50, "Metro", cat="Other Transport", tid="t-metro"),
        "sub_a": f.tx(conn, "2026-10-06", 13.24, "Netflix", cat="Other", tid="t-sub-a"),
        "sub_b": f.tx(conn, "2026-10-06", 13.24, "Hulu", cat="Other", tid="t-sub-b"),
        "lunch": f.tx(conn, "2026-10-06", 18.25, "Chipotle", cat="Eating Out", tid="t-lunch"),
        "dinner": f.tx(conn, "2026-10-09", 22.40, "Sweetgreen", cat="Eating Out", account_id="acc_nova", item_id="item_b"),
        "brunch": f.tx(conn, "2026-10-12", 14.72, "Blue Plate", cat="Eating Out"),
        "movie": f.tx(conn, "2026-10-14", 21.50, "AMC", cat="Entertainment"),
        "amazon": f.tx(conn, "2026-10-02", 420.00, "Amazon", cat="Other Shopping"),
        "target": f.tx(conn, "2026-10-08", 365.10, "Target", cat="Other Shopping"),
        "bestbuy": f.tx(conn, "2026-10-15", 289.99, "Best Buy", cat="Other Shopping"),
        "costco": f.tx(conn, "2026-10-03", 512.30, "Costco", cat="Groceries"),
        "tj": f.tx(conn, "2026-10-06", 288.75, "Trader Joe's", cat="Groceries", tid="t-tj"),
        "wf": f.tx(conn, "2026-10-18", 401.22, "Whole Foods", cat="Groceries"),
        "pay": f.tx(conn, "2026-10-06", -2000.00, "Payroll", cat="Income", tid="t-pay"),
        "sept": f.tx(conn, "2026-09-20", 30.00, "Chipotle", cat="Eating Out"),
    }
    for k in ("sub_a", "sub_b"):
        classify.set_category(conn, t[k], "Subscriptions")
    authorized(conn, t["tj"], "2026-10-06T10:05:00Z")
    authorized(conn, t["lunch"], "2026-10-06T12:30:00Z")
    authorized(conn, t["sub_a"], "2026-10-06T08:00:00Z")
    authorized(conn, t["sub_b"], "2026-10-06T19:30:00Z")
    return t


def get(conn, **params):
    return api.transactions(conn, {"month": "2026-10", "limit": "500", **params}, TODAY)


def ids(page):
    return [r["transaction_id"] for r in page["rows"]]


# --- the acceptance fixture ------------------------------------------------------------------------------------------

def test_small_categories_and_groups_reconcile_with_transactions(october, conn):
    spend = {c["category"]: (c["spend"], c["count"]) for c in insights.category_spend(conn, "2026-10", TODAY) if c["count"]}
    assert {k: n for k, (_, n) in spend.items()} == {"Other Transport": 1, "Subscriptions": 2, "Eating Out": 3,
                                                     "Entertainment": 1, "Other Shopping": 3, "Groceries": 3}
    for g in insights.group_spend(conn, "2026-10", insights.category_spend(conn, "2026-10", TODAY)):
        filtered = get(conn, group=g["group"])
        assert (g["spend"], g["count"]) == (filtered["sum"], filtered["total"])
    page = get(conn, category=SMALL_CATEGORIES, facets="1")
    assert page["total"] == 7 == sum(spend[c][1] for c in SMALL_CATEGORIES) and page["sum"] == 112.85
    assert page["counts"] == {"all": 7, "review": 0, "spend": 7, "income": 0, "transfers": 0}
    # the drawer's counts are Where it went's, for every spending category; groups count each payment once
    facets = get(conn, facets="1")["facets"]
    assert {c: n for c, n in facets["categories"].items() if c in spend} == {c: n for c, (_, n) in spend.items()}
    assert facets["categories"]["Income"] == 1 and "Other" not in facets["categories"]
    assert facets["groups"] == {"Food": 6, "Transport": 1, "Shopping": 3, "Bills": 2, "Leisure": 1}
    assert facets["uncategorized"] == 0


def test_the_oct_6_rows_come_in_a_stable_meaningful_order(october, conn):
    t = october
    day = [r["transaction_id"] for r in get(conn)["rows"] if r["date"] == "2026-10-06"]
    # the biggest spend first; the two $13.24 subscriptions by the latest authorization; money in last
    assert day == [t["tj"], t["lunch"], t["sub_b"], t["sub_a"], t["metro"], t["pay"]]
    assert ids(get(conn)) == ids(get(conn))
    assert api.recent(conn, "2026-10", 20)[-6:][0]["date"] == "2026-10-06"
    assert [r["transaction_id"] for r in api.recent(conn, "2026-10", 20) if r["date"] == "2026-10-06"] == day[:-1] + [t["pay"]]


def test_same_amount_same_time_falls_back_to_the_id(october, conn):
    for tid in ("t-z", "t-a"):
        f.tx(conn, "2026-10-07", 5.00, "Parking", cat="Other Transport", tid=tid)
    rows = [r["transaction_id"] for r in get(conn)["rows"] if r["date"] == "2026-10-07"]
    assert rows == ["t-a", "t-z"]


@pytest.mark.parametrize("sort", list(api.SORTS))
def test_every_sort_is_deterministic_and_pages_agree(october, conn, sort):
    whole = ids(get(conn, sort=sort))
    assert whole == ids(get(conn, sort=sort)) and len(whole) == 14
    paged = [tid for off in range(0, 14, 4) for tid in ids(get(conn, sort=sort, limit="4", offset=str(off)))]
    assert paged == whole
    rows = get(conn, sort=sort)["rows"]
    col, way = sort.rsplit("_", 1)
    key = {"date": lambda r: r["date"], "amount": lambda r: r["amount"],
           "merchant": lambda r: (r["merchant_name"] or r["name"] or "").casefold(),
           "category": lambda r: r["category"].casefold(),
           "account": lambda r: (r["institution_name"] or "").casefold()}[col]
    keys = [key(r) for r in rows]
    assert keys == sorted(keys, reverse=way == "desc")


def test_amount_sorts_spend_first_and_money_in_last(october, conn):
    rows = get(conn, sort="amount_desc")["rows"]
    assert rows[0]["amount"] == 512.30 and rows[-1]["amount"] == -2000.00
    assert get(conn, sort="amount_asc")["rows"][0]["amount"] == -2000.00


def test_an_unknown_sort_is_refused(october, conn):
    with pytest.raises(api.ApiError, match="sort must be one of"):
        get(conn, sort="date; DROP TABLE x")


# --- filters ---------------------------------------------------------------------------------------------------------

def test_groups_categories_and_uncategorized_are_any_of(october, conn):
    t = october
    assert set(ids(get(conn, group="Food"))) == {t["lunch"], t["dinner"], t["brunch"], t["costco"], t["tj"], t["wf"]}
    assert set(ids(get(conn, group=["Bills", "Leisure"]))) == {t["sub_a"], t["sub_b"], t["movie"]}
    both = get(conn, group=["Shopping"], category=["Other Transport", "Other Shopping"])     # the overlap counts once
    assert both["total"] == 4 and both["sum"] == 1084.59
    other = f.tx(conn, "2026-10-10", 7.77, "Mystery", cat="Other")              # under Other, nobody decided it
    assert ids(get(conn, uncategorized="1")) == [other]
    assert set(ids(get(conn, uncategorized="1", category="Other Transport"))) == {other, t["metro"]}
    assert get(conn, facets="1")["facets"]["uncategorized"] == 1
    # the single category of before still works, and a category that matches nothing is no error
    assert ids(get(conn, category="Entertainment")) == [t["movie"]]
    assert get(conn, category="Nope")["total"] == 0


def test_a_split_matches_by_its_lines_once_and_totals_count_its_lines(october, conn):
    t = october
    before = get(conn)
    txmeta.split(conn, t["costco"], [{"category": "Groceries", "amount": 400.00},
                                     {"category": "Other Shopping", "amount": 112.30}])
    shop = get(conn, category="Other Shopping")
    assert t["costco"] in ids(shop) and shop["total"] == 4 and shop["sum"] == round(1075.09 + 112.30, 2)
    groc = get(conn, category="Groceries")
    assert groc["total"] == 3 and groc["sum"] == round(1202.27 - 112.30, 2)
    both = get(conn, category=["Groceries", "Other Shopping"])
    assert both["total"] == 6 and ids(both).count(t["costco"]) == 1 and both["sum"] == round(1075.09 + 1202.27, 2)
    assert get(conn, group="Food")["sum"] == round(1202.27 - 112.30 + 55.37, 2)
    assert get(conn, group=["Food", "Shopping"])["total"] == 9
    after = get(conn)
    assert (after["total"], after["sum"], after["counts"]) == (before["total"], before["sum"], before["counts"])
    # the drawer counts it in each of its categories, once in a group, as Where it went does
    facets = get(conn, facets="1")["facets"]
    spend = {c["category"]: c["count"] for c in insights.category_spend(conn, "2026-10", TODAY) if c["count"]}
    assert facets["categories"]["Other Shopping"] == spend["Other Shopping"] == 4
    assert facets["categories"]["Groceries"] == 3
    assert facets["groups"]["Food"] == 6 and facets["groups"]["Shopping"] == 4


def test_tags_are_any_of_and_compose_with_everything(october, conn):
    t = october
    txmeta.set_meta(conn, t["lunch"], {"tags": ["coffee-run"]})
    txmeta.set_meta(conn, t["brunch"], {"tags": ["coffee-run", "trip"]})
    txmeta.set_meta(conn, t["amazon"], {"tags": ["trip"]})
    assert set(ids(get(conn, tag=["Coffee-Run", "trip"]))) == {t["lunch"], t["brunch"], t["amazon"]}
    assert set(ids(get(conn, tag="trip", category="Eating Out"))) == {t["brunch"]}
    assert ids(get(conn, tag="trip", group="Food", bank="Chase", q="blue")) == [t["brunch"]]
    assert get(conn, tag="trip", group="Food", bank="Nova")["total"] == 0
    assert ids(get(conn, group="Food", bank="Nova")) == [t["dinner"]]
    page = get(conn, tag=["trip"], kind="spend", facets="1")
    assert page["counts"] == {"all": 2, "review": 0, "spend": 2, "income": 0, "transfers": 0}
    # the tag counts ignore the tag picks (so another tag can be added) but follow the rest
    assert page["facets"]["tags"] == [{"name": "coffee-run", "count": 2}, {"name": "trip", "count": 2}]
    assert get(conn, group="Shopping", facets="1")["facets"]["tags"] == [{"name": "coffee-run", "count": 0},
                                                                         {"name": "trip", "count": 1}]
    # and the category counts ignore the category picks but follow the tags
    cats = get(conn, tag="trip", category="Other Shopping", facets="1")["facets"]["categories"]
    assert cats == {"Eating Out": 1, "Other Shopping": 1}


def test_day_totals_and_sum_follow_the_filters(october, conn):
    page = get(conn, limit="3")
    assert set(page["days"]) == {"2026-10-18", "2026-10-15", "2026-10-14"}       # the dates on this page
    assert page["days"]["2026-10-18"] == {"count": 1, "total": 401.22}
    six = get(conn, limit="500")["days"]["2026-10-06"]
    assert six == {"count": 6, "total": round(288.75 + 18.25 + 13.24 * 2 + 9.50 - 2000, 2)}
    assert get(conn, kind="spend")["days"]["2026-10-06"] == {"count": 5, "total": 342.98}
    assert get(conn, kind="spend")["sum"] == round(112.85 + 1075.09 + 1202.27, 2)
    # a day's total is the whole day's, even when the page shows only part of it
    assert get(conn, limit="9")["days"]["2026-10-06"]["count"] == 6


def test_totals_leave_a_transfer_out_unless_only_transfers_match(october, conn):
    before = get(conn)
    card = f.tx(conn, "2026-10-06", 731.63, "Chase Credit Card Payment", cat="Transfers")
    page = get(conn)
    assert card in ids(page) and page["total"] == before["total"] + 1
    assert page["sum"] == before["sum"] and page["days"]["2026-10-06"] == {**before["days"]["2026-10-06"], "count": 7}
    for only in ({"kind": "transfers"}, {"category": "Transfers"}):
        page = get(conn, **only)
        assert ids(page) == [card] and page["sum"] == 731.63 and page["days"]["2026-10-06"] == {"count": 1, "total": 731.63}


def test_too_many_picks_and_non_text_are_refused(october, conn):
    with pytest.raises(api.ApiError, match="at most"):
        get(conn, category=[f"c{i}" for i in range(api.MAX_PICKS + 1)])
    with pytest.raises(api.ApiError, match="must be text"):
        api.transactions(conn, {"tag": [3]}, TODAY)


# --- the web route ---------------------------------------------------------------------------------------------------

def test_the_route_takes_repeated_picks(october, conn, client):
    app = App(config.db_path(), lambda: client)
    app.today = lambda: TODAY

    def call(path):
        status, _, raw = app.handle("GET", path, LOCAL, b"")
        return status, json.loads(raw)

    qs = "&".join(f"category={c.replace(' ', '%20')}" for c in SMALL_CATEGORIES)
    s, body = call(f"/api/transactions?month=2026-10&{qs}&sort=amount_desc&facets=1")
    assert s == 200 and body["total"] == 7 and body["sum"] == 112.85 and body["sort"] == "amount_desc"
    assert [r["amount"] for r in body["rows"]][:2] == [22.40, 21.50] and "facets" in body
    s, body = call("/api/transactions?month=2026-10&group=Food&group=Bills&tag=nope")
    assert s == 200 and body["total"] == 0
    assert call("/api/transactions?month=2026-10&category=Other%20Transport")[1]["total"] == 1
    assert call("/api/transactions?sort=sideways")[0] == 400


# --- saved searches --------------------------------------------------------------------------------------------------

def test_a_saved_search_round_trips_lists(october, conn, client):
    txmeta.set_meta(conn, october["brunch"], {"tags": ["trip"]})
    filters = {"category": ["Other Transport", "Subscriptions"], "group": "Leisure",
               "tag": ["trip", "#Trip", "coffee-run"], "uncategorized": True, "kind": "spend", "q": ""}
    saved = txmeta.save_filter(conn, "Small stuff", filters)
    assert saved["filters"] == {"category": ["Other Transport", "Subscriptions"], "group": "Leisure",
                                "uncategorized": "1", "kind": "spend", "tag": ["trip", "coffee-run"]}
    back = txmeta.saved_filters(conn)[0]["filters"]
    assert back == saved["filters"]
    # it applies as saved: the categories, the group, Uncategorized (any of them) AND the tags
    page = api.transactions(conn, {**back, "month": "2026-10"}, TODAY)
    assert page["total"] == 0
    back2 = txmeta.save_filter(conn, "Picks", {"category": SMALL_CATEGORIES})["filters"]
    assert api.transactions(conn, {**back2, "month": "2026-10"}, TODAY)["total"] == 7
    got = features.saved_filters(conn, {"month": "2026-10"}, TODAY)["filters"]
    assert [(x["name"], x["count"]) for x in got] == [("Small stuff", 0), ("Picks", 7)]
    one = txmeta.save_filter(conn, "One", {"category": ["Other Transport"], "tag": ["trip"]})["filters"]
    assert one == {"category": "Other Transport", "tag": "trip"}               # one pick is kept as a name, as before


@pytest.mark.parametrize("filters,message", [
    ({"group": "Snacks"}, "unknown category group"),
    ({"category": ["Other Transport", "Nope"]}, "unknown category"),
    ({"bank": ["Chase", "Nova"]}, "bank must be text"),
    ({"category": ["Other Transport"] * 61}, "at most 60"),
    ({"uncategorized": "maybe"}, "uncategorized must be 1"),
    ({"tag": ["ok", 5]}, "tag must be text"),
    ({"uncategorized": False, "category": []}, "nothing to save"),
])
def test_bad_saved_searches_are_refused(october, conn, filters, message):
    with pytest.raises(txmeta.TxMetaError, match=message):
        txmeta.save_filter(conn, "x", filters)


# --- finance txns ----------------------------------------------------------------------------------------------------

def test_finance_txns_mirrors_group_sort_and_tags(october, conn, data_dir, capsys, monkeypatch):
    t = october
    txmeta.set_meta(conn, t["lunch"], {"tags": ["coffee-run"]})
    txmeta.set_meta(conn, t["amazon"], {"tags": ["trip"]})
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    assert cli.main(["txns", "--month", "2026-10", "--group", "food", "--sort", "amount_asc", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert [r["amount"] for r in got["rows"]] == [14.72, 18.25, 22.40, 288.75, 401.22, 512.30]
    assert got["filters"]["group"] == "Food" and got["filters"]["sort"] == "amount_asc"
    assert cli.main(["txns", "--tag", "trip", "--tag", "coffee-run", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert {r["transaction_id"] for r in got["rows"]} == {t["lunch"], t["amazon"]} and got["filters"]["tag"] == ["trip", "coffee-run"]
    assert cli.main(["txns", "--group", "Snacks", "--json"]) == 2
    assert "unknown category group" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        cli.main(["txns", "--sort", "sideways"])
    assert e.value.code == 2 and "invalid choice: 'sideways'" in capsys.readouterr().err
    assert cli.main(["txns", "--month", "2026-10", "--group", "Food", "--tag", "coffee-run"]) == 0
    assert "group Food, tag coffee-run" in capsys.readouterr().out
    # the default order is the page's: within Oct 6 the biggest amount first
    f_ = reads.parse_filters(conn, TODAY, month="2026-10")
    day = [r["transaction_id"] for r in reads.txns(conn, f_, TODAY)["rows"] if r["date"] == "2026-10-06"]
    assert day == [t["tj"], t["lunch"], t["sub_b"], t["sub_a"], t["metro"], t["pay"]]
