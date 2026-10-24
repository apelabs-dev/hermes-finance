Scheduled weekly finance summary (hermes-finance). The pre-run output above synced the banks once and saved this week's overview, budgets and recurring snapshots (the *_file paths). Each step is marked ok, partial or FAILED.

Read those files and write a short weekly summary: spending this month against the budgets (category caps and group budgets, each counted once as the snapshot's totals do), the categories and groups near or over their budget, bills and subscriptions due in the coming week, and any connection that needs attention. If a snapshot is marked FAILED or stale, say so and don't present numbers from an older file as this week's. Use only the files named above or the finance_* read tools.

Do not call finance_sync or any finance_* write tool. A scheduled run has nobody to approve a change, so the plugin blocks them; if something needs the owner's decision, say so in the summary.

Format dollar amounts like the app: $1,250 for whole dollars after rounding to cents, $12.50 otherwise; use -$1,250 for negatives and $0 for rounded zero. Use commas for thousands and a minus before the dollar symbol.
