"""TASK-14's net worth "Cash & debts" (cash minus every debt; investments, vehicles, property and other assets not
counted), which TASK-15 turned into a preset of the Custom net worth and its first saved selection. insights.net_worth()
with the preset (insights.cash_preset) and each history point's custom_assets / custom_net are recomputed here straight
from the DB, on every gate DB (tests/gate_dbs.py: full, nocaps, empty, history-empty, onepoint) and on the edge cases (a
manual bank account, an archived manual bank account and asset, a negative cash balance); All's numbers stay what they
were. The migration: a DB without a saved selection gets exactly this one on its first read. Then the hidden
`finance networth --cash-only` (kept for one release), text and JSON: the preset whatever the saved selection is."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

import gate_dbs
from hermes_finance import cli, config, db, insights, manual, report

TODAY = date(2026, 9, 26)
CASH_ONLY_GROUPS = ["cash", "cards", "loans", "other_debts"]


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    try:
        code = cli.main(argv)
    except SystemExit as e:
        code = e.code
    out, err = capsys.readouterr()
    return code, out, err


def cli_json(capsys, *argv: str) -> dict:
    code, out, err = run([*argv, "--json"], capsys)
    assert code == 0, err
    return json.loads(out)


def gate_db(kind: str):
    """The gate DB `kind` at the configured path (FINANCE_DATA_DIR, the data_dir fixture), so the CLI reads it too."""
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    gate_dbs.build(kind, path, TODAY)
    return db.connect(path)


# --- the same numbers, written again from the tables ---------------------------------------------------------

def plaid_sum(conn, types: tuple[str, ...]) -> float:
    q = ",".join("?" * len(types))
    return conn.execute(f"SELECT COALESCE(SUM(a.current_balance), 0) FROM accounts a JOIN items i ON i.item_id = a.item_id "
                        f"WHERE i.removed_at IS NULL AND a.type IN ({q})", types).fetchone()[0] + (
        alpaca_equity(conn) if "investment" in types else 0.0)


def alpaca_equity(conn) -> float:
    """TASK-19: the Alpaca live account, an investment account at its latest counted snapshot's equity while ACTIVE."""
    row = conn.execute("SELECT status, equity FROM alpaca_snapshots WHERE counted = 1 ORDER BY date DESC LIMIT 1").fetchone()
    return row[1] if row and row[0] == "ACTIVE" else 0.0


def manual_sum(conn, kinds: tuple[str, ...]) -> float:
    q = ",".join("?" * len(kinds))
    return conn.execute(f"SELECT COALESCE(SUM(balance), 0) FROM manual_accounts WHERE archived_at IS NULL AND kind IN ({q})",
                        kinds).fetchone()[0]


def by_sql(conn) -> dict:
    cash = plaid_sum(conn, ("depository",)) + manual_sum(conn, ("bank",))
    assets = plaid_sum(conn, ("depository", "investment")) + manual_sum(
        conn, ("bank", "investment", "vehicle", "property", "other_asset"))
    debts = plaid_sum(conn, ("credit", "loan")) + manual_sum(conn, ("credit_card", "loan", "other_liability"))
    return {"cash": round(cash, 2), "assets": round(assets, 2), "debts": round(debts, 2)}


