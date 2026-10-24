"""Synthetic but realistic data for smoke tests and screenshots (no real data, no Plaid).

    uv run python tests/demo_data.py /tmp/some-dir      # writes /tmp/some-dir/finance.db
    uv run python tests/demo_data.py /tmp/some-dir 30   # ... with 30 days of holding snapshots

Three fake banks with very different history depths (Nova from 2024-09, Chase from May 23, U.S. Bank from
the 1st of this month and broken), recurring bills and subscriptions, caps, rules, pending rows, logos,
and two weeks of balance snapshots. `extras()` (run by the command line too) adds what the Investments and
Loans & debt pages need: holdings with today's holding snapshot, card liabilities, and manual accounts (a
car loan matched to its bank payments, a paid-off loan, a car).
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from hermes_finance import classify, config, db, manual

LOGOS = "https://plaid-merchant-logos.plaid.com/"
ICONS = "https://plaid-category-icons.plaid.com/PFC_"
PFC = {
    "groceries": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
    "restaurant": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),
    "coffee": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"),
    "shopping": ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"),
    "superstore": ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES"),
    "gas": ("TRANSPORTATION", "TRANSPORTATION_GAS"),
    "taxi": ("TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"),
    "parking": ("TRANSPORTATION", "TRANSPORTATION_PARKING"),
    "pharmacy": ("MEDICAL", "MEDICAL_OTHER_MEDICAL"),   # stays Other Health (v16 maps MEDICAL_PHARMACIES_AND_SUPPLEMENTS to Pharmacy)
    "dentist": ("MEDICAL", "MEDICAL_OTHER_MEDICAL"),   # stays Other Health (v16 maps MEDICAL_DENTAL_CARE to Doctor & Dentist)
    "tv": ("ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES"),
    "music": ("ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO"),
    "flights": ("TRAVEL", "TRAVEL_FLIGHTS"),
    "rent": ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT"),
    "gas_electric": ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY"),
    "phone": ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_TELEPHONE"),
    "insurance": ("GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE"),
    "services": ("GENERAL_SERVICES", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"),
    "car_payment": ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CAR_PAYMENT"),
    "card_payment": ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
    "wages": ("INCOME", "INCOME_WAGES"),
    "transfer_out": ("TRANSFER_OUT", "TRANSFER_OUT_SAVINGS"),
    "transfer_in": ("TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER"),
    "fees": ("BANK_FEES", "BANK_FEES_ATM_FEES"),
}
# name, pfc key, logo file (or None), (low, high), visits per month
EVERYDAY = [
    ("Trader Joe's", "groceries", "trader_joes_1.png", (18, 95), 5),
    ("Costco", "groceries", "costco_1.png", (60, 190), 1.5),
    ("Whole Foods Market", "groceries", None, (15, 70), 2),
    ("Chipotle", "restaurant", "chipotle_1.png", (11, 18), 3),
    ("Blue Bottle Coffee", "coffee", None, (5, 9), 4),
    ("Sweetgreen", "restaurant", None, (14, 21), 2),
    ("DoorDash", "restaurant", "doordash_1.png", (22, 48), 1.5),
    ("Amazon", "shopping", "amazon_1.png", (8, 130), 6),
    ("Target", "superstore", "target_1.png", (15, 90), 1.5),
    ("Shell", "gas", "shell_1.png", (38, 62), 2.5),
    ("Uber", "taxi", "uber_1060.png", (11, 34), 2.5),
    ("CVS Pharmacy", "pharmacy", "cvs_1.png", (8, 45), 1.5),
    ("AMC Theatres", "tv", None, (14, 32), 0.5),
]
# name, pfc key, logo, amount (or (low, high)), day of month, account key
MONTHLY = [
    ("Innago", "rent", None, 2252.00, 3, "nova_chk"),
    ("SoCalGas", "gas_electric", None, (19.6, 24.9), 11, "nova_chk"),
    ("AT&T", "phone", "att_1.png", 45.23, 14, "nova_chk"),
    ("GEICO", "insurance", "geico_1.png", 165.03, 26, "nova_chk"),
    ("Zenith Financial Services", "car_payment", None, 465.10, 29, "nova_chk"),
    ("Netflix", "tv", "netflix_1.png", 15.49, 12, "chase_cc"),
    ("Spotify", "music", "spotify_1.png", 11.99, 18, "chase_cc"),
    ("OpenAI", "services", None, 20.00, 27, "chase_cc"),
    ("iCloud", "services", "apple_1.png", 2.99, 8, "nova_chk"),
]
# GEICO and Zenith filed by hand (v16's map says Car Payment for Zenith)
RULES = [("OpenAI", "Subscriptions"), ("iCloud", "Subscriptions"), ("Namecheap", "Subscriptions"),
         ("GEICO", "Other Transport"), ("Zenith Financial Services", "Other Transport")]
CAPS = {"Other Health": 225, "Eating Out": 250, "Other Shopping": 850, "Entertainment": 50, "Other Transport": 600}


def build(conn, today: date | None = None, seed: int = 4) -> None:
    today = today or date.today()
    rnd = random.Random(seed)
    now = db.now()
    synced = (datetime.now(timezone.utc) - timedelta(minutes=42)).strftime("%Y-%m-%dT%H:%M:%SZ")
    month_start = today.replace(day=1)
    banks = [
        ("demo_nova", "ins_demo_nova", "Nova", date(2024, 9, 3), "active", None),
        ("demo_chase", "ins_demo_chase", "Chase", today.replace(month=5, day=23), "active", None),
        ("demo_usb", "ins_demo_usb", "U.S. Bank", month_start, "login_required", "ITEM_LOGIN_REQUIRED"),
    ]
    accounts = {  # key: (account_id, item_id, name, official, mask, type, subtype, current, available, limit)
        "nova_chk": ("acc_nova_chk", "demo_nova", "Nova Checking", "Nova Checking Account", "7385", "depository",
                     "checking", 2410.37, 2410.37, None),
        "nova_sav": ("acc_nova_sav", "demo_nova", "Nova Savings", "Nova Savings Account", "7731", "depository",
                     "savings", 9583.12, 9583.12, None),
        "nova_vault": ("acc_nova_vault", "demo_nova", "Emergency Fund Vault", None, "6084", "depository", "savings",
                       0.42, 0.42, None),
        "nova_inv": ("acc_nova_inv", "demo_nova", "Nova Self-directed", "Nova Invest", "6820", "investment",
                     "brokerage", 491.08, None, None),
        "chase_cc": ("acc_chase_cc", "demo_chase", "Freedom Unlimited", "Chase Freedom Unlimited", "3591", "credit",
                     "credit card", 991.44, 4008.56, 5000.0),
        "usb_chk": ("acc_usb_chk", "demo_usb", "Smartly Checking", "U.S. Bank Smartly Checking", "5108", "depository",
                    "checking", 1840.00, 1790.00, None),
        "usb_cc": ("acc_usb_cc", "demo_usb", "Altitude Go Visa", None, "7266", "credit", "credit card", 305.18,
                   2694.82, 3000.0),
    }
    with conn:
        for item_id, inst_id, name, _, status, code in banks:
            conn.execute(
                """INSERT INTO items (item_id, institution_id, institution_name, access_token, status, error_code,
                       error_message, products, txn_update_status, created_at, last_sync_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, inst_id, name, f"access-production-demo-{item_id}", status, code,
                 "the login details of this item have changed" if code else None, '["transactions"]',
                 "HISTORICAL_UPDATE_COMPLETE", now, synced))
        for acc in accounts.values():
            conn.execute(
                """INSERT INTO accounts (account_id, item_id, name, official_name, mask, type, subtype, current_balance,
                       available_balance, credit_limit, iso_currency, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'USD',?)""",
                (*acc, now))

    starts = {b[0]: b[3] for b in banks}
    rows: list[tuple] = []
    n = 0

    def add(day: date, amount: float, name: str, pfc: str, acc_key: str, logo: str | None = None,
            merchant: str | None = None, pending: bool = False) -> None:
        nonlocal n
        acc = accounts[acc_key]
        if day < starts[acc[1]] or day > today:
            return
        n += 1
        primary, detailed = PFC[pfc]
        tid = f"demo_{n:05d}"
        logo_url = LOGOS + logo if logo else None
        icon = f"{ICONS}{primary}.png"
        raw = {"transaction_id": tid, "account_id": acc[0], "amount": amount, "date": day.isoformat(), "name": name,
               "merchant_name": merchant, "pending": pending, "logo_url": logo_url,
               "personal_finance_category": {"primary": primary, "detailed": detailed, "confidence_level": "HIGH"},
               "personal_finance_category_icon_url": icon}
        rows.append((tid, acc[0], acc[1], day.isoformat(), day.isoformat(), name, merchant, round(amount, 2),
                     int(pending), primary, detailed, logo_url, json.dumps(raw), now, now))

    first = date(2024, 9, 1)
    m = first
    while m <= today:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        days = (nxt - m).days
        card = "chase_cc" if m >= starts["demo_chase"].replace(day=1) else "nova_chk"
        for name, pfc, logo, (lo, hi), per_month in EVERYDAY:
            for _ in range(int(per_month) + (rnd.random() < per_month % 1)):
                d = m + timedelta(days=rnd.randrange(days))
                acc = card if rnd.random() < 0.7 else "nova_chk"
                if m == month_start and rnd.random() < 0.25:
                    acc = "usb_cc"
                add(d, rnd.uniform(lo, hi), name.upper() if rnd.random() < 0.3 else name, pfc, acc, logo, name)
        for name, pfc, logo, amount, dom, acc in MONTHLY:
            d = m.replace(day=min(dom, days))
            value = rnd.uniform(*amount) if isinstance(amount, tuple) else amount
            if name == "Innago" and m.month in (1, 7):
                value = 2150.00  # rent change mid-history is still "stable"
            add(d, value, name, pfc, acc, logo, name)
        for dom in (10, 25):  # paychecks
            d = m.replace(day=min(dom, days))
            add(d, -2353.10, f"Electronic Deposit {d:%m/%d} 246810 Orbitex L", "wages", "nova_chk")
        add(m.replace(day=min(26, days)), 500.00, "To Savings - 7731", "transfer_out", "nova_chk")
        add(m.replace(day=min(26, days)), -500.00, "From Checking - 7385", "transfer_in", "nova_sav")
        add(m.replace(day=min(20, days)), round(rnd.uniform(600, 1400), 2), "Chase Credit Card Payment",
            "card_payment", "nova_chk")
        if rnd.random() < 0.35:
            add(m + timedelta(days=rnd.randrange(days)), round(rnd.uniform(180, 520), 2), "United Airlines", "flights",
                card, "united_airlines_1.png", "United Airlines")
        if rnd.random() < 0.5:
            add(m + timedelta(days=rnd.randrange(days)), 80.00, "Zelle Payment to Corner Barber", "services", "nova_chk")
        m = nxt
    # weekly parking, yearly domain
    d = today - timedelta(days=7 * 20)
    while d <= today:
        add(d, 6.99 if d.day % 3 else 7.49, "SpotHero", "parking", "chase_cc", "spothero_1.png", "SpotHero",
            pending=d == today)
        d += timedelta(days=7)
    for y in (today.year - 2, today.year - 1):
        add(date(y, 10, 1), 12.99, "NAMECHEAP.COM", "services", "nova_chk", None, "Namecheap")
    # this month: a dentist visit (Other Health goes over its cap), a couple of review items, pending rows
    add(month_start + timedelta(days=min(8, (today - month_start).days)), 240.00, "Smile Dental", "dentist", "nova_chk",
        None, "Smile Dental")
    add(month_start, -1500.00, "Mobile Deposit", "transfer_in", "usb_chk")  # U.S. Bank history starts on the 1st
    add(today - timedelta(days=1), 0.01, "246810 ORBITEX L", "services", "nova_sav")
    add(today, 6.45, "Blue Bottle Coffee", "coffee", "usb_cc", None, "Blue Bottle Coffee", pending=True)
    add(today, 23.48, "AMAZON MKTPL*2K4", "shopping", "chase_cc", "amazon_1.png", "Amazon", pending=True)
    # a merchant name full of markup: must render as plain text
    prev = (month_start - timedelta(days=1)).replace(day=12)
    add(prev, 18.40, "Joe's <b>Diner</b> <img src=x onerror=alert(1)>", "restaurant", "chase_cc", None,
        "Joe's <b>Diner</b> <img src=x onerror=alert(1)>")

    with conn:
        conn.executemany(
            """INSERT INTO transactions (transaction_id, account_id, item_id, date, authorized_date, name, merchant_name,
                   amount, iso_currency, pending, pfc_primary, pfc_detailed, pfc_confidence, logo_url, raw_json,
                   removed, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,'USD',?,?,?,'HIGH',?,?,0,?,?)""", rows)
        for category, value in CAPS.items():
            conn.execute("UPDATE categories SET monthly_cap = ? WHERE name = ?", (value, category))
    for pattern, category in RULES:
        classify.add_rule(conn, "merchant_name", pattern, category, created_from="user")
    classify.apply(conn)
    # two weeks of real-looking daily balance snapshots ending at today's balances
    with conn:
        for acc in accounts.values():
            bal = acc[7]
            trend = rnd.uniform(-0.004, 0.006) * max(bal, 50)  # a gentle daily drift plus a little noise
            for back in range(13, -1, -1):
                day = today - timedelta(days=back)
                if day < starts[acc[1]]:
                    continue
                drift = 0 if back == 0 else -trend * back + rnd.uniform(-0.004, 0.004) * max(bal, 50)
                conn.execute("INSERT OR REPLACE INTO balance_snapshots VALUES (?,?,?,?, 'USD')",
                             (acc[0], day.isoformat(), round(bal + drift, 2), acc[8]))


