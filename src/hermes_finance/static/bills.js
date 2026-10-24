/* Bills (TASK-21): any schedule, amounts that change, bills tracked by hand. Loaded before app.js (window.HF_EXT).
 * The transaction sheet gets a Bill section: the charge's merchant as a bill (its schedule and estimate, "higher than
 * usual" on this charge) with Edit schedule and Stop tracking, or "Track as a bill". That opens a small sheet: the
 * schedule (pre-picked: the one it was found on, else every month), "Same each time" / "Changes each time" (pre-picked
 * from the category), "Usually about" for one that changes, starting from this charge; the line under it is what the
 * server says it will expect next (GET /api/bills/preview), before anything is saved. A Recurring row opens its bill
 * sheet: the estimate and usual range, the schedule, where it was last paid from, its latest charges on any account,
 * Edit schedule and Stop tracking. Every change is a POST through the page's CSRF token (post()). */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, plural, money, money2, billAmt, usualText, billTag, fmtDate, acctName, nameMask, instShort, avatar,
    bigMoney, api, post, S, HOOKS, openSheet, closeModal, closeBtn, backBtn, toast, modal} = X;
  let OPEN = null;  // the transaction sheet's Bill section on screen: {sec, t, ctx}

  const fld = (label, control, opts = {}) => h('div', {class: 'fld', dataset: opts.key ? {f: opts.key} : null},
    opts.for ? h('label', {for: opts.for}, label, opts.optional ? h('em', {}, 'optional') : null) : h('div', {class: 'lbl'}, label),
    control, h('p', {class: 'ferr', role: 'alert'}));
  const usualValue = (u) => (u ? (u[0] === u[1] ? String(u[0]) : `${u[0]}–${u[1]}`) : '');
  const nextLine = (b) => [`Next ${b.variable ? '~' : ''}${fmtDate(b.next_expected)}`, billAmt(b),
    b.variable && b.usual ? `usually ${usualText(b.usual)}` : null].filter(Boolean).join(' · ');
  const from = (a) => (a ? nameMask(acctName(a.name) || instShort(a.institution), a.mask, {pre: 'from '}) : null);

  // after a change: the page (and an open transaction's Bill section) shows it
  function after() {
    X.loadCounts();
    if (S.page === 'transactions') {
      X.loadTx(S.token, true).catch(() => {});
      if (OPEN && OPEN.sec.isConnected) load(OPEN.sec, OPEN.t, OPEN.ctx);
    } else {
      X.refresh();
    }
  }

  // ---- the transaction sheet's Bill section ---------------------------------------------------------------------
  HOOKS.txBill = (t, ctx) => {
    if (!(t.amount > 0) || t.is_income || t.is_transfer) return null;  // a bill is money going out
    const sec = h('div', {class: 'tsec billsec'}, h('div', {class: 'tsh'}, h('b', {}, 'Bill')), h('p', {class: 'hint'}, 'Checking…'));
    load(sec, t, ctx);
    return sec;
  };

  function load(sec, t, ctx) {
    OPEN = {sec, t, ctx};
    api('/api/bills/lookup', {transaction_id: t.transaction_id}).then((d) => draw(sec, t, d, ctx)).catch(() => sec.remove());
  }

  function draw(sec, t, d, ctx) {
    if (!d.bill && !d.trackable) {
      sec.remove();
      return;
    }
    // the phone's transaction sheet is the modal a Track / Stop sheet replaces: what was typed in it is saved first, and
    // Back opens it again
    const docked = Boolean(sec.closest('#txpanel'));
    const leave = () => { if (!docked && ctx && ctx.save) ctx.save(); };
    const back = docked || !HOOKS.openTx ? null : () => HOOKS.openTx(t.transaction_id);
    if (d.bill) {
      const b = d.bill;
      fill(sec, h('div', {class: 'tsh'}, h('b', {}, 'Bill'), h('span', {}, b.source === 'tracked' ? 'Tracked by hand' : 'Found from its charges')),
        h('div', {class: 'bline'}, h('b', {}, `${b.cadence_label[0].toUpperCase()}${b.cadence_label.slice(1)}`), billTag(d.outlier)),
        h('p', {class: 'hint'}, nextLine(b)),
        h('div', {class: 'row'},
          h('button', {class: 'btn sm ghost', type: 'button', onclick: () => { leave(); trackSheet({...d, action: 'update'}, back); }}, icon('pencil'), 'Edit schedule'),
          h('button', {class: 'btn sm danger', type: 'button', onclick: () => { leave(); confirmStop(b, back); }}, 'Stop tracking')));
      return;
    }
    fill(sec, h('div', {class: 'tsh'}, h('b', {}, 'Bill'), d.hidden ? h('span', {}, 'You stopped tracking it') : null),
      h('div', {class: 'row'}, h('button', {class: 'btn sm ghost', type: 'button', onclick: () => {
        leave();
        trackSheet({...d, action: 'track', transaction_id: t.transaction_id}, back);
      }}, icon('repeat'), 'Track as a bill')));
  }

  // ---- Track as a bill / Edit schedule ----------------------------------------------------------------------------
  // o: /api/bills/lookup's answer + {action: track | update, transaction_id (track from a transaction)}
  function trackSheet(o, back) {
    const upd = o.action === 'update';
    const st = {every: o.suggest.every, varies: o.suggest.varies};
    const who = o.transaction_id ? {transaction_id: o.transaction_id} : {key: o.key};
    const sel = h('select', {id: 'bt-every', 'aria-label': 'Schedule'},
      o.schedules.map((x) => h('option', {value: x.every, selected: x.every === st.every}, x.label)));
    const seg = h('div', {class: 'seg bseg', role: 'group', 'aria-label': 'Amount'},
      [['same', 'Same each time'], ['varies', 'Changes each time']].map(([k, label]) => h('button', {type: 'button', dataset: {k},
        onclick: () => { st.varies = k === 'varies'; redraw(); }}, label)));
    const usual = h('input', {id: 'bt-usual', type: 'text', inputmode: 'decimal', autocomplete: 'off', placeholder: 'e.g. 100–150',
      value: usualValue(o.suggest.usually), 'aria-label': 'Usually about'});
    const usualFld = fld('Usually about', h('div', {class: 'inp'}, h('span', {class: 'pre', 'aria-hidden': 'true'}, '$'), usual),
      {key: 'usually', for: 'bt-usual', optional: true});
    const usualHint = h('p', {class: 'hint'}, 'One amount or a range. Until it has 3 charges, the middle of this is the estimate.');
    usualFld.append(usualHint);
    const preview = h('p', {class: 'bprev', 'aria-live': 'polite'});
    const saveBtn = h('button', {class: 'btn w', type: 'button'}, upd ? 'Save' : 'Track as a bill');
    const errs = () => document.querySelectorAll('#sheet .trackf .ferr').forEach((e) => { e.textContent = ''; });
    const showErr = (e) => {
      const slot = e.field && document.querySelector(`#sheet .trackf [data-f="${e.field}"] .ferr`);
      if (slot) slot.textContent = e.message;
      else preview.textContent = e.message;
    };
    const body = () => ({action: o.action, ...who, every: sel.value, varies: st.varies, usually: st.varies ? usual.value.trim() : ''});
    let timer = null;
    const ask = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const b = body();
        try {
          const d = await api('/api/bills/preview', {...b, varies: b.varies ? '1' : '0'});
          errs();
          fill(preview, d.bill ? [icon('calendar'), h('span', {}, nextLine(d.bill))] : null);
        } catch (e) {
          errs();
          showErr(e);
        }
      }, 200);
    };
    const redraw = () => {
      seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String((b.dataset.k === 'varies') === st.varies)));
      usualFld.hidden = !st.varies;
      ask();
    };
    sel.addEventListener('change', ask);
    usual.addEventListener('input', ask);
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      try {
        const res = await post('/api/bills/track', body());
        closeModal();
        toast([upd ? 'Saved ' : 'Tracking ', h('b', {}, res.merchant), upd ? ': ' : ' as a bill: ',
          `${res.bill.cadence_label}, ${billAmt(res.bill)}.`], [], {timeout: 4000});
        after();
      } catch (e) {
        errs();
        showErr(e);
      } finally {
        saveBtn.disabled = false;
      }
    });
    const a = upd && o.bill ? {amount: o.bill.last_amount, date: o.bill.last_date} : o.anchor;
    const start = a ? `${upd ? 'Last charge' : 'Starting from this charge'}: ${money2(a.amount)} on ${fmtDate(a.date, true).replace(/ /g, '\u00a0')}` : '';
    openSheet([
      h('div', {class: 'shd'}, back ? backBtn(back) : null, h('h2', {id: 'sheet-title'}, upd ? 'Edit schedule' : 'Track as a bill'), closeBtn()),
      h('p', {}, h('b', {}, o.merchant), start ? ` · ${start}` : null),
      h('div', {class: 'flds trackf'}, fld('Schedule', h('div', {class: 'inp sel'}, sel), {key: 'every', for: 'bt-every'}),
        fld('Amount', seg, {key: 'varies'}), usualFld),
      preview,
      h('div', {class: 'row'}, saveBtn)], {narrow: true, cls: 'billsheet', focus: sel});
    redraw();
  }

  // ---- Stop tracking ------------------------------------------------------------------------------------------------
  function confirmStop(b, back) {
    const text = b.source === 'tracked'
      ? 'It stops being a bill. Its charges stay as they are, and you can track it again from any of them.'
      : 'It won’t be listed with your bills. Its charges stay as they are, and you can track it again from any of them.';
    modal(`Stop tracking ${b.merchant}?`, h('p', {class: 'mut'}, text), [
      {label: 'Stop tracking', primary: true, onclick: async () => {
        try {
          await post('/api/bills/stop', {key: b.key});
          closeModal();
          toast(['Stopped tracking ', h('b', {}, b.merchant), '.'], [], {timeout: 3500});
          after();
        } catch (e) {
          toast(`Could not stop it: ${e.message}`, [], {error: true, timeout: 6000});
        }
      }},
      {label: 'Cancel', onclick: back || closeModal},
    ]);
  }

  // ---- a Recurring row's bill sheet ---------------------------------------------------------------------------------
  const chargeRow = (c) => h('div', {class: 'li'}, h('span', {class: 'grow'}, h('b', {}, fmtDate(c.date, true), billTag(c.outlier)),
    h('span', {}, from(c.account))), h('span', {class: 'amt v'}, money2(c.amount)));

  let billRequest = 0;
  HOOKS.billSheet = async (r, context = {}) => {
    const request = ++billRequest, token = S.token;
    let d;
    try {
      d = await api('/api/bills/lookup', {key: r.key});
    } catch (e) {
      toast(`Could not open ${r.merchant}: ${e.message}`, [], {error: true, timeout: 5000});
      return;
    }
    if (request !== billRequest || token !== S.token) return;
    if (!d.bill) {
      toast('This bill is no longer tracked. Refresh Recurring to see its current schedule.', [], {timeout: 5000});
      return;
    }
    const b = d.bill;
    const occurrence = context.occurrence;
    const reopen = () => HOOKS.billSheet(r, context);
    openSheet([
      h('div', {class: 'shd'}, avatar(b.merchant, b.logo_url, b.icon_url, 32), h('h2', {id: 'sheet-title'}, b.merchant), closeBtn()),
      occurrence ? h('div', {class: 'bill-occurrence sub', dataset: {date: occurrence.date}},
        `${occurrence.state === 'paid' ? 'Paid' : occurrence.state === 'late' ? 'Late' : occurrence.projected ? 'Expected' : 'Due'} ${fmtDate(occurrence.date, true)} · `,
        h('b', {class: 'v'}, `${occurrence.variable ? '~' : ''}${money2(occurrence.amount)}${occurrence.variable ? ' est.' : ''}`)) : null,
      h('div', {class: 'txhead'}, b.variable ? h('div', {class: 'big v s'}, `~${money(b.amount)}`, h('span', {class: 'unit'}, ' est.'))
        : bigMoney(b.amount, 's'),
      h('div', {class: 'sub'}, [`${b.cadence_label[0].toUpperCase()}${b.cadence_label.slice(1)}`, b.category,
        `next ${b.variable ? '~' : ''}${fmtDate(b.next_expected)}`].join(' · ')),
      b.variable && b.usual ? h('div', {class: 'sub dim'}, `Usually ${usualText(b.usual)} ${b.usual.source === 'you'
        ? '(what you said)' : '(its charges in the last 12 months)'}`) : null),
      h('div', {class: 'tsec'}, h('div', {class: 'tsh'}, h('b', {}, 'Charges'), h('span', {}, b.source === 'tracked' ? 'Tracked by hand'
        : `Found from ${plural(b.charges, 'charge')}`)),
      d.charges.length ? d.charges.map(chargeRow) : h('p', {class: 'hint'}, `Last paid ${fmtDate(b.last_date, true)}.`)),
      h('div', {class: 'row'},
        h('button', {class: 'btn ghost', type: 'button', onclick: () => trackSheet({...d, action: 'update'}, reopen)}, icon('pencil'), 'Edit schedule'),
        h('button', {class: 'btn danger', type: 'button', onclick: () => confirmStop(b, reopen)}, 'Stop tracking'))],
    {narrow: true, cls: 'billsheet'});
    document.querySelector('#sheet').dataset.billKey = b.key;
  };
});
