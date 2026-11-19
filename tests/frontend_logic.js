// No browser or third-party dependency: exercise display invariants using a small DOM double.
function runFrontendChecks(source) {
  class TestNode {
    constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.events = {}; }
    append(...children) { this.children.push(...children); }
    insertBefore(child, before) { this.children.splice(this.children.indexOf(before), 0, child); }
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
  const loadDisplay = new Function('Node', 'document', 'localStorage', 'window', 'location', 'history', 'setInterval', 'fetch', bootstrapRemoved + '\nreturn {sectorComparison,marketBreadth,leadersPanel,sourceCoverage,overview,heatmap,newsPage,newsSummaryCard,newsItem,trendPanel,chart,relayIntraday,statCard,intradayFor,displayQuote,sectorQuote,stockRankLabel,normalizeDashboard,quantItem,metricHelp,industryRelations,sentimentLabel,newsTone,requestSnapshot,validateSnapshot,openMetricHelp,fundsPanel,fundPremium,researchHelp,sectorNewsSummary,newsBoardChange,trendRoom,trendFeed,trendView,render,state,baseData,pages};');
  const startDisplay = document => loadDisplay(
    TestNode, document, {getItem() {return null;}, setItem() {}}, {addEventListener() {}, scrollTo() {}, dispatchEvent() {}},
    {hash: ''}, {replaceState() {}}, () => {}, fakeFetch);
  const api = startDisplay(doc);
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
  check('trend room publishes three windows, current coverage, and expandable leader evidence',()=>{
    const windowResult=state=>({state,start_date:'2026-11-04',return_pct:2.3,score:1.4,efficiency:.6,direction_share:.7});
    const windows={5:windowResult(2),10:windowResult(1),20:{reason:'missing_window'}};
    const stock={as_of:'2026-11-17',windows:{...windows,10:windowResult(-2)}};
    const market={id:'sh000001',name:'趋势指数',as_of:'2026-11-18',windows};
    const board={id:'801780.SI',name:'验证银行',group:'sw',as_of:'2026-11-18',windows,leaders:[{id:'600000.SH',name:'龙头样本',rank_value:2e11}],
      rank_basis:'total_market_cap',rank_date:'2026-11-18',rank_coverage:.97};
    const packet={method:'closing_trend_v1',expected_date:'2026-11-18',market:[market],boards:[board],stocks:{'600000.SH':stock}};
    ok(api.trendFeed.valid(packet));ok(!api.trendFeed.valid({...packet,stocks:{}}));
    api.state.page='trend';api.state.trends=packet;api.render();
    const listed=text(root);
    for(const label of ['趋势观察室','趋势指数','快速上升','波动上升','待数据','1 / 1','10 日 · 主观察','龙头 1 只'])ok(listed.includes(label));
    ok(!listed.includes('龙头样本'));ok(!listed.includes('上涨概率'));
    api.trendView.expanded=board.id;api.render();
    const expanded=text(root);
    for(const label of ['龙头样本','大幅下降','待更新','2026-11-17','成分覆盖 97.0%'])ok(expanded.includes(label));
    api.trendView.window=20;api.render();ok(text(root).includes('0 / 1'));
    api.trendView.window=10;api.trendView.expanded=null;api.state.trends=null;
  });
  check('window columns sort numeric returns and keep stale rows last',()=>{
    const dates='2026-11-18';
    const asset=(id,move,as_of=dates)=>({id,name:id,as_of,windows:{5:{},10:{return_pct:move},20:{}}});
    api.state.page='trend';
    api.state.trends={expected_date:dates,market:[asset('测试低',-4),asset('旧日线',30,'2026-11-17'),asset('测试高',2),asset('待接收',null)],boards:[],stocks:{}};
    api.render();
    const walk=node=>[node,...node.children.flatMap(walk)];
    const sortButton=()=>walk(root).find(n=>n.tag==='button'&&n.attrs['aria-label']?.startsWith('按 10 日涨跌幅'));
    sortButton().events.click();
    const descending=text(root);
    ok(descending.indexOf('测试高')<descending.indexOf('测试低'));ok(descending.indexOf('测试低')<descending.indexOf('旧日线'));
    sortButton().events.click();
    const ascending=text(root);
    ok(ascending.indexOf('测试低')<ascending.indexOf('测试高'));ok(ascending.indexOf('测试高')<ascending.indexOf('旧日线'));
    api.trendView.sorts={};api.state.trends=null;
  });
  check('ready dashboard status illuminates market status',()=>{api.state.data={...d,status:'ready'};api.state.page='overview';api.render();const nodes=[];function walk(n){if(String(n.className||'').includes('status-dot live'))nodes.push(n);(n.children||[]).forEach(walk);}walk(root);ok(nodes.length>0);api.state.data=d;});
  check('per-article model tone is distinguished from directional impact',()=>{const node=api.newsTone({sentiment:'negative',sentiment_method:'finbert_chinese_cpu',impact:'positive'});const output=text(node);ok(output.includes('文本语气'));ok(output.includes('偏负面'));ok(output.includes('金融文本模型'));ok(!output.includes('潜在利好'));});
  check('per-article fallback rules are explicitly identified',()=>{const output=text(api.newsTone({sentiment:'positive',sentiment_method:'financial-tone-rules-v2'}));ok(output.includes('偏正面'));ok(output.includes('规则分析'));});
  check('per-article missing sentiment remains unknown',()=>{ok(text(api.newsTone({})).includes('暂无覆盖'));});
  check('membership fallback is never claimed to be turnover ranked',()=>{eq(api.stockRankLabel({rank_basis:'membership_order'}),'成分示例 · 市值排序待数据');eq(api.stockRankLabel({stocks:[{rank_basis:'membership_order'}]}),'成分示例 · 市值排序待数据');});
  check('unknown member order remains explicitly unconfirmed',()=>{eq(api.stockRankLabel({stocks:[]}),'成分示例 · 排序依据待确认');});
  check('known turnover order remains available for retained datasets',()=>{eq(api.stockRankLabel({rank_basis:'turnover_amount'}),'按已获取成交额排序');});
  check('stock rank basis is used when the sector rank basis is absent',()=>{const copy=JSON.parse(JSON.stringify(d));copy.sectors[0].stocks=[{code:'600000',name:'测试股票',price:10,as_of:'2026-11-13',timestamp_quality:'daily_close',rank_basis:'total_market_cap'}];api.state.data=copy;api.state.page='sectors';api.state.search='';api.state.selected=copy.sectors[0].id;api.render();ok(text(root).includes('按已获取总市值排序'));api.state.data=d;});
  check('vendor help states published methodology and independent date',()=>{api.openMetricHelp({name:'测试行业',vendor_pe:18.5,vendor_valuation_date:'2026-11-13'},'pe_ttm');const content=text(doc.body.children.at(-1));ok(content.includes('未声明 TTM'));ok(content.includes('2026-11-13'));ok(content.includes('未在本网站用归母利润'));ok(content.includes('PE = 股价 ÷ 每股收益'));ok(content.includes('利润周期、亏损处理'));});
  check('vendor PE help button uses its actual supplied metric label',()=>{const node=api.metricHelp({name:'测试行业',vendor_pe:18.5},'pe_ttm');ok(node.attrs['aria-label'].includes('供应商 PE（未声明 TTM）'));ok(!node.attrs['aria-label'].includes('PE (TTM)'));});
  check('vendor PB help retains a general formula and unverified aggregation caveat',()=>{api.openMetricHelp({name:'测试行业',vendor_pb:1.5},'pb');const content=text(doc.body.children.at(-1));ok(content.includes('PB = 股价 ÷ 每股净资产'));ok(content.includes('指数聚合'));ok(content.includes('尚未核实'));});
  check('empty industry relations show an honest consolidated empty state',()=>{const output=text(api.industryRelations({drivers:[],upstream:[],downstream:[]}));ok(output.includes('尚无已核实的产业链关系'));ok(!output.includes('核心驱动'));});
  for(const key of ['pe_ttm','pb','sharpe'])check(`metric help ${key} handles missing data`,()=>api.openMetricHelp(d.sectors[0],key));
  check('fund premium requires same-date price and NAV',()=>{eq(api.fundPremium({price:2.2,nav:2,price_date:'2026-11-16',nav_date:'2026-11-13',premium_pct:10}),null);ok(Math.abs(api.fundPremium({price:2.2,nav:2,price_date:'2026-11-16',nav_date:'20261116'})-10)<1e-10);eq(api.fundPremium({price:2.2,nav:0,price_date:'2026-11-16',nav_date:'2026-11-16'}),null);});
  check('fund cards preserve independent dates and do not treat NAV as valuation',()=>{const node=api.fundsPanel({funds:[{code:'TEST.SH',name:'测试行业基金',manager:'测试管理人',broker_relation:'测试券商参股',price:2.2,price_date:'2026-11-16',nav:2,nav_date:'2026-11-13',aum_cny:12000000000,aum_date:'2026-11-12',match_status:'theme_candidate',source_url:'javascript:alert(1)'}]});const output=text(node);ok(output.includes('2026-11-16'));ok(output.includes('2026-11-13'));ok(output.includes('2026-11-12'));ok(output.includes('120.00 亿'));ok(output.includes('持仓待核验'));ok(output.includes('日期不齐 / 待数据'));const links=[];function walk(n){if(n.tag==='a')links.push(n);(n.children||[]).forEach(walk);}walk(node);eq(links.length,0);});
  check('fund panel displays at most three legitimate candidates',()=>{const node=api.fundsPanel({funds:Array.from({length:8},(_,i)=>({code:'fund'+i,name:'候选'+i}))});const output=text(node);ok(output.includes('候选2'));ok(!output.includes('候选3'));});
  class Immediate {constructor(value,error){this.value=value;this.error=error;}then(fn){if(this.error)return this;try{const value=fn(this.value);return value instanceof Immediate?value:new Immediate(value);}catch(error){return new Immediate(null,error);}}catch(fn){if(!this.error)return this;try{return new Immediate(fn(this.error));}catch(error){return new Immediate(null,error);}}}
  function requestFixture(responses){const calls=[];let value,error;const fetcher=(url,options)=>{calls.push({url,options});const next=responses.shift();if(next instanceof Error)return new Immediate(null,next);return new Immediate({status:next.status,ok:next.status>=200&&next.status<300,json:()=>new Immediate(next.body,next.jsonError)});};api.requestSnapshot(fetcher,12345).then(x=>value=x).catch(e=>error=e);return{calls,value,error};}
  check('lightweight view is the preferred request',()=>{const r=requestFixture([{status:200,body:d}]);eq(r.calls.length,1);eq(r.calls[0].url,'./data/view.json?t=12345');eq(r.calls[0].options.cache,'no-store');eq(r.value,d);});
  check('view 404 alone falls back to full dashboard with matching cache bust',()=>{const r=requestFixture([{status:404},{status:200,body:d}]);eq(r.calls.length,2);eq(r.calls[1].url,'./data/dashboard.json?t=12345');eq(r.value,d);});
  check('view server error does not trigger expensive dashboard fallback',()=>{const r=requestFixture([{status:500}]);eq(r.calls.length,1);ok(r.error.message.includes('500'));});
  check('view network failure preserves failure without fallback',()=>{const r=requestFixture([new Error('offline')]);eq(r.calls.length,1);eq(r.error.message,'offline');});
  check('invalid view schema does not silently switch datasets',()=>{const r=requestFixture([{status:200,body:{schema_version:2,market:[],sectors:[]}}]);eq(r.calls.length,1);ok(r.error.message.includes('数据格式'));});
  check('unsafe array shape is rejected before rendering',()=>{const r=requestFixture([{status:200,body:{...d,news:{}}}]);ok(r.error.message.includes('news'));});
  check('invalid stock prediction array is rejected before rendering',()=>{const r=requestFixture([{status:200,body:{...d,equity_forecast:{stocks:{}}}}]);ok(r.error.message.includes('equity_forecast.stocks'));});
  check('fallback dashboard is validated too',()=>{const r=requestFixture([{status:404},{status:200,body:{market:[],sectors:[]}}]);eq(r.calls.length,2);ok(r.error);});

  check('one damaged baseline section retains only its previous value',()=>{
    const next={...d,market:[{id:'fresh-index'}],news:{invalid:true}};
    const partial=api.validateSnapshot(next,d);
    eq(partial.news,d.news);eq(partial.market[0].id,'fresh-index');eq(partial.unavailable_sections[0],'资讯');
    const cold=api.validateSnapshot(next,null);eq(cold.news,undefined);eq(cold.market[0].id,'fresh-index');
    eq(api.validateSnapshot({...next,news:[]},partial).unavailable_sections,undefined);
  });

  check('exchange breadth and rankings compare one observed date while preserving older reference prices',()=>{
    const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai'}).format(new Date());
    const yesterday=new Date(Date.parse(today+'T00:00:00Z')-86400000).toISOString().slice(0,10);
    const sector=(id,day,change)=>({id,name:id,taxonomy:'SSE',provider:'stock_api',price:100,change_pct:change,
      timestamp_quality:'source_reported',quote_date:day,quote_timestamp:day+'T00:00:00+08:00'});
    const stale=sector('昨日领涨',yesterday,8),now=sector('今日小跌',today,-1);
    const pending={id:'pending',name:'尚无行情',taxonomy:'SSE'};
    const pre={...d,sectors:[stale,pending]};
    eq(api.sectorComparison(pre.sectors).day,yesterday);
    eq(api.sourceCoverage(pre).today,0);eq(api.sourceCoverage(pre).quoted,1);
    ok(text(api.leadersPanel(pre)).includes('昨日领涨'));
    const part={...d,sectors:[stale,now,pending]},comparison=api.sectorComparison(part.sectors);
    eq(comparison.day,today);eq(comparison.ranked.length,1);eq(comparison.rows[0].sector.id,now.id);
    eq(comparison.label,today+' 涨跌幅覆盖 1 / 3');eq(api.sourceCoverage(part).today,1);eq(api.sourceCoverage(part).quoted,2);
    ok(text(api.marketBreadth(part)).includes('0 涨'));ok(text(api.marketBreadth(part)).includes('1 跌'));
    ok(!text(api.leadersPanel(part)).includes(stale.name));ok(text(api.leadersPanel(part)).includes(now.name));
    ok(text(api.heatmap(part)).includes(yesterday+' 参考'));ok(!text(api.heatmap(part)).includes('+8.00%'));
    eq(api.relayIntraday({trade_date:yesterday,series:{[stale.id]:[[stale.quote_timestamp,100]]}},stale,stale,today).length,0);
  });

  check('daily forecast array shapes are validated before rendering',()=>{let error;try{api.validateSnapshot({...d,daily_forecast:{market:{}}});}catch(err){error=err;}ok(error?.message.includes('daily_forecast.market'));});
  check('page observation is a labeled fallback without replacing a current Tushare close',()=>{
    const page={price:3200,change_pct:2,observed_at:'2026-11-16T15:55:00+08:00',timestamp_quality:'page_observed'};
    let q=api.displayQuote({...daily('2026-11-13',3000),page_quote:page},now);
    eq(q.price,3200);ok(q.label.includes('网页观测'));ok(!q.trusted);
    q=api.displayQuote({...daily('2026-11-16',3110),page_quote:page},now);eq(q.price,3110);eq(q.provider,'stock_api');
  });
  check('a fresh primary Tushare timestamp stays ahead of a page observation',()=>{
    const q=api.displayQuote({...live('14:59:00',3110),page_quote:{price:3200,observed_at:'2026-11-16T15:01:00+08:00',timestamp_quality:'page_observed'}},now);
    eq(q.price,3110);eq(q.provider,'stock_api');
  });
  check('page sampling and SW taxonomy stay separate, including matching names',()=>{
    api.state.pageQuotes={schema_version:1,market:[],sectors:[{id:'em:BK0475',name:'银行',taxonomy:'EASTMONEY'}],intraday:{}};
    api.state.page='sectors';api.state.taxonomy='eastmoney';let result=api.normalizeDashboard(d);eq(result.sectors[0].id,'em:BK0475');
    api.state.taxonomy='sw';result=api.normalizeDashboard(d);eq(result.sectors[0].id,'801010.SI');api.state.pageQuotes=null;
  });
  check('observed-page ticks keep observed time and exclude missing observation clocks',()=>{
    const rows=api.intradayFor({intraday:{x:{points:[{price:1,observed_at:'2026-11-16T10:00:00+08:00',timestamp_quality:'page_observed'},{price:2,observed_at:'2026-11-16T10:05:00+08:00',timestamp_quality:'page_observed'},{price:3,observed_at:'2026-11-16T10:10:00+08:00',timestamp_quality:'retrieved_only'}]}}},{id:'x'});
    eq(rows.length,2);eq(rows[1].timestamp,'2026-11-16T10:05:00+08:00');
  });
  check('chart reads the selected value above the plot and preserves crosshair controls',()=>{
    const [header,plot]=api.chart([{date:'2026-11-12',close:100},{date:'2026-11-13',close:101}],{metric:'index'}).children;
    ok(plot.events.pointermove);ok(plot.events.pointerleave);ok(plot.events.keydown);eq(plot.attrs.tabindex,'0');
    const crosshair=plot.children.find(n=>n.attrs.class==='chart-crosshair');
    const readout=header.children.at(-1);
    eq(crosshair.children.map(n=>n.tag).join(','),'line,circle');
    plot.events.focus();ok(text(readout).includes('101.00'));ok(text(readout).includes('2026-11-13'));
    eq(readout.attrs['data-active'],'true');eq(crosshair.attrs.visibility,'visible');
    plot.events.pointerleave();eq(readout.attrs['data-active'],'false');eq(crosshair.attrs.visibility,'hidden');
  });
  check('malformed direction forecast cannot bypass snapshot array checks',()=>{let bad=false;try{api.validateSnapshot({...d,direction_probability:{market:{}}});}catch{bad=true;}ok(bad);});
  check('today sector chart keeps tone, sector impact and quote date independent',()=>{
    const day='2026-11-19',now=new Date(day+'T19:00:00+08:00'),id='801780.SI';
    const sector={id,name:'银行',taxonomy:'SW2021',level:'L1',daily_quote:{
      price:300,change_pct:-1.5,quote_date:day,quote_timestamp:day+'T15:00:00+08:00',timestamp_quality:'daily_close'}};
    const a={id:'a',published_at:day+'T15:05:00+08:00',sector_ids:[id,id],llm_analysis:{
      status:'ready',tone:'positive',sector_impacts:[{sector_code:id,direction:'negative'},{sector_code:id,direction:'negative'}]}};
    const missing={id:'b',published_at:day+'T15:15:00+08:00',sector_ids:[id]};
    const rumor={...a,id:'c',credibility:'rumor'};
    const yesterday={...a,id:'d',published_at:'2026-11-18T15:05:00+08:00'};
    const r=api.sectorNewsSummary({sectors:[sector],news:[a,missing,rumor,yesterday]},day,now);
    eq(r.total,2);eq(r.related,2);eq(r.labeled,1);eq(r.rows[0].count,2);
    eq(r.rows[0].tones.positive,1);eq(r.rows[0].impact.negative,1);
    eq(api.newsBoardChange(sector,day,now).value,-1.5);
    eq(api.newsBoardChange(sector,'2026-11-20',new Date('2026-11-20T09:00:00+08:00')).value,null);
    ok(!api.pages.some(([id])=>id==='review'));
  });
  check('full-text summary is the first article block and evidence stays folded',()=>{
    const n={title:'原油上涨',llm_analysis:{status:'ready',summary:'原油涨价利好开采业务，并增加航空燃油成本。',sector_impacts:[{sector_name:'石油石化',direction:'positive',reason:'收入改善',evidence:['原文依据']}],uncertainties:[]}};
    const node=api.newsItem(n,d);eq(node.children[0].className,'news-ai-summary');const out=text(node);ok(out.includes('全文提要'));ok(out.includes('石油石化 · 利好'));ok(out.includes('影响依据'));ok(!out.includes('[object Object]'));
    eq(api.newsSummaryCard({summary:'只有摘要'}),null);
  });
  check('news chronology uses release instants before pagination and filters',()=>{
    const rows=[
      {id:'unconfirmed',first_seen_at:'2026-11-20T09:00:00+08:00'},
      {id:'invalid',published_at:'unknown'},
      {id:'old',published_at:'2026-11-18T23:59:00+08:00',first_seen_at:'2026-11-20T12:00:00+08:00'},
      {id:'tie-b',published_at:'2026-11-19T15:07:21+08:00'},
      {id:'tie-a',published_at:'2026-11-19T07:07:21Z'},
      {id:'latest',published_at:'2026-11-19 15:08:01'},
      ...Array.from({length:12},(_,i)=>({id:'morning-'+i,published_at:'2026-11-19T10:'+String(i).padStart(2,'0')+':00+08:00'}))
    ].map(row=>({...row,title:row.id,credibility:row.id==='invalid'?'official':'reliable'}));
    const ordered=api.normalizeDashboard({...api.baseData(),news:rows});
    const reversed=api.normalizeDashboard({...api.baseData(),news:[...rows].reverse()});
    eq(ordered.news.map(n=>n.id).join(),reversed.news.map(n=>n.id).join());
    eq(ordered.news.slice(0,3).map(n=>n.id).join(),'latest,tie-a,tie-b');
    eq(ordered.news.slice(-3).map(n=>n.id).join(),'old,invalid,unconfirmed');
    eq(rows[0].id,'unconfirmed');
    const walk=node=>[node,...node.children.flatMap(walk)];
    const list=()=>walk(api.newsPage(ordered)).find(n=>n.className==='news-list');
    const ids=timeline=>timeline.children.filter(n=>n.className==='news-item').map(n=>n.attrs.id.slice(5));
    const timeline=list();
    eq(ids(timeline).join(),ordered.news.slice(0,12).map(n=>n.id).join());
    timeline.children.find(n=>n.id==='news-load-more').events.click();
    eq(ids(timeline).join(),ordered.news.map(n=>n.id).join());
    api.state.newsSearch='tie-';eq(ids(list()).join(),'tie-a,tie-b');api.state.newsSearch='';
    api.state.newsFilter='官方信息';eq(ids(list()).join(),'invalid');api.state.newsFilter='全部';
    api.state.newsLimit=12;
  });
  check('news cards distinguish the cloud analysis queue from older archived articles',()=>{
    const n={id:'etf',title:'ETF收盘',published_at:'2026-11-19T15:07:00+08:00',analysis_queue:'tencent_news'};
    for(const [state,label] of [['pending','MiniMax 分析排队中'],['waiting_body','缺少正文，等待补采'],
      ['retry','MiniMax 分析等待重试'],['provider_cooldown','MiniMax 服务冷却中']]){
      const card=text(api.newsItem({...n,llm_status:state,analysis_retry_at:'2026-11-19T16:35:00+08:00'},d));
      ok(card.includes(label));eq(card.includes('16:35 后可重试'),['retry','provider_cooldown'].includes(state));
    }
    ok(!text(api.newsItem({...n,analysis_queue:undefined,llm_status:'pending'},d)).includes('分析排队中'));
    ok(!text(api.newsItem({...n,llm_status:'pending',llm_analysis:{status:'ready',summary:'已完成的分析'}},d)).includes('分析排队中'));
  });
  check('official minutes replace only the same identity and date',()=>{
    const entity={id:'801780.SI',index_code:'801780.SI'},minute=(price,code='801780.SI')=>({price,timestamp:'2026-11-17T14:00:00+08:00',provider:'stock_api',kind:'historical_minute',source_code:code});
    const x={intraday:{'801780.SI':{points:[{price:99,timestamp:'2026-11-17T13:00:00+08:00',provider:'eastmoney_page'},minute(100),{...minute(101),timestamp:'2026-11-17T15:00:00+08:00'}]}}};
    eq(api.intradayFor(x,entity).length,2);eq(api.intradayFor(x,entity)[0].price,100);
  });
  check('today chart draws short and long data gaps only within a real trading session',()=>{
    const day='2026-11-18',item={id:'idx'};
    const stamp=t=>`${day}T${t}+08:00`;
    const state={trade_date:day,series:{idx:[
      ['2026-11-17T15:00:00+08:00',90],
      [stamp('11:29:06'),100],[stamp('11:30:03'),101],[stamp('12:03:00'),101],
      [stamp('13:00:01'),102],[stamp('13:01:59'),103],[stamp('13:03:01'),104],[stamp('13:42:00'),105],
    ]}};
    const latest={quote_date:day,quote_timestamp:stamp('13:42:50'),timestamp_quality:'source_reported',price:106};
    const ticks=api.relayIntraday(state,latest,item,day);
    eq(ticks.length,6);eq(ticks.at(-1).price,106);
    eq(api.relayIntraday(state,latest,item,'2026-11-19').length,0);
    const graph=api.chart(ticks,{today:true,metric:'intraday',range:null}).children.at(-1);
    eq(graph.children.filter(node=>node.attrs.class==='session-line').length,2);
    eq(graph.children.filter(node=>node.attrs.class==='session-interpolation').length,2);
    eq(graph.children.filter(node=>node.attrs.class==='isolated-sample').length,2);
    ok(text(graph).includes('11:30'));ok(text(graph).includes('13:00'));
  });
  check('daily curves bridge only verified missing sessions without creating observations',()=>{
    const rows=[{date:'2026-11-13',close:100},{date:'2026-11-16',close:101},{date:'2026-11-18',close:103}];
    const ordinary=api.chart(rows,{metric:'index',range:null}).children.at(-1);
    eq(ordinary.children.filter(n=>n.attrs.class==='session-interpolation').length,0);
    const graph=api.chart(rows,{metric:'index',range:null,sessions:['2026-11-12','2026-11-17','2026-11-19']}).children.at(-1);
    eq(graph.children.filter(n=>n.attrs.class==='session-interpolation').length,1);
    graph.events.focus();graph.events.keydown({key:'ArrowLeft',preventDefault(){}});
    ok(graph.attrs['aria-label'].includes('2026-11-16'));ok(!graph.attrs['aria-label'].includes('2026-11-17'));
  });
  check('recovered bars fill only their own missing minute, date and index',()=>{
    const day='2026-11-19', stamp=t=>day+'T'+t+'+08:00';
    const live={trade_date:day,series:{a:[[stamp('09:51:02'),101],[stamp('09:53:01'),103]]}};
    const recovery={trade_date:day,series:{a:[[stamp('09:51:59'),999],[stamp('09:52:00'),102],null,[stamp('12:00:00'),2]],b:[[stamp('09:54:00'),1]]}};
    const points=api.relayIntraday(live,null,{id:'a'},day,recovery);
    eq(points.map(p=>p.price).join(','),'101,102,103');eq(points[1].timestamp_quality,'minute_backfill');
    eq(api.relayIntraday(live,null,{id:'a'},day,{...recovery,trade_date:'2026-11-18'}).length,2);
  });
  function overviewFixture() {
    const page = new TestNode('root');
    const cloud = startDisplay({...doc, querySelector: selector =>
      selector === 'meta[name="radar-live-quotes"]' ? {content:'sxdata'} : page});
    const day = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai'}).format(new Date());
    const indices = ['上证指数','深证成指','创业板指','沪深300','科创50','中证500'];
    const ids = ['sh000001','sz399001','sz399006','sh000300','sh000688','sh000905'];
    const market = indices.map((name,i)=>({id:ids[i],name,provider:'stock_api'}));
    const sectors = Array.from({length:16},(_,i)=>({
      id:`0000${32+i}.SH`, name:'行业'+String(i+1).padStart(2,'0'),
      provider:'stock_api',taxonomy:'SSE',price:i===15?null:100+i,amount:1e8*(16-i),
      change_pct:i===15?null:i-7,quote_timestamp:day+'T00:00:00+08:00',
      quote_date:day,timestamp_quality:'source_reported',
    }));
    const sw = {id:'801780.SI',name:'申万银行',taxonomy:'SW2021',level:'L1',provider:'stock_api'};
    cloud.state.data = {...cloud.baseData(),market,sectors:[sw]};
    const feed = {schema_version:1,source:'sxdata_relay',trade_date:day,market,sectors};
    const nodes = (node,match) => (match(node)?[node]:[]).concat(
      node.children.flatMap(child=>nodes(child,match)));
    function byId(id) {
      const matches = nodes(page,n=>n.attrs.id===id);
      eq(matches.length,1);
      return matches[0];
    }
    function buttons(id,predicate) {
      return nodes(byId(id),n=>n.tag==='button'&&predicate(n));
    }
    function select(id,predicate) {
      const matches = buttons(id,predicate);
      ok(matches.length>0,'missing control in '+id);
      matches[0].events.click();
    }
    const named = name => n => text(n).includes(name);
    const chartTitle = id => text(nodes(byId(id),n=>/^h[1-6]$/.test(n.tag))[0]);
    return {cloud,page,market,sectors,feed,sw,nodes,byId,buttons,select,named,chartTitle};
  }

  check('delayed industry feed populates only the board below the six market indices',()=>{
    const f=overviewFixture();
    f.cloud.render();
    const boards=f.nodes(f.page,n=>['overview-indices','overview-industries'].includes(n.attrs.id));
    eq(boards.map(n=>n.attrs.id).join(','),'overview-indices,overview-industries');
    eq(f.buttons('overview-indices',n=>n.attrs['aria-controls']==='overview-trend').length,6);
    ok(f.chartTitle('overview-trend').includes(f.market[0].name));
    ok(!text(f.byId('overview-industries')).includes(f.sw.name));
    f.cloud.state.liveQuotes=f.feed;
    f.cloud.render();
    const industryNames=f.sectors.map(s=>s.name);
    const tileButtons=()=>f.buttons('overview-industries',n=>(n.className||'').split(' ').includes('heat-tile')
      && n.attrs['aria-controls']==='overview-sector-trend' && n.attrs['aria-pressed']!==undefined);
    eq(tileButtons().length,10);
    ok(text(f.byId('overview-industries')).includes('交易所成交活跃榜'));
    f.select('overview-industries',n=>text(n)==='全部 16');
    const first=tileButtons();eq(first.length,12);
    f.select('overview-industries',n=>text(n)==='下一页');
    const tiles=[...first,...tileButtons()];eq(tiles.length,16);
    for(const name of industryNames) {
      ok(tiles.some(f.named(name)),name+' missing from industry board');
      ok(!text(f.byId('overview-indices')).includes(name));
    }
    for(const index of f.market)ok(!text(f.byId('overview-industries')).includes(index.name));
    const missing=text(tiles.find(f.named(f.sectors[15].name)));
    ok(missing.includes('—'));ok(!missing.includes('0.00%'));
    f.select('overview-industries',n=>text(n)==='上一页');
    ok(f.chartTitle('overview-sector-trend').includes(f.sectors[0].name));

    const day=f.feed.trade_date;
    const prior=new Date(Date.parse(day+'T00:00:00Z')-86400000).toISOString().slice(0,10);
    const older={...f.sectors[1],quote_date:prior,quote_timestamp:prior+'T14:59:00+08:00',change_pct:12};
    const index={...f.market[0],price:3333,quote_date:day,quote_timestamp:day+'T00:00:00+08:00',timestamp_quality:'source_reported'};
    f.cloud.state.liveQuotes={...f.feed,market:[index,...f.market.slice(1)],
      sectors:f.sectors.map(sector=>sector.id===older.id?older:sector)};
    f.cloud.render();
    ok(text(f.byId('overview-indices')).includes('3,333.00'));
    const tile=f.buttons('overview-industries',n=>(n.className||'').includes('heat-tile')&&f.named(older.name)(n))[0];
    ok(text(tile).includes(prior+' 参考'));ok(!text(tile).includes('+12.00%'));
  });

  check('each broad index opens research for its own published exchange code',()=>{
    const f=overviewFixture();
    f.cloud.state.liveQuotes=f.feed;f.cloud.render();
    const expected=['000001.SH','399001.SZ','399006.SZ','000300.SH','000688.SH','000905.SH'];
    f.market.forEach((market,i)=>{
      f.select('overview-indices',f.named(market.name));
      const links=f.nodes(f.byId('overview-trend'),n=>n.tag==='a' && n.attrs.href==='./asset/?type=index&code='+expected[i]);
      eq(links.length,1);
    });
  });

  check('industry tiles and rankings do not replace the index or change its chart controls',()=>{
    const f=overviewFixture();
    f.cloud.state.liveQuotes=f.feed;f.cloud.render();
    f.select('overview-indices',f.named(f.market[1].name));
    f.select('overview-trend',n=>text(n)==='日线');
    f.select('overview-trend',n=>text(n)==='3M');
    f.select('overview-industries',n=>n.attrs['aria-pressed']!==undefined&&f.named(f.sectors[2].name)(n));
    eq(f.cloud.state.page,'overview');
    ok(f.chartTitle('overview-trend').includes(f.market[1].name));
    ok(f.chartTitle('overview-sector-trend').includes(f.sectors[2].name));
    const selectedMode=(label)=>f.buttons('overview-trend',n=>text(n)===label&&n.className==='active').length;
    eq(selectedMode('日线'),1);eq(selectedMode('3M'),1);
    f.select('overview-industries',n=>(n.className||'').split(' ').includes('leader-row')&&f.named(f.sectors[14].name)(n));
    ok(f.chartTitle('overview-sector-trend').includes(f.sectors[14].name));
    eq(selectedMode('日线'),1);
    f.select('overview-indices',f.named(f.market[2].name));
    ok(f.chartTitle('overview-trend').includes(f.market[2].name));
    ok(f.chartTitle('overview-sector-trend').includes(f.sectors[14].name));
    f.select('overview-industries',n=>text(n)==='全部 16');
    f.select('overview-industries',n=>text(n)==='下一页');
    const selectedTiles=f.buttons('overview-industries',n=>(n.className||'').split(' ').includes('heat-tile')
      && n.attrs['aria-pressed']==='true');
    eq(selectedTiles.length,1);ok(f.named(f.sectors[14].name)(selectedTiles[0]));
    f.cloud.state.liveQuotes={...f.feed,sectors:[...f.sectors].reverse()};
    f.cloud.render();
    ok(f.chartTitle('overview-sector-trend').includes(f.sectors[14].name));
  });

  check('an industry-only publication remains usable while market data is absent',()=>{
    const f=overviewFixture();
    f.cloud.state.data=f.cloud.baseData();
    f.cloud.state.liveQuotes={...f.feed,market:[]};
    f.cloud.render();
    f.select('overview-industries',n=>n.attrs['aria-pressed']!==undefined&&f.named(f.sectors[3].name)(n));
    ok(f.chartTitle('overview-sector-trend').includes(f.sectors[3].name));
    ok(!text(f.byId('overview-indices')).includes(f.sectors[3].name));
    f.cloud.state.liveQuotes=f.feed;
    f.cloud.render();
    ok(f.chartTitle('overview-trend').includes(f.market[0].name));
    ok(f.chartTitle('overview-sector-trend').includes(f.sectors[3].name));
  });

  check('changing industry taxonomy keeps the index and isolates the two charts’ periods',()=>{
    const f=overviewFixture();
    f.cloud.state.liveQuotes=f.feed;f.cloud.render();
    f.select('overview-indices',f.named(f.market[1].name));
    f.select('overview-trend',n=>text(n)==='日线');
    f.select('overview-trend',n=>text(n)==='3M');
    f.select('overview-industries',n=>text(n).includes('申万行业')&&text(n).includes('估算'));
    ok(f.chartTitle('overview-sector-trend').includes(f.sw.name));
    ok(!text(f.byId('overview-industries')).includes(f.sectors[0].name));
    f.select('overview-sector-trend',n=>text(n)==='市值');
    f.select('overview-sector-trend',n=>text(n)==='1Y');
    eq(f.buttons('overview-trend',n=>n.className==='active'&&text(n)==='3M').length,1);
    eq(f.buttons('overview-trend',n=>n.className==='active'&&text(n)==='日线').length,1);
    ok(f.chartTitle('overview-trend').includes(f.market[1].name));
    f.select('overview-industries',n=>text(n)==='板块行情目录');
    ok(f.chartTitle('overview-sector-trend').includes(f.sectors[0].name));
    ok(!text(f.byId('overview-industries')).includes(f.sw.name));
  });
  check('directory searches aliases and exact codes, isolates sources, and dates the official heat ranking',()=>{
    const f=overviewFixture();
    const day=f.feed.trade_date,older='2020-05-06';
    const exchange={code:'980017.SZ',name:'国证芯片',fullname:'国证半导体芯片指数',kind:'theme',
      intro:'跟踪半导体与集成电路相关公司表现',keywords:['集成电路']};
    const ths={code:'881121.TI',name:'半导体',kind:'I',intro:'关注半导体行业上市公司表现'};
    const concept={code:'885999.TI',name:'人工智能',kind:'N',intro:'关注人工智能相关概念公司表现'};
    const unusual={code:'700051R.TI',name:'金仓全收益',kind:'TH',intro:'跟踪对应全收益指数的表现'};
    const extras=Array.from({length:10},(_,i)=>({code:String(886000+i)+'.TI',name:'概念'+i,kind:'N',intro:'关注该概念相关公司的市场表现'}));
    const hot=[concept,...extras].map((item,i)=>({code:item.code,name:item.name,rank:i+1,hot:15894-i,change_pct:2.28}));
    const state=f.cloud.state;
    state.liveQuotes=f.feed;
    state.sectorCatalog={items:[exchange]};
    state.thsCatalog={items:[ths,concept,unusual,...extras],sw_items:[{code:f.sw.id,kind:'L1',intro:'关注商业银行及相关金融服务'}]};
    state.sectorReference={quotes:{[concept.code]:{price:404.42,change_pct:-0.5,quote_date:older}}};
    state.sectorHot={trade_date:older,rank_time:'10:30:05',items:[...hot].reverse()};
    f.cloud.render();
    const board=()=>f.byId('overview-industries');
    const tiles=()=>f.buttons('overview-industries',n=>(n.className||'').split(' ').includes('heat-tile'));
    const query=value=>{const input=f.byId('directory-search');input.value=value;input.events.input({target:input});};
    eq(tiles().length,10);ok(text(board()).includes('历史榜 '+older+' 10:30:05'));
    ok(text(tiles()[0]).includes(concept.name));ok(text(tiles()[0]).includes('点位待更新'));
    ok(!text(tiles()[0]).includes('404.42'));ok(!text(tiles()[0]).includes('0.00 点'));
    state.sectorHot={trade_date:day,rank_time:'09:46:01',items:hot};
    f.cloud.render();
    ok(text(board()).includes('今日 '+day+' 09:46:01'));ok(!text(board()).includes('交易所成交活跃榜'));
    f.select('overview-industries',n=>n.attrs.id==='overview-industry-'+concept.code);
    const snapshot=text(f.byId('overview-sector-trend'));
    ok(snapshot.includes('行情快照'));ok(snapshot.includes('+2.28%'));ok(snapshot.includes(older+' 收盘'));ok(snapshot.includes('404.42'));
    const links=f.nodes(board(),n=>n.tag==='a'&&n.attrs.href==='./sector/?code='+concept.code);
    eq(links.length,2);
    const sampled={change_pct:1.07,quote_at:day+'T00:00:00+08:00',quoted_count:49,total_count:52};
    state.boardLiveSummary={trade_date:day,items:{[concept.code]:sampled}};
    f.cloud.render();
    ok(text(tiles()[0]).includes('+1.07%')&&text(tiles()[0]).includes('覆盖 49/52 只'));
    ok(text(f.byId('overview-sector-trend')).includes('成分股等权涨跌'));
    state.boardLiveSummary={trade_date:older,items:{[concept.code]:sampled}};f.cloud.render();
    ok(text(tiles()[0]).includes('+2.28%')&&!text(tiles()[0]).includes('+1.07%'));
    const composing=f.byId('directory-search');
    composing.events.compositionstart({target:composing});
    composing.value='bandaoti';composing.events.input({target:composing,isComposing:true});eq(tiles().length,10);
    composing.value='半导体';composing.events.compositionend({target:composing});
    eq(tiles().length,2);ok(tiles().some(f.named('交易所主题')));ok(tiles().some(f.named('同花顺行业')));
    ok(text(board()).includes(exchange.intro));ok(text(board()).includes(ths.intro));
    ok(f.chartTitle('overview-sector-trend').includes(concept.name));
    query('集成电路');eq(tiles().length,1);ok(text(tiles()[0]).includes(exchange.code));
    query('７０００５１ｒ.ti');eq(tiles().length,1);ok(text(tiles()[0]).includes(unusual.name));
    query('人工智能');f.select('overview-industries',n=>text(n)==='行业');eq(tiles().length,0);
    f.select('overview-industries',n=>text(n)==='概念');eq(tiles().length,1);
    f.select('overview-industries',n=>text(n).includes('申万行业')&&text(n).includes('估算'));
    ok(text(board()).includes('关注商业银行及相关金融服务'));
    ok(f.nodes(board(),n=>n.tag==='a'&&n.attrs.href==='./sector/?code='+f.sw.id).length>0);
    f.select('overview-industries',n=>text(n)==='板块行情目录');
    eq(tiles().length,1);ok(f.chartTitle('overview-sector-trend').includes(concept.name));
  });
  return {passed, failures};
}

