#!/usr/bin/env python3
"""Fail loudly if the built demo carries anything real. The last step of scripts/build_demo.py; run it on any dist/.

    uv run python scripts/demo_canary.py dist/ --private PATH [--private PATH ...] [--private-amounts PATH]
    HERMES_FINANCE_TEST_PRIVATE=word,word uv run python scripts/demo_canary.py dist/

Exit 0: nothing found. 1: a hit (each printed as file:line, the rule, the context with the match masked). 2: usage or
input error. 3: INDETERMINATE: no private token list was given (the owner's own names, handles, employer and ids can't be
checked without it); --no-private runs everything else and still exits 3.

No owner value is written in this file. They come from outside the repo (A), from the repo's own personal-token list
(B), from this machine (C), or from the generator itself (E, F), and a hit is reported with the token masked.

 A. private tokens (names, handles, employer, Discord / cron ids): --private PATH (one per line, or a Python file with
    DEFAULT_TOKENS = [...], such as the release identifier sweep) and HERMES_FINANCE_TEST_PRIVATE (comma-separated).
    Case-insensitive, in every file, binaries included.
 B. the repo's private word list (scripts/dehardcode.py: HERMES_FINANCE_TEST_PRIVATE and HERMES_FINANCE_PRIVATE_FILE /
    .private-words, both untracked), read at run time. Case-insensitive, every file.
 C. this machine: the login, the home directory, the full name from pwd, the host name. Every file.
 D. shapes, in every text file: e-mail addresses (example.com / .org / .net allowed), VINs, US street addresses (an
    Example / Sample / Demo street or a landmark of scripts/demo_assets.json allowed), a ZIP after a state code (00000
    or a listed landmark's allowed), tailnet host names (*.ts.net; the
    placeholder tailnet label "tailnet" allowed), Tailscale 100.64/10 addresses, finance.db paths and SQLite files, Plaid
    access / public tokens, unmasked MCP tokens, API keys, 17-20 digit (Discord-sized) ids, /Users/ and /home/ paths.
    PNGs: no text chunk at all (metadata).
 E. the generator's own values: tests/gate_dbs.py's `features` DB is built again here (a temp dir, network off, the
    build's pinned clock) and every bank, account name, mask, merchant, statement line, payee, employer descriptor,
    manual account, address, street, VIN, ZIP, ticker, match pattern, picture credit and proxy host it writes must be
    absent from the files the build generated (fixtures, index.html, the shim, export.csv). A few generic statement
    lines it keeps on purpose are listed (GENERIC) and printed. A JSON field name (lowercase: target, kind) and a URL
    written in src/hermes_finance itself (a provider's API) are the app's text, not data: E skips them, A-D don't.
    Files under static/ must be byte-identical to src/hermes_finance/static (the reviewed source: A-D, not E).
 F. amounts: the generator's fixed amounts (the ones that are the same with another random seed; distinctive ones
    only: >= $100 with cents, or >= $1,000 and not a multiple of $50) and every amount in --private-amounts PATH (one
    per line, e.g. made on the owner's machine from the live DB), as JSON numbers and as $-text, in the generated files.
"""

from __future__ import annotations

