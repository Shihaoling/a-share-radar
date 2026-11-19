'use strict';

// All remote text enters the document through textContent. No data is evaluated.
const $ = (q, root = document) => root.querySelector(q);
const NS = 'http://www.w3.org/2000/svg';
const local = { get(k, fallback) { try { return JSON.parse(localStorage.getItem(k)) ?? fallback; } catch { return fallback; } }, set(k,v) { try { localStorage.setItem(k,JSON.stringify(v)); } catch {} } };
const state = { page: 'overview', demo: false, data: null, loading: true, group: '全部', search: '', sort: 'change', taxonomy:'sw', pageQuotes:null, selected: null, marketSelected:null, overviewSelected:null, range: 21, metric: 'intraday', newsFilter: '全部', newsSearch:'', newsLimit:12, equitySearch:'', equitySector:'all', equityPage:0, favorites: local.get('guanlan.favorites',[]), preferences: local.get('guanlan.preferences',{showRumors:false,autoRefresh:true}), toast: null };
const pages = [['overview','市场总览','grid'],['sectors','板块雷达','layers'],['news','A股关联资讯','globe'],['forecast','预测实验室','activity'],['review','每日复盘','chart'],['settings','数据设置','settings']];
const svgPaths = {
  grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  layers:'m12 3 10 6-10 6L2 9z M2 13l10 6 10-6 M2 17l10 6 10-6',
  globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M3 12h18 M12 3a18 18 0 0 1 0 18 18 18 0 0 1 0-18',
  activity:'M2 12h4l3-8 6 16 3-8h4',chart:'M4 3v17h17 M8 15l4-5 4 2 5-7',
  settings:'M9 3h6l1 4 4 1v8l-4 1-1 4H9l-1-4-4-1V8l4-1z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  search:'m20 20-5-5 M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0',refresh:'M20 8a8 8 0 0 0-14-3L3 8 M3 3v5h5 M4 16a8 8 0 0 0 14 3l3-3 M21 21v-5h-5',
  arrow:'M4 12h16 M14 6l6 6-6 6',download:'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5',info:'M12 11v6 M12 7v1 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  shield:'M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7z M8 12l3 3 5-6',database:'M21 6c0 2-4 3-9 3S3 8 3 6s4-3 9-3 9 1 9 3 M3 6v12c0 2 4 3 9 3s9-1 9-3V6 M3 12c0 2 4 3 9 3s9-1 9-3',
  clock:'M12 7v5l4 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',external:'M14 3h7v7 M21 3 10 14 M10 3H3v18h18v-7',check:'m4 12 5 5L20 6',file:'M14 2H4v20h16V8z M14 2v6h6 M8 13h8 M8 17h6',logo:'M3 17 8 10l5 4 8-10 M3 22h18'
};
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  Object.entries(props).forEach(([key,value]) => {
    if(value == null || value === false) return;
    if(key==='class') el.className=value;
    else if(key==='text') el.textContent=value;
    else if(key==='checked') el.checked=Boolean(value);
    else if(key==='value') el.value=value;
    else if(key.startsWith('on')) el.addEventListener(key.slice(2).toLowerCase(),value);
    else el.setAttribute(key,value===true?'':String(value));
  });
  children.flat(Infinity).forEach(child => { if(child!==null && child!==undefined && child!==false) el.append(child instanceof Node ? child : document.createTextNode(String(child))); });
  return el;
}
function s(tag, attrs={}) { const el=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));return el; }
function icon(name,size=17) { const el=s('svg',{viewBox:'0 0 24 24',width:size,height:size,fill:'none',stroke:'currentColor','stroke-width':1.55,'stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'});el.append(s('path',{d:svgPaths[name]||svgPaths.grid}));return el; }
const finite = v => v!==null && v!==undefined && v!=='' && Number.isFinite(Number(v));
const num = (v,d=2) => finite(v)?Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const pct = (v,d=2) => finite(v)?`${Number(v)>0?'+':''}${num(v,d)}%`:'—';
const accuracy = v => finite(v)?`${num(Number(v)*100,1)}%`:'—';
const sign = v => !finite(v)?'neutral':Number(v)>0?'up':Number(v)<0?'down':'neutral';
function amount(v) { if(!finite(v))return'—'; const n=Number(v);return Math.abs(n)>=1e12?`${num(n/1e12,2)} 万亿`:`${num(n/1e8,2)} 亿`; }
function dateText(value,short=false) {if(!value)return'尚未更新';if(/^\d{4}-\d{2}-\d{2}$/.test(String(value)))return String(value);const d=new Date(value);if(Number.isNaN(d.getTime()))return String(value);return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',...(short?{}:{month:'2-digit',day:'2-digit'}),hour:'2-digit',minute:'2-digit',hour12:false}).format(d);}
function safeURL(url){try{const u=new URL(url);return ['http:','https:'].includes(u.protocol)?u.href:null;}catch{return null;}}
function link(text,url,className=''){const safe=safeURL(url);return safe?h('a',{href:safe,target:'_blank',rel:'noopener noreferrer',class:className},text):h('span',{class:className},text);}
function go(page) {if(page==='news'&&state.page!==page)state.newsLimit=12;state.page=page;history.replaceState(null,'',`#${page}`);render();window.dispatchEvent(new CustomEvent('radar:navigate',{detail:{page,title:pages.find(x=>x[0]===page)?.[1]||page}}));window.scrollTo({top:0,behavior:'instant'});}
function button(text,fn,className='outline-button',ico){return h('button',{class:className,type:'button',onClick:fn},ico?icon(ico,13):null,text);}
function pill(text,cls=''){return h('span',{class:`tag ${cls}`},text);}
function empty(title,body,ico='database'){return h('div',{class:'empty'},icon(ico,26),h('strong',{},title),h('p',{},body));}
function panel(title,subtitle,body,tools=null,cls=''){return h('section',{class:`panel ${cls}`},h('div',{class:'panel-head'},h('div',{},h('h3',{class:'panel-title'},title),subtitle?h('div',{class:'panel-subtitle'},subtitle):null),tools),body);}
function segmented(options,value,fn){return h('div',{class:'segmented'},options.filter(([v])=>!relayOnly||v!=='eastmoney').map(([v,label])=>button(label,()=>fn(v),value===v?'active':'')));}
function toast(message){state.toast=message;render();setTimeout(()=>{state.toast=null;render();},3000);}
function download(name,content,type='application/json'){const a=h('a',{href:URL.createObjectURL(new Blob([content],{type})),download:name});document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
function baseData(){return{schema_version:1,generated_at:null,data_mode:'live',status:'pending',market_session:{label:'待更新交易状态'},market:[],sectors:[],news:[],predictions:[],ledger:[],metrics:{},models:{},sources:[],notices:[],intraday:{}};}
function data(){const result=state.demo?demoData(state.data):{...baseData(),...(state.data||{})};return normalizeDashboard(result);}

function isCanonicalSector(s){return s.taxonomy==='SW2021'&&(!s.level||s.level==='L1');}
const relayOnly=document.querySelector('meta[name="radar-live-quotes"]')?.content==='sxdata';
state.overviewIndustryView={metric:'intraday',range:21};
if(relayOnly)state.taxonomy='sse';
function quoteSnapshotStamp(snapshot){return String(snapshot?.collector?.checked_at||snapshot?.generated_at||'');}
function currentPageQuotes(){
 const full=state.pageQuotes;
 const latest=state.liveQuotes;
 if(!latest||quoteSnapshotStamp(latest)<quoteSnapshotStamp(full))return full;
 if(full?.trade_date!==latest.trade_date)return latest;
 return {...full,...latest};
}
function normalizeDashboard(raw){
 const d={...baseData(),...raw};const canonical=d.sectors.filter(isCanonicalSector);
 if(canonical.length)d.sectors=canonical;d.sectors=Array.from(new Map(d.sectors.map(s=>[s.id,s])).values());
 if(relayOnly&&!state.demo){
  const research=state.cloudResearch;
  if(research){
   const withResearch=item=>({...item,...research.entities?.[item.id]});
   d.market=d.market.map(withResearch);
   d.sectors=d.sectors.map(withResearch);
   for(const key of ['daily_review','data_checks','validation_summary','equity_forecast','fund_summary','fund_state','equity_collection','ledger']){
    if(research[key]!=null)d[key]=research[key];
   }
   d.research_as_of=research.generated_at;
  }
  const feed=state.liveQuotes?.source==='sxdata_relay'?state.liveQuotes:null;
  const trustedQuote=quote=>quote?.provider==='stock_api'?quote:undefined;
  const trustedDaily=item=>{
   const additions=Object.values(state.cloudDaily?.bars?.[item.id]||{}).sort((a,b)=>a.date.localeCompare(b.date));
   const latest=additions.at(-1),capHistory=state.cloudDaily?.market_caps?.[item.id]||{};
   const capDate=Object.keys(capHistory).sort().at(-1)||latest?.date,cap=capHistory[capDate]??latest?.market_cap_cny;
   const hist=new Map((item.history||[]).filter(bar=>(bar.provider||bar.source||item.provider)==='stock_api').map(bar=>[bar.date,bar]));
   for(const bar of additions)hist.set(bar.date,bar);
   const caps=new Map((item.market_cap_history||[]).map(row=>[row.date,row]));
   for(const row of additions)if(finite(row.market_cap_cny))caps.set(row.date,{date:row.date,value:row.market_cap_cny});
   for(const [date,value] of Object.entries(capHistory))caps.set(date,{date,value});
   const official=latest&&latest.date>=(item.daily_quote?.quote_date||'')?{
    quote_date:latest.date,quote_timestamp:latest.as_of,price:latest.close,change_pct:latest.change_pct,
    timestamp_quality:'daily_close',provider:'stock_api',
   }:trustedQuote(item.daily_quote);
   return {
   ...item,
   price:trustedQuote(item)?.price,
   daily_quote:official,
   live_quote:trustedQuote(item.live_quote),
   page_quote:undefined,
   history:[...hist.values()].sort((a,b)=>a.date.localeCompare(b.date)),
   market_cap:finite(cap)?cap:item.market_cap,market_cap_date:finite(cap)?capDate:item.market_cap_date,
   market_cap_history:[...caps.values()].sort((a,b)=>a.date.localeCompare(b.date)),
   };
  };
  const update=(entity,quotes)=>{
   const item=trustedDaily(entity);
   const cached=item.live_quote;
   const incoming=quotes?.find(quote=>quote.id===entity.id);
   const newest=incoming&&incoming.quote_timestamp>=(cached?.quote_timestamp||'')?incoming:cached;
   return {...item,live_quote:newest};
  };
  const marketById=new Map(d.market.map(item=>[item.id,item]));
  for(const item of feed?.market||[])if(!marketById.has(item.id))marketById.set(item.id,item);
  d.market=[...marketById.values()].map(entity=>update(entity,feed?.market));
  const stockFeed=state.relayStocks?.trade_date===chinaDay(new Date())?state.relayStocks.stocks:[];
  const stocks=new Map(stockFeed.map(q=>[q.id,q]));
  const estimates=new Map((state.relayStocks?.trade_date===chinaDay(new Date())?state.relayStocks.board_estimates||[]:[]).map(q=>[q.id,q]));
  const withStockReference=st=>{
   const record=state.cloudDaily?.stocks?.[st.ts_code];
   const stock=record&&record.quote_date>=(st.quote_date||'')?{...st,...record}:st;
   const flow=research?.moneyflow?.stocks?.[st.ts_code];
   return flow?{...stock,moneyflow:{...flow,date:research.moneyflow.trade_date}}:stock;
  };
  const referenceStocks=new Map(d.sectors.flatMap(entity=>(entity.stocks||[]).map(st=>[st.ts_code,withStockReference(st)])));
  d.sectors=d.sectors.map(entity=>({
   ...trustedDaily(entity),
   proxy:undefined,
   intraday_estimate:estimates.get(entity.id),
   stocks:entity.stocks?.filter(st=>st.provider==='stock_api')
    .map(st=>({...trustedDaily(withStockReference(st)),live_quote:stocks.get(st.ts_code)||trustedQuote(st.live_quote)})),
  }));
  if(['sectors','overview'].includes(state.page)&&state.taxonomy==='sse')d.sectors=(feed?.sectors||[]).map(item=>({
   ...item,stocks:(item.reference?.stocks||[]).map(row=>{
    const quote=stocks.get(row.ts_code),ref=referenceStocks.get(row.ts_code);
    return {...ref,...row,name:quote?.name||ref?.name||row.ts_code,live_quote:quote};
   }),
  }));
  mergeCloudNews(d,state.cloudNews);
  const models=state.cloudModels;
  if(models?.schema_version===1){
   d.daily_forecast=models.daily_forecast||d.daily_forecast;
   d.direction_probability=models.direction_probability||d.direction_probability;
   d.cloud_model_status=models.status;
  }
  const sessions={morning:'上午交易中',afternoon:'下午交易中',midday_break:'午间休市',closing_auction:'收盘集合竞价',post_close:'已收盘',closed:'休市'};
  if(feed?.trade_date===chinaDay(new Date())&&sessions[feed.collector?.session]){
   d.market_session={...d.market_session,label:sessions[feed.collector.session]};
  }
  d.eastmoney_sectors=[];
  d.sources=[...d.sources.filter(source=>source.id!=='eastmoney_page'),{id:'sxdata_relay',name:'SXData 盘中中转',provider:'stock_api',status:feed?.collector?.status||'pending',type:'intraday',message:feed?.collector?.min_gap_seconds?`行情请求最短间隔 ${feed.collector.min_gap_seconds} 秒。各报价保留来源时间。`:'行情分批读取，各报价保留来源时间。'}];
  return d;
 }
 const page=state.demo?null:currentPageQuotes();
 if(page?.schema_version===1){
  d.page_collector=page.collector;d.eastmoney_sectors=page.sectors||[];
  d.sources=[...d.sources.filter(x=>x.id!=='eastmoney_page'),{id:'eastmoney_page',name:'东方财富网页采样',type:'page_crawler',provider:'eastmoney_page',url:'https://quote.eastmoney.com/center/hszs.html',status:page.collector?.status||'pending',last_success:page.collector?.last_success,message:'五分钟采样；每轮最多两页行业、一次翻页；失败冷却30分钟'}];
  d.market=d.market.map(e=>({...e,page_quote:(page.market||[]).find(q=>q.id===e.id)}));
  d.intraday={...d.intraday};for(const [id,bucket] of Object.entries(page.intraday||{})){
   const prior=Array.isArray(d.intraday[id])?d.intraday[id]:d.intraday[id]?.points||[];
   d.intraday[id]={points:[...prior,...(bucket.points||[])]};
  }
 }
 if(['sectors','overview'].includes(state.page)&&state.taxonomy==='eastmoney')d.sectors=(d.eastmoney_sectors||[]).map(e=>({...e,page_quote:e}));
 return d;
}
function retainDayDigest(incoming,previous,day){
 const date=incoming.date||day;
 if(incoming.summary||!previous?.summary||previous.date!==date)return {...incoming,date};
 return {...previous,date,status:incoming.status==='temporarily_unavailable'?incoming.status:'updating',
  retry_after:incoming.retry_after,current_article_count:incoming.current_article_count};
}
function mergeCloudNews(d,news){
 if(!news)return;
 const articles=new Map(d.news.map(article=>[article.id,article]));
 for(const article of [...(news.recent_articles||[]),...news.articles])articles.set(article.id,article);
 d.news=[...articles.values()].sort((a,b)=>String(b.published_at||b.first_seen_at||'').localeCompare(String(a.published_at||a.first_seen_at||'')));
 const digest=news.news_digest;
 if(digest&&(digest.date||news.trade_date)>=(d.news_digest?.date||'')){
  const day=digest.date||news.trade_date;
  const effective=retainDayDigest(digest,d.news_digest,day);
  if(!effective.summary){
   d.previous_news_digest=[d.news_digest,news.previous_digest]
    .filter(item=>item?.summary&&item.date<day).sort((a,b)=>b.date.localeCompare(a.date))[0];
  }
  d.news_digest=effective;
 }
}
function taxonomyLabel(d){if(relayOnly&&!state.demo&&['sectors','overview'].includes(state.page)&&state.taxonomy==='sse')return d.sectors.length?`交易所行业与主题 · ${d.sectors.length} 个指数`:'行业行情等待更新';if(!relayOnly&&['sectors','overview'].includes(state.page)&&state.taxonomy==='eastmoney')return'东方财富行业 · 网页采样';return d.sectors.length&&d.sectors.every(isCanonicalSector)?`申万 2021 · ${d.sectors.length} 个一级行业`:'行业分类等待更新';}
function industryOptions(){return relayOnly&&!state.demo?[['sse','交易所行业与主题'],['sw','申万行业 · 权重股估算']]:[['sw','申万行业'],['eastmoney','东方财富 · 网页采样']];}
function providerName(value){return({stock_api:relayOnly?'SXData':'Tushare',tushare:'Tushare',tencent:'腾讯行情',sina:'新浪行情',eastmoney:'东方财富',baostock:'BaoStock',eastmoney_page:'东方财富网页'})[value]||value||'来源待确认';}
function chinaDay(value){if(!value)return null;const text=String(value);if(/^\d{8}$/.test(text))return`${text.slice(0,4)}-${text.slice(4,6)}-${text.slice(6,8)}`;if(/^\d{4}-\d{2}-\d{2}$/.test(text))return text;const d=new Date(value);return Number.isFinite(d.getTime())?new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai'}).format(d):null;}
function quoteTime(value,daily=false){if(!value)return null;let text=String(value);if(/^\d{8}$/.test(text))text=chinaDay(text);if(/^\d{4}-\d{2}-\d{2}$/.test(text))text+=daily?'T15:00:00+08:00':'T00:00:00+08:00';const t=new Date(text).getTime();return Number.isFinite(t)?t:null;}
const chinaClock=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
function indexUpdate(q){
 const stamp=q.time!==null&&q.time!==undefined?chinaClock.format(q.time):'';
 return q.day&&stamp?`指数更新时间 ${q.is_today?'':q.day+' '}${stamp}`:'指数更新时间待确认';
}
function displayQuote(entity,now=new Date()){
  const today=chinaDay(now),nowMs=new Date(now).getTime(),candidates=[];
  const add=(value,origin)=>{if(!value||!finite(value.price))return;const quality=value.timestamp_quality||'',daily=quality==='daily_close',stamp=value.quote_timestamp||value.as_of||value.quote_date||value.date;const day=chinaDay(value.quote_date||stamp),time=quoteTime(stamp,daily);const trusted=daily||['source_reported','exchange_timestamp'].includes(quality);if(day&&day>today||time&&time>nowMs+60000)return;let change=finite(value.change_pct)?Number(value.change_pct):finite(value.previous_close)&&Number(value.previous_close)>0?(Number(value.price)/Number(value.previous_close)-1)*100:null;candidates.push({price:Number(value.price),change_pct:change,as_of:stamp,day,time,quality,daily,trusted,origin,provider:value.provider||entity.provider});};
  add(entity,'primary');add(entity.daily_quote,'daily');add(entity.live_quote,'live');
  if(!candidates.some(q=>q.daily)){const bars=(entity.history||[]).filter(b=>finite(b.close)&&b.complete!==false&&b.is_complete!==false&&chinaDay(b.date)<=today&&(chinaDay(b.date)<today||b.complete===true||nowMs>=quoteTime(b.date,true)+15*60000)).slice().sort((a,b)=>String(a.date).localeCompare(String(b.date)));const last=bars.at(-1);if(last)add({...last,price:last.close,as_of:last.as_of||last.date,timestamp_quality:'daily_close',provider:last.provider||last.source||entity.provider},'history');}
  const confirmed=candidates.filter(q=>q.trusted&&q.day&&q.time!==null),pool=confirmed.length?confirmed:candidates;
  pool.sort((a,b)=>String(b.day||'').localeCompare(String(a.day||''))||(b.time||0)-(a.time||0)||(b.daily?1:0)-(a.daily?1:0)||(b.origin==='live'?1:0)-(a.origin==='live'?1:0));
  const finalDaily=confirmed.filter(x=>x.daily&&x.day===today).sort((a,b)=>(b.time||0)-(a.time||0));
  let q=(nowMs>=quoteTime(today,true)&&finalDaily.length?finalDaily[0]:pool[0])||{price:null,change_pct:null,as_of:null,day:null,origin:'none',provider:entity.provider};
  const page=entity.page_quote,observed=quoteTime(page?.observed_at),pageDay=chinaDay(page?.observed_at);
  if(page&&finite(page.price)&&page.timestamp_quality==='page_observed'&&pageDay<=today&&observed<=nowMs+60000&&(q.quality==='page_observed'||!finite(q.price)||pageDay===today&&(!q.is_today&&q.day!==today||!q.daily&&(!q.time||observed-q.time>300000)))){
   q={price:Number(page.price),change_pct:page.change_pct,as_of:page.observed_at,day:pageDay,time:observed,quality:'page_observed',daily:false,trusted:false,origin:'page',provider:'eastmoney_page'};
  }
  const finalToday=candidates.some(x=>x.daily&&x.day===today&&x.trusted),isToday=q.day===today;
  let label='等待行情';if(finite(q.price)){if(q.origin==='page')label=q.day===today?'网页观测 · 非交易所时点':`${q.day} 网页观测`;else if(!q.trusted)label='时点未确认';else if(q.daily)label=isToday?'今日收盘':`${q.day} 收盘`;else if(isToday)label=nowMs>=quoteTime(today,true)?finalToday?'今日行情 · 日线已更新':'今日行情 · 待最终日线':'当日行情快照';else label=`${q.day||'日期未确认'} 历史快照`;}
  return{...q,label,is_today:isToday,has_today_daily:finalToday,stale:!isToday};
}
function selectedMarket(d){return d.market.find(x=>x.id===state.marketSelected)||d.market[0]||{};}
function quoteCaption(q){return h('div',{class:`quote-caption ${q.is_today?'current':''}`},h('span',{},q.label),h('span',{},q.as_of?dateText(q.as_of,false):'暂无确认时点'),h('span',{},providerName(q.provider)));}
function sentimentLabel(s){if(!(Number(s?.article_count)>0))return'暂无覆盖';return({positive:'偏正面',negative:'偏负面',mixed:'观点分歧',neutral:'中性',unknown:'暂无覆盖'})[s?.label]||'暂无覆盖';}
function sentimentClass(s){return s?.label==='positive'?'up':s?.label==='negative'?'down':s?.label==='mixed'?'rumor':'';}
function sentimentSummary(entity){const st=entity.sentiment||{};return h('div',{class:'sentiment-inline'},pill(sentimentLabel(st),sentimentClass(st)),h('span',{},`${st.article_count||0} 条关联消息`));}
function sentimentPanel(entity,d){const st=entity.sentiment||{},evidence=Array.isArray(st.evidence)?st.evidence:[],method=String(st.method||''),methodLabel=/model|minimax|finbert|transformer|neural/i.test(method)?/rule|lexicon|keyword/i.test(method)?'金融文本模型与规则综合':'金融文本模型':/rule|lexicon|keyword/i.test(method)?'规则分析':'分析方式待确认';return panel(`${entity.name||'市场'} · 新闻情绪`,`${st.window_hours?`近 ${st.window_hours} 小时 · `:''}消息语气，不代表股价涨跌概率`,h('div',{class:'sentiment-body'},h('div',{class:'sentiment-overview'},h('div',{},pill(sentimentLabel(st),sentimentClass(st)),h('strong',{class:'sentiment-score'},st.article_count>0&&finite(st.score)?`${Number(st.score)>0?'+':''}${num(st.score,2)}`:'—'),h('span',{class:'panel-subtitle'},'语气分值 · −1 至 +1')),h('div',{class:'sentiment-counts'},h('strong',{},st.article_count||0),h('span',{},'关联消息'),h('strong',{},st.source_count||0),h('span',{},'信息来源'))),h('div',{class:'sentiment-method'},methodLabel,' · ',st.as_of?dateText(st.as_of,false):'尚未形成判断'),st.article_count>0&&finite(st.confidence)?h('p',{class:'inline-note'},st.confidence_kind==='model_softmax_uncalibrated'?`模型分类分数 ${accuracy(st.confidence)}，尚未校准，不代表判断准确率。`:`综合证据分数 ${accuracy(st.confidence)}，未经准确率校准，不代表预测胜率。`):null,evidence.length?h('div',{class:'sentiment-evidence'},evidence.slice(0,5).map(e=>h('article',{},h('div',{class:'news-meta'},e.label?pill(sentimentLabel({...e,article_count:1}),sentimentClass(e)):null,e.source||'来源待确认',e.published_at?dateText(e.published_at,false):'发布时间未确认'),link(e.title||'查看消息原文',e.url),e.relevance_note?h('p',{class:'inline-note'},e.relevance_note):Array.isArray(e.associations)&&e.associations.some(a=>(a.terms||[]).length)?h('p',{class:'inline-note'},'关联线索：',Array.from(new Set(e.associations.flatMap(a=>a.terms||[]))).slice(0,6).join('、')):null))):h('div',{class:'info-strip'},'当前时间窗内尚无足够关联消息。暂无覆盖不等于中性，也不推断上涨或下跌。'),st.note?h('p',{class:'inline-note'},st.note):null));}

function sectorName(id,d=data()){return d.sectors.find(x=>x.id===id)?.name||(state.data?.sectors||[]).find(x=>x.id===id)?.name||id||'整体市场';}
function selectedSector(d=data()){return d.sectors.find(s=>s.id===state.selected)||d.sectors[0];}
function changeDemo(){state.demo=!state.demo;state.selected=null;render();}
function setPreference(name,value){state.preferences[name]=value;local.set('guanlan.preferences',state.preferences);render();}
function sourceCoverage(d){
 const quotes=d.sectors.map(sector=>displayQuote(sector)).filter(quote=>finite(quote.price));
 return {quoted:quotes.length,today:quotes.filter(quote=>quote.is_today).length,total:d.sectors.length,
  proxy:d.sectors.filter(s=>finite(displayQuote(s.proxy||{}).price)).length,healthy:d.sources.filter(s=>['ok','success','healthy'].includes(s.status)).length};
}

function sectorQuote(sec){
 const own=displayQuote(sec),estimate=sec.intraday_estimate;
 if(estimate&&estimate.quote_date===chinaDay(new Date())&&(!own.daily||!own.is_today))
  return {value:estimate.change_pct,price:estimate.price,day:estimate.quote_date,quote:own,estimate:true};
 const proxy=displayQuote(sec.proxy||{}),useProxy=!finite(own.price)&&finite(proxy.price);
 return {value:useProxy?proxy.change_pct:own.change_pct,day:useProxy?proxy.day:own.day,price:useProxy?proxy.price:own.price,quote:useProxy?proxy:own,proxy:useProxy};
}
function validationStatus(entity){const v=entity?.validation;return typeof v==='string'?v:v?.status||null;}
function validationBadge(entity){const status=validationStatus(entity);const map={verified:['多源已核对','reliable'],single_source:['单源 · 未交叉核验','rumor'],conflict:['数据冲突 · 暂停预测','up'],not_comparable:['暂无同日核验','rumor']};return status?pill(...(map[status]||['待交叉核验',''])):null;}
function validationDetail(entity){const v=entity?.validation;if(!v)return null;return h('div',{class:`validation-note ${validationStatus(entity)==='conflict'?'conflict':''}`},validationBadge(entity),h('span',{},typeof v==='object'?v.message||v.reason||v.note||'以同一标的、交易日期与价格口径核对。':'以同一标的、交易日期与价格口径核对。'));}
function checkSummary(d){const checks=d.data_checks||[];const counts={verified:0,single_source:0,conflict:0,not_comparable:0};checks.forEach(x=>{if(x.status in counts)counts[x.status]++;});return panel('行情交叉验证','同一标的、同一交易时点、同一价格口径才可比较',h('div',{class:'form-body'},h('div',{class:'tags'},pill(`已核对 ${counts.verified}`,'reliable'),pill(`单源 ${counts.single_source}`,'rumor'),pill(`冲突 ${counts.conflict}`,counts.conflict?'up':''),pill(`不可比 ${counts.not_comparable}`)),checks.length?h('div',{class:'sources-list',style:'padding:10px 0 0'},checks.map(check=>h('div',{class:'source-row'},h('div',{},h('strong',{},check.name||sectorName(check.entity_id,d)),h('p',{},check.message||check.reason||check.note||'需核对相同交易日期与口径')),validationBadge({validation:check})))):h('p',{class:'inline-note'},'等待多源采集结果。数据源连接成功不等于行情已通过交叉验证。'),counts.conflict?h('div',{class:'validation-note conflict'},'存在冲突的标的暂停预测，待来源与口径核实后恢复。'):null));}


function intradayFor(d,entity,sector){const keys=[sector&&entity?.code?`${sector.id}:proxy:${entity.code}`:null,entity?.id,entity?.code,sector?`${sector.id}:proxy`:null].filter(Boolean);let rows=[];for(const k of keys){const bucket=d.intraday?.[k],points=Array.isArray(bucket)?bucket:bucket?.points;if(Array.isArray(points)&&points.length){rows=points;break;}}rows=rows.map(x=>({...x,timestamp:x.quote_timestamp||x.timestamp||(x.timestamp_quality==='page_observed'?x.observed_at:null)})).filter(x=>finite(x.price)&&x.timestamp&&Number.isFinite(new Date(x.timestamp).getTime())).sort((a,b)=>new Date(a.timestamp)-new Date(b.timestamp));if(!rows.length)return[];const day=t=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai'}).format(new Date(t));const latest=day(rows.at(-1).timestamp);const todayRows=rows.filter(x=>day(x.timestamp)===latest),primary=todayRows.filter(x=>x.provider==='stock_api'&&x.kind==='historical_minute'&&x.source_code===(entity.index_code||entity.code||(entity.id?.startsWith('sh')?entity.id.slice(2)+'.SH':entity.id?.startsWith('sz')?entity.id.slice(2)+'.SZ':entity.id)));return primary.length>=2?primary:todayRows;}
function relayIntraday(snapshot, latest, item, day, recovery){
 const minutes=new Map();
 const start=quoteTime(day+'T09:30:00+08:00');
 function add(row,quality){
  if(!Array.isArray(row))return;
  const [timestamp,price]=row;
  const time=quoteTime(timestamp),seconds=(time-start)/1000,minute=Math.floor(seconds/60);
  if(typeof price!=='number'||!Number.isFinite(price)||price<=0||time===null||minute<0||minute>330||minute>120&&minute<210)return;
  const prior=minutes.get(minute);
  if(!prior||(prior.timestamp_quality==='minute_backfill'&&quality==='source_reported')||time>=prior.time)
   minutes.set(minute,{timestamp,price,time,minute,seconds,timestamp_quality:quality});
 }
 for(const [feed,quality] of [[recovery,'minute_backfill'],[snapshot,'source_reported']]){
  const rows=feed?.trade_date===day?feed.series?.[item.id]:null;
  if(Array.isArray(rows))for(const row of rows)add(row,quality);
 }
 if(latest?.quote_date===day&&latest.timestamp_quality==='source_reported')add([latest.quote_timestamp,latest.price],'source_reported');
 return [...minutes.values()].sort((a,b)=>a.minute-b.minute);
}
function todayRelayTicks(item,day){
 const feed=state.liveQuotes;
 const quote=feed?.trade_date===day?[...feed.market,...feed.sectors].find(q=>q.id===item.id):null;
 return relayIntraday(state.relayIntraday,quote,item,day,state.minuteRecovery);
}
function tickNote(rows){if(!rows.length)return h('div',{class:'tick-note'},'今日尚无分时采样。交易时段约每五分钟采集，取得两个真实样本后绘图。');const observed=rows.at(-1).timestamp_quality==='page_observed',historical=rows.every(p=>p.provider==='stock_api'&&p.kind==='historical_minute'&&p.frequency==='1min');return h('div',{class:'tick-note'},`${observed?'网页观测时间':'报价时间'} ${dateText(rows.at(-1).timestamp,false)} · ${rows.length} 个真实样本 · ${historical?'收盘后 · 真实一分钟数据':'盘中约五分钟更新，调度可能延迟'}`);}
function liveQuoteBlock(entity){const q=entity?.live_quote;if(!q||!finite(q.price))return null;const stamp=q.quote_timestamp||q.timestamp||q.fetched_at;return h('div',{class:'live-quote-block'},h('span',{},'行情快照'),h('strong',{},num(q.price,entity.basis==='etf'?3:2)),h('span',{},`${q.timestamp_quality==='retrieved_only'?'仅采集时间（源时点未确认）':'来源时间'} ${stamp?dateText(stamp,false):'未提供'}`),h('span',{},q.provider||''));}
function quantItem(entity,key){const vendorKey=key==='pe_ttm'?finite(entity?.vendor_pe)?'vendor_pe':finite(entity?.vendor_pe_ttm)?'vendor_pe_ttm':null:key==='pb'&&finite(entity?.vendor_pb)?'vendor_pb':null;if(vendorKey){const isTTM=vendorKey==='vendor_pe_ttm';return{value:Number(entity[vendorKey]),label:key==='pb'?(entity.basis==='index'||entity.level==='L1'?'供应商指数 PB':'供应商 PB'):isTTM?'供应商 PE (TTM)':'供应商 PE（未声明 TTM）',units:'倍',as_of:entity.vendor_valuation_date||null,status:'vendor',vendor:true,vendor_key:vendorKey,definition:entity[vendorKey+'_definition']||null,reason:'直接展示供应商发布值，估值日期独立于盘中行情；不是本网站按归母财务计算的整体估值。'};}const x=entity?.quant_metrics?.[key];return x&&typeof x==='object'?x:{value:finite(x)?Number(x):null,reason:'尚无可用指标；需要同一日期、同一口径的完整输入。'};}
const metricInfo={pe_ttm:{label:'PE (TTM)',formula:'PE_TTM = Σ 成分股总市值 ÷ Σ 成分股归母净利润_TTM',meaning:'用整体市值与整体过去十二个月净利润衡量估值。净利润应纳入亏损公司；不能把各股票 PE 简单求平均。整体利润不为正时PE不适用；覆盖不足时只能显示样本组合估值，不能视为整个板块。',caution:'市值、归母利润和成分必须有一致口径及可追溯日期。ETF价格不能作为板块市值，也不能用ETF价格推算板块PE。'},pb:{label:'PB',formula:'PB = Σ 成分股总市值 ÷ Σ 成分股归母净资产',meaning:'衡量市场定价相对于归母净资产的倍数。使用整体法，不把各股票 PB 简单平均。整体净资产不为正时PB不适用；覆盖不足时只能解释为样本组合。',caution:'财务报告发布时间必须早于指标生成时点。参考ETF价格与持仓并不自动代表完整板块，不能替代板块PB。'},sharpe:{label:'夏普比率',formula:'r_t = P_t / P_(t−1) − 1\nr_f,d = (1 + r_f,annual)^(1/252) − 1\nSharpe = √252 × mean(r_t − r_f,d) / sampleSTD(r_t − r_f,d)',meaning:'每单位收益波动对应的平均超额收益，越高不等于未来一定更好。标准差采用样本标准差（n−1），零波动或样本不足时不输出数值。',caution:'默认年无风险利率2%是可配置的研究假设，不是当前市场利率。未复权收盘价算的是价格收益，不是包含分红的总回报；ETF代理夏普仅描述该ETF，不等同于板块。'}};
function openMetricHelp(entity,key){const m=quantItem(entity,key),info=m.vendor?{label:m.label,formula:key==='pb'?'一般定义：PB = 股价 ÷ 每股净资产 = 总市值 ÷ 同口径净资产':m.vendor_key==='vendor_pe_ttm'?'一般定义：PE (TTM) = 股价 ÷ 每股收益 (TTM) = 总市值 ÷ 对应 TTM 利润':'一般定义：PE = 股价 ÷ 每股收益 = 总市值 ÷ 对应利润',meaning:(m.definition?m.definition+'。':'')+(key==='pb'?'一般公式适用于同口径股数与净资产。此处为供应商发布值，其指数聚合、负净资产处理及财务归属口径尚未核实。':m.vendor_key==='vendor_pe_ttm'?'TTM声明来自供应商原数据；亏损处理与指数聚合细则尚未核实。':'一般公式适用于同口径股数与利润。此供应商PE未说明利润周期、亏损处理与指数聚合细则，不能视为TTM。'),caution:'此处是供应商发布值，未在本网站用归母利润或归母净资产重算。一般定义不代表已经确认供应商的具体计算办法；行情时间和估值日期相互独立。'}:metricInfo[key],rf=m.risk_free_annual??m.rf_annual??m.risk_free_rate_annual??.02;const coverage=m.coverage??m.coverage_ratio;const coverageText=coverage&&typeof coverage==='object'?`${coverage.used_count??'—'} / ${coverage.total_count??'—'} 只；数量覆盖 ${accuracy(coverage.count_ratio)}；市值覆盖 ${accuracy(coverage.market_cap_ratio)}`:finite(coverage)?accuracy(coverage):coverage||'未提供';const dialog=h('dialog',{class:'metric-dialog','aria-labelledby':'metric-dialog-title'},h('div',{class:'metric-dialog-head'},h('h3',{id:'metric-dialog-title'},`${entity.name||'标的'} · ${info.label}`),button('关闭',()=>dialog.close(),'outline-button')),h('p',{class:'metric-meaning'},info.meaning),h('pre',{class:'metric-formula'},info.formula),h('div',{class:'kv'},h('span',{},'当前数值'),h('span',{},`${num(m.value)}${m.units?' '+m.units:''}`)),h('div',{class:'kv'},h('span',{},'计算口径'),h('span',{},m.label||'未提供')),key==='sharpe'?h('div',{class:'kv'},h('span',{},'样本要求'),h('span',{},`至少 ${m.min_sample_count??60} 个，最多 ${m.max_sample_count??252} 个日收益；跨缺失日不视作单日收益`)):null,h('div',{class:'kv'},h('span',{},'样本 / 成分数量'),h('span',{},m.sample_count??m.observations??m.sample_size??m.n??m.constituent_count??'未提供')),h('div',{class:'kv'},h('span',{},'覆盖率'),h('span',{},coverageText)),h('div',{class:'kv'},h('span',{},'数据时点'),h('span',{},m.as_of?dateText(m.as_of):entity.quant_metrics?.as_of?dateText(entity.quant_metrics.as_of):entity.as_of?dateText(entity.as_of):'待确认')),key==='sharpe'?h('div',{class:'kv'},h('span',{},'年无风险利率假设'),h('span',{},`${num(Number(rf)*100,2)}% · 可配置`)):null,h('div',{class:'status-box'},m.reason||m.note||m.status||'请结合覆盖率、时间口径与数据来源解读。'),h('p',{class:'metric-meaning'},info.caution),!m.vendor&&entity.quant_metrics?.help?.[key]?.caveats?h('p',{class:'inline-note'},entity.quant_metrics.help[key].caveats):null,m.methodology?h('p',{class:'inline-note'},typeof m.methodology==='string'?m.methodology:JSON.stringify(m.methodology)):null);dialog.addEventListener('close',()=>dialog.remove());dialog.addEventListener('click',e=>{if(e.target===dialog)dialog.close();});document.body.append(dialog);dialog.showModal();}
function metricHelp(entity,key){const m=quantItem(entity,key);return h('button',{type:'button',class:'metric-help','aria-label':`查看${entity.name||'标的'}${m.label||metricInfo[key].label}公式与说明`,onClick:()=>openMetricHelp(entity,key)},'?');}
function quantCell(entity,key){const m=quantItem(entity,key);return h('td',{class:'numeric'},h('span',{class:'quant-inline'},num(m.value),metricHelp(entity,key)),m.vendor?h('div',{class:'panel-subtitle'},'供应商 · ',m.as_of||'日期未提供'):m.status==='partial'?h('div',{class:'panel-subtitle'},'样本组合'):null);}
function quantPanel(entity,proxy=false){return h('section',{class:'quant-panel'},h('div',{class:'theme-heading'},proxy?'参考ETF量化指标 · 不等同板块':'估值与风险指标 · 详见覆盖口径'),h('div',{class:'quant-grid'},Object.keys(metricInfo).map(key=>{const m=quantItem(entity,key);return h('div',{class:'quant-card'},h('div',{},h('span',{},m.vendor?(key==='pb'?'供应商 PB':'供应商 PE'):metricInfo[key].label),metricHelp(entity,key)),h('strong',{},num(m.value)),m.label?h('p',{class:'quant-label'},m.label):null,m.as_of?h('p',{class:'quant-date'},`指标日期 ${dateText(m.as_of,false)}`):null,h('p',{},m.reason||(!finite(m.value)?'数据不足，点击 ? 查看口径':key==='sharpe'?'历史价格收益指标':'整体法口径')));})) );}

function points(history,metric='index'){return(history||[]).map(x=>({date:metric==='intraday'?x.timestamp:x.date,value:metric==='intraday'?x.price:metric==='cap'?x.value:x.close??x.value,provider:x.provider,timestamp_quality:x.timestamp_quality,minute:x.minute,seconds:x.seconds})).filter(x=>finite(x.value));}
function intradaySegments(values){
  const groups=[];
  for(const value of values){
   const last=groups.at(-1);
   if(!last||value.minute-last.at(-1).minute>1)groups.push([value]);
   else last.push(value);
  }
  return groups;
}
function shortIntradayBridge(a,b){
  return a.minute<=120&&b.minute<=120||a.minute>=210&&b.minute>=210
    ?b.minute-a.minute<=5
    :false;
}
function chart(history,{height=205,compact=false,color='#4c85d5',metric='index',range=state.range,unit='点',today=false}={}) {
  let values=points(history,metric);if(range)values=values.slice(-range);
  if(today?!values.length:values.length<2||(metric==='intraday'&&new Set(values.map(v=>v.date)).size<2))return compact?null:h('div',{class:'empty-chart'},h('span',{},today?'今日暂无分时数据':metric==='intraday'?'分时有效报价时点不足两个 · 等待下一次采样':metric==='cap'?'历史市值样本不足 · 持续记录中':'等待真实行情历史'));
  const w=620, hh=height, left=compact?0:8,right=compact?0:56,top=compact?3:14,bottom=compact?3:30;
  let lo=Math.min(...values.map(v=>Number(v.value))),hi=Math.max(...values.map(v=>Number(v.value)));let pad=(hi-lo)*.18||Math.max(Math.abs(hi)*.005,1);lo-=pad;hi+=pad;
  const firstTime=new Date(values[0].date).getTime(),lastTime=new Date(values.at(-1).date).getTime();
  const sessionX=minute=>left+minute/330*(w-left-right);
  const sampleX=v=>sessionX(Math.min(v.minute<=120?120:330,v.seconds/60));
  const X=i=>today?sampleX(values[i]):left+(metric==='intraday'&&lastTime>firstTime?(new Date(values[i].date).getTime()-firstTime)/(lastTime-firstTime):i/(values.length-1))*(w-left-right);
  const Y=v=>top+(hi-Number(v))/(hi-lo)*(hh-top-bottom);
  const el=s('svg',{viewBox:`0 0 ${w} ${hh}`,class:compact?'spark':today?'chart session-chart':'chart',role:'img','aria-label':`${metric==='intraday'?'分时':metric==='cap'?'市值':'指数'}趋势，${values[0].date}至${values.at(-1).date}`});
  if(today)el.append(s('rect',{class:'lunch-break',x:sessionX(120),y:top,width:sessionX(210)-sessionX(120),height:hh-top-bottom,fill:'#f6f8fb'}));
  if(!compact){for(let i=0;i<5;i++){let y=top+i*(hh-top-bottom)/4;el.append(s('line',{x1:left,y1:y,x2:w-right,y2:y,stroke:'#e9eef4','stroke-dasharray':'3 5'}));const text=s('text',{x:w-right+10,y:y+3});text.textContent=metric==='cap'?amount(hi-(hi-lo)*i/4):num(hi-(hi-lo)*i/4,0);el.append(text);}}
  if(today){
   for(const [minute,label] of [[0,'09:30'],[60,'10:30'],[120,'11:30'],[210,'13:00'],[270,'14:00'],[330,'15:00']]){
    const t=s('text',{x:sessionX(minute),y:hh-7,'text-anchor':minute===0?'start':'middle'});
    t.textContent=label;el.append(t);
   }
   const segments=intradaySegments(values);
   for(const [i,group] of segments.entries()){
    const prior=segments[i-1]?.at(-1),first=group[0];
    if(prior&&shortIntradayBridge(prior,first)){
     el.append(s('path',{class:'session-interpolation',d:`M${sampleX(prior).toFixed(2)} ${Y(prior.value).toFixed(2)} L${sampleX(first).toFixed(2)} ${Y(first.value).toFixed(2)}`,stroke:color,'stroke-width':1.45,'stroke-dasharray':'3 3',opacity:.62,fill:'none'}));
    }
    if(group.length===1){
     const [v]=group;el.append(s('circle',{class:'isolated-sample',cx:sampleX(v),cy:Y(v.value),r:2,fill:color}));
    }else{
     const path=group.map((v,i)=>`${i?'L':'M'}${sampleX(v).toFixed(2)} ${Y(v.value).toFixed(2)}`).join(' ');
     el.append(s('path',{class:'session-line',d:path,stroke:color,'stroke-width':1.8,fill:'none','stroke-linecap':'round','stroke-linejoin':'round'}));
    }
   }
  }else if(!compact){for(let i=0;i<4;i++){let j=Math.round(i*(values.length-1)/3);const t=s('text',{x:X(j),y:hh-7,'text-anchor':i===0?'start':i===3?'end':'middle'});t.textContent=metric==='intraday'?dateText(values[j].date,true):String(values[j].date).slice(5);el.append(t);}}
  const line=today?'':values.map((v,i)=>`${i?'L':'M'}${X(i).toFixed(2)} ${Y(v.value).toFixed(2)}`).join(' ');
  if(!compact&&metric!=='intraday')el.append(s('path',{d:`${line} L${X(values.length-1)} ${hh-bottom} L${left} ${hh-bottom} Z`,fill:color,opacity:.065}));
  const providerColor=p=>p==='sina'?'#b68d4f':p==='tencent'?'#4c85d5':color;
  if(!today&&metric==='intraday'){for(const provider of new Set(values.map(v=>v.provider))){const grouped=values.map((v,i)=>({v,i})).filter(x=>x.v.provider===provider);const path=grouped.map((x,j)=>`${j?'L':'M'}${X(x.i).toFixed(2)} ${Y(x.v.value).toFixed(2)}`).join(' ');el.append(s('path',{d:path,stroke:providerColor(provider),'stroke-width':1.7,fill:'none','stroke-dasharray':'3 3'}));}}else if(!today)el.append(s('path',{d:line,stroke:color,'stroke-width':compact?3:2,fill:'none','stroke-linecap':'round','stroke-linejoin':'round'}));
  if(!today&&metric==='intraday'){values.forEach((v,i)=>{const dot=s('circle',{cx:X(i),cy:Y(v.value),r:4,fill:providerColor(v.provider),'fill-opacity':.65,tabindex:0});const label=s('title');label.textContent=`${v.timestamp_quality==='retrieved_only'?'采集时间（未确认源时点）':'来源时间'} ${dateText(v.date,false)} · ${num(v.value,3)} · ${v.provider||'来源未提供'} · ${v.timestamp_quality==='retrieved_only'?'仅采集时点':'来源报价时点'}`;dot.append(label);el.append(dot);});}if(!today&&!compact){let last=values.at(-1);el.append(s('circle',{cx:X(values.length-1),cy:Y(last.value),r:3.5,fill:color,stroke:'#fff','stroke-width':2}));const title=s('title');title.textContent=`${last.date}: ${num(last.value)}`;el.append(title);}if(!compact){
   el.setAttribute('tabindex','0');el.setAttribute('role','group');
   const overlay=s('g',{class:'chart-crosshair',visibility:'hidden','pointer-events':'none'});
   const vertical=s('line',{y1:top,y2:hh-bottom,stroke:'#334155','stroke-width':1,'stroke-dasharray':'4 3'});
   const intersection=s('circle',{r:5,fill:color,stroke:'#fff','stroke-width':2});
   const box=s('rect',{width:208,height:58,rx:7,fill:'#182b43',opacity:.97});
   const stamp=s('text',{fill:'#fff',class:'crosshair-label'}),value=s('text',{fill:'#fff',class:'crosshair-value'});
   overlay.append(vertical,intersection,box,stamp,value);el.append(overlay);let active=values.length-1;
   function show(i){active=Math.max(0,Math.min(values.length-1,i));const v=values[active],x=X(active),y=Y(v.value),bx=Math.max(left,Math.min(w-right-208,x+12)),by=top;
    overlay.setAttribute('visibility','visible');vertical.setAttribute('x1',x);vertical.setAttribute('x2',x);intersection.setAttribute('cx',x);intersection.setAttribute('cy',y);
    box.setAttribute('x',bx);box.setAttribute('y',by);stamp.setAttribute('x',bx+10);stamp.setAttribute('y',by+20);value.setAttribute('x',bx+10);value.setAttribute('y',by+43);
    stamp.textContent=(today?chinaClock.format(quoteTime(v.date)):metric==='intraday'?dateText(v.date,false):v.date)+(v.timestamp_quality==='page_observed'?' · 网页观测':v.timestamp_quality==='minute_backfill'?' · 补采分钟收盘价':'');
    value.textContent=(metric==='cap'?amount(v.value):num(v.value,metric==='intraday'?3:2)+' '+unit)+(v.provider?' · '+providerName(v.provider):'');
    el.setAttribute('aria-label',`${stamp.textContent} ${value.textContent}；左右方向键查看相邻样本`);
   }
   el.addEventListener('pointermove',e=>{const point=el.createSVGPoint();point.x=e.clientX;point.y=e.clientY;const ctm=el.getScreenCTM();if(!ctm)return;const x=point.matrixTransform(ctm.inverse()).x;let i=0;for(let j=1;j<values.length;j++)if(Math.abs(X(j)-x)<Math.abs(X(i)-x))i=j;if(today&&(x>sessionX(120)&&x<sessionX(210)||Math.abs(X(i)-x)>sessionX(1)-left)){overlay.setAttribute('visibility','hidden');return;}show(i);});
   el.addEventListener('pointerleave',()=>overlay.setAttribute('visibility','hidden'));
   el.addEventListener('focus',()=>show(active));el.addEventListener('blur',()=>overlay.setAttribute('visibility','hidden'));
   el.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();show(e.key==='Home'?0:e.key==='End'?values.length-1:active+(e.key==='ArrowRight'?1:-1));}});
  }
  return el;
}
function heading(title,description,label='RESEARCH WORKSPACE'){return h('div',{class:'page-heading'},h('div',{},h('div',{class:'eyebrow'},label),h('h2',{},title),h('p',{},description)),h('div',{class:'market-state'},h('i',{class:`status-dot ${['ok','ready'].includes(data().status)?'live':''}`}),data().market_session?.label||'等待数据'));}
let newsScrollObserver=null;
function observeNewsTail(){
 newsScrollObserver?.disconnect();
 const tail=document.getElementById('news-load-more');
 if(!tail||!('IntersectionObserver' in window))return;
 newsScrollObserver=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)&&tail.isConnected)tail.click();},{rootMargin:'150px 0px'});
 newsScrollObserver.observe(tail);
}
function buildPage(view){
 const d=data();
 return {d,cov:sourceCoverage(d),conflict:(d.data_checks||[]).some(row=>row.status==='conflict'),body:view(d)};
}
function pageWithAvailableData(view){
 try{return buildPage(view);}catch(error){
  if(state.demo)throw error;
 }
 // A nested field may only be consumed after navigation. Rebuild this page with
 // each published feed independently and discard just the one it cannot render.
 const specs=[dashboardFeed,...(relayOnly?feeds:[pageQuoteFeed])];
 const inputs=specs.filter(spec=>state[spec.key]).map(spec=>[spec,state[spec.key]]);
 for(const [spec] of inputs)state[spec.key]=null;
 let page=buildPage(view);
 for(const [spec,value] of inputs){
  state[spec.key]=value;
  try{page=buildPage(view);continue;}catch{}
  const task=publications.get(spec.key);
  state[spec.key]=null;
  if(task?.previous){
   state[spec.key]=task.previous;
   try{page=buildPage(view);}catch{state[spec.key]=null;}
   task.previous=null;
  }
  if(task){task.failures=Math.max(1,task.failures);task.retryAt=Math.max(task.retryAt,Date.now()+15000);}
 }
 return page;
}
function render(){
  const focus=document.activeElement;
  const quoteFocus=focus?.matches?.('.index-select, .heat-tile, .overview-leader')?focus.id:null;
  const view={overview,sectors,news:newsPage,forecast,review,settings,analytics}[state.page]||overview;
  const {d,cov,body,conflict}=pageWithAvailableData(view),current=pages.find(x=>x[0]===state.page)||pages[0];
  const sidebar=h('aside',{class:'sidebar'},h('div',{class:'brand'},h('div',{class:'brand-symbol'},icon('logo',23)),h('div',{class:'brand-text'},h('h1',{},'观澜'),h('small',{},'MARKET OBSERVATORY'))),h('div',{class:'nav-title'},'研究工作台'),h('nav',{class:'nav','aria-label':'主导航'},pages.map(([id,label,ico])=>h('button',{type:'button',class:state.page===id?'active':'',title:label,'aria-current':state.page===id?'page':null,onClick:()=>go(id)},icon(ico,18),h('span',{},label)))),h('div',{class:'side-bottom'},h('div',{class:'small-divider'}),h('div',{class:'side-label'},'数据状态'),h('div',{class:'side-status'},h('i',{class:`status-dot ${cov.healthy?'live':''}`}),`${cov.healthy} / ${d.sources.length} 数据源可用`),h('div',{class:'side-caption'},relayOnly?'SXData 主源 · 指数分批更新':'Tushare 主源 · 五分钟行情采样'),relayOnly?h('a',{href:'./training-progress/',class:'training-progress-link'},'180 天新闻采集进度'):null));
  const topbar=h('header',{class:'topbar'},h('div',{class:'breadcrumb'},'工作台',h('span',{},'/'),h('strong',{},current[1])),h('div',{class:'top-actions'},h('span',{class:'clock'},`${dateText(d.generated_at)} · 北京时间`),button(state.demo?'演示模式 · ON':'查看演示',changeDemo,`outline-button demo-button ${state.demo?'on':''}`),button('刷新',()=>refresh(),'outline-button','refresh'),h('div',{class:'avatar'},'研')));
  const main=h('main',{id:'main',class:'content',tabindex:-1},
    state.demo?h('div',{class:'banner demo'},icon('info',15),h('span',{},'演示模式：以下行情、新闻和预测均为合成示例，仅用于体验界面。'),h('span',{class:'spacer'}),button('返回真实数据',changeDemo,'')):null,
    !state.demo&&!d.generated_at&&!publications.get('data')?.failures?h('div',{class:'banner'},icon('info',15),h('span',{},state.loading?'正在加载研究数据…':'等待首次数据采集。行情与预测将于数据管道成功运行后显示。'),h('span',{class:'spacer'}),button('查看数据状态',()=>go('settings'),'')):null,
    !state.demo?publicationNotice():null,
    conflict?h('div',{class:'banner conflict'},icon('info',15),'多源行情存在冲突：相关标的暂停预测，请在数据设置中核对来源、交易日期与价格口径。'):null,
    body,h('footer',{class:'footnote'},h('span',{},`观澜 · A股研究工作台　|　${state.demo?'合成演示数据':`行业报价 · 今日 ${cov.today} / ${cov.total} · 有参考价 ${cov.quoted} / ${cov.total}`}`),h('span',{},'预测是待验证的研究结果，不代表确定收益。')));
  const shell=h('div',{class:'shell'},sidebar,h('div',{class:'main-wrap'},topbar,main));$('#app').replaceChildren(shell);observeNewsTail();if(state.toast)$('#app').append(h('div',{class:'toast',role:'status'},state.toast));
  if(quoteFocus)document.getElementById(quoteFocus)?.focus({preventScroll:true});
}
function statCard(item,active){
 const q=displayQuote(item);
 return h('button',{class:`stat-card index-select ${sign(q.change_pct)} ${active?'selected-index':''}`,
  id:`market-select-${item.id}`,type:'button',title:`查看${item.name}走势`,'aria-pressed':String(active),'aria-controls':'overview-trend',
  onClick:()=>{state.marketSelected=item.id;state.metric='intraday';render();}},
  h('span',{class:'stat-top'},item.name),
  h('span',{class:'stat-quote'},h('strong',{class:'stat-value'},num(q.price)),h('span',{class:`change ${sign(q.change_pct)}`},pct(q.change_pct))),
  h('span',{class:'stat-source'},indexUpdate(q)));
}
function quoteActivity(item){
 const q=item.live_quote||item;
 if(!q.quote_timestamp||!finite(q.amount)&&!finite(q.volume))return null;
 return h('div',{class:'quote-activity'},finite(q.amount)?h('span',{},'累计成交额 ',amount(q.amount)):null,
  finite(q.volume)?h('span',{},'累计成交量 ',num(q.volume,0),q.volume_unit==='share'?' 股':'（源单位待核实）'):null);
}
function stockCapital(st){
 const quote=st.live_quote,base=st.market_cap;
 const basisMatched=quote&&st.market_cap_date===st.quote_date&&finite(st.price)&&Number(st.price)>0&&
  finite(quote.previous_close)&&Math.abs(Number(st.price)/Number(quote.previous_close)-1)<.002;
 return basisMatched&&finite(base)&&quote.quote_date>st.market_cap_date
  ?{value:Number(base)*Number(quote.price)/Number(st.price),label:'估算'}
  :{value:base,label:st.market_cap_date||''};
}
function stockFlow(stock){
 const flow=stock.moneyflow;
 if(!finite(flow?.net_mf_amount))return null;
 return h('div',{class:'panel-subtitle'},`${flow.date} 盘后净流入 ${num(flow.net_mf_amount)} 万元`);
}
function exchangeSectorPanel(sec,d){
 const weights=sec.stocks||[];
 return h('div',{},trendPanel(d,sec),panel('主要权重股',sec.reference?.date?`指数权重日期 ${sec.reference.date}`:'等待中转返回成分权重',weights.length?
  h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},['股票','权重','涨跌幅','总市值'].map(label=>h('th',{},label)))),
   h('tbody',{},weights.map(st=>{
    const quote=displayQuote(st),cap=stockCapital(st);
    return h('tr',{},h('td',{},st.name,h('div',{class:'panel-subtitle'},st.ts_code)),h('td',{},pct(st.weight_pct).replace(/^\+/,'')),
     h('td',{class:sign(quote.change_pct)},pct(quote.change_pct)),h('td',{},amount(cap.value),cap.label?h('div',{class:'panel-subtitle'},cap.label):null,stockFlow(st)));
   })))):h('p',{class:'inline-note'},'当前指数没有已核验的权重清单。')));
}
function sectorComparison(sectors){
 const rows=sectors.map(sector=>({sector,q:sectorQuote(sector)}));
 rows.sort((a,b)=>String(b.q.day||'').localeCompare(String(a.q.day||''))||
  Number(finite(b.q.value))-Number(finite(a.q.value))||Number(b.q.value||0)-Number(a.q.value||0));
 const day=rows.find(({q})=>q.day&&finite(q.price))?.q.day;
 const ranked=rows.filter(({q})=>day&&q.day===day&&finite(q.price)&&finite(q.value));
 return {day,rows,ranked,label:day?`${day} 涨跌幅覆盖 ${ranked.length} / ${rows.length}`:'等待带日期的行业行情'};
}
function chooseOverviewIndustry(id){
 state.selected=id;
 if(state.page!=='overview'){go('sectors');return;}
 state.overviewSelected=id;
 render();
}
function heatmap(d,limit=12,comparison=sectorComparison(d.sectors),selectedId=state.overviewSelected){
 const today=chinaDay(new Date());
 return h('div',{class:'heatmap'},comparison.rows.slice(0,limit).map(({sector:sec,q})=>{
  const comparable=Boolean(comparison.day&&q.day===comparison.day&&finite(q.price)&&finite(q.value));
  const value=comparable?q.value:null;
  const stamp=q.estimate?sec.intraday_estimate?.quote_timestamp:q.quote.as_of;
  const time=quoteTime(stamp,q.quote.daily&&!q.estimate);
  const when=!q.day?'待更新':q.day!==today?`${q.day} 参考`:
   time===null?'时点待确认':chinaClock.format(time);
  const basis=q.estimate?'权重股估算':q.proxy?'ETF代理':'';
  const selected=selectedId===sec.id;
  return h('button',{class:`heat-tile ${finite(value)?value>0?'positive':value<0?'negative':'':''} ${Math.abs(value)>1.8?'strong':''} ${selected?'selected-sector':''}`,
   id:`overview-industry-${sec.id}`,type:'button','aria-pressed':String(selected),'aria-controls':'overview-sector-trend',
   onClick:()=>chooseOverviewIndustry(sec.id),title:[sec.name,num(q.price),pct(value),basis,when].filter(Boolean).join(' · ')},
   h('span',{class:'name'},sec.name),h('strong',{class:'change'},pct(value)),
   h('span',{class:'industry-quote'},finite(q.price)?`${num(q.price)} 点`:'点位待更新'),
   h('span',{class:'industry-stamp'},[basis,when].filter(Boolean).join(' · ')));
 }));
}
function trendPanel(d,item=selectedMarket(d),view=state){
 const mainQuote=displayQuote(item),today=relayOnly&&!state.demo;
 const estimate=item.intraday_estimate;
 const day=chinaDay(new Date()),ticks=today?todayRelayTicks(item,day):intradayFor(d,item);
 const requested=view.metric==='intraday'||item.taxonomy==='SSE',fallback=requested&&!ticks.length&&!today;
 const metric=requested&&!fallback?'intraday':fallback?'index':view.metric;
 const intraday=metric==='intraday',hist=intraday?ticks:metric==='cap'?item.market_cap_history:item.history;
 return panel(`${item.name||'市场'} · 走势`,intraday?(today?`今日分时 · ${day}${estimate?' · 权重股估算':''}`:'今日走势 · 实际采样点'):metric==='cap'?`总市值 · ${item.market_cap_date||'待更新'}`:`日线截至 ${item.history?.at(-1)?.date||'待更新'}`,
  h('div',{},fallback?h('p',{class:'chart-data-note'},isCanonicalSector(item)?minuteFallbackNote(d):'暂无分时采样，先显示已有日线。'):null,
   h('div',{class:'chart-body'},h('div',{class:'chart-meta'},h('strong',{},metric==='cap'?amount(item.market_cap):intraday?num(ticks.at(-1)?.price):num(mainQuote.price)),h('span',{},today&&intraday&&ticks.length?chinaClock.format(ticks.at(-1).time):item.name||'上证指数')),chart(hist,{height:today?170:205,metric,range:intraday?null:view.range,today:today&&intraday})),
   today&&intraday?quoteActivity(item):null,
   today&&intraday&&estimate?h('p',{class:'chart-data-note'},`采用 ${estimate.basis_date} 市值加权 · 覆盖 ${estimate.sample_count}/${estimate.sample_size} 只样本股（样本权重 ${accuracy(estimate.weight_coverage)}）`):null,
   metric==='cap'&&today&&!hist?.length?h('p',{class:'chart-data-note'},'当前指数缺少可核验的总市值历史。申万行业与有市值基准的个股可查看各自口径。'):null,
   !today&&!intraday&&metric!=='cap'?quoteCaption(mainQuote):null,intraday&&!today?tickNote(ticks):null,
   !today||!intraday||ticks.length?h('div',{class:'chart-footer'},h('span',{class:'legend'},h('i',{}),item.name||'上证指数'),h('span',{},today&&intraday?`虚线为短缺口插值 · 悬停查看${estimate?'估算点位':'实际报价'}`:intraday?'悬停查看时间与点位':'悬停查看日期与点位')):null),
  item.taxonomy==='SSE'?null:h('div',{class:'panel-tools'},segmented([['intraday','今日走势'],['index','日线'],['cap','市值']],view.metric,v=>{view.metric=v;render();}),!intraday?segmented([[21,'1M'],[63,'3M'],[252,'1Y']],view.range,v=>{view.range=v;render();}):null),today?'chart-panel current-trend':'chart-panel');
}
function marketBreadth(d,comparison=sectorComparison(d.sectors)){
 const {ranked,label}=comparison,up=ranked.filter(({q})=>q.value>0).length,down=ranked.filter(({q})=>q.value<0).length;
 if(!ranked.length)return h('div',{class:'breadth'},h('span',{},taxonomyLabel(d),' · ',label),h('span',{},'暂无可统计涨跌幅'));
 return h('div',{class:'breadth'},h('span',{},taxonomyLabel(d),' · ',label),h('strong',{class:'up'},`${up} 涨`),
  h('strong',{class:'down'},`${down} 跌`),h('span',{},`${ranked.length-up-down} 平`));
}
function leadersPanel(d,comparison=sectorComparison(d.sectors),selectedId=state.overviewSelected){
 const row=({sector:x,q},i)=>h('button',{class:'leader-row overview-leader',type:'button',
  id:`overview-leader-${x.id}`,'aria-controls':'overview-sector-trend','aria-pressed':String(x.id===selectedId),
  onClick:()=>chooseOverviewIndustry(x.id)},h('span',{class:'leader-rank'},i+1),h('strong',{},x.name),
  h('span',{class:`change ${sign(q.value)}`},pct(q.value)));
 const rising=comparison.ranked.filter(({q})=>q.value>0).slice(0,3);
 const falling=comparison.ranked.filter(({q})=>q.value<0).reverse().slice(0,3);
 return panel('行业焦点',comparison.label,h('div',{class:'leaders-grid'},
  h('div',{},h('h4',{class:'up'},'涨幅居前'),rising.length?rising.map(row):comparison.ranked.length?'已覆盖行业中暂无上涨':'等待同日涨跌幅'),
  h('div',{},h('h4',{class:'down'},'跌幅居前'),falling.length?falling.map(row):comparison.ranked.length?'已覆盖行业中暂无下跌':'等待同日涨跌幅')),
  button('查看全部行业 ↗',()=>go('sectors'),'text-button'));
}
function overviewIndustryBoard(d){
 const comparison=sectorComparison(d.sectors);
 const quoted=new Set(comparison.rows.filter(({q})=>q.day===comparison.day&&finite(q.price)).map(({sector})=>sector.id));
 const selected=d.sectors.find(item=>item.id===state.overviewSelected)
  ||d.sectors.find(item=>quoted.has(item.id))||d.sectors[0];
 const switchTaxonomy=value=>{
  if(value===state.taxonomy)return;
  state.taxonomy=value;
  state.overviewSelected=null;
  state.overviewIndustryView={metric:'intraday',range:21};
  render();
 };
 return h('section',{id:'overview-industries',class:'overview-section','aria-labelledby':'overview-industry-title'},
  h('header',{class:'overview-section-head'},h('h3',{id:'overview-industry-title'},'行业涨跌'),
   segmented(industryOptions(),state.taxonomy,switchTaxonomy)),
  marketBreadth(d,comparison),
  h('div',{class:'industry-board-grid'},
   h('div',{class:'panel industry-quotes'},d.sectors.length?heatmap(d,d.sectors.length,comparison,selected.id):
    empty('等待行业行情','取得报价后自动更新。')),
   h('div',{class:'industry-detail-stack'},h('div',{id:'overview-sector-trend'},selected?
    trendPanel(d,selected,state.overviewIndustryView):panel('行业走势',null,empty('等待行业分时','取得行业数据后显示走势。'))),
    leadersPanel(d,comparison,selected?.id))));
}
function overview(d){
 const market=selectedMarket(d);
 const validNews=d.news.filter(n=>n.credibility!=='rumor');
 return h('div',{class:'market-overview'},heading('市场总览','大盘指数与行业行情'),
  h('section',{id:'overview-indices',class:'overview-section','aria-labelledby':'overview-indices-title'},
   h('header',{class:'overview-section-head'},h('h3',{id:'overview-indices-title'},'大盘指数')),
   h('div',{class:'stat-grid compact-indices'},d.market.map(item=>statCard(item,item.id===market.id))),
   h('div',{id:'overview-trend'},d.market.length?trendPanel(d,market):panel('指数走势',null,
    empty('等待大盘行情','取得指数数据后自动更新。')))),
  overviewIndustryBoard(d),
  h('div',{class:'overview-research-grid'},forecastDigest(d),panel('关联资讯',null,
   validNews.length?h('div',{class:'news-list'},validNews.slice(0,3).map(n=>newsItem(n,d,true))):empty('暂无关联资讯','稍后更新'),
   button('全部资讯 ↗',()=>go('news'),'text-button'))));
}
function forecastDigest(d){const f=d.direction_probability||{},rows=[...(f.sectors||[]),...(f.market||[])].slice(0,3);return panel('收盘方向 · 模型概率',null,rows.length?h('div',{class:'forecast-digest'},rows.map(r=>h('div',{class:'leader-row'},h('strong',{},r.name),h('span',{},r.target_date),h('span',{class:'up'},`涨 ${accuracy(probabilityValue(f,r))}`),h('span',{class:'down'},`跌 ${accuracy(probabilityValue(f,r,'down_probability'))}`)))):h('p',{class:'inline-note'},'分类模型等待最新完整收盘数据。'),button('查看概率与测试 ↗',()=>go('forecast'),'text-button'));}
function targetLabel(p){return ['close','today_close'].includes(p.target_kind)?'今日收盘涨跌':'下次开盘点位';}
function newsTone(n){const raw=n.sentiment,st=raw&&typeof raw==='object'?raw:{label:raw},method=String(n.sentiment_method||st.method||'');const model=/model|minimax|finbert|transformer|neural/i.test(method),rules=/rule|lexicon|keyword/i.test(method),methodLabel=model&&rules?'模型与规则综合':model?'金融文本模型':rules?'规则分析':'分析方式待确认';return h('div',{class:'news-tone-row'},h('span',{class:'tone-prefix'},'文本语气'),pill(sentimentLabel({...st,article_count:1}),sentimentClass(st)),h('span',{},methodLabel));}
function minuteFallbackNote(d){const e=d.primary_minutes_state?.endpoints?.sw_mins||{},reason=e.provider_reason||e.error_code;const note=reason==='authorization_denied'?'当前服务的申万历史分钟请求认证失败':reason==='permission_denied'?'当前服务拒绝申万历史分钟权限':reason==='points_insufficient'?'当前服务返回申万历史分钟积分不足':reason==='unsupported_api'?'当前服务未支持申万历史分钟接口':reason==='invalid_parameters'?'申万历史分钟参数被当前服务拒绝':e.error_code?'申万历史分钟请求失败，原因待核验':'尚未取得申万历史分钟数据';return `${note}${Number.isInteger(e.provider_code)?`（业务码 ${e.provider_code}）`:''}，暂显示日线${relayOnly?'。':'，可切换东财盘中参考查看独立网页采样。'}`;}
function newsSummaryCard(n,compact=false){
 const a=n.llm_analysis;if(a?.status!=='ready'||!a.summary)return null;
 const label=x=>({positive:'利好',negative:'利空',mixed:'影响分化',neutral:'中性',unknown:'待判断'})[x]||'待判断';
 const impacts=Array.isArray(a.sector_impacts)?a.sector_impacts:[],priority=x=>x.direction==='positive'||x.direction==='negative'?0:1;
 const highlights=impacts.filter(x=>['positive','negative','mixed'].includes(x.direction)).sort((x,y)=>priority(x)-priority(y)||(Number(y.confidence)||0)-(Number(x.confidence)||0)).slice(0,compact?3:5);
 return h('div',{class:'news-ai-summary'},h('div',{class:'news-ai-label'},h('span',{class:'ai-dot'}),'全文提要',h('span',{},'MiniMax')),
  h('p',{class:'news-ai-lead'},a.summary),highlights.length?h('div',{class:'news-ai-impacts'},highlights.map(x=>pill(`${x.sector_name} · ${label(x.direction)}`,x.direction==='positive'?'up':x.direction==='negative'?'down':''))):null,
  !compact&&(impacts.length||a.uncertainties?.length)?h('details',{class:'news-impact-details','data-reader-key':'analysis-'+n.id},h('summary',{},impacts.length>highlights.length?`全部 ${impacts.length} 个板块与影响依据`:'影响依据'),impacts.map(x=>h('div',{class:'news-impact-row'},h('strong',{},`${x.sector_name} · ${label(x.direction)}`),h('p',{},x.reason),(x.evidence||[]).map(q=>h('blockquote',{},q)))),a.uncertainties?.length?h('p',{class:'inline-note'},a.uncertainties.join('；')):null):null);
}
const articleTextCache=new Map();
function newsTextCard(n){
 if(!n.article_text_url)return h('details',{class:'news-text-card','data-reader-key':'text-'+n.id},h('summary',{},'查看资讯文本',h('span',{},'来源摘要 · 全文待采集')),h('div',{class:'news-article-body'},n.summary||'尚未取得可展示的正文；可通过原文链接阅读。'),h('p',{class:'inline-note'},n.full_text_status==='unavailable'?'来源访问受限或正文提取失败，已暂缓重试。':'正在限速补充全文。'));
 const content=h('div',{class:'news-article-body'},articleTextCache.get(n.article_text_url)||'展开后加载完整正文。');
 let loading=false;
 const box=h('details',{class:'news-text-card','data-reader-key':'text-'+n.id,onToggle:async()=>{
  if(!box.open||loading)return;
  if(articleTextCache.has(n.article_text_url)){content.textContent=articleTextCache.get(n.article_text_url);return;}
  loading=true;content.textContent='正在加载正文…';
  try{
   if(!/^\.\/data\/(?:cloud-)?articles\/[a-f0-9]{64}\.json$/.test(n.article_text_url))throw Error('invalid');
   const response=await fetchPublished(n.article_text_url);if(!response.ok)throw responseError(response);
   const body=await response.json();if(body.status!=='complete'||body.id!==n.id||typeof body.full_text!=='string')throw Error('invalid');
   articleTextCache.set(n.article_text_url,body.full_text);content.textContent=body.full_text;
  }catch{content.textContent='正文暂时加载失败，可稍后重新展开，或查看原文链接。';}finally{loading=false;}
 }},h('summary',{},'查看完整正文',h('span',{},`${n.article_text_characters||0} 字符`)),content,h('p',{class:'inline-note'},n.full_text_method==='provider_news_text'?'接口提供的原始资讯文本 · ':'公开原文提取 · ',dateText(n.full_text_fetched_at),' · ',link('查看来源',n.url)));
 return box;
}
function newsDigestCard(d,previous=false){
 const a=d.news_digest||{},ready=Boolean(a.summary);
 const articles=new Map(d.news.map(article=>[article.id,article]));
 const section=(title,key,cls)=>h('details',{class:'digest-column','data-reader-key':'digest-'+a.date+'-'+key},h('summary',{class:cls},`${title} · ${(a[key]||[]).length} 项`),(a[key]||[]).length?(a[key]||[]).map(row=>{
  const evidence=(row.article_ids||[]).map(id=>articles.get(id)).filter(Boolean);
  const direction=key==='positives'?'positive':'negative';
  const inferred=evidence.flatMap(n=>(n.llm_analysis?.sector_impacts||[]).filter(x=>x.direction===direction).map(x=>x.sector_name));
  const sectors=[...new Set(row.sectors?.length?row.sectors:inferred)].filter(Boolean);
  return h('div',{class:'digest-point'},h('p',{},row.text),h('div',{class:'digest-sectors'},h('span',{},'影响板块'),sectors.length?sectors.map(name=>pill(name,cls)):h('span',{},'待核实')),
   h('details',{class:'digest-evidence','data-reader-key':'evidence-'+a.date+'-'+key+'-'+row.text},h('summary',{},`${evidence.length} 条资讯依据`),evidence.map(n=>h('p',{},link(n.title,n.url)))));
 }):h('p',{class:'inline-note'},'暂无充分依据'));
 return h('section',{class:'panel news-digest'},h('div',{class:'digest-heading'},h('div',{},h('span',{class:'news-ai-label'},'MiniMax · 综合解读'),h('h3',{},previous?`最近一次总览 · ${a.date}`:a.date&&a.date!==chinaDay(new Date())?`${a.date} · 资讯总览`:'今日资讯总览')),h('span',{class:'panel-subtitle'},a.generated_at?`更新时间 ${dateText(a.generated_at)}`:'等待生成')),
 ready?h('p',{class:'digest-lead'},a.summary):h('p',{class:'digest-lead'},'正在汇总当日收录资讯，生成主要事件、利好与风险解读。'),
 h('p',{class:'inline-note'},ready?`本次覆盖 ${a.article_count||0} 条收录资讯，其中 ${a.fulltext_analysis_count||0} 条使用单篇全文分析，${a.fulltext_source_count||0} 条使用来源完整文本；其余参考标题或摘要。`:`当日已收录 ${a.current_article_count||0} 条资讯。`,a.status==='updating'?' 新资讯正在补入，当前显示上一版解读。':a.status==='temporarily_unavailable'?(ready?' 模型暂时不可用，当前显示上一版解读。':' 模型暂时不可用，稍后自动重试。'):'',ready&&['updating','temporarily_unavailable'].includes(a.status)&&a.current_article_count!=null?` 当前已收录 ${a.current_article_count} 条。`:'',a.retry_after&&new Date(a.retry_after)>new Date()?` 可重试时间 ${dateText(a.retry_after)}。`:''),
 ready?h('div',{class:'digest-columns'},section('重点利好','positives','up'),section('主要风险','risks','down')):null,
 ready&&(a.watch||[]).length?h('details',{class:'digest-watch','data-reader-key':'watch-'+a.date},h('summary',{},'后续观察与不确定性'),(a.watch||[]).map(row=>h('p',{},row.text))):null);
}

