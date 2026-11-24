/* Reports (0.5, B2): the Spending year view, Cash flow and Forecast pages, approved on the round-3 mock board
 * (hermes-finance-reports-cash-4c710f19113adaf6: Spending C heatmap + KPI rail, Cash flow B money flow, Forecast A
 * balance line, at 390 px and up).
 *
 * Data: GET /api/reports/spending, /cashflow, /forecast and /activity, drawn only from reports.py's functions. The two
 * answers (the forecast's accounts, a paycheck Add or Leave out) save through POST /api/settings. Every number and every
 * name is a text node built with h()/sv(); nothing here writes HTML, and the charts are createElementNS from numbers.
 * No percentage on the forecast (q6): the two lines are named, never a confidence band. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {$, h, fill, sv, icon, glyph, card, money, money2, bigMoney, plural, monthName, monthLabel, monthShort,
    thisMonth, fmtDate, RGB, ACC, OK, WARN, BAD, PAGES, S, RENDER, api, post, toast, route,
    errorCard, skel, lnk, pill, msgBox, ibadge, rgbOf} = X;

  PAGES.reports = 'Reports';

  const VIEWS = [['spending', 'Spending'], ['cashflow', 'Cash flow'], ['forecast', 'Forecast']];
  const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
  const REP = {view: 'spending', month: null, open: null, busy: false};
  const pct1 = (n) => (n == null ? '—' : `${Number(n).toFixed(1)}%`);
  const pct0 = (n) => (n == null ? '—' : `${Math.round(Number(n))}%`);
  const signedSpend = (n) => (n < 0 ? `−${money2(-n)}` : money2(n));
  const shortMonth = (m) => monthShort(m);
  const monthText = (m) => monthLabel(m);
  const RGB_OF = (value, fallback) => (typeof value === 'string' && /^\d{1,3},\d{1,3},\d{1,3}$/.test(value) ? value : fallback);

  function shiftMonth(m, n) {
    const k = Number(m.slice(0, 4)) * 12 + Number(m.slice(5, 7)) - 1 + n;
    return `${Math.floor(k / 12)}-${String((k % 12) + 1).padStart(2, '0')}`;
  }

  // SVG text keeps its 13px only when one viewBox unit is one CSS pixel: every chart's width is the card's own width.
  function chartW() {
    const page = ($('page') && $('page').clientWidth) || innerWidth;
    return Math.max(280, page - (innerWidth < 1024 ? 30 : 44));
  }

  function reportsHash(view, month) {
    return `#/reports?view=${encodeURIComponent(view)}&month=${encodeURIComponent(month)}`;
  }

  function go(view, month) {
    location.hash = reportsHash(view, month);
  }

  function viewOf() {
    const v = S.params.get('view');
    return VIEWS.some(([k]) => k === v) ? v : 'spending';
  }

  function monthOf() {
    const m = S.params.get('month');
    return m && MONTH_RE.test(m) && m <= thisMonth() ? m : thisMonth();
  }

  // --- the bar above every view: the three pages and the period ---------------------------------------------------
  function topBar(view, month) {
    const tabs = VIEWS.map(([key, label]) => h('button', {type: 'button', 'aria-pressed': String(key === view),
      onclick: () => go(key, month)}, label));
    const prev = h('button', {type: 'button', class: 'rp-step', 'aria-label': 'Previous month',
      onclick: () => go(view, shiftMonth(month, -1))}, icon('chevl'));
    const next = h('button', {type: 'button', class: 'rp-step', 'aria-label': 'Next month', disabled: month >= thisMonth(),
      onclick: () => go(view, shiftMonth(month, 1))}, icon('chevr'));
    const cyc = h('div', {class: 'rp-cycle', role: 'group', 'aria-label': 'Period'},
      h('span', {class: 'rp-range'}, view === 'forecast' ? 'Next 30 days' : monthText(month)),
      view === 'forecast' ? null : prev, view === 'forecast' ? null : next);
    return h('div', {class: 'rp-bar'},
      h('div', {class: 'seg rp-views'}, ...tabs), h('span', {class: 'sp'}), cyc);
  }

  function heading(title, note) {
    return h('div', {class: 'rp-head'}, h('h2', {}, title), note ? h('p', {class: 'rp-note'}, note) : null);
  }

  // --- the page --------------------------------------------------------------------------------------------------
  function renderReports() {
    const view = viewOf();
    const month = monthOf();
    REP.view = view; REP.month = month; REP.open = null; REP.busy = false;
    const body = h('div', {class: 'rp-body', 'aria-busy': 'true'}, h('span', {class: 'skel sk-card'}));
    fill($('page'), topBar(view, month), body);
    const path = {spending: '/api/reports/spending', cashflow: '/api/reports/cashflow', forecast: '/api/reports/forecast'}[view];
    const params = view === 'forecast' ? {} : {month};
    return api(path, params).then((d) => {
      body.removeAttribute('aria-busy');
      if (REP.view !== view || REP.month !== month) return;   // a newer tap already drew
      fill(body, {spending: drawSpending, cashflow: drawCashflow, forecast: drawForecast}[view](d, month));
    }).catch((e) => {
      body.removeAttribute('aria-busy');
      fill(body, errorCard(e, () => renderReports()));
    });
  }

  // --- Spending: the year heatmap and the KPI rail (C) -----------------------------------------------------------
  function drawSpending(d, month) {
    const y = d.year;
    if (!y.data_from) {
      return [h('div', {class: 'rp-rc'}, h('section', {class: 'card rp-heat'},
        h('h3', {}, h('span', {class: 'ttl'}, 'Year view')),
        msgBox('No transactions yet', 'Reports fill in after the first sync or a CSV import.')))];
    }
    const out = [h('div', {class: 'rp-rc'})];
    const heat = h('section', {class: 'card rp-heat'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.acc}}, glyph('calendar')),
        h('span', {class: 'ttl'}, 'Year view'),
        h('small', {}, `${monthShort(y.months[0].month)} ${y.months[0].month.slice(0, 4)} – ${monthShort(y.months[y.months.length - 1].month)} ${y.months[y.months.length - 1].month.slice(0, 4)}`)),
      h('p', {class: 'rp-sub'}, 'A month is that category’s net: spending minus its refunds. Each row is shaded against its own busiest month.'),
      heatmapDesktop(y, month),
      h('div', {class: 'rp-legend'}, h('span', {}, 'vs. each category’s own 12-month peak'),
        h('span', {class: 'rp-ramp'}, 'lower', h('i', {}), 'higher')),
      h('p', {class: 'rp-note rp-phonly'}, 'Tap a category to open its rows in place. The outlined cell is this month.'));
    const phone = phoneCards(y, month);
    heat.append(phone);
    const rail = h('div', {class: 'rp-rail'}, ...kpiCards(y));
    out[0].append(rail, heat);
    out.push(monthLine(y));
    const foreign = y.foreign.map((f) => `${f.currency} ${money2(f.totals.net)}`).join(' · ');
    if (foreign) out.push(h('p', {class: 'rp-note'}, `${foreign}: reported on its own and never added to ${y.currency}.`));
    return out;
  }

  function heatmapDesktop(y, month) {
    const grid = h('div', {class: 'rp-hm', role: 'table', 'aria-label': 'Spending by category and month'});
    grid.append(h('div', {class: 'rp-hh rp-lab'}, ''));
    y.months.forEach((m) => grid.append(h('div', {class: 'rp-hh' + (m.month === month ? ' on' : '')}, shortMonth(m.month))));
    grid.append(h('div', {class: 'rp-hh rp-tot'}, '12 mo'));
    y.categories.forEach((c) => {
      const name = h('div', {class: 'rp-rl'},
        ibadge(c.icon || 'tag', RGB_OF(c.group_rgb, RGB.grey), 's'), h('span', {}, c.category));
      grid.append(name);
      c.months.forEach((cell) => {
        const label = `${c.category}, ${monthText(cell.month)}: ${cell.nodata ? 'no data' : money2(cell.net)}`;
        const b = h(cell.nodata || cell.net === 0 ? 'div' : 'button', {
          class: 'rp-c' + (cell.nodata ? ' nodata' : cell.net < 0 ? ' refund' : '') + (cell.month === month ? ' on' : ''),
          type: cell.nodata || cell.net === 0 ? null : 'button',
          style: cell.nodata ? null : {'--a': String(Math.max(0.12, cell.alpha))},
          title: label, 'aria-label': label,
          onclick: cell.nodata || cell.net === 0 ? null : () => drill(b, c, cell.month),
        }, cell.nodata ? h('span', {class: 'sr'}, 'No data') : null);
        grid.append(b);
      });
      grid.append(h('div', {class: 'rp-tv'}, money(c.net)));
      const slot = h('div', {class: 'rp-drill', id: `d${c.category_id}`});
      grid.append(slot);
    });
    return grid;
  }

  function phoneCards(y, month) {
    const box = h('div', {class: 'rp-cards'});
    y.categories.forEach((c) => {
      const art = h('article', {class: 'card rp-cat', 'data-cat': c.category});
      const head = h('button', {class: 'rp-cat-b', type: 'button', 'aria-expanded': 'false',
        'aria-label': `${c.category}: ${money(c.net)} over 12 months. Open its rows.`},
        ibadge(c.icon || 'tag', RGB_OF(c.group_rgb, RGB.grey), 's'), h('b', {}, c.category),
        h('span', {class: 'amt v'}, money(c.net)), icon('chevd'));
      const spark = h('div', {class: 'rp-spark', role: 'img',
        'aria-label': `${c.category} by month, ${monthShort(y.months[0].month)} to ${monthShort(y.months[y.months.length - 1].month)}`},
        c.months.map((cell) => h('i', {class: (cell.nodata ? 'nd' : '') + (cell.month === month ? ' on' : ''),
          style: cell.nodata ? null : {'--a': String(Math.max(0.12, cell.alpha))}})));
      const slot = h('div', {class: 'rp-drill', id: `p${c.category_id}`});
      head.addEventListener('click', () => {
        art.classList.toggle('open', slot.dataset.open !== month);
        drill(head, c, month, slot);
      });
      art.append(head, spark, slot);
      box.append(art);
    });
    return box;
  }

  function kpiCards(y) {
    const k = y.kpis;
    const cards = [];
    cards.push(h('section', {class: 'card rp-kpi'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.acc}}, glyph('linechart')), h('span', {class: 'ttl'}, '12-month average')),
      bigMoney(k.average), h('div', {class: 'rp-sub'}, `spent per month · ${plural(k.months, 'complete month')}`)));
    const best = k.best;
    cards.push(h('section', {class: 'card rp-kpi'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.ok}}, glyph('sprout')), h('span', {class: 'ttl'}, 'Best month')),
      h('div', {class: 'big v md rp-name'}, best ? monthName(best.month) : '—'),
      h('div', {class: 'rp-sub'}, best ? `remaining ${money2(best.remaining)} · ${pct1(best.share)} of income` : 'no month has data yet')));
    const v = k.most_variable;
    cards.push(h('section', {class: 'card rp-kpi'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.bad}}, glyph('bag')), h('span', {class: 'ttl'}, 'Most variable')),
      h('div', {class: 'big v md rp-name'}, v ? v.category : '—'),
      h('div', {class: 'rp-sub'}, v ? `from ${money(v.from)} to ${money(v.to)} a month` : 'not enough months yet')));
    const s = k.savings_rate;
    cards.push(h('section', {class: 'card rp-kpi'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.ok}}, glyph('piggy')), h('span', {class: 'ttl'}, 'Savings rate now')),
      h('div', {class: 'big v md'}, pct1(s.now)),
      h('div', {class: 'rp-sub'}, `12-month average ${pct1(s.average)}${y.months[y.months.length - 1].month === thisMonth() ? ' · this month so far' : ''}`,
        s.delta == null ? null : h('span', {class: `pill sm ${s.delta >= 0 ? '' : 'warn'}`},
          `${s.delta >= 0 ? 'above' : 'below'} by ${Math.abs(s.delta).toFixed(1)} pts`))));
    return cards;
  }

  // The chart the mock calls "Net saved per month": now "Remaining after spending" (q2's wording).
  function monthLine(y) {
    const rows = y.months;
    const W = chartW(), H = 150, L = 10, R = 10, T = 16, B = 26;
    const vals = rows.map((r) => r.remaining);
    const max = Math.max(1, ...vals), min = Math.min(0, ...vals);
    const x = (i) => L + (i * (W - L - R)) / Math.max(1, rows.length - 1);
    const yy = (v) => T + ((max - v) * (H - T - B)) / Math.max(1, max - min);
    const pts = vals.map((v, i) => [x(i), yy(v)]);
    const path = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('');
    const area = `${path}L${x(vals.length - 1).toFixed(1)},${yy(Math.min(0, min)).toFixed(1)}L${x(0).toFixed(1)},${yy(Math.min(0, min)).toFixed(1)}Z`;
    const svg = sv('svg', {class: 'rp-msvg', viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': `Remaining after spending per month, ${monthShort(rows[0].month)} to ${monthShort(rows[rows.length - 1].month)}`},
      sv('line', {x1: L, x2: W - R, y1: yy(0).toFixed(1), y2: yy(0).toFixed(1), class: 'rp-axis'}),
      sv('path', {d: area, class: 'rp-area'}),
      sv('path', {d: path, class: 'rp-line'}),
      ...pts.map((p, i) => (rows[i].nodata ? null : sv('circle', {cx: p[0].toFixed(1), cy: p[1].toFixed(1),
        r: i === pts.length - 1 ? 4 : 2.4, class: i === pts.length - 1 ? 'rp-dot now' : 'rp-dot'}))),
      ...rows.map((r, i) => ((i % 2 === 0 || i === rows.length - 1) && !r.nodata
        ? sv('text', {x: x(i).toFixed(1), y: H - 8, class: 'rp-tick',
          'text-anchor': i === 0 ? 'start' : i === rows.length - 1 ? 'end' : 'middle'}, monthShort(r.month))
        : null)));
    const last = rows[rows.length - 1];
    return h('section', {class: 'card rp-month'},
      h('div', {class: 'rp-mhead'},
        h('div', {}, h('b', {}, 'Remaining after spending'), h('span', {class: 'rp-sub'}, 'per month')),
        h('div', {class: 'rp-mnow v'}, money2(last.remaining), h('small', {}, `in ${monthShort(last.month)}`))),
      svg,
      h('p', {class: 'rp-note'}, 'Income minus net spending. Money moved between your accounts is not income or spending.'));
  }

  // --- the drill-down, open in place (q3) -----------------------------------------------------------------------
  function drill(anchor, cat, month, slot) {
    const box = slot || (anchor.closest && anchor.closest('.rp-hm')).querySelector(`#d${cat.category_id}`);
    if (!box) return;
    const isOpen = box.dataset.open === month;
    box.replaceChildren();
    delete box.dataset.open;
    if (isOpen) {
      if (anchor.setAttribute) anchor.setAttribute('aria-expanded', 'false');
      return;
    }
    box.dataset.open = month;
    if (anchor.setAttribute) anchor.setAttribute('aria-expanded', 'true');
    box.append(h('p', {class: 'rp-note'}, 'Loading…'));
    api('/api/reports/activity', {month, category_id: cat.category_id}).then((d) => {
      if (box.dataset.open !== month) return;
      fill(box, activityPanel(d, cat));
    }).catch((e) => fill(box, h('p', {class: 'rp-note bad'}, `${e.message}. Nothing was changed.`)));
  }

  function activityPanel(d, cat) {
    return [
      h('div', {class: 'rp-dh'}, `${monthText(d.month)} · ${cat.category}`),
      h('div', {class: 'rp-gnr'},
        h('div', {class: 'rp-gn'}, h('span', {}, 'Gross'), h('b', {class: 'v'}, money2(d.gross))),
        h('div', {class: 'rp-gn'}, h('span', {}, 'Refunds'), h('b', {class: 'v'}, d.refunds ? signedSpend(d.refunds) : money2(0))),
        h('div', {class: 'rp-gn net'}, h('span', {}, 'Net'), h('b', {class: 'v'}, money2(d.net)))),
      d.rows.length ? h('div', {class: 'rp-tx'}, d.rows.map((r) => h('div', {class: 'rp-txr'},
        h('span', {}, r.label, h('span', {class: 'rp-d'}, fmtDate(r.date))),
        h('span', {class: 'amt v'}, signedSpend(r.amount))))) : h('p', {class: 'rp-note'}, 'No transactions in this month.'),
      h('div', {class: 'rp-dfoot'}, h('span', {class: 'rp-note'}, plural(d.count, 'transaction')),
        lnk(`#/transactions?category=${encodeURIComponent(cat.category)}&month=${d.month}`, 'Open in Transactions')),
    ];
  }

  // --- Cash flow (B): the Sankey on a desktop, labelled bars on a phone ------------------------------------------
  function drawCashflow(d, month) {
    if (!d.money_in && !d.money_out) {
      return [h('section', {class: 'card'}, h('h3', {}, h('span', {class: 'ttl'}, 'Money flow')),
        msgBox(`Nothing moved in ${monthText(month)}`, 'Reports fill in after the first sync or a CSV import.'))];
    }
    const out = [];
    const chips = h('div', {class: 'rp-chips'},
      pill('nt sm', null, `${money(d.money_out)} out`,
        d.income.total ? ` · ${pct0((d.money_out / d.income.total) * 100)} of income` : ''),
      d.short ? pill('bad sm', null, `Short by ${money(-d.remaining)}`)
        : pill('', null, `${money(d.remaining)} remaining · ${pct1(d.remaining_share)}`));
    out.push(h('section', {class: 'card rp-flow'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.acc}}, glyph('swap')),
        h('span', {class: 'ttl'}, 'Money flow'), h('small', {}, `${monthText(month)} · income to spending groups`)),
      chips,
      h('p', {class: 'rp-note rp-skn'}, 'Every bar is sized by its share of the flow; a group’s % is its share of money out.'),
      sankey(d),
      barsPhone(d)));
    out.push(h('p', {class: 'rp-note rp-phonly'},
      'A purchase refund reduces its own category. If a category’s refunds beat its spending, it appears here as its own source, “Refunds over spending · category”. Tax refunds stay in Interest and tax refunds.'));
    out.push(movesCard(d));
    return out;
  }

  function flowSources(d) {
    const rows = d.income.sources.map((s) => ({label: s.label, amount: s.amount, rgb: RGB.ok}));
    d.refunds_over.forEach((r) => rows.push({label: r.label, amount: r.amount, rgb: RGB.ok}));
    return rows;
  }

  function flowSinks(d) {
    const rows = d.groups.map((g) => ({label: g.group, amount: g.net, rgb: RGB_OF(g.rgb, RGB.acc), share: g.share}));
    if (d.remaining > 0) rows.push({label: 'Remaining after spending', amount: d.remaining, rgb: RGB.ok, rem: true});
    return rows;   // a short month's cushion is a source ("From the balance"), never a spending group
  }

  function sankey(d) {
    const W = chartW(), H = Math.max(400, Math.min(540, Math.round(W * 0.52))), TOP = 56, BOT = 44, GAP = 4, BAR = 14;
    const sources = flowSources(d);
    if (d.short) sources.push({label: 'From the balance', amount: -d.remaining, rgb: RGB.bad});  // not income: the cushion
    const sinks = flowSinks(d).filter((s) => !s.short);
    if (!sources.length || !sinks.length) return msgBox('No flow to draw', 'There is nothing in or out for this month.');
    // The source column is exactly the server's figure: income plus refunds over spending, and for a short month that
    // plus the balance it drew (money out). Nothing here adds a displayed number up.
    const flow = d.short ? d.money_out : d.money_in;
    const avail = H - TOP - BOT;
    const unit = avail / Math.max(flow, 1);                // every node's height is its share of the flow
    // Every label is measured in the card's own font and cut to its gutter, so no label can leave the chart; the bars
    // list and the aria-label keep the whole text. The 1.04 covers a font that swapped in after the first measure.
    const measure = document.createElement('canvas').getContext('2d');
    const textW = (s, px, weight) => {
      if (!measure) return s.length * px * 0.64;
      measure.font = `${weight} ${px}px Inter, system-ui, -apple-system, sans-serif`;
      return measure.measureText(s).width * 1.04;
    };
    const fitted = (s, px, weight, room) => {
      let out = s;
      while (out.length > 4 && textW(out, px, weight) > room) out = `${out.slice(0, out.length - 2).trimEnd()}…`;
      return out;
    };
    const leftRoom = Math.round(W * 0.34) - 12;            // the label column may take 17%..34% of the chart
    const srcText = sources.map((s) => fitted(s.label, 13.5, 600, leftRoom));
    const xL = Math.round(Math.max(W * 0.17, Math.max(0, ...srcText.map((t) => textW(t, 13.5, 600))) + 12));
    const xLb = xL + 10, xLr = xLb + BAR + 1;              // the bar, then its ribbons
    const amountText = (n) => `${money(n.amount)}${n.share != null ? ` · ${pct0(n.share)}` : ''}`;
    const xM = Math.round(W * 0.48), xMr = xM + 16;
    const rightRoom = W - 26 - xMr - BAR;                  // the widest a right label may be if its bar starts early
    const sinkText = sinks.map((n) => fitted(n.label, 13.5, 600, rightRoom));
    const rightNeed = Math.max(0, ...sinks.map((n, i) => Math.max(
      textW(sinkText[i], 13.5, 600), textW(amountText(n), 13, 400)))) + 12;
    const xR = Math.round(Math.min(W * 0.79, W - 8 - rightNeed - BAR));
    const xRl = xR + BAR + 6;
    const color = (rgb) => `rgb(${rgb})`;
    const ribbon = (x1, y1, h1, x2, y2, h2, rgb, opacity) => {
      const mx = (x1 + x2) / 2;
      return sv('path', {d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2} `
        + `L${x2},${y2 + h2} C${mx},${y2 + h2} ${mx},${y1 + h1} ${x1},${y1 + h1} Z`,
        fill: color(rgb), 'fill-opacity': opacity});
    };
    // left: the sources (income, refunds over spending, and a short month's balance), stacked with gaps
    const leftAvail = avail - GAP * Math.max(0, sources.length - 1);
    let y = TOP;
    const left = sources.map((s, i) => {
      const n = {...s, drawn: srcText[i], y, h: Math.max(4, leftAvail * (s.amount / Math.max(flow, 1)))};
      y += n.h + GAP;
      return n;
    });
    // right: each sink's height is its share of the same flow, so the columns close exactly
    let cy = TOP;
    const right = sinks.map((s, i) => {
      const n = {...s, drawn: sinkText[i], y: cy, h: Math.max(2, s.amount * unit)};
      cy += n.h;
      return n;
    });
    let cursor = TOP;
    const leftRibbons = left.map((n) => {
      const h2 = n.amount * unit;
      const r = ribbon(xLr, n.y, n.h, xM, cursor, h2, n.rgb, 0.26);
      cursor += h2;
      return r;
    });
    cursor = TOP;
    const rightRibbons = right.map((n) => {
      const r = ribbon(xMr, cursor, n.h, xR, n.y, n.h, n.rgb, 0.22);
      cursor += n.h;
      return r;
    });
    // A label is a name, and its amount one line under it only when the bar has the room for both: each line is 16px
    // tall and the amount sits on an 18px pitch, so a two-line label takes 34px of the bar. A shorter bar keeps its name
    // only (the amount stays in the bar's <title>, and the whole text in the chart's aria-label). Labels in a column are
    // pushed down as far as needed to keep a clear line between them, and one that cannot fit in the chart is dropped.
    // (B2 follow-up: a 24-30px bar's amount line used to land on the next bar's name at 1024.)
    const LINE_H = 16, PITCH = 18, CLEAR = 1;   // 16px is what 13.5px / 13px type measures as a line box here
    const labelColumn = (nodes, x, anchor, amountOf) => {
      const shown = nodes.filter((n) => n.h >= 13);
      const tops = shown.map((n) => n.y + Math.min(n.h / 2 + 5, n.h - 2) - LINE_H);   // where the name would sit
      const out = [];
      let floor = -Infinity;                     // the bottom of the last line drawn in this column
      shown.forEach((n, i) => {
        const top = Math.max(tops[i], floor + CLEAR);
        if (top + LINE_H > H - 1) return;        // no room left: the bar's <title> keeps the label
        out.push(sv('text', {x, y: top + 13, class: 'rp-sk-name', 'text-anchor': anchor}, n.drawn,
          n.drawn === n.label ? null : sv('title', {}, n.label)));
        floor = top + LINE_H;
        const at = top + PITCH;
        const next = i + 1 < shown.length ? Math.max(tops[i + 1], floor + CLEAR) : Infinity;
        if (at + LINE_H + CLEAR > next || at + LINE_H > H - 1) return;   // no clear line before the next name
        out.push(sv('text', {x, y: at + 13, class: 'rp-sk-amt', 'text-anchor': anchor}, amountOf(n)));
        floor = at + LINE_H;
      });
      return out;
    };
    const bar = (x, n) => sv('rect', {x, y: n.y, width: BAR, height: Math.max(2, n.h), rx: 4, fill: color(n.rgb)},
      sv('title', {}, `${n.label}: ${money2(n.amount)}${n.share != null ? ` (${pct0(n.share)} of money out)` : ''}`));
    return sv('svg', {class: 'rp-svg', viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': `Where ${monthText(d.month)}’s money in went: ${sources.map((s) => s.label).join(', ')}; `
        + `money out to ${sinks.map((s) => s.label).join(', ')}`},
      ...leftRibbons, ...rightRibbons,
      ...left.map((n) => bar(xLb, n)),
      sv('rect', {x: xM, y: TOP, width: 16, height: avail, rx: 4, fill: color(RGB.grey)}),
      sv('text', {x: xM + 8, y: TOP - 30, class: 'rp-sk-cap', 'text-anchor': 'middle'}, d.short ? 'Money out' : 'Money in'),
      sv('text', {x: xM + 8, y: TOP - 8, class: 'rp-sk-big', 'text-anchor': 'middle'}, money(flow)),
      ...right.map((n) => bar(xR, n)),
      ...labelColumn(left, xL, 'end', (n) => money(n.amount)),
      ...labelColumn(right, xRl, 'start', amountText));
  }

  function barsPhone(d) {
    const sources = flowSources(d);
    const sinks = flowSinks(d);
    const peak = Math.max(d.money_in, d.money_out, 1);
    const row = (label, amount, rgb, extra) => h('div', {class: 'rp-fr'},
      h('div', {class: 'rp-fl'}, h('i', {class: 'rp-sw', style: {background: `rgb(${rgb})`}}), h('b', {}, label),
        h('span', {class: 'rp-fa'}, `${money(amount)}${extra || ''}`)),
      h('div', {class: 'rp-track'}, h('i', {style: {width: `${Math.min(100, (amount / peak) * 100).toFixed(1)}%`, background: `rgb(${rgb})`}})));
    return h('div', {class: 'rp-bars'},
      h('div', {class: 'rp-flh'}, h('b', {}, 'Money in'), h('span', {}, money(d.money_in))),
      sources.map((s) => row(s.label, s.amount, s.rgb)),
      d.short ? h('div', {class: 'rp-flh bal'}, h('b', {}, 'From the balance'),
        h('span', {}, `${money(-d.remaining)} · not income`)) : null,
      h('div', {class: 'rp-flh'}, h('b', {}, 'Money out'), h('span', {}, `${money(d.money_out)} · share of money out`)),
      sinks.map((s) => row(s.label, s.amount, s.rgb, s.share != null ? ` · ${s.share >= 1 ? pct0(s.share) : s.share > 0 ? '<1%' : '0%'}` : '')),
      d.short ? null : h('div', {class: 'rp-rem'},
        h('b', {}, 'Remaining after spending'), h('span', {class: 'v'}, money(d.remaining)),
        h('small', {}, `${pct1(d.remaining_share)} of income`)));
  }

  function movesCard(d) {
    const rows = d.moves.rows.slice(-10);         // newest 10: moves.rows is the newest 50, oldest first
    const shown = d.moves.total;
    return h('section', {class: 'card rp-moves'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.grey}}, glyph('swap')), h('span', {class: 'ttl'}, 'Moved between your accounts'),
        h('small', {}, 'not drawn')),
      rows.length ? rows.map((m) => h('div', {class: 'li'},
        h('span', {class: 'grow'}, h('b', {}, m.label), h('span', {}, fmtDate(m.date))),
        h('span', {class: 'amt v'}, money2(m.amount)))) : h('p', {class: 'rp-note'}, 'No transfers or card payments in this month.'),
      d.moves.rows.length ? h('div', {class: 'rp-tot'}, h('span', {}, plural(d.moves.count, 'move')), h('b', {class: 'v'}, money2(shown))) : null,
      d.moves.count > rows.length ? h('p', {class: 'rp-note'}, `${plural(d.moves.count - rows.length, 'more move')} in Transactions.`) : null);
  }

  // --- Forecast (A): the balance line, the accounts, the ask-first paycheck card ----------------------------------
  function drawForecast(d) {
    const out = [];
    out.push(accountsCard(d));
    const k = d.expected;
    out.push(h('div', {class: 'rp-kpis'},
      kpi('Expected in', money(k.in), RGB.ok, 'paychecks and other deposits in the window'),
      kpi('Expected out', money(k.out), RGB.bad, 'bills and typical everyday spending'),
      kpi('Net', `${k.net < 0 ? '' : '+'}${money(k.net)}`, k.net < 0 ? RGB.bad : RGB.ok, k.net < 0 ? 'more out than in' : 'to the good'),
      kpi(`Available ${fmtDate(d.available_date)}`, money(d.available), RGB.acc,
        `expected balance ${money(d.lowest.balance)} on ${fmtDate(d.lowest.date)}`)));
    const waiting = d.paychecks.detected.filter((s) => s.answer == null);
    if (waiting.length) out.push(h('div', {class: 'rp-askwrap'}, waiting.map(askCard)));
    const excluded = d.paychecks.excluded_by_account || [];
    if (excluded.length) out.push(h('section', {class: 'card rp-warn'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.warn}}, glyph('alert')), h('span', {class: 'ttl'}, 'Added, but not in this line')),
      excluded.map((s) => h('div', {class: 'li'},
        h('span', {class: 'grow'}, h('b', {}, s.label), h('span', {}, `pays into ${s.account_name || s.account_id}, which is not ticked above`)),
        h('span', {class: 'amt v'}, money2(s.amount)))),
      h('p', {class: 'rp-note'}, 'Tick that account above, or leave the paycheck out. Until then it is not counted.')));
    out.push(chartCard(d));
    out.push(h('div', {class: 'rp-lists'},
      inflowCard(d), billsCard(d)));
    const notes = d.warnings.filter((w) => !w.startsWith('possible paycheck:'));
    if (notes.length) out.push(h('section', {class: 'card rp-warn'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.warn}}, glyph('alert')), h('span', {class: 'ttl'}, 'What to know')),
      notes.map((w) => h('p', {class: 'rp-note'}, w))));
    return out;
  }

  function kpi(title, value, rgb, foot) {
    return h('section', {class: 'card rp-kpi'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': rgb}}, glyph('dollar')), h('span', {class: 'ttl'}, title)),
      h('div', {class: 'big v md'}, value), h('div', {class: 'rp-sub'}, foot));
  }

  function accountsCard(d) {
    const o = d.account_options;
    const chips = o.accounts.map((a) => h('button', {class: `rp-chip${a.ticked ? ' on' : ''}`, type: 'button',
      'aria-pressed': String(a.ticked),
      onclick: () => saveAccounts(d, o.accounts.map((x) => (x.account_id === a.account_id ? {...x, ticked: !x.ticked} : x)))},
      h('span', {class: 'bx'}, a.ticked ? icon('check') : null), a.name || a.account_id));
    return h('section', {class: 'card rp-acc'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.acc}}, glyph('landmark')),
        h('span', {class: 'ttl'}, 'Accounts in this line')),
      chips.length ? h('div', {class: 'rp-chips2', role: 'group', 'aria-label': 'Accounts in the line'}, chips)
        : h('p', {class: 'rp-note'}, 'No cash account reports an available balance yet.'),
      h('p', {class: 'rp-note'}, 'Uses the bank’s available balance. Loans and investments are left out. A euro account gets its own line and is never added to dollars.'));
  }

  function saveAccounts(d, accounts) {
    if (REP.busy) return;
    REP.busy = true;
    post('/api/settings', {reports_forecast_accounts: accounts.filter((a) => a.ticked).map((a) => a.account_id)})
      .then(() => { toast('Forecast accounts saved'); route({keepToast: true}); })
      .catch((e) => toast(e.message))
      .finally(() => { REP.busy = false; });
  }

  function askCard(src) {
    return h('section', {class: 'card rp-ask', dataset: {paycheck: src.key}},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.warn}}, glyph('card')), h('span', {class: 'ttl'}, 'Possible paycheck found')),
      h('p', {class: 'rp-q'}, h('b', {}, src.label), ` pays about ${money2(src.amount)} ${src.cadence_label} `,
        `(last: ${fmtDate(src.last_date)}) into ${src.account_name || src.account_id}. It is not in the line until you add it.`),
      h('div', {class: 'rp-btns'},
        h('button', {class: 'btn sm', type: 'button', onclick: () => answerPaycheck(src, 'add')}, 'Add to the line'),
        h('button', {class: 'btn sm ghost', type: 'button', onclick: () => answerPaycheck(src, 'omit')}, 'Leave it out')),
      h('p', {class: 'rp-note'}, 'One answer per paycheck source. You can change it later.'));
  }

  function answerPaycheck(src, answer) {
    if (REP.busy) return;
    REP.busy = true;
    // Merge into the saved map instead of rebuilding it from the sources detected now: an answer already saved for a
    // source that is not detected this month (or another currency's) must survive the tap (reviewer round 1, security L3).
    api('/api/settings').then((st) => {
      const answers = {...((st && st.reports_paychecks) || {})};
      answers[src.key] = answer;
      return post('/api/settings', {reports_paychecks: answers});
    }).then(() => { toast(answer === 'add' ? 'Added to the line' : 'Left out'); route({keepToast: true}); })
      .catch((e) => toast(e.message))
      .finally(() => { REP.busy = false; });
  }

  function endRow(label, value, rgb) {
    return h('div', {class: 'rp-er'}, h('i', {style: {background: `rgb(${rgb})`}}),
      h('b', {}, label, h('small', {}, `Balance on ${fmtDate(value.date)}`)), h('span', {class: 'v'}, money2(value.balance)));
  }

  function chartCard(d) {
    const phone = innerWidth < 1024;
    const lines = d.lines;
    const low = d.lowest;
    const hasIn = d.events.some((e) => e.amount > 0), hasOut = d.events.some((e) => e.amount < 0);
    const box = h('section', {class: 'card rp-chart'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.acc}}, glyph('linechart')),
        h('span', {class: 'ttl'}, `Next ${d.horizon.days} days · ${fmtDate(d.horizon.start)}–${fmtDate(d.horizon.end)}`),
        h('small', {}, 'projected, chosen accounts')),
      h('div', {class: 'rp-leg'},
        h('span', {}, h('i', {class: 'lnacc'}), 'Balance so far'),
        h('span', {}, h('i', {class: 'dash'}), 'Projected'),
        h('span', {}, h('i', {class: 'band'}), 'Lower to higher everyday spending'),
        hasIn ? h('span', {}, h('i', {class: 'dot in'}), 'Paycheck') : null,
        hasOut ? h('span', {}, h('i', {class: 'dot out'}), 'Bill') : null),
      forecastLine(d, phone),
      h('div', {class: 'rp-ends'},
        endRow(lines.lower.label, {date: d.horizon.end, balance: lines.lower.end}, RGB.acc),
        endRow(lines.higher.label, {date: d.horizon.end, balance: lines.higher.end}, RGB.acc)),
      h('p', {class: 'rp-note'}, 'A lower and a higher everyday-spending case. Bills and paychecks are the same in both.'));
    const stale = d.opening.stale;
    if (stale) box.insertBefore(h('p', {class: 'rp-note warn'}, `The opening balance is from ${fmtDate(d.opening.as_of)} (${plural(d.opening.stale_days, 'day')} ago) and may be off.`), box.querySelector('.rp-leg'));
    return box;
  }

  function forecastLine(d, phone) {
    const W = chartW(), H = phone ? 300 : 316, L = phone ? 40 : 50, R = 12, T = 18, B = phone ? 66 : 62;
    const lines = d.lines;
    const all = [...lines.lower.points, ...lines.expected.points, ...lines.higher.points].map((p) => p.balance);
    let min = Math.min(...all), max = Math.max(...all);
    const pad = Math.max(50, (max - min) * 0.12);
    min -= pad; max += pad;
    const n = lines.expected.points.length;
    const x = (i) => L + (i * (W - L - R)) / Math.max(1, n - 1);
    const yy = (v) => T + ((max - v) * (H - T - B)) / Math.max(1, max - min);
    const path = (pts) => pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${yy(p.balance).toFixed(1)}`).join('');
    const exp = lines.expected.points;
    // The band is the area between the two named cases: along the lower line, back along the higher one. It is what the
    // legend "Lower to higher everyday spending" says it is (the projected line is drawn inside it, dashed).
    const band = lines.lower.points.concat([...lines.higher.points].reverse());
    const bandD = band.map((p, i) => `${i ? 'L' : 'M'}${x(i < n ? i : 2 * n - 1 - i).toFixed(1)},${yy(p.balance).toFixed(1)}`).join('') + 'Z';
    const solid = [exp[0], exp[1]];
    const grid = [];
    const span = max - min, step = span > 4000 ? 1000 : span > 1500 ? 500 : 250;
    for (let v = Math.ceil(min / step) * step; v <= max; v += step) {
      grid.push(sv('line', {x1: L, x2: W - R, y1: yy(v).toFixed(1), y2: yy(v).toFixed(1), class: 'rp-gl'}),
        sv('text', {x: 0, y: (yy(v) + 4).toFixed(1), class: 'rp-tick'}, money(v)));
    }
    const dayIndex = (iso) => Math.max(0, Math.min(n - 1, Math.round((new Date(`${iso}T12:00:00`) - new Date(`${d.horizon.start}T12:00:00`)) / 86400000)));
    const markers = d.events.filter((e) => e.date >= d.horizon.start && e.date <= d.horizon.end && Math.abs(e.amount) >= 1).map((e) => {
      const i = dayIndex(e.date);
      return sv('circle', {cx: x(i).toFixed(1), cy: yy(exp[i].balance).toFixed(1), r: 3.2,
        class: e.amount > 0 ? 'rp-ev in' : 'rp-ev out'});
    });
    const lo = dayIndex(d.lowest.date);
    const loX = x(lo), loY = yy(d.lowest.balance);
    const loText = `Expected balance on ${fmtDate(d.lowest.date)} · ${money(d.lowest.balance)}`;
    const below = loY + 20 < H - B - 4;
    return sv('svg', {class: 'rp-svg rp-fsvg', viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': `Projected balance for the next ${d.horizon.days} days: ${lines.lower.label} ends at ${money2(lines.lower.end)}, ${lines.higher.label} ends at ${money2(lines.higher.end)}`},
      ...grid,
      sv('path', {d: bandD, class: 'rp-band'}),
      sv('path', {d: path(lines.lower.points), class: 'rp-line lower'}),
      sv('path', {d: path(lines.higher.points), class: 'rp-line higher'}),
      sv('path', {d: `${path(solid)}`, class: 'rp-line sofar'}),
      sv('path', {d: path(exp), class: 'rp-line exp'}),
      ...markers,
      sv('circle', {cx: loX.toFixed(1), cy: loY.toFixed(1), r: 3, class: 'rp-ev low'}),
      sv('text', {x: Math.min(Math.max(loX, L + 4), W - R - 4).toFixed(1), y: (below ? loY + 20 : loY - 10).toFixed(1),
        class: 'rp-callout', 'text-anchor': loX > W * 0.75 ? 'end' : 'start'}, loText),
      sv('line', {x1: L, x2: W - R, y1: H - B, y2: H - B, class: 'rp-axis'}),
      sv('text', {x: L + 2, y: H - B + 18, class: 'rp-tick', 'text-anchor': 'start'}, fmtDate(d.horizon.start)),
      sv('text', {x: W - R - 2, y: H - B + 18, class: 'rp-tick', 'text-anchor': 'end'}, fmtDate(d.horizon.end)));
  }

  function inflowCard(d) {
    const rows = d.events.filter((e) => e.amount > 0);
    return h('section', {class: 'card'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.ok}}, glyph('arrow-up')),
        h('span', {class: 'ttl'}, 'Expected inflows'), h('small', {}, `${fmtDate(d.horizon.start)}–${fmtDate(d.horizon.end)}`)),
      rows.length ? rows.map((e) => h('div', {class: 'li'},
        h('span', {class: 'grow'}, h('b', {}, e.label), h('span', {}, fmtDate(e.date))),
        h('span', {class: 'amt v pos'}, `+${money2(e.amount)}`))) : h('p', {class: 'rp-note'}, 'Nothing expected in.'),
      h('div', {class: 'rp-tot'}, h('span', {}, plural(rows.length, 'expected deposit')), h('b', {class: 'v'}, money2(d.expected.in))));
  }

  function billsCard(d) {
    const all = d.events.filter((e) => e.amount < 0);
    const rows = all.slice().sort((a, b) => Math.abs(b.amount) - Math.abs(a.amount)).slice(0, 3);
    return h('section', {class: 'card'},
      h('h3', {}, h('span', {class: 'hi', style: {'--rgb': RGB.bad}}, glyph('receipt')),
        h('span', {class: 'ttl'}, 'Biggest upcoming bills'), h('small', {}, `${fmtDate(d.horizon.start)}–${fmtDate(d.horizon.end)}`)),
      rows.length ? rows.map((e) => h('div', {class: 'li'},
        h('span', {class: 'grow'}, h('b', {}, e.label),
          h('span', {}, `${fmtDate(e.date)}${e.overdue ? ' · overdue' : ''}${e.variable ? ' · estimate' : ''}`)),
        h('span', {class: 'amt v'}, signedSpend(e.amount)))) : h('p', {class: 'rp-note'}, 'No bills expected in the window.'),
      h('div', {class: 'rp-tot'}, h('span', {}, `all ${plural(all.length, 'bill')}`), h('b', {class: 'v'}, money2(d.bills_total))));
  }

  RENDER.reports = renderReports;
});
