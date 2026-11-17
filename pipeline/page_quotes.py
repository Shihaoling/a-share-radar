"""Read Eastmoney's rendered public tables, never call a quote API.

python -m pipeline.page_quotes --once [--inspect-closed]
The Actions schedule runs this every ~5 minutes. A local daemon uses --interval 300.
"""
from argparse import ArgumentParser
from datetime import datetime, timedelta, time as clock
from pathlib import Path
import json
import math
import re
import time
import sys
from .calendar import shanghai_now, is_trading_day
from .sources import _Gate, SourceUnavailable

ROOT = Path(__file__).resolve().parents[1]
URLS = {'index': 'https://quote.eastmoney.com/center/hszs.html',
        'sector': 'https://quote.eastmoney.com/center/gridlist.html#industry_board'}
INDEX_CODES = {'000001':'sh000001','399001':'sz399001','399006':'sz399006',
               '000300':'sh000300','000688':'sh000688','000905':'sh000905'}
UA = 'AshareRadar/1.0 (personal research; rendered-page collector)'
# DOM extraction only; no network response interception or application-state reads.
READ_ROWS = r"""() => Array.from(document.querySelectorAll('table')).flatMap(t => {
 const rows=Array.from(t.querySelectorAll('tr')); if(!rows.length)return[];
 const headers=Array.from(rows[0].querySelectorAll('th,td')).map(c=>c.innerText.replace(/\s/g,''));
 return rows.slice(1).map(r=>({headers,cells:Array.from(r.querySelectorAll('td')).map(c=>c.innerText.trim()),
   links:Array.from(r.querySelectorAll('a[href]')).map(a=>a.getAttribute('href'))}));
})"""


def collection_open(now):
    now = shanghai_now(now)
    t = now.time().replace(tzinfo=None)
    return is_trading_day(now.date()) is True and (clock(9,30) <= t <= clock(11,35) or clock(13) <= t <= clock(15,5))


def number(value):
    try:
        v = float(str(value).replace(',', '').replace('%','').replace('−','-'))
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def parse_rows(rows, kind, now):
    result, seen = [], set()
    for row in rows:
        heads, cells = row.get('headers', []), row.get('cells', [])
        def cell(*names):
            for name in names:
                if name in heads and len(cells) > heads.index(name):
                    return cells[heads.index(name)].strip()
            return None
        code = cell('代码') if kind == 'index' else None
        if kind == 'sector':
            match = re.search(r'(?:90[./]|/)(BK\d{4})(?:[./?#]|$)', ' '.join(row.get('links', [])))
            code = match.group(1) if match else None
        identity = INDEX_CODES.get(code) if kind == 'index' else 'em:'+code if code else None
        price, change = number(cell('最新价','最新')), number(cell('涨跌幅'))
        name = cell('板块名称','名称','指数名称')
        if not identity or identity in seen or not name or price is None or price <= 0:
            continue
        stamp = shanghai_now(now).isoformat(timespec='seconds')
        result.append(dict(id=identity, code=code, name=name, kind=kind,
            taxonomy='EASTMONEY' if kind == 'sector' else 'INDEX', price=price,
            change_pct=change, previous_close=number(cell('昨收')), quote_timestamp=None,
            observed_at=stamp, as_of=stamp, fetched_at=stamp, timestamp_quality='page_observed',
            provider='eastmoney_page', source_url=URLS[kind]))
        seen.add(identity)
    return result


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def merge_snapshot(old, quotes, status, now, sampled=True):
    """Keep the actual sample day, keep failures visible, never manufacture ticks."""
    stamp = shanghai_now(now).isoformat(timespec='seconds')
    day = stamp[:10]
    if not quotes:
        return {**old, 'schema_version':1, 'collector':status, 'checked_at':stamp}
    same_day = old.get('trade_date') == day
    buckets = old.get('intraday', {}) if same_day else {}
    latest = {q['id']:q for key in ('market','sectors') for q in old.get(key, [])} if same_day else {}
    for q in quotes:
        latest[q['id']] = q
        # A diagnostic after close validates the scraper, not an intraday sample.
        if sampled:
            bucket = buckets.setdefault(q['id'], {'points':[]})
            rows = {p['observed_at']:p for p in bucket.get('points', [])}
            rows[q['observed_at']] = q
            bucket['points'] = sorted(rows.values(), key=lambda p:p['observed_at'])[-300:]
    return dict(schema_version=1, generated_at=stamp, checked_at=stamp, trade_date=day,
        collector=status, market=[q for q in latest.values() if q['kind']=='index'],
        sectors=[q for q in latest.values() if q['kind']=='sector'], intraday=buckets)


