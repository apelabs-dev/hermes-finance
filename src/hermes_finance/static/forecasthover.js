/* Forecast chart: the day under the pointer (mock board forecast-chart-hover-details-3b3aa66104aeed8b, round 2, option A:
 * the floating card).
 *
 * Hovering the line or any dot, tapping it, or stepping through the days with the keyboard (focus the chart, then ← / →,
 * Home / End; Escape closes) opens one card for that day: the date, the expected balance, the lower and higher
 * everyday-spending balances, each paycheck or bill that day and, on the day of the lowest expected balance (the yellow
 * dot), "Expected balance on <date> · <amount>" as a yellow line. That line is never written on the chart itself. A
 * crosshair and a ring mark the day. The card only fades (a fade is all reduced motion keeps, too).
 *
 * reports.js draws the chart and hands it here through HOOKS.forecastHover(svg, geometry); this returns what goes in
 * the chart's place: the svg and its card in one box. Every name and number is a text node built with h()/sv(); nothing
 * here writes HTML. The numbers are the forecast answer's own (GET /api/reports/forecast), so the frozen demo, which
 * replays that answer, shows the same card. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, sv, money, money2, fmtDate, asDate, LOC, todayISO, HOOKS} = X;
  const GAP = 14;       // px between the point and the card's side
  const ABOVE = 12;     // px between the point and the card's bottom edge when the card sits above it
  const BELOW = 16;
  let open = null;      // the chart whose card is open: a tap anywhere else closes it

  document.addEventListener('pointerdown', (e) => {
    if (open && !open.contains(e.target)) open.hide();
  }, true);

  const weekday = (iso) => asDate(iso).toLocaleDateString(LOC, {weekday: 'short'});

  HOOKS.forecastHover = (svg, g) => {
    const d = g.d, lines = d.lines, exp = lines.expected.points, n = exp.length;
    const today = d.today || todayISO();
    const byDay = new Map();   // a day's paychecks and bills: the ones drawn as dots
    for (const e of g.events) {
      const i = g.dayIndex(e.date);
      byDay.set(i, [...(byDay.get(i) || []), e]);
    }
    const first = svg.querySelector('.rp-ev');   // the crosshair and the ring go under the dots: a dot keeps its colour
    const cross = sv('line', {class: 'rp-hx', x1: 0, x2: 0, y1: g.top, y2: g.base});
    const ring = sv('circle', {class: 'rp-hring', cx: 0, cy: 0, r: 5});
    svg.insertBefore(cross, first);
    svg.insertBefore(ring, first);
    svg.setAttribute('tabindex', '0');
    svg.setAttribute('aria-label', `${svg.getAttribute('aria-label')}. Focus the chart and use the left and right arrow keys to read each day.`);
    const tip = h('div', {class: 'rp-htip', role: 'status'});
    const box = h('div', {class: 'rp-fwrap'}, svg, tip);
    let cur = -1;

    function card(i) {
      const p = exp[i];
      const kids = [h('div', {class: 'rp-hd'}, `${weekday(p.date)}, ${fmtDate(p.date)}${p.date === today ? ' · today' : ''}`)];
      for (const e of byDay.get(i) || []) {
        kids.push(h('div', {class: `rp-hev ${e.amount > 0 ? 'in' : 'out'}`}, h('i'),
          `${e.label} · ${e.amount > 0 ? '+' : '−'}${money2(Math.abs(e.amount))}`));
      }
      if (i === g.lowest) {
        kids.push(h('div', {class: 'rp-hev low'}, h('i'), `Expected balance on ${fmtDate(d.lowest.date)} · ${money(d.lowest.balance)}`));
      }
      kids.push(h('div', {class: 'rp-hbig v'}, money(p.balance)), h('div', {class: 'rp-hsub'}, 'Expected balance'));
      for (const k of ['lower', 'higher']) {
        kids.push(h('div', {class: `rp-hrow ${k}`}, h('span', {}, h('i'), lines[k].label), h('b', {class: 'v'}, money(lines[k].points[i].balance))));
      }
      return kids;
    }

    // Beside the point, on its right unless that runs off the box, then on its left; above it unless that runs off the
    // top, then under it. Never over the point, and never wider than the box (so nothing scrolls sideways).
    function place(vx, vy) {
      const r = svg.getBoundingClientRect(), b = box.getBoundingClientRect();
      const k = r.width / g.W;   // viewBox units to px (height: auto keeps the ratio)
      const px = r.left - b.left + vx * k, py = r.top - b.top + vy * k;
      tip.style.left = '0px';    // measured with the whole box to grow into, not squeezed at its last place
      const tw = tip.offsetWidth, th = tip.offsetHeight, bw = box.clientWidth;
      let left = px + GAP;
      if (left + tw > bw) left = px - GAP - tw;
      left = Math.max(0, Math.min(bw - tw, left));
      const top = py - th - ABOVE >= 0 ? py - th - ABOVE : py + BELOW;
      tip.style.left = `${Math.round(left)}px`;
      tip.style.top = `${Math.round(top)}px`;
    }

    function show(i) {
      cur = i;
      const vx = g.x(i).toFixed(1), vy = g.y(exp[i].balance).toFixed(1);
      cross.setAttribute('x1', vx);
      cross.setAttribute('x2', vx);
      ring.setAttribute('cx', vx);
      ring.setAttribute('cy', vy);
      tip.replaceChildren(...card(i));
      box.classList.add('on');
      place(+vx, +vy);
      open = box;
    }

    function hide() {
      box.classList.remove('on');
      if (open === box) open = null;
    }
    box.hide = hide;

    // The day at the pointer: a dot's own day, else the day nearest the pointer's x.
    function dayAt(e) {
      const dot = e.target.closest && e.target.closest('[data-day]');
      if (dot && svg.contains(dot)) return +dot.dataset.day;
      const r = svg.getBoundingClientRect();
      const vx = ((e.clientX - r.left) * g.W) / r.width;
      return Math.max(0, Math.min(n - 1, Math.round(((vx - g.x(0)) * (n - 1)) / Math.max(1, g.x(n - 1) - g.x(0)))));
    }

    svg.addEventListener('pointermove', (e) => show(dayAt(e)));
    svg.addEventListener('pointerdown', (e) => show(dayAt(e)));
    // a finger lifting off the screen leaves the chart too: a tap's card stays until the next tap elsewhere
    svg.addEventListener('pointerleave', (e) => { if (e.pointerType !== 'touch') hide(); });
    svg.addEventListener('focus', () => show(cur < 0 ? 0 : cur));
    svg.addEventListener('blur', hide);
    svg.addEventListener('keydown', (e) => {
      const at = cur < 0 ? 0 : cur;
      const to = {ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: n - 1}[e.key];
      if (to != null) {
        e.preventDefault();
        show(Math.max(0, Math.min(n - 1, to)));
      } else if (e.key === 'Escape' && box.classList.contains('on')) {
        e.preventDefault();
        e.stopPropagation();
        hide();
      }
    });
    return box;
  };
});
