'use strict';

// All remote text enters the document through textContent. No data is evaluated.
const $ = (q, root = document) => root.querySelector(q);
const NS = 'http://www.w3.org/2000/svg';
const local = { get(k, fallback) { try { return JSON.parse(localStorage.getItem(k)) ?? fallback; } catch { return fallback; } }, set(k,v) { try { localStorage.setItem(k,JSON.stringify(v)); } catch {} } };
const state = { page: 'overview', demo: false, data: null, loading: true, group: '全部', search: '', sort: 'change', taxonomy:'sw', pageQuotes:null, selected: null, marketSelected:null, overviewSelected:null, range: 21, metric: 'intraday', newsFilter: '全部', newsSearch:'', newsLimit:12, favorites: local.get('guanlan.favorites',[]), preferences: local.get('guanlan.preferences',{showRumors:false,autoRefresh:true}), toast: null };
const pages = [['overview','市场总览','grid'],['sectors','板块雷达','layers'],['news','A股关联资讯','globe'],['trend','趋势观察室','activity'],['settings','数据设置','settings']];
const newsOverview={sector:null,sort:'volume'};
const trendView={window:10,group:'sw',filter:'all',query:'',page:0,expanded:null,sorts:{}};
const trendWindows=[5,10,20];
const trendStates={'-2':'大幅下降','-1':'抖动下降',0:'波动',1:'波动上升',2:'快速上升'};
const trendReasons={missing_window:'窗口内连续收盘日线不足',missing_baseline:'历史波动基准不足',zero_baseline:'历史波动基准为零',calendar:'等待确认交易日历'};
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
function go(page) {if(page==='news'&&state.page!==page)state.newsLimit=12;state.page=page;history.replaceState(null,'',`#${page}`);render();loadVisibleDirectoryFeeds();window.dispatchEvent(new CustomEvent('radar:navigate',{detail:{page,title:pages.find(x=>x[0]===page)?.[1]||page}}));window.scrollTo({top:0,behavior:'instant'});}
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
const directoryView={scope:'hot',query:'',page:0,composing:false,selection:{},views:{}};
const directoryPageSize=12;
const radarView={ths:false,kind:'all',page:0,composing:false};
const thsKinds={industry:'行业',concept:'概念',theme:'主题'};
const sectorCode=/^\d{6}\.(?:SH|SZ)$/;
const thsCode=/^\d{6}R?\.TI$/;
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
 mergeCloudNews(d,relayOnly&&!state.demo?state.cloudNews:null);
 if(relayOnly&&!state.demo){
  const research=state.cloudResearch;
  if(research){
   const withResearch=item=>({...item,...research.entities?.[item.id]});
   d.market=d.market.map(withResearch);
   d.sectors=d.sectors.map(withResearch);
   for(const key of ['data_checks','validation_summary','fund_summary','fund_state','equity_collection']){
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
  const sessions={morning:'上午交易中',afternoon:'下午交易中',midday_break:'午间休市',closing_auction:'收盘集合竞价',post_close:'已收盘',closed:'休市'};
  if(feed?.trade_date===chinaDay(new Date())&&sessions[feed.collector?.session]){
   d.market_session={...d.market_session,label:sessions[feed.collector.session]};
  }
  d.eastmoney_sectors=[];
  d.sources=connectedSources(state.connectedSources, feed);
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
// Older China-market feeds can omit the UTC offset on their local wall clock.
function newsPublishedTime(value){
 if(typeof value!=='string'||!value.trim())return null;
 const stamp=value.trim();
 return quoteTime(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(stamp)?stamp.replace(' ','T')+'+08:00':stamp);
}
function mergeCloudNews(d,news){
 let rows=d.news;
 if(news){
  const articles=new Map(rows.map(article=>[article.id,article]));
  for(const article of [...(news.recent_articles||[]),...news.articles]){
   articles.set(article.id,{...article,analysis_queue:'tencent_news'});
  }
  rows=[...articles.values()];
 }
 d.news=rows.map(article=>({article,published:newsPublishedTime(article.published_at)??-Infinity}))
  .sort((a,b)=>b.published-a.published||String(a.article.id||'').localeCompare(String(b.article.id||'')))
  .map(row=>row.article);
 if(!news)return;
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
function usingThsRadar(){return relayOnly&&!state.demo&&state.page==='sectors'&&radarView.ths;}
function industryOptions(){return relayOnly&&!state.demo?[['sse',state.page==='overview'?'板块行情目录':'交易所行业与主题'],['sw','申万行业 · 权重股估算'],...(state.page==='sectors'?[['ths','同花顺板块']]:[])]:[['sw','申万行业'],['eastmoney','东方财富 · 网页采样']];}
function providerName(value){return({stock_api:relayOnly?'SXData':'Tushare',tushare:'Tushare',tencent:'腾讯行情',sina:'新浪行情',eastmoney:'东方财富',baostock:'BaoStock',eastmoney_page:'东方财富网页'})[value]||value||'来源待确认';}
const chinaDate=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai'});
function chinaDay(value){if(!value)return null;const text=String(value);if(/^\d{8}$/.test(text))return`${text.slice(0,4)}-${text.slice(4,6)}-${text.slice(6,8)}`;if(/^\d{4}-\d{2}-\d{2}$/.test(text))return text;const d=new Date(value);return Number.isFinite(d.getTime())?chinaDate.format(d):null;}
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
function validationBadge(entity){const status=validationStatus(entity);const map={verified:['多源已核对','reliable'],single_source:['单源 · 未交叉核验','rumor'],conflict:['行情数据冲突','up'],not_comparable:['暂无同日核验','rumor']};return status?pill(...(map[status]||['待交叉核验',''])):null;}
function validationDetail(entity){const v=entity?.validation;if(!v)return null;return h('div',{class:`validation-note ${validationStatus(entity)==='conflict'?'conflict':''}`},validationBadge(entity),h('span',{},typeof v==='object'?v.message||v.reason||v.note||'以同一标的、交易日期与价格口径核对。':'以同一标的、交易日期与价格口径核对。'));}
function checkSummary(d){const checks=d.data_checks||[];const counts={verified:0,single_source:0,conflict:0,not_comparable:0};checks.forEach(x=>{if(x.status in counts)counts[x.status]++;});return panel('行情交叉验证','同一标的、同一交易时点、同一价格口径才可比较',h('div',{class:'form-body'},h('div',{class:'tags'},pill(`已核对 ${counts.verified}`,'reliable'),pill(`单源 ${counts.single_source}`,'rumor'),pill(`冲突 ${counts.conflict}`,counts.conflict?'up':''),pill(`不可比 ${counts.not_comparable}`)),checks.length?h('div',{class:'sources-list',style:'padding:10px 0 0'},checks.map(check=>h('div',{class:'source-row'},h('div',{},h('strong',{},check.name||sectorName(check.entity_id,d)),h('p',{},check.message||check.reason||check.note||'需核对相同交易日期与口径')),validationBadge({validation:check})))):h('p',{class:'inline-note'},'等待多源采集结果。数据源连接成功不等于行情已通过交叉验证。'),counts.conflict?h('div',{class:'validation-note conflict'},'存在行情冲突，请核对来源、交易日期与价格口径。'):null));}


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
 if(item.directoryEntry){
  const series=state.sectorSeries;
  const history=series?.id===item.id&&series.trade_date===day?{trade_date:day,series:{[item.id]:series.points}}:null;
  const core=state.relayIntraday;
  const samples=[...(core?.trade_date===day?core.series?.[item.id]||[]:[]),...(history?.series[item.id]||[])];
  return relayIntraday({trade_date:day,series:{[item.id]:samples}},item.live_quote,item,day,state.minuteRecovery);
 }
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
function sameIntradaySession(a,b){
  return (a.minute<=120 && b.minute<=120) || (a.minute>=210 && b.minute>=210);
}
function mainlandSessions(d,item){
  const mainland=/^(?:(?:sh|sz)\d{6}|\d{6}\.(?:SH|SZ|SI))$/i;
  if(!mainland.test(item.code) && !mainland.test(item.id))return [];
  return (d.market||[]).filter(m=>/^(?:sh000001|sz399001)$/.test(m.id))
   .flatMap(m=>(m.history||[]).filter(r=>finite(r.close)&&Number(r.close)>0).map(r=>r.date));
}
function chart(history,{height=205,compact=false,color='#4c85d5',metric='index',range=state.range,unit='点',today=false,sessions=[],caption=null}={}) {
  let values=points(history,metric);if(range)values=values.slice(-range);
  if(today?!values.length:values.length<2||(metric==='intraday'&&new Set(values.map(v=>v.date)).size<2)){
   if(compact)return null;
   const emptyPlot=h('div',{class:'empty-chart'},h('span',{},today?'今日暂无分时数据':metric==='intraday'?'分时有效报价时点不足两个 · 等待下一次采样':metric==='cap'?'历史市值样本不足 · 持续记录中':'等待真实行情历史'));
   return caption?h('div',{},caption,emptyPlot):emptyPlot;
  }
  const w=620, hh=height, left=compact?0:8,right=compact?0:56,top=compact?3:14,bottom=compact?3:30;
  let lo=Math.min(...values.map(v=>Number(v.value))),hi=Math.max(...values.map(v=>Number(v.value)));let pad=(hi-lo)*.18||Math.max(Math.abs(hi)*.005,1);lo-=pad;hi+=pad;
  const firstTime=new Date(values[0].date).getTime(),lastTime=new Date(values.at(-1).date).getTime();
  const sessionX=minute=>left+minute/330*(w-left-right);
  const sampleX=v=>sessionX(Math.min(v.minute<=120?120:330,v.seconds/60));
  const dailyDays=metric==='index' ? [...new Set([...sessions,...history.map(r=>r.date)])]
    .filter(day=>typeof day==='string' && day>=values[0].date && day<=values.at(-1).date).sort() : [];
  const daySlot=new Map(dailyDays.map((date,i)=>[date,i]));
  const dailyBreak=i=>i>0 && daySlot.get(values[i].date)>daySlot.get(values[i-1].date)+1;
  const X=i=>{
   if(today)return sampleX(values[i]);
   const fraction=metric==='intraday'&&lastTime>firstTime?(new Date(values[i].date).getTime()-firstTime)/(lastTime-firstTime)
     :dailyDays.length>1?daySlot.get(values[i].date)/(dailyDays.length-1):i/(values.length-1);
   return left+fraction*(w-left-right);
  };
  const Y=v=>top+(hi-Number(v))/(hi-lo)*(hh-top-bottom);
  const chartLabel=`${metric==='intraday'?'分时':metric==='cap'?'市值':'指数'}趋势，${values[0].date}至${values.at(-1).date}`;
  const el=s('svg',{viewBox:`0 0 ${w} ${hh}`,class:compact?'spark':today?'chart session-chart':'chart',role:'img','aria-label':chartLabel});
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
    if(prior&&sameIntradaySession(prior,first)){
     el.append(s('path',{class:'session-interpolation',d:`M${sampleX(prior).toFixed(2)} ${Y(prior.value).toFixed(2)} L${sampleX(first).toFixed(2)} ${Y(first.value).toFixed(2)}`,stroke:color,'stroke-width':1.45,'stroke-dasharray':'3 3',opacity:.62,fill:'none','pointer-events':'none'}));
    }
    if(group.length===1){
     const [v]=group;el.append(s('circle',{class:'isolated-sample',cx:sampleX(v),cy:Y(v.value),r:2,fill:color}));
    }else{
     const path=group.map((v,i)=>`${i?'L':'M'}${sampleX(v).toFixed(2)} ${Y(v.value).toFixed(2)}`).join(' ');
     el.append(s('path',{class:'session-line',d:path,stroke:color,'stroke-width':1.8,fill:'none','stroke-linecap':'round','stroke-linejoin':'round'}));
    }
   }
  }else if(!compact){for(let i=0;i<4;i++){let j=Math.round(i*(values.length-1)/3);const t=s('text',{x:X(j),y:hh-7,'text-anchor':i===0?'start':i===3?'end':'middle'});t.textContent=metric==='intraday'?dateText(values[j].date,true):String(values[j].date).slice(5);el.append(t);}}
  const line=today?'':values.map((v,i)=>`${i&&!dailyBreak(i)?'L':'M'}${X(i).toFixed(2)} ${Y(v.value).toFixed(2)}`).join(' ');
  const breaks=dailyDays.length ? values.map((v,i)=>i).filter(dailyBreak) : [];
  if(!compact&&metric!=='intraday'&&!breaks.length)el.append(s('path',{d:`${line} L${X(values.length-1)} ${hh-bottom} L${left} ${hh-bottom} Z`,fill:color,opacity:.065}));
  for(const i of breaks)el.append(s('path',{class:'session-interpolation',d:`M${X(i-1)} ${Y(values[i-1].value)} L${X(i)} ${Y(values[i].value)}`,stroke:color,'stroke-width':1.45,'stroke-dasharray':'3 3',opacity:.62,fill:'none','pointer-events':'none'}));
  const providerColor=p=>p==='sina'?'#b68d4f':p==='tencent'?'#4c85d5':color;
  if(!today&&metric==='intraday'){for(const provider of new Set(values.map(v=>v.provider))){const grouped=values.map((v,i)=>({v,i})).filter(x=>x.v.provider===provider);const path=grouped.map((x,j)=>`${j?'L':'M'}${X(x.i).toFixed(2)} ${Y(x.v.value).toFixed(2)}`).join(' ');el.append(s('path',{d:path,stroke:providerColor(provider),'stroke-width':1.7,fill:'none','stroke-dasharray':'3 3'}));}}else if(!today)el.append(s('path',{d:line,stroke:color,'stroke-width':compact?3:2,fill:'none','stroke-linecap':'round','stroke-linejoin':'round'}));
  if(!today&&metric==='intraday'){values.forEach((v,i)=>{const dot=s('circle',{cx:X(i),cy:Y(v.value),r:4,fill:providerColor(v.provider),'fill-opacity':.65,tabindex:0});const label=s('title');label.textContent=`${v.timestamp_quality==='retrieved_only'?'采集时间（未确认源时点）':'来源时间'} ${dateText(v.date,false)} · ${num(v.value,3)} · ${v.provider||'来源未提供'} · ${v.timestamp_quality==='retrieved_only'?'仅采集时点':'来源报价时点'}`;dot.append(label);el.append(dot);});}if(!today&&!compact){let last=values.at(-1);el.append(s('circle',{cx:X(values.length-1),cy:Y(last.value),r:3.5,fill:color,stroke:'#fff','stroke-width':2}));const title=s('title');title.textContent=`${last.date}: ${num(last.value)}`;el.append(title);}if(!compact){
   el.setAttribute('tabindex','0');el.setAttribute('role','group');
   const overlay=s('g',{class:'chart-crosshair',visibility:'hidden','pointer-events':'none'});
   const vertical=s('line',{y1:top,y2:hh-bottom,stroke:'#334155','stroke-width':1,'stroke-dasharray':'4 3'});
   const intersection=s('circle',{r:5,fill:color,stroke:'#fff','stroke-width':2});
   const stamp=h('span'),value=h('strong'),source=h('span');
   const readout=h('div',{class:'chart-readout','aria-hidden':'true'},stamp,value,source);
   overlay.append(vertical,intersection);el.append(overlay);let active=values.length-1;
   function show(i){active=Math.max(0,Math.min(values.length-1,i));const v=values[active],x=X(active),y=Y(v.value);
    overlay.setAttribute('visibility','visible');vertical.setAttribute('x1',x);vertical.setAttribute('x2',x);intersection.setAttribute('cx',x);intersection.setAttribute('cy',y);
    stamp.textContent=(today?chinaClock.format(quoteTime(v.date)):metric==='intraday'?dateText(v.date,false):v.date)+(v.timestamp_quality==='page_observed'?' · 网页观测':v.timestamp_quality==='minute_backfill'?' · 补采分钟收盘价':'');
    value.textContent=metric==='cap'?amount(v.value):num(v.value,metric==='intraday'?3:2)+' '+unit;
    source.textContent=v.provider?providerName(v.provider):'';
    readout.setAttribute('data-active','true');
    el.setAttribute('aria-label',`${stamp.textContent} ${value.textContent}${source.textContent?' · '+source.textContent:''}，左右方向键查看相邻样本`);
   }
   function hide(){overlay.setAttribute('visibility','hidden');readout.setAttribute('data-active','false');el.setAttribute('aria-label',chartLabel);}
   el.addEventListener('pointermove',e=>{const point=el.createSVGPoint();point.x=e.clientX;point.y=e.clientY;const ctm=el.getScreenCTM();if(!ctm)return;const x=point.matrixTransform(ctm.inverse()).x;let i=0;for(let j=1;j<values.length;j++)if(Math.abs(X(j)-x)<Math.abs(X(i)-x))i=j;if(today&&(x>sessionX(120)&&x<sessionX(210)||Math.abs(X(i)-x)>sessionX(1)-left)){hide();return;}show(i);});
   el.addEventListener('pointerleave',hide);
   el.addEventListener('focus',()=>show(active));el.addEventListener('blur',hide);
   el.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();show(e.key==='Home'?0:e.key==='End'?values.length-1:active+(e.key==='ArrowRight'?1:-1));}});
   return h('div',{},h('div',{class:'chart-header'},caption,readout),el);
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
  const newsScroll=document.getElementById('news-sector-scroll')?.scrollTop;
  const quoteFocus=focus?.matches?.('.index-select, .heat-tile, .overview-leader, [data-overview-focus]')?focus.id:null;
  const view={overview,sectors,news:newsPage,trend:trendRoom,settings,analytics}[state.page]||overview;
  const {d,cov,body,conflict}=pageWithAvailableData(view),current=pages.find(x=>x[0]===state.page)||pages[0];
  const sidebar=h('aside',{class:'sidebar'},h('div',{class:'brand'},h('div',{class:'brand-symbol'},icon('logo',23)),h('div',{class:'brand-text'},h('h1',{},'观澜'),h('small',{},'MARKET OBSERVATORY'))),h('div',{class:'nav-title'},'研究工作台'),h('nav',{class:'nav','aria-label':'主导航'},pages.map(([id,label,ico])=>h('button',{type:'button',class:state.page===id?'active':'',title:label,'aria-current':state.page===id?'page':null,onClick:()=>go(id)},icon(ico,18),h('span',{},label)))),h('div',{class:'side-bottom'},h('div',{class:'small-divider'}),h('div',{class:'side-label'},'数据状态'),h('div',{class:'side-status'},h('i',{class:`status-dot ${cov.healthy?'live':''}`}),`${cov.healthy} / ${d.sources.length} 数据源可用`),h('div',{class:'side-caption'},relayOnly?'SXData 主源 · 指数分批更新':'Tushare 主源 · 五分钟行情采样'),relayOnly?h('a',{href:'./training-progress/',class:'training-progress-link'},'180 天新闻采集进度'):null));
  const topbar=h('header',{class:'topbar'},h('div',{class:'breadcrumb'},'工作台',h('span',{},'/'),h('strong',{},current[1])),h('div',{class:'top-actions'},h('span',{class:'clock'},`${dateText(d.generated_at)} · 北京时间`),button(state.demo?'演示模式 · ON':'查看演示',changeDemo,`outline-button demo-button ${state.demo?'on':''}`),button('刷新',()=>refresh(),'outline-button','refresh'),relayOnly?h('a',{class:'outline-button',href:'./account/'},'账户'):null));
  const main=h('main',{id:'main',class:'content',tabindex:-1},
    state.demo?h('div',{class:'banner demo'},icon('info',15),h('span',{},'演示模式：以下行情及新闻为合成示例，仅用于体验界面。'),h('span',{class:'spacer'}),button('返回真实数据',changeDemo,'')):null,
    !state.demo&&!d.generated_at&&!publications.get('data')?.failures?h('div',{class:'banner'},icon('info',15),h('span',{},state.loading?'正在加载研究数据…':'等待首次数据采集。行情将在数据管道成功运行后显示。'),h('span',{class:'spacer'}),button('查看数据状态',()=>go('settings'),'')):null,
    !state.demo?publicationNotice():null,
    conflict?h('div',{class:'banner conflict'},icon('info',15),'多源行情存在冲突，请在数据设置中核对来源、交易日期与价格口径。'):null,
    body,h('footer',{class:'footnote'},h('span',{},`观澜 · A股研究工作台　|　${state.demo?'合成演示数据':`行业报价 · 今日 ${cov.today} / ${cov.total} · 有参考价 ${cov.quoted} / ${cov.total}`}`),h('span',{},'趋势观察依据已完成的收盘日线。')));
  const shell=h('div',{class:'shell'},sidebar,h('div',{class:'main-wrap'},topbar,main));$('#app').replaceChildren(shell);observeNewsTail();if(state.toast)$('#app').append(h('div',{class:'toast',role:'status'},state.toast));
  const newsTable=document.getElementById('news-sector-scroll');
  if(newsTable&&newsScroll!==undefined)newsTable.scrollTop=newsScroll;
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
    return h('tr',{},h('td',{},assetResearchLink('stock',st,st.name||st.ts_code),h('div',{class:'panel-subtitle'},st.ts_code)),h('td',{},pct(st.weight_pct).replace(/^\+/,'')),
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
 if(visibleDirectory()&&sectorCode.test(id))requestSectorSeries(id);
}
function assetResearchLink(type, item, label) {
  let code = item.ts_code || item.index_code || item.code || item.id || '';
  const oldIndex = type === 'index' && /^(sh|sz)(\d{6})$/i.exec(code);
  if (oldIndex) code = oldIndex[2] + '.' + oldIndex[1].toUpperCase();
  const supported = type === 'index' ? /^\d{6}\.(SH|SZ)$/ : /^(?:\d{6}\.(?:SH|SZ|BJ|NQ)|\d{5,6}\.HK)$/;
  if (state.demo || !supported.test(code)) return type === 'stock' ? label : null;
  return h('a', {class:'directory-details', href:'./asset/?type='+type+'&code='+encodeURIComponent(code),
    'aria-label':'查看'+(item.name || code)+'的研究指标'}, label);
}
function sectorDetailsLink(item,context='preview'){
 return h('a',{class:'directory-details',id:'sector-link-'+context+'-'+item.id,'data-overview-focus':true,href:'./sector/?code='+encodeURIComponent(item.id),
  'aria-label':item.name+'成分与研究详情'},'成分与研究 ↗');
}
function boardSample(code,today) {
 const packet=state.boardLiveSummary;
 if(!packet || packet.trade_date!==today)return null;
 const row=packet.items?.[code], at=quoteTime(row?.quote_at);
 if(!row||at===null||at>Date.now()||chinaDay(at)!==today||!finite(row.change_pct)||
    !Number.isInteger(row.total_count)||!Number.isInteger(row.quoted_count)||row.total_count<=0||
    row.quoted_count<Math.ceil(row.total_count*.8)||row.quoted_count>row.total_count)return null;
 return row;
}
function thsSnapshotRows(item){
 const ranking=item.ranking,reference=item.reference;
 const entries=[];
 const sampled=boardSample(item.id,chinaDay(new Date()));
 if(sampled)entries.push(['成分股等权涨跌',pct(sampled.change_pct)],['采样覆盖',sampled.quoted_count+'/'+sampled.total_count+' 只'],
  ['报价时点',chinaClock.format(quoteTime(sampled.quote_at))]);
 if(ranking){
  entries.push(['热榜涨跌',pct(ranking.change_pct)],['热榜点位',finite(ranking.price)&&ranking.price>0?num(ranking.price)+' 点':'待更新']);
  if(finite(ranking.hot))entries.push(['热度',num(ranking.hot,0)]);
 }
 if(reference)entries.push([reference.quote_date+' 收盘',num(reference.price)+' 点'],['收盘涨跌',pct(reference.change_pct)]);
 return entries;
}
function snapshotTime(hot){
 if(!hot)return '';
 const stamp=String(hot.rank_time||'');
 const clock=/^\d{2}:\d{2}(?::\d{2})?$/.test(stamp)?stamp:quoteTime(stamp)!==null?chinaClock.format(quoteTime(stamp)):'';
 return [hot.trade_date,clock].filter(Boolean).join(' ');
}
function thsSectorPanel(item){
 const entries=thsSnapshotRows(item);
 return panel(item.name+' · 行情快照',boardSample(item.id,chinaDay(new Date()))?'当日成分股采样':item.ranking?'同花顺概念热榜 · '+snapshotTime(state.sectorHot):'同花顺 · 收盘参考',
  h('div',{class:'directory-snapshot'},entries.length?h('dl',{},entries.map(([label,value])=>[h('dt',{},label),h('dd',{},value)])):
   h('p',{},'已收录该板块，等待中转站返回带日期的行情。')),sectorDetailsLink(item));
}
function heatmap(d,limit=12,comparison=sectorComparison(d.sectors),selectedId=state.overviewSelected){
 const today=chinaDay(new Date());
 return h('div',{class:'heatmap'},comparison.rows.slice(0,limit).map(({sector:sec,q})=>{
  const ths=sec.taxonomy==='THS',rank=sec.ranking,reference=sec.reference;
  const comparable=Boolean(comparison.day&&q.day===comparison.day&&finite(q.price)&&finite(q.value));
  const sampled=(ths || sec.id.endsWith('.SI')) ? boardSample(sec.id,today) : null;
  const value=sampled?sampled.change_pct:ths?(rank?rank.change_pct:reference?.change_pct):comparable?q.value:null;
  const price=ths?(rank?rank.price:reference?.price):q.price;
  const stamp=q.estimate?sec.intraday_estimate?.quote_timestamp:q.quote?.as_of;
  const time=quoteTime(stamp,q.quote?.daily&&!q.estimate);
  const when=sampled?'成分等权 '+chinaClock.format(quoteTime(sampled.quote_at)):ths?(rank?'热榜 '+snapshotTime(state.sectorHot):reference?reference.quote_date+' 收盘':'待更新'):
   !q.day?'待更新':q.day!==today?`${q.day} 参考`:time===null?'时点待确认':q.day.slice(5)+' '+chinaClock.format(time);
  const basis=sampled?'':q.estimate?'权重股估算':q.proxy?'ETF代理':'';
  const selected=selectedId===sec.id;
  const category=ths?'同花顺'+({industry:'行业',concept:'概念',theme:'主题'}[sec.kind]||''):
   sec.directoryEntry?'交易所'+({industry:'行业',theme:'主题'}[sec.kind]||'指数'):'申万一级行业';
  const enriched=relayOnly&&!state.demo&&(sec.directoryEntry||isCanonicalSector(sec));
  const tile=h('button',{class:`heat-tile ${finite(value)?value>0?'positive':value<0?'negative':'':''} ${Math.abs(value)>1.8?'strong':''} ${selected?'selected-sector':''}`,
   id:`overview-industry-${sec.id}`,type:'button','aria-pressed':String(selected),'aria-controls':'overview-sector-trend',
   onClick:()=>chooseOverviewIndustry(sec.id),title:[sec.fullname||sec.name,category,basis,when].filter(Boolean).join(' · ')},
   h('span',{class:'name'},rank?h('small',{class:'directory-rank'},'#'+rank.rank):null,sec.name),
   enriched?h('span',{class:'directory-code'},category+' · '+sec.id):null,h('strong',{class:'change'},pct(value)),
   h('span',{class:'industry-quote'},sampled?`覆盖 ${sampled.quoted_count}/${sampled.total_count} 只`:finite(price)&&price>0?`${num(price)} 点`:'点位待更新'),
   h('span',{class:'industry-stamp'},[basis,when].filter(Boolean).join(' · ')),
   rank&&finite(rank.hot)?h('span',{class:'industry-stamp'},'热度 '+num(rank.hot,0)):null);
  return enriched?h('article',{class:'sector-listing'},tile,h('div',{class:'directory-foot'},
   h('p',{},sec.intro||'板块简介待更新。'),sectorDetailsLink(sec,'card'))):tile;
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
   h('div',{class:'chart-body'},chart(hist,{height:today?170:205,metric,range:intraday?null:view.range,today:today&&intraday,sessions:metric==='index'?mainlandSessions(d,item):[],caption:h('div',{class:'chart-meta'},h('strong',{},metric==='cap'?amount(item.market_cap):intraday?num(ticks.at(-1)?.price):num(mainQuote.price)),h('span',{},today&&intraday&&ticks.length?chinaClock.format(ticks.at(-1).time):item.name||'上证指数'))})),
   today&&intraday?quoteActivity(item):null,
   today&&intraday&&estimate?h('p',{class:'chart-data-note'},`采用 ${estimate.basis_date} 市值加权 · 覆盖 ${estimate.sample_count}/${estimate.sample_size} 只样本股（样本权重 ${accuracy(estimate.weight_coverage)}）`):null,
   metric==='cap'&&today&&!hist?.length?h('p',{class:'chart-data-note'},'当前指数缺少可核验的总市值历史。申万行业与有市值基准的个股可查看各自口径。'):null,
   !today&&!intraday&&metric!=='cap'?quoteCaption(mainQuote):null,intraday&&!today?tickNote(ticks):null,
   !today||!intraday||ticks.length?h('div',{class:'chart-footer'},h('span',{class:'legend'},h('i',{}),item.name||'上证指数'),h('span',{},today&&intraday?`虚线连接缺口两端的实采点 · 悬停查看${estimate?'估算点位':'实际报价'}`:intraday?'悬停查看时间与点位':metric==='index'?'虚线连接缺口两端的实采点 · 悬停查看日期与点位':'悬停查看日期与点位')):null),
  item.taxonomy==='SSE'?(relayOnly&&!state.demo?sectorDetailsLink(item):null):h('div',{class:'panel-tools'},isCanonicalSector(item)&&relayOnly&&!state.demo?sectorDetailsLink(item):assetResearchLink('index',item,'指数研究 ↗'),segmented([['intraday','今日走势'],['index','日线'],['cap','市值']],view.metric,v=>{view.metric=v;render();}),!intraday?segmented([[21,'1M'],[63,'3M'],[252,'1Y']],view.range,v=>{view.range=v;render();}):null),today?'chart-panel current-trend':'chart-panel');
}
function marketBreadth(d,comparison=sectorComparison(d.sectors)){
 const {ranked,label}=comparison,up=ranked.filter(({q})=>q.value>0).length,down=ranked.filter(({q})=>q.value<0).length;
 if(!ranked.length)return h('div',{class:'breadth'},h('span',{},taxonomyLabel(d),' · ',label),h('span',{},'暂无可统计涨跌幅'));
 return h('div',{class:'breadth'},h('span',{},taxonomyLabel(d),' · ',label),h('strong',{class:'up'},`${up} 涨`),
  h('strong',{class:'down'},`${down} 跌`),h('span',{},`${ranked.length-up-down} 平`));
}
function leadersPanel(d,comparison=sectorComparison(d.sectors),selectedId=state.overviewSelected,browse=()=>go('sectors')){
 const row=({sector:x,q},i)=>h('button',{class:'leader-row overview-leader',type:'button',
  id:`overview-leader-${x.id}`,'aria-controls':'overview-sector-trend','aria-pressed':String(x.id===selectedId),
  onClick:()=>chooseOverviewIndustry(x.id)},h('span',{class:'leader-rank'},i+1),h('strong',{},x.name),
  h('span',{class:`change ${sign(q.value)}`},pct(q.value)));
 const rising=comparison.ranked.filter(({q})=>q.value>0).slice(0,3);
 const falling=comparison.ranked.filter(({q})=>q.value<0).reverse().slice(0,3);
 return panel('行业焦点',comparison.label,h('div',{class:'leaders-grid'},
  h('div',{},h('h4',{class:'up'},'涨幅居前'),rising.length?rising.map(row):comparison.ranked.length?'已覆盖行业中暂无上涨':'等待同日涨跌幅'),
  h('div',{},h('h4',{class:'down'},'跌幅居前'),falling.length?falling.map(row):comparison.ranked.length?'已覆盖行业中暂无下跌':'等待同日涨跌幅')),
  button('查看全部行业 ↗',browse,'text-button'));
}
function visibleDirectory(){return relayOnly&&!state.demo&&state.page==='overview'&&state.taxonomy==='sse';}
function searchKey(value){return String(value||'').normalize('NFKC').toLowerCase().replace(/\s+/gu,'');}
function thsDirectory(today){
 const rankings=state.sectorHot,reference=state.sectorReference;
 const metadata=new Map((state.thsCatalog?.items||[]).map(item=>[item.code,item]));
 const ranked=new Map((rankings?.trade_date<=today?rankings.items:[]).map(row=>[row.code,row]));
 return [...new Set([...metadata.keys(),...ranked.keys(),...Object.keys(reference?.quotes||{}).filter(code=>thsCode.test(code))])].map(code=>{
  const meta=metadata.get(code),ranking=ranked.get(code),daily=reference?.quotes?.[code];
  const name=meta?.name||ranking?.name||daily?.name||code;
  return {id:code,name,kind:({I:'industry',N:'concept',TH:'theme'})[meta?.kind]||(ranking?'concept':undefined),
   intro:meta?.intro,taxonomy:'THS',directoryEntry:true,ranking,
   reference:daily&&dateKey(daily.quote_date)&&daily.quote_date<=today&&finite(daily.price)&&Number(daily.price)>0?daily:null,
   searchText:[code,name,ranking?.name,...(meta?.keywords||[])].map(searchKey).join('|')};
 });
}
let lastDirectory;
function exchangeDirectory(d){
 const today=chinaDay(new Date()),catalog=state.sectorCatalog,feed=state.sectorQuotes,core=state.liveQuotes;
 const ths=state.thsCatalog,reference=state.sectorReference,rankings=state.sectorHot;
 const inputs=[today,catalog,feed,core,ths,reference,rankings];
 if(lastDirectory&&inputs.every((input,i)=>input===lastDirectory.inputs[i]))
  return lastDirectory;
 const list=catalog?.items||[];
 const entries=new Map(list.map(item=>[item.code,item]));
 const quotes=new Map();
 function remember(quote){
  if(!quote||!sectorCode.test(quote.id)||!finite(quote.price)||Number(quote.price)<=0||quote.timestamp_quality!=='source_reported')return;
  const time=quoteTime(quote.quote_timestamp);
  if(time===null||time>Date.now()+60000||quote.quote_date!==chinaDay(quote.quote_timestamp))return;
  const prior=quotes.get(quote.id);
  if(!prior||time>=quoteTime(prior.quote_timestamp))quotes.set(quote.id,quote);
 }
 for(const row of feed?.sectors||[])remember(row);
 for(const row of d.sectors)remember(row);
 const byId=new Map(d.sectors.map(item=>[item.id,item]));
 // Quotes can arrive before the independently published catalogue. Their verified IDs
 // remain usable while names, aliases and classifications load.
 for(const row of feed?.sectors||[])if(!byId.has(row.id))byId.set(row.id,{id:row.id,name:row.name||row.id});
 for(const item of list)if(!byId.has(item.code))byId.set(item.code,{id:item.code});
 const sectors=[...byId.values()].map(base=>{
  const meta=entries.get(base.id),quote=quotes.get(base.id);
  const label=meta?.name||base.name||quote?.name||base.id;
  const name=meta?.fullname&&!/[\u3400-\u9fff]/u.test(label)?meta.fullname.replace(/指数$/u,''):label;
  return {id:base.id,name,fullname:meta?.fullname,intro:meta?.intro,kind:meta?.kind,taxonomy:'SSE',directoryEntry:true,live_quote:quote,
   searchText:[base.id,meta?.name,meta?.fullname,meta?.quote_name,base.name,quote?.name,...(meta?.keywords||[])].map(searchKey).join('|')};
 });
 const comparison=sectorComparison(sectors);
 // Directory pages stay in code order when quotes move. Only the hot tab is ranked.
 const thsRows=thsDirectory(today).map(sector=>({sector,q:{}}));
 const ordered=[...comparison.rows,...thsRows].sort((a,b)=>a.sector.id.localeCompare(b.sector.id));
 const ranked=thsRows.filter(({sector})=>sector.ranking).sort((a,b)=>a.sector.ranking.rank-b.sector.ranking.rank).slice(0,10);
 const active=comparison.rows.filter(({sector,q})=>q.day===today&&q.quote.trusted&&finite(q.price)&&
  finite(sector.live_quote?.amount)&&Number(sector.live_quote.amount)>0)
  .sort((a,b)=>Number(b.sector.live_quote.amount)-Number(a.sector.live_quote.amount)||a.sector.id.localeCompare(b.sector.id));
 const hot=ranked.length?ranked:active.slice(0,10);
 const note=ranked.length?`同花顺 A股概念热榜 · ${rankings.trade_date===today?'今日':'历史榜'} ${snapshotTime(rankings)} · 按中转站热度排名`:
  active.length?`热榜待更新 · 交易所成交活跃榜 · ${today} ${chinaClock.format(Math.max(...active.map(({q})=>q.quote.time)))} · 按当日累计成交额排序`:'同花顺概念热榜待更新';
 lastDirectory={inputs,sectors:ordered.map(row=>row.sector),exchange:sectors,comparison,ordered,hot,note};return lastDirectory;
}
function changeDirectoryScope(scope){
 directoryView.scope=scope;directoryView.page=0;
 if(scope==='hot')directoryView.query='';
 render();
}
function directoryToolbar(total){
 const commit=(input)=>{
  directoryView.query=input.value;directoryView.page=0;
  if(searchKey(input.value)&&directoryView.scope==='hot')directoryView.scope='all';
  renderKeepFocus('directory-search',input.selectionStart);
 };
 return h('div',{class:'directory-toolbar'},
  h('label',{class:'search directory-search'},icon('search',15),h('input',{id:'directory-search',type:'search',
   placeholder:'搜索半导体、人工智能或代码',value:directoryView.query,'aria-label':'搜索板块名称或代码',
   onCompositionStart:()=>{directoryView.composing=true;},onCompositionEnd:e=>{directoryView.composing=false;commit(e.target);},
   onInput:e=>{if(!directoryView.composing&&!e.isComposing&&e.target.value!==directoryView.query)commit(e.target);}})),
  h('div',{class:'directory-filters'},[['hot','热门'],['all',`全部 ${total}`],['industry','行业'],['concept','概念'],['theme','主题']]
   .map(([value,label])=>h('button',{type:'button',id:'directory-filter-'+value,'data-overview-focus':true,'aria-pressed':String(directoryView.scope===value),
    onClick:()=>changeDirectoryScope(value)},label))));
}
function directoryPage(board){
 const {scope,query}=directoryView;
 const search=searchKey(query);
 const hot=scope==='hot';
 const results=hot?board.hot.slice(0,10):board.ordered.filter(({sector})=>
  (scope==='all'||sector.kind===scope)&&(!search||sector.searchText.includes(search)));
 const pages=Math.max(1,Math.ceil(results.length/directoryPageSize));
 const page=Math.min(directoryView.page,pages-1);
 const rows=results.slice(page*directoryPageSize,(page+1)*directoryPageSize);
 const move=step=>{directoryView.page=page+step;render();};
 const pager=!hot&&pages>1?h('div',{class:'directory-pager'},
  h('button',{type:'button',id:'directory-prev','data-overview-focus':true,disabled:page===0,onClick:()=>move(-1)},'上一页'),
  h('span',{},`${page+1} / ${pages}`),h('button',{type:'button',id:'directory-next','data-overview-focus':true,disabled:page+1>=pages,onClick:()=>move(1)},'下一页')):null;
 const note=hot?board.note:
  `共 ${results.length} 个匹配板块${rows.length?` · 显示 ${page*directoryPageSize+1}–${page*directoryPageSize+rows.length}`:''}`;
 return {rows,results,note,pager};
}
function overviewIndustryBoard(d){
 const useDirectory=visibleDirectory();
 const board=useDirectory?exchangeDirectory(d):null;
 const swMeta=new Map((state.thsCatalog?.sw_items||[]).map(item=>[item.code,item]));
 const sectors=board?.sectors||d.sectors.map(item=>swMeta.has(item.id)?{...item,intro:swMeta.get(item.id).intro}:item);
 const comparison=board?.comparison||sectorComparison(sectors);
 const page=board?directoryPage(board):null;
 const quoted=new Set(comparison.rows.filter(({q})=>q.day===comparison.day&&finite(q.price)).map(({sector})=>sector.id));
 const selected=sectors.find(item=>item.id===state.overviewSelected)
  ||board?.hot[0]?.sector||sectors.find(item=>quoted.has(item.id))||sectors[0];
 const chartCode=useDirectory&&selected&&sectorCode.test(selected.id)&&(state.overviewSelected===selected.id||selected.live_quote)&&!d.sectors.some(item=>item.id===selected.id)?selected.id:null;
 if(chartCode!==directoryView.chartCode){
  directoryView.chartCode=chartCode;
  if(chartCode)setTimeout(()=>{if(directoryView.chartCode===chartCode&&visibleDirectory())requestSectorSeries(chartCode);},300);
 }
 const switchTaxonomy=value=>{
  if(value===state.taxonomy)return;
  directoryView.selection[state.taxonomy]=selected?.id;
  directoryView.views[state.taxonomy]=state.overviewIndustryView;
  state.taxonomy=value;
  state.overviewSelected=directoryView.selection[value]||null;directoryView.composing=false;
  state.overviewIndustryView=directoryView.views[value]||{metric:'intraday',range:21};
  render();loadVisibleDirectoryFeeds();
 };
 return h('section',{id:'overview-industries',class:'overview-section','aria-labelledby':'overview-industry-title'},
  h('header',{class:'overview-section-head'},h('h3',{id:'overview-industry-title'},'行业涨跌'),
   segmented(industryOptions(),state.taxonomy,switchTaxonomy)),
  marketBreadth(board?{...d,sectors:board.exchange}:d,comparison),
  h('div',{class:'industry-board-grid'},
   h('div',{class:'panel industry-quotes'},
    board?directoryToolbar(sectors.length):null,page?h('div',{class:'directory-note','aria-live':'polite'},page.note):null,
    (page?page.rows.length:sectors.length)?heatmap(d,page?directoryPageSize:sectors.length,page?{...comparison,rows:page.rows}:comparison,selected?.id):
     page&&sectors.length?empty(directoryView.scope==='hot'?'等待板块热榜':'暂无匹配板块',
      directoryView.scope==='hot'?'可切换到全部板块查看已有行情。':'试试其他名称、代码或分类。'):
      empty('等待行业行情','取得报价后自动更新。'),page?.pager),
   h('div',{class:'industry-detail-stack'},h('div',{id:'overview-sector-trend'},selected?
    (selected.taxonomy==='THS'?thsSectorPanel(selected):trendPanel(d,selected,state.overviewIndustryView)):panel('行业走势',null,empty('等待行业分时','取得行业数据后显示走势。'))),
    leadersPanel(d,comparison,selected?.id,board?()=>changeDirectoryScope('all'):undefined))));
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
  h('div',{class:'overview-research-grid'},trendDigest(),panel('关联资讯',null,
   validNews.length?h('div',{class:'news-list'},validNews.slice(0,3).map(n=>newsItem(n,d,true))):empty('暂无关联资讯','稍后更新'),
   button('全部资讯 ↗',()=>go('news'),'text-button'))));
}
function trendDigest(){
 const packet=state.demo?null:state.trends;
 return panel('指数趋势','近 10 个交易日',packet?.market.length?h('div',{class:'forecast-digest'},packet.market.slice(0,3).map(row=>
  h('div',{class:'leader-row'},h('strong',{},row.name),h('span',{},row.as_of||'—'),trendBadge(row.windows['10']),
   h('span',{class:`numeric ${sign(row.windows['10'].return_pct)}`},pct(row.windows['10'].return_pct))))):
  h('p',{class:'inline-note'},'等待收盘日线。'),button('打开趋势观察室 ↗',()=>go('trend'),'text-button'));
}

function trendBadge(result){
 const label=trendStates[result?.state];
 return h('span',{class:`trend-tag ${label?`trend-level-${result.state}`:'trend-pending'}`,
  title:label?null:trendReasons[result?.reason]||'等待日线'},label||'待数据');
}

function trendDate(row,expected){
 return h('span',{class:'trend-date'},row.as_of||'—',row.as_of&&expected&&row.as_of<expected?
  h('small',{class:'trend-lag'},'待更新'):null);
}

function trendCells(row){
 return trendWindows.map(n=>{
  const result=row.windows[String(n)];
  return h('td',{class:n===trendView.window?'trend-active':null},trendBadge(result),
   h('span',{class:`trend-return numeric ${sign(result.return_pct)}`},pct(result.return_pct)));
 });
}

function trendHeaders(first,scope){
 return h('thead',{},h('tr',{},h('th',{scope:'col'},first),...trendWindows.map(n=>{
  const sort=trendView.sorts[scope];
  const direction=sort?.window===n?sort.direction:0;
  return h('th',{scope:'col',class:n===trendView.window?'trend-active':null,
   'aria-sort':direction?(direction===1?'ascending':'descending'):null},
   h('button',{type:'button',class:'trend-sort','aria-label':`按 ${n} 日涨跌幅${direction===-1?'升序':'降序'}排序`,
    onClick:()=>{trendView.sorts[scope]={window:n,direction:direction===-1?1:-1};if(scope==='boards')trendView.page=0;render();}},
    `${n} 日`,h('span',{'aria-hidden':'true'},direction===1?'↑':direction===-1?'↓':'↕')));
 }),h('th',{scope:'col'},'日线截至')));
}

function sortedTrends(rows,sort,expected,observation=row=>row){
 if(!sort)return rows;
 const value=item=>{
  const row=observation(item),change=row.windows[sort.window].return_pct;
  return row.as_of===expected&&Number.isFinite(change)?change:null;
 };
 return [...rows].sort((a,b)=>{
  const left=value(a),right=value(b);
  if(left===null||right===null)return Number(right!==null)-Number(left!==null);
  return sort.direction*(left-right);
 });
}

function trendEvidence(row){
 const result=row?.windows[String(trendView.window)];
 if(!result||!Object.hasOwn(trendStates,result.state))return trendReasons[result?.reason]||'等待日线';
 return `${result.start_date} 收盘起 · 幅度 ${num(result.score,2)} · 一致性 ${accuracy(result.efficiency)} · 同向日占比 ${accuracy(result.direction_share)}`;
}

function trendLeaders(board,packet){
 const {leaders,rank_basis,rank_date,rank_coverage}=board;
 const rank=rank_basis==='index_weight'?'官方指数权重前 3':'已覆盖 A 股总市值前 3';
 return h('div',{class:'trend-leaders'},
  h('p',{class:'trend-evidence'},`板块 ${trendView.window} 日 · ${trendEvidence(board)}`),
  h('h4',{},'代表龙头'),leaders.length?h('p',{class:'trend-evidence'},`${rank} · 排序日期 ${rank_date} · 成分覆盖 ${accuracy(rank_coverage)}`,
   board.membership_date&&board.membership_date!==rank_date?` · 成分参考 ${board.membership_date}`:null):
   h('p',{class:'trend-evidence'},rank_basis==='membership_pending'?'等待完整成分名单。':`等待同日市值或指数权重覆盖 · 当前 ${accuracy(rank_coverage)}`),
  leaders.length?h('table',{class:'trend-table trend-leader-table'},trendHeaders('股票','leaders'),h('tbody',{},
   sortedTrends(leaders,trendView.sorts.leaders,packet.expected_date,row=>packet.stocks[row.id]).map(stock=>{
   const row=packet.stocks[stock.id];
   return h('tr',{},h('th',{scope:'row'},relayOnly?assetResearchLink('stock',stock,stock.name):stock.name,
    h('div',{class:'trend-evidence'},stock.id,rank_basis==='index_weight'?` · 权重 ${num(stock.rank_value)}%`:` · ${amount(stock.rank_value)}`),
    h('div',{class:'trend-evidence'},trendEvidence(row))),...trendCells(row),h('td',{},trendDate(row,packet.expected_date)));
  }))):null);
}

function leaderOverview(board,packet,key){
 if(!board.leaders.length)return '龙头待数据';
 const counts=new Map();
 for(const stock of board.leaders){
  const row=packet.stocks[stock.id];
  const label=row.as_of===packet.expected_date&&trendStates[row.windows[key].state]||'待数据';
  counts.set(label,(counts.get(label)||0)+1);
 }
 return `龙头 ${board.leaders.length} 只 · ${[...counts].map(([label,count])=>`${label} ${count}`).join(' · ')}`;
}

const briefTopics=[['market','近期市场'],['economy','中国经济'],['global','国际与中美']];
const briefRelations={possible:'可能有关',coincides:'同期观察',insufficient:'证据不足'};

function briefReferences(ids,packet){
 const items=ids.map(id=>packet.references[id]).filter(Boolean);
 return items.length?h('ul',{class:'brief-references'},items.map(row=>h('li',{},
  h('span',{},`${row.source} · ${dateText(row.published_at)} `),link(row.title,row.url)))):null;
}

function trendBrief(expected){
 if(!relayOnly||state.demo)return null;
 const packet=state.trendBrief;
 if(packet?.status!=='ready')return panel('近期市场简报','MiniMax',h('p',{class:'inline-note'},'盘后汇总行情及已发布的经济与国际资讯，首份简报生成中。'));
 const range=`${packet.period_start} 至 ${packet.trade_date} · ${packet.window} 个交易日`;
 const date=packet.trade_date<expected?`${range} · 等待最新简报`:`${range} · 更新于 ${dateText(packet.generated_at)}`;
 const intro=h('div',{class:'brief-topics'},briefTopics.map(([key,label])=>{
  const row=packet.sections[key];
  return h('article',{},h('h4',{},label),h('p',{},row?.text||'本期可复核资料不足。'),
   row?briefReferences(row.refs,packet):null);
 }));
 const cases=packet.cases.length?h('div',{class:'brief-cases'},h('h4',{},'极端走势与事件'),
  packet.cases.map(row=>h('article',{class:'brief-case'},h('div',{class:'brief-case-title'},h('strong',{},row.name),
   trendBadge(row),h('span',{class:`numeric ${sign(row.return_pct)}`},pct(row.return_pct)),pill(briefRelations[row.relation])),
   h('p',{},row.text),briefReferences(row.refs,packet)))):null;
 return panel('近期市场简报',`MiniMax · ${date}`,h('div',{class:'brief-body'},intro,cases));
}

function trendRoom(){
 const packet=state.demo?null:state.trends;
 const title=heading('趋势观察室','以已收盘日线观察指数、板块整体和板块龙头的走势。');
 if(!packet)return h('div',{},title,empty(state.demo?'演示模式':'等待趋势数据',state.demo?'切回真实数据查看已收盘走势。':'收盘日线处理完成后显示趋势。','activity'));
 const totals=Object.keys(trendStates).map(Number).sort((a,b)=>a-b);
 const key=String(trendView.window), dates=packet.expected_date;
 const groups=[['sw','申万一级'],['exchange','交易所板块'],['ths','同花顺']].filter(([id])=>packet.boards.some(row=>row.group===id));
 const group=groups.some(([id])=>id===trendView.group)?trendView.group:groups[0]?.[0];
 const boards=packet.boards.filter(row=>row.group===group);
 const current=row=>row.as_of===dates&&Object.hasOwn(trendStates,row.windows[key].state);
 const query=searchKey(trendView.query), chosen=trendView.filter;
 const filtered=boards.filter(row=>searchKey(`${row.name} ${row.id}`).includes(query)&&
  (chosen==='all'||(chosen==='pending'?!current(row):current(row)&&row.windows[key].state===Number(chosen))));
 filtered.sort((a,b)=>Number(current(b))-Number(current(a))||a.name.localeCompare(b.name,'zh-CN'));
 const matched=sortedTrends(filtered,trendView.sorts.boards,dates);
 const pageCount=Math.max(1,Math.ceil(matched.length/20));
 const page=Math.min(trendView.page,pageCount-1);
 const change=(field,value)=>{trendView[field]=value;if(field!=='page')trendView.page=0;render();};
 const indices=h('div',{class:'table-wrap'},h('table',{class:'trend-table'},trendHeaders('指数','market'),
  h('tbody',{},sortedTrends(packet.market,trendView.sorts.market,dates).map(row=>h('tr',{},h('th',{scope:'row'},relayOnly?assetResearchLink('index',row,row.name)||row.name:row.name),
   ...trendCells(row),h('td',{},trendDate(row,dates)))))));
 const table=h('div',{class:'table-wrap'},h('table',{class:'trend-table'},trendHeaders('板块整体','boards'),h('tbody',{},
  matched.slice(page*20,page*20+20).map(row=>{
   const open=trendView.expanded===row.id;
   return [h('tr',{},h('th',{scope:'row'},h('button',{type:'button',class:'sector-name trend-expand','aria-expanded':String(open),
    onClick:()=>{trendView.expanded=open?null:row.id;render();}},row.name,h('span',{'aria-hidden':'true'},open?'▾':'▸')),
    h('small',{class:'ticker'},row.id),h('small',{class:'trend-leader-preview'},leaderOverview(row,packet,key))),
    ...trendCells(row),h('td',{},trendDate(row,dates))),
    open?h('tr',{},h('td',{colspan:5,class:'trend-detail'},trendLeaders(row,packet))):null];
  }))));
 const boardPane=h('div',{},h('div',{class:'toolbar trend-toolbar'},segmented(groups,group,id=>change('group',id)),
  h('label',{class:'search'},icon('search',14),h('input',{id:'trend-search',type:'search',value:trendView.query,
   placeholder:'搜索板块名称或代码','aria-label':'搜索板块',onInput:e=>{trendView.query=e.target.value;trendView.page=0;renderKeepFocus('trend-search',e.target.selectionStart);}})),
  h('label',{class:'trend-filter'},`${key} 日状态`,h('select',{'aria-label':'筛选趋势状态',onChange:e=>change('filter',e.target.value)},
   [['all','全部'],...totals.map(level=>[String(level),trendStates[level]]),['pending','待数据或待更新']].map(([value,label])=>
    h('option',{value,selected:chosen===value},label))))),
  matched.length?table:empty('没有匹配板块','可切换板块分类或清除筛选。','search'),
  h('div',{class:'equity-pagination'},h('span',{},`${matched.length} 个板块 · 第 ${page+1} / ${pageCount} 页`),
   h('div',{},h('button',{type:'button',class:'outline-button',disabled:page===0,onClick:()=>change('page',page-1)},'上一页'),
    h('button',{type:'button',class:'outline-button',disabled:page+1>=pageCount,onClick:()=>change('page',page+1)},'下一页'))));
 return h('div',{class:'trend-room'},title,trendBrief(dates),h('div',{class:'toolbar trend-controls'},
  h('span',{},dates?`最新应有收盘 ${dates}`:'交易日历待确认'),h('div',{class:'segmented','aria-label':'观察主周期'},
   trendWindows.map(n=>h('button',{type:'button',class:n===trendView.window?'active':'','aria-pressed':String(n===trendView.window),
    onClick:()=>change('window',n)},`${n} 日${n===10?' · 主观察':''}`)))),
  panel('主要指数','点击 5、10、20 日列可按累计涨跌幅排序',indices),
  panel('板块整体与龙头',`${boards.filter(current).length} / ${boards.length} 个板块已有最新 ${key} 日判断 · 展开板块查看代表龙头`,boardPane),
  h('details',{class:'panel secondary-details trend-method','data-reader-key':'trend-method'},h('summary',{},'周期与分档规则'),
   h('div',{class:'mini-info'},h('p',{},'默认看 10 个交易日。5 日用于观察近期变化，20 日用于看更长一段走势。三列都以各标的最后一根可用日线为终点。'),
    h('p',{},'幅度分数是带方向的区间对数涨跌，除以此前 40 个交易日的典型日波动，再除以窗口天数的平方根。典型日波动取日对数涨跌绝对值的中位数除以 0.67449，至少需要 35 个有效日。'),
    h('p',{},'一致性为净变动绝对值占每日绝对变动总和的比例。幅度绝对值达到 0.75 且一致性达到 20% 时，按方向判为抖动下降或波动上升。幅度达到 1.75、一致性达到 40%，且至少 60% 的交易日同向时，判为大幅下降或快速上升。其他可计算情形归为波动。'),
    h('p',{},'板块整体使用对应的正式指数收盘。龙头在已核实完整成分名单中按官方指数权重排序，或在同日总市值覆盖至少 90% 时按已覆盖 A 股总市值排序。个股收益逐日使用除权昨收衔接。缺失日线和停牌保留为空。'),
    link('Kaufman 效率比的定义 ↗','https://ta-lib.org/functions/er.html'))));
}
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
function newsQueueNote(n){
 if(n.analysis_queue!=='tencent_news')return null;
 const labels={pending:'MiniMax 分析排队中',waiting_body:'缺少正文，等待补采',retry:'MiniMax 分析等待重试',provider_cooldown:'MiniMax 服务冷却中'};
 const label=labels[n.llm_status];
 if(!label)return null;
 const retry=newsPublishedTime(n.analysis_retry_at);
 return retry!==null&&['retry','provider_cooldown'].includes(n.llm_status)?label+' · '+dateText(retry)+' 后可重试':label;
}
function newsItem(n,d,compact=false){
 const published=newsPublishedTime(n.published_at);
 const ready=n.llm_analysis?.status==='ready'&&Boolean(n.llm_analysis.summary);
 const queueNote=ready?null:newsQueueNote(n);
 const credibility={official:['官方发布','official'],reliable:['可靠媒体','reliable'],trusted:['可靠媒体','reliable'],media:['媒体报道','reliable'],unverified:['待核实来源','rumor'],rumor:['传言 · 未核实','rumor']}[n.credibility]||['待核实','rumor'];
 const impact={positive:['潜在利好','up'],negative:['潜在利空','down'],neutral:['中性',''],uncertain:['影响待判断','']}[n.impact]||['影响待判断',''];
 const transmission=Array.isArray(n.transmission)?n.transmission:[];
 const method=ready?'MiniMax 全文分析':n.analysis_method==='keyword_rules'?'关键词线索':n.analysis_method&&n.analysis_method!=='minimax_full_text'?n.analysis_method:'尚无全文分析';
 return h('article',{class:'news-item',id:'news-'+n.id},newsSummaryCard(n,compact),
  h('div',{class:'news-meta'},pill(...credibility),h('span',{},n.source||'未知来源'),h('span',{},'·'),
   h('time',{datetime:published===null?'':n.published_at},published===null?'发布时间未确认':dateText(/^\d{4}-\d{2}-\d{2}$/.test(n.published_at)?n.published_at:published)),queueNote?pill(queueNote):null),
  h('h4',{class:'news-title'},link(n.title||'无标题',n.url)),
  !compact&&n.summary&&!ready?h('p',{class:'news-summary'},n.summary):null,ready?null:newsTone(n),
  h('div',{class:'tags'},ready?null:pill(...impact),ready?null:(n.sector_ids||[]).filter(id=>d.sectors.some(x=>x.id===id)||(state.data?.sectors||[]).some(x=>x.id===id)).slice(0,3).map(id=>pill(sectorName(id,d))),n.category?pill(n.category):null),
  ready?null:transmission.length?h('div',{class:'chain'},transmission.map((x,i)=>[i?h('b',{},'→'):null,h('span',{},x)])):h('div',{class:'chain'},n.relevance_note||'A股关联线索待核实，尚未形成明确传导链。'),
  !ready&&n.relevance_note&&transmission.length?h('p',{class:'relevance-note'},n.relevance_note):null,!compact?newsTextCard(n):null,
  !compact?h('p',{class:'inline-note'},`首次采集：${dateText(n.first_seen_at||n.fetched_at)} · ${method}`):null);
}

const newsTones=[['positive','偏正面'],['negative','偏负面'],['neutral','中性'],['uncertain','不确定']];

function relatedNewsSectors(article){
 const analysis=article.llm_analysis;
 const impacts=analysis?.status==='ready'&&Array.isArray(analysis.sector_impacts)?analysis.sector_impacts:[];
 return new Set([...(article.sector_ids||[]),...impacts.map(row=>row?.sector_code)]);
}

function datedNews(article,day,now){
 const published=newsPublishedTime(article.published_at);
 return published!==null&&published<=now.getTime()&&chinaDay(published)===day;
}

function sectorNewsSummary(d,day,now){
 const counts=()=>Object.fromEntries(newsTones.map(([key])=>[key,0]));
 const rows=new Map(d.sectors.filter(isCanonicalSector).map(sector=>[sector.id,
  {sector,count:0,tones:counts(),impact:counts()}]));
 const dated=d.news.filter(article=>datedNews(article,day,now)&&article.credibility!=='rumor'&&article.llm_analysis?.fact_status!=='rumor');
 let related=0,labeled=0;
 for(const article of dated){
  const ids=[...relatedNewsSectors(article)].filter(id=>rows.has(id));
  if(!ids.length)continue;
  related++;
  const analysis=article.llm_analysis;
  const tone=analysis?.status==='ready'?analysis.tone:null;
  if(newsTones.some(([key])=>key===tone))labeled++;
  for(const id of ids){
   const row=rows.get(id);
   row.count++;
   if(Object.hasOwn(row.tones,tone))row.tones[tone]++;
  }
  const effects=analysis?.status==='ready'&&Array.isArray(analysis.sector_impacts)?analysis.sector_impacts:[];
  const bySector=new Map();
  for(const effect of effects){
   if(!effect||!rows.has(effect.sector_code))continue;
   if(!bySector.has(effect.sector_code))bySector.set(effect.sector_code,new Set());
   const directions=bySector.get(effect.sector_code);
   if(Object.hasOwn(rows.get(effect.sector_code).impact,effect.direction))directions.add(effect.direction);
  }
  for(const [id,directions] of bySector){
   const row=rows.get(id);
   if(directions.size)row.impact[directions.size===1?[...directions][0]:'uncertain']++;
  }
 }
 return {rows:[...rows.values()],total:dated.length,related,labeled};
}

function newsBoardChange(sector,day,now){
 const own=displayQuote(sector,now);
 if(own.trusted&&own.day===day&&finite(own.change_pct))return {value:own.change_pct,label:own.daily?'正式收盘':own.label};
 const estimate=sector.intraday_estimate;
 if(estimate?.quote_date===day&&finite(estimate.change_pct)&&quoteTime(estimate.quote_timestamp)!==null&&
    quoteTime(estimate.quote_timestamp)<=now.getTime())return {value:estimate.change_pct,label:`权重股估算 · ${dateText(estimate.quote_timestamp,true)}`};
 return {value:null,label:own.day&&own.day!==day?'当日行情待更新':'暂无当日行情'};
}

function newsToneBar(row){
 if(!row.count)return h('span',{class:'panel-subtitle'},'今日暂无关联资讯');
 const missing=row.count-Object.values(row.tones).reduce((a,b)=>a+b,0);
 const tones=[...newsTones,['pending','待分析']];
 const values={...row.tones,pending:missing};
 const description=tones.filter(([key])=>values[key]).map(([key,label])=>`${label} ${values[key]}`).join(' · ');
 return h('div',{},h('div',{class:'news-tone-bar','aria-hidden':'true'},
  tones.filter(([key])=>values[key]).map(([key])=>h('span',{class:`news-tone-${key}`,
   style:`width:${100*values[key]/row.count}%`,'aria-hidden':'true'}))),h('span',{class:'news-tone-counts'},description));
}

function newsSectorPanel(d,day,now){
 const summary=sectorNewsSummary(d,day,now);
 if(!summary.rows.length)return null;
 const selected=newsOverview.sort;
 const rows=summary.rows.map(row=>({...row,change:newsBoardChange(row.sector,day,now)}));
 rows.sort((a,b)=>{
  if(selected==='gain'||selected==='loss'){
   if(a.change.value===null||b.change.value===null)return Number(b.change.value!==null)-Number(a.change.value!==null);
   if(a.change.value!==b.change.value)return (selected==='gain'?-1:1)*(a.change.value-b.change.value);
  }
  return b.count-a.count||a.sector.name.localeCompare(b.sector.name,'zh-CN');
 });
 const legends=[...newsTones,['pending','待分析']];
 return panel('板块资讯可视化',`${day} · 申万一级 ${summary.rows.length} 个板块 · ${summary.total} 条已知发布时间的资讯`,
  h('div',{},h('div',{class:'news-sector-tools'},h('div',{class:'news-sector-counts'},
    h('span',{},h('strong',{},summary.related),' 条关联板块'),h('span',{},h('strong',{},summary.labeled),' 条已判情绪'),
    h('span',{},h('strong',{},rows.filter(row=>row.count).length),' 个板块有消息')),
   h('label',{class:'trend-filter'},'排列',h('select',{'aria-label':'板块资讯排列',onChange:e=>{newsOverview.sort=e.target.value;render();}},
    [['volume','按消息量'],['gain','今日涨幅优先'],['loss','今日跌幅优先']].map(([key,label])=>h('option',{value:key,selected:key===selected},label))))),
   h('div',{class:'news-tone-legend'},legends.map(([key,label])=>h('span',{},h('i',{class:`news-tone-${key}`,'aria-hidden':'true'}),label))),
   h('div',{id:'news-sector-scroll',class:'table-wrap news-sector-scroll',tabindex:0,'aria-label':'申万板块资讯情绪与当日涨跌幅'},h('table',{class:'news-sector-table'},
    h('thead',{},h('tr',{},['板块 / 资讯数','关联资讯情绪','MiniMax 板块影响','今日涨跌幅'].map(label=>h('th',{scope:'col'},label)))),
    h('tbody',{},rows.map(row=>{
     const {sector,change,impact}=row;
     const effects=newsTones.filter(([key])=>impact[key]).map(([key,label])=>`${label} ${impact[key]}`).join(' · ');
     return h('tr',{},h('th',{scope:'row'},row.count?h('button',{class:'sector-name',type:'button',onClick:()=>{
      newsOverview.sector=sector.id;state.newsLimit=12;state.newsFilter='全部';state.newsSearch='';render();
      document.getElementById('news-sector-filter')?.scrollIntoView({block:'start'});
     }},sector.name):sector.name,h('small',{class:'panel-subtitle'},`${row.count} 条`)),
      h('td',{},newsToneBar(row)),h('td',{},effects||'—'),h('td',{},h('strong',{class:`numeric ${sign(change.value)}`},pct(change.value)),
       h('small',{class:'panel-subtitle'},change.label)));
    })))),h('p',{class:'news-sector-note'},'情绪概括关联资讯的语气，板块影响按每条消息对该行业的判断计数。同一条资讯可能关联多个板块。')),
  null,'news-sector-panel');
}

function industryRelations(sec){const hasLinks=[sec.upstream,sec.drivers,sec.downstream].some(rows=>Array.isArray(rows)&&rows.length);return panel('产业关联','仅展示已核实关系',hasLinks?h('div',{class:'mini-info'},h('div',{},'上游　',...(sec.upstream||[]).map(x=>pill(x))),h('div',{class:'divider'}),h('div',{},'核心驱动　',...(sec.drivers||[]).map(x=>pill(x))),h('div',{class:'divider'}),h('div',{},'下游　',...(sec.downstream||[]).map(x=>pill(x)))):h('div',{class:'mini-info'},'尚无已核实的产业链关系；可先查看上方关联消息与证据。'));}
function thsRadarQuote(sector,today){
 const {reference,ranking}=sector;
 const sampled=boardSample(sector.id,today);
 const hotPrice=finite(ranking?.price)&&Number(ranking.price)>0?ranking.price:null;
 const hotDay=ranking&&(hotPrice!==null||finite(ranking.change_pct))?state.sectorHot.trade_date:null;
 if(reference&&reference.quote_date>=(sampled?today:hotDay||''))
  return {day:reference.quote_date,price:reference.price,value:reference.change_pct,label:reference.quote_date+' 收盘'};
 if(sampled)return {day:today,price:null,value:sampled.change_pct,label:'成分股等权 · '+chinaClock.format(quoteTime(sampled.quote_at))};
 if(hotDay)return {day:hotDay,price:hotPrice,value:ranking.change_pct,label:'热榜 · '+snapshotTime(state.sectorHot)};
 return {day:null,price:null,value:null,label:'待更新'};
}

function sectors(d){
 const ths=usingThsRadar(),taxonomy=ths?'ths':state.taxonomy,today=chinaDay(new Date());
 const directory=ths?thsDirectory(today):d.sectors;
 const query=searchKey(state.search),favorites=state.favorites;
 const matches=directory.filter(sector=>(state.group!=='我的自选'||favorites.includes(sector.id))&&
  (!ths||radarView.kind==='all'||sector.kind===radarView.kind)&&
  (!query||(ths?sector.searchText:searchKey([sector.name,sector.code,sector.id,...(sector.keywords||[])].join(' '))).includes(query)));
 const comparison=ths?null:sectorComparison(matches);
 const rows=comparison?.rows||matches.map(sector=>({sector,q:thsRadarQuote(sector,today)}));
 if(state.sort==='name')rows.sort((a,b)=>a.sector.name.localeCompare(b.sector.name,'zh-CN'));
 else if(!ths&&state.sort==='cap')rows.sort((a,b)=>(Number(b.sector.market_cap)||0)-(Number(a.sector.market_cap)||0));
 else if(ths)rows.sort((a,b)=>String(b.q.day||'').localeCompare(String(a.q.day||''))||
  Number(finite(b.q.value))-Number(finite(a.q.value))||Number(b.q.value||0)-Number(a.q.value||0)||a.sector.id.localeCompare(b.sector.id));
 const pageSize=20,pages=Math.max(1,Math.ceil(rows.length/pageSize)),page=Math.min(radarView.page,pages-1);
 const shown=ths?rows.slice(page*pageSize,(page+1)*pageSize):rows;
 const sec=(shown.find(({sector})=>sector.id===state.selected)||shown[0])?.sector;
 state.selected=sec?.id||null;
 const switchTaxonomy=value=>{
  if(taxonomy===value)return;
  radarView.ths=value==='ths';
  if(!radarView.ths)state.taxonomy=value;
  radarView.page=0;radarView.composing=false;state.selected=null;state.group='全部';state.search='';
  state.metric=value==='sw'&&!relayOnly?'index':'intraday';
  render();loadVisibleDirectoryFeeds();
 };
 const search=input=>{state.search=input.value;radarView.page=0;renderKeepFocus('sector-search',input.selectionStart);};
 const controls=h('div',{class:'toolbar'},segmented(industryOptions(),taxonomy,switchTaxonomy),
  h('label',{class:'search'},icon('search',14),h('input',{id:'sector-search',type:'search',value:state.search,
   placeholder:'搜索名称或代码','aria-label':'搜索板块',
   onCompositionStart:()=>{radarView.composing=true;},onCompositionEnd:e=>{radarView.composing=false;search(e.target);},
   onInput:e=>{if(!radarView.composing&&!e.isComposing&&e.target.value!==state.search)search(e.target);}})),
  segmented([['全部','全部'],['我的自选','我的自选']],state.group,value=>{state.group=value;radarView.page=0;render();}));
 const headingText=ths?('同花顺 · '+directory.length+' 个板块'):taxonomyLabel(d);
 const subtitle=ths?('共 '+rows.length+' 个匹配板块 · 各行标注行情口径与时间'):
  comparison.label+' · '+(state.sort==='name'?'按名称排序':state.sort==='cap'?'按市值排序':'同日按涨跌幅排序，历史参考列后');
 const filters=ths?h('div',{class:'directory-toolbar'},segmented([['all','全部类型'],...Object.entries(thsKinds)],
  radarView.kind,kind=>{radarView.kind=kind;radarView.page=0;render();})):null;
 const select=id=>{state.selected=id;render();};
 const table=shown.length?h('div',{class:'table-wrap'},h('table',{class:'sector-quote-table'},
  h('thead',{},h('tr',{},['自选','板块','指数点位','涨跌幅','行情时间'].map(label=>h('th',{scope:'col'},label)))),
  h('tbody',{},shown.map(({sector,q})=>{
   const saved=favorites.includes(sector.id);
   const stamp=ths?q.label:q.estimate?'估算 '+dateText(sector.intraday_estimate.quote_timestamp,true):
    q.quote.as_of?dateText(q.quote.as_of,false):'待更新';
   return h('tr',{class:sec?.id===sector.id?'selected':'',onClick:()=>select(sector.id)},
    h('td',{},button(saved?'★':'☆',e=>{e.stopPropagation();state.favorites=saved?favorites.filter(x=>x!==sector.id):[...favorites,sector.id];
     local.set('guanlan.favorites',state.favorites);render();},'star '+(saved?'saved':''))),
    h('th',{scope:'row'},h('button',{class:'sector-name',type:'button',onClick:e=>{e.stopPropagation();select(sector.id);}},sector.name),
     ths?h('div',{class:'panel-subtitle'},[thsKinds[sector.kind],sector.id].filter(Boolean).join(' · ')):null),
    h('td',{class:'numeric'},num(q.price)),h('td',{class:'numeric sector-change '+sign(q.value)},pct(q.value)),
    h('td',{class:'quote-time'},stamp));
  })))):empty(ths&&!directory.length?'等待同花顺板块目录':'尚无匹配板块',
   ths&&!directory.length?'目录加载后即可按名称、代码或类型查找。':taxonomy==='eastmoney'?'网页采样尚未完成，或当前来源正在冷却。':'调整关键词或筛选后重试。');
 const move=step=>{radarView.page=page+step;state.selected=null;render();};
 const pager=ths&&pages>1?h('div',{class:'directory-pager'},
  h('button',{type:'button',disabled:page===0,onClick:()=>move(-1)},'上一页'),
  h('span',{},(page+1)+' / '+pages),h('button',{type:'button',disabled:page+1>=pages,onClick:()=>move(1)},'下一页')):null;
 const details=sec?(ths?thsSectorPanel(sec):sec.taxonomy==='SSE'?exchangeSectorPanel(sec,d):sectorDetail(sec,d)):null;
 return h('div',{},heading('板块雷达',headingText),controls,
  taxonomy==='eastmoney'?h('p',{class:'inline-note'},'独立行业分类。每轮最多两页，各行保留实际采样时间。'):null,
  h('div',{class:'sector-layout'},panel('板块行情',subtitle,h('div',{},filters,table,pager)),
   h('div',{class:'stack'},details,sec&&taxonomy==='sw'?h('details',{class:'panel secondary-details'},
    h('summary',{},'成分研究、基金与关联资讯'),fundsPanel(sec),sentimentPanel(sec,d),industryRelations(sec)):null)));
}
function renderKeepFocus(id,pos){render();const input=document.getElementById(id);if(input){input.focus({preventScroll:true});try{input.setSelectionRange(pos,pos);}catch{}}}
function stockRankLabel(sector){const basis=sector.rank_basis||sector.stocks?.[0]?.rank_basis;if(['market_cap','total_market_cap'].includes(basis))return'按已获取总市值排序';if(basis==='membership_order')return'成分示例 · 市值排序待数据';if(['turnover_amount','amount','turnover'].includes(basis))return'按已获取成交额排序';return'成分示例 · 排序依据待确认';}
function sectorDetail(sec,d){const stocks=sec.stocks||[],mainQuote=displayQuote(sec),shown=sectorQuote(sec),proxy=sec.proxy,proxyQuote=displayQuote(proxy||{}),hasProxy=!finite(mainQuote.price)&&finite(proxyQuote.price),useProxy=state.metric!=='cap'&&hasProxy;const quote=useProxy?proxy:sec;const today=relayOnly&&!state.demo,ticks=today?todayRelayTicks(quote,chinaDay(new Date())):intradayFor(d,quote,useProxy?sec:null),isIntraday=state.metric==='intraday';const hist=isIntraday?ticks:state.metric==='cap'?sec.market_cap_history:quote.history;return panel(`${sec.name}`,`${providerName(mainQuote.provider)} · ${sec.code||sec.id||'代码待确认'} · ${isCanonicalSector(sec)?'申万 2021 一级行业':sec.taxonomy==='EASTMONEY'?'东方财富行业':'行业口径待确认'}`,h('div',{},relayOnly&&!state.demo&&isCanonicalSector(sec)?sectorDetailsLink(sec):null,h('div',{class:'detail-top'},h('div',{},h('div',{class:'panel-subtitle'},shown.estimate?'板块点位 · 权重股估算':'板块指数点位'),h('strong',{class:'detail-price'},num(shown.estimate?shown.price:mainQuote.price))),h('span',{class:`change ${sign(shown.value)}`},pct(shown.value))),shown.estimate?h('p',{class:'chart-data-note'},`权重基准 ${sec.intraday_estimate.basis_date} · 样本股 ${sec.intraday_estimate.sample_count}/${sec.intraday_estimate.sample_size} · 报价时间 ${dateText(sec.intraday_estimate.quote_timestamp,false)}`):quoteCaption(mainQuote),validationStatus(sec)==='conflict'?validationDetail(sec):null,hasProxy?h('div',{class:'proxy-box'},h('div',{class:'proxy-title'},'参考ETF · ',proxy.name,` (${proxy.code})`),h('div',{class:'proxy-quote'},h('strong',{},num(proxyQuote.price,3)),h('span',{},`元 / ${proxyQuote.label}`),h('span',{class:`change ${sign(proxyQuote.change_pct)}`},pct(proxyQuote.change_pct))),h('p',{},`${providerName(proxyQuote.provider)} · ${proxyQuote.as_of?dateText(proxyQuote.as_of):'日期待确认'} · ETF仅为相关资产代理，不等同板块指数。`),validationDetail(proxy),liveQuoteBlock(proxy)):null,isCanonicalSector(sec)?h('details',{class:'secondary-details'},h('summary',{},'估值与风险指标'),quantPanel(sec),proxy?.quant_metrics?quantPanel(proxy,true):null):null,h('div',{class:'chart-body'},chart(hist,{height:165,metric:state.metric,range:isIntraday?null:state.range,unit:useProxy?'元':'点',today:today&&isIntraday,sessions:state.metric==='index'?mainlandSessions(d,quote):[],caption:h('div',{class:'panel-subtitle'},isIntraday?(today?(sec.intraday_estimate?'今日分时 · 权重股估算':'今日分时'):useProxy?'参考ETF分时快照 · 单位：元':'板块分时快照 · 单位：点'):state.metric==='cap'?(sec.market_cap_date?`历史总市值 · 最新 ${sec.market_cap_date}`:'历史总市值'):useProxy?`参考ETF趋势 · ${proxy.name} (${proxy.code}) · 单位：元`:'板块指数趋势 · 单位：点')})),isIntraday&&!today?tickNote(ticks):null,h('div',{class:'chart-footer'},segmented([['intraday','分时'],['index',hasProxy?'ETF日线':'指数日线'],['cap','板块市值']],state.metric,v=>{state.metric=v;render();}),h('span',{},isIntraday?(today?'虚线连接缺口两端的实采点':'定时采样，非秒级实时'):state.metric==='cap'?'真实快照':(useProxy?'ETF日收盘':'日频行情')+' · 虚线连接缺口两端的实采点')),h('div',{class:'panel-head'},h('h3',{class:'panel-title'},'主要成分股'),h('span',{class:'panel-subtitle'},stockRankLabel(sec))),stocks.length?h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},(today?['股票','价格','涨跌幅','总市值']:['股票','价格','涨跌幅']).map(t=>h('th',{},t)))),h('tbody',{},stocks.slice(0,8).map(x=>{const q=displayQuote(x),cap=stockCapital(x);return h('tr',{},h('td',{},assetResearchLink('stock',x,x.name||x.code),h('div',{class:'panel-subtitle'},x.code)),h('td',{class:'numeric'},num(q.price),h('div',{class:'panel-subtitle'},today?(q.daily?q.day:q.is_today&&q.time!==null?chinaClock.format(q.time):q.day||'待更新'):q.label)),h('td',{class:`numeric ${sign(q.change_pct)}`},pct(q.change_pct)),today?h('td',{class:'numeric'},amount(cap.value),cap.label?h('div',{class:'panel-subtitle'},cap.label):null,stockFlow(x)):null);})))):empty('成分股尚未接入','板块成分与ETF持仓不同，不以ETF持仓填充板块成分。')));}
function newsPage(d){const now=new Date(),day=chinaDay(now);const groups=['全部','官方信息','可靠媒体','未核实传言'];let items=d.news.filter(n=>{if(newsOverview.sector&&(!datedNews(n,day,now)||!relatedNewsSectors(n).has(newsOverview.sector)))return false;if(state.newsFilter==='官方信息'&&n.credibility!=='official')return false;if(state.newsFilter==='可靠媒体'&&!['reliable','trusted','media'].includes(n.credibility))return false;if(state.newsFilter==='未核实传言'&&n.credibility!=='rumor')return false;if(state.newsFilter==='全部'&&!state.preferences.showRumors&&n.credibility==='rumor')return false;return`${n.title} ${n.source} ${(n.sector_ids||[]).map(id=>sectorName(id,d)).join(' ')}`.toLowerCase().includes(state.newsSearch.toLowerCase());});const sources=d.sources.filter(s=>s.type!=='market');
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
 return h('div',{},heading('资讯与板块影响','先看全文提要，再查看受影响板块与原文依据。','A-SHARE INTELLIGENCE'),newsCollectionNote(),newsDigestCard(d),newsSectorPanel(d,day,now),d.previous_news_digest?newsDigestCard({...d,news_digest:d.previous_news_digest},true):null,d.news_backfill_digest?.summary&&d.news_backfill_digest.date!==d.news_digest?.date&&d.news_backfill_digest.date!==d.previous_news_digest?.date?h('details',{class:'panel secondary-details'},h('summary',{},`${d.news_backfill_digest.date} · 补全文综合解读`),newsDigestCard({...d,news_digest:d.news_backfill_digest})):null,progress?h('p',{class:'inline-note news-backfill-progress'},`全文补全 ${progress.body_complete||0}/${progress.total||103} · 发布时间已核实 ${progress.publication_verified||0} · MiniMax 已分析 ${progress.model_complete||0} · 分批更新中`):null,newsOverview.sector?h('div',{id:'news-sector-filter',class:'info-strip news-sector-filter'},'今日资讯 · '+sectorName(newsOverview.sector,d),button('显示全部板块',()=>{newsOverview.sector=null;state.newsLimit=12;render();},'text-button')):null,h('div',{class:'toolbar'},h('div',{class:'filter-group'},groups.map(g=>button(g,()=>{state.newsFilter=g;state.newsLimit=12;render();},`filter ${state.newsFilter===g?'active':''}`))),h('label',{class:'search'},icon('search',14),h('input',{id:'news-search',type:'search',placeholder:'搜索事件、来源或板块',value:state.newsSearch,'aria-label':'搜索资讯',onInput:e=>{state.newsSearch=e.target.value;state.newsLimit=12;renderKeepFocus('news-search',e.target.selectionStart);}}))),state.newsFilter==='未核实传言'?h('div',{class:'banner demo'},icon('info',15),'传言缺少独立证实。请先阅读原始来源与核实状态。'):null,h('div',{class:'news-grid'},panel('事件时间线',`${items.length} 条 · 按发布时间从新到旧`,items.length?timeline:empty('暂无匹配资讯','采集成功后会显示真实新闻；调整筛选可查看其他来源。','globe')),h('div',{class:'stack'},h('details',{class:'panel secondary-details'},h('summary',{},'查看信息源状态'),sourceList(sources)),panel('解读原则','把事实、推断与传言分开',h('div',{class:'mini-info'},h('p',{},pill('官方发布','official'),' 政策正文、监管披露、央行与政府公报。'),h('p',{},pill('媒体报道','reliable'),' 保留媒体名称与原文链接，仍需交叉核对。'),h('p',{},pill('传言','rumor'),' 单独展示，不把传播热度等同于事实。'),h('div',{class:'info-strip'},'全文提要与行业影响由 MiniMax 分别判断，并保留原文依据；经营影响不等于股价走势。'))))) );}
