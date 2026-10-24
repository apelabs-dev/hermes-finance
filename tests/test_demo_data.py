"""The synthetic demo DB (used for smoke tests and screenshots) works with every endpoint."""

from __future__ import annotations

import json
from datetime import date

import demo_data
from hermes_finance import config
from hermes_finance.web import App

TODAY = date(2026, 9, 25)
LOCAL = {"Host": "127.0.0.1:8765"}


def test_demo_db_drives_every_page(conn, client):
    demo_data.build(conn, TODAY)
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    app.today = lambda: TODAY

    def get(path):
        status, ctype, body = app.handle("GET", path, LOCAL, b"")
        assert status == 200, (path, body[:200])
        return json.loads(body) if ctype == "application/json" else body.decode()

    assert "Add account" in get("/")
    ov = get("/api/overview?month=2026-09")
    assert ov["summary"]["day"] == 25 and ov["summary"]["mtd"] > 0 and len(ov["summary"]["cum"]) == 25
    assert {a["key"] for a in ov["alerts"]} >= {"cap:Other Health:2026-09:over", "item:demo_usb:ITEM_LOGIN_REQUIRED"}
    flow = ov["cashflow"]["months"]
    assert [m["coverage"] for m in flow] == ["partial"] * 5 + ["full"]
    assert flow[0]["missing"] == ["Chase", "U.S. Bank"] and flow[-2]["missing"] == ["U.S. Bank"]
    rec = get("/api/recurring")
    kinds = {r["merchant"]: (r["kind"], r["cadence"]) for r in rec["items"]}
    assert kinds["Innago"] == ("bill", "monthly") and kinds["Zenith Financial Services"] == ("bill", "monthly")
    assert kinds["GEICO"] == ("bill", "monthly") and kinds["Netflix"] == ("subscription", "monthly")
    assert kinds["SpotHero"] == ("subscription", "weekly") and kinds["Namecheap"] == ("subscription", "yearly")
    assert not any(m in kinds for m in ("Amazon", "Trader Joe's", "To Savings - 7731", "Chase Credit Card Payment"))
    tx = get("/api/transactions?month=2026-08&q=Diner")
    assert [r["merchant"] for r in tx["rows"]] == ["Joe's <b>Diner</b> <img src=x onerror=alert(1)>"]
    assert get("/api/transactions?month=2026-09&kind=review")["total"] >= 1
    b = get("/api/budgets?month=2026-09")
    assert b["budgets"]["count"] == 5 and b["suggestions"]["total"] > 0
    acc = get("/api/accounts")
    assert len(acc["net_worth"]["history"]) == 14 and acc["slots_used"] == 3
    assert get("/api/counts?month=2026-09")["recurring"] == rec["count"]
    for m in ("2026-08", "2026-02", "2025-12"):
        get(f"/api/overview?month={m}")
        get(f"/api/budgets?month={m}")


def test_demo_extras_drive_the_new_pages(conn, client):
    demo_data.build(conn, TODAY)
    demo_data.extras(conn, TODAY, holding_days=3)
    app = App(config.db_path(), lambda: client, "127.0.0.1", 8765)
    app.today = lambda: TODAY

    def get(path):
        status, _, body = app.handle("GET", path, LOCAL, b"")
        assert status == 200, (path, body[:200])
        return json.loads(body)

    inv = get("/api/investments")
    assert inv["days"] == 3 and inv["total"] == 491.08 and inv["changes"]["7d"] is None and inv["changes"]["all"]
    debt = get("/api/debt")
    zenith = next(l for l in debt["loans"] if l["name"] == "Zenith")
    assert zenith["applied_count"] == 1 and zenith["balance"] == round(5910.0 - 465.10, 2)   # last month's payment
    assert any(l["paid_off"] for l in debt["loans"]) and debt["cards"][0]["min_payment"]
    acc = get("/api/accounts")
    assert {m["name"] for m in acc["manual"]} == {"Zenith", "Other loan", "2019 Zenith Mesa"}
    assert next(m for m in acc["manual"] if m["kind"] == "vehicle")["stale"] is True
    assert len(acc["net_worth"]["history"]) == 14                # manual rows add no extra points here
    ov = get("/api/overview?month=2026-09")
    assert ov["cards_due"] and ov["investments"]["total"] == 491.08 and ov["debt"]["loan"]["name"] == "Zenith"
