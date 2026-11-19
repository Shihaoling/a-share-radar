'use strict';
(() => {
  const $=id=>document.getElementById(id);
  const numbers=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:0});
  const averages=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:3});
  const when=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  const known=value=>typeof value==='number' && Number.isFinite(value) && value>=0;
  const count=value=>known(value)?numbers.format(value):'—';
  const rate=value=>known(value)?averages.format(value):'—';
  const stamp=value=>typeof value==='string' && Number.isFinite(Date.parse(value))?when.format(new Date(value)):'—';
  const sum=(rows,key)=>{
    const values=rows.map(row=>row[key]).filter(known);
    return values.length?values.reduce((a,b)=>a+b,0):null;
  };
  function cell(tag,value){const node=document.createElement(tag);node.textContent=value;return node;}
  function hint(id,text,error=false){$(id).textContent=text;$(id).dataset.error=String(error);}
  function row(head,values){
    const tr=document.createElement('tr'),th=cell('th',head);th.scope='row';tr.append(th);
    for(const value of values)tr.append(cell('td',value));
    return tr;
  }
  let session=null, loading=false, saving=false, policy=null, dirty=false, policyRevision=0;
  function clearPrivate(){
    $('results').hidden=true;$('policy-section').hidden=true;
    for(const id of ['periods','sources','relay-apis','model-scopes'])$(id).replaceChildren();
    for(const id of ['relay-total','relay-now','model-count','model-tokens'])$(id).textContent='—';
    for(const id of ['relay-limit','token-split','coverage','window-note','historical-note','model-since'])$(id).textContent='';
    session=null;policy=null;dirty=false;policyRevision++;
    $('policy-limit').value='';$('policy-save').disabled=true;$('policy-limit').disabled=true;
    $('as-of').textContent='管理后台仅面向管理员。';
  }
  async function api(path,body){
    const opts={credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(12000)};
    if(body!==undefined){opts.method='POST';opts.headers={'Content-Type':'application/json','X-CSRF-Token':session.csrf_token};opts.body=JSON.stringify(body);}
    const response=await fetch(path,opts);
    let data=null;try{data=await response.json();}catch{}
    if(!response.ok){
      const e=Error(typeof data?.message==='string'?data.message:'服务器暂时无法提供数据。');
      e.status=response.status;throw e;
    }
    if(!data || typeof data!=='object')throw Error('服务返回了无法识别的数据。');
    return data;
  }
  function authError(error){
    if(error.status!==401 && error.status!==403)return false;
    clearPrivate();
    if(error.status===401)location.replace('/account/?next=/admin/');
    else hint('status','当前账户没有管理权限。',true);
    return true;
  }
  function displayUsage(data){
    if(data.schema_version!==1 || !data.relay || !data.minimax || !data.coverage)throw Error('用量统计暂不可用。');
    const {relay,minimax,coverage}=data,history=minimax.historical||{};
    const saved=[history.news_analysis,history.training_labels].filter(r=>r && typeof r==='object');
    const tokens=saved.filter(r=>known(r.usage_responses) && r.usage_responses>0);
    $('as-of').textContent='最近更新 '+stamp(data.as_of)+' · 北京时间';
    $('relay-total').textContent=count(relay.cumulative?.requests);
    $('relay-now').textContent=count(relay.rolling_60s);
    $('relay-limit').textContent='上限 '+count(relay.limit_per_minute)+' 次 / 60 秒';
    $('model-count').textContent=count(sum(saved,'requests'));
    $('model-tokens').textContent=count(sum(tokens,'total_tokens'));
    $('token-split').textContent=tokens.length?'输入 '+count(sum(tokens,'input_tokens'))+' · 输出 '+count(sum(tokens,'output_tokens')):'暂无用量回执';
    const since=stamp(relay.cumulative?.since);
    $('historical-note').textContent=(since==='—'?'':'SXData 自 '+since+' 起累计。')+
      (history.note||'MiniMax 统计已保存的新闻分析及训练标注记录，Token 按实际用量回执合计。');
    const end=Date.parse(data.as_of),start=Date.parse(coverage.since);
    const lower=Number.isFinite(end) && Number.isFinite(start) && known(coverage.window_hours)
      ?new Date(Math.max(start,Math.floor(end/60000)*60000-coverage.window_hours*3600000)).toISOString():coverage.since;
    $('coverage').textContent=stamp(lower)==='—'?'观测窗口建立中。':'观测窗口 '+stamp(lower)+' 至 '+stamp(coverage.through)+' · 北京时间';
    const periodRows=[];
    for(const [service,label] of [[relay,'SXData'],[minimax,'MiniMax']]){
      for(const [key,name] of [['trading','盘中'],['other','盘外']]){
        const p=service.periods?.[key]||{},hasUsage=service===minimax && known(p.usage_responses) && p.usage_responses>0;
        const cells=[count(p.minutes),count(p.requests),known(p.minutes)&&p.minutes>0?rate(p.avg_requests_per_minute):'—',
          count(p.success),count(p.failed),...['input_tokens','output_tokens','total_tokens'].map(k=>hasUsage?count(p[k]):'—')];
        periodRows.push(row(label+' / '+name,cells));
      }
    }
    $('periods').replaceChildren(...periodRows);
    $('window-note').textContent=coverage.note||'每分钟平均按已覆盖的完整分钟计算。没有观测记录的时段显示为 —。';
    function emptyRow(columns,note){
      const tr=document.createElement('tr'),td=cell('td',note);td.colSpan=columns;tr.append(td);return tr;
    }
    const apis=Object.entries(relay.apis||{}).sort(([a],[b])=>a.localeCompare(b));
    $('relay-apis').replaceChildren(...(apis.length?apis.map(([id,v])=>{
      const trading=v.periods?.trading||{},other=v.periods?.other||{};
      const outcomes=v.outcomes;
      const result=key=>outcomes && typeof outcomes==='object'?(outcomes[key]??0):null;
      const providerLimits=outcomes?(result('provider_429')+result('provider_rate_limited')):null;
      return row(id,[count(v.starts),count(result('http_429')),count(providerLimits),count(result('transport_error')),
        count(trading.requests),rate(trading.avg_requests_per_minute),count(other.requests),rate(other.avg_requests_per_minute)]);
    }):[emptyRow(9,'尚未记录接口请求。')]));
    const observed=minimax.observed, names={news_analysis:'即时资讯分析',news_digest:'资讯摘要',training_labels:'训练数据打标'};
    $('model-since').textContent=observed?.since?'持续计量始于 '+stamp(observed.since)+'，与历史记录的重叠时段单独统计。':'等待持续计量的首笔调用。';
    const groups=Object.entries(minimax.scopes||{}).map(([id,r])=>[names[id]||id,r]);
    if(observed)groups.unshift(['持续计量合计',{...observed,periods:minimax.periods}]);
    for(const [id,label] of [['news_analysis','历史资讯分析'],['training_labels','历史训练打标']]){
      if(history[id] && known(history[id].requests))groups.push([label,history[id]]);
    }
    $('model-scopes').replaceChildren(...(groups.length?groups.map(([name,r])=>row(name,[
      ...['requests','success','failed','usage_responses'].map(k=>count(r[k])),
      ...['input_tokens','output_tokens','total_tokens'].map(k=>r.usage_responses>0?count(r[k]):'—'),
      rate(r.periods?.trading?.avg_requests_per_minute),rate(r.periods?.other?.avg_requests_per_minute)
    ])):[emptyRow(10,'尚未记录任务调用。')]));
    const sources=Array.isArray(data.sources)?data.sources:[],sourceRows=[];
    for(const source of sources){
      const tr=row(source.label||source.id||'数据源',['',stamp(source.last_success_at),stamp(source.expires_at)]);
      const status=source.status==='cooling_down'?'冷却中':source.status==='connected'||source.connected===true?'已接通':'待核实';
      const statusNode=cell('span',status);
      statusNode.className='source-state';statusNode.dataset.state=status==='已接通'?'ok':status==='冷却中'?'error':'unknown';
      tr.children[1].append(statusNode);sourceRows.push(tr);
    }
    if(!sourceRows.length){
      sourceRows.push(emptyRow(4,'暂无已核实接通的数据源。'));
    }
    $('sources').replaceChildren(...sourceRows);
    $('results').hidden=false;
  }
  function displayPolicy(next){
    if(!Number.isInteger(next.limit_60s)||next.limit_60s<1||next.limit_60s>100)throw Error('调度上限响应异常。');
    policy=next;
    $('policy-limit').max=String(Math.min(100,Number.isInteger(next.maximum)?next.maximum:100));
    if(!dirty)$('policy-limit').value=String(next.limit_60s);
    $('policy-limit').disabled=saving;$('policy-save').disabled=saving;
    const updated=next.updated_at?' · 最近修改 '+stamp(next.updated_at):'';
    const expires=next.expires_at?' · 额度到期 '+stamp(next.expires_at):'';
    hint('policy-state','当前上限 '+count(next.limit_60s)+' 次'+updated+expires);
  }
  async function refresh(){
    if(loading||saving)return;
    loading=true;$('refresh').disabled=true;
    try{
      const next=await api('/api/account/session');
      if(!next.user){clearPrivate();location.replace('/account/?next=/admin/');return;}
      if(next.user.role!=='admin'){clearPrivate();hint('status','当前账户没有管理权限。',true);return;}
      if(!next.csrf_token)throw Error('账户会话未完成验证。');
      session=next;$('policy-section').hidden=false;
      const revision=policyRevision;
      const [usage,policyResult]=await Promise.allSettled([api('/api/admin/usage'),api('/api/admin/policy')]);
      for(const reply of [usage,policyResult])if(reply.status==='rejected' && authError(reply.reason))return;
      try{
        if(usage.status!=='fulfilled')throw usage.reason;
        displayUsage(usage.value);hint('status','');
      }catch{hint('status',$('results').hidden?'用量统计暂不可用，请稍后刷新。':'本次刷新失败，仍显示上次成功统计。',true);}
      if(revision===policyRevision && !saving){
        if(policyResult.status==='fulfilled')displayPolicy(policyResult.value);
        else{policy=null;$('policy-limit').disabled=true;$('policy-save').disabled=true;hint('policy-state','调度设置暂不可用，请刷新后重试。',true);}
      }
    }catch(error){if(!authError(error))hint('status',$('results').hidden?'管理服务暂不可用，请稍后刷新。':'连接暂时中断，仍显示上次成功统计。',true);}
    finally{loading=false;$('refresh').disabled=saving;}
  }
  $('policy-limit').addEventListener('input',()=>{dirty=true;});
  $('policy-form').addEventListener('submit',async event=>{
    event.preventDefault();
    if(saving||!session||!policy)return;
    const limit=Number($('policy-limit').value),maximum=Number($('policy-limit').max);
    if(!Number.isInteger(limit)||limit<1||limit>maximum){hint('policy-state','请输入 1 至 '+maximum+' 之间的整数。',true);return;}
    saving=true;policyRevision++;$('policy-save').disabled=true;$('policy-limit').disabled=true;$('refresh').disabled=true;
    try{const next=await api('/api/admin/policy',{limit_60s:limit});dirty=false;displayPolicy(next);}
    catch(error){if(authError(error))return;hint('policy-state','未能确认保存结果，请刷新核对当前上限。',true);}
    finally{saving=false;$('policy-limit').disabled=!policy;$('policy-save').disabled=!policy;$('refresh').disabled=loading;}
  });
  $('refresh').addEventListener('click',refresh);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  setInterval(()=>{if(!document.hidden)refresh();},60000);
  refresh();
})();
