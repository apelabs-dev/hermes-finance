/* The bills calendar on Recurring (TASK-19). Loaded before app.js (window.HF_EXT). It takes the place of the "Next 30 days"
 * card: this month and the next 11, the recurring charges /api/recurring finds on their days (GET /api/bills-calendar:
 * paid on the last charge's day, due on the next one and after it at the item's cadence), paid vs due, the month
 * total. Below 1200px it's an agenda, day by day. The week starts on the Region setting's day (Sunday until then). */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, glyph, sum, money, money2, fmtDate, relDay, asDate, pad, monthName, monthLabel, catColor, avatar, cal, api,
    S, RENDER, HOOKS, help, card} = X;
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  let MONTH = null;  // the month on show (kept while you stay on Recurring)

  const isoOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const check = (state) => h('span', {class: `ck ${state === 'paid' ? 'paid' : 'due'}${state === 'late' ? ' late' : ''}`,
    role: 'img', 'aria-label': state === 'paid' ? 'Paid' : state === 'late' ? 'Late' : 'Due'}, state === 'paid' ? glyph('check', 2.6) : null);
  // (TASK-21) a schedule other than monthly is named; a bill whose amount changes is due at its estimate, "~$125"
  const every = (b) => (b.cadence === 'monthly' ? '' : ` · ${b.cadence_label}`);
  const stateText = (b) => (b.state === 'paid' ? `Paid ${fmtDate(b.date)}${every(b)}` : b.state === 'late' ? `Late · ${b.category}${every(b)}`
    : `${b.projected ? 'Expected' : 'Due'} · ${b.category}${every(b)}`);
  const amt = (b) => (b.variable ? `~${money(b.amount)}` : money2(b.amount));

  function calendarCard(d) {
    const [y, m] = [Number(d.month.slice(0, 4)), Number(d.month.slice(5, 7))];
    const start = new Date(y, m - 1, 1, 12);
    const days = new Date(y, m, 0).getDate();
    const lead = (start.getDay() - d.week_start + 7) % 7;
    const cells = Math.ceil((lead + days) / 7) * 7;
    const first = new Date(y, m - 1, 1 - lead, 12);
    const byDay = new Map();
    d.bills.forEach((b) => { if (!byDay.has(b.date)) byDay.set(b.date, []); byDay.get(b.date).push(b); });
    // (TASK-20) the merchant's logo as on every transaction row (Plaid's logo_url, else its category icon; initials
    // without either), like the phone agenda
    const entryProps = (b) => ({type: 'button', dataset: {cadence: b.cadence, billKey: b.key, occurrence: b.date},
      'aria-label': `${b.merchant}, ${amt(b)}, ${fmtDate(b.date, true)}, ${stateText(b)}: open bill details`,
      onclick: () => HOOKS.billSheet(b, {occurrence: b})});
    const chip = (b) => h('button', {...entryProps(b), class: `bchip ${b.state}${b.projected ? ' proj' : ''}`, style: {'--cc': catColor(b.category)},
      title: `${b.merchant} ${amt(b)}${b.variable ? ' est.' : ''} · ${stateText(b)}`},
    check(b.state), avatar(b.merchant, b.logo_url, b.icon_url, 24),
    h('span', {class: 'bct'}, h('b', {}, b.merchant), h('span', {class: 'v'}, amt(b))));
    const week = Array.from({length: 7}, (_, i) => DAYS[(i + d.week_start) % 7]);
    const grid = h('div', {class: 'calg', role: 'grid', 'aria-label': `Bills in ${monthLabel(d.month)}`},
      h('div', {class: 'cwk', role: 'row'}, week.map((w) => h('div', {class: 'cwd', role: 'columnheader'}, w))),
      Array.from({length: cells / 7}, (_, r) => h('div', {class: 'cwk', role: 'row'}, Array.from({length: 7}, (__, c) => {
        const dt = new Date(first);
        dt.setDate(first.getDate() + r * 7 + c);
        const iso = isoOf(dt);
        const out = iso.slice(0, 7) !== d.month;
        const here = out ? [] : byDay.get(iso) || [];
        const today = iso === d.today;
        return h('div', {class: `cday${out ? ' out' : ''}${today ? ' today' : ''}`, role: 'gridcell',
          'aria-label': out ? null : `${asDate(iso).toLocaleDateString('en-US', {month: 'long', day: 'numeric'})}${here.length
            ? `: ${here.map((b) => `${b.merchant} ${amt(b)}, ${b.state}`).join('; ')}` : ''}`},
        h('span', {class: 'dn', 'aria-hidden': 'true'}, today ? [h('b', {}, String(dt.getDate())), h('span', {class: 'tdy'}, 'Today')] : String(dt.getDate())),
        here.map(chip));
      }))));
    const agenda = h('div', {class: 'agenda'}, [...byDay.entries()].map(([iso, list]) => h('div', {class: 'agd'},
      h('div', {class: 'agh'}, cal(iso, iso === d.today ? 'hot' : ''), h('span', {class: 'grow'},
        h('b', {}, asDate(iso).toLocaleDateString('en-US', {weekday: 'long', month: 'short', day: 'numeric'})),
        h('span', {}, relDay(iso, d.today))), list.length > 1 ? h('span', {class: 'amt v'},
        `${list.some((b) => b.variable) ? '~' : ''}${money2(sum(list, (b) => b.amount))}`) : null),
      list.map((b) => h('button', {...entryProps(b), class: `li agi ${b.state}`, style: {'--cc': catColor(b.category)}},
        check(b.state), avatar(b.merchant, b.logo_url, b.icon_url, 24),
        h('span', {class: 'grow'}, h('b', {}, b.merchant), h('span', {}, stateText(b))),
        h('span', {class: 'amt v'}, b.variable ? `~${money(b.amount)} est.` : money2(b.amount)))))),
    d.bills.length ? null : h('p', {class: 'mut'}, `No recurring charges expected in ${monthName(d.month)}.`));
    const nav = h('div', {class: 'mon calnav', role: 'group', 'aria-label': 'Month'},
      h('button', {type: 'button', 'aria-label': 'Previous month', disabled: !d.prev, onclick: () => show(d.prev)}, icon('chevl')),
      h('span', {'aria-live': 'polite'}, monthLabel(d.month)),
      h('button', {type: 'button', 'aria-label': 'Next month', disabled: !d.next, onclick: () => show(d.next)}, icon('chevr')));
    const legend = h('div', {class: 'calleg'}, h('span', {}, check('paid'), 'Paid'), h('span', {}, check('due'), 'Due'),
      h('span', {}, '~ Estimate'), help('Calendar legend', 'Colour shows category. Dates after the next due date are expected at the usual interval; ~ marks an estimate.',
        {key: 'calendar-legend'}));
    return card({cls: 'calcard', title: 'Bills calendar', icon: 'calendar', rgb: X.RGB.acc, extra: nav},
      h('div', {class: 'calsum'},
        h('div', {}, h('span', {class: 'mut'}, `${monthName(d.month)} total`), h('b', {class: 'v', dataset: {calTotal: String(d.total)}}, money2(d.total))),
        h('div', {}, h('span', {class: 'mut'}, 'Paid'), h('b', {class: 'v'}, `${d.paid_count}\u00a0of\u00a0${d.count}`)),
        h('div', {}, h('span', {class: 'mut'}, 'Still due'), h('b', {class: 'v'}, `${d.bills.some((b) => b.variable) ? '~' : ''}${money2(d.due_total)}`))),
      grid, agenda, legend);
  }

  async function show(month) {
    const old = document.querySelector('#page .calcard');
    const tk = S.token;
    const d = await api('/api/bills-calendar', {month});
    if (tk !== S.token) return;
    MONTH = d.month;
    const el = calendarCard(d);
    if (old && old.isConnected) old.replaceWith(el);
    const nav = el.querySelector('.calnav button:not([disabled])');
    if (old && nav && document.activeElement === document.body) nav.focus();
  }

  const renderRecurring = RENDER.recurring;
  RENDER.recurring = async (tk) => {
    // asked at the same time as the page's own data, so the calendar goes in as soon as the page is drawn
    const calP = api('/api/bills-calendar', {month: MONTH || undefined})
      .catch(() => api('/api/bills-calendar'));  // the kept month is out of range now (a new month began)
    await renderRecurring(tk);
    if (tk !== S.token) return;
    const d = await calP;
    if (tk !== S.token) return;
    MONTH = d.month;
    const first = X.$('page').firstElementChild;  // "Next 30 days": the calendar takes its place
    if (first && first.classList.contains('card')) first.replaceWith(calendarCard(d));
    // Phone (the approved layout call): a Bills row puts its status under the amount, as the Subscriptions card does
    // (beside it, the payee's name was squeezed onto 3-4 lines at 390px)
    if (matchMedia('(max-width: 1023.98px)').matches) {
      document.querySelectorAll('#page .li.wrap').forEach((li) => {
        const pl = li.querySelector(':scope > .pill');
        const amt = li.querySelector(':scope > .amt');
        if (!pl || !amt) return;
        const rt = h('span', {class: 'rt'});
        li.insertBefore(rt, pl);
        rt.append(amt, pl);
        amt.style.removeProperty('min-width');
      });
    }
  };
  window.addEventListener('hashchange', () => { if (!location.hash.startsWith('#/recurring')) MONTH = null; });
});
