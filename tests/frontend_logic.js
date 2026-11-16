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
  const api = new Function('Node', 'document', 'localStorage', 'window', 'location', 'history', 'setInterval', 'fetch', bootstrapRemoved + '\nreturn {dailyForecastValue,dailyForecastPanel,dailySectorPanel,dailyGoal,displayQuote,sectorQuote,stockRankLabel,normalizeDashboard,quantItem,metricHelp,industryRelations,sentimentLabel,newsTone,requestSnapshot,validateSnapshot,openMetricHelp,equityValue,equityStatus,equityStockPage,equitySummary,fundsPanel,fundPremium,researchHelp,render,state,baseData,pages};')(
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
  check('membership fallback is never claimed to be turnover ranked',()=>{eq(api.stockRankLabel({rank_basis:'membership_order'}),'成分示例 · 市值排序待数据');eq(api.stockRankLabel({stocks:[{rank_basis:'membership_order'}]}),'成分示例 · 市值排序待数据');});
  check('unknown member order remains explicitly unconfirmed',()=>{eq(api.stockRankLabel({stocks:[]}),'成分示例 · 排序依据待确认');});
  check('known turnover order remains available for retained datasets',()=>{eq(api.stockRankLabel({rank_basis:'turnover_amount'}),'按已获取成交额排序');});
  check('stock rank basis is used when the sector rank basis is absent',()=>{const copy=JSON.parse(JSON.stringify(d));copy.sectors[0].stocks=[{code:'600000',name:'测试股票',price:10,as_of:'2026-11-13',timestamp_quality:'daily_close',rank_basis:'total_market_cap'}];api.state.data=copy;api.state.page='sectors';api.state.search='';api.state.selected=copy.sectors[0].id;api.render();ok(text(root).includes('按已获取总市值排序'));api.state.data=d;});
  check('vendor help states published methodology and independent date',()=>{api.openMetricHelp({name:'测试行业',vendor_pe:18.5,vendor_valuation_date:'2026-11-13'},'pe_ttm');const content=text(doc.body.children.at(-1));ok(content.includes('未声明 TTM'));ok(content.includes('2026-11-13'));ok(content.includes('未在本网站用归母利润'));ok(content.includes('PE = 股价 ÷ 每股收益'));ok(content.includes('利润周期、亏损处理'));});
  check('vendor PE help button uses its actual supplied metric label',()=>{const node=api.metricHelp({name:'测试行业',vendor_pe:18.5},'pe_ttm');ok(node.attrs['aria-label'].includes('供应商 PE（未声明 TTM）'));ok(!node.attrs['aria-label'].includes('PE (TTM)'));});
  check('vendor PB help retains a general formula and unverified aggregation caveat',()=>{api.openMetricHelp({name:'测试行业',vendor_pb:1.5},'pb');const content=text(doc.body.children.at(-1));ok(content.includes('PB = 股价 ÷ 每股净资产'));ok(content.includes('指数聚合'));ok(content.includes('尚未核实'));});
  check('empty industry relations show an honest consolidated empty state',()=>{const output=text(api.industryRelations({drivers:[],upstream:[],downstream:[]}));ok(output.includes('尚无已核实的产业链关系'));ok(!output.includes('核心驱动'));});
  for(const key of ['pe_ttm','pb','sharpe'])check(`metric help ${key} handles missing data`,()=>api.openMetricHelp(d.sectors[0],key));
  check('pending stock result is never shown as a zero-return prediction',()=>{eq(api.equityValue({status:'pending',predicted_return_pct:0}),null);ok(api.equityStatus({status:'pending',predicted_return_pct:0}).includes('等待'));});
  check('explicit zero-return baseline stays visible and labeled',()=>{eq(api.equityValue({status:'baseline',predicted_return_pct:0,model:'zero_return'}),0);eq(api.equityStatus({status:'baseline',predicted_return_pct:0,model:'zero_return'}),'基线预测');});
  check('partial portfolio remains explicitly partial',()=>{eq(api.equityValue({status:'partial',predicted_return_pct:.8}),.8);ok(api.equityStatus({status:'partial',predicted_return_pct:.8}).includes('部分覆盖'));});
  check('fund premium requires same-date price and NAV',()=>{eq(api.fundPremium({price:2.2,nav:2,price_date:'2026-11-16',nav_date:'2026-11-13',premium_pct:10}),null);ok(Math.abs(api.fundPremium({price:2.2,nav:2,price_date:'2026-11-16',nav_date:'20261116'})-10)<1e-10);eq(api.fundPremium({price:2.2,nav:0,price_date:'2026-11-16',nav_date:'2026-11-16'}),null);});
  check('fund cards preserve independent dates and do not treat NAV as valuation',()=>{const node=api.fundsPanel({funds:[{code:'TEST.SH',name:'测试行业基金',manager:'测试管理人',broker_relation:'测试券商参股',price:2.2,price_date:'2026-11-16',nav:2,nav_date:'2026-11-13',aum_cny:12000000000,aum_date:'2026-11-12',match_status:'theme_candidate',source_url:'javascript:alert(1)'}]});const output=text(node);ok(output.includes('2026-11-16'));ok(output.includes('2026-11-13'));ok(output.includes('2026-11-12'));ok(output.includes('120.00 亿'));ok(output.includes('持仓待核验'));ok(output.includes('日期不齐 / 待数据'));const links=[];function walk(n){if(n.tag==='a')links.push(n);(n.children||[]).forEach(walk);}walk(node);eq(links.length,0);});
  check('fund panel displays at most three legitimate candidates',()=>{const node=api.fundsPanel({funds:Array.from({length:8},(_,i)=>({code:'fund'+i,name:'候选'+i}))});const output=text(node);ok(output.includes('候选2'));ok(!output.includes('候选3'));});
  check('stock pagination is bounded and clamps after search changes',()=>{const fixture={...d,equity_forecast:{stocks:Array.from({length:5572},(_,i)=>({code:String(i),name:'股票'+i,sector_id:i%2?d.sectors[0].id:d.sectors[1].id,status:'pending'}))}};api.state.equitySearch='';api.state.equitySector='all';api.state.equityPage=0;let rows=api.equityStockPage(fixture);eq(rows.rows.length,30);eq(rows.count,5572);eq(rows.pageCount,186);api.state.equityPage=999;rows=api.equityStockPage(fixture);eq(rows.page,185);eq(rows.rows.length,22);api.state.equitySearch='股票5571';rows=api.equityStockPage(fixture);eq(rows.count,1);eq(rows.page,0);api.state.equitySearch='';api.state.equityPage=0;api.state.equitySector=d.sectors[0].id;eq(api.equityStockPage(fixture).count,2786);api.state.equitySector='all';});
  check('aggregated research forecast never displays an official index target',()=>{const fixture={...d,equity_forecast:{status:'partial',coverage:{universe_count:100,predicted_count:80,weight_coverage:.75},market:{status:'partial',predicted_return_pct:.8,target_kind:'next_open',target_date:'2026-11-17',model:'ridge'}}};const output=text(api.equitySummary(fixture));ok(output.includes('80 / 100'));ok(output.includes('75.0%'));ok(output.includes('+0.80%'));ok(output.includes('部分覆盖'));ok(!output.includes('上证指数'));ok(!output.includes('预测点位'));});
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

  const closeForecast={schema_version:1,status:'ready',model_id:'daily-research-20261117',issued_at:'2026-11-17T01:00:00+08:00',trained_at:'2026-11-17T00:50:00+08:00',target_date:'2026-11-18',target:'next_close_return_pct',unit:'percentage_points',mae_target:.1,
    evaluation:{historical_research:true,note:'历史回溯研究，未来表现待结算',market:{mae_pct:.5,baseline_mae_pct:.6,target_met:false},sector:{mae_pct:1.2,baseline_mae_pct:1.3,target_met:false}},
    market:Array.from({length:6},(_,i)=>({id:'index'+i,name:'预测指数'+i,reference_date:'2026-11-17',reference_close:3000,predicted_return_pct:i===0?0:.2,predicted_close:3006,validation_mae_pct:.5,model_family:'ridge'})),
    sectors:[{id:'801080.SI',name:'电子',rank:1,ranking_score:.99,predicted_return_pct:.6,relative_return_pct:.4,validation_mae_pct:1.2,model_family:'ridge'}],ledger:[{id:'new-daily'}]};
  check('daily close uses its own target date and all six direct indices',()=>{const output=text(api.dailyForecastPanel({...d,daily_forecast:closeForecast}));ok(output.includes('2026-11-18'));ok(output.includes('下一交易日收盘'));ok(output.includes('预测指数5'));ok(output.includes('daily-research-20261117'));ok(output.includes('训练'));ok(output.includes('历史回溯'));});
  check('daily relative strength uses percentage points without a probability claim',()=>{const output=text(api.dailyForecastPanel({...d,daily_forecast:closeForecast}));ok(output.includes('+0.400 个百分点'));ok(output.includes('相对沪深300'));ok(!output.includes('60.0%'));ok(output.includes('不是上涨概率'));});
  check('daily target status derives from numerical MAE even with a contradictory flag',()=>{eq(api.dailyGoal({mae_pct:.5,target_met:true},.1),'尚未达到 0.1 个百分点目标');eq(api.dailyGoal({mae_pct:.1,target_met:false},.1),'历史 MAE 已达到 0.1 个百分点目标');eq(api.dailyGoal({mae_pct:null},.1),'目标待验证');});
  check('daily industry rank and numerical relative return remain distinct',()=>{const f={...closeForecast,sectors:[{id:'a',name:'排序优先',rank:1,ranking_score:5,predicted_return_pct:-.5,relative_return_pct:-.7},{id:'b',name:'幅度优先',rank:2,ranking_score:2,predicted_return_pct:1,relative_return_pct:.8}]};const output=text(api.dailyForecastPanel({...d,daily_forecast:f}));ok(output.includes('按独立模型得分排序'));ok(output.includes('独立排序得分'));ok(output.indexOf('排序优先')<output.indexOf('幅度优先'));ok(output.includes('-0.700 个百分点'));});
  check('constant daily scores do not invent top industry ranks',()=>{const f={...closeForecast,sectors:closeForecast.sectors.map(row=>({...row,rank:null,ranking_score:0}))};const output=text(api.dailyForecastPanel({...d,daily_forecast:f},true));ok(output.includes('排序得分相同或缺失时不排强弱位次'));ok(!output.includes('强弱榜 · 前五'));});
  check('zero index models disclose the baseline meaning without promising unchanged prices',()=>{const f={...closeForecast,market:closeForecast.market.map(row=>({...row,model_family:'zero',predicted_return_pct:0}))};const output=text(api.dailyForecastPanel({...d,daily_forecast:f}));ok(output.includes('指数当前采用零收益基线'));ok(output.includes('尚未获得优于基线的指数预测信号'));ok(output.includes('0% 不代表明天一定不涨跌'));ok(output.includes('基线预测'));});
  check('negative retrospective ranking performance is disclosed with ranking coverage',()=>{const f={...closeForecast,evaluation:{...closeForecast.evaluation,sector_ranking:{daily_rank_ic:-.057593,ranking_coverage:1,ranking_valid_dates:20,dates:20}}};const output=text(api.dailyForecastPanel({...d,daily_forecast:f}));ok(output.includes('-0.0576'));ok(output.includes('有效排序覆盖 100.0%'));ok(output.includes('20 / 20 个交易日'));ok(output.includes('历史回溯尚未验证排序有效'));ok(output.includes('均不是上涨命中率'));});
  check('missing ranking evidence cannot be presented as validated',()=>{const output=text(api.dailyForecastPanel({...d,daily_forecast:closeForecast}));ok(output.includes('历史排序表现待验证'));ok(!output.includes('排序相关为正'));});
  check('daily missing results stay missing and an explicit zero remains valid',()=>{eq(api.dailyForecastValue(closeForecast,{predicted_return_pct:null}),null);eq(api.dailyForecastValue(closeForecast,{predicted_return_pct:0}),0);eq(api.dailyForecastValue({...closeForecast,status:'error'},{predicted_return_pct:5}),null);eq(api.dailyForecastValue({...closeForecast,target:'next_open_return_pct'},{predicted_return_pct:5}),null);});
  check('daily live observations remain separate from retrospective MAE',()=>{let output=text(api.dailyForecastPanel({...d,daily_forecast:closeForecast}));ok(output.includes('前向留存：已结算 0 条'));const f={...closeForecast,forward_evaluation:{settled_rows:37,mae_pct:.8}};output=text(api.dailyForecastPanel({...d,daily_forecast:f}));ok(output.includes('前向留存：已结算 37 条'));ok(output.includes('0.8000 个百分点'));ok(output.includes('0.5000 个百分点'));});
  check('daily errors hide untrusted predictions and preserve the reason',()=>{const output=text(api.dailyForecastPanel({...d,daily_forecast:{...closeForecast,status:'error',reason:'模型完整性检查失败'}}));ok(output.includes('模型完整性检查失败'));ok(!output.includes('预测指数5'));});
  check('daily close is primary and stocks are auxiliary on the forecast page',()=>{api.state.data={...d,daily_forecast:closeForecast};api.state.page='forecast';api.render();const output=text(root);ok(output.indexOf('下一交易日收盘')<output.indexOf('个股辅助研究'));ok(output.includes('尚未达到 0.1 个百分点目标'));api.state.data=d;});
  check('daily industry detail retains a missing forecast instead of zero',()=>{const output=text(api.dailySectorPanel(d.sectors[0],{...d,daily_forecast:closeForecast}));ok(output.includes('待生成'));ok(!output.includes('+0.00%'));});
  check('daily forecast array shapes are validated before rendering',()=>{let error;try{api.validateSnapshot({...d,daily_forecast:{market:{}}});}catch(err){error=err;}ok(error?.message.includes('daily_forecast.market'));});
  return {passed, failures};
}
