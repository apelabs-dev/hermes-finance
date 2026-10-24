"""TASK-19 (Batch C): budget rollover, off by default. With it on, last month's unused cap (cap - spent, never below 0)
adds to this month's cap; left, rings, alerts and budget-status use that effective cap.

With every switch off (and nothing split, no goal, no Alpaca), every number the pages and the read commands give is
byte-identical to what the code before TASK-19 gives on the same database: checked by running that commit's code
(git show ee63f1f) in a child process on the full and nocaps gate DBs."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import factory as f
import gate_dbs
from hermes_finance import api, config, db, insights, reads, rollover
from hermes_finance.web import App

TODAY = date(2026, 9, 28)
ROOT = Path(__file__).resolve().parents[1]
BASE = "6f642ca"                       # TASK-18 (Batch A): the code TASK-19 was merged onto (TASK-20)
TASK17 = "ee63f1f"                     # TASK-17, the commit TASK-19 started from
LOCAL = {"Host": "127.0.0.1:8765"}
POST = {**LOCAL, "Content-Type": "application/json"}


@pytest.fixture
def month(conn):
    f.bank(conn, "item_a", "Chase")
    f.acct(conn, "acc_chk", current=1000.0)
    f.cap(conn, "Groceries", 400.0)
    f.cap(conn, "Eating Out", 150.0)
    f.tx(conn, "2026-08-10", 354.95, "Safeway", cat="Groceries")           # August: $45.05 left
    f.tx(conn, "2026-08-12", 200.00, "Nopa", cat="Eating Out")             # August: overspent by $50
    f.tx(conn, "2026-09-05", 262.79, "Safeway", cat="Groceries")
    f.tx(conn, "2026-09-06", 140.00, "Nopa", cat="Eating Out")
    return conn


def cat(conn, name, m="2026-09"):
    return next(c for c in insights.category_spend(conn, m, TODAY) if c["category"] == name)


def test_off_by_default_and_on_adds_last_months_unused_cap(month):
    conn = month
    g = cat(conn, "Groceries")
    assert g["cap"] == 400.0 and "base_cap" not in g and "rollover" not in g
    rollover.set_enabled(conn, "Groceries", True)
    g = cat(conn, "Groceries")
    assert (g["cap"], g["base_cap"], g["rollover"]) == (445.05, 400.0, {"amount": 45.05, "from": "2026-08"})
    assert g["left"] == round(445.05 - 262.79, 2) and g["pct"] == round(262.79 / 445.05 * 100, 2) and g["status"] == "ok"
    b = insights.budgets(conn, "2026-09", TODAY)
    assert b["total_cap"] == 595.05 and b["left"] == round(595.05 - 262.79 - 140.0, 2)


def test_overspending_never_rolls_negative(month):
    conn = month
    rollover.set_enabled(conn, "Eating Out", True)
    e = cat(conn, "Eating Out")
    assert (e["cap"], e["base_cap"], e["rollover"]["amount"]) == (150.0, 150.0, 0.0)
    assert e["status"] == "warn" and e["left"] == 10.0


def test_the_effective_cap_reaches_alerts_and_budget_status(month):
    conn = month
    f.tx(conn, "2026-09-20", 90.0, "Safeway", cat="Groceries")             # 352.79: over 400*80% but under 445.05*80%
    assert {a["category"] for a in insights.alerts(conn, "2026-09", TODAY) if a["kind"] == "cap"} == {"Eating Out", "Groceries"}
    rollover.set_enabled(conn, "Groceries", True)
    assert [a["category"] for a in insights.alerts(conn, "2026-09", TODAY) if a["kind"] == "cap"] == ["Eating Out"]
    bs = reads.budget_status(conn, "2026-09", TODAY)
    g = next(c for c in bs["caps"] if c["category"] == "Groceries")
    assert (g["cap"], g["base_cap"], g["rollover"], g["status"]) == (445.05, 400.0, {"amount": 45.05, "from": "2026-08"}, "ok")
    assert g["left"] == round(445.05 - 352.79, 2)
    e = next(c for c in bs["caps"] if c["category"] == "Eating Out")
    assert "rollover" not in e and "base_cap" not in e


def test_only_last_months_own_cap_rolls_it_doesnt_snowball(month):
    conn = month
    f.tx(conn, "2026-07-10", 100.0, "Safeway", cat="Groceries")            # July left $300: never reaches September
    rollover.set_enabled(conn, "Groceries", True)
    assert cat(conn, "Groceries")["rollover"]["amount"] == 45.05
    assert cat(conn, "Groceries", "2026-08")["rollover"] == {"amount": 300.0, "from": "2026-07"}


def test_on_then_off_leaves_every_number_as_it_was(month):
    conn = month
    snap = lambda: json.dumps([api.budgets_page(conn, {}, TODAY), api.overview(conn, {}, TODAY),  # noqa: E731
                               reads.budget_status(conn, "2026-09", TODAY)], sort_keys=True, default=str)
    before = snap()
    rollover.set_enabled(conn, "Groceries", True)
    assert snap() != before
    rollover.set_enabled(conn, "Groceries", False)
    assert snap() == before and conn.execute("SELECT COUNT(*) FROM category_rollover").fetchone()[0] == 0


@pytest.mark.parametrize("category, on, message", [("Income", True, "not a spending category"), ("Food", True, "unknown category"),
                                                   ("Other Health", True, "has no budget"),
                                                   ("", True, "category is required"),
                                                   ("Groceries", "yes", "true or false")])
def test_bad_switches_are_refused(month, category, on, message):
    with pytest.raises(rollover.RolloverError, match=message):
        rollover.set_enabled(month, category, on)


def test_the_route(month, client):
    app = App(config.db_path(), lambda: client)
    app.today = lambda: TODAY
    status, _, raw = app.handle("POST", "/api/budgets/rollover", POST, json.dumps({"category": "groceries", "enabled": True}).encode())
    assert status == 200 and json.loads(raw) == {"category": "Groceries", "enabled": True}
    status, _, raw = app.handle("GET", "/api/budgets?month=2026-09", LOCAL, b"")
    g = next(c for c in json.loads(raw)["categories"] if c["category"] == "Groceries")
    assert g["rollover"] == {"amount": 45.05, "from": "2026-08"} and g["cap"] == 445.05
    body = json.dumps({"category": "Other Health", "enabled": True}).encode()
    assert app.handle("POST", "/api/budgets/rollover", POST, body)[0] == 400


# --- byte-identical with every switch off: the code before TASK-19, on the same databases -----------------------------

DUMP = r"""
import json, sys
from datetime import date
from hermes_finance import api, db, reads
path, today = sys.argv[1], date.fromisoformat(sys.argv[2])
conn = db.connect(path)
m = today.isoformat()[:7]
out = {"overview": api.overview(conn, {}, today), "budgets": api.budgets_page(conn, {}, today),
       "budgets_prev": api.budgets_page(conn, {"month": "2026-08"}, today), "recurring": api.recurring_page(conn, {}, today),
       "accounts": api.accounts(conn, {}, today), "investments": api.investments_page(conn, {}, today),
       "debt": api.debt_page(conn, {}, today), "counts": api.counts(conn, {}, today),
       "transactions": api.transactions(conn, {"month": m, "limit": "500"}, today),
       "budget_status": reads.budget_status(conn, m, today),
       "spend": reads.spend(conn, reads.parse_filters(conn, today, month=m, spending=True), today, "category"),
       "spend_merchant": reads.spend(conn, reads.parse_filters(conn, today, since="90d", spending=True), today, "merchant"),
       "cashflow": reads.cashflow(conn, today, 12), "networth": reads.networth(conn, today, history=True)}
