"""TASK-10: the app icon is design "B" (two periwinkle pillars and a mint arrow on navy), traced from
assets/brand/source/icon-B-gptimage.png into a clean full-bleed SVG master, with a bolder favicon cut."""

from __future__ import annotations

import math
import re
from pathlib import Path

from pngtools import decode_png, pixel

from hermes_finance import web

ROOT = Path(__file__).resolve().parents[1]
ICONS = Path(web.__file__).parent / "static" / "icons"
BRAND = ROOT / "assets" / "brand"
# sampled from the source art (medians over each shape's interior)
BG, PILLAR, ARROW = "#090b13", "#6a91fc", "#64e4b4"
FILES = ("icon.svg", "favicon.svg", "favicon-16.png", "favicon-32.png", "favicon.ico", "apple-touch-icon.png",
         "icon-192.png", "icon-512.png", "icon-maskable-512.png")


def rgb(hex_: str) -> tuple[int, int, int]:
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def near(px, colour, tol=36) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(px[:3], rgb(colour)))


def count(decoded, colour, tol=36) -> int:
    w, h = decoded[0], decoded[1]
    return sum(near(pixel(decoded, x, y), colour, tol) and (len(pixel(decoded, x, y)) < 4 or pixel(decoded, x, y)[3] > 200)
               for y in range(h) for x in range(w))


def test_master_svg_is_design_b_on_a_full_bleed_square():
    svg = (ICONS / "icon.svg").read_text()
    assert svg.startswith("<svg") and 'viewBox="0 0 512 512"' in svg
    first = re.search(r"<(?:rect|path|polygon|circle)\b[^>]*>", svg).group(0)
    assert first == f'<rect width="512" height="512" fill="{BG}"/>'           # full bleed: no rounded corners
    pillars = re.findall(rf'<rect [^>]*fill="{PILLAR}"[^>]*>', svg)
    assert len(pillars) == 2 and all(" rx=" in p for p in pillars)             # two rounded pillars
    assert ARROW in svg and ("<polygon" in svg or "<path" in svg)              # the arrow
    for banned in ("Gradient", "<text", "href", "<image", "filter", 'rx="112"'):
        assert banned not in svg, banned


def test_favicon_svg_is_a_bolder_cut_of_the_same_mark():
    fav = (ICONS / "favicon.svg").read_text()
    master = (ICONS / "icon.svg").read_text()
    assert fav != master and PILLAR in fav and ARROW in fav and BG in fav
    widths = lambda s: [float(w) for w in re.findall(rf'<rect [^>]*width="([\d.]+)"[^>]*fill="{PILLAR}"', s)]
    assert min(widths(fav)) > max(widths(master))                              # thicker pillars
    for banned in ("Gradient", "<text", "href", "<image", "filter"):
        assert banned not in fav, banned


def test_touch_and_maskable_icons_are_opaque_and_full_bleed():
    for name, size in (("apple-touch-icon.png", 180), ("icon-maskable-512.png", 512)):
        blob = (ICONS / name).read_bytes()
        assert blob[25] == 2, name                                             # RGB: no alpha channel at all
        d = decode_png(blob)
        assert (d[0], d[1]) == (size, size)
        for x, y in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
            assert near(pixel(d, x, y), BG, 3), (name, x, y)                   # the corners are the navy, not black
        assert count(d, PILLAR, 20) > size * size * 0.08 and count(d, ARROW, 24) > size * size * 0.02, name
    d = decode_png((ICONS / "icon-maskable-512.png").read_bytes())
    far = max(math.hypot(x - 255.5, y - 255.5) for y in range(0, 512, 2) for x in range(0, 512, 2)
              if not near(pixel(d, x, y), BG, 10))
    assert far <= 0.4 * 512, far                                               # the whole mark in the 80% safe zone


def test_any_purpose_pngs_have_rounded_transparent_corners():
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512)):
        d = decode_png((ICONS / name).read_bytes())
        assert (d[0], d[1], d[2]) == (size, size, 4)
        assert pixel(d, 0, 0)[3] == 0 and pixel(d, size - 1, size - 1)[3] == 0
        assert near(pixel(d, size // 2, 6), BG, 6) and pixel(d, size // 2, 6)[3] == 255


def test_favicons_still_show_both_pillars_and_the_arrow_at_16_and_32():
    for name, size, need in (("favicon-16.png", 16, (14, 6)), ("favicon-32.png", 32, (60, 25))):
        d = decode_png((ICONS / name).read_bytes())
        assert (d[0], d[1]) == (size, size)
        assert count(d, PILLAR) >= need[0] and count(d, ARROW) >= need[1], name
    ico = (ICONS / "favicon.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00" and int.from_bytes(ico[4:6], "little") == 2


def test_brand_folder_has_the_same_set_and_a_contact_sheet():
    for name in FILES:
        assert (BRAND / name).read_bytes() == (ICONS / name).read_bytes(), name
    sheet = (BRAND / "contact-sheet.png").read_bytes()
    assert sheet[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(sheet[16:20], "big") >= 1200
