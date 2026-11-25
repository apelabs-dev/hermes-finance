# SimpleFIN setup

SimpleFIN Bridge is a second, read-only bank connection. It is a way around the [10-connection limit](plaid.md#slots)
and needs no Plaid keys. The app talks to it with a small stdlib HTTP client (`src/hermes_finance/simplefin.py`), and
the connection is read-only: the app never moves money.

You can use Plaid and SimpleFIN side by side. They end in the same pages: accounts, balances, transactions, budgets
and net worth.

## Create a setup token and paste it

1. At [SimpleFIN Bridge](https://beta-bridge.simplefin.org/), create a setup token for this app.
2. In the app: **Add account → Link a bank or card → Continue with SimpleFIN**.
3. Paste the token under **Setup token** and choose **Connect**.

The token works once. The app trades it for a private access link (an `https://user:password@host/path` URL) and
keeps that in the secrets file, `~/.config/hermes-finance/secrets.env` (0600, `FINANCE_SECRETS_FILE` moves it), next
to your Plaid keys. The access link is never written to the database, never sent to the browser, and never printed or
logged: it is registered with the redactor the moment it is read. A token Bridge refuses (mistyped, or
already used) gets the message *SimpleFIN Bridge didn't accept this token. It may be mistyped or already used: create a
new one at SimpleFIN Bridge.*

## Confirm the account types

SimpleFIN does not send an account type, so the app guesses one from the account's name, whether it carries holdings,
and the sign of its balance. **Confirm what it found** lists every account with its guess: Checking, Savings, Credit
card, Loan or Investment. Every account you add needs a type before the first sync. *Don't add* skips it.

An account that looks like one already on Plaid (same last four, name, institution or balance) is marked **On Plaid**.
It is skipped by default; **Add as a second account** creates it but leaves it out of every total until you pick a
source (see [Duplicates](#duplicates-and-picking-a-source)).

## What is imported

- **Accounts:** name, last four when the name ends with them, balance, available balance and currency. The connection
  name (from SimpleFIN's `connections`) is the bank's name.
- **Transactions:** posted and pending rows, with the description, payee and date. SimpleFIN's amount is positive for
  money in; the app stores its own Plaid sign (positive is money out), so the two providers agree everywhere.
- **No holdings and no liability detail.** The Bridge may send holdings for an investment account; the app keeps the
  balance only and shows *holdings not imported* on the Investments page. Investment, card and loan accounts are
  balance-only in this version.
- **No Plaid category.** A SimpleFIN row has no Plaid personal-finance category, so the app applies your rules and
  then leaves it for the AI categorizer; until it is filed, the page shows *Uncategorized*.
  Money coming in on a bank account is filed as Income, or as a transfer when the description says so; a card's own
  payment line is a transfer. A refund is never Income: on a card or loan, and on any line that says refund, return,
  reversal or credit voucher, money in stays unfiled and nets against spending. The exception is a tax refund on a bank
  account (a description with TAX REFUND, TAX REF, TAXRFD, TAX RFD or IRS TREAS): it is Income.

## The request budget

SimpleFIN expects 24 `/accounts` requests a day or fewer. The app counts every request in the database, so the app,
the CLI and the Hermes jobs share one budget:

- **24 requests a day** in total. The daily sync, each **Sync now**, the claim and the first backfill all count.
- **6 manual syncs a day.** The seventh says so and makes no request. The daily sync is not one of the six.
- **No retry loop.** If the Bridge says the quota is used up, the connection shows a limited state and the next daily
  sync tries again.

Each install picks a random daily minute once, at the claim (shown in Settings and `finance simplefin status`).
Schedule the external daily job at that minute. The daily run also waits a random few seconds before its request, so
installs do not fire together. The app does not run a scheduler.

## The first sync and later ones

The first sync asks for up to one year in 89-day windows (at most five requests). Every later sync asks from
`max(coverage_start, last_success - 5 days)` to today, so five days overlap and a transaction that posts late is not
missed. Coverage is what the Bridge actually returned: the earliest transaction date, not the year the app asked for.
If the Bridge only kept 90 days, Settings says 90 days.

## Pending rows

Pending transactions are imported with their own id. When the posted charge arrives within five days for the same
account, with a similar description and up to 25% more (a tip), the two are matched one-to-one: the posted row is the
charge and the pending row is kept, soft-removed, with the link between them. If more than one pairing fits, nothing
is merged; both rows stay for review and the sync reports how many. A pending row that never posts is soft-removed
after ten days, and one the Bridge stops returning is soft-removed as soon as a successful answer covers its date. Rows
are never deleted, and a transaction id the Bridge returns again counts again, whether it comes back pending or posted;
only a pending merged into a posted twin that is still returned stays soft-removed, so the charge is counted once. When
an answer's `errlist` names a connection (`con.*`) or an account (`act.*`), that account's pending rows are left alone
for that answer: a partial answer is not read as a removal.

## Duplicates and picking a source

A SimpleFIN account that is already on Plaid is skipped, or added as a second account. Until you choose, the
SimpleFIN copy is in the account policy (`excluded_accounts`), so every total is Plaid-only. When the duplicate is
shown:

- **Use Plaid** keeps the SimpleFIN copy out and clears the review marker.
- **Use SimpleFIN** stops counting the Plaid copy and starts counting the SimpleFIN one.
- **Not a duplicate** counts both.

The choice is under **Settings → Connections → SimpleFIN** and on `GET /api/simplefin/status`
(`duplicates`); no money math changes, only which account the existing policy counts.

## Disconnect and reconnect

**Settings → Connections → SimpleFIN → Remove** removes the connection on this computer only. It does **not** revoke
it at SimpleFIN Bridge, so remove the app there too. The item is marked removed, its accounts go into the account
policy, and every row stays. A new setup token reconnects with the same deterministic ids
(`simplefin:<connection>:<account>` and `simplefin:<connection>:<transaction>`): no row is orphaned or counted twice.

## Broken states

| You see | What it means |
|---|---|
| **Needs a new login** and *Balances stay as of <date>* | The access link was revoked or expired (HTTP 403). Paste a new setup token; **Fix at Bridge** opens SimpleFIN Bridge. |
| **Subscription needed** | SimpleFIN answered HTTP 402: activate the subscription. The last balances stay. |
| **Budget used up** | The 24 requests a day are spent. Nothing was changed. |
| **Needs confirming** | A token was claimed but the account types were not confirmed. **Finish setup** in Settings. |
| **Needs retry** | The access link is saved, but SimpleFIN's first answer never arrived. **Try again** reuses the saved link; no new setup token is needed. The same happens after a reconnect whose first fetch fails: the new link is kept, so the spent token is never needed again. |

A broken connection never replaces a balance with zero. The last synced balances stay, with the date they are from.

## Command line

`finance sync` syncs Plaid and SimpleFIN together; a failure in one provider does not stop the other. The daily
Hermes pre-run runs `finance sync --scheduled`, which is the daily SimpleFIN request and not one of the six manual
syncs.

```sh
finance simplefin status            # connection state, requests today, coverage, duplicates
finance simplefin status --json
finance simplefin claim <token>     # claim and confirm: --type KEY=checking (repeatable), --skip KEY
```

## Privacy

The access link is a secret: it lives only in the 0600 secrets file and is scrubbed from output and logs. The client
resolves the host at connect time and refuses any address that is not public (no localhost, private, link-local or
metadata address), follows no redirects, uses a 10-second timeout and caps the response; the setup token is
shape-checked before it is decoded. Errors from SimpleFIN's `errlist` are stripped of links and control characters;
`gen.api` entries are developer errors and are not shown as a broken connection.

## Database version

The SimpleFIN step is schema v21: `items.provider`, `accounts.conn_id`, the widened `transactions.source` and the
`simplefin_requests` request-budget table; `db.SCHEMA_VERSION` has moved on with later steps and its comment lists
them. The migration from v20 is additive and keeps every row. A 0.3.0 binary opening a v21 file does not recognise
SimpleFIN: it treats a `simplefin:*` item as a Plaid item with an empty token. Back up before upgrading, and do not
downgrade.
