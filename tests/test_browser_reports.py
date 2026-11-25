"""The three Reports pages at 1440 / 1024 / 768 / 390, with screenshots: the year heatmap and its four KPIs, the
drill-down opening in place with Open in Transactions, the money flow (Sankey on a desktop, bars on a phone), and the
balance line with its two named everyday-spending lines (never "confidence", never a percent).

Numbers are compared to reports.py's own output on the same database (±$0.01), so a value recomputed in JS would fail.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome, tap
from hermes_finance import db, insights, reports

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout  # noqa: E402  (the layout gate's own checks, run against a short-history DB)

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date(2026, 11, 15)
SHOTS = Path(os.environ.get("REPORTS_SHOTS", Path(__file__).resolve().parents[1] / "shots" / "reports"))


def amount(text):
    """'$4,428.31' / '−$30.00' -> float."""
    t = re.sub(r"[−–]", "-", text).replace("$", "").replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    assert m, text
    return float(m.group(0))


def points(d):
    """An SVG path's 'd' as [(x, y), ...] (the numbers are written with one decimal)."""
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", d)]
    assert len(nums) % 2 == 0, d
    return list(zip(nums[0::2], nums[1::2]))


def outside_viewbox(b, selector):
    """Text nodes whose bbox leaves its SVG's viewBox (the reviewer's round-1 overflow finding: a label starting at
    x -3.6 at 1024, and month ticks overhanging the right edge)."""
    return b.js(f"""(() => {{
        const s = document.querySelector({json.dumps(selector)});
        if (!s || getComputedStyle(s).display === 'none') return [];
        const vb = s.viewBox.baseVal, bad = [];
        for (const t of s.querySelectorAll('text')) {{
          const bb = t.getBBox();
          if (bb.x < vb.x - 0.5 || bb.y < vb.y - 0.5
              || bb.x + bb.width > vb.x + vb.width + 0.5 || bb.y + bb.height > vb.y + vb.height + 0.5)
            bad.push([t.textContent, +bb.x.toFixed(1), +bb.y.toFixed(1), +bb.width.toFixed(1), +bb.height.toFixed(1)]);
        }}
        return bad;}})()""")


def overlapping_text(b, selector):
    """[label A, label B, x overlap, y overlap] for visible SVG text boxes that intersect by more than 1px each way
    (B2 follow-up: at 1024 a 24-30px bar's amount line landed on the next bar's name). Labels in the Sankey share one
    x column per side, so a vertical overlap is a collision."""
    return b.js(f"""(() => {{
        const s = document.querySelector({json.dumps(selector)});
        if (!s || getComputedStyle(s).display === 'none') return [];
        const boxes = [...s.querySelectorAll('text')].filter((t) => t.getClientRects().length).map((t) => {{
          const r = t.getBoundingClientRect();
          return {{t: t.textContent, l: r.left, r: r.right, top: r.top, bottom: r.bottom}};
        }});
        const out = [];
        for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {{
          const a = boxes[i], c = boxes[j];
          const ox = Math.min(a.r, c.r) - Math.max(a.l, c.l);
          const oy = Math.min(a.bottom, c.bottom) - Math.max(a.top, c.top);
          if (ox > 1 && oy > 1) out.push([a.t, c.t, +ox.toFixed(1), +oy.toFixed(1)]);
        }}
        return out;}})()""")


