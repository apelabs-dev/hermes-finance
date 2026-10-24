/* The category picker (polish P0): the category control on a transaction row and in its sheet (HOOKS.catPick). A button
 * names the category; it opens every category under its group (an uppercase header, a tile in the group's colour, each
 * with its own icon), Income and Transfers last, with a search box on top. On a wide screen a popover under the button
 * (above it when there's no room below); under 600px a panel at the bottom over a dimmed page. Typing filters, the
 * arrows move, Enter picks; Escape, Tab or a tap outside closes it. The groups come from /api/category-groups. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, badge, api, toast, HOOKS, catLabel} = X;
  const PHONE = '(max-width: 600px)';
  let GROUPS = null;   // the request for /api/category-groups, made the first time a picker opens
  let OPEN = null;     // {root, scrim, btn, close} of the picker on screen
  let uid = 0;

  const loadGroups = () => {
    if (!GROUPS) GROUPS = api('/api/category-groups').catch((e) => { GROUPS = null; throw e; });
    return GROUPS;
  };

  // The sections to show: the groups in order, then the categories outside them (Income, Transfers, any not in a
  // group yet), each kept to the names this control offers.
  function sections(data, names) {
    const left = new Set(names);
    const out = [];
    for (const g of data.groups) {
      const cats = g.categories.map((c) => c.name).filter((n) => left.delete(n));
      if (cats.length) out.push({name: g.name, cats});
    }
    const rest = data.ungrouped.map((c) => c.name).filter((n) => left.delete(n));
    rest.push(...left);
    if (rest.length) out.push({name: 'Income & transfers', cats: rest});
    return out;
  }

  function close(returnFocus = false) {
    if (!OPEN) return;
    const {root, scrim, btn} = OPEN;
    OPEN = null;
    root.remove();
    if (scrim) scrim.remove();
    btn.setAttribute('aria-expanded', 'false');
    if (returnFocus) btn.focus();
  }

  // Under the button if the whole list fits there, else above it, else as much of the window as it needs (over the button)
  function place(root, btn) {
    Object.assign(root.style, {width: '', left: '', top: '', bottom: '', maxHeight: ''});
    if (matchMedia(PHONE).matches) return;   // the CSS docks it at the bottom
    const r = btn.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const w = Math.min(660, vw - 24);
    Object.assign(root.style, {width: `${w}px`, left: `${Math.max(12, Math.min(r.left, vw - w - 12))}px`, top: '0px'});
    const need = root.offsetHeight;
    if (need <= vh - r.bottom - 18) root.style.top = `${r.bottom + 6}px`;
    else if (need <= r.top - 18) Object.assign(root.style, {top: '', bottom: `${vh - r.top + 6}px`});
    else Object.assign(root.style, {top: `${Math.max(12, vh - need - 12)}px`, maxHeight: `${vh - 24}px`});
  }

  async function open(btn, names, onPick) {
    if (OPEN && OPEN.btn === btn) { close(true); return; }
    close();
    let data;
    try {
      data = await loadGroups();
    } catch (e) {
      toast(`Could not load the categories: ${e.message}`, [], {error: true, timeout: 5000});
      return;
    }
    const id = `cpk${++uid}`;
    const phone = matchMedia(PHONE).matches;
    const q = h('input', {type: 'search', class: 'cpkq', placeholder: 'Search categories', autocomplete: 'off', spellcheck: 'false',
      role: 'combobox', 'aria-label': 'Search categories', 'aria-controls': `${id}-l`, 'aria-expanded': 'true', 'aria-autocomplete': 'list'});
    const list = h('div', {class: 'cpkl', id: `${id}-l`, role: 'listbox', 'aria-label': 'Categories'});
    const none = h('p', {class: 'cpkn', hidden: true}, 'No category matches.');
    const opts = [];
    const groups = sections(data, names).map((s, gi) => {
      const hd = h('div', {class: 'cpkh', id: `${id}-g${gi}`}, s.name);
      const items = s.cats.map((n) => {
        const o = h('div', {class: 'cpko', role: 'option', id: `${id}-o${opts.length}`, 'aria-selected': String(n === btn.value),
          'data-cat': n}, badge(n, 's'), h('span', {class: 'cpkt'}, n), n === btn.value ? icon('check') : null);
        o.addEventListener('click', () => pick(n));
        o.addEventListener('pointermove', () => setActive(o, false));
        opts.push({el: o, key: `${n} ${s.name}`.toLowerCase()});
        return o;
      });
      return {el: h('div', {class: 'cpkg', role: 'group', 'aria-labelledby': hd.id}, hd, h('div', {class: 'cpki'}, items)), items};
    });
    fill(list, groups.map((g) => g.el), none);
    const root = h('div', {class: 'cpk', role: 'dialog', 'aria-label': 'Choose a category'},
      h('div', {class: 'cpks'}, icon('search'), q), list);
    const scrim = phone ? h('div', {class: 'cpkscrim', onclick: () => close(true)}) : null;
    let active = null;
    const shown = () => opts.filter((o) => !o.el.hidden).map((o) => o.el);
    function setActive(el, scroll = true) {
      if (active) active.classList.remove('on');
      active = el;
      if (el) {
        el.classList.add('on');
        q.setAttribute('aria-activedescendant', el.id);
        if (scroll) el.scrollIntoView({block: 'nearest'});
      } else q.removeAttribute('aria-activedescendant');
    }
    function filter() {
      const words = q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      for (const o of opts) o.el.hidden = !words.every((w) => o.key.includes(w));
      for (const g of groups) g.el.hidden = g.items.every((el) => el.hidden);
      none.hidden = shown().length > 0;
      setActive(words.length ? shown()[0] || null : null);
    }
    function pick(n) {
      close(true);
      if (n === btn.value) return;
      btn.value = n;
      onPick(n);
    }
    q.addEventListener('input', filter);
    root.addEventListener('keydown', (e) => {
      const els = shown();
      const i = els.indexOf(active);
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (els.length) setActive(els[(i + (e.key === 'ArrowDown' ? 1 : els.length - (i < 0 ? 0 : 1))) % els.length]);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (active && !active.hidden) pick(active.dataset.cat);
      } else if (e.key === 'Tab') {
        e.preventDefault();
        close(true);
      } else if (e.key.length === 1 && document.activeElement !== q && !e.metaKey && !e.ctrlKey) q.focus();
    });
    if (scrim) document.body.append(scrim);
    document.body.append(root);
    place(root, btn);
    OPEN = {root, scrim, btn};
    btn.setAttribute('aria-expanded', 'true');
    const cur = opts.find((o) => o.el.dataset.cat === btn.value);
    if (cur) setActive(cur.el);
    // a phone's keyboard would cover half the list: there the search box waits for a tap (or a typed letter)
    if (phone) { root.tabIndex = -1; root.focus(); } else q.focus();
  }

  // Escape closes only the picker (not a sheet under it); a tap outside closes it; so do scrolling the page and resizing
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && OPEN) { e.preventDefault(); e.stopImmediatePropagation(); close(true); }
  }, true);
  document.addEventListener('pointerdown', (e) => {
    if (OPEN && !OPEN.root.contains(e.target) && !OPEN.btn.contains(e.target) && e.target !== OPEN.scrim) close();
  }, true);
  window.addEventListener('resize', () => { if (OPEN) place(OPEN.root, OPEN.btn); });
  window.addEventListener('hashchange', () => close());

  // ({value, names, cls, label, title}, onPick): `value` reads and sets the category the button names, like a select's
  HOOKS.catPick = ({value, names, cls = '', label, title, short = false}, onPick) => {
    let cur = value;
    // `short`: a table cell shows the bare name (its icon and colour carry the group; the column has no room for both);
    // the accessible name always says 'Group · Category'
    const shown = (v) => (short ? v : catLabel(v));
    const text = h('span', {class: 'catn'}, shown(cur));
    const btn = h('button', {type: 'button', class: `catb${cls}`, 'aria-label': `${label}: ${catLabel(cur)}`, title: title || null,
      'aria-haspopup': 'dialog', 'aria-expanded': 'false'}, text);
    Object.defineProperty(btn, 'value', {get: () => cur, set: (v) => {
      cur = v;
      text.textContent = shown(v);
      btn.setAttribute('aria-label', `${label}: ${catLabel(v)}`);
    }});
    btn.addEventListener('click', (e) => { e.stopPropagation(); open(btn, names, onPick); });
    return btn;
  };
});
