'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag) {
    this.tag=tag; this.children=[]; this.attrs={}; this.events={}; this.textContent='';
    this.className=''; this.clientWidth=360;
  }
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=[...children];}
  setAttribute(name,value) {this.attrs[name]=String(value);}
  getBoundingClientRect() {return {width:this.clientWidth,left:0};}
  addEventListener(name,callback) {(this.events[name]??=[]).push(callback);}
  fire(name,props={}) {
    const event={target:this,preventDefault(){this.defaultPrevented=true;},...props};
    (this.events[name]||[]).forEach(fn=>fn(event));return event;
  }
}
const doc={createElement:name=>new Element(name), createElementNS:(_,name)=>new Element(name)};
let resize;
class Observer {
  constructor(callback){this.callback=callback;resize=this;}
  observe(host){this.host=host;}
  disconnect(){this.disconnected=true;}
}
const scope=vm.createContext({document:doc,window:{},ResizeObserver:Observer,
  fetch:()=>{throw Error('A chart must not contact another data source');}});
vm.runInContext(fs.readFileSync('web/asset/chart.js','utf8'),scope);
const {mount}=scope.window.AssetChart;
const tree=node=>[node,...node.children.flatMap(tree)];
const classes=node=>(node.className||node.attrs.class||'').split(' ');
const nodes=(host,name)=>tree(host).filter(el=>classes(el).includes(name));
const one=(host,name)=>{const matching=nodes(host,name);assert.equal(matching.length,1,name);return matching[0];};
const words=host=>tree(host).map(n=>n.textContent).join(' ');
const key={key:'stock/600001.SH',name:'研究样本'};
const at=t=>'2026-11-19T'+t+':00+08:00';
const iData={day:'2026-11-19',basis_label:'实际报价采样',measure:'price',previous_close:100,open:101,high:106,low:99,
  max_gap_minutes:3,points:[[at('09:30'),101],[at('09:31'),102],[at('09:40'),103],[at('11:30'),100],[at('13:00'),104],
    [at('13:01'),105],[at('13:02'),106],[at('12:00'),777],['2026-11-18T14:00:00+08:00',999]],
  relative:[[at('09:30'),-1],[at('09:31'),0],[at('13:00'),2.4]]};
const dData={basis:'reference_linked',basis_label:'按参考昨收收益链折算',
  rows:[{day:'2026-11-09',close:90,raw_close:87,ma20:80,momentum20:0,volatility20:14,beta60:0.8,drawdown60:4},
    {day:'2026-11-10',close:100,raw_close:98,ma20:85,momentum20:1,volatility20:15,beta60:0.9,drawdown60:3},
    {day:'2026-11-11',close:null,gap:true},
    {day:'2026-11-12',close:95,raw_close:94,ma20:87,momentum20:2,volatility20:16,beta60:1.3,drawdown60:5,gap:true},
    {day:'2026-11-13',close:80,raw_close:80,ma20:85,momentum20:-3,volatility20:10,beta60:1.1,drawdown60:16}],
  drawdown:{peak_day:'2026-11-12',trough_day:'2026-11-13',pct:15.79}};