@pytest.mark.parametrize("width", [1440, 1024, 768, 390])
def test_reports_pages_and_capture(tmp_path, width):
    path = tmp_path / "synthetic.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    year = reports.year_spending(conn, TODAY)
    month = insights.month_of(TODAY)
    last = insights.days_in_month(month)
    flow = reports.money_flow(conn, f"{month}-01", f"{month}-{last}")
    chart = reports.forecast(conn, TODAY, reports.FORECAST_DAYS)
    groceries = next(c for c in year["categories"] if c["category"] == "Groceries")
    drill_net = reports.category_activity(conn, groceries["category_id"], f"{month}-01", f"{month}-{last}")["net"]
    conn.close()
    server = Server(path, today=TODAY)
    SHOTS.mkdir(parents=True, exist_ok=True)
    try:
        with Chrome() as b:
            b.size(width, 900 if width >= 1024 else 844, scale=1)
            b.goto(f"{server.url}/#/reports?view=spending&month={insights.month_of(TODAY)}")
            b.settle()
            assert b.js("document.documentElement.scrollWidth <= innerWidth + 1"), "horizontal overflow"
            assert b.js("document.querySelectorAll('.rp-kpi').length") == 4
            # the 12-month average KPI is reports.py's number, to the cent
            assert abs(amount(b.js("document.querySelector('.rp-kpi .big').textContent")) - year["kpis"]["average"]) < 0.01
            # the year spans the 12 months ending at the selected one
            assert b.js("document.querySelectorAll('.rp-hm button.rp-c, .rp-cards .rp-spark i').length") > 0
            assert outside_viewbox(b, ".rp-month .rp-msvg") == []       # the month tick labels stay inside the chart
            if width >= 1024:
                assert b.js("document.querySelectorAll('.rp-hm').length") == 1
                assert b.js("getComputedStyle(document.querySelector('.rp-cards')).display") == "none"
                assert b.js("document.querySelectorAll('.rp-hm .rp-c.on[aria-label^=\"Groceries, \"]').length") == 1  # the selected month, one per row
            else:
                assert b.js("document.querySelectorAll('.rp-cards .rp-cat').length") > 0
                assert b.js("getComputedStyle(document.querySelector('.rp-hm')).display") == "none"
            b.screenshot(SHOTS / f"reports-spending-{width}.png", full=True)

            # q3: the drill-down opens in place, shows the same Net, and links to Transactions
            if width >= 1024:
                tap(b, '.rp-hm button.rp-c.on[aria-label^="Groceries, "]')
            else:
                tap(b, '.rp-cat[data-cat="Groceries"] .rp-cat-b')
            b.settle()
            slot = f"#d{groceries['category_id']}" if width >= 1024 else f"#p{groceries['category_id']}"
            assert b.js(f"document.querySelector({json.dumps(slot)}).querySelectorAll('.rp-gnr').length") == 1
            assert "Open in Transactions" in b.js(f"document.querySelector({json.dumps(slot)}).textContent")
            assert "#/transactions?category=" in b.js(f"document.querySelector({json.dumps(slot + ' a.lnk')}).getAttribute('href')")
            if width == 1440:
                assert abs(amount(b.js(f"document.querySelector({json.dumps(slot + ' .rp-gnr .rp-gn.net .v')}).textContent")) - drill_net) < 0.01
            b.screenshot(SHOTS / f"reports-spending-drill-{width}.png", full=True)

            # Cash flow: the remaining is money_flow's number; the Sankey on a desktop, bars on a phone
            b.goto(f"{server.url}/#/reports?view=cashflow&month={insights.month_of(TODAY)}")
            b.settle()
            assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
            if width >= 1024:
                assert b.js("document.querySelectorAll('.rp-flow .rp-svg').length") == 1
                assert b.js("getComputedStyle(document.querySelector('.rp-flow .rp-svg')).display") != "none"
                assert outside_viewbox(b, ".rp-flow .rp-svg") == []    # every Sankey label inside the chart
                assert overlapping_text(b, ".rp-flow .rp-svg") == []   # and no label lands on another (B2 follow-up)
                # a short bar keeps its name and loses only the amount line; every bar keeps both in its <title>
                assert b.js("document.querySelectorAll('.rp-flow .rp-sk-amt').length") >= 2
                assert all("$" in t for t in b.js(
                    "[...document.querySelectorAll('.rp-flow .rp-svg > rect > title')].map((t) => t.textContent)"))
                # the centre total is the server's money in / money out, not a sum of the source rows
                centre = amount(b.js("document.querySelector('.rp-flow .rp-sk-big').textContent"))
                assert abs(centre - (flow["money_out"] if flow["short"] else flow["money_in"])) < 1.0
                text = b.js("document.querySelector('.rp-chips').textContent")
                assert "Short by" in text if flow["short"] else "remaining" in text
            else:
                assert b.js("getComputedStyle(document.querySelector('.rp-flow .rp-svg')).display") == "none"
                bars = b.js("document.querySelector('.rp-bars').textContent")
                if flow["short"]:
                    assert "From the balance" in bars and "not income" in bars
                    assert "Short by" in b.js("document.querySelector('.rp-chips').textContent")
                else:
                    assert abs(amount(b.js("document.querySelector('.rp-rem .v').textContent")) - round(flow["remaining"])) < 0.01
            assert b.js("document.querySelectorAll('.rp-moves').length") == 1    # moves are listed, never drawn
            b.screenshot(SHOTS / f"reports-cashflow-{width}.png", full=True)

            # a month with money left over draws "Remaining after spending" as a sink: the widest label on the right,
            # so the gutter must keep it inside the chart at 1024 too (the card is at its narrowest there)
            b.goto(f"{server.url}/#/reports?view=cashflow&month=2026-05")
            b.settle()
            assert b.js("document.querySelectorAll('.rp-moves').length") == 1
            if width >= 1024:
                assert "Remaining after spending" in b.js("document.querySelector('.rp-flow .rp-svg').textContent")
                assert outside_viewbox(b, ".rp-flow .rp-svg") == []

            # February 2026: the small Leisure bar, whose amount line used to land on the next name at 1024
            b.goto(f"{server.url}/#/reports?view=cashflow&month=2026-02")
            b.settle()
            if width >= 1024:
                assert b.js("document.querySelectorAll('.rp-flow .rp-sk-name').length") >= 4
                assert overlapping_text(b, ".rp-flow .rp-svg") == []
                assert outside_viewbox(b, ".rp-flow .rp-svg") == []

            # Forecast: two named lines, no percent, no "confidence"
            b.goto(f"{server.url}/#/reports?view=forecast")
            b.settle()
            assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
            page = b.js("document.querySelector('#page').textContent")
            assert "Lower everyday spending" in page and "Higher everyday spending" in page
            assert "%" not in page and "confidence" not in page.lower()
            assert abs(amount(b.js("document.querySelector('.rp-ends .rp-er .v').textContent")) - chart["lines"]["lower"]["end"]) < 0.01
            assert b.js("document.querySelectorAll('.rp-acc .rp-chip').length") >= 1
            assert b.js("document.querySelector('.rp-acc .rp-chip[aria-pressed=true]') !== null")
            assert b.js("document.querySelectorAll('.rp-fsvg .rp-band').length") == 1
            # the band is the area between the two named cases: its first n points are the lower line, its last n the
            # higher line reversed (not the projected line: the round-1 defect)
            band_d = b.js("document.querySelector('.rp-fsvg .rp-band').getAttribute('d')")
            low_d = b.js("document.querySelector('.rp-fsvg .rp-line.lower').getAttribute('d')")
            high_d = b.js("document.querySelector('.rp-fsvg .rp-line.higher').getAttribute('d')")
            exp_d = b.js("document.querySelector('.rp-fsvg .rp-line.exp').getAttribute('d')")
            assert band_d.endswith("Z")
            band_pts, low_pts, high_pts = points(band_d[:-1]), points(low_d), points(high_d)
            assert band_pts[:len(low_pts)] == low_pts, (band_pts[:3], low_pts[:3])
            assert band_pts[len(low_pts):] == list(reversed(high_pts))
            assert not band_d.startswith(exp_d)
            assert outside_viewbox(b, ".rp-fsvg") == []
            assert b.js("document.querySelectorAll('.rp-lists .card').length") == 2
            # the bills footer total is forecast()'s own bills_total, not two rows summed in the browser
            bills_total = amount(b.js("document.querySelectorAll('.rp-lists .card')[1].querySelector('.rp-tot .v').textContent"))
            assert abs(bills_total - chart["bills_total"]) < 0.01
            assert b.problems() == [], b.problems()
            b.screenshot(SHOTS / f"reports-forecast-{width}.png", full=True)
    finally:
        server.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_a_partial_history_month_is_drawn_light_in_the_light_theme(tmp_path, width):
    """Round-1 Major: the no-data hatch was a hard-coded near-black, so on the light theme's white card every month
    before the account's history was a dark block (ui-invariants rule 10). The hatch now uses the theme's own
    --card2 / --card3."""
    path = tmp_path / "short-history.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    with conn:
        conn.execute("DELETE FROM transactions WHERE date < '2026-06-01'")
    year = reports.year_spending(conn, TODAY)
    assert any(m["nodata"] for m in year["months"]) and any(not m["nodata"] for m in year["months"])
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.size(width, 900 if width >= 1024 else 844, scale=1)
            b.goto(f"{server.url}/#/reports?view=spending&month={insights.month_of(TODAY)}")
            b.settle()
            b.js("document.documentElement.dataset.theme = 'light'")   # the light theme's own variables
            b.settle()
            sel = ".rp-hm .rp-c.nodata" if width >= 1024 else ".rp-cards .rp-spark i.nd"
            assert b.js(f"document.querySelectorAll({json.dumps(sel)}).length") > 0
            shades = b.js(f"""(() => {{
                const out = new Set();
                for (const el of document.querySelectorAll({json.dumps(sel)})) out.add(getComputedStyle(el).backgroundImage);
                return [...out];}})()""")
            assert shades, "no computed hatch to check"
            for bg in shades:
                rgb = [(int(r), int(g), int(bl)) for r, g, bl in
                       re.findall(r"rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)", bg)]
                assert rgb, bg
                assert all(min(c) >= 200 for c in rgb), bg     # light theme: --card2 #f3f5f9, --card3 #e7eaf0
            b.screenshot(SHOTS / f"reports-spending-light-partial-{width}.png", full=True)
    finally:
        server.close()


