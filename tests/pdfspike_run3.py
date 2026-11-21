"""Spike 3's frozen runner: one arm, one corpus, one table.

    .dispatch/docvenv/bin/python tests/pdfspike_run3.py --arm d --corpus tune
    .dispatch/docvenv/bin/python tests/pdfspike_run3.py --arm a --corpus tune
    .dispatch/docvenv/bin/python tests/pdfspike_run3.py --arm a --corpus heldout
    .dispatch/docvenv/bin/python tests/pdfspike_run3.py --arm a --corpus tune --rescore

The runner verifies every sample's sha256 against the committed manifest-real.json, refuses a
file whose committed set does not match `--corpus`, and writes
`.dispatch/spike3-<arm>-<corpus>.json` plus a markdown fragment. A held-out run happens exactly
once per arm: the result file is its own marker, and a second run exits 2. There is no --force in
the protocol's command line.

Per-file records carry only id, country, pages, row count, outcome, seconds and (arm A) token and
list-price figures. No statement text is written to them or printed. Arm A also keeps, per
statement, the exact text and prompt sent and the raw reply in `.dispatch/replies/<corpus>/<id>.json`
(git-ignored scratch, owner-only, never committed), so `--rescore` can re-judge the saved replies
offline: zero model calls, no PDF read, and the held-out marker is neither checked nor written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DISPATCH = HERE.parent / ".dispatch"
RAW = DISPATCH / "samples" / "raw"
META = DISPATCH / "corpus-meta.json"
REAL_MANIFEST = HERE / "fixtures" / "pdfspike" / "manifest-real.json"
MODEL_LOG = DISPATCH / "model-calls.md"
REPLIES = DISPATCH / "replies"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import pdfspike_extract as extract  # noqa: E402
import pdfspike_layout as layout  # noqa: E402
from pdfspike_judge import Candidate, judge  # noqa: E402

VOCABULARY = ("reconciled", "refused:", "errored:")
ERRORED_CODES = frozenset({"timeout", "no_engine", "missing", "auth", "rate_limited", "failed", "no_time"})


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _environment(arm: str, corpus: str) -> dict:
    return {
        "arm": arm,
        "corpus": corpus,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "date": date.today().isoformat(),
        "commit": _git_sha(),
        "utc_timestamp": _utc_now(),
        "runner": "tests/pdfspike_run3.py",
        "prompt_version": extract.PROMPT_VERSION if arm == "a" else None,
        "text_budget": extract.TEXT_BUDGET if arm == "a" else None,
        "model_deadline_seconds": extract.MODEL_DEADLINE_SECONDS if arm == "a" else None,
        "balance_anchor": True if arm == "a" else None,
    }


def _would_reconcile(candidate: Candidate | None, profile: str, anchored: bool = True) -> bool | None:
    """The diagnostic: would the judge accept these rows with the multi-account veto lifted?

    Null when the extractor never got as far as having rows (a parse, transport or engine
    refusal); True or False otherwise. Costs nothing: it is the same judge, run again. Lifting
    the veto never lifts the balance anchor: an arm A candidate whose balances are not printed in
    the text (`anchored=False`) is False, whatever its own arithmetic says.
    """
    if candidate is None or not candidate.rows:
        return None
    if not anchored:
        return False
    relaxed = Candidate(candidate.rows, candidate.opening, candidate.closing, candidate.balances, False)
    try:
        judge(relaxed, profile)
    except layout.PdfSpikeError:
        return False
    return True


def _path(arm: str, corpus: str, suffix: str, tag: str = "") -> Path:
    return DISPATCH / f"spike3-{arm}-{corpus}{tag}.{suffix}"


def _log_model_call(item: dict, record: dict, note: str) -> None:
    if not MODEL_LOG.is_file():
        MODEL_LOG.write_text(
            "| utc | arm | purpose | id | outcome | tokens_in | tokens_out | list_usd | note |\n"
            "|---|---|---|---|---|---:|---:|---:|---|\n")
    with MODEL_LOG.open("a") as handle:
        handle.write(f"| {_utc_now()} | a | statement extraction | {item['id']} | {record['outcome']} | "
                     f"{record['tokens_in'] if record['tokens_in'] is not None else ''} | "
                     f"{record['tokens_out'] if record['tokens_out'] is not None else ''} | "
                     f"{record['list_usd'] if record['list_usd'] is not None else ''} | {note} |\n")


def _save_reply(corpus: str, item: dict, sent: dict) -> None:
    """Keep one arm A request's exact text, prompt and raw reply for an offline re-score (scratch, owner-only)."""
    folder = REPLIES / corpus
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"id": item["id"], "country": item["country"], "pages": item["pages"],
               "profile": item["profile"], "corpus": corpus, "prompt_version": extract.PROMPT_VERSION,
               "commit": _git_sha(), "utc_timestamp": _utc_now(), **sent}
    path = folder / f"{item['id']}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def _new_record(item: dict) -> dict:
    return {
        "id": item["id"],
        "country": item["country"],
        "pages": item["pages"],
        "rows": 0,
        "outcome": None,
        "would_reconcile": None,
        "seconds": None,
        "tokens_in": None,
        "tokens_out": None,
        "list_usd": None,
        "truncated": False,
    }


