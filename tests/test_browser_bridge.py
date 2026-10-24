"""The Ask panel in Hermes mode (the plugin's completion bridge) in headless Chrome (tests/cdp.py), on the gate's
"features" DB, the app's real backend behind it (cdp.Server: chat.py, bridge.py, mcp_state, tooldefs, the DB). The
bridge's child is tests/bridge_stub.py's scripted stand-in (not Hermes: tests/test_bridge_runtime.py runs the real
runtime); the panel and its cards are the approved Ask UI, unchanged. Every button is pressed with a real pointer tap
and every question typed with real key input (test_browser_task23's helpers), at phone 390 and desktop 1440, dark and
light, the theme asserted in the page and in the screenshot's own pixels.

States: a pending card (nothing applied) and the same card approved (applied once); a failed turn (the model's answer
refused: one line, no card, its held card withdrawn, nothing changed); Hermes without the bridge (one line). Settings in
Hermes mode shows the spend cap row (the bridge's requests are capped) inside its card at 360, 390 and 1440.
Screenshots go to $HF_BRIDGE_SHOTS (outside the repo) when set, else the test's temp dir."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

import pytest

import bridge_stub as bs
import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import bridge, db, prefs, tooldefs
from pngtools import decode_png, pixel
from test_browser_task23 import capped, cap_of, failed_requests, key, tap, text, type_ask, until

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()
WIDTHS = (390, 1440)
THEMES = ("dark", "light")


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


@pytest.fixture
def shots(tmp_path) -> Path:
    d = Path(os.environ.get("HF_BRIDGE_SHOTS") or tmp_path / "shots")
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    return bs.StubHermes(tmp_path / "stub", monkeypatch)


def serve(tmp_path):
    path = tmp_path / "finance.db"
    gate_dbs.build("features", path, TODAY)
    conn = db.connect(path)
    prefs.save(conn, {"ai.mode": "hermes", "ai.monthly_cap": 5.0})
    conn.close()
    srv = Server(path, today=TODAY)
    srv.path = path
    return srv


def go(b, srv, width: int, theme: str, page: str = "overview") -> None:
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{srv.url}/?t={time.time():.3f}#/{page}")
    b.js(f"window.HFTheme.set({json.dumps(theme)})")
    b.settle()
    b.js("sessionStorage.removeItem('hf.chat.v1'); 1")
    assert b.js("document.documentElement.dataset.theme") == theme
    b.problems()


def open_panel(b) -> None:
    tap(b, "#askb")
    b.js(until("#chat:not([hidden]) .chin"))


ACTION_FIELDS = ("id", "tool", "arguments", "summary", "client", "status", "created_at", "decided_at", "result",
                 "origin", "held_by")


def action_rows(path) -> list[tuple]:
    """Every persisted action field, so fixture rows can be proved untouched rather than merely filtered away."""
    c = sqlite3.connect(path)
    try:
        return c.execute(f"SELECT {', '.join(ACTION_FIELDS)} FROM pending_actions ORDER BY id").fetchall()
    finally:
        c.close()


def fixture_actions(path) -> list[tuple]:
    rows = action_rows(path)
    assert any(row[9] == "mcp" and row[5] == "pending" for row in rows), rows
    return rows


def new_actions(path, before: list[tuple]) -> list[tuple]:
    """Assert the full pre-turn snapshot is intact and return only rows created by this turn."""
    rows = action_rows(path)
    old_ids = {row[0] for row in before}
    assert [row for row in rows if row[0] in old_ids] == before
    return [row for row in rows if row[0] not in old_ids]


def assert_new_actions(path, before: list[tuple], expected: list[tuple[int, str]]) -> None:
    rows = new_actions(path, before)
    assert [(row[0], row[9], row[5]) for row in rows] == [
        (action_id, "ask_hermes", status) for action_id, status in expected
    ]


def luminance(png: Path, x: float, y: float, scale: int) -> float:
    decoded = decode_png(png.read_bytes())
    r, g, bl = pixel(decoded, int(x * scale), int(y * scale))[:3]
    return (0.2126 * r + 0.7152 * g + 0.0722 * bl) / 255


def shot(b, png: Path, theme: str, width: int, sel: str = "#chat") -> None:
    """Check a screenshot pixel that is visibly occupied by the requested panel/card body, not an offscreen corner."""
    sample = b.js(f"""(() => {{
      const e = document.querySelector({json.dumps(sel)}); if (!e) return null;
      const r = e.getBoundingClientRect(), vw = innerWidth, vh = innerHeight;
      const l = Math.max(0, r.left), t = Math.max(0, r.top), rr = Math.min(vw, r.right), bb = Math.min(vh, r.bottom);
      if (rr - l < 12 || bb - t < 12) return null;
      const xs = [l + 6, rr - 6];
      for (let y = t + 6; y <= bb - 6; y += 12) {{
        for (const x of xs) {{
          const hit = document.elementFromPoint(x, y);
          if (hit && (hit === e || e.contains(hit))) return {{x, y, hit: hit.tagName}};
        }}
      }}
      return null;
    }})()""")
    assert sample, f"{sel} has no visible sample point"
    b.screenshot(png)
    lum = luminance(png, sample["x"], sample["y"], 2 if width < 760 else 1)
    assert (lum < 0.25) if theme == "dark" else (lum > 0.75), f"{png.name}: luminance {lum:.2f} is not {theme}"


CAP = "#page .aibody input[aria-label='Monthly spend cap']"


def cap_geometry(b, deadline: float | None = None) -> dict | None:
    """The cap input, viewport, and the nearest real vertical scroll container at this instant."""
    return b.js(f"""(() => {{
      const input = document.querySelector({json.dumps(CAP)}); if (!input) return null;
      const row = input.closest('.srow'), body = input.closest('.aibody'), card = body.closest('.sc');
      let scroll = input.parentElement;
      while (scroll && scroll !== document.body && scroll !== document.documentElement) {{
        const s = getComputedStyle(scroll);
        if (/(auto|scroll)/.test(s.overflowY) && scroll.scrollHeight > scroll.clientHeight + 1) break;
        scroll = scroll.parentElement;
      }}
      if (!scroll || scroll === document.body || scroll === document.documentElement) scroll = document.scrollingElement;
      const ir = input.getBoundingClientRect(), rr = row.getBoundingClientRect(), cr = card.getBoundingClientRect();
      const sr = scroll === document.scrollingElement
        ? {{left: 0, top: 0, right: innerWidth, bottom: innerHeight}}
        : scroll.getBoundingClientRect();
      const x = Math.max(1, Math.min(innerWidth - 1, (sr.left + sr.right) / 2));
      const y = Math.max(1, Math.min(innerHeight - 1, (sr.top + sr.bottom) / 2));
      const cx = ir.left + ir.width / 2, cy = ir.top + ir.height / 2, hit = document.elementFromPoint(cx, cy);
      return {{input: {{l: ir.left, t: ir.top, r: ir.right, b: ir.bottom}},
        row: {{l: rr.left, r: rr.right}}, card: {{l: cr.left, r: cr.right}}, center: {{x: cx, y: cy}},
        viewport: {{w: innerWidth, h: innerHeight, visualW: visualViewport && visualViewport.width,
          visualH: visualViewport && visualViewport.height}},
        scroll: {{tag: scroll.tagName, id: scroll.id, cls: scroll.className, top: scroll.scrollTop,
          height: scroll.clientHeight, total: scroll.scrollHeight, x, y}},
        hit: hit && {{tag: hit.tagName, id: hit.id, cls: hit.className}}, inputOnTop: !!hit && hit === input,
        centerVisible: cx >= 0 && cx < innerWidth && cy >= 0 && cy < innerHeight,
        rowInside: rr.left >= cr.left - .5 && rr.right <= cr.right + .5,
        inputInside: ir.left >= cr.left - .5 && ir.right <= cr.right + .5,
        sw: document.documentElement.scrollWidth}};
    }})()""", deadline)


def stable_cap_geometry(b, deadline: float, changed_from: float | None = None, samples: int = 3) -> dict:
    """Return only repeated-identical geometry/hit snapshots, optionally after a wheel has made progress."""
    last = None
    repeats = 0
    while time.monotonic() < deadline:
        got = cap_geometry(b, deadline)
        assert got, "no spend cap row in Hermes mode"
        key = json.dumps(got, sort_keys=True, separators=(",", ":"))
        progressed = changed_from is None or got["scroll"]["top"] != changed_from or got["centerVisible"]
        if progressed and key == last:
            repeats += 1
        elif progressed:
            last, repeats = key, 1
        else:
            last, repeats = None, 0
        if repeats >= samples:
            return got
        time.sleep(min(0.04, max(0, deadline - time.monotonic())))
    raise AssertionError(f"spend cap geometry did not settle before deadline: {got if 'got' in locals() else None}")


def wheel_cap_into_view(b, timeout: float = 5.0, steps: int = 20) -> dict:
    """Bring the input to a genuine hit point using only bounded wheel input over its actual scroll container."""
    deadline = time.monotonic() + timeout
    got = stable_cap_geometry(b, deadline)
    last = got
    for _ in range(steps):
        last = got
        if got["centerVisible"] and got["inputOnTop"]:
            return got
        assert time.monotonic() < deadline, got
        cy, vh = got["center"]["y"], got["viewport"]["h"]
        delta = max(-500, min(500, cy - vh / 2))
        if abs(delta) < 80:
            delta = 80 if cy >= vh / 2 else -80
        before = got["scroll"]["top"]
        b.call("Input.dispatchMouseEvent", type="mouseWheel", x=got["scroll"]["x"], y=got["scroll"]["y"],
               deltaX=0, deltaY=delta, _deadline=deadline)
        got = stable_cap_geometry(b, deadline, changed_from=before)
    last = got
    if got["centerVisible"] and got["inputOnTop"]:
        return got
    raise AssertionError(f"spend cap input not reachable after {steps} wheel steps: {last}")


def pref_cap(path) -> float:
    conn = db.connect(path)
    try:
        return prefs.get(conn, "ai.monthly_cap")
    finally:
        conn.close()


def fits(b, width: int) -> None:
    """Nothing wider than the screen; every card and its buttons inside the panel, on top where they are."""
    got = b.js("""(() => { const p = document.getElementById('chat').getBoundingClientRect();
      const inside = (r) => r.left >= p.left - 0.5 && r.right <= p.right + 0.5;
      const cards = [...document.querySelectorAll('#chat .appr')].map((e) => e.getBoundingClientRect());
      const btns = [...document.querySelectorAll('#chat .appr button')].map((e) => { const r = e.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !!hit && e.contains(hit); });
      return {sw: document.documentElement.scrollWidth, cards: cards.every(inside), btns: btns.every(Boolean)}; })()""")
    assert got["sw"] <= width and got["cards"] and got["btns"], got


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_a_pending_hermes_card_then_approved_once(chrome, tmp_path, hermes, shots, monkeypatch, width, theme):
    srv = serve(tmp_path)
    try:
        before = fixture_actions(srv.path)
        applied = []
        real_apply = tooldefs.apply
        monkeypatch.setattr(tooldefs, "apply", lambda *a, **kw: applied.append(a[0]) or real_apply(*a, **kw))
        b = chrome
        cat, cap = capped(srv.path)
        new = float(int(cap) + 100)
        hermes.script(bs.read("finance_budget_status"), bs.propose("finance_set_cap", {"category": cat, "monthly_cap": new}),
                      bs.answer(f"You're close to your {cat} cap. Raising it to ${new:.0f} would cover a month like this."))
        go(b, srv, width, theme)
        open_panel(b)
        assert text(b, "#chat .brain") == "via Hermes"
        type_ask(b, f"Should I raise my {cat} cap?")
        b.js(until("#chat .appr[data-status=pending]"))
        assert text(b, "#chat .src").startswith("from budget-status")
        assert text(b, "#chat .appr .apt") == f"Raise {cat} cap to ${new:.0f}?"
        assert "Nothing changes until you approve." in text(b, "#chat .appr")
        created = new_actions(srv.path, before)
        assert len(created) == 1
        action_id = created[0][0]
        assert_new_actions(srv.path, before, [(action_id, "pending")])
        assert b.js("Number(document.querySelector('#chat .appr').dataset.card)") == action_id
        assert cap_of(srv.path, cat) == cap and applied == []                         # nothing applied
        assert [r["request"]["protocol"] for r in hermes.records("start")] == [bridge.PROTOCOL] * 3
        fits(b, width)
        shot(b, shots / f"bridge-pending-{width}-{theme}.png", theme, width)
        tap(b, "#chat .appr .btn", "Approve")
        b.js(until("#chat .appr[data-status=applied]"))
        assert text(b, "#chat .appr .apres") == f"Saved: {cat} cap is now ${new:.0f}"
        assert b.js("document.querySelectorAll('#chat .appr button').length") == 0
        assert b.js("Number(document.querySelector('#chat .appr').dataset.card)") == action_id
        assert cap_of(srv.path, cat) == new and applied == ["finance_set_cap"]
        assert_new_actions(srv.path, before, [(action_id, "applied")])
        duplicate = b.js(f"""fetch('/api/chat/decide', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{id: {action_id}, approve: true}})}}).then(async (r) =>
            ({{status: r.status, body: await r.json()}}))""")
        assert duplicate["status"] == 409 and duplicate["body"]["card"]["status"] == "applied", duplicate
        assert cap_of(srv.path, cat) == new and applied == ["finance_set_cap"]
        assert_new_actions(srv.path, before, [(action_id, "applied")])
        fits(b, width)
        shot(b, shots / f"bridge-approved-{width}-{theme}.png", theme, width)
        assert b.js("document.documentElement.dataset.theme") == theme
        assert failed_requests(b, (409, "/api/chat/decide")) == []
    finally:
        srv.close()


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_a_failed_hermes_turn_is_one_line_and_changes_nothing(chrome, tmp_path, hermes, shots, width, theme):
    srv = serve(tmp_path)
    try:
        before = fixture_actions(srv.path)
        b = chrome
        cat, cap = capped(srv.path)
        hermes.script(bs.propose("finance_set_cap", {"category": cat, "monthly_cap": float(int(cap) + 50)}),
                      {"text": "Sure, I've raised it for you!"})          # not a decision: refused
        go(b, srv, width, theme)
        open_panel(b)
        type_ask(b, "Raise it?")
        b.js(until("#chat .msg.bot.err"))
        assert text(b, "#chat .msg.bot.err") == bridge.INVALID
        assert b.js("document.querySelectorAll('#chat .appr').length") == 0
        created = new_actions(srv.path, before)
        assert len(created) == 1
        assert_new_actions(srv.path, before, [(created[0][0], "rejected")])
        assert cap_of(srv.path, cat) == cap                                             # withdrawn, nothing applied
        assert b.js("!document.querySelector('#chat .chin').disabled && !document.getElementById('chsend').disabled")
        assert "Traceback" not in b.js("document.getElementById('chat').textContent")
        fits(b, width)
        shot(b, shots / f"bridge-failed-{width}-{theme}.png", theme, width)
        assert failed_requests(b, (502, "/api/chat")) == []
    finally:
        srv.close()


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_hermes_without_the_bridge_is_one_line(chrome, tmp_path, hermes, shots, width, theme):
    srv = serve(tmp_path)
    try:
        before = fixture_actions(srv.path)
        b = chrome
        hermes.script({"unavailable": True})
        go(b, srv, width, theme)
        open_panel(b)
        type_ask(b, "What bills are due this week?")
        b.js(until("#chat .msg.bot.err"))
        assert text(b, "#chat .msg.bot.err") == bridge.ERRORS["unavailable"][0]
        assert b.js("document.querySelectorAll('#chat .appr').length") == 0
        assert_new_actions(srv.path, before, [])
        fits(b, width)
        shot(b, shots / f"bridge-unavailable-{width}-{theme}.png", theme, width)
        assert failed_requests(b, (503, "/api/chat")) == []
    finally:
        srv.close()


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", (360, 390, 1440))
def test_settings_in_hermes_mode_shows_the_spend_cap(chrome, tmp_path, hermes, shots, width, theme):
    srv = serve(tmp_path)
    try:
        b = chrome
        go(b, srv, width, theme, page="settings")
        b.js(until("#page .aibody .srow"))
        got = wheel_cap_into_view(b)
        label = text(b, "#page .aibody .srow")
        assert label and label.startswith("Monthly spend cap"), label   # P3b debloat: the price note moved into the card's help
        assert b.js(f"document.querySelector({json.dumps(CAP)}).value") == "5.00"
        assert got["rowInside"] and got["inputInside"], got
        assert got["sw"] <= width, got
        assert got["viewport"]["w"] == width and got["viewport"]["h"] == (900 if width > 800 else 844), got
        assert got["centerVisible"] and got["inputOnTop"] and got["hit"]["tag"] == "INPUT", got
        tap(b, CAP)
        assert b.js(f"document.activeElement === document.querySelector({json.dumps(CAP)})")
        select_all = 4 if "Mac" in b.js("navigator.platform") else 2
        for kind in ("keyDown", "keyUp"):
            b.call("Input.dispatchKeyEvent", type=kind, key="a", code="KeyA", windowsVirtualKeyCode=65,
                   nativeVirtualKeyCode=65, modifiers=select_all,
                   **({"commands": ["selectAll"]} if kind == "keyDown" else {}))
        selection = b.js(f"(() => {{ const e = document.querySelector({json.dumps(CAP)}); "
                         "return [e.selectionStart, e.selectionEnd]; })()")
        assert selection == [0, 4], selection
        b.call("Input.insertText", text="6.00")
        assert b.js(f"document.querySelector({json.dumps(CAP)}).value") == "6.00"
        key(b, "Enter")
        assert b.js(f"document.activeElement !== document.querySelector({json.dumps(CAP)})")
        deadline = time.monotonic() + 3
        while pref_cap(srv.path) != 6.0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pref_cap(srv.path) == 6.0
        shot(b, shots / f"bridge-settings-cap-{width}-{theme}.png", theme, width, sel="#page .aibody")
        assert b.js("document.documentElement.dataset.theme") == theme
        assert b.problems() == []
    finally:
        srv.close()
