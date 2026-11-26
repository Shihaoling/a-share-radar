(() => {
  'use strict';
  const get = id => document.getElementById(id);
  const source = '/data/funds.json';
  const state = { data: null, query: '', kind: 'all', group: 'all', sort: 'aum' };
  const params = new URLSearchParams(location.search);
  if (params.get('sector')) { state.kind = 'sector'; state.group = params.get('sector'); }
  if (params.get('index')) { state.kind = 'index'; state.group = params.get('index'); }

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const fmt = (value, digits = 2) => {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
    return new Intl.NumberFormat('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
  };
  const yi = value => typeof value === 'number' && Number.isFinite(value) ? fmt(value / 1e8, 2) + ' 亿' : '—';
  const signed = (value, digits = 2) => typeof value === 'number' && Number.isFinite(value)
    ? (value > 0 ? '+' : '') + fmt(value, digits) + '%' : '—';
  const stamp = value => {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0, 16);
    return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
  };
  const safeLink = value => {
    try {
      const url = new URL(value, location.href);
      return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
    } catch { return null; }
  };

  function summary() {
    const data = state.data;
    get('funds-time').textContent = stamp(data?.generated_at);
    get('funds-count').textContent = data?.funds?.length ?? '—';
    get('funds-index-count').textContent = data?.indices?.length ?? '—';
    get('funds-sector-count').textContent = data?.sectors?.length ?? '—';
  }

  function groupOptions() {
    const select = get('fund-group');
    select.replaceChildren(el('option', '', '全部'));
    select.firstChild.value = 'all';
    if (!state.data) return;
    const seen = new Map();
    for (const fund of state.data.funds || []) {
      for (const tag of fund.tags || []) {
        if (state.kind !== 'all' && tag.type !== state.kind) continue;
        if (!seen.has(tag.code)) seen.set(tag.code, tag.name || tag.code);
      }
    }
    for (const [code, name] of [...seen].sort((a, b) => a[1].localeCompare(b[1], 'zh-CN'))) {
      const option = el('option', '', `${name} · ${code}`);
      option.value = code;
      select.append(option);
    }
    select.value = seen.has(state.group) ? state.group : 'all';
    if (select.value !== state.group) state.group = 'all';
  }

  function tagsOf(fund) {
    const wrap = el('span', 'fund-tags');
    for (const tag of fund.tags || []) {
      const label = tag.type === 'index' ? '宽基' : '行业';
      wrap.append(el('span', `fund-tag ${tag.type === 'sector' ? 'sector' : ''}`, `${label} · ${tag.name || tag.code}`));
    }
    if (!(fund.tags || []).length) wrap.append(el('span', 'fund-sub', '未分类'));
    return wrap;
  }

  function filtered() {
    const data = state.data?.funds || [];
    const query = state.query.trim().toLowerCase();
    const rows = data.filter(fund => {
      if (state.kind !== 'all' && !(fund.tags || []).some(tag => tag.type === state.kind)) return false;
      if (state.group !== 'all' && !(fund.tags || []).some(tag => tag.code === state.group)) return false;
      if (!query) return true;
      return [fund.code, fund.name, fund.manager, fund.index_code, fund.index_name,
        ...(fund.tags || []).map(tag => tag.name)].some(value => String(value || '').toLowerCase().includes(query));
    });
    const metric = row => ({
      aum: row.aum_cny || 0, pct: row.pct_chg ?? -Infinity,
      turnover: row.turnover_cny || 0, premium: row.premium_pct ?? -Infinity,
      price: row.price || 0,
    }[state.sort] || 0);
    return rows.sort((a, b) => metric(b) - metric(a) || String(a.code).localeCompare(String(b.code)));
  }

  function rowFor(fund) {
    const tr = el('tr');
    tr.tabIndex = 0;
    tr.setAttribute('aria-label', `查看 ${fund.name || fund.code} 详情`);
    const nameCell = el('td');
    nameCell.append(el('div', 'fund-name', fund.name || '—'), el('div', 'fund-code', fund.code || '—'));
    tr.append(nameCell);
    tr.append(el('td', '', ''));
    tr.lastChild.append(tagsOf(fund));
    const quoteCell = el('td', 'fund-value');
    quoteCell.append(el('div', '', fmt(fund.price, 3)));
    const cls = fund.pct_chg > 0 ? 'fund-up' : fund.pct_chg < 0 ? 'fund-down' : 'fund-flat';
    quoteCell.append(el('div', cls, signed(fund.pct_chg)));
    tr.append(quoteCell);
    const navCell = el('td', 'fund-value');
    navCell.append(el('div', '', fmt(fund.nav, 4)));
    navCell.append(el('div', 'fund-sub', fund.nav_date || '净值日期未披露'));
    tr.append(navCell);
    tr.append(el('td', 'fund-value', yi(fund.aum_cny)));
    const premiumCell = el('td', 'fund-value');
    const premiumCls = fund.premium_pct > 0 ? 'fund-up' : fund.premium_pct < 0 ? 'fund-down' : 'fund-flat';
    premiumCell.append(el('div', premiumCls, signed(fund.premium_pct)));
    premiumCell.append(el('div', 'fund-sub', fund.premium_pct === null || fund.premium_pct === undefined ? '价格/净值日期不同' : '同日口径'));
    tr.append(premiumCell);
    const indexCell = el('td');
    indexCell.append(el('div', '', fund.index_name || '—'));
    indexCell.append(el('div', 'fund-sub', fund.index_code || ''));
    tr.append(indexCell);
    tr.append(el('td', '', fund.manager || '—'));
    const dateCell = el('td', 'fund-value');
    dateCell.append(el('div', '', fund.price_date || '—'));
    dateCell.append(el('div', 'fund-sub', fund.stale ? '非最新交易日' : '已更新'));
    tr.append(dateCell);
    const open = () => openDetail(fund);
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } });
    return tr;
  }

  function render() {
    if (!state.data) return;
    groupOptions();
    const rows = filtered();
    const body = get('fund-rows');
    body.replaceChildren(...rows.map(rowFor));
    get('fund-visible-count').textContent = `显示 ${rows.length} / ${state.data.funds?.length || 0} 只基金；点击任意一行查看完整指标。`;
    get('fund-list').hidden = false;
    const notes = get('fund-notes');
    notes.replaceChildren(...(state.data.notes || []).map(note => el('li', '', note)));
  }

  function kv(label, value, cls) {
    const box = el('div', 'fund-kv');
    box.append(el('span', '', label), el('strong', cls || '', value ?? '—'));
    return box;
  }

  function openDetail(fund) {
    const dialog = get('fund-detail');
    const body = get('fund-detail-body');
    get('fund-detail-title').textContent = `${fund.name || '基金'} · ${fund.code || ''}`;
    const grid = el('div', 'fund-detail-grid');
    grid.append(
      kv('场内收盘价', fmt(fund.price, 3)), kv('当日涨跌幅', signed(fund.pct_chg), fund.pct_chg > 0 ? 'fund-up' : fund.pct_chg < 0 ? 'fund-down' : ''),
      kv('单位净值', fmt(fund.nav, 4)), kv('净值日期', fund.nav_date || '—'),
      kv('基金规模', yi(fund.aum_cny)), kv('规模日期', fund.aum_date || '—'),
      kv('基金份额（万份）', fmt(fund.total_share, 0)), kv('成交额', yi(fund.turnover_cny)),
      kv('溢折率', signed(fund.premium_pct), fund.premium_pct > 0 ? 'fund-up' : fund.premium_pct < 0 ? 'fund-down' : ''),
      kv('行情日期', fund.price_date || '—'), kv('跟踪指数', fund.index_name || '—'), kv('指数代码', fund.index_code || '—'),
      kv('基金管理人', fund.manager || '—'), kv('券商关系', fund.broker_relation || '—'),
      kv('成立日期', fund.list_date || '—'), kv('更新状态', fund.stale ? '非最新交易日' : '已更新'));
    body.replaceChildren(grid);
    body.append(el('h3', '', '关联与口径'));
    body.append(el('p', '', fund.match_basis || '—'));
    body.append(el('p', '', `匹配状态：${fund.match_status || '—'}。市场价、净值、规模各自标注日期，单位净值高低不代表估值贵贱。`));
    const links = el('p');
    links.append(document.createTextNode('数据来源：'));
    const hrefs = [fund.source_url, ...Object.values(fund.data_sources || {})].filter(Boolean);
    if (!hrefs.length) links.append(document.createTextNode('站内内置行情接口'));
    hrefs.forEach((href, index) => {
      const safe = safeLink(href);
      if (!safe) return;
      if (index) links.append(document.createTextNode('、'));
      const link = el('a', '', `来源 ${index + 1}`);
      link.href = safe; link.target = '_blank'; link.rel = 'noopener noreferrer';
      links.append(link);
    });
    body.append(links);
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }

  async function load() {
    try {
      const response = await fetch(source, { cache: 'no-store', credentials: 'same-origin' });
      if (!response.ok) throw new Error('status_' + response.status);
      const data = await response.json();
      if (!data || data.schema_version !== 1 || !Array.isArray(data.funds)) throw new Error('schema');
      state.data = data;
      get('fund-status').textContent = `数据更新于 ${stamp(data.generated_at)} · 来源 ${data.provider || '内置行情接口'}`;
      summary();
      render();
    } catch {
      get('fund-status').textContent = '基金快照尚未生成或暂时无法读取。数据由收盘后研究任务写入，请稍后刷新。';
    }
  }

  get('fund-search').addEventListener('input', event => { state.query = event.target.value || ''; render(); });
  get('fund-kind').value = state.kind;
  get('fund-kind').addEventListener('change', event => { state.kind = event.target.value; state.group = 'all'; render(); });
  get('fund-group').addEventListener('change', event => { state.group = event.target.value; render(); });
  get('fund-sort').addEventListener('change', event => { state.sort = event.target.value; render(); });
  get('fund-reset').addEventListener('click', () => {
    state.query = ''; state.kind = 'all'; state.group = 'all'; state.sort = 'aum';
    get('fund-search').value = ''; get('fund-kind').value = 'all'; get('fund-sort').value = 'aum';
    render();
  });
  get('fund-detail-close').addEventListener('click', () => {
    const dialog = get('fund-detail');
    if (typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
  });
  load();
  setInterval(load, 300000);
})();
