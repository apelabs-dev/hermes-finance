"""importer.preview/commit and the HTTP API for PDF statements: nothing written by preview, teach saved once,
currency from the target account, idempotent commits, no partial insert, reason codes in the error body."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from hermes_finance import clibrain, config, db
from hermes_finance.pdfimport import ai, templates, words
from hermes_finance.web import App

from pdf_fixtures import many_pages_pdf, statement

TODAY = date(2026, 9, 28)
H = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
GOOD = [("05/03/2024", "Coffee", "10.00", "", "90.00"),
        ("14/03/2024", "Salary", "", "50.00", "140.00"),
        ("20/03/2024", "Rent", "40.00", "", "100.00")]
CFG = {"header_words": ["date", "description", "debit", "credit", "balance"],
       "columns": [{"role": "date", "x0": 40, "x1": 70}, {"role": "desc", "x0": 120, "x1": 200},
                   {"role": "debit", "x0": 300, "x1": 330}, {"role": "credit", "x0": 380, "x1": 410},
                   {"role": "balance", "x0": 460, "x1": 495}],
       "date_order": "DMY", "amount_sign": "explicit", "row_start": "date_or_amount"}


def app_for(conn):
    app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
    app.today = lambda: TODAY
    return app


def post(app, path, body):
    s, _, out = app.handle("POST", path, H, json.dumps(body).encode())
    return s, json.loads(out)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def make_account(conn, name="Everyday Checking", currency="EUR") -> int:
    with conn:
        cur = conn.execute("INSERT INTO manual_accounts (kind, name, institution, balance, balance_as_of, created_at, "
                           "updated_at, currency) VALUES ('bank', ?, 'Added by you', 0, '2024-03-31', ?, ?, ?)",
                           (name, db.now(), db.now(), currency))
    return cur.lastrowid


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("transactions", "imports", "manual_accounts", "pdf_templates")}


def test_preview_returns_rows_and_pdf_info_and_writes_nothing(conn):
    app = app_for(conn)
    before = counts(conn)
    raw = statement(GOOD, period="Statement period 01/03/2024 to 31/03/2024")
    s, p = post(app, "/api/import/preview", {"filename": "fictional-march.pdf", "data": b64(raw)})
    assert s == 200 and p["kind"] == "pdf" and p["rows"] == 3
    assert p["groups"][0]["label"] == "Fictional march"
    assert p["groups"][0]["balance"] == 100.0 and p["groups"][0]["balance_date"] == "2024-03-31"
    assert p["pdf"]["reason"] is None and p["pdf"]["balance_mode"] == "running"
    assert p["pdf"]["period"] == ["2024-03-01", "2024-03-31"]
    assert p["pdf"]["opening"] == "100.00" and p["pdf"]["closing"] == "100.00"
    assert p["pdf"]["template"] is None and p["pdf"]["proposal"] is None and p["pdf"]["ai_used"] is False
    assert p["pdf"]["detail"]["header_words"]
    assert counts(conn) == before


def test_preview_lists_the_rows_the_check_screen_shows(conn, monkeypatch):
    """The check screen lists every row the replay accepted under the proof it prints; the PDF branch sends them
    (capped, with the total counted) so the page never has to re-read the file."""
    from hermes_finance import importer

    app = app_for(conn)
    s, p = post(app, "/api/import/preview", {"filename": "rows.pdf", "data": b64(statement(GOOD))})
    assert s == 200 and p["preview_rows_total"] == 3
    assert p["preview_in"] == 50.0 and p["preview_out"] == 50.0
    assert p["preview_rows"] == [{"date": "2024-03-05", "description": "Coffee", "amount": 10.0},
                                 {"date": "2024-03-14", "description": "Salary", "amount": -50.0},
                                 {"date": "2024-03-20", "description": "Rent", "amount": 40.0}]
    monkeypatch.setattr(importer, "PREVIEW_ROWS", 2)   # a cap: the total is still the whole statement
    s, p = post(app, "/api/import/preview", {"filename": "rows.pdf", "data": b64(statement(GOOD))})
    assert s == 200 and p["preview_rows_total"] == 3 and len(p["preview_rows"]) == 2
    assert [r["date"] for r in p["preview_rows"]] == ["2024-03-05", "2024-03-14"]


def test_a_fixable_refusal_carries_the_printed_columns_and_a_sample_of_each(conn):
    """Teach this layout needs what the parser saw: the header words, each column's band and a printed example from
    it. A refusal carries them, so the sheet can show the statement's own columns without the page reading the PDF."""
    app = app_for(conn)
    bad = [("05/03/2024", "Coffee", "10.00", "", "90.00"),
           ("14/03/2024", "Salary", "", "50.00", "140.00"),
           ("20/03/2024", "Rent", "40.00", "", "99.00")]
    s, e = post(app, "/api/import/preview", {"filename": "off.pdf", "data": b64(statement(bad))})
    assert s == 400 and e["code"] == "does_not_reconcile"
    detail = e["detail"]
    assert detail["row_count"] == 3 and len(detail["samples"]) == len(detail["columns"]) == 5
    assert "rows" not in detail                                              # a count, never the rows themselves
    assert detail["date_order"] == "DMY"                                     # resolved from 14/03: Teach starts there
    assert [c["role"] for c in detail["columns"]] == ["date", "desc", "debit", "credit", "balance"]
    assert [c["label"] for c in detail["columns"]] == ["Date", "Description", "Debit", "Credit", "Balance"]
    assert detail["samples"][0][:2] == ["05/03/2024", "14/03/2024"]          # the printed date cells
    assert "Coffee" in detail["samples"][1][0] and "10.00" in detail["samples"][2][:3]
    assert detail["samples"][3][:1] == ["50.00"] and "90.00" in detail["samples"][4][:3]
    # A hard refusal (a scan) carries no detail at all: there is nothing to teach.
    s, e = post(app, "/api/import/preview", {"filename": "pic.pdf", "data": b64(statement(GOOD, header=None))})
    assert s == 400 and e["code"] == "no_header"
    assert e["detail"]["samples"] == [] and e["detail"]["row_count"] == 0


