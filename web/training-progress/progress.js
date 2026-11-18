'use strict';

const get = id => document.getElementById(id);
const format = new Intl.NumberFormat('zh-CN');
const rateFormat = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 1});
const fmt = n => format.format(n || 0);
const dateTime = value => value ? value.slice(0, 19).replace('T', ' ') : '等待';
let selected = '';
let version = null;
let collectionRetryAt = '';

function showCollectionCooldown() {
  const status = get('collection-cooldown');
  const deadline = Date.parse(collectionRetryAt);
  status.hidden = !Number.isFinite(deadline);
  if (status.hidden) return;
  const seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
  status.textContent = seconds
    ? `SXData 限流冷却剩余 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
    : 'SXData 限流冷却已结束，等待采集任务调度';
}

function detail(day, receiving) {
  const counts = receiving ? '等待源回包'
    : day.limited_windows ? `数据源限流 · ${fmt(day.limited_windows)} 个窗口待重试`
    : day.audited_windows === day.windows && day.returned === 0 ? '源接口已返回 0 条'
    : day.audited_windows === day.windows
    ? `原始 ${fmt(day.returned)} 条 · 同文不同时间 ${fmt((day.occurrences||0)-(day.collected||0))} 条 · 同一秒重复 ${fmt(day.same_time_copies)} 条`
    : day.returned!=null ? `原始 ${fmt(day.returned)} 条 · 时间戳复核中`
    : day.updated ? '采集窗口待核实' : '待发起采集';
  const labels = [
    day.label_first_pending ? `${fmt(day.label_first_pending)} 条待首次标注` : '',
    day.label_retry_pending ? `${fmt(day.label_retry_pending)} 条待重试` : '',
    day.label_review_pending ? `${fmt(day.label_review_pending)} 条待复核` : '',
  ].filter(Boolean).map(text => ` · ${text}`).join('');
  get('detail').textContent = `${day.day} · ${counts} · 收录 ${fmt(day.collected)} 条 · 已标注 ${fmt(day.labelled)} 条${labels}${day.incomplete_windows ? ' · 有未核实窗口' : ''}`;
}

function show(data) {
  const inFlightDay = Date.parse(data.collection_inflight_until) > Date.now() ? data.collection_inflight_day : '';
  collectionRetryAt = inFlightDay ? '' : data.collection_retry_at || '';
  showCollectionCooldown();
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
  const rate = data.label_rate;
  const speed = rate?.minutes ? ` · 近 ${rate.minutes} 分钟 ${rateFormat.format(rate.articles / rate.minutes)} 条/分钟` : '';
  get('pending').textContent = `${fmt(data.collected-data.labelled)} 条未标注 · ${fmt(data.exhausted)} 条待复核${speed}`;
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
  if (!selected) selected = inFlightDay || recent?.day || '';
  for (const day of data.days) {
    const completed = day.complete_windows === day.windows;
    const receiving = day.day === inFlightDay;
    const retry = day.limited_windows ? '限流待重试' : day.label_retry_pending ? '标注待重试' : '';
    const kind = receiving ? 'receiving' : retry ? 'retry' : !day.updated ? 'waiting' : completed && !day.collected ? 'empty'
      : completed && day.labelled === day.collected ? 'ready' : 'partial';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = kind;
    const state = receiving ? '等待源回包' : retry;
    btn.title = `${day.day} · ${fmt(day.collected)} 条已收录，${fmt(day.labelled)} 条已标注${state ? ` · ${state}` : ''}`;
    btn.setAttribute('aria-label', btn.title);
    btn.setAttribute('aria-pressed', String(day.day === selected));
    if (day.day === selected) detail(day, receiving);
    btn.addEventListener('click', () => {
      selected = day.day;
      controls.forEach(control => control.setAttribute('aria-pressed', String(control === btn)));
      detail(day, receiving);
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
setInterval(showCollectionCooldown, 1000);
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
