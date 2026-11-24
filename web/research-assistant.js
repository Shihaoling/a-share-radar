(() => {
  'use strict';
  const entry = document.currentScript;
  const page = entry?.dataset.assistantPage;
  if (!page && document.querySelector('meta[name="radar-live-quotes"]')?.content !== 'sxdata') return;
  document.body.insertAdjacentHTML('beforeend', `<div id="research-assistant" hidden>
    <button id="assistant-toggle" class="assistant-toggle assistant-glass" type="button" title="研究助手" aria-label="研究助手" aria-expanded="false" aria-controls="assistant-dock">
      <span class="assistant-orb" aria-hidden="true"></span>
    </button>
    <aside id="assistant-dock" class="assistant-dock assistant-glass" aria-labelledby="assistant-title" hidden>
      <header class="assistant-header">
        <span class="assistant-orb" aria-hidden="true"></span>
        <div><h2 id="assistant-title">研究助手</h2><p id="assistant-page-context" hidden></p></div>
        <button id="assistant-history-toggle" class="assistant-control" type="button" title="历史对话" aria-label="历史对话" aria-expanded="false" aria-controls="assistant-history-menu" hidden>↺</button>
        <button id="assistant-new" class="assistant-control" type="button" title="新对话" aria-label="新对话">＋</button>
        <button id="assistant-close" class="assistant-control" type="button" title="收起助手" aria-label="收起助手">×</button>
        <div id="assistant-history-menu" class="assistant-history-menu" hidden>
          <label for="assistant-history">历史对话</label>
          <select id="assistant-history"><option value="">选择一段对话</option></select>
        </div>
      </header>
      <div id="assistant-thread" class="assistant-thread">
        <div id="assistant-welcome" class="assistant-welcome">
          <h3>想看什么？</h3>
          <p><a href="/metrics/">指标口径一览</a></p>
          <div class="assistant-skills" aria-label="分析技能">
            <button id="assistant-current-metric" type="button" data-skill="instrument" data-question="请解释当前页面标的的主要研究指标，包括估值及数据口径。" hidden>当前标的指标</button>
            <button id="assistant-current-report" type="button" data-skill="reports" data-question="请总结这篇研报的核心观点、关键数据和主要风险。" hidden>解读本篇研报</button>
            <button type="button" data-skill="metrics" data-question="市盈率、滚动市盈率和市净率分别怎么计算？">估值公式</button>
            <button type="button" data-skill="market" data-question="最近一个交易日，主要指数和申万行业表现如何？">市场与行业</button>
            <button type="button" data-skill="trend" data-question="比较 CPO、半导体设备和算力租赁近 5、10、20 个交易日的趋势。">板块趋势</button>
            <button type="button" data-skill="news" data-question="最近有哪些与 A 股科技板块相关的快讯？结合已发布行情解释可能的联系。">快讯与行情</button>
            <button type="button" data-skill="macro" data-question="最近公布了哪些重要中美经济指标？请列出实际值和公布时间。">经济指标</button>
          </div>
        </div>
        <button id="assistant-older" class="assistant-older" type="button" hidden>加载更早的对话</button>
        <div id="assistant-turns" role="log" aria-label="研究对话" aria-live="polite"></div>
      </div>
      <form id="assistant-form" class="assistant-form">
        <div class="assistant-effort-row">
          <span id="assistant-effort-label">回答思考强度</span>
          <div id="assistant-effort" class="assistant-effort" role="group" aria-labelledby="assistant-effort-label">
            <button type="button" data-glass-value="auto">默认</button><button type="button" data-glass-value="low">低</button><button type="button" data-glass-value="medium">中</button>
            <button type="button" data-glass-value="high">高</button><button type="button" data-glass-value="xhigh">很高</button><button type="button" data-glass-value="max">最高</button>
          </div>
        </div>
        <label for="assistant-input" class="assistant-sr">向研究助手提问</label>
        <textarea id="assistant-input" rows="2" maxlength="1000" placeholder="询问本站行情、板块或快讯"></textarea>
        <div class="assistant-form-bar"><span id="assistant-history-note" role="status">仅分析本站已有数据</span><button id="assistant-send" type="submit" disabled>发送 <span aria-hidden="true">↑</span></button><button id="assistant-stop" type="button" hidden>停止等待</button></div>
      </form>
    </aside>
  </div>`);

  const get = id => document.getElementById(id);
  const widget = get('research-assistant');
  const toggle = get('assistant-toggle'), dock = get('assistant-dock'), close = get('assistant-close');
  const thread = get('assistant-thread'), turns = get('assistant-turns'), welcome = get('assistant-welcome');
  const form = get('assistant-form'), input = get('assistant-input'), send = get('assistant-send');
  const stop = get('assistant-stop');
  const historyToggle = get('assistant-history-toggle'), historyMenu = get('assistant-history-menu');
  const historySelect = get('assistant-history'), historyNote = get('assistant-history-note');
  const older = get('assistant-older'), newChat = get('assistant-new');
  const api = '/api/research-assistant/';
  const storageKey = 'guanlan.assistant.conversation';
  const batchSize = 4;
  const guestTurns = [];
  const sourceLinks = new Set(['./#overview', './#news', './#trend', '/#overview', '/#reports', '/#news-announcements', '/metrics/']);
  const titles = {overview:'市场总览', sectors:'板块雷达', news:'A股关联资讯', reports:'研报', 'report-reader':'研报阅读', trend:'趋势观察室',
    settings:'数据设置', asset:'研究详情', sector:'板块详情', account:'账户',
    admin:'控制台', 'training-progress':'采集进度', 'training-monitor':'训练进度', metrics:'指标口径'};
  const pageLabel = get('assistant-page-context');
  const currentMetric = get('assistant-current-metric');
  const currentReport = get('assistant-current-report');
  const effortControl = get('assistant-effort');
  const effortButtons = [...effortControl.querySelectorAll('[data-glass-value]')];
  const effortKey = 'guanlan.assistant.answer-effort';
  let selectedEffort = 'auto';
  try {
    const saved=localStorage.getItem(effortKey);
    if (effortButtons.some(button=>button.dataset.glassValue===saved)) selectedEffort=saved;
  } catch {}
  function paintEffort() {
    for (const button of effortButtons) button.setAttribute('aria-pressed',String(button.dataset.glassValue===selectedEffort));
    if (!dock.hidden) window.GuanlanGlass?.select(effortControl,selectedEffort);
  }
  for (const button of effortButtons) button.addEventListener('click',()=>{
    selectedEffort=button.dataset.glassValue;
    try {localStorage.setItem(effortKey,selectedEffort);} catch {}
    paintEffort();
  });
  if (!window.GuanlanGlass) import('/liquid-glass.js?v=assistant-effort-20261122').then(paintEffort).catch(()=>{});
  paintEffort();
  function pageContext() {
    if (page === 'report-reader') {
      const ident=new URLSearchParams(location.search).get('id');
      const selected=Number(document.getElementById('report-pages')?.dataset.currentPage||
        document.getElementById('pdf-page')?.value);
      return {page,report_id:/^[a-f0-9]{24}$/.test(ident||'')?ident:undefined,
        report_page:Number.isInteger(selected)&&selected>0?selected:undefined};
    }
    if (page === 'asset' || page === 'sector') {
      const query = new URLSearchParams(location.search);
      const code = (query.get('code') || '').toUpperCase();
      const type = page === 'sector' ? 'sector' : query.get('type');
      const chart = document.querySelector('.ac-choice[aria-pressed="true"]')?.textContent;
      const study = chart === '日线' ? document.querySelector('.ac-select')?.value : undefined;
      return {page, subjects: /^[0-9]{5,6}R?\.[A-Z]{2}$/.test(code) ? [{type, code}] : [], chart, study};
    }
    return page ? {page} : window.GuanlanAssistantPage?.() || {page:location.hash.slice(1) || 'overview'};
  }
  function syncPage() {
    const context = pageContext();
    const code = context.subjects?.[0]?.code;
    const label = [titles[context.page], code].filter(Boolean).join(' · ');
    pageLabel.textContent = label;
    pageLabel.hidden = !label;
    currentMetric.hidden = !code;
    currentReport.hidden = !context.report_id;
    return context;
  }
  function sourceHref(href) {
    if (sourceLinks.has(href)) return href.replace(/^\.\//, '/');
    if (/^\/reports\/\?id=[a-f0-9]{24}$/.test(href)) return href;
    return /^\/(?:asset\/\?type=(?:stock|index)&code=|sector\/\?code=)[0-9]{5,6}R?\.[A-Z]{2}$/.test(href) ? href : null;
  }
  let request, mode, viewer, current, before, loading = false, compressing = false;
  let count = 0, summarized = 0, guestSummary = '', notice = '';

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text) item.textContent = text;
    return item;
  }
  async function loadCatalog() {
    const list = get('catalog-entries'), filter = get('catalog-search'), status = get('catalog-status');
    try {
      const {metrics} = await getJSON('metrics');
      function renderCatalog() {
        const query = filter.value.normalize('NFKC').toLowerCase().replace(/\s+/g, '');
        const shown = metrics.filter(row => (row.label + ' ' + row.key).normalize('NFKC').toLowerCase()
          .replace(/\s+/g, '').includes(query));
        status.textContent = shown.length ? shown.length + ' 项指标' : '没有匹配的指标';
        list.replaceChildren(...shown.map(row => {
          const card = node('details');
          card.append(node('summary', '', row.label + (row.unit ? ' · ' + row.unit : '')),
            node('p', '', row.formula), node('p', '', row.description));
          return card;
        }));
      }
      filter.addEventListener('input', renderCatalog);
      renderCatalog();
    } catch {
      status.textContent = '暂时无法读取指标口径';
    }
  }
  function sync() {
    const busy = Boolean(request || compressing || loading);
    send.hidden = Boolean(request);
    input.disabled = Boolean(request || loading);
    stop.hidden = !request;
    send.disabled = busy || !mode || !input.value.trim();
    newChat.disabled = historySelect.disabled = older.disabled = busy;
    older.hidden = !before || turns.hidden;
    historyToggle.hidden = mode !== 'account';
    historyNote.textContent = notice || (mode === 'account' ? (summarized ? '已压缩前 ' + summarized + ' 轮 · 原文已保存' : '历史保存到当前账号') :
      mode === 'guest' ? '未登录 · 历史仅在当前标签页' : '正在确认会话');
  }
  function scroll() {
    thread.scrollTop = thread.scrollHeight;
  }
  function answerText(text) {
    return text.replace(/\bminimax(?:-[a-z0-9][a-z0-9._-]*)?\b/gi, '专属模型')
      .replace(/(^|[。！？\n])\s*(?:以上(?:内容|分析|信息|研判|结论)?仅供参考|(?:以上(?:内容|分析|信息|研判|结论)?|本回答|本分析)不构成(?:任何)?投资建议|投资有风险[，、,]\s*入市需谨慎)[。！! ]*(?=\n|$)/g, '$1').trim();
  }
  let morphing = false;
  function finishDisplay(open) {
    if (open) {
      paintEffort();
      syncPage();
      if (request || compressing) close.focus({preventScroll:true});
      else refreshAccount().then(() => { if (!dock.hidden) input.focus({preventScroll:true}); });
      scroll();
    } else {
      toggle.focus({preventScroll:true});
    }
  }
  function glassFrame(element) {
    const {left, top, width, height} = element.getBoundingClientRect();
    return {left:left+'px', top:top+'px', width:width+'px', height:height+'px',
      borderRadius:getComputedStyle(element).borderRadius};
  }
  async function display(open) {
    if (morphing || open === !dock.hidden) return;
    const from = open ? toggle : dock, to = open ? dock : toggle;
    const shouldAnimate = typeof to.animate === 'function' &&
      !matchMedia('(prefers-reduced-motion: reduce)').matches;
    const start = shouldAnimate ? glassFrame(from) : null;
    if (!open) setHistoryMenu(false);
    toggle.hidden = open;
    toggle.setAttribute('aria-expanded', String(open));
    dock.hidden = !open;
    if (open) paintEffort();
    if (!shouldAnimate) return finishDisplay(open);

    morphing = true;
    to.classList.add('assistant-morph-target');
    const shell = node('div', 'assistant-morph assistant-glass');
    shell.setAttribute('aria-hidden', 'true');
    const orb = node('span', 'assistant-orb');
    shell.append(orb);
    widget.append(shell);
    const options = {duration:open ? 460 : 350, easing:'cubic-bezier(.23,.78,.24,1)', fill:'both'};
    orb.animate([{opacity:open ? 1 : 0}, {opacity:open ? 0 : 1}], options);
    try {
      await shell.animate([start, glassFrame(to)], options).finished;
    } finally {
      shell.remove();
      to.classList.remove('assistant-morph-target');
      morphing = false;
      if (open) for (const item of dock.children) item.animate([{opacity:0}, {opacity:1}], {duration:155});
      finishDisplay(open);
    }
  }
  function addTurn(question, insert = article => turns.append(article)) {
    welcome.hidden = true;
    const turn = node('article', 'assistant-turn');
    const message = node('p', 'assistant-message assistant-question', question);
    message.setAttribute('aria-label', '你的问题');
    const progress = node('details', 'assistant-progress');
    const summary = node('summary', '', '正在准备');
    summary.setAttribute('aria-live','polite');
    const steps = node('ol', 'assistant-steps');
    progress.append(summary, steps);
    const response = node('div', 'assistant-response');
    turn.append(message, progress, response);
    insert(turn);
    return {progress, summary, steps, response, started:performance.now()};
  }
  function paintSaved(rows, destination = turns) {
    for (const item of rows) {
      const turn = addTurn(item.question, article => destination.append(article));
      if (Array.isArray(item.progress?.steps)) {
        for (const step of item.progress.steps) addStep(turn,step,false);
        setComplete(turn,'分析过程 '+turn.steps.children.length+' 步',item.progress.elapsed_ms);
      } else turn.progress.hidden = true;
      turn.response.append(node('p', 'assistant-message', answerText(item.answer)));
      showSources(turn.response, item.sources);
    }
  }
  function showSources(response, sources) {
    if (!sources?.length) return;
    const section = node('details', 'assistant-sources');
    section.append(node('summary', '', '查看所用数据'));
    const list = node('ul');
    for (const source of sources) {
      const item = node('li');
      const label = '[' + source.ref + '] ' + source.label + (source.date ? ' · ' + source.date : '');
      const href = sourceHref(source.href);
      const link = node(href ? 'a' : 'span', '', label);
      if (href) link.href = href;
      item.append(link);
      list.append(item);
    }
    section.append(list);
    response.append(section);
  }
  function addStep(turn,event,working=true) {
    turn.steps.lastElementChild?.classList.remove('working');
    const row = node('li',working?'working':'',event.label);
    if (event.detail) row.append(node('small','',event.detail));
    turn.steps.append(row);
  }
  function elapsedLabel(amount) {
    const seconds=Math.max(0,amount)/1000;
    return seconds<60?seconds.toFixed(1)+' 秒':Math.floor(seconds/60)+' 分 '+Math.floor(seconds%60)+' 秒';
  }
  function setComplete(turn, label, elapsed) {
    turn.steps.lastElementChild?.classList.remove('working');
    turn.summary.textContent=label+' · 用时 '+elapsedLabel(Number.isFinite(elapsed)?elapsed:performance.now()-turn.started);
    turn.progress.open=false;
  }
  function update(turn, event) {
    if (event.type === 'step') {
      addStep(turn,event);
      turn.summary.textContent=event.label;
    } else if (event.type === 'answer') {
      setComplete(turn,'分析过程 '+turn.steps.children.length+' 步',event.progress?.elapsed_ms);
      turn.response.append(node('p', 'assistant-message', answerText(event.answer)));
      showSources(turn.response, event.sources);
    } else if (event.type === 'error') {
      setComplete(turn, '分析未完成');
      turn.response.append(node('p', 'assistant-message assistant-error', event.message));
    }
    scroll();
  }
  async function receive(response, handle) {
    if (!response.ok || !response.body) {
      if (response.status === 429) throw new Error('助手正忙，请稍后再发。');
      throw new Error('研究助手暂时无法连接，请稍后再试。');
    }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '', finished = false;
    const line = text => {
      if (!text.trim()) return;
      const event = JSON.parse(text);
      handle(event);
      if (['answer', 'summary', 'error'].includes(event.type)) finished = true;
    };
    try {
      for (;;) {
        const {value, done} = await reader.read();
        buffer += decoder.decode(value, {stream: !done});
        const lines = buffer.split('\n');
        buffer = lines.pop();
        lines.forEach(line);
        if (done) { line(buffer); break; }
      }
    } finally {
      reader.releaseLock();
    }
    if (!finished) throw new Error('连接提前结束，请重试这次提问。');
  }

  function setHistoryMenu(open) {
    historyMenu.hidden = !open;
    historyToggle.setAttribute('aria-expanded', String(open));
    if (open) historySelect.focus();
  }
  function storedId(id) {
    try {
      if (id === undefined) return sessionStorage.getItem(storageKey);
      if (id) sessionStorage.setItem(storageKey, id);
      else sessionStorage.removeItem(storageKey);
    } catch {}
    return null;
  }
  async function getJSON(path) {
    const response = await fetch(api + path, {cache:'no-store', credentials:'same-origin'});
    if (!response.ok) throw new Error('读取历史失败');
    return response.json();
  }
  function listHistory(items) {
    historySelect.replaceChildren(new Option('选择一段对话', ''));
    for (const item of items) historySelect.add(new Option(item.title, item.id));
    historySelect.value = current || '';
  }
  function clearView() {
    count = summarized = 0;
    before = current = null;
    turns.replaceChildren();
    welcome.hidden = false;
    historySelect.value = '';
  }
  async function showConversation(id) {
    const stored = await getJSON('conversations/' + id);
    clearView();
    current = stored.id;
    count = stored.count;
    summarized = stored.summarized;
    before = stored.before;
    paintSaved(stored.turns);
    storedId(current);
    historySelect.value = current;
    scroll();
  }
  async function refreshAccount() {
    if (loading || request || compressing) return;
    const oldMode = mode;
    const oldViewer = viewer;
    loading = true;
    turns.hidden = true;
    older.hidden = true;
    notice = '';
    sync();
    try {
      const result = await getJSON('conversations');
      mode = result.mode;
      viewer = result.viewer;
      if (mode === 'guest') {
        if (oldMode !== 'guest') {
          clearView();
          count = guestTurns.length;
          summarized = guestThrough;
          paintSaved(guestTurns);
          scroll();
        }
      } else {
        const id = current || storedId();
        const item = result.items.find(row => row.id === id);
        if (oldMode !== 'account' || oldViewer !== viewer || (id && !item)) clearView();
        if (item && (current !== id || count !== item.turn_count)) await showConversation(id);
        listHistory(result.items);
      }
    } catch {
      if (mode === 'account') clearView();
      mode = null;
      viewer = null;
      notice = '无法确认登录状态，请收起后重新打开。';
    } finally {
      loading = false;
      turns.hidden = false;
      sync();
    }
    compressHistory();
  }
  function post(path, body, signal) {
    return fetch(api + path, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
      signal, credentials:'same-origin',
    });
  }
  let guestThrough = 0;
  function guestContext() {
    const start = Math.max(0, guestTurns.length - 6, Math.min(guestThrough, guestTurns.length - 2));
    return {conversation_summary:guestSummary, history:guestTurns.slice(start).flatMap(row => [
      {role:'user', text:row.question}, {role:'assistant', text:row.answer},
    ])};
  }
  async function compressHistory() {
    if (loading || request || compressing || !mode || count - summarized < batchSize) return;
    compressing = true;
    notice = '正在压缩对话…';
    sync();
    try {
      while (count - summarized >= batchSize) {
        const from = summarized;
        const body = mode === 'account' ? {save_history:true, viewer, conversation_id:current} :
          {previous_summary:guestSummary,turns:guestTurns.slice(from,from+batchSize).map(({question,answer})=>({question,answer}))};
        const response = await post('compress', body);
        await receive(response, event => {
          if (event.type === 'error') throw new Error(event.message);
          if (event.type !== 'summary') return;
          if (mode === 'account') summarized = event.summarized;
          else {
            guestSummary = event.summary;
            summarized = guestThrough = from + batchSize;
          }
        });
        if (summarized <= from) throw new Error('摘要未更新');
      }
      notice = '已压缩前 ' + summarized + ' 轮 · 原文仍可查看';
    } catch {
      notice = '暂未压缩，原文已保留，稍后重试。';
    } finally {
      compressing = false;
      sync();
    }
  }

  async function submit(question, skill) {
    question = question.trim();
    if (!question || !mode || request || loading || compressing) return;
    const controller = new AbortController();
    request = controller;
    notice = '';
    const turn = addTurn(question);
    scroll();
    input.value = '';
    sync();
    stop.focus({preventScroll:true});
    try {
      const context = mode === 'account' ? {save_history:true, viewer, conversation_id:current || null} : guestContext();
      const answer_effort=selectedEffort==='auto'?undefined:selectedEffort;
      const response = await post('chat', {question,skill,page_context:syncPage(),answer_effort,...context},controller.signal);
      if (response.status === 401) throw new Error('登录已过期，请重新登录后继续这段对话。');
      if (response.status === 409) throw new Error('当前登录账号已切换，请收起后重新打开助手。');
      await receive(response, event => {
        update(turn, event);
        if (event.type === 'answer') {
          if (mode === 'account' && event.conversation) {
            ({id:current, count, summarized} = event.conversation);
            storedId(current);
          } else if (mode === 'guest') {
            guestTurns.push({question,answer:event.answer,sources:event.sources,progress:event.progress});
            count = guestTurns.length;
          }
        }
        if (event.type === 'error') input.value = question;
      });
    } catch (error) {
      const message = controller.signal.aborted ? '已停止等待。' :
        error instanceof SyntaxError || error instanceof TypeError ? '连接中断，请重试这次提问。' : error.message;
      update(turn, {type: 'error', message});
      input.value = question;
    } finally {
      request = undefined;
      sync();
      if (!dock.hidden && (document.activeElement === stop || document.activeElement === document.body)) input.focus();
      compressHistory();
    }
  }

  toggle.addEventListener('click', () => display(true));
  close.addEventListener('click', () => display(false));
  dock.addEventListener('keydown', event => { if (event.key === 'Escape') display(false); });
  newChat.addEventListener('click', () => {
    if (request || loading || compressing) return;
    const guest = mode === 'guest';
    if (guest) {
      guestTurns.length = 0;
      guestSummary = '';
      guestThrough = 0;
    }
    clearView();
    if (!guest) storedId(null);
    setHistoryMenu(false);
    notice = '';
    input.value = '';
    delete input.dataset.suggested;
    sync();
    input.focus();
  });
  historyToggle.addEventListener('click', async () => {
    if (!historyMenu.hidden) return setHistoryMenu(false);
    await refreshAccount();
    if (mode === 'account' && !dock.hidden) setHistoryMenu(true);
  });
  historySelect.addEventListener('change', async () => {
    const id = historySelect.value;
    if (!id || request || loading || compressing) return;
    loading = true;
    notice = '';
    sync();
    setHistoryMenu(false);
    try { await showConversation(id); }
    catch { notice = '无法打开这段对话，请重新选择。'; }
    finally { loading = false; sync(); }
    compressHistory();
  });
  older.addEventListener('click', async () => {
    if (!before || !current || loading || request || compressing) return;
    const height = thread.scrollHeight, fromTop = thread.scrollTop;
    loading = true;
    sync();
    try {
      const previous = await getJSON('conversations/' + current + '?before=' + before);
      const fragment = document.createDocumentFragment();
      paintSaved(previous.turns, fragment);
      before = previous.before;
      turns.prepend(fragment);
      thread.scrollTop = fromTop + thread.scrollHeight - height;
    } catch { notice = '暂时无法加载更早的对话。'; }
    finally { loading = false; sync(); }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !dock.hidden) refreshAccount();
  });
  stop.addEventListener('click', () => request?.abort());
  input.addEventListener('input', sync);
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    const skill = input.dataset.suggested;
    delete input.dataset.suggested;
    submit(input.value, skill);
  });
  for (const button of welcome.querySelectorAll('[data-question]')) {
    button.addEventListener('click', () => {
      input.value = button.dataset.question;
      input.dataset.suggested = button.dataset.skill;
      sync();
      input.focus();
    });
  }
  window.addEventListener('radar:navigate', syncPage);
  syncPage();
  widget.hidden = false;
  if (page === 'metrics') loadCatalog();

  // Each lens uses a smooth, mostly neutral center and strongly bends the rim.
  function installLens(target, name, radius, rim, strength) {
    if (!CSS.supports('backdrop-filter', 'url(#lens)')) return;
    const svg = 'http://www.w3.org/2000/svg';
    const defs = document.createElementNS(svg, 'svg');
    defs.classList.add('assistant-lens');
    defs.setAttribute('aria-hidden', 'true');
    const filter = document.createElementNS(svg, 'filter');
    filter.id = name;
    filter.setAttribute('color-interpolation-filters', 'sRGB');
    const map = document.createElementNS(svg, 'feImage');
    map.setAttribute('result', 'rim');
    map.setAttribute('preserveAspectRatio', 'none');
    const displacement = document.createElementNS(svg, 'feDisplacementMap');
    for (const [key, value] of Object.entries({in: 'SourceGraphic', in2: 'rim', scale: strength, xChannelSelector: 'R', yChannelSelector: 'G'})) {
      displacement.setAttribute(key, value);
    }
    filter.append(map, displacement);
    defs.append(filter);
    widget.append(defs);
    const canvas = document.createElement('canvas'), context = canvas.getContext('2d');
    if (!context) return;
    new ResizeObserver(([entry]) => {
      const width = Math.round(entry.borderBoxSize?.[0]?.inlineSize || target.offsetWidth);
      const height = Math.round(entry.borderBoxSize?.[0]?.blockSize || target.offsetHeight);
      if (width < 16 || height < 16) return;
      canvas.width = width;
      canvas.height = height;
      const data = context.createImageData(width, height);
      const cx = width / 2, cy = height / 2, r = Math.min(radius, cx, cy);
      for (let y = 0, i = 0; y < height; y++) {
        for (let x = 0; x < width; x++, i += 4) {
          const dx = Math.abs(x - cx) - (cx - r), dy = Math.abs(y - cy) - (cy - r);
          const vx = Math.max(dx, 0), vy = Math.max(dy, 0), diagonal = Math.hypot(vx, vy);
          const distance = r - diagonal - Math.min(0, Math.max(dx, dy));
          const t = 1 - Math.min(1, Math.max(0, distance) / rim);
          const bend = distance < 0 ? 0 : 116 * Math.sin(t * Math.PI * .72) * t;
          const nx = diagonal ? vx / diagonal : Number(dx > dy);
          const ny = diagonal ? vy / diagonal : Number(dy >= dx);
          data.data[i] = 128 - bend * nx * Math.sign(x - cx);
          data.data[i + 1] = 128 - bend * ny * Math.sign(y - cy);
          data.data[i + 2] = 128;
          data.data[i + 3] = 255;
        }
      }
      context.putImageData(data, 0, 0);
      map.setAttribute('href', canvas.toDataURL());
      target.style.setProperty('--assistant-lens', 'url(#' + name + ')');
    }).observe(target);
  }
  installLens(toggle, 'guanlan-lens-toggle', 17, 15, 45);
  installLens(dock, 'guanlan-lens-dock', 26, 27, 56);
})();
