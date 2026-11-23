# Release 0.4.0

This page is the source for the 0.4.0 pull request body: what ships, what was cut and why, the schema change from v21
to v24, and what an owner upgrading from 0.3.0 should know. It is a maintainer page, not part of the site.

0.3.0 is schema v20. The 0.4.0 branch adds four numbered schema steps (v21 SimpleFIN, v22 multi-currency, v23
categories, v24 category colour), and a v20 database goes through all of them on first start.

## What ships

- **SimpleFIN Bridge as a second bank connection (schema v21).** A read-only link beside Plaid, with no Plaid keys and
  outside Plaid's 10-connection limit. The setup token is traded once for an access link that lives only in the
  secrets file. A standard-library client, an **Add account → Continue with SimpleFIN** flow, a confirm-the-account-types
  step, a daily sync, pending-transaction matching, duplicate review against Plaid accounts, and a retry state. The
  sync panel and Bank connections name the provider. See [SimpleFIN setup](simplefin.md).
- **Multi-currency accounts (schema v22).** Every account and transaction carries its own currency. **Amounts are never
  converted:** a currency code decides how a number is written, nothing else. An account in a currency other than the
  Settings → Region display currency is listed apart (an **Other currencies** card, per-currency day totals, a budget
  "also" line, foreign rows on Investments and Loans & debt) and is in no one-number total. OFX and CSV imports keep
  each row's own code. Goals, alerts, the agent prompts and the Ask text write each amount in its own currency.