def test_a_short_history_db_passes_the_layout_audit(tmp_path):
    """B2 follow-up (2): every no-data heatmap cell carries `span.sr` "No data", and the gate's clipping check counted
    it as content bigger than its overflow:hidden box (scrollWidth 55 vs clientWidth 1). On a DB whose history starts in
    June 2026 that was 4 FAILs (reports at 1440 and 1024, dark and light); the full-history gate DBs never see them.
    `.sr` is never drawn, as the audit's own vis2 already says, so the clipping check now skips it."""
    path = tmp_path / "short-history.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    with conn:
        conn.execute("DELETE FROM transactions WHERE date < '2026-06-01'")
    year = reports.year_spending(conn, TODAY)
    assert any(m["nodata"] for m in year["months"]), "the fixture has no hatched month"
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            for theme in ("dark", "light"):
                res = audit_layout.run_state(b, server.url, 1440, "reports", "reports", [], "page", theme=theme)
                assert not res["problems"], (theme, res["problems"])
                assert res["seen"]["clipping"] > 0, (theme, res["seen"])
                assert res["violations"] == [], (theme, [(v["check"], v["rule"], v["what"][:3])
                                                         for v in res["violations"]])
    finally:
        server.close()


def _month_with_moves(path, count=64):
    """A demo database whose current month holds `count` moves. The demo's own months stay under the 50-row cap, so
    the tests add transfers until the month holds 64; the rows are dated 1-28 so the newest ten are well defined."""
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    month = insights.month_of(TODAY)
    start, end = f"{month}-01", f"{month}-{insights.days_in_month(month):02d}"
    currency = db.display_currency(conn)
    account_id, item_id = conn.execute("SELECT account_id, item_id FROM accounts WHERE type = 'depository' "
                                       "AND iso_currency = ? LIMIT 1", (currency,)).fetchone()
    have = conn.execute("SELECT COUNT(*) FROM v_transactions WHERE is_transfer = 1 AND amount > 0 AND date >= ? "
                        "AND date <= ? AND iso_currency = ?", (start, end, currency)).fetchone()[0]
    with conn:
        for i in range(count - have):
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                "iso_currency, pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 20.00, ?, 0, 'TRANSFER_OUT', 'TRANSFER_OUT_ACCOUNT_TRANSFER', 0, ?, ?)",
                (f"mvcap{i:02d}", account_id, item_id, f"{month}-{1 + i % 28:02d}", f"Move {i:02d}",
                 f"Move {i:02d}", currency, "2026-11-15T20:00:00Z", "2026-11-15T20:00:00Z"))
    return conn, month, start, end


