"""P2d: calendar-month pace, additive v17 appearance, validation and public HTTP round trips."""
from __future__ import annotations

import json
from datetime import date

import pytest

from hermes_finance import config, db, goals, web


@pytest.mark.parametrize('today,deadline,months,need,past_due', [
    (date(2026, 10, 1), '2027-01', 3, 334, False),
    (date(2026, 10, 31), '2027-01', 3, 334, False),
    (date(2026, 12, 31), '2027-01', 1, 1001, False),
    (date(2027, 1, 1), '2027-01', 1, 1001, False),
    (date(2027, 1, 31), '2027-01', 1, 1001, False),
    (date(2027, 2, 1), '2027-01', 0, None, True),
    (date(2028, 2, 29), '2028-03', 1, 1001, False),
    (date(2026, 10, 20), None, None, None, False),
])
def test_pace_calendar_boundaries(today, deadline, months, need, past_due):
    pace = goals._pace(200, 1200.01, 90, deadline, today)
    assert (pace['months_left'], pace['need_per_month'], pace['past_due']) == (months, need, past_due)


@pytest.mark.parametrize('saved', [1200.01, 1500])
@pytest.mark.parametrize('deadline', [None, '2026-09', '2026-10', '2027-01'])
def test_reached_goals_have_no_required_pace(saved, deadline):
    p = goals._pace(saved, 1200.01, 90, deadline, date(2026, 10, 20))
    assert p['need_per_month'] is None and p['months_left'] is None and not p['past_due']


@pytest.mark.parametrize('saved,target,expected', [(0, 1200, 400), (0.1, 1200.1, 400), (0, 0.01, 1)])
def test_pace_rounds_up_whole_dollars_without_float_artifacts(saved, target, expected):
    assert goals._pace(saved, target, 0, '2027-01', date(2026, 10, 20))['need_per_month'] == expected


