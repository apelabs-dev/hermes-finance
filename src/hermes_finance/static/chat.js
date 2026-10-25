/* Hermes Finances: the Ask panel (TASK-23), as approved in MOCK-017 (item 3 + addendum).
 *
 * An "Ask" button bottom-right on every page (a phone: above the tab bar) opens a panel on the right (a phone: the whole
 * screen) with the conversation, three suggestions, the input and Send. The header's chip names the brain ("via Hermes",
 * "via OpenRouter · <model>"). With AI off the page has no data-ask flag: no button, and the server refuses /api/chat.
 *
 * The app owns the tool loop (chat.py): POST /api/chat answers with the reply, the source lines of the reads it used, the
 * budget rings of the categories it names, and an approval card per change the model asked for. A card's Approve / Deny
 * is POST /api/chat/decide, Settings' own path (mcp_state.decide): one record, applied once. A card decided elsewhere
 * (Settings) shows its final state when the panel is drawn again. A card left "approved" (an Approve still being applied,
 * maybe by another tab or Settings) is asked for again while the panel is open, backing off from 1 s to 15 s, until it's
 * final (the server settles one whose applier stopped: mcp_state.reconcile); it's never drawn back as pending.
 * The conversation is kept for the browser session
 * (sessionStorage). Enter sends, Shift+Enter is a new line; Esc or ✕ closes and focus goes back to the Ask button.
 * Runs inside app.js's building blocks (window.HF_EXT, see app.js).
 */
