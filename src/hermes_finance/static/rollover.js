/* Budget rollover (TASK-19). Loaded before app.js (window.HF_EXT). The cap editor gets a "Roll over what's left" switch,
 * off by default, that saves as it flips (POST /api/budgets/rollover). With it on, a budget card reads
 * "Budget $400 + $45.05 rolled over" and "Rolled over +$45.05 from Aug": its ring, left and status already use the
 * effective cap the server sends (cap; base_cap is the one you set). */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, money, money2, monthShort, monthName, pill, post, HOOKS, toast, reloadBudgets} = X;
  const CARDS = new Map();  // category -> its budgets row, as the cards were drawn (the editor reads it)

  // 2026-10-02: the footer's amount is just `Budget: $X`. The rollover figure moved OUT of the label and into the
  // card's Details panel (budgetNote below), because the whole label measured 246px of a 254px footer at 1280 and
  // could not share the line with Details and Edit. The ring, "left" and status already use the effective cap, so
  // dropping the second number from the label loses no arithmetic — only the explanation, which is what the panel is.
  HOOKS.budgetLabel = (c) => {
    CARDS.set(c.category, c);
    if (!c.rollover) return null;
    return h('span', {}, 'Budget: ', h('b', {class: 'v'}, money(c.base_cap)));
  };

  // The rollover fact as a disclosure: what carried over, from which month, and why the effective cap is higher than
  // the number in the footer. Rendered inside the card's Details panel by app.js.
  HOOKS.budgetNote = (c) => {
    if (!c.rollover) return null;
    const from = monthShort(c.rollover.from);
    return c.rollover.amount > 0
      ? h('div', {}, h('b', {}, `${money2(c.rollover.amount)} rolled over from ${from}.`),
        h('span', {class: 'mut'}, ` It was added to this month’s budget, so you have ${money(c.cap)} to spend in total, not ${money(c.base_cap)}.`))
      : h('div', {}, h('b', {}, `Nothing left over from ${from}.`),
        h('span', {class: 'mut'}, ` Your budget is the ${money(c.base_cap)} you set.`));
  };

  // 2026-10-02: the rollover pill no longer sits in its own reserved band under the figure. It is the same fact as
  // budgetNote, and the user picked "one thing in the header, the rest in the panel" — showing it twice would undo that.
  // Returning null leaves `.budget-extra` empty, and the band collapses on every card alike, so heights stay equal.
  HOOKS.budgetExtra = (c) => null;

  // The cap editor's roll-over row (a settings row in app.js capEditor's sheet): the label and what is happening now on the
  // left, the switch on the right. It saves as it flips; the sheet stays open, so the row repaints from the redrawn data
  // (CARDS is refilled by the redraw) and `onChange` lets the sheet update its own figures. `capped`: the editor's category
  // has a budget (CARDS may still hold its card from before it was removed).
  HOOKS.capEditorExtra = (category, {capped = true, onChange = () => {}} = {}) => {
    const sw = h('button', {class: 'tgl', type: 'button', role: 'switch', 'aria-label': `Roll over what's left in ${category}`}, h('i', {}));
    const state = h('span', {});
    const paint = () => {
      const c = CARDS.get(category);
      const has = Boolean(capped && c && c.cap != null);
      const on = has && Boolean(c.rollover);
      const prev = monthName(c && c.rollover ? c.rollover.from : X.addMonths(X.S.month, -1));
      const cur = monthName(X.S.month);
      const base = c ? (c.base_cap != null ? c.base_cap : c.cap) : null;
      const left = has ? Math.max(Math.round((base - c.last_month) * 100) / 100, 0) : 0;
      state.textContent = !has ? 'Set a budget first; then what’s left each month can carry into the next.'
        : on ? (c.rollover.amount > 0 ? `Adding ${prev}’s ${money2(c.rollover.amount)} to ${cur}` : `On · nothing was left in ${prev}`)
          : left > 0 ? `Off · turn on to add ${prev}’s ${money2(left)} to ${cur}` : `Off · ${cur} starts fresh`;
      sw.setAttribute('aria-checked', String(on));
      sw.disabled = !has;
    };
    paint();
    sw.addEventListener('click', async () => {
      const on = sw.getAttribute('aria-checked') === 'true';
      sw.disabled = true;
      try {
        await post('/api/budgets/rollover', {category, enabled: !on});
        await reloadBudgets();
        paint();
        onChange();
        toast(!on ? ['Rollover on for ', h('b', {}, category), '.'] : ['Rollover off for ', h('b', {}, category), '.'], [], {timeout: 3500});
      } catch (e) {
        sw.disabled = false;
        toast(`Could not save: ${e.message}`, [], {error: true, timeout: 6000});
      }
      if (sw.isConnected && !sw.disabled) sw.focus();   // disabling it while it saved dropped the focus
    });
    return h('div', {class: 'bx-roll'}, h('span', {class: 'grow'}, h('b', {}, 'Roll over what’s left'), state), sw);
  };
});
