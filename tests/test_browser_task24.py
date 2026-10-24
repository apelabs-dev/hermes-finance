"""TASK-24 in headless Chrome (tests/cdp.py): Settings' "Categorization rules" card, on the gate's "full" DB with
/api/rules answered in the page (a stub built to the API contract in TASK-24-rules.md, so the card is tested on its own;
the POSTs it sends are recorded and checked). Collapsed to one summary line; open: suggestions (3 shown, "+N more"),
search + All / Yours / AI, dense one-line rows (desktop icon actions, a phone's one ⋯ menu, 44px), 20 shown then
"Show all N", the edit sheet, the inline delete confirm and its Undo toast. No JS errors, no sideways scroll."""

from __future__ import annotations

import json
import time
from datetime import date

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()
T = "var T = (el) => (el ? el.textContent.replace(/\\s+/g, ' ').trim() : null);"


def _rule(i, source, pattern, category, field="merchant_name", typ="exact", matches=3):
    label = f"Merchant is {pattern}" if typ == "exact" else f"Name contains {pattern}"
    return {"id": i, "field": field, "type": typ, "pattern": pattern, "label": label, "category": category, "source": source,
            "matches": matches, "created_at": f"2026-09-{(i % 27) + 1:02d}T10:00:00Z"}


RULES = ([_rule(100, "user", "Xai", "Subscriptions", matches=4), _rule(99, "user", "GEICO", "Other Transport", matches=1)]
         + [_rule(98 - k, "ai", f"Merchant{k:02d}", "Other Shopping" if k % 2 else "Eating Out", matches=k) for k in range(28)]
         + [_rule(50, "ai", "OPENROUTER", "Subscriptions", field="name", typ="regex", matches=12)])
SUGS = [{"key": f"m{k}", "merchant": m, "category": c, "times": t, "text": f"You filed {m} under {c} {t} times. Always do that?"}
        for k, (m, c, t) in enumerate([("Trader Joe's", "Groceries", 3), ("Blue Bottle", "Eating Out", 2),
                                       ("Uber", "Other Transport", 4), ("Steam", "Entertainment", 2)])]
DATA = {"rules": RULES, "counts": {"total": len(RULES), "user": 2, "ai": len(RULES) - 2}, "suggestions": SUGS}

# the stub: GET /api/rules answers DATA (as changed by the POSTs); every POST to /api/rules/* is recorded in __posts
STUB = """((data) => { window.__posts = []; const f = window.fetch;
  const J = (b, s = 200) => Promise.resolve(new Response(JSON.stringify(b), {status: s, headers: {'Content-Type': 'application/json'}}));
  window.fetch = (u, o = {}) => { const url = String(u);
    if (!url.includes('/api/rules')) return f(u, o);
    if (!o.method || o.method === 'GET') return J(data);
    const b = JSON.parse(o.body || '{}'); window.__posts.push([url.replace(location.origin, ''), b, (o.headers || {})['X-CSRF-Token'] || null]);
    const r = data.rules.find((x) => x.id === b.id);
    if (url.endsWith('/update')) { Object.assign(r, {category: b.category}); return J({rule: r, refiled: 3}); }
    if (url.endsWith('/keep')) { r.source = 'user'; return J({rule: r}); }
    if (url.endsWith('/delete')) { data.rules = data.rules.filter((x) => x !== r); data.counts.total -= 1; window.__gone = r;
      return J({deleted: r.id, rule: r, filed: [], undo: {receipt: 'receipt-' + r.id}}); }     // TASK-24 fix: a receipt only
    if (url.endsWith('/restore')) { const g = window.__gone; if (!b.undo || b.undo.receipt !== 'receipt-' + g.id) return J({error: 'no'}, 400);
      data.rules.unshift(g); data.counts.total += 1; return J({rule: g, relinked: 0}); }
    if (url.endsWith('/accept') || url.endsWith('/dismiss')) { data.suggestions = data.suggestions.filter((s) => s.key !== b.key);
      return url.endsWith('/accept') ? J({rule: data.rules[0], refiled: 2}) : J({dismissed: true}); }
    return J({error: 'no'}, 404); };
  location.hash = '#/settings?r=' + Date.now(); return 1; })"""


def wait(ms):
    return f"new Promise((r) => setTimeout(r, {ms}))"


def until(sel, ms=5000):
    return (f"new Promise((res, rej) => {{ const t0 = Date.now(); const tick = () => {{ const e = [...document.querySelectorAll("
            f"{json.dumps(sel)})].find((x) => x.getClientRects().length); if (e) res(true); else if (Date.now() - t0 > {ms}) "
            f"rej(new Error('never appeared: ' + {json.dumps(sel)})); else setTimeout(tick, 50); }}; tick(); }})")


