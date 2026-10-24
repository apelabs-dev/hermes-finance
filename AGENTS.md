# AGENTS.md: working in hermes-finance

Instructions for any coding agent (and any person) changing this repository. `CLAUDE.md` is the longer guide, with the
details and the files behind each rule. This file stands alone on the essentials, and the two must agree.

UI changes must preserve the numbered rules in docs/ui-invariants.md.
See that page for scope and the exact enforcing tests before changing layout.

## The project

A self-hosted personal-finance dashboard: Plaid → a local SQLite database → budgets, bills, goals, loans,
investments, net worth. The numbers are exposed through a Hermes plugin (native tools), an MCP server (read-only by
default), and an API-key mode where the app runs its own tool loop for the Ask panel. Python 3.12, managed with uv.

| Path | Contents |
|---|---|
| `src/hermes_finance/` | The app: `cli.py`, `web.py`, `db.py`, `tooldefs.py`, the route modules and the number modules |
| `src/hermes_finance/static/` | The front end: plain JS and CSS, no build step, served from the allowlist `web.STATIC_FILES` |
| `hermes_plugin/` | The Hermes plugin: runs on Hermes's Python 3.11, standard library and Hermes only |
| `scripts/` | `check.py` (the check loop), `run_gates.py` and the three audits, `dehardcode.py` (the privacy scanner), the docs generator |
| `tests/` | pytest, with `conftest.py` (the safe environment), synthetic data, and `cdp.py` (headless Chrome) |
| `docs/`, `mkdocs.yml` | The docs site. `docs/reference/` is generated. |

## Run

```sh
uv sync
uv run python tests/demo_data.py /tmp/hf-demo                     # a synthetic database
FINANCE_DATA_DIR=/tmp/hf-demo uv run finance serve --port 8799    # a temp data directory, never your real data/
```

## Test

```sh
uv run python scripts/check.py ci              # what CI runs: every test that needs no browser, plus the release scan
uv run python scripts/check.py fast            # narrowed to what your diff against main can affect
uv run python scripts/check.py full            # everything, the gates included (also the bare `check.py`)
uv run python scripts/check.py full --part 1   # or in two calls: every test except the gates,
uv run python scripts/check.py full --part 2   # then tests/test_gates.py (the layout gate alone takes about 8 minutes)
```

- **No network, no real keys.** `tests/conftest.py` blocks every non-loopback connection and sets `HERMES_PLAID=off`,
  `HERMES_ALPACA=off` and `HERMES_CHROME_DEVTOOLS=off`, plus missing key files, for every test and every process a test
  starts. `scripts/check.py` strips provider and credential-named variables before pytest runs. Never run tests with
  real credentials in the environment. A new outbound path needs an off switch that conftest sets, because a test's
  child process doesn't get conftest's monkeypatches.
- **The no-browser set** is `check.test_files()[0]` in `scripts/check.py`. It runs under `HF_NO_CHROME=1`, where
  starting a Chrome fails. A file joins the browser set when its text matches `BROWSER_RE`, even in a comment.
- **Chrome**: `tests/cdp.py` uses `HF_CHROME` when set, else the usual install paths, else `google-chrome` /
  `chromium` on `PATH`. Without one, the browser tests and gates skip.
- **Chrome budget: at most 4 headless Chromes at once** (`check.py`, `run_gates.py`, `tests/cdp.py`). Run suites in
  the foreground, and stop everything you start, with its whole process group.
- **Prove a fix**: the new test must fail on the old code (`git show HEAD:<file>` into a temp directory).
- The live Plaid test (`-m live`) and the WebKit checks (`-m webkit`) are deselected by default. Don't run the live one.

## Rules the code enforces

- **Schema** (`db.py`, `SCHEMA_VERSION = 19`; the next one is 20): changes are idempotent (`CREATE TABLE IF NOT
  EXISTS`, `ADDED_COLUMNS`), in their own named function, and come with a proof: a `tests/fixtures/schema_vN.sql` of
  the previous version and an upgrade test that keeps every row. Read-only connections never upgrade a database.
- **One source per number**: the dashboard, `reads.py` and the agent tools call the same functions. Tool definitions
  live only in `tooldefs.py`, and `plugin.yaml` lists exactly those tools.
- **Writes are always approved** before they run: the plugin's hook, an MCP pending action, or an Ask card.
- **The front end never writes HTML** (no `innerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`; no inline
  scripts). Put new UI in new `static/<feature>.js` / `.css` files, and keep shared Python files additive.
- **UI**: body ≥ 15 px, secondary ≥ 13 px, contrast ≥ 4.5:1, sentence case, plain short copy. No glows, no "New"
  chips. Motion is short fades and slides plus the Overview count-up; under reduced motion only fades remain.
- **The three gates pass before a UI change ships** (numbers ±$0.01; layout at planned widths from 360 to 1440 px,
  with state-specific theme coverage; controls: every control does something when clicked). The raw layout audit can
  report `SKIP`; `scripts/check.py` accepts only expected absences declared in `ABSENT`, reporting them as absent, not
  checked. Unexpected skips fail. Audits only read: they send GETs, and a database is opened `mode=ro`.
- **No personal data** anywhere that ships: no private word in `src/`, `hermes_plugin/` or `tests/` (fixtures and
  generators included), and no home path, credential, real e-mail or long id in any shipped file
  (`scripts/dehardcode.py`, `tests/test_dehardcode_task18.py`, `tests/test_release_scan.py`). Use synthetic data only.
- **README compatibility strings**: keep `Chart.js v4.5.1` with its `sha256` digest, `networth [--history] [--custom]
  [--json]`, `finance networth-custom`, `networth_excluded` and `All · Custom`; never "my pick" / `--pick`
  (`tests/test_dashboard.py`, `tests/test_networth_task15.py`).
- **Generated docs**: after changing a command, tool or setting, run `uv run python scripts/gen_docs_reference.py`.

## Your branch, your data

- Work on your own branch (a worktree is easiest). Use a temp `FINANCE_DATA_DIR` and a port other than 8765 for
  anything you try by hand, so your own install, its `data/` and its server are never touched.
- Write failing tests first, then the fix; run `check.py full` (or its two parts) before you call a change done, and
  report the literal counts and anything you couldn't verify.

Releases follow the squashed public export loop in [docs/releasing.md](docs/releasing.md); sync public release versions before the next export.
