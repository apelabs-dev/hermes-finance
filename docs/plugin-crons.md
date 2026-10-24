# Scheduled reports: `hermes finance crons`

The Hermes plugin can set up two report jobs in a Hermes profile: a daily check at 06:30 and a weekly summary on
Sunday at 18:00. Each job runs the finance app's own pre-run steps first, then hands their output to the agent that
writes the report. Nothing is set up when the plugin is installed, enabled or loaded. You run three commands:

```sh
hermes finance crons status                    # what the two jobs are now
hermes finance crons plan --deliver discord:<channel id>
hermes finance crons apply --plan-id <id from plan> --deliver discord:<channel id>
```

`status` and `plan` only read. `plan` prints every change it would make and a plan id. `apply` needs that id and the
same options, and refuses if anything changed since (another apply, a job edited by hand). Run `plan` again in that
case. Add `--json` to any of them for machine-readable output.

## Settings

Plugin settings live under `plugins.entries.hermes-finance.settings` in the profile's `config.yaml`:

| Setting | Used for |
|---|---|
| `HERMES_FINANCE_APP_DIR` | The finance app checkout. New jobs run from it, and the entry scripts use its `.venv/bin/python`. |
| `HERMES_FINANCE_DATA_DIR` | The data directory the entry scripts point the app at. Empty: the app's own default. |
| `HERMES_FINANCE_CRON_DELIVER` | Where a new job is delivered when `plan` gets no `--deliver`. |

A destination is one Hermes delivery target written as `PLATFORM:CHAT_ID`, for example `discord:<channel id>`. `plan`
refuses `local`, `origin`, `all`, a bare platform name, a list of targets and anything Hermes can't deliver to. The
Settings page's alert "destination" label is a label only; the plugin never reads it as a target.

## Jobs that already exist

If the profile already has report jobs from an earlier setup, `apply` adopts them and keeps their IDs. It finds them
in this order:

1. The job this profile already owns. The plugin records the job IDs in its own state after each apply.
2. A job named with `--adopt daily=<job id>` or `--adopt weekly=<job id>`. The job's name or script has to be the
   role's, so a typo can't adopt an unrelated job.
3. The one job whose name, script and working directory all match the role: `hermes-finance-daily` or
   `finance-daily-classify` running `hermes-finance/daily.sh` or `finance_daily_prerun.sh` from the app directory
   (the weekly job likewise). Two matches block the role and nothing is written until `--adopt` picks one. A job that
   only mentions finance somewhere, such as a project coordinator, never matches.

An adopted job keeps every field: schedule, destination, prompt, attached skills, model, provider, paused or not, and
its run history. Only an option changes a field, and `plan` lists each change as from/to first:

| Option | Changes |
|---|---|
| `--deliver PLATFORM:CHAT_ID` | the destination |
| `--daily-time HH:MM`, `--weekly-day DAY`, `--weekly-time HH:MM` | the schedule |
| `--use-bundled-scripts` | the script, to the plugin's entry script |

A paused job stays paused. If a job the profile owns was deleted, the role is blocked until `--recreate daily` (or
`weekly`) asks for a new one. `--adopt` names one job per role.

Before it creates a job, `apply` records the job's name, script and working directory as pending. If the process
stops between the two, the next `plan` finds that job by the record, even after a settings change, and adopts it.

## New jobs

When no job exists for a role, `apply` creates one named `hermes-finance-daily` or `hermes-finance-weekly`. It runs
from the app directory, uses the bundled prompt (the plugin's `crons/prompts/`), attaches no skills and pins no model.
Its script is the plugin's entry script, `$HERMES_HOME/scripts/hermes-finance/<role>.sh`. The default schedules are
the cron expressions `30 6 * * *` and `0 18 * * 0`, and Hermes supplies the timezone: the profile's `timezone` setting
when there is one, otherwise the host's local time. The plugin passes no timezone of its own.

The entry scripts are rendered from `crons/templates/entry.sh` with the profile's own paths filled in. `apply` writes
them only inside the profile's scripts directory and refuses a symbolic link anywhere on that path (`.backup/`
included). It creates and renames each file relative to the open directory, so a directory swapped for a link during
the install gets nothing written through the link. It keeps the previous copy in `scripts/hermes-finance/.backup/`
when it replaces one, and reads each file back before recording its sha256. A relative `HERMES_FINANCE_DATA_DIR` blocks
the plan rather than falling back to the app's default database.