import argparse
import ast
import getpass
import json
import os
import pwd
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "hermes_finance" / "static"
TEXT_EXT = {".html", ".js", ".css", ".json", ".txt", ".md", ".csv", ".svg", ".webmanifest", ".xml", ""}
# statement lines the generator writes that name nobody (kept as they are by the build): allowed, and printed
GENERIC = {"Mobile Deposit", "ONLINE PAYMENT, THANK YOU"}
KEEP = {"Alpaca"}   # the app's own Alpaca integration (alpaca.py), not a person's bank
ALLOWED_EMAIL = re.compile(r"@example\.(com|org|net)$", re.I)
# the demo's properties are famous public / commercial Los Angeles landmarks (never a home), listed in
# scripts/demo_assets.json: their published street addresses and ZIPs are allowed, exactly as listed, nothing else
LANDMARKS = [p["address"] for p in json.loads((ROOT / "scripts" / "demo_assets.json").read_text())["properties"]]
LANDMARK_STREETS = {a.split(",")[0] for a in LANDMARKS}
LANDMARK_ZIPS = {m.group(0) for a in LANDMARKS for m in re.finditer(r"\b[A-Z]{2}\s+\d{5}\b", a)}
SHAPES = [   # (rule, regex, allowed(match) -> bool)
    ("e-mail address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
     lambda m: bool(ALLOWED_EMAIL.search(m.group(0)))),
    ("VIN", re.compile(r"(?<![A-Za-z0-9])(?=[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9]))(?=(?:[^0-9]*[0-9]){3})(?=(?:[^A-Z]*[A-Z]){3})"
                       r"[A-HJ-NPR-Z0-9]{17}"), lambda m: False),
    ("street address", re.compile(r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][a-z]+\s+){1,3}(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|"
                                  r"Blvd|Boulevard|Ln|Lane|Way|Ct|Court|Pl|Place|Pkwy|Parkway|Hwy|Highway|Ter|Terrace|Cir|Circle|"
                                  r"Trl|Trail)\b\.?"),
     lambda m: bool(re.search(r"\b(Example|Sample|Demo)\b", m.group(0)))
     or m.group(0).rstrip(".") in LANDMARK_STREETS),
    ("ZIP after a state", re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"),
     lambda m: m.group(0).endswith("00000") or m.group(0) in LANDMARK_ZIPS),
    ("tailnet host name", re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.ts\.net\b", re.I),
     lambda m: m.group(0).lower().split(".")[-3] == "tailnet"),
    ("Tailscale address", re.compile(r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?![\d.])"), lambda m: False),
    ("finance.db path or dump", re.compile(r"finance\.db\b|tokens\.backup\.jsonl|SQLite format 3"), lambda m: False),
    ("Plaid token", re.compile(r"\b(?:access|public|link)-(?:production|sandbox|development)-[0-9a-f-]{8,}", re.I), lambda m: False),
    ("unmasked MCP token", re.compile(r"\bhfm_[A-Za-z0-9_-]{16,}"), lambda m: False),
    ("API key", re.compile(r"\b(?:sk-(?:ant-|or-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,})"),
     lambda m: False),
    ("Discord-sized id", re.compile(r"(?<![\d.])\d{17,20}(?![\d.])"), lambda m: False),
    ("machine path", re.compile(r"/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+|[A-Z]:\\\\Users\\\\"), lambda m: False),
]


class Hits:
    def __init__(self):
        self.rows: list[tuple[str, str, int, str]] = []

    def add(self, rule: str, path: str, text: str | None, start: int, end: int, label: str) -> None:
        line = text.count("\n", 0, start) + 1 if text is not None else 0
        ctx = ""
        if text is not None:
            a, b = max(0, start - 30), min(len(text), end + 30)
            ctx = (text[a:start] + f"«{label}»" + text[end:b]).replace("\n", " ")
        self.rows.append((rule, path, line, ctx))


def mask(token: str) -> str:
    """How a hit names what it found: its length only (no digest: a short name hashes back by guessing)."""
    return f"{len(token)} chars"


def token_rx(token: str) -> str:
    """A private token, anywhere; a token of digits only (an account mask, a ZIP, an id) not inside a longer number: the
    "1234" of "51234", "$1,234" or "1234.36" is another number, while "x1234", "••1234" and "id 1234" still match."""
    if token.isdigit():
        return rf"(?<![\d.,]){re.escape(token)}(?![\d]|[.,]\d)"
    return re.escape(token)