def test_the_cashflow_card_counts_every_move_past_the_ten_it_lists(tmp_path):
    """t_209138b7 review nit 1: with 64 moves the card said "40 more moves" — the 50 capped rows minus the 10 drawn —
    instead of 54, every move past the 10 rows. The demo's own months stay under the 50-row cap, so this DB adds
    transfers until the month holds 64."""
    path = tmp_path / "moves-cap.sqlite"
    conn, month, start, end = _month_with_moves(path)
    moves = reports.moves(conn, start, end)
    assert (moves["count"], moves["shown"], moves["truncated"]) == (64, 50, True)
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.size(1440, 900, scale=1)
            b.goto(f"{server.url}/#/reports?view=cashflow&month={month}")
            b.settle()
            assert b.js("document.querySelectorAll('.rp-moves .li').length") == 10
            note = b.js("document.querySelector('.rp-moves .rp-note')?.textContent || null")
            assert note == "54 more moves in Transactions.", note
            assert b.problems() == [], b.problems()
    finally:
        server.close()


def test_the_cashflow_card_lists_the_newest_ten_moves(tmp_path):
    """t_853a4662: `moves.rows` is the newest 50, oldest first, so `slice(0, 10)` drew a middle slice — moves 15-24 of
    64 (2026-10-05..10-09 in the reviewer's probe) — above "54 more moves". The card lists the newest ten now, the same
    end of the list the server caps on; a month with 11-50 moves draws its newest ten where it used to draw its
    oldest."""
    path = tmp_path / "moves-newest.sqlite"
    conn, month, start, end = _month_with_moves(path)
    moves = reports.moves(conn, start, end)
    assert (moves["count"], moves["shown"], moves["truncated"]) == (64, 50, True)
    newest = [r["label"] for r in moves["rows"][-10:]]
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.size(1440, 900, scale=1)
            b.goto(f"{server.url}/#/reports?view=cashflow&month={month}")
            b.settle()
            drawn = b.js("Array.from(document.querySelectorAll('.rp-moves .li b')).map((n) => n.textContent)")
            assert drawn == newest, drawn
            assert b.problems() == [], b.problems()
    finally:
        server.close()


def test_answering_a_paycheck_keeps_answers_for_sources_not_detected(tmp_path):
    """Round-1 Minor (security L3): saving one answer rebuilt the map from the sources detected now, dropping an answer
    for a source that is not in this month's list. The page now merges into GET /api/settings' saved map."""
    path = tmp_path / "answers.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    key = reports.detected_paychecks(conn, TODAY)["sources"][0]["key"]
    other = "acc_gone|USD|old employer"
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     ("reports_paychecks", json.dumps({other: "add"}), db.now()))
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.size(1440, 900, scale=1)
            b.goto(f"{server.url}/#/reports?view=forecast")
            b.settle()
            sel = ".rp-ask[data-paycheck=" + json.dumps(key) + "]"
            assert b.js(f"document.querySelector({json.dumps(sel)}) !== null")
            tap(b, sel + " .btn")
            b.settle()
            saved = server.api("/api/settings")["reports_paychecks"]
            assert saved.get(other) == "add", saved     # not detected right now: the saved answer survives the tap
            assert saved.get(key) == "add", saved
            assert b.js(f"document.querySelector({json.dumps(sel)}) === null")   # answered: the card is gone
            assert b.problems() == [], b.problems()
    finally:
        server.close()
