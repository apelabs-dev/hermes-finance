"""P2b: October synthetic spending reconciles with P1 filters, including split payments."""
from datetime import date

import pytest
import demo_data
from hermes_finance import api, insights, txmeta

TODAY = date(2026, 10, 20)


@pytest.mark.parametrize('split', [False, True])
def test_october_groups_reconcile_with_transactions(conn, split):
    demo_data.build(conn, TODAY)
    if split:
        row = conn.execute("SELECT transaction_id, amount FROM v_transactions WHERE month = '2026-10' "
                           "AND category = 'Groceries' AND pending = 0 AND amount > 10 LIMIT 1").fetchone()
        txmeta.split(conn, row['transaction_id'], [
            {'category': 'Groceries', 'amount': 3}, {'category': 'Eating Out', 'amount': 2},
            {'category': 'Other Shopping', 'amount': round(row['amount'] - 5, 2)}])
    ov = api.overview(conn, {'month': '2026-10'}, TODAY)
    assert len(ov['spend_groups']) == 8
    assert sum(g['spend'] for g in ov['spend_groups']) == pytest.approx(ov['summary']['mtd'])
    for g in ov['spend_groups']:
        tx = api.transactions(conn, {'month': '2026-10', 'group': g['group']}, TODAY)
        assert (g['spend'], g['count']) == (tx['sum'], tx['total'])
        assert sum(c['spend'] for c in g['categories']) == pytest.approx(g['spend'])
        for c in g['categories']:
            filtered = api.transactions(conn, {'month': '2026-10', 'category': c['category']}, TODAY)
            assert (c['spend'], c['count']) == (filtered['sum'], filtered['total'])
    food = next(g for g in ov['spend_groups'] if g['group'] == 'Food')
    assert next(c for c in food['categories'] if c['category'] == 'Eating Out')['spend'] > 0
    if split:
        assert sum(c['count'] for c in food['categories']) > food['count']
    # Chart exclusions never change totals.
    api.save_settings(conn, {'chart_excluded_categories': ['Eating Out']})
    assert insights.group_spend(conn, '2026-10', ov['categories']) == ov['spend_groups']
