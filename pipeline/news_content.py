"""Incremental public article HTML collection; no quote/news API and no bypass."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlsplit, urljoin
import urllib.request

from bs4 import BeautifulSoup
from .calendar import shanghai_now
from .sources import _Gate, SourceUnavailable

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'article-html-v1'
PUBLICATION_VERSION = 'publisher-labelled-time-v2'


def text_hash(text):
    return sha256(text.encode('utf-8')).hexdigest()


def public_url(url):
    p = urlsplit(str(url or ''))
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
        raise SourceUnavailable('invalid_public_url')
    addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80))
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise SourceUnavailable('non_public_address')
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _Redirect(Exception):
    def __init__(self, location): self.location = location


class ArticleGate(_Gate):
    def get(self, url):
        for _ in range(3):
            public_url(url)
            self.permitted(url)
            try:
                body, headers = self._raw(url)
                return body, {**headers, 'Article-Final-URL': url}
            except _Redirect as e:
                target = urljoin(url, e.location)
                if urlsplit(url).scheme == 'https' and urlsplit(target).scheme != 'https':
                    raise SourceUnavailable('redirect_not_followed')
                # New origin gets its own robots check and public-address check.
                url = target
        raise SourceUnavailable('redirect_not_followed')

    def _raw_unlocked(self, url, headers=None):
        public_url(url)
        domain = self._before(url)
        req = urllib.request.Request(url, headers={**(headers or {}), 'User-Agent': self.cfg['user_agent']})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=self.timeout()) as r:
                self._status(r.status, r.headers, domain)
                body = r.read(int(self.cfg['max_response_bytes']) + 1)
                if len(body) > int(self.cfg['max_response_bytes']):
                    raise SourceUnavailable('response_too_large')
                return body, dict(r.headers)
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400 and e.headers.get('Location'):
                raise _Redirect(e.headers['Location']) from None
            self._status(e.code, e.headers, domain)
            raise SourceUnavailable('http_error') from None


def extract_article(raw, headers=None):
    """Use explicit article containers/JSON-LD, never whole page navigation text."""
    soup = BeautifulSoup(raw, 'html.parser')
    plain = soup.get_text(' ', strip=True)
    if any(x in plain[:2000].lower() for x in ('verify you are human', 'access denied', '安全验证', '访问过于频繁', '请输入验证码')):
        raise SourceUnavailable('access_challenge')
    if soup.select_one('[class*="paywall"], [id*="paywall"]') or re.search(r'订阅后(?:阅读|查看)|登录后(?:阅读|查看)全文|subscribe to (?:continue|read)', plain, re.I):
        raise SourceUnavailable('restricted_article')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        queue = payload if isinstance(payload, list) else [payload]
        while queue:
            item = queue.pop(0)
            if not isinstance(item, dict):
                continue
            queue.extend(item.get('@graph', []) if isinstance(item.get('@graph'), list) else [])
            body = item.get('articleBody')
            if isinstance(body, str) and len(body.strip()) >= 100:
                return BeautifulSoup(body, 'html.parser').get_text('\n', strip=True), 'jsonld_articleBody'
    for node in soup.select('script,style,nav,header,footer,aside,form,button,.advertisement,.related-news,.recommend'):
        node.decompose()
    selectors = ['#ContentBody', '#artibody', '#articletxt', '.article-content', '.article_content',
                 '.article-body', '.article_body', '#articleContent', '#contentArea', '#article', '.press-release',
                 '[itemprop="articleBody"]', 'article', 'main']
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = node.get_text('\n', strip=True)
            if len(text) >= 100:
                if selector == 'main' and sum(len(a.get_text()) for a in node.select('a')) > len(text)*.3:
                    continue
                if node.select_one('.pagination, .article-page'):
                    raise SourceUnavailable('multipage_article_requires_review')
                return text, 'html:' + selector
    raise SourceUnavailable('article_body_not_found')


def publication_metadata(raw, url, now=None):
    """Only publisher-labelled dates count; URL dates and article prose do not."""
    now=shanghai_now(now);soup=BeautifulSoup(raw,'html.parser');candidates=[]
    for selector,attr in [('meta[property="article:published_time"]','content'),('meta[name="pubdate"]','content'),('meta[name="publishdate"]','content'),('meta[itemprop="datePublished"]','content'),('time[datetime]','datetime')]:
        candidates.extend((selector,n.get(attr,'')) for n in soup.select(selector))
    for script in soup.select('script[type="application/ld+json"]'):
        try: queue=json.loads(script.string or script.get_text())
        except (ValueError,TypeError):continue
        queue=queue if isinstance(queue,list) else [queue]
        while queue:
            item=queue.pop(0)
            if not isinstance(item,dict):continue
            if isinstance(item.get('@graph'),list):queue.extend(item['@graph'])
            if item.get('datePublished'):candidates.append(('jsonld:datePublished',str(item['datePublished'])))
    for selector in ['#pubtime','.publish-time','.pubtime','.time','.infos > .item','[itemprop="datePublished"]']:
        candidates.extend((selector,n.get_text(' ',strip=True)) for n in soup.select(selector))
    valid=[]
    for method,value in candidates:
        value=str(value).strip()
        try: at=datetime.fromisoformat(value.replace('Z','+00:00')) if ':' in value else None
        except ValueError:at=None
        if at is None:
            m=re.search(r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?',value)
            if m:
                try:at=datetime(*[int(x or 0) for x in m.groups()])
                except ValueError:pass
        if at is not None:
            at=shanghai_now(at)
            if at<=now and at.year>=2000:valid.append((at,method,value[:160]))
    receipt={'status':'unknown','parser_version':PUBLICATION_VERSION,'source_url':url,'checked_at':now.isoformat()}
    if not valid:return {'publication_verification':receipt}
    if max(v[0] for v in valid)-min(v[0] for v in valid)>timedelta(minutes=5):
        receipt['status']='conflict';return {'publication_verification':receipt}
    at,method,value=valid[0]
    receipt.update(status='verified',method=method,source_value=value)
    return {'published_at':at.isoformat(),'published_at_verified_at':now.isoformat(),'publication_verification':receipt}


def enrich_full_text(dashboard, config=None, now=None, root=None, gate=None):
    now = shanghai_now(now); cfg = (config or {}).get('news_content', {})
    result = deepcopy(dashboard)
    root = Path(root) if root else ROOT/'data/news_content'; root.mkdir(parents=True, exist_ok=True)
    path = root/'cache.json'
    try: state = json.loads(path.read_text())
    except (OSError, ValueError): state = {'articles': {}, 'hosts': {}}
    gate_cfg = {'user_agent': 'AshareRadar/1.0 (+https://shihaoling.github.io/a-share-radar/)',
                'respect_robots': True, 'max_retries': 0, 'min_request_interval_seconds': max(4,float(cfg.get('request_interval_seconds',20))),
                'request_budget_seconds': 240, 'max_http_requests': 64, 'max_response_bytes': 4000000, 'timeout_seconds': 15}
    gate = gate or ArticleGate(gate_cfg)
    enabled = (config or {}).get('network_enabled', False) and cfg.get('enabled', True) and not cfg.get('reuse_only', False)
    # A content-only extraction failure is local to that URL, not an IP block.
    for host,until in list(state['hosts'].items()):
        causes=[r for r in state['articles'].values() if urlsplit(str(r.get('url',''))).hostname==host and r.get('retry_after')==until]
        if causes and all(r.get('reason') in ('article_body_not_found','restricted_article','multipage_article_requires_review') for r in causes):
            state['hosts'].pop(host,None)
    attempted = 0; hosts = {}; reused = 0
    for n in sorted(result.get('news', []), key=lambda x: x.get('published_at') or x.get('first_seen_at') or '', reverse=True):
        if n.get('primary_shadowed'):continue
        if n.get('provider')=='stock_api' and n.get('full_text_method')=='provider_news_text' and n.get('full_text_status')=='complete' and n.get('full_text'):
            reused+=1;continue
        url = n.get('url') or n.get('original_url')
        parsed = urlsplit(str(url or ''))
        if parsed.scheme == 'http' and parsed.hostname == 'finance.eastmoney.com':
            n.setdefault('original_url', url)
            url = 'https' + str(url)[4:]
            n['url'] = url
        key = text_hash(str(url or n.get('id')))
        row = state['articles'].get(key, {})
        if row.get('url') != url:
            row = {}
        metadata_pending=cfg.get('verify_publication_times') and row.get('publication_verification',{}).get('parser_version')!=PUBLICATION_VERSION
        if row.get('status') == 'complete' and not metadata_pending:
            reused += 1
        elif enabled and url and attempted < int(cfg.get('max_articles_per_run', 20)):
            host = urlsplit(str(url)).hostname or ''
            until = state['hosts'].get(host, '')
            if until > now.isoformat() or row.get('retry_after', '') > now.isoformat() or hosts.get(host, 0) >= int(cfg.get('max_articles_per_host', 3)):
                n['full_text_status'] = row.get('status', 'deferred'); continue
            attempted += 1; hosts[host] = hosts.get(host, 0) + 1
            try:
                raw, headers = gate.get(url)
                text, method = extract_article(raw, headers)
                row = {'url': url, 'status': 'complete', 'full_text': text, 'text_hash': text_hash(text),
                       'fetched_at': shanghai_now().isoformat(), 'extraction_method': method, 'collector_version': VERSION,
                       'resolved_url': headers.get('Article-Final-URL', url)}
                row.update(publication_metadata(raw,row['resolved_url']))
            except Exception as e:
                # Fixed error code only. Never retain a request/response/exception
                # string that could contain credentials or entire source pages.
                if hasattr(gate, 'started') and (time.monotonic()-gate.started >= gate_cfg['request_budget_seconds'] or gate.count >= gate_cfg['max_http_requests']):
                    n['full_text_status']='deferred'
                    break
                retry = max(1800, int(getattr(e, 'retry_seconds', 86400)))
                reason = str(e) if type(e) is SourceUnavailable else type(e).__name__
                allowed = {'article_body_not_found', 'access_challenge', 'restricted_article', 'redirect_not_followed', 'multipage_article_requires_review', 'non_public_address', 'invalid_public_url', 'response_too_large'}
                row = {'url': url, 'status': 'unavailable', 'reason': reason if reason in allowed else 'source_unavailable',
                       'attempted_at': now.isoformat(), 'retry_after': (now + timedelta(seconds=retry)).isoformat()}
                if row['reason'] not in ('article_body_not_found','restricted_article','multipage_article_requires_review'):
                    state['hosts'][host] = row['retry_after']
            state['articles'][key] = row
        n['full_text_status'] = row.get('status') or ('no_article_url' if not url else 'deferred')
        if row.get('status') == 'complete':
            n.update(full_text=row['full_text'], full_text_hash=row['text_hash'], full_text_fetched_at=row['fetched_at'],
                     full_text_method=row['extraction_method'])
            for field in ('published_at','published_at_verified_at','publication_verification'):
                if row.get(field):n[field]=deepcopy(row[field])
        elif row:
            n['full_text_reason'] = row.get('reason')
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    result['news_content_summary'] = {'attempted': attempted, 'reused': reused,
        'complete': sum(n.get('full_text_status') == 'complete' for n in result.get('news', [])),
        'total': len(result.get('news', [])), 'as_of': now.isoformat(), 'status': 'ready' if enabled else 'cache_only'}
    return result
