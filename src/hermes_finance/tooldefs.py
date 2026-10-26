"""The finance_* agent tools (TASK-17): one definition each (name, description, JSON schema) and one way to run it,
shared by the two adapters, the Hermes plugin (hermes_plugin/) and the MCP server (mcp_server.py).

Reads: each calls the reads.py function behind its `finance <command> --json`, with the same argument parsing and
defaults, on the same read-only connection (read_connection is cli._read_conn's rule), so a tool answers exactly
what the command prints. No business logic here.

Writes (finance_set_category, finance_add_rule, finance_set_cap, and TASK-21's finance_track_bill): describe() says what
a call would change, in plain words, without changing anything (the plugin shows it on Hermes's approval card; the MCP
server records it as a pending action, which the owner approves in Settings); apply() makes the change, with the CLI's
own functions. A rule is regex by default and is refused when it matches no transaction: an exact rule that matches
nothing (the known pitfall) is never saved. finance_track_bill goes through bills.plan / bills.apply, the dashboard's
Track as a bill sheet's own path, so every way of tracking a bill saves the same row.

finance_sync (TASK-22) is neither: it changes nothing the user entered, so it runs without an approval card (the hook
skips it, and over MCP it runs directly) unless it's asked to refresh (ask the banks for fresh data) on a paid Plaid plan,
where Plaid bills each refresh: approval_needed() then says so, and the call goes through the approval path of the writes
(Hermes's card; an MCP pending action, applied by apply() once the owner approves it).

Every expected failure is a ToolError: a bad argument, a missing or old database, a refused change. Nothing is
changed when one is raised.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from . import api, bills, classify, config, db, goals, reads, redact, report, rulebook, syncnow

DEFAULT_NEXT = "14d"       # finance bills --next
DEFAULT_MONTHS = 6         # finance cashflow --months


class ToolError(Exception):
    """A bad argument or a refused change: the adapters return it as the tool's error (nothing was changed)."""


def today() -> date:
    return date.today()  # tests pin this


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    run: Callable[..., Any]
    command: str | None = None          # a read: the `finance` command whose --json it equals
    required: tuple[str, ...] = ()
    write: bool = False
    action: bool = False                # TASK-22: finance_sync (not a read of a command, not a write)

    def schema(self) -> dict[str, Any]:
        """The JSON schema of the arguments (what both adapters advertise)."""
        return {"type": "object", "properties": self.properties,
                **({"required": list(self.required)} if self.required else {}), "additionalProperties": False}


# --- arguments ------------------------------------------------------------------------------------------------------

def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


PERIOD = {
    "month": _string("one month, YYYY-MM (not with since/until)"),
    "since": _string("from: Nd (the last N days, today included) or YYYY-MM-DD"),
    "until": _string("through YYYY-MM-DD, inclusive (default today)"),
}
FILTERS = {
    **PERIOD,
    "category": _string("category name, case-insensitive; must exist"),
    "merchant": _string("part of the merchant or transaction name"),
    "account": _string("account: last digits, name, or part of the name"),
    "limit": {"type": "integer", "minimum": 1, "maximum": api.MAX_LIMIT,
              "description": f"most rows/groups returned (default {api.DEFAULT_LIMIT})"},
}
JSON_TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "array": (list,),
              "object": (dict,), "null": (type(None),)}
TYPE_WORDS = {"string": "a string", "integer": "an integer", "number": "a number", "boolean": "true or false",
              "null": "null", "array": "a list", "object": "an object"}


