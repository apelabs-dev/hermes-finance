"""A posted transaction retires the pending one it names (pending_transaction_id): same Item, same account only.

Plaid sends the posted row in `added` with pending_transaction_id and the pending id in `removed`, but the removal can
come in a later sync or not at all; until then both rows were visible and both counted. Everything here is synthetic
(ids, banks, merchants) and runs on the mocked Plaid transport.
"""

from __future__ import annotations

import itertools

import pytest

from conftest import TOKEN_A, TOKEN_B, account, add_item, page, txn
from hermes_finance import classify, sync as sync_mod
from hermes_finance.sync import sync_all

SYNC = "/transactions/sync"
ITEM, ITEM_B = "item_synth_a", "item_synth_b"
CHK, CARD, CHK_B = "acc_synth_chk", "acc_synth_card", "acc_synth_b_chk"
ACCOUNTS_A = [account(CHK, "Synth Checking", "0001"),
              account(CARD, "Synth Card", "0002", "credit", "credit card", 50.0, 950.0, 1000.0)]
ACCOUNTS_B = [account(CHK_B, "Synth B Checking", "0003")]
MONTH = "2026-09"


def tx(tid, amount=12.34, account_id=CHK, pending=False, pending_id=None, date="2026-09-03"):
    return txn(tid, amount, name="SYNTH CAFE 0042", merchant="Synth Cafe", date=date,
               pfc=("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"), account_id=account_id, pending=pending,
               pending_id=pending_id)


@pytest.fixture
def bank(conn, fake):
    add_item(conn, ITEM, "ins_synth_1", name="Synth Bank A", token=TOKEN_A)
    fake.on("/accounts/get", {"accounts": ACCOUNTS_A})
    return conn


def sync(conn, client, fake, added=(), modified=(), removed=(), accounts=ACCOUNTS_A):
    fake.on(SYNC, page(added, modified, removed, accounts=accounts))
    results = sync_all(conn, client)
    assert results and all(r.ok for r in results)
    return results


def row(conn, tid):
    return conn.execute("SELECT * FROM transactions WHERE transaction_id = ?", (tid,)).fetchone()


