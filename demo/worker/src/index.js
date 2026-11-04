// Hermes Finances demo: live-AI worker.
//
// The public static demo has canned Ask answers ("presets"). With live AI on, the page posts every
// question here; a matched preset is answered from its frozen synthetic data, anything else from a
// general summary of the demo's accounts, budgets, goals, bills and recent transactions. The summary is
// fetched by this worker from the demo origin, never taken from the client.
//
// Quota exhaustion (our daily caps, or the upstream's 429/402) is distinct from a transient failure: the
// page locks the switch until the reset time for the former and falls back quietly for the latter. Every
// failure body is bare {"error": ...} with no upstream detail.

const MAX_BODY_BYTES = 2048;
const MAX_QUESTION = 300;
const MAX_REPLY = 1500;
const MAX_CONTEXT_CHARS = 6000;
const CACHE_MS = 10 * 60 * 1000;
const KV_TTL = 172800;
const PRESET_RE = /^[a-z0-9_-]{1,40}$/;
const ORIGIN_RE = /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?$/;
const SITE_ORIGIN = "https://financing.apelabs.dev";
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
const PATHS = new Set(["/chat", "/ai/chat"]);

// Mutable so tests can shorten the timeouts.
export const config = { upstreamTimeoutMs: 8000, askTimeoutMs: 5000, turnstileTimeoutMs: 5000 };

let clock = () => Date.now();
export function setClock(fn) {
  clock = fn || (() => Date.now());
}

const askCache = new Map(); // url -> { at, data }
export function clearCache() {
  askCache.clear();
}

export function isAllowedOrigin(origin) {
  return origin === SITE_ORIGIN || ORIGIN_RE.test(origin);
}

function baseHeaders(origin) {
  const h = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  };
  if (origin && isAllowedOrigin(origin)) {
    h["Access-Control-Allow-Origin"] = origin;
    h["Vary"] = "Origin";
    h["Access-Control-Allow-Methods"] = "POST, OPTIONS";
    h["Access-Control-Allow-Headers"] = "Content-Type";
    h["Access-Control-Max-Age"] = "600";
  }
  return h;
}

// One log line per request: path, status, outcome word. Nothing else, ever.
function respond(origin, path, status, outcome, body) {
  const known = PATHS.has(path) ? path : "other";
  console.log(`${known} ${status} ${outcome}`);
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: baseHeaders(origin),
  });
}

const BUSY = { error: "busy" };