- **User-managed categories (schema v23, colour v24).** The 36 seeded categories are marked built-in once, by name; hand-made ones
  are yours. A Settings card above Categorization rules creates, renames, re-icons, hides, moves and
  move-and-deletes categories, with one shared 100-icon picker that Goals use too. The same operations are in
  `finance categories` and the agent tools (`finance_categories`, with approve-first create / update / delete).
  Hidden categories stay in every money total, and the AI never offers one. A category's icon colour follows its group
  unless you pick one of the eight goal swatches. See [Settings](settings.md#categories).
- **CLI brains.** **Settings → AI provider → CLI** runs a Claude Code or Codex CLI that is already signed in on the
  computer, as a plain text-completion endpoint: one process per request, no tools, no keys to paste. See
  [AI modes](ai-modes.md#cli-a-locally-installed-agent). The app finds the CLI under a service's minimal PATH too
  (`~/.local/bin`, `~/.claude/local`, the Homebrew folders and npm's global bin), or at a path you save.
- **Keyboard-operable transaction rows.** A row opens and closes from the keyboard, details close on navigation, and
  focus and scroll survive a sheet save that redraws the list.
- **Test and CI hardening.** A SimpleFIN sync reads only the `today=` it is given, so its tests no longer depend on the
  real clock. Every from-scratch test environment sets the Plaid and SimpleFIN offline switches, enforced by an AST
  guard test. The Chrome driver records evidence for a page that loaded but never drew, no longer leaks a singleton
  temp directory, and keeps presample state per navigation. The motion and layout-audit checks wait on state instead
  of fixed frame counts. The release scan gained a brand-name rule.

## What was cut

**PDF statement import and batch import are not in 0.4.0.** Four measured spikes tried to read bank-statement PDFs
into rows under a reconcile-or-refuse judge (a row set is returned only if the printed opening balance plus the rows
equals the printed closing balance). The gate was 7 of 8 held-out statements. None reached it:

- Spike 1, a layout parser on a synthetic corpus: 20 of 20 on the synthetic set, but 0 of 8 on real statements, and
  1 reconciled after tuning on those same files (so not an out-of-sample number).
- Spike 2, the same parser behind the reconcile-or-refuse judge: 0 of 6 clean held-out files (0 of 8 on the full set).
  It returned no row set that failed to reconcile, but arithmetic is not correctness: one statement's signs were
  reversed under the bank-account convention.
- Spike 3, Docling and the AI brain: AI brain 4 of 8 without the balance anchor, Docling 0 of 8; the anchored
  AI-brain number is unknown because the raw replies were not saved.
- Spike 4, Docling plus `claude-haiku-5-5`, balances anchored to the text layer, a fresh held-out set:
  **Docling plus Haiku (DA) reconciled 3 of 8; the text-layer-plus-Haiku control (AH) reconciled 2 of 8; the gate is
  7 of 8.** Neither arm returned a row the judge had not accepted (`wrong_rows_returned` is 0 in every run).

Per the owner rule, the PDF import card and the batch import card were dropped. The four spikes ship as reports and
tests only: `docs/pdf-import-spike.md`, `tests/test_pdfspike_*.py` and `tests/fixtures/pdfspike/`. They add no
product code path. CSV, OFX and QFX import stays as it was, plus the currency handling above.

## Schema v21 to v24

- **v21 (SimpleFIN).** Adds `items.provider` (`plaid` or `simplefin`), `accounts.conn_id` (the Bridge connection id),
  widens `transactions.source` to allow `simplefin` (SQLite cannot alter a CHECK, so the transactions table is rebuilt
  with every row, index, trigger and view kept), and adds `simplefin_requests`, the daily and manual request budget.
  A downgrade is not supported.
- **v22 (multi-currency).** Adds `manual_accounts.currency` and makes `v_spend_lines` carry `iso_currency`. The step
  fills what is empty and changes no amount: a manual account takes the display currency, and a transaction or balance
  snapshot with no code takes its account's currency (else the display currency). A code that is already there is never
  replaced.
- **v23 (categories).** Adds `categories.is_builtin` and `categories.is_hidden`. Exactly 36 rows become built-in, by
  name; every other category stays yours and visible.
- **v24 (category colour).** Adds `categories.colour`, empty for every existing row. Empty means the icon follows its
  group's colour; a set value is one of the eight goal swatches. No amount or filing changes.

Each step is idempotent and runs through `db.connect`, the app's own start. A read-only connection never upgrades a
database and refuses an old one with a clear message.

## Upgrade notes

1. **Back up first.** `finance backup --out <dir>` (see [Backup and restore](backup-restore.md)). The first start on 0.4.0
   upgrades the database in place, and a backup taken on 0.4.0 does not restore onto an older app.
2. **Nothing to configure for the upgrade itself.** A 0.3.0 database (v20), and a database from a branch build at v21 or
   v22, all end at v24 with every row kept. `tests/test_release_040_migration.py` proves this on a clean 0.3.0 schema
   and on both v21 shapes, and that all three end with the same tables, columns, indexes, views and triggers as a new
   database.
3. **Display currency.** It defaults to USD, so nothing changes for an all-USD household. If you set Settings →
   Region to another currency, accounts that are still in USD become "other currency" accounts: listed apart, in their
   own currency, and out of the net-worth and budget totals. Nothing is converted, by design.
4. **Refunds are never Income, except tax refunds.** At the first sync after updating, money in on a card or loan, and
   any line that says refund, return, reversal or credit voucher, moves from Income to Other when Plaid's category put
   it there, and nets against spending. Tax refunds stay Income. Your own filings, your rules and AI filings are kept.
   Money in on a SimpleFIN bank account is Income.
5. **SimpleFIN and the CLI brain are opt-in.** Neither runs until you set it up. The SimpleFIN access link is kept in
   the secrets file (`~/.config/hermes-finance/secrets.env`, or where `FINANCE_SECRETS_FILE` points), never in the
   database.
6. **Restart the app after updating** so the server and its generated reference pages (`docs/reference/*`) match the
   code. Updating the plugin or the catalog entry is a separate step.

## Verification at the release head

The figures below come from GitHub Actions run 45078494839 of the manual Nightly workflow (macOS runner, four
headless Chromes), which ran on commit `dd317ef`. The commit after it changes one expected count in a browser test
(see the end of this section), one code comment and this page, and no app code, so a checkout of `dd317ef`
reproduces them with:

```
uv run python scripts/check.py full --part 1   # every test but the gates; the browser files run with it
uv run python scripts/check.py full --part 2   # tests/test_gates.py: the numbers, layout and controls gates
python3 scripts/dehardcode.py --release-tracked
```

`full --part 1` took 32 minutes and `full --part 2` 53 minutes on the CI runner. Both run from the manual Nightly
workflow (Actions, Nightly, Run workflow, on macOS).

**The three gates** (`full --part 2`: 16 gate tests, 0 failed; all on synthetic databases built by
`tests/gate_dbs.py`, nothing real is read):

- **Numbers** (`scripts/audit_numbers.py`: every number the pages show, the API and the read-only CLI agree, to
  ±$0.01): passed on all six databases (full, nocaps, empty, history-empty, onepoint, features).
- **Layout** (`scripts/audit_layout.py`: five widths, both themes, every page and every modal, drawer and editor
  state): 5,220 states planned on eight databases. 4,509 were checked and 711 are reported **absent**, not checked,
  because their database lacks the thing to open (no bill with an estimate in a database without one, no Alpaca
  account outside the features database, and so on). The 4,509 checked states made 90,180 layout checks, 0 failed,
  and none was checked before it settled. The audit's self-test passed on the six databases that run it.
- **Controls** (`scripts/audit_controls.py`: every visible, enabled control on every page does something you can see
  when clicked, at 1440 and 390 px): passed on the full and nocaps databases, and the planted dead controls were
  caught on both (self-test ok).

**The rest of the suite** (`full --part 1`): 4,489 tests, 94 skipped: 3,564 without a browser (0 failed) and 925
browser tests (1 failed). The one failure was a stale expected count, not an app fault:
`tests/test_browser_task20.py::test_every_extension_runs_once` expected 16 front-end extensions, and the v24 Colour
row (`catcolour.js`) is the 17th, one file and one run each. The next commit sets the count to 17. Among them is the migration proof test above (`tests/test_release_040_migration.py`). The release scan
(`scripts/dehardcode.py --release-tracked`) is clean, and a case-insensitive search of the tracked tree for the former
product's name finds nothing.
