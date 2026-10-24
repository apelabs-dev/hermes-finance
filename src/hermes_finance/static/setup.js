/* Hermes Finances: first-run setup (#/setup, TASK-18), as approved in MOCK-017. Shown when there are no Plaid keys and
 * nothing is connected or added yet (the page shell's data-setup).
 *
 *   1  Welcome: the two Plaid keys, and how AI should work (Hermes / an API key / none)
 *   2  Who can open this app? (the three access modes; Built-in login is picked for you when this browser reached the
 *      app at another address than this computer's)
 *   3  Try demo (Plaid Sandbox, fake data) or connect real banks (Production)
 *   4  (real banks only, optional; Skip is picked) Add Alpaca? A read-only key, checked like Settings' Connect Alpaca
 *   5  Connect the first bank (Plaid Link), or add one by hand
 *   6  Suggested budgets from the last 60 days, "Use" per row
 *
 * Every step but the keys can be skipped and done later in Settings. The step is in the address (#/setup?step=3), so
 * setting up the built-in login (which reloads the page, signed in) carries on where it was.
 */
(window.HF_EXT = window.HF_EXT || []).push((HF) => {
  'use strict';
  const {h, fill, icon, api, post, toast, pill, badge, ring, money, money2, plural, RGB, ACC} = HF;
  HF.PAGES.setup = 'Set up';
  HF.BOARDS.add('setup');

  const LOOPBACK = ['127.0.0.1', 'localhost', '[::1]', '::1'];
  const brandRow = () => h('div', {class: 'brand wb'}, h('img', {src: '/static/icons/icon.svg', alt: '', width: '30', height: '30'}), 'Hermes Finances');
  const STEPS = 6;
  const stepper = (k, n = STEPS) => h('div', {class: 'steps', role: 'img', 'aria-label': `Step ${k} of ${n}`},
    Array.from({length: n}, (_, i) => h('i', {class: i < k ? 'on' : ''})), h('span', {'aria-hidden': 'true'}, `Step ${k} of ${n}`));
  const errBox = () => h('p', {class: 'formerr', role: 'alert'});
  const fld = (label, input, hint) => {
    const id = `su-${Math.random().toString(36).slice(2, 8)}`;
    input.id = id;
    return h('div', {class: 'fld'}, h('label', {for: id}, label), h('div', {class: 'inp code'}, input), hint ? h('p', {class: 'hint'}, hint) : null);
  };
  const choice = (on, iconName, rgb, title, text, note, onclick, size = 'l') => h('button', {class: `choice${on ? ' on' : ''}`, type: 'button', role: 'radio',
    'aria-checked': String(on), onclick}, HF.ibadge(iconName, rgb, size), h('span', {class: 'grow'}, h('b', {}, title), h('span', {}, text),
    note ? h('span', {class: 'dim'}, note) : null), h('span', {class: 'rad'}));
  const go = (step) => { location.hash = `#/setup?step=${step}`; };

  function wiz(k, ...kids) {
    fill(document.getElementById('page'), h('div', {class: 'wiz'}, brandRow(), h('div', {class: 'sheet static'}, stepper(k), kids)));
    const first = document.querySelector('#page input, #page .choice.on, #page .btn.w');
    if (first) first.focus();
  }
  const row = (...btns) => h('div', {class: 'row'}, btns);
  const btn = (text, onclick, cls = 'btn w', extra = {}) => h('button', {class: cls, type: 'button', onclick, ...extra}, text);

  // 1: keys + AI
  function stepKeys(st) {
    const id = h('input', {type: 'text', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', placeholder: st.plaid.set ? 'Set (paste to replace)' : 'Client ID'});
    const secret = h('input', {type: 'password', autocomplete: 'off', spellcheck: 'false', placeholder: st.plaid.set ? 'Set (paste to replace)' : 'Secret'});
    let ai = st.hermes ? 'hermes' : 'off';
    const aiBox = h('div', {class: 'choices sm', role: 'radiogroup', 'aria-label': 'How should AI work?'});
    const drawAi = () => fill(aiBox,
      choice(ai === 'hermes', 'sparkles', RGB.purple, st.hermes ? 'Hermes (detected)' : 'Hermes', st.hermes ? 'Found on this computer. Uses your Hermes model; nothing to paste.'
        : "Not found on this computer yet; install Hermes, or pick another way.", null, () => { ai = 'hermes'; drawAi(); }, 's'),
      choice(ai === 'api', 'key', RGB.acc, 'Paste an API key', 'OpenRouter, OpenAI or a local model (Ollama, LM Studio).', null, () => { ai = 'api'; drawAi(); }, 's'),
      choice(ai === 'off', 'x', RGB.grey, 'No AI for now', 'The dashboard works without it: no chat, no auto-classify.', null, () => { ai = 'off'; drawAi(); }, 's'));
    drawAi();
    const err = errBox();
    const next = btn('Continue', async () => {
      err.textContent = '';
      next.disabled = true;
      try {
        if (id.value.trim() || secret.value.trim()) {
          await post('/api/setup/plaid', {client_id: id.value, secret: secret.value, demo: false});
        } else if (!st.plaid.set) {
          throw new Error('Paste your Plaid client ID and secret (or skip: you can still add accounts by hand).');
        }
        await post('/api/prefs', {'ai.mode': ai});
        go(2);
      } catch (e) {
        err.textContent = e.message;
      } finally {
        next.disabled = false;
      }
    });
    wiz(1, h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Welcome')),
      h('p', {}, 'Your bank data stays on this computer. To start, paste the two keys from your Plaid account.'),
      fld('Plaid client ID', id), fld('Plaid secret', secret, `Both are at dashboard.plaid.com → Developers → Keys. They are saved in ${st.keys_path} on this computer, never sent anywhere else.`),
      h('div', {class: 'gh'}, 'How should AI work?', h('span', {class: 'ln'})), aiBox,
      h('p', {class: 'hint'}, 'You can change this later in Settings → AI provider.'), err,
      row(next, btn('Skip for now', () => post('/api/prefs', {'ai.mode': ai}).catch(() => null).then(() => go(2)), 'btn ghost')));
  }

  // 2: who can open the app
  function stepAccess(st) {
    const elsewhere = st.exposed || !LOOPBACK.includes(location.hostname);
    let mode = st.login_ready ? 'builtin' : st.access !== 'local' ? st.access : elsewhere ? 'builtin' : 'local';
    const box = h('div');
    const idIn = h('input', {'aria-label': 'Allowed identity', spellcheck: 'false', autocapitalize: 'off', placeholder: 'you@example.com'});
    const extra = h('div');
    const draw = () => {
      fill(box, HF.accessChoices(mode, (k) => { mode = k; draw(); }));
      fill(extra, mode === 'proxy' ? h('div', {class: 'fld'}, h('label', {}, 'The login your proxy signs you in with'), h('div', {class: 'inp code'}, idIn),
        h('p', {class: 'hint'}, 'Add the proxy’s public address in Settings → Access & security, or start the app with --public-url.')) : null);
    };
    draw();
    const err = errBox();
    const next = btn('Continue', async () => {
      err.textContent = '';
      try {
        if (mode === 'builtin') {
          if (st.login_ready) {
            go(3);
            return;
          }
          HF.loginSetup(() => { location.hash = '#/setup?step=3'; location.reload(); });
          return;
        }
        if (mode === 'proxy') await post('/api/access/identities', {add: idIn.value});
        await post('/api/access', {mode});
        go(3);
      } catch (e) {
        err.textContent = e.message;
      }
    });
    wiz(2, h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Who can open this app?')),
      h('p', {}, elsewhere ? `This browser reached the app at ${location.host}, not this computer only.` : `Found: the app answers on ${st.bind}, this computer only.`),
      box, extra,
      h('p', {class: 'hint'}, 'When the app is reachable at another address, Built-in login is picked for you. You can change this in Settings → Access & security.'),
      err, row(next, btn('Back', () => go(1), 'btn ghost')));
  }

  // 3: demo or real banks
  function stepStart(st) {
    let demo = st.demo;
    const box = h('div', {class: 'choices', role: 'radiogroup', 'aria-label': 'How to start'});
    const draw = () => fill(box,
      choice(demo, 'sparkles', RGB.purple, 'Try demo', 'Sandbox, fake data. A sample bank with made-up transactions, to look around.', 'Nothing real is connected.',
        () => { demo = true; draw(); }),
      choice(!demo, 'landmark', RGB.ok, 'Connect real banks', 'Production. Link your own banks with Plaid.', `Each bank uses 1 of your ${st.slot_limit} Plaid connections.`,
        () => { demo = false; draw(); }));
    draw();
    const err = errBox();
    const next = btn('Continue', async () => {
      err.textContent = '';
      try {
        if (st.plaid.set) await post('/api/setup/plaid', {demo});
        go(demo ? 5 : 4);
      } catch (e) {
        err.textContent = e.message;
      }
    });
    wiz(3, h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'How do you want to start?')),
      h('p', {}, st.plaid.set ? 'You can switch later in Settings.' : 'Without Plaid keys you can still add accounts by hand; paste the keys in Settings any time.'),
      box, err, row(next, btn('Back', () => go(2), 'btn ghost')));
  }

  // 4: (real banks) Alpaca, optional; Skip is picked. A key goes through the same check as Settings -> Connections ->
  // Connect Alpaca (POST /api/alpaca/key -> alpaca.py: one read-only GET /v2/account on the live host, accepted only for an
  // ACTIVE live account, a paper key refused, nothing saved on a failure), is saved in its own 0600 file and is never
  // shown again.
  function stepAlpaca(st) {
    const done = st.alpaca && st.alpaca.set;
    let add = false;
    const f = HF.alpacaForm('A read-only key from alpaca.markets \u2192 your account \u2192 API keys. It stays on this computer.');
    const box = h('div', {class: 'choices', role: 'radiogroup', 'aria-label': 'Add Alpaca?'});
    const form = h('div', {class: 'alpf'});
    const result = h('div', {class: 'alpres', 'aria-live': 'polite'});
    const err = errBox();
    const next = btn('Continue', async () => {
      err.textContent = '';
      if (!add) { go(5); return; }
      if (!f.filled()) { err.textContent = 'Paste the key ID and secret key, or pick Skip.'; return; }
      next.disabled = true;
      fill(next, 'Checking with Alpaca\u2026');
      try {
        const r = await f.send();
        add = false;   // connected: the form (and the secret in it) goes; Continue moves on
        fill(box, null);
        fill(form, null);
        fill(result, h('div', {class: 'matchbox'}, pill('sm', 'checkc', 'Connected'), h('span', {},
          `Equity ${money2(r.equity)} \u00b7 ${r.positions == null ? 'positions at the next sync' : plural(r.positions, 'position')}`)));
        HF.loadAccounts().catch(() => {});
      } catch (e) {
        err.textContent = e.message;   // nothing was saved
      } finally {
        next.disabled = false;
        fill(next, add ? 'Check and add' : 'Continue');
      }
    });
    const draw = () => {
      fill(box, choice(!add, 'x', RGB.grey, 'Skip', 'No Alpaca account, or add it later in Settings \u2192 Connections.', null, () => { add = false; draw(); }, 's'),
        choice(add, 'key', RGB.acc, 'Add my Alpaca key', 'Balance and positions, read once a day. It never trades.', null, () => { add = true; draw(); }, 's'));
      fill(form, add ? f.fields : null);
      fill(next, add ? 'Check and add' : 'Continue');
    };
    if (done) fill(result, h('div', {class: 'matchbox'}, pill('sm', 'checkc', 'Connected'), h('span', {}, 'Alpaca is connected. Replace or remove it in Settings.')));
    else draw();
    wiz(4, h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Add Alpaca?')),
      h('p', {}, 'If you invest with Alpaca, the app can read its balance and positions. It never trades. You can do this later in Settings.'),
      box, form, result, err, row(next, btn('Back', () => go(3), 'btn ghost')));
  }

  // 5: connect the first bank
  function stepConnect(st) {
    const left = st.slot_limit - st.slots_used;
    const connect = h('button', {class: 'btn w', type: 'button', disabled: !st.plaid.set || left <= 0, onclick: () => HF.connectBank()}, icon('link'), 'Connect a bank');
    wiz(5, h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Connect your first bank')),
      h('p', {}, 'Plaid opens in a secure window. Your bank login never touches Hermes.'),
      h('div', {class: 'prev'}, ring(st.slots_used / st.slot_limit, ACC, 62, 7, h('b', {class: 'v', style: {'font-size': '15px'}}, String(st.slots_used))),
        h('div', {}, h('b', {}, `${left} of ${st.slot_limit} connections left`),
          h('div', {class: 'mut', style: {'font-size': '14px'}}, "Each bank you link uses one. Removing a bank doesn't free its spot."))),
      st.demo ? h('div', {class: 'matchbox none'}, HF.ibadge('info', RGB.acc, 'xs'), h('span', {}, 'Demo: pick any bank. Sign in as ', h('span', {class: 'mono'}, 'user_good'),
        ', password ', h('span', {class: 'mono'}, 'pass_good'), '.')) : null,
      st.banks ? h('p', {class: 'nc'}, `${plural(st.banks, 'bank')} connected.`) : null,
      row(connect, btn('Add one by hand', () => HF.openAdd('manual'), 'btn ghost')),
      h('div', {class: 'row'}, btn(st.banks ? 'Continue' : 'Skip for now', () => go(6), 'btn ghost w')));
  }

  // 6: suggested budgets
  async function stepCaps(tk) {
    const s = await api('/api/setup/suggestions');
    if (tk !== HF.S.token) return;
    const rows = s.categories;
    const pending = new Set(rows.filter((c) => c.cap == null).map((c) => c.category));
    const list = h('div', {class: 'capl'});
    const finish = async () => {
      try {
        await post('/api/setup/done', {});
      } catch (e) { /* the set-up just shows again next time */ }
      document.body.removeAttribute('data-setup');
      location.hash = '#/overview';
      HF.loadAccounts().catch(() => {});
    };
    const useOne = async (c) => {
      await post('/api/budgets/cap', {category: c.category, monthly_cap: c.suggested});
      c.cap = c.suggested;
      pending.delete(c.category);
    };
    const actions = h('div', {class: 'row'});
    const useAll = async (e) => {
      e.currentTarget.disabled = true;
      try {
        for (const c of rows.filter((x) => pending.has(x.category))) await useOne(c);
      } catch (err) {
        toast(`Not saved: ${err.message}`, [], {error: true, timeout: 5000});
      }
      draw();
    };
    const draw = () => {
      fill(list, rows.map((c) => h('div', {class: 'li'}, badge(c.category, 's'), h('span', {class: 'grow'}, h('b', {}, c.category),
        h('span', {}, `Spent ${money2(c.spent)} in ${s.days} days`)),
      h('span', {class: 'amt v'}, money(c.cap != null ? c.cap : c.suggested), h('span', {class: 'dim', style: {'font-weight': '500'}}, ' /mo')),
      c.cap != null ? pill('sm', 'check', 'Set') : h('button', {class: 'btn sm ghost', type: 'button', onclick: async (e) => {
        e.currentTarget.disabled = true;
        try {
          await useOne(c);
        } catch (err) {
          toast(`Not saved: ${err.message}`, [], {error: true, timeout: 5000});
        }
        draw();
      }}, 'Use'))));
      fill(actions, pending.size ? btn(`Use the other ${pending.size}`, useAll) : btn('Done', finish),
        btn(pending.size ? 'Skip for now' : 'Back', pending.size ? finish : () => go(5), 'btn ghost'));
    };
    const bank = s.banks.length === 1 ? ` at ${s.banks[0]}` : '';
    wiz(6, h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Suggested budgets')),
      rows.length ? h('p', {}, `From your last ${s.days} days${bank}, rounded up. Nothing is set until you tap Use.`)
        : h('p', {}, `No spending in the last ${s.days} days yet. Suggestions show up here and on Budgets once your bank's history is in.`),
      list, actions);
    draw();
  }

  HF.RENDER.setup = async (tk) => {
    const st = await api('/api/setup');
    if (tk !== HF.S.token) return;
    const step = Math.min(STEPS, Math.max(1, Number(HF.S.params.get('step')) || 1));
    if (step === 1) stepKeys(st);
    else if (step === 2) stepAccess(st);
    else if (step === 3) stepStart(st);
    else if (step === 4) stepAlpaca(st);
    else if (step === 5) stepConnect(st);
    else await stepCaps(tk);
    document.title = `Set up \u00b7 step ${step} of ${STEPS} \u00b7 Hermes Finances`;
  };
});
