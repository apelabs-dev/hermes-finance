# hermes-finance

hermes-finance is a self-hosted personal-finance dashboard. It pulls your accounts, balances and transactions from
[Plaid](https://plaid.com) into a SQLite database on your own computer. It files spending into categories and tracks
budgets, recurring bills, goals, loans, investments and net worth. For what Plaid can't see (a car, a home, a loan
from a lender that isn't connected) you add an account by hand or import a CSV, OFX or QFX file from your bank.

An AI agent can use the same numbers in three ways:

| Mode | What it is | Changes need |
|---|---|---|
| **Hermes plugin** | Native `finance_*` tools for your Hermes agent, plus scheduled daily and weekly reports | Hermes's approval prompt |
| **MCP server** | `finance mcp` (stdio) or `/mcp` on the dashboard, for Claude Desktop, Cursor, Codex and other MCP clients | Read-only by default; an allowed write is only a pending action you approve in Settings |
| **API key** | The dashboard's Ask panel runs its own tool loop against any OpenAI-compatible endpoint | An approval card in the panel |

All three use one set of tool definitions (`src/hermes_finance/tooldefs.py`). A read tool returns exactly what the
matching `finance <command> --json` prints, so the dashboard, the CLI and the agent always agree.

## Where to go next

- [Getting started](getting-started.md): install, the five-minute Sandbox run, the Hermes plugin.
- [Plaid setup](plaid.md): keys, Sandbox vs Production, connecting banks and the 10-connection limit.
- [AI modes](ai-modes.md): the three modes in detail, and the daily classification loop.
- [Security model](security.md): what stays on your computer, the access modes, and what leaves it.
- [Settings](settings.md): every card on the Settings page, plus the files and environment variables.
- Reference: [CLI](reference/cli.md), [agent tools](reference/tools.md), [settings keys](reference/settings-keys.md).
  These pages are generated from the code.
- [Architecture](architecture.md), [FAQ](faq.md), [Contributing](contributing.md).

!!! note "Personal project"
    hermes-finance is a personal project, shared as is. Support is best-effort.