function newsCollectionNote(){
 const feed=state.cloudNews,collector=feed?.collector;
 if(!relayOnly||state.demo||!feed)return null;
 const lines=[];
 if(feed.trade_date!==chinaDay(new Date()))lines.push(`当前资讯快照日期 ${feed.trade_date}`);
 if(collector){
  const label={ready:'已更新',cooldown:'采集暂停，等待重试',partial:'正在补采',pending:'等待采集'};
  if(label[collector.status])lines.push(label[collector.status]);
  if(collector.coverage_end)lines.push(`已查至 ${dateText(collector.coverage_end)}`);
  if(collector.last_success_at)lines.push(`最近采集成功 ${dateText(collector.last_success_at)}`);
  if(collector.retry_after&&new Date(collector.retry_after)>new Date())lines.push(`可重试时间 ${dateText(collector.retry_after)}`);
 }
 return lines.length?h('p',{class:'inline-note'},lines.join(' · ')):null;
}
function newsItem(n,d,compact=false){const credibility={official:['官方发布','official'],reliable:['可靠媒体','reliable'],trusted:['可靠媒体','reliable'],media:['媒体报道','reliable'],unverified:['待核实来源','rumor'],rumor:['传言 · 未核实','rumor']}[n.credibility]||['待核实','rumor'];const impact={positive:['潜在利好','up'],negative:['潜在利空','down'],neutral:['中性',''],uncertain:['影响待判断','']}[n.impact]||['影响待判断',''];const transmission=Array.isArray(n.transmission)?n.transmission:[];return h('article',{class:'news-item',id:'news-'+n.id},newsSummaryCard(n,compact),h('div',{class:'news-meta'},pill(...credibility),h('span',{},n.source||'未知来源'),h('span',{},'·'),h('time',{datetime:n.published_at||''},n.published_at?dateText(n.published_at):'发布时间未确认')),h('h4',{class:'news-title'},link(n.title||'无标题',n.url)),!compact&&n.summary&&n.llm_analysis?.status!=='ready'?h('p',{class:'news-summary'},n.summary):null,n.llm_analysis?.status==='ready'?null:newsTone(n),h('div',{class:'tags'},n.llm_analysis?.status==='ready'?null:pill(...impact),n.llm_analysis?.status==='ready'?null:(n.sector_ids||[]).filter(id=>d.sectors.some(x=>x.id===id)||(state.data?.sectors||[]).some(x=>x.id===id)).slice(0,3).map(id=>pill(sectorName(id,d))),n.category?pill(n.category):null),n.llm_analysis?.status==='ready'?null:transmission.length?h('div',{class:'chain'},transmission.map((x,i)=>[i?h('b',{},'→'):null,h('span',{},x)])):h('div',{class:'chain'},n.relevance_note||'A股关联线索待核实，尚未形成明确传导链。'),n.llm_analysis?.status!=='ready'&&n.relevance_note&&transmission.length?h('p',{class:'relevance-note'},n.relevance_note):null,!compact?newsTextCard(n):null,!compact?h('p',{class:'inline-note'},`首次采集：${dateText(n.first_seen_at||n.fetched_at)} · ${n.analysis_method==='minimax_full_text'?'MiniMax 全文分析':n.analysis_method==='keyword_rules'?'关键词线索 · 全文分析待完成':n.analysis_method||'待分析'}`):null);}
function industryRelations(sec){const hasLinks=[sec.upstream,sec.drivers,sec.downstream].some(rows=>Array.isArray(rows)&&rows.length);return panel('产业关联','仅展示已核实关系',hasLinks?h('div',{class:'mini-info'},h('div',{},'上游　',...(sec.upstream||[]).map(x=>pill(x))),h('div',{class:'divider'}),h('div',{},'核心驱动　',...(sec.drivers||[]).map(x=>pill(x))),h('div',{class:'divider'}),h('div',{},'下游　',...(sec.downstream||[]).map(x=>pill(x)))):h('div',{class:'mini-info'},'尚无已核实的产业链关系；可先查看上方关联消息与证据。'));}
function sectors(d){const subset=d.sectors.filter(s=>`${s.name} ${s.code||''} ${(s.keywords||[]).join(' ')}`.toLowerCase().includes(state.search.toLowerCase())&&(state.group!=='我的自选'||state.favorites.includes(s.id)));const comparison=sectorComparison(subset),matches=comparison.rows.map(row=>row.sector);
 if(state.sort==='name')matches.sort((a,b)=>a.name.localeCompare(b.name,'zh-CN'));
 if(state.sort==='cap')matches.sort((a,b)=>(Number(b.market_cap)||0)-(Number(a.market_cap)||0));
 const sec=matches.find(s=>s.id===state.selected)||matches[0];state.selected=sec?.id||null;return h('div',{},heading('行业雷达',taxonomyLabel(d)),h('div',{class:'toolbar'},segmented(industryOptions(),state.taxonomy,v=>{state.taxonomy=v;state.selected=null;state.group='全部';state.search='';state.metric=v==='sw'&&!relayOnly?'index':'intraday';render();}),h('label',{class:'search'},icon('search',14),h('input',{id:'sector-search',type:'search',value:state.search,placeholder:'搜索行业','aria-label':'搜索板块',onInput:e=>{state.search=e.target.value;renderKeepFocus('sector-search',e.target.selectionStart);}})),segmented([['全部','全部'],['我的自选','我的自选']],state.group,v=>{state.group=v;render();})),state.taxonomy==='eastmoney'?h('p',{class:'inline-note'},'独立行业分类，不与申万指数混用。每轮最多两页，显示已采集板块；各行保留实际采样时间。'):null,h('div',{class:'sector-layout'},panel('板块行情',`${comparison.label} · ${state.sort==='name'?'按名称排序':state.sort==='cap'?'按市值排序':'同日按涨跌幅排序，历史参考列后'}`,matches.length?h('div',{class:'table-wrap'},h('table',{class:'sector-quote-table'},h('thead',{},h('tr',{},['自选','行业','指数点位','涨跌幅','时间'].map(t=>h('th',{},t)))),h('tbody',{},matches.map(s=>{const sq=sectorQuote(s),q=sq.quote;return h('tr',{class:sec?.id===s.id?'selected':'',onClick:()=>{state.selected=s.id;render();}},h('td',{},button(state.favorites.includes(s.id)?'★':'☆',e=>{e.stopPropagation();state.favorites=state.favorites.includes(s.id)?state.favorites.filter(x=>x!==s.id):[...state.favorites,s.id];local.set('guanlan.favorites',state.favorites);render();},`star ${state.favorites.includes(s.id)?'saved':''}`)),h('td',{},h('button',{class:'sector-name',onClick:()=>{state.selected=s.id;render();}},s.name)),h('td',{class:'numeric'},num(sq.price)),h('td',{class:`numeric sector-change ${sign(sq.value)}`},pct(sq.value)),h('td',{class:'quote-time'},sq.estimate?`估算 ${dateText(s.intraday_estimate.quote_timestamp,true)}`:q.as_of?dateText(q.as_of,false):'待更新' ));})))):empty('尚无匹配行情',state.taxonomy==='eastmoney'?'网页采样尚未完成，或当前来源正在冷却。':'调整关键词后重试。')),h('div',{class:'stack'},sec?(sec.taxonomy==='SSE'?exchangeSectorPanel(sec,d):sectorDetail(sec,d)):null,sec&&state.taxonomy==='sw'?sectorProbabilityPanel(sec,d):null,sec&&state.taxonomy==='sw'?h('details',{class:'panel secondary-details'},h('summary',{},'成分研究、基金与关联资讯'),sectorEquityPanel(sec),fundsPanel(sec),sentimentPanel(sec,d),industryRelations(sec)):null)) );}