def test_preview_then_cancel_changes_no_counts(conn):
    app = app_for(conn)
    before = counts(conn)
    raw = statement(GOOD)
    assert post(app, "/api/import/preview", {"filename": "cancel.pdf", "data": b64(raw)})[0] == 200
    assert counts(conn) == before


def test_commit_imports_rows_currency_idempotent_and_teaches_once(conn):
    app = app_for(conn)
    account = make_account(conn, currency="EUR")
    raw = statement(GOOD)
    s0, preview = post(app, "/api/import/preview", {"filename": "fictional-march.pdf", "data": b64(raw)})
    assert s0 == 200
    label = preview["groups"][0]["label"]
    body = {"filename": "fictional-march.pdf", "data": b64(raw), "teach": True, "template": CFG,
            "targets": {label: {"account_id": account}}}
    s, r = post(app, "/api/import/commit", body)
    assert s == 200 and (r["new"], r["skipped"]) == (3, 0) and r["kind"] == "pdf"
    rows = conn.execute("SELECT date, name, amount, iso_currency, account_id FROM transactions ORDER BY date").fetchall()
    assert [tuple(x) for x in rows] == [("2024-03-05", "Coffee", 10.0, "EUR", f"manual:{account}"),
                                        ("2024-03-14", "Salary", -50.0, "EUR", f"manual:{account}"),
                                        ("2024-03-20", "Rent", 40.0, "EUR", f"manual:{account}")]
    assert conn.execute("SELECT kind FROM imports").fetchone()[0] == "pdf"
    assert conn.execute("SELECT COUNT(*) FROM pdf_templates").fetchone()[0] == 1
    assert conn.execute("SELECT source FROM pdf_templates").fetchone()[0] == "user"

    s, again = post(app, "/api/import/commit", body)
    assert s == 200 and (again["new"], again["skipped"]) == (0, 3)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM pdf_templates").fetchone()[0] == 1


