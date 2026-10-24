# Contributing

hermes-finance is a personal project with best-effort support. Issues and small, focused changes are welcome. The
full working rules for this repository, for people and coding agents alike, are in `CLAUDE.md` and `AGENTS.md` at the
repository root. This page is the short version.

## Set up

```sh
uv sync          # Python 3.12 .venv with the dev group: pytest, pytest-xdist, the MCP SDK, Playwright
```

## Tests never touch the outside world

- `tests/conftest.py` blocks the network for every test. Sockets may connect only to loopback, and urllib and every
  opener raise. It also points every key file at a path that doesn't exist and sets `HERMES_PLAID=off`,
  `HERMES_ALPACA=off` and `HERMES_CHROME_DEVTOOLS=off`. Every child process a test starts inherits those switches.
  Plaid is a mocked transport (`FakePlaid`), and the data is synthetic (`tests/factory.py`, `tests/demo_data.py`,
  `tests/gate_dbs.py`).
- The one live test (`-m live`: Plaid's `/link/token/create` on Production, which creates nothing) and the optional
  WebKit checks (`-m webkit`) are deselected by default (`addopts = "-m 'not live and not webkit'"` in `pyproject.toml`).
- Never run tests with real credentials in your shell. `scripts/check.py` strips every provider variable and anything
  named like a credential before it starts pytest. Do the same if you run pytest yourself.

## The check loop

```sh
uv run python scripts/check.py ci               # what CI runs on every push: the no-browser tests and the release scan
uv run python scripts/check.py fast             # narrowed: what your diff against main can affect
uv run python scripts/check.py fast --dry-run   # print the plan, run nothing
uv run python scripts/check.py full             # everything, the gates included: required before a change is done
```

- **ci** runs every test that needs no browser (the same list `fast` and `full` use) and the release scan of every
  tracked file (`scripts/dehardcode.py --release-tracked`). It's what `.github/workflows/ci.yml` runs on Linux; the nightly
  workflow runs `full` with Chrome on macOS, where the browser tests and the layout gate were tuned (Linux Chromium
  renders text a fraction of a pixel wider).

- **fast** runs every test that needs no browser, the browser tests your change can affect, and the layout and
  numbers gates narrowed to it. Its last line says what it left out.
- **full** runs the whole suite in two halves: every test except the gates, then `tests/test_gates.py`. Use
  `--part 1` or `--part 2` to run one half, and `--part 2 --kinds full,nocaps` to run the gates on some databases only.
  The extended layout gate alone takes about 8 minutes.
- `full` is also what the bare `check.py` runs.
- Chrome: `tests/cdp.py` uses `HF_CHROME` when set, else the usual install paths, else `google-chrome` / `chromium` on
  `PATH`. Without one, the browser tests and gates skip.
- At most **4 headless Chromes** run at once (`HF_CHROMES`, capped at 4). Everything a run starts is stopped when it
  ends, including on Ctrl-C and SIGTERM.

To run the browser-free tests with plain pytest, use the same file list `check.py` uses:

```sh
env -u PLAID_CLIENT_ID -u PLAID_SECRET -u PLAID_ENV HERMES_PLAID=off HERMES_ALPACA=off HERMES_CHROME_DEVTOOLS=off \
  HF_NO_CHROME=1 uv run pytest -q -p no:cacheprovider -n 8 --dist loadfile \
  $(uv run python -c 'import sys; sys.path.insert(0, "scripts"); import check; print(" ".join(check.test_files()[0]))')
```

(`env -u` above covers Plaid only. Unset any other provider key your shell exports too.) A test that starts a Chrome
fails loudly under `HF_NO_CHROME=1`, so a misfiled browser test can't hide in the fast set.

## The three gates

A UI change ships only with the three gates passing (`scripts/run_gates.py` runs them on the synthetic databases):

- **Gate 1, numbers** (`scripts/audit_numbers.py`): every figure and percentage the pages show, checked against the
  API and the read-only CLI to ±$0.01.
- **Gate 2, layout** (`scripts/audit_layout.py`): every page at 390, 768, 1024, 1280 and 1440 px in both themes (plus
  430 and 360 for the phone tap check), and every sheet, panel and editor at a subset of those widths. It fails on
  clipping, misalignment, uneven spacing, small or low-contrast text, and overlays off screen. The numbered rules it
  enforces are in [UI invariants](ui-invariants.md).
- **Gate 3, controls** (`scripts/audit_controls.py`): clicks every control on the `full` and `nocaps` databases at 1440
  and 390 px, and fails on one that does nothing.

```sh
uv run python scripts/run_gates.py [numbers|layout|both|controls] [full,nocaps,empty,history-empty,onepoint,features,signin,signin-setup] [--out DIR]
```

Each database is served by the app on a throwaway loopback port with its own headless Chrome. The gates never reach
Plaid, a valuation provider or the real Chrome. Both audits only read: they send GETs, and `audit_numbers.py --db`
opens the database with `mode=ro`.

## The privacy scanner

`scripts/dehardcode.py` keeps personal data out of everything that ships: see [The privacy scanner](dehardcode.md).
Install its pre-commit hook once with `sh scripts/install-hooks.sh`.

## Database changes

`SCHEMA_VERSION` is in `src/hermes_finance/db.py`. A schema change bumps it, adds its tables and columns
idempotently (`CREATE TABLE IF NOT EXISTS`, guarded `ALTER TABLE` through `ADDED_COLUMNS`), and comes with proof: a
fixture of the previous schema (`tests/fixtures/schema_vN.sql`) and an upgrade test that fills it with data and shows
every row survives (`tests/test_schema_upgrade.py`, `tests/test_schema15_acceptance.py`).

## Docs

The site is MkDocs with the Material theme. Three pages are generated from the code, and the build fails while they're
stale:

```sh
uv run python scripts/gen_docs_reference.py            # rewrite docs/reference/{cli,tools,settings-keys}.md
uv run python scripts/gen_docs_reference.py --check    # exit 1 when one is stale

uvx --with "mkdocs-material>=9.5,<10" --from "mkdocs>=1.6,<2" mkdocs build --strict   # writes site/ (git-ignored)
```

The generator needs Python 3.12, because argparse lays out help differently in other versions.