(window.HF_EXT = window.HF_EXT || []).push((HF) => {
  'use strict';
  const {h, fill, icon, glyph, api, post, money, money2, plural, budgetRing, ibadge, RGB, refresh, loadCounts} = HF;
  let mode = document.body.dataset.ask;
  const enabled = () => mode === 'api' || mode === 'hermes';
  let settingsToken = 0;
  const KEY = 'hf.chat.v1';
  // Read-only page data, never model-generated suggestions. Dates in questions are explicit so a selected past
  // month cannot be confused with today's bills (or with the same month in another year). A chip label must fit on
  // ONE line in the desktop panel, so the compact month token (short name + 4-digit year, never a bare month) and
  // MAX_LABEL are a hard data-level rule: an over-length question is dropped, never clipped or ellipsised (the
  // layout gate rejects clipped text too). 50 chars keeps a margin under the ~54-char one-line boundary measured for
  // this wording in the real panel.
  let context = null;
  let contextToken = 0;
  const MAX_LABEL = 50;
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const validMonth = (m) => typeof m === 'string' && /^\d{4}-(0[1-9]|1[0-2])$/.test(m);
  const monthToken = (m) => `${MONTHS[Number(m.slice(5)) - 1]} ${m.slice(0, 4)}`;
  const previousMonth = (m) => {
    const d = new Date(`${m}-01T00:00:00Z`);
    d.setUTCMonth(d.getUTCMonth() - 1);
    return d.toISOString().slice(0, 7);
  };
  const fit = (question) => typeof question === 'string' && question.length <= MAX_LABEL;
  // One builder per question so a starter and its follow-up are the exact same string, and every one of them is a
  // real question the backend tools answer.
  const Q = {
    over: (c, mon) => `Why is ${c} over budget in ${mon}?`,
    spend: (c, mon) => `How much for ${c} in ${mon}?`,
    merchants: (c, mon) => `Top ${c} merchants in ${mon}?`,
    compare: (c, mon, prev) => `How does ${c} compare with ${prev}?`,
    billsBudget: (c, mon) => `Bills in my ${c} budget for ${mon}?`,
    billsLeft: (c) => `What's left in ${c} after bills?`,
    billsShare: (c, mon) => `How much of ${c} is bills in ${mon}?`,
  };
  const BILLS_WINDOW = ['What bills are due in the next 7 days?', 'How much do I owe in bills this week?',
    'Which bill is due next?'];
  const selectedMonth = () => HF.S && HF.S.month;
  // The categories the panel can ask about, biggest spend first; the API's own 'over' flag wins, else the cap rule
  // (the same rule as the category's budget card / alert).
  const readCats = () => (Array.isArray(context.categories) ? context.categories : [])
    .filter((c) => c && typeof c.category === 'string' && c.category.trim() && Number.isFinite(c.spend))
    .sort((a, b) => b.spend - a.spend || a.category.localeCompare(b.category));
  const overCats = (cats) => {
    const flagged = cats.filter((c) => c.status === 'over');
    return flagged.length ? flagged : cats.filter((c) => Number.isFinite(c.cap) && c.cap > 0 && c.spend > c.cap);
  };
  function starterQuestions() {
    const month = selectedMonth();
    const mon = validMonth(month) ? monthToken(month) : null;
    const fallback = ['What spending data is available?',
      mon ? `Show my budgets for ${mon}?` : 'Show my budgets.', 'What bills are coming up?'].filter(fit);
    if (!context || context.month !== month || !mon) return fallback;
    const cats = readCats();
    const over = overCats(cats);
    const committed = cats.filter((c) => c.status === 'warn' && Number.isFinite(c.due) && c.due > 0);
    const questions = [];
    if (over.length) questions.push(Q.over(over[0].category, mon));
    else if (committed.length && Number.isFinite(committed[0].cap))
      questions.push(Q.billsBudget(committed[0].category, mon));
    if (billWindow()) questions.push(BILLS_WINDOW[0]);
    const spent = cats.find((c) => c.spend > 0);   // readCats() is spend-desc, so this is the biggest spender
    if (spent) questions.push(Q.spend(spent.category, mon));
    // Hard invariant: dedupe, drop anything over MAX_LABEL (so nothing can wrap), then at most three chips.
    return [...new Set([...questions.filter(fit), ...fallback])].slice(0, 3);
  }
  // True only when a real bill (not a subscription) is expected today..today+6; the month token never hides a date.
  function billWindow() {
    const today = context && context.today;
    if (typeof today !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(today)) return false;
    const date = new Date(`${today}T00:00:00Z`);
    if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== today) return false;
    date.setUTCDate(date.getUTCDate() + 6);
    const end = date.toISOString().slice(0, 10);
    return (Array.isArray(context.upcoming) ? context.upcoming : []).some((b) => b && b.kind === 'bill' &&
      typeof b.next_expected === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(b.next_expected) &&
      b.next_expected >= today && b.next_expected <= end);
  }
  // Only supported, unambiguous questions are classified. Unknown prompts and failed requests keep starter chips;
  // assistant prose is never mined for facts. Metadata is tied to the month and the API's current bill window. Every
  // question raised here is the exact string a starter can emit (so a pressed starter always classifies), and is
  // filtered through MAX_LABEL, so a topic may carry fewer than three usable questions.
  function questionTopics() {
    if (!context || context.month !== selectedMonth() || !validMonth(context.month)) return [];
    const month = context.month;
    const mon = monthToken(month);
    const prev = monthToken(previousMonth(month));
    const cats = readCats();
    const over = new Set(overCats(cats).map((c) => c.category));
    const topics = [];
    for (const c of cats) {
      if (c.spend < 0) continue;
      const category = c.category;
      const questions = c.spend > 0 ? [Q.spend(category, mon), Q.merchants(category, mon),
        Q.compare(category, mon, prev)] : [];
      if (over.has(category)) questions.push(Q.over(category, mon));
      const usable = questions.filter(fit);
      if (usable.length) topics.push({topic: 'category', category, month, questions: usable});
      if (Number.isFinite(c.cap) && c.cap >= 0 && Number.isFinite(c.due) && c.due > 0) {
        const bills = [Q.billsBudget(category, mon), Q.billsLeft(category), Q.billsShare(category, mon)].filter(fit);
        if (bills.length) topics.push({topic: 'budget-bills', category, month, questions: bills});
      }
    }
    if (billWindow()) topics.push({topic: 'bills', month, today: context.today, questions: BILLS_WINDOW.filter(fit)});
    return topics;
  }
  function classifyQuestion(q) {
    const found = questionTopics().find((t) => t.questions.some((v) => v.toLowerCase() === q.toLowerCase()));
    return found ? {topic: found.topic, category: found.category, month: found.month, today: found.today,
      question: q} : null;
  }
  function suggestionQuestions() {
    const last = LOG[LOG.length - 1];
    const answered = last && last.role === 'assistant' && last.questionContext;
    if (!answered) return starterQuestions();
    const topic = questionTopics().find((t) => t.topic === answered.topic && t.month === answered.month &&
      t.category === answered.category && t.today === answered.today);
    if (!topic) return starterQuestions();
    return topic.questions.filter((q) => q.toLowerCase() !== String(answered.question).toLowerCase()).slice(0, 3);
  }
  function drawChips() {
    fill(chips, suggestionQuestions().map((q) => h('button', {class: 'fchip', type: 'button', disabled: busy,
      onclick: () => ask(q)}, q)));
  }
  async function refreshContext() {
    const token = ++contextToken;
    const month = selectedMonth();
    // A different month makes the old data wrong at once, so the neutral questions stand in until the new rows arrive.
    // Under the same month the previous, still-correct answers stay until the fetch lands.
    if (context && (!validMonth(month) || context.month !== month)) context = null;
    drawChips();
    if (panel.hidden || !validMonth(month)) return;
    try {
      const data = await api('/api/overview', {month});
      if (token !== contextToken || panel.hidden || month !== selectedMonth()) return;
      context = data && data.month === month ? data : (context && context.month === month ? context : null);
    } catch (e) {
      if (context && context.month !== month) context = null;   // a failure never leaves another month's facts up
    }
    if (token === contextToken) drawChips();
  }
  const HELLO = 'Ask about your money.';

  const load = () => { try { const v = JSON.parse(sessionStorage.getItem(KEY) || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; } };
  const store = () => { try { sessionStorage.setItem(KEY, JSON.stringify(LOG)); } catch (e) { /* private mode: kept in memory */ } };
  const LOG = load();        // [{role: 'user'|'assistant'|'note', content, sources?, stats?, cards?: [id]}]
  const CARDS = new Map();   // id -> the card as the server last sent it
  let busy = false;
  let poll = null;           // the timer asking again for "approved" cards (watch()), and whether that ask is out
  let polling = false;
  let pollWait = 1000;
  // A phone or tablet (coarse pointer): opening the panel must NOT pop the keyboard — that happens only when the user
  // taps the field. On a desktop the caret is ready to type straight away.
  const COARSE = typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches;

  const askBtn = h('button', {class: 'askb', type: 'button', id: 'askb', 'aria-haspopup': 'dialog', 'aria-controls': 'chat',
    'aria-expanded': 'false'}, icon('chat'), 'Ask');
  const panel = h('section', {class: 'chat', id: 'chat', role: 'dialog', 'aria-modal': 'false', 'aria-labelledby': 'chat-title', hidden: true});
  const msgs = h('div', {class: 'chmsgs', role: 'log', 'aria-live': 'polite'});
  const input = h('textarea', {class: 'chin', rows: 1, placeholder: 'Ask about your money', 'aria-label': 'Ask about your money', maxlength: '4000'});
  const chips = h('div', {class: 'chchips', role: 'group', 'aria-label': 'Suggestions'});
  const send = h('button', {class: 'btn sm', type: 'button', id: 'chsend'}, icon('send'), 'Send');
  const brain = document.body.dataset.askBrain || (mode === 'hermes' ? 'via Hermes' : 'via API');
  const brainBadge = h('span', {class: 'brain'});
  function setBrain(label) {
    brainBadge.title = mode === 'hermes' ? 'Answers come from your Hermes model' : `Answers come from ${label.replace(/^via /, '')}`;
    fill(brainBadge, icon(mode === 'hermes' ? 'sparkles' : 'cpu'), label);
  }
  setBrain(brain);
  fill(panel,
    h('div', {class: 'chh'}, h('span', {class: 'hi', style: {'--rgb': RGB.acc}}, glyph('sparkles')),
      h('div', {class: 'grow'}, h('div', {class: 'chtl'}, h('h2', {id: 'chat-title'}, 'Ask'),
        brainBadge)),
      h('button', {class: 'ib', type: 'button', 'aria-label': 'Close', onclick: () => close()}, icon('x'))),
    msgs,
    h('div', {class: 'chfoot'},
      chips,
      h('div', {class: 'chrow'}, input, send)));
  if (enabled()) document.body.append(askBtn, panel);
  else askBtn.hidden = true;

  // Settings saves in place. Keep one controller and one set of listeners across mode changes.
  document.addEventListener('hf-prefs', async (e) => {
    const token = ++settingsToken;
    mode = e.detail.ai.mode;
    if (enabled()) document.body.dataset.ask = mode;
    else delete document.body.dataset.ask;
    if (!enabled()) {
      close();
      stopWatching();
      askBtn.hidden = true;
      askBtn.remove();
      panel.remove();
      delete document.body.dataset.askBrain;
      return;
    }
    if (!askBtn.isConnected) document.body.append(askBtn, panel);
    askBtn.hidden = !panel.hidden;
    setBrain(mode === 'hermes' ? 'via Hermes' : 'via API');
    try {
      const state = await api('/api/chat');   // metadata only; the server owns provider/model labels
      if (token !== settingsToken || !enabled()) return;
      document.body.dataset.askBrain = state.label;
      setBrain(state.label);
    } catch (e) { /* keep the mode label if metadata is temporarily unavailable */ }
  });

  // ---- drawing ------------------------------------------------------------------------------------------------------
  function stat(s) {
    return h('div', {class: 'chstat'}, budgetRing(s.spent, s.due || 0, s.cap, 54, 5, 13, {used: false}),
      h('span', {class: 'grow'}, h('b', {}, s.category),
        h('span', {}, h('b', {class: `v ${s.status === 'over' ? 'bad' : s.status === 'warn' ? 'warn' : ''}`}, money2(s.spent)),
          ` of ${money(s.cap)} · ${plural(s.count, 'payment')}`)));
  }
  function paras(text) {
    return String(text).split(/\n{2,}/).map((p) => h('p', {}, p.replace(/\n/g, ' ').replace(/\*\*(.+?)\*\*/g, '$1')));
  }
  function cardEl(c) {
    const box = h('div', {class: 'appr', role: 'group', 'aria-label': 'Suggested change', dataset: {card: String(c.id), status: c.status}});
    const decided = c.status !== 'pending';
    const result = c.status === 'applied' ? 'ok' : c.status === 'failed' ? 'bad' : '';
    const ico = c.status === 'applied' || c.status === 'approved' ? 'check' : c.status === 'failed' ? 'alert' : 'x';
    const state = h('p', {class: `apres ${result}`, role: 'status'}, icon(ico), c.result_line || 'Decided');
    // still pending on the server (an Approve / Deny that didn't go through): the buttons stay, with why
    const buttons = [c.error ? h('p', {class: 'apres bad', role: 'alert'}, icon('alert'), c.error) : null,
      h('div', {class: 'row'},
        h('button', {class: 'btn sm', type: 'button', onclick: (e) => decide(c, true, e.currentTarget)}, icon('check'), 'Approve'),
        h('button', {class: 'btn sm ghost', type: 'button', onclick: (e) => decide(c, false, e.currentTarget)}, 'Deny'))];
    fill(box,
      h('div', {class: 'aph'}, ibadge('pencil', RGB.warn, 'xs'), h('span', {}, 'Suggested change')),
      h('b', {class: 'apt'}, c.title),
      h('p', {}, c.summary),
      decided ? state : [...buttons, h('p', {class: 'hint'}, 'Nothing changes until you approve.')]);
    return box;
  }
  function entry(m) {
    if (m.role === 'user') return h('div', {class: 'msg me'}, h('p', {}, m.content));
    if (m.role === 'note') return h('div', {class: 'msg bot err', role: 'alert'}, h('p', {}, icon('alert'), m.content));
    const out = [h('div', {class: 'msg bot'}, paras(m.content), (m.stats || []).map(stat),
      (m.sources || []).map((s) => h('p', {class: 'src'}, icon('database'), s)))];
    for (const id of m.cards || []) {
      const c = CARDS.get(id);
      if (c) out.push(h('div', {class: 'msg bot card-msg'}, cardEl(c)));
    }
    return out;
  }
  function draw() {
    drawChips();
    fill(msgs, LOG.length ? LOG.map(entry) : h('div', {class: 'msg bot'}, h('p', {}, HELLO)),
      busy ? h('div', {class: 'msg bot busy', role: 'status'}, h('p', {}, 'Looking at your numbers…')) : null);
    send.disabled = busy;
    input.disabled = busy;
    msgs.scrollTop = msgs.scrollHeight;
  }

  // ---- open / close ---------------------------------------------------------------------------------------------------
  function stopWatching() {
    clearTimeout(poll);
    poll = null;
    pollWait = 1000;
  }
  function watch() {   // an "approved" card (being applied right now, maybe elsewhere): asked for again until it's final
    const ids = [...CARDS.values()].filter((c) => c.status === 'approved').map((c) => c.id);
    if (!ids.length || panel.hidden) { stopWatching(); return; }
    if (poll || polling) return;   // one chain only (closed and opened again while an ask was out: that one goes on)
    poll = setTimeout(async () => {
      poll = null;
      polling = true;
      let applied = false;
      try {
        const r = await api('/api/chat/cards', {ids: ids.join(',')});
        for (const c of r.cards) {
          if (c.status === 'applied' && (CARDS.get(c.id) || {}).status !== 'applied') applied = true;
          CARDS.set(c.id, c);
        }
      } catch (e) { /* asked again at the next step */ }
      polling = false;
      pollWait = Math.min(pollWait * 2, 15000);
      if (applied) { loadCounts(); refresh(); }   // the page shows the change
      if (!panel.hidden) draw();
      watch();
    }, pollWait);
  }
  async function syncCards() {
    const ids = [...new Set(LOG.flatMap((m) => m.cards || []))];
    if (!ids.length) return;
    try {
      const r = await api('/api/chat/cards', {ids: ids.join(',')});
      for (const c of r.cards) CARDS.set(c.id, c);
    } catch (e) { /* drawn as they were */ }
  }
  async function open() {
    if (!enabled() || !panel.hidden) return;
    panel.hidden = false;
    askBtn.hidden = true;
    askBtn.setAttribute('aria-expanded', 'true');
    document.body.classList.add('chaton');
    draw();
    await Promise.all([refreshContext(), syncCards()]);   // a card decided in Settings meanwhile shows its final state
    draw();
    watch();
    // Desktop only. On a phone/tablet this is what popped the keyboard the moment the panel opened.
    if (!panel.hidden && !COARSE && !busy) input.focus();
  }
  function close() {
    if (panel.hidden) return;
    stopWatching();
    ++contextToken;
    panel.hidden = true;
    askBtn.hidden = false;
    askBtn.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('chaton');
    askBtn.focus();
  }
  askBtn.addEventListener('click', open);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || panel.hidden || document.getElementById('modal').classList.contains('on')) return;
    close();
  });

  // ---- asking ----------------------------------------------------------------------------------------------------------
  async function ask(text) {
    const q = String(text || '').trim();
    if (!enabled() || !q || busy) return;
    const questionContext = classifyQuestion(q);
    LOG.push({role: 'user', content: q});
    store();
    input.value = '';
    grow();
    busy = true;
    draw();
    try {
      const r = await post('/api/chat', {messages: LOG.filter((m) => m.role !== 'note').map((m) => ({role: m.role, content: m.content}))});
      for (const c of r.cards || []) CARDS.set(c.id, c);
      LOG.push({role: 'assistant', questionContext, content: r.reply, sources: r.sources || [], stats: r.stats || [], cards: (r.cards || []).map((c) => c.id)});
    } catch (e) {
      LOG.push({role: 'note', content: e.message || 'Something went wrong. Try again.'});
    }
    store();
    busy = false;
    draw();
    if (!panel.hidden && !COARSE) input.focus();
  }
  async function decide(c, approve, btn) {
    for (const b of btn.parentElement.querySelectorAll('button')) b.disabled = true;
    try {
      const r = await post('/api/chat/decide', {id: c.id, approve});
      CARDS.set(c.id, r.card);
      if (r.card.status === 'applied') { loadCounts(); refresh(); }   // the page shows the change
    } catch (e) {
      await syncCards();   // decided elsewhere (409), or this click did go through: the server's state
      const now = CARDS.get(c.id) || c;
      // still pending (or the app unreachable): nothing was decided, so Approve / Deny stay live under the reason
      if (now.status === 'pending') CARDS.set(c.id, {...now, error: e.status ? e.message : "Couldn't reach the app. Try again."});
    }
    draw();
    watch();
  }
  const grow = () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight + 2, 140)}px`; };
  input.addEventListener('input', grow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      ask(input.value);
    }
  });
  send.addEventListener('click', () => ask(input.value));

  function reset() {
    if (busy) return;
    LOG.length = 0;
    CARDS.clear();
    stopWatching();
    store();
    draw();
  }
  if (typeof window.addEventListener === 'function') window.addEventListener('focus', refreshContext);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') return refreshContext();
  });
  HF.chat = {open, close, ask, reset, refreshContext, log: LOG};   // (the tests and the layout gate open it this way too)
});
