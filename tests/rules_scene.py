"""TASK-24: a small synthetic DB for the rules manager (tests and docs/rules-api.md): 3 AI rules, 2 user rules, hand
filings that make 2 suggestions (one an AI override), and one user row a rule would otherwise change."""

from __future__ import annotations

from factory import basic, tx
from hermes_finance import classify

AI_RUN = "ai-20260901-000000"


def build(conn) -> dict:
    basic(conn)
    ids = {}
    ids["xai"] = [tx(conn, f"2026-09-0{d}", 20.0, "Xai", cat="Other Shopping") for d in (1, 2, 3)]
    ids["openrouter"] = [tx(conn, f"2026-08-1{d}", 10.0, None, name=f"OPENROUTER INC {d}", cat="Other Shopping")
                         for d in (1, 2)]
    ids["netflix"] = [tx(conn, f"2026-0{m}-05", 15.49, "Netflix", cat="Entertainment") for m in (7, 8, 9)]
    ids["bluebottle"] = [tx(conn, f"2026-09-1{d}", 6.5, "Blue Bottle", cat="Eating Out") for d in (1, 2, 3)]
    ids["trader"] = [tx(conn, f"2026-09-2{d}", 55.0, "Trader Joe's", cat="Other Shopping") for d in (1, 2)]
    ids["gym"] = [tx(conn, f"2026-0{m}-01", 40.0, "Equinox", cat="Other Shopping") for m in (7, 8, 9)]
    with conn:
        conn.execute("INSERT INTO ai_runs (run_id, created_at) VALUES (?, '2026-09-01T00:00:00Z')", (AI_RUN,))
    classify.add_rule(conn, "merchant_name", "Xai", "Subscriptions", created_from="ai")
    classify.add_rule(conn, "name", "OPENROUTER", "Subscriptions", "regex", created_from="ai")
    classify.add_rule(conn, "merchant_name", "Equinox", "Entertainment", created_from="ai")
    classify.add_rule(conn, "merchant_name", "Netflix", "Entertainment", created_from="user")
    classify.add_rule(conn, "merchant_name", "Blue Bottle", "Eating Out", created_from="user")
    classify.apply(conn)
    # hand filings: Trader Joe's -> Groceries twice (a suggestion); Equinox's AI rule overridden twice to Other Health
    # (a suggestion, an AI override: the AI had reviewed them as Entertainment); one Xai row kept as Other Shopping by hand
    ai_pick = classify.category_id(conn, "Entertainment")
    for t in ids["gym"][:2]:
        with conn:
            conn.execute("INSERT INTO ai_reviews (transaction_id, run_id, reviewed_at, verdict, category_id) "
                         "VALUES (?, ?, '2026-09-01T00:00:00Z', 'confirmed', ?)", (t, AI_RUN, ai_pick))
    for t in ids["trader"]:
        classify.set_category(conn, t, "Groceries")
    for t in ids["gym"][:2]:
        classify.set_category(conn, t, "Other Health")
    classify.set_category(conn, ids["xai"][0], "Other Shopping")
    with conn:  # fixed dates, so the doc and the order are stable
        for i, (rid,) in enumerate(conn.execute("SELECT id FROM rules ORDER BY id").fetchall()):
            conn.execute("UPDATE rules SET created_at = ? WHERE id = ?", (f"2026-09-0{i + 1}T12:00:00Z", rid))
    return ids
