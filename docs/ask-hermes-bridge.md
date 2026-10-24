# Ask through Hermes: the completion bridge

With **Settings → AI provider → Hermes**, the Ask panel uses the model and login Hermes already has. The app never sees
Hermes's keys. It runs one command of the hermes-finance plugin for each request:

```sh
hermes finance completion      # JSON request on stdin, one JSON envelope on stdout; started by the app
```

The command makes one `ctx.llm.complete_structured` call with the profile's default model and nothing else: no finance
database, no finance tool, no provider, model or profile override. Everything else stays in the app, as in API-key mode:
the app runs the reads, turns a proposed change into an approval card, and applies it only when you tap Approve.

## Setting it up

1. The hermes-finance plugin must be installed and enabled in the Hermes profile (see plugin-crons.md for the plugin
   settings). A Hermes that doesn't have the plugin, or has an older one without `completion`, answers every question
   with one line saying so. Nothing is answered in its place.
2. The app runs `$HERMES_HOME/hermes-agent/venv/bin/hermes` (`~/.hermes/...` without `HERMES_HOME`). To run another one,
   set `FINANCE_HERMES_BIN` to its absolute path in the app's own environment. There is no Settings field for it, so a
   request can't change which program runs.
3. Hermes needs a working model (`hermes model`). Without one, Ask says so and nothing is sent.

The child gets a fixed command line (`finance completion`) and a small environment: `HOME`, `HERMES_HOME`, `USER`,
`LOGNAME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TMPDIR` and a system `PATH`. No finance key, provider key or app setting is passed.

## One turn

Each request asks the model for exactly one decision, as JSON:

| Decision | What the app does |
|---|---|
| `{"kind": "read", "tool", "arguments"}` | Runs that read itself (one of the `finance_*` read tools) and asks again with the result |
| `{"kind": "propose", "tool", "arguments"}` | Records an approval card (the four writes, and `finance_sync`) and asks again |
| `{"kind": "answer", "text"}` | Shows the answer, with the source lines of the reads the app ran and the turn's cards |

The app checks every decision itself: exactly one of these shapes, no other field, a tool from the list for its kind,
and every keyword used by the tool's advertised argument schema (including nullability, finite numeric bounds, array
items and cardinality), plus size limits. Hermes checks the schema too, but only when its optional `jsonschema`
package is installed, and the app never relies on it. A decision that fails the check ends the turn with one line
("Hermes sent an answer the app couldn't use, so nothing was changed"), and any card that turn recorded is withdrawn.

In a Hermes turn every `finance_sync` is a card, even a plain sync that API-key mode runs without asking. A turn never
syncs by itself, because a sync can't be stopped once it starts.

Cards from a Hermes turn are recorded with origin `ask_hermes`, set by the app, and an opaque binding to the originating
built-in-login session or trusted proxy identity. Another authenticated session or identity cannot fetch or decide one
by guessing its id. Settings lists genuine MCP rows plus only the current authenticated client's Ask rows, and its
pending count matches that list; successful Settings decisions return the same scoped list and count. They are held, so
neither the panel nor Settings can decide them until the turn answers. They can be decided once, with the same checks run
again at Approve.

“This computer only” has no authenticated browser identity. Its current ownerless Ask cards, and ownerless Ask cards
created by older versions, therefore remain shared among permitted local clients for local usability. This is a
shared-machine trust limitation, not authenticated or per-browser isolation. An authenticated built-in-login or proxy
client cannot adopt those unbound legacy cards. Choosing a stricter local-client identity policy remains separate work.

## Limits

- One deadline for the whole turn (60 s) and at most 6 requests. A request starts only with 2 s or more left. The
  child is killed with its process group at the deadline and again once its answer is read. It also ends by itself
  (an alarm set before Hermes starts) if the app goes away.
- The request is at most 512 KiB (the conversation and results at most 200,000 characters). One read result sent back
  is at most 24,000 characters; a bigger one is replaced by a note asking for a narrower question and isn't a source.
- A tool worker's raw stdout is read to 128 KiB at most, before JSON decoding. The Hermes child's stdout is read to
  1 MiB at most; stderr is never read. The plugin accepts at most 128 Ki characters of model text, while the app accepts
  at most 16,000 characters as a decision.
- At most two whole turns are admitted per app; another gets a retry response instead of adding an unbounded waiter or
  worker. Within those turns, one provider request is out at a time per database, as in API-key mode.

## Cost

Each request is reserved against **Monthly spend cap** before the child starts, so at the cap Hermes isn't started at
all. A genuinely reported price is retained, including zero. Otherwise valid reported token counts are estimated at
the app's deliberately high rates; missing or invalid components of failed-call usage are estimated conservatively,
without treating “unavailable” as zero. A failed call with no usable usage is charged its worst case. One Hermes proves
it never sent (no model set up, the plugin missing, a refused request) costs nothing. Hermes drops the output-token
limit for most providers, so a worst case is an estimate, not a bound.

## Privacy

The request carries your question, the conversation and the results of the reads the app ran, scrubbed as every tool
result is. Hermes sends them to its model provider, as API-key mode sends them to yours. The command logs nothing, and
Hermes's own log line for the call names the plugin, provider, model and token count, never the text. The tests check
that nothing of the request is left in the Hermes profile.

## What the tests prove, and what they don't

`tests/test_bridge_runtime.py` runs the installed Hermes itself (its CLI, plugin loader, `PluginContext` and
`complete_structured`) in a throwaway profile under `sandbox-exec`, against a fake provider on a loopback port. That
proves the app side and the plugin/host code path with a synthetic provider. It does not prove your real profile's
login, a real provider, or anything about the gateway. Turning the plugin on in a real profile is a separate step.
