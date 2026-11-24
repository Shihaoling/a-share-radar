import {AnnotationMode, GlobalWorkerOptions, getDocument} from './pdfjs-6.5.438/pdf.min.js';

const assets=new URL('./pdfjs-6.5.438/',import.meta.url);
GlobalWorkerOptions.workerSrc=new URL('pdf.worker.min.js',assets).href;
const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));

function protectWheel(container){
 let timer;
 function release(){
  clearTimeout(timer);
  container.classList.remove('is-wheel-guarded');
 }
 function hold(){
  clearTimeout(timer);
  container.classList.add('is-wheel-guarded');
  timer=setTimeout(release,1000);
 }
 container.addEventListener('pointerenter',event=>{if(event.pointerType==='mouse')hold();});
 container.addEventListener('pointerleave',event=>{if(event.pointerType==='mouse')release();});
 container.addEventListener('pointerdown',event=>{if(event.pointerType==='touch')release();});
 if(matchMedia('(any-hover:hover) and (any-pointer:fine)').matches&&container.matches(':hover'))hold();
}

function bindPinch(container,currentScale,changeZoom){
 let nativeStart,frame,pending;
 function queue(scale,event){
  const point={x:event.clientX,y:event.clientY};
  pending={scale:clamp(scale,50,200),point};
  if(frame)return;
  frame=requestAnimationFrame(()=>{
   const {scale,point}=pending;
   pending=undefined;
   frame=undefined;
   changeZoom(scale,point);
  });
 }
 container.addEventListener('wheel',event=>{
  if(!event.ctrlKey||!event.cancelable)return;
  event.preventDefault();
  if(nativeStart!==undefined||!event.deltaY)return;
  const unit=event.deltaMode===1?16:event.deltaMode===2?container.clientHeight:1;
  const factor=Math.exp(clamp(-event.deltaY*unit/100,-.35,.35));
  queue((pending?.scale??currentScale())*factor,event);
 },{passive:false});
 // WebKit exposes an absolute scale relative to the start of its pinch.
 container.addEventListener('gesturestart',event=>{
  event.preventDefault();
  nativeStart=pending?.scale??currentScale();
 },{passive:false});
 container.addEventListener('gesturechange',event=>{
  if(nativeStart===undefined)return;
  event.preventDefault();
  if(Number.isFinite(event.scale)&&event.scale>0)queue(nativeStart*event.scale,event);
 },{passive:false});
 container.addEventListener('gestureend',event=>{
  if(nativeStart===undefined)return;
  event.preventDefault();
  nativeStart=undefined;
 },{passive:false});
}

