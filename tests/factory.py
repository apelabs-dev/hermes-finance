"""Tiny builders for dashboard/insights fixture DBs: rows go straight into the tables (no Plaid mock)."""

from __future__ import annotations

import json
from itertools import count

from hermes_finance import db

# category -> a Plaid (primary, detailed) pair that maps to it through plaid_category_map
PFC = {
    "Groceries": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
    "Eating Out": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),
    "Other Shopping": ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"),
    "Entertainment": ("ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES"),
    "Other Health": ("MEDICAL", "MEDICAL_OTHER_MEDICAL"),                    # v16: a pharmacy is Pharmacy
    "Other Transport": ("TRANSPORTATION", "TRANSPORTATION_TOLLS"),           # v16: gas is Gas
    "Rent & Utilities": ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT"),
    "Phone": ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_TELEPHONE"),          # -> Internet & Phone (v16)
    "Water": ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_WATER"),              # -> Rent & Utilities (TASK-21: a utility)
    "Gas & electric": ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY"),  # -> Rent & Utilities (a utility)
    "Income": ("INCOME", "INCOME_WAGES"),
    "Transfers": ("TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER"),
    "Card payment": ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),  # -> Transfers
    "Car payment": ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CAR_PAYMENT"),           # -> Car Payment (v16)
    "Insurance": ("GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE"),         # -> Insurance (v16)
    "Other": ("GENERAL_SERVICES", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"),
    "Travel": ("TRAVEL", "TRAVEL_FLIGHTS"),
    "Fees": ("BANK_FEES", "BANK_FEES_OTHER_BANK_FEES"),
}
_ids = count(1)


def bank(conn, item_id="item_a", name="Chase", institution_id=None, status="active", error_code=None,
         removed=False, last_sync="2026-09-25T15:11:00Z"):
    with conn:
        conn.execute(
            """INSERT INTO items (item_id, institution_id, institution_name, access_token, status, error_code,
                   error_message, products, created_at, last_sync_at, removed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, institution_id or f"ins_{item_id}", name, f"access-production-{item_id}-0000", status,
             error_code, f"mock {error_code}" if error_code else None, '["transactions"]', db.now(), last_sync,
             db.now() if removed else None))


def acct(conn, account_id="acc_chk", item_id="item_a", name="Checking", type_="depository", subtype="checking",
         current=1000.0, available=None, limit=None, mask="1234", official_name=None):
    with conn:
        conn.execute(
            """INSERT INTO accounts (account_id, item_id, name, official_name, mask, type, subtype, current_balance,
                   available_balance, credit_limit, iso_currency, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'USD',?)""",
            (account_id, item_id, name, official_name, mask, type_, subtype, current, available, limit, db.now()))


def tx(conn, date, amount, merchant=None, name=None, cat="Other Shopping", account_id="acc_chk", item_id="item_a",
       pending=False, logo=None, icon=None, tid=None):
    tid = tid or f"t{next(_ids)}"
    primary, detailed = PFC[cat]
    raw = {"transaction_id": tid, "amount": amount, "date": date, "name": name or merchant, "merchant_name": merchant,
           "personal_finance_category": {"primary": primary, "detailed": detailed},
           "personal_finance_category_icon_url": icon, "logo_url": logo, "pending": pending}
    with conn:
        conn.execute(
            """INSERT INTO transactions (transaction_id, account_id, item_id, date, authorized_date, name, merchant_name,
                   amount, iso_currency, pending, pfc_primary, pfc_detailed, pfc_confidence, logo_url, raw_json,
                   removed, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?, 'USD', ?,?,?, 'HIGH', ?,?, 0, ?, ?)""",
            (tid, account_id, item_id, date, date, name or merchant, merchant, amount, int(pending), primary, detailed,
             logo, json.dumps(raw), db.now(), db.now()))
    return tid


def cap(conn, category, value):
    with conn:
        conn.execute("UPDATE categories SET monthly_cap=? WHERE name=?", (value, category))


def snapshot(conn, account_id, day, current, available=None):
    with conn:
        conn.execute("INSERT OR REPLACE INTO balance_snapshots (account_id, date, current, available, iso_currency) "
                     "VALUES (?,?,?,?, 'USD')", (account_id, day, current, available))


def basic(conn):
    """One healthy bank with a checking account and a credit card."""
    bank(conn)
    acct(conn)
    acct(conn, "acc_cc", name="Card", type_="credit", subtype="credit card", current=250.0, limit=5000.0, mask="9999")
