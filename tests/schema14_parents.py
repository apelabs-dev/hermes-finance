"""The two schema-14s the integration reconciled, pinned from immutable Git objects (schema-15 acceptance G2).

Both lanes stamped 14 with different DDL: the chat lane (ff94705) added pending_actions.origin / held_by; the rules lane
(562beb6) added rule_deletions, its trigger, classifications.detached_by and receipt-aware views. The fixtures
tests/fixtures/schema_v14_{chat,rules}[_from_v13].sql are what each parent's db.init_schema leaves on a new database and
on the pinned v13 fixture, made here WITHOUT importing or running any of the parents' code:

- each parent's src/hermes_finance/db.py is read from the Git object store by its blob id (checked against the commit's
  tree and re-hashed);
- its module-level SQL literals are read with ast.literal_eval;
- its init_schema is replayed statement by statement: every statement (and every statement of the helpers it calls) is
  matched by its exact source text to a step below that runs only those literals on a scratch in-memory database. A
  statement this file doesn't know, or a branch whose data condition doesn't hold, stops it (Unreplayable).

`definition()` is the structural comparison both this file and test_schema15_acceptance.py use: every table, index, view
and trigger with its SQL minus comments and layout, and SQLite's own reading of each table (columns, foreign keys,
indexes). Stdlib only; never imports hermes_finance.

    python tests/schema14_parents.py --check   # the committed fixtures are exactly what the pinned objects give
    python tests/schema14_parents.py --write   # (re)write them, only ever from those objects
"""
from __future__ import annotations

import ast
import hashlib
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DB_PY = "src/hermes_finance/db.py"


@dataclass(frozen=True)
class Pin:
    lane: str
    commit: str
    blob: str        # git rev-parse <commit>:src/hermes_finance/db.py


V13 = Pin("v13", "6b89dd50d5a4f1ca8ac1a55e0c02499e5d49ecec", "1d0cf5d931e249e108cb6ef1c52896c5b81ec1b8")
PARENTS = {
    "chat": Pin("chat", "ff94705fa9f6b3f156035a3717b9375578926992", "b988ca8ef07eb850a1f8168835ad42c758253de9"),
    "rules": Pin("rules", "562beb67db7c1b5abc637f637d76c600b6bec039", "530da6538df4c395950bf6b1a5e2d6c21532d9c2"),
}
V13_FIXTURE = FIXTURES / "schema_v13.sql"
V13_FIXTURE_BLOB = "0ce05454130633fd923f6645f4dbbf56e7c6637e"   # the rules parent's own tests/fixtures/schema_v13.sql


def fixture(lane: str, from_v13: bool) -> Path:
    return FIXTURES / f"schema_v14_{lane}{'_from_v13' if from_v13 else ''}.sql"


class Unreplayable(Exception):
    """The parent's init_schema has a statement (or takes a branch) this replay doesn't reproduce exactly."""


# --- reading the immutable objects -------------------------------------------------------------------------------------

def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True, timeout=30).stdout


def objects_available() -> bool:
    try:
        return all(_git("cat-file", "-t", p.commit).strip() == b"commit" for p in (*PARENTS.values(), V13))
    except (OSError, subprocess.SubprocessError):
        return False


def blob(commit: str, path: str, expected: str) -> str:
    """The file `path` at `commit`, only if its blob id is `expected` (and its bytes hash to it)."""
    found = _git("rev-parse", f"{commit}:{path}").decode().strip()
    if found != expected:
        raise Unreplayable(f"{commit[:12]}:{path} is blob {found}, not the pinned {expected}")
    data = _git("cat-file", "blob", expected)
    if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != expected:
        raise Unreplayable(f"blob {expected} does not hash to its id")
    return data.decode()


class Source:
    """A parent's db.py as data: its literal module-level values and its function definitions (never executed)."""

    def __init__(self, text: str):
        tree = ast.parse(text)
        self.lit: dict[str, object] = {}
        self.fns: dict[str, ast.FunctionDef] = {}
        computed = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                self.fns[node.name] = node
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:
                    computed.append(ast.unparse(node))
                    continue
                if name in self.lit:
                    raise Unreplayable(f"{name} is bound to a literal twice")
                self.lit[name] = value
        # the one computed module value the schema depends on (db.py splices ASSET_VALUATIONS into SCHEMA)
        if "SCHEMA = SCHEMA.replace('{asset_valuations}', ASSET_VALUATIONS)" not in computed:
            raise Unreplayable("SCHEMA is not built the way this replay builds it")
        self.lit["SCHEMA"] = self.lit["SCHEMA"].replace("{asset_valuations}", self.lit["ASSET_VALUATIONS"])

    def body(self, fn: str) -> list[ast.stmt]:
        stmts = self.fns[fn].body
        if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant):
            stmts = stmts[1:]                                  # the docstring
        return stmts