def _record_refusal(record: dict, exc: Exception, profile: str) -> dict:
    """A refusal or a coded error into the record: outcome, usage and the anchored counterfactual."""
    if isinstance(exc, extract.ExtractionError) and exc.code in ERRORED_CODES:
        record["outcome"] = f"errored:{exc.code}"
    else:
        record["outcome"] = f"refused:{exc.code}"
    record["tokens_in"] = getattr(exc, "tokens_in", None)
    record["tokens_out"] = getattr(exc, "tokens_out", None)
    record["list_usd"] = getattr(exc, "list_usd", None)
    record["truncated"] = bool(getattr(exc, "truncated", False))
    record["would_reconcile"] = _would_reconcile(getattr(exc, "candidate", None), profile,
                                                 getattr(exc, "anchored", True))
    return record


def _measure(arm: str, item: dict, data: bytes, on_reply=None) -> dict:
    record = _new_record(item)
    started = time.perf_counter()
    try:
        result = (extract.extract_docling(data, profile=item["profile"]) if arm == "d"
                  else extract.extract_ai(data, profile=item["profile"], on_reply=on_reply))
    except (layout.PdfSpikeError, extract.ExtractionError) as exc:
        record["seconds"] = round(time.perf_counter() - started, 4)
        return _record_refusal(record, exc, item["profile"])
    except Exception as exc:      # noqa: BLE001 - an unexpected engine failure is an error bucket, not a crash
        record["seconds"] = round(time.perf_counter() - started, 4)
        record["outcome"] = f"errored:{type(exc).__name__}"
        return record
    record["seconds"] = round(time.perf_counter() - started, 4)
    record["rows"] = len(result.rows)
    record["outcome"] = "reconciled"
    record["would_reconcile"] = _would_reconcile(result.candidate, item["profile"])
    record["truncated"] = bool(result.truncated)
    record["tokens_in"] = result.tokens_in
    record["tokens_out"] = result.tokens_out
    record["list_usd"] = result.list_usd
    return record


def _summary(records: list[dict]) -> dict:
    refused: dict[str, int] = {}
    errored = 0
    for record in records:
        if record["outcome"].startswith("refused:"):
            code = record["outcome"].split(":", 1)[1]
            refused[code] = refused.get(code, 0) + 1
        elif record["outcome"].startswith("errored:"):
            errored += 1
    tokens_in = [r["tokens_in"] for r in records if r["tokens_in"] is not None]
    tokens_out = [r["tokens_out"] for r in records if r["tokens_out"] is not None]
    prices = [r["list_usd"] for r in records if r["list_usd"] is not None]
    would = [r["would_reconcile"] for r in records]
    return {
        "files": len(records),
        "reconciled": sum(1 for r in records if r["outcome"] == "reconciled"),
        "would_reconcile": {"true": would.count(True), "false": would.count(False),
                            "null": would.count(None)},
        "refused_by_reason": refused,
        "wrong_rows_returned": sum(1 for r in records if r["rows"] > 0 and r["outcome"] != "reconciled"),
        "errored": errored,
        "seconds_total": round(sum(r["seconds"] or 0 for r in records), 3),
        "tokens_in_total": sum(tokens_in) if tokens_in else None,
        "tokens_out_total": sum(tokens_out) if tokens_out else None,
        "list_usd_total": round(sum(prices), 4) if prices else None,
    }


