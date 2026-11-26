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
from datetime import date, timedelta
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


@pytest.mark.parametrize("case", ["moves", "refund", "refund_geometry", "ratio", "reversal"])
def test_money_flow_small_windows(tmp_path, case):
    path = tmp_path / "synthetic.sqlite"
    day = "2026-11-14"
    with db.connect(path) as conn:
        demo_data.build(conn, today=TODAY)
        conn.execute("DELETE FROM transactions WHERE date = ?", (day,))
        labels = {
            "moves": [(500, "TRANSFER_OUT", "TRANSFER_OUT_SAVINGS"),
                      (-500, "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")],
            "refund": [(-0.12, "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")],
            "ratio": [(-90, "INCOME", "INCOME_WAGES"), (-10, "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
                      (20, "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT")],
            "reversal": [(70, "INCOME", "INCOME_WAGES"), (-20, "INCOME", "INCOME_INTEREST_EARNED")],
        }
        for i, (value, primary, detail) in enumerate(labels[case.split('_')[0]]):
            conn.execute("""INSERT INTO transactions (transaction_id, account_id, item_id, date, name, amount,
                               iso_currency, pfc_primary, pfc_detailed, first_seen_at, updated_at)
                            VALUES (?, 'acc_nova_chk', 'demo_nova', ?, ?, ?, 'USD', ?, ?, ?, ?)""",
                         (f"flow-{i}", day, f"Flow row {i}", value, primary, detail, day, day))
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.pin_clock(TODAY)
            b.size(390, 844)
            b.goto(f"{server.url}/#/reports?view=cashflow&since={day}&until={day}")
            b.settled()
            if case == "moves":
                assert b.js("document.querySelector('.rp-moves .rp-tot')?.textContent ?? null") == "1 move$500"
                assert b.js("document.querySelectorAll('.rp-flow .rp-svg').length") == 0
            elif case == "refund":
                assert b.js("""[
                    document.querySelector('.rp-rem').textContent,
                    document.querySelectorAll('.rp-bars .rp-fr').length,
                    document.querySelector('.rp-flow .rp-sk-big').textContent
                ]""") == ["Remaining after spending$0.12", 1, "$0.12"]  # one refund, no fake money-out row
            elif case == "refund_geometry":
                width, path, mid_bottom = b.js("""(() => {const s = document.querySelector('.rp-flow .rp-svg');
                    const mid = s.querySelector('rect:not(:has(title))');
                    return [document.querySelector('.rp-bars .rp-track i').style.width,
                        s.querySelector('path').getAttribute('d'),
                        Number(mid.getAttribute('y')) + Number(mid.getAttribute('height'))];})()""")
                assert (width, points(path)[4][1]) == ("100%", mid_bottom)
            elif case == "ratio":
                assert "20% of money in" in b.js("document.querySelector('.rp-chips').textContent")
                assert b.js("document.querySelector('.rp-rem small').textContent") == "88.9% of income"
                assert b.js("document.querySelectorAll('.rp-bars .rp-fr').length") == 3
            else:
                assert b.js("document.querySelector('.rp-bars .rp-flh').textContent") == "Money in$20"
                assert b.js("document.querySelector('.rp-bars .rp-fr:last-child').textContent") == "Paychecks reversed$70 · 100%"
                assert b.js("document.querySelector('.rp-bars .rp-flh.bal span').textContent") == "$50 · not income"
                assert b.js("document.querySelector('.rp-flow .rp-sk-big').textContent") == "$70"
    finally:
        server.close()


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
                    "[...document.querySelectorAll('.rp-flow .rp-svg rect > title')].map((t) => t.textContent)"))
                # the centre total is the server's money in / money out, not a sum of the source rows
                centre = amount(b.js("document.querySelector('.rp-flow .rp-sk-big').textContent"))
                assert centre == (flow["money_out"] if flow["short"] else flow["money_in"])
                text = b.js("document.querySelector('.rp-chips').textContent")
                assert "Short by" in text if flow["short"] else "remaining" in text
            else:
                assert b.js("getComputedStyle(document.querySelector('.rp-flow .rp-svg')).display") == "none"
                bars = b.js("document.querySelector('.rp-bars').textContent")
                if flow["short"]:
                    assert "From the balance" in bars and "not income" in bars
                    assert "Short by" in b.js("document.querySelector('.rp-chips').textContent")
                else:
                    assert amount(b.js("document.querySelector('.rp-rem .v').textContent")) == flow["remaining"]
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
            assert b.js("document.querySelectorAll('.rp-acc .vx-row input[type=checkbox]').length") >= 1
            assert b.js("document.querySelector('.rp-acc .vx-row input:checked') !== null")
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


