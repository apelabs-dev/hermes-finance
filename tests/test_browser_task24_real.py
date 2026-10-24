"""TASK-24 against the real server: Settings' "Categorization rules" card on tests/rules_scene.py's synthetic DB (5 rules,
3 AI and 2 yours, 2 suggestions, one of them an AI override), no stub. Every change the card makes is checked in the DB:
an edit re-files matching rows but never a hand-filed one; Keep makes an AI rule yours; Delete leaves filed rows as they
are and Undo brings the rule back; Add rule / Dismiss; and with the built-in login the card's POSTs carry the page's CSRF
token (a wrong one is refused, the card says so and nothing changes)."""

from __future__ import annotations

import json
import time
from datetime import date

import pytest

import gate_dbs
import rules_scene
from cdp import Chrome, Server, find_chrome
from hermes_finance import auth, db, prefs, rulebook

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date(2026, 9, 28)
T = "var T = (el) => (el ? el.textContent.replace(/\\s+/g, ' ').trim() : null);"


def until(sel, ms=6000):
    return (f"new Promise((res, rej) => {{ const t0 = Date.now(); const tick = () => {{ const e = [...document.querySelectorAll("
            f"{json.dumps(sel)})].find((x) => x.getClientRects().length); if (e) res(true); else if (Date.now() - t0 > {ms}) "
            f"rej(new Error('never appeared: ' + {json.dumps(sel)})); else setTimeout(tick, 50); }}; tick(); }})")


def gone(sel, ms=6000):
    return (f"new Promise((res, rej) => {{ const t0 = Date.now(); const tick = () => {{ if (!document.querySelector({json.dumps(sel)})) "
            f"res(true); else if (Date.now() - t0 > {ms}) rej(new Error('still there: ' + {json.dumps(sel)})); else setTimeout(tick, 50); }}; "
            "tick(); })")


def click(sel, text=None):
    return (f"(() => {{ const els = [...document.querySelectorAll({json.dumps(sel)})].filter((e) => e.getClientRects().length); "
            f"const el = els.find((e) => {json.dumps(text)} === null || e.textContent.includes({json.dumps(text)}) "
            f"|| (e.getAttribute('aria-label') || '').includes({json.dumps(text)})); "
            f"if (!el) throw new Error('nothing to click: ' + {json.dumps(sel)} + ' ' + {json.dumps(text)}); el.click(); return 1; }})()")


def wait(ms):
    return f"new Promise((r) => setTimeout(r, {ms}))"


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def _scene(path, login=False):
    conn = db.connect(path)
    ids = rules_scene.build(conn)
    token = csrf = None
    if login:
        prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
        with conn:
            conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                         "VALUES (?, ?, ?, ?, ?)", (gate_dbs.SIGNIN_USER, auth.hash_secret(gate_dbs.SIGNIN_PASSWORD),
                                                    auth.new_totp_secret(), db.now(), db.now()))
        uid = conn.execute("SELECT id FROM auth_users").fetchone()[0]
        token, csrf = auth.create_session(conn, uid)
    conn.close()
    return ids, token, csrf


@pytest.fixture
def scene(tmp_path):
    path = tmp_path / "finance.db"
    ids, _, _ = _scene(path)
    srv = Server(path, today=TODAY)
    yield srv, ids, path
    srv.close()


def q(path, sql, args=()):
    conn = db.connect(path)
    try:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def filed(path, tid):
    return q(path, "SELECT category, category_source FROM v_transactions WHERE transaction_id = ?", (tid,))[0]


def rid(path, pattern):
    return q(path, "SELECT id FROM rules WHERE pattern = ?", (pattern,))[0][0]


def open_card(b, url, width):
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{url}/?t={time.time():.3f}#/settings")
    b.settle()
    b.js(until("#page .sc.rules .rsum"))
    b.js(click("#page .sc.rules .rsum"))
    b.js(until("#page .sc.rules .rrow"))


