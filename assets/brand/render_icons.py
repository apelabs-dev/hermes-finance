"""Hermes Finances app icon, design "B": two periwinkle pillars (an "H") and a mint arrow climbing through them.

Traced from assets/brand/source/icon-B-gptimage.png (1254 x 1254, generated art with its own rounded corners and
a black surround baked in). The trace below is in that image's pixel coordinates, measured edge by edge from the
art (pillar bounds and corner radius; each straight edge of the arrow fitted through two points on it, corners
where they meet, rounded where the art is round), then scaled into a 512 box. The colours are the medians of each
shape's interior in the art: flat fills, no gradients. The master is full bleed (iOS and Android round the
corners themselves); the favicon is a bolder cut that still reads at 16 px.

    uv run --with pillow python assets/brand/render_icons.py

The rendered icons are committed; re-rendering needs the source art, which the public release doesn't include.

Renders with its own headless Chrome (tests/cdp.py: temp profile, random port, never 9222). Pillow is used here
at build time only (the opaque PNGs and the contact sheet); nothing in this folder is imported by the app.
"""
from __future__ import annotations

import base64
import io
import math
import shutil
import struct
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from cdp import Chrome  # noqa: E402

ICONS = ROOT / "src" / "hermes_finance" / "static" / "icons"
BRAND = ROOT / "assets" / "brand"
SOURCE = BRAND / "source" / "icon-B-gptimage.png"
BG, PILLAR, ARROW = "#090b13", "#6a91fc", "#64e4b4"   # sampled: tile, pillars, arrow (medians)
K = 512 / 1254                                           # source pixels -> the 512 box

# --- the trace, in source pixels ---------------------------------------------------------------------------
PILLARS = [(284, 248, 228, 759), (741, 248, 229, 759)]  # x, y, width, height
PILLAR_R = 82                                            # corner radius


def line(p, q):
    return p, q


def meet(a, b):
    """Where the lines a=(p, q) and b=(p, q) cross."""
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d
    return x1 + t * (x2 - x1), y1 + t * (y2 - y1)


# each edge of the arrow's band through two points measured on it
TOP1, TOP2, TOP3 = line((284, 741), (500, 566)), line((540, 561), (620, 611)), line((660, 601), (720, 547))
BOT1, BOT2, BOT3 = line((284, 867), (500, 698)), line((540, 691), (620, 743)), line((660, 733), (840, 576))
CLIP = line((284, 0), (284, 1254))                       # the band starts flush with the left pillar's edge
NOTCH_L, NOTCH_R = (762, 0), (843, 0)                    # where the head's barbs meet the shaft (x only)
BARB_L, TIP, BARB_R = (725, 462), (919, 408), (902, 618)


def arrow_points():
    """(x, y, corner radius) around the arrow, clockwise from the band's top-left."""
    nl = meet(TOP3, line(NOTCH_L, (NOTCH_L[0], 1)))
    nr = meet(BOT3, line(NOTCH_R, (NOTCH_R[0], 1)))
    return [(*meet(TOP1, CLIP), 0), (*meet(TOP1, TOP2), 40), (*meet(TOP2, TOP3), 0), (*nl, 3), (*BARB_L, 11),
            (*TIP, 14), (*BARB_R, 11), (*nr, 3), (*meet(BOT3, BOT2), 42), (*meet(BOT2, BOT1), 0), (*meet(BOT1, CLIP), 0)]


def rounded_path(points, scale=1.0, dx=0.0, dy=0.0) -> str:
    """A closed path through the points; a corner with radius r > 0 becomes a circular arc tangent to both edges."""
    pts = [((x * scale + dx), (y * scale + dy), r * scale) for x, y, r in points]
    n = len(pts)
    out = []
    for i, (x, y, r) in enumerate(pts):
        px, py, _ = pts[i - 1]
        nx, ny, _ = pts[(i + 1) % n]
        if r <= 0:
            out.append(("L", (x, y)))
            continue
        v1 = (px - x, py - y)
        v2 = (nx - x, ny - y)
        l1, l2 = math.hypot(*v1), math.hypot(*v2)
        u1, u2 = (v1[0] / l1, v1[1] / l1), (v2[0] / l2, v2[1] / l2)
        ang = math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1])))
        t = min(r / math.tan(ang / 2), l1 / 2, l2 / 2)
        r = t * math.tan(ang / 2)
        a = (x + u1[0] * t, y + u1[1] * t)
        b = (x + u2[0] * t, y + u2[1] * t)
        sweep = 1 if (u1[0] * u2[1] - u1[1] * u2[0]) < 0 else 0
        out.append(("L", a))
        out.append(("A", (r, sweep, b)))
    d = []
    for k, (op, v) in enumerate(out):
        if op == "L":
            d.append(f"{'M' if k == 0 else 'L'}{v[0]:.1f} {v[1]:.1f}")
        else:
            r, sweep, b = v
            d.append(f"A{r:.1f} {r:.1f} 0 0 {sweep} {b[0]:.1f} {b[1]:.1f}")
    return "".join(d) + "Z"