print(json.dumps(out, sort_keys=True, default=str))
"""
NEW_KEYS = {"overview": ("goals",), "accounts": ("api_items",)}  # TASK-19's additions, read by its pages only


def old_code(tmp: Path, base: str = BASE) -> Path:
    """The source tree at `base`, from this repository's own history. A clone without that commit (the public repo is
    one squashed commit; a shallow clone) can't make this comparison, and the test says so by skipping."""
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{base}^{{commit}}"], capture_output=True).returncode:
        pytest.skip(f"needs commit {base} (the code before this change) in the clone's history: the public repository "
                    "is one squashed commit, so this comparison runs in the development repository only")
    src = tmp / f"base-src-{base}"
    files = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", base, "src/hermes_finance"],
                           capture_output=True, text=True, check=True).stdout.split()
    for name in files:
        if not name.endswith(".py"):
            continue
        out = src / Path(name).relative_to("src")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:{name}"], capture_output=True, check=True).stdout)
    return src


# v16 (polish P0): the categories split off Rent & Utilities, Eating Out and Other Shopping keep their parents' policy
# (Internet & Phone is a fixed bill and never suggested; Coffee, Alcohol & Bars, Clothing, Electronics and Home Goods are
# never recurring). The old code runs with the same sets, so every number must still be byte-identical.
V16_POLICY = f"""from hermes_finance import insights
insights.FIXED_CATEGORIES = {insights.FIXED_CATEGORIES!r}
insights.RECURRING_EXCLUDED = {insights.RECURRING_EXCLUDED!r}
insights.NO_SUGGEST = {insights.NO_SUGGEST!r}
insights.NO_SUGGEST_REASONS = {insights.NO_SUGGEST_REASONS!r}
"""


