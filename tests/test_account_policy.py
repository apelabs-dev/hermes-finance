"""The account policy: one durable denylist applied to every read that aggregates accounts or transactions."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from conftest import account, add_item, page, txn
from factory import acct, bank, snapshot as balance_snapshot, tx
from hermes_finance import (api, cli, config, db, goals, insights, manual, portfolio, reads, report, sync, syncnow,
                            transit)
from hermes_finance.sync import ItemSyncResult, Phase, sync_all

FIXTURES = Path(__file__).parent / "fixtures"

ACCOUNTS = [
    account("acc_a", "Brokerage A", "1111", "investment", "brokerage", 1000.0, None),
    account("acc_b", "Brokerage B", "2222", "investment", "brokerage", 3000.0, None),
]


def holdings_route(accounts=None, value_b=3000.0):
    return {
        "accounts": accounts or ACCOUNTS,
        "holdings": [
            {"account_id": "acc_a", "security_id": "sec_a", "quantity": 10, "institution_price": 100.0,
             "institution_value": 1000.0, "cost_basis": 800.0, "iso_currency_code": "USD"},
            {"account_id": "acc_b", "security_id": "sec_b", "quantity": 10,
             "institution_price": value_b / 10, "institution_value": value_b, "cost_basis": 2000.0,
             "iso_currency_code": "USD"},
        ],
        "securities": [
            {"security_id": "sec_a", "ticker_symbol": "AAA", "name": "Alpha Fund", "type": "etf"},
            {"security_id": "sec_b", "ticker_symbol": "BBB", "name": "Beta Fund", "type": "etf"},
        ],
    }


def all_transactions(today):
    day = today.isoformat() if hasattr(today, "isoformat") else today
    return [
        txn("a_spend", 10.0, "COFFEE A", "Coffee A", day, account_id="acc_a"),
        txn("b_spend", 20.0, "COFFEE B", "Coffee B", day, account_id="acc_b"),
        txn("a_income", -200.0, "PAY A", "Pay A", day, pfc=("INCOME", "INCOME_WAGES"), account_id="acc_a"),
        txn("b_income", -500.0, "PAY B", "Pay B", day, pfc=("INCOME", "INCOME_WAGES"), account_id="acc_b"),
    ]


def sync_two(conn, client, fake, added=None, accounts=None, value_b=3000.0):
    accounts = accounts or ACCOUNTS
    fake.on("/item/get", {"item": {"products": ["transactions", "investments"]}})
    fake.on("/transactions/sync", page(added=added if added is not None else all_transactions(date.today()),
                                       accounts=accounts))
    fake.on("/accounts/get", {"accounts": accounts})
    fake.on("/investments/holdings/get", holdings_route(accounts, value_b))
    (result,) = sync_all(conn, client)
    assert result.ok


def snapshot(conn, today):
    filters = reads.parse_filters(conn, today, default_period="all")
    flow = reads.cashflow(conn, today, months=6)
    month = next(m for m in flow["months"] if m["month"] == insights.month_of(today))
    nw = reads.networth(conn, today)
    custom = reads.networth(conn, today, custom=True)
    holdings = reads.holdings(conn, today)
    return {
        "spend": reads.spend(conn, filters, today)["total"],
        "income": month["income"],
        "flow_spending": month["spending"],
        "assets": nw["assets"],
        "custom_assets": custom["assets"],
        "holdings": holdings["total"],
        "holdings_total": holdings["holdings_total"],
        "accounts": [a["id"] for a in holdings["accounts"]],
        "tickers": [h["ticker"] for h in holdings["holdings"]],
    }


def test_excluded_account_contributes_nothing_and_included_one_is_whole(conn, client, fake):
    today = date.today()
    insights.save_networth_excluded(conn, [])
    add_item(conn, products=("transactions", "investments"))
    sync_two(conn, client, fake)

    before = snapshot(conn, today)
    assert before == {"spend": 30.0, "income": 700.0, "flow_spending": 30.0, "assets": 4000.0,
                      "custom_assets": 4000.0, "holdings": 4000.0, "holdings_total": 4000.0,
                      "accounts": ["acc_a", "acc_b"], "tickers": ["BBB", "AAA"]}

    insights.save_excluded_accounts(conn, ["acc_b"])
    after = snapshot(conn, today)
    assert after == {"spend": 10.0, "income": 200.0, "flow_spending": 10.0, "assets": 1000.0,
                     "custom_assets": 1000.0, "holdings": 1000.0, "holdings_total": 1000.0,
                     "accounts": ["acc_a"], "tickers": ["AAA"]}

    nw = reads.networth(conn, today)
    assert nw["policy"]["excluded_count"] == 1
    assert nw["policy"]["unresolved_count"] == 0
    assert nw["policy"]["coverage"] == "complete"
    assert nw["policy"]["accounts"] == [{"name": "Brokerage B", "mask": "2222", "institution": "Chase",
                                         "kind": "investment", "group": "investments"}]
    assert nw["custom"]["excluded"] == []

    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
    assert json.loads(conn.execute("SELECT value FROM settings WHERE key = ?",
                                   (db.ACCOUNT_POLICY_KEY,)).fetchone()[0]) == ["acc_b"]


def test_repeated_sync_and_an_account_refresh_do_not_reverse_the_policy(conn, client, fake):
    today = date.today()
    insights.save_networth_excluded(conn, [])
    add_item(conn, products=("transactions", "investments"))
    sync_two(conn, client, fake)
    insights.save_excluded_accounts(conn, ["acc_b"])
    first = snapshot(conn, today)

    changed = [account("acc_a", "Brokerage A", "1111", "investment", "brokerage", 1000.0, None),
               account("acc_b", "Brokerage B", "2222", "investment", "brokerage", 4000.0, None)]
    sync_two(conn, client, fake, added=[txn("b_new", 25.0, "COFFEE B2", "Coffee B2", today.isoformat(),
                                            account_id="acc_b")], accounts=changed, value_b=4000.0)
    sync.upsert_accounts(conn, "item_a", changed)
    second = snapshot(conn, today)

    assert second == first
    assert json.loads(conn.execute("SELECT value FROM settings WHERE key = ?",
                                   (db.ACCOUNT_POLICY_KEY,)).fetchone()[0]) == ["acc_b"]
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = 'acc_b'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM holdings WHERE account_id = 'acc_b'").fetchone()[0] == 1
    assert conn.execute("SELECT current_balance FROM accounts WHERE account_id = 'acc_b'").fetchone()[0] == 4000.0
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE account_id = 'acc_b'").fetchone()[0] == 0


def test_custom_net_worth_keeps_its_own_selection_separate(conn, client, fake):
    today = date.today()
    add_item(conn, products=("transactions", "investments"))
    sync_two(conn, client, fake)
    insights.save_excluded_accounts(conn, ["acc_b"])
    insights.save_networth_excluded(conn, ["acc_a"])

    nw = reads.networth(conn, today, custom=True)
    assert nw["mode"] == "custom"
    assert nw["assets"] == 0.0 and nw["net"] == 0.0
    assert nw["custom"]["excluded"] == [{"id": "acc_a", "source": "plaid", "name": "Brokerage A", "mask": "1111",
                                         "institution": "Chase", "kind": "investment", "group": "investments",
                                         "signed": 1000.0, "balance": 1000.0}]
    assert insights.networth_excluded(conn) == ["acc_a"]
    assert insights.excluded_accounts(conn) == ["acc_b"]

    all_view = reads.networth(conn, today)
    assert all_view["assets"] == 1000.0
    assert [a["id"] for a in all_view["custom"]["excluded"]] == ["acc_a"]


def test_an_unresolved_id_is_kept_and_reported(conn):
    insights.save_excluded_accounts(conn, ["no-such-account"])

    assert insights.excluded_accounts(conn) == ["no-such-account"]
    status = report.status(conn)["account_policy"]
    assert status == {"configured_count": 1, "resolved_count": 0, "unresolved_count": 1,
                      "coverage": "incomplete", "accounts": []}
    policy = insights.net_worth(conn, history=False)["policy"]
    assert policy["excluded_count"] == 1 and policy["unresolved_count"] == 1
    assert policy["coverage"] == "incomplete"


def test_a_null_or_empty_account_id_is_never_hidden(conn, client, fake):
    today = date.today()
    add_item(conn, products=("transactions", "investments"))
    sync_two(conn, client, fake)
    insights.save_excluded_accounts(conn, ["acc_b"])

    with conn:
        conn.execute(
            """INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, pfc_primary,
                   pfc_detailed, first_seen_at, updated_at)
               VALUES ('empty_account', '', 'item_a', ?, 'NO ACCOUNT', 5.0, 'FOOD_AND_DRINK',
                       'FOOD_AND_DRINK_COFFEE', ?, ?)""", (today.isoformat(), db.now(), db.now()))
    assert conn.execute("SELECT COUNT(*) FROM v_transactions WHERE transaction_id = 'empty_account'").fetchone()[0] == 1
    filters = reads.parse_filters(conn, today, default_period="all")
    assert reads.spend(conn, filters, today)["total"] == 15.0

    null_row = conn.execute(f"SELECT {db.account_policy_sql('x')} FROM (SELECT NULL AS account_id) x").fetchone()[0]
    assert null_row == 1

    with conn:  # a hand-corrupted setting reads as empty, never as "hide everything"
        conn.execute("UPDATE settings SET value = 'not json' WHERE key = ?", (db.ACCOUNT_POLICY_KEY,))
    assert insights.excluded_accounts(conn) == []
    assert reads.spend(conn, filters, today)["total"] == 35.0


def test_a_manual_account_left_out_by_the_policy_is_kept_and_not_counted(conn):
    from hermes_finance import manual
    with conn:
        conn.execute("INSERT INTO manual_accounts (id, kind, name, institution, balance, balance_as_of, created_at, "
                     "updated_at) VALUES (1, 'loan', 'Garage loan', 'Credit Union', 500.0, '2026-08-01', "
                     "'2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')")
        conn.execute("INSERT INTO manual_accounts (id, kind, name, institution, balance, balance_as_of, created_at, "
                     "updated_at) VALUES (2, 'investment', 'Side brokerage', 'Credit Union', 250.0, '2026-08-01', "
                     "'2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')")
    today = date.today()
    insights.save_networth_excluded(conn, [])   # Custom's own selection, saved so it isn't the Cash & debts preset
    insights.save_excluded_accounts(conn, ["manual:1"])

    summary = manual.summary(conn, today)
    assert summary["debts"] == 0.0 and summary["assets"] == 250.0 and summary["net"] == 250.0
    assert summary["excluded_count"] == 1
    assert sorted((a["id"], a["excluded"]) for a in summary["accounts"]) == [(1, True), (2, False)]

    nw = reads.networth(conn, today)
    assert (nw["assets"], nw["debts"], nw["net"]) == (250.0, 0.0, 250.0)
    rows = {a["nw_id"]: a for g in nw["groups"] for a in g["accounts"]}
    assert rows["manual:1"]["counted"] is False and rows["manual:1"]["excluded"] is True
    assert rows["manual:2"]["counted"] is True and rows["manual:2"]["excluded"] is False
    assert conn.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0] == 2


def test_api_saves_unresolved_ids_and_validates_the_type(conn):
    add_item(conn, products=("transactions",))
    with conn:
        conn.execute("INSERT INTO accounts (account_id, item_id, name, type) "
                     "VALUES ('acc_a', 'item_a', 'Checking', 'depository')")
    out = api.save_settings(conn, {"excluded_accounts": ["acc_a", "ghost", "acc_a"]})
    assert out["excluded_accounts"] == ["acc_a", "ghost"]
    assert out["unresolved"] == ["ghost"] and out["coverage"] == "incomplete"
    assert insights.excluded_accounts(conn) == ["acc_a", "ghost"]

    with pytest.raises(api.ApiError) as e:
        api.save_settings(conn, {"excluded_accounts": "acc_a"})
    assert e.value.field == "excluded_accounts"
    with pytest.raises(api.ApiError) as e:
        api.save_settings(conn, {"excluded_accounts": ["acc_a", ""]})
    assert e.value.field == "excluded_accounts"
    assert insights.excluded_accounts(conn) == ["acc_a", "ghost"]


def test_cli_import_is_scriptable_and_malformed_input_saves_nothing(conn, data_dir, tmp_path, capsys):
    add_item(conn, products=("transactions",))
    with conn:
        conn.execute("INSERT INTO accounts (account_id, item_id, name, type) "
                     "VALUES ('acc_a', 'item_a', 'Checking', 'depository')")
    good = tmp_path / "policy.json"
    good.write_text(json.dumps(["acc_a", "ghost"]))
    assert cli.main(["account-policy", "--import", str(good), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"excluded_accounts": ["acc_a", "ghost"], "unresolved": ["ghost"], "count": 2,
                   "resolved_count": 1, "coverage": "incomplete"}

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"ids": ["acc_a"]}))
    assert cli.main(["account-policy", "--import", str(bad), "--json"]) == 2
    assert insights.excluded_accounts(conn) == ["acc_a", "ghost"]

    assert cli.main(["account-policy", "--clear", "--json"]) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["count"] == 0 and cleared["excluded_accounts"] == []
    assert insights.excluded_accounts(conn) == []


def test_v19_upgrade_keeps_every_row_and_the_recreated_view_filters(data_dir):
    path = config.db_path()
    config.ensure_data_dir(path.parent)
    raw = sqlite3.connect(path)
    raw.executescript((FIXTURES / "schema_v19.sql").read_text())
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 19
    ts = "2026-08-01T00:00:00Z"
    raw.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, created_at) "
                "VALUES ('item_a', 'ins_1', 'Chase', 'access-production-x', 'active', ?)", (ts,))
    for aid, name, mask in (("acc_a", "Brokerage A", "1111"), ("acc_b", "Brokerage B", "2222")):
        raw.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                    "iso_currency, updated_at) VALUES (?, 'item_a', ?, ?, 'investment', 'brokerage', 1000, 'USD', ?)",
                    (aid, name, mask, ts))
    for tid, aid in (("t_a", "acc_a"), ("t_b", "acc_b")):
        raw.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount, "
                    "pfc_primary, pfc_detailed, first_seen_at, updated_at) "
                    "VALUES (?, ?, 'item_a', '2026-08-02', 'X', 10, 'FOOD_AND_DRINK', 'FOOD_AND_DRINK_COFFEE', ?, ?)",
                    (tid, aid, ts, ts))
    for aid, sec, ticker in (("acc_a", "sec_a", "AAA"), ("acc_b", "sec_b", "BBB")):
        raw.execute("INSERT INTO holdings (account_id, security_id, item_id, ticker_symbol, security_type, quantity, "
                    "institution_price, institution_value, updated_at) "
                    "VALUES (?, ?, 'item_a', ?, 'etf', 10, 100, 1000, ?)", (aid, sec, ticker, ts))
    raw.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (db.ACCOUNT_POLICY_KEY, json.dumps(["acc_b"]), ts))
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 20
    view = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'v_transactions'").fetchone()[0]
    assert "excluded_accounts" in view
    assert [tuple(r) for r in conn.execute(
        "SELECT account_id, name, mask FROM accounts ORDER BY account_id")] == [
        ("acc_a", "Brokerage A", "1111"), ("acc_b", "Brokerage B", "2222")]
    assert [tuple(r) for r in conn.execute(
        "SELECT transaction_id, account_id FROM transactions ORDER BY transaction_id")] == [
        ("t_a", "acc_a"), ("t_b", "acc_b")]
    assert [tuple(r) for r in conn.execute(
        "SELECT account_id, ticker_symbol, institution_value FROM holdings ORDER BY account_id")] == [
        ("acc_a", "AAA", 1000.0), ("acc_b", "BBB", 1000.0)]
    assert [r[0] for r in conn.execute("SELECT transaction_id FROM v_transactions ORDER BY transaction_id")] == ["t_a"]
    assert [tuple(r) for r in conn.execute(
        "SELECT account_id, security_id FROM holding_snapshots ORDER BY account_id")] == []
    conn.close()


# --- the rework round: one policy, every consumer -----------------------------------------------------

def transfer_scene(conn):
    """A checking account paying a credit card, with a snapshot of both on the day after the payment."""
    bank(conn, "item_harbor", "Harbor")
    bank(conn, "item_chase", "Chase")
    acct(conn, "acc_chk", "item_harbor", "Harbor Checking", current=1200.0, mask="4410")
    acct(conn, "acc_cc", "item_chase", "CREDIT CARD", type_="credit", subtype="credit card", current=450.0,
         limit=5000.0, mask="1234")
    tx(conn, "2026-09-27", 450.00, name="CHASE CREDIT CRD AUTOPAY", cat="Card payment",
       account_id="acc_chk", item_id="item_harbor", tid="chk-pay")
    balance_snapshot(conn, "acc_chk", "2026-09-28", 1200.0)
    balance_snapshot(conn, "acc_cc", "2026-09-28", 450.0)


def test_R1_an_excluded_source_account_never_moves_net_worth_through_transit(conn):
    """The transit adjustment uses only included rows: an excluded checking account's payment to an included card
    cannot be added back to the card (the excluded-destination control keeps working too)."""
    today = date(2026, 9, 28)
    transfer_scene(conn)

    ordinary = insights.net_worth(conn, today=today)
    assert (ordinary["assets"], ordinary["debts"], ordinary["net"]) == (1200.0, 0.0, 1200.0)
    assert [e["transaction_id"] for e in ordinary["in_transit"]] == ["chk-pay"]

    insights.save_excluded_accounts(conn, ["acc_chk"])
    nw = insights.net_worth(conn, today=today)
    assert (nw["assets"], nw["debts"], nw["net"]) == (0.0, 450.0, -450.0)
    assert nw["in_transit"] == []
    assert (nw["history"][-1]["assets"], nw["history"][-1]["debts"], nw["history"][-1]["net"]) == (0.0, 450.0, -450.0)
    assert transit.entries(conn, today, {"acc_chk"}) == []
    overview = api.overview(conn, {}, today)
    assert (overview["net_worth"]["assets"], overview["net_worth"]["debts"]) == (0.0, 450.0)
    assert overview["debt"]["total"] == 450.0 and overview["debt"]["in_transit"] == []

    insights.save_excluded_accounts(conn, ["acc_cc"])   # excluded destination: already correct, keeps passing
    nw = insights.net_worth(conn, today=today)
    assert (nw["assets"], nw["debts"], nw["net"]) == (1200.0, 0.0, 1200.0)
    assert nw["in_transit"] == []


def investment_universe(conn):
    bank(conn, "item_a", "Chase")
    acct(conn, "acc_a", "item_a", "Brokerage A", type_="investment", subtype="brokerage", current=1000.0, mask="1111")
    acct(conn, "acc_b", "item_a", "Brokerage B", type_="investment", subtype="brokerage", current=3000.0, mask="2222")


def test_R2_saving_custom_never_drops_a_globally_excluded_account(conn):
    """The global policy is an effective read filter, not a rewrite of Custom's own stored preference."""
    investment_universe(conn)
    insights.save_networth_excluded(conn, ["acc_b"])
    insights.save_excluded_accounts(conn, ["acc_b"])

    rows = {a["id"]: a for a in insights.networth_accounts(conn)}
    assert rows["acc_b"]["excluded"] is True and rows["acc_b"]["counted"] is False
    assert rows["acc_a"]["excluded"] is False and rows["acc_a"]["counted"] is True

    out = api.save_settings(conn, {"networth_excluded": ["acc_b"]})
    assert out["networth_excluded"] == ["acc_b"]      # the unchanged Custom list survives the active policy

    insights.save_excluded_accounts(conn, [])
    nw = insights.net_worth(conn, history=False)
    assert (nw["assets"], nw["custom"]["assets"], nw["custom"]["net"]) == (4000.0, 1000.0, 1000.0)
    assert [a["id"] for a in nw["custom"]["excluded"]] == ["acc_b"]


