"""TASK-22g: Plaid plan switch safety. Changing plaid.plan puts "Allow on-demand bank refresh" (plaid.on_demand_refresh)
back to the new plan's default (Trial on, Paid off) in prefs.save, so the page's POST /api/prefs and anything else that
saves settings get it: a switch turned on while on Trial doesn't stay on (billed) once the plan is Paid. Saving the
same plan keeps the owner's choice, a save that sets the switch too keeps that, and the switch can be turned on again on
Paid. On Paid, Settings says the bigger cost under the plan's line: "Plaid bills each linked bank monthly on paid
plans." (Plaid's subscription products are billed per Item each month); Trial shows nothing new."""

from __future__ import annotations

import json
import time
from datetime import date

import pytest

from agent_tools import plugin_ctx
from conftest import add_item
from hermes_finance import config, prefs, syncnow
from hermes_finance.web import App
from test_task22f_sync_ui import T, chrome, chrome_only, go, scene_dbs, site  # noqa: F401  (the scene, its fixtures)

LOCAL = {"Host": "127.0.0.1:8765"}
JSONH = {**LOCAL, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}
TODAY = date(2026, 9, 28)
LINE = "Plaid bills each linked bank monthly on paid plans."


@pytest.fixture
def app(conn, fake, client):
    add_item(conn)
    fake.on("/transactions/refresh", {"request_id": "req-refresh"})
    a = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    a.today = lambda: TODAY
    return a


@pytest.fixture(params=["page", "prefs"])
def save(request, app, conn):
    """Saves settings the page's way (POST /api/prefs) or straight through prefs.save (what any other writer calls)."""
    def page(values):
        status, _, out = app.handle("POST", "/api/prefs", JSONH, json.dumps(values).encode())
        assert status == 200, out
    return page if request.param == "page" else (lambda values: prefs.save(conn, values, allowed=prefs.PAGE_KEYS))


def refresh(app):
    """What Settings shows (GET /api/settings/page's plaid.refresh: syncnow.refresh_policy)."""
    r = json.loads(app.handle("GET", "/api/settings/page", LOCAL, b"")[2])["plaid"]["refresh"]
    return {k: r[k] for k in ("plan", "allowed", "set", "billed")}


def ask(app):
    status, _, out = app.handle("POST", "/api/sync", JSONH, json.dumps({"refresh": True}).encode())
    return status, json.loads(out)


def test_turned_on_on_trial_then_paid_is_off_and_asks_nobody(app, fake, save):
    save({"plaid.on_demand_refresh": True})
    assert refresh(app) == {"plan": "trial", "allowed": True, "set": True, "billed": False}
    save({"plaid.plan": "paid"})
    assert refresh(app) == {"plan": "paid", "allowed": False, "set": False, "billed": True}   # Paid's default
    status, r = ask(app)
    assert status == 403 and fake.calls == []                                                # nothing billed
    assert r["error"] == ("On-demand bank refresh is off: Settings → Connections → Allow on-demand bank refresh. "
                          "On your paid Plaid plan, Plaid bills each refresh.")


def test_turned_off_on_paid_then_trial_is_on(app, save):
    save({"plaid.plan": "paid"})
    save({"plaid.on_demand_refresh": False})
    assert refresh(app) == {"plan": "paid", "allowed": False, "set": True, "billed": True}
    save({"plaid.plan": "trial"})
    assert refresh(app) == {"plan": "trial", "allowed": True, "set": False, "billed": False}   # Trial's default


def test_saving_the_same_plan_keeps_the_choice_only_a_change_clears_it(app, save):
    save({"plaid.on_demand_refresh": False})                    # off on Trial
    save({"plaid.plan": "trial"})                               # the same plan (and the default one, never saved)
    assert refresh(app) == {"plan": "trial", "allowed": False, "set": True, "billed": False}
    save({"plaid.plan": "paid"})
    save({"plaid.plan": "trial"})                               # a change and back: Trial's default again
    assert refresh(app) == {"plan": "trial", "allowed": True, "set": False, "billed": False}
    save({"plaid.plan": "paid"})
    save({"plaid.on_demand_refresh": True})
    save({"plaid.plan": "paid"})                                # Paid again: still on
    assert refresh(app) == {"plan": "paid", "allowed": True, "set": True, "billed": True}
    save({"plaid.plan": "trial", "plaid.on_demand_refresh": False})   # a change that sets the switch too keeps it
    assert refresh(app) == {"plan": "trial", "allowed": False, "set": True, "billed": False}


def test_on_paid_the_owner_can_turn_it_on_again(app, fake, save):
    save({"plaid.on_demand_refresh": True})
    save({"plaid.plan": "paid"})
    assert refresh(app)["allowed"] is False
    save({"plaid.on_demand_refresh": True})
    assert refresh(app) == {"plan": "paid", "allowed": True, "set": True, "billed": True}
    status, r = ask(app)
    assert status == 200 and fake.paths()[0] == "/transactions/refresh" and r["refresh_requested"][0]["item_id"] == "item_a"


