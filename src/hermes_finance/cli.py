"""`finance` command line."""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from . import ai, api, classify, config, db, insights, logos, manual, photos, reads, redact, report, syncnow, usage, valuation
from .plaid import PlaidClient
from .sync import sync_all

log = logging.getLogger("hermes_finance")

EXPORT_COLUMNS = [
    "date", "name", "merchant_name", "amount", "spend", "iso_currency", "category", "category_source",
    "pending", "account_name", "account_mask", "institution_name", "pfc_primary", "pfc_detailed",
    "transaction_id",
]


def emit(text: str = "", stream=None) -> None:
    print(redact.scrub(text), file=stream or sys.stdout)


def emit_json(payload) -> None:
    emit(json.dumps(payload, indent=2, default=str))


def _client() -> PlaidClient:
    return PlaidClient(config.load_settings())


def _load_known_tokens(conn: sqlite3.Connection) -> None:
    for (tok,) in conn.execute("SELECT access_token FROM items"):
        redact.register(tok)


# --- commands ----------------------------------------------------------------------------------

def serve_access(args) -> tuple[list[str], list[str]]:
    """(public URLs, allowed users): the repeatable flags merged with their comma-separated env vars."""
    def merge(flags, env):
        return list(dict.fromkeys([*(flags or []), *config.env_list(env)]))
    return merge(args.public_url, "FINANCE_PUBLIC_URLS"), merge(args.allow_user, "FINANCE_ALLOWED_USERS")


def _serve_refused(problems: list[str]) -> int:
    for msg in problems:
        emit(f"error: {msg}", sys.stderr)
        log.error("finance serve refused to start: %s", msg)
    return 2


def cmd_serve(args) -> int:
    from . import access, auth
    from .web import App, make_server, parse_public_url

    public_urls, allowed_users = serve_access(args)
    try:
        for url in public_urls:
            parse_public_url(url)
    except ValueError as e:
        emit(f"error: {e}", sys.stderr)
        return 2
    path = config.db_path()
    if not access.is_loopback(args.host) and not path.exists():  # nothing is created for a start that can't happen
        acc = access.Access("local", access.DEFAULT_HEADER, set(), public_urls)
        return _serve_refused(access.startup_problems(acc, args.host, False))
    with db.connect(path) as conn:
        chosen = access.migrate(conn, public_urls, allowed_users)
        acc = access.load(conn, public_urls, allowed_users)
        login_ready = auth.configured(conn)
        problems = access.startup_problems(acc, args.host, login_ready)
        if problems:
            return _serve_refused(problems)
        _load_known_tokens(conn)
    try:
        _client()  # the keys are checked now; without any, the page opens on first-run setup
        keys = True
    except config.ConfigError:
        keys = False
    app = App(path, lambda: _client(), args.host, args.port, public_urls, allowed_users)
    server = make_server(app, args.host, args.port)
    emit(f"hermes-finance serving on http://{args.host}:{app.port}/  (Ctrl-C to stop)")
    if chosen:
        emit(f"access: set to {access.MODE_NAMES[chosen]} (the first start of this version; change it in Settings)")
    if acc.mode == "proxy" and acc.public_origins:
        emit(f"behind your proxy login ({acc.header}): {', '.join(sorted(o + '/' for o in acc.public_origins))}  "
             f"allowed: {', '.join(access.mask(u) for u in sorted(acc.identities))}")
    elif acc.mode == "builtin":
        emit("built-in login: every page needs a sign-in" + ("" if login_ready else
             " (not set up yet: open the page on this computer to set it up)"))
    if not keys:
        emit(f"no Plaid keys yet: open http://127.0.0.1:{app.port}/ to set up (they're saved in "
             f"{config.secrets_file_path()})")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_sync(args) -> int:
    client = _client()
    conn = db.connect()
    _load_known_tokens(conn)
    if args.item and not conn.execute("SELECT 1 FROM items WHERE item_id=?", (args.item,)).fetchone():
        emit(f"unknown item {args.item}", sys.stderr)
        return 2
    before = syncnow.balances(conn)
    results = sync_all(conn, client, args.item)
    if not results:
        emit("No active Items to sync. Connect a bank with `finance serve`.")
        return 0
    if any(r.ok for r in results):  # TASK-22e: a bank still drawn with initials is looked up again, once a day
        try:
            logos.refresh_institutions(conn, client, missing_only=True)
        except Exception as e:  # never fails the sync
            log.warning("bank logo retry failed: %s", e)
    fail = syncnow.failure(results)
    if fail and fail["unreachable"]:   # TASK-22c: as the page's toast: one line, not an error per bank
        emit(syncnow.UNREACHABLE)
        return 1
    for r in results:
        emit(r.summary())
    ok = sum(r.ok for r in results)
    emit(f"synced {ok}/{len(results)} item(s)")
    c = syncnow.changes(before, syncnow.balances(conn), results)
    if c["text"]:   # (none when no bank synced)
        emit(syncnow.summary_line(c))   # TASK-22: as the page's toast
    if fail:
        emit(" ".join(fail["lines"]))
    return 0 if ok else 1


def cmd_status(args) -> int:
    with db.connect() as conn:
        _load_known_tokens(conn)
        s = report.status(conn)
    if args.json:
        emit_json(s)
    else:
        emit(report.status_text(s))
    return 0


def cmd_query(args) -> int:
    try:
        conn = db.connect_readonly()
        cur = conn.execute(args.sql)
        rows = cur.fetchall()
    except (sqlite3.Error, FileNotFoundError) as e:
        hint = " (finance query is read-only: SELECT/introspection only)" if "not authorized" in str(e) else ""
        emit(f"error: {e}{hint}", sys.stderr)
        return 1
    columns = [d[0] for d in cur.description] if cur.description else []
    if args.json:
        emit_json([dict(zip(columns, r)) for r in rows])
    elif columns:
        emit(report.table(columns, rows))
    else:
        emit("(no result set)")
    return 0


def cmd_export(args) -> int:
    try:
        date.fromisoformat(args.since)
    except ValueError:
        emit("error: --since must be YYYY-MM-DD", sys.stderr)
        return 2
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {', '.join(EXPORT_COLUMNS)} FROM v_transactions WHERE date >= ? "
            "ORDER BY date, transaction_id", (args.since,))]
    if not args.csv:
        emit_json(rows)
        return 0
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    if args.csv == "-":
        sys.stdout.write(buf.getvalue())
    else:
        with open(args.csv, "w", newline="") as f:
            f.write(buf.getvalue())
        emit(f"wrote {len(rows)} transactions to {args.csv}")
    return 0


def cmd_classify_apply(args) -> int:
    with db.connect() as conn:
        counts = classify.apply(conn)
    emit(f"classify-apply: {counts['rule']} set by rules, {counts['plaid']} set to Plaid defaults, "
         f"{counts['unchanged']} unchanged (ai/user rows are never touched)")
    return 0


def cmd_unclassified(args) -> int:
    with db.connect() as conn:
        rows = classify.unclassified(conn, args.limit, args.since)
        categories = [dict(r) for r in conn.execute(
            "SELECT name, is_income, is_transfer, monthly_cap FROM categories ORDER BY name")]
    if args.json:
        emit_json({"count": len(rows), "categories": categories, "transactions": rows})
    else:
        emit(report.table(["transaction_id", "date", "amount", "name", "merchant_name", "suggested_category",
                           "pfc_confidence"],
                          [[report.money(r[c]) if c == "amount" else r[c]
                            for c in ("transaction_id", "date", "amount", "name", "merchant_name",
                                      "suggested_category", "pfc_confidence")] for r in rows], 40))
    return 0


def cmd_set_category(args) -> int:
    with db.connect() as conn:
        try:
            outcome = classify.set_category(conn, args.txn_id, args.category, args.source, args.confidence, args.note)
            if outcome == "kept-user":
                emit(f"{args.txn_id}: kept existing user classification (source={args.source} never overrides user)")
                return 1
            if outcome == "kept-user-rule":
                emit(f"{args.txn_id}: kept its user rule's category (source={args.source} never overrides a user rule)")
                return 1
            emit(f"{args.txn_id}: {args.category} (source={args.source})")
            if args.make_rule:
                saved = classify.rule_from_transaction(conn, args.txn_id, args.category, args.source)
                if saved.kept_user_rule:
                    emit(f"rule #{saved.id} is a user rule; kept it unchanged")
                    return 1
                counts = classify.apply(conn)
                emit(f"rule #{saved.id} saved; {_refiled(saved)}classify-apply: {counts['rule']} set by rules")
        except classify.ClassifyError as e:
            emit(f"error: {e}", sys.stderr)
            return 2
    return 0


def _refiled(saved: classify.RuleSaved) -> str:
    return f"{saved.refiled} re-filed to it; " if saved.refiled else ""


def cmd_add_rule(args) -> int:
    with db.connect() as conn:
        try:
            saved = classify.add_rule(conn, args.field, args.pattern, args.category,
                                      "regex" if args.regex else "exact", args.priority, args.created_from)
        except classify.ClassifyError as e:
            emit(f"error: {e}", sys.stderr)
            return 2
        if saved.kept_user_rule:
            emit(f"rule #{saved.id} is a user rule; kept it unchanged (only --created-from user can change it)")
            return 1
        counts = classify.apply(conn)
    emit(f"rule #{saved.id} saved; {_refiled(saved)}classify-apply: {counts['rule']} set by rules")
    return 0


def cmd_ai_queue(args) -> int:
    if args.max_groups < 1:
        emit("error: --max-groups must be at least 1", sys.stderr)
        return 2
    with db.connect() as conn:
        payload = ai.queue(conn, args.max_groups)
    emit(json.dumps(payload, separators=(",", ":")))
    return 0


