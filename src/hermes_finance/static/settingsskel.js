/* Settings' loading skeleton (settings.js's HF.RENDER.settings): the real page's eleven cards, in the real order, each
 * with shimmering rows where that card's text and controls go, so the page keeps its shape while GET /api/settings/page
 * is in flight. Layout uses the page's own .sgrid / .card / .sc / .hi / .ttl / .srow / .grow classes; settingsskel.css
 * only sizes the blocks, and the shimmer itself is app.css's .skel (light.css themes it). */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h} = X;
  const blk = (cls) => h('span', {class: `skel ${cls}`, 'aria-hidden': 'true'});
  const srow = (ctl = 'ssk-ctl') => h('div', {class: 'srow'}, h('span', {class: 'grow'}, blk('ssk-lab'), blk('ssk-sub')),
    blk(ctl));
  const listRow = () => h('div', {class: 'srow tight ssk-list'}, h('span', {class: 'grow'}, blk('ssk-lab'), blk('ssk-sub')),
    blk('ssk-ctl ssk-ctl-sm'));
  const card = (name, ...rows) => h('section', {class: 'card sc ssk-card', dataset: {skelCard: name}},
    h('h3', {}, h('span', {class: 'hi'}, blk('ssk-ico')), h('span', {class: 'ttl'}, blk('ssk-ttl'))), rows);
  X.settingsSkeleton = () => h('div', {class: 'sgrid', 'aria-busy': 'true', 'aria-label': 'Loading settings'},
    card('connections', srow(), srow(), srow('ssk-ctl-wide'), srow()),
    card('access', srow(), srow(), srow('ssk-ctl-wide')),
    card('ai', h('div', {class: 'srow'}, blk('ssk-seg')), srow(), srow(), srow()),
    card('mcp', srow(), srow('ssk-ctl-wide'), srow(), srow()),
    card('sync', srow(), srow(), srow('ssk-ctl-wide'), srow()),
    card('valuations', srow(), srow(), srow()),
    card('region', srow('ssk-ctl-wide'), srow(), srow(), srow()),
    card('look', srow('ssk-ctl-wide')),
    card('data', h('div', {class: 'data-actions'}, h('div', {}, blk('ssk-btn')),
      h('div', {}, blk('ssk-btn')), h('div', {}, blk('ssk-btn')))),
    card('categories', listRow(), listRow(), listRow(), listRow()),
    card('rules', listRow(), listRow(), listRow(), listRow()));
});