def test_the_agent_tool_follows_the_plan_change(app, conn, client, monkeypatch):
    """finance_sync (plugin; MCP reads the same policy): after Trial + on -> Paid, a refresh is blocked, not a card."""
    monkeypatch.setattr(syncnow, "make_client", lambda: client)
    agent = plugin_ctx()
    prefs.save(conn, {"plaid.on_demand_refresh": True}, allowed=prefs.PAGE_KEYS)
    prefs.save(conn, {"plaid.plan": "paid"}, allowed=prefs.PAGE_KEYS)
    directive = agent.call("finance_sync", {"refresh": True})["directive"]
    assert directive["action"] == "block" and "On-demand bank refresh is off" in directive["message"]


# --- Settings: the Paid line ------------------------------------------------------------------------------------------

ROW = f"""(() => {{ const T = {T}; const row = [...document.querySelectorAll('#page .srow')].find((r) => T(r.querySelector('.grow > b')) === 'Plaid plan');
  const rf = [...document.querySelectorAll('#page .srow')].find((r) => T(r.querySelector('.grow > b')) === 'Allow on-demand bank refresh');
  const card = row.closest('.card'), spans = [...row.querySelectorAll(':scope > .grow > span')];
  const box = (el) => {{ const r = el.getBoundingClientRect(); return {{l: r.left, r: r.right}}; }};
  const style = (el) => {{ const s = getComputedStyle(el); return [s.fontSize, s.color, s.lineHeight, s.fontWeight, s.display, s.whiteSpace]; }};
  return {{lines: spans.map(T), size: Math.min(...spans.map((x) => parseFloat(getComputedStyle(x).fontSize))), styles: spans.map(style),
    boxes: spans.map(box), row: box(row), card: box(card), cut: spans.some((x) => x.scrollWidth > x.clientWidth + 0.5),
    page: document.documentElement.scrollWidth, win: innerWidth, plan: T(row.querySelector('.seg [aria-pressed=true]')),
    on: rf.querySelector('[role=switch]').getAttribute('aria-checked'), refreshLine: T(rf.querySelector('.grow > span'))}}; }})()"""


@chrome_only
def test_paid_says_plaid_bills_each_bank_monthly_trial_doesnt(chrome, site):
    b = chrome
    for plan in ("trial", "paid"):
        s = site()
        prefs.save(s.conn, {"plaid.plan": plan}, allowed=prefs.PAGE_KEYS)
        for width in (1440, 390, 360):
            go(b, s, "settings", width)
            st = b.js(ROW)
            assert st["lines"] == ([LINE] if plan == "paid" else []), (plan, width)
            assert st["plan"] == plan.title() and (not st["lines"] or st["size"] >= 13)
            assert len({tuple(x) for x in st["styles"]}) == len(st["lines"])        # the new line looks like the plan's own
            assert st["page"] <= st["win"] and not st["cut"], (plan, width, st)
            assert all(st["card"]["l"] <= x["l"] and x["r"] <= st["row"]["r"] + 0.5 <= st["card"]["r"] + 0.5 for x in st["boxes"])
        assert b.problems() == []


@chrome_only
def test_picking_paid_on_the_page_turns_the_switch_back_off(chrome, site):
    b = chrome
    s = site()
    prefs.save(s.conn, {"plaid.on_demand_refresh": True}, allowed=prefs.PAGE_KEYS)   # on while on Trial
    go(b, s, "settings", 390)
    assert (b.js(ROW)["on"], b.js(ROW)["lines"]) == ("true", [])
    b.js("[...document.querySelectorAll('#page .seg button')].find((x) => x.textContent.trim() === 'Paid').click()")
    b.js("new Promise((r, j) => { const t0 = Date.now(); const k = () => { const on = document.querySelector('#page .seg [data-k=paid][aria-pressed=true]');"
         " if (on) r(1); else if (Date.now() - t0 > 15000) j(new Error('never Paid')); else setTimeout(k, 40); }; k(); })")
    b.settle()
    st = b.js(ROW)
    assert (st["plan"], st["on"], st["lines"]) == ("Paid", "false", [LINE])
    assert st["refreshLine"] == "Plaid bills each refresh request on paid plans. Off unless you turn it on."
    assert syncnow.refresh_policy(s.conn, "production")["allowed"] is False
    b.js("document.querySelector('#page [role=switch][aria-label=\"Allow on-demand bank refresh\"]').click()")
    for _ in range(150):                                         # (the switch flips at once; the save follows)
        if syncnow.refresh_policy(s.conn, "production")["allowed"]:
            break
        time.sleep(0.1)
    b.settle()
    assert syncnow.refresh_policy(s.conn, "production")["allowed"] is True and b.js(ROW)["on"] == "true"
    assert b.problems() == []
