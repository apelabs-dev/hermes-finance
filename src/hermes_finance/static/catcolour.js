/* The category's icon colour (schema v24, B3). A category's tile follows its group's colour unless the owner picks one of
 * the eight goal swatches (the same palette the goal picker uses: GET /api/categories/page gives it as `colours`);
 * NULL means follow the group.
 *
 * Two parts. (1) HOOKS.catRgb(category): the "r,g,b" every tile (.cb) is drawn in, from the payload's effective_colour
 * (the override, else the group's), so a picker row, a transaction badge, the Settings list and the charts that use a
 * category's colour all show an override. (2) X.catColourField(...): the Colour row of the New / Edit category sheet:
 * the same eight goal swatches, with the group's own swatch already selected. Picking another swatch sets the icon's
 * colour; picking the group's swatch keeps following the group. A group whose colour is outside the eight (Shopping,
 * Bills) gets one leading swatch in the group's own colour. Names come from the app, never markup. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, HOOKS, rgbOf} = X;
  const NAMES = ['Mint', 'Blue', 'Cyan', 'Purple', 'Amber', 'Pink', 'Coral', 'Slate'];   // the goal picker's names, in order

  HOOKS.catRgb = (c) => (c && c.effective_colour ? rgbOf(c.effective_colour) : (c && c.rgb) || null);

  // {palette, group, groupRgb, value, onChange(value)} -> {el, get value(), setGroup(name, rgb)}
  X.catColourField = ({palette, group, groupRgb, value = null, onChange = () => {}}) => {
    let current = palette.includes(value) ? value : null;   // the saved override: a palette hex, or null = follow the group
    let name = group;
    let rgb = groupRgb;
    const row = h('div', {class: 'gcolours', role: 'group', 'aria-label': 'Category colour'});
    let entries = [];
    const pick = (colour) => {
      current = colour;
      draw();
      onChange(current);
    };
    const draw = () => {
      for (const e of entries) {
        const on = e.group ? current === null || current === e.colour : current === e.colour;
        e.button.setAttribute('aria-pressed', String(on));
        fill(e.button, on ? icon('check') : null);
      }
    };
    const build = () => {
      const matched = palette.find((c) => rgbOf(c) === rgb);   // the group's colour, when one of the eight is it
      entries = [];
      const kids = [];
      if (!matched) {   // a group colour outside the palette: one leading swatch in the group's own colour
        const colour = `rgb(${rgb})`;
        entries.push({button: h('button', {class: 'gcolour', type: 'button', dataset: {colour, group: '1'},
          'aria-label': name, title: name, style: {'--swatch': colour}, onclick: () => pick(null)}), colour, group: true});
        kids.push(entries[0].button);
      }
      palette.forEach((colour, i) => {
        const groupSwatch = colour === matched;
        const button = h('button', {class: 'gcolour', type: 'button',
          dataset: groupSwatch ? {colour, group: '1'} : {colour},
          'aria-label': NAMES[i] || colour, title: NAMES[i] || colour, style: {'--swatch': colour},
          onclick: () => pick(groupSwatch ? null : colour)});
        entries.push({button, colour, group: groupSwatch});
        kids.push(button);
      });
      fill(row, kids);
      draw();
    };
    build();
    return {
      el: row,
      get value() { return current; },
      setGroup(n, r) { name = n; rgb = r; build(); },
    };
  };
});