def headerless_reply(raw: bytes) -> clibrain.Reply:
    """A stubbed model reply for statement(GOOD, header=None): word ids of the period line and of the first row."""
    pages, _ = words.read(raw)
    _view, gid = ai.word_view(pages)
    by_text: dict[str, list[int]] = {}
    for wid, w in gid.items():
        by_text.setdefault(w.text, []).append(wid)
    text = json.dumps({"header_ids": [by_text["Statement"][0], by_text["period"][0]],
                       "columns": [{"role": "date", "ids": [by_text["05/03/2024"][0]]},
                                   {"role": "desc", "ids": [by_text["Coffee"][0]]},
                                   {"role": "debit", "ids": [by_text["10.00"][0]]},
                                   {"role": "credit", "ids": [by_text["50.00"][0]]},
                                   {"role": "balance", "ids": [by_text["90.00"][0]]}],
                       "end_ids": [], "amount_sign": "explicit", "year_missing": False})
    return clibrain.Reply(text, "claude-code", "test", 1, 1, None, 0.0)


@pytest.fixture
def brain(monkeypatch):
    monkeypatch.setenv("FINANCE_PDF_AI", "on")
    monkeypatch.setattr(clibrain, "off", lambda: False)
    monkeypatch.setattr(clibrain, "ready", lambda conn, cli_id: (Path("/bin/true"), ""))


def no_model(*args, **kwargs):
    raise AssertionError("a commit called the model")


def test_commit_saves_an_accepted_proposal_when_teach_is_true(conn, brain, monkeypatch):
    """Preview asks the model (stubbed) once; the commit sends that proposal back as the template with
    template_source 'ai', never calls the model, reconciles it locally and saves it with source 'ai'."""
    app = app_for(conn)
    raw = statement(GOOD, header=None)
    calls = []
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: calls.append(1) or headerless_reply(raw))
    s, preview = post(app, "/api/import/preview", {"filename": "teach.pdf", "data": b64(raw), "ai_template": True})
    assert s == 200 and preview["pdf"]["ai_used"] is True and len(calls) == 1
    proposal = preview["pdf"]["proposal"]
    assert proposal and preview["pdf"]["template"]["source"] == "ai"
    label = preview["groups"][0]["label"]

    monkeypatch.setattr(ai, "_ask", no_model)
    before = counts(conn)
    body = {"filename": "teach.pdf", "data": b64(raw), "teach": True, "template": proposal, "template_source": "ai",
            "targets": {label: {"new": {"name": label, "kind": "bank"}}}}
    s, r = post(app, "/api/import/commit", body)
    assert s == 200 and r["new"] == 3
    assert counts(conn) == {**before, "transactions": before["transactions"] + 3, "imports": before["imports"] + 1,
                            "manual_accounts": before["manual_accounts"] + 1,
                            "pdf_templates": before["pdf_templates"] + 1}
    row = conn.execute("SELECT config, source FROM pdf_templates").fetchone()
    assert row["source"] == "ai" and json.loads(row["config"]) == proposal


def test_a_commit_never_calls_the_model(conn, brain, monkeypatch):
    """ai_template on a commit is refused (400, ai_on_commit) before anything is read or written: a commit imports
    only what was previewed, so it can't ask the model again and commit a different proposal."""
    app = app_for(conn)
    monkeypatch.setattr(ai, "_ask", no_model)
    monkeypatch.setattr(clibrain, "complete", no_model)
    before = counts(conn)
    for raw in (statement(GOOD, header=None), statement(GOOD)):
        s, e = post(app, "/api/import/commit", {"filename": "ai.pdf", "data": b64(raw), "ai_template": True,
                                                "teach": True,
                                                "targets": {"Ai": {"new": {"name": "Ai", "kind": "bank"}}}})
        assert s == 400 and e["code"] == "ai_on_commit" and e["field"] == "ai_template"
    assert counts(conn) == before
    s, r = post(app, "/api/import/commit", {"filename": "ai.pdf", "data": b64(statement(GOOD)), "ai_template": False,
                                            "targets": {"Ai": {"new": {"name": "Ai", "kind": "bank"}}}})
    assert s == 200 and r["new"] == 3


