#!/usr/bin/env python3
"""Proof that scripts/demo_canary.py fails loudly: one leak planted per rule, each in its own throwaway copy of a built
dist/ (the given dist/ is never written; its file hashes are checked before and after), and the canary must exit 1 with
that rule in its output. The untouched copy must pass. A/B plants use the children's scrubbed marker context, not the
parent's ambient private list. Exit 2 means B cannot be exercised (e.g. a service account with no repo tokens).

    uv run python scripts/demo_canary_selftest.py dist/ --private PATH

No owner value is written here either: the planted private token is read from --private at run time, the repo token from
scripts/dehardcode.py's private word list, the generator value and fixed amount from the canary's own generator run; the rest are
made-up shapes. The canary prints every hit masked, and so does this.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import demo_canary as canary  # noqa: E402


def tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(d).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def plant_json(dist: Path, rel: str, text: str) -> None:
    """Into the first transaction row's note (a string the page shows)."""
    p = dist / rel
    d = json.loads(p.read_text())
    d["tx"][0]["note"] = text
    p.write_text(json.dumps(d))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dist", type=Path)
    ap.add_argument("--private", type=Path, action="append", default=[])
    ap.add_argument('--synthetic-markers', action='store_true', help='exercise privacy rules using disposable invented owner/repo markers')
    args = ap.parse_args(argv)
    if args.synthetic_markers:
        if args.private:
            ap.error('--synthetic-markers does not read private files')
        with tempfile.TemporaryDirectory(prefix='hf-demo-markers-') as td:
            marker = Path(td) / 'private.txt'
            marker.write_text('planted-owner-marker-xx\n')
            repo = Path(td) / 'repo.txt'
            repo.write_text('planted-repo-marker-yy\n')
            os.environ['HERMES_FINANCE_PRIVATE_FILE'] = str(repo)
            os.environ.pop('HERMES_FINANCE_TEST_PRIVATE', None)
            return main([str(args.dist), '--private', str(marker)])
    dist = args.dist.resolve()
    before = tree_hash(dist)
    meta = json.loads((dist / "demo" / "data" / "core.json").read_text())["meta"]
    month = f"demo/data/m/{meta['this_month']}.json"
    gen, fixed = canary.generator_values(date.fromisoformat(meta["today"]))
    # Select A/B plants in the same fresh, scrubbed marker context as the canary children.
    # The parent's ambient private list must not choose a token the children never check.
    env = {k: v for k, v in os.environ.items() if k != "HERMES_FINANCE_TEST_PRIVATE"}
    context = subprocess.run(
        [sys.executable, "-c",
         "import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
         "import demo_canary as c; "
         "print(json.dumps([c.load_private([Path(p) for p in sys.argv[2:]]), c.repo_forbidden()]))",
         str(ROOT / "scripts"), *(str(p) for p in args.private)],
        capture_output=True, text=True, env=env, timeout=60, check=True)
    private, forbidden = json.loads(context.stdout)
    if not forbidden:
        # Service accounts can have no own_words(). Never crash or claim B was exercised.
        print("demo_canary_selftest: cannot exercise B: child repo personal-token list is empty")
        return 2
    merchant = next(v for k, v in gen if k == "merchant" and len(v) > 4)
    amount = max(fixed)
    whole, cents = divmod(amount, 100)
    plants = {   # rule expected in the output -> what to do to a fresh copy
        "A private token": lambda d: plant_json(d, month, f"note about {private[0]} here") if private else None,
        "B repo personal-token list": lambda d: (d / "index.html").write_text(
            (d / "index.html").read_text().replace("</body>", f"<p>{forbidden[0]}</p></body>")),
        "C this machine's login": lambda d: plant_json(d, month, f"see /Users/{getpass.getuser()}/x"),
        "D e-mail address": lambda d: plant_json(d, month, "write to planted.person@mailbox-provider.test"),
        "D VIN": lambda d: plant_json(d, month, "VIN 1ABCD23EF4G567890"),
        "D street address": lambda d: plant_json(d, month, "lives at 742 Evergreen Terrace"),
        "D tailnet host name": lambda d: plant_json(d, month, "https://box.tail1a2b3c.ts.net:8765"),
        "D finance.db path or dump": lambda d: plant_json(d, month, "copy of data/finance.db"),
        "D Plaid token": lambda d: plant_json(d, month, "access-" + "production-" + "12345678-aaaa-bbbb"),
        "D Discord-sized id": lambda d: plant_json(d, month, "channel 123456789012345678"),
        "D SQLite file": lambda d: (d / "demo" / "data" / "dump.json").write_bytes(b"SQLite format 3\x00" + b"\0" * 64),
        "E generator merchant": lambda d: plant_json(d, month, f"paid {merchant}"),
        "F generator fixed amount": lambda d: plant_json(d, month, f"paid ${whole:,}.{cents:02d}"),
        "static file differs": lambda d: (d / "static" / "app.css").write_text((d / "static" / "app.css").read_text() + "\n/* x */\n"),
    }
    expect_text = {"D SQLite file": "D finance.db path or dump",
                   # Rule E is private-scoped (the fixtures are synthetic), so the private rule
                   # is what carries the hit for a private value the generator emits. The assert below still
                   # requires exit 1 and a HIT -- only the rule label differs.
                   "E generator merchant": "A private token"}
    priv_args = [f"--private={p}" for p in args.private] or ["--no-private"]
    tmp = Path(tempfile.mkdtemp(prefix="hf-demo-canary-selftest-"))
    failures = 0
    # Rule E has candidates only for values the private rules call private (the fixtures and
    # their generators are synthetic, so the fixtures' own names are expected to appear in the demo). To exercise E,
    # this one plant needs a private list that CONTAINS a value the generator itself emits -- so E still has to
    # catch it. Without this, E would be untestable rather than absent.
    e_tokens = tmp / "e-private.txt"
    e_tokens.write_text("\n".join([*(str(t) for t in private), merchant]) + "\n")
    e_args = [f"--private={e_tokens}"]
    try:
        def run(copy: Path, priv: list[str] | None = None) -> tuple[int, str]:
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo_canary.py"), str(copy),
                                *(priv if priv is not None else priv_args)],
                               capture_output=True, text=True, env=env, timeout=600)
            return r.returncode, r.stdout + r.stderr
        clean = tmp / "clean"
        shutil.copytree(dist, clean)
        code, out = run(clean)
        ok = code == (0 if args.private else 3)
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} untouched copy: exit {code} ({out.strip().splitlines()[-1]})")
        for rule, plant in plants.items():
            if rule.startswith("A") and not private:
                print("skip A private token: no --private list given")
                continue
            copy = tmp / f"plant-{len(list(tmp.iterdir()))}"
            shutil.copytree(dist, copy)
            plant(copy)
            code, out = run(copy, e_args if rule.startswith("E") else None)
            want = expect_text.get(rule, rule)
            hit = [line for line in out.splitlines() if line.startswith("HIT") and want in line]
            ok = code == 1 and bool(hit)
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} planted {rule}: exit {code}, {len(hit)} hit(s)"
                  + (f": {hit[0][:150]}" if hit else f"\n{out[-600:]}"))
            shutil.rmtree(copy)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    after = tree_hash(dist)
    same = after == before
    failures += not same
    print(f"{'ok  ' if same else 'FAIL'} {args.dist} unchanged (tree sha256 {before[:16]} before, {after[:16]} after)")
    print(f"demo_canary_selftest: {'PASS' if not failures else 'FAIL'} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