def cmd_ai_apply(args) -> int:
    try:
        with open(args.file, encoding="utf-8") as f:
            payload = json.load(f)
    except OSError as e:
        emit(f"error: can't read {args.file}: {e.strerror}", sys.stderr)
        return 2
    except ValueError as e:
        emit(f"error: {args.file} is not valid JSON: {e}", sys.stderr)
        return 2
    with db.connect() as conn:
        try:
            summary = ai.apply_decisions(conn, payload, args.dry_run)
        except ai.AiError as e:
            emit(f"error: {e}", sys.stderr)
            return 2
        except Exception as e:
            log.debug("ai-apply failed", exc_info=True)
            emit(f"error: ai-apply failed and was rolled back, nothing changed ({type(e).__name__}: {e})", sys.stderr)
            return 1
    emit_json(summary)
    return 0


# --- manual accounts and loans ------------------------------------------------------------------

MANUAL_FLAGS = (  # (flag, field, type, help)
    ("--name", "name", str, "nickname (vehicles: defaults to 'YEAR MAKE MODEL')"),
    ("--institution", "institution", str, "bank / card company / institution / loan provider"),
    ("--subtype", "subtype", str, "bank: checking|savings|money_market|cd; investment: brokerage|401k|roth_ira|"
                                  "traditional_ira|hsa|crypto|other; loan: auto|student|personal|mortgage|bnpl|other; "
                                  "free text for other_asset / other_liability"),
    ("--balance", "balance", float, "current balance / value / amount remaining (always >= 0)"),
    ("--credit-limit", "credit_limit", float, "credit cards"),
    ("--interest-rate", "interest_rate", float, "APR in percent (loans, credit cards)"),
    ("--original-amount", "original_amount", float, "loans"),
    ("--monthly-payment", "monthly_payment", float, "loans"),
    ("--match-pattern", "match_pattern", str, "loans and credit cards: text in the bank transaction's name, e.g. "
                                              "'ACME AUTO LOAN'"),
    ("--vehicle-make", "vehicle_make", str, "vehicles"),
    ("--vehicle-model", "vehicle_model", str, "vehicles"),
    ("--vehicle-year", "vehicle_year", int, "vehicles"),
    ("--vin", "vin", str, "vehicles: 17-character VIN (check digit verified; decoded by NHTSA to fill year/make/model/"
                          "trim left out; with --mileage and --zip, Carfax values it)"),
    ("--vehicle-trim", "vehicle_trim", str, "vehicles"),
    ("--mileage", "mileage", int, "vehicles: odometer miles (for the estimate)"),
    ("--zip", "zip_code", str, "vehicles: ZIP code where the car is (for the estimate)"),
    ("--condition", "vehicle_condition", str, "vehicles: fair | good | excellent, what Carfax prices it in "
                                              "(carfax.com/value's own list; default good)"),
    ("--address", "address", str, "property: street, city, state and ZIP (Redfin values it)"),
    ("--loan", "linked_loan_id", int, "vehicles / property: the id of the manual loan it's financed by (its equity)"),
    ("--as-of", "balance_as_of", str, "YYYY-MM-DD the balance was true (default: today)"),
)
CLEARABLE = ("institution", "subtype", "credit_limit", "interest_rate", "original_amount", "monthly_payment",
             "match_pattern", "vehicle_make", "vehicle_model", "vehicle_year", "vin", "vehicle_trim", "mileage",
             "zip_code", "address", "vehicle_condition", "linked_loan_id")


def _manual_fields(args) -> dict:
    fields = {field: getattr(args, field) for _, field, _, _ in MANUAL_FLAGS if getattr(args, field) is not None}
    for field in getattr(args, "clear", None) or []:
        if field in fields:
            raise manual.ManualError(f"--clear {field} and a new value for it can't be combined", field)
        fields[field] = None
    return fields


def _manual_line(m: dict) -> str:
    extra = []
    if m["kind"] == "loan":
        extra.append(f"{m['paid_pct']:.0f}% paid off" if m["paid_pct"] is not None else "")
        po = m["payoff"]
        extra.append(f"payoff {po['payoff_month']} ({po['payments_left']} payments, {po['method']})"
                     if po["payoff_month"] else f"payoff: {po['method']}")
        if m["match_pattern"]:
            extra.append(f"matches {m['match_pattern']!r}")
    if m["kind"] == "credit_card" and m.get("approx"):
        extra.append(f"approx: last set {report.money(m['entered_balance'])} on {m['balance_as_of']}, minus "
                     f"{report.money(m['matched_total'])} in {m['matched_count']} matched payment(s)")
        if m["match_pattern"]:
            extra.append(f"matches {m['match_pattern']!r}")
        if m.get("nudge"):
            extra.append("update it from a statement")
    if m.get("valuation"):
        extra.append(_valuation_text(m))
    if m["stale"]:
        extra.append(f"STALE ({m['age_days']} days old)")
    if m["archived_at"]:
        extra.append("archived")
    sub = f" {m['subtype_label']}" if m.get("subtype_label") else ""
    return (f"#{m['id']:<3} {m['kind']:<15} {m['name']}{sub}  {report.money(m['signed'])}  as of {m['balance_as_of']}"
            + "".join(f"  · {x}" for x in extra if x))


def _valuation_text(m: dict) -> str:
    """"Trade-in · Carfax · 2026-09-26" / "Your value (Carfax trade-in $X on DATE)" + what's missing, and the quiet
    note (Carfax's higher odometer)."""
    v = m["valuation"]
    est = v["estimate"]
    if v["source"] == "estimate":
        text = (f"{est['basis_label']} · {est['source_label']} · {est['date']}" if est
                else f"no estimate yet ({v['last_error']['message'] if v['last_error'] else v['refresh_note']})")
    else:
        text = "Your value" + (f" ({est['source_label']} {reads.BASIS_WORDS[est['basis']]} {report.money(est['value'])} "
                               f"on {est['date']})" if est else "")
    if v["mileage_nudge"]:
        text += "; update mileage for a better estimate"
    if v.get("odometer_hint"):
        text += f"; {v['odometer_hint']['message']} (finance manual edit {m['id']} --mileage N)"
    return text


def cmd_manual_list(args) -> int:
    with db.connect() as conn:
        s = manual.summary(conn, date.today(), include_archived=args.archived)
    if args.json:
        emit_json(s)
        return 0
    if not s["accounts"]:
        emit("No manual accounts. Add one with `finance manual add --kind ...` or the dashboard's Add account.")
        return 0
    for m in s["accounts"]:
        emit(_manual_line(m))
    emit(f"manual assets {report.money(s['assets'])}, liabilities {report.money(s['debts'])}, net {report.money(s['net'])}")
    return 0


def _manual_write(fn, args, *call_args) -> int:
    with db.connect() as conn:
        try:
            m = fn(conn, *call_args, date.today())
        except manual.ManualError as e:
            emit(f"error: {e} (nothing was changed)", sys.stderr)
            return 2
    if getattr(args, "json", False):
        emit_json(m)
    else:
        emit(_manual_line(m))
        if m.get("decode_problem"):
            emit(f"  VIN: {m['decode_problem']}")
        elif m.get("decoded"):
            emit("  VIN decoded by NHTSA: " + " ".join(str(m["decoded"][k]) for k in valuation.DECODED if m["decoded"].get(k)))
        if m.get("estimate_result"):
            r = m["estimate_result"]
            emit(f"  estimate: {r['message']}" + ("" if r["ok"] or r["applied"] else " (the value stays as it was)"))
    return 0


def cmd_manual_add(args) -> int:
    try:
        fields = {"kind": args.kind, **_manual_fields(args)}
    except manual.ManualError as e:
        emit(f"error: {e} (nothing was changed)", sys.stderr)
        return 2
    return _manual_write(valuation.create, args, fields)


def cmd_manual_edit(args) -> int:
    try:
        fields = _manual_fields(args)
    except manual.ManualError as e:
        emit(f"error: {e} (nothing was changed)", sys.stderr)
        return 2
    if args.use_estimate:
        if fields:
            emit("error: --use-estimate takes no other field (a new value would be your value again)", sys.stderr)
            return 2
        return _manual_write(valuation.use_estimate, args, args.id)
    if not fields:
        emit("error: nothing to change (pass at least one field flag, --clear FIELD or --use-estimate)", sys.stderr)
        return 2
    return _manual_write(valuation.edit, args, args.id, fields)


def cmd_manual_archive(args) -> int:
    return _manual_write(manual.archive, args, args.id)


def _payment_line(p: dict, verb: str) -> str:
    if p.get("kind") == "credit_card":
        split = f"card payment, {report.money(p['principal'])} off the balance"
    elif p["apr_set"]:
        split = f"interest {report.money(p['interest'])} + principal {report.money(p['principal'])}"
    else:
        split = f"principal {report.money(p['principal'])} (APR not set, whole payment counted as principal)"
    done = "  · PAID OFF" if p["paid_off"] else ""
    return (f"{verb} {p['transaction_id']}  {p['date']}  {p['name']}  {report.money(p['amount'])} -> "
            f"#{p['loan_id']} {p['loan']}: {split}; balance {report.money(p['balance_before'])} -> "
            f"{report.money(p['balance_after'])}{done}")