def test_one_day_forecast_draws_only_projected_balances(tmp_path):
    path = tmp_path / "one-day.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.size(390, 844, scale=1, mobile=True)
            b.pin_clock(TODAY)
            for window in (f"until={TODAY.isoformat()}", "days=30"):
                b.goto(f"{server.url}/#/reports?view=forecast&{window}")
                b.settle()
                assert b.js("document.querySelector('.rp-fsvg .rp-ev.low') !== null"), b.js("document.querySelector('#page').innerText")
                days = 1 if window.startswith("until") else 30
                assert b.js(f"document.querySelector('.rp-chart .ttl').textContent.startsWith('Next {days} day')")
                assert b.js("document.querySelector('.rp-chart .rp-leg').innerText.includes('Projected')")
                assert b.js("!document.querySelector('.rp-chart').innerText.includes('Balance so far')")
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
            income = "[...document.querySelectorAll('.rp-kpi')].find((c) => c.querySelector('h3').textContent === 'Expected in')"
            assert amount(b.js(f"{income}.querySelector('.big').textContent")) == 0
            for width in (1440, 390):
                b.size(width, 900, scale=1, mobile=width == 390)
                for theme in ("light", "dark"):
                    b.js(f"document.documentElement.dataset.theme = {json.dumps(theme)}")
                    assert "Paycheck needs confirmation" in b.js(f"{income}.innerText")
                    assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
            b.size(1440, 900, scale=1)
            tap(b, sel + " .btn")
            b.settle()
            saved = server.api("/api/settings")["reports_paychecks"]
            assert saved.get(other) == "add", saved     # not detected right now: the saved answer survives the tap
            assert saved.get(key) == "add", saved
            assert b.js(f"document.querySelector({json.dumps(sel)}) === null")   # answered: the card is gone
            assert "Paycheck needs confirmation" not in b.js(f"{income}.innerText")
            assert amount(b.js(f"{income}.querySelector('.big').textContent")) > 0
            assert b.problems() == [], b.problems()
    finally:
        server.close()


# --- 0.5 UI fix: the Reports pages match the approved mocks (window pill, ⓘ helper text, centred heroes, checkboxes) ---

@pytest.mark.parametrize("width", [1440, 390])
def test_reports_pages_match_the_approved_mocks(tmp_path, width):
    """The round-4 mock board is the source of truth: the Transactions pill sits right after the title, each card's
    small text is behind an ⓘ (a bounded count of visible words), the hero cards are centred, and the account choices
    are real checkboxes. The audits only measure overflow, so this one compares each page with what was approved."""
    path = tmp_path / "mock.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.size(width, 900, scale=1, mobile=width < 500)
            for view, icons_min, words_max in (("spending", 5, 160), ("cashflow", 1, 190), ("forecast", 6, 230)):
                b.goto(f"{server.url}/#/reports?view={view}")
                b.settle()
                pill = b.js("(() => { const m = document.getElementById('mon'), t = document.querySelector('#topbar h1, .top h1, h1');"
                            "const a = m.getBoundingClientRect(), r = t.getBoundingClientRect();"
                            "return {cls: m.className, h1: t.textContent, gap: a.left - r.right, dy: Math.abs((a.top + a.height / 2) - (r.top + r.height / 2)),"
                            "below: a.top - r.bottom, label: document.getElementById('monl').textContent.trim()}; })()")
                assert "txwindow" in pill["cls"] and pill["h1"].strip() == "Reports", (view, pill)
                if width >= 1024:
                    assert 0 <= pill["gap"] <= 40 and pill["dy"] < 24, (view, pill)       # directly after the title
                else:
                    assert pill["below"] >= -1, (view, pill)                              # full width under it
                assert b.js("document.querySelectorAll('.rp-body .vx-i summary').length") >= icons_min, view
                assert b.js("document.querySelector('.rp-body .rp-sub, .rp-body .rp-skn, .rp-body .rp-phonly') === null"), view
                visible = b.js("(() => { const c = document.querySelector('.rp-body').cloneNode(true);"
                               "c.querySelectorAll('.vx-pop, svg, script, style').forEach((e) => e.remove());"
                               "return c.textContent.split(/\\s+/).filter(Boolean).length; })()")
                assert visible <= words_max * 3, (view, visible)       # a budget, not an exact count: the page stays short
                # the hero icons wear the category-icon chip: a tinted background and a coloured glyph, not a bare outline
                chips = b.js("[...document.querySelectorAll('.vx-kpi > .vx-hi')].map((e) => { const s = getComputedStyle(e);"
                             "return {bg: s.backgroundColor, color: s.color, r: s.borderRadius, w: e.getBoundingClientRect().width}; })")
                assert chips or view == "cashflow", view
                for c in chips:
                    assert c["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"), (view, c)
                    assert c["color"] != b.js("getComputedStyle(document.body).color"), (view, c)
                    assert c["r"] == "10px" and 30 <= c["w"] <= 34, (view, c)
            b.goto(f"{server.url}/#/reports?view=spending")
            b.settle()
            kp = b.js("[...document.querySelectorAll('.vx-kpi')].map((c) => getComputedStyle(c).textAlign)")
            assert kp and set(kp) == {"center"}
            b.goto(f"{server.url}/#/reports?view=forecast")
            b.settle()
            assert b.js("document.querySelectorAll('.rp-acc .rp-chip').length") == 0
            assert b.js("document.querySelectorAll('.rp-acc input[type=checkbox]').length") >= 1
    finally:
        server.close()