function connectedSources(directory, feed){
 const names={sxdata:'SXData 行情与资讯',minimax:'MiniMax 资讯分析'};
 if(directory){
  return directory.sources.filter(row=>Object.hasOwn(names,row.id)).map(row=>({
   id:row.id,name:names[row.id],type:row.id==='sxdata'?'market':'analysis',
   status:row.cooling_down?'cooldown':'ok',last_success:row.last_success_at,message:'已核实成功记录'
  }));
 }
 const observed=(feed?.market||[]).filter(row=>row.provider==='stock_api'&&quoteTime(row.quote_timestamp)!==null)
  .map(row=>row.quote_timestamp).sort().at(-1);
 return observed?[{id:'sxdata',name:names.sxdata,type:'market',status:'ok',last_success:observed}]:[];
}
function sourcePermissions(d){const states=d.stock_api_state?.endpoints||d.sources.find(s=>s.id==='stock_api')?.endpoint_status||{};const names={trade_cal:'交易日历',index_classify:'申万行业分类',classify:'申万行业分类',index_member_all:'行业成分',index_member:'行业成分',sw_daily:'申万行业日线',index_daily:'市场指数日线',daily_basic:'个股估值',rt_idx_k:'市场指数快照',rt_sw_k:'行业指数快照',news:'财经资讯'};const entries=Object.entries(states).filter(([,status])=>record(status));return panel('Tushare 服务状态','日线与估值为主；快照和资讯按实际权限分别启用',entries.length?h('div',{class:'sources-list'},entries.map(([key,status])=>{const denied=status.error_code==='api_denied',available=['success','ok','available'].includes(status.status)&&!denied;const label=denied?'当前权限不可用':available?'已取得数据':status.status==='cooldown'?'稍后重试':status.status==='error'?'暂不可用':'待检查';return h('div',{class:'source-row'},h('div',{},h('strong',{},names[key]||'行情服务'),h('p',{},key),h('p',{},status.last_success?`最近成功 ${dateText(status.last_success,false)}`:status.cooldown_until?`下次检查 ${dateText(status.cooldown_until,false)}`:'尚无成功记录')),h('span',{class:`source-state ${available?'ok':''}`},label));})):empty('服务状态待更新','以各项服务的实际成功记录为准，不默认所有接口均已开通。'));}
function sourceList(sources){return sources.length?h('div',{class:'sources-list'},sources.map(src=>h('div',{class:'source-row'},h('div',{},h('strong',{},link(src.name,src.url)),h('p',{},src.last_success?dateText(src.last_success):src.message||'待采集')),h('span',{class:`source-state ${['ok','success','healthy'].includes(src.status)?'ok':''}`},({ok:'已连接',success:'已连接',healthy:'已连接',error:'采集失败',failed:'采集失败',pending:'待连接',disabled:'未启用',cooldown:'暂缓访问',blocked:'访问受限',robots_disallowed:'站点规则禁止',stale:'缓存过期',partial:'部分可用'})[src.status]||src.status||'待连接')))):empty('尚无来源状态','数据源状态将在首次执行后更新。');}
function researchHelp(title,meaning,formula,note) {
  const dialog=h('dialog',{class:'metric-dialog','aria-labelledby':'research-help-title'},
    h('div',{class:'metric-dialog-head'},h('h3',{id:'research-help-title'},title),button('关闭',()=>dialog.close())),
    h('p',{class:'metric-meaning'},meaning),h('pre',{class:'metric-formula'},formula),h('p',{class:'metric-meaning'},note));
  dialog.addEventListener('close',()=>dialog.remove());dialog.addEventListener('click',e=>{if(e.target===dialog)dialog.close();});
  document.body.append(dialog);dialog.showModal();
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
function metric(label,value,sub){return h('div',{class:'metric'},h('p',{},label),h('strong',{},value),h('small',{},sub));}
function settings(d){return h('div',{},heading('数据与研究设置','查看来源健康状态，管理本地偏好与后端配置。','DATA & PREFERENCES'),h('div',{class:'settings-grid'},h('div',{class:'stack'},panel('浏览器偏好','以下设置仅保存在当前浏览器',h('div',{class:'form-body'},h('label',{class:'form-row'},h('div',{},h('strong',{},'自动刷新研究概览'),h('p',{},relayOnly?'按各数据源的发布周期读取行情、资讯与研究概览。':'每 60 秒重新读取已发布的数据；不会触发后台采集。')),h('input',{type:'checkbox',class:'switch',checked:state.preferences.autoRefresh,onChange:e=>setPreference('autoRefresh',e.target.checked)})),h('label',{class:'form-row'},h('div',{},h('strong',{},'在全部资讯中包含传言'),h('p',{},'仍保留未核实标记；不改变主模型的输入规则。')),h('input',{type:'checkbox',class:'switch',checked:state.preferences.showRumors,onChange:e=>setPreference('showRumors',e.target.checked)})),h('div',{class:'form-row'},h('div',{},h('strong',{},'我的自选板块'),h('p',{},`当前保存 ${state.favorites.length} 个板块，仅保存在本机。`)),button('清空自选',()=>{state.favorites=[];local.set('guanlan.favorites',[]);toast('已清空当前浏览器的自选板块');},'outline-button')))),relayOnly&&!state.demo?null:panel('后端配置','GitHub Actions 或本机执行，配置需提交仓库生效',h('div',{class:'form-body'},h('div',{class:'status-box'},'此页展示已发布的研究结果。训练任务和密钥由服务器管理。下载配置后，请合并至项目配置并重新运行数据任务。'),h('label',{class:'form-row'},h('div',{},h('strong',{},'训练模式'),h('p',{},'训练与调优在本机完成；GitHub只负责推断、核验和发布。')),h('select',{class:'select',id:'model-select','aria-label':'训练模式'},h('option',{value:'ridge'},'Ridge 基线'),h('option',{value:'ridge_mlp'},'Ridge + 小型 MLP'))),h('div',{class:'form-actions'},button('下载配置建议',()=>{const model=$('#model-select').value;download('research-settings.suggestion.json',JSON.stringify({note:'配置建议，需按项目实际字段合并；不会自动更新服务器。',model:model,exclude_unverified_rumors:true,require_point_in_time_news:true,timezone:'Asia/Shanghai',request_interval_seconds:2,max_retries:2},null,2));toast('配置建议已下载；后台设置尚未改变');},'outline-button','download')),h('p',{class:'source-detail'},'API 密钥只放在 GitHub Secrets 或本机环境变量。不要写入公开 JSON、网页或仓库历史。')))),h('div',{class:'stack'},relayOnly&&!state.demo?null:sourcePermissions(d),panel('已连接的数据源','按最近一次成功记录更新',sourceList(d.sources)),checkSummary(d),panel('当前数据快照','透明呈现覆盖范围与数据时效',h('div',{class:'form-body'},h('div',{class:'kv'},h('span',{},'最后生成'),h('span',{},dateText(d.generated_at))),h('div',{class:'kv'},h('span',{},'数据模式'),h('span',{},state.demo?'合成演示':'真实数据')),h('div',{class:'kv'},h('span',{},'板块报价 / 配置数量'),h('span',{},`${sourceCoverage(d).quoted} / ${d.sectors.length}`)),h('div',{class:'kv'},h('span',{},'参考ETF覆盖 / 配置数量'),h('span',{},`${sourceCoverage(d).proxy} / ${d.sectors.length}`)),h('div',{class:'kv'},h('span',{},'资讯条数'),h('span',{},d.news.length)),h('div',{class:'kv'},h('span',{},'新闻特征状态'),h('span',{},d.models?.news_features||'尚未接入')),h('div',{class:'divider'}),h('ul',{class:'notice-list'},(d.notices?.length?d.notices:['定时任务可能延迟，以数据时间为准。','免费来源可能限流、变更或无法返回数据。','市值快照需要逐日积累，不以指数代替。']).map(n=>h('li',{},n))),h('div',{class:'form-actions'},button('下载当前快照',()=>download(`dashboard${state.demo?'-DEMO':''}.json`,JSON.stringify(d,null,2)),'outline-button','download')))))) );}
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
const dateKey=value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value);
const validTrendRow=row=>record(row)&&(row.as_of===null||dateKey(row.as_of))&&record(row.windows)&&trendWindows.every(n=>{
 const r=row.windows[n];
 return record(r)&&(r.state===undefined?Object.hasOwn(trendReasons,r.reason):
  Number.isInteger(r.state)&&Object.hasOwn(trendStates,r.state)&&dateKey(r.start_date)&&
  [r.return_pct,r.score,r.efficiency,r.direction_share].every(Number.isFinite));
});
const trendFeed={file:'trends',key:'trends',label:'趋势观察',interval:300000,pages:['overview','trend'],source:'trend_observations',
 valid:p=>(p.expected_date===null||dateKey(p.expected_date))&&p.method==='closing_trend_v1'&&
  records(p.market)&&records(p.boards)&&record(p.stocks)&&[...p.market,...p.boards].every(row=>
   typeof row.id==='string'&&typeof row.name==='string'&&validTrendRow(row))&&
  p.boards.every(row=>records(row.leaders)&&row.leaders.every(leader=>typeof leader.id==='string'&&
   typeof leader.name==='string'&&Number.isFinite(leader.rank_value)&&validTrendRow(p.stocks[leader.id])))};
