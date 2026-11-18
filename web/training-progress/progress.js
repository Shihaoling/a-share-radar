'use strict';

const get = id => document.getElementById(id);
const numberFormat = new Intl.NumberFormat('zh-CN');
const rateFormat = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 1});
const dateFormat = new Intl.DateTimeFormat('zh-CN', {dateStyle: 'full', timeZone: 'UTC'});
const monthFormat = new Intl.DateTimeFormat('zh-CN', {year: 'numeric', month: 'long', timeZone: 'UTC'});
const fmt = value => numberFormat.format(value ?? 0);
const dateTime = value => typeof value === 'string' && value ? value.slice(0, 19).replace('T', ' ') : '等待';
const dateFromKey = key => new Date(key + 'T00:00:00Z');

const legends = {
  collection: [
    ['queued', '待采集'], ['active', '采集中'], ['retry', '待重试'],
    ['review', '待核验'], ['done', '已采集'], ['empty', '源返回空'],
  ],
  label: [
    ['queued', '待标注'], ['active', '部分完成'], ['retry', '待重试'],
    ['review', '待复核'], ['done', '已完成'], ['empty', '无资讯可标注'],
  ],
};

let snapshot = null;
let version = null;
let loading = false;
let failures = 0;
let retryAt = 0;
let view = 'collection';
let daysByMonth = new Map();
let month = '';
let selectedDay = '';
let renderedMonth = '';
let renderedInFlight = '';
const dateButtons = new Map();

function verifiedWindows(day) {
  return day.verified_windows ?? (day.complete_windows === day.windows && day.audited_windows === day.windows
    ? day.windows : 0);
}

function collectionState(day, inFlight) {
  if (day.day === inFlight) return {tone: 'active', label: '采集请求处理中'};
  if (day.limited_windows) return {tone: 'retry', label: '限流待重试'};
  if (day.collection_retry_windows) return {tone: 'retry', label: '采集待重试'};
  if (day.verification_retry_windows) return {tone: 'retry', label: '窗口待重新核验'};
  if (day.incomplete_windows || day.complete_windows > verifiedWindows(day)) {
    return {tone: 'review', label: '待核验'};
  }
  if (verifiedWindows(day) === day.windows) {
    return day.returned === 0 ? {tone: 'empty', label: '源返回 0 条'} : {tone: 'done', label: '采集完成'};
  }
  return day.updated ? {tone: 'active', label: '采集未完成'} : {tone: 'queued', label: '待采集'};
}

function labelState(day) {
  if (!day.collected) {
    const empty = day.returned === 0 && verifiedWindows(day) === day.windows;
    return {tone: 'empty', label: empty ? '无资讯可标注' : '尚未入标注队列'};
  }
  if (day.labelled === day.collected) {
    const complete = verifiedWindows(day) === day.windows;
    return {tone: 'done', label: complete ? '标注完成' : '已入库资讯已标注'};
  }
  if (day.label_retry_pending) return {tone: 'retry', label: '标注待重试'};
  if (day.label_review_pending || day.label_missing_body) return {tone: 'review', label: '待复核'};
  return day.labelled ? {tone: 'active', label: '部分已标注'} : {tone: 'queued', label: '待标注'};
}

function inFlightDay(data) {
  return Date.parse(data.collection_inflight_until) > Date.now() ? data.collection_inflight_day || '' : '';
}

