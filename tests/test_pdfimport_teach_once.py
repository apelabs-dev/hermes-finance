"""Teach once on statement A, then import a different statement B with the taught template.

The fixture is a headerless table: the generic parser cannot read it (`no_header`), and the template's
`table_start_pattern` names the line that starts the table. A and B share the layout (so B's masked fingerprint finds
A's saved template) and have different periods, openings, descriptions and amounts.
"""

from __future__ import annotations

from datetime import date

import pytest

from hermes_finance import importer
from hermes_finance.pdfimport import PdfRefused, parse_statement, templates, words

from pdf_fixtures import statement

A_ROWS = [("05/03/2024", "Coffee", "10.00", "", "90.00"),
          ("14/03/2024", "Salary", "", "50.00", "140.00"),
          ("20/03/2024", "Rent", "40.00", "", "100.00")]
B_ROWS = [("07/04/2024", "Books", "25.00", "", "175.00"),
          ("18/04/2024", "Refund", "", "60.00", "235.00"),
          ("22/04/2024", "Fuel", "15.00", "", "220.00")]
CFG = {"table_start_pattern": r"^transactions$",
       "columns": [{"role": "date", "x0": 40, "x1": 80}, {"role": "desc", "x0": 120, "x1": 200},
                   {"role": "debit", "x0": 300, "x1": 330}, {"role": "credit", "x0": 380, "x1": 410},
                   {"role": "balance", "x0": 460, "x1": 495}]}


def _pdf(rows, period, opening, closing) -> bytes:
    return statement(rows, header=None, period=period, opening=opening, closing=closing,
                     extra=[(40, 46, "Transactions")])


def _fingerprint(raw: bytes) -> dict:
    pages, sizes = words.read(raw)
    return templates.fingerprint(pages, sizes, words.producer(raw))


def test_teach_on_a_imports_a_different_statement_b(conn):
    raw_a = _pdf(A_ROWS, "Statement period 01/03/2024 to 31/03/2024", "Opening balance 100.00",
                 "Closing balance 100.00")
    raw_b = _pdf(B_ROWS, "Statement period 01/04/2024 to 30/04/2024", "Opening balance 200.00",
                 "Closing balance 220.00")

    # A is taught only because it reconciles; the same config then makes up the saved template.
    taught = parse_statement(None, raw_a, template=CFG)
    assert [(r.date, r.amount) for r in taught.rows] == [("2024-03-05", 10.0), ("2024-03-14", -50.0),
                                                         ("2024-03-20", 40.0)]
    saved = templates.save(conn, _fingerprint(raw_a), CFG, "user")

    # B is a different statement of the same layout: no generic parse, no template supplied by the caller.
    with pytest.raises(PdfRefused) as refused:
        parse_statement(None, raw_b)
    assert refused.value.code == "no_header"

    found = templates.find(conn, _fingerprint(raw_b))
    assert found is not None and found["id"] == saved["id"]
    first_sight = parse_statement(conn, raw_b)
    assert first_sight.template and first_sight.template["id"] == saved["id"]
    assert [(r.date, r.amount) for r in first_sight.rows] == [("2024-04-07", 25.0), ("2024-04-18", -60.0),
                                                              ("2024-04-22", 15.0)]
    # The statement's own printed opening and closing are what reconcile it; nothing from A's rows leaked into B.
    assert str(first_sight.opening) == "200.00" and str(first_sight.closing) == "220.00"
    # The saved template kept the mapping A was taught (the validator normalizes numbers and adds an empty
    # header_words list); B's rows were not part of it.
    assert saved["config"]["table_start_pattern"] == CFG["table_start_pattern"]
    assert [(c["role"], c["x0"], c["x1"]) for c in saved["config"]["columns"]] == \
           [(c["role"], float(c["x0"]), float(c["x1"])) for c in CFG["columns"]]


def test_a_teach_that_does_not_reconcile_saves_nothing(conn):
    good = _pdf(A_ROWS, "Statement period 01/03/2024 to 31/03/2024", "Opening balance 100.00",
                "Closing balance 100.00")
    wrong = _pdf(A_ROWS, "Statement period 01/03/2024 to 31/03/2024", "Opening balance 100.00",
                 "Closing balance 999.00")
    targets = {"Wrong": {"new": {"name": "Wrong", "kind": "bank"}}}
    with pytest.raises(importer.ImportRefused) as refused:   # the product's teach path, not a bare parse
        importer.commit(conn, "wrong.pdf", wrong, {"teach": True, "template": CFG, "targets": targets}, date(2024, 4, 1))
    assert refused.value.code == "does_not_reconcile"
    assert [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("pdf_templates", "transactions", "manual_accounts", "imports")] == [0, 0, 0, 0]

    # Control through the same commit path: once A reconciles, the teach does save exactly one template, so the
    # zeros above are a result of the refusal and not of a teach path that saves nothing at all.
    importer.commit(conn, "good.pdf", good,
                    {"teach": True, "template": CFG,
                     "targets": {"Good": {"new": {"name": "Good", "kind": "bank"}}}}, date(2024, 4, 1))
    assert conn.execute("SELECT COUNT(*) FROM pdf_templates").fetchone()[0] == 1