def visible(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT transaction_id FROM v_transactions")}


def spend(conn) -> tuple:
    """(lines, total) as the spending pages read them, and (count, total) from the monthly category view."""
    lines = conn.execute("SELECT COUNT(*), ROUND(COALESCE(SUM(spend), 0), 2) FROM v_spend_lines WHERE month = ?",
                         (MONTH,)).fetchone()
    monthly = conn.execute("SELECT COALESCE(SUM(txn_count), 0), ROUND(COALESCE(SUM(spend), 0), 2) "
                           "FROM v_monthly_category_spend WHERE month = ?", (MONTH,)).fetchone()
    return tuple(lines), tuple(monthly)


def category(conn, tid):
    r = conn.execute("SELECT c.source, cat.name FROM classifications c JOIN categories cat ON cat.id = c.category_id "
                     "WHERE c.transaction_id = ?", (tid,)).fetchone()
    return tuple(r) if r else None


def kept(r) -> dict:
    """A row's Plaid data: everything but the removed flag and its timestamp."""
    return {k: r[k] for k in r.keys() if k not in ("removed", "updated_at")}


@pytest.fixture
def ticking(monkeypatch):
    """sync.now() moves one second per call, so a second write to a row always changes its updated_at."""
    tick = itertools.count(1)
    monkeypatch.setattr(sync_mod, "now", lambda: f"2026-09-30T{(n := next(tick)) // 3600:02d}:{n // 60 % 60:02d}:"
                                                 f"{n % 60:02d}Z")


# --- the bug: posted with no removal event for its pending --------------------------------------------------------

def test_posted_retires_its_pending_without_a_removal_event(bank, client, fake):
    sync(bank, client, fake, added=[tx("p_synth_1", 42.0, pending=True)])
    classify.set_category(bank, "p_synth_1", "Other Health", "user")
    before = kept(row(bank, "p_synth_1"))
    assert visible(bank) == {"p_synth_1"}

    sync(bank, client, fake, added=[tx("t_synth_1", 42.0, pending_id="p_synth_1", date="2026-09-05")])

    p = row(bank, "p_synth_1")
    assert p["removed"] == 1                                   # soft: the row is kept ...
    assert kept(p) == before                                   # ... with its data untouched (still pending=1)
    assert category(bank, "p_synth_1") == ("user", "Other Health")   # ... and its decision kept
    assert category(bank, "t_synth_1") == ("user", "Other Health")   # carried to the posted one, as before
    assert row(bank, "t_synth_1")["removed"] == 0
    assert visible(bank) == {"t_synth_1"}
    assert spend(bank) == ((1, 42.0), (1, 42.0))               # counted once


def test_spend_counts_only_the_posted_row(bank, client, fake):
    """Pending 40.00 authorized, posted 43.21 with a tip: the total is the posted amount, not both."""
    sync(bank, client, fake, added=[tx("p_synth_tip", 40.0, pending=True)])
    assert spend(bank) == ((1, 40.0), (1, 40.0))
    sync(bank, client, fake, added=[tx("t_synth_tip", 43.21, pending_id="p_synth_tip")])
    assert spend(bank) == ((1, 43.21), (1, 43.21))
    assert bank.execute("SELECT COUNT(*) FROM v_spend_lines WHERE pending = 1").fetchone()[0] == 0


def test_the_usual_removal_event_still_works(bank, client, fake):
    sync(bank, client, fake, added=[tx("p_synth_2", pending=True)])
    sync(bank, client, fake, added=[tx("t_synth_2", pending_id="p_synth_2")], removed=["p_synth_2"])
    assert row(bank, "p_synth_2")["removed"] == 1
    assert visible(bank) == {"t_synth_2"}


def test_retirement_is_idempotent(bank, client, fake, ticking):
    sync(bank, client, fake, added=[tx("p_synth_3", 9.5, pending=True)])
    classify.set_category(bank, "p_synth_3", "Groceries", "ai")
    posted = tx("t_synth_3", 9.5, pending_id="p_synth_3")
    sync(bank, client, fake, added=[posted])
    retired_at = row(bank, "p_synth_3")["updated_at"]
    state = kept(row(bank, "p_synth_3"))

    for step in ({"modified": [posted]}, {"added": [posted]}, {"modified": [posted]}):
        sync(bank, client, fake, **step)
        p = row(bank, "p_synth_3")
        assert (p["removed"], p["updated_at"]) == (1, retired_at)   # the posted row sent again never rewrites it
        assert kept(p) == state
    sync(bank, client, fake, removed=["p_synth_3"])                 # Plaid's own removal, late: stamps it, as always
    assert row(bank, "p_synth_3")["removed"] == 1 and kept(row(bank, "p_synth_3")) == state

    assert visible(bank) == {"t_synth_3"}
    assert category(bank, "t_synth_3") == ("ai", "Groceries")
    assert bank.execute("SELECT COUNT(*) FROM classifications WHERE transaction_id IN ('p_synth_3', 't_synth_3')"
                        ).fetchone()[0] == 2
    assert spend(bank) == ((1, 9.5), (1, 9.5))


# --- only an explicit, same-Item, same-account posted -> pending link retires -----------------------------------

def test_link_to_another_account_retires_nothing(bank, client, fake):
    sync(bank, client, fake, added=[tx("p_synth_chk", 20.0, pending=True)])
    sync(bank, client, fake, added=[tx("t_synth_card", 20.0, account_id=CARD, pending_id="p_synth_chk")])
    assert row(bank, "p_synth_chk")["removed"] == 0
    sync(bank, client, fake, modified=[tx("p_synth_chk", 20.0, pending=True)])                # sent again after
    assert row(bank, "p_synth_chk")["removed"] == 0
    assert visible(bank) == {"p_synth_chk", "t_synth_card"}


def test_link_to_another_item_retires_nothing(bank, client, fake):
    add_item(bank, ITEM_B, "ins_synth_2", name="Synth Bank B", token=TOKEN_B)
    fake.on("/accounts/get", lambda b: {"accounts": ACCOUNTS_A if b["access_token"] == TOKEN_A else ACCOUNTS_B})
    pages = {TOKEN_A: page([tx("p_synth_a", 15.0, pending=True)], accounts=ACCOUNTS_A),
             TOKEN_B: page(accounts=ACCOUNTS_B)}
    fake.on(SYNC, lambda b: pages[b["access_token"]])
    sync_all(bank, client)

    pages[TOKEN_A] = page(accounts=ACCOUNTS_A)
    pages[TOKEN_B] = page([tx("t_synth_b", 15.0, account_id=CHK_B, pending_id="p_synth_a")], accounts=ACCOUNTS_B)
    assert all(r.ok for r in sync_all(bank, client))

    assert row(bank, "t_synth_b")["item_id"] == ITEM_B
    assert row(bank, "p_synth_a")["removed"] == 0
    assert visible(bank) == {"p_synth_a", "t_synth_b"}


def test_same_account_id_under_another_item_retires_nothing(bank, client, fake):
    """The item check stands on its own: the pending's row still says item A while the posted one says item B."""
    add_item(bank, ITEM_B, "ins_synth_2", name="Synth Bank B", token=TOKEN_B)
    pages = {TOKEN_A: page([tx("p_synth_x", 7.0, pending=True)], accounts=ACCOUNTS_A),
             TOKEN_B: page(accounts=[])}
    fake.on(SYNC, lambda b: pages[b["access_token"]])
    fake.on("/accounts/get", lambda b: {"accounts": ACCOUNTS_A if b["access_token"] == TOKEN_A else []})
    sync_all(bank, client)

    pages[TOKEN_A] = page(accounts=ACCOUNTS_A)
    pages[TOKEN_B] = page([tx("t_synth_x", 7.0, account_id=CHK, pending_id="p_synth_x")], accounts=[])
    sync_all(bank, client)
    assert row(bank, "p_synth_x")["removed"] == 0
    pages[TOKEN_A] = page(modified=[tx("p_synth_x", 7.0, pending=True)], accounts=ACCOUNTS_A)  # sent again after
    pages[TOKEN_B] = page(accounts=[])
    sync_all(bank, client)

    assert (row(bank, "p_synth_x")["item_id"], row(bank, "t_synth_x")["item_id"]) == (ITEM, ITEM_B)
    assert row(bank, "t_synth_x")["account_id"] == row(bank, "p_synth_x")["account_id"] == CHK
    assert row(bank, "p_synth_x")["removed"] == 0
    assert visible(bank) == {"p_synth_x", "t_synth_x"}


def test_self_reference_never_hides_the_row(bank, client, fake):
    sync(bank, client, fake, added=[tx("t_synth_self", 11.0, pending_id="t_synth_self"),
                                    tx("p_synth_self", 12.0, pending=True, pending_id="p_synth_self")])
    sync(bank, client, fake, modified=[tx("t_synth_self", 11.0, pending_id="t_synth_self"),
                                       tx("p_synth_self", 12.0, pending=True, pending_id="p_synth_self")])
    assert row(bank, "t_synth_self")["removed"] == row(bank, "p_synth_self")["removed"] == 0
    assert visible(bank) == {"t_synth_self", "p_synth_self"}


def test_pending_to_pending_and_posted_to_posted_links_retire_nothing(bank, client, fake):
    sync(bank, client, fake, added=[tx("p_synth_old", 30.0, pending=True), tx("t_synth_old", 31.0)])
    sync(bank, client, fake, added=[tx("p_synth_new", 30.0, pending=True, pending_id="p_synth_old"),
                                    tx("t_synth_new", 31.0, pending_id="t_synth_old")])
    assert visible(bank) == {"p_synth_old", "p_synth_new", "t_synth_old", "t_synth_new"}
    # and the other way round: a row sent again after one linking to it stays too
    sync(bank, client, fake, modified=[tx("p_synth_old", 30.0, pending=True), tx("t_synth_old", 31.0)])
    assert visible(bank) == {"p_synth_old", "p_synth_new", "t_synth_old", "t_synth_new"}
    assert spend(bank) == ((4, 122.0), (4, 122.0))


def test_same_amount_purchases_are_never_matched(bank, client, fake):
    """Two identical coffees the same day, both pending. One posts naming its own pending: only that one goes."""
    sync(bank, client, fake, added=[tx("p_synth_cup1", 5.75, pending=True), tx("p_synth_cup2", 5.75, pending=True)])
    sync(bank, client, fake, added=[tx("t_synth_cup2", 5.75, pending_id="p_synth_cup2"),
                                    tx("t_synth_cup3", 5.75),                                # no link at all
                                    tx("t_synth_cup4", 5.75, pending_id="p_synth_unknown")])  # link to nothing here
    assert row(bank, "p_synth_cup2")["removed"] == 1
    assert row(bank, "p_synth_cup1")["removed"] == 0
    assert visible(bank) == {"p_synth_cup1", "t_synth_cup2", "t_synth_cup3", "t_synth_cup4"}
    assert spend(bank) == ((4, 23.0), (4, 23.0))


# --- order: the posted row first, or the old pending coming back -------------------------------------------------

def test_posted_first_then_its_pending_arrives_retired(bank, client, fake):
    sync(bank, client, fake, added=[tx("t_synth_early", 18.0, pending_id="p_synth_late")])
    assert visible(bank) == {"t_synth_early"}

    sync(bank, client, fake, added=[tx("p_synth_late", 18.0, pending=True)])

    p = row(bank, "p_synth_late")
    assert p is not None and p["removed"] == 1 and p["pending"] == 1 and p["amount"] == 18.0   # stored, not shown
    assert visible(bank) == {"t_synth_early"}
    assert spend(bank) == ((1, 18.0), (1, 18.0))


def test_posted_and_pending_in_one_batch_either_order(bank, client, fake):
    sync(bank, client, fake, added=[tx("t_synth_b1", 3.0, pending_id="p_synth_b1"),
                                    tx("p_synth_b1", 3.0, pending=True),
                                    tx("p_synth_b2", 4.0, pending=True),
                                    tx("t_synth_b2", 4.0, pending_id="p_synth_b2")])
    assert row(bank, "p_synth_b1")["removed"] == row(bank, "p_synth_b2")["removed"] == 1
    assert visible(bank) == {"t_synth_b1", "t_synth_b2"}


def test_old_pending_reappearing_after_its_removal_stays_retired(bank, client, fake):
    sync(bank, client, fake, added=[tx("p_synth_back", 25.0, pending=True)])
    sync(bank, client, fake, added=[tx("t_synth_back", 25.0, pending_id="p_synth_back")], removed=["p_synth_back"])

    sync(bank, client, fake, modified=[tx("p_synth_back", 25.0, pending=True)])   # stale copy, later
    assert row(bank, "p_synth_back")["removed"] == 1
    sync(bank, client, fake, added=[tx("p_synth_back", 25.0, pending=True)])
    assert row(bank, "p_synth_back")["removed"] == 1
    assert visible(bank) == {"t_synth_back"}
    assert spend(bank) == ((1, 25.0), (1, 25.0))


def test_old_pending_stays_retired_after_its_posted_row_is_removed(bank, client, fake):
    """The link is what retires it: Plaid removing the posted row later does not bring the pending back."""
    sync(bank, client, fake, added=[tx("p_synth_gone", 6.0, pending=True)])
    sync(bank, client, fake, added=[tx("t_synth_gone", 6.0, pending_id="p_synth_gone")])
    sync(bank, client, fake, removed=["t_synth_gone"])
    sync(bank, client, fake, modified=[tx("p_synth_gone", 6.0, pending=True)])
    assert row(bank, "p_synth_gone")["removed"] == row(bank, "t_synth_gone")["removed"] == 1
    assert visible(bank) == set()


def test_pending_linked_from_another_account_comes_back_as_before(bank, client, fake):
    """A removed pending with no same-account posted row naming it returns when Plaid sends it again (unchanged)."""
    sync(bank, client, fake, added=[tx("t_synth_elsewhere", 8.0, account_id=CARD, pending_id="p_synth_again")])
    sync(bank, client, fake, added=[tx("p_synth_again", 8.0, pending=True)])
    assert row(bank, "p_synth_again")["removed"] == 0
    sync(bank, client, fake, removed=["p_synth_again"])
    sync(bank, client, fake, modified=[tx("p_synth_again", 8.0, pending=True)])
    assert row(bank, "p_synth_again")["removed"] == 0
    assert visible(bank) == {"t_synth_elsewhere", "p_synth_again"}


# --- classification carry-over, unchanged ------------------------------------------------------------------------

def test_carry_over_keeps_its_rules(bank, client, fake):
    sync(bank, client, fake, added=[tx("p_synth_u", pending=True), tx("p_synth_plaid", pending=True),
                                    tx("p_synth_own", pending=True)])
    classify.set_category(bank, "p_synth_u", "Other Health", "user")
    classify.set_category(bank, "p_synth_own", "Other Health", "user")
    # p_synth_plaid keeps its Plaid default (classify.apply): not a decision, so not carried as one
    sync(bank, client, fake, added=[tx("t_synth_own", pending_id=None)])
    classify.set_category(bank, "t_synth_own", "Groceries", "user")

    sync(bank, client, fake,
         added=[tx("t_synth_u", pending_id="p_synth_u"), tx("t_synth_plaid", pending_id="p_synth_plaid")],
         modified=[tx("t_synth_own", pending_id="p_synth_own")])

    assert category(bank, "t_synth_u") == ("user", "Other Health")
    assert category(bank, "t_synth_plaid")[0] == "plaid"
    assert category(bank, "t_synth_own") == ("user", "Groceries")      # its own decision is never overwritten
    assert [row(bank, tid)["removed"] for tid in ("p_synth_u", "p_synth_plaid", "p_synth_own")] == [1, 1, 1]
    assert visible(bank) == {"t_synth_u", "t_synth_plaid", "t_synth_own"}


# --- the one-off repair for rows synced before this fix (documented in the report; never run by the app) ----------

REPAIR_PREVIEW_SQL = """
SELECT p.transaction_id, s.transaction_id AS posted_id, p.item_id, p.account_id, p.date, p.amount
FROM transactions p
JOIN transactions s ON s.pending_transaction_id = p.transaction_id
WHERE p.removed = 0 AND p.pending = 1 AND p.source = 'plaid'
  AND s.removed = 0 AND s.pending = 0 AND s.source = 'plaid'
  AND s.transaction_id <> p.transaction_id
  AND s.item_id = p.item_id AND s.account_id = p.account_id
ORDER BY p.transaction_id
"""

REPAIR_SQL = """
UPDATE transactions SET removed = 1, updated_at = :ts
WHERE removed = 0 AND pending = 1 AND source = 'plaid'
  AND EXISTS (SELECT 1 FROM transactions s
              WHERE s.pending_transaction_id = transactions.transaction_id
                AND s.removed = 0 AND s.pending = 0 AND s.source = 'plaid'
                AND s.transaction_id <> transactions.transaction_id
                AND s.item_id = transactions.item_id AND s.account_id = transactions.account_id)
"""


def test_repair_sql_retires_exactly_the_linked_pending_rows(bank):
    # the state a sync before this fix left: every row visible (the old code never retired anything)
    rows =[tx("p_r_linked", 10.0, pending=True), tx("t_r_linked", 10.0, pending_id="p_r_linked"),
            tx("p_r_card", 11.0, pending=True), tx("t_r_card", 11.0, account_id=CARD, pending_id="p_r_card"),
            tx("t_r_self", 12.0, pending_id="t_r_self"),
            tx("p_r_pp1", 13.0, pending=True), tx("p_r_pp2", 13.0, pending=True, pending_id="p_r_pp1"),
            tx("t_r_pt1", 14.0), tx("t_r_pt2", 14.0, pending_id="t_r_pt1"),
            tx("p_r_same", 10.0, pending=True)]                                       # same amount, no link
    ts = "2026-09-30T00:00:00Z"
    with bank:
        sync_mod.upsert_accounts(bank, ITEM, ACCOUNTS_A)
        for t in rows:
            sync_mod.upsert_transaction(bank, ITEM, t, ts)
        bank.execute("UPDATE transactions SET removed = 0")                            # undo this fix's own work
    assert len(visible(bank)) == len(rows)
    before = {r["transaction_id"]: kept(r) for r in bank.execute("SELECT * FROM transactions")}

    preview = [r[0] for r in bank.execute(REPAIR_PREVIEW_SQL)]
    assert preview == ["p_r_linked"]
    with bank:
        n = bank.execute(REPAIR_SQL, {"ts": "2026-09-30T12:00:00Z"}).rowcount
    assert n == len(preview) == 1
    assert visible(bank) == {r["transaction_id"] for r in rows} - {"p_r_linked"}
    assert {r["transaction_id"]: kept(r) for r in bank.execute("SELECT * FROM transactions")} == before
    with bank:
        assert bank.execute(REPAIR_SQL, {"ts": "2026-09-30T12:00:01Z"}).rowcount == 0   # a second run is a no-op
    assert list(bank.execute(REPAIR_PREVIEW_SQL)) == []