def dump(path: Path, pythonpath: Path) -> dict:
    code = DUMP if pythonpath == ROOT / "src" else V16_POLICY + DUMP
    r = subprocess.run([sys.executable, "-c", code, str(path), TODAY.isoformat()], capture_output=True, text=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(pythonpath), "HERMES_ENV_FILE": str(path.parent / "none.env"),
                            "HERMES_CHROME_DEVTOOLS": "off", "HERMES_ALPACA": "off",
                            "HERMES_ALPACA_KEY_FILE": str(path.parent / "none-alpaca.env"), "FINANCE_DATA_DIR": str(path.parent),
                            "TZ": "America/Los_Angeles"})
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


# TASK-21's additions: each recurring item's new fields (the schedule's words, a changing amount's estimate and range,
# detected or tracked, last paid from, outlier), a bill still due's `variable` and a transaction row's `bill_outlier`. Set
# aside by name where they are; every value that was there before must still be byte-identical.
TASK21_ITEM_KEYS = ("cadence_label", "variable", "estimate", "amount_label", "usual", "source", "last_amount", "last_account",
                    "outlier")


def _no_task21(d):
    if isinstance(d, list):
        return [_no_task21(x) for x in d]
    if not isinstance(d, dict):
        return d
    item = "next_expected" in d and "merchant" in d   # a recurring item, or a bill still due
    return {k: _no_task21(v) for k, v in d.items() if not (item and k in TASK21_ITEM_KEYS)}


# TASK-22's addition: the accounts payload's data_age ("synced 42 min ago"), the header's words for how fresh the data is
TASK22_KEYS = {"accounts": ("data_age",)}