def test_template_source_must_be_user_or_ai(conn):
    app = app_for(conn)
    before = counts(conn)
    s, e = post(app, "/api/import/commit", {"filename": "x.pdf", "data": b64(statement(GOOD)), "template": CFG,
                                            "template_source": "model", "teach": True,
                                            "targets": {"X": {"new": {"name": "X", "kind": "bank"}}}})
    assert s == 400 and e["field"] == "template_source"
    assert counts(conn) == before


def test_invalid_template_and_bad_replay_insert_nothing(conn):
    app = app_for(conn)
    raw = statement(GOOD)
    before = counts(conn)
    s, e = post(app, "/api/import/commit", {"filename": "bad.pdf", "data": b64(raw),
                                            "template": {"columns": "nope"},
                                            "targets": {"Bad": {"new": {"name": "Bad", "kind": "bank"}}}})
    assert s == 400 and e["code"] == "template_invalid" and e["field"] == "file"
    assert counts(conn) == before

    bad = statement(GOOD[:2] + [("20/03/2024", "Rent", "40.00", "", "99.00")])
    s, e = post(app, "/api/import/commit", {"filename": "bad2.pdf", "data": b64(bad),
                                            "targets": {"Bad": {"new": {"name": "Bad", "kind": "bank"}}}})
    assert s == 400 and e["code"] == "does_not_reconcile"
    assert counts(conn) == before


def test_bad_password_is_a_400_with_code_and_password_field(conn):
    app = app_for(conn)
    raw = statement(GOOD, password="synthetic-password")
    s, e = post(app, "/api/import/preview", {"filename": "locked.pdf", "data": b64(raw), "password": "wrong"})
    assert s == 400 and e["code"] == "password_wrong" and e["field"] == "password"
    assert "wrong" not in e["error"]
    s, e = post(app, "/api/import/preview", {"filename": "locked.pdf", "data": b64(raw)})
    assert s == 400 and e["code"] == "password_required" and e["field"] == "password"


