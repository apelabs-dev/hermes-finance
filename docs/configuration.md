# Configuration

Everything the app reads, where it reads it from, and in what order. This page describes what the code does
(`config.py`, `secretstore.py`, `prefs.py`, `alpaca.py`, `bridge.py`, `cdp_client.py`, `photos.py`, `access.py`); it
changes nothing. Check an install with `finance doctor` (below).

## The one file a new install should use

**The secrets file**, `~/.config/hermes-finance/secrets.env` (`FINANCE_SECRETS_FILE` moves it). The app writes it
itself when you paste keys into first-run setup or Settings (0600, in a 0700 directory, written atomically), and you
can write it by hand in the same `KEY=VALUE` format (`chmod 600` it). Its values win over the env file's.

The env file (`~/.hermes/.env`, `HERMES_ENV_FILE` moves it) keeps working for installs that already keep their keys
there, and is the only place the three valuation flags are read from (see below).

## Where each kind of setting comes from

| kind | read from, in precedence order | read by |
|---|---|---|
| Keys: `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV`, `AI_API_KEY` | 1. the secrets file; 2. the env file. A key with an empty value counts as not set. **The process environment is not read for these.** | `config.key_values()` (and `config.load_settings()` for Plaid: the same files) |
| Valuation flags: `CARFAX_ENABLED`, `REDFIN_ENABLED`, `PHOTOS_ENABLED` (`0` / `false` / `off` / `no` = off, anything else = on) | 1. the env file; 2. the Settings page's switch (database setting `valuations.*`); 3. the default: Carfax and Redfin off on a new install (on for an install upgraded from before this setting existed), photos on. Not the secrets file, not the process environment. | `config.free_sources()` |
| Alpaca's key: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `APCA_ENV` (must be `live`), optional `APCA_API_BASE_URL` / `APCA_BASE_URL` (must be the live host) | its own key file only: `HERMES_ALPACA_KEY_FILE`, default `~/.config/hermes-finance/alpaca-live.env` (the Connect form writes it after one read-only check). A paper key (`PK…`) is refused. | `alpaca.load_keys()` |
| Paths and switches (process environment) | the process environment only | see the next table |
| Settings kept in the database | the database (`settings` table), set on the Settings page or with the CLI | `prefs.get_many()` |

