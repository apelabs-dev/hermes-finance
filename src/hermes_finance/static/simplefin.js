/* SimpleFIN Bridge (v21): the chooser's second way, the setup token, the confirm sheet, and the Settings row's actions.
 * The token is pasted once and sent once; the server trades it for the access link, which it keeps in secrets.env and
 * never sends back. Account types are guesses the owner confirms (every one) before the first sync. */
(window.HF_EXT = window.HF_EXT || []).push((X) => {
  'use strict';
  const {h, plural, tag, post, api, HOOKS, openSheet, closeModal, closeBtn, backBtn, toast, modal, openAdd, help} = X;
  // After a write the sidebar badges change too (a removed or newly added account moves the Transactions count).
  const refresh = () => { X.loadCounts(); X.refresh(); };
  const TYPES = [['checking', 'Checking'], ['savings', 'Savings'], ['credit', 'Credit card'], ['loan', 'Loan'],
    ['investment', 'Investment']];
  const LABEL = Object.fromEntries(TYPES);
  const VENDOR = 'ins_hf_simplefin';
  const fmt = (d) => (d ? new Date(`${d}T12:00:00`).toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) : '');

  const guessKey = (g) => {
    if (!g) return 'checking';
    if (g.type === 'depository') return g.subtype === 'savings' ? 'savings' : 'checking';
    if (g.type === 'credit') return 'credit';
    if (g.type === 'loan') return 'loan';
    if (g.type === 'investment') return 'investment';
    return 'checking';
  };

  function select(options, value, label) {
    const s = h('select', {class: 'selv', 'aria-label': label},
      options.map(([v, text]) => h('option', {value: v}, text)));
    s.value = value;
    return s;
  }

  // ---- the setup token -------------------------------------------------------------------------------------------
  function openToken(onBack) {
    const back = onBack || (() => closeModal());
    const input = h('input', {type: 'text', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off',
      placeholder: 'aHR0cHM6Ly9icmlkZ2Uuc2ltcGxlZmluLm9yZy9zaW1wbGVmaW4vY2xhaW0v…'});
    const error = h('p', {class: 'formerr', role: 'alert'});
    const connect = h('button', {class: 'btn w', type: 'button'}, 'Connect');
    const retry = h('button', {class: 'btn ghost', type: 'button', hidden: true}, 'Try again');
    retry.addEventListener('click', async () => {
      retry.disabled = true;
      error.textContent = '';
      try {
        const got = await post('/api/simplefin/claim', {});
        openConfirm(got, onBack);
      } catch (e) {
        error.textContent = e.message;
        retry.disabled = false;
      }
    });
    const submit = async () => {
      const token = input.value.trim();
      if (!token) {
        error.textContent = 'Paste the setup token from SimpleFIN Bridge.';
        return;
      }
      connect.disabled = true;
      error.textContent = '';
      try {
        const got = await post('/api/simplefin/claim', {token});
        openConfirm(got, onBack);
      } catch (e) {
        error.textContent = e.message;
        connect.disabled = false;
        try {
          const st = await api('/api/simplefin/status');
          retry.hidden = !st.needs_retry;
        } catch (ignored) {
          retry.hidden = true;
        }
      }
    };
    connect.addEventListener('click', submit);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
    const helpText = 'Create a setup token at SimpleFIN Bridge and paste it here. A token works once: the app trades it '
      + 'for a private access link and keeps that in secrets.env, next to your Plaid keys. SimpleFIN is read-only: it '
      + 'reads balances and transactions, never your bank password.';
    openSheet([
      h('div', {class: 'shd'}, backBtn(back), h('h2', {id: 'sheet-title'}, 'Connect with SimpleFIN'), closeBtn()),
      h('p', {}, 'Create a setup token at SimpleFIN Bridge and paste it here. A token works once: the app trades it for a '
        + 'private access link and keeps that in secrets.env, next to your Plaid keys.'),
      h('div', {class: 'fld'}, h('label', {}, 'Setup token'), h('div', {class: 'inp code'}, input)),
      error,
      h('div', {class: 'row'}, connect, retry,
        h('button', {class: 'btn ghost', type: 'button', onclick: back}, 'Back')),
      h('div', {class: 'sub help-line'}, help('Where to get a token, and what SimpleFIN can read',
        () => h('div', {}, h('p', {}, 'At SimpleFIN Bridge, create a setup token and paste it here. The token works once; '
          + 'the app keeps the private access link it returns in secrets.env on this computer, never in the database.'),
          h('p', {}, 'SimpleFIN can read balances and transactions, including pending ones. Holdings and loan details '
            + 'are not imported. The app is read-only through Bridge.')),
        {long: true, text: 'Where to get a token, and what SimpleFIN can read'}))]);
  }

  // ---- confirm what it found -------------------------------------------------------------------------------------
  function openConfirm(data, onBack) {
    const back = onBack || (() => closeModal());
    const rows = [];
    const groups = new Map();
    for (const a of data.accounts || []) {
      const bank = a.connection || 'SimpleFIN';
      if (!groups.has(bank)) groups.set(bank, []);
      const guess = a.guess || {};
      const dup = a.plaid;
      const tags = [];
      if (dup) tags.push(tag('On Plaid', 'sf', 'This looks like an account already synced through Plaid'));
      else if (guess.confidence === 'low') tags.push(tag('Check type', 'warn', 'The app guessed this type: check it before adding'));
      const kind = select(TYPES.map(([k, t]) => [k, `Add as ${t}`]).concat([['skip', "Don't add"]]),
        guessKey(guess), `Type for ${a.name || 'this account'}`);
      const dupKind = select([['skip', 'Skip'], ['second', 'Add as a second account']], 'skip',
        `What to do with ${a.name || 'this account'}`);
      // A Plaid twin starts skipped and reveals the type only for "Add as a second account"; every other account
      // shows its type select on the row, preselected to the guess, so the owner can fix it or choose Don't add.
      const second = dup ? h('span', {class: 'sf-second'}, kind) : null;
      if (second) second.hidden = true;
      const choice = () => {
        if (dup && dupKind.value !== 'second') return {add: false};
        return {key: a.key, type: kind.value === 'skip' ? undefined : kind.value, add: kind.value !== 'skip'};
      };
      if (dup) {
        dupKind.addEventListener('change', () => {
          second.hidden = dupKind.value !== 'second';
          if (dupKind.value === 'second') kind.value = guessKey(guess);
        });
      }
      const amount = a.balance == null ? '' : ` · ${X.money2(a.balance)}`;
      const row = h('div', {class: `li sf-acct${dup ? ' dup' : ''}`},
        h('span', {class: 'grow'}, h('b', {class: 'nmrow'}, h('span', {class: 'nm'}, a.name || 'Account'), tags),
          h('span', {class: 'meta2'}, h('span', {class: 'chip2'}, a.mask ? `••${a.mask}` : bank),
            dup ? ` · already synced through Plaid${amount}` : ` · looks like ${LABEL[guessKey(guess)].toLowerCase()}${amount}`)),
        dup ? dupKind : kind, second);
      rows.push({account: a, choice});
      groups.get(bank).push(row);
    }
    const count = () => rows.filter((r) => r.choice().add).length;
    const add = h('button', {class: 'btn w', type: 'button'}, 'Add accounts');
    const error = h('p', {class: 'formerr', role: 'alert'});
    const drawCount = () => {
      const n = count();
      add.textContent = `Add ${n} account${n === 1 ? '' : 's'}`;
      add.disabled = n === 0;
    };
    const body = h('div', {});
    for (const [bank, list] of groups) {
      body.append(h('div', {class: 'gh'}, bank, h('span', {class: 'ln'})));
      list.forEach((el) => body.append(el));
    }
    body.addEventListener('change', drawCount);
    add.addEventListener('click', async () => {
      const accounts = rows.map((r) => r.choice()).filter(Boolean);
      add.disabled = true;
      error.textContent = '';
      try {
        const got = await post('/api/simplefin/confirm', {accounts});
        closeModal();
        const review = got.duplicate_reviews ? ` ${plural(got.duplicate_reviews, 'account')} to review.` : '';
        toast(`SimpleFIN connected.${review}`, [], {timeout: 4000});
        refresh();
      } catch (e) {
        error.textContent = e.message;
        add.disabled = false;
      }
    });
    drawCount();
    openSheet([
      h('div', {class: 'shd'}, backBtn(back), h('h2', {id: 'sheet-title'}, 'Confirm what it found'), closeBtn()),
      h('div', {class: 'filec'}, X.ia('SimpleFIN', 's', null, VENDOR),
        h('span', {class: 'grow'}, h('b', {}, 'SimpleFIN Bridge'),
          h('span', {}, `${plural(data.counts ? data.counts.banks : 0, 'bank')} · ${plural(data.counts ? data.counts.accounts : 0, 'account')} found`))),
      body, error,
      h('div', {class: 'row'}, add, h('button', {class: 'btn ghost', type: 'button', onclick: back}, 'Back'))],
    {cls: 'wide'});
  }

  // ---- the Settings row's actions --------------------------------------------------------------------------------
  async function resume() {
    try {
      const got = await api('/api/simplefin/confirm');
      openConfirm(got);
    } catch (e) {
      toast(e.message, [], {error: true, timeout: 5000});
    }
  }

  function remove() {
    modal('Remove SimpleFIN?', h('div', {}, h('p', {}, 'This removes the connection on this computer only: it does not '
      + 'revoke it at SimpleFIN Bridge, so remove the app there too. Your accounts and transactions stay, and a new setup '
      + 'token reconnects them.')), [{label: 'Remove', primary: true, onclick: async () => {
      closeModal();
      try {
        await post('/api/simplefin/disconnect', {});
        toast('SimpleFIN removed on this computer.', [], {timeout: 4000});
        refresh();
      } catch (e) {
        toast(`Not removed: ${e.message}`, [], {error: true, timeout: 5000});
      }
    }}, {label: 'Keep it', onclick: closeModal}]);
  }

  function openDuplicates(duplicates) {
    const pick = (pair, choice, text) => h('button', {class: 'btn sm ghost', type: 'button', onclick: async () => {
      try {
        await post('/api/simplefin/duplicate', {simplefin_account_id: pair.simplefin, plaid_account_id: pair.plaid,
          choice});
        toast('Saved. The account policy now follows your pick.', [], {timeout: 3000});
        closeModal();
        refresh();
      } catch (e) {
        toast(`Not saved: ${e.message}`, [], {error: true, timeout: 5000});
      }
    }}, text);
    const row = (pair) => h('div', {class: 'li sf-dup'},
      h('span', {class: 'grow'}, h('b', {}, pair.simplefin_name || 'SimpleFIN account'),
        h('span', {}, `${pair.plaid_name || 'Plaid account'}${pair.mask ? ` · ••${pair.mask}` : ''}`)),
      h('span', {class: 'row'}, pick(pair, 'plaid', 'Use Plaid'), pick(pair, 'simplefin', 'Use SimpleFIN'),
        pick(pair, 'not_duplicate', 'Not a duplicate')));
    openSheet([h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Accounts on both'), closeBtn()),
      h('p', {}, 'These look like the same account through Plaid and SimpleFIN. Choose which one counts. Until you do, '
        + 'only the Plaid copy is in your totals.'),
      (duplicates || []).map(row)], {cls: 'wide'});
  }

  async function tryAgain() {
    try {
      const got = await post('/api/simplefin/claim', {});
      openConfirm(got);
    } catch (e) {
      toast(`Couldn't reach SimpleFIN: ${e.message}`, [], {error: true, timeout: 6000});
    }
  }

  async function syncNow() {
    try {
      await post('/api/simplefin/sync', {});
      toast('Syncing SimpleFIN…', [], {timeout: 2200});
      setTimeout(() => refresh(), 1800);
    } catch (e) {
      toast(`Couldn't sync SimpleFIN: ${e.message}`, [], {error: true, timeout: 6000});
    }
  }

  HOOKS.addSteps.simplefin = () => openToken(() => openAdd('choose'));
  X.simplefin = {openToken, openConfirm, resume, remove, tryAgain, sync: syncNow, openDuplicates, fmt};
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-sfsync]');
    if (b) {
      e.preventDefault();
      syncNow();
    }
    const fix = e.target.closest('[data-fixbridge]');
    if (fix) {
      e.preventDefault();
      window.open('https://bridge.simplefin.org/', '_blank', 'noopener');
    }
  });
});