def click(sel, text=None):
    return (f"(() => {{ const els = [...document.querySelectorAll({json.dumps(sel)})].filter((e) => e.getClientRects().length); "
            f"const el = els.find((e) => {json.dumps(text)} === null || e.textContent.includes({json.dumps(text)}) "
            f"|| (e.getAttribute('aria-label') || '').includes({json.dumps(text)})); "
            f"if (!el) throw new Error('nothing to click: ' + {json.dumps(sel)} + ' ' + {json.dumps(text)}); el.click(); return 1; }})()")


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("browser24") / "finance.db"
    gate_dbs.build("full", path, TODAY)
    srv = Server(path, today=TODAY)
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def open_card(b, site, width, data=DATA, expand=True):
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{site.url}/?t={time.time():.3f}#/overview")
    b.settle()
    b.js(f"{STUB}({json.dumps(data)})")
    b.js(until("#page .sc.rules .rsum"))
    b.js(wait(300))
    if expand:
        b.js(click("#page .sc.rules .rsum"))
        b.js(until("#page .sc.rules .rrow"))
        b.js(wait(150))


def clean(b):
    assert b.problems() == []
    assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
    # nothing in the card sticks out of it
    assert b.js("""(() => { const c = document.querySelector('#page .sc.rules').getBoundingClientRect();
      return [...document.querySelectorAll('#page .sc.rules *')].filter((e) => e.getClientRects().length).every((e) => {
        const r = e.getBoundingClientRect(); return r.left >= c.left - 1 && r.right <= c.right + 1; }); })()""")


@pytest.mark.parametrize("width", [1440, 390])
def test_collapsed_to_one_summary_line(site, chrome, width):
    open_card(chrome, site, width, expand=False)
    got = chrome.js(T + """(() => { const c = document.querySelector('#page .sc.rules');
      return {sum: T(c.querySelector('.rsum')), exp: c.querySelector('.rsum').getAttribute('aria-expanded'),
        rows: c.querySelectorAll('.rrow').length, last: c === document.querySelector('#page .sgrid').lastElementChild,
        title: T(c.querySelector('h3 .ttl'))}; })()""")
    assert got == {"sum": "31 rules · 2 yours · 4 suggestions", "exp": "false", "rows": 0, "last": True,
                   "title": "Categorization rules"}
    clean(chrome)


@pytest.mark.parametrize("width", [1440, 390])
def test_open_list_suggestions_groups_and_show_all(site, chrome, width):
    open_card(chrome, site, width)
    got = chrome.js(T + """(() => { const c = document.querySelector('#page .sc.rules');
      return {sugs: [...c.querySelectorAll('.rrow.sug')].map((r) => T(r.querySelector('.rst'))),
        more: T(c.querySelector('.rmoresug')), gh: [...c.querySelectorAll('.rlist .gh')].map(T),
        rows: c.querySelectorAll('.rlist .rrow').length, all: T(c.querySelector('.rall')),
        first: T(c.querySelector('.rlist .rrow .rpat')), bold: T(c.querySelector('.rlist .rrow .rpat b')),
        seg: [...c.querySelectorAll('.rseg button')].map(T), pressed: T(c.querySelector('.rseg [aria-pressed=true]'))}; })()""")
    assert got["sugs"] == ["You filed Trader Joe's under Groceries 3 times. Always do that?",
                           "You filed Blue Bottle under Eating Out 2 times. Always do that?",
                           "You filed Uber under Other Transport 4 times. Always do that?"]
    assert got["more"] == "+1 more" and got["gh"] == ["Yours · 2", "AI · 29"]
    assert got["rows"] == 20 and got["all"] == "Show all 31"
    assert (got["first"], got["bold"]) == ("Merchant is Xai", "Xai")
    assert got["seg"] == ["All", "Yours", "AI"] and got["pressed"] == "All"
    chrome.js(click("#page .sc.rules .rall"))
    chrome.js(wait(150))
    assert chrome.js("document.querySelectorAll('#page .sc.rules .rlist .rrow').length") == 31
    rows = chrome.js("[...document.querySelectorAll('#page .sc.rules .rlist .rrow')].map((r) => Math.round(r.getBoundingClientRect().height))")
    if width == 1440:  # dense: one line, ~40px, icon actions with labels on hover / aria
        assert max(rows) <= 44
        acts = chrome.js("[...document.querySelectorAll('#page .sc.rules .rrow[data-source=ai] .racts button')].slice(0, 3).map((b) => b.getAttribute('aria-label'))")
        assert acts == ["Edit", "Keep as yours", "Delete"]
        assert chrome.js("getComputedStyle(document.querySelector('#page .sc.rules .rmore')).display") == "none"
    else:              # a phone: one ⋯ per row, 44px; the category on the second line
        sizes = chrome.js("[...document.querySelectorAll('#page .sc.rules .rmore')].map((b) => { const r = b.getBoundingClientRect(); return [r.width, r.height]; })")
        assert len(sizes) == 31 and all(w >= 44 and hh >= 44 for w, hh in sizes)
        assert chrome.js("getComputedStyle(document.querySelector('#page .sc.rules .racts')).display") == "none"
        assert chrome.js(T + "T(document.querySelector('#page .sc.rules .rlist .rrow .rsub'))") == "→ Subscriptions · 4 matches"
    clean(chrome)


