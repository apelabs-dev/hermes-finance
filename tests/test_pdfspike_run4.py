"""Spike 4's runner (`tests/pdfspike_run4.py`): the model pin, the served-model check, reply saving,
and arm DA's anchor on the pdfplumber text layer (never on what Docling produced).

No model call and no Docling run: the CLI child and the converter are faked. One committed
synthetic fixture (us-01) gives the real text layer.

    env PYTHONPATH=$PWD/src taskpolicy -b .dispatch/docvenv/bin/python -m pytest -q tests/test_pdfspike_run4.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pdfplumber")

# Arm DA wraps the PDF in Docling's DocumentStream even when the converter is faked, so those tests need Docling
# itself. The dev group carries pdfplumber (PDF import) but not Docling, which lives only in the spike's venv.
needs_docling = pytest.mark.skipif(importlib.util.find_spec("docling") is None,
                                   reason="Docling is only in the disposable spike venv")

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "pdfspike"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import pdfspike_extract as extract  # noqa: E402
import pdfspike_run4 as run4  # noqa: E402
from hermes_finance import clibrain  # noqa: E402

US01 = next(item for item in json.loads((FIXTURES / "manifest.json").read_text())["files"] if item["id"] == "us-01")
PDF = (FIXTURES / "us-01.pdf").read_bytes()
ITEM = {"id": "t01", "country": "US", "pages": 1, "profile": "US"}


def _money(value: float) -> str:
    return f"{value:.2f}"


def _good_reply(opening: str | None = None, closing: str | None = None) -> str:
    """us-01's own printed opening, closing and rows (no running balances): it reconciles."""
    return json.dumps({
        "opening": opening or _money(US01["opening_balance"]),
        "closing": closing or _money(US01["closing_balance"]),
        "multi_account": False,
        "rows": [{"date": r["date"], "description": r["description"], "amount": _money(r["amount"]), "balance": None}
                 for r in US01["expected_rows_detail"]],
    })


def _stream(text: str, models=("claude-haiku-5-5",)) -> bytes:
    """What `claude -p --output-format stream-json --verbose` prints: an init event, then a result event."""
    init = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "model": models[0] if models else ""}
    result = {"type": "result", "subtype": "success", "is_error": False, "result": text,
              "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                        "output_tokens": 50},
              "modelUsage": {model: {"inputTokens": 100} for model in models}, "total_cost_usd": 0.0012}
    return (json.dumps(init) + "\n" + json.dumps(result) + "\n").encode()


class _FakeTable:
    def __init__(self, markdown: str):
        self.markdown = markdown

    def export_to_markdown(self, doc=None):
        return self.markdown


class _FakeDocument:
    def __init__(self, tables=(), markdown=""):
        self.tables = [_FakeTable(t) for t in tables]
        self.markdown = markdown

    def export_to_markdown(self, traverse_pictures=False):
        self.traverse_pictures = traverse_pictures
        return self.markdown


class _FakeConverter:
    def __init__(self, document):
        self.document = document
        self.calls = 0

    def convert(self, _source):
        self.calls += 1
        return SimpleNamespace(document=self.document)


def _send(reply_text: str, models=("claude-haiku-5-5",)):
    """A PinnedSend whose child is faked; it records the argv it was given."""
    seen = {}

    def fake_run(cli_id, argv, body, deadline, cwd):
        seen["argv"], seen["body"] = argv, body
        return _stream(reply_text, models), 0, None
    send = run4.PinnedSend(run=fake_run)
    return send, seen


# --- the model pin ----------------------------------------------------------------------------------------------------

def test_the_pin_is_the_seams_own_argv_plus_the_full_model_id(tmp_path):
    argv = run4.pinned_argv(Path("/bin/claude"), tmp_path)
    seam = clibrain._argv("claude", Path("/bin/claude"), tmp_path, "stream-json")
    assert argv[:len(seam)] == seam
    assert argv[len(seam):] == ["--model", "claude-haiku-5-5"]
    assert "haiku" not in argv[:-1] and argv.count("--model") == 1


def test_the_pinned_send_runs_the_pinned_argv_and_keeps_the_served_model():
    send, seen = _send("{}")
    reply = send("claude", Path("/bin/claude"), {"prompt": "hello"}, run4.time.monotonic() + 60)
    assert seen["argv"][-2:] == ["--model", "claude-haiku-5-5"]
    assert seen["body"] == b"hello"
    assert send.keys == ["claude-haiku-5-5"] and send.seconds is not None
    assert reply.text == "{}" and reply.model == "claude-haiku-5-5" and reply.tokens_in == 100


@pytest.mark.parametrize("keys,ok", [
    (["claude-haiku-5-5"], True),
    (["haiku"], False),
    (["claude-sonnet-5-5"], False),
    (["claude-haiku-5-5", "claude-sonnet-5-5"], False),
    ([], False),
])
def test_check_served_accepts_only_the_pin(keys, ok):
    if ok:
        run4.check_served(keys)
    else:
        with pytest.raises(run4.WrongModel):
            run4.check_served(keys)