def cmd_match_payments(args) -> int:
    """match-payments (loan-match is its alias): loans and credit cards with a match pattern."""
    name = args.command
    with db.connect() as conn:
        if getattr(args, "suggest", False):
            out = manual.suggest_patterns(conn, _today())
            if args.json:
                emit_json(out)
                return 0
            for x in out["suggestions"]:
                shared = " (connected cards use this label too; their payments are never applied)" \
                    if x["shared_with_connected_cards"] else ""
                emit(f"#{x['card_id']} {x['card']}: {x['date']} {x['name']} {report.money(x['amount'])} from "
                     f"{report.account_name(x['account_name'], x['account_mask']) or 'an account'}"
                     f"{' ••' + x['account_mask'] if x['account_mask'] else ''} pays no "
                     f"connected card{shared}. Suggested pattern: {x['pattern']!r} "
                     f"(finance manual edit {x['card_id']} --match-pattern {x['pattern']!r})")
            emit(f"{name} --suggest: {len(out['suggestions'])} line(s) for manual cards without a pattern "
                 f"(suggestions only, nothing saved)")
            return 0
        out = manual.match_payments(conn, dry_run=args.dry_run, today=_today())
    if args.json:
        emit_json(out)
        return 0
    verb = "would apply" if args.dry_run else "applied"
    for p in out["applied"]:
        emit(_payment_line(p, verb))
    for x in out["unapplied"]:
        emit(f"{'would take back' if args.dry_run else 'took back'} {x['transaction_id']} from #{x['loan_id']} "
             f"{x['loan']}: {x['reason']}")
    for x in out["skipped"]:
        emit(f"skipped {x['transaction_id']} for #{x['loan_id']}: {x['reason']}")
    emit(f"{name}: {len(out['applied'])} payment(s) {verb}" + (" (dry run, nothing written)" if args.dry_run else ""))
    return 0


cmd_loan_match = cmd_match_payments


def cmd_loan_apply(args) -> int:
    with db.connect() as conn:
        try:
            p = manual.apply_payment(conn, args.txn_id, args.loan_id, args.source, dry_run=args.dry_run)
        except manual.ManualError as e:
            emit(f"error: {e} (nothing was changed)", sys.stderr)
            return 2
    emit(_payment_line(p, "would apply" if args.dry_run else "applied"))
    return 0


def cmd_loan_unapply(args) -> int:
    with db.connect() as conn:
        try:
            p = manual.unapply_payment(conn, args.txn_id, dry_run=args.dry_run)
        except manual.ManualError as e:
            emit(f"error: {e} (nothing was changed)", sys.stderr)
            return 2
    verb = "would unapply" if args.dry_run else "unapplied"
    emit(f"{verb} {p['transaction_id']} ({report.money(p['amount'])}) from #{p['loan_id']} {p['loan']}: balance "
         f"{report.money(p['balance_before'])} -> {report.money(p['balance_after'])}")
    return 0


# --- automatic valuation (TASK-12) -------------------------------------------------------------

def cmd_revalue(args) -> int:
    """Estimates for vehicles / properties: those due (30+ days since their last lookup), or --account N now (at most
    once a day). Every call goes through the call guard. Exit 1 when a lookup failed (non-fatal for the daily run)."""
    with db.connect() as conn:
        try:
            out = valuation.revalue(conn, _today(), args.account, args.dry_run)
        except manual.ManualError as e:
            emit(f"error: {e}", sys.stderr)
            return 2
        usage_now = usage.report(conn, _today())
    if args.json:
        emit_json({**out, "usage": usage_now})
        return 1 if out["failed"] else 0
    for x in out["results"]:
        emit(f"#{x['account_id']} {x['name']} ({x['provider']}): {x['action'].replace('_', ' ')}: {x['message']}"
             + ("" if x["action"] != "valued" or x.get("applied") else "; kept your value"))
    if not out["results"]:
        emit("No vehicles or properties to value.")
    used = ", ".join(f"{p['label']} {p['calls']}/{p['cap']}" for p in usage_now["providers"] if p["provider"] in valuation.SOURCES)
    emit(f"revalue: {out['valued']} valued, {out['failed']} failed" + (" (dry run: nothing called or counted)"
                                                                       if args.dry_run else "") + f"; calls this month: {used}")
    return 1 if out["failed"] else 0


def cmd_photos(args) -> int:
    """The car / property pictures (TASK-13b): fetch the ones due (never fetched; a try that found nothing 30+ days
    ago; one that failed yesterday), or --account N now (--force: again, even if it has one). Every call goes through
    the call guard. Exit 1 when a fetch failed."""
    today = _today()
    with db.connect() as conn:
        if args.account is not None:
            try:
                row = manual.get(conn, args.account)
            except manual.ManualError as e:
                emit(f"error: {e}", sys.stderr)
                return 2
            if row["kind"] not in manual.VALUE_KINDS:
                emit(f"error: #{row['id']} {row['name']} is a {row['kind'].replace('_', ' ')}: only vehicles and properties "
                     "have a picture", sys.stderr)
                return 2
            rows = [row]
        else:
            rows = conn.execute("SELECT * FROM manual_accounts WHERE kind IN ('vehicle', 'property') AND archived_at IS NULL "
                                "ORDER BY id").fetchall()
        out = []
        for row in rows:
            rec = photos.ensure(conn, row["id"], today, photos.images_dir(config.db_path()), force=args.force)
            view = photos.view(conn, manual.get(conn, row["id"], include_archived=True))
            out.append({"account_id": row["id"], "name": row["name"], "fetched": rec is not None,
                        "status": rec["status"] if rec else view["status"], "picture": view})
        usage_now = usage.report(conn, today)
    if args.json:
        emit_json({"today": today.isoformat(), "results": out, "usage": usage_now})
    else:
        for x in out:
            v = x["picture"]
            what = {"cutout": "a photo, cut out", "photo": "a photo", "map": "a map with a pin", "silhouette": "the drawn car",
                    "outline": "the drawn house"}[v["kind"]]
            head = "fetched" if x["fetched"] else "kept"
            emit(f"#{x['account_id']} {x['name']}: {head}: {x['status']}, shows {what}" + (f" ({v['credit']})" if v["credit"] else "")
                 + (f"; {v['note']}" if v["note"] and x["status"] != "ready" else ""))
        if not out:
            emit("No vehicles or properties.")
        used = ", ".join(f"{p['label']} {p['calls']}/{p['cap']}" for p in usage_now["providers"] if p["provider"] in ("commons", "osm", "redfin"))
        emit(f"photos: {sum(x['fetched'] for x in out)} fetched; calls this month: {used}")
    return 1 if any(x["status"] == "error" and x["fetched"] for x in out) else 0


def cmd_api_usage(args) -> int:
    if args.unblock:
        with db.connect() as conn:
            n = usage.unblock(conn, args.unblock)
        emit(f"{usage.LABELS[args.unblock]}: {'block lifted (the monthly cap still applies)' if n else 'was not blocked'}")
        return 0
    return _read(args, usage.report, usage.report_text)


# --- logos (TASK-10) ----------------------------------------------------------------------------

def _institution_line(r: dict) -> str:
    name = f"{r['name'] or r['institution_id']} ({r['institution_id']})"
    if r.get("error"):
        return f"{name}: error, kept what was cached ({r['error']})"
    what = {"plaid": "plaid logo", "site": "icon from the bank's site",
            "none": f"initials{' in ' + r['color'] if r.get('color') else ''}"}[r["source"]]
    return f"{name}: {what}" + (f"  ({r['note']})" if r.get("note") and r["source"] != "plaid" else "")


def _refresh_banks(conn) -> tuple[list[str], bool]:
    results = logos.refresh_institutions(conn, _client())
    if not results:
        return ["No connected banks."], True
    return [_institution_line(r) for r in results], all(not r.get("error") for r in results)


def cmd_refresh_institutions(args) -> int:
    """Look up every connected bank (free /institutions/get_by_id; never creates an Item) and cache its logo."""
    with db.connect() as conn:
        _load_known_tokens(conn)
        lines, ok = _refresh_banks(conn)
    for line in lines:
        emit(line)
    return 0 if ok else 1


def cmd_refresh_logos(args) -> int:
    """Bank logos (as refresh-institutions) plus the logos of held stocks, ETFs and funds not cached yet."""
    with db.connect() as conn:
        _load_known_tokens(conn)
        lines, ok = _refresh_banks(conn)
        sec = logos.refresh_security_logos(conn, force=args.force)
    for line in lines:
        emit(line)
    emit(f"holdings: {len(sec['found'])} logo(s) fetched{' (' + ', '.join(sec['found']) + ')' if sec['found'] else ''}"
         f"; {len(sec['missed'])} without one{' (' + ', '.join(sec['missed']) + ', letters instead)' if sec['missed'] else ''}"
         f"; {len(sec['cached'])} already cached")
    if sec["error"]:
        emit(f"holdings: stopped early, network trouble ({sec['error']}); run it again later")
    return 0 if ok and not sec["error"] else 1

# --- read-only answers for agents (reads.py) ------------------------------------------------------------

def _today() -> date:
    return date.today()  # tests pin this


def _read_conn() -> sqlite3.Connection:
    conn = db.connect_readonly()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < db.SCHEMA_VERSION:
        conn.close()
        raise sqlite3.DatabaseError(f"the database is at schema v{version}, these commands need "
                                    f"v{db.SCHEMA_VERSION}; run `finance sync` once to upgrade it")
    return conn


def _read(args, build, text) -> int:
    """Open the DB read-only, build the payload, print it as JSON or text. Bad arguments exit 2."""
    try:
        conn = _read_conn()
    except (FileNotFoundError, sqlite3.Error) as e:
        emit(f"error: {e}", sys.stderr)
        return 1
    try:
        payload = build(conn, _today())
    except reads.ReadError as e:
        emit(f"error: {e}", sys.stderr)
        return 2
    except sqlite3.Error as e:
        emit(f"error: {e}", sys.stderr)
        return 1
    finally:
        conn.close()
    if args.json:
        emit_json(payload)
    else:
        emit(text(payload))
    return 0


