"""polish P3a, static checks of the motion token set (no browser): the tokens exist, the animated files use them instead of
ad-hoc durations, only opacity/transform animate, and prefers-reduced-motion keeps fades only."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "hermes_finance" / "static"
CSS = {p.name: p.read_text() for p in STATIC.glob("*.css")}
APP = CSS["app.css"]


def token(name: str) -> str:
    m = re.search(rf"--{name}:\s*([^;]+);", APP)
    assert m, name
    return m.group(1).strip()


def test_tokens():
    assert token("mo-sheet") == "220ms"
    assert token("mo-menu") == "120ms"
    assert token("mo-page") == "150ms"
    assert token("mo-hover") == "150ms"
    assert token("mo-bars") == "250ms"
    assert token("mo-exit") == "120ms"
    assert "ease-in" in token("mo-out")


def test_drawers_use_the_tokens_not_ad_hoc_durations():
    for name in ("txfilter.css", "txmeta.css"):
        css = CSS[name]
        assert not re.search(r"(?:transition|animation)[^;{}]*\b\d*\.?\d+m?s\b", css), name
    tf = CSS["txfilter.css"]
    assert "var(--mo-sheet)" in tf and "var(--mo-exit)" in tf
    assert "var(--mo-sheet)" in CSS["txmeta.css"]


def test_side_nav_and_sheets_use_the_tokens():
    assert re.search(r"aside#side \{[^}]*transition: transform var\(--mo-exit\)", APP)
    assert re.search(r"body\.drawer aside#side \{[^}]*var\(--mo-sheet\)", APP)
    assert ".modal.on > .sheet { animation: mo-slide-y" in APP        # the bottom sheet on a phone
    assert re.search(r"\.help-panel:popover-open \{ animation: mo-pop", APP)


def test_only_opacity_and_transform_animate():
    for kf in re.findall(r"@keyframes (mo-[\w-]+) \{(.*)\}", APP):
        props = set(re.findall(r"([\w-]+):", kf[1])) - {"from", "to"}
        assert props <= {"opacity", "transform"}, kf[0]
    for name, css in CSS.items():
        for m in re.finditer(r"transition:\s*([^;}]+)", css):
            for part in m.group(1).split(","):
                prop = part.split()[0]
                assert prop in {"transform", "opacity", "color", "background-color", "border-color", "visibility", "none"}, (name, m.group(0))


def test_bars_fill_once():
    assert re.search(r"body:not\(\.bars-done\) :is\([^)]*\.meter i[^)]*\) \{[^}]*mo-bar var\(--mo-bars\)", APP)
    assert "bars-done" in (STATIC / "app.js").read_text()


def test_reduced_motion_keeps_fades_only():
    block = APP[APP.rindex("@media (prefers-reduced-motion: reduce)"):]
    assert "--mo-dx: 0px" in block and "--mo-dy: 0px" in block and "--mo-scale: 1" in block
    assert "transition: none !important" not in APP and "animation: none !important" not in APP
    assert "animation: none" in block                                  # the bars don't fill
    js = (STATIC / "app.js").read_text()
    assert "prefers-reduced-motion: reduce" in js and "reducedMotion() ||" in js   # no count-up
