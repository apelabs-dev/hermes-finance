"""TASK-22f: MOCK-022's Sync now scene on synthetic data, for the browser tests and the screenshots (shots/022f).

build(path, today) is the gate's `full` DB (tests/gate_dbs.py: Nova, Chase and U.S. Bank with months of history) with the
mock's eight Plaid banks (PayPal, Citibank Online, Wells Fargo, American Express and Capital One added, U.S. Bank healthy),
Alpaca (read live: no Plaid slot, nothing to sync) and the mock's balances before its 6:31 AM sync. SceneFake is a Plaid
transport whose first sync of each bank brings in that sync's changes: 12 new transactions (4 of them spending: Zenith
Financial Services $465.10, Costco $269.14, Whole Foods $10.09 pending, Xai $5.00; 8 transfers and card payments), 10
updated, and 5 balances moved (Chase ••3591 $994.42 -> $22.29 the largest). A second sync brings nothing new.

  mode "relogin"      Wells Fargo answers ITEM_LOGIN_REQUIRED (the other 7 sync)
  mode "unreachable"  every call fails with a passing error (Plaid out of reach)

build(..., transit=True) is the day before instead (MOCK-022's screen 5): Nova Checking ••7385 paid Chase $994.42
yesterday, and Chase ••3591 still shows it owed: a payment in transit. presync() runs the 6:31 sync itself (the state
after it: a Sync now then brings nothing new).

Every figure is made up or copied from the mock's own ledger (sketches/022-sync/NOTES.md); nothing here reads the live DB.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import gate_dbs
from conftest import FAKE_CLIENT_ID, FAKE_SECRET
from hermes_finance import alpaca, db, manual
from hermes_finance.config import Settings
from hermes_finance.plaid import PlaidClient
from hermes_finance.transit import DAYS as TRANSIT_DAYS
from pngtools import logo_png

# (item_id, institution_id, name): the mock's order (created_at)
BANKS = [("demo_nova", "ins_126339", "Nova"), ("scene_paypal", "ins_22", "PayPal"), ("demo_chase", "ins_56", "Chase"),
         ("scene_citi", "ins_5", "Citibank Online"), ("scene_wf", "ins_127991", "Wells Fargo"),
         ("demo_usb", "ins_127990", "U.S. Bank"), ("scene_amex", "ins_10", "American Express"),
         ("scene_capone", "ins_128026", "Capital One")]
COLORS = {"ins_126339": "#00a9ce", "ins_22": "#009cde", "ins_56": "#095aa6", "ins_5": "#204081", "ins_127991": "#d11f37",
          "ins_127990": "#102670", "ins_10": "#007cc3", "ins_128026": "#00668f"}
# the accounts the scene adds (account_id, item_id, name, mask, type, subtype, current, limit)
ADDED = [("acc_paypal", "scene_paypal", "PayPal", None, "depository", "paypal", 38.62, None),
         ("acc_wf_cc", "scene_wf", "WELLS FARGO ACTIVE CASH VISA CARD", "1618", "credit", "credit card", 296.92, 8000.0),
         ("acc_amex_chk", "scene_amex", "Rewards Checking", "9641", "depository", "checking", 0.0, None),
         ("acc_capone_chk", "scene_capone", "360 Checking", "5086", "depository", "checking", 0.0, None)]
BEFORE = {"acc_nova_chk": 260.0, "acc_nova_sav": 8986.80, "acc_nova_inv": 491.62, "acc_chase_cc": 994.42,
          "acc_usb_cc": 390.97, "acc_usb_chk": 1840.0}
AFTER = BEFORE | {"acc_nova_sav": 8370.44, "acc_nova_inv": 499.89, "acc_chase_cc": 22.29, "acc_usb_cc": 560.50,
                  "acc_wf_cc": 304.91}
MOVED = ["acc_chase_cc", "acc_nova_sav", "acc_usb_cc", "acc_nova_inv", "acc_wf_cc"]   # largest move first
SPEND = ["Zenith Financial Services", "Costco", "Whole Foods", "Xai"]                 # largest first
LOGOS = Path.home() / "projects" / "hermes-finance" / "sketches" / "022-sync" / "logos"   # the mock's (read only)
PFC = {"transfer_in": ("TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER"),
       "transfer_out": ("TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER"),
       "card": ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
       "car": ("TRANSPORTATION", "TRANSPORTATION_OTHER_TRANSPORTATION"),
       "shop": ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES"),
       "groceries": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
       "ai": ("GENERAL_SERVICES", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES")}


def build(path: Path, today: date | None = None, real_logos: bool = False, transit: bool = False) -> Path:
    """The scene's DB at `path` (made fresh). real_logos: the bank logos from the mock's folder, when it's there (the
    screenshots), else the tests' drawn ones. transit: the card payment in transit (see the module's docstring)."""
    today = today or date.today()
    gate_dbs.build("full", path, today)
    conn = db.connect(path)
    try:
        synced = f"{today.isoformat()}T13:31:20Z"
        with conn:
            conn.execute("UPDATE items SET status = 'active', error_code = NULL, error_message = NULL, cursor = NULL, "
                         "last_sync_at = ?", (synced,))
            for k, (item_id, inst, name) in enumerate(BANKS):
                created = f"2024-09-{k + 3:02d}T12:00:00Z"
                if conn.execute("SELECT 1 FROM items WHERE item_id = ?", (item_id,)).fetchone():
                    conn.execute("UPDATE items SET institution_id = ?, created_at = ? WHERE item_id = ?", (inst, created, item_id))
                else:
                    conn.execute("""INSERT INTO items (item_id, institution_id, institution_name, access_token, status,
                                        products, txn_update_status, created_at, last_sync_at)
                                    VALUES (?,?,?,?, 'active', '["transactions"]', 'HISTORICAL_UPDATE_COMPLETE', ?, ?)""",
                                 (item_id, inst, name, f"access-production-{item_id}", created, synced))
            for account_id, item_id, name, mask, type_, subtype, current, limit in ADDED:
                conn.execute("""INSERT INTO accounts (account_id, item_id, name, mask, type, subtype, current_balance,
                                    available_balance, credit_limit, iso_currency, updated_at)
                                VALUES (?,?,?,?,?,?,?,?,?, 'USD', ?)""",
                             (account_id, item_id, name, mask, type_, subtype, current, None, limit, db.now()))
            # the full DB keeps its Citi card under Chase: it moves to its own bank, paid off
            conn.execute("UPDATE accounts SET item_id = 'scene_citi', current_balance = 0.0, credit_limit = 6800.0 "
                         "WHERE account_id = 'acc_citi_cc'")
            conn.execute("UPDATE transactions SET item_id = 'scene_citi' WHERE account_id = 'acc_citi_cc'")
            # the mock's own manual accounts: a car loan, a car and a store card (the rest put away)
            conn.execute("UPDATE manual_accounts SET archived_at = ? WHERE name NOT IN ('Car Loan', '2019 Zenith Mesa', "
                         "'Best Buy')", (db.now(),))
            for account_id, value in BEFORE.items():
                conn.execute("UPDATE accounts SET current_balance = ? WHERE account_id = ?", (value, account_id))
            conn.execute("UPDATE accounts SET credit_limit = 6700.0, name = 'CREDIT CARD' WHERE account_id = 'acc_chase_cc'")
            conn.execute("UPDATE accounts SET credit_limit = 9000.0 WHERE account_id = 'acc_usb_cc'")
            yesterday = (today - timedelta(days=1)).isoformat()
            for account_id, value in (BEFORE | {a[0]: a[6] for a in ADDED} | {"acc_citi_cc": 0.0}).items():
                for day in (yesterday, today.isoformat()):
                    conn.execute("INSERT OR REPLACE INTO balance_snapshots (account_id, date, current, available, iso_currency) "
                                 "VALUES (?,?,?,NULL,'USD')", (account_id, day, value))
            for _, inst, name in BANKS:
                png = logo_png(48)
                if real_logos:
                    f = next((p for p in (LOGOS / f"inst-{inst}.png", LOGOS / f"inst-{inst}.jpg") if p.exists()), None)
                    png = f.read_bytes() if f else png
                conn.execute("INSERT OR REPLACE INTO institutions VALUES (?,?,?,?,?,?, '2026-09-26T00:00:00Z')",
                             (inst, name, png, "plaid", COLORS[inst], None))
        if transit:   # Nova Checking paid the Chase card yesterday; the card hasn't posted it
            day = (today - timedelta(days=1)).isoformat()
            with conn:
                conn.execute("""INSERT INTO transactions (transaction_id, account_id, item_id, date, authorized_date, name,
                                    amount, iso_currency, pending, pfc_primary, pfc_detailed, pfc_confidence, removed,
                                    first_seen_at, updated_at)
                                VALUES ('s22_transit', 'acc_nova_chk', 'demo_nova', ?, ?, 'CHASE CREDIT CRD', 994.42, 'USD',
                                        0, 'LOAN_PAYMENTS', 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT', 'HIGH', 0, ?, ?)""",
                             (day, day, db.now(), db.now()))
                # The gate DB has its own monthly "Chase Credit Card Payment" (demo_data.py). When one falls inside
                # transit.DAYS it competes with the scene's payment for the same card's single owed balance, so put the
                # gate's Chase card payments in the window away: screen 5 has exactly one payment in transit.
                since = (today - timedelta(days=TRANSIT_DAYS)).isoformat()
                for row in conn.execute(
                        f"""SELECT t.transaction_id, t.name, t.merchant_name FROM transactions t
                            JOIN accounts a ON a.account_id = t.account_id JOIN items i ON i.item_id = t.item_id
                            WHERE t.transaction_id <> 's22_transit' AND t.removed = 0 AND t.pending = 0 AND t.amount > 0
                              AND a.type = 'depository' AND i.removed_at IS NULL
                              AND t.date >= ? AND t.date <= ? AND {manual.CARD_PAYMENT_SQL}""",
                        (since, today.isoformat())).fetchall():
                    if "chase" in f"{row['name'] or ''} {row['merchant_name'] or ''}".casefold():
                        conn.execute("UPDATE transactions SET removed = 1 WHERE transaction_id = ?",
                                     (row["transaction_id"],))
        # Alpaca, read live (no Plaid slot, nothing to sync): one ACTIVE snapshot
        alpaca.save(conn, {"status": "ACTIVE", "account_mask": "4321", "currency": "USD", "cash": gate_dbs.ALPACA_CASH,
                           "equity": round(gate_dbs.ALPACA_CASH + sum(p["value"] for p in gate_dbs.ALPACA_POSITIONS), 2),
                           "reported_equity": None, "positions": gate_dbs.ALPACA_POSITIONS}, today)
        alp = LOGOS / "inst-alpaca.png"
        with conn:
            conn.execute("INSERT OR REPLACE INTO institutions VALUES (?,?,?,?,?,?, '2026-09-26T00:00:00Z')",
                         (alpaca.INSTITUTION_ID, "Alpaca", alp.read_bytes() if real_logos and alp.exists() else logo_png(48),
                          "site", None, alpaca.SITE))
    finally:
        conn.close()
    return path


