import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import worker, { setClock, clearCache, config, renderDates } from "../src/index.js";

const KEY = "sk-or-v1-TESTKEY-" + "0123456789abcdef".repeat(3).slice(0, 40);
const SITE = "https://financing.apelabs.dev";
const ASK = {
  version: 2,
  presets: {
    budgets: {
      question: "Am I on track with my budgets this month?",
      reply: "CANNED-REFERENCE-REPLY about budgets.",
      context: { budgets: [{ category: "Groceries", limit: 600, spent: 412 }] },
    },
  },
};
const T0 = Date.UTC(2026, 9, 25, 12, 0, 0);

function makeKV() {
  const store = new Map();
  return {
    store,
    async get(k) {
      return store.has(k) ? store.get(k) : null;
    },
    async put(k, v, opts) {
      assert.ok(opts && opts.expirationTtl === 172800, "expirationTtl 172800");
      store.set(k, String(v));
    },
  };
}

function makeEnv(extra = {}) {
  return {
    DEMO_ORIGIN: SITE,
    MODEL: "openrouter/free",
    GLOBAL_DAILY_CAP: "100",
    VISITOR_DAILY_CAP: "10",
    TURNSTILE_REQUIRED: "false",
    OPENROUTER_KEY: KEY,
    DEMO_AI_KV: makeKV(),
    ...extra,
  };
}

const origFetch = globalThis.fetch;
const origLog = console.log;
const origErr = console.error;
const origWarn = console.warn;
let calls;
let logs;
let upstream; // (url, init) => Response | throws
let askHandler;
let siteverify;
let now;

const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...headers } });
const okUpstream = (content = "You have spent 412 of 600 on groceries.", extra = {}) =>
  json({ model: "upstage/solar-pro-3:free", choices: [{ message: { role: "assistant", content } }], ...extra });

beforeEach(() => {
  calls = [];
  logs = [];
  now = T0;
  setClock(() => now);
  clearCache();
  config.upstreamTimeoutMs = 8000;
  config.askTimeoutMs = 5000;
  upstream = () => okUpstream();
  askHandler = () => json(ASK);
  siteverify = () => json({ success: true });
  const grab = (...a) => logs.push(a.map(String).join(" "));
  console.log = grab;
  console.error = grab;
  console.warn = grab;
  globalThis.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push({ url, init });
    if (url.includes("/demo/data/")) return askHandler(url, init);
    if (url.startsWith("https://openrouter.ai/")) return upstream(url, init);
    if (url.startsWith("https://challenges.cloudflare.com/")) return siteverify(url, init);
    throw new Error("unexpected fetch " + url);
  };
});

afterEach(() => {
  globalThis.fetch = origFetch;
  console.log = origLog;
  console.error = origErr;
  console.warn = origWarn;
  setClock(null);
});

const upstreamCalls = () => calls.filter((c) => c.url.startsWith("https://openrouter.ai/"));

function req(body, { method = "POST", path = "/chat", headers = {}, raw } = {}) {
  const h = { "Content-Type": "application/json", "CF-Connecting-IP": "203.0.113.7", ...headers };
  const init = { method, headers: h };
  if (method !== "GET" && method !== "HEAD") init.body = raw !== undefined ? raw : JSON.stringify(body);
  return new Request("https://worker.example" + path, init);
}
const good = (over = {}) => ({ question: "How are my budgets?", preset: "budgets", ...over });
const run = (request, env) => worker.fetch(request, env, {});

