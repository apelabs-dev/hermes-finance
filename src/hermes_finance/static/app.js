/* Hermes Finances dashboard.
 *
 * Every element is built with createElement + textContent through h(); strings that come from banks
 * (merchants, account names, categories) or from the user (manual account names) never become markup.
 * Icons are cloned from the page's <template>; rings and sparklines are SVG built with createElementNS from
 * numbers only. The only network calls are this server's JSON API and Plaid Link. Pages use hash routing:
 * #/overview, #/transactions?category=..., #/investments, #/debt, ..., and a car's / property's page #/asset/<id>.
 */
(() => {
  'use strict';

  // ---------- DOM helpers ----------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'style') for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    return add(el, kids);
  }

  function add(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k == null || k === false || k === '') continue;
      el.append(k instanceof Node ? k : String(k));  // strings become text nodes
    }
    return el;
  }

  function fill(el, ...kids) {  // replace the children; like h(), arrays are flattened and null/false skipped
    el.replaceChildren();
    return add(el, kids);
  }

  // The SVG namespace, taken from the icons the HTML parser already made (this file has no absolute URLs).
  const SVGNS = document.querySelector('#icons').content.firstElementChild.namespaceURI;
  function sv(tag, attrs, ...kids) {  // SVG element; attribute values are numbers or fixed strings from this file
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, String(v));
    for (const k of kids) if (k) el.append(k);
    return el;
  }

  function icon(name) {
    const svg = $('icons').content.querySelector(`[data-icon="${name}"]`);
    return svg ? svg.cloneNode(true) : h('span');
  }

  // Icon tiles (badges, card-title tiles, check circles, holding bubbles) draw every glyph at the same optical size,
  // centred: its viewBox is cropped to the shape it draws (measured once per icon), and CSS sizes the svg to 56% of
  // the tile. The stroke is scaled back to the weight a plain 24-unit icon of that size has. Plain icons elsewhere
  // keep the 24-unit box.
  const INK = new Map();
  let probe = null;
  function inkBox(name) {
    if (INK.has(name)) return INK.get(name);
    let box = null;
    const src = $('icons').content.querySelector(`[data-icon="${name}"]`);
    if (src) {
      if (!probe) {
        probe = sv('svg', {class: 'probe', width: 24, height: 24, 'aria-hidden': 'true', focusable: 'false'});
        document.body.append(probe);
      }
      const g = sv('g');
      for (const k of src.children) g.append(k.cloneNode(true));
      probe.append(g);
      try {
        const b = g.getBBox();
        if (b.width > 0 || b.height > 0) box = {x: b.x, y: b.y, w: b.width, h: b.height};
      } catch (e) { /* not rendered: keep the 24-unit box */ }
      g.remove();
    }
    INK.set(name, box);
    return box;
  }
  function glyph(name, weight = 1.9) {
    const svg = icon(name);
    const b = inkBox(name);
    if (b && svg instanceof SVGElement) {
      const side = Math.max(b.w, b.h) / (1 - weight / 24);  // the stroke's half-width on each side fits too
      const x = b.x + b.w / 2 - side / 2;
      const y = b.y + b.h / 2 - side / 2;
      svg.setAttribute('viewBox', `${x.toFixed(2)} ${y.toFixed(2)} ${side.toFixed(2)} ${side.toFixed(2)}`);
      svg.style.strokeWidth = (weight * side / 24).toFixed(3);
    }
    return svg;
  }

  const has = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  const plural = (n, word, many) => `${n} ${n === 1 ? word : many || `${word}s`}`;
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const sum = (xs, f) => xs.reduce((s, x) => s + (Number(f ? f(x) : x) || 0), 0);

  // ---------- formatting -----------------------------------------------------------------------
  // Currency and locale come from Settings > Region (TASK-18; <html data-currency data-locale>, USD / en-US by default).
  // Amounts are never converted: this is how they're written. The symbol's side, its gap, the separators and the number
  // of decimals are the locale's (Intl.NumberFormat); USD in en-US reads exactly as before ("$1,234.56").
  const LOC = document.documentElement.dataset.locale || 'en-US';
  const CUR = (() => {
    const code = document.documentElement.dataset.currency || 'USD';
    let f;
    try {
      f = new Intl.NumberFormat(LOC, {style: 'currency', currency: code});
    } catch (e) {
      f = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'});
    }
    const parts = f.formatToParts(1234.5);
    const at = (t) => parts.findIndex((x) => x.type === t);
    const ci = at('currency'), ii = at('integer');
    const before = ci < ii;
    const lit = parts.slice(Math.min(ci, ii) + 1, Math.max(ci, ii)).filter((x) => x.type === 'literal').map((x) => x.value).join('');
    return {code, sym: parts[ci].value, before, gap: before ? lit : (parts.slice(ci - 1, ci).find((x) => x.type === 'literal') || {}).value || '',
      dec: (parts.find((x) => x.type === 'decimal') || {value: '.'}).value, digits: f.resolvedOptions().maximumFractionDigits};
  })();
  const NF0 = new Intl.NumberFormat(LOC, {minimumFractionDigits: 0, maximumFractionDigits: 0});
  const NF2 = new Intl.NumberFormat(LOC, {minimumFractionDigits: CUR.digits, maximumFractionDigits: CUR.digits});
  const withSym = (s) => (CUR.before ? `${CUR.sym}${CUR.gap}${s}` : `${s}${CUR.gap}${CUR.sym}`);

  // Shared numeric parts keep hero figures and ordinary amounts identical, including rounded zero.
  function moneyParts(n, cents = false) {
    const v = Number(n) || 0;
    const nf = cents ? NF2 : NF0;
    const parts = nf.formatToParts(Math.abs(v));
    const whole = parts.filter((p) => p.type !== 'decimal' && p.type !== 'fraction').map((p) => p.value).join('');
    const fraction = parts.find((p) => p.type === 'fraction');
    const zeroFraction = nf.formatToParts(0).find((p) => p.type === 'fraction');
    const tail = fraction && fraction.value !== zeroFraction.value ? CUR.dec + fraction.value : '';
    const sign = v < 0 && nf.format(Math.abs(v)) !== nf.format(0) ? '-' : '';
    return {whole, tail, sign};
  }
  function money(n, cents = false) {
    const {whole, tail, sign} = moneyParts(n, cents);
    return sign + withSym(whole + tail);
  }
  const money2 = (n) => money(n, true);
  const signed = (amount) => (amount < 0 ? '+' + money2(-amount) : money2(amount));  // Plaid sign: < 0 is money in
  // TASK-21: a bill whose amount changes is shown at its estimate ("~$296 est."), and where there's room with its usual
  // range ("usually $4–40"); a fixed bill at its exact amount. A charge far from the usual the owner typed gets a quiet tag.
  const billAmt = (r) => (r.variable ? `~${money(r.amount)} est.` : money2(r.amount));
  const usualText = (u) => {
    const [lo, hi] = [Math.round(u.low), Math.round(u.high)];
    return lo === hi ? money(lo) : lo < 0 ? `${money(lo)}\u2013${money(hi)}` : withSym(`${NF0.format(lo)}\u2013${NF0.format(hi)}`);
  };
  const billTag = (o) => (o ? h('span', {class: 'pend', dataset: {outlier: o}}, `${o} than usual`) : null);

  // A hero number: the symbol and the cents set small, like the 005 mockup.
  function splitMoney(n, negative) {
    const v = Number(n) || 0;
    const {whole, tail, sign} = moneyParts(negative == null ? v : (negative ? -Math.abs(v) : Math.abs(v)), true);
    const cts = tail ? h('span', {class: 'cts'}, tail) : null;
    if (!CUR.before) return [sign + whole, cts, h('span', {class: 'cur'}, `${CUR.gap}${CUR.sym}`)];
    return [h('span', {class: 'cur'}, `${sign}${CUR.sym}${CUR.gap}`), whole, cts];
  }
  const bigMoney = (n, cls = '', negative) => h('div', {class: `big v ${cls}`}, splitMoney(n, negative));

  // Hero count-up (polish P3a): Spent, Left and Net worth count up to their value ONLY on a month switch (route() sets
  // COUNT.tk to that render's token), <= 400ms, by requestAnimationFrame, never under prefers-reduced-motion. The final
  // text is always the real formatted value (it is set on finish, on a new render and on a timer if frames stop), the
  // container carries the final value as its aria-label from the start, and tabular digits plus a min-width taken from the
  // final text keep the layout still.
  const COUNT = {tk: 0, done: new Set(), live: new Set()};
  const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const heroMoney = (n, key, cls = '') => { const el = bigMoney(n, cls); countUp(el, n, key); return el; };
  function finishCounts() { for (const stop of [...COUNT.live]) stop(); }
  function countUp(el, value, key, negative) {
    const to = Number(value) || 0;
    const tag = `${COUNT.tk}:${key}`;
    if (COUNT.tk !== S.token || COUNT.done.has(tag) || reducedMotion() || to === 0) return;
    COUNT.done.add(tag);
    const final = () => fill(el, splitMoney(to, negative));
    el.setAttribute('role', 'img');
    el.setAttribute('aria-label', money(to, true));
    let raf = 0, timer = 0;
    const stop = () => {
      cancelAnimationFrame(raf); clearTimeout(timer); COUNT.live.delete(stop);
      final(); el.style.minWidth = ''; el.removeAttribute('role'); el.removeAttribute('aria-label');
    };
    COUNT.live.add(stop);
    timer = setTimeout(stop, 400);
    raf = requestAnimationFrame((t0) => {
      el.style.minWidth = `${el.getBoundingClientRect().width}px`;
      const tick = (now) => {
        const p = Math.min(1, (now - t0) / 340);
        if (p >= 1) return stop();
        fill(el, splitMoney(Math.round(to * (1 - (1 - p) ** 3)), negative));
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    });
  }

  const decs = (text) => (CUR.dec === '.' ? text : text.replace('.', CUR.dec));
  function kf(v) {  // axis ticks: $0, $800, $1.2k, $12k
    const a = Math.abs(v);
    const s = v < 0 ? '-' : '';
    if (a < 1000) return money(v);
    const k = a / 1000;
    return `${s}${withSym(decs(`${k >= 10 || Number.isInteger(k) ? Math.round(k) : k.toFixed(1)}k`))}`;
  }

  function axisMoney(v, i, ticks) {  // like kf, with enough decimals that neighbouring ticks differ
    const step = ticks && ticks.length > 1 ? Math.abs(ticks[1].value - ticks[0].value) : 1000;
    const a = Math.abs(v);
    const s = v < 0 ? '-' : '';
    if (a < 1000) return money(v, step < 1);
    // Ticks a few dollars apart on a four-figure value (net worth moving $3 in a day): "$1.16k" four times said
    // nothing (2026-09-27), so whole dollars ("$1,157").
    if (step < 10) return money(v);
    const dec = step >= 1000 ? (Number.isInteger(a / 1000) ? 0 : 1) : step >= 100 ? 1 : 2;
    return `${s}${withSym(decs(`${(a / 1000).toFixed(dec).replace(/\.0+$|(\.\d*?)0+$/, '$1')}k`))}`;  // 1.50 -> 1.5, 40 stays 40
  }

  const pct = (ratio) => `${clamp(ratio, 0, 1) * 100}%`;
  const pctOf = (part, total) => (total ? `${Math.round((part / total) * 100)}%` : '0%');

  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
    'October', 'November', 'December'];
  const pad = (n) => String(n).padStart(2, '0');
  const asDate = (iso) => new Date(`${iso.slice(0, 10)}T12:00:00`);
  const monthName = (m) => MONTHS[Number(m.slice(5, 7)) - 1];
  const monthShort = (m) => monthName(m).slice(0, 3);
  const monthLabel = (m) => `${monthName(m)} ${m.slice(0, 4)}`;
  const monthYear = (m) => `${monthShort(m)} ${m.slice(0, 4)}`;

  function todayISO() {
    const d = new Date();
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }
  const thisMonth = () => todayISO().slice(0, 7);

  function addMonths(m, n) {
    const k = Number(m.slice(0, 4)) * 12 + Number(m.slice(5, 7)) - 1 + n;
    return `${Math.floor(k / 12)}-${pad((k % 12) + 1)}`;
  }

  function fmtDate(iso, forceYear) {
    const d = asDate(iso);
    const opts = {month: 'short', day: 'numeric'};
    if (forceYear || d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString(LOC, opts);
  }

  const daysFrom = (a, b) => Math.round((asDate(a) - asDate(b)) / 864e5);

  function relDay(iso, today) {
    const k = daysFrom(iso, today);
    if (k === 0) return 'Today';
    if (k === 1) return 'Tomorrow';
    if (k > 1 && k < 7) return `In ${k} days`;
    if (k < 0) return `${plural(-k, 'day')} ago`;
    return asDate(iso).toLocaleDateString(LOC, {weekday: 'long'});
  }

  // "Oct 30 · in 10 days"; a late bill says "late", not "ago". `short` drops the date for narrow cards and rows
  // that already show a calendar tile (it was cut off as "in 6 d…" on the Overview Bills card).
  function relativeDate(iso, today, late, short) {
    const k = daysFrom(iso, today);
    const relative = k === 0 ? 'Today' : k === 1 ? 'Tomorrow' : k > 1 ? `in ${k} days` : `${-k} ${-k === 1 ? 'day' : 'days'} ${late ? 'late' : 'ago'}`;
    if (short) return k < 0 && late ? relative : k <= 1 ? relative : `Due ${relative}`;
    return `${fmtDate(iso)} · ${relative}`;
  }

  function daysAgo(iso) {  // "updated today", "yesterday", "12 days ago"
    const k = daysFrom(todayISO(), iso);
    if (k <= 0) return 'today';
    if (k === 1) return 'yesterday';
    return `${k} days ago`;
  }

  function ago(ts) {  // a timestamp: "just now", "24 min ago", "3 h ago", "2 d ago"
    const t = new Date(ts).getTime();
    if (Number.isNaN(t)) return '';
    const m = Math.max(0, Math.round((Date.now() - t) / 6e4));
    return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
  }

  function syncLabel(ts) {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return '';
    const time = d.toLocaleTimeString(LOC, {hour: 'numeric', minute: '2-digit'});
    if (d.toDateString() === new Date().toDateString()) return time;
    return `${d.toLocaleDateString(LOC, {month: 'short', day: 'numeric'})}, ${time}`;
  }

  // ---------- colours --------------------------------------------------------------------------
  // One fixed colour and icon per category, used by every chart, badge and bar. Spending categories sit in groups
  // (polish P0): a tile takes its group's colour, and every category has its own Lucide icon. The same table as
  // db.CATEGORY_GROUPS (test_taxonomy_p0 checks they agree); Income and Transfers stay outside the groups.
  const CAT_GROUPS = [
    ['Home', '142,168,255', {'Rent & Utilities': 'house', 'Internet & Phone': 'wifi', 'Home Goods': 'armchair', 'Home Repair': 'paint-roller'}],
    ['Food', '245,180,84', {'Groceries': 'shopping-cart', 'Eating Out': 'utensils', 'Coffee': 'coffee', 'Alcohol & Bars': 'wine'}],
    ['Transport', '95,198,232', {'Gas': 'fuel', 'Parking': 'square-parking', 'Rideshare': 'car-taxi-front', 'Car Payment': 'hand-coins',
      'Car Maintenance': 'wrench', 'Public Transit': 'train-front', 'Other Transport': 'route'}],
    ['Shopping', '245,158,139', {'Clothing': 'shirt', 'Electronics': 'laptop', 'Other Shopping': 'shopping-bag'}],
    ['Bills', '163,168,255', {'Subscriptions': 'repeat', 'Insurance': 'shield-check', 'Fees': 'receipt'}],
    ['Health', '199,146,234', {'Personal Care': 'scissors', 'Gym': 'dumbbell', 'Pharmacy': 'pill', 'Doctor & Dentist': 'stethoscope',
      'Other Health': 'heart-pulse'}],
    ['Leisure', '255,143,177', {'Entertainment': 'clapperboard', 'Travel': 'plane', 'Hobbies': 'palette', 'Hotels': 'bed-double'}],
    ['Misc', '155,162,182', {'Gifts & Charity': 'gift', 'Pets': 'paw-print', 'Education': 'graduation-cap', 'Other': 'ellipsis'}],
  ];
  // A group's tile (the Budgets table, a group budget's row and alert): its own drawing, in the group's colour. Transport
  // keeps the car (v19: its catch-all, Other Transport, is the route), the rest borrow a category's.
  const GROUP_ICON = {Home: 'house', Food: 'utensils', Transport: 'car-front', Shopping: 'shopping-bag', Bills: 'repeat',
    Health: 'heart-pulse', Leisure: 'clapperboard', Misc: 'ellipsis'};
  const hexOf = (rgb) => `#${rgb.split(',').map((v) => Number(v).toString(16).padStart(2, '0')).join('')}`;
  const PALETTE = {'Income': '#4fd1a5', 'Transfers': '#8a93a8'};
  const CATI = {'Income': 'income', 'Transfers': 'swap'};
  const CATG = {};
  for (const [group, rgb, cats] of CAT_GROUPS) {
    for (const [name, ic] of Object.entries(cats)) { PALETTE[name] = hexOf(rgb); CATI[name] = ic; CATG[name] = group; }
  }
  // polish x-labels: format category name with group prefix using middle dot
  const catLabel = (name) => {
    const group = CATG[name];
    return group && group !== name ? `${group} · ${name}` : name;
  };
  const EXTRA = ['#e6a0c4', '#9fd8cb', '#d7b98e', '#b3c7f7', '#f0c987', '#a5d6a7'];
  const ACC = '#8ea8ff';
  const OK = '#4fd1a5';
  const WARN = '#f5b454';
  const BAD = '#ff7b7b';
  // Chart ink follows the theme (TASK-18): read from the page's colour variables before each page is drawn (light.css
  // sets them for the light theme); the dark values are the fallbacks.
  const CINK = {grid: '#1f2533', tick: '#9ba2b6', fg: '#eef0f6', card: '#141821', mut: '#b6bccb', ref: 'rgba(238,240,246,.6)'};
  function readInk() {
    const cs = getComputedStyle(document.documentElement);
    const v = (name, dflt) => cs.getPropertyValue(name).trim() || dflt;
    const light = document.documentElement.dataset.theme === 'light';
    Object.assign(CINK, {grid: v('--grid', '#1f2533'), tick: v('--dim', '#9ba2b6'), fg: v('--fg', '#eef0f6'), card: v('--card', '#141821'),
      mut: v('--mut', '#b6bccb'), ref: light ? 'rgba(21,25,34,.55)' : 'rgba(238,240,246,.6)'});
    if (chartsReady) {
      window.Chart.defaults.color = CINK.tick;
      window.Chart.defaults.borderColor = CINK.grid;
    }
  }
  const SC = {ok: OK, warn: WARN, over: BAD, nocap: ACC};
  const RGB = {acc: '142,168,255', ok: '79,209,165', warn: '245,180,84', bad: '255,123,123', purple: '199,146,234',
    cyan: '95,198,232', grey: '155,162,182'};

  function hue(s) {
    let x = 0;
    for (const ch of String(s || '?')) x = (x * 31 + ch.codePointAt(0)) % 360;
    return x;
  }
  const catColor = (name) => (has(PALETTE, name) ? PALETTE[name] : EXTRA[hue(name) % EXTRA.length]);

  function rgbOf(hex) {
    return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(',');
  }
  const rgba = (hex, a) => `rgba(${rgbOf(hex)},${a})`;

  // Status colours mean the same thing everywhere.
  const STATUS = {
    ok: {label: 'On track', pill: ''},
    warn: {label: 'Almost there', pill: 'warn'},
    over: {label: 'Over budget', pill: 'bad'},
    nocap: {label: 'No budget', pill: 'nt'},
  };

  function capStatus(spend, cap) {  // same rule as insights.cap_status (compared in cents)
    if (cap == null) return 'nocap';
    const s = Math.round(spend * 100);
    const c = Math.round(cap * 100);
    if (c <= 0) return s > 0 ? 'over' : 'ok';
    if (s >= c) return 'over';
    return s * 10 >= c * 8 ? 'warn' : 'ok';
  }

  // ---------- avatars, badges, rings, sparks ---------------------------------------------------
  const IMG_HOSTS = new Set(['plaid-merchant-logos.plaid.com', 'plaid-category-icons.plaid.com']);

  function safeImg(u) {
    if (typeof u !== 'string' || !u) return null;
    try {
      // the public demo's own invented merchant logos (t_4cb4d435): same origin, one fixed folder, nothing else
      if (/^\/api\/merchant-logos\/[a-z0-9-]+\.png$/.test(u)) return u;
      const url = new URL(u);
      const ok = url.protocol === 'https:' && IMG_HOSTS.has(url.hostname) && !url.port && !url.username && !url.password;
      return ok ? url.href : null;
    } catch (e) {
      return null;
    }
  }

  function initials(s) {
    const m = String(s || '').match(/[A-Za-z0-9]/);
    return m ? m[0].toUpperCase() : '?';
  }

  // Merchant logo, then category icon, then initials on a hue derived from the merchant name.
  function avatar(seed, logo, iconUrl, size = 32) {
    const el = h('span', {class: `av av${size}`, 'aria-hidden': 'true', style: {'--h': String(hue(seed))}}, initials(seed));
    const srcs = [safeImg(logo), safeImg(iconUrl)].filter(Boolean);
    if (srcs.length) {
      let k = 0;
      const img = h('img', {alt: '', decoding: 'async', referrerpolicy: 'no-referrer'});
      img.addEventListener('error', () => {
        k += 1;
        if (k < srcs.length) img.src = srcs[k];
        else img.remove();
      });
      img.src = srcs[0];
      el.append(img);
    }
    return el;
  }

  // Institution avatar: a brand-ish colour for banks we know, else a hue from the name.
  const INST = {
    'Chase': ['#2a6fc4', 'C'], 'Citibank Online': ['#1b86c9', 'Ci'], 'Citi': ['#1b86c9', 'Ci'],
    'Wells Fargo': ['#cf3b33', 'W'], 'U.S. Bank': ['#3f5dc8', 'U'], 'PayPal': ['#2a70e0', 'P'],
    'American Express': ['#2e77bc', 'A'], 'Capital One': ['#1f4f7d', 'C'],
    'Bank of America': ['#c8252f', 'B'], 'Fidelity': ['#3b8a3e', 'F'], 'Vanguard': ['#96151d', 'V'],
    'Charles Schwab': ['#1b9ad6', 'S'], 'Ally Bank': ['#6b2c91', 'A'], 'Discover': ['#e36a18', 'D'],
  };
  const instShort = (name) => (name === 'Citibank Online' ? 'Citi' : name || '');
  // Cached institutions (/api/accounts .institutions): a logo served by this server, else the bank's own colour.
  const INSTS = {byId: new Map(), byName: new Map()};
  function setInstitutions(list) {
    INSTS.byId = new Map((list || []).map((x) => [x.institution_id, x]));
    INSTS.byName = new Map((list || []).filter((x) => x.name).map((x) => [x.name, x]));
  }
  const COLOR_RE = /^#[0-9a-fA-F]{6}$/;
  // White initials need 4.5:1 on their bubble: a bank colour that's too light (#1d9fd0 gives 3:1) is darkened,
  // keeping its hue, until they do. `rgb` is [r, g, b] 0-255.
  function inkable(rgb) {
    const lin = (v) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
    let [r, g, b] = rgb;
    for (let k = 0; k < 40 && 1.05 / (0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b) + 0.05) < 4.5; k++) {
      r *= 0.94; g *= 0.94; b *= 0.94;
    }
    return `rgb(${Math.round(r)},${Math.round(g)},${Math.round(b)})`;
  }
  const hexRgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  function hslRgb(hh, sat, light) {  // hsl(h s% l%) -> [r, g, b]
    const a = sat * Math.min(light, 1 - light);
    const f = (n) => { const k = (n + hh / 30) % 12; return 255 * (light - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))); };
    return [f(0), f(8), f(4)];
  }
  // A bank's avatar: its logo on a light round chip, or its initials on its colour. `status` adds the dot.
  function ia(inst, size = '', status, id, cached) {
    const info = cached || (id && INSTS.byId.get(id)) || INSTS.byName.get(inst) || null;
    const known = INST[inst];
    const bg = inkable(info && COLOR_RE.test(info.color || '') ? hexRgb(info.color) : known ? hexRgb(known[0]) : hslRgb(hue(inst || '?'), 0.4, 0.4));
    const dot = () => (status ? h('span', {class: `st${status === 'ok' ? '' : ' bad'}`}) : null);
    const bubble = () => h('span', {class: `ia ${size}`, 'aria-hidden': 'true', style: {background: bg}}, known ? known[1] : initials(inst), dot());
    if (!info || !info.logo) return bubble();
    const img = h('img', {alt: '', decoding: 'async', src: `/api/institutions/${encodeURIComponent(info.institution_id)}/logo?v=${encodeURIComponent(info.v || '0')}`});
    const chip = h('span', {class: `ia logo ${size}`, 'aria-hidden': 'true'}, img, dot());
    img.addEventListener('error', () => chip.replaceWith(bubble()));
    return chip;
  }
  // Manual accounts: a logo only when the typed institution is exactly a cached bank's name (else null: the caller
  // shows the kind icon).
  function manualChip(name, size = '') {
    const info = name ? INSTS.byName.get(name) : null;
    return info ? ia(name, size, null, null, info) : null;
  }

  // A rounded square with an icon: categories, account groups, manual kinds.
  function ibadge(name, rgb, size = '') {
    return h('span', {class: `cb ${size}`, 'aria-hidden': 'true', style: {'--rgb': rgb}}, glyph(name));
  }
  const badge = (category, size = '') => ibadge(CATI[category] || 'tag', rgbOf(catColor(category)), size);
  const gbadge = (group, rgb, size = '') => ibadge(GROUP_ICON[group] || 'tag', rgb || RGB.grey, size);

  // A ring of arcs from 12 o'clock, each [ratio (0..1), colour, opacity], later ones drawn on top; `inner` in the middle.
  function ringOf(arcs, size, sw, inner, attrs = {}) {
    const r = (size - sw) / 2;
    const c = 2 * Math.PI * r;
    const m = size / 2;
    const bgc = sv('circle', {cx: m, cy: m, r, fill: 'none', stroke: 'rgba(255,255,255,.08)', 'stroke-width': sw});
    const arc = ([ratio, color, op]) => {
      const v = clamp(Number(ratio) || 0, 0, 1);
      return v > 0.003 ? sv('circle', {cx: m, cy: m, r, fill: 'none', stroke: color, 'stroke-opacity': op, 'stroke-width': sw,
        'stroke-linecap': 'round', 'stroke-dasharray': `${(c * v).toFixed(2)} ${c.toFixed(2)}`, transform: `rotate(-90 ${m} ${m})`}) : null;
    };
    return h('span', {...attrs, class: attrs.class ? `ring ${attrs.class}` : 'ring', style: {width: `${size}px`, height: `${size}px`}},
      sv('svg', {viewBox: `0 0 ${size} ${size}`, 'aria-hidden': 'true'}, bgc, ...arcs.map(arc)), h('span', {class: 'rin'}, inner));
  }
  const ring = (ratio, color, size = 44, sw = 5, ...inner) => ringOf([[ratio, color]], size, sw, inner);
  const ringPct = (ratio, color, size, sw, fs, label) => ring(ratio, color, size, sw,
    h('b', {class: 'v', style: {'font-size': `${fs}px`}}, `${Math.round(clamp(ratio, 0, 9.99) * 100)}%`), label ? h('small', {}, label) : null);

  // Every budget ring (Overview "Left to spend" and its Budgets rows, the Budgets summary, each category card) counts
  // what's committed: spent + the bills still due in it, the same sum as the status and "over budget once bills
  // clear". The % is committed / cap; the colour is capStatus(committed, cap), the one status rule (red at 100%, amber
  // at 80%). The spent part is drawn in it, the bills part right after in a lighter shade; past 100% the ring is full.
  // The small label reads "with bills" while any are due, else "used" (`used: false`: none then, as on the cards);
  // `inner` replaces the % (the Overview rows' icon); `of` names the cap in the aria-label (a suggestion's card).
  // data-ring-pct / data-ring-status are for the gates.
  // A budget's % as shown everywhere (rings, alerts): rounded, but never "100%" unless it's exactly at the cap
  // (Transport at 99.8% with $1.53 left read "100%"): 99 below it, 101 above.
  function ringPctOf(committed, cap) {
    const raw = (cap > 0 ? committed / cap : committed > 0 ? 1 : 0) * 100;
    return Math.round(raw) === 100 && raw !== 100 ? (raw < 100 ? 99 : 101) : Math.round(raw);
  }

  function budgetRing(spend, due, cap, size, sw, fs, {used = true, inner, of = 'budget'} = {}) {
    const committed = spend + due;
    const status = capStatus(committed, cap);
    const color = SC[status];
    const part = (x) => (cap > 0 ? x / cap : x > 0 ? 1 : 0);
    // Under the cap it never reads 100% (Transport: 99.8% with $1.53 left showed "100%" in amber), and over it never
    // reads 100% either: round, then keep 99 / 101 on the right side of the line.
    const pct = ringPctOf(committed, cap);
    const label = due > 0 ? 'with bills' : used ? 'used' : null;
    const aria = due > 0 ? `${pct}% of ${of} with bills still due: ${money(spend)} spent, ${money(due)} due, of ${money(cap)}`
      : `${pct}% of ${of} used: ${money(spend)} spent of ${money(cap)}`;
    const mid = inner || [h('b', {class: 'v', style: {'font-size': `${fs}px`}}, `${clamp(pct, 0, 999)}%`), label ? h('small', {}, label) : null];
    return ringOf(due > 0 ? [[part(committed), color, 0.45], [part(spend), color]] : [[part(spend), color]], size, sw, mid,
      {class: due > 0 && size < 80 && !inner ? 'snug' : null, role: 'img', 'aria-label': aria, dataset: {ringPct: String(pct), ringStatus: status}});
  }

  let gid = 0;
  function spark(values, color, w = 86, hgt = 30) {
    if (!values || values.length < 2) return null;
    const mn = Math.min(...values);
    const mx = Math.max(...values);
    const rg = mx - mn;
    const pts = values.map((v, i) => [2 + (i / (values.length - 1)) * (w - 5), rg ? hgt - 3 - ((v - mn) / rg) * (hgt - 6) : hgt / 2]);
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join('');
    const e = pts[pts.length - 1];
    const id = `sg${gid++}`;
    return sv('svg', {class: 'spark', width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, 'aria-hidden': 'true'},
      sv('defs', {}, sv('linearGradient', {id, x1: 0, y1: 0, x2: 0, y2: 1},
        sv('stop', {offset: 0, 'stop-color': color, 'stop-opacity': 0.32}), sv('stop', {offset: 1, 'stop-color': color, 'stop-opacity': 0}))),
      sv('path', {d: `${d}L${e[0].toFixed(1)} ${hgt}L2 ${hgt}Z`, fill: `url(#${id})`}),
      sv('path', {d, fill: 'none', stroke: color, 'stroke-width': 1.8, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}),
      sv('circle', {cx: e[0].toFixed(1), cy: e[1].toFixed(1), r: 2.6, fill: color}));
  }

  function sbars(values, color) {  // six small bars, the last one (this month) solid
    const mx = Math.max(...values.map((v) => Math.max(v, 0)), 1);
    return h('span', {class: 'sbars', title: 'Last 6 months', 'aria-hidden': 'true'},
      values.map((v, i) => h('i', {style: {height: `${Math.max(2, (Math.max(v, 0) / mx) * 26)}px`, background: color,
        opacity: i === values.length - 1 ? '1' : '.42'}})));
  }

  function cal(iso, cls = '') {
    const d = asDate(iso);
    return h('span', {class: `cal ${cls}`, 'aria-hidden': 'true'}, h('em', {}, d.toLocaleDateString(LOC, {month: 'short'})),
      h('b', {}, String(d.getDate())));
  }

  const pill = (cls, iconName, ...text) => h('span', {class: `pill ${cls}`}, iconName ? icon(iconName) : null, text);
  const tag = (text, cls = '', title) => h('span', {class: `tag ${cls}`, title}, text);
  // (TASK-18 review) an account name as shown: Plaid sometimes sends U+FFFD junk ("VISA\ufffd\ufffd CARD"), and a short
  // trailing "(PHEV)" / "(401k)" must not be left alone on the last line
  const acctName = (s) => String(s || '').replace(/\ufffd+/g, '').replace(/\s+/g, ' ').trim().replace(/ (\([^()]{1,12}\))$/, '\u00a0$1');
  // (TASK-20) an account's name (through acctName, after an optional lead-in) and its ••last-4 on one line, the
  // Accounts card's .nmrow pattern: only the name shortens with an ellipsis; the last-4 is never cut or pushed out
  const nameMask = (name, mask, {fallback = 'Account', pre = '', title} = {}) => h('span', {class: 'nmrow',
    title: title === undefined ? `${pre}${acctName(name) || fallback}${mask ? ` ••${mask}` : ''}` : title},
  h('span', {class: 'nm'}, `${pre}${acctName(name) || fallback}`), mask ? h('span', {class: 'mk'}, `••${mask}`) : null);
  const manualTag = () => tag('manual', '', 'Added by you');
  function staleTag(m) {
    return m.stale ? tag('stale', 'stale', `Balance is from ${fmtDate(m.balance_as_of, true)}. Update it any time.`) : null;
  }

  function track(ratio, color, label, cls = '', extra) {
    return h('div', {class: `track ${cls}`, role: 'progressbar', 'aria-label': label, 'aria-valuemin': '0', 'aria-valuemax': '100',
      'aria-valuenow': String(Math.round(clamp(ratio, 0, 1) * 100))},
    h('i', {style: {width: pct(ratio), background: color}}), extra);
  }

  // A budget row's thin bar: the same numbers, colours and aria-label as budgetRing (the numbers audit reads both alike).
  function hairline(spend, due, cap) {
    const committed = spend + due;
    const status = capStatus(committed, cap);
    const color = SC[status];
    const spentRatio = clamp(cap > 0 ? spend / cap : spend > 0 ? 1 : 0, 0, 1);
    const totalRatio = clamp(cap > 0 ? committed / cap : committed > 0 ? 1 : 0, 0, 1);
    const pctVal = ringPctOf(committed, cap);
    const aria = due > 0 ? `${pctVal}% of budget with bills still due: ${money(spend)} spent, ${money(due)} due, of ${money(cap)}`
      : `${pctVal}% of budget used: ${money(spend)} spent of ${money(cap)}`;
    return h('span', {class: 'hairline', role: 'img', 'aria-label': aria, dataset: {ringPct: String(pctVal), ringStatus: status}},
      due > 0 && totalRatio > 0.003 ? h('i', {dataset: {c: color, o: '0.45'}, style: {width: pct(totalRatio), background: rgba(color, '0.45')}}) : null,
      spentRatio > 0.003 ? h('i', {dataset: {c: color}, style: {width: pct(spentRatio), background: color}}) : null);
  }


  // ---------- API ------------------------------------------------------------------------------
  // Built-in login (TASK-18): a signed-out answer reloads the page (the server shows the sign-in), and every POST
  // carries the session's CSRF token from <meta name="csrf-token">.
  const signedOut = (r, j) => {
    if (r.status === 401 && j && j.signin) location.reload();
  };
  const csrf = () => (document.querySelector('meta[name=csrf-token]') || {}).content;

  async function api(path, params) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) {
      if (Array.isArray(v)) v.forEach((x) => qs.append(k, x));  // polish P1: a repeated parameter (?category=A&category=B)
      else if (v != null && v !== '') qs.set(k, v);
    }
    const url = qs.toString() ? `${path}?${qs}` : path;
    const r = await fetch(url, {headers: {Accept: 'application/json'}, cache: 'no-store'});
    const j = await r.json().catch(() => ({}));
    signedOut(r, j);
    if (!r.ok) {
      const err = new Error(j.error || `${r.status} ${r.statusText}`);
      err.field = j.field;  // (TASK-21: the Track as a bill preview shows it under its field)
      throw err;
    }
    return j;
  }

  async function post(url, body) {
    const headers = {'Content-Type': 'application/json'};
    if (csrf()) headers['X-CSRF-Token'] = csrf();
    const r = await fetch(url, {method: 'POST', headers, body: JSON.stringify(body || {})});
    const j = await r.json().catch(() => ({}));
    signedOut(r, j);
    if (!r.ok) {
      const err = new Error(j.error || r.statusText);
      err.field = j.field;
      err.retryAfter = j.retry_after;   // (TASK-22c: Sync now's minute between two syncs)
      err.status = r.status;            // (TASK-22f: 409 a sync already running, 403 refresh off)
      throw err;
    }
    return j;
  }

  // ---------- state ----------------------------------------------------------------------------
  const PAGES = {overview: 'Overview', transactions: 'Transactions', budgets: 'Budgets', recurring: 'Recurring',
    accounts: 'Accounts', investments: 'Investments', debt: 'Loans & debt', asset: 'Account'};
  const NAV_OF = {asset: 'accounts'};  // a car's / property's page sits under Accounts
  const MONTH_PAGES = new Set(['overview', 'transactions', 'budgets']);
  const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
  const KINDS = [['all', 'All'], ['review', 'Needs review'], ['spend', 'Spending'], ['income', 'Income'], ['transfers', 'Transfers']];
  const S = {month: thisMonth(), page: null, params: new URLSearchParams(), token: 0, charts: [],
    accounts: null, accountsP: null, categories: null, counts: null, debt: null, cameFrom: null};
  // Institutions that already have an Item: Link is closed early if one of them is picked.
  let CONNECTED_INSTITUTIONS = new Set();
  // The extension files (TASK-18: settings.js, setup.js, import.js; TASK-19: goals.js, txmeta.js, billcal.js,
  // rollover.js, alpaca.js) fill these in (see the end of this file); each default leaves the page exactly as it was.
  // addOptions: rows pushed onto the Add account chooser; addSteps: its extra steps by name.
  const HOOKS = {tiles: () => [], txTags: () => null, txCat: () => null, budgetLabel: () => null, budgetExtra: () => null,
    budgetNote: () => null,
    budgetsBody: () => [],  // polish P2A (budgets.js): the Budgets page under the summary
    capEditorExtra: () => null, acctRow: () => null, investRow: () => null, bankRow: () => null, addOptions: [], addSteps: {},
    billSheet: () => {},    // TASK-21 (bills.js): a Recurring row opens its bill sheet
    // polish P0 (catpick.js: the grouped picker): a transaction's category control; onPick() once `.value` is the new one
    catPick: ({value, names, cls = '', label, title}, onPick) => {
      const sel = h('select', {class: `cat${cls}`, 'aria-label': label, title: title || null},
        names.map((n) => h('option', {value: n, selected: n === value}, n)));
      sel.addEventListener('change', () => onPick(sel.value));
      return sel;
    },
    // polish P1 (txfilter.js: the Filters drawer, the chips of what's picked): the Transactions bar's controls after the
    // kind chips, and what follows each load of the list
    txBar: (f, banks) => {
      const bank = h('select', {class: 'bsel', 'aria-label': 'Bank'}, h('option', {value: ''}, 'All banks'),
        banks.map((b) => h('option', {value: b, selected: b === f.bank}, b)));
      bank.addEventListener('change', () => updateTx({bank: bank.value}));
      return [f.category.map((c) => h('button', {class: 'fchip cat', type: 'button', title: 'Clear category filter',
        onclick: () => updateTx({category: f.category.filter((x) => x !== c)}, true)}, c, icon('x'))), bank];
    },
    txLoaded: () => {}};

  // Explanations stay out of the main reading path. Short help supports hover/focus/tap;
  // lists use a labelled, scrollable non-modal popover with outside/Escape dismissal.
  let helpId = 0;
  let activeHelp = null;
  function closeHelp(returnFocus = false) {
    if (activeHelp) activeHelp.close(returnFocus);
  }
  function help(label, content, {key = '', long = false, text = ''} = {}) {
    const id = `hf-help-${++helpId}`;
    const button = h('button', {type: 'button', class: `help-trigger${text ? ' labelled' : ''}`,
      'aria-label': label, 'aria-expanded': 'false', 'aria-haspopup': long ? 'dialog' : null,
      dataset: key ? {help: key} : null}, text || icon('info'));
    let panel = null, pinned = false, timer = null, returning = false;
    const position = () => {
      if (!panel) return;
      const r = button.getBoundingClientRect();
      const p = {width: panel.offsetWidth, height: panel.offsetHeight};  // layout size: the pop-in's scale must not change it
      panel.style.left = `${Math.max(8, Math.min(r.left, innerWidth - p.width - 8))}px`;
      panel.style.top = `${Math.max(8, Math.min(r.bottom + 8, innerHeight - p.height - 8))}px`;
    };
    const cleanup = (restore = false) => {
      clearTimeout(timer);
      const old = panel;
      if (!old) return;
      const inside = old.contains(document.activeElement);
      panel = null;
      pinned = false;
      button.setAttribute('aria-expanded', 'false');
      button.removeAttribute('aria-controls');
      button.removeAttribute('aria-describedby');
      old.remove();
      window.removeEventListener('resize', position);
      window.removeEventListener('scroll', position, true);
      if (activeHelp && activeHelp.button === button) activeHelp = null;
      if ((restore || inside) && button.isConnected) {
        returning = true;
        button.focus({preventScroll: true});
        returning = false;
      }
    };
    const close = (restore = false) => { if (panel) { panel.hidePopover(); cleanup(restore); } };
    const open = (pin = false) => {
      clearTimeout(timer);
      if (panel) { pinned ||= pin; return; }
      closeHelp();
      pinned = pin;
      panel = h('div', {id, class: `help-panel${long ? ' detail' : ''}`, popover: 'manual',
        role: long ? 'dialog' : 'tooltip', 'aria-label': long ? label : null},
      long ? h('div', {class: 'help-head'}, h('b', {}, label), h('button', {type: 'button', class: 'ib',
        'aria-label': 'Close details', onclick: () => close(true)}, icon('x'))) : null,
      h('div', {class: 'help-content'}, typeof content === 'function' ? content() : content));
      document.body.append(panel);
      button.setAttribute('aria-expanded', 'true');
      button.setAttribute(long ? 'aria-controls' : 'aria-describedby', id);
      activeHelp = {button, close, contains: (node) => button.contains(node) || Boolean(panel && panel.contains(node))};
      panel.addEventListener('toggle', (e) => { if (e.target === panel && e.newState === 'closed') cleanup(); });
      panel.addEventListener('pointerenter', () => clearTimeout(timer));
      panel.addEventListener('pointerleave', () => { if (!pinned && document.activeElement !== button) timer = setTimeout(close, 150); });
      panel.showPopover();
      position();
      window.addEventListener('resize', position);
      window.addEventListener('scroll', position, true);
      if (long) panel.querySelector('button').focus({preventScroll: true});
    };
    button.addEventListener('click', (e) => { e.stopPropagation(); if (panel && pinned) close(); else open(true); });
    if (!long) {
      button.addEventListener('pointerenter', () => { if (matchMedia('(hover: hover) and (pointer: fine)').matches) open(); });
      button.addEventListener('pointerleave', () => { if (!pinned && document.activeElement !== button) timer = setTimeout(close, 150); });
      button.addEventListener('focus', () => { if (!returning) open(); });
      button.addEventListener('blur', () => { if (!pinned) close(); });
    }
    return button;
  }
  document.addEventListener('pointerdown', (e) => {
    if (activeHelp && !activeHelp.contains(e.target)) closeHelp();
  }, true);
  document.addEventListener('focusin', (e) => {
    if (activeHelp && !activeHelp.contains(e.target)) closeHelp();
  });
  // Escape closes only the explanation, not an underlying sheet.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && activeHelp) { e.preventDefault(); e.stopImmediatePropagation(); closeHelp(true); }
  }, true);
  window.addEventListener('hashchange', () => closeHelp());
  const helpBills = (bills) => h('div', {class: 'help-bills'}, bills.map((b) => h('div', {class: 'help-bill'},
    h('span', {}, b.merchant, b.next_expected ? h('small', {}, fmtDate(b.next_expected, true)) : null),
    h('b', {class: 'v'}, `${b.variable ? '~' : ''}${money2(b.amount)}${b.variable ? ' est.' : ''}`))));

  // ---------- net worth mode (TASK-14, TASK-15) --------------------------------------------------
  // "All" (the default) or "Custom": every account except the ones switched off on the Accounts card's Edit sheet. That
  // choice lives on the server (the settings table's networth_excluded), so the phone, the laptop and `finance
  // networth --custom` agree. The mode is one setting for the whole app, remembered on this device (localStorage
  // hf.networth.mode: all | custom; TASK-14's "cash" reads as custom, whose first selection is that one). Flipping it
  // redraws in place the sidebar's figure and the net worth views on the page (the Overview tile, the Accounts card);
  // nothing is refetched. <html data-nw> lets CSS show the "Not counted" tags in Custom.
  const NW_KEY = 'hf.networth.mode';
  const NW = {mode: 'all', views: new Map(), out: new Set()};  // views: slot -> [element, redraw]; out: the ids left out
  try {
    const stored = localStorage.getItem(NW_KEY);
    if (stored === 'custom' || stored === 'cash') NW.mode = 'custom';
    if (stored === 'cash') localStorage.setItem(NW_KEY, 'custom');
  } catch (e) { /* storage blocked: All */ }
  document.documentElement.dataset.nw = NW.mode;
  const customMode = () => NW.mode === 'custom';
  const worthOf = (nw) => (customMode() ? nw.custom : nw);  // {assets, debts, net} in the mode
  // TASK-22: a card payment that left the bank but the card hasn't posted yet is counted as paid in the net (the figure
  // is the counted-once one); each one says so under Own / Owe. TASK-22f (MOCK-022): "$994.42 Chase payment in transit ·
  // counted once" (the sidebar, the Net worth and Loans & debt tiles, the Custom sheet's preview); on Accounts in a box
  // with where it left from and what net worth would read counted twice. Custom: only to a card it counts.
  const transitLine = (e) => h('p', {class: 'trl transit', dataset: {transit: e.transaction_id}}, icon('swap'),
    h('span', {}, h('b', {class: 'v'}, money2(e.amount)), ` ${instShort(e.institution) || 'Card'} payment in transit \u00b7 counted once`));
  const transitShown = (nw) => (nw.in_transit || []).filter((e) => !customMode() || e.counted_custom);
  const transitLines = (nw) => transitShown(nw).map(transitLine);
  const cardLabel = (e) => `${instShort(e.institution) || acctName(e.card) || 'The card'}${e.mask ? `\u00a0\u2022\u2022${e.mask}` : ''}`;
  const transitBoxes = (nw) => transitShown(nw).map((e) => {
    const why = help('About this payment', () => h('p', {},
      `Left ${acctName(e.from_account.name) || 'your bank account'}${e.from_account.mask ? `\u00a0\u2022\u2022${e.from_account.mask}` : ''} `
      + `on ${fmtDate(e.date)}; ${cardLabel(e)} still shows it owed. Counted twice, net worth would read ${money(worthOf(nw).net - e.amount)}.`));
    return h('div', {class: 'trbox', style: {display: 'flex', 'align-items': 'flex-start', gap: '10px'}}, h('div', {style: {flex: '1'}}, transitLine(e)), why);
  });
  const manualNwId = (id) => `manual:${id}`;
  const leftOut = (id) => NW.out.has(id);
  // An Accounts group in the mode: 'on' (all of it counts), 'part' (some of its accounts) or 'off' (none).
  function nwState(g) {
    if (!customMode() || !g.count) return 'on';
    return !g.custom.count ? 'off' : g.custom.count < g.count ? 'part' : 'on';
  }
  const notCounted = () => tag('Not counted', 'nct', 'Not counted in the Custom net worth');  // shown in Custom only
  function ownOwe(w) {  // the Own / Owe bar's two parts (negative assets show no green)
    const own = Math.max(w.assets, 0);
    const owe = Math.max(w.debts, 0);
    const tot = own + owe || 1;
    return [h('i', {style: {width: pct(own / tot), background: 'var(--ok)'}}), h('i', {style: {width: pct(owe / tot), background: 'var(--bad)'}})];
  }
  function nwSeg() {  // All · Custom. On the Overview tile (a link to Accounts) a tap on it, buttons or the space
    // between them, flips the mode and never navigates. A tap on Custom (already on or not) also opens the Custom net
    // worth sheet: it's the one way in to editing what Custom counts (2026-09-27 review: no separate Edit button).
    return h('div', {class: 'seg nwseg', role: 'group', 'aria-label': 'Count in net worth', onclick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      const b = e.target.closest('button[data-nw]');
      if (!b) return;
      setNwMode(b.dataset.nw);
      if (b.dataset.nw === 'custom') openWorthSheet();
    }}, [['all', 'All'], ['custom', 'Custom']].map(([k, label]) => h('button', {type: 'button',
      'aria-pressed': String(NW.mode === k), dataset: {nw: k}}, label)));
  }
  // From the Custom chip: the page is in Custom behind the sheet and stays there however the sheet closes; Cancel only
  // throws away switch changes.
  async function openWorthSheet() {  // the sheet needs the accounts payload; the Overview may not have it yet
    const d = S.accounts || await loadAccounts();
    if (d) worthSheet(d);
  }
  function nwView(slot, el, redraw) {  // redraw() now, and again whenever the mode flips while el is on the page
    NW.views.set(slot, [el, redraw]);
    redraw();
    return el;
  }
  function rememberNwMode(mode) {
    NW.mode = mode;
    document.documentElement.dataset.nw = mode;
    try {
      localStorage.setItem(NW_KEY, mode);
    } catch (e) { /* not remembered on this device */ }
    document.querySelectorAll('.nwseg button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.nw === mode)));
  }
  function setNwMode(mode) {
    if (mode === NW.mode) return;
    rememberNwMode(mode);
    if (S.accounts) renderSideWorth(S.accounts);
    for (const [slot, [el, redraw]] of NW.views) {
      if (el.isConnected) redraw();
      else NW.views.delete(slot);
    }
  }

  // ---------- messages, toast, modal -----------------------------------------------------------
  function show(text, kind) {
    const m = $('msg');
    m.textContent = text;
    m.className = kind;
  }

  let toastTimer = null;
  function toast(content, actions = [], opts = {}) {
    const t = $('toast');
    clearTimeout(toastTimer);
    fill(t, h('span', {class: 'tm'}, content),
      actions.map((a) => h('button', {type: 'button', class: a.primary ? 'y' : 'n', onclick: a.onclick}, a.label)));
    t.className = `toast on${opts.error ? ' err' : ''}`;
    if (opts.timeout) toastTimer = setTimeout(hideToast, opts.timeout);
  }
  function hideToast() {
    $('toast').className = 'toast';
  }

  // The modal sheet: Add account steps, forms, confirmations. Focus stays inside it while it's open.
  let modalReturn = null;
  function openSheet(kids, opts = {}) {
    closeHelp();
    const sheet = $('sheet');
    const stale = sheetRelease;
    sheetRelease = opts.release || null;
    if (stale) stale();  // a sheet replacing another lets the old one clean up first (its hook is dropped below)
    sheetOnClose = opts.onClose || null;  // a sheet replacing another drops the old one's hook
    if (!$('modal').classList.contains('on')) modalReturn = document.activeElement;
    sheet.className = `sheet${opts.narrow ? ' narrow' : ''}${opts.cls ? ` ${opts.cls}` : ''}`;
    fill(sheet, kids);
    $('modal').classList.add('on');
    document.body.classList.remove('drawer');
    sheet.scrollTop = 0;
    const first = opts.focus || sheet.querySelector('input, [aria-checked=true], button:not([disabled]):not(.ib)') ||
      sheet.querySelector('button:not([disabled])');
    if (first) first.focus();
  }
  let sheetOnClose = null;  // run once when the open sheet closes (×, Escape, the backdrop, or code)
  let sheetRelease = null;  // an open sheet's clean-up (listeners, inline styles): run when it closes or another replaces it
  function closeModal() {
    $('modal').classList.remove('on');
    const release = sheetRelease;
    sheetRelease = null;
    if (release) release();
    const done = sheetOnClose;
    sheetOnClose = null;
    if (done) done();
    if (modalReturn && modalReturn.isConnected && modalReturn.focus) modalReturn.focus();
    modalReturn = null;
  }
  function modal(title, body, actions) {  // a simple question: title, body, buttons
    openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, title), closeBtn()), body,
      h('div', {class: 'row'}, actions.map((a) => h('button', {type: 'button', class: `btn${a.primary ? '' : ' ghost'}`,
        disabled: a.disabled, onclick: a.onclick}, a.label)))], {narrow: true});
  }
  const closeBtn = () => h('button', {class: 'ib', type: 'button', 'aria-label': 'Close', onclick: closeModal}, icon('x'));
  const backBtn = (onclick) => h('button', {class: 'ib', type: 'button', 'aria-label': 'Back', onclick}, icon('arrowl'));

  function trapFocus(e, root) {
    if (e.key !== 'Tab') return;
    const els = [...root.querySelectorAll('button, input, select, a[href], [tabindex]:not([tabindex="-1"])')]
      .filter((el) => !el.disabled && el.offsetParent !== null);
    if (!els.length) return;
    const first = els[0];
    const last = els[els.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  // ---------- building blocks ------------------------------------------------------------------
  // A card with a sentence-case title behind an icon badge: card({cls, title, icon, rgb, extra}, ...kids).
  function card(o, ...kids) {
    // `o.help` is an explanation trigger (or several) that belongs in the card header, right after the title. A card that
    // is itself a link never takes one: a trigger must not be nested inside an <a> (TASK-20 accessibility rule).
    const header = o.title != null ? h('h3', {},
      o.icon ? h('span', {class: 'hi', style: {'--rgb': o.rgb || RGB.acc}}, glyph(o.icon)) : null,
      h('span', {class: 'ttl'}, o.title), o.href ? null : o.help, o.extra) : null;
    return h(o.href ? 'a' : 'section', {class: `card ${o.cls || ''}`, href: o.href}, header, kids);
  }
  const small = (text) => h('small', {}, text);
  // An empty state (checked by the layout gate: [data-empty]). As a chart's stand-in (emptyChart) it's marked so the
  // packer doesn't stretch its card far, and in a .grow card it takes the stretch itself, the message staying centred.
  const msgBox = (title, text) => h('div', {class: 'msgbox'}, h('div', {dataset: {empty: ''}}, h('b', {}, title), text));
  const emptyChart = (title, text) => {
    const box = msgBox(title, text);
    box.dataset.emptyChart = '';
    return box;
  };
  const emptyNote = (iconName, title, text, rgb = RGB.ok) => h('div', {class: 'empty'}, ibadge(iconName, rgb),
    h('div', {}, h('b', {}, title), text));
  const skel = (cls) => h('span', {class: `skel ${cls}`});
  const chartBox = (cls, label) => {
    const canvas = h('canvas', {role: 'img', 'aria-label': label});
    return [h('div', {class: `cw ${cls}`}, canvas), canvas];
  };
  const lnk = (href, text) => h('a', {class: 'lnk', href}, text, icon('chevr'));
  function ghostRow(iconName, rgb, title, text, onclick, data) {
    return h('button', {class: 'ghost-row', type: 'button', onclick, dataset: data}, ibadge(iconName, rgb, 's'),
      h('span', {class: 'grow'}, h('b', {}, title), h('span', {}, text)), icon('chevr'));
  }

  function skeleton(layout) {
    const cards = layout.map((cls) => h('div', {class: cls}, skel('sk-card' + (cls.includes('s2') ? ' tall' : ''))));
    return h('div', {class: 'grid', 'aria-busy': 'true', 'aria-label': 'Loading'}, cards);
  }

  function errorCard(err, retry) {
    return card({cls: 'errcard', title: 'This page could not load', icon: 'alert', rgb: RGB.bad},
      h('p', {}, `${err.message}. Nothing was changed. Check that finance serve is still running, then try again.`),
      h('button', {class: 'btn sm', type: 'button', onclick: retry}, 'Try again'));
  }

  const reconnectBtn = (itemId) => h('button', {class: 'rc', type: 'button', 'data-item': itemId}, 'Reconnect');
  // A healthy bank: the same update-mode Link (with account selection) as Reconnect, so a card or account opened later
  // joins the connection it already has. Never a new Item, so it uses no Plaid slot.
  const updateBtn = (itemId, name) => h('button', {class: 'btn sm ghost upd', type: 'button', 'data-item': itemId,
    'aria-label': `Update ${name} accounts`}, 'Update');

  // ---------- card packing ---------------------------------------------------------------------
  // Cards marked .grow have a chart that takes up any extra height (.fillh), so they stretch like list cards.
  // Multi-column card grids are packed like masonry so short cards don't leave holes. The grid gets
  // 4px rows (.grid.pk) and every card is placed on them explicitly, spanning its measured height plus
  // the 16px gutter; s2/s3 cards keep their width. DOM order stays the reading order: a card never
  // starts above the card before it, and cards starting on the same line run left to right. Among
  // those layouts the search keeps the one with no side-by-side tops 4-12px apart (a near miss looks like a
  // mistake) and no hole over 24px, then the least ragged bottom edge, then the shortest. Every card then
  // stretches down to the card below it, so the gap between cards is always the gutter (TASK-11 layout gate), and
  // to the bottom edge when nothing is below it. A chart card's canvas doesn't grow (nor does an empty chart area,
  // [data-empty-chart]), so stretching one more than 40px counts like a hole: layouts that need it lose to ones that
  // don't (at the bottom edge it stops at 40px).
  // data-pin="start"/"end" keeps a card in the first/last column, data-flex lets the packer pick the
  // card's width, and data-wide is the order on wider screens (phones keep the order as rendered).
  // Re-packing, batched in requestAnimationFrame: one ResizeObserver on the cards (a stretched card is at least
  // its area's height, never pinned to it, so content that grows makes the box grow), and one MutationObserver
  // on each grid for any change inside a card (an editor swapped in, "See suggestions", an inline error, a group
  // opened), which also catches content that shrinks.
  const PACK = {unit: 4, gutter: 16, hole: 24, chartStretch: 40, budget: 20000};

  // items: [{spans: [{k, h}], chart, pin}] in reading order -> [{c, k, top, rows}] in 4px rows, or null.
  function packLayout(items, cols) {
    const {unit: U, gutter: G, hole: HOLE, chartStretch} = PACK;
    const tol = Math.ceil(HOLE / U);
    const n = items.length;
    const opts = items.map((it) => {
      const out = [];
      for (const s of it.spans) {
        const k = Math.min(s.k, cols);
        const rows = Math.ceil((s.h + G) / U - 1e-6);
        for (let c = 0; c + k <= cols; c++) {
          if ((it.pin === 'start' && c > 0) || (it.pin === 'end' && c + k < cols)) continue;
          out.push({c, k, h: s.h, rows});
        }
      }
      return out;
    });
    const rest = new Array(n + 1).fill(0);  // least content (row-columns) still to place from item i on
    for (let i = n - 1; i >= 0; i--) rest[i] = rest[i + 1] + Math.min(...opts[i].map((o) => o.rows * o.k));
    const bottom = new Array(cols).fill(0);
    const pos = new Array(n);
    let best = null;
    let budget = PACK.budget;

    function score() {
      const T = Math.max(...bottom);
      const below = pos.map((p) => new Array(p.k).fill(T));  // per column: top of the next card down, or T
      const last = new Array(cols).fill(-1);
      const first = new Array(cols).fill(T);  // top of each column's first card (T: an empty column)
      pos.forEach((p, i) => {
        for (let j = p.c; j < p.c + p.k; j++) {
          if (last[j] >= 0) below[last[j]][j - pos[last[j]].c] = p.top;
          else first[j] = p.top;
          last[j] = i;
        }
      });
      let excess = Math.max(0, Math.max(...first) * U - HOLE);
      let edge = 0;  // a ragged bottom edge is masonry: it only breaks ties between layouts without holes
      let stretch = 0;
      let order = 0;
      const area = pos.map((p, i) => {
        const room = Math.min(...below[i]) - p.top;  // down to the next card in its columns: the gap is the gutter
        const last = below[i].every((b) => b === T);  // nothing below it: stretch to the bottom edge (charts 40px)
        const cap = Math.max(p.rows, Math.floor((p.h + G + chartStretch) / U));
        const a = last && items[i].chart ? Math.min(room, cap) : room;
        for (const b of below[i]) {  // a hole between cards counts first; space at the grid's bottom edge only after
          if (b === T) edge = Math.max(edge, (b - p.top - a) * U - HOLE);
          else excess = Math.max(excess, (b - p.top - a) * U - HOLE);
        }
        if (items[i].chart && a > cap) excess = Math.max(excess, (a - cap) * U);  // an empty stretch under a chart
        stretch += (a * U - G - p.h) * p.k;
        order += p.top * cols + p.c;
        return a;
      });
      let ragged = 0;  // side-by-side cards whose tops miss each other by 4-12px look like a mistake: never, if avoidable
      for (let a = 0; a < n; a++) {
        for (let b = a + 1; b < n; b++) {
          const d = Math.abs(pos[a].top - pos[b].top) * U;
          if (d > 0 && d <= 12) ragged += 1;
        }
      }
      return {key: [ragged * 1e6 + excess, Math.max(edge, 0), T, Math.round(stretch), order], area};
    }

    const better = (a, b) => {
      for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i];
      return false;
    };

    function dfs(i, floor, prev) {
      if (budget-- <= 0 && best) return;
      if (i === n) {
        const s = score();
        if (!best || better(s.key, best.key)) best = {key: s.key, pos: pos.map((p, j) => ({c: p.c, k: p.k, top: p.top, rows: s.area[j]}))};
        return;
      }
      if (best && best.key[0] <= 0 && best.key[1] <= 0) {  // nothing yet to beat but height: stop once this branch can't be shorter
        const used = bottom.reduce((sum, b) => sum + b, 0);
        if (Math.max(...bottom, Math.ceil((used + rest[i]) / cols)) > best.key[2]) return;
      }
      // A card starts where its columns end, or lower to line up with another column's end or with the
      // card before it (the card above then stretches into the space, when it may).
      const cand = [];
      for (const o of opts[i]) {
        const base = Math.max(...bottom.slice(o.c, o.c + o.k));
        for (const top of new Set([base, floor, ...bottom])) {
          if (top < base || top < floor - tol || (prev && Math.abs(top - prev.top) <= tol && o.c < prev.c)) continue;
          cand.push({...o, top});
        }
      }
      cand.sort((a, b) => a.top - b.top || a.c - b.c);
      for (const o of cand) {
        const saved = bottom.slice(o.c, o.c + o.k);
        pos[i] = o;
        bottom.fill(o.top + o.rows, o.c, o.c + o.k);
        dfs(i + 1, Math.max(floor, o.top), o);
        saved.forEach((v, j) => { bottom[o.c + j] = v; });
      }
    }

    dfs(0, 0, null);
    return best && best.pos;
  }

  const PK = {grids: new Set(), base: new WeakMap(), placed: new WeakMap(), memo: new WeakMap(), seen: new WeakSet(),
    ro: null, mo: null, frame: 0, fresh: false, streak: 0, at: 0, vp: ''};
  const WATCH = {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['class', 'hidden', 'open']};

  function repack(content) {
    if (content) PK.fresh = true;  // a real change, not the packer re-triggering itself
    if (!PK.frame) PK.frame = requestAnimationFrame(packAll);
  }

  // The packer's own writes (card order, the .pk / .fill classes) land on the grid and its cards: not content.
  function contentChanged(records) {
    return records.some((r) => {
      const el = r.target.nodeType === 1 ? r.target : r.target.parentElement;
      return el && !PK.grids.has(el) && !PK.grids.has(el.parentElement) && !el.closest('canvas');
    });
  }

  function pack(grid) {
    if (!PK.ro && window.ResizeObserver) PK.ro = new ResizeObserver(() => repack(false));
    if (!PK.mo && window.MutationObserver) PK.mo = new MutationObserver((records) => { if (contentChanged(records)) repack(true); });
    PK.grids.add(grid);
    PK.base.set(grid, [...grid.children]);
    if (PK.mo) PK.mo.observe(grid, WATCH);
    PK.fresh = true;
    packAll();  // before the first paint, so the unpacked grid never shows
  }

  function forgetGrid(grid) {
    for (const el of PK.base.get(grid) || []) {
      if (PK.ro) PK.ro.unobserve(el);
      PK.seen.delete(el);
    }
    PK.grids.delete(grid);
    if (PK.mo) {  // a MutationObserver can only stop watching everything: watch the grids still on the page again
      PK.mo.disconnect();
      for (const g of PK.grids) PK.mo.observe(g, WATCH);
    }
  }

  function orderCards(grid, wide) {  // phones: the order as rendered; wider screens: data-wide
    let base = PK.base.get(grid);
    if (base.length !== grid.children.length || base.some((el) => el.parentNode !== grid)) {
      base = [...grid.children];
      PK.base.set(grid, base);
    }
    const key = (el) => (el.dataset.wide != null ? Number(el.dataset.wide) : base.indexOf(el));
    const want = wide ? base.slice().sort((a, b) => key(a) - key(b)) : base;
    const active = document.activeElement;
    want.forEach((el, i) => { if (grid.children[i] !== el) grid.insertBefore(el, grid.children[i]); });
    if (active && active !== document.activeElement && active.isConnected) active.focus({preventScroll: true});
  }

  function unpackGrid(grid, els) {
    grid.classList.remove('pk');
    for (const el of els) {
      el.style.removeProperty('grid-column');
      el.style.removeProperty('grid-row');
      el.classList.remove('fill');
      PK.placed.delete(el);
    }
  }

  // Cards the packer may stretch only 40px (PACK.chartStretch): a chart card, whose canvas doesn't grow (a .grow card's
  // chart does fill extra height, so it stretches like a list card), and a card showing an empty chart area
  // ([data-empty-chart], TASK-12b): stretched to a tall neighbour, it would carry a tall blank box again.
  function packsLikeChart(el) {
    return (Boolean(el.querySelector('canvas')) && !el.classList.contains('grow')) || Boolean(el.querySelector('[data-empty-chart]'));
  }

  function packAll() {
    PK.frame = 0;
    const now = performance.now();
    const vp = `${innerWidth}x${innerHeight}`;
    // Re-packing that keeps re-triggering itself without the window changing (a scrollbar appearing and
    // disappearing) stops after a few rounds; it starts again after a second of quiet, or on a real change.
    PK.streak = !PK.fresh && vp === PK.vp && now - PK.at < 1000 ? PK.streak + 1 : 0;
    PK.fresh = false;
    PK.at = now;
    PK.vp = vp;
    if (PK.streak > 8) return;
    const jobs = [];
    for (const grid of PK.grids) {
      if (grid.isConnected) jobs.push({grid, cols: parseInt(getComputedStyle(grid).getPropertyValue('--cols'), 10) || 1});
      else forgetGrid(grid);
    }
    // 1. writes: card order for this width, and no stretching so natural heights can be read
    for (const j of jobs) {
      orderCards(j.grid, j.cols > 1);
      j.els = [...j.grid.children];
      for (const el of j.els) {
        if (PK.ro && !PK.seen.has(el)) {
          PK.seen.add(el);
          PK.ro.observe(el);
        }
      }
      if (j.cols > 1) j.els.forEach((el) => el.classList.remove('fill'));
      else unpackGrid(j.grid, j.els);
    }
    const wide = jobs.filter((j) => j.cols > 1);
    // 2. reads: every card's height in one layout
    for (const j of wide) {
      j.items = j.els.map((el) => {
        const span = parseInt(getComputedStyle(el).getPropertyValue('--span'), 10);  // CSS may widen a card per width
        const k = Math.min(span || (el.classList.contains('s3') ? 3 : el.classList.contains('s2') ? 2 : 1), j.cols);
        return {el, spans: [{k, h: el.getBoundingClientRect().height}], chart: packsLikeChart(el), pin: el.dataset.pin || null};
      });
    }
    // 3. cards that may take any width are measured at each one (one layout per width)
    for (const j of wide) {
      for (const it of j.items) {
        if (it.el.dataset.flex == null) continue;
        it.spans = [];
        for (let k = 1; k <= j.cols; k++) {
          it.el.style.gridColumn = `1 / span ${k}`;
          it.spans.push({k, h: it.el.getBoundingClientRect().height});
        }
        PK.placed.delete(it.el);
      }
    }
    // 4. place (the search is skipped when nothing changed by a whole row) and write
    for (const j of wide) {
      const memo = `${j.cols}|${j.items.map((it) => `${it.pin}${it.chart ? 'c' : ''}:${it.spans.map((s) => `${s.k}x${Math.ceil(s.h / PACK.unit)}`)}`)}`;
      const prev = PK.memo.get(j.grid);
      const layout = prev && prev.memo === memo ? prev.layout : packLayout(j.items, j.cols);
      PK.memo.set(j.grid, {memo, layout});
      if (!layout) {  // no layout keeps the reading order: leave it to the plain CSS grid
        unpackGrid(j.grid, j.els);
        continue;
      }
      j.grid.classList.add('pk');
      layout.forEach((p, i) => {
        const el = j.els[i];
        const col = `${p.c + 1} / span ${p.k}`;
        const row = `${p.top + 1} / span ${p.rows - PACK.gutter / PACK.unit}`;  // the gutter rows stay empty
        if (PK.placed.get(el) !== col + row) {
          el.style.gridColumn = col;
          el.style.gridRow = row;
          PK.placed.set(el, col + row);
        }
        el.classList.add('fill');
      });
    }
    if (PK.mo) PK.mo.takeRecords();  // this pass's own writes
  }

  // ---------- charts ---------------------------------------------------------------------------
  let chartsReady = false;

  function setupCharts() {
    if (chartsReady) return true;
    const C = window.Chart;
    if (!C) return false;
    C.defaults.color = CINK.tick;
    C.defaults.borderColor = CINK.grid;
    C.defaults.font.family = '"Inter", system-ui, sans-serif';
    C.defaults.font.size = 12.5;
    C.defaults.responsive = true;
    C.defaults.maintainAspectRatio = false;
    // (t_acbcd9f4: no global `C.defaults.animation = false`: Chart.js resolves every animation's settings through that
    // default, so a chart's own animation could never turn back on. chart() sets it per chart instead)
    Object.assign(C.defaults.plugins.tooltip, {backgroundColor: '#1d2331', borderColor: '#333c53', borderWidth: 1, padding: 11,
      bodyAlign: 'right', footerAlign: 'right', titleColor: '#eef0f6', bodyColor: '#d3d8e2', footerColor: '#9ba2b6', boxPadding: 4, usePointStyle: true,
      titleFont: {weight: '600', size: 13}, bodyFont: {size: 13}, footerFont: {size: 12.5, weight: '500'}});
    Object.assign(C.defaults.plugins.legend.labels, {boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: 'rectRounded',
      padding: 16, font: {size: 13}});
    chartsReady = true;
    return true;
  }

  function chart(canvas, config) {
    if (!setupCharts()) {
      canvas.parentElement.replaceWith(msgBox('Chart unavailable', 'The chart library did not load. Reload the page.'));
      return null;
    }
    // t_acbcd9f4: a chart grows in (400ms) after a page or month switch; a refresh in place and reduced motion draw it
    // still, and hover and tooltips are always instant
    config.options = {...config.options, animation: S.animate ? {duration: 400, easing: 'easeOutCubic'} : false,
      transitions: {active: {animation: {duration: 0}}}};
    const c = new window.Chart(canvas, config);
    S.charts.push(c);
    // a chart is built before its canvas is on the page, so its first draw is a resize (never animated): once it's
    // attached, put it back at the baseline and grow it in
    if (S.animate) requestAnimationFrame(() => { if (S.charts.includes(c) && c.canvas && c.canvas.isConnected) { c.reset(); c.update(); } });
    return c;
  }

  function destroyCharts(scope) {
    const keep = [];
    S.charts.forEach((c) => {
      if (!scope || scope.contains(c.canvas)) c.destroy();
      else keep.push(c);
    });
    S.charts = keep;
  }

  const yMoney = (extra) => ({beginAtZero: true, ticks: {maxTicksLimit: 5, callback: axisMoney}, grid: {color: CINK.grid},
    border: {display: false}, ...extra});
  const FONT = '"Inter", system-ui, sans-serif';
  const gradFill = (rgb, top = 0.34) => (ctx) => {
    const {ctx: c, chartArea: a} = ctx.chart;
    if (!a) return `rgba(${rgb},.2)`;
    const g = c.createLinearGradient(0, a.top, 0, a.bottom);
    g.addColorStop(0, `rgba(${rgb},${top})`);
    g.addColorStop(1, `rgba(${rgb},0)`);
    return g;
  };

  // Cumulative spend: this month (to today) vs last month, x = day 1..days in month.
  function cumChart(canvas, s) {
    const n = Math.max(s.days_in_month, s.cum_prev.length);
    const labels = Array.from({length: n}, (_, i) => i + 1);
    const cur = s.cum;
    const prev = s.cum_prev;
    const last = cur.length - 1;
    const pm = monthName(s.prev_month);
    const todayMarker = {
      id: 'today',
      afterDatasetsDraw(ch) {
        if (!s.is_current || last < 0 || !ch.isDatasetVisible(0)) return;
        const p = ch.getDatasetMeta(0).data[last];
        if (!p) return;
        const {ctx, chartArea: a} = ch;
        ctx.save();
        ctx.strokeStyle = 'rgba(142,168,255,.5)';
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 4]);
        ctx.beginPath();
        ctx.moveTo(p.x, a.top + 20);
        ctx.lineTo(p.x, a.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = CINK.fg;
        ctx.font = `600 13px ${FONT}`;
        const right = p.x > a.right - 130;
        ctx.textAlign = right ? 'right' : 'left';
        ctx.fillText(`Today · ${money(s.mtd)}`, p.x + (right ? -8 : 8), a.top + 13);
        ctx.restore();
      },
    };
    chart(canvas, {
      type: 'line',
      plugins: [todayMarker],
      data: {labels, datasets: [
        {label: monthName(s.month), data: cur, borderColor: ACC, borderWidth: 2.8, fill: 'origin', backgroundColor: gradFill(RGB.acc),
          tension: 0.25, pointRadius: (c) => (s.is_current && c.dataIndex === last ? 6 : 0), pointBackgroundColor: ACC,
          pointBorderColor: CINK.card, pointBorderWidth: 3, pointHoverRadius: 5},
        {label: pm, data: prev, borderColor: '#7d869c', borderWidth: 2, borderDash: [5, 5], fill: false, tension: 0.25,
          pointRadius: 0, pointHoverRadius: 4},
      ]},
      options: {
        interaction: {mode: 'index', intersect: false},
        layout: {padding: {top: 4}},
        scales: {
          x: {grid: {display: false}, border: {display: false},
            ticks: {autoSkip: false, maxRotation: 0, minRotation: 0, callback: (v, i) => (i === 0 || (i + 1) % 5 === 0 ? labels[i] : '')}},
          y: yMoney(),
        },
        plugins: {
          legend: {position: 'top', align: 'end'},
          tooltip: {callbacks: {
            title: (it) => `Day ${it[0].label}`,
            label: (c) => ` ${c.dataset.label}: ${money2(c.parsed.y)}`,
            footer: (it) => {
              const i = it[0].dataIndex;
              if (i >= cur.length || i >= prev.length) return '';
              const d = cur[i] - prev[i];
              return `${d > 0 ? '+' : d === 0 ? '±' : ''}${money2(d)} vs ${pm}`;
            },
          }},
        },
      },
    });
  }

  // Slice separators (the card colour between slices) only with 2+ non-zero slices: a lone slice would meet itself at
  // 12 o'clock and draw a notch there, so it's one full ring. Every doughnut (Where it went, Allocation) uses this.
  const sliceBorders = (values) => (values.filter((v) => v > 0).length > 1 ? {borderWidth: 3, borderRadius: 4}
    : {borderWidth: 0, borderRadius: 0});

  function centreText(big, sub, sub2) {  // the doughnut's middle: a figure and one or two lines under it
    return {
      id: 'centre',
      afterDraw(ch) {
        const {ctx, chartArea: a} = ch;
        const x = (a.left + a.right) / 2;
        const y = (a.top + a.bottom) / 2 - (sub2 ? 8 : 0);
        ctx.save();
        ctx.textAlign = 'center';
        ctx.fillStyle = CINK.fg;
        ctx.font = `700 21px ${FONT}`;
        ctx.fillText(big, x, y + 4);
        ctx.fillStyle = CINK.tick;
        ctx.font = `500 13px ${FONT}`;
        ctx.fillText(sub, x, y + 23);
        if (sub2) ctx.fillText(sub2, x, y + 40);
        ctx.restore();
      },
    };
  }

  // The "Where it went" doughnut: only the slices not hidden. Its middle names what it shows: all of the month's
  // spending ("$4,031 spent so far"), or with categories hidden "$1,649 shown" over "of $4,031 spent" (also in the
  // canvas label, so screen readers and the numbers audit read the same words).
  function catCentre(included, total, isCurrent, anyHidden) {
    return anyHidden ? [money(included), 'shown', `of ${money(total)} spent`] : [money(total), isCurrent ? 'spent so far' : 'spent', null];
  }

  function catChart(canvas, sl, included, total, isCurrent, select) {
    const centre = catCentre(included, total, isCurrent, Math.abs(included - total) > 0.005);  // not !==: the same cents summed in two orders differ by ~1e-13
    canvas.setAttribute('aria-label', `${canvas.dataset.label}: ${centre.filter(Boolean).join(' ')}`);
    chart(canvas, {
      type: 'doughnut',
      plugins: [centreText(...centre)],
      data: {labels: sl.map((x) => x.name), datasets: [{data: sl.map((x) => x.spend), backgroundColor: sl.map((x) => x.color),
        borderColor: CINK.card, hoverOffset: 6, ...sliceBorders(sl.map((x) => x.spend))}]},
      options: {
        cutout: '74%',
        layout: {padding: 6},
        plugins: {
          legend: {display: false},
          tooltip: {callbacks: {
            label: (c) => {
              const x = sl[c.dataIndex];
              return ` ${x.name}: ${money2(x.spend)} · ${pctOf(x.spend, included)} · ${plural(x.count, 'payment')}`;
            },
          }},
        },
        onClick: (e, els) => { if (els.length) { const x = sl[els[0].index]; if (select) requestAnimationFrame(() => select(x)); else location.hash = x.href; } },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
      },
    });
  }

  // Income vs spending: grouped bars from $0; partial-coverage months lighter, current month "so far".
  function flowChart(canvas, months) {
    const tint = (hex) => months.map((m) => (m.coverage === 'partial' ? rgba(hex, 0.4) : hex));
    chart(canvas, {
      type: 'bar',
      data: {labels: months.map((m) => (m.is_current ? `${monthShort(m.month)} (so far)` : monthShort(m.month))), datasets: [
        {label: 'Income', data: months.map((m) => m.income), backgroundColor: tint(OK), borderRadius: 6, maxBarThickness: 30},
        {label: 'Spending', data: months.map((m) => m.spending), backgroundColor: tint(ACC), borderRadius: 6, maxBarThickness: 30},
      ]},
      options: {
        interaction: {mode: 'index', intersect: false},
        scales: {x: {grid: {display: false}, border: {display: false}, ticks: {maxRotation: 0}}, y: yMoney()},
        plugins: {
          legend: {position: 'top', align: 'end', labels: {generateLabels: (ch) => ch.data.datasets.map((ds, i) => ({
            text: ds.label, fillStyle: i ? ACC : OK, strokeStyle: 'transparent', lineWidth: 0, hidden: !ch.isDatasetVisible(i),
            datasetIndex: i, fontColor: CINK.mut, pointStyle: 'rectRounded'}))}},
          tooltip: {callbacks: {
            title: (it) => {
              const m = months[it[0].dataIndex];
              return monthLabel(m.month) + (m.is_current ? ' (so far)' : '');
            },
            label: (c) => ` ${c.dataset.label}: ${money2(c.parsed.y)}`,
            footer: (it) => {
              const m = months[it[0].dataIndex];
              const out = [`Net ${m.net >= 0 ? '+' : ''}${money2(m.net)}`];
              if (m.coverage === 'partial') out.push(`Partial month: no ${m.missing.join(', ')} history yet`);
              return out;
            },
          }},
        },
      },
    });
  }

  // 6-month mini bars from $0 with the cap (or suggestion) as a dashed line.
  function miniChart(canvas, name, values, hist, ref, refLabel) {
    const color = catColor(name);
    const cov = hist.coverage;
    const datasets = [{type: 'bar', label: 'Spent', data: values.map((v) => Math.max(v, 0)), borderRadius: 5, maxBarThickness: 22,
      backgroundColor: values.map((v, k) => rgba(color, cov[k].is_current ? 1 : cov[k].coverage === 'partial' ? 0.35 : 0.6))}];
    if (ref != null) {
      datasets.push({type: 'line', label: refLabel, data: values.map(() => ref), borderColor: CINK.ref,
        borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 0});
    }
    chart(canvas, {
      data: {labels: hist.months.map(monthShort), datasets},
      options: {
        interaction: {mode: 'index', intersect: false},
        plugins: {legend: {display: false}, tooltip: {callbacks: {
          title: (it) => {
            const k = it[0].dataIndex;
            return monthLabel(hist.months[k]) + (cov[k].is_current ? ' (so far)' : cov[k].coverage === 'partial' ? ' (partial)' : '');
          },
          label: (x) => {
            if (x.dataset.type === 'line') return ` ${refLabel}: ${money(x.parsed.y)}`;
            const v = values[x.dataIndex];
            return v < 0 ? ` Net refund: ${money2(-v)}` : ` Spent: ${money2(v)}`;
          },
          footer: (it) => {
            const c = cov[it[0].dataIndex];
            return c.coverage === 'partial' ? `Missing: ${c.missing.join(', ')}` : '';
          },
        }}},
        scales: {
          x: {grid: {display: false}, border: {display: false}, ticks: {font: {size: 12}}},
          y: yMoney({min: 0, ticks: {maxTicksLimit: 3, callback: axisMoney, font: {size: 12}}}),
        },
      },
    });
  }

  function lineChart(canvas, points, opts = {}) {  // net worth / portfolio value over time
    const color = opts.color || ACC;
    // A nearly flat line gets some air (at least 2% of the value, $10 minimum) instead of a $3 axis that makes a
    // rounding-level wobble look like a crash.
    const vals = points.map((p) => p.value);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max((hi - lo) * 0.1, Math.max(Math.abs(hi), Math.abs(lo)) * 0.02, 10);
    const yRange = opts.fromZero || !vals.length ? {} : {suggestedMin: lo - pad, suggestedMax: hi + pad};
    chart(canvas, {
      type: 'line',
      data: {labels: points.map((p) => fmtDate(p.date)), datasets: [{label: opts.label, data: points.map((p) => p.value),
        borderColor: color, borderWidth: 2.6, tension: 0.25, fill: 'origin', backgroundColor: gradFill(rgbOf(color), 0.28),
        pointRadius: points.length > 45 ? 0 : 3, pointBackgroundColor: color, pointHoverRadius: 5}]},
      options: {
        interaction: {mode: 'index', intersect: false},
        plugins: {legend: {display: false}, tooltip: {callbacks: {
          label: (c) => ` ${opts.label}: ${money2(c.parsed.y)}`,
          afterLabel: opts.after ? (c) => opts.after(points[c.dataIndex]) : undefined,
        }}},
        scales: {
          x: {grid: {display: false}, border: {display: false}, ticks: {maxTicksLimit: 6, maxRotation: 0}},
          y: {beginAtZero: Boolean(opts.fromZero), ...yRange, ticks: {maxTicksLimit: 4, callback: axisMoney}, grid: {color: CINK.grid}, border: {display: false}},
        },
      },
    });
  }

  // One real point and nothing else yet: the axis, the dot and its label.
  function onePointChart(canvas, day, value) {
    const labels = [];
    for (let k = 29; k >= 0; k--) {
      const d = asDate(day);
      d.setDate(d.getDate() - k);
      labels.push(fmtDate(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`));
    }
    const data = labels.map((_, i) => (i === labels.length - 1 ? value : null));
    const label = {
      id: 'pt',
      afterDatasetsDraw(ch) {
        const p = ch.getDatasetMeta(0).data[labels.length - 1];
        if (!p) return;
        const {ctx, chartArea: a} = ch;
        ctx.save();
        ctx.strokeStyle = 'rgba(142,168,255,.45)';
        ctx.setLineDash([4, 5]);
        ctx.beginPath();
        ctx.moveTo(a.left, p.y);
        ctx.lineTo(p.x - 10, p.y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = CINK.fg;
        ctx.font = `650 13px ${FONT}`;
        ctx.textAlign = 'right';
        ctx.fillText(`Today · ${money2(value)}`, p.x - 12, p.y - 14);
        ctx.restore();
      },
    };
    chart(canvas, {
      type: 'line',
      plugins: [label],
      data: {labels, datasets: [{data, borderColor: ACC, pointRadius: 7, pointBackgroundColor: ACC, pointBorderColor: CINK.card,
        pointBorderWidth: 3, spanGaps: false}]},
      options: {
        layout: {padding: {right: 10, top: 10}},
        scales: {x: {grid: {display: false}, border: {display: false}, ticks: {maxTicksLimit: 6, maxRotation: 0, autoSkip: true}},
          y: {beginAtZero: true, suggestedMax: Math.max(value * 1.25, 10), ticks: {maxTicksLimit: 4, callback: axisMoney},
            grid: {color: CINK.grid}, border: {display: false}}},
        plugins: {legend: {display: false}, tooltip: {callbacks: {label: (c) => ` ${money2(c.parsed.y)}`}}},
      },
    });
  }

  function doughnut(canvas, items, total, sub) {
    return chart(canvas, {
      type: 'doughnut',
      plugins: [centreText(money2(total), sub)],
      data: {labels: items.map((x) => x.label), datasets: [{data: items.map((x) => x.value), backgroundColor: items.map((x) => x.color),
        borderColor: CINK.card, ...sliceBorders(items.map((x) => x.value))}]},
      options: {cutout: '76%', layout: {padding: 6}, plugins: {legend: {display: false}, tooltip: {callbacks: {
        label: (c) => ` ${money2(c.parsed)} · ${total ? ((c.parsed / total) * 100).toFixed(1) : 0}%`}}}},
    });
  }

  // ---------- routing --------------------------------------------------------------------------
  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, '');
    const i = raw.indexOf('?');
    const name = i < 0 ? raw : raw.slice(0, i);
    const asset = /^asset\/([1-9][0-9]{0,8})$/.exec(name);  // #/asset/3: a car's / property's page
    if (asset) return {page: 'asset', params: new URLSearchParams({id: asset[1]})};
    return {page: has(PAGES, name) && name !== 'asset' ? name : 'overview',
      params: new URLSearchParams(i < 0 ? '' : raw.slice(i + 1))};
  }

  function hashFor(page, params, month = S.month) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) {
      if (Array.isArray(v)) v.forEach((x) => p.append(k, x));   // polish P1: a repeated parameter
      else if (v != null && v !== '' && k !== 'month') p.set(k, v);
    }
    if (MONTH_PAGES.has(page) && month !== thisMonth()) p.set('month', month);
    const qs = p.toString();
    return `#/${page}${qs ? `?${qs}` : ''}`;
  }

  function route(opts = {}) {  // opts.keepToast: a refresh after a write keeps its confirmation up
    const {page, params} = parseHash();
    const prevMonth = S.month;
    if (MONTH_PAGES.has(page)) {
      const m = params.get('month');
      S.month = m && MONTH_RE.test(m) && (page === 'transactions' || m <= thisMonth()) ? m : thisMonth();
    }
    const switched = MONTH_PAGES.has(page) && page === S.page && S.month !== prevMonth;  // polish P3a: hero count-up
    finishCounts();
    const monthChanged = S.counts && S.counts.month !== S.month;
    // t_acbcd9f4 (motion A): the bars refill and the charts grow on a page switch or a month switch (and the first
    // load), not on a refresh in place after a write or a filter change on the same page
    const moved = page !== S.page || switched;
    S.animate = moved && !reducedMotion();
    S.cameFrom = S.page ? S.lastHash : null;  // the back arrow goes back in the app when you came from it
    S.lastHash = location.hash;
    S.page = page;
    S.params = params;
    // Hidden deep link (no UI): #/settings?relink=<item_id> opens update-mode Link with account selection,
    // so a healthy bank can add accounts opened since it was linked (same Item, no new slot).
    const relink = params.get('relink');
    if (relink) {
      params.delete('relink');
      history.replaceState(null, '', hashFor(page, Object.fromEntries(params)));
      const go = (n) => (typeof Plaid !== 'undefined' || n <= 0 ? reconnect(relink) : setTimeout(() => go(n - 1), 250));
      setTimeout(() => go(40), 0);
    }
    if (HF.chat) HF.chat.refreshContext();  // selected month / sync / approved changes while Ask stays open
    renderNav();
    renderTop();
    closeDrawer();
    closePanel();
    if (!opts.keepToast) {
      hideToast();
      closeSyncPanel();   // (TASK-22f: a sync's own reload keeps its panel open)
    }
    destroyCharts();
    if (monthChanged) loadCounts();
    const tk = ++S.token;
    document.body.classList.toggle('bars-done', !moved);
    COUNT.tk = switched ? tk : 0;
    window.scrollTo(0, 0);
    document.body.classList.toggle('board', BOARDS.has(page));  // first-run setup: no app chrome
    readInk();
    RENDER[page](tk).then(() => {  // the bars have filled: anything drawn later on this page doesn't animate them again
      if (tk === S.token) S.animate = false;   // nor its charts: a toggle or drill redrawn in place stays still
      if (tk === S.token && !document.body.classList.contains('bars-done')) setTimeout(() => document.body.classList.add('bars-done'), 400);
    }).catch((e) => {
      if (tk === S.token) fill($('page'), errorCard(e, () => route()));
    });
  }

  function refresh() {  // after a write: reload the sidebar and the current page in place, keeping the toast
    S.debt = null;  // the loan panel refetches too
    loadAccounts().catch(() => {});
    route({keepToast: true});
  }
  const refreshPage = () => { loadCounts(); refresh(); };   // TASK-22: after a sync (the badges may change too)

  function setMonth(m) {
    if (!MONTH_RE.test(m) || (S.page !== 'transactions' && m > thisMonth())) return;
    if (S.page === 'transactions' && TX.filters) {
      TX.filters = {...TX.filters, since: '', until: '', window: '', txmonth: TX.filters.q ? '1' : ''};
      location.hash = hashFor('transactions', txParams(TX.filters), m);
      return;
    }
    const keep = {};
    for (const [k, v] of S.params) keep[k] = v;
    location.hash = hashFor(S.page, keep, m);
  }

  function renderNav() {
    document.querySelectorAll('[data-pg]').forEach((a) => {
      const key = a.dataset.pg;
      a.setAttribute('href', hashFor(key, {}));
      if (key === (NAV_OF[S.page] || S.page)) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }

  function renderTop() {
    $('title').textContent = PAGES[S.page];
    document.title = `${PAGES[S.page]} · Hermes Finances`;
    const detail = S.page === 'asset';
    $('back').hidden = !detail;
    $('title').classList.toggle('t1', detail);  // a long car name ends with an ellipsis, on one line
    const mon = $('mon');
    mon.hidden = !MONTH_PAGES.has(S.page);
    renderTxWindowControl();
    $('mnext').disabled = S.month >= thisMonth();
  }

  function renderSync(d) {  // the header's status (and on a phone its copy under the page title)
    const active = d.items.filter((it) => !it.removed_at);
    const broken = active.filter((it) => it.status !== 'active' || it.error_code);
    const dot = `dot ${!active.length ? '' : broken.length ? 'bad' : 'ok'}`;
    $('syncdot').className = dot;
    $('syncdot2').className = dot;
    $('syncbox').title = broken.length ? `${plural(broken.length, 'connection')} need${broken.length === 1 ? 's' : ''} attention`
      : 'All connections healthy';
    const fresh = d.last_sync && Date.now() - new Date(d.last_sync).getTime() < 6e4;
    $('sync').textContent = d.last_sync ? `Synced ${fresh ? 'just now' : syncLabel(d.last_sync)}` : 'Not synced yet';
    $('sync2').textContent = $('sync').textContent;
  }

  // ---------- Sync now (TASK-22; TASK-22f: as approved in MOCK-022) ---------------------------------------------------
  // The header's status ("● Synced 6:31 AM"; on a phone under the page title) and beside it the Sync now button. A tap
  // syncs every bank (POST /api/sync): the button is disabled and says "Syncing…" (HOTFIX-1: a light runs round its
  // border, the one thing on the page that moves; a still highlighted border with reduced motion) and the
  // status keeps the last good sync; then the page's data reloads and the panel (#syncp) says what changed: on a desktop
  // under the button (two columns when balances moved and there's new spending), on a phone a bottom sheet. It stays until
  // ×, Escape or a tap outside, except "Nothing new" (and "Synced a moment ago"), which closes by itself after NOTHING_MS
  // unless the pointer or the keyboard is in it. Its "Ask banks for fresh data" (Plaid's refresh, when Settings allows it)
  // asks every bank not asked in the last 30 minutes, and says when the others can be asked again. "8 banks" here are
  // the Plaid banks a sync covers (Alpaca is read live). The server allows one sync at a time and one a minute per bank;
  // a failed sync leaves the status as it was.
  const NOTHING_MS = 4000;
  const ASK = 'Ask banks for fresh data';
  const SY = {busy: false, timer: null};
  const onPhone = () => innerWidth < 1024;
  function syncButton(label) {  // null: ready ("Sync now"; "Sync" on a phone), else disabled with that label
    const b = $('syncnow');
    b.disabled = Boolean(label);
    b.toggleAttribute('aria-busy', Boolean(label));
    b.querySelector('.lbl-d').textContent = label || 'Sync now';
    b.querySelector('.lbl-m').textContent = label || 'Sync';
    b.setAttribute('aria-label', label || 'Sync now');
  }
  function placeSyncPanel() {  // a desktop's panel hangs under the button, right edges lined up; a phone's is a sheet
    const p = $('syncp');
    if (p.hidden) return;
    if (onPhone()) {
      p.style.removeProperty('top');
      p.style.removeProperty('right');
      return;
    }
    const r = $('syncnow').getBoundingClientRect();
    p.style.top = `${Math.round(r.bottom + scrollY + 10)}px`;
    p.style.right = `${Math.max(16, Math.round(document.documentElement.clientWidth - r.right))}px`;
  }
  function openSyncPanel(kids, {tall = false, brief = false, foot = null} = {}) {
    const p = $('syncp');
    clearTimeout(SY.timer);
    fill(p, h('div', {class: 'spbody'}, kids), foot);   // (the body scrolls; "Ask banks for fresh data" stays below it)
    p.classList.toggle('tall', tall);
    p.hidden = false;
    $('syncscrim').hidden = false;   // (drawn on a phone only)
    $('syncnow').setAttribute('aria-expanded', 'true');
    placeSyncPanel();
    p.firstChild.scrollTop = 0;
    p.focus({preventScroll: true});
    // (decision c) "Nothing new" goes away by itself, no animation; anything with changes or errors stays until closed
    if (brief) SY.timer = setTimeout(closeSyncPanel, NOTHING_MS);   // (the pointer or a Tab into it keeps it: boot())
  }
  function closeSyncPanel() {
    const p = $('syncp');
    clearTimeout(SY.timer);
    if (p.hidden) return;
    const inside = p.contains(document.activeElement);
    p.hidden = true;
    $('syncscrim').hidden = true;
    $('syncnow').setAttribute('aria-expanded', 'false');
    if (inside) $('syncnow').focus({preventScroll: true});
  }

  const closeX = () => h('button', {class: 'spx', type: 'button', 'aria-label': 'Close', onclick: closeSyncPanel}, icon('x'));
  const spHead = (tone, ic, title, sub) => h('div', {class: 'sphd'}, h('span', {class: `spic ${tone}`}, icon(ic)),
    h('div', {class: 'grow'}, h('h2', {id: 'syncp-title'}, title), sub ? h('p', {}, sub) : null), closeX());
  const spLine = (tone, ic, text) => h('div', {class: 'sphd one'}, h('span', {class: `spic ${tone}`}, icon(ic)),
    h('p', {class: 'grow', id: 'syncp-title'}, text), closeX());
  const spSec = (label, ...rows) => h('div', {class: 'spsec'}, h('div', {class: 'gh'}, label, h('span', {class: 'ln'})), rows);
  const ACCT_KIND = {credit: 'Credit card · owed', loan: 'Loan · owed', investment: 'Investments'};
  const acctKind = (m) => (m.type === 'depository' ? (m.subtype === 'savings' || /saving/i.test(m.name || '') ? 'Savings'
    : m.subtype === 'checking' ? 'Checking' : 'Cash') : ACCT_KIND[m.type] || m.type || '');
  const bankLabel = (bank, mask, name) => h('b', {class: 'nmrow'}, h('span', {class: 'nm'}, mask ? instShort(bank) : `${instShort(bank)} ${acctName(name)}`.trim()),
    mask ? h('span', {class: 'mk'}, `••${mask}`) : null);
  const movedRow = (m) => h('div', {class: 'sprow'}, ia(m.bank, '', null, m.institution_id),
    h('span', {class: 'grow'}, bankLabel(m.bank, m.mask, m.name), h('span', {}, acctKind(m))),
    h('span', {class: 'spmv v'}, h('span', {class: 'was'}, m.before == null ? 'new' : money2(m.before)),
      h('span', {class: 'to', 'aria-label': 'to'}, '→'), h('b', {}, money2(m.after))));
  const spendRow = (t) => h('div', {class: 'sprow'}, avatar(t.merchant_name || t.name, t.logo_url, null, 32),
    h('span', {class: 'grow'}, h('b', {}, t.merchant_name || t.name),
      h('span', {}, `${instShort(t.bank)}${t.mask ? ` ••${t.mask}` : ''}${t.pending ? ' · pending' : ''}`)),
    h('b', {class: 'v spamt'}, money2(t.amount)));
  const changeCounts = (c) => [c.new ? plural(c.new, 'new transaction') : null, c.updated ? `${c.updated} updated` : null,
    c.removed ? `${c.removed} removed` : null].filter(Boolean).join(' · ');
  const MOVED_ROWS = 6;
  function movedSec(moved) {
    return spSec('Balances that moved', moved.slice(0, MOVED_ROWS).map(movedRow), moved.length > MOVED_ROWS
      ? h('p', {class: 'spmore'}, `+ ${moved.length - MOVED_ROWS} more · `, lnk('#/accounts', 'See Accounts')) : null);
  }
  function moreLine(m, lead) {  // "+ 8 transfers and card payments · See all in Transactions"
    const parts = [m.spend ? plural(m.spend, 'more purchase') : null,
      m.transfers ? (m.transfers === 1 ? '1 transfer or card payment' : `${m.transfers} transfers and card payments`) : null,
      m.money_in ? (m.money_in === 1 ? '1 deposit or refund' : `${m.money_in} deposits and refunds`) : null].filter(Boolean);
    return parts.length ? h('p', {class: 'spmore'}, `${lead}${parts.join(', ')} · `, lnk('#/transactions', 'See all in Transactions')) : null;
  }
  const banksWord = (n, name) => (n === 1 ? instShort(name) || '1 bank' : `All ${n} banks`);
  const minutes = (s) => `${Math.max(1, Math.ceil(s / 60))} min`;
  const askedAgo = (b) => (b.asked_ago == null || b.asked_ago < 60 ? 'just now' : `${Math.floor(b.asked_ago / 60)} min ago`);
  function askFoot(rf) {  // "Ask banks for fresh data", when Settings allows it and a bank can be asked
    if (!rf || !rf.allowed || !(rf.banks || []).length) return null;
    const ready = rf.banks.filter((b) => !b.retry_after);
    const wait = rf.banks.filter((b) => b.retry_after).sort((a, b) => a.retry_after - b.retry_after);
    const cost = rf.billed ? 'Plaid bills each request on your paid plan.' : rf.sandbox ? 'free in Sandbox.' : 'free on your Trial plan.';
    const Cost = cost[0].toUpperCase() + cost.slice(1);
    const ask = (label, disabled) => h('button', {class: 'btn ghost spask', type: 'button', disabled,
      onclick: () => syncNow(true)}, icon('landmark'), label);
    if (!ready.length) {  // every bank asked in the last 30 minutes: when the first can be asked again
      return h('div', {class: 'spfoot'}, ask(ASK, true), h('p', {class: 'spwait'}, icon('clock'),
        h('span', {}, `Asked ${askedAgo(wait[0])} · available again in ${minutes(wait[0].retry_after)}`)));
    }
    if (wait.length) {  // some asked recently (Hermes' finance_sync can ask one bank): the others, and when those are
      return h('div', {class: 'spfoot'}, ask(`Ask ${plural(ready.length, 'bank')} for fresh data`),
        wait.map((b) => h('p', {class: 'spwait'}, ia(b.institution_name, '', null, b.institution_id),
          h('span', {}, h('b', {}, instShort(b.institution_name)), ` · asked ${askedAgo(b)} · available again in ${minutes(b.retry_after)}`))),
        h('p', {class: 'hint'}, Cost));
    }
    return h('div', {class: 'spfoot'}, ask(ASK),
      h('p', {class: 'hint'}, `Banks can send new items hours late. This asks each bank to check now; ${cost}`));
  }
  const unreachable = () => [[spHead('bad', 'alert', 'Couldn’t reach Plaid', 'Nothing changed; try again in a minute.')], {}];
  function resultView(r) {  // [the panel's content, its options] for a sync's answer
    if (!r.banks) return [[spLine('mut', 'info', r.message)], {brief: true}];
    if (r.unreachable) return unreachable();
    const c = r.changes;
    const bad = r.items.filter((it) => !it.ok);
    const counts = changeCounts(c);
    // (HOTFIX-1) the banks this sync covered: one synced in the last minute waits (r.waiting), and isn't claimed
    const ran = (r.waiting || []).length ? r.items.length : r.banks;
    if (bad.length) {  // some banks failed: each with what to do (Reconnect), then what the others brought in
      const ok = ran - bad.length;
      const none = r.banks === 1 ? instShort(bad[0].institution_name) || 'your bank' : `your ${r.banks} banks`;
      const kids = [spHead(ok ? 'warn' : 'bad', 'alert', ok ? `Synced ${ok} of ${r.banks} banks` : `Couldn’t sync ${none}`, null)];
      for (const it of bad) {
        const relogin = it.error_code === 'ITEM_LOGIN_REQUIRED';
        kids.push(h('div', {class: 'sprow relog', title: it.error || null}, ia(it.institution_name, '', 'bad', it.institution_id),
          h('span', {class: 'grow'}, h('b', {}, instShort(it.institution_name) || 'A bank'),
            h('span', {}, relogin ? 'Needs you to sign in again' : 'Couldn’t sync this time')),
          relogin ? h('button', {class: 'btn sm spre', type: 'button', dataset: {item: it.item_id}}, 'Reconnect') : null));
      }
      if (ok) {
        kids.push(h('p', {class: 'spsum'}, `From the other ${ok === 1 ? 'bank' : ok}: ${counts
          || (c.balances.length ? `${plural(c.balances.length, 'balance')} moved` : 'nothing new')}`));
        if (c.balances.length) kids.push(movedSec(c.balances));
      }
      return [kids, {}];
    }
    const kids = [spHead('ok', 'check', 'Synced just now', ran === r.banks ? banksWord(r.banks, (r.items[0] || {}).institution_name)
      : `${ran} of ${plural(r.banks, 'bank')}; the rest synced a moment ago`)];
    const spend = c.new_spend || [];
    const more = c.new_more || {};
    if (!counts && !c.balances.length) {  // nothing new: it closes by itself
      kids.push(h('p', {class: 'spsum'}, r.previous_sync ? `Nothing new since ${syncLabel(r.previous_sync)}.` : 'Nothing new.'));
      return [kids, {brief: true, foot: askFoot(r.refresh)}];
    }
    kids.push(h('p', {class: 'spsum'}, counts || `${plural(c.balances.length, 'balance')} moved`));
    const cols = [c.balances.length ? movedSec(c.balances) : null,
      spend.length ? spSec('New spending', spend.map(spendRow), moreLine(more, '+ ')) : null].filter(Boolean);
    kids.push(cols.length > 1 ? h('div', {class: 'spcols'}, cols) : cols, spend.length ? null : moreLine(more, ''));
    return [kids, {tall: cols.length > 1, foot: askFoot(r.refresh)}];
  }
  function askedView(r) {  // after "Ask banks for fresh data"
    const asked = r.refresh_requested || [];
    if (!asked.length) return r.unreachable || r.errors.length ? resultView(r) : [[spLine('mut', 'clock', r.note)], {}];
    const all = (r.refresh.banks || []).length;
    const ids = new Set(asked.map((a) => a.item_id));
    const next = (r.refresh.banks || []).filter((b) => ids.has(b.item_id)).map((b) => b.retry_after).filter(Boolean);
    const counts = changeCounts(r.changes);
    // (HOTFIX-1) asked within a minute of a sync: nothing was synced again, and the panel says so (never "This sync")
    const skipped = r.sync_skipped;
    const part = !skipped && (r.waiting || []).length ? `${r.items.length} of ${plural(r.banks, 'bank')}` : '';
    return [[spHead('ok', 'landmark', 'Banks asked for fresh data', asked.length === all ? banksWord(all, asked[0].institution_name)
      : asked.length === 1 ? instShort(asked[0].institution_name) : `${asked.length} of ${all} banks`),
    h('p', {class: 'spsum'}, 'New items usually land within a few minutes. Tap Sync again then.'),
    skipped ? h('p', {class: 'spmore'}, `Not synced again: the last sync was ${Math.max(1, skipped.ago)} s ago.`) : null,
    next.length ? h('p', {class: 'spwait'}, icon('clock'), h('span', {}, `You can ask again in ${minutes(Math.min(...next))}`)) : null,
    counts || part ? h('p', {class: 'spmore'}, `This sync: ${[part, counts || (part ? 'nothing new' : '')].filter(Boolean).join(' · ')}`)
      : null], {}];
  }
  function refusedView(e) {  // the server said no: the minute between syncs, one already running, the setting; or a failure
    if (e.retryAfter) return [[spLine('mut', 'clock', e.message.replace(/ in (\d+) s\./, ' in $1 s.'))], {brief: true}];
    if (e.status === 409) return [[spLine('mut', 'clock', e.message)], {brief: true}];
    if (e.status === 403) return [[spLine('warn', 'alert', e.message)], {}];
    return [[spHead('bad', 'alert', 'Couldn’t sync', e.message)], {}];
  }
  function lostView(e) {  // (HOTFIX-1) no answer and no look-up either: what may still have happened
    return [[spHead('warn', 'alert', 'Lost the connection', e.message)], {}];
  }
  // (HOTFIX-1) A sync runs on the server in the background, under the page's own id for it (rid). POST /api/sync answers
  // with the result when it's done within a few seconds, else 202, and the page asks GET /api/sync?rid= every POLL_MS
  // until it is. A POST whose answer never came (a phone through a proxy: the browser's "Load failed", the app sent to the
  // background) is looked up the same way, so the panel shows what really happened; only when the look-ups fail too
  // (LOST_TRIES in a row, for LOST_MS or more) does it say the connection was lost, and what may still have happened.
  const POLL_MS = 1500, LOST_TRIES = 4, LOST_MS = 20000;
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  const newRid = () => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
  function lostError(refresh, known) {
    const e = new Error(known === 'never' ? `The server didn’t get it, so nothing ran. Tap ${refresh ? 'Ask banks' : 'Sync'} again.`
      : refresh ? (known === 'running' ? 'Your banks are still being asked. Reload the page in a minute to see it.'
        : 'If the server got it, your banks were still asked. Reload the page in a minute to see it.')
        : known === 'running' ? 'The sync is still running. Reload the page in a minute to see it.'
          : 'If the server got it, the sync still ran. Reload the page in a minute to see it.');
    e.lost = true;
    return e;
  }
  async function syncRun(refresh) {
    const rid = newRid();
    let r = null;
    try {
      r = await post('/api/sync', refresh ? {refresh: true, rid} : {rid});
    } catch (e) {
      if (e.status) throw e;   // the server answered: a refusal (409, 429, 403, 400) or an error
    }                          // (else no answer: look it up)
    let known = r ? 'running' : 'unknown', misses = 0, since = Date.now();
    while (!r || r.running) {
      await pause(POLL_MS);
      let s;
      try {
        s = await api('/api/sync', {rid});
      } catch (e) {
        if (++misses >= LOST_TRIES && Date.now() - since >= LOST_MS) throw lostError(refresh, known);
        continue;
      }
      misses = 0;
      since = Date.now();
      if (!s.found) throw lostError(refresh, 'never');   // it never reached the server (or the server restarted)
      if (s.error) throw Object.assign(new Error(s.error), {status: 500});
      known = 'running';
      r = s.running ? s : s.result;
    }
    return r;
  }
  async function syncNow(refresh = false) {
    if (SY.busy) return;
    SY.busy = true;
    closeSyncPanel();
    hideToast();
    syncButton(refresh ? 'Asking banks…' : 'Syncing…');
    let view;
    try {
      const r = await syncRun(refresh);
      view = refresh ? askedView(r) : resultView(r);
      refreshPage();   // its /api/accounts redraws the status from the last sync that worked ("Synced just now" after one)
    } catch (e) {
      view = e.lost ? lostView(e) : refusedView(e);
      if (S.accounts) renderSync(S.accounts);
    } finally {
      SY.busy = false;
      syncButton(null);
    }
    openSyncPanel(...view);
  }

  // ---------- drawer (phone) -------------------------------------------------------------------
  function openDrawer() {
    document.body.classList.add('drawer');
    $('menu').setAttribute('aria-expanded', 'true');
    const cur = document.querySelector('#nav [aria-current=page]');
    if (cur) cur.focus();
  }
  function closeDrawer() {
    if (!document.body.classList.contains('drawer')) return;
    document.body.classList.remove('drawer');
    $('menu').setAttribute('aria-expanded', 'false');
  }

  // ---------- sidebar --------------------------------------------------------------------------
  function loadAccounts() {
    S.accountsP = api('/api/accounts').then((d) => {
      S.accounts = d;
      NW.out = new Set(d.net_worth.custom.excluded.map((a) => a.id));
      setInstitutions(d.institutions);
      CONNECTED_INSTITUTIONS = new Set(d.connected_institution_ids);
      renderSidebar(d);
      renderSync(d);
      return d;
    }).catch((e) => {
      S.accountsP = null;
      throw e;
    });
    return S.accountsP;
  }
  const ensureAccounts = () => S.accountsP || loadAccounts();

  async function ensureCategories() {
    if (!S.categories) S.categories = await api('/api/categories');
    return S.categories;
  }

  async function loadCounts() {
    try {
      S.counts = await api('/api/counts', {month: S.month});
    } catch (e) {
      return;  // badges are optional
    }
    const c = S.counts;
    const set = (key, text, cls, title) => {
      const el = document.querySelector(`[data-count="${key}"]`);
      if (!el) return;
      el.textContent = text;
      el.className = `n ${cls || ''}`;
      if (title) el.title = title;
      else el.removeAttribute('title');
    };
    if (c.review) set('transactions', String(c.review), 'rv', `${c.review} need a category`);
    else set('transactions', '', '');
    if (c.over) set('budgets', `${c.over} over`, 'ov', `${plural(c.over, 'budget')} over the cap`);
    else if (c.warn) set('budgets', `${c.warn} near`, 'wn', `${plural(c.warn, 'budget')} at 80% or more`);
    else set('budgets', '');
    set('recurring', c.recurring ? String(c.recurring) : '', '', `${plural(c.recurring, 'recurring charge')}`);
  }

  // Institutions from /api/accounts, with net worth numbers where the Item has accounts.
  function banksOf(d) {
    const byItem = new Map(d.net_worth.institutions.map((b) => [b.item_id, b]));
    return [...d.items, ...(d.api_items || [])].filter((it) => !it.removed_at).map((it) => {  // TASK-19: + Alpaca (API)
      const nw = byItem.get(it.item_id);
      return {item: it, id: it.institution_id, name: it.institution_name || it.institution_id || it.item_id, net: nw ? nw.net : 0,
        accounts: nw ? nw.accounts : [], broken: it.status !== 'active' || Boolean(it.error_code)};
    });
  }

  function renderSideWorth(d) {  // the sidebar's net worth figure and Own / Owe, in the mode
    const w = worthOf(d.net_worth);
    $('snw').textContent = money(w.net);
    // The note under "Net worth" says which total it is: "Custom" in Custom (redrawn with the flip), else as before.
    const nw = d.net_worth;
    $('nwn').textContent = NW.mode === 'custom' ? 'Custom'
      : nw.manual.count ? 'incl. manual' : plural(banksOf(d).length, 'connection');   // (TASK-22f: Alpaca isn't a bank)
    fill($('ssplit'), ownOwe(w));
    $('sas').textContent = `Own ${money(w.assets)}`;
    $('sli').textContent = `Owe ${money(w.debts)}`;
    const box = $('snw').parentNode;
    box.querySelectorAll('.transit').forEach((el) => el.remove());
    add(box, transitLines(nw));
  }

  function renderSidebar(d) {
    const nw = d.net_worth;
    const banks = banksOf(d);
    renderSideWorth(d);
    fill($('banks'), ...(banks.length ? banks.map((b, i) => bankRow(b, i === 0)) : [h('p', {class: 'nc'}, 'No banks connected yet.')]),
      d.manual.length ? manualRow(d.manual) : null);
    fill($('slots'), 'slots used: ', h('b', {}, `${d.slots_used} / ${d.slot_limit}`));
    const meter = $('meter');
    meter.setAttribute('aria-valuenow', String(d.slots_used));
    fill(meter, h('i', {style: {width: pct(d.slots_used / d.slot_limit)}}));
  }

  function sideGroup(id, head, rows, open) {  // a sidebar row that expands to its accounts
    const panel = h('div', {class: 'accts', id}, rows);
    const wrap = h('div', {class: `bank${open ? ' open' : ''}`});
    const tog = h('button', {class: 'tog', type: 'button', 'aria-expanded': String(open), 'aria-controls': id,
      'aria-label': `${head.label} accounts`}, icon('chevr'));
    tog.addEventListener('click', () => tog.setAttribute('aria-expanded', String(wrap.classList.toggle('open'))));
    return add(wrap, [h('div', {class: 'row'}, head.link, tog), head.extra, panel]);
  }

  function bankRow(b, open) {
    const hooked = HOOKS.bankRow(b, open);
    if (hooked) return hooked;
    const accts = b.accounts;
    const big = accts.filter((a) => Math.abs(a.current || 0) >= 1);
    const tiny = accts.filter((a) => Math.abs(a.current || 0) < 1);
    const line = (a) => h('div', {class: 'ac'}, nameMask(a.name, a.mask || '----', {title: acctName(a.official_name || a.name)}),
      leftOut(a.account_id) ? notCounted() : null, h('span', {class: 'v'}, money(a.signed == null ? a.current : a.signed)));
    const rows = big.map(line);
    if (tiny.length) {
      const more = h('button', {type: 'button', class: 'showall'}, `+${tiny.length} under $1 · show all`);
      more.addEventListener('click', () => more.replaceWith(...tiny.map(line)));
      rows.push(more);
    }
    if (!accts.length) rows.push(h('div', {class: 'ac'}, h('span', {}, 'No accounts yet')));
    rows.push(h('a', {class: 'go', href: hashFor('transactions', {bank: b.name})}, 'View transactions →'));
    return sideGroup(`accts-${b.item.item_id}`, {
      label: b.name,
      link: h('a', {class: 'link', href: hashFor('transactions', {bank: b.name}), title: `${b.name}: view transactions`},
        ia(b.name, 's', b.broken ? 'bad' : 'ok', b.id), h('span', {class: 'nm'}, instShort(b.name)), h('span', {class: 'v'}, money(b.net))),
      extra: b.broken ? h('div', {class: 'relogin'}, h('span', {}, b.item.error_code === 'ITEM_LOGIN_REQUIRED' || b.item.status === 'login_required'
        ? 'Needs a re-login' : `Connection problem (${b.item.error_code || b.item.status})`), reconnectBtn(b.item.item_id)) : null,
    }, rows, open);
  }

  function manualRow(items) {
    const rows = items.map((m) => (['vehicle', 'property'].includes(m.kind)
      ? h('a', {class: 'ac', href: `#/asset/${m.id}`}, h('span', {}, acctName(m.name)), leftOut(manualNwId(m.id)) ? notCounted() : null,
        h('span', {class: 'v'}, money(m.signed)))
      : h('div', {class: 'ac'}, h('span', {}, acctName(m.name)), leftOut(manualNwId(m.id)) ? notCounted() : null,
        h('span', {class: 'v'}, `${m.approx ? '≈ ' : ''}${money(m.signed)}`))));
    rows.push(h('a', {class: 'go', href: '#/accounts'}, 'Accounts →'));
    return sideGroup('accts-manual', {
      label: 'Added by you',
      link: h('a', {class: 'link', href: '#/accounts'}, ibadge('pencil', RGB.warn, 'xs'), h('span', {class: 'nm'}, 'Added by you'),
        h('span', {class: 'v'}, money(sum(items, (m) => m.signed)))),
    }, rows, false);
  }

  // ---------- Plaid Link: connect + update-mode reconnect --------------------------------------
  async function connectBank() {
    let blocked = null;
    try {
      await loadAccounts();  // fresh connected-institution list for the duplicate guard
      if (typeof Plaid === 'undefined') throw new Error('Plaid Link has not loaded yet (or was blocked). Try again in a moment.');
      const {link_token} = await post('/api/link_token');
      const handler = Plaid.create({
        token: link_token,
        onEvent: (name, meta) => {
          // Early duplicate guard: close Link before credentials are submitted (before an Item exists).
          if (name === 'SELECT_INSTITUTION' && CONNECTED_INSTITUTIONS.has(meta.institution_id)) {
            blocked = meta.institution_name || meta.institution_id;
            handler.exit({force: true});
          }
        },
        onSuccess: async (public_token, meta) => {
          show('Saving connection and running the first sync…', 'info');
          try {
            const res = await post('/api/exchange', {public_token, metadata: {
              institution: meta.institution, accounts: meta.accounts, link_session_id: meta.link_session_id}});
            show('Connected ' + (res.institution_name || '') + ' — ' + res.sync, res.sync_ok ? 'info' : 'error');
            setTimeout(() => location.reload(), 1500);
          } catch (e) { show(e.message, 'error'); }
        },
        onExit: (err) => {
          if (blocked) show(blocked + ' is already connected — use Reconnect instead.', 'error');
          else if (err) show('Link exited: ' + (err.display_message || err.error_message || err.error_code), 'error');
        },
      });
      handler.open();
    } catch (e) { show(e.message, 'error'); }
  }

  async function reconnect(itemId) {
    try {
      if (typeof Plaid === 'undefined') throw new Error('Plaid Link has not loaded yet (or was blocked). Try again in a moment.');
      const {link_token} = await post('/api/link_token/update', {item_id: itemId});
      const handler = Plaid.create({
        token: link_token,
        onSuccess: async () => {  // update mode: no exchange, the access token is unchanged
          show('Reconnected — syncing…', 'info');
          try {
            const res = await post('/api/reconnected', {item_id: itemId});
            show(res.sync, res.sync_ok ? 'info' : 'error');
            setTimeout(() => location.reload(), 1500);
          } catch (e) { show(e.message, 'error'); }
        },
        onExit: (err) => { if (err) show('Reconnect exited: ' + (err.display_message || err.error_code), 'error'); },
      });
      handler.open();
    } catch (e) { show(e.message, 'error'); }
  }

  // ---------- Add account: chooser, forms, edit + archive --------------------------------------
  const KIND = {  // manual kind -> icon, colour, labels
    bank: {i: 'landmark', c: RGB.ok, label: 'Bank account'}, credit_card: {i: 'card', c: RGB.bad, label: 'Credit card'},
    investment: {i: 'linechart', c: RGB.acc, label: 'Investment'}, loan: {i: 'handcoins', c: RGB.warn, label: 'Loan'},
    vehicle: {i: 'car', c: RGB.cyan, label: 'Vehicle'}, property: {i: 'house', c: RGB.purple, label: 'Property'},
    other_asset: {i: 'package', c: RGB.ok, label: 'Other asset'}, other_liability: {i: 'fileminus', c: RGB.bad, label: 'Other liability'},
  };
  const TXT = (key, label, ph, o = {}) => ({key, label, type: 'text', ph, max: key === 'subtype' ? 40 : key.startsWith('vehicle') ? 40 : 80, ...o});
  const MONEY = (key, label, o = {}) => ({key, label, type: 'money', ph: '0.00', ...o});
  const CHIPS = (key, label, opts) => ({key, label, type: 'chips', opts});
  const FORMS = {
    bank: {kind: 'bank', t: 'Bank account', title: 'Add a bank account', s: 'Checking, savings, money market or CD', save: 'Add bank account',
      f: [TXT('name', 'Nickname', 'e.g. Joint savings'), TXT('institution', 'Bank name', 'e.g. Ally Bank'),
        CHIPS('subtype', 'Account type', [['checking', 'Checking'], ['savings', 'Savings'], ['money_market', 'Money market'], ['cd', 'CD']]),
        MONEY('balance', 'Current balance')]},
    card: {kind: 'credit_card', t: 'Credit card', title: 'Add a credit card', s: 'Balance and credit limit', save: 'Add credit card',
      f: [TXT('name', 'Nickname', 'e.g. Travel card'), TXT('institution', 'Card company', 'e.g. American Express'),
        MONEY('credit_limit', 'Credit limit', {half: true, positive: true}), MONEY('balance', 'Current balance', {half: true}),
        {key: 'match_pattern', label: 'Match payments from', type: 'match', ph: 'Name on your bank statement', optional: true, max: 60}]},
    invest: {kind: 'investment', t: 'Investment or retirement account', title: 'Add an investment account', s: '401(k), IRA, brokerage, HSA or crypto',
      save: 'Add account',
      f: [TXT('name', 'Nickname', 'e.g. Work 401(k)'), TXT('institution', 'Institution', 'e.g. Fidelity'),
        CHIPS('subtype', 'Account type', [['brokerage', 'Brokerage'], ['401k', '401(k)'], ['roth_ira', 'Roth IRA'],
          ['traditional_ira', 'Traditional IRA'], ['hsa', 'HSA'], ['crypto', 'Crypto'], ['other', 'Other']]),
        MONEY('balance', 'Current balance')]},
    loan: {kind: 'loan', t: 'Loan', title: 'Add a loan', s: 'Auto, student, personal, mortgage or BNPL', save: 'Add loan',
      f: [TXT('name', 'Nickname', 'e.g. Car loan'), TXT('institution', 'Provider', 'e.g. Ally Auto'),
        CHIPS('subtype', 'Loan type', [['auto', 'Auto'], ['student', 'Student'], ['personal', 'Personal'], ['mortgage', 'Mortgage'],
          ['bnpl', 'BNPL'], ['other', 'Other']]),
        {key: 'interest_rate', label: 'Interest rate', type: 'pct', ph: 'e.g. 4.9', half: true, optional: true},
        MONEY('original_amount', 'Original amount', {half: true, positive: true}), MONEY('balance', 'Amount remaining', {half: true}),
        MONEY('monthly_payment', 'Monthly payment', {half: true, positive: true}),
        {key: 'match_pattern', label: 'Match payments from', type: 'match', ph: 'Name on your bank statement', optional: true, max: 60}]},
    car: {kind: 'vehicle', t: 'Car', title: 'Add a vehicle', s: 'Make, model, year and value', save: 'Add vehicle',
      f: [{key: 'vin', label: 'VIN', type: 'vin', ph: '17 characters, e.g. on your registration', optional: true, max: 17},
        TXT('vehicle_make', 'Make', 'e.g. Honda', {half: true}), TXT('vehicle_model', 'Model', 'e.g. CR-V', {half: true}),
        {key: 'vehicle_year', label: 'Year', type: 'year', ph: 'e.g. 2022', half: true},
        TXT('vehicle_trim', 'Trim', 'e.g. EX', {half: true, optional: true}),
        {key: 'mileage', label: 'Mileage', type: 'miles', ph: 'e.g. 42,000', half: true, optional: true},
        {key: 'zip_code', label: 'ZIP code', type: 'zip', ph: 'e.g. 94110', half: true, optional: true, max: 10},
        {key: 'linked_loan_id', label: 'Loan', type: 'loan', optional: true},
        MONEY('balance', 'Your value')]},
    property: {kind: 'property', t: 'Property', title: 'Add a property', s: "Your home's value", save: 'Add property',
      f: [TXT('name', 'Nickname', 'e.g. Home'),
        TXT('address', 'Address', 'Street, city, state and ZIP', {optional: true, max: 160}),
        {key: 'linked_loan_id', label: 'Mortgage', type: 'loan', optional: true}, MONEY('balance', 'Your value')]},
    asset: {kind: 'other_asset', t: 'Other asset', title: 'Add another asset', s: 'Anything else you own', save: 'Add asset',
      f: [TXT('name', 'Name', 'e.g. Watch'), TXT('subtype', 'Type', 'e.g. Collectible'), MONEY('balance', 'Value')]},
    liab: {kind: 'other_liability', t: 'Other liability', title: 'Add another liability', s: 'Anything else you owe', save: 'Add liability',
      f: [TXT('name', 'Name', 'e.g. Loan from family'), TXT('subtype', 'Type', 'e.g. Personal IOU'), MONEY('balance', 'Amount left')]},
  };
  const FORM_OF = {bank: 'bank', credit_card: 'card', investment: 'invest', loan: 'loan', vehicle: 'car', property: 'property',
    other_asset: 'asset', other_liability: 'liab'};
  const MANUAL_ORDER = ['bank', 'card', 'invest', 'loan', 'car', 'asset', 'liab'];

  function slotInfo() {
    const d = S.accounts;
    const used = d ? d.slots_used : Number(document.body.dataset.slotsUsed);
    const limit = d ? d.slot_limit : Number(document.body.dataset.slotLimit);
    return {used, limit, full: used >= limit};
  }

  function opt(iconName, rgb, title, text, onclick, extra, disabled) {
    return h('button', {class: 'opt', type: 'button', onclick, disabled}, ibadge(iconName, rgb, 'l'),
      h('span', {class: 'grow'}, h('b', {}, title), h('span', {}, text)), extra, icon('chevr'));
  }

  function openAdd(step = 'home') {
    if (step.startsWith('edit:')) {
      editManual(Number(step.slice(5)));
      return;
    }
    if (step.startsWith('form:')) {
      addForm(step.slice(5), null, ['property', 'car'].includes(step.slice(5)) ? 'home' : 'manual');
      return;
    }
    const {used, limit, full} = slotInfo();
    const meter = h('span', {class: 'slotm'}, `${used} of ${limit} used`, h('span', {class: 'meter'}, h('i', {style: {width: pct(used / limit)}})));
    if (step === 'home') {
      openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Add account'), closeBtn()),
        h('div', {class: 'opts'},
          opt('link', RGB.acc, 'Link a bank or card', full ? `All ${limit} connections are used` : 'Connect securely with Plaid',
            () => openAdd('plaid'), meter),
          opt('house', RGB.purple, 'Add a property', "Track your home's value", () => openAdd('form:property')),
          opt('car', RGB.cyan, 'Add a vehicle', "Track your car's value", () => openAdd('form:car')),
          opt('pencil', RGB.warn, 'Add manually', "Loans, cards and accounts Plaid can't reach", () => openAdd('manual')),
          HOOKS.addOptions.map((f) => f()))]);
    } else if (step === 'plaid') {
      // Duplicate guard that actually saves the slot: Plaid creates the Item the moment its login finishes, and Link's
      // SELECT_INSTITUTION event only reaches us at the end of the flow, so nothing inside Link can stop it in time.
      // This sheet is the last point the app controls: it lists the banks already connected (each with Update, which
      // reuses the Item) before Plaid opens at all.
      const conns = ((S.accounts && S.accounts.items) || []).filter((it) => !it.removed_at);
      const bankLine = (it) => {
        const name = it.institution_name || it.institution_id || it.item_id;
        return h('div', {class: 'conn'}, ia(name, 's', 'ok', it.institution_id), h('span', {class: 'grow'}, h('b', {title: name}, instShort(name))),
          h('button', {class: 'btn sm ghost upd', type: 'button', onclick: () => { closeModal(); reconnect(it.item_id); },
            'aria-label': `Update ${instShort(name)} accounts`}, 'Update'));
      };
      const go = h('button', {class: 'btn w', type: 'button', disabled: full,
        onclick: () => { closeModal(); connectBank(); }}, conns.length ? 'Connect a different bank' : 'Continue to Plaid');
      openSheet([h('div', {class: 'shd'}, backBtn(() => openAdd('home')), h('h2', {id: 'sheet-title'}, 'Link a bank or card'), closeBtn()),
        h('div', {class: 'prev'}, ring(used / limit, ACC, 62, 7, h('b', {class: 'v', style: {'font-size': '15px'}}, String(used))),
          h('div', {}, h('b', {}, `${limit - used} of ${limit} connections left`),
            h('div', {class: 'mut', style: {'font-size': '14px'}}, 'Each bank you link uses one for good, even if you log in and back out.'))),
        conns.length ? [
          h('h3', {class: 'duph'}, 'Already connected'),
          h('p', {class: 'mut dupp'}, 'New card or account at one of these? Use Update. It uses no connection.'),
          h('div', {class: 'duplist'}, conns.map(bankLine))] : null,
        full ? h('p', {class: 'nc warn'}, `All ${limit} Plaid slots are used. You can still add accounts by hand.`) : null,
        h('div', {class: 'row'}, go, h('button', {class: 'btn ghost', type: 'button', onclick: () => openAdd('home')}, 'Back'))]);
    } else if (has(HOOKS.addSteps, step)) {
      HOOKS.addSteps[step]();
    } else if (step === 'manual') {
      openSheet([h('div', {class: 'shd'}, backBtn(() => openAdd('home')), h('h2', {id: 'sheet-title'}, 'What do you want to add?'), closeBtn()),
        h('p', {}, 'You type in the numbers. Update them any time.'),
        h('div', {class: 'opts'}, MANUAL_ORDER.map((k) => {
          const F = FORMS[k];
          const K = KIND[F.kind];
          return h('button', {class: 'opt', type: 'button', onclick: () => openAdd(`form:${k}`)}, ibadge(K.i, K.c),
            h('span', {class: 'grow'}, h('b', {}, F.t), h('span', {}, F.s)), icon('chevr'));
        }))]);
    }
  }

  async function editManual(id) {  // prefill from the server's current values, not the page's copy
    const d = await api('/api/manual').catch(() => null);
    const m = d && d.accounts.find((x) => x.id === id);
    if (!m) {
      toast('That account is gone (archived or removed).', [], {error: true, timeout: 5000});
      return;
    }
    addForm(FORM_OF[m.kind], m, null);
  }

  function parseAmount(raw) {  // "30,947.00", "$465.10" -> number; "" -> null; junk -> NaN (a "," decimal locale: "1.234,56")
    let s = String(raw).split(CUR.sym).join('').replace(/[$\s\u00a0\u202f]/g, '');
    s = CUR.dec === ',' ? s.replace(/\./g, '').replace(',', '.') : s.replace(/,/g, '');
    if (!s) return null;
    return /^\d*\.?\d+$|^\d+\.$/.test(s) ? Number(s) : NaN;
  }

  // Payoff like the server's manual.payoff: amortized in cents with an APR, else remaining / payment.
  function payoffOf(balance, apr, monthly, firstMonth) {
    const cents = Math.round(balance * 100);
    if (cents <= 0) return {n: 0, month: null, method: 'paid'};
    if (!monthly || monthly <= 0) return {n: null, month: null, method: 'none'};
    const pay = Math.round(monthly * 100);
    let n;
    if (apr) {
      const rate = apr / 1200;
      if (pay <= Math.round(cents * rate)) return {n: null, month: null, method: 'never'};
      let left = cents;
      n = 0;
      while (left > 0 && n < 1200) {
        left = left + Math.round(left * rate) - pay;
        n += 1;
      }
      if (left > 0) return {n: null, month: null, method: 'never'};
    } else {
      n = Math.ceil(cents / pay);
    }
    return {n, month: addMonths(firstMonth, n - 1), method: apr ? 'amortized' : 'simple'};
  }

  function field(f, value, kind) {
    const id = `f-${f.key}`;
    const eid = `e-${f.key}`;
    const lab = [f.label, f.optional ? h('em', {}, '· optional') : null];
    const err = h('div', {class: 'ferr', id: eid, role: 'alert'});
    let control;
    let get;
    if (f.type === 'loan') {  // a vehicle / property: the manual loan it's financed by (its equity), or none
      const loans = ((S.accounts && S.accounts.manual) || []).filter((m) => m.kind === 'loan' && !m.archived_at);
      const sel = h('select', {id, name: f.key, 'aria-describedby': eid},
        h('option', {value: ''}, loans.length ? 'None' : 'None (add the loan first)'),
        loans.map((l) => h('option', {value: String(l.id), selected: value === l.id},
          `${l.name}${l.institution ? ` · ${l.institution}` : ''} · ${money2(l.balance)} left`)));
      const wrap = h('div', {class: `fld${f.half ? ' h' : ''}`, dataset: {key: f.key}}, h('label', {for: id}, lab),
        h('div', {class: 'inp sel'}, sel), err, h('div', {class: 'hint'}, 'Its balance comes off the value as your equity.'));
      sel.addEventListener('change', () => wrap.dispatchEvent(new Event('input', {bubbles: true})));
      wrap.getValue = () => sel.value;
      wrap.input = sel;
      wrap.focusFirst = () => sel.focus();
      return wrap;
    }
    if (f.type === 'chips') {
      let current = value || null;
      const btns = f.opts.map(([v, text]) => h('button', {type: 'button', role: 'radio', 'aria-checked': String(v === current),
        tabindex: v === current || (!current && v === f.opts[0][0]) ? '0' : '-1', dataset: {v}}, text));
      const pick = (b, focus) => {
        current = b.dataset.v;
        btns.forEach((x) => {
          x.setAttribute('aria-checked', String(x === b));
          x.tabIndex = x === b ? 0 : -1;
        });
        if (focus) b.focus();
        wrap.classList.remove('bad');
        err.textContent = '';
        wrap.dispatchEvent(new Event('input', {bubbles: true}));
      };
      btns.forEach((b, i) => {
        b.addEventListener('click', () => pick(b));
        b.addEventListener('keydown', (e) => {
          const step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[e.key];
          if (step) {
            e.preventDefault();
            pick(btns[(i + step + btns.length) % btns.length], true);
          }
        });
      });
      control = h('div', {class: 'chips', role: 'radiogroup', 'aria-labelledby': `l-${f.key}`, 'aria-describedby': eid}, btns);
      get = () => current;
      const wrap = h('div', {class: 'fld', dataset: {key: f.key}}, h('span', {class: 'lbl', id: `l-${f.key}`}, lab), control, err);
      wrap.getValue = get;
      wrap.focusFirst = () => (btns.find((b) => b.tabIndex === 0) || btns[0]).focus();
      return wrap;
    }
    const money = f.type === 'money';
    const shown = value == null ? '' : money ? NF2.format(value) : f.type === 'miles' ? NF0.format(value) : String(value);
    const input = h('input', {id, name: f.key, value: shown, placeholder: f.ph,
      autocomplete: 'off', inputmode: money || f.type === 'pct' ? 'decimal' : ['year', 'miles', 'zip'].includes(f.type) ? 'numeric' : 'text',
      'aria-describedby': eid, 'aria-required': f.optional ? null : 'true', maxlength: f.max ? String(f.max + (f.type === 'vin' ? 4 : 0)) : null,
      spellcheck: ['match', 'vin'].includes(f.type) ? 'false' : null, autocapitalize: f.type === 'vin' ? 'characters' : null});
    control = h('div', {class: `inp${['match', 'vin'].includes(f.type) ? ' code' : ''}`}, money ? h('span', {class: 'pre', 'aria-hidden': 'true'}, CUR.sym) : null, input,
      f.type === 'miles' ? h('span', {class: 'suf', 'aria-hidden': 'true'}, 'mi') : null,
      f.type === 'pct' ? h('span', {class: 'suf', 'aria-hidden': 'true'}, '%') : null);
    const wrap = h('div', {class: `fld${f.half ? ' h' : ''}`, dataset: {key: f.key}}, h('label', {for: id}, lab), control, err);
    input.addEventListener('input', () => {
      wrap.classList.remove('bad');
      err.textContent = '';
      input.removeAttribute('aria-invalid');
    });
    wrap.getValue = () => input.value;
    wrap.input = input;
    wrap.focusFirst = () => input.focus();
    if (f.type === 'pct' && kind === 'loan') wrap.append(h('div', {class: 'hint'}, 'Makes the payoff date exact'));
    return wrap;
  }

  // The VIN check the server does (ISO 3779; the 9th character is the North American check digit).
  const VIN_VALUES = {A: 1, B: 2, C: 3, D: 4, E: 5, F: 6, G: 7, H: 8, J: 1, K: 2, L: 3, M: 4, N: 5, P: 7, R: 9, S: 2, T: 3, U: 4, V: 5,
    W: 6, X: 7, Y: 8, Z: 9};
  const VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2];
  function vinProblem(vin) {
    if (vin.length !== 17) return `A VIN has 17 characters (this has ${vin.length})`;
    const bad = [...new Set(vin.replace(/[^IOQ]/g, ''))].sort();
    if (bad.length) return `A VIN never has the letters I, O or Q (${bad.join(', ')} here: maybe 1 or 0?)`;
    if (!/^[A-HJ-NPR-Z0-9]{17}$/.test(vin)) return 'A VIN has only letters and digits';
    const total = [...vin].reduce((t, c, i) => t + (/\d/.test(c) ? Number(c) : VIN_VALUES[c]) * VIN_WEIGHTS[i], 0);
    const check = total % 11 === 10 ? 'X' : String(total % 11);
    return vin[8] === check ? null : "That VIN doesn't check out (its 9th character, the check digit, doesn't match): look for a typo";
  }

  // Validate one field like the server does; returns [value, error].
  function checkField(f, raw, kind) {
    const year = new Date().getFullYear();
    if (f.type === 'chips') return raw ? [raw, null] : [null, `Pick a ${f.label.toLowerCase()}`];
    if (f.type === 'loan') return [raw ? Number(raw) : null, null];
    const text = String(raw == null ? '' : raw).replace(/\s+/g, ' ').trim();
    if (!text) return f.optional ? [null, null] : [null, `${f.label} is required`];
    if (f.type === 'money') {
      const n = parseAmount(text);
      if (Number.isNaN(n)) return [null, 'Enter an amount, like 1,250.00'];
      if (f.positive && n <= 0) return [null, 'Enter an amount above $0'];
      if (n > 100000000) return [null, 'That is more than $100,000,000'];
      return [Math.round(n * 100) / 100, null];
    }
    if (f.type === 'pct') {
      const n = parseAmount(text.replace(/%$/, ''));
      if (Number.isNaN(n) || n > 100) return [null, 'Enter a rate from 0 to 100'];
      return [n, null];
    }
    if (f.type === 'year') {
      const n = Number(text);
      if (!/^\d{4}$/.test(text) || n < 1900 || n > year + 1) return [null, `Enter a year from 1900 to ${year + 1}`];
      return [n, null];
    }
    if (f.type === 'vin') {
      const vin = text.replace(/\s+/g, '').toUpperCase();
      const problem = vinProblem(vin);
      return problem ? [null, problem] : [vin, null];
    }
    if (f.type === 'miles') {
      const n = parseAmount(text.replace(/\s*mi(les)?$/i, ''));
      if (Number.isNaN(n) || !Number.isInteger(n) || n > 2000000) return [null, 'Enter whole miles, like 42,000'];
      return [n, null];
    }
    if (f.type === 'zip') {
      if (!/^\d{5}(-\d{4})?$/.test(text)) return [null, 'Enter a 5-digit ZIP code, like 94110'];
      return [text, null];
    }
    if (f.max && text.length > f.max) return [null, `At most ${f.max} characters`];
    if (f.type === 'match' && text.length < 3) return [null, 'Needs at least 3 characters'];
    return [text, null];
  }

  function addForm(formKey, existing, back) {
    const F = FORMS[formKey];
    if (!F) return;
    const K = KIND[F.kind];
    const edit = Boolean(existing);
    const fields = F.f.map((f) => field(f, existing ? existing[f.key] : null, F.kind));
    const byKey = Object.fromEntries(F.f.map((f, i) => [f.key, fields[i]]));
    const formErr = h('div', {class: 'formerr', role: 'alert'});
    const preview = h('div', {class: 'prev', hidden: true});
    if (edit) {
      const bal = byKey.balance;
      bal.append(h('div', {class: 'hint'}, F.kind === 'loan' && existing.applied_count
        ? `Payments after ${fmtDate(existing.balance_as_of, true)} are already taken off. A new amount here counts from today.`
        : F.kind === 'credit_card' && existing.matched_count
          ? `Last set ${money2(existing.entered_balance)} on ${fmtDate(existing.balance_as_of, true)}, minus ${money2(existing.matched_total)} in ${plural(existing.matched_count, 'matched payment')}. Purchases don't show up here: enter your latest statement balance and it counts from today.`
          : `Last set ${fmtDate(existing.balance_as_of, true)}. Changing it saves a new balance as of today.`));
    }
    if (F.kind === 'loan') loanHelpers(byKey, preview, existing);
    if (F.kind === 'credit_card') matchHelpers(byKey, existing, 'credit_card', () => {});
    const valued = F.kind === 'vehicle' || F.kind === 'property';
    const val = valued ? valueHelpers(F, byKey, existing) : null;
    const save = h('button', {class: 'btn w', type: 'submit'}, edit ? 'Save changes' : F.save);
    const confirmBox = h('div', {class: 'confirm', hidden: true});
    const archiveBtn = edit ? h('button', {class: 'btn danger', type: 'button', onclick: () => {
      fill(confirmBox, h('b', {}, `Archive ${existing.name}?`), ` It stops counting in every total. Its history is kept.`,
        h('div', {class: 'row'}, h('button', {class: 'btn danger', type: 'button', onclick: () => archive(existing)}, 'Archive'),
          h('button', {class: 'btn ghost', type: 'button', onclick: () => { confirmBox.hidden = true; archiveBtn.focus(); }}, 'Cancel')));
      confirmBox.hidden = false;
      confirmBox.querySelector('button').focus();
    }}, icon('archive'), 'Archive') : null;
    const form = h('form', {novalidate: true}, val ? val.box : null,
      h('div', {class: 'flds'}, fields), preview, F.note ? h('div', {class: 'hint', style: {'margin-top': '14px'}}, F.note) : null,
      val ? val.note : null, formErr,
      h('div', {class: 'row'}, save, back ? h('button', {class: 'btn ghost', type: 'button', onclick: () => openAdd(back)}, 'Back')
        : h('button', {class: 'btn ghost', type: 'button', onclick: closeModal}, 'Cancel'), archiveBtn),
      confirmBox);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      formErr.textContent = '';
      const body = edit ? {} : {kind: F.kind};
      let firstBad = null;
      F.f.forEach((f, i) => {
        const [value, error] = checkField(val ? val.effective(f) : f, fields[i].getValue(), F.kind);
        if (error) {
          markBad(fields[i], error);
          firstBad = firstBad || fields[i];
        } else if (!edit || !sameValue(f, value, existing[f.key])) {
          body[f.key] = value;  // an edit sends only what changed, so a balance moved by a payment since stays
        }
      });
      if (firstBad) {
        firstBad.focusFirst();
        return;
      }
      if (edit && !Object.keys(body).length) {
        closeModal();
        toast('Nothing changed.', [], {timeout: 2500});
        return;
      }
      save.disabled = true;
      try {
        const res = await post(edit ? `/api/manual/${existing.id}` : '/api/manual', body);
        closeModal();
        const est = res.estimate_result;
        toast([edit ? 'Saved ' : 'Added ', h('b', {}, res.name), '.', res.decode_problem ? ` VIN: ${res.decode_problem}.` : '',
          est ? ` ${est.ok ? est.message : `No estimate: ${est.message}`}.` : ''], [], {timeout: est && !est.ok ? 9000 : 4500, error: Boolean(est && !est.ok)});
        refresh();
      } catch (err) {
        const target = err.field && byKey[err.field];
        if (target) {
          markBad(target, err.message);
          target.focusFirst();
        } else {
          formErr.textContent = `Could not save: ${err.message}`;
        }
      } finally {
        save.disabled = false;
      }
    });
    const title = edit ? `Edit ${existing.name}` : F.title;
    openSheet([h('div', {class: 'shd'}, back ? backBtn(() => openAdd(back)) : null, ibadge(K.i, K.c),
      h('h2', {id: 'sheet-title'}, title), closeBtn()), form], {focus: fields[0].input || null});
    form.dispatchEvent(new Event('input'));
  }

  // Vehicle / property forms (TASK-12, TASK-13b): with a VIN the year / make / model are filled from it; with what the
  // value source needs (VIN + mileage + ZIP code for Carfax, the address for Redfin) the value may be left to the
  // estimate. In the edit form, the estimate box: "Trade-in $X · Carfax · Sep 26", "Use estimate again", "Refresh".
  function valueHelpers(F, byKey, existing) {
    const provider = F.kind === 'vehicle' ? 'carfax' : 'redfin';
    const V = () => (S.accounts && S.accounts.valuation && S.accounts.valuation[provider]) || null;
    const filled = (key) => byKey[key] && String(byKey[key].getValue() || '').trim() !== '';
    const vinOk = () => filled('vin') && !checkField({type: 'vin'}, byKey.vin.getValue())[1];
    const canEstimate = () => Boolean(V() && V().enabled) && (F.kind === 'vehicle' ? vinOk() && filled('mileage') && filled('zip_code') : filled('address'));
    const effective = (f) => ((['vehicle_make', 'vehicle_model', 'vehicle_year'].includes(f.key) && vinOk())
      || (f.key === 'balance' && !existing && canEstimate()) ? {...f, optional: true} : f);
    const note = h('div', {class: 'hint', style: {'margin-top': '14px'}});
    const valHint = h('div', {class: 'hint'});
    const fromVin = ['vehicle_make', 'vehicle_model', 'vehicle_year'].filter((k) => byKey[k]).map((k) => {
      const em = h('em', {hidden: true}, '· optional');  // with a VIN, a blank one is filled from it on save
      byKey[k].querySelector('label').append(em);
      return em;
    });
    byKey.balance.append(valHint);
    if (byKey.vin) byKey.vin.append(h('div', {class: 'hint'}, 'Optional. Fills in the year, make, model and trim when you save. You can change them.'));
    const v = existing && existing.valuation;
    if (byKey.mileage && v && v.mileage_nudge) {
      byKey.mileage.append(h('div', {class: 'hint'}, `Last set ${fmtDate(existing.mileage_as_of, true)}. Update mileage for a better estimate.`));
    }
    const update = () => {
      fromVin.forEach((em) => { em.hidden = !vinOk(); });
      const P = V();
      const on = Boolean(P && P.enabled);
      const need = F.kind === 'vehicle' ? 'VIN, mileage and ZIP code' : 'address';
      fill(note, !P ? '' : on ? `${P.privacy_note}.` : F.kind === 'vehicle'
        ? `${P.off_note}. The VIN is only sent to NHTSA (the free VIN lookup) to fill in the car.` : `${P.off_note}.`);
      if (!existing) {
        fill(valHint, on ? (canEstimate() ? `Leave blank to use the ${P.label} estimate.` : `Or leave it to the ${P.label} estimate: add the ${need}.`) : '');
      } else if (v && v.source === 'estimate') {
        fill(valHint, `Follows the ${v.provider_label} estimate. A value you type here is used instead.`);
      }
      valHint.hidden = !valHint.textContent;
    };
    if (!S.accounts) ensureAccounts().then(update).catch(() => {});
    Object.values(byKey).forEach((fld) => fld.addEventListener('input', update));
    update();
    return {box: existing && v ? estimateBox(existing, v) : null, note, effective};
  }

  function estimateBox(m, v) {
    const e = v.estimate;
    const act = (label, url, done) => h('button', {class: 'btn ghost sm', type: 'button', onclick: async (ev) => {
      ev.currentTarget.disabled = true;
      try {
        const res = await post(url, {});
        closeModal();
        done(res);
        refresh();
      } catch (err) {
        ev.currentTarget.disabled = false;
        toast(`Could not ${label.toLowerCase()}: ${err.message}`, [], {error: true, timeout: 6000});
      }
    }}, label);
    const use = e && v.source === 'user' ? act('Use estimate again', `/api/manual/${m.id}/use-estimate`,
      (res) => toast(['Now using the ', h('b', {}, `${v.provider_label} estimate`), ` for ${res.name}: ${money(res.balance)}.`], [], {timeout: 4500})) : null;
    const again = v.can_refresh ? act('Refresh estimate', `/api/manual/${m.id}/refresh`, (res) => {
      const r = res.estimate_result;
      toast(r.ok ? [`${r.message}.`, r.applied ? '' : ' Your value still counts.'] : `No estimate: ${r.message}.`, [],
        {timeout: r.ok ? 4500 : 9000, error: !r.ok});
    }) : null;
    if (!e && !v.enabled) return null;
    return h('div', {class: 'valbox'},
      h('div', {class: 'vl'}, e ? [h('b', {class: 'v'}, `${e.basis_label} ${money(e.value)}`), ` · ${e.source_label} · ${fmtDate(e.date)}`]
        : h('b', {}, 'No estimate yet')),
      h('div', {class: 'mut'}, v.source === 'user' ? `Your value (${money2(m.balance)}) counts in your net worth.`
        : e ? 'Your net worth uses this estimate.' : 'Your net worth counts it at $0 until there is an estimate or a value.'),
      v.last_error ? h('div', {class: 'warn'}, `Last try ${fmtDate(v.last_error.date)}: ${v.last_error.message}.`) : null,
      use || again ? h('div', {class: 'row'}, use, again) : null,
      v.refresh_note && !v.can_refresh ? h('div', {class: 'hint'}, `${isoDays(v.refresh_note)}.`) : null);
  }

  const isoDays = (text) => text.replace(/\b\d{4}-\d{2}-\d{2}\b/g, (d) => fmtDate(d))  // "until 2026-10-01" -> "until Oct 1"
    .replace(/ \([A-Z_]+=\d in the \.env\)/, '');  // "Carfax is switched off (CARFAX_ENABLED=0 in the .env)" -> no env var text

  function sameValue(f, value, old) {
    if (value == null || old == null) return value == null && old == null;
    if (f.type === 'money') return Math.round(value * 100) === Math.round(old * 100);
    if (f.type === 'pct' || f.type === 'year' || f.type === 'loan') return Number(value) === Number(old);
    return String(value) === String(old);
  }

  function markBad(fieldEl, message) {
    fieldEl.classList.add('bad');
    fieldEl.querySelector('.ferr').textContent = message;
    if (fieldEl.input) fieldEl.input.setAttribute('aria-invalid', 'true');
  }

  async function archive(m) {
    try {
      await post(`/api/manual/${m.id}/archive`, {});
      closeModal();
      closePanel();
      toast(['Archived ', h('b', {}, acctName(m.name)), '. It no longer counts in your totals.'], [], {timeout: 4500});
      if (S.page === 'asset') location.hash = '#/accounts';  // its page is gone from the lists: back to them
      else refresh();
    } catch (e) {
      toast(`Could not archive: ${e.message}`, [], {error: true, timeout: 6000});
    }
  }

  // "Match payments from" on the loan and credit card forms: what the text matches as you type, and names seen in the
  // bank data to pick from. A card shows the dry run of the last 90 days ("This would have matched N payments ($T) in
  // the last 90 days") with match-payments' guard: a payment that pays a connected card never counts.
  // onMatch(last) gets the latest matched payment (the loan's payoff preview starts from it).
  function matchHelpers(byKey, existing, kind, onMatch) {
    const matchFld = byKey.match_pattern;
    const box = h('div', {hidden: true});
    const sugs = h('div', {class: 'sugs', hidden: true});
    matchFld.append(sugs, box);
    let timer = null;
    let req = 0;
    const idle = kind === 'loan' ? "We'll look for it in your bank transactions."
      : "Optional. Payments from your bank that contain this text lower the balance. Purchases don't show up here, so update it from a statement now and then.";
    const showMatch = async () => {
      const [pattern] = checkField({type: 'match', optional: true, max: 60}, matchFld.getValue());
      const mine = ++req;
      if (!pattern) {
        fill(box, h('div', {class: 'hint'}, idle));
        box.hidden = false;
        onMatch(null);
        return;
      }
      try {
        const d = await api('/api/manual/match', {pattern, kind, id: existing ? existing.id : null});
        if (mine !== req) return;
        onMatch(d.last);
        box.hidden = false;
        if (kind === 'credit_card') {
          const p = d.preview;
          const aside = [p.paired ? `${plural(p.paired, 'more', 'more')} ${p.paired === 1 ? 'pays' : 'pay'} a connected card and ${p.paired === 1 ? 'is' : 'are'} left out` : null,
            p.waiting ? `${p.waiting} too new to tell yet` : null, p.elsewhere ? `${p.elsewhere} already counted on another account` : null].filter(Boolean);
          fill(box, h('div', {class: `matchbox${p.count ? '' : ' none'}`}, h('span', {class: `ck ${p.count ? 'paid' : 'due'}`}, p.count ? glyph('check', 2.6) : null),
            h('span', {}, h('b', {}, `This would have matched ${plural(p.count, 'payment')} (${money2(p.total)}) in the last ${p.days} days.`),
              aside.length ? ` ${aside.join(' · ')}.` : '', h('br'),
              'New ones that contain this text lower the balance a few days after they post, never one that pays a connected card.')));
        } else if (!d.count) {
          fill(box, h('div', {class: 'matchbox none'}, h('span', {class: 'ck due'}), h('span', {},
            h('b', {}, 'No payments match yet. '), 'New ones that contain this text will lower the balance automatically.')));
        } else {
          const l = d.last;
          fill(box, h('div', {class: 'matchbox'}, h('span', {class: 'ck paid'}, glyph('check', 2.6)), h('span', {},
            h('b', {}, `${plural(d.count, 'payment')} found`), ` · ${money2(d.typical)} each from ${acctName(l.account_name) || 'your account'}`,
            l.account_mask ? ` ••${l.account_mask}` : '', ` · last on ${fmtDate(l.date)}`, h('br'),
            'Every day, new payments from this merchant lower the balance automatically.')));
        }
      } catch (e) {
        if (mine === req) fill(box, h('div', {class: 'hint'}, `Could not check: ${e.message}`));
      }
    };
    matchFld.input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(showMatch, 350);
    });
    if (!existing || !existing.match_pattern) {
      api('/api/manual/match', {kind}).then((d) => {
        if (!d.suggestions || !d.suggestions.length) return;
        fill(sugs, 'Seen in your transactions:', d.suggestions.map((x) => h('button', {type: 'button', onclick: () => {
          matchFld.input.value = x;
          matchFld.input.dispatchEvent(new Event('input', {bubbles: true}));
          clearTimeout(timer);
          showMatch();
        }}, x)));
        sugs.hidden = false;
      }).catch(() => {});
    }
    if (existing && existing.match_pattern) {
      showMatch();
    } else {
      fill(box, h('div', {class: 'hint'}, idle));
      box.hidden = false;
    }
  }

  // The loan form: "Match payments from" (matchHelpers) and the payoff preview under the form.
  function loanHelpers(byKey, preview, existing) {
    let last = null;
    matchHelpers(byKey, existing, 'loan', (l) => {
      last = l;
      updatePreview();
    });
    const val = (key) => checkField(FORMS.loan.f.find((f) => f.key === key), byKey[key].getValue(), 'loan')[0];
    function updatePreview() {
      const bal = val('balance');
      const monthly = val('monthly_payment');
      const orig = val('original_amount');
      const apr = val('interest_rate');
      if (bal == null || !monthly) {
        preview.hidden = true;
        return;
      }
      let first = addMonths(thisMonth(), 1);
      if (last) {
        const d = asDate(last.date);
        d.setMonth(d.getMonth() + 1);
        while (d < new Date()) d.setMonth(d.getMonth() + 1);
        first = `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
      }
      const p = payoffOf(bal, apr, monthly, first);
      const paid = orig ? clamp((orig - bal) / orig, 0, 1) : null;
      const head = p.method === 'paid' ? 'Paid off' : p.method === 'never' ? "This payment doesn't cover the interest"
        : `${paid != null ? `${Math.round(paid * 100)}% paid off · ` : ''}about ${plural(p.n, 'payment')} left`;
      const sub = p.month ? `Paid off around ${monthYear(p.month)}. ${apr ? `Counted with ${apr}% APR.` : 'Add the interest rate to make this exact.'}`
        : p.method === 'never' ? 'Check the interest rate and monthly payment.' : '';
      fill(preview, paid != null ? ringPct(paid, ACC, 54, 6, 13) : ibadge('flag', RGB.acc),
        h('div', {}, h('b', {}, head), h('div', {class: 'mut', style: {'font-size': '14px'}}, sub)));
      preview.hidden = false;
    }
    ['balance', 'monthly_payment', 'original_amount', 'interest_rate'].forEach((k) => byKey[k].input.addEventListener('input', updatePreview));
    setTimeout(updatePreview, 0);
  }

  // ---------- loan panel -----------------------------------------------------------------------
  let panelReturn = null;
  async function openLoan(id) {
    let d = S.debt;
    let loan = d && d.loans.find((l) => l.manual && l.id === id);
    if (!loan) {
      d = S.debt = await api('/api/debt');
      loan = d.loans.find((l) => l.manual && l.id === id);
    }
    if (!loan) return;
    destroyCharts($('panel'));
    panelReturn = document.activeElement;
    const p = $('panel');
    const later = [];
    const paid = loan.paid_pct != null ? loan.paid_pct / 100 : null;
    const po = loan.payoff;
    const [box, canvas] = chartBox('ch-loan', `${loan.name}: balance over time`);
    const hist = loan.history.map((x) => ({date: x.date, value: x.balance}));
    later.push(() => loanChart(canvas, hist, loan));
    const detail = (label, value, cls = '') => h('div', {class: cls}, h('span', {}, label), h('b', {}, value));
    const payRow = (x) => h('div', {class: 'li'},
      h('span', {class: `ck ${x.applied ? 'paid' : x.included ? 'inc' : 'due'}`, title: x.applied ? 'Taken off the balance'
        : x.included ? 'Already in the balance you entered' : 'Not applied yet'}, x.applied || x.included ? glyph('check', 2.6) : null),
      h('span', {class: 'grow'}, h('b', {}, fmtDate(x.date, true)),
        h('span', {}, `${x.name || x.merchant_name || ''}${x.account_name ? ` · ${acctName(x.account_name)}` : ''}${x.account_mask ? ` ••${x.account_mask}` : ''}`),
        h('span', {}, x.applied ? (loan.apr ? `Interest ${money2(x.interest)} · principal ${money2(x.principal)}`
          : 'Whole payment counted as principal') : x.included ? 'Included in the balance you entered' : 'Not applied yet')),
      h('span', {class: 'amt v'}, money2(x.amount)));
    fill(p,
      h('div', {class: 'shd'}, ibadge(KIND.loan.i, KIND.loan.c, 'l'),
        h('div', {style: {flex: '1', 'min-width': '0'}}, h('h2', {id: 'panel-title'}, loan.name, manualTag()),
          h('div', {class: 'mut', style: {'font-size': '14px'}}, [loan.type_label, loan.institution].filter(Boolean).join(' · '))),
        h('button', {class: 'ib', type: 'button', 'aria-label': 'Close', onclick: closePanel}, icon('x'))),
      h('div', {style: {'margin-top': '22px'}}, bigMoney(loan.balance, 'md'),
        h('div', {class: 'sub'}, loan.original_amount ? [`left of ${money2(loan.original_amount)} · `, h('b', {class: 'accc'}, `${Math.round(loan.paid_pct)}% paid off`)] : 'left to pay'),
        paid != null ? track(paid, 'linear-gradient(90deg,#6f8cff,#8ea8ff)', `${Math.round(paid * 100)}% paid off`, loan.paid_off ? 'win' : '') : null),
      loan.paid_off ? h('div', {class: 'win-t', style: {'padding-left': '0'}}, icon('party'),
        `Congratulations! 100% of ${money(loan.original_amount || 0)} paid off`) : null,
      h('h4', {}, icon('linechart'), 'Balance over time'), hist.length ? box : msgBox('No history yet', 'It starts with the balance you entered.'),
      h('div', {class: 'hint', style: {'margin-top': '8px'}}, loan.apr
        ? `Dots are real balances. The dashed line projects the payoff at ${loan.apr}% APR.`
        : 'Dots are real balances. The dashed line projects the payoff; interest is left out until you add the APR.'),
      h('h4', {}, icon('pencil'), 'Loan details'),
      h('div', {class: 'fg'},
        detail('Nickname', loan.name), detail('Provider', loan.institution || '—'), detail('Loan type', loan.type_label),
        h('div', {}, h('span', {}, 'Interest rate'), loan.apr != null ? h('b', {}, `${loan.apr}%`)
          : h('b', {}, tag('not set', '', 'APR not set'), ' ', h('button', {class: 'lnk', type: 'button', onclick: () => openAdd(`edit:${loan.id}`)}, 'Add'))),
        detail('Original amount', loan.original_amount ? money2(loan.original_amount) : '—'),
        detail('Amount remaining', money2(loan.balance)),
        detail('Monthly payment', loan.monthly_payment ? money2(loan.monthly_payment) : '—'),
        h('div', {}, h('span', {}, 'Paid off by'), h('b', {class: 'accc'}, po.payoff_month ? `~${monthYear(po.payoff_month)}`
          : po.method === 'paid' ? 'Paid off' : po.method === 'never' ? 'Payment too low' : '—')),
        h('div', {class: 'w2'}, h('span', {}, 'Match payments from'),
          loan.match_pattern ? h('b', {}, h('span', {class: 'mono'}, loan.match_pattern)) : h('b', {class: 'mut'}, 'Not set')),
        h('div', {class: 'w2'}, h('span', {}, 'Balance entered'), h('b', {}, `${fmtDate(loan.balance_as_of, true)}${loan.applied_count
          ? ` · ${plural(loan.applied_count, 'payment')} taken off since` : ''}`))),
      loan.apr == null ? h('div', {class: 'hint'}, 'APR not set, whole payment counted as principal.') : null,
      h('h4', {}, icon('checkc'), 'Matched payments', loan.payments.length ? h('span', {class: 'pill sm', style: {'margin-left': 'auto'}},
        `${loan.payments.length} found`) : null),
      loan.payments.length ? loan.payments.map(payRow) : h('p', {class: 'mut', style: {'font-size': '14px'}},
        loan.match_pattern ? 'No bank payments match yet.' : 'Add "Match payments from" to find its payments in your bank transactions.'),
      h('div', {class: 'row'},
        h('button', {class: 'btn w', type: 'button', onclick: () => openAdd(`edit:${loan.id}`)}, 'Edit loan'),
        loan.paid_off ? null : h('button', {class: 'btn ghost w', type: 'button', onclick: () => markPaidOff(loan)}, 'Mark as paid off')));
    p.hidden = false;
    $('dscrim').classList.add('on');
    p.scrollTop = 0;
    later.forEach((fn) => fn());
    p.querySelector('.ib').focus();
  }

  function closePanel() {
    const p = $('panel');
    if (p.hidden) return;
    destroyCharts(p);
    p.hidden = true;
    $('dscrim').classList.remove('on');
    if (panelReturn && panelReturn.isConnected) panelReturn.focus();
    panelReturn = null;
  }

  function markPaidOff(loan) {
    modal(`Mark ${loan.name} as paid off?`, h('p', {}, `Its balance becomes $0 as of today. You can change it later.`), [
      {label: 'Mark as paid off', primary: true, onclick: async () => {
        try {
          await post(`/api/manual/${loan.id}`, {balance: 0});
          closeModal();
          closePanel();
          toast(['Congratulations! ', h('b', {}, loan.name), ' is paid off.'], [], {timeout: 4500});
          refresh();
        } catch (e) {
          toast(`Could not save: ${e.message}`, [], {error: true, timeout: 6000});
        }
      }},
      {label: 'Cancel', onclick: closeModal},
    ]);
  }

  // Real balances (dots), then a dashed projection month by month to $0.
  function loanChart(canvas, hist, loan) {
    const proj = [];
    const pay = loan.monthly_payment;
    if (pay && loan.balance > 0 && loan.payoff.payoff_month) {
      let b = Math.round(loan.balance * 100);
      const rate = loan.apr ? loan.apr / 1200 : 0;
      let m = loan.next_payment ? loan.next_payment.slice(0, 7) : addMonths(thisMonth(), 1);
      for (let k = 0; k < 360 && b > 0; k++) {
        b = Math.max(0, b + Math.round(b * rate) - Math.round(pay * 100));
        proj.push({date: `${m}-01`, value: b / 100});
        m = addMonths(m, 1);
      }
    }
    const labels = [...hist.map((x) => fmtDate(x.date, true)), ...proj.map((x) => monthYear(x.date))];
    chart(canvas, {
      type: 'line',
      data: {labels, datasets: [
        {label: 'Balance', data: [...hist.map((x) => x.value), ...proj.map(() => null)], borderColor: ACC, borderWidth: 2.6, fill: 'origin',
          backgroundColor: gradFill(RGB.acc, 0.3), tension: 0.2, pointRadius: 3, pointBackgroundColor: ACC},
        {label: 'Projected', data: [...hist.map((x, k) => (k === hist.length - 1 ? x.value : null)), ...proj.map((x) => x.value)],
          borderColor: '#7d869c', borderDash: [5, 5], borderWidth: 2, pointRadius: 0, tension: 0.2},
      ]},
      options: {interaction: {mode: 'index', intersect: false},
        scales: {x: {grid: {display: false}, border: {display: false}, ticks: {maxTicksLimit: 5, maxRotation: 0}},
          y: yMoney({ticks: {maxTicksLimit: 4, callback: kf}})},
        plugins: {legend: {display: false}, tooltip: {callbacks: {label: (c) => (c.parsed.y == null ? '' : ` ${c.dataset.label}: ${money2(c.parsed.y)}`)}}}},
    });
  }

  // ---------- Overview -------------------------------------------------------------------------
  async function renderOverview(tk) {
    const page = $('page');
    fill(page, skeleton(['card s2', 'stack', 'card', 'card', 'card', 'card', 'card', 'card', 'card s2', 'card']));
    const d = await api('/api/overview', {month: S.month});
    if (tk !== S.token) return;
    const later = [];
    CHART.hidden = new Set(d.chart.excluded);
    CHART.fixed = d.chart.fixed_categories;
    const spent = spentCard(d, later);
    const stack = h('div', {class: 'stack'}, leftCard(d), paceCard(d));
    const budgets = budgetsCard(d);
    const where = whereCard(d, later);
    const bills = billsCard(d);
    const flow = flowCard(d, later);
    const upcoming = upcomingCard(d);
    upcoming.dataset.flex = '';  // beside Income vs spending (3 columns), or its own full row (2 columns)
    const recent = recentCard(d);
    spent.dataset.pin = 'start';  // Spent stays top-left, Left to spend / On pace for top-right
    stack.dataset.pin = 'end';
    // The three tiles (net worth, investments, loans & debt) are one full-width row, so they always line up.
    const tiles = h('div', {class: 'tiles s3'}, worthTile(d), investTile(d), debtTile(d), HOOKS.tiles(d));
    // Wider screens and phones read the same order as the 005 mockup: the tiles, then Budgets, Where it went,
    // Bills, then Income vs spending with Coming up beside it, Recent transactions last.
    [spent, stack, tiles, budgets, where, bills, flow, upcoming, recent].forEach((el, i) => { el.dataset.wide = String(i); });
    const grid = h('div', {class: 'grid'}, spent, stack, tiles, budgets, where, bills, flow, upcoming, recent);
    fill(page, alertsStrip(d), grid);
    later.forEach((fn) => fn());  // charts need their canvas in the DOM
    pack(grid);
  }

  function alertsStrip(d) {
    const items = d.alerts.map((a) => {
      if (a.kind === 'cap') {
        const over = a.level === 'over';
        const due = a.due || 0;  // its bills still due this month count before they post
        const when = due ? ' once bills clear' : '';
        const grp = a.category == null && a.group != null;   // polish P2e: a group budget's alert
        return h('a', {class: `al ${over ? 'bad' : 'warn'}`, href: hashFor('transactions', grp ? {group: a.group} : {category: a.category}),
          dataset: grp ? {group: a.group} : {}},
          grp ? ibadge(GROUP_ICON[a.group] || 'tag', over ? RGB.bad : RGB.warn) : ibadge(CATI[a.category] || 'tag', over ? RGB.bad : RGB.warn),
          h('span', {}, h('b', {}, grp ? a.group : a.category), over ? [` is over budget${when} · `, h('b', {class: 'bad'},
            `${due ? 'over' : 'overspent'} by ${money(a.spend + due - a.cap)}`)]
            : ` is at ${ringPctOf(a.spend + due, a.cap)}% of its budget${when}`));
      }
      const rest = a.message.startsWith(a.institution_name) ? a.message.slice(a.institution_name.length) : `: ${a.message}`;
      return h('div', {class: 'al bad'}, ibadge('alert', RGB.bad), h('span', {}, h('b', {}, a.institution_name), rest),
        reconnectBtn(a.item_id));
    });
    for (const c of d.cards_due || []) {
      const k = daysFrom(c.due, d.today);
      items.push(h('a', {class: 'al warn', href: '#/debt'}, ibadge('card', RGB.warn),
        h('span', {}, h('b', {}, `${instShort(c.institution_name) || c.name} card`), ` · ${money2(c.amount)} minimum due `,
          k === 0 ? 'today' : k === 1 ? 'tomorrow' : `in ${k} days`)));
    }
    if (!d.connections.broken) {  // TASK-22f: every connection (the Plaid banks and Alpaca), never "9 banks"
      const n = d.connections.total;
      items.push(h('a', {class: 'al ok', href: '#/accounts'}, ibadge('checkc', RGB.ok),
        h('span', {}, n ? `All ${plural(n, 'connection')} healthy` : 'No banks connected yet')));
    }
    // t_3d65332a: a broken connection (it has a Reconnect button) comes first: it's the one that needs you
    items.sort((a, b) => (b.tagName === 'DIV') - (a.tagName === 'DIV'));  // stable: the rest keep their order
    return h('div', {class: 'alerts'}, items);
  }

  function spentCard(d, later) {
    const s = d.summary;
    const pm = monthName(s.prev_month);
    const range = s.is_current ? `${monthShort(s.month)} 1–${s.day} · ${plural(s.days_left, 'day')} left` : s.day ? 'whole month' : 'not started';
    let chip = null;
    if (s.day) {
      const diff = s.delta;
      const when = s.is_current ? `by day ${s.day}` : 'in total';
      chip = Math.abs(diff) < 0.5 ? pill('nt', null, `Same as ${pm} ${when}`)
        : pill(diff > 0 ? 'bad' : '', diff > 0 ? 'trend' : 'income', `${money(Math.abs(diff))} ${diff > 0 ? 'more' : 'less'} than ${pm} ${when}`);
    }
    const pending = s.pending_count ? pill('nt sm', null, `${money2(s.pending_spend)} pending`) : null;
    const enough = s.cum.length >= 2 || s.cum_prev.some((v) => v !== 0);
    let body;
    if (enough) {
      const [box, canvas] = chartBox('ch-cum fillh', `Cumulative spending in ${monthName(s.month)} compared with ${pm}`);
      body = box;
      later.push(() => cumChart(canvas, s));
    } else {
      body = emptyChart('Not enough data yet', 'The chart shows up once there are two days of spending to compare.');
    }
    return card({cls: 's2 grow', title: `Spent in ${monthName(s.month)}`, icon: 'trend', rgb: RGB.acc, extra: small(range)},
      h('div', {class: 'herohead'}, heroMoney(s.mtd, 'spent'), chip), pending, body);
  }

  function leftCard(d) {
    const b = d.budgets;
    if (!b.count) {
      return card({title: 'Left to spend', icon: 'wallet', rgb: RGB.ok, extra: small('no budgets yet')},
        h('div', {class: 'big md'}, '—'),
        h('p', {class: 'sub'}, 'Set monthly budgets to see what is left. ', h('a', {href: hashFor('budgets', {})}, 'Suggested budgets are on Budgets →')));
    }
    const helps = [];
    const lines = leftLines(b, helps);
    // No `extra` in the header: the header already carries the icon badge, the title and the bills trigger, and a
    // fourth item ("in your budgets") makes h3 wrap at 1280 and 1024, which drops this card's hero out of line with
    // the cards beside it. The note reads as part of the body text it describes instead.
    return card({title: 'Left to spend', icon: 'wallet', rgb: RGB.ok, help: helps},
      h('div', {style: {display: 'flex', 'align-items': 'center', gap: '16px'}},
        h('div', {class: 'grow'}, heroMoney(b.left, 'left', `md${b.left < 0 ? ' bad' : ''}`), lines),
        budgetRing(b.total_spend, b.total_due, b.total_cap, 70, 7, 15)));
  }

  // "Left to spend" is the net: budgeted − spent − the capped categories' bills still due. Under it: the allowance a
  // day (or how far over), the bills it counts, and how it splits ("$829 left in 4 budgets · $538 over in 3"). The two
  // explanations land in the card header (via `helps`), next to the title, not floating in the body.
  function leftLines(b, helps) {
    const over = b.left < 0;
    let head;
    if (over) head = [h('b', {class: 'bad'}, `Over budget by ${money(-b.left)}`), b.total_due ? ' once bills clear' : ''];
    else if (b.per_day == null) head = [`Left unspent across ${plural(b.count, 'budget')}`];
    else head = ['About ', h('b', {}, `${money(b.per_day)} a day`), ` for the ${plural(b.days_left, 'day')} left`];
    const bills = b.total_due ? h('div', {class: 'sub'}, h('span', {}, h('b', {}, money(b.total_due)), ' in bills still due')) : null;
    const split = [`${money(b.under)} left in ${plural(b.under_count, 'budget')}`,
      b.over ? [' · ', h('span', {class: 'bad'}, `${money(b.over_amount)} over in ${b.over}`)] : null];
    // Only the compact icon trigger goes in the card header (`helps`). The labelled "Budget breakdown" button is
    // ~140px wide: headered next to the title and the icon trigger it does not fit a card's content width, and h3
    // wraps, so the header grows a line and this card's hero drops out of line with the cards beside it. It belongs
    // in the body, under the line it explains, where it fits.
    if (b.total_due) helps.push(help('Bills due in your budgets', () => helpBills(b.bills_due), {key: 'left-bills', long: true}));
    const breakdown = help('Budget breakdown', () => h('p', {}, split), {key: 'left-split', long: true, text: 'Budget breakdown'});
    return [h('div', {class: 'sub'}, head), bills,
      h('div', {class: 'sub'}, 'in your budgets'), h('div', {class: 'help-line'}, breakdown)];
  }

  function paceCard(d) {
    const p = d.pace;
    const s = d.summary;
    if (!s.is_current) {
      return card({title: s.day ? 'Month total' : 'On pace for', icon: 'gauge', rgb: RGB.warn}, bigMoney(p.projection, 'md'),
        h('p', {class: 'sub'}, s.day ? `Final spending for ${monthName(s.month)}. ${monthName(s.prev_month)} was ${money(p.prev_total)}.`
          : 'This month has not started yet.'));
    }
    const end = `${monthShort(s.month)} ${s.days_in_month}`;
    const scale = Math.max(p.projection, p.prev_total, 1) * 1.06;
    const mark = p.prev_total / scale;
    const bar = h('div', {class: 'pbar', role: 'img', 'aria-label':
      `Spent ${money(p.mtd)} so far, projected ${money(p.projection)}, ${monthName(s.prev_month)} total ${money(p.prev_total)}`},
    h('b', {style: {width: pct(p.projection / scale)}}), h('i', {style: {width: pct(p.mtd / scale)}}),
    p.prev_total > 0 ? [h('s', {style: {left: pct(mark)}}),
      h('em', {class: mark > 0.8 ? 'end' : '', style: {left: pct(mark)}}, `${monthName(s.prev_month)}: ${kf(p.prev_total)}`)] : null);
    const under = p.prev_total - p.projection;
    // An estimate, and the copy says so: the pace is this month's day-to-day spending so far, not a long-run average.
    // One-time purchases (insights._one_offs) count once and are listed, so a big buy doesn't inflate the number.
    const oneOffs = p.one_offs || [];
    const paceHelp = help('Spending projection', () => [
      h('p', {}, h('b', {}, money(p.mtd)), ' spent + ', h('b', {}, money(p.bills_due_total)), ' bills + ~',
        h('b', {}, money(p.pace_add)), ' at your usual pace.'),
      oneOffs.length ? [h('h4', {}, 'One-time, counted once'), helpBills(oneOffs.map((o) =>
        ({merchant: o.merchant, next_expected: o.date, amount: o.amount})))] : null,
      p.bills_due.length ? [h('h4', {}, 'Still due'), helpBills(p.bills_due)] : null], {key: 'pace', long: true});
    return card({title: 'On pace for', icon: 'gauge', rgb: RGB.warn, extra: small(`by ${end}`), help: paceHelp},
      bigMoney(p.projection, 'md'), bar,
      h('div', {class: 'sub'}, h('span', {}, 'Includes ', h('b', {}, money(p.bills_due_total)), ' in bills to come')),
      under > 0.5 ? h('div', {class: 'sub pos'}, `${money(under)} under ${monthName(s.prev_month)}.`) : null);
  }

  function worthTile(d) {
    const big = bigMoney(0);
    const bar = h('div', {class: 'split', style: {height: '8px'}});
    const tl2 = h('div', {class: 'tl2'});
    const transit = h('div');
    const tile = card({cls: 'tile nwtile', href: '#/accounts', title: 'Net worth', icon: 'landmark', rgb: RGB.ok,
      extra: [nwSeg(), h('span', {class: 'lnk end', 'aria-hidden': 'true'}, icon('chevr'))]}, big, bar, tl2, transit);
    return nwView('tile', tile, () => {
      const w = worthOf(d.net_worth);
      fill(big, splitMoney(w.net));
      countUp(big, w.net, 'networth');
      fill(bar, ownOwe(w));
      fill(tl2, h('span', {}, 'Own ', h('b', {class: 'pos v'}, money(w.assets))), h('span', {}, 'Owe ', h('b', {class: 'bad v'}, money(w.debts))));
      fill(transit, transitLines(d.net_worth));
    });
  }

  const HOLD_COLORS = ['#7bd88f', '#8ea8ff', '#5fc6e8', '#c792ea', '#f5b454', '#ff8fb1', '#a3a8ff'];
  // cash grey; the balance/holdings residual a quieter grey
  const holdColor = (x, i) => (x.residual ? '#646b80' : x.cash ? '#9ba2b6' : HOLD_COLORS[i % HOLD_COLORS.length]);

  function investTile(d) {
    const inv = d.investments;
    const sl = inv.slices;
    const top = sl.find((x) => !x.cash && !x.residual);
    const cash = sum(sl.filter((x) => x.cash), (x) => x.value);
    const of = inv.priced_total || inv.total;  // hotfix 2026-10-05: shares of the priced total, never past 100%
    // (TASK-20) short enough for the approved four-tile row (Goals is the fourth from 1360px): never on two lines
    const history = inv.days < 2 ? pill('nt sm', 'sprout', inv.days ? 'History starts today' : 'History after a sync')
      : inv.change_7d ? pill(`sm ${inv.change_7d.amount < 0 ? 'bad' : ''}`, inv.change_7d.amount < 0 ? 'income' : 'trend',
        `${inv.change_7d.amount < 0 ? '' : '+'}${money2(inv.change_7d.amount)} past 7 days`) : null;
    return card({cls: 'tile', href: '#/investments', title: 'Investments', icon: 'linechart', rgb: RGB.acc,
      extra: h('span', {class: 'lnk end', 'aria-hidden': 'true'}, icon('chevr'))},
    bigMoney(inv.total),
    h('div', {class: 'stk', style: {height: '8px', margin: '12px 0 8px'}},
      of > 0 ? sl.map((x, i) => h('i', {style: {width: `${(x.value / of) * 100}%`, background: holdColor(x, i)}})) : null),
    h('div', {class: 'tl2'}, of > 0 && top ? h('span', {}, h('b', {}, top.label), ` ${((top.value / of) * 100).toFixed(1)}%`,
      cash ? ` · cash ${money2(cash)}` : '') : h('span', {}, 'Nothing invested yet'), history));
  }

  function debtTile(d) {  // TASK-22f: its total counts each payment in transit as paid (as net worth's Owe), with its line
    const t = d.debt;
    const paid = t.loan ? t.loan.paid_pct / 100 : null;
    return card({cls: 'tile', href: '#/debt', title: 'Loans & debt', icon: 'handcoins', rgb: RGB.warn,
      extra: h('span', {class: 'lnk end', 'aria-hidden': 'true'}, icon('chevr'))},
    bigMoney(t.total),
    paid != null ? track(paid, 'linear-gradient(90deg,#6f8cff,#8ea8ff)', `${t.loan.name}: ${Math.round(paid * 100)}% paid off`, '',
    ) : h('div', {class: 'track', style: {opacity: '.5'}}),
    h('div', {class: 'tl2'}, t.loan ? h('span', {}, `${t.loan.name} `, h('b', {}, `${Math.round(t.loan.paid_pct)}% paid off`))
      : h('span', {}, t.count ? plural(t.count, 'account') : 'Nothing owed'),
    t.cards_util != null ? h('span', {}, `Cards ${Math.round(t.cards_util)}% used`) : null),
    (t.in_transit || []).map(transitLine));
  }

  function budgetsCard(d) {
    const b = d.budgets;
    // polish P2e: the rows are the budgets the totals count, each once: a group budget is one row (its tile and name),
    // and the caps inside it aren't rows of their own. The same order as the caps: % used, highest first
    // (insights.budgets' key: -pct, a missing % first, then the name by code point)
    const pctKey = (c) => (c.pct != null ? -c.pct : -Infinity);
    const nameOf = (c) => (c.category != null ? c.category : c.group);
    const byPct = (x, y) => (pctKey(x) - pctKey(y)) || (nameOf(x) < nameOf(y) ? -1 : nameOf(x) > nameOf(y) ? 1 : 0);
    const caps = (b.group_budgets || []).length
      ? [...b.categories.filter((c) => c.counted !== false), ...b.group_budgets].sort(byPct).slice(0, 5) : b.categories.slice(0, 5);
    if (!caps.length) {
      return card({title: 'Budgets', icon: 'target', rgb: RGB.bad, extra: lnk(hashFor('budgets', {}), 'Budgets')},
        msgBox('No budgets yet', 'Pick them on Budgets. Suggestions are ready there.'));
    }
    const rows = caps.map((c) => {
      const st = c.status;  // from spent + the bills still due in it
      const grp = c.category == null;
      return h('a', {class: 'brow', href: hashFor('transactions', grp ? {group: c.group} : {category: c.category}),
        dataset: grp ? {group: c.group} : {}},
        grp ? gbadge(c.group, c.rgb, 's') : badge(c.category, 's'),
        // The space before the cap is non-breaking: with a real overspent budget ("$56 of $50") the tablet row wrapped
        // and stranded "$50" alone on the last line (the live layout audit's orphans check, 1280 only).
        // Option A (2026-10-20): name | figures on one line, bar | left under them: two lines a row, not three
        h('span', {class: 'grow'},
          h('span', {class: 'bl1'}, h('b', {}, grp ? c.group : c.category), h('span', {class: 'amt'}, h('b', {class: `v ${st === 'over' ? 'bad' : ''}`}, money(c.spend)), ` of\u00a0${money(c.cap)}`,
            c.due ? ` · ${money(c.due)} still due` : '')),
          h('span', {class: 'bl2'}, hairline(c.spend, c.due, c.cap),
            // Left as plain text (a pill's "Overspent by $40" took a line of its own); "$X over" mirrors "$X left": cap - spent - due
            st === 'over' ? h('span', {class: 'bad v lft'}, `${money(-c.left)} over`)
              : h('span', {class: `${st === 'warn' ? 'warn' : 'mut'} v lft`}, `${money(c.left)} left`))));
    });
    const ok = b.count - b.over - b.warn;
    return card({title: 'Budgets', icon: 'target', rgb: RGB.bad, extra: lnk(hashFor('budgets', {}), 'All budgets')}, rows,
      h('div', {class: 'bfoot'}, [['bad', 'over', b.over], ['warn', 'almost there', b.warn], ['', 'on track', ok]].map(([k, l, n]) =>
        h('span', {class: `bc ${k}`}, h('b', {class: 'v'}, String(n)), l))));
  }

  const CHART = {hidden: new Set(), fixed: []};

  function whereCard(d, later) {
    return HOOKS.whereCard(d, later);
  }

  function billsCard(d) {
    const month = d.today.slice(0, 7);
    const bills = d.bills.filter((r) => r.paid_this_month || r.next_expected.slice(0, 7) === month)
      .sort((a, b) => (a.paid_this_month - b.paid_this_month) || (a.paid_this_month ? a.last_date.localeCompare(b.last_date)
        : a.next_expected.localeCompare(b.next_expected)));
    const paid = bills.filter((r) => r.paid_this_month);
    const toGo = bills.filter((r) => !r.paid_this_month).reduce((s, r) => s + r.amount, 0);  // a changing bill: its estimate
    const rows = bills.map((r) => h('div', {class: 'li'},
      h('span', {class: `ck ${r.paid_this_month ? 'paid' : 'due'}`, title: r.paid_this_month ? 'Paid' : 'Due'}, r.paid_this_month ? glyph('check', 2.6) : null),
      avatar(r.merchant, r.logo_url, r.icon_url, 24),
      h('span', {class: 'grow'}, h('b', {}, r.merchant),
        h('span', {class: r.status === 'late' && !r.paid_this_month ? 'bad' : ''}, r.paid_this_month ? `Paid ${fmtDate(r.last_date)}`
          : relativeDate(r.next_expected, d.today, r.status === 'late', true))),
      h('span', {class: 'amt v'}, r.paid_this_month ? money2(r.last_amount) : billAmt(r))));
    const head = bills.length ? [h('div', {style: {display: 'flex', 'justify-content': 'space-between', 'font-size': '14px', margin: '-4px 0 8px'}},
      h('span', {}, h('b', {class: 'pos'}, `${paid.length} of ${bills.length}`), h('span', {class: 'mut'}, ' paid')),
      h('span', {class: 'mut'}, h('b', {class: 'v', style: {color: 'var(--fg)'}}, money2(toGo)), ' to go')),
    h('div', {class: 'segs', 'aria-hidden': 'true'}, bills.slice().sort((a, b) => b.paid_this_month - a.paid_this_month)
      .map((r) => h('i', {class: r.paid_this_month ? 'on' : ''})))] : null;
    const note = S.month !== month ? h('p', {class: 'note'}, `Showing ${monthName(month)}, the current month.`) : null;
    return card({title: 'Bills this month', icon: 'receipt', rgb: RGB.cyan}, head,
      rows.length ? rows : msgBox('No bills found yet', 'Bills show up after a few charges from the same payee, or track one from a transaction.'), note);
  }

  function flowCard(d, later) {
    const f = d.cashflow;
    const months = f.months;
    const withData = months.filter((m) => m.income || m.spending).length;
    let body;
    if (withData >= 2) {
      const [box, canvas] = chartBox('ch-flow fillh', 'Income and spending for the last 6 months');
      body = box;
      later.push(() => flowChart(canvas, months));
    } else {
      body = emptyChart('Not enough history yet', 'This compares months once there are two with data.');
    }
    const cm = months[months.length - 1];
    const net = cm.income - cm.spending;
    const head = withData ? h('div', {style: {margin: '-4px 0 6px'}}, net >= 0
      ? pill('', 'piggy', `${money(net)} saved${cm.is_current ? ' so far' : ''} in ${monthName(cm.month)}`)
      : pill('bad', 'alert', `${money(-net)} more out than in`)) : null;
    const start = `${months[0].month}-01`;
    const late = Object.entries(f.first_dates).filter(([, first]) => first > start).sort((a, b) => b[1].localeCompare(a[1]));
    const historyHelp = late.length && withData >= 2 ? help('Incomplete history', () => [h('p', {}, 'Faded bars are incomplete months. Bank history starts on:'),
      h('dl', {class: 'help-facts'}, late.map(([bank, first]) => h('div', {}, h('dt', {}, bank), h('dd', {}, fmtDate(first, true)))))],
    {key: 'history', long: true}) : null;
    return card({cls: 's2 grow', title: 'Income vs spending', icon: 'bars', rgb: RGB.ok, extra: small('last 6 months'), help: historyHelp}, head, body);
  }

  function upcomingCard(d) {
    const rows = d.upcoming.slice(0, 6).map((r) => {
      const k = daysFrom(r.next_expected, d.today);
      return h('div', {class: 'li'}, cal(r.next_expected, k <= 1 ? 'hot' : ''),
        h('span', {class: 'grow'}, h('b', {}, r.merchant), h('span', {}, relativeDate(r.next_expected, d.today, false, true))),
        h('span', {class: 'amt v'}, billAmt(r)));
    });
    return card({title: 'Coming up', icon: 'calendar', rgb: RGB.acc, extra: small('next 30 days')},
      rows.length ? rows : msgBox('Nothing due', 'No recurring charges expected in the next 30 days.'),
      d.upcoming.length > 6 ? h('div', {style: {'margin-top': '10px'}}, lnk(hashFor('recurring', {}), `All ${d.upcoming.length} on Recurring`)) : null);
  }

  function txLine(t) {
    return h('div', {class: 'li'}, avatar(t.seed, t.logo_url, t.icon_url, 32),
      h('span', {class: 'grow'},
        h('b', {title: t.name || ''}, t.merchant || '—', t.pending ? h('span', {class: 'pend'}, 'pending') : null),
        h('span', {style: {'white-space': 'normal', overflow: 'visible'}}, badge(t.category, 'xs'), ' ', `${t.category} · ${fmtDate(t.date).replace(' ', '\u00a0')}`)),
      h('span', {class: `amt v${t.amount < 0 ? ' pos' : ''}`}, signed(t.amount)));
  }

  function recentCard(d) {
    return card({cls: 's3', title: 'Recent transactions', icon: 'swap', rgb: RGB.grey, extra: lnk(hashFor('transactions', {}), 'See all')},
      d.recent.length ? h('div', {class: 'txgrid'}, d.recent.map(txLine)) : msgBox('No transactions yet', `Nothing in ${monthName(d.month)}.`));
  }

  // ---------- Transactions ---------------------------------------------------------------------
  const TX = {rows: [], total: 0, limit: 50, req: 0, filters: null, el: null, day: null};
  // polish P1: the picks that may repeat (any of them matches); the columns a header sorts by (the first click on one
  // goes its natural way: newest, A to Z, the biggest spend); an order is `${column}_${asc|desc}`, newest first by default
  const TX_LISTS = ['category', 'group', 'tag'];
  const TX_COLS = [['date', 'Date', 'c-date', 'desc'], ['merchant', 'Merchant', 'c-m', 'asc'], ['account', 'Account', 'c-acct', 'asc'],
    ['category', 'Category', 'c-cat', 'asc'], ['amount', 'Amount', 'num', 'desc']];
  const TX_SORTS = TX_COLS.flatMap(([c]) => [`${c}_desc`, `${c}_asc`]);
  const txParams = (f) => ({q: f.q, category: f.category, group: f.group, uncategorized: f.uncategorized ? '1' : '', bank: f.bank,
    since: f.since, until: f.until, window: f.window, txmonth: f.txmonth, tag: f.tag, kind: f.kind === 'all' ? '' : f.kind, sort: f.sort === 'date_desc' ? '' : f.sort});
  const txFiltered = (f) => Boolean(f.q || f.category.length || f.group.length || f.uncategorized || f.bank || f.tag.length
    || f.kind !== 'all');

  // Window state lives in the hash. `month` remains the return context for ranges and automatic search.
  const txAutoAll = (f) => Boolean(f.q && !f.since && !f.until && !f.window && !f.txmonth);
  const txWindowParams = (f = TX.filters) => f.since || f.until
    ? {since: f.since, until: f.until} : f.window === 'all' || txAutoAll(f) ? {} : {month: S.month};
  let windowResult = null;
  let windowPicker = null;
  function closeTxWindow() {
    if (!windowPicker) return;
    const {root, scrim} = windowPicker;
    root.remove(); scrim.remove(); windowPicker = null;
    $('monl').setAttribute('aria-expanded', 'false');
    $('monl').focus();
  }
  function renderTxWindowControl() {
    const tx = S.page === 'transactions';
    const mon = $('mon');
    mon.classList.toggle('txwindow', tx);
    mon.setAttribute('aria-label', tx ? 'Time window' : 'Month');
    const old = $('monl');
    if ((old.tagName === 'BUTTON') !== tx) {
      old.replaceWith(tx ? h('button', {id: 'monl', type: 'button', 'aria-haspopup': 'dialog',
        'aria-expanded': 'false', onclick: openTxWindow}) : h('span', {id: 'monl', 'aria-live': 'polite'}));
    }
    const f = tx && TX.filters;
    const range = f && (f.since || f.until || f.window === 'all' || txAutoAll(f));
    $('mprev').hidden = $('mnext').hidden = Boolean(range);
    document.getElementById('rgclear')?.remove();
    let label = monthLabel(S.month);
    if (range) {
      label = f.since && /^\d+d$/.test(f.since) ? `Last ${parseInt(f.since, 10)} days`
        : f.since || f.until ? 'Custom range' : 'All time';
      const p = windowResult && windowResult.period;
      if ((f.since || f.until) && p && p.kind === 'range') {
        label += ` · ${p.start ? fmtDate(p.start) : 'Beginning'} – ${fmtDate(p.end)}`;
      } else if (txAutoAll(f)) label += ' · searching';
    }
    fill($('monl'), label, tx ? icon('chevd') : null);
    if (range) mon.append(h('button', {id: 'rgclear', type: 'button', 'aria-label': 'Back to months', onclick: () => {
      windowResult = null;
      updateTx({since: '', until: '', window: '', txmonth: TX.filters.q ? '1' : ''});
      $('monl').focus();
    }}, icon('x')));
  }
  function drawTxWindowResult(d) {
    windowResult = d;
    renderTxWindowControl();
    const note = document.querySelector('#page .rgnote');
    const summary = document.querySelector('#page .rgsummary');
    note.hidden = !d.coverage_month || Boolean(d.period.start && d.period.start >= `${d.coverage_month}-01`);
    note.textContent = note.hidden ? '' : `Some banks are missing before ${monthShort(d.coverage_month)} ${d.coverage_month.slice(0, 4)}, so older totals look low.`;
    summary.hidden = Boolean(txWindowParams().month);
    summary.textContent = `${plural(d.total, 'payment')} · ${signed(d.sum || 0)}`;
  }
  function openTxWindow() {
    if (S.page !== 'transactions') return;
    if (windowPicker) { closeTxWindow(); return; }
    let year = Number(S.month.slice(0, 4));
    const f = TX.filters;
    const choose = (changes) => { closeTxWindow(); windowResult = null; updateTx(changes); };
    const from = h('input', {type: 'date', id: 'rgfrom', min: '1900-01-01',
      value: /^\d{4}-/.test(f.since) ? f.since : `${S.month}-01`, required: true});
    const to = h('input', {type: 'date', id: 'rgto', min: '1900-01-01', value: f.until || todayISO(), required: true});
    const months = h('div', {class: 'rgmg'});
    const yearLabel = h('b');
    function drawMonths() {
      yearLabel.textContent = year;
      fill(months, MONTHS.map((name, i) => {
        const m = `${year}-${pad(i + 1)}`;
        return h('button', {class: 'rgm', type: 'button', 'aria-pressed': String(m === S.month && !f.since && !f.window),
          onclick: () => { closeTxWindow(); setMonth(m); }}, name.slice(0, 3));
      }));
    }
    const form = h('form', {class: 'rgc', onsubmit: (e) => {
      e.preventDefault();
      to.setCustomValidity(from.value > to.value ? 'To must be on or after From.' : '');
      if (form.reportValidity()) choose({since: from.value, until: to.value, window: '', txmonth: ''});
    }}, h('b', {class: 'rgch'}, 'Custom range'),
      h('label', {for: 'rgfrom'}, 'From', from), h('label', {for: 'rgto'}, 'To', to),
      h('button', {class: 'btn w rgapply', type: 'submit'}, 'Show'));
    for (const input of [from, to]) input.addEventListener('input', () => to.setCustomValidity(''));
    const anchor = $('monl').getBoundingClientRect();
    const root = h('div', {class: 'rgpicker', style: {'--rg-left': `${Math.max(12, Math.min(anchor.left, innerWidth - 372))}px`,
      '--rg-top': `${anchor.bottom + 8}px`}, role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Time window', tabindex: '-1'},
      h('div', {class: 'rghd'}, h('h2', {}, 'Time window'), h('button', {class: 'ib', type: 'button',
        'aria-label': 'Close time window', onclick: closeTxWindow}, icon('x'))),
      h('h3', {}, 'Month'), h('div', {class: 'rgyr'},
        h('button', {type: 'button', 'aria-label': 'Previous year', onclick: () => { year = Math.max(1901, year - 1); drawMonths(); }}, icon('chevl')),
        yearLabel, h('button', {type: 'button', 'aria-label': 'Next year', onclick: () => { year = Math.min(9999, year + 1); drawMonths(); }}, icon('chevr'))),
      months, h('h3', {}, 'Quick range'), h('div', {class: 'rgpills'}, [7, 30, 90, 'all'].map((n) =>
        h('button', {type: 'button', 'aria-label': n === 'all' ? 'All time' : `Last ${n} days`,
          'aria-pressed': String(n === 'all' ? f.window === 'all' : f.since === `${n}d`),
          onclick: () => choose({since: n === 'all' ? '' : `${n}d`, until: '', window: n === 'all' ? 'all' : '', txmonth: ''})},
        n === 'all' ? 'All time' : `${n}d`))), form);
    root.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); closeTxWindow(); } else trapFocus(e, root); });
    const scrim = h('div', {class: 'rgscrim', onclick: closeTxWindow});
    document.body.append(scrim, root);
    windowPicker = {root, scrim};
    $('monl').setAttribute('aria-expanded', 'true');
    drawMonths(); root.focus();
  }
  window.addEventListener('hashchange', closeTxWindow);

  async function renderTransactions(tk) {
    const p = S.params;
    const kind = KINDS.some(([k]) => k === p.get('kind')) ? p.get('kind') : 'all';
    const list = (k) => [...new Set(p.getAll(k).filter(Boolean))];
    TX.filters = {q: p.get('q') || '', category: list('category'), group: list('group'), uncategorized: p.get('uncategorized') === '1',
      bank: p.get('bank') || '', tag: list('tag'), kind, since: p.get('since') || '', until: p.get('until') || '',
      window: p.get('window') || '', txmonth: p.get('txmonth') || '', sort: TX_SORTS.includes(p.get('sort')) ? p.get('sort') : 'date_desc'};
    fill($('page'), h('div', {class: 'fbar'}, skel('sk-line')), skeleton(['card s3']));
    const [cats, accts] = await Promise.all([ensureCategories(), ensureAccounts().catch(() => null)]);
    if (tk !== S.token) return;
    const f = TX.filters;

    const search = h('input', {class: 'search', type: 'search', placeholder: 'Search merchants', autocomplete: 'off',
      'aria-label': 'Search merchants', value: f.q});
    let debounce = null;
    search.addEventListener('input', () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => updateTx({q: search.value.trim()}), 250);
    });
    const chips = KINDS.map(([k, label]) => h('button', {class: `fchip${k === 'review' ? ' rv' : ''}`, type: 'button',
      'aria-pressed': String(k === f.kind), dataset: {kind: k}, onclick: () => updateTx({kind: k})}, label));
    const banks = accts ? banksOf(accts).map((b) => b.name) : [];
    if (f.bank && !banks.includes(f.bank)) banks.push(f.bank);
    const tbody = h('tbody');
    const head = h('tr');
    const more = h('button', {class: 'more', type: 'button', hidden: true, onclick: () => loadTx(S.token, false)});
    TX.el = {tbody, more, chips, cats, head, banks: banks.sort()};
    renderTxWindowControl();
    drawTxHead();
    fill($('page'),
      h('div', {class: 'fbar'}, search, chips, HOOKS.txBar(f, TX.el.banks)),
      h('p', {class: 'rgnote', hidden: true}),
      h('p', {class: 'rgsummary', hidden: true, 'aria-live': 'polite'}),
      h('table', {class: 'tx'}, h('thead', {}, head), tbody),
      more);
    await loadTx(tk, true);
  }

  // polish P1: every header sorts by its column (aria-sort, an arrow on the one in use; the others show they can)
  function drawTxHead() {
    const [col, way] = TX.filters.sort.split('_');
    fill(TX.el.head, TX_COLS.map(([key, label, cls, first]) => {
      const on = key === col;
      return h('th', {class: cls, 'aria-sort': on ? (way === 'asc' ? 'ascending' : 'descending') : 'none'},
        h('button', {class: `sortb${on ? ' on' : ''}`, type: 'button',
          onclick: () => updateTx({sort: `${key}_${on ? (way === 'asc' ? 'desc' : 'asc') : first}`})},
        label, icon(on ? (way === 'asc' ? 'arrow-up' : 'arrow-down') : 'chevrons-up-down')));
    }));
  }

  function updateTx(changes, rerender) {
    const c = {...changes};
    // Returning to a month scopes only the unchanged query; a new search uses all history.
    if (has(c, 'q')) c.txmonth = '';
    for (const k of TX_LISTS) {
      if (has(c, k)) c[k] = [...new Set((Array.isArray(c[k]) ? c[k] : [c[k]]).filter(Boolean))];   // a name, a list, or ''
    }
    if (has(c, 'uncategorized')) c.uncategorized = [true, '1', 'true'].includes(c.uncategorized);
    TX.filters = {...TX.filters, ...c};
    const f = TX.filters;
    const hash = hashFor('transactions', txParams(f));
    if (rerender) {
      location.hash = hash;
      return;
    }
    history.pushState(null, '', hash);
    S.params = parseHash().params;
    renderTxWindowControl();
    TX.el.chips.forEach((x) => x.setAttribute('aria-pressed', String(x.dataset.kind === f.kind)));
    drawTxHead();
    loadTx(S.token, true).catch((e) => {
      fill(TX.el.tbody, h('tr', {}, h('td', {class: 'c-empty', colspan: '5'}, `Could not load: ${e.message}`)));
    });
  }

  async function loadTx(tk, reset) {
    const f = TX.filters;
    const req = ++TX.req;
    const d = await api('/api/transactions', {...txParams(f), ...txWindowParams(f), kind: f.kind,
      limit: TX.limit, offset: reset ? 0 : TX.rows.length});
    if (tk !== S.token || req !== TX.req) return;
    const {tbody, more, chips} = TX.el;
    if (reset) {
      TX.rows = [];
      TX.day = null;
      TX.rowMonth = null;
      tbody.replaceChildren();
    }
    TX.rows.push(...d.rows);
    TX.total = d.total;
    for (const t of d.rows) {   // polish P1: newest or oldest first, each day under its header with the day's total
      if (f.sort.startsWith('date_') && t.date !== TX.day) {
        const month = t.date.slice(0, 7);
        if (Object.keys(d.months || {}).length > 1 && TX.rowMonth !== month) {
          TX.rowMonth = month;
          const row = dayRow(t.date, d.months[month]);
          row.classList.add('mhd');
          row.querySelector('th').textContent = monthLabel(month);
          tbody.append(row);
        }
        TX.day = t.date;
        tbody.append(dayRow(t.date, d.days && d.days[t.date]));
      }
      tbody.append(txRow(t));
    }
    if (!TX.rows.length) {
      fill(tbody, h('tr', {}, h('td', {class: 'c-empty mut', colspan: '5'},
        txFiltered(f) ? 'Nothing matches these filters.' : txWindowParams(f).month
          ? `No transactions in ${monthName(S.month)}.` : 'No transactions in this time window.')));
    }
    const review = chips.find((c) => c.dataset.kind === 'review');
    review.textContent = `Needs review (${d.counts.review})`;
    const left = d.total - TX.rows.length;
    more.hidden = left <= 0;
    more.textContent = `Show ${left} more`;
    drawTxWindowResult(d);
    HOOKS.txLoaded(d);
  }

  function dayRow(iso, day) {
    const k = daysFrom(iso, todayISO());
    const label = k === 0 ? 'Today' : k === -1 ? 'Yesterday'
      : `${asDate(iso).toLocaleDateString(LOC, {weekday: 'short'})}, ${fmtDate(iso)}`;
    return h('tr', {class: 'day'}, h('th', {colspan: '2', scope: 'rowgroup'}, label), h('td', {class: 'c-acct'}), h('td', {class: 'c-cat'}),
      h('td', {class: `num${day && day.total < 0 ? ' in' : ''}`}, day ? signed(day.total) : ''));
  }

  function txRow(t) {
    return h('tr', {class: t.is_transfer ? 'xfer' : '', dataset: {tid: t.transaction_id}},
      h('td', {class: 'c-date mut'}, fmtDate(t.date)),
      h('td', {class: 'c-m'}, h('div', {class: 'm'}, avatar(t.seed, t.logo_url, t.icon_url, 24),
        h('div', {class: 'mt'},
          h('b', {title: t.name || ''}, h('span', {class: 't'}, t.merchant || '—'), t.pending ? h('span', {class: 'pend'}, 'pending') : null,
            billTag(t.bill_outlier), HOOKS.txTags(t)),
          h('span', {class: 'only-m'}, `${fmtDate(t.date)} · ${t.institution_name || ''}`)))),
      h('td', {class: 'c-acct mut'}, h('span', {class: 'acc'}, ia(t.institution_name, 'xs', null, t.institution_id),
        nameMask(instShort(t.institution_name), t.account_mask || '----', {fallback: '',
          title: `${acctName(t.account_name) || instShort(t.institution_name)} ••${t.account_mask || '----'}`}))),
      h('td', {class: 'c-cat'}, HOOKS.txCat(t) || h('span', {class: 'catc'}, badge(t.category, 'xs'), categorySelect(t))),
      h('td', {class: `num v${t.amount < 0 ? ' in' : ''}`}, signed(t.amount)));
  }

  function categorySelect(t) {
    const names = TX.el.cats.map((c) => c.name);
    if (!names.includes(t.category)) names.push(t.category);
    const uncat = !!t.uncategorized;   // the server's rule, the same one ?uncategorized=1 filters by
    const sel = HOOKS.catPick({value: t.category, names, cls: `${t.review ? ' review' : ''}${t.category_source === 'user' ? ' done' : ''}${uncat ? ' uncat' : ''}`,
      label: `Category for ${t.merchant}`, title: t.ai_note, short: true}, () => changeCategory(t, sel));
    return sel;
  }

  const shortName = (s) => (s.length > 28 ? `${s.slice(0, 27)}…` : s);

  async function changeCategory(t, sel) {
    const before = t.category;
    const category = sel.value;
    sel.disabled = true;
    try {
      const res = await post('/api/transactions/category', {transaction_id: t.transaction_id, category, make_rule: false});
      Object.assign(t, {category: res.category, category_source: res.category_source, review: false});
      sel.classList.remove('review', 'uncat');
      sel.classList.add('done');
      sel.previousElementSibling.replaceWith(badge(res.category, 'xs'));
      const same = TX.rows.filter((r) => r !== t && r.merchant === t.merchant).length;
      toast(['Moved ', h('b', {}, t.merchant), ' to ', h('b', {}, category), '.', same ? ` ${same} more from them in this list.` : ''], [
        {label: `Always file ${shortName(t.merchant)} here`, primary: true, onclick: () => makeRule(t, category)},
        {label: 'Just this once', onclick: hideToast},
      ]);
      loadCounts();
    } catch (e) {
      sel.value = before;
      toast(`Could not save: ${e.message}`, [], {error: true, timeout: 6000});
    } finally {
      sel.disabled = false;
    }
  }

  async function makeRule(t, category) {
    try {
      const res = await post('/api/transactions/category', {transaction_id: t.transaction_id, category, make_rule: true});
      toast(['Rule saved: ', h('b', {}, res.rule ? res.rule.pattern : t.merchant), ' → ', h('b', {}, category),
        `. ${plural(res.changed, 'transaction')} updated; future syncs follow it too.`], [], {timeout: 5000});
      loadCounts();
      await loadTx(S.token, true);
    } catch (e) {
      toast(`Could not save the rule: ${e.message}`, [], {error: true, timeout: 6000});
    }
  }

  // ---------- Budgets --------------------------------------------------------------------------
  // Suggestions stay quiet: a category with a cap never shows one; the big "Suggested budgets / Use all" hero
  // only appears while no cap exists at all (d.suggestion_hero). Otherwise the page sums up your own caps and
  // the suggestions for uncapped categories wait behind one small "See suggestions" link.
  async function renderBudgets(tk) {
    fill($('page'), skeleton(['card s3', 'card', 'card', 'card']));
    const d = await api('/api/budgets', {month: S.month});
    if (tk !== S.token) return;
    drawBudgets(d);
  }

  async function reloadBudgets() {
    const tk = S.token;
    const d = await api('/api/budgets', {month: S.month});
    if (tk !== S.token) return;
    destroyCharts();
    drawBudgets(d);
    loadCounts();
  }

  async function applyCap(category, cap) {
    await post('/api/budgets/cap', {category, monthly_cap: cap});
    await reloadBudgets();
    toast(cap == null ? ['Budget removed from ', h('b', {}, category), '.'] : ['Budget set: ', h('b', {}, category), ` ${money(cap)} a month.`], [], {timeout: 3500});
  }

  // The budget editor: the app's modal sheet (a bottom sheet below 1024px, a centred dialog above). The header is the
  // row's badge and name with its figures (`opts.badge`, `opts.sub()`); then the "Monthly budget" field, "Suggested $X ·
  // Use" (saves the suggestion in one click, like the other Use buttons on this page), a category's roll-over row
  // (capEditorExtra) and Save · Cancel with Remove budget at the far end (one tap, no confirm). Escape and the backdrop
  // close it through the modal's own handlers; `opts.again` finds the opener once the page behind has been redrawn.
  // polish P2e: the same editor sets a group's own budget (budgets.js): `opts.save(amount | null)` saves instead of the
  // category's cap, `opts.label` names the field, `opts.placeholder` is its hint, and there an empty field clears the budget
  // (the group's Planned goes back to its caps' sum). The rollover row is a category's only.
  function capEditor(opener, category, current, suggested, opts = {}) {
    const eid = `ederr-${opts.save ? 'g' : ''}${hue(category)}`;
    const input = h('input', {type: 'text', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false',
      value: current != null ? String(current) : '',
      placeholder: opts.placeholder != null ? String(opts.placeholder) : suggested != null ? String(suggested) : '0',
      'aria-label': opts.label || `Monthly budget for ${category}`, 'aria-describedby': eid});
    const err = h('span', {class: 'err', role: 'alert', id: eid});
    const sub = h('p', {class: 'bx-sub'}, opts.sub ? opts.sub() : '');
    const refresh = () => { if (opts.sub) sub.textContent = opts.sub(); };   // the page behind was redrawn: new figures
    let busy = false;
    const saveCap = async (cap) => {
      if (busy) return;
      busy = true;
      try {
        await (opts.save ? opts.save(cap) : applyCap(category, cap));
        if ($('modal').classList.contains('on') && root.isConnected) closeModal();
      } catch (e) {
        err.textContent = e.message;
      }
      busy = false;
    };
    const save = () => {
      const n = parseAmount(input.value);
      if (n == null && opts.save && current != null) {
        saveCap(null);
        return;
      }
      if (n == null || Number.isNaN(n) || n > 100000) {
        err.textContent = 'Enter an amount from $0 to $100,000.';
        input.setAttribute('aria-invalid', 'true');
        input.focus();
        return;
      }
      saveCap(Math.round(n * 100) / 100);
    };
    input.addEventListener('input', () => {
      err.textContent = '';
      input.removeAttribute('aria-invalid');
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') save();
    });
    const offer = suggested != null && suggested !== current;
    const sug = offer ? h('span', {class: 'bx-sug'}, 'Suggested ', h('b', {class: 'v'}, money(suggested)), ' · ',
      h('button', {type: 'button', class: 'lnk', onclick: () => saveCap(suggested)}, 'Use')) : null;
    const remove = current != null ? h('button', {type: 'button', class: 'rm', 'aria-label': `Remove the ${category} budget`,
      onclick: () => saveCap(null)}, icon('trash'), h('span', {}, 'Remove budget')) : null;
    const extra = opts.save ? null : HOOKS.capEditorExtra(category, {capped: current != null, onChange: refresh});
    const root = h('div', {class: 'bx'},
      h('div', {class: 'bx-top'},
        h('label', {class: 'bx-field'}, h('span', {class: 'bx-lab'}, 'Monthly budget'),
          h('span', {class: 'edin bx-in'}, h('span', {class: 'pre', 'aria-hidden': 'true'}, CUR.sym), input)), err, sug),
      extra,
      h('div', {class: 'bx-foot'}, h('button', {type: 'button', class: 'p', onclick: save}, 'Save'),
        h('button', {type: 'button', onclick: closeModal}, 'Cancel'), h('span', {class: 'sp'}), remove));
    // after a save the table is redrawn, so the opener is a new element: focus goes to its successor
    const back = () => (opener.isConnected ? opener : (opts.again && document.querySelector(opts.again)) || $('page'));
    let stop = () => {};
    openSheet([h('span', {class: 'bx-grab', 'aria-hidden': 'true'}),
      h('div', {class: 'shd'}, opts.badge || null, h('h2', {id: 'sheet-title'}, category)), sub, root],
    {narrow: true, cls: 'bx-sheet', focus: input, onClose: () => back().focus(), release: () => stop()});
    input.select();
    stop = keepInView($('sheet'), root, input);
  }

  // On a phone the on-screen keyboard shrinks window.visualViewport, not the layout viewport, so the bottom sheet (fixed to
  // the layout viewport's bottom) would sit under it. While the cap editor is open the sheet is lifted by the keyboard's
  // height and capped to the visual viewport; its footer is sticky (budgets.css), so Save / Cancel / Remove stay on screen
  // while the field area scrolls, and the field is kept above the footer. Below 1024px only: the dialog above it is not
  // touched. Returns the clean-up, run when the sheet closes (any way) or is replaced: the listeners and the inline styles go.
  function keepInView(sheet, root, field) {
    const vv = window.visualViewport;
    const phone = matchMedia('(max-width: 1023.98px)');
    const foot = root.querySelector('.bx-foot');
    const box = root.querySelector('.bx-field');
    let frame = 0;
    const reveal = () => {  // the field, with its label, above the sticky footer and below the sheet's top edge
      if (!phone.matches || !box.isConnected) return;
      const s = sheet.getBoundingClientRect();
      const f = box.getBoundingClientRect();
      const floor = getComputedStyle(foot).position === 'sticky' ? foot.getBoundingClientRect().top : s.bottom;
      if (f.bottom > floor - 6) sheet.scrollTop += f.bottom - floor + 6;
      else if (f.top < s.top + 6) sheet.scrollTop -= s.top + 6 - f.top;
    };
    const fit = () => {
      if (vv) {
        const shrunk = phone.matches && vv.height < innerHeight - 1;
        const lift = Math.max(0, innerHeight - vv.height - vv.offsetTop);
        sheet.style.bottom = shrunk && lift ? `${Math.round(lift)}px` : '';
        sheet.style.maxHeight = shrunk ? `${Math.max(0, Math.min(vv.height - 12, innerHeight * 0.92))}px` : '';
      }
      reveal();
    };
    const later = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(reveal); };
    if (vv) {
      vv.addEventListener('resize', fit);
      vv.addEventListener('scroll', fit);
    }
    field.addEventListener('focus', later);
    fit();
    return () => {
      cancelAnimationFrame(frame);
      if (vv) {
        vv.removeEventListener('resize', fit);
        vv.removeEventListener('scroll', fit);
      }
      field.removeEventListener('focus', later);
      sheet.style.bottom = '';
      sheet.style.maxHeight = '';
      if (!sheet.getAttribute('style')) sheet.removeAttribute('style');
    };
  }

  function drawBudgets(d) {
    const sug = new Map(d.suggestions.categories.map((c) => [c.category, c]));  // uncapped categories only
    const hasSug = (c) => sug.has(c.category) && sug.get(c.category).suggested != null;
    const cats = d.categories;
    const b = d.budgets;
    const ratio = (c, ref) => {  // the cards run from the fullest ring down: (spent + bills still due) / budget
      const x = c.spend + (c.due || 0);
      return ref > 0 ? x / ref : x > 0 ? 1 : 0;
    };
    let summary;
    if (d.suggestion_hero) {
      const suggested = cats.filter(hasSug).sort((a, c) => ratio(c, sug.get(c.category).suggested) - ratio(a, sug.get(a.category).suggested));
      const total = d.suggestions.total;
      const spent = sum(suggested, (c) => c.spend);
      const pending = suggested.map((c) => sug.get(c.category));
      summary = h('section', {class: 'card bsum'}, ringPct(total ? spent / total : 0, ACC, 84, 8, 17, 'used'),
        h('div', {class: 'sp'},
          h('h3', {}, h('span', {class: 'hi'}, glyph('target')), h('span', {class: 'ttl'}, 'Suggested budgets')),
          h('div', {class: 'big md v'}, money(total), h('span', {class: 'unit'}, ` a month · ${plural(suggested.length, 'category', 'categories')}`)),
          h('div', {class: 'sub'}, `Based on what you spent in ${d.suggestions.months.map(monthShort).join(', ')}, rounded up. Nothing changes until you tap Use.`)),
        h('button', {class: 'cbtn', type: 'button', onclick: () => confirmApplyAll(pending)}, `Use all ${pending.length}`));
    } else {
      const over = b.left < 0;
      summary = b.count ? h('section', {class: 'card bsum'}, budgetRing(b.total_spend, b.total_due, b.total_cap, 84, 8, 17),
        h('div', {class: 'sp'},
          h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.ok}}, glyph('target')), h('span', {class: 'ttl'}, 'Your budgets')),
          h('div', {class: 'big md v'}, money(b.total_cap), h('span', {class: 'unit'}, ` a month · ${plural(b.count, 'budget')}`)),
          h('div', {class: 'sub'}, h('b', {}, money(b.total_spend)), ` spent in ${monthName(d.month)} · `,
            b.total_due ? [h('b', {}, money(b.total_due)), ' in bills still due · '] : null,
            over ? [h('b', {class: 'bad'}, `${money(-b.left)} over budget`), b.total_due ? ' once bills clear' : '']
              : [h('b', {class: b.left > 0 ? 'pos' : ''}, money(b.left)), ' left'],
            !over && b.per_day != null && b.days_left ? ` · about ${money(b.per_day)} a day` : ''),
          h('div', {class: 'sub dim'}, `${money(b.under)} left in ${plural(b.under_count, 'budget')}`,
            b.over ? [' · ', h('span', {class: 'bad'}, `${money(b.over_amount)} over in ${b.over}`)] : null))) : null;
    }
    // polish P2A: the page's body (the grouped Planned / Spent / Left table, the "No budget yet" list and the move banner)
    // is budgets.js's; the summary above and the cap editor / suggestion helpers below stay here.
    fill($('page'), summary, HOOKS.budgetsBody(d));
  }

  function confirmApplyAll(pending) {
    const total = pending.reduce((s, x) => s + x.suggested, 0);
    modal(`Use ${plural(pending.length, 'suggested budget')}?`, h('div', {},
      h('p', {class: 'mut'}, `That's ${money(total)} a month in total. You can change or remove any of them later.`),
      h('ul', {}, pending.map((x) => h('li', {}, `${x.category}: ${money(x.suggested)}`)))), [
      {label: `Use ${pending.length}`, primary: true, onclick: async () => {
        closeModal();
        try {
          for (const x of pending) await post('/api/budgets/cap', {category: x.category, monthly_cap: x.suggested});
          await reloadBudgets();
          toast(`${plural(pending.length, 'budget')} set.`, [], {timeout: 3500});
        } catch (e) {
          toast(`Stopped: ${e.message}`, [], {error: true, timeout: 6000});
          reloadBudgets();
        }
      }},
      {label: 'Cancel', onclick: closeModal},
    ]);
  }

  // ---------- Recurring ------------------------------------------------------------------------
  const isPaid = (r) => r.paid_this_month && r.cadence !== 'weekly';
  function recPill(r) {
    const k = r.days_until;
    if (isPaid(r)) return pill('sm', null, `Paid ${fmtDate(r.last_date)}`);
    if (r.status === 'late') return pill('bad sm', null, `Late · expected ${fmtDate(r.next_expected)}`);
    if (r.status === 'due_soon') return pill('warn sm', null, k < 0 ? `Expected ${fmtDate(r.next_expected)}` : k === 0 ? 'Due today' : k === 1 ? 'Due tomorrow' : `Due in ${k} days`);
    return pill('nt sm', null, `Next ${fmtDate(r.next_expected)}`);
  }

  async function renderRecurring(tk) {
    fill($('page'), skeleton(['card s3', 'card s2', 'card']));
    const d = await api('/api/recurring');
    if (tk !== S.token) return;
    const soon = d.items.filter((r) => r.status === 'late' || (r.days_until >= 0 && r.days_until <= 30))
      .sort((a, b) => a.next_expected.localeCompare(b.next_expected));
    const upcomingTotal = soon.filter((r) => r.status !== 'late').reduce((s, r) => s + r.amount, 0);
    const timeline = soon.length ? h('div', {class: 'tlw'}, soon.map((r) => {
      const late = r.status === 'late';
      return h('div', {class: `tlc${late ? ' late' : ''}`},
        h('div', {class: 'd'}, cal(r.next_expected, late ? 'late' : r.days_until <= 1 ? 'hot' : ''),
          h('span', {class: `when${late ? ' bad' : ''}`}, relativeDate(r.next_expected, d.today, late, true))),
        h('div', {class: 'tm'}, avatar(r.merchant, r.logo_url, r.icon_url, 24), h('b', {}, r.merchant)),
        h('div', {class: 'amt v'}, billAmt(r)));
    })) : msgBox('Nothing expected', 'No recurring charges in the next 30 days.');
    const row = (r, narrow) => {  // the narrow Subscriptions card puts the status under the amount
      const per = r.cadence === 'monthly' ? '' : ` · ${money2(r.monthly_amount)} a month`;
      const usual = r.variable && r.usual ? ` · usually ${usualText(r.usual)}` : '';  // TASK-21: a bill that changes
      const amount = h('span', {class: 'amt v', style: {'min-width': '88px', 'text-align': 'right'}}, billAmt(r));
      // TASK-21: the row opens the bill's sheet (its schedule, charges, Edit schedule and Stop tracking)
      return h('button', {class: `li wrap${isPaid(r) ? ' paid' : ''}`, type: 'button', dataset: {bill: r.key, ...(r.variable ? {est: ''} : {})},
        onclick: () => HOOKS.billSheet(r)}, avatar(r.merchant, r.logo_url, r.icon_url, 32),  // wrap: "$1.08 a month" never cut
        h('span', {class: 'grow'}, h('b', {}, r.merchant, billTag(r.outlier)),
          h('span', {style: {display: 'flex', 'align-items': 'center', gap: '7px'}}, badge(r.category, 'xs'),
            `${r.category} · ${r.cadence_label}${per}${usual}`),
          h('span', {class: 'rel-date'}, relativeDate(r.next_expected, d.today, r.status === 'late'))),
        // the Bills card: pill then amount on a row; on a phone the status goes under the amount (app.css .rt.bill)
        narrow ? h('span', {class: 'rt'}, amount, recPill(r)) : h('span', {class: 'rt bill'}, recPill(r), amount));
    };
    const byDueStatus = (a, b) => Number(isPaid(a)) - Number(isPaid(b));
    const bills = d.items.filter((r) => r.kind === 'bill').sort(byDueStatus);
    const subs = d.items.filter((r) => r.kind === 'subscription').sort(byDueStatus);
    const empty = (what) => msgBox(`No ${what} found yet`, 'A payee counts after a few similar charges at a steady interval, or track one from any of its transactions.');
    const subsCard = card({title: 'Subscriptions', icon: 'repeat', rgb: RGB.purple},
      h('p', {class: 'sub recurring-total'}, `${money2(d.monthly_totals.subscription)} a month`),
      subs.length ? subs.map((r) => row(r, true)) : empty('subscriptions'));
    subsCard.dataset.flex = '';  // beside Bills (3 columns), or its own full row (2 columns)
    const grid = h('div', {class: 'grid'},
      card({cls: 's2', title: 'Bills', icon: 'receipt', rgb: RGB.cyan},
        h('p', {class: 'sub recurring-total'}, `${money2(d.monthly_totals.bill)} a month`),
        bills.length ? bills.map((r) => row(r)) : empty('bills')),
      subsCard);
    fill($('page'), card({title: 'Next 30 days', icon: 'calendar', rgb: RGB.acc, extra: small(`${money2(upcomingTotal)} expected`)}, timeline), grid);
    pack(grid);
  }

  // ---------- Accounts -------------------------------------------------------------------------
  const GROUP = {cash: {i: 'piggy', c: RGB.ok}, cards: {i: 'card', c: RGB.bad}, loans: {i: 'handcoins', c: RGB.warn},
    investments: {i: 'linechart', c: RGB.acc}, property: {i: 'house', c: RGB.purple}, vehicles: {i: 'car', c: RGB.cyan},
    other_assets: {i: 'package', c: RGB.ok}, other_debts: {i: 'fileminus', c: RGB.bad}};
  const TYPE_GROUP = {depository: 'cash', credit: 'cards', loan: 'loans', investment: 'investments'};
  const SUBTYPE = {checking: 'Checking', savings: 'Savings', 'cash management': 'Vault', paypal: 'Wallet', 'credit card': 'Credit card',
    brokerage: 'Brokerage', 'money market': 'Money market', cd: 'CD', auto: 'Auto loan', student: 'Student loan', mortgage: 'Mortgage'};

  // Shared Accounts details line: exclusion is mode-dependent; identity and amounts stay on line one. Approved density
  // mock D1 B: one details line. Its figures (`facts`: a payment on its way, "27% of $4,000", "81% paid off") are never
  // shortened; only the words after them (`rest`, no money) may end in an ellipsis, the whole line in its title.
  const accountMeta = (id, facts, rest, chip = null) => {
    const parts = [...facts.filter(Boolean), rest || null].filter(Boolean);
    const text = parts.map((p) => (typeof p === 'string' ? p : p.text)).join(' · ');
    const sep = (i) => (i < parts.length - 1 ? ' · ' : '');
    return h('span', {class: 'meta2', title: text}, chip, leftOut(id) ? h('span', {class: 'nc2'}, 'Not counted · ') : null,
      facts.filter(Boolean).map((f, i) => h('span', {class: 'fx', dataset: f.dataset || {}}, `${typeof f === 'string' ? f : f.text}${sep(i)}`)),
      rest ? h('span', {class: 'mt', title: text}, rest) : null);
  };
  const accountCounted = (id) => leftOut(id) ? 'Not counted in Custom net worth.' : 'Counted in net worth.';
  const accountTrend = (values, liability) => values && values.length >= 2
    ? h('div', {}, h('p', {}, 'Recent balance trend'), spark(values, liability ? BAD : OK, 160, 36)) : null;
  function accountBar(ratio) {
    return h('span', {class: 'track thin bar2'},
      h('i', {style: {width: `${clamp(ratio * 100, 1.5, 100)}%`, background: ratio > 0.3 ? 'var(--warn)' : 'var(--ok)'}}),
      h('s', {style: {left: '30%'}}));
  }
  function plaidRow(a, inst, sparks, liab, instId, transit = []) {
    const hooked = HOOKS.acctRow(a, inst, instId);
    if (hooked) return hooked;
    const credit = a.type === 'credit';
    const util = credit && a.limit ? (a.current || 0) / a.limit : null;
    const value = a.signed != null ? a.signed : a.current;
    const sub = [SUBTYPE[a.subtype] || a.subtype || a.type, instShort(inst)].filter(Boolean).join(' · ');
    const l = liab && liab[a.account_id];
    const payments = transit.filter((e) => e.card_id === a.account_id);
    // A payment on its way changes what the card's balance means, so it leads the details line, whole (TASK-22f); where
    // it left from and when are in the row's panel. Beside it "20% of $5,000" doesn't fit a 1024px or phone card, so
    // that card's use of its limit is "20% used" among the words that may shorten (the limit is in its panel, the bar stays).
    const used = credit && a.current && util != null ? Math.round(util * 100) : null;
    const facts = [...payments.map((e) => ({text: `${money2(e.amount)} payment on its way`, dataset: {transit: e.transaction_id}})),
      used != null && !payments.length ? `${used}% of ${money(a.limit)}` : null];
    const rest = [used != null && payments.length ? `${used}% used` : null, a.mask ? `••${a.mask}` : null,
      credit ? a.current ? util != null ? null : 'Credit card' : 'No balance' : sub].filter(Boolean).join(' · ');
    const row = h('div', {class: 'li acct r2', dataset: {account: a.account_id}}, ia(inst, '', null, instId),
      h('span', {class: 'grow'},
        h('b', {class: 'nmrow', title: acctName(a.official_name || a.name)}, h('span', {class: 'nm'}, acctName(a.name) || 'Account'),
          help(`Account details: ${acctName(a.name)}`, () => [
            h('dl', {class: 'help-facts'}, [
              ['Account', acctName(a.official_name || a.name)], ['Bank', inst], ['Type', sub],
              a.mask ? ['Account number', `••${a.mask}`] : null,
              a.limit ? ['Credit limit', money2(a.limit)] : null, l && l.apr != null ? ['APR', `${l.apr}%`] : null
            ].filter(Boolean).map(([label, v]) => h('div', {}, h('dt', {}, label), h('dd', {}, v)))),
            h('p', {}, accountCounted(a.account_id)), accountTrend(sparks[a.account_id], credit || a.type === 'loan'),
            payments.map((e) => h('p', {dataset: {transit: e.transaction_id}},
              `${money2(e.amount)} payment on its way from ${acctName(e.from_account.name) || 'your bank account'} `
              + `on ${fmtDate(e.date)}. The bank still shows it owed; net worth counts this payment once.`))
          ], {key: 'account', long: true})),
        accountMeta(a.account_id, facts, rest),
        credit && !!a.current && util != null ? accountBar(util) : null),
      h('span', {class: 'rt'}, h('span', {class: `amt v${value < 0 ? ' bad' : ''}`}, value == null ? '—' : money2(value))));
    return h('div', {class: 'account-entry'}, row);
  }

  function manualLine(m) {  // a manual account row; loans open their panel, cars / property their page, the rest the edit form
    const K = KIND[m.kind];
    let sub;
    let note = null;  // a disclosure: it goes in the trigger's panel, never glued onto the row's facts with " · "
    if (m.kind === 'loan' && m.paid_off) {
      sub = ['Paid off', `updated ${daysAgo(m.as_of)}`].join(' · ');
    } else if (m.kind === 'loan') {
      // Two clean facts ("82% paid off · $465.10 a month"); whether the balance tracks payments is explanation.
      sub = [m.paid_pct != null ? `${Math.round(m.paid_pct)}% paid off` : m.subtype_label,
        m.monthly_payment ? `${money2(m.monthly_payment)} a month` : null,
        !m.auto_updates ? `updated ${daysAgo(m.as_of)}` : null].filter(Boolean).join(' · ');
      note = m.auto_updates ? 'Balance updates from your payments automatically.' : null;
    } else if (m.kind === 'credit_card' && m.approx) {
      // Option C: the row keeps its headline facts ("27% of $4,000 · updated Aug 23, 2026"). Why the balance is
      // approximate — payments lower it, purchases aren't seen, and what has been matched so far — is EXPLANATION,
      // so it goes in `note` (the trigger's panel), never glued onto the row's facts with " · ". This branch is the
      // one the `note` declaration above was written for; the loan branch above already uses it.
      sub = [m.credit_limit ? `${Math.round((m.balance / m.credit_limit) * 100)}% of ${money(m.credit_limit)}` : null,
        `updated ${fmtDate(m.balance_as_of, true)}`].filter(Boolean).join(' · ');
      note = `Manual balance: last set ${money2(m.entered_balance)} on ${fmtDate(m.balance_as_of, true)}, minus `
        + `${money2(m.matched_total)} in ${m.matched_count === 1 ? 'matched payment' : 'matched payments'} seen so far. `
        + 'Purchases since then are not included — update it from a statement.';
    } else if (m.valuation) {
      sub = valueLine(m);
    } else {
      const what = m.kind === 'vehicle' ? null : m.kind === 'credit_card' && m.credit_limit
        ? `${Math.round((m.balance / m.credit_limit) * 100)}% of ${money(m.credit_limit)}` : m.subtype_label || m.institution;
      sub = [what, `updated ${daysAgo(m.as_of)}`].filter(Boolean).join(' · ');
    }
    const id = manualNwId(m.id);
    const credit = m.kind === 'credit_card';
    const ratio = credit && m.credit_limit ? m.balance / m.credit_limit : null;
    // line two: its figure (a card's use of its limit, a loan's % paid off) whole, then the words that may be shortened
    const used = credit && m.balance && m.credit_limit ? `${Math.round((m.balance / m.credit_limit) * 100)}% of ${money(m.credit_limit)}` : null;
    const fact = used || (m.kind === 'loan' && !m.paid_off && m.paid_pct != null ? `${Math.round(m.paid_pct)}% paid off` : null);
    const rest = credit && !m.balance ? 'No balance'
      : used ? `updated ${m.approx ? fmtDate(m.balance_as_of, true) : daysAgo(m.as_of)}`
        : m.valuation ? `${m.valuation.source === 'estimate' ? 'Estimated value' : 'Your value'} · updated ${daysAgo(m.as_of)}`
          : m.kind === 'loan' ? `${m.paid_off ? 'Paid off · ' : fact ? '' : `${m.subtype_label || 'Loan'} · `}updated ${daysAgo(m.as_of)}`
            : sub;
    const page = m.kind === 'vehicle' || m.kind === 'property';
    // The name is the control; the name inside it is what shortens. On a phone the control's 44px hit box (tap.css
    // ::after) reaches past its 24px line, so the control itself can't be the overflow:hidden box: that cut the hit box
    // and made even "Cabin" read as clipped.
    const action = h(page ? 'a' : 'button', {class: 'account-open', type: page ? null : 'button',
      href: page ? `#/asset/${m.id}` : null,
      dataset: m.kind === 'loan' ? {loan: String(m.id)} : page ? {asset: String(m.id), kind: m.kind} : {edit: String(m.id), kind: m.kind},
      title: acctName(m.name),
      'aria-label': `${acctName(m.name)}: ${m.kind === 'loan' ? 'open loan details' : page ? 'open its page' : 'edit'}`},
    h('span', {class: 'nm'}, acctName(m.name)));
    const row = h('div', {class: 'li r2', dataset: {account: id}, onclick: (e) => {
      if (!e.target.closest('button, a')) action.click();
    }}, manualChip(m.institution, 's') || ibadge(K.i, K.c, 's'),
    h('span', {class: 'grow'}, h('b', {class: 'nmrow'}, action,
      help(`Details: ${acctName(m.name)}`, () => [
        h('dl', {class: 'help-facts'}, [
          ['Account', acctName(m.name)], ['Type', K.label], m.institution ? ['Provider', m.institution] : null,
          ['Updated', fmtDate(m.balance_as_of || m.as_of, true)],
          ['Details', sub], m.credit_limit ? ['Credit limit', money2(m.credit_limit)] : null,
          m.interest_rate != null ? ['APR', `${m.interest_rate}%`] : null,
          m.original_amount ? ['Original amount', money2(m.original_amount)] : null,
          m.monthly_payment ? ['Monthly payment', money2(m.monthly_payment)] : null,
          m.next_payment ? ['Next payment', fmtDate(m.next_payment, true)] : null,
          m.payoff && m.payoff.payoff_month ? ['Estimated payoff', fmtDate(m.payoff.payoff_month, true)] : null
        ].filter(Boolean).map(([label, v]) => h('div', {}, h('dt', {}, label), h('dd', {}, v)))),
        note ? h('p', {}, note) : null,
        m.stale ? h('p', {}, `Balance is from ${fmtDate(m.balance_as_of, true)}. Update it any time.`) : null,
        h('p', {}, accountCounted(id)), accountTrend(m.spark, m.liability)
      ], {key: 'manual-account', long: true})),
      accountMeta(id, [fact], rest, h('span', {class: 'chip2'}, 'Manual')),
      credit && !!m.balance && ratio != null ? accountBar(ratio) : null),
    h('span', {class: 'rt'}, h('span', {class: `amt v${m.liability && m.signed < 0 ? ' bad' : ''}`,
      title: m.approx ? 'Approximate: purchases since the balance you set are not seen' : null},
    `${m.approx ? '≈ ' : ''}${money2(m.signed)}`)));
    return h('div', {class: 'account-entry'}, row);
  }

  // Full valuation detail for the vehicle / property disclosure: "Trade-in $X · Carfax · Sep 26" / "Estimated $X · Redfin · Sep 26", or
  // "Your value".
  function valueLine(m) {
    const v = m.valuation;
    const e = v.estimate;
    const nudge = v.mileage_nudge ? ' · Update mileage for a better estimate' : '';
    if (v.source === 'estimate' && e) {
      return [`${e.basis_label} ${money(e.value)} · ${e.source_label} · ${fmtDate(e.date).replace(' ', '\u00a0')}`, nudge];
    }
    if (v.source === 'estimate') return `No estimate yet · ${v.last_error ? 'see why' : 'add a value any time'}${nudge}`;
    return [`Your value · updated ${daysAgo(m.as_of)}`, e ? ` · ${e.source_label} ${money(e.value)}` : '', nudge];
  }

  // A group's month change, as a muted line under its card title: an arrow and the change on the signed basis (the growth
  // of its net-worth contribution). Good is green for every group — for a debt that means the balance came down, for an
  // asset that it went up — so the arrow shows the balance's own direction (a debt's down is the up-right of its net-worth
  // contribution reversed). Hidden when the server has no month-start baseline (month_change null) or nothing moved.
  function monthChange(g) {
    const c = g.month_change;
    if (!c || Math.abs(c.amount) < 0.5) return null;   // nothing moved (it would read "$0 (0.0%)"): no line
    const amount = c.amount;
    const up = g.asset ? amount > 0 : amount < 0;   // the balance's direction: an asset up / a debt down
    const tone = amount > 0 ? 'pos' : amount < 0 ? 'bad' : '';
    const arrow = amount === 0 ? null : icon(up ? 'arrowupright' : 'arrowdownright');
    const text = `${money(Math.abs(amount))}${c.pct == null ? '' : ` (${Math.abs(c.pct).toFixed(1)}%)`} this month`;
    return h('span', {class: `mchg${tone ? ` ${tone}` : ''}`}, arrow, text);
  }

  // ---------- collapsed Accounts groups (localStorage, per device) ----------------------------------------------
  // Each group card on Accounts folds from the chevron at the end of its header, like the Budgets page's groups
  // (budgets.js, key hf.budgets.shut). What is folded is remembered on this device; a blocked or full localStorage
  // just means the page starts unfolded.
  const ACCT_SHUT_KEY = 'hf.accounts.shut';
  const ACCT_SHUT = (() => {
    try {
      const v = JSON.parse(localStorage.getItem(ACCT_SHUT_KEY) || '[]');
      return new Set(Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);
    } catch (e) { return new Set(); }
  })();
  const saveAcctShut = () => {
    try { localStorage.setItem(ACCT_SHUT_KEY, JSON.stringify([...ACCT_SHUT])); }
    catch (e) { /* not remembered on this device */ }
  };

  async function renderAccounts(tk) {
    fill($('page'), skeleton(['card s2', 'stack', 'card', 'card']));
    const d = await loadAccounts();
    if (tk !== S.token) return;
    const nw = d.net_worth;
    const later = [];
    const stale = d.manual.filter((m) => m.stale);
    // In Custom a group it leaves out entirely stays listed with its total, dimmed, "Not counted" in place of its share;
    // a group it counts in part shows the counted total and "N of M counted". The shares are of the mode's own assets /
    // debts.
    const group = (g, w) => {
      const G = GROUP[g.key];
      const base = g.asset ? w.assets : w.debts;
      const st = nwState(g);
      const c = st === 'part' ? g.custom : g;
      const addStep = {property: 'form:property', vehicles: 'form:car', loans: 'form:loan', investments: 'form:invest'}[g.key];
      return h('div', {class: `grp${st === 'on' ? '' : ` ${st}`}`, dataset: {nw: st}}, ibadge(G.i, G.c),
        h('span', {class: 'grow'}, h('b', {}, g.label, h('span', {class: 'cnt'}, ` · ${g.count}`)),
          st === 'off' ? h('span', {}, 'Not counted')
            : g.count ? [st === 'part' ? h('span', {class: 'of'}, `${g.custom.count} of ${g.count} counted`) : null,
              h('div', {class: 'share'}, h('i', {style: {width: pct(base > 0 ? c.total / base : 0), background: `rgb(${G.c})`}}))]
              : h('span', {}, 'Nothing added yet')),
        g.count || !addStep ? h('span', {class: `gtot v${c.signed < 0 ? ' bad' : ''}`}, money2(c.signed))
          : h('button', {class: 'fchip', type: 'button', dataset: {add: addStep}}, 'Add'));
    };
    let trend;
    let drawTrend = null;
    if (nw.history.length >= 2) {
      const [box, canvas] = chartBox('ch-nw fillh', 'Net worth over time, from daily balance snapshots');
      trend = box;
      drawTrend = () => {
        destroyCharts(box);
        const k = customMode() ? 'custom_' : '';
        lineChart(canvas, nw.history.map((p) => ({...p, value: p[`${k}net`]})), {label: customMode() ? 'Custom' : 'Net worth',
          after: (p) => `Own ${money2(p[`${k}assets`])} · Owe ${money2(p[`${k}debts`])}`});
      };
      later.push(drawTrend);
    } else {
      trend = h('div', {style: {'margin-top': '14px'}}, emptyNote('sprout', nw.tracking_since ? `Your trend line started ${fmtDate(nw.tracking_since)}`
        : 'Your trend line starts with the next sync', 'We save one snapshot a day. The chart fills in as the days go by.'));
    }
    const what = small('');
    const big = bigMoney(0);
    const left = nw.custom.excluded.length;
    const note = h('p', {class: 'nwnote'}, h('button', {type: 'button', onclick: () => worthSheet(d)},
      `Custom · ${left ? `${plural(left, 'account')} not counted` : 'every account counted'}`));
    const split = h('div', {class: 'split', style: {height: '10px', margin: '18px 0 8px'}, role: 'img'});
    const ownowe = h('div', {class: 'ac', style: {'font-size': '14px'}});
    const transit = h('div');
    const groups = h('div', {style: {'margin-top': '14px'}});
    const worth = card({cls: `s2${nw.history.length >= 2 ? ' grow' : ''}`, title: 'Net worth', icon: 'landmark', rgb: RGB.ok, extra: [nwSeg(), what]},
      h('div', {class: 'herohead'}, big, stale.length ? pill('warn sm', 'clock', `${plural(stale.length, 'balance')} to update`) : null),
      note, split, ownowe, transit, groups, trend);
    let drawn = false;
    nwView('accounts', worth, () => {
      const w = worthOf(nw);
      what.textContent = customMode() ? 'only the accounts you count' : '';
      fill(big, splitMoney(w.net));
      note.hidden = !customMode();
      fill(split, ownOwe(w));
      split.setAttribute('aria-label', `Own ${money(w.assets)}, owe ${money(w.debts)}`);
      fill(ownowe, h('span', {}, 'Own ', h('b', {class: 'pos v'}, money2(w.assets))),
        h('span', {}, 'Owe ', h('b', {class: 'bad v'}, money2(w.debts))));
      fill(transit, transitBoxes(nw));
      fill(groups, nw.breakdown.map((g) => group(g, w)));
      if (drawn && drawTrend) drawTrend();  // the first time, after the page is on screen
      drawn = true;
    });
    const conns = connectionsCard(d);
    worth.dataset.pin = 'start';
    conns.dataset.pin = 'end';
    // Approved density mock D2 B dropped "Added by you", so Bank connections is a lone card, no longer a stack: beside
    // Net worth (3 columns), or its own full row (2 columns) instead of leaving a column empty under Net worth.
    conns.dataset.flex = '';
    const grid = h('div', {class: 'grid'}, worth, conns);

    // one card per group: Plaid accounts, then what you added
    const plaidBy = {};
    for (const inst of nw.institutions) {
      for (const a of inst.accounts) {
        const g = TYPE_GROUP[a.type];
        if (g) (plaidBy[g] = plaidBy[g] || []).push([a.api ? {...a, account_status: inst.account_status,
          as_of: inst.as_of, fetched_at: inst.fetched_at} : a, inst.institution_name, inst.institution_id]);
      }
    }
    const manualBy = {};
    for (const m of d.manual) (manualBy[m.group] = manualBy[m.group] || []).push(m);
    const emptyAdd = {property: 'form:property', vehicles: 'form:car', loans: 'form:loan'};
    const cards = nw.breakdown.map((g) => {
      const G = GROUP[g.key];
      const plaid = (plaidBy[g.key] || []).sort((x, y) => (y[0].current || 0) - (x[0].current || 0));
      const rows = [...plaid.map(([a, inst, id]) => plaidRow(a, inst, d.sparks, d.liabilities, id, nw.in_transit)), ...(manualBy[g.key] || []).map((m) => manualLine(m))];
      if (!rows.length && !emptyAdd[g.key]) return null;
      const extra = [];
      if (g.key === 'cards') {
        const lim = sum(plaid, ([a]) => a.limit) + sum(manualBy.cards || [], (m) => m.credit_limit);
        if (lim) {
          const used = sum(plaid.filter(([a]) => a.limit), ([a]) => a.current) + sum((manualBy.cards || []).filter((m) => m.credit_limit), (m) => m.balance);
          extra.push(h('div', {class: 'note'}, icon('info'), h('span', {}, `Using ${Math.round((used / lim) * 100)}% of ${money(lim)} in limits. Under 30% is great.`)));
        }
      }
      if (!rows.length && emptyAdd[g.key]) {
        const step = emptyAdd[g.key];
        return h('div', {class: 'card emptyg', dataset: {emptyGroup: g.key}},
          ibadge(G.i, G.c, 's'), h('b', {class: 'ttl'}, g.label), h('small', {}, 'none yet'),
          h('button', {class: 'fchip addbtn', type: 'button', dataset: {add: step}}, 'Add'));
      }
      const off = h('span', {class: 'offtag'}, 'Not counted');
      const total = h('b', {class: 'v', style: {color: g.signed < 0 ? 'var(--bad)' : 'var(--fg)'}}, money2(g.signed));
      const sec = card({cls: 'acctgrp', title: g.label, icon: G.i, rgb: G.c,
        extra: h('small', {}, `${g.count} · `, total)}, monthChange(g), rows, extra);
      nwView(`accounts-group-${g.key}`, sec, () => {
        const excluded = nwState(g) === 'off';
        sec.classList.toggle('off', excluded);
        off.hidden = !excluded;
      });
      sec.dataset.fold = g.key;
      const shut = ACCT_SHUT.has(g.key);
      const fold = h('button', {type: 'button', class: 'gfold', 'aria-expanded': String(!shut),
        'aria-label': `${shut ? 'Expand' : 'Collapse'} ${g.label}`, onclick: () => {
          const now = !sec.classList.contains('shut');
          sec.classList.toggle('shut', now);
          fold.setAttribute('aria-expanded', String(!now));
          fold.setAttribute('aria-label', `${now ? 'Expand' : 'Collapse'} ${g.label}`);
          if (now) ACCT_SHUT.add(g.key); else ACCT_SHUT.delete(g.key);
          saveAcctShut();
          pack(grid2);   // a folded card is shorter: re-pack so the grid has no gaps
        }}, h('span', {class: 'chev', 'aria-hidden': 'true'}, icon('chevd')));
      if (shut) sec.classList.add('shut');
      sec.querySelector('h3').append(fold, off);
      return sec;
    }).filter(Boolean);
    const grid2 = h('div', {class: 'grid two'}, cards);
    fill($('page'), grid, grid2, ghostRow('plus', RGB.warn, 'Add an account or asset',
      'A card, loan, property or vehicle Plaid can’t see', null, {add: 'home'}));
    later.forEach((fn) => fn());
    pack(grid);
    pack(grid2);
  }

  // ---------- the Custom net worth's sheet (TASK-15) ---------------------------------------------------------------
  // Every account, grouped like the Accounts page, each with a switch; a group's own switch turns all of it on or off
  // (mixed while only some of it counts) and its name folds it away. The preview at the top is the Custom net worth
  // with the switches as they are; the presets set them all at once ("Cash & debts": every investment, vehicle,
  // property and other asset off). Save stores the accounts switched off (POST /api/settings networth_excluded) and
  // turns Custom on; Cancel, Esc or a tap outside keeps what was saved. Archived accounts aren't listed (they never count).
  // TASK-22b: a payment in transit to a card switched on counts as paid in the preview and its Owe, with its line.
  function worthSheet(d, pending) {  // pending: a selection whose save failed, shown again to retry
    const nw = d.net_worth;
    const groups = nw.breakdown.filter((g) => g.count).map((g) => ({g, rows: []}));
    const plaid = [];
    for (const inst of nw.institutions) {
      for (const a of inst.accounts) {
        if (!TYPE_GROUP[a.type]) continue;
        plaid.push({id: a.account_id, group: TYPE_GROUP[a.type], name: a.name || 'Account', mask: a.mask, balance: a.current || 0,
          sub: [SUBTYPE[a.subtype] || a.subtype || a.type, instShort(inst.institution_name)].filter(Boolean).join(' · '),
          art: () => ia(inst.institution_name, 's', null, inst.institution_id)});
      }
    }
    plaid.sort((x, y) => y.balance - x.balance);  // as on the Accounts page: largest first, then what you added
    const mine = d.manual.filter((m) => !m.archived_at).map((m) => ({id: manualNwId(m.id), group: m.group, name: m.name, mask: null,
      balance: m.balance, sub: [KIND[m.kind].label, m.institution].filter(Boolean).join(' · '),
      art: () => manualChip(m.institution, 's') || ibadge(KIND[m.kind].i, KIND[m.kind].c, 's')}));
    for (const r of [...plaid, ...mine]) {
      const x = groups.find((y) => y.g.key === r.group);
      if (x) x.rows.push({...r, asset: x.g.asset, signed: x.g.asset ? r.balance : -r.balance});
    }
    const all = groups.flatMap((x) => x.rows);
    const off = new Set(pending ? pending.filter((id) => all.some((r) => r.id === id)) : all.filter((r) => leftOut(r.id)).map((r) => r.id));
    const cents = (x) => Math.round((Number(x) || 0) * 100);
    const presets = [['Everything', []], ['Cash & debts', all.filter((r) => r.asset && r.group !== 'cash').map((r) => r.id)]]
      .map(([label, ids]) => [h('button', {type: 'button', class: 'fchip', 'aria-pressed': 'false', onclick: () => {
        off.clear();
        ids.forEach((id) => off.add(id));
        sync();
      }}, label), ids]);
    const figure = bigMoney(0);
    const bar = h('div', {class: 'split', role: 'img'});
    const ownowe = h('div', {class: 'ac'});
    const transit = h('div');
    const count = h('span', {class: 'nwcnt'});
    const sections = groups.map((x) => {
      const G = GROUP[x.g.key];
      const id = `nwg-${x.g.key}`;
      const tally = h('span', {});
      const total = h('span', {class: 'amt v'});
      const gsw = h('button', {type: 'button', class: 'tgl', role: 'checkbox', 'aria-label': `Count all ${x.g.label}`, onclick: () => {
        const allOn = x.rows.every((r) => !off.has(r.id));
        x.rows.forEach((r) => (allOn ? off.add(r.id) : off.delete(r.id)));
        sync();
      }}, h('i'));
      const rows = x.rows.map((r) => {
        const sw = h('button', {type: 'button', class: 'tgl', role: 'switch', 'aria-label': `Count ${acctName(r.name)}${r.mask ? ` ••${r.mask}` : ''}`,
          onclick: (e) => {
            e.stopPropagation();
            if (off.has(r.id)) off.delete(r.id);
            else off.add(r.id);
            sync();
          }}, h('i'));
        const row = h('div', {class: 'nwr', dataset: {id: r.id}, onclick: () => sw.click()}, r.art(),
          h('span', {class: 'grow'}, h('b', {class: 'nmrow'}, h('span', {class: 'nm'}, acctName(r.name)), r.mask ? h('span', {class: 'dim mk'}, `••${r.mask}`) : null),
            h('span', {}, r.sub)),
          h('span', {class: `amt v${r.signed < 0 ? ' bad' : ''}`}, money2(r.signed)), sw);
        r.sync = () => {
          sw.setAttribute('aria-checked', String(!off.has(r.id)));
          row.classList.toggle('off', off.has(r.id));
        };
        return row;
      });
      const sec = h('section', {class: 'nwg', dataset: {group: x.g.key}});
      const tog = h('button', {type: 'button', class: 'nwgt', 'aria-expanded': 'true', 'aria-controls': id, onclick: () => {
        tog.setAttribute('aria-expanded', String(!sec.classList.toggle('shut')));
      }}, icon('chevd'), ibadge(G.i, G.c, 's'), h('span', {class: 'grow'}, h('b', {}, x.g.label), tally));
      x.sync = () => {
        const n = x.rows.filter((r) => !off.has(r.id)).length;
        gsw.setAttribute('aria-checked', n === x.rows.length ? 'true' : n ? 'mixed' : 'false');
        tally.textContent = `${n} of ${x.rows.length} counted`;
        const t = x.rows.reduce((acc, r) => acc + (off.has(r.id) ? 0 : cents(r.signed)), 0) / 100;
        total.textContent = money2(t);
        total.classList.toggle('bad', t < 0);
        x.rows.forEach((r) => r.sync());
      };
      return add(sec, [h('div', {class: 'nwgh'}, tog, total, gsw), h('div', {class: 'nwrows', id}, rows)]);
    });
    const sync = () => {
      let a = 0;
      let b = 0;
      for (const r of all) {
        if (off.has(r.id)) continue;
        if (r.asset) a += cents(r.balance);
        else b += cents(r.balance);
      }
      const paid = (nw.in_transit || []).filter((e) => !off.has(e.card_id));  // the server's counted_custom, for this selection
      b -= paid.reduce((acc, e) => acc + cents(e.amount), 0);
      const w = {assets: a / 100, debts: b / 100, net: (a - b) / 100};
      fill(figure, splitMoney(w.net));
      fill(bar, ownOwe(w));
      bar.setAttribute('aria-label', `Own ${money(w.assets)}, owe ${money(w.debts)}`);
      fill(ownowe, h('span', {}, 'Own ', h('b', {class: 'pos v'}, money2(w.assets))), h('span', {}, 'Owe ', h('b', {class: 'bad v'}, money2(w.debts))));
      fill(transit, paid.map(transitLine));
      count.textContent = off.size ? `${off.size} of ${plural(all.length, 'account')} not counted` : `all ${plural(all.length, 'account')} counted`;
      groups.forEach((x) => x.sync());
      for (const [btn, ids] of presets) btn.setAttribute('aria-pressed', String(ids.length === off.size && ids.every((i) => off.has(i))));
    };
    // No Save / Cancel (2026-09-27 review): closing the sheet (×, Escape, a tap outside) saves what changed and
    // leaves the page in Custom. Nothing changed: nothing is sent. A failed save says so and offers to try again.
    const before = all.filter((r) => leftOut(r.id)).map((r) => r.id).sort().join('|');  // what's saved now
    const persist = async () => {
      rememberNwMode('custom');
      const ids = [...off];
      if (ids.slice().sort().join('|') === before) {
        refresh();
        return;
      }
      try {
        const r = await post('/api/settings', {networth_excluded: ids});
        const n = r.networth_excluded.length;
        toast(`Custom net worth saved: ${n ? `${plural(n, 'account')} not counted` : 'every account counts'}${r.note ? `. ${r.note}` : ''}`, [],
          {timeout: 4000});
      } catch (e) {
        toast(`Couldn’t save Custom net worth: ${e.message}`, [{label: 'Try again', primary: true, onclick: () => { hideToast(); worthSheet(d, ids); }}],
          {error: true});
      }
      refresh();
    };
    const head = h('div', {class: 'nwhead'},
      h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Custom net worth'), closeBtn()),
      h('p', {}, 'Switch off what shouldn’t count. Changes save when you close.'),
      h('div', {class: 'nwprev'}, h('div', {class: 'nwpl'}, h('span', {}, 'Custom'), count), h('div', {'aria-live': 'polite'}, figure), bar, ownowe, transit),
      h('div', {class: 'nwpre'}, h('span', {}, 'Start from'), presets.map(([btn]) => btn)));
    openSheet([head, h('div', {class: 'nwlist'}, sections.length ? sections : msgBox('No accounts yet', 'Connect a bank or add an account first.'))],
      {cls: 'nwsheet', focus: sections.length ? sections[0].querySelector('.nwgt') : null, onClose: persist});
    sync();
  }

  function connectionsCard(d) {
    const active = d.items.filter((it) => !it.removed_at);
    const broken = active.filter((it) => it.status !== 'active' || it.error_code);
    const removed = d.items.filter((it) => it.removed_at);
    const api = (d.api_items || []).filter((it) => !it.removed_at);   // TASK-22f: Alpaca counts as a connection, not a bank
    const apiBad = api.filter((it) => it.status !== 'active' || it.error_code);
    const left = d.slot_limit - d.slots_used;
    const full = left <= 0;
    const line = (it) => {
      const bad = !it.removed_at && (it.status !== 'active' || it.error_code);
      const name = it.institution_name || it.institution_id || it.item_id;
      return h('div', {class: `conn${it.removed_at ? ' removed' : ''}`}, ia(name, 's', it.removed_at ? null : bad ? 'bad' : 'ok', it.institution_id),
        h('span', {class: 'grow'}, h('b', {title: name}, instShort(name)),
          h('span', {}, it.removed_at ? 'Removed · still uses a slot' : bad ? `${it.error_code === 'ITEM_LOGIN_REQUIRED' || it.status === 'login_required'
            ? 'Needs a re-login' : `Problem: ${it.error_code || it.status}`}` : `Synced ${it.last_sync_at ? syncLabel(it.last_sync_at) : 'never'}`)),
        it.removed_at ? null : bad ? reconnectBtn(it.item_id) : updateBtn(it.item_id, instShort(name)));
    };
    return card({title: 'Bank connections', icon: 'link', rgb: RGB.acc},
      h('div', {class: 'slotbig'}, ring(d.slots_used / d.slot_limit, ACC, 78, 8, h('b', {class: 'v', style: {'font-size': '18px'}}, String(d.slots_used)),
        h('small', {}, `of ${d.slot_limit}`)),
      h('div', {}, h('div', {style: {'font-weight': '650', 'font-size': '16px'}}, full ? 'No connections left' : `${plural(left, 'connection')} left`),
        h('div', {class: 'mut', style: {'font-size': '14px', 'margin-top': '2px'}}, active.length || api.length ? (broken.length
          ? `${plural(broken.length, 'bank')} need${broken.length === 1 ? 's' : ''} you to log in again`
          : apiBad.length ? `${instShort(apiBad[0].institution_name)} needs attention` : `All ${plural(active.length + api.length, 'connection')} healthy`)
          : 'No banks linked yet', d.last_sync ? ` · synced ${syncLabel(d.last_sync)}` : ''))),
      h('button', {class: 'cbtn', type: 'button', style: {'margin-top': '18px'}, dataset: {add: 'home'}}, icon('plus'), 'Add account'),
      active.length || removed.length ? h('div', {style: {'margin-top': '14px'}}, [...broken, ...active.filter((it) => !broken.includes(it))].map(line),
        removed.map(line)) : null,
      full ? h('p', {class: 'nc warn'}, `All ${d.slot_limit} Plaid slots are used. Use Reconnect for existing banks.`) : null,
      d.blocked_duplicate_links ? h('p', {class: 'nc warn'}, `${plural(d.blocked_duplicate_links, 'duplicate Link completion')} ${d.blocked_duplicate_links === 1 ? 'was' : 'were'} blocked before exchange; Plaid may have counted ${d.blocked_duplicate_links === 1 ? 'it' : 'them'} (worst case ${d.slots_used + d.blocked_duplicate_links} / ${d.slot_limit}).`) : null);
  }

  // ---------- a car's / property's page (TASK-13b) --------------------------------------------------------------
  // #/asset/<id>, from GET /api/manual/<id>/detail: the value and its change, the picture (fetched once in the
  // background: a cut-out Wikimedia Commons photo of the car, the home's listing photo or a map tile with a pin; a drawn
  // car / house until then, never a broken image), the value table (Carfax's three figures, or Redfin's), the linked
  // loan and the equity, the history, specs and safety (NHTSA + Carfax), and the details with their actions.
  const kv = (label, value, note, cls) => h('div', {class: cls}, h('dt', {}, label), h('dd', {}, value, note ? h('small', {}, note) : null));
  const cap1 = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  function art(name) {  // the drawn car / house (static SVG in the page's <template id="art">)
    return $('art').content.querySelector(`[data-art="${name}"]`).cloneNode(true);
  }

  function picture(d) {
    const p = d.photo;
    let box;
    if (p.status === 'ready' && p.url) {
      const what = d.kind === 'vehicle' ? `Photo of a ${d.title}` : `Photo of ${d.details.address || d.name}`;
      const img = h('img', {src: p.url, alt: p.kind === 'map' ? `Map around ${d.details.address || d.name}` : what, decoding: 'async'});
      if (p.kind === 'map' && p.pin) {  // object-position at the pin's spot keeps it inside the crop, at the same spot
        const at = [p.pin.x, p.pin.y].map((f) => `${(f * 100).toFixed(1)}%`);
        img.style.setProperty('object-position', at.join(' '));
        box = h('div', {class: 'vd-pic map'}, img, h('span', {class: 'vd-pin', 'aria-hidden': 'true', style: {left: at[0], top: at[1]}}));
      } else {
        box = h('div', {class: `vd-pic ${p.kind}`}, img);
      }
    } else {
      box = h('div', {class: 'vd-pic drawn', role: 'img', 'aria-label': d.kind === 'vehicle' ? 'A drawn car (no photo yet)' : 'A drawn house (no photo)'},
        art(d.kind === 'vehicle' ? 'car' : 'house'));
    }
    const credit = p.status === 'ready' && p.credit
      ? h('div', {class: 'vd-credit'}, p.credit_url ? h('a', {href: p.credit_url, target: '_blank', rel: 'noopener noreferrer'}, p.credit) : p.credit)
      : p.status === 'pending' ? h('div', {class: 'vd-credit'}, 'Finding a photo…') : null;
    return h('div', {class: 'vd-picw', dataset: {photo: p.status, kind: p.kind}}, box, credit);
  }

  function assetAct(e, url, body, done) {  // a button that posts, then reloads the page (a toast says what happened)
    const btn = e.currentTarget;
    btn.disabled = true;
    post(url, body).then((res) => { done(res); refresh(); }).catch((err) => {
      btn.disabled = false;
      toast(`Could not save: ${err.message}`, [], {error: true, timeout: 6000});
    });
  }

  function estimateToast(res, what) {
    const r = res.estimate_result;
    toast([what, r ? ` ${r.ok ? `${r.message}.` : `${r.provider === 'carfax' ? 'Carfax' : 'Redfin'}: ${isoDays(r.message)}.`}` : ''], [],
      {timeout: r && !r.ok ? 8000 : 4500});
  }

  function assetHead(d) {
    const v = d.value;
    const chg = v.change ? h('span', {class: 'vd-chg'}, `${v.change.amount < 0 ? '↓ ' : v.change.amount > 0 ? '↑ ' : ''}${money(Math.abs(v.change.amount))} since ${fmtDate(v.change.since)}`)
      : v.first_check ? h('span', {class: 'vd-chg'}, `first ${d.kind === 'vehicle' ? 'Carfax' : 'Redfin'} check`) : null;
    return h('section', {class: 'vd-head', 'aria-label': 'Value'},
      h('div', {class: 'vd-row'}, bigMoney(v.amount), chg, d.archived ? pill('nt sm', 'archive', 'Archived') : null),
      h('div', {class: 'sub'}, v.line));
  }

  function valueTable(d) {
    return h('table', {class: 'vd-vt'}, h('tbody', {}, d.table.map((r) => h('tr', {class: r.primary ? 'pri' : r.value == null ? 'none' : null},
      h('td', {}, r.label, ' ', h('span', {class: 's'}, `(${r.source})`)),
      h('td', {class: 'v'}, r.value == null ? r.empty || 'no estimate' : money(r.value))))));
  }

  function quietNotes(d) {
    const n = d.notes;
    const q = (cls, iconName, ...kids) => h('div', {class: `vd-quiet${cls ? ` ${cls}` : ''}`}, icon(iconName), h('span', {}, kids));
    return [
      n.odometer ? q('', 'gauge', `${n.odometer.message} `, h('button', {class: 'lnk', type: 'button', onclick: () => openAdd(`edit:${d.id}`)}, 'Update mileage')) : null,
      n.blocked ? q('warn', 'alert', `${isoDays(n.blocked)}.`) : null,
      n.no_estimate ? q('', 'info', `${n.no_estimate}.`) : null,
      n.your_value ? q('', 'pencil', `${n.your_value}. `, h('button', {class: 'lnk', type: 'button', onclick: (e) => assetAct(e, `/api/manual/${d.id}/use-estimate`, {},
        (res) => toast(['Now using the estimate: ', h('b', {}, money(res.balance)), '.'], [], {timeout: 4500}))}, 'Use the estimate')) : null,
    ];
  }

  function heroCard(d) {
    const f = d.facts;
    const facts = d.kind === 'property' && f.beds && f.baths && f.sqft
      ? h('div', {class: 'vd-facts'}, h('div', {}, h('b', {class: 'v'}, NF0.format(f.beds)), 'beds'),
        h('div', {}, h('b', {class: 'v'}, String(f.baths)), 'baths'), h('div', {}, h('b', {class: 'v'}, NF0.format(f.sqft)), 'sq ft')) : null;
    const sell = d.sell ? h('div', {class: 'vd-sell'}, h('b', {}, 'Want to sell?'),
      h('a', {class: 'lnk', href: d.sell.url, target: '_blank', rel: 'noopener noreferrer'}, `${d.sell.label} ↗`))
      : f && f.redfin_url ? h('div', {class: 'vd-sell'}, h('b', {}, 'On Redfin'),
        h('a', {class: 'lnk', href: f.redfin_url, target: '_blank', rel: 'noopener noreferrer'}, 'See the listing ↗')) : null;
    return h('section', {class: 'card vd-herocard', 'aria-label': 'What it is worth'}, h('div', {class: 'vd-hero'},
      h('div', {}, picture(d), h('div', {class: 'vd-cap'}, h('b', {}, d.kind === 'vehicle' ? d.title : d.details.address || d.name)), facts),
      h('div', {}, h('div', {class: 'vd-lbl'}, "What it's worth"), valueTable(d),
        d.source_line ? h('div', {class: 'vd-src'}, d.source_line) : null, quietNotes(d), sell)));
  }

  function loanCard(d) {
    const title = d.kind === 'vehicle' ? 'Loan & equity' : 'Mortgage & equity';
    const l = d.loan;
    if (!l) {
      return card({title, icon: 'wallet', rgb: RGB.ok}, msgBox('No loan linked', h('span', {}, 'Pick its loan in ',
        h('button', {class: 'lnk', type: 'button', onclick: () => openAdd(`edit:${d.id}`)}, 'Edit'), ' to see your equity.')));
    }
    const share = l.loan_share == null ? 1 : l.loan_share;
    return card({cls: 'vd-eq', title, icon: 'wallet', rgb: RGB.ok},
      h('p', {class: 'sub'}, `${l.name}${l.institution ? ` · ${l.institution}` : ''}`),
      h('div', {class: 'vd-lbl'}, 'Equity (value − loan)'),
      bigMoney(l.equity, `s${l.equity < 0 ? ' bad' : ''}`, l.equity < 0),
      h('div', {class: 'vd-bar', role: 'img', 'aria-label': `Loan ${Math.round(share * 100)}% of the value, equity ${Math.round((1 - share) * 100)}%`},
        h('i', {class: 'ln', style: {width: pct(share)}}), share < 1 ? h('i', {class: 'eq', style: {width: pct(1 - share)}}) : null),
      h('div', {class: 'vd-legend'}, h('span', {}, h('i', {class: 'ln'}), 'Loan ', h('b', {class: 'v'}, money2(l.balance))),
        h('span', {}, h('i', {class: 'eq'}), 'Equity ', h('b', {class: 'v'}, money2(l.equity)))),
      h('dl', {class: 'vd-kv one'}, kv('Payment', l.monthly_payment ? `${money2(l.monthly_payment)}/mo` : '—'),
        kv('Paid off', l.payoff_month ? monthYear(l.payoff_month) : '—',
          l.payments_left != null ? `${plural(l.payments_left, 'payment')} left${l.interest_rate ? ` at ${l.interest_rate}% APR` : ''}` : null)),
      h('div', {class: 'vd-foot'}, h('button', {class: 'lnk', type: 'button', dataset: {loan: String(l.id)}}, 'Loan details')));
  }

  function historyCard(d, later) {
    const pts = d.history;
    const noun = d.kind === 'vehicle' ? 'trade-in' : 'estimate';
    let body;
    if (pts.length >= 2) {
      const [box, canvas] = chartBox('ch-asset', `${d.title}: value over time`);
      body = box;
      later.push(() => lineChart(canvas, pts, {label: cap1(noun)}));
    } else {
      const next = d.details.next_check ? ` (${fmtDate(d.details.next_check)})` : '';
      body = emptyChart('Not enough history yet', pts.length
        ? `The chart shows up after the next monthly check${next}: one ${noun} so far, ${money(pts[0].value)} on ${fmtDate(pts[0].date)}.`
        : `It starts with the first ${d.kind === 'vehicle' ? 'Carfax' : 'Redfin'} estimate${next}.`);
    }
    return card({title: 'Value history', icon: 'linechart', rgb: RGB.acc, extra: small(plural(pts.length, 'check'))}, body);
  }

  function specsCard(d) {
    const vin = h('span', {class: 'v'}, d.vin_masked || '—');
    const rows = d.specs.map(([k, v]) => kv(k, v));
    if (d.vin) {
      rows.push(kv('VIN', [vin, h('button', {class: 'lnk', type: 'button', 'aria-label': 'Show the whole VIN',
        onclick: (e) => { vin.textContent = d.vin; e.currentTarget.remove(); }}, 'Show')]));
    }
    if (d.records != null) rows.push(kv('History', plural(d.records, 'Carfax record')));
    return card({title: 'Specs', icon: 'gauge', rgb: RGB.acc, extra: small('NHTSA · Carfax')},
      rows.length ? h('dl', {class: 'vd-kv'}, rows) : msgBox('No specs yet', 'They come with the VIN decode and the first Carfax estimate.'));
  }

  function safetyCard(d) {
    const top = d.safety.slice(0, 8);
    const rest = d.safety.slice(8);
    return card({title: 'Safety & driver assist', icon: 'shield', rgb: RGB.ok},
      h('p', {class: 'sub'}, 'from NHTSA'),
      top.length ? h('div', {class: 'vd-chips'}, top.map((s) => h('span', {class: 'vd-chip'}, icon('check'), s.label,
        s.value !== 'Standard' ? [' ', h('small', {}, s.value)] : null)))
        : d.safety_state === 'no_vin' ? msgBox('No VIN yet', 'Add the VIN (Edit) and NHTSA fills this in.')
        : d.safety_state === 'not_fetched' ? msgBox('Not fetched yet', 'It fills in at the next value check.')
        : msgBox('No safety data', "NHTSA's decode of this VIN lists none."),
      top.length ? h('div', {class: 'vd-foot'}, rest.length ? `Also: ${rest.map((s) => `${s.label.toLowerCase()}${s.value !== 'Standard' ? ` (${s.value})` : ''}`).join(', ')}. ` : '',
        "As NHTSA's decode of the VIN lists them (standard unless marked).") : null);
  }

  function detailsCard(d) {
    const t = d.details;
    const edit = () => h('button', {class: 'lnk', type: 'button', onclick: () => openAdd(`edit:${d.id}`)}, 'Edit');
    const loan = d.loan ? `${d.loan.name}${d.loan.institution ? ` · ${d.loan.institution}` : ''}` : 'None linked';
    const schedule = kv('Estimates', t.enabled ? `${t.provider_label}, monthly` : `${t.provider_label} is off`,
      t.next_check ? `next check ${fmtDate(t.next_check)}` : t.refresh_note ? isoDays(t.refresh_note) : null);
    let rows;
    if (d.kind === 'vehicle') {
      const seg = h('div', {class: 'seg', role: 'group', 'aria-label': 'Condition'}, ['fair', 'good', 'excellent'].map((c) =>
        h('button', {type: 'button', 'aria-pressed': String(c === t.condition), disabled: d.archived,
          onclick: (e) => { if (c !== t.condition) assetAct(e, `/api/manual/${d.id}`, {vehicle_condition: c}, (res) => estimateToast(res, `Condition: ${cap1(c)}.`)); }},
        cap1(c))));
      rows = [kv('Mileage', [t.mileage != null ? `${NF0.format(t.mileage)} mi` : '—', edit()], t.mileage_as_of ? `last updated ${fmtDate(t.mileage_as_of)}` : null),
        kv('Condition', seg, null, 'vd-seg'), kv('ZIP code', [t.zip_code || '—', edit()]), schedule, kv('Loan', [loan, edit()])];
    } else {
      rows = [kv('Address', [t.address || '—', edit()]), schedule, kv('Mortgage', [loan, edit()])];
    }
    const ask = () => openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, `Archive ${d.name}?`), closeBtn()),
      h('p', {}, 'It stops counting in every total. Its history is kept.'),
      h('div', {class: 'row'}, h('button', {class: 'btn danger', type: 'button', onclick: () => archive({id: d.id, name: d.name})}, icon('archive'), 'Archive'),
        h('button', {class: 'btn ghost', type: 'button', onclick: closeModal}, 'Cancel'))]);
    return card({cls: 's2', title: 'Details', icon: d.kind === 'vehicle' ? 'car' : 'house', rgb: d.kind === 'vehicle' ? RGB.cyan : RGB.purple},
      h('dl', {class: `vd-kv${d.kind === 'property' ? ' one' : ''}`}, rows),
      d.archived ? null : h('div', {class: 'vd-actions'},
        h('button', {class: 'btn ghost sm', type: 'button', onclick: () => openAdd(`edit:${d.id}`)}, icon('pencil'), 'Edit'),
        h('button', {class: 'btn ghost sm', type: 'button', disabled: !t.can_refresh, title: t.can_refresh ? null : isoDays(t.refresh_note || ''),
          onclick: (e) => assetAct(e, `/api/manual/${d.id}/refresh`, {}, (res) => estimateToast(res, 'Refreshed.'))}, icon('refresh'), 'Refresh estimate'),
        h('span', {class: 'sp'}),
        h('button', {class: 'btn danger sm', type: 'button', onclick: ask}, icon('archive'), 'Archive')));
  }

  async function renderAsset(tk) {
    const id = Number(S.params.get('id'));
    fill($('page'), skeleton(['card s3', 'card', 'card']));
    const d = await api(`/api/manual/${id}/detail`);
    if (tk !== S.token) return;
    $('title').textContent = d.title;
    document.title = `${d.title} · Hermes Finances`;
    const later = [];
    const rows = d.kind === 'vehicle'
      ? [h('div', {class: 'grid two vd-row2'}, loanCard(d), historyCard(d, later)),
        h('div', {class: 'grid two vd-row2'}, specsCard(d), safetyCard(d))]
      : [h('div', {class: 'grid two vd-row2'}, loanCard(d), historyCard(d, later))];
    fill($('page'), assetHead(d), heroCard(d), rows, h('div', {class: 'grid two'}, detailsCard(d)));
    later.forEach((fn) => fn());
    if (d.photo.status === 'pending') waitForPhoto(tk, id, 0);
  }

  function waitForPhoto(tk, id, n) {  // the picture is fetched in the background: swap it in when it's there
    if (n >= 10) return;
    setTimeout(async () => {
      if (tk !== S.token) return;
      const d = await api(`/api/manual/${id}/detail`).catch(() => null);
      if (!d || tk !== S.token) return;
      if (d.photo.status === 'pending') {
        waitForPhoto(tk, id, n + 1);
        return;
      }
      const old = document.querySelector('#page .vd-picw');
      if (old) old.replaceWith(picture(d));
    }, 3000);
  }

  // ---------- Investments ----------------------------------------------------------------------
  const RANGES = [['7d', '7D', 'past 7 days'], ['1m', '1M', 'past month'], ['ytd', 'YTD', 'this year'], ['all', 'All', 'since tracking began']];
  const TYPE_COLORS = {Brokerage: '#8ea8ff', Robo: '#5fc6e8', Retirement: '#c792ea', HSA: '#4fd1a5', Crypto: '#f5b454', Other: '#9ba2b6'};
  const BK_COLORS = {General: '#8ea8ff', Retirement: '#c792ea', Crypto: '#f5b454'};
  const INV = {range: '1m', alloc: 'holding'};

  function rangeStart(range, today) {
    const d = asDate(today);
    if (range === '7d') d.setDate(d.getDate() - 7);
    else if (range === '1m') d.setMonth(d.getMonth() - 1);
    else if (range === 'ytd') return `${today.slice(0, 4)}-01-01`;
    else return '0000-00-00';
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  async function renderInvestments(tk) {
    fill($('page'), skeleton(['card s2', 'card', 'card', 'card', 'card']));
    const [d] = await Promise.all([api('/api/investments'), ensureAccounts().catch(() => null)]);  // accounts: the logos
    if (tk !== S.token) return;
    const later = [];
    const top = investTotalCard(d, later);
    const alloc = allocationCard(d, later);
    alloc.dataset.flex = '';  // beside the total where there's room (1440); alone in a row, both columns (1024)
    const grid = h('div', {class: 'grid'}, top, alloc);
    const grid2 = h('div', {class: 'grid'}, investAccountsCard(d), holdingsCard(d), breakdownCard(d));
    fill($('page'), grid, grid2);
    later.forEach((fn) => fn());
    pack(grid);
    pack(grid2);
  }

  function investTotalCard(d, later) {
    const chipSlot = h('span');
    const area = h('div', {class: 'fillh-wrap'});
    const seg = h('div', {class: 'seg', role: 'group', 'aria-label': 'Time range'});
    const draw = () => {
      destroyCharts(area);
      const [, , words] = RANGES.find((r) => r[0] === INV.range);
      const c = d.changes[INV.range];
      fill(chipSlot, c ? pill(c.amount < 0 ? 'bad' : '', c.amount < 0 ? 'income' : 'trend',
        `${c.amount < 0 ? '' : '+'}${money2(c.amount)}${c.pct != null ? ` (${Math.abs(c.pct).toFixed(1)}%)` : ''} ${words}`)
        : d.change_7d_from && d.change_7d_from > d.today ? pill('nt', 'clock', `7-day change shows up ${fmtDate(d.change_7d_from)}`)
          : pill('nt', 'clock', d.days ? `Not enough history for ${words} yet` : 'Changes show up after a few daily syncs'));
      const start = rangeStart(INV.range, d.today);
      const pts = d.history.filter((p) => p.date >= start);
      const [box, canvas] = chartBox('chwrap fillh', 'Portfolio value over time, from daily snapshots');
      if (d.history.length >= 2 && pts.length >= 2) {
        fill(area, box);
        lineChart(canvas, pts, {label: 'Value'});
      } else {
        const one = d.history[d.history.length - 1];
        const why = d.days === 0 ? 'The first snapshot is saved after the next sync with holdings. Then a new point lands after every daily sync.'
          : d.history.length >= 2 ? `Only one snapshot falls in this range so far. Pick a longer range to see the line.`
            : `First snapshot saved ${fmtDate(d.tracking_since)}. A new point lands after every daily sync, so this line grows day by day.`;
        const msg = h('div', {class: 'emptyov'}, ibadge('sprout', RGB.ok), h('div', {}, h('b', {}, d.history.length >= 2 ? 'Not enough points yet' : 'History builds from today'),
          h('span', {}, why)));
        // one point: the chart with the message over it; none: just the message, centred in a short box
        fill(area, one ? h('div', {class: 'chwrap fillh onept'}, h('div', {class: 'plot'}, canvas), msg)
          : h('div', {class: 'chwrap nochart', dataset: {emptyChart: ''}}, msg));
        if (one) onePointChart(canvas, one.date, one.value);
      }
      seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.r === INV.range)));
    };
    RANGES.forEach(([k, label]) => seg.append(h('button', {type: 'button', 'aria-pressed': String(k === INV.range), dataset: {r: k},
      onclick: () => { INV.range = k; draw(); }}, label)));
    later.push(draw);
    const plaidAccts = d.accounts.filter((a) => !a.manual);
    const insts = [...new Set(plaidAccts.map((a) => instShort(a.institution)))];
    const updated = plaidAccts.map((a) => a.updated_at).filter(Boolean).sort().pop();
    const sub = [plural(d.accounts.length, 'account') + (insts.length === 1 ? ` at ${insts[0]}` : ''),
      d.prices_as_of ? `prices as of ${fmtDate(d.prices_as_of)}` : null, updated ? `updated ${ago(updated)}` : null].filter(Boolean).join(' · ');
    return card({cls: 's2 grow'}, h('div', {class: 'hdrow'}, h('h3', {}, h('span', {class: 'hi'}, glyph('linechart')),
      h('span', {class: 'ttl'}, 'Total investments')), seg),
    h('div', {class: 'herohead'}, bigMoney(d.total), chipSlot), h('div', {class: 'sub'}, d.accounts.length ? sub : 'No investment accounts yet'), area);
  }

  // A share of the total, one decimal, signed: "−0.7%" for a negative residual (TASK-22f: it was left blank, and the
  // gate read 0.0%), "0.0%" for less than a twentieth of a percent either way.
  function sharePct(ratio) {
    const v = Math.abs(ratio * 100).toFixed(1);
    return `${ratio < 0 && v !== '0.0' ? '\u2212' : ''}${v}%`;
  }

  function allocationCard(d, later) {
    const legend = h('div', {style: {'margin-top': '12px'}});
    const [box, canvas] = chartBox('ch-alloc', 'Allocation');
    const seg = h('div', {class: 'seg', role: 'group', 'aria-label': 'Group by', style: {'margin-bottom': '14px'}});
    const total = d.total;
    const draw = () => {
      destroyCharts(box);
      const list = INV.alloc === 'type'
        ? d.allocation.by_type.map((x) => ({...x, color: TYPE_COLORS[x.label] || '#9ba2b6', sub: x.count ? plural(x.count, 'account') : 'No accounts yet', none: !x.count}))
        : d.allocation.by_holding.map((x, i) => ({...x, color: holdColor(x, i), sub: x.name || ''}));
      const nz = list.filter((x) => x.value > 0);  // a negative residual ("-$0.07") is in the list, not the ring
      if (nz.length) doughnut(canvas, nz, total, 'total');
      box.hidden = !nz.length;
      fill(legend, list.length ? list.map((x) => h('div', {class: `lgr${x.value > 0 || x.residual ? '' : ' zero'}${x.residual ? ' rs' : ''}`},
        h('span', {class: 'sw', style: {background: x.color}}),
        h('span', {class: 'grow'}, h('b', {style: {'font-size': '14.5px'}}, x.label, x.manual ? manualTag() : null), h('span', {}, x.sub)),
        h('span', {class: 'rt'}, h('span', {class: 'amt v'}, x.none ? '—' : money2(x.value)),
          h('span', {}, x.none || !total ? '' : sharePct(x.value / total))))) : msgBox('Nothing to show', 'No holdings yet.'));
      seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.k === INV.alloc)));
    };
    [['holding', 'By holding'], ['type', 'By account type']].forEach(([k, label]) => seg.append(h('button', {type: 'button',
      'aria-pressed': String(k === INV.alloc), dataset: {k}, onclick: () => { INV.alloc = k; draw(); }}, label)));
    later.push(draw);
    return card({title: 'Allocation', icon: 'pie', rgb: RGB.purple}, seg, box, legend);
  }

  function breakdownCard(d) {
    const total = d.total;
    return card({title: 'Portfolio breakdown', icon: 'layers', rgb: RGB.cyan},
      h('div', {class: 'stk', role: 'img', 'aria-label': d.breakdown.map((b) => `${b.label} ${money2(b.value)}`).join(', ')},
        total > 0 ? d.breakdown.filter((b) => b.value > 0).map((b) => h('i', {style: {width: `${(b.value / total) * 100}%`, background: BK_COLORS[b.label]}})) : null),
      d.breakdown.map((b) => h('div', {class: `lgr${b.value > 0 ? '' : ' zero'}`}, h('span', {class: 'sw', style: {background: BK_COLORS[b.label]}}),
        h('span', {class: 'grow'}, h('b', {style: {'font-size': '14.5px'}}, b.label),
          h('span', {}, b.value > 0 ? `${Math.round((b.value / total) * 100)}% of your investments` : b.count ? plural(b.count, 'account') : 'Nothing here yet')),
        h('span', {class: 'amt v'}, money2(b.value)))));
  }

  function investAccountsCard(d) {
    const byGroup = {General: [], Retirement: [], Crypto: []};
    d.accounts.forEach((a) => byGroup[a.group].push(a));
    const row = (a) => HOOKS.investRow(a) || h(a.manual ? 'button' : 'div', {class: 'li wrap', type: a.manual ? 'button' : null, dataset: a.manual ? {edit: String(a.manual_id)} : null},
      a.manual ? manualChip(a.institution, 'l') || ibadge(KIND.investment.i, KIND.investment.c) : ia(a.institution, 'l', null, a.institution_id),
      h('span', {class: 'grow'}, h('b', {class: 'nmrow', title: acctName(a.name)}, h('span', {class: 'nm'}, acctName(a.name) || 'Account'),
        a.mask ? h('span', {class: 'dim mk'}, `••${a.mask}`) : null,
        a.manual ? manualTag() : null, a.manual ? staleTag({...a, balance_as_of: a.as_of}) : null),
      h('span', {}, [a.manual ? a.subtype_label : a.type, a.institution ? instShort(a.institution) : null].filter(Boolean).join(' · '))),
      h('span', {class: 'rt'}, h('span', {class: 'amt v'}, money2(a.value)),
        h('span', {}, a.manual ? `updated ${daysAgo(a.as_of)}` : a.updated_at ? ago(a.updated_at) : '')));
    const gh = (label) => h('div', {class: 'gh'}, label, h('span', {class: 'ln'}));
    return card({title: 'Accounts', icon: 'landmark', rgb: RGB.acc, extra: small(`${plural(d.accounts.length, 'account')}`)},
      gh('General'), byGroup.General.length ? byGroup.General.map(row)
        : ghostRow('plus', RGB.acc, 'No brokerage accounts', 'Link one with Plaid or add it by hand', null, {add: 'form:invest'}),
      gh('Retirement'), byGroup.Retirement.length ? byGroup.Retirement.map(row)
        : ghostRow('briefcase', RGB.purple, 'Have a 401(k) or IRA?', 'Add it by hand. It shows up here with a manual tag.', null, {add: 'form:invest'}),
      gh('Crypto'), byGroup.Crypto.length ? byGroup.Crypto.map(row)
        : ghostRow('bitcoin', RGB.warn, 'No crypto accounts', 'Add a wallet or exchange balance by hand', null, {add: 'form:invest'}));
  }

  function holdingsCard(d) {
    const total = d.priced_total || d.total;  // hotfix 2026-10-05: NVDA read "102.3% of portfolio" against the balance
    const stocks = d.holdings.filter((x) => !x.is_cash);
    const cash = d.holdings.filter((x) => x.is_cash);
    const cashT = sum(cash, (x) => x.value);
    const manual = d.accounts.filter((a) => a.manual);
    const gh = (label) => h('div', {class: 'gh'}, label, h('span', {class: 'ln'}));
    const pctOfTotal = (v) => (total ? `${((v / total) * 100).toFixed(1)}% of portfolio` : '');
    const letters = (x) => h('span', {class: 'tick', 'aria-hidden': 'true', style: {background: `hsl(${hue(x.ticker || x.name)} 42% 36%)`}},
      (x.ticker || x.name || '?').slice(0, 2));
    const tick = (x) => {  // the company or fund logo this server cached, else the letters
      if (!x.logo || !x.ticker) return letters(x);
      const img = h('img', {alt: '', decoding: 'async', src: `/api/securities/${encodeURIComponent(x.ticker.toUpperCase())}/logo`});
      const chip = h('span', {class: 'tick logo', 'aria-hidden': 'true'}, img);
      img.addEventListener('error', () => chip.replaceWith(letters(x)));
      return chip;
    };
    const stockRow = (x, i) => h('div', {class: 'li wrap'}, tick(x),
      h('span', {class: 'grow'}, h('b', {}, x.ticker || x.name, x.ticker && x.name ? h('span', {class: 'dim', style: {'font-weight': '500'}}, ` ${x.name}`) : null),
        h('span', {}, [x.quantity != null ? `${Number(x.quantity.toFixed(5))} shares` : null, x.price != null ? `${money2(x.price)} each` : null].filter(Boolean).join(' · ')),
        h('span', {class: 'track thin', style: {'margin-top': '7px', 'max-width': '220px'}},
          h('i', {style: {width: pct(total ? x.value / total : 0), background: HOLD_COLORS[i % HOLD_COLORS.length]}}))),
      h('span', {class: 'rt'}, h('span', {class: 'amt v'}, money2(x.value)), h('span', {}, pctOfTotal(x.value))));
    const body = [];
    if (stocks.length) body.push(gh('Stocks and funds'), stocks.map(stockRow));
    if (cash.length) {
      body.push(gh('Cash & equivalents'), h('div', {class: 'li'}, h('span', {class: 'tick', style: {background: '#2b3346', color: '#c9ceda'}}, glyph('dollar')),
        h('span', {class: 'grow'}, h('b', {}, 'Cash'), h('span', {}, cash.length === 1 ? '1 account' : `${cash.length} accounts`)),
        h('span', {class: 'rt'}, h('span', {class: 'amt v'}, money2(cashT)), h('span', {}, pctOfTotal(cashT)))),
      // hotfix 2026-09-29: each account on its own full-width line under the row, its amount right-aligned in the
      // same column as the total (was squeezed inside the name column, where amounts wrapped and floated mid-row)
      h('div', {class: 'cashsub'}, cash.map((x) => h('div', {class: 'cashl'},
        nameMask(x.account_name, x.mask), h('span', {class: 'v'}, money2(x.value))))));
    }
    if (manual.length) {
      body.push(gh('Added by you'), manual.map((a) => h('div', {class: 'li'}, manualChip(a.institution) || ibadge(KIND.investment.i, KIND.investment.c),
        h('span', {class: 'grow'}, h('b', {}, acctName(a.name), manualTag()), h('span', {}, 'Holdings not tracked · the balance you entered')),
        h('span', {class: 'rt'}, h('span', {class: 'amt v'}, money2(a.value)), h('span', {}, pctOfTotal(a.value))))));
    }
    const summaryText = [stocks.length ? plural(stocks.length, 'holding') : null, cash.length ? 'cash' : null].filter(Boolean).join(' · ');
    return card({title: 'Holdings', icon: 'coins', rgb: RGB.ok, extra: summaryText ? small(summaryText) : null},
      body.length ? body : msgBox('No holdings yet', 'They appear after a sync with an investment account.'));
  }

  // ---------- Loans & debt ---------------------------------------------------------------------
  const SORTS = {
    bal: ['Balance', (a, b) => a.bal - b.bal, 'Smallest first'],
    apr: ['Interest rate', (a, b) => (b.apr ?? -1) - (a.apr ?? -1), 'Highest rate first. Paying these down first saves the most interest.'],
    time: ['Time remaining', (a, b) => (a.months ?? 1e9) - (b.months ?? 1e9), 'Closest to done first. Cards have no end date, so they sit at the bottom.'],
    pay: ['Monthly payment', (a, b) => (b.monthly ?? 0) - (a.monthly ?? 0), 'Biggest monthly payment first. Cards show their minimum.'],
  };
  let dsort = 'bal';
  const LOAN_ICON = {auto: 'car', student: 'briefcase', mortgage: 'house', personal: 'handcoins', bnpl: 'bag', other: 'handcoins'};

  async function renderDebt(tk) {
    fill($('page'), skeleton(['card s2', 'stack', 'card s3']));
    const [d] = await Promise.all([api('/api/debt'), ensureAccounts().catch(() => null)]);  // accounts: the logos
    S.debt = d;
    if (tk !== S.token) return;
    if (!d.cards.length && !d.loans.length && !d.other.length) {
      fill($('page'), card({title: 'Nothing owed', icon: 'handcoins', rgb: RGB.warn},
        emptyNote('party', 'No cards or loans yet', 'Linked credit cards show up here. Add a loan by hand to track its payoff.'),
        h('div', {class: 'row', style: {display: 'flex', gap: '10px', 'margin-top': '16px'}},
          h('button', {class: 'btn', type: 'button', dataset: {add: 'form:loan'}}, 'Add a loan'))));
      return;
    }
    const totalCard = card({cls: 's2', title: 'Total owed', icon: 'handcoins', rgb: RGB.warn,
    },
    h('div', {class: 'herohead'}, bigMoney(d.total)),
    h('div', {class: 'sub', style: {'margin-bottom': '18px'}}, h('b', {}, money2(d.monthly.total)), ' a month in payments',
      d.monthly.loans ? [' · ', h('b', {}, money2(d.monthly.loans)), ' loans'] : null,
      d.monthly.card_minimums ? [' + ', h('b', {}, money2(d.monthly.card_minimums)), ' card minimums'] : null),
    h('div', {}, d.groups.map((g) => debtGroup(g, d))), debtStats(d));
    const primary = d.loans.filter((l) => l.manual && !l.paid_off).sort((a, b) => b.balance - a.balance)[0];
    const stack = h('div', {class: 'stack'}, primary ? payoffCard(primary) : card({title: 'Track a loan', icon: 'flag', rgb: RGB.acc},
      ghostRow('plus', RGB.warn, 'Add a loan', 'See its payoff date and matched payments', null, {add: 'form:loan'})), upcomingDebtCard(d));
    totalCard.dataset.pin = 'start';
    stack.dataset.pin = 'end';
    const list = allDebtsCard(d);
    const grid = h('div', {class: 'grid'}, totalCard, stack, list);
    fill($('page'), grid);
    pack(grid);
  }

  function debtGroup(g, d) {
    if (g.key === 'cards') {
      const u = (g.util || 0) / 100;
      const uc = u < 0.3 ? OK : u < 0.5 ? WARN : BAD;
      const cards = d.cards.slice().sort((a, b) => b.balance - a.balance);
      const wrap = h('div', {class: 'dg'});
      const hd = h('button', {class: 'hd', type: 'button', 'aria-expanded': 'false', onclick: () => {
        hd.setAttribute('aria-expanded', String(wrap.classList.toggle('open')));
      }}, ibadge('card', RGB.bad), h('b', {}, 'Credit cards', h('span', {class: 'cnt'}, ` · ${g.count}`)), h('span', {class: 'gtot v'}, money2(g.total)),
      h('span', {class: 'chev tg'}, icon('chevd')));
      return add(wrap, [hd,
        g.util != null ? h('div', {class: 'trackw'}, track(u, uc, `${Math.round(g.util)}% of your card limits used`, '',
          [h('s', {style: {left: '30%'}}), h('em', {style: {left: '30%'}}, '30%')])) : null,
        g.util != null ? h('div', {class: 'meta', style: {'margin-top': '22px'}}, h('span', {}, h('b', {class: u < 0.3 ? 'pos' : 'warn'}, `${Math.round(g.util)}% utilization`),
          ` of ${money(g.limit)} limit`), u < 0.3 ? pill('sm', 'checkc', 'Well under 30%') : pill('warn sm', 'alert', 'Over 30%')) : null,
        h('div', {class: 'xtra'}, cards.map((c) => h('div', {class: 'li', style: {padding: '8px 0'}},
          c.manual ? manualChip(c.institution, 's') || ibadge(KIND.credit_card.i, KIND.credit_card.c, 's') : ia(c.institution, 's', null, c.institution_id),
          h('span', {class: 'grow'}, h('b', {style: {'font-size': '14.5px'}}, c.name, c.manual ? manualTag() : null)),
          h('span', {class: 'mut', style: {'font-size': '13px'}}, c.limit ? `${Math.round(c.util)}% of ${money(c.limit)}` : ''),
          h('span', {class: 'amt v', style: {'min-width': '80px', 'text-align': 'right'}, title: c.approx ? 'Approximate: purchases since the balance you set are not seen' : null},
            `${c.approx ? '≈ ' : ''}${money2(c.balance)}`))))]);
    }
    if (g.key === 'other') {
      return h('div', {class: 'dg'}, h('div', {class: 'hd'}, ibadge('fileminus', RGB.bad), h('b', {}, 'Other debts', h('span', {class: 'cnt'}, ` · ${g.count}`)),
        h('span', {class: 'gtot v'}, money2(g.total))),
      h('div', {class: 'meta'}, d.other.map((o) => h('span', {}, `${o.name}: `, h('b', {class: 'v'}, money2(o.balance))))));
    }
    const loans = d.loans.filter((l) => g.loans.includes(l.id));
    const one = loans.length === 1 ? loans[0] : null;
    const manualOnly = loans.every((l) => l.manual);
    const key = g.key.slice(5);
    const hdKids = [ibadge(g.all_paid_off ? 'party' : LOAN_ICON[key] || 'handcoins', g.all_paid_off ? RGB.acc : RGB.warn),
      h('b', {}, g.label, h('span', {class: 'cnt'}, ` · ${g.count}`), manualOnly ? manualTag() : null), h('span', {class: 'gtot v'}, money2(g.total)),
      one && one.manual ? h('span', {class: 'chev'}, icon('chevr')) : null];
    const hd = one && one.manual ? h('button', {class: 'hd', type: 'button', dataset: {loan: String(one.id)},
      'aria-label': `${one.name}: open loan details`}, hdKids) : h('div', {class: 'hd'}, hdKids);
    const paid = g.paid_pct != null ? g.paid_pct / 100 : null;
    const kids = [hd];
    if (g.all_paid_off) {
      kids.push(h('div', {class: 'trackw'}, track(1, null, '100% paid off', 'win')),
        h('div', {class: 'win-t'}, icon('party'), `Congratulations! 100% of ${money(g.original || 0)} paid off`));
      return h('div', {class: 'dg'}, kids);
    }
    if (paid != null) kids.push(h('div', {class: 'trackw'}, h('div', {class: 'track', style: {height: '12px'}},
      h('i', {style: {width: pct(paid), background: 'linear-gradient(90deg,#6f8cff,#8ea8ff)'}}))));
    if (one) {
      const po = one.payoff;
      kids.push(h('div', {class: 'meta'},
        paid != null ? h('span', {}, h('b', {class: 'accc'}, `${Math.round(g.paid_pct)}% paid off`), ` of ${money(g.original)}`) : null,
        po.payoff_month ? h('span', {}, icon('calendar'), 'Payoff ', h('b', {}, `~${monthYear(po.payoff_month)}`)) : null,
        one.monthly_payment ? h('span', {}, h('b', {class: 'v'}, money2(one.monthly_payment)), ' a month') : null,
        one.apr != null ? h('span', {}, 'APR ', h('b', {}, `${one.apr}%`)) : h('span', {}, 'APR ', tag('not set', '', 'APR not set, whole payment counted as principal'))));
      if (one.last_payment) {
        kids.push(h('div', {class: 'matched'}, h('span', {class: 'ck paid'}, glyph('check', 2.6)),
          h('span', {}, 'Last payment matched: ', h('b', {}, `${fmtDate(one.last_payment.date)} · ${money2(one.last_payment.amount)}`))));
      }
    } else {
      kids.push(h('div', {class: 'meta'}, paid != null ? h('span', {}, h('b', {class: 'accc'}, `${Math.round(g.paid_pct)}% paid off`), ` of ${money(g.original)}`) : null,
        loans.map((l) => l.manual ? h('button', {class: 'lnk', type: 'button', dataset: {loan: String(l.id)}}, l.name) : h('span', {}, l.name))));
    }
    return h('div', {class: 'dg'}, kids);
  }

  function debtStats(d) {
    const s = d.stats;
    const box = (ic, rgb, value, text, cls = '') => h('div', {}, ibadge(ic, rgb, 's'), h('p', {}, h('b', {class: `v ${cls}`}, value), h('span', {}, text)));
    const year = d.today.slice(0, 4);
    const out = [];
    if (s.paid_so_far) out.push(box('checkc', RGB.ok, money(s.paid_so_far), 'paid on your loans so far', 'pos'));
    if (s.matched_this_year) out.push(box('calendar', RGB.acc, money2(s.matched_this_year_total), `${plural(s.matched_this_year, 'loan payment')} matched in ${year}`));
    if (s.paid_off_count) out.push(box('party', RGB.warn, plural(s.paid_off_count, 'loan'), `fully paid off · ${money(s.paid_off_total)}`));
    return out.length ? h('div', {class: 'stats'}, out) : null;
  }

  function payoffCard(l) {
    const po = l.payoff;
    const paid = l.paid_pct != null ? l.paid_pct / 100 : 0;
    // A missing APR is something to act on, so it stays in the card. How a set rate is applied is explanation: it moves
    // to the header, next to the title, so it never floats beside the body copy.
    const aprHelp = l.apr != null ? help('How the payoff is counted', `Month by month at ${l.apr}% APR, so interest on what you still owe is added `
      + 'before each payment comes off it. Change the rate on the loan if your lender charges a different one.',
    {key: 'payoff-apr'}) : null;
    return card({title: `${l.name} payoff`, icon: 'flag', rgb: RGB.acc, help: aprHelp},
      h('div', {class: 'bigring'}, ring(paid, ACC, 116, 11, h('b', {class: 'v', style: {'font-size': '26px', 'letter-spacing': '-.02em'}}, `${Math.round(paid * 100)}%`),
        h('small', {}, 'paid off')),
      h('div', {class: 'rstats'},
        h('div', {}, h('b', {class: 'accc'}, po.payoff_month ? `~${monthYear(po.payoff_month)}` : po.method === 'never' ? 'Not with this payment' : '—'),
          h('span', {}, 'projected payoff')),
        h('div', {}, h('b', {class: 'v'}, po.payments_left != null ? `~${po.payments_left}` : '—'), h('span', {}, 'payments left')),
        h('div', {}, h('b', {class: 'v'}, money2(l.balance)), h('span', {}, 'left to pay')))),
      l.apr != null ? h('div', {class: 'sub'}, h('span', {class: 'mut'}, `Counted at ${l.apr}% APR`))
        : h('div', {class: 'note'}, icon('info'), h('button', {class: 'lnk', type: 'button', title: 'Until then the whole payment counts as principal',
          onclick: () => openAdd(`edit:${l.id}`)}, 'Add APR for an exact date')),
      h('button', {class: 'btn ghost', type: 'button', style: {width: '100%', 'margin-top': '16px'}, dataset: {loan: String(l.id)}}, 'Open loan details'));
  }

  function upcomingDebtCard(d) {
    const rows = d.upcoming.slice(0, 6).map((u) => {
      const k = daysFrom(u.date, d.today);
      return h('div', {class: 'li'}, cal(u.date, k <= 1 ? 'hot' : ''),
        h('span', {class: 'grow'}, h('b', {}, u.name), h('span', {}, k === 0 ? h('b', {class: 'warn'}, 'Due today · ') : null,
          u.kind === 'card' ? 'Minimum payment' : `${u.type_label || 'Loan'} · expected`)),
        h('span', {class: 'amt v'}, money2(u.amount)));
    });
    return card({title: 'Payments coming up', icon: 'calendar', rgb: RGB.warn},
      rows.length ? rows : msgBox('Nothing due soon', 'Card due dates and loan payments show up here.'));
  }

  function allDebtsCard(d) {
    const items = [
      ...d.cards.map((c) => ({kind: 'card', id: c.id, manual: c.manual, name: c.name, inst: c.institution, instId: c.institution_id, mask: c.mask, bal: c.balance, apr: c.apr,
        monthly: c.min_payment, months: null, approx: c.approx,
        sub: `Card${c.mask ? ` ••${c.mask}` : ''}${c.limit ? ` · ${Math.round(c.util)}% of limit` : ''}${c.approx ? ` · last set ${fmtDate(c.balance_as_of)}` : ''}`})),
      ...d.loans.map((l) => ({kind: 'loan', id: l.id, manual: l.manual, name: l.name, inst: l.institution, instId: l.institution_id, sub_kind: l.subtype, bal: l.balance, apr: l.apr,
        monthly: l.paid_off ? null : l.monthly_payment, months: l.paid_off ? 0 : l.payoff.payments_left, paid: l.paid_off,
        sub: l.paid_off ? 'Paid off' : `${l.type_label}${l.paid_pct != null ? ` · ${Math.round(l.paid_pct)}% paid off` : ''}`})),
      ...d.other.map((o) => ({kind: 'other', id: o.id, manual: true, name: o.name, inst: null, bal: o.balance, apr: null, monthly: null,
        months: null, sub: o.subtype || 'Other debt'})),
    ];
    const tabs = h('div', {class: 'sorttabs', role: 'group', 'aria-label': 'Sort by'});
    const tip = h('div', {class: 'tip'});
    const list = h('div');
    const draw = () => {
      const [, cmp, text] = SORTS[dsort];
      const live = items.filter((x) => x.bal > 0).sort(cmp);
      const zero = items.filter((x) => x.bal <= 0);
      fill(tip, icon('bulb'), h('span', {}, text));
      const on = (k) => (k === dsort ? 'on' : '');
      const apr = (x) => (x.apr == null ? (x.kind === 'loan' && x.manual && !x.paid ? tag('not set', '', 'APR not set') : '—') : `${x.apr}%`);
      const tl = (x) => (x.months == null ? '—' : x.months === 0 ? 'Done' : `~${x.months} mo`);
      const row = (x) => {
        const clickable = x.manual;
        const data = x.kind === 'loan' ? {loan: String(x.id)} : {edit: String(x.id)};
        return h(clickable ? 'button' : 'div', {class: 'dl', type: clickable ? 'button' : null, dataset: clickable ? data : null},
          h('span', {class: 'nmc'}, x.paid ? ibadge('party', RGB.acc, 's') : !x.manual && x.inst ? ia(x.inst, '', null, x.instId)
            : manualChip(x.inst) || ibadge(x.kind === 'card' ? KIND.credit_card.i : x.kind === 'loan' ? LOAN_ICON[x.sub_kind] || 'handcoins' : 'fileminus',
              x.kind === 'loan' ? RGB.warn : RGB.bad, 's'),
            h('span', {class: 'grow'}, h('b', {}, acctName(x.name), x.manual ? manualTag() : null), h('span', {}, x.sub))),
          h('span', {class: `c ${on('apr')}`, 'data-l': 'APR'}, apr(x)),
          h('span', {class: `c v ${on('pay')}`, 'data-l': 'Monthly'}, x.monthly ? money2(x.monthly) : '—'),
          h('span', {class: `c ${on('time')}`, 'data-l': 'Time left'}, tl(x)),
          h('span', {class: `bal v${x.paid ? ' pos' : ''}`}, `${x.approx ? '≈ ' : ''}${money2(x.bal)}`));
      };
      fill(list, h('div', {class: 'dl hdr'}, h('span', {}, 'Account'), h('span', {class: `c ${on('apr')}`}, 'Interest'),
        h('span', {class: `c ${on('pay')}`}, 'Monthly'), h('span', {class: `c ${on('time')}`}, 'Time left'),
        h('span', {class: `c ${on('bal')}`, style: {'text-align': 'right'}}, 'Balance')),
      live.map(row), zero.length ? [h('div', {class: 'zero-h'}, `Paid off or $0 · ${zero.length}`), zero.map(row)] : null);
      tabs.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.s === dsort)));
    };
    Object.entries(SORTS).forEach(([k, [label]]) => tabs.append(h('button', {type: 'button', 'aria-pressed': String(k === dsort), dataset: {s: k},
      onclick: () => { dsort = k; draw(); }}, label)));
    draw();
    return card({cls: 's3', title: 'All debts', icon: 'sort', rgb: RGB.grey, extra: small(plural(items.length, 'account'))}, tabs, tip, list);
  }

  const RENDER = {overview: renderOverview, transactions: renderTransactions, budgets: renderBudgets,
    recurring: renderRecurring, accounts: renderAccounts, investments: renderInvestments, debt: renderDebt, asset: renderAsset};
  const BOARDS = new Set();  // pages drawn without the sidebar, top bar and tab bar (first-run setup)

  // ---------- boot -----------------------------------------------------------------------------
  function boot() {
    $('menu').addEventListener('click', () => (document.body.classList.contains('drawer') ? closeDrawer() : openDrawer()));
    $('back').addEventListener('click', (e) => {  // back where you came from in the app; straight in: to Accounts
      if (S.cameFrom) {
        e.preventDefault();
        history.back();
      }
    });
    $('scrim').addEventListener('click', closeDrawer);
    $('dscrim').addEventListener('click', closePanel);
    $('mprev').addEventListener('click', () => setMonth(addMonths(S.month, -1)));
    $('mnext').addEventListener('click', () => setMonth(addMonths(S.month, 1)));
    $('modal').addEventListener('click', (e) => { if (e.target === $('modal')) closeModal(); });
    $('msg').addEventListener('click', () => { $('msg').className = ''; });
    document.addEventListener('click', (e) => {
      if (e.target.closest('#side a')) closeDrawer();
      const addBtn = e.target.closest('[data-add]');
      if (addBtn && !addBtn.disabled) {
        closeDrawer();
        openAdd(addBtn.dataset.add || 'home');
        return;
      }
      const loan = e.target.closest('[data-loan]');
      if (loan) {
        openLoan(Number(loan.dataset.loan)).catch((err) => toast(`Could not open the loan: ${err.message}`, [], {error: true, timeout: 5000}));
        return;
      }
      const edit = e.target.closest('[data-edit]');
      if (edit) {
        openAdd(`edit:${edit.dataset.edit}`);
        return;
      }
      const rc = e.target.closest('button[data-item]');
      if (rc) {
        e.preventDefault();
        closeDrawer();
        reconnect(rc.dataset.item);
      }
    });
    document.addEventListener('keydown', (e) => {
      if ($('modal').classList.contains('on')) trapFocus(e, $('sheet'));
      else if (!$('panel').hidden) trapFocus(e, $('panel'));
      if (e.key !== 'Escape') return;
      if ($('modal').classList.contains('on')) closeModal();
      else if (!$('panel').hidden) closePanel();
      else if (document.body.classList.contains('drawer')) closeDrawer();
      else if (!$('syncp').hidden) closeSyncPanel();
      else hideToast();
    });
    window.addEventListener('hashchange', () => route());
    $('syncnow').addEventListener('click', () => syncNow());   // TASK-22: Sync now
    // TASK-22f: the sync panel closes on a tap outside it (on a phone, the dimmed page) and follows the button on resize
    document.addEventListener('click', (e) => {
      const p = $('syncp');
      if (!p.hidden && !p.contains(e.target) && !$('syncnow').contains(e.target) && !e.target.closest('#modal')) closeSyncPanel();
    });
    window.addEventListener('resize', placeSyncPanel);
    // the pointer on it, or a Tab into it, keeps a "Nothing new" (its own focus() when it opens doesn't)
    $('syncp').addEventListener('pointerenter', () => clearTimeout(SY.timer));
    $('syncp').addEventListener('focusin', (e) => { if (e.target !== $('syncp')) clearTimeout(SY.timer); });
    // the theme changed (Settings > Appearance, or the device while it's System): charts redraw in its ink
    document.addEventListener('hf-theme', () => { if (S.page) route({keepToast: true}); });
    // first run (no keys, nothing connected or added): opening the app shows the set-up (a page asked for by name
    // still opens)
    if (document.body.hasAttribute('data-setup') && has(RENDER, 'setup') && ['', '#', '#/', '#/overview'].includes(location.hash)) {
      history.replaceState(null, '', '#/setup');
    }
    loadAccounts().catch((e) => {
      fill($('banks'), h('p', {class: 'nc warn'}, `Could not load accounts: ${e.message}`));
    });
    loadCounts();
    if (!location.hash) history.replaceState(null, '', '#/overview');
    // Charts draw text on a canvas once, so wait (briefly) for Inter before the first page.
    const fonts = document.fonts ? Promise.race([Promise.all(['400 15px Inter', '600 15px Inter', '700 46px Inter']
      .map((f) => document.fonts.load(f))), new Promise((r) => setTimeout(r, 1500))]).catch(() => {}) : Promise.resolve();
    fonts.then(() => route());
  }

  // ---------- extensions (TASK-18, TASK-19) --------------------------------------------------------------------
  // Settings, first-run setup and import (settings.js, setup.js, import.js) and the TASK-19 features (goals.js,
  // txmeta.js, billcal.js, rollover.js, alpaca.js) live in their own files, loaded before this one. Each pushes a function
  // onto window.HF_EXT; it's called once here with the building blocks below and fills in its pages (PAGES / RENDER),
  // HOOKS and board pages (BOARDS) before the first page is drawn. One that throws is logged and skipped.
  const HF = {$, h, add, fill, sv, icon, glyph, has, plural, clamp, sum, money, money2, signed, splitMoney, bigMoney, kf, pct,
    billAmt, usualText, billTag, CHART, catChart, chartBox, destroyCharts,
    pctOf, pad, asDate, monthName, monthShort, monthLabel, monthYear, todayISO, thisMonth, addMonths, fmtDate, daysFrom, relDay,
    daysAgo, ago, syncLabel, hue, CAT_GROUPS, PALETTE, CATI, CATG, catLabel, catColor, rgbOf, RGB, ACC, OK, WARN, BAD, SC, CUR, LOC, CINK, readInk,
    capStatus, avatar, ia, instShort, acctName, nameMask, ibadge, badge, gbadge, GROUP_ICON, ringOf, ring, ringPctOf, budgetRing, hairline, cal, pill, tag, track,
    api, post, show, PAGES, S, RENDER, BOARDS, HOOKS, TX, KIND, openSheet, closeModal, closeBtn, backBtn, modal, trapFocus,
    help, helpBills, toast, hideToast, card, small, msgBox, emptyNote, ghostRow, skel, skeleton, errorCard, pack, lnk, hashFor, parseHash,
    route, refresh, syncNow, ensureAccounts, ensureCategories, loadAccounts, loadCounts, parseAmount, reloadBudgets, updateTx, loadTx,
    txParams, txWindowParams, txFiltered, slotInfo, opt, openAdd, connectBank, notCounted, leftOut, worthOf, customMode, applyCap, capEditor,
    field: (...a) => field(...a), checkField: (...a) => checkField(...a)};
  for (const ext of window.HF_EXT || []) {
    try {
      ext(HF);
    } catch (e) {
      console.error('extension failed', e);  // eslint-disable-line no-console
    }
  }

  boot();
})();
