/* The Alpaca live account (TASK-19). Loaded before app.js (window.HF_EXT). It shows as an institution on Investments,
 * Accounts and the sidebar ("Live · read-only via API · N positions · Counted in net worth"), with no Plaid slot. The
 * Add account chooser gets "Alpaca (API key)": the key is checked with one read-only call on the server before anything
 * is saved (POST /api/alpaca/key); the row's Replace / Remove change or delete it (DELETE /api/alpaca/key). The secret
 * is only ever typed here and sent once; the server never sends it back. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, fill, icon, plural, money, money2, ago, daysAgo, ia, tag, pill, post, HOOKS, RGB, openSheet, closeModal, closeBtn,
    backBtn, toast, modal, opt, notCounted, leftOut, acctName, nameMask} = X;
  const NOTE = 'The app only reads your account. Alpaca keys can also trade, so keep this key private.';

  const statusLine = (a) => (a.account_status && a.account_status !== 'ACTIVE'
    ? `Alpaca says ${a.account_status.toLowerCase().replace(/_/g, ' ')} · not counted`
    : `Live · read-only via API · ${plural(a.positions || 0, 'position')}`);
  const counted = (a, id) => (a.account_status && a.account_status !== 'ACTIVE'
    ? h('div', {class: 'nwc bad'}, icon('alert'), 'Not counted: the account isn’t active')
    : leftOut(id) ? h('div', {class: 'nwc off'}, icon('info'), 'Not counted in Custom')
      : h('div', {class: 'nwc'}, icon('checkc'), 'Counted in net worth'));
  const controls = () => h('div', {class: 'apictl'},
    h('button', {class: 'lnk', type: 'button', onclick: (e) => { e.stopPropagation(); openAlpaca('replace'); }}, 'Replace key'),
    h('button', {class: 'lnk', type: 'button', onclick: (e) => { e.stopPropagation(); confirmRemove(); }}, 'Remove'));

  function apiRow(a, value, when) {
    return h('div', {class: 'li wrap apirow', dataset: {api: 'alpaca'}}, ia('Alpaca', 'l', null, a.institution_id || 'ins_hf_alpaca'),
      h('span', {class: 'grow'}, h('b', {}, acctName(a.name) || 'Alpaca', tag('API', 'api', 'Connected with an API key, not Plaid: no bank slot')),
        h('span', {}, statusLine(a)), counted(a, a.id || a.account_id), controls()),
      h('span', {class: 'rt'}, h('span', {class: 'amt v'}, money2(value)), h('span', {}, when)));
  }

  const whenOf = (a) => (a.as_of ? `updated ${daysAgo(a.as_of)}` : a.updated_at ? ago(a.updated_at) : '');
  HOOKS.investRow = (a) => (a.api ? apiRow(a, a.value, whenOf(a)) : null);
  // Accounts follows the shared two-line rule; management actions live in its existing sheet system.
  function accountDetails(a) {
    openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Alpaca details'), closeBtn()),
      h('p', {}, statusLine(a)), h('p', {}, whenOf(a)), counted(a, a.account_id),
      h('p', {class: 'apnote'}, NOTE), controls()], {narrow: true, cls: 'apsheet'});
  }
  HOOKS.acctRow = (a, inst, instId) => {
    if (!a.api) return null;
    const id = a.account_id;
    // Match the inline info control while opening a sheet for the management actions.
    const info = h('button', {class: 'help-trigger', type: 'button', 'aria-label': 'Alpaca details',
      'aria-haspopup': 'dialog', onclick: () => accountDetails(a)}, icon('info'));
    const row = h('div', {class: 'li acct r2 apirow', dataset: {api: 'alpaca', account: id}},
      ia('Alpaca', '', null, instId || 'ins_hf_alpaca'),
      h('span', {class: 'grow'}, h('b', {class: 'nmrow'}, h('span', {class: 'nm'}, acctName(a.name) || 'Alpaca'), info),
        h('span', {class: 'meta2'}, h('span', {class: 'chip2'}, 'Read-only'),
          leftOut(id) ? h('span', {class: 'nc2'}, 'Not counted · ') : null,
          h('span', {class: 'mt'}, `API · ${plural(a.positions || 0, 'position')}`))),
      h('span', {class: 'rt'}, h('span', {class: 'amt v'}, money2(a.current || 0))));
    return h('div', {class: 'account-entry'}, row);
  };

  // the sidebar: like a bank's row, with its status in place of Reconnect
  HOOKS.bankRow = (b, open) => {
    if (!b.item.api) return null;
    const it = b.item;
    const ok = it.account_status === 'ACTIVE';
    const panel = h('div', {class: 'accts', id: 'accts-alpaca'},
      b.accounts.map((a) => h('div', {class: 'ac'}, nameMask(a.name, a.mask, {fallback: 'Alpaca'}),
        leftOut(a.account_id) ? notCounted() : null, h('span', {class: 'v'}, money(a.signed == null ? a.current : a.signed)))),
      h('div', {class: 'ac'}, h('span', {}, ok ? `Read-only via API · ${plural(it.positions || 0, 'position')}` : 'Not counted')),
      h('a', {class: 'go', href: '#/investments'}, 'View holdings →'));
    const wrap = h('div', {class: `bank${open ? ' open' : ''}`});
    const tog = h('button', {class: 'tog', type: 'button', 'aria-expanded': String(open), 'aria-controls': 'accts-alpaca',
      'aria-label': 'Alpaca accounts'}, icon('chevr'));
    tog.addEventListener('click', () => tog.setAttribute('aria-expanded', String(wrap.classList.toggle('open'))));
    return fill(wrap, h('div', {class: 'row'}, h('a', {class: 'link', href: '#/investments', title: 'Alpaca: view holdings'},
      ia('Alpaca', 's', ok ? 'ok' : 'bad', it.institution_id), h('span', {class: 'nm'}, 'Alpaca'), h('span', {class: 'v'}, money(b.net))), tog),
    ok ? null : h('div', {class: 'relogin'}, h('span', {}, `Alpaca says ${String(it.account_status).toLowerCase().replace(/_/g, ' ')}`),
      h('button', {class: 'rc', type: 'button', onclick: () => openAlpaca('replace')}, 'Replace key')), panel);
  };

  HOOKS.addOptions.push(() => opt('key', RGB.acc, 'Alpaca (API key)', 'Your live brokerage account, read-only', () => openAlpaca('add')));

  async function del(url) {  // like app.js's post(): JSON, and with the built-in login the session's CSRF token (TASK-20)
    const headers = {'Content-Type': 'application/json'};
    const csrf = (document.querySelector('meta[name=csrf-token]') || {}).content;
    if (csrf) headers['X-CSRF-Token'] = csrf;
    const r = await fetch(url, {method: 'DELETE', headers, body: '{}'});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }

  function confirmRemove() {
    modal('Remove Alpaca?', h('p', {class: 'mut'}, 'The saved key is deleted and Alpaca leaves net worth, every total and the history. '
      + 'Nothing changes at Alpaca. You can connect it again any time.'), [
      {label: 'Remove', primary: true, onclick: async () => {
        closeModal();
        try {
          await del('/api/alpaca/key');
          X.refresh();
          toast('Alpaca removed. Its key is deleted.', [], {timeout: 4000});
        } catch (e) {
          toast(`Could not remove it: ${e.message}`, [], {error: true, timeout: 7000});
        }
      }},
      {label: 'Cancel', onclick: closeModal},
    ]);
  }

  // The Connect form (also for a first-run step: openAlpaca('setup', {onDone, onSkip}) draws the same form).
  function openAlpaca(mode = 'add', hooks = {}) {
    const keyId = h('input', {id: 'ap-key', autocomplete: 'off', spellcheck: 'false', maxlength: '64', 'aria-label': 'Key ID',
      placeholder: 'e.g. AK…'});
    const secret = h('input', {id: 'ap-secret', type: 'password', autocomplete: 'off', spellcheck: 'false', maxlength: '128',
      'aria-label': 'Secret', placeholder: 'Secret key'});
    const err = h('p', {class: 'ferr', role: 'alert'});
    const result = h('div', {class: 'apres', 'aria-live': 'polite'});
    const go = h('button', {class: 'btn w', type: 'button'}, icon('key'), mode === 'replace' ? 'Replace key' : 'Connect');
    const connect = async () => {
      err.textContent = '';
      if (!keyId.value.trim() || !secret.value.trim()) {
        err.textContent = 'Paste both the Key ID and the Secret.';
        return;
      }
      go.disabled = true;
      fill(go, 'Checking with Alpaca…');
      try {
        const r = await post('/api/alpaca/key', {key_id: keyId.value.trim(), secret: secret.value.trim()});
        secret.value = '';
        fill(result, pill('sm', 'checkc', 'Connected'), h('div', {class: 'apsum'},
          h('div', {}, h('span', {class: 'mut'}, 'Equity'), h('b', {class: 'v'}, money2(r.equity))),
          h('div', {}, h('span', {class: 'mut'}, 'Positions'), h('b', {class: 'v'}, r.positions == null ? 'next sync' : String(r.positions)))));
        fill(go, 'Done');
        go.disabled = false;
        go.onclick = () => { closeModal(); if (hooks.onDone) hooks.onDone(r); };
        X.refresh();
      } catch (e) {
        err.textContent = e.message;
        go.disabled = false;
        fill(go, icon('key'), mode === 'replace' ? 'Replace key' : 'Connect');
      }
    };
    go.onclick = connect;
    secret.addEventListener('keydown', (e) => { if (e.key === 'Enter') connect(); });
    openSheet([h('div', {class: 'shd'}, mode === 'add' ? backBtn(() => X.openAdd('home')) : null,
      h('h2', {id: 'sheet-title'}, mode === 'replace' ? 'Replace the Alpaca key' : 'Connect Alpaca'), closeBtn()),
    h('p', {}, 'Your live brokerage account, read with an API key: equity, cash and positions, once a day. It never places a trade.'),
    h('div', {class: 'flds'},
      h('div', {class: 'fld'}, h('label', {for: 'ap-key'}, 'Key ID'), h('div', {class: 'inp code'}, keyId)),
      h('div', {class: 'fld'}, h('label', {for: 'ap-secret'}, 'Secret'), h('div', {class: 'inp code'}, secret), err)),
    h('p', {class: 'apnote'}, icon('info'), h('span', {}, NOTE)),
    result,
    h('div', {class: 'row'}, go, hooks.onSkip ? h('button', {class: 'btn ghost', type: 'button', onclick: () => { closeModal(); hooks.onSkip(); }}, 'Skip') : null)],
    {narrow: true, cls: 'apsheet', focus: keyId});
  }
  X.openAlpaca = openAlpaca;  // for a first-run step (TASK-18's setup, at merge)
});