test("happy path: bearer, model, prompt content, no leaked request headers", async () => {
  const env = makeEnv();
  const res = await run(
    req(good(), { headers: { Origin: SITE, "X-Secret-Header": "LEAKME-HEADER", "User-Agent": "LEAKME-UA" } }),
    env,
  );
  assert.equal(res.status, 200);
  const out = await res.json();
  assert.equal(out.reply, "You have spent 412 of 600 on groceries.");
  assert.equal(out.model, "upstage/solar-pro-3:free");
  assert.equal(res.headers.get("content-type"), "application/json");
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.equal(res.headers.get("x-content-type-options"), "nosniff");
  const ups = upstreamCalls();
  assert.equal(ups.length, 1);
  assert.equal(ups[0].init.headers.Authorization, `Bearer ${KEY}`);
  assert.equal(ups[0].init.headers["HTTP-Referer"], SITE);
  assert.equal(ups[0].init.headers["X-Title"], "Hermes Finances demo");
  const sent = JSON.parse(ups[0].init.body);
  assert.equal(sent.model, "openrouter/free");
  assert.equal(sent.max_tokens, 300);
  assert.equal(sent.temperature, 0.3);
  assert.equal(sent.messages[0].role, "system");
  assert.match(sent.messages[0].content, /Groceries/);
  assert.match(sent.messages[0].content, /CANNED-REFERENCE-REPLY/);
  assert.equal(sent.messages[1].role, "user");
  assert.equal(sent.messages[1].content, "How are my budgets?");
  const all = ups[0].init.body;
  for (const s of ["LEAKME", "203.0.113.7", "worker.example", "Mozilla"]) assert.ok(!all.includes(s), s);
  assert.equal(logs.length, 1);
  assert.equal(logs[0], "/chat 200 ok");
});

test("both /chat and /ai/chat work", async () => {
  const res = await run(req(good(), { path: "/ai/chat" }), makeEnv());
  assert.equal(res.status, 200);
});

test("the key never appears in any response or console output, in any scenario", async () => {
  const echoErr = () =>
    new Response(JSON.stringify({ error: { message: `bad key ${KEY}` } }), {
      status: 401,
      headers: { "X-Echo": KEY, "Content-Type": "application/json" },
    });
  const scenarios = [
    { name: "ok", setup() {}, expect: 200 },
    { name: "upstream error echoing key", setup: () => (upstream = echoErr), expect: 502 },
    { name: "upstream 429 echoing key", setup: () => (upstream = () => new Response(KEY, { status: 429, headers: { "X-Echo": KEY } })), expect: 429 },
    { name: "200 content has key", setup: () => (upstream = () => okUpstream(`here is ${KEY} ok`)), expect: 502 },
    { name: "200 model has key", setup: () => (upstream = () => okUpstream("fine", { model: KEY })), expect: 200 },
    { name: "fetch throws with key", setup: () => (upstream = () => { throw new Error(`boom ${KEY}`); }), expect: 502 },
    { name: "unknown preset", body: good({ preset: "nope" }), setup() {}, expect: 503 },
    { name: "bad request", body: { question: "" }, setup() {}, expect: 400 },
    { name: "evil origin", headers: { Origin: "https://evil.example" }, setup() {}, expect: 403 },
  ];
  for (const sc of scenarios) {
    beforeEachReset();
    sc.setup();
    const res = await run(req(sc.body || good(), { headers: sc.headers || { Origin: SITE } }), makeEnv());
    assert.equal(res.status, sc.expect, sc.name);
    const text = await res.text();
    const hdrs = [...res.headers].map(([k, v]) => `${k}: ${v}`).join("\n");
    assert.ok(!text.includes(KEY) && !hdrs.includes(KEY), `response leak: ${sc.name}`);
    assert.ok(!logs.join("\n").includes(KEY), `log leak: ${sc.name}`);
    if (res.status !== 200) assert.ok(!text.includes("bad key"), `upstream body passed: ${sc.name}`);
    if (sc.name === "200 model has key") assert.equal(JSON.parse(text).model, "openrouter/free");
  }
});

function beforeEachReset() {
  calls = [];
  logs = [];
  clearCache();
  upstream = () => okUpstream();
  askHandler = () => json(ASK);
}