def test_model_usage_keys_come_from_the_last_result_event():
    assert run4.model_usage_keys(_stream("x", ("claude-haiku-5-5",))) == ["claude-haiku-5-5"]
    assert run4.model_usage_keys(_stream("x", ("b-model", "a-model"))) == ["a-model", "b-model"]
    assert run4.model_usage_keys(b"not json\n") == []


# --- arm DA: Docling's tables are sent, the text layer anchors -------------------------------------------------------

@needs_docling
def test_da_sends_docling_tables_and_anchors_on_the_text_layer():
    document = _FakeDocument(tables=["| Date | Amount |\n|---|---|\n| 01/01 | 1.00 |"], markdown="ignored")
    send, seen = _send(_good_reply())
    saved = {}
    result = run4.extract_da(PDF, profile="US", send=send, converter=_FakeConverter(document),
                             exe=Path("/bin/claude"), on_reply=saved.update)
    assert len(result.rows) == len(US01["expected_rows_detail"])
    assert saved["payload_kind"] == "tables"
    assert saved["sent_text"].startswith("Table 1:\n| Date | Amount |")
    assert "| 01/01 | 1.00 |" in seen["body"].decode()
    assert "ignored" not in seen["body"].decode()
    # the anchor text is the PDF's own text layer, which was never sent
    assert saved["anchor_text"] == run4.text_layer(PDF)
    assert saved["anchor_text"] not in seen["body"].decode()


@needs_docling
def test_da_balances_printed_only_by_docling_are_unanchored():
    # Docling's table "prints" 99999.99 but the PDF's text layer does not: the reply is refused.
    fake_opening = "99999.99"
    document = _FakeDocument(tables=[f"| Opening | {fake_opening} |"])
    reply = _good_reply(opening=fake_opening)
    send, _seen = _send(reply)
    with pytest.raises(extract.ExtractionError) as refused:
        run4.extract_da(PDF, profile="US", send=send, converter=_FakeConverter(document), exe=Path("/bin/claude"))
    assert refused.value.code == "unanchored"
    assert refused.value.tokens_in == 100


@needs_docling
def test_da_sends_markdown_when_docling_found_no_table():
    document = _FakeDocument(tables=[], markdown="# Statement\nsome text")
    send, seen = _send(_good_reply())
    saved = {}
    run4.extract_da(PDF, profile="US", send=send, converter=_FakeConverter(document), exe=Path("/bin/claude"),
                    on_reply=saved.update)
    assert saved["payload_kind"] == "markdown"
    assert document.traverse_pictures is True        # da2: picture-region text is part of the markdown
    assert "# Statement\nsome text" in seen["body"].decode()
    assert "THE STATEMENT PDF CONVERTED TO MARKDOWN" in seen["body"].decode()


@needs_docling
def test_da_refuses_no_rows_when_docling_found_nothing_and_makes_no_call():
    send, seen = _send(_good_reply())
    with pytest.raises(extract.ExtractionError) as refused:
        run4.extract_da(PDF, profile="US", send=send, converter=_FakeConverter(_FakeDocument()),
                        exe=Path("/bin/claude"))
    assert refused.value.code == "no_rows"
    assert seen == {}


@needs_docling
def test_da_prompt_goes_through_the_clibrain_seam(monkeypatch):
    calls = []
    real = clibrain.request

    def spy(cli_id, instructions, history, steps, max_tokens):
        calls.append((cli_id, history, steps, max_tokens))
        return real(cli_id, instructions, history, steps, max_tokens)
    monkeypatch.setattr(clibrain, "request", spy)
    send, seen = _send(_good_reply())
    run4.extract_da(PDF, profile="US", send=send, converter=_FakeConverter(_FakeDocument(tables=["| a |"])),
                    exe=Path("/bin/claude"))
    assert calls == [("claude", [], [], extract.MAX_TOKENS)]
    assert seen["body"].decode().startswith(clibrain.PREAMBLE)


# --- _measure: every request saved, logged and checked -----------------------------------------------------------------

@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(run4, "REPLIES", tmp_path / "replies")
    monkeypatch.setattr(run4, "MODEL_LOG", tmp_path / "model-calls.md")
    monkeypatch.setattr(clibrain, "executable", lambda _cli: Path("/bin/claude"))
    return tmp_path


def _da_converter(monkeypatch, document):
    monkeypatch.setattr(run4, "_CONVERTER", _FakeConverter(document))


@needs_docling
def test_measure_saves_the_exact_request_and_the_served_model(scratch, monkeypatch):
    _da_converter(monkeypatch, _FakeDocument(tables=["| a |"]))
    send, seen = _send(_good_reply())
    record = run4._measure("da", "tune", ITEM, PDF, send)
    assert record["outcome"] == "reconciled" and record["served_model"] == "claude-haiku-5-5"
    saved = json.loads((scratch / "replies" / "da" / "tune" / "t01.json").read_text())
    assert saved["model_usage_keys"] == ["claude-haiku-5-5"] and saved["model_pin"] == "claude-haiku-5-5"
    assert saved["prompt"] == seen["body"].decode()
    assert saved["reply"] == _good_reply() and saved["prompt_version"] == run4.DA_PROMPT_VERSION
    assert saved["anchor_text"] == run4.text_layer(PDF)
    assert oct((scratch / "replies" / "da" / "tune" / "t01.json").stat().st_mode & 0o777) == "0o600"
    assert run4.calls_logged() == 1
    assert "| da | tune | t01 | reconciled |" in (scratch / "model-calls.md").read_text()