def load_private(paths: list[Path]) -> list[str]:
    tokens: list[str] = []
    for p in paths:
        text = p.read_text(errors="replace")
        if p.suffix == ".py":   # e.g. the release identifier sweep: its DEFAULT_TOKENS list, read without running it
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DEFAULT_TOKENS" for t in node.targets):
                    tokens += [ast.literal_eval(e) for e in node.value.elts]
        else:
            tokens += [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    tokens += [t.strip() for t in os.environ.get("HERMES_FINANCE_TEST_PRIVATE", "").split(",") if t.strip()]
    return list(dict.fromkeys(t for t in tokens if len(t) >= 3))


def repo_forbidden() -> list[str]:
    """The repo's own private word list: scripts/dehardcode.py's untracked sources (HERMES_FINANCE_TEST_PRIVATE and
    HERMES_FINANCE_PRIVATE_FILE / .private-words), the same list every other privacy check reads."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import dehardcode                                                   # noqa: PLC0415
    return sorted(w for w in dehardcode.private_words(include_own=False) if len(w) >= 3)


def machine_markers() -> list[tuple[str, str]]:
    out = []
    login = getpass.getuser()
    out.append(("this machine's login", login))
    home = str(Path.home())
    out.append(("this machine's home directory", home))
    try:
        gecos = pwd.getpwnam(login).pw_gecos.split(",")[0]
    except KeyError:
        gecos = ""
    if gecos.strip():
        out.append(("this machine's full name", gecos.strip()))
    for part in re.split(r"\s+", gecos):
        if len(part) >= 3:
            out.append(("this machine's full name", part))
    host = socket.gethostname().split(".")[0]
    if len(host) >= 3 and host.lower() not in ("localhost",):
        out.append(("this machine's host name", host))
    return [(k, v) for k, v in out if v]


def generator_values(today: date) -> tuple[list[tuple[str, str]], set[int]]:
    """Build the generator's DB again (as build_demo does, before any renaming) and read what it writes. Returns
    [(kind, value)] and the distinctive fixed amounts in cents."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_demo as b
    from datetime import datetime, timezone
    tmp = Path(tempfile.mkdtemp(prefix="hf-demo-canary-"))
    try:
        b.safe_env(tmp)
        b.no_network()
        sys.path.insert(0, str(ROOT / "src"))
        sys.path.insert(0, str(ROOT / "tests"))
        import demo_data
        import gate_dbs  # noqa: F401
        b.freeze_clock(datetime(today.year, today.month, today.day, 12, tzinfo=timezone.utc))
        b.offline_app()
        runs = {}
        for seed in (4, 5):   # demo_data's own seed, and another: what stays the same is a fixed (written-in) amount
            d = tmp / f"seed{seed}"
            d.mkdir()
            build = demo_data.build
            demo_data.build = lambda conn, today=None, seed=seed, _b=build: _b(conn, today, seed)
            try:
                b.generate(d / "finance.db", today)
            finally:
                demo_data.build = build
            runs[seed] = sqlite3.connect(d / "finance.db")
        c = runs[4]
        vals: list[tuple[str, str]] = []

        def q(kind, sql):
            for row in c.execute(sql):
                for v in row:
                    if isinstance(v, str) and v.strip():
                        vals.append((kind, v.strip()))

        q("bank", "SELECT institution_name FROM items")
        q("bank", "SELECT name FROM institutions")
        q("account name", "SELECT name, official_name FROM accounts")
        q("mask", "SELECT mask FROM accounts")
        q("mask", "SELECT account_mask FROM alpaca_snapshots")
        q("merchant", "SELECT DISTINCT merchant_name FROM transactions")
        q("statement line", "SELECT DISTINCT name FROM transactions")
        q("manual account", "SELECT name, institution, vehicle_make, vehicle_model, match_pattern FROM manual_accounts")
        q("address", "SELECT address FROM manual_accounts")
        q("VIN", "SELECT vin FROM manual_accounts UNION SELECT vin FROM vin_decodes")
        q("ZIP", "SELECT zip_code FROM manual_accounts")
        q("bill", "SELECT merchant FROM tracked_bills")
        q("rule", "SELECT pattern FROM rules")
        q("ticker", "SELECT ticker_symbol, security_name FROM holdings WHERE security_type <> 'cash'")
        q("picture credit", "SELECT artist, credit_url FROM asset_images")
        for (raw,) in c.execute("SELECT details_json FROM asset_valuations WHERE details_json IS NOT NULL"):
            d = json.loads(raw)
            for k in ("name", "url", "photo_url", "zip"):
                if isinstance(d.get(k), str):
                    vals.append(("listing", d[k]))
        for (raw,) in c.execute("SELECT specs_json FROM vin_decodes"):
            for group in json.loads(raw or "{}").values():
                if isinstance(group, dict) and isinstance(group.get("PlantCity"), str):
                    vals.append(("VIN plant", group["PlantCity"]))
        for (raw,) in c.execute("SELECT value FROM settings WHERE key IN ('access.public_urls', 'access.last_proxy')"):
            for host in re.findall(r"https?://([^/:\"]+)|\"host\": \"([^\"]+)\"", raw):
                vals += [("proxy host", h) for h in host if h]
        for (addr,) in c.execute("SELECT address FROM manual_accounts WHERE address IS NOT NULL"):
            street = addr.split(",")[0]
            vals += [("street", street), ("street", street.split("#")[0].strip())]
        for (name,) in c.execute("SELECT DISTINCT name FROM transactions WHERE pfc_detailed = 'INCOME_WAGES'"):
            m = re.match(r"^.*?\b\d{2}/\d{2}\s+(.+)$", name or "")
            tail = m.group(1) if m else name
            vals.append(("employer descriptor", tail))
            vals += [("employer descriptor", w) for w in tail.split() if len(w) >= 4]
        for (name,) in c.execute("SELECT DISTINCT name FROM transactions WHERE merchant_name IS NULL"):
            m = re.match(r"^(zelle|venmo|cash app|paypal)( payment)? (to|from) (.+)$", name or "", re.I)
            if m:
                vals.append(("person", m.group(4)))
        # fixed amounts: written in (the same under both seeds) or a balance / value / payment (all written in)
        def cents(sql, conn):
            return {round(v * 100) for (v,) in conn.execute(sql) if isinstance(v, (int, float))}
        fixed = cents("SELECT amount FROM transactions", runs[4]) & cents("SELECT amount FROM transactions", runs[5])
        for sql in ("SELECT current_balance FROM accounts", "SELECT available_balance FROM accounts",
                    "SELECT balance FROM manual_accounts", "SELECT original_amount FROM manual_accounts",
                    "SELECT monthly_payment FROM manual_accounts", "SELECT credit_limit FROM manual_accounts",
                    "SELECT value FROM asset_valuations", "SELECT institution_value FROM holdings",
                    "SELECT institution_price FROM holdings", "SELECT value FROM holding_snapshots",
                    "SELECT target FROM goals", "SELECT earmark FROM goals", "SELECT amount FROM goal_entries",
                    "SELECT interest_rate FROM manual_accounts", "SELECT minimum_payment_amount FROM liabilities"):
            fixed |= cents(sql, runs[4])
        distinctive = {c_ for c_ in fixed if (abs(c_) >= 10000 and c_ % 100) or (abs(c_) >= 100000 and c_ % 5000)}
        for conn in runs.values():
            conn.close()
        seen, out = set(), []
        for kind, v in vals:
            if v in GENERIC or v in KEEP or (kind, v) in seen or len(v) < 2:
                continue
            seen.add((kind, v))
            out.append((kind, v))
        return out, distinctive
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


SCHEMA_KEY = re.compile(r"[a-z][a-z0-9_]*")   # a payload's field name (target, kind): the app's, not the data's


def product_urls() -> set[str]:
    """The URLs written in the app's own source (a provider's API, Carfax's page): product text, not data (E skips a
    value that is exactly one of these; A-D still apply)."""
    out = set()
    for p in (ROOT / "src" / "hermes_finance").glob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(("http://", "https://")):
                out.add(node.value)
    return out


def _strings(x, skip=frozenset()):
    if isinstance(x, str):
        if x not in skip:
            yield x
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v, skip)
    elif isinstance(x, dict):
        for k, v in x.items():
            if not SCHEMA_KEY.fullmatch(k):
                yield k
            yield from _strings(v, skip)


