/* Read-only static demo adapter. The application assets are copied without modification.
 * Financial page answers and sort orders come from the real App; no mutable money overlay.
 * Only transaction selection/pagination and its displayed aggregates run in this browser.
 */
(() => {
  'use strict';
  const ME = document.currentScript;
  const realFetch = window.fetch.bind(window), RealDate = window.Date;
  // Demo v2 (t_d31fda2a). The weekly build (scripts/demo_weekly.sh, demo_combine.py) holds 7 variants, one per weekday
  // (Monday 0), each built for its own day: a visitor gets their weekday's (?demo-variant=0-6 picks one). The demo's
  // today is the visitor's today (?demo-today=YYYY-MM-DD or =build sets it): every date in every answer moves by the
  // same number of days (SHIFT.days) and every month key by the months that moves the build day (SHIFT.months), the
  // question moved back first, so each answer is the frozen one with its dates moved (scripts/demo_parity.py proves it).
  const ASKED = new URL(location.href).searchParams;
  const VARIANTS = ME.dataset.variants ? ME.dataset.variants.split(',') : null;
  const VARIANT = !VARIANTS ? null : /^[0-6]$/.test(ASKED.get('demo-variant') || '') ? Number(ASKED.get('demo-variant')) :
    (new RealDate().getDay() + 6) % 7;
  const BUILD = ME.dataset.build, NOW = Date.parse(VARIANTS ? VARIANTS[VARIANT] : ME.dataset.now);
  const DATA = VARIANT === null ? '/demo/data/' : `/demo/data/v${VARIANT}/`;
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const addMonths = (m, n) => { const k = Number(m.slice(0, 4)) * 12 + Number(m.slice(5, 7)) - 1 + n; return `${String(Math.floor(k / 12)).padStart(4, '0')}-${String(k % 12 + 1).padStart(2, '0')}`; };
  const addDays = (iso, n) => new RealDate(RealDate.parse(`${iso}T12:00:00Z`) + n * 864e5).toISOString().slice(0, 10);
  const BUILD_DAY = new RealDate(NOW).toISOString().slice(0, 10);
  const SHIFT = {days: 0, months: 0};
  {
    const want = ASKED.get('demo-today') || '', here = new RealDate();
    const day = want === 'build' ? BUILD_DAY : /^\d{4}-\d{2}-\d{2}$/.test(want) && Number.isFinite(RealDate.parse(want)) ? want :
      new RealDate(RealDate.UTC(here.getFullYear(), here.getMonth(), here.getDate())).toISOString().slice(0, 10);
    const days = Math.round((RealDate.parse(day) - RealDate.parse(BUILD_DAY)) / 864e5);
    if (Math.abs(days) <= 366) {
      const to = addDays(BUILD_DAY, days);
      Object.assign(SHIFT, {days, months: (Number(to.slice(0, 4)) - Number(BUILD_DAY.slice(0, 4))) * 12 + Number(to.slice(5, 7)) - Number(BUILD_DAY.slice(5, 7))});
    }
  }
  const DAY_RE = /^(\d{4}-\d{2}-\d{2})([T ].*)?$/, MONTH_RE = /^\d{4}-\d{2}$/;
  const PROSE_RE = /\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2})\b/g;
  function moveText(s, sign, prose) {   // one string, moved by SHIFT (sign -1: back)
    const m = DAY_RE.exec(s);
    if (m && Number.isFinite(RealDate.parse(m[1]))) return addDays(m[1], sign * SHIFT.days) + (m[2] || '');
    if (MONTH_RE.test(s) && Number(s.slice(5)) >= 1 && Number(s.slice(5)) <= 12) return addMonths(s, sign * SHIFT.months);
    if (!prose) return s;
    return s.replace(PROSE_RE, (all, mon, d) => {   // "sent Oct 25": the nearest such day to the build day
      const k = MONTHS.findIndex((x) => x.startsWith(mon)), y = Number(BUILD_DAY.slice(0, 4));
      const near = [y - 1, y, y + 1].map((yy) => `${yy}-${String(k + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`)
        .filter((x) => Number.isFinite(RealDate.parse(x)) && new RealDate(RealDate.parse(x)).toISOString().slice(0, 10) === x)
        .sort((a, b) => Math.abs(RealDate.parse(a) - RealDate.parse(BUILD_DAY)) - Math.abs(RealDate.parse(b) - RealDate.parse(BUILD_DAY)))[0];
      if (!near) return all;
      const x = addDays(near, sign * SHIFT.days);
      return `${MONTHS[Number(x.slice(5, 7)) - 1].slice(0, 3)} ${Number(x.slice(8))}`;
    });
  }
  const moved = (x, sign = 1, prose = true) => !SHIFT.days && !SHIFT.months ? x : Array.isArray(x) ? x.map((v) => moved(v, sign, prose)) :
    x && typeof x === 'object' ? Object.fromEntries(Object.entries(x).map(([k, v]) => [moveText(k, sign, prose), moved(v, sign, prose)])) :
      typeof x === 'string' ? moveText(x, sign, prose) : x;
  const offset = NOW + SHIFT.days * 864e5 - RealDate.now();
  function DemoDate(...args) {
    if (!new.target) return new RealDate(RealDate.now() + offset).toString();
    return args.length ? new RealDate(...args) : new RealDate(RealDate.now() + offset);
  }
  DemoDate.prototype = RealDate.prototype;
  DemoDate.now = () => RealDate.now() + offset;
  DemoDate.parse = RealDate.parse; DemoDate.UTC = RealDate.UTC;
  window.Date = DemoDate;
  const REFUSAL = 'Not available in the demo';
  const KEY = 'hf-demo.display.v2', cache = new Map();
  let prefs = {}, HF = null;
  try { prefs = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (_) { /* storage unavailable */ }
  if (['dark', 'light', 'system'].includes(prefs['appearance.theme'])) document.documentElement.dataset.themePref = prefs['appearance.theme'];
  for (const k of ['currency', 'locale']) if (typeof prefs[`region.${k}`] === 'string') document.documentElement.dataset[k] = prefs[`region.${k}`];
  // No previous build's mutable overlays are ever loaded.
  (window.HF_EXT = window.HF_EXT || []).push((h) => { HF = h; });
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const load = (name) => {
    if (!cache.has(name)) cache.set(name, realFetch(`${DATA}${name}.json`).then((r) => {
      if (!r.ok) throw new Error('Missing demo snapshot');
      return r.json();
    }));
    return cache.get(name);
  };
  const core = () => load('core');
  const key = (path, p) => path + ([...p].filter(([, v]) => v !== '').sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
    .map(([k, v], i) => `${i ? '&' : '?'}${k}=${v}`).join(''));
  const fail = (status, error) => { throw Object.assign(new Error(error), {status}); };
  function refuse() { if (HF) HF.toast(REFUSAL); return [403, {error: REFUSAL}]; }
  const cents = (v) => Math.round(v * 100);
  const kinds = {all: () => true, review: (r) => r.review, spend: (r) => !r.is_income && !r.is_transfer,
    income: (r) => r.is_income, transfers: (r) => r.is_transfer};
  const truth = (s) => s === '1' || s === 'true';
  function period(p, today) {
    const month = p.get('month'); let start = p.get('since') || null, end = p.get('until') || null;
    if (month && (start || end)) fail(400, "--month can't be combined with --since/--until");
    if (month) {
      if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) fail(400, 'Invalid month');
      start = `${month}-01`;
      end = new RealDate(RealDate.UTC(Number(month.slice(0, 4)), Number(month.slice(5)), 0)).toISOString().slice(0, 10);
    } else {
      if (start && /^\d+d$/.test(start)) start = new RealDate(Date.parse(`${today}T12:00:00Z`) - (Number(start.slice(0, -1)) - 1) * 864e5).toISOString().slice(0, 10);
      if (start && !end) end = today;
    }
    for (const d of [start, end]) if (d && (!/^\d{4}-\d{2}-\d{2}$/.test(d) || !Number.isFinite(Date.parse(d)) || new RealDate(d).toISOString().slice(0, 10) !== d)) fail(400, 'Invalid date');
    if (start && end && start > end) fail(400, '--since is after --until');
    return {kind: month ? 'month' : start || end ? 'range' : 'all', month: month || null, start, end,
      days: start && end ? Math.round((Date.parse(end) - Date.parse(start)) / 864e5) + 1 : null};
  }
  async function transactions(p) {
    const allowed = new Set(['month', 'since', 'until', 'q', 'bank', 'kind', 'category', 'group', 'tag', 'uncategorized', 'sort', 'limit', 'offset', 'facets']);
    if ([...p.keys()].some((k) => !allowed.has(k))) fail(404, 'Unknown demo query');
    const data = await load('transactions'), c = await core();
    const per = period(p, c.meta.today), kind = p.get('kind') || 'all', sort = p.get('sort') || 'date_desc';
    if (!kinds[kind] || !data.orders[sort]) fail(400, 'Invalid transaction kind or sort');
    const integer = (k, def, min, max) => { const v = p.get(k) || String(def); if (!/^\d+$/.test(v) || +v < min || +v > max) fail(400, `Invalid ${k}`); return +v; };
    const limit = integer('limit', 50, 1, 500), offset = integer('offset', 0, 0, 10000000);
    const cats = p.getAll('category'), groups = p.getAll('group'), tags = p.getAll('tag').map((v) => v.toLowerCase());
    const q = (p.get('q') || '').trim().toLowerCase(), bank = p.get('bank');
    const groupOf = data.groups;
    const lines = (r) => r.split || [{category: r.category, amount: r.amount}];
    const pickedLine = (l) => cats.includes(l.category) || groups.includes(groupOf[l.category]);
    const picked = (r) => (!(cats.length || groups.length || truth(p.get('uncategorized'))) ||
      lines(r).some(pickedLine) || (truth(p.get('uncategorized')) && r.uncategorized));
    const tagged = (r) => !tags.length || r.tags.some((t) => tags.includes(t.toLowerCase()));
    const base = data.rows.filter((r) => (!per.start || r.date >= per.start) && (!per.end || r.date <= per.end) &&
      (!bank || r.institution_name === bank) && (!q || [r.merchant_name, r.name, r.note, r.tags.join(' ')].some((s) => (s || '').toLowerCase().includes(q))));
    const matched = base.filter((r) => picked(r) && tagged(r));
    const counts = Object.fromEntries(Object.entries(kinds).map(([k, fn]) => [k, matched.filter(fn).length]));
    const selected = matched.filter(kinds[kind]), byId = new Map(selected.map((r) => [r.transaction_id, r]));
    const rows = data.orders[sort].filter((id) => byId.has(id)).slice(offset, offset + limit).map((id) => byId.get(id));
    const nonTransfer = selected.some((r) => !r.is_transfer);
    const amount = (r) => r.is_transfer && nonTransfer ? 0 : cats.length || groups.length ?
      lines(r).filter(pickedLine).reduce((n, l) => n + cents(l.amount), 0) : cents(r.amount);
    const days = {}, months = {};
    for (const r of selected) {
      for (const [out, k] of [[days, r.date], [months, r.date.slice(0, 7)]]) {
        if (!out[k]) out[k] = {count: 0, total: 0};
        out[k].count++; out[k].total += amount(r);
      }
    }
    for (const o of [days, months]) for (const v of Object.values(o)) v.total /= 100;
    const out = {total: counts[kind], counts, limit, offset, rows, sort, sum: selected.reduce((n, r) => n + amount(r), 0) / 100,
      days: Object.fromEntries([...new Set(rows.map((r) => r.date))].sort().map((d) => [d, days[d]])), months,
      period: per, coverage_month: data.coverage_month};
    if (truth(p.get('facets'))) {
      const fc = base.filter((r) => kinds[kind](r) && tagged(r)), ft = base.filter((r) => kinds[kind](r) && picked(r));
      const categoryCounts = {}, groupCounts = {};
      for (const r of fc) {
        for (const cat of new Set(lines(r).map((l) => l.category))) categoryCounts[cat] = (categoryCounts[cat] || 0) + 1;
        for (const group of new Set(lines(r).map((l) => groupOf[l.category]).filter(Boolean))) groupCounts[group] = (groupCounts[group] || 0) + 1;
      }
      out.facets = {categories: categoryCounts, groups: groupCounts, uncategorized: fc.filter((r) => r.uncategorized).length,
        tags: data.tags.map((t) => ({name: t.name, count: ft.filter((r) => r.tags.includes(t.name)).length}))};
    }
    return out;
  }
  // ---- Ask v2 (t_d31fda2a): preset questions, answered at build time by the real read tools (scripts/demo_ask.py) ----
  // A question is matched to a preset by its words (token overlap plus the build's synonyms; no model, no ML), the answer
  // drawn by the app's own chat renderer, and demo/ask.js adds the answer's blocks and chips. With "Try live AI" on, a
  // matched question also goes to the demo's worker (same origin, /ai/chat); any failure or a slow (8 s) answer falls
  // back to the canned one, quietly, with a one-line note.
  const LIVE_KEY = 'hf-demo.live-ai', ANSWERS_KEY = 'hf-demo.ask.v2', LIVE_MS = 8000;
  // {{d:YYYY-MM-DD}} "Oct 26", {{D:..}} "Oct 26, 2026", {{m:YYYY-MM}} "October 2026", {{M:..}} "Oct 2026" (demo_ask.render)
  function render(text) {
    return String(text).replace(/\{\{([dDmM]):(\d{4}-\d{2}(?:-\d{2})?)\}\}/g, (_, kind, v) => {
      if (kind === 'd' || kind === 'D') {
        if (v.length !== 10) return v;
        const x = addDays(v, SHIFT.days);
        return `${MONTHS[Number(x.slice(5, 7)) - 1].slice(0, 3)} ${Number(x.slice(8))}${kind === 'D' ? `, ${x.slice(0, 4)}` : ''}`;
      }
      const mo = addMonths(v.slice(0, 7), SHIFT.months), name = MONTHS[Number(mo.slice(5)) - 1];
      return `${kind === 'm' ? name : name.slice(0, 3)} ${mo.slice(0, 4)}`;
    });
  }
  const renderDeep = (x) => Array.isArray(x) ? x.map(renderDeep) : x && typeof x === 'object' ?
    Object.fromEntries(Object.entries(x).map(([k, v]) => [k, renderDeep(v)])) : typeof x === 'string' ? render(x) : x;
  function words(text, m) {
    let t = ` ${String(text).toLowerCase().replace(/[‘’]/g, "'").replace(/'s\b/g, '').replace(/[^a-z0-9$]+/g, ' ')} `;
    for (const [phrase, concept] of Object.entries(m.phrases)) t = t.split(` ${phrase} `).join(` ${concept} `);
    return t.trim().split(/\s+/).filter(Boolean).map((w) => (w.length > 3 && /ies$/.test(w) ? `${w.slice(0, -3)}y` : w));
  }
  function concepts(text, m) {
    const by = new Map();
    for (const [concept, list] of Object.entries(m.synonyms)) for (const w of list) by.set(w, [...(by.get(w) || []), concept]);
    const out = new Set();
    for (const w of words(text, m)) {
      for (const v of [w, w.length > 3 && /[^s]s$/.test(w) ? w.slice(0, -1) : null]) for (const c of (v && by.get(v)) || []) out.add(c);
    }
    return out;
  }
  // A merchant counts when its whole name is in the question, or its first word is (4+ letters, no other merchant's).
  function merchantHit(text, name, all, m) {
    const q = ` ${words(text, m).join(' ')} `, n = words(name, m);
    if (!n.length) return false;
    if (q.includes(` ${n.join(' ')} `)) return true;
    const first = n[0], stop = new Set(m.stopwords);
    return first.length >= 4 && !stop.has(first) && !Object.values(m.synonyms).some((l) => l.includes(first)) &&
      all.filter((x) => words(x, m)[0] === first).length === 1 && q.includes(` ${first} `);
  }
  function match(text, ask) {
    const m = ask.matcher, have = concepts(text, m);
    const ids = Object.keys(ask.presets), names = ids.map((id) => ask.presets[id].merchant).filter(Boolean);
    let best = null, score = 0;
    for (const id of ids) {
      const p = ask.presets[id], present = new Set(have);
      if (p.merchant) { if (!merchantHit(text, p.merchant, names, m)) continue; present.add('merchant_name'); }
      if (!p.need.some((alt) => alt.every((c) => present.has(c)))) continue;
      const s = Object.entries(p.terms).reduce((n, [c, w]) => n + (present.has(c) ? w : 0), 0);
      if (s > score) { best = id; score = s; }
    }
    return score >= m.threshold ? best : null;
  }
  const ANSWERS = new Map();   // reply text -> {preset, blocks, next | suggestions, live, at}: what demo/ask.js draws
  try { for (const [k, v] of JSON.parse(sessionStorage.getItem(ANSWERS_KEY) || '[]')) ANSWERS.set(k, v); } catch (_) { /* none */ }
  function remember(reply, extra) {
    ANSWERS.delete(reply); ANSWERS.set(reply, {...extra, at: RealDate.now()});
    while (ANSWERS.size > 40) ANSWERS.delete(ANSWERS.keys().next().value);
    try { sessionStorage.setItem(ANSWERS_KEY, JSON.stringify([...ANSWERS])); } catch (_) { /* kept in memory */ }
  }
  const live = {
    get: () => { try { return localStorage.getItem(LIVE_KEY) === '1'; } catch (_) { return false; } },
    set: (on) => { try { if (on) localStorage.setItem(LIVE_KEY, '1'); else localStorage.removeItem(LIVE_KEY); } catch (_) { /* session only */ } },
  };
  async function liveReply(question, preset) {
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), LIVE_MS);
    try {
      const r = await realFetch('/ai/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, signal: ctl.signal,
        body: JSON.stringify({question: question.slice(0, 300), preset, variant: VARIANT, shift: SHIFT})});
      if (r.status !== 200) return null;
      const j = await r.json();
      const text = j && typeof j.reply === 'string' ? j.reply.trim() : '';
      return text && text.length <= 1500 ? render(text) : null;
    } catch (_) { return null; } finally { clearTimeout(timer); }
  }
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  async function chat(question) {
    const ask = await load('ask'), id = match(question, ask);
    const wait = typeof window.HFDemoAskDelay === 'number' ? window.HFDemoAskDelay : 650;   // "Looking at your numbers…"
    if (!id) {
      const u = ask.unmatched, out = {reply: render(u.reply), sources: u.sources.map(render), stats: [], cards: []};
      if (wait) await pause(wait);
      remember(out.reply, {preset: null, blocks: [], suggestions: u.suggestions, live: false});
      return out;
    }
    const p = ask.presets[id];
    const out = {reply: render(p.reply), sources: p.sources.map(render), stats: clone(p.stats), cards: []};
    const extra = {preset: id, blocks: moved(renderDeep(p.blocks), 1, false), next: p.next, live: false};
    if (live.get()) {
      const text = await liveReply(question, id);
      if (text) { out.reply = text; out.sources = [ask.live.source, ...out.sources]; extra.live = true; }
      else out.sources = [...out.sources, ask.live.note];
    } else if (wait) await pause(wait);
    remember(out.reply, extra);
    return out;
  }
  function withPrefs(page) {
    for (const [k, v] of Object.entries(prefs)) {
      const [group, name] = k.split('.');
      if (page.prefs[group]) page.prefs[group][name] = v;
    }
    return page;
  }
  async function route(method, url, body) {
    if ((!SHIFT.days && !SHIFT.months) || !['GET', 'HEAD'].includes(method)) return answer(method, url, body);
    const u = new URL(url, location.href), back = new URLSearchParams();
    for (const [k, v] of u.searchParams) back.append(k, moveText(v, -1, false));
    u.search = back.toString();
    const [status, value] = await answer(method, u.href, body);
    return [status, moved(value)];
  }
  async function answer(method, url, body) {
    try {
      const u = new URL(url, location.href), path = u.pathname, p = u.searchParams;
      if (u.origin !== location.origin) return refuse();
      if (!['GET', 'HEAD'].includes(method)) {
        let d = {};
        try { d = body ? JSON.parse(body) : {}; } catch (_) { return refuse(); }
        if (method === 'POST' && path === '/api/chat') {
          const q = String(((d.messages || []).filter((m) => m.role === 'user').pop() || {}).content || '').trim();
          return [200, await chat(q)];
        }
        if (path === '/api/prefs') coreKeys = (await core()).meta.page_keys;
        if (method === 'POST' && path === '/api/prefs' && Object.keys(d).length &&
          Object.keys(d).every((k) => /^(appearance|region)\./.test(k) && (coreKeys || []).includes(k))) {
          const page = clone((await core()).core['/api/settings/page']);
          const choices = {'appearance.theme': ['dark', 'light', 'system'], 'region.currency': page.options.currencies,
            'region.locale': page.options.locales, 'region.week_start': ['sun', 'mon'], 'region.budget_month_start': [1]};
          if (Object.entries(d).some(([k, v]) => !choices[k] || !choices[k].includes(v))) fail(400, 'Invalid display preference');
          Object.assign(prefs, d);
          try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch (_) { /* local only */ }
          return [200, {saved: d, prefs: withPrefs(clone((await core()).core['/api/settings/page'])).prefs}];
        }
        return refuse();
      }
      const c = await core(); coreKeys = c.meta.page_keys;
      if (path === '/api/transactions') return [200, await transactions(p)];
      if (path === '/api/chat/cards') return [200, {cards: []}];
      if (['/api/overview', '/api/budgets', '/api/counts'].includes(path)) {
        if ([...p.keys()].some((k) => k !== 'month')) fail(404, 'Unknown demo query');
        const m = p.get('month') || c.meta.this_month;
        if (!c.meta.months.includes(m)) fail(400, 'Month outside demo history');
        return [200, clone((await load(`m/${m}`))[path.slice(5)])];
      }
      if (path === '/api/bills/lookup' && p.get('transaction_id')) {
        const m = c.meta.tx_month[p.get('transaction_id')];
        if (m) return [200, clone((await load(`m/${m}`)).bill_lookups[p.get('transaction_id')])];
      }
      if (path === '/api/saved-filters') {
        const m = p.get('month') || c.meta.this_month;
        const out = clone(c.core[path]); out.month = m;
        for (const f of out.filters) {
          const fp = new URLSearchParams();
          for (const [k, v] of Object.entries(f.filters)) for (const x of Array.isArray(v) ? v : [v]) fp.append(k, x);
          for (const k of ['month', 'since', 'until']) if (p.get(k)) fp.set(k, p.get(k));
          if (!p.get('window')) fp.set('month', m);
          f.count = (await transactions(fp)).total;
        }
        return [200, out];
      }
      let k = key(path, p);
      if (path === '/api/sync' && [...p.keys()].every((v) => v === 'rid')) k = path;
      if (Object.hasOwn(c.core, k)) {
        const out = clone(c.core[k]);
        return [200, path === '/api/settings/page' ? withPrefs(out) : out];
      }
      if (c.errors[k]) return [c.errors[k].status, clone(c.errors[k].body)];
      fail(404, 'Unknown demo API path or query');
    } catch (e) { return [e.status || 500, {error: e.message}]; }
  }
  let coreKeys = null;
  window.fetch = async (input, init = {}) => {
    const u = new URL(typeof input === 'string' || input instanceof URL ? String(input) : input.url, location.href);
    const method = (init.method || input.method || 'GET').toUpperCase();
    if (u.origin !== location.origin || u.pathname.startsWith('/api/')) {
      const body = init.body !== undefined ? init.body : input instanceof Request ? await input.clone().text() : null;
      const [status, value] = await route(method, u.href, body);
      return new Response(JSON.stringify(value), {status, headers: {'Content-Type': 'application/json'}});
    }
    if (!['GET', 'HEAD'].includes(method)) {
      const [status, value] = refuse(); return new Response(JSON.stringify(value), {status});
    }
    return realFetch(input, init);
  };
  // XHR has no consumers in the real UI; reject it before it can open any network connection.
  XMLHttpRequest.prototype.open = function () { refuse(); throw new Error(REFUSAL); };
  if (window.navigator && navigator.sendBeacon) navigator.sendBeacon = () => { refuse(); return false; };
  function banner() {
    document.documentElement.classList.add('hf-demo');
    const bar = document.createElement('div'); bar.id = 'hf-demo-bar';
    bar.setAttribute('role', 'region'); bar.setAttribute('aria-label', 'Demo notice');
    const tag = document.createElement('strong'); tag.className = 'hfd-tag'; tag.textContent = 'Demo — synthetic data';
    const text = document.createElement('span'); text.className = 'hfd-text'; text.textContent = 'Read-only · no account or model connected';
    bar.append(tag, text); document.body.prepend(bar);
    const prompts = () => {
      // Some product handlers add context to an API error. The demo refusal toast is an exact contract.
      const toast = document.getElementById('toast');
      if (HF && toast && toast.textContent.includes(REFUSAL) && toast.textContent.trim() !== REFUSAL) HF.toast(REFUSAL);
      for (const a of document.querySelectorAll('a[href]')) {
        const u = new URL(a.href, location.href);
        if (VARIANT !== null && u.origin === location.origin && u.pathname === '/api/export.csv') a.href = `${DATA}export.csv`;
        if (u.origin !== location.origin || u.pathname.startsWith('/docs') || u.pathname === '/api/backup') {
          a.dataset.demoBlocked = '1'; a.href = '#'; a.removeAttribute('target');
        }
      }
    };
    prompts();
    new MutationObserver(prompts).observe(document.body, {subtree: true, childList: true});
    document.addEventListener('click', (e) => {
      const a = e.target instanceof Element && e.target.closest('a[href]');
      if (!a) return;
      const u = new URL(a.href, location.href);
      if (a.dataset.demoBlocked || u.origin !== location.origin || u.pathname === '/api/backup') { e.preventDefault(); refuse(); }
    }, true);
    document.addEventListener('submit', (e) => { if (e.target.hasAttribute('action')) { e.preventDefault(); refuse(); } }, true);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', banner, {once: true}); else banner();
  window.HFDemo = {route, build: BUILD, now: NOW, variant: VARIANT, shift: SHIFT, store: () => ({prefs}), transactions,
    ask: {match, render, answerFor: (reply) => ANSWERS.get(reply) || null, data: () => load('ask'), live, clock: () => RealDate.now()}};   // (answers' `at` is the real clock, not the demo's)
})();
