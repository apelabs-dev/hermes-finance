"""Teach-once on a second statement: the measured pair runner (0.5, card A2).

Dev-only. It reads the committed synthetic statement corpus, groups the statements by the engine's own masked
layout fingerprint (or, with `--group-by profile`, by their generator/issuer profile), and for every pair teaches a
parser template on A, then imports B first-sight with that saved template. No network, no model call, no owner
statement and no database outside a temp dir.

    env -u PYTHONPATH PYTHONPATH=$PWD/src .venv/bin/python tests/pdfteach_pairs.py       # Markdown + summary
    env -u PYTHONPATH PYTHONPATH=$PWD/src .venv/bin/python tests/pdfteach_pairs.py --json out.json
    env -u PYTHONPATH PYTHONPATH=$PWD/src .venv/bin/python tests/pdfteach_pairs.py --group-by profile

The teacher sees only A: a header line is read from A's words, the engine's template keys are searched in a fixed
simplest-first order, and a candidate counts as taught only when A itself reconciles (the product's rule). Nothing
from B is used to choose the config. `--json` writes every per-pair row; stdout carries no statement text.

The pair census is the point of the runner. It does not lower the card's gate: a pair whose A cannot be taught is
a failure of the pair, not a pair to drop.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from hermes_finance import db  # noqa: E402
from hermes_finance.pdfimport import PdfRefused, layout, parse_statement, templates, words  # noqa: E402

FIXTURES = HERE / "fixtures" / "pdfspike"
MANIFEST = FIXTURES / "manifest.json"

# The template keys a teacher can set from A's visible header, simplest first. `skip_pattern` is the zero placeholder
# of the stacked-cell layout; it is escaped so it never looks like an amount to `templates.clean_config`. Patterns are
# escaped plain text only (0.5 review): a class such as `^0[.,]00$` is no longer accepted, so it has no variant here.
OVERRIDES = (
    {},
    {"date_order": "MDY"},
    {"date_order": "DMY"},
    {"row_start": "date"},
    {"skip_pattern": r"^0\.00$"},
    {"row_start": "date", "skip_pattern": r"^0\.00$"},
    {"amount_sign": "explicit"},
    {"amount_sign": "unsigned_is_debit"},
    {"amount_sign": "unsigned_is_credit"},
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _rows_match(rows, expected: list[dict]) -> tuple[bool, str]:
    """Exact row count, ISO date and signed amount against the generator manifest (never a description)."""
    if len(rows) != len(expected):
        return False, f"row count {len(rows)} != {len(expected)}"
    for i, (row, want) in enumerate(zip(rows, expected)):
        if row.date != want["date"]:
            return False, f"row {i}: date mismatch"
        got = Decimal(str(row.amount)).quantize(Decimal("0.01"))
        if got != Decimal(str(want["amount"])).quantize(Decimal("0.01")):
            return False, f"row {i}: amount mismatch"
    return True, "ok"


def _fingerprints(statements: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for fid, item in statements.items():
        pages, sizes = words.read(item["raw"])
        out[fid] = templates.fingerprint(pages, sizes, words.producer(item["raw"]))
    return out


def _header_base(raw: bytes) -> dict | None:
    """A's first recognizable header line as a template base: its roles, their header extents and header words."""
    pages, _ = words.read(raw)
    lines = words.build_lines(pages)
    for line in lines:
        cols = layout.header_columns(line)
        if cols:
            lexicon = layout._header_lexicon()  # the engine's own generic header words, never merchant text
            header_words = [w.text.lower() for w in line.words if w.text.lower() in lexicon][:8]
            if not header_words:
                return None
            return {"header_words": header_words,
                    "columns": [{"role": c.role, "x0": round(c.hx0, 1), "x1": round(c.hx1, 1)} for c in cols]}
    return None