test("global cap: 101st request of a UTC day is 429 and upstream is not called; next day allowed", async () => {
  const env = makeEnv({ VISITOR_DAILY_CAP: "1000" });
  for (let i = 0; i < 100; i++) {
    const res = await run(req(good(), { headers: { "CF-Connecting-IP": `198.51.100.${i % 250}` } }), env);
    assert.equal(res.status, 200, `request ${i + 1}`);
  }
  assert.equal(upstreamCalls().length, 100);
  const res = await run(req(good(), { headers: { "CF-Connecting-IP": "192.0.2.99" } }), env);
  assert.equal(res.status, 429);
  assert.deepEqual(await res.json(), { error: "busy" });
  assert.equal(upstreamCalls().length, 100);
  assert.ok(logs.at(-1).endsWith("429 capped"));
  assert.ok([...env.DEMO_AI_KV.store.keys()].includes("g:2026-10-25"));
  // advance past 00:00 UTC
  now = Date.UTC(2026, 9, 26, 0, 0, 1);
  const next = await run(req(good(), { headers: { "CF-Connecting-IP": "192.0.2.99" } }), env);
  assert.equal(next.status, 200);
  assert.equal(upstreamCalls().length, 101);
  assert.ok([...env.DEMO_AI_KV.store.keys()].includes("g:2026-10-26"));
});

test("per-visitor cap: 11th request from one IP is 429, another IP still allowed; raw IP not stored", async () => {
  const env = makeEnv();
  for (let i = 0; i < 10; i++) {
    const res = await run(req(good()), env);
    assert.equal(res.status, 200, `request ${i + 1}`);
  }
  const res = await run(req(good()), env);
  assert.equal(res.status, 429);
  assert.equal(upstreamCalls().length, 10);
  const other = await run(req(good(), { headers: { "CF-Connecting-IP": "203.0.113.8" } }), env);
  assert.equal(other.status, 200);
  const keys = [...env.DEMO_AI_KV.store.keys()];
  const vkeys = keys.filter((k) => k.startsWith("v:2026-10-25:"));
  assert.equal(vkeys.length, 2);
  for (const k of vkeys) assert.match(k, /^v:2026-10-25:[0-9a-f]{32}$/);
  assert.ok(!keys.join().includes("203.0.113"));
});

test("a visitor over their cap does not consume the global counter", async () => {
  const env = makeEnv({ VISITOR_DAILY_CAP: "1" });
  await run(req(good()), env);
  await run(req(good()), env);
  assert.equal(env.DEMO_AI_KV.store.get("g:2026-10-25"), "1");
});

test("body limits and validation: no upstream call, nothing counted", async () => {
  const env = makeEnv();
  const cases = [
    ["413 big body", req(null, { raw: JSON.stringify(good({ question: "x".repeat(2100) })) }), 413],
    ["413 content-length", req(null, { raw: "{}", headers: { "Content-Length": "5000" } }), 413],
    ["400 bad json", req(null, { raw: "{not json" }), 400],
    ["400 array", req([1, 2]), 400],
    ["400 missing question", req({ preset: "budgets" }), 400],
    ["400 blank question", req(good({ question: "   " })), 400],
    ["400 long question", req(good({ question: "x".repeat(301) })), 400],
    ["400 question not string", req(good({ question: 5 })), 400],
    ["400 bad preset", req(good({ preset: "Bad Preset!" })), 400],
    ["400 missing preset", req({ question: "hi" }), 400],
    ["400 variant 7", req(good({ variant: 7 })), 400],
    ["400 variant -1", req(good({ variant: -1 })), 400],
    ["400 variant 1.5", req(good({ variant: 1.5 })), 400],
    ["400 variant string", req(good({ variant: "3" })), 400],
    ["400 shift days 32", req(good({ shift: { days: 32, months: 1 } })), 400],
    ["400 shift months 3", req(good({ shift: { days: 0, months: 3 } })), 400],
    ["400 shift not integer", req(good({ shift: { days: 1.5, months: 0 } })), 400],
    ["400 shift missing months", req(good({ shift: { days: 1 } })), 400],
    ["400 wrong content type", req(null, { raw: JSON.stringify(good()), headers: { "Content-Type": "text/plain" } }), 400],
    ["405 GET", req(null, { method: "GET" }), 405],
    ["405 PUT", req(good(), { method: "PUT" }), 405],
    ["404 path", req(good(), { path: "/nope" }), 404],
  ];
  for (const [name, r, status] of cases) {
    const res = await run(r, env);
    assert.equal(res.status, status, name);
    if (status === 400) assert.deepEqual(await res.json(), { error: "bad request" }, name);
  }
  assert.equal(calls.length, 0);
  assert.equal(env.DEMO_AI_KV.store.size, 0);
  const ok = await run(req(good({ question: "x".repeat(300) })), env);
  assert.equal(ok.status, 200);
});