function renderKeepFocus(id,pos){render();const input=document.getElementById(id);if(input){input.focus();try{input.setSelectionRange(pos,pos);}catch{}}}
function stockRankLabel(sector){const basis=sector.rank_basis||sector.stocks?.[0]?.rank_basis;if(['market_cap','total_market_cap'].includes(basis))return'按已获取总市值排序';if(basis==='membership_order')return'成分示例 · 市值排序待数据';if(['turnover_amount','amount','turnover'].includes(basis))return'按已获取成交额排序';return'成分示例 · 排序依据待确认';}
function sectorDetail(sec,d){const stocks=sec.stocks||[],mainQuote=displayQuote(sec),shown=sectorQuote(sec),proxy=sec.proxy,proxyQuote=displayQuote(proxy||{}),hasProxy=!finite(mainQuote.price)&&finite(proxyQuote.price),useProxy=state.metric!=='cap'&&hasProxy;const quote=useProxy?proxy:sec;const today=relayOnly&&!state.demo,ticks=today?todayRelayTicks(quote,chinaDay(new Date())):intradayFor(d,quote,useProxy?sec:null),isIntraday=state.metric==='intraday';const hist=isIntraday?ticks:state.metric==='cap'?sec.market_cap_history:quote.history;return panel(`${sec.name}`,`${providerName(mainQuote.provider)} · ${sec.code||sec.id||'代码待确认'} · ${isCanonicalSector(sec)?'申万 2021 一级行业':sec.taxonomy==='EASTMONEY'?'东方财富行业':'行业口径待确认'}`,h('div',{},h('div',{class:'detail-top'},h('div',{},h('div',{class:'panel-subtitle'},shown.estimate?'板块点位 · 权重股估算':'板块指数点位'),h('strong',{class:'detail-price'},num(shown.estimate?shown.price:mainQuote.price))),h('span',{class:`change ${sign(shown.value)}`},pct(shown.value))),shown.estimate?h('p',{class:'chart-data-note'},`权重基准 ${sec.intraday_estimate.basis_date} · 样本股 ${sec.intraday_estimate.sample_count}/${sec.intraday_estimate.sample_size} · 报价时间 ${dateText(sec.intraday_estimate.quote_timestamp,false)}`):quoteCaption(mainQuote),validationStatus(sec)==='conflict'?validationDetail(sec):null,hasProxy?h('div',{class:'proxy-box'},h('div',{class:'proxy-title'},'参考ETF · ',proxy.name,` (${proxy.code})`),h('div',{class:'proxy-quote'},h('strong',{},num(proxyQuote.price,3)),h('span',{},`元 / ${proxyQuote.label}`),h('span',{class:`change ${sign(proxyQuote.change_pct)}`},pct(proxyQuote.change_pct))),h('p',{},`${providerName(proxyQuote.provider)} · ${proxyQuote.as_of?dateText(proxyQuote.as_of):'日期待确认'} · ETF仅为相关资产代理，不等同板块指数。`),validationDetail(proxy),liveQuoteBlock(proxy)):null,isCanonicalSector(sec)?h('details',{class:'secondary-details'},h('summary',{},'估值与风险指标'),quantPanel(sec),proxy?.quant_metrics?quantPanel(proxy,true):null):null,h('div',{class:'chart-body'},h('div',{class:'panel-subtitle'},isIntraday?(today?(sec.intraday_estimate?'今日分时 · 权重股估算':'今日分时'):useProxy?'参考ETF分时快照 · 单位：元':'板块分时快照 · 单位：点'):state.metric==='cap'?(sec.market_cap_date?`历史总市值 · 最新 ${sec.market_cap_date}`:'历史总市值'):useProxy?`参考ETF趋势 · ${proxy.name} (${proxy.code}) · 单位：元`:'板块指数趋势 · 单位：点'),chart(hist,{height:165,metric:state.metric,range:isIntraday?null:state.range,unit:useProxy?'元':'点',today:today&&isIntraday})),isIntraday&&!today?tickNote(ticks):null,h('div',{class:'chart-footer'},segmented([['intraday','分时'],['index',hasProxy?'ETF日线':'指数日线'],['cap','板块市值']],state.metric,v=>{state.metric=v;render();}),h('span',{},isIntraday?(today?'':'定时采样，非秒级实时'):state.metric==='cap'?'真实快照':useProxy?'ETF日收盘，非板块点位':'日频行情')),h('div',{class:'panel-head'},h('h3',{class:'panel-title'},'主要成分股'),h('span',{class:'panel-subtitle'},stockRankLabel(sec))),stocks.length?h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},(today?['股票','价格','涨跌幅','总市值']:['股票','价格','涨跌幅']).map(t=>h('th',{},t)))),h('tbody',{},stocks.slice(0,8).map(x=>{const q=displayQuote(x),cap=stockCapital(x);return h('tr',{},h('td',{},x.name,h('div',{class:'panel-subtitle'},x.code)),h('td',{class:'numeric'},num(q.price),h('div',{class:'panel-subtitle'},today?(q.daily?q.day:q.is_today&&q.time!==null?chinaClock.format(q.time):q.day||'待更新'):q.label)),h('td',{class:`numeric ${sign(q.change_pct)}`},pct(q.change_pct)),today?h('td',{class:'numeric'},amount(cap.value),cap.label?h('div',{class:'panel-subtitle'},cap.label):null,stockFlow(x)):null);})))):empty('成分股尚未接入','板块成分与ETF持仓不同，不以ETF持仓填充板块成分。')));}
function newsPage(d){const groups=['全部','官方信息','可靠媒体','未核实传言'];let items=d.news.filter(n=>{if(state.newsFilter==='官方信息'&&n.credibility!=='official')return false;if(state.newsFilter==='可靠媒体'&&!['reliable','trusted','media'].includes(n.credibility))return false;if(state.newsFilter==='未核实传言'&&n.credibility!=='rumor')return false;if(state.newsFilter==='全部'&&!state.preferences.showRumors&&n.credibility==='rumor')return false;return`${n.title} ${n.source} ${(n.sector_ids||[]).map(id=>sectorName(id,d)).join(' ')}`.toLowerCase().includes(state.newsSearch.toLowerCase());});const sources=d.sources.filter(s=>s.type!=='market');
 const timeline=h('div',{class:'news-list'},items.slice(0,state.newsLimit).map(n=>newsItem(n,d)));
 if(items.length>state.newsLimit){
  const more=button(`向下滚动加载更多 · 已显示 ${Math.min(state.newsLimit,items.length)} / ${items.length} 条`,()=>{
   const start=state.newsLimit;state.newsLimit=Math.min(items.length,start+12);
   items.slice(start,state.newsLimit).forEach(n=>timeline.insertBefore(newsItem(n,d),more));
   more.textContent=`向下滚动加载更多 · 已显示 ${state.newsLimit} / ${items.length} 条`;
   if(state.newsLimit>=items.length){more.remove();newsScrollObserver?.disconnect();timeline.append(h('p',{class:'inline-note news-list-end'},'已显示全部匹配资讯'));}
  },'button secondary news-load-more');more.id='news-load-more';timeline.append(more);
 }
 const progress=relayOnly&&!state.demo?null:d.news_backfill_summary;
 return h('div',{},heading('资讯与板块影响','先看全文提要，再查看受影响板块与原文依据。','A-SHARE INTELLIGENCE'),newsCollectionNote(),newsDigestCard(d),d.previous_news_digest?newsDigestCard({...d,news_digest:d.previous_news_digest},true):null,d.news_backfill_digest?.summary&&d.news_backfill_digest.date!==d.news_digest?.date&&d.news_backfill_digest.date!==d.previous_news_digest?.date?h('details',{class:'panel secondary-details'},h('summary',{},`${d.news_backfill_digest.date} · 补全文综合解读`),newsDigestCard({...d,news_digest:d.news_backfill_digest})):null,progress?h('p',{class:'inline-note news-backfill-progress'},`全文补全 ${progress.body_complete||0}/${progress.total||103} · 发布时间已核实 ${progress.publication_verified||0} · MiniMax 已分析 ${progress.model_complete||0} · 分批更新中`):null,h('div',{class:'toolbar'},h('div',{class:'filter-group'},groups.map(g=>button(g,()=>{state.newsFilter=g;state.newsLimit=12;render();},`filter ${state.newsFilter===g?'active':''}`))),h('label',{class:'search'},icon('search',14),h('input',{id:'news-search',type:'search',placeholder:'搜索事件、来源或板块',value:state.newsSearch,'aria-label':'搜索资讯',onInput:e=>{state.newsSearch=e.target.value;state.newsLimit=12;renderKeepFocus('news-search',e.target.selectionStart);}}))),state.newsFilter==='未核实传言'?h('div',{class:'banner demo'},icon('info',15),'传言缺少独立证实，默认不进入主预测模型。请先阅读原始来源与核实状态。'):null,h('div',{class:'news-grid'},panel('事件时间线',`${items.length} 条符合筛选的资讯 · 以采集范围为准`,items.length?timeline:empty('暂无匹配资讯','采集成功后会显示真实新闻；调整筛选可查看其他来源。','globe')),h('div',{class:'stack'},h('details',{class:'panel secondary-details'},h('summary',{},'查看信息源状态'),sourceList(sources)),panel('解读原则','把事实、推断与传言分开',h('div',{class:'mini-info'},h('p',{},pill('官方发布','official'),' 政策正文、监管披露、央行与政府公报。'),h('p',{},pill('媒体报道','reliable'),' 保留媒体名称与原文链接，仍需交叉核对。'),h('p',{},pill('传言','rumor'),' 单独展示，不把传播热度等同于事实。'),h('div',{class:'info-strip'},'全文提要与行业影响由 MiniMax 分别判断，并保留原文依据；经营影响不等于股价走势。'))))) );}