def _filters(args, conn, today, **kw) -> reads.Filters:
    return reads.parse_filters(conn, today, month=args.month, since=args.since, until=args.until,
                               category=args.category, merchant=args.merchant, account=args.account,
                               limit=args.limit, **kw)


def cmd_spend(args) -> int:
    return _read(args, lambda conn, today: reads.spend(conn, _filters(args, conn, today, spending=True), today,
                                                       args.by), reads.spend_text)


def cmd_budget_status(args) -> int:
    return _read(args, lambda conn, today: reads.budget_status(conn, reads.parse_month(args.month, today), today),
                 reads.budget_status_text)


def cmd_txns(args) -> int:
    return _read(args, lambda conn, today: reads.txns(conn, _filters(args, conn, today, default_period="all"), today,
                                                      args.search, args.flagged, args.uncategorized, args.tag,
                                                      args.group, args.sort),
                 reads.txns_text)


def cmd_networth(args) -> int:
    return _read(args, lambda conn, today: reads.networth(conn, today, args.history, args.custom, args.cash_only),
                 reads.networth_text)


def cmd_networth_custom(args) -> int:
    """List, or change, the accounts the Custom net worth leaves out (the `networth_excluded` setting the dashboard's
    Edit sheet saves). An id that isn't a current account is an error (exit 2) and nothing is saved."""
    exclude, include = args.exclude or [], args.include or []
    conn = db.connect()
    try:
        before = insights.networth_excluded(conn)
        known = [a["id"] for a in insights.networth_accounts(conn)]
        bad = [x for x in exclude + include if x not in known]
        if bad:
            emit(f"error: not a current account: {', '.join(map(repr, bad))} (use the ids `finance networth-custom --list` "
                 "shows, the nw_id of `finance networth --json`)", sys.stderr)
            return 2
        both = sorted(set(exclude) & set(include))
        if both:
            emit(f"error: both --exclude and --include: {', '.join(map(repr, both))}", sys.stderr)
            return 2
        changed = None
        if args.reset or args.preset or exclude or include:
            new = [] if args.reset else insights.cash_preset(conn) if args.preset else list(before)
            new = [x for x in new + [x for x in exclude if x not in new] if x not in include]
            saved, _ = insights.save_networth_excluded(conn, new)
            changed = set(saved) != {x for x in before if x in known}
        payload = reads.networth_custom(conn, _today(), changed)
    finally:
        conn.close()
    if args.json:
        emit_json(payload)
    else:
        emit(reads.networth_custom_text(payload))
    return 0


def cmd_bills(args) -> int:
    return _read(args, lambda conn, today: reads.bills(
        conn, today, reads.parse_days(args.next, "--next", 0, reads.MAX_NEXT_DAYS, bare=True)), reads.bills_text)


def _bill_write(args, action: str, **kw) -> int:
    """TASK-21: finance bills track / untrack, through the same plan and apply as the dashboard's Track as a bill sheet and
    the finance_track_bill tool. A request that can't be done exits 2 and changes nothing."""
    from . import bills
    conn = db.connect()
    try:
        p = bills.plan(conn, _today(), action, merchant=args.merchant, **kw)
        out = bills.apply(conn, p, _today())
    except bills.BillError as e:
        emit(f"error: {e}", sys.stderr)
        return 2
    finally:
        conn.close()
    if args.json:
        emit_json(out)
        return 0
    emit(("Saved. " if action != "stop" else "Stopped. ") + out["summary"])
    b = out["bill"]
    if b:
        acct = reads.account_label(b["last_account"]["name"], b["last_account"]["mask"]) if b["last_account"] else "-"
        emit(f"{b['merchant']}: {b['cadence_label']}, next expected {b['next_expected']}, {reads.bill_amount_text(b)}, "
             f"last paid {b['last_date']} {report.money(b['last_amount'])} from {acct}"
             + (f"; that charge is {b['outlier']} than usual" if b["outlier"] else ""))
    return 0


def cmd_bills_track(args) -> int:
    kw = {"every": args.every, "varies": args.varies}
    if args.usually is not None:
        kw["usually"] = args.usually
    return _bill_write(args, "track", **kw)


def cmd_bills_untrack(args) -> int:
    return _bill_write(args, "stop")


def cmd_loans(args) -> int:
    return _read(args, reads.loans, reads.loans_text)


def cmd_holdings(args) -> int:
    return _read(args, reads.holdings, reads.holdings_text)


def cmd_cashflow(args) -> int:
    return _read(args, lambda conn, today: reads.cashflow(conn, today, args.months), reads.cashflow_text)