def _teach(raw: bytes) -> tuple[dict | None, str, str]:
    """(config, label, last refusal code). A candidate is taught only when A reconciles with it."""
    base = _header_base(raw)
    if base is None:
        return None, "no header", "no_header"
    last = "no_header"
    for extra in OVERRIDES:
        cfg = {**base, **extra}
        try:
            parse_statement(None, raw, template=cfg)
        except PdfRefused as exc:
            last = exc.code
            continue
        except ValueError:  # an invalid candidate is not a teach; try the next one
            last = "template_invalid"
            continue
        label = ", ".join(f"{k}={v}" for k, v in extra.items()) or "columns only"
        return cfg, label, ""
    return None, "no candidate", last


def _census(statements: dict[str, dict], fps: dict[str, dict]) -> list[list[str]]:
    ids = sorted(statements)
    groups: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for fid in ids:
        group = tuple(sorted(other for other in ids if templates.same_layout(fps[fid], fps[other])))
        if group not in seen:
            seen.add(group)
            groups.append(list(group))
    return groups


def _census_profile(statements: dict[str, dict]) -> list[list[str]]:
    """The cautious view: group by the generator/issuer profile, not by the fingerprint `templates.find` matches.

    A second statement whose columns drifted has a different fingerprint and would otherwise never pair; here it is
    a pair that teach-once has to survive (and, where it does not, a counted failure)."""
    groups: dict[str, list[str]] = {}
    for fid in sorted(statements):
        groups.setdefault(statements[fid]["profile"], []).append(fid)
    return [sorted(ids) for _profile, ids in sorted(groups.items())]


def _teach_once(fp_a: dict, cfg: dict, item: dict) -> dict:
    """Save A's template into a fresh DB, then import B with it. Also reports B without any template."""
    result = {"found": False, "forced": {"status": "not_run"}, "cascade": {"status": "not_run"},
              "first_sight": {"status": "not_run"}, "false_accept": False}
    raw_b = item["raw"]
    expected = item["expected_rows_detail"]

    try:
        plain = parse_statement(None, raw_b)
        ok, detail = _rows_match(plain.rows, expected)
        result["first_sight"] = {"status": "correct" if ok else "wrong", "detail": detail}
    except PdfRefused as exc:
        result["first_sight"] = {"status": "refused", "code": exc.code}

    with tempfile.TemporaryDirectory() as td:
        conn = db.connect(Path(td) / "teach.db")
        try:
            saved = templates.save(conn, fp_a, cfg, "user")
            found = templates.find(conn, item["fingerprint_b"])
            result["found"] = found is not None and found["id"] == saved["id"]
            if found is not None:
                try:
                    forced = parse_statement(None, raw_b, template=found["config"])
                    ok, detail = _rows_match(forced.rows, expected)
                    result["forced"] = {"status": "correct" if ok else "wrong", "detail": detail}
                except PdfRefused as exc:
                    result["forced"] = {"status": "refused", "code": exc.code}
            try:
                cascade = parse_statement(conn, raw_b)
                ok, detail = _rows_match(cascade.rows, expected)
                result["cascade"] = {"status": "correct" if ok else "wrong", "detail": detail,
                                     "used": "template" if cascade.template else "generic"}
            except PdfRefused as exc:
                result["cascade"] = {"status": "refused", "code": exc.code, "used": "-"}
        finally:
            conn.close()
    result["false_accept"] = any(result[k]["status"] == "wrong" for k in ("forced", "cascade"))
    return result