def local_day(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()


def cash_on(conn, day: str, archived: bool = False) -> float:
    """Cash on `day`: each depository account's latest real snapshot on or before it, each manual bank account's latest
    balance row on or before it: with `archived` (All's rule) an archived one until the local day it was archived,
    without it (Custom's rule, TASK-15: an archived account never counts in Custom) none."""
    plaid = conn.execute(
        """SELECT COALESCE(SUM(s.current), 0) FROM balance_snapshots s
           JOIN accounts a ON a.account_id = s.account_id JOIN items i ON i.item_id = a.item_id
           WHERE i.removed_at IS NULL AND a.type = 'depository' AND s.current IS NOT NULL
             AND s.date = (SELECT MAX(s2.date) FROM balance_snapshots s2 WHERE s2.account_id = s.account_id
                           AND s2.date <= ? AND s2.current IS NOT NULL)""", (day,)).fetchone()[0]
    bank = 0.0
    for m in conn.execute("SELECT id, archived_at FROM manual_accounts WHERE kind = 'bank'").fetchall():
        if m["archived_at"] and (not archived or day >= local_day(m["archived_at"])):
            continue
        row = conn.execute("SELECT balance FROM manual_balance_history WHERE manual_account_id = ? AND date <= ? "
                           "ORDER BY date DESC LIMIT 1", (m["id"], day)).fetchone()
        bank += row["balance"] if row else 0.0
    return plaid + bank


def preset(conn) -> dict:
    """net_worth() with the "Cash & debts" preset in place of the saved selection."""
    return insights.net_worth(conn, excluded=insights.cash_preset(conn))


def drop_selection(conn) -> None:
    with conn:
        conn.execute("DELETE FROM settings WHERE key = 'networth_excluded'")


def check(conn) -> dict:
    """Every TASK-14 invariant on this DB, for the preset; returns net_worth() with it."""
    nw = preset(conn)
    want = by_sql(conn)
    co = nw["custom"]
    assert set(co) == {"assets", "debts", "net", "excluded"}
    assert {k: co[k] for k in ("assets", "debts", "net")} == {"assets": want["cash"], "debts": want["debts"],
                                                            "net": round(want["cash"] - want["debts"], 2)}
    # All is untouched: assets = every asset account, the debts are the same ones
    assert (nw["assets"], nw["debts"], nw["net"]) == (want["assets"], want["debts"], round(want["assets"] - want["debts"], 2))
    assert co["debts"] == nw["debts"]
    cash_group = next(g for g in nw["breakdown"] if g["key"] == "cash")
    assert co["assets"] == cash_group["total"]
    left_out = sum(g["total"] for g in nw["breakdown"] if g["asset"] and g["key"] != "cash")
    assert round(nw["assets"] - left_out, 2) == co["assets"]       # All minus what it leaves out
    assert round(sum(x["balance"] for x in co["excluded"]), 2) == round(left_out, 2)
    assert {x["group"] for x in co["excluded"]} <= {"investments", "vehicles", "property", "other_assets"}
    for p in nw["history"]:
        assert set(p) == {"date", "assets", "debts", "net", "custom_assets", "custom_debts", "custom_net"}
        assert p["custom_assets"] == pytest.approx(cash_on(conn, p["date"]), abs=0.005), p["date"]
        assert p["custom_debts"] == p["debts"]
        assert p["custom_net"] == pytest.approx(p["custom_assets"] - p["debts"], abs=0.011), p["date"]
        assert p["net"] == pytest.approx(p["assets"] - p["debts"], abs=0.011)
    return nw


# --- insights.net_worth ------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", gate_dbs.KINDS)
def test_cash_preset_on_every_gate_db(data_dir, kind):
    conn = gate_db(kind)
    nw = check(conn)
    if kind == "empty":   # one checking account, nothing owed, no snapshot
        assert nw["custom"] == {"assets": 812.5, "debts": 0.0, "net": 812.5, "excluded": []} and nw["history"] == []
    else:                 # the demo banks: investments, a car (and on full the homes) are left out
        assert nw["custom"]["assets"] < nw["assets"] and nw["custom"]["net"] < nw["net"]
        assert len(nw["history"]) >= 2
        assert any(p["custom_net"] != p["net"] for p in nw["history"])
    conn.close()


@pytest.mark.parametrize("kind", gate_dbs.KINDS)
def test_migration_default_is_the_cash_selection(data_dir, kind):
    """No saved selection (full has one: dropped here): the first read saves the "Cash & debts" one, so Custom is
    TASK-14's cash & debts number exactly; the read after it reads the saved row."""
    conn = gate_db(kind)
    drop_selection(conn)
    want = check(conn)["custom"]
    nw = insights.net_worth(conn)
    assert nw["custom"] == want
    row = conn.execute("SELECT value FROM settings WHERE key = 'networth_excluded'").fetchone()
    assert row is not None and json.loads(row[0]) == insights.cash_preset(conn)
    assert insights.net_worth(conn)["custom"] == want
    conn.close()


def test_history_points_count_only_cash(data_dir):
    conn = gate_db("full")
    h = preset(conn)["history"]
    last = h[-1]
    # the last day's cash = today's depository snapshots (the gate DB snapshots every Plaid account daily)
    assert last["custom_assets"] == round(plaid_sum(conn, ("depository",)), 2)
    assert last["custom_net"] == round(last["custom_assets"] - last["debts"], 2)
    assert all(p["custom_assets"] <= p["assets"] for p in h)         # no bank is negative here: a part of the assets
    conn.close()


def test_manual_bank_archived_asset_and_negative_cash(data_dir):
    conn = gate_db("full")
    before = preset(conn)
    chk = conn.execute("SELECT current_balance FROM accounts WHERE account_id = 'acc_nova_chk'").fetchone()[0]
    manual.create(conn, {"kind": "bank", "name": "Credit Union", "institution": "Golden 1", "subtype": "checking",
                         "balance": 2500.0, "balance_as_of": "2026-09-20"}, TODAY)
    old = manual.create(conn, {"kind": "bank", "name": "Old savings", "institution": "Wells", "subtype": "savings",
                               "balance": 999.0, "balance_as_of": "2026-09-18"}, TODAY)
    boat = manual.create(conn, {"kind": "other_asset", "name": "Boat", "subtype": "Other", "balance": 12000.0,
                                "balance_as_of": "2026-09-18"}, TODAY)
    for m in (old, boat):
        manual.archive(conn, m["id"], TODAY)
    with conn:   # archived on the fixture's day (noon UTC: Sep 26 in any US zone), not the wall clock's
        conn.execute("UPDATE manual_accounts SET archived_at = '2026-09-26T12:00:00Z' WHERE id IN (?, ?)", (old["id"], boat["id"]))
        conn.execute("UPDATE accounts SET current_balance = -250.0 WHERE account_id = 'acc_nova_chk'")   # overdrawn
        conn.execute("UPDATE balance_snapshots SET current = -250.0 WHERE account_id = 'acc_nova_chk' AND date = ?",
                     (TODAY.isoformat(),))
    nw = check(conn)
    moved = 2500.0 - chk - 250.0                                      # the new bank account, the overdraft
    assert nw["custom"]["assets"] == round(before["custom"]["assets"] + moved, 2)
    assert nw["assets"] == round(before["assets"] + moved, 2)         # All moves the same way (a bank is cash in both)
    assert nw["custom"]["debts"] == nw["debts"] == before["debts"]
    assert not {m["id"] for m in (old, boat)} & {x["id"] for x in nw["custom"]["excluded"]}  # archived: not even listed
    cash = next(g for g in nw["breakdown"] if g["key"] == "cash")
    assert cash["count"] == 5 and cash["manual_count"] == 1           # the archived bank account isn't in it
    assert "other_assets" not in {g["key"] for g in nw["breakdown"]}  # the archived boat: no group, in no total
    h = {p["date"]: p for p in nw["history"]}
    assert h[TODAY.isoformat()]["custom_assets"] == pytest.approx(cash_on(conn, TODAY.isoformat()), abs=0.005)
    # before they were archived, the old savings and the boat are in All's line (the savings as cash), never in Custom's
    before_day = h["2026-09-20"]
    assert before_day["assets"] - before_day["custom_assets"] > 999.0 + 12000.0 - 0.01
    assert cash_on(conn, "2026-09-20", archived=True) - cash_on(conn, "2026-09-20") == pytest.approx(999.0)
    conn.close()


def test_negative_cash_total(conn):
    """Overdrawn with nothing else in the bank: cash is negative, and cash & debts is below it by every debt."""
    from factory import acct, bank
    bank(conn)
    acct(conn, "chk", current=-300.0)
    acct(conn, "inv", type_="investment", subtype="brokerage", current=1000.0)
    acct(conn, "cc", type_="credit", subtype="credit card", current=200.0)
    nw = insights.net_worth(conn, history=False)                    # the first read: the cash selection, saved
    assert (nw["assets"], nw["debts"], nw["net"]) == (700.0, 200.0, 500.0)
    assert {k: nw["custom"][k] for k in ("assets", "debts", "net")} == {"assets": -300.0, "debts": 200.0, "net": -500.0}
    assert [x["id"] for x in nw["custom"]["excluded"]] == ["inv"]


def test_cash_only_groups():
    assert list(insights.CASH_ONLY_GROUPS) == CASH_ONLY_GROUPS


# --- finance networth --cash-only (hidden, one release) ------------------------------------------------------------

def test_cli_cash_only_text(data_dir, capsys):
    conn = gate_db("full")
    co = preset(conn)["custom"]
    conn.close()
    code, out, err = run(["networth", "--cash-only"], capsys)
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == (f"Net worth (cash & debts only) {report.money(co['net'])}: cash {report.money(co['assets'])}, "
                        f"owe {report.money(co['debts'])}")
    heads = [ln.strip().split(" (")[0] for ln in lines if ln.startswith("  ") and not ln.startswith("    ")]
    assert heads == ["Cash", "Credit cards", "Loans"]                 # (Other debts only when used)
    for gone in ("Investments", "Vehicles", "Property", "Mesa", "401(k)", "Condo"):
        assert gone not in out.replace("investments, vehicles, property", "")
    assert "not counted" in out and "--cash-only" in out
    assert "Emergency Fund Vault" in out and "Sapphire" in out       # the saved selection leaves these out: not here
    code, all_out, _ = run(["networth"], capsys)
    assert all_out.splitlines()[0].startswith("Net worth $1,") and "Investments (" in all_out and "cash & debts" not in all_out


def test_cli_cash_only_history_text(data_dir, capsys):
    conn = gate_db("full")
    h = preset(conn)["history"]
    conn.close()
    code, out, _ = run(["networth", "--cash-only", "--history"], capsys)
    assert code == 0
    cells = lambda ln: [c.strip() for c in ln.split("|")]  # noqa: E731
    head = next(ln for ln in out.splitlines() if ln.startswith("date"))
    assert cells(head) == ["date", "cash", "debts", "net"]
    rows = [ln for ln in out.splitlines() if ln[:4].isdigit()]
    assert len(rows) == len(h) and out.splitlines()[-1] == f"({len(h)} rows)"
    last = cells(rows[-1])
    assert last == [h[-1]["date"], report.money(h[-1]["custom_assets"]), report.money(h[-1]["debts"]),
                    report.money(h[-1]["custom_net"])]


def test_cli_json_cash_only_is_the_preset_whatever_is_saved(data_dir, capsys):
    conn = gate_db("full")
    nw = preset(conn)
    saved = insights.net_worth(conn)
    conn.close()
    assert saved["custom"]["net"] != nw["custom"]["net"]             # full's saved selection isn't the preset
    full = cli_json(capsys, "networth")
    cash = cli_json(capsys, "networth", "--cash-only")
    co = {k: nw["custom"][k] for k in ("assets", "debts", "net")}
    assert cash["mode"] == "cash_only" and cash["cash_only"] == co and cash["custom"] == nw["custom"]
    assert full["mode"] == "all" and "cash_only" not in full and full["custom"] == saved["custom"]
    assert (full["assets"], full["debts"], full["net"]) == (nw["assets"], nw["debts"], nw["net"])   # unchanged
    assert (cash["assets"], cash["debts"], cash["net"]) == tuple(co[k] for k in ("assets", "debts", "net"))
    assert [g["key"] for g in full["groups"]] == [g["key"] for g in nw["breakdown"]]
    assert [g["key"] for g in cash["groups"]] == [k for k in CASH_ONLY_GROUPS if k in {g["key"] for g in nw["breakdown"]}]
    # the listed groups add up to the net in each mode
    assert round(sum(g["signed"] for g in cash["groups"]), 2) == cash["net"]
    assert round(sum(g["signed"] for g in full["groups"]), 2) == full["net"]
    hist = cli_json(capsys, "networth", "--cash-only", "--history")["history"]
    assert [{k: p[k] for k in ("date", "assets", "debts", "net", "custom_assets", "custom_debts", "custom_net")} for p in hist] \
        == nw["history"]
    assert all(p["cash_assets"] == p["custom_assets"] and p["cash_net"] == p["custom_net"] for p in hist)


def test_cli_help_hides_cash_only(capsys):
    code, out, _ = run(["networth", "--help"], capsys)
    assert code == 0 and "--cash-only" not in out and "cash_only" not in out
    assert "--custom" in out