function showCollectionCooldown() {
  const status = get('collection-cooldown');
  const deadline = inFlightDay(snapshot) ? NaN : Date.parse(snapshot.collection_retry_at);
  status.hidden = !Number.isFinite(deadline);
  if (status.hidden) return;
  const seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
  const cause = !snapshot.collection_pause_reason || snapshot.collection_pause_reason === 'rate_limited' ? '限流' : '接口';
  status.textContent = seconds
    ? `SXData ${cause}冷却剩余 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
    : 'SXData 冷却已结束，等待采集调度';
}

function showMetrics(data) {
  const end = new Date(dateFromKey(data.end_day_exclusive).getTime() - 86400000).toISOString().slice(0, 10);
  get('scope').textContent = `${data.start_day} 至 ${end} · ${data.total_days} 天 · ${data.source}`;
  get('message').textContent = `更新于 ${dateTime(data.generated_at)} 北京时间`;
  get('queried').textContent = `${fmt(data.audited_days)} / ${fmt(data.total_days)} 天`;
  get('with-news').textContent = `剩余 ${fmt(data.total_days - data.audited_days)} 天`;
  get('collected').textContent = fmt(data.collected);
  get('missing').textContent = data.missing_body ? `${fmt(data.missing_body)} 条缺少正文` : '去重后累计';
  get('labelled').textContent = fmt(data.labelled);
  const rate = data.label_rate;
  const speed = rate?.minutes ? ` · 近 ${rate.minutes} 分钟 ${rateFormat.format(rate.articles / rate.minutes)} 条/分` : '';
  get('pending').textContent = `${fmt(data.collected - data.labelled)} 条未标注${speed}`;
  get('tokens').textContent = fmt(data.usage.total_tokens);
  get('calls').textContent = `tokens · ${fmt(data.usage.calls)} 次调用`;
  const labelCooldown = get('label-cooldown');
  labelCooldown.hidden = !data.label_error;
  labelCooldown.textContent = data.label_error
    ? `MiniMax 等待重试 · ${dateTime(data.label_retry_at)} 后可重试` : '';
  labelCooldown.title = data.label_error || '';
  const archive = data.final_archived_at
    ? `正式归档已校验 · ${dateTime(data.final_archived_at)}`
    : `等待正式归档 · 上次阶段导出 ${dateTime(data.export_at)}`;
  get('sampling').textContent = `最近采集 ${dateTime(data.latest_collection?.checked_at)} · ${archive}`;
}

function showBadge(id, status) {
  const badge = get(id);
  badge.dataset.tone = status.tone;
  badge.textContent = status.label;
}

function showDetailLine(id, parts) {
  const element = get(id);
  element.textContent = parts.join(' · ');
  element.hidden = parts.length === 0;
}

function renderDetail(day, inFlight) {
  get('selected-date').textContent = dateFormat.format(dateFromKey(day.day));
  showBadge('collection-status', collectionState(day, inFlight));
  showBadge('label-status', labelState(day));
  get('collection-summary').textContent = `已核验 ${fmt(verifiedWindows(day))} / ${fmt(day.windows)} 个窗口 · 入库 ${fmt(day.collected)} 条`;
  const sources = [];
  if (day.returned != null) sources.push(`源回包 ${fmt(day.returned)} 条`);
  if (day.occurrences > day.collected) sources.push(`同文不同时间 ${fmt(day.occurrences - day.collected)} 条`);
  if (day.same_time_copies) sources.push(`同一秒重复 ${fmt(day.same_time_copies)} 条`);
  if (day.limited_windows) sources.push(`${fmt(day.limited_windows)} 个窗口限流`);
  const otherRetries = (day.collection_retry_windows || 0) - (day.limited_windows || 0);
  if (otherRetries > 0) sources.push(`${fmt(otherRetries)} 个窗口待重试`);
  if (day.verification_retry_windows) sources.push(`${fmt(day.verification_retry_windows)} 个窗口待重新核验`);
  showDetailLine('collection-detail', sources);

  get('label-summary').textContent = `已标注 ${fmt(day.labelled)} / ${fmt(day.collected)} 条`;
  const labels = [];
  if (day.label_first_pending) labels.push(`${fmt(day.label_first_pending)} 条待首次标注`);
  if (day.label_retry_pending) labels.push(`${fmt(day.label_retry_pending)} 条待重试`);
  if (day.label_review_pending) labels.push(`${fmt(day.label_review_pending)} 条待复核`);
  if (day.label_missing_body) labels.push(`${fmt(day.label_missing_body)} 条缺正文`);
  showDetailLine('label-detail', labels);
}

function renderLegend() {
  const items = legends[view].map(([tone, label]) => {
    const item = document.createElement('li');
    const marker = document.createElement('i');
    marker.dataset.tone = tone;
    marker.setAttribute('aria-hidden', 'true');
    item.append(marker, label);
    return item;
  });
  get('legend').replaceChildren(...items);
  get('show-collection').setAttribute('aria-pressed', String(view === 'collection'));
  get('show-labels').setAttribute('aria-pressed', String(view === 'label'));
}

function renderCalendar() {
  const entries = daysByMonth.get(month);
  const inFlight = inFlightDay(snapshot);
  renderedInFlight = inFlight;
  if (month !== renderedMonth || entries.length !== dateButtons.size || entries.some(day => !dateButtons.has(day.day))) {
    const grid = document.createDocumentFragment();
    dateButtons.clear();
    for (const day of entries) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = String(Number(day.day.slice(-2)));
      button.addEventListener('click', () => {
        selectedDay = day.day;
        renderCalendar();
      });
      dateButtons.set(day.day, button);
      grid.append(button);
    }
    const weekday = (dateFromKey(entries[0].day).getUTCDay() + 6) % 7;
    dateButtons.get(entries[0].day).style.gridColumnStart = String(weekday + 1);
    get('days').replaceChildren(grid);
    renderedMonth = month;
  }

  for (const day of entries) {
    const button = dateButtons.get(day.day);
    const status = view === 'collection' ? collectionState(day, inFlight) : labelState(day);
    button.dataset.tone = status.tone;
    button.title = `${day.day} · ${status.label}`;
    button.setAttribute('aria-label', button.title);
    button.setAttribute('aria-pressed', String(day.day === selectedDay));
    if (day.day === selectedDay) renderDetail(day, inFlight);
  }
  const months = [...daysByMonth.keys()];
  const index = months.indexOf(month);
  get('month').value = month;
  get('previous-month').disabled = index === 0;
  get('next-month').disabled = index === months.length - 1;
}

function selectMonth(next) {
  const days = daysByMonth.get(next);
  if (!days) return;
  month = next;
  selectedDay = days.find(day => day.day.endsWith(selectedDay.slice(-3)))?.day || days[0].day;
  renderCalendar();
}

function selectAdjacentMonth(offset) {
  const months = [...daysByMonth.keys()];
  selectMonth(months[months.indexOf(month) + offset]);
}

function show(data) {
  snapshot = data;
  const grouped = new Map();
  for (const day of [...data.days].sort((a, b) => a.day.localeCompare(b.day))) {
    const key = day.day.slice(0, 7);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(day);
  }
  if ([...daysByMonth.keys()].join() !== [...grouped.keys()].join()) {
    const options = [...grouped.keys()].map(key => {
      const option = document.createElement('option');
      option.value = key;
      option.textContent = monthFormat.format(dateFromKey(key + '-01'));
      return option;
    });
    get('month').replaceChildren(...options);
    renderedMonth = '';
  }
  daysByMonth = grouped;
  if (!daysByMonth.has(month)) {
    const recent = [...grouped.values()].flat().reverse().find(day => day.updated);
    const inFlight = inFlightDay(data);
    selectedDay = data.days.some(day => day.day === inFlight) ? inFlight : recent?.day || data.days[0].day;
    month = selectedDay.slice(0, 7);
  }
  if (!daysByMonth.get(month).some(day => day.day === selectedDay)) {
    renderedMonth = '';
    selectedDay = daysByMonth.get(month)[0].day;
  }
  showMetrics(data);
  showCollectionCooldown();
  renderCalendar();
  get('metrics').hidden = get('coverage').hidden = false;
}

function validSnapshot(data) {
  const dateKey = value => {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const time = dateFromKey(value);
    return Number.isFinite(time.getTime()) && time.toISOString().slice(0, 10) === value;
  };
  return data?.schema_version === 1 && data.usage && Array.isArray(data.days) && data.days.length > 0
    && dateKey(data.start_day) && dateKey(data.end_day_exclusive) && typeof data.generated_at === 'string'
    && Number.isFinite(Date.parse(data.generated_at)) && Number.isInteger(data.total_days)
    && data.total_days === data.days.length && Number.isInteger(data.audited_days)
    && Number.isInteger(data.collected) && Number.isInteger(data.labelled)
    && data.days.every(day => dateKey(day?.day) && day.day >= data.start_day
      && day.day < data.end_day_exclusive && Number.isInteger(day.windows) && day.windows > 0)
    && new Set(data.days.map(day => day.day)).size === data.days.length;
}

async function refresh() {
  if (loading || Date.now() < retryAt) return;
  loading = true;
  let serverDelay = 0;
  try {
    const response = await fetch('../data/training-progress.json', {cache: 'no-cache', signal: AbortSignal.timeout(6000)});
    if (!response.ok) {
      if ([429, 503].includes(response.status)) {
        const value = response.headers.get('retry-after');
        if (value) serverDelay = /^\d+$/.test(value) ? Number(value) * 1000 : Date.parse(value) - Date.now();
      }
      throw new Error('unavailable');
    }
    const etag = response.headers.get('etag');
    if (etag && etag === version) {
      if (failures) get('message').textContent = `更新于 ${dateTime(snapshot.generated_at)} 北京时间`;
    } else {
      const data = await response.json();
      if (!validSnapshot(data) || (snapshot && Date.parse(data.generated_at) < Date.parse(snapshot.generated_at))) {
        throw new Error('invalid');
      }
      show(data);
      version = etag;
    }
    failures = 0;
    retryAt = 0;
  } catch {
    failures = Math.min(5, failures + 1);
    retryAt = Date.now() + Math.max(15000 * 2 ** (failures - 1), Number.isFinite(serverDelay) ? serverDelay : 0);
    get('message').textContent = snapshot
      ? `刷新失败，当前显示 ${dateTime(snapshot.generated_at)} 北京时间的进度，页面会自动重试。`
      : '进度暂不可读取，页面会自动重试。';
  } finally {
    loading = false;
  }
}

for (const [id, phase] of [['show-collection', 'collection'], ['show-labels', 'label']]) {
  get(id).addEventListener('click', () => {
    view = phase;
    renderLegend();
    renderCalendar();
  });
}
get('month').addEventListener('change', event => selectMonth(event.target.value));
get('previous-month').addEventListener('click', () => selectAdjacentMonth(-1));
get('next-month').addEventListener('click', () => selectAdjacentMonth(1));

renderLegend();
refresh();
setInterval(() => {
  if (!snapshot || document.hidden) return;
  showCollectionCooldown();
  if (inFlightDay(snapshot) !== renderedInFlight) renderCalendar();
}, 1000);
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