def test_v16_to_v17_preserves_existing_defaults_and_choices(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic.sqlite'
    with monkeypatch.context() as m:
        m.setattr(db, '_v17_schema', lambda conn: None)
        m.setattr(db, 'SCHEMA_VERSION', 16)
        conn = db.connect(path)
    assert 'icon' not in {r[1] for r in conn.execute('PRAGMA table_info(goals)')}
    with conn:
        conn.execute("INSERT INTO items (item_id, institution_name, access_token, created_at) VALUES ('i','Synthetic','fake','now')")
        conn.execute("INSERT INTO accounts (account_id,item_id,name,type,current_balance) VALUES ('cc','i','Card','credit',50)")
        conn.execute("INSERT INTO manual_accounts (id,kind,name,balance,balance_as_of,created_at,updated_at) VALUES (1,'credit_card','Card',50,'2026-10-20','now','now')")
        rows = [('save', 'account', None, None), ('save', 'manual', None, None),
                ('save', 'earmark', None, 'archived'), ('monthly', None, None, None),
                ('payoff', None, 'cc', None), ('payoff', None, 'manual:1', None), ('payoff', None, 'missing', None)]
        for i, (kind, track, aid, archived) in enumerate(rows):
            conn.execute("INSERT INTO goals (kind,name,target,track,account_id,archived_at,created_at,updated_at) VALUES (?,?,100,?,?,?,'now','now')",
                         (kind, f'Goal {i}', track, aid, archived))
    original = [tuple(r) for r in conn.execute('SELECT * FROM goals ORDER BY id')]
    conn.close()
    conn = db.connect(path)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 19
        upgraded = [tuple(r) for r in conn.execute('SELECT * FROM goals ORDER BY id')]
        assert [r[:-2] for r in upgraded] == original
        assert [r[-2] for r in upgraded] == ['piggy-bank', 'pencil', 'split', 'piggy-bank', 'card', 'card', 'handcoins']
        assert [r[-1] for r in upgraded] == ['#4fd1a5'] * 4 + ['#f5b454'] * 3
        db.init_schema(conn)
        assert [tuple(r) for r in conn.execute('SELECT * FROM goals ORDER BY id')] == upgraded
        conn.execute("UPDATE goals SET icon='plane',colour='#8ea8ff' WHERE id=1")
        conn.commit()
        db.init_schema(conn)
        assert tuple(conn.execute('SELECT icon,colour FROM goals WHERE id=1').fetchone()) == ('plane', '#8ea8ff')
    finally:
        conn.close()


@pytest.mark.parametrize('field,value', [('icon', 'evil'), ('icon', 'pencil'), ('icon', None), ('icon', []),
    ('colour', '#ffffff'), ('colour', 'red'), ('colour', None), ('colour', {})])
def test_invalid_appearance_is_rejected_on_create_and_edit(conn, field, value):
    data = dict(kind='save', name='Trip', track='manual', target=100)
    with pytest.raises(goals.GoalError) as e:
        goals.create(conn, {**data, field: value}, date(2026, 10, 20))
    assert e.value.field == field
    assert conn.execute('SELECT COUNT(*) FROM goals').fetchone()[0] == 0
    g = goals.create(conn, data, date(2026, 10, 20))
    with pytest.raises(goals.GoalError) as e:
        goals.edit(conn, g['id'], {field: value}, date(2026, 10, 20))
    assert e.value.field == field
    assert goals.one(conn, g['id'], date(2026, 10, 20))[field] == g[field]


def test_http_appearance_create_edit_list_and_overview(conn, data_dir):
    app = web.App(config.db_path(), lambda: pytest.fail('provider called'), '127.0.0.1', 18991)
    app.today = lambda: date(2026, 10, 20)
    def call(method, route, data=None):
        headers = {'Host': '127.0.0.1:18991', 'Origin': 'http://127.0.0.1:18991', 'Content-Type': 'application/json'}
        status, _, body = app.handle(method, route, headers, json.dumps(data).encode() if data else b'')
        assert status == 200, body
        return json.loads(body)
    g = call('POST', '/api/goals', dict(kind='save', name='Trip', track='manual', target=1000, icon='plane', colour='#8ea8ff'))
    assert (g['icon'], g['colour']) == ('plane', '#8ea8ff')
    for i, icon in enumerate(goals.ICONS):
        colour = goals.COLOURS[i % len(goals.COLOURS)]
        changed = call('POST', '/api/goals/edit', dict(id=g['id'], icon=icon, colour=colour))
        listed = call('GET', '/api/goals')['goals'][0]
        tile = call('GET', '/api/overview')['goals']['top']
        assert all((x['icon'], x['colour']) == (icon, colour) for x in (changed, listed, tile))
    assert set(goals.ICONS) <= web.ICONS.keys()


@pytest.mark.parametrize('kind', ['monthly', 'payoff'])
def test_appearance_is_editable_for_every_goal_kind(conn, kind):
    today = date(2026, 10, 20)
    if kind == 'payoff':
        with conn:
            conn.execute("INSERT INTO manual_accounts (id,kind,name,balance,balance_as_of,created_at,updated_at) "
                         "VALUES (1,'loan','Synthetic loan',500,'2026-10-20','now','now')")
    data = dict(kind=kind, target=500, account_id='manual:1', icon='sprout', colour='#5fc6e8')
    goal = goals.create(conn, data, today)
    assert (goal['icon'], goal['colour']) == ('sprout', '#5fc6e8')
    changed = goals.edit(conn, goal['id'], dict(icon='target', colour='#ff8fb1'), today)
    assert (changed['icon'], changed['colour']) == ('target', '#ff8fb1')
    if kind == 'monthly':
        assert goals.tile(conn, today)['monthly']['icon'] == 'target'
    goals.edit(conn, goal['id'], {'archived': True}, today)
    archived = goals.payload(conn, today, archived=True)['archived'][0]
    assert (archived['icon'], archived['colour']) == ('target', '#ff8fb1')


@pytest.mark.parametrize('partial_column', ['icon', 'colour'])
def test_v17_resumes_a_partially_added_appearance(conn, partial_column):
    # Simulate a stop after either ALTER, with an existing goal and no version stamp.
    with conn:
        conn.execute('ALTER TABLE goals DROP COLUMN icon')
        conn.execute('ALTER TABLE goals DROP COLUMN colour')
        conn.execute(f'ALTER TABLE goals ADD COLUMN {partial_column} TEXT')
        conn.execute("INSERT INTO goals (kind,name,target,track,created_at,updated_at) "
                     "VALUES ('save','Trip',100,'manual','now','now')")
        conn.execute('PRAGMA user_version=16')
    db.init_schema(conn)
    assert tuple(conn.execute('SELECT icon,colour FROM goals').fetchone()) == ('pencil', '#4fd1a5')
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 19
