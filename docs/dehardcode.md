# The privacy scanner

Personal data (a name, a login, an email, an address, a car, a bank) is data: it lives in the database or the
settings, never as a string in a file that ships. One scanner, `scripts/dehardcode.py` (Python 3.9+, standard library
only), enforces that with two checks:

- **The word check**: no private word in the app (`src/`, `static/` included), the Hermes plugin (`hermes_plugin/`)
  or the tests (`tests/`, their fixtures and generators included).
- **The release scan**: nothing private in any file that ships, binaries included: a private word, a home-directory
  path, a credential, a real email address or a long id.

Four things run it, so they can't drift:

| Caller | What it runs |
|---|---|
| `tests/test_dehardcode_task18.py` | the word check on the working tree (always on, in every tier of `scripts/check.py`) |
| `tests/test_release_scan.py` | the release scan's own tests, and the release scan of every tracked file that ships |
| `scripts/check.py ci` (CI) | the release scan of every tracked file (`--release-tracked`), then the no-browser tests |
| `scripts/hooks/pre-commit` | the word check, then the release scan, on what a commit will hold |

## Where the private words come from

Never from a tracked file. Every source is merged:

1. **Your own account**: your login, your home directory's name and the words of your full name (GECOS), 4+ letters.
   A service account's name (`root`, `runner`, `ubuntu`, ...: `SERVICE_LOGINS`) is not a private word.
2. **`HERMES_FINANCE_TEST_PRIVATE`**: a comma-separated environment variable, e.g. in your shell profile
   (`export HERMES_FINANCE_TEST_PRIVATE="word,another word"`) or a CI secret. `tests/test_plugin_crons.py` reads the
   same variable.
3. **A word file**: the file `HERMES_FINANCE_PRIVATE_FILE` names, else `.private-words` at the repo root (listed in
   `.gitignore`). One word or phrase per line; `#` starts a comment.

A private word matches case-insensitively as a whole word, not inside a longer word or number.

## The word check

```sh
python3 scripts/dehardcode.py            # every text file under src/ and hermes_plugin/
python3 scripts/dehardcode.py PATH...    # these files
python3 scripts/dehardcode.py --staged   # the staged blobs (`git show :path`) under src/ and hermes_plugin/
```

It prints `path:line: token` per hit and exits 1 when there is one (0 when there's none, 2 on bad usage). Exempt
paths would be listed in `EXEMPT`, each with its reason; there are none.

## The release scan

```sh
python3 scripts/dehardcode.py --release DIR        # every file under DIR (.git/ aside), e.g. an exported tree
python3 scripts/dehardcode.py --release-tracked    # this checkout's tracked files that ship
python3 scripts/dehardcode.py --release-staged     # the staged blobs that ship (what the hook runs)
```

Each hit is printed as `path:line: rule`, **never the text it found**, because the scan runs in public CI logs. Exit 1
on a hit, 0 when clean, 2 on bad usage. The rules:

| Rule | Finds |
|---|---|
| `private-word` | a private word, as above |
| `personal-path` | `/home/<name>/`, `/Volumes/<name>/`, the same under macOS's `Users` folder, or `C:\Users\<name>\`, unless the name is a placeholder (`you`, `<user>`, `$USER`, ...) |
| `token:<kind>` | a credential's shape (Plaid production/development, Alpaca, OpenAI, Anthropic, GitHub, Slack, AWS, a private key), or a long high-entropy value assigned to a `*_SECRET` / `*_KEY` / `*_TOKEN` / password name |
| `email` | an email address outside the reserved and placeholder domains (`example.com`, `.test`, `.invalid`, ...) |
| `long-id` | a 17–20 digit number (an account or channel id) that isn't obviously made up |
| `hex-id` | a standalone 12-hex id (a job id) that isn't obviously made up |

What it lets through, each a committed, reasoned decision in `scripts/dehardcode.py`:

- `ALLOWED_PUBLIC`: exact public strings masked before matching, each with its reason. It's empty: no shipped file
  names the repository's own URL, so the owner's login is a private word everywhere.
- `RELEASE_EXEMPT`: a path pattern, a rule, a regex the hit must match, and the reason (for example the tests'
  synthetic Plaid tokens). `tests/test_release_scan.py` checks that each one covers its own case and nothing wider.

**What ships** is decided in the development repository by its public export's allowlist; in a released tree every
tracked file ships and is scanned.

## Test reports

CI runs the tests with the private words in its environment, and its log is public. A failing test's report shows
its assertion's values, its frames' arguments and its captured output, any of which can hold the word a privacy test
found. So `tests/redact.py`, a pytest plugin that `tests/conftest.py` loads, replaces every private word in every
report with `<private>` before pytest prints it (`tests/test_redact.py`).

## Adding your own words

Put them in `.private-words` (or a file named by `HERMES_FINANCE_PRIVATE_FILE`) or in `HERMES_FINANCE_TEST_PRIVATE`,
then run `python3 scripts/dehardcode.py` and `python3 scripts/dehardcode.py --release-tracked`.

## The hook

Install it once per clone with `sh scripts/install-hooks.sh`. It sets `core.hooksPath` to `scripts/hooks`, which
applies to every worktree of the clone (undo: `git config --unset core.hooksPath`). It runs the scanner with the
repo's `.venv/bin/python` if there is one, else `python3` (`HERMES_FINANCE_HOOK_PYTHON` overrides).

A blocked commit names the path and line: the word check also prints the word (on your own terminal), the release
scan only the rule. For a false positive, fix the text or add a reasoned `RELEASE_EXEMPT` entry with a test. `git
commit --no-verify` skips the hook, but the same scans run in `scripts/check.py` and CI, so a real hit can't slip
through.
