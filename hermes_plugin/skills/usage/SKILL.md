---
name: usage
description: Use when the user asks about their money, spending, budgets, bills, net worth, loans or investments, and the finance_* tools are available.
version: 0.1.0
---

# hermes-finance: answering money questions with the finance_* tools

For day-to-day questions ("how much did I spend eating out last month", "what's left in Shopping", "what bills are
coming", "what's my net worth"). Every number comes from the user's own hermes-finance database through the tools
below; never from memory, never estimated.

## Where the data lives

- One local SQLite database, filled by hermes-finance from the user's banks (Plaid) plus accounts they entered by hand
  (cars, homes, loans, cards Plaid can't see). The read tools open it read-only and never call a bank.
- The data is only as fresh as the last sync: `finance_status` shows each bank's last sync, any connection error and
  `data_age` ("synced 14 h ago"). Say so when a bank needs reconnecting instead of presenting stale numbers as current.
- `finance_sync` pulls what the banks already sent (no approval needed; one sync a minute per bank) and says what
  changed. `refresh=true` first asks the banks for fresh data: only if the user allows it in Settings, and on a paid
  Plaid plan the user approves it (Plaid bills each refresh). New items land a few minutes later: sync again then.
- Net worth may carry a "Payment in transit" line: a card payment that left the bank but the card hasn't posted yet,
  counted as already paid. Mention it when it explains a change.
- Dates are the local dates the banks report; "today" is the machine's local date.

## Read tools (read-only, safe, no approval needed)

| Tool | Answers |
|---|---|
| `finance_spend` | spending for a period vs the previous equal period; `by` = category, merchant, month or account |
| `finance_budget_status` | each budget cap and group budget (`groups`): spent, bills still due, left, % used, status, projection; uncapped spend |
| `finance_transactions` | transactions newest first; `search` (notes and tags too), `tag`, `flagged`, `uncategorized`; who set each category |
| `finance_networth` | assets, debts, net by group (cash, investments, vehicles, property, cards, loans); `custom` |
| `finance_bills` | recurring bills and subscriptions due in the next N days (`next`, default 14d), overdue ones too |
| `finance_loans` | loans: remaining, APR, payment, payoff estimate, last matched payment |
| `finance_holdings` | investment accounts and holdings with value and % of the portfolio |
| `finance_cashflow` | income vs spending per month (`months`, default 6) |
| `finance_status` | connected banks, errors, last sync, balances |
| `finance_goals` | the user's goals (save up, pay off, monthly savings): progress, what's left, pace |
| `finance_rules` | categorization rules (label, category, yours or AI, matches now) and suggested rules; deleting one is for the user in Settings |

Periods: `month` (YYYY-MM), or `since` (`30d` = the last 30 days including today, or YYYY-MM-DD) with an optional
`until`. No period means this month (`finance_transactions`: all history). `category`, `merchant` and `account`
narrow any of them. A bad argument comes back as an `error` with the reason: fix it and call again.

## Write tools (see the approval rules)

| Tool | Changes |
|---|---|
| `finance_set_category` | files one transaction under a category, as the user's own decision |
| `finance_add_rule` | files every transaction whose merchant name (or name) matches a pattern, now and on every sync |
| `finance_set_cap` | sets a spending category's monthly budget cap, or clears it (`monthly_cap: null`) |
| `finance_track_bill` | tracks a merchant as a bill by hand (`action: track`, `every`: 2w/1m/2m/3m/6m/1y, `varies`, `usually`: an amount or `[low, high]`), changes one (`update`) or stops one (`stop`) |

Bills: `finance_bills` marks a bill whose amount changes (`variable`, with its `estimate`, `amount_label` and `usual`
range), whether it was found or tracked by hand (`source`), the account it was last paid from (`last_account`) and a
last charge far from the usual (`outlier`). Call a variable bill's amount "about" that much, an estimate, never an exact
figure. `finance_track_bill` needs the merchant's name as the transactions show it: if it's refused as unknown or
ambiguous, pick from the names it lists, or ask.

## Standing rules

- **Money text.** Use the dollar symbol and thousands separators. Round to cents, then omit cents for whole amounts and keep two decimals otherwise. Put an ASCII minus before the symbol for negatives; suppress it for rounded zero. Keep numeric tool arguments and JSON fields numeric.
- **Sign convention.** Spend and money out are positive; refunds and income are negative. Spending totals already
  net refunds against their category. Income and transfers (card payments, moves between the user's own accounts)
  are never spending. Cash flow shows income as a positive number.
- **Never guess a category.** Use the exact names the tools return (`finance_spend` with `by: "category"`, or
  `finance_budget_status`). If the user's word doesn't match one ("food"), ask which one they mean, or show the
  close ones. Never invent a category, and never file a transaction on a hunch: ask.
- **Quote, don't recompute.** The tools already apply the app's rules (what counts as spending, bills still due,
  the pace projection, Custom net worth). Report their numbers; don't re-add rows yourself.
- **Say what's missing.** A month before a bank's first transaction is partial (`coverage: partial` in cash flow);
  a manual balance can be stale (`stale: true`); an estimate has a date. Mention it when it matters.

## How to answer

1. Pick the narrowest tool: a category's spend is `finance_spend` with `category`, not a list of transactions.
2. Lead with the number and the period ("$X on Eating Out in August, across N transactions"), then the comparison
   the tool gives (previous period, cap left, projection).
3. Round to cents; keep currency signs; name the period and any filter used.
4. For "why is X so high", follow with `finance_spend` `by: "merchant"` for that category, then
   `finance_transactions` for the top merchant.
5. Keep private data private: don't repeat account numbers beyond the last digits the tools show.

## Approval rules

- Every write needs the user's approval: Hermes shows them an approval card with the change in plain words before
  anything happens. Explain what you're about to change before calling; never call a write tool "to check".
- Only write when the user asked for that change (or clearly agreed to your suggestion). Reading never needs approval.
- `finance_add_rule` matches by regex (case-insensitive, searched anywhere in the name) unless `match: "exact"`. A
  pattern that matches no transaction is refused: look the merchant up first (`finance_transactions` with `search`),
  and escape regex characters (`.`, `*`, `+`, `(`) in names. The result says how many transactions were re-filed.
- A denied or blocked call changed nothing. Don't retry it unchanged; ask the user what they want instead.

## Asset values

- Vehicles and homes the user added by hand carry an estimate (a car's trade-in value, a home's online estimate) or the
  user's own value. `finance_networth` shows which one counts (`valuation.source`), where it came from and its date.
- Say "estimated" for estimates, and give the date.

## Frequent questions

- "How much did I spend on X this month?" → `finance_spend` with `category: "X"`.
- "Am I on budget?" → `finance_budget_status`: over / warn caps and group budgets first, then what's left and the
  per-day amount. A group budget (e.g. Food) covers every category in its group and counts once; a cap inside it
  (`counted: false`) is reported on its own but never added to the totals again.
- "What's coming up?" → `finance_bills` (`next: "7d"` for this week).
- "Where did my money go last month?" → `finance_spend` with `month` and `by: "category"`.
- "Net worth?" → `finance_networth` (with `custom: true` when the user counts only some accounts).
- "Why is this charge filed as Y?" → `finance_transactions` with `search`: `category_source` says who filed it (user,
  rule, ai, plaid, default).
