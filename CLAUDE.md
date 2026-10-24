# CLAUDE.md: working in hermes-finance

The operative guide for anyone (person or coding agent) changing this repository. `AGENTS.md` is the agent-neutral
short version and carries the same essentials. User-facing docs are in `README.md` and `docs/`.

## What this is

A self-hosted personal-finance dashboard. It pulls accounts and transactions from Plaid into a local SQLite database,
classifies spending, and tracks budgets, goals, bills, loans and investments. It exposes the numbers three ways: a
Hermes plugin (native tools), an MCP server (`/mcp` and `finance mcp`, read-only by default), and an API-key mode
where the app runs its own tool loop for the Ask panel. Python 3.12 (`.python-version`, `requires-python = ">=3.12"`),
managed with uv. Runtime dependencies: `argon2-cffi` and `ofxtools`. Optional extras: `mcp`, `photos`.

## Layout

| Path | What's there |
|---|---|
| `src/hermes_finance/` | The app. `cli.py` (the `finance` command), `web.py` (`finance serve`: stdlib HTTP server, access gate, routes), `api.py` / `admin.py` / `features.py` / `chat.py` (route modules), `db.py` (schema and connections), `tooldefs.py` (the 16 agent tools), `mcp_server.py`, `plaid.py` / `link.py` / `sync.py` / `syncnow.py`, `insights.py` / `reads.py` / `portfolio.py` (the numbers). See `docs/architecture.md` for the full map. |
| `src/hermes_finance/static/` | The front end: plain JS and CSS, no build step. `app.js` plus one file per feature (`goals.js`, `bills.js`, `chat.js`, ...), vendored Chart.js, the Inter font, icons. Served only from the allowlist `web.STATIC_FILES`, so a new file must be added there. |
| `hermes_plugin/` | The Hermes plugin: `plugin.yaml`, `__init__.py` (tools and the approval hook), `completion.py` (the Ask bridge), `crons/` (`hermes finance crons`), `skills/usage/`. It runs on Hermes's own Python (3.11), so standard library and Hermes only. It loads the app from `HERMES_FINANCE_APP_DIR`. |
| `scripts/` | `check.py` (the check loop), `run_gates.py` / `audit_numbers.py` / `audit_layout.py` / `audit_controls.py` (the three gates), `dehardcode.py` and `hooks/pre-commit` (the privacy scanner), `gen_docs_reference.py` and `mkdocs_hooks.py` (generated docs), `rules_api_doc.py`. |
| `tests/` | The pytest suite. `conftest.py` (the safe environment), synthetic data (`factory.py`, `demo_data.py`, `gate_dbs.py`, `*_scene.py`), `cdp.py` (the headless-Chrome driver), `hermes_runtime.py` (the real Hermes in a throwaway sandboxed profile), `fixtures/` (old schemas, import files). |
| `docs/`, `mkdocs.yml` | The docs site. `docs/reference/*.md` is generated: don't edit it by hand. |
| `assets/brand/` | Icon sources and their renderer. |

`data/` (the database, the token backup, pictures), `*.db` and `.env*` are git-ignored and never committed.

## Run the app

```sh
uv sync                                   # .venv; the dev group adds pytest, pytest-xdist, the MCP SDK and Playwright
uv run finance serve --port 8799          # http://127.0.0.1:8799/ (8765 is the default)
uv run finance --help                     # every command; docs/reference/cli.md has them all
```

To look at the dashboard without real data, build a synthetic database and serve it from a temp directory:

```sh
uv run python tests/demo_data.py /tmp/hf-demo          # writes /tmp/hf-demo/finance.db
FINANCE_DATA_DIR=/tmp/hf-demo uv run finance serve --port 8799
```