const briefFeed={file:'trend-brief',key:'trendBrief',label:'MiniMax 简报',interval:300000,pages:['trend'],source:'minimax_trend_brief',
 valid:p=>{
  if(!dateKey(p.trade_date))return false;
  if(p.status==='pending')return true;
  const block=r=>record(r)&&typeof r.text==='string'&&Array.isArray(r.refs)&&
   r.refs.every(id=>id==='M'||Object.hasOwn(p.references,id));
  return p.status==='ready'&&dateKey(p.period_start)&&p.window===10&&record(p.sections)&&record(p.references)&&
   Object.values(p.references).every(r=>record(r)&&['source','title','published_at'].every(k=>typeof r[k]==='string'))&&
   briefTopics.every(([key])=>p.sections[key]===null||block(p.sections[key]))&&records(p.cases)&&p.cases.every(r=>
    block(r)&&typeof r.name==='string'&&[-2,2].includes(r.state)&&Number.isFinite(r.return_pct)&&Object.hasOwn(briefRelations,r.relation));
 }};
const validThsItem=x=>thsCode.test(x.code)&&typeof x.name==='string'&&['I','N','TH'].includes(x.kind)&&
 (x.keywords==null||Array.isArray(x.keywords)&&x.keywords.every(k=>typeof k==='string'));