export function utcDay(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

export async function visitorKey(ip, day) {
  const data = new TextEncoder().encode(`${ip || "unknown"}|${day}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `v:${day}:${hex.slice(0, 32)}`;
}

async function readBodyLimited(request) {
  const declared = request.headers.get("Content-Length");
  if (declared !== null && Number(declared) > MAX_BODY_BYTES) return null;
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_BODY_BYTES) {
      try {
        await reader.cancel();
      } catch {
        // ignore
      }
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.byteLength;
  }
  return out;
}

// Returns the validated fields or null.
export function validateBody(raw) {
  let obj;
  try {
    obj = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
  } catch {
    return null;
  }
  if (obj === null || typeof obj !== "object" || Array.isArray(obj)) return null;
  if (typeof obj.question !== "string") return null;
  const question = obj.question.trim();
  if (question.length < 1 || question.length > MAX_QUESTION) return null;
  // A preset is optional: with none, the question is answered from the general summary.
  let preset = null;
  if (obj.preset !== undefined && obj.preset !== null) {
    if (typeof obj.preset !== "string" || !PRESET_RE.test(obj.preset)) return null;
    preset = obj.preset;
  }
  let variant = null;
  if (obj.variant !== undefined && obj.variant !== null) {
    if (!Number.isInteger(obj.variant) || obj.variant < 0 || obj.variant > 6) return null;
    variant = obj.variant;
  }
  // The page's visitor-relative date shift (demo/shim.js SHIFT), so the model's dates match the page's.
  let shift = { days: 0, months: 0 };
  if (obj.shift !== undefined && obj.shift !== null) {
    const { days, months } = obj.shift;
    if (!Number.isInteger(days) || Math.abs(days) > 31 || !Number.isInteger(months) || Math.abs(months) > 2) return null;
    shift = { days, months };
  }
  let turnstile = null;
  if (obj.turnstile !== undefined && obj.turnstile !== null) {
    if (typeof obj.turnstile !== "string" || obj.turnstile.length > 2048) return null;
    turnstile = obj.turnstile;
  }
  return { question, preset, variant, shift, turnstile };
}

async function timedFetch(url, init, ms) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const res = await fetch(url, { ...init, signal: ctl.signal });
    const text = await res.text();
    return { status: res.status, text };
  } finally {
    clearTimeout(timer);
  }
}

async function loadPreset(env, variant, presetId) {
  const base = String(env.DEMO_ORIGIN || SITE_ORIGIN).replace(/\/+$/, "");
  const url = variant === null ? `${base}/demo/data/ask.json` : `${base}/demo/data/v${variant}/ask.json`;
  const now = clock();
  let entry = askCache.get(url);
  if (!entry || now - entry.at > CACHE_MS) {
    try {
      const r = await timedFetch(url, { method: "GET", headers: { Accept: "application/json" } }, config.askTimeoutMs);
      if (r.status !== 200) return null;
      const data = JSON.parse(r.text);
      if (!data || typeof data !== "object" || !data.presets || typeof data.presets !== "object") return null;
      entry = { at: now, data };
      askCache.set(url, entry);
    } catch {
      return null;
    }
  }
  const presets = entry.data.presets;
  if (!Object.hasOwn(presets, presetId)) return null;
  const p = presets[presetId];
  if (!p || typeof p !== "object") return null;
  return p;
}

async function loadJson(env, url) {
  const now = clock();
  let entry = askCache.get(url);
  if (!entry || now - entry.at > CACHE_MS) {
    try {
      const r = await timedFetch(url, { method: "GET", headers: { Accept: "application/json" } }, config.askTimeoutMs);
      if (r.status !== 200) return null;
      const data = JSON.parse(r.text);
      if (!data || typeof data !== "object") return null;
      entry = { at: now, data };
      askCache.set(url, entry);
    } catch {
      return null;
    }
  }
  return entry.data;
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];

function isoDay(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

function isoMonth(value) {
  return typeof value === "string" && /^\d{4}-\d{2}$/.test(value) ? value : null;
}

const usd = (value) => {
  const n = Number(value);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : null;
};

// A compact read-only summary of the demo's synthetic data, for a question that names no preset. Dates go in
// as tokens and out through renderDates, so the model sees the visitor's dates.
export function buildGeneralSummary(core, bundle, shift = { days: 0, months: 0 }) {
  const meta = core && core.meta && typeof core.meta === "object" ? core.meta : {};
  const pages = core && core.core && typeof core.core === "object" ? core.core : {};
  const lines = [];
  const today = isoDay(meta.today);
  const month = isoMonth(meta.this_month);
  if (today || month) {
    lines.push(`Demo data as of ${today ? `{{D:${today}}}` : "today"}${month ? `, month {{m:${month}}}` : ""}.`);
  }
  const overview = bundle && bundle.overview && typeof bundle.overview === "object" ? bundle.overview : {};
  const summary = overview.summary && typeof overview.summary === "object" ? overview.summary : {};
  if (usd(summary.mtd)) {
    lines.push(`This month so far: ${usd(summary.mtd)} spent${usd(summary.income) ? `, ${usd(summary.income)} income` : ""}` +
      `${Number.isFinite(summary.days_left) ? `; ${summary.days_left} days left` : ""}.`);
  }
  const accounts = pages["/api/accounts"] && typeof pages["/api/accounts"] === "object" ? pages["/api/accounts"] : {};
  const nw = accounts.net_worth;
  if (nw && usd(nw.net)) {
    lines.push(`Net worth: ${usd(nw.net)} (assets ${usd(nw.assets)}, debts ${usd(nw.debts)}).`);
  }
  const accountLines = [];
  for (const item of Array.isArray(accounts.items) ? accounts.items : []) {
    for (const a of item && Array.isArray(item.accounts) ? item.accounts : []) {
      const balance = usd(a && a.current_balance);
      if (!a || !a.name || !balance) continue;
      const kind = [a.type, a.subtype].filter(Boolean).join("/");
      accountLines.push(`${a.name}${kind ? ` (${kind})` : ""} ${balance}`);
    }
  }
  if (accountLines.length) lines.push(`Accounts: ${accountLines.slice(0, 12).join("; ")}.`);
  const budgets = bundle && bundle.budgets && typeof bundle.budgets === "object" ? bundle.budgets : {};
  const cats = Array.isArray(budgets.categories) ? budgets.categories :
    Array.isArray(overview.budgets) ? overview.budgets : [];
  const spent = cats.filter((c) => c && Number(c.spend) > 0).sort((a, b) => Number(b.spend) - Number(a.spend)).slice(0, 10);
  if (spent.length) {
    lines.push(`Spending this month by category: ${spent.map((c) =>
      `${c.category} ${usd(c.spend)}${usd(c.cap) ? ` of ${usd(c.cap)}${Number.isFinite(c.pct) ? ` (${Math.round(c.pct)}%)` : ""}` : ""}`
    ).join("; ")}.`);
  }
  const goals = pages["/api/goals"] && Array.isArray(pages["/api/goals"].goals) ? pages["/api/goals"].goals : [];
  const goalLines = goals.filter((g) => g && g.name && usd(g.target)).map((g) =>
    `${g.name}: ${usd(g.progress)} of ${usd(g.target)}${Number.isFinite(g.pct) ? ` (${Math.round(g.pct)}%)` : ""}`).slice(0, 8);
  if (goalLines.length) lines.push(`Savings goals: ${goalLines.join("; ")}.`);
  const rec = pages["/api/recurring"] && Array.isArray(pages["/api/recurring"].items) ? pages["/api/recurring"].items : [];
  const due = rec.filter((r) => r && Number.isFinite(r.days_until) && r.days_until >= 0).sort((a, b) => a.days_until - b.days_until).slice(0, 10);
  const dueLines = due.map((r) => {
    const day = isoDay(r.next_expected);
    return `${r.merchant}: ${usd(r.amount) || r.amount_label || "amount unknown"} ${r.cadence || "recurring"}` +
      `${day ? `, next {{d:${day}}}` : ""}${Number.isFinite(r.days_until) ? ` (${r.days_until} days)` : ""}`;
  });
  if (dueLines.length) lines.push(`Upcoming bills and subscriptions: ${dueLines.join("; ")}.`);
  const inv = overview.investments;
  if (inv && usd(inv.total)) {
    lines.push(`Investments: ${usd(inv.total)}${inv.change_7d && usd(inv.change_7d.amount) ? `, ${usd(inv.change_7d.amount)} in 7 days` : ""}.`);
  }
  const debt = overview.debt;
  if (debt && usd(debt.total)) lines.push(`Debt: ${usd(debt.total)}${Number.isFinite(debt.cards_util) ? `, cards at ${debt.cards_util}% of limit` : ""}.`);
  const recent = Array.isArray(overview.recent) ? overview.recent : [];
  const recentLines = recent.filter((r) => r && usd(r.amount)).slice(0, 8).map((r) => {
    const day = isoDay(r.date);
    return `${day ? `{{d:${day}}}` : ""} ${r.merchant_name || r.name || "transaction"} ${usd(Math.abs(r.amount))} ${r.category || ""}`.trim();
  });
  if (recentLines.length) lines.push(`Recent transactions: ${recentLines.join("; ")}.`);
  const text = renderDates(lines.join("\n"), shift);
  return text.length > MAX_CONTEXT_CHARS ? text.slice(0, MAX_CONTEXT_CHARS) : text;
}

async function loadGeneralSummary(env, variant, shift) {
  const base = String(env.DEMO_ORIGIN || SITE_ORIGIN).replace(/\/+$/, "");
  const dir = variant === null ? `${base}/demo/data` : `${base}/demo/data/v${variant}`;
  const core = await loadJson(env, `${dir}/core.json`);
  if (!core || !core.meta || !isoMonth(core.meta.this_month)) return null;
  const bundle = await loadJson(env, `${dir}/m/${core.meta.this_month}.json`);
  if (!bundle) return null;
  return buildGeneralSummary(core, bundle, shift);
}

// The one date rule (scripts/demo_ask.py shift_day, demo/shim.js shiftDate): a date never leaves
// month(date) + months. Plain day arithmetic is kept when it lands in that month; a date it would carry out
// of the month is clamped to the month's first or last day. So a {{d:}} token and the {{m:}} label of the
// same answer always name the same month, and dates keep their order.
export function shiftDate(v, days, months) {
  const plain = new Date(Date.parse(`${v}T12:00:00Z`) + days * 864e5).toISOString().slice(0, 10);
  const k = Number(v.slice(0, 4)) * 12 + Number(v.slice(5, 7)) - 1 + months;
  const target = `${String(Math.floor(k / 12)).padStart(4, "0")}-${String(k % 12 + 1).padStart(2, "0")}`;
  if (plain.slice(0, 7) === target) return plain;
  if (plain.slice(0, 7) < target) return `${target}-01`;
  const last = new Date(Date.UTC(Math.floor(k / 12), k % 12 + 1, 0)).getUTCDate();
  return `${target}-${String(last).padStart(2, "0")}`;
}

// The presets carry dates as tokens the page renders (scripts/demo_ask.py render, demo/shim.js render):
// {{d:YYYY-MM-DD}} "Oct 26", {{D:..}} "Oct 26, 2026", {{m:YYYY-MM}} "October 2026", {{M:..}} "Oct 2026",
// moved by the visitor's shift. The model sees the same plain dates the page shows.
export function renderDates(text, shift = { days: 0, months: 0 }) {
  return String(text).replace(/\{\{([dDmM]):(\d{4}-\d{2}(?:-\d{2})?)\}\}/g, (_, kind, v) => {
    if (kind === "d" || kind === "D") {
      if (v.length !== 10) return v;
      const x = shiftDate(v, shift.days, shift.months);
      return `${MONTHS[Number(x.slice(5, 7)) - 1].slice(0, 3)} ${Number(x.slice(8))}${kind === "D" ? `, ${x.slice(0, 4)}` : ""}`;
    }
    const k = Number(v.slice(0, 4)) * 12 + Number(v.slice(5, 7)) - 1 + shift.months;
    const name = MONTHS[k % 12];
    return `${kind === "m" ? name : name.slice(0, 3)} ${Math.floor(k / 12)}`;
  });
}

export function buildMessages(preset, question, shift) {
  let context = renderDates(JSON.stringify(preset.context === undefined ? null : preset.context), shift);
  if (context.length > MAX_CONTEXT_CHARS) context = context.slice(0, MAX_CONTEXT_CHARS);
  const reference = typeof preset.reply === "string" ? renderDates(preset.reply, shift) : "";
  const system = [
    "You are the demo assistant of Hermes Finances, a self-hosted personal finance dashboard.",
    "All data is synthetic demo data.",
    "Answer ONLY from the JSON below. If the data does not cover the question, say so briefly.",
    "Plain text only, at most 120 words, no tables, no links, no markdown headings.",
    "",
    "Data:",
    context,
    "",
    "Reference answer to the preset question:",
    reference,
  ].join("\n");
  return [
    { role: "system", content: system },
    { role: "user", content: question },
  ];
}

export function buildGeneralMessages(summary, question) {
  const context = String(summary === undefined || summary === null ? "" : summary).slice(0, MAX_CONTEXT_CHARS);
  const system = [
    "You are the demo assistant of Hermes Finances, a self-hosted personal finance dashboard.",
    "All data is synthetic demo data.",
    "Answer ONLY from the JSON summary below. If the summary does not cover the question, say so plainly and briefly.",
    "Do not give advice beyond what the data shows.",
    "Plain text only, at most 120 words, no tables, no links, no markdown headings.",
    "",
    "Data summary:",
    context,
  ].join("\n");
  return [
    { role: "system", content: system },
    { role: "user", content: question },
  ];
}

export function cleanReply(content, key) {
  if (typeof content !== "string") return null;
  // eslint-disable-next-line no-control-regex
  let text = content.replace(/[\u0000-\u0009\u000B-\u001F\u007F]/g, "");
  // Some free models behind openrouter/free put their reasoning in the answer: drop a <think> block, and refuse a
  // reply that is the model working through our own instructions (it would show the visitor the prompt).
  text = text.replace(/<think>[\s\S]*?<\/think>/gi, "").replace(/^[\s\S]*<\/think>/i, "").trim();
  if (LEAK_RE.test(text)) return null;
  if (text.length < 1 || text.length > MAX_REPLY) return null;
  if (key && text.includes(key)) return null;
  return text;
}

// Phrases from our system prompts, or a model's own step headers, that only appear when reasoning leaked into the reply.
export const LEAK_RE = /answer only from|json summary provided|data summary:|reference answer to the preset|constraint \d|analy[sz]e the (request|data|question)|<think>/i;

async function verifyTurnstile(env, token, ip) {
  if (!token || !env.TURNSTILE_SECRET) return false;
  const form = new URLSearchParams();
  form.set("secret", env.TURNSTILE_SECRET);
  form.set("response", token);
  if (ip) form.set("remoteip", ip);
  try {
    const r = await timedFetch(
      TURNSTILE_URL,
      { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: form.toString() },
      config.turnstileTimeoutMs,
    );
    if (r.status !== 200) return false;
    return JSON.parse(r.text).success === true;
  } catch {
    return false;
  }
}

function capOf(value, fallback) {
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

// Daily counters in KV. KV is eventually consistent, so the cap is approximate under bursts
// (acceptable for a demo; a Durable Object would make it exact).
// Returns {allowed, scope}: the request may go upstream (and has been counted), or the cap that stopped it.
async function takeQuota(env, ip) {
  const kv = env.DEMO_AI_KV;
  if (!kv) return { allowed: false, scope: null, unavailable: true };   // a config gap is not the visitor's quota
  const day = utcDay(clock());
  const gKey = `g:${day}`;
  const vKey = await visitorKey(ip, day);
  const [g, v] = await Promise.all([kv.get(gKey), kv.get(vKey)]);
  const gCount = Number.parseInt(g || "0", 10) || 0;
  const vCount = Number.parseInt(v || "0", 10) || 0;
  if (gCount >= capOf(env.GLOBAL_DAILY_CAP, 100)) return { allowed: false, scope: "global" };
  if (vCount >= capOf(env.VISITOR_DAILY_CAP, 25)) return { allowed: false, scope: "visitor" };
  await Promise.all([
    kv.put(gKey, String(gCount + 1), { expirationTtl: KV_TTL }),
    kv.put(vKey, String(vCount + 1), { expirationTtl: KV_TTL }),
  ]);
  return { allowed: true, scope: null };
}

export function quotaResponse(scope, nowMs) {
  const d = new Date(nowMs);
  const resets = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) + 864e5).toISOString();
  return { error: "quota", scope, resets_at: resets };
}

async function callUpstream(env, messages) {
  const key = env.OPENROUTER_KEY;
  let r;
  try {
    r = await timedFetch(
      OPENROUTER_URL,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${key}`,
          "Content-Type": "application/json",
          "HTTP-Referer": SITE_ORIGIN,
          "X-Title": "Hermes Finances demo",
          "X-OpenRouter-Title": "Hermes Finances demo",
        },
        body: JSON.stringify({ model: env.MODEL, messages, max_tokens: 300, temperature: 0.3, reasoning: { exclude: true } }),
      },
      config.upstreamTimeoutMs,
    );
  } catch (e) {
    return { status: e && e.name === "AbortError" ? 504 : 502 };
  }
  // Out of credit (402) or OpenRouter's daily free-model limit means "used up for today". Any other 429 is one free
  // provider's per-minute limit: transient, so busy (a canned answer now, live again on the next question).
  if (r.status === 402 || (r.status === 429 && /per-day|per day|daily/i.test(String(r.text || "")))) {
    return { quota: true, status: r.status };
  }
  if (r.status === 429) return { status: 503 };
  if (r.status !== 200) return { status: 502 };
  let data;
  try {
    data = JSON.parse(r.text);
  } catch {
    return { status: 502 };
  }
  const content = data && data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content;
  const reply = cleanReply(content, key);
  if (reply === null) return { status: 502 };
  let model = env.MODEL;
  if (
    data && typeof data.model === "string" && data.model.length <= 100 &&
    /^[A-Za-z0-9_.:\/~@-]+$/.test(data.model) && !(key && data.model.includes(key))
  ) {
    model = data.model;
  }
  return { status: 200, reply, model };
}

