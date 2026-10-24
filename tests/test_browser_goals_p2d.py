"""Synthetic P2d appearance round trips and required desktop/phone evidence, using tests/cdp.py."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import demo_data
from cdp import Chrome, Server, find_chrome
from hermes_finance import db, goals, insights

pytestmark = pytest.mark.skipif(find_chrome() is None, reason='Chrome is not installed')
SHOTS = Path(__file__).resolve().parents[1] / 'shots' / 'p2d'


def tap(b, selector):
    box = b.js(f"""(() => {{const el=document.querySelector({json.dumps(selector)});
        el.scrollIntoView({{block:'center'}}); const r=el.getBoundingClientRect();
        return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()""")
    b.call('Input.dispatchMouseEvent', type='mousePressed', button='left', clickCount=1, **box)
    b.call('Input.dispatchMouseEvent', type='mouseReleased', button='left', clickCount=1, **box)
    b.settle()


@pytest.mark.parametrize('width', [1280, 390])
def test_goal_appearance_persists_and_capture(tmp_path, width):
    today = date.today()
    path = tmp_path / 'synthetic.sqlite'
    conn = db.connect(path)
    demo_data.build(conn, today=today)
    month = insights.add_months(today.strftime('%Y-%m'), 3)
    emergency = goals.create(conn, dict(kind='save', name='Emergency fund', track='account',
        account_id='acc_nova_sav', target=12000, target_month=month, icon='shield-check', colour='#4fd1a5'), today)
    trip = goals.create(conn, dict(kind='save', name='Japan adventure', track='manual', target=3000,
        target_month=month, icon='plane', colour='#8ea8ff'), today)
    goals.add_entry(conn, trip['id'], {'amount': 1800}, today)
    complete = goals.create(conn, dict(kind='save', name='New laptop', track='manual', target=1200,
        target_month=month, icon='briefcase', colour='#c792ea'), today)
    goals.add_entry(conn, complete['id'], {'amount': 1200}, today)
    conn.close()
    server = Server(path, today=today)
    try:
        assert server.port != 8765
        with Chrome() as b:
            b.size(width, 900 if width == 1280 else 844, scale=1)
            b.goto(f'{server.url}/#/goals')
            b.settle()
            assert b.js("document.documentElement.scrollWidth <= innerWidth + 1")
            assert '$400 a month to reach it on time' in b.js(f"document.querySelector('[data-goal=\"{trip['id']}\"] .gpace').textContent")
            assert 'a month' not in b.js(f"document.querySelector('[data-goal=\"{complete['id']}\"] .gpace').textContent")
            SHOTS.mkdir(parents=True, exist_ok=True)
            b.screenshot(SHOTS / f'goal-cards-{width}.png', full=True)
            tap(b, f'[data-goal="{emergency["id"]}"]')
            assert b.js("document.querySelectorAll('#sheet .gicon').length") == 12
            assert b.js("document.querySelectorAll('#sheet .gcolour').length") == 8
            tap(b, '#sheet button[data-icon="rocket"]')
            tap(b, '#sheet [data-colour="#f5b454"]')
            assert b.js("document.querySelector('#sheet button[data-icon=rocket]').getAttribute('aria-pressed')") == 'true'
            assert b.js("document.querySelector('#sheet [data-colour=\"#f5b454\"]').getAttribute('aria-pressed')") == 'true'
            assert b.js("document.querySelector('#sheet').scrollWidth <= document.querySelector('#sheet').clientWidth + 1")
            b.js("document.querySelector('#sheet').scrollTop=0")
            b.settle()
            b.screenshot(SHOTS / f'goal-edit-{width}.png')
            tap(b, '#sheet [aria-label="Close"]')
            b.goto(f'{server.url}/#/goals')
            b.settle()
            selector = f'[data-goal="{emergency["id"]}"]'
            assert b.js(f"document.querySelector({json.dumps(selector+' .cb svg')}).dataset.icon") == 'rocket'
            assert b.js(f"getComputedStyle(document.querySelector({json.dumps(selector+' .cb')})).getPropertyValue('--rgb').trim()") == '245,180,84'
            conn = db.connect(path)
            assert tuple(conn.execute('SELECT icon,colour FROM goals WHERE id=?', (emergency['id'],)).fetchone()) == ('rocket', '#f5b454')
            conn.close()
            b.goto(f'{server.url}/#/overview')
            b.settle()
            assert b.js("document.querySelector('.gtile .hi svg').dataset.icon") == 'rocket'
            assert b.problems() == []
    finally:
        server.close()