def test_the_reports_pill_opens_the_transactions_sheet_and_applies_a_window(tmp_path):
    path = tmp_path / "win.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.size(1440, 900, scale=1)
            b.goto(f"{server.url}/#/reports?view=cashflow")
            b.settle()
            assert b.js("document.getElementById('monl').textContent.trim()") == "November 2026"
            tap(b, "#monl")
            assert b.js("document.querySelector('.rgpicker .rgmg') !== null && document.querySelector('.rgpicker #rgfrom') !== null")
            b.js("[...document.querySelectorAll('.rgpicker .rgpills button')].find((x) => x.textContent === '30d').click()")
            b.settle()
            assert "since=30d" in b.js("location.hash")
            assert b.js("document.getElementById('monl').textContent.trim()") == "Last 30 days"
            assert b.js("document.getElementById('rgclear') !== null")
            tap(b, "#rgclear")
            b.settle()
            assert b.js("document.getElementById('monl').textContent.trim()") == "November 2026"
            b.goto(f"{server.url}/#/reports?view=forecast")
            b.settle()
            tap(b, "#monl")
            assert b.js("document.querySelector('.rgpicker .rgmg') === null && document.querySelector('.rgpicker #rguntil') !== null")
    finally:
        server.close()


def test_spending_for_a_range_sizes_its_heatmap_to_the_months_it_sends(tmp_path):
    """Review round 1: the grid had 12 month tracks whatever the API sent, so a 2-month window put the first category's
    name in the wrong column and a 24-month one wrapped. The grid, its total label and the pill follow the months."""
    path = tmp_path / "range.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.pin_clock(TODAY)
            b.size(1440, 900, scale=1)
            for query, months in (("since=7d", 2), ("window=all", None), ("", 12)):
                b.goto(f"{server.url}/#/reports?view=spending&{query}")
                b.settle()
                got = b.js("""(() => { const g = document.querySelector('.rp-hm'); if (!g) return null;
                  const first = [...g.children].filter((c) => c.classList.contains('rp-hh') && !c.classList.contains('rp-tot')).length;
                  const rows = getComputedStyle(g).gridTemplateColumns.split(' ').length;
                  const top = Math.round(g.children[1].getBoundingClientRect().top);
                  const hd = [...g.children].filter((c) => c.classList.contains('rp-hh')).map((c) => c.getBoundingClientRect());
                  const lab = g.querySelector('.rp-lab').getBoundingClientRect(), row = g.querySelector('.rp-rl').getBoundingClientRect();
                  const tot = g.querySelector('.rp-tot').getBoundingClientRect(), cell = g.querySelector('.rp-c').getBoundingClientRect();
                  // one header row (the cells differ in height, so their tops may differ a little), the first category under
                  // its own column, and the total column to the right of every month
                  const oneRow = Math.max(...hd.map((r) => r.top)) - Math.min(...hd.map((r) => r.top)) < 30 && row.top > lab.bottom - 1;
                  return {n: Number(g.style.getPropertyValue('--n')), tracks: rows, hh: first, tot: g.querySelector('.rp-tot').textContent,
                          oneRow: oneRow && Math.abs(row.left - lab.left) < 2 && tot.left > cell.left, pill: document.getElementById('monl').textContent}; })()""")
                assert got and got["tracks"] == got["n"] + 2 and got["oneRow"], (query, got)
                assert got["tot"] == f"{got['n']} mo" and (months is None or got["n"] == months), (query, got)
                if query:
                    assert "·" in got["pill"], got
            # The old side rail squeezed both ranges at 1181–1279 px. Chrome still showed touching labels at 1280,
            # so also check 1319 and 1320, on either side of the new breakpoint. Measure text, not grid cells:
            # a label can extend past its cell without changing the cell's box.
            for query in ("window=all", ""):
                for width in (1024, 1181, 1200, 1230, 1279, 1280, 1319, 1320, 1440):
                    b.size(width, 900, scale=1)
                    b.goto(f"{server.url}/#/reports?view=spending&{query}")
                    b.settle()
                    boxes = b.js("""[...document.querySelectorAll('.rp-hm .rp-hh:not(.rp-lab, .rp-tot)')]
                      .filter((c) => c.textContent.trim()).map((c) => {
                        const r = document.createRange(); r.selectNodeContents(c); const t = r.getBoundingClientRect();
                        return [c.textContent, t.left, t.right]; })""")
                    assert len(boxes) >= 12, (query or "default", width, len(boxes))
                    for (a, _, right), (c, left, _) in zip(boxes, boxes[1:]):
                        assert right < left, (query or "default", width, a, c, left - right)
                    assert b.js("document.documentElement.scrollWidth <= innerWidth + 1"), (query or "default", width)
    finally:
        server.close()


