"""Collapsed groups remembered on this device: the Accounts page's group cards (hf.accounts.shut) and Where it went's
drilled group (hf.wherewent.group), against October synthetic data in headless Chrome (tests/cdp.py)."""
import math
from datetime import date

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from hermes_finance import db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason='Chrome is not installed')
TODAY = date(2026, 10, 20)


@pytest.fixture(scope='module')
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp('collapsed') / 'finance.db'
    conn = db.connect(path)
    demo_data.build(conn, TODAY)
    conn.close()
    server = Server(path, today=TODAY)
    yield server
    server.close()


def open_page(b, site, page, fresh=False):
    b.goto(f'{site.url}/#/{page}')
    if fresh:
        b.js("localStorage.removeItem('hf.accounts.shut'); localStorage.removeItem('hf.wherewent.group')")
    b.goto(f'{site.url}/?r=1#/{page}')
    b.settled()   # its fetches done too, not just two frames (t_d2b88168)


def acct(b, key='cash'):
    return b.js(f"""(() => {{
      const c = document.querySelector('.acctgrp[data-fold="{key}"]');
      const btn = c.querySelector('.gfold');
      const head = c.querySelector('h3');
      const kids = [...c.children].filter((e) => e.tagName !== 'H3');
      return {{tag: btn.tagName, type: btn.type, expanded: btn.getAttribute('aria-expanded'),
        label: btn.getAttribute('aria-label'), shut: c.classList.contains('shut'),
        hidden: kids.length > 0 && kids.every((e) => e.getClientRects().length === 0),
        shown: kids.length > 0 && kids.some((e) => e.getClientRects().length > 0),
        title: head.querySelector('.ttl').getClientRects().length > 0,
        total: head.querySelector('small').getClientRects().length > 0,
        chev: getComputedStyle(btn.querySelector('svg')).transform,
        stored: localStorage.getItem('hf.accounts.shut')}};
    }})()""")


def chevron_deg(transform):
    """The SVG's rotation in degrees, from computed matrix(a, b, c, d, tx, ty): atan2(b, a)."""
    a, b = (float(v) for v in transform[len('matrix('):-1].split(',')[:2])
    return math.degrees(math.atan2(b, a))


def ww(b):
    return b.js("""(() => { const c = document.querySelector('.wherewent');
      const nav = c.querySelector('.wwnav b');
      return {drilled: nav ? nav.textContent : null, stored: localStorage.getItem('hf.wherewent.group'),
        focused: !!(document.activeElement && document.activeElement.closest('.wherewent'))}; })()""")


def test_an_accounts_group_folds_and_is_remembered(site):
    with Chrome() as b:
        b.size(1280, 1000)
        open_page(b, site, 'accounts', fresh=True)
        s = acct(b)
        assert s['tag'] == 'BUTTON' and s['type'] == 'button', s
        assert s['expanded'] == 'true' and s['shut'] is False and s['shown'] is True, s
        assert s['label'] == 'Collapse Cash', s
        assert s['title'] is True and s['total'] is True, s

        b.js("document.querySelector('.acctgrp[data-fold=\"cash\"] .gfold').click()")
        b.settled()   # waits for the 150 ms fold transition; settle()'s 60 ms pause lost that race under load
        s = acct(b)
        assert s['shut'] is True and s['hidden'] is True and s['expanded'] == 'false', s
        assert s['label'] == 'Expand Cash', s
        assert s['title'] is True and s['total'] is True, 'the title, count and total stay in view'
        assert abs(chevron_deg(s['chev']) + 90) <= 1, f"the chevron turns to -90deg when folded: {s['chev']}"
        assert b.js("JSON.parse(localStorage.getItem('hf.accounts.shut'))") == ['cash']

        open_page(b, site, 'accounts')   # a reload keeps it folded
        s = acct(b)
        assert s['shut'] is True and s['hidden'] is True and s['expanded'] == 'false', s

        b.js("document.querySelector('.acctgrp[data-fold=\"cash\"] .gfold').click()")
        b.settled()   # the unfold transition too, for the same reason
        assert acct(b)['shown'] is True
        assert b.js("JSON.parse(localStorage.getItem('hf.accounts.shut'))") == []
        open_page(b, site, 'accounts')   # and unfolding is remembered too
        assert acct(b)['shut'] is False and acct(b)['shown'] is True
        assert b.problems() == []


def test_the_chevron_has_a_44px_hit_area_on_a_phone(site):
    with Chrome() as b:
        b.size(390, 844)
        open_page(b, site, 'accounts', fresh=True)
        box = b.js("""(() => { const e = document.querySelector('.acctgrp[data-fold="cash"] .gfold');
          e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();
          return {h: r.height, w: r.width}; })()""")
        assert box['h'] >= 44 and box['w'] >= 44, box
        assert b.problems() == []


def test_where_it_went_remembers_the_drilled_group(site):
    with Chrome() as b:
        b.size(1280, 1000)
        open_page(b, site, 'overview?month=2026-10', fresh=True)
        assert ww(b) == {'drilled': None, 'stored': None, 'focused': False}
        b.js("document.querySelector('[data-wwfocus=\"drill:Food\"]').click()")
        b.settle()
        assert ww(b)['drilled'] == 'Food' and ww(b)['stored'] == 'Food'
        b.goto(f'{site.url}/?r=2#/overview?month=2026-10')   # a reload still shows the drilled group...
        b.settle()
        s = ww(b)
        assert s['drilled'] == 'Food' and s['stored'] == 'Food', s
        assert s['focused'] is False, 'restoring the drilled group does not steal focus'

        b.js("document.querySelector('[data-wwfocus=back]').click()")
        b.settle()
        assert ww(b)['drilled'] is None and ww(b)['stored'] is None
        b.goto(f'{site.url}/?r=3#/overview?month=2026-10')
        b.settle()
        assert ww(b)['drilled'] is None and ww(b)['stored'] is None

        b.js("localStorage.setItem('hf.wherewent.group', 'No such group')")
        b.goto(f'{site.url}/?r=4#/overview?month=2026-10')   # a group that is gone is ignored and cleared
        b.settle()
        assert ww(b)['drilled'] is None and ww(b)['stored'] is None
        assert b.problems() == []
