"""Gate fixtures must make the states they promise reachable across calendar boundaries."""
from datetime import date

import pytest

import gate_dbs
from hermes_finance import db, rulebook


@pytest.mark.parametrize('today', [date(2026, 10, 1), date(2026, 10, 2), date(2027, 1, 1)])
def test_empty_fixture_keeps_transaction_sheet_rows_in_current_month(tmp_path, today):
    path = gate_dbs.build('empty', tmp_path / 'finance.db', today)
    conn = db.connect(path)
    try:
        dates = [row[0] for row in conn.execute('SELECT date FROM transactions')]
        assert len(dates) == 3
        assert all(day.startswith(today.strftime('%Y-%m')) and day <= today.isoformat() for day in dates)
        assert not rulebook.suggestions(conn)
    finally:
        conn.close()


@pytest.mark.parametrize('kind', [kind for kind in gate_dbs.KINDS if kind != 'empty'])
def test_rule_suggestions_reachable_without_mocked_api(tmp_path, kind):
    path = gate_dbs.build(kind, tmp_path / 'finance.db', date(2026, 10, 1))
    conn = db.connect(path)
    try:
        assert rulebook.suggestions(conn)
    finally:
        conn.close()
