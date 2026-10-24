# Getting started

There are two ways to install:

- **Standalone**: the dashboard and the `finance` CLI. Use the Ask panel with an API key, or with no AI at all.
- **With Hermes**: install the app the same way, then add the Hermes plugin. Hermes gets the `finance_*` tools, the
  Ask panel can use Hermes's model, and Hermes can run scheduled reports.

Both start with the same steps.

## What you need

- **Python 3.12** and **[uv](https://docs.astral.sh/uv/)**. The project pins 3.12 (`.python-version`,
  `requires-python = ">=3.12"`) and is built and locked with uv (`uv.lock`).
- **A Plaid account.** Sandbox keys are enough to try it with fake banks. Production keys connect real banks.
- Optional: **Hermes**, for the plugin mode.
- Optional: **Google Chrome with remote debugging on port 9222**, only if you want car values from Carfax (they are
  off by default).

## Install

```sh
git clone <repo-url> hermes-finance
cd hermes-finance
uv sync                      # creates .venv on Python 3.12
uv run finance serve         # http://127.0.0.1:8765/
```

`finance serve` binds `127.0.0.1:8765` (`--port` changes the port). Without Plaid keys it starts anyway and prints:

```text
hermes-finance serving on http://127.0.0.1:8765/  (Ctrl-C to stop)
access: set to This computer only (the first start of this version; change it in Settings)
no Plaid keys yet: open http://127.0.0.1:8765/ to set up (they're saved in /home/you/.config/hermes-finance/secrets.env)
```

There is no `finance setup` command: first-run setup happens in the browser.

Optional extras:

