'use strict';

window.AssetChart = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  const instances = new WeakMap();
  const studies = {
    momentum20: ['20日动量', '%'], volatility20: ['20日年化波动率', '%'],
    beta60: ['60日沪深300贝塔', '倍'], drawdown60: ['60日最大回撤', '%']
  };
  const days = /^\d{4}-\d{2}-\d{2}$/;
  const numbers = new Intl.NumberFormat('zh-CN', {maximumFractionDigits:2});
  const precise = new Intl.NumberFormat('zh-CN', {maximumFractionDigits:4});
  const finite = Number.isFinite;
  const positive = v => finite(v) && v > 0;
  const label = (v, unit='') => finite(v) ? precise.format(v)+unit : '—';

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function svg(tag, attrs, text) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k,v]) => node.setAttribute(k, String(v)));
    if (text != null) node.textContent = text;
    return node;
  }
  function control(text, action, cls, description) {
    const button = el('button', cls, text);
    button.type = 'button';
    if (description) button.setAttribute('aria-label', description);
    button.addEventListener('click', action);
    return button;
  }
  function tradingSession(key) {
    const hk = key.endsWith('.HK');
    return {start:570, midday:hk?720:690, resume:780, close:hk?960:900, gap:12};
  }
  function sessionX(minute, s) {
    return minute <= s.midday ? minute-s.start : s.midday-s.start+s.gap+minute-s.resume;
  }
  function minuteAt(x, session) {
    const offset=Math.floor(x);
    const morningEnd=session.midday-session.start;
    const afternoonStart=morningEnd+session.gap;
    if (offset>=0 && offset<morningEnd) return session.start+offset;
    if (offset>=afternoonStart && offset<sessionX(session.close,session)) return session.resume+offset-afternoonStart;
    return null;
  }
  const clock=minute=>String(Math.floor(minute/60)).padStart(2,'0')+':'+String(Math.floor(minute%60)).padStart(2,'0');
  function read(chart, key) {
    const daily = chart?.daily || {}, intraday = chart?.intraday || {};
    const byDay = new Map();
    for (const row of Array.isArray(daily.rows) ? daily.rows : []) {
      if (row && days.test(row.day)) byDay.set(row.day, row);
    }
    const dailyRows = [...byDay.values()].sort((a,b) => a.day.localeCompare(b.day)).map((r,i) => ({
      ...r, close:positive(r.close)?r.close:null, ma20:positive(r.ma20)?r.ma20:null, x:i, id:r.day
    }));
    const session = tradingSession(key), heat = intraday.measure === 'change_pct';
    const weighted = intraday.measure === 'weighted_change_pct';
    const percent = heat || weighted || intraday.measure === 'constituent_change_pct';
    const maxGap = positive(intraday.max_gap_minutes) ? intraday.max_gap_minutes : 35;
    function samples(pairs, signed) {
      const byTime=new Map();
      for (const pair of Array.isArray(pairs)?pairs:[]) {
        if (!Array.isArray(pair)) continue;
        const [timestamp,value]=pair;
        if (typeof timestamp!=='string' || !/T.*(?:Z|[+-]\d\d:\d\d)$/i.test(timestamp) ||
            !(signed?finite(value):positive(value))) continue;
        const ms=Date.parse(timestamp);
        if (!finite(ms)) continue;
        const local=new Date(ms+8*3600000).toISOString();
        if (local.slice(0,10)!==intraday.day) continue;
        const minute=Number(local.slice(11,13))*60+Number(local.slice(14,16))+Number(local.slice(17,19))/60;
        const bucket=Math.floor(minute);
        const phase=bucket<session.start?'pre':bucket<=session.midday?'morning':bucket<session.resume?'lunch':
          bucket<=session.close?'afternoon':'post';
        const position=phase==='morning'?Math.min(minute,session.midday):Math.min(minute,session.close);
        // Heat rankings are published at real wall-clock times, including during the break.
        if (!heat && phase!=='morning' && phase!=='afternoon') continue;
        const coverage=intraday.partial && Number.isInteger(pair[2]) && pair[2]>0 &&
          Number.isInteger(pair[3]) && pair[3]>=pair[2] ? [pair[2],pair[3]] : null;
        if(intraday.partial && !coverage)continue;
        byTime.set(ms,{coverage,id:String(ms),time:local.slice(11,19),x:heat?minute:sessionX(position,session),value,minute,bucket,phase});
      }
      const rows=[...byTime.values()].sort((a,b)=>a.minute-b.minute);
      rows.forEach((r,i)=>{
        const prior=rows[i-1];
        r.bridge=Boolean(prior && r.phase===prior.phase && (heat?r.minute-prior.minute>maxGap:r.bucket>prior.bucket+1));
        r.gap=Boolean(prior && r.phase!==prior.phase);
      });
      return rows;
    }
    return {daily,intraday,dailyRows,minutes:samples(intraday.points,percent),
      relative:samples(intraday.relative,true),session,heat,weighted,percent,maxGap};
  }

  function range(values) {
    let low=Infinity, high=-Infinity;
    for (const v of values) if (finite(v)) {low=Math.min(low,v); high=Math.max(high,v);}
    if (!finite(low)) return null;
    const pad = (high-low)*0.10 || Math.max(Math.abs(high)*0.004,0.01);
    return [low-pad,high+pad];
  }
  function addSeries(graph, rows, field, x, y, cls, joinDaily=false) {
    const runs=[];
    let run=[];
    function flush() { if (run.length) runs.push(run); run=[]; }
    for (const r of rows) {
      if (!joinDaily && (r.gap || !finite(r[field]))) flush();
      if (finite(r[field])) run.push([x(r.x),y(r[field])]);
    }
    flush();
    const path = runs.filter(p=>p.length>1).map(p=>p.map(([a,b],i)=>
      (i?'L':'M')+a.toFixed(2)+' '+b.toFixed(2)).join(' ')).join(' ');
    if (path) graph.append(svg('path',{d:path,class:'ac-line '+cls,fill:'none'}));
    for (const p of runs) if (p.length===1) graph.append(svg('circle',{cx:p[0][0],cy:p[0][1],r:2.6,class:'ac-dot '+cls}));
  }
  function nearest(points, target) {
    let best=0;
    for (let i=1;i<points.length;i++) if (Math.abs(points[i].x-target)<Math.abs(points[best].x-target)) best=i;
    return best;
  }

  class View {
    constructor(host,key) {
      this.host=host; this.key=key; this.name=''; this.mode=null; this.study='momentum20';
      this.anchor={daily:null,intraday:null,members:null}; this.width=0; this.chart=read(null,key);
      this.hasMembers=key.startsWith('sector/'); this.manualMode=false;
      this.officialSector=this.hasMembers && /\.(?:SH|SZ)$/.test(key);
      this.root=el('section','asset-chart-view'); this.root.setAttribute('aria-label','行情走势');
      const header=el('header','ac-head');
      const modes=el('div','ac-controls'); modes.setAttribute('aria-label','行情周期');
      this.modes={};
      const marketLabel=this.officialSector?'板块指数分时':key.endsWith('.TI')?'热榜分时':'权重估算分时';
      const choices=[['intraday',this.hasMembers?marketLabel:'分时'],['daily','日线']];
      if(this.hasMembers)choices.splice(this.officialSector?1:0,0,['members','成分股分时']);
      for (const [id,text] of choices) {
        this.modes[id]=control(text,()=>{this.manualMode=true;this.mode=id;this.draw();},'ac-choice');
        modes.append(this.modes[id]);
      }
      header.append(el('h2','', '行情走势'),modes);
      this.readout=el('div','ac-readout'); this.time=el('span','ac-time');
      this.primary=el('strong','ac-current'); this.change=el('span','ac-change'); this.context=el('span','ac-context');
      this.change.title='涨跌幅 · 相对上一交易日收盘';
      this.previous=control('‹',()=>this.step(-1),'ac-arrow','上一个采样点');
      this.next=control('›',()=>this.step(1),'ac-arrow','下一个采样点');
      const arrows=el('span','ac-arrows'); arrows.append(this.previous,this.next);
      this.readout.append(this.time,this.primary,this.change,this.context,arrows);
      this.legend=el('div','ac-legend'); this.main=el('div','ac-plot ac-main-plot');
      this.main.tabIndex=0; this.main.setAttribute('role','slider');
      this.main.setAttribute('aria-label','行情采样点，可按左右方向键查看');
      this.main.setAttribute('aria-orientation','horizontal');
      this.subpanel=el('div','ac-study'); this.selector=el('select','ac-select');
      this.selector.setAttribute('aria-label','副图指标');
      for (const [key,[name]] of Object.entries(studies)) {
        const opt=el('option','',name);opt.value=key;this.selector.append(opt);
      }
      this.selector.value=this.study;
      this.selector.addEventListener('change',()=>{this.study=this.selector.value;this.draw();});
      const subhead=el('div','ac-study-head'); this.studyValue=el('strong','ac-study-value');
      this.relativeTitle=el('span','ac-relative-title','相对沪深300 · 涨跌差（百分点）');
      subhead.append(this.selector,this.relativeTitle,this.studyValue); this.sub=el('div','ac-plot ac-study-plot');
      this.subpanel.append(subhead,this.sub);
      this.note=el('div','ac-note');
      this.root.append(header,this.readout,this.legend,this.main,this.subpanel,this.note);
      host.replaceChildren(this.root);
      for (const area of [this.main,this.sub]) {
        const probe=event=>{
          if (!this.axes || !this.points.length) return;
          const box=area.getBoundingClientRect();
          if (!box.width) return;
          const pixel=(event.clientX-box.left)/box.width*this.axes.width;
          const x=this.axes.inverse(pixel), data=this.currentChart();
          const inspectStudy=area===this.sub;
          if (this.mode==='daily') {
            const row=this.rows[Math.round(x)];
            if (row && !finite(row.close)) {this.display({stamp:row.day,x:row.x});return;}
          }
          const minute=this.mode!=='daily' && !data.heat ? minuteAt(x,data.session) : null;
          if (minute!==null) {
            const sampled=this.points.findIndex(point=>point.bucket===minute);
            if (sampled>=0) {this.select(sampled,inspectStudy);return;}
            const now=new Date(Date.now()+8*3600000).toISOString().slice(0,16);
            const stamp=clock(minute);
            if (data.intraday.day+'T'+stamp<now) {this.display({stamp,x});return;}
          }
          const index=nearest(this.points,x);
          if (this.mode!=='daily' && data.heat) {
            const next=x>this.points[index].x?index+1:index;
            const before=this.points[next-1], after=this.points[next];
            if (after?.bridge && Math.min(x-before.x,after.x-x)>data.maxGap/2) {
              this.display({stamp:clock(x),x});return;
            }
          }
          this.select(index,inspectStudy);
        };
        area.addEventListener('pointerdown',probe);
        area.addEventListener('pointermove',e=>{if(e.pointerType!=='touch'||e.buttons)probe(e);});
        area.addEventListener('pointerleave',()=>this.display());
      }
      this.main.addEventListener('keydown',e=>{
        if (!this.points.length) return;
        if (!['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) return;
        e.preventDefault();
        if(e.key==='Home'||e.key==='End')this.select(e.key==='Home'?0:this.points.length-1);
        else this.step(e.key==='ArrowLeft'?-1:1);
      });
      if (typeof ResizeObserver==='function') {
        this.observer=new ResizeObserver(()=>{if (Math.abs(this.measure()-this.width)>1)this.draw();});
        this.observer.observe(host);
      }
    }
    measure() {return Math.max(248,Math.min(1500,Math.round(this.main.getBoundingClientRect().width||this.host.clientWidth||600)));}
    update(chart,name,members) {
      this.chart=read(chart,this.key); this.name=name||'';
      if (this.hasMembers) {
        const complete=Array.isArray(members?.points) && members.points.length>0;
        const partial=Array.isArray(members?.sample_points) && members.sample_points.length>0 &&
          (!complete || members.sample_trade_date>members.trade_date);
        const memberDay=partial?members.sample_trade_date:members?.trade_date;
        const recovered=members?.historical_recovered_count;
        const basis=partial?'仅代表已有报价的成分股':'成分股等权涨跌幅采样' +
          (Number.isInteger(recovered)&&recovered>0?` · 历史分钟补采 ${recovered} 点`:'');
        this.modes.members.hidden=this.officialSector&&!complete&&!partial;
        this.modes.members.textContent=partial?'部分成分分时':'成分股分时';
        this.memberChart=read({intraday:{measure:'constituent_change_pct',day:memberDay,partial,
          points:partial?members.sample_points:members?.points,max_gap_minutes:3,
          basis_label:basis}},this.key);
        if (!this.manualMode) {
          const newestHeat=this.chart.minutes.length && this.chart.intraday.day;
          if (this.memberChart.minutes.length && (!newestHeat || memberDay>newestHeat ||
              !this.officialSector && memberDay===newestHeat)) this.mode='members';
          else if (this.chart.minutes.length) this.mode='intraday';
          else if (this.chart.dailyRows.some(row=>positive(row.close))) this.mode='daily';
          else this.mode=this.officialSector?'intraday':'members';
        }
      } else if (!this.mode || (this.key.startsWith('sector/') && !this.manualMode)) {
        if((this.key.startsWith('sector/') && this.chart.minutes.length) || this.chart.minutes.length>1)this.mode='intraday';
        else if(this.chart.dailyRows.some(r=>positive(r.close)))this.mode='daily';
        else if(this.chart.minutes.length)this.mode='intraday';
      }
      this.draw();
    }
    currentChart() {return this.mode==='members'?this.memberChart:this.chart;}
    step(d) { if(this.points.length)this.select(this.index+d); }
    select(index,inspectStudy=false) {
      this.index=Math.max(0,Math.min(this.points.length-1,index));
      this.anchor[this.mode||'intraday']=this.points[this.index].id;
      this.display(null,inspectStudy);
    }
    draw() {
      const data=this.currentChart(), daily=this.mode==='daily', mode=this.mode||'intraday';
      Object.entries(this.modes).forEach(([id,b])=>b.setAttribute('aria-pressed',String(id===mode)));
      this.rows=daily?data.dailyRows:data.minutes;
      this.field=daily?'close':'value';
      this.points=this.rows.filter(row=>finite(row[this.field]));
      const anchor=this.anchor[mode];
      this.index=this.points.findIndex(r=>r.id===anchor);
      if(this.index<0 && this.points.length && anchor){
        const next=this.points.findIndex(row=>row.id>anchor);
        this.index=next<0?this.points.length-1:Math.max(0,next-1);
      }
      if(this.index<0)this.index=this.points.length-1;
      this.width=this.measure(); this.subpanel.hidden=!daily && !data.relative.length;
      this.selector.hidden=!daily; this.relativeTitle.hidden=daily;
      this.mainGuides=null; this.subGuides=null; this.axes=null;
      this.legend.replaceChildren();
      const dailyPrice=data.daily.basis==='reference_linked'?'收益链收盘':'收盘';
      const name=daily?dailyPrice:mode==='members'?(data.intraday.partial?'部分成分平均涨跌幅':'成分股等权涨跌幅'):data.heat?'热榜涨跌幅':data.weighted?'权重股估算涨跌幅':'分时价格';
      this.legend.append(el('span','ac-key ac-price-key',name));
      if(daily?this.rows.some(r=>!finite(r.close)):this.points.some(r=>r.bridge))
        this.legend.append(el('span','','实线跨越数据缺口，悬停查看状态'));
      if(daily && this.rows.some(r=>positive(r.ma20)))this.legend.append(el('span','ac-key ac-average-key','MA20'));
      let baseline=null;
      if(!daily && (data.percent || positive(data.intraday.previous_close))) {
        baseline=data.percent?0:data.intraday.previous_close;
        this.legend.append(el('span','ac-key ac-base-key',data.percent?'零线':'昨收 '+label(baseline)));
      }
      if(!daily && !data.percent && this.points.length){
        const {open,high,low,previous_close:previous}=data.intraday;
        if(positive(open)){
          const jump=positive(previous)?(open/previous-1)*100:null;
          this.legend.append(el('span','ac-level','开 '+label(open)+(finite(jump)?'（'+(jump>0?'+':'')+numbers.format(jump)+'%）':'')));
        }
        if(positive(high))this.legend.append(el('span','ac-level','高 '+label(high)));
        if(positive(low))this.legend.append(el('span','ac-level','低 '+label(low)));
      }
      const dd=data.daily.drawdown;
      const peak=daily && this.points.find(r=>r.day===dd?.peak_day);
      const trough=daily && this.points.find(r=>r.day===dd?.trough_day);
      const showDrop=peak && trough && peak.x<trough.x && positive(dd.pct);
      if(showDrop)this.legend.append(el('span','ac-key ac-dd-key','最大回撤 '+label(dd.pct,'%')+' · '+peak.day.slice(5)+' 至 '+trough.day.slice(5)));
      if(this.points.length) {
        this.mainGuides=this.plot(this.main,this.field,daily?['ma20']:[],baseline,false,showDrop?[peak,trough]:null);
      } else {
        this.main.replaceChildren(el('p','ac-empty',daily?'等待已核验的日线收盘数据':
          mode==='members'?'等待覆盖达标的成分股报价':'等待交易时段的真实采样'));
      }
      if(daily) {
        if(this.rows.some(r=>finite(r[this.study]))) {
          this.subGuides=this.plot(this.sub,this.study,[],this.study==='momentum20'?0:null,true,null);
        } else this.sub.replaceChildren(el('p','ac-empty ac-study-empty','当前交易日样本不足，等待'+studies[this.study][0]+'更新。'));
      }
      else if(data.relative.length) this.subGuides=this.plot(this.sub,'value',[],0,true,null);
      const note=[];
      if(this.points.length===1)note.push('仅有一个有效采样点');
      if(!daily&&days.test(data.intraday.day))note.push(data.intraday.day);
      const source=daily?data.daily.basis_label:(data.intraday.basis_label||data.intraday.basis);
      if(typeof source==='string'&&source)note.push(source);
      this.note.textContent=note.join(' · ');this.note.hidden=!this.note.textContent;
      this.display();
    }
    plot(host,field,extra,base,small,drop) {
      const data=this.currentChart(), daily=this.mode==='daily', width=this.width;
      const height=small?(width<480?108:122):(width<480?186:220);
      const l=8,r=width-(width<420?52:63),top=10,bottom=height-23;
      const rows=small && !daily?data.relative:this.rows;
      const ohlc=!small && !daily && !data.percent?data.intraday:{};
      const levels=[ohlc.open,ohlc.high,ohlc.low].filter(positive);
      const domain=range([...rows.flatMap(row=>[row[field],...extra.map(k=>row[k])]),base,...levels]);
      const natural=!daily && data.heat;
      const minX=natural?Math.min(data.session.start,data.minutes[0]?.minute??data.session.start):0;
      const maxX=daily?Math.max(1,this.rows.length-1):natural?
        Math.max(data.session.close,data.minutes.at(-1)?.minute??data.session.close):sessionX(data.session.close,data.session);
      const x=val=>l+(val-minX)/(maxX-minX)*(r-l);
      const y=val=>bottom-(val-domain[0])/(domain[1]-domain[0])*(bottom-top);
      const graph=svg('svg',{viewBox:`0 0 ${width} ${height}`,class:'ac-svg','aria-hidden':'true'});
      if(!daily) {
        const start=x(natural?data.session.midday:data.session.midday-data.session.start);
        const end=x(natural?data.session.resume:sessionX(data.session.resume,data.session));
        graph.append(svg('rect',{x:start,y:top,width:end-start,height:bottom-top,class:'ac-recess'}));
      }
      if(drop)graph.append(svg('rect',{x:x(drop[0].x),y:top,width:x(drop[1].x)-x(drop[0].x),height:bottom-top,class:'ac-drawdown'}));
      if(positive(ohlc.high)&&positive(ohlc.low)&&ohlc.high>=ohlc.low){
        const hi=y(ohlc.high),lo=y(ohlc.low);
        graph.append(svg('path',{d:`M${l+6} ${hi}h-5V${lo}h5`,class:'ac-range-mark',fill:'none'}));
      }
      if(positive(ohlc.open))graph.append(svg('line',{x1:l,x2:l+13,y1:y(ohlc.open),y2:y(ohlc.open),class:'ac-open-mark'}));
      const ticks=small?2:3,unit=small?(daily?studies[this.study][1]:'百分点'):!daily&&data.percent?'%':'';
      const step=(domain[1]-domain[0])/ticks;
      const axisNumber=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:Math.min(6,Math.max(0,1-Math.floor(Math.log10(step))))});
      for(let i=0;i<=ticks;i++) {
        const p=domain[1]-(domain[1]-domain[0])*i/ticks,py=y(p);
        graph.append(svg('line',{x1:l,y1:py,x2:r,y2:py,class:'ac-grid'}),
          svg('text',{x:width-2,y:py+3,'text-anchor':'end',class:'ac-axis'},axisNumber.format(p)+(unit==='%'?'%':'')));
      }
      if(finite(base))graph.append(svg('line',{x1:l,x2:r,y1:y(base),y2:y(base),class:'ac-baseline'}));
      const axis = daily ? [...new Set([0,Math.floor((this.rows.length-1)/2),this.rows.length-1])].map(i=>[x(i),this.rows[i].day.slice(5)]) :
        natural?[[l,clock(minX)],[x(720),'12:00'],[r,clock(maxX)]]:
        [[l,'09:30'],[x(data.session.midday-data.session.start+data.session.gap/2),'午休'],[r,data.session.close===960?'16:00':'15:00']];
      axis.forEach(([px,text],i)=>graph.append(svg('text',{x:px,y:height-4,'text-anchor':i===0?'start':i===axis.length-1?'end':'middle',class:'ac-axis'},text)));
      addSeries(graph,rows,field,x,y,small?'ac-factor':'ac-price',daily);
      extra.forEach(k=>addSeries(graph,rows,k,x,y,'ac-average'));
      if(drop)for(const row of drop)graph.append(svg('circle',{cx:x(row.x),cy:y(row.close),r:3,class:'ac-peak'}));
      const guide=svg('line',{y1:top,y2:bottom,class:'ac-guide'});
      const dot=svg('circle',{r:3.6,class:small?'ac-focus ac-factor':'ac-focus ac-price'});
      graph.append(guide,dot); host.replaceChildren(graph);
      if(!small)this.axes={width,inverse:pixel=>minX+(pixel-l)/(r-l)*(maxX-minX)};
      return {guide,dot,x,y,field,relative:small&&!daily};
    }
    display(missing=null,inspectStudy=false) {
      const data=this.currentChart(), row=missing?null:this.points[this.index], daily=this.mode==='daily';
      const unit=!daily&&data.percent?'%':this.key.startsWith('stock/')?(this.key.endsWith('.HK')?' 港元':' 元'):' 点';
      this.time.textContent=missing?missing.stamp:row?(daily?row.day:row.time):'';
      this.primary.className=missing?'ac-current ac-missing':'ac-current';
      this.primary.textContent=missing?'未采集到数据':row?label(row[this.field],unit):'—';
      const prior=daily?(row&&!row.gap?data.dailyRows[row.x-1]?.close:null):data.intraday.previous_close;
      const change=row?(daily||!data.percent?(positive(prior)?(row[this.field]/prior-1)*100:null):row.value):null;
      const rounded=finite(change)?Number(change.toFixed(2)):null;
      const direction=!finite(rounded)||rounded===0?'neutral':rounded>0?'up':'down';
      this.primary.setAttribute('data-direction',!daily&&data.percent&&row?direction:'');
      this.change.setAttribute('data-direction',direction);
      this.change.textContent=row&&(daily||!data.percent)?
        (finite(rounded)?(rounded>0?'+':'')+rounded.toFixed(2)+'%':'—'):'';
      const context=[];
      if(row && daily) {
        if(positive(row.ma20))context.push('MA20 '+label(row.ma20));
        if(data.daily.basis==='reference_linked' && positive(row.raw_close))context.push('原始收盘 '+label(row.raw_close));
      }
      if(row?.coverage)context.push(`样本 ${row.coverage[0]}/${row.coverage[1]} 只`);
      this.context.textContent=context.join(' · ');
      let paired=null;
      if(!daily && row && data.relative.length){
        const near=data.relative[nearest(data.relative,row.x)];
        if(near.phase===row.phase && (inspectStudy?near.bucket===row.bucket:Math.abs(near.minute-row.minute)<=1.5))paired=near;
      }
      const studyMissing=inspectStudy&&row&&(daily?!finite(row[this.study]):!paired);
      this.studyValue.className=studyMissing?'ac-study-value ac-missing':'ac-study-value';
      this.studyValue.textContent=studyMissing?'未采集到数据':daily&&row?label(row[this.study],studies[this.study][1]):paired?
        label(paired.value,' 个百分点')+(paired.time!==row.time?' · '+paired.time:''):'—';
      for(const g of [this.mainGuides,this.subGuides])if(g&&(row||missing)) {
        const actual=g.relative?paired:row;
        const px=g.x(missing?.x??row.x);g.guide.setAttribute('x1',px);g.guide.setAttribute('x2',px);
        const v=actual?.[g.field];
        g.dot.setAttribute('visibility',finite(v)?'visible':'hidden');
        if(finite(v)){g.dot.setAttribute('cx',g.x(actual.x));g.dot.setAttribute('cy',g.y(v));}
      }
      const total=this.points.length;
      this.main.setAttribute('aria-disabled',String(!total));
      this.main.setAttribute('aria-valuemin',total?'1':'0');
      this.main.setAttribute('aria-valuemax',String(total));
      this.main.setAttribute('aria-valuenow',String(total?this.index+1:0));
      const study=studyMissing?this.studyValue.textContent:daily&&row?studies[this.study][0]+' '+this.studyValue.textContent:paired?'相对沪深300 '+this.studyValue.textContent:'';
      this.main.setAttribute('aria-valuetext',[this.time.textContent,this.primary.textContent,this.change.textContent&&'涨跌幅 '+this.change.textContent,this.context.textContent,study].filter(Boolean).join('，'));
      this.previous.disabled=this.index<=0;this.next.disabled=this.index<0||this.index>=total-1;
    }
  }

  function mount(host,chart,{key='',name='',members}={}) {
    if(!host)return;
    let view=instances.get(host);
    if(!view || view.key!==key){
      if(view?.observer)view.observer.disconnect();
      view=new View(host,key);instances.set(host,view);
    }
    view.update(chart,name,members);
  }
  return {mount};
})();