def _schema_value(value: Any, spec: dict[str, Any], name: str) -> None:
    """Validate the JSON-schema keywords used by our advertised tool schemas, without optional jsonschema."""
    kinds = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
    if not any(isinstance(value, JSON_TYPES[k]) and not (isinstance(value, bool) and k in ("integer", "number"))
               for k in kinds):
        raise ToolError(f"{name} must be {' or '.join(TYPE_WORDS[k] for k in kinds)}")
    if "enum" in spec and value not in spec["enum"]:
        raise ToolError(f"{name} must be one of {', '.join(spec['enum'])}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ToolError(f"{name} must be finite")
        if "minimum" in spec and value < spec["minimum"]:
            raise ToolError(f"{name} must be at least {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise ToolError(f"{name} must be at most {spec['maximum']}")
    if isinstance(value, list):
        if "minItems" in spec and len(value) < spec["minItems"]:
            raise ToolError(f"{name} must have at least {spec['minItems']} item(s)")
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            raise ToolError(f"{name} must have at most {spec['maxItems']} item(s)")
        if "items" in spec:
            for i, item in enumerate(value):
                _schema_value(item, spec["items"], f"{name}[{i}]")


def check_args(tool: Tool, args: Any, strict_schema: bool = False) -> dict[str, Any]:
    """Check tool arguments. The completion bridge uses strict_schema to enforce every advertised schema keyword;
    established plugin/MCP callers retain their parser-compatible optional-null and business-error behavior."""
    if args is None and not strict_schema:
        args = {}
    if not isinstance(args, dict):
        raise ToolError(f"{tool.name} takes an object of named arguments")
    unknown = sorted(k for k in args if k not in tool.properties)
    if unknown:
        takes = ", ".join(tool.properties) or "no arguments"
        raise ToolError(f"unknown argument {str(unknown[0])[:api.MAX_TEXT]!r} for {tool.name} (it takes: {takes})")
    out: dict[str, Any] = {}
    for key, spec in tool.properties.items():
        kinds = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        value = args.get(key)
        if value is None and not strict_schema:
            if key in tool.required and not (key in args and "null" in kinds):
                raise ToolError(f"{key} is required")
            if key in args and "null" in kinds:
                out[key] = None
            continue
        if key not in args:
            if key in tool.required:
                raise ToolError(f"{key} is required")
            continue
        value = args[key]
        if strict_schema:
            _schema_value(value, spec, key)
        else:
            if not any(isinstance(value, JSON_TYPES[k]) and not (isinstance(value, bool) and k != "boolean")
                       for k in kinds if k != "null"):
                raise ToolError(f"{key} must be {' or '.join(TYPE_WORDS[k] for k in kinds)}")
            if "enum" in spec and value not in spec["enum"]:
                raise ToolError(f"{key} must be one of {', '.join(spec['enum'])}")
        out[key] = value
    return out


def _filters(conn: sqlite3.Connection, a: dict[str, Any], today: date, **kw: Any) -> reads.Filters:
    return reads.parse_filters(conn, today, month=a.get("month"), since=a.get("since"), until=a.get("until"),
                               category=a.get("category"), merchant=a.get("merchant"), account=a.get("account"),
                               limit=a.get("limit", api.DEFAULT_LIMIT), **kw)


# --- reads (the cli.cmd_* builders) ---------------------------------------------------------------------------------

def _spend(conn, a, today):
    return reads.spend(conn, _filters(conn, a, today, spending=True), today, a.get("by"))


def _budget_status(conn, a, today):
    return reads.budget_status(conn, reads.parse_month(a.get("month"), today), today)


def _transactions(conn, a, today):
    return reads.txns(conn, _filters(conn, a, today, default_period="all"), today, a.get("search"),
                      a.get("flagged", False), a.get("uncategorized", False), a.get("tag"))


def _networth(conn, a, today):
    return reads.networth(conn, today, a.get("history", False), a.get("custom", False))


def _bills(conn, a, today):
    return reads.bills(conn, today, reads.parse_days(a.get("next", DEFAULT_NEXT), "--next", 0, reads.MAX_NEXT_DAYS,
                                                     bare=True))


def _cashflow(conn, a, today):
    return reads.cashflow(conn, today, a.get("months", DEFAULT_MONTHS))


def _status(conn, a, today):
    return report.status(conn)


# --- writes: (describe, apply) --------------------------------------------------------------------------------------

def _money(value: float | None) -> str:
    return report.money(value, none="no cap")


def _category(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, name, monthly_cap, is_income, is_transfer FROM categories WHERE name = ? COLLATE NOCASE",
                       (name.strip(),)).fetchone()
    if row is None:
        names = ", ".join(r[0] for r in conn.execute("SELECT name FROM categories ORDER BY name"))
        raise ToolError(f"unknown category {name[:api.MAX_TEXT]!r}; categories: {names}")
    return row


def _text(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise ToolError(f"{name} can't be empty")
    if len(value) > api.MAX_TEXT:
        raise ToolError(f"{name} is too long (at most {api.MAX_TEXT} characters)")
    return value


def _transaction(conn: sqlite3.Connection, tid: str) -> sqlite3.Row:
    row = conn.execute("SELECT transaction_id, date, name, merchant_name, amount, category, category_source "
                       "FROM v_transactions WHERE transaction_id = ?", (tid,)).fetchone()
    if row is None:
        raise ToolError(f"unknown transaction {tid[:api.MAX_TEXT]!r} (use the transaction_id finance_transactions shows)")
    return row


def _set_category_plan(conn: sqlite3.Connection, a: dict[str, Any]) -> tuple[sqlite3.Row, str, str]:
    t = _transaction(conn, _text(a["transaction_id"], "transaction_id"))
    new = _category(conn, a["category"])["name"]
    merchant = (t["merchant_name"] or t["name"] or "").strip() or "(no name)"
    summary = (f"File the transaction {t['date']} · {merchant} · {report.money(t['amount'])} under {new} "
               f"(now {t['category']}, set by {t['category_source']}). It becomes your decision: rules and the AI "
               f"won't change it.")
    return t, new, summary


def _set_category(conn: sqlite3.Connection, a: dict[str, Any], today: date) -> dict[str, Any]:
    t, new, _ = _set_category_plan(conn, a)
    note = _text(a["note"], "note") if a.get("note") is not None else None
    try:
        classify.set_category(conn, t["transaction_id"], new, "user", None, note)
    except classify.ClassifyError as e:
        raise ToolError(str(e)) from None
    after = _transaction(conn, t["transaction_id"])
    return {"transaction_id": t["transaction_id"], "category": after["category"],
            "category_source": after["category_source"], "note": note,
            "previous": {"category": t["category"], "category_source": t["category_source"]}}


FIELD_WORDS = {"merchant_name": "merchant name", "name": "transaction name"}


def _rule_plan(conn: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    """The rule a call would save and what it matches now. A pattern that matches no transaction is refused."""
    field, match = a.get("field", "merchant_name"), a.get("match", "regex")
    pattern = _text(a["pattern"], "pattern")
    category = _category(conn, a["category"])["name"]
    regex = None
    if match == "regex":
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ToolError(f"pattern is not a valid regex ({e}); use match='exact' for a literal name") from None
    rule = classify.Rule(0, field, match, pattern, 0, regex)
    hits = [r for r in conn.execute("SELECT t.transaction_id, t.merchant_name, t.name, c.source FROM transactions t "
                                    "LEFT JOIN classifications c USING (transaction_id) WHERE t.removed = 0")
            if rule.matches(r)]
    how = (f"the regex {pattern!r} (searched anywhere in the {FIELD_WORDS[field]}, case-insensitive)" if regex
           else f"{pattern!r} (the whole {FIELD_WORDS[field]}, case-insensitive)")
    if not hits:
        raise ToolError(f"{how} matches no transaction, so no rule was saved. Look the name up first "
                        f"(finance_transactions with search=...){'' if regex else ', or use the default regex match'}")
    yours = sum(r["source"] == "user" for r in hits)
    summary = (f"Add a rule: file every transaction whose {FIELD_WORDS[field]} matches {how} under {category}, now and "
               f"on every sync. It matches {len(hits)} transaction{'s' if len(hits) != 1 else ''} today"
               + (f"; the {yours} you filed yourself stay as they are" if yours else "") + ".")
    return {"field": field, "match": match, "pattern": pattern, "category": category, "matched": len(hits),
            "yours": yours, "summary": summary}


def _add_rule(conn: sqlite3.Connection, a: dict[str, Any], today: date) -> dict[str, Any]:
    plan = _rule_plan(conn, a)
    try:
        saved = classify.add_rule(conn, plan["field"], plan["pattern"], plan["category"], plan["match"], None, "user")
    except classify.ClassifyError as e:
        raise ToolError(str(e)) from None
    counts = classify.apply(conn)
    return {"rule_id": saved.id, **{k: plan[k] for k in ("field", "match", "pattern", "category", "matched")},
            "refiled": saved.refiled, "set_by_rules": counts["rule"]}


def _cap_plan(conn: sqlite3.Connection, a: dict[str, Any]) -> tuple[sqlite3.Row, float | None, str]:
    row = _category(conn, a["category"])
    if row["is_income"] or row["is_transfer"]:
        raise ToolError(f"{row['name']} is not a spending category; it can't have a cap")
    cap = a["monthly_cap"]
    if cap is not None:   # api.set_cap's rule (a huge int first: math.isfinite can't take it)
        if (isinstance(cap, int) and abs(cap) > api.MAX_CAP) or not (math.isfinite(cap) and 0 <= cap <= api.MAX_CAP):
            raise ToolError(f"monthly_cap must be between 0 and {api.MAX_CAP:,} (or null to clear the cap)")
        cap = round(float(cap), 2)
    was = row["monthly_cap"]
    summary = (f"Clear the monthly budget cap on {row['name']} (now {_money(was)})." if cap is None
               else f"Set the monthly budget cap on {row['name']} to {_money(cap)} (now {_money(was)}).")
    return row, cap, summary


def _set_cap(conn: sqlite3.Connection, a: dict[str, Any], today: date) -> dict[str, Any]:
    row, cap, _ = _cap_plan(conn, a)
    try:
        out = api.set_cap(conn, {"category": row["name"], "monthly_cap": cap})
    except api.ApiError as e:
        raise ToolError(str(e)) from None
    return {**out, "previous": row["monthly_cap"]}


def _bill_plan(conn: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    """TASK-21: bills.plan for the tool's arguments. The merchant must be in the data (exact, or a unique
    case-insensitive match; otherwise refused with the candidates); every and usually are checked there, stop only on a
    bill that is tracked or detected."""
    usually = a.get("usually", bills.UNSET)
    if isinstance(usually, list) and not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in usually):
        raise ToolError("usually must be a number or a list of two numbers [low, high]")
    if a["action"] == "stop" and any(k in a for k in ("every", "varies", "usually")):
        raise ToolError("stop takes only the merchant")
    try:
        return bills.plan(conn, today(), a["action"], merchant=a["merchant"], every=a.get("every"),
                          varies=a.get("varies"), usually=usually)
    except bills.BillError as e:
        raise ToolError(str(e)) from None


def _track_bill(conn: sqlite3.Connection, a: dict[str, Any], day: date) -> dict[str, Any]:
    try:
        out = bills.apply(conn, _bill_plan(conn, a), day)
    except bills.BillError as e:
        raise ToolError(str(e)) from None
    return {k: out[k] for k in ("action", "merchant", "key", "summary")} | {
        "bill": {k: out["bill"][k] for k in reads.BILL_FIELDS} if out["bill"] else None}


# --- finance_sync (TASK-22) -------------------------------------------------------------------------------------------

def _sync(conn: sqlite3.Connection, a: dict[str, Any], today: date) -> dict[str, Any]:
    try:
        return syncnow.run(conn, syncnow.make_client, refresh=bool(a.get("refresh")))
    except syncnow.SyncRefused as e:
        raise ToolError(str(e)) from None
    except config.ConfigError as e:
        if config.hermes_credentials():   # names the hermes config set commands (never a value)
            raise ToolError(str(e)) from None
        raise ToolError("Plaid keys are missing (Settings \u2192 Connections \u2192 Replace keys); nothing was synced") from None


def _sync_approval(conn: sqlite3.Connection, a: dict[str, Any]) -> str | None:
    if not a.get("refresh"):
        return None
    policy = syncnow.refresh_policy(conn)
    if not policy["allowed"]:
        raise ToolError(policy["reason"])
    n = len(syncnow.plan(conn, refresh=True)["refresh"])
    if not policy["billed"] or not n:
        return None
    return f"Ask your banks for fresh data (Plaid bills each refresh on your plan: {n} connection{'s' if n != 1 else ''})"


PLANS: dict[str, Callable[[sqlite3.Connection, dict[str, Any]], str]] = {
    "finance_set_category": lambda conn, a: _set_category_plan(conn, a)[2],
    "finance_add_rule": lambda conn, a: _rule_plan(conn, a)["summary"],
    "finance_set_cap": lambda conn, a: _cap_plan(conn, a)[2],
    "finance_track_bill": lambda conn, a: _bill_plan(conn, a)["summary"],
}

# --- the tools --------------------------------------------------------------------------------------------------------

TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("finance_spend",
         "Spending total (income and transfers excluded, refunds netted) for a period, with the previous equal period; "
         "optionally grouped. Default: this month. Spend is positive, refunds negative.",
         {**FILTERS, "by": _string("group the total", enum=list(reads.SPEND_GROUPS))}, _spend, "spend"),
    Tool("finance_budget_status",
         "Each monthly budget cap and group budget: spent, bills still due, left, % used, status (ok/warn/over), pace "
         "projection; uncapped spend and totals. A cap in a group budget is counted: false; group_rest: a capped group's "
         "uncapped spend. Default: this month.",
         {"month": _string("YYYY-MM (default this month)")}, _budget_status, "budget-status"),
    Tool("finance_transactions",
         "Transactions, newest first, with category and who set it (user/rule/ai/plaid/default). Filters narrow. "
         f"Default: all history, {api.DEFAULT_LIMIT} rows. Money out is positive, money in negative.",
         {**FILTERS,
          "search": _string("text in the name, merchant, category, account, bank, AI note, the user's note or a tag, "
                            "or the amount (465.10)"),
          "flagged": {"type": "boolean", "description": "only rows the AI flagged and the user hasn't decided"},
          "uncategorized": {"type": "boolean", "description": "only rows nobody has decided a category for"},
          "tag": _string("only rows with this tag (case-insensitive)")},
         _transactions, "txns"),
    Tool("finance_networth",
         "Net worth: assets, debts and net, grouped as on the Accounts page (connected and manual accounts, with "
         "vehicle/property values).",
         {"history": {"type": "boolean", "description": "also the daily net worth points"},
          "custom": {"type": "boolean", "description": "only the accounts the user chose to count (Custom)"}},
         _networth, "networth"),
    Tool("finance_bills",
         "Recurring bills and subscriptions expected in the next N days (overdue ones included): amount (an estimate "
         "and usual range when it changes), cadence, status, detected or tracked, last paid from, outlier; plus "
         "monthly totals.",
         {"next": _string(f"how far ahead: Nd, 0d-{reads.MAX_NEXT_DAYS}d (default {DEFAULT_NEXT})")}, _bills, "bills"),
    Tool("finance_loans", "Loans: remaining balance, APR, monthly payment, payoff estimate, last matched payment.",
         {}, lambda conn, a, today: reads.loans(conn, today), "loans"),
    Tool("finance_holdings",
         "Investment accounts and holdings: value, quantity, price, % of the portfolio; portfolio changes.",
         {}, lambda conn, a, today: reads.holdings(conn, today), "holdings"),
    Tool("finance_cashflow", "Income vs spending per month, oldest first, ending with this month (income positive).",
         {"months": {"type": "integer", "minimum": 1, "maximum": reads.MAX_MONTHS,
                     "description": f"how many months (default {DEFAULT_MONTHS})"}}, _cashflow, "cashflow"),
    Tool("finance_status",
         "Connected banks: status, errors, last sync, accounts with balances, bank connection slots used.",
         {}, _status, "status"),
    Tool("finance_goals",
         "The user's goals: Save up (an account, part of one, or logged entries), Pay off (a loan or card) and Monthly "
         "savings (income minus spending vs a target): progress, % and what's left, pace. Goals never change any total.",
         {}, lambda conn, a, today: goals.payload(conn, today), "goals"),
    Tool("finance_rules",
         "Categorization rules, newest first: what each matches (label), its category, who made it (user/ai), how "
         "many transactions it matches now; counts; and suggested rules (merchants the user filed by hand 2+ times). "
         "Read-only.",
         {}, lambda conn, a, today: rulebook.payload(conn), "rules"),   # TASK-24 (deleting a rule is human-only)
    Tool("finance_sync",
         "Sync now: pull what the banks already sent Plaid (new transactions, balances) and say what changed, with "
         "data_age. refresh=true first asks the banks for fresh data, if Settings allows it; on a paid Plaid plan that "
         "needs the user's approval.",
         {"refresh": {"type": "boolean", "description": "first ask the banks for fresh data (Plaid's refresh; default "
                                                        "false). New items land a few minutes later: sync again then"}},
         _sync, action=True),
    Tool("finance_set_category",
         "File one transaction under a category, as the user's decision (rules and the AI never change it). Needs "
         "the user's approval.",
         {"transaction_id": _string("the transaction_id from finance_transactions"),
          "category": _string("category name, case-insensitive; must exist"),
          "note": _string("optional note kept with the decision")},
         _set_category, required=("transaction_id", "category"), write=True),
    Tool("finance_add_rule",
         "Always file transactions whose merchant name (or name) matches a pattern under a category; re-files the "
         "matching ones now. Regex (case-insensitive search) by default; a pattern matching no transaction is "
         "refused. Needs the user's approval.",
         {"pattern": _string("regex searched in the name (or the whole name, with match=exact)"),
          "category": _string("category name, case-insensitive; must exist"),
          "field": _string("what to match (default merchant_name)", enum=["merchant_name", "name"]),
          "match": _string("regex (default) or exact (the whole name, case-insensitive)", enum=["regex", "exact"])},
         _add_rule, required=("pattern", "category"), write=True),
    Tool("finance_set_cap",
         "Set a spending category's monthly budget cap, or clear it with null. Needs the user's approval.",
         {"category": _string("spending category name, case-insensitive"),
          "monthly_cap": {"type": ["number", "null"], "minimum": 0, "maximum": api.MAX_CAP,
                          "description": "the cap in dollars, or null to clear it"}},
         _set_cap, required=("category", "monthly_cap"), write=True),
    Tool("finance_track_bill",
         "Track a merchant as a bill by hand (track), change a bill's schedule or amount (update), or stop tracking one "
         "(stop: a detected one is no longer listed). A bill is the merchant: its charges on any account count. Needs "
         "the user's approval.",
         {"action": _string("track, update or stop", enum=list(bills.ACTIONS)),
          "merchant": _string("the merchant's name as finance_transactions shows it (exact, or a unique "
                              "case-insensitive match)"),
          "every": _string("the schedule: 2w, 1m, 2m, 3m, 6m or 1y (also 1w); default: the detected one, else 1m",
                           enum=list(bills.EVERY)),
          "varies": {"type": "boolean", "description": "true: the amount changes each time (a utility); default: from "
                                                       "the category"},
          "usually": {"type": ["number", "array", "null"], "items": {"type": "number"}, "minItems": 1, "maxItems": 2,
                      "description": "what a bill that changes usually is: an amount, or [low, high] (null clears it)"}},
         _track_bill, required=("action", "merchant"), write=True),
)}
READS = tuple(n for n, t in TOOLS.items() if not t.write and not t.action)
WRITES = tuple(n for n, t in TOOLS.items() if t.write)
ACTIONS = tuple(n for n, t in TOOLS.items() if t.action)
UNGATED = tuple(n for n, t in TOOLS.items() if not t.write)   # what MCP lists without allow_writes: READS + finance_sync


# --- running them ---------------------------------------------------------------------------------------------------

def tool(name: str) -> Tool:
    found = TOOLS.get(name)
    if found is None:
        raise ToolError(f"unknown tool {str(name)[:api.MAX_TEXT]!r}")
    return found


def read_connection(path: Path, timeout: float = 30) -> sqlite3.Connection:
    """The read commands' connection (cli._read_conn): read-only, and only on a database at the current schema.
    `timeout`: db.connect's (seconds a statement waits for another connection's lock, the schema check included)."""
    try:
        conn = db.connect_readonly(path, timeout)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except (FileNotFoundError, sqlite3.Error) as e:
        raise ToolError(str(e)) from None
    if version < db.SCHEMA_VERSION:
        conn.close()
        raise ToolError(f"the database is at schema v{version}, these commands need v{db.SCHEMA_VERSION}; run "
                        "`finance sync` once to upgrade it")
    return conn


def write_connection(path: Path, timeout: float = 30) -> sqlite3.Connection:
    """A read-write connection, only on an existing database at the current schema (never creates or upgrades one).
    `timeout`: read_connection's."""
    read_connection(path, timeout).close()
    return db.connect(path, timeout)


def run_read(name: str, args: Any, path: Path, day: date) -> dict[str, Any]:
    """A read tool's payload: exactly `finance <command> --json` for the same arguments."""
    t = tool(name)
    if t.write or t.action:
        raise ToolError(f"{name} is not a read tool")
    a = check_args(t, args)
    conn = read_connection(path)
    try:
        return t.run(conn, a, day)
    except reads.ReadError as e:
        raise ToolError(str(e)) from None
    except sqlite3.Error as e:
        raise ToolError(str(e)) from None
    finally:
        conn.close()


def approval_needed(name: str, args: Any, path: Path) -> tuple[dict[str, Any], str | None]:
    """TASK-22, finance_sync: (the checked arguments, what the approval card says, or None when it runs without one),
    read-only. ToolError when it can't be done (refresh while Settings doesn't allow it)."""
    t = tool(name)
    if not t.action:
        raise ToolError(f"{name} is not finance_sync")
    a = check_args(t, args)
    conn = read_connection(path)
    try:
        return a, _sync_approval(conn, a)
    finally:
        conn.close()


def run_action(name: str, args: Any, path: Path, day: date) -> dict[str, Any]:
    """Run finance_sync (without a card, or after the approval approval_needed asked for)."""
    t = tool(name)
    if not t.action:
        raise ToolError(f"{name} is not finance_sync")
    a = check_args(t, args)
    conn = write_connection(path)
    try:
        return t.run(conn, a, day)
    finally:
        conn.close()


def describe(name: str, args: Any, path: Path) -> tuple[dict[str, Any], str]:
    """(the checked arguments, what the write would change in plain words), read-only. ToolError when it can't be done."""
    t = tool(name)
    if not t.write:
        raise ToolError(f"{name} is not a write tool")
    a = check_args(t, args)
    conn = read_connection(path)
    try:
        return a, PLANS[name](conn, a)
    finally:
        conn.close()


def apply(name: str, args: Any, path: Path, day: date) -> dict[str, Any]:
    """Make a write tool's change (the plugin, after the user approved it; Settings, approving an MCP pending action,
    finance_sync's too). ToolError: nothing was changed."""
    t = tool(name)
    if t.action:
        return run_action(name, args, path, day)
    if not t.write:
        raise ToolError(f"{name} is not a write tool")
    a = check_args(t, args)
    conn = write_connection(path)
    try:
        return t.run(conn, a, day)
    finally:
        conn.close()


def render(payload: Any) -> str:
    """A tool's answer: compact JSON, scrubbed like everything the CLI prints."""
    return redact.scrub(json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":")))


def instructions() -> str:
    """What the tools' users should know up front (the MCP server's instructions)."""
    return ("hermes-finance: the owner's own money data from a local database, read-only. Amounts use the app's sign: "
            "spend and money out are positive, refunds and income negative. Use a category's exact name "
            "(finance_spend with by=category lists them); never guess one. Dates are local. "
            "In human-facing text, use $ and thousands separators. Round to cents, omit cents for whole dollars, "
            "and keep two decimals otherwise. Put an ASCII minus before $ for negatives and suppress it for rounded "
            "zero. Keep numeric tool arguments and JSON fields numeric.")
