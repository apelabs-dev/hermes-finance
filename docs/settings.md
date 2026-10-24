# Settings

Everything on the Settings page (`#/settings`) saves as it changes. Every device sees the same values: they're stored
in the database's `settings` table, not in the browser. `finance settings` prints them read-only, and
`finance settings --json` gives the same values to scripts and scheduled jobs. Every key and its default is in
[Settings keys](reference/settings-keys.md).

## Connections

| Row | What it does |
|---|---|
| Plaid keys | *Set* or *Missing*. **Replace keys** writes a new client ID and secret to the secrets file. |
| Plaid environment | Production (your banks) or Sandbox (fake demo data). Switching asks for the matching keys. |
| Plaid connections | Slots used out of 10. Removing a bank doesn't free one ([why](plaid.md#slots)). |
| Plaid plan | Trial or Paid. Plaid doesn't tell the app, so you pick it. On Paid, Plaid bills each linked bank monthly. |
| Allow on-demand bank refresh | Adds *Ask banks for fresh data* to the Sync now panel. On by default for Trial and Sandbox, off for Paid (billed per request). Changing the plan resets it to that plan's default. |
| Alpaca | Connect, replace or remove a read-only Alpaca key. The app reads the live account's balance and positions once a day and never trades. It uses no Plaid slot. |

Banks and their **Reconnect** buttons are on the Accounts page.

## Access & security

