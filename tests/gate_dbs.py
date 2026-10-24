"""The three synthetic DBs the final-review gates (scripts/audit_numbers.py, scripts/audit_layout.py) and the TASK-11
checks run against. No real data, no Plaid.

  full     tests/demo_data.py build + extras (a loan matched to its payments, a paid-off loan, a car, holdings with
           21 days of snapshots), plus what TASK-10's sweep added (a manual 401(k), a manual card at "Chase", AI-flagged
           rows, cached logos, a late Comcast bill) and TASK-11's cases: a Plaid "Automated Investing" account at $0
           (the real Allocation screenshot's Robo row), the Anomaly -> "OPENCODE ANOMA.LY" merchant rename, a Citi card
           paid from Nova as "CITI CARD ONLINE" (with its "ONLINE PAYMENT, THANK YOU"), the manual Best Buy card matched
           to its autopay with that same label (set 40 days ago: "≈" and the statement nudge), and Rent & Utilities
           hidden from the "Where it went" chart
           and the valuations (TASK-12, TASK-13b), each with its page state: the Mesa follows its Carfax trade-in (two
           checks: the change shows), is linked to the Car Loan (equity) and has its Commons photo cut out; a 2015
           Honda Fit keeps "Your value" with its Carfax trade-in beside it, the 90-day mileage nudge and the drawn
           silhouette (no photo found); a condo follows its Redfin Estimate with its listing photo; a cabin follows its
           Redfin Estimate with no listing photo (an OpenStreetMap tile with a pin); and a condo unit Redfin has no
           record of (no estimate, the drawn house); and (TASK-15) a saved Custom net worth selection with groups counted
           in part (Cash, Credit cards, Vehicles), left out (Investments, Property) and whole (Loans)
  nocaps   build only, caps removed: the suggestions hero, "Track a loan", no investment history; a car with no
           estimate yet whose Carfax check hit the bot wall (paused for 7 days: the explained fallback)
  empty    one bank, one checking account: "Nothing owed", no investments, ghost rows
  history-empty  TASK-12b: the live Investments page the owner reported: holdings (NVDA + cash) in two Nova accounts
           (Self-directed, and Automated Investing at $0) and NO holding snapshot yet, so "History builds from today"
           and no chart, beside a tall Allocation card
  onepoint TASK-12b's history case with one point: build + extras with ONE day of holding snapshots, so the
           Investments chart is a single point under its empty-state message (nocaps: one investment account, no
           holdings, no snapshot; empty: no investment account); and (polish P2e, add_group_budgets) two group budgets,
           each with a category cap inside: Food $200 under its Eating Out cap ($250: "categories add up to $250") and
           Leisure $700 over its Entertainment cap ($50); the other caps' groups are offered by the one-time move
  features TASK-19 (Batch C): full plus every new feature in use: goals (a whole account, two earmarks in one account
           that now holds less than they set aside, entries logged by hand, a loan to pay off, monthly savings), split
           transactions (this month and last), notes and tags, two saved filters, rollover on for Eating Out (last
           month's unused cap rolls in) and Other Transport (overspent: nothing rolls), and the Alpaca live account (a week of
           ACTIVE snapshots with two positions and cash, its cached site icon); and TASK-21's bills (add_bills): a gas
           bill whose amount changes, LADWP tracked by hand every 2 months with its usual amount, a pending agent change;
           and TASK-22f's payment in transit (add_transit): $212.34 from Nova Checking to the Chase card ••3591 yesterday
  (only full and features have a saved Custom selection: on the other four the first read saves TASK-14's "Cash & debts" one)

    uv run python tests/gate_dbs.py /tmp/some-dir        # writes full.db, nocaps.db, empty.db, ... there
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_data  # noqa: E402
from pngtools import logo_png, make_png  # noqa: E402

from hermes_finance import auth, config, db, insights, manual, photos, prefs, usage, valuation  # noqa: E402

KINDS = ("full", "nocaps", "empty", "history-empty", "onepoint", "features")


def _valued(conn, account_id: int, value: float, source: str, miles: int | None, day: date, follow: bool,
            details: dict | None = None) -> None:
    """A synthetic successful lookup on `day` (the api_calls row the guard would have written, the asset_valuations
    row) and, when the account follows its estimate, its balance. Nothing is called."""
    at = f"{day.isoformat()}T19:00:00Z"  # noon on the US west coast: the same local day
    with conn:
        conn.execute("INSERT INTO api_calls (provider, account_id, trigger, at, day, status, outcome) "
                     "VALUES (?, ?, 'daily', ?, ?, 200, 'ok')", (source, account_id, at, day.isoformat()))
        conn.execute("INSERT INTO api_usage (provider, month, calls, updated_at) VALUES (?, ?, 1, ?) ON CONFLICT(provider, "
                     "month) DO UPDATE SET calls = calls + 1", (source, day.isoformat()[:7], at))
        row = conn.execute("SELECT * FROM manual_accounts WHERE id = ?", (account_id,)).fetchone()
        conn.execute("INSERT INTO asset_valuations (account_id, value, source, miles, subject, fetched_at, raw_json, "
                     "details_json) VALUES (?, ?, ?, ?, ?, ?, '{}', ?)",
                     (account_id, value, source, miles, valuation.subject(row), at, json.dumps(details) if details else None))
        if follow:
            conn.execute("UPDATE manual_accounts SET balance = ?, balance_as_of = ?, value_source = 'estimate' WHERE id = ?",
                         (value, day.isoformat(), account_id))
            conn.execute("INSERT OR REPLACE INTO manual_balance_history VALUES (?, ?, ?)", (account_id, day.isoformat(), value))


def _carfax(trade: float, trim: str, miles: int, specs: dict) -> dict:
    return {"trade_in": trade, "private_sale": round(trade * 1.1), "dealer_retail": round(trade * 1.2), "records": 12,
            "carfax_odometer": miles, "trim": trim, "condition": "good", "zip": "94110", "colour": "Silver",
            "display_status": "Displayable", "specs": specs}


def _image(conn, db_path: Path, row, kind: str, status: str, data: bytes | None = None, **fields) -> None:
    """A cached picture as photos.py would have saved it (file in images/ next to the DB, row in asset_images)."""
    key = photos.key_for(row)
    name = None
    if data:
        name = photos.file_name(key, "png")
        photos._write(photos.images_dir(db_path), name, data)
    photos._save(conn, key, kind, status, file=name, **fields)


def _car_png() -> bytes:
    """A small cut-out car (transparent around it): body, cabin, two wheels."""
    def px(x, y):
        if (x - 110) ** 2 + (y - 170) ** 2 < 30 ** 2 or (x - 370) ** 2 + (y - 170) ** 2 < 30 ** 2:
            return (18, 20, 26, 255)
        if 40 <= x <= 440 and 110 <= y <= 165:
            return (196, 200, 208, 255)
        if 140 <= x <= 360 and 60 <= y < 110 and abs(x - 250) < 110 - (110 - y) * 0.6:
            return (120, 140, 170, 255)
        return (0, 0, 0, 0)
    return make_png(480, 210, px)


def _house_png() -> bytes:
    def px(x, y):
        if y < 120:
            return (150, 190, 230, 255)
        if 150 <= x <= 390 and 150 <= y <= 330:
            return (214, 196, 170, 255)
        if y > 330:
            return (90, 140, 80, 255)
        return (170, 205, 235, 255)
    return make_png(540, 360, px)


def _tile_png() -> bytes:
    return make_png(256, 256, lambda x, y: (225, 228, 222, 255) if (x % 64) > 6 and (y % 64) > 6 else (255, 255, 255, 255))


MESA_SPECS = {"specs": {"BodyClass": "Sport Utility Vehicle (SUV)/Multi-Purpose Vehicle (MPV)", "Doors": "4", "Seats": "5",
                        "DisplacementL": "2.5", "EngineCylinders": "4", "EngineConfiguration": "In-Line",
                        "TransmissionStyle": "Automatic", "DriveType": "AWD/All-Wheel Drive", "CurbWeightLB": "3490",
                        "WheelBaseShort": "105.9", "WheelSizeFront": "17", "PlantCity": "TAHARA", "PlantCountry": "JAPAN"},
              "safety": {"AdaptiveCruiseControl": "Standard", "LaneDepartureWarning": "Standard",
                         "ForwardCollisionWarning": "Standard", "CIB": "Standard", "PedestrianAutomaticEmergencyBraking": "Standard",
                         "BlindSpotMon": "Optional", "TPMS": "Direct", "ABS": "Standard", "ESC": "Standard"}}


def add_valuations(conn, today: date, db_path: Path) -> None:
    """The vehicle / property states the Accounts page and their pages show (TASK-12, TASK-13b)."""
    mesa = conn.execute("SELECT id FROM manual_accounts WHERE kind = 'vehicle' AND name = '2019 Zenith Mesa'").fetchone()[0]
    loan = conn.execute("SELECT id FROM manual_accounts WHERE kind = 'loan' AND subtype = 'auto'").fetchone()[0]
    with conn:
        conn.execute("UPDATE manual_accounts SET vin = '2T3P1RFV2KW012345', vin_decoded = '2T3P1RFV2KW012345', "
                     "vehicle_trim = 'LE', mileage = 48210, mileage_as_of = ?, zip_code = '94110', linked_loan_id = ? "
                     "WHERE id = ?", ((today - timedelta(days=34)).isoformat(), loan, mesa))
        conn.execute("INSERT OR REPLACE INTO vin_decodes VALUES ('2T3P1RFV2KW012345', 2019, 'Zenith', 'Mesa', 'LE', ?, ?)",
                     (db.now(), json.dumps(MESA_SPECS)))
    mesa_specs = {"engineName": "2.5L I4 203hp", "driveType": "AWD", "fuelType": "GASOLINE", "bodyType": "4 DOOR SUV",
                  "segmentationDesc": "COMPACT SUV"}
    _valued(conn, mesa, 21450.0, "carfax", 48210, today - timedelta(days=34), True, _carfax(21450.0, "LE", 48210, mesa_specs))
    _valued(conn, mesa, 20875.0, "carfax", 48210, today - timedelta(days=4), True, _carfax(20875.0, "LE", 48210, mesa_specs))
    mesa_row = manual.get(conn, mesa)
    _image(conn, db_path, mesa_row, "vehicle", "ready", _car_png(), source="commons", cutout=True, width=4000, height=2250,
           credit="Photo: Kevauto, CC BY-SA 4.0 · Wikimedia Commons",
           credit_url="https://commons.wikimedia.org/wiki/File:2019_Zenith_MESA_LE_front.jpg", artist="Kevauto",
           license="CC BY-SA 4.0", note="File:2019 Zenith Mesa LE front.jpg (cut out)")
    fit = manual.create(conn, {"kind": "vehicle", "vin": "JHMGE8H30FC012345", "vehicle_make": "Honda", "vehicle_model": "Fit",
                               "vehicle_year": 2015, "vehicle_trim": "EX", "mileage": 91500, "zip_code": "94110",
                               "balance": 7200.0, "balance_as_of": (today - timedelta(days=20)).isoformat()}, today)
    with conn:
        conn.execute("UPDATE manual_accounts SET mileage_as_of = ?, vin_decoded = vin WHERE id = ?",
                     ((today - timedelta(days=120)).isoformat(), fit["id"]))
    _valued(conn, fit["id"], 6480.0, "carfax", 91500, today - timedelta(days=6), False,
            _carfax(6480.0, "EX", 91500, {"engineName": "1.5L I4 130hp", "driveType": "FWD", "fuelType": "GASOLINE",
                                          "bodyType": "4 DOOR HATCHBACK", "segmentationDesc": "SUBCOMPACT CAR"}))
    _image(conn, db_path, manual.get(conn, fit["id"]), "vehicle", "none",
           note="Wikimedia Commons: 3 photos tried; none cut out cleanly")
    condo = manual.create(conn, {"kind": "property", "name": "Condo", "address": "5500 Grand Lake Dr, San Antonio, TX 78244"},
                          today, estimate=True)
    _valued(conn, condo["id"], 432000.0, "redfin", None, today - timedelta(days=9), True,
            {"property_id": "7001", "name": "5500 Grand Lake Dr", "url": "/TX/San-Antonio/5500-Grand-Lake-Dr-78244/home/7001",
             "preview": "$432,000 (+$247K since last sold)", "beds": 3.0, "baths": 2.0, "sqft": 1878.0,
             "last_sold": {"amount": 185000.0, "label": "Last Sold Price"}, "lat": 29.475962, "lng": -98.351442,
             "via": "https", "photo_url": "https://ssl.cdn-redfin.com/photo/40/bigphoto/001/7001_0.jpg"})
    _image(conn, db_path, manual.get(conn, condo["id"]), "property", "ready", _house_png(), source="redfin",
           width=540, height=360, credit="Photo: listing via Redfin")
    cabin = manual.create(conn, {"kind": "property", "name": "Cabin", "address": "1 Lake Rd, Tahoe City, CA 96145"},
                          today, estimate=True)
    _valued(conn, cabin["id"], 615000.0, "redfin", None, today - timedelta(days=9), True,
            {"property_id": "7002", "name": "1 Lake Rd", "beds": 2.0, "baths": 1.0, "sqft": 960.0, "last_sold": None,
             "lat": 39.1677, "lng": -120.1452, "via": "https", "photo_url": None})
    x, y, px, py = photos.tile_for(39.1677, -120.1452)
    _image(conn, db_path, manual.get(conn, cabin["id"]), "property", "ready", _tile_png(), source="osm", width=256,
           height=256, credit="© OpenStreetMap contributors", credit_url="https://www.openstreetmap.org/copyright",
           lat=39.1677, lng=-120.1452, pin_x=px, pin_y=py, note=f"tile 17/{x}/{y}")
    unit = manual.create(conn, {"kind": "property", "name": "Studio", "address": "4120 Birch Ave #303, Santa Rosa, CA 95404",
                                "balance": 410000.0}, today)
    note = "Redfin has no record for unit 303 (only the building or other units): no estimate for this unit"
    with conn:
        conn.execute("INSERT INTO api_calls (provider, account_id, trigger, at, day, status, outcome, note) "
                     "VALUES ('redfin', ?, 'save', ?, ?, 200, 'none', ?)",
                     (unit["id"], f"{today.isoformat()}T19:00:00Z", today.isoformat(), note))
    _image(conn, db_path, manual.get(conn, unit["id"]), "property", "none", note="no listing photo and no location on Redfin")


def _txn(conn, tid: str, account_id: str, item_id: str, day: str, name: str, merchant: str | None, amount: float,
         pfc: tuple[str, str], logo: str | None = None) -> None:
    conn.execute("INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, amount, "
                 "pending, pfc_primary, pfc_detailed, logo_url, raw_json, first_seen_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,0,?,?,?,'{}',?,?)", (tid, account_id, item_id, day, name, merchant, amount, *pfc,
                                                              demo_data.LOGOS + logo if logo else None, day, day))


def build_full(path: Path, today: date | None = None) -> None:
    today = today or date.today()
    conn = db.connect(path)
    demo_data.build(conn, today)
    demo_data.extras(conn, today, holding_days=21)
    # TASK-11b (2), the real car loan: its APR is on screen ("3.81%", "at 3.81% APR") beside "81% paid off"
    car = conn.execute("SELECT id FROM manual_accounts WHERE kind = 'loan' AND name = 'Zenith'").fetchone()[0]
    manual.edit(conn, car, {"name": "Car Loan", "interest_rate": 3.81, "balance": 5907.88, "balance_as_of": today.isoformat()}, today)
    with conn:  # TASK-11b (1): Plaid's balance (491.62) and its holdings (490.90 + 0.57 cash = 491.47) disagree
        conn.execute("UPDATE accounts SET current_balance = 491.62 WHERE account_id = 'acc_nova_inv'")
        conn.execute("UPDATE holdings SET institution_value = 490.90 WHERE account_id = 'acc_nova_inv' AND security_id = 'sec_nvda'")
        conn.execute("UPDATE holding_snapshots SET value = 490.90 WHERE account_id = 'acc_nova_inv' AND security_id = 'sec_nvda' "
                     "AND date = ?", (today.isoformat(),))
    with conn:  # AI-flagged rows (no user decision since), so "Needs review" includes them
        conn.execute("INSERT INTO ai_runs (run_id, created_at, applied_at, queued_groups, queued_txns) "
                     "VALUES ('ai-20260925-000000', '2026-09-25T06:00:00Z', '2026-09-25T06:05:00Z', 3, 3)")
        rows = conn.execute("SELECT transaction_id, category_id FROM v_transactions WHERE month = ? AND is_income = 0 "
                            "AND is_transfer = 0 AND category <> 'Other' ORDER BY date DESC LIMIT 3",
                            (today.isoformat()[:7],)).fetchall()
        for r in rows:
            conn.execute("INSERT INTO ai_reviews VALUES (?, 'ai-20260925-000000', '2026-09-25T06:05:00Z', 'flagged', ?, "
                         "0.4, 'Unusual for this merchant: check it')", (r[0], r[1]))
        conn.executemany("INSERT INTO institutions VALUES (?,?,?,?,?,?, '2026-09-26T00:00:00Z')", [
            ("ins_demo_nova", "Nova", logo_png(64), "plaid", "#00a9ce", "https://www.novabank.example"),
            ("ins_demo_chase", "Chase", logo_png(48), "site", "#095aa6", "https://www.chase.com"),
            ("ins_demo_usb", "U.S. Bank", None, "none", "#102670", "https://www.usbank.com")])
        conn.execute("INSERT INTO security_logos VALUES ('NVDA', ?, 'fmp', '2026-09-26T00:00:00Z')", (logo_png(64),))
        # a monthly bill two weeks overdue, so the "Late" states are on screen
        for k, back in enumerate((107, 76, 45)):
            d = (today - timedelta(days=back)).isoformat()
            _txn(conn, f"late_{k}", "acc_nova_chk", "demo_nova", d, "COMCAST CABLE", "Comcast", 89.99,
                 ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_INTERNET_AND_CABLE"))
        # TASK-11 (e): Plaid renamed the merchant "Anomaly" to "Opencode Anoma.ly"; this month's charge is paid
        for k in (3, 2, 1):
            _txn(conn, f"anomaly_{k}", "acc_nova_chk", "demo_nova", demo_data_month_day(today, -k, 16), "ANOMALY",
                 "Anomaly", 10.0, ("GENERAL_SERVICES", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"))
        if today.day >= 16:
            _txn(conn, "anomaly_0", "acc_nova_chk", "demo_nova", today.replace(day=16).isoformat(), "OPENCODE ANOMA.LY",
                 "Opencode Anoma.ly", 10.0, ("GENERAL_SERVICES", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"))
        # the real Allocation screenshot: a Plaid "Automated Investing" (Robo) account at $0 next to the brokerage
        conn.execute("INSERT INTO accounts (account_id, item_id, name, official_name, mask, type, subtype, current_balance, "
                     "iso_currency, updated_at) VALUES ('acc_nova_robo', 'demo_nova', 'Nova Automated Investing', NULL, "
                     "'7710', 'investment', 'brokerage', 0.0, 'USD', ?)", (db.now(),))
        # a connected Citi card, paid from Nova as "CITI CARD ONLINE" (it pairs with the card's own payment line)
        conn.execute("INSERT INTO accounts (account_id, item_id, name, official_name, mask, type, subtype, current_balance, "
                     "credit_limit, iso_currency, updated_at) VALUES ('acc_citi_cc', 'demo_chase', 'Citi Double Cash', "
                     "NULL, '2417', 'credit', 'credit card', 318.40, 7500.0, 'USD', ?)", (db.now(),))
        pay_day = (today - timedelta(days=12)).isoformat()
        _txn(conn, "citi_out", "acc_nova_chk", "demo_nova", pay_day, "CITI CARD ONLINE PAYMENT", None, 250.0,
             ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))
        _txn(conn, "citi_in", "acc_citi_cc", "demo_chase", (today - timedelta(days=11)).isoformat(),
             "ONLINE PAYMENT, THANK YOU", None, -250.0, ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))
        # The owner hides Rent & Utilities from "Where it went" (TASK-11 f): the dimmed row and "1 hidden · Show all"
        conn.execute("INSERT INTO settings VALUES ('chart_excluded_categories', '[\"Rent & Utilities\"]', ?)", (db.now(),))
    manual.create(conn, {"kind": "investment", "name": "Work 401(k)", "institution": "Fidelity", "subtype": "401k",
                         "balance": 18250.0}, today)
    manual.create(conn, {"kind": "credit_card", "name": "Sapphire", "institution": "Chase", "credit_limit": 6000.0,
                         "balance": 420.0}, today)
    # the Best Buy card, matched to its Citi autopay (the label it shares with the Double Cash, whose own payment
    # pairs with the card's line and is left out); its balance was set 40 days ago, so it asks for a statement
    manual.create(conn, {"kind": "credit_card", "name": "Best Buy", "institution": "Citi", "credit_limit": 4000.0,
                         "balance": 1110.66, "match_pattern": "CITI CARD ONLINE",
                         "balance_as_of": (today - timedelta(days=40)).isoformat()}, today)
    with conn:
        _txn(conn, "bb_auto", "acc_nova_chk", "demo_nova", (today - timedelta(days=9)).isoformat(), "CITI CARD ONLINE PAYMENT",
             None, 35.0, ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))
    manual.match_payments(conn, today=today)
    add_valuations(conn, today, path)
    # TASK-15: a saved Custom selection with every state the Accounts page and its Edit sheet have: Cash and Credit cards
    # counted in part (the Nova vault, the manual Sapphire card left out), Vehicles in part (the Honda Fit out, the Mesa
    # in), Investments and Property left out entirely, Loans all counted
    ids = {r["name"]: insights.manual_nw_id(r["id"]) for r in conn.execute("SELECT id, name FROM manual_accounts")}
    insights.save_networth_excluded(conn, ["acc_nova_vault", "acc_nova_inv", "acc_nova_robo", ids["Work 401(k)"],
                                           ids["2015 Honda Fit"], ids["Cabin"], ids["Condo"], ids["Studio"], ids["Sapphire"]])
    # TASK-18: Settings in its fuller states: an API-key AI provider, the proxy mode with an identity and a public URL
    # (the last request through it) and two MCP tokens
    prefs.save(conn, {"ai.mode": "api", "ai.model": "anthropic/claude-haiku-4.5"})
    prefs.save(conn, {"access.mode": "proxy", "access.allowed_identities": ["owner@example.com"],
                      "access.public_urls": ["https://box.tail0000.ts.net:8765"],
                      "access.last_proxy": {"identity": "owner@example.com", "via": "Tailscale", "host": "box.tail0000.ts.net",
                                            "at": db.now()}}, allowed=list(prefs.SPEC))
    auth.mcp_token_create(conn, "Claude Desktop")
    auth.mcp_token_create(conn, "Cursor")
    conn.close()


def demo_data_month_day(today: date, months: int, day: int) -> str:
    """The `day` of the month `months` away from today's (clamped to the month's length)."""
    k = today.year * 12 + today.month - 1 + months
    y, m = divmod(k, 12)
    first = date(y, m + 1, 1)
    last = ((first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    return first.replace(day=min(day, last)).isoformat()


def build_nocaps(path: Path, today: date | None = None) -> None:
    today = today or date.today()
    conn = db.connect(path)
    demo_data.build(conn, today)
    with conn:
        conn.execute("UPDATE categories SET monthly_cap = NULL")
    # a car left to its estimate, whose Carfax check hit the bot wall: $0, explained, paused for 7 days
    car = manual.create(conn, {"kind": "vehicle", "vin": "1HGCM82633A004352", "vehicle_make": "Honda", "vehicle_model": "Accord",
                               "vehicle_year": 2003, "mileage": 120000, "zip_code": "94110"}, today, estimate=True)
    reason = "Carfax blocked the check (trims: a captcha page)"
    with conn:
        conn.execute("INSERT INTO api_calls (provider, account_id, trigger, at, day, status, outcome, note) "
                     "VALUES ('carfax', ?, 'save', ?, ?, 403, 'blocked', ?)",
                     (car["id"], f"{today.isoformat()}T19:00:00Z", today.isoformat(), reason))
    usage.block(conn, "carfax", today, reason)
    conn.close()


def build_empty(path: Path, today: date | None = None) -> None:
    today = today or date.today()
    conn = db.connect(path)
    now = db.now()
    with conn:
        conn.execute("INSERT INTO items (item_id, institution_id, institution_name, access_token, status, products, "
                     "created_at, last_sync_at) VALUES ('it1', 'ins_1', 'Ally Bank', 'access-production-empty-it1', "
                     "'active', '[\"transactions\"]', ?, ?)", (now, now))
        conn.execute("INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance, iso_currency, "
                     "updated_at) VALUES ('a1', 'it1', 'Checking', '0001', 'depository', 'checking', 812.5, 'USD', ?)",
                     (now,))
        for k, (days, amount, name, pfc) in enumerate([
                (2, 24.5, "Corner Deli", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT")),
                (5, 61.2, "Safeway", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")),
                (9, -1200.0, "Payroll", ("INCOME", "INCOME_WAGES"))]):
            # Sheet states need a current-month row even when this run lands at the month boundary.
            d = max(today.replace(day=1), today - timedelta(days=days)).isoformat()
            _txn(conn, f"e{k}", "a1", "it1", d, name, name, amount, pfc)
    prefs.save(conn, {"ai.mode": "off"})  # TASK-18: Settings with AI off
    conn.close()


# TASK-18: the built-in login's own page, for the layout gate only (audit_layout.AUTH_STATES; never the numbers gate:
# every API answer there is "sign in first"). signin: a login exists (this user and password); signin-setup: the mode is
# chosen and no login exists yet, so this computer gets the first-time set-up.
SIGNIN_USER, SIGNIN_PASSWORD = "owner", "gate password for the layout audit"


def build_signin(path: Path, today: date | None = None) -> None:
    build_empty(path, today)
    conn = db.connect(path)
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    with conn:
        conn.execute("INSERT INTO auth_users (username, password_hash, totp_secret, created_at, password_changed_at) "
                     "VALUES (?, ?, ?, ?, ?)", (SIGNIN_USER, auth.hash_secret(SIGNIN_PASSWORD), auth.new_totp_secret(),
                                                db.now(), db.now()))
    conn.close()


def build_signin_setup(path: Path, today: date | None = None) -> None:
    build_empty(path, today)
    conn = db.connect(path)
    prefs.save(conn, {"access.mode": "builtin"}, allowed=["access.mode"])
    conn.close()


def build_history_empty(path: Path, today: date | None = None) -> None:
    today = today or date.today()
    conn = db.connect(path)
    demo_data.build(conn, today)
    demo_data.extras(conn, today, holding_days=0)  # holdings, but not one snapshot yet
    with conn:
        conn.execute("INSERT INTO accounts (account_id, item_id, name, official_name, mask, type, subtype, current_balance, "
                     "iso_currency, updated_at) VALUES ('acc_nova_robo', 'demo_nova', 'Nova Automated Investing', NULL, "
                     "'7710', 'investment', 'brokerage', 0.0, 'USD', ?)", (db.now(),))
    conn.close()


def build_onepoint(path: Path, today: date | None = None) -> None:
    today = today or date.today()
    conn = db.connect(path)
    demo_data.build(conn, today)
    demo_data.extras(conn, today, holding_days=1)  # one snapshot: today's
    add_group_budgets(conn)
    conn.close()


def add_group_budgets(conn) -> None:
    """polish P2e: a budget on two groups, demo_data's caps left as they are (see the module docstring's `onepoint`)."""
    from hermes_finance import groupbudget
    groupbudget.set_budget(conn, {"group": "Food", "monthly_budget": 200})
    groupbudget.set_budget(conn, {"group": "Leisure", "monthly_budget": 700})


ALPACA_POSITIONS = [{"symbol": "AAPL", "asset_class": "us_equity", "qty": 3.0, "price": 227.52, "value": 682.56, "cost_basis": 601.2},
                    {"symbol": "VTI", "asset_class": "us_equity", "qty": 1.5, "price": 281.4, "value": 422.1, "cost_basis": 400.0}]
ALPACA_CASH = 125.34


def add_features(conn, today: date) -> None:
    """TASK-19's features in use, on top of a built DB (see the module docstring's `features`)."""
    from hermes_finance import alpaca, goals, rollover, txmeta
    loan = conn.execute("SELECT id FROM manual_accounts WHERE kind = 'loan' AND name = 'Car Loan'").fetchone()[0]
    goals.create(conn, {"kind": "save", "name": "Rainy day", "target": 12000, "track": "account", "account_id": "acc_nova_sav"}, today)
    goals.create(conn, {"kind": "save", "name": "New laptop", "target": 2000, "track": "earmark", "account_id": "acc_nova_chk",
                        "earmark": 1500, "target_month": demo_data_month_day(today, 6, 1)[:7]}, today)
    goals.create(conn, {"kind": "save", "name": "Concert tickets", "target": 600, "track": "earmark", "account_id": "acc_nova_chk",
                        "earmark": 400}, today)
    trip = goals.create(conn, {"kind": "save", "name": "Trip to Lisbon", "target": 1800, "track": "manual"}, today)
    for back, amount, note in ((70, 300.0, "Tax refund"), (40, 250.0, None), (12, 150.0, "Paycheck"), (5, -50.0, "Deposit back")):
        with conn:
            conn.execute("INSERT INTO goal_entries (goal_id, date, amount, note, created_at) VALUES (?,?,?,?,?)",
                         (trip["id"], (today - timedelta(days=back)).isoformat(), amount, note, db.now()))
    payoff = goals.create(conn, {"kind": "payoff", "account_id": insights.manual_nw_id(loan)}, today)
    goals.create(conn, {"kind": "monthly", "target": 800}, today)
    with conn:  # as if made months ago: the loan has come down since (its balance then)
        conn.execute("UPDATE goals SET start_balance = 7400.0, created_at = ? WHERE id = ?", ("2026-04-02T17:00:00Z", payoff["id"]))
        # the checking account now holds less than its two earmarks: Concert tickets (the newer) is short
        conn.execute("UPDATE accounts SET current_balance = 1720.37 WHERE account_id = 'acc_nova_chk'")
    # splits: a Groceries run with household things this month (on the 1st, so on any day it's this month's and never the
    # newest row), a bigger Other Shopping order last month; and a dinner on the 1st, so this month always has an Eating Out
    # charge to split in the sheet (tests/test_browser_task19.py). All three are charged here, on Nova Checking (history
    # since 2024 on any day). Baseline K6: picking one of demo_data's random charges instead found no Groceries one this
    # month on some days (the 1st to the 8th of Oct 2026: a TypeError)
    this_one, last_one = ("feat_split_groceries", 86.47), ("feat_split_order", 148.60)
    first = today.replace(day=1).isoformat()
    with conn:
        _txn(conn, this_one[0], "acc_nova_chk", "demo_nova", first, "TRADER JOE'S #127", "Trader Joe's", this_one[1],
             demo_data.PFC["groceries"], "trader_joes_1.png")
        _txn(conn, last_one[0], "acc_nova_chk", "demo_nova", demo_data_month_day(today, -1, 18), "AMAZON MKTPL*7Q2",
             "Amazon", last_one[1], demo_data.PFC["shopping"], "amazon_1.png")
        _txn(conn, "feat_dinner", "acc_nova_chk", "demo_nova", first, "DOORDASH*SWEETGREEN", "DoorDash", 36.85,
             demo_data.PFC["restaurant"], "doordash_1.png")
    txmeta.split(conn, this_one[0], [{"category": "Groceries", "amount": round(this_one[1] - 22.25, 2)},
                                     {"category": "Other Shopping", "amount": 22.25}])
    txmeta.split(conn, last_one[0], [{"category": "Other Shopping", "amount": round(last_one[1] - 35.0, 2)},
                                     {"category": "Entertainment", "amount": 35.0}])
    txmeta.set_meta(conn, this_one[0], {"note": "Paper towels, detergent and trash bags on the Shopping line.", "tags": ["household"]})
    # the newest 3 (last month's too early in a month, so always 3)
    for tid in [r[0] for r in conn.execute("SELECT transaction_id FROM v_transactions WHERE category IN ('Other Transport', 'Eating Out') "
                                           "AND pending = 0 ORDER BY date DESC, transaction_id LIMIT 3")]:
        txmeta.set_meta(conn, tid, {"tags": ["trip"]})
    txmeta.save_filter(conn, "Tagged: trip", {"tag": "trip"})
    txmeta.save_filter(conn, "Amazon", {"q": "amazon"})
    rollover.set_enabled(conn, "Eating Out", True)
    rollover.set_enabled(conn, "Other Transport", True)
    # Alpaca: a week of daily snapshots, ACTIVE, equity = cash + positions (a little lower each day before today)
    for back in range(6, -1, -1):
        day = today - timedelta(days=back)
        drift = round(back * 3.5, 2)
        pos = [{**p, "value": round(p["value"] - (drift if i == 0 else 0), 2)} for i, p in enumerate(ALPACA_POSITIONS)]
        alpaca.save(conn, {"status": "ACTIVE", "account_mask": "4321", "currency": "USD", "cash": ALPACA_CASH,
                           "equity": round(ALPACA_CASH + sum(p["value"] for p in pos), 2), "reported_equity": None,
                           "positions": pos}, day)
    with conn:
        conn.execute("INSERT OR REPLACE INTO institutions VALUES (?,?,?,?,?,?, '2026-09-26T00:00:00Z')",
                     (alpaca.INSTITUTION_ID, "Alpaca", logo_png(48), "site", None, alpaca.SITE))
    add_bills(conn, today)
    add_transit(conn, today)


def add_transit(conn, today: date) -> None:
    """TASK-22f: a card payment in transit (MOCK-022's screen 5): Nova Checking paid the Chase card ••3591 $212.34
    yesterday and the card hasn't posted it, so net worth, the Loans & debt tile and Accounts count it once, with its
    line, and the card's own row says it's on its way."""
    day = (today - timedelta(days=1)).isoformat()
    with conn:
        conn.execute("""INSERT INTO transactions (transaction_id, account_id, item_id, date, authorized_date, name, amount,
                            iso_currency, pending, pfc_primary, pfc_detailed, pfc_confidence, removed, first_seen_at,
                            updated_at)
                        VALUES ('feat_transit', 'acc_nova_chk', 'demo_nova', ?, ?, 'CHASE CREDIT CRD 3591', 212.34, 'USD', 0,
                                'LOAN_PAYMENTS', 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT', 'HIGH', 0, ?, ?)""",
                     (day, day, db.now(), db.now()))


GAS = (39.77, 31.2, 22.4, 12.05, 8.4, 5.3, 4.12, 4.9, 6.2)   # SoCalGas's real spread: 9 monthly charges, winter to summer
LADWP = "Los Angeles Department of Water and Power"


def add_bills(conn, today: date) -> None:
    """TASK-21: a power bill whose amount changes with the season, with the real SoCalGas's spread (the demo already has
    a steady "SoCalGas"; found by its schedule alone: "~$5 est.", usually $4–40),
    LADWP (water & power, every 2 months, one charge in the data, on the Freedom Unlimited card ••3591) tracked by hand
    with what it usually is ($100–150; that $295.50 charge was big: "higher than usual", next ~31 days on: the calendar's
    every-2-months chip), and a change an MCP agent asked for, waiting in Settings for the owner's OK."""
    from hermes_finance import bills, mcp_state
    # this month's charge from the 5th: on the 4th the 8th charge back was on the 7-month window's first day, so found
    # from 8 charges on that day alone, 7 on the others (baseline K6)
    k0 = 0 if today.day > 4 else -1
    with conn:
        for k, amount in enumerate(GAS):
            _txn(conn, f"sce_{k}", "acc_nova_chk", "demo_nova", demo_data_month_day(today, k0 - len(GAS) + 1 + k, 4),
                 "SO CAL EDISON CO", "Southern California Edison", amount,
                 ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY"))
        _txn(conn, "ladwp_0", "acc_chase_cc", "demo_chase", (today - timedelta(days=29)).isoformat(), "CITY OF LA DWP", LADWP,
             295.50, ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_WATER"))
    bills.apply(conn, bills.plan(conn, today, "track", merchant=LADWP, every="2m", varies=True, usually=[100, 150]), today)
    # the agent's bill is Netflix, on the card on the 12th: its last 4 charges, as on 2026-09-30 (baseline K6:
    # demo_data's Chase history starts on May 23 of today's year, so before June 12 there was none: a BillError)
    with conn:
        for k in range(4):
            day = demo_data_month_day(today, -k - (today.day < 12), 12)
            if not conn.execute("SELECT 1 FROM transactions WHERE merchant_name = 'Netflix' AND date = ?", (day,)).fetchone():
                _txn(conn, f"feat_netflix_{k}", "acc_chase_cc", "demo_chase", day, "Netflix", "Netflix", 15.49,
                     demo_data.PFC["tv"], "netflix_1.png")
    ask = {"action": "track", "merchant": "Netflix", "every": "1m", "varies": False}
    mcp_state.record_pending(conn, "finance_track_bill", ask, bills.plan(conn, today, **{**ask, "merchant": "Netflix"})["summary"],
                             "Claude Desktop")


def build_features(path: Path, today: date | None = None) -> None:
    today = today or date.today()
    build_full(path, today)
    conn = db.connect(path)
    add_features(conn, today)
    conn.close()


BUILDERS = {"full": build_full, "nocaps": build_nocaps, "empty": build_empty, "history-empty": build_history_empty,
            "onepoint": build_onepoint, "features": build_features, "signin": build_signin,
            "signin-setup": build_signin_setup}
AUTH_KINDS = ("signin", "signin-setup")


def build(kind: str, path: Path, today: date | None = None) -> Path:
    BUILDERS[kind](path, today)
    # The layout plan includes rule suggestions. Seed actual user-filed charges that produce one through the
    # normal rulebook path, rather than expecting an unseeded state or faking its API response.
    if kind in KINDS and kind != 'empty':
        from hermes_finance import classify, rulebook
        conn = db.connect(path)
        try:
            day = (today or date.today()).isoformat()
            for i in range(rulebook.MIN_TIMES):
                tid = f'gate_rule_suggestion_{i}'
                with conn:
                    _txn(conn, tid, 'acc_nova_chk', 'demo_nova', day, 'Gate Stationery', 'Gate Stationery',
                         1.25, demo_data.PFC['shopping'])
                classify.set_category(conn, tid, 'Other Shopping')
            assert rulebook.suggestions(conn), 'rules-suggestions must be reachable on this fixture'
        finally:
            conn.close()
    return path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out = Path(argv[1])
    if out.resolve() == (config.project_root() / "data").resolve():
        print("refusing to write into the live data/ directory")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        path = out / f"{kind}.db"
        if path.exists():
            print(f"refusing to overwrite {path}")
            return 1
        build(kind, path)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
