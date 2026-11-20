"""Multi-currency UI (t_3f83e261) in headless Chrome: the Other currencies card, per-currency day totals,
the Budgets "also" line and the manual-account currency picker, at 390 and 1280 in both themes; goal cards in their
account's currency, foreign Investments and Loans & debt rows apart and in their own currency, and a CSV imported a
second time into the accounts its first import made."""

from __future__ import annotations

import base64
import json
import time
from datetime import date
from pathlib import Path

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import classify, db, goals, insights

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")
TODAY = date.today()
TS = f"{TODAY.isoformat()}T00:00:00Z"
FIX = Path(__file__).parent / "fixtures" / "import"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = gate_dbs.build("full", tmp_path_factory.mktemp("browser_currency") / "finance.db", TODAY)
    conn = db.connect(path)
    item_id = conn.execute("SELECT item_id FROM items WHERE removed_at IS NULL LIMIT 1").fetchone()[0]
    with conn:
        for aid, name, cur, bal in (("cur_eur", "N26 Girokonto", "EUR", 3420.00),
                                    ("cur_krw", "Seoul checking", "KRW", 1200000.00)):
            conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                         "available_balance, iso_currency, updated_at) VALUES (?, ?, ?, ?, 'depository', 'checking', "
                         "?, ?, ?, ?)", (aid, item_id, name, aid[-4:], bal, bal, cur, TS))
        # a brokerage and a card in euros: listed on Investments / Loans & debt, in no total there
        conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                     "available_balance, iso_currency, updated_at) VALUES ('cur_inv', ?, 'Euro brokerage', '4401', "
                     "'investment', 'brokerage', 5000, 5000, 'EUR', ?)", (item_id, TS))
        conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, "
                     "available_balance, credit_limit, iso_currency, updated_at) VALUES ('cur_card', ?, 'Euro card', "
                     "'4402', 'credit', 'credit card', 250, 1750, 2000, 'EUR', ?)", (item_id, TS))
        for tid, aid, amount, cur, name in (("cur_usd_today", "acc_nova_chk", 133.18, "USD", "Corner shop"),
                                            ("cur_eur_today", "cur_eur", 23.80, "EUR", "REWE Markt"),
                                            ("cur_krw_today", "cur_krw", 12000.00, "KRW", "Seoul market")):
            conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, "
                         "amount, iso_currency, pending, pfc_primary, pfc_detailed, removed, first_seen_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'FOOD_AND_DRINK', 'FOOD_AND_DRINK_GROCERIES', 0, ?, ?)",
                         (tid, aid, item_id, TODAY.isoformat(), name, name, amount, cur, TS, TS))
        conn.execute("UPDATE categories SET monthly_cap = 500 WHERE name = 'Groceries'")
    classify.apply(conn)
    conn.close()
    srv = Server(path, today=TODAY)
    srv.path = path
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def chrome():
    with Chrome() as b:
        yield b


def go(b, site, page, width, theme="dark"):
    b.size(width, 900 if width > 800 else 844)
    b.goto(f"{site.url}/?t={time.time():.3f}#/{page}")
    b.settle()
    b.js(f"document.documentElement.dataset.theme = {json.dumps(theme)}; 1")
    b.js("new Promise((r) => setTimeout(r, 180))")
    assert b.problems() == [], (width, theme, page)
    assert b.js("document.documentElement.scrollWidth <= innerWidth + 1"), (width, theme, page)


def test_accounts_other_currencies_card_at_390_and_1280_in_both_themes(site, chrome):
    for width in (390, 1280):
        for theme in ("dark", "light"):
            go(chrome, site, "accounts", width, theme)
            card = chrome.js(r"""(() => {
              const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
              const sec = [...document.querySelectorAll('#page .acctgrp')].find((c) => T(c.querySelector('h3 .ttl')) === 'Other currencies');
              if (!sec) return null;
              return {title: T(sec.querySelector('h3 .ttl')),
                header: T(sec.querySelector('h3 small')), off: T(sec.querySelector('h3 .offtag')),
                rows: [...sec.querySelectorAll('.li.acct')].map((r) => ({name: T(r.querySelector('.nm')), tag: T(r.querySelector('.tag')),
                  amount: T(r.querySelector('.amt'))})),
                net: T(document.querySelector('#page .big'))};
            })()""")
            assert card is not None, (width, theme)
            assert card["title"] == "Other currencies" and card["off"] == "Not counted"
            by = {r["name"]: r for r in card["rows"]}
            assert by["N26 Girokonto"]["tag"] == "EUR" and by["N26 Girokonto"]["amount"] == "€3,420.00"
            assert by["Seoul checking"]["tag"] == "KRW" and by["Seoul checking"]["amount"] == "KRW 1,200,000"
            # the header's subtotals are each in their own currency (D2: "4 · €…  · KRW 1,200,000"), never "$"
            assert "€" in card["header"] and "KRW 1,200,000" in card["header"] and "$" not in card["header"], card
            # the one-number net worth did not move: the foreign balances are not in it
            assert card["net"].startswith("$")