test("body over the limit with no Content-Length header is still 413", async () => {
  const env = makeEnv();
  const big = new TextEncoder().encode(JSON.stringify(good({ question: "x".repeat(3000) })));
  const stream = new ReadableStream({
    start(c) {
      c.enqueue(big.slice(0, 1500));
      c.enqueue(big.slice(1500));
      c.close();
    },
  });
  const r = new Request("https://worker.example/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: stream,
    duplex: "half",
  });
  const res = await run(r, env);
  assert.equal(res.status, 413);
  assert.equal(calls.length, 0);
});

test("fallback triggers all give busy and never leak upstream detail", async () => {
  const cases = [
    ["upstream 429", () => new Response("rate limited UPSTREAMBODY", { status: 429 }), 429],
    ["upstream 500", () => new Response("oops UPSTREAMBODY", { status: 500 }), 502],
    ["upstream 401", () => new Response("UPSTREAMBODY", { status: 401 }), 502],
    ["network error", () => { throw new TypeError("network down UPSTREAMBODY"); }, 502],
    ["invalid json", () => new Response("not json UPSTREAMBODY", { status: 200 }), 502],
    ["no choices", () => json({ choices: [] }), 502],
    ["empty content", () => okUpstream("   \n "), 502],
    ["null content", () => okUpstream(null), 502],
    ["content too long", () => okUpstream("a".repeat(1501)), 502],
  ];
  for (const [name, fn, status] of cases) {
    beforeEachReset();
    upstream = fn;
    const res = await run(req(good()), makeEnv());
    assert.equal(res.status, status, name);
    const text = await res.text();
    assert.deepEqual(JSON.parse(text), { error: "busy" }, name);
    assert.ok(!logs.join("\n").includes("UPSTREAMBODY"), name);
  }
});

test("upstream timeout gives 504 busy (fast, via short injected timeout)", async () => {
  config.upstreamTimeoutMs = 30;
  upstream = (url, init) =>
    new Promise((_, reject) => {
      init.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
    });
  const res = await run(req(good()), makeEnv());
  assert.equal(res.status, 504);
  assert.deepEqual(await res.json(), { error: "busy" });
});

test("content is sanitised: control characters stripped, newlines kept, trimmed", async () => {
  upstream = () => okUpstream("  line one\u0000\u0007\nline two\u001b[0m\r  ");
  const res = await run(req(good()), makeEnv());
  assert.equal(res.status, 200);
  assert.equal((await res.json()).reply, "line one\nline two[0m");
});

test("model field falls back to env.MODEL when missing or odd", async () => {
  upstream = () => okUpstream("hello", { model: "bad model with spaces" });
  let res = await run(req(good()), makeEnv());
  assert.equal((await res.json()).model, "openrouter/free");
  upstream = () => json({ choices: [{ message: { content: "hello" } }] });
  res = await run(req(good()), makeEnv({ DEMO_AI_KV: makeKV() }));
  assert.equal((await res.json()).model, "openrouter/free");
});