Point `FINANCE_DATA_DIR` at a temp directory for anything you try by hand. A real install's `data/` and its server are
never touched by anything you try (see [Your branch, your data](#your-branch-your-data)).

## The safe environment (tests, gates, anything you run)

Tests and gates use synthetic data and fake keys, and never reach the outside world. This is enforced:

- `tests/conftest.py` points `HERMES_ENV_FILE`, `FINANCE_SECRETS_FILE` and `HERMES_ALPACA_KEY_FILE` at files that
  don't exist, and sets `HERMES_PLAID=off`, `HERMES_ALPACA=off`, `HERMES_CHROME_DEVTOOLS=off` and a missing
  `FINANCE_HERMES_BIN`. Every child process a test starts inherits these. Its autouse `no_network` fixture lets a
  socket connect only to loopback, makes urllib and every opener raise, refuses the real Chrome's DevTools port
  (9222), and swaps the valuation transport and browser for fakes.
- `scripts/check.py` removes every provider variable (`PLAID_`, `APCA_`, `ALPACA_`, `MARKETCHECK_`, `RENTCAST_`,
  `CARFAX_`, `REDFIN_`, `OPENROUTER_`, `OPENAI_`, `ANTHROPIC_`) and anything named like a credential (`KEY`, `SECRET`,
  `TOKEN`, `PASSWORD`, ...) before it starts pytest, and `GATE_KINDS` / `PYTEST_ADDOPTS`, which would narrow a run.
  It also sets `HERMES_PLAID=off`, `HERMES_ALPACA=off` and `HERMES_CHROME_DEVTOOLS=off`. Run pytest by hand the same
  way: never with real credentials in the shell.
- A **`python -c` child of a test has none of conftest's monkeypatches**, only its environment. So a new outbound path
  needs an environment switch that conftest sets, like `HERMES_PLAID=off` for Plaid (`plaid.urllib_transport`
  refuses while it's set).
- The one live test (`-m live`: Plaid's `/link/token/create`) and the optional WebKit checks (`-m webkit`) are
  deselected by `addopts = "-m 'not live and not webkit'"`. Don't run the live one unless a task asks for it.
- Synthetic data only. `scripts/dehardcode.py` is the one privacy scanner (see `docs/dehardcode.md`):
  `tests/test_dehardcode_task18.py` checks for private words in `src/`, `hermes_plugin/` and `tests/` (fixtures and
  generators included), and `tests/test_release_scan.py` runs the release scan (private words, home paths,
  credentials, real e-mails, long ids) on every tracked file that ships. Tests use invented values. CI's log is
  public, so `tests/redact.py` (loaded by `conftest.py`) replaces every private word in a test report with
  `<private>`; a scanner's own output names the path, line and rule, never the text.

## Tests and gates

```sh
uv run python scripts/check.py ci                 # what CI runs: every no-browser test, plus the release scan
uv run python scripts/check.py fast               # narrowed: what the diff against main can affect
uv run python scripts/check.py fast --dry-run     # the plan only
uv run python scripts/check.py full               # everything, the gates included: required before a task is done
uv run python scripts/check.py                    # the same as full
uv run python scripts/check.py full --part 1      # every test except the gates
uv run python scripts/check.py full --part 2 --kinds full,nocaps   # the gates on some databases
```

- **The no-browser set** is `check.test_files()[0]`: every `tests/test_*.py` whose source doesn't match `BROWSER_RE`
  (`Chrome(`, `audit_layout.audit|selftest`, `audit_numbers.audit`, `audit_controls.audit|selftest|probe`, `run_gates.run`, `an.audit(`), and doesn't import
  a module that does. `tests/test_gates.py` is in neither set. `check.py` runs that set with `HF_NO_CHROME=1`, under
  which `tests/cdp.py` refuses to start a Chrome, so a misfiled browser test fails loudly. The match is textual: the
  pattern in a docstring or comment moves a file into the browser set. Get the list for a manual run with
  `uv run python -c 'import sys; sys.path.insert(0, "scripts"); import check; print(" ".join(check.test_files()[0]))'`.
- **Runtime:** the no-browser set runs in parallel (`-n`, `--dist loadfile`). The extended layout gate alone takes
  about 8 minutes (`tests/test_gates.py`). `full --part 1` and `--part 2 [--kinds ...]` exist so each call fits in a
  10-minute window. Run them in the foreground, and don't end your turn while a run is still going.
- **Chrome budget:** browser tests and gates drive headless Chrome through `tests/cdp.py` (its own temp profile, a
  random DevTools port, every `https://` request blocked). It uses `HF_CHROME` when set, else the usual install
  paths, else `google-chrome` / `chromium` on `PATH`. At most **4** run at once (`check.py` `MAX_CHROMES`,
  `HF_CHROMES`; `run_gates.py` `GATES_CHROMES`; machine-wide slots in `tests/cdp.py`). Everything a run starts is
  stopped when it ends, however it ends. Without Chrome, the browser tests and gates skip.
- **The gates** (`scripts/run_gates.py [numbers|layout|both|controls] [kinds] [--out DIR]`, on the synthetic databases
  in `tests/gate_dbs.py`): Gate 1 checks every number and percentage the pages show against the API and the read-only CLI
  (±$0.01). Gate 2 checks clipping, alignment, spacing, charts, text size (≥ 13 px) and contrast (≥ 4.5:1) on every
  page and state at planned widths from 360 to 1440 px, with state-specific theme coverage. The raw audit can
  report `SKIP` when an element is absent. `scripts/check.py` accepts only fixture- and reason-specific absences
  declared in `ABSENT`, reporting them as absent, not checked. Unexpected skips and missing states fail. Gate 3
  (`scripts/audit_controls.py`, on `full` and `nocaps` at 1440 and 390 px) clicks every control and fails on one that
  does nothing.
- **A test proves a fix only if it fails on the old code.** Run the new test against the previous tree (`git show
  HEAD:<file>` into a temp directory) and record that it fails there, as `tests/test_layout_regressions.py` did for each
  layout bug.

## The database

`src/hermes_finance/db.py`. `SCHEMA_VERSION` is **19**, kept in `PRAGMA user_version`. The next migration is 20.

- `db.connect()` creates the data directory (0700) and the file (0600), turns on WAL, and calls `init_schema()` when
  the stored version is behind. `init_schema()` is idempotent and keeps every row: `CREATE TABLE IF NOT EXISTS`,
  `ADDED_COLUMNS` (guarded `ALTER TABLE ... ADD COLUMN`), one-time data steps gated on the version it started from,
  the views made again, then the version stamped. Each schema step has its own named function (`_migrate_task18`,
  `_task19_schema`, `_task21_schema`, `_task24_schema`, `_v16_schema` to `_v19_schema`, `_v19_rename`).
- `db.connect_readonly()` is `mode=ro` + `query_only` + an authorizer that allows only reads and makes credential
  columns (`HIDDEN_COLUMNS`) read as `NULL`. The read commands, `finance query` and the agent tools use it. They
  never upgrade a database, and they refuse an older one.
- **A schema change needs a proof.** Bump `SCHEMA_VERSION`, add a fixture of the previous schema
  (`tests/fixtures/schema_vN.sql`), and add an upgrade test that fills it with data and shows every row and value
  survives, and that the result matches a new database's definitions (`tests/test_schema_upgrade.py`,
  `tests/test_schema15_acceptance.py`, `tests/test_category_names_v19.py`). Two branches that both bump the version must be renumbered at merge into one
  cumulative step. Version 15 is such a step (see the comment on `SCHEMA_VERSION`).
- Signs follow Plaid: `amount > 0` is money out, `< 0` is money in, for every account type. Classification precedence
  is user > user rule > AI > AI rule > Plaid. A trigger protects user rows. Item rows are never deleted: with any blocked duplicate
  Plaid counted anyway, they are the 10-slot count (`config.SLOT_LIMIT`, `db.slots_used`).

## Conventions that are enforced

- **No network in tests** (above). New outbound code gets an injectable transport and an off switch that conftest
  sets.
- **One source for every number.** The dashboard (`api.py`), the read commands (`reads.py`) and the agent tools
  (`tooldefs.py`) call the same functions. `tests/test_reads.py` and the numbers gate compare them. Never compute a
  figure a second way.
- **One definition per agent tool**, in `tooldefs.py`. `hermes_plugin/plugin.yaml`'s `provides_tools` must list
  exactly `tooldefs.TOOLS` (`tests/test_tools_task17.py`). Writes go through `describe()` / `apply()`, so a write is
  always approved (the plugin hook, an MCP pending action, an Ask card) before it runs.
- **The front end never writes HTML.** `app.js` builds elements with `createElement` / `textContent`. No
  `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(` or `new Function`
  (`tests/test_grid_pack.py` checks these patterns in `app.js`; the rule applies to every front-end file).
  No inline scripts: the CSP doesn't allow them.
- **UI rules:** body text ≥ 15 px, secondary ≥ 13 px, contrast ≥ 4.5:1, sentence-case headings, plain short copy, one
  colour per category. No glows behind numbers and no "New" chips. Motion is short fades and slides plus the Overview
  count-up; under reduced motion only the fades remain (`docs/ui-invariants.md` rule 5). Editor sheets save when they
  close.
- **The gates before a UI change ships.** Anything that touches a page, its CSS or its numbers passes
  the numbers, layout and controls gates (`check.py full`), and keeps the numbered rules of `docs/ui-invariants.md`. A layout bug the gates find gets its own regression test
  (`tests/test_layout_regressions.py`).
- **Money math:** a new feature that changes a total must leave every existing number unchanged while it's unused,
  and adds a numbers-gate check for its own invariant.
- **Audits only read.** The gates send GETs only, and `audit_numbers.py --db` opens the database `mode=ro`. To audit
  a real database, make one read-only backup, run the gates on byte copies of it, and delete the copies afterwards.
  Never point a gate, a test or a write at a live database.
- **Additive changes in shared files.** Put new front-end code in new `static/<feature>.js` / `.css` files, loaded by
  the page shell (`web.FEATURE_FILES`, `web.STATIC_FILES`). Keep `api.py`, `cli.py`, `web.py` and `db.py` changes
  additive: new functions, routes and subparsers.
- **README compatibility strings.** `tests/test_dashboard.py` checks the `Chart.js v4.5.1` line and its `sha256`
  digest against the vendored file; `tests/test_networth_task15.py` checks `networth [--history] [--custom] [--json]`,
  `finance networth-custom`, `networth_excluded` and `All · Custom`, and that "my pick" / `--pick` never appear. Keep
  these strings when editing the README.
- **Generated docs.** After changing a command, a tool or a setting, run `uv run python scripts/gen_docs_reference.py`.
  `mkdocs build --strict` fails while `docs/reference/` is stale.
- **Processes you start are yours to stop.** Do as `scripts/check.py`, `tests/cdp.py` and `chat._lookup` do: a
  server, browser or worker runs in its own process group (`start_new_session=True`) and is stopped with the whole
  group (`os.killpg`) in a `finally` or signal handler, so a crash or timeout leaves nothing behind
  (`tests/test_cdp_cleanup.py` checks it for Chrome).

## Your branch, your data

1. Work on your own branch, in a worktree if you like: `git worktree add ../hermes-finance-fix -b fix main`.
2. Never point anything you run at your own install: use a temp `FINANCE_DATA_DIR` and a port other than 8765, keep
   real Plaid and Alpaca calls out of tests, and don't restart a server you use.
3. Reproduce the bug with synthetic data, and write tests that fail on the old code (with the old-code proof above).
   Fix it.
4. Run the full suite in two calls (`check.py full --part 1`, then `--part 2`). If the bug came from real data, also
   run the gates on copies of one read-only backup (see *Audits only read*).
5. For a UI change, look at every changed state (390 and 1440 px, both themes where it applies).
6. In the pull request, say what changed and why, with the literal test and gate counts and anything still unproven.