export async function handle(request, env) {
  const origin = request.headers.get("Origin");
  if (origin !== null && !isAllowedOrigin(origin)) {
    const path = new URL(request.url).pathname;
    // No ACAO header for a forbidden origin.
    return respond(null, path, 403, "bad", { error: "forbidden origin" });
  }
  const path = new URL(request.url).pathname;
  if (!PATHS.has(path)) return respond(origin, path, 404, "bad", { error: "not found" });
  if (request.method === "OPTIONS") return respond(origin, path, 204, "ok", null);
  if (request.method !== "POST") return respond(origin, path, 405, "bad", { error: "method not allowed" });

  const ctype = request.headers.get("Content-Type") || "";
  const raw = await readBodyLimited(request);
  if (raw === null) return respond(origin, path, 413, "bad", { error: "too large" });
  const body = ctype.toLowerCase().includes("application/json") ? validateBody(raw) : null;
  if (!body) return respond(origin, path, 400, "bad", { error: "bad request" });

  const ip = request.headers.get("CF-Connecting-IP") || "";

  if (env.TURNSTILE_REQUIRED === "true") {
    const ok = await verifyTurnstile(env, body.turnstile, ip);
    if (!ok) return respond(origin, path, 403, "bad", { error: "verification failed" });
  }

  if (!env.OPENROUTER_KEY) return respond(origin, path, 503, "busy", BUSY);

  // A named preset is answered from its frozen data. An absent, unknown or unloadable one falls through to
  // the general summary (never a 503 of its own).
  const preset = body.preset === null ? null : await loadPreset(env, body.variant, body.preset);
  let messages;
  if (preset) {
    messages = buildMessages(preset, body.question, body.shift);
  } else {
    const summary = await loadGeneralSummary(env, body.variant, body.shift);
    if (summary === null) return respond(origin, path, 503, "busy", BUSY);
    messages = buildGeneralMessages(summary, body.question);
  }

  let quota;
  try {
    quota = await takeQuota(env, ip);
  } catch {
    return respond(origin, path, 503, "busy", BUSY);
  }
  if (!quota.allowed) {
    if (quota.unavailable) return respond(origin, path, 503, "busy", BUSY);
    return respond(origin, path, 429, "capped", quotaResponse(quota.scope, clock()));
  }

  const up = await callUpstream(env, messages);
  if (up.quota) return respond(origin, path, 429, "capped", quotaResponse("global", clock()));
  if (up.status !== 200) return respond(origin, path, up.status, "busy", BUSY);
  return respond(origin, path, 200, "ok", { reply: up.reply, model: up.model });
}

export default {
  async fetch(request, env, ctx) {
    try {
      return await handle(request, env, ctx);
    } catch {
      const o = request.headers.get("Origin");
      return respond(o !== null && isAllowedOrigin(o) ? o : null, "", 500, "busy", BUSY);
    }
  },
};