def measure(group_by: str = "fingerprint") -> dict:
    started = time.monotonic()
    manifest = json.loads(MANIFEST.read_text())
    statements: dict[str, dict] = {}
    for item in manifest["files"]:
        raw = (FIXTURES / f"{item['id']}.pdf").read_bytes()
        if _sha(raw) != item["sha256"]:
            raise SystemExit(f"{item['id']}: sha256 does not match the committed manifest")
        statements[item["id"]] = {"raw": raw, "expected_rows_detail": item["expected_rows_detail"],
                                  "country": item["country"], "profile": item["profile"]}
    fps = _fingerprints(statements)
    for fid, fp in fps.items():
        statements[fid]["fingerprint"] = fp
    groups = _census(statements, fps) if group_by == "fingerprint" else _census_profile(statements)
    pairs = [pair for group in groups for pair in itertools.combinations(group, 2)]

    rows = []
    for a, b in pairs:
        cfg, label, refusal = _teach(statements[a]["raw"])
        statements[b]["fingerprint_b"] = fps[b]
        if cfg is None:
            outcome = {"found": False, "forced": {"status": "not_run"}, "cascade": {"status": "not_run"},
                       "first_sight": {"status": "refused", "code": refusal}, "false_accept": False}
            # A first-sight baseline still exists even when A cannot be taught.
            try:
                plain = parse_statement(None, statements[b]["raw"])
                ok, detail = _rows_match(plain.rows, statements[b]["expected_rows_detail"])
                outcome["first_sight"] = {"status": "correct" if ok else "wrong", "detail": detail}
            except PdfRefused as exc:
                outcome["first_sight"] = {"status": "refused", "code": exc.code}
            outcome["false_accept"] = outcome["first_sight"]["status"] == "wrong"
            entry = {"a": a, "b": b, "layout": statements[a]["country"], "teach": "refused", "teach_code": refusal,
                     **outcome}
        else:
            outcome = _teach_once(fps[a], cfg, statements[b])
            entry = {"a": a, "b": b, "layout": statements[a]["country"], "teach": "accepted", "teach_config": label,
                     **outcome}
        rows.append(entry)

    pairs_total = len(rows)
    teach_ok = [r for r in rows if r["teach"] == "accepted"]
    taught_correct = [r for r in teach_ok if r["cascade"]["status"] == "correct"]
    forced_correct = [r for r in teach_ok if r["forced"]["status"] == "correct"]
    first_sight_correct = [r for r in rows if r["first_sight"]["status"] == "correct"]
    false_accepts = [r for r in rows if r["false_accept"]]
    second_statements = sorted({r["b"] for r in rows})
    second_after_teach = [b for b in second_statements if any(r["b"] == b and r["cascade"]["status"] == "correct"
                                                              for r in teach_ok)]
    second_first_sight = [b for b in second_statements if any(r["b"] == b and r["first_sight"]["status"] == "correct"
                                                              for r in rows)]

    # One teach per layout: teach on a group's first statement, then import every other statement of that group.
    one_per_layout = []
    for group in groups:
        if len(group) < 2:
            continue
        a = group[0]
        cfg, label, refusal = _teach(statements[a]["raw"])
        for b in group[1:]:
            statements[b]["fingerprint_b"] = fps[b]
            if cfg is None:
                one_per_layout.append({"layout": statements[a]["country"], "a": a, "b": b, "teach": "refused",
                                       "teach_code": refusal, "cascade": {"status": "not_run"}})
            else:
                outcome = _teach_once(fps[a], cfg, statements[b])
                one_per_layout.append({"layout": statements[a]["country"], "a": a, "b": b, "teach": "accepted",
                                       "teach_config": label, **outcome})

    seconds = round(time.monotonic() - started, 2)
    label_key = "country" if group_by == "fingerprint" else "profile"
    return {
        "corpus": "tests/fixtures/pdfspike (committed synthetic statements, sha256-pinned)",
        "group_by": group_by,
        "statements": len(statements),
        "groups": [{"layout": statements[g[0]][label_key], "ids": g, "size": len(g)} for g in groups],
        "pairs_total": pairs_total,
        "pairs": rows,
        "teach_accepted": len(teach_ok),
        "teach_refused": pairs_total - len(teach_ok),
        "forced_correct": len(forced_correct),
        "cascade_correct": len(taught_correct),
        "first_sight_pairs_correct": len(first_sight_correct),
        "false_accepts": len(false_accepts),
        "second_statements": len(second_statements),
        "second_after_teach_correct": len(second_after_teach),
        "second_first_sight_correct": len(second_first_sight),
        "one_per_layout": one_per_layout,
        "one_per_layout_second_statements": len(one_per_layout),
        "one_per_layout_correct": len([r for r in one_per_layout if r["cascade"]["status"] == "correct"]),
        "seconds": seconds,
    }