def row_action(b, rule_id, label, width):
    if width >= 1024:
        b.js(click(f"#page .sc.rules .rrow[data-rule='{rule_id}'] .rib", label))
    else:
        b.js(click(f"#page .sc.rules .rrow[data-rule='{rule_id}'] .rmore"))
        b.js(until(f"#page .sc.rules .rrow[data-rule='{rule_id}'] .rmenu"))
        b.js(click(f"#page .sc.rules .rrow[data-rule='{rule_id}'] .rmenu button", label))


def clean(b):
    assert b.problems() == []
    assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")


def toast(b, want=""):
    """The toast's text once it shows `want` (an earlier toast may still be fading out)."""
    b.js(f"""new Promise((res, rej) => {{ const t0 = Date.now(); const tick = () => {{ const t = document.querySelector('#toast.on');
      if (t && t.textContent.includes({json.dumps(want)})) res(1); else if (Date.now() - t0 > 6000) rej(new Error('no toast with '
      + {json.dumps(want)} + ': ' + (t ? t.textContent : 'none'))); else setTimeout(tick, 50); }}; tick(); }})""")
    return b.js(T + "T(document.querySelector('#toast'))")


def hide_toast(b):
    b.js("(() => { if (window.HF && HF.hideToast) HF.hideToast(); return 1; })()")
    b.js(wait(250))


@pytest.mark.parametrize("width", [1440, 390])
def test_real_list_counts_suggestions_and_ai_override_wording(chrome, scene, width):
    srv, _, _ = scene
    chrome.size(width, 900 if width > 800 else 844)
    chrome.goto(f"{srv.url}/?t={time.time():.3f}#/settings")
    chrome.settle()
    chrome.js(until("#page .sc.rules .rsum"))
    assert chrome.js(T + "T(document.querySelector('#page .sc.rules .rsum'))") == "5 rules · 2 yours · 2 suggestions"
    chrome.js(click("#page .sc.rules .rsum"))
    chrome.js(until("#page .sc.rules .rrow"))
    got = chrome.js(T + """(() => { const c = document.querySelector('#page .sc.rules');
      return {sugs: [...c.querySelectorAll('.rrow.sug .rst')].map(T), bold: [...c.querySelectorAll('.rrow.sug .rst b')].map(T),
        gh: [...c.querySelectorAll('.rlist .gh')].map(T), pats: [...c.querySelectorAll('.rlist .rrow .rpat')].map(T)}; })()""")
    assert got["sugs"] == ["You moved Equinox from the AI's pick to Other Health 2 times. Always do that?",
                           "You filed Trader Joe's under Groceries 2 times. Always do that?"]
    assert got["bold"] == ["Equinox", "Other Health", "Trader Joe's", "Groceries"]
    assert got["gh"] == ["Yours · 2", "AI · 3"]
    assert got["pats"] == ["Merchant is Blue Bottle", "Merchant is Netflix", "Merchant is Equinox", "Name contains OPENROUTER",
                           "Merchant is Xai"]
    clean(chrome)


@pytest.mark.parametrize("width", [1440, 390])
def test_real_edit_refiles_rows_but_never_a_hand_filed_one(chrome, scene, width):
    srv, ids, path = scene
    x = ids["xai"]
    assert filed(path, x[0]) == ("Other Shopping", "user") and filed(path, x[1]) == ("Subscriptions", "rule")
    xai = rid(path, "Xai")
    open_card(chrome, srv.url, width)
    row_action(chrome, xai, "Edit", width)
    chrome.js(until("#sheet.rsheet select"))
    assert chrome.js("document.querySelectorAll('#sheet input').length") == 0          # an AI rule: its category only
    chrome.js("(() => { const s = document.querySelector('#sheet select'); s.value = 'Entertainment'; s.dispatchEvent(new Event('change')); return 1; })()")
    chrome.js(click("#sheet .row .btn", "Save"))
    assert "2 charges re-filed" in toast(chrome, "Rule saved")
    assert filed(path, x[1]) == filed(path, x[2]) == ("Entertainment", "rule")
    assert filed(path, x[0]) == ("Other Shopping", "user")                                     # the user's own decision stays
    chrome.js(until(f"#page .rrow[data-rule='{xai}'][data-source=user]"))                # editing made it yours
    clean(chrome)