def test_reading_a_pdf_never_logs_or_stores_the_password_or_description(conn, caplog):
    app = app_for(conn)
    caplog.set_level(logging.DEBUG)
    account = make_account(conn, currency="USD")
    password = "synthetic-secret-123"
    raw = statement([("05/03/2024", "Fictional Coffee Shop", "10.00", "", "90.00"),
                     ("14/03/2024", "Salary", "", "50.00", "140.00"),
                     ("20/03/2024", "Rent", "40.00", "", "100.00")], password=password)
    s, p = post(app, "/api/import/preview", {"filename": "private.pdf", "data": b64(raw), "password": password,
                                             "template": CFG})
    assert s == 200
    s, r = post(app, "/api/import/commit", {"filename": "private.pdf", "data": b64(raw), "password": password,
                                            "teach": True, "template": CFG,
                                            "targets": {"Private": {"account_id": account}}})
    assert s == 200 and r["new"] == 3
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert password not in text
    assert "Fictional Coffee Shop" not in text
    assert "10.00" not in text
    assert json.dumps(CFG, sort_keys=True) not in text

    stored = []
    for table in ("transactions", "imports", "pdf_templates", "manual_accounts"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            stored.append(" ".join(str(v) for v in tuple(row) if v is not None))
    blob = "\n".join(stored)
    assert password not in blob
    assert json.dumps(CFG, sort_keys=True) not in blob


def test_no_model_for_a_local_parse_or_any_refusal(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a model was called")

    monkeypatch.setattr(ai, "_ask", boom)
    app = app_for(conn)
    assert post(app, "/api/import/preview", {"filename": "local.pdf",
                                             "data": b64(statement(GOOD))})[0] == 200
    for raw, code in ((statement(GOOD, header=None), "no_header"),
                      (statement([], opening="Opening balance 100.00", closing="Closing balance 100.00"), "no_rows")):
        s, e = post(app, "/api/import/preview", {"filename": "refused.pdf", "data": b64(raw)})
        assert s == 400 and e["code"] == code


# A layout the generic parser can't read (its header words aren't the usual ones) but a taught template can.
ODD_HEADER = ("Date", "Text", "Out", "In", "Total")
ODD_CFG = {**CFG, "header_words": ["date", "text", "out", "in", "total"]}
APRIL = [("03/04/2024", "Coffee", "10.00", "", "990.00"),
         ("09/04/2024", "Salary", "", "50.00", "1040.00"),
         ("17/04/2024", "Rent", "40.00", "", "1000.00"),
         ("25/04/2024", "Books", "5.00", "", "995.00")]
MAY = [("06/05/2024", "Coffee", "10.00", "", "9990.00"),
       ("21/05/2024", "Rent", "40.00", "", "9950.00")]


def odd_month(rows, month: str, last_day: int, opening: str) -> bytes:
    return statement(rows, header=ODD_HEADER, period=f"Statement period 01/{month}/2024 to {last_day}/{month}/2024",
                     opening=f"Opening balance {opening}")


@pytest.mark.parametrize("target", ["new", "existing"])
def test_teaching_the_same_layout_twice_updates_the_template_and_writes_once(conn, target):
    """Next month's statement of a taught layout matches the saved template (same_layout) under a different exact
    layout_id. Teaching it again with the same mapping is a 200 and keeps one template; the month after is then read by
    that template with no mapping sent."""
    app = app_for(conn)
    existing = make_account(conn, name="Everyday Checking", currency="USD") if target == "existing" else None
    march, april, may = (odd_month(GOOD, "03", 31, "100.00"), odd_month(APRIL, "04", 30, "1000.00"),
                         odd_month(MAY, "05", 31, "10000.00"))
    fps = []
    for raw in (march, april, may):
        pages, sizes = words.read(raw)
        fps.append(templates.fingerprint(pages, sizes, words.producer(raw)))
    assert len({templates.layout_id(fp) for fp in fps}) == 3                 # three exact fingerprints...
    assert templates.same_layout(fps[0], fps[1]) and templates.same_layout(fps[0], fps[2])   # ...one layout
    for raw in (march, april, may):   # the generic parser can't read this layout
        s, e = post(app, "/api/import/preview", {"filename": "odd.pdf", "data": b64(raw)})
        assert s == 400 and e["code"] == "does_not_reconcile"
    base = counts(conn)

    def teach(raw, name):
        choice = {"account_id": existing} if existing else {"new": {"name": name, "kind": "bank"}}
        s, p = post(app, "/api/import/preview", {"filename": f"{name}.pdf", "data": b64(raw), "template": ODD_CFG})
        assert s == 200, p
        label = p["groups"][0]["label"]
        s, r = post(app, "/api/import/commit", {"filename": f"{name}.pdf", "data": b64(raw), "teach": True,
                                                "template": p["pdf"]["template"]["config"],
                                                "targets": {label: choice}})
        assert s == 200, r
        return p, r

    p1, r1 = teach(march, "odd-march")
    assert r1["new"] == 3
    assert counts(conn) == {"transactions": base["transactions"] + 3, "imports": base["imports"] + 1,
                            "manual_accounts": base["manual_accounts"] + (1 if target == "new" else 0),
                            "pdf_templates": base["pdf_templates"] + 1}
    saved = conn.execute("SELECT id, config, source FROM pdf_templates").fetchone()
    assert saved["source"] == "user"

    p2, r2 = teach(april, "odd-april")
    assert p2["pdf"]["template"]["id"] == saved["id"]     # the preview already matched the saved template
    assert r2["new"] == 4
    assert counts(conn) == {"transactions": base["transactions"] + 7, "imports": base["imports"] + 2,
                            "manual_accounts": base["manual_accounts"] + (2 if target == "new" else 0),
                            "pdf_templates": base["pdf_templates"] + 1}
    assert tuple(conn.execute("SELECT id, config, source FROM pdf_templates").fetchone()) == tuple(saved)

    before_may = counts(conn)
    s, p3 = post(app, "/api/import/preview", {"filename": "odd-may.pdf", "data": b64(may)})
    assert s == 200 and p3["rows"] == 2
    assert p3["pdf"]["template"]["id"] == saved["id"] and p3["pdf"]["template"]["source"] == "user"
    assert p3["pdf"]["template"]["config"] == json.loads(saved["config"])
    assert counts(conn) == before_may


def test_a_failure_inside_the_commit_leaves_no_new_account_behind(conn, monkeypatch):
    """Account, rows, import and template are one transaction: an error while teaching writes nothing at all."""
    app = app_for(conn)
    raw = odd_month(GOOD, "03", 31, "100.00")
    before = counts(conn)

    def boom(*args, **kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: pdf_templates.id")

    monkeypatch.setattr(templates, "save", boom)
    s, _, _ = app.handle("POST", "/api/import/commit", H, json.dumps({
        "filename": "odd.pdf", "data": b64(raw), "teach": True, "template": ODD_CFG,
        "targets": {"Odd": {"new": {"name": "Odd", "kind": "bank"}}}}).encode())
    assert s != 200
    assert counts(conn) == before


def test_a_pdf_over_the_page_cap_is_a_400_refusal_on_preview_and_commit(conn):
    """A crafted many-page file is the product's refusal on both routes, with the batch's field and code: not a 500."""
    app = app_for(conn)
    raw = many_pages_pdf(200)        # 200 pages in ~20 KB: over the 50-page cap, and far under the 10 MB file cap
    for path in ("/api/import/preview", "/api/import/commit"):
        s, e = post(app, path, {"filename": "many.pdf", "data": b64(raw),
                                "targets": {"Big": {"new": {"name": "Big", "kind": "bank"}}}})
        assert s == 400 and e["field"] == "file" and e["code"] == "pdf_too_complex", (path, s, e)


def test_a_pdf_over_max_rows_is_refused_like_csv_and_ofx(conn, monkeypatch):
    from hermes_finance import importer

    app = app_for(conn)
    monkeypatch.setattr(importer, "MAX_ROWS", 2)
    raw = statement(GOOD)   # 3 rows
    before = counts(conn)
    for path in ("/api/import/preview", "/api/import/commit"):
        s, e = post(app, path, {"filename": "big.pdf", "data": b64(raw),
                                "targets": {"Big": {"new": {"name": "Big", "kind": "bank"}}}})
        assert s == 400 and e["field"] == "file"
        assert e["error"] == "This file has 3 rows; import at most 2 at a time."
    assert counts(conn) == before
    csv = b"Date,Description,Amount\n2024-03-05,Coffee,-10.00\n2024-03-14,Salary,50.00\n2024-03-20,Rent,-40.00\n"
    s, e = post(app, "/api/import/preview", {"filename": "big.csv", "data": b64(csv)})
    assert s == 400 and e["error"] == "This file has 3 rows; import at most 2 at a time."   # the same refusal


def _new_target(p, name="Odd"):
    return {p["groups"][0]["label"]: {"new": {"name": name, "kind": "bank"}}}


def test_a_layout_taught_with_typed_balances_is_not_saved(conn):
    """(security L1) Balances typed by hand can't be read off next month's statement, so teach=true still imports the
    rows but saves no layout (a saved one would refuse every later statement), and the reply says why."""
    app = app_for(conn)
    raw = odd_month(GOOD, "03", 31, "100.00")
    before = counts(conn)
    body = {"filename": "odd.pdf", "data": b64(raw), "template": ODD_CFG, "opening": "100.00", "closing": "100.00"}
    s, p = post(app, "/api/import/preview", body)
    assert s == 200, p
    s, r = post(app, "/api/import/commit", {**body, "teach": True, "targets": _new_target(p)})
    assert s == 200, r
    assert counts(conn)["pdf_templates"] == before["pdf_templates"]      # no layout saved...
    assert counts(conn)["transactions"] == before["transactions"] + 3    # ...and the rows still imported
    assert r["new"] == 3 and r["template"] == {"saved": False, "reason": "typed_balances"}


def test_a_saved_layout_is_never_replaced_silently(conn):
    """(security L1) Teaching a different mapping for a layout that already has one: the check says it would replace
    the saved layout; a commit without replace_template is refused (template_exists) and writes nothing at all; with
    it, the old layout is replaced and the reply says so. The same mapping again replaces nothing (no flag needed)."""
    app = app_for(conn)
    march, april = odd_month(GOOD, "03", 31, "100.00"), odd_month(APRIL, "04", 30, "1000.00")
    s, p = post(app, "/api/import/preview", {"filename": "m.pdf", "data": b64(march), "template": ODD_CFG})
    assert s == 200, p
    s, r = post(app, "/api/import/commit", {"filename": "m.pdf", "data": b64(march), "template": ODD_CFG,
                                            "teach": True, "targets": _new_target(p, "March")})
    assert s == 200, r
    first = tuple(conn.execute("SELECT id, config, source FROM pdf_templates").fetchone())

    other = {**ODD_CFG, "columns": [{**ODD_CFG["columns"][0], "x1": 75}] + ODD_CFG["columns"][1:]}
    s, p2 = post(app, "/api/import/preview", {"filename": "a.pdf", "data": b64(april), "template": other})
    assert s == 200, p2
    before = counts(conn)
    s, e = post(app, "/api/import/commit", {"filename": "a.pdf", "data": b64(april), "template": other,
                                            "teach": True, "targets": _new_target(p2, "April")})
    assert s == 400 and e["code"] == "template_exists" and e["field"] == "teach"
    assert counts(conn) == before                                         # refused before anything is written
    assert tuple(conn.execute("SELECT id, config, source FROM pdf_templates").fetchone()) == first
    # the check had said so, and the first teach had nothing to replace
    assert p["pdf"]["replaces"] is None and r["template"] == {"saved": True, "replaced": False, "source": "user"}
    assert p2["pdf"]["replaces"]["source"] == "user" and p2["pdf"]["replaces"]["created_at"]
    s, r = post(app, "/api/import/commit", {"filename": "a.pdf", "data": b64(april), "template": other,
                                            "teach": True, "replace_template": True, "targets": _new_target(p2, "April")})
    assert s == 200 and r["new"] == 4
    assert r["template"] == {"saved": True, "replaced": True, "source": "user"}
    rows = conn.execute("SELECT config FROM pdf_templates").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["columns"][0]["x1"] == 75


def test_ai_view_lists_word_for_word_what_the_ai_would_be_sent(conn, brain, monkeypatch):
    """(security L2) ai_view returns every prompt an AI preview could send before anything is sent: the model then
    gets exactly those texts, in that order. Building the list calls no model and writes nothing."""
    app = app_for(conn)
    raw = statement(GOOD, header=None)          # no header line: refused locally, so the AI would be asked
    sent: list[str] = []
    monkeypatch.setattr(ai, "_ask", lambda cli_id, exe, prompt, deadline:
                        sent.append(prompt) or clibrain.Reply("{}", "claude-code", "test", 1, 1, None, 0.0))
    before = counts(conn)
    s, v = post(app, "/api/import/preview", {"filename": "h.pdf", "data": b64(raw), "ai_view": True})
    assert s == 200, v
    assert sent == [] and counts(conn) == before
    prompts = v["ai_view"]["prompts"]
    s, e = post(app, "/api/import/preview", {"filename": "h.pdf", "data": b64(raw), "ai_template": True})
    assert s == 400 and e["code"] == "ai_failed"                 # "{}" is no layout: every try goes out
    assert len(prompts) == 5 and sent == prompts
    joined = "\n".join(prompts)
    assert "90.00" not in joined and "Coffee" not in joined and "05/03/2024" not in joined