def test_transactions_day_total_is_one_figure_per_currency(site, chrome):
    for width in (390, 1280):
        go(chrome, site, "transactions", width)
        got = chrome.js(r"""(() => {
          const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
          const days = [...document.querySelectorAll('#page tr.day')];
          const day = days[0];
          const row = (tid) => document.querySelector(`#page tr[data-tid="${tid}"] .num`);
          return {day: T(day && day.querySelector('td.num')), eur: T(row('cur_eur_today')), krw: T(row('cur_krw_today')),
            usd: T(row('cur_usd_today'))};
        })()""")
        assert got["day"].startswith("$") and "€23.80" in got["day"] and "KRW 12,000" in got["day"], got
        assert got["eur"] == "€23.80" and got["krw"] == "KRW 12,000" and got["usd"] == "$133.18"


def test_budgets_shows_foreign_spend_beside_the_category_not_in_the_bar(site, chrome):
    for width in (390, 1280):
        go(chrome, site, "budgets", width)
        got = chrome.js(r"""(() => {
          const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
          const row = document.querySelector('#page tr[data-category="Groceries"]');
          return row && {also: T(row.querySelector('.also')), spent: T(row.querySelector('.a .act')),
            over: T(row.querySelector('.r'))};
        })()""")
        assert got and got["also"] == "also €23.80 · KRW 12,000", got
        assert got["spent"].startswith("$") and "€" not in got["spent"] and got["over"], got


def test_the_manual_account_form_has_a_currency_picker(site, chrome):
    go(chrome, site, "accounts", 1280)
    chrome.js(r"""(() => {
      const T = (el) => (el ? el.textContent.trim() : '');
      document.querySelector('[data-add]').click();
      return 1;
    })()""")
    chrome.settled()
    chrome.js(r"""(() => {
      const T = (el) => (el ? el.textContent.trim() : '');
      const b = [...document.querySelectorAll('#sheet .opt')].find((x) => T(x.querySelector('b')) === 'Add manually');
      b.click(); return 1;
    })()""")
    chrome.settled()
    chrome.js(r"""(() => {
      const T = (el) => (el ? el.textContent.trim() : '');
      const b = [...document.querySelectorAll('#sheet .opt')].find((x) => T(x.querySelector('b')) === 'Bank account');
      b.click(); return 1;
    })()""")
    chrome.settled()
    got = chrome.js(r"""(() => {
      const sel = document.querySelector('#sheet select[name=currency]');
      return sel && {value: sel.value, options: [...sel.options].map((o) => o.value),
        pre: document.querySelector('#sheet [data-money-prefix]').textContent};
    })()""")
    assert got and got["value"] == "USD" and "EUR" in got["options"] and "KRW" in got["options"]
    assert got["pre"] == "$"
    # choosing a foreign currency changes the amount's prefix, and saves with the form
    chrome.js("""(() => { const s = document.querySelector('#sheet select[name=currency]'); s.value = 'EUR';
      s.dispatchEvent(new Event('change', {bubbles: true})); return 1; })()""")
    assert chrome.js("document.querySelector('#sheet [data-money-prefix]').textContent") == "€"


def test_goal_cards_are_in_their_accounts_currency(site, chrome):
    conn = db.connect(site.path)
    month = insights.add_months(TODAY.strftime("%Y-%m"), 3)
    euro = goals.create(conn, dict(kind="save", name="Euro trip", track="account", account_id="cur_eur",
                                   target=5000, target_month=month), TODAY)
    plain = goals.create(conn, dict(kind="save", name="Dollar trip", track="manual", target=3000,
                                    target_month=month), TODAY)
    conn.close()
    for width in (390, 1280):
        go(chrome, site, "goals", width)
        got = chrome.js(f"""(() => {{
          const T = (el) => (el ? el.textContent.replace(/\\s+/g, ' ').trim() : null);
          const card = (id) => document.querySelector(`#page [data-goal="${{id}}"]`);
          return {{n: document.querySelectorAll('#page [data-goal]').length, euro: T(card({euro["id"]})),
            plain: T(card({plain["id"]})), pace: T(card({plain["id"]}) && card({plain["id"]}).querySelector('.gpace'))}};
        }})()""")
        assert got["euro"] and got["plain"], got
        assert "€5,000" in got["euro"] and "€3,420" in got["euro"] and "$" not in got["euro"], got
        assert "$3,000" in got["plain"] and "€" not in got["plain"] and got["pace"], got


