#!/usr/bin/env python3
"""One-off: pull ONE real, freely licensed photo for each demo car and landmark and freeze it in demo/photos/.

    uv run python scripts/demo_photos_pull.py [--only NAME] [--skip "File:..."] [--dry-run]

The demo build never calls anything: it reads demo/photos/ and the "photo" entry this script writes into each car /
landmark of scripts/demo_assets.json (file, credit, licence, source page). This script is the only thing that ever
asked, once, by hand.

- Cars: Wikimedia Commons, chosen and cut out exactly as the app does it (photos.search_url / info_url / candidate,
  then photos.cut: rembg + clean(), 1000 px wide). A photo that doesn't cut out cleanly is skipped for the next candidate.
- Landmarks: Wikimedia Commons, a photo whose title names the landmark (every word of its name), JPEG / PNG at least
  photos.MIN_WIDTH wide, a free licence (photos.LICENSE_RE, never NC / ND), landscape, no interior / night / aerial
  shot first. Saved as a 1400 px wide JPEG.
- Only Commons is called (no key, no account); >= photos.API_PAUSE seconds between calls. An item that already has a
  photo is never asked again (delete its "photo" entry to pull it again; --skip names a file not to take).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ASSETS = ROOT / "scripts" / "demo_assets.json"
PHOTOS = ROOT / "demo" / "photos"
WIDTH = 1400
CAR_WIDTH = 1000
PLACE_LAST = ("interior", "inside", "night", "aerial", "detail", "sign", "plaque", "ceiling", "lobby", "stage", "concert",
              "construction", "map", "drawing", "postcard", "1920", "1930", "1940", "1950", "1960")

_last = [0.0]


def get(url: str, ua: str) -> bytes:
    from hermes_finance import photos
    wait = photos.API_PAUSE - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(photos.MAX_IMAGE_BYTES)


def slug(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.casefold()))


def place_candidates(name: str, ua: str, skip: set[str]) -> list[dict]:
    from hermes_finance import photos
    words = photos._words(name)
    hits = json.loads(get(photos.search_url(name), ua)).get("query", {}).get("search") or []
    titles = [h["title"] for h in hits if all(w in photos._words(h["title"]) for w in words) and h["title"] not in skip]
    titles.sort(key=lambda t: any(b in t.casefold() for b in PLACE_LAST))
    if not titles:
        return []
    pages = json.loads(get(photos.info_url(titles[:photos.MAX_INFO]), ua)).get("query", {}).get("pages") or []
    out = []
    for p in pages:
        info = (p.get("imageinfo") or [None])[0]
        if not isinstance(info, dict) or info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        w, h = info.get("width") or 0, info.get("height") or 0
        meta = info.get("extmetadata") or {}
        lic = photos._text((meta.get("LicenseShortName") or {}).get("value"))
        thumb = info.get("thumburl") or info.get("url")
        if w < photos.MIN_WIDTH or not h * 1.2 <= w <= h * 2.2 or not photos.LICENSE_RE.match(lic) or re.search(r"\b(nc|nd)\b", lic, re.I) \
                or not str(thumb).startswith(photos.COMMONS_IMAGE_HOSTS):
            continue
        out.append({"title": p["title"], "thumb": thumb, "width": w, "height": h, "license": lic,
                    "artist": photos._text((meta.get("Artist") or {}).get("value")) or "unknown",
                    "page_url": info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{p['title'].replace(' ', '_')}",
                    "rank": (any(b in p["title"].casefold() for b in PLACE_LAST), -w)})
    return sorted(out, key=lambda c: c["rank"])


def car_candidates(car: dict, ua: str, skip: set[str]) -> list[dict]:
    from hermes_finance import photos
    make, model = photos._words(car["make"]), photos.model_words(car["model"])
    for q in (f"{car['year']} {car['make']} {car['model']}", f"{car['make']} {car['model']}"):
        hits = json.loads(get(photos.search_url(q), ua)).get("query", {}).get("search") or []
        titles = sorted((h["title"] for h in hits if photos.title_ok(h["title"], make, model) and h["title"] not in skip),
                        key=lambda t: photos._title_rank(t, car["year"]))
        if not titles:
            continue
        pages = json.loads(get(photos.info_url(titles[:photos.MAX_INFO]), ua)).get("query", {}).get("pages") or []
        found = [c for c in (photos.candidate(p, car["year"], make, model) for p in pages) if c]
        if found:
            return sorted(found, key=lambda c: c["rank"])
    return []


def jpeg(data: bytes) -> tuple[bytes, int, int]:
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.width > WIDTH:
        img = img.resize((WIDTH, round(img.height * WIDTH / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
    return buf.getvalue(), img.width, img.height


def main(argv: list[str] | None = None) -> int:
    from hermes_finance import photos
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="pull just this car / landmark (its name)")
    ap.add_argument("--skip", action="append", default=[], help="a Commons file title not to take")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    ua = "hermes-finance demo build (one-off photo pull; https://github.com/arspesk/hermes-finance)"
    data = json.loads(ASSETS.read_text())
    PHOTOS.mkdir(parents=True, exist_ok=True)
    skip = set(args.skip)
    for kind, items in (("vehicle", data["vehicles"]), ("property", data["properties"])):
        for a in items:
            name = a.get("name") or f"{a['year']} {a['make']} {a['model']}"
            if (args.only and args.only != name) or a.get("photo"):
                continue
            cands = car_candidates(a, ua, skip) if kind == "vehicle" else place_candidates(name, ua, skip)
            print(f"{name}: {len(cands)} candidates: " + "; ".join(c["title"] for c in cands[:5]))
            if args.dry_run:
                continue
            for c in cands[:photos.MAX_CANDIDATES]:
                raw = get(c["thumb"], ua)
                if kind == "vehicle":
                    cut = photos.cut(raw)
                    if not (cut.ok and cut.png):
                        print(f"  {c['title']}: {cut.reason}")
                        continue
                    from PIL import Image
                    img = Image.open(io.BytesIO(cut.png))
                    if img.width > CAR_WIDTH:   # a phone-sized demo: ~1/3 of clean()'s 1400 px bytes
                        img = img.resize((CAR_WIDTH, round(img.height * CAR_WIDTH / img.width)), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, "PNG", optimize=True)
                    (w, h), body, ext, cutout = img.size, buf.getvalue(), "png", True
                else:
                    body, w, h = jpeg(raw)
                    ext, cutout = "jpg", False
                file = f"{slug(name)}.{ext}"
                (PHOTOS / file).write_bytes(body)
                a["photo"] = {"file": file, "cutout": cutout, "width": w, "height": h, "title": c["title"],
                              "artist": c["artist"], "license": c["license"], "credit_url": c["page_url"],
                              "credit": f"Photo: {c['artist']}, {c['license']} · Wikimedia Commons"}
                ASSETS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                print(f"  -> {file} ({len(body) // 1024} KB, {w}x{h}) from {c['title']}")
                break
            else:
                print(f"  {name}: nothing usable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