def test_real_user_rule_pattern_edit(chrome, scene):
    srv, ids, path = scene
    nf = rid(path, "Netflix")
    open_card(chrome, srv.url, 1440)
    row_action(chrome, nf, "Edit", 1440)
    chrome.js(until("#sheet.rsheet select"))
    chrome.js("(() => { const i = document.querySelector('#sheet input'); i.value = 'Netflix.com'; i.dispatchEvent(new Event('input')); return 1; })()")
    chrome.js(click("#sheet .row .btn", "Save"))
    assert "3 charges re-filed" in toast(chrome, "Rule saved")
    assert q(path, "SELECT pattern FROM rules WHERE id = ?", (nf,)) == [("Netflix.com",)]
    assert all(filed(path, t)[1] == "plaid" for t in ids["netflix"])
    chrome.js(until(f"#page .rrow[data-rule='{nf}']"))
    assert chrome.js(T + f"T(document.querySelector(\"#page .rrow[data-rule='{nf}'] .rpat\"))") == "Merchant is Netflix.com"
    clean(chrome)


@pytest.mark.parametrize("width", [1440, 390])
def test_real_keep_then_delete_leaves_rows_and_undo_restores(chrome, scene, width):
    srv, ids, path = scene
    op = rid(path, "OPENROUTER")
    open_card(chrome, srv.url, width)
    row_action(chrome, op, "Keep", width)
    assert "it’s yours now" in toast(chrome, "Kept")
    assert q(path, "SELECT created_from, priority FROM rules WHERE id = ?", (op,)) == [("user", 200)]
    chrome.js(until(f"#page .rrow[data-rule='{op}'][data-source=user]"))
    hide_toast(chrome)
    bb = rid(path, "Blue Bottle")
    before = [filed(path, t) for t in ids["bluebottle"]]
    row_action(chrome, bb, "Delete", width)
    chrome.js(until("#page .rrow.confirm"))
    assert chrome.js(T + "[...document.querySelector('#page .rrow.confirm').children].map((c) => [...c.children].length > 1 ? [...c.children].map(T).join(' | ') : T(c)).join(' · ')") \
        == "Delete? Filed charges stay. · Delete | Cancel"
    assert q(path, "SELECT COUNT(*) FROM rules WHERE id = ?", (bb,)) == [(1,)]           # nothing yet
    clean(chrome)
    chrome.js(click("#page .rrow.confirm .btn", "Delete"))
    assert "Filed charges stay as they are" in toast(chrome, "Deleted")
    assert q(path, "SELECT COUNT(*) FROM rules WHERE id = ?", (bb,)) == [(0,)]
    assert [filed(path, t) for t in ids["bluebottle"]] == before                          # filed charges stay
    chrome.js(gone(f"#page .rrow[data-rule='{bb}']"))
    chrome.js(click("#toast button", "Undo"))
    chrome.js(until(f"#page .rrow[data-rule='{bb}']"))
    assert q(path, "SELECT pattern, created_from FROM rules WHERE id = ?", (bb,)) == [("Blue Bottle", "user")]
    assert chrome.js(T + "T(document.querySelector('#page .sc.rules .rsum'))") == "5 rules · 3 yours · 2 suggestions"
    clean(chrome)