def test_investments_and_debt_list_foreign_accounts_apart_in_their_own_currency(site, chrome):
    for width in (390, 1280):
        for theme in ("dark", "light"):
            go(chrome, site, "investments", width, theme)
            inv = chrome.js(r"""(() => {
              const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
              const row = [...document.querySelectorAll('#page .li')].find((r) => T(r.querySelector('.nm')) === 'Euro brokerage');
              const heads = [...document.querySelectorAll('#page .gh')].map(T);
              const after = row && [...document.querySelectorAll('#page .gh')].filter((g) =>
                g.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).map(T).pop();
              return row && {amount: T(row.querySelector('.amt')), tag: T(row.querySelector('.offtag')), heads, under: after};
            })()""")
            assert inv, (width, theme)
            assert inv["amount"] == "€5,000.00" and inv["tag"] == "Not counted", inv
            assert inv["under"] == "Other currencies · 1", inv
            go(chrome, site, "debt", width, theme)
            debt = chrome.js(r"""(() => {
              const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
              const row = [...document.querySelectorAll('#page .dl')].find((r) => (T(r.querySelector('.nmc b')) || '').startsWith('Euro card'));
              const hd = [...document.querySelectorAll('#page .dg .hd')].find((x) => (T(x.querySelector('b')) || '').startsWith('Credit cards'));
              const wrap = hd && hd.closest('.dg');
              return {bal: T(row && row.querySelector('.bal')), tag: T(row && row.querySelector('.offtag')),
                heads: [...document.querySelectorAll('#page .zero-h')].map(T),
                cnt: T(hd && hd.querySelector('.cnt')), cards: wrap ? [...wrap.querySelectorAll('.xtra .li')].map(T) : null};
            })()""")
            assert debt["bal"] == "€250.00" and debt["tag"] == "Not counted", debt
            assert "Other currencies · 1" in debt["heads"], debt
            # "Credit cards · N" counts exactly the rows it lists, and the euro card is not one of them
            assert debt["cards"] is not None and debt["cnt"] == f"· {len(debt['cards'])}", debt
            assert not any("Euro card" in c for c in debt["cards"]), debt


WAIT = """new Promise((r) => {{ const t0 = Date.now();
  const t = () => document.querySelector({sel}) ? r(true) : Date.now() - t0 > 8000 ? r(false) : setTimeout(t, 50); t(); }})"""


def _drop(b, raw, name):
    data = base64.b64encode(raw).decode()
    b.js(f"""(() => {{ const bin = atob({json.dumps(data)}); const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
      const dt = new DataTransfer(); dt.items.add(new File([bytes], {json.dumps(name)}));
      const i = document.querySelector('#sheet input[type=file]'); i.files = dt.files; i.dispatchEvent(new Event('change')); return 1; }})()""")


def test_a_csv_imported_again_goes_into_the_accounts_it_made(chrome, tmp_path):
    raw = (FIX / "everyday-export.csv").read_bytes()
    lines = raw.split(b"\n")
    first = b"\n".join(lines[:21]) + b"\n"              # the header and the first 20 rows
    srv = Server(gate_dbs.build("full", tmp_path / "finance.db", TODAY), today=TODAY)
    try:
        chrome.size(1280, 900)
        chrome.goto(f"{srv.url}/?t={time.time():.3f}#/accounts")
        chrome.settle()
        chrome.js("document.querySelector('[data-add=home]').click()")
        chrome.js("[...document.querySelectorAll('#sheet .opt')].find((o) => o.textContent.startsWith('Import a file')).click()")
        _drop(chrome, first, "everyday-export.csv")
        assert chrome.js(WAIT.format(sel=json.dumps("#sheet table.map")))
        assert chrome.js("[...document.querySelectorAll('#sheet .btn.w')].map((b) => b.textContent)") == ["Import 20 rows"]
        chrome.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
        assert chrome.js(WAIT.format(sel=json.dumps("#sheet .impdone")))
        conn = db.connect(srv.app.db_path)
        made = conn.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0]
        # the whole file again: each group's target is now the account the first import made ({account_id: N})
        chrome.js("[...document.querySelectorAll('#sheet .btn.ghost')].find((b) => b.textContent === 'Import another').click()")
        _drop(chrome, raw, "everyday-export.csv")
        drawn = chrome.js(WAIT.format(sel=json.dumps("#sheet table.map")))
        assert drawn, chrome.js("(document.querySelector('#sheet .err') || {}).textContent || document.getElementById('sheet').textContent")
        sheet = chrome.js(r"""({go: [...document.querySelectorAll('#sheet .btn.w')].map((b) => b.textContent),
          into: [...document.querySelectorAll('#sheet .mapacct select')].map((s) => s.value)})""")
        assert sheet["go"] == ["Import 17 rows"] and sheet["into"] and all(v.startswith("id:") for v in sheet["into"]), sheet
        chrome.js("[...document.querySelectorAll('#sheet .btn.w')][0].click()")
        assert chrome.js(WAIT.format(sel=json.dumps("#sheet .impdone")))
        assert chrome.js("document.querySelector('#sheet .impdone b').textContent") == "17 new rows, 20 duplicates skipped"
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'import'").fetchone()[0] == 37
        assert conn.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0] == made
        conn.close()
        assert chrome.problems() == []
    finally:
        srv.close()
