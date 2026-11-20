# Captured CLI help text

Real output, captured on 2026-11-15 from a local install. The stub in `tests/cli_stub.py`
serves these files so the capability checks read the same text the real CLIs print.

| File | Command | Version |
|---|---|---|
| `claude-help.txt` | `claude --help` | `2.1.286 (Claude Code)` |
| `codex-help.txt` | `codex --help` (top level) | `codex-cli 0.162.1` |
| `codex-exec-help.txt` | `codex exec --help` | `codex-cli 0.162.1` |
| `codex-features.txt` | `codex features list` | `codex-cli 0.162.1` |

`codex --help` lists the subcommands but not the `exec` flags: `--ignore-user-config` and `--json`
are missing from it on 0.162.1, so the capability check reads `codex exec --help`, not the top-level
help. `codex features list` still names every feature the lockdown turns off.

The text is verbatim apart from the program name in usage lines, which the captures already show as
`claude` / `codex` (no path); no path, login or token is in these files.