Choose who can open the app: **This computer only**, **Behind your proxy login** or **Built-in login**. The
[Security model](security.md#access-modes) covers each mode in full.

- *Behind your proxy login*: the trusted header (default `Tailscale-User-Login`), the public URLs, and the allowed
  identities (shown masked). The card also shows when the last request came through the proxy, and from whom.
- *Built-in login*: set up here, from this computer: a password, then the authenticator app (a QR code and a code to
  confirm it), then the recovery codes. Once it's set up: change the password, make new recovery codes, sign out
  everywhere.
- A red line under the choice says what the next `finance serve` would refuse, and why.

`finance access` shows and changes the same settings from the terminal.

## AI provider

| Choice | What it means |
|---|---|
| **Hermes** (the default) | Ask uses Hermes's model through the plugin. Nothing to paste. The card says whether Hermes is found on this computer. |
| **API key** | Provider preset (OpenRouter, OpenAI, Local, Custom), endpoint, model and API key. The key is written to the secrets file and never shown again. |
| **Off** | Dashboard only. No Ask, nothing queued for AI classification. |

- **Monthly spend cap** (Hermes and API key, default $5): a soft cap on Ask. See
  [AI modes](ai-modes.md#api-key-the-app-runs-the-loop).
- **Auto-apply confidence** (0.5 to 0.95, default 0.7): an AI classification at or above it is filed for you; one
  below waits in *Needs review*.
- *Test connection* is shown but disabled ("Coming in the next update"): it isn't built yet.

## Connect other agents (MCP)

- **MCP server**: on or off. Off makes `/mcp` answer 404 and `finance mcp` refuse to start. It needs the `mcp` extra
  (`uv sync --extra mcp`).
- The endpoints: `http://127.0.0.1:<port>/mcp` on this computer, and `<public URL>/mcp` through your proxy, which needs
  a token (or, behind your proxy login, an allowed identity).
- **Copy config** for Claude Desktop, Cursor or Codex.
- **What agents can do**: *Read-only* (the default) or *Allow writes (asks for approval)*. This is the same switch as
  `finance mcp allow-writes on|off`.
- **Waiting for your OK**: the changes agents asked for, and the Ask panel's, each with *Approve* and *Decline*.
- **Tokens**: *New token* (shown once, then only its last 4 characters) and revoke.

## Sync & alerts

| Row | Default |
|---|---|
| Daily sync | 06:30 |
| Weekly summary | Sunday, 18:00 |
| Alerts go to | "Hermes chat" (a label) |
| Alert me when a budget reaches 80% / goes over its cap / a bank connection breaks | all on |

The app doesn't schedule or send anything itself. These values are for scheduled jobs to read through
`finance settings --json`:

- The plugin's bundled jobs (`hermes finance crons`) read the three alert switches. Their schedule comes from
  `hermes finance crons plan --daily-time HH:MM --weekly-day DAY --weekly-time HH:MM` (default 06:30 and Sunday
  18:00), not from these two rows. Where they're delivered comes from `--deliver PLATFORM:CHAT_ID`, never from the
  *Alerts go to* label. See [Scheduled reports](plugin-crons.md).
- A job of your own can read every value.

## Valuations

Automatic car and home values and pictures, from free public pages. These are not official APIs, so they can change
or block lookups. A value you type always wins.

| Source | What | A new install |
|---|---|---|
| Carfax | A car's trade-in value, from its VIN, mileage, ZIP code and condition. Runs inside your own Chrome, which must be started with remote debugging on port 9222 (the card shows the one-line command for macOS, Linux and Windows). | off |
| Redfin | A home's Redfin Estimate, from its address | off |
| Photos | A car's picture (Wikimedia Commons), a home's listing photo (Redfin) or a map tile (OpenStreetMap) | on |

Each source shows this month's calls against its cap, and stops at the cap. A flag in the env file (`CARFAX_ENABLED`,
`REDFIN_ENABLED`, `PHOTOS_ENABLED`, `0` or `1`) wins over the switch, and the card says so.

## Region

- **Currency**: how amounts are written. Nothing is converted.
- **Number and date format**: the locale.
- **Week starts on**: Sunday or Monday (the bills calendar).
- **Budget month starts on**: day 1 only for now. Budgets use calendar months.

## Appearance

Dark (the default), light, or system (follows the device).

## Data

- **Export CSV**: every transaction as a CSV download.
- **Download backup**: a consistent copy of the database with every credential removed ([what's
  removed](security.md#what-stays-on-your-computer)).
- **Import** (the same as **Add account** → **Import a file**): a bank's CSV (you match the columns in a preview), OFX or QFX, into a
  manual account. Rows that are already there are skipped.

## Categorization rules

Every rule, newest first: what it matches, its category, whose it is (yours or the AI's), and how many transactions it
matches now. Edit a rule's category (an AI rule then becomes yours), delete one (with Undo), and accept or dismiss
suggested rules: merchants you filed by hand two or more times under one category. `finance rules` shows the same
list. Deleting a rule leaves the transactions it filed as they are.

## Files and environment variables

| Variable | Default | What it is |
|---|---|---|
| `FINANCE_DATA_DIR` | `data/` in the checkout | The data directory: `finance.db`, `tokens.backup.jsonl`, `images/` |
| `HERMES_ENV_FILE` | `~/.hermes/.env` | The env file (see the keys below) |
| `FINANCE_SECRETS_FILE` | `~/.config/hermes-finance/secrets.env` | Keys pasted in the app (Plaid's, `AI_API_KEY`) |
| `HERMES_ALPACA_KEY_FILE` | `~/.config/hermes-finance/alpaca-live.env` | Alpaca's read-only key |
| `FINANCE_PUBLIC_URLS` | none | Public URLs, comma-separated (same as `serve --public-url`) |
| `FINANCE_ALLOWED_USERS` | none | Proxy identities, comma-separated (same as `serve --allow-user`) |
| `HERMES_HOME` | `~/.hermes` | Where Hermes lives: detection, and the Ask bridge's `hermes` executable |
| `FINANCE_HERMES_BIN` | `$HERMES_HOME/hermes-agent/venv/bin/hermes` | Another `hermes` executable for the Ask bridge (an absolute path) |
| `HERMES_CHROME_DEVTOOLS` | `127.0.0.1:9222` | The Chrome used for Carfax (`host:port`), or `off` |
| `FINANCE_CONTACT` | none | An email or URL added to the User-Agent of picture requests, as Wikimedia asks |
| `HERMES_PLAID=off` | unset | Refuse every Plaid call (the tests set it) |
| `HERMES_ALPACA=off` | unset | Make no Alpaca request (the tests set it) |

Keys read from the **env file** (not from the process environment): `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV`,
`CARFAX_ENABLED`, `REDFIN_ENABLED`, `PHOTOS_ENABLED`. The secrets file's values win over the env file's.

Hermes plugin settings (`plugins.entries.hermes-finance.settings` in the profile's `config.yaml`):
`HERMES_FINANCE_APP_DIR` (the checkout to load the app from; also read from the environment),
`HERMES_FINANCE_DATA_DIR` (empty: the app's own choice) and `HERMES_FINANCE_CRON_DELIVER` (the default destination for
new report jobs).