def test_R2_cli_custom_write_accepts_a_globally_excluded_account(conn, capsys):
    investment_universe(conn)
    insights.save_networth_excluded(conn, ["acc_b"])
    insights.save_excluded_accounts(conn, ["acc_b"])

    assert cli.main(["networth-custom", "--exclude", "acc_b", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    row = next(a for a in out["accounts"] if a["id"] == "acc_b")
    assert row["counted"] is False and row["excluded"] is True
    assert insights.networth_excluded(conn) == ["acc_b"]   # the CLI write kept the independent preference
    insights.save_excluded_accounts(conn, [])
    assert insights.net_worth(conn, history=False)["custom"]["assets"] == 1000.0


def test_R2_the_first_custom_read_keeps_the_whole_cash_and_debts_preset(conn):
    """A globally excluded investment still belongs in the preset's universe, so clearing the policy cannot silently
    change what Custom counts."""
    bank(conn)
    acct(conn, "acc_cash", "item_a", "Checking", current=500.0, mask="1111")
    acct(conn, "acc_inv", "item_a", "Brokerage", type_="investment", subtype="brokerage", current=3000.0, mask="2222")
    insights.save_excluded_accounts(conn, ["acc_inv"])

    out = api.settings(conn)      # first read: the Cash & debts preset is saved
    assert out["networth_excluded"] == ["acc_inv"]

    insights.save_excluded_accounts(conn, [])
    nw = insights.net_worth(conn, history=False)
    assert nw["assets"] == 3500.0                       # All counts the investment again
    assert nw["custom"]["assets"] == 500.0              # Custom keeps the saved preset
    assert [a["id"] for a in nw["custom"]["excluded"]] == ["acc_inv"]


def test_R3_the_manual_payload_marks_policy_rows_and_its_net_worth_total_counts_only_included(conn):
    with conn:
        conn.execute("INSERT INTO manual_accounts (id, kind, name, institution, balance, balance_as_of, created_at, "
                     "updated_at) VALUES (1, 'bank', 'Excluded savings', 'Credit Union', 1000.0, '2026-08-01', "
                     "'2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')")
        conn.execute("INSERT INTO manual_accounts (id, kind, name, institution, balance, balance_as_of, created_at, "
                     "updated_at) VALUES (2, 'bank', 'Counted savings', 'Credit Union', 100.0, '2026-08-01', "
                     "'2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')")
    insights.save_networth_excluded(conn, [])
    insights.save_excluded_accounts(conn, ["manual:1"])

    payload = api.accounts(conn)
    rows = {m["id"]: m for m in payload["manual"]}
    assert (rows[1]["excluded"], rows[2]["excluded"]) == (True, False)
    assert payload["net_worth"]["manual"] == {"assets": 100.0, "debts": 0.0, "count": 1}
    assert payload["net_worth"]["custom"]["assets"] == 100.0


def test_R4_an_excluded_card_raises_no_payment_due_alert(conn):
    from factory import basic
    today = date(2026, 9, 28)
    basic(conn)                    # item_a: checking + acc_cc (the card)
    acct(conn, "acc_cc2", "item_a", "SAPPHIRE", type_="credit", subtype="credit card", current=500.0,
         limit=5000.0, mask="9999")
    with conn:
        conn.executemany(
            "INSERT INTO liabilities (account_id, kind, item_id, minimum_payment_amount, next_payment_due_date, "
            "updated_at) VALUES (?, 'credit', 'item_a', ?, ?, ?)",
            [("acc_cc", 25.0, today.isoformat(), db.now()), ("acc_cc2", 10.0, today.isoformat(), db.now())])
    insights.save_excluded_accounts(conn, ["acc_cc"])

    overview = api.overview(conn, {}, today)
    assert [c["account_id"] for c in overview["cards_due"]] == ["acc_cc2"]
    assert overview["debt"]["total"] == 500.0            # the included card only


def test_R5_sync_summary_never_observes_an_excluded_balance_move(conn):
    bank(conn)
    acct(conn, "acc_a", "item_a", "Included", current=100.0, mask="1111")
    acct(conn, "acc_b", "item_a", "Excluded", current=3000.0, mask="2222")
    insights.save_excluded_accounts(conn, ["acc_b"])     # the policy exists before the run, so both snapshots carry it
    before = syncnow.balances(conn)
    with conn:
        conn.execute("UPDATE accounts SET current_balance = 200 WHERE account_id = 'acc_a'")
        conn.execute("UPDATE accounts SET current_balance = 4000 WHERE account_id = 'acc_b'")
    after = syncnow.balances(conn)
    result = ItemSyncResult(item_id="item_a", institution_name="Chase", added=1)
    result.phases["transactions"] = Phase(attempted=True, ok=True)

    c = syncnow.changes(before, after, [result], insights.excluded_accounts(conn))
    assert [m["account_id"] for m in c["balances"]] == ["acc_a"]
    assert "••2222" not in c["text"] and "Chase ••1111 $100 → $200" in c["text"]
    # the stored rows are untouched: only the observable summary filters
    assert conn.execute("SELECT current_balance FROM accounts WHERE account_id = 'acc_b'").fetchone()[0] == 4000.0

    direct = syncnow.changes(before, after, [result])     # no policy argument: the same summary, from the rows themselves
    assert [m["account_id"] for m in direct["balances"]] == ["acc_a"]
    assert "••2222" not in direct["text"]


def test_R5_a_goal_cannot_follow_a_policy_excluded_account(conn):
    # R3/R5's rule applies to the Goals surface too: an account every other read hides must not be offered as a goal,
    # because a goal's growth reads that account's raw balance and transactions.
    bank(conn)
    acct(conn, "acc_goal", "item_a", "Savings", subtype="savings", current=500.0, mask="3333")
    acct(conn, "acc_other", "item_a", "Savings 2", subtype="savings", current=50.0, mask="4444")
    insights.save_excluded_accounts(conn, ["acc_goal"])

    assert [a["id"] for a in goals.accounts(conn)] == ["acc_other"]
    listed = {a["id"] for a in goals.payload(conn, date(2026, 9, 28))["accounts"]}
    assert listed == {"acc_other"}
    assert not [s for s in goals.payload(conn, date(2026, 9, 28))["suggestions"] if s["id"] == "acc_goal"]


TODAY = date(2026, 9, 28)


def _excluded_goal_scene(conn):
    """A whole-account save goal made before the policy excludes its account."""
    bank(conn)
    acct(conn, "acc_goal", "item_a", "Savings", subtype="savings", current=500.0, mask="3333")
    acct(conn, "acc_other", "item_a", "Savings 2", subtype="savings", current=50.0, mask="4444")
    return goals.create(conn, {"kind": "save", "name": "Rainy day", "target": 1000, "track": "account",
                               "account_id": "acc_goal"}, TODAY)


def _payload_goal(conn, goal_id):
    return next(g for g in goals.payload(conn, TODAY)["goals"] if g["id"] == goal_id)


def test_a_goal_on_a_policy_excluded_account_is_excluded_not_missing(conn):
    # The account policy is a reversible read filter, not a deletion: the goal must say so and present no figure.
    g = _excluded_goal_scene(conn)
    insights.save_excluded_accounts(conn, ["acc_goal"])

    x = _payload_goal(conn, g["id"])
    assert x["excluded_account"] is True
    assert x["missing_account"] is False
    assert x["progress"] is None and x["pct"] is None and x["left"] is None
    assert x["short"] is None and x["account"] is None and x["done"] is False
    assert x["pace"] is None and x["payoff"] is None and x["entries"] == []

    p = goals.payload(conn, TODAY)
    assert p["totals"]["count"] == 1                  # every goal is still counted
    assert p["totals"]["save_count"] == 0             # but an unknown progress is never added as 0
    assert p["totals"]["saving_target"] == 0.0
    assert p["totals"]["saved"] == 0.0
    assert p["totals"]["pct"] is None
    assert goals.tile(conn, TODAY)["top"] is None


def test_goals_text_names_the_policy_instead_of_a_missing_account(conn):
    _excluded_goal_scene(conn)
    insights.save_excluded_accounts(conn, ["acc_goal"])
    text = goals.text(goals.payload(conn, TODAY))
    line = next(x for x in text.splitlines() if x.startswith("#") and "Rainy day" in x)
    assert "excluded by the account policy" in line
    assert "isn't there any more" not in text
    assert "$" not in line


def test_an_earmark_edit_on_an_excluded_account_names_the_policy(conn):
    bank(conn)
    acct(conn, "acc_goal", "item_a", "Savings", subtype="savings", current=500.0, mask="3333")
    g = goals.create(conn, {"kind": "save", "name": "Rainy day", "target": 1000, "track": "earmark",
                            "account_id": "acc_goal", "earmark": 200}, TODAY)
    insights.save_excluded_accounts(conn, ["acc_goal"])
    with pytest.raises(goals.GoalError) as raised:
        goals.edit(conn, g["id"], {"earmark": 250}, TODAY)
    assert "account policy" in str(raised.value)
    assert "isn't there any more" not in str(raised.value)
    assert raised.value.field == "earmark"


def test_clearing_the_policy_restores_every_goal_figure(conn):
    g = _excluded_goal_scene(conn)
    before = _payload_goal(conn, g["id"])
    captured = (before["progress"], before["pct"], before["left"], before["done"], before["account"]["label"])
    insights.save_excluded_accounts(conn, ["acc_goal"])
    assert _payload_goal(conn, g["id"])["excluded_account"] is True

    insights.save_excluded_accounts(conn, [])
    after = _payload_goal(conn, g["id"])
    assert after["excluded_account"] is False
    assert (after["progress"], after["pct"], after["left"], after["done"], after["account"]["label"]) == captured


def test_a_goal_on_an_account_net_worth_never_knew_is_still_missing(conn):
    bank(conn)
    acct(conn, "acc_other", "item_a", "Savings 2", subtype="savings", current=50.0, mask="4444")
    with conn:
        conn.execute("INSERT INTO goals (kind, name, target, track, account_id, created_at, updated_at, icon, colour) "
                     "VALUES ('save', 'Ghost', 1000, 'account', 'acc_gone', ?, ?, 'piggy-bank', '#4fd1a5')",
                     (db.now(), db.now()))
    x = next(g for g in goals.payload(conn, TODAY)["goals"] if g["name"] == "Ghost")
    assert x["missing_account"] is True
    assert x["excluded_account"] is False


@pytest.mark.parametrize("raw, ids, shape", [
    (json.dumps("acc_b"), [], "invalid"),                    # a valid JSON scalar is not a policy
    (json.dumps({"ids": ["acc_b"]}), [], "invalid"),         # nor an object
    (json.dumps(["acc_a", 5]), ["acc_a"], "partial"),        # a mixed array: the string ids apply in both
    ("not json", [], "invalid"),
    (json.dumps(["acc_b"]), ["acc_b"], "valid"),             # the exact-id control
])
def test_R6_sql_python_and_status_agree_on_every_policy_shape(conn, raw, ids, shape):
    bank(conn)
    acct(conn, "acc_a", "item_a", "Checking A", current=1000.0, mask="1111")
    acct(conn, "acc_b", "item_a", "Checking B", current=2000.0, mask="2222")
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                     (db.ACCOUNT_POLICY_KEY, raw, db.now()))

    seen = insights.excluded_accounts(conn)
    shown = {r[0] for r in conn.execute(f"SELECT a.account_id FROM accounts a WHERE {db.account_policy_sql('a')}")}
    hidden = {"acc_a", "acc_b"} - shown
    status = insights.account_policy_status(conn)

    assert seen == ids
    assert hidden == set(ids)                       # SQL hides exactly the ids Python resolves
    assert status["configured_count"] == len(ids)
    if shape == "valid":
        assert status["coverage"] == "complete" and "valid" not in status
    else:
        assert status["valid"] is False and status["shape"] == shape and status["coverage"] == "invalid"


def test_R7_a_direct_history_call_reads_the_saved_policy(conn):
    bank(conn)
    acct(conn, "acc_a", "item_a", "Checking A", current=1000.0, mask="1111")
    acct(conn, "acc_b", "item_a", "Checking B", current=4000.0, mask="2222")
    balance_snapshot(conn, "acc_a", "2026-09-28", 1000.0)
    balance_snapshot(conn, "acc_b", "2026-09-28", 4000.0)
    insights.save_excluded_accounts(conn, ["acc_b"])

    direct = insights.net_worth_history(conn)                 # no explicit policy: it reads the saved one
    wrapper = insights.net_worth(conn, history=True)
    assert direct["history"][-1]["assets"] == wrapper["history"][-1]["assets"] == 1000.0
    explicit = insights.net_worth_history(conn, policy_excluded=[])
    assert explicit["history"][-1]["assets"] == 5000.0        # the explicit argument still wins


# --- round 2: manual payment matching obeys the policy on both sides ---------------------------------

PAYMENT_TODAY = date(2026, 11, 5)


def payment_scene(conn):
    """An included manual auto loan paid from a checking account that a test may exclude."""
    bank(conn)
    acct(conn, "acc_source", "item_a", "Checking", current=1200.0, mask="1234")
    loan = manual.create(conn, dict(kind="loan", subtype="auto", name="Garage loan", institution="Synthetic CU",
                                    balance=500.0, original_amount=500.0, monthly_payment=100.0,
                                    balance_as_of="2026-08-01", match_pattern="GARAGE PAYMENT"), PAYMENT_TODAY)
    tx(conn, "2026-10-20", 100.0, name="GARAGE PAYMENT", cat="Car payment", account_id="acc_source",
       tid="loan-payment")
    return loan


def test_R8_an_excluded_source_cannot_show_or_apply_a_manual_payment(conn):
    loan = payment_scene(conn)
    insights.save_networth_excluded(conn, [])
    insights.save_excluded_accounts(conn, ["acc_source"])

    api_preview = api.match_preview(conn, {"kind": "loan", "id": str(loan["id"]), "pattern": "GARAGE PAYMENT"},
                                    PAYMENT_TODAY)
    assert api_preview["count"] == 0 and api_preview["matches"] == [] and api_preview["last"] is None
    assert api_preview["typical"] is None
    assert api_preview["preview"] == {"days": 90, "since": "2026-08-07", "count": 0, "total": 0.0, "paired": 0,
                                      "paired_total": 0.0, "waiting": 0, "elsewhere": 0}
    leaked = json.dumps(api_preview)
    assert "Checking" not in leaked and "1234" not in leaked and "loan-payment" not in leaked
    assert manual.match_preview(conn, "GARAGE PAYMENT", PAYMENT_TODAY)["count"] == 0
    assert manual.matching_transactions(conn, "GARAGE PAYMENT") == []

    out = manual.match_payments(conn, today=PAYMENT_TODAY)
    assert out["applied"] == [] and out["unapplied"] == []
    assert manual.get(conn, loan["id"])["balance"] == 500.0
    assert manual.history(conn, loan["id"]) == [{"date": "2026-08-01", "balance": 500.0}]
    nw = insights.net_worth(conn, today=PAYMENT_TODAY)
    assert (nw["debts"], nw["net"]) == (500.0, -500.0)
    assert portfolio.debt(conn, PAYMENT_TODAY)["total"] == 500.0
    assert conn.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE transaction_id = 'loan-payment'").fetchone()[0] == 1


def test_R8_an_included_source_still_matches_and_is_shown(conn):
    loan = payment_scene(conn)
    insights.save_networth_excluded(conn, [])

    preview = api.match_preview(conn, {"kind": "loan", "id": str(loan["id"]), "pattern": "GARAGE PAYMENT"},
                                PAYMENT_TODAY)
    assert preview["count"] == 1 and preview["typical"] == 100.0
    assert preview["last"]["account_name"] == "Checking" and preview["last"]["account_mask"] == "1234"

    out = manual.match_payments(conn, today=PAYMENT_TODAY)
    assert [p["transaction_id"] for p in out["applied"]] == ["loan-payment"]
    assert manual.get(conn, loan["id"])["balance"] == 400.0
    nw = insights.net_worth(conn, today=PAYMENT_TODAY)
    assert (nw["assets"], nw["debts"], nw["net"]) == (1200.0, 400.0, 800.0)
    assert portfolio.debt(conn, PAYMENT_TODAY)["total"] == 400.0


def test_R8_a_policy_added_after_a_match_takes_the_payment_back_out_of_the_balance(conn):
    loan = payment_scene(conn)
    insights.save_networth_excluded(conn, [])
    assert [p["transaction_id"] for p in manual.match_payments(conn, today=PAYMENT_TODAY)["applied"]] == ["loan-payment"]
    assert manual.get(conn, loan["id"])["balance"] == 400.0
    raw_before = [tuple(r) for r in conn.execute(
        "SELECT transaction_id, account_id, date, name, amount, pending, removed FROM transactions "
        "WHERE transaction_id = 'loan-payment'")]
    class_before = [tuple(r) for r in conn.execute(
        "SELECT transaction_id, category_id, source FROM classifications WHERE transaction_id = 'loan-payment'")]

    insights.save_excluded_accounts(conn, ["acc_source"])       # saved after the payment was applied
    assert manual.get(conn, loan["id"])["balance"] == 500.0
    assert manual.history(conn, loan["id"]) == [{"date": "2026-08-01", "balance": 500.0}]
    assert (insights.net_worth(conn, today=PAYMENT_TODAY)["debts"]) == 500.0
    assert portfolio.debt(conn, PAYMENT_TODAY)["total"] == 500.0

    out = manual.match_payments(conn, today=PAYMENT_TODAY)      # reported, not silently kept
    assert out["applied"] == []
    assert out["unapplied"] == []                               # kept, so nothing was taken back
    assert [(u["transaction_id"], u["loan_id"]) for u in out["not_counted"]] == [("loan-payment", loan["id"])]
    assert "excluded by the account policy" in out["not_counted"][0]["reason"]
    retained = [tuple(r) for r in conn.execute("SELECT transaction_id, source FROM loan_payments")]
    assert retained == [("loan-payment", "auto")]              # only counting stops: the row and its source stay
    assert [tuple(r) for r in conn.execute(
        "SELECT transaction_id, account_id, date, name, amount, pending, removed FROM transactions "
        "WHERE transaction_id = 'loan-payment'")] == raw_before
    assert [tuple(r) for r in conn.execute(
        "SELECT transaction_id, category_id, source FROM classifications WHERE transaction_id = 'loan-payment'")] \
        == class_before
    assert manual.get(conn, loan["id"])["balance"] == 500.0
    assert manual.history(conn, loan["id"]) == [{"date": "2026-08-01", "balance": 500.0}]
    assert insights.net_worth(conn, today=PAYMENT_TODAY)["debts"] == 500.0
    assert portfolio.debt(conn, PAYMENT_TODAY)["total"] == 500.0


def test_R8_a_direct_apply_keeps_its_errors_and_refuses_an_excluded_source(conn):
    loan = payment_scene(conn)
    insights.save_networth_excluded(conn, [])
    direct = manual.apply_payment(conn, "loan-payment", loan["id"])
    assert direct["balance_after"] == 400.0
    assert manual.unapply_payment(conn, "loan-payment")["balance_after"] == 500.0

    insights.save_excluded_accounts(conn, ["acc_source"])
    with pytest.raises(manual.ManualError, match="excluded by the account policy"):
        manual.apply_payment(conn, "loan-payment", loan["id"])
    assert conn.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 0
    assert manual.get(conn, loan["id"])["balance"] == 500.0


def test_R8_an_excluded_destination_offers_and_applies_nothing_but_keeps_its_matches(conn):
    loan = payment_scene(conn)
    insights.save_networth_excluded(conn, [])
    assert [p["transaction_id"] for p in manual.match_payments(conn, today=PAYMENT_TODAY)["applied"]] == ["loan-payment"]
    assert manual.get(conn, loan["id"])["balance"] == 400.0

    insights.save_excluded_accounts(conn, [f"manual:{loan['id']}"])   # the destination itself is excluded
    preview = api.match_preview(conn, {"kind": "loan", "id": str(loan["id"]), "pattern": "GARAGE PAYMENT"},
                                PAYMENT_TODAY)
    assert preview["count"] == 0 and preview["matches"] == [] and preview["last"] is None
    assert manual.match_preview(conn, "GARAGE PAYMENT", PAYMENT_TODAY, loan["id"])["count"] == 0
    out = manual.match_payments(conn, today=PAYMENT_TODAY)
    assert out["applied"] == [] and out["unapplied"] == []
    assert manual.get(conn, loan["id"])["balance"] == 500.0             # none of the kept row counts while excluded
    assert conn.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 1

    insights.save_excluded_accounts(conn, [])                         # retention: the row works again
    assert manual.get(conn, loan["id"])["balance"] == 400.0
    assert [h["balance"] for h in manual.history(conn, loan["id"])] == [500.0, 400.0]


def _write_policy_without_rebuild(conn, ids):
    """Clear the policy outside save_excluded_accounts (as the reviewer's probe and any direct settings write do),
    so only match_payments() can bring the derived balance back."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                     (db.ACCOUNT_POLICY_KEY, json.dumps(ids), db.now()))


@pytest.mark.parametrize("applied_by", ["user", "auto"])
def test_R12_a_payment_survives_the_daily_match_run_while_its_source_is_excluded(conn, applied_by):
    loan = payment_scene(conn)
    insights.save_networth_excluded(conn, [])
    if applied_by == "user":
        manual.apply_payment(conn, "loan-payment", loan["id"], source="user")
    else:
        assert [p["transaction_id"] for p in manual.match_payments(conn, today=PAYMENT_TODAY)["applied"]] \
            == ["loan-payment"]
    assert manual.get(conn, loan["id"])["balance"] == 400.0
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, source FROM loan_payments")] \
        == [("loan-payment", applied_by)]

    insights.save_excluded_accounts(conn, ["acc_source"])
    out = manual.match_payments(conn, today=PAYMENT_TODAY)      # the ordinary daily cronpipe step
    assert out["unapplied"] == []
    assert [(u["transaction_id"], u["loan_id"]) for u in out["not_counted"]] == [("loan-payment", loan["id"])]
    assert "excluded by the account policy" in out["not_counted"][0]["reason"]
    assert manual.get(conn, loan["id"])["balance"] == 500.0
    assert manual.history(conn, loan["id"]) == [{"date": "2026-08-01", "balance": 500.0}]
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, source FROM loan_payments")] \
        == [("loan-payment", applied_by)]

    _write_policy_without_rebuild(conn, [])                     # clearing does not rebuild on its own
    assert manual.get(conn, loan["id"])["balance"] == 500.0
    out = manual.match_payments(conn, today=PAYMENT_TODAY)      # the next daily step must catch up
    assert out["applied"] == [] and out["unapplied"] == []
    assert manual.get(conn, loan["id"])["balance"] == 400.0
    assert [h["balance"] for h in manual.history(conn, loan["id"])] == [500.0, 400.0]
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, source FROM loan_payments")] \
        == [("loan-payment", applied_by)]
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE transaction_id = 'loan-payment'").fetchone()[0] == 1


@pytest.mark.parametrize("applied_by", ["user", "auto"])
def test_R12_a_source_excluded_from_the_start_stays_uncounted(conn, applied_by):
    loan = payment_scene(conn)
    insights.save_networth_excluded(conn, [])
    insights.save_excluded_accounts(conn, ["acc_source"])
    if applied_by == "user":
        with pytest.raises(manual.ManualError, match="excluded by the account policy"):
            manual.apply_payment(conn, "loan-payment", loan["id"], source="user")
    out = manual.match_payments(conn, today=PAYMENT_TODAY)
    assert out["applied"] == [] and out["unapplied"] == []
    assert conn.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 0
    assert manual.get(conn, loan["id"])["balance"] == 500.0
    assert manual.history(conn, loan["id"]) == [{"date": "2026-08-01", "balance": 500.0}]


@pytest.mark.parametrize("applied_by", ["user", "auto"])
def test_R12_an_included_source_applies_once_for_400(conn, applied_by):
    loan = payment_scene(conn)
    insights.save_networth_excluded(conn, [])
    if applied_by == "user":
        manual.apply_payment(conn, "loan-payment", loan["id"], source="user")
    else:
        assert [p["transaction_id"] for p in manual.match_payments(conn, today=PAYMENT_TODAY)["applied"]] \
            == ["loan-payment"]
    assert manual.get(conn, loan["id"])["balance"] == 400.0
    assert [h["balance"] for h in manual.history(conn, loan["id"])] == [500.0, 400.0]
    assert [tuple(r) for r in conn.execute("SELECT transaction_id, source FROM loan_payments")] \
        == [("loan-payment", applied_by)]

    out = manual.match_payments(conn, today=PAYMENT_TODAY)      # a second run never applies it twice
    assert out["applied"] == []
    assert conn.execute("SELECT COUNT(*) FROM loan_payments").fetchone()[0] == 1
    assert manual.get(conn, loan["id"])["balance"] == 400.0


def test_R12_the_accounts_payload_presets_equal_the_cli_selection_with_hidden_plaid_and_manual(conn, capsys):
    bank(conn)
    acct(conn, "acc_visible", "item_a", "Visible savings", current=100.0, mask="1111")
    acct(conn, "acc_hidden", "item_a", "Hidden brokerage", type_="investment", subtype="brokerage",
         current=1000.0, mask="2222")
    visible_manual = manual.create(conn, dict(kind="bank", subtype="savings", name="Visible manual savings",
                                              institution="Synthetic CU", balance=100.0,
                                              balance_as_of=PAYMENT_TODAY.isoformat()), PAYMENT_TODAY)
    hidden_manual = manual.create(conn, dict(kind="investment", subtype="brokerage", name="Hidden manual brokerage",
                                             institution="Synthetic CU", balance=1000.0,
                                             balance_as_of=PAYMENT_TODAY.isoformat()), PAYMENT_TODAY)
    assert visible_manual["id"] and hidden_manual["id"]
    insights.save_networth_excluded(conn, [])
    hidden = ["acc_hidden", f"manual:{hidden_manual['id']}"]
    insights.save_excluded_accounts(conn, hidden)              # policy-hidden, so not on the sheet's switch rows

    presets = api.accounts(conn, {}, PAYMENT_TODAY)["networth_presets"]
    assert presets["everything"] == []
    assert presets["cash_and_debts"] == hidden                 # the full eligible universe, hidden ids included

    assert cli.main(["networth-custom", "--preset", "cash-and-debts", "--json"]) == 0
    capsys.readouterr()
    assert insights.networth_excluded(conn) == presets["cash_and_debts"]
    assert cli.main(["networth-custom", "--reset", "--json"]) == 0
    capsys.readouterr()
    assert insights.networth_excluded(conn) == presets["everything"]


# --- round 2: plain text never calls a malformed policy an all-clear ---------------------------------

@pytest.mark.parametrize("raw, kind", [
    (None, "empty"),                                      # absent setting
    (json.dumps([]), "empty"),                            # a valid empty policy
    (json.dumps(["acc_b"]), "exact"),                     # the exact-id control
    (json.dumps("acc_b"), "invalid"),                     # a JSON scalar
    (json.dumps({"ids": ["acc_b"]}), "invalid"),          # a JSON object
    (json.dumps(["acc_a", 5]), "invalid"),                # a mixed-type array (partial)
    ("not json", "invalid"),                              # not JSON at all
])
def test_R9_status_and_account_policy_text_agree_on_every_shape(conn, capsys, raw, kind):
    bank(conn)
    acct(conn, "acc_a", "item_a", "Checking A", current=1000.0, mask="1111")
    acct(conn, "acc_b", "item_a", "Checking B", current=2000.0, mask="2222")
    with conn:
        if raw is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (db.ACCOUNT_POLICY_KEY,))
        else:
            conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                         (db.ACCOUNT_POLICY_KEY, raw, db.now()))

    status_text = report.status_text(report.status(conn))
    capsys.readouterr()
    assert cli.main(["account-policy", "--list"]) == 0
    cli_text = capsys.readouterr().out

    assert "every account counts" not in status_text
    if kind == "empty":
        assert "Account policy" not in status_text
        assert cli_text == "Account policy: nothing excluded; every account counts.\n"
    elif kind == "exact":
        assert "Account policy: 1 excluded, 0 unresolved (complete)" in status_text
        assert "Account policy: 1 excluded, 0 unresolved (complete)" in cli_text
    else:
        assert "invalid" in status_text.lower()
        assert "invalid" in cli_text.lower()
        assert "nothing excluded" not in cli_text.lower()
        assert "every account counts" not in cli_text.lower()


def test_R10_the_accounts_payload_has_no_spark_for_an_excluded_account(conn):
    bank(conn)
    acct(conn, "acc_kept", "item_a", "Checking", current=100.0, mask="1111")
    acct(conn, "acc_hidden", "item_a", "Hidden", current=2000.0, mask="2222")
    balance_snapshot(conn, "acc_kept", "2026-09-28", 100.0)
    balance_snapshot(conn, "acc_hidden", "2026-09-28", 2000.0)
    insights.save_excluded_accounts(conn, ["acc_hidden"])

    payload = api.accounts(conn, {}, date(2026, 9, 28))
    assert payload["sparks"] == {"acc_kept": [100.0]}
    assert "acc_hidden" not in payload["sparks"]

    with pytest.raises(reads.ReadError) as e:      # a CLI filter cannot resolve or name the hidden account either
        reads.parse_filters(conn, date(2026, 9, 28), account="acc_hidden", default_period="all")
    assert "Hidden" not in str(e.value) and "2222" not in str(e.value)


def test_R11_coverage_and_tag_facets_ignore_an_excluded_accounts_transactions(conn):
    bank(conn)
    acct(conn, "acc_kept", "item_a", "Checking", current=100.0, mask="1111")
    acct(conn, "acc_hidden", "item_a", "Hidden", current=2000.0, mask="2222")
    tx(conn, "2026-09-10", 10.0, name="KEPT COFFEE", account_id="acc_kept", tid="t_kept")
    tx(conn, "2026-01-05", 20.0, name="HIDDEN COFFEE", account_id="acc_hidden", tid="t_hidden")
    with conn:
        conn.execute("INSERT INTO tags (name, created_at) VALUES ('Hidden tag', ?)", (db.now(),))
        conn.execute("INSERT INTO transaction_tags (transaction_id, tag_id) VALUES ('t_hidden', "
                     "(SELECT id FROM tags WHERE name = 'Hidden tag'))")
    insights.save_excluded_accounts(conn, ["acc_hidden"])

    assert api.transaction_coverage(conn) == "2026-09"
    facets = api.facets(conn, "all", (["1 = 1"], []), (["1 = 1"], []))
    assert "Hidden tag" not in [t["name"] for t in facets["tags"]]


# --- round 4: a kept payment is a state line, never a take-back event ---------------------------------

def test_R13_the_cli_never_calls_a_policy_kept_payment_a_take_back(conn, capsys):
    """cronpipe feeds `finance match-payments` output to the daily report, so a retained row must not read as a
    removal — on the first run, on the second, or on a dry run. The genuine take-back wording is pinned by
    tests/test_card_matching.py."""
    loan = payment_scene(conn)
    insights.save_networth_excluded(conn, [])
    manual.apply_payment(conn, "loan-payment", loan["id"])       # a user-applied payment: balance 400
    insights.save_excluded_accounts(conn, ["acc_source"])        # its source is excluded: kept, not counted

    for _ in range(2):                                          # two consecutive daily runs
        assert cli.main(["match-payments"]) == 0
        text = capsys.readouterr().out
        assert "took back" not in text and "would take back" not in text
        assert "not counted: loan-payment on #" in text and "excluded by the account policy" in text
        assert manual.get(conn, loan["id"])["balance"] == 500.0
        assert len(conn.execute("SELECT * FROM loan_payments").fetchall()) == 1

    assert cli.main(["match-payments", "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "took back" not in text and "would take back" not in text
    assert "not counted: loan-payment on #" in text and "(dry run, nothing written)" in text
