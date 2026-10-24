"""Baseline K7 (2026-09-30): every Bills calendar chip on Recurring shows its whole amount, at every width the calendar is
drawn at (1200px and up) in both themes. At 1200 (seven 115px days) every chip's amount ran past its chip: "$2,252.00"
was drawn "$2,252." under the next day, a Saturday's "$165.03" was cut by the calendar's edge (the layout gate's
clipping and cliptext, on the days a bill fell on a Saturday); at 1280, a gate width, 12 of 14 chips on Sept 30. The
gate sees the second, not the first: the next day's cell paints over the overflow, and no overflow box clips it. The fix
(billcal.css, .bchip) keeps the name and amount's column at least as wide as the amount, under the check and logo where
it can't be that wide beside them.

The fixture is the gate's `full` DB plus seven monthly bills with four- and five-figure amounts on the 8th to the 14th,
dated from the run's own day, so every weekday column (the calendar's last one too) has a wide amount on any day. Each
amount is read on the page: the box of its own text inside its chip, on top at both ends (nothing painted over it), and
exactly the amount the API gives."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

import gate_dbs
from cdp import READY_NOW, Chrome, Server, find_chrome
from hermes_finance import classify, db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()   # the page's own clock (the calendar opens on this month): the server uses the same day

# (merchant, amount) on the 8th, 9th, ... 14th of each month: one in every weekday column (synthetic names)
WIDE = (("Harbor Tuition", 12480.00), ("Maple Rent Co", 2252.00), ("Northside HOA", 1865.40), ("Pinecrest Loan", 3410.75),
        ("Summit Daycare", 1999.99), ("Valley Storage", 465.10), ("Lakeview Insurance", 1165.03))
# the calendar's widths (under 1200 it's the agenda): the gate's 1440 and 1280, 1200 the narrowest, and between / above
WIDTHS = (1920, 1440, 1366, 1280, 1240, 1200)
# the fix taken out: the chip as it was (one line; the name and amount's column may shrink to nothing)
FIX_OUT = (".bchip { flex-wrap: nowrap !important; gap: 7px !important; } .bchip .bct { flex: 0 1 auto !important; "
           "min-width: 0 !important; } .bchip .bct b { contain: none !important; } .bchip .bct span { white-space: normal !important; }")


def add_wide(path: Path, today: date) -> None:
    """WIDE in the DB at `path`: a charge on its day this month (once the day has come) and in each of the 6 months before,
    from Nova Checking, filed under Rent & Utilities. So this month's calendar has it paid or due on its day."""
    conn = db.connect(path)
    with conn:
        for i, (name, amount) in enumerate(WIDE):
            m = today.replace(day=1)
            for k in range(7):
                day = m.replace(day=8 + i)
                if day <= today:
                    gate_dbs._txn(conn, f"k7_{i}_{k}", "acc_nova_chk", "demo_nova", day.isoformat(), name.upper(), name, amount,
                                  ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT"))
                m = (m - timedelta(days=1)).replace(day=1)
    for name, _ in WIDE:
        classify.add_rule(conn, "merchant_name", name, "Rent & Utilities", created_from="user")
    classify.apply(conn)
    conn.close()


def amount_text(b: dict) -> str:
    """billcal.js amt(): a bill due at its estimate "~$125", else "$2,252" (worked out here, not read from the page)."""
    if b["variable"]:
        return f"~${int(Decimal(str(b['amount'])).quantize(Decimal(1), ROUND_HALF_UP)):,}"
    return f"${b['amount']:,.0f}" if float(b["amount"]).is_integer() else f"${b['amount']:,.2f}"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = gate_dbs.build("full", tmp_path_factory.mktemp("k7") / "finance.db", TODAY)
    add_wide(path, TODAY)
    srv = Server(path, today=TODAY)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


# every chip of the calendar: its day, name, amount; the amount's own text inside the chip's content box and on top at both
# ends (elementFromPoint, the chip scrolled to the middle of the window); the chip inside its day, under the day's number;
# the day's number inside its day and on top; whether the name and amount are beside the logo or under it
CHIPS_JS = """(() => { const txt = (el) => { const r = document.createRange(); r.selectNodeContents(el); return r.getBoundingClientRect(); };
  const top = (el, x, y) => { const e = document.elementFromPoint(x, y); return !!e && (e === el || el.contains(e)); };
  const out = [...document.querySelectorAll('#page .calg .bchip')].map((c) => {
    c.scrollIntoView({block: 'center'});
    const cell = c.closest('.cday'), v = c.querySelector('.bct .v'), dn = cell.querySelector('.dn'), cs = getComputedStyle(c);
    const k = c.getBoundingClientRect(), d = cell.getBoundingClientRect(), t = txt(v), n = txt(dn), my = (t.top + t.bottom) / 2;
    const left = k.left + parseFloat(cs.paddingLeft), right = k.right - parseFloat(cs.paddingRight);
    return {day: Number(dn.textContent.match(/\\d+/)[0]), name: c.querySelector('.bct b').textContent, amount: v.textContent,
      title: c.getAttribute('title'), inChip: t.left >= left - 0.5 && t.right <= right + 0.5, over: +(t.right - right).toFixed(1),
      onTop: top(v, t.left + 1, my) && top(v, t.right - 1, my), inDay: k.left >= d.left - 0.5 && k.right <= d.right + 0.5 && k.top >= n.bottom,
      dayShown: n.left >= d.left - 0.5 && n.right <= d.right + 0.5 && top(dn, (n.left + n.right) / 2, (n.top + n.bottom) / 2),
      under: v.getBoundingClientRect().left < c.querySelector('.av').getBoundingClientRect().right}; });
  scrollTo(0, 0);
  const nav = [...document.querySelectorAll('#page .calnav button')].map((b) => { const r = b.getBoundingClientRect();
    return top(b, (r.left + r.right) / 2, (r.top + r.bottom) / 2); });
  return {chips: out, nav, grid: getComputedStyle(document.querySelector('#page .calg')).display}; })()"""


def open_recurring(b: Chrome, site, width: int, theme: str, inject: str | None = None) -> dict:
    b.size(width, 1500)   # the whole calendar on screen
    audit_layout.load(b, site.url, "recurring")
    b.settled()
    if theme != "dark":   # as the gate does: this device's theme, then the redraw it makes
        b.js(f"window.HFTheme.set('{theme}')")
        b.js(READY_NOW)
        b.settled()
    if inject:
        b.js(f"(() => {{ const s = document.createElement('style'); s.textContent = {json.dumps(inject)}; document.head.append(s); return 1; }})()")
        b.settled(force=True)
    return b.js(CHIPS_JS)


def test_every_chip_shows_its_whole_amount_at_every_width_in_both_themes(site, chrome):
    """Every chip, every calendar width, both themes: its amount exactly the API's, all of it inside the chip and on top;
    the chip inside its day under the day's number, which shows; the month buttons on top. At 1440 a chip with the room
    (up to four figures) is laid out as before, the name and amount beside the logo; at 1200 under it."""
    want = None
    try:
        for width in WIDTHS:
            for theme in ("dark", "light"):
                got = open_recurring(chrome, site, width, theme)
                if want is None:   # the API's bills for the month on show, read once
                    cal = json.loads(chrome.js("fetch('/api/bills-calendar').then((r) => r.text())"))
                    assert cal["month"] == TODAY.strftime("%Y-%m")
                    want = sorted((int(b["date"][8:]), b["merchant"], amount_text(b)) for b in cal["bills"])
                    assert {(8 + i, n) for i, (n, _) in enumerate(WIDE)} <= {(d, n) for d, n, _ in want}, want
                assert got["grid"] != "none" and all(got["nav"]), (width, theme, got["nav"])
                chips = got["chips"]
                assert sorted((c["day"], c["name"], c["amount"]) for c in chips) == want, (width, theme)
                for c in chips:
                    assert c["inChip"] and c["onTop"], (width, theme, c)
                    assert c["inDay"] and c["dayShown"], (width, theme, c)
                    assert c["title"].startswith(f"{c['name']} {c['amount']}"), (width, theme, c)
                by = {c["name"]: c for c in chips}
                if width == 1440:
                    assert not any(by[n]["under"] for n, a in WIDE if a < 10000), (theme, by)  # whole dollars ("$12,480") may now fit beside the logo too
                if width == 1200:
                    assert all(c["under"] for c in chips), theme
                assert chrome.problems() == [], (width, theme)
                assert chrome.js("document.documentElement.scrollWidth <= innerWidth + 1"), (width, theme)
                chrome.js("localStorage.removeItem('hf.theme')")   # the next state starts dark, as the gate's do
    finally:
        chrome.js("localStorage.removeItem('hf.theme')")


def test_the_layout_gate_passes_the_calendar_at_its_widths_in_both_themes(site, tmp_path, monkeypatch):
    """The gate's own checks on Recurring at 1440, 1280 and 1200, both themes, with a wide amount in the calendar's last
    column (the one whose edge cut "$165.03") whatever the day."""
    monkeypatch.setitem(audit_layout.HEIGHT, 1200, 800)   # audit_layout.HEIGHT has the gate's widths only
    lines: list[str] = []
    rep = audit_layout.audit(site.url, tmp_path / "cal", "k7", (1440, 1280, 1200), log=lines.append, states={"recurring"},
                             themes=("dark", "light"), self_test=False, workers=2)
    ran = {(w, key) for w, states in rep["widths"].items() for key, e in states.items() if "checks" in e}
    assert ran == {(str(w), key) for w in (1440, 1280, 1200) for key in ("recurring", "recurring@light")}
    assert rep["fail"] == 0, "\n".join(line for line in lines if not line.startswith("PASS"))


def test_the_amounts_are_cut_with_the_fix_taken_out(site, chrome, tmp_path, monkeypatch):
    """Discriminating: with the chip as it was, the four-figure amounts run past their chips at 1280 and 1200 (painted over
    by the next day), and the gate's clipping cuts the last column's at 1200."""
    for width in (1280, 1200):
        cut = {c["name"] for c in open_recurring(chrome, site, width, "dark", FIX_OUT)["chips"] if not (c["inChip"] and c["onTop"])}
        assert {"Maple Rent Co", "Northside HOA", "Pinecrest Loan", "Summit Daycare", "Lakeview Insurance"} <= cut, (width, cut)
    monkeypatch.setitem(audit_layout.HEIGHT, 1200, 800)
    old = audit_layout.audit(site.url, tmp_path / "old", "old", (1200,), log=lambda line: None, states={"recurring"},
                             inject=FIX_OUT, themes=("dark",), self_test=False, workers=1)
    cut = {v["detail"]["text"] for v in old["widths"]["1200"]["recurring"]["checks"]["cliptext"]["violations"]}
    assert any(t.startswith("$") for t in cut), cut
