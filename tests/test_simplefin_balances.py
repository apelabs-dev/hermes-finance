"""SimpleFIN balances come from the newest window only. A first sync (claim, confirm) and a reconnect import the newest
window and then backfill older ones; the older windows add transactions and must never overwrite the account's current
balance or today's balance snapshot (REPORT M-1: the oldest window, processed last, used to win)."""

from __future__ import annotations

from datetime import date, timedelta

from test_simplefin_sync import (PUBLIC, TODAY, Bridge, account_set, card, claimed, confirmed, epoch, simplefin_sync,
                                 tx)

NEWEST, OLDER = "114545.51", "115525.50"
ACCOUNT = "simplefin:CON-1:ACT-1"


def _by_window(q) -> dict:
    """A bridge whose balance is as of each window's end-date, and whose transactions sit inside that window."""
    end = int(q["end-date"])
    newest = end == epoch(TODAY + timedelta(days=1))
    stamp = date.fromtimestamp(end - 86400)
    row = tx(f"T-{stamp.isoformat()}", stamp, "-5.00", f"Shop {stamp.isoformat()}")
    return account_set([card(balance=NEWEST if newest else OLDER, txs=[row])])


def _balance(conn) -> float:
    return conn.execute("SELECT current_balance FROM accounts WHERE account_id = ?", (ACCOUNT,)).fetchone()[0]


def _snapshots(conn) -> list[float]:
    return [r[0] for r in conn.execute("SELECT current FROM balance_snapshots WHERE account_id = ?", (ACCOUNT,))]


def _txn_ids(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT transaction_id FROM transactions ORDER BY transaction_id")]


def _assert_newest(conn):
    assert _balance(conn) == float(NEWEST)
    assert _snapshots(conn) == [float(NEWEST)]               # one row for today, holding the newest balance


def test_a_first_sync_keeps_the_newest_windows_balance(conn):
    bridge = Bridge(_by_window)
    confirmed(conn, bridge)
    assert len(bridge.windows()) == 5
    _assert_newest(conn)
    assert len(_txn_ids(conn)) == 5                          # every window's transaction still landed


def test_a_reconnect_keeps_the_newest_windows_balance(conn):
    bridge = Bridge(_by_window)
    confirmed(conn, bridge)
    simplefin_sync.disconnect(conn)
    ids = _txn_ids(conn)
    confirmed(conn, bridge, minute=8)
    _assert_newest(conn)
    assert _txn_ids(conn) == ids                             # same ids: nothing doubled


def test_a_multi_window_sync_keeps_the_newest_windows_balance(conn):
    bridge = Bridge(_by_window)
    confirmed(conn, bridge)
    simplefin_sync._del(conn, simplefin_sync.LAST_SUCCESS_KEY)   # no success on record: the sync asks for the year
    before = len(bridge.windows())
    simplefin_sync.sync(conn, "daily", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert len(bridge.windows()) - before > 1
    _assert_newest(conn)


def test_a_second_sync_changes_nothing_but_the_balance_it_is_told(conn):
    bridge = Bridge(_by_window)
    confirmed(conn, bridge)
    ids = _txn_ids(conn)
    bridge.payload = account_set([card(balance="120000.00")])
    simplefin_sync._set(conn, simplefin_sync.LAST_SUCCESS_KEY, f"{TODAY - timedelta(days=1)}T12:00:00Z")
    simplefin_sync.sync(conn, "manual", transport=bridge, resolve=PUBLIC, today=TODAY, sleep=lambda s: None, jitter=0)
    assert _balance(conn) == 120000.0 and _snapshots(conn) == [120000.0]
    assert _txn_ids(conn) == ids
