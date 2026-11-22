/* The category's icon colour (schema v24, B3). A category's tile follows its group's colour unless the owner picks one of
 * the eight goal swatches (the same palette the goal picker uses: GET /api/categories/page gives it as `colours`);
 * NULL means follow the group.
 *
 * Two parts. (1) HOOKS.catRgb(category): the "r,g,b" every tile (.cb) is drawn in, from the payload's effective_colour
 * (the override, else the group's), so a picker row, a transaction badge, the Settings list and the charts that use a
 * category's colour all show an override. (2) X.catColourField(...): the Colour row of the New / Edit category sheet:
 * "Same as <Group>" with a dot and a Change button; Change opens the eight swatches; picking one shows its name with a
 * "Use <Group> colour" button that goes back to following the group. Its elements are built once and updated in place,
 * so the focused button stays focused. Names come from the app, never markup. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, icon, HOOKS, rgbOf} = X;
  const NAMES = ['Mint', 'Blue', 'Cyan', 'Purple', 'Amber', 'Pink', 'Coral', 'Slate'];   // the goal picker's names, in order

  HOOKS.catRgb = (c) => (c && c.effective_colour ? rgbOf(c.effective_colour) : (c && c.rgb) || null);

  // {palette, group, groupRgb, value, onChange(value)} -> {el, get value(), setGroup(name, rgb)}
  X.catColourField = ({palette, group, groupRgb, value = null, onChange = () => {}}) => {
    let current = palette.includes(value) ? value : null;
    let open = Boolean(current);
    let name = group;
    let rgb = groupRgb;
    const dot = h('i', {class: 'cc-dot', 'aria-hidden': 'true'});
    const label = h('span', {class: 'cc-name'});
    const action = h('button', {class: 'btn sm ghost', type: 'button', 'aria-expanded': 'false', 'aria-controls': 'cc-swatches'});
    const swatches = h('div', {class: 'gcolours cc-swatches', id: 'cc-swatches', role: 'group', 'aria-label': 'Category colour'},
      palette.map((colour, i) => h('button', {class: 'gcolour', type: 'button', dataset: {colour}, 'aria-label': NAMES[i] || colour,
        title: NAMES[i] || colour, style: {'--swatch': colour}, onclick: () => { current = colour; open = true; draw(); onChange(current); }})));
    action.addEventListener('click', () => {
      if (current) { current = null; open = false; draw(); onChange(null); } else { open = !open; draw(); }
    });
    const row = h('div', {class: 'cc-row'}, dot, label, action);
    const draw = () => {
      const i = palette.indexOf(current);
      dot.style.setProperty('--c', current || `rgb(${rgb})`);
      label.textContent = current ? NAMES[i] || current : `Same as ${name}`;
      action.textContent = current ? `Use ${name} colour` : 'Change';
      action.setAttribute('aria-expanded', String(open));
      swatches.hidden = !open;
      swatches.querySelectorAll('.gcolour').forEach((b) => {
        const on = b.dataset.colour === current;
        b.setAttribute('aria-pressed', String(on));
        b.replaceChildren(...(on ? [icon('check')] : []));
      });
    };
    draw();
    return {
      el: h('div', {class: 'cc'}, row, swatches),
      get value() { return current; },
      setGroup(n, r) { name = n; rgb = r; draw(); },
    };
  };
});