@pytest.mark.parametrize("check", ["exact window", "offsetting refund", "foreign precision"])
def test_spending_number_display(tmp_path, check):
    from test_reports_spending_audit import plant

    path = tmp_path / "spending.sqlite"
    with db.connect(path) as conn:
        demo_data.build(conn, today=TODAY)
        conn.execute("DELETE FROM transactions")
        plant(conn, "outside", "2026-11-08", 800, "FOOD_AND_DRINK_RESTAURANT")
        plant(conn, "market", "2026-11-09", 25, "FOOD_AND_DRINK_GROCERIES")
        plant(conn, "reversal", "2026-11-15", -25, "FOOD_AND_DRINK_GROCERIES")
        plant(conn, "inside", "2026-11-15", 7.52, "FOOD_AND_DRINK_RESTAURANT")
        plant(conn, "souk", "2026-11-15", 1.234, "FOOD_AND_DRINK_GROCERIES", "KWD")
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.pin_clock(TODAY)
            b.size(1440, 900, scale=1)
            b.goto(f"{server.url}/#/reports?view=spending&since=7d")
            b.until("Boolean(document.querySelector('.rp-hm'))")
            if check == "exact window":
                summary = b.js("document.querySelector('.rp-selection')?.textContent || ''")
                assert "$7.52" in summary and "Nov 9" in summary and "Nov 15" in summary, summary
                assert "807.52" in b.js("document.querySelector('.rp-hm .rp-tv').textContent")
            elif check == "offsetting refund":
                sel = '.rp-hm button.rp-c.on[aria-label^="Groceries, "]'
                assert b.js(f"Boolean(document.querySelector({json.dumps(sel)}))"), "a $0 net with two rows must open"
                tap(b, sel)
                b.until("document.querySelectorAll('.rp-hm .rp-txr').length === 2")
                assert b.js("[...document.querySelectorAll('.rp-hm .rp-gn b')].map(e => e.textContent)") == ["$25", "−$25", "$0"]
            else:
                notes = b.js("[...document.querySelectorAll('.rp-body .rp-note')].map(e => e.textContent)")
                assert any("KWD 1.234" in note and "KWD $" not in note for note in notes), notes
    finally:
        server.close()


# --- Cash flow: a row opens Transactions with the same filter and the same time frame --------------------------------

def _flow_db(path):
    """The demo database plus the income and refund rows it lacks: interest, a tax refund and other income (each its
    own source), and refunds in a category with no spending (a "Refunds over spending" source). Synthetic, dated in
    October and November 2026 so every window the test opens holds them."""
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    currency = db.display_currency(conn)
    account_id, item_id = conn.execute("SELECT account_id, item_id FROM accounts WHERE type = 'depository' "
                                       "AND iso_currency = ? LIMIT 1", (currency,)).fetchone()
    extra = [("fl_int_a", "2026-10-20", "Savings interest", -4.12, "INCOME", "INCOME_INTEREST_EARNED"),
             ("fl_int_b", "2026-11-03", "Savings interest", -5.37, "INCOME", "INCOME_INTEREST_EARNED"),
             ("fl_tax", "2026-11-04", "State tax refund", -61.40, "INCOME", "INCOME_TAX_REFUND"),
             ("fl_other", "2026-10-22", "Garage sale", -75.00, "INCOME", "INCOME_OTHER_INCOME"),
             ("fl_gift", "2026-11-05", "Gift from family", -20.00, "INCOME", "INCOME_OTHER_INCOME"),
             ("fl_refund_a", "2026-10-21", "Pet store credit", -30.00, "GENERAL_MERCHANDISE",
              "GENERAL_MERCHANDISE_PET_SUPPLIES"),
             ("fl_refund_b", "2026-11-06", "Pet store credit", -48.25, "GENERAL_MERCHANDISE",
              "GENERAL_MERCHANDISE_PET_SUPPLIES")]
    with conn:
        for tid, day, name, amt, primary, detailed in extra:
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                "iso_currency, pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?)",
                (tid, account_id, item_id, day, name, name, amt, currency, primary, detailed,
                 "2026-11-15T20:00:00Z", "2026-11-15T20:00:00Z"))
    return conn


def _flow_window(conn, query):
    """(hash query, start, end) of a Reports window as the server reads it."""
    if query == "":
        return "", "2026-11-01", "2026-11-30"
    if query.startswith("month="):
        m = query[6:]
        return query, f"{m}-01", f"{m}-{insights.days_in_month(m):02d}"
    if query == "since=30d":
        return query, (TODAY - timedelta(days=29)).isoformat(), TODAY.isoformat()
    if query == "window=all":
        return query, conn.execute("SELECT MIN(date) FROM v_transactions").fetchone()[0], TODAY.isoformat()
    since, until = re.fullmatch(r"since=([\d-]+)&until=([\d-]+)", query).groups()
    return query, since, until


# what Transactions shows (its count and total) once a Cash flow row has been clicked
SUMMARY_JS = """(() => {
  const el = [...document.querySelectorAll('#page .rgsummary, #page .tfsum')].find((x) => !x.hidden && x.getClientRects().length);
  return el ? el.textContent : null; })()"""


def _opened_transactions(b, click_selector):
    tap(b, click_selector)
    b.settle()
    assert b.js("location.hash").startswith("#/transactions?"), b.js("location.hash")
    text = b.js(SUMMARY_JS)
    assert text, "Transactions shows no count and total for this filter"
    n = int(re.search(r"(\d+) payment", text).group(1))
    total = text.split("·", 1)[1]
    return n, amount(total), "+" in total, b.js("location.hash")


