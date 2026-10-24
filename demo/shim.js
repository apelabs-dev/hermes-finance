/* Read-only static demo adapter. The application assets are copied without modification.
 * Financial page answers and sort orders come from the real App; no mutable money overlay.
 * Only transaction selection/pagination and its displayed aggregates run in this browser.
 */
(() => {
  'use strict';
  const ME = document.currentScript;
  const BUILD = ME.dataset.build, NOW = Date.parse(ME.dataset.now);
  const realFetch = window.fetch.bind(window), RealDate = window.Date;
  const offset = NOW - RealDate.now();
  function DemoDate(...args) {
    if (!new.target) return new RealDate(RealDate.now() + offset).toString();
    return args.length ? new RealDate(...args) : new RealDate(RealDate.now() + offset);
  }
  DemoDate.prototype = RealDate.prototype;
  DemoDate.now = () => RealDate.now() + offset;
  DemoDate.parse = RealDate.parse; DemoDate.UTC = RealDate.UTC;
  window.Date = DemoDate;
  const REFUSAL = 'Not available in the demo';
  const OFF = 'AI chat is off in the demo — install it to use your own model.';
  const QUESTIONS = ['How much did I spend on dining last month?', 'Am I on track with my budgets this month?',
    'What bills are due in the next 7 days?', 'How has my net worth changed this year?'];
  const KEY = 'hf-demo.display.v2', cache = new Map();
  let prefs = {}, HF = null;
  try { prefs = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (_) { /* storage unavailable */ }
  if (['dark', 'light', 'system'].includes(prefs['appearance.theme'])) document.documentElement.dataset.themePref = prefs['appearance.theme'];
  for (const k of ['currency', 'locale']) if (typeof prefs[`region.${k}`] === 'string') document.documentElement.dataset[k] = prefs[`region.${k}`];
  // No previous build's mutable overlays are ever loaded.
  (window.HF_EXT = window.HF_EXT || []).push((h) => { HF = h; });
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const load = (name) => {
    if (!cache.has(name)) cache.set(name, realFetch(`/demo/data/${name}.json`).then((r) => {
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
  function withPrefs(page) {
    for (const [k, v] of Object.entries(prefs)) {
      const [group, name] = k.split('.');
      if (page.prefs[group]) page.prefs[group][name] = v;
    }
    return page;
  }
  async function route(method, url, body) {
    try {
      const u = new URL(url, location.href), path = u.pathname, p = u.searchParams;
      if (u.origin !== location.origin) return refuse();
      if (!['GET', 'HEAD'].includes(method)) {
        let d = {};
        try { d = body ? JSON.parse(body) : {}; } catch (_) { return refuse(); }
        if (method === 'POST' && path === '/api/chat') {
          const q = String(((d.messages || []).filter((m) => m.role === 'user').pop() || {}).content || '').trim();
          const answers = await load('ask');
          return [200, clone(answers[q] || {reply: OFF, sources: [], stats: [], cards: []})];
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
    // Keep the real Ask UI, replacing its dynamic prompts with the four frozen examples.
    const prompts = () => {
      // Some product handlers add context to an API error. The demo refusal toast is an exact contract.
      const toast = document.getElementById('toast');
      if (HF && toast && toast.textContent.includes(REFUSAL) && toast.textContent.trim() !== REFUSAL) HF.toast(REFUSAL);
      for (const a of document.querySelectorAll('a[href]')) {
        const u = new URL(a.href, location.href);
        if (u.origin !== location.origin || u.pathname.startsWith('/docs') || u.pathname === '/api/backup') {
          a.dataset.demoBlocked = '1'; a.href = '#'; a.removeAttribute('target');
        }
      }
      const chips = document.querySelector('#chat .chchips');
      if (!chips || chips.textContent === QUESTIONS.join('')) return;
      chips.replaceChildren(...QUESTIONS.map((q) => {
        const b = document.createElement('button'); b.type = 'button'; b.className = 'fchip'; b.textContent = q;
        b.disabled = Boolean(chips.querySelector('button:disabled'));
        b.addEventListener('click', () => HF.chat.ask(q)); return b;
      }));
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
  window.HFDemo = {route, build: BUILD, now: NOW, store: () => ({prefs}), transactions};
})();