class Collector:
    def __init__(self, output):
        self.output = Path(output)

    def run_once(self, now=None, inspect_closed=False):
        now = shanghai_now(now)
        old = load(self.output)
        prior = old.get('collector', {})
        until = prior.get('retry_at')
        if until and datetime.fromisoformat(until) > now:
            return old
        if not collection_open(now) and not inspect_closed:
            return old  # Keep the last trading day's genuine timestamps.
        # Use the OS trust store on desktop platforms; certificate checks stay on.
        if sys.platform in ('darwin', 'win32'):
            import truststore
            truststore.inject_into_ssl()
        gate = _Gate(dict(user_agent=UA, respect_robots=True, max_http_requests=4,
            request_budget_seconds=90, min_request_interval_seconds=2,
            timeout_seconds=15, max_response_bytes=1000000, max_retries=0))
        quotes, errors, coverage = [], [], {}
        robots_checked = False
        retry_seconds = 0
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=UA, locale='zh-CN', service_workers='block')
                try:
                    for kind, url in URLS.items():
                        page = None
                        try:
                            gate.permitted(url)
                            robots_checked = True
                            page = context.new_page()
                            # Browser loads normal page resources. We never construct/call
                            # the page's underlying JSON endpoints or inspect their responses.
                            denied = []
                            page.on('response', lambda r: denied.append(r.status) if r.status in (401,403,429) and 'eastmoney.com' in r.url else None)
                            response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
                            if not response or response.status >= 400:
                                raise SourceUnavailable('Page navigation failed')
                            page.wait_for_function("() => Array.from(document.querySelectorAll('table td')).some(c => /^[-+]?\\d+\\.\\d+%?$/.test(c.innerText.trim()))", timeout=25000)
                            rows = page.evaluate(READ_ROWS)
                            if denied:
                                raise SourceUnavailable('网页资源拒绝访问，暂停此来源')
                            parsed = parse_rows(rows, kind, shanghai_now())
                            if not parsed:
                                raise SourceUnavailable('Rendered table missing or changed')
                            quotes.extend(parsed)
                            coverage[kind] = {'pages':1, 'rows':len(parsed), 'complete':kind=='index' and len(parsed)==6}
                            # Industry list is paginated. Follow visible next-page controls,
                            # bounded to 2 pages; lack of coverage stays explicit.
                            if kind == 'sector':
                                page_numbers = page.locator('a').evaluate_all("els => [...new Set(els.filter(e=>e.offsetWidth>0 && /^[1-9]\\d?$/.test(e.innerText.trim())).map(e=>Number(e.innerText.trim())))].filter(n=>n<=20).sort((a,b)=>a-b)")
                                targets = [n for n in page_numbers if n > 1]
                                coverage[kind]['available_pages'] = max(page_numbers, default=1)
                                coverage[kind]['complete'] = False
                                if targets:
                                    cursor = int(prior.get('page_cursor', 0)) % len(targets)
                                    target = targets[cursor]
                                    link = page.get_by_role('link', name=str(target), exact=True)
                                    if link.count() == 1:
                                        # One deliberate click per round, after a fixed gap.
                                        page.wait_for_timeout(3000)
                                        first = parsed[0]['id']
                                        link.click(timeout=5000)
                                        page.wait_for_function("first => {const a=Array.from(document.querySelectorAll('table a[href]')).map(a=>a.getAttribute('href'));return a.some(h=>/BK[0-9]{4}/.test(h))&&!a.some(h=>h.includes(first));}", arg=first.removeprefix('em:'), timeout=12000)
                                        parsed = parse_rows(page.evaluate(READ_ROWS), kind, shanghai_now())
                                        if parsed and parsed[0]['id'] != first:
                                            quotes.extend(parsed)
                                            coverage[kind].update(pages=2, rows=coverage[kind]['rows']+len(parsed), extra_page=target)
                                        prior['page_cursor'] = cursor+1
                        except Exception as exc:
                            errors.append({'kind':kind, 'error':type(exc).__name__, 'reason':str(exc)[:160] if isinstance(exc, SourceUnavailable) else '页面读取失败'})
                            retry_seconds = max(retry_seconds, 1800, getattr(exc, 'retry_seconds', 1800))
                            if not robots_checked or isinstance(exc, SourceUnavailable):
                                break
                        finally:
                            if page:
                                page.close()
                finally:
                    context.close()
                    browser.close()
        except Exception as exc:
            errors.append({'kind':'browser', 'error':type(exc).__name__})
            retry_seconds = max(retry_seconds, 1800)
        status = dict(status='partial' if quotes and errors else 'error' if errors else 'ok',
            checked_at=shanghai_now().isoformat(), last_success=shanghai_now().isoformat() if quotes else prior.get('last_success'),
            interval_seconds=300, method='rendered_html', errors=errors, coverage=coverage,
            retry_at=(now+timedelta(seconds=retry_seconds)).isoformat() if errors else None,
            page_cursor=prior.get('page_cursor', 0),
            diagnostic_only=inspect_closed and not collection_open(now))
        result = merge_snapshot(old, quotes, status, shanghai_now(), sampled=collection_open(now))
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temp = self.output.with_suffix('.tmp')
        temp.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False)+'\n', encoding='utf-8')
        temp.replace(self.output)
        return result


def main(argv=None):
    p = ArgumentParser(description=__doc__)
    p.add_argument('--output', default=str(ROOT/'web/data/page-quotes.json'))
    p.add_argument('--once', action='store_true')
    p.add_argument('--inspect-closed', action='store_true', help='Check closed-market DOM; never add intraday samples')
    p.add_argument('--interval', type=int, default=300)
    args = p.parse_args(argv)
    if args.interval < 300:
        p.error('interval must be at least 300 seconds')
    collector = Collector(args.output)
    while True:
        started = time.monotonic()
        result = collector.run_once(inspect_closed=args.inspect_closed)
        print(json.dumps(result.get('collector', {'status':'market_closed'}), ensure_ascii=False), flush=True)
        if args.once:
            return 0
        time.sleep(max(0, args.interval-(time.monotonic()-started)))

if __name__ == '__main__':
    raise SystemExit(main())