def mark(scale=K, dx=0.0, dy=0.0) -> str:
    bars = "".join(f'<rect x="{x * scale + dx:.1f}" y="{y * scale + dy:.1f}" width="{w * scale:.1f}" height="{h * scale:.1f}" '
                   f'rx="{PILLAR_R * scale:.1f}" fill="{PILLAR}"/>' for x, y, w, h in PILLARS)
    return bars + f'<path d="{rounded_path(arrow_points(), scale, dx, dy)}" fill="{ARROW}"/>'


def master_svg(fraction: float = 1.0, rounded: float = 0.0) -> str:
    """The mark on the navy, as big as in the art (fraction=1) or shrunk about the centre; rounded: corner radius."""
    s = K * fraction
    off = 256 * (1 - fraction)
    tile = f'<rect width="512" height="512" fill="{BG}"/>' if not rounded else \
        f'<rect width="512" height="512" rx="{rounded}" fill="{BG}"/>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
            f'<title>Hermes Finances</title>{tile}{mark(s, off, off)}</svg>\n')


# --- the favicon cuts, on the 16 px grid (1 px = 32 units): fatter pillars 4 px wide with a 4 px gap, a 3 px
# arrow. favicon.svg (what tabs draw, at 32 device px on a retina screen) and favicon-32 keep the chart zig;
# favicon-16 straightens the shaft, the one detail a 16 px grid can't hold, so the head stays a clear block.
FAV_PILLARS = [(64, 64, 128, 384), (320, 64, 128, 384)]
FAV_ARROW = [(64, 320, 0), (208, 208, 24), (256, 256, 0), (352, 168, 0), (318, 136, 6), (464, 72, 10),
             (440, 232, 6), (406, 200, 0), (262, 344, 28), (208, 296, 0), (64, 416, 0)]
FAV16_ARROW = [(64, 330, 0), (330, 150, 0), (290, 112, 8), (468, 64, 12), (430, 236, 8), (394, 200, 0), (64, 430, 0)]


def favicon_svg(arrow=FAV_ARROW) -> str:
    bars = "".join(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="52" fill="{PILLAR}"/>' for x, y, w, h in FAV_PILLARS)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
            f'<title>Hermes Finances</title><rect width="512" height="512" rx="104" fill="{BG}"/>{bars}'
            f'<path d="{rounded_path(arrow)}" fill="{ARROW}"/></svg>\n')


def png_ico(entries: list[tuple[int, bytes]]) -> bytes:
    """A .ico holding PNG images (Vista+ format)."""
    head = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    dirs, blobs = b"", b""
    for size, data in entries:
        dirs += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    return head + dirs + blobs


def render(b: Chrome, svg: str, size: int) -> Image.Image:
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    html = (f"<!doctype html><html><body style='margin:0;background:transparent'>"
            f"<img src='{uri}' width='{size}' height='{size}' style='display:block'></body></html>")
    b.call("Emulation.setDeviceMetricsOverride", width=size, height=size, deviceScaleFactor=1, mobile=False)
    b.call("Page.navigate", url="data:text/html;base64," + base64.b64encode(html.encode()).decode())
    time.sleep(0.35)
    shot = b.call("Page.captureScreenshot", format="png", clip={"x": 0, "y": 0, "width": size, "height": size, "scale": 1})
    return Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGBA")


def save(img: Image.Image, name: str, opaque: bool) -> bytes:
    buf = io.BytesIO()
    (img.convert("RGB") if opaque else img).save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    (ICONS / name).write_bytes(data)
    print(f"{name}: {img.size[0]}x{img.size[1]} {'RGB' if opaque else 'RGBA'}, {len(data)} bytes")
    return data