# --- replaying init_schema ---------------------------------------------------------------------------------------------

Step = Callable[[sqlite3.Connection, Source, ast.stmt, int], None]


def _script(name: str) -> Step:
    return lambda conn, src, node, before: conn.executescript(src.lit[name])


def _add_missing_columns(name: str) -> Step:
    def step(conn, src, node, before):
        for table, column, decl in src.lit[name]:
            if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return step


def _literal_sql(node: ast.stmt) -> str:
    call = node.value if isinstance(node, ast.Expr) else node.body[0].value
    return call.args[0].value


def _if_before(test: Callable[[int], bool]) -> Step:
    """`if before < N: conn.execute(<SQL literal>)`: that SQL, when the branch is taken."""
    def step(conn, src, node, before):
        if test(before):
            conn.execute(_literal_sql(node))
    return step


def _never_before(test: Callable[[int], bool], why: str) -> Step:
    def step(conn, src, node, before):
        if test(before):
            raise Unreplayable(why)
    return step


def _executemany(values: Callable[[Source], object]) -> Step:
    return lambda conn, src, node, before: conn.executemany(_literal_sql(node), values(src))


def _asset_valuations_is_v8(conn, src, node, before):
    """_rebuild_asset_valuations returns at once when the table names 'carfax' (every v8+ database, and a new one)."""
    if [ast.unparse(s) for s in src.body("_rebuild_asset_valuations")[:2]] != list(ASSET_VALUATIONS_EARLY_RETURN):
        raise Unreplayable("_rebuild_asset_valuations does not return early as this replay assumes")
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'asset_valuations'").fetchone()
    if row is None or "'carfax'" not in row[0]:
        raise Unreplayable("asset_valuations would be rebuilt (a pre-v8 table): not replayed")


def _no_loans_to_link(conn, src, node, before):
    """`if before < 9: _link_loans(conn)`: only replayed with no manual account (then it links nothing)."""
    if before < 9 and conn.execute("SELECT COUNT(*) FROM manual_accounts").fetchone()[0]:
        raise Unreplayable("_link_loans would run over manual accounts: not replayed")


def _call(fn: str) -> Step:
    def step(conn, src, node, before):
        for stmt in src.body(fn):
            _replay(conn, src, stmt, before)
    return step


ASSET_VALUATIONS_EARLY_RETURN = (
    'row = conn.execute("SELECT sql FROM sqlite_master WHERE type = \'table\' AND name = \'asset_valuations\'")'
    '.fetchone()',
    'if row is None or "\'carfax\'" in row[0]:\n    return',
)
_ADD = ("for table, column, decl in {}:\n"
        "    if column not in {{r[1] for r in conn.execute(f'PRAGMA table_info({{table}})')}}:\n"
        "        conn.execute(f'ALTER TABLE {{table}} ADD COLUMN {{column}} {{decl}}')")
# Every statement of init_schema (inside `with conn:`) and of the helpers it calls, in any of the pinned commits, by its
# exact source text (ast.unparse), and how it is replayed.
STEPS: dict[str, Step] = {
    "conn.executescript(SCHEMA)": _script("SCHEMA"),
    _ADD.format("ADDED_COLUMNS"): _add_missing_columns("ADDED_COLUMNS"),
    "if before < 7:\n    conn.execute(\"UPDATE manual_accounts SET value_source = 'user' WHERE kind IN ('vehicle', "
    "'property') AND value_source IS NULL\")": _if_before(lambda b: b < 7),
    "if before < 8:\n    conn.execute(\"UPDATE manual_accounts SET vehicle_condition = 'good' WHERE kind = 'vehicle' AND "
    "vehicle_condition IS NULL\")": _if_before(lambda b: b < 8),
    "_rebuild_asset_valuations(conn)": _asset_valuations_is_v8,
    "if before < 9:\n    _link_loans(conn)": _no_loans_to_link,
    "_migrate_task18(conn, before)": _call("_migrate_task18"),
    "conn.executescript(TASK18_SCHEMA)": _script("TASK18_SCHEMA"),
    "if 0 < before < 11:\n"
    "    conn.executemany(\"INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, 'true', ?)\", "
    "[('valuations.carfax', now()), ('valuations.redfin', now())])":
        _never_before(lambda b: 0 < b < 11, "an install from before v11 seeds settings: not replayed"),
    _ADD.format("TASK18_COLUMNS"): _add_missing_columns("TASK18_COLUMNS"),
    "conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_fitid ON transactions(account_id, fitid) "
    "WHERE fitid IS NOT NULL')": lambda conn, src, node, before: conn.execute(_literal_sql(node)),
    "_task24_schema(conn)": _call("_task24_schema"),                  # the rules parent only
    "conn.executescript(TASK24_SCHEMA)": _script("TASK24_SCHEMA"),
    "conn.executescript(VIEWS)": _script("VIEWS"),
    "_task19_schema(conn)": _call("_task19_schema"),
    "conn.executescript(TASK19_SCHEMA)": _script("TASK19_SCHEMA"),
    "_task21_schema(conn)": _call("_task21_schema"),
    "conn.executescript(TASK21_SCHEMA)": _script("TASK21_SCHEMA"),
    "conn.executemany('INSERT OR IGNORE INTO categories (name, is_income, is_transfer) VALUES (?, ?, ?)', "
    "DEFAULT_CATEGORIES)": _executemany(lambda src: src.lit["DEFAULT_CATEGORIES"]),
    "conn.executemany('INSERT OR IGNORE INTO plaid_category_map (pfc_code, category_id) "
    "SELECT ?, id FROM categories WHERE name = ?', DEFAULT_PFC_MAP.items())":
        _executemany(lambda src: src.lit["DEFAULT_PFC_MAP"].items()),
    "conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')":
        lambda conn, src, node, before: conn.execute(f"PRAGMA user_version = {int(src.lit['SCHEMA_VERSION'])}"),
}


