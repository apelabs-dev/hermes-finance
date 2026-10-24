"""TASK-24 fix (review P2): clearing a rule's pattern in Settings -> Categorization rules -> Edit is caught in the sheet.
Against the real server on tests/rules_scene.py's synthetic DB, with real input only: pointer taps through the browser's
input pipeline (Input.dispatchMouseEvent at the control's center, after checking nothing covers it) and real key presses,
never el.click(). At 390 and 1440, dark and light, with the category left alone and changed too: Save keeps the sheet
open, says what to do next, marks and focuses the field, sends no POST and changes nothing in the DB; each screenshot's
own pixels show its theme. A valid pattern then saves; a server refusal is shown in the sheet too.

Named in scripts/task24_fix_failfirst.py: fails on the pre-fix settings.js (6b89dd5), which closed the sheet."""

from __future__ import annotations

import json
import os
import struct
import time
import zlib
from datetime import date
from pathlib import Path

import pytest

import rules_scene
from agent_tools import table_hashes
from cdp import Chrome, Server, find_chrome
from hermes_finance import db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date(2026, 9, 28)
SHEET = "#modal.on #sheet.rsheet"
EMPTY_HINT = "Enter what the rule matches"


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


@pytest.fixture
def served(tmp_path):
    path = tmp_path / "finance.db"
    conn = db.connect(path)
    rules_scene.build(conn)
    conn.close()
    srv = Server(path, today=TODAY)
    try:
        yield srv, path
    finally:
        srv.close()


@pytest.fixture
def shots(tmp_path) -> Path:
    """Where the screenshots go: $TASK24_SHOTS (outside the repo) when set, else the test's temp dir."""
    d = Path(os.environ.get("TASK24_SHOTS") or tmp_path / "shots")
    d.mkdir(parents=True, exist_ok=True)
    return d


def until(b, js_cond: str, what: str, ms: int = 6000) -> None:
    b.js(f"""new Promise((res, rej) => {{ const t0 = Date.now(); const tick = () => {{ if ({js_cond}) res(true);
             else if (Date.now() - t0 > {ms}) rej(new Error({json.dumps('never: ' + what)})); else setTimeout(tick, 50); }};
             tick(); }})""")


def tap(b, sel: str, text: str | None = None) -> None:
    """A real pointer tap at the element's center, once elementFromPoint says that's what a finger there would hit."""
    b.settled()   # P3a motion: a sheet or menu still sliding in would move the target under the finger
    box = b.js(f"""(() => {{ const want = {json.dumps(text)};
      const el = [...document.querySelectorAll({json.dumps(sel)})].find((e) => e.getClientRects().length
        && (want === null || e.textContent.includes(want) || (e.getAttribute('aria-label') || '').includes(want)));
      if (!el) return null;
      el.scrollIntoView({{block: 'center', behavior: 'instant'}});
      const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      return [x, y, !!hit && (hit === el || el.contains(hit)), hit ? hit.outerHTML.slice(0, 80) : null]; }})()""")
    assert box, f"nothing to tap: {sel} {text}"
    assert box[2], f"{sel} {text}: covered at its center by {box[3]}"
    b.call("Input.dispatchMouseEvent", type="mouseMoved", x=box[0], y=box[1], button="none")
    for kind in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=kind, x=box[0], y=box[1], button="left", clickCount=1)


def press(b, key: str, code: str, vk: int, times: int = 1) -> None:
    for _ in range(times):
        for kind in ("keyDown", "keyUp"):
            b.call("Input.dispatchKeyEvent", type=kind, key=key, code=code, windowsVirtualKeyCode=vk)