**Under the Hermes plugin** (its tools, and its report jobs, whose entry script sets
`HERMES_FINANCE_CREDENTIAL_SOURCE=hermes`) the rows above change: the Plaid and Alpaca keys come from Hermes only
(`hermes config set NAME VALUE`, read with Hermes's `get_secret`, or the environment Hermes runs a report job with),
and the valuation flags from the process environment. The secrets file, the env file and Alpaca's key file are never
opened. The Settings page can't change keys there, and a missing Plaid key reads "Plaid not configured in Hermes
(...)". Without the plugin or the variable, everything is as in the table.

### Process environment variables

| variable | default | what it does |
|---|---|---|
| `FINANCE_DATA_DIR` | `data/` in the checkout | the data directory (holds `finance.db` and `tokens.backup.jsonl`) |
| `HERMES_ENV_FILE` | `~/.hermes/.env` | the env file |
| `FINANCE_SECRETS_FILE` | `~/.config/hermes-finance/secrets.env` | the secrets file |
| `HERMES_ALPACA_KEY_FILE` | `~/.config/hermes-finance/alpaca-live.env` | Alpaca's key file |
| `HERMES_FINANCE_CREDENTIAL_SOURCE` | none | `hermes`: keys come from the process environment only and no key file is opened (the plugin's report jobs set it) |
| `FINANCE_PUBLIC_URLS`, `FINANCE_ALLOWED_USERS` | none | comma-separated; merged with `finance serve --public-url` / `--allow-user` (see [Access modes](guide.md#access-modes)) |
| `FINANCE_HERMES_BIN` | `$HERMES_HOME/hermes-agent/venv/bin/hermes` | the Hermes executable the Ask panel's bridge runs (AI mode `hermes`) |
| `HERMES_HOME` | `~/.hermes` | where Hermes is installed (only its existence and its executable are looked at; nothing in it is read) |
| `HERMES_CHROME_DEVTOOLS` | `127.0.0.1:9222` | the local Chrome Carfax runs through (`host:port`), or `off` |
| `FINANCE_CONTACT` | none | an email or URL put in the User-Agent of Wikimedia Commons requests (they ask for one) |
| `HERMES_PLAID=off` | on | kill switch: no Plaid request at all (the tests set it) |
| `HERMES_ALPACA=off` | on | kill switch: no Alpaca request |

The Hermes plugin has its own settings (`plugins.entries.hermes-finance.settings` in Hermes, else the environment
variable of the same name): `HERMES_FINANCE_APP_DIR` (the checkout to load the app from), `HERMES_FINANCE_DATA_DIR`
(empty: the app's own choice, so both read the same database) and `HERMES_FINANCE_CRON_DELIVER`. See
`hermes_plugin/plugin.yaml` and [plugin-crons.md](plugin-crons.md).

### Settings kept in the database

Set on the Settings page (or `finance access …`, `finance mcp allow-writes …`); listed by `finance settings --json`.
The ones `finance doctor` looks at:

| setting | default | meaning |
|---|---|---|
| `ai.mode` | `hermes` | `hermes` (Hermes's own model through the plugin's bridge), `api` (an OpenAI-compatible endpoint) or `off` |
| `ai.preset`, `ai.base_url`, `ai.model` | `openrouter`, its URL, empty | the `api` mode's endpoint and model (`local` preset: no key needed) |
| `access.mode` | `local` (or `proxy` when public URLs are set) | `local`, `proxy` or `builtin` |
| `access.allowed_identities` | empty | the proxy mode's allowed logins |
| `mcp.enabled` | on | `/mcp` and `finance mcp` |
| `valuations.carfax`, `valuations.redfin`, `valuations.photos` | see above | the Settings page's switches (an env-file flag wins) |

### Not used any more

`MARKETCHECK_API_KEY` and `RENTCAST_API_KEY` are no longer read by anything (car and home values come from
Carfax and Redfin, which need no key). Their old rows stay in the database as history. You can delete them from your
env file.

## `finance doctor`

```sh
finance doctor          # one line per integration; exit 0 when nothing is missing, else 1
finance doctor --json   # the same, as JSON (stable: schema 1)
```

Offline and read-only: it makes no network call (not even to the local Chrome), writes nothing, and creates no file or
directory. It works with no data directory and no database. It resolves every key through the app's own code (the
table above), and it never prints a key's value, not even in part: only key names, file paths and fixed sentences.

The integrations, always all of them, in this order: `database`, `plaid`, `ai`, `login`, `alpaca`, `mcp`, `carfax`,
`redfin`, `photos`. Each has a status:

| status | meaning | counts as a problem |
|---|---|---|
| `ok` | set up | no |
| `off` | switched off on purpose (a setting, a default, or a kill switch) | no |
| `missing` | something to set up | **yes** |
| `error` | set up but unusable (a refused key file, an unreadable database, a schema newer than the code) | **yes** |

**Exit code:** 0 when no integration is `missing` or `error`, 1 otherwise (2: bad usage). Optional integrations
(`alpaca`, `mcp`, `carfax`, `redfin`, `photos`) count too: "nothing is missing" means nothing. A caller that only cares
about what the app needs can look at the JSON's `optional` field.

**Text output**, one line per integration:

```
ok <name>
<status> <name>: <what> -> README.md#<anchor>
```

`<what>` is ASCII and never contains ` -> `. Every anchor is a heading of README.md's Configuration section.

**JSON output** (`--json`), schema 1:

```json
{
  "schema": 1,
  "ok": false,
  "missing": ["database", "plaid"],
  "integrations": [
    {"name": "plaid", "status": "missing", "detail": "PLAID_CLIENT_ID and PLAID_SECRET not set (...)",
     "keys": ["PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV"], "missing_keys": ["PLAID_CLIENT_ID", "PLAID_SECRET"],
     "optional": false, "fix": "README.md#integration-plaid-banks"}
  ],
  "files": {"env_file": "...", "secrets_file": "...", "data_dir": "...", "db_path": "...",
            "schema_version": null, "code_schema_version": 19}
}
```

- `ok`: true exactly when `missing` is empty (the exit code is 0).
- `missing`: the names whose status is `missing` or `error`, in order.
- `integrations`: every integration, in order, each with exactly these fields. `keys`: the settings it depends on
  (key names, database settings, environment variables); `missing_keys`: the ones to set (may be empty when the fix
  isn't a key, e.g. the database); `fix`: the README anchor.
- `files.schema_version`: the database's schema version, `null` without a database; `code_schema_version`: the
  version this code upgrades to.

New fields may be added within schema 1; a field is never removed or renamed without bumping `schema`.

### What each check looks at

| name | ok when | fix |
|---|---|---|
| `database` | `finance.db` exists in the data directory, can be read, is writable, and its schema is not newer than the code (an older one is `ok`: it's upgraded when the app next opens it; the upgrade is tried in memory to make sure it would work) | README, *Integration: Data directory and database* |
| `plaid` | `PLAID_CLIENT_ID` and `PLAID_SECRET` set, `PLAID_ENV` (if set) `production` or `sandbox` (`off` with `HERMES_PLAID=off`) | README, *Integration: Plaid (banks)* |
| `ai` | mode `hermes`: the Hermes executable is found; mode `api`: `ai.base_url`, `ai.model` and `AI_API_KEY` (not needed for the `local` preset) set; mode `off`: `off` | README, *Integration: AI provider (Ask and classification)* |
| `login` | `local`; `proxy` with an allowed identity; `builtin` with its set-up finished | README, *Integration: Access and login* |
| `alpaca` | the key file is there and holds a live key (`off` with `HERMES_ALPACA=off`) | README, *Integration: Alpaca (investments)* |
| `mcp` | the optional `mcp` package is installed (`off` when MCP is switched off) | README, *Integration: MCP server* |
| `carfax` | switched on and `HERMES_CHROME_DEVTOOLS` isn't `off` (the Chrome itself isn't contacted) | README, *Integration: Carfax (car values)* |
| `redfin` | switched on | README, *Integration: Redfin (home values)* |
| `photos` | switched on | README, *Integration: Photos (car and home pictures)* |
