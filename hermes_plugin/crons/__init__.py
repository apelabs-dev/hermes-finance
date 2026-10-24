"""`hermes finance crons`: the daily and weekly finance report jobs, set up and kept in step by an explicit apply.

Nothing here runs on import, on `register(ctx)`, on `hermes plugins doctor` or on any other command: `status` and `plan`
only read (jobs.json, the plugin's own state, the installed entry scripts), and `apply` changes Hermes's cron jobs and
the profile's scripts only for a plan the operator saw, under a profile-wide lock.

  roles.py      the two jobs the plugin owns (a new job's shape; the exact signature an older job is adopted by)
  adapter.py    the one place that touches Hermes's scheduler (its cron.jobs module: internal, checked on use)
  planner.py    what apply would do, and the plan id that pins it
  entry.py      the entry scripts: rendering, path checks, install with a backup and a readback
  reconcile.py  the lock and apply itself (each change read back before ownership is recorded)
  command.py    the argparse tree and the three handlers

Stdlib only: Hermes loads the plugin on its own Python (3.11 today), not the app's."""
