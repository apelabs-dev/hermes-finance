/* Hermes Finances, static demo: Ask v2 (t_d31fda2a) on top of the app's own Ask panel (static/chat.js, unchanged).
 *
 * chat.js draws every answer (its text, budget rings and source lines). This file adds, from what demo/shim.js matched:
 * - the answer's blocks under its text: tiles, bars, a line, columns, cards and goal progress, built with the app's own
 *   building blocks (createElement only, never HTML),
 * - a token-ish reveal of the newest answer (words appear, then its blocks fade in; under reduced motion only the fade),
 * - the chips: three starters, then each answer's three follow-ups (an unmatched question gets three suggestions),
 * - the "Try live AI" switch (kept in localStorage); the shim does the asking and the quiet fallback.
 * chat.js redraws the whole conversation now and then, so everything here is drawn again on each redraw.
 */
(window.HF_EXT = window.HF_EXT || []).push((HF) => {
  'use strict';
  const D = window.HFDemo && window.HFDemo.ask;
  const panel = document.getElementById('chat');
  if (!D || !panel || !HF.chat) return;
  const {h, fill, money2, avatar, badge, ibadge, rgbOf, monthShort, fmtDate, relDay, todayISO} = HF;
  const msgs = panel.querySelector('.chmsgs'), chips = panel.querySelector('.chchips'), foot = panel.querySelector('.chfoot');
  const REDUCED = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const WORD_MS = 24, FRESH_MS = 4000;
  let ask = null;
  D.data().then((a) => { ask = a; drawChips(); }).catch(() => { /* chips stay as chat.js drew them */ });

  // ---- blocks ---------------------------------------------------------------------------------------------------------
  const signedMoney = (v) => (v > 0 ? '+' : v < 0 ? '−' : '') + money2(Math.abs(v));
  function value(t) {
    if (t.fmt === 'money') return t.signed ? signedMoney(t.value) : money2(t.value);
    if (t.fmt === 'pct') return `${t.value > 0 ? '+' : ''}${Math.abs(t.value) < 10 ? t.value.toFixed(1) : Math.round(t.value)}%`;
    if (t.fmt === 'count') return String(t.value);
    return String(t.value);
  }
  const title = (text) => (text ? h('p', {class: 'hfd-h'}, text) : null);
  function tiles(b) {
    return h('div', {class: 'hfd-tiles'}, b.items.map((t) => h('div', {class: 'hfd-tile'},
      h('span', {}, t.label), h('b', {class: t.tone || ''}, value(t)))));
  }
  function mark(it) {
    if (it.merchant) return avatar(it.merchant, it.logo_url || null, null, 24);
    if (it.category) return badge(it.category, 'xs');
    return null;
  }
  function bars(b) {
    const max = Math.max(...b.items.map((i) => i.value), 0) || 1;
    return h('div', {class: 'hfd-bars'}, title(b.title), b.items.map((i) => h('div', {class: 'hfd-bar'},
      mark(i),
      h('div', {class: 'grow'},
        h('div', {class: 'hfd-row'}, h('span', {class: 'n'}, i.label),
          h('b', {}, (i.negative ? '−' : '') + money2(i.value))),
        h('div', {class: 'hfd-track'}, h('i', {class: i.negative ? 'neg' : '', style: {width: `${Math.max(2, (i.value / max) * 100)}%`}})),
        i.note ? h('span', {class: 'hfd-note'}, i.note) : null))));
  }
  const SVG = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    return el;
  }
  function line(b) {
    const pts = b.points, W = 300, H = 72, pad = 4;
    const vals = pts.map((p) => p.value), lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
    const xy = pts.map((p, i) => [pad + (i / Math.max(1, pts.length - 1)) * (W - 2 * pad), pad + (1 - (p.value - lo) / span) * (H - 2 * pad)]);
    const chart = svg('svg', {viewBox: `0 0 ${W} ${H}`, class: 'hfd-line', role: 'img', preserveAspectRatio: 'none',
      'aria-label': `${b.title}: ${money2(pts[0].value)} to ${money2(pts[pts.length - 1].value)}`});
    const path = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    chart.append(svg('path', {d: `${path} L${xy[xy.length - 1][0].toFixed(1)},${H} L${xy[0][0].toFixed(1)},${H} Z`, class: 'area'}),
      svg('path', {d: path, class: 'stroke'}));
    const first = pts[0], last = pts[pts.length - 1];
    return h('div', {class: 'hfd-chart'}, title(b.title), chart,
      h('div', {class: 'hfd-row hfd-note'}, h('span', {}, `${fmtDate(first.date, true)} · ${money2(first.value)}`),
        h('span', {}, `${fmtDate(last.date, true)} · ${money2(last.value)}`)));
  }
  function columns(b) {
    const keys = b.series.map((s) => s.key);
    const max = Math.max(...b.items.flatMap((i) => keys.map((k) => i[k] || 0)), 0) || 1;
    return h('div', {class: 'hfd-chart'}, title(b.title),
      b.series.length > 1 ? h('div', {class: 'hfd-legend'}, b.series.map((s, k) => h('span', {class: `k${k}`}, s.label))) : null,
      h('div', {class: 'hfd-cols', role: 'img', 'aria-label': b.items.map((i) => `${monthShort(i.month)}: ${keys.map((k) => money2(i[k] || 0)).join(' / ')}`).join(', ')},
        b.items.map((i) => h('div', {class: 'hfd-col'},
          h('div', {class: 'bars'}, keys.map((k, n) => h('i', {class: `k${n}`, title: money2(i[k] || 0),
            style: {height: `${Math.max(2, ((i[k] || 0) / max) * 100)}%`}}))),
          h('span', {}, monthShort(i.month))))));
  }
  function cards(b) {
    return h('div', {class: 'hfd-cards'}, title(b.title), b.items.map((c) => h('div', {class: 'hfd-card'},
      avatar(c.merchant || c.title, c.logo_url || null, null, 24),
      h('div', {class: 'grow'}, h('b', {}, c.title),
        h('span', {class: 'hfd-note'}, [c.badge, c.note].filter(Boolean).join(' · '))),
      h('div', {class: 'end'}, h('b', {}, money2(c.amount)),
        h('span', {class: 'hfd-note'}, typeof c.days === 'number' ? relDay(c.date, todayISO()) : fmtDate(c.date))))));
  }
  function progress(b) {
    return h('div', {class: 'hfd-goals'}, b.items.map((g) => h('div', {class: 'hfd-goal'},
      ibadge(g.icon, rgbOf(g.colour), 'xs'),
      h('div', {class: 'grow'},
        h('div', {class: 'hfd-row'}, h('b', {}, g.name), h('span', {}, `${Math.round(g.pct)}%`)),
        h('div', {class: 'hfd-track'}, h('i', {style: {width: `${Math.min(100, Math.max(2, g.pct))}%`, background: g.colour}})),
        h('span', {class: 'hfd-note'}, `${money2(g.progress)} of ${money2(g.goal)}` + (g.note ? ` · ${g.note}` : ''))))));
  }
  const KINDS = {tiles, bars, line, columns, cards, progress};
  const blocks = (x) => h('div', {class: 'hfd-blocks'}, (x.blocks || []).map((b) => (KINDS[b.type] ? KINDS[b.type](b) : null)));

  // ---- the answers on screen ------------------------------------------------------------------------------------------
  let reveal = null;   // {content, start, total}: the newest answer while its words appear
  let frame = 0;
  const kick = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; if (reveal) step(); }); };
  const done = new Set();
  function bubbles() {
    const answers = HF.chat.log.filter((m) => m.role === 'assistant');
    const els = [...msgs.children].filter((e) => e.matches('.msg.bot') && !e.matches('.busy, .err, .card-msg'));
    return answers.length && els.length === answers.length ? els.map((el, i) => [el, answers[i]]) : [];
  }
  function decorate() {
    const list = bubbles();
    list.forEach(([el, m], i) => {
      if (el.dataset.hfd) return;
      el.dataset.hfd = '1';
      const x = D.answerFor(m.content);
      if (!x) return;
      const src = el.querySelector(':scope > .src');
      if (x.blocks && x.blocks.length) el.insertBefore(blocks(x), src);
      if (x.live) el.classList.add('hfd-live-answer');
      const newest = i === list.length - 1;
      const asked = `${x.at} ${m.content}`;   // (the same answer asked again streams again)
      if (newest && !done.has(asked) && D.clock() - x.at < FRESH_MS) {
        done.add(asked);
        reveal = {content: m.content, start: performance.now(), total: m.content.split(/\s+/).length};
      }
      if (reveal && reveal.content === m.content) step();
    });
  }
  function current() {
    const hit = bubbles().reverse().find(([, m]) => reveal && m.content === reveal.content);
    return hit ? hit[0] : null;
  }
  function step() {
    const el = current();
    if (!el) { reveal = null; msgs.removeAttribute('aria-busy'); return; }
    if (REDUCED) {   // the fade only (rule 5)
      el.classList.add('hfd-fade');
      reveal = null;
      return;
    }
    const shown = Math.floor((performance.now() - reveal.start) / WORD_MS);
    const paras = [...el.querySelectorAll(':scope > p:not(.src)')];
    if (!el.dataset.full) el.dataset.full = JSON.stringify(paras.map((p) => p.textContent));
    const full = JSON.parse(el.dataset.full);
    let left = shown;
    paras.forEach((p, k) => {
      const parts = full[k].split(/(\s+)/);
      const n = Math.max(0, Math.min(parts.length, left * 2));
      left -= Math.ceil(parts.length / 2);
      p.textContent = parts.slice(0, n).join('');
      p.classList.toggle('hfd-wait', n === 0);
    });
    if (shown >= reveal.total) {
      el.classList.remove('hfd-typing');
      el.classList.add('hfd-shown');
      msgs.removeAttribute('aria-busy');
      reveal = null;
      return;
    }
    el.classList.add('hfd-typing');
    msgs.setAttribute('aria-busy', 'true');
    msgs.scrollTop = msgs.scrollHeight;
    kick();
  }

  // ---- chips --------------------------------------------------------------------------------------------------------------
  function chipIds() {
    const last = HF.chat.log[HF.chat.log.length - 1];
    const x = last && last.role === 'assistant' ? D.answerFor(last.content) : null;
    return (x && (x.next || x.suggestions)) || ask.starters;
  }
  function drawChips() {
    if (!ask || !chips) return;
    const questions = chipIds().map((id) => ask.presets[id] && ask.presets[id].question).filter(Boolean);
    const busy = Boolean(document.getElementById('chsend') && document.getElementById('chsend').disabled);
    const key = JSON.stringify([questions, busy]);
    if (chips.dataset.hfd === key && chips.querySelector('[data-hfd-chip]')) return;
    chips.dataset.hfd = key;
    fill(chips, questions.map((q) => h('button', {class: 'fchip', type: 'button', disabled: busy, dataset: {hfdChip: '1'},
      onclick: () => HF.chat.ask(q)}, q)));
  }

  // ---- live AI -----------------------------------------------------------------------------------------------------------
  const sw = h('button', {class: 'hfd-switch', type: 'button', role: 'switch', id: 'hfd-live'});
  const liveRow = h('div', {class: 'hfd-liverow'}, sw);
  function drawSwitch() {
    const on = D.live.get();
    sw.setAttribute('aria-checked', String(on));
    fill(sw, h('span', {class: 'knob', 'aria-hidden': 'true'}),
      h('span', {}, on ? 'Live AI on · a free model, may be busy' : 'Try live AI (a free model)'));
  }
  sw.addEventListener('click', () => { D.live.set(!D.live.get()); drawSwitch(); });
  drawSwitch();
  if (foot) foot.insertBefore(liveRow, foot.firstChild);

  new MutationObserver(() => { decorate(); drawChips(); }).observe(msgs, {childList: true});
  new MutationObserver(() => drawChips()).observe(chips, {childList: true});
  decorate();
});
