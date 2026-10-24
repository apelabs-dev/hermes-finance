/* Goals (TASK-19): the #/goals page, the Overview tile and the New goal sheet. Loaded before app.js; app.js calls the
 * function below with the helpers it builds everything with (window.HF_EXT), before the first page is drawn.
 * Data: GET /api/goals (goals.payload). Nothing is created until Create goal is tapped; an existing goal's sheet saves
 * when it closes (POST /api/goals/edit). Every string from the server goes in as text (h()), never as markup. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, glyph, plural, clamp, sum, money, money2, bigMoney, pct, monthName, monthShort, monthYear, thisMonth,
    addMonths, fmtDate, RGB, OK, WARN, ACC, ibadge, ring, pill, tag, track, api, post, PAGES, S, RENDER, HOOKS, openSheet,
    closeModal, closeBtn, toast, card, small, skeleton, errorCard, pack, parseAmount, acctName, nameMask} = X;

  PAGES.goals = 'Goals';
  const KIND_LABEL = {save: 'Save up', payoff: 'Pay off', monthly: 'Monthly savings'};
  const TRACK_TEXT = {account: 'whole account', earmark: 'part of', manual: 'logged by you'};
  const r2 = (x) => Math.round(x * 100) / 100;
  const pctText = (g) => `${Math.floor(clamp(g.pct || 0, 0, 999))}%`;
  const goalColour = (g) => g.colour || (g.kind === 'payoff' ? WARN : OK);
  const goalRGB = (g) => goalColour(g).slice(1).match(/../g).map((c) => parseInt(c, 16)).join(',');
  const goalIcon = (g) => g.icon || 'piggy-bank';
  let LAST = null;  // the last /api/goals payload the page drew (the sheets read accounts and unassigned from it)

  const whereText = (g) => {
    if (g.kind === 'monthly') return 'Income − spending each month';
    if (g.kind === 'payoff') return g.account ? `Pay off · ${acctName(g.account.label)}` : 'Pay off';
    if (g.track === 'manual') return 'Save up · logged by you';
    if (!g.account) return 'Save up · its account is gone';
    return `Save up · ${g.track === 'earmark' ? `part of ${acctName(g.account.label)}` : acctName(g.account.label)}`;
  };
  // (TASK-20) the card's line: an account's name shortens, its ••last-4 is never cut (nameMask)
  const whereLine = (g) => {
    const a = g.account;
    if (g.kind === 'monthly' || g.track === 'manual' || !a) return h('span', {}, whereText(g));
    return nameMask(a.name, a.mask, {pre: g.kind === 'payoff' ? 'Pay off · ' : g.track === 'earmark' ? 'Save up · part of ' : 'Save up · '});
  };

  // Historical growth without a deadline; otherwise the required monthly saving or Past due.
  function paceLine(g) {
    const p = g.pace || {};
    const trend = p.growth_90d > 0.5 ? `+${money(p.growth_90d)} in the last 90 days` : p.growth_90d < -0.5
      ? `${money(p.growth_90d)} in the last 90 days` : 'No change in the last 90 days';
    if (p.past_due) return [h('b', {class: 'bad'}, 'Past due')];
    if (p.need_per_month != null) {
      return [h('b', {}, `${money(p.need_per_month)} a month to reach it on time`),
        h('span', {class: 'dim'}, ` · ${trend}`)];
    }
    if (p.on_pace_month) {
      return [h('b', {class: 'pos'}, `On pace for ${monthYear(p.on_pace_month)}`),
        h('span', {class: 'dim'}, ` · ${trend}, about ${money(p.per_month)} a month`)];
    }
    return [h('b', {}, 'Not growing yet'), h('span', {class: 'dim'}, ` · ${trend}. Add a month to see what to save.`)];
  }

  function doneLine(g) {
    return h('div', {class: 'gdone'}, h('p', {class: 'gpace'}, h('b', {class: 'pos'}, `100% of ${money(g.target)} saved`),
      h('span', {class: 'dim'}, ' · you reached it')),
    h('div', {class: 'row'},
      h('button', {class: 'btn sm ghost', type: 'button', onclick: (e) => { e.stopPropagation(); archive(g); }}, icon('archive'), 'Archive'),
      h('button', {class: 'btn sm', type: 'button', onclick: (e) => { e.stopPropagation(); editSheet(g, 'target'); }}, icon('target'), 'Raise target')));
  }

  function goalCard(g) {
    const iconName = goalIcon(g);
    const rgb = goalRGB(g);
    const color = goalColour(g);
    const ratio = g.target ? g.progress / g.target : 0;
    let fig;
    if (g.kind === 'payoff') {
      fig = h('div', {class: 'grow'}, h('div', {class: 'big s v'}, money(g.progress)),
        h('div', {class: 'mut'}, `paid off of ${money(g.target)} · `, h('span', {}, `${money(g.remaining || 0)} left`)));
    } else {
      fig = h('div', {class: 'grow'}, h('div', {class: 'big s v'}, money2(g.progress)),
        h('div', {class: 'mut'}, `of ${money(g.target)}${g.target_month ? ` by ${monthYear(g.target_month)}` : ''} · `,
          h('span', {}, `${money(g.left || 0)} left`)));
    }
    let foot;
    if (g.missing_account) {
      foot = h('p', {class: 'gpace'}, h('b', {class: 'bad'}, 'Its account isn’t there any more'),
        h('span', {class: 'dim'}, ' · archive this goal, or connect the account again'));
    } else if (g.done && g.kind === 'save') {
      foot = doneLine(g);
    } else if (g.kind === 'payoff') {
      const po = g.payoff;
      foot = h('p', {class: 'gpace'}, g.done ? h('b', {class: 'pos'}, 'Paid off') : po && po.payoff_month
        ? [h('b', {class: 'pos'}, `Paid off ${monthYear(po.payoff_month)}`), po.monthly_payment
          ? h('span', {class: 'dim'}, ` · ${money2(po.monthly_payment)} a month, ${plural(po.payments_left, 'payment')} left`) : null]
        : [h('b', {}, `${money(g.remaining || 0)} to go`), h('span', {class: 'dim'}, ' · add a monthly payment on Loans & debt to see when')]);
    } else {
      foot = h('p', {class: 'gpace'}, paceLine(g));
    }
    const el = h('section', {class: `card goal${g.short ? ' short' : ''}`, dataset: {goal: String(g.id)}, tabindex: '0', role: 'button',
      'aria-label': `${g.name}: ${pctText(g)} · open to change it`},
    h('div', {class: 'hd'}, ibadge(iconName, rgb), h('span', {class: 'grow'}, h('b', {}, g.name), whereLine(g))),
    h('div', {class: 'gfig'}, ring(ratio, color, 78, 8, h('b', {class: 'v', style: {'font-size': '16px'}}, pctText(g))), fig),
    g.short ? h('div', {class: 'gshort'}, pill('bad sm', 'alert', `Short by ${money2(g.short.amount)}`),
      h('p', {}, `${g.account ? acctName(g.account.label) : 'Its account'} now holds less than its goals set aside: `
        + `${money2(g.short.covered)} of this goal's ${money2(g.progress)} is covered.`)) : null,
    foot);
    const open = () => editSheet(g);
    el.addEventListener('click', (e) => { if (!e.target.closest('button')) open(); });
    el.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === el) { e.preventDefault(); open(); } });
    return el;
  }

  function suggestionCard(s) {
    return h('section', {class: 'card goal gempty', dataset: {suggest: s.id}},
      h('div', {class: 'hd'}, ibadge('piggy', RGB.ok), h('span', {class: 'grow'}, h('b', {}, acctName(s.name)),
        h('span', {}, `Save up · ${s.vault ? 'vault' : 'savings'}${s.mask ? ` ••${s.mask}` : ''}`))),
      h('div', {class: 'gnow'}, h('span', {class: 'mut'}, 'In it now'), h('b', {class: 'v'}, money2(s.balance))),
      h('div', {class: 'row'},
        h('button', {class: 'btn sm', type: 'button', onclick: () => newGoalSheet('save', {name: acctName(s.name), track: 'account', account_id: s.id})},
          icon('target'), 'Set a target'),
        h('button', {class: 'btn sm ghost', type: 'button', onclick: () => dismiss(s)}, 'Not a goal')));
  }

  function templateCard(kind) {
    const T = {save: ['piggy', RGB.ok, 'Save up', 'Save toward a target'],
      payoff: ['handcoins', RGB.warn, 'Pay off', 'Count down a loan or card'],
      monthly: ['bars', RGB.acc, 'Monthly savings', 'Save a set amount monthly']}[kind];
    return h('button', {class: 'card goal gtpl', type: 'button', dataset: {template: kind}, onclick: () => newGoalSheet(kind)},
      h('div', {class: 'hd'}, ibadge(T[0], T[1]), h('span', {class: 'grow'}, h('b', {}, T[2]), h('span', {}, 'Template'))),
      h('p', {class: 'sub'}, T[3]), h('span', {class: 'lnk'}, 'Start', icon('chevr')));
  }

  function savingsBars(months, target) {  // the last 6 months' income − spending, the target dashed
    const H = 118;
    const maxPos = Math.max(target, ...months.map((f) => f.net), 1);
    const maxNeg = Math.max(0, ...months.map((f) => -f.net));
    const scale = H / (maxPos + maxNeg);
    const zero = maxPos * scale;
    return h('div', {class: 'msb', role: 'img', 'aria-label': `Saved each month: ${months.map((f) => `${monthShort(f.month)} ${money(f.net)}`).join(', ')}; target ${money(target)}`},
      h('div', {class: 'msplot', style: {height: `${H}px`}},
        h('i', {class: 'mszero', style: {top: `${zero}px`}}),
        h('i', {class: 'mstgt', style: {top: `${zero - target * scale}px`}}),
        h('span', {class: 'mstl', style: {top: `${Math.max(zero - target * scale - 20, 0)}px`}}, `Target ${money(target)}`),
        months.map((f) => {
          const hgt = Math.max(Math.abs(f.net) * scale, 2);
          return h('span', {class: `msc${f.coverage === 'partial' ? ' part' : ''}`},
            h('i', {class: f.net >= 0 ? (f.net >= target ? 'hit' : 'pos') : 'neg',
              style: f.net >= 0 ? {top: `${zero - hgt}px`, height: `${hgt}px`} : {top: `${zero}px`, height: `${hgt}px`}}));
        })),
      h('div', {class: 'mslbl'}, months.map((f) => h('span', {class: f.coverage === 'partial' ? 'part' : ''},
        h('b', {}, monthShort(f.month), f.is_current ? h('span', {class: 'sofar'}, ' so far') : null),
        h('span', {class: f.net < 0 ? 'bad' : ''}, X.kf(f.net))))));
  }

  function monthlyCard(g, today) {
    const dim = new Date(Number(g.month.slice(0, 4)), Number(g.month.slice(5, 7)), 0).getDate();
    const daysLeft = dim - Number(today.slice(8, 10));
    const togo = r2(g.target - g.progress);
    const partial = g.months.some((m) => m.coverage === 'partial');
    const el = card({cls: 's2 mscard', title: g.name, icon: goalIcon(g), rgb: goalRGB(g), extra: small('income − spending')},
      h('div', {class: 'msrow'},
        ring(g.target ? g.progress / g.target : 0, goalColour(g), 96, 9, h('b', {class: 'v', style: {'font-size': '19px'}}, pctText(g)), h('small', {}, `of ${money(g.target)}`)),
        h('div', {class: 'grow'}, bigMoney(g.progress, 'md'),
          h('div', {class: 'sub'}, `saved in ${monthName(g.month)} so far · `,
            togo > 0 ? [h('b', {}, `${money2(togo)} to go`), ` with ${plural(daysLeft, 'day')} left`] : h('b', {class: 'pos'}, 'target reached')),
          h('div', {class: 'sub dim'}, `Income ${money2(g.income)} − spending ${money2(g.spending)}`))),
      savingsBars(g.months, g.target),
      partial ? h('div', {class: 'sub help-line'}, h('span', {}, 'Faded months: incomplete history'),
        X.help('Incomplete goal history', 'Not every bank was linked for the faded months. Their totals may be incomplete.', {key: 'goal-history'})) : null);
    el.dataset.goal = String(g.id);
    el.classList.add('goalm');
    el.style.setProperty('--goal-colour', goalColour(g));
    el.addEventListener('click', (e) => { if (!e.target.closest('button, a')) editSheet(g); });
    return el;
  }

  function summaryCard(d) {
    const saves = d.goals.filter((g) => g.kind === 'save' && g.target);
    const t = d.totals;
    return card({cls: 'gsum', title: 'Saving toward', icon: 'target', rgb: RGB.acc, extra: small(plural(saves.length, 'goal'))},
      saves.length ? [bigMoney(t.saved, 's'),
        h('div', {class: 'sub'}, `of ${money(t.saving_target)} · `, h('b', {}, `${Math.round(t.pct || 0)}%`)),
        h('div', {class: 'stk', role: 'img', 'aria-label': saves.map((g) => `${g.name} ${money(Math.min(g.progress, g.target))}`).join(', ')},
          saves.map((g) => h('i', {style: {width: `${(Math.min(Math.max(g.progress, 0), g.target) / t.saving_target) * 100}%`, background: goalColour(g)}})))]
        : h('p', {class: 'sub'}, 'No Save up goal yet. A target turns a savings account into progress you can see.'),
      h('button', {class: 'ghost-row', type: 'button', dataset: {add: 'goal'}, 'aria-label': 'Add a new goal',
        onclick: () => newGoalSheet('save')},
        ibadge('plus', RGB.acc, 's'),
        h('span', {class: 'grow'}, h('b', {}, 'Add a new goal'), h('span', {}, 'Save up, pay off, or monthly')),
        icon('chevr')));
  }

  async function renderGoals(tk) {
    fill(X.$('page'), skeleton(['card s2', 'card', 'card', 'card', 'card']));
    const d = await api('/api/goals');
    if (tk !== S.token) return;
    LAST = d;
    const page = X.$('page');
    const others = d.goals.filter((g) => g.kind !== 'monthly');
    if (!d.goals.length) {
      // hotfix 2026-09-29: the savings accounts and the 3 templates are 2 different kinds of card; packed into one
      // masonry grid, 5 accounts + 3 templates left the templates split across columns and a stretched hole under
      // an account card (desktop, 3 columns). Two plain grids: accounts in rows, then the templates as one row.
      const accts = d.suggestions.length ? h('div', {class: 'grid gsugs', dataset: {uniform: ''}}, d.suggestions.map(suggestionCard)) : null;
      const tpls = h('div', {class: 'grid gtpls', dataset: {uniform: ''}}, ['save', 'payoff', 'monthly'].map(templateCard));
      fill(page, accts ? h('h2', {class: 'sech2'}, 'Your savings accounts') : null, accts,
        h('h2', {class: 'sech2'}, accts ? 'Or start from a template' : 'Start from a template'), tpls);
      return;
    }
    const sum = summaryCard(d);
    sum.dataset.flex = '';  // beside Monthly savings (3 columns), or its own full row (2 columns)
    const top = h('div', {class: 'grid'}, d.monthly ? monthlyCard(d.monthly, d.today) : null, sum);
    const grid = h('div', {class: 'grid'}, others.map(goalCard), d.suggestions.map(suggestionCard));
    fill(page, top, others.length || d.suggestions.length ? h('h2', {class: 'sech2'}, 'Your goals') : null,
      others.length || d.suggestions.length ? grid : null,
      d.archived_count ? h('p', {class: 'garch'}, `${plural(d.archived_count, 'archived goal')} kept · `,
        h('button', {class: 'lnk', type: 'button', onclick: () => showArchived()}, 'Show')) : null);
    pack(top);
    if (others.length || d.suggestions.length) pack(grid);
  }
  RENDER.goals = renderGoals;

  async function showArchived() {
    const d = await api('/api/goals', {archived: '1'});
    openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Archived goals'), closeBtn()),
      h('p', {}, 'Kept as they were. Bring one back to follow it again.'),
      h('div', {class: 'opts'}, (d.archived || []).map((g) => h('div', {class: 'li'}, ibadge(goalIcon(g), goalRGB(g), 's'),
        h('span', {class: 'grow'}, h('b', {}, g.name), h('span', {}, `${KIND_LABEL[g.kind]} · ${pctText(g)}`)),
        h('button', {class: 'btn sm ghost', type: 'button', onclick: async () => {
          try {
            await post('/api/goals/edit', {id: g.id, archived: false});
            closeModal();
            X.route({keepToast: true});
            toast(['Brought back ', h('b', {}, g.name), '.'], [], {timeout: 3500});
          } catch (e) {
            toast(`Could not bring it back: ${e.message}`, [], {error: true, timeout: 6000});
          }
        }}, 'Bring back'))))], {narrow: true});
  }

  async function archive(g) {
    try {
      await post('/api/goals/edit', {id: g.id, archived: true});
      toast(['Archived ', h('b', {}, g.name), '. It’s kept; nothing else changes.'], [], {timeout: 4000});
      X.route({keepToast: true});
    } catch (e) {
      toast(`Could not archive it: ${e.message}`, [], {error: true, timeout: 6000});
    }
  }

  async function dismiss(s) {
    try {
      await post('/api/goals/dismiss', {account_id: s.id});
      X.route({keepToast: true});
      toast([h('b', {}, s.name), ' won’t be suggested again.'], [], {timeout: 3500});
    } catch (e) {
      toast(`Could not save that: ${e.message}`, [], {error: true, timeout: 6000});
    }
  }

  // ---- the sheets --------------------------------------------------------------------------------------------------
  const moneyIn = (label, value, id) => {
    const input = h('input', {type: 'text', inputmode: 'decimal', autocomplete: 'off', id, 'aria-label': label,
      value: value != null ? X.money2(value).replace(/^\$/, '').replace(/^−\$/, '-') : ''});
    return [h('div', {class: 'inp'}, h('span', {class: 'pre', 'aria-hidden': 'true'}, '$'), input), input];
  };
  const fld = (label, control, opts = {}) => h('div', {class: `fld${opts.half ? ' h' : ''}`, dataset: opts.key ? {f: opts.key} : null},
    h('label', {for: opts.for}, label, opts.optional ? h('em', {}, 'optional') : null), control, h('p', {class: 'ferr', role: 'alert'}));
  const choice = (on, iconName, rgb, title, text) => h('button', {class: `choice${on ? ' on' : ''}`, type: 'button', role: 'radio',
    'aria-checked': String(on)}, ibadge(iconName, rgb), h('span', {class: 'grow'}, h('b', {}, title), h('span', {}, text)), h('span', {class: 'rad'}));
  const accountOpt = (a, selected) => h('option', {value: a.id, selected}, `${acctName(a.label)} · ${money2(a.balance)}`);
  const monthInput = (value, id) => h('input', {type: 'month', id, 'aria-label': 'By', value: value || '', min: thisMonth()});

  // The API owns the exact whitelist. Buttons expose selection independently of colour.
  function appearancePicker(d, state) {
    const preview = h('div', {class: 'gappearance-preview', 'aria-live': 'polite'});
    const icons = h('div', {class: 'gicons', role: 'group', 'aria-label': 'Goal icon'});
    const colours = h('div', {class: 'gcolours', role: 'group', 'aria-label': 'Goal colour'});
    const colourNames = ['Mint', 'Blue', 'Cyan', 'Purple', 'Amber', 'Pink', 'Coral', 'Slate'];
    const draw = () => {
      fill(preview, ibadge(goalIcon(state), goalRGB(state)), h('span', {}, 'Your goal’s look'));
      icons.querySelectorAll('button').forEach((b) => {
        b.setAttribute('aria-pressed', String(b.dataset.icon === state.icon));
        b.style.setProperty('--rgb', goalRGB(state));
      });
      colours.querySelectorAll('button').forEach((b) => {
        const selected = b.dataset.colour === state.colour;
        b.setAttribute('aria-pressed', String(selected));
        fill(b, selected ? icon('check') : null);
      });
    };
    d.appearance.icons.forEach((name) => icons.append(h('button', {
      class: 'cb gicon', type: 'button', dataset: {icon: name}, 'aria-label': name.replaceAll('-', ' '),
      title: name.replaceAll('-', ' '), onclick: () => { state.icon = name; draw(); }
    }, glyph(name))));
    d.appearance.colours.forEach((colour, i) => colours.append(h('button', {
      class: 'gcolour', type: 'button', dataset: {colour}, 'aria-label': colourNames[i], title: colourNames[i],
      style: {'--swatch': colour}, onclick: () => { state.colour = colour; draw(); }
    })));
    draw();
    return h('div', {class: 'gappearance'}, preview,
      fld('Icon', icons, {key: 'icon'}), fld('Colour', colours, {key: 'colour'}));
  }

  function showErrors(sheet, e) {
    const f = e && e.field;
    const slot = f && sheet.querySelector(`[data-f="${f}"] .ferr`);
    if (slot) slot.textContent = e.message;
    else toast(`Could not save: ${e.message}`, [], {error: true, timeout: 6000});
  }

  async function loadGoals() {
    if (!LAST) LAST = await api('/api/goals');
    return LAST;
  }

  async function newGoalSheet(kind = 'save', prefill = {}) {
    const d = await loadGoals().catch(() => null);
    if (!d) {
      toast('Could not load your accounts. Try again.', [], {error: true, timeout: 5000});
      return;
    }
    const sheet = X.$('sheet');
    const st = {kind, track: prefill.track || (d.accounts.length ? 'account' : 'manual'), account: prefill.account_id || null};
    const appearance = {icon: kind === 'payoff' ? 'target' : 'piggy-bank', colour: kind === 'payoff' ? WARN : OK};
    const picker = appearancePicker(d, appearance);
    const body = h('div', {class: 'gform'});
    const create = h('button', {class: 'btn w', type: 'button'}, 'Create goal');
    const seg = h('div', {class: 'seg tseg', role: 'group', 'aria-label': 'Template'});
    const inputs = {};
    const draw = () => {
      seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.k === st.kind)));
      create.disabled = false;
      if (st.kind === 'monthly') {
        const [box, input] = moneyIn('Monthly target', null, 'g-target');
        inputs.target = input;
        fill(body, h('div', {class: 'flds'}, fld('Save each month', box, {key: 'target', for: 'g-target'})),
          h('p', {class: 'hint'}, `Income minus spending, the same numbers as the Overview's cash flow. This month so far: ${money2(d.cashflow[d.cashflow.length - 1].net)}.`));
        return;
      }
      if (st.kind === 'payoff') {
        if (!d.debts.length) {
          fill(body, h('p', {class: 'hint'}, 'No loan or card with a balance yet. Add one on Accounts first.'));
          create.disabled = true;
          return;
        }
        const sel = h('select', {id: 'g-debt', 'aria-label': 'Loan or card'}, d.debts.map((a) => accountOpt(a, a.id === st.account)));
        inputs.account = sel;
        fill(body, h('div', {class: 'flds'}, fld('Loan or card', h('div', {class: 'inp sel'}, sel), {key: 'account_id', for: 'g-debt'})),
          h('p', {class: 'hint'}, 'It counts down from what it owes today; the Loans & debt page’s payment and payoff date show on the goal.'));
        return;
      }
      const name = h('input', {id: 'g-name', 'aria-label': 'Name', maxlength: '60', value: prefill.name || '', placeholder: 'e.g. New laptop'});
      const [tbox, target] = moneyIn('Target', null, 'g-target');
      const by = monthInput(null, 'g-by');
      Object.assign(inputs, {name, target, by});
      const slot = h('div', {class: 'trk'});
      const drawTrack = () => {
        const opts = [
          ['account', 'landmark', RGB.ok, 'A whole account', 'Its balance is the progress, like a savings account or a vault.'],
          ['earmark', 'split', RGB.acc, 'Part of an account', 'An amount set aside inside a bigger account.'],
          ['manual', 'pencil', RGB.warn, 'I’ll log it myself', 'Add entries as you save; the goal adds them up.'],
        ];
        fill(slot, h('div', {class: 'choices sm', role: 'radiogroup', 'aria-label': 'Track progress from'}, opts.map(([k, ic, rgb, title, text]) => {
          const disabled = k !== 'manual' && !d.accounts.length;
          const b = choice(k === st.track, ic, rgb, title, disabled ? 'Add a savings or checking account first.' : text);
          b.disabled = disabled;
          b.addEventListener('click', () => { st.track = k; drawTrack(); });
          return [b, k === st.track ? trackDetail(k) : null];
        })));
      };
      const trackDetail = (k) => {
        if (k === 'manual') return h('div', {class: 'trkd'}, h('p', {class: 'hint'}, 'Add your first entry after you create it: + Add on the goal.'));
        const usable = d.accounts.filter((a) => (k === 'account' ? !a.whole_goal && !a.earmarked : !a.whole_goal));
        if (!usable.length) return h('div', {class: 'trkd'}, h('p', {class: 'hint'}, 'Every account already has a goal that follows it whole.'));
        if (!usable.some((a) => a.id === st.account)) st.account = usable[0].id;
        const sel = h('select', {id: 'g-acct', 'aria-label': 'Account'}, usable.map((a) => accountOpt(a, a.id === st.account)));
        inputs.account = sel;
        if (k === 'account') {
          sel.addEventListener('change', () => { st.account = sel.value; });
          return h('div', {class: 'trkd'}, fld('Account', h('div', {class: 'inp sel'}, sel), {key: 'account_id', for: 'g-acct'}));
        }
        const [abox, amt] = moneyIn('Amount to set aside', null, 'g-earmark');
        inputs.earmark = amt;
        const unas = h('b', {class: 'v'});
        const unasLbl = h('span', {});
        const left = h('p', {class: 'hint gleft', 'aria-live': 'polite'});
        const upd = () => {
          const a = d.accounts.find((x) => x.id === sel.value) || usable[0];
          st.account = a.id;
          unasLbl.textContent = `Unassigned in ${acctName(a.name) || 'this account'}`;
          unas.textContent = money2(a.unassigned);
          const n = amt.value.trim() === '' ? 0 : parseAmount(amt.value);
          const bad = n == null || Number.isNaN(n) || n < 0 || Math.round(n * 100) > Math.round(a.unassigned * 100);
          amt.toggleAttribute('aria-invalid', bad);
          create.disabled = bad;
          left.className = `${bad ? 'ferr' : 'hint'} gleft`;
          left.textContent = bad ? `Only ${money2(a.unassigned)} is unassigned in ${acctName(a.name) || 'this account'}. Set aside that much or less.`
            : `After this, ${money2(a.unassigned - (n || 0))} stays unassigned.`;
        };
        sel.addEventListener('change', upd);
        amt.addEventListener('input', upd);
        setTimeout(upd);
        return h('div', {class: 'trkd'},
          h('div', {class: 'flds'}, fld('Account', h('div', {class: 'inp sel'}, sel), {key: 'account_id', for: 'g-acct', half: true}),
            fld('Amount to set aside', abox, {key: 'earmark', for: 'g-earmark', half: true})),
          h('div', {class: 'unas'}, unasLbl, unas), left);
      };
      drawTrack();
      fill(body, h('div', {class: 'flds'},
        fld('Name', h('div', {class: 'inp'}, name), {key: 'name', for: 'g-name'}),
        fld('Target', tbox, {key: 'target', for: 'g-target', half: true}),
        fld('By', h('div', {class: 'inp'}, by), {key: 'target_month', for: 'g-by', half: true, optional: true})),
      h('div', {class: 'gh'}, 'Track progress from', h('span', {class: 'ln'})), slot);
    };
    [['save', 'Save up', 'piggy'], ['payoff', 'Pay off', 'handcoins'], ['monthly', 'Monthly savings', 'bars']].forEach(([k, label, ic]) => {
      seg.append(h('button', {type: 'button', dataset: {k}, 'aria-pressed': String(k === st.kind), onclick: () => { st.kind = k; draw(); }}, icon(ic), label));
    });
    create.addEventListener('click', async () => {
      sheet.querySelectorAll('.ferr').forEach((x) => { x.textContent = ''; });
      const num = (input) => (input && input.value.trim() !== '' ? parseAmount(input.value) : null);
      const data = {kind: st.kind, ...appearance};
      if (st.kind === 'monthly') data.target = num(inputs.target);
      else if (st.kind === 'payoff') data.account_id = inputs.account ? inputs.account.value : null;
      else {
        Object.assign(data, {name: inputs.name.value, target: num(inputs.target), target_month: inputs.by.value || null, track: st.track});
        if (st.track !== 'manual') data.account_id = st.account;
        if (st.track === 'earmark') data.earmark = num(inputs.earmark) || 0;
      }
      if (data.target !== undefined && (data.target == null || Number.isNaN(data.target))) {
        showErrors(sheet, {field: 'target', message: 'Enter the target, like 2,000.'});
        return;
      }
      create.disabled = true;
      try {
        const g = await post('/api/goals', data);
        closeModal();
        LAST = null;
        if (S.page === 'goals') X.route({keepToast: true});
        toast(['Goal made: ', h('b', {}, g.name), '. Nothing else changes.'], [], {timeout: 4000});
      } catch (e) {
        create.disabled = false;
        showErrors(sheet, e);
      }
    });
    draw();
    openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, prefill.name ? `Set a target for ${prefill.name}` : 'New goal'), closeBtn()),
      h('p', {}, 'Nothing is made until you tap Create goal.'), seg, body, picker, h('div', {class: 'row'}, create)], {cls: 'gsheet'});
  }

  // An existing goal: name, target, month and (part of an account) the amount set aside save when the sheet closes;
  // entries (logged goals) are added right away; Archive asks nothing and keeps it.
  async function editSheet(g, focus) {
    const d = await loadGoals().catch(() => null);
    const sheet = X.$('sheet');
    const inputs = {};
    const kids = [h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, g.name), closeBtn()),
      h('p', {}, `${KIND_LABEL[g.kind]} · ${pctText(g)}${g.target ? ` of ${money(g.target)}` : ''}`)];
    const flds = h('div', {class: 'flds'});
    const name = h('input', {id: 'ge-name', 'aria-label': 'Name', maxlength: '60', value: g.name});
    inputs.name = name;
    flds.append(fld('Name', h('div', {class: 'inp'}, name), {key: 'name', for: 'ge-name'}));
    if (g.kind !== 'payoff') {
      const [tbox, target] = moneyIn(g.kind === 'monthly' ? 'Save each month' : 'Target', g.target, 'ge-target');
      inputs.target = target;
      flds.append(fld(g.kind === 'monthly' ? 'Save each month' : 'Target', tbox, {key: 'target', for: 'ge-target', half: g.kind === 'save'}));
    }
    if (g.kind === 'save') {
      const by = monthInput(g.target_month, 'ge-by');
      inputs.by = by;
      flds.append(fld('By', h('div', {class: 'inp'}, by), {key: 'target_month', for: 'ge-by', half: true, optional: true}));
    }
    const appearance = {icon: goalIcon(g), colour: goalColour(g)};
    kids.push(flds, d ? appearancePicker(d, appearance) : null);
    if (g.track === 'earmark' && d) {
      const a = d.accounts.find((x) => x.id === g.account_id);
      const free = a ? r2(a.unassigned + (g.earmark || 0)) : 0;
      const [abox, amt] = moneyIn('Amount set aside', g.earmark, 'ge-earmark');
      inputs.earmark = amt;
      const hint = h('p', {class: 'hint', 'aria-live': 'polite'});
      const upd = () => {
        const n = amt.value.trim() === '' ? 0 : parseAmount(amt.value);
        const bad = n == null || Number.isNaN(n) || n < 0 || (Math.round(n * 100) > Math.round(free * 100) && n > (g.earmark || 0));
        amt.toggleAttribute('aria-invalid', bad);
        hint.className = bad ? 'ferr' : 'hint';
        hint.textContent = bad ? `At most ${money2(free)} can be set aside in ${a ? acctName(a.name) : 'this account'}.` : `Up to ${money2(free)} available in ${a ? acctName(a.label) : 'this account'}.`;
      };
      amt.addEventListener('input', upd);
      upd();
      kids.push(h('div', {class: 'flds'}, fld('Amount set aside', abox, {key: 'earmark', for: 'ge-earmark'})), hint);
    }
    if (g.track === 'manual') {
      const [ebox, eamt] = moneyIn('Amount', null, 'ge-entry');
      const note = h('input', {id: 'ge-note', 'aria-label': 'Note', maxlength: '200', placeholder: 'e.g. Paycheck'});
      const addEntry = async (sign) => {
        const n = parseAmount(eamt.value);
        if (n == null || Number.isNaN(n) || n <= 0) {
          showErrors(sheet, {field: 'amount', message: 'Enter an amount, like 50.'});
          return;
        }
        try {
          const upd = await post('/api/goals/entry', {id: g.id, amount: sign * n, note: note.value || null});
          LAST = null;
          saveOnClose.done = true;
          closeModal();
          editSheet(upd);
          if (S.page === 'goals') X.route({keepToast: true});
          toast(sign > 0 ? `Added ${money2(n)}.` : `Took out ${money2(n)}.`, [], {timeout: 3000});
        } catch (e) {
          showErrors(sheet, e);
        }
      };
      kids.push(h('div', {class: 'gh'}, 'Entries', h('span', {class: 'ln'})),
        h('div', {class: 'flds'}, fld('Amount', ebox, {key: 'amount', for: 'ge-entry', half: true}),
          fld('Note', h('div', {class: 'inp'}, note), {key: 'note', for: 'ge-note', half: true, optional: true})),
        h('div', {class: 'row gentry'}, h('button', {class: 'btn sm', type: 'button', onclick: () => addEntry(1)}, icon('plus'), 'Add'),
          h('button', {class: 'btn sm ghost', type: 'button', onclick: () => addEntry(-1)}, 'Take out')),
        g.entries.length ? h('div', {class: 'gents'}, g.entries.map((e) => h('div', {class: 'li'},
          h('span', {class: 'grow'}, h('b', {}, fmtDate(e.date, true)), h('span', {}, e.note || (e.amount > 0 ? 'Added' : 'Taken out'))),
          h('span', {class: `amt v${e.amount < 0 ? ' bad' : ''}`}, `${e.amount < 0 ? '−' : '+'}${money2(Math.abs(e.amount))}`))))
          : h('p', {class: 'hint'}, 'No entries yet.'));
    }
    kids.push(h('div', {class: 'row'}, h('button', {class: 'btn sm ghost', type: 'button', onclick: () => { saveOnClose.done = true; closeModal(); archive(g); }},
      icon('archive'), 'Archive')), h('p', {class: 'savenote'}, icon('check'), 'Saves when you close'));
    const saveOnClose = async () => {
      if (saveOnClose.done) return;
      const data = {};
      if (appearance.icon !== g.icon) data.icon = appearance.icon;
      if (appearance.colour !== g.colour) data.colour = appearance.colour;
      if (inputs.name.value.trim() && inputs.name.value.trim() !== g.name) data.name = inputs.name.value;
      if (inputs.target) {
        const n = parseAmount(inputs.target.value);
        if (n != null && !Number.isNaN(n) && Math.round(n * 100) !== Math.round((g.target || 0) * 100)) data.target = n;
      }
      if (inputs.by && (inputs.by.value || null) !== (g.target_month || null)) data.target_month = inputs.by.value || null;
      if (inputs.earmark) {
        const n = inputs.earmark.value.trim() === '' ? 0 : parseAmount(inputs.earmark.value);
        if (n != null && !Number.isNaN(n) && Math.round(n * 100) !== Math.round((g.earmark || 0) * 100)) data.earmark = n;
      }
      if (!Object.keys(data).length) return;
      try {
        await post('/api/goals/edit', {id: g.id, ...data});
        LAST = null;
        toast(['Saved ', h('b', {}, data.name || g.name), '.'], [], {timeout: 3000});
        X.route({keepToast: true});
      } catch (e) {
        toast(`Not saved: ${e.message}`, [], {error: true, timeout: 7000});
      }
    };
    openSheet(kids, {cls: 'gsheet', onClose: saveOnClose, focus: focus === 'target' ? inputs.target : undefined});
  }

  // ---- the Overview tile ------------------------------------------------------------------------------------------
  HOOKS.tiles = (d) => {
    const g = d.goals || {};
    const m = g.monthly;
    const top = g.top;
    const cur = d.cashflow.months[d.cashflow.months.length - 1];  // the month the Overview shows: its income − spending
    const hero = m ? cur.net : top ? top.progress : cur.net;
    const ratio = m ? (m.target ? cur.net / m.target : 0) : top ? top.pct / 100 : null;
    const mon = monthShort(cur.month);
    const bar = ratio != null ? track(ratio, goalColour(m || top || {}),
      m ? `Monthly savings: ${Math.round(ratio * 100)}% of ${money(m.target)}` : `${top.name}: ${Math.floor(top.pct)}%`)
      : h('div', {class: 'track', style: {opacity: '.5'}});
    const detail = m ? [h('span', {}, `Saved in ${mon} · `, h('b', {}, `${X.ringPctOf(Math.max(cur.net, 0), m.target)}% of ${money(m.target)}`)),
      top ? h('span', {class: 'gtop-goal'}, ibadge(goalIcon(top), goalRGB(top), 'xs'), `${top.name} ${Math.floor(top.pct)}%`) : null]
      : top ? [h('span', {}, h('b', {}, top.name), ` ${Math.floor(top.pct)}% of ${money(top.target)}`)]
        : [h('span', {}, `Saved in ${mon} so far`), h('span', {}, 'No goals yet · set one')];
    return card({cls: 'tile gtile', href: '#/goals', title: 'Goals', icon: goalIcon(m || top || {}), rgb: goalRGB(m || top || {}),
      extra: h('span', {class: 'lnk end', 'aria-hidden': 'true'}, icon('chevr'))},
    bigMoney(hero), bar, h('div', {class: 'tl2'}, detail));
  };
});
