'use strict';

const RadarResearch = (() => {
  const labels = {stock:'股票', index:'大盘指数', sector:'板块'};
  const groups = {intraday:'盘中指标', daily:'日线指标', valuation:'收盘估值与交易指标'};
  const patterns = {stock:/^(?:\d{6}\.(?:SH|SZ|BJ|NQ)|\d{5,6}\.HK)$/, index:/^\d{6}\.(SH|SZ)$/,
    sector:/^\d{6}(?:\.(SH|SZ|SI)|R?\.TI)$/};
  const number = new Intl.NumberFormat('zh-CN', {maximumFractionDigits:4});
  const price = new Intl.NumberFormat('zh-CN', {minimumFractionDigits:2,maximumFractionDigits:3});
  const localTime = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit',
    day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  function valid(type, code) {
    return Object.hasOwn(patterns,type) && typeof code==='string' && patterns[type].test(code);
  }
  function stamp(value) {
    if (typeof value!=='string') return '';
    if (/^\d{4}-\d\d-\d\d$/.test(value)) return value;
    if (!/^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(value)) return '';
    const dt = new Date(value);
    return Number.isFinite(dt.valueOf()) ? localTime.format(dt).replaceAll('/','-') : '';
  }
  function payload(data,type,code) {
    if (data?.schema_version!==1 || data.source!=='sxdata_asset_research' || data.type!==type ||
        data.code!==code || !Array.isArray(data.metrics)) return false;
    const keys=new Set();
    return data.metrics.every(row=>{
      if (!row || typeof row.key!=='string' || !row.key || keys.has(row.key) || typeof row.label!=='string' ||
          !row.label || !Object.hasOwn(groups,row.group) || (row.value!==null && !Number.isFinite(row.value))) return false;
      keys.add(row.key);
      return true;
    });
  }
  function node(tag,cls,value) {
    const el=document.createElement(tag);
    if (cls) el.className=cls;
    if (value!==undefined && value!==null) el.textContent=value;
    return el;
  }
  const summaryValue=new Intl.NumberFormat('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
  const preciseValue=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:8});
  const directional=new Set(['opening_gap_pct','relative_hs300_pp','ma20_bias_pct','momentum20_pct']);
  function displayTime(value,today) {
    const full=stamp(value);
    return full.startsWith(today+' ')?full.slice(11):full;
  }
  function detailsLine(list,label,value) {
    if (value===null || value===undefined || value==='') return;
    list.append(node('dt','',label),node('dd','',value));
  }
  function shortReason(reason) {
    if (!reason) return '等待所需数据';
    if (/亏损/.test(reason)) return '数据源未发布该估值';
    if (reason.length<=20) return reason;
    if (/不同步|时点|时间差/.test(reason)) return '行情与基准时点未对齐';
    if (/不足|不够/.test(reason)) return /覆盖/.test(reason)?'成分覆盖不足':'有效样本不足';
    if (/昨收/.test(reason)) return '行情或昨收不全';
    return '计算所需数据不全';
  }
  function metricCard(row,expanded,today) {
    const card=node('article','research-metric');
    const hasValue=Number.isFinite(row.value), sign=directional.has(row.key) && hasValue?Math.sign(row.value):0;
    const value=node('div','metric-value'+(sign>0?' up':sign<0?' down':''),
      hasValue?(sign>0?'+':'')+summaryValue.format(row.value):'—');
    if (hasValue && row.unit) value.append(node('small','',row.unit));
    card.append(node('h4','',row.label),value);
    const intra=row.group==='intraday',at=stamp(intra && row.quote_timestamp || row.as_of);
    const day=at.slice(0,10);
    const shortDay=day.slice(0,4)===today.slice(0,4)?day.slice(5):day;
    const period=/^(\d+)(?:个收盘|个日收益|对日收益|个交易日)$/.exec(row.window||'');
    const window=period?period[1]+'个交易日':row.window;
    const context=intra?'盘中 · '+(displayTime(row.quote_timestamp||row.as_of,today)||'时点待核验'):
      [shortDay?'截至 '+shortDay:'日期待核验',window].filter(Boolean).join(' · ');
    card.append(node('p','metric-context',context));
    const notes=[];
    if (/估算/.test(row.basis||'')) notes.push('成分股估算');
    else if (/热榜/.test(row.basis||'')) notes.push('热榜行情');
    if (Number.isFinite(row.coverage_pct) && row.coverage_pct<100) notes.push('成分覆盖 '+number.format(row.coverage_pct)+'%');
    if (intra && row.benchmark_timestamp && row.quote_timestamp) {
      const delta=Math.abs(Date.parse(row.benchmark_timestamp)-Date.parse(row.quote_timestamp))/1000;
      if (Number.isFinite(delta) && delta>0) notes.push(delta<60?'基准相差 '+Math.round(delta)+' 秒':'基准时点不同');
    }
    if (!hasValue) notes.push(shortReason(row.missing_reason));
    if (notes.length) card.append(node('p','metric-note',notes.join(' · ')));
    const help=node('details');help.dataset.key=row.key;help.open=expanded;
    help.append(node('summary','','计算说明'));
    const list=node('dl','metric-info');
    if (hasValue) detailsLine(list,'原值',preciseValue.format(row.value)+(row.unit?' '+row.unit:''));
    detailsLine(list,'计算公式',row.formula);detailsLine(list,'释义',row.description);
    detailsLine(list,'数据口径',row.basis);detailsLine(list,'样本窗口',row.window);
    if (Number.isInteger(row.sample_count)) {
      const required=Number.isInteger(row.required_samples)?'，至少需要 '+row.required_samples:'';
      detailsLine(list,'有效样本',String(row.sample_count)+required);
    }
    if (Number.isInteger(row.total_count)) detailsLine(list,'成分总数',String(row.total_count));
    if (Number.isFinite(row.coverage_pct)) detailsLine(list,'成分覆盖率',preciseValue.format(row.coverage_pct)+'%');
    detailsLine(list,'数据时点',at);detailsLine(list,'基准时点',stamp(row.benchmark_timestamp));
    detailsLine(list,'成分日期',stamp(row.membership_as_of));detailsLine(list,'成分核验',stamp(row.checked_at));
    if (!hasValue) detailsLine(list,'缺数原因',row.missing_reason);
    help.append(list);card.append(help);
    return card;
  }
  function render(data) {
    const panel=document.getElementById('research-groups');
    const open=new Set(Array.from(panel.querySelectorAll('details[open]'),el=>el.dataset.key));
    const fragment=document.createDocumentFragment();
    for (const [key,label] of Object.entries(groups)) {
      const rows=data.metrics.filter(row=>row.group===key);
      if (!rows.length) continue;
      const section=node('section'), grid=node('div','research-grid');
      section.append(node('h3','',label),grid);
      const today=localTime.format(new Date()).replaceAll('/','-').slice(0,10);
      rows.forEach(row=>grid.append(metricCard(row,open.has(row.key),today)));
      fragment.append(section);
    }
    panel.replaceChildren(fragment);
  }
  function researchSummary(data) {
    const generated=stamp(data.generated_at);
    const today=localTime.format(new Date()).replaceAll('/','-').slice(0,10);
    const line=generated?'更新于 '+displayTime(data.generated_at,today):'';
    const dateNode=document.getElementById('research-date');
    if (dateNode.textContent!==line) dateNode.textContent=line;
    const available=data.metrics.filter(row=>row.value!==null).length;
    const message=data.metrics.length?available+' / '+data.metrics.length+' 项可用':'当前标的暂无已发布的研究指标。';
    const status=document.getElementById('research-status');
    if (status.textContent!==message) status.textContent=message;
  }
  function assetHeader(data) {
    const get=id=>document.getElementById(id);
    if (typeof data.name==='string' && data.name.trim()) {
      get('asset-title').textContent=data.name;
      document.title=data.name+' · '+labels[data.type]+'研究 · 观澜';
    }
    const captions={live:'盘中',close:'收盘',estimate:'估算',hot:'概念热榜'};
    const today=localTime.format(new Date()).replaceAll('/','-').slice(0,10);
    function caption(at,basis) {
      return [captions[basis],displayTime(at,today)].filter(Boolean).join(' · ')||'行情时间待核验';
    }
    get('asset-quote').hidden=false;
    get('asset-price-label').textContent=data.type==='index'?'指数点位':data.code.endsWith('.HK')?'价格（港元）':'价格（元）';
    const hasPrice=Number.isFinite(data.price) && data.price>0, hasChange=Number.isFinite(data.change_pct);
    get('asset-price').textContent=hasPrice?price.format(data.price):'—';
    get('asset-change').textContent=hasChange?(data.change_pct>0?'+':'')+summaryValue.format(data.change_pct)+'%':'—';
    get('asset-change').className=hasChange?(data.change_pct>0?'up':data.change_pct<0?'down':''):'';
    get('asset-price-time').textContent=hasPrice?caption(data.quote_at,data.quote_basis):'点位或价格待更新';
    const changeAt=Object.hasOwn(data,'change_at')?data.change_at:data.quote_at;
    const changeBasis=data.change_basis || (changeAt===data.quote_at?data.quote_basis:
      /^\d{4}-\d\d-\d\d$/.test(changeAt)?'close':'live');
    get('asset-change-time').textContent=hasChange?caption(changeAt,changeBasis):'涨跌幅待更新';
    const shared=get('asset-common-time');
    const same=hasPrice && hasChange && data.quote_at && changeAt===data.quote_at && data.quote_basis===changeBasis;
    shared.hidden=!same;shared.textContent=same?caption(data.quote_at,data.quote_basis):'';
    get('asset-price-time').hidden=Boolean(same);get('asset-change-time').hidden=Boolean(same);
  }
  function mount(type,code,standalone=false,onData) {
    const status=document.getElementById('research-status');
    if (!valid(type,code)) {status.textContent='代码或市场类型无效，请从行情目录选择标的。';return;}
    if (standalone) {
      document.getElementById('asset-meta').textContent=labels[type]+' · '+code;
      document.getElementById('asset-title').textContent=code;
    }
    let last=null, lastMetrics='', lastQuote='', timer=null, active=null, restart=false, failures=0;
    const host=document.getElementById('asset-chart');
    let chartKey;
    function renderChart(data) {
      if (!host || typeof window==='undefined' || typeof window.AssetChart?.mount!=='function') return;
      const members=data?.constituent_live;
      const signature=JSON.stringify([data?.name,data?.chart,members?.trade_date,members?.points]);
      if (signature===chartKey) return;
      try {
        host.hidden=false;
        window.AssetChart.mount(host,data?.chart,{key:type+'/'+code,name:data?.name||code,members});
        chartKey=signature;
      } catch {host.textContent='行情图暂时无法绘制。';}
    }
    renderChart(null);
    const url='../data/asset-research/'+type+'/'+encodeURIComponent(code)+'.json';
    function schedule(wait) {
      clearTimeout(timer);
      if (!document.hidden) timer=setTimeout(refresh,wait);
    }
    async function refresh() {
      if (document.hidden || active) return;
      const abort=new AbortController();
      active=abort;
      const timeout=setTimeout(()=>abort.abort(),10000);
      try {
        const response=await fetch(url,{cache:'no-cache',signal:abort.signal});
        if (!response.ok) throw new Error(String(response.status));
        const data=await response.json();
        if (!payload(data,type,code)) throw new Error('schema');
        if (!document.hidden && (!last || !(stamp(last.generated_at)>stamp(data.generated_at)))) {
          const today=localTime.format(new Date()).replaceAll('/','-').slice(0,10);
          const values=JSON.stringify([today,data.metrics]);
          if (values!==lastMetrics) {render(data);lastMetrics=values;}
          researchSummary(data);
          renderChart(data);
          if (onData) onData(data,false);
          if (standalone) {
            const quote=JSON.stringify([today,data.name,data.price,data.change_pct,data.quote_basis,data.change_basis,data.quote_at,data.change_at]);
            if (quote!==lastQuote) {assetHeader(data);lastQuote=quote;}
          }
          last=data;
        } else if (!document.hidden && last) researchSummary(last);
        failures=0;
      } catch (error) {
        if (!document.hidden && !restart) {
          failures=Math.min(failures+1,5);
          const message=last?'研究数据暂时无法更新，当前显示最近成功读取的数据。':
            error.message==='404'?'当前标的暂无已发布的研究数据，稍后自动再查。':'研究数据暂时无法读取，稍后自动重试。';
          if (status.textContent!==message) status.textContent=message;
          if (onData) onData(last,true);
        }
      } finally {
        clearTimeout(timeout);
        active=null;
        const base=refreshInterval(new Date(),last);
        schedule(restart?0:Math.min(120000,base*2**failures));
        restart=false;
      }
    }
    document.addEventListener('visibilitychange',()=>{
      clearTimeout(timer);
      if (document.hidden) {if (active) active.abort();return;}
      if (active) restart=true;
      else void refresh();
    });
    void refresh();
  }
  function refreshInterval(now,data) {
    const local=localTime.format(now).replaceAll('/','-'), day=local.slice(0,10);
    const weekday=new Date(day+'T00:00:00Z').getUTCDay(), hm=local.slice(11,16);
    const trading=data?.market_session_day===day && typeof data.is_trading_day==='boolean'?
      data.is_trading_day:weekday>0 && weekday<6;
    return trading && ((hm>='09:30' && hm<='11:30') || (hm>='13:00' && hm<='15:00'))?5000:60000;
  }
  return {valid,stamp,payload,metricCard,mount,refreshInterval};
})();

if (typeof document!=='undefined' && document.getElementById('asset-detail')) {
  const query=new URLSearchParams(location.search);
  const type=query.get('type')||'';
  if (type==='stock' || type==='index') RadarResearch.mount(type,(query.get('code')||'').toUpperCase(),true);
  else document.getElementById('research-status').textContent='标的类型无效，请从行情目录选择股票或指数。';
}