export async function mountPdf(url){
 const container=document.getElementById('report-pages');
 const controls=document.getElementById('report-pager');
 const status=document.getElementById('pdf-status');
 const input=document.getElementById('pdf-page');
 const previous=document.getElementById('pdf-previous');
 const next=document.getElementById('pdf-next');
 const zoom=document.getElementById('pdf-zoom');
 const zoomOut=document.getElementById('pdf-zoom-out');
 const zoomReset=document.getElementById('pdf-zoom-reset');
 const zoomIn=document.getElementById('pdf-zoom-in');
 const pdf=await getDocument({
  url,withCredentials:false,isEvalSupported:false,enableXfa:false,
  cMapUrl:new URL('cmaps/',assets).href,cMapPacked:true,
  standardFontDataUrl:new URL('standard_fonts/',assets).href,
  wasmUrl:new URL('wasm/',assets).href,iccUrl:new URL('iccs/',assets).href,
 }).promise;
 const first=await pdf.getPage(1);
 const preview=first.getViewport({scale:1});
 const pages=Array.from({length:pdf.numPages},(_,index)=>{
  const element=document.createElement('div');
  element.className='reader-pdf-page';
  element.style.aspectRatio=`${preview.width} / ${preview.height}`;
  element.dataset.page=String(index+1);
  return {element,number:index+1,revision:0,width:0,pending:0,near:false};
 });
 container.replaceChildren(...pages.map(page=>page.element));
 container.hidden=false;
 protectWheel(container);
 let activePage=1,scrollFrame,scale=100,zoomRenderTimer;
 input.max=pdf.numPages;
 document.getElementById('pdf-total').textContent=`/ 共 ${pdf.numPages} 页`;
 controls.hidden=false;
 zoom.hidden=false;
 status.hidden=true;

 function pageAt(anchor){
  let low=0,high=pages.length;
  while(low+1<high){
   const middle=(low+high)>>1;
   if(pages[middle].element.offsetTop<=anchor)low=middle;
   else high=middle;
  }
  return low;
 }
 function updatePage(){
  const anchor=container.scrollTop+container.clientHeight*.38;
  const atBottom=container.scrollTop>0&&container.scrollTop+container.clientHeight>=container.scrollHeight-1;
  activePage=atBottom?pages.length:pageAt(anchor)+1;
  container.dataset.currentPage=String(activePage);
  if(document.activeElement!==input)input.value=String(activePage);
  previous.disabled=activePage===1;
  next.disabled=activePage===pdf.numPages;
 }
 function schedulePage(){
  if(scrollFrame)return;
  scrollFrame=requestAnimationFrame(()=>{scrollFrame=undefined;updatePage();});
 }
 function jump(number){
  const requested=Math.round(Math.max(1,Math.min(Number(number)||activePage,pdf.numPages)));
  container.scrollTop=requested===1?0:pages[requested-1].element.offsetTop;
  updatePage();
  input.value=String(activePage);
 }
 function renderNearby(){
  for(const view of pages)if(view.near)render(view);
 }
 function changeZoom(requested,point){
  const nextScale=clamp(requested,50,200);
  if(nextScale===scale)return;
  let x=container.clientWidth/2,y=container.clientHeight*.38;
  if(point){
   const bounds=container.getBoundingClientRect();
   if(point.x>=bounds.left&&point.x<bounds.right)x=point.x-bounds.left;
   if(point.y>=bounds.top&&point.y<bounds.bottom)y=point.y-bounds.top;
  }
  const top=container.scrollTop;
  const page=pages[pageAt(top+y)].element;
  const vertical=(top+y-page.offsetTop)/page.offsetHeight;
  const horizontal=(container.scrollLeft+x-page.offsetLeft)/page.offsetWidth;
  scale=nextScale;
  container.style.setProperty('--pdf-scale',String(scale/100));
  container.scrollTop=!point&&top===0?0:page.offsetTop+vertical*page.offsetHeight-y;
  container.scrollLeft=page.offsetLeft+horizontal*page.offsetWidth-x;
  zoomReset.textContent=`${Math.round(scale)}%`;
  zoomOut.disabled=scale===50;
  zoomIn.disabled=scale===200;
  clearTimeout(zoomRenderTimer);
  if(point){
   // The current canvases stretch immediately. Refresh their resolution once the pinch settles.
   zoomRenderTimer=setTimeout(()=>{zoomRenderTimer=undefined;renderNearby();},120);
  }else{
   zoomRenderTimer=undefined;
   renderNearby();
  }
  schedulePage();
 }
 async function render(view){
  const width=view.element.clientWidth;
  if(!width||view.pending===width)return;
  if(view.width===width){
   if(view.pending){
    view.revision++;
    view.task?.cancel();
    view.task=undefined;
    view.pending=0;
   }
   return;
  }
  view.pending=width;
  view.task?.cancel();
  const revision=++view.revision;
  try{
   const page=view.number===1?first:await pdf.getPage(view.number);
   if(revision!==view.revision)return;
   const base=page.getViewport({scale:1});
   view.element.style.aspectRatio=`${base.width} / ${base.height}`;
   const viewport=page.getViewport({scale:width/base.width});
   const density=Math.min(window.devicePixelRatio||1,2,Math.sqrt(6_000_000/(viewport.width*viewport.height)));
   const canvas=document.createElement('canvas');
   canvas.width=Math.ceil(viewport.width*density);
   canvas.height=Math.ceil(viewport.height*density);
   canvas.setAttribute('aria-label',`研报原文第 ${view.number} 页`);
   view.task=page.render({canvas,viewport,transform:[density,0,0,density,0,0],
    annotationMode:AnnotationMode.DISABLE});
   await view.task.promise;
   if(revision!==view.revision)return;
   view.element.replaceChildren(canvas);
   view.width=width;
  }catch{
   if(revision!==view.revision)return;
   const message=document.createElement('p');
   message.textContent=`第 ${view.number} 页暂时无法加载，可通过上方链接打开 PDF。`;
   view.element.replaceChildren(message);
  }finally{
   if(revision===view.revision){view.task=undefined;view.pending=0;}
  }
 }
 const observer=new IntersectionObserver(entries=>{
  for(const entry of entries){
   const view=pages[Number(entry.target.dataset.page)-1];
   view.near=entry.isIntersecting;
   if(view.near){
    if(!zoomRenderTimer||!view.element.firstChild)render(view);
   }else if(view.width||view.pending||view.element.firstChild){
    view.revision++;
    view.task?.cancel();
    view.task=undefined;
    view.width=view.pending=0;
    view.element.replaceChildren();
   }
  }
  schedulePage();
 },{root:container,rootMargin:'420px 0px'});
 pages.forEach(view=>observer.observe(view.element));
 container.addEventListener('scroll',schedulePage,{passive:true});
 previous.addEventListener('click',()=>jump(activePage-1));
 next.addEventListener('click',()=>jump(activePage+1));
 bindPinch(container,()=>scale,changeZoom);
 zoomOut.addEventListener('click',()=>changeZoom(scale-25));
 zoomIn.addEventListener('click',()=>changeZoom(scale+25));
 zoomReset.addEventListener('click',()=>changeZoom(100));
 input.addEventListener('change',()=>jump(input.value));
 input.addEventListener('keydown',event=>{
  if(event.key!=='Enter')return;
  event.preventDefault();
  jump(input.value);
 });
 let width=container.clientWidth;
 new ResizeObserver(()=>{
  if(container.clientWidth===width)return;
  width=container.clientWidth;
  if(!zoomRenderTimer)renderNearby();
  schedulePage();
 }).observe(container);
 updatePage();
}