def _txn(tid, account_id, amount, name, merchant, pfc, day, pending=False):
    return {"transaction_id": tid, "account_id": account_id, "amount": amount, "iso_currency_code": "USD", "date": day,
            "authorized_date": day, "name": name, "merchant_name": merchant, "pending": pending,
            "pending_transaction_id": None, "payment_channel": "online",
            "personal_finance_category": {"primary": PFC[pfc][0], "detailed": PFC[pfc][1], "confidence_level": "HIGH"},
            "logo_url": None, "website": None, "location": {}, "counterparties": []}


def changes(today: date) -> dict[str, list[dict]]:
    """The 6:31 AM sync's new transactions, by bank (12: 4 spending, 8 transfers and card payments)."""
    d0, d1 = today.isoformat(), (today - timedelta(days=1)).isoformat()
    return {
        "demo_nova": [_txn("s22_zenith", "acc_nova_chk", 465.10, "ZENITH FINANCIAL SERVICES", "Zenith Financial Services", "car", d1),
                      _txn("s22_tochk1", "acc_nova_sav", 465.10, "To Checking - 7385", None, "transfer_out", d1),
                      _txn("s22_fromsav1", "acc_nova_chk", -465.10, "From Savings - 7731", None, "transfer_in", d1),
                      _txn("s22_tochk2", "acc_nova_sav", 151.26, "To Checking - 7385", None, "transfer_out", d1),
                      _txn("s22_fromsav2", "acc_nova_chk", -151.26, "From Savings - 7731", None, "transfer_in", d1),
                      _txn("s22_citipay", "acc_nova_chk", 151.26, "CITI CARD ONLINE", None, "card", d1)],
        "scene_paypal": [_txn("s22_costco", "acc_paypal", 269.14, "Costco", "Costco", "shop", d1),
                         _txn("s22_pptransfer", "acc_paypal", -260.00, "Money Transfer from MasterCard Debit Card", None,
                              "transfer_in", d1)],
        "demo_chase": [_txn("s22_wholefoods", "acc_chase_cc", 10.09, "WHOLEFDS", "Whole Foods", "groceries", d0, pending=True),
                       _txn("s22_chasepay", "acc_chase_cc", -994.42, "Payment Thank You-Mobile", None, "card", d1)],
        "scene_citi": [_txn("s22_citithanks", "acc_citi_cc", -151.26, "ONLINE PAYMENT, THANK YOU", None, "card", d1)],
        "scene_wf": [_txn("s22_xai", "acc_wf_cc", 5.00, "X.AI", "Xai", "ai", d1)],
    }


