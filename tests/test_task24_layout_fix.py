"""TASK-24: the two layout FAILs gate 2 found on the full DB on 2026-09-30 (no browser here: the pages themselves are the
layout gate's, tests/test_gates.py, and test_layout_gate_task18's recurring 1280 case), their data, and the CSS that
answers them.

  1280 recurring   the full DB's Namecheap is charged every Oct 1, so on Sep 30 it is due tomorrow: the "Due tomorrow"
                   pill (wider than the amount's 88px) left the narrow Subscriptions row's name column 82px, and
                   "Namecheap" and "Subscriptions" were cut. Now the column keeps 6em, and a row whose pill leaves it
                   less puts the amount and its pill under the name, on the right.
  1024 transactions (and its sheets) the title, "September 2026", "Synced Sep 29, 11:42 PM" (demo_data's last sync is
                   42 minutes before the build: a run just after midnight shows yesterday's date) and Sync now were
                   1px wider than the window. Now from 1024 to 1199px the status goes under the title, as on a phone.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import gate_dbs

from hermes_finance import api, db, web

STATIC = Path(web.__file__).parent / "static"
CSS = (STATIC / "app.css").read_text()
JS = (STATIC / "app.js").read_text()
NARROW = ".li.wrap:has(> .rt:not(.bill) > .pill)"
LAPTOP = "@media (min-width: 1024px) and (max-width: 1199.98px)"
PHONE = "@media (max-width: 1023.98px)"


def _plain(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _blocks(css: str, query: str) -> list[str]:
    """The bodies of every `query { ... }` block (braces matched; comments dropped first)."""
    css, out, at = _plain(css), [], 0
    while (at := css.find(query + " {", at)) >= 0:
        i = start = css.index("{", at) + 1
        depth = 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[start:i - 1])
        at = i
    return out


def _top_level(css: str, rule: str) -> bool:
    css = _plain(css)
    i = css.index(rule)
    return css.count("{", 0, i) == css.count("}", 0, i)


def test_on_sep_30_the_full_db_has_a_subscription_due_tomorrow(tmp_path):
    """The data behind the 1280 FAIL: the pill is "Due tomorrow" (recPill), not a Paid / Next one."""
    today = date(2026, 9, 30)
    path = gate_dbs.build("full", tmp_path / "finance.db", today)
    conn = db.connect(path)
    try:
        items = api.recurring_page(conn, {}, today)["items"]
    finally:
        conn.close()
    nc = next(r for r in items if r["merchant"] == "Namecheap")
    assert nc["kind"] == "subscription" and nc["status"] == "due_soon" and nc["days_until"] == 1
    assert not (nc["paid_this_month"] and nc["cadence"] != "weekly")
    assert "k === 1 ? 'Due tomorrow'" in JS
    # the narrow card's row: the amount (min-width 88px) with its pill under it, in .rt beside the name
    assert "narrow ? h('span', {class: 'rt'}, amount, recPill(r))" in JS and "'min-width': '88px'" in JS


def test_a_narrow_subscriptions_row_wraps_its_amount_under_the_name_rather_than_cut_it():
    for rule in (f"{NARROW} {{ flex-wrap: wrap; row-gap: 6px; }}", f"{NARROW} > .grow {{ flex-basis: 6em; }}",
                 f"{NARROW} > .rt {{ margin-left: auto; }}"):
        assert rule in CSS and _top_level(CSS, rule), rule   # every width: the card is narrow at 1280 and 1440
    # only the Subscriptions card's rows (.rt): a Bills row's .rt.bill is display: contents on wider screens
    assert ".rt.bill { display: contents; }" in CSS
    # the name column still shrinks to nothing when it must (no min-width: auto): a word longer than its box is still
    # cut, and the gate still sees it (test_layout_gate_task18 re-shows the category badge and must get a cliptext FAIL).
    # (integration) The Recurring rows also keep baseline K1's floor (.li.wrap[data-bill] > .grow { min-width:
    # min-content }); test_layout_gate_task18's recurring case takes that out along with the badge
    assert ".grow { flex: 1; min-width: 0; }" in CSS and "> .grow { min-width: auto" not in CSS
    assert f"{NARROW} .grow > span > .cb {{ display: none; }}" in CSS   # TASK-18's fix stays


def test_from_1024_to_1199_the_sync_status_goes_under_the_title():
    laptop = "".join(_blocks(CSS, LAPTOP))
    for rule in (".ttlw { display: flex; flex-direction: column; min-width: 0; flex: 0 1 auto; }",
                 ".ttlw h1 { line-height: 1.2; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }",
                 ".ttlw .syncst { display: flex;", ".syncst .dot { background: var(--dim); }",
                 ".syncst .dot.ok { background: var(--ok); }", ".syncst .dot.bad { background: var(--bad); }",
                 ".top #syncbox { display: none; }"):
        assert rule in laptop, rule
    assert ".sp" not in laptop                      # the spacer stays: the month beside the title, Sync now on the right
    assert ".syncbtn" not in laptop                 # "Sync now", 38px, as on a desktop
    # a desktop keeps the status beside the button (test_task22f_sync_ui at 1440); a phone as before
    assert ".ttlw { display: contents; }" in CSS and ".syncst { display: none; }" in CSS
    assert CSS.index(".syncst { display: none; }") < CSS.index(LAPTOP + " {\n  .grid")  # the laptop block wins
    phone = "".join(_blocks(CSS, PHONE))
    assert ".ttlw { display: flex; flex-direction: column; min-width: 0; flex: 1 1 auto; }" in phone
    assert ".top #syncbox, .top > .sp { display: none; }" in phone
    # the copy under the title is the header status itself
    assert "$('sync2').textContent = $('sync').textContent;" in JS
