"""Hotfix 2026-09-29: a merchant tracked by hand is a bill or a subscription by the same rule as a detected one.

Before, `_tracked_item` hard-coded kind="bill", so the owner's tracked gym (WP High Voltage, Plaid
PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS, category Health) counted under bills. It must be a subscription;
LADWP (Rent & Utilities) stays a bill.
"""

from __future__ import annotations

from hermes_finance import insights


def _row(category, pfc, name):
    return {"date": "2026-09-01", "name": name, "merchant_name": name, "spend": 207.98, "category": category,
            "is_income": 0, "is_transfer": 0, "pfc_detailed": pfc, "logo_url": None, "account_name": "Card",
            "account_mask": "1618", "institution_name": "Wells Fargo", "icon_url": None, "transaction_id": "t1"}


def _tracked(name):
    return {"merchant_key": name.lower(), "merchant": name, "cadence": "monthly", "variable": 0, "usual_low": None,
            "usual_high": None, "anchor_date": "2026-09-01", "anchor_amount": 207.98, "anchor_transaction_id": "t1",
            "stopped_at": None}


def test_a_tracked_gym_is_a_subscription():
    rows = [_row("Health", "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", "Wp High Voltage")]
    item = insights._tracked_item(None, _tracked("Wp High Voltage"), rows, ["wp high voltage"],
                                  insights.date(2026, 9, 29))
    assert item["kind"] == "subscription"
    assert item["source"] == "tracked"


def test_a_tracked_utility_stays_a_bill():
    rows = [_row("Rent & Utilities", "RENT_AND_UTILITIES_WATER", "Los Angeles Department of Water and Power")]
    item = insights._tracked_item(None, _tracked("Los Angeles Department of Water and Power"), rows,
                                  ["los angeles department of water and power"], insights.date(2026, 9, 29))
    assert item["kind"] == "bill"
