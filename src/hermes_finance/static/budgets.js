/* The Budgets page body (polish P2A). Loaded before app.js (window.HF_EXT); app.js draws the summary card and the cap editor,
 * and asks this file (HOOKS.budgetsBody) for everything under the summary:
 *   - a table of the budgets grouped by category group (db.CATEGORY_GROUPS, from /api/budgets `groups`): a group row
 *     with its Planned / Spent / Left and a chevron that folds it, then one row per capped category: its name (a link to
 *     its transactions, and a Details disclosure), a 3px line, Planned (the edit control), Spent and Left as a tinted pill;
 *   - "No budget yet": the categories without a cap, those with spending this month first, each with its suggested budget
 *     and a Use button, and Set; the ones with nothing spent wait behind one "Show N more" button.
 * Every figure is the one the cards had: the line and the pill use spent + the bills still due against the effective cap
 * (rollover included), the same sum as the rings (ringPctOf, capStatus).
 *
 * Polish P2e, group budgets (groupbudget.py): a group's Planned is an edit control too. A group with a budget of its own
 * shows it as Planned, every category in it as Spent (the caps inside keep their rows, the uncapped ones with spending get
 * a plain Spent row) and, when its caps add up to more, a short warn note. A group without one: Planned is its caps' sum,
 * and (group roll-up, 2026-10-22) Spent / Left take every category in it: its uncapped ones with spending or a bill due
 * get a Spent row under the group with Set in Planned (they leave No budget yet); a group with no cap stays there whole.
 * Above the table, while a group with caps has
 * no budget and wasn't kept, "Move N budgets to groups" opens the one-time review (a sheet: per group, Group budget or
 * Keep category budgets). "Not now" hides it until next month, on this device. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, ibadge, badge, gbadge, money, money2, plural, clamp, capStatus, ringPctOf, help, helpBills, hashFor, toast, card,
    applyCap, capEditor, monthName, monthShort, monthLabel, addMonths, rgbOf, catColor, HOOKS, RGB, GROUP_ICON, post, reloadBudgets,
    openSheet, closeModal, closeBtn, parseAmount, thisMonth, CUR} = X;

  // ---- what this device remembers: the folded groups and the move banner's "Not now" (localStorage, like hf.networth.mode) ----
  const SHUT_KEY = 'hf.budgets.shut';
  const MOVE_KEY = 'hf.budgets.move';   // the month "Not now" was tapped in: the banner stays away for the rest of it
  const read = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const write = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: kept until the page reloads */ } };
  const SHUT = (() => {
    try {
      const v = JSON.parse(read(SHUT_KEY) || '[]');
      const names = Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
      return new Set(names.map((x) => (x === 'Other' ? 'Misc' : x)));   // v19: the group Other is Misc
    } catch (e) { return new Set(); }
  })();

  const REDUCED = '(prefers-reduced-motion: reduce)';
  const failed = (e) => toast(`Could not save: ${e.message}`, [], {error: true, timeout: 6000});
  const cents = (n) => Math.round(n * 100) / 100;

  // ---- one capped category's numbers: the same as the cards had (the effective cap, the bills still due) ----
  function figures(c) {
    const due = c.due || 0;
    const committed = c.spend + due;
    const left = cents(c.cap - c.spend - due);
    const pct = ringPctOf(committed, c.cap);
    const part = (x) => (c.cap > 0 ? x / c.cap : x > 0 ? 1 : 0);
    const spentW = clamp(part(c.spend), 0, 1);
    return {due, left, pct, status: capStatus(committed, c.cap), spentW, dueW: clamp(part(committed), 0, 1) - spentW};
  }
  const TONE = {ok: '', warn: ' w', over: ' b'};

  // The 3px line under the name: spent in solid, the bills still due after it in a lighter shade; past the cap it's full.
  function hairline(c, f) {
    const aria = f.due > 0 ? `${f.pct}% of budget with bills still due: ${money(c.spend)} spent, ${money(f.due)} due, of ${money(c.cap)}`
      : `${f.pct}% of budget used: ${money(c.spend)} spent of ${money(c.cap)}`;
    return h('div', {class: 'hair', role: 'img', 'aria-label': aria, dataset: {ringPct: String(f.pct), ringStatus: f.status}},
      h('i', {class: TONE[f.status].trim(), style: {width: `${(f.spentW * 100).toFixed(1)}%`}}),
      f.dueW > 0.001 ? h('i', {class: `d ${TONE[f.status].trim()}`.trim(), style: {width: `${(f.dueW * 100).toFixed(1)}%`}}) : null);
  }

  // The last six months as bars (spent, the current month so far and a partial month lighter), a dashed line at `cap`.
  // `o`: {cap, rgb, note, capTitle}. A category passes its own cap, colour and the same caption and title as before; a
  // group sums its categories into `values` and passes the group's line.
  function history(values, hist, {cap = null, rgb = '', note = 'Last 6 months. The dashed line is the budget.',
    capTitle = null} = {}) {
    if (!values.some((v) => v !== 0)) return h('p', {class: 'mut'}, 'No spending in the last 6 months.');
    const top = Math.max(cap || 0, ...values.map((v) => Math.max(v, 0)), 1);
    const label = (v, k) => {
      const cov = hist.coverage[k];
      return `${monthLabel(hist.months[k])}${cov.is_current ? ' (so far)' : cov.coverage === 'partial' ? ' (partial)' : ''}: ${v < 0 ? `net refund ${money2(-v)}` : money2(v)}`;
    };
    return h('div', {class: 'hist', style: {'--rgb': rgb}},
      h('div', {class: 'plot'}, values.map((v, k) => h('span', {class: 'hc', title: label(v, k), role: 'img', 'aria-label': label(v, k)},
        h('i', {class: hist.coverage[k].is_current ? 'cur' : hist.coverage[k].coverage === 'partial' ? 'part' : '',
          style: {height: `${(Math.max(v, 0) / top * 100).toFixed(1)}%`}}))),
      cap != null ? h('span', {class: 'cap', style: {bottom: `${(cap / top * 100).toFixed(1)}%`},
        title: capTitle || `Budget ${money(cap)}`}) : null),
      h('div', {class: 'lbl'}, hist.months.map((m) => h('span', {}, monthShort(m)))),
      h('small', {}, note));
  }

  // A capped category's Details: the rollover, the bills still due, last month and the average, the six months.
  function details(c, d, f) {
    return help(`Details for ${c.category}`, () => {
      const kids = [];
      const extra = HOOKS.budgetExtra(c);
      const note = HOOKS.budgetNote(c);
      if (extra) kids.push(h('div', {class: 'rollnote'}, extra));
      if (note) kids.push(h('div', {class: 'rollnote'}, note));
      if (f.due) kids.push(h('h4', {}, 'Still due'), helpBills(c.bills_due || []));
      kids.push(h('div', {class: 'row3'}, h('span', {}, `${monthName(addMonths(d.month, -1))}: `, h('b', {class: 'v'}, money(c.last_month))),
        h('span', {}, '3-month average: ', h('b', {class: 'v'}, money(c.avg3)))),
      history(d.history.series[c.category] || [], d.history, {cap: c.cap, rgb: rgbOf(catColor(c.category))}));
      return kids;
    }, {key: 'budget-bills', long: true});
  }

  // A group's Details: the six months of the whole group, last month and the average, the bills still due across it, and
  // this month's breakdown by category (a link each, to that category's transactions).
  function groupDetails(g, cats, d) {
    return help(`Details for ${g.name}`, () => {
      const byName = new Map([...(cats || []), ...d.categories].map((c) => [c.category, c]));
      const sum = (key) => cents(g.categories.reduce((s, n) => s + ((byName.get(n) || {})[key] || 0), 0));
      // a short or missing series counts as 0: the group's chart always has one bar per month
      const values = d.history.months.map((_, k) => g.categories.reduce((s, n) => s + (((d.history.series[n] || [])[k]) || 0), 0));
      const own = g.budget != null;
      const cap = own ? g.budget : g.planned;
      const note = own ? `Last 6 months. The dashed line is the group budget (${money(cap)}).`
        : `Last 6 months. The dashed line is its category budgets added up (${money(cap)}).`;
      const merged = g.categories.flatMap((n) => (byName.get(n) || {}).bills_due || []);
      const order = new Map(g.categories.map((n, i) => [n, i]));
      const rows = g.categories.map((n) => byName.get(n)).filter((c) => c && c.spend !== 0)
        .sort((a, b) => b.spend - a.spend || (order.get(a.category) ?? 0) - (order.get(b.category) ?? 0));
      const kids = [history(values, d.history, {cap, rgb: g.rgb, note,
        capTitle: own ? `Group budget ${money(cap)}` : `Category budgets added up ${money(cap)}`}),
      h('div', {class: 'row3'}, h('span', {}, `${monthName(addMonths(d.month, -1))}: `, h('b', {class: 'v'}, money(sum('last_month')))),
        h('span', {}, '3-month average: ', h('b', {class: 'v'}, money(sum('avg3')))))];
      if (merged.length) kids.push(h('h4', {}, 'Still due'), helpBills(merged));
      kids.push(h('h4', {}, 'This month'), rows.length ? h('div', {class: 'bd'}, rows.map((c) => h('div', {class: 'bdr',
        dataset: {category: c.category, spend: String(c.spend)}},
      h('a', {class: 'nml', href: hashFor('transactions', {category: c.category}), title: `See ${c.category} transactions`}, c.category),
      h('b', {class: 'v'}, money(c.spend))))) : h('p', {class: 'mut'}, 'No spending this month.'));
      return kids;
    }, {key: 'budget-group', long: true});
  }

  // ---- the cap editor (app.js capEditor) is the app's sheet: its header says what is being edited, as the row shows it ----
  let LATEST = null;   // the last /api/budgets answer drawn: the sheet's figures follow a redraw (the roll-over switch)
  const attr = (v) => String(v).replace(/["\\]/g, '\\$&');
  // where focus goes back to after a save redraws the table: the category's Planned, else its Set in No budget yet
  const againCat = (name) => `#page tr[data-category="${attr(name)}"] .plan, #page .nbrow[data-category="${attr(name)}"] .setc`;
  const againGroup = (name) => `#page tr.g[data-group="${attr(name)}"] .gplan, #page .nbgrp[data-group="${attr(name)}"] .setc`;
  const spentOf = (m) => (x) => `${money(x)} spent in ${m}`;

  function catSub(name) {
    return () => {
      const c = LATEST && LATEST.categories.find((x) => x.category === name);
      if (!c) return '';
      const m = monthName(LATEST.month);
      if (c.cap == null) return `${spentOf(m)(c.spend)} · no budget yet`;
      const f = figures(c);
      return `${money(c.spend)} spent of ${money(c.cap)} in ${m}${f.due ? ` · ${money(f.due)} in bills still due` : ''} · ` +
        (f.left < 0 ? `${money(-f.left)} over budget` : `${money(f.left)} left`);
    };
  }

  function groupSub(name) {
    return () => {
      const g = LATEST && LATEST.groups.find((x) => x.name === name);
      if (!g) return '';
      const m = monthName(LATEST.month);
      if (g.budget == null && !g.caps_total) {   // no budget and no caps: the whole group's spending, as No budget yet shows it
        const byName = new Map(LATEST.categories.map((c) => [c.category, c]));
        return `${spentOf(m)(g.categories.reduce((n, k) => n + ((byName.get(k) || {}).spend || 0), 0))} · no budget yet`;
      }
      return `${money(g.spent)} spent of ${money(g.planned)} in ${m} · ` + (g.left < 0 ? `${money(-g.left)} over budget` : `${money(g.left)} left`);
    };
  }

  // Planned is the edit control: it opens the cap editor, a sheet ("Suggested $X · Use", Remove budget, the rollover
  // switch, Save / Cancel, Enter, Escape: app.js capEditor).
  function openEditor(c, btn) {
    // an uncapped row (group roll-up's Set) with no history offers its starter, as No budget yet's Use did
    const sug = c.cap == null && c.suggested == null ? c.starter : c.suggested;
    capEditor(btn, c.category, c.base_cap != null ? c.base_cap : c.cap, sug,
      {badge: badge(c.category), sub: catSub(c.category), again: againCat(c.category)});
  }

  function categoryRow(c, d) {
    const f = figures(c);
    const rolled = c.rollover && c.rollover.amount > 0 ? c.rollover.amount : 0;
    const label = HOOKS.budgetLabel(c);   // rollover.js keeps the row for the editor's switch here
    const set = c.base_cap != null ? c.base_cap : c.cap;
    const plan = h('button', {type: 'button', class: 'plan', title: label ? label.textContent : 'Edit this budget',
      'aria-label': `Budget for ${c.category}: ${money(c.cap)}${rolled ? `, ${money(set)} set and ${money(rolled)} rolled over` : ''}. Edit`},
    money(c.cap), rolled ? h('span', {class: 'roll', 'aria-hidden': 'true'}, icon('repeat')) : null);
    const tr = h('tr', {class: 'c', role: 'row',
      // the audit (scripts/audit_numbers.py) reads the bills still due and the rollover from here, as it did from the card
      dataset: {category: c.category, due: String(f.due || 0), rollover: String((c.rollover && c.rollover.amount) || 0),
        left: String(f.left), status: f.status}},
    h('td', {class: 'n', role: 'cell'}, h('div', {class: 'nm'},
      h('span', {class: 'l'}, badge(c.category, 'xs'),
        h('a', {class: 'nml', href: hashFor('transactions', {category: c.category}), title: `See ${c.category} transactions`}, c.category),
        details(c, d, f)),
      hairline(c, f))),
    h('td', {class: 'p', role: 'cell'}, plan),
    h('td', {class: 'a', role: 'cell'}, h('span', {class: 'act'}, money(c.spend))),
    h('td', {class: 'r', role: 'cell'}, h('span', {class: `rp${f.left === 0 ? ' z' : TONE[f.status]}`,
      'aria-label': f.left < 0 ? `${money(-f.left)} over budget` : `${money(f.left)} left`}, money(f.left))));
    plan.addEventListener('click', () => openEditor(c, plan));
    return tr;
  }

  // polish P2e: an uncapped category inside a group budget: its spending counts in the group's Spent, so it gets a row
  // with just that (no Planned, no Left of its own). Group roll-up: in a group with no budget of its own the same row's
  // Planned is Set, which opens the cap editor (its suggestion included), as No budget yet's Set did.
  function spendRow(c, g) {
    const set = g.budget == null ? h('button', {type: 'button', class: 'plan set', title: 'Set a budget for this category',
      'aria-label': `Set a budget for ${c.category}`}, 'Set') : null;
    const tr = h('tr', {class: 'u', role: 'row', dataset: {category: c.category, spent: String(c.spend)}},
      h('td', {class: 'n', role: 'cell'}, h('div', {class: 'nm'}, h('span', {class: 'l'}, badge(c.category, 'xs'),
        h('a', {class: 'nml', href: hashFor('transactions', {category: c.category}), title: `See ${c.category} transactions`}, c.category)))),
      h('td', {class: 'p', role: 'cell'}, set), h('td', {class: 'a', role: 'cell'},
        c.spend === 0 && c.due > 0 ? h('span', {class: 'act udue', title: 'A bill still due this month: it counts in the group'}, `${money(c.due)} due`)
          : h('span', {class: 'act'}, money(c.spend))),
      h('td', {class: 'r', role: 'cell'}));
    if (set) set.addEventListener('click', () => openEditor(c, set));
    return tr;
  }

  async function applyGroup(group, amount) {
    await post('/api/budgets/group', {group, monthly_budget: amount});
    await reloadBudgets();
    toast(amount == null ? ['Group budget removed from ', h('b', {}, group), '.']
      : ['Group budget set: ', h('b', {}, group), ` ${money(amount)} a month.`], [], {timeout: 3500});
  }

  // A group's Planned opens the same editor as a category's: its own budget, or (none set) the caps' sum as the hint. An
  // empty field or Remove budget clears it, and Planned is the caps' sum again.
  const groupEditor = (g, btn) => capEditor(btn, g.name, g.budget, null, {badge: gbadge(g.name, g.rgb), sub: groupSub(g.name),
    again: againGroup(g.name), save: (amount) => applyGroup(g.name, amount), label: `Monthly budget for the ${g.name} group`,
    placeholder: g.caps_total});

  // A group row: the chevron button (the keyboard and touch way to fold it), its tile and name, and the group's totals.
  function groupBody(g, cats, d) {
    const shut = SHUT.has(g.name);
    const own = g.budget != null;
    const capped = cats.filter((c) => c.cap != null).length;
    const rows = cats.map((c) => (c.cap != null ? categoryRow(c, d) : spendRow(c, g)));
    const ids = rows.map((r, i) => { r.id = `bt-${g.name.replace(/\W+/g, '-').toLowerCase()}-${i}`; return r.id; });
    // the fold button is the chevron alone (a link can't live inside a button); its hit box stays 44px on a phone, and
    // a tap 24px in from its left edge still folds it
    const btn = h('button', {type: 'button', class: 'gbtn', 'aria-expanded': String(!shut), 'aria-controls': ids.join(' ') || null,
      'aria-label': own ? `${g.name}, group budget${capped ? ` and ${plural(capped, 'category budget')}` : ''}` : `${g.name}, ${plural(cats.length, 'budget')}`},
    h('span', {class: 'chev', 'aria-hidden': 'true'}, icon('chevd')));
    const name = h('a', {class: 'nml gn', href: hashFor('transactions', {group: g.name}), title: `See ${g.name} transactions`}, g.name);
    const line = h('div', {class: 'ghl'}, btn, gbadge(g.name, g.rgb, 'xs'), name,
      h('span', {class: 'gc', 'aria-hidden': 'true'}, String(cats.length)), groupDetails(g, cats, d));
    const tone = own && g.status !== 'ok' ? TONE[g.status].trim() : g.left < 0 ? 'b' : g.left > 0 ? 'o' : '';
    const plan = h('button', {type: 'button', class: `plan gplan${own ? ' own' : ''}`,
      title: own ? 'Edit the group budget' : 'Set a budget for the whole group',
      'aria-label': own ? `${g.name} group budget: ${money(g.planned)}. Edit`
        : `${g.name}: ${money(g.planned)}, its category budgets added up. Set a group budget`}, money(g.planned));
    // the caps inside a group budget add up to more than it: said once, quietly, next to the figure it's about
    const note = g.over_note != null ? h('span', {class: 'gover', role: 'note'}, `categories add up to ${money(g.over_note)}`) : null;
    // no budget of its own: Planned is its caps' sum. Shown as a plain figure like every other Planned (review
    // 2026-10-21: no visible "sum" marker); the button's label still says so to a screen reader.
    const head = h('tr', {class: `g${own ? ' own' : ''}`, role: 'row', dataset: {group: g.name, planned: String(g.planned), spent: String(g.spent),
      left: String(g.left), budget: own ? String(g.budget) : '', caps: String(g.caps_total), status: own ? g.status : ''}},
    h('td', {class: 'gh', role: 'cell'}, note ? h('div', {class: 'ghw'}, line, note) : line), h('td', {class: 'p', role: 'cell'}, plan), h('td', {class: 'a', role: 'cell'}, money(g.spent)),
    h('td', {class: 'r', role: 'cell'}, h('span', {class: `gtot ${tone}`.trim(),
      'aria-label': g.left < 0 ? `${money(-g.left)} over budget` : `${money(g.left)} left`}, money(g.left))));
    plan.addEventListener('click', () => groupEditor(g, plan));
    const body = h('tbody', {class: `bg${shut ? ' shut' : ''}`, role: 'rowgroup', dataset: {group: g.name}}, head, rows);
    rows.forEach((r) => { r.hidden = shut; });
    btn.addEventListener('click', () => {
      const now = !body.classList.contains('shut');
      body.classList.toggle('shut', now);
      btn.setAttribute('aria-expanded', String(!now));
      for (const r of body.rows) {
        if (r === head) continue;
        r.hidden = now;
        if (!now && !matchMedia(REDUCED).matches) {   // opacity and transform only, 160ms (budgets.css)
          r.classList.add('in');
          r.addEventListener('animationend', () => r.classList.remove('in'), {once: true});
        }
      }
      if (now) SHUT.add(g.name); else SHUT.delete(g.name);
      write(SHUT_KEY, JSON.stringify([...SHUT]));
    });
    return body;
  }

  // The groups with a budget of their own or a capped category in them, each with its rows in the taxonomy's order (a
  // group's `categories`): the capped ones, and the uncapped ones it counts (rolled: spending, or a bill due without one).
  const rolled = (c, g) => c.cap == null && (c.spend !== 0 || (g.budget == null && c.due > 0));
  const counts = (g, byName) => g.budget != null || g.categories.some((n) => (byName.get(n) || {}).cap != null);
  function shown(d) {
    const cats = new Map(d.categories.map((c) => [c.category, c]));
    return d.groups.map((g) => [g, counts(g, cats) ? g.categories.map((n) => cats.get(n))
      .filter((c) => c && (c.cap != null || rolled(c, g))) : []])
      .filter(([g, rows]) => g.budget != null || rows.length);
  }

  function table(d, groups) {
    const bodies = groups.map(([g, rows]) => groupBody(g, rows, d));
    return h('section', {class: 'card btcard'}, h('table', {class: 'bt', role: 'table', 'aria-label': 'Budgets by category group'},
      h('thead', {role: 'rowgroup'}, h('tr', {role: 'row'}, ['Category', 'Planned', 'Spent', 'Left'].map((t) => h('th', {scope: 'col', role: 'columnheader'}, t)))),
      bodies));
  }

  // ---- No budget yet ----
  function noBudgetRow(c, d, reason) {
    const suggested = c.suggested != null ? c.suggested : c.starter;   // `starter`: no history yet, so what it has taken so far
    const useIt = suggested != null ? () => applyCap(c.category, suggested).catch(failed) : null;
    const why = c.spend > 0 ? `${money(c.spend)} spent in ${monthName(d.month)}${reason && suggested == null ? ` · ${reason}` : ''}` : reason || 'No budget set';
    const basis = suggested != null ? help(`Budget suggestion for ${c.category}`, () => h('div', {},
      h('p', {class: 'mut'}, c.suggested == null
        ? `There isn’t enough history to go on yet, so this is what you have spent in ${monthName(d.month)} so far, rounded up: ${money(suggested)} a month.`
        : `Based on what you usually spend, this month’s budget would be ${money(suggested)}.`),
      h('p', {}, h('button', {class: 'lnk', type: 'button', onclick: useIt}, 'Use that as the budget'), '.')),
    {key: 'budget-suggestion', long: true}) : null;
    if (basis) basis.classList.add('nbsg');
    const setc = h('button', {class: 'setc', type: 'button', 'aria-label': `Set a budget for ${c.category}`,
      onclick: () => capEditor(setc, c.category, null, suggested,
        {badge: badge(c.category), sub: catSub(c.category), again: againCat(c.category)})}, 'Set');
    const use = suggested != null ? h('button', {class: 'usec', type: 'button', title: `Set the budget to ${money(suggested)}`,
      'aria-label': `Use ${money(suggested)} as the budget for ${c.category}`, onclick: useIt}, `Use ${money(suggested)}`) : null;
    return h('div', {class: 'li wrap nbrow', dataset: {category: c.category, spent: String(c.spend), ...(c.new ? {new: ''} : {})}},
      badge(c.category, 's'),
      h('span', {class: 'grow'}, h('b', {}, c.category, c.new ? h('span', {class: 'newc'}, 'New') : null), basis, h('span', {title: why}, why)),
      use, setc);
  }

  // A group with no budget and no capped category isn't in the table, so this is where it gets one (review of P2e,
  // 2026-10-20): the same row as a category's, its Set opening the group editor. Its spend is the whole group's.
  function noBudgetGroupRow(g, spend, d) {
    const why = spend > 0 ? `Whole group · ${money(spend)} spent in ${monthName(d.month)}` : 'Whole group · no budget set';
    const setc = h('button', {class: 'setc', type: 'button', 'aria-label': `Set a budget for the whole ${g.name} group`,
      onclick: () => capEditor(setc, g.name, null, null, {badge: gbadge(g.name, g.rgb), sub: groupSub(g.name), again: againGroup(g.name),
        save: (amount) => applyGroup(g.name, amount), label: `Monthly budget for the ${g.name} group`})}, 'Set');
    return h('div', {class: 'li wrap nbrow nbgrp', dataset: {group: g.name, spent: String(spend)}},
      gbadge(g.name, g.rgb, 's'), h('span', {class: 'grow'},
        h('a', {class: 'nml nbgn', href: hashFor('transactions', {group: g.name}), title: `See ${g.name} transactions`}, g.name),
        h('span', {title: why}, why)), setc);
  }

  function noBudget(d, uncapped) {
    const reasons = new Map(d.not_budgeted.map((x) => [x.category, x.reason]));
    const order = new Map(d.groups.flatMap((g) => g.categories).map((n, i) => [n, i]));
    const byOrder = (a, b) => (order.get(a.category) ?? 1e6) - (order.get(b.category) ?? 1e6);
    const byName = new Map(d.categories.map((c) => [c.category, c]));
    const open = d.groups.filter((g) => g.budget == null && !g.categories.some((n) => (byName.get(n) || {}).cap != null))
      .map((g) => [g, g.categories.reduce((s, n) => s + ((byName.get(n) || {}).spend || 0), 0)]);
    const grow = ([g, spend]) => noBudgetGroupRow(g, spend, d);
    const spending = uncapped.filter((c) => c.spend > 0).sort((a, b) => b.spend - a.spend || byOrder(a, b));
    const idle = uncapped.filter((c) => !(c.spend > 0)).sort(byOrder);
    const row = (c) => noBudgetRow(c, d, reasons.get(c.category));
    const idleGroups = open.filter(([, s]) => !(s > 0)).map(grow);
    const kids = [...open.filter(([, s]) => s > 0).sort((a, b) => b[1] - a[1]).map(grow), ...spending.map(row)];
    if (idleGroups.length && !idle.length) kids.push(...idleGroups);
    if (idle.length) {
      const rows = h('div', {class: 'idle', id: 'nb-idle', hidden: kids.length > 0}, idleGroups, idle.map(row));
      const label = (open) => (open ? 'Hide them' : idleGroups.length ? `Show ${idle.length + idleGroups.length} with nothing spent`
        : `Show ${plural(idle.length, 'category', 'categories')} with nothing spent`);
      const more = h('button', {type: 'button', class: 'more', 'aria-expanded': String(!rows.hidden), 'aria-controls': 'nb-idle'}, label(!rows.hidden));
      more.addEventListener('click', () => {
        rows.hidden = !rows.hidden;
        more.setAttribute('aria-expanded', String(!rows.hidden));
        more.textContent = label(!rows.hidden);
      });
      kids.push(kids.length ? more : null, rows);
    }
    const nb = card({cls: 'nb', title: 'No budget yet', icon: 'tag', rgb: RGB.grey},
      kids.some(Boolean) ? kids : h('p', {class: 'mut'}, 'Every spending category has a budget.'));
    return nb;
  }

  // ---- polish P2e: the one-time move to group budgets ----
  // The banner: while a group with caps has no budget of its own and wasn't kept (/api/budgets `move`).
  function moveBanner(d) {
    const m = d.move;
    if (!m || !m.count || read(MOVE_KEY) === thisMonth()) return null;
    const later = h('button', {type: 'button', class: 'later', onclick: () => {
      write(MOVE_KEY, thisMonth());
      const next = banner.nextElementSibling;
      banner.remove();
      const to = next && next.querySelector('button, a[href]');
      if (to) to.focus();
    }}, 'Not now');
    const banner = h('div', {class: 'mvb', role: 'region', 'aria-label': 'Move budgets to groups', dataset: {note: 'move'}}, icon('layers'),
      h('p', {}, `Move ${plural(m.count, 'budget')} to groups`),
      h('button', {type: 'button', class: 'got', onclick: () => openMove(d)}, 'Review'), later);
    return banner;
  }

  // What the move changed, in a toast (the response's moved / kept)
  const moved = (res) => [
    res.moved.length ? `Group budgets: ${res.moved.map((x) => `${x.group} ${money(x.monthly_budget)}`).join(', ')}.` : '',
    res.kept.length ? `Kept: ${res.kept.map((x) => x.group).join(', ')}.` : ''].filter(Boolean).join(' ') || 'Nothing changed.';

  // The review: one row per eligible group, its tile and name, its caps ("Groceries $400 + Eating Out $150"), what the group
  // spent this month, Group budget (the default: the caps' sum, editable) or Keep category budgets. Apply is one request.
  function openMove(d) {
    const rows = d.move.groups.map((g, i) => {
      const st = {choice: 'group'};
      const eid = `mverr-${i}`;
      const input = h('input', {type: 'text', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', value: String(g.amount),
        'aria-label': `Group budget for ${g.group}`, 'aria-describedby': eid});
      const amount = h('label', {class: 'edin mvamt'}, h('span', {class: 'pre', 'aria-hidden': 'true'}, CUR.sym), input);
      const err = h('span', {class: 'err', role: 'alert', id: eid});
      const btns = [['group', 'Group budget'], ['keep', 'Keep category budgets']].map(([v, label]) =>
        h('button', {type: 'button', 'aria-pressed': String(v === st.choice), dataset: {choice: v}}, label));
      const seg = h('div', {class: 'seg mvseg', role: 'group', 'aria-label': `${g.group}: group budget or keep its category budgets`}, btns);
      for (const b of btns) {
        b.addEventListener('click', () => {
          st.choice = b.dataset.choice;
          for (const x of btns) x.setAttribute('aria-pressed', String(x === b));
          input.disabled = st.choice === 'keep';
          amount.classList.toggle('off', input.disabled);
          err.textContent = '';
          input.removeAttribute('aria-invalid');
          relabel();
        });
      }
      input.addEventListener('input', () => {
        err.textContent = '';
        input.removeAttribute('aria-invalid');
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') go();
      });
      const row = h('div', {class: 'mvrow', role: 'group', 'aria-label': g.group, dataset: {group: g.group}},
        gbadge(g.group, g.rgb, 's'),
        h('div', {class: 'mvn'}, h('b', {}, g.group), h('span', {class: 'mvsp'}, `${money(g.spent)} spent`),
          h('span', {class: 'mvcaps'}, g.caps.map((c) => `${c.category} ${money(c.cap)}`).join(' + '))),
        h('div', {class: 'mvch'}, seg, amount), err);
      return {g, st, input, err, row, first: btns[0]};
    });
    const apply = h('button', {type: 'button', class: 'btn', onclick: () => go()});
    const fail = h('p', {class: 'mvfail', role: 'alert'});
    function relabel() {
      const n = rows.reduce((s, r) => s + (r.st.choice === 'group' ? r.g.caps.length : 0), 0);
      apply.textContent = n ? `Move ${n}` : 'Keep all';
    }
    async function go() {
      const groups = [];
      for (const r of rows) {
        if (r.st.choice === 'keep') {
          groups.push({group: r.g.group, choice: 'keep'});
          continue;
        }
        const n = parseAmount(r.input.value);
        if (n == null || Number.isNaN(n) || n < 0 || n > 100000) {
          r.err.textContent = 'Enter an amount from $0 to $100,000.';
          r.input.setAttribute('aria-invalid', 'true');
          r.input.focus();
          return;
        }
        groups.push({group: r.g.group, choice: 'group', amount: Math.round(n * 100) / 100});
      }
      apply.disabled = true;
      fail.textContent = '';
      try {
        const res = await post('/api/budgets/move', {groups});
        closeModal();
        await reloadBudgets();
        toast(moved(res), [], {timeout: 4500});
      } catch (e) {
        fail.textContent = `Nothing was changed: ${e.message}`;
        apply.disabled = false;
      }
    }
    relabel();
    openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Move budgets to groups'), closeBtn()),
      h('div', {class: 'mvlist'}, rows.map((r) => r.row)), fail,
      h('div', {class: 'row'}, apply, h('button', {type: 'button', class: 'btn ghost', onclick: closeModal}, 'Cancel'))],
    {cls: 'mvsheet', focus: rows.length ? rows[0].first : null});
  }

  HOOKS.budgetsBody = (d) => {
    LATEST = d;
    // a group budget counts every category in its group: those are budgeted, never "No budget yet"; a group with caps
    // counts its uncapped ones too, and the ones with spending (or a bill due) sit under it instead
    const byName = new Map(d.categories.map((c) => [c.category, c]));
    const covered = new Set(d.groups.flatMap((g) => (g.budget != null ? g.categories
      : counts(g, byName) ? g.categories.filter((n) => byName.has(n) && rolled(byName.get(n), g)) : [])));
    const uncapped = d.categories.filter((c) => c.cap == null && !covered.has(c.category));
    const groups = shown(d);
    return [moveBanner(d), groups.length ? table(d, groups) : null, noBudget(d, uncapped)];
  };
});