function sourcePermissions(d){const states=d.stock_api_state?.endpoints||d.sources.find(s=>s.id==='stock_api')?.endpoint_status||{};const names={trade_cal:'交易日历',index_classify:'申万行业分类',classify:'申万行业分类',index_member_all:'行业成分',index_member:'行业成分',sw_daily:'申万行业日线',index_daily:'市场指数日线',daily_basic:'个股估值',rt_idx_k:'市场指数快照',rt_sw_k:'行业指数快照',news:'财经资讯'};const entries=Object.entries(states).filter(([,status])=>record(status));return panel('Tushare 服务状态','日线与估值为主；快照和资讯按实际权限分别启用',entries.length?h('div',{class:'sources-list'},entries.map(([key,status])=>{const denied=status.error_code==='api_denied',available=['success','ok','available'].includes(status.status)&&!denied;const label=denied?'当前权限不可用':available?'已取得数据':status.status==='cooldown'?'稍后重试':status.status==='error'?'暂不可用':'待检查';return h('div',{class:'source-row'},h('div',{},h('strong',{},names[key]||'行情服务'),h('p',{},key),h('p',{},status.last_success?`最近成功 ${dateText(status.last_success,false)}`:status.cooldown_until?`下次检查 ${dateText(status.cooldown_until,false)}`:'尚无成功记录')),h('span',{class:`source-state ${available?'ok':''}`},label));})):empty('服务状态待更新','以各项服务的实际成功记录为准，不默认所有接口均已开通。'));}
function sourceList(sources){return sources.length?h('div',{class:'sources-list'},sources.map(src=>h('div',{class:'source-row'},h('div',{},h('strong',{},link(src.name,src.url)),h('p',{},src.last_success?dateText(src.last_success):src.message||'待采集')),h('span',{class:`source-state ${['ok','success','healthy'].includes(src.status)?'ok':''}`},({ok:'已连接',success:'已连接',healthy:'已连接',error:'采集失败',failed:'采集失败',pending:'待连接',disabled:'未启用',cooldown:'暂缓访问',blocked:'访问受限',robots_disallowed:'站点规则禁止',stale:'缓存过期',partial:'部分可用'})[src.status]||src.status||'待连接')))):empty('尚无来源状态','数据源状态将在首次执行后更新。');}
// The classifier's probabilities are learned directly from up/down labels.
function probabilityValue(f,r,key='up_probability'){const v=r?.[key];return ['ready','partial'].includes(f?.status)&&f.target==='next_close_direction'&&finite(v)&&Number(v)>=0&&Number(v)<=1?Number(v):null;}
function probabilityRow(d,id){const f=d.direction_probability||{};return [...(f.market||[]),...(f.sectors||[])].find(r=>r.id===id);}
function probabilityPair(f,r){const up=probabilityValue(f,r),down=probabilityValue(f,r,'down_probability');return h('div',{class:'probability-pair'},h('div',{class:'up'},h('span',{},'上涨概率'),h('strong',{},accuracy(up))),h('div',{class:'down'},h('span',{},'下跌概率'),h('strong',{},accuracy(down))));}
function probabilityEvaluation(f,group,label=null){const e=f.evaluation?.[group]||{},b=e.baseline||{},test=e.dates?`${e.first_date}—${e.last_date} · ${e.dates} 个交易日`:'测试待完成';return h('div',{class:'probability-evaluation'},h('h4',{},label||(group==='market'?'大盘指数 · 历史测试':'行业板块 · 历史测试')),h('p',{},test),h('div',{class:'probability-metrics'},h('span',{},`涨跌正确率 ${accuracy(e.direction_accuracy)}`),h('span',{},`频率基线 ${accuracy(b.direction_accuracy)}`),h('span',{},`Brier ${num(e.brier,4)} / 基线 ${num(b.brier,4)}`),h('span',{},`AUC ${num(e.auc,3)}`)),h('p',{class:'inline-note'},e.statistically_supported?'该历史窗口内概率误差低于频率基线，仍需前向检验。':'当前历史检验尚未证明概率优于频率基线。Brier越小越好；涨跌正确率与单次预测概率含义不同。'));}
function probabilityPanel(d){const f=d.direction_probability||{},ready=['ready','partial'].includes(f.status),rows=f.sectors||[],stale=(f.stale_entities||[]).filter(r=>d.sectors.some(e=>e.id===r.id)),arc=d.news_archive_summary||{};return panel('收盘涨跌概率 · 分类模型','相对参考交易日收盘 · 预测下一交易日方向',h('div',{class:'probability-body'},h('div',{class:'tags'},pill(f.model_id?'已完成真实分类训练':'等待分类模型',f.model_id?'official':''),f.model_id?pill('历史测试尚未证明优于频率基线','rumor'):null,h('span',{},f.model_id||'')),h('p',{class:'inline-note'},'概率来自量价分类器，不由涨跌幅正负转换。50%左右表示模型判断接近均衡。'),ready?h('div',{},h('div',{class:'probability-index-grid'},(f.market||[]).map(r=>h('article',{class:'probability-card'},h('div',{class:'stat-top'},h('strong',{},r.name),pill(r.target_date)),probabilityPair(f,r),h('p',{class:'inline-note'},r.model_lead_sessions===2?`滞后行情分类器 · 行情截至 ${r.input_price_date}`:`参考 ${r.reference_date} 收盘 ${num(r.reference_close)} 点`)))),h('div',{class:'table-wrap'},h('table',{class:'probability-table'},h('thead',{},h('tr',{},['行业','目标日期','上涨概率','下跌概率','消息情绪'].map(t=>h('th',{},t)))),h('tbody',{},rows.map(r=>{const entity=d.sectors.find(e=>e.id===r.id);return h('tr',{},h('td',{},button(r.name,()=>{state.taxonomy='sw';state.selected=r.id;go('sectors');},'sector-name'),h('div',{class:'panel-subtitle'},r.model_lead_sessions===2?`滞后行情模型 · ${r.input_price_date}`:'常规模型')),h('td',{},r.target_date),h('td',{class:'up numeric'},accuracy(probabilityValue(f,r))),h('td',{class:'down numeric'},accuracy(probabilityValue(f,r,'down_probability'))),h('td',{},sentimentLabel(entity?.sentiment)));}),stale.map(r=>h('tr',{},h('td',{},r.name),h('td',{colspan:4,class:'panel-subtitle'},`等待最新收盘日线 · 当前参考日期 ${r.reference_date}`))))))):empty('概率暂不可用',f.reason||'等待有效的完整收盘日线与分类模型。'),f.lagged_model?.used_entities?h('p',{class:'inline-note'},`${f.lagged_model.used_entities} 个标的使用单独训练的滞后行情分类器，预测目标日相对前一日涨跌；未补造缺失的今日收盘。`):null,h('div',{class:'probability-news-status'},h('strong',{},'当日资讯情绪'),h('p',{},f.sentiment_training?.enabled?'情绪特征已参与分类模型，预测随新数据更新。':`已接通情绪特征；成熟训练日期 ${f.sentiment_training?.mature_dates_with_news??0} / ${f.sentiment_training?.required_dates??30}。当前样本不足，情绪分值尚不调整上涨概率。`),h('p',{class:'inline-note'},`每天09:00—16:00每30分钟更新资讯与情绪 · 最近归档 ${dateText(arc.last_collection)} · ${arc.archived_versions??0} 个消息版本`)),h('details',{class:'secondary-details'},h('summary',{},'查看实际训练与概率测试结果'),probabilityEvaluation(f,'market'),probabilityEvaluation(f,'sector'),f.lagged_model?probabilityEvaluation({...f,evaluation:f.lagged_model.evaluation},'sector','滞后行情分类器 · 行业历史测试'):null,h('p',{class:'inline-note'},'测试按完整交易日划分；这些历史日期此前用于回归研究，不是全新的独立市场样本。模型依据开发段选择，测试段未用于选型。'),h('p',{class:'inline-note'},`模型 ${f.model_id||'待更新'} · 训练 ${dateText(f.trained_at)} · 已结算前向预测 ${f.forward_evaluation?.count??0} 条`))),null,'probability-panel');}
function sectorProbabilityPanel(sec,d){const f=d.direction_probability||{},r=probabilityRow(d,sec.id);return panel(`${sec.name} · 收盘方向概率`,r?`目标 ${r.target_date} · ${r.model_lead_sessions===2?'行情截至 '+r.input_price_date:'参考 '+r.reference_date+' 收盘'}`:'等待最新完整日线',h('div',{class:'probability-body'},probabilityPair(f,r),h('p',{class:'inline-note'},r?.sentiment_note||'缺少最新完整日线时保留空值，不把过去目标日期当作明日预测。'),sentimentSummary(sec)));}
// Daily index/sector forecasts have their own target, model and issued ledger.
function dailyForecastValue(f,row) {
  return f&&['ready','partial','issued','baseline'].includes(f.status)&&f.target==='next_close_return_pct'&&row&&finite(row.predicted_return_pct)
    ? Number(row.predicted_return_pct) : null;
}
function dailyForecastRow(d,id) {const f=d.daily_forecast||{};return (f.sectors||[]).find(row=>row.id===id)||(f.ledger||[]).find(row=>row.id===id&&!row.settled_at&&row.target_date===chinaDay(new Date()));}
function dailyMaeText(value) {return finite(value)?`${num(value,4)} 个百分点`:'尚无验证误差';}
function dailyGoal(e={},target=.1) {
  return finite(e.mae_pct)?Number(e.mae_pct)<=target?`历史 MAE 已达到 ${num(target,1)} 个百分点目标`:`尚未达到 ${num(target,1)} 个百分点目标`:'目标待验证';
}
function dailyEvaluation(f,group) {
  const e=f.evaluation?.[group]||{},target=finite(f.mae_target)?Number(f.mae_target):.1;
  return h('div',{class:'daily-evaluation'},h('strong',{},group==='market'?'整体指数':'行业板块'),
    h('span',{},`历史回溯 MAE ${dailyMaeText(e.mae_pct)}`),
    h('span',{},`基线 ${dailyMaeText(e.baseline_mae_pct)}`),
    pill(dailyGoal(e,target),finite(e.mae_pct)&&Number(e.mae_pct)<=target?'reliable':'rumor'));
}
function dailyRankingEvaluation(f) {
  const e=f.evaluation?.sector_ranking||{},ic=finite(e.daily_rank_ic)?Number(e.daily_rank_ic):null;
  const message=ic===null?'历史排序表现待验证':ic<=0?'历史回溯尚未验证排序有效':'历史回溯排序相关为正，仍需前向验证';
  return h('div',{class:'daily-ranking-evaluation'},
    h('div',{class:'tags'},pill(message,ic!==null&&ic<=0?'rumor':''),
      h('span',{},`历史日均 RankIC ${ic===null?'—':num(ic,4)}`),
      h('span',{},`有效排序覆盖 ${accuracy(e.ranking_coverage)}`),
      finite(e.ranking_valid_dates)&&finite(e.dates)?h('span',{},`${e.ranking_valid_dates} / ${e.dates} 个交易日`):null),
    h('p',{},'RankIC 衡量同日行业强弱排序的相关性；覆盖率表示可计算排序的日期占比，均不是上涨命中率。'));
}
function dailyIndexCard(f,row) {
  const value=dailyForecastValue(f,row),hasValue=value!==null,baseline=/zero|median|baseline/.test(row.model_family||'');
  return h('article',{class:'daily-index-card'},h('div',{class:'stat-top'},h('strong',{},row.name||row.id),pill(baseline?'基线预测':'研究预测')),
    h('div',{class:`daily-return ${sign(value)}`},hasValue?(baseline&&value===0?'暂无方向信号':pct(value)):'待生成'),
    h('div',{class:'panel-subtitle'},`预测收盘 ${hasValue&&!(baseline&&value===0)&&finite(row.predicted_close)?num(row.predicted_close)+' 点':'—'}`),
    h('p',{},`参考 ${row.reference_date||'待更新'} · ${num(row.reference_close)} 点`),
    h('p',{},`历史 MAE ${dailyMaeText(row.validation_mae_pct)}`),h('span',{class:'ticker'},row.model_family||'模型待就绪'));
}
function dailySectorTable(f,compact=false) {
  const rows=(f.sectors||[]).slice().sort((a,b)=>finite(a.rank)&&finite(b.rank)?Number(a.rank)-Number(b.rank):finite(b.ranking_score)&&finite(a.ranking_score)?Number(b.ranking_score)-Number(a.ranking_score):0);
  return rows.length?h('div',{class:'table-wrap'},h('table',{class:'daily-sector-table'},
    h('thead',{},h('tr',{},['强弱位次','行业','独立排序得分','次日收盘涨跌','相对沪深300','历史 MAE（百分点）'].map(x=>h('th',{},x)))),
    h('tbody',{},(compact?rows.slice(0,5):rows).map(row=>{const value=dailyForecastValue(f,row),relative=value!==null&&finite(row.relative_return_pct)?Number(row.relative_return_pct):null;
      return h('tr',{},h('td',{class:'numeric'},value!==null&&finite(row.rank)?String(Number(row.rank)):'—'),
        h('td',{},button(row.name||row.id,()=>{state.selected=row.id;state.search='';state.group='全部';go('sectors');},'sector-name')),
        h('td',{class:'numeric'},value!==null&&finite(row.ranking_score)?num(row.ranking_score,4):'—'),
        h('td',{class:`numeric ${sign(value)}`},value===null?'—':pct(value)),
        h('td',{class:`numeric ${sign(relative)}`},relative===null?'—':`${relative>0?'+':''}${num(relative,3)} 个百分点`),
        h('td',{class:'numeric'},finite(row.validation_mae_pct)?num(row.validation_mae_pct,4):'—'));
    })))):empty('行业收盘预测待生成',f.reason||'本机模型和有效日线就绪后，按目标交易日发布。');
}
function dailyForecastPanel(d,compact=false) {
  const f=d.daily_forecast||{},ready=['ready','partial','issued','baseline'].includes(f.status),target=f.target_date||'目标交易日待定',hasRanking=(f.sectors||[]).some(row=>finite(row.rank));
  return panel('下一交易日收盘 · 指数与板块',`${target} · 预测一个交易日 · 相对参考日收盘`,h('div',{class:'daily-forecast-body'},
    h('div',{class:'daily-forecast-meta'},pill(ready?'已发布研究预测':'等待日频模型',ready?'official':''),
      h('span',{},`本次推断 ${dateText(f.issued_at)}`),h('span',{},`训练 ${f.trained_at?dateText(f.trained_at):'待完成'}`)),
    ready?h('div',{},(f.market||[]).length&&(f.market||[]).every(row=>/zero|baseline/.test(row.model_family||''))?h('div',{class:'daily-baseline-note'},'指数当前采用零收益基线：尚未获得优于基线的指数预测信号；0% 不代表明天一定不涨跌。'):null,h('div',{class:'daily-index-grid'},(f.market||[]).map(row=>dailyIndexCard(f,row))),
      h('details',{class:'secondary-details'},h('summary',{},'查看历史误差与模型验证'),h('div',{class:'daily-evaluations'},dailyEvaluation(f,'market'),dailyEvaluation(f,'sector')),dailyRankingEvaluation(f)),
      h('div',{class:'daily-table-title'},h('strong',{},hasRanking?(compact?'行业强弱榜 · 前五':'行业强弱榜'):(compact?'行业预测 · 先列五项':'行业预测 · 暂无有效排序')),h('span',{},hasRanking?'按独立模型得分排序；相对收益 = 行业预测涨跌 − 沪深300预测涨跌':'排序得分相同或缺失时不排强弱位次')),
      dailySectorTable(f,compact)):empty('尚无下一交易日收盘预测',f.reason||'加载本机完成验证的指数与行业模型后生成；缺失结果保持为空。'),
    h('p',{class:'inline-note'},'排序得分仅用于比较强弱；涨跌幅与相对收益均不是上涨概率。',f.evaluation?.note||'历史回溯误差用于研究比较，未来实盘误差需等待冻结预测结算。'),
    h('div',{class:'daily-forward-result'},`前向留存：已结算 ${f.forward_evaluation?.settled_rows??0} 条 · MAE ${dailyMaeText(f.forward_evaluation?.mae_pct)}`),
    awaitingForecasts(d),h('div',{class:'daily-forecast-foot'},h('span',{},`模型版本 ${f.model_id||'待部署'}`),h('span',{},f.evaluation?.historical_research?'验证性质：历史研究':'验证状态待更新'))),
    compact?button('完整预测 ↗',()=>go('forecast'),'text-button'):null,'daily-forecast wide-panel');
}
function awaitingForecasts(d){const f=d.daily_forecast||{},ids=new Set([...(f.market||[]),...(f.sectors||[])].map(x=>x.id));const pending=(f.ledger||[]).filter(x=>!ids.has(x.id)&&!x.settled_at&&x.target_date===chinaDay(new Date()));if(!pending.length)return null;return h('div',{class:'awaiting-forecasts'},h('h4',{},'最近发布 · 等待今日收盘数据'),h('p',{class:'inline-note'},'以下为此前已发布的预测；目标日期单独标注，最新收盘数据到达后更新与结算。'),h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},['行业 / 指数','目标日期','此前预测涨跌','状态'].map(x=>h('th',{},x)))),h('tbody',{},pending.map(r=>h('tr',{},h('td',{},r.name||r.id),h('td',{},r.target_date),h('td',{class:`numeric ${sign(r.predicted_return_pct)}`},/zero|baseline/.test(r.model_family||'')?'暂无方向信号':pct(r.predicted_return_pct)),h('td',{},'待收盘数据')))))));}
function dailySectorPanel(sec,d) {
  const f=d.daily_forecast||{},row=dailyForecastRow(d,sec.id),value=dailyForecastValue(f,row);
  return panel(`${sec.name} · 次日收盘预测`,`${row?.target_date||f.target_date||'目标日期待定'} · 官方行业指数${row&&row.target_date!==f.target_date?' · 等待最新收盘数据':''}`,h('div',{class:'daily-sector-detail'},
    h('div',{class:`daily-return ${sign(value)}`},value===null?'待生成':pct(value)),
    h('div',{class:'kv'},h('span',{},'相对沪深300'),h('span',{},value!==null&&finite(row.relative_return_pct)?`${row.relative_return_pct>0?'+':''}${num(row.relative_return_pct,3)} 个百分点`:'—')),
    h('div',{class:'kv'},h('span',{},'预测收盘点位'),h('span',{},value!==null?num(row.predicted_close):'—')),
    h('div',{class:'kv'},h('span',{},'历史 MAE'),h('span',{},dailyMaeText(row?.validation_mae_pct))),
    h('p',{class:'inline-note'},value===null?f.reason||'模型或最新参考日线待就绪':`参考 ${row.reference_date} 收盘 ${num(row.reference_close)} 点 · ${row.model_family||'研究模型'}`)));
}
// These returns describe research portfolios, never official index point targets.
function equityValue(item) {
  return item && ['ready','baseline','partial','issued'].includes(item.status) && finite(item.predicted_return_pct)
    ? Number(item.predicted_return_pct) : null;
}
function equityStatus(item) {
  const value=equityValue(item);
  if(value===null)return ({pending:'等待数据 / 训练',insufficient_history:'本站可用训练样本不足',suspended:'暂停预测',stale:'数据待更新'})[item?.status]||'尚未形成预测';
  if(item.status==='partial')return'部分覆盖 · 研究结果';
  return item.status==='baseline'||/zero|baseline|naive/.test(item.model||'')?'基线预测':'个股汇总 · 研究预测';
}
function equityTarget(item) {
  return ({next_open:'下一交易日开盘收益',next_close:'下一交易日收盘收益',today_close:'今日收盘收益'})[item?.target_kind]||'目标时段待确定';
}
function researchHelp(title,meaning,formula,note) {
  const dialog=h('dialog',{class:'metric-dialog','aria-labelledby':'research-help-title'},
    h('div',{class:'metric-dialog-head'},h('h3',{id:'research-help-title'},title),button('关闭',()=>dialog.close())),
    h('p',{class:'metric-meaning'},meaning),h('pre',{class:'metric-formula'},formula),h('p',{class:'metric-meaning'},note));
  dialog.addEventListener('close',()=>dialog.remove());dialog.addEventListener('click',e=>{if(e.target===dialog)dialog.close();});
  document.body.append(dialog);dialog.showModal();
}
function equityHelp() {
  return h('button',{class:'metric-help',type:'button','aria-label':'查看逐股汇总公式与覆盖率说明',onClick:()=>researchHelp('逐股预测与研究组合',
    '共享模型为每只股票分别预测，再按预测时已知流通市值加权。整体组合直接汇总去重股票，不把重叠板块再次平均。缺失股票保留在覆盖率分母中。',
    'wᵢ = 股票流通市值ᵢ / 已覆盖股票流通市值之和\n组合预测收益 = Σ wᵢ × 个股预测收益ᵢ\n市值覆盖率 = 已预测股票流通市值 / 股票池已知流通市值\n下一开盘收益 = 下一日开盘价 / 下一日除权参考前收盘价 − 1',
    '这是已覆盖成分股研究组合，不是申万或上证官方指数点位。未知市值另计数量，不能视为零市值。下一日除权参考价尚未确定时，不换算绝对股价。日线模型不能替代经过验证的盘中模型。')},'?');
}
function aggregateBody(item={},compact=false) {
  const value=equityValue(item);
  return h('div',{class:`equity-aggregate ${compact?'compact':''}`},
    h('div',{class:'equity-lead'},h('div',{},pill(equityStatus(item),value!==null?'official':''),h('div',{class:'equity-target'},`${item.target_date||'目标日期待定'} · ${equityTarget(item)}`)),
      h('div',{class:`equity-return ${sign(value)}`},value===null?'待生成':pct(value),h('small',{},'已覆盖部分 · 加权收益'))),
    h('div',{class:'equity-coverage'},h('span',{},'已预测 / 股票池 ',h('strong',{},`${item.predicted_count??0} / ${item.universe_count??0}`)),
      h('span',{},'已知市值覆盖 ',h('strong',{},accuracy(item.weight_coverage))),finite(item.known_weight_count)?h('span',{},`市值可用 ${item.known_weight_count} / ${item.universe_count??0} 只`):null,equityHelp()),
    h('div',{class:'equity-coverage-track','aria-hidden':'true'},h('i',{style:`width:${Math.max(0,Math.min(100,Number(item.weight_coverage||0)*100))}%`})),
    h('p',{class:'inline-note'},item.note||'逐股数据和有效模型就绪后生成；缺失结果不按零收益补齐。'),
    !compact?h('div',{class:'equity-foot'},h('span',{},`信息时点 ${dateText(item.as_of)}`),h('span',{},`模型 ${item.model||'待训练'}`)):null);
}
function equitySummary(d,compact=false) {
  const f=d.equity_forecast||{},market={...f.coverage,...f.market};
  for(const k of ['status','as_of','target_date','target_kind','model'])if(market[k]===undefined)market[k]=f[k];
  return panel('A 股整体 · 个股汇总预测','逐股输出 → 板块组合 → 全市场去重汇总',aggregateBody(market,compact),
    compact?button('逐股结果 ↗',()=>go('forecast'),'text-button'):null,'equity-summary wide-panel');
}
function sectorEquityPanel(sec) {
  return panel(`${sec.name} · 成分股研究组合`,'个股预测按流通市值汇总 · 与官方指数点位分开展示',aggregateBody(sec.equity_prediction||{}));
}
function fundPremium(f) {
  if(!finite(f.price)||!finite(f.nav)||Number(f.nav)<=0||!f.price_date||chinaDay(f.price_date)!==chinaDay(f.nav_date))return null;
  return (Number(f.price)/Number(f.nav)-1)*100;
}
function fundHelp(f) {
  return h('button',{class:'metric-help',type:'button','aria-label':`查看${f.name||'基金'}净值、规模与溢折价说明`,onClick:()=>researchHelp('基金价值怎么读',
    '市场价是基金在交易所的成交价格；单位净值是每份基金对应的净资产。基金规模反映资产总额，不是上市公司的市值。每项数值保留自己的数据日期。',
    '单位净值 = 基金净资产 / 基金份额\n规模（亿元）= Tushare total_size（万元）/ 10,000\n同日溢折价 = (市场价格 / 单位净值 − 1) × 100%',
    '只有行情与净值日期一致时才计算收盘溢折价；正值为溢价，负值为折价。单位净值高低不代表便宜或昂贵，也不是预测收益。券商关联说明股权关系，不代表该券商直接管理基金。')},'?');
}
function fundsPanel(sec) {
  const funds=Array.isArray(sec.funds)?sec.funds.slice(0,3):[];
  return panel('券商关联基金','大型券商关联管理人 · 行业 / 主题匹配',funds.length?h('div',{class:'fund-grid'},funds.map(f=>{
    const premium=fundPremium(f),match=f.match_status==='holdings_verified'?'持仓关联已核验':'主题关联 · 持仓待核验';
    return h('article',{class:'fund-card'},h('div',{class:'fund-title'},h('div',{},h('h4',{},link(f.name||f.code,f.source_url)),h('span',{class:'ticker'},f.code)),fundHelp(f)),
      h('p',{class:'fund-manager'},f.manager||'管理人待确认',h('br'),f.broker_relation||'券商股权关系待确认'),
      h('div',{class:'fund-values'},h('div',{},h('span',{},'市场价'),h('strong',{},finite(f.price)?`${num(f.price,3)} 元`:'—'),h('small',{},f.price_date||'行情待更新')),
        h('div',{},h('span',{},'单位净值'),h('strong',{},finite(f.nav)?`${num(f.nav,4)} 元`:'—'),h('small',{},f.nav_date||'净值待更新')),
        h('div',{},h('span',{},'基金规模'),h('strong',{},amount(f.aum_cny)),h('small',{},f.aum_date||'规模待更新'))),
      h('div',{class:'fund-premium'},h('span',{},'同日收盘溢折价'),h('strong',{},premium===null?'日期不齐 / 待数据':pct(premium))),
      h('div',{class:'tags'},pill(match,f.match_status==='holdings_verified'?'reliable':'')),
      h('p',{class:'inline-note'},f.match_basis||'基金与本行业的关联依据仍待补齐。'));
  })):empty('暂无足够匹配的基金',sec.funds_note||'等待券商关联行业基金数据；不以宽基或海外产品凑足数量。'),null,'funds-panel');
}
function equityStockPage(d) {
  const source=Array.isArray(d.equity_forecast?.stocks)?d.equity_forecast.stocks:[];
  const query=String(state.equitySearch||'').trim().toLowerCase();
  const matched=source.filter(x=>(state.equitySector==='all'||x.sector_id===state.equitySector)&&`${x.code||''} ${x.name||''} ${sectorName(x.sector_id,d)}`.toLowerCase().includes(query));
  const pageCount=Math.max(1,Math.ceil(matched.length/30));
  const page=Math.max(0,Math.min(pageCount-1,Number.isInteger(state.equityPage)?state.equityPage:0));
  return{rows:matched.slice(page*30,(page+1)*30),count:matched.length,page,pageCount};
}
function equityStocksPanel(d) {
  const result=equityStockPage(d);state.equityPage=result.page;
  return panel('逐股预测明细',`${result.count} 只符合筛选 · 每页最多 30 只 · 未生成结果保留原因`,h('div',{},
    h('div',{class:'equity-table-tools'},h('label',{class:'search'},icon('search',14),h('input',{id:'equity-search',type:'search',value:state.equitySearch,placeholder:'搜索股票名称、代码或行业','aria-label':'搜索个股预测',onInput:e=>{state.equitySearch=e.target.value;state.equityPage=0;renderKeepFocus('equity-search',e.target.selectionStart);}})),
      h('select',{class:'select','aria-label':'按行业筛选个股预测',onChange:e=>{state.equitySector=e.target.value;state.equityPage=0;render();}},[['all','全部行业'],...d.sectors.map(s=>[s.id,s.name])].map(([v,label])=>h('option',{value:v,selected:state.equitySector===v},label)))),
    result.rows.length?h('div',{class:'table-wrap'},h('table',{class:'equity-stock-table'},h('thead',{},h('tr',{},['股票 / 行业','预测收益','目标时点','状态 / 模型'].map(x=>h('th',{},x)))),h('tbody',{},result.rows.map(x=>h('tr',{},
      h('td',{},h('strong',{},x.name||x.code),h('div',{class:'panel-subtitle'},`${x.code} · ${sectorName(x.sector_id,d)}`)),
      h('td',{class:`numeric ${sign(equityValue(x))}`},equityValue(x)===null?'—':pct(equityValue(x)),h('div',{class:'panel-subtitle'},'相对目标交易日除权参考价')),
      h('td',{},x.target_date||'待确定',h('div',{class:'panel-subtitle'},equityTarget(x)),h('div',{class:'panel-subtitle'},`特征日期 ${x.reference_date||'待更新'}`)),
      h('td',{},equityStatus(x),h('div',{class:'panel-subtitle'},x.model||'模型待就绪'),x.note?h('div',{class:'panel-subtitle'},x.note):null)))))):empty('暂无符合条件的个股结果','数据管道按交易日批量补齐日线；本站历史缓存尚未补齐或模型未就绪时保留空值。'),
    h('div',{class:'equity-pagination'},h('span',{},`第 ${result.page+1} / ${result.pageCount} 页 · ${result.count} 只`),h('div',{},
      h('button',{type:'button',class:'outline-button',disabled:result.page===0,onClick:()=>{state.equityPage--;render();}},'上一页'),
      h('button',{type:'button',class:'outline-button',disabled:result.page>=result.pageCount-1,onClick:()=>{state.equityPage++;render();}},'下一页')))),null,'wide-panel');
}
function equityTrainingPanel(d) {
  const f=d.equity_forecast||{},t=f.training||{},e=f.evaluation||{},c=d.equity_collection||{},n=t.neural_candidates||{};
  const status=t.inference_only?'本机训练 · 云端推断':t.promoted?'已通过升级门槛':['validated','evaluated'].includes(t.status)?'候选已评估 · 保留基线':['training','evaluating'].includes(t.status)?'候选模型验证中':'数据与训练准备中';
  const neural=Object.entries(n.models||{});
  const collectionText=`本站已归档 ${Number(c.cached_days||0)+Number(c.collected_days||0)} / 本次计划 ${c.requested_days||180} 个交易日 · 本轮请求 ${c.requests_this_run||0} 次`;
  const reason=({rate_limited:'请求达到来源限制',authorization_denied:'来源拒绝授权',permission_denied:'接口权限不足',points_insufficient:'来源报告积分不足',provider_error:'来源返回业务错误'})[c.provider_reason]||({api_denied:'上次下载请求未成功，具体原因尚未识别',transport_error:'连接暂不可用',incomplete_batch:'返回数据未通过完整性检查'})[c.error_code]||c.error_code;
  return panel('个股模型 · 训练与核验',status,h('div',{class:'mini-info'},h('div',{class:'tags'},pill(`当前 ${f.model||'尚未训练'}`,'official'),pill('GRU / TCN / 集成候选'),pill(`训练 ${t.train_samples??0} · 验证 ${t.validation_samples??0} · 测试 ${t.test_samples??0}`)),
    h('p',{class:'inline-note'},collectionText),h('p',{class:'inline-note'},'Tushare daily 提供历史日线；这里的计划天数是本站采集预算，不是接口历史上限。'),reason?h('div',{class:'info-strip'},`${reason}；已保留缓存。${c.cooldown_until?`最早恢复时间 ${dateText(c.cooldown_until)}，由后续任务续传。`:'等待后续更新。'}`):null,
    t.training_note?h('p',{class:'inline-note'},t.training_note):null,t.deployed_model?.status==='active'?h('p',{class:'inline-note'},`已加载本机模型 ${t.deployed_model.model_id} · 训练时间 ${dateText(t.deployed_model.trained_at)}`):null,h('p',{class:'inline-note'},t.note||'共用日期边界划分训练、验证和测试样本，保留时间间隔。神经网络需优于基线与当前模型，才替换正式预测。'),
    neural.length?h('div',{},h('p',{class:'inline-note'},`离线候选 · ${n.device||'设备待确认'} · ${n.selected_stocks??n.stocks??0} 只股票；${t.deployed_model?.status==='active'?'线上版本见上方模型信息':'尚未部署神经网络'}。`),h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},['候选模型','开盘验证MAE','收盘验证MAE','已训练轮次'].map(x=>h('th',{},x)))),h('tbody',{},neural.map(([name,r])=>h('tr',{},h('td',{},name.toUpperCase()),h('td',{},num(r.validation?.next_open_return_pct?.mae_pct,4)),h('td',{},num(r.validation?.next_close_return_pct?.mae_pct,4)),h('td',{},r.epochs_completed??'—'))))))):h('p',{class:'inline-note'},'GRU / TCN 尚无真实数据试训报告；本机完成训练与验证后再上传报告。'),
    h('div',{class:'equity-evaluation'},h('div',{},h('span',{},'已结算个股预测'),h('strong',{},e.settled_count??0)),h('div',{},h('span',{},'已结算方向命中率'),h('strong',{},accuracy(e.direction_accuracy))),h('div',{},h('span',{},'平均绝对误差'),h('strong',{},finite(e.mae_pct)?`${num(e.mae_pct)} 个百分点`:'—'))),
    h('div',{class:'info-strip'},'当前上市股票与当前申万成分的历史研究存在幸存偏差和成员回填限制。日线样本不能证明盘中预测能力；记录原始预测，真实结果到达后再评估。')) ,null,'wide-panel');
}
function equitySectorPredictions(d) {
  return panel('各行业 · 逐股汇总',taxonomyLabel(d),h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},['行业','组合预测收益','个股覆盖','已知市值覆盖','状态'].map(x=>h('th',{},x)))),h('tbody',{},d.sectors.map(sec=>{
    const p=sec.equity_prediction||{},value=equityValue(p);
    return h('tr',{},h('td',{},button(sec.name,()=>{state.selected=sec.id;state.search='';state.group='全部';go('sectors');},'sector-name')),
      h('td',{class:`numeric ${sign(value)}`},value===null?'—':pct(value)),h('td',{class:'numeric'},`${p.predicted_count??0} / ${p.universe_count??0}`),
      h('td',{class:'numeric'},accuracy(p.weight_coverage)),h('td',{},equityStatus(p),h('div',{class:'panel-subtitle'},p.target_date||'目标日期待定')));
  })))),null,'wide-panel');
}