def amount_res(cents: int) -> list[re.Pattern]:
    whole, frac = divmod(abs(cents), 100)
    w = f"{whole}"
    wc = f"{whole:,}".replace(",", ",?")
    if frac:
        dec = f"\\.{frac:02d}" if frac % 10 else f"\\.{frac // 10}0?"
        return [re.compile(rf"(?<![\d.,]){wc}{dec}(?!\d)")]
    return [re.compile(rf"(?<![\d.,]){wc}(?:\.0+)?(?![.\d])")] if len(w) >= 4 else []


def fixed_amounts_shown(dist: Path, today: date) -> list[tuple[str, int]]:
    """Rule F's generator half alone: (file, cents) for each distinctive generator fixed amount a generated file shows.
    A sum of scaled amounts can land on one by chance; build_demo.py checks with this and builds the variant again with
    another salt rather than publish it (the full canary still runs F, unchanged)."""
    _, fixed_cents = generator_values(today)
    compiled = [(c_, r) for c_ in sorted(fixed_cents) for r in amount_res(c_)]
    shown = []
    for p in sorted(dist.rglob("*")):
        rel = p.relative_to(dist).as_posix()
        if p.is_file() and not rel.startswith("static/") and p.suffix in (".json", ".csv", ".html", ".js", ".css"):
            text = p.read_text(errors="replace")
            shown += [(rel, c_) for c_, r in compiled if r.search(text)]
    return shown


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dist", type=Path)
    ap.add_argument("--private", type=Path, action="append", default=[], help="private token file (outside the repo)")
    ap.add_argument("--private-amounts", type=Path, default=None, help="amounts from the owner's real data, one per line")
    ap.add_argument("--no-private", action="store_true", help="run without a private token list (still exits 3)")
    ap.add_argument("--today", type=date.fromisoformat, default=None, help="the build's day (default: read from the demo)")
    ap.add_argument('--synthetic-only', action='store_true', help='public synthetic checks only; NOT publication approval')
    ap.add_argument('--fixed-amounts', action='store_true',
                    help="only rule F's generator fixed amounts, counted, not shown (build_demo.py's salt check)")
    args = ap.parse_args(argv)
    if args.fixed_amounts:
        meta = json.loads((args.dist / "demo/data/core.json").read_text())["meta"]
        shown = fixed_amounts_shown(args.dist.resolve(), args.today or date.fromisoformat(meta["today"]))
        print(f"demo_canary: {len(shown)} generator fixed amount(s) shown in {len({f for f, _ in shown})} file(s)")
        return 1 if shown else 0
    if args.synthetic_only:
        if args.private or args.private_amounts or args.no_private:
            ap.error('--synthetic-only cannot be combined with private scan options')
        os.environ['HERMES_FINANCE_PRIVATE_FILE'] = '/nonexistent'
        os.environ.pop('HERMES_FINANCE_TEST_PRIVATE', None)
    dist = args.dist.resolve()
    sys.path.insert(0, str(ROOT / "scripts"))
    from demo_combine import data_dir                                     # noqa: PLC0415  (a combined weekly dist too)
    if not (dist / "index.html").is_file() or not (data_dir(dist) / "core.json").is_file():
        print(f"demo_canary: {args.dist} is not a built demo (no index.html / demo/data/core.json)")
        return 2
    for p in args.private + ([args.private_amounts] if args.private_amounts else []):
        if not p.is_file():
            print(f"demo_canary: {p} not found")
            return 2
    meta = json.loads((data_dir(dist) / "core.json").read_text())["meta"]
    today = args.today or date.fromisoformat(meta["today"])
    private = load_private(args.private)
    if not private and not args.no_private and not args.synthetic_only:
        print("demo_canary: INDETERMINATE: no private token list (--private PATH or HERMES_FINANCE_TEST_PRIVATE); "
              "pass --no-private to run the rest anyway")
        return 3
    forbidden = repo_forbidden()
    markers = machine_markers()
    gen, fixed_cents = generator_values(today)
    # The fixtures and their generators are synthetic, so holding a generator value is no evidence of the owner: rule
    # E bites on private values only. An owner value the rename pass misses still fails (it is a private word or
    # matches an identity shape); the fixtures' own invented names ("Grocery", "Acme", "Sunrise") are allowed to
    # appear, which is what the demo is supposed to show.
    sys.path.insert(0, str(ROOT / "scripts"))
    import dehardcode                                                     # noqa: PLC0415
    matcher = dehardcode.Matcher(dehardcode.private_words())
    gen = [(k, v) for k, v in gen if matcher.scan("", v)]
    private_cents = set()
    if args.private_amounts:
        for line in args.private_amounts.read_text().split():
            try:
                private_cents.add(round(float(line.replace(",", "").replace("$", "")) * 100))
            except ValueError:
                pass

    hits = Hits()
    files = sorted(p for p in dist.rglob("*") if p.is_file())
    static_ok = 0
    generated: list[Path] = []
    for p in files:
        rel = p.relative_to(dist).as_posix()
        if rel.startswith("static/"):
            src = STATIC / rel[len("static/"):]
            if src.is_file() and src.read_bytes() == p.read_bytes():
                static_ok += 1
                continue
            hits.add("static file differs from src/hermes_finance/static", rel, None, 0, 0, "")
        generated.append(p)
    for p in files:
        rel = p.relative_to(dist).as_posix()
        raw = p.read_bytes()
        low = raw.decode("latin-1").lower()
        is_text = p.suffix in TEXT_EXT
        text = raw.decode("utf-8", "replace") if is_text else None
        # (a text file is searched as text, so a hit's context lines up with it; a binary one byte for byte, no context)
        hay, ctx = (text, text) if text is not None else (low, None)
        for i, tok in enumerate(private, 1):
            for m in re.finditer(token_rx(tok), hay, re.I):
                hits.add("A private token", rel, ctx, m.start(), m.end(), f"private #{i} ({mask(tok)})")
        for i, tok in enumerate(forbidden, 1):
            for m in re.finditer(token_rx(tok), hay, re.I):
                hits.add("B repo personal-token list", rel, ctx, m.start(), m.end(), f"FORBIDDEN #{i} ({mask(tok)})")
        for kind, tok in markers:   # at word edges: a short name part must not match inside other words
            for m in re.finditer(f"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", hay, re.I):
                hits.add(f"C {kind}", rel, ctx, m.start(), m.end(), mask(tok))
        if text is not None and "\ufffd" in text:   # bytes that aren't UTF-8 in a text file: their latin-1 view too
            for i, tok in enumerate(private + forbidden, 1):
                if re.search(re.escape(tok), low, re.I):
                    hits.add("A/B token in a text file's non-UTF-8 bytes", rel, None, 0, 0, "")
        if p.suffix == ".png":
            for chunk in (b"tEXt", b"iTXt", b"zTXt", b"eXIf"):
                if chunk in raw:
                    hits.add(f"D PNG metadata chunk {chunk.decode()}", rel, None, 0, 0, "")
        if raw.startswith(b"SQLite format 3"):
            hits.add("D finance.db path or dump", rel, None, 0, 0, "")
        if text is None:
            continue
        for rule, rx, allowed in SHAPES:
            for m in rx.finditer(text):
                if not allowed(m):
                    hits.add(f"D {rule}", rel, text, m.start(), m.end(), m.group(0) if rule in ("finance.db path or dump",) else rule)

    # E, F: the generated files only
    urls = product_urls()
    data_files = [p for p in generated if p.suffix in (".json", ".csv")]
    code_files = [p for p in generated if p.suffix in (".html", ".js", ".css")]
    gen_sorted = sorted(gen, key=lambda kv: -len(kv[1]))
    ci = re.compile("|".join(f"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])" for _, v in gen_sorted), re.I) if gen else None
    cs_forms = {}
    for kind, v in gen_sorted:
        for form in (v, v.upper(), v.title()):
            cs_forms.setdefault(form, kind)
    cs = re.compile("|".join(f"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])" for v in sorted(cs_forms, key=len, reverse=True))) if gen else None
    kind_of: dict[str, str] = {}
    for k, v in gen:   # a value of two kinds (a merchant that is also a statement line): its first
        kind_of.setdefault(v.casefold(), k)
    for p in data_files + code_files:
        rel = p.relative_to(dist).as_posix()
        text = p.read_text(errors="replace")
        if p.suffix == ".json":   # the strings of the data (keys too), not its numbers: a mask is never a number's digits
            try:
                pieces = list(_strings(json.loads(text), urls))
            except ValueError:   # not JSON at all (something else under a .json name): all of it, as text
                hits.add("D generated .json file that doesn't parse", rel, None, 0, 0, "")
                pieces = [text]
            rx = ci
        else:
            pieces, rx = [text], (ci if p in data_files else cs)
        if rx:
            for piece in pieces:
                for m in rx.finditer(piece):
                    kind = kind_of.get(m.group(0).casefold(), "value")
                    hits.add(f"E generator {kind}", rel, piece, m.start(), m.end(), f"{kind} ({mask(m.group(0))})")
    amount_rx = [(c_, "generator fixed amount") for c_ in sorted(fixed_cents)] + [(c_, "private amount") for c_ in sorted(private_cents)]
    compiled = [(c_, label, r) for c_, label in amount_rx for r in amount_res(c_)]
    for p in data_files + code_files:
        rel = p.relative_to(dist).as_posix()
        text = p.read_text(errors="replace")
        for c_, label, r in compiled:
            for m in r.finditer(text):
                hits.add(f"F {label}", rel, text, m.start(), m.end(), f"{label} ({mask(str(c_))})")

    counts = Counter(h[0] for h in hits.rows)
    print(f"demo_canary: {args.dist}: {len(files)} files ({static_ok} byte-identical product files, {len(generated)} generated), "
          f"{len(private)} private tokens, {len(forbidden)} repo tokens, {len(markers)} machine markers, "
          f"{len(gen)} generator values, {len(fixed_cents)} fixed amounts, {len(private_cents)} private amounts")
    print(f"demo_canary: allowed on purpose: generic statement lines {sorted(GENERIC)}, kept institution {sorted(KEEP)}, "
          "e-mail @example.com/.org/.net, Example/Sample/Demo streets, ZIP 00000, the tailnet label 'tailnet'")
    for rule, n in sorted(counts.items()):
        print(f"  {rule}: {n}")
    for rule, path, line, ctx in hits.rows[:200]:
        print(f"HIT {path}:{line}: {rule}: {ctx[:160]}")
    if len(hits.rows) > 200:
        print(f"... and {len(hits.rows) - 200} more")
    if hits.rows:
        print(f"demo_canary: FAIL: {len(hits.rows)} hit(s) in {len({h[1] for h in hits.rows})} file(s)")
        return 1
    if args.synthetic_only:
        print('demo_canary: SYNTHETIC CHECKS PASS: 0 hits; publication privacy approval NOT performed')
        return 0
    if not private:
        print("demo_canary: INDETERMINATE: no hit, but no private token list was checked (--no-private)")
        return 3
    print("demo_canary: PASS: 0 hits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
