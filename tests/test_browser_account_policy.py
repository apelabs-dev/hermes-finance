"""R3's manual-account aggregate in headless Chrome: a manual account the account policy leaves out is still manageable
but never counted by the sidebar's "Added by you" total or the Custom sheet's preview/rows. Isolated: one temp database,
one loopback server and one test-owned Chrome profile, all closed here."""

from __future__ import annotations

import json
import re
import time

import pytest

from cdp import Chrome, Server, find_chrome
from hermes_finance import db

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
MONEY = re.compile(r"([−-]?)\$([0-9][0-9,]*(?:\.[0-9]+)?)")


def money(text: str) -> float:
    m = MONEY.search(text or "")
    assert m, text
    v = float(m.group(2).replace(",", ""))
    return -v if m.group(1) else v


PAGE = r"""(() => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
  const panel = document.getElementById('accts-manual');
  const row = panel && panel.parentElement.querySelector('.row');
  return {
    total: row && T(row.querySelector('.v')),
    names: [...document.querySelectorAll('#accts-manual .ac')].map((r) => T(r.querySelector('span:not(.v)'))),
    tags: [...document.querySelectorAll('#accts-manual .ac .tag.nct')].map(T),
  };
})()"""

SHEET = r"""(() => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
  const s = document.getElementById('sheet');
  return {
    open: document.getElementById('modal').classList.contains('on') && s.classList.contains('nwsheet'),
    preview: T(s.querySelector('.nwprev .big')), ownowe: T(s.querySelector('.nwprev .ac')),
    count: T(s.querySelector('.nwcnt')), note: T(s.querySelector('.nwnote')),
    rows: [...s.querySelectorAll('.nwr')].map((r) => r.dataset.id),
  };
})()"""


def test_manual_accounts_agree_with_the_server_when_one_is_excluded_by_policy(tmp_path):
    path = tmp_path / "finance.db"
    conn = db.connect(path)
    ts = "2026-08-01T00:00:00Z"
    with conn:
        conn.execute("INSERT INTO manual_accounts (id, kind, name, institution, balance, balance_as_of, created_at, "
                     "updated_at) VALUES (1, 'bank', 'Excluded savings', 'Credit Union', 1000.0, '2026-08-01', ?, ?)",
                     (ts, ts))
        conn.execute("INSERT INTO manual_accounts (id, kind, name, institution, balance, balance_as_of, created_at, "
                     "updated_at) VALUES (2, 'bank', 'Counted savings', 'Credit Union', 100.0, '2026-08-01', ?, ?)",
                     (ts, ts))
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('networth_excluded', '[]', ?)",
                     (ts,))
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('excluded_accounts', "
                     "'[\"manual:1\"]', ?)", (ts,))
    conn.close()
    srv = Server(path)
    try:
        with Chrome() as b:
            b.size(1440, 900)
            b.goto(f"{srv.url}/?t={time.time():.3f}#/accounts")
            b.settle()
            page = b.js(PAGE)
            assert page["total"] == "$100"
            assert sorted(page["names"]) == ["Counted savings", "Excluded savings"]
            assert page["tags"] == ["Not counted"]

            b.js("document.querySelector('#page .nwseg button[data-nw=custom]').click()")
            b.settled()
            sheet = b.js(SHEET)
            assert sheet["open"] and money(sheet["preview"]) == 100.0
            assert "Own $100" in sheet["ownowe"] and "Owe $0" in sheet["ownowe"]
            assert sheet["count"] == "all 1 account counted"
            assert sheet["note"] == "1 account excluded by the account policy, so not counted here"
            assert sheet["rows"] == ["manual:2"]
    finally:
        srv.close()


def _custom_scene(path, source, today):
    """Visible 100 + hidden 1000, Custom leaving the hidden one out, the account policy hiding it too."""
    from hermes_finance import insights, manual
    conn = db.connect(path)
    try:
        if source == "manual":
            a = manual.create(conn, dict(kind="investment", subtype="brokerage", name="Visible brokerage",
                                         institution="Synthetic CU", balance=100.0,
                                         balance_as_of=today.isoformat()), today)
            b = manual.create(conn, dict(kind="investment", subtype="brokerage", name="Hidden brokerage",
                                         institution="Synthetic CU", balance=1000.0,
                                         balance_as_of=today.isoformat()), today)
            visible, hidden = f"manual:{a['id']}", f"manual:{b['id']}"
        else:
            from factory import acct, bank
            bank(conn)
            acct(conn, "acc_visible", "item_a", "Visible brokerage", type_="investment", subtype="brokerage",
                 current=100.0, mask="1111")
            acct(conn, "acc_hidden", "item_a", "Hidden brokerage", type_="investment", subtype="brokerage",
                 current=1000.0, mask="2222")
            visible, hidden = "acc_visible", "acc_hidden"
        insights.save_networth_excluded(conn, [hidden])
        insights.save_excluded_accounts(conn, [hidden])
    finally:
        conn.close()
    return visible, hidden


