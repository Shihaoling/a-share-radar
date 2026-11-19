'use strict';

const sectorPattern = /^\d{6}(?:\.(?:SH|SZ|SI)|R?\.TI)$/;
const datePattern = /^\d{4}-\d{2}-\d{2}$/;
const sectorKinds = {industry:'行业指数', theme:'主题指数', I:'同花顺行业', N:'同花顺概念', TH:'同花顺主题', L1:'申万一级行业'};
const memberMethods = {ths_member:'同花顺板块成分', index_weight:'指数成分及权重', index_member_all:'申万行业成分'};
const pageSize = 30;
const cnTime = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit',
  day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
const displayNumber = new Intl.NumberFormat('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2});

function observationTime(stamp) {
  if (typeof stamp !== 'string' || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d\d:\d\d)$/.test(stamp)) return '';
  const moment = new Date(stamp);
  return Number.isFinite(moment.getTime()) ? cnTime.format(moment).replaceAll('/', '-') : '';
}

function findEntry(catalog, code) {
  if (catalog?.schema_version !== 1 || !sectorPattern.test(code)) return null;
  const rows = code.endsWith('.SI') ? catalog.sw_items : catalog.items;
  return Array.isArray(rows) ? rows.find(item => item?.code === code && typeof item.name === 'string' && Object.hasOwn(sectorKinds, item.kind)) : null;
}

function sourceRows(code, feeds) {
  // A price and a percentage may have different source times (e.g. a hot-list row with no index level).
  const quotes = [];
  const add = (row, day, stamp, label, close = false) => {
    if (!row || !datePattern.test(day)) return;
    const time = close ? day + ' 收盘' : observationTime(stamp);
    if (!close && (!time || time.slice(0, 10) !== day)) return;
    const order = Date.parse(close ? day + 'T15:00:00+08:00' : stamp);
    quotes.push({price:row.price, change:row.change_pct, order, caption:label + ' · ' + time, close});
  };
  if (code.endsWith('.TI')) {
    const ref = feeds.reference?.quotes?.[code];
    add(ref, ref?.quote_date, null, '同花顺', true);
    const hot = Array.isArray(feeds.hot?.items) && feeds.hot.items.find(row => row?.code === code);
    add(hot, feeds.hot?.trade_date, feeds.hot?.rank_time, '概念热榜');
  } else if (code.endsWith('.SI')) {
    const history = feeds.daily?.bars?.[code];
    const last = history && Object.keys(history).filter(day => datePattern.test(day)).sort().at(-1);
    const closing = history?.[last];
    if (closing) add({price:closing.close, change_pct:closing.change_pct}, last, null, '申万行业', true);
    const estimates = feeds.stocks?.board_estimates;
    const estimate = Array.isArray(estimates) && estimates.find(row => row?.id === code);
    add(estimate, estimate?.quote_date, estimate?.quote_timestamp, '权重股估算');
  } else {
    const sectors = feeds.exchange?.sectors;
    const quote = Array.isArray(sectors) && sectors.find(row => row?.id === code);
    add(quote, quote?.quote_date, quote?.quote_timestamp, '交易所指数');
  }
  return quotes.sort((a,b) => b.order - a.order || Number(b.close) - Number(a.close));
}

function memberPage(rows, search, number) {
  const query = search.trim().toUpperCase();
  const filtered = rows.filter(row => !query || row.code.toUpperCase().includes(query) || String(row.name || '').toUpperCase().includes(query));
  const count = filtered.length, pages = Math.max(1, Math.ceil(count / pageSize));
  const current = Math.min(Math.max(1, number), pages);
  return {count, pages, current, rows:filtered.slice((current - 1) * pageSize, current * pageSize)};
}

function memberDates(data) {
  const parts = [memberMethods[data.method]].filter(Boolean);
  if (datePattern.test(data.as_of)) parts.push('名单日期 ' + data.as_of);
  else if (data.method === 'ths_member' && data.status !== 'pending') parts.push('来源未提供成分生效日');
  const checked = observationTime(data.checked_at);
  if (checked) parts.push('核验时间 ' + checked);
  return parts.join(' · ');
}

async function readFile(name) {
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 10000);
  try {
    const response = await fetch('../data/' + name + '.json', {cache:'no-cache', signal:abort.signal});
    if (!response.ok) throw new Error(String(response.status));
    return await response.json();
  } finally { clearTimeout(timeout); }
}

