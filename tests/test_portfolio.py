"""Investments and Loans & debt payloads, and net worth with manual accounts (overview, accounts, history)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from factory import acct, bank, snapshot, tx
from hermes_finance import config, insights, manual, portfolio
from hermes_finance.web import App

TODAY = date(2026, 9, 26)
LOCAL = {"Host": "127.0.0.1:8765"}
LOAN = {"kind": "loan", "name": "Zenith", "institution": "Zenith Financial Services", "subtype": "auto",
        "original_amount": 30947.0, "balance": 5910.0, "monthly_payment": 465.10, "match_pattern": "ZENITH ACH RTL",
        "balance_as_of": "2026-09-23"}


def holding(conn, account_id, security_id, ticker, name, typ, qty, price, value, item_id="item_a"):
    with conn:
        conn.execute("INSERT INTO holdings (account_id, security_id, item_id, ticker_symbol, security_name, "
                     "security_type, quantity, institution_price, institution_value, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?, '2026-09-26T15:00:00Z')",
                     (account_id, security_id, item_id, ticker, name, typ, qty, price, value))


def hsnap(conn, day, account_id, security_id, value, ticker=None):
    with conn:
        conn.execute("INSERT OR REPLACE INTO holding_snapshots VALUES (?,?,?,?,?,?,?)",
                     (day, account_id, security_id, ticker, None, None, value))


@pytest.fixture
def invest(conn):
    bank(conn, "item_a", "Nova")
    acct(conn, "inv", "item_a", name="Nova Self-directed", type_="investment", subtype="brokerage", current=491.47,
         mask="6820")
    acct(conn, "robo", "item_a", name="Nova Robo", type_="investment", subtype="brokerage", current=0.0, mask="7104")
    holding(conn, "inv", "sec_nvda", "NVDA", "NVIDIA Corp", "equity", 2.18176, 225.0, 490.90)
    holding(conn, "inv", "sec_cash", "CUR:USD", "U S Dollar", "cash", 0.57, 1.0, 0.57)
    return conn


def get(conn, client, path):
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    app.today = lambda: TODAY
    status, ctype, body = app.handle("GET", path, LOCAL, b"")
    assert status == 200, body
    return json.loads(body)


# --- investments ---------------------------------------------------------------------------------------

def test_investments_with_no_snapshots(invest, client):
    d = get(invest, client, "/api/investments")
    assert d["total"] == 491.47 and d["holdings_total"] == 491.47
    assert d["history"] == [] and d["days"] == 0 and d["tracking_since"] is None
    assert d["changes"] == {"7d": None, "1m": None, "ytd": None, "all": None}
    assert [(a["name"], a["type"], a["group"], a["value"]) for a in d["accounts"]] == [
        ("Nova Robo", "Robo", "General", 0.0), ("Nova Self-directed", "Brokerage", "General", 491.47)]
    rows = {h["ticker"]: h for h in d["holdings"]}
    assert rows["CUR:USD"]["is_cash"] and not rows["NVDA"]["is_cash"] and rows["NVDA"]["pct"] == round(490.90 / 491.47 * 100, 2)
    alloc = {a["label"]: a["value"] for a in d["allocation"]["by_holding"]}
    assert alloc == {"NVDA": 490.90, "Cash": 0.57}
    types = {a["label"]: a["value"] for a in d["allocation"]["by_type"]}
    assert types == {"Brokerage": 491.47, "Robo": 0.0, "Retirement": 0.0, "Crypto": 0.0}
    assert [(b["label"], b["value"]) for b in d["breakdown"]] == [("General", 491.47), ("Retirement", 0.0),
                                                                  ("Crypto", 0.0)]


RESIDUAL = "Unpriced / pending"


def balance(conn, value):
    with conn:
        conn.execute("UPDATE accounts SET current_balance = ? WHERE account_id = 'inv'", (value,))


def test_a_balance_above_its_holdings_shows_the_residual_below_cash(invest, client):
    # TASK-11b (1): Plaid's balance (491.62) and the holdings (490.90 + 0.57 cash = 491.47) are snapshotted at
    # slightly different times. The account keeps Plaid's balance; the $0.15 is its own row, so allocation = total.
    balance(invest, 491.62)
    d = get(invest, client, "/api/investments")
    assert d["total"] == 491.62 and d["holdings_total"] == 491.47
    inv = next(a for a in d["accounts"] if a["id"] == "inv")
    assert inv["value"] == 491.62 and inv["residual"] == 0.15
    assert [(a["label"], a["value"]) for a in d["allocation"]["by_holding"]] == [
        ("NVDA", 490.90), ("Cash", 0.57), (RESIDUAL, 0.15)]
    row = d["allocation"]["by_holding"][-1]
    assert row["residual"] and not row["cash"] and row["name"]
    for k in ("by_holding", "by_type"):
        assert round(sum(a["value"] for a in d["allocation"][k]), 2) == d["total"] == 491.62
    tile = get(invest, client, "/api/overview")["investments"]
    assert tile["total"] == 491.62 and round(sum(x["value"] for x in tile["slices"]), 2) == 491.62
    assert tile["slices"][-1] == {"label": RESIDUAL, "value": 0.15, "cash": False, "residual": True}


def test_a_balance_below_its_holdings_is_a_negative_row_but_no_slice(invest, client):
    balance(invest, 491.40)
    d = get(invest, client, "/api/investments")
    assert [(a["label"], a["value"]) for a in d["allocation"]["by_holding"]] == [
        ("NVDA", 490.90), ("Cash", 0.57), (RESIDUAL, -0.07)]
    assert round(sum(a["value"] for a in d["allocation"]["by_holding"]), 2) == d["total"] == 491.40
    assert [x["label"] for x in get(invest, client, "/api/overview")["investments"]["slices"]] == ["NVDA", "Cash"]


@pytest.mark.parametrize("bal", [511.55, 523.73, 530.0])
def test_a_holdings_share_never_passes_100_when_the_balance_lags_its_prices(invest, client, bal):
    # hotfix 2026-10-05 (his screenshot: NVDA "102.3% of portfolio"): Nova's balance ($511.55) lagged the fresh
    # holdings price ($523.16 NVDA + $0.57 cash), and each row was divided by the balance. Shares are now of
    # priced_total: each account at the larger of its balance and its holdings.
    balance(invest, bal)
    with invest:
        invest.execute("UPDATE holdings SET institution_value = 523.16 WHERE security_id = 'sec_nvda'")
    manual.create(invest, {"kind": "investment", "name": "Work 401(k)", "institution": "Fidelity", "subtype": "401k",
                           "balance": 100.0, "balance_as_of": "2026-09-20"}, TODAY)
    from hermes_finance import reads
    want = round(max(bal, 523.73) + 100.0, 2)
    for d in (get(invest, client, "/api/investments"), portfolio.investments(invest, TODAY), reads.holdings(invest, TODAY)):
        assert d["priced_total"] == want and d["total"] == round(bal + 100.0, 2)  # the headline keeps the balances
        pcts = {h["ticker"]: h["pct"] for h in d["holdings"]}
        assert pcts["NVDA"] == round(523.16 / want * 100, 2) and all(p <= 100 for p in pcts.values())
        assert sum(pcts.values()) + 100.0 / want * 100 <= 100.01
    tile = get(invest, client, "/api/overview")["investments"]
    assert tile["priced_total"] == want and round(sum(x["value"] for x in tile["slices"]), 2) == want


@pytest.mark.parametrize("value", [491.47, 491.474, 491.466])
def test_a_residual_under_a_cent_is_hidden(invest, client, value):
    balance(invest, value)
    d = get(invest, client, "/api/investments")
    assert [a["label"] for a in d["allocation"]["by_holding"]] == ["NVDA", "Cash"]
    assert abs(next(a for a in d["accounts"] if a["id"] == "inv")["residual"]) < 0.01


def test_accounts_without_holdings_and_manual_accounts_have_no_residual(invest, client):
    manual.create(invest, {"kind": "investment", "name": "Work 401(k)", "institution": "Fidelity", "subtype": "401k",
                           "balance": 1000.0, "balance_as_of": "2026-09-20"}, TODAY)
    d = get(invest, client, "/api/investments")
    assert all(a["residual"] == 0.0 for a in d["accounts"])
    assert RESIDUAL not in [a["label"] for a in d["allocation"]["by_holding"]]


def test_investments_with_one_snapshot_day_has_no_changes(invest, client):
    hsnap(invest, "2026-09-26", "inv", "sec_nvda", 490.90)
    hsnap(invest, "2026-09-26", "inv", "sec_cash", 0.57)
    d = get(invest, client, "/api/investments")
    assert d["history"] == [{"date": "2026-09-26", "value": 491.47}] and d["days"] == 1
    assert d["changes"] == {"7d": None, "1m": None, "ytd": None, "all": None}
    assert d["tracking_since"] == "2026-09-26" and d["change_7d_from"] == "2026-10-03"


def test_investments_with_several_snapshot_days(invest, client):
    for day, v in (("2026-08-20", 400.0), ("2026-09-18", 450.0), ("2026-09-19", 460.0), ("2026-09-26", 490.90)):
        hsnap(invest, day, "inv", "sec_nvda", v)
    hsnap(invest, "2026-09-26", "inv", "sec_cash", 0.57)
    d = get(invest, client, "/api/investments")
    assert [(p["date"], p["value"]) for p in d["history"]] == [
        ("2026-08-20", 400.0), ("2026-09-18", 450.0), ("2026-09-19", 460.0), ("2026-09-26", 491.47)]
    c = d["changes"]
    assert c["7d"] == {"from": "2026-09-19", "to": "2026-09-26", "amount": 31.47, "pct": round(31.47 / 460 * 100, 2)}
    assert c["1m"]["from"] == "2026-08-20" and c["1m"]["amount"] == 91.47
    assert c["all"]["from"] == "2026-08-20" and c["ytd"] is None          # nothing on or before Jan 1


def test_changes_compare_the_same_accounts_so_a_new_account_is_not_a_gain(invest, client):
    hsnap(invest, "2026-09-18", "inv", "sec_nvda", 480.0)
    hsnap(invest, "2026-09-26", "inv", "sec_nvda", 490.0)
    manual.create(invest, {"kind": "investment", "name": "Work 401(k)", "institution": "Fidelity", "subtype": "401k",
                           "balance": 15000.0, "balance_as_of": "2026-09-25"}, TODAY)
    d = get(invest, client, "/api/investments")
    assert d["total"] == round(491.47 + 15000.0, 2)
    assert [(p["date"], p["value"]) for p in d["history"]] == [("2026-09-18", 480.0), ("2026-09-25", 15480.0),
                                                                ("2026-09-26", 15490.0)]
    assert d["changes"]["7d"]["amount"] == 10.0                         # not 15010
    retirement = next(b for b in d["breakdown"] if b["label"] == "Retirement")
    assert retirement["value"] == 15000.0 and retirement["count"] == 1
    m = next(a for a in d["accounts"] if a["manual"])
    assert (m["type"], m["group"], m["subtype_label"]) == ("Retirement", "Retirement", "401(k)")
    assert any(s["label"] == "Work 401(k)" and s["manual"] for s in d["allocation"]["by_holding"])


def test_removed_items_and_other_account_types_are_left_out(invest, client):
    bank(invest, "item_gone", "Gone", removed=True)
    acct(invest, "gone_inv", "item_gone", type_="investment", subtype="ira", current=999.0)
    hsnap(invest, "2026-09-26", "gone_inv", "sec_x", 999.0)
    acct(invest, "chk", "item_a", current=100.0)
    d = get(invest, client, "/api/investments")
    assert d["total"] == 491.47 and {a["name"] for a in d["accounts"]} == {"Nova Robo", "Nova Self-directed"}
    assert d["history"] == []


@pytest.mark.parametrize("subtype,name,expected", [
    ("brokerage", "Invest", "Brokerage"), ("brokerage", "Nova Robo", "Robo"), ("401k", "", "Retirement"),
    ("roth", "", "Retirement"), ("ira", "", "Retirement"), ("hsa", "", "HSA"), ("crypto exchange", "", "Crypto"),
    ("non-custodial wallet", "", "Crypto"), (None, "Automated Investing", "Robo")])
def test_plaid_investment_types(subtype, name, expected):
    assert portfolio.plaid_type(subtype, name) == expected


# --- debt -----------------------------------------------------------------------------------------------

@pytest.fixture
def debts(conn):
    bank(conn, "item_a", "Nova")
    bank(conn, "item_c", "Chase")
    acct(conn, "chk", "item_a", name="Nova Checking", mask="7385", current=2400.0)
    acct(conn, "cc", "item_c", name="Freedom", type_="credit", subtype="credit card", current=991.20, limit=6700.0,
         mask="3591")
    with conn:
        conn.execute("INSERT INTO liabilities (account_id, kind, item_id, minimum_payment_amount, next_payment_due_date, "
                     "interest_rate, updated_at) VALUES ('cc', 'credit', 'item_c', 35.0, '2026-09-28', 25.99, 'now')")
    for tid, day in (("p1", "2026-07-28"), ("p2", "2026-08-30"), ("p3", "2026-09-25")):
        tx(conn, day, 465.10, None, name="ZENITH ACH RTL", cat="Car payment", account_id="chk", tid=tid)
    return conn


def test_debt_payload(debts, client):
    loan = manual.create(debts, LOAN, TODAY)
    manual.match_loans(debts)
    paid = manual.create(debts, {**LOAN, "name": "Other loan", "subtype": "other", "original_amount": 2250.0,
                                 "balance": 0.0, "match_pattern": None, "monthly_payment": 100.0}, TODAY)
    d = get(debts, client, "/api/debt")
    assert d["total"] == round(991.20 + 5444.90, 2)
    groups = {g["key"]: g for g in d["groups"]}
    assert groups["cards"]["util"] == round(991.20 / 6700 * 100, 2) and groups["cards"]["count"] == 1
    assert groups["loan:auto"]["paid_pct"] == round((30947 - 5444.90) / 30947 * 100, 2)
    assert groups["loan:other"]["all_paid_off"] is True
    t = next(x for x in d["loans"] if x["id"] == loan["id"])
    assert (t["apr"], t["monthly_payment"], t["balance"]) == (None, 465.10, 5444.90)
    assert t["last_payment"]["date"] == "2026-09-25" and t["next_payment"] == "2026-10-25"
    assert t["payoff"] == {"payments_left": 12, "payoff_month": "2027-09", "method": "simple"}
    assert [(p["transaction_id"], p["applied"], p["included"]) for p in t["payments"]] == [
        ("p3", True, False), ("p2", False, True), ("p1", False, True)]
    assert [h["date"] for h in t["history"]] == ["2026-09-23", "2026-09-25"]
    assert next(x for x in d["loans"] if x["id"] == paid["id"])["paid_off"] is True
    assert d["monthly"] == {"total": round(465.10 + 35.0, 2), "loans": 465.10, "card_minimums": 35.0}
    assert [(u["kind"], u["date"]) for u in d["upcoming"]] == [("card", "2026-09-28"), ("loan", "2026-10-25")]
    assert d["stats"]["matched_this_year"] == 3 and d["stats"]["paid_off_count"] == 1
    assert d["stats"]["paid_off_total"] == 2250.0
    assert d["cards"][0]["apr"] == 25.99 and d["cards"][0]["min_payment"] == 35.0


def test_debt_payoff_with_apr_is_amortized(debts, client):
    manual.create(debts, {**LOAN, "interest_rate": 6.0, "match_pattern": None}, TODAY)
    (t,) = get(debts, client, "/api/debt")["loans"]
    assert t["payoff"]["method"] == "amortized" and t["payoff"]["payments_left"] == 14


def test_archived_and_plaid_loans(debts, client):
    acct(debts, "auto", "item_c", name="Auto Loan", type_="loan", subtype="auto", current=8000.0, mask="1111")
    arch = manual.create(debts, {"kind": "other_liability", "name": "IOU", "subtype": "Family", "balance": 500.0}, TODAY)
    manual.archive(debts, arch["id"], TODAY)
    d = get(debts, client, "/api/debt")
    assert d["total"] == round(991.20 + 8000.0, 2) and d["other"] == []
    (plaid_loan,) = d["loans"]
    assert plaid_loan["manual"] is False and plaid_loan["type_label"] == "Auto loan"


# --- net worth with manual accounts --------------------------------------------------------------------

@pytest.fixture
def worth(conn):
    bank(conn, "item_a", "Nova")
    acct(conn, "chk", "item_a", current=10000.0)
    acct(conn, "cc", "item_a", type_="credit", subtype="credit card", current=1000.0, limit=5000.0)
    snapshot(conn, "chk", "2026-09-24", 9900.0)
    snapshot(conn, "cc", "2026-09-24", 900.0)
    snapshot(conn, "chk", "2026-09-26", 10000.0)
    snapshot(conn, "cc", "2026-09-26", 1000.0)
    manual.create(conn, {"kind": "vehicle", "vehicle_make": "Zenith", "vehicle_model": "Mesa", "vehicle_year": 2019,
                         "balance": 20000.0, "balance_as_of": "2026-09-25"}, TODAY)
    manual.create(conn, {**LOAN, "match_pattern": None, "balance_as_of": "2026-09-20"}, TODAY)
    manual.create(conn, {"kind": "property", "name": "Home", "balance": 300000.0}, TODAY)
    gone = manual.create(conn, {"kind": "other_liability", "name": "IOU", "subtype": "Family", "balance": 777.0,
                                "balance_as_of": "2026-09-20"}, TODAY)
    manual.archive(conn, gone["id"], TODAY)
    with conn:   # archive() stamps the wall clock: pin it to the fixture's TODAY (noon UTC: Sep 26 in any US zone)
        conn.execute("UPDATE manual_accounts SET archived_at = '2026-09-26T12:00:00Z' WHERE id = ?", (gone["id"],))
    return conn


EXPECTED_NET = round(10000.0 - 1000.0 + 20000.0 + 300000.0 - 5910.0, 2)


def test_net_worth_adds_manual_assets_and_subtracts_manual_liabilities(worth):
    nw = insights.net_worth(worth, history=False)
    assert (nw["assets"], nw["debts"], nw["net"]) == (330000.0, 6910.0, EXPECTED_NET)
    assert nw["manual"] == {"assets": 320000.0, "debts": 5910.0, "count": 3}
    assert nw["groups"] == {"depository": 10000.0, "investment": 0.0, "credit": 1000.0, "loan": 0.0}   # Plaid part
    b = {g["key"]: (g["count"], g["signed"]) for g in nw["breakdown"]}
    assert b == {"cash": (1, 10000.0), "cards": (1, -1000.0), "loans": (1, -5910.0), "investments": (0, 0.0),
                 "property": (1, 300000.0), "vehicles": (1, 20000.0)}   # "other" groups only when used


def test_overview_and_accounts_include_manual(worth, client):
    ov = get(worth, client, "/api/overview?month=2026-09")
    custom = ov["net_worth"].pop("custom")
    assert ov["net_worth"] == {"assets": 330000.0, "debts": 6910.0, "net": EXPECTED_NET}
    # TASK-15: Custom's first selection is TASK-14's "Cash & debts": the checking minus every debt, the car and the home
    # left out
    assert {k: custom[k] for k in ("assets", "debts", "net")} == {"assets": 10000.0, "debts": 6910.0, "net": 3090.0}
    assert [(x["name"], x["kind"], x["group"], x["balance"], x["signed"], x["id"].startswith("manual:")) for x in custom["excluded"]] \
        == [("2019 Zenith Mesa", "vehicle", "vehicles", 20000.0, 20000.0, True), ("Home", "property", "property", 300000.0, 300000.0, True)]
    assert ov["debt"]["total"] == round(1000.0 + 5910.0, 2) and ov["debt"]["loan"]["name"] == "Zenith"
    a = get(worth, client, "/api/accounts")
    assert a["net_worth"]["net"] == EXPECTED_NET
    assert {m["name"] for m in a["manual"]} == {"2019 Zenith Mesa", "Zenith", "Home"}
    assert a["sparks"]["chk"] == [9900.0, 10000.0]


def test_history_includes_manual_items_from_the_first_snapshot_on(worth):
    h = {p["date"]: p for p in insights.net_worth(worth)["history"]}
    assert list(h) == ["2026-09-24", "2026-09-25", "2026-09-26"]         # the loan's 09-20 row adds no point
    # 09-24: loan (entered 09-20) and the IOU count, the car (09-25) and home (09-26) don't yet
    assert h["2026-09-24"]["net"] == round(9900.0 - 900.0 - 5910.0 - 777.0, 2)
    assert h["2026-09-25"]["net"] == round(9900.0 - 900.0 - 5910.0 - 777.0 + 20000.0, 2)
    # 09-26: the IOU was archived today, the home counts from today
    assert h["2026-09-26"]["net"] == EXPECTED_NET


def test_history_follows_loan_payments(debts):
    manual.create(debts, LOAN, TODAY)
    manual.match_loans(debts)
    snapshot(debts, "chk", "2026-09-24", 2400.0)
    snapshot(debts, "chk", "2026-09-26", 2400.0)
    pts = {p["date"]: p["debts"] for p in insights.net_worth(debts)["history"]}
    assert pts == {"2026-09-24": 5910.0, "2026-09-25": 5444.90, "2026-09-26": 5444.90}


def test_archiving_an_account_is_not_a_loss_in_the_change_chips(invest, client):
    hsnap(invest, "2026-09-18", "inv", "sec_nvda", 1000.0)
    hsnap(invest, "2026-09-26", "inv", "sec_nvda", 1030.0)
    k = manual.create(invest, {"kind": "investment", "name": "Work 401(k)", "institution": "Fidelity", "subtype": "401k",
                               "balance": 50000.0, "balance_as_of": "2026-09-18"}, TODAY)
    assert get(invest, client, "/api/investments")["changes"]["7d"]["amount"] == 30.0
    manual.archive(invest, k["id"], TODAY)
    d = get(invest, client, "/api/investments")
    assert d["changes"]["7d"]["amount"] == 30.0 and d["changes"]["all"]["amount"] == 30.0