| Extra | What it adds |
|---|---|
| `uv sync --extra mcp` | The MCP server (`finance mcp`, `/mcp`). Without it `/mcp` answers 501 and every other command works. The dev dependency group, which a plain `uv sync` installs, includes it too. |
| `uv sync --extra photos` | Background removal for car photos (rembg's `birefnet-general` model, about 973 MB, downloaded once). Without it a photo is shown as it is. |

## First run: the setup wizard

Open <http://127.0.0.1:8765/>. A new install opens on the setup page (`#/setup`) and shows it until you have Plaid keys,
a bank, or an account added by hand, or until you finish or skip it. It has six steps. Every step except the keys can be skipped and done later in
Settings.

1. **Welcome.** Paste your Plaid client ID and secret, and choose how AI should work: Hermes (preselected when
   `~/.hermes` exists), an API key, or none.
2. **Who can open this app?** This computer only, Behind your proxy login, or Built-in login. See
   [Security model](security.md#access-modes).
3. **Try demo or connect real banks.** *Try demo* uses Plaid Sandbox and fake data. *Connect real banks* uses
   Production.
4. **Alpaca** (real banks only, optional). A read-only key for an Alpaca brokerage account.
5. **Connect the first bank** with Plaid Link, or add an account by hand.
6. **Suggested budgets** from the last 60 days. Tap *Use* on a row to save it as that category's monthly cap.

The keys are written to the secrets file (`~/.config/hermes-finance/secrets.env`, mode 0600 in a 0700 directory) and
never sent back to the browser.

## The five-minute Sandbox run

1. Get the **client ID** and the **Sandbox secret** from your Plaid account.
2. `uv sync`, then `uv run finance serve`, then open <http://127.0.0.1:8765/>.
3. Step 1: paste both keys. For AI, pick *No AI for now* if you just want to look around.
4. Step 2: keep **This computer only**.
5. Step 3: **Try demo** (Sandbox, fake data).
6. Step 5: **Connect a bank**. Plaid Link opens. Pick any bank and sign in as `user_good`, password `pass_good`
   (the wizard shows these in demo mode).
7. The app exchanges the token, stores the connection and runs the first sync. Your dashboard has data.

Then try the terminal (in another shell, from the checkout):

```sh
uv run finance status                 # banks, accounts, balances, slots used
uv run finance sync                   # pull what's new
uv run finance spend --by category    # this month's spending by category
uv run finance networth               # assets, debts, net worth
uv run finance bills --next 14d       # recurring bills coming up
```

From another directory, use `uv run --project /path/to/hermes-finance finance <command>`.

If you'd rather keep the keys in a file than paste them, put them in the env file (`~/.hermes/.env` by default, or
the file `HERMES_ENV_FILE` names):

```sh
PLAID_CLIENT_ID=<your client id>
PLAID_SECRET=<your sandbox secret>
PLAID_ENV=sandbox            # production (the default) or sandbox
```

With keys present, the wizard doesn't open. Connect a bank from **Add account** instead. The secrets file's values
win over the env file's.

## Where your data lives

| What | Where | Change it with |
|---|---|---|
| The database | `data/finance.db` in the checkout (directory 0700, file 0600, WAL mode) | `FINANCE_DATA_DIR` |
| Bank token backup | `data/tokens.backup.jsonl` (0600, append-only, written before a new connection is stored) | `FINANCE_DATA_DIR` |
| Car and home pictures | `data/images/` | `FINANCE_DATA_DIR` |
| Keys pasted in the app | `~/.config/hermes-finance/secrets.env` | `FINANCE_SECRETS_FILE` |
| Keys in an env file | `~/.hermes/.env` | `HERMES_ENV_FILE` |
| Alpaca's read-only key | `~/.config/hermes-finance/alpaca-live.env` | `HERMES_ALPACA_KEY_FILE` |

`data/`, `*.db` and `.env*` are in `.gitignore`.

## Keeping it up to date

The app doesn't schedule anything itself. Something has to run `finance sync` regularly:

- **With Hermes:** `hermes finance crons` sets up a daily job (sync, payment matching, valuations, the classification
  queue, alerts) and a weekly summary. See [Scheduled reports](plugin-crons.md).
- **Without Hermes:** run `uv run --project /path/to/hermes-finance finance sync` from your own scheduler (cron,
  launchd, systemd). Add `finance match-payments` after it if you track manual loans or cards by a match pattern.

You can also tap **Sync now** in the dashboard header at any time.

## Install the Hermes plugin

The plugin lives in `hermes_plugin/`. It loads the app from your checkout, so install the app first (above).
Hermes runs plugins on its own Python (3.11 at the time of writing). The app needs 3.12, so installing the app
package into Hermes's environment isn't supported. Point the plugin at the checkout instead.

1. **Put the plugin where Hermes looks for it.** Copy or symlink `hermes_plugin/` to
   `$HERMES_HOME/plugins/hermes-finance` (`~/.hermes/plugins/hermes-finance` by default).

    ```sh
    mkdir -p ~/.hermes/plugins
    ln -s "$PWD/hermes_plugin" ~/.hermes/plugins/hermes-finance    # run from the checkout
    ```

2. **Enable and configure it** in the profile's `config.yaml`. Add `hermes-finance` to `plugins.enabled`, and set
   its settings:

    ```yaml
    plugins:
      enabled: [hermes-finance]
      entries:
        hermes-finance:
          settings:
            HERMES_FINANCE_APP_DIR: /path/to/hermes-finance   # the checkout (its src/ is loaded)
            HERMES_FINANCE_DATA_DIR: ""                       # empty: the app's own choice (FINANCE_DATA_DIR, else data/)
    ```

3. **Check it.** `doctor` loads the plugin in an isolated home, so it reads the app directory from the environment:

    ```sh
    HERMES_FINANCE_APP_DIR=/path/to/hermes-finance hermes plugins doctor ~/.hermes/plugins/hermes-finance --ci
    hermes plugins list --plain
    ```

    `doctor` should report that registration passed, with 16 tools and 1 hook.

4. **Use Hermes in the Ask panel** (optional): Settings → AI provider → **Hermes**. This is the default. See
   [Ask through Hermes](ask-hermes-bridge.md).
5. **Scheduled reports** (optional): `hermes finance crons status`, then `plan`, then `apply`. See
   [Scheduled reports](plugin-crons.md).

The plugin's reads open the database read-only. Its four write tools ask for your approval in Hermes first. See
[AI modes](ai-modes.md#hermes-plugin).

## Upgrading

```sh
git pull
uv sync
```

The database upgrades itself on the next `finance serve` or `finance sync`. `db.connect` applies the schema when the
database's `PRAGMA user_version` is behind, and keeps every row. Read-only commands and the agent tools never upgrade a
database. On an older one they stop with "run `finance sync` once to upgrade it".

Before an upgrade, stop `finance serve` and copy the `data/` directory. **Settings → Data → Download backup** takes
the bank tokens, the login and the MCP tokens out of its copy, so it can't restore your connections. After restoring
from it, you reconnect each bank.
