/* Optional cookieless hosted analytics. No API key belongs in this file. */
(() => {
  'use strict';
  let cfg, loaded = false, lastPage = null;
  let current = {page:'overview', title:'市场总览'};
  function track(detail) {
    current = detail || current;
    if (!loaded || !window.goatcounter?.count) return;
    const page = String(current.page || 'overview').replace(/[^a-z0-9_-]/gi, '');
    if (page === lastPage) return;
    lastPage = page;
    const path = location.pathname.replace(/index\.html$/, '') + page;
    // No search query or arbitrary user-supplied content is transmitted.
    window.goatcounter.count({path, title:String(current.title || page), referrer: document.referrer ? new URL(document.referrer).origin : ''});
    // Separate explicit page-view event; the regular entry is a session visit.
    window.goatcounter.count({path:'pageview:' + page, title:'浏览 · ' + String(current.title || page),event:true,no_session:true,referrer:''});
  }
  window.addEventListener('radar:navigate', e => track(e.detail));
  fetch('./data/analytics.json', {cache:'no-store'}).then(r => r.ok ? r.json() : null).then(value => {
    cfg = value;
    if (!cfg?.enabled || !/^https:\/\/[a-z0-9-]+\.goatcounter\.com\/count$/.test(cfg.endpoint)) return;
    if (['localhost','127.0.0.1',''].includes(location.hostname)) return;
    if (navigator.doNotTrack === '1' || navigator.globalPrivacyControl === true) return;
    window.goatcounter = {no_onload:true};
    const script = document.createElement('script');
    script.async = true;
    script.src = 'https://gc.zgo.at/count.js';
    script.dataset.goatcounter = cfg.endpoint;
    script.onload = () => { loaded = true; track(current); };
    document.head.append(script);
  }).catch(() => { /* Research pages keep working if analytics is unavailable. */ });
})();