@pytest.mark.parametrize("query", ["", "month=2026-10", "since=30d", "since=2026-10-18&until=2026-11-05", "window=all"])
def test_a_cashflow_row_opens_transactions_with_its_filter_and_the_reports_time_frame(tmp_path, query):
    """Cash flow: every income source, every "Refunds over spending" source and every spending group opens
    Transactions with that row's filter and the report's own window (a month, 30 days, From/To or all time). The count
    and the total Transactions shows are the report row's own, so the numbers the row showed are the ones it opens."""
    path = tmp_path / "flow-link.sqlite"
    conn = _flow_db(path)
    query, start, end = _flow_window(conn, query)
    flow = reports.money_flow(conn, start, end)
    conn.close()
    assert {s["source"] for s in flow["income"]["sources"]} == {"paychecks", "interest_tax_refunds", "other"}
    assert [r["category"] for r in flow["refunds_over"]] == ["Pets"]
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.size(1440, 900, scale=1)
            b.pin_clock(TODAY)
            rows = ([(s["label"], s["amount"], s["count"], True) for s in flow["income"]["sources"]]
                    + [(r["label"], r["amount"], r["count"], True) for r in flow["refunds_over"]]
                    + [(g["group"], g["net"], g["count"], False) for g in flow["groups"]])
            assert len(rows) >= 6
            for label, total, count, money_in in rows:
                b.goto(f"{server.url}/#/reports?view=cashflow&{query}")
                b.settle()
                n, shown, plus, hash_ = _opened_transactions(
                    b, f'.rp-flow .rp-svg a.rp-sk-a[aria-label^="{label}:"]')
                assert n == count and abs(shown - total) < 0.01 and plus == money_in, (label, n, shown, total, hash_)
                # the window the report had is the window Transactions has
                params = b.js("Object.fromEntries(new URLSearchParams(location.hash.split('?')[1]))")
                assert params.get("kind") == ("income" if label in {s["label"] for s in flow["income"]["sources"]} else "spend")
                if query.startswith("month=") or query == "":
                    assert params.get("month", "2026-11") == start[:7] and "since" not in params, params
                elif query == "since=30d":
                    assert params.get("since") == "30d" and "month" not in params, params
                else:
                    assert (params.get("since"), params.get("until")) == (start, end), params
            assert b.problems() == [], b.problems()
    finally:
        server.close()


def test_a_cashflow_row_on_a_phone_opens_transactions_and_its_source_chip_clears(tmp_path):
    """The phone's bars list links too (each whole row is the 44px tap), and an income source shows as a chip on
    Transactions that removes itself."""
    path = tmp_path / "flow-phone.sqlite"
    conn = _flow_db(path)
    flow = reports.money_flow(conn, "2026-11-01", "2026-11-30")
    conn.close()
    other = next(s for s in flow["income"]["sources"] if s["source"] == "other")
    home = next(g for g in flow["groups"] if g["group"] == "Home")
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.size(390, 844)
            b.pin_clock(TODAY)
            b.goto(f"{server.url}/#/reports?view=cashflow")
            b.settle()
            rows = b.js("[...document.querySelectorAll('.rp-bars a.rp-fr')].map((a) => a.querySelector('b').textContent)")
            assert "Other income" in rows and "Home" in rows and "Remaining after spending" not in rows, rows
            assert b.js("document.querySelector('.rp-bars a.rp-fr').getBoundingClientRect().height") >= 44
            b.js(f"document.querySelectorAll('.rp-bars a.rp-fr')[{rows.index('Other income')}].dataset.pick = '1'")
            tap(b, ".rp-bars a.rp-fr[data-pick]")       # a finger's tap at the row's centre, not a script's click()
            b.settle()
            assert b.js("location.hash") == "#/transactions?source=other&kind=income&month=2026-11"
            text = b.js(SUMMARY_JS)
            assert re.search(rf"^{other['count']} payments? · ", text) and abs(amount(text.split("·", 1)[1]) - other["amount"]) < 0.01
            chips = b.js("[...document.querySelectorAll('#page .txchips .tfchip')].map((c) => c.getAttribute('aria-label'))")
            assert chips == ["Remove Other income"], chips
            tap(b, "#page .txchips .tfchip")
            b.settle()
            params = b.js("Object.fromEntries(new URLSearchParams(location.hash.split('?')[1]))")
            assert "source" not in params and params.get("kind") == "income", params
            assert b.js("document.querySelectorAll('#page .txchips .tfchip').length") == 0
            b.goto(f"{server.url}/#/reports?view=cashflow")
            b.settle()
            b.js(f"document.querySelectorAll('.rp-bars a.rp-fr')[{rows.index('Home')}].dataset.pick = '1'")
            tap(b, ".rp-bars a.rp-fr[data-pick]")
            b.settle()
            text = b.js(SUMMARY_JS)
            assert re.search(rf"^{home['count']} payments? · ", text) and abs(amount(text.split("·", 1)[1]) - home["net"]) < 0.01
            assert b.problems() == [], b.problems()
    finally:
        server.close()


# --- Forecast chart: the day under the pointer (mock board forecast-chart-hover-details-3b3aa66104aeed8b, round 2,
# option A: the floating card) ---------------------------------------------------------------------------------------

HOVER_KEYS = {"ArrowRight": 39, "ArrowLeft": 37, "Home": 36, "End": 35, "Escape": 27}

