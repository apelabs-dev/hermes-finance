/* The Transactions page's filters (polish P1). The bar keeps the search and the kind chips; a Filters button (Lucide's
 * funnel, with how many are picked) opens a drawer on the right, or a sheet from the bottom under 600px. In it:
 * Category (Uncategorized first, then each group with its categories: a group's box picks the whole group, a row's
 * count is how many payments it has in the month shown, typing filters), Tags (a multi-select: any of them matches),
 * Bank, and under 1024px Sort (the table's headers aren't shown there). The picks are a draft until "Show N payments";
 * every count follows the draft and the rest of the page's filters (search, kind). What's picked shows as chips under
 * the bar, each one clears itself, with how many match and their total, and "Clear all". */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, plural, badge, signed, api, S, TX, HOOKS, updateTx, txParams, txWindowParams, toast, trapFocus, catLabel} = X;
  const PHONE = '(max-width: 600px)';
  const SORT_NAMES = {date_desc: 'Newest first', date_asc: 'Oldest first', amount_desc: 'Biggest amount', amount_asc: 'Smallest amount',
    merchant_asc: 'Merchant A–Z', merchant_desc: 'Merchant Z–A', category_asc: 'Category A–Z', category_desc: 'Category Z–A',
    account_asc: 'Account A–Z', account_desc: 'Account Z–A'};
  let GROUPS = null;   // /api/category-groups, asked once
  let OPEN = null;     // the drawer on screen: {root, scrim, close}
  let uid = 0;

  const loadGroups = () => {
    if (!GROUPS) GROUPS = api('/api/category-groups').catch((e) => { GROUPS = null; throw e; });
    return GROUPS;
  };
  const picks = (f) => f.category.length + f.group.length + (f.uncategorized ? 1 : 0) + f.tag.length + (f.bank ? 1 : 0);
  const groupOf = (data, name) => (data ? data.groups.find((g) => g.categories.some((c) => c.name === name)) : null);

  // ---- the bar: the Filters button; under it, the chips of what's picked ----------------------------------------------
  const btnCount = h('span', {class: 'tfn', hidden: true});
  HOOKS.txBar = (f) => {
    const btn = h('button', {class: 'tfbtn', type: 'button', 'aria-haspopup': 'dialog', 'aria-expanded': 'false',
      onclick: () => openDrawer(btn)}, icon('funnel'), h('span', {}, 'Filters'), btnCount);
    drawCount(f);
    return btn;
  };

  function drawCount(f) {
    const n = picks(f);
    btnCount.textContent = String(n);
    btnCount.hidden = !n;
    const btn = btnCount.parentElement;
    if (btn) btn.setAttribute('aria-label', n ? `Filters, ${plural(n, 'filter')} on` : 'Filters');
  }

  function chipLabel(data, kind, name) {
    if (kind === 'group') return [h('b', {}, name), ' · all'];
    if (kind === 'tag') return `#${name}`;
    if (kind === 'category') {
      return catLabel(name);
    }
    return name;
  }

  async function drawChips(d) {
    const fbar = document.querySelector('#page .fbar');
    if (!fbar || !TX.filters) return;
    let row = document.querySelector('#page .txchips');
    if (!row) {
      row = h('div', {class: 'txchips', role: 'group', 'aria-label': 'Filters on'});
      fbar.after(row);
    }
    const f = TX.filters;
    drawCount(f);
    const sorted = f.sort !== 'date_desc';   // its chip shows under 1024px only, where the table has no headers
    if (!picks(f) && !sorted) {
      row.hidden = true;
      row.replaceChildren();
      return;
    }
    const data = f.category.length ? await loadGroups().catch(() => null) : null;
    if (TX.filters !== f) return;
    // the chip clicked goes away with what it cleared: focus moves to the Filters button
    const then = (clear) => () => { clear(); if (btnCount.parentElement) btnCount.parentElement.focus(); };
    const chip = (kind, name, clear) => h('button', {class: 'tfchip', type: 'button', 'aria-label': `Remove ${kind === 'tag' ? 'tag ' : ''}${name}`,
      onclick: then(clear)}, chipLabel(data, kind, name), icon('x'));
    const drop = (k, name) => () => updateTx({[k]: f[k].filter((x) => x !== name)});
    fill(row,
      f.uncategorized ? chip('uncategorized', 'Uncategorized', () => updateTx({uncategorized: false})) : null,
      f.group.map((g) => chip('group', g, drop('group', g))),
      f.category.map((c) => chip('category', c, drop('category', c))),
      f.tag.map((t) => chip('tag', t, drop('tag', t))),
      f.bank ? chip('bank', f.bank, () => updateTx({bank: ''})) : null,
      sorted ? h('button', {class: 'tfchip tfsortc', type: 'button', 'aria-label': `Sorted by ${SORT_NAMES[f.sort]}: back to newest first`,
        onclick: then(() => updateTx({sort: 'date_desc'}))}, icon('sort'), SORT_NAMES[f.sort], icon('x')) : null,
      picks(f) ? [txWindowParams().month ? h('span', {class: 'tfsum'}, `${plural(d.total, 'payment')} · `, h('b', {class: d.sum < 0 ? 'in' : ''}, signed(d.sum || 0))) : null,
        h('button', {class: 'lnk tfclr', type: 'button',
          onclick: then(() => updateTx({category: [], group: [], uncategorized: false, tag: [], bank: ''}))}, 'Clear all')] : null);
    row.classList.toggle('sortonly', !picks(f));
    row.hidden = false;
  }

  const loaded = HOOKS.txLoaded;
  HOOKS.txLoaded = (d) => {
    loaded(d);
    drawChips(d);
  };

  // ---- the drawer ---------------------------------------------------------------------------------------------------
  function closeDrawer(returnFocus = true) {
    if (!OPEN) return;
    const {root, scrim, btn} = OPEN;
    OPEN = null;
    btn.setAttribute('aria-expanded', 'false');
    root.classList.remove('on');
    scrim.classList.remove('on');
    const gone = () => { root.remove(); scrim.remove(); };
    setTimeout(gone, 160);   // the exit (--mo-exit, 120ms; a fade under reduced motion) is quicker than the entrance
    if (returnFocus && btn.isConnected) btn.focus();
  }

  async function openDrawer(btn) {
    if (OPEN) { closeDrawer(); return; }
    let data;
    try {
      data = await loadGroups();
    } catch (e) {
      toast(`Could not load the categories: ${e.message}`, [], {error: true, timeout: 5000});
      return;
    }
    const f = TX.filters;
    const id = `tf${++uid}`;
    // the draft: what "Show N payments" applies
    const D = {category: new Set(f.category), group: new Set(f.group), uncategorized: f.uncategorized, tag: new Set(f.tag),
      bank: f.bank, sort: f.sort};
    let facets = null;
    let total = TX.total;
    const expanded = new Set(data.groups.filter((g) => D.group.has(g.name) || g.categories.some((c) => D.category.has(c.name)))
      .map((g) => g.name));
    let tagsOpen = D.tag.size > 0;

    const sub = h('p', {class: 'tfsub', id: `${id}-sub`});
    const showBtn = h('button', {class: 'btn w', type: 'button', onclick: apply});
    const catCount = h('span', {class: 'tfsel'});
    const q = h('input', {type: 'search', class: 'tfq', placeholder: 'Search categories', autocomplete: 'off', spellcheck: 'false',
      'aria-label': 'Search categories'});
    const tree = h('div', {class: 'tftree', role: 'group', 'aria-label': 'Categories'});
    const tagBox = h('div', {class: 'tftags'});
    const bankBox = h('div', {class: 'tfpills', role: 'group', 'aria-label': 'Bank'});
    const sortSel = h('select', {class: 'tfsort', 'aria-label': 'Sort by'},
      Object.entries(SORT_NAMES).map(([k, label]) => h('option', {value: k, selected: k === D.sort}, label)));
    sortSel.addEventListener('change', () => { D.sort = sortSel.value; });

    // a box: a real checkbox laid over the whole row (so the row is what a finger hits), drawn by the span after it
    const box = (checked, mixed, onchange, label) => {
      const input = h('input', {type: 'checkbox', class: 'tfc', 'aria-label': label});
      input.checked = checked;
      input.indeterminate = mixed;
      input.addEventListener('change', () => onchange(input.checked));
      return [input, h('span', {class: 'tfbox', 'aria-hidden': 'true'}, icon('check'), icon('minus'))];
    };
    const n = (x) => h('span', {class: `tfcnt${x ? '' : ' zero'}`}, facets ? (x ? String(x) : '—') : '');

    function groupState(g) {
      if (D.group.has(g.name)) return 'all';
      const on = g.categories.filter((c) => D.category.has(c.name)).length;
      return on === 0 ? 'none' : on === g.categories.length ? 'all' : 'some';
    }
    function setGroup(g, on) {
      g.categories.forEach((c) => D.category.delete(c.name));
      if (on) D.group.add(g.name); else D.group.delete(g.name);
      changed();
    }
    function setCategory(g, name, on) {
      if (g && D.group.has(g.name)) {   // one out of a whole group: the rest of it, one by one
        D.group.delete(g.name);
        g.categories.forEach((c) => D.category.add(c.name));
      }
      if (on) D.category.add(name); else D.category.delete(name);
      if (g && g.categories.every((c) => D.category.has(c.name))) setGroup(g, true);   // all of them: the whole group
      else changed();
    }

    function drawTree() {
      const words = q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      const hit = (...s) => words.every((w) => s.join(' ').toLowerCase().includes(w));
      const rows = [];
      if (hit('Uncategorized', 'no category')) {
        rows.push(h('label', {class: 'tfr tfu'}, box(D.uncategorized, false, (on) => { D.uncategorized = on; changed(); }, 'Uncategorized'),
          h('span', {class: 'tfun', 'aria-hidden': 'true'}), h('span', {class: 'tft'}, 'Uncategorized'),
          n(facets && facets.uncategorized)));
      }
      for (const g of data.groups) {
        const cats = hit(g.name) ? g.categories : g.categories.filter((c) => hit(c.name, g.name));
        if (!cats.length) continue;
        const open = words.length > 0 || expanded.has(g.name);
        const state = groupState(g);
        const gid = `${id}-g-${g.name.replace(/\W+/g, '')}`;
        rows.push(h('div', {class: 'tfg'},
          h('button', {class: 'tfx', type: 'button', 'aria-expanded': String(open), 'aria-controls': gid,
            'aria-label': `${open ? 'Hide' : 'Show'} the ${g.name} categories`, onclick: () => {
              if (expanded.has(g.name)) expanded.delete(g.name); else expanded.add(g.name);
              drawTree();
              tree.querySelector(`[aria-controls="${gid}"]`).focus();
            }}, icon(open ? 'chevd' : 'chevr')),
          h('label', {class: 'tfr tfgr'}, box(state === 'all', state === 'some', (on) => setGroup(g, on), `${g.name}, the whole group`),
            h('span', {class: 'tfdot', style: {background: `rgb(${g.rgb})`}, 'aria-hidden': 'true'}), h('span', {class: 'tft'}, g.name),
            n(facets && facets.groups[g.name]))),
        open ? h('div', {class: 'tfkids', id: gid}, cats.map((c) => h('label', {class: 'tfr'},
          box(D.group.has(g.name) || D.category.has(c.name), false, (on) => setCategory(g, c.name, on), c.name),
          badge(c.name, 'xs'), h('span', {class: 'tft'}, c.name), n(facets && facets.categories[c.name])))) : null);
      }
      const rest = data.ungrouped.filter((c) => hit(c.name, 'income transfers'));
      if (rest.length) {
        rows.push(h('div', {class: 'tfh2'}, 'Income & transfers'), rest.map((c) => h('label', {class: 'tfr'},
          box(D.category.has(c.name), false, (on) => setCategory(null, c.name, on), c.name),
          badge(c.name, 'xs'), h('span', {class: 'tft'}, c.name), n(facets && facets.categories[c.name]))));
      }
      fill(tree, rows.length ? rows : h('p', {class: 'tfnone'}, 'No category matches.'));
    }

    function drawTags() {
      const all = facets ? facets.tags : [];
      const names = [...D.tag];
      const head = h('button', {class: 'tfdd', type: 'button', 'aria-expanded': String(tagsOpen), 'aria-controls': `${id}-tl`,
        onclick: () => { tagsOpen = !tagsOpen; drawTags(); tagBox.querySelector('.tfdd').focus(); }},
      icon('tag'), h('span', {class: 'tft'}, names.length ? names.map((x) => `#${x}`).join(', ') : 'Any tag'), icon('chevd'));
      if (!tagsOpen) { fill(tagBox, head); return; }
      const tq = h('input', {type: 'search', class: 'tfq', placeholder: 'Search tags', autocomplete: 'off', 'aria-label': 'Search tags'});
      const list = h('div', {class: 'tftl', id: `${id}-tl`, role: 'group', 'aria-label': 'Tags'});
      const known = new Set(all.map((x) => x.name.toLowerCase()));
      const rows = () => {
        const w = tq.value.trim().toLowerCase().replace(/^#/, '');
        const shown = [...all, ...names.filter((x) => !known.has(x.toLowerCase())).map((x) => ({name: x, count: 0}))]
          .filter((x) => x.name.toLowerCase().includes(w));
        fill(list, shown.length ? shown.map((x) => h('label', {class: 'tfr'},
          box([...D.tag].some((y) => y.toLowerCase() === x.name.toLowerCase()), false, (on) => {
            for (const y of [...D.tag]) if (y.toLowerCase() === x.name.toLowerCase()) D.tag.delete(y);
            if (on) D.tag.add(x.name);
            changed();
          }, `#${x.name}`),
          h('span', {class: 'tft'}, `#${x.name}`), n(x.count)))
          : h('p', {class: 'tfnone'}, all.length ? 'No tag matches.' : 'No tags yet. Add one from a transaction’s sheet.'));
      };
      tq.addEventListener('input', rows);
      rows();
      fill(tagBox, head, h('div', {class: 'tfddp'}, all.length > 6 ? tq : null, list, h('p', {class: 'tfnote'}, 'Matches any selected tag')));
    }

    function drawBanks() {
      const pill = (value, label) => h('button', {class: 'tfpill', type: 'button', 'aria-pressed': String(D.bank === value),
        onclick: () => { D.bank = value; changed(); }}, label);
      fill(bankBox, pill('', 'All banks'), TX.el.banks.map((b) => pill(b, b)));
    }

    function summary() {
      const on = [D.uncategorized ? 'Uncategorized' : null, ...[...D.group].map((g) => `${g} (all)`),
        ...[...D.category].map((c) => catLabel(c)),
        ...[...D.tag].map((x) => `#${x}`), D.bank || null].filter(Boolean);
      const k = D.category.size + D.group.size + (D.uncategorized ? 1 : 0);
      catCount.textContent = k ? `${k} selected` : '';
      sub.textContent = on.length ? `${on.length} active · ${on.join(', ')}` : 'Nothing picked: every payment shows';
      showBtn.textContent = `Show ${plural(total, 'payment')}`;
    }

    // the counts follow the draft (the newest answer wins; a slow one never overwrites it)
    let ask = 0;
    let timer = null;
    async function refresh() {
      const mine = ++ask;
      try {
        const d = await api('/api/transactions', {...txParams({...TX.filters, category: [...D.category], group: [...D.group],
          uncategorized: D.uncategorized, tag: [...D.tag], bank: D.bank}), ...txWindowParams(), kind: TX.filters.kind, limit: 1, facets: 1});
        if (mine !== ask || !OPEN) return;
        facets = d.facets;
        total = d.total;
        draw();
      } catch (e) {
        if (mine === ask && OPEN) sub.textContent = `Could not count: ${e.message}`;
      }
    }
    function changed() {
      draw();
      clearTimeout(timer);
      timer = setTimeout(refresh, 120);
    }
    function draw() {
      const focused = document.activeElement;
      const label = focused && focused.closest && focused.closest('.tfd') && focused.getAttribute('aria-label');
      drawTree();
      drawTags();
      drawBanks();
      summary();
      if (label && !root.contains(focused)) {   // the row redrawn: focus back on its box
        const again = [...root.querySelectorAll('[aria-label]')].find((el) => el.getAttribute('aria-label') === label);
        if (again) again.focus();
      }
    }
    function apply() {
      updateTx({category: [...D.category], group: [...D.group], uncategorized: D.uncategorized, tag: [...D.tag], bank: D.bank,
        sort: D.sort});
      closeDrawer();
    }
    function reset() {
      D.category.clear();
      D.group.clear();
      D.tag.clear();
      D.uncategorized = false;
      D.bank = '';
      changed();
    }
    q.addEventListener('input', drawTree);

    const sec = (title, extra, ...kids) => h('section', {class: 'tfs'}, h('div', {class: 'tfsh'}, h('h3', {}, title), extra), ...kids);
    const root = h('div', {class: 'tfd', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': `${id}-t`, 'aria-describedby': sub.id,
      tabindex: '-1'},
    h('div', {class: 'tfhd'}, h('div', {class: 'tfti'}, h('h2', {id: `${id}-t`}, 'Filters'), sub),
      h('button', {class: 'ib', type: 'button', 'aria-label': 'Close filters', onclick: () => closeDrawer()}, icon('x'))),
    h('div', {class: 'tfbody'},
      sec('Category', catCount, h('div', {class: 'tfsearch'}, icon('search'), q), tree),
      sec('Tags', null, tagBox),
      sec('Bank', null, bankBox),
      h('section', {class: 'tfs tfsorts'}, h('div', {class: 'tfsh'}, h('h3', {}, h('label', {for: `${id}-s`}, 'Sort'))),
        Object.assign(sortSel, {id: `${id}-s`}))),
    h('div', {class: 'tfft'}, h('button', {class: 'btn ghost', type: 'button', onclick: reset}, 'Reset'), showBtn));
    const scrim = h('div', {class: 'tfscrim', onclick: () => closeDrawer()});
    root.addEventListener('keydown', (e) => trapFocus(e, root));
    draw();
    document.body.append(scrim, root);
    OPEN = {root, scrim, btn};
    btn.setAttribute('aria-expanded', 'true');
    void root.offsetWidth;   // the start of the transition
    root.classList.add('on');
    scrim.classList.add('on');
    // a phone's keyboard would cover the sheet: there focus goes to the sheet itself, not the search box
    if (matchMedia(PHONE).matches) root.focus(); else q.focus();
    refresh();
  }

  // Escape closes the drawer only (not a sheet under it); so does leaving the page
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && OPEN) { e.preventDefault(); e.stopImmediatePropagation(); closeDrawer(); }
  }, true);
  window.addEventListener('hashchange', () => closeDrawer(false));
});
