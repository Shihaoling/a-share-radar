(() => {
  'use strict';
  const root=document.documentElement;
  const sunrise=6*60, sunset=18*60;
  const dawn=sunrise-30, dusk=sunset-30;
  let displayed;

  function ease(value) {
    const t=Math.max(0,Math.min(1,value));
    return t*t*(3-2*t);
  }

  function localScene(clock) {
    const minute=clock.getHours()*60+clock.getMinutes()+clock.getSeconds()/60+
      clock.getMilliseconds()/60000;
    const light=ease((minute-dawn)/60)*(1-ease((minute-dusk)/60));
    const twilight=4*light*(1-light);
    const dayProgress=Math.max(0,Math.min(1,(minute-sunrise)/(sunset-sunrise)));
    const nightMinute=minute<sunrise+30?minute+1440:minute;
    const nightX=Math.max(0,Math.min(1,(nightMinute-sunset)/720));
    const reflection=8+84*(dayProgress*light+nightX*(1-light));
    const mode=light>=.58?(minute<sunrise+30?'dawn':'day'):light>0&&minute>dusk?'dusk':'night';
    return {mode,light,twilight,reflection};
  }

  function repaint() {
    const scene=localScene(new Date());
    root.style.setProperty('--market-daylight',scene.light.toFixed(5));
    root.style.setProperty('--market-twilight',scene.twilight.toFixed(5));
    root.style.setProperty('--glass-light-x',scene.reflection.toFixed(4)+'%');
    if(displayed===scene.mode)return;
    displayed=scene.mode;
    root.dataset.marketTheme=scene.mode;
    const chromeColors={day:'#edf6fc',dawn:'#ffeddf',dusk:'#2d2947',night:'#111c30'};
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content',chromeColors[scene.mode]);
  }

  repaint();
  document.addEventListener('DOMContentLoaded',repaint,{once:true});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)repaint();});
  setInterval(()=>{if(!document.hidden)repaint();},1000);
})();
