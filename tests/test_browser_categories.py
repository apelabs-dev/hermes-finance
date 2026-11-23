"""v23 Categories, in a real headless Chrome: the Settings card above Categorization rules opening and closing in place,
drawn as the approved frame (its head: Search categories and New category on one line, the summary "N categories · N
yours · N hidden"; group headers "Food · 5"; one dense line per category at 1280 with its transaction count and
icon-only actions, the count under the name and one ⋯ at 390; a Yours tag only on yours, no Built-in tag; the first rows
then "Show all N"; search filtering live), a user category created with a non-default icon from the shared picker (16
tiles closed on a desktop, 12 on a phone; the open list scrolls and searches; Show fewer icons closes it; the selected
ring is fully inside the scroller), the icon drawn on the settings row, a transaction badge and the picker, the + New
category row, hide keeping the row in its group (greyed, Hidden, a Show button) and the month's total alone, and delete
confirming in the row where the transactions go. At 390 and 1280, dark and light."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from hermes_finance import catops, classify, db, insights

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")


def tap(b, selector):
    box = b.js(f"""(() => {{const el=document.querySelector({json.dumps(selector)});
        el.scrollIntoView({{block:'center'}}); const r=el.getBoundingClientRect();
        return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()""")
    b.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, **box)
    b.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, **box)
    b.settle()


def clear_toast(b):
    # the toast sits above the sheet on a phone (its own z-index); a confirmation from the last action must not
    # intercept a tap on the sheet's controls
    b.js("(() => {const t=document.querySelector('#toast'); if (t) t.className='toast';})()")


def set_input(b, selector, value):
    b.js(f"""(() => {{const el=document.querySelector({json.dumps(selector)}); el.value={json.dumps(value)};
        el.dispatchEvent(new Event('input', {{bubbles: true}})); el.dispatchEvent(new Event('change', {{bubbles: true}}));}})()""")
    b.settle()


def visible_count(b, selector):
    return b.js(f"[...document.querySelectorAll({json.dumps(selector)})].filter((el) => el.offsetParent !== null).length")


def wait_js(b, expr, ms=4000):
    return b.js(f"new Promise((res) => {{const t0=Date.now(); const tick=() => {{ if (({expr})) res(true); "
                f"else if (Date.now()-t0>{ms}) res(false); else setTimeout(tick, 50); }}; tick(); }})")


def icon_box(b, selector):
    return b.js(f"""(() => {{const s=document.querySelector({json.dumps(selector)});
        if (!s) return null; const r=s.getBBox(); return {{w: r.width, h: r.height}};}})()""")


def act(b, width, name, what):
    """A row's action: its icon button on a wide screen, its ⋯ menu on a phone."""
    row = f'.catrow[data-cat="{name}"]'
    if width >= 1024:
        tap(b, f'{row} .racts [data-act="{what}"]')
    else:
        tap(b, f"{row} .rmore")
        assert b.js(f"document.querySelector('{row}').classList.contains('rmenu-open')")
        tap(b, f'{row} .rmenu [data-act="{what}"]')


def card_frame(b, width, payload):
    """The open card against the D3 frame: head, summary, group headers, rows, tags, counts, actions, footer."""
    counts = payload["counts"]
    assert b.js("document.querySelector('.cats .rsumt').textContent") == \
        f"{counts['total']} categories · {counts['user']} yours · {counts['hidden']} hidden"
    head = b.js("""(() => {const q=document.querySelector('.cats .rbar .search.rq'), n=document.querySelector('.cats .rbar .catnew');
        const a=q.getBoundingClientRect(), c=n.getBoundingClientRect();
        return [q.placeholder, n.textContent.trim(), Math.abs((a.top+a.bottom)/2-(c.top+c.bottom)/2) <= 2, a.right <= c.left];})()""")
    assert head == ["Search categories", "New category", True, True], head
    text = b.js("document.querySelector('.cats').textContent")
    assert "Built-in" not in text and "Show hidden" not in text
    assert not b.js("Boolean(document.querySelector('.cats .tgl'))")
    first = payload["groups"][0]
    assert b.js("document.querySelector('.cats .rlist .gh').textContent") == f"{first['name']} · {len(first['categories'])}"
    assert b.js("document.querySelectorAll('.cats .catrow').length") == 10
    assert b.js("document.querySelector('.cats .rall').textContent") == f"Show all {counts['total']}"
    shown = b.js("[...document.querySelectorAll('.cats .catrow')].map((r) => r.dataset.cat)")
    by_name = {c["name"]: c for c in payload["categories"]}
    for name in shown:
        n = by_name[name]["transactions"]
        word = f"{n} transaction" + ("" if n == 1 else "s")
        sel = f'.catrow[data-cat="{name}"]'
        assert b.js(f"document.querySelector('{sel} .rn').textContent") == word
        assert b.js(f"document.querySelector('{sel} .rsub').textContent") == word
    rows = b.js("""[...document.querySelectorAll('.cats .catrow')].map((r) => {const v=(s) => {const e=r.querySelector(s);
        return Boolean(e) && e.offsetParent !== null;};
        return {h: r.getBoundingClientRect().height, rn: v('.rn'), sub: v('.rsub'), acts: v('.racts'), more: v('.rmore'),
          labelled: [...r.querySelectorAll('.racts .rib')].every((x) => x.getAttribute('aria-label') && !x.textContent.trim()),
          ribs: [...r.querySelectorAll('.racts .rib')].map((x) => x.dataset.act).join(','),
          tags: [...r.querySelectorAll('.rpat .tag')].map((x) => x.textContent).join(',')};})""")
    for r in rows:
        assert r["labelled"] and r["tags"] == "", r
        assert r["ribs"] == "edit,hide", r                       # built-ins: pencil and eye-off, no trash
        if width >= 1024:
            assert (r["rn"], r["sub"], r["acts"], r["more"]) == (True, False, True, False), r
            assert r["h"] <= 44, r                              # one dense line
        else:
            assert (r["rn"], r["sub"], r["acts"], r["more"]) == (False, True, False, True), r
    if width < 1024:
        assert b.js("document.querySelector('.cats .catrow .rmore').getBoundingClientRect().width") >= 44
    # search filters live by name, case-insensitively, through every group (not only the first page); a group with no
    # match disappears, and nothing is capped
    set_input(b, ".cats .rbar .search.rq", "COF")
    assert b.js("[...document.querySelectorAll('.cats .catrow')].map((r) => r.dataset.cat)") == ["Coffee"]
    assert b.js("[...document.querySelectorAll('.cats .rlist .gh')].map((g) => g.textContent)") == ["Food · 1"]
    assert not b.js("Boolean(document.querySelector('.cats .rall'))")
    set_input(b, ".cats .rbar .search.rq", "zzz no such")
    assert b.js("document.querySelector('.cats .rnone').textContent") == "No categories match."
    set_input(b, ".cats .rbar .search.rq", "")
    assert b.js("document.querySelectorAll('.cats .catrow').length") == 10


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_categories_end_to_end(tmp_path, width, theme):
    today = date.today()
    path = tmp_path / "synthetic.sqlite"
    conn = db.connect(path)
    demo_data.build(conn, today=today)
    # a merchant with at least two live rows, so the page shows a second row whose picker must not offer the hidden one
    merchant = conn.execute(
        "SELECT merchant_name FROM transactions WHERE removed = 0 AND pending = 0 AND merchant_name IS NOT NULL "
        "GROUP BY merchant_name ORDER BY COUNT(*) DESC, merchant_name LIMIT 1").fetchone()[0]
    tid = conn.execute("SELECT transaction_id FROM transactions WHERE merchant_name = ? AND removed = 0 "
                       "AND pending = 0 ORDER BY date DESC, transaction_id LIMIT 1", (merchant,)).fetchone()[0]
    payload = catops.payload(conn, today)
    conn.close()
    query = quote_plus(merchant)
    server = Server(path, today=today)
    try:
        with Chrome() as b:
            b.size(width, 900 if width == 1280 else 844, scale=1)
            b.goto(f"{server.url}/#/settings")
            b.js(f"window.HFTheme.set({json.dumps(theme)})")
            b.settle()
            assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
            # the card sits above Categorization rules, both in the same page grid
            order = b.js("""(() => {const g=document.querySelector('#page .sgrid');
                const cats=g.querySelector('.card.cats'), rules=g.querySelector('.card.rules');
                return [Boolean(cats), Boolean(rules), cats.compareDocumentPosition(rules) & Node.DOCUMENT_POSITION_FOLLOWING ? 1 : 0];})()""")
            assert order == [True, True, 1], order
            # open in place: no navigation, the body appears; close hides it again
            tap(b, ".cats .rsum")
            assert b.js("document.querySelector('#categories-body').hidden") is False
            assert b.js("location.hash").endswith("#/settings")
            tap(b, ".cats .rsum")
            assert b.js("document.querySelector('#categories-body').hidden") is True
            tap(b, ".cats .rsum")
            b.settle()
            card_frame(b, width, payload)
            assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
            # New category: name, group, and an icon from the open list's search (rocket is not in the common two rows)
            tap(b, ".cats .catnew")
            assert b.js("document.querySelector('#sheet .shd h2').textContent") == "New category"
            groups = b.js("[...document.querySelectorAll('#sheet select option')].map((o) => o.value)")
            assert groups == [g["name"] for g in payload["groups"] if g["rgb"]] and len(groups) == 8, groups
            set_input(b, "#sheet .fld input", "Astronomy")
            b.js("""(() => {const s=document.querySelector('#sheet select'); s.value='Leisure';
                s.dispatchEvent(new Event('change', {bubbles:true}));})()""")
            b.settle()
            expected = 16 if width > 751 else 12
            assert visible_count(b, "#sheet .ipk-quick .gicon") == expected
            tap(b, "#sheet .ipk-toggle")
            assert b.js("document.querySelector('#sheet .ipk-open').hidden") is False
            assert b.js("document.querySelector('#sheet .ipk-scroll') !== null")
            set_input(b, "#sheet .ipk-search", "rocket")
            assert b.js("document.querySelectorAll('#sheet .ipk-scroll .gicon').length") == 1
            assert b.js("document.querySelector('#sheet .ipk-count')") is None   # no "N icons" count beside the search
            tap(b, "#sheet .ipk-scroll .gicon[aria-label='rocket']")
            assert b.js("document.querySelector('#sheet .gicon[aria-label=rocket]').getAttribute('aria-pressed')") == "true"
            tap(b, "#sheet .row .btn:not(.ghost)")
            b.settle()
            clear_toast(b)
            # a new category is shown at once (the list opens past its first page) with the Yours tag, the only one
            assert wait_js(b, "Boolean(document.querySelector('.catrow[data-cat=Astronomy]'))"), \
                [c["name"] for c in server.api("/api/categories")][-5:]
            assert b.js("[...document.querySelectorAll('.cats .catrow')].filter((r) => r.querySelector('.rpat .tag')).map((r) => r.dataset.cat)") \
                == ["Astronomy"]
            assert b.js("document.querySelector('.catrow[data-cat=Astronomy] .rpat .tag').textContent") == "Yours"
            assert b.js("[...document.querySelectorAll('.catrow[data-cat=Astronomy] .racts .rib')].map((x) => x.dataset.act).join(',')") \
                == "edit,hide,delete"
            assert "1 yours" in b.js("document.querySelector('.cats .rsumt').textContent")
            # the settings row draws the stored icon: a real <svg> with non-zero ink
            box = icon_box(b, '.catrow[data-cat="Astronomy"] .cb svg')
            assert box and box["w"] > 0 and box["h"] > 0, box
            assert b.js("document.querySelector('.catrow[data-cat=Astronomy] .cb svg').dataset.icon") == "rocket"
            assert b.js("document.querySelector('.catrow[data-cat=Astronomy]').getBoundingClientRect().height") >= 36
            # edit: the closed picker has the same two rows, the list opens with search, and the selected ring is whole
            act(b, width, "Astronomy", "edit")
            b.settle()
            assert b.js("document.querySelector('#sheet .shd h2').textContent") == "Edit category"
            assert visible_count(b, "#sheet .ipk-quick .gicon") == expected
            tap(b, "#sheet .ipk-toggle")
            assert b.js("document.querySelector('#sheet .ipk-open').hidden") is False
            assert b.js("document.querySelector('#sheet .ipk-scroll') !== null")
            set_input(b, "#sheet .ipk-search", "")
            b.js("""(() => {const sc=document.querySelector('#sheet .ipk-scroll');
                sc.scrollTop=sc.scrollHeight; sc.querySelector('.gicon[aria-label=church]').click();})()""")
            b.settle()
            inside = b.js("""(() => {const t=document.querySelector('#sheet .gicon[aria-label=church]').getBoundingClientRect();
                const s=document.querySelector('#sheet .ipk-scroll').getBoundingClientRect();
                const sc=document.querySelector('#sheet .ipk-scroll');
                return [t.top >= s.top + 3, t.bottom <= s.bottom - 3, sc.scrollTop > 0];})()""")
            assert inside == [True, True, True], inside
            tap(b, "#sheet .ipk-toggle")
            assert b.js("document.querySelector('#sheet .ipk-open').hidden") is True
            assert "Show all 100 icons" in b.js("document.querySelector('#sheet .ipk-toggle').textContent")
            tap(b, "#sheet .row .btn:not(.ghost)")
            b.settle()
            clear_toast(b)
            assert wait_js(b, "document.querySelector('.catrow[data-cat=Astronomy] .cb svg')?.dataset.icon === 'church'")
            # file one transaction under it, then its row badge shows the same real icon
            conn = db.connect(path)
            classify.set_category(conn, tid, "Astronomy", "user")
            conn.close()
            spent = server.api("/api/overview")["summary"]["mtd"]
            b.goto(f"{server.url}/#/transactions?q={query}")
            b.settle()
            row = f'tr[data-tid="{tid}"]'
            assert b.js(f"document.querySelector('{row} .c-cat .cb svg').dataset.icon") == "church"
            assert icon_box(b, f"{row} .c-cat .cb svg")["w"] > 0
            # + New category: the last row of the category picker, aligned with the rows above
            other = b.js(f"[...document.querySelectorAll('tr[data-tid]')].map((r) => r.dataset.tid).find((x) => x !== {json.dumps(tid)})")
            assert other
            tap(b, f'tr[data-tid="{other}"] .c-cat .catb')
            b.settle()
            assert b.js("Boolean(document.querySelector('.cpk .cpkadd .cpko'))")
            aligned = b.js("""(() => {const rows=[...document.querySelectorAll('.cpk .cpkl .cpko')];
                const add=document.querySelector('.cpk .cpkadd .cpko');
                const others=rows.filter((r)=>r!==add);
                const a=add.getBoundingClientRect(), o=others[0].getBoundingClientRect();
                return [Math.abs(a.left-o.left) <= 1, [...document.querySelectorAll('.cpk .cpkl > .cpkg')].pop().contains(add)];})()""")
            assert aligned == [True, True], aligned
            set_input(b, ".cpk .cpkq", "No such category")
            assert b.js("document.querySelector('.cpk .cpkn').hidden") is False
            assert 'Create “No such category”' in b.js("document.querySelector('.cpk .cpkc').textContent")
            tap(b, ".cpk .cpkc")
            assert b.js("document.querySelector('#sheet .shd h2').textContent") == "New category"
            b.js("document.querySelector('#sheet .row .btn.ghost').click()")
            b.settle()
            assert b.js("document.querySelector('#modal').className") == "modal"
            # hide: the row stays in its group, greyed with a Hidden tag and a Show button; the summary counts it, the
            # picker stops offering it, and the month total stays
            b.goto(f"{server.url}/#/settings")
            b.settle()
            tap(b, ".cats .rsum")
            assert not b.js("Boolean(document.querySelector('.catrow[data-cat=Astronomy]'))")   # past the first page
            tap(b, ".cats .rall")
            assert wait_js(b, "Boolean(document.querySelector('.catrow[data-cat=Astronomy]'))")
            assert not b.js("Boolean(document.querySelector('.cats .rall'))")
            assert b.js("document.querySelectorAll('.cats .catrow').length") == payload["counts"]["total"] + 1
            act(b, width, "Astronomy", "hide")
            b.settle()
            clear_toast(b)
            assert wait_js(b, "Boolean(document.querySelector('.catrow[data-cat=Astronomy].off'))")
            assert b.js("[...document.querySelectorAll('.catrow[data-cat=Astronomy] .rpat .tag')].map((t) => t.textContent)") \
                == ["Yours", "Hidden"]
            assert b.js("document.querySelector('.catrow[data-cat=Astronomy]').closest('.rlist') !== null")
            assert "1 hidden" in b.js("document.querySelector('.cats .rsumt').textContent")
            if width >= 1024:
                assert b.js("document.querySelector('.catrow[data-cat=Astronomy] .racts .catshow').textContent") == "Show"
                assert not b.js("Boolean(document.querySelector('.catrow[data-cat=Astronomy] .racts [data-act=hide]'))")
            assert server.api("/api/overview")["summary"]["mtd"] == spent
            b.goto(f"{server.url}/#/transactions?q={query}")
            b.settle()
            tap(b, f'tr[data-tid="{other}"] .c-cat .catb')
            b.settle()
            assert not b.js("Boolean(document.querySelector('.cpk [data-cat=Astronomy]'))")
            # delete confirms inline in the row: how many transactions move, and where
            b.goto(f"{server.url}/#/settings")
            b.settle()
            tap(b, ".cats .rsum")
            set_input(b, ".cats .rbar .search.rq", "astro")
            assert b.js("[...document.querySelectorAll('.cats .catrow')].map((r) => r.dataset.cat)") == ["Astronomy"]
            clear_toast(b)
            act(b, width, "Astronomy", "delete")
            assert b.js("document.querySelector('#modal').className") == "modal"   # no sheet: the row asks
            ask = '.rrow.confirm.catask[data-cat="Astronomy"]'
            assert b.js(f"document.querySelector('{ask} .grow').textContent") == "Delete Astronomy? Its 1 transaction moves to"
            # the destination starts on a sibling in its own group; on a wide screen the question is one line
            leisure = {c["name"] for c in payload["categories"] if c["group"] == "Leisure" and not c["hidden"]}
            assert b.js(f"document.querySelector('{ask} select').value") in leisure
            if width >= 1024:
                assert b.js(f"document.querySelector('{ask}').getBoundingClientRect().height") <= 60
            b.js(f"""(() => {{const s=document.querySelector('{ask} select'); s.value='Other';
                s.dispatchEvent(new Event('change', {{bubbles:true}}));}})()""")
            assert b.js(f"document.querySelector('{ask} .btn.danger').textContent") == "Move and delete"
            assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
            tap(b, f"{ask} .btn.danger")
            b.settle()
            assert wait_js(b, "!document.querySelector('[data-cat=Astronomy]')")
            assert server.api("/api/overview")["summary"]["mtd"] == spent
            assert "0 yours" in b.js("document.querySelector('.cats .rsumt').textContent")
            assert b.problems() == []
    finally:
        server.close()