def strip_new(d: dict) -> dict:
    out = {k: ({kk: vv for kk, vv in v.items() if kk not in NEW_KEYS.get(k, ()) + TASK22_KEYS.get(k, ())} if isinstance(v, dict) else v)
           for k, v in d.items()}
    # polish x-amber: each row's `uncategorized` (the ?uncategorized=1 rule for one row) is new
    if isinstance(out.get("overview"), dict) and isinstance(out["overview"].get("recent"), list):
        out["overview"] = {**out["overview"], "recent": [{k: v for k, v in r.items() if k != "uncategorized"}
                                                         for r in out["overview"]["recent"]]}
    # polish P1: the page's `days`, `sort` and `sum` are new. Time windows add `period`,
    # `coverage_month` and per-month totals. Set aside only these additions; every pre-existing
    # value still has to match. tests/test_transaction_windows.py checks the new metadata/totals.
    out["transactions"] = {k: v for k, v in out["transactions"].items()
                           if k not in ("days", "sort", "sum", "period", "coverage_month", "months")}
    out["transactions"] = {**out["transactions"], "rows": [{k: v for k, v in r.items() if k not in ("split", "note", "tags", "bill_outlier", "uncategorized")}
                                                           for r in out["transactions"]["rows"]]}
    # polish P2a: Budgets' `groups` (per-group sums of the same rows) and each category's `group` / `starter` / `new` are
    # new; set aside. Every cap, spend, left, pct, status and suggested is still compared
    blocks = [out.get(k) for k in ("budgets", "budgets_prev") if isinstance(out.get(k), dict)]
    for b in blocks + [b.get("budgets") for b in blocks]:
        if isinstance(b, dict):
            b.pop("groups", None)
            if isinstance(b.get("categories"), list):
                b["categories"] = [{k: v for k, v in c.items() if k not in ("group", "starter", "new")} for c in b["categories"]]
    # polish P2e: group budgets. With none set, `group_budgets` (insights.budgets, so the Overview's and Budgets' budgets
    # blocks) and budget-status's `groups` are EMPTY, set aside only then; Budgets' `move` (the one-time review's
    # proposal, read by the page only) is new, set aside. Every cap, total, left and status is still compared
    for b in blocks:
        b.pop("move", None)
    for b in [b.get("budgets") for b in blocks] + [(out.get("overview") or {}).get("budgets")]:
        if isinstance(b, dict) and b.get("group_budgets") == []:
            del b["group_budgets"]
    if isinstance(out.get("budget_status"), dict) and out["budget_status"].get("groups") == []:
        del out["budget_status"]["groups"]
    # TASK-22: net worth carries `in_transit`, the card payments counted as paid. Set aside only when it is EMPTY: a test DB
    # with none must give every old number exactly; one with a payment in transit changes the net and must still fail here
    for blk in (out.get("networth"), (out.get("accounts") or {}).get("net_worth")):
        if isinstance(blk, dict) and blk.get("in_transit") == []:
            del blk["in_transit"]
    # polish acctchange (26d0185): each net-worth group carries its `month_start` / `month_change`, set aside as new
    for blk, key in ((out.get("networth"), "groups"), ((out.get("accounts") or {}).get("net_worth"), "breakdown")):
        if isinstance(blk, dict) and isinstance(blk.get(key), list):
            blk[key] = [{k: v for k, v in g.items() if k not in ("month_start", "month_change")} if isinstance(g, dict) else g
                        for g in blk[key]]
    # TASK-22f: the health count's Plaid part (`banks`), and the Loans & debt tile's `reported` / `in_transit`, set aside
    # the same way: only with no payment in transit (then `reported` is the old total, and must be)
    ov = out.get("overview")
    if isinstance(ov, dict):
        ov.pop("spend_groups", None)   # polish P2b: Where it went's per-group totals of the same spending, new; set aside
        if isinstance(ov.get("connections"), dict):
            ov["connections"] = {k: v for k, v in ov["connections"].items() if k != "banks"}
        debt = ov.get("debt")
        if isinstance(debt, dict) and debt.get("in_transit") == [] and debt.get("reported") == debt.get("total"):
            ov["debt"] = {k: v for k, v in debt.items() if k not in ("reported", "in_transit")}
    # hotfix 2026-10-05 (da03a79): investments carry `priced_total`, the denominator of each holding's "% of portfolio"
    # (an account at max(balance, holdings + cash)). Set aside only when it equals the old `total`: then every share is
    # the old one and must still match; a DB where a balance lags its holdings changes the shares and must still fail
    for inv in (out.get("investments"), (ov or {}).get("investments") if isinstance(ov, dict) else None):
        if isinstance(inv, dict) and "priced_total" in inv and inv["priced_total"] == inv.get("total"):
            del inv["priced_total"]
    # hotfix 2026-10-07: the pace projection lists one-time purchases (insights._one_offs), counted once instead of paced.
    # Set aside only when there are NONE: then every pace number is the old one and must still match
    pace = (ov or {}).get("pace") if isinstance(ov, dict) else None
    if isinstance(pace, dict) and pace.get("one_offs") == [] and pace.get("one_off_total") == 0.0:
        del pace["one_offs"], pace["one_off_total"]
    return _no_task21(out)


