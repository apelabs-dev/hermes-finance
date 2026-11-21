"""Measure the PDF spike's seam over the committed synthetic corpus and the real-statement sets.

    .dispatch/venv/bin/python tests/pdfspike_run.py --corpus synthetic   # writes results.json, prints GATE:
    .dispatch/venv/bin/python tests/pdfspike_run.py --corpus local       # external signal, non-gating
    .dispatch/venv/bin/python tests/pdfspike_run.py --corpus tune \
        --samples-dir .dispatch/samples/tune --meta .dispatch/samples/tune-meta.json \
        --out .dispatch/tune-results.json
    .dispatch/venv/bin/python tests/pdfspike_run.py --corpus heldout \
        --samples-dir .../samples/held --meta .../samples/held-meta.json \
        --out .dispatch/heldout-results.json

A synthetic file PASSES only when the row count is exact AND the parsed amounts reconcile the
statement's own printed opening -> closing balance within 0.01. Because the seam returns amounts
in the app's sign (money out > 0), the balance check is `opening - sum(amounts) == closing`; the
equivalent statement with money-in-positive amounts is `opening + sum == closing`.

The local, tune and held-out records carry only ids, countries, page counts, row counts, booleans,
outcomes and seconds: no description, amount, bank name or account number is written or printed.
The runner verifies every real sample's sha256 against the committed manifest-real.json and, for
held-out files, refuses to run a second time once `.dispatch/heldout-run.json` exists (unless
`--force`, which the measurement protocol never uses).
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
FIXTURES = HERE / "fixtures" / "pdfspike"
RESULTS = FIXTURES / "results.json"
LOCAL = HERE.parent / ".dispatch" / "corpus-local"
REAL_MANIFEST = FIXTURES / "manifest-real.json"
HELDOUT_MARKER = HERE.parent / ".dispatch" / "heldout-run.json"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
import pdfspike_layout as layout  # noqa: E402
from pdfspike_corpus import LOCKED_PASSWORD  # noqa: E402

GATE_PASSES = 16
GATE_COUNTRIES = 6
TOLERANCE_CENTS = 1


def _cents(value: float) -> int:
    return int(round(value * 100))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _engine_version() -> str:
    try:
        import pdfplumber
        return pdfplumber.__version__
    except ImportError:
        return "not installed"


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _environment() -> dict:
    return {
        "python": sys.version.split()[0],
        "pdfplumber": _engine_version(),
        "platform": platform.platform(),
        "date": date.today().isoformat(),
        "commit": _git_sha(),
        "utc_timestamp": _utc_now(),
        "runner": "tests/pdfspike_run.py",
    }


def _measure(item: dict, data: bytes) -> dict:
    record = {
        "id": item["id"],
        "country": item["country"],
        "profile": item["profile"],
        "pages": item["pages"],
        "sha256_ok": _sha(data) == item["sha256"],
        "expected_rows": item["expected_rows"],
        "opening_balance": item["opening_balance"],
        "closing_balance": item["closing_balance"],
        "rows_found": 0,
        "amount_matches": 0,
        "date_matches": 0,
        "reconcile_delta": None,
        "seconds": None,
        "verdict": "fail",
        "fail_reason": None,
    }
    started = time.perf_counter()
    try:
        parsed = layout.rows(data, profile=item["profile"])
    except layout.PdfSpikeError as exc:
        record["seconds"] = round(time.perf_counter() - started, 4)
        record["fail_reason"] = f"error:{exc.code}"
        return record
    except Exception as exc:      # noqa: BLE001 - an unexpected engine failure is still a fail, not a crash
        record["seconds"] = round(time.perf_counter() - started, 4)
        record["fail_reason"] = f"error:{type(exc).__name__}"
        return record
    record["seconds"] = round(time.perf_counter() - started, 4)
    record["rows_found"] = len(parsed)

    detail = item["expected_rows_detail"]
    for got, expected in zip(parsed, detail):
        if abs(got.amount - expected["amount"]) <= 0.005:
            record["amount_matches"] += 1
        if got.date == expected["date"]:
            record["date_matches"] += 1

    delta_cents = (_cents(item["closing_balance"])
                   - (_cents(item["opening_balance"]) - sum(_cents(row.amount) for row in parsed)))
    record["reconcile_delta"] = round(delta_cents / 100, 2)
    row_count_ok = len(parsed) == item["expected_rows"]
    reconcile_ok = abs(delta_cents) <= TOLERANCE_CENTS
    record["row_count_ok"] = row_count_ok
    record["reconcile_ok"] = reconcile_ok
    if row_count_ok and reconcile_ok:
        record["verdict"] = "pass"
    elif not row_count_ok:
        record["fail_reason"] = "row_count"
    else:
        record["fail_reason"] = "reconcile"
    return record


def _negative_controls(manifest: dict) -> list[dict]:
    observations = []
    for control in manifest["negative_controls"]:
        path = FIXTURES / control["file"]
        data = path.read_bytes()
        if control["file"] == "locked.pdf":
            attempts = (("locked_missing", None), ("locked_wrong", "not-the-password"),
                        ("locked_right", LOCKED_PASSWORD))
            for label, password in attempts:
                try:
                    count = len(layout.rows(data, password, profile="AU"))
                    observed = f"rows:{count}"
                except layout.PdfSpikeError as exc:
                    observed = exc.code
                observations.append({"id": label, "file": control["file"], "observed": observed})
        else:
            try:
                count = len(layout.rows(data, profile="AU"))
                observed = f"rows:{count}"
            except layout.PdfSpikeError as exc:
                observed = exc.code
            observations.append({"id": control["id"], "file": control["file"], "observed": observed})
    return observations


def _local_out_of_sample() -> dict:
    manifest_path = LOCAL / "manifest.json"
    if not manifest_path.is_file():
        return {"ran": False, "note": "the local corpus is not present (it is never committed)"}
    manifest = json.loads(manifest_path.read_text())
    records = []
    for item in manifest["files"]:
        data = (LOCAL / item["file"]).read_bytes()
        started = time.perf_counter()
        try:
            result = layout.parse(data, profile=item["profile"])
            rows = len(result.rows)
            found = result.opening is not None and result.closing is not None
            reconciled = bool(found and abs(_cents(result.closing)
                                           - (_cents(result.opening) - sum(_cents(r.amount) for r in result.rows)))
                              <= TOLERANCE_CENTS)
            records.append({"id": item["id"], "country": item["country"], "pages": item["pages"],
                            "rows": rows, "printed_opening_found": result.opening is not None,
                            "printed_closing_found": result.closing is not None,
                            "reconciled": reconciled, "text_layer": True,
                            "seconds": round(time.perf_counter() - started, 4)})
        except layout.PdfSpikeError as exc:
            records.append({"id": item["id"], "country": item["country"], "pages": item["pages"],
                            "rows": 0, "printed_opening_found": False, "printed_closing_found": False,
                            "reconciled": False, "text_layer": exc.code != "no_text_layer",
                            "seconds": round(time.perf_counter() - started, 4)})
    text_layer = sum(1 for r in records if r["text_layer"])
    reconciled = sum(1 for r in records if r["reconciled"])
    return {"ran": True, "fetched": len(records), "text_layer": text_layer, "reconciled": reconciled,
            "files": records}


def _seam_reconciles(result) -> bool:
    if result.opening is None or result.closing is None:
        return True     # a running-balance-only statement was checked inside the seam
    delta = _cents(result.closing) - (_cents(result.opening) - sum(_cents(row.amount) for row in result.rows))
    return abs(delta) <= TOLERANCE_CENTS


def _measure_real(item: dict, data: bytes) -> dict:
    record = {
        "id": item["id"],
        "country": item["country"],
        "pages": item["pages"],
        "rows": 0,
        "reconciled": False,
        "outcome": None,
        "seconds": None,
    }
    started = time.perf_counter()
    try:
        result = layout.parse(data, profile=item["profile"])
    except layout.PdfSpikeError as exc:
        record["seconds"] = round(time.perf_counter() - started, 4)
        record["outcome"] = f"refused:{exc.code}"
        return record
    except Exception:      # noqa: BLE001 - an unexpected engine failure is a refusal, never a crash
        record["seconds"] = round(time.perf_counter() - started, 4)
        record["outcome"] = "refused:error"
        return record
    record["seconds"] = round(time.perf_counter() - started, 4)
    record["rows"] = len(result.rows)
    record["reconciled"] = _seam_reconciles(result)
    record["outcome"] = "reconciled"
    return record


def run_real(corpus: str, samples_dir: Path, meta_path: Path, out_path: Path, force: bool = False) -> int:
    if corpus == "heldout" and HELDOUT_MARKER.is_file() and not force:
        print("a held-out run is already recorded; refusing to measure a second time (--force is not used in the "
              "measurement protocol)", file=sys.stderr)
        return 2

    manifest = json.loads(REAL_MANIFEST.read_text())
    expected = {item["id"]: item for item in manifest["files"]}
    meta = json.loads(meta_path.read_text())
    verified = []
    for item in meta["files"]:
        entry = expected.get(item["id"])
        if entry is None:
            raise SystemExit(f"{item['id']}: not in the committed manifest-real.json")
        expected_set = "held" if corpus == "heldout" else corpus
        if entry["set"] != expected_set:
            raise SystemExit(f"{item['id']}: the committed split does not put it in {expected_set!r}")
        data = (samples_dir / f"{item['id']}.pdf").read_bytes()
        actual = _sha(data)
        if actual != entry["sha256"]:
            raise SystemExit(f"{item['id']}: sha256 does not match the committed manifest-real.json")
        if item.get("sha256") and item["sha256"] != actual:
            raise SystemExit(f"{item['id']}: sha256 does not match its metadata")
        verified.append((item, data))
    records = [_measure_real(item, data) for item, data in verified]

    reconciled = sum(1 for record in records if record["reconciled"])
    refused = sum(1 for record in records if record["outcome"].startswith("refused:"))
    wrong_rows = sum(1 for record in records if record["rows"] > 0 and not record["reconciled"])
    payload = {
        "corpus": corpus,
        "environment": _environment(),
        "files": records,
        "summary": {
            "files": len(records),
            "reconciled": reconciled,
            "refused": refused,
            "wrong_rows_returned": wrong_rows,
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    print(f"{'id':<5} {'ctry':<4} {'pg':>3} {'rows':>5} {'recon':>5} {'secs':>6}  outcome")
    print("-" * 52)
    for record in records:
        print(f"{record['id']:<5} {record['country']:<4} {record['pages']:>3} {record['rows']:>5} "
              f"{str(record['reconciled']):>5} {record['seconds']:>6.3f}  {record['outcome']}")
    print(f"{corpus.upper()}: reconciled {reconciled}/{len(records)}, refused {refused}, "
          f"wrong rows returned {wrong_rows}")

    if corpus == "heldout":
        HELDOUT_MARKER.write_text(json.dumps({
            "commit": payload["environment"]["commit"],
            "utc_timestamp": payload["environment"]["utc_timestamp"],
            "results": out_path.name,
        }, indent=2) + "\n")
    return 0 if wrong_rows == 0 else 1


def _print_table(records: list[dict]) -> None:
    header = f"{'id':<7} {'ctry':<4} {'pg':>3} {'rows':>5} {'exp':>5} {'amt':>5} {'date':>5} " \
             f"{'delta':>9} {'secs':>6}  verdict"
    print(header)
    print("-" * len(header))
    for r in records:
        delta = "" if r["reconcile_delta"] is None else f"{r['reconcile_delta']:+.2f}"
        verdict = r["verdict"] + (f" ({r['fail_reason']})" if r["fail_reason"] else "")
        print(f"{r['id']:<7} {r['country']:<4} {r['pages']:>3} {r['rows_found']:>5} {r['expected_rows']:>5} "
              f"{r['amount_matches']:>5} {r['date_matches']:>5} {delta:>9} {r['seconds']:>6.3f}  {verdict}")


def run_synthetic() -> dict:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    records = []
    for item in manifest["files"]:
        data = (FIXTURES / f"{item['id']}.pdf").read_bytes()
        if _sha(data) != item["sha256"]:
            raise SystemExit(f"sha256 mismatch for {item['id']}.pdf: the committed bytes are not the measured ones")
        records.append(_measure(item, data))

    passes = sum(1 for r in records if r["verdict"] == "pass")
    passing_countries = sorted({r["country"] for r in records if r["verdict"] == "pass"})
    total_seconds = sum(r["seconds"] or 0 for r in records)
    per_file = [r["seconds"] for r in records if r["seconds"] is not None]
    gate_pass = passes >= GATE_PASSES and len(passing_countries) >= GATE_COUNTRIES
    local = _local_out_of_sample()
    retained = False
    if not local.get("ran") and RESULTS.is_file():
        previous = json.loads(RESULTS.read_text()).get("local_out_of_sample")
        if previous and previous.get("ran"):
            local = previous
            retained = True
    results = {
        "environment": _environment(),
        "reconciliation": {
            "convention": "app sign: > 0 money out, < 0 money in",
            "check": "opening_balance - sum(rows.amount) == closing_balance within 0.01",
            "equivalent_money_in_positive": "opening_balance + sum(rows.amount) == closing_balance",
            "tolerance": 0.01,
        },
        "files": records,
        "summary": {
            "passes": passes,
            "denominator": len(records),
            "countries": len(passing_countries),
            "countries_total": len({r["country"] for r in records}),
            "pass_rate": round(passes / len(records), 4) if records else 0.0,
            "total_seconds": round(total_seconds, 3),
            "per_file_seconds_min": min(per_file) if per_file else None,
            "per_file_seconds_max": max(per_file) if per_file else None,
        },
        "gate": {
            "pass": gate_pass,
            "required_passes": GATE_PASSES,
            "required_countries": GATE_COUNTRIES,
            "passes": passes,
            "countries": len(passing_countries),
            "required": f"{GATE_PASSES}/{len(records)} required",
        },
        "negative_controls": _negative_controls(manifest),
        "local_out_of_sample": local,
    }
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    _print_table(records)
    print()
    print(f"pdfplumber {results['environment']['pdfplumber']} · python {results['environment']['python']} · "
          f"{total_seconds:.2f}s total ({min(per_file):.3f}s .. {max(per_file):.3f}s per file)"
          if per_file else "no files measured")
    print(f"negative controls: " + ", ".join(f"{c['id']}={c['observed']}" for c in results["negative_controls"]))
    local = results["local_out_of_sample"]
    if local.get("ran"):
        label = "recorded local out-of-sample" if retained else "local out-of-sample"
        print(f"{label}: fetched {local['fetched']}, text-layer {local['text_layer']}, "
              f"reconciled {local['reconciled']}")
    print(f"GATE: {'PASS' if gate_pass else 'FAIL'}  {passes}/{len(records)}")
    return results


def run_local() -> int:
    if not (LOCAL / "manifest.json").is_file():
        print(f"no local manifest at {LOCAL / 'manifest.json'}", file=sys.stderr)
        return 2
    data = _local_out_of_sample()
    print(f"{'id':<9} {'ctry':<4} {'pages':>5} {'rows':>5} {'open':>5} {'close':>5} {'recon':>6} "
          f"{'text':>5} {'secs':>7}")
    for r in data["files"]:
        print(f"{r['id']:<9} {r['country']:<4} {r['pages']:>5} {r['rows']:>5} "
              f"{str(r['printed_opening_found']):>5} {str(r['printed_closing_found']):>5} "
              f"{str(r['reconciled']):>6} {str(r['text_layer']):>5} {r['seconds']:>7.3f}")
    print(f"LOCAL: fetched {data['fetched']}, text-layer {data['text_layer']}, reconciled {data['reconciled']} "
          f"(non-gating; row counts cannot be independently verified)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", choices=("synthetic", "local", "tune", "heldout"), default="synthetic")
    parser.add_argument("--samples-dir", type=Path)
    parser.add_argument("--meta", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--force", action="store_true", help="re-run a held-out measurement (never used)")
    args = parser.parse_args()
    if args.corpus == "local":
        return run_local()
    if args.corpus in ("tune", "heldout"):
        if args.samples_dir is None or args.meta is None or args.out is None:
            parser.error("--samples-dir, --meta and --out are required for tune and heldout")
        return run_real(args.corpus, args.samples_dir, args.meta, args.out, force=args.force)
    results = run_synthetic()
    return 0 if results["gate"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
