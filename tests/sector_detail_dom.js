'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const js = fs.readFileSync('web/sector/sector.js', 'utf8');
const html = fs.readFileSync('web/sector/index.html', 'utf8');
const catalog = JSON.parse(fs.readFileSync('web/data/ths-sector-catalog.json', 'utf8'));
const exchangeCatalog = JSON.parse(fs.readFileSync('web/data/sector-catalog.json', 'utf8'));
const hotCode = '700051R.TI', hotDate = '2026-11-19';
const quote = data => ({schema_version:1, ...data});
const response = (payload, status = 200) => ({ok:status >= 200 && status < 300, status, json:async () => payload});
const rows = Array.from({length:65}, (_,i) => ({name:'样本' + i,
  code:String(600000 + i) + '.SH', weight_pct:i === 0 ? 0.00345678 : null}));
function node() {
  return {textContent:'', value:'', hidden:false, children:[], events:{},
    addEventListener(name, fn) { this.events[name] = fn; },
    setAttribute(name, value) { this[name] = value; },
    append(child) { this.children.push(child); },
    replaceChildren(fragment) { this.children = fragment.children; }};
}
async function mount(code, publications) {
  const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], node()]));
  const calls = [], timers = new Map();
  let onData;
  let seq = 0;
  const context = vm.createContext({Intl, AbortController, URLSearchParams,
    RadarResearch:{mount(type, symbol, standalone, listener) {onData=listener;}},
    setTimeout(fn, ms) { timers.set(++seq, {fn,ms}); return seq; }, clearTimeout(id) {timers.delete(id);},
    location:{search:'?code=' + encodeURIComponent(code)}, fetch:async url => {
      calls.push(url);
      const result = publications[url];
      if (result instanceof Error) throw result;
      return result || response({},404);
    }});
  vm.runInContext(js, context);
  context.document = {hidden:false, getElementById:id => nodes[id], createElement:node, createDocumentFragment:node};
  await vm.runInContext('openDetail()', context);
  await new Promise(resolve => setImmediate(resolve));
  async function tick(ms) {
    const active = [...timers].find(([,timer]) => timer.ms === ms);
    assert.ok(active, 'Scheduled retry ' + ms);
    timers.delete(active[0]);
    await active[1].fn();
    await new Promise(resolve => setImmediate(resolve));
  }
  return {nodes, calls, tick, onData};
}
function members(code, status, list, extra = {}) {
  return response(quote({source:'sxdata_sector_members', code, status, members:list, as_of:null,
    method:'ths_member', checked_at:'2026-11-19T12:20:00+08:00', ...extra}), status === 'pending' ? 202 : 200);
}
async function checks() {
  const api = {'../data/ths-sector-catalog.json':response(catalog), '../data/sector-catalog.json':response(exchangeCatalog)};
  for (const code of ['../../../input.SI', '000000.TI', '700051R.SH']) {
    const page = await mount(code, api);
    assert.equal(page.calls.some(url => url.includes('sector-members/')), false);
    assert.match(page.nodes['catalog-status'].textContent, /无效|未在/);
  }
  api['../data/sector-hot.json'] = response(quote({source:'sxdata_ths_hot', trade_date:hotDate,
    rank_time:hotDate + 'T10:30:00+08:00', items:[{code:hotCode, price:0, change_pct:2.01}]}));
  api['../data/sector-reference.json'] = response(quote({source:'sxdata_sector_reference',
    quotes:{[hotCode]:{quote_date:'2026-11-18', price:1200.25, change_pct:-3.22}}}));
  const file = '../data/sector-members/' + hotCode + '.json';
  api[file] = members(hotCode, 'pending', []);
  const {nodes:n, tick, onData} = await mount(hotCode, api);
  assert.equal(n['sector-title'].textContent, '同花顺金仓30全收益');
  assert.match(n['quote-price'].textContent, /1,200\.25/);
  assert.match(n['quote-price-time'].textContent, /2026-11-18 收盘/);
  assert.equal(n['quote-change'].textContent, '+2.01%');
  assert.match(n['quote-change-time'].textContent, /2026-11-19 10:30:00/);
  assert.equal(n['quote-change-label'].textContent, '热榜涨跌幅');
  assert.equal(n['quote-price-label'].textContent, '收盘点位');
  const at=hotDate+'T13:05:00+08:00', first=hotDate+'T13:04:00+08:00';
  const waiting={status:'partial',quoted_count:1,total_count:65,required_count:52,change_pct:null,quote_at:at,
    members:[{code:rows[0].code,quote_at:at,change_pct:-0.23,price:10}]};
  onData({constituent_live:waiting},false);
  assert.match(n['constituent-status'].textContent,/52 只/);
  assert.equal(n['constituent-coverage'].textContent,'1 / 65 只');
  assert.equal(n['constituent-change'].textContent,'—');
  onData({constituent_live:{...waiting,roster_complete:false,total_count:1}},false);
  assert.match(n['constituent-status'].textContent,/补齐成分名单/);
  assert.equal(n['constituent-change'].textContent,'—');
  assert.match(n['member-status'].textContent, /补采/);
  api[file] = members(hotCode, 'partial', rows.slice(0,2));
  await tick(15000);
  assert.equal(n['member-rows'].children.length, 2);
  const cells=n['member-rows'].children[0].children;
  assert.equal(cells[3].textContent,'10.00');
  assert.equal(cells[4].textContent,'-0.23%');
  assert.equal(cells[5].textContent,'2026-11-19 13:05:00');
  assert.equal(n['member-rows'].children[1].children[4].textContent,'—');
  const ready={...waiting,status:'ready',quoted_count:60,oldest_at:first,change_pct:0};
  onData({constituent_live:ready},false);
  assert.equal(n['constituent-change'].textContent,'0.00%');
  assert.match(n['constituent-time'].textContent,/13:04:00 至 13:05:00/);
  assert.equal(n['quote-change'].textContent,'+2.01%');
  onData({constituent_live:ready},true);
  assert.match(n['constituent-status'].textContent,/保留/);
  assert.equal(n['constituent-change'].textContent,'0.00%');
  onData({constituent_live:{...ready,status:'stale',members:[],current_quoted_count:0}},false);
  assert.match(n['constituent-status'].textContent,/本轮已取 0 只/);
  assert.equal(cells[4].textContent,'—');
  onData({constituent_live:{...waiting,status:'waiting',quoted_count:0,quote_at:null}},false);
  assert.equal(n['constituent-change'].textContent,'—');
  assert.equal(n['constituent-time'].textContent,'');
  assert.match(n['member-source'].textContent, /来源未提供成分生效日.*核验时间 2026-11-19/);
  api[file] = response({}, 503);
  await tick(30000);
  assert.equal(n['member-rows'].children.length, 2);
  api[file] = members(hotCode, 'ready', rows, {expected_count:65});
  // Members and quotes both have one 60s timer at this point.
  await tick(60000);
  if (n['member-rows'].children.length === 2) await tick(60000);
  assert.equal(n['member-rows'].children.length, 30);
  assert.equal(n['member-rows'].children[0].children[2].textContent, '0.00345678%');
  assert.equal(n['member-rows'].children[1].children[2].textContent, '—');
  n.next.events.click(); n.next.events.click();
  assert.equal(n['member-rows'].children.length, 5);
  assert.equal(n.next.disabled, true);
  n['member-search'].value = '600031.sh';
  n['member-search'].events.input();
  const link=n['member-rows'].children[0].children[0].children[0];
  assert.equal(link.textContent, '样本31');
  assert.equal(link.href, '../asset/?type=stock&code=600031.SH');
  n['member-search'].value = '无匹配';
  n['member-search'].events.input();
  assert.equal(n['table-wrap'].hidden, true);
  assert.match(n['member-status'].textContent, /没有匹配/);
  const si = '801010.SI';
  api['../data/daily-market.json'] = response(quote({source:'sxdata_daily',
    bars:{[si]:{'2026-11-18':{close:1234.56,change_pct:-1}}}}));
  api['../data/stock-quotes.json'] = response(quote({source:'sxdata_relay',
    board_estimates:[{id:si, quote_date:hotDate, quote_timestamp:hotDate + 'T11:30:00+08:00', change_pct:-0.75}]}));
  const sw = await mount(si, api);
  assert.match(sw.nodes['quote-price-time'].textContent, /申万行业.*2026-11-18/);
  assert.match(sw.nodes['quote-change-time'].textContent, /权重股估算.*11:30:00/);
  api['../data/sector-quotes.json'] = response(quote({source:'sxdata_relay', sectors:[{id:'000004.SH', quote_date:hotDate,
    quote_timestamp:hotDate + 'T11:29:57+08:00', price:6000.12, change_pct:0}]}));
  const sh = await mount('000004.SH',api);
  assert.match(sh.nodes['quote-price'].textContent, /6,000\.12/);
  assert.equal(sh.nodes['quote-change'].textContent, '0.00%');
  api['../data/sector-quotes.json'] = response(quote({source:'sxdata_relay', sectors:{invalid:true}}));
  await sh.tick(20000);
  assert.equal(sh.nodes['quote-change'].textContent, '0.00%');
}
checks().catch(error => { console.error(error); process.exitCode=1; });
