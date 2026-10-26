"""t_4cb4d435: the public demo shows invented logos for its fictional banks and merchants, and Alpaca's own.

Pure checks on scripts/build_demo.py's logo helpers and the page's image allowlist (the full build is tested in
tests/test_static_demo.py)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_demo_logos", ROOT / "scripts" / "build_demo.py")
bd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bd)


def test_slug_matches_the_logo_files():
    assert bd.logo_slug("Riverbend Water & Power") == "riverbend-water-power"
    assert bd.logo_slug("SUNNY CRATE GROCERS") == "sunny-crate-grocers"
    assert bd.logo_slug("Alpaca") == "alpaca"


def test_every_logo_is_a_small_png_and_alpaca_is_there():
    files = sorted(bd.DEMO_LOGOS.glob("*.png"))
    assert len(files) >= 40 and (bd.DEMO_LOGOS / "alpaca.png").is_file()
    for p in files:
        b = p.read_bytes()
        assert b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) < 64_000, p.name
        assert re.fullmatch(r"[a-z0-9-]+\.png", p.name), p.name


def test_merchant_rows_get_a_same_origin_logo_and_others_are_left_alone():
    used = set()
    out = bd.with_merchant_logos({"tx": [
        {"merchant_name": "Bean Voyage", "name": "BEAN VOYAGE #12", "logo_url": None},
        {"merchant": "Nobody We Drew", "logo_url": None},
        {"merchant": "Bean Voyage", "logo_url": "https://plaid-merchant-logos.plaid.com/x.png"},
        {"name": "Bean Voyage"},
    ]}, used)["tx"]
    assert out[0]["logo_url"] == "/api/merchant-logos/bean-voyage.png"
    assert out[1]["logo_url"] is None                      # no logo drawn: initials, as before
    assert out[2]["logo_url"].startswith("https://plaid")   # a real logo is never replaced
    assert "logo_url" not in out[3]                         # no field is added where the page doesn't use one
    assert used == {"bean-voyage"}


def test_the_page_takes_only_the_demo_logo_folder_from_its_own_origin():
    js = (ROOT / "src/hermes_finance/static/app.js").read_text()
    m = re.search(r"if \(/(\^\\/api\\/merchant-logos\\/\[a-z0-9-\]\+\\\.png\$)/\.test\(u\)\) return u;", js)
    assert m, "safeImg's demo-logo rule moved"
    rx = re.compile(m.group(1).replace("\\/", "/"))
    assert rx.fullmatch("/api/merchant-logos/bean-voyage.png")
    for bad in ("//evil.example/api/merchant-logos/a.png", "/api/merchant-logos/../x.png", "/api/merchant-logos/a.svg",
                "https://evil.example/api/merchant-logos/a.png", "/api/merchant-logos/a.png?x=1", "/api/images/a.png"):
        assert not rx.fullmatch(bad), bad


def test_alpaca_is_the_real_icon_not_a_redraw():
    """t_d31fda2a: demo/logos/alpaca.png is Alpaca's own icon (sketches/017-launch/logos/inst-alpaca.png,
    180 px) made 128 px by scripts/demo_logo_prep.py, not the earlier redraw."""
    import hashlib
    b = (bd.DEMO_LOGOS / "alpaca.png").read_bytes()
    assert hashlib.sha256(b).hexdigest() == "af1a5c9fad4185c197a7b4284a9e68900beb6a8c4b0298ffb7aca941130e1460"
    spec2 = importlib.util.spec_from_file_location("demo_logo_prep", ROOT / "scripts" / "demo_logo_prep.py")
    prep = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(prep)
    assert prep.chunks(b) == ["IHDR", "IDAT", "IEND"]


def test_every_demo_name_has_its_logo_and_no_logo_is_left_over():
    """t_d31fda2a: every merchant and institution name the demo can show (its pools, the merchant overflow, Alpaca) has a
    logo in demo/logos, and every logo there belongs to one of them (the v1 files are gone). The four overflow banks are
    the one exception: a fifth+ bank of a kind shows its colour and initials, as the app does for a bank without a logo."""
    names = [n for pool in (*bd.INSTITUTIONS.values(), *bd.MERCHANTS.values()) for n in pool]
    names += [*bd.MERCHANT_OVERFLOW, "Alpaca"]
    slugs = {bd.logo_slug(n) for n in names}
    have = {p.stem for p in bd.DEMO_LOGOS.glob("*.png")}
    assert sorted(slugs - have) == [], "names without a logo"
    assert sorted(have - slugs) == [], "logos no demo name uses"
    assert not {bd.logo_slug(n) for n in bd.INSTITUTION_OVERFLOW} & have