function forecast(d){return h('div',{},heading('指数与行业 · 收盘方向','直接训练涨跌分类，展示模型概率与实际检验结果。','PREDICTION LAB'),d.cloud_model_status?h('p',{class:'inline-note'},d.cloud_model_status):null,probabilityPanel(d),h('details',{class:'legacy-forecasts'},h('summary',{},'原涨跌幅回归模型与历史输出'),dailyForecastPanel(d)),h('details',{class:'legacy-forecasts'},h('summary',{},'个股辅助研究与成分股组合'),equitySummary(d),equityTrainingPanel(d),equityStocksPanel(d),equitySectorPredictions(d)),h('details',{class:'legacy-forecasts'},h('summary',{},'辅助开盘 / 盘中模型与历史预测对照'),panel('模型运行状态',['ready','active'].includes(d.models?.status)?'模型已准备 · 请结合样本外验证观察':'研究阶段 · 先积累可验证样本',h('div',{class:'mini-info'},h('div',{class:'tags'},pill(`主模型 ${d.models?.champion||'待训练'}`,'official'),pill(`挑战模型 ${d.models?.challenger||'未启用'}`),pill(`最近训练 ${dateText(d.models?.last_trained_at)}`)),h('div',{class:'info-strip'},'日频模型用于下一次开盘研究。盘中缺少分钟级样本时，只显示显式基准，不把日频回测包装成盘中准确率。新闻与情绪特征只有在保留首次采集时间、积累可验证样本后才能训练。')) ,null,'wide-panel'),h('div',{class:'toolbar'},h('div',{class:'theme-heading'},`预测记录 · ${d.predictions.length} 个观察标的`),button('查看每日复盘',()=>go('review'),'outline-button','chart')),d.predictions.length?h('div',{class:'prediction-grid'},d.predictions.map(p=>predictionCard(p,d))):panel('预测输出','尚未满足生成条件',empty('等待模型与数据就绪','缺少数据、市场日历或有效样本时，系统保留空值和原因。','activity'))) );}
function predictionCard(p,d){let value=finite(p.predicted_price)?num(p.predicted_price):'待生成';let status=finite(p.predicted_price)?(/naive|zero_return|zero-return/.test(p.model_version||'')?'基线预测':'研究预测'):p.status==='abstained'?'暂不预测':'样本积累中';return h('article',{class:'prediction-card'},h('div',{class:'panel-tools',style:'justify-content:space-between'},h('h3',{},p.sector_name||sectorName(p.sector_id,d)),pill(status,finite(p.predicted_price)?'official':'')),h('div',{class:'target'},`${p.target_date||'目标日期待确定'} · ${p.target_label||targetLabel(p)}`),h('div',{class:'prediction-number'},value,h('span',{style:'font-size:10px;color:#94a1b0;letter-spacing:0;margin-left:7px'},finite(p.predicted_price)?p.basis==='etf'?'元':'点':'')),h('div',{class:'prediction-range'},`参考区间　${num(p.lower_price)} — ${num(p.upper_price)}`),h('div',{class:'kv'},h('span',{},'预期涨跌 / 上涨概率'),h('span',{},`${pct(p.predicted_return_pct)} / ${accuracy(p.up_probability)}`)),h('div',{class:'kv'},h('span',{},'参考点位'),h('span',{class:'numeric'},num(p.reference_price))),h('div',{class:'kv'},h('span',{},'有效样本'),h('span',{},`${p.sample_count||0} 条`)),h('div',{class:'kv'},h('span',{},'模型版本'),h('span',{},p.model_version||'尚未训练')),h('div',{class:'kv'},h('span',{},'信息截点'),h('span',{},dateText(p.features_as_of||p.issued_at))),h('div',{class:'kv'},h('span',{},'消息特征'),h('span',{},({active:'已用于本次模型',collecting:'历史样本积累中',disabled:'未启用'})[p.news_features]||'待验证')),p.news_note?h('p',{class:'inline-note'},p.news_note):null,h('div',{class:'model-note'},p.reason||'区间来自历史误差，实际覆盖率需要持续检验。未校准时不展示上涨概率。'));}
function metric(label,value,sub){return h('div',{class:'metric'},h('p',{},label),h('strong',{},value),h('small',{},sub));}
function taskMetrics(r){const rows=r.by_task||[];return panel('验证口径',r.evaluation_cohort||'按目标交易日期统计；今日发出数按生成日期单独计数。',rows.length?h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},['预测任务','已验证','方向命中率','MAE','基准 MAE'].map(x=>h('th',{},x)))),h('tbody',{},rows.map(x=>h('tr',{},h('td',{},targetLabel(x)),h('td',{class:'numeric'},x.count||0),h('td',{class:'numeric'},accuracy(x.direction_accuracy)),h('td',{class:'numeric'},finite(x.mae_pct)?`${num(x.mae_pct)} 个百分点`:'—'),h('td',{class:'numeric'},finite(x.baseline_mae_pct)?`${num(x.baseline_mae_pct)} 个百分点`:'—')))))):h('div',{class:'mini-info'},'收盘与开盘任务会分别展示，防止不同目标混淆。'),null,'wide-panel');}
function review(d){const r=d.daily_review||{},sent=r.sentiment||{},ledger=d.ledger||[],by=r.by_sector||[];return h('div',{},heading('每日复盘，校准判断',d.research_as_of?`${r.date||'今日'} · 盘后研究更新 ${dateText(d.research_as_of)}`:`${r.date||'今日'} · 将所有预测与结果对照，也审视情绪判断的边界。`,'DAILY REVIEW'),h('div',{class:'metrics-grid'},metric('今日到期已结算',`${r.settled_count??0}`,`今日发出 ${r.issued_count??0} · 待验证 ${r.pending_count??0} · 暂不预测 ${r.abstained_count??0}`),metric('方向命中率',accuracy(r.direction_accuracy),'只统计已匹配真实结果的预测'),metric('平均绝对误差',finite(r.mae_pct)?`${num(r.mae_pct)} 个百分点`:'—','预测点位相对参考价的绝对误差'),metric('相对基准改进',finite(r.skill_pct)?`${num(r.skill_pct,1)}%`:'—',`基准 MAE ${finite(r.baseline_mae_pct)?`${num(r.baseline_mae_pct)} 个百分点`:'—'}`)),taskMetrics(r),h('div',{class:'overview-grid'},panel('板块准确性','以目标日期归属当日；样本数不足时不下结论',by.length?h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},['板块','已验证','方向命中','MAE'].map(t=>h('th',{},t)))),h('tbody',{},by.map(x=>h('tr',{},h('td',{},x.name||sectorName(x.sector_id,d)),h('td',{class:'numeric'},x.count||0),h('td',{class:'numeric'},accuracy(x.direction_accuracy)),h('td',{class:'numeric'},finite(x.mae_pct)?`${num(x.mae_pct)} 个百分点`:'—')))))):empty('今天还没有已结算结果','目标交易时点到达后，系统自动匹配实际行情。','chart')),r.model_settlements?.closing_point&&r.model_settlements?.closing_direction?h('div',{class:'breadth'},h('span',{},`次日收盘点位已结算 ${r.model_settlements.closing_point.count} · MAE ${pct(r.model_settlements.closing_point.mae_pct)}`),h('span',{},`次日收盘方向已结算 ${r.model_settlements.closing_direction.count} · 命中率 ${accuracy(r.model_settlements.closing_direction.accuracy)}`)):null,panel('情绪判断汇总','规则标签与价格走势分别观察',h('div',{class:'prediction-mini'},...[["偏正面",sent.positive,'up'],['偏负面',sent.negative,'down'],['中性',sent.neutral,'neutral'],['不确定',sent.uncertain,'neutral']].map(([label,count,cls])=>h('div',{class:'pred-row'},h('strong',{class:cls},label),h('span',{class:'numeric'},count??0))),h('div',{class:'info-strip'},h('div',{},`情绪与后续走势一致率：${accuracy(sent.alignment_rate)}（${sent.alignment_count||0} 条可比样本）`),h('div',{},`情绪标签准确率：${finite(sent.label_accuracy)?accuracy(sent.label_accuracy):'待人工标注真值'}`),h('div',{},`未核实传言：${sent.rumor_count||0} 条 · 默认排除主模型`)),h('p',{class:'inline-note'},sent.note||'一致性不是因果证据，也不等于情绪分类准确率。')))),h('div',{class:'toolbar',style:'margin-top:25px'},h('div',{},h('h3',{class:'panel-title'},'预测台账'),h('p',{class:'panel-subtitle'},'保留原始预测；实际结果单独结算，不覆盖历史判断。')),button('导出 CSV',()=>exportLedger(d),'outline-button','download')),panel('全部预测记录',`共 ${ledger.length} 条 · ${state.demo?'合成演示记录':'按生成时间保留'}`,ledger.length?h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},['生成时间','板块','目标','预测点位','实际点位','误差','方向','状态'].map(t=>h('th',{},t)))),h('tbody',{},ledger.slice().reverse().map(p=>h('tr',{},h('td',{},dateText(p.issued_at)),h('td',{},p.sector_name||sectorName(p.sector_id,d)),h('td',{},`${p.target_date||'—'} ${targetLabel(p)}`),h('td',{class:'numeric'},num(p.predicted_price)),h('td',{class:'numeric'},num(p.actual_price)),h('td',{class:'numeric'},pct(p.error_pct)),h('td',{},p.direction_correct===true?pill('命中','reliable'):p.direction_correct===false?pill('未命中','rumor'):'—'),h('td',{},p.settled_at?pill('已结算','reliable'):finite(p.predicted_price)?pill('待验证'):pill('暂不预测'))))))):empty('预测台账尚为空','第一条预测生成后会在这里永久保留，供后续对照。','file'))) ;}
function exportLedger(d){const columns=[['id','ID'],['sector_name','板块'],['issued_at','生成时间'],['target_date','目标日期'],['target_kind','目标类型'],['model_version','模型版本'],['reference_price','基准价格'],['predicted_price','预测点位'],['lower_price','区间下界'],['upper_price','区间上界'],['actual_price','实际点位'],['error_pct','误差百分点'],['direction_correct','方向命中'],['settled_at','结算时间']];const cell=v=>`"${String(v??'').replace(/^[=+@-]/,"'$&").replaceAll('"','""')}"`;const rows=[columns.map(c=>cell(c[1])).join(','),...d.ledger.map(r=>columns.map(([k])=>cell(r[k])).join(','))];download(`guanlan-ledger${state.demo?'-DEMO':''}.csv`,'\ufeff'+rows.join('\r\n'),'text/csv;charset=utf-8');}
function settings(d){return h('div',{},heading('数据与研究设置','查看来源健康状态，管理本地偏好与后端配置。','DATA & PREFERENCES'),h('div',{class:'settings-grid'},h('div',{class:'stack'},panel('浏览器偏好','以下设置仅保存在当前浏览器',h('div',{class:'form-body'},h('label',{class:'form-row'},h('div',{},h('strong',{},'自动刷新研究快照'),h('p',{},relayOnly?'按各数据源的发布周期读取行情、资讯与研究快照。':'每 60 秒重新读取已发布的数据；不会触发后台采集。')),h('input',{type:'checkbox',class:'switch',checked:state.preferences.autoRefresh,onChange:e=>setPreference('autoRefresh',e.target.checked)})),h('label',{class:'form-row'},h('div',{},h('strong',{},'在全部资讯中包含传言'),h('p',{},'仍保留未核实标记；不改变主模型的输入规则。')),h('input',{type:'checkbox',class:'switch',checked:state.preferences.showRumors,onChange:e=>setPreference('showRumors',e.target.checked)})),h('div',{class:'form-row'},h('div',{},h('strong',{},'我的自选板块'),h('p',{},`当前保存 ${state.favorites.length} 个板块，仅保存在本机。`)),button('清空自选',()=>{state.favorites=[];local.set('guanlan.favorites',[]);toast('已清空当前浏览器的自选板块');},'outline-button')))),panel('后端配置','GitHub Actions 或本机执行，配置需提交仓库生效',h('div',{class:'form-body'},h('div',{class:'status-box'},'网站是静态研究快照，不能在浏览器中启动训练或安全保存密钥。下载配置后，请合并至项目配置并重新运行数据任务。'),h('label',{class:'form-row'},h('div',{},h('strong',{},'训练模式'),h('p',{},'训练与调优在本机完成；GitHub只负责推断、核验和发布。')),h('select',{class:'select',id:'model-select','aria-label':'训练模式'},h('option',{value:'ridge'},'Ridge 基线'),h('option',{value:'ridge_mlp'},'Ridge + 小型 MLP'))),h('div',{class:'form-actions'},button('下载配置建议',()=>{const model=$('#model-select').value;download('research-settings.suggestion.json',JSON.stringify({note:'配置建议，需按项目实际字段合并；不会自动更新服务器。',model:model,exclude_unverified_rumors:true,require_point_in_time_news:true,timezone:'Asia/Shanghai',request_interval_seconds:2,max_retries:2},null,2));toast('配置建议已下载；后台设置尚未改变');},'outline-button','download')),h('p',{class:'source-detail'},'API 密钥只放在 GitHub Secrets 或本机环境变量。不要写入公开 JSON、网页或仓库历史。')))),h('div',{class:'stack'},sourcePermissions(d),panel('数据源健康','连接状态与行情核验状态分别判断',sourceList(d.sources)),checkSummary(d),panel('当前数据快照','透明呈现覆盖范围与数据时效',h('div',{class:'form-body'},h('div',{class:'kv'},h('span',{},'最后生成'),h('span',{},dateText(d.generated_at))),h('div',{class:'kv'},h('span',{},'数据模式'),h('span',{},state.demo?'合成演示':'真实数据')),h('div',{class:'kv'},h('span',{},'板块报价 / 配置数量'),h('span',{},`${sourceCoverage(d).quoted} / ${d.sectors.length}`)),h('div',{class:'kv'},h('span',{},'参考ETF覆盖 / 配置数量'),h('span',{},`${sourceCoverage(d).proxy} / ${d.sectors.length}`)),h('div',{class:'kv'},h('span',{},'资讯条数'),h('span',{},d.news.length)),h('div',{class:'kv'},h('span',{},'新闻特征状态'),h('span',{},d.models?.news_features||'尚未接入')),h('div',{class:'divider'}),h('ul',{class:'notice-list'},(d.notices?.length?d.notices:['定时任务可能延迟，以数据时间为准。','免费来源可能限流、变更或无法返回数据。','市值快照需要逐日积累，不以指数代替。']).map(n=>h('li',{},n))),h('div',{class:'form-actions'},button('下载当前快照',()=>download(`dashboard${state.demo?'-DEMO':''}.json`,JSON.stringify(d,null,2)),'outline-button','download')))))) );}
function analytics(){const cfg=state.analytics||{};const dashboard=safeURL(cfg.dashboard_url);const connected=cfg.enabled&&dashboard;return h('div',{},heading('访问统计','通过独立统计服务观察网站访问，不在浏览器中累加虚构访客数。','SITE ANALYTICS'),h('div',{class:'metrics-grid'},metric('页面浏览量','—','在私人统计后台查看'),metric('独立会话估计','—','不等于准确的独立人数'),metric('流量来源','—','在私人统计后台查看'),metric('热门页面','—','按导航页面分别记录')),panel('统计服务',connected?'GoatCounter 已配置':'GoatCounter · 待连接',h('div',{class:'form-body'},h('div',{class:'status-box'},connected?'统计脚本已配置；实际记录仍取决于访问者网络、拦截设置及统计服务可用性。这个页面不读取你的私人统计数据。':'尚未连接统计账户，因此当前没有真实访问统计。注册并配置统计端点后，可在私人后台查看浏览量、来源、设备和热门页面。'),connected?link('打开私人统计后台 ↗',dashboard,'outline-button primary'):null,h('div',{class:'divider'}),h('ul',{class:'notice-list'},h('li',{},'页面浏览量记录访问事件，独立会话是服务估计值。'),h('li',{},'过滤机器人、广告拦截与网络失败均可能影响统计。'),h('li',{},'API 密钥不应写入前端；本网页只保存公开统计端点。')))) );}
let demoCache=null;
function demoData(original){if(demoCache)return demoCache;let seed=37;const random=()=>{seed=(seed*1664525+1013904223)>>>0;return seed/4294967296;};const now=new Date(),today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai'}).format(now);function history(base,n=110){let v=base;return Array.from({length:n},(_,i)=>{let day=new Date(now);day.setDate(day.getDate()-(n-i));let open=v;v*=1+(random()-.43)*.025;return{date:day.toISOString().slice(0,10),open:+open.toFixed(2),close:+v.toFixed(2),high:+(Math.max(open,v)*1.009).toFixed(2),low:+(Math.min(open,v)*.994).toFixed(2),volume:Math.round(1e8*random())};});}const fallback=[['semiconductor','半导体','科技成长'],['ai','人工智能','科技成长'],['compute','算力','科技成长'],['robot','机器人','先进制造'],['pv','光伏设备','能源转型'],['battery','电池','能源转型'],['pharma','创新药','医药健康'],['bank','银行','金融地产'],['broker','证券','金融地产'],['gold','黄金','资源周期'],['oil','石油石化','资源周期'],['liquor','白酒','消费生活'],['defense','国防军工','先进制造'],['car','汽车','先进制造'],['agri','农业','消费生活'],['shipping','航运','资源周期']];const secs=original?.sectors?.length?original.sectors.slice(0,40):fallback.map(([id,name,group])=>({id,name,group,kind:'industry',drivers:['需求周期','政策变化'],upstream:['原材料','关键设备'],downstream:['终端需求']}));demoCache={...baseData(),data_mode:'demo',status:'ok',generated_at:now.toISOString(),market_session:{phase:'demo',label:'合成场景 · 非实际行情'},market:[['sh000001','上证指数',3296,.84],['sz399001','深证成指',10842,1.26],['sz399006','创业板指',2219,1.73],['sh000300','沪深300',3865,.67]].map(([id,name,price,change_pct])=>({id,name,price,change_pct,as_of:now.toISOString(),history:history(price*.92),market_cap_history:history(4e13).map(x=>({date:x.date,value:x.close}))})),sectors:secs.map((sec,i)=>{let hist=history(1000+i*132);return{...sec,price:hist.at(-1).close,change_pct:+(3.6-i*.29+(random()-.5)*1.4).toFixed(2),market_cap:(2+random()*9)*1e12,as_of:now.toISOString(),history:hist,market_cap_history:hist.map(x=>({date:x.date,value:x.close*1e9})),provider:'合成演示数据',stocks:[{name:'演示成分股 A',code:'DEMO01',price:26.78,change_pct:2.31,market_cap:4.2e11},{name:'演示成分股 B',code:'DEMO02',price:115.36,change_pct:-.84,market_cap:3.8e11},{name:'演示成分股 C',code:'DEMO03',price:48.21,change_pct:1.07,market_cap:2.7e11}]};}),sources:[{id:'demo_market',name:'演示行情源',type:'market',status:'ok',message:'合成示例',last_success:now.toISOString()},{id:'demo_news',name:'演示资讯源',type:'news',status:'ok',message:'合成示例',last_success:now.toISOString()}],models:{champion:'ridge-demo-v1',challenger:'mlp-32-16-demo',status:'ready',news_features:'演示 · 尚不代表真实训练',last_trained_at:now.toISOString()},notices:['当前全部数值与事件为合成演示，不代表真实市场。']};const ids=demoCache.sectors.map(x=>x.id);demoCache.news=[{title:'[演示] 产业支持政策变化，观察设备与制造环节的传导',source:'模拟政策公报',credibility:'official',category:'国际政策',sector_ids:ids.slice(0,2),impact:'positive',transmission:['政策预期','资本开支','设备需求']},{title:'[演示] 能源价格出现波动，上下游利润分配受到关注',source:'模拟能源观察',credibility:'reliable',category:'能源定价',sector_ids:ids.slice(9,11),impact:'uncertain',transmission:['能源成本','制造利润','终端定价']},{title:'[演示] 供应链交付周期变化，关注库存与订单的错配',source:'模拟产业报道',credibility:'reliable',category:'供应链',sector_ids:ids.slice(2,4),impact:'negative',transmission:['交期延长','库存调整','订单兑现']},{title:'[演示] 网络流传行业订单消息，尚待独立来源核实',source:'模拟网络信息',credibility:'rumor',category:'未核实',sector_ids:ids.slice(4,5),impact:'uncertain',transmission:['传言传播','情绪变化','事实待核验']}].map((n,i)=>({...n,id:`demo-news-${i}`,summary:'此条为界面演示而编写，不是真实新闻。实际使用时会显示来源、采集时间和证据链接。',published_at:new Date(now.getTime()-(i+1)*2200000).toISOString(),first_seen_at:now.toISOString(),analysis_method:'keyword_rules',url:null}));demoCache.predictions=demoCache.sectors.slice(0,9).map((sec,i)=>({id:`demo-p-${i}`,sector_id:sec.id,sector_name:sec.name,issued_at:now.toISOString(),target_date:today,target_kind:'next_open',target_label:'下次开盘点位 · 合成示例',status:'issued',reason:'合成示例：展示点位预测与误差区间，不代表实际模型表现。',model_version:'ridge-demo-v1',reference_price:sec.price,predicted_price:sec.price*(1+.003-i*.0006),predicted_return_pct:.3-i*.06,lower_price:sec.price*.989,upper_price:sec.price*1.013,up_probability:null,sample_count:240,basis:'index',features_as_of:now.toISOString()}));demoCache.ledger=demoCache.predictions.map((p,i)=>({...p,actual_price:i<6?p.reference_price*(1+.004-i*.0012):null,error_pct:i<6?.1+i*.08:null,direction_correct:i<6?i!==3:null,settled_at:i<6?now.toISOString():null,status:i<6?'settled':'pending'}));demoCache.daily_review={date:today,issued_count:9,settled_count:6,pending_count:3,abstained_count:0,direction_accuracy:5/6,mae_pct:.3,baseline_mae_pct:.46,skill_pct:34.8,by_sector:demoCache.sectors.slice(0,6).map((s,i)=>({sector_id:s.id,name:s.name,count:1,direction_accuracy:i===3?0:1,mae_pct:.1+i*.08})),sentiment:{positive:1,negative:1,neutral:0,uncertain:2,rumor_count:1,alignment_count:2,alignment_rate:.5,label_accuracy:null,note:'合成演示指标；情绪准确率仍需人工标签真值。'}};return demoCache;}
const sectionLabels={market:'指数',sectors:'行业',news:'资讯',news_digest:'资讯总览',predictions:'预测',ledger:'台账',
 sources:'数据来源',data_checks:'数据核验',market_session:'交易状态',models:'模型',intraday:'分时',notices:'通知',
 daily_review:'复盘',equity_forecast:'个股预测',daily_forecast:'收盘预测',direction_probability:'方向预测'};
function validateSnapshot(result,previous){
 if(!record(result)||result.schema_version!==1||!('market' in result||'sectors' in result))throw new Error('数据格式不符合约定');
 let clean=result;
 const missing=new Set();
 function reject(key,field=key){
  if(previous===undefined)throw new Error(`数据字段 ${field} 格式不符合约定`);
  if(clean===result)clean={...result};
  missing.add(key);
  if(previous&&key in previous)clean[key]=previous[key];
  else delete clean[key];
 }
 for(const key of ['market','sectors','predictions','ledger','sources','data_checks']){
  if((key==='market'||key==='sectors'||key in result)&&!records(result[key]))reject(key);
 }
 for(const key of ['market_session','models','intraday','equity_forecast','daily_forecast','direction_probability']){
  if(key in result&&!record(result[key]))reject(key);
 }
 if('daily_review' in result&&!validReviewRows(result.daily_review))reject('daily_review');
 if('notices' in result&&!Array.isArray(result.notices))reject('notices');
 if('news' in result&&!validArticles(result.news))reject('news');
 if(result.news_digest&&!validDigest(result.news_digest))reject('news_digest');
 if(record(result.equity_forecast)&&'stocks' in result.equity_forecast&&!records(result.equity_forecast.stocks))reject('equity_forecast','equity_forecast.stocks');
 for(const model of ['direction_probability','daily_forecast'])for(const key of ['market','sectors','ledger','stale_entities']){
  if(record(result[model])&&key in result[model]&&!records(result[model][key]))reject(model,`${model}.${key}`);
 }
 if(previous!==undefined&&missing.size)clean.unavailable_sections=[...missing].map(key=>sectionLabels[key]);
 return clean;
}
function records(value){return Array.isArray(value)&&value.every(record);}
function record(value){return value!==null&&typeof value==='object'&&!Array.isArray(value);}
function optionalArray(value){return value==null||Array.isArray(value);}
function validArticles(items){
 return records(items)&&items.every(n=>typeof n.id==='string'&&optionalArray(n.sector_ids)&&
  (!n.llm_analysis||(optionalArray(n.llm_analysis.uncertainties)&&
   (n.llm_analysis.sector_impacts==null||records(n.llm_analysis.sector_impacts)&&n.llm_analysis.sector_impacts.every(row=>optionalArray(row.evidence))))));
}
function validModelRows(model){
 return model==null||record(model)&&['market','sectors','ledger','stale_entities'].every(key=>model[key]==null||records(model[key]));
}
function validReviewRows(review){
 return record(review)&&['by_sector','by_task'].every(key=>review[key]==null||records(review[key]));
}
function validDigest(digest){
 return record(digest)&&['positives','risks','watch'].every(key=>digest[key]==null||
  records(digest[key])&&digest[key].every(row=>optionalArray(row.article_ids)&&optionalArray(row.sectors)));
}
function responseError(response){
 const error=new Error(`HTTP ${response.status}`),header=response.headers?.get('Retry-After');
 if(header){
  const until=/^\d+$/.test(header)?Date.now()+1000*Number(header):Date.parse(header);
  if(Number.isFinite(until))error.retryAt=until;
 }
 return error;
}
function fetchPublished(url,options={}){
 // The full historical fallback is much larger than the separately published feeds.
 const timeout=url.includes('/dashboard.json')?180000:url.includes('/view.json')?45000:12000;
 if(typeof AbortSignal.timeout==='function')return fetch(url,{...options,signal:AbortSignal.timeout(timeout)});
 const controller=new AbortController();
 setTimeout(()=>controller.abort(),timeout);
 return fetch(url,{...options,signal:controller.signal});
}
function requestSnapshot(fetcher=fetchPublished,cacheBust=null,previous){
 const options={cache:cacheBust===null?'no-cache':'no-store'},suffix=cacheBust===null?'':`?t=${cacheBust}`;
 const parse=response=>{if(!response.ok)throw responseError(response);return response.json();};
 return fetcher(`./data/view.json${suffix}`,options).then(response=>response.status===404&&!relayOnly?
  fetcher(`./data/dashboard.json${suffix}`,options).then(parse):parse(response)).then(result=>validateSnapshot(result,previous));
}
const publications=new Map();
const marketPages=['overview','sectors','settings'];
const feeds=[
 {file:'live-quotes',key:'liveQuotes',label:'行情',interval:5000,pages:marketPages,source:'sxdata_relay',
  valid:p=>records(p.market)&&records(p.sectors)&&Boolean(quoteSnapshotStamp(p))},
 {file:'today-intraday',key:'relayIntraday',label:'分时',interval:60000,pages:marketPages,source:'sxdata_relay',
  valid:p=>record(p.series)&&Object.values(p.series).every(Array.isArray)},
 {file:'minute-recovery',key:'minuteRecovery',label:'分钟补采',interval:60000,pages:marketPages,source:'sxdata_minute_recovery',
  valid:p=>/^\d{4}-\d{2}-\d{2}$/.test(p.trade_date)&&quoteTime(p.generated_at)!==null&&record(p.series)&&Object.values(p.series).every(Array.isArray)},
 {file:'stock-quotes',key:'relayStocks',label:'个股',interval:30000,pages:marketPages,source:'sxdata_relay',
  valid:p=>records(p.stocks)&&(p.board_estimates==null||records(p.board_estimates))},
 {file:'cloud-news',key:'cloudNews',label:'资讯',interval:30000,pages:['news',...marketPages],source:'tencent_news',
  valid:p=>/^\d{4}-\d{2}-\d{2}$/.test(p.trade_date)&&validArticles(p.articles)&&
    (p.recent_articles==null||validArticles(p.recent_articles))&&validDigest(p.news_digest)&&
    (p.previous_digest==null||validDigest(p.previous_digest))},
 {file:'cloud-models',key:'cloudModels',label:'模型',interval:60000,pages:['forecast',...marketPages],source:'tencent_models',
  valid:p=>typeof p.status==='string'&&validModelRows(p.direction_probability)&&validModelRows(p.daily_forecast)},
 {file:'daily-market',key:'cloudDaily',label:'日线',interval:300000,pages:marketPages,source:'sxdata_daily',
  valid:p=>record(p.bars)&&record(p.stocks)&&Object.values(p.bars).every(bucket=>record(bucket)&&Object.values(bucket).every(bar=>record(bar)&&typeof bar.date==='string'))},
 {file:'cloud-research',key:'cloudResearch',label:'盘后研究',interval:300000,pages:['forecast','review',...marketPages],source:'tencent_research',
  valid:p=>record(p.entities)&&Object.values(p.entities).every(record)&&validReviewRows(p.daily_review)&&
    ['data_checks','ledger'].every(key=>p[key]==null||records(p[key]))},
];
const dashboardFeed={key:'data',label:'研究快照'};
const pageQuoteFeed={key:'pageQuotes',file:'page-quotes',label:'网页行情',pages:marketPages,
 valid:p=>records(p.market)&&records(p.sectors)&&record(p.intraday)};
function visiblePublication(spec){return !spec.pages||spec.pages.includes(state.page);}
function publicationNotice(){
 const issues=[...publications.values()].filter(item=>item.failures&&visiblePublication(item.spec));
 const sections=state.data?.unavailable_sections;
 if(!issues.length&&!Array.isArray(sections))return null;
 return h('div',{class:'banner',role:'status'},icon('info',15),h('span',{},Array.isArray(sections)?`等待新快照修复以下字段：${sections.join('、')}。`:'',issues.map(({spec})=>
  `${spec.label}暂时读取失败，${state[spec.key]?'保留上次数据':'等待首次成功读取'}`).join('。')));
}
function renderAfterRefresh(){
 const focus=document.activeElement;
 const search=focus?.id&&focus.type==='search'?{id:focus.id,start:focus.selectionStart,end:focus.selectionEnd}:null;
 const opened=new Set(Array.from(document.querySelectorAll?.('details[data-reader-key][open]')||[],el=>el.dataset.readerKey));
 render();
 if(opened.size)for(const el of document.querySelectorAll('details[data-reader-key]')){
  if(opened.has(el.dataset.readerKey))el.open=true;
 }
 if(search){
  const input=document.getElementById(search.id);
  input?.focus({preventScroll:true});input?.setSelectionRange(search.start,search.end);
 }
}
async function updatePublication(spec,read){
 let task=publications.get(spec.key);
 if(!task){task={spec,pending:false,failures:0,retryAt:0};publications.set(spec.key,task);}
 if(task.pending||Date.now()<task.retryAt)return;
 task.pending=true;
 task.lastStartedAt=Date.now();
 const previous=state[spec.key],failures=task.failures;
 try{
  const value=await read();
  if(value){task.previous=previous;state[spec.key]=value;}
  task.failures=0;task.retryAt=0;
  if(spec===dashboardFeed)state.loading=false;
  if(!state.demo&&visiblePublication(spec)&&(value||failures))renderAfterRefresh();
 }catch(error){
  state[spec.key]=previous;
  task.failures=failures+1;
  const delay=Math.min(300000,15000*2**Math.min(5,failures));
  task.retryAt=Math.max(Date.now()+delay,error.retryAt||0);
  if(spec===dashboardFeed)state.loading=false;
  if(!failures&&!state.demo&&visiblePublication(spec))renderAfterRefresh();
 }finally{task.pending=false;}
}
function refreshFeed(spec){
 return updatePublication(spec,async()=>{
  const previous=state[spec.key];
  const response=await fetchPublished(`./data/${spec.file}.json`,{cache:'no-cache'});
  if(!response.ok)throw responseError(response);
  const version=response.headers.get('ETag');
  if(version&&version===previous?.version)return null;
  const next=await response.json();
  if(next?.schema_version!==1||(spec.source&&next.source!==spec.source)||!spec.valid(next))throw Error('invalid publication');
  if(next.trade_date&&previous?.trade_date&&next.trade_date<previous.trade_date)return null;
  if(spec.key==='minuteRecovery'&&previous?.trade_date===next.trade_date&&quoteTime(next.generated_at)<quoteTime(previous.generated_at))return null;
  if(spec.key==='cloudNews')next.news_digest=retainDayDigest(next.news_digest,previous?.news_digest,next.trade_date);
  if(spec.key==='liveQuotes'&&quoteSnapshotStamp(next)<=quoteSnapshotStamp(previous))return null;
  return {...next,version};
 });
}
function refresh(includeFeeds=true){
 if(state.loading&&!publications.has('data'))render();
 const jobs=[updatePublication(dashboardFeed,async()=>{
  const result=await requestSnapshot(fetchPublished,null,state.data);
  if(state.data?.generated_at&&result.generated_at&&result.generated_at<state.data.generated_at)return null;
  demoCache=null;
  return {...baseData(),...result};
 })];
 if(relayOnly&&includeFeeds)jobs.push(...feeds.map(refreshFeed));
 if(!relayOnly)jobs.push(refreshFeed(pageQuoteFeed));
 return Promise.all(jobs);
}
const hash=location.hash.slice(1);if(pages.some(x=>x[0]===hash))state.page=hash;
window.addEventListener('hashchange',()=>{const next=location.hash.slice(1);if(pages.some(x=>x[0]===next)){state.page=next;render();}});
function quietQuoteSession(quote,now){
 if(!quote?.trade_date)return false;
 const beijing=new Date(now+8*3600000);
 if(quote.trade_date!==beijing.toISOString().slice(0,10))return false;
 const minute=beijing.getUTCHours()*60+beijing.getUTCMinutes(),phase=quote.collector?.session;
 if(phase==='closed')return true;
 if(['pre_open','opening_auction','auction_complete'].includes(phase))return minute<570;
 if(phase==='midday_break')return minute>=690&&minute<780;
 return phase==='post_close'&&minute>=900;
}
function pollFeed(feed,now=Date.now()){
 if(feed.source==='sxdata_relay'&&quietQuoteSession(state.liveQuotes,now)&&
    now-(publications.get(feed.key)?.lastStartedAt??-Infinity)<300000)return;
 return refreshFeed(feed);
}
function poll(action){if(state.preferences.autoRefresh&&!state.demo&&!document.hidden)action();}
setInterval(()=>poll(()=>refresh(false)),relayOnly?300000:60000);
if(relayOnly)for(const feed of feeds)setInterval(()=>poll(()=>pollFeed(feed)),feed.interval);
refresh();

fetch('./data/analytics.json',{cache:'no-store'}).then(r=>r.ok?r.json():{}).then(cfg=>{state.analytics=cfg; if(state.page==='analytics')render();}).catch(()=>{});
