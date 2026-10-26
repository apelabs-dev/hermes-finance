#!/usr/bin/env python3
"""One publishable demo from the weekly variants: seven builds (build_demo.py --seed K, one per weekday) in one dist/.

    demo_combine.py OUT 0=DIST0 1=DIST1 ... 6=DIST6

Everything the variants share (the app's static files, the demo's scripts, the logos and pictures) must be the same
bytes in every build and is written once. Each variant's data goes to demo/data/v<K>/ (its export.csv too), with
demo/data/variants.json listing them, and index.html gets data-variants (each variant's pinned clock), from which
demo/shim.js picks the visitor's weekday's variant (Monday 0). Nothing else changes: a variant answers exactly as its
own build does (tests/test_demo_variants.py checks it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEEKDAYS = range(7)
OWN = {"index.html", "api/export.csv"}   # per variant: rewritten below
SHIM_TAG = re.compile(r'(<script src="/demo/shim\.js\?v=)([0-9a-f]+)(" data-build=")([0-9a-f]+)(" data-now=")([^"]+)(">)')


def files(dist: Path) -> dict[str, Path]:
    return {p.relative_to(dist).as_posix(): p for p in sorted(dist.rglob("*")) if p.is_file()}


def combine(out: Path, variants: dict[int, Path]) -> dict:
    if sorted(variants) != list(WEEKDAYS):
        raise SystemExit(f"demo_combine: need one build per weekday 0-6, got {sorted(variants)}")
    metas, shared, html = {}, {}, {}
    for k, dist in variants.items():
        if not (dist / "index.html").is_file() or not (dist / "demo/data/core.json").is_file():
            raise SystemExit(f"demo_combine: {dist} is not a single demo build")
        metas[k] = json.loads((dist / "demo/data/core.json").read_text())["meta"]
        for rel, p in files(dist).items():
            if rel.startswith("demo/data/") or rel in OWN:
                continue
            if rel in shared and shared[rel][1] != p.read_bytes():
                raise SystemExit(f"demo_combine: {rel} differs between the builds ({shared[rel][0]} and {dist})")
            shared.setdefault(rel, (dist, p.read_bytes()))
        page = (dist / "index.html").read_text()
        m = SHIM_TAG.search(page)
        if not m or m.group(2) != metas[k]["build"] or m.group(4) != metas[k]["build"]:
            raise SystemExit(f"demo_combine: {dist}/index.html has no shim tag for its build")
        html[k] = page.replace(metas[k]["build"], "@@BUILD@@").replace(m.group(6), "@@NOW@@")
    if len(set(html.values())) != 1:
        raise SystemExit("demo_combine: the builds' index.html differ beyond their build id and clock")
    build = hashlib.sha256(" ".join(metas[k]["build"] for k in WEEKDAYS).encode()).hexdigest()[:12]
    nows = ",".join(metas[k]["now"] for k in WEEKDAYS)
    page = html[0].replace("@@BUILD@@", build).replace('data-now="@@NOW@@"', f'data-now="{metas[0]["now"]}" data-variants="{nows}"')

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for rel, (_, data) in shared.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)
    (out / "index.html").write_text(page)
    shutil.copy2(variants[0] / "api/export.csv", out / "api/export.csv")   # (the shim points the link at its variant's)
    listing = []
    for k in WEEKDAYS:
        shutil.copytree(variants[k] / "demo/data", out / f"demo/data/v{k}")
        shutil.copy2(variants[k] / "api/export.csv", out / f"demo/data/v{k}/export.csv")
        listing.append({"variant": k, "seed": metas[k].get("seed", 0), "today": metas[k]["today"], "now": metas[k]["now"],
                        "build": metas[k]["build"]})
    (out / "demo/data/variants.json").write_text(json.dumps({"build": build, "variants": listing}, indent=1))
    return {"build": build, "files": len(files(out)), "bytes": sum(p.stat().st_size for p in files(out).values()),
            "variants": listing}


def data_dir(dist: Path, variant: int | None = None) -> Path:
    """Where a dist's data is: demo/data/ for one build, demo/data/v<variant>/ (default 0) for a combined one."""
    if (dist / "demo/data/variants.json").is_file():
        return dist / "demo/data" / f"v{variant or 0}"
    return dist / "demo/data"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("variants", nargs="+", metavar="K=DIST")
    args = ap.parse_args(argv)
    variants = {}
    for v in args.variants:
        k, _, d = v.partition("=")
        if not k.isdigit() or not d:
            ap.error(f"expected K=DIST, got {v}")
        variants[int(k)] = Path(d).resolve()
    out = args.out.resolve()
    if out == ROOT or out in ROOT.parents or ROOT / "src" in [out, *out.parents] or out in variants.values():
        ap.error(f"refusing to write the combined demo into {out}")
    log = combine(out, variants)
    for v in log["variants"]:
        print(f"demo_combine: v{v['variant']} seed {v['seed']} today {v['today']} build {v['build']}")
    print(f"demo_combine: 7 variants, {log['files']} files, {log['bytes']:,} bytes -> {out} (build {log['build']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
