"""Multi-currency import (t_3f83e261): OFX CURDEF end to end, per-transaction CURRENCY blocks, and the CSV picker.

No FX: every row keeps the amount the file gave and its own code; a symbol takes the picked currency
(a '$' is not necessarily USD) and an explicit ISO code wins. Preview, commit, the transactions API and
the CSV export all read back the same code.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from datetime import date

import pytest

from hermes_finance import api, cli, config, importfile, importer, reads

TODAY = date(2026, 11, 15)


def ofx_bank(rows: str) -> bytes:
    return f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0
<SEVERITY>INFO
</STATUS><DTSERVER>20261115120000</DTSERVER><LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0
<SEVERITY>INFO
</STATUS><STMTRS>
<CURDEF>KRW
<BANKACCTFROM><BANKID>999999999<ACCTID>9990007788<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20261101<DTEND>20261115
{rows}
</BANKTRANLIST>
<LEDGERBAL><BALAMT>12000.00<DTASOF>20261115</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
""".encode()


KRW_ROWS = """<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261102<TRNAMT>-12000.00<FITID>K1<NAME>Seoul groceries</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261103<TRNAMT>-3500.50<FITID>K2<NAME>Seoul coffee</STMTTRN>"""


def commit(conn, filename: str, raw: bytes, data: dict) -> dict:
    return importer.commit(conn, filename, raw, data, TODAY)


def test_krw_ofx_keeps_curdef_amounts_and_codes_end_to_end(data_dir, conn):
    raw = ofx_bank(KRW_ROWS)
    p = importer.preview(conn, "seoul.ofx", raw, {}, TODAY)
    assert p["kind"] == "ofx" and p["currency"] == "KRW"
    assert [(g["currency"], g["rows"]) for g in p["groups"]] == [("KRW", 2)]
    r = commit(conn, "seoul.ofx", raw, {})
    assert r["new"] == 2 and r["accounts"][0]["account"]["currency"] == "KRW"
    rows = conn.execute("SELECT transaction_id, amount, iso_currency FROM transactions ORDER BY date").fetchall()
    assert [(x["amount"], x["iso_currency"]) for x in rows] == [(12000.0, "KRW"), (3500.5, "KRW")]
    assert conn.execute("SELECT currency FROM manual_accounts").fetchone()[0] == "KRW"

    # the API keeps the row's own amount and code, and its day total is one figure per currency
    month = api.transactions(conn, {"month": "2026-11"})
    by_id = {x["transaction_id"]: x for x in month["rows"]}
    assert {x["iso_currency"] for x in by_id.values()} == {"KRW"}
    assert {round(x["amount"], 2) for x in by_id.values()} == {12000.0, 3500.5}
    day = month["days"]["2026-11-02"]
    assert day["totals"] == {"KRW": 12000.0} and day["total"] == 0.0   # display currency has no row that day

    # the CSV export carries both columns
    out = io.StringIO()
    with conn:
        pass
    path = config.db_path()
    csv_path = path.parent / "export.csv"
    assert cli.main(["export", "--since", "2026-11-01", "--csv", str(csv_path)]) == 0
    got = list(csv.DictReader(io.StringIO(csv_path.read_text())))
    assert {(x["iso_currency"], round(float(x["amount"]), 2)) for x in got} == {("KRW", 12000.0), ("KRW", 3500.5)}


def test_ofx_per_transaction_currency_block_is_kept_or_refused(data_dir, conn):
    one = ofx_bank("<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261102<TRNAMT>-12.00<FITID>X1<NAME>EUR shop"
                   "<CURRENCY><CURRATE>1.08<CURSYM>EUR</CURSYM></CURRENCY></STMTTRN>")
    p = importer.preview(conn, "one.ofx", one, {}, TODAY)
    assert p["currency"] == "KRW"                       # the statement's CURDEF
    r = commit(conn, "one.ofx", one, {})
    assert r["new"] == 1
    assert conn.execute("SELECT iso_currency FROM transactions").fetchone()[0] == "EUR"   # the row's explicit code wins

    two = ofx_bank("<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261102<TRNAMT>-12.00<FITID>X2<NAME>Mixed"
                   "<CURRENCY><CURSYM>USD</CURSYM></CURRENCY><ORIGCURRENCY><CURSYM>EUR</CURSYM></ORIGCURRENCY>"
                   "</STMTTRN>")
    with pytest.raises(importfile.ImportFileError, match="more than one currency"):
        importfile.ofx_statements(two)
    with pytest.raises(importer.ImportRefused):
        importer.preview(conn, "two.ofx", two, {}, TODAY)