class SceneFake:
    """A Plaid transport (PlaidClient(transport=...)) for the scene. Records every call as (path, item_id)."""

    def __init__(self, conn, today: date, mode: str = "sync"):
        self.today, self.mode, self.calls = today, mode, []
        self.by_token = {r["access_token"]: r["item_id"] for r in conn.execute("SELECT item_id, access_token FROM items")}
        self.accounts = {r["account_id"]: dict(r) for r in conn.execute(
            "SELECT account_id, item_id, name, mask, type, subtype, current_balance, credit_limit FROM accounts")}
        # 10 of Nova's own recent rows come back updated
        self.modified = [{"transaction_id": r["transaction_id"], "account_id": r["account_id"], "amount": r["amount"],
                          "iso_currency_code": "USD", "date": r["date"], "authorized_date": r["date"], "name": r["name"],
                          "merchant_name": r["merchant_name"], "pending": bool(r["pending"]), "pending_transaction_id": None,
                          "payment_channel": "online", "logo_url": r["logo_url"], "website": None, "location": {},
                          "counterparties": [], "personal_finance_category": {
                              "primary": r["pfc_primary"], "detailed": r["pfc_detailed"], "confidence_level": "HIGH"}}
                         for r in conn.execute("SELECT * FROM transactions WHERE item_id = 'demo_nova' AND removed = 0 "
                                               "AND pending = 0 ORDER BY date DESC, transaction_id LIMIT 10")]
        self.new = changes(today)

    def client(self) -> PlaidClient:
        return PlaidClient(Settings(FAKE_CLIENT_ID, FAKE_SECRET, "production"), transport=self)

    def _accounts(self, item_id):
        out = []
        for a in self.accounts.values():
            if a["item_id"] != item_id:
                continue
            cur = AFTER.get(a["account_id"], a["current_balance"])
            out.append({"account_id": a["account_id"], "name": a["name"], "official_name": None, "mask": a["mask"],
                        "type": a["type"], "subtype": a["subtype"],
                        "balances": {"current": cur, "available": None, "limit": a["credit_limit"], "iso_currency_code": "USD"}})
        return out

    def __call__(self, url, data, headers, timeout):
        path, body = urlparse(url).path, json.loads(data)
        item_id = self.by_token.get(body.get("access_token"))
        self.calls.append((path, item_id))
        if self.mode == "unreachable":
            return 503, b"{}"   # an HTTP 5xx with no Plaid body: a passing error
        if self.mode == "relogin" and item_id == "scene_wf":
            return 400, json.dumps({"error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED",
                                    "error_message": "the login details of this item have changed",
                                    "display_message": None, "request_id": "req-scene"}).encode()
        if path == "/item/get":
            return 200, json.dumps({"item": {"item_id": item_id, "products": ["transactions"]}}).encode()
        if path == "/accounts/get":
            return 200, json.dumps({"accounts": self._accounts(item_id)}).encode()
        if path == "/transactions/sync":
            first = not body.get("cursor")
            page = {"added": self.new.get(item_id, []) if first else [],
                    "modified": self.modified if first and item_id == "demo_nova" else [], "removed": [],
                    "next_cursor": "scene-1", "has_more": False, "accounts": self._accounts(item_id),
                    "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE", "request_id": "req-scene"}
            return 200, json.dumps(page).encode()
        if path in ("/transactions/refresh", "/investments/refresh"):
            return 200, json.dumps({"request_id": "req-scene"}).encode()
        return 400, json.dumps({"error_type": "INVALID_REQUEST", "error_code": "UNMOCKED_ENDPOINT", "error_message": path,
                                "display_message": None, "request_id": "req-scene"}).encode()


def presync(conn, fake: SceneFake, at: str | None = None) -> dict:
    """Run the 6:31 sync on `conn` (as if it happened at `at`, the items' last sync: default today 13:31:20Z)."""
    from hermes_finance import syncnow
    out = syncnow.run(conn, fake.client)
    with conn:
        conn.execute("UPDATE items SET last_sync_at = ?", (at or f"{fake.today.isoformat()}T13:31:20Z",))
    return out


def ask_log(conn, item_ids, minutes_ago: float, now: datetime | None = None) -> None:
    """As if these banks were asked for fresh data `minutes_ago` before `now` (default: the server's clock, syncnow.clock)
    (Hermes' finance_sync can ask one bank)."""
    from hermes_finance import syncnow
    at = ((now or syncnow.clock()) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = syncnow._refresh_log(conn) | {i: at for i in item_ids}
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                     "SET value = excluded.value", (syncnow.REFRESH_LOG, json.dumps(log), db.now()))
