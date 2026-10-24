"""Browser-free behavior checks for the Accounts rows' deferred financial disclosures."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'src/hermes_finance/static'
NODE = shutil.which('node')


@pytest.mark.skipif(NODE is None, reason='node is not installed')
def test_connection_names_and_update_actions_in_card_and_link_sheet():
    source = (STATIC / 'app.js').read_text()
    card = source[source.index('  function connectionsCard(d) {'):source.index("  // ---------- a car's / property's page")]
    link = source[source.index('      const bankLine = (it) => {'):source.index("      const go = h('button'", source.index('      const bankLine = (it) => {'))]
    harness = r"""
const assert=require('node:assert/strict');
const h=(tag,attrs,...kids)=>({tag,attrs:attrs||{},kids:kids.flat(Infinity).filter(x=>x!=null && x!==false)});
const nodes=n=>typeof n==='object'?[n,...n.kids.flatMap(nodes)]:[];
const flat=n=>typeof n==='object'?n.kids.map(flat).join(''):String(n);
const cls=(n,c)=>(n.attrs.class||'').split(' ').includes(c);
const card=(o,...kids)=>h('section',{},kids),ia=()=>h('i'),ring=()=>h('i'),icon=()=>h('i');
const RGB={acc:'blue'},ACC='blue',plural=(n,s)=>`${n} ${s}`,syncLabel=x=>x;
const instShort=x=>x==='JPMorgan Chase Bank, N.A.'?'Chase':x;
let updated,closed=false;
const reconnect=id=>{updated=id},closeModal=()=>{closed=true};
const updateBtn=(id,name)=>h('button',{class:'upd','data-item':id,onclick:()=>reconnect(id)},'Update');
const reconnectBtn=id=>h('button',{onclick:()=>reconnect(id)},'Reconnect');
const name='International Community Financial Services Federal Credit Union';
const item={item_id:'synthetic-bank',institution_name:name,status:'active',last_sync_at:'today'};
""" + card + link + r"""
for(const institution_name of [name,'JPMorgan Chase Bank, N.A.']) {
  const it={...item,institution_name};
  const result=connectionsCard({items:[it],slot_limit:5,slots_used:1});
  const row=nodes(result).find(n=>cls(n,'conn'));
  const grow=nodes(row).find(n=>cls(n,'grow'));
  assert.equal(grow.kids[0].attrs.title,institution_name);
  assert.equal(flat(grow.kids[0]),instShort(institution_name));
  assert(flat(grow.kids[1]).startsWith('Synced '));
  const update=nodes(row).find(n=>cls(n,'upd'));
  assert.equal(flat(update),'Update');
  update.attrs.onclick(); assert.equal(updated,item.item_id);
  const linked=bankLine(it), linkName=nodes(linked).find(n=>n.tag==='b');
  assert.equal(linkName.attrs.title,institution_name);
  assert.equal(flat(linkName),instShort(institution_name));
  updated=null; closed=false;
  nodes(linked).find(n=>cls(n,'upd')).attrs.onclick();
  assert(closed); assert.equal(updated,item.item_id);
}
console.log('Connection names and Update actions passed');
"""
    result = subprocess.run([NODE, '-'], input=harness, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(NODE is None, reason='node is not installed')
def test_accounts_rows_preserve_financial_facts_and_sheet_actions():
    source = (STATIC / 'app.js').read_text()
    rows = source[source.index('  const accountMeta ='):source.index("  // A group's month change")]
    alpaca = (STATIC / 'alpaca.js').read_text()
    # Execute the production renderers with a minimal element factory; deferred help/sheet content is exercised too.
    harness = r"""