def _replay(conn: sqlite3.Connection, src: Source, stmt: ast.stmt, before: int) -> None:
    text = ast.unparse(stmt)
    if text not in STEPS:
        raise Unreplayable(f"unknown statement in the parent's schema code: {text!r}")
    STEPS[text](conn, src, stmt, before)


def replay_init_schema(conn: sqlite3.Connection, src: Source) -> None:
    """What the parent's init_schema(conn) does to `conn`, from its own statements, in its own order."""
    head, block = src.body("init_schema")
    if ast.unparse(head) != "before = conn.execute('PRAGMA user_version').fetchone()[0]" or \
            not (isinstance(block, ast.With) and ast.unparse(block.items[0]) == "conn"):
        raise Unreplayable("init_schema is not `before = user_version; with conn: ...`")
    before = conn.execute("PRAGMA user_version").fetchone()[0]
    for stmt in block.body:
        _replay(conn, src, stmt, before)
    conn.commit()


def parent_source(pin: Pin) -> Source:
    return Source(blob(pin.commit, DB_PY, pin.blob))


def build(pin: Pin, base_sql: str | None = None) -> sqlite3.Connection:
    """A scratch in-memory database: `base_sql` (a dump), then the parent's init_schema replayed over it."""
    conn = sqlite3.connect(":memory:")
    if base_sql is not None:
        conn.executescript(base_sql)
    replay_init_schema(conn, parent_source(pin))
    return conn


def dump(conn: sqlite3.Connection) -> str:
    """As tests/fixtures/schema_v13.sql was made: the version, then sqlite3 iterdump (schema and default rows)."""
    return f"PRAGMA user_version = {conn.execute('PRAGMA user_version').fetchone()[0]};\n" + \
        "\n".join(conn.iterdump()) + "\n"


def header(pin: Pin, from_v13: bool) -> str:
    what = {"chat": "the chat lane (TASK-23 review: pending_actions.origin, held_by)",
            "rules": "the rules lane (TASK-24 fix: rule_deletions, its trigger, classifications.detached_by)"}[pin.lane]
    base = "on tests/fixtures/schema_v13.sql" if from_v13 else "on a new database"
    return (f"-- Schema v14 as {what} left it\n"
            f"-- {base}: {DB_PY} at commit {pin.commit}\n"
            f"-- (blob {pin.blob}), its SQL literals replayed in its init_schema's order\n"
            f"-- by tests/schema14_parents.py (no project code imported or run), dumped with sqlite3 iterdump. Used by\n"
            f"-- test_schema15_acceptance.py; do not edit (tests/schema14_parents.py --write rebuilds it from the Git\n"
            f"-- objects).\n")


def v13_base() -> str:
    """tests/fixtures/schema_v13.sql, checked to be the rules parent's own committed copy."""
    text = V13_FIXTURE.read_text()
    if text != blob(PARENTS["rules"].commit, "tests/fixtures/schema_v13.sql", V13_FIXTURE_BLOB):
        raise Unreplayable("tests/fixtures/schema_v13.sql is not the rules parent's committed fixture")
    return text


