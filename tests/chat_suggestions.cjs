const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(process.env.CHAT_JS || path.join(__dirname, '../src/hermes_finance/static/chat.js'), 'utf8');
const MAX_LABEL = 50;   // the hard data-level cap in chat.js: a chip label over it is dropped, never rendered
function fixture(month = '2026-12') {
  return {month, today: '2027-01-01', categories: [
    {category: 'Groceries', spend: 220, cap: 200, due: 0, status: 'over'},
    {category: 'Shopping', spend: 20, cap: 200, due: 0, status: 'ok'},
  ], upcoming: [{kind: 'bill', merchant: 'Example Power', next_expected: '2027-01-03'}]};
}
function boot(data = fixture(), coarse = false, saved = []) {
  function el(tag) { return {tag, kids: [], attrs: {}, dataset: {}, style: {}, hidden: false, value: '', scrollHeight: 0,
    events: {}, focusCount: 0, classList: {add(){}, remove(){}, contains(){return false;}},
    append(...kids){this.kids.push(...kids);}, setAttribute(k,v){this.attrs[k]=v;},
    addEventListener(k,v){this.events[k]=v;}, focus(){this.focusCount++;}}; }
  const flat = kids => kids.flat(Infinity).filter(k => k != null && k !== false);
  function h(tag, attrs, ...kids) {const e=el(tag); Object.assign(e.attrs, attrs); if(attrs?.hidden)e.hidden=true;
    if(attrs?.dataset)Object.assign(e.dataset,attrs.dataset); e.append(...flat(kids)); return e;}
  function fill(e,...kids){e.kids=flat(kids);return e;}
  const body=el('body');body.dataset.ask='api';
  const calls=[], posts=[], storage={'hf.chat.v1':JSON.stringify(saved)};
  const HF={h,fill,icon:()=>el('i'),glyph:()=>el('i'),RGB:{}, S:{month:data?.month || '2026-12'},
    money:String,money2:String,plural:String,budgetRing:()=>el('svg'),ibadge:()=>el('i'),refresh(){},loadCounts(){},
    async api(url,q){calls.push({url,q});return await HF.answer(url,q);}, answer:async()=>data,
    async post(url,payload){posts.push({url,payload});return await HF.reply();},reply:async()=>({reply:'Fixture answer'})};
  const events={}, documentEvents={};
  const sandbox={document:{body,visibilityState:'visible',addEventListener(k,v){documentEvents[k]=v;},getElementById:()=>el('div')},console,setTimeout,clearTimeout,
    addEventListener(k,v){events[k]=v;},
    sessionStorage:{getItem:k=>storage[k],setItem:(k,v)=>storage[k]=v},matchMedia:()=>({matches:coarse})};
  sandbox.window=sandbox;vm.createContext(sandbox);vm.runInContext(source,sandbox);sandbox.HF_EXT[0](HF);
  function all(e){return [e,...(e.kids||[]).filter(x=>typeof x==='object').flatMap(all)];}
  const nodes=()=>all(body), chips=()=>nodes().filter(e=>e.attrs.class==='fchip');
  const texts=()=>chips().map(e=>e.kids.filter(k=>typeof k==='string').join(''));
  return {HF,calls,posts,storage,body,nodes,chips,texts,events,documentEvents,input:()=>nodes().find(e=>e.tag==='textarea')};
}
async function starters() {
  const t=boot();await t.HF.chat.open();
  assert(t.texts().includes('Why is Groceries over budget in Dec 2026?'), JSON.stringify(t.texts()));
  assert(t.texts().includes('How much for Groceries in Dec 2026?'));
  assert(t.texts().includes('What bills are due in the next 7 days?'));
  assert.equal(t.calls[0].url,'/api/overview');assert.equal(t.calls[0].q.month,'2026-12');
  assert(!t.texts().some(q=>q.includes('Shopping') ||
    /January|February|March|April|May|June|July|August|September|October|November|December/.test(q)),
    'no other category, and never a full month name');
  assert(t.texts().every(q=>q.length<=MAX_LABEL));
}
async function followups() {
  const t=boot();await t.HF.chat.open();
  await t.HF.chat.ask('Why is Groceries over budget in Dec 2026?');
  assert(t.texts().includes('Top Groceries merchants in Dec 2026?'), JSON.stringify(t.texts()));
  assert(t.texts().includes('How does Groceries compare with Nov 2026?'));
  assert(!t.texts().some(q=>q.includes('next 7 days')));
  await t.HF.chat.ask('What bills are due in the next 7 days?');
  assert(t.texts().includes('How much do I owe in bills this week?'));
  assert(!t.texts().some(q=>q.includes('Groceries')));
  await t.HF.chat.ask('Tell me a joke about budgets in March 2024.');
  assert(t.texts().includes('Why is Groceries over budget in Dec 2026?'));
  t.input().value='unsent draft';
  t.HF.chat.reset();
  assert.equal(t.HF.chat.log.length,0);assert.equal(t.input().value,'unsent draft');
  assert(t.texts().includes('Why is Groceries over budget in Dec 2026?'));
  assert.equal(JSON.parse(t.storage['hf.chat.v1']).length,0);
}
async function refreshCases() {
  const t=boot();await t.HF.chat.open();t.input().value='draft';
  t.HF.S.month='2027-01';t.HF.answer=async()=>fixture('2027-01');
  await t.HF.chat.refreshContext();
  assert(t.texts().some(q=>q.includes('Jan 2027')));assert(!t.texts().some(q=>q.includes('Dec 2026')));
  assert.equal(t.input().value,'draft');
  let release;
  t.HF.answer=()=>new Promise(r=>release=r);
  const old=t.HF.chat.refreshContext();
  t.HF.S.month='2026-11';t.HF.answer=async()=>fixture('2026-11');await t.HF.chat.refreshContext();
  release(fixture('2027-01'));await old;
  assert(t.texts().some(q=>q.includes('Nov 2026')));assert(!t.texts().some(q=>q.includes('Jan 2027')));
  const fails=boot();fails.HF.answer=async()=>{throw Error('offline');};await fails.HF.chat.open();
  assert(!fails.texts().some(q=>q.includes('over budget') || q.includes('Groceries') || q.includes('next 7 days')));
  assert(fails.texts().includes('Show my budgets for Dec 2026?'),'the neutral fallback still names the month and year');
  t.HF.answer=async()=>{throw Error('offline');};await t.HF.chat.refreshContext();
  assert(t.texts().some(q=>q.includes('Nov 2026')),'a same-month refetch failure keeps the still-correct chips');
  assert.equal(t.posts.length,0);
  t.HF.answer=async()=>fixture('2026-11');
  assert.equal(typeof t.events.focus,'function','returning to the app refreshes server dates and data');
  await t.events.focus();assert(t.texts().some(q=>q.includes('Groceries')));
  assert.equal(typeof t.documentEvents.visibilitychange,'function');
  await t.documentEvents.visibilitychange();
}
async function guards() {
  const d=fixture();d.categories=[{category:'Utilities',spend:50,cap:100,due:80,status:'warn'}];
  d.upcoming=[{kind:'subscription',next_expected:'2027-01-02'},{kind:'bill',next_expected:'2027-01-08'},
    {kind:'bill',next_expected:'2026-12-31'}];
  const booking=fixture();booking.categories=[{category:'Rent',spend:0,cap:2000,due:2000,status:'warn'}];
  const b=boot(booking);await b.HF.chat.open();
  assert(b.texts().includes('Bills in my Rent budget for Dec 2026?'),JSON.stringify(b.texts()));
  const t=boot(d,true);await t.HF.chat.open();
  assert(t.texts().includes('Bills in my Utilities budget for Dec 2026?'));
  assert(!t.texts().some(q=>q.includes('Utilities over budget') || q.includes('next 7 days')));
  assert.equal(t.input().focusCount,0);
  await t.HF.chat.ask('Bills in my Utilities budget for Dec 2026?');
  assert(t.texts().includes("What's left in Utilities after bills?"));
  assert(!t.texts().some(q=>q.includes('merchants')));
  t.HF.reply=async()=>{throw Error('request failed');};await t.HF.chat.ask(t.texts()[0]);
  assert.equal(t.input().focusCount,0, 'a chip answer must not open the mobile keyboard');
  assert(!t.texts().some(q=>q.includes('merchants')));
  const empty=boot({month:'2026-12',today:'2027-01-01',categories:[],upcoming:[]});await empty.HF.chat.open();
  assert(empty.texts().every(q=>!q.includes('over budget') && !q.includes('next 7 days')));
  assert.equal(empty.input().focusCount,1);
  const malformed=boot({month:'2026-12',today:'not-a-date',categories:[null,{category:'Fake',spend:'300',cap:20,status:'over'}],upcoming:[null]});
  await malformed.HF.chat.open();assert(malformed.texts().every(q=>!q.includes('over budget') && !q.includes('Fake')));
  const hostile=fixture();hostile.categories[0].category='<img src=x>';
  const x=boot(hostile);await x.HF.chat.open();
  assert(x.texts().some(q=>q.includes('<img src=x>')));assert(!x.nodes().some(e=>e.tag==='img'));
  await x.chips()[0].attrs.onclick();
  assert.equal(x.posts[0].payload.messages[0].content,'Why is <img src=x> over budget in Dec 2026?');
  assert.deepEqual(Object.keys(x.posts[0].payload.messages[0]).sort(),['content','role']);
}
async function lifecycle() {
  const d=fixture('2027-01');d.today='2026-12-30';d.upcoming=[{kind:'bill',next_expected:'2027-01-02'}];
  const t=boot(d);await t.HF.chat.open();
  assert(t.texts().includes('What bills are due in the next 7 days?'));
  assert(t.texts().includes('Why is Groceries over budget in Jan 2027?'));
  await t.HF.chat.ask('How much for Groceries in Jan 2027?');
  assert(t.texts().includes('How does Groceries compare with Dec 2026?'));
  const restored=boot(d,false,JSON.parse(t.storage['hf.chat.v1']));await restored.HF.chat.open();
  assert.deepEqual(restored.texts(),t.texts());
  let reply; t.HF.reply=()=>new Promise(r=>reply=r);
  const pending=t.HF.chat.ask(t.texts()[0]);
  const count=t.HF.chat.log.length;
  t.HF.chat.reset();await t.HF.chat.ask('another question');
  assert.equal(t.HF.chat.log.length,count);assert(t.chips().every(c=>c.attrs.disabled));
  t.HF.S.month='2026-11';t.HF.answer=async()=>fixture('2026-11');await t.HF.chat.refreshContext();
  reply({reply:'Answer for the earlier selected month'});await pending;
  assert(t.texts().some(q=>q.includes('Nov 2026')));assert(!t.texts().some(q=>q.includes('Jan 2027')));
  t.HF.chat.close();const calls=t.calls.length;await t.HF.chat.refreshContext();assert.equal(t.calls.length,calls);
  const closed=boot();let data;closed.HF.answer=()=>new Promise(r=>data=r);
  const opening=closed.HF.chat.open();closed.HF.chat.close();data(fixture());await opening;
  assert.equal(closed.input().focusCount,0,'a delayed open must not focus a closed textarea');
  const feb=fixture('2028-02');feb.today='2028-02-27';feb.upcoming=[{kind:'bill',next_expected:'2028-03-02'}];
  const leap=boot(feb);await leap.HF.chat.open();assert(leap.texts().includes('What bills are due in the next 7 days?'));
}
async function bridge() {
  // Chips -> message -> follow-up: the guard that carries the answered topic is the only thing follow-ups key on.
  const t=boot();await t.HF.chat.open();
  await t.HF.chat.ask(t.texts()[0]);                       // "Why is Groceries over budget in Dec 2026?"
  const assistant=t.HF.chat.log[t.HF.chat.log.length-1];
  // JSON round-trip: the log object comes from the vm realm, so its prototype differs from an object literal here
  assert.deepEqual(JSON.parse(JSON.stringify(assistant.questionContext)),{topic:'category',category:'Groceries',
    month:'2026-12',question:'Why is Groceries over budget in Dec 2026?'});
  assert.equal(assistant.questionContext.today,undefined,'a category question carries no bill window');
  assert(Object.keys(t.posts[0].payload.messages[0]).every(k=>k==='role'||k==='content')
    && !JSON.stringify(t.posts[0].payload).includes('questionContext'), 'the guard never rides along to the server');
  assert(t.texts().includes('Top Groceries merchants in Dec 2026?'));
  assert(t.texts().includes('How does Groceries compare with Nov 2026?'));
  const asks=t.posts.length;await t.HF.chat.ask('Something unrelated');
  assert(t.texts().includes('Why is Groceries over budget in Dec 2026?'),'an unclassified prompt falls back to starters');
  assert.equal(t.posts.length,asks+1);
}
async function lengthInvariant() {
  // The invariant the layout gate enforces, asserted directly: every emitted chip label fits on one desktop line.
  const isMonth=/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?![a-z])/;
  const withYear=/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}/;
  const fullMonth=/January|February|March|April|May|June|July|August|September|October|November|December/;
  const check=(texts,mon)=>{
    assert(texts.length>0 && texts.length<=3, JSON.stringify(texts));
    assert(texts.every(q=>q.length<=MAX_LABEL), `every chip label <= ${MAX_LABEL}: ${JSON.stringify(texts)}`);
    assert(!texts.some(q=>fullMonth.test(q)), 'never a full month name in a chip');
    assert.equal(texts.filter(q=>isMonth.test(q) && !withYear.test(q)).length,0,
      `a month token without its 4-digit year: ${JSON.stringify(texts)}`);
    assert(texts.some(q=>q.includes(mon)), `a rendered chip names ${mon} (month + year)`);
  };
  // The 16-char worst case ("Rent & Utilities"): over-budget 48, compare 48, bills-budget 49 and share 50 all survive.
  const util=(month,over)=>({month, today:'2027-01-01', categories:[
    {category:'Rent & Utilities', spend:over?2500:900, cap:2000, due:over?120:500,
      status:over?'over':'warn'}], upcoming:[{kind:'bill', next_expected:'2027-01-03'}]});
  const t=boot(util('2026-12',true));await t.HF.chat.open();
  check(t.texts(),'Dec 2026');
  assert(t.texts().includes('Why is Rent & Utilities over budget in Dec 2026?'),'the 48-char over chip survives');
  await t.HF.chat.ask('Why is Rent & Utilities over budget in Dec 2026?');
  assert(t.texts().includes('How does Rent & Utilities compare with Nov 2026?'),'the 48-char compare chip survives');
  const bills=boot(util('2026-12',false));await bills.HF.chat.open();
  check(bills.texts(),'Dec 2026');
  assert(bills.texts().includes('Bills in my Rent & Utilities budget for Dec 2026?'),'the 49-char chip survives');
  await bills.HF.chat.ask('Bills in my Rent & Utilities budget for Dec 2026?');
  assert(bills.texts().includes('How much of Rent & Utilities is bills in Dec 2026?'),'the 50-char chip survives');
  check(bills.texts(),'Dec 2026');
  // Boundary: an 18-char name pushes that last form to 51 chars, so it is filtered and the shorter spend chip stands in.
  const mid=boot({month:'2026-12', today:'2027-01-01', categories:[
    {category:'Rent & Utilities!!', spend:900, cap:2000, due:500, status:'warn'}], upcoming:[]});
  await mid.HF.chat.open();
  check(mid.texts(),'Dec 2026');
  assert(!mid.texts().some(q=>q.includes('Bills in my Rent & Utilities!!')),'the 51-char form is filtered out');
  assert(mid.texts().includes('How much for Rent & Utilities!! in Dec 2026?'),'the shorter form still names it');
  // A name so long that no data-aware question fits at all: only the fixed-length chips remain, still under the cap.
  const huge=boot({month:'2026-12', today:'2027-01-01', categories:[
    {category:'X'.repeat(30), spend:99, cap:1, due:0, status:'over'}],
    upcoming:[{kind:'bill', next_expected:'2027-01-03'}]});
  await huge.HF.chat.open();
  check(huge.texts(),'Dec 2026');
  assert(!huge.texts().some(q=>q.includes('X'.repeat(30))),'an unrenderable length is never clipped into a chip');
  // Flipping the selected month changes the month token in the starters (the user-visible claim: the chips vary).
  t.HF.S.month='2026-09'; t.HF.answer=async()=>util('2026-09',true);
  await t.HF.chat.refreshContext();
  check(t.texts(),'Sep 2026');
  assert(t.texts().includes('Why is Rent & Utilities over budget in Sep 2026?'),'the over chip follows the month');
  assert(!t.texts().some(q=>q.includes('Dec 2026')),'the previously selected month is gone');
  assert(t.texts().every(q=>!/Sep(?! \d{4})/.test(q)),'the month token always carries its 4-digit year');
}
function routeRefresh() {
  const app=fs.readFileSync(path.join(__dirname,'../src/hermes_finance/static/app.js'),'utf8');
  const route=app.slice(app.indexOf('  function route(opts'),app.indexOf('  function refresh()'));
  const S={month:'2026-12',counts:{month:'2026-12'},token:0};let seen=[];
  const sandbox={S, HF:{chat:{refreshContext:()=>seen.push(S.month)}},
    parseHash:()=>({page:'overview',params:new URLSearchParams('month=2026-11')}),
    MONTH_PAGES:new Set(['overview']), MONTH_RE:/^\d{4}-(0[1-9]|1[0-2])$/,thisMonth:()=> '2027-01',
    location:{hash:'#/overview?month=2026-11'}, window:{scrollTo(){}},document:{body:{classList:{toggle(){},contains(){return true;},add(){}}}},
    BOARDS:new Set(),RENDER:{overview:async()=>{}},readInk(){},loadCounts(){},renderNav(){},renderTop(){},
    closeDrawer(){},closePanel(){},finishCounts(){},COUNT:{tk:0,done:new Set(),live:new Set()},hideToast(){},closeSyncPanel(){},destroyCharts(){},reducedMotion:()=>false};
  vm.createContext(sandbox);vm.runInContext(route+'\nroute(); route({keepToast:true});',sandbox);
  assert.deepEqual(seen,['2026-11','2026-11'], 'navigation and writes refresh open chat after selecting month');
}
(async()=>{await starters();await followups();await refreshCases();await guards();await lifecycle();await bridge();await lengthInvariant();routeRefresh();console.log('contextual suggestions: 8 scenario groups passed');})().catch(e=>{console.error(e);process.exitCode=1;});