def extras(conn, today: date | None = None, holding_days: int = 1) -> None:
    """Holdings (+ `holding_days` of daily holding snapshots ending today), card liabilities, manual accounts."""
    today = today or date.today()
    ts = db.now()
    # values add up to the account's balance (491.08), as they do in one Plaid holdings response
    positions = [("sec_nvda", "NVDA", "NVIDIA Corp", "equity", 2.18176, 224.82, 490.51),
                 ("sec_cash", "CUR:USD", "U S Dollar", "cash", 0.57, 1.0, 0.57)]
    with conn:
        for sec, ticker, name, typ, qty, price, value in positions:
            conn.execute("INSERT OR REPLACE INTO holdings (account_id, security_id, item_id, ticker_symbol, security_name, "
                         "security_type, quantity, institution_price, institution_value, iso_currency, updated_at) "
                         "VALUES ('acc_nova_inv', ?, 'demo_nova', ?, ?, ?, ?, ?, ?, 'USD', ?)",
                         (sec, ticker, name, typ, qty, price, value, ts))
        rnd = random.Random(9)
        value = 490.51
        for back in range(holding_days):  # walk backwards from today's value
            day = (today - timedelta(days=back)).isoformat()
            conn.execute("INSERT OR REPLACE INTO holding_snapshots VALUES (?, 'acc_nova_inv', 'sec_nvda', 'NVDA', 2.18176, ?, ?)",
                         (day, round(value / 2.18176, 2), round(value, 2)))
            conn.execute("INSERT OR REPLACE INTO holding_snapshots VALUES (?, 'acc_nova_inv', 'sec_cash', 'CUR:USD', 0.57, 1, 0.57)",
                         (day,))
            value /= 1 + (rnd.random() - 0.47) * 0.03
        for acc, item, due, minimum, apr in (("acc_chase_cc", "demo_chase", 2, 35.0, 25.99), ("acc_usb_cc", "demo_usb", 5, 40.0, 0.0)):
            conn.execute("INSERT OR REPLACE INTO liabilities (account_id, kind, item_id, minimum_payment_amount, "
                         "next_payment_due_date, interest_rate, updated_at) VALUES (?, 'credit', ?, ?, ?, ?, ?)",
                         (acc, item, minimum, (today + timedelta(days=due)).isoformat(), apr, ts))
    as_of = (today.replace(day=1) - timedelta(days=17)).isoformat()   # mid last month: last month's payment applies
    manual.create(conn, {"kind": "loan", "name": "Zenith", "institution": "Zenith Financial Services", "subtype": "auto",
                         "original_amount": 30947.0, "balance": 5910.0, "monthly_payment": 465.10,
                         "match_pattern": "Zenith Financial", "balance_as_of": as_of}, today)
    manual.match_loans(conn)
    manual.create(conn, {"kind": "loan", "name": "Other loan", "institution": "Affirm", "subtype": "other",
                         "original_amount": 2250.0, "balance": 0.0, "monthly_payment": 187.50}, today)
    manual.create(conn, {"kind": "vehicle", "vehicle_make": "Zenith", "vehicle_model": "Mesa", "vehicle_year": 2019,
                         "balance": 20000.0, "balance_as_of": (today - timedelta(days=60)).isoformat()}, today)


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__)
        return 2
    path = Path(argv[1]) / "finance.db"
    if Path(argv[1]).resolve() == (config.project_root() / "data").resolve():
        print("refusing to write into the live data/ directory")
        return 1
    if path.exists():
        print(f"refusing to overwrite {path}")
        return 1
    conn = db.connect(path)
    build(conn)
    extras(conn, holding_days=int(argv[2]) if len(argv) > 2 else 1)
    count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.close()
    print(f"wrote {count} synthetic transactions to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