def test_real_add_rule_and_dismiss(chrome, scene):
    srv, ids, path = scene
    open_card(chrome, srv.url, 1440)
    keys = chrome.js("[...document.querySelectorAll('#page .rrow.sug')].map((r) => r.dataset.sug)")
    assert len(keys) == 2                                                                 # Equinox (AI override), Trader Joe's
    chrome.js(click(f"#page .rrow.sug[data-sug=\"{keys[0]}\"] .btn", "Add rule"))
    assert "Rule added" in toast(chrome, "Rule added")
    assert q(path, "SELECT created_from, match_field FROM rules r JOIN categories c ON c.id = r.category_id "
                   "WHERE pattern = ? AND c.name = 'Other Health'", ("Equinox",)) == [("user", "merchant_name")]
    assert all(filed(path, t)[0] == "Other Health" for t in ids["gym"])                          # the third Equinox row too
    chrome.js(gone(f"#page .rrow.sug[data-sug=\"{keys[0]}\"]"))
    hide_toast(chrome)
    chrome.js(click(f"#page .rrow.sug[data-sug=\"{keys[1]}\"] .btn", "Dismiss"))           # Trader Joe's
    chrome.js(gone("#page .sc.rules .rsugs"))                                             # none left: the block goes
    assert json.loads(q(path, "SELECT value FROM settings WHERE key = 'rules_dismissed_suggestions'")[0][0]) == [keys[1]]
    assert q(path, "SELECT COUNT(*) FROM rules WHERE pattern = ?", ("Trader Joe's",)) == [(0,)]
    assert chrome.js(T + "T(document.querySelector('#page .sc.rules .rsum'))") == "5 rules · 3 yours"
    # a reload keeps the dismissal
    open_card(chrome, srv.url, 1440)
    assert chrome.js("document.querySelectorAll('#page .sc.rules .rsugs').length") == 0
    clean(chrome)


def listed(b):
    """The rows the card lists now (rule ids, top to bottom), its group heads and the pressed filter."""
    return b.js(T + """(() => { const c = document.querySelector('#page .sc.rules');
      return {ids: [...c.querySelectorAll('.rlist .rrow')].map((r) => Number(r.dataset.rule)), gh: [...c.querySelectorAll('.rlist .gh')].map(T),
        on: T(c.querySelector('.rseg [aria-pressed=true]')), q: c.querySelector('.rq').value, none: T(c.querySelector('.rlist .rnone'))}; })()""")


def search(b, text):
    b.js(f"(() => {{ const i = document.querySelector('#page .sc.rules .rq'); i.value = {json.dumps(text)}; "
         "i.dispatchEvent(new Event('input')); return 1; })()")


def test_real_search_and_filter_hold_across_a_saved_change(chrome, scene):
    srv, _, path = scene
    nf, eq, op, xai = (rid(path, p) for p in ("Netflix", "Equinox", "OPENROUTER", "Xai"))
    assert {r for (r,) in q(path, "SELECT r.id FROM rules r JOIN categories c ON c.id = r.category_id "
                                  "WHERE c.name = 'Entertainment'")} == {nf, eq}
    open_card(chrome, srv.url, 1440)
    search(chrome, "ENTERTAIN")                                                           # the category, any case
    assert listed(chrome) == {"ids": [nf, eq], "gh": ["Yours · 1", "AI · 1"], "on": "All", "q": "ENTERTAIN", "none": None}
    search(chrome, "subscr")
    assert listed(chrome) == {"ids": [op, xai], "gh": ["AI · 2"], "on": "All", "q": "subscr", "none": None}
    chrome.js(click("#page .sc.rules .rseg button", "Yours"))
    assert listed(chrome) == {"ids": [], "gh": [], "on": "Yours", "q": "subscr", "none": "No rules match."}
    chrome.js(click("#page .sc.rules .rseg button", "AI"))
    assert listed(chrome) == {"ids": [op, xai], "gh": [], "on": "AI", "q": "subscr", "none": None}
    # Keep redraws the card from the server: the search and the filter stay, so the rule leaves the AI list
    row_action(chrome, op, "Keep", 1440)
    assert "it’s yours now" in toast(chrome, "Kept")
    assert q(path, "SELECT created_from FROM rules WHERE id = ?", (op,)) == [("user",)]
    chrome.js(gone(f"#page .rrow[data-rule='{op}']"))
    assert listed(chrome) == {"ids": [xai], "gh": [], "on": "AI", "q": "subscr", "none": None}
    chrome.js(click("#page .sc.rules .rseg button", "Yours"))
    assert listed(chrome) == {"ids": [op], "gh": [], "on": "Yours", "q": "subscr", "none": None}
    search(chrome, "")
    chrome.js(click("#page .sc.rules .rseg button", "All"))
    got = listed(chrome)
    assert sorted(got["ids"]) == sorted(r for (r,) in q(path, "SELECT id FROM rules")) and got["gh"] == ["Yours · 3", "AI · 2"]
    assert chrome.js(T + "T(document.querySelector('#page .sc.rules .rsum'))") == "5 rules · 3 yours · 2 suggestions"
    clean(chrome)