# The open card, read back as the page shows it (None while it is closed or faded out).
HOVER_CARD = """(() => {
  const t = document.querySelector('.rp-chart .rp-htip');
  if (!t || !t.checkVisibility({opacityProperty: true, visibilityProperty: true})) return null;
  const s = document.querySelector('.rp-fsvg'), ring = s.querySelector('.rp-hring'), cross = s.querySelector('.rp-hx');
  const shown = (e) => Boolean(e) && e.checkVisibility({opacityProperty: true, visibilityProperty: true});
  const q = (sel) => { const e = t.querySelector(sel); return e ? e.textContent : null; };
  const r = t.getBoundingClientRect(), c = t.closest('.rp-chart').getBoundingClientRect();
  return {date: q('.rp-hd'), big: q('.rp-hbig'), lower: q('.rp-hrow.lower'), higher: q('.rp-hrow.higher'),
          lowerV: q('.rp-hrow.lower .v'), higherV: q('.rp-hrow.higher .v'),
          events: [...t.querySelectorAll('.rp-hev')].map((e) => [e.className, e.textContent]),
          ring: shown(ring) ? [+ring.getAttribute('cx'), +ring.getAttribute('cy')] : null,
          cross: shown(cross) ? [+cross.getAttribute('x1'), +cross.getAttribute('x2')] : null,
          box: [r.left, r.top, r.right, r.bottom], chart: [c.left, c.top, c.right, c.bottom], vw: innerWidth};
})()"""

# Every text in the open card: [class, font size, contrast against the card's own background].
HOVER_TEXT = """(() => {
  const t = document.querySelector('.rp-chart .rp-htip'), out = [];
  const rgb = (s) => s.match(/[\\d.]+/g).slice(0, 3).map(Number);
  const lum = (c) => c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; })
    .reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
  const bg = lum(rgb(getComputedStyle(t).backgroundColor));
  for (const e of t.querySelectorAll('*')) {
    if (![...e.childNodes].some((n) => n.nodeType === 3 && n.data.trim())) continue;
    const cs = getComputedStyle(e), fg = lum(rgb(cs.color));
    out.push([e.className, parseFloat(cs.fontSize), +((Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05)).toFixed(2)]);
  }
  return out;
})()"""


def hover_scene(tmp_path):
    """The demo DB with its detected paycheck added to the line, so the chart has paycheck, bill and lowest dots."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "hover.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=TODAY)
    key = reports.detected_paychecks(conn, TODAY)["sources"][0]["key"]
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     ("reports_paychecks", json.dumps({key: "add"}), db.now()))
    conn.close()
    return Server(path, today=TODAY)


def day_label(iso):
    d = date.fromisoformat(iso)
    return f"{d:%a}, {d:%b} {d.day}" + (" · today" if d == TODAY else "")


def day_point(b, i):
    """Day i of the drawn projected line: its viewBox point and where that is on the screen."""
    return b.js(f"""(() => {{ const s = document.querySelector('.rp-fsvg');
      s.scrollIntoView({{block: 'center', behavior: 'instant'}});
      const n = s.querySelector('.rp-line.exp').getAttribute('d').match(/-?\\d+(?:\\.\\d+)?/g).map(Number);
      const p = new DOMPoint(n[2 * {i}], n[2 * {i} + 1]).matrixTransform(s.getScreenCTM());
      return {{x: p.x, y: p.y, vx: n[2 * {i}], vy: n[2 * {i} + 1]}}; }})()""")


def dot_point(b, cls, vx):
    """The centre, on the screen, of the `cls` dot drawn at viewBox x `vx`."""
    return b.js(f"""(() => {{ const e = [...document.querySelectorAll('.rp-fsvg .rp-ev.{cls}')]
        .find((c) => Math.abs(+c.getAttribute('cx') - {vx}) < 0.2);
      if (!e) return null;
      const r = e.getBoundingClientRect(); return {{x: r.left + r.width / 2, y: r.top + r.height / 2}}; }})()""")


def card_for(b, label, timeout=4.0):
    """The open card once it shows the day `label` (else what is there, for the assert message)."""
    got = b.until(f"(() => {{ const c = {HOVER_CARD}; return c && c.date === {json.dumps(label)} ? c : null; }})()",
                  timeout)
    return got or b.js(HOVER_CARD)


def card_closed(b):
    return b.until(f"{HOVER_CARD} === null")


def mouse_to(b, x, y):
    b.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)


def press(b, key):
    for t in ("keyDown", "keyUp"):
        b.call("Input.dispatchKeyEvent", type=t, key=key, code=key, windowsVirtualKeyCode=HOVER_KEYS[key])


def touch_at(b, x, y):
    b.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y}])
    b.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])


def hover_days(chart):
    """(a plain day: no paycheck or bill on it or next to it, two or more days from the lowest; a paycheck; a bill
    that is not on the lowest day; the lowest day), the days as indexes into the line's points."""
    pts = chart["lines"]["expected"]["points"]
    index = {p["date"]: i for i, p in enumerate(pts)}
    busy = {index[e["date"]] for e in chart["events"] if e["date"] in index}
    low = index[chart["lowest"]["date"]]
    plain = next(i for i in range(1, len(pts)) if not busy & {i - 1, i, i + 1} and abs(i - low) >= 2)
    pay = next(e for e in chart["events"] if e["amount"] >= 1)
    bill = next(e for e in chart["events"] if e["amount"] <= -1 and index[e["date"]] != low)
    return index, plain, pay, bill, low


def check_day(card, chart, i):
    """The card shows day i: its date, and the three balances (whole dollars, as the chart's labels show them)."""
    lines = chart["lines"]
    assert card and card["date"] == day_label(lines["expected"]["points"][i]["date"]), card
    assert abs(amount(card["big"]) - lines["expected"]["points"][i]["balance"]) <= 0.5, (card, i)
    assert card["lower"].startswith("Lower everyday spending") and card["higher"].startswith("Higher everyday spending")
    assert abs(amount(card["lowerV"]) - lines["lower"]["points"][i]["balance"]) <= 0.5, card
    assert abs(amount(card["higherV"]) - lines["higher"]["points"][i]["balance"]) <= 0.5, card


