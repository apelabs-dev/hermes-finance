"""P2b interaction and review screenshots, against October synthetic data only."""
from datetime import date
from pathlib import Path

import pytest
import demo_data
from cdp import Chrome, Server, find_chrome
from hermes_finance import db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason='Chrome is not installed')
TODAY = date(2026, 10, 20)


@pytest.fixture(scope='module')
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp('wherewent') / 'finance.db'
    conn = db.connect(path)
    demo_data.build(conn, TODAY)
    conn.close()
    server = Server(path, today=TODAY)
    yield server
    server.close()


def key(b, name, code=None):
    text = {'Enter': '\r', ' ': ' '}.get(name)
    b.call('Input.dispatchKeyEvent', type='keyDown', key=name, code=code or name,
           **({'text': text} if text else {}), windowsVirtualKeyCode={'Enter': 13, 'Escape': 27, ' ': 32}.get(name, 0))
    b.call('Input.dispatchKeyEvent', type='keyUp', key=name, code=code or name,
           windowsVirtualKeyCode={'Enter': 13, 'Escape': 27, ' ': 32}.get(name, 0))
    b.settle()


@pytest.mark.parametrize('width', [1280, 390])
def test_groups_drill_filters_and_screenshots(site, width):
    with Chrome() as b:
        b.size(width, 1000 if width == 1280 else 844)
        b.goto(f'{site.url}/#/overview?month=2026-10')
        b.settle()
        assert b.js("document.querySelectorAll('.wherewent .crow').length") > 3
        assert b.js("[...document.querySelectorAll('.wherewent .crowl')].every(x => x.tagName === 'BUTTON')")
        assert b.js("(() => { const ch = Chart.getChart(document.querySelector('.wherewent canvas')); return ch.data.labels.every((n,i) => ch.data.datasets[0].backgroundColor[i] === 'rgb(' + "
                    "({Home:'142,168,255',Food:'245,180,84',Transport:'95,198,232',Shopping:'245,158,139',Bills:'163,168,255',Health:'199,146,234',Leisure:'255,143,177',Misc:'155,162,182'})[n] + ')'); })()")
        shots = Path('shots/p2b')
        shots.mkdir(parents=True, exist_ok=True)
        b.js("document.querySelector('.wherewent').closest('.card').scrollIntoView({block:'center'})")
        b.screenshot(shots / f'groups-{width}.png')
        b.js("document.querySelector('[data-wwfocus=\"drill:Food\"]').focus()")
        key(b, 'Enter')
        assert b.js("document.activeElement.dataset.wwfocus") == 'back'
        assert b.js("[...document.querySelectorAll('.wherewent .crow b')].some(x => x.textContent === 'Eating Out')")
        assert b.js("[...document.querySelectorAll('.wherewent a.crowl')].every(x => x.hash.includes('category='))")
        b.js("document.querySelector('.wherewent').closest('.card').scrollIntoView({block:'center'})")
        b.screenshot(shots / f'food-{width}.png')
        key(b, 'Escape')
        assert b.js("document.activeElement.dataset.wwfocus") == 'drill:Food'
        key(b, ' ', 'Space')
        assert b.js("document.activeElement.dataset.wwfocus") == 'back'
        b.js("document.querySelector('[data-wwfocus=back]').click()")
        b.settle()
        # A real pointer (touch on phone) selects Food's donut slice.
        b.js("document.querySelector('.wherewent canvas').scrollIntoView({block:'center'})")
        x, y = b.js("(() => { const cv = document.querySelector('.wherewent canvas'), ch = Chart.getChart(cv); const p = ch.getDatasetMeta(0).data[ch.data.labels.indexOf('Food')].getCenterPoint(); const r = cv.getBoundingClientRect(); return [r.left+p.x,r.top+p.y]; })()")
        if width == 390:
            b.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[{'x': x, 'y': y}])
            b.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
        else:
            b.call('Input.dispatchMouseEvent', type='mousePressed', x=x, y=y, button='left', clickCount=1)
            b.call('Input.dispatchMouseEvent', type='mouseReleased', x=x, y=y, button='left', clickCount=1)
        b.settle()
        assert b.js("document.querySelector('.wwnav b').textContent") == 'Food'
        b.js("[...document.querySelectorAll('.wherewent a.crowl')].find(x => x.textContent.includes('Eating Out')).click()")
        b.settle()
        assert 'category=Eating' in b.js('location.hash')
        assert b.until("document.querySelectorAll('.tx [data-tid]').length") > 0
        b.goto(f'{site.url}/#/overview?month=2026-10')
        b.settle()
        # The drilled group is remembered (hf.wherewent.group): the reload comes back to Food, so go back to the groups.
        b.js("(() => { const b = document.querySelector('[data-wwfocus=back]'); if (b) b.click(); })()")
        b.settle()
        b.js("document.querySelector('[aria-label=\"View Food payments\"]').click()")
        b.settle()
        assert 'group=Food' in b.js('location.hash')
        assert b.js('document.documentElement.scrollWidth <= innerWidth')
        assert b.problems() == []


def test_a_partly_hidden_group_row_shows_only_what_the_chart_shows(site):
    """The owner, 2026-10-20: hiding a category hides it everywhere in the card. A group with one category hidden reads
    the shown part's amount, payment count and %, and names what's hidden; data-spend keeps the full month."""
    post = ("fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
            "body: JSON.stringify({chart_excluded_categories: CATS})}).then((r) => r.status)")
    with Chrome() as b:
        b.size(1280, 1000)
        b.goto(f'{site.url}/#/overview?month=2026-10')
        b.settle()
        ov = b.js("fetch('/api/overview?month=2026-10').then((r) => r.json())")
        g = next(g for g in ov['spend_groups'] if sum(1 for c in g['categories'] if c['count']) >= 2)
        paid = sorted((c for c in g['categories'] if c['count']), key=lambda c: -c['spend'])
        hide, keep = paid[0], paid[1:]
        assert b.js(post.replace('CATS', repr([hide['category']]))) == 200
        try:
            b.goto(f'{site.url}/#/overview?month=2026-10&r=1')
            b.settle()
            row = b.js(f"""(() => {{ const r = [...document.querySelectorAll('.wherewent .crow')]
                .find((x) => x.querySelector('b').textContent === {g['group']!r});
              return {{amt: r.querySelector('.amt').textContent, sub: r.querySelector('.grow > span').textContent,
                       spend: r.dataset.spend, shown: r.dataset.shown, off: r.classList.contains('off')}}; }})()""")
            shown = sum(c['spend'] for c in keep)
            n = sum(c['count'] for c in keep)
            assert not row['off'], row
            assert float(row['amt'].replace('$', '').replace(',', '')) == pytest.approx(shown, abs=0.5), row
            assert f"· {n} payment{'s' if n != 1 else ''} · {hide['category']} hidden" in row['sub'], row
            assert float(row['spend']) == pytest.approx(g['spend'], abs=0.005), row      # the full month is kept
            assert float(row['shown']) == pytest.approx(shown, abs=0.005), row
            assert b.problems() == []
        finally:
            b.js(post.replace('CATS', '[]'))
