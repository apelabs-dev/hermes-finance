/* Hermes Finances: import a file (TASK-18), from Add account > Import a file (and Settings > Data > Import).
 *
 *   drop zone -> (CSV) match the columns: what each column is, 5 sample rows, the date format and what a minus sign
 *   means -> which account each group of rows goes into (a new one, "add as account: <name>", or a manual account you
 *   have) -> Import -> "N new, M duplicates skipped".
 *
 * OFX / QFX files skip the column step (they say what they are). The server does the reading and the counting
 * (POST /api/import/preview, nothing saved) and the import (POST /api/import/commit); rows already there are skipped.
 * Imports go into manual accounts only, never a bank connected through Plaid.
 */
(window.HF_EXT = window.HF_EXT || []).push((HF) => {
  'use strict';
  const {h, fill, icon, post, openSheet, closeModal, closeBtn, backBtn, ring, glyph, plural, fmtDate, RGB, OK} = HF;
  const MAX = 10 * 1024 * 1024;
  const FIELDS = [['', 'Skip'], ['date', 'Date'], ['description', 'Description'], ['amount', 'Amount'], ['debit', 'Money out'],
    ['credit', 'Money in'], ['account', 'Account']];
  const FMT = {'%m/%d/%Y': 'MM/DD/YYYY', '%d/%m/%Y': 'DD/MM/YYYY', '%Y-%m-%d': 'YYYY-MM-DD', '%m/%d/%y': 'MM/DD/YY', '%d/%m/%y': 'DD/MM/YY',
    '%d.%m.%Y': 'DD.MM.YYYY', '%Y/%m/%d': 'YYYY/MM/DD', '%m-%d-%Y': 'MM-DD-YYYY', '%d-%m-%Y': 'DD-MM-YYYY', '%Y%m%d': 'YYYYMMDD',
    '%b %d, %Y': 'Mon DD, YYYY', '%d %b %Y': 'DD Mon YYYY'};
  const fmtName = (f) => FMT[f] || f.replace(/%/g, '');
  const range = (a, b) => (a === b ? fmtDate(a, true) : `${fmtDate(a, true)} – ${fmtDate(b, true)}`);

  HF.HOOKS.addOptions.push(() => HF.opt('filetext', RGB.ok, 'Import a file', "CSV, OFX or QFX from your bank's website · uses no Plaid slot",
    () => HF.openAdd('import')));
  HF.HOOKS.addSteps.import = () => dropSheet();

  const head = (title, back) => h('div', {class: 'shd'}, back ? backBtn(back) : null, h('h2', {id: 'sheet-title'}, title), closeBtn());
  const errBox = () => h('p', {class: 'formerr', role: 'alert'});

  function toBase64(buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  // ---- 1: the drop zone ----------------------------------------------------------------------------------------
  function dropSheet() {
    const err = errBox();
    const input = h('input', {type: 'file', accept: '.csv,.ofx,.qfx,.txt,text/csv', hidden: true, 'aria-label': 'Choose a file'});
    const choose = h('button', {class: 'btn sm ghost', type: 'button', onclick: () => input.click()}, 'Choose file');
    const zone = h('div', {class: 'drop'}, h('span', {class: 'dri'}, icon('upload')), h('b', {}, 'Drop a file here'),
      h('span', {}, 'CSV, OFX or QFX · up to 10 MB'), choose, input);
    const take = async (file) => {
      err.textContent = '';
      if (!file) return;
      if (file.size > MAX) { err.textContent = 'That file is over 10 MB. Download a shorter date range.'; return; }
      choose.disabled = true;
      try {
        const st = {filename: file.name, data: toBase64(await file.arrayBuffer()), targets: {}, limits: {}};
        const p = await post('/api/import/preview', {filename: st.filename, data: st.data});
        Object.assign(st, {mapping: p.mapping, date_format: p.date_format, sign: p.sign});
        mapSheet(st, p);
      } catch (e) {
        err.textContent = e.message;
      } finally {
        choose.disabled = false;
      }
    };
    input.addEventListener('change', () => take(input.files[0]));
    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('over'));
    zone.addEventListener('drop', (e) => { e.preventDefault(); zone.classList.remove('over'); take(e.dataTransfer.files[0]); });
    openSheet([head('Import a file', () => HF.openAdd('home')), h('p', {}, "Download transactions from your bank's website, then drop the file here."),
      zone, err, h('div', {class: 'sub help-line'}, h('span', {class: 'mut'}, 'Where to find the file, and what each format needs'),
        HF.help('Finding your statement', "Look for Download, Export or Statements on the bank's site. OFX and QFX are read "
          + 'as they are; a CSV shows its columns first so you can match them before anything is saved.', {key: 'import-formats'}))],
    {focus: choose});
  }

  // ---- 2: columns (CSV) and accounts -----------------------------------------------------------------------------
  function targetSelect(st, g, accounts, redraw) {
    const opts = [['new:bank', `Add as account: ${HF.acctName(g.label)}`], ['new:credit_card', `Add as card: ${HF.acctName(g.label)}`],
      ...accounts.map((a) => [`id:${a.id}`, `Into ${HF.acctName(a.name)}${a.kind === 'credit_card' ? ' (card)' : ''}`])];
    const cur = g.target.account_id ? `id:${g.target.account_id}` : `new:${g.target.new.kind}`;
    const s = h('select', {class: 'selv', 'aria-label': `Where ${HF.acctName(g.label)} goes`}, opts.map(([v, t]) => h('option', {value: v}, t)));
    s.value = cur;
    s.addEventListener('change', () => {
      const [kind, v] = s.value.split(':');
      st.targets[g.label] = kind === 'id' ? {account_id: Number(v)} : {new: {name: g.label, kind: v}};
      redraw();
    });
    return s;
  }

  function mapSheet(st, p) {
    const err = errBox();
    const body = h('div');
    const go = h('button', {class: 'btn w', type: 'button'}, 'Import');
    let seq = 0;
    const payload = () => {
      const targets = {};
      for (const g of p.groups) {
        const t = st.targets[g.label] || g.target;
        if (t.new && t.new.kind === 'credit_card') targets[g.label] = {new: {...t.new, credit_limit: Number(st.limits[g.label]) || null}};
        else targets[g.label] = t;
      }
      return {filename: st.filename, data: st.data, mapping: st.mapping, date_format: st.date_format, sign: st.sign, targets};
    };
    const refresh = async () => {
      const mine = ++seq;
      err.textContent = '';
      try {
        const q = await post('/api/import/preview', payload());
        if (mine !== seq) return;
        p = q;
        draw();
      } catch (e) {
        if (mine === seq) {
          err.textContent = e.message;
          go.disabled = true;
        }
      }
    };
    const draw = () => {
      const csv = p.kind === 'csv';
      const cols = csv ? p.headers.map((_, i) => Object.keys(st.mapping).find((k) => st.mapping[k] === i) || '') : [];
      const colSel = (i) => {
        const s = h('select', {class: 'colsel', 'aria-label': `Column ${p.headers[i] || i + 1}`}, FIELDS.map(([v, t]) => h('option', {value: v}, t)));
        s.value = cols[i];
        s.addEventListener('change', () => {
          const m = {...st.mapping};
          for (const k of Object.keys(m)) if (m[k] === i) m[k] = null;
          if (s.value) m[s.value] = i;
          st.mapping = m;
          refresh();
        });
        return s;
      };
      const table = csv ? h('div', {class: 'mapw'}, h('table', {class: 'map'},
        h('thead', {}, h('tr', {}, p.headers.map((c) => h('th', {}, c)))),
        h('tbody', {}, h('tr', {class: 'maprow'}, p.headers.map((_, i) => h('td', {}, colSel(i)))),
          p.sample.map((r) => h('tr', {}, r.map((v, i) => h('td', {class: cols[i] === 'amount' || cols[i] === 'debit' || cols[i] === 'credit' ? 'num v' : ''}, v))))))) : null;
      const detect = csv ? h('div', {class: 'detect'},
        p.date_formats.length > 1
          ? h('span', {}, 'Dates read as', h('select', {class: 'selv', 'aria-label': 'Date format', onchange: (e) => { st.date_format = e.target.value; refresh(); }},
            p.date_formats.map((f) => h('option', {value: f, selected: f === st.date_format}, fmtName(f)))))
          : h('span', {}, icon('check'), `Dates read as ${fmtName(st.date_format || '')}`),
        st.sign === 'split' ? h('span', {}, icon('check'), 'Money out and money in are separate columns')
          : h('span', {}, h('select', {class: 'selv', 'aria-label': 'What a minus sign means', onchange: (e) => { st.sign = e.target.value; refresh(); }},
            h('option', {value: 'minus_out', selected: st.sign === 'minus_out'}, 'A minus sign means money out'),
            h('option', {value: 'minus_in', selected: st.sign === 'minus_in'}, 'A minus sign means money in')))) : null;
      const groups = p.groups.map((g) => {
        const t = st.targets[g.label] || g.target;
        const card = t.new && t.new.kind === 'credit_card';
        const limit = card ? h('span', {class: 'inp tin'}, h('span', {class: 'pre', 'aria-hidden': 'true'}, HF.CUR.sym),
          h('input', {'aria-label': `Credit limit of ${HF.acctName(g.label)}`, inputmode: 'decimal', value: st.limits[g.label] || '', placeholder: 'Limit',
            oninput: (e) => { st.limits[g.label] = HF.parseAmount(e.target.value); go.disabled = !(st.limits[g.label] > 0); }})) : null;
        return h('div', {class: 'li mapacct'}, h('span', {class: 'grow'}, h('b', {}, HF.acctName(g.label)),
          h('span', {}, `${plural(g.rows, 'row')} · ${g.new} new${g.skipped ? ` · ${g.skipped} already here` : ''}`)),
        targetSelect(st, g, p.accounts, refresh), limit);
      });
      go.textContent = p.new ? `Import ${plural(p.new, 'row')}` : 'Nothing new to import';
      go.disabled = !p.new || p.groups.some((g) => { const t = st.targets[g.label] || g.target; return t.new && t.new.kind === 'credit_card' && !(st.limits[g.label] > 0); });
      fill(body, h('div', {class: 'filec'}, HF.ibadge('filetext', RGB.ok, 's'), h('span', {class: 'grow'}, h('b', {}, st.filename),
        h('span', {}, `${plural(p.rows, 'row')} · ${range(p.first, p.last)}`))),
      table, detect, h('div', {class: 'gh'}, p.groups.length > 1 ? 'Accounts in this file' : 'Goes into', h('span', {class: 'ln'})), groups,
      p.skipped ? h('p', {class: 'hint'}, `${plural(p.skipped, 'row')} already here (same date, amount and description${p.kind === 'ofx' ? ', or the same bank ID' : ''}) will be skipped.`) : null);
    };
    go.addEventListener('click', async () => {
      go.disabled = true;
      err.textContent = '';
      try {
        const r = await post('/api/import/commit', payload());
        doneSheet(st, r);
      } catch (e) {
        err.textContent = e.message;
        go.disabled = false;
      }
    });
    openSheet([head(p.kind === 'csv' ? 'Match the columns' : 'Import a file', dropSheet), body, err,
      h('div', {class: 'row'}, go, h('button', {class: 'btn ghost', type: 'button', onclick: dropSheet}, 'Back'))], {cls: 'wide', focus: go});
    draw();
  }

  // ---- 3: done -------------------------------------------------------------------------------------------------
  function doneSheet(st, r) {
    const month = r.last.slice(0, 7);
    openSheet([head('Imported'),
      h('div', {class: 'impdone'}, ring(1, OK, 72, 7, glyph('check', 2.6)), h('div', {}, h('b', {class: 'v'},
        `${plural(r.new, 'new row')}${r.skipped ? `, ${plural(r.skipped, 'duplicate')} skipped` : ''}`),
      h('span', {}, `${st.filename} · ${range(r.first, r.last)}`))),
      h('div', {class: 'fg'}, r.accounts.map((a) => h('div', {}, h('span', {}, HF.acctName(a.account.name)), h('b', {}, `${a.new} new · ${a.skipped} skipped`)))),
      h('p', {class: 'hint'}, 'They are in accounts added by you (manual); categories are filled in the usual way.'),
      h('div', {class: 'row'}, h('button', {class: 'btn w', type: 'button', onclick: () => { closeModal(); location.hash = `#/transactions?month=${month}`; }},
        'See them in Transactions'), h('button', {class: 'btn ghost', type: 'button', onclick: dropSheet}, 'Import another'))],
    {onClose: () => HF.refresh()});
  }
});
