/* Overview spending by taxonomy group. Hiding only changes the donut; rows retain the full month totals. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, badge, ibadge, CATI, icon, card, msgBox, money, pctOf, plural, monthName, monthShort, hashFor,
    post, toast, HOOKS, CHART, RGB, catChart, chartBox, destroyCharts} = X;
  HOOKS.whereCard = (d, later) => {
    const groups = d.spend_groups.filter((g) => g.count > 0);
    // Drilling into a group is this card's expanded state: remembered on this device (the group's name), and restored
    // on the next page while that group is still there. A name that no longer matches is ignored and cleared.
    const GROUP_KEY = 'hf.wherewent.group';
    // (v19: the group Other is Misc, so a device that remembered it drills into Misc)
    const readGroup = () => {
      try { const v = localStorage.getItem(GROUP_KEY); return v === 'Other' ? 'Misc' : v; } catch (e) { return null; }
    };
    const saveGroup = (name) => {
      try {
        if (name) localStorage.setItem(GROUP_KEY, name);
        else localStorage.removeItem(GROUP_KEY);
      } catch (e) { /* not remembered on this device */ }
    };
    if (!groups.length) {
      if (readGroup()) saveGroup(null);   // a group that has no spending any more is not restored
      return card({title: 'Where it went', icon: 'pie', rgb: RGB.purple},
        msgBox('No spending yet', `Nothing spent in ${monthName(d.month)} so far.`));
    }
    const body = h('div', {class: 'wherewent'});
    const remembered = readGroup();
    let active = (remembered && groups.find((g) => g.group === remembered)) || null;
    if (remembered && !active) saveGroup(null);
    let saving = false;
    const color = (rgb) => `rgb(${rgb})`;
    const names = (g) => g.categories.map((c) => c.category);
    const hidden = (g) => names(g).every((n) => CHART.hidden.has(n));
    const save = async (next, focus) => {
      if (saving) return;
      saving = true;
      const before = CHART.hidden;
      CHART.hidden = next;
      draw(focus)();
      try {
        const r = await post('/api/settings', {chart_excluded_categories: [...next]});
        CHART.hidden = new Set(r.chart_excluded_categories);
      } catch (e) {
        CHART.hidden = before;
        toast(`Could not save: ${e.message}`, [], {error: true});
      } finally {
        saving = false;
        if (body.isConnected) draw(focus)();
      }
    };
    const toggle = (keys, off, focus) => {
      const next = new Set(CHART.hidden);
      keys.forEach((n) => off ? next.delete(n) : next.add(n));
      save(next, focus);
    };
    const drill = (g) => { active = g; saveGroup(g.group); draw('back')(); };
    const back = () => { const key = active.group; active = null; saveGroup(null); draw(`drill:${key}`)(); };
    body.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && active) { e.preventDefault(); e.stopPropagation(); back(); }
    });
    function draw(focus) {
      destroyCharts(body);
      const total = active ? active.spend : groups.reduce((s, g) => s + g.spend, 0);
      const entries = active ? active.categories.filter((c) => c.count > 0).map((c) => ({
        name: c.category, spend: c.spend, count: c.count, color: color(active.rgb), keys: [c.category],
        off: CHART.hidden.has(c.category), tile: c.category, href: hashFor('transactions', {category: c.category}, d.month),
      })) : groups.map((g) => ({name: g.group, spend: g.spend, count: g.count, color: color(g.rgb), keys: names(g),
        off: hidden(g), tile: g.categories[0].category, group: g,
        href: hashFor('transactions', {group: g.group}, d.month)}));
      const shown = entries.map((x) => ({...x, spend: active ? (x.off ? 0 : x.spend)
        : x.group.categories.reduce((s, c) => s + (CHART.hidden.has(c.category) ? 0 : c.spend), 0)})).filter((x) => x.spend > 0);
      const included = shown.reduce((s, x) => s + x.spend, 0);
      const showAll = () => save(new Set(), 'first');
      const heading = active ? h('div', {class: 'wwnav'},
        h('button', {type: 'button', class: 'lnk', dataset: {wwfocus: 'back'}, onclick: back}, icon('chevl'), 'All groups'),
        h('b', {}, active.group)) : h('p', {class: 'wwhint'}, 'Choose a group to see its categories');
      let canvas;
      let top = h('div', {class: 'msgbox allhid'}, 'All categories hidden · ',
        h('button', {type: 'button', class: 'lnk', onclick: showAll}, 'Show all'));
      if (shown.length) {
        [top, canvas] = chartBox('ch-cat', '');
        canvas.dataset.label = `Spending by ${active ? `category in ${active.group}` : 'group'} in ${monthName(d.month)}`;
      }
      const part = new Map(shown.map((x) => [x.name, x.spend]));          // each row's shown part (a group may be partly hidden)
      const nHidden = entries.filter((x) => x.off).length || CHART.hidden.size;
      // A partly hidden group (2026-10-20: "hidden means hidden"): the row's amount, payment count and % all
      // describe only what the chart shows, and name what's hidden. data-spend keeps the full month for reconciling.
      const partial = (x) => {
        const off = x.off || !x.group ? [] : x.group.categories.filter((c) => c.count && CHART.hidden.has(c.category));
        if (!off.length) return null;
        const on = x.group.categories.filter((c) => !CHART.hidden.has(c.category));
        return {spend: part.get(x.name) || 0, count: on.reduce((s, c) => s + c.count, 0),
          note: `${off.length === 1 ? off[0].category : `${off.length} categories`} hidden`};
      };
      const rows = entries.map((x) => [x, partial(x)]).map(([x, p]) => h('div', {class: `crow${x.off ? ' off' : ''}`,
        dataset: {spend: x.spend.toFixed(2), ...(p ? {shown: p.spend.toFixed(2)} : {})}},
        h('button', {class: 'ctog', type: 'button', 'aria-pressed': String(x.off),
          'aria-label': `${x.off ? 'Show' : 'Hide'} ${x.name} ${x.off ? 'in' : 'from'} the chart`,
          dataset: {key: x.name, wwfocus: `toggle:${x.name}`}, onclick: () => toggle(x.keys, x.off, `toggle:${x.name}`)}, x.group ? ibadge(CATI[x.tile] || 'tag', x.group.rgb, 's') : badge(x.tile, 's')),
        h(x.group ? 'button' : 'a', {class: 'crowl', ...(x.group ? {type: 'button',
          'aria-label': `Explore ${x.name} categories`, dataset: {wwfocus: `drill:${x.name}`}, onclick: () => drill(x.group)} : {href: x.href})},
          h('span', {class: 'grow'}, h('b', {}, x.name),
            h('span', {}, x.off ? `hidden · ${plural(x.count, 'payment')}`
              : `${pctOf(part.get(x.name) || 0, included)} · ${plural(p ? p.count : x.count, 'payment')}${p ? ` · ${p.note}` : ''}`)),
          h('span', {class: 'amt v'}, money(p ? p.spend : x.spend))),
        x.group ? h('a', {class: 'wwpayments', href: x.href, 'aria-label': `View ${x.name} payments`, title: `View ${x.name} payments`}, icon('chevr')) : null));
      const fixed = CHART.fixed.filter((n) => CHART.hidden.has(n) || d.categories.some((c) => c.category === n && c.count));
      const allFixed = fixed.length && fixed.every((n) => CHART.hidden.has(n));
      fill(body, heading, top, h('div', {class: 'leg'}, rows),
        h('div', {class: 'wfoot'}, fixed.length ? h('button', {type: 'button', class: 'lnk', dataset: {preset: '', wwfocus: 'preset'},
          onclick: () => toggle(CHART.fixed, allFixed, 'preset')}, allFixed ? 'Show fixed bills' : 'Hide fixed bills') : null,
        CHART.hidden.size && shown.length ? h('span', {class: 'whid'}, `${nHidden} hidden · `,
          h('button', {class: 'lnk', type: 'button', onclick: showAll}, 'Show all')) : null));
      const target = focus === 'first' ? body.querySelector('.ctog')
        : [...body.querySelectorAll('[data-wwfocus]')].find((el) => el.dataset.wwfocus === focus);
      if (target) target.focus();
      return () => { if (canvas) catChart(canvas, shown, included, total, d.summary.is_current,
        (x) => x.group ? drill(x.group) : (location.hash = x.href)); };
    }
    later.push(draw());
    return card({title: 'Where it went', icon: 'pie', rgb: RGB.purple,
      extra: h('span', {class: 'hx'}, d.summary.is_current ? 'this month' : monthShort(d.month))}, body);
  };
});