def replayed(pin: Pin, from_v13: bool) -> sqlite3.Connection:
    """The parent's init_schema replayed on a new database, or on the pinned v13 fixture (a v13 database it upgraded)."""
    return build(pin, v13_base() if from_v13 else None)


def expected_fixtures() -> dict[Path, str]:
    """Every pinned fixture's exact text, from the Git objects alone."""
    return {fixture(lane, from_v13): header(pin, from_v13) + dump(replayed(pin, from_v13))
            for lane, pin in PARENTS.items() for from_v13 in (False, True)}


def exact(conn: sqlite3.Connection) -> tuple:
    """The stamp, every object's SQL exactly as stored (comments included) and every row of every table: what a fixture
    must reproduce, whichever Python's iterdump wrote it."""
    master = sorted(tuple(r) for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
    rows = {name: [tuple(r) for r in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            for kind, name, _, _ in master if kind == "table"}
    return conn.execute("PRAGMA user_version").fetchone()[0], master, rows


def loaded(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(path.read_text())
    return conn


def v13_fixture_matches_its_commit() -> bool:
    """The v13 base (made before this file, by other means) is exactly what a replay of 6b89dd5's init_schema makes."""
    return exact(loaded(V13_FIXTURE)) == exact(build(V13))


# --- structure ---------------------------------------------------------------------------------------------------------

_TIGHT = set("(),;")


def normalized(sql: str | None) -> str | None:
    """`sql` as SQLite parses it: comments and layout dropped, quoted text kept exactly. `x (a, b)` and `x(a,b)` compare
    equal, and so do a CREATE TABLE and the same table grown by ALTER TABLE ADD COLUMN (SQLite splices ", <column>" in
    before its closing parenthesis)."""
    if sql is None:
        return None
    out: list[str] = []
    gap, i, n = False, 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i, gap = (n if end < 0 else end), True
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i, gap = (n if end < 0 else end + 2), True
            continue
        if ch.isspace():
            i, gap = i + 1, True
            continue
        j = i + 1
        if ch in "'\"":                                          # a string or quoted name ('' / "" escape the quote)
            while j < n and not (sql[j] == ch and not sql.startswith(ch * 2, j)):
                j += 2 if sql.startswith(ch * 2, j) else 1
            j += 1
        atom = sql[i:j]
        if gap and out and out[-1][-1] not in _TIGHT and atom[0] not in _TIGHT:
            out.append(" ")
        out.append(atom)
        gap, i = False, j
    return "".join(out)


def definition(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Every table, index, view and trigger, by (type, name): its SQL without comments or layout (the declarations:
    types, NOT NULL, DEFAULT, CHECK, REFERENCES, UNIQUE, COLLATE, a partial index's WHERE, a view's or trigger's body)
    and, for a table, SQLite's own reading of it: its columns in order (PRAGMA table_xinfo), foreign keys and indexes
    (automatic ones too). Two databases with equal definitions have the same schema whatever path made each."""
    out = {}
    for kind, name, table, sql in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                                               "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name"):
        d: dict = {"on": table, "sql": normalized(sql)}
        if kind == "table":
            d["columns"] = [tuple(r) for r in conn.execute(f'PRAGMA table_xinfo("{name}")')]
            d["foreign_keys"] = sorted(tuple(r) for r in conn.execute(f'PRAGMA foreign_key_list("{name}")'))
            d["indexes"] = {r[1]: (r[2], r[3], r[4], [tuple(x) for x in conn.execute(f'PRAGMA index_xinfo("{r[1]}")')])
                            for r in conn.execute(f'PRAGMA index_list("{name}")')}
        out[(kind, name)] = d
    return out


def changes(before: dict, after: dict) -> tuple[set, set, set]:
    """(objects added, removed, changed) from one definition() to another."""
    return (set(after) - set(before), set(before) - set(after),
            {k for k in set(before) & set(after) if before[k] != after[k]})


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--check"
    if mode not in ("--check", "--write"):
        print(__doc__)
        return 2
    wanted = expected_fixtures()
    if mode == "--write":
        for path, text in wanted.items():
            path.write_text(text)
            print(f"wrote {path.relative_to(ROOT)}")
    stale = [p for p, text in wanted.items() if not p.exists() or p.read_text() != text]
    for p in stale:
        print(f"differs from the pinned objects: {p.relative_to(ROOT)}")
    v13_ok = v13_fixture_matches_its_commit()
    print(f"{len(wanted) - len(stale)}/{len(wanted)} v14 fixtures match the pinned parents; "
          f"schema_v13.sql {'matches' if v13_ok else 'DIFFERS FROM'} {V13.commit[:12]}'s init_schema")
    return 1 if stale or not v13_ok else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