test("unknown preset, failing ask.json and missing key give 503 busy", async () => {
  let res = await run(req(good({ preset: "unknown_one" })), makeEnv());
  assert.equal(res.status, 503);
  assert.deepEqual(await res.json(), { error: "busy" });
  assert.equal(upstreamCalls().length, 0);

  for (const h of [() => new Response("nope", { status: 404 }), () => { throw new Error("down"); }, () => new Response("{bad", { status: 200 }), () => json({ presets: 5 })]) {
    beforeEachReset();
    askHandler = h;
    const env = makeEnv();
    res = await run(req(good()), env);
    assert.equal(res.status, 503);
    assert.deepEqual(await res.json(), { error: "busy" });
    assert.equal(upstreamCalls().length, 0);
    assert.equal(env.DEMO_AI_KV.store.size, 0);
  }

  beforeEachReset();
  res = await run(req(good()), makeEnv({ OPENROUTER_KEY: undefined }));
  assert.equal(res.status, 503);
  assert.deepEqual(await res.json(), { error: "busy" });
  assert.equal(upstreamCalls().length, 0);
});

test("ask.json is fetched with a timeout and cached for 10 minutes", async () => {
  const env = makeEnv();
  await run(req(good()), env);
  await run(req(good()), env);
  const asks = () => calls.filter((c) => c.url.includes("/demo/data/"));
  assert.equal(asks().length, 1);
  assert.equal(asks()[0].url, `${SITE}/demo/data/ask.json`);
  assert.ok(asks()[0].init.signal);
  now += 11 * 60 * 1000;
  await run(req(good()), env);
  assert.equal(asks().length, 2);
});

test("ask.json fetch that hangs is aborted and gives 503", async () => {
  config.askTimeoutMs = 30;
  askHandler = (url, init) =>
    new Promise((_, reject) => {
      init.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
    });
  const res = await run(req(good()), makeEnv());
  assert.equal(res.status, 503);
});

test("CORS: allowed origins get ACAO on success and errors, preflight is 204", async () => {
  for (const o of [SITE, "http://localhost:8799", "http://localhost", "http://127.0.0.1:5173", "http://127.0.0.1"]) {
    for (const r of [req(good(), { headers: { Origin: o } }), req({ question: "" }, { headers: { Origin: o } })]) {
      const res = await run(r, makeEnv());
      assert.equal(res.headers.get("access-control-allow-origin"), o);
      assert.equal(res.headers.get("vary"), "Origin");
      assert.equal(res.headers.get("access-control-allow-methods"), "POST, OPTIONS");
      assert.equal(res.headers.get("access-control-allow-headers"), "Content-Type");
      assert.equal(res.headers.get("access-control-max-age"), "600");
    }
    const pre = await run(
      new Request("https://worker.example/ai/chat", {
        method: "OPTIONS",
        headers: { Origin: o, "Access-Control-Request-Method": "POST" },
      }),
      makeEnv(),
    );
    assert.equal(pre.status, 204);
    assert.equal(pre.headers.get("access-control-allow-origin"), o);
    assert.equal(pre.headers.get("access-control-max-age"), "600");
  }
  assert.equal(upstreamCalls().length, 5);
});

test("CORS: no Origin header is allowed without ACAO", async () => {
  const res = await run(req(good()), makeEnv());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("access-control-allow-origin"), null);
});

test("CORS: forbidden origins get 403, no ACAO, no upstream call, nothing counted", async () => {
  const env = makeEnv();
  for (const o of ["https://evil.example", "https://financing.apelabs.dev.evil.example", "http://financing.apelabs.dev", "http://localhost.evil.example", "http://localhost:80/x", "null"]) {
    const res = await run(req(good(), { headers: { Origin: o } }), env);
    assert.equal(res.status, 403, o);
    assert.deepEqual(await res.json(), { error: "forbidden origin" });
    assert.equal(res.headers.get("access-control-allow-origin"), null);
    const pre = await run(new Request("https://worker.example/chat", { method: "OPTIONS", headers: { Origin: o } }), env);
    assert.equal(pre.status, 403);
  }
  assert.equal(calls.length, 0);
  assert.equal(env.DEMO_AI_KV.store.size, 0);
});