def test_real_no_suggestions_block_after_db_dismissal(chrome, scene):
    srv, _, path = scene
    conn = db.connect(path)
    try:
        keys = [s["key"] for s in rulebook.suggestions(conn)]
        assert len(keys) == 2                                                             # Equinox (AI override), Trader Joe's
        for k in keys:
            rulebook.dismiss(conn, k)                                                     # the server's own write path
        assert rulebook.suggestions(conn) == []
    finally:
        conn.close()
    assert json.loads(q(path, "SELECT value FROM settings WHERE key = ?", (rulebook.DISMISSED,))[0][0]) == keys
    chrome.size(1440, 900)
    chrome.goto(f"{srv.url}/?t={time.time():.3f}#/settings")
    chrome.settle()
    chrome.js(until("#page .sc.rules .rsum"))
    assert chrome.js(T + "T(document.querySelector('#page .sc.rules .rsum'))") == "5 rules · 2 yours"
    assert chrome.js("document.querySelectorAll('#page .sc.rules .rsum .rsg').length") == 0
    chrome.js(click("#page .sc.rules .rsum"))
    chrome.js(until("#page .sc.rules .rrow"))
    got = chrome.js(T + """(() => { const c = document.querySelector('#page .sc.rules');
      return {sugs: c.querySelectorAll('.rsugs, .rrow.sug, .rmoresug').length, first: c.querySelector('.rbody').firstElementChild.className,
        gh: [...c.querySelectorAll('.gh')].map(T), rows: c.querySelectorAll('.rlist .rrow').length}; })()""")
    assert got == {"sugs": 0, "first": "rbar", "gh": ["Yours · 2", "AI · 3"], "rows": 5}   # the list starts the card
    api = chrome.js("fetch('/api/rules').then((r) => r.json())")
    assert api["suggestions"] == [] and api["counts"] == {"total": 5, "user": 2, "ai": 3}
    clean(chrome)


