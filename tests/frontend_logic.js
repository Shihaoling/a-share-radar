// No browser or third-party dependency: exercise display invariants using a small DOM double.
function runFrontendChecks(source) {
  class TestNode {
    constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.events = {}; }
    append(...children) { this.children.push(...children); }
    setAttribute(key, value) { this.attrs[key] = value; }
    addEventListener(name, fn) { this.events[name] = fn; }
    replaceChildren(...children) { this.children = children; }
    remove() {}
    showModal() { this.opened = true; }
    close() { this.events.close?.(); }
  }
  const root = new TestNode('root');
  const doc = {body: new TestNode('body'), createElement: tag => new TestNode(tag),
    createElementNS: (_, tag) => new TestNode(tag),
    createTextNode: text => { const node = new TestNode('text'); node.textContent = String(text); return node; },
    querySelector: () => root, getElementById: () => null};
  const fakeFetch = () => ({then() { return this; }, catch() { return this; }});
  const bootstrapRemoved = source.replace(/^refresh\(\);$/m, '');
  const api = new Function('Node', 'document', 'localStorage', 'window', 'location', 'history', 'setInterval', 'fetch', bootstrapRemoved + '\nreturn {displayQuote,sectorQuote,normalizeDashboard,quantItem,sentimentLabel,newsTone,openMetricHelp,render,state,baseData,pages};')(
    TestNode, doc, {getItem() {return null;}, setItem() {}}, {addEventListener() {}, scrollTo() {}, dispatchEvent() {}},
    {hash: ''}, {replaceState() {}}, () => {}, fakeFetch);
  let passed = 0;
  const failures = [];
  function check(name, fn) { try { fn(); passed++; } catch (err) { failures.push({name, message: err.message}); } }
  function eq(actual, expected) { if (actual !== expected) throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`); }
  function ok(value, message) { if (!value) throw new Error(message || 'assertion failed'); }
  const text = node => (node.textContent || '') + (node.children || []).map(text).join(' ');
  const now = new Date('2026-11-16T16:00:00+08:00');
  const daily = (day, price) => ({price, as_of: day + 'T15:00:00+08:00', quote_date: day, timestamp_quality: 'daily_close', change_pct: 1, provider: 'stock_api'});
  const live = (time, price, quality = 'exchange_timestamp') => ({price, quote_timestamp: '2026-11-16T' + time + '+08:00', timestamp_quality: quality, provider: 'stock_api'});
  check('new source-timestamp quote outranks an old daily close', () => {
    const q = api.displayQuote({...daily('2026-11-13', 3000), live_quote: live('15:05:00', 3100)}, now);
    eq(q.price, 3100); eq(q.label, '今日行情 · 待最终日线'); eq(q.origin, 'live');
  });
  check('confirmed same-day closing bar outranks an earlier intraday quote', () => {
    const q = api.displayQuote({...daily('2026-11-13', 3000), daily_quote: daily('2026-11-16', 3110), live_quote: live('14:59:00', 3100)}, now);
    eq(q.price, 3110); eq(q.label, '今日收盘'); ok(q.has_today_daily);
  });
  check('confirmed same-day close wins after close even over a later quote', () => {
    const q = api.displayQuote({...daily('2026-11-16', 3110), daily_quote: daily('2026-11-16', 3110), live_quote: live('15:05:00', 3112)}, now);
    eq(q.price, 3110); eq(q.label, '今日收盘');
  });
  check('retrieval time cannot displace a confirmed market time', () => {
    const q = api.displayQuote({...daily('2026-11-13', 3000), live_quote: live('15:50:00', 4000, 'retrieved_only')}, now);
    eq(q.price, 3000); ok(q.label.includes('2026-11-13'));
  });
  check('future quotes are rejected', () => {
    const entity = {...daily('2026-11-13', 3000), live_quote: {...live('16:30:00', 4100)}};
    eq(api.displayQuote(entity, now).price, 3000);
  });
  check('new live quote does not inherit old daily return', () => {
    const q = api.displayQuote({...daily('2026-11-13', 3000), change_pct: 9.8, live_quote: live('14:59:00', 3100)}, now);
    eq(q.change_pct, null);
  });
  check('current primary quote is not overwritten by an older attached quote', () => {
    const q = api.displayQuote({...live('15:05:00', 3112), daily_quote: daily('2026-11-13', 3000), live_quote: live('14:55:00', 3100)}, now);
    eq(q.price, 3112);
  });
  check('missing explicit source time is labeled unconfirmed', () => {
    const q = api.displayQuote({price: 10, as_of:'2026-11-16T16:00:00+08:00', timestamp_quality:'retrieved_only'}, now);
    eq(q.label, '时点未确认');
  });
  check('history fallback retains its historical date', () => {
    const q = api.displayQuote({history:[{date:'2026-11-13', close:3000}]}, now);
    eq(q.price,3000); eq(q.label,'2026-11-13 收盘');
  });
  check('canonical SW2021 sector list does not mix legacy categories', () => {
    const d=api.normalizeDashboard({...api.baseData(),sectors:[{id:'legacy',name:'概念'}, {id:'801010.SI',taxonomy:'SW2021',level:'L1'}, {id:'801010.SI',taxonomy:'SW2021',level:'L1'}]});
    eq(d.sectors.length,1);eq(d.sectors[0].id,'801010.SI');
  });
  check('published vendor PE is not renamed TTM and keeps its independent date', () => {
    const m=api.quantItem({vendor_pe:18.5,vendor_valuation_date:'2026-11-13',as_of:'2026-11-16T15:05:00+08:00'},'pe_ttm');
    eq(m.value,18.5);eq(m.as_of,'2026-11-13');ok(m.label.includes('未声明 TTM'));ok(m.vendor);
  });
  check('explicit vendor TTM keeps its declared distinction', () => {
    const m=api.quantItem({vendor_pe_ttm:12.5,vendor_valuation_date:'2026-11-13'},'pe_ttm');
    eq(m.label,'供应商 PE (TTM)');
  });
  check('zero news coverage cannot be displayed as neutral', () => {
    eq(api.sentimentLabel({label:'neutral',article_count:0}), '暂无覆盖');
    eq(api.sentimentLabel({label:'neutral',article_count:2}), '中性');
  });
  const d={...api.baseData(),generated_at:'2026-11-16T16:00:00+08:00',market:Array.from({length:6},(_,i)=>({...daily('2026-11-13',3000+i),id:'index'+i,name:'测试指数'+i})),sectors:[
    {id:'801010.SI',name:'农林牧渔',taxonomy:'SW2021',level:'L1',group:'消费',...daily('2026-11-13',2000),stocks:[],history:[]},
    {id:'801080.SI',name:'电子',keywords:['半导体'],taxonomy:'SW2021',level:'L1',group:'科技',...daily('2026-11-13',3000),stocks:[],history:[],sentiment:{label:'negative',score:-.3,article_count:2,source_count:2,confidence:.6,confidence_kind:'model_softmax_uncalibrated',method:'finbert',evidence:[{title:'<script>alert(1)</script>',url:'javascript:alert(1)',source:'测试来源'}]}}
  ]};
  d.stock_api_state={endpoints:{index_daily:{status:'success',last_success:'2026-11-16T16:00:00+08:00'},rt_idx_k:{status:'cooldown',error_code:'api_denied',cooldown_until:'2026-11-17T16:00:00+08:00'}}};
  api.state.data=d;
  check('all six market indices are rendered',()=>{api.state.page='overview';api.render();ok(text(root).includes('测试指数5'));});
  check('filtered list and active detail remain aligned',()=>{api.state.page='sectors';api.state.selected='801010.SI';api.state.search='半导体';api.render();eq(api.state.selected,'801080.SI');});
  check('no result clears selected sector',()=>{api.state.search='不存在的行业';api.render();eq(api.state.selected,null);});
  check('entity evidence uses safe text and never creates script nodes',()=>{api.state.search='';api.state.selected='801080.SI';api.render();const scripts=[];function walk(n){if(n.tag==='script')scripts.push(n);(n.children||[]).forEach(walk);}walk(root);eq(scripts.length,0);ok(text(root).includes('<script>alert(1)</script>'));});
  for(const metric of ['index','intraday','cap'])for(const [page] of api.pages)check(`render ${page} in ${metric} mode`,()=>{api.state.metric=metric;api.state.page=page;api.render();});
  check('endpoint permissions distinguish successful daily data from denied real-time access',()=>{api.state.page='settings';api.render();const content=text(root);ok(content.includes('已取得数据'));ok(content.includes('当前权限不可用'));ok(content.includes('市场指数快照'));});
  check('active model status is displayed as available',()=>{api.state.data={...d,models:{status:'active'}};api.state.page='forecast';api.render();ok(text(root).includes('模型已准备'));api.state.data=d;});
  check('ready dashboard status illuminates market status',()=>{api.state.data={...d,status:'ready'};api.state.page='overview';api.render();const nodes=[];function walk(n){if(String(n.className||'').includes('status-dot live'))nodes.push(n);(n.children||[]).forEach(walk);}walk(root);ok(nodes.length>0);api.state.data=d;});
  check('per-article model tone is distinguished from directional impact',()=>{const node=api.newsTone({sentiment:'negative',sentiment_method:'finbert_chinese_cpu',impact:'positive'});const output=text(node);ok(output.includes('文本语气'));ok(output.includes('偏负面'));ok(output.includes('金融文本模型'));ok(!output.includes('潜在利好'));});
  check('per-article fallback rules are explicitly identified',()=>{const output=text(api.newsTone({sentiment:'positive',sentiment_method:'financial-tone-rules-v2'}));ok(output.includes('偏正面'));ok(output.includes('规则分析'));});
  check('per-article missing sentiment remains unknown',()=>{ok(text(api.newsTone({})).includes('暂无覆盖'));});
  check('stock rank basis is used when the sector rank basis is absent',()=>{const copy=JSON.parse(JSON.stringify(d));copy.sectors[0].stocks=[{code:'600000',name:'测试股票',price:10,as_of:'2026-11-13',timestamp_quality:'daily_close',rank_basis:'total_market_cap'}];api.state.data=copy;api.state.page='sectors';api.state.search='';api.state.selected=copy.sectors[0].id;api.render();ok(text(root).includes('按已获取总市值排序'));api.state.data=d;});
  check('vendor help states published methodology and independent date',()=>{api.openMetricHelp({name:'测试行业',vendor_pe:18.5,vendor_valuation_date:'2026-11-13'},'pe_ttm');const content=text(doc.body.children.at(-1));ok(content.includes('未声明 TTM'));ok(content.includes('2026-11-13'));ok(content.includes('未在本网站用归母利润'));});
  for(const key of ['pe_ttm','pb','sharpe'])check(`metric help ${key} handles missing data`,()=>api.openMetricHelp(d.sectors[0],key));
  return {passed, failures};
}