# hotfix 2026-09-29 (deb0246): a month-based bill billed on the same day of the month now comes back on that day instead of
# last + the median gap in days (the gym, Aug 1 + Sep 1, was put on Oct 2). That moves those items' next_expected (and so
# days_until, and their place in date-sorted lists) and nothing else. Set aside exactly that: every such item must land on
# its last charge's day of the month (the month's last day when shorter), and every other value must still be identical.
HOTFIX_DATE_KEYS = ("next_expected", "days_until")


def _same_day_of_month(item: dict) -> bool:
    import calendar
    nxt, last = date.fromisoformat(item["next_expected"]), date.fromisoformat(item["last_date"])
    return nxt.day == min(last.day, calendar.monthrange(nxt.year, nxt.month)[1])


def hotfix_dates(new, old):
    """(new, old) with the hotfix's date moves set aside; asserts each moved item is on its day of the month."""
    def norm(d, other):
        if isinstance(d, list):
            items = d
            if d and all(isinstance(x, dict) and "next_expected" in x and "key" in x for x in d):
                items = sorted(d, key=lambda x: (x["key"], x.get("last_date") or ""))
                others = {(x["key"], x.get("last_date")): x for x in (other or []) if isinstance(x, dict)}
                return [norm(x, others.get((x["key"], x.get("last_date")))) for x in items]
            return [norm(x, None) for x in items]
        if isinstance(d, dict):
            item = "next_expected" in d and "merchant" in d
            if item and other is not None and other.get("next_expected") != d.get("next_expected") and d is not None:
                assert _same_day_of_month(d) or _same_day_of_month(other), (d["merchant"], d["next_expected"], other["next_expected"])
            return {k: norm(v, (other or {}).get(k) if isinstance(other, dict) else None)
                    for k, v in d.items() if not (item and k in HOTFIX_DATE_KEYS)}
        return d
    return norm(new, old), norm(old, new)


def unroll(new: dict, old: dict) -> None:
    """Group roll-up (2026-10-22): a group with caps but no group budget counts its uncapped categories' spending too
    (`group_rest`). With none, `group_rest` is empty and set aside. With some, the totals move by EXACTLY the rest:
    spent / due grow by its spend / due, left shrinks by both, under - over_amount is still left, and budget-status's
    uncapped loses exactly those categories. Checked here, then the old values are put back, so every other number
    must still be byte-identical."""
    c = lambda x: round(x * 100)
    for key in ("budgets", "budgets_prev", "overview"):
        n, o = (new.get(key) or {}).get("budgets"), (old.get(key) or {}).get("budgets")
        if not isinstance(n, dict) or "group_rest" not in n:
            continue
        rest = n.pop("group_rest")
        if not rest:
            continue
        spend, due = sum(r["spend"] for r in rest), sum(r["due"] for r in rest)
        assert c(n["total_spend"]) == c(o["total_spend"] + spend), key
        assert c(n["total_due"]) == c(o["total_due"] + due), key
        assert c(n["left"]) == c(o["left"] - spend - due), key
        assert c(n["under"] - n["over_amount"]) == c(n["left"]), key
        assert n["over"] >= o["over"] and n["count"] == o["count"], key
        for k in ("total_spend", "total_due", "left", "under", "over_amount", "over", "per_day", "bills_due"):
            n[k] = o[k]
        if key == "overview" and isinstance(new.get("counts"), dict):
            new["counts"]["over"] = old["counts"]["over"]
    n, o = new.get("budget_status"), old.get("budget_status")
    if isinstance(n, dict) and "group_rest" in n:
        rest = n.pop("group_rest")
        if rest:
            spend, due = sum(r["spent"] for r in rest), sum(r["due"] for r in rest)
            t, ot = n["totals"], o["totals"]
            assert c(t["spent"]) == c(ot["spent"] + spend) and c(t["due"]) == c(ot["due"] + due)
            assert c(t["left"]) == c(ot["left"] - spend - due) and c(t["under"] - t["over_amount"]) == c(t["left"])
            for k in ("spent", "due", "left", "under", "over_amount", "over", "per_day", "bills_due"):
                t[k] = ot[k]
            gone = {x for r in rest for x in r["categories"]}
            assert n["uncapped"]["categories"] == [x for x in o["uncapped"]["categories"] if x["category"] not in gone]
            assert c(n["uncapped"]["spent"]) == c(o["uncapped"]["spent"] - spend)
            n["uncapped"] = o["uncapped"]