def test_search_and_filter_as_you_type(site, chrome):
    open_card(chrome, site, 1440)
    chrome.js("(() => { const i = document.querySelector('#page .sc.rules .rq'); i.value = 'subscr'; i.dispatchEvent(new Event('input')); return 1; })()")
    chrome.js(wait(100))
    pats = chrome.js("[...document.querySelectorAll('#page .sc.rules .rlist .rrow .rpat')].map((e) => e.textContent)")
    assert pats == ["Merchant is Xai", "Name contains OPENROUTER"]
    chrome.js(click("#page .sc.rules .rseg button", "AI"))
    chrome.js(wait(100))
    assert chrome.js("[...document.querySelectorAll('#page .sc.rules .rlist .rrow .rpat')].map((e) => e.textContent)") == ["Name contains OPENROUTER"]
    assert chrome.js("document.querySelector('#page .sc.rules .rq').value") == "subscr"
    chrome.js("(() => { const i = document.querySelector('#page .sc.rules .rq'); i.value = 'zzz'; i.dispatchEvent(new Event('input')); return 1; })()")
    chrome.js(wait(100))
    assert chrome.js("document.querySelector('#page .sc.rules .rnone').textContent") == "No rules match."
    clean(chrome)
    # the state is the page's own: put it back for the next tests
    chrome.js("(() => { const i = document.querySelector('#page .sc.rules .rq'); i.value = ''; i.dispatchEvent(new Event('input')); return 1; })()")
    chrome.js(click("#page .sc.rules .rseg button", "All"))


@pytest.mark.parametrize("width", [1440, 390])
def test_edit_sheet_saves_category_and_toasts_the_refiled_count(site, chrome, width):
    open_card(chrome, site, width, data=json.loads(json.dumps(DATA)))
    if width == 1440:
        chrome.js(click("#page .sc.rules .rrow[data-rule='100'] .rib", "Edit"))
    else:
        chrome.js(click("#page .sc.rules .rrow[data-rule='100'] .rmore"))
        chrome.js(until("#page .sc.rules .rrow[data-rule='100'] .rmenu"))
        assert chrome.js(T + "[...document.querySelectorAll('#page .rrow[data-rule=\"100\"] .rmenu button')].map(T)") == ["Edit", "Delete"]
        chrome.js(click("#page .sc.rules .rrow[data-rule='100'] .rmenu button", "Edit"))
    chrome.js(until("#sheet.rsheet select"))
    sheet = chrome.js(T + """({h: T(document.querySelector('#sheet h2')), lead: T(document.querySelector('#sheet .rlead')),
      pat: document.querySelector('#sheet input') && document.querySelector('#sheet input').value,
      cat: document.querySelector('#sheet select').value, hint: T(document.querySelector('#sheet .hint'))})""")
    assert sheet["h"] == "Edit rule" and sheet["lead"] == "Merchant is Xai" and sheet["pat"] == "Xai" and sheet["cat"] == "Subscriptions"
    assert sheet["hint"].startswith("Made by you ·") and "matches 4 transactions now" in sheet["hint"]
    chrome.js("(() => { const s = document.querySelector('#sheet select'); s.value = 'Other Shopping'; s.dispatchEvent(new Event('change')); return 1; })()")
    chrome.js(click("#sheet .row .btn", "Save"))
    chrome.js(until("#toast.on"))
    assert "3 charges re-filed" in chrome.js(T + "T(document.querySelector('#toast'))")
    posts = chrome.js("window.__posts")
    # the header is the page's CSRF token (none here: this DB has no built-in login; test_browser_task24_real checks one)
    csrf = chrome.js("(document.querySelector('meta[name=csrf-token]') || {}).content || null")
    assert posts[0][0] == "/api/rules/update" and posts[0][1] == {"id": 100, "category": "Other Shopping"} and posts[0][2] == csrf
    chrome.js(wait(200))
    assert chrome.js(T + "T(document.querySelector('#page .rrow[data-rule=\"100\"] .rsub'))") == "→ Other Shopping · 4 matches"
    clean(chrome)