const host=new Element('div');
const packet={daily:dData,intraday:iData};
mount(host,undefined,key);
assert.match(words(host),/等待交易时段/);
assert.equal(one(host,'ac-main-plot').attrs['aria-disabled'],'true');
const main=one(host,'ac-main-plot');
const choices=nodes(host,'ac-choice'), slider=main;
mount(host,packet,key);
assert.equal(one(host,'ac-main-plot'),slider);
assert.equal(main.attrs['aria-valuemax'],'7');
assert.match(words(host),/开 101（\+1%）/);assert.match(words(host),/高 106/);assert.match(words(host),/低 99/);
assert.equal(nodes(main,'ac-range-mark').length,1);
assert.equal(nodes(main,'ac-open-mark').length,1);
let lines=nodes(main,'ac-line').filter(n=>classes(n).includes('ac-price'));
assert.equal(lines.length,1);assert.equal((lines[0].attrs.d.match(/M/g)||[]).length,2);
assert.equal(nodes(main,'ac-dot').length,2);
assert.match(main.attrs['aria-valuetext'],/13:02:00，106 元/);
assert.equal(main.fire('keydown',{key:'Home'}).defaultPrevented,true);
assert.match(main.attrs['aria-valuetext'],/09:30:00，101 元.*相对沪深300 -1 个百分点/);
main.fire('keydown',{key:'ArrowRight'});
assert.match(main.attrs['aria-valuetext'],/09:31:00.*0 个百分点/);
mount(host,{...packet,intraday:{...iData,points:[...iData.points,[at('13:03'),107]]}},key);
assert.equal(one(host,'ac-main-plot'),slider);
assert.equal(nodes(host,'ac-choice')[0],choices[0]);
assert.match(main.attrs['aria-valuetext'],/09:31:00/);
main.fire('pointerdown',{pointerType:'touch',clientX:355});
assert.match(main.attrs['aria-valuetext'],/13:03:00/);
assert.equal(nodes(host,'ac-arrow')[1].disabled,true);
choices[1].fire('click');
assert.equal(choices[1].attrs['aria-pressed'],'true');
assert.equal(main.attrs['aria-valuemax'],'4');
assert.equal(nodes(main,'ac-drawdown').length,1);assert.equal(nodes(main,'ac-peak').length,2);
assert.match(words(host),/最大回撤 15.79% · 11-12 至 11-13/);
lines=nodes(main,'ac-line').filter(n=>classes(n).includes('ac-average'));
assert.equal(lines.length,1);assert.equal((lines[0].attrs.d.match(/M/g)||[]).length,2);
assert.match(main.attrs['aria-valuetext'],/2026-11-13.*MA20 85/);
main.fire('keydown',{key:'Home'});assert.match(main.attrs['aria-valuetext'],/原始收盘 87/);
const select=one(host,'ac-select');select.value='beta60';select.fire('change');
assert.match(main.attrs['aria-valuetext'],/60日沪深300贝塔 0.8倍/);
const sub=one(host,'ac-study-plot');
assert.ok(nodes(sub,'ac-line').some(n=>classes(n).includes('ac-factor')));
assert.ok(!nodes(main,'ac-line').some(n=>classes(n).includes('ac-factor')));
mount(host,packet,key);assert.equal(one(host,'ac-select'),select);assert.equal(select.value,'beta60');
assert.equal(choices[1].attrs['aria-pressed'],'true');assert.match(main.attrs['aria-valuetext'],/2026-11-09/);
main.clientWidth=680; resize.callback();
assert.match(one(main,'ac-svg').attrs.viewBox,/0 0 680 /);assert.equal(one(host,'ac-select').value,'beta60');
choices[0].fire('click');assert.match(main.attrs['aria-valuetext'],/13:02:00/);

const heatHost=new Element('div');
const ranks={intraday:{day:'2026-11-19',measure:'change_pct',max_gap_minutes:35,previous_close:NaN,
  points:[['2026-11-19T08:45:57+08:00',-1.3],[at('09:30'),-2],[at('10:00'),0],[at('11:00'),1],[at('11:30'),-0.2],[at('12:00'),0.7],[at('13:00'),1.2],[at('16:00'),-0.5]],
  high:100,low:99,open:101}};
mount(heatHost,ranks,{key:'sector/885001.TI'});
const hotPlot=one(heatHost,'ac-main-plot');
assert.match(words(heatHost),/热榜涨跌幅/);assert.match(words(heatHost),/零线/);
assert.equal(nodes(hotPlot,'ac-range-mark').length,0);
assert.equal(nodes(hotPlot,'ac-baseline').length,1);
assert.equal(nodes(hotPlot,'ac-dot').length,4);
assert.equal(hotPlot.attrs['aria-valuemax'],'8');
assert.match(words(hotPlot),/08:45.*12:00.*16:00/);
assert.equal((nodes(hotPlot,'ac-line')[0].attrs.d.match(/M/g)||[]).length,2);
hotPlot.fire('keydown',{key:'Home'});assert.match(hotPlot.attrs['aria-valuetext'],/08:45:57.*-1.3%/);
hotPlot.fire('keydown',{key:'ArrowRight'});assert.match(hotPlot.attrs['aria-valuetext'],/-2%/);
hotPlot.fire('keydown',{key:'ArrowRight'});assert.match(hotPlot.attrs['aria-valuetext'],/10:00:00，0%/);

const singleton={daily:{rows:[{day:'2026-11-19',close:7.5,raw_close:7.5}]}};
const lastObserver=resize;
mount(heatHost,singleton,{key:'index/000300.SH'});
assert.equal(lastObserver.disconnected,true);
const singlePlot=one(heatHost,'ac-main-plot');
assert.match(words(heatHost),/仅有一个有效采样点/);
assert.equal(nodes(singlePlot,'ac-dot').length,1);assert.equal(nodes(singlePlot,'ac-line').length,0);
assert.match(singlePlot.attrs['aria-valuetext'],/7.5 点/);
assert.match(words(one(heatHost,'ac-study-plot')),/样本不足/);
assert.equal(nodes(singlePlot,'ac-drawdown').length,0);
console.log('Asset chart DOM checks passed');
