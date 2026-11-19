'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
class Element {
  constructor(){this.children=[];this.attrs={};this.events={};this.dataset={};this.hidden=false;this.textContent='';this.value='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  addEventListener(k,fn){this.events[k]=fn;}
  setAttribute(k,v){this.attrs[k]=v;}
  fire(k){return this.events[k]({preventDefault(){}});}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const words=n=>[n.textContent,...n.children.map(words)].join(' ');
function screen(dir,handler,search=''){
  const html=fs.readFileSync('web/'+dir+'/index.html','utf8'),nodes={};
  for(const m of html.matchAll(/<[^<>]*\bid="([^"]+)"[^<>]*>/g)){
    const n=new Element();n.hidden=/\shidden(?:\s|>)/.test(m[0]);nodes[m[1]]=n;
  }
  const urls=[],redirects=[];
  const doc={createElement:()=>new Element(),getElementById:id=>{assert.ok(nodes[id],id);return nodes[id];},addEventListener(){},hidden:false};
  const fetch=async(path,opts)=>{
    urls.push([path,opts]);const [status,payload]=await handler(path,opts);
    return {status, ok:status>=200 && status<300,json:async()=>payload};
  };
  vm.runInNewContext(fs.readFileSync('web/'+dir+'/'+dir+'.js','utf8'),{document:doc,fetch,
    location:{search,replace:url=>redirects.push(url)},setInterval(){},URLSearchParams,AbortSignal,Intl,Date});
  return {nodes,urls,redirects};
}
const guest={user:null,csrf_token:'visitor-csrf'};
const admin={user:{username:'manager',role:'admin'},csrf_token:'admin-csrf'};
const member={user:{username:'member',role:'user'},csrf_token:'member-csrf'};
async function run(){
  let active=guest;
  const account=screen('account',(path,opts)=>{
    if(path.endsWith('/session'))return [200,active];
    const isRegister=path.endsWith('/register');
    assert.equal(opts.headers['X-CSRF-Token'],active.csrf_token);
    assert.equal(opts.method,'POST');
    if(isRegister)assert.deepEqual(JSON.parse(opts.body),{username:'member',password:' test 12 3456 '});
    active=isRegister?member:guest;return [200,active];
  },'?next=https://other.example/');
  await tick();
  const n=account.nodes;
  assert.equal(n.guest.hidden,false);assert.equal(n.member.hidden,true);
  n['register-tab'].fire('click');assert.equal(n.password.attrs.autocomplete,'new-password');
  n.username.value=' member ';n.password.value=' test 12 3456 ';
  n.credentials.fire('submit');await tick();
  assert.equal(n.member.hidden,false);assert.equal(n['admin-link'].hidden,true);assert.equal(n.password.value,'');
  assert.equal(n.name.textContent,'member');assert.deepEqual(account.redirects,[]);
  n.logout.fire('click');await tick();assert.equal(n.guest.hidden,false);
  const redirect=screen('account',()=>[200,admin],'?next=/admin/');await tick();
  assert.deepEqual(redirect.redirects,['/admin/']);
  for(const [user,expected] of [[guest,['/account/?next=/admin/']],[member,[]]]){
    const rejected=screen('admin',()=>[200,user]);await tick();
    assert.equal(rejected.urls.length,1);assert.equal(rejected.nodes.results.hidden,true);assert.deepEqual(rejected.redirects,expected);
  }
  const p={minutes:12,requests:6,success:5,failed:1,avg_requests_per_minute:.5,usage_responses:1,input_tokens:0,output_tokens:41,total_tokens:41};
  const empty={minutes:0,requests:null,avg_requests_per_minute:null,usage_responses:0};
  const history={requests:2,usage_responses:1,input_tokens:9,output_tokens:11,total_tokens:20};
  const snapshot={schema_version:1,as_of:'2026-11-19T15:00:00+08:00',coverage:{since:'2026-11-18T15:00:00+08:00',through:'2026-11-19T15:00:00+08:00',note:'只计完整分钟'},
    relay:{periods:{trading:p,other:empty},rolling_60s:0,limit_per_minute:100,cumulative:{requests:123}},
    minimax:{periods:{trading:p,other:empty},historical:{news_analysis:history,training_labels:history}},
    sources:[{id:'relay',label:'SXData',status:'cooling_down',expires_at:'2026-12-20T22:06:00+08:00'}]};
  snapshot.relay.apis={index_daily:{starts:123,periods:{trading:p,other:empty}}};
  snapshot.minimax.scopes={training_labels:{...p,periods:{trading:p,other:empty}}};
  snapshot.minimax.observed={...p,since:'2026-11-19T10:00:00+08:00'};
  let usageOk=true,limit=100,session=admin,blocked=false;
  const board=screen('admin',(path,opts)=>{
    if(path.endsWith('/session'))return [200,session];
    if(path.endsWith('/usage'))return usageOk?[200,snapshot]:[503,{message:'离线'}];
    assert.equal(path,'/api/admin/policy');
    if(opts.method==='POST'){
      assert.equal(opts.headers['X-CSRF-Token'],admin.csrf_token);
      if(blocked)return [503,{}];limit=JSON.parse(opts.body).limit_60s;
    }
    return [200,{limit_60s:limit,maximum:100,expires_at:'2026-12-20T22:06:00+08:00'}];
  });
  await tick();
  const b=board.nodes;
  assert.equal(b.results.hidden,false);assert.equal(b['relay-now'].textContent,'0');
  assert.equal(b['model-tokens'].textContent,'40');assert.equal(b['model-count'].textContent,'4');
  assert.match(words(b.periods),/0\.5/);assert.match(words(b.sources),/冷却中/);assert.match(words(b.sources),/22:06:00/);
  assert.match(words(b['relay-apis']),/index_daily.*123/);assert.match(words(b['model-scopes']),/持续计量合计.*训练数据打标.*历史资讯分析.*历史训练打标/);
  const historyTraining=b['model-scopes'].children.find(n=>n.children[0].textContent==='历史训练打标');
  assert.deepEqual(historyTraining.children.slice(-5).map(cell=>cell.textContent),['9','11','20','—','—']);
  const usageRequests=()=>board.urls.filter(([url,opts])=>url.endsWith('/policy') && opts.method==='POST').length;
  b['policy-limit'].value='101';await b['policy-form'].fire('submit');assert.equal(usageRequests(),0);
  b['policy-limit'].value='45';b['policy-limit'].fire('input');await b.refresh.fire('click');
  assert.equal(b['policy-limit'].value,'45');
  await b['policy-form'].fire('submit');assert.equal(limit,45);assert.match(b['policy-state'].textContent,/当前上限 45/);
  blocked=true;b['policy-limit'].value='23';await b['policy-form'].fire('submit');assert.equal(limit,45);assert.equal(b['policy-state'].dataset.error,'true');
  usageOk=false;await b.refresh.fire('click');assert.equal(b['relay-total'].textContent,'123');assert.match(b.status.textContent,/上次/);
  usageOk=true;snapshot.sources=[];snapshot.minimax.historical={};await b.refresh.fire('click');
  assert.match(words(b.sources),/暂无/);assert.equal(b['model-tokens'].textContent,'—');assert.equal(b['model-count'].textContent,'—');
  session=guest;await b.refresh.fire('click');assert.equal(b.results.hidden,true);assert.equal(b['policy-section'].hidden,true);assert.equal(b['relay-total'].textContent,'—');
  assert.deepEqual(board.redirects,['/account/?next=/admin/']);
  console.log('Account and administration DOM checks passed');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