def test_forecast_hover_shows_the_day_under_the_pointer(tmp_path):
    """Hovering the line or any dot opens one floating card: the day's date, its expected balance, the lower and
    higher everyday-spending balances and each paycheck or bill that day. The lowest expected balance is no longer
    written on the chart; its yellow dot's card says it, in yellow. A crosshair and a ring mark the day, the keyboard
    steps through the days, and the card only fades."""
    server = hover_scene(tmp_path)
    chart = server.api("/api/reports/forecast")
    pts = chart["lines"]["expected"]["points"]
    index, plain, pay, bill, low = hover_days(chart)
    try:
        with Chrome() as b:
            b.size(1440, 900, scale=1)
            b.pin_clock(TODAY)
            b.goto(f"{server.url}/#/reports?view=forecast")
            b.settled()
            assert b.js("document.querySelector('.rp-fsvg .rp-callout') === null")
            assert "Expected balance on" not in b.js("document.querySelector('.rp-fsvg').textContent")
            assert b.js(HOVER_CARD) is None                              # nothing open until the pointer is on it

            # the line, on a day with no event
            p = day_point(b, plain)
            mouse_to(b, p["x"], p["y"])
            card = card_for(b, day_label(pts[plain]["date"]))
            check_day(card, chart, plain)
            assert card["events"] == [], card
            assert card["ring"] and abs(card["ring"][0] - p["vx"]) < 0.2 and abs(card["ring"][1] - p["vy"]) < 0.2, card
            assert card["cross"] and abs(card["cross"][0] - p["vx"]) < 0.2 and card["cross"][0] == card["cross"][1]
            # the card sits inside the chart's card, beside the point (not on it), and nothing scrolls sideways
            assert card["chart"][0] <= card["box"][0] and card["box"][2] <= card["chart"][2], card
            assert not (card["box"][0] <= p["x"] <= card["box"][2] and card["box"][1] <= p["y"] <= card["box"][3]), card
            assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
            b.screenshot(SHOTS / "reports-forecast-hover-line-1440.png")

            # a paycheck dot: its name and amount
            i = index[pay["date"]]
            dot = dot_point(b, "in", day_point(b, i)["vx"])
            assert dot, "no paycheck dot on its day"
            mouse_to(b, dot["x"], dot["y"])
            card = card_for(b, day_label(pay["date"]))
            check_day(card, chart, i)
            assert any("in" in c.split() and pay["label"] in t and f"+${pay['amount']:,.2f}" in t
                       for c, t in card["events"]), card

            # a bill dot
            i = index[bill["date"]]
            dot = dot_point(b, "out", day_point(b, i)["vx"])
            assert dot, "no bill dot on its day"
            mouse_to(b, dot["x"], dot["y"])
            card = card_for(b, day_label(bill["date"]))
            check_day(card, chart, i)
            assert any("out" in c.split() and bill["label"] in t and f"−${-bill['amount']:,.2f}" in t
                       for c, t in card["events"]), card
            assert not any("low" in c.split() for c, _ in card["events"]), card

            # the yellow dot: the lowest expected balance, as a yellow line in the same card
            dot = dot_point(b, "low", day_point(b, low)["vx"])
            assert dot, "no lowest-balance dot"
            mouse_to(b, dot["x"], dot["y"])
            card = card_for(b, day_label(chart["lowest"]["date"]))
            check_day(card, chart, low)
            lows = [t for c, t in card["events"] if "low" in c.split()]
            d = date.fromisoformat(chart["lowest"]["date"])
            assert lows == [f"Expected balance on {d:%b} {d.day} · ${round(chart['lowest']['balance']):,}"], card
            assert b.js("getComputedStyle(document.querySelector('.rp-htip .rp-hev.low')).color") == \
                b.js("getComputedStyle(document.querySelector('.rp-fsvg .rp-ev.low')).fill")
            b.screenshot(SHOTS / "reports-forecast-hover-low-1440.png")

            # readable in both themes: secondary text 13px or more, the rest 15px, contrast 4.5:1 or more
            for theme in ("dark", "light"):
                b.js(f"document.documentElement.dataset.theme = {json.dumps(theme)}")
                texts = b.js(HOVER_TEXT)
                assert texts, theme
                for cls, size, ratio in texts:
                    assert size >= 13 and ratio >= 4.5, (theme, cls, size, ratio)
                    if any(k in cls.split() for k in ("rp-hbig", "rp-hev")):
                        assert size >= 15, (theme, cls, size)
            b.js("document.documentElement.dataset.theme = 'dark'")

            # the pointer leaves: the card, the crosshair and the ring go
            mouse_to(b, 5, 5)
            assert card_closed(b), b.js(HOVER_CARD)
            assert b.js("getComputedStyle(document.querySelector('.rp-fsvg .rp-hring')).visibility") == "hidden"

            # the keyboard: the chart takes focus, Home / End / ← / → step through the days, Escape closes
            assert b.js("document.querySelector('.rp-fsvg').tabIndex") == 0
            b.js("document.querySelector('.rp-fsvg').focus()")
            press(b, "Home")
            check_day(card_for(b, day_label(pts[0]["date"])), chart, 0)
            press(b, "ArrowRight")
            press(b, "ArrowRight")
            check_day(card_for(b, day_label(pts[2]["date"])), chart, 2)
            press(b, "ArrowLeft")
            check_day(card_for(b, day_label(pts[1]["date"])), chart, 1)
            press(b, "End")
            check_day(card_for(b, day_label(pts[-1]["date"])), chart, len(pts) - 1)
            press(b, "Escape")
            assert card_closed(b), b.js(HOVER_CARD)

            # motion: the card fades, nothing slides, with or without reduced motion
            for reduce in ("no-preference", "reduce"):
                b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": reduce}])
                moved = b.js("(() => { const s = getComputedStyle(document.querySelector('.rp-htip'));"
                             "return [s.transitionProperty, s.animationName]; })()")
                assert set(moved[0].replace(" ", "").split(",")) <= {"opacity", "visibility"}, (reduce, moved)
                assert moved[1] == "none", (reduce, moved)
            assert b.problems() == [], b.problems()
    finally:
        server.close()