def _csv(rows: str) -> bytes:
    return ("Date,Description,Amount,Account\n" + rows).encode()


def test_csv_picker_symbol_and_explicit_codes(data_dir, conn):
    raw = _csv("""11/02/2026,Coffee,$4.50,Everyday
11/03/2026,Book,EUR 12.00,Everyday
11/04/2026,Tea,€3.25,Everyday
""")
    p = importer.preview(conn, "mixed.csv", raw, {"currency": "USD"}, TODAY)
    assert p["currency"] == "USD" and set(p["currencies"]) == {"$", "EUR", "€"}
    r = commit(conn, "mixed.csv", raw, {"currency": "USD"})
    assert r["new"] == 3
    rows = {x["name"]: x for x in conn.execute("SELECT name, amount, iso_currency FROM transactions").fetchall()}
    assert (rows["Coffee"]["amount"], rows["Coffee"]["iso_currency"]) == (4.5, "USD")   # bare $ -> picked currency
    assert (rows["Book"]["amount"], rows["Book"]["iso_currency"]) == (12.0, "EUR")      # explicit code wins
    assert (rows["Tea"]["amount"], rows["Tea"]["iso_currency"]) == (3.25, "USD")        # € -> the picked currency

    # a file picked as KRW keeps a bare $ as KRW: a dollar sign is not a promise of USD
    krw = _csv("11/02/2026,Lunch,$9,Wallet\n")
    commit(conn, "krw.csv", krw, {"currency": "KRW"})
    assert conn.execute("SELECT amount, iso_currency FROM transactions WHERE name = 'Lunch'").fetchone()[0:2] == (9.0, "KRW")


def test_csv_ambiguous_currency_is_reported(data_dir, conn):
    raw = _csv("11/02/2026,Confusing,USD 12.00 EUR,Everyday\n")
    with pytest.raises(importer.ImportRefused, match="more than one currency"):
        importer.preview(conn, "bad.csv", raw, {"currency": "USD"}, TODAY)
    raw = _csv("11/02/2026,Two,1.00 EUR,Everyday\n11/03/2026,Three,1.00 CAD,Everyday\n")
    # two rows with two codes is fine (each row is labelled); preview says what is in the file
    p = importer.preview(conn, "two.csv", raw, {"currency": "USD"}, TODAY)
    assert set(p["currencies"]) == {"EUR", "CAD"}


def test_import_defaults_to_the_target_account_currency(data_dir, conn):
    with conn:
        conn.execute("INSERT INTO manual_accounts (kind, name, institution, subtype, balance, balance_as_of, "
                     "created_at, updated_at, currency) VALUES ('bank', 'EUR account', 'Bank', 'checking', 100, "
                     "'2026-11-01', '2026-11-01', '2026-11-01', 'EUR')")
    targets = importer.targets(conn)
    assert targets[0]["currency"] == "EUR"
    raw = _csv("11/02/2026,Bread,3.50\n")
    p = importer.preview(conn, "eur.csv", raw, {"targets": {"eur": {"account_id": targets[0]["id"]}}}, TODAY)
    assert p["currency"] == "EUR"
    r = commit(conn, "eur.csv", raw, {"targets": {"eur": {"account_id": targets[0]["id"]}}, "currency": "EUR"})
    assert r["new"] == 1
    assert conn.execute("SELECT iso_currency FROM transactions").fetchone()[0] == "EUR"


def test_preview_and_commit_payload_keep_the_code(data_dir, conn):
    raw = _csv("11/02/2026,Coffee,$4.50,Everyday\n")
    encoded = base64.b64encode(raw).decode()
    from hermes_finance import web
    from hermes_finance.web import App
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    app.today = lambda: TODAY
    h = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
    s, _, out = app.handle("POST", "/api/import/preview", h,
                           json.dumps({"filename": "everyday.csv", "data": encoded, "currency": "CAD"}).encode())
    assert s == 200 and json.loads(out)["currency"] == "CAD"
    s, _, out = app.handle("POST", "/api/import/commit", h,
                           json.dumps({"filename": "everyday.csv", "data": encoded, "currency": "CAD"}).encode())
    assert s == 200 and json.loads(out)["accounts"][0]["account"]["currency"] == "CAD"
    assert conn.execute("SELECT iso_currency FROM transactions").fetchone()[0] == "CAD"
