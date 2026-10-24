"""TASK-15: hand-select which accounts count in net worth, the "Custom" net worth. Every account counts unless its id is
in the `networth_excluded` setting (a Plaid account_id, or manual:<id>): insights.net_worth()'s custom block, each
Accounts group's custom part and each history point's custom_* are recomputed here straight from the tables, on every
gate DB (tests/gate_dbs.py: full, nocaps, empty, history-empty, onepoint), for a Plaid account, a manual account, a whole
group and everything left out, a new account (counted by default) and an archived one (never counted). The first read
without a saved selection saves TASK-14's "Cash & debts" one (tests/test_networth_task14.py checks its numbers), logged
once, and a read-only connection saves nothing. Then POST /api/settings (validation, the same Host / Origin / JSON gate
as every write) and the CLI: `finance networth --custom`, the account ids in `finance networth --json`, and
`finance networth-custom`."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

import pytest

import gate_dbs
from hermes_finance import api, cli, config, db, insights, manual, report
from hermes_finance.web import App

TODAY = date(2026, 9, 26)
ROOT = Path(__file__).resolve().parents[1]
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
ASSET_TYPES, DEBT_TYPES = ("depository", "investment"), ("credit", "loan")
ASSET_KINDS = ("bank", "investment", "vehicle", "property", "other_asset")
DEBT_KINDS = ("credit_card", "loan", "other_liability")
# full's saved selection (tests/gate_dbs.py)
FULL_OUT_PLAID = {"acc_nova_vault", "acc_nova_inv", "acc_nova_robo"}
FULL_OUT_MANUAL = {"Work 401(k)", "2015 Honda Fit", "Cabin", "Condo", "Studio", "Sapphire"}


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


def saved(conn) -> list[str] | None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'networth_excluded'").fetchone()
    return None if row is None else json.loads(row[0])


def manual_ids(conn) -> dict[str, str]:
    return {r["name"]: f"manual:{r['id']}" for r in conn.execute("SELECT id, name FROM manual_accounts")}


# --- the same numbers, written again from the tables ---------------------------------------------------------

def accounts_by_sql(conn) -> list[tuple[str, str, float, bool]]:
    """(id, group-ish kind, balance, is_asset) of every account net worth counts: connected Plaid accounts of the four
    types, live manual accounts."""
    out = [(r[0], r[1], r[2] or 0.0, r[1] in ASSET_TYPES) for r in conn.execute(
        "SELECT a.account_id, a.type, a.current_balance FROM accounts a JOIN items i ON i.item_id = a.item_id "
        "WHERE i.removed_at IS NULL AND a.type IN ('depository', 'investment', 'credit', 'loan')")]
    out += [(f"manual:{r[0]}", r[1], r[2], r[1] in ASSET_KINDS) for r in conn.execute(
        "SELECT id, kind, balance FROM manual_accounts WHERE archived_at IS NULL")]
    row = conn.execute("SELECT status, equity FROM alpaca_snapshots WHERE counted = 1 ORDER BY date DESC LIMIT 1").fetchone()
    if row and row[0] == "ACTIVE":  # TASK-19: the Alpaca live account, while ACTIVE
        out.append(("alpaca:live", "investment", row[1], True))
    return out


def expected(conn, excluded) -> dict:
    skip = set(excluded)
    a = sum(b for i, _, b, asset in accounts_by_sql(conn) if asset and i not in skip)
    d = sum(b for i, _, b, asset in accounts_by_sql(conn) if not asset and i not in skip)
    return {"assets": round(a, 2), "debts": round(d, 2), "net": round(a - d, 2)}


def custom3(nw) -> dict:
    return {k: nw["custom"][k] for k in ("assets", "debts", "net")}


def point(conn, day: str, excluded, custom: bool = True) -> tuple[float, float]:
    """(assets, debts) on `day`: each Plaid account's latest real snapshot on or before it, each manual account's latest
    balance row on or before it. Custom: without the accounts `excluded` and without any archived manual account; All:
    an archived one until the local day it was archived."""
    skip = set(excluded)
    a = d = 0.0
    for r in conn.execute(
            """SELECT s.account_id, a.type, s.current FROM balance_snapshots s
               JOIN accounts a ON a.account_id = s.account_id JOIN items i ON i.item_id = a.item_id
               WHERE i.removed_at IS NULL AND a.type IN ('depository', 'investment', 'credit', 'loan')
                 AND s.current IS NOT NULL AND s.date = (SELECT MAX(s2.date) FROM balance_snapshots s2
                   WHERE s2.account_id = s.account_id AND s2.date <= ? AND s2.current IS NOT NULL)""", (day,)):
        if custom and r[0] in skip:
            continue
        if r[1] in ASSET_TYPES:
            a += r[2]
        else:
            d += r[2]
    for m in conn.execute("SELECT id, kind, archived_at FROM manual_accounts").fetchall():
        if m["archived_at"] and (custom or day >= insights.local_day(m["archived_at"])):
            continue
        if custom and f"manual:{m['id']}" in skip:
            continue
        row = conn.execute("SELECT balance FROM manual_balance_history WHERE manual_account_id = ? AND date <= ? "
                           "ORDER BY date DESC LIMIT 1", (m["id"], day)).fetchone()
        if row:
            if m["kind"] in ASSET_KINDS:
                a += row[0]
            else:
                d += row[0]
    if not (custom and "alpaca:live" in skip):  # TASK-19: Alpaca's latest counted snapshot on or before the day
        row = conn.execute("SELECT status, equity FROM alpaca_snapshots WHERE counted = 1 AND date <= ? ORDER BY date DESC "
                           "LIMIT 1", (day,)).fetchone()
        a += row[1] if row and row[0] == "ACTIVE" else 0.0
    return a, d


def check(conn, excluded=None) -> dict:
    """Every Custom invariant for this selection (default: the saved one); returns net_worth()."""
    nw = insights.net_worth(conn) if excluded is None else insights.net_worth(conn, excluded=excluded)
    sel = saved(conn) if excluded is None else list(excluded)
    assert custom3(nw) == expected(conn, sel)
    ids = {i for i, *_ in accounts_by_sql(conn)}
    assert [x["id"] for x in nw["custom"]["excluded"]] == [a["id"] for a in insights.networth_accounts(conn)
                                                          if a["id"] in set(sel)]
    assert {x["id"] for x in nw["custom"]["excluded"]} == set(sel) & ids
    # Custom's net = the sum of the counted accounts' signed balances
    signed = {a["id"]: a["signed"] for a in insights.networth_accounts(conn)}
    assert nw["custom"]["net"] == pytest.approx(sum(v for i, v in signed.items() if i not in set(sel)), abs=0.005)
    # each group's custom part, and the groups adding up to Custom's net
    for g in nw["breakdown"]:
        mine = [a for a in insights.networth_accounts(conn) if a["group"] == g["key"]]
        assert g["custom"]["count"] == sum(a["id"] not in set(sel) for a in mine)
        assert g["custom"]["total"] == round(sum(a["balance"] for a in mine if a["id"] not in set(sel)), 2)
        assert g["custom"]["signed"] == (g["custom"]["total"] if g["asset"] else round(-g["custom"]["total"], 2))
        assert g["count"] == len(mine)
    assert round(sum(g["custom"]["signed"] for g in nw["breakdown"]), 2) == nw["custom"]["net"]
    # every point, the selection applied to every day
    for p in nw["history"]:
        a, d = point(conn, p["date"], sel)
        assert (p["custom_assets"], p["custom_debts"]) == (pytest.approx(a, abs=0.005), pytest.approx(d, abs=0.005)), p["date"]
        assert p["custom_net"] == pytest.approx(a - d, abs=0.011)
        aa, dd = point(conn, p["date"], (), custom=False)
        assert (p["assets"], p["debts"]) == (pytest.approx(aa, abs=0.005), pytest.approx(dd, abs=0.005))
    return nw


# --- the math, on every gate DB ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", gate_dbs.KINDS)
def test_custom_math_on_every_gate_db(data_dir, kind):
    conn = gate_db(kind)
    base = check(conn)                                              # the saved selection (full) or the first one
    everyone = insights.networth_accounts(conn)
    plaid = [a for a in everyone if a["source"] == "plaid"]
    mine = [a for a in everyone if a["source"] == "manual"]
    # a Plaid account left out: Custom moves by exactly its signed balance
    one = max(plaid, key=lambda a: abs(a["signed"]))
    nw = check(conn, [one["id"]])
    assert nw["custom"]["net"] == round(nw["net"] - one["signed"], 2)
    # a manual account left out (the empty DB has none)
    if mine:
        m = max(mine, key=lambda a: abs(a["signed"]))
        nw = check(conn, [m["id"]])
        assert nw["custom"]["net"] == round(nw["net"] - m["signed"], 2)
    # a whole group left out: Cash
    cash = [a["id"] for a in everyone if a["group"] == "cash"]
    nw = check(conn, cash)
    group = next(g for g in nw["breakdown"] if g["key"] == "cash")
    assert group["custom"] == {"count": 0, "total": 0.0, "signed": 0.0}
    assert nw["custom"]["assets"] == round(nw["assets"] - group["total"], 2) and nw["custom"]["debts"] == nw["debts"]
    # everything left out: all zero, nothing divided by zero
    nw = check(conn, [a["id"] for a in everyone])
    assert custom3(nw) == {"assets": 0.0, "debts": 0.0, "net": 0.0} and len(nw["custom"]["excluded"]) == len(everyone)
    assert all(p["custom_assets"] == p["custom_debts"] == p["custom_net"] == 0.0 for p in nw["history"])
    assert all(g["custom"]["count"] == 0 for g in nw["breakdown"])
    # nothing left out: Custom is All, every day (no manual account is archived on these DBs)
    nw = check(conn, [])
    assert custom3(nw) == {k: nw[k] for k in ("assets", "debts", "net")} and nw["custom"]["excluded"] == []
    assert all((p["custom_assets"], p["custom_debts"], p["custom_net"]) == (p["assets"], p["debts"], p["net"])
               for p in nw["history"])
    # an id that isn't an account changes nothing
    assert custom3(check(conn, ["nope", "manual:99999"])) == custom3(nw)
    if kind == "full":   # the saved selection: Cash, Credit cards and Vehicles counted in part
        ids = manual_ids(conn)
        assert set(saved(conn)) == FULL_OUT_PLAID | {ids[n] for n in FULL_OUT_MANUAL}
        parts = {g["key"]: (g["custom"]["count"], g["count"]) for g in base["breakdown"]}
        assert parts == {"cash": (3, 4), "investments": (0, 3), "vehicles": (1, 2), "property": (0, 3), "cards": (4, 5),
                         "loans": (2, 2)}
    conn.close()


def test_history_takes_todays_selection_for_every_day(data_dir):
    """One line, no jumps: leaving an account out takes it out of every point, not only from today on."""
    conn = gate_db("full")
    nw_all = insights.net_worth(conn, excluded=[])
    nw = insights.net_worth(conn, excluded=["acc_nova_sav"])
    assert len(nw["history"]) >= 14
    for p, q in zip(nw_all["history"], nw["history"]):
        sav = conn.execute("SELECT current FROM balance_snapshots WHERE account_id = 'acc_nova_sav' AND date <= ? "
                           "ORDER BY date DESC LIMIT 1", (p["date"],)).fetchone()
        assert q["custom_net"] == round(p["custom_net"] - (sav[0] if sav else 0.0), 2), p["date"]
        assert (q["assets"], q["net"]) == (p["assets"], p["net"])      # All's line doesn't move
    conn.close()


def test_a_new_account_counts_by_default(data_dir):
    conn = gate_db("full")
    before = insights.net_worth(conn, history=False)
    kept = saved(conn)
    boat = manual.create(conn, {"kind": "other_asset", "name": "Boat", "subtype": "Other", "balance": 3000.0}, TODAY)
    with conn:   # a bank account connected later (the next sync's accounts/get)
        conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
                     "updated_at) VALUES ('acc_new', 'demo_nova', 'New Vault', '0099', 'depository', 'savings', 250.0, "
                     "'USD', ?)", (db.now(),))
    nw = check(conn)
    assert saved(conn) == kept                                      # the saved list didn't change...
    assert nw["custom"]["net"] == round(before["custom"]["net"] + 3000.0 + 250.0, 2)   # ...and both count
    assert f"manual:{boat['id']}" not in {x["id"] for x in nw["custom"]["excluded"]}
    other = next(g for g in nw["breakdown"] if g["key"] == "other_assets")
    assert other["custom"] == {"count": 1, "total": 3000.0, "signed": 3000.0}
    conn.close()


def test_an_archived_manual_account_never_counts(data_dir):
    conn = gate_db("full")
    ids = manual_ids(conn)
    insights.save_networth_excluded(conn, [])                       # Custom counts everything...
    fit = int(ids["2015 Honda Fit"].split(":")[1])
    before = insights.net_worth(conn)
    manual.archive(conn, fit, TODAY)
    with conn:   # archived on the fixture's day (noon UTC: the same local day in any US zone)
        conn.execute("UPDATE manual_accounts SET archived_at = '2026-09-26T12:00:00Z' WHERE id = ?", (fit,))
    nw = check(conn)
    assert nw["custom"]["net"] == nw["net"] == round(before["net"] - 7200.0, 2)   # ...but not an archived account
    assert ids["2015 Honda Fit"] not in {a["id"] for a in insights.networth_accounts(conn)}   # not listed
    # not in the Custom line on any day, while All's line has it until the day it was archived
    h = {p["date"]: p for p in nw["history"]}
    assert h["2026-09-20"]["assets"] - h["2026-09-20"]["custom_assets"] == pytest.approx(7200.0)
    assert h[TODAY.isoformat()]["assets"] == h[TODAY.isoformat()]["custom_assets"]
    # its id can't be saved (dropped), and one saved before it was archived is simply not listed
    assert insights.save_networth_excluded(conn, [ids["2015 Honda Fit"], "acc_nova_inv"]) == (["acc_nova_inv"],
                                                                                            [ids["2015 Honda Fit"]])
    with conn:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'networth_excluded'", (json.dumps([ids["2015 Honda Fit"]]),))
    nw = check(conn)
    assert nw["custom"]["excluded"] == [] and custom3(nw) == {k: nw[k] for k in ("assets", "debts", "net")}
    conn.close()


def test_first_read_saves_the_cash_selection_once_and_logs_it(data_dir, caplog):
    conn = gate_db("nocaps")
    assert saved(conn) is None
    with caplog.at_level(logging.INFO, logger="hermes_finance.insights"):
        first = insights.networth_excluded(conn)
        again = insights.networth_excluded(conn)
        insights.net_worth(conn)
    assert first == again == saved(conn) == insights.cash_preset(conn) and first
    logged = [r for r in caplog.records if "Custom starts as the Cash & debts selection" in r.getMessage()]
    assert len(logged) == 1 and f"{len(first)} account(s) not counted" in logged[0].getMessage()
    conn.close()


def test_a_read_only_connection_saves_nothing(data_dir):
    conn = gate_db("onepoint")
    conn.close()
    ro = db.connect_readonly(config.db_path())
    assert insights.networth_excluded(ro) == insights.cash_preset(ro)
    assert custom3(insights.net_worth(ro)) == expected(ro, insights.cash_preset(ro))
    assert ro.execute("SELECT COUNT(*) FROM settings WHERE key = 'networth_excluded'").fetchone()[0] == 0
    ro.close()


# --- POST /api/settings -------------------------------------------------------------------------------------

@pytest.fixture
def site(data_dir, client):
    conn = gate_db("full")
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    app.today = lambda: TODAY
    yield app, conn
    conn.close()


def get(app, path):
    status, _, body = app.handle("GET", path, LOCAL, b"")
    return status, json.loads(body)


def post(app, payload, headers=POST, raw=None):
    status, _, body = app.handle("POST", "/api/settings", headers, raw if raw is not None else json.dumps(payload).encode())
    return status, json.loads(body)


def test_settings_save_the_selection_and_every_view_follows(site):
    app, conn = site
    assert get(app, "/api/settings")[1]["networth_excluded"] == saved(conn)
    status, d = post(app, {"networth_excluded": ["acc_nova_inv", "acc_chase_cc", "acc_nova_inv"]})
    assert status == 200 and d["networth_excluded"] == ["acc_nova_inv", "acc_chase_cc"]   # once each
    assert "note" not in d and "dropped" not in d and d["chart_excluded_categories"] == ["Rent & Utilities"]
    assert saved(conn) == ["acc_nova_inv", "acc_chase_cc"]
    want = expected(conn, saved(conn))
    acc = get(app, "/api/accounts")[1]["net_worth"]
    ov = get(app, "/api/overview")[1]["net_worth"]
    assert custom3(acc) == custom3(ov) == want and acc["custom"] == ov["custom"]
    assert {x["id"] for x in acc["custom"]["excluded"]} == {"acc_nova_inv", "acc_chase_cc"}
    # another device (a new App on the same DB) reads the same selection
    other = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    assert get(other, "/api/settings")[1]["networth_excluded"] == ["acc_nova_inv", "acc_chase_cc"]
    assert post(app, {"networth_excluded": []})[1]["networth_excluded"] == [] and saved(conn) == []


def test_unknown_ids_are_dropped_with_a_note(site):
    app, conn = site
    fit = manual_ids(conn)["2015 Honda Fit"]
    manual.archive(conn, int(fit.split(":")[1]), TODAY)
    status, d = post(app, {"networth_excluded": ["acc_nova_inv", "nope", fit, "manual:4242"]})
    assert status == 200 and d["networth_excluded"] == ["acc_nova_inv"] and saved(conn) == ["acc_nova_inv"]
    assert d["dropped"] == ["nope", fit, "manual:4242"]
    assert d["note"].startswith("3 unknown account ids dropped (not a current account): 'nope'")
    status, d = post(app, {"networth_excluded": ["x" * 500]})
    assert status == 200 and d["note"] == f"1 unknown account id dropped (not a current account): {'x' * 200!r}"


@pytest.mark.parametrize("payload,why", [
    ({}, "required"), ({"networth_excluded": "acc_nova_inv"}, "must be a list of account ids"),
    ({"networth_excluded": None}, "must be a list"), ({"networth_excluded": {"acc_nova_inv": True}}, "must be a list"),
    ({"networth_excluded": [3]}, "must be a list of account ids"), ({"networth_excluded": [["acc_nova_inv"]]}, "must be a list"),
    ({"networth_excluded": ["a"] * 1001}, "at most 1000"),
    ({"networth_excluded": [], "theme": "light"}, "unknown setting"),
    ({"networth_excluded": ["acc_nova_inv"], "chart_excluded_categories": ["Nope"]}, "not a spending category")])
def test_bad_selections_are_refused_and_nothing_is_saved(site, payload, why):
    app, conn = site
    before = conn.execute("SELECT key, value, updated_at FROM settings ORDER BY key").fetchall()
    status, d = post(app, payload)
    assert status == 400 and why in d["error"], d
    assert conn.execute("SELECT key, value, updated_at FROM settings ORDER BY key").fetchall() == before


def test_bad_json_and_the_write_gate(site):
    app, conn = site
    before = saved(conn)
    body = {"networth_excluded": []}
    assert post(app, None, raw=b"{nope") == (400, {"error": "invalid JSON"})
    assert post(app, None, raw=b'["acc_nova_inv"]') == (400, {"error": "expected a JSON object"})
    assert post(app, body, headers={**POST, "Origin": "https://evil.example"})[0] == 403          # CSRF
    assert post(app, body, headers={**POST, "Content-Type": "text/plain"})[0] == 415              # no simple POST
    assert post(app, body, headers={"Host": "evil.example:8765", "Content-Type": "application/json"})[0] == 403
    assert post(app, body, headers={**POST, "X-Forwarded-For": "1.2.3.4"})[0] == 403              # proxied on loopback
    assert saved(conn) == before


def test_both_settings_in_one_post(site):
    app, conn = site
    status, d = post(app, {"chart_excluded_categories": ["Other Shopping"], "networth_excluded": ["acc_nova_robo"]})
    assert status == 200 and d["chart_excluded_categories"] == ["Other Shopping"]
    assert d["networth_excluded"] == ["acc_nova_robo"]
    assert api.chart_excluded(conn) == ["Other Shopping"] and saved(conn) == ["acc_nova_robo"]
    # the chart setting alone leaves the selection as it is
    assert post(app, {"chart_excluded_categories": []})[1]["networth_excluded"] == ["acc_nova_robo"]


# --- finance networth --custom / --json / networth-custom ----------------------------------------------------

def test_cli_custom_text(data_dir, capsys):
    conn = gate_db("full")
    nw = insights.net_worth(conn)
    conn.close()
    code, out, err = run(["networth", "--custom"], capsys)
    assert code == 0, err
    lines = out.splitlines()
    c = nw["custom"]
    assert lines[0] == f"Net worth (custom) {report.money(c['net'])}: own {report.money(c['assets'])}, owe {report.money(c['debts'])}"
    heads = [ln.strip() for ln in lines if ln.startswith("  ") and not ln.startswith("    ")]
    cash = next(g for g in nw["breakdown"] if g["key"] == "cash")["custom"]
    assert heads[0] == f"Cash (3): {report.money(cash['signed'])}"
    assert [h.split(" (")[0] for h in heads] == ["Cash", "Vehicles", "Credit cards", "Loans"]   # Investments, Property out
    for gone in ("Emergency Fund Vault", "Honda Fit", "Sapphire  ", "Condo", "401(k)"):
        assert gone not in "\n".join(lines[:-2])
    assert "2019 Zenith Mesa" in out and lines[-2].startswith("(not counted: ") and "without --custom" in lines[-2]
    code, all_out, _ = run(["networth"], capsys)
    assert "Emergency Fund Vault ••6084 [not counted in Custom]" in all_out
    assert f"(Custom, finance networth --custom: {report.money(c['net'])}, 9 accounts not counted)" in all_out
    code, hist, _ = run(["networth", "--custom", "--history"], capsys)
    rows = [[x.strip() for x in ln.split("|")] for ln in hist.splitlines() if ln[:4].isdigit()]
    last = nw["history"][-1]
    assert rows[-1] == [last["date"], report.money(last["custom_assets"]), report.money(last["custom_debts"]),
                        report.money(last["custom_net"])]


def test_cli_json_ids_and_custom(data_dir, capsys):
    conn = gate_db("full")
    nw = insights.net_worth(conn)
    ids = manual_ids(conn)
    conn.close()
    full = cli_json(capsys, "networth")
    custom = cli_json(capsys, "networth", "--custom")
    assert full["mode"] == "all" and custom["mode"] == "custom" and full["custom"] == custom["custom"] == nw["custom"]
    assert set(full["custom"]) == {"assets", "debts", "net", "excluded"}
    assert all({"id", "name", "kind", "balance"} <= set(x) for x in full["custom"]["excluded"])
    accts = [a for g in full["groups"] for a in g["accounts"]]
    for a in accts:
        assert a["nw_id"] == (a["id"] if a["source"] == "plaid" else f"manual:{a['id']}")
        assert a["counted"] == (a["nw_id"] not in FULL_OUT_PLAID | {ids[n] for n in FULL_OUT_MANUAL})
    assert all(g["custom"] == next(b for b in nw["breakdown"] if b["key"] == g["key"])["custom"] for g in full["groups"])
    assert (custom["assets"], custom["debts"], custom["net"]) == tuple(nw["custom"][k] for k in ("assets", "debts", "net"))
    assert [g["key"] for g in custom["groups"]] == ["cash", "vehicles", "cards", "loans"]
    assert all(a["counted"] for g in custom["groups"] for a in g["accounts"])
    assert all(g["count"] == len(g["accounts"]) for g in custom["groups"])
    assert round(sum(g["signed"] for g in custom["groups"]), 2) == custom["net"]
    assert round(sum(a["signed"] for g in custom["groups"] for a in g["accounts"]), 2) == custom["net"]
    assert "cash_only" not in custom
    hist = cli_json(capsys, "networth", "--custom", "--history")["history"]
    assert hist == nw["history"]


def test_networth_custom_lists_and_edits(data_dir, capsys):
    conn = gate_db("full")
    ids = manual_ids(conn)
    conn.close()
    listed = cli_json(capsys, "networth-custom")
    assert listed["changed"] is None and len(listed["accounts"]) == 19
    assert {a["id"] for a in listed["accounts"] if not a["counted"]} == set(listed["excluded"]) \
        == FULL_OUT_PLAID | {ids[n] for n in FULL_OUT_MANUAL}
    assert [a["group"] for a in listed["accounts"]] == sorted((a["group"] for a in listed["accounts"]),
                                                              key=[k for k, *_ in insights.BREAKDOWN].index)
    code, text, _ = run(["networth-custom", "--list"], capsys)
    assert code == 0 and text.splitlines()[0].startswith("Custom net worth ") and "9 of 19 accounts not counted" in text
    assert re.search(r"no\s+\| Investments\s+\| Nova Self-directed ••6820\s+\| \$491\.62\s+\| acc_nova_inv", text)

    d = cli_json(capsys, "networth-custom", "--include", "acc_nova_inv", ids["Sapphire"], "--exclude", "acc_usb_chk")
    assert d["changed"] is True and "acc_nova_inv" not in d["excluded"] and "acc_usb_chk" in d["excluded"]
    conn = db.connect(config.db_path())
    assert set(saved(conn)) == set(d["excluded"]) and d["custom"] == custom3(insights.net_worth(conn, history=False))
    conn.close()
    assert cli_json(capsys, "networth-custom", "--exclude", "acc_usb_chk")["changed"] is False
    assert cli_json(capsys, "networth-custom", "--reset")["excluded"] == []
    d = cli_json(capsys, "networth-custom", "--preset", "cash-and-debts")
    conn = db.connect(config.db_path())
    assert d["excluded"] == insights.cash_preset(conn) and saved(conn) == insights.cash_preset(conn)
    d = cli_json(capsys, "networth-custom", "--preset", "cash-and-debts", "--include", ids["2019 Zenith Mesa"])
    assert set(d["excluded"]) == set(insights.cash_preset(conn)) - {ids["2019 Zenith Mesa"]}
    before = saved(conn)
    code, out, err = run(["networth-custom", "--exclude", "acc_nova_inv", "nope"], capsys)
    assert code == 2 and out == "" and "not a current account: 'nope'" in err
    code, out, err = run(["networth-custom", "--exclude", "acc_nova_inv", "--include", "acc_nova_inv"], capsys)
    assert code == 2 and "both --exclude and --include" in err
    code, _, err = run(["networth-custom", "--reset", "--preset", "cash-and-debts"], capsys)
    assert code == 2 and "not allowed with" in err
    assert saved(conn) == before                                    # nothing saved by a refused run
    conn.close()


def test_help_and_docs(capsys):
    code, out, _ = run(["networth", "--help"], capsys)
    assert code == 0 and "--custom" in out and "nw_id" in out and "custom_net" in out and "networth-custom" in out
    code, out, _ = run(["networth-custom", "--help"], capsys)
    assert code == 0 and all(f in out for f in ("--exclude", "--include", "--reset", "--preset", "--list", "cash-and-debts"))
    readme = (ROOT / "README.md").read_text()
    assert "networth [--history] [--custom] [--json]" in readme and "finance networth-custom" in readme
    assert "networth_excluded" in readme and "All · Custom" in readme


def keys(x) -> set[str]:
    if isinstance(x, dict):
        return set(x) | {k for v in x.values() for k in keys(v)}
    return {k for v in x for k in keys(v)} if isinstance(x, list) else set()


def test_the_names_are_all_and_custom(data_dir, capsys, client):
    """The owner, 2026-09-27: the switch reads All · Custom and the flag is --custom, never "My pick" / "--pick", in the UI
    text, the CLI, the JSON keys, the help or the README."""
    src = ROOT / "src" / "hermes_finance"
    for path in [*src.glob("*.py"), src / "static" / "app.js", src / "static" / "app.css", ROOT / "README.md"]:
        assert not re.search(r"my pick|--pick\b", path.read_text(), re.IGNORECASE), path
    assert "[['all', 'All'], ['custom', 'Custom']]" in (src / "static" / "app.js").read_text()
    gate_db("full").close()
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    payloads = [cli_json(capsys, "networth", "--history"), cli_json(capsys, "networth", "--custom", "--history"),
                cli_json(capsys, "networth-custom"), get(app, "/api/settings")[1], get(app, "/api/accounts")[1]["net_worth"],
                get(app, "/api/overview")[1]["net_worth"]]
    found = {k for p in payloads for k in keys(p)}
    assert {"custom", "excluded", "nw_id", "counted", "custom_net", "networth_excluded"} <= found
    assert not [k for k in found if "pick" in k.lower()]
    for argv in (["networth", "--help"], ["networth-custom", "--help"], ["--help"]):
        code, out, _ = run(argv, capsys)
        assert code == 0 and "pick" not in out.lower(), argv