# --- wiring ------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="finance", description="Plaid -> local SQLite budgeting pipeline")
    p.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="local budgeting dashboard; connect / reconnect banks")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1",
                   help="loopback unless the built-in login is set up; use tailscale serve for other devices")
    s.add_argument("--public-url", action="append", metavar="URL",
                   help="URL tailscale serve exposes, e.g. https://host.tailnet.ts.net:8765 (repeatable; "
                        "also FINANCE_PUBLIC_URLS, comma-separated)")
    s.add_argument("--allow-user", action="append", metavar="LOGIN",
                   help="Tailscale login (or your proxy's identity) allowed on public URLs (repeatable; also "
                        "FINANCE_ALLOWED_USERS; the first start saves them: Settings -> Access & security)")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("sync", help="pull transactions, balances, liabilities, holdings")
    s.add_argument("--item", help="only this item_id")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("status", help="items, accounts, balances, errors, slots used")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("query", help="read-only SQL against the database")
    s.add_argument("sql")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_query)

    s = sub.add_parser("export", help="export transactions (JSON to stdout, or CSV)")
    s.add_argument("--since", required=True, help="YYYY-MM-DD")
    s.add_argument("--csv", metavar="PATH", help="write CSV to PATH ('-' for stdout)")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("classify-apply", help="apply rules, then Plaid-mapped defaults (deterministic)")
    s.set_defaults(func=cmd_classify_apply)

    s = sub.add_parser("unclassified", help="transactions still needing an AI/human decision")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--since", help="YYYY-MM-DD")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_unclassified)

    s = sub.add_parser("set-category", help="classify one transaction (ai or user)")
    s.add_argument("txn_id")
    s.add_argument("category", help="category name (case-insensitive) or id")
    s.add_argument("--source", choices=["ai", "user"], default="user")
    s.add_argument("--confidence", type=float)
    s.add_argument("--note")
    s.add_argument("--make-rule", action="store_true",
                   help="also create an exact rule on this merchant_name (or name) and apply it")
    s.set_defaults(func=cmd_set_category)

    s = sub.add_parser("add-rule", help="create/update a merchant/name rule and apply it")
    s.add_argument("--field", choices=["merchant_name", "name"], default="merchant_name")
    s.add_argument("--pattern", required=True)
    s.add_argument("--category", required=True)
    s.add_argument("--regex", action="store_true", help="pattern is a regex (case-insensitive search)")
    s.add_argument("--priority", type=int, help="higher wins (default: 200 for user rules, 100 otherwise)")
    s.add_argument("--created-from", default="user",
                   help="user (default: re-files matching rows, ai guesses included) | ai | <free text>")
    s.set_defaults(func=cmd_add_rule)

    s = sub.add_parser("ai-queue", help="save a run of transactions needing an AI look, by merchant; print its JSON")
    s.add_argument("--max-groups", type=int, default=ai.MAX_GROUPS, metavar="N",
                   help=f"at most N merchant groups, oldest first (default {ai.MAX_GROUPS})")
    s.set_defaults(func=cmd_ai_queue)

    s = sub.add_parser("ai-apply", help="apply the AI's decisions for an ai-queue run (validated, atomic, once)")
    s.add_argument("file", metavar="FILE", help='decisions JSON: {"run_id": ..., "decisions": [...]}')
    s.add_argument("--dry-run", action="store_true", help="compute the full outcome, then roll it all back")
    s.set_defaults(func=cmd_ai_apply)

    m = sub.add_parser("manual", help="accounts Plaid can't see: list / add / edit / archive")
    msub = m.add_subparsers(dest="manual_command", required=True)
    s = msub.add_parser("list", help="active manual accounts (and totals)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--archived", action="store_true", help="include archived ones")
    s.set_defaults(func=cmd_manual_list)
    s = msub.add_parser("add", help="add a manual account")
    s.add_argument("--kind", required=True, choices=manual.KINDS)
    for flag, field, typ, help_ in MANUAL_FLAGS:
        s.add_argument(flag, dest=field, type=typ, help=help_)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_manual_add)
    s = msub.add_parser("edit", help="change fields of a manual account (a new --balance is true as of --as-of / today)")
    s.add_argument("id", type=int)
    for flag, field, typ, help_ in MANUAL_FLAGS:
        s.add_argument(flag, dest=field, type=typ, help=help_)
    s.add_argument("--clear", action="append", choices=CLEARABLE, metavar="FIELD",
                   help=f"unset an optional field (repeatable): {', '.join(CLEARABLE)}")
    s.add_argument("--use-estimate", action="store_true",
                   help="vehicles / property: follow the estimate again instead of your value")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_manual_edit)
    s = msub.add_parser("archive", help="hide a manual account from every total (kept, never deleted)")
    s.add_argument("id", type=int)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_manual_archive)

    s = sub.add_parser("networth-custom", help="list or change which accounts the Custom net worth counts",
                       description="List or change which accounts the Custom net worth counts.",
                       epilog=NETWORTH_CUSTOM_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("--exclude", nargs="+", metavar="ID", help="leave these accounts out")
    s.add_argument("--include", nargs="+", metavar="ID", help="count these accounts again")
    base = s.add_mutually_exclusive_group()
    base.add_argument("--reset", action="store_true", help="count every account (the sheet's \"Everything\")")
    base.add_argument("--preset", choices=["cash-and-debts"], help="start again from a preset (see below)")
    s.add_argument("--list", action="store_true", help="every account with its id and whether Custom counts it (the default)")
    s.add_argument("--json", action="store_true", help="print the JSON described below")
    s.set_defaults(func=cmd_networth_custom)

    for name, help_ in (("match-payments", "apply new bank payments to loans and credit cards by their match pattern "
                                           "(source auto; never a connected card's payment)"),
                        ("loan-match", "alias of match-payments")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--dry-run", action="store_true", help="show what would be applied; write nothing")
        s.add_argument("--suggest", action="store_true",
                       help="list unpaired card-payment lines that mention a manual card without a pattern, with the "
                            "pattern to use (nothing is saved)")
        s.add_argument("--json", action="store_true")
        s.set_defaults(func=cmd_match_payments)

    s = sub.add_parser("loan-apply", help="apply one bank transaction to a loan (e.g. a payment the pattern misses)")
    s.add_argument("txn_id")
    s.add_argument("loan_id", type=int)
    s.add_argument("--source", choices=["ai", "user"], default="user")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_loan_apply)

    s = sub.add_parser("loan-unapply", help="take a transaction back off its loan (restores the balance)")
    s.add_argument("txn_id")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_loan_unapply)

    s = sub.add_parser("revalue", help="estimate vehicle and property values (Carfax for cars, Redfin for homes, "
                                       "within their caps): the ones due for their monthly lookup, or --account N now")
    s.add_argument("--account", type=int, metavar="N", help="this manual account now (at most once a day)")
    s.add_argument("--dry-run", action="store_true", help="show what would be looked up; call and count nothing")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_revalue)

    s = sub.add_parser("photos", help="fetch the car / property pictures that are due (Wikimedia Commons, Redfin, "
                                      "OpenStreetMap; within their caps), or --account N now")
    s.add_argument("--account", type=int, metavar="N", help="this vehicle / property")
    s.add_argument("--force", action="store_true", help="fetch again even if it has a picture (or was tried lately)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_photos)

    s = sub.add_parser("api-usage", help="valuation / photo calls this month: used / cap per provider")
    s.add_argument("--json", action="store_true")
    s.add_argument("--unblock", choices=usage.PROVIDERS, metavar="PROVIDER",
                   help="lift a provider's block by hand (the monthly cap still applies)")
    s.set_defaults(func=cmd_api_usage)

    s = sub.add_parser("refresh-institutions",
                       help="cache each connected bank's logo and colour (free Plaid lookup, else the bank's site icon)")
    s.set_defaults(func=cmd_refresh_institutions)

    s = sub.add_parser("refresh-logos", help="refresh-institutions, plus logos for held stocks, ETFs and funds")
    s.add_argument("--force", action="store_true", help="fetch holding logos again, cached ones and misses too")
    s.set_defaults(func=cmd_refresh_logos)

    _add_read_commands(sub)
    _add_mcp_command(sub)
    _add_settings_commands(sub)
    _add_task19_commands(sub)
    _add_rules_commands(sub)   # TASK-24
    return p


READ_NOTE = ("Read-only (the database is opened read-only; never calls Plaid). Same numbers as the dashboard. "
             "Money is signed as in the app: spend and money out positive, refunds and income negative. "
             "Dates are local. Bad arguments exit 2.")
SPEND_EPILOG = """Spending = every category except Income and Transfers (the dashboard's rule); refunds net against their
category. No period: this month. The comparison: a month is compared with the month before up to the same day
while it is the current month (as on the Overview), else with the whole month before; a date range with the
same number of days just before it; an open-ended range (only --until) with nothing.

JSON: {today, period: {kind: month|range, month, start, end, days},
        filters: {category, merchant, account: {query, matched: [{account_id, name, mask, type, institution}]}},
        total, count, pending: {total, count},
        previous: {kind, month, start, end, days, total, count} | null, change: {amount, pct} | null,
        by, groups: [{key, label, total, count, share, previous}], group_count}
  groups are sorted by total (months: oldest first) and cut to --limit; group_count is how many there were.
  key: category name | merchant key (lowercase) | YYYY-MM | account_id. share: % of total. previous: the same
  key's total in the previous period (null for --by month)."""
BUDGET_EPILOG = """Each cap: the Budgets page's cap, spent, its recurring bills still due this month (due), % used (spent /
cap) and status from spent + due (ok, warn = at or above 80%, over = at or above 100%). left = cap - spent - due
(negative when over). projection = the Overview's pace projection for that category alone: spend so far + its
non-fixed spend at its daily pace for the days left + its recurring bills still due this month (Rent & Utilities,
Subscriptions and recurring bills aren't extrapolated).

JSON: {today, month, is_current, days_in_month, days_left,
       caps: [{category, cap, spent, due, bills_due, left, pct, status, count, days_left, projection,
               projection_status, last_month, avg3, counted?, in_group_budget?}],
       groups: [{group, budget, spent, due, bills_due, left, pct, status, count, days_left, projection,
                 projection_status, categories, caps: [{category, cap}], caps_total, over_note}],
       group_rest: [{group, categories, spent, due}],
       totals: {budgeted, spent, due, left, per_day, count, over, warn, under, under_count, over_amount,
                bills_due: [{merchant, amount, next_expected, category}]},
       uncapped: {spent, count, categories: [{category, spent, count}]},
       spending: {total, projection, daily_pace, bills_due_total, prev_month_total}}
  groups are the group budgets (a budget on a whole category group, e.g. Food): spent is every category in the
  group, and the group counts once in the totals. A cap inside a budgeted group is still listed, with
  counted: false and in_group_budget: <group>, and isn't counted again; its category isn't in uncapped either.
  over_note is the caps' sum when they add up to more than the group budget, else null.
  group_rest: a group with caps but no group budget counts all its spending: its uncapped categories' spent and
  due (categories: the ones with any) are in the totals, so they aren't in uncapped. That rest uses up the group's
  free cap room first (under); what's past it is over_amount, and a group it alone pushes over adds 1 to over.
  caps are sorted by % used, highest first. totals.spent is spend in the counted budgets. totals.left is the net,
  budgeted - spent - due: an overspent cap counts against it, so it is negative when the month is over budget
  (the Overview's "Left to spend"). It splits into under (still free in the under_count caps not used up) minus
  over_amount (how far the `over` caps are past theirs). per_day is max(left, 0) over the days left (0 once it's
  used up, null for a past month). due is only this month's (a past month has none). spending.* is all spending
  this month (the Overview's pace card)."""
TXNS_EPILOG = """No period: all history. Newest first; within a day the biggest amount first, then the latest
authorized, then by id (the Transactions page's order).
  --search TEXT     part of the name, merchant, category, account, bank, AI note, your note or a tag, or the
                    amount (e.g. 465.10)
  --tag NAME        only rows with this tag; repeat it for any of several
  --group NAME      only rows in a category of this group (Food, Bills, ...); a split purchase by any of its lines
  --sort ORDER      date_desc (default), date_asc, amount_desc, amount_asc, merchant_asc, merchant_desc,
                    category_asc, category_desc, account_asc, account_desc (amount: Plaid sign, money out first)
  --flagged         flagged by the AI and not decided by you since
  --uncategorized   under Other and nobody decided it (not confirmed or changed by the AI, not filed there
                    by you or your rule), or with no category at all

JSON: {today, period, filters: {category, merchant, account, search, flagged, uncategorized, tag?, group?, sort},
        total, count, limit, counts: {all, review, spend, income, transfers},
        rows: [{transaction_id, date, name, merchant, merchant_name, amount, category, category_source, account,
                account_name, account_mask, institution_name, pending, ai_flagged, review, ai_note, is_income,
                is_transfer, split: [{category, amount}] | null, note, tags}]}
  total = rows matching; counts = those rows by kind. category_source: user | rule | ai | plaid | default. split: the
  lines of a split purchase (spending totals count each line under its own category); --category matches a split
  purchase by any of its lines."""
NETWORTH_EPILOG = """Assets minus debts from current balances of connected Plaid accounts plus your manual accounts, grouped
as on the Accounts page (Cash, Investments, Vehicles, Property, Other assets, then Credit cards, Loans, Other debts;
the two "Other" groups only when used). --custom: the Accounts page's "Custom" net worth, only the accounts you count
(chosen on the page's Edit sheet, or with `finance networth-custom`; every account counts unless you left it out).

JSON: {today, mode: all | custom, assets, debts, net,
       custom: {assets, debts, net, excluded: [{id, source, name, mask, institution, kind, group, balance, signed}]},
       groups: [{key, label, asset, count, total, signed, month_start, month_change: {amount, pct} | null,
                 custom: {count, total, signed} (without --custom),
                 accounts: [{source: plaid|manual, id, nw_id, counted, name, mask, institution, type, subtype, balance,
                             signed, limit, as_of, stale, approx,
                             valuation (vehicles, property): {value, source: estimate|user, label, provider, basis,
                                                              as_of, estimate: {value, source, date, miles, basis}
                                                              | null}}]}],
       in_transit: [{transaction_id, date, amount, from_account: {name, mask}, card_id, card, mask, institution, label,
                     line, counted_custom}],
       manual: {assets, debts, count}, tracking_since,
       history: [{date, assets, debts, net, custom_assets, custom_debts, custom_net}] | null}
  in_transit: card payments that left a bank account in the last 5 days but the card hasn't posted yet; each is counted
  as already paid (debts and net, and today's history point; with --custom only those to a card Custom counts). The
  accounts keep the balances the banks report.
  custom (always there): the Custom net worth, every account except the ones left out (excluded, by id). nw_id is the
  id Custom uses (the Plaid account_id, or manual:<id>; `finance networth-custom --exclude / --include` take it);
  counted: whether Custom counts the account. With --custom, mode is custom, the top assets / debts / net are Custom's,
  and each group's count / total / signed and accounts are only the ones it counts (a group left out entirely isn't
  listed). History points always carry both (custom_*: the same day counting only the accounts Custom counts today).
   month_start (the all-accounts view; null in --custom): the group's total at the start of the current month, or null
   when any of its accounts has no balance that early. month_change (same view): {amount, pct} on the signed basis —
   how much the group's contribution to net worth grew this month, so a debt group's amount is positive when it came
   down (good) and an asset's when it went up; pct = amount / the start's absolute value, null when that is 0.
   balance is positive for debts too; signed is negative for debts. history only with --history. approx: a manual
  credit card whose balance follows matched payments (purchases since its balance date aren't seen). valuation:
  source estimate = the value follows the estimate (label = the provider, as_of = its date); user = the value typed by
  you ("Your value", as_of = when you set it), with the estimate beside it. basis: trade_in = a car's Carfax trade-in;
  estimate = a home's Redfin Estimate. No estimate (a car Carfax can't price, a condo unit Redfin has no record of):
  estimate is null."""
NETWORTH_CUSTOM_EPILOG = """The Custom net worth (the Accounts page's All · Custom switch, and `finance networth --custom`) counts every
account except the ones you leave out. The same saved choice as the page's Edit sheet: the phone, the laptop and the
CLI all agree. An account connected or added later counts until you leave it out. Without an option it lists every
account with its id (--list). Ids are the ones --list and `finance networth --json` (nw_id) show: the Plaid
account_id, or manual:<id>. An unknown id is an error and nothing is saved.

  --exclude ID...   leave these accounts out
  --include ID...   count these accounts again
  --reset           count every account (the sheet's "Everything")
  --preset cash-and-debts
                    start again from the "Cash & debts" choice: cash minus every debt (every investment, vehicle,
                    property and other-asset account left out); --exclude / --include apply after it

JSON: {today, changed: true | false | null, excluded: [id], custom: {assets, debts, net}, all: {assets, debts, net},
       accounts: [{id, source: plaid|manual, name, mask, institution, kind, group, group_label, balance, signed,
                   counted}]}
  changed: null when nothing was asked to change."""
BILLS_EPILOG = """From the Recurring page's detector (weekly, every 2 weeks, monthly, every 2 / 3 / 6 months and yearly charges with
stable amounts; a utility whose amount changes by its schedule alone) and the bills you track by hand (finance bills
track). A bill is the merchant, not the account. Lists the ones expected within --next days, plus any whose expected
date has already passed.

JSON: {today, next_days, until, count,
       items: [{merchant, key, kind: bill|subscription, category, amount, cadence, cadence_label, interval_days, charges,
                last_date, last_amount, next_expected, days_until, status, paid_this_month, monthly_amount, variable,
                estimate, amount_label, usual: {low, high, source: charges|you} | null, source: detected|tracked,
                last_account: {name, mask, institution} | null, outlier: higher|lower|null}],
       recurring_count, monthly_totals: {bill, subscription}}
  cadence: weekly | biweekly | monthly | bimonthly | quarterly | semiannual | yearly (cadence_label: "every 2 months" ...).
  variable: the amount changes each time; amount (= estimate) is the median of its last 3 charges, or while there are
  fewer than 3, the middle of the usual amount you gave (amount_label "~$125 est."). usual: the range its charges spanned,
  or the one you gave (source you). outlier: its last charge was over 1.5x the top of the usual you gave (higher) or under
  half its bottom (lower). monthly_amount: amount x how often it comes a month (every 2 months: 0.5).
  status: upcoming | due_soon (expected within 3 days, or already passed) | late (more than the schedule's longest
  interval + 5 days since the last charge: monthly 40, every 2 months 75). paid_this_month: its last charge was this
  calendar month. Sorted by next_expected.

  finance bills track MERCHANT [--every 2m] [--varies | --same] [--usually 100-150]   (writes)
  finance bills untrack MERCHANT                                                      (writes)"""
BILLS_TRACK_EPILOG = """Every charge from the merchant, on any account, confirms the bill; the next one is expected one schedule after
the last. --every: 1w, 2w, 1m, 2m, 3m, 6m or 1y. Not given: the schedule it was detected on, else every month; the
amount "changes each time" for a utility, else the same each time. It starts from the merchant's latest charge. Tracking
a bill again replaces its settings. --usually (a bill that changes): one amount or a range; while there are fewer than 3
charges its middle is the estimate (one big bill doesn't set it), after that the median of the last 3.

The merchant must be in your transactions: its exact name, or a unique case-insensitive match (an ambiguous or unknown
name is refused with the candidates). Exit 2 and nothing saved when it can't be done."""
LOANS_EPILOG = """Loans from the Loans & debt page: your manual loans (their balance comes down with each matched bank
payment), and any loan a bank reports through Plaid (manual: false).

JSON: {today, count, total, monthly,
       loans: [{id, manual, name, institution, type, type_label, remaining, original_amount, paid_pct, paid_off,
                apr, monthly_payment, payoff: {payments_left, payoff_month, method}, next_payment,
                last_payment: {date, amount, name} | null, match_pattern, auto_updates, applied_count,
                balance_as_of, stale}]}
  payoff.method: amortized (with APR) | simple (no APR) | paid | none (no payment set) | never (payment doesn't
  cover the interest). last_payment: the latest matched payment. total: remaining across all loans; monthly:
  payments across the loans not paid off."""
HOLDINGS_EPILOG = """The Investments page: accounts (Plaid + manual) and holdings, each with its % of the portfolio.

JSON: {today, total, holdings_total, prices_as_of,
       accounts: [{id, name, institution, mask, type, group, manual, value, pct, holdings, as_of, stale}],
       holdings: [{account_id, account_name, mask, ticker, name, type, quantity, price, value, cost_basis,
                   is_cash, pct}],
       breakdown: [{label, value, count}], changes: {7d, 1m, ytd, all}, tracking_since}
  a change is {from, to, amount, pct} or null until there are real daily snapshots old enough."""
CASHFLOW_EPILOG = """Income vs spending per month, oldest first, ending with this month (the Overview's cash flow).

JSON: {today, months: [{month, income, spending, net, coverage: full|partial, missing, is_current}], first_dates}
  income is positive here; partial = the month starts before a connected bank's first transaction (missing
  names those banks, first_dates has each bank's first day)."""


def _add_filters(s) -> None:
    g = s.add_argument_group("filters (each one narrows; all are validated)")
    g.add_argument("--month", metavar="YYYY-MM", help="one calendar month")
    g.add_argument("--since", metavar="30d|YYYY-MM-DD",
                   help="from that day, through --until or today (Nd = the last N days, today included)")
    g.add_argument("--until", metavar="YYYY-MM-DD", help="through that day (inclusive; not with --month)")
    g.add_argument("--category", metavar="NAME", help="case-insensitive; must exist")
    g.add_argument("--merchant", metavar="TEXT", help="part of the merchant name or the transaction name")
    g.add_argument("--account", metavar="MASK|NAME", help="last digits, name, or part of the name (all that match)")
    g.add_argument("--limit", type=int, default=api.DEFAULT_LIMIT, metavar="N",
                   help=f"rows / groups shown, 1-{api.MAX_LIMIT} (default {api.DEFAULT_LIMIT})")


def _add_read_commands(sub) -> None:
    def read(name, help_, epilog, func):
        s = sub.add_parser(name, help=help_, description=f"{help_[0].upper()}{help_[1:]}. {READ_NOTE}".replace("%%", "%"),
                           epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
        s.add_argument("--json", action="store_true", help="print the JSON described below")
        s.set_defaults(func=func)
        return s

    s = read("spend", "real spending totals, with the previous equal period", SPEND_EPILOG, cmd_spend)
    _add_filters(s)
    s.add_argument("--by", choices=list(reads.SPEND_GROUPS), help="group the total")
    s = read("budget-status", "each budget cap: spent, left, %% used, status, pace projection", BUDGET_EPILOG,
             cmd_budget_status)
    s.add_argument("--month", metavar="YYYY-MM", help="default: this month")
    s = read("txns", "transactions, newest first", TXNS_EPILOG, cmd_txns)
    _add_filters(s)
    s.add_argument("--search", metavar="TEXT", help="free text (see below)")
    s.add_argument("--flagged", action="store_true", help="only rows the AI flagged")
    s.add_argument("--uncategorized", action="store_true", help="only rows without a decided category (undecided Other, or none)")
    s.add_argument("--tag", metavar="NAME", action="append", help="only rows with this tag (case-insensitive; repeat for any)")
    s.add_argument("--group", metavar="NAME", help="only rows in this category group (case-insensitive)")
    s.add_argument("--sort", metavar="ORDER", default=api.DEFAULT_SORT, choices=api.SORTS, help="see below")
    s = read("networth", "assets, debts and net worth, grouped as on the Accounts page", NETWORTH_EPILOG,
             cmd_networth)
    s.add_argument("--history", action="store_true", help="also the daily net worth points")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--custom", action="store_true", help="only the accounts you count (the Accounts page's \"Custom\" "
                                                            "net worth; change them with finance networth-custom)")
    # TASK-14's "Cash & debts" mode, now a preset of Custom: kept working for one release, not shown in --help
    mode.add_argument("--cash-only", action="store_true", help=argparse.SUPPRESS)
    s = read("bills", "upcoming recurring bills and subscriptions", BILLS_EPILOG, cmd_bills)
    s.add_argument("--next", default="14d", metavar="Nd", help="how far ahead, 0d-730d (default 14d)")
    # TASK-21: bills tracked by hand (these two write; the list above never does)
    bsub = s.add_subparsers(dest="bills_command", metavar="{track,untrack}")
    t = bsub.add_parser("track", help="track a merchant as a bill by hand (writes)", epilog=BILLS_TRACK_EPILOG,
                        description="Track a merchant as a bill: every charge from it, on any account, confirms it.",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    t.add_argument("merchant", help="the merchant's name as the transactions show it (exact, or a unique match)")
    t.add_argument("--every", metavar="2w|1m|2m|3m|6m|1y",
                   help="the schedule (default: the one it was detected on, else 1m); 1w also works")
    amount = t.add_mutually_exclusive_group()
    amount.add_argument("--varies", action="store_true", default=None, help="the amount changes each time")
    amount.add_argument("--same", dest="varies", action="store_false", help="the same amount each time")
    t.add_argument("--usually", metavar="100-150", help="what it usually is: an amount or a range (implies --varies)")
    t.add_argument("--json", action="store_true", help="print the result as JSON")
    t.set_defaults(func=cmd_bills_track)
    u = bsub.add_parser("untrack", help="stop tracking a bill (a detected one is hidden from bills; writes)",
                        description="Stop tracking a bill. One tracked by hand stops; a detected one is no longer listed.")
    u.add_argument("merchant", help="the merchant's name as the transactions show it (exact, or a unique match)")
    u.add_argument("--json", action="store_true", help="print the result as JSON")
    u.set_defaults(func=cmd_bills_untrack)
    read("loans", "loans: remaining, APR, payment, payoff, last matched payment", LOANS_EPILOG, cmd_loans)
    read("holdings", "investment accounts and holdings with value and %% of portfolio", HOLDINGS_EPILOG,
         cmd_holdings)
    s = read("cashflow", "income vs spending per month", CASHFLOW_EPILOG, cmd_cashflow)
    s.add_argument("--months", type=int, default=6, metavar="N", help=f"how many months, 1-{reads.MAX_MONTHS} "
                                                                      "(default 6)")


# --- MCP server (TASK-17) ------------------------------------------------------------------------------------------

MCP_EPILOG = """Serves the finance_* tools over the Model Context Protocol on stdin/stdout, for an MCP client that starts it
(Claude Desktop, Cursor, Codex, ...). The same tools as the Hermes plugin and the same numbers as `finance <command>
--json`: finance_spend, finance_budget_status, finance_transactions, finance_networth, finance_bills, finance_loans,
finance_holdings, finance_cashflow, finance_status, finance_goals, finance_rules. Read-only (the database is opened read-only; never calls Plaid).
`finance serve` also serves them over streamable HTTP at /mcp, behind its access gate.

finance_sync syncs the banks now (Plaid, one sync a minute per bank) and says what changed; with refresh=true it
first asks the banks for fresh data, only if Settings allows it, and on a paid Plaid plan as a pending action.

The write tools (finance_set_category, finance_add_rule, finance_set_cap, finance_track_bill) are listed only after
`finance mcp allow-writes on`, and even then a call changes nothing: it is saved as a pending action for you to
decide (Settings -> Connect other agents -> Approve / Decline). Needs the optional extra: uv sync --extra mcp"""


def _mcp_path():
    return config.db_path()


def cmd_mcp(args) -> int:
    try:
        from .mcp_server import serve_stdio
    except ImportError as e:
        emit(f"error: the MCP server needs the mcp extra (uv sync --extra mcp): {e}", sys.stderr)
        return 2
    if not _mcp_enabled():
        emit("error: the MCP server is switched off (Settings -> Connect other agents -> MCP server)", sys.stderr)
        return 2
    serve_stdio(_mcp_path())  # stdout is the protocol: nothing else is ever printed there
    return 0


def _mcp_enabled() -> bool:
    """Settings -> Connect other agents -> MCP server (read-only; a missing or older database counts as on)."""
    from . import prefs
    try:
        conn = db.connect_readonly(_mcp_path())
    except (FileNotFoundError, sqlite3.Error):
        return True
    try:
        return prefs.get(conn, "mcp.enabled") is not False
    except sqlite3.Error:
        return True
    finally:
        conn.close()


def cmd_mcp_status(args) -> int:
    """Whether MCP clients may ask for changes, who connected last, and the changes waiting (read-only)."""
    from . import mcp_state, tooldefs
    try:
        conn = tooldefs.read_connection(_mcp_path())
    except tooldefs.ToolError as e:
        emit(f"error: {e}", sys.stderr)
        return 1
    try:
        payload = {**mcp_state.summary(conn), "pending_actions": mcp_state.pending(conn)}
    finally:
        conn.close()
    if args.json:
        emit_json(payload)
        return 0
    last = payload["last_used"]
    emit(f"write tools: {'listed (each call is saved as a pending action)' if payload['allow_writes'] else 'off'}")
    emit(f"last used: {last['client']}{' ' + last['version'] if last['version'] else ''} over {last['transport']} at "
         f"{last['at']}" if last else "last used: never")
    for a in payload["pending_actions"]:
        emit(f"pending #{a['id']} {a['created_at']} {a['client'] or '-'}: {a['summary']}")
    emit(f"{payload['pending']} pending action(s)")
    return 0


def cmd_mcp_allow_writes(args) -> int:
    from . import mcp_state, tooldefs
    try:
        conn = tooldefs.write_connection(_mcp_path())
    except tooldefs.ToolError as e:
        emit(f"error: {e}", sys.stderr)
        return 1
    try:
        mcp_state.set_allow_writes(conn, args.state == "on")
    finally:
        conn.close()
    emit("MCP write tools: " + ("listed; each call is saved as a pending action, nothing is applied" if args.state == "on"
                                else "off (read-only)"))
    return 0


def _add_mcp_command(sub) -> None:
    m = sub.add_parser("mcp", help="MCP server on stdio (the finance_* tools; read-only)", epilog=MCP_EPILOG,
                       description="MCP server on stdio: the finance_* tools, read-only.",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    m.set_defaults(func=cmd_mcp)
    msub = m.add_subparsers(dest="mcp_command")
    s = msub.add_parser("status", help="write tools on/off, the last client, pending actions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_mcp_status)
    s = msub.add_parser("allow-writes", help="list the write tools to MCP clients (calls only become pending actions)")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(func=cmd_mcp_allow_writes)


SETTINGS_EPILOG = """The Settings page's values, read-only (the database is opened read-only). For the crons: sync.daily_time,
sync.weekly_day / weekly_time, which alerts are on and where they go.

JSON: {sync: {daily_time, weekly_day, weekly_time}, alerts: {cap_80, over_cap, broken_connection, destination},
       ai: {mode: hermes|api|off, preset, base_url, model, monthly_cap, min_confidence}, mcp: {enabled, allow_writes},
       valuations: {carfax, redfin, photos}, region: {currency, locale, week_start, budget_month_start},
       appearance: {theme}, access: {mode: local|proxy|builtin, proxy_header, identities (count)}}
  Times are local, 24-hour HH:MM; days sun..sat. valuations is what's in effect (an env-file flag wins).
  No key, password or identity is ever printed."""
ACCESS_EPILOG = """Who can open the app (Settings -> Access & security):
  local    This computer only: localhost, no login (a new install's default)
  proxy    Behind your proxy login: a proxy on this computer (tailscale serve, Cloudflare Access, ...) signs people in;
           requests for its public URL (--public-url) need an allowed identity in its header
  builtin  Built-in login: a password and an authenticator app (set it up on the Settings page)

  finance access                    show the mode (identities masked)
  finance access mode proxy         choose a mode (proxy needs an identity; builtin a finished set-up)
  finance access allow LOGIN        allow an identity (the proxy's signed-in login, e.g. you@example.com)
  finance access disallow LOGIN
  finance access reset-login        remove the built-in login (password, authenticator, codes, sessions) and go back
                                    to "This computer only": the way back in when you're locked out"""


def _settings_payload(conn) -> dict:
    from . import access, prefs
    out = prefs.public(conn)
    out["mcp"]["allow_writes"] = bool(prefs.get(conn, "mcp.allow_writes"))
    out["valuations"] = config.free_sources(conn=conn)
    acc = access.load(conn)
    out["access"] = {"mode": acc.mode, "proxy_header": acc.header, "identities": len(acc.identities)}
    return out


def cmd_settings(args) -> int:
    try:
        conn = db.connect_readonly()
    except FileNotFoundError as e:
        emit(f"error: {e}", sys.stderr)
        return 1
    try:
        payload = _settings_payload(conn)
    except sqlite3.OperationalError as e:  # a database from before TASK-18 still has every default
        emit(f"error: {e} (run `finance sync` once to upgrade the database)", sys.stderr)
        return 1
    finally:
        conn.close()
    if args.json:
        emit_json(payload)
        return 0
    for group, values in payload.items():
        emit(f"{group}: " + ", ".join(f"{k}={v}" for k, v in values.items()))
    return 0


def cmd_access(args) -> int:
    from . import access, auth
    with db.connect() as conn:
        acc = access.load(conn)
        try:
            if args.access_command == "mode":
                access.set_mode(conn, args.mode, auth.configured(conn), acc)
            elif args.access_command == "allow":
                access.add_identity(conn, args.login)
            elif args.access_command == "disallow":
                access.remove_identity(conn, access.identity_id(args.login.strip().lower()), acc)
            elif args.access_command == "reset-login":
                auth.reset(conn)
                if acc.mode == "builtin":
                    access.set_mode(conn, "local", False, acc)
                emit("built-in login removed (password, authenticator, recovery codes, sessions)"
                     + ("; access is now This computer only" if acc.mode == "builtin" else ""))
        except access.AccessError as e:
            emit(f"error: {e}", sys.stderr)
            return 2
        acc = access.load(conn)
        emit(f"access: {access.MODE_NAMES[acc.mode]}" + ("" if acc.saved_mode else " (not chosen yet: the default)"))
        if acc.mode == "proxy" or acc.identities:
            emit(f"  trusted header: {acc.header}; public URLs: {', '.join(acc.public_urls) or 'none'}")
            emit(f"  allowed identities: {', '.join(access.mask(u) for u in sorted(acc.identities)) or 'none'}")
        emit(f"  built-in login: {'set up' if auth.configured(conn) else 'not set up'}")
    return 0


def _add_settings_commands(sub) -> None:
    s = sub.add_parser("settings", help="the Settings page's values (read-only; --json for the crons)",
                       epilog=SETTINGS_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_settings)
    a = sub.add_parser("access", help="who can open the app: this computer only, a proxy login, or the built-in login",
                       epilog=ACCESS_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.set_defaults(func=cmd_access, access_command=None)
    asub = a.add_subparsers(dest="access_command")
    s = asub.add_parser("mode", help="choose the access mode")
    s.add_argument("mode", choices=["local", "proxy", "builtin"])
    s = asub.add_parser("allow", help="allow a proxy identity")
    s.add_argument("login")
    s = asub.add_parser("disallow", help="remove a proxy identity")
    s.add_argument("login")
    asub.add_parser("reset-login", help="remove the built-in login and go back to this computer only")
# --- TASK-19 (Batch C): goals, the Alpaca live account ------------------------------------------------------------------

GOALS_EPILOG = """Save up (a whole account, part of one, or entries you log), Pay off (a loan or card, from its balance when the
goal was made) and Monthly savings (income - spending this month, the Overview's cash flow) goals, as on the Goals page.
Goals never change net worth, budgets or any other total.

JSON: {today, month, goals: [{id, kind: save|payoff|monthly, name, target, target_month, track: account|earmark|manual,
        account: {id, name, mask, institution, kind, group, balance, label} | null, earmark, start_balance, progress, pct,
        left, done, short: {amount, covered} | null, missing_account, pace: {growth_90d, per_month, on_pace_month,
        months_to_go, need_per_month, months_left} | null, payoff: {payoff_month, payments_left, method,
        monthly_payment} | null, entries, (monthly) month, income, spending, months}], monthly, cashflow,
       suggestions: [{id, name, mask, institution, balance, label, subtype, vault}], accounts: [{..., earmarked,
       unassigned, whole_goal}], debts, totals: {count, save_count, saving_target, saved, pct}, archived_count}
  progress: a whole account's balance; part of one: the amount set aside (short: the account now holds less than its
  goals set aside, newest first); entries: what you logged; pay off: paid down since the goal was made; monthly: this
  month's income - spending. on_pace_month: from the last 90 days (about a third of it a month)."""


def cmd_goals(args) -> int:
    from . import goals
    return _read(args, lambda conn, today: goals.payload(conn, today), goals.text)


def cmd_alpaca_sync(args) -> int:
    """The Alpaca live account's daily read-only snapshot (the same daily run as revalue). Exit 1 when it failed or the
    account isn't ACTIVE (non-fatal for the daily run); 0 with no key file (nothing to do)."""
    from . import alpaca
    with db.connect() as conn:
        out = alpaca.sync(conn, _today())
    if args.json:
        emit_json(out)
    else:
        emit(f"alpaca-sync: {out['action']}: {out['message']}")
    return 0 if out["ok"] else 1


RULES_EPILOG = """Rules file transactions by merchant name (exact) or name (regex, case-insensitive search). Yours (source user,
priority 200) beat the AI's (100); a transaction you filed yourself is never changed by any rule.

  finance rules [list] [--json]       every rule, newest first: label, category, source, matches now; counts; suggestions
  finance rules suggestions [--json]  merchants you filed by hand 2+ times under one category that no rule covers
  finance rules delete <id>           delete a rule; the transactions it filed stay as they are (human-only: no agent tool)

JSON: {"rules": [{id, field, type, pattern, label, category, source, matches, created_at}], "counts": {total, user, ai},
"suggestions": [{key, merchant, category, times, text}]}. Settings -> Categorization rules edits them."""


def cmd_rules(args) -> int:
    from . import rulebook
    if getattr(args, "rules_action", None) == "delete":
        with db.connect() as conn:
            try:
                out = rulebook.delete(conn, args.id)
            except rulebook.RuleError as e:
                emit(f"error: {e}", sys.stderr)
                return 2
        r, n = out["rule"], len(out["filed"])
        emit(f"rule #{out['deleted']} deleted ({r['label']} -> {r['category']}); the {n} transaction"
             f"{'s' if n != 1 else ''} it filed stay as they are")
        return 0
    if getattr(args, "rules_action", None) == "suggestions":
        return _read(args, lambda conn, today: rulebook.suggestions(conn), rulebook.suggestions_text)
    return _read(args, lambda conn, today: rulebook.payload(conn), rulebook.text)


def _add_rules_commands(sub) -> None:
    """TASK-24: finance rules list|delete|suggestions."""
    s = sub.add_parser("rules", help="categorization rules: list, suggestions, delete",
                       description="Categorization rules.", epilog=RULES_EPILOG,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("--json", action="store_true", help="print the JSON described below")
    s.set_defaults(func=cmd_rules, rules_action="list")
    rs = s.add_subparsers(dest="rules_action")
    x = rs.add_parser("list", help="every rule, newest first (read-only)")
    x.add_argument("--json", action="store_true")
    x = rs.add_parser("suggestions", help="rules you could add (read-only)")
    x.add_argument("--json", action="store_true")
    x = rs.add_parser("delete", help="delete a rule (the transactions it filed stay as they are)")
    x.add_argument("id", type=int)


def _add_task19_commands(sub) -> None:
    s = sub.add_parser("goals", help="goals: progress, what's left, pace (read-only)",
                       description=f"Goals: progress, what's left, pace. {READ_NOTE}", epilog=GOALS_EPILOG,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("--json", action="store_true", help="print the JSON described below")
    s.set_defaults(func=cmd_goals)
    s = sub.add_parser("alpaca-sync", help="the Alpaca live account's daily read-only snapshot (needs its key file)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_alpaca_sync)

    s = sub.add_parser("doctor", help="every integration and what it's missing (offline, read-only; exit 1 if any)",
                       description="One line per integration: `ok NAME`, or `STATUS NAME: what -> README.md#anchor` "
                                   "(STATUS: missing, error, off). Exit 0 when nothing is missing or in error, else 1. "
                                   "Never prints a key's value. See docs/configuration.md.")
    s.add_argument("--json", action="store_true", help="the stable JSON shape (docs/configuration.md)")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("backup", help="write a backup of the database, every credential taken out (as Settings > Backup)",
                       description="A consistent copy of the database (safe while `finance serve` runs) with the bank "
                                   "access tokens, the built-in login and the MCP tokens taken out; 0600. Never the "
                                   "secrets file. See docs/backup-restore.md.")
    s.add_argument("--out", required=True, metavar="PATH",
                   help="the file to write (refused if it exists), or a directory to write "
                        "hermes-finance-backup-YYYY-MM-DD.db into")
    s.set_defaults(func=cmd_backup)
    s = sub.add_parser("restore", help="replace the database with a backup (checked first; the old one is moved aside)",
                       description="Checks the backup (SQLite's integrity check, a hermes-finance schema no newer than "
                                   "this code), then replaces the database with it and runs the migrations. Refuses to "
                                   "replace a database that holds data unless --force; with --force the old one is "
                                   "moved aside (finance.db.<timestamp>.pre-restore), never deleted. Stop `finance "
                                   "serve` first. See docs/backup-restore.md.")
    s.add_argument("path", metavar="PATH", help="the backup file")
    s.add_argument("--force", action="store_true", help="replace a database that holds data (moved aside first)")
    s.set_defaults(func=cmd_restore)


def cmd_doctor(args) -> int:
    from . import doctor
    report = doctor.run()
    if args.json:
        emit_json(report)
    else:
        for line in doctor.lines(report):
            emit(line)
    return 0 if report["ok"] else 1


def cmd_backup(args) -> int:
    from . import backup
    if not config.db_path().exists():
        emit(f"no database at {config.db_path()}: nothing to back up", sys.stderr)
        return 1
    out = Path(args.out).expanduser()
    if out.is_dir():
        out = out / f"hermes-finance-backup-{date.today().isoformat()}.db"
    if out.exists() or out.is_symlink():
        emit(f"{out} already exists: not overwritten (choose another --out)", sys.stderr)
        return 1
    if not out.parent.is_dir():
        emit(f"{out.parent} is not a directory", sys.stderr)
        return 1
    conn = db.connect()
    try:
        backup.make_backup(conn, out)
    finally:
        conn.close()
    emit(f"backup written: {out} ({out.stat().st_size} bytes; bank tokens, the built-in login and MCP tokens taken out)")
    return 0


def cmd_restore(args) -> int:
    from . import backup
    try:
        r = backup.restore(Path(args.path).expanduser(), force=args.force)
    except backup.RestoreError as e:
        emit(f"not restored: {e}", sys.stderr)
        return 1
    emit(f"restored {args.path} into {r.path} (schema v{r.schema_from}"
         + (f", upgraded to v{r.schema_to})" if r.schema_to != r.schema_from else ")"))
    if r.moved_aside:
        emit(f"the database it replaced was moved aside: {r.moved_aside}")
    emit("now:")
    for line in r.redo():
        emit(f"  - {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # everything we create (DB, WAL, backups, exports) is owner-only
    args = build_parser().parse_args(argv)
    redact.install_logging(logging.INFO if args.verbose else logging.WARNING)
    try:
        return args.func(args)
    except config.ConfigError as e:
        emit(f"config error: {e}", sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
