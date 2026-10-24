# Backup and restore

The whole household is one SQLite file, `finance.db` in the data directory (`data/`, or `FINANCE_DATA_DIR`). A backup
is a copy of that file with every credential taken out; a restore puts one back. Both live in
`src/hermes_finance/backup.py`.

## Taking a backup

```sh
finance backup --out ~/Backups/                       # writes ~/Backups/hermes-finance-backup-YYYY-MM-DD.db
finance backup --out ~/Backups/before-upgrade.db      # or exactly this file
```

or Settings → Data → **Download backup** (`GET /api/backup`). The two produce the same thing:

- **A consistent copy**, made with SQLite's online backup API: safe while `finance serve` is running (what's still in
  its WAL is included), nothing has to stop.
- **Without any credential**: the bank access tokens are replaced by `removed-from-backup`, and the built-in login
  (user, password hash, 2FA key, recovery codes, sessions, lockouts) and the MCP tokens are deleted. The file is then
  vacuumed with `secure_delete` on, so nothing of them is left in its free pages.
- **0600**, written beside its destination and renamed into place: the file is either the whole backup or isn't there.
  `finance backup` never overwrites an existing file.
- **The database only.** Not the secrets file (Plaid and AI keys), not Alpaca's key file, not the env file, not the
  cached car and home pictures (`data/images/`) and not `data/tokens.backup.jsonl` (the bank tokens' own append-only
  log). Copy `data/images/` along with it if you want the pictures back without fetching them again.

A backup holds your whole financial history in the clear: keep it somewhere only you can read.

## Restoring

Stop `finance serve` (and anything else using the database) first, then:

```sh
finance restore ~/Backups/hermes-finance-backup-2026-10-23.db            # into an empty data directory
finance restore --force ~/Backups/hermes-finance-backup-2026-10-23.db    # replacing a database that holds data
```

In order, changing nothing until every check has passed:

1. **The backup is checked**: it must open as SQLite, pass `PRAGMA integrity_check`, be a hermes-finance database
   (its tables are there), and its schema version must not be newer than the code's (`db.SCHEMA_VERSION`):
   downgrades aren't supported, so upgrade the app first. The backup file itself is only read (opened read-only, and
   `immutable` when there's no `-wal` beside it, so not even a `-shm` is created next to it).
2. **The target is checked**: a database already there that holds data (any bank Item, account, transaction, manual
   account, group budget or goal) is replaced only with `--force`. An empty one (a fresh `finance status`) is replaced
   without it.
3. **A consistent copy** of the backup is written beside the target (`.finance.db.restore-…`, 0600, out of WAL mode)
   and integrity-checked again.
4. **The old database is moved aside, never deleted**: its WAL is checkpointed into it and it's taken out of WAL mode
   (so no `-wal` is left to be replayed into what replaces it; this fails, and nothing changes, while another process
   has it open), then renamed to `finance.db.<YYYYmmdd-HHMMSS>.pre-restore` in the same directory.
5. **The copy is renamed into place** and opened through `db.connect()`, so the migrations run: a backup from an older
   version is upgraded exactly as the app would upgrade it at start.

Then it prints what to redo:

- **Reconnect each bank** it lists (Accounts → Reconnect): their access tokens weren't in the backup.
- **Set the built-in login up again** (Settings → Access & security), when the access mode is "Built-in login" (or
  `finance access mode local` to go back to "This computer only").
- **New MCP tokens** for other agents that used them.
- **The pictures** missing from `data/images/`, if any: copy the old folder there.
- **The keys**: the Plaid and AI keys and Alpaca's key file are per machine. `finance doctor` lists what's missing.

To undo a restore: stop the app, move `finance.db` away and rename the `.pre-restore` file back to `finance.db`.

## Exit codes

`finance backup`: 0 written; 1 no database, or `--out` exists or its directory doesn't. `finance restore`: 0 restored;
1 refused (the message starts with `not restored:` and says why; nothing was changed). Both: 2 bad usage.

Tests: `tests/test_backup_restore.py` (a synthetic database backed up through the route and the CLI, restored into a
fresh directory, compared table by table and through the read commands; every refusal).
