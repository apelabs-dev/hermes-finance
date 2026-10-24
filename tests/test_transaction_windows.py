"""Inclusive API windows share reads' parser, filtering and full-result totals."""
from datetime import date
import json

import pytest
import factory as f
from hermes_finance import api, config, features, reads, txmeta
from hermes_finance.web import App

TODAY = date(2026, 10, 21)


@pytest.fixture
def history(conn):
    f.bank(conn)
    f.acct(conn)
    for day, amount in [('2026-06-15', 1), ('2026-09-21', 2), ('2026-09-22', 3),
                        ('2026-10-14', 4), ('2026-10-15', 5), ('2026-10-21', 6), ('2026-10-22', 7)]:
        f.tx(conn, day, amount, 'Window shop', cat='Groceries', tid=day)
    return conn


@pytest.mark.parametrize('params,expected', [
    ({'month': '2026-10'}, ['2026-10-22', '2026-10-21', '2026-10-15', '2026-10-14']),
    ({'since': '7d'}, ['2026-10-21', '2026-10-15']),
    ({'since': '30d'}, ['2026-10-21', '2026-10-15', '2026-10-14', '2026-09-22']),
    ({'since': '2026-10-15', 'until': '2026-10-21'}, ['2026-10-21', '2026-10-15']),
    ({'since': '2026-10-21', 'until': '2026-10-21'}, ['2026-10-21']),
    ({'until': '2026-06-15'}, ['2026-06-15']),
    ({}, ['2026-10-22', '2026-10-21', '2026-10-15', '2026-10-14', '2026-09-22', '2026-09-21', '2026-06-15']),
])
def test_windows_and_reads_parity(history, params, expected):
    d = api.transactions(history, params, TODAY)
    period = reads.parse_period(TODAY, default='all', **params)
    r = reads.txns(history, reads.Filters(period), TODAY)
    assert [t['date'] for t in d['rows']] == expected == [t['date'] for t in r['rows']]
    assert d['total'] == r['total'] == len(expected)
    assert d['period'] == period.json()


@pytest.mark.parametrize('params', [
    {'month': '2026-10', 'since': '7d'}, {'month': '2026-10', 'until': '2026-10-21'},
    {'since': '0d'}, {'since': '3661d'}, {'since': '2026-02-30'}, {'until': 'yesterday'},
    {'since': '2026-10-22', 'until': '2026-10-21'},
])
def test_invalid_windows_return_400(history, params):
    app = App(config.db_path(), lambda: pytest.fail('provider call'), '127.0.0.1', 0)
    app.today = lambda: TODAY
    from urllib.parse import urlencode
    status, _, body = app.handle('GET', '/api/transactions?' + urlencode(params), {'Host': '127.0.0.1:0'}, b'')
    assert status == 400, body
    assert json.loads(body)['error']


def test_full_totals_and_months_ignore_pagination(history):
    for i in range(1700):
        f.tx(history, '2026-10-15', 0.25, 'Window shop', cat='Groceries', tid=f'bulk-{i}')
    for params in ({'since': '90d'}, {}):
        d = api.transactions(history, {**params, 'limit': '1', 'offset': '2'}, TODAY)
        full = api.transactions(history, params, TODAY)
        period = reads.parse_period(TODAY, default='all', **params)
        w, a = period.sql()
        amount = history.execute('SELECT SUM(v.amount) FROM v_transactions v WHERE ' + (' AND '.join(w) or '1'), a).fetchone()[0]
        assert len(d['rows']) == 1 and d['total'] == full['total'] > 1700
        assert d['sum'] == full['sum'] == amount
        assert d['months'] == full['months']
        assert d['days']['2026-10-15']['total'] == 430


def test_coverage_uses_latest_connected_first_month(history):
    assert api.transaction_coverage(history) == '2026-06'
    f.bank(history, 'b', 'Second bank'); f.acct(history, 'b-account', item_id='b')
    f.tx(history, '2026-08-12', 10, 'Other bank', account_id='b-account', item_id='b')
    f.bank(history, 'empty', 'Empty bank')
    assert api.transactions(history, {}, TODAY)['coverage_month'] == '2026-08'
    history.execute("UPDATE items SET removed_at = 'removed' WHERE item_id = 'b'")
    assert api.transaction_coverage(history) == '2026-06'
    history.execute('UPDATE transactions SET removed = 1')
    assert api.transaction_coverage(history) is None


def test_composed_filters_facets_saved_counts(history):
    txmeta.save_filter(history, 'Groceries', {'category': ['Groceries']})
    d = api.transactions(history, {'since': '7d', 'category': 'Groceries', 'q': 'Window', 'bank': 'Chase', 'kind': 'spend',
                                   'sort': 'amount_asc', 'facets': '1'}, TODAY)
    assert d['total'] == 2 and d['sum'] == 11
    assert d['facets']['categories']['Groceries'] == 2
    counts = features.saved_filters(history, {'since': '7d', 'window': '1'}, TODAY)
    assert counts['filters'][0]['count'] == 2
    assert features.saved_filters(history, {'month': '2026-09'}, TODAY)['filters'][0]['count'] == 2


def test_window_preserves_split_money_and_tags(history):
    tid = f.tx(history, '2026-10-15', 20, 'Split shop', tid='split-window')
    txmeta.split(history, tid, [{'category': 'Groceries', 'amount': 7}, {'category': 'Other Shopping', 'amount': 13}])
    txmeta.set_meta(history, tid, {'tags': ['window']})
    d = api.transactions(history, {'since': '7d', 'group': 'Food', 'tag': 'window', 'facets': '1'}, TODAY)
    assert d['total'] == 1 and d['sum'] == 7
    assert d['days']['2026-10-15']['total'] == d['months']['2026-10']['total'] == 7
    assert d['rows'][0]['amount'] == 20  # row still shows the complete payment