def _saved(path, key):
    from hermes_finance import insights
    conn = db.connect(path)
    try:
        return insights.networth_excluded(conn) if key == "custom" else insights.excluded_accounts(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("source", ["plaid", "manual"])
def test_custom_sheet_round_trips_keep_hidden_ids_and_never_write_a_no_op(tmp_path, source):
    from datetime import date
    from hermes_finance import api

    today = date(2026, 11, 5)
    path = tmp_path / "finance.db"
    visible, hidden = _custom_scene(path, source, today)
    srv = Server(path, today=today)
    try:
        with Chrome() as b:
            b.size(1440, 1000)
            b.goto(f"{srv.url}/?t={time.time():.3f}#/accounts")
            b.settled()

            def settings():
                return b.js("fetch('/api/settings').then(r=>r.json())")

            def open_sheet():
                b.js("document.querySelector('#page .nwseg button[data-nw=custom]').click()")
                b.settled()
                return b.js(SHEET)

            def close_sheet():
                b.js("document.querySelector('.nwsheet .nwhead .shd button').click()")
                b.settled()

            # 1. a no-op open/close writes nothing
            before = settings()["networth_excluded"]
            sheet = open_sheet()
            assert sheet["rows"] == [visible]
            close_sheet()
            assert settings()["networth_excluded"] == before == [hidden]

            # 2. a preset press changes only visible rows and keeps the hidden preference
            open_sheet()
            b.js("[...document.querySelectorAll('.nwsheet .fchip')]"
                 ".find((x) => x.textContent === 'Cash & debts').click()")
            close_sheet()
            assert sorted(settings()["networth_excluded"]) == sorted([visible, hidden])
            assert sorted(_saved(path, "custom")) == sorted([visible, hidden])

            # reset Custom to only the hidden account, through the server, then reload the page
            conn = db.connect(path)
            try:
                api.save_settings(conn, {"networth_excluded": [hidden]})
            finally:
                conn.close()
            b.goto(f"{srv.url}/?t={time.time():.3f}#/accounts")
            b.settled()

            # 3. an ordinary visible-switch edit keeps the hidden id
            open_sheet()
            b.js("document.querySelector('.nwsheet .nwr [role=switch]').click()")
            close_sheet()
            assert sorted(settings()["networth_excluded"]) == sorted([visible, hidden])
            assert sorted(_saved(path, "custom")) == sorted([visible, hidden])

            # 4. clearing the policy restores the stored Custom total (both accounts off -> $0)
            conn = db.connect(path)
            try:
                api.save_settings(conn, {"excluded_accounts": []})
            finally:
                conn.close()
            b.goto(f"{srv.url}/?t={time.time():.3f}#/accounts")
            b.settled()
            sheet = open_sheet()
            assert sorted(sheet["rows"]) == sorted([visible, hidden])
            assert sheet["count"] == "2 of 2 accounts not counted"
            assert money(sheet["preview"]) == 0.0
            assert "Own $0" in sheet["ownowe"]
            close_sheet()
            assert sorted(settings()["networth_excluded"]) == sorted([visible, hidden])
    finally:
        srv.close()


def _preset_scene(path, source, exclude_all, today):
    """Visible savings/cash 100 + brokerage 1000 that the account policy hides (not a switch row)."""
    from hermes_finance import insights, manual
    conn = db.connect(path)
    try:
        if source == "manual":
            a = manual.create(conn, dict(kind="bank", subtype="savings", name="Visible savings",
                                         institution="Synthetic CU", balance=100.0,
                                         balance_as_of=today.isoformat()), today)
            b = manual.create(conn, dict(kind="investment", subtype="brokerage", name="Hidden brokerage",
                                         institution="Synthetic CU", balance=1000.0,
                                         balance_as_of=today.isoformat()), today)
            visible, hidden = f"manual:{a['id']}", f"manual:{b['id']}"
        else:
            from factory import acct, bank
            bank(conn)
            acct(conn, "acc_visible", "item_a", "Visible savings", current=100.0, mask="1111")
            acct(conn, "acc_hidden", "item_a", "Hidden brokerage", type_="investment", subtype="brokerage",
                 current=1000.0, mask="2222")
            visible, hidden = "acc_visible", "acc_hidden"
        insights.save_networth_excluded(conn, [visible, hidden] if exclude_all else [])
        insights.save_excluded_accounts(conn, [hidden])
    finally:
        conn.close()
    return visible, hidden


def _custom_assets(path):
    from hermes_finance import insights
    conn = db.connect(path)
    try:
        return insights.net_worth(conn, history=False)["custom"]["assets"]
    finally:
        conn.close()


def _open_sheet(b):
    b.js("document.querySelector('#page .nwseg button[data-nw=custom]').click()")
    b.settled()
    return b.js(SHEET)


def _tap_preset(b, label):
    b.js("[...document.querySelectorAll('.nwsheet .fchip')]"
         f".find((x) => x.textContent === {json.dumps(label)}).click()")
    b.settled()
    return b.js("[...document.querySelectorAll('.nwsheet .fchip')]"
                ".filter((x) => x.getAttribute('aria-pressed') === 'true').map((x) => x.textContent)")


def _close_sheet(b):
    b.js("document.querySelector('.nwsheet .nwhead .shd button').click()")
    b.settled()


@pytest.mark.parametrize("source", ["plaid", "manual"])
def test_custom_sheet_cash_and_debts_preset_is_absolute_over_policy_hidden(tmp_path, source):
    from datetime import date
    from hermes_finance import api

    today = date(2026, 11, 5)
    path = tmp_path / f"cash-{source}.db"
    visible, hidden = _preset_scene(path, source, False, today)
    srv = Server(path, today=today)
    try:
        with Chrome() as b:
            b.size(1440, 1000)
            b.goto(f"{srv.url}/?t={time.time():.3f}#/accounts")
            b.settled()
            assert b.js("fetch('/api/settings').then(r=>r.json())")["networth_excluded"] == []
            sheet = _open_sheet(b)
            assert sheet["rows"] == [visible]
            assert money(sheet["preview"]) == _custom_assets(path) == 100.0

            pressed = _tap_preset(b, "Cash & debts")
            assert "Cash & debts" in pressed
            sheet = b.js(SHEET)
            preview = money(sheet["preview"])
            assert preview == 100.0                    # hidden id is already policy-hidden
            _close_sheet(b)
            assert b.js("fetch('/api/settings').then(r=>r.json())")["networth_excluded"] == [hidden]
            assert _custom_assets(path) == preview == 100.0
            assert b.problems() == []
    finally:
        srv.close()

    conn = db.connect(path)
    try:
        api.save_settings(conn, {"excluded_accounts": []})
        from hermes_finance import insights
        assert insights.net_worth(conn, history=False)["custom"]["assets"] == 100.0
    finally:
        conn.close()


@pytest.mark.parametrize("source", ["plaid", "manual"])
def test_custom_sheet_everything_preset_is_absolute_over_policy_hidden(tmp_path, source):
    from datetime import date
    from hermes_finance import api

    today = date(2026, 11, 5)
    path = tmp_path / f"everything-{source}.db"
    visible, hidden = _preset_scene(path, source, True, today)

    srv = Server(path, today=today)
    try:
        with Chrome() as b:
            b.size(1440, 1000)
            b.goto(f"{srv.url}/?t={time.time():.3f}#/accounts")
            b.settled()
            assert sorted(b.js("fetch('/api/settings').then(r=>r.json())")["networth_excluded"]) \
                == sorted([visible, hidden])
            sheet = _open_sheet(b)
            assert sheet["rows"] == [visible]
            assert money(sheet["preview"]) == _custom_assets(path) == 0.0

            pressed = _tap_preset(b, "Everything")
            assert "Everything" in pressed
            sheet = b.js(SHEET)
            preview = money(sheet["preview"])
            assert preview == 100.0                    # hidden id is already policy-hidden
            _close_sheet(b)
            assert b.js("fetch('/api/settings').then(r=>r.json())")["networth_excluded"] == []
            assert _custom_assets(path) == preview == 100.0
            assert b.problems() == []
    finally:
        srv.close()

    conn = db.connect(path)
    try:
        from hermes_finance import insights
        api.save_settings(conn, {"excluded_accounts": []})
        assert insights.net_worth(conn, history=False)["custom"]["assets"] == 1100.0
    finally:
        conn.close()