def _markdown(arm: str, corpus: str, records: list[dict], summary: dict, environment: dict) -> str:
    token_columns = " tokens_in | tokens_out | list_usd |" if arm == "a" else ""
    rescored = environment.get("mode") == "rescore"
    lines = [f"### {corpus.upper()} — arm {arm.upper()}" + (" (offline re-score)" if rescored else ""), "",
             f"Run {environment['utc_timestamp']} at `{environment['commit']}`."
             + (f" Prompt `{environment['prompt_version']}`." if arm == "a" else "")
             + (" Saved replies re-judged with the balance anchor; zero model calls; seconds not re-measured."
                if rescored else ""), "",
             f"| id | country | pages | rows | outcome | would_reconcile | seconds |{token_columns}",
             "|---|---|---:|---:|---|---|---:|" + ("---:|---:|---:|" if arm == "a" else "")]
    for record in records:
        would = "" if record["would_reconcile"] is None else str(record["would_reconcile"]).lower()
        seconds = "" if record["seconds"] is None else f"{record['seconds']:.2f}"
        cells = (f"| {record['id']} | {record['country']} | {record['pages']} | {record['rows']} | "
                 f"{record['outcome']} | {would} | {seconds} |")
        if arm == "a":
            cells += (f" {record['tokens_in'] if record['tokens_in'] is not None else ''} | "
                      f"{record['tokens_out'] if record['tokens_out'] is not None else ''} | "
                      f"{record['list_usd'] if record['list_usd'] is not None else ''} |")
        lines.append(cells)
    would = summary["would_reconcile"]
    lines += ["", f"**{summary['reconciled']} of {summary['files']} reconciled; "
                  f"would_reconcile true {would['true']}, false {would['false']}, null {would['null']}; "
                  f"wrong_rows_returned {summary['wrong_rows_returned']}; "
                  f"errored {summary['errored']}; {summary['seconds_total']:.1f} s total.**", ""]
    return "\n".join(lines)


def run(arm: str, corpus: str, ids: set[str] | None = None) -> int:
    tag = "-retry" if ids else ""
    out_json = _path(arm, corpus, "json", tag)
    out_md = _path(arm, corpus, "md", tag)
    if corpus == "heldout" and out_json.is_file():
        print(f"{out_json.name} already exists: a held-out run is recorded; refusing a second one.",
              file=sys.stderr)
        return 2

    manifest = json.loads(REAL_MANIFEST.read_text())
    expected = {item["id"]: item for item in manifest["files"]}
    meta = json.loads(META.read_text())
    wanted_set = "held" if corpus == "heldout" else "tune"
    verified: list[tuple[dict, bytes]] = []
    for item in meta["files"]:
        if item["set"] != wanted_set:
            continue
        entry = expected.get(item["id"])
        if entry is None:
            raise SystemExit(f"{item['id']}: not in the committed manifest-real.json")
        if entry["set"] != wanted_set:
            raise SystemExit(f"{item['id']}: the committed split does not put it in {wanted_set!r}")
        path = RAW / f"{item['id']}.pdf"
        if not path.is_file():
            raise SystemExit(f"{item['id']}: sample is missing")
        data = path.read_bytes()
        if _sha(data) != entry["sha256"]:
            raise SystemExit(f"{item['id']}: sha256 does not match the committed manifest-real.json")
        if item.get("sha256") and item["sha256"] != _sha(data):
            raise SystemExit(f"{item['id']}: sha256 does not match its metadata")
        if ids is not None and item["id"] not in ids:
            continue
        verified.append((item, data))
    if ids is not None:
        missing = ids - {item["id"] for item, _data in verified}
        if missing:
            raise SystemExit(f"unknown tune ids: {', '.join(sorted(missing))}")
    if not verified:
        raise SystemExit(f"no {wanted_set} files in {META}")
    if ids is None and sorted(item["id"] for item, _data in verified) != sorted(
            entry["id"] for entry in manifest["files"] if entry["set"] == wanted_set):
        raise SystemExit(f"the metadata and the committed manifest disagree about the {wanted_set} set")

    records: list[dict] = []
    for item, data in verified:
        on_reply = (lambda sent, item=item: _save_reply(corpus, item, sent)) if arm == "a" else None
        record = _measure(arm, item, data, on_reply)
        records.append(record)
        if arm == "a":
            _log_model_call(item, record, "measured")
        print(f"{record['id']:<5} {record['country']:<4} {record['pages']:>3} {record['rows']:>5} "
              f"{record['outcome']:<26} {record['seconds']:>7.2f}"
              + (f"  {record['tokens_in']}/{record['tokens_out']}" if arm == "a" else ""))

    summary = _summary(records)
    payload = {
        "arm": arm.upper(),
        "corpus": corpus,
        "environment": _environment(arm, corpus),
        "files": records,
        "summary": summary,
    }
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    out_md.write_text(_markdown(arm, corpus, records, summary, payload["environment"]))

    print()
    print(f"ARM {arm.upper()} {corpus.upper()}: reconciled {summary['reconciled']}/{summary['files']}, "
          f"refused {summary['refused_by_reason']}, wrong rows returned "
          f"{summary['wrong_rows_returned']}, errored {summary['errored']}, "
          f"{summary['seconds_total']:.1f} s")
    return 0 if summary["wrong_rows_returned"] == 0 and summary["errored"] == 0 else 1