def contact_sheet(images: dict[str, Image.Image]) -> None:
    W = 1600
    sheet = Image.new("RGB", (W, 1100), "#e9ecf2")
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
        small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
    except OSError:
        font = small = ImageFont.load_default()
    d.text((30, 20), "Hermes Finances icon, design B: every file at its real size, plus the 16/32 favicons blown up",
           fill="#1b2030", font=font)
    src = Image.open(SOURCE).convert("RGB").resize((256, 256), Image.LANCZOS)
    x, y = 30, 70
    sheet.paste(src, (x, y))
    d.text((x, y + 262), "source art (generated)", fill="#394155", font=small)
    x += 290
    for name in ("icon-512.png", "icon-maskable-512.png"):
        im = images[name].resize((256, 256), Image.LANCZOS)
        sheet.paste(im, (x, y), im)
        d.text((x, y + 262), f"{name} (shown at 256)", fill="#394155", font=small)
        x += 290
    im = images["apple-touch-icon.png"]
    sheet.paste(im, (x, y + 38), im)
    d.text((x, y + 262), "apple-touch-icon.png 180", fill="#394155", font=small)
    x += 220
    im = images["icon-192.png"]
    sheet.paste(im, (x, y + 32), im)
    d.text((x, y + 262), "icon-192.png", fill="#394155", font=small)
    # iOS-style mask preview of the full-bleed touch icon
    y2 = 400
    mask = Image.new("L", (180, 180), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 179, 179), radius=40, fill=255)
    for i, bg in enumerate(("#ffffff", "#1c1c1e")):
        tile = Image.new("RGB", (220, 220), bg)
        tile.paste(images["apple-touch-icon.png"].convert("RGB"), (20, 20), mask)
        sheet.paste(tile, (30 + i * 240, y2))
    d.text((30, y2 + 226), "touch icon under an iOS-style corner mask (light, dark)", fill="#394155", font=small)
    circ = Image.new("L", (512, 512), 0)
    ImageDraw.Draw(circ).ellipse((0, 0, 511, 511), fill=255)
    tile = Image.new("RGB", (220, 220), "#ffffff")
    tile.paste(images["icon-maskable-512.png"].convert("RGB").resize((180, 180), Image.LANCZOS), (20, 20),
               circ.resize((180, 180), Image.LANCZOS))
    sheet.paste(tile, (520, y2))
    d.text((520, y2 + 226), "maskable under a circle mask", fill="#394155", font=small)
    # favicons: real size on light and dark tab strips, and blown up 12x / 8x (nearest neighbour)
    x3 = 800
    for i, strip in enumerate(("#dfe3ea", "#202124")):
        d.rectangle((x3, y2 + i * 60, x3 + 300, y2 + 46 + i * 60), fill=strip)
        for j, (name, s) in enumerate((("favicon-16.png", 16), ("favicon-32.png", 32))):
            im = images[name]
            sheet.paste(im, (x3 + 14 + j * 60, y2 + 23 - s // 2 + i * 60), im)
        d.text((x3 + 130, y2 + 12 + i * 60), "16 and 32 in a tab", fill="#9aa0a6" if i else "#3c4043", font=small)
    y4 = 690
    for j, (name, s, z) in enumerate((("favicon-16.png", 16, 16), ("favicon-32.png", 32, 8))):
        im = images[name].resize((s * z, s * z), Image.NEAREST)
        bg = Image.new("RGB", im.size, "#c9ced8")
        bg.paste(im, (0, 0), im)
        sheet.paste(bg, (30 + j * 300, y4))
        d.text((30 + j * 300, y4 + s * z + 6), f"{name} x{z}", fill="#394155", font=small)
    im = images["favicon.svg@256"]
    sheet.paste(im, (650, y4), im)
    d.text((650, y4 + 262), "favicon.svg (the tab cut, 16 px grid) at 256", fill="#394155", font=small)
    im = images["icon.svg@256"]
    sheet.paste(im, (950, y4), im)
    d.text((950, y4 + 262), "icon.svg (full-bleed master) at 256", fill="#394155", font=small)
    out = BRAND / "contact-sheet.png"
    sheet.save(out, optimize=True)
    print(f"contact-sheet.png: {sheet.size[0]}x{sheet.size[1]}")


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    master = master_svg()
    fav = favicon_svg()
    (ICONS / "icon.svg").write_text(master)
    (ICONS / "favicon.svg").write_text(fav)
    images: dict[str, Image.Image] = {}
    with Chrome() as b:
        b.call("Emulation.setDefaultBackgroundColorOverride", color={"r": 0, "g": 0, "b": 0, "a": 0})
        jobs = [  # file, svg, size, opaque
            ("favicon-16.png", favicon_svg(FAV16_ARROW), 16, False),
            ("favicon-32.png", fav, 32, False),
            ("apple-touch-icon.png", master, 180, True),                  # full bleed; iOS rounds the corners
            ("icon-192.png", master_svg(1.0, 112), 192, False),           # "any": rounded, transparent corners
            ("icon-512.png", master_svg(1.0, 112), 512, False),
            ("icon-maskable-512.png", master_svg(0.8), 512, True),        # the mark inside the 80% safe zone
        ]
        pngs = {}
        for name, svg, size, opaque in jobs:
            img = render(b, svg, size)
            pngs[name] = save(img, name, opaque)
            images[name] = img
        images["favicon.svg@256"] = render(b, fav, 256)
        images["icon.svg@256"] = render(b, master, 256)
    (ICONS / "favicon.ico").write_bytes(png_ico([(16, pngs["favicon-16.png"]), (32, pngs["favicon-32.png"])]))
    print("favicon.ico: 16 + 32")
    for f in sorted(ICONS.iterdir()):
        shutil.copy2(f, BRAND / f.name)
    print("copied", len(list(ICONS.iterdir())), "files to", BRAND.relative_to(ROOT))
    contact_sheet(images)


if __name__ == "__main__":
    main()