def test_real_phone_action_menu_geometry(chrome, scene):
    srv, _, path = scene
    op = rid(path, "OPENROUTER")                                                          # AI: the longest label, all 3 actions
    open_card(chrome, srv.url, 390)
    row = f"#page .sc.rules .rrow[data-rule='{op}']"
    chrome.js(click(f"{row} .rmore"))
    chrome.js(until(f"{row} .rmenu"))
    got = chrome.js(T + f"""(() => {{ const row = document.querySelector({json.dumps(row)}); const R = (e) => e.getBoundingClientRect();
      const r = R(row), g = R(row.querySelector('.grow')), m = R(row.querySelector('.rmenu')), more = R(row.querySelector('.rmore'));
      const box = (e) => {{ const x = R(e); return [x.left, x.top, x.right, x.bottom]; }};
      const clipped = [...row.querySelectorAll('.rpat, .rsub')].filter((e) => e.scrollWidth > e.clientWidth + 1).map(T);
      const out = [...row.querySelectorAll('*')].filter((e) => e.getClientRects().length).filter((e) => {{ const x = R(e);
        return x.left < r.left - 1 || x.right > r.right + 1 || x.top < r.top - 1 || x.bottom > r.bottom + 1; }}).map((e) => e.className || e.tagName);
      return {{w: r.width, h: r.height, gh: g.height, mh: m.height, textBottom: Math.max(g.bottom, more.bottom), menuTop: m.top,
        next: R(row.nextElementSibling).top - r.bottom, btns: [...row.querySelectorAll('.rmenu button')].map(box),
        labels: [...row.querySelectorAll('.rmenu button')].map(T), text: [T(row.querySelector('.rpat')), T(row.querySelector('.rsub'))],
        clipped, out, open: row.classList.contains('rmenu-open'), exp: row.querySelector('.rmore').getAttribute('aria-expanded'),
        menus: document.querySelectorAll('#page .sc.rules .rmenu').length}}; }})()""")
    assert got["w"] > 250, got
    assert got["h"] >= got["gh"] + got["mh"], got                                        # tall enough for the text and the buttons
    assert got["menuTop"] >= got["textBottom"] - 1, got                                  # the buttons sit under the text and the ⋯
    assert got["next"] >= -1, got                                                         # and push the next row down
    assert got["out"] == [] and got["clipped"] == [], got                                # nothing sticks out, no text cut off
    assert got["labels"] == ["Edit", "Keep", "Delete"] and len(got["btns"]) == 3
    assert got["text"] == ["Name contains OPENROUTER", "→ Subscriptions · 2 matches"]
    b = got["btns"]
    assert all(not (p[0] < s[2] - 1 and s[0] < p[2] - 1 and p[1] < s[3] - 1 and s[1] < p[3] - 1)
               for i, p in enumerate(b) for s in b[i + 1:]), b                             # the buttons don't overlap
    assert (got["open"], got["exp"], got["menus"]) == (True, "true", 1)
    clean(chrome)
    chrome.js(click(f"{row} .rmore"))                                                     # ⋯ again closes it
    chrome.js(gone(f"{row} .rmenu"))
    closed = chrome.js(f"(() => {{ const row = document.querySelector({json.dumps(row)}); "
                       "return [row.getBoundingClientRect().height, row.querySelector('.rmore').getAttribute('aria-expanded')]; })()")
    assert closed[0] < got["h"] and closed[1] == "false"
    assert q(path, "SELECT created_from FROM rules WHERE id = ?", (op,)) == [("ai",)]  # opening the menu changes nothing
    clean(chrome)


def test_real_builtin_login_csrf_and_the_error_toast(chrome, tmp_path):
    path = tmp_path / "finance.db"
    _, token, _csrf = _scene(path, login=True)
    srv = Server(path, today=TODAY)
    try:
        chrome.call("Network.setCookie", name=auth.COOKIE, value=token, url=srv.url)
        open_card(chrome, srv.url, 1440)
        assert chrome.js("!!document.querySelector('meta[name=csrf-token]')")
        xai = rid(path, "Xai")
        row_action(chrome, xai, "Keep", 1440)                                              # the page's token: accepted
        assert "it’s yours now" in toast(chrome, "Kept")
        assert q(path, "SELECT created_from FROM rules WHERE id = ?", (xai,)) == [("user",)]
        hide_toast(chrome)
        chrome.js("document.querySelector('meta[name=csrf-token]').content = 'wrong'; 1")
        op = rid(path, "OPENROUTER")
        row_action(chrome, op, "Keep", 1440)
        msg = toast(chrome, "Not saved")
        assert msg.startswith("Not saved:") and "CSRF" in msg
        assert q(path, "SELECT created_from FROM rules WHERE id = ?", (op,)) == [("ai",)]  # refused: nothing changed
        chrome.js(until(f"#page .rrow[data-rule='{op}'][data-source=ai]"))                # the card still shows it as AI
        probs = chrome.problems()                                                           # only the refused POST's 403 log
        assert all("403" in p for p in probs), probs
    finally:
        chrome.call("Network.clearBrowserCookies")
        srv.close()
