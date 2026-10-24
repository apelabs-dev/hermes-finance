/* Transactions (TASK-19): splits, notes, tags and saved filters. Loaded before app.js (window.HF_EXT).
 * A row opens its transaction sheet (docked beside the table from 1400px, where both fit; the usual sheet below): its
 * category or split lines (they must add up to the amount to the cent; "Left to split" updates as you type), its note
 * and tags. Everything saves when the sheet closes; a split that doesn't add up yet isn't saved and says so.
 * Saved filters: chips above the table (how many match in the month shown), saved and deleted on the server.
 * (TASK-21) bills.js adds the sheet's Bill section (HOOKS.txBill): Track as a bill, Edit schedule, Stop tracking. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, plural, sum, money2, bigMoney, pct, fmtDate, instShort, avatar, badge, api, post, S, RENDER, HOOKS, TX,
    openSheet, closeModal, closeBtn, toast, modal, parseAmount, updateTx, loadCounts, catLabel, txWindowParams} = X;
  const r2 = (x) => Math.round(x * 100) / 100;
  const cents = (x) => Math.round(x * 100);
  const DOCK = '(min-width: 1400px)';
  let TAGS = null;       // tags in use, for "Used before"
  let SAVED = null;      // the saved filters as last loaded ({month, filters})
  const panel = h('section', {class: 'txpanel', id: 'txpanel', role: 'dialog', 'aria-labelledby': 'tx-title', hidden: true});
  document.body.append(panel);

  // ---- rows: tag chips, a split's category cell -----------------------------------------------------------------
  HOOKS.txTags = (t) => (t.tags && t.tags.length ? h('span', {class: 'ttags'}, t.tags.map((x) => h('button', {class: 'ttag', type: 'button',
    title: `Show everything tagged ${x}`, 'aria-label': `Tag ${x}: show everything tagged ${x}`,
    onclick: (e) => { e.stopPropagation(); updateTx({tag: x}, true); }}, x))) : null);
  HOOKS.txCat = (t) => (t.split ? h('span', {class: 'catc splitc'}, h('span', {class: 'sbadges'}, t.split.slice(0, 3).map((s) => badge(s.category, 'xs'))),
    h('span', {class: 'splt', title: t.split.map((s) => `${catLabel(s.category)} ${money2(s.amount)}`).join(' + ')},
      h('b', {}, 'Split'), ` · ${t.split.map((s) => catLabel(s.category)).join(' + ')}`)) : null);

  // ---- saved filters ----------------------------------------------------------------------------------------------
  // polish P1: category, group and tag are lists (a saved one keeps a single name as text), uncategorized is '1'
  const LISTS = ['category', 'group', 'tag'];
  const many = (v) => (Array.isArray(v) ? v : v ? [v] : []);
  const active = () => {
    const f = TX.filters || {};
    const out = {};
    for (const k of ['q', 'bank']) if (f[k]) out[k] = f[k];
    for (const k of LISTS) if (many(f[k]).length) out[k] = many(f[k]);
    if (f.uncategorized) out.uncategorized = '1';
    if (f.kind && f.kind !== 'all') out.kind = f.kind;
    return out;
  };
  const norm = (f) => Object.entries(f).map(([k, v]) => [k, LISTS.includes(k) ? many(v).map((x) => x.toLowerCase()).sort() : String(v)]).sort();
  const same = (a, b) => JSON.stringify(norm(a)) === JSON.stringify(norm(b));

  let savedRequest = 0;
  async function loadSaved() {
    const mine = ++savedRequest;
    const data = await api('/api/saved-filters', {...txWindowParams(), window: '1'}).catch(() => ({filters: []}));
    if (mine === savedRequest) SAVED = data;
    return data;
  }

  function savedBar() {
    const f = active();
    const bar = h('div', {class: 'sfbar', role: 'group', 'aria-label': 'Saved filters'});
    const items = (SAVED && SAVED.filters) || [];
    const chips = items.map((x) => {
      const on = same(x.filters, f);
      const chip = h('span', {class: `sfchip${on ? ' on' : ''}`},
        h('button', {class: 'sfgo', type: 'button', 'aria-pressed': String(on), title: describe(x.filters),
          onclick: () => updateTx({q: '', category: [], group: [], uncategorized: false, bank: '', tag: [], kind: 'all', ...x.filters}, true)},
        icon(x.filters.tag && Object.keys(x.filters).length === 1 ? 'tag' : 'bookmark'), x.name,
        x.count != null ? h('span', {class: 'cnt'}, String(x.count)) : null),
        h('button', {class: 'sfx', type: 'button', 'aria-label': `Delete the saved filter ${x.name}`, onclick: () => confirmDelete(x)}, icon('x')));
      return chip;
    });
    // (the tag filter's own chip is with the other picks now, under the bar: txfilter.js)
    const canSave = Object.keys(f).length && !items.some((x) => same(x.filters, f));
    fill(bar, items.length || canSave ? h('span', {class: 'sfl'}, 'Saved') : null, chips,
      canSave ? h('button', {class: 'lnk sfadd', type: 'button', onclick: () => saveSheet(f)}, icon('plus'), 'Save this search') : null);
    bar.hidden = !bar.childElementCount;
    return bar;
  }

  function describe(f) {
    const cats = [f.uncategorized ? 'Uncategorized' : null, ...many(f.group).map((g) => `${g} (all)`), ...many(f.category)].filter(Boolean);
    return [f.q ? `"${f.q}"` : null, cats.join(' or '), f.bank, many(f.tag).length ? `tagged ${many(f.tag).join(' or ')}` : null,
      f.kind ? {review: 'needs review', spend: 'spending', income: 'income', transfers: 'transfers'}[f.kind] : null].filter(Boolean).join(' · ');
  }

  function drawBar() {
    const old = document.querySelector('#page .sfbar');
    const fbar = document.querySelector('#page .fbar');
    if (!fbar) return;
    const bar = savedBar();
    if (old) old.replaceWith(bar);
    else (document.querySelector('#page .txchips') || fbar).after(bar);   // under the picks' chips (txfilter.js)
  }

  function saveSheet(f) {
    const picked = [...many(f.group), ...many(f.category), f.uncategorized ? 'Uncategorized' : null].filter(Boolean);
    const name = h('input', {id: 'sf-name', maxlength: '60', 'aria-label': 'Name', placeholder: 'e.g. Amazon this year',
      value: (f.q ? f.q : f.tag ? `Tagged: ${many(f.tag).join(', ')}` : picked.join(', ') || f.bank || '').slice(0, 60)});
    const err = h('p', {class: 'ferr', role: 'alert'});
    const save = async () => {
      try {
        await post('/api/saved-filters', {name: name.value, filters: f});
        closeModal();
        await loadSaved();
        drawBar();
        toast(['Saved ', h('b', {}, name.value.trim()), ' above the table.'], [], {timeout: 3500});
      } catch (e) {
        err.textContent = e.message;
      }
    };
    name.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
    openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Save this search'), closeBtn()),
      h('p', {}, describe(f)),
      h('div', {class: 'flds'}, h('div', {class: 'fld'}, h('label', {for: 'sf-name'}, 'Name'), h('div', {class: 'inp'}, name), err)),
      h('div', {class: 'row'}, h('button', {class: 'btn w', type: 'button', onclick: save}, 'Save'))], {narrow: true, focus: name});
  }

  function confirmDelete(x) {
    modal(`Delete “${x.name}”?`, h('p', {class: 'mut'}, 'Only the saved chip goes. No transaction changes.'), [
      {label: 'Delete', primary: true, onclick: async () => {
        closeModal();
        try {
          await post('/api/saved-filters/delete', {id: x.id});
          await loadSaved();
          drawBar();
        } catch (e) {
          toast(`Could not delete it: ${e.message}`, [], {error: true, timeout: 6000});
        }
      }},
      {label: 'Cancel', onclick: closeModal},
    ]);
  }

  const renderTransactions = RENDER.transactions;
  RENDER.transactions = async (tk) => {
    closeTx(false);
    SAVED = null;   // counts for this window, not the last one shown
    ++savedRequest;
    await renderTransactions(tk);
    if (tk !== S.token) return;
    const search = document.querySelector('#page .fbar .search');
    if (search) {
      search.placeholder = 'Search merchants, notes, tags';
      search.setAttribute('aria-label', 'Search merchants, notes and tags');
    }
    await loadSaved();
    if (tk !== S.token) return;
    drawBar();
  };
  // the filters change in place (search, kind chips, bank): the bar follows them. (TASK-21: one listener on the document
  // from the start, so a search typed while the page or its saved filters are still loading still offers "Save this
  // search"; the listeners each render used to add came after the saved filters had loaded)
  let barTimer = null;
  for (const type of ['input', 'change', 'click']) {
    document.addEventListener(type, (e) => {
      if (!e.target.closest || !e.target.closest('#page .fbar')) return;
      clearTimeout(barTimer);
      barTimer = setTimeout(drawBar, 350);
    });
  }
  // polish P1: the drawer and the picks' chips change the filters from outside the bar; the bar follows every list loaded
  const txLoaded = HOOKS.txLoaded;
  HOOKS.txLoaded = (d) => {
    txLoaded(d);
    const tk = S.token;
    loadSaved().then(() => { if (tk === S.token && S.page === 'transactions') drawBar(); });
  };

  // ---- the transaction sheet -------------------------------------------------------------------------------------
  let OPEN = null;  // {t, state, save} of the sheet on screen
  document.addEventListener('click', (e) => {
    const tr = e.target.closest('#page table.tx tbody tr[data-tid]');
    if (!tr || e.target.closest('select, button, a, input')) return;
    openTx(tr.dataset.tid);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !panel.hidden && !X.$('modal').classList.contains('on')) closeTx(true);
  });
  window.addEventListener('hashchange', () => closeTx(true));

  HOOKS.openTx = (tid) => openTx(tid);   // TASK-21: Back from Track as a bill (a phone) opens the transaction again

  function closeTx(save) {
    if (panel.hidden) return;
    const open = OPEN;
    panel.hidden = true;
    document.body.classList.remove('txopen');
    document.querySelectorAll('#page tr.sel').forEach((tr) => tr.classList.remove('sel'));
    OPEN = null;
    if (save && open) open.save();
  }

  async function openTx(tid) {
    const t = TX.rows.find((r) => r.transaction_id === tid);
    if (!t) return;
    if (OPEN && OPEN.t.transaction_id === tid && !panel.hidden) return;
    closeTx(true);
    if (!TAGS) TAGS = await api('/api/tags').then((d) => d.tags).catch(() => []);
    const cats = await X.ensureCategories();
    document.querySelectorAll('#page tr.sel').forEach((tr) => tr.classList.remove('sel'));
    const tr = document.querySelector(`#page tr[data-tid="${CSS.escape(tid)}"]`);
    if (tr) tr.classList.add('sel');
    if (matchMedia(DOCK).matches) {
      const sheet = txSheet(t, cats, () => closeTx(true));
      fill(panel, sheet.kids);
      panel.hidden = false;
      document.body.classList.add('txopen');
      OPEN = {t, save: sheet.save};
      const first = panel.querySelector('.ib');
      if (first) first.focus();
    } else {
      const sheet = txSheet(t, cats, closeModal);
      openSheet(sheet.kids, {cls: 'txsheet', onClose: () => {
        document.querySelectorAll('#page tr.sel').forEach((x) => x.classList.remove('sel'));
        sheet.save();
      }});
    }
  }

  function txSheet(t, cats, close) {
    const spending = cats.filter((c) => c.spending).map((c) => c.name);
    const canSplit = !t.is_income && !t.is_transfer && !t.pending && cents(t.amount) !== 0;
    const st = {category: t.category, lines: t.split ? t.split.map((s) => ({...s})) : null, note: t.note || '', tags: [...(t.tags || [])]};
    const sec = h('div', {class: 'tsec'});
    const drawCat = () => {
      if (!st.lines) {
        const names = cats.map((c) => c.name);
        if (!names.includes(st.category)) names.push(st.category);
        const sel = HOOKS.catPick({value: st.category, names, label: 'Category'}, (n) => {
          st.category = n;
          drawCat();
          sec.querySelector('.spl.one > :nth-child(2)').focus();   // the new control, where the old one was
        });
        fill(sec, h('div', {class: 'tsh'}, h('b', {}, 'Category')),
          h('div', {class: 'spl one'}, badge(st.category, 's'), sel,
            canSplit ? h('button', {class: 'btn sm ghost', type: 'button', onclick: () => {
              const other = spending.find((n) => n !== st.category) || spending[0];
              st.lines = [{category: spending.includes(st.category) ? st.category : spending[0], amount: null}, {category: other, amount: null}];
              drawCat();
              const first = sec.querySelector('.spl input');
              if (first) first.focus();
            }}, icon('split'), 'Split') : null));
        return;
      }
      const remain = h('div', {class: 'remain', 'aria-live': 'polite'});
      const bar = h('span', {class: 'track'});
      const inputs = [];
      const update = () => {
        const assigned = sum(st.lines, (l) => l.amount || 0);
        const left = r2(t.amount - assigned);
        const ok = cents(left) === 0 && st.lines.every((l) => l.amount && cents(l.amount) !== 0);
        remain.className = `remain ${ok ? 'ok' : 'warn'}`;
        fill(remain, ok ? [icon('checkc'), h('span', {}, `Adds up to ${money2(t.amount)}`)]
          : cents(left) > 0 ? [h('span', {}, 'Left to split'), h('b', {class: 'v'}, money2(left))]
            : cents(left) < 0 ? [h('span', {}, 'Over by'), h('b', {class: 'v'}, money2(-left))]
              : [h('span', {}, 'Every line needs an amount')]);
        fill(bar, h('i', {style: {width: pct(t.amount ? assigned / t.amount : 0), background: ok ? 'var(--ok)' : 'var(--warn)'}}));
      };
      const lineEl = (l, i) => {
        const input = h('input', {type: 'text', inputmode: 'decimal', autocomplete: 'off', placeholder: '0.00',
          value: l.amount != null ? Math.abs(l.amount).toFixed(2) : '', 'aria-label': `Amount for ${catLabel(l.category)}`});
        input.addEventListener('input', () => {
          const n = parseAmount(input.value);
          l.amount = n == null || Number.isNaN(n) ? null : r2(t.amount < 0 ? -Math.abs(n) : n);
          input.toggleAttribute('aria-invalid', Number.isNaN(n));
          update();
        });
        inputs.push(input);
        const sel = h('select', {class: 'cat', 'aria-label': `Category of line ${i + 1}`},
          spending.map((n) => h('option', {value: n, selected: n === l.category, disabled: n !== l.category && st.lines.some((x) => x.category === n)}, catLabel(n))));
        sel.addEventListener('change', () => { l.category = sel.value; drawCat(); });
        return h('div', {class: 'spl'}, badge(l.category, 's'), sel,
          h('label', {class: 'edin'}, h('span', {class: 'pre', 'aria-hidden': 'true'}, '$'), input),
          h('button', {class: 'ib sm', type: 'button', 'aria-label': `Remove the ${catLabel(l.category)} line`, disabled: st.lines.length <= 2,
            onclick: () => { st.lines.splice(i, 1); drawCat(); }}, icon('x')));
      };
      const free = spending.filter((n) => !st.lines.some((l) => l.category === n));
      fill(sec, h('div', {class: 'tsh'}, h('b', {}, 'Split'), h('span', {}, `${plural(st.lines.length, 'line')} · must add up to ${money2(t.amount)}`)),
        st.lines.map(lineEl),
        free.length && st.lines.length < 20 ? h('button', {class: 'lnk addl', type: 'button', onclick: () => {
          st.lines.push({category: free[0], amount: null});
          drawCat();
        }}, icon('plus'), 'Add a line') : null,
        bar, remain,
        h('p', {class: 'hint'}, t.split ? 'Change the lines, or unsplit it to give it one category again.'
          : `The split is saved once the lines add up. Close before that and it stays in ${st.category}.`),
        h('button', {class: 'lnk unsplit', type: 'button', onclick: () => { st.lines = null; drawCat(); }},
          t.split ? 'Unsplit' : 'Don’t split'));
      update();
    };
    drawCat();
    const note = h('textarea', {class: 'txta', id: `txnote-${t.transaction_id}`, rows: '3', maxlength: '2000', placeholder: 'Add a note'});
    note.value = st.note;
    note.addEventListener('input', () => { st.note = note.value; });
    const tagRow = h('div', {class: 'tagrow'});
    const used = h('p', {class: 'hint'});
    const drawTags = () => {
      const adder = h('button', {class: 'addtag', type: 'button', onclick: () => {
        const input = h('input', {class: 'taginp', maxlength: '40', 'aria-label': 'New tag', placeholder: 'tag name'});
        const commit = () => {
          const v = input.value.trim().replace(/^#/, '');
          if (v && !st.tags.some((x) => x.toLowerCase() === v.toLowerCase()) && st.tags.length < 20) st.tags.push(v);
          drawTags();
        };
        input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); commit(); }
          if (e.key === 'Escape') { e.stopPropagation(); drawTags(); }
        });
        input.addEventListener('blur', commit);
        adder.replaceWith(input);
        input.focus();
      }}, icon('plus'), 'Add tag');
      fill(tagRow, st.tags.map((x) => h('span', {class: 'ttag big'}, x, h('button', {type: 'button', 'aria-label': `Remove tag ${x}`,
        onclick: () => { st.tags = st.tags.filter((y) => y !== x); drawTags(); }}, icon('x')))), adder);
      const others = (TAGS || []).map((x) => x.name).filter((n) => !st.tags.some((y) => y.toLowerCase() === n.toLowerCase())).slice(0, 6);
      fill(used, others.length ? ['Used before: ', others.map((n, i) => [i ? ', ' : '', h('button', {class: 'lnk tagpick', type: 'button',
        onclick: () => { st.tags.push(n); drawTags(); }}, n)])] : null);
      used.hidden = !others.length;
    };
    drawTags();
    const kids = [
      h('div', {class: 'shd'}, avatar(t.seed, t.logo_url, t.icon_url, 32), h('h2', {id: 'tx-title'}, t.merchant || t.name || 'Transaction'),
        h('button', {class: 'ib', type: 'button', 'aria-label': 'Close', onclick: close}, icon('x'))),
      h('div', {class: 'txhead'}, bigMoney(Math.abs(t.amount), 's'),
        h('div', {class: 'sub'}, `${t.amount < 0 ? 'Money in · ' : ''}${fmtDate(t.date, true)} · ${instShort(t.institution_name)}${t.account_mask ? ` ••${t.account_mask}` : ''}`
          + (t.pending ? ' · pending' : '')),
        t.name && t.name !== t.merchant ? h('div', {class: 'sub dim'}, `On the statement: ${t.name}`) : null),
      sec,
      HOOKS.txBill ? HOOKS.txBill(t, {close, save: () => save()}) : null,   // TASK-21 (bills.js): Track as a bill
      h('div', {class: 'tsec'}, h('label', {class: 'tsh', for: note.id}, h('b', {}, 'Notes')), note),
      h('div', {class: 'tsec'}, h('div', {class: 'tsh'}, h('b', {}, 'Tags')), tagRow, used),
    ];

    // What changed, saved on close: the category (unsplit), the split (only once it adds up), the note, the tags.
    const save = async () => {
      const inputOpen = tagRow.querySelector('.taginp');
      if (inputOpen && inputOpen.value.trim()) {
        const v = inputOpen.value.trim().replace(/^#/, '');
        if (!st.tags.some((x) => x.toLowerCase() === v.toLowerCase())) st.tags.push(v);
      }
      const done = [];
      const problems = [];
      const wasSplit = Boolean(t.split);
      try {
        if (st.lines) {
          const ok = cents(sum(st.lines, (l) => l.amount || 0)) === cents(t.amount) && st.lines.every((l) => l.amount && cents(l.amount) !== 0);
          const changed = !wasSplit || JSON.stringify(st.lines.map((l) => [l.category, cents(l.amount || 0)]))
            !== JSON.stringify(t.split.map((l) => [l.category, cents(l.amount)]));
          if (changed && ok) {
            const res = await post('/api/transactions/split', {transaction_id: t.transaction_id,
              lines: st.lines.map((l) => ({category: l.category, amount: l.amount}))});
            Object.assign(t, {split: res.split, category_source: 'user', review: false});
            done.push('split');
          } else if (changed) {
            problems.push(`the split doesn't add up to ${money2(t.amount)} yet, so it wasn't saved`);
          }
        } else if (wasSplit) {
          await post('/api/transactions/split', {transaction_id: t.transaction_id, lines: []});
          t.split = null;
          done.push('unsplit');
        }
        if (!st.lines && st.category !== t.category) {
          const res = await post('/api/transactions/category', {transaction_id: t.transaction_id, category: st.category, make_rule: false});
          Object.assign(t, {category: res.category, category_source: res.category_source, review: false});
          done.push('category');
        }
        const noteChanged = (st.note || '').trim() !== (t.note || '');
        const tagsChanged = JSON.stringify([...st.tags].sort()) !== JSON.stringify([...(t.tags || [])].sort());
        if (noteChanged || tagsChanged) {
          const body = {transaction_id: t.transaction_id};
          if (noteChanged) body.note = st.note;
          if (tagsChanged) body.tags = st.tags;
          const res = await post('/api/transactions/meta', body);
          Object.assign(t, {note: res.note, tags: res.tags});
          TAGS = null;
          done.push(noteChanged && tagsChanged ? 'note and tags' : noteChanged ? 'note' : 'tags');
        }
      } catch (e) {
        problems.push(e.message);
      }
      if (done.length) {
        X.loadTx(S.token, true).catch(() => {});  // the list redrawn with it (its tags, its split cell)
        if (done.includes('split') || done.includes('unsplit') || done.includes('category')) loadCounts();
        if (done.some((x) => x.includes('tags'))) loadSaved().then(drawBar);
      }
      if (problems.length) toast(`Not saved: ${problems.join('; ')}.`, [], {error: true, timeout: 7000});
      else if (done.length) toast(['Saved ', h('b', {}, t.merchant || 'the transaction'), `: ${done.join(', ')}.`], [], {timeout: 3000});
    };
    return {kids, save};
  }

});
