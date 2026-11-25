'use strict';

globalThis.GuanlanScheduler = function ({api, authError}) {
  const el = id => document.getElementById(id);
  const endpoint = '/api/admin/scheduler';
  const date = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const time = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
  const statuses = {running:'运行中', success:'成功', failed:'失败', skipped:'跳过', unknown:'待确认'};
  const timerLabels = {active:'已启用', inactive:'未运行', failed:'定时器失败', activating:'启动中', deactivating:'停止中'};
  const outcomes = {ok:'成功', provider_429:'上游业务码 429', http_429:'HTTP 429', transport_error:'传输失败', provider_rate_limited:'上游限流', in_flight:'请求中', legacy_unobserved:'未记录结果'};
  const sources = {initializing:'调度快照', services:'服务状态', timers:'定时器', journal:'运行日志', traffic:'请求计量', collector:'状态采集', control:'任务控制'};
  const reasons = {condition:'执行条件未满足', 'exec-condition':'执行条件未满足', 'exit-code':'进程退出异常', timeout:'超时', signal:'收到终止信号', 'oom-kill':'内存不足', shared_spacing:'共享请求间隔', rate_limited:'冷却限流', transport_error:'传输冷却', http_error:'HTTP 冷却', budget_exhausted:'任务预算耗尽', adaptive_spacing:'自适应请求间隔'};
  let active = false, authorized = false, loading = false, writing = false, epoch = 0, failedRead = false;
  let snapshot = null;
  let warmupTries = 0, warmupTimer = 0;

  function make(tag, value) {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = value;
    return node;
  }
  const known = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const number = value => known(value) ? value.toLocaleString('zh-CN') : '—';
  function stamp(value) {
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? date.format(ms) : '—';
  }
  function duration(seconds) {
    if (!known(seconds)) return '—';
    if (seconds < 1) return Math.round(seconds * 1000) + ' 毫秒';
    if (seconds < 60) return Math.round(seconds * 10) / 10 + ' 秒';
    return Math.floor(seconds / 60) + ' 分 ' + Math.floor(seconds % 60) + ' 秒';
  }
  function message(id, value, error = false) {
    el(id).textContent = value;
    el(id).dataset.error = String(error);
  }
  function tableRow(values, head = true) {
    const tr = make('tr');
    values.forEach((value, index) => {
      const cell = make(head && index === 0 ? 'th' : 'td');
      if (head && index === 0) cell.scope = 'row';
      if (typeof value === 'string') cell.textContent = value;
      else cell.append(value);
      tr.append(cell);
    });
    return tr;
  }
  function table(id, rows, columns, empty) {
    if (!rows.length) {
      const tr = make('tr'), cell = make('td', empty);
      cell.colSpan = columns;
      tr.append(cell);
      rows.push(tr);
    }
    el(id).replaceChildren(...rows);
  }
  function pausedUntil(id) {
    const until = snapshot?.control?.pauses?.[id];
    return known(until) && until * 1000 > Date.now() ? new Date(until * 1000).toISOString() : null;
  }
  function stateLabel(status) {
    const node = make('span', statuses[status] || statuses.unknown);
    node.className = 'schedule-state';
    node.dataset.state = status || 'unknown';
    return node;
  }
  function renderTraffic() {
    const traffic = snapshot.traffic;
    const starts = traffic?.starts_last_60s, limit = traffic?.limit_60s;
    el('schedule-rolling').textContent = number(starts) + ' / ' + number(limit);
    const meter = el('schedule-meter');
    meter.hidden = !(known(starts) && known(limit) && limit > 0);
    if (!meter.hidden) { meter.max = limit; meter.value = starts; }
    el('schedule-peak').textContent = number(traffic?.peak_60s);
    el('schedule-deferred').textContent = number(traffic?.deferred_total);
    el('schedule-429').textContent = (traffic?.rt_429_capped ? '≥' : '') + number(traffic?.rt_429_last_10m);
    const details = [];
    if (traffic?.since) details.push('累计计量始于 ' + stamp(traffic.since));
    if (known(traffic?.realtime_gap_seconds)) details.push('当前实时行情批次最小间隔 ' + traffic.realtime_gap_seconds + ' 秒');
    el('schedule-traffic-note').textContent = details.join(' · ') || '等待共享请求计量。';
    const waits = Object.entries(traffic?.deferred_by_reason || {}).sort((a,b) => b[1]-a[1]);
    el('schedule-deferrals').textContent = waits.map(([key,value]) => (reasons[key] || key) + ' ' + number(value)).join(' · ');
    const rows = [...(traffic?.recent || [])].sort((a,b) => Date.parse(b.at) - Date.parse(a.at)).map(event =>
      tableRow([stamp(event.at), event.api || '—', event.worker || '—', outcomes[event.outcome] || event.outcome || '—', known(event.duration_ms) ? duration(event.duration_ms / 1000) : '—']));
    table('schedule-requests', rows, 5, traffic ? '近期样本中没有请求。' : '请求计量暂不可用。');
  }
  function renderTimeline() {
    if (!snapshot) return;
    const end = Date.parse(snapshot.generated_at), span = Number(el('schedule-window').value) * 60000;
    const start = end - span;
    const runsByTask = new Map(snapshot.tasks.map(task => [task.id, []]));
    const starts = new Map();
    // Reverse newest-first journal entries to retain true source order within the same second.
    const chronological = [...(snapshot.events || [])].reverse().sort((a,b) => Date.parse(a.at)-Date.parse(b.at));
    for (const event of chronological) {
      const runs = runsByTask.get(event.task_id), when = Date.parse(event.at);
      if (!runs || !Number.isFinite(when)) continue;
      if (event.status === 'running') starts.set(event.task_id, when);
      else if (['success','failed','skipped'].includes(event.status)) {
        const began = starts.get(event.task_id);
        if (began !== undefined && when >= began) runs.push({began, ended:when, status:event.status});
        starts.delete(event.task_id);
      }
    }
    const lanes = [];
    for (const task of snapshot.tasks) {
      const runs = runsByTask.get(task.id);
      const began = Date.parse(task.last_start_at);
      const ended = task.status === 'running' ? end : Date.parse(task.last_end_at);
      if (Number.isFinite(began) && Number.isFinite(ended) && ended >= began) {
        const same = runs.find(run => run.began === began || run.ended === ended && run.ended >= began && ended >= run.began);
        if (same) Object.assign(same, {ended, status:task.status});
        else runs.push({began, ended, status:task.status});
      }
      const visible = runs.filter(run => run.began <= end && run.ended >= start).sort((a,b) => a.began-b.began);
      if (visible.length) lanes.push({task, runs:visible});
    }
    lanes.sort((a,b) => a.runs[0].began - b.runs[0].began);
    const box = el('schedule-timeline');
    if (!lanes.length) {
      const empty = make('p', '此时间范围内没有可绘制的任务起止记录。');
      empty.className = 'schedule-empty'; box.replaceChildren(empty); return;
    }
    const axis = make('div'), ticks = make('div');
    axis.className = 'schedule-axis';
    ticks.append(...[start,(start+end)/2,end].map(at => make('span',time.format(at))));
    axis.append(make('span','北京时间'), ticks);
    const rows = lanes.map(({task, runs}) => {
      const row = make('div'), track = make('div'), name = make('span', task.label);
      row.className = 'schedule-lane'; track.className = 'schedule-track';
      name.title = task.label + ' · ' + runs.length + ' 轮';
      for (const {began, ended, status} of runs) {
        const bar = make('i');
        bar.title = task.label + ' · ' + (statuses[status] || statuses.unknown) + ' · ' +
          date.format(began) + ' 至 ' + (status === 'running' ? '观测时刻' : date.format(ended));
        bar.setAttribute('role','img'); bar.setAttribute('aria-label',bar.title);
        const left = (Math.max(start,began)-start)/span*100;
        const width = (Math.min(end,ended)-Math.max(start,began))/span*100;
        bar.style.left = 'min(' + left + '%, calc(100% - 3px))';
        bar.style.width = width + '%'; bar.dataset.state = status; track.append(bar);
      }
      row.append(name,track); return row;
    });
    box.replaceChildren(axis, ...rows);
  }
  function renderTasks() {
    if (!snapshot) return;
    const filter = el('schedule-filter').value;
    const tasks = snapshot.tasks.filter(task => filter === 'all' || (filter === 'paused' ? pausedUntil(task.id) : task.status === filter));
    const rows = tasks.map(task => {
      const until = pausedUntil(task.id);
      const schedule = until ? '暂停至 ' + stamp(until) : task.timer ? (timerLabels[task.timer_state] || task.timer_state || '待确认') : '无独立定时器';
      const title = make('span', task.label || task.id); title.title = task.unit || task.id;
      if (typeof task.schedule === 'string' && task.schedule) title.append(make('small', task.schedule));
      const result = stateLabel(task.status);
      if (['failed','skipped'].includes(task.status) && task.last_result) {
        result.append(make('small', reasons[task.last_result] || task.last_result));
      }
      return tableRow([title, result, stamp(task.last_trigger_at), stamp(task.last_start_at), duration(task.duration_seconds), stamp(task.next_at), schedule]);
    });
    table('schedule-rows', rows, 7, snapshot.tasks.length ? '没有符合条件的任务。' : '暂未取得任务目录。');
    const names = new Map(snapshot.tasks.map(task => [task.id, task.label]));
    const events = [...(snapshot.events || [])].sort((a,b) => Date.parse(b.at)-Date.parse(a.at));
    table('schedule-events', events.map(event => tableRow([stamp(event.at), names.get(event.task_id) || event.task_id || '—',
      event.status === 'running' ? '启动' : stateLabel(event.status), reasons[event.reason] || event.reason || '—'])), 4,
      snapshot.unavailable?.includes('journal') ? '运行日志暂不可用。' : '近期样本中没有任务事件。');
  }
  function controls() {
    const select = el('schedule-task'), id = select.value;
    const enabled = authorized && !writing && !loading && !failedRead && Number.isInteger(snapshot?.control?.revision) && !!id;
    const paused = pausedUntil(id);
    select.disabled = !enabled;
    el('schedule-minutes').disabled = !enabled;
    el('schedule-pause').disabled = !enabled;
    el('schedule-resume').disabled = !enabled || !paused;
    el('schedule-refresh').disabled = !authorized || loading || writing;
    el('schedule-control-current').textContent = !snapshot ? '' : !Number.isInteger(snapshot.control?.revision) ? '控制策略暂不可用。' :
      !id ? '没有支持临时暂停的后台任务。' : paused ? '暂停至 ' + stamp(paused) + '（北京时间）。' : '所选任务按原计划调度。';
  }
  function render() {
    renderTraffic(); renderTasks(); renderTimeline();
    const select = el('schedule-task'), previous = select.value;
    const items = snapshot.tasks.filter(task => task.controllable);
    select.replaceChildren(...items.map(task => { const option = make('option', task.label); option.value = task.id; return option; }));
    if (items.some(task => task.id === previous)) select.value = previous;
    el('schedule-results').hidden = false;
    el('schedule-time').textContent = snapshot.generated_at ? '观测于 ' + stamp(snapshot.generated_at) + ' · 北京时间' : '尚未取得观测时间';
    const partial = (snapshot.unavailable || []).map(s => sources[s] || s).join('、');
    const note = snapshot.unavailable?.includes('initializing') ? '正在采集首份调度快照…' :
      [snapshot.stale ? '部分观测尚未更新。' : '', partial ? '暂缺' + partial + '。' : ''].join('');
    message('schedule-notice', note, !!note);
    controls();
  }
  async function load() {
    if (!active || !authorized || loading || writing) return;
    const ticket = epoch; loading = true; controls();
    try {
      const fresh = await api(endpoint);
      if (ticket !== epoch) return;
      if (fresh.schema_version !== 1 || !Array.isArray(fresh.tasks)) throw Error('调度数据格式异常。');
      snapshot = fresh; failedRead = false; render();
      if (snapshot.unavailable?.includes('initializing') && warmupTries < 4) {
        warmupTries++;
        clearTimeout(warmupTimer);
        warmupTimer = setTimeout(load, 2000);
      } else if (!snapshot.unavailable?.includes('initializing')) warmupTries = 0;
    } catch (error) {
      if (ticket !== epoch || authError(error)) return;
      failedRead = true;
      message('schedule-notice', snapshot ? '刷新失败，当前显示上次观测。请刷新后再操作。' : '读取调度台失败，请稍后刷新。', true);
      if (!snapshot) el('schedule-time').textContent = '尚未取得调度状态';
    } finally {
      if (ticket === epoch) { loading = false; controls(); }
    }
  }
  async function save(minutes) {
    if (writing || loading || !snapshot || !authorized || failedRead) return;
    const id = el('schedule-task').value;
    if (!snapshot.tasks.some(task => task.id === id && task.controllable) || !Number.isInteger(snapshot.control?.revision)) return;
    const task = snapshot.tasks.find(item => item.id === id);
    const ticket = epoch; writing = true; controls();
    message('schedule-feedback', '正在保存…');
    try {
      const result = await api(endpoint,{task_id:id, minutes, revision:snapshot.control.revision});
      if (ticket !== epoch) return;
      if (!Number.isInteger(result.revision) || !result.pauses || typeof result.pauses !== 'object') throw Error('保存响应异常。');
      snapshot.control = result;
      renderTasks();
      message('schedule-feedback', task.label + (minutes ? '已暂停 ' + minutes + ' 分钟。' : '已恢复调度。'));
    } catch (error) {
      if (ticket !== epoch || authError(error)) return;
      failedRead = true;
      message('schedule-feedback', error.status === 409 ? '其他管理员已更新策略，正在重新读取，请核对后再操作。' :
        '未能确认操作结果，正在重新读取当前策略。', true);
    } finally {
      if (ticket === epoch) { writing = false; controls(); await load(); }
    }
  }
  function route() {
    const hash = location.hash;
    if (hash !== '#main') active = hash === '#scheduler' || hash === '#policy-section';
    el('scheduler').hidden = !active; el('usage').hidden = active;
    for (const [id,current] of [['usage-link',!active],['scheduler-link',active]]) {
      if (current) el(id).setAttribute('aria-current','page'); else el(id).removeAttribute('aria-current');
    }
    if (active && authorized) load();
  }
  el('schedule-refresh').addEventListener('click', load);
  el('schedule-filter').addEventListener('change', renderTasks);
  el('schedule-window').addEventListener('change', renderTimeline);
  el('schedule-task').addEventListener('change', controls);
  el('schedule-form').addEventListener('submit', event => { event.preventDefault(); save(Number(el('schedule-minutes').value)); });
  el('schedule-resume').addEventListener('click', () => save(0));
  window.addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  setInterval(() => { if (!document.hidden) load(); }, 30000);
  route();
  return {
    authorize() { if (!authorized) { authorized = true; controls(); load(); } },
    clear() {
      epoch++; authorized = false; loading = false; writing = false; snapshot = null; failedRead = false;
      clearTimeout(warmupTimer); warmupTries = 0;
      el('schedule-results').hidden = true; el('schedule-time').textContent = '管理后台仅面向管理员。';
      for (const id of ['schedule-rows','schedule-events','schedule-requests','schedule-task','schedule-timeline']) el(id).replaceChildren();
      for (const id of ['schedule-rolling','schedule-peak','schedule-deferred','schedule-429']) el(id).textContent = '—';
      el('schedule-meter').hidden = true; el('schedule-traffic-note').textContent = ''; el('schedule-deferrals').textContent = '';
      message('schedule-notice','需要管理员权限才能查看调度台。',true); message('schedule-feedback',''); controls();
    }
  };
};
