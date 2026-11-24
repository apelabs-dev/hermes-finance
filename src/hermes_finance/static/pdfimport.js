/* Hermes Finances · importing a bank statement PDF (epic 0.5).
 *
 *   drop a PDF -> "Reading your statement" (on this computer) -> "Check the statement": the printed balances, the
 *   proof that opening + rows = closing, every row, and where it goes -> Import.
 *
 * A statement whose layout the parser can't use is refused with a code and a plain reason. A fixable refusal offers
 * "Teach it": four questions about the printed layout (columns, date order, how money out is written, where the
 * balance lives), with the printed columns and an example cell under each, and the check running live until it adds
 * up. The fourth answer may ask for the two balances the statement prints. A layout that was taught is saved once
 * and matched by fingerprint next time.
 *
 * The one thing that leaves this computer is the AI layout proposal, and only when the person presses its own button
 * on its own privacy screen. It sends the masked layout shape the parser prints: no amounts, names or account
 * numbers, never the file or a password. A proposal is only ever a suggestion: it still has to pass the same check
 * here, and an import never calls the model.
 *
 * Everything is built with h()/textContent: nothing read from the file becomes markup.
 */
(window.HF_EXT = window.HF_EXT || []).push((HF) => {
  'use strict';
  const {h, fill, icon, ibadge, post, openSheet, closeModal, closeBtn, backBtn, plural, fmtDate, RGB, acctName,
    CUR, LOC} = HF;

  const MAX = 10 * 1024 * 1024;
  const OKR = RGB.ok, BADR = RGB.bad, ACCR = RGB.acc, GREYR = RGB.grey;
  const INSTALL = "pip install 'hermes-finance[pdf]'";

  const ROLE_OPTS = [['', 'Skip'], ['date', 'Date'], ['desc', 'Description'], ['debit', 'Money out'],
    ['credit', 'Money in'], ['balance', 'Balance']];
  const ROLE_NAME = {'': 'Skip', date: 'Date', desc: 'Description', debit: 'Money out', credit: 'Money in',
    amount: 'Amount', balance: 'Balance'};
  const SIGNS = [['explicit', 'In its own column', 'Withdrawals and Deposits are separate'],
    ['unsigned_is_credit', 'A minus sign', '\u221284.12'],
    ['unsigned_is_credit', 'A minus after the number', '84.12\u2212'],
    ['explicit', 'Marked DR or CR', '84.12 DR']];
  const BALANCES = [['running', 'A running balance column', 'Checked after every row'],
    ['totals', 'Opening and closing only', 'Checked once, at the end'],
    ['typed', 'I will type them', 'The statement does not print them']];
  // Where a row begins. "A date or an amount" is the engine's default cut; "a new date" keeps a row whose date is on
  // its own line and the money on the next (the IN shape in docs/pdf-teach-once.md: the only layout where a teach is
  // measured to change the outcome, and it needs row_start=date to do it).
  const ROWSTARTS = [['date_or_amount', 'A date or an amount'], ['date', 'A new date']];
  const ROW_TRY = new Set(['does_not_reconcile', 'unreadable_date', 'no_rows']);

  // A refusal's title, its file line, and the copy under it. The message from the server is used for every refusal
  // it doesn't draw (the frame's own words for the six it does).
  const TITLE = {
    no_text_layer: 'Can\u2019t read this PDF', pdf_unreadable: 'Can\u2019t read this PDF', too_big: 'Can\u2019t read this PDF',
    no_header: 'Can\u2019t read this PDF', no_rows: 'Can\u2019t read this PDF', unreadable_date: 'Can\u2019t read this PDF',
    no_period: 'Can\u2019t read this PDF',
    credit_card: 'Can\u2019t import this PDF', multi_account: 'Can\u2019t import this PDF',
    template_invalid: 'Can\u2019t import this PDF',
    no_balances: 'Can\u2019t check this PDF', does_not_reconcile: 'Can\u2019t check this PDF',
    ai_failed: 'Can\u2019t check this PDF', ai_unavailable: 'Can\u2019t check this PDF',
    missing_extra: 'PDF import isn\u2019t installed',
  };
  const SUB = {
    no_text_layer: 'A picture of a page', credit_card: 'Credit card statement', multi_account: 'More than one account',
    no_balances: 'No balances printed', does_not_reconcile: 'The rows don\u2019t add up', unreadable_date: 'Dates not found',
    no_period: 'No year or period', no_header: 'The table wasn\u2019t recognized', no_rows: 'No transactions found',
    template_invalid: 'Saved layout doesn\u2019t fit', pdf_unreadable: 'Not a readable PDF', too_big: 'Over 10 MB',
    ai_unavailable: 'AI isn\u2019t available', ai_failed: 'The AI\u2019s layout doesn\u2019t add up',
    missing_extra: 'An add-on is missing',
  };
  const WORD = {
    no_text_layer: 'This PDF has no text in it, only a picture of a page. Download the statement from your bank as CSV, '
      + 'OFX or QFX.',
    credit_card: 'This is a credit card statement. Download the activity from your card issuer as CSV, OFX or QFX.',
    multi_account: 'This statement covers more than one account. Download each account as CSV, OFX or QFX.',
    no_balances: 'This statement does not print balances, so the rows can\u2019t be checked. Teach it the layout, or use '
      + 'CSV, OFX or QFX.',
    does_not_reconcile: 'The rows do not add up to the closing balance, so nothing was imported. Teach it the layout, '
      + 'or use CSV, OFX or QFX.',
    unreadable_date: 'The dates in this statement could not be read. Teach it the layout, or use CSV, OFX or QFX.',
    no_period: 'The dates or the statement period in this PDF could not be read. Teach it the layout, or use CSV, OFX '
      + 'or QFX.',
    ai_failed: 'The AI\u2019s layout does not add up to the closing balance, so nothing was imported. Teach it the '
      + 'layout, or use CSV, OFX or QFX.',
    ai_unavailable: 'The AI layout proposal isn\u2019t available. Turn on a local Claude Code or Codex brain in '
      + 'Settings \u2192 AI provider, or teach it the layout.',
  };
  const FIXABLE = new Set(['no_header', 'unreadable_date', 'no_period', 'does_not_reconcile', 'no_balances',
    'ai_failed']);
  const ASK_AI = new Set(['no_header', 'no_rows', 'no_balances', 'does_not_reconcile', 'unreadable_date', 'no_period']);

  // ---- 0: shared bits ------------------------------------------------------------------------------------------
  const S = {file: null, data: '', password: '', preview: null, refusal: null};
  let ticket = 0;  // every sheet opened from here; a reply whose ticket is stale must never redraw anything

  // The file, its bytes and a password live only as long as the flow: an import, a close (x, Escape, the backdrop)
  // or Choose another file lets them go, so nothing of the statement outlives the sheet.
  function forget() {
    S.file = null; S.data = ''; S.password = ''; S.preview = null; S.refusal = null;
  }

  function open(kids, opts = {}) {
    const extra = opts.release;
    const done = opts.onClose;
    openSheet(kids, {...opts, release: () => { ticket++; if (extra) extra(); },
      onClose: () => { forget(); if (done) done(); }});
    return ++ticket;
  }
  const dropSheet = () => { forget(); HF.openAdd('import'); };
  const head = (title, back) => h('div', {class: 'shd'}, back ? backBtn(back) : null,
    h('h2', {id: 'sheet-title'}, title), closeBtn());
  const fileChip = (name, sub, extra) => h('div', {class: 'filec'}, ibadge('filetext', OKR, 's'),
    h('span', {class: 'grow'}, h('b', {}, name), sub ? h('span', {}, sub) : null), extra || null);
  const btn = (label, cls, onclick) => h('button', {class: 'btn' + (cls ? ` ${cls}` : ''), type: 'button', onclick}, label);
  const rowOf = (...kids) => h('div', {class: 'row pi-actions'}, kids);
  // The statement's own cents, always: the app drops ".00" from a whole amount, the proof line never does.
  const amt = (n, code) => {
    const v = Number(n) || 0;
    const c = code || CUR.code;
    const s = new Intl.NumberFormat(LOC, {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(Math.abs(v));
    return (v < 0 ? '-' : '') + HF.currencyPrefix(c) + s;
  };
  const errLine = () => h('p', {class: 'formerr', role: 'alert'});

  const kb = (n) => (n >= 1024 * 1024 ? `${(n / (1024 * 1024)).toFixed(n >= 10 * 1024 * 1024 ? 0 : 1)} MB`
    : `${Math.max(1, Math.round(n / 1024))} KB`);
  const span = (a, b) => (!a || !b ? '' : a === b ? fmtDate(a, true)
    : a.slice(0, 4) !== b.slice(0, 4) ? `${fmtDate(a, true)} \u2013 ${fmtDate(b, true)}`
      : `${fmtDate(a)} \u2013 ${fmtDate(b, true)}`);
  const rowMoney = (n, code) => (n < 0 ? `+${amt(-n, code)}` : `\u2212${amt(n, code)}`);  // app sign: < 0 is money in

  function toBase64(buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  // ---- 1: the file arrives -------------------------------------------------------------------------------------
  function start(file) {
    S.file = file || null; S.data = ''; S.password = ''; S.preview = null; S.refusal = null;
    if (!S.file) return;
    if (S.file.size > MAX) {
      refusalSheet({code: 'too_big', message: 'That file is over 10 MB. Download a shorter date range from your bank.'});
      return;
    }
    const mine = reading();
    S.file.arrayBuffer().then((buf) => {
      if (mine !== ticket) return;
      S.data = toBase64(buf);
      return ask(mine);
    }).catch(() => {
      if (mine === ticket) refusalSheet({code: 'pdf_unreadable', message: 'That file could not be read.'});
    });
  }

  function reading() {
    return open([head('Reading your statement', dropSheet), fileChip(S.file.name, kb(S.file.size)),
      h('div', {class: 'pi-read', role: 'status'}, h('span', {class: 'grow'}, 'Reading on this computer')),
      h('div', {class: 'pi-bar', 'aria-hidden': 'true'}, h('i', {style: {width: '38%'}})),
      rowOf(btn('Cancel', 'ghost', closeModal))]);
  }

  async function ask(mine, extra) {
    const body = {filename: S.file.name, data: S.data, ...(extra || {})};
    if (S.password) body.password = S.password;
    try {
      const p = await post('/api/import/preview', body);
      if (mine !== ticket) return;
      S.preview = p;
      checkSheet(p);
    } catch (e) {
      if (mine !== ticket) return;
      route(e);
    }
  }

  function route(e) {
    const code = e.code || 'pdf_unreadable';
    const refusal = {code, message: e.message || WORD[code] || SUB[code] || 'This PDF could not be read.',
      detail: e.detail || null, proposal: e.proposal || null};
    S.refusal = refusal;
    if (code === 'password_required' || code === 'password_wrong') passwordSheet(code === 'password_wrong');
    else if (code === 'missing_extra') extraSheet();
    else refusalSheet(refusal);
  }

  // ---- 2: the check (a statement that adds up) -----------------------------------------------------------------
  function checkSheet(p, keep) {
    const g = (p.groups || [])[0] || {label: 'Statement', rows: p.rows, new: p.new, skipped: p.skipped};
    const pdf = p.pdf || {};
    let target = (keep && keep.target) || g.target || {new: {name: g.label, kind: 'bank'}};
    const slot = h('div');
    const go = h('button', {class: 'btn w', type: 'button'}, 'Import');
    const err = errLine();
    const goDisabled = () => { go.disabled = !p.new; };
    go.addEventListener('click', async () => {
      go.disabled = true;
      err.textContent = '';
      const body = {filename: S.file.name, data: S.data, targets: {[g.label]: target}};
      if (pdf.template && pdf.template.config) {
        body.template = pdf.template.config;
        body.template_source = pdf.template.source;
      }
      if (S.password) body.password = S.password;
      try {
        doneSheet(await post('/api/import/commit', body));
      } catch (e) {
        err.textContent = e.message;
        goDisabled();
      }
    });
    const draw = () => {
      go.textContent = p.new ? `Import ${plural(p.new, 'row')}` : 'Nothing new to import';
      goDisabled();
      const accounts = p.accounts || [];
      const opts = [['new:bank', `Add as account: ${acctName(g.label)}`],
        ...accounts.map((a) => [`id:${a.id}`, `Into ${acctName(a.name)}${a.kind === 'credit_card' ? ' (card)' : ''}`])];
      const cur = target.account_id ? `id:${target.account_id}` : `new:${(target.new && target.new.kind) || 'bank'}`;
      const sel = h('select', {class: 'selv', 'aria-label': 'Where it goes'}, opts.map(([v, t]) => h('option', {value: v}, t)));
      sel.value = cur;
      sel.addEventListener('change', async () => {
        const [kind, v] = sel.value.split(':');
        target = kind === 'id' ? {account_id: Number(v)} : {new: {name: g.label, kind: v}};
        sel.disabled = true;
        try {
          const q = await post('/api/import/preview', {filename: S.file.name, data: S.data,
            targets: {[g.label]: target}, ...(S.password ? {password: S.password} : {})});
          if (!slot.isConnected) return;
          p = q;
          draw();
        } catch (e) {
          err.textContent = e.message;
          sel.disabled = false;
        }
      });
      const code = pdf.currency || CUR.code;
      const listed = p.preview_rows || [];
      const all = (p.preview_rows_total || listed.length) <= listed.length;
      const allRows = p.groups && p.groups.length ? g.rows : p.rows;
      fill(slot, fileChip(S.file.name, `${plural(allRows, 'row')} · ${span(p.first, p.last)}`),
        h('div', {class: 'gh'}, 'Does it add up', h('span', {class: 'ln'})),
        h('div', {class: 'srow tight'}, h('span', {class: 'grow'}, h('b', {}, 'Opening balance')),
          h('b', {class: 'v'}, amt(Number(pdf.opening), code))),
        h('div', {class: 'srow tight'}, h('span', {class: 'grow'},
          h('b', {}, `Money in${all ? ` · ${plural(listed.filter((r) => r.amount < 0).length, 'row')}` : ''}`)),
        h('b', {class: 'v pi-in'}, `+${amt(p.preview_in || 0, code)}`)),
        h('div', {class: 'srow tight'}, h('span', {class: 'grow'},
          h('b', {}, `Money out${all ? ` · ${plural(listed.filter((r) => r.amount > 0).length, 'row')}` : ''}`)),
        h('b', {class: 'v'}, `\u2212${amt(p.preview_out || 0, code)}`)),
        h('div', {class: 'srow tight'}, h('span', {class: 'grow'}, h('b', {}, 'Closing balance'),
          h('span', {}, 'Same as the statement prints')),
        h('span', {class: 'pi-pair'}, h('span', {class: 'pill sm'}, icon('check'), 'Adds up'),
          h('b', {class: 'v'}, amt(Number(pdf.closing), code)))),
        h('div', {class: 'gh'}, 'Every row', h('span', {class: 'ln'})),
        h('div', {class: 'mapw pi-rows'}, h('table', {class: 'map'},
          h('thead', {}, h('tr', {}, h('th', {}, 'Date'), h('th', {}, 'Description'), h('th', {class: 'num'}, 'Amount'))),
          h('tbody', {}, listed.map((r) => h('tr', {}, h('td', {}, fmtDate(r.date)), h('td', {}, r.description),
            h('td', {class: 'num'}, rowMoney(r.amount, code))))))),
        all ? null : h('p', {class: 'pi-hint'},
          `Showing the first ${listed.length} of ${plural(p.preview_rows_total, 'row')}. All of them are checked.`),
        h('div', {class: 'gh'}, 'Where it goes', h('span', {class: 'ln'})),
        h('div', {class: 'li mapacct'}, h('span', {class: 'grow'}, h('b', {}, acctName(g.label)),
          h('span', {}, `${plural(g.rows, 'row')} · ${g.new} new${g.skipped ? ` · ${g.skipped} already here` : ''}`)), sel));
    };
    open([head('Check the statement', dropSheet), slot, err, rowOf(go, btn('Back', 'ghost', dropSheet))],
      {cls: 'wide', focus: go});
    draw();
  }

  // ---- 3: a refusal --------------------------------------------------------------------------------------------
  function refusalSheet(r) {
    const code = r.code;
    const title = TITLE[code] || 'Can\u2019t read this PDF';
    const detail = r.detail || {columns: [], samples: [], row_count: 0};
    const teachable = (detail.columns || []).length > 0 && FIXABLE.has(code);
    const askable = ASK_AI.has(code) && !r.noAi;
    const message = WORD[code] || r.message;
    open([head(title, dropSheet), fileChip(S.file.name, SUB[code] || 'Not readable'),
      h('p', {class: 'formerr', role: 'alert'}, message),
      rowOf(teachable ? h('button', {class: 'btn w', type: 'button', onclick: () => teachSheet({
        detail, source: 'user', proposal: r.proposal, prev: () => refusalSheet(r),
        rows: detail.row_count, refusal: r,
      })}, 'Teach it') : null,
      h('button', {class: 'btn w', type: 'button', onclick: dropSheet}, 'Choose another file')),
      askable ? rowOf(h('button', {class: 'btn ghost pi-wide', type: 'button', onclick: () => aiSheet(r)},
        icon('sparkles'), '\u00a0Ask AI to suggest the layout')) : null]);
  }

  // ---- 4: the password -----------------------------------------------------------------------------------------
  function passwordSheet(wrong) {
    const input = h('input', {type: 'password', id: 'pdf-password', 'aria-label': 'PDF password', autocomplete: 'off',
      spellcheck: 'false'});
    if (S.password) input.value = S.password;
    const err = errLine();
    if (wrong) err.textContent = 'That password did not open the file. Try again.';
    const go = h('button', {class: 'btn w', type: 'button'}, 'Open statement');
    const sheet = [head('This PDF is locked', dropSheet), fileChip(S.file.name, 'Locked with a password'),
      h('div', {class: 'fld'}, h('label', {for: 'pdf-password'}, 'PDF password'), h('div', {class: 'inp'}, input)),
      h('p', {class: 'pi-hint'}, 'Used once to open this file on this computer. Never saved.'), err,
      rowOf(go, btn('Back', 'ghost', dropSheet))];
    let mine = 0;
    const submit = async () => {
      if (!input.value) { err.textContent = 'Type the password the statement was saved with.'; input.focus(); return; }
      go.disabled = true;
      err.textContent = '';
      try {
        const p = await post('/api/import/preview', {filename: S.file.name, data: S.data, password: input.value});
        if (mine !== ticket) return;
        S.password = input.value;
        checkSheet(p);
      } catch (e) {
        if (mine !== ticket) return;
        if (e.code === 'password_wrong' || e.code === 'password_required') {
          S.password = input.value;
          passwordSheet(true);
          return;
        }
        S.password = input.value;
        route(e);
      }
    };
    go.addEventListener('click', submit);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    mine = open(sheet, {focus: input});
  }

  // ---- 5: the missing add-on -----------------------------------------------------------------------------------
  function extraSheet() {
    const input = h('input', {type: 'text', readonly: true, value: INSTALL, 'aria-label': 'Install command',
      id: 'pdf-install'});
    const copy = h('button', {class: 'btn sm ghost', type: 'button', 'aria-label': 'Copy the command'}, icon('copy'));
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(INSTALL);
        copy.replaceChildren(icon('check'));
        copy.setAttribute('aria-label', 'Copied');
      } catch (e) {
        input.select();
      }
    });
    open([head('PDF import isn\u2019t installed', dropSheet), fileChip(S.file.name, kb(S.file.size)),
      h('p', {class: 'formerr', role: 'alert'}, 'PDF import is an optional add-on and isn\u2019t installed. Install it, '
        + 'restart Hermes Finance, then drop the file again.'),
      h('div', {class: 'pi-codeline'}, h('div', {class: 'inp code'}, input), copy),
      h('p', {class: 'pi-hint'}, 'Running from a source checkout? Use uv sync --extra pdf.'),
      h('p', {class: 'pi-hint'}, 'Or download the statement as CSV, OFX or QFX. Those work now.'),
      rowOf(h('button', {class: 'btn w', type: 'button', onclick: dropSheet}, 'Choose another file'))], {focus: copy});
  }

  // ---- 6: the AI opt-in ----------------------------------------------------------------------------------------
  function aiSheet(r) {
    // What leaves is shown word for word before Send can be pressed: the server works out every prompt the AI tries
    // could send (ai_view, nothing sent), and this screen shows the first in full and each line a later try adds.
    const sentLine = h('span', {}, 'Working out exactly what would be sent\u2026');
    const slot = h('div', {class: 'pi-sent'});
    const send = h('button', {class: 'btn w', type: 'button', disabled: true, onclick: () => askAI(r)},
      'Send layout shape');
    const mine = open([head('Ask AI to suggest the layout', () => refusalSheet(r)), fileChip(S.file.name,
      'Can\u2019t read this layout yet'),
    h('div', {class: 'pi-priv'},
      h('div', {class: 'gh'}, 'Sent', h('span', {class: 'ln'})),
      h('ul', {class: 'yes'}, h('li', {}, icon('check'), sentLine)),
      h('div', {class: 'gh'}, 'Never sent', h('span', {class: 'ln'})),
      h('ul', {class: 'no'}, h('li', {}, icon('x'), h('span', {}, 'Amounts, names or account numbers')),
        h('li', {}, icon('x'), h('span', {}, 'The PDF itself, or your password')))),
    slot,
    h('p', {class: 'pi-hint'}, 'The AI only suggests which column is which. The layout still has to add up here.'),
    rowOf(send, btn('Cancel', 'ghost', () => refusalSheet(r)))], {focus: null});
    post('/api/import/preview', {filename: S.file.name, data: S.data, ai_view: true,
      ...(S.password ? {password: S.password} : {})}).then((v) => {
      if (mine !== ticket) return;
      const prompts = ((v && v.ai_view && v.ai_view.prompts) || []).map(String);
      if (!prompts.length) {
        sentLine.textContent = 'Nothing (see below)';
        fill(slot, h('p', {class: 'formerr', role: 'alert'}, 'Nothing on this statement can be sent safely, so the AI '
          + 'can\u2019t be asked. Teach it the layout instead.'));
        return;
      }
      const seen = new Set(prompts[0].split('\n'));
      const more = [];
      for (const p of prompts.slice(1)) {
        for (const line of p.split('\n')) if (line.trim() && !seen.has(line)) { seen.add(line); more.push(line); }
      }
      sentLine.textContent = `Up to ${plural(prompts.length, 'request')}, word for word below: these instructions, and `
        + 'the statement\u2019s words as positions and shapes (digits as 9, uncommon words as x)';
      fill(slot, h('pre', {class: 'pi-mask', tabindex: '0', 'aria-label': 'The first request, exactly as sent'},
        prompts[0]),
      more.length ? [h('p', {class: 'pi-note'}, 'Later tries also send these lines:'),
        h('pre', {class: 'pi-mask', tabindex: '0', 'aria-label': 'Lines later tries add'}, more.join('\n'))] : null);
      send.disabled = false;
    }).catch((e) => { if (mine === ticket) route(e); });
    return mine;
  }

  async function askAI(r) {
    const mine = open([head('Asking the AI', () => refusalSheet(r)), fileChip(S.file.name, 'Layout shape sent'),
      h('div', {class: 'pi-read', role: 'status'}, h('span', {class: 'grow'}, 'Waiting for a suggestion')),
      h('div', {class: 'pi-bar', 'aria-hidden': 'true'}, h('i', {style: {width: '38%'}})),
      rowOf(btn('Cancel', 'ghost', closeModal))]);
    try {
      const p = await post('/api/import/preview', {filename: S.file.name, data: S.data, ai_template: true,
        ...(S.password ? {password: S.password} : {})});
      if (mine !== ticket) return;
      S.preview = p;
      teachSheet({detail: p.pdf.detail || {columns: [], samples: [], row_count: 0}, source: 'ai',
        proposal: p.pdf.proposal || null, prev: () => aiSheet(r), rows: p.rows, preview: p});
    } catch (e) {
      if (mine !== ticket) return;
      if (e.code === 'password_wrong' || e.code === 'password_required') { passwordSheet(e.code === 'password_wrong'); return; }
      if (e.code === 'missing_extra') { extraSheet(); return; }
      // ai_failed's own detail is only the candidates' outcomes: Teach it starts from the statement's printed columns
      // (the first refusal's detail), with the AI's roles on them; with no printed header, from the AI's columns.
      const cols = (d) => !!(d && (d.columns || []).length);
      const detail = cols(r.detail) ? r.detail : cols(e.detail) ? e.detail : fromProposal(e.proposal, r.detail);
      refusalSheet({code: e.code || 'ai_failed', message: e.message, detail: detail || r.detail || null,
        proposal: e.proposal || null, noAi: true});
    }
  }

  function fromProposal(prop, detail) {
    if (!prop || !(prop.columns || []).length) return null;
    return {...(detail || {}), header_words: prop.header_words || [], samples: [],
      columns: prop.columns.map((c) => ({role: c.role, label: '', x0: c.x0, x1: c.x1}))};
  }

  // ---- 7: teach this layout ------------------------------------------------------------------------------------
  function teachSheet(from) {
    const detail = from.detail || {columns: [], samples: [], row_count: 0};
    const prop = from.proposal || null;
    const T = {
      step: 1, detail, rows: from.rows || detail.row_count || 0, source: from.source || 'user', base: prop,
      prev: from.prev || (() => refusalSheet(S.refusal || {code: 'does_not_reconcile'})),
      cols: (detail.columns || []).map((c, i) => ({
        role: roleFor(prop, c) || c.role || '',
        label: c.label || ROLE_NAME[c.role] || `Column ${i + 1}`,
        samples: ((detail.samples || [])[i] || []).slice(0, 3),
        x0: c.x0, x1: c.x1,
        parsed: c.role,  // `amount` (a signed column) is not in the drawn six: offered only when the parser used it
      })),
      order: (prop && prop.date_order) || detail.date_order || null,   // the server's resolved order; else step 2 asks
      sign: normalSign(prop && prop.amount_sign),
      signKey: 0,
      balance: (detail.columns || []).some((c) => c.role === 'balance') ? 'running' : 'totals',
      rowStart: (prop && prop.row_start) || 'date_or_amount', rowAuto: false, rowTried: false,
      typed: {opening: '', closing: ''}, year: '', teach: true, checking: false, check: null, seq: 0, alive: true,
      hint: '',
    };
    T.signKey = Math.max(0, SIGNS.findIndex(([v]) => v === T.sign));
    const slot = h('div');
    const err = errLine();
    const draw = (focus) => {
      fill(slot, h('div', {class: 'pi-steps', 'aria-label': `Step ${T.step} of 4`}, h('span', {}, `Step ${T.step} of 4`),
        [1, 2, 3, 4].map((k) => h('i', {class: k <= T.step ? 'on' : ''}))),
      stepBody(T, draw), checkLine(T),
      T.rowAuto && T.check && T.check.ok ? h('p', {class: 'pi-hint'}, 'Each row starts at its date: the amounts sit '
        + 'on the line below the date.') : null, err, actions(T, draw));
      if (focus) {
        const first = slot.querySelector('.pi-cols select, .pi-opts .pi-opt, .pi-opts input');
        if (first) first.focus();
      }
    };
    T.draw = draw;
    T.recheck = () => runCheck(T);
    T.recheckSoon = () => { clearTimeout(T.timer); T.timer = setTimeout(() => runCheck(T), 350); };
    const meta = T.source === 'ai' ? `Suggested by AI · ${plural(T.rows, 'row')}` : `First time seeing this layout · ${plural(T.rows, 'row')}`;
    const tag = T.source === 'ai' ? h('span', {class: 'pi-tag'}, icon('sparkles'), 'AI layout') : null;
    open([head('Teach this layout', () => (T.step > 1 ? goStep(T, T.step - 1, draw) : T.prev())),
      fileChip(S.file.name, meta, tag), slot], {release: () => { T.alive = false; T.seq++; }});
    draw(true);
  }

  // Any change to the answers drops a check that was made against the old ones, and cancels one still out.
  function touch(T) {
    T.check = null;
    T.hint = '';
    T.rowAuto = false;
    T.rowTried = false;   // a different answer gets its own one look at the other row cut
    T.seq++;
  }

  function normalSign(sign) {
    return sign === 'unsigned_is_debit' || sign === 'debit_positive' || sign === 'credit_positive'
      ? 'unsigned_is_credit' : sign || 'explicit';
  }

  function roleFor(prop, col) {
    const cols = (prop && prop.columns) || [];
    const hit = cols.find((c) => c.x0 < col.x1 && c.x1 > col.x0) || null;
    return hit ? hit.role : null;
  }

  function goStep(T, step, draw) {
    touch(T);
    T.step = step;
    draw(true);
    if (step === 4) T.recheck();
  }

  function actions(T, draw) {
    const back = T.step > 1 ? () => goStep(T, T.step - 1, draw) : T.prev;
    if (T.step < 4) {
      return rowOf(h('button', {class: 'btn w', type: 'button', onclick: () => {
        const why = whyNot(T);
        if (why) { T.hint = why; draw(false); return; }
        goStep(T, T.step + 1, draw);
      }}, 'Next'), btn('Back', 'ghost', back));
    }
    const go = h('button', {class: 'btn w', type: 'button'}, T.check && T.check.ok
      ? `Save and import ${plural(T.check.p.new, 'row')}` : 'Save and import');
    go.disabled = !(T.check && T.check.ok && T.check.p.new > 0);
    go.addEventListener('click', () => commit(T, go));
    return rowOf(go, btn('Back', 'ghost', back));
  }

  function stepBody(T, draw) {
    if (T.step === 1) return stepColumns(T, draw);
    if (T.step === 2) return stepDates(T, draw);
    if (T.step === 3) return stepSigns(T, draw);
    return stepBalance(T, draw);
  }

  function stepColumns(T, draw) {
    const rowSel = h('select', {class: 'selv', 'aria-label': 'Where a row starts'}, ROWSTARTS.map(([v, t]) =>
      h('option', {value: v}, t)));
    rowSel.value = T.rowStart;
    rowSel.addEventListener('change', () => {
      T.rowStart = rowSel.value;
      touch(T);
      draw(false);
    });
    return h('div', {class: 'pi-cols'}, T.cols.map((c) => {
      const opts = c.parsed === 'amount' && !ROLE_OPTS.some(([v]) => v === 'amount')
        ? ROLE_OPTS.concat([['amount', 'Amount']]) : ROLE_OPTS;
      const sel = h('select', {class: 'selv', 'aria-label': `Column ${c.label}`},
        opts.map(([v, t]) => h('option', {value: v}, t)));
      sel.value = c.role || '';
      sel.addEventListener('change', () => {
        c.role = sel.value;
        touch(T);
        draw(false);
      });
      return h('div', {class: 'pi-col'}, h('span', {class: 'grow'}, h('b', {}, c.label),
        c.samples.length ? h('span', {}, c.samples.join(' · ')) : null), sel);
    }), h('div', {class: 'pi-col pi-rowstart'}, h('span', {class: 'grow'}, h('b', {}, 'A row starts at'),
      h('span', {}, 'A new date keeps a date line with the amounts printed under it.')), rowSel));
  }

  function whyNot(T) {
    const roles = T.cols.map((c) => c.role);
    if (roles.filter((r) => r === 'date').length !== 1) return 'Pick one column as the date.';
    if (!roles.some((r) => r === 'debit' || r === 'credit' || r === 'amount')) return 'Pick the column that holds the money.';
    return '';
  }

  function dateChoices(text) {
    const m = /^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$/.exec(String(text || '').trim());
    if (!m) return [{order: 'MDY', label: 'Dates are clear', hint: text || 'The dates read as they are'}];
    const a = Number(m[1]), b = Number(m[2]);
    const y = Number(m[3]) + (m[3].length === 2 ? 2000 : 0);
    const when = (day, month) => new Date(y, month - 1, day).toLocaleDateString(LOC, {month: 'long', day: 'numeric', year: 'numeric'});
    const out = [];
    if (a <= 12) out.push({order: 'MDY', label: when(b, a), hint: 'Month first: MM/DD/YYYY'});
    if (b <= 12 && b !== a) out.push({order: 'DMY', label: when(a, b), hint: 'Day first: DD/MM/YYYY'});
    return out.length ? out : [{order: 'DMY', label: when(a, b), hint: 'Day first: DD/MM/YYYY'}];
  }

  function stepDates(T, draw) {
    const sample = ((T.cols.find((c) => c.role === 'date') || {}).samples || [])[0] || '';
    const choices = dateChoices(sample);
    if (!choices.some((c) => c.order === T.order)) T.order = choices[0].order;
    const year = T.detail.year_placeholder ? h('div', {class: 'fld pi-year'},
      h('label', {for: 'pdf-year'}, 'Year the statement covers'),
      h('div', {class: 'inp'}, h('input', {id: 'pdf-year', inputmode: 'numeric', maxlength: '4', value: T.year,
        'aria-label': 'Year the statement covers',
        oninput: (e) => { T.year = e.target.value.replace(/\D/g, '').slice(0, 4); touch(T); }}))) : null;
    return h('div', {},
      h('p', {class: 'pi-note'}, sample ? `The first date is ${sample}. Which day is it?` : 'Which day comes first in the dates?'),
      h('div', {class: 'pi-opts'}, choices.map((c) => h('button', {class: `pi-opt${T.order === c.order ? ' on' : ''}`,
        type: 'button', onclick: () => { T.order = c.order; touch(T); draw(false); }},
      h('span', {class: 'rd'}), h('span', {class: 'grow'}, h('b', {}, c.label), h('span', {}, c.hint))))),
      year);
  }

  function stepSigns(T, draw) {
    return h('div', {}, h('p', {class: 'pi-note'}, 'How does this statement show money out?'),
      h('div', {class: 'pi-opts'}, SIGNS.map(([, title, hint], i) => h('button',
        {class: `pi-opt${T.signKey === i ? ' on' : ''}`, type: 'button', onclick: () => {
          T.signKey = i;
          T.sign = SIGNS[i][0];
          const money = T.cols.filter((c) => c.role === 'debit' || c.role === 'credit');
          if (i !== 0 && money.length === 1 && !T.cols.some((c) => c.role === 'amount')) money[0].role = 'amount';
          touch(T);
          draw(false);
        }}, h('span', {class: 'rd'}), h('span', {class: 'grow'}, h('b', {}, title), h('span', {}, hint))))));
  }

  function stepBalance(T, draw) {
    const typed = T.balance !== 'running';
    const field = (key, label) => h('div', {class: 'fld'}, h('label', {}, label),
      h('div', {class: 'inp'}, h('input', {value: T.typed[key], inputmode: 'decimal', 'aria-label': label,
        placeholder: '0.00', oninput: (e) => {
          T.typed[key] = e.target.value.trim();
          touch(T);
          T.recheckSoon();
        }})));
    T.saveable = T.balance === 'running';   // with typed balances it can't be re-read next month without asking again
    if (!T.saveable) T.teach = false;
    // The check says when saving would replace a layout saved before for these statements: never silently.
    const replaces = T.check && T.check.ok && T.check.p.pdf ? T.check.p.pdf.replaces : null;
    // created_at is a UTC timestamp: the day it was saved is the local day of that instant, not its UTC date
    const at = replaces ? new Date(String(replaces.created_at || '')) : null;
    const pad2 = (n) => String(n).padStart(2, '0');
    const savedOn = at && !Number.isNaN(at.getTime())
      ? fmtDate(`${at.getFullYear()}-${pad2(at.getMonth() + 1)}-${pad2(at.getDate())}`, true) : '';
    const saveHint = !replaces ? 'Statements that look like this import without asking.'
      : savedOn ? `Replaces the layout saved on ${savedOn} for statements like this.`
        : 'Replaces the layout saved before for statements like this.';
    const toggle = h('button', {class: 'tgl', type: 'button', role: 'switch', 'aria-checked': T.teach ? 'true' : 'false',
      'aria-label': 'Save this layout', onclick: () => {
        T.teach = !T.teach;
        toggle.setAttribute('aria-checked', T.teach ? 'true' : 'false');
      }}, h('i'));
    return h('div', {}, h('p', {class: 'pi-note'}, 'Where does the statement print its balance?'),
      h('div', {class: 'pi-opts'}, BALANCES.map(([value, title, hint]) => h('button',
        {class: `pi-opt${T.balance === value ? ' on' : ''}`, type: 'button', onclick: () => {
          T.balance = value;
          touch(T);
          draw(false);
          T.recheck();
        }}, h('span', {class: 'rd'}), h('span', {class: 'grow'}, h('b', {}, title), h('span', {}, hint))))),
      typed ? h('div', {class: 'pi-typed'}, field('opening', 'Opening balance'), field('closing', 'Closing balance')) : null,
      T.saveable ? h('div', {class: 'srow pi-save'}, h('span', {class: 'grow'}, h('b', {}, 'Save this layout'),
        h('span', {}, saveHint)), toggle)
        : h('p', {class: 'pi-hint'}, 'This layout can\u2019t be saved: it needs the two balances typed in, '
          + 'so it will ask for them again next month.'));
  }

  function cfgOf(T) {
    const cols = T.cols.filter((c) => c.role).map((c) => ({role: c.role, x0: c.x0, x1: c.x1}));
    const {row_start: _cut, date_order: _order, ...base} = T.base && typeof T.base === 'object' ? T.base : {};
    // An AI layout names its own header line (the parser's detail may have none): keep it, or the check can't find it.
    const words = (base.header_words || []).length ? base.header_words : (T.detail.header_words || []);
    return {...base, header_words: words.slice(0, 8), columns: cols, amount_sign: T.sign,
      ...(T.order ? {date_order: T.order} : {}),
      ...(T.rowStart === 'date' ? {row_start: 'date'} : {})};
  }

  function checkLine(T) {
    const idleHint = T.hint || (T.step < 2 ? 'The check runs once dates and money are set.'
      : T.step < 4 ? 'The check runs once the balance is set.' : 'Pick where the balance lives to run the check.');
    if (T.checking) return h('div', {class: 'pi-chk idle', role: 'status'}, icon('info'),
      h('span', {class: 'grow'}, h('b', {}, 'Checking\u2026'), h('span', {}, 'Reading the statement with your answers.')));
    const c = T.check;
    if (!c) return h('div', {class: 'pi-chk idle', role: 'status'}, icon('info'),
      h('span', {class: 'grow'}, h('b', {}, 'Not checked yet'), h('span', {}, idleHint)));
    if (!c.ok) return h('div', {class: 'pi-chk bad', role: 'status'}, icon('alert'),
      h('span', {class: 'grow'}, h('b', {}, 'Doesn\u2019t add up'), h('span', {}, c.message)));
    const p = c.p, pdf = p.pdf || {}, code = pdf.currency || CUR.code;
    return h('div', {class: 'pi-chk ok', role: 'status'}, icon('check'),
      h('span', {class: 'grow'}, h('b', {}, 'Adds up'),
        h('span', {}, `${amt(Number(pdf.opening), code)} + ${amt(p.preview_in || 0, code)} `
          + `\u2212 ${amt(p.preview_out || 0, code)} = ${amt(Number(pdf.closing), code)}, `
          + (c.typed ? 'as you typed' : 'as printed'))));   // balances typed by hand were never printed
  }

  async function runCheck(T, tryDateRow) {
    if (!T || !T.alive) return;
    const why = whyNot(T);
    if (why) { T.checking = false; T.check = null; T.hint = why; T.draw(false); return; }
    if (T.balance !== 'running' && (!T.typed.opening || !T.typed.closing)) {
      T.checking = false; T.check = null;
      T.hint = 'Type the two balances the statement prints.';
      T.draw(false);
      return;
    }
    const mine = ++T.seq;
    T.checking = true;
    T.draw(false);
    const cfg = cfgOf(T);
    if (tryDateRow) cfg.row_start = 'date';
    const body = {filename: S.file.name, data: S.data, template: cfg, template_source: T.source};
    if (T.balance !== 'running') { body.opening = T.typed.opening; body.closing = T.typed.closing; }
    if (T.year) body.year = Number(T.year);
    if (S.password) body.password = S.password;
    try {
      const p = await post('/api/import/preview', body);
      if (mine !== T.seq || !T.alive) return;
      T.check = {ok: true, p, typed: 'opening' in body};   // the proof line names where its balances came from
      if (tryDateRow) { T.rowStart = 'date'; T.rowAuto = true; }
    } catch (e) {
      if (mine !== T.seq || !T.alive) return;
      // The other row cut is worth one try on the way: a row whose date is on its own line (the IN shape) can only
      // add up with row_start=date, and the person has no way to know which cut their statement needs.
      if (!tryDateRow && !T.rowTried && T.rowStart === 'date_or_amount' && ROW_TRY.has(e.code)) {
        T.rowTried = true;
        T.checking = false;
        return runCheck(T, true);
      }
      T.check = {ok: false, message: e.message, code: e.code};
    }
    T.checking = false;
    T.hint = '';
    T.draw(false);
  }

  async function commit(T, go) {
    go.disabled = true;
    const body = {filename: S.file.name, data: S.data, template: cfgOf(T), template_source: T.source, teach: T.teach};
    if (T.teach && T.check && T.check.ok && (T.check.p.pdf || {}).replaces) body.replace_template = true;  // as shown
    if (T.balance !== 'running') { body.opening = T.typed.opening; body.closing = T.typed.closing; }
    if (T.year) body.year = Number(T.year);
    if (S.password) body.password = S.password;
    try {
      const r = await post('/api/import/commit', body);
      T.alive = false;
      doneSheet(r);
    } catch (e) {
      go.disabled = false;
      const err = document.querySelector('#sheet .formerr');
      if (err) err.textContent = e.message;
    }
  }

  // ---- 8: done -------------------------------------------------------------------------------------------------
  function doneSheet(r) {
    const month = (r.last || '').slice(0, 7);
    const name = r.filename || (S.file ? S.file.name : '');
    forget();   // imported: neither the statement's bytes nor its password are needed any more
    open([head('Imported'),
      h('div', {class: 'impdone'}, HF.ring(1, OKR, 72, 7, HF.glyph('check', 2.6)), h('div', {},
        h('b', {}, `${plural(r.new, 'new row')}${r.skipped ? `, ${plural(r.skipped, 'duplicate')} skipped` : ''}`),
        h('span', {}, `${name} · ${span(r.first, r.last)}`))),
      h('div', {class: 'fg'}, (r.accounts || []).map((a) => h('div', {}, h('span', {}, acctName(a.account.name)),
        h('b', {}, `${a.new} new · ${a.skipped} skipped`)))),
      h('p', {class: 'hint'}, 'They are in accounts added by you (manual); categories are filled in the usual way.'),
      rowOf(h('button', {class: 'btn w', type: 'button', onclick: () => {
        closeModal();
        location.hash = `#/transactions?month=${month}`;
      }}, 'See them in Transactions'), btn('Import another', 'ghost', dropSheet))],
    {onClose: () => HF.refresh()});
  }

  // The import sheet hands a dropped PDF here. held(): whether the flow still holds the file, its bytes or a password
  // (booleans only, never the values), so a test can prove they are let go when the flow ends.
  HF.pdf = {start, held: () => ({file: !!S.file, data: !!S.data, password: !!S.password})};
});
