/* The one icon picker (v23): categories and goals both open this. Closed, it shows two rows of "common" icons (16 on a
 * desktop, 12 on a phone) and a "Show all 100 icons" toggle below them. Open, the same icons become the first group,
 * "Common", inside a searchable scrolling list of all 100 with one group per taxonomy group; "Show fewer icons" closes
 * it. Every tile is a real button with glyph(), so every drawing is the app's own sprite. The selected ring is drawn
 * inside the tile (outline-offset -2), so opening the list and tabbing to the last row never clips it.
 * Data: GET /api/categories/icons (fetched once). It exposes X.iconPicker({rgb, value, quick, label, onPick}). */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, glyph, api} = X;
  let DATA = null;   // the one /api/categories/icons answer, shared by every picker on the page

  const load = () => {
    if (!DATA) DATA = api('/api/categories/icons').catch((e) => { DATA = null; throw e; });
    return DATA;
  };
  const words = (name) => name.replaceAll('-', ' ');

  X.iconPicker = ({rgb = '142,168,255', value = null, quick = null, label = 'Icon', onPick = () => {}} = {}) => {
    let data = null;
    let open = false;
    let selected = value;
    const root = h('div', {class: 'ipk'});
    const quickBox = h('div', {class: 'gicons ipk-quick', role: 'group', 'aria-label': `${label}: common`});
    const toggle = h('button', {class: 'btn ghost w ipk-toggle', type: 'button', 'aria-expanded': 'false'},
      h('span', {class: 'ipk-show'}, 'Show all 100 icons'), h('span', {class: 'ipk-few'}, 'Show fewer icons'),
      h('span', {class: 'ipk-chev'}, icon('chevd')));
    const search = h('input', {type: 'search', class: 'search ipk-search', placeholder: 'Search all icons',
      'aria-label': 'Search icons', autocomplete: 'off', spellcheck: 'false'});
    const count = h('span', {class: 'ipk-count'});
    const scroll = h('div', {class: 'ipk-scroll'});
    const none = h('p', {class: 'ipk-none', hidden: true}, 'No icon matches.');
    const panel = h('div', {class: 'ipk-open', hidden: true},
      h('div', {class: 'ipk-head'}, search, count), scroll, none);
    const tiles = [];   // every tile button in the open list (searchable), in document order

    function tile(name) {
      const b = h('button', {type: 'button', class: 'cb gicon', 'aria-pressed': String(name === selected),
        'aria-label': words(name), title: words(name), style: {'--rgb': rgb}}, glyph(name));
      b.addEventListener('click', () => pick(name));
      return b;
    }

    function pick(name) {
      selected = name;
      for (const b of tiles) b.setAttribute('aria-pressed', String(b.dataset.name === selected));
      for (const b of quickBox.querySelectorAll('.gicon')) b.setAttribute('aria-pressed', String(b.dataset.name === selected));
      onPick(name);
    }

    const quickList = () => (quick && quick.length ? quick : (data ? data.common.categories : []));

    function groups() {
      if (!data) return [];
      const q = quickList();
      return [{name: 'Common', icons: q}].concat(
        data.groups.map((g) => ({name: g.name, icons: g.icons.filter((n) => !q.includes(n))})));
    }

    function drawOpen() {
      tiles.length = 0;
      const sections = [];
      const q = search.value.trim().toLowerCase();
      for (const g of groups()) {
        const names = q ? g.icons.filter((n) => words(n).includes(q) || n.includes(q)) : g.icons;
        if (!names.length) continue;
        const grid = h('div', {class: 'gicons', role: 'group', 'aria-label': `${label}: ${g.name}`});
        for (const name of names) {
          const b = tile(name);
          b.dataset.name = name;
          grid.append(b);
          tiles.push(b);
        }
        sections.push(h('section', {class: 'ipk-group', dataset: {group: g.name}},
          h('div', {class: 'gh'}, g.name, h('span', {class: 'ln'})), grid));
      }
      fill(scroll, sections);
      none.hidden = tiles.length > 0;
      count.textContent = `${tiles.length} icon${tiles.length === 1 ? '' : 's'}`;
    }

    function drawQuick() {
      fill(quickBox, quickList().map((name) => {
        const b = tile(name);
        b.dataset.name = name;
        return b;
      }));
    }

    function setOpen(next) {
      open = next;
      panel.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      root.classList.toggle('ipk-opened', open);
      if (open) {
        drawOpen();
        search.focus();
      }
    }

    toggle.addEventListener('click', () => setOpen(!open));
    search.addEventListener('input', () => { drawOpen(); });
    root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && open) {
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
        toggle.focus();
        return;
      }
      if (!open) return;
      const shown = tiles.filter((b) => b.offsetParent !== null);
      const at = shown.indexOf(document.activeElement);
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
        if (shown.length) { e.preventDefault(); shown[Math.min(shown.length - 1, at + 1)]?.focus(); }
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
        if (shown.length) { e.preventDefault(); shown[Math.max(0, at - 1)]?.focus(); }
      }
    });

    fill(root, quickBox, toggle, panel);
    load().then((d) => {
      data = d;
      drawQuick();
      if (open) drawOpen();
      else count.textContent = `${d.count} icons`;
      root.dataset.ready = 'true';
    }).catch((e) => {
      fill(root, h('p', {class: 'ipk-none'}, `Could not load the icons: ${e.message}`));
    });
    return root;
  };
});
