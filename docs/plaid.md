# Plaid setup

hermes-finance talks to Plaid with a small stdlib HTTP client (`src/hermes_finance/plaid.py`). You bring your own
Plaid account. The app needs two keys from it: the **client ID** and a **secret** for one environment.

## Sandbox or Production

| `PLAID_ENV` | What it is | API host |
|---|---|---|
| `sandbox` | Plaid's test environment: fake banks, fake data | `sandbox.plaid.com` |
| `production` (the default) | Your real banks | `production.plaid.com` |

No other environment is accepted. The first-run wizard's *Try demo* step saves `PLAID_ENV=sandbox` and turns on demo
mode. In demo mode, the server refuses to make a link token or exchange one unless the saved keys are Sandbox keys, so
no Production connection can appear while you look around.

To switch later: **Settings → Connections → Plaid environment**, then paste the matching keys (*Replace keys*).
Choosing Production ends demo mode.

## Where the keys go

- **Pasted in the app** (the wizard, or Settings → Connections → Replace keys): written to the secrets file,
  `~/.config/hermes-finance/secrets.env` (`FINANCE_SECRETS_FILE` moves it). The file is 0600 in a 0700 directory and
  written atomically.
- **Or in an env file**: `PLAID_CLIENT_ID`, `PLAID_SECRET` and `PLAID_ENV` in `~/.hermes/.env` (`HERMES_ENV_FILE`
  names another file). Values in the secrets file win.

No key is ever sent to the browser, printed or logged. Output and logs pass through a redactor that removes the
secret, every access token it has loaded, and anything shaped like a Plaid access or public token.

## Connecting a bank

**Add account → Link a bank or card** (or the wizard's step 5) opens Plaid Link in a pop-up:

- Link shows the app as "Hermes Budget". Country: US. Language: English.
- Products: `transactions` (730 days of history requested), plus `liabilities` and `investments` when the bank
  supports them.
- In Sandbox, pick any bank and sign in as `user_good` / `pass_good`.

When Link finishes, the server:

1. checks that the bank isn't connected already (see [duplicates](#duplicates)),
2. exchanges the public token for an access token,
3. appends the token to `data/tokens.backup.jsonl` (0600, fsync'd) before anything else can fail,
4. stores the connection (a Plaid *Item*) in the database,
5. runs the first sync.

Plaid can take a while to pull a long history. While it does, `finance sync` adds `plaid status <STATUS>` to that
bank's line (for example `NOT_READY`). Sync again later.

## The 10-connection limit {#slots}

!!! warning "Read this before connecting real banks"
    The app allows **10 Plaid connections for the life of the database**, the limit of Plaid's Trial plan
    (`config.SLOT_LIMIT = 10`). Every connection ever exchanged counts, and none is ever given back.

- `finance status`, Settings → Connections and the Add account sheet all show *slots used: N / 10*.
- At 10, the server refuses to make a new link token. **Reconnect** keeps working.
- The count is the number of Items in this database (plus any blocked duplicate connection Plaid counted anyway),
  whatever the environment or plan. A Sandbox demo connection
  counts too. To connect real banks to a clean count, start them in a fresh data directory: stop the app, then move
  `data/` aside, or set `FINANCE_DATA_DIR` to another directory.
- The app has no *remove* for a Plaid connection, and it never calls Plaid's `/item/remove`.
- The code refuses to call any `/sandbox/*` endpoint. Only the Link flow can create a connection.

### Duplicates {#duplicates}

Never connect the same bank twice: it uses a second slot. Two guards stop it:

1. In the browser: picking a bank that's already connected closes Link before you sign in.
2. On the server, the guard that decides: before the exchange, it compares Link's institution with the stored ones
   and refuses ("… is already connected — use Reconnect instead"). Each refusal is recorded, and `finance status`
   says how many there were.

### Reconnect

When a bank needs you to sign in again (for example `ITEM_LOGIN_REQUIRED` after a password change), it gets a red
dot and a **Reconnect** button: in the sidebar, the Overview alerts and the Accounts page. Reconnect opens Link in
*update mode*. It re-authenticates the existing connection, never creates a new one, and re-syncs when it's done.
`finance sync` prints ``ERROR ITEM_LOGIN_REQUIRED (open `finance serve` and click Reconnect)`` for such a bank.

Temporary trouble (no network, Plaid maintenance, a rate limit, the bank down) is logged as a failed sync, but the
bank's saved status stays as it was.

## Syncing

- **`finance sync`** pulls new, changed and removed transactions from each bank's stored cursor, balances, and
  liabilities and holdings where the bank has those products. It then applies your rules and Plaid's categories, and
  fetches missing logos. It exits 1 only if every bank failed.
- **Sync now** (the dashboard header, the `finance_sync` tool) runs the same sync. One runs at a time (a second
  gets 409), at most once a minute per bank (429 with `retry_after`).
- **Ask banks for fresh data** (in the Sync now panel, or `finance_sync` with `refresh=true`) first calls Plaid's
  `/transactions/refresh` and `/investments/refresh`, at most once per 30 minutes per bank. New items usually land a
  few minutes later.

Plaid bills each refresh request on paid plans. Plaid has no API that tells the app which plan you're on, so you pick it
in **Settings → Connections → Plaid plan** (Trial or Paid). *Allow on-demand bank refresh* defaults to on for Trial
and in Sandbox, and off for Paid. On Paid, an agent's `refresh=true` needs your approval.

## Troubleshooting

| You see | What it means |
|---|---|
| `config error: Plaid credentials file not found` or `missing PLAID_CLIENT_ID` | No keys yet: paste them in the wizard or Settings, or add them to the env file. |
| `All 10 Plaid Trial slots are used` | The [limit](#slots). Reconnect still works. |
| `… is already connected — use Reconnect instead` | The [duplicate guard](#duplicates) refused a second connection to the same bank. |
| `Demo mode only uses Plaid Sandbox, and the saved keys are for Production` | Demo mode is on with Production keys. Choose *Connect real banks* in Settings → Connections. |
| `Couldn't reach Plaid. Nothing changed; try again in a minute.` | Every bank failed with a network error. |
| `plaid status NOT_READY` on a sync line | Plaid is still pulling the history. Sync again later. |