const assert=require('node:assert/strict');
const h=(tag,attrs,...kids)=>({tag,attrs:attrs||{},addEventListener:()=>{},kids:kids.flat(Infinity).filter(x=>x!==null && x!==undefined && x!==false)});
const flat=n=> typeof n==='object' ? n.kids.map(flat).join('') : String(n);
const nodes=n=> typeof n==='object' ? [n,...n.kids.flatMap(nodes)] : [];
const cls=(n,c)=>(n.attrs.class||'').split(' ').includes(c);
const find=(n,c)=>nodes(n).find(e=>cls(e,c));
const count=(n,c)=>nodes(n).filter(e=>cls(e,c)).length;
const help=(label,content)=>({...h('button',{class:'help-trigger','aria-label':label}),content});
const money=v=>'$'+Number(v).toFixed(0),money2=v=>'$'+Number(v).toFixed(2);
const fmtDate=x=>String(x),daysAgo=x=>String(x),acctName=x=>x||'';
const clamp=(v,l,u)=>Math.max(l,Math.min(u,v)),sum=(xs,fn)=>xs.reduce((s,x)=>s+(fn(x)||0),0);
const BAD='red',OK='green',spark=(values)=>h('svg',{class:'spark',values});
const manualNwId=id=>'manual:'+id,leftOut=id=>id==='manual:7'||id==='excluded';
const icon=x=>h('svg',{'data-icon':x}),ia=()=>h('i',{class:'ia'}),manualChip=()=>null,ibadge=()=>h('i');
const KIND={credit_card:{label:'Credit card'},loan:{label:'Loan'},vehicle:{label:'Vehicle'},property:{label:'Property'}};
const SUBTYPE={checking:'Checking'},instShort=x=>x,HOOKS={acctRow:()=>null},RGB={};
""" + rows + r"""
const card={id:7,kind:'credit_card',name:'BestBuy Citi Credit Card Platinum Rewards',institution:'Citi',balance:1075.66,
  signed:-1075.66,liability:true,credit_limit:4000,approx:true,balance_as_of:'2026-09-11',as_of:'2026-10-21',
  spark:[1200,1110.66,1075.66],entered_balance:1110.66,matched_total:35,matched_count:1,interest_rate:22.5};
let row=manualLine(card);
assert.equal(count(row,'account-open'),1);
assert.equal(find(row,'account-open').attrs.dataset.edit,'7');
assert.equal(flat(find(row,'nm')),'BestBuy Citi Credit Card Platinum Rewards');
assert.equal(flat(find(row,'chip2')),'Manual');
assert.equal(flat(find(row,'amt')),'≈ $-1075.66');
assert.equal(count(row,'bar2'),1);
assert(!flat(row).includes('last set'));
assert.deepEqual(find(h('div',{},find(row,'help-trigger').content()),'spark').attrs.values,card.spark);
let facts=flat(h('div',{},find(row,'help-trigger').content()));
for(const fact of ['Credit limit','$4000.00','APR','22.5%','last set','$1110.66','$35.00','matched payment','Purchases','Not counted in Custom'])
  assert(facts.includes(fact),fact+' missing from manual disclosure: '+facts);
const zero=manualLine({...card,balance:0,signed:0});
assert(flat(find(zero,'meta2')).includes('No balance'));
assert(!flat(zero).includes('0% of'));
assert.equal(count(zero,'bar2'),0);
const loan=manualLine({id:8,kind:'loan',name:'Loan',balance:500,signed:-500,liability:true,original_amount:1000,
  paid_pct:50,monthly_payment:100,interest_rate:3.81,next_payment:'2026-11-21',payoff:{payoff_month:'2027-03-21'},
  as_of:'2026-10-21',auto_updates:true});
assert(!flat(loan).includes('$100.00'));
assert.equal(count(loan,'bar2'),0);
assert.equal(find(loan,'account-open').attrs.dataset.loan,'8');
facts=flat(h('div',{},find(loan,'help-trigger').content()));
for(const fact of ['Original amount','$1000.00','Monthly payment','$100.00','Next payment','2026-11-21',
  'Estimated payoff','2027-03-21','3.81%','Balance updates from your payments automatically.']) assert(facts.includes(fact),fact);
const car=manualLine({id:9,kind:'vehicle',name:'2019 Zenith Mesa Hybrid XLE Premium',signed:20875,as_of:'2026-10-21',
  valuation:{source:'estimate',estimate:{basis_label:'Trade-in',value:20875,source_label:'Carfax',date:'2026-10-17'},mileage_nudge:true}});
