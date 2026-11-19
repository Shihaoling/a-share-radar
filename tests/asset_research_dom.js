'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const script=fs.readFileSync('web/asset/research.js','utf8');
const html=fs.readFileSync('web/asset/index.html','utf8');
function node(tag) {
  return {tag,children:[],dataset:{},textContent:'',hidden:false,
    append(...items){this.children.push(...items);},
    replaceChildren(frag){this.children=frag.children;},
    querySelectorAll(){return this.children.flatMap(child=>[...(child.tag==='details' && child.open?[child]:[]),...child.querySelectorAll()]);}};
}
const flatten=el=>el.textContent+' '+el.children.map(flatten).join(' ');
const sample=()=>({schema_version:1,source:'sxdata_asset_research',type:'stock',code:'000001.SZ',name:'平安银行',
  generated_at:'2026-11-19T11:00:20+08:00',quote_at:'2026-11-19T11:00:05+08:00',quote_basis:'live',price:11.12,change_pct:0,
  metrics:[{key:'amplitude_pct',label:'日内振幅',value:0,unit:'%',group:'intraday',window:'当日',sample_count:1,
    as_of:'2026-11-19T11:00:05+08:00',formula:'（最高价－最低价）÷ 昨收',description:'当日高低波动幅度'},
    {key:'beta60',label:'60日贝塔',value:null,unit:'',group:'daily',window:'60个交易日',
     sample_count:3,as_of:'2026-11-18',missing_reason:'配对日收益样本不足'}]});
async function mount(type,code,publication) {
  const nodes=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],node('div')]));
  const calls=[],charts=[],timers=new Map(),events={};let seq=0;
  const context=vm.createContext({Intl,URLSearchParams,AbortController,Date,
    window:{AssetChart:{mount:(host,data,options)=>charts.push({host,data,options})}},
    document:{getElementById:id=>nodes[id],hidden:false,createElement:node,createDocumentFragment:()=>node('fragment'),
      addEventListener:(name, action)=>{events[name]=action;}},
    location:{search:'?type='+type+'&code='+encodeURIComponent(code)},
    setTimeout(fn,ms){timers.set(++seq,{fn,ms});return seq;},clearTimeout(id){timers.delete(id);},
    fetch:async url=>{calls.push(url);return {ok:publication.status===200,status:publication.status,json:async()=>publication.data};}});
  vm.runInContext(script,context);
  const tick=async()=>{const first=timers.entries().next().value;assert.ok(first);timers.delete(first[0]);await first[1].fn();await new Promise(resolve=>setImmediate(resolve));};
  await new Promise(resolve=>setImmediate(resolve));
  return {nodes,calls,charts,tick,context,timers,events};
}
async function checks() {
  const doc={status:200,data:sample()},page=await mount('stock','000001.sz',doc),n=page.nodes;
  const text=()=>flatten(n['research-groups']);
  assert.deepEqual(page.calls,['../data/asset-research/stock/000001.SZ.json']);
  assert.equal(n['asset-title'].textContent,'平安银行');
  assert.equal(n['asset-price'].textContent,'11.12');assert.equal(n['asset-change'].textContent,'0.00%');
  assert.equal(n['asset-change-time'].hidden,true);assert.match(n['asset-common-time'].textContent,/盘中.*11:00:05/);
  assert.match(n['research-status'].textContent,/1 \/ 2/);assert.match(text(),/0.00.*%/);assert.match(text(),/计算说明/);
  assert.match(text(),/配对日收益样本不足/);assert.match(text(),/60个交易日/);assert.match(text(),/最高价/);
  assert.match(text(),/2026-11-18/);assert.match(n['research-date'].textContent,/11:00:20/);
  assert.equal(page.charts[0].host,n['asset-chart']);assert.equal(page.charts[0].data,undefined);
  assert.equal(page.charts.at(-1).options.key,'stock/000001.SZ');
  doc.data.chart={daily:[{date:'2026-11-18',close:11.12}]};
  const first=n['research-groups'].children[0];
  await page.tick();assert.equal(n['research-groups'].children[0],first);
  assert.deepEqual(page.charts.at(-1).data,doc.data.chart);
  const charts=page.charts.length;await page.tick();assert.equal(page.charts.length,charts);
  page.context.document.hidden=true;page.events.visibilitychange();assert.equal(page.timers.size,0);
  page.context.document.hidden=false;page.events.visibilitychange();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(n['research-groups'].children[0],first);
  const intervals=vm.runInContext("RadarResearch.refreshInterval",page.context);
  assert.equal(intervals(new Date('2026-11-19T09:31:00+08:00'),null),5000);
  assert.equal(intervals(new Date('2026-11-19T11:45:00+08:00'),null),60000);
  assert.equal(intervals(new Date('2026-11-21T14:00:00+08:00'),null),60000);
  assert.equal(intervals(new Date('2026-10-01T10:00:00+08:00'),{market_session_day:'2026-10-01',is_trading_day:false}),60000);
  doc.status=503; await page.tick(); assert.match(n['research-status'].textContent,/最近成功/); assert.match(text(),/日内振幅/);
  doc.status=200;doc.data={...sample(),type:'index'};await page.tick();assert.match(n['research-status'].textContent,/最近成功/);
  doc.data=sample();doc.data.change_at='2026-11-18';doc.data.metrics[1].value=-0.32;await page.tick();
  assert.match(n['research-status'].textContent,/2 \/ 2/);assert.match(text(),/-0.32/);assert.equal(n['asset-common-time'].hidden,true);assert.match(n['asset-change-time'].textContent,/收盘.*2026-11-18/);
  assert.equal((await mount('index','../../private',doc)).calls.length,0);
  for (const [kind,id] of [['index','000001.SH'],['stock','000001.SZ'],['stock','834683.NQ']]) {
    const p=await mount(kind,id,{status:404});
    assert.deepEqual(p.calls,['../data/asset-research/'+kind+'/'+id+'.json']);assert.match(p.nodes['research-status'].textContent,/暂无已发布/);
  }
  assert.equal((await mount('sector','000001.SH',doc)).calls.length,0);
}
checks().catch(err=>{console.error(err);process.exitCode=1;});