async function openDetail() {
  const id = key => document.getElementById(key);
  const code = (new URLSearchParams(location.search).get('code') || '').toUpperCase();
  if (!sectorPattern.test(code)) {
    id('catalog-status').textContent = '板块代码无效，请从市场总览选择板块。';
    return;
  }
  const ths = code.endsWith('.TI'), sw = code.endsWith('.SI');
  let entry;
  try { entry = findEntry(await readFile(ths || sw ? 'ths-sector-catalog' : 'sector-catalog'), code); }
  catch {
    id('catalog-status').textContent = '板块目录暂时无法读取，稍后自动重试。';
    setTimeout(openDetail, 30000);
    return;
  }
  if (!entry) {
    id('catalog-status').textContent = '未在行情目录中找到 ' + code + '。';
    return;
  }
  document.title = entry.name + ' · 板块详情 · 观澜';
  id('sector-title').textContent = entry.name;
  id('sector-meta').textContent = [sectorKinds[entry.kind], code].join(' · ');
  id('sector-intro').textContent = [entry.fullname !== entry.name && entry.fullname, entry.intro].filter(Boolean).join(' · ');
  id('catalog-status').textContent = '';
  id('quote-strip').hidden = false;
  id('members').hidden = false;
  id('research').hidden = false;
  if (typeof RadarResearch !== 'undefined') RadarResearch.mount('sector', code);
  else id('research-status').textContent = '研究指标组件暂时无法读取。';

  const feedFiles = ths ? [['hot','sector-hot','sxdata_ths_hot'], ['reference','sector-reference','sxdata_sector_reference']] :
    sw ? [['daily','daily-market','sxdata_daily'], ['stocks','stock-quotes','sxdata_relay']] :
      [['exchange','sector-quotes','sxdata_relay']];
  const fields = {};
  async function refreshQuotes() {
    if (!document.hidden) {
      const feeds = {};
      await Promise.all(feedFiles.map(async ([key, file, source]) => {
        try {
          const data = await readFile(file);
          if (data?.schema_version === 1 && data.source === source) feeds[key] = data;
        } catch { /* Each published source recovers independently. */ }
      }));
      const candidates = sourceRows(code, feeds);
      for (const key of ['price','change']) {
        const candidate = candidates.find(row => Number.isFinite(row[key]) && (key !== 'price' || row.price > 0));
        const prior = fields[key];
        if (candidate && (!prior || candidate.order > prior.order || (candidate.order === prior.order && (candidate.close || !prior.close)))) fields[key] = candidate;
        const row = fields[key], node = id('quote-' + key);
        if (row) {
          node.textContent = (key === 'change' && row.change > 0 ? '+' : '') + displayNumber.format(row[key]) + (key === 'change' ? '%' : '');
          node.className = key === 'change' ? (row.change > 0 ? 'up' : row.change < 0 ? 'down' : '') : '';
        }
        id('quote-' + key + '-time').textContent = row ? row.caption : '等待来源更新';
      }
    }
    setTimeout(refreshQuotes, ths || sw ? 60000 : 20000);
  }

  let data = null, page = 1, notice = '', retry = 15000;
  function renderMembers() {
    const all = data?.members || [];
    const result = memberPage(all, id('member-search').value, page);
    page = result.current;
    const expected = data?.expected_count ?? entry.member_count;
    id('coverage').textContent = data?.status === 'ready' ? '共 ' + all.length + ' 只成分股' :
      ['已收录 ' + all.length + ' 只', Number.isInteger(expected) && expected > 0 ? '目录记录 ' + expected + ' 只' : ''].filter(Boolean).join(' · ');
    id('member-source').textContent = data ? memberDates(data) : '';
    const message = all.length && !result.count ? '没有匹配的成分股。' : '';
    id('member-status').textContent = [notice, message].filter(Boolean).join(' ');
    const contents = document.createDocumentFragment();
    for (const row of result.rows) {
      const tr = document.createElement('tr');
      for (const [index, value] of [row.name || '—', row.code, Number.isFinite(row.weight_pct) ? String(row.weight_pct) + '%' : '—'].entries()) {
        const cell = document.createElement('td');
        if (index === 2) cell.className = 'numeric';
        if (index === 0 && /^(?:\d{6}\.(?:SH|SZ|BJ|NQ)|\d{5,6}\.HK)$/.test(row.code)) {
          const link = document.createElement('a');
          link.className = 'member-link';
          link.href = '../asset/?type=stock&code=' + encodeURIComponent(row.code);
          link.textContent = row.name || row.code;
          link.setAttribute('aria-label', '查看' + link.textContent + '的研究指标');
          cell.append(link);
        } else cell.textContent = value;
        tr.append(cell);
      }
      contents.append(tr);
    }
    id('member-rows').replaceChildren(contents);
    id('table-wrap').hidden = !result.count;
    id('pager').hidden = result.pages <= 1;
    id('page-info').textContent = '第 ' + page + ' / ' + result.pages + ' 页 · 共 ' + result.count + ' 只';
    id('prev').disabled = page <= 1;
    id('next').disabled = page >= result.pages;
  }

  async function refreshMembers() {
    if (document.hidden) { setTimeout(refreshMembers, 15000); return; }
    try {
      const received = await readFile('sector-members/' + code);
      if (received?.schema_version !== 1 || received.source !== 'sxdata_sector_members' || received.code !== code ||
          !['ready','partial','pending'].includes(received.status) || !Array.isArray(received.members)) throw new Error('schema');
      if (received.members.some(row => !row || typeof row.code !== 'string' || !row.code)) throw new Error('schema');
      if (!data || received.status === 'ready' || received.members.length >= data.members.length) data = received;
      notice = received.status === 'ready' ? (received.members.length ? '' : '来源返回的名单为空。') :
        received.reason === 'no_rows' ? '本次核验时来源未返回成分名单，系统会按计划再次查询。' :
        received.status === 'partial' ? '当前显示已取得的名单，余下成分正在补采。' : '成分名单正在补采，页面会自动更新。';
      renderMembers();
      if (received.status === 'ready') {
        setTimeout(refreshMembers, 30 * 60 * 1000);
        return;
      }
    } catch (error) {
      notice = error.message === '404' ? '成分名单正在补采，页面会自动更新。' :
        '成分名单暂时无法更新，稍后自动重试。';
      renderMembers();
    }
    setTimeout(refreshMembers, retry);
    retry = Math.min(60000, retry * 2);
  }
  id('member-search').addEventListener('input', () => { page = 1; renderMembers(); });
  id('prev').addEventListener('click', () => { page--; renderMembers(); });
  id('next').addEventListener('click', () => { page++; renderMembers(); });
  void refreshMembers();
  void refreshQuotes();
}

if (typeof document !== 'undefined') void openDetail();