assert(!flat(car).includes('Carfax'));
assert.equal(find(car,'account-open').attrs.href,'#/asset/9');
facts=flat(h('div',{},find(car,'help-trigger').content()));
assert(facts.includes('Carfax') && facts.includes('Update mileage'));
const a={account_id:'excluded',type:'credit',name:'Bank Rewards Platinum',mask:'3591',current:991.44,signed:-991.44,limit:5000};
row=plaidRow(a,'International Federal Credit Union',{}, {excluded:{apr:19.9}},'inst',
 [{card_id:'excluded',transaction_id:'transit1',amount:212.25,date:'2026-10-20',from_account:{name:'Checking'}}]);
assert(!flat(find(row,'nmrow')).includes('3591'));
assert(flat(find(row,'meta2')).includes('••3591'));
// A payment on its way is a numbers fact, not detail: the card's row says it whole (money2 and the word "payment",
// first on the details line so only what follows may be shortened); where it left from stays in the panel.
const lead=nodes(find(row,'meta2')).filter(e=>cls(e,'fx') || cls(e,'mt'))[0];
assert.equal(flat(lead),'$212.25 payment on its way · ');
assert.equal(lead.attrs.dataset.transit,'transit1');
assert(!flat(row).includes('from Checking'));
facts=flat(h('div',{},find(row,'help-trigger').content()));
for(const fact of ['Credit limit','$5000.00','19.9%','from Checking','2026-10-20','counts this payment once']) assert(facts.includes(fact),fact);
row=plaidRow({...a,current:0,signed:0},'Bank',{}, {},'inst');
assert(flat(row).includes('No balance'));
assert(!flat(row).includes('0% of'));
assert.equal(count(row,'bar2'),0);
let sheet,confirmation;
const X={h,fill:()=>{},icon,plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`,money,money2,ago:String,daysAgo,ia,
 tag:(x)=>h('i',{},x),pill:()=>h('i'),post:()=>{},HOOKS:{addOptions:[]},RGB:{},
 openSheet:content=>{sheet=h('div',{},content);},closeModal:()=>{},closeBtn:()=>h('button',{},'Close'),
 backBtn:()=>h('button'),toast:()=>{},modal:(title,content,actions)=>{confirmation={title,content,actions};},
 opt:()=>{},notCounted:()=>h('i'),leftOut,acctName,nameMask:()=>{},refresh:()=>{},openAdd:()=>{}};
global.window={HF_EXT:[]};
""" + alpaca + r"""
window.HF_EXT[0](X);
row=X.HOOKS.acctRow({account_id:'excluded',name:'Alpaca',current:1230,positions:2,as_of:'2026-10-21',api:true},'Alpaca','inst');
assert(flat(row).includes('Read-only'));
assert(flat(row).includes('API · 2 positions'));
assert(!flat(row).includes('Replace key'));
find(row,'help-trigger').attrs.onclick();
assert(flat(sheet).includes('Not counted in Custom'));
const controls=nodes(sheet).filter(e=>e.tag==='button');
controls.find(e=>flat(e)==='Replace key').attrs.onclick({stopPropagation:()=>{}});
assert(flat(sheet).includes('Replace the Alpaca key'));
find(row,'help-trigger').attrs.onclick();
nodes(sheet).find(e=>e.tag==='button' && flat(e)==='Remove').attrs.onclick({stopPropagation:()=>{}});
assert.equal(confirmation.title,'Remove Alpaca?');
assert(confirmation.actions.some(e=>e.label==='Remove'));
let request;
global.document={querySelector:()=>null};
global.fetch=async (url,options)=>{request={url,method:options.method}; return {ok:true,json:async ()=>({})};};
confirmation.actions.find(e=>e.label==='Remove').onclick().then(()=>{
  assert.deepEqual(request,{url:'/api/alpaca/key',method:'DELETE'});
  console.log('Accounts financial disclosures and actions passed');
});
"""
    result = subprocess.run([NODE, '-'], input=harness, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
