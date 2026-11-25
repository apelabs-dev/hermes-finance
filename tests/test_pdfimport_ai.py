"""The opt-in AI template proposal: ids only, no numbers, the env gate, candidate loop and privacy guard."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from hermes_finance import clibrain
from hermes_finance.pdfimport import PdfRefused, ai, layout, parse_statement, templates, words

from pdf_fixtures import plain, statement

GOOD = [("05/03/2024", "Coffee", "10.00", "", "90.00"),
        ("14/03/2024", "Salary", "", "50.00", "140.00"),
        ("20/03/2024", "Rent", "40.00", "", "100.00")]
NO_YEAR = [("05/06", "Coffee", "10.00", "", "90.00"),
           ("14/06", "Salary", "", "50.00", "140.00"),
           ("20/06", "Rent", "40.00", "", "100.00")]
NO_YEAR_PERIOD = "Statement period 1 June to 30 June"


def reply(text: str):
    return clibrain.Reply(text, "claude-code", "test", 1, 1, None, 0.0)


def header_json(raw: bytes) -> str:
    pages, _ = words.read(raw)
    _view, gid = ai.word_view(pages)
    by_text: dict[str, list[int]] = {}
    for wid, w in gid.items():
        by_text.setdefault(w.text, []).append(wid)
    header = [by_text[t][0] for t in ("Date", "Description", "Debit", "Credit", "Balance") if t in by_text]
    return json.dumps({"header_ids": header,
                       "columns": [{"role": "date", "ids": [by_text["Date"][0]]},
                                   {"role": "desc", "ids": [by_text["Description"][0]]},
                                   {"role": "debit", "ids": [by_text["Debit"][0]]},
                                   {"role": "credit", "ids": [by_text["Credit"][0]]},
                                   {"role": "balance", "ids": [by_text["Balance"][0]]}],
                       "end_ids": [], "amount_sign": "explicit", "date_order": "DMY", "year_missing": False})


def no_year_json(raw: bytes) -> str:
    """A model reply for a header-less statement whose printed period and rows carry no year."""
    pages, _ = words.read(raw)
    _view, gid = ai.word_view(pages)
    by_text: dict[str, list[int]] = {}
    for wid, w in gid.items():
        by_text.setdefault(w.text, []).append(wid)
    return json.dumps({"header_ids": [by_text["Statement"][0], by_text["period"][0]],
                       "columns": [{"role": "date", "ids": [by_text["05/06"][0]]},
                                   {"role": "desc", "ids": [by_text["Coffee"][0]]},
                                   {"role": "debit", "ids": [by_text["10.00"][0]]},
                                   {"role": "credit", "ids": [by_text["50.00"][0]]},
                                   {"role": "balance", "ids": [by_text["90.00"][0]]}],
                       "end_ids": [], "amount_sign": "explicit", "date_order": "DMY", "year_missing": True})


@pytest.fixture
def brain(monkeypatch):
    monkeypatch.setenv("FINANCE_PDF_AI", "on")
    monkeypatch.setattr(clibrain, "off", lambda: False)
    monkeypatch.setattr(clibrain, "ready", lambda conn, cli_id: (Path("/bin/true"), ""))


def test_to_config_rejects_a_number_or_date():
    pages, _ = words.read(statement(GOOD))
    _view, gid = ai.word_view(pages)
    obj = json.loads(header_json(statement(GOOD)))
    obj["note"] = "opening 100.00"
    with pytest.raises(ValueError, match="json_has_data"):
        ai.to_config(obj, gid)
    obj["note"] = "05/03/2024"
    with pytest.raises(ValueError, match="json_has_data"):
        ai.to_config(obj, gid)


def test_to_config_rejects_unknown_ids_and_bad_roles():
    pages, _ = words.read(statement(GOOD))
    _view, gid = ai.word_view(pages)
    with pytest.raises(ValueError, match="unknown_id"):
        ai.to_config({"header_ids": [999999],
                      "columns": [{"role": "date", "ids": [999999]},
                                  {"role": "debit", "ids": [1]}]}, gid)
    with pytest.raises(ValueError, match="missing_roles"):
        ai.to_config({"header_ids": [1], "columns": [{"role": "date", "ids": [1]}]}, gid)
    plain_id = next(i for i, w in gid.items() if w.text.lower() not in words.KEEP_WORDS)
    with pytest.raises(ValueError, match="header_without_words"):
        ai.to_config({"header_ids": [plain_id],
                      "columns": [{"role": "date", "ids": [plain_id]},
                                  {"role": "amount", "ids": [plain_id]}]}, gid)


def test_table_end_pattern_never_carries_a_statement_word(conn):
    raw = statement(GOOD)
    pages, sizes = words.read(raw)
    _view, gid = ai.word_view(pages)
    by_text: dict[str, list[int]] = {}
    for wid, w in gid.items():
        by_text.setdefault(w.text, []).append(wid)
    obj = json.loads(header_json(raw))

    obj["end_ids"] = [by_text["Coffee"][0]]
    cfg = ai.to_config(obj, gid)
    assert isinstance(cfg["table_end_y"], float)
    assert "table_end_pattern" not in cfg
    assert "coffee" not in json.dumps(cfg).lower()

    saved = templates.save(conn, templates.fingerprint(pages, sizes, ""), cfg, "ai")
    row = conn.execute("SELECT config FROM pdf_templates").fetchone()
    assert "coffee" not in json.dumps(saved["config"]).lower()
    assert "coffee" not in row["config"].lower()

    obj["end_ids"] = [by_text["Closing"][0], by_text["balance"][0]]
    cfg = ai.to_config(obj, gid)
    assert "closing" in cfg["table_end_pattern"]
    assert re.search(cfg["table_end_pattern"], "Closing balance 100.00", re.I)
    assert isinstance(cfg["table_end_y"], float)


def test_mask_text_and_assert_masked():
    real = ["Jane", "Doe", "ACME", "Grocery", "1,234.56"]
    masked = words.mask_text("Jane Doe paid ACME Grocery 1,234.56 Balance Date")
    assert "Jane" not in masked and "ACME" not in masked and "1,234.56" not in masked
    assert "Balance" in masked and "Date" in masked          # structural words stay readable in a header line
    view = masked + " L1 p1 y20: 1@40-59:Date"
    ai.assert_masked(view, real)
    with pytest.raises(PermissionError):
        ai.assert_masked("Jane Doe paid ACME Grocery 1,234.56 Balance Date", real)
    with pytest.raises(PermissionError):
        ai.assert_masked(view + " Grocery", real)
    with pytest.raises(PermissionError):
        ai.assert_masked(view + " 1,234.56", real)


# ---------------------------------------------------------------- item C (round 6): a description holding a header word

COLLIDING = [("04/03/2024", "ATM Deposit Balance Credit", "10.00", "", "90.00")]


def test_a_description_holding_a_header_word_does_not_empty_the_privacy_screen():
    """A description of header words ("ATM Deposit Balance Credit") made all five tries fail the mask guard, so the
    privacy screen showed 0 prompts for a statement it should have sent.

    The description's own copies were always masked. The copies that failed the guard were the header line's, because
    a structural line keeps its lexicon words while the same token is sensitive in a description. The view now masks
    a kept word when the token is sensitive anywhere else in the statement; the guard itself is unchanged, so it still
    refuses the token wherever it does appear.
    """
    pages, _sizes = words.read(statement(COLLIDING))
    real = ai._sensitive_words(pages)
    letters, _numbers = ai._sensitive(real)
    assert {"Balance", "Credit", "Deposit", "ATM"} <= letters      # the description's words: all sensitive

    prompts = ai.outgoing(pages)
    assert len(prompts) == ai.TRIES, "the privacy screen showed nothing for a statement it can send"
    for k in range(ai.TRIES):
        view, _gid = ai.word_view(pages, variant=k)
        assert not (letters & set(ai._SENSITIVE_WORD.findall(view)))
        ai.assert_masked(view, real)                              # never raises

    view, _gid = ai.word_view(pages)
    assert "Balance" not in view and "Credit" not in view         # the token is gone from the prompt
    assert "Date" in view and "Description" in view and "Debit" in view   # the rest of the header still reads
    with pytest.raises(PermissionError):                          # and the guard is not loosened
        ai.assert_masked(view + " Balance", real)


def test_a_statement_without_the_collision_keeps_its_readable_header():
    """The masking is per token, not per line: a statement whose description is not a header word keeps every header
    word it kept before, so the 19 statement views stay byte-identical."""
    pages, _sizes = words.read(statement([("04/03/2024", "Coffee", "10.00", "", "90.00")]))
    view, _gid = ai.word_view(pages)
    for word in ("Date", "Description", "Debit", "Credit", "Balance", "Opening", "balance"):
        assert word in view
    assert "Coffee" not in view


def test_the_view_masks_against_the_guards_own_set():
    """One definition of sensitivity: the set a view masks against is `_sensitive` of `_sensitive_words`, the set the
    guard charges. If they drifted, a word kept for a structural line would be refused (an empty screen), or a
    sensitive word could be kept and pass."""
    for rows in (COLLIDING, [("04/03/2024", "Coffee", "10.00", "", "90.00")]):
        pages, _sizes = words.read(statement(rows))
        lines = ai.build_lines(pages)
        assert ai._sensitive_letters(lines) == ai._sensitive(ai._sensitive_words(pages))[0]
        view, _gid = ai.word_view(pages)
        assert not (ai._sensitive_letters(lines) & set(ai._SENSITIVE_WORD.findall(view)))


def test_propose_accepts_the_first_reconciling_candidate(conn, brain, monkeypatch):
    pages, sizes = words.read(statement(GOOD))
    calls = []

    def ask(cli_id, exe, prompt, deadline):
        calls.append(prompt)
        return reply(header_json(statement(GOOD)))

    monkeypatch.setattr(ai, "_ask", ask)
    cfg, parsed, verdict, outcomes = ai.propose(conn, pages, sizes, anchors={}, year=None, totals={}, carry=())
    assert cfg is not None and parsed is not None and verdict is not None and verdict.accepted
    assert outcomes[-1] == "accepted" and len(calls) == 1
    assert "Coffee" not in calls[0] and "100.00" not in calls[0]


def test_propose_gives_up_after_five_candidates(conn, brain, monkeypatch):
    pages, sizes = words.read(statement(GOOD))
    calls = []

    def ask(cli_id, exe, prompt, deadline):
        calls.append(prompt)
        return reply("not json")

    monkeypatch.setattr(ai, "_ask", ask)
    cfg, parsed, verdict, outcomes = ai.propose(conn, pages, sizes, anchors={}, year=None, totals={}, carry=())
    assert cfg is None and parsed is None and verdict is None
    assert len(calls) == 5 and len(outcomes) == 5 and set(outcomes) <= {"bad_json", "no_json"}


def test_ai_off_is_unavailable_and_never_calls(conn, monkeypatch):
    monkeypatch.setenv("FINANCE_PDF_AI", "off")
    called = []
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: called.append(1))
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, statement(GOOD, header=None), ai=True, today=date(2026, 9, 28))
    assert caught.value.code == "ai_unavailable"
    assert called == []


def test_cli_brain_off_is_unavailable(conn, monkeypatch):
    monkeypatch.setenv("FINANCE_PDF_AI", "on")
    monkeypatch.setattr(clibrain, "off", lambda: True)
    called = []
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: called.append(1))
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, statement(GOOD, header=None), ai=True, today=date(2026, 9, 28))
    assert caught.value.code == "ai_unavailable" and not called


def test_a_failed_call_is_ai_failed(conn, brain, monkeypatch):
    def ask(cli_id, exe, prompt, deadline):
        raise clibrain.CliError("failed")

    monkeypatch.setattr(ai, "_ask", ask)
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, statement(GOOD, header=None), ai=True, today=date(2026, 9, 28))
    assert caught.value.code == "ai_failed"
    assert caught.value.extra["detail"]["candidates"] >= 1


def test_a_runtime_error_in_the_model_call_is_ai_failed(conn, brain, monkeypatch):
    def ask(*args, **kwargs):
        raise RuntimeError("child died")

    monkeypatch.setattr(ai, "_ask", ask)
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, statement(GOOD, header=None), ai=True, today=date(2026, 9, 28))
    assert caught.value.code == "ai_failed"
    assert "child died" not in str(caught.value)


def test_a_crashing_proposal_is_ai_failed_not_a_500(conn, brain, monkeypatch):
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: reply(header_json(statement(GOOD))))

    def boom(*args, **kwargs):
        raise NameError("proposal bug")

    monkeypatch.setattr(ai, "to_config", boom)
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, statement(GOOD, header=None), ai=True, today=date(2026, 9, 28))
    assert caught.value.code == "ai_failed"
    assert caught.value.extra["detail"]["reason"] == "proposal_error"
    assert "NameError" not in json.dumps(caught.value.extra)
    assert "proposal bug" not in json.dumps(caught.value.extra)


def test_a_failed_candidate_parse_does_not_stop_the_next_candidate(conn, brain, monkeypatch):
    pages, sizes = words.read(statement(GOOD))
    failures = []
    real = layout.parse

    def flaky(pages, sizes=None, config=None, **kwargs):
        if config and not failures:
            failures.append(1)
            raise RuntimeError("bad candidate")
        return real(pages, sizes, config, **kwargs)

    monkeypatch.setattr(layout, "parse", flaky)
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: reply(header_json(statement(GOOD))))
    cfg, parsed, verdict, outcomes = ai.propose(conn, pages, sizes, anchors={}, year=None, totals={}, carry=())
    assert outcomes[0] == "parse_failed" and outcomes[-1] == "accepted"
    assert cfg is not None and parsed is not None and verdict is not None and verdict.accepted


def test_ai_proposal_without_a_year_is_refused_and_offered(conn, brain, monkeypatch):
    raw = statement(NO_YEAR, period=NO_YEAR_PERIOD, header=None)
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: reply(no_year_json(raw)))
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, raw, ai=True, today=date(2026, 9, 28))
    assert caught.value.code == "no_period"
    assert caught.value.extra.get("needs_year") is True
    assert caught.value.extra.get("ai_used") is True
    assert caught.value.extra["proposal"]["columns"]

    st = parse_statement(conn, raw, ai=True, year=2025, today=date(2026, 9, 28))
    assert st.ai_used is True
    assert [r.date for r in st.rows] == ["2025-06-05", "2025-06-14", "2025-06-20"]


def test_local_parse_never_calls_a_model(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a model was called for a statement the local parser read")

    monkeypatch.setattr(ai, "_ask", boom)
    st = parse_statement(conn, statement(GOOD), ai=True, today=date(2026, 9, 28))
    assert st.rows and st.ai_used is False


AMBIGUOUS = [("01/02/2024", "A", "10.00", "", "90.00"), ("02/02/2024", "B", "", "50.00", "140.00")]
AMBIGUOUS_PERIOD = "Statement period 01/01/2024 to 02/02/2024"


def _with_date_order(text: str, order: str) -> str:
    obj = json.loads(text)
    obj["date_order"] = order
    return json.dumps(obj)


@pytest.mark.parametrize("order", ["MDY", "DMY"])
def test_an_ai_reply_cannot_pick_the_date_order_of_an_ambiguous_statement(conn, brain, monkeypatch, order):
    """The model sees 99/99/9999 only, so its date_order is a guess: the AI path refuses exactly like the local one."""
    raw = statement(AMBIGUOUS, period=AMBIGUOUS_PERIOD)
    with pytest.raises(PdfRefused) as local:
        parse_statement(conn, raw, today=date(2026, 9, 28))
    assert local.value.code == "unreadable_date"
    assert "ambiguous_date_order" in local.value.extra["detail"]["problems"]

    calls = []

    def ask(cli_id, exe, prompt, deadline):
        calls.append(prompt)
        return reply(_with_date_order(header_json(raw), order))

    monkeypatch.setattr(ai, "_ask", ask)
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, raw, ai=True, today=date(2026, 9, 28))
    assert calls, "the AI path ran"
    assert caught.value.code == local.value.code == "unreadable_date"
    assert "ambiguous_date_order" in caught.value.extra["detail"]["problems"]
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def headerless_json(raw: bytes) -> str:
    """A model reply for statement(GOOD, header=None): the period line above the table, columns from the first row."""
    pages, _ = words.read(raw)
    _view, gid = ai.word_view(pages)
    by_text: dict[str, list[int]] = {}
    for wid, w in gid.items():
        by_text.setdefault(w.text, []).append(wid)
    return json.dumps({"header_ids": [by_text["Statement"][0], by_text["period"][0]],
                       "columns": [{"role": "date", "ids": [by_text["05/03/2024"][0]]},
                                   {"role": "desc", "ids": [by_text["Coffee"][0]]},
                                   {"role": "debit", "ids": [by_text["10.00"][0]]},
                                   {"role": "credit", "ids": [by_text["50.00"][0]]},
                                   {"role": "balance", "ids": [by_text["90.00"][0]]}],
                       "end_ids": [], "amount_sign": "explicit", "year_missing": False})


def test_an_ai_reply_date_order_is_ignored_and_the_local_resolver_decides(conn, brain, monkeypatch):
    raw = statement(GOOD, header=None)
    prompts = []

    def ask(cli_id, exe, prompt, deadline):
        prompts.append(prompt)
        return reply(_with_date_order(headerless_json(raw), "MDY"))   # 14/03 can only be day first

    monkeypatch.setattr(ai, "_ask", ask)
    st = parse_statement(conn, raw, ai=True, today=date(2026, 9, 28))
    assert st.ai_used is True
    assert [r.date for r in st.rows] == ["2024-03-05", "2024-03-14", "2024-03-20"]   # day first, as printed
    assert "date_order" not in st.proposal and "date_order" not in st.template["config"]
    assert prompts and "date_order" not in prompts[0]   # the model is never asked for it


def test_to_config_drops_a_date_order():
    pages, _ = words.read(statement(GOOD))
    _view, gid = ai.word_view(pages)
    for order in ("DMY", "MDY"):
        cfg = ai.to_config(json.loads(_with_date_order(header_json(statement(GOOD)), order)), gid)
        assert "date_order" not in cfg


def test_a_candidate_that_replays_but_has_a_problem_is_not_accepted(conn, brain, monkeypatch):
    """The AI path uses the generic/template gate: a replayed candidate with a parse problem is no proposal to import."""
    raw = statement(GOOD, header=None)
    real = layout.parse

    def with_problem(pages, sizes=None, config=None, **kwargs):
        parsed = real(pages, sizes, config, **kwargs)
        if config and "columns" in config:
            parsed.problems.append("unassigned_money")
        return parsed

    monkeypatch.setattr(layout, "parse", with_problem)
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: reply(headerless_json(raw)))
    pages, sizes = words.read(raw)
    cfg, parsed, verdict, outcomes = ai.propose(conn, pages, sizes, anchors={}, year=None, totals={}, carry=())
    assert parsed is None and verdict is None and "accepted" not in outcomes
    assert set(outcomes) == {"does_not_reconcile"} and len(outcomes) == 5
    with pytest.raises(PdfRefused) as caught:
        parse_statement(conn, raw, ai=True, today=date(2026, 9, 28))
    assert caught.value.code == "ai_failed"