## What the entry scripts run

Each script runs `python -m hermes_finance.cronpipe daily|weekly` with the app's own Python. The daily run takes the
steps of the earlier `finance_daily_prerun.sh`, in the same order: `finance sync`, the Alpaca snapshot, loan payment
matching, card autopay suggestions (listed only, never saved), car and property estimates, the classification queue
(at most 120 merchant groups) and the alerts. The weekly run syncs once, as the weekly job always did, and saves the
overview, budgets and recurring snapshots. Neither run asks the banks for fresh data, which Plaid bills on paid plans.

The alerts come straight from the database, so the dashboard doesn't have to be running. Only the three kinds the
Settings page offers are reported (a category at 80% of its cap, over its cap, a broken connection), each only while
its switch is on. Switched-off integrations are skipped: AI off queues nothing, and the valuation and Alpaca steps
follow their own settings.

Every step prints one status line (`ok`, `partial`, `skipped` or `FAILED`) and the last lines of its output, and the
run ends with `== status: ok` or `== status: partial` naming the failed steps. Artifacts go to
`$HERMES_HOME/cache/hermes-finance/` (mode 0700, files 0600) under dated names. When a step fails, it writes nothing
under today's name, and an earlier file from today is renamed `*.stale.json`, so an old snapshot is never read as
this run's. A missing database stops the run with an error; it doesn't create an empty one. The run keeps within
50 minutes, under Hermes's one-hour script limit: a step with no time left isn't started and is marked `FAILED`.

## Approvals in scheduled and unattended runs

A scheduled run has nobody to answer an approval, and neither do Hermes's unattended platforms (the API server and
webhooks) or a single-query `hermes chat -q` run. With `approvals.cron_mode`, `unattended_mode` or `single_query_mode`
set to `approve`, Hermes would treat the finance write tools' approval request as a yes. So the plugin blocks them
there instead of asking:

- in a scheduled job: the four write tools and `finance_sync` (the job already ran its own sync);
- on an unattended platform or in a single-query run: the four write tools and a billed `finance_sync` refresh, also
  when the plugin can't read the database to describe the change.

The read tools answer as usual everywhere. The daily job classifies with `finance ai-apply`, the app's validated
path that applies each queue run once.

## Daylight saving time

Tests drive a copy of each new job through Hermes's own fire path across the 2026 and 2027 switches. On every
ordinary day both jobs fire at 06:30 and Sunday 18:00 local time. The two switch days depend on where Hermes gets its
timezone. The deviations come from Hermes and its `croniter` library; the plugin only hands Hermes the cron
expressions:

| Timezone source | Fall-back day | Spring-forward day |
|---|---|---|
| profile `timezone` setting | the run is skipped (daily and weekly) | on time |
| host local time (no `timezone` set) | on time | one hour late (07:30, and 19:00 for the weekly) |

## Python versions

The finance app requires Python 3.12. Hermes loads plugins with its own interpreter, 3.11 at the time of writing.
The plugin's cron modules use only the standard library and Hermes, and the tools load the app's source through
`HERMES_FINANCE_APP_DIR`; a test compiles all of it with Hermes's interpreter. The pipeline itself never runs on
Hermes's Python: the entry scripts call the app's `.venv/bin/python`, and `cronpipe` refuses an older interpreter.
Installing the app package into Hermes's environment isn't supported, because pip would refuse the version.

## Exit codes and files

| Code | Meaning |
|---|---|
| 0 | done, or nothing to do |
| 1 | refused or blocked; no job, script or state was changed (a refusal after waiting for the lock leaves the lock file) |
| 2 | wrong usage |
| 3 | another apply held the lock for `--lock-timeout` seconds (default 10); nothing was written |
| 4 | apply stopped after writing something; `plan` shows where things stand and the next apply adopts, never duplicates |

`apply` writes only these: the jobs, through Hermes's own `create_job` and `update_job`; the entry scripts and their
backups under `scripts/hermes-finance/`; and the plugin's state and lock file in its Hermes plugin data directory
(`status --json` prints both paths). `status` and `plan` write nothing; the only file that can appear while they run is
Python's own bytecode cache in the plugin's directory, as for any module Hermes imports. `status` exits 1 when it
can't read the jobs or the plugin's state.