const feeds=[
 {file:'connected-sources',key:'connectedSources',label:'已连接的数据源',interval:60000,pages:['settings','overview','news'],
  valid:p=>records(p.sources)&&p.sources.every(s=>['sxdata','minimax'].includes(s.id)&&(s.last_success_at==null||quoteTime(s.last_success_at)!==null)&&(s.cooling_down==null||typeof s.cooling_down==='boolean'))},

 {file:'ths-sector-catalog',key:'thsCatalog',label:'同花顺及申万目录',interval:30000,maxAge:3600000,pages:['overview','sectors'],directory:'taxonomy',
  valid:p=>records(p.items)&&p.items.length>0&&p.items.every(validThsItem)&&
   (p.sw_items==null||records(p.sw_items)&&p.sw_items.every(x=>/^\d{6}\.SI$/.test(x.code)&&x.kind==='L1'&&typeof x.name==='string'))},
 {file:'board-live-summary',key:'boardLiveSummary',label:'板块分时采样',interval:15000,pages:['overview','sectors'],directory:'ths',source:'sxdata_board_summary',
  valid:p=>dateKey(p.trade_date)&&record(p.items)},
 {file:'sector-reference',key:'sectorReference',label:'同花顺收盘行情',interval:60000,maxAge:300000,pages:['overview','sectors'],directory:'ths',source:'sxdata_sector_reference',
  valid:p=>dateKey(p.trade_date)&&record(p.quotes)&&Object.values(p.quotes).every(q=>q==null||record(q))},
 {file:'sector-hot',key:'sectorHot',label:'同花顺热榜',interval:60000,pages:['overview','sectors'],directory:'ths',source:'sxdata_ths_hot',
  valid:p=>dateKey(p.trade_date)&&p.trade_date<=chinaDay(new Date())&&records(p.items)&&p.items.length>0&&
   p.items.every(x=>thsCode.test(x.code)&&typeof x.name==='string'&&Number.isInteger(x.rank)&&x.rank>0)&&
   new Set(p.items.map(x=>x.code)).size===p.items.length&&new Set(p.items.map(x=>x.rank)).size===p.items.length},
 {file:'sector-catalog',key:'sectorCatalog',label:'交易所板块目录',interval:30000,maxAge:3600000,pages:['overview'],directory:true,
  valid:p=>/^\d{4}-\d{2}-\d{2}$/.test(p.as_of)&&records(p.items)&&p.items.length>0&&
   p.items.every(x=>sectorCode.test(x.code)&&typeof x.name==='string'&&typeof x.fullname==='string'&&
    ['industry','theme'].includes(x.kind)&&(x.keywords==null||Array.isArray(x.keywords)&&x.keywords.every(k=>typeof k==='string')))&&
   new Set(p.items.map(x=>x.code)).size===p.items.length},
 {file:'sector-quotes',key:'sectorQuotes',label:'板块行情',interval:15000,pages:['overview'],directory:true,source:'sxdata_relay',
  valid:p=>/^\d{4}-\d{2}-\d{2}$/.test(p.trade_date)&&quoteTime(p.generated_at)!==null&&records(p.sectors)&&p.sectors.every(x=>sectorCode.test(x.id))},
 {file:'live-quotes',key:'liveQuotes',label:'行情',interval:5000,pages:[...marketPages,'news'],source:'sxdata_relay',
  valid:p=>records(p.market)&&records(p.sectors)&&Boolean(quoteSnapshotStamp(p))},
 {file:'today-intraday',key:'relayIntraday',label:'分时',interval:60000,pages:marketPages,source:'sxdata_relay',
  valid:p=>record(p.series)&&Object.values(p.series).every(Array.isArray)},
 {file:'minute-recovery',key:'minuteRecovery',label:'分钟补采',interval:60000,pages:marketPages,source:'sxdata_minute_recovery',
  valid:p=>/^\d{4}-\d{2}-\d{2}$/.test(p.trade_date)&&quoteTime(p.generated_at)!==null&&record(p.series)&&Object.values(p.series).every(Array.isArray)},
 {file:'stock-quotes',key:'relayStocks',label:'个股',interval:30000,pages:[...marketPages,'news'],source:'sxdata_relay',
  valid:p=>records(p.stocks)&&(p.board_estimates==null||records(p.board_estimates))},
 {file:'cloud-news',key:'cloudNews',label:'资讯',interval:30000,pages:['news',...marketPages],source:'tencent_news',
  valid:p=>/^\d{4}-\d{2}-\d{2}$/.test(p.trade_date)&&validArticles(p.articles)&&
    (p.recent_articles==null||validArticles(p.recent_articles))&&validDigest(p.news_digest)&&
    (p.previous_digest==null||validDigest(p.previous_digest))},
 {file:'daily-market',key:'cloudDaily',label:'日线',interval:300000,pages:[...marketPages,'news'],source:'sxdata_daily',
  valid:p=>record(p.bars)&&record(p.stocks)&&Object.values(p.bars).every(bucket=>record(bucket)&&Object.values(bucket).every(bar=>record(bar)&&typeof bar.date==='string'))},
 {file:'cloud-research',key:'cloudResearch',label:'盘后研究',interval:300000,pages:marketPages,source:'tencent_research',
  valid:p=>record(p.entities)&&Object.values(p.entities).every(record)&&validReviewRows(p.daily_review)&&
    ['data_checks','ledger'].every(key=>p[key]==null||records(p[key]))},
];
const dashboardFeed={key:'data',label:'研究概览'};
const pageQuoteFeed={key:'pageQuotes',file:'page-quotes',label:'网页行情',pages:marketPages,
 valid:p=>records(p.market)&&records(p.sectors)&&record(p.intraday)};
