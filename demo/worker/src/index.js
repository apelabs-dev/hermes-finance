// Hermes Finances demo: live-AI worker.
//
// The public static demo has canned Ask answers ("presets"). With "Try live AI" on, the page posts the
// visitor's question here; we ask an OpenRouter free model to answer from ONLY the matched preset's
// frozen synthetic data (fetched by this worker from the demo origin, never taken from the client).
// The browser falls back silently to the canned answer on anything but a valid 200, so every failure
// here is a bare {"error": "..."} with no upstream detail.

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
  if (typeof obj.preset !== "string" || !PRESET_RE.test(obj.preset)) return null;
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
  return { question, preset: obj.preset, variant, shift, turnstile };
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

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];

// The presets carry dates as tokens the page renders (scripts/demo_ask.py render, demo/shim.js render):
// {{d:YYYY-MM-DD}} "Oct 26", {{D:..}} "Oct 26, 2026", {{m:YYYY-MM}} "October 2026", {{M:..}} "Oct 2026",
// moved by the visitor's shift. The model sees the same plain dates the page shows.
export function renderDates(text, shift = { days: 0, months: 0 }) {
  return String(text).replace(/\{\{([dDmM]):(\d{4}-\d{2}(?:-\d{2})?)\}\}/g, (_, kind, v) => {
    if (kind === "d" || kind === "D") {
      if (v.length !== 10) return v;
      const x = new Date(Date.parse(`${v}T12:00:00Z`) + shift.days * 864e5).toISOString().slice(0, 10);
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

export function cleanReply(content, key) {
  if (typeof content !== "string") return null;
  // eslint-disable-next-line no-control-regex
  const text = content.replace(/[\u0000-\u0009\u000B-\u001F\u007F]/g, "").trim();
  if (text.length < 1 || text.length > MAX_REPLY) return null;
  if (key && text.includes(key)) return null;
  return text;
}

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
// Returns true when the request may go upstream (and has been counted).
async function takeQuota(env, ip) {
  const kv = env.DEMO_AI_KV;
  if (!kv) return false;
  const day = utcDay(clock());
  const gKey = `g:${day}`;
  const vKey = await visitorKey(ip, day);
  const [g, v] = await Promise.all([kv.get(gKey), kv.get(vKey)]);
  const gCount = Number.parseInt(g || "0", 10) || 0;
  const vCount = Number.parseInt(v || "0", 10) || 0;
  if (gCount >= capOf(env.GLOBAL_DAILY_CAP, 100)) return false;
  if (vCount >= capOf(env.VISITOR_DAILY_CAP, 10)) return false;
  await Promise.all([
    kv.put(gKey, String(gCount + 1), { expirationTtl: KV_TTL }),
    kv.put(vKey, String(vCount + 1), { expirationTtl: KV_TTL }),
  ]);
  return true;
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
        body: JSON.stringify({ model: env.MODEL, messages, max_tokens: 300, temperature: 0.3 }),
      },
      config.upstreamTimeoutMs,
    );
  } catch (e) {
    return { status: e && e.name === "AbortError" ? 504 : 502 };
  }
  if (r.status === 429) return { status: 429 };
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

  const preset = await loadPreset(env, body.variant, body.preset);
  if (!preset) return respond(origin, path, 503, "busy", BUSY);

  let allowed = false;
  try {
    allowed = await takeQuota(env, ip);
  } catch {
    return respond(origin, path, 503, "busy", BUSY);
  }
  if (!allowed) return respond(origin, path, 429, "capped", BUSY);

  const up = await callUpstream(env, buildMessages(preset, body.question, body.shift));
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
