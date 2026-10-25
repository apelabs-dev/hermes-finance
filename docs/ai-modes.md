# AI modes

hermes-finance has 16 agent tools, defined once in `src/hermes_finance/tooldefs.py`: eleven reads, `finance_sync`, and
four writes (`finance_set_category`, `finance_add_rule`, `finance_set_cap`, `finance_track_bill`). Three adapters
expose them. The [agent tools reference](reference/tools.md) lists every argument.

| | Hermes plugin | MCP server | API key (Ask panel) |
|---|---|---|---|
| Where you ask | Hermes, wherever you talk to it | Any MCP client (Claude Desktop, Cursor, Codex, ...) | The dashboard's Ask panel |
| Who runs the model | Hermes | The client | The app, against an OpenAI-compatible endpoint you choose |
| A write | Hermes's approval prompt, then it runs | Listed only with writes allowed; a call is saved as a pending action you approve in Settings | An approval card in the panel |
| Your model key | Stays in Hermes | Stays in the client | In the app's secrets file |

The dashboard and the `finance` CLI work without any of them.

## Hermes plugin

Install it with [Getting started](getting-started.md#install-the-hermes-plugin). The plugin registers all 16 tools
(toolset `hermes-finance`), a `pre_tool_call` hook, the `hermes-finance:usage` skill, and the `hermes finance`
command group.

- **Reads** open the database read-only and answer exactly what `finance <command> --json` prints.
- **Writes** go through Hermes's approval gate. The hook describes the change in plain words (which transaction,
  under which category, and what it's filed under now), and the tool runs only after you approve. A call that can't
  be done (an unknown transaction, a rule that matches nothing) is blocked with the reason, without asking.
- **`finance_sync`** runs without asking, except `refresh=true` on a paid Plaid plan, which Plaid bills.
- **Where nobody can answer**, the hook blocks instead of asking. Hermes's `approvals.cron_mode`, `unattended_mode` or
  `single_query_mode` set to `approve` would otherwise turn the request into a yes nobody gave:
    - in a scheduled job: the four writes and `finance_sync` (the job has already synced);
    - on an unattended platform (the API server, webhooks) and in a single-query `hermes chat -q` run: the four writes
      and a billed refresh.
- `hermes finance crons status|plan|apply` sets up the daily and weekly reports, only when you run it:
  [Scheduled reports](plugin-crons.md).
- `hermes finance completion` is the bridge the Ask panel uses when **Settings → AI provider** is **Hermes**: one
  structured completion through Hermes's own model and login. The app never sees Hermes's keys:
  [Ask through Hermes](ask-hermes-bridge.md).

**Trade-off:** you need Hermes, and the plugin installed and enabled in the profile. Approvals work where a person
is there to answer. In scheduled and unattended runs, writes are refused, not queued.

## MCP server

The same tools over the Model Context Protocol, using the official SDK (`uv sync --extra mcp`).

- **stdio:** `finance mcp`, for a client that starts the server itself.
- **HTTP:** `finance serve` also serves streamable HTTP at `/mcp`, behind the same access gate as every other route.

**Read-only by default.** The write tools are listed only after `finance mcp allow-writes on` (or Settings → Connect
other agents → *Allow writes*). Even then a call changes nothing: it's saved as a pending action. Approve or decline it
in **Settings → Connect other agents → Waiting for your OK**. `finance mcp status` lists what's waiting. Approving runs
the same checks again and applies the change exactly as the plugin would after its approval. `finance_sync` is always
listed and runs directly, except a billed refresh on a paid plan, which becomes a pending action too.

The **MCP server** switch on the same card turns both `/mcp` (it answers 404) and `finance mcp` off.

### Client configuration

Settings → Connect other agents builds these snippets for you, with your real paths.

Claude Desktop (`claude_desktop_config.json`) starts the server on this computer over stdio, so no token is needed:

```json
{
  "mcpServers": {
    "hermes-finance": {
      "command": "/path/to/hermes-finance/.venv/bin/finance",
      "args": ["mcp"]
    }
  }
}
```

Cursor (`~/.cursor/mcp.json`) over HTTP:

```json
{
  "mcpServers": {
    "hermes-finance": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {"Authorization": "Bearer <your token>"}
    }
  }
}
```

Codex (`~/.codex/config.toml`) over HTTP, with the token in an environment variable:

```toml
[mcp_servers.hermes-finance]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "HERMES_FINANCE_TOKEN"
```

### Tokens

Tokens come from Settings → Connect other agents → *New token*. A token is `hfm_` plus 32 random bytes, shown once and
stored only as a SHA-256 hash (at most 20 live tokens). It opens `/mcp` and nothing else: never `/api/*`. When you
need one:

| Access mode | `/mcp` from this computer | `/mcp` through your proxy |
|---|---|---|
| This computer only | Open, no token (the `headers` line above isn't needed) | Refused |
| Behind your proxy login | Open, no token | An allowed proxy identity, or a token |
| Built-in login | A token, always | A token, always |

**Trade-off:** there's no approval prompt inside the client. You decide pending changes in the dashboard. In *This
computer only* mode, any program on this computer can reach `/mcp` without a token. It's read-only unless you allow
writes, and an allowed write still waits for you.

## API key: the app runs the loop

**Settings → AI provider → API key** makes the Ask panel use any OpenAI-compatible `/chat/completions` endpoint:

| Preset | Endpoint |
|---|---|
| OpenRouter (the default) | `https://openrouter.ai/api/v1` |
| OpenAI | `https://api.openai.com/v1` |
| Local (Ollama, LM Studio) | `http://127.0.0.1:11434/v1` |
| Custom | the address you type |

An OpenCode address (host `opencode.ai` or a subdomain, e.g. `https://opencode.ai/zen/go/v1`) gets the
`x-opencode-session` header it requires automatically: a random id made once per install, never from your key, and left
out of backups.

You set the model name and paste the key. The app is its own agent harness. It sends the tool schemas, runs every tool
call itself (up to 6 rounds, one 60-second deadline per question), and sends the results back. A write becomes an
approval card in the panel (Approve / Deny). Nothing changes until you approve it. Settings lists Ask's pending cards
too.

- **The key** lives in the secrets file (`~/.config/hermes-finance/secrets.env`, 0600) as `AI_API_KEY`. It is never
  shown again or sent to the browser.
- **Monthly spend cap**, default $5: a soft cap. Requests go out one at a time, and none is sent once this month's
  settled spend has reached the cap, so a month can end over it by at most one request. The cost is the endpoint's own
  figure when it reports one (OpenRouter does). Otherwise it's an estimate from token counts at deliberately high rates
  ($3 / $15 per million input / output tokens).
- **What's sent:** your question, the conversation and the results of the reads the app ran (scrubbed like every tool
  result). It goes to the endpoint you configured. With the *Local* preset and a model server on this computer, nothing
  leaves it.

**Trade-off:** the app is the harness, so its limits are yours (6 rounds, 60 seconds, each answer arrives whole).
Your provider key sits in the app's secrets file on disk. *Test connection* in Settings is not built yet: the button is
disabled.

## Off

**Settings → AI provider → Off**: no Ask button, and every `/api/chat` route answers 404. Rules, Plaid's categories and
your own choices still file transactions.

## Classifying new transactions

The app itself never asks an LLM to classify. Transactions are filed by, in order of precedence:

**your decision > your rule > an AI decision > an AI rule > Plaid's category**

A daily agent fills in the AI part through two commands:

1. `finance ai-queue` saves a run: posted transactions that only Plaid has filed and no AI has reviewed yet, grouped
   by merchant, oldest first (at most 250 groups by default). It prints the run as JSON. With **AI provider → Off**
   it queues nothing.
2. The agent decides each group (`rule`, `set`, `split`, `confirm` or `flag`) and writes a decisions file.
3. `finance ai-apply FILE` validates it and applies it in one SQLite transaction, once per run. A decision below
   **Settings → AI provider → Auto-apply confidence** (default 0.7) becomes a flag for you to review. Your own decisions
   and your rules' rows are never changed.

The Hermes daily job runs this loop (`hermes finance crons`). In API-key mode nothing runs it for you. New
transactions keep Plaid's category and your rules until you run the loop with an agent of your own.

## When the model is unavailable

Every expected failure is one line in the Ask panel, never a made-up answer. A turn that fails withdraws any approval
card it made. The dashboard, the CLI and the tools keep working.

| Situation | What Ask says |
|---|---|
| Hermes mode, no Hermes on this computer | "Ask can't find Hermes on this computer ... Install Hermes, or use an API key in Settings → AI provider." |
| API-key mode with no model, address or key | "Pick a model ..." / "Set the provider's address ..." / "Add your API key in Settings → AI provider first." |
| The key is refused | "The AI provider refused the key (401). Check it in Settings → AI provider." (or 403) |
| Rate limited | "The AI provider says too many requests (429). Try again in a minute." |
| Over 60 seconds | "Ask took too long to answer (over a minute). Try again." |
| More than 6 look-ups | "That took more than 6 look-ups, so Ask stopped. Try a narrower question." |
| This month's spend reached the cap | Ask pauses until next month or a higher cap |
