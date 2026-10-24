"""TASK-22b: the CLI names an account as the pages do (report.account_name, through reads.account_label): Plaid's garbled
card name, with its U+FFFD junk and a trailing "...4321" that repeats the mask, reads "WELLS FARGO ACTIVE CASH VISA CARD
••4321" in finance networth, bills (last paid from), txns and status."""

from __future__ import annotations

import json
from datetime import date

import pytest

from agent_tools import run_cli
from factory import acct, bank, tx
from hermes_finance import cli, reads, report

GARBLED = "WELLS FARGO ACTIVE CASH VISA�� CARD ...4321"     # as Plaid sent it
CLEAN = "WELLS FARGO ACTIVE CASH VISA CARD ••4321"
LADWP = "Los Angeles Department of Water and Power"
TODAY = date(2026, 9, 28)


def test_one_helper_cleans_the_name():
    assert report.account_name(GARBLED, "4321") == "WELLS FARGO ACTIVE CASH VISA CARD"
    assert reads.account_label(GARBLED, "4321") == CLEAN
    assert reads.account_label(GARBLED, "9999") == "WELLS FARGO ACTIVE CASH VISA CARD ...4321 ••9999"   # not its mask: kept
    assert reads.account_label("Joint\x00 Checking​\t(401k)￼", None) == "Joint Checking (401k)"
    assert reads.account_label("��", "4321") == "Account ••4321" and reads.account_label(None, None) == "Account"
    assert reads.account_label("Checking", "1234") == "Checking ••1234"                                 # a clean name as it was


@pytest.fixture
def wf(conn, monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    bank(conn, "item_wf", "Wells Fargo")
    acct(conn, "acc_wf", "item_wf", GARBLED, type_="credit", subtype="credit card", current=295.5, limit=5000.0, mask="4321")
    tx(conn, "2026-08-30", 295.5, LADWP, name="CITY OF LA DWP", cat="Water", account_id="acc_wf", item_id="item_wf")
    return conn


@pytest.mark.parametrize("argv", [["networth"], ["txns"], ["status"], ["bills", "track", LADWP, "--every", "2m", "--varies"]])
def test_the_cli_shows_the_clean_name(wf, capsys, argv):
    code, out, err = run_cli(argv, capsys)
    assert code == 0, err
    assert CLEAN in out and "�" not in out and "...4321" not in out, out
    if argv[0] == "bills":                                                  # last paid from, and in the bills table
        assert f"from {CLEAN}" in out
        code, out, _ = run_cli(["bills", "--next", "400d"], capsys)
        assert code == 0 and CLEAN in out and "�" not in out and "...4321" not in out, out


def test_txns_json_labels_it_and_keeps_the_raw_name(wf, capsys):
    code, out, _ = run_cli(["txns", "--json"], capsys)
    [row] = json.loads(out)["rows"]
    assert code == 0 and row["account"] == CLEAN and row["account_name"] == GARBLED    # the label cleaned, the data as stored
