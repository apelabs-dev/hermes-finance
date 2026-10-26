#!/usr/bin/env python3
"""Make a demo logo from a source picture: 128 x 128 PNG, only IHDR / IDAT / IEND (no text, colour-profile or size
chunks: the canary allows no PNG metadata), RGB when the source is fully opaque, RGBA otherwise.

    uv run python scripts/demo_logo_prep.py SRC.png demo/logos/<slug>.png [--level N]

--level picks zlib's compression level (default 9). The pixels are the same at every level; only the compressed bytes
differ, which is how a logo whose deflate stream happens to spell a private-list token is fixed (demo_canary.py names
the file, never the token). Needs Pillow (the dev environment has it); the demo build itself only copies the result.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

SIZE = 128


def prep(src: bytes, level: int = 9) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(src))
    im.load()
    im = im.convert("RGBA")
    if im.size != (SIZE, SIZE):
        side = min(im.size)   # a square from the middle (the sources are square already)
        left, top = (im.width - side) // 2, (im.height - side) // 2
        im = im.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.LANCZOS)
    if im.getextrema()[3][0] == 255:
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, "PNG", optimize=level == 9, compress_level=level)
    return out.getvalue()


def chunks(png: bytes) -> list[str]:
    out, i = [], 8
    while i < len(png):
        n = int.from_bytes(png[i:i + 4], "big")
        out.append(png[i + 4:i + 8].decode("latin-1"))
        i += 12 + n
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--level", type=int, default=9, choices=range(1, 10))
    a = ap.parse_args(argv)
    png = prep(a.src.read_bytes(), a.level)
    assert chunks(png) == ["IHDR", "IDAT", "IEND"], chunks(png)
    a.dst.write_bytes(png)
    print(f"{a.dst.name}: {SIZE}x{SIZE}, {len(png):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