function visiblePublication(spec){
 if(spec.pages&&!spec.pages.includes(state.page))return false;
 return !spec.directory||visibleDirectory()||
  (spec.directory==='taxonomy'&&relayOnly&&!state.demo&&state.page==='overview')||
  (['ths','taxonomy'].includes(spec.directory)&&usingThsRadar());
}
function loadVisibleDirectoryFeeds(){
 for(const spec of feeds)if(spec.directory&&visiblePublication(spec)&&!state[spec.key])refreshFeed(spec);
 if(!state.demo&&visiblePublication(trendFeed)&&!state.trends)refreshFeed(trendFeed);
 if(relayOnly&&!state.demo&&visiblePublication(briefFeed)&&!state.trendBrief)refreshFeed(briefFeed);
}
function publicationNotice(){
 const issues=[...publications.values()].filter(item=>item.failures&&visiblePublication(item.spec));
 const sections=state.data?.unavailable_sections;
 if(!issues.length&&!Array.isArray(sections))return null;
 return h('div',{class:'banner',role:'status'},icon('info',15),h('span',{},Array.isArray(sections)?`等待新快照修复以下字段：${sections.join('、')}。`:'',issues.map(({spec})=>
  `${spec.label}暂时读取失败，${state[spec.key]?'保留上次数据':'等待首次成功读取'}`).join('。')));
}
function renderAfterRefresh(){
 const focus=document.activeElement;
 if(directoryView.composing&&focus?.id==='directory-search'||radarView.composing&&focus?.id==='sector-search')return;
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
let activeSeriesRequest;
async function requestSectorSeries(id){
 if(!visibleDirectory()||id!==directoryView.chartCode||!sectorCode.test(id))return;
 const day=chinaDay(new Date());
 let request=activeSeriesRequest;
 if(!request||request.id!==id||request.day!==day)request=activeSeriesRequest={id,day};
 if(request.pending||Date.now()<(request.next||0))return;
 request.pending=true;
 try{
  const response=await fetchPublished(`./data/sector-intraday/${id}.json`,{cache:'no-cache'});
  if(!response.ok)throw responseError(response);
  const payload=await response.json();
  if(payload.schema_version!==1||payload.source!=='sxdata_relay'||payload.id!==id||payload.trade_date!==day||!Array.isArray(payload.points))
   throw Error('invalid intraday series');
  if(activeSeriesRequest!==request||directoryView.chartCode!==id||!visibleDirectory())return;
  state.sectorSeries=payload;
  request.next=Date.now()+55000;
  renderAfterRefresh();
 }catch(error){request.next=Math.max(Date.now()+30000,error.retryAt||0);}
 finally{request.pending=false;}
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
  if(spec===trendFeed&&previous?.generated_at>next.generated_at)return null;
  if(['minuteRecovery','sectorQuotes','sectorReference'].includes(spec.key)&&previous?.trade_date===next.trade_date&&quoteTime(next.generated_at)<quoteTime(previous.generated_at))return null;
  if(['sectorCatalog','thsCatalog'].includes(spec.key)&&previous?.as_of>next.as_of)return null;
  if(spec.key==='sectorHot'&&previous?.trade_date===next.trade_date&&snapshotTime(next)<snapshotTime(previous))return null;
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
 if(relayOnly&&includeFeeds)jobs.push(...feeds.filter(spec=>!spec.directory||visiblePublication(spec)).map(refreshFeed));
 if(!relayOnly)jobs.push(refreshFeed(pageQuoteFeed));
 if(includeFeeds&&!state.demo&&visiblePublication(trendFeed))jobs.push(refreshFeed(trendFeed));
 if(includeFeeds&&relayOnly&&!state.demo&&visiblePublication(briefFeed))jobs.push(refreshFeed(briefFeed));
 return Promise.all(jobs);
}
function pageFromHash(){const hash=location.hash.slice(1);return ({forecast:'trend',review:'news'})[hash]||hash;}
const hash=pageFromHash();if(pages.some(x=>x[0]===hash))state.page=hash;
window.addEventListener('hashchange',()=>{const next=pageFromHash();if(pages.some(x=>x[0]===next)){state.page=next;render();loadVisibleDirectoryFeeds();}});
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
 if(feed.directory&&!visiblePublication(feed))return;
 const task=publications.get(feed.key);
 if(feed.maxAge&&state[feed.key]&&!task?.failures&&now-(task?.lastStartedAt??-Infinity)<feed.maxAge)return;
 if(feed.source==='sxdata_relay'&&quietQuoteSession(state.liveQuotes,now)&&
    now-(publications.get(feed.key)?.lastStartedAt??-Infinity)<300000)return;
 return refreshFeed(feed);
}
function poll(action){if(state.preferences.autoRefresh&&!state.demo&&!document.hidden)action();}
setInterval(()=>poll(()=>refresh(false)),relayOnly?300000:60000);
setInterval(()=>poll(()=>{if(visiblePublication(trendFeed))pollFeed(trendFeed);}),trendFeed.interval);
if(relayOnly)setInterval(()=>poll(()=>{if(visiblePublication(briefFeed))pollFeed(briefFeed);}),briefFeed.interval);
if(relayOnly)for(const feed of feeds)setInterval(()=>poll(()=>pollFeed(feed)),feed.interval);
if(relayOnly)setInterval(()=>poll(()=>requestSectorSeries(directoryView.chartCode)),60000);
refresh();

fetch('./data/analytics.json',{cache:'no-store'}).then(r=>r.ok?r.json():{}).then(cfg=>{state.analytics=cfg; if(state.page==='analytics')render();}).catch(()=>{});
