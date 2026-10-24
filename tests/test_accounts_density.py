"""Approved D1–D5 Accounts layout guard. Synthetic long names, never a real database.

Discovered by check.py (Chrome()) and selected by its static JS/CSS tier mapping.
ACCOUNT_LAYOUT is also used by scripts/accounts_proof.py for reviewable screenshots.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

import gate_dbs
from cdp import Chrome, Server, find_chrome
from hermes_finance import db, insights, manual

CARD_NAME = 'BestBuy Citi Credit Card Platinum Rewards'
CAR_NAME = '2019 Zenith Mesa Hybrid XLE Premium'
BANK_NAME = 'International Community Financial Services Federal Credit Union'

# Measure real text fragments as well as boxes: a flex child can hide wrapping/clipping.
HEADER_LAYOUT = r"""(() => {
  const errors=[], headers=[...document.querySelectorAll('.acctgrp > h3')];
  let secondLineHeaders=0;
  const bad=(message,e)=>errors.push({message,text:e.textContent.trim()});
  for(const header of headers) {
    const title=header.querySelector('.ttl'), total=header.querySelector('small'),
      value=total.querySelector('.v'), chev=header.querySelector('.gfold .chev');
    const tr=title.getBoundingClientRect(), cr=chev.getBoundingClientRect(), hr=header.getBoundingClientRect();
    const pill=header.querySelector('.offtag');
    if(pill && !pill.hidden && pill.getClientRects().length) {
      secondLineHeaders++;
      const pr=pill.getBoundingClientRect(), strip=parseFloat(getComputedStyle(header).minHeight);
      if(pr.top<Math.max(tr.bottom,total.getBoundingClientRect().bottom,cr.bottom)-1)
        bad('Not counted pill is not on its own second line',header);
      // Match audit_layout.py's declared-strip contract for an intentional second line.
      if(!strip || Math.abs(hr.height-strip)>2)
        bad('second-line header height differs from declared min-height strip',header);
    } else {
      const firstRowHeight=Math.max(...[...header.children].filter(e=>e.getClientRects().length)
        .map(e=>e.getBoundingClientRect().height));
      if(Math.abs(hr.height-firstRowHeight)>2) bad('single-line header reserves extra height',header);
    }
    if(Math.abs(cr.top-tr.top)>5) bad('chevron is not on title line',header);
    if(!tr.width) bad('title has no space',header);
    for(const el of [total,value]) {
      const range=document.createRange(); range.selectNodeContents(el);
      // The optional Not counted pill may live on a second header line; the count/total may not.
      const rects=[...range.getClientRects()].filter(r=>r.width && ![...el.querySelectorAll('.offtag')]
        .some(p=>p.getBoundingClientRect().top===r.top));
      const tops=rects.map(r=>Math.round(r.top));
      if(new Set(tops).size>1) bad('header count/total wraps',el);
      if(getComputedStyle(el).textOverflow==='ellipsis' || rects.some(r=>r.left<hr.left-1 || r.right>hr.right+1))
        bad('header total ellipsized or outside header',el);
      if(el.clientWidth && el.scrollWidth>el.clientWidth+1) bad('header total clipped',el);
    }
    const vr=value.getBoundingClientRect();
    if(Math.abs(vr.top-tr.top)>5) bad('total is not on title line',header);
    if(cr.right>hr.right+1 || tr.right>total.getBoundingClientRect().left+1)
      bad('header items overlap or overflow',header);
    if(header.closest('.off') && getComputedStyle(value).color!==getComputedStyle(total).color)
      bad('excluded group total is not dim',value);
  }
  return {errors,headers:headers.length,secondLineHeaders};
})()"""


CONNECTION_LAYOUT = r"""(() => {
  const errors=[], rows=[...document.querySelectorAll('#page .conn, #sheet .duplist .conn')];
  const bad=(message,e)=>errors.push({message,text:e.textContent.trim()});
  for(const row of rows) {
    const name=row.querySelector('.grow > b'), status=row.querySelector('.grow > span');
    const ns=getComputedStyle(name), rs=getComputedStyle(row), nr=name.getBoundingClientRect(), rr=row.getBoundingClientRect();
    if(ns.whiteSpace!=='nowrap' || ns.textOverflow!=='ellipsis' || ns.overflowX!=='hidden')
      bad('connection name may wrap instead of ellipsize',name);
    if(nr.height>parseFloat(ns.lineHeight)+1) bad('connection name spans multiple lines',name);
    if(!name.title) bad('connection name lacks full-name title',name);
    const textHeight=parseFloat(ns.lineHeight)+(status?parseFloat(getComputedStyle(status).lineHeight):0);
    const coarse=matchMedia('(pointer: coarse)').matches;
    const max=Math.max(textHeight,coarse?44:0)+parseFloat(rs.paddingTop)+parseFloat(rs.paddingBottom)
      +parseFloat(rs.borderTopWidth)+parseFloat(rs.borderBottomWidth);
    if(rr.height>max+1) bad('connection exceeds two text lines plus padding',row);
    if(status && status.getBoundingClientRect().top<nr.bottom-1) bad('connection status is not below name',row);
    const update=row.querySelector('.upd');
    if(update) {
      const ur=update.getBoundingClientRect();
      if(ur.left<nr.right-1 || Math.abs(ur.right-rr.right)>1) bad('Update is not at right',row);
      if(coarse && ur.height<44) bad('Update coarse-pointer target below 44px',update);
    }
  }
  return {errors,rows:rows.length};
})()"""


def long_names_db(path, today=None):
    today = today or date.today()
    gate_dbs.build('features', path, today)
    conn = db.connect(path)
    try:
        with conn:
            conn.execute("UPDATE manual_accounts SET name = ? WHERE name = 'Best Buy'", (CARD_NAME,))
            conn.execute("UPDATE manual_accounts SET name = ? WHERE name = '2019 Zenith Mesa'", (CAR_NAME,))
            conn.execute("UPDATE accounts SET name = ? WHERE account_id = 'acc_nova_chk'", (BANK_NAME + ' Checking',))
            conn.execute("UPDATE items SET institution_name = ? WHERE item_id = 'demo_nova'", (BANK_NAME,))
            # Both zero-balance paths must say No balance and have no usage bar.
            conn.execute("UPDATE accounts SET current_balance = 0 WHERE account_id = 'acc_citi_cc'")
            conn.execute("UPDATE manual_accounts SET balance = 0 WHERE name = 'Sapphire'")
        for row in list(conn.execute("SELECT id FROM manual_accounts WHERE kind = 'property'")):
            manual.archive(conn, row['id'], today)
        excluded = insights.networth_excluded(conn) + [r[0] for r in conn.execute("SELECT account_id FROM accounts WHERE type = 'investment'")]
        excluded += [insights.manual_nw_id(r['id']) for r in manual.rows(conn) if r['kind'] in ('vehicle', 'investment')]
        # Alpaca is virtual, so include its ID along with investments to exercise an entirely excluded group.
        from hermes_finance import alpaca
        insights.save_networth_excluded(conn, [*excluded, alpaca.ACCOUNT_ID])
    finally:
        conn.close()
    return path


ACCOUNT_LAYOUT = r"""(() => {
  // The info button draws 24px but below 1024px wide its transparent ::before/::after hit boxes are 44px (app.css, tap.css). That
  // widens every ancestor's scrollWidth by design and is not text overflow, so it's off while we measure.
  const hit=document.createElement('style'); hit.textContent='.help-trigger::before,.help-trigger::after{display:none!important}';
  document.head.append(hit);
  try { return measure(); } finally { hit.remove(); }
  function measure() {
  const errors=[], rows=[...document.querySelectorAll('#page .acctgrp .li')];
  const visible=e=>e.getClientRects().length && !e.closest('[hidden]');
  const bad=(message, e)=>errors.push({message,text:e?.textContent.trim().slice(0,100)});
  if (!rows.length) bad('no account rows');
  if(document.documentElement.scrollWidth>innerWidth+1) bad('page scrolls horizontally');
  const ids=[];
  for(const row of rows) {
    ids.push(row.dataset.account);
    if(!row.matches('.r2')) bad('account does not use two-line layout',row);
    const cs=getComputedStyle(row), name=row.querySelector('.nm'), meta=row.querySelector('.meta2');
    const bar=row.querySelector('.track');
    const max=24+18+2+parseFloat(cs.paddingTop)+parseFloat(cs.paddingBottom)
      +parseFloat(cs.borderTopWidth)+parseFloat(cs.borderBottomWidth)+(bar?13:0);
    if(row.getBoundingClientRect().height>max+1) bad('more than two lines plus padding/bar',row);
    if(!name || !meta || !row.querySelector('.nmrow > .help-trigger')) bad('missing name/details/inline help',row);
    if(row.querySelector('.nmrow .mk, .nmrow .tag')) bad('mask or chip on name line',row);
    const amount=row.querySelector('.amt');
    if(amount && Math.abs(amount.getBoundingClientRect().top-name.getBoundingClientRect().top)>5)
      bad('amount is not on line one',row);
    for(const el of row.querySelectorAll('*')) {
      if(!visible(el)) continue;
      const s=getComputedStyle(el);
      if(s.display==='contents' || !el.clientWidth) continue;
      // Only names and line-two details may elide, with a real single-line ellipsis.
      const elides=el.matches('.nm,.mt') && s.textOverflow==='ellipsis' && s.whiteSpace==='nowrap' && s.overflowX==='hidden';
      if(el.scrollWidth>el.clientWidth+1 && !elides) bad('horizontal text overflow: '+el.className,el);
      if(el.matches('.nm,.mt') && !elides) bad('name/details may wrap instead of ellipsize',el);
    }
    if(meta?.textContent.includes('No balance') && (bar || /0% of/.test(meta.textContent))) bad('zero card has utilization',row);
    if(row.dataset.account?.startsWith('manual:') && !meta?.querySelector('.chip2')) bad('manual chip missing',row);
  }
  if(rows.filter(e=>e.querySelector('.meta2')?.textContent.includes('No balance')).length!==2) bad('both zero-balance fixture cards must be present');
  if(rows.filter(e=>e.dataset.account?.startsWith('manual:')).length!==7) bad('manual fixture account missing or duplicated');
  const manualIds=[...document.querySelectorAll('#page [data-edit],#page [data-loan],#page [data-asset]')]
    .map(e=>e.dataset.edit || e.dataset.loan || e.dataset.asset);
  if(new Set(manualIds).size!==manualIds.length) bad('manual item appears twice anywhere on the page');
  if(ids.some(id=>!id) || new Set(ids).size!==ids.length) bad('manual/account appears twice or has no identity');
  if([...document.querySelectorAll('#page h3 .ttl')].some(e=>e.textContent==='Added by you')) bad('Added by you card');
  if(document.querySelector('#page .acctgrp .ghost-row, #page .emptyg .ghost-row')) bad('empty section has dashed add box');
  if(!document.querySelector('#page [data-empty-group=property] .addbtn')) bad('compact empty Property missing');
  if(document.querySelectorAll('#page .ghost-row[data-add=home]').length!==1) bad('bottom add chooser row missing/duplicated');
  return {errors,rows:rows.length,ids,height:document.documentElement.scrollHeight};
  }
})()"""


@pytest.fixture(scope='module')
def density_site(tmp_path_factory):
    path=long_names_db(tmp_path_factory.mktemp('accounts-density')/'synthetic.db')
    site=Server(path, today=date.today())
    try:
        yield site
    finally:
        site.close()


@pytest.fixture(scope='module')
def density_browser():
    with Chrome() as b:
        yield b


def open_accounts(b, site, width=390, theme='dark', mode='custom'):
    b.size(width,844 if width==390 else 900)
    b.goto(site.url+'/#/accounts')
    b.js(f"localStorage.setItem('hf.networth.mode',{json.dumps(mode)}); localStorage.removeItem('hf.accounts.shut')")
    b.goto(site.url+f'/?density={width}-{theme}-{mode}#/accounts')
    b.js(f'HFTheme.set({json.dumps(theme)})')
    b.settled(force=True)


browser=pytest.mark.skipif(find_chrome() is None,reason='Chrome is not installed')


@browser
@pytest.mark.parametrize('width',[390,1280])
@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('mode',['all','custom'])
def test_accounts_two_lines(density_browser,density_site,width,theme,mode):
    b=density_browser
    open_accounts(b,density_site,width,theme,mode)
    result=b.js(ACCOUNT_LAYOUT)
    assert result['rows']>=12, result
    assert not result['errors'], result
    assert b.js("document.documentElement.dataset.nw")==mode
    # Investments and Vehicles are fully out; Cash remains partly counted as on the features fixture.
    for group in ('investments','vehicles'):
        assert b.js(f"document.querySelector('.acctgrp[data-fold={group}]').classList.contains('off')")== (mode=='custom')
    assert b.js("document.querySelector('.acctgrp[data-fold=cash] .offtag').hidden")
    assert b.problems()==[]


@browser
@pytest.mark.parametrize('width',[390,1280])
@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('mode',['all','custom'])
def test_accounts_headers_and_connections(density_browser,density_site,width,theme,mode):
    b=density_browser
    open_accounts(b,density_site,width,theme,mode)
    for stress in (False,True):
        if stress:
            # DOM-only label/total stress: no taxonomy or financial fixture semantics changed.
            b.js("""const h=document.querySelector('.acctgrp[data-fold=investments] > h3');
              h.querySelector('.ttl').textContent='Investments with an exceptionally long display label';
              h.querySelector('small .v').textContent='$1,234,567.89';""")
        result=b.js(HEADER_LAYOUT)
        assert result['headers']>=4,result
        assert (result['secondLineHeaders']>0)==(mode=='custom'),result
        assert not result['errors'],result
    result=b.js(CONNECTION_LAYOUT)
    assert result['rows']>=2,result
    assert not result['errors'],result
    assert b.js(f"[...document.querySelectorAll('#page .conn .grow > b')].some(e=>e.title==={json.dumps(BANK_NAME)})")
    assert b.problems()==[]


@browser
@pytest.mark.parametrize('theme',['light','dark'])
def test_link_sheet_connection_density(density_browser,density_site,theme):
    b=density_browser
    # Touch emulation makes the coarse-pointer Update target measurable as well.
    b.call('Emulation.setTouchEmulationEnabled',enabled=True)
    try:
        open_accounts(b,density_site,390,theme)
        assert b.js("matchMedia('(pointer: coarse)').matches")
        b.js("document.querySelector('#page .ghost-row[data-add=home]').click()")
        b.js("[...document.querySelectorAll('#sheet .opt')].find(e=>e.textContent.includes('Link a bank')).click()")
        b.settled(force=True)
        assert 'Already connected' in b.js("document.querySelector('#sheet').innerText")
        result=b.js(CONNECTION_LAYOUT)
        assert result['rows']>=4,result
        assert not result['errors'],result
        assert b.js(f"[...document.querySelectorAll('#sheet .duplist .grow > b')].some(e=>e.title==={json.dumps(BANK_NAME)})")
        assert b.js("[...document.querySelectorAll('#sheet .duplist .upd')].every(e=>e.textContent==='Update')")
        assert b.problems()==[]
    finally:
        b.call('Emulation.setTouchEmulationEnabled',enabled=False)


@browser
def test_accounts_disclosures_and_add_actions(density_browser,density_site):
    b=density_browser
    open_accounts(b,density_site)
    b.js("document.querySelector('#page [data-api=alpaca] .help-trigger').click()")
    text=b.js("document.querySelector('#sheet').innerText")
    assert all(x in text for x in ('Alpaca details','2 positions','Not counted in Custom','Replace key','Remove')),text
    b.js("[...document.querySelectorAll('#sheet .apictl button')].find(e=>e.textContent==='Replace key').click()")
    assert b.js("document.querySelector('#sheet h2').textContent")=='Replace the Alpaca key'
    b.js("document.querySelector('#sheet [aria-label=Close]').click()")
    b.js("document.querySelector('#page [data-api=alpaca] .help-trigger').click()")
    b.js("[...document.querySelectorAll('#sheet .apictl button')].find(e=>e.textContent==='Remove').click()")
    assert b.js("document.querySelector('#sheet h2').textContent")=='Remove Alpaca?'
    # Intercept the destructive request locally: verify the sheet action without a provider or key file.
    b.js("""window.__removed=[]; const original=window.fetch; window.fetch=(...args)=>{
      if(args[0]==='/api/alpaca/key' && args[1]?.method==='DELETE') {
        __removed.push(args[1].method); return Promise.resolve(new Response('{}',{status:200}));
      } return original(...args); };""")
    b.js("[...document.querySelectorAll('#sheet button')].find(e=>e.textContent==='Remove').click()")
    b.settle()
    assert b.js('__removed')==['DELETE']
    b.js(f"[...document.querySelectorAll('#page .account-entry')].find(e=>e.textContent.includes({json.dumps(CARD_NAME)})).querySelector('.help-trigger').click()")
    text=b.js("document.querySelector('.help-panel').innerText")
    assert all(x in text for x in ('Credit limit','$4,000','last set','matched payment','Purchases','Updated')),text
    b.js("document.querySelector('.help-panel [aria-label=\"Close details\"]').click()")
    b.js("document.querySelector('[data-empty-group=property] [data-add]').click()")
    assert 'property' in b.js("document.querySelector('#sheet h2').textContent").lower()
    b.js("document.querySelector('#sheet [aria-label=Close]').click()")
    b.js("document.querySelector('#page .ghost-row[data-add=home]').click()")
    text=b.js("document.querySelector('#sheet').innerText")
    assert 'Add a property' in text and 'Add a vehicle' in text and 'Add manually' in text,text
    b.js("[...document.querySelectorAll('#sheet .opt')].find(e=>e.textContent.includes('Add manually')).click()")
    text=b.js("document.querySelector('#sheet').innerText").lower()
    assert 'loan' in text and 'card' in text,text
    assert b.problems()==[]