@pytest.mark.parametrize("kind", ["full", "nocaps"])
def test_every_number_is_byte_identical_to_the_code_before_task19(data_dir, tmp_path, kind):
    path = tmp_path / kind / "finance.db"
    path.parent.mkdir()
    gate_dbs.build(kind, path, TODAY)
    new = dump(path, ROOT / "src")        # first: the one-time Custom selection save happens here, not in between
    old = dump(path, old_code(tmp_path))
    a, b = within_day(*hotfix_dates(strip_new(new), old_money(old)))
    unroll(a, b)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    # and against TASK-17 too, where the only change is TASK-18's own: Carfax and Redfin start off on a new install (a
    # gate DB is one), so the valuation switch blocks are set aside there and every other value must match
    task17 = dump(path, old_code(tmp_path, TASK17))
    a, b = within_day(*hotfix_dates(no_valuation(strip_new(new)), no_valuation(old_money(task17))))
    unroll(a, b)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def within_day(a, b):
    """polish P1 (t_9335baaa) replaced the random transaction_id tiebreak with a stable order WITHIN a day. Every row and
    every date in sequence must still match: Transactions rows are compared as each day's set; the Overview's last 10
    must have the same dates in order and the same rows on every day but the oldest, where the cut-off falls."""
    def by_day(rows):
        return sorted(rows, key=lambda r: (r.get("date") or "", r.get("transaction_id") or ""))
    for d in (a, b):
        d["transactions"]["rows"] = by_day(d["transactions"]["rows"])
    ra, rb = a["overview"]["recent"], b["overview"]["recent"]
    assert [r.get("date") for r in ra] == [r.get("date") for r in rb]
    if ra:
        cut = min(r.get("date") or "" for r in ra)
        for d, rs in ((a, ra), (b, rb)):
            d["overview"]["recent"] = by_day([r for r in rs if (r.get("date") or "") != cut])
    return a, b


def old_money(d):
    """polish moneytext (f5a17ca): a whole-dollar amount in alert text drops its cents ("$225", was "$225.00"). Only the
    text form changes: the old code's alert messages are rewritten the same way, every number in them still compared."""
    ov = d.get("overview") if isinstance(d, dict) else None
    if isinstance(ov, dict) and isinstance(ov.get("alerts"), list):
        ov["alerts"] = [{**a, "message": re.sub(r"(\$[\d,]+)\.00\b", r"\1", a["message"])} if isinstance(a, dict) and isinstance(a.get("message"), str) else a
                        for a in ov["alerts"]]
    return d


def no_valuation(d):
    if isinstance(d, dict):
        return {k: no_valuation(v) for k, v in d.items() if k != "valuation"}
    return [no_valuation(x) for x in d] if isinstance(d, list) else d
    assert new["overview"]["goals"] == {"count": 0, "monthly": None, "top": None} and new["accounts"]["api_items"] == []
    assert all(r["split"] is None and r["note"] is None and r["tags"] == [] for r in new["transactions"]["rows"])
    # a switch on changes exactly its category (and the totals it's in); off again, byte-identical again
    conn = db.connect(path)
    capped = [c["category"] for c in new["budgets"]["categories"] if c["cap"] is not None]
    if capped:
        rollover.set_enabled(conn, capped[0], True)
        on = dump(path, ROOT / "src")
        diff = [c["category"] for c, o in zip(on["budgets"]["categories"], new["budgets"]["categories"]) if c != o]
        assert diff in ([capped[0]], []) and "rollover" in next(c for c in on["budgets"]["categories"] if c["category"] == capped[0])
        rollover.set_enabled(conn, capped[0], False)
        assert json.dumps(dump(path, ROOT / "src"), sort_keys=True) == json.dumps(new, sort_keys=True)
    conn.close()
