"""The PDF spike's seam, negative controls and locale controls.

The repository's own venv has no pdfplumber and no fpdf2: this module skips cleanly there. In the
disposable venv (`uv pip install --python .dispatch/venv/bin/python pdfplumber fpdf2`) it runs the
whole committed corpus and proves the seam's contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "pdfspike"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import pdfspike_corpus as corpus  # noqa: E402
import pdfspike_layout as layout  # noqa: E402
from hermes_finance.importfile import Row  # noqa: E402

MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
EXTRAS = {item["id"]: item for item in MANIFEST.get("extra_files", [])}
RESULTS = json.loads((FIXTURES / "results.json").read_text())
TOLERANCE_CENTS = 1


def _cents(value: float) -> int:
    return int(round(value * 100))


def _data(item: dict) -> bytes:
    return (FIXTURES / f"{item['id']}.pdf").read_bytes()


def _reconciles(opening: float, rows: list[Row], closing: float) -> bool:
    delta = _cents(closing) - (_cents(opening) - sum(_cents(row.amount) for row in rows))
    return abs(delta) <= TOLERANCE_CENTS


@pytest.mark.parametrize("item", MANIFEST["files"], ids=[f["id"] for f in MANIFEST["files"]])
def test_every_statement_has_the_exact_rows_and_reconciles(item):
    data = _data(item)
    assert hashlib.sha256(data).hexdigest() == item["sha256"]
    rows = layout.rows(data, profile=item["profile"])
    assert len(rows) == item["expected_rows"]
    assert _reconciles(item["opening_balance"], rows, item["closing_balance"])


def test_results_json_is_reproduced_by_this_run():
    passes, countries = 0, set()
    for item in MANIFEST["files"]:
        rows = layout.rows(_data(item), profile=item["profile"])
        ok = len(rows) == item["expected_rows"] and _reconciles(
            item["opening_balance"], rows, item["closing_balance"])
        if ok:
            passes += 1
            countries.add(item["country"])
    assert RESULTS["summary"]["passes"] == passes
    assert RESULTS["summary"]["denominator"] == len(MANIFEST["files"])
    assert RESULTS["gate"]["required"] == "16/20 required"
    assert RESULTS["gate"]["pass"] == (passes >= 16 and len(countries) >= 6)
    assert RESULTS["environment"]["pdfplumber"]


def test_misaligned_header_reconciles_or_refuses():
    item = EXTRAS["x-misaligned"]
    data = (FIXTURES / f"{item['id']}.pdf").read_bytes()
    assert hashlib.sha256(data).hexdigest() == item["sha256"]
    try:
        rows = layout.rows(data, profile=item["profile"])
    except layout.ReconcileMismatch as exc:
        assert exc.code == "does_not_reconcile"
        assert "CSV or OFX" in exc.message
    else:
        assert len(rows) == item["expected_rows"]
        assert _reconciles(item["opening_balance"], rows, item["closing_balance"])


@pytest.mark.parametrize("id", ["x-two-row", "x-yearless", "x-no-balance"])
def test_extra_layouts_reconcile(id):
    item = EXTRAS[id]
    data = (FIXTURES / f"{item['id']}.pdf").read_bytes()
    assert hashlib.sha256(data).hexdigest() == item["sha256"]
    rows = layout.rows(data, profile=item["profile"])
    assert len(rows) == item["expected_rows"]
    assert _reconciles(item["opening_balance"], rows, item["closing_balance"])
    for row, want in zip(rows, item["expected_rows_detail"]):
        assert row.date == want["date"]
        assert row.amount == pytest.approx(want["amount"], abs=0.005)


def test_no_balance_statement_is_refused():
    item = EXTRAS["x-no-balances"]
    with pytest.raises(layout.NoBalances) as exc:
        layout.rows((FIXTURES / f"{item['id']}.pdf").read_bytes(), profile=item["profile"])
    assert exc.value.code == "no_balances"
    assert "CSV or OFX" in exc.value.message


def test_no_opening_statement_is_refused():
    # A running balance alone doesn't anchor row 1: with no printed opening balance the seam
    # must refuse rather than derive one from the first row's own (possibly wrong) amount.
    item = EXTRAS["x-no-opening"]
    data = _data(item)
    assert hashlib.sha256(data).hexdigest() == item["sha256"]
    with pytest.raises(layout.NoBalances) as exc:
        layout.rows(data, profile=item["profile"])
    assert exc.value.code == "no_balances"
    assert "opening balance" in exc.value.message
    assert "CSV or OFX" in exc.value.message


def test_reviewer_portrait_layout_reconciles_or_refuses():
    item = EXTRAS["x-reviewer-left"]
    data = _data(item)
    assert hashlib.sha256(data).hexdigest() == item["sha256"]
    try:
        rows = layout.rows(data, profile=item["profile"])
    except layout.ReconcileMismatch as exc:
        assert exc.code == "does_not_reconcile"
        assert "CSV or OFX" in exc.message
    else:
        assert len(rows) == item["expected_rows"]
        assert _reconciles(item["opening_balance"], rows, item["closing_balance"])
        for row, want in zip(rows, item["expected_rows_detail"]):
            assert row.date == want["date"]
            assert row.amount == pytest.approx(want["amount"], abs=0.005)


def test_closing_balance_mismatch_beyond_the_tolerance_is_refused():
    # The brief's tolerance is 0.01, so a one-cent rounding difference is accepted; this fixture is
    # two cents out, which is the smallest mismatch the rule can refuse.
    item = EXTRAS["x-off-cent"]
    with pytest.raises(layout.ReconcileMismatch) as exc:
        layout.rows((FIXTURES / f"{item['id']}.pdf").read_bytes(), profile=item["profile"])
    assert exc.value.code == "does_not_reconcile"
    assert "don't add up" in exc.value.message
    assert "CSV or OFX" in exc.value.message


def test_sha256_pins_every_committed_pdf():
    for item in MANIFEST["files"] + MANIFEST.get("extra_files", []):
        assert hashlib.sha256(_data(item)).hexdigest() == item["sha256"], item["id"]


def test_locked_pdf_needs_the_right_password():
    data = (FIXTURES / "locked.pdf").read_bytes()
    with pytest.raises(layout.PasswordRequired) as missing:
        layout.rows(data, profile="AU")
    assert missing.value.code == "password_required_or_wrong"
    with pytest.raises(layout.PasswordRequired) as wrong:
        layout.rows(data, "wrong-password", profile="AU")
    assert wrong.value.code == "password_required_or_wrong"
    assert len(layout.rows(data, corpus.LOCKED_PASSWORD, profile="AU")) == 4


def test_scanned_pdf_says_export_csv_or_ofx():
    with pytest.raises(layout.NoTextLayer) as exc:
        layout.rows((FIXTURES / "scan.pdf").read_bytes(), profile="AU")
    assert exc.value.code == "no_text_layer"
    assert "looks scanned, export CSV/OFX instead" in exc.value.message


def test_multi_account_pdf_is_refused():
    with pytest.raises(layout.MultiAccountUnsupported) as exc:
        layout.rows((FIXTURES / "multi_account.pdf").read_bytes(), profile="AU")
    assert exc.value.code == "multi_account"


def test_text_without_transactions_is_no_rows():
    with pytest.raises(layout.NoRows) as exc:
        layout.rows((FIXTURES / "empty.pdf").read_bytes(), profile="AU")
    assert exc.value.code == "no_rows"


def test_negative_control_codes_are_the_recorded_ones():
    observed = {item["id"]: item["observed"] for item in RESULTS["negative_controls"]}
    assert observed["locked_missing"] == "password_required_or_wrong"
    assert observed["locked_wrong"] == "password_required_or_wrong"
    assert observed["locked_right"] == "rows:4"
    assert observed["scan"] == "no_text_layer"
    assert observed["multi_account"] == "multi_account"
    assert observed["empty"] == "no_rows"


def test_engine_missing_is_a_category_not_a_traceback(monkeypatch):
    monkeypatch.setitem(sys.modules, "pdfplumber", None)
    with pytest.raises(layout.PdfEngineMissing) as exc:
        layout.rows(b"%PDF-1.4", profile="AU")
    assert exc.value.code == "no_engine"
    assert "pdfplumber" in exc.value.message


def test_rows_are_importfile_rows_with_the_app_sign_and_iso_dates():
    item = next(f for f in MANIFEST["files"] if f["id"] == "au-01")
    rows = layout.rows(_data(item), profile="AU")
    assert all(isinstance(row, Row) for row in rows)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.date) for row in rows)
    assert all(row.account is None and row.fitid is None and row.trntype is None for row in rows)
    assert [row.line for row in rows] == list(range(1, len(rows) + 1))
    expected = item["expected_rows_detail"]
    assert any(row.amount > 0 for row in rows)          # a withdrawal, money out
    assert any(row.amount < 0 for row in rows)          # a deposit, money in
    for row, want in zip(rows, expected):
        assert row.amount == pytest.approx(want["amount"], abs=0.005)


def _mini(spec: corpus.Spec) -> list[Row]:
    data, _pages = corpus.build_statement(spec)
    return layout.rows(data, profile=spec.country)


def _assert_matches_expected(spec: corpus.Spec, rows: list[Row]) -> None:
    expected, _opening, _closing = corpus._txns(spec)
    assert len(rows) == len(expected)
    for row, want in zip(rows, expected):
        assert row.date == want.day.isoformat()
        assert row.amount == pytest.approx(float(want.app_amount), abs=0.005)


def test_locale_decimal_comma_and_dot_thousands():
    spec = corpus.Spec("t-br", "BR", "token", 4, 11, "5000.00", [], token=True)
    _assert_matches_expected(spec, _mini(spec))


def test_locale_indian_lakh_grouping():
    spec = corpus.Spec("t-in", "IN", "in", 4, 12, "100000.00", [], stacked=True)
    rows = _mini(spec)
    _assert_matches_expected(spec, rows)
    assert any(row.amount == pytest.approx(123456.78) for row in rows)


def test_locale_cr_dr_suffix():
    spec = corpus.Spec("t-sg", "SG", "split", 4, 13, "5000.00", [], suffix=True)
    _assert_matches_expected(spec, _mini(spec))


def test_locale_dr_cr_marker_column():
    spec = corpus.Spec("t-sg2", "SG", "marker", 4, 14, "5000.00", [], marker=True)
    _assert_matches_expected(spec, _mini(spec))


def test_locale_trailing_minus():
    spec = corpus.Spec("t-us1", "US", "signed", 4, 15, "5000.00", [], negative_style="trailing")
    _assert_matches_expected(spec, _mini(spec))


def test_locale_parentheses_negative():
    spec = corpus.Spec("t-us2", "US", "signed", 4, 16, "5000.00", [], negative_style="paren")
    _assert_matches_expected(spec, _mini(spec))


def test_locale_plus_minus_token():
    spec = corpus.Spec("t-br2", "BR", "token", 4, 17, "5000.00", [], token=True)
    _assert_matches_expected(spec, _mini(spec))


def test_locale_ambiguous_date_is_resolved_by_the_profile():
    assert layout._profile_date("05/06/2026", layout.PROFILES["US"]) == date(2026, 5, 6)
    assert layout._profile_date("05/06/2026", layout.PROFILES["AU"]) == date(2026, 6, 5)
    assert layout._profile_date("05/06/2026", layout.PROFILES["BR"]) == date(2026, 6, 5)


def test_locale_preamble_and_summary_lines_are_not_rows():
    sg = corpus.Spec("t-sg3", "SG", "split", 5, 18, "5000.00", [], suffix=True,
                     summary_above=True, opening_above=False)
    _assert_matches_expected(sg, _mini(sg))
    us = corpus.Spec("t-us3", "US", "signed", 5, 19, "5000.00", [], summary_below=True)
    _assert_matches_expected(us, _mini(us))


def test_non_amount_parenthesised_document_number_is_not_an_amount():
    spec = corpus.Spec("t-in2", "IN", "in", 4, 20, "100000.00", [], stacked=True,
                       references=True, document_number=True)
    rows = _mini(spec)
    _assert_matches_expected(spec, rows)
    assert all(abs(row.amount) != pytest.approx(1000055.00) for row in rows)
