# User guide and reference

Everything `finance` does, in detail: every command, the access modes, the database, classification, manual accounts
and loans, car and property values, the agent commands and tools, the dashboard and its JSON API, and backups. Start
with [Getting started](getting-started.md); every key and setting is in [Configuration](configuration.md).

## Commands

All commands run as `uv run finance <cmd>` in the checkout. From another directory use
`uv run --project /path/to/hermes-finance finance <cmd>`.

| command | what it does |
|---|---|
| `serve [--port 8765] [--host 127.0.0.1] [--public-url URL] [--allow-user LOGIN]` | The [dashboard](#dashboard): Overview, Transactions, Budgets, Recurring, Accounts, Investments, Loans & debt, Settings, plus **Add account** (link a bank with Plaid, add accounts by hand, or import a CSV / OFX / QFX file), per-Item **Reconnect** and the slot counter. Who may open it is one of three [access modes](#access-modes); it refuses to start when it would be reachable from other devices with no login protecting it. Starts without Plaid keys too (the page opens on first-run setup). |
| `doctor [--json]` | Every integration (database, Plaid, AI, login, Alpaca, MCP, Carfax, Redfin, photos) and what each is missing, one line each with the README's Configuration section that fixes it. Offline and read-only, never prints a key's value; exits 1 when anything is missing. See [Configuration](configuration.md). |
| `backup --out PATH` | A consistent copy of the database without any credential (as Settings → Data → Download backup), 0600; `PATH` is a new file or a directory. Safe while the app runs. See [Backup and restore](#backup-and-restore). |
| `restore PATH [--force]` | Replace the database with a backup: checked first (integrity, schema), the old database moved aside (never deleted; `--force` when it holds data), migrations run, then what to redo. Stop `finance serve` first. See [Backup and restore](#backup-and-restore). |
| `settings [--json]` | The Settings page's values, read-only: sync time, weekly summary, which alerts are on and where they go, AI mode, MCP, valuations, region, theme, access mode. For the crons. |
| `access [mode local\|proxy\|builtin \| allow LOGIN \| disallow LOGIN \| reset-login]` | Show or change the [access mode](#access-modes) and the proxy's allowed identities (masked when shown). `reset-login` removes the built-in login and goes back to "This computer only": the way back in when you're locked out. |
| `sync [--item ID]` | For every non-removed Item: `/transactions/sync` from the stored cursor (all pages), `/accounts/get` balances, plus `/liabilities/get` and `/investments/holdings/get` if the Item has those products. Then runs `classify-apply`, and fetches the logo of any held stock/ETF/fund not cached yet (see [Logos](#logos); a failure there never fails the sync). Prints one line per Item, e.g. `Chase: +12 ~3 -1 \| balances ok`. Exits 1 only if **every** Item failed. |
| `status [--json]` | Items, accounts, balances, last sync, error states, slots used. |
| `query "<SQL>" [--json]` | Read-only SQL (`mode=ro` + `query_only` + an authorizer that allows only reads). `items.access_token` always reads as NULL. |
| `export --since YYYY-MM-DD [--csv PATH]` | Transactions from `v_transactions`: JSON to stdout, or CSV to PATH (`-` = stdout). |
| `classify-apply` | Deterministic: apply `rules`, then Plaid-mapped defaults, to everything not decided by ai/user. |
| `unclassified [--limit N] [--since D] [--json]` | Transactions still needing an AI/human decision, plus the list of valid categories. |
| `set-category <txn_id> <category> [--source ai\|user] [--confidence x] [--note s] [--make-rule]` | Record a decision (default source `user`). `--make-rule` also creates an exact rule on that merchant (or name) and applies it. |
| `add-rule --pattern P --category C [--field merchant_name\|name] [--regex] [--priority N] [--created-from user\|ai\|…]` | Create or update a rule and apply it. A user rule (the default) re-files matching ai rows too and defaults to priority 200; other rules default to 100 and can't replace a user rule. |
| `ai-queue [--max-groups N]` | Save a run of transactions that still need an AI look, grouped by merchant (oldest first, default 250 groups), and print it as compact JSON. See [Daily AI classification](#daily-ai-classification). |
| `ai-apply FILE [--dry-run]` | Apply the AI's decisions for a run: validated, in one SQLite transaction, once per run. Prints the summary JSON. |
| `manual list [--json] [--archived]` | [Manual accounts](#manual-accounts-and-loans) (what Plaid can't see) with their signed values, freshness and loan progress, plus totals. |
| `manual add --kind K [--name] [--institution] [--subtype] [--balance] [--credit-limit] [--interest-rate] [--original-amount] [--monthly-payment] [--match-pattern] [--vehicle-make] [--vehicle-model] [--vehicle-year] [--vin] [--vehicle-trim] [--mileage] [--zip] [--condition] [--address] [--loan ID] [--as-of YYYY-MM-DD]` | Add one. `K` is `bank`, `credit_card`, `investment`, `loan`, `vehicle`, `property`, `other_asset` or `other_liability`; each kind takes exactly the fields its dashboard form has. A vehicle's VIN is decoded and it (or a property with an address) gets its first estimate, as on the dashboard (see [Car and property values](#car-and-property-values)); `--loan` links the manual loan it's financed by (its equity). |
| `manual edit ID [field flags] [--clear FIELD] [--use-estimate]` | Change fields. A new `--balance` (or `--as-of`) is a new entered balance, true on `--as-of` (default today); for a vehicle / property it's "Your value", which wins over the estimate until `--use-estimate`. |
| `manual archive ID` | Hide it from every total and list (the row and its history are kept). |
| `match-payments [--dry-run] [--json]` (alias `loan-match`) | For every active loan **and credit card** with a match pattern, apply each new posted bank payment that matches it (`source=auto`) and print what was (or would be) applied, what was skipped and why. Never a payment that pairs with a connected card's own payment line; card payments wait until they're 5 days old. Run it after `finance sync`. |
| `match-payments --suggest [--json]` | For each manual credit card without a pattern: the posted bank payments since its balance date that mention it (its name, its card company or the issuer's other names, e.g. CITI / CBNA), look like a card payment and pay no connected card, each with the pattern to use. Suggestions only: nothing is saved (the daily run posts them). |
| `loan-apply TXN_ID LOAN_ID [--source ai\|user] [--dry-run]` | Apply one specific transaction to a loan (for a payment the pattern misses). |
| `loan-unapply TXN_ID [--dry-run]` | Take a transaction back off its loan; the balance is exactly what it would be without it. |
| `revalue [--account N] [--dry-run] [--json]` | [Car and property values](#car-and-property-values): ask Carfax (cars) and Redfin (property) for each active vehicle / property due for its monthly estimate (30+ days since its last lookup), or `--account N` now (at most once a day). Every call goes through the free-tier guard. `--dry-run` calls and counts nothing. Exits 1 when a lookup failed (the daily run carries on). |
| `photos [--account N] [--force] [--json]` | The [car / property pictures](#the-car--property-page) that are due (never fetched, or a try that found nothing 30+ days ago / failed yesterday), or `--account N` now; `--force` fetches again even if it has one. Within the Commons / Redfin / OpenStreetMap caps. |
| `api-usage [--json] [--unblock PROVIDER]` | Outbound calls this month per provider (NHTSA, Carfax, Redfin, Wikimedia Commons, OpenStreetMap): used / cap, the last 32 days and any block. `--unblock` lifts a block by hand (the cap still applies). |
| `refresh-institutions` | Look up every connected bank with Plaid's free `/institutions/get_by_id` (never creates an Item) and cache its logo and colour: Plaid's logo, else the bank's own icon fetched from its site, else nothing (the page draws initials in the bank's colour). Also runs by itself when a bank is connected, and `finance sync` looks up again, once a day, each bank still without a logo. |
| `refresh-logos [--force]` | `refresh-institutions`, plus the logos of held stocks, ETFs and funds not cached yet (`--force`: fetch them all again). |
| `networth-custom [--exclude ID...] [--include ID...] [--reset \| --preset cash-and-debts] [--list] [--json]` | List (the default) or change which accounts the **Custom** net worth counts: the same saved choice as the Accounts page's Edit sheet. IDs are the `nw_id`s of `networth --json` (a Plaid `account_id`, or `manual:<id>`); `--reset` counts everything; `--preset cash-and-debts` starts again from cash minus every debt. An unknown ID exits 2 and saves nothing. |
| `spend [filters] [--by category\|merchant\|month\|account] [--json]` | [Read-only](#agent-read-commands): real spending (not income, not transfers) with its count and pending part, compared with the previous equal period, optionally grouped. |
| `budget-status [--month YYYY-MM] [--json]` | Read-only: each cap's spent, bills still due, left, % used, status and pace projection, plus uncapped spend and the totals (`left` = budgeted − spent − bills still due, negative when over). |
| `txns [filters] [--search TEXT] [--flagged] [--uncategorized] [--json]` | Read-only: transactions, newest first, with category, source, account, pending and AI flag. |
| `networth [--history] [--custom] [--json]` | Read-only: assets, debts and net, grouped as on the Accounts page (manual accounts included; a vehicle / property with its value, where it comes from and as of when), and the daily points. `--custom`: the page's **Custom** net worth, only the accounts you count (see [Net worth: All · Custom](#net-worth-all--custom)); `--json` carries the `custom` block and each account's `nw_id` / `counted` either way. |
| `bills [--next 14d] [--json]` | Read-only: recurring bills and subscriptions expected in the next N days (overdue ones included), with status; a bill whose amount changes at its estimate ("~$125 est.", usually $100–150), found or tracked by hand, last paid from. `bills track MERCHANT [--every 2m] [--varies\|--same] [--usually 100-150]` / `bills untrack MERCHANT` write (see [Bills](#bills-any-schedule-amounts-that-change-tracked-by-hand)). |
| `loans [--json]` | Read-only: loans with remaining, APR, payment, projected payoff and the last matched payment. |
| `holdings [--json]` | Read-only: investment accounts and holdings with value and % of the portfolio. |
| `cashflow [--months 6] [--json]` | Read-only: income vs spending per month. |
| `mcp` / `mcp status [--json]` / `mcp allow-writes on\|off` | The [MCP server](#agent-tools-hermes-plugin-and-mcp-server) on stdio (the `finance_*` tools, read-only; needs `uv sync --extra mcp`); what it allows, who used it last and the changes waiting; whether MCP clients may ask for changes (each one is only saved as a pending action). |

Bad input to `manual` / `loan-*` exits 2 with a message and writes nothing.

`-v` before the command (e.g. `finance -v sync`) logs progress to stderr.

Scheduling is up to the orchestrator: run `finance sync` periodically (and `finance match-payments` after it,
so loans and cards with a match pattern keep up with their payments; `finance match-payments --suggest` lists a
new card-payment line to post for a card that has no pattern yet, and `finance revalue` after that refreshes the
monthly car / property estimates). This repo does not install any launchd/cron jobs; `scripts/finance_daily_prerun.sh`
is an example daily pre-run (opt in with `FINANCE_DAILY_ENABLE=1`), with the `revalue` step after the
loan payments; the Hermes plugin can schedule the reports ([Scheduled reports](plugin-crons.md)). The weekly summary reads `finance budget-status --json`: its `totals.left` is now the net (see Budgets).

## Connecting a bank / reconnecting

1. `uv run finance serve`, open http://127.0.0.1:8765/
2. **Connect a bank** (sidebar footer on every page, or the Accounts page) → a confirmation showing the
   slots left → Plaid Link (popup; OAuth banks work without a redirect URI).
   Requested products: `transactions` (730 days of history); `liabilities` and `investments` are
   optional and are added only if the bank supports them.
3. On success the server runs the duplicate guard, then:
   - exchanges the token,
   - appends it to `data/tokens.backup.jsonl` (0600, fsync'd) before anything else can fail,
   - stores the Item in the DB,
   - runs the first sync.

   Plaid often needs a few minutes to pull 730 days of history. If the first sync shows few or no
   transactions (`plaid status NOT_READY` / `INITIAL_UPDATE_COMPLETE`), just run `finance sync` later.
4. If an Item shows `login_required` (or any error) it gets a red dot and a **Reconnect** button (sidebar,
   Overview alerts, Accounts page). Click it. When Link
   finishes, the page tells the server, which clears the error and re-syncs. There's no exchange and
   no new Item.

## Access modes

Settings → Access & security (or `finance access`) picks one:

- **This computer only** (a new install's default): the server binds 127.0.0.1 and refuses every proxied or
  foreign-Host request (and says why in the log).
- **Behind your proxy login**: Tailscale Serve, Cloudflare Access, Authelia or oauth2-proxy signs you in on this
  computer and forwards requests with the identity in a trusted header (default `Tailscale-User-Login`, a setting).
  Requests for a public URL must carry an allowed identity. This is the Tailnet setup below; an install started with
  `--public-url` becomes this mode on its first start (its `--allow-user` logins seeded into the setting, once). It
  refuses a non-loopback `--host`: anyone reaching the port directly could send the header.
- **Built-in login**: for public hosting (behind an HTTPS proxy, or bound to another address): a username, a password
  (argon2id) and a required authenticator app (TOTP, RFC 6238), 8 one-time recovery codes, sessions in an
  `HttpOnly; Secure; SameSite=Strict` cookie (idle 8 h, at most 7 days), a CSRF token on every write, and 5 wrong tries
  = a 15-minute lock per user and per address. It's set up on the Settings page (a password, the QR code, the recovery
  codes); `finance access reset-login` removes it.

`finance serve` refuses to start when the app would be reachable from other devices (a non-loopback `--host`, or public
URLs) and neither the proxy mode (with an allowed identity) nor a finished built-in login protects it.

**MCP tokens** (Settings → Connect other agents): per-client bearer tokens (`hfm_` + 32 random bytes, shown once,
stored hashed) for `/mcp` through the proxy or with the built-in login (`Authorization: Bearer hfm_...`); on this
computer in "This computer only" `/mcp` stays open as before. A token never opens `/api/*`. The MCP switch there turns
`/mcp` and `finance mcp` off.

**Keys** pasted in the app (first-run setup, Replace keys, the AI key, Alpaca's) go to a secrets file, 0600 in a 0700
directory: `~/.config/hermes-finance/secrets.env` (`FINANCE_SECRETS_FILE` moves it). Its values win over the env file,
which keeps working as before (`HERMES_ENV_FILE` / `~/.hermes/.env`). No key is ever sent back to the browser or logged,
and **Download backup** (Settings → Data) never includes this file: it's a consistent copy of the database with every
credential taken out (bank tokens, the login, sessions, MCP tokens), so after a restore you reconnect each bank.

## Tailnet access

To open the page from your phone or laptop, put Tailscale Serve in front of the local server.
`finance serve` itself still binds loopback only. It refuses a non-loopback `--host` and tells you
to use `tailscale serve` instead.

```sh
tailscale serve --bg --https=8765 http://127.0.0.1:8765
uv run finance serve --public-url https://my-machine.tail0a1b2c.ts.net:8765
```

Then open https://my-machine.tail0a1b2c.ts.net:8765/ in **Safari or Chrome**, not in Discord's
or another app's built-in browser. Bank logins (OAuth) open in a pop-up or new tab, so allow
pop-ups. There's no Plaid `redirect_uri`; web OAuth doesn't need one. (The page itself no longer shows
a reminder about this.)

Serve flags (both repeatable; merged with the comma-separated env vars):

| flag | env var | meaning |
|---|---|---|
| `--public-url URL` | `FINANCE_PUBLIC_URLS` | The `https://host[:port]` URL that Serve exposes. Requests with that Host are accepted (with or without the port). |
| `--allow-user LOGIN` | `FINANCE_ALLOWED_USERS` | Tailscale login allowed on the public URL (case-insensitive). There's no default: the first start with `--public-url` saves these as the allowed identities of "Behind your proxy login", and without one the server refuses to start. Add or remove them later in Settings → Access & security, or with `finance access allow / disallow`. |

Header checks:

- **Host.** Localhost-style Hosts (`127.0.0.1`, `localhost`, `[::1]` with the port) work exactly as
  before. A Host matching a `--public-url` is a tailnet request. Anything else gets 403.
- **Identity** (tailnet requests only). The `Tailscale-User-Login` header must be present and be one
  of the allowed users, otherwise 403 with a message explaining why. Serve sets this header for
  logged-in tailnet users and replaces any copy the client sends. It can be trusted only because
  the server listens on loopback, so only local processes and Serve can reach it.
- **Origin** (sent on fetch/POST). It must exactly match `http://127.0.0.1:<port>`,
  `http://localhost:<port>`, `http://[::1]:<port>`, or a public URL's origin (e.g.
  `https://my-machine.tail0a1b2c.ts.net:8765`). Anything else gets 403, including the right
  host over `http://`.
- Requests arriving via Serve are never treated as localhost. Any `X-Forwarded-*`, `Forwarded` or
  `Tailscale-User-*`/`Tailscale-Headers-Info` header plus a localhost Host gets 403. These headers never grant access.

Turn it off:

```sh
tailscale serve --https=8765 off
```

## Database (`data/finance.db`)

### Sign convention

`transactions.amount` follows Plaid's convention: positive = money **out** (purchases, fees,
payments), negative = money **in** (paychecks, refunds, deposits). This holds for every account
type: on a credit card, a purchase is positive and a card payment is negative. In the views,
`spend = amount`, so `SUM(spend)` is net spending and a negative total means net inflow.

### Tables

| table | contents |
|---|---|
| `items` | One row per Item ever exchanged (never deleted). `status` (`active` / `login_required` / `error`), `error_code`, `error_message`, `cursor`, `products` (JSON), `txn_update_status`, `created_at`, `last_sync_at`, `removed_at`. `access_token` is here but hidden from `finance query`. Use `v_items` for a token-free view. |
| `accounts` | name, official_name, mask, type/subtype, current/available balance, credit_limit, currency. |
| `transactions` | Full Plaid fields + `raw_json`. `pfc_*` = Plaid personal_finance_category (on an imported OFX money-in row, the income code its `TRNTYPE` means: `INCOME_WAGES` or `INCOME_INTEREST_EARNED`; the type itself is in `raw_json`). `removed=1` means Plaid removed it; the row is kept but excluded from views. |
| `categories` | Rent & Utilities, Groceries, Eating Out, Transport, Shopping, Subscriptions, Entertainment, Health, Travel, Income (`is_income`), Transfers (`is_transfer`), Fees, Other. `monthly_cap` is the budget (NULL = none). |
| `plaid_category_map` | Plaid PFC code (detailed or primary) → category. Detailed wins, e.g. `FOOD_AND_DRINK_GROCERIES` → Groceries, but other `FOOD_AND_DRINK` → Eating Out. Card/loan payments and transfers → Transfers. Unknown → Other. Plaid has no "subscription" category, so Subscriptions is filled by rules/AI. |
| `classifications` | One row per transaction: `category_id`, `source` (`plaid`/`rule`/`ai`/`user`), `confidence`, `note`, `rule_id`. |
| `rules` | `match_field` (`merchant_name`/`name`), `match_type` (`exact` = case-insensitive equality; `regex` = case-insensitive Python `re.search`), `pattern`, `category_id`, `priority` (higher wins; ties go to the older rule), `created_from`. |
| `holdings`, `liabilities` | Snapshots from the investments / liabilities products (key fields + `raw_json`). |
| `sync_log` | One row per Item per sync: counts, error, details (balances/liabilities/investments outcome). |
| `link_attempts` | Every Link completion the server saw: `exchanged`, `duplicate_blocked`, `rejected`. |
| `balance_snapshots` | `(account_id, date, current, available, iso_currency)`, PK `(account_id, date)`. One row per account per local day, upserted at the end of every successful sync. Net worth history comes only from these (never back-filled). |
| `ai_runs` | One row per `ai-queue`: `run_id` (`ai-YYYYmmdd-HHMMSS`, UTC), `created_at`, `applied_at` (NULL until applied), `queued_groups`, `queued_txns`, `summary_json` (the `ai-apply` output). |
| `ai_run_groups` | The queue snapshot, PK `(run_id, group_id)`: `merchant`, `rule_field`, `rule_pattern`, `transaction_ids_json`. `ai-apply` only touches transactions listed here. |
| `ai_reviews` | One row per transaction the AI has looked at (PK `transaction_id`): `run_id`, `reviewed_at`, `verdict` (`changed` / `confirmed` / `flagged`), `category_id` (after the review), `confidence`, `note`. |
| `manual_accounts` | What Plaid can't see, entered by hand: `kind` (`bank`, `credit_card`, `investment`, `loan`, `vehicle`, `property`, `other_asset`, `other_liability`), `name`, `institution`, `subtype`, `balance` (always ≥ 0; liabilities are subtracted by kind), `credit_limit`, `interest_rate` (APR %), `original_amount`, `monthly_payment`, `match_pattern` (loans and credit cards), `vehicle_make/model/year`, `balance_as_of` (the day the entered balance was true), `created_at`, `updated_at`, `archived_at` (archived rows leave every total). v7: `vin`, `vehicle_trim`, `mileage`, `mileage_as_of`, `zip_code` (vehicles), `address` (property), `vin_decoded` (the VIN NHTSA last decoded), `value_source` (`user` = the typed value wins, `estimate` = the balance follows the estimate). v8: `vehicle_condition` (`fair` / `good` / `excellent`, what Carfax prices; `good` by default). v9: `linked_loan_id` (a vehicle / property: the manual loan it's financed by; its equity counts only that loan). |
| `asset_valuations` | One row per successful estimate: `account_id`, `value` (Carfax: the trade-in; Redfin: the Estimate), `source` (`carfax` / `redfin`), `miles`, `subject` (the VIN / address valued), `fetched_at` (UTC), `raw_json` (the response, trimmed), `details_json` (v8: what the page shows beside the value: Carfax private sale / dealer retail / records / odometer / specs / condition; Redfin beds, baths, sqft, last sold, location, listing photo). Rows from the providers used earlier (`marketcheck` / `rentcast`, with their `low` / `high`, `flag`, `confirmed_at`) are kept as history but never shown, used or charted. |
| `api_usage` | The free-tier guard, PK `(provider, month)`: `calls` this local month (counted before each call, failed ones too), `blocked_until` / `blocked_reason` (NHTSA's refusal stops it until the 1st of next month; a bot wall stops Carfax / Redfin for 7 days, OpenStreetMap for 7, Commons for 1). Rows of retired providers are left as history. |
| `api_calls` | One row per outbound call, reserved before it's sent: `provider`, `account_id` (NULL for a picture: it's shared), `trigger` (`save` / `refresh` / `daily`; `seed` on old rows), `at`, `day`, `status`, `outcome` (`ok` / `error` / `timeout` / `blocked` / `none`: answered, no estimate for this home), `note` (Redfin: `via https` / `via chrome`, `photo`, `image`). The per-account limits and the 32-day window read it. |
| `asset_images` | v9, the car / property pictures, PK `key` (`vehicle:<year>\|<make>\|<model>`, shared by every account with that car; `property:<address>`): `kind`, `status` (`ready` / `none`: nothing usable, tried again after 30 days / `error`: tried again the next day), `source` (`commons` / `redfin` / `osm`), `file` (in `data/images/`), `cutout` (background removed), `width`, `height`, `credit` / `credit_url` / `artist` / `license` (shown under it), `lat` / `lng` / `pin_x` / `pin_y` (a map: the home and where it is on the tile), `note`, `fetched_at`. |
| `manual_balance_history` | `(manual_account_id, date, balance)`, PK both: the entered balance on its `balance_as_of`, plus a loan's balance after each applied payment. Net worth history reads these. |
| `loan_payments` | A bank transaction applied to a manual loan or credit card, PK `transaction_id` (one account, once): `loan_id` (the manual account), `amount`, `interest`, `principal` (a card payment is all principal), `applied_at`, `source` (`auto` / `ai` / `user`). |
| `settings` | Dashboard preferences every device shares, PK `key`, `value` (JSON), `updated_at`. `chart_excluded_categories`: the categories hidden from the Overview's "Where it went" chart. |
| `holding_snapshots` | `(date, account_id, security_id, ticker, quantity, price, value)`, PK `(account_id, security_id, date)`: one row per security per day, written after a sync whose holdings call succeeded. The portfolio chart comes only from these (never back-filled). |
| `institutions` | Bank logo cache, PK `institution_id`: `name`, `logo_png` (the checked image bytes: PNG, or the site's JPEG/GIF/WebP/ICO), `logo_source` (`plaid` / `site` / `none`), `primary_color`, `url`, `fetched_at`. |
| `security_logos` | Holding logo cache, PK `ticker`: `png` (NULL on a miss), `source` (`fmp` / `none`), `fetched_at`. A miss is retried after 30 days. |

`SCHEMA_VERSION` is 9 (v2 added `balance_snapshots`, v3 the three `ai_*` tables, v4 the manual accounts, loan payments and holding snapshots, v5 the two logo caches, v6 `settings`: dashboard preferences shared by every device, one JSON value per key; today only `chart_excluded_categories`; v7 the valuation columns, `asset_valuations`, `api_usage`, `api_calls`, `vin_decodes`; v8 Carfax / Redfin in `asset_valuations` (rebuilt with every row kept: SQLite can't change a CHECK) and `vehicle_condition`; v9 `linked_loan_id` and `asset_images`: the upgrade links a vehicle to its loan when there is exactly one active vehicle and one active auto loan, a property when there is one property and one mortgage, and nothing otherwise). Upgrades keep every row: `db.connect` applies
the schema with `CREATE ... IF NOT EXISTS` and recreates the views when `PRAGMA user_version` is behind,
so the live DB upgrades itself on the next `finance serve` / `finance sync` without touching existing rows.

### Classification precedence

The write order is **user > user-rule > ai > ai-rule > plaid** (`classifications.source` is `rule` for
both kinds of rule; `rules.created_from` tells them apart):

- `user` rows are never overwritten. A DB trigger enforces this, and `set-category --source ai`
  on a user row is refused (exit 1).
- A user rule (`created_from = 'user'`: the dashboard's "Always file it here", `add-rule` by default)
  re-files every matching `plaid`, `rule` or `ai` row as soon as it's saved, and defaults to priority
  200. Nothing from the AI replaces it: an AI rule on its key is a no-op, and `set-category --source ai`
  on a row it filed is refused (exit 1).
- `ai` rows are otherwise never touched by rules or `classify-apply`.
- `classify-apply` (which also runs after every sync and after each new rule) rewrites only rows
  that are missing, `plaid`, or `rule`. The best-matching rule wins (highest priority, ties to the
  older rule), otherwise the Plaid-mapped default. AI rules default to priority 100.
- A pending transaction's user/ai category carries over to its posted version.

### Views

- **`v_transactions`**: non-removed transactions joined with account and institution, plus the
  effective category. The effective category is the classification row if there is one, else the
  live Plaid-mapped default, else Other. Columns include `category`, `category_source`, `is_income`,
  `is_transfer`, `spend` (positive = spend) and `month` (`YYYY-MM`), plus the AI review:
  `ai_verdict`, `ai_note`, and `ai_flagged` (1 = flagged by the AI with no user decision since).
- **`v_monthly_category_spend`**: `month, category, spend, cap, over_cap, txn_count, is_income, is_transfer`.

Example queries:

```sh
uv run finance query "SELECT month, category, spend, cap, over_cap FROM v_monthly_category_spend WHERE month = strftime('%Y-%m','now') AND is_transfer = 0 AND is_income = 0 ORDER BY spend DESC"
uv run finance query "SELECT date, name, spend, category FROM v_transactions WHERE category_source = 'plaid' ORDER BY date DESC LIMIT 20" --json
```

## AI layer seam

```sh
uv run finance unclassified --limit 50 --json      # {count, categories[], transactions[]}
uv run finance set-category <txn_id> Subscriptions --source ai --confidence 0.9 --make-rule
uv run finance add-rule --field name --regex --pattern 'spotify|netflix|hulu' --category Subscriptions --created-from ai
```

`unclassified` returns transactions with no user/ai/rule decision (only a Plaid default, or
nothing). It includes the Plaid suggestion, its confidence, and `original_description`.

## Daily AI classification

Hermes, the LLM agent in a daily cron, is the classifier; the app never calls an LLM. It gives Hermes a
queue, applies the decisions it hands back (validated, in one SQLite transaction), and records what the AI
reviewed in `ai_reviews`, so every transaction is looked at **once**. The loop, after `finance sync`:

```sh
uv run finance ai-queue > queue.json                # saves a run (ai_runs + ai_run_groups), prints JSON
# Hermes reads queue.json and writes decisions.json for the same run_id
uv run finance ai-apply decisions.json --dry-run    # optional: the full outcome, then rolled back
uv run finance ai-apply decisions.json              # applies it, once; prints the summary
```

### What gets queued

Every transaction that is posted (`pending = 0`; pending rows wait until they post), not removed, filed
only by Plaid (or not at all), and not yet in `ai_reviews`. Rows are grouped by merchant (`merchant_name`,
else `name`, case-insensitive). The oldest groups come first, at most `--max-groups N` (default 250) per
run, so a backlog drains over several days; `truncated: true` means more groups are waiting. An empty queue
still creates a run (`queued_groups = 0`, `groups: []`).

```json
{"run_id": "ai-20260926-063000", "created_at": "2026-09-26T06:30:00Z",
 "categories": [{"name": "Subscriptions", "is_income": false, "is_transfer": false}, "..."],
 "queued_txns": 3, "truncated": false,
 "groups": [{
   "group_id": "g1", "merchant": "OpenAI", "rule_field": "merchant_name", "rule_pattern": "OpenAI",
   "names": ["OPENAI *CHATGPT", "openai"], "count": 3, "spend": {"min": 5.0, "max": 20.0, "sum": 45.0},
   "current": {"Shopping": 3}, "pfc": {"GENERAL_MERCHANDISE_ONLINE_MARKETPLACES": 3},
   "accounts": ["Chase / Checking"], "dates": ["2026-06-01", "2026-07-02"],
   "samples": [{"transaction_id": "…", "date": "2026-07-02", "name": "openai", "spend": 5.0,
                "account": "Chase / Checking", "pfc_detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
                "category": "Shopping"}],
   "existing_rule": null, "history": {"Subscriptions": 1}, "history_sources": {"user": 1}}]}
```

- `spend` is positive for money out. `current` counts the rows' categories now, `pfc` their Plaid
  detailed codes (an imported row's: its OFX type's income code, else none).
- `rule_field` / `rule_pattern` is what a `rule` decision matches: the group's most common
  `merchant_name`, else `name` (the same choice as `set-category --make-rule`).
- Up to 5 raw `names`, 3 `accounts` and 8 `samples`: the cheapest and dearest rows (refunds, outliers),
  the latest row of each name, then the most recent. A `split` can only name transactions it has seen.
- `existing_rule` is `{category, created_from}` when an exact rule already exists for that field and
  pattern.
- `history` / `history_sources` show how this merchant's already-decided rows (ai/user/rule) are filed,
  so decisions stay consistent.

### Decisions

```json
{"run_id": "ai-20260926-063000", "decisions": [
  {"group_id": "g1", "action": "rule",    "category": "Subscriptions", "confidence": 0.95, "note": "AI tool subscription"},
  {"group_id": "g2", "action": "confirm", "confidence": 0.9},
  {"group_id": "g3", "action": "set",     "category": "Transfers", "confidence": 0.95, "note": "micro-deposit verification"},
  {"group_id": "g4", "action": "split",   "items": [{"transaction_id": "…", "category": "Health", "confidence": 0.9, "note": "refund of a clinic visit"}], "rest": "confirm"},
  {"group_id": "g5", "action": "flag",    "note": "can't tell what this is"}
]}
```

| action | effect |
|---|---|
| `rule` | Saves an exact AI rule on the group's `rule_field` / `rule_pattern` (priority 100, `created_from = 'ai'`) and runs `classify-apply`. Group rows the rule doesn't file (e.g. the ones with no `merchant_name`) get a per-transaction ai category instead. If a **user** rule already has that key, nothing changes (`kept_user_rule`) and the rows are recorded as confirmed. |
| `set` | A per-transaction ai category, confidence and note for every row in the group. |
| `split` | A per-transaction ai category for each listed item (its `transaction_id` must be in the group); the other rows get `rest`: `confirm` (default) or `flag`. |
| `confirm` | No change; recorded as confirmed. |
| `flag` | No change; recorded as flagged with the note, which puts the rows in **Needs review**. |

- `category` must be an existing category (case-insensitive). `confidence` is 0 to 1 and required for
  `rule`, `set` and split items. **Below 0.7 they're downgraded to a flag** (listed in `downgraded`, with
  a note such as `low confidence (0.55) for Health: …`).
- User rows, and rows filed by a user rule, are never changed: they count in `kept_user` /
  `kept_user_rule` and are recorded as confirmed.
- Only the run's queued transactions can be touched. An unknown `run_id` or an already-applied run is
  refused (exit 2), so applying is idempotent.
- Every transaction in a **decided** group gets an `ai_reviews` row: `changed` (its category moved),
  `confirmed`, or `flagged`. Groups without a decision, and the rows of invalid split items, stay
  unreviewed and come back in the next queue. Rows Plaid removed, or that another run reviewed since this
  queue was made, are skipped.
- Invalid decisions (unknown group or action, unknown category, confidence out of range, a missing field,
  a second decision for one group) are listed in `errors`; the valid ones still apply. A malformed file
  (bad JSON, no `decisions` list, no `run_id`) exits 2 and changes nothing.
- The whole apply is **one SQLite transaction**: any unexpected error rolls everything back and exits 1.
  `--dry-run` computes the full outcome, then rolls it back.

### Summary

Printed to stdout and stored in `ai_runs.summary_json` (except on `--dry-run`):

```json
{"run_id": "ai-20260926-063000", "dry_run": false, "groups_decided": 5, "txns_changed": 6,
 "by_action": {"rule": 1, "set": 1, "split": 1, "confirm": 1, "flag": 1},
 "changes": [{"group_id": "g1", "merchant": "OpenAI", "from": {"Shopping": 3}, "to": "Subscriptions", "n": 3, "action": "rule"}],
 "flagged": [{"group_id": "g5", "merchant": "ACH VERIFY", "n": 1, "note": "can't tell what this is"}],
 "kept_user": 0,
 "kept_user_rule": [{"group_id": "g6", "merchant": "Acme Rentals", "rule_id": 7, "category": "Rent & Utilities", "n": 2}],
 "downgraded": [{"group_id": "g7", "action": "set", "category": "Health", "confidence": 0.55, "transaction_id": null}],
 "errors": [{"index": 7, "group_id": "g9", "error": "unknown group_id 'g9' for this run"}],
 "skipped": [{"group_id": "g4", "transaction_id": "…", "reason": "removed"}],
 "unreviewed_groups": ["g8"]}
```

`changes` lists the rows whose category moved (`txns_changed` counts them). `by_action` counts decided
groups by what actually ran, so a downgraded `rule` or `set` counts as `flag`. Split item errors also
carry `item` and `transaction_id`.

### Precedence

**user > user-rule > ai > ai-rule > plaid**

1. **user**: a per-transaction decision from the dashboard or `set-category`. Never overwritten.
2. **user-rule**: a rule with `created_from = 'user'` ("Always file it here", `add-rule`). The user's
   explicit "always" beats an AI guess: saving it re-files the matching `plaid`, `rule` and `ai` rows, and
   it defaults to priority 200. `add_rule(created_from="ai")` on its key is a no-op.
3. **ai**: a per-transaction AI decision (`ai-apply` `set` / `split`, `set-category --source ai`).
4. **ai-rule**: any other rule (an `ai-apply` `rule` decision), priority 100.
5. **plaid**: the Plaid-mapped default.

**Needs review** (the transactions `review` filter, `/api/counts`, the sidebar badge and the "Needs review
(n)" chip, and `finance txns --uncategorized` / `--flagged`) is what nobody has decided: category Other
that nobody decided (not confirmed or changed to Other by the AI, not filed there by you or one of your
rules), **or** no category at all, **or** flagged by the AI with no user decision since (a user
classification, or a user rule re-filing the row). Each `/api/transactions` row carries the AI's note as
`ai_note` (null when there isn't one).

## Manual accounts and loans

Loans, cards and assets Plaid can't reach (a car loan, a car, a house, a 401(k) at another firm) are added
by hand: the dashboard's **Add account → Add manually / Add a property / Add a vehicle**, or `finance manual
add`. They count in net worth everywhere (overview, sidebar, accounts, history) and show a `manual` tag;
a balance older than 45 days gets a `stale` tag, except a loan or card with a match pattern.

A loan with **Match payments from** (a case-insensitive piece of the bank transaction's name or merchant,
e.g. `ZENITH ACH RTL`) pays itself down: `finance loan-match` applies every new posted outflow that matches.
The rules:

- Only transactions dated **after** the loan's `balance_as_of` count (the entered balance already includes
  earlier ones), a transaction goes to one loan once, and pending ones wait until they post.
- With an APR, interest = `round(balance_before × APR / 100 / 12, 2)` and principal = payment − interest;
  without one, the whole payment is principal (the page says so). The balance never goes below $0; at $0
  the loan shows as paid off.
- The balance, the splits and the history after `balance_as_of` are rebuilt from the entered balance and
  the applied payments in date order, so apply / unapply in any order ends at the same exact balance.
- Payoff: amortized month by month when the APR is set, else remaining ÷ payment (rounded up), counted
  from the next expected payment (a month after the last matched one).

A **credit card** takes **Match payments from** too (same validation: at least 3 characters), and
`finance match-payments` applies its payments the same way (each one all off the balance; the payments live in
`loan_payments` with the card as `loan_id`). What's different for a card:

- **Shared labels.** A card payment's label is often shared with cards Plaid does see: "CITI CARD ONLINE" pays
  the connected Citi cards too. An outflow that **pairs** with a same-amount inflow on a
  connected credit card within ±5 days (one to one: two $250 payments and one $250 card credit make one pair) is
  that card's payment and is never applied, to a card or a loan. A card payment is only applied once it's 5
  days old (when the connected side would have posted), and an automatic match that pairs later is taken back.
- **Only payments are seen, never purchases.** A matched card's balance only goes down, so it's shown as
  approximate: `≈ −$1,075.66 · last set $1,110.66 on Sep 26, minus $35.00 matched payments`. After 35 days the
  row adds a quiet "update it from a statement" (no alert, no badge); entering the statement balance restarts
  from that date.
- **The form's dry run.** Typing a pattern shows "This would have matched N payments ($T) in the last 90 days"
  with the same guards ("2 more pay a connected card and are left out"). With no payment posted yet it says 0.
- **The first autopay line.** Until a card has a pattern, `finance match-payments --suggest` lists new bank
  payments that mention it (CITI / CBNA for a store card issued by Citi), pay no connected card and aren't
  claimed by a pattern, with the pattern to use; the daily run posts it. Nothing is saved automatically.

```sh
uv run finance manual add --kind loan --name "Car loan" --institution "Zenith Financial Services" --subtype auto \
    --original-amount 24000 --balance 8200 --monthly-payment 412.50 --match-pattern "ZENITH ACH RTL" --as-of 2026-09-23
uv run finance manual edit 2 --match-pattern "CBNA STORE CARD"   # a store card, once its label is known
uv run finance match-payments --dry-run      # what would be applied (loan-match is the same command)
uv run finance match-payments --suggest      # card-payment lines for cards without a pattern
uv run finance loan-apply <txn_id> 1 --source ai   # a payment the pattern misses
uv run finance loan-unapply <txn_id>
```

## Car and property values

A vehicle can carry its **VIN** (17 characters; the check digit, its 9th character, is verified on the page and on the
server before anything is sent), **mileage**, **ZIP code** and **condition** (`fair` / `good` / `excellent`, the values
carfax.com/value's own code uses; `good` by default); a property its **address**. Then:

- **VIN decode** (NHTSA vPIC's extended decode, free, no key): on save, a VIN the car hasn't been decoded with fills the
  year, make, model and trim left blank (a changed VIN replaces the old car's unless you typed them in the same edit),
  and keeps the specs and safety equipment for the car page. Once per VIN (a v7 decode is redone once for the specs).
- **Car value: Carfax trade-in.** Carfax's own free value page (carfax.com/value) answers from the real VIN, mileage,
  ZIP and condition: trade-in (its `wholesalePrice`, the value), private sale and dealer retail, the number of history
  records, its odometer and the model specs. Its API sits behind bot protection, so the lookup runs **inside the local
  Chrome** (DevTools on `127.0.0.1:9222`): a new background tab on carfax.com/value, the two requests the page makes
  (`GET …/hbv/cow/trims/<VIN>`, then `POST …/hbv/cow/prices` exactly as the page sends it), then the tab is closed. It
  never touches another tab. The trim is yours if it's one of Carfax's, else Carfax's default; the colour Carfax's
  default. Chrome not running: skipped ("Chrome not running"), nothing counted. `CARFAX_ENABLED=0` in the `.env` turns
  it off.
- **A car's value**, in this order: your typed value; the latest Carfax trade-in; nothing ("No estimate yet").
  When Carfax's odometer is higher than yours, the row says so quietly ("Carfax records show 143,210 mi, update?");
  the mileage is never changed for you.
- **Property value: Redfin Estimate** (free, no key; `location-autocomplete`, then `home/details/avm`): plain HTTPS
  first, the same background-tab approach on redfin.com when that's refused (`REDFIN_ENABLED=0` turns it off). A
  property's value: your typed value; the latest Redfin Estimate; nothing.
- **A condo unit is only valued from its own record.** "#303", "Unit 303", "Apt 303", "Ste 303" are the same unit.
  Redfin's row must name exactly that unit, or it's "no estimate for this unit" (never the building's record, never a
  neighbouring unit's).
- Fetched on save when what it needs is there, by **Refresh estimate** (once per account per day, counted), by a new
  condition on the car page (the same limit) and by `finance revalue` in the daily run (once per account per 30 days).
  Every success is a row in `asset_valuations`.
- **Your value always wins.** Leave the value blank and the balance follows the estimate: the row reads
  `Trade-in · Carfax · Sep 26` (`Estimated · Redfin` for a home). Type a value and it's **Your value** (the estimate is
  shown beside it and never replaces it) until **Use estimate again**.
- **Mileage nudge**: 90 days after the mileage was set, the row adds a quiet "Update mileage for a better estimate"
  (no badge, no alert).
- **No keys.** Every source is keyless; nothing is read from the `.env` but the on / off switches (`CARFAX_ENABLED`,
  `REDFIN_ENABLED`, `PHOTOS_ENABLED`). The form says where data goes ("Your VIN, mileage and ZIP code are sent to NHTSA
  (the free VIN lookup) and to Carfax (through your Chrome) to get an estimate"; "Your address is sent to Redfin to
  get an estimate").
- **A refusal is a clean fallback**: the value stays as it was (yours, the last estimate, or $0 with "No estimate yet")
  and the page says why ("Carfax blocked the check, last value from Sep 26").
- MarketCheck and RentCast were removed: nothing asks them, their keys are never read, and their old
  `asset_valuations` rows are history only (never shown, never a value, never in the chart).

<a id="the-car--property-page"></a>

### The car / property page

Tap a vehicle or property (the Accounts page, the net worth card, the sidebar) and its page opens at `#/asset/<id>`
(a real URL: the browser's back works, and the page's back arrow goes back, or to Accounts when opened directly).
Loans keep their panel. The data is `GET /api/manual/<id>/detail`:

- **The value** (big), its basis and date, and the change since the previous check.
- **What it's worth**: a car's Carfax trade-in, private sale and dealer retail (at its mileage and condition), then
  "Want to sell? Get a Carfax cash offer"; a home's Redfin Estimate and its last sold price, beds / baths / sqft, and
  the listing link. No estimate: "not checked yet" until Redfin (Carfax) is asked, then "no estimate" and why.
- **Loan & equity** (a home: Mortgage & equity): only the loan linked to it (`linked_loan_id`: the edit form's Loan /
  Mortgage picker, or `finance manual edit N --loan ID`), with equity = value − that loan's balance. No link: no equity.
- **Value history**: the Carfax / Redfin checks (never the retired providers' rows).
- A car's **specs** (NHTSA + Carfax: one engine row, Carfax's body style, fuel, drive, …) and **safety & driver
  assist** (only what NHTSA's decode of the VIN lists).
- **Details**: mileage, ZIP, the loan, and the **condition** (Fair / Good / Excellent): a new one is saved and Carfax is
  asked again within the guard (once a day).

**The picture** is fetched automatically, once, in the background (on save or the first view; the page draws a
silhouette meanwhile and swaps the picture in). Never uploaded:

- A car: a **Wikimedia Commons** photo of that year / make / model (any brand). The search asks "{year} {make} {model}",
  then "{make} {model}"; a file's title must hold the make and all the model's words and none of interior / dashboard /
  engine / badge / wheel / rear / crash / concept / show / logo / seat; at least 1200 px wide; CC0, public domain, CC BY
  or CC BY-SA. Ranked by the exact year (±1 next), a front / side shot, then size. The requests carry the User-Agent
  `hermes-finance/1.0 (self-hosted personal net-worth dashboard)` (`FINANCE_CONTACT`, an email or URL, is put in when
  set, as Wikimedia asks), ≥ 1.5 s apart. The background is
  removed with **rembg** (`isnet-general-use`) when the optional extra is installed (`uv sync --extra photos`; the
  model is downloaded once to `~/.u2net`): the largest shape is kept, scraps under it are trimmed, and a cut that
  covers < 20% or isn't car-shaped (width / height 1.3–3.5) is rejected (up to 3 candidates, then the silhouette).
  Without rembg, or if the cut fails, the photo is shown uncut, cover-cropped. The artist and licence are shown under it
  with a link to the file page.
- A home: its **Redfin listing photo** (the MLS watermark stays; "Photo: listing via Redfin"), else an
  **OpenStreetMap** tile around its Redfin location with a pin the page draws ("© OpenStreetMap contributors"), else a
  drawn house. Never Redfin's Street View image. No background removal.
- Cached in `data/images/` (served at `/api/images/<file>`) and `asset_images`, one per car model or home; a try that
  found nothing is retried after 30 days, a failed one the next day. `PHOTOS_ENABLED=0` in the `.env` turns it off.
  `finance photos [--account N] [--force]` fetches from the command line.

### The free-tier guard

The rule: never go past a free tier. Every outbound call (NHTSA, Carfax, Redfin, Wikimedia Commons,
OpenStreetMap) is first reserved in **one `BEGIN IMMEDIATE` SQLite transaction** that checks and increments
`api_usage` (provider, month, calls), so the daily run and a dashboard click can't both take the last call:

| provider | what | our cap / month | notes |
|---|---|---|---|
| NHTSA | free VIN decode, no key | 300 | once per VIN |
| Carfax | free page, no key | **6** | through the local Chrome; a lookup (its two requests) counts once |
| Redfin | free page, no key | **20** | a lookup (autocomplete + estimate) counts once; a listing photo download counts too |
| Wikimedia Commons | free API, no key | 60 | car photos: a search and an info request per query, then the image |
| OpenStreetMap | free tiles, no key | 20 | one tile per home without a listing photo |

- The count goes up **before** the request, so failed calls, 429s and timeouts count. Nothing is ever retried.
- The calls in the **last 32 local days** must be under the cap too, so the cap holds for any billing month (31 UTC
  days span 32 local ones), whatever day the provider's starts on.
- Carfax and Redfin value lookups, per account: at most one a day (a save, Refresh or new condition on a day it was
  already looked up is refused) and the daily run only 30+ days after the last one.
- A refusal (401 / 403, or a 429 that says quota) stops NHTSA for the rest of the month, Commons for a day,
  OpenStreetMap for 7 days (`api_usage.blocked_until`; it survives restarts). `finance api-usage --unblock` lifts it by
  hand.
- Carfax / Redfin: a 403, a captcha page, a web page where JSON was asked for (or, for Carfax, an answer without a
  `vehiclePrice`) stops that provider for **7 days**, persisted the same way. While blocked, the page keeps the last
  value.

```sh
uv run finance manual add --kind vehicle --vin 1HGCM82633A004352 --mileage 48210 --zip 94110   # decode + estimate
uv run finance revalue --dry-run          # who is due, what would be asked (nothing is called or counted)
uv run finance revalue --account 3        # refresh one now (once a day)
uv run finance manual edit 3 --use-estimate
uv run finance manual edit 3 --condition excellent   # Carfax is asked again (the next day at the earliest)
uv run finance manual edit 3 --loan 1     # its equity counts loan #1
uv run finance photos --account 3         # its picture now (if it's due; --force to fetch again)
uv run finance api-usage                  # Carfax 1 used / cap 6 / free page, no key ...
```

## Agent read commands

`spend`, `budget-status`, `txns`, `networth`, `bills`, `loans`, `holdings` and `cashflow` are how Hermes reads
the data: plain CLI calls on the same machine, so they keep working when the dashboard is down. They:

- open the DB **read-only** (`mode=ro` + `query_only` + the read authorizer, like `finance query`), never
  upgrade its schema, and never call Plaid. A missing DB or an older schema exits 1 and changes nothing;
- build their numbers with the **same functions the dashboard API uses**, so the CLI and the page always agree:
  `spend` sums with `insights.spend_by` (the same function behind the category totals, pending spend and cash flow), `budget-status`
  is `insights.category_spend` / `budgets` / `pace_projection`, `txns` is `api.transaction_page` (the
  Transactions page's query and rows), `networth` is `insights.net_worth` + `manual.summary`, `bills` is
  `insights.recurring`, `loans` is `portfolio.debt`, `holdings` is `portfolio.investments`, `cashflow` is
  `insights.cashflow`. `tests/test_reads.py` builds both on the same fixture DB and compares them;
- print a short table, or with `--json` a stable shape that each command's `--help` documents field by field;
- sign money as the app does (spend and money out positive, refunds and income negative) and use local dates.

**Filters** (`spend` and `txns`; every one narrows, all are validated, bad ones exit 2 with a message):

| filter | meaning |
|---|---|
| `--month YYYY-MM` | one calendar month (`v.month`, as the dashboard filters) |
| `--since 30d\|YYYY-MM-DD` | from that day through `--until` or today; `Nd` is the last N days, today included (1–3660) |
| `--until YYYY-MM-DD` | through that day, inclusive; `--month` can't be combined with `--since` / `--until` |
| `--category NAME` | case-insensitive, must exist; `spend` refuses Income / Transfers (they have no spend) |
| `--merchant TEXT` | part of `merchant_name` or `name`, case-insensitive |
| `--account MASK\|NAME` | an account id, its last digits, its name (case-insensitive), or part of its name; every match counts |
| `--limit N` | rows (`txns`) or groups (`spend --by`) shown, 1–500, default 50 |

With no period, `spend` covers this month and `txns` all history. `spend` compares with the **previous equal
period**: a month with the month before up to the same day while it's the current month (the Overview's "vs
last month"), else with the whole month before; a date range with the same number of days just before it.
`budget-status` adds a per-category projection (`insights.category_pace`): the Overview's pace rule on that
category alone (fixed categories and recurring bills aren't extrapolated; bills still due this month are added).
`bills --next Nd` (default 14d, 0–730) lists recurring items expected by then plus any already past their
expected date; `cashflow --months N` is 1–120 (default 6).
`networth --custom` is the dashboard's **Custom** net worth (the **All · Custom** switch on the Accounts page and
the Overview tile): every account except the ones you left out, chosen per account on the Accounts page's Edit
sheet or with `finance networth-custom`. It's one saved choice (the `networth_excluded` setting), so the phone, the
laptop and the CLI agree, and an account connected or added later counts until it's left out. It prints "Net worth
(custom)", each group with only its counted accounts and their total (a group left out entirely isn't listed), then
what isn't counted. The JSON always has `mode: all | custom` and `custom: {assets, debts, net, excluded: [{id,
source, name, mask, institution, kind, group, balance, signed}]}`, each account its `nw_id` (the id Custom uses: the
Plaid `account_id`, or `manual:<id>`) and `counted`, each group (without the flag) its `custom: {count, total,
signed}`, and every history point `custom_assets` / `custom_debts` / `custom_net` (today's choice applied to every
day) next to `assets` / `debts` / `net`; with the flag, the top `assets` / `debts` / `net` are Custom's. So "net
worth the way I count it" is one call, and so is changing what counts:
`networth-custom --exclude <nw_id>...` / `--include <nw_id>...` / `--reset` (everything counts) /
`--preset cash-and-debts` (cash minus every debt) / `--list`.
The older `networth --cash-only` still works (hidden from `--help`): it prints the Cash & debts preset whatever is
saved, with `mode: cash_only` and a `cash_only: {assets, debts, net}` block.

```sh
finance spend --json                                   # this month so far vs last month to the same day
finance spend --since 30d --by merchant --limit 10
finance spend --month 2026-08 --category "eating out"
finance budget-status --json
finance txns --merchant zenith --limit 5 --json
finance txns --flagged --json                          # what the AI wants a human to look at
finance networth --history --json
finance networth --custom --json                       # only the accounts you count (Custom)
finance networth-custom --list                         # every account, its id, counted or not
finance networth-custom --exclude acc_123 manual:4     # leave these out of Custom
finance bills --next 7d
```

## Agent tools: Hermes plugin and MCP server

The read commands above are also agent tools, with the same names in both adapters and the same answers as the
command's `--json`: `finance_spend`, `finance_budget_status`, `finance_transactions` (`txns`), `finance_networth`,
`finance_bills`, `finance_loans`, `finance_holdings`, `finance_cashflow` and `finance_status`. Their arguments are
the commands' flags (`month`, `since`, `until`, `category`, `merchant`, `account`, `limit`, `by`, `search`,
`flagged`, `uncategorized`, `history`, `custom`, `next`, `months`). Four write tools: `finance_set_category`,
`finance_add_rule` (regex by default; a pattern matching no transaction is refused), `finance_set_cap` and
`finance_track_bill` (`action` track / update / stop, `merchant`, `every`, `varies`, `usually`). `finance_sync`
(`refresh`) syncs now and says what changed, with `data_age` (also in `finance_status`): no approval,
except `refresh=true` on a paid Plaid plan (Plaid bills each refresh). 15 tools. One
definition each, in `src/hermes_finance/tooldefs.py`, shared by:

- **The Hermes plugin** (`hermes_plugin/`, a standalone plugin directory): the reads open the database read-only; each
  write goes through Hermes's approval gate (a `pre_tool_call` hook asks with the change in plain words; a call that
  can't be done is blocked with the reason). It bundles the `hermes-finance:usage` skill. Settings
  (`plugins.entries.hermes-finance.settings`): `HERMES_FINANCE_DATA_DIR` (default: the app's own data directory) and
  `HERMES_FINANCE_APP_DIR` (a checkout to load the app from, when `hermes_finance` isn't installed in Hermes's
  Python; also an environment variable). Check it with
  `HERMES_FINANCE_APP_DIR=$PWD hermes plugins doctor --ci hermes_plugin`. In a scheduled job, and on Hermes's
  unattended platforms (the API server, webhooks), the hook blocks the writes instead of asking, since nobody is
  there to approve. `hermes finance crons status|plan|apply` sets up the daily and weekly report jobs, adopting
  existing ones: see [Scheduled reports](plugin-crons.md).
- **The MCP server** (the official SDK, `uv sync --extra mcp`): `finance mcp` on stdio, and streamable HTTP at `/mcp`
  on `finance serve`, behind the same Host / Tailscale login / Origin gate as every other route. Read-only by default:
  the write tools are listed only after `finance mcp allow-writes on`, and even then a call only records a row in
  `pending_actions` (nothing is applied until you approve it in Settings → Connect other agents → Waiting for
  your OK: `POST /api/mcp/pending/approve` / `decline {id}`). The last client to connect (its `clientInfo`) is saved as the
  `mcp.last_used` setting and shown in `GET /api/settings` (`mcp: {allow_writes, last_used, pending}`).

```json
{"mcpServers": {"hermes-finance": {"command": "uv",
  "args": ["run", "--project", "/path/to/hermes-finance", "--extra", "mcp", "finance", "mcp"]}}}
```

## Dashboard

`finance serve` renders one HTML shell; `static/app.js` draws the pages from a JSON API. Hash routes:
`#/overview`, `#/transactions` (filters in the query: `?category=Shopping`, `?bank=Chase`,
`?kind=review|spend|income|transfers`, `?q=`), `#/budgets`, `#/recurring`, `#/accounts`, `#/investments`,
`#/debt`, `#/settings`, `#/setup` (first run); `?month=YYYY-MM` picks a past month on Overview/Transactions/Budgets.
The left sidebar (below 1024px a drawer plus a tab bar: Overview, Transactions, Budgets, Accounts and
Settings; the drawer lists Overview, Transactions, Budgets, Goals, then Accounts, Recurring, Investments, Loans & debt,
with Settings at its bottom; the desktop sidebar has Goals under Budgets too) has the page nav with counts, net
worth, accounts grouped by bank plus "Added by you" (expand, "show all" for sub-$1 accounts, Reconnect on broken ones)
and the connections footer with **Add account** and **Settings**. Settings holds Connections, Access &
security, the AI provider, Connect other agents (MCP), Sync & alerts, Valuations, Region (currency and locale for every
amount and date), Appearance (dark / light / system) and Data (export CSV, backup, import); everything saves as it
changes. Add account → **Import a file** reads a bank's CSV (the columns are matched in a preview), OFX or QFX into a
manual account; rows already there are skipped. Imported rows go through your rules like synced ones; one no rule matches lands in Other (Needs review), except money coming in on an OFX / QFX row whose type says what it is: a direct deposit, deposit or payroll, or interest is filed under Income (a generic credit is not: a bank statement's credit and a refund stay in Other, while a card statement's own payment line is filed as a transfer). The look: Inter (self-hosted), 15px body, icon card titles,
rings, bars, sparklines and category badges, no glows and no motion (charts draw once).

- **Overview**: left to spend + daily allowance, on-pace projection (and how it's made), month-to-date vs
  last month, cumulative spend chart, alerts, budgets, "Where it went" doughnut, bills this month, coming
  up, income vs spending (partial-history months drawn lighter), recent transactions.
  **Left to spend** is the net: budgeted − spent − the capped categories' recurring bills still due this month,
  negative (red, "Over budget by $X once bills clear") when over; the allowance a day is max(left, 0) ÷ days
  left; under it "$630 in bills still due (GEICO, Zenith)" and "$829 left in 4 budgets · $538 over in 3". A
  category's status (and its alert) comes from spent + its bills still due; its card says "Still due $X".
  **Budget rings** (Left to spend, each Overview Budgets row, the Budgets summary, each category card; one helper,
  `budgetRing`) count the same thing: the % is (spent + bills still due) ÷ budget ("113% with bills" for $1,747
  spent + $465 due of $1,960). The spent part is drawn in the status colour, the bills still due right after it in
  a lighter shade; past 100% the ring is full. One colour rule, the category status's (`capStatus` /
  `insights.cap_status`): red at 100%, amber at 80%, green under. The small label reads "with bills" while any are
  due, else "used" (cards: nothing when none are due). The Budgets cards run from the fullest ring down.
  **Where it went**: each legend row's category badge is a toggle (a button, `aria-pressed`) that hides the
  category from the ring; the rest of the row still opens its transactions. Hidden rows stay, dimmed, with
  "hidden" where their % was; the %s and the middle ("$1,649 shown / of $4,031 spent") count only what's shown;
  the header says "2 hidden · Show all"; "Hide fixed bills" hides `insights.FIXED_CATEGORIES`. The choice is
  saved on the server (`chart_excluded_categories`), so phone and desktop agree; no total, budget, alert or read
  command uses it. A doughnut with one slice is a full ring (no separator notch).
- **Transactions**: search, filter chips, bank filter, per-row category select. Changing a category saves it
  (`source=user`) and offers "Always file <merchant> here", which adds an exact user rule and re-files the
  merchant's other rows (AI guesses included, user decisions never).
- **Budgets**: a card per capped category with a 6-month mini chart and a summary of your caps (budgeted,
  spent, % used). Suggestions stay quiet: a capped card never shows one, and the big "Suggested
  budgets / Use all" hero only appears while **no** cap exists (`suggestion_hero` in the payload). With
  caps set, suggestions for uncapped categories wait behind a small **See suggestions** link in "No budget
  yet" (median of the last 3 full months, rounded up to $25 under $300, $50 above; applied only by **Use**).
  The inline editor (Edit / Set budget) is `$ [amount] Save Cancel`, then `Suggested $X · Use` and **Remove
  budget**; **Use** saves the suggestion in one click. Enter saves, Esc cancels and puts focus back.
- **Recurring**: detected bills and subscriptions (and the ones tracked by hand), the bills calendar, status (upcoming /
  due soon / late / paid). A row opens the bill's sheet: its estimate and usual range, schedule, latest charges on any
  account, **Edit schedule** and **Stop tracking**.
- **Accounts**: net worth (Plaid + manual) grouped Cash · Credit cards · Loans · Investments · Property ·
  Vehicles with counts and totals, a trend line once there are 2 days of snapshots, bank connections
  (slots ring, Reconnect), "Added by you" (edit / archive), and a card per group with sparklines from real
  snapshots.
- <a id="net-worth-all--custom"></a>**Net worth: All · Custom**: a switch next to the title of the
  Accounts net worth card and of the Overview Net worth tile (a tap on it never follows the tile's link). **All**
  counts everything; **Custom** counts every account except the ones switched off. Which ones is chosen per account
  (every Plaid account, every active manual one) on the **Edit** sheet of the Accounts card (or by tapping the quiet
  line under the figure): accounts grouped like the page, each with its logo / badge, mask, signed balance and a
  switch, a switch per group (on / off / mixed; the group's name folds it away), a live preview of the Custom figure
  and two presets, **Everything** and **Cash & debts** (every investment, vehicle, property and
  other asset off). **Save** stores it on the server (`POST /api/settings {networth_excluded}`: the ids left out, so
  an account connected later counts) and turns Custom on; **Cancel** keeps what was saved. Archived accounts aren't
  listed and never count. The first read without a saved choice saves the Cash & debts one (logged once), so Custom
  starts out as the Cash & debts figure. All vs Custom is one setting for the whole app, remembered per device
  (`localStorage` `hf.networth.mode`: `all` | `custom`, default All; an older `cash` reads as `custom`); flipping
  it redraws in place the sidebar figure and its Own / Owe bar, the Overview tile, and the Accounts figure, bar and
  trend line (`custom_net`: today's choice applied to every day, so the line never jumps when it changes). In
  Custom a quiet line under the figure says "Custom · N accounts not counted"; a group left out entirely stays listed
  with its total, dimmed, "Not counted"; a group counted in part shows its counted total and "N of M counted"; and
  each left-out account has a small "Not counted" tag in the sidebar's bank lists and the Accounts cards.
- **Investments**: total with 7D / 1M / YTD / All change chips (from real holding snapshots only; "7-day
  change shows up <date>" until there's enough), the value line (or "History builds from today"),
  allocation by holding or account type, portfolio breakdown, accounts and holdings. An account's value is
  Plaid's balance; when that differs from its holdings + cash (Plaid snapshots the balance and the prices at
  slightly different times), the gap is its own grey "Unpriced / pending" row below Cash, so the allocation
  adds up to the total. It's hidden under a cent, and a negative gap ("−$3.50") is listed with its signed share
  ("−0.7%") but not drawn.
- **Loans & debt**: total owed with card utilization and loan payoff groups (paid-off celebration), the
  biggest loan's payoff ring, payments coming up, and every debt sortable by balance / rate / time left /
  monthly payment. A loan opens a panel with its balance over time, details and matched payments.
- **Add account**: Link a bank or card (Plaid, with the slot meter) · Add a property · Add a vehicle · Add
  manually (bank, card, investment, loan, car, other asset, other liability), with inline validation.

Card grids are packed like masonry on a 4px row grid (three columns from 1200px, two from 760px, one on phones).
A packed card is at least its area's height, never pinned to it, so content that grows inside it (an editor, an
error, "See suggestions", an opened group) makes the card taller at once; a ResizeObserver on the cards and a
MutationObserver on the grids re-pack. Every card reaches down to the card below it, so the gap between cards is
always 16px, and side-by-side cards never start 4-12px apart.

### Logos

Bank chips (sidebar, Accounts groups and rows, the Transactions account column, Investments and Loans & debt
rows) and holding bubbles show real logos. Images are fetched **server-side, once**, checked, stored in the
DB and served from this server; the page never loads an image from anywhere else (the CSP is unchanged).

- **Banks**: Plaid's `/institutions/get_by_id` with optional metadata (free, creates nothing) at connect time
  and in `finance refresh-institutions`. Plaid's logo first. When it has none, the bank's own
  `apple-touch-icon`, else its largest `<link rel=icon>`, else `/favicon.ico`, from the url Plaid gives.
  Otherwise the initials bubble, in the bank's `primary_color`, and the daily `finance sync` looks the bank up again
  once a day until a logo is found (a network blip at connect time heals the next day); a found logo is kept.
  Alpaca's icon (its site's) is retried the same way by `finance alpaca-sync`. A logo sits on a light round chip, and the
  status dot stays. A manual account uses a bank's logo only when its typed institution is exactly a cached
  bank's name; otherwise its kind icon.
- **Holdings**: `https://financialmodelingprep.com/image-stock/<TICKER>.png` for stocks, ETFs and funds, in
  `finance sync` (after holdings) and `finance refresh-logos`. Cash keeps its `$` badge; crypto, unknown types
  and odd or empty tickers keep the letters. A miss is retried after 30 days; network trouble isn't a miss.
- **Every fetch**: https only (redirects too), 5 s timeout, 256 KB cap (a bank's home page is only read up to
  that, for its `<head>`), an image content type whose bytes really are that image (PNG, JPEG, GIF, WebP or
  ICO; never SVG), at least 16 px and at most 1024 px, and not blank (one colour or fully transparent).

The numbers come from `src/hermes_finance/insights.py`: pure functions over a DB connection that return
plain dicts (month summary, pace projection, category spend, budgets, suggested caps, recurring detection,
alerts with stable `key`s, cash flow with coverage flags, net worth). The weekly summary reuses them.

### JSON API

All behind the same Host/Origin/identity gate as the page. GETs take `?month=YYYY-MM` where it matters.

| endpoint | returns |
|---|---|
| `GET /api/overview` | month summary, pace, budgets (net `left`, `total_due`, `bills_due`, `under` / `under_count`, `over_amount`), category spend (each with `due`, `bills_due`, `left`), alerts, bills, upcoming, cash flow, net worth headline (with manual accounts, and its `custom` block), investments / debt tiles, cards due soon, 6-month category history, recent, last sync, `chart` (the hidden categories and the fixed-bill list) |
| `GET /api/transactions?month=&q=&category=&bank=&kind=&limit=&offset=` | `{total, counts, rows}`; rows carry `logo_url` / `icon_url` (allowed hosts only), the initials `seed`, `review`, `ai_flagged`, the AI's `ai_note` and the bank's `institution_id` |
| `GET /api/budgets` | category spend (each with `suggested`: the editor's suggestion, capped or not, null when there's no usable one), budgets, suggested caps for uncapped categories only (the visible list), `suggestion_hero` (true only with no cap at all), 6-month history per category with coverage |
| `GET /api/recurring` | recurring items and monthly totals per kind |
| `GET /api/accounts` | Items (no tokens) with accounts, slots used, net worth (Plaid + manual, groups each with its `custom` part, `custom` = the Custom net worth with the accounts it leaves out, history with `custom_assets` / `custom_debts` / `custom_net` per point), manual accounts, 60-day sparklines from snapshots, connected institution ids, and `institutions` (per cached bank: `logo` yes/no with its version `v`, and `color`) |
| `GET /api/manual[?archived=1]`, `GET /api/manual/match[?pattern=&kind=loan\|credit_card&id=]` | manual accounts + totals; the loan and card forms' match preview, with `preview`: the 90-day dry run under match-payments' guards `{days, since, count, total, paired, paired_total, waiting, elsewhere}` (without a pattern: names to offer; for cards only unpaired card payments no pattern claims) |
| `GET /api/settings`, `POST /api/settings` | `{chart_excluded_categories, fixed_categories, networth_excluded}`; POST either or both of `{chart_excluded_categories: [names]}` (spending categories, case-insensitive, saved once each; only the "Where it went" chart reads it) and `{networth_excluded: [ids]}` (the accounts Custom leaves out: Plaid `account_id`s or `manual:<id>`, once each; an id that isn't a current account is dropped and the answer says so in `note` / `dropped`; not a list: 400). Same Host/Origin/JSON gate as every write |
| `GET /api/investments` | total, accounts (Plaid + manual; each with `residual`: its balance minus its holdings + cash), holdings, allocation by holding (the residual as a last row with `residual: true`, from a cent) / account type, breakdown, history and change chips (null without enough real snapshots) |
| `GET /api/debt` | total owed, cards (utilization, APR, minimum, due date), loans (APR, payment, remaining, payoff, payments left, matched payments, balance history), groups, upcoming payments, stats |
| `GET /api/categories`, `GET /api/counts` | category list; sidebar badge counts |
| `GET /api/institutions/<id>/logo`, `GET /api/securities/<TICKER>/logo` | the cached logo, from the DB, with its real content type and `Cache-Control: private, max-age=86400`; 404 when there's none |
| `POST /api/transactions/category` | `{transaction_id, category, make_rule}` → how many rows changed |
| `POST /api/budgets/cap` | `{category, monthly_cap}`: a spending category, 0–100000, or `null` to clear |
| `POST /api/manual`, `POST /api/manual/<id>`, `POST /api/manual/<id>/archive` | create (`{kind, ...fields}`), edit (the fields to change; `null` clears an optional one), archive. Errors are 400/404 with `{error, field}`. A vehicle / property's create / edit also returns `decoded`, `decode_problem` and `estimate_result` (`{ok, message, called, reason, value, applied, blocked_until}`); its picture is then fetched in the background when due |
| `POST /api/manual/<id>/refresh`, `POST /api/manual/<id>/use-estimate` | "Refresh estimate" (one guarded call, at most one per account per day; the result in `estimate_result`) and "Use estimate again". Each manual vehicle / property in `/api/accounts` and `/api/manual` carries `valuation` (`source`, the latest `estimate` with its date and basis, `last_error`, `can_refresh` / `refresh_note`, `mileage_nudge`, the form `note`), and `/api/accounts` has `valuation` for Carfax and Redfin (`enabled`, this month's `calls` / `cap`, `blocked_until`, the privacy / off notes) |
| `GET /api/manual/<id>/detail` | The car / property page: `value` (amount, basis line, change since the last check), `table` (Carfax's three figures / Redfin's estimate and last sold, `empty`: "not checked yet" / "no estimate"), `sell`, `loan` (the linked loan with `equity`), `history`, `photo` (`status` ready / pending / none, `kind` cutout / photo / map / silhouette / outline, `url`, `credit`, `pin`), `notes`, `details`; a vehicle's `specs` / `safety` / `vin_masked` / `records`, a property's `facts`. A pending picture starts its background fetch |
| `GET /api/images/<file>` | A cached picture from `data/images/` (16 hex characters `.png` / `.jpg`, nothing else), `Cache-Control: private, max-age=86400`; 404 when there's none |

`/api/status`, `/api/link_token`, `/api/link_token/update`, `/api/exchange` and `/api/reconnected` are unchanged.

### Front end and headers

- **Chart.js v4.5.1**, UMD build vendored at `src/hermes_finance/static/vendor/chart.umd.min.js`
  (sha256 `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a`, from the npm tarball
  `chart.js-4.5.1.tgz`, whose registry sha512 integrity was checked). MIT license next to it in
  `chart.js.LICENSE.md`. The only runtime CDN is Plaid's `link-initialize.js`.
- `GET /static/<file>` serves exactly the files listed in `web.STATIC_FILES` (`app.js`, `app.css`,
  `vendor/chart.umd.min.js`, `fonts/InterVariable.woff2` as `font/woff2` with its license, the icons and
  `manifest.webmanifest` as `application/manifest+json`); anything else is 404. `/favicon.ico` and
  `/apple-touch-icon.png` are served from the icon set.
- **Inter** (variable, v4) is self-hosted under `static/fonts/` with its SIL Open Font License.
- **App icon**: design "B": two periwinkle pillars (an "H") and a mint arrow climbing through them, on navy.
  Traced from the generated art in `assets/brand/source/icon-B-gptimage.png` into a clean SVG of simple
  shapes in its sampled colours; the master `icon.svg` is full bleed (iOS and Android round the corners),
  `favicon.svg` is a bolder cut for tabs, `favicon-16.png` a 16 px hinted one. `assets/brand/render_icons.py`
  writes both SVGs and renders every PNG, `favicon.ico` and `assets/brand/contact-sheet.png` with its own
  headless Chrome; copies live in `assets/brand/`.
- The page has no inline script or inline event handlers. `app.js` builds every element with
  `createElement`/`textContent` and never uses `innerHTML`, `outerHTML`, `insertAdjacentHTML` or
  `document.write` (a test greps for them). Icons are cloned from a `<template>` in the page; rings and
  sparklines are SVG made with `createElementNS` from numbers only. Merchant logos and category icons are shown only from `https:` URLs on
  `plaid-merchant-logos.plaid.com` / `plaid-category-icons.plaid.com`; otherwise initials on a colour
  derived from the merchant name. Bank and holding logos come from this server only (see [Logos](#logos)).
- Every response carries `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and this CSP:

  ```
  default-src 'self' https://cdn.plaid.com; script-src 'self' https://cdn.plaid.com; frame-src https://cdn.plaid.com; connect-src 'self' https://*.plaid.com; img-src 'self' data: https://plaid-merchant-logos.plaid.com https://plaid-category-icons.plaid.com; style-src 'self' 'unsafe-inline'; font-src 'self'; manifest-src 'self'; base-uri 'none'; frame-ancestors 'none'
  ```

  Plaid's Link CSP guidance (plaid.com/docs/link/web) asks for `https://cdn.plaid.com/` in `default-src`,
  `script-src` and `frame-src`, `https://production.plaid.com/` in `connect-src` (covered by
  `https://*.plaid.com`) and `'unsafe-inline'` styles. It also lists `'unsafe-inline'` for `script-src`, but
  that is for an inline `Plaid.create` snippet; this page has none, so it is left out.

## Goals, splits, notes and tags, saved filters, bills calendar, rollover, Alpaca

| command | what it does |
|---|---|
| `goals [--json]` | Read-only: the goals (Save up, Pay off, Monthly savings) with progress, %, what's left and pace, the savings accounts suggested as goals, and each account's unassigned money. Same numbers as the Goals page and the `finance_goals` tool. |
| `alpaca-sync [--json]` | The Alpaca live account's daily read-only snapshot (equity, cash, positions). Nothing to do without its key file; exit 1 when it failed or the account isn't ACTIVE (the daily run carries on). Runs in `scripts/finance_daily_prerun.sh` after `revalue`. |
| `txns ... --tag NAME` | Only transactions with that tag; `--search` also matches notes and tags. |

- **Goals** (`#/goals`, the Overview's fourth tile, `goals.py`). Made only from a template, only when you tap Create.
  *Save up*: a target (and optionally a month); progress from a whole account's balance, part of an account (an
  earmark: the earmarks in one account can never add up to more than its balance when set; if the balance drops later,
  the newest goals it no longer covers are flagged short, never shrunk) or entries you log (+ add / take out). "On pace
  for <month>" comes from the last 90 days (a third of it a month); with a month set it says what to save a month.
  *Pay off*: a loan or card, counted down from its balance when the goal was made. *Monthly savings*: this month's
  income − spending (cash flow's numbers) against a target. Goals never change net worth, budgets or any total.
- **Splits** (`txmeta.py`): a purchase in 2+ lines (spending categories), adding up to its amount to the cent. Spending
  by category everywhere (budgets, rings, the chart, cash flow, alerts, `spend`, `budget-status`, the tools) reads the
  view `v_spend_lines` (a split's lines in place of the transaction), so no total changes. A split is the user's decision
  (`classifications.source = 'user'`): rules and the AI never touch it; `set-category` refuses it until it's unsplit.
  **Notes** and **tags** on any transaction; **saved filters** are named sets of the Transactions filters, kept in the DB.
  The transaction sheet docks beside the table from 1400px wide (the usual sheet below) and saves when it closes.
- **Bills calendar** (`billcal.py`, on Recurring in place of "Next 30 days"): `/api/recurring`'s items on their days
  (paid on the last charge's day, due on the next expected day and after it at the cadence), this month and the next
  11; an agenda below 1200px. The week starts on Sunday unless a `region` setting says `week_start: monday`.
- **Budget rollover** (`rollover.py`): a switch in the cap editor, off by default. On: last month's unused cap (cap −
  spent, never below 0) adds to this month's cap (`cap` is then the effective cap, `base_cap` the one you set,
  `rollover` = `{amount, from}`), used by left-to-spend, rings, alerts and budget-status. All off: nothing changes.
- **Alpaca live account** (`alpaca.py`), read-only. The only calls are GET `/v2/account`, `/v2/positions` and
  `/v2/account/portfolio/history` on `api.alpaca.markets`, enforced at its one call site; a paper key is refused. The key
  file is `~/.config/hermes-finance/alpaca-live.env` (`HERMES_ALPACA_KEY_FILE` names another): **Add account → Alpaca
  (API key)** checks a key with one call (only an ACTIVE live account is accepted) and only then writes it (0600, in a
  0700 folder); the Alpaca row's Remove deletes it and leaves every snapshot out. Those two routes answer only on this
  computer. It shows as the institution "Alpaca" (its site icon) on Investments, Accounts and the sidebar, counted in net
  worth once and only while ACTIVE, in the Custom picker like any account, with no Plaid slot. `HERMES_ALPACA=off`
  switches every Alpaca request off (the tests and the gates set it).

| endpoint | returns |
|---|---|
| `GET /api/goals[?archived=1]`, `POST /api/goals`, `POST /api/goals/edit`, `POST /api/goals/entry`, `POST /api/goals/dismiss` | the Goals page (goals, accounts with `unassigned`, suggestions, totals); create; change (name, target, month, earmark, archived); log an entry; "Not a goal" |
| `POST /api/transactions/split`, `POST /api/transactions/meta`, `GET /api/tags` | `{transaction_id, lines: [{category, amount}]}` (`[]` unsplits); `{transaction_id, note?, tags?}`; the tags in use. `/api/transactions` also takes `tag=`, its `q` matches notes and tags, and its rows carry `split`, `note`, `tags` |
| `GET /api/saved-filters?month=`, `POST /api/saved-filters`, `POST /api/saved-filters/delete` | the saved filters with a count for the month; save `{name, filters}`; delete `{id}` |
| `GET /api/bills-calendar?month=` | `{month, week_start, bills: [{date, state: paid\|due\|late, projected, merchant, amount, category, ...}], count, paid_count, total, paid_total, due_total, prev, next}` |
| `POST /api/budgets/rollover` | `{category, enabled}` |
| `GET /api/alpaca`, `POST /api/alpaca/key`, `DELETE /api/alpaca/key` | whether a key is saved and the last snapshot (never the key); connect `{key_id, secret}` → `{connected, status, equity, cash, positions}`; remove. The last two refuse a request that came through the tailnet proxy |

## Bills: any schedule, amounts that change, tracked by hand

Detection (`insights.recurring`) knows **weekly, every 2 weeks (13–16 days), monthly, every 2 months (55–70), every 3
months (85–100), every 6 months (170–195) and yearly**. The window scales with the cadence (7 months for up to monthly,
13 for every 2 / 3 months, 25 for every 6 months and yearly). Weekly, every 2 weeks and monthly need 3 charges; every
2 / 3 / 6 months 2 (one interval), but two charges only count for a bill-like merchant (a utility, rent, a loan,
insurance: a random merchant charged twice 60 days apart is not a bill); yearly keeps its old rule (2 charges, any
merchant). The cadences beyond monthly and yearly also need most intervals to fit, not just their median. `PER_MONTH` normalizes
the monthly totals (every 2 months = 0.5).

- **Amounts that change.** A utility (Plaid `RENT_AND_UTILITIES_*` except rent, or a name `BILL_NAME_RE` counts as a
  utility) whose amounts don't fit the ±25% band is found by its schedule alone (monthly or slower). Its amount is an
  estimate, the median of its last 3 charges, shown as "~$5 est." and, where there's room, "usually $4–40" (its
  charges over 13 months). Fixed bills show their exact amount, as before.
- **A bill is the merchant, not the account**: charges from it on any account count; "last paid from" names the last.
- **Track as a bill** (the transaction sheet, desktop and phone; `finance bills track`; the `finance_track_bill` tool):
  a schedule (pre-picked: the detected one, else every month), "Same each time" / "Changes each time" (pre-picked from
  the category) and an optional **Usually about** (an amount or a range), starting from that charge. One row per
  merchant in `tracked_bills` (schema **v13**); every later charge from the merchant confirms it, the next one is
  expected one schedule after the last. With a usual amount and fewer than 3 charges, its middle is the estimate (a
  single big bill never is); from 3 charges, the median of the last 3, the range still shown. A charge over 1.5× the
  usual's top or under half its bottom gets a quiet "higher / lower than usual" tag on the transaction and the Recurring
  row (nothing else changes). **Edit schedule** / **Stop tracking** on a Recurring row's sheet or the transaction
  sheet: a tracked bill stops, a detected one is hidden (the same `tracked_bills` row, `stopped_at`).
- **Late** only after the cadence's longest interval + 5 days since the last charge (monthly 40, every 2 months 75).
  An every-2-months bill counts in "still due" and **Left to spend** only in the month it's expected, at its estimate.
- Everywhere: Overview bills and Coming up, Recurring (rows, calendar chips with logos, the phone's agenda), budgets
  still due, the pace, `finance bills` (+ `track` / `untrack`), `finance_bills` and MCP (cadence, variable, estimate,
  usual, source, last paid from, outlier), and the Sunday summary (it reads `/api/overview`, `/api/budgets` and
  `/api/recurring`, whose items carry `amount_label`, `cadence_label`, `variable`, `usual`, `source`).

| endpoint | returns |
|---|---|
| `GET /api/bills/lookup?transaction_id=` or `?key=` | the merchant's bill (or none; `hidden` when stopped), what Track as a bill starts with, the charge it starts from, its latest charges, the schedules |
| `GET /api/bills/preview?transaction_id=\|key=&every=&varies=&usually=` | what Track as a bill would save, before it does: `{summary, bill}` |
| `POST /api/bills/track`, `POST /api/bills/stop` | `{action: track\|update, transaction_id\|key, every, varies, usually}`; `{key}`. Through the gate and CSRF like every write |
| `POST /api/mcp/pending/approve`, `POST /api/mcp/pending/decline` | `{id}`: a change an MCP agent asked for, applied exactly as the Hermes plugin applies one after its approval card, or declined |

## Sync now, fresh data from the banks, payments in transit

- The header shows "● Synced 9:18 PM" and a **Sync now** button beside it (on a phone "Sync",
  with the status under the page title). `POST /api/sync {item_id?, refresh?}` runs `finance sync` for every bank, one at
  a time (a second call: `409 {busy: true}`) and at most once a minute per bank (`429 {retry_after}`). The
  sync runs in the background under the page's own id for it (`rid`): the POST answers with the result when it's done
  within 4 seconds, else `202 {rid, running: true}`, and the page asks `GET /api/sync?rid=` every 1.5 s until it is; a
  POST whose answer never came (a phone through a proxy: Safari's "Load failed") is looked up the same way, so the panel
  shows what really happened, or "Lost the connection" with what may still have happened. `serve.log` gets one line per
  sync (sync or refresh, the banks, how long, what happened, counts only) and per client that hung up, even without
  `--verbose`. While it runs the button is disabled and says "Syncing…" with a light running round its border (the
  page's one animation; still and lit with reduced motion); then a panel says what changed (on a phone a bottom sheet):
  `changes` has the counts, every balance that moved (largest first, with its bank, account and type: before → after),
  the new spending (`new_spend`, largest first) and how many other new rows (`new_more`: transfers and card payments,
  money in); `previous_sync` is the sync before ("Nothing new since 6:31 AM."), `banks` the Plaid banks it covers
  ("All 8 banks": Alpaca is read live). "Nothing new" closes by itself after 4 seconds; changes and errors stay until
  closed. A bank that needs a re-login has its Reconnect in the panel, with what the others brought in. `finance sync`
  prints "3 new transactions · Chase ••1234 $450.00 → $0.00". Through the gate and CSRF like every write.
- "Ask banks for fresh data" (`refresh: true`, only in the Sync now panel) first calls Plaid's `/transactions/refresh`
  (and `/investments/refresh`) for each connected bank with that product, once per 30 minutes each (8 calls
  at a time, 10 s each at most; one not answered in time may be asked again right away), then syncs the banks out of
  their minute; new items land a few minutes later. Asked within a minute of a sync, nothing is synced again and the
  answer says so (`sync_skipped {ago, retry_after}`: "Not synced again: the last sync was 40 s ago."); a sync that left
  some banks in their minute says "7 of 8 banks". `refresh.banks` says, per bank, when it was asked and when it can be again ("Ask 7
  banks for fresh data", "Chase · asked 12 min ago · available again in 18 min"). Allowed by Settings → Connections →
  "Allow on-demand bank refresh": unset, on in Sandbox or on the Trial plan (free there), off on Paid (Plaid bills each
  refresh). Otherwise `403` with the reason.
- **Payment in transit:** a card payment that left a connected bank account in the last 5 days, naming exactly one
  connected card, that the card hasn't posted yet (no inflow of the same amount ±$0.01 within 5 days) is counted as paid
  in net worth (Overview, Accounts, the sidebar, `finance networth`, `finance_networth`, today's history point) and in
  the Overview's Loans & debt tile (it never contradicts net worth's Owe), with the line "$450.00 Chase payment
  in transit · counted once"; Accounts adds where it left from and what net worth would read counted twice, and the
  card's own row (which keeps what the card reports) says "$450.00 payment on its way from Checking". `finance
  networth` prints "Payment in transit: $450.00 to Chase ••1234 (sent Sep 27)". Stored balances never change.

## Backup and restore

```sh
finance backup --out ~/Backups/                  # hermes-finance-backup-YYYY-MM-DD.db, safe while the app runs
finance restore ~/Backups/hermes-finance-backup-2026-10-23.db           # stop `finance serve` first
finance restore --force ~/Backups/hermes-finance-backup-2026-10-23.db   # replace a database that holds data
```

A backup (`finance backup`, or Settings → Data → **Download backup**: the same file) is a consistent copy of
`finance.db` with every credential taken out: bank access tokens, the built-in login, MCP tokens. It never holds the
secrets file, Alpaca's key file or the cached pictures (`data/images/`). `finance restore` checks the backup first
(SQLite's integrity check, a hermes-finance schema no newer than the code), refuses to replace a database that holds
data without `--force`, moves the old database aside (`finance.db.<timestamp>.pre-restore`, never deleted), runs the
migrations on the restored one, and prints what to redo: reconnect each bank, set the built-in login up again, and the
keys `finance doctor` says are missing. Everything, step by step: [Backup and restore](backup-restore.md).

## Security notes

The full model is in [Security](security.md). In short:


- `data/` is 0700. `finance.db` (and its WAL files) and `tokens.backup.jsonl` are 0600. `data/` is
  gitignored.
- Access tokens are stored in `items.access_token` **and** in `data/tokens.backup.jsonl`. If the DB
  is ever lost, the backup's JSON lines (`item_id`, `access_token`, `institution_id`,
  `institution_name`) are enough to re-insert the `items` rows. After that, `finance sync` rebuilds
  everything with no new Items.
- Output and logs are passed through a redactor (the secret plus anything shaped like an
  access/public token). Plaid request/response bodies are never logged.
- The web server binds loopback only. It accepts `Host: 127.0.0.1/localhost:<port>` (or a configured
  tailnet `--public-url` host from an allowed Tailscale user), same-origin requests, and JSON POSTs.
  That blocks CSRF and DNS rebinding from other sites.

