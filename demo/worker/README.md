# hermes-finance-demo-ai

A small Cloudflare Worker for the public static demo (https://financing.apelabs.dev). The demo's Ask panel has
canned answers and live AI on by default. The page posts every question here (`POST /ai/chat` on the demo's own
origin, or `POST /chat` on the workers.dev URL). A matched preset is answered from its frozen synthetic data;
any other question gets a general summary of the demo's accounts, net worth, budgets, goals, bills and recent
transactions, fetched from the demo site itself. An OpenRouter free model (`openrouter/free`) answers from only
that data, and the worker returns `{"reply", "model"}`.

Quota exhaustion and a transient failure are different answers, so the page can tell them apart:

- the daily caps, or the upstream's 429 / 402, return `429 {"error":"quota","scope":"global"|"visitor",
  "resets_at":"<next UTC midnight ISO>"}`; the page switches live AI off and locks it until then;
- a timeout or any other upstream failure returns `{"error":"busy"}` (503/502/504); the page shows the canned
  answer for that question with the quiet note and leaves live AI on.

**Nobody has deployed it yet.**

## Limits

- 100 live answers per UTC day for the whole demo (`GLOBAL_DAILY_CAP`) and 25 per visitor (`VISITOR_DAILY_CAP`).
  Counted in KV before the model is called. KV is eventually consistent, so the cap is approximate under bursts.
- Body at most 2048 bytes, question at most 300 characters, replies at most 1500 characters.
- 8 s timeout on the model call. Only `https://financing.apelabs.dev` and `localhost` / `127.0.0.1` origins are allowed.
- Optional Turnstile check, off by default (`TURNSTILE_REQUIRED = "true"` plus the `TURNSTILE_SECRET` secret).
- Logs one line per request (path, status, outcome). No question, IP, header or key is ever logged.

## Test

```sh
node --test demo/worker/test/
```

No dependencies and no network: `fetch` and KV are mocked.

## Deploy

```sh
cd demo/worker
npx wrangler kv namespace create DEMO_AI_KV      # copy the printed id into wrangler.toml
npx wrangler secret put OPENROUTER_KEY           # already done by the owner
npx wrangler deploy                              # also attaches the route financing.apelabs.dev/ai/*
```

The demo page only talks to its own origin (its CSP is `connect-src 'self'`), so the live switch works once that route
is attached. Until then the page's `POST /ai/chat` fails and every answer is the canned one with the busy note.

The page sends `{question, preset, variant, shift}`: `preset` is optional (`null` for a general question), `variant`
picks `demo/data/v<variant>/` (the weekday's seeded data set) and `shift` is the visitor-relative date shift, so the
dates the model reads are the dates the page shows.
