'use strict';

const get = id => document.getElementById(id);
const format = new Intl.NumberFormat('zh-CN');
const fmt = n => format.format(n || 0);
const dateTime = value => value ? value.slice(0, 19).replace('T', ' ') : '等待';
let selected = '';
let version = null;

function detail(day) {
  const counts = day.audited_windows === day.windows
    ? `原始 ${fmt(day.returned)} 条 · 同文不同时间 ${fmt((day.occurrences||0)-(day.collected||0))} 条 · 同一秒重复 ${fmt(day.same_time_copies)} 条`
    : day.returned!=null ? `原始 ${fmt(day.returned)} 条 · 时间戳复核中` : '等待源回包';
  get('detail').textContent = `${day.day} · ${counts} · 收录 ${fmt(day.collected)} 条 · 已标注 ${fmt(day.labelled)} 条${day.incomplete_windows ? ' · 有未核实窗口' : ''}`;
}

function show(data) {
  const end = new Date(Date.parse(data.end_day_exclusive+'T00:00:00Z')-86400000).toISOString().slice(0,10);
  get('scope').textContent = `计划范围 ${data.start_day} 至 ${end} · ${data.total_days} 天 · ${data.source}`;
  get('message').textContent = data.label_error
    ? `MiniMax 等待恢复 · ${data.label_error} · 下次尝试 ${dateTime(data.label_retry_at)}`
    : `进度更新 ${dateTime(data.generated_at)} 北京时间`;
  get('queried').textContent = `${data.queried_days} / ${data.total_days} 天`;
  get('with-news').textContent = data.queried_days < data.total_days
    ? `尚有 ${fmt(data.total_days-data.queried_days)} 天待查或待核实`
    : `${data.days_with_news} 天取得快讯`;
  get('collected').textContent = fmt(data.collected);
  get('missing').textContent = `${fmt(data.missing_body)} 条缺少正文`;
  get('labelled').textContent = fmt(data.labelled);
  get('pending').textContent = `${fmt(data.collected-data.labelled)} 条未标注 · ${fmt(data.exhausted)} 条待复核`;
  get('tokens').textContent = fmt(data.usage.total_tokens);
  get('calls').textContent = `${fmt(data.usage.calls)} 次模型调用记录`;
  const archive = data.final_archived_at
    ? `正式归档已校验 · ${dateTime(data.final_archived_at)}`
    : `等待正式归档 · 上次阶段导出 ${dateTime(data.export_at)}`;
  get('sampling').textContent = `已复核来源时间 ${fmt(data.audited_days)}/${data.total_days} 天 · 最近采集 ${dateTime(data.latest_collection?.checked_at)} · ${archive}`;
  const days = get('days');
  const controls = [];
  const fragment = document.createDocumentFragment();
  const recent = [...data.days].reverse().find(day => day.updated);
  if (!selected && recent) selected = recent.day;
  for (const day of data.days) {
    const completed = day.complete_windows === day.windows;
    const kind = !day.updated ? 'waiting' : completed && !day.collected ? 'empty'
      : completed && day.labelled === day.collected ? 'ready' : 'partial';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = kind;
    btn.title = `${day.day} · ${fmt(day.collected)} 条已收录，${fmt(day.labelled)} 条已标注`;
    btn.setAttribute('aria-label', btn.title);
    btn.setAttribute('aria-pressed', String(day.day === selected));
    if (day.day === selected) detail(day);
    btn.addEventListener('click', () => {
      selected = day.day;
      controls.forEach(control => control.setAttribute('aria-pressed', String(control === btn)));
      detail(day);
    });
    controls.push(btn);
    fragment.append(btn);
  }
  days.replaceChildren(fragment);
  get('metrics').hidden = get('coverage').hidden = false;
}

async function refresh() {
  try {
    const response = await fetch('../data/training-progress.json', {cache:'no-cache', signal:AbortSignal.timeout(6000)});
    if (!response.ok) throw new Error('unavailable');
    const etag = response.headers.get('etag');
    if (etag && etag === version) return;
    const data = await response.json();
    if (data.schema_version !== 1 || !Array.isArray(data.days)) throw new Error('invalid');
    show(data);
    version = etag;
  } catch {
    get('message').textContent = '进度暂不可读取，页面会自动重试。';
  }
}

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
