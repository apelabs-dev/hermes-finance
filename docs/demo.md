# Static demo

The demo runs the real dashboard UI with read-only synthetic data. Deployment consists entirely of files in
`dist/demo/`; it needs no backend, account, API key, model, or scheduled reset.

From a cold clone with the project's normal `uv` prerequisite, one command builds it:

```sh
uv run --frozen python scripts/build_static_demo.py --synthetic-only --today 2026-10-24
```

Add `--serve` to that command to open a standard-library static server on `http://127.0.0.1:8000/`
(`--port` changes the port). Stop it with Ctrl-C. The equivalent independent preview is:

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory dist/demo
```

`scripts/build_static_demo.py` delegates to the existing `scripts/build_demo.py`. There is only one builder.
A pinned `--today` produces identical file names and bytes on repeated builds from the same source and dependencies.
Without it, the build uses the local date. Browser time starts at noon UTC on that date.

## Data and interaction

The builder creates `tests/gate_dbs.py`'s `features` database and adds its existing group-budget fixture in a temporary
directory. It uses the existing sanitizer and rename map: monetary values are scaled, names and identifiers become
fictional, remote pictures/logos are removed, and synthetic local images remain. Network transports are disabled and
credentials are removed before importing the app. It never opens an installed database, credential file, or Hermes
profile. Temporary databases and rename maps are removed after the build. No database ships.

Every allowlisted product asset under `src/hermes_finance/static/` is copied byte-for-byte. The App supplies the page
shell; the demo adds its banner, CSP, adapter and sequential asset loader, removes Plaid Link, and labels Ask as canned.
The loader fetches the original styles and scripts in order, so an unmodified static server's small connection backlog
works too. No bundler or new dependency is required.

Frozen JSON under `demo/data/` contains real App GET responses: core pages, every historical month, bills calendars,
asset details and bill lookups. Transaction rows and their ten sort orders also come from the App. The adapter selects
those frozen rows for searches, repeated category/group/tag filters, date windows, facets and pagination; it computes
only the selected transaction list's sums/counts. Overview, budgets (including group budgets), balances and forecasts
are never recalculated by a browser money port. Saved-filter counts follow the selected window.

Tabs, sheets, months, ranges and all-time search work locally. Every data write, including sync/connect, budget edits,
settings network changes and pending approvals, is refused with the toast **Not available in the demo**. Only
appearance and region display preferences can persist in this browser. The fixed banner says **Demo — synthetic data**.
External links, documentation links and database backups are refused. The synthetic CSV export is available locally.
Unknown API paths/queries fail closed. Cross-origin fetch/XHR requests are refused; CSP includes `connect-src 'self'`.
No Plaid script, paid API, external image/logo, CDN, documentation request, or model runs.

Ask presents exactly four examples, all clearly labelled **Canned demo answer**:

1. How much did I spend on dining last month?
2. Am I on track with my budgets this month?
3. What bills are due in the next 7 days?
4. How has my net worth changed this year?

The builder calls the real `finance_spend`, `finance_budget_status`, `finance_bills` and `finance_networth` tools on the
sanitized synthetic database. It freezes their complete arguments/results in `demo/data/ask.json`. The first three
answers use the real CLI formatters; the fourth compares actual history points, identifying the first available point
this year instead of assuming a January 1 balance. No model prose or invented tool result is involved. Other questions
return exactly **AI chat is off in the demo — install it to use your own model.**

## Privacy boundary and verification

`--synthetic-only` is the public cold-clone build mode. It explicitly ignores ambient owner-token variables and files.
It runs canary checks for identity/credential shapes, machine markers, unchanged product assets and distinctive
unscaled generator amounts. Its success says **publication privacy approval NOT performed**. It is not evidence that
an owner's private identifiers were checked.

Publication remains fail-closed: the default builder/canary requires an explicit private-token list (or the documented
`HERMES_FINANCE_TEST_PRIVATE` variable). Missing private input exits 3 in the canary and fails the builder. A hit exits 1.
`--no-private` still exits 3 even with no hits. Never put private inputs in a public repository or build artifact.
Only a publication operator authorized to access that private list should run:

```sh
uv run python scripts/demo_canary.py dist/demo --private /path/outside/repo/private-tokens.txt
```

Build and inspect with synthetic fixtures only:

```sh
uv run python -m pytest tests/test_static_demo.py tests/test_demo_data.py
uv run python scripts/demo_parity.py --today 2026-10-24
uv run python scripts/demo_canary.py dist/demo --synthetic-only
uv run python scripts/demo_canary_selftest.py dist/demo --synthetic-markers
uv run python scripts/demo_verify.py dist/demo --out /tmp/hf-demo-evidence
```

The canary self-test uses disposable invented owner/repository markers and plants a leak for every rule. It does not
need the owner's file. The browser verifier uses `tests/cdp.py`'s machine-wide Chrome slots, disables orphan sweeping,
and closes only processes it starts. It serves a scratch **copy** with literal `python -m http.server` bound to loopback,
screenshots every page and all five asset details at 1440 and 390 in both themes, checks local link resources and network
errors, and exercises read-only controls, transaction windows/search/filters and the four Ask examples. `verify.json`
records counts, a screenshot manifest, requests, checks, owned PIDs and cleanup. Screenshots/logs belong outside the repo.
`scripts/serve_demo.py dist/demo` can separately preview Cloudflare's `_headers`; the browser proof needs no such server.

## Weekly publication

`.github/workflows/demo-weekly.yml` replaces the local weekly start/report crons. It runs Mondays at 11:00 UTC
(03:00 PST, 04:00 PDT), or through **Run workflow**, and serializes runs. GitHub disables scheduled workflows after
60 days without repository activity. The owner must create repository Actions secrets `HF_PRIVATE_WORDS_CONTENT`
(the multiline publication word list), `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.

The workflow installs locked dependencies, runs all seven weekday variants and the combined demo, then deploys with
Wrangler 4.153.0 only after a zero exit and `DEMO_WEEKLY_OK`. It requires HTTP 200 from the published core JSON.
Public artifacts expire after seven days: only a redacted summary and scrubbed stage logs are eligible. Private-canary
logs, screenshots and word files are excluded; word files are deleted even after failure. Ubuntu Chrome uses a local
wrapper for runner sandbox flags. Linux text rendering can differ from the macOS browser checks; a failed verification
blocks publication and leaves the previous deployment in place.

## Cloudflare Pages publication (operator steps; no deployment performed)

After the private publication scan passes, upload the exact scanned directory:

```sh
npx wrangler pages deploy dist/demo --project-name hermes-finance-demo
```

Use `dist/demo` as the Pages output directory. For a Git build, use the cold-clone command above and perform the private
publication scan in an appropriately authorized release process before publishing. `_headers` supplies the CSP,
no-referrer, nosniff and noindex headers; the HTML also carries CSP for ordinary static servers. No Functions, Wrangler
runtime, credentials, periodic reset, health service or database is deployed. Roll back using a previous Pages deployment.

For your own hostname (for example `demo.example.com`), first add it in **Workers & Pages → hermes-finance-demo → Custom domains → Set up
a custom domain**. Then create/confirm the DNS record **CNAME `demo` → `hermes-finance-demo.pages.dev`** in the
`example.com` zone. Inspect existing records first and resolve conflicting A/AAAA/CNAME records through the operator's
normal review process; do not add a conflicting record. Wait for Pages domain/certificate validation. These are
instructions only: no authentication, deployment, DNS, service or gateway change is part of this build.
