"""Baseline K1 (2026-09-30): a Recurring row keeps its merchant, status, category, cadence and monthly money whole and on
screen, at every width in both themes, when its status is a wide one. On Sept 30 the synthetic Namecheap renewal was
"Due tomorrow" (a 110px pill under the amount), and the Subscriptions card beside Bills at 1280 left the name column
82px: "Namecheap" and "Subscriptions · yearly · $1.08 a month" were cut (the layout gate's cliptext and clipping, on
every DB with that renewal). The fix (app.css, .li.wrap[data-bill]) gives the name column its longest word; where the
amount and the status would leave it less, they go on a line of their own under it.

The fixture is the gate's `full` DB plus three yearly renewals dated from the run's own day, so every run has the wide
statuses, not only Sept 30's: a short one-word name due tomorrow (Dynadot, like Namecheap), a longer one due in 3 days
(Squarespace), and one 24 days late ("Late · expected <24 days ago>", the widest). The layout gate's own checks
(scripts/audit_layout.py, unchanged) judge the page, and fail it with the fix taken out."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import classify, db, insights

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout  # noqa: E402

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()   # the page's own clock (a date's year shows when it isn't this one): the server uses the same day

# (merchant, days until its renewal: < 0 is late, amount)
RENEWALS = (("Dynadot", 1, 10.99), ("Squarespace", 3, 23.00), ("Hostinger", -24, 35.88))
# the gate's widths and the two the gate doesn't have: 1200, the narrowest three-column one (the Subscriptions card is
# ~227px inside there), and 1100, two columns
WIDTHS = (1440, 1280, 1200, 1100, 1024, 768, 430, 390, 360)
EXTRA_HEIGHTS = {1200: 800, 1100: 800}   # audit_layout.HEIGHT has the gate's widths only
# the fix taken out: the name column may shrink to nothing again (so nothing ever wraps under it). (integration) Its
# basis too: TASK-24's 6em (app.css, the same Subscriptions rows) alone wraps the late row under the name at 1280 and
# 1200, so both come out, back to .grow's own `flex: 1` (a 0% basis)
FIX_OUT = ".li.wrap[data-bill] > .grow { min-width: 0 !important; flex-basis: 0% !important; }"


def years_back(d: date, n: int) -> date:
    """The same day n years earlier (Feb 29: Feb 28)."""
    try:
        return d.replace(year=d.year - n)
    except ValueError:
        return d.replace(year=d.year - n, day=28)


def add_renewals(path: Path, today: date) -> dict[str, dict]:
    """RENEWALS in the DB at `path`: two charges a year apart, on the day a year and two years before the renewal, from
    Nova Checking, filed under Subscriptions. Returns the Recurring items by merchant (the API's own)."""
    conn = db.connect(path)
    with conn:
        for name, until, amount in RENEWALS:
            due = today + timedelta(days=until)
            for n in (1, 2):
                gate_dbs._txn(conn, f"k1_{name.lower()}_{n}", "acc_nova_chk", "demo_nova", years_back(due, n).isoformat(),
                              f"{name.upper()}.COM", name, amount, ("GENERAL_SERVICES", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"))
    for name, *_ in RENEWALS:
        classify.add_rule(conn, "merchant_name", name, "Subscriptions", created_from="user")
    classify.apply(conn)
    items = {r["merchant"]: r for r in insights.recurring(conn, today)["items"]}
    conn.close()
    return items


def wide_statuses(today: date) -> dict[str, tuple[str, int]]:
    """What the fixture must give on `today`: (status, days until) for each renewal. A renewal due on a Feb 29 comes
    back a day sooner, on the 28th (the years before have no Feb 29): the eve of one is "Due today", not tomorrow."""
    out = {}
    for name, until, _ in RENEWALS:
        due = today + timedelta(days=until)
        out[name] = ("late" if until < 0 else "due_soon", until - ((due.month, due.day) == (2, 29)))
    return out


def day_text(iso: str, today: date) -> str:
    """app.js fmtDate in en-US: "Sep 28", with the year when it isn't this one ("Sep 28, 2025")."""
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}" + (f", {d.year}" if d.year != today.year else "")


def status_text(r: dict, today: date) -> str:
    """The status pill app.js recPill draws for a Recurring item (worked out here, not read from the page)."""
    k = r["days_until"]
    if r["paid_this_month"] and r["cadence"] != "weekly":
        return f"Paid {day_text(r['last_date'], today)}"
    if r["status"] == "late":
        return f"Late · expected {day_text(r['next_expected'], today)}"
    if r["status"] == "due_soon":
        return (f"Expected {day_text(r['next_expected'], today)}" if k < 0 else "Due today" if k == 0
                else "Due tomorrow" if k == 1 else f"Due in {k} days")
    return f"Next {day_text(r['next_expected'], today)}"


def money2(x: float) -> str:
    # numbers pass (t_7ec2be8e): whole dollars drop their cents, everything else keeps two decimals
    x = round(x, 2)
    return f"${x:,.0f}" if x == int(x) else f"${x:,.2f}"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = gate_dbs.build("full", tmp_path_factory.mktemp("k1") / "finance.db", TODAY)
    items = add_renewals(path, TODAY)
    got = {name: (items[name]["status"], items[name]["days_until"]) for name, *_ in RENEWALS if name in items}
    assert got == wide_statuses(TODAY), got
    assert all(items[name]["kind"] == "subscription" and items[name]["cadence"] == "yearly" for name, *_ in RENEWALS)
    srv = Server(path, today=TODAY)
    srv.items = items
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


# every Recurring row on the page: its four parts, each with its text, whether it's drawn, and whether all of it shows
# (nothing cut by its own box, nothing past the card's sides)
ROWS_JS = """(() => { const T = (el) => (el ? el.textContent.replace(/\\s+/g, ' ').trim() : null);
  return [...document.querySelectorAll('#page .li[data-bill]')].map((row) => {
    const card = row.closest('.card').getBoundingClientRect();
    const part = (sel) => { const el = row.querySelector(sel); if (!el) return null; const cs = getComputedStyle(el), r = el.getBoundingClientRect();
      return {text: T(el), shown: el.getClientRects().length > 0 && cs.display !== 'none' && cs.visibility === 'visible' && +cs.opacity > 0,
        whole: el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1,
        inside: r.left >= card.left - 0.5 && r.right <= card.right + 0.5}; };
    const grow = row.querySelector(':scope > .grow').getBoundingClientRect(), status = row.querySelector('.pill').getBoundingClientRect();
    return {narrow: !!row.querySelector(':scope > .rt:not(.bill)'), name: part('.grow > b'), sub: part('.grow > span'), status: part('.pill'),
      amount: part('.amt'), beside: status.top < grow.bottom && status.bottom > grow.top}; }); })()"""


def open_recurring(b: Chrome, site, width: int, theme: str) -> None:
    b.size(width, {**audit_layout.HEIGHT, **EXTRA_HEIGHTS}[width])
    audit_layout.load(b, site.url, "recurring")
    b.settled()
    if theme != "dark":   # as the gate does: this device's theme, then the redraw it makes
        b.js(f"window.HFTheme.set('{theme}')")
        b.js(audit_layout.READY_NOW)
        b.settled()


def test_the_fixture_has_the_wide_statuses_on_any_day(tmp_path):
    """The renewals come out due tomorrow / in 3 days / 24 days late whatever the day (month and year ends, Feb 29 and
    its eve), so the page tests below see the wide statuses on every run, not on Sept 30 alone."""
    for day in (date(2026, 9, 30), date(2026, 10, 1), date(2026, 12, 31), date(2027, 1, 1), date(2027, 2, 27),
                date(2028, 2, 26), date(2028, 2, 28), date(2028, 2, 29), date(2028, 3, 1), date(2028, 3, 24), date(2026, 1, 31)):
        path = gate_dbs.build("full", tmp_path / f"{day}.db", day)
        items = add_renewals(path, day)
        got = {name: (items[name]["status"], items[name]["days_until"]) for name, *_ in RENEWALS if name in items}
        assert got == wide_statuses(day), (day, got)
        assert len(status_text(items["Hostinger"], day)) > len("Due tomorrow") and status_text(items["Dynadot"], day) in (
            "Due tomorrow", "Due today"), day


def test_every_row_reads_whole_at_every_width_in_both_themes(site, chrome):
    """Every Recurring row, both cards: name, sub-line (category · cadence · a month), status and amount drawn and whole
    (none hidden to make room); the renewals' four read exactly what the API says."""
    want = {}
    for name, *_ in RENEWALS:
        r = site.items[name]
        want[name] = {"sub": f"{r['category']} · {r['cadence_label']} · {money2(r['monthly_amount'])} a month",
                      "status": status_text(r, TODAY), "amount": money2(r["amount"])}
    assert want["Hostinger"]["status"].startswith("Late · expected ") and want["Squarespace"]["status"] == "Due in 3 days"
    try:
        for width in WIDTHS:
            for theme in ("dark", "light"):
                open_recurring(chrome, site, width, theme)
                rows = chrome.js(ROWS_JS)
                assert len(rows) == len(site.items), (width, theme, len(rows))
                for row in rows:
                    for part in ("name", "sub", "status", "amount"):
                        p = row[part]
                        assert p and p["text"] and p["shown"] and p["whole"] and p["inside"], (width, theme, row["name"]["text"], part, p)
                mine = {row["name"]["text"]: row for row in rows if row["name"]["text"] in want}
                assert set(mine) == set(want), (width, theme, sorted(mine))
                for name, w in want.items():
                    got = {k: mine[name][k]["text"] for k in ("sub", "status", "amount")}
                    assert got == w, (width, theme, name)
                    assert mine[name]["narrow"] is True   # the Subscriptions card: the status under the amount
                assert chrome.problems() == [], (width, theme)
                if width == 1440:   # a row with the room is laid out as before: the amount and status beside the name
                    assert mine["Dynadot"]["beside"] and mine["Squarespace"]["beside"], (theme, mine)
                chrome.js("localStorage.removeItem('hf.theme')")   # the next state starts dark, as the gate's do
    finally:
        chrome.js("localStorage.removeItem('hf.theme')")


def test_the_layout_gate_passes_the_page_at_every_width_in_both_themes(site, tmp_path, monkeypatch):
    """The gate's own checks on the Recurring page at the gate's seven widths and at 1200 and 1100, both themes (the
    gate's plan: light at 430 and 360 is its dark-only phone pass; the test above reads those in light)."""
    for width, height in EXTRA_HEIGHTS.items():
        monkeypatch.setitem(audit_layout.HEIGHT, width, height)
    lines: list[str] = []
    rep = audit_layout.audit(site.url, tmp_path / "fit", "k1", WIDTHS, log=lines.append, states={"recurring"},
                             themes=("dark", "light"), self_test=False, workers=2)
    ran = {(w, key) for w, states in rep["widths"].items() for key, e in states.items() if "checks" in e}
    assert ran == {(str(w), key) for w in WIDTHS for key in ("recurring", "recurring@light") if not (w in (430, 360) and "@" in key)}
    assert rep["fail"] == 0, "\n".join(line for line in lines if not line.startswith("PASS"))


def test_the_gate_cuts_the_rows_with_the_fix_taken_out(site, tmp_path, monkeypatch):
    """Discriminating: the same check fails the page at 1280 and 1200 once the name column may shrink past its longest
    word again (the late renewal's status leaves it about 40px there)."""
    monkeypatch.setitem(audit_layout.HEIGHT, 1200, EXTRA_HEIGHTS[1200])
    for width in (1280, 1200):
        old = audit_layout.audit(site.url, tmp_path / f"old{width}", "old", (width,), log=lambda line: None, states={"recurring"},
                                 inject=FIX_OUT, themes=("dark",), self_test=False, workers=1)
        vs = old["widths"][str(width)]["recurring"]["checks"]["cliptext"]["violations"]
        cut = {v["detail"]["text"] for v in vs}
        assert any(t == "Hostinger" or t.startswith("Subscriptions") for t in cut), (width, cut)