def test_ai_rule_edit_has_no_pattern_field_and_keep_promotes_it(site, chrome):
    open_card(chrome, site, 1440, data=json.loads(json.dumps(DATA)))
    chrome.js(click("#page .sc.rules .rall"))
    chrome.js(click("#page .sc.rules .rrow[data-rule='50'] .rib", "Edit"))
    chrome.js(until("#sheet.rsheet select"))
    assert chrome.js("document.querySelectorAll('#sheet input').length") == 0
    assert chrome.js(T + "T(document.querySelector('#sheet .hint'))").startswith("Made by AI ·")
    chrome.js(click("#sheet .row .btn", "Cancel"))
    chrome.js(click("#page .sc.rules .rrow[data-rule='50'] .rib", "Keep as yours"))
    chrome.js(until("#toast.on"))
    assert chrome.js("window.__posts")[-1][:2] == ["/api/rules/keep", {"id": 50}]
    chrome.js(wait(200))
    assert chrome.js("document.querySelector('#page .rrow[data-rule=\"50\"]').dataset.source") == "user"
    clean(chrome)


@pytest.mark.parametrize("width", [1440, 390])
def test_delete_confirms_inline_then_undo_restores(site, chrome, width):
    open_card(chrome, site, width, data=json.loads(json.dumps(DATA)))
    if width == 1440:
        chrome.js(click("#page .sc.rules .rrow[data-rule='99'] .rib", "Delete"))
    else:
        chrome.js(click("#page .sc.rules .rrow[data-rule='99'] .rmore"))
        chrome.js(click("#page .sc.rules .rrow[data-rule='99'] .rmenu button", "Delete"))
    chrome.js(until("#page .rrow.confirm"))
    assert chrome.js(T + "[...document.querySelector('#page .rrow.confirm').children].map((c) => [...c.children].length > 1 ? [...c.children].map(T).join(' | ') : T(c)).join(' · ')") \
        == "Delete? Filed charges stay. · Delete | Cancel"
    assert chrome.js("document.querySelector('#modal').classList.contains('on')") is False     # inline, not a modal
    assert chrome.js("window.__posts.length") == 0
    clean(chrome)
    chrome.js(click("#page .rrow.confirm .btn", "Delete"))
    chrome.js(until("#toast.on"))
    assert "Filed charges stay as they are" in chrome.js(T + "T(document.querySelector('#toast'))")
    chrome.js(wait(200))
    assert chrome.js("document.querySelectorAll('#page .rrow[data-rule=\"99\"]').length") == 0
    chrome.js(click("#toast button", "Undo"))
    chrome.js(wait(300))
    posts = chrome.js("window.__posts")
    assert [p[0] for p in posts] == ["/api/rules/delete", "/api/rules/restore"]
    assert posts[1][1] == {"undo": {"receipt": "receipt-99"}}              # the delete's receipt, sent back as it came
    assert chrome.js("document.querySelectorAll('#page .rrow[data-rule=\"99\"]').length") == 1
    clean(chrome)


def test_suggestion_add_rule_and_dismiss(site, chrome):
    open_card(chrome, site, 1440, data=json.loads(json.dumps(DATA)))
    chrome.js(click("#page .rrow.sug[data-sug=m0] .btn", "Add rule"))
    chrome.js(until("#toast.on"))
    chrome.js(wait(200))
    chrome.js(click("#page .rrow.sug[data-sug=m1] .btn", "Dismiss"))
    chrome.js(wait(300))
    assert [p[:2] for p in chrome.js("window.__posts")] == [["/api/rules/suggestions/accept", {"key": "m0"}],
                                                             ["/api/rules/suggestions/dismiss", {"key": "m1"}]]
    assert chrome.js("[...document.querySelectorAll('#page .rrow.sug')].map((r) => r.dataset.sug)") == ["m2", "m3"]
    assert chrome.js("document.querySelector('#page .rmoresug')") is None
    assert "2 suggestions" in chrome.js(T + "T(document.querySelector('#page .sc.rules .rsum'))")
    clean(chrome)


def test_no_suggestions_hides_the_block(site, chrome):
    open_card(chrome, site, 1440, data={**DATA, "suggestions": []})
    assert chrome.js("document.querySelectorAll('#page .sc.rules .rsugs').length") == 0
    assert chrome.js(T + "T(document.querySelector('#page .sc.rules .rsum'))") == "31 rules · 2 yours"
    clean(chrome)