def _rescore_one(saved: dict) -> dict:
    """One saved arm A reply through the anchor and the judge again; never a model call."""
    record = _new_record(saved)
    if saved.get("reply") is None:
        code = saved.get("error") or "failed"
        record["outcome"] = f"errored:{code}" if code in ERRORED_CODES else f"refused:{code}"
    else:
        try:
            rows, candidate = extract.judge_reply(saved["reply"], saved["statement_text"], saved["profile"])
        except (layout.PdfSpikeError, extract.ExtractionError) as exc:
            _record_refusal(record, exc, saved["profile"])
        else:
            record["rows"] = len(rows)
            record["outcome"] = "reconciled"
            record["would_reconcile"] = _would_reconcile(candidate, saved["profile"])
    # The usage is the original request's: a re-score costs nothing.
    record["tokens_in"] = saved.get("tokens_in")
    record["tokens_out"] = saved.get("tokens_out")
    record["list_usd"] = saved.get("list_usd")
    record["truncated"] = bool(saved.get("truncated", False))
    return record


def rescore(corpus: str) -> int:
    """Re-judge every saved arm A reply for `corpus` with the balance anchor: zero model calls, no PDF read."""
    folder = REPLIES / corpus
    paths = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not paths:
        print(f"no saved arm A replies in {folder}: nothing to re-score (a run made before replies were "
              f"saved cannot be re-scored).", file=sys.stderr)
        return 2
    records: list[dict] = []
    commits: set[str] = set()
    for path in paths:
        saved = json.loads(path.read_text())
        if saved.get("corpus") != corpus:
            raise SystemExit(f"{path.name}: saved for corpus {saved.get('corpus')!r}, not {corpus!r}")
        commits.add(saved.get("commit") or "unknown")
        record = _rescore_one(saved)
        records.append(record)
        print(f"{record['id']:<5} {record['country']:<4} {record['pages']:>3} {record['rows']:>5} "
              f"{record['outcome']:<26}")

    summary = _summary(records)
    environment = _environment("a", corpus)
    environment.update({"mode": "rescore", "model_calls": 0, "replies_commits": sorted(commits)})
    payload = {"arm": "A", "corpus": corpus, "environment": environment, "files": records, "summary": summary}
    _path("a", corpus, "json", "-rescore").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    _path("a", corpus, "md", "-rescore").write_text(_markdown("a", corpus, records, summary, environment))

    print()
    print(f"ARM A {corpus.upper()} RE-SCORE (anchored, zero model calls): reconciled "
          f"{summary['reconciled']}/{summary['files']}, refused {summary['refused_by_reason']}, "
          f"wrong rows returned {summary['wrong_rows_returned']}, errored {summary['errored']}")
    return 0 if summary["wrong_rows_returned"] == 0 and summary["errored"] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("a", "d"), required=True)
    parser.add_argument("--corpus", choices=("tune", "heldout"), required=True)
    parser.add_argument("--ids", help="tune only: a comma-separated subset to re-measure into -retry files")
    parser.add_argument("--rescore", action="store_true",
                        help="arm A only: re-judge the saved replies offline with the balance anchor "
                             "(zero model calls)")
    args = parser.parse_args(argv)
    ids = {item.strip() for item in args.ids.split(",") if item.strip()} if args.ids else None
    if ids and args.corpus != "tune":
        parser.error("--ids is only for a tune re-measurement")
    if args.rescore:
        if args.arm.lower() != "a" or ids:
            parser.error("--rescore is arm A only and takes no --ids")
        return rescore(args.corpus)
    return run(args.arm.lower(), args.corpus, ids)


if __name__ == "__main__":
    raise SystemExit(main())