def png_luminance(data: bytes, x: int, y: int, size: int = 4) -> float:
    """Mean relative luminance (0 black .. 1 white) of a size x size block at device pixel (x, y) of an 8-bit RGB / RGBA
    non-interlaced PNG (what Page.captureScreenshot writes). Only the rows down to the block are unfiltered."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    assert depth == 8 and ctype in (2, 6) and interlace == 0 and 0 <= x < w - size and 0 <= y < h - size
    bpp = 3 if ctype == 2 else 4
    raw, stride = zlib.decompress(idat), w * bpp
    prev, picked = bytearray(stride), []
    for row in range(y + size):
        f, line = raw[row * (stride + 1)], bytearray(raw[row * (stride + 1) + 1:(row + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            up = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + up) & 255
            elif f == 3:
                line[i] = (line[i] + (a + up) // 2) & 255
            elif f == 4:
                p = a + up - c
                pa, pb, pc = abs(p - a), abs(p - up), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else up if pb <= pc else c)) & 255
        if row >= y:
            picked += [line[(x + k) * bpp:(x + k) * bpp + 3] for k in range(size)]
        prev = line
    lin = lambda v: (v / 255) / 12.92 if v <= 10 else ((v / 255 + 0.055) / 1.055) ** 2.4  # noqa: E731
    return sum(0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2]) for p in picked) / len(picked)


def open_edit(b, url: str, width: int, theme: str, rule_id: int) -> None:
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{url}/?t={time.time():.3f}#/settings")
    b.js(f"window.HFTheme.set({json.dumps(theme)})")
    b.settle()
    until(b, "document.querySelector('#page .sc.rules .rsum')", "the rules card")
    tap(b, "#page .sc.rules .rsum")
    until(b, "document.querySelector('#page .sc.rules .rrow')", "the rule rows")
    row = f"#page .sc.rules .rrow[data-rule='{rule_id}']"
    if width >= 1024:
        tap(b, f"{row} .rib", "Edit")
    else:
        tap(b, f"{row} .rmore")
        until(b, f"document.querySelector(\"{row} .rmenu\")", "the row menu")
        tap(b, f"{row} .rmenu button", "Edit")
    until(b, f"document.querySelector('{SHEET} input')", "the edit sheet")


def clear_pattern(b) -> None:
    tap(b, f"{SHEET} input")
    press(b, "End", "End", 35)
    press(b, "Backspace", "Backspace", 8, times=len("Netflix") + 3)
    assert b.js(f"document.querySelector('{SHEET} input').value") == ""


def rule_row(path, rule_id: int) -> tuple:
    conn = db.connect(path)
    try:
        return tuple(conn.execute("SELECT r.pattern, c.name, r.created_from FROM rules r JOIN categories c "
                                  "ON c.id = r.category_id WHERE r.id = ?", (rule_id,)).fetchone())
    finally:
        conn.close()


def netflix_id(path) -> int:
    conn = db.connect(path)
    try:
        return conn.execute("SELECT id FROM rules WHERE pattern = 'Netflix'").fetchone()[0]
    finally:
        conn.close()


def posts(b, path_part: str) -> list[str]:
    return [u for m, u in b.requests() if m == "POST" and path_part in u]


@pytest.mark.parametrize("category_too", [False, True], ids=["pattern-only", "category-too"])
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [390, 1440])
def test_an_empty_pattern_keeps_the_sheet_and_changes_nothing(chrome, served, shots, width, theme, category_too):
    srv, path = served
    b = chrome
    rid = netflix_id(path)
    try:
        open_edit(b, srv.url, width, theme, rid)
        clear_pattern(b)
        if category_too:  # the category too (a native <select>: its value set in the page; the Save below is a real tap)
            b.js(f"(() => {{ const s = document.querySelector('{SHEET} select'); s.value = 'Travel'; "
                 "s.dispatchEvent(new Event('change')); return 1; })()")
        b.requests()                                                            # drop everything sent so far
        before = table_hashes(path)
        tap(b, f"{SHEET} .row .btn", "Save")
        time.sleep(0.6)                                                         # time for a POST, a close, a toast
        state = b.js(f"""(() => {{ const s = document.querySelector('{SHEET}'), i = s && s.querySelector('input');
          const e = s && s.querySelector('.ferr');
          const hint = s && s.querySelector('.hint');
          return {{open: !!s, err: e ? e.textContent : null, errShown: !!e && e.getClientRects().length > 0,
                   errColor: e ? getComputedStyle(e).color : null, hintColor: hint ? getComputedStyle(hint).color : null,
                   invalid: !!i && i.getAttribute('aria-invalid') === 'true', focused: !!i && document.activeElement === i,
                   theme: document.documentElement.dataset.theme,
                   rect: s ? (() => {{ const r = s.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }})() : null}}; }})()""")
        assert state["open"], "the edit sheet closed without validation"
        assert EMPTY_HINT in (state["err"] or "") and state["errShown"], f"no actionable message in the sheet: {state['err']!r}"
        assert state["errColor"] != state["hintColor"], f"the message is painted like the hint ({state['errColor']})"
        assert state["invalid"], "the pattern field is not marked aria-invalid=true"
        assert state["focused"], "the pattern field does not have the focus"
        assert posts(b, "/api/rules/update") == [], "an empty pattern was sent"
        assert table_hashes(path) == before and rule_row(path, rid) == ("Netflix", "Entertainment", "user")
        # the screenshot is of this theme: its own pixels, inside the sheet
        png = shots / f"edit-empty-{'cat' if category_too else 'pat'}-{'phone' if width < 1024 else 'desktop'}-{width}-{theme}.png"
        b.screenshot(png)
        assert state["theme"] == theme
        scale = 2 if width < 760 else 1
        lum = png_luminance(png.read_bytes(), int((state["rect"][0] + 10) * scale), int((state["rect"][1] + 10) * scale))
        assert (lum < 0.2) if theme == "dark" else (lum > 0.8), f"{png.name}: sheet luminance {lum:.2f} is not {theme}"
        # then a valid pattern saves (and closes the sheet), from real typing
        tap(b, f"{SHEET} input")
        b.call("Input.insertText", text="Netflix Synthetic")
        tap(b, f"{SHEET} .row .btn", "Save")
        until(b, "!document.querySelector('#modal.on')", "the sheet to close after a valid save")
        until(b, "document.querySelector('#toast.on')", "the saved toast")
        assert rule_row(path, rid) == ("Netflix Synthetic", "Travel" if category_too else "Entertainment", "user")
        assert b.problems() == []
    finally:
        b.js("localStorage.removeItem('hf.theme')")


def test_a_refused_save_is_shown_in_the_sheet(chrome, served, shots):
    """The server says no (another rule has that pattern): the sheet stays, says why, and nothing changes."""
    srv, path = served
    b = chrome
    rid = netflix_id(path)
    try:
        open_edit(b, srv.url, 1440, "dark", rid)
        clear_pattern(b)
        b.call("Input.insertText", text="Blue Bottle")
        b.requests()
        before = table_hashes(path)
        tap(b, f"{SHEET} .row .btn", "Save")
        until(b, f"(document.querySelector('{SHEET} .ferr') || {{}}).textContent", "the refusal in the sheet")
        err = b.js(f"document.querySelector('{SHEET} .ferr').textContent")
        assert "already has that pattern" in err and b.js(f"!!document.querySelector('{SHEET}')"), err
        assert len(posts(b, "/api/rules/update")) == 1
        assert table_hashes(path) == before and rule_row(path, rid) == ("Netflix", "Entertainment", "user")
        b.screenshot(shots / "edit-refused-desktop-1440-dark.png")
        # the browser logs the 400 itself; nothing else may be wrong
        assert [p for p in b.problems() if "status of 400" not in p] == []
    finally:
        b.js("localStorage.removeItem('hf.theme')")