test("Turnstile: off by default", async () => {
  const res = await run(req(good()), makeEnv());
  assert.equal(res.status, 200);
  assert.equal(calls.filter((c) => c.url.includes("challenges.cloudflare.com")).length, 0);
});

test("Turnstile: on requires a token and a successful siteverify", async () => {
  const env = () => makeEnv({ TURNSTILE_REQUIRED: "true", TURNSTILE_SECRET: "ts-secret", DEMO_AI_KV: makeKV() });
  const verifies = () => calls.filter((c) => c.url.includes("challenges.cloudflare.com"));

  let res = await run(req(good()), env());
  assert.equal(res.status, 403);
  assert.deepEqual(await res.json(), { error: "verification failed" });
  assert.equal(verifies().length, 0);

  siteverify = () => json({ success: false });
  const e2 = env();
  res = await run(req(good({ turnstile: "tok" })), e2);
  assert.equal(res.status, 403);
  assert.equal(upstreamCalls().length, 0);
  assert.equal(e2.DEMO_AI_KV.store.size, 0);

  siteverify = () => json({ success: true });
  res = await run(req(good({ turnstile: "tok" })), env());
  assert.equal(res.status, 200);
  const v = verifies().at(-1);
  assert.equal(v.url, "https://challenges.cloudflare.com/turnstile/v0/siteverify");
  const form = new URLSearchParams(v.init.body);
  assert.equal(form.get("secret"), "ts-secret");
  assert.equal(form.get("response"), "tok");
  assert.equal(form.get("remoteip"), "203.0.113.7");
});

test("variant 3 fetches /demo/data/v3/ask.json, absent variant uses the base file", async () => {
  const res = await run(req(good({ variant: 3 })), makeEnv());
  assert.equal(res.status, 200);
  assert.ok(calls.some((c) => c.url === `${SITE}/demo/data/v3/ask.json`));
  assert.ok(!calls.some((c) => c.url === `${SITE}/demo/data/ask.json`));
  await run(req(good({ variant: null })), makeEnv());
  assert.ok(calls.some((c) => c.url === `${SITE}/demo/data/ask.json`));
});

test("logs: one line per request, only path, status and outcome", async () => {
  await run(req(good({ question: "SECRETQUESTION" }), { headers: { Origin: SITE } }), makeEnv());
  await run(req({ question: "" }), makeEnv());
  await run(req(good(), { path: "/zzz-secret-path" }), makeEnv());
  assert.deepEqual(logs, ["/chat 200 ok", "/chat 400 bad", "other 404 bad"]);
});

test("date tokens reach the model as the page's plain dates, moved by the visitor's shift", async () => {
  assert.equal(renderDates("{{d:2026-10-26}} / {{D:2026-10-26}} / {{m:2026-09}} / {{M:2026-12}} / {{x:2026-01}}"),
    "Oct 26 / Oct 26, 2026 / September 2026 / Dec 2026 / {{x:2026-01}}");
  assert.equal(renderDates("{{D:2026-12-30}} {{m:2026-12}}", { days: 3, months: 1 }), "Jan 2, 2027 January 2027");
  const ask = { presets: { budgets: { question: "q", reply: "Due {{d:2026-10-26}}.",
    context: { answer: "In {{m:2026-10}}", data: { due: "{{D:2026-10-30}}" } } } } };
  askHandler = () => json(ask);
  const res = await run(req(good({ shift: { days: 2, months: 1 } })), makeEnv());
  assert.equal(res.status, 200);
  const system = JSON.parse(upstreamCalls()[0].init.body).messages[0].content;
  assert.ok(!system.includes("{{"), system);
  for (const s of ["Due Oct 28.", "In November 2026", "Nov 1, 2026"]) assert.ok(system.includes(s), s);
});