def _mark(result: dict) -> str:
    """A compact, text-free cell for one pair outcome."""
    status = result.get("status")
    if status == "correct":
        return "correct"
    if status == "wrong":
        return "WRONG"
    if status == "refused":
        return f"refused:{result.get('code', '?')}"
    return "—"


def markdown(data: dict) -> str:
    lines = [
        "| pair | layout | teach on A | found | B, template | B, cascade (used) | B, first sight | false accept |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in data["pairs"]:
        found = "yes" if row["found"] else "no"
        cascade = _mark(row["cascade"])
        if row["cascade"].get("used"):
            cascade += f" ({row['cascade']['used']})"
        teach = row["teach"] if row["teach"] == "accepted" else f"refused:{row['teach_code']}"
        if row.get("teach_config"):
            teach += f" ({row['teach_config']})"
        lines.append(f"| {row['a']} → {row['b']} | {row['layout']} | {teach} | {found} | "
                     f"{_mark(row['forced'])} | {cascade} | {_mark(row['first_sight'])} | "
                     f"{'YES' if row['false_accept'] else 'no'} |")
    return "\n".join(lines)


def census(dirs: list[Path]) -> dict:
    """Layout-group census of any directory of statement PDFs: counts only, never statement text."""
    found: dict[str, dict] = {}
    for root in dirs:
        for path in sorted(root.rglob("*.pdf")):
            raw = path.read_bytes()
            try:
                pages, sizes = words.read(raw)
                fp = templates.fingerprint(pages, sizes, words.producer(raw))
            except Exception:  # noqa: BLE001 - a file that cannot be opened is reported as unreadable, not fatal
                found[f"{root.name}/{path.relative_to(root)}"] = None
                continue
            found[f"{root.name}/{path.relative_to(root)}"] = fp
    readable = {k: v for k, v in found.items() if v is not None}
    groups: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for fid in sorted(readable):
        group = tuple(sorted(other for other in readable if templates.same_layout(readable[fid], readable[other])))
        if group not in seen:
            seen.add(group)
            groups.append(list(group))
    return {"files": len(found), "unreadable": len(found) - len(readable),
            "groups": sorted((len(g) for g in groups), reverse=True),
            "multi": [g for g in groups if len(g) > 1]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", type=Path, help="write the full per-pair result here")
    parser.add_argument("--quiet", action="store_true", help="print only the summary")
    parser.add_argument("--group-by", choices=("fingerprint", "profile"), default="fingerprint",
                        help="pair by the engine's same_layout fingerprint (default) or by the generator/issuer profile")
    parser.add_argument("--census", type=Path, nargs="+",
                        help="only census these directories of PDFs (layout group sizes, no text)")
    args = parser.parse_args(argv)
    if args.census:
        data = census(args.census)
        print(f"files {data['files']}; unreadable {data['unreadable']}; groups {len(data['groups'])}; "
              f"sizes {data['groups']}; multi-statement groups {len(data['multi'])}")
        return 0
    data = measure(args.group_by)
    if not args.quiet:
        print(markdown(data))
        print()
    print(f"grouping {data['group_by']}; statements {data['statements']}; layout groups {len(data['groups'])}; "
          f"pairs {data['pairs_total']}; "
          f"teach accepted {data['teach_accepted']} / refused {data['teach_refused']}; "
          f"template correct {data['forced_correct']}; cascade correct {data['cascade_correct']}; "
          f"first-sight correct (pairs) {data['first_sight_pairs_correct']}; false accepts {data['false_accepts']}; "
          f"second statements {data['second_statements']} (after teach {data['second_after_teach_correct']}, "
          f"first sight {data['second_first_sight_correct']}); one teach per layout "
          f"{data['one_per_layout_correct']}/{data['one_per_layout_second_statements']}; "
          f"wall {data['seconds']:.2f}s")
    if args.json:
        args.json.write_text(json.dumps(data, indent=1, sort_keys=True, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
