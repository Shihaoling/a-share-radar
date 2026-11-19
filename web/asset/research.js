'use strict';

const RadarResearch = (() => {
  const labels = {stock:'股票', index:'大盘指数', sector:'板块'};
  const groups = {intraday:'盘中指标', daily:'日线指标'};
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
  function metricCard(row, expanded) {
    const card=node('article','research-metric');
    const value=node('div','metric-value',Number.isFinite(row.value)?number.format(row.value):'—');
    if (Number.isFinite(row.value) && row.unit) value.append(node('small','',row.unit));
    card.append(node('h4','',row.label),value);
    if (row.value===null) card.append(node('p','metric-reason',row.missing_reason||'来源尚未提供计算所需数据。'));
    const hasSamples=Number.isInteger(row.sample_count) && row.sample_count>=0;
    const samples=hasSamples ? (Number.isInteger(row.total_count)?'成分覆盖 '+row.sample_count+' / '+row.total_count:
      '有效样本 '+row.sample_count):'';
    const asOf=(row.group==='intraday' && stamp(row.quote_timestamp)) || stamp(row.as_of);
    const context=[row.window,samples].filter(Boolean).join(' · ');
    if (context) card.append(node('p','metric-context',context));
    card.append(node('p','metric-context',asOf?'数据时点 '+asOf:'数据时点待核验'));
    if (row.basis) card.append(node('p','metric-context',row.basis));
    if (Number.isFinite(row.coverage_pct)) card.append(node('p','metric-context','覆盖率 '+number.format(row.coverage_pct)+'%'));
    if (stamp(row.membership_as_of)) card.append(node('p','metric-context','成分日期 '+stamp(row.membership_as_of)));
    else if (stamp(row.checked_at)) card.append(node('p','metric-context','成分核验 '+stamp(row.checked_at)));
    const benchmark=stamp(row.benchmark_timestamp);
    if (benchmark) card.append(node('p','metric-context','基准时点 '+benchmark));
    if (row.formula || row.description) {
      const help=node('details'); help.dataset.key=row.key; help.open=expanded;
      help.append(node('summary','','公式与释义'));
      if (row.formula) help.append(node('p','metric-help-body metric-formula',row.formula));
      if (row.description) help.append(node('p','metric-help-body',row.description));
      card.append(help);
    }
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
      rows.forEach(row=>grid.append(metricCard(row, open.has(row.key))));
      fragment.append(section);
    }
    panel.replaceChildren(fragment);
  }
  function researchSummary(data) {
    const generated=stamp(data.generated_at);
    const line=generated?'最近计算 '+generated:'';
    const dateNode=document.getElementById('research-date');
    if (dateNode.textContent!==line) dateNode.textContent=line;
    const available=data.metrics.filter(row=>row.value!==null).length;
    const message=data.metrics.length?'已取得 '+available+' / '+data.metrics.length+' 项指标':'当前标的暂无已发布的研究指标。';
    const status=document.getElementById('research-status');
    if (status.textContent!==message) status.textContent=message;
  }
  function assetHeader(data) {
    const get=id=>document.getElementById(id);
    if (typeof data.name==='string' && data.name.trim()) {
      get('asset-title').textContent=data.name;
      document.title=data.name+' · '+labels[data.type]+'研究 · 观澜';
    }
    const captions={live:'行情',close:'收盘',estimate:'估算',hot:'概念热榜'};
    function caption(at,basis) {
      return [captions[basis],stamp(at)].filter(Boolean).join(' · ')||'行情时间待核验';
    }
    get('asset-quote').hidden=false;
    get('asset-price-label').textContent=data.type==='index'?'指数点位':data.code.endsWith('.HK')?'价格（港元）':'价格（元）';
    const hasPrice=Number.isFinite(data.price) && data.price>0, hasChange=Number.isFinite(data.change_pct);
    get('asset-price').textContent=hasPrice?price.format(data.price):'—';
    get('asset-change').textContent=hasChange?(data.change_pct>0?'+':'')+number.format(data.change_pct)+'%':'—';
    get('asset-change').className=hasChange?(data.change_pct>0?'up':data.change_pct<0?'down':''):'';
    get('asset-price-time').textContent=hasPrice?caption(data.quote_at,data.quote_basis):'点位或价格待更新';
    const changeAt=Object.hasOwn(data,'change_at')?data.change_at:data.quote_at;
    const changeBasis=data.change_basis || (changeAt===data.quote_at?data.quote_basis:
      /^\d{4}-\d\d-\d\d$/.test(changeAt)?'close':'live');
    get('asset-change-time').textContent=hasChange?caption(changeAt,changeBasis):'涨跌幅待更新';
  }
  function mount(type,code,standalone=false) {
    const status=document.getElementById('research-status');
    if (!valid(type,code)) {status.textContent='代码或市场类型无效，请从行情目录选择标的。';return;}
    if (standalone) {
      document.getElementById('asset-meta').textContent=labels[type]+' · '+code;
      document.getElementById('asset-title').textContent=code;
    }
    let last=null, lastMetrics='', lastQuote='', timer=null, active=null, restart=false, failures=0;
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
          const values=JSON.stringify(data.metrics);
          if (values!==lastMetrics) {render(data);lastMetrics=values;}
          researchSummary(data);
          if (standalone) {
            const quote=JSON.stringify([data.name,data.price,data.change_pct,data.quote_basis,data.change_basis,data.quote_at,data.change_at]);
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
