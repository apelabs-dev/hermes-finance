/* Hermes Finances: the Settings page (#/settings, TASK-18), as approved in MOCK-017.
 *
 * Every value is on the server (GET /api/settings/page, POST /api/prefs and the few routes of their own) and saves as
 * it changes: a switch, a choice or a select on the spot; a text field when it's left or Enter is pressed. Editor
 * sheets (Replace keys, Change password, the AI key) save when they close. Keys and passwords are write-only: the page
 * only ever learns whether one is set. Runs inside app.js's building blocks (window.HF_EXT, see app.js).
 */
(window.HF_EXT = window.HF_EXT || []).push((HF) => {
  'use strict';
  const {h, fill, icon, card, api, post, toast, pill, tag, ibadge, badge, money, pct, plural, fmtDate, ago, RGB,
    openSheet, closeModal, closeBtn} = HF;
  HF.PAGES.settings = 'Settings';

  // an address that may be longer than its card: a break chance only after the scheme, so it wraps as
  // "https://" + host:port/path rather than mid-word (hotfix 2026-09-29: the live tailnet host stuck out of its card)
  const epText = (u) => { const m = /^([a-z]+:\/\/)(.*)$/i.exec(String(u)); return m ? [m[1], h('wbr', {}), m[2]] : String(u); };
  const saved = (text = 'Saved') => toast(text, [], {timeout: 1600});
  const failed = (e) => toast(`Not saved: ${e.message}`, [], {error: true, timeout: 5000});
  async function save(values, after) {
    try {
      const r = await post('/api/prefs', values);
      if (Object.keys(values).some((key) => key.startsWith('ai.')))
        document.dispatchEvent(new CustomEvent('hf-prefs', {detail: r.prefs}));
      saved();
      if (after) after(r);
      return r;
    } catch (e) {
      failed(e);
      throw e;
    }
  }

  // ---- small controls ---------------------------------------------------------------------------------------------
  const srow = (label, sub, control, cls = '') => h('div', {class: `srow ${cls}`}, h('span', {class: 'grow'}, h('b', {}, label),
    sub ? h('span', {}, sub) : null), control);
  function sw(on, label, onchange, disabled) {
    const b = h('button', {class: 'tgl', type: 'button', role: 'switch', 'aria-checked': String(Boolean(on)), 'aria-label': label, disabled},
      h('i'));
    b.addEventListener('click', async () => {
      const next = b.getAttribute('aria-checked') !== 'true';
      b.setAttribute('aria-checked', String(next));
      try {
        await onchange(next);
      } catch (e) {
        b.setAttribute('aria-checked', String(!next));
      }
    });
    return b;
  }
  function seg(opts, on, label, onpick, cls = '') {
    const g = h('div', {class: `seg ${cls}`, role: 'group', 'aria-label': label});
    const draw = (cur) => fill(g, opts.map(([k, text, ic]) => h('button', {type: 'button', 'aria-pressed': String(k === cur), dataset: {k},
      onclick: () => { if (k !== cur) onpick(k, draw); }}, ic ? icon(ic) : null, text)));
    draw(on);
    return g;
  }
  function select(options, value, label, onchange, cls = 'selv') {
    const s = h('select', {class: cls, 'aria-label': label}, options.map(([v, text]) => h('option', {value: String(v)}, text)));
    s.value = String(value);
    s.addEventListener('change', () => onchange(s.value, s));
    return s;
  }
  // a text / time / number field that saves when it's left or on Enter (and only when it changed)
  function saveOnLeave(input, onsave) {
    let last = input.value;
    const go = async () => {
      if (input.value === last) return;
      try {
        await onsave(input.value);
        last = input.value;
      } catch (e) {
        input.value = last;
      }
    };
    input.addEventListener('change', go);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } });
    return input;
  }
  const inp = (input, cls = 'fx', pre) => h('span', {class: `inp ${cls}`}, pre ? h('span', {class: 'pre', 'aria-hidden': 'true'}, pre) : null, input);
  const gh = (text) => h('div', {class: 'gh'}, text, h('span', {class: 'ln'}));
  const meter = (ratio, label, max, now) => h('span', {class: 'meter', role: 'progressbar', 'aria-label': label, 'aria-valuemin': '0',
    'aria-valuemax': String(max), 'aria-valuenow': String(now)}, h('i', {style: {width: pct(ratio)}}));
  const whenShort = (ts) => (ts ? fmtDate(ts.slice(0, 10)) : '');

  // A sheet that saves when it closes (×, Escape, a tap outside): `commit()` returns a promise, or null for "nothing
  // to save". Its result (or error) is a toast.
  function editorSheet(title, lead, body, commit, done) {
    openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, title), closeBtn()), lead ? h('p', {}, lead) : null, body,
      h('p', {class: 'hint'}, 'Saved when you close this.')], {narrow: true, onClose: async () => {
      const p = commit();
      if (!p) return;
      try {
        const r = await p;
        saved();
        if (done) done(r);
      } catch (e) {
        failed(e);
      }
    }});
  }
  const fld = (label, input, hint) => {
    const id = `sf-${Math.random().toString(36).slice(2, 8)}`;
    input.id = id;
    return h('div', {class: 'fld'}, h('label', {for: id}, label), h('div', {class: 'inp'}, input), hint ? h('p', {class: 'hint'}, hint) : null);
  };

  let D = null;          // the last GET /api/settings/page
  const reload = () => HF.route({keepToast: true});

  // ---- Connections -------------------------------------------------------------------------------------------------
  function replaceKeys(env) {
    const id = h('input', {type: 'text', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', placeholder: 'Client ID'});
    const secret = h('input', {type: 'password', autocomplete: 'off', spellcheck: 'false', placeholder: 'Secret'});
    let pick = env;
    const envSeg = seg([['production', 'Production'], ['sandbox', 'Sandbox']], env, 'Plaid environment', (k, draw) => { pick = k; draw(k); });
    const banks = D.plaid.banks;
    editorSheet('Replace Plaid keys', 'Both are at dashboard.plaid.com → Developers → Keys. Each environment has its own secret.',
      [fld('Plaid client ID', id), fld('Plaid secret', secret, `Saved in ${D.secrets.path} on this computer, never shown again.`),
        h('div', {class: 'fld'}, h('span', {class: 'lbl'}, 'Environment'), envSeg),
        banks ? h('p', {class: 'nc warn'}, `${plural(banks, 'connected bank')} belong to the environment they were linked in; they stop syncing in the other.`) : null],
      () => {
        if (!id.value.trim() && !secret.value.trim()) return pick !== env ? Promise.reject(new Error('paste the client ID and secret for that environment')) : null;
        return post('/api/secrets/plaid', {client_id: id.value, secret: secret.value, env: pick});
      }, reload);
  }

  // Alpaca (TASK-19 reads the account; its key is set through POST / DELETE /api/alpaca/key): a read-only key pair
  async function del(url) {
    const headers = {'Content-Type': 'application/json'};   // the route takes JSON only, like every write
    const meta = document.querySelector('meta[name=csrf-token]');
    if (meta && meta.content) headers['X-CSRF-Token'] = meta.content;
    const r = await fetch(url, {method: 'DELETE', headers, body: '{}'});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `${r.status} ${r.statusText}`);
    return j;
  }
  HF.alpacaForm = (commitLabel) => {
    const id = h('input', {type: 'text', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', placeholder: 'Key ID'});
    const secret = h('input', {type: 'password', autocomplete: 'off', spellcheck: 'false', placeholder: 'Secret key'});
    return {fields: [fld('API key ID', id), fld('Secret key', secret, commitLabel),
      // TASK-19's note, as on the Add account chooser's Connect form (alpaca.js)
      h('p', {class: 'apnote'}, icon('info'), h('span', {}, 'The app only reads your account. Alpaca keys can also trade, so keep this key private.'))],
      filled: () => Boolean(id.value.trim() || secret.value.trim()),
      send: () => post('/api/alpaca/key', {key_id: id.value.trim(), secret: secret.value.trim()})};
  };
  function alpacaKey() {
    const f = HF.alpacaForm(`A read-only key from alpaca.markets \u2192 your account \u2192 API keys. Saved in ${D.secrets.alpaca.where}, never shown again.`);
    editorSheet(D.secrets.alpaca.set ? 'Replace the Alpaca key' : 'Connect Alpaca', 'The app only reads your balance and positions; it never trades.',
      f.fields, () => (f.filled() ? f.send() : null), reload);
  }
  function alpacaRemove() {
    HF.modal('Remove the Alpaca key?', h('p', {}, 'Alpaca leaves your net worth and Investments until you connect it again.'),
      [{label: 'Remove', primary: true, onclick: async () => {
        closeModal();
        try {
          await del('/api/alpaca/key');
          saved('Removed');
          reload();
        } catch (e) {
          failed(e);
        }
      }}, {label: 'Keep it', onclick: closeModal}]);
  }

  // v21: one SimpleFIN row, in the same logo slot as the Alpaca one. It shows the connection's state, the banks and
  // accounts, the request budget and the broken-state copy; Replace / Remove (or Connect) are the actions.
  function simplefinRows(sf) {
    const state = sf.state || 'not_connected';
    const connected = Boolean(sf.connected);
    const bad = ['needs_login', 'subscription', 'quota'].includes(state);
    const status = !connected ? (sf.needs_confirm ? pill('sm nt', 'clock', 'Needs confirming')
      : sf.needs_retry ? pill('sm nt', 'clock', 'Needs retry') : pill('sm nt', null, 'Not connected'))
      : state === 'needs_login' ? pill('sm bad', 'alert', 'Needs a new login')
        : state === 'subscription' ? pill('sm bad', 'alert', 'Subscription needed')
          : state === 'quota' ? pill('sm bad', 'alert', 'Budget used up')
            : pill('sm', 'check', 'Connected');
    const sub = connected ? `${plural(sf.banks || 0, 'bank')} · ${plural(sf.accounts || 0, 'account')}`
      : sf.needs_confirm ? 'The setup token is in; confirm the account types'
        : sf.needs_retry ? 'The access link is saved, but the first fetch failed'
          : 'A second, read-only bank connection';
    const actions = connected
      ? [h('button', {class: 'btn sm ghost', type: 'button', onclick: () => HF.simplefin.openToken()}, 'Replace'),
        h('button', {class: 'btn sm danger', type: 'button', onclick: () => HF.simplefin.remove()}, 'Remove')]
      : sf.needs_confirm ? [h('button', {class: 'btn sm', type: 'button', onclick: () => HF.simplefin.resume()}, 'Finish setup')]
        : sf.needs_retry ? [h('button', {class: 'btn sm', type: 'button', onclick: () => HF.simplefin.tryAgain()}, 'Try again')]
          : [h('button', {class: 'btn sm', type: 'button', onclick: () => HF.simplefin.openToken()}, 'Connect')];
    const broken = state === 'needs_login' || state === 'subscription'
      ? h('p', {class: 'nc warn'}, `SimpleFIN stopped accepting this access link, so it was revoked or expired. Balances `
        + `stay as of ${sf.as_of ? fmtDate(sf.as_of) : 'the last sync'}. Paste a new setup token to resume.`)
      : state === 'quota'
        ? h('p', {class: 'nc warn'}, 'SimpleFIN\'s 24 requests a day are used up. The last balances stay; the daily sync tries again tomorrow.')
        : state === 'needs_retry'
          ? h('p', {class: 'nc warn'}, 'The access link is saved, but SimpleFIN\'s first answer never arrived, so no accounts were added. Try again uses the same link; no new token is needed.')
          : null;
    const pending = sf.last_result && sf.last_result.ambiguous
      ? h('p', {class: 'nc warn'}, `${plural(sf.last_result.ambiguous, 'pending charge')} could not be matched one-to-one. `
        + 'Both rows are kept for review in Transactions.') : null;
    const dupes = (sf.duplicates || []).length
      ? h('div', {class: 'srow'}, h('span', {class: 'grow'}, h('b', {}, 'Accounts on both Plaid and SimpleFIN'),
        h('span', {}, 'Choose which copy counts. Totals stay Plaid-only until you pick.')),
      h('button', {class: 'btn sm', type: 'button', onclick: () => HF.simplefin.openDuplicates(sf.duplicates)}, 'Review'))
      : null;
    return [
      h('div', {class: 'srow'}, HF.ia('SimpleFIN', '', null, 'ins_hf_simplefin'),
        h('span', {class: 'grow'}, h('b', {}, 'SimpleFIN'), h('span', {}, sub)), h('span', {class: 'pairc'}, status, ...actions)),
      broken, pending, dupes,
      h('div', {class: 'srow'}, h('span', {class: 'grow'}, h('b', {}, 'SimpleFIN requests today'),
        h('span', {}, 'Bridge allows 24 a day · the daily sync and each Sync now count as one'),
        meter((sf.requests_today || 0) / (sf.request_limit || 24), 'SimpleFIN requests today', sf.request_limit || 24,
          sf.requests_today || 0)),
      h('b', {class: 'slotn v'}, `${sf.requests_today || 0}/${sf.request_limit || 24}`)),
    ];
  }

  function connectionsCard(d) {
    const s = d.secrets, p = d.plaid;
    const keys = s.plaid.set ? pill('sm', 'check', 'Set') : pill('sm bad', 'alert', 'Missing');
    const env = s.plaid.env || 'production';
    const alp = s.alpaca;
    return card({cls: 'sc', title: 'Connections', icon: 'link', rgb: RGB.acc},
      srow('Plaid keys', s.plaid.set ? 'Stored locally' : 'Paste the two keys from your Plaid account',
        h('span', {class: 'pairc'}, keys, h('button', {class: 'btn sm ghost', type: 'button', onclick: () => replaceKeys(env)}, 'Replace keys'))),
      srow('Plaid environment', p.demo ? 'Sandbox demo: fake banks and data. Nothing real is connected.' : 'Real banks / Demo',
        seg([['production', 'Production'], ['sandbox', 'Sandbox']], env, 'Plaid environment', (k) => replaceKeys(k))),
      h('div', {class: 'srow'}, h('span', {class: 'grow'}, h('b', {}, 'Plaid connections'),
        h('span', {}, `${p.slots_used} of ${p.slot_limit} used · removing a bank doesn't free its slot`),
        meter(p.slots_used / p.slot_limit, 'Plaid slots used', p.slot_limit, p.slots_used)),
      h('b', {class: 'slotn v'}, `${p.slots_used}/${p.slot_limit}`)),
      // TASK-22: Plaid has no API for the plan; on-demand refresh ("Ask banks for fresh data") is free on Trial only.
      // TASK-22f (MOCK-022): Trial / Paid, and the switch (on for Trial, off by default for Paid) with the line for the plan;
      // the action itself is only in the Sync now panel. TASK-22g: on Paid, the bigger cost too (Plaid's subscription
      // products, Transactions / Investments / Liabilities, are billed per Item each month); changing the plan puts the
      // switch back to the plan's default (prefs.save)
      h('div', {class: 'srow'}, h('span', {class: 'grow'}, h('b', {}, 'Plaid plan'),
        p.refresh.plan === 'paid' ? h('span', {}, 'Plaid bills each linked bank monthly on paid plans.') : null),
      seg([['trial', 'Trial'], ['paid', 'Paid']], p.refresh.plan, 'Plaid plan', (k) => save({'plaid.plan': k}, reload))),
      srow('Allow on-demand bank refresh', p.refresh.plan === 'paid' ? 'Plaid bills each refresh request on paid plans. Off unless you turn it on.'
        : 'Adds Refresh',
      sw(p.refresh.allowed, 'Allow on-demand bank refresh', (on) => save({'plaid.on_demand_refresh': on}, reload))),
      simplefinRows(d.simplefin || {}),
      h('div', {class: 'srow'}, HF.ia('Alpaca', '', null, 'ins_hf_alpaca'), h('span', {class: 'grow'}, h('b', {}, 'Alpaca'),
        h('span', {}, alp.set ? 'Read-only' :'Brokerage balance and positions, read-only \u00b7 uses no Plaid slot')),
      h('span', {class: 'pairc'}, alp.set ? pill('sm', 'check', 'Live') : pill('sm nt', null, 'Not configured'),
        alp.set ? [h('button', {class: 'btn sm ghost', type: 'button', onclick: alpacaKey}, 'Replace'),
          h('button', {class: 'btn sm danger', type: 'button', onclick: alpacaRemove}, 'Remove')]
          : h('button', {class: 'btn sm', type: 'button', onclick: alpacaKey}, 'Connect'))),
      h('div', {class: 'sfootl'}, HF.lnk('#/accounts', 'Banks and reconnects are on Accounts')));
  }

  // ---- Access & security -------------------------------------------------------------------------------------------
  const MODES = [
    ['local', 'monitor', RGB.ok, 'This computer only', 'Answers on this computer (localhost) only. No login.', 'Default'],
    ['proxy', 'shield', RGB.acc, 'Behind your proxy login', 'Tailscale Serve, Cloudflare Access, Authelia or oauth2-proxy signs you in; the app trusts the header it adds.'],
    ['builtin', 'lock', RGB.purple, 'Built-in login', 'For public hosting: your password plus an authenticator app.'],
  ];
  const choice = (on, iconName, rgb, title, text, note, onclick) => h('button', {class: `choice${on ? ' on' : ''}`, type: 'button', role: 'radio',
    'aria-checked': String(on), onclick}, ibadge(iconName, rgb, 's'), h('span', {class: 'grow'}, h('b', {}, title), h('span', {}, text),
    note ? h('span', {class: 'dim'}, note) : null), h('span', {class: 'rad'}));
  HF.accessChoices = (on, onpick) => h('div', {class: 'choices sm', role: 'radiogroup', 'aria-label': 'Who can open this app'},
    MODES.map(([k, ic, rgb, title, text, note]) => choice(k === on, ic, rgb, title, text, note, () => onpick(k))));

  // The built-in login's set-up in a sheet; when it's done the app is on the built-in login and this browser signed in.
  HF.loginSetup = (onDone) => {
    const body = h('div', {class: 'lsetup'});
    openSheet([h('div', {class: 'shd'}, h('span', {class: 'sp'}), closeBtn()), body], {narrow: true});
    window.HFLogin.enroll(body, {onDone: () => { closeModal(); (onDone || (() => location.reload()))(); }});
  };

  async function setMode(k) {
    if (k === 'builtin' && !D.login.configured) return HF.loginSetup();
    const local = ['127.0.0.1', 'localhost', '[::1]', '::1'].includes(location.hostname);
    const go = async () => {
      try {
        await post('/api/access', {mode: k});
        saved();
        if (k === 'local' && !local) location.reload();
        else reload();
      } catch (e) {
        if (e.field === 'identity') { ACCESS_PENDING = 'proxy'; reload(); toast(e.message, [], {error: true, timeout: 6000}); } else failed(e);
      }
    };
    if (k === 'local' && !local) {
      HF.modal('Only this computer?', h('p', {}, "This device isn't the computer the app runs on, so it loses access as soon as this is on."),
        [{label: 'Switch to this computer only', primary: true, onclick: () => { closeModal(); go(); }}, {label: 'Keep it as it is', onclick: closeModal}]);
      return undefined;
    }
    return go();
  }
  let ACCESS_PENDING = null;   // "proxy" chosen before it had an identity: its settings show, the mode waits

  function accessDetail(d, mode) {
    const a = d.access;
    if (mode === 'local') {
      return h('div', {class: 'acd'}, srow('Address', 'Nothing outside this computer can reach it', h('span', {class: 'mono ep'}, epText(a.bind)), 'tight'));
    }
    if (mode === 'proxy') {
      const hdr = saveOnLeave(h('input', {value: a.proxy_header, 'aria-label': 'Trusted user header', spellcheck: 'false', autocapitalize: 'off'}),
        (v) => post('/api/access/proxy', {proxy_header: v}).then(saved, (e) => { failed(e); throw e; }));
      const url = saveOnLeave(h('input', {value: a.public_urls[0] || '', 'aria-label': 'Public address', spellcheck: 'false', autocapitalize: 'off',
        placeholder: 'https://machine.tailnet.ts.net:8765'}), (v) => post('/api/access/proxy', {public_urls: v.trim() ? [v.trim(), ...a.public_urls.slice(1)] : a.public_urls.slice(1)})
        .then(() => { saved(); reload(); }, (e) => { failed(e); throw e; }));
      const add = h('input', {'aria-label': 'Identity to allow', spellcheck: 'false', autocapitalize: 'off', placeholder: 'you@example.com'});
      const addRow = h('div', {class: 'srow tight', hidden: true}, h('span', {class: 'inp fx'}, add),
        h('button', {class: 'btn sm', type: 'button', onclick: async () => {
          try {
            await post('/api/access/identities', {add: add.value});
            if (ACCESS_PENDING === 'proxy' && d.access.mode !== 'proxy') {
              ACCESS_PENDING = null;
              await post('/api/access', {mode: 'proxy'});
            }
            saved();
            reload();
          } catch (e) {
            failed(e);
          }
        }}, 'Allow'));
      add.addEventListener('keydown', (e) => { if (e.key === 'Enter') addRow.querySelector('.btn').click(); });
      return h('div', {class: 'acd'},
        srow('Trusted user header', 'The header your proxy adds with the signed-in user', inp(hdr, 'fx code hdr'), 'tight'),
        srow('Public address', 'The https address your proxy serves the app at', inp(url, 'fx code'), 'tight'),
        gh('Allowed identities'),
        a.identities.length ? a.identities.map((x) => h('div', {class: 'srow tight'}, h('span', {class: 'ibx'}, icon('user')),
          h('span', {class: 'grow'}, h('b', {class: 'v'}, x.masked)),
          h('button', {class: 'btn sm ghost', type: 'button', onclick: async () => {
            try {
              await post('/api/access/identities', {remove: x.id});
              saved();
              reload();
            } catch (e) {
              failed(e);
            }
          }}, 'Remove'))) : h('p', {class: 'hint'}, 'None yet: add the login your proxy signs you in with.'),
        addRow,
        h('div', {class: 'sfootl'}, h('button', {class: 'lnk', type: 'button', onclick: () => { addRow.hidden = false; add.focus(); }}, icon('plus'),
          'Allow another identity')),
        a.last_proxy ? h('div', {class: 'lastreq'}, h('b', {class: 'dot'}), `Last request: via ${a.last_proxy.via} · ${a.last_proxy.masked}`) : null);
    }
    const L = d.login;
    if (!L.configured) {
      return h('div', {class: 'acd'}, srow('Not set up yet', 'A password and an authenticator app; takes a minute.',
        h('button', {class: 'btn sm', type: 'button', onclick: () => HF.loginSetup()}, 'Set up'), 'tight'));
    }
    const sessions = L.sessions.length;
    return h('div', {class: 'acd'},
      srow('Username', null, h('b', {class: 'v'}, L.username), 'tight'),
      srow('Password', `Set ${whenShort(L.password_changed_at)}`, h('button', {class: 'btn sm ghost', type: 'button', onclick: changePassword}, 'Change password'), 'tight'),
      srow('2FA', "Authenticator app · required with built-in login, can't be turned off", pill('sm', 'lock', 'On'), 'tight'),
      srow('Recovery codes', `${L.recovery_left} left`, h('button', {class: 'btn sm ghost', type: 'button', onclick: regenerate}, 'Regenerate'), 'tight'),
      srow('Sessions', sessions ? `${plural(sessions, 'active')}${L.sessions.some((x) => x.this) ? ', this browser one of them' : ''}` : 'None',
        h('button', {class: 'btn sm ghost', type: 'button', onclick: signOutAll}, 'Sign out everywhere'), 'tight'),
      h('p', {class: 'hint'}, `${L.lockout.tries} wrong tries = ${L.lockout.minutes} min lock.`));
  }

  function changePassword() {
    const cur = h('input', {type: 'password', autocomplete: 'current-password'});
    const nw = h('input', {type: 'password', autocomplete: 'new-password'});
    const again = h('input', {type: 'password', autocomplete: 'new-password'});
    editorSheet('Change password', 'Every other signed-in browser is signed out.', [fld('Current password', cur), fld('New password', nw, 'At least 12 characters.'),
      fld('New password again', again)], () => {
      if (!cur.value && !nw.value && !again.value) return null;
      if (nw.value !== again.value) return Promise.reject(new Error('the two new passwords are different'));
      return post('/api/auth/password', {current: cur.value, new: nw.value});
    }, reload);
  }
  async function regenerate() {
    try {
      const r = await post('/api/auth/recovery', {});
      const text = `Hermes Finances recovery codes (each works once)\n\n${r.recovery_codes.join('\n')}\n`;
      openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'New recovery codes'), closeBtn()),
        h('p', {}, "The old ones no longer work. Save these; they won't be shown again."),
        h('div', {class: 'rcodes mono'}, r.recovery_codes.map((c) => h('span', {}, c))),
        h('div', {class: 'row'}, h('button', {class: 'btn sm ghost', type: 'button', onclick: () => {
          const a = h('a', {href: URL.createObjectURL(new Blob([text], {type: 'text/plain'})), download: 'hermes-finance-recovery-codes.txt'});
          document.body.append(a);
          a.click();
          a.remove();
        }}, icon('download'), 'Download'))], {narrow: true, onClose: reload});
    } catch (e) {
      failed(e);
    }
  }
  async function signOutAll() {
    try {
      await post('/api/auth/logout-all', {});
      location.reload();
    } catch (e) {
      failed(e);
    }
  }

  function accessCard(d) {
    const mode = ACCESS_PENDING || d.access.mode;
    return card({cls: 'sc', title: 'Access & security', icon: 'shield', rgb: RGB.bad},
      HF.accessChoices(mode, setMode),
      d.access.problems.length ? h('p', {class: 'safety', role: 'alert'}, icon('alert'),
        h('span', {}, "Reachable from other devices and no login set up. The app won't start like this: ", d.access.problems[0].split(':')[0], '.')) : null,
      accessDetail(d, mode));
  }

  // ---- AI provider -------------------------------------------------------------------------------------------------
  const PRESETS = [['openrouter', 'OpenRouter'], ['openai', 'OpenAI'], ['local', 'Local (Ollama / LM Studio)'], ['custom', 'Custom URL']];
  function aiKey() {
    const key = h('input', {type: 'password', autocomplete: 'off', spellcheck: 'false', placeholder: 'Paste the key'});
    editorSheet('API key', `Kept in ${D.secrets.path} on this computer, never shown again.`, [fld('API key', key)],
      () => (key.value.trim() ? post('/api/secrets/ai', {key: key.value}) : null), reload);
  }
  function capInput(ai) {
    return saveOnLeave(h('input', {value: ai.monthly_cap.toFixed(2), 'aria-label': 'Monthly spend cap', inputmode: 'decimal'}),
      (v) => { const n = Number(String(v).replace(/[^0-9.]/g, '')); return save({'ai.monthly_cap': Number.isFinite(n) ? n : -1}); });
  }
  // The CLI brains (ai.mode "cli"): the chosen local CLI, its stored isolation check for the version found now, and
  // the terms note. The check is the same POST Settings has always had; its answer is one item, drawn in place.
  function aiCli(d, redraw) {
    const cli = d.ai.cli;
    const item = cli.items.find((i) => i.id === cli.chosen) || cli.items[0];
    const state = !item.found ? 'missing'
      : item.checked_version && item.checked_version === item.version ? (item.passed ? 'passed' : 'failed') : 'unchecked';
    const strip = state === 'passed' ? pill('sm', 'check', 'Locked down')
      : state === 'failed' ? pill('sm bad', 'alert', 'Not used')
      : pill('sm nt', null, state === 'missing' ? 'Not found' : 'Not checked');
    let sub;
    if (state === 'passed') sub = item.id === 'codex'
      ? 'Its tool calls were rejected in the check, so it can only answer. Checked again whenever the version changes.'
      : 'Started with no tools and no MCP servers, so it can only answer. Checked again whenever the version changes.';
    else if (state === 'failed') sub = item.reason && item.reason.length <= 160
      ? item.reason : 'One tool stayed reachable after lockdown on this version. Ask stays off until a version passes.';
    else if (state === 'unchecked') sub = 'Not checked on this version yet. The check sends one short request with this CLI and reads what it could reach.';
    else sub = `${item.name} isn't installed on this computer.`;
    if (cli.off) sub += ' The CLI brains are switched off on this computer (HERMES_CLI_BRAINS=off).';
    const checkPill = state === 'passed' ? pill('sm', 'check', `Passed · ${item.checked_version}`)
      : state === 'failed' ? pill('sm bad', 'x', `Failed · ${item.checked_version}`)
      : pill('sm nt', null, state === 'missing' ? 'Not found' : 'Not checked');
    const checkBtn = h('button', {class: 'btn sm ghost', type: 'button', disabled: cli.off,
      onclick: async () => {
        checkBtn.disabled = true;
        fill(checkBtn, icon('refresh'), 'Checking…');
        try {
          const r = await post('/api/ai/cli/check', {cli: item.id});
          Object.assign(item, r.item);
          redraw();
        } catch (e) {
          toast(e.message, [], {error: true, timeout: 7000});
          checkBtn.disabled = false;
          fill(checkBtn, icon('refresh'), 'Check again');
        }
      }}, icon('refresh'), 'Check again');
    const plan = item.id === 'codex' ? 'ChatGPT subscription' : 'Claude subscription';
    return [
      h('div', {class: 'aist'}, ibadge('terminal', RGB.purple, 's'),
        h('span', {class: 'grow'}, h('b', {}, item.found && item.version ? `${item.name} ${item.version}` : item.name),
          h('span', {}, item.found ? `Found at ${item.display || item.path}` : 'Not found on this computer')), strip),
      srow('Which CLI', 'Runs on this computer with your own login',
        select([['claude', 'Claude Code'], ['codex', 'Codex']], cli.chosen, 'Which CLI',
          (v) => save({'ai.cli': v}, () => { cli.chosen = v; redraw(); })), 'tight'),
      srow('Isolation check', sub, h('span', {class: 'pairc'}, checkPill, checkBtn), 'tight'),
      srow('Changes still ask you first', 'It suggests categories and rules. Nothing is applied without your OK, same as today.',
        pill('sm nt', 'lock', 'Always'), 'tight'),
      h('div', {class: 'safety'}, icon('alert'),
        h('span', {}, `This runs on your ${plan} outside its own app. Check that your plan allows it. It counts against that plan's limits, not the monthly cap here.`)),
    ];
  }
  function aiBody(d, mode, redraw) {
    const ai = d.prefs.ai;
    if (mode === 'hermes') {
      return [h('div', {class: 'aist'}, ibadge('sparkles', RGB.purple, 's'), h('span', {class: 'grow'}, h('b', {}, 'Using your Hermes model'),
        h('span', {}, d.ai.hermes_detected ? 'Hermes is on this computer' : "Hermes isn't found on this computer")),
      d.ai.hermes_detected ? pill('sm', 'check', 'Detected') : pill('sm nt', null, 'Not found')),
      h('p', {class: 'hint'}, 'Via Hermes'),
      // the bridge (chat.py, bridge.py): the same cap as the API key's, on estimates (Hermes reports no price)
      srow('Monthly spend cap', `Monthly limit for Ask · ${HF.money2(d.ai.spent || 0)} used`,
        h('span', {class: 'pairc'}, inp(capInput(ai), 'tin', HF.CUR.sym), h('span', {class: 'mut'}, '/ month')), 'tight')];
    }
    if (mode === 'off') {
      return [h('div', {class: 'aist'}, ibadge('x', RGB.grey, 's'), h('span', {class: 'grow'}, h('b', {}, 'Dashboard only'),
        h('span', {}, 'No chat and no auto-classify. Rules, Plaid categories and your own choices still work.')))];
    }
    if (mode === 'cli') return aiCli(d, redraw);
    const endpoint = saveOnLeave(h('input', {value: ai.base_url, 'aria-label': 'Endpoint', spellcheck: 'false', autocapitalize: 'off',
      placeholder: 'https://example.com/v1'}), (v) => save({'ai.base_url': v}));
    const model = saveOnLeave(h('input', {value: ai.model, 'aria-label': 'Model', spellcheck: 'false', autocapitalize: 'off',
      placeholder: 'e.g. anthropic/claude-haiku-4.5'}), (v) => save({'ai.model': v}));
    const cap = capInput(ai);
    return [
      srow('Provider', 'Any OpenAI-compatible endpoint', select(PRESETS, ai.preset, 'Provider preset', (v) => save({'ai.preset': v,
        ...(v !== 'custom' ? {'ai.base_url': d.ai.presets[v]} : {})}, reload)), 'tight'),
      srow('Endpoint', null, inp(endpoint, 'fx code'), 'tight'),
      srow('Model', null, inp(model, 'fx code'), 'tight'),
      srow('API key', `Kept in ${d.secrets.path}, never shown again`, h('span', {class: 'pairc'},
        d.ai.key_set ? pill('sm', 'check', 'Set') : pill('sm nt', null, 'Not set'),
        h('button', {class: 'btn sm ghost', type: 'button', onclick: aiKey}, d.ai.key_set ? 'Replace' : 'Add key')), 'tight'),
      srow('Monthly spend cap', `Monthly limit for Ask · ${HF.money2(d.ai.spent || 0)} used`,   // TASK-23 (review C3)
        h('span', {class: 'pairc'}, inp(cap, 'tin', HF.CUR.sym), h('span', {class: 'mut'}, '/ month')), 'tight'),
      srow('Check it works', 'Sends one short request with this key and model',
        h('span', {class: 'pairc'}, h('span', {class: 'soon'}, 'Coming in the next update'),
          h('button', {class: 'btn sm ghost', type: 'button', disabled: true}, icon('refresh'), 'Test connection')), 'tight'),
    ];
  }
  function aiCard(d) {
    const body = h('div', {class: 'aibody'});
    const redraw = () => fill(body, aiBody(d, d.prefs.ai.mode, redraw));
    fill(body, aiBody(d, d.prefs.ai.mode, redraw));
    const conf = h('input', {type: 'range', min: '0.5', max: '0.95', step: '0.05', value: String(d.prefs.ai.min_confidence),
      'aria-label': 'Auto-apply confidence'});
    const val = h('b', {class: 'sv v'}, d.prefs.ai.min_confidence.toFixed(2).replace(/0$/, ''));
    conf.addEventListener('input', () => { val.textContent = Number(conf.value).toFixed(2).replace(/0$/, ''); });
    conf.addEventListener('change', () => save({'ai.min_confidence': Number(conf.value)}));
    const help = HF.help('About the monthly limit', () => h('p', {}, 'A soft cap on Ask: it sends one request at a time and stops once this month’s spend reaches it (it can end over by that one).',
      d.prefs.ai.mode === 'hermes' ? ' Hermes reports no price, so each request is an estimate from its size.' : ''), {key: 'ai-cap'});
    return card({cls: 'sc', title: 'AI provider', icon: 'sparkles', rgb: RGB.purple, help},
      seg([['hermes', 'Hermes'], ['api', 'API key'], ['cli', 'CLI'], ['off', 'Off']], d.prefs.ai.mode, 'AI provider', (k, draw) => save({'ai.mode': k}, () => {
        d.prefs.ai.mode = k;
        draw(k);
        fill(body, aiBody(d, k, redraw));
      }), 'aiseg'),
      body,
      gh('Classifier'),
      h('div', {class: 'srow col'}, h('span', {class: 'grow'}, h('b', {}, 'Auto-apply confidence'),
        h('span', {}, 'Auto-file above this confidence')),
      h('div', {class: 'range'}, conf, val)),
      h('div', {class: 'sticks'}, h('span', {}, '0.5 · more filed for you'), h('span', {}, '0.95 · more to review')));
  }

  // ---- Connect other agents (MCP) ------------------------------------------------------------------------------------
  function snippets(m) {
    const url = m.endpoint;
    const needs = m.token_needed;
    const desktop = {mcpServers: {'hermes-finance': {command: m.command, args: ['mcp'], ...(m.data_dir ? {env: {FINANCE_DATA_DIR: m.data_dir}} : {})}}};
    const cursor = {mcpServers: {'hermes-finance': {url, ...(needs ? {headers: {Authorization: 'Bearer <your token>'}} : {})}}};
    const codex = `[mcp_servers.hermes-finance]\nurl = "${url}"${needs ? '\nbearer_token_env_var = "HERMES_FINANCE_TOKEN"' : ''}`;
    return {
      'Claude Desktop': {file: 'claude_desktop_config.json', lang: 'JSON', text: JSON.stringify(desktop, null, 2),
        note: ''},
      Cursor: {file: '~/.cursor/mcp.json', lang: 'JSON', text: JSON.stringify(cursor, null, 2), note: needs ? 'Put a token from the list below in place of <your token>.' : ''},
      Codex: {file: '~/.codex/config.toml', lang: 'TOML', text: codex, note: needs ? 'Then export HERMES_FINANCE_TOKEN=<your token> where Codex runs.' : ''},
    };
  }
  function newToken(m, box) {
    const name = h('input', {'aria-label': 'Agent name', placeholder: 'e.g. Claude Desktop', maxlength: '60'});
    const make = h('button', {class: 'btn sm', type: 'button'}, 'Create');
    const run = async () => {
      try {
        const t = await post('/api/mcp/tokens', {name: name.value});
        const copy = h('button', {class: 'btn sm ghost', type: 'button', onclick: async (e) => {
          try { await navigator.clipboard.writeText(t.token); e.currentTarget.lastChild.textContent = 'Copied'; } catch (err) { e.currentTarget.lastChild.textContent = 'Select and copy'; }
        }}, icon('copy'), 'Copy');
        fill(box, h('div', {class: 'token-new', role: 'status'}, h('b', {}, `Token for ${t.name}`), ' · shown once, copy it now',
          h('span', {class: 'mono'}, t.token), h('div', {class: 'row'}, copy, h('button', {class: 'btn sm ghost', type: 'button', onclick: reload}, 'Done'))));
      } catch (e) {
        failed(e);
      }
    };
    make.addEventListener('click', run);
    name.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
    fill(box, h('div', {class: 'srow tight'}, h('span', {class: 'inp fx'}, name), make));
    name.focus();
  }
  // TASK-21: the changes MCP agents asked for, waiting for the owner. Approve makes the change exactly as the Hermes
  // plugin does after its approval card (checked again first); Decline changes nothing.
  function pendingRows(m) {
    const list = m.pending_actions || [];
    if (!list.length) return null;
    const decide = async (a, ok) => {
      try {
        const r = await post(`/api/mcp/pending/${ok ? 'approve' : 'decline'}`, {id: a.id});
        if (r.action.status === 'failed') toast(r.action.result.uncertain ? `Interrupted while applying it (${r.action.result.error}): it may be partly done, check before trying again`
          : `Not applied: ${r.action.result.error}`, [], {error: true, timeout: 7000});
        else saved(ok ? 'Approved and applied' : 'Declined: nothing changed');
        reload();
      } catch (e) {
        failed(e);
      }
    };
    const what = {finance_track_bill: 'Track a bill', finance_set_cap: 'Set a budget cap', finance_set_category: 'File a transaction',
      finance_add_rule: 'Add a rule'};
    return [gh(`Waiting for your OK (${list.length})`), list.map((a) => h('div', {class: 'srow tight', dataset: {pending: String(a.id)}},
      ibadge('pencil', RGB.warn, 's'), h('span', {class: 'grow'}, h('b', {}, what[a.tool] || a.tool), h('span', {}, a.summary),
        h('span', {class: 'v'}, (a.origin === 'ask' || a.origin === 'ask_hermes') ? `Ask suggested it ${ago(a.created_at)}` : `${a.client || 'An agent'} asked ${ago(a.created_at)}`)),   // TASK-23: the Ask panel's cards, by origin (review C1: a client can call itself Ask)
      h('span', {class: 'pairc'}, h('button', {class: 'btn sm', type: 'button', onclick: () => decide(a, true)}, 'Approve'),
        h('button', {class: 'btn sm ghost', type: 'button', onclick: () => decide(a, false)}, 'Decline'))))];
  }

  function mcpCard(d) {
    const m = d.mcp;
    const cfg = snippets(m);
    let client = 'Claude Desktop';
    const tabs = h('span');
    const det = h('details', {class: 'cfg'});
    const draw = () => {
      fill(tabs, seg(Object.keys(cfg).map((k) => [k, k]), client, 'Agent', (k) => { client = k; draw(); }));
      const c = cfg[client];
      fill(det, h('summary', {}, icon('chevr'), `Show the config (${c.lang}, ${plural(c.text.split('\n').length, 'line')}) · paste into ${c.file}`),
        h('pre', {class: 'mono'}, c.text), c.note ? h('p', {class: 'hint'}, c.note) : null);
    };
    draw();
    const copyBtn = h('button', {class: 'btn sm', type: 'button', onclick: async (e) => {
      const b = e.currentTarget;
      try {
        await navigator.clipboard.writeText(cfg[client].text);
        b.lastChild.textContent = 'Copied';
      } catch (err) {
        det.open = true;
        b.lastChild.textContent = 'Select and copy';
      }
      setTimeout(() => { b.lastChild.textContent = 'Copy config'; }, 1600);
    }}, icon('copy'), 'Copy config');
    const tokenBox = h('div');
    return card({cls: 'sc', title: 'Connect other agents (MCP)', icon: 'plug', rgb: RGB.cyan},
      srow('MCP server', m.installed ? 'Lets Claude Desktop, Cursor or Codex read your finances' : 'Needs the mcp extra: uv sync --extra mcp',
        sw(m.enabled, 'MCP server', (on) => save({'mcp.enabled': on}))),
      srow('Local endpoint', 'On this computer (127.0.0.1)', h('span', {class: 'mono ep'}, epText(m.endpoint)), 'tight'),
      m.public_endpoints.map((u) => srow('Through your proxy', 'Needs a token', h('span', {class: 'mono ep'}, epText(u)), 'tight')),
      h('div', {class: 'srow col'}, h('span', {class: 'grow'}, h('b', {}, 'Copy config'), h('span', {}, 'Pick your agent, copy, paste into its settings, restart it')),
        h('div', {class: 'cfgrow'}, tabs, copyBtn), det),
      gh('Access'),
      srow('What agents can do', 'Balances, transactions, budgets, bills and loans', m.allow_writes ? pill('sm warn', 'pencil', 'Can ask for changes') : pill('sm acc', 'lock', 'Read-only'), 'tight'),
      srow('Allow writes (asks for approval)', 'Agent changes need your OK',
        sw(m.allow_writes, 'Allow writes', (on) => save({'mcp.allow_writes': on}, reload)), 'tight'),
      pendingRows(m),
      gh('Tokens'),
      m.tokens.length ? m.tokens.map((t) => h('div', {class: 'srow tight'}, ibadge('key', RGB.cyan, 's'), h('span', {class: 'grow'}, h('b', {}, t.name),
        h('span', {class: 'v'}, `${t.masked} · made ${whenShort(t.created_at)} · ${t.last_used_at ? `last used ${ago(t.last_used_at)}` : 'not used yet'}`)),
      h('button', {class: 'btn sm danger', type: 'button', onclick: async () => {
        try {
          await post('/api/mcp/tokens/revoke', {id: t.id});
          saved('Revoked');
          reload();
        } catch (e) {
          failed(e);
        }
      }}, 'Revoke'))) : h('p', {class: 'hint'}, 'No tokens'),
      tokenBox,
      h('div', {class: 'sfootl'}, h('button', {class: 'lnk', type: 'button', onclick: () => newToken(m, tokenBox)}, icon('plus'), 'New token')));
  }

  // ---- Sync & alerts ---------------------------------------------------------------------------------------------
  const DAYS = [['sun', 'Sunday'], ['mon', 'Monday'], ['tue', 'Tuesday'], ['wed', 'Wednesday'], ['thu', 'Thursday'], ['fri', 'Friday'], ['sat', 'Saturday']];
  function syncCard(d) {
    const p = d.prefs;
    // 24-hour HH:MM (the browser's own time field shows 12-hour AM / PM in en-US, too wide for the row)
    const time = (key, label) => inp(saveOnLeave(h('input', {type: 'text', inputmode: 'numeric', maxlength: '5', pattern: '[0-2][0-9]:[0-5][0-9]',
      placeholder: 'HH:MM', value: p[key.split('.')[0]][key.split('.')[1]], 'aria-label': label}), (v) => save({[key]: v.trim()})), 'tin');
    const dest = saveOnLeave(h('input', {value: p.alerts.destination, 'aria-label': 'Alerts go to', maxlength: '60'}), (v) => save({'alerts.destination': v}));
    return card({cls: 'sc', title: 'Sync & alerts', icon: 'bell', rgb: RGB.warn},
      srow('Daily sync', 'Every day at this time', time('sync.daily_time', 'Daily sync time')),
      srow('Weekly summary', 'Spending, budgets and bills for the week', h('span', {class: 'pairc'},
        select(DAYS, p.sync.weekly_day, 'Summary day', (v) => save({'sync.weekly_day': v})), time('sync.weekly_time', 'Summary time'))),
      srow('Alerts go to', 'Where the summary and alerts are sent', inp(dest)),
      gh('Alert me when'),
      srow('A budget reaches 80%', null, sw(p.alerts.cap_80, 'A budget reaches 80%', (on) => save({'alerts.cap_80': on})), 'tight'),
      srow('A budget goes over its cap', null, sw(p.alerts.over_cap, 'A budget goes over its cap', (on) => save({'alerts.over_cap': on})), 'tight'),
      srow('A bank connection breaks', null, sw(p.alerts.broken_connection, 'A bank connection breaks', (on) => save({'alerts.broken_connection': on})), 'tight'),
      h('p', {class: 'hint'}, 'Your Hermes jobs read these: ', h('span', {class: 'mono nw'}, 'finance settings --json')));
  }

  // ---- Valuations ------------------------------------------------------------------------------------------------
  // None of these needs a key. Carfax works through a Chrome with remote debugging on this computer (the app only
  // checks it's there; it never starts one); Redfin is plain HTTPS; the photos come from Wikimedia Commons.
  const CHROME_HOW = [['macOS', 'open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-hermes"'],
    ['Linux', 'google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-hermes"'],
    ['Windows', 'start chrome --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\\.chrome-hermes"']];
  function needs(v) {
    if (v.key === 'carfax') {
      return v.chrome.ready ? 'Ready \u00b7 uses your local Chrome'
        : 'Needs Chrome with remote debugging on port 9222 \u00b7 Car values stay manual until then';
    }
    return v.key === 'redfin' ? 'No key needed (Chrome only if Redfin refuses plain HTTPS)' : 'No key needed';
  }
  function valuationsCard(d) {
    return card({cls: 'sc', title: 'Valuations', icon: 'car', rgb: RGB.cyan},
      d.valuations.map((v) => h('div', {class: 'srow'}, h('span', {class: 'grow'}, h('b', {}, v.name),
        h('span', {}, v.what), h('span', {class: v.key === 'carfax' && !v.chrome.ready ? 'warnl' : ''}, needs(v)),
        v.env_flag ? h('span', {}, `Set by ${v.env_flag} in your env file`) : null,
        h('span', {class: 'use'}, meter(v.cap ? v.used / v.cap : 0, `${v.name} calls this month`, v.cap, v.used), `Used this month ${v.used} / ${v.cap}`),
        v.key === 'carfax' && !v.chrome.ready ? h('details', {class: 'cfg'}, h('summary', {}, icon('chevr'), 'How'),
          h('p', {class: 'hint'}, 'Start Chrome with remote debugging (one line), then open this page again:'),
          CHROME_HOW.map(([os, cmd]) => h('div', {class: 'howos'}, h('b', {}, os), h('pre', {class: 'mono'}, cmd)))) : null),
      sw(v.on, v.name, (on) => save({[`valuations.${v.key}`]: on}), Boolean(v.env_flag)))));
  }

  // ---- Region ------------------------------------------------------------------------------------------------------
  function sample(locale, currency) {
    try {
      const date = new Date(2026, 8, 28).toLocaleDateString(locale, {month: 'short', day: 'numeric', year: 'numeric'});
      return `${date} · ${new Intl.NumberFormat(locale, {style: 'currency', currency}).format(1234.56)}`;
    } catch (e) {
      return '';
    }
  }
  function symbolOf(currency) {
    try {
      return new Intl.NumberFormat(HF.LOC, {style: 'currency', currency}).formatToParts(1).find((x) => x.type === 'currency').value;
    } catch (e) {
      return currency;
    }
  }
  function regionCard(d) {
    const r = d.prefs.region;
    const again = () => location.reload();  // numbers and dates are formatted once per page load
    return card({cls: 'sc', title: 'Region', icon: 'globe', rgb: RGB.ok},
      srow('Currency', 'Totals count only accounts in this currency. Nothing is converted.', select(d.options.currencies.map((c) => [c, `${c} · ${symbolOf(c)}`]), r.currency, 'Currency',
        (v) => save({'region.currency': v}, again)), 'tight'),
      srow('Number and date format', sample(r.locale, r.currency), select(d.options.locales.map((l) => [l, l]), r.locale, 'Locale',
        (v) => save({'region.locale': v}, again)), 'tight'),
      srow('Week starts on', null, seg([['sun', 'Sunday'], ['mon', 'Monday']], r.week_start, 'Week starts on', (k, draw) => save({'region.week_start': k}, () => draw(k))), 'tight'),
      srow('Budget month starts on', null, select([[1, 'Day 1']], 1, 'Budget month start', () => {}), 'tight'));
  }

  // ---- Appearance --------------------------------------------------------------------------------------------------
  function lookCard(d) {
    const pref = d.prefs.appearance.theme;
    return card({cls: 'sc', title: 'Appearance', icon: 'palette', rgb: RGB.purple},
      srow('Theme', 'System follows this device', seg([['dark', 'Dark', 'moon'], ['light', 'Light', 'sun'], ['system', 'System', 'monitor']], pref, 'Theme',
        (k, draw) => save({'appearance.theme': k}, () => { draw(k); window.HFTheme.set(k); })), 'col'));
  }

  // ---- Data --------------------------------------------------------------------------------------------------------
  // One header explanation covers export, backup and import, including the transaction count.
  function dataCard(d) {
    const help = HF.help('About your data', () => [
      h('p', {}, `Export CSV saves every transaction (${d.counts.transactions}) as a spreadsheet, with its category and notes.`),
      h('p', {}, 'Download backup saves the whole database without bank tokens or sign-in secrets. After a restore you reconnect each bank.'),
      h('p', {}, 'Import reads CSV, OFX or QFX from any bank. CSV columns are matched before you confirm.'),
    ], {key: 'data', long: true});
    return card({cls: 'sc', title: 'Data', icon: 'database', rgb: RGB.grey, help},
      h('div', {class: 'data-actions'},
        h('div', {}, h('a', {class: 'btn ghost', href: '/api/export.csv', download: ''}, icon('download'), 'Export CSV')),
        h('div', {}, h('a', {class: 'btn ghost', href: '/api/backup', download: ''}, icon('archive'), 'Download backup')),
        h('div', {}, h('button', {class: 'btn ghost', type: 'button', onclick: () => HF.openAdd('import')}, icon('upload'), 'Import'))));
  }

  // ---- Categories (v23) ---------------------------------------------------------------------------------------------
  // Settings -> Categories: above Categorization rules, collapsed to one summary line and opening in place the same way,
  // in the rules card's look. Open: Search categories + New category, then each group's header and its rows, the first
  // CAT_SHOWN rows then "Show all N" (a search always looks through all of them). A row: icon, name, a Yours tag on the
  // ones you made, a Hidden tag on hidden ones (greyed, in their group), how many transactions are filed under it, and
  // its actions, icons on a wide screen and one ⋯ on a phone: Edit (a built-in, Income and Transfers: the icon only),
  // Hide / Show (spending categories), Delete (yours only). Delete confirms in the row, asking where its transactions
  // go. Every action redraws this card only, then invalidates the app's category cache.
  const CAT_SHOWN = 10;
  let CATS = null;   // the last GET /api/categories/page
  // the eight groups, from the payload (the Income & transfers bucket after them has no colour)
  const groupNames = () => (CATS ? CATS.groups.filter((g) => g.rgb).map((g) => g.name) : []);
  const groupRgb = (name) => {
    const g = CATS && CATS.groups.find((x) => x.name === name);
    return (g && g.rgb) || '155,162,182';
  };
  const groupFallbackIcon = (name) => {
    const g = CATS && CATS.groups.find((x) => x.name === name);
    return (g && g.categories.length && g.categories[0].icon) || 'tag';
  };

  async function loadCats() {
    if (!CATS) CATS = await api('/api/categories/page');
    return CATS;
  }

  function categorySheet(c, opts = {}) {
    const built = Boolean(c && c.built_in);
    const structural = Boolean(c && !c.spending);
    const state = {icon: (c && c.icon) || groupFallbackIcon('Misc')};
    const errs = {name: h('p', {class: 'ferr', role: 'alert'}), group: h('p', {class: 'ferr', role: 'alert'}),
      icon: h('p', {class: 'ferr', role: 'alert'})};
    const name = h('input', {type: 'text', maxlength: '60', value: c ? c.name : (opts.name || ''), disabled: built});
    const groupOptions = structural
      ? [h('option', {value: '', selected: true}, 'Income & transfers')]
      : groupNames().map((g) => h('option', {value: g, selected: g === (c ? c.group : 'Misc')}, g));
    const group = h('select', {class: 'selv', 'aria-label': 'Group', disabled: built || structural}, groupOptions);
    const iconSlot = h('div');
    let picked = Boolean(c && c.icon);
    const picker = () => fill(iconSlot, HF.iconPicker({
      rgb: groupRgb(group.value), value: state.icon, label: 'Category icon',
      onPick: (namePicked) => { picked = true; state.icon = namePicked; }
    }));
    group.addEventListener('change', () => {
      if (!picked) state.icon = groupFallbackIcon(group.value);
      picker();
    });
    const err = h('div', {class: 'ferr', role: 'alert'});
    const saveBtn = h('button', {class: 'btn', type: 'button'}, c ? 'Save' : 'Add category');
    const field = (label, control, hint, key) => h('div', {class: 'fld', dataset: {f: key}},
      h('label', {}, label), h('div', {class: key === 'icon' ? '' : 'inp'}, control), hint ? h('p', {class: 'hint'}, hint) : null,
      errs[key] || null);
    const limits = built ? (structural
      ? 'Income and transfers stay outside the categories: their icon can change, nothing else.'
      : 'A built-in category can change its icon or be hidden, not renamed or moved between groups.') : null;
    const save = async () => {
      for (const slot of Object.values(errs)) slot.textContent = '';
      err.textContent = '';
      let body;
      if (c) {
        body = {name: c.name, icon: state.icon};
        if (!built && !structural) {
          const wanted = name.value.trim();
          if (wanted && wanted !== c.name) body.rename = wanted;
          if (group.value !== c.group) body.group = group.value;
        }
        if (body.icon === c.icon && !body.rename && !body.group) { closeModal(); return; }
      } else {
        body = {name: name.value, group: group.value, icon: state.icon};
      }
      saveBtn.disabled = true;
      try {
        await post(c ? '/api/categories/update' : '/api/categories/create', body);
      } catch (e) {
        saveBtn.disabled = false;
        if (e.field && errs[e.field]) errs[e.field].textContent = e.message;
        else err.textContent = `Not saved: ${e.message}`;
        return;
      }
      closeModal();
      saved(c ? 'Category saved' : 'Category added');
      HF.categoriesChanged();
      await refreshCats();
      if (opts.onDone) opts.onDone(CATS);
    };
    saveBtn.addEventListener('click', save);
    picker();
    openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, c ? (built ? 'Category icon' : 'Edit category') : 'New category'),
      closeBtn()),
      h('p', {class: 'rlead'}, c ? catName(c) : 'A spending category, in one of the eight groups.'),
      field('Name', name, built ? limits : 'Up to 60 characters; names are unique ignoring case.', 'name'),
      field('Group', group, built ? null : 'Counts as spending and joins this group in budgets and charts.', 'group'),
      field('Icon', iconSlot, null, 'icon'),
      err,
      h('div', {class: 'row'}, saveBtn, h('button', {class: 'btn ghost', type: 'button', onclick: closeModal}, 'Cancel'))],
      {narrow: true, cls: 'catsheet'});
  }

  function catName(c) { return c.spending ? c.name : `${c.name} · structural`; }

  async function actCategory(c, body, line) {
    try {
      await post('/api/categories/update', {name: c.name, ...body});
    } catch (e) {
      failed(e);
      return;
    }
    saved(line);
    HF.categoriesChanged();
    await refreshCats();
  }

  async function refreshCats() {
    try {
      CATS = await api('/api/categories/page');
    } catch (e) {
      failed(e);
      return;
    }
    catRedraw();
  }

  let catRedraw = () => {};
  function categoriesCard(first) {
    CATS = first;
    const CS = {open: false, q: '', all: false, menu: null, confirm: null};   // each render starts closed, like the rules card
    const body = h('div', {class: 'rbody catbody'});
    body.id = 'categories-body';
    const sumTxt = h('span', {class: 'rsumt'});
    const sum = h('button', {class: 'rsum', type: 'button', 'aria-expanded': 'false', 'aria-controls': 'categories-body',
      onclick: () => { CS.open = !CS.open; draw(); }}, sumTxt, icon('chevd'));
    const el = card({cls: 'sc cats', title: 'Categories', icon: 'grid', rgb: RGB.warn, extra: sum}, body);
    el.dataset.categories = '';

    const ibtn = (ic, label, onclick, act, cls = '') => h('button', {class: `rib ${cls}`, type: 'button', 'aria-label': label, title: label,
      dataset: {act}, onclick}, icon(ic));
    const ask = (c) => { CS.confirm = c.name; CS.menu = null; draw(); };

    function confirmRow(c) {
      const options = CATS.categories.filter((x) => x.spending && !x.hidden && x.name !== c.name);
      const cancel = h('button', {class: 'btn sm ghost', type: 'button', onclick: () => { CS.confirm = null; draw(); }}, 'Cancel');
      if (!options.length) {
        return h('div', {class: 'rrow confirm catask', dataset: {cat: c.name}},
          h('span', {class: 'grow'}, h('b', {}, `Delete ${c.name}? First add another visible category for its transactions.`)),
          h('span', {class: 'pairc'}, cancel));
      }
      const n = c.transactions;
      const line = n ? `Delete ${c.name}? Its ${plural(n, 'transaction')} ${n === 1 ? 'moves' : 'move'} to`
        : `Delete ${c.name}? It has no transactions. Anything else that uses it moves to`;
      const sel = h('select', {class: 'selv', 'aria-label': 'Move its transactions to'},
        options.map((x) => h('option', {value: x.name}, x.name)));
      sel.value = (options.find((x) => x.group === c.group) || options[0]).name;   // a sibling in its group first
      const go = h('button', {class: 'btn sm danger', type: 'button'}, n ? 'Move and delete' : 'Delete');
      go.addEventListener('click', async () => {
        go.disabled = true;
        try {
          const r = await post('/api/categories/delete', {name: c.name, move_to: sel.value});
          const moved = Object.values(r.moved || {}).reduce((a, b) => a + b, 0);
          saved(moved ? `Deleted ${r.deleted}: moved ${plural(moved, 'row')} to ${r.name}` : `Deleted ${r.deleted}`);
        } catch (e) {
          go.disabled = false;
          failed(e);
          return;
        }
        CS.confirm = null;
        HF.categoriesChanged();
        await refreshCats();
      });
      return h('div', {class: 'rrow confirm catask', dataset: {cat: c.name}},
        h('span', {class: 'grow'}, h('b', {}, line)), h('span', {class: 'pairc'}, sel, go, cancel));
    }

    function row(c) {
      if (CS.confirm === c.name) return confirmRow(c);
      const open = CS.menu === c.name;
      const count = plural(c.transactions || 0, 'transaction');
      const iconOnly = c.built_in || !c.spending;
      const edit = () => { CS.menu = null; categorySheet(c); };
      const hide = c.spending ? () => actCategory(c, {hidden: !c.hidden}, c.hidden ? `${c.name} is visible again` : `${c.name} is hidden`) : null;
      const mine = !c.built_in && c.spending;
      return h('div', {class: `rrow catrow${c.hidden ? ' off' : ''}${open ? ' rmenu-open' : ''}`, dataset: {cat: c.name}},
        ibadge(c.icon || 'tag', c.rgb || groupRgb(c.group), 'xs'),
        h('span', {class: 'grow'}, h('span', {class: 'rpat'}, h('b', {}, c.name), mine ? tag('Yours') : null, c.hidden ? tag('Hidden') : null),
          h('span', {class: 'rsub'}, count)),
        h('span', {class: 'rn v'}, count),
        h('span', {class: 'racts'}, ibtn('pencil', iconOnly ? `Change the ${c.name} icon` : `Edit ${c.name}`, edit, 'edit'),
          hide && !c.hidden ? ibtn('eye-off', `Hide ${c.name}`, hide, 'hide') : null,
          hide && c.hidden ? h('button', {class: 'btn sm ghost catshow', type: 'button', 'aria-label': `Show ${c.name}`, dataset: {act: 'show'}, onclick: hide}, 'Show') : null,
          mine ? ibtn('trash', `Delete ${c.name}`, () => ask(c), 'delete', 'rdel') : null),
        h('button', {class: 'rmore', type: 'button', 'aria-label': `Actions for ${c.name}`, 'aria-expanded': String(open),
          onclick: () => { CS.menu = open ? null : c.name; draw(); }}, '⋯'),
        open ? h('span', {class: 'rmenu', role: 'group', 'aria-label': 'Category actions'},
          h('button', {class: 'btn sm ghost', type: 'button', dataset: {act: 'edit'}, onclick: edit}, icon('pencil'), iconOnly ? 'Change icon' : 'Edit'),
          hide ? h('button', {class: 'btn sm ghost', type: 'button', dataset: {act: c.hidden ? 'show' : 'hide'}, onclick: hide},
            icon('eye-off'), c.hidden ? 'Show' : 'Hide') : null,
          mine ? h('button', {class: 'btn sm danger', type: 'button', dataset: {act: 'delete'}, onclick: () => ask(c)}, icon('trash'), 'Delete') : null)
          : null);
    }

    function listPart() {
      const q = CS.q.trim().toLowerCase();
      const total = CATS.categories.length;
      let left = CS.all || q ? Infinity : CAT_SHOWN;
      const parts = [];
      for (const g of CATS.groups) {
        const cats = g.categories.filter((c) => !q || c.name.toLowerCase().includes(q));
        if (!cats.length || left <= 0) continue;
        const shown = cats.slice(0, left);
        left -= shown.length;
        parts.push(gh(`${g.name} · ${cats.length}`), shown.map(row));
      }
      return h('div', {class: 'rlist'},
        parts.length ? parts : h('p', {class: 'hint rnone', dataset: {empty: ''}}, 'No categories match.'),
        !q && !CS.all && total > CAT_SHOWN ? h('button', {class: 'more rall', type: 'button', onclick: () => { CS.all = true; draw(); }},
          `Show all ${total}`) : null);
    }
    const search = h('input', {class: 'search rq', type: 'search', placeholder: 'Search categories', 'aria-label': 'Search categories',
      autocomplete: 'off', spellcheck: 'false'});
    const listSlot = h('div');
    search.addEventListener('input', () => { CS.q = search.value; CS.menu = null; fill(listSlot, listPart()); });
    const addNew = h('button', {class: 'btn sm catnew', type: 'button',
      onclick: () => categorySheet(null, {onDone: () => { CS.all = true; draw(); }})}, icon('plus'), 'New category');

    const draw = () => {
      if (!CATS) {
        fill(sumTxt, "Couldn't load the categories");
        body.hidden = true;
        return;
      }
      const counts = CATS.counts;
      fill(sumTxt, `${plural(counts.total, 'category', 'categories')} · ${counts.user} yours · ${counts.hidden} hidden`);
      sum.setAttribute('aria-expanded', String(CS.open));
      el.classList.toggle('open', CS.open);
      body.hidden = !CS.open;
      if (!CS.open) { fill(body); return; }
      search.value = CS.q;
      fill(listSlot, listPart());
      fill(body, h('div', {class: 'rbar'}, search, addNew), listSlot);
    };
    catRedraw = draw;
    draw();
    return el;
  }

  // The + New category row inside any category picker opens this and then picks the new one (catpick.js).
  HF.categoryNew = async (opts = {}) => {
    if (!CATS) await loadCats().catch(() => null);
    if (!CATS) { toast('Could not load the categories', [], {error: true, timeout: 5000}); return; }
    categorySheet(null, opts);
  };

  // ---- Categorization rules (TASK-24) -------------------------------------------------------------------------------
  // GET /api/rules (every rule newest first, its counts and the suggestions); edits through /api/rules/*. Collapsed to
  // one summary line; open: suggestions (at most 3 shown), search + All / Yours / AI, one dense row per rule (pattern ·
  // → category · matches · actions: icons on a wide screen, one ⋯ on a phone), 20 shown then "Show all N". Edit is a
  // small sheet; Delete confirms inline in the row and its toast has Undo. The card redraws itself, never the page.
  const RULE_SHOWN = 20;
  function rulesCard(first) {
    const RU = {open: false, filter: 'all', q: '', all: false, sugAll: false, menu: null, confirm: null};
    let R = first;
    const body = h('div', {class: 'rbody'});
    const sumTxt = h('span', {class: 'rsumt'});
    const sum = h('button', {class: 'rsum', type: 'button', 'aria-expanded': 'false', 'aria-controls': 'rules-body',
      onclick: () => { RU.open = !RU.open; draw(); }}, sumTxt, icon('chevd'));
    body.id = 'rules-body';
    const el = card({cls: 'sc rules', title: 'Categorization rules', icon: 'tag', rgb: RGB.acc, extra: sum}, body);
    el.dataset.rules = '';
    const refresh = async () => {
      try {
        R = await api('/api/rules');
      } catch (e) {
        failed(e);
      }
      draw();
    };
    const summary = () => {
      if (!R) return "Couldn't load the rules";
      const c = R.counts;
      const s = (R.suggestions || []).length;
      return [`${plural(c.total, 'rule')} · ${c.user} yours`, s ? h('b', {class: 'rsg'}, ` · ${plural(s, 'suggestion')}`) : null];
    };
    const labelOf = (r) => {                            // the pattern in bold inside its plain-words label
      const i = r.label.lastIndexOf(r.pattern);
      return i > 0 ? [r.label.slice(0, i), h('b', {}, r.pattern), r.label.slice(i + r.pattern.length)] : r.label;
    };
    // the server's own sentence (it words an AI override differently), its merchant and category in bold
    const boldIn = (text, words) => {
      const out = [];
      let rest = String(text || '');
      for (const w of words) {
        const i = w ? rest.indexOf(w) : -1;
        if (i < 0) continue;
        out.push(rest.slice(0, i), h('b', {}, w));
        rest = rest.slice(i + w.length);
      }
      out.push(rest);
      return out;
    };
    async function act(url, payload, done) {
      try {
        const r = await post(url, payload);
        done(r);
      } catch (e) {
        failed(e);
      }
      RU.menu = RU.confirm = null;
      await refresh();
    }
    const refiled = (n) => ` · ${plural(n, 'charge')} re-filed`;
    function undoDelete(r) {
      act('/api/rules/restore', {undo: r.undo}, () => saved('Rule restored'));
    }
    const keep = (r) => act('/api/rules/keep', {id: r.id}, () => saved(['Kept ', h('b', {}, r.pattern), ': it’s yours now']));
    const del = (r) => act('/api/rules/delete', {id: r.id}, (x) => toast(['Deleted ', h('b', {}, r.pattern), '. Filed charges stay as they are.'],
      [{label: 'Undo', primary: true, onclick: () => { HF.hideToast(); undoDelete(x); }}], {timeout: 8000}));
    async function edit(r) {
      let cats = [];
      try {
        cats = (await HF.ensureCategories()).map((c) => c.name);
      } catch (e) { /* the rule's own category at least */ }
      if (!cats.includes(r.category)) cats.unshift(r.category);
      const cat = select(cats.map((n) => [n, n]), r.category, 'Category', () => {});
      const pat = r.source === 'user' ? h('input', {value: r.pattern, spellcheck: 'false', autocapitalize: 'off', maxlength: '120'}) : null;
      // the sheet stays open until the server has saved it: an empty pattern, or an error, is shown in it (nothing sent,
      // nothing lost), whether or not the category changed too
      const err = h('div', {class: 'ferr', role: 'alert'});   // a div: `.sheet > p` would paint it muted
      if (pat) pat.addEventListener('input', () => { err.textContent = ''; pat.removeAttribute('aria-invalid'); });
      let saveBtn = null;
      const save = async () => {
        const p = {id: r.id, category: cat.value};
        err.textContent = '';
        if (pat) {
          const v = pat.value.trim();
          if (!v) {
            pat.setAttribute('aria-invalid', 'true');       // (an empty aria-invalid reads as "false")
            err.textContent = 'Enter what the rule matches. To stop using it, delete the rule instead.';
            pat.focus();
            return;
          }
          pat.removeAttribute('aria-invalid');
          if (v !== r.pattern) p.pattern = v;
        }
        if (p.category === r.category && !p.pattern) { closeModal(); return; }
        let x;
        saveBtn.disabled = true;                          // one POST per tap, however fast the taps
        try {
          x = await post('/api/rules/update', p);
        } catch (e) {
          err.textContent = `Not saved: ${e.message}`;
          saveBtn.disabled = false;
          return;
        }
        closeModal();
        saved(['Rule saved', refiled(x.refiled)]);
        RU.menu = RU.confirm = null;
        await refresh();
      };
      openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Edit rule'), closeBtn()),
        h('p', {class: 'rlead'}, labelOf(r)),
        pat ? fld(r.type === 'regex' ? 'Name contains' : 'Merchant is', pat) : null,
        h('div', {class: 'fld'}, h('span', {class: 'lbl'}, 'File under'), cat),
        err,
        h('p', {class: 'hint'}, `${r.source === 'user' ? 'Made by you' : 'Made by AI'} · ${whenShort(r.created_at)} · matches ${plural(r.matches, 'transaction')} now. `
          + 'Saving re-files the charges it matches, except ones you filed yourself.'),
        h('div', {class: 'row'}, saveBtn = h('button', {class: 'btn', type: 'button', onclick: save}, 'Save'),
          h('button', {class: 'btn ghost', type: 'button', onclick: closeModal}, 'Cancel'))], {narrow: true, cls: 'rsheet'});
    }
    const ibtn = (ic, label, onclick, cls = '') => h('button', {class: `rib ${cls}`, type: 'button', 'aria-label': label, title: label, onclick}, icon(ic));
    function row(r) {
      if (RU.confirm === r.id) {
        return h('div', {class: 'rrow confirm', dataset: {rule: String(r.id)}}, h('span', {class: 'grow'}, h('b', {}, 'Delete? Filed charges stay.')),
          h('span', {class: 'pairc'}, h('button', {class: 'btn sm danger', type: 'button', onclick: () => del(r)}, 'Delete'),
            h('button', {class: 'btn sm ghost', type: 'button', onclick: () => { RU.confirm = null; draw(); }}, 'Cancel')));
      }
      const ask = () => { RU.confirm = r.id; RU.menu = null; draw(); };
      const open = RU.menu === r.id;
      const n = h('span', {class: 'rn v', title: 'Transactions it matches now'}, plural(r.matches, 'match', 'matches'));
      return h('div', {class: `rrow${open ? ' rmenu-open' : ''}`, dataset: {rule: String(r.id), source: r.source}},
        h('span', {class: 'grow'}, h('span', {class: 'rpat', title: r.label}, labelOf(r)),
          h('span', {class: 'rsub'}, `→ ${r.category} · ${plural(r.matches, 'match', 'matches')}`)),
        h('span', {class: 'rcat'}, h('span', {class: 'rar', 'aria-hidden': 'true'}, '→'), HF.badge(r.category, 'xs'), h('span', {class: 'rcn'}, r.category)),
        n,
        h('span', {class: 'racts'}, ibtn('pencil', 'Edit', () => edit(r)), r.source === 'ai' ? ibtn('bookmark', 'Keep as yours', () => keep(r)) : null,
          ibtn('trash', 'Delete', ask, 'rdel')),
        h('button', {class: 'rmore', type: 'button', 'aria-label': `Actions for ${r.label}`, 'aria-expanded': String(open),
          onclick: () => { RU.menu = open ? null : r.id; draw(); }}, '⋯'),
        open ? h('span', {class: 'rmenu', role: 'group', 'aria-label': 'Rule actions'},
          h('button', {class: 'btn sm ghost', type: 'button', onclick: () => edit(r)}, icon('pencil'), 'Edit'),
          r.source === 'ai' ? h('button', {class: 'btn sm ghost', type: 'button', onclick: () => keep(r)}, icon('bookmark'), 'Keep') : null,
          h('button', {class: 'btn sm danger', type: 'button', onclick: ask}, icon('trash'), 'Delete')) : null);
    }
    function sugRows() {
      const list = R.suggestions || [];
      if (!list.length) return null;
      const shown = RU.sugAll ? list : list.slice(0, 3);
      return h('div', {class: 'rsugs'}, gh('Suggestions'), shown.map((s) => h('div', {class: 'rrow sug', dataset: {sug: s.key}},
        h('span', {class: 'grow'}, h('span', {class: 'rst', title: s.text}, boldIn(s.text, [s.merchant, s.category]))),
        h('span', {class: 'pairc'}, h('button', {class: 'btn sm', type: 'button', onclick: () => act('/api/rules/suggestions/accept', {key: s.key},
          (x) => saved(['Rule added', refiled(x.refiled)]))}, 'Add rule'),
        h('button', {class: 'btn sm ghost', type: 'button', onclick: () => act('/api/rules/suggestions/dismiss', {key: s.key}, () => saved('Dismissed'))},
          'Dismiss')))),
      list.length > 3 && !RU.sugAll ? h('button', {class: 'lnk rmoresug', type: 'button', onclick: () => { RU.sugAll = true; draw(); }},
        `+${list.length - 3} more`) : null);
    }
    function listPart() {
      const q = RU.q.trim().toLowerCase();
      const hit = (r) => (RU.filter === 'all' || r.source === RU.filter)
        && (!q || `${r.pattern} ${r.label} ${r.category}`.toLowerCase().includes(q));
      const rules = R.rules.filter(hit);
      const mine = rules.filter((r) => r.source === 'user');
      const ai = rules.filter((r) => r.source !== 'user');
      let left = RU.all ? Infinity : RULE_SHOWN;
      const take = (xs) => { const out = xs.slice(0, Math.max(left, 0)); left -= out.length; return out; };
      const a = take(mine);
      const b = take(ai);
      const groups = [];
      if (a.length) groups.push(RU.filter === 'all' ? gh(`Yours · ${mine.length}`) : null, a.map(row));
      if (b.length) groups.push(RU.filter === 'all' ? gh(`AI · ${ai.length}`) : null, b.map(row));
      return h('div', {class: 'rlist'},
        rules.length ? groups : h('p', {class: 'hint rnone', dataset: {empty: ''}}, R.rules.length ? 'No rules match.' : 'No rules yet. Moving a charge on Transactions offers to make one.'),
        rules.length > RULE_SHOWN && !RU.all ? h('button', {class: 'more rall', type: 'button', onclick: () => { RU.all = true; draw(); }},
          `Show all ${rules.length}`) : null);
    }
    const search = h('input', {class: 'search rq', type: 'search', placeholder: 'Search rules', 'aria-label': 'Search rules', autocomplete: 'off',
      spellcheck: 'false'});
    const listSlot = h('div');
    search.addEventListener('input', () => { RU.q = search.value; RU.all = false; fill(listSlot, listPart()); });
    function draw() {
      fill(sumTxt, summary());
      sum.setAttribute('aria-expanded', String(RU.open && Boolean(R)));
      el.classList.toggle('open', RU.open && Boolean(R));
      if (!RU.open || !R) { fill(body); body.hidden = true; return; }
      body.hidden = false;
      search.value = RU.q;
      fill(listSlot, listPart());
      fill(body, sugRows(), h('div', {class: 'rbar'}, search,
        seg([['all', 'All'], ['user', 'Yours'], ['ai', 'AI']], RU.filter, 'Show rules', (k) => { RU.filter = k; RU.all = false; draw(); }, 'rseg')),
      listSlot);
    }
    draw();
    return el;
  }

  // ---- the page ----------------------------------------------------------------------------------------------------
  HF.RENDER.settings = async (tk) => {
    fill(document.getElementById('page'), HF.skeleton(['s1', 's1', 's1', 's1']));
    // TASK-24 loads rules independently; an unavailable rules route does not hide the other settings.
    const [d, , rules, cats] = await Promise.all([api('/api/settings/page'), HF.ensureAccounts().catch(() => null),
      api('/api/rules').catch(() => null), api('/api/categories/page').catch(() => null)]);
    if (tk !== HF.S.token) return;
    D = d;
    if (ACCESS_PENDING && d.access.mode === ACCESS_PENDING) ACCESS_PENDING = null;
    // hotfix 2026-09-29: one column, every card the same width, read top to bottom (user feedback: the packed 2-column
    // masonry left cards of different lengths side by side, hard to read on desktop). Phones were one column already.
    const grid = h('div', {class: 'sgrid'}, connectionsCard(d), accessCard(d), aiCard(d), mcpCard(d), syncCard(d), valuationsCard(d),
      regionCard(d), lookCard(d), dataCard(d), categoriesCard(cats), rulesCard(rules));
    fill(document.getElementById('page'), grid);
  };
});
