# FAQ

## Do I need Hermes?

No. The dashboard, the CLI, the MCP server and the API-key Ask panel all work without it. Hermes adds native tools, Ask
through your Hermes model, and scheduled reports. See [AI modes](ai-modes.md).

## Can I try it without real banks?

Yes. Use Plaid **Sandbox** keys and *Try demo* in the setup wizard, then sign in to any bank in Link as `user_good` /
`pass_good`. See the [five-minute run](getting-started.md#the-five-minute-sandbox-run). The demo connection counts
toward the 10-connection limit of that database, so try it in a separate data directory if you'll connect real banks
in the same install later.

For a look with no Plaid account at all, build a synthetic database:

```sh
mkdir -p /tmp/hf-demo
uv run python tests/demo_data.py /tmp/hf-demo                       # writes /tmp/hf-demo/finance.db
FINANCE_DATA_DIR=/tmp/hf-demo uv run finance serve --port 8799      # http://127.0.0.1:8799/
```

## Does it work outside the US?

Plaid Link is opened for US institutions only (`country_codes: ["US"]`). The currency setting changes how amounts are
written. Nothing is converted.

## I pay for Plaid. Do I still get only 10 connections?

Yes, today. The limit is fixed in the code (`config.SLOT_LIMIT = 10`, the Trial plan's) and counts every connection in
the database, whatever the plan. The *Plaid plan* setting only changes the refresh rules.

## How do I disconnect a bank?

The app has no remove for a Plaid connection, and it never calls Plaid's `/item/remove`. A bank that stops syncing needs
**Reconnect**, never a second connection. See [Plaid setup](plaid.md#reconnect).

## Why isn't a transfer or my paycheck in "spending"?

Spending is every category except **Income** and **Transfers**, as on the dashboard. Refunds net against their
category. Money out is positive and money in is negative, following Plaid's sign.

## Can an agent change my data?

Only with your approval. MCP clients are read-only unless you allow writes, and even then each change waits in
Settings → Connect other agents. The Hermes plugin asks before each write, and refuses writes where nobody can
answer (scheduled jobs, unattended platforms). The Ask panel shows an approval card. `finance_sync` is the exception:
it pulls new data from Plaid without asking, but never changes anything you entered.

## Is my data sent anywhere?

Your bank data is in a local SQLite file. What leaves the computer, and when, is listed in
[Security model](security.md#what-leaves-your-computer).

## Can I open it from my phone?

Yes, with *Behind your proxy login* (for example Tailscale Serve in front of the local server) or *Built-in login*.
`finance serve` refuses to start where it would be exposed without one of them. See
[Security model](security.md#access-modes).

## I'm locked out of the built-in login.

On the machine running the app: `finance access reset-login`. It removes the login (password, authenticator, recovery
codes, sessions) and goes back to *This computer only*.

## A command says "the database is at schema vN, these commands need vM".

Read-only commands never upgrade the database. Run `finance sync` (or start `finance serve`) once, and it upgrades
itself, keeping every row.

## What does "payment in transit" mean?

A card payment that left a connected bank account in the last 5 days, and that the card hasn't posted yet. Net worth
counts it once, as already paid. The stored balances don't change.

## How do I back up?

Stop `finance serve` and copy the `data/` directory: it has the database and the token backup. **Settings → Data →
Download backup** is a copy with every credential removed. It's safe to keep elsewhere, but after restoring from it you
reconnect each bank.

## Can I query the database myself?

`finance query "<SQL>"` runs read-only SQL (SELECT and introspection only; bank tokens read as `NULL`). For example:

```sh
uv run finance query "SELECT month, category, spend FROM v_monthly_category_spend ORDER BY month DESC LIMIT 12"
```

Writes go through the CLI's own commands (`set-category`, `add-rule`, `manual ...`), so their checks always apply.
