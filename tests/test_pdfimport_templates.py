"""Teach-once PDF templates: fingerprints, same_layout, clean_config validation, save/find/load."""

from __future__ import annotations

import json
import math
from datetime import date

import pytest

from hermes_finance.pdfimport import PdfRefused, parse_statement, templates, words

from pdf_fixtures import statement

GOOD = [("05/03/2024", "Coffee", "10.00", "", "90.00"),
        ("14/03/2024", "Salary", "", "50.00", "140.00"),
        ("20/03/2024", "Rent", "40.00", "", "100.00")]
HEADER = ("Date", "Description", "Debit", "Credit", "Balance")
CFG = {"header_words": ["date", "description", "debit", "credit", "balance"],
       "columns": [{"role": "date", "x0": 40, "x1": 70}, {"role": "desc", "x0": 120, "x1": 200},
                   {"role": "debit", "x0": 300, "x1": 330}, {"role": "credit", "x0": 380, "x1": 410},
                   {"role": "balance", "x0": 460, "x1": 495}],
       "date_order": "DMY", "amount_sign": "explicit", "row_start": "date_or_amount"}


def fingerprint_of(raw: bytes) -> dict:
    pages, sizes = words.read(raw)
    return templates.fingerprint(pages, sizes, words.producer(raw))


def test_fingerprint_has_no_statement_text_or_numbers():
    fp = fingerprint_of(statement(GOOD))
    blob = json.dumps(fp)
    assert fp["header_roles"] == ["date", "desc", "debit", "credit", "balance"]
    assert fp["header_words"] == ["date", "description", "debit", "credit", "balance"]
    assert "Coffee" not in blob and "Salary" not in blob and "100.00" not in blob


def test_save_find_and_parse_with_saved_template(conn):
    raw = statement(GOOD)
    fp = fingerprint_of(raw)
    saved = templates.save(conn, fp, CFG, "user")
    assert saved["source"] == "user"
    assert conn.execute("SELECT COUNT(*) FROM pdf_templates").fetchone()[0] == 1

    second = statement(GOOD, period="Statement period 01/03/2024 to 31/03/2024",
                       opening="Opening balance 100.00", closing="Closing balance 100.00")
    fp2 = fingerprint_of(second)
    assert templates.same_layout(fp, fp2)
    found = templates.find(conn, fp2)
    assert found and found["id"] == saved["id"]
    assert templates.load(conn, saved["id"])["config"] == saved["config"]

    st = parse_statement(conn, second, template=found["config"], today=date(2026, 9, 28))
    assert [r.amount for r in st.rows] == [10.0, -50.0, 40.0]
    assert st.template and st.template["id"] == saved["id"]


def test_second_teach_for_same_layout_replaces_not_duplicates(conn):
    raw = statement(GOOD)
    fp = fingerprint_of(raw)
    templates.save(conn, fp, CFG, "user")
    templates.save(conn, fp, {**CFG, "date_order": "MDY"}, "ai")
    rows = conn.execute("SELECT source, config FROM pdf_templates").fetchall()
    assert len(rows) == 1 and rows[0]["source"] == "ai"
    assert json.loads(rows[0]["config"])["date_order"] == "MDY"


def test_same_layout_needs_page_size_and_shapes():
    a = {"header_roles": ["date"], "header_words": ["date"], "page_w": 612, "page_h": 792,
         "top_shape": ["xxx 999", "Date 999", "x"]}
    b = dict(a, page_w=615)
    assert not templates.same_layout(a, b)
    b = dict(a, page_w=613, top_shape=["xxx 999", "Date 999", "y"])
    assert templates.same_layout(a, b)
    b = dict(a, top_shape=["other", "other", "other"])
    assert not templates.same_layout(a, b)


@pytest.mark.parametrize("bad,reason", [
    ({**CFG, "unknown": 1}, "unknown template setting"),
    ({**CFG, "columns": [c for c in CFG["columns"] if c["role"] != "date"]}, "date column"),
    ({**CFG, "columns": [c for c in CFG["columns"] if c["role"] not in ("debit", "credit", "amount")]},
     "amount, debit or credit"),
    ({**CFG, "columns": CFG["columns"] + [{"role": "balance", "x0": 500, "x1": 510}]}, "at most one balance"),
    ({**CFG, "columns": [{"role": "date", "x0": 50, "x1": 40}] + CFG["columns"][1:]}, "x0 < x1"),
    ({**CFG, "columns": [{"role": "date", "x0": 40, "x1": 20000}] + CFG["columns"][1:]}, "out of range"),
    ({**CFG, "columns": [{"role": "date", "x0": float("nan"), "x1": 90}] + CFG["columns"][1:]}, "out of range"),
    ({**CFG, "header_words": ["date1"]}, "without digits"),
    ({**CFG, "header_words": ["x" * 41]}, "short text"),
    ({**CFG, "header_words": ["date"] * 9}, "at most 8"),
    ({**CFG, "skip_pattern": "("}, "valid pattern"),
    ({**CFG, "skip_pattern": "x" * 201}, "too long"),
    ({**CFG, "date_order": "YMD"}, "date_order"),
    ({**CFG, "amount_sign": "nope"}, "amount_sign"),
    ({**CFG, "year_placeholder": "yes"}, "true or false"),
    ({**CFG, "columns": "nope"}, "needs columns"),
    ({**CFG, "columns": [{"role": "date", "x0": 40, "x1": 90}]}, "amount, debit or credit"),
])
def test_clean_config_rejects_invalid_shapes(bad, reason):
    with pytest.raises(ValueError, match=reason):
        templates.clean_config(bad)


def test_clean_config_rejects_numbers_and_dates():
    with pytest.raises(ValueError, match="amount or a date"):
        templates.clean_config({**CFG, "skip_pattern": "opening 100.00"})
    with pytest.raises(ValueError, match="amount or a date"):
        templates.clean_config({**CFG, "skip_pattern": "on 05/03/2024"})
    with pytest.raises(ValueError, match="amount or a date"):
        templates.clean_config({**CFG, "header_words": ["date", "2024-03-05"]})
    with pytest.raises(ValueError, match="too big"):
        templates.clean_config({**CFG, "skip_pattern": "a" * 8100})


def test_client_config_with_a_number_is_refused(conn):
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, statement(GOOD), template={**CFG, "skip_pattern": "total 100.00"},
                        today=date(2026, 9, 28))
    assert caught.value.code == "template_invalid"


def test_contains_data_ignores_positions_and_ids():
    assert not templates.contains_data({"columns": [{"role": "date", "x0": 40.5, "x1": 90.25}], "ids": [1, 2, 3]})
    assert templates.contains_data({"note": "opening 100.00"})
    assert templates.contains_data({"note": "05/03/2024"})
    assert not templates.contains_data({"year": 2024})
