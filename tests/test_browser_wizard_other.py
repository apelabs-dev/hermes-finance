"""t_0db639c5 in headless Chrome (tests/cdp.py): step 6 of the first-run set-up, "Suggested budgets".

An import-only user's debits land in Other, which `admin.suggestions` skips, so step 6 used to claim "No spending in
the last 60 days yet" while the window did have spending. With `other_spend` from /api/setup/suggestions the page says
the spending is uncategorized and links to the Other filter; a truly empty DB keeps the old sentence. Synthetic rows
only, on throwaway loopback servers.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from cdp import Chrome, Server, find_chrome
from hermes_finance import db, importer

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()
HEAD = ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
        "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n")
OLD = "No spending in the last 60 days yet."
NEW = "is still uncategorized"


def trn(amount: str, name: str, fitid: str) -> str:
    return (f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{TODAY.strftime('%Y%m%d')}<TRNAMT>{amount}<FITID>{fitid}<NAME>{name}"
            f"</STMTTRN>")


def ofx(*trns: str) -> bytes:
    """An OFX 1 (SGML) bank statement with these transactions, all posted today (inside the wizard's 60-day window
    and inside the current month, so the Transactions link shows them without a month parameter)."""
    return (HEAD + "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS></SONRS></SIGNONMSGSRSV1>"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>999999999"
            "<ACCTID>9990001234<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST>" + "".join(trns) + "</BANKTRANLIST>"
            "<LEDGERBAL><BALAMT>1000.00<DTASOF>20260926</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>").encode()


@pytest.fixture(scope="module")
def browser():
    with Chrome() as b:
        yield b


def other_only(tmp_path) -> Path:
    """A first-run DB whose only spending came from an import: both debits in Other (no rule, no Plaid category)."""
    path = tmp_path / "other-only" / "finance.db"
    conn = db.connect(path)
    out = importer.commit(conn, "acceptance.ofx", ofx(trn("-12.34", "ACCEPTANCE COFFEE", "ACC-1"),
                                                     trn("-45.67", "ACCEPTANCE GROCER", "ACC-2")), {}, TODAY)
    assert out["new"] == 2
    assert {r[0] for r in conn.execute("SELECT category FROM v_transactions")} == {"Other"}
    conn.close()
    return path


def empty(tmp_path) -> Path:
    path = tmp_path / "empty" / "finance.db"
    db.connect(path).close()
    return path


def step6(b: Chrome, srv: Server) -> dict:
    b.size(1440, 900)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/setup?step=6")
    b.settle()
    return b.js("({title: document.querySelector('#page h2').textContent,"
                " text: document.querySelector('#page .sheet').textContent,"
                " links: [...document.querySelectorAll('#page .sheet a')].map((a) => [a.textContent, a.getAttribute('href')])})")


def test_a_window_whose_spending_is_all_in_other_says_so_and_links_to_it(browser, tmp_path):
    srv = Server(other_only(tmp_path), today=TODAY)
    try:
        page = step6(browser, srv)
        assert page["title"] == "Suggested budgets"
        assert NEW in page["text"] and OLD not in page["text"]
        assert page["text"].count("Your spending from the last 60 days is still uncategorized.") == 1
        assert page["links"] == [["Transactions (Other)", "#/transactions?category=Other"]]
        # the link goes where it says: Transactions, filtered to Other, showing the imported rows
        browser.js("document.querySelector('#page .sheet a').click()")
        browser.js("new Promise((r) => { const t = () => location.hash.startsWith('#/transactions') &&"
                   " document.querySelectorAll('#page .tx tr[data-tid]').length ? r(1) : setTimeout(t, 50); t(); })")
        browser.settle()
        assert browser.js("location.hash") == "#/transactions?category=Other"
        assert browser.js("document.getElementById('page').textContent").count("ACCEPTANCE") == 2
        assert browser.problems() == []
    finally:
        srv.close()


def test_a_truly_empty_database_keeps_the_old_sentence(browser, tmp_path):
    srv = Server(empty(tmp_path), today=TODAY)
    try:
        page = step6(browser, srv)
        assert page["title"] == "Suggested budgets"
        assert OLD in page["text"] and NEW not in page["text"]
        assert page["links"] == []
        assert browser.problems() == []
    finally:
        srv.close()
