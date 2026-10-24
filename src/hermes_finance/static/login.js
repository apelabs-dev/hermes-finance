/* Hermes Finances: the built-in login's screens (TASK-18).
 *
 *   window.HFLogin.enroll(root, {onDone})  the set-up, three steps: a username and password (with a strength meter),
 *                                          then the authenticator (a QR code drawn by the server, as SVG, and a code
 *                                          to confirm it), then the 8 recovery codes ("I saved them" unlocks Continue).
 *                                          Used by Settings > Access & security and by first-run setup.
 *   the sign-in page                       on the page the server shows signed-out browsers (<body class="signin">):
 *                                          username + password, then the 6-digit code or a recovery code; while no
 *                                          login exists yet (on this computer only), the set-up instead.
 *
 * Built with createElement + textContent like app.js: nothing typed or received ever becomes markup. The QR code is an
 * <img> of the server's SVG (a data: URL); the key never goes to a third-party service.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style') for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false || kid === '') continue;
      el.append(kid instanceof Node ? kid : String(kid));
    }
    return el;
  }
  const fill = (el, ...kids) => { el.replaceChildren(); kids.flat(Infinity).forEach((k) => { if (k != null && k !== false) el.append(k); }); return el; };
  function icon(name) {
    const tpl = $('icons');
    const svg = tpl && tpl.content.querySelector(`[data-icon="${name}"]`);
    return svg ? svg.cloneNode(true) : h('span');
  }

  async function call(url, body) {
    const headers = {'Content-Type': 'application/json'};
    const meta = document.querySelector('meta[name=csrf-token]');
    if (meta && meta.content) headers['X-CSRF-Token'] = meta.content;
    const r = await fetch(url, {method: 'POST', headers, body: JSON.stringify(body || {}), credentials: 'same-origin'});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(j.error || `${r.status} ${r.statusText}`);
      e.field = j.field;
      e.status = r.status;
      throw e;
    }
    return j;
  }

  // ---- pieces --------------------------------------------------------------------------------------------------
  const brand = () => h('div', {class: 'brand wb'}, h('img', {src: '/static/icons/icon.svg', alt: '', width: '30', height: '30'}),
    'Hermes Finances');
  const stepper = (k, n) => h('div', {class: 'steps', role: 'img', 'aria-label': `Step ${k} of ${n}`},
    Array.from({length: n}, (_, i) => h('i', {class: i < k ? 'on' : ''})), h('span', {'aria-hidden': 'true'}, `Step ${k} of ${n}`));
  const errBox = () => h('p', {class: 'formerr', role: 'alert'});
  function field(label, input, hint) {
    const id = `lf-${Math.random().toString(36).slice(2, 8)}`;
    input.id = id;
    return h('div', {class: 'fld'}, h('label', {for: id}, label), h('div', {class: 'inp'}, input), hint ? h('p', {class: 'hint'}, hint) : null);
  }

  // A password's strength, 0-4, from its length and the kinds of characters in it (a long phrase scores well; a
  // repeated or sequential run, or a very common password, doesn't). The server only insists on 12 characters.
  const COMMON = /^(password|passw0rd|qwerty|letmein|welcome|admin|iloveyou|monkey|dragon|123456|abc123)/i;
  function strength(pw) {
    const n = [...pw].length;
    if (n < 12) return {score: 0, label: n ? 'Too short' : ''};
    let pool = 0;
    if (/[a-z]/.test(pw)) pool += 26;
    if (/[A-Z]/.test(pw)) pool += 26;
    if (/[0-9]/.test(pw)) pool += 10;
    if (/[^A-Za-z0-9]/.test(pw)) pool += 33;
    let bits = n * Math.log2(Math.max(pool, 2));
    if (/(.)\1{3,}/.test(pw)) bits -= 20;
    if (/(0123|1234|2345|3456|4567|5678|6789|abcd|bcde|cdef|qwer|asdf)/i.test(pw)) bits -= 15;
    if (COMMON.test(pw)) bits -= 40;
    if (new Set(pw).size < 5) bits -= 30;
    const score = bits < 50 ? 1 : bits < 64 ? 2 : bits < 80 ? 3 : 4;
    return {score, label: ['Too short', 'Weak', 'Fair', 'Good', 'Strong'][score]};
  }

  // Six boxes for a 6-digit code: a digit moves on, Backspace moves back, a pasted code fills them all.
  function codeBoxes(onFull) {
    const boxes = Array.from({length: 6}, (_, i) => h('input', {type: 'text', inputmode: 'numeric', maxlength: '1', autocomplete: i ? 'off' : 'one-time-code',
      'aria-label': `Digit ${i + 1} of 6`, pattern: '[0-9]*'}));
    const value = () => boxes.map((b) => b.value).join('');
    boxes.forEach((b, i) => {
      b.addEventListener('input', () => {
        const digits = b.value.replace(/\D/g, '');
        if (digits.length > 1) {  // a paste or an autofill of the whole code
          digits.slice(0, 6 - i).split('').forEach((d, k) => { boxes[i + k].value = d; });
          (boxes[Math.min(i + digits.length, 5)]).focus();
        } else {
          b.value = digits;
          if (digits && i < 5) boxes[i + 1].focus();
        }
        if (value().length === 6 && onFull) onFull(value());
      });
      b.addEventListener('keydown', (e) => {
        if (e.key === 'Backspace' && !b.value && i > 0) boxes[i - 1].focus();
        if (e.key === 'Enter' && onFull && value().length === 6) onFull(value());
      });
    });
    const el = h('div', {class: 'codeb', role: 'group', 'aria-label': '6‑digit code'}, boxes);
    el.value = value;
    el.clear = () => { boxes.forEach((b) => { b.value = ''; }); boxes[0].focus(); };
    el.focusFirst = () => boxes[0].focus();
    return el;
  }

  function downloadText(name, text) {
    const a = h('a', {href: URL.createObjectURL(new Blob([text], {type: 'text/plain'})), download: name});
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      if (btn) { const was = btn.lastChild.textContent; btn.lastChild.textContent = 'Copied'; setTimeout(() => { btn.lastChild.textContent = was; }, 1500); }
    } catch (e) {
      if (btn) btn.lastChild.textContent = 'Select and copy';
    }
  }

  // ---- the set-up ----------------------------------------------------------------------------------------------
  function enroll(root, opts = {}) {
    const st = {id: null, key: null, codes: null};
    const wiz = (k, ...kids) => fill(root, h('div', {class: 'wiz'}, opts.brand ? brand() : null, h('div', {class: 'sheet static'}, stepper(k, 3), kids)));

    function stepPassword() {
      const user = h('input', {type: 'text', autocomplete: 'username', spellcheck: 'false', autocapitalize: 'off', value: opts.username || '',
        placeholder: 'e.g. alex', maxlength: '64'});
      const pw = h('input', {type: 'password', autocomplete: 'new-password', maxlength: '256'});
      const again = h('input', {type: 'password', autocomplete: 'new-password', maxlength: '256'});
      const segs = h('span', {class: 'segs', 'aria-hidden': 'true'}, [0, 1, 2, 3].map(() => h('i')));
      const lbl = h('b');
      const len = h('span');
      const meter = h('div', {class: 'pws', role: 'meter', 'aria-label': 'Password strength', 'aria-valuemin': '0', 'aria-valuemax': '4', 'aria-valuenow': '0'},
        segs, h('span', {}, lbl, len));
      const err = errBox();
      const go = h('button', {class: 'btn w', type: 'submit'}, 'Continue');
      const draw = () => {
        const s = strength(pw.value);
        [...segs.children].forEach((i, k) => { i.className = k < s.score ? (s.score <= 1 ? 'on weak' : s.score === 2 ? 'on fair' : 'on') : ''; });
        lbl.textContent = s.label;
        lbl.className = s.score >= 3 ? 'pos' : s.score === 2 ? 'warn' : 'bad';
        len.textContent = pw.value ? `${lbl.textContent ? ' · ' : ''}${[...pw.value].length} characters` : '';
        meter.setAttribute('aria-valuenow', String(s.score));
      };
      pw.addEventListener('input', draw);
      const form = h('form', {novalidate: true, onsubmit: async (e) => {
        e.preventDefault();
        err.textContent = '';
        if (pw.value !== again.value) { err.textContent = 'The two passwords are different.'; again.focus(); return; }
        go.disabled = true;
        try {
          const r = await call('/api/auth/enroll/start', {username: user.value, password: pw.value, confirm: again.value});
          Object.assign(st, {id: r.enroll_id, key: r.key, username: r.username});
          stepQr();
        } catch (ex) {
          err.textContent = ex.message;
          (ex.field === 'username' ? user : pw).focus();
        } finally {
          go.disabled = false;
        }
      }},
      h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Create a password')),
      h('p', {}, 'For signing in from other devices. At least 12 characters; a long phrase works well.'),
      field('Username', user), field('Password', pw), meter, field('Same password again', again), err,
      h('div', {class: 'row'}, go));
      wiz(1, form);
      draw();
      (user.value ? pw : user).focus();
    }

    async function stepQr() {
      const img = h('img', {class: 'qr', alt: 'QR code for your authenticator app', width: '144', height: '144'});
      const boxes = codeBoxes(() => verify());
      const err = errBox();
      const go = h('button', {class: 'btn w', type: 'button', onclick: () => verify()}, 'Verify');
      async function verify() {
        if (go.disabled) return;
        err.textContent = '';
        if (boxes.value().length !== 6) { err.textContent = 'Enter all 6 digits.'; boxes.focusFirst(); return; }
        go.disabled = true;
        try {
          const r = await call('/api/auth/enroll/verify', {enroll_id: st.id, code: boxes.value()});
          st.codes = r.recovery_codes;
          stepCodes();
        } catch (ex) {
          err.textContent = ex.message;
          boxes.clear();
          if (ex.status === 410) stepPassword();
        } finally {
          go.disabled = false;
        }
      }
      wiz(2, h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Scan the QR code')),
        h('p', {}, 'With your authenticator app: Google Authenticator, 1Password, Authy or any app that shows 6‑digit codes.'),
        h('div', {class: 'qrrow'}, img, h('div', {}, h('span', {class: 'mut'}, "Can't scan? Enter this key"), h('b', {class: 'mono qk'}, st.key))),
        h('div', {class: 'fld'}, h('span', {class: 'lbl'}, 'Then enter the 6‑digit code it shows'), boxes), err,
        h('div', {class: 'row'}, go, h('button', {class: 'btn ghost', type: 'button', onclick: stepPassword}, 'Back')));
      boxes.focusFirst();
      try {
        const r = await call('/api/auth/enroll/qr', {enroll_id: st.id});
        img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(r.svg)}`;
      } catch (ex) {
        err.textContent = `The QR code didn't load (${ex.message}); type the key into your app instead.`;
      }
    }

    function stepCodes() {
      const text = `Hermes Finances recovery codes (each works once)\n\n${st.codes.join('\n')}\n`;
      const tick = h('input', {type: 'checkbox'});
      const go = h('button', {class: 'btn w', type: 'button', disabled: true}, 'Continue');
      const err = errBox();
      tick.addEventListener('change', () => { go.disabled = !tick.checked; });
      go.addEventListener('click', async () => {
        go.disabled = true;
        err.textContent = '';
        try {
          await call('/api/auth/enroll/finish', {enroll_id: st.id, saved: true});
          if (opts.onDone) opts.onDone();
        } catch (ex) {
          err.textContent = ex.message;
          go.disabled = !tick.checked;
        }
      });
      const copy = h('button', {class: 'btn sm ghost', type: 'button', onclick: (e) => copyText(text, e.currentTarget)}, icon('copy'), 'Copy');
      wiz(3, h('div', {class: 'shd'}, h('h2', {id: 'sheet-title'}, 'Save your recovery codes')),
        h('p', {}, "Each code works once, if you lose your phone. They won't be shown again."),
        h('div', {class: 'rcodes mono', 'aria-label': 'Recovery codes'}, st.codes.map((c) => h('span', {}, c))),
        h('div', {class: 'row'}, h('button', {class: 'btn sm ghost', type: 'button', onclick: () => downloadText('hermes-finance-recovery-codes.txt', text)},
          icon('download'), 'Download'), copy),
        h('label', {class: 'chk'}, tick, h('span', {}, 'I saved them')), err,
        h('div', {class: 'row'}, go),
        h('p', {class: 'hint'}, 'Continue unlocks once “I saved them” is ticked.'));
      tick.focus();
    }

    stepPassword();
  }

  // ---- the sign-in page ------------------------------------------------------------------------------------------
  function signIn(root) {
    const plain = document.body.hasAttribute('data-plain-http');
    const lock = h('p', {class: 'hint'}, '5 wrong tries = 15 min lock.');
    const card = (...kids) => fill(root, h('div', {class: 'sheet static login'}, brand(), kids));

    function password() {
      const user = h('input', {type: 'text', autocomplete: 'username', spellcheck: 'false', autocapitalize: 'off', maxlength: '64'});
      const pw = h('input', {type: 'password', autocomplete: 'current-password', maxlength: '256'});
      const err = errBox();
      const go = h('button', {class: 'btn w', type: 'submit'}, 'Sign in');
      card(h('form', {novalidate: true, onsubmit: async (e) => {
        e.preventDefault();
        err.textContent = '';
        go.disabled = true;
        try {
          const r = await call('/api/auth/login', {username: user.value, password: pw.value});
          code(r.ticket);
        } catch (ex) {
          err.textContent = ex.message;
          pw.value = '';
          pw.focus();
        } finally {
          go.disabled = false;
        }
      }}, h('h2', {id: 'title'}, 'Sign in'),
      plain ? h('p', {class: 'formerr'}, 'Signing in needs HTTPS: open this app at its https:// address (the browser keeps no sign-in over plain http).') : null,
      field('Username', user), field('Password', pw), err, h('div', {class: 'row'}, go), lock));
      user.focus();
    }

    function code(ticket, recovery = false) {
      const err = errBox();
      const go = h('button', {class: 'btn w', type: 'button'}, 'Verify');
      let read;
      let box;
      const send = async () => {
        if (go.disabled) return;
        err.textContent = '';
        go.disabled = true;
        try {
          await call('/api/auth/code', recovery ? {ticket, recovery: read()} : {ticket, code: read()});
          location.reload();
        } catch (ex) {
          err.textContent = ex.message;
          if (ex.status === 401 && /again/.test(ex.message)) { password(); return; }
          if (box && box.clear) box.clear();
        } finally {
          go.disabled = false;
        }
      };
      go.addEventListener('click', send);
      if (recovery) {
        const inp = h('input', {type: 'text', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', maxlength: '20', placeholder: 'xxxx-xxxx',
          onkeydown: (e) => { if (e.key === 'Enter') send(); }});
        read = () => inp.value;
        box = null;
        card(h('h2', {id: 'title'}, 'Use a recovery code'), h('p', {}, 'One of the 8 codes you saved. Each works once.'),
          field('Recovery code', inp), err, h('div', {class: 'row'}, go),
          h('div', {class: 'lrow'}, h('button', {class: 'lnk', type: 'button', onclick: () => code(ticket, false)}, 'Use the 6‑digit code'),
            h('button', {class: 'lnk', type: 'button', onclick: password}, 'Back')));
        inp.focus();
        return;
      }
      box = codeBoxes(() => send());
      read = () => box.value();
      card(h('h2', {id: 'title'}, 'Enter the 6‑digit code'), h('p', {}, 'From the authenticator app on your phone.'), box, err,
        h('div', {class: 'row'}, go),
        h('div', {class: 'lrow'}, h('button', {class: 'lnk', type: 'button', onclick: () => code(ticket, true)}, 'Use a recovery code'),
          h('button', {class: 'lnk', type: 'button', onclick: password}, 'Back')));
      box.focusFirst();
    }

    password();
  }

  window.HFLogin = {enroll, strength, codeBoxes};

  if (document.body.classList.contains('signin')) {
    const page = $('page');
    if (document.body.dataset.mode === 'setup') {
      document.title = 'Set up sign-in · Hermes Finances';
      enroll(page, {brand: true, onDone: () => location.reload()});
    } else {
      signIn(page);
    }
  }
})();
