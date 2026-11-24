(() => {
 'use strict';
 const el=id=>document.getElementById(id);
 const id=new URLSearchParams(location.search).get('id');
 const title=el('report-title'),summary=el('report-summary'),basis=el('report-summary-source');
 const retry=el('report-retry'),original=el('report-original');
 let pending=false,finished=false;

 function originalURL(value){
  if(typeof value!=='string')return null;
  try{
   const url=new URL(value);
   return url.origin==='https://pdf.dfcfw.com'&&!url.search&&!url.hash&&
    /^\/pdf\/[A-Za-z0-9_.-]+\.pdf$/i.test(url.pathname)?url:null;
  }catch{return null;}
 }
 function display(packet){
  const report=packet.report;
  title.textContent=report.title;
  document.title=report.title+' · 观澜';
  el('report-meta').textContent=[report.date,report.broker,report.author,[report.company,report.code].filter(Boolean).join(' ')].filter(Boolean).join(' · ');
  el('report-sectors').replaceChildren(...report.sectors.filter(name=>typeof name==='string').map(name=>{
   const tag=document.createElement('span');
   tag.className='tag';tag.textContent=name;
   return tag;
  }));
  const source=originalURL(report.url);
  if(source&&original.href!==source.href){
   original.href=source.href;
   el('report-host').textContent=source.hostname;
   el('report-document').hidden=false;
   import('./pdf-reader.js?v=reader-pinch-20261122').then(module=>module.mountPdf(source.href))
    .catch(()=>{el('pdf-status').textContent='页面暂时无法加载，请通过上方链接打开 PDF。';});
  }
  if(typeof packet.summary?.text==='string'&&packet.summary.text.trim()){
   summary.textContent=packet.summary.text;
   basis.textContent=packet.summary.basis==='pdf_excerpt'?'根据研报原文节选整理':'根据券商原摘要整理';
   basis.hidden=false;
   finished=true;
  }else{
   summary.textContent='摘要生成中';
  }
 }
 async function load(){
  if(pending||finished)return;
  pending=true;
  try{
   const response=await fetch(`../data/reports/items/${id}.json`,{cache:'no-cache'});
   if(!response.ok)throw Error('unavailable report');
   const packet=await response.json();
   if(packet?.schema_version!==1||packet.source!=='primary_research_reports'||packet.report?.id!==id||
      typeof packet.report.title!=='string'||!Array.isArray(packet.report.sectors))throw Error('invalid report');
   display(packet);retry.hidden=true;
  }catch{
   if(title.textContent==='研报阅读')summary.textContent='研报暂时无法读取';
   retry.hidden=false;
  }finally{pending=false;}
 }
 if(!/^[a-f0-9]{24}$/.test(id||'')){
  summary.textContent='请从研报列表选择一份研报';
  return;
 }
 retry.addEventListener('click',load);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
 setInterval(()=>{if(!document.hidden)load();},30000);
 load();
})();