def test_forecast_hover_on_a_phone_is_a_tap(tmp_path):
    """At 390 px a tap opens the same card and it stays open (a touch's pointerleave must not close it); the card
    stays on the screen, also for the last day at the right edge; a tap on the yellow dot adds the lowest balance; a
    tap anywhere else closes it."""
    server = hover_scene(tmp_path)
    chart = server.api("/api/reports/forecast")
    pts = chart["lines"]["expected"]["points"]
    index, plain, pay, bill, low = hover_days(chart)
    try:
        with Chrome() as b:
            b.size(390, 844, mobile=True)
            b.pin_clock(TODAY)
            b.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
            try:
                b.goto(f"{server.url}/#/reports?view=forecast")
                b.settled()
                assert b.js("document.querySelector('.rp-fsvg .rp-callout') === null")
                for theme in ("dark", "light"):
                    b.js(f"document.documentElement.dataset.theme = {json.dumps(theme)}")
                    for i in (plain, len(pts) - 1):
                        p = day_point(b, i)
                        touch_at(b, p["x"], p["y"])
                        card = card_for(b, day_label(pts[i]["date"]))
                        check_day(card, chart, i)
                        b.settled()
                        assert b.still(".rp-chart") is True     # any fade the tap started has ended
                        card = b.js(HOVER_CARD)
                        assert card and card["date"] == day_label(pts[i]["date"]), ("closed after the tap", theme, card)
                        assert 0 <= card["box"][0] and card["box"][2] <= card["vw"], card
                        assert card["chart"][0] <= card["box"][0] and card["box"][2] <= card["chart"][2], card
                        assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
                    for cls, size, ratio in b.js(HOVER_TEXT):
                        assert size >= 13 and ratio >= 4.5, (theme, cls, size, ratio)
                    b.screenshot(SHOTS / f"reports-forecast-hover-tap-390-{theme}.png")

                    dot = dot_point(b, "low", day_point(b, low)["vx"])
                    touch_at(b, dot["x"], dot["y"])
                    card = card_for(b, day_label(chart["lowest"]["date"]))
                    check_day(card, chart, low)
                    assert any("low" in c.split() and t.startswith("Expected balance on") for c, t in card["events"]), card
                    i = index[bill["date"]]
                    dot = dot_point(b, "out", day_point(b, i)["vx"])
                    touch_at(b, dot["x"], dot["y"])
                    card = card_for(b, day_label(bill["date"]))
                    assert any(bill["label"] in t for _, t in card["events"]), card

                    # a tap outside the chart closes the card
                    title = b.js("(() => { const r = document.querySelector('.rp-chart h3 .ttl').getBoundingClientRect();"
                                 "return {x: r.left + 4, y: r.top + r.height / 2}; })()")
                    touch_at(b, title["x"], title["y"])
                    assert card_closed(b), (theme, b.js(HOVER_CARD))
                assert b.problems() == [], b.problems()
            finally:
                b.call("Emulation.setTouchEmulationEnabled", enabled=False)
    finally:
        server.close()


def test_a_phone_category_name_wraps_instead_of_ending_in_an_ellipsis(tmp_path):
    """Review round 2 (Nightly layout gate, 360 px): the amount next to a Spending card's category name shows cents, which
    left \"Rent & Utilities\" and \"Other Transport\" an ellipsis at 360. The name now wraps; nothing on the row is cut."""
    path = tmp_path / "synthetic.sqlite"
    with db.connect(path) as conn:
        demo_data.build(conn, today=TODAY)
    conn.close()
    server = Server(path, today=TODAY)
    try:
        with Chrome() as b:
            b.pin_clock(TODAY)
            b.size(360, 800)
            b.goto(f"{server.url}/#/reports?view=spending")
            b.settled()
            clipped = b.js("""[...document.querySelectorAll('.rp-cat-b > b')].filter((e) => e.scrollWidth > e.clientWidth + 0.5
                || getComputedStyle(e).textOverflow === 'ellipsis').map((e) => e.textContent)""")
            seen = b.js("document.querySelectorAll('.rp-cat-b > b').length")
            assert seen > 0 and clipped == []
    finally:
        server.close()