def test_measure_arm_ah_saves_the_statement_text(scratch):
    send, _seen = _send(_good_reply())
    record = run4._measure("ah", "tune", ITEM, PDF, send)
    assert record["outcome"] == "reconciled"
    saved = json.loads((scratch / "replies" / "ah" / "tune" / "t01.json").read_text())
    assert saved["prompt_version"] == extract.PROMPT_VERSION and "statement_text" in saved
    assert saved["model_usage_keys"] == ["claude-haiku-5-5"]


@needs_docling
def test_a_reply_from_another_model_is_saved_logged_then_stops_the_run(scratch, monkeypatch):
    _da_converter(monkeypatch, _FakeDocument(tables=["| a |"]))
    send, _seen = _send(_good_reply(), models=("claude-sonnet-5-5",))
    with pytest.raises(run4.WrongModel):
        run4._measure("da", "tune", ITEM, PDF, send)
    saved = json.loads((scratch / "replies" / "da" / "tune" / "t01.json").read_text())
    assert saved["model_usage_keys"] == ["claude-sonnet-5-5"]
    assert run4.calls_logged() == 1


@needs_docling
def test_a_transport_error_is_errored_not_a_wrong_model(scratch, monkeypatch):
    _da_converter(monkeypatch, _FakeDocument(tables=["| a |"]))

    def broken(*_args):
        return b"", 1, None
    record = run4._measure("da", "tune", ITEM, PDF, run4.PinnedSend(run=broken))
    # spike 3's vocabulary: an unparseable CLI stream is `garbled`, a refusal; never WrongModel
    assert record["outcome"] == "refused:garbled" and record["served_model"] == ""
    saved = json.loads((scratch / "replies" / "da" / "tune" / "t01.json").read_text())
    assert saved["reply"] is None and saved["model_usage_keys"] == []


@needs_docling
def test_no_rows_is_recorded_as_a_refusal_without_a_call(scratch, monkeypatch):
    _da_converter(monkeypatch, _FakeDocument())
    send, seen = _send(_good_reply())
    record = run4._measure("da", "tune", ITEM, PDF, send)
    assert record["outcome"] == "refused:no_rows" and seen == {}
    assert run4.calls_logged() == 0


# --- rescore: offline, anchored on what was saved ----------------------------------------------------------------------

@needs_docling
def test_rescore_rejudges_a_saved_da_reply_against_its_saved_text_layer(scratch, monkeypatch):
    _da_converter(monkeypatch, _FakeDocument(tables=["| a |"]))
    send, _seen = _send(_good_reply())
    run4._measure("da", "tune", ITEM, PDF, send)
    saved = json.loads((scratch / "replies" / "da" / "tune" / "t01.json").read_text())
    assert run4.rescore_one(saved)["outcome"] == "reconciled"
    saved["anchor_text"] = "nothing printed here"
    assert run4.rescore_one(saved)["outcome"] == "refused:unanchored"
    monkeypatch.setattr(run4, "_path", lambda arm, corpus, suffix, tag="": scratch / f"{arm}-{corpus}{tag}.{suffix}")
    assert run4.rescore("da", "tune") == 0
    assert json.loads((scratch / "da-tune-rescore.json").read_text())["summary"]["reconciled"] == 1


# --- run: one held-out run per arm, and the call budget -----------------------------------------------------------------

def _manifest(tmp_path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "t01.pdf").write_bytes(PDF)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [{**ITEM, "set": "held", "sha256": run4._sha(PDF)}]}))
    return manifest


def test_load_corpus_checks_every_sha(tmp_path):
    manifest = _manifest(tmp_path)
    assert [item["id"] for item, _ in run4.load_corpus("heldout", manifest, tmp_path / "raw")] == ["t01"]
    (tmp_path / "raw" / "t01.pdf").write_bytes(PDF + b"\n")
    with pytest.raises(SystemExit, match="sha256"):
        run4.load_corpus("heldout", manifest, tmp_path / "raw")


def test_a_second_held_out_run_is_refused(scratch, monkeypatch):
    monkeypatch.setattr(run4, "_path", lambda arm, corpus, suffix, tag="": scratch / f"{arm}-{corpus}{tag}.{suffix}")
    (scratch / "da-heldout.json").write_text("{}")
    assert run4.run("da", "heldout") == 2


def test_the_call_budget_is_enforced_before_any_call(scratch, monkeypatch):
    monkeypatch.setattr(run4, "_path", lambda arm, corpus, suffix, tag="": scratch / f"{arm}-{corpus}{tag}.{suffix}")
    monkeypatch.setattr(run4, "load_corpus", lambda corpus: [(dict(ITEM, set="tune"), PDF)])
    monkeypatch.setattr(run4, "calls_logged", lambda log=None: run4.CALL_BUDGET)
    assert run4.run("ah", "tune") == 4