// Exercise actual poll and refresh entry points against delayed/failed local fixtures.
async function runRefreshChecks(source) {
  const assert = require('node:assert/strict');
  const requests = [], intervals = [], replies = new Map(), doc = {
    hidden: false,
    querySelector: () => ({content:'sxdata'}),
    querySelectorAll: () => [],
  };
  let renders = 0;
  const json = (body, status = 200, headers = {}) => ({
    status, ok:status >= 200 && status < 300, headers:new Headers(headers), json:async () => body,
  });
  function fetcher(url, options) {
    const file = url.split('/').pop().split('?')[0];
    requests.push({file,options});
    return Promise.resolve(replies.get(file) || json(null, 503));
  }
  const program = source.replace(/^refresh\(\);$/m, '') + `
    render=()=>onRender();
    return {refresh,refreshFeed,pollFeed,quietQuoteSession,feeds,publications,state,normalizeDashboard,baseData,responseError};
  `;
  const api = new Function('document','location','history','localStorage','window','setInterval','fetch','onRender', program)(
    doc,{hash:'#news'},{},{getItem:k=>k==='guanlan.preferences'?'{"autoRefresh":false}':null},
    {addEventListener(){}},(fn,ms)=>intervals.push({fn,ms}),fetcher,()=>{renders++;});
  const fs=require('node:fs');
  for(const file of ['sector-catalog','ths-sector-catalog']){
    const feed=api.feeds.find(f=>f.file===file);
    assert.ok(feed.valid(JSON.parse(fs.readFileSync('web/data/'+file+'.json','utf8'))),file+' must accept its published catalog');
  }
  const yesterday = '2026-11-18', today = '2026-11-19';
  const old = {id:'last-night',title:'夜间更新',published_at:yesterday+'T23:58:00+08:00'};
  const previous_digest = {date:yesterday,summary:'上一期',generated_at:yesterday+'T23:59:00+08:00'};
  const news = {schema_version:1,source:'tencent_news',trade_date:today,articles:[],recent_articles:[old],
    news_digest:{date:today,status:'pending',current_article_count:0},previous_digest};
  const spec = api.feeds.find(feed=>feed.key==='cloudNews');
  let release;
  replies.set('view.json',new Promise(resolve=>{release=resolve;}));
  replies.set('cloud-news.json',json(news,200,{ETag:'one'}));
  const pollStart = api.refresh();
  const simultaneous = api.refresh();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(api.state.loading,true);
  assert.equal(api.state.cloudNews.articles.length,0);
  assert.ok(renders>1);
  assert.equal(requests.filter(r=>r.file==='view.json').length,1);
  assert.equal(requests.filter(r=>r.file==='live-quotes.json').length,1);
  assert.ok(requests.filter(r=>r.file==='view.json'||r.file==='cloud-news.json').every(r=>r.options.signal instanceof AbortSignal));
  release(json({...api.baseData(),generated_at:yesterday+'T12:00:00+08:00',market:[{id:'index'}],
    news:[{id:old.id,title:'旧版标题',published_at:old.published_at}]}));
  await Promise.all([pollStart,simultaneous]);
  assert.ok(api.state.data?.market.length);
  const page = api.normalizeDashboard(api.state.data);
  assert.equal(page.news.filter(article=>article.id===old.id).length,1);
  assert.equal(page.news.find(article=>article.id===old.id).analysis_queue,'tencent_news');
  assert.equal(page.news_digest.date,today);
  assert.equal(page.news_digest.current_article_count,0);
  assert.equal(page.previous_news_digest.date,yesterday);
  const countBefore = requests.length;
  for(const timer of intervals)timer.fn();
  assert.equal(requests.length,countBefore);

  // A same-day server-confirmed break reduces only quote polling and automatically
  // releases the throttle on the first scheduled poll after the local session opens.
  const quoteSpec=api.feeds.find(feed=>feed.key==='liveQuotes');
  assert.equal(quoteSpec.interval,5000);
  const liveTask=api.publications.get(quoteSpec.key);
  const stamp=time=>Date.parse(today+'T'+time+'+08:00');
  const quiet={source:'sxdata_relay',schema_version:1,trade_date:today,market:[],sectors:[],
      generated_at:today+'T11:30:00+08:00',collector:{session:'midday_break'}};
  liveTask.retryAt=0;
  replies.set('live-quotes.json',json(quiet,200,{ETag:'lunch'}));
  await api.refreshFeed(quoteSpec);
  liveTask.lastStartedAt=stamp('12:58:00');
  const beforeLunchPoll=requests.length;
  await api.pollFeed(quoteSpec,stamp('12:59:54'));
  assert.equal(requests.length,beforeLunchPoll);
  await api.pollFeed(quoteSpec,stamp('13:00:00'));
  assert.equal(requests.length,beforeLunchPoll+1);
  await api.refreshFeed(quoteSpec);
  assert.equal(requests.length,beforeLunchPoll+2);
  const quoteAt=(phase,trade_date=today)=>({...quiet,trade_date,collector:{session:phase}});
  assert.equal(api.quietQuoteSession(quoteAt('midday_break'),stamp('11:29:00')),false);
  assert.equal(api.quietQuoteSession(quoteAt('closed',yesterday),stamp('09:30:00')),false);
  assert.equal(api.quietQuoteSession(quoteAt('pre_open'),stamp('09:30:00')),false);
  assert.equal(api.quietQuoteSession(quoteAt('post_close'),stamp('15:00:00')),true);
  assert.equal(api.quietQuoteSession(quoteAt('closed'),stamp('14:00:00')),true);

  const before = api.state.cloudNews;
  replies.set('cloud-news.json',json(null,429,{'Retry-After':'600'}));
  const attemptTime = Date.now();
  await api.refreshFeed(spec);
  assert.equal(api.state.cloudNews,before);
  const task = api.publications.get(spec.key);
  assert.ok(task.retryAt>=attemptTime+600000);
  const countLimited = requests.length;
  await api.refreshFeed(spec);
  assert.equal(requests.length,countLimited);
  assert.equal(api.responseError(json(null,503,{'Retry-After':'Tue, 01 Jan 2030 00:00:00 GMT'})).retryAt,
    Date.parse('Tue, 01 Jan 2030 00:00:00 GMT'));
  task.retryAt=0;
  const malformed = {...news,articles:[{id:'bad',llm_analysis:{sector_impacts:[null]}}]};
  replies.set('cloud-news.json',json(malformed,200,{ETag:'invalid'}));
  await api.refreshFeed(spec);
  assert.equal(api.state.cloudNews,before);
  assert.equal(task.failures,2);
  task.retryAt=0;
  const current={...old,id:'today',published_at:today+'T00:15:00+08:00'};
  const recovered={...news,articles:[current],news_digest:{date:today,summary:'新一期',current_article_count:1}};
  replies.set('cloud-news.json',json(recovered,200,{ETag:'two'}));
  await api.refreshFeed(spec);
  assert.equal(task.failures,0);
  const updated=api.normalizeDashboard(api.state.data);
  assert.equal(updated.news[0].id,current.id);
  assert.equal(updated.previous_news_digest,undefined);
  assert.ok(updated.news.some(article=>article.id===old.id));
  replies.set('cloud-news.json',json({...news,trade_date:yesterday},200,{ETag:'stale'}));
  await api.refreshFeed(spec);
  assert.equal(api.state.cloudNews.version,'two');
  // A temporarily empty same-day digest preserves the dated successful text and its evidence.
  replies.set('cloud-news.json',json({...news,news_digest:{date:today,status:'temporarily_unavailable',current_article_count:2}},200,{ETag:'three'}));
  await api.refreshFeed(spec);
  assert.equal(api.state.cloudNews.news_digest.summary,'新一期');
  assert.equal(api.state.cloudNews.news_digest.status,'temporarily_unavailable');
  assert.equal(api.state.cloudNews.news_digest.current_article_count,2);
  api.state.cloudNews=news;
  const sameDayBase=api.normalizeDashboard({...api.state.data,news_digest:{date:today,summary:'当日基线总览',article_count:3}});
  assert.equal(sameDayBase.news_digest.summary,'当日基线总览');
  assert.equal(sameDayBase.news_digest.status,'updating');
  assert.equal(sameDayBase.news_digest.current_article_count,0);
  replies.set('view.json',json(null,500));
  const goodDashboard=api.state.data;
  await api.refresh();
  assert.equal(api.state.data,goodDashboard);
  assert.ok(api.publications.get('data').failures>0);
}
