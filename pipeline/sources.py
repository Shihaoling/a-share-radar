"""Low-frequency public-source adapters. No fabricated quotes or news.

fetch_data(previous, config, now) is synchronous. AKShare's requests transport is
guarded while an adapter runs; do not run unrelated requests threads alongside it.
All amounts are CNY, returns are percentage points, and quote times from sources
without a timestamp are explicitly labelled as retrieval times.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
_TRANSPORT_LOCK = threading.RLock()


def _dt(value=None):
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = parsedate_to_datetime(str(value))
            except (TypeError, ValueError):
                return None
    else:
        return datetime.now(SHANGHAI)
    return dt.replace(tzinfo=SHANGHAI) if dt.tzinfo is None else dt.astimezone(SHANGHAI)


def _iso(value):
    dt = _dt(value)
    return dt.isoformat(timespec="seconds") if dt else None


def _number(value):
    try:
        val = float(str(value).replace(",", "").replace("%", ""))
        return val if math.isfinite(val) else None
    except (ValueError, TypeError):
        return None


def _text(value, limit=1200):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", str(value or ""))).strip()[:limit]


def _get(record, *keys):
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _records(frame):
    if isinstance(frame, list):
        return frame
    return frame.to_dict(orient="records")


def _validated_records(frame, required):
    records = _records(frame)
    if not records:
        raise SourceUnavailable("来源返回空行情，保留上一份缓存")
    if not all(key in records[0] for key in required):
        raise SourceUnavailable("来源字段变更，缺少 " + "/".join(key for key in required if key not in records[0]))
    return records


def _merge(a, b):
    out = copy.deepcopy(a)
    for key, val in (b or {}).items():
        out[key] = _merge(out[key], val) if isinstance(val, dict) and isinstance(out.get(key), dict) else val
    return out


def _incremental_start(item, default_start, end, overlap_days=7):
    """Request a small correction overlap while retaining every cached bar."""
    valid = []
    for row in item.get("history", []):
        try:
            day = datetime.fromisoformat(str(row.get("date", ""))[:10]).date()
        except (ValueError, TypeError):
            continue
        if day <= end:
            valid.append(day)
    return max(valid) - timedelta(days=max(0, int(overlap_days))) if valid else default_start


class SourceUnavailable(RuntimeError):
    pass


class RateLimited(SourceUnavailable):
    def __init__(self, message, retry_seconds=900):
        super().__init__(message)
        self.retry_seconds = max(30, float(retry_seconds))


class _Gate:
    """Counts actual requests, including robots and AKShare pagination."""
    def __init__(self, config):
        self.cfg = config
        self.started = time.monotonic()
        self.count = 0
        self.last = {}
        self.robots = {}
        self.blocked = set()
        self.send_lock = threading.RLock()

    @staticmethod
    def _host(url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise SourceUnavailable("只允许公开 HTTP(S) 数据来源")
        return parsed.hostname.lower()

    def _before(self, url):
        host = self._host(url)
        domain = "eastmoney.com" if host.endswith(".eastmoney.com") else host
        if domain in self.blocked:
            raise SourceUnavailable("该来源本轮已熔断，等待下次调度")
        elapsed = time.monotonic() - self.started
        if self.count >= int(self.cfg.get("max_http_requests", 48)) or elapsed >= float(self.cfg.get("request_budget_seconds", 100)):
            raise SourceUnavailable("达到本轮请求/时间预算，保留缓存待下次增量采集")
        jitter = random.uniform(0, min(0.8, max(0, float(self.cfg.get("request_jitter_seconds", 0.8)))))
        delay = float(self.cfg.get("min_request_interval_seconds", 2)) + jitter - (time.monotonic() - self.last.get(domain, 0))
        if delay > 0:
            if elapsed + delay > float(self.cfg.get("request_budget_seconds", 100)):
                raise SourceUnavailable("本轮时间预算不足")
            time.sleep(min(delay, 10))
        self.count += 1
        self.last[domain] = time.monotonic()
        return domain

    def _retry_seconds(self, headers):
        value = headers.get("Retry-After", "")
        try:
            return max(30, float(value))
        except (ValueError, TypeError):
            when = _dt(value) if value else None
            return max(30, (when - datetime.now(SHANGHAI)).total_seconds()) if when else 900

    def timeout(self):
        remaining = float(self.cfg.get("request_budget_seconds", 180)) - (time.monotonic() - self.started)
        return max(0.1, min(float(self.cfg.get("timeout_seconds", 12)), remaining))

    def _status(self, code, headers, domain):
        if code == 429:
            self.blocked.add(domain)
            raise RateLimited("HTTP 429：尊重 Retry-After，已暂停此来源", self._retry_seconds(headers))
        if code in (401, 403):
            self.blocked.add(domain)
            raise SourceUnavailable(f"HTTP {code}：来源限制访问，停止请求，不绕过验证")
        if code >= 400:
            raise SourceUnavailable(f"HTTP {code}")

    def _raw(self, url, headers=None):
        with self.send_lock:
            return self._raw_unlocked(url, headers)

    def _raw_unlocked(self, url, headers=None):
        domain = self._before(url)
        req = urllib.request.Request(url, headers={**(headers or {}), "User-Agent": self.cfg["user_agent"]})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout()) as response:
                self._status(response.status, response.headers, domain)
                limit = int(self.cfg.get("max_response_bytes", 4000000))
                content = response.read(limit + 1)
                if len(content) > limit:
                    raise SourceUnavailable("响应超过大小预算，停止解析")
                return content, dict(response.headers)
        except urllib.error.HTTPError as exc:
            self._status(exc.code, exc.headers, domain)
            raise

    def permitted(self, url):
        if not self.cfg.get("respect_robots", True):
            return True
        p = urllib.parse.urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            parser = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
            try:
                body, _ = self._raw(origin + "/robots.txt")
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
            except RateLimited:
                self.robots[origin] = False
                raise
            except Exception as exc:
                # 404/410 explicitly mean no published robots file. Other failure
                # states fail closed; don't treat network failure as permission.
                if "HTTP 404" in str(exc) or "HTTP 410" in str(exc):
                    parser.parse([])
                else:
                    self.robots[origin] = False
                    raise SourceUnavailable("robots.txt 无法核验，按配置暂停此来源") from exc
            self.robots[origin] = parser
        parser = self.robots[origin]
        if parser is False or not parser.can_fetch(self.cfg["user_agent"], url):
            raise SourceUnavailable("robots.txt 不允许此路径，未继续访问")
        delay = parser.crawl_delay(self.cfg["user_agent"]) or parser.crawl_delay("*")
        if delay and delay > 10:
            raise SourceUnavailable("站点 crawl-delay 超过本轮允许等待时长，暂缓采集")
        if delay:
            host = self._host(url)
            remaining = delay - (time.monotonic() - self.last.get(host, 0))
            if remaining > 0:
                time.sleep(remaining)
        return True

    def get(self, url):
        self.permitted(url)
        retries = int(self.cfg.get("max_retries", 1))
        for attempt in range(retries + 1):
            try:
                return self._raw(url)
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == retries:
                    raise
                time.sleep(min(5, float(self.cfg.get("retry_backoff_seconds", 3)) * 2 ** attempt))

    @contextmanager
    def guard_requests(self):
        """Limit requests issued inside optional AKShare without changing its API."""
        import requests
        with _TRANSPORT_LOCK:
            original = requests.sessions.Session.send
            gate = self

            def guarded(session, request, **kwargs):
                with gate.send_lock:
                    gate.permitted(request.url)
                    domain = gate._before(request.url)
                    request.headers["User-Agent"] = gate.cfg["user_agent"]
                    kwargs["timeout"] = gate.timeout()
                    response = original(session, request, **kwargs)
                    gate._status(response.status_code, response.headers, domain)
                    if len(response.content) > int(gate.cfg.get("max_response_bytes", 4000000)):
                        raise SourceUnavailable("AKShare 响应超过大小预算")
                    return response

            requests.sessions.Session.send = guarded
            try:
                yield
            finally:
                requests.sessions.Session.send = original


def _seed_sector(seed, previous):
    row = copy.deepcopy(previous or {})
    row.update({k: copy.deepcopy(v) for k, v in seed.items()})
    for key in ("price", "change_pct", "market_cap", "as_of", "code", "prediction"):
        row.setdefault(key, None)
    for key in ("history", "market_cap_history", "stocks", "drivers", "upstream", "downstream"):
        row.setdefault(key, [])
    row.setdefault("provider", "eastmoney")
    row["stale"] = True
    return row


def _history(frame, source, now, limit):
    out = []
    today = now.date().isoformat()
    for record in _records(frame):
        day = str(_get(record, "date", "日期"))[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        if day > today or (day == today and (now.hour, now.minute) < (15, 10)):
            continue
        row = {"date": day, "source": source, "as_of": now.isoformat(timespec="seconds")}
        for target, aliases in {"open": ("open", "开盘"), "close": ("close", "收盘"), "high": ("high", "最高"), "low": ("low", "最低"), "volume": ("volume", "成交量"), "amount": ("amount", "成交额")}.items():
            row[target] = _number(_get(record, *aliases))
        if row["close"] is not None and row["close"] > 0 and row["open"] is not None and row["open"] > 0:
            out.append(row)
    return sorted({r["date"]: r for r in out}.values(), key=lambda r: r["date"])[-limit:]


def _cap_snapshot(row, now, config):
    if row.get("market_cap") is None:
        return
    session = config.get("market_session", {})
    is_trading = session.get("is_trading_day") is True
    if not is_trading or (now.hour, now.minute) < (15, 15):
        return
    day = now.date().isoformat()
    item = {"date": day, "snapshot_date": day, "market_date": None, "date_basis": "collection_date", "value": row["market_cap"], "market_cap": row["market_cap"], "as_of": row["as_of"], "source": "eastmoney", "basis": "provider_snapshot", "currency": "CNY", "note": "采集日快照；来源未提供行情时间，不能确认其对应交易日"}
    items = {r["date"]: r for r in row.get("market_cap_history", []) if "date" in r}
    items[day] = item
    row["market_cap_history"] = sorted(items.values(), key=lambda r: r["date"])[-900:]
    row["market_cap_history_note"] = "本系统自开始采集起积累的市值快照；横轴为采集日期，非供应商历史市值回补"


def _host_matches(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


POSITIVE = ["growth", "rises", "rise", "gains", "gain", "expand", "expansion", "approval", "approved", "recovery", "boost", "增长", "提升", "批准", "获批", "突破", "复苏", "增长", "上涨"]
NEGATIVE = ["decline", "falls", "fall", "cuts", "shortage", "disruption", "sanction", "ban", "restrict", "tariff", "risk", "slump", "下降", "下跌", "制裁", "限制", "短缺", "中断", "风险", "关税"]
RUMOR = ["rumor", "rumour", "unconfirmed", "未经证实", "网传", "传闻", "据传", "小道消息"]
MACRO_TERMS = ["interest rate", "monetary policy", "inflation", "rate decision", "rate cut", "rate hike", "foreign exchange", "货币政策", "通胀", "利率", "降息", "降准", "加息", "人民币汇率", "流动性"]
CHINA_MARKET_TERMS = ["A-share", "Chinese stocks", "China stocks", "Chinese economy", "China economy", "Chinese exports", "A股", "中国经济", "中国股市", "中国出口", "国内经济", "中国制造", "人民银行"]


def _a_share_relevance(title, summary, sector_ids, sectors):
    body = title + " " + summary
    macro = [term for term in MACRO_TERMS if _contains(body, term)]
    china = [term for term in CHINA_MARKET_TERMS if _contains(body, term)]
    if not sector_ids and not macro and not china:
        return None
    names = [s["name"] for s in sectors if s["id"] in sector_ids][:5]
    reasons = []
    if names:
        reasons.append("关键词关联中国A股板块：" + "、".join(names) + "；需核验对供需、成本或订单的实际影响")
    if macro:
        reasons.append("宏观利率、汇率或流动性信息，可能经全球资金成本与估值预期传导至A股")
    if china:
        reasons.append("明确涉及中国经济或A股市场，可作为全市场研究线索")
    return {"market_scope": "A-share", "market_ids": ["sh000001", "sz399001", "sh000300"] if macro or china else [], "relevance_note": "；".join(reasons), "relevance_method": "keyword_rules", "relevance_evidence": {"macro": macro, "china_market": china}, "relevance_verified": False}


def _contains(text, word):
    if re.fullmatch(r"[a-zA-Z0-9 ]+", word):
        return bool(re.search(r"\b" + re.escape(word.lower()) + r"\b", text.lower()))
    return word.lower() in text.lower()


def _news_item(raw, spec, sectors, config, now, old_by_id):
    title = _text(raw.get("title"), 280)
    url = str(raw.get("url") or "")
    if not title or urllib.parse.urlparse(url).scheme not in ("https", "http"):
        return None
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    credibility = spec.get("credibility", "unverified")
    if spec.get("type") == "gdelt":
        credibility = "official" if _host_matches(host, config.get("official_domains", [])) else "trusted" if _host_matches(host, config.get("trusted_domains", [])) else "unverified"
    body = title + " " + _text(raw.get("summary"), 1200)
    rumor = any(_contains(body, w) for w in RUMOR)
    if rumor:
        credibility = "rumor"
    sector_ids = [s["id"] for s in sectors if any(_contains(body, word) for word in s.get("keywords", []) if word)]
    relevance = _a_share_relevance(title, _text(raw.get("summary"), 1200), sector_ids, sectors)
    if not relevance:
        return None
    pos = sorted({word for word in POSITIVE if _contains(title, word)})
    neg = sorted({word for word in NEGATIVE if _contains(title, word)})
    score = len(pos) - len(neg)
    sentiment = "positive" if score > 0 else "negative" if score < 0 else "neutral" if pos or neg else "uncertain"
    nid = hashlib.sha256(url.split("#")[0].encode()).hexdigest()[:20]
    previous = old_by_id.get(nid, {})
    published = _iso(raw.get("published_at")) if raw.get("published_at") else None
    # Lexical matches are topic associations, never a claim of return causality.
    return {"id": nid, "title": title, "source": raw.get("source") or spec["name"], "source_id": spec["id"], "url": url, "published_at": published, "first_seen_at": previous.get("first_seen_at") or now.isoformat(timespec="seconds"), "fetched_at": now.isoformat(timespec="seconds"), "credibility": credibility, "category": spec.get("category", "全球产业"), "sector_ids": sector_ids, "summary": _text(raw.get("summary"), 240), "impact": "uncertain", "transmission": [], "analysis_method": "keyword_rules", "verification": "unverified", "sentiment": sentiment, "sentiment_score": score, "sentiment_method": "lexicon_rules", "sentiment_evidence": {"positive": pos, "negative": neg}, "sentiment_note": "标题词典语气，不代表对A股涨跌的判断；无命中为未知。", "include_in_sentiment": not rumor, "topic_note": "板块关联来自关键词匹配，未验证因果和收益方向", **relevance}


def _parse_rss(data):
    root = ET.fromstring(data)
    out = []
    entries = root.findall(".//item") + root.findall("{http://www.w3.org/2005/Atom}entry")
    for item in entries:
        def val(*names):
            for name in names:
                node = item.find(name)
                if node is not None:
                    return "".join(node.itertext())
            return None
        link = val("link")
        if not link:
            for node in item.findall("{http://www.w3.org/2005/Atom}link"):
                if node.attrib.get("rel", "alternate") == "alternate":
                    link = node.attrib.get("href")
                    break
        out.append({"title": val("title", "{http://www.w3.org/2005/Atom}title"), "url": link, "summary": val("description", "{http://www.w3.org/2005/Atom}summary"), "published_at": val("pubDate", "{http://purl.org/dc/elements/1.1/}date", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated")})
    return out


def fetch_data(previous=None, config=None, now=None):
    """Return dashboard fields plus history. Set network_enabled=False to bootstrap.

    Config overrides are recursively merged with config/sources.json. Pass the
    scheduler's market_session to record cap snapshots on confirmed trading days.
    Sources retain failure counters and Retry-After across invocations in previous.
    """
    previous = previous or {}
    cfg = _merge(json.loads((ROOT / "config/sources.json").read_text()), config or {})
    sector_cfg = json.loads((ROOT / "config/sectors.json").read_text())
    now = _dt(now)
    if now is None:
        raise ValueError("now 必须为 datetime 或 ISO 日期")
    stamp = now.isoformat(timespec="seconds")
    old_sectors = {s["id"]: s for s in previous.get("sectors", [])}
    sectors = [_seed_sector(seed, old_sectors.get(seed["id"])) for seed in sector_cfg["sectors"]]
    known = {s["id"] for s in sectors}
    sectors.extend(_seed_sector(s, s) for s in previous.get("sectors", []) if s["id"] not in known)
    old_market = {m["id"]: m for m in previous.get("market", [])}
    market = []
    for spec in cfg["akshare"]["market_indices"]:
        item = copy.deepcopy(old_market.get(spec["id"], {}))
        item.update(spec)
        for key in ("price", "change_pct", "as_of"):
            item.setdefault(key, None)
        item.setdefault("history", [])
        item.setdefault("market_cap_history", [])
        quote_time = _dt(item.get("as_of")) if item.get("as_of") else None
        item["stale"] = not (quote_time and (now - quote_time).total_seconds() <= cfg["cache_seconds"]["quotes"])
        market.append(item)
    old_sources = {s["id"]: s for s in previous.get("sources", [])}
    definitions = [{"id": "eastmoney", "name": "东方财富 / AKShare", "type": "market", "url": "https://akshare.akfamily.xyz/data/stock/stock_board.html"}, *cfg["feeds"]]
    states = {}
    for spec in definitions:
        state = copy.deepcopy(old_sources.get(spec["id"], {}))
        state.update({k: spec[k] for k in ("id", "name", "type", "url")})
        state.setdefault("last_success", None)
        state.setdefault("status", "pending")
        state.setdefault("message", "待首次采集")
        states[spec["id"]] = state
    for portal in cfg.get("official_portals", []):
        sid = "portal_" + hashlib.sha256(portal["url"].encode()).hexdigest()[:8]
        states[sid] = {"id": sid, "name": portal["name"], "type": "official_portal", "status": "pending", "last_success": None, "url": portal["url"], "message": "官方核验入口；尚未配置获准自动feed/API"}
    news = []
    for item in copy.deepcopy(previous.get("news", [])):
        relevance = _a_share_relevance(item.get("title", ""), item.get("summary", ""), item.get("sector_ids", []), sectors)
        if relevance:
            item.update(relevance)
            news.append(item)
    history = copy.deepcopy(previous.get("history", {}))
    result = {"market": market, "sectors": sectors, "news": news, "sources": list(states.values()), "history": history, "official_portals": cfg.get("official_portals", [])}
    for item in sectors:
        quote_time = _dt(item.get("as_of")) if item.get("as_of") else None
        item["stale"] = not (quote_time and (now - quote_time).total_seconds() <= cfg["cache_seconds"]["quotes"])
    if not cfg.get("network_enabled", True):
        for state in states.values():
            if state["type"] != "official_portal":
                state["status"] = "stale" if state["last_success"] else "pending"
                state["message"] = "未发起网络采集；已有数据仅为缓存" if state["last_success"] else "等待配置网络后首次采集"
        return result
    # Separate budgets guarantee that market pagination cannot starve news.
    total_seconds = float(cfg.get("request_budget_seconds", 180))
    total_requests = int(cfg.get("max_http_requests", 80))
    market_cfg = _merge(cfg, {"request_budget_seconds": total_seconds * 2 / 3, "max_http_requests": max(1, total_requests * 3 // 4)})
    gate = market_gate = _Gate(market_cfg)
    ak = None
    ak_calls = 0

    def usable(sid, ttl=0):
        state = states[sid]
        retry = _dt(state.get("retry_after")) if state.get("retry_after") else None
        if retry and retry > now:
            state["status"] = "stale" if state.get("last_success") else "error"
            state["message"] = "来源冷却中，等待 " + retry.isoformat(timespec="seconds")
            return False
        last = _dt(state.get("last_success")) if state.get("last_success") else None
        return not (last and (now - last).total_seconds() < ttl)

    def success(sid, message):
        states[sid].update(status="success", last_success=stamp, last_attempt=stamp, message=message, failures=0, retry_after=None)

    def failure(sid, exc):
        state = states[sid]
        count = int(state.get("failures", 0)) + 1
        delay = getattr(exc, "retry_seconds", float(cfg.get("circuit_cooldown_seconds", 900)) * min(8, 2 ** (count - 1)))
        state.update(status="stale" if state.get("last_success") else "error", last_attempt=stamp, failures=count, message=_text(f"{type(exc).__name__}: {exc}", 240))
        state["retry_after"] = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")

    def ak_call(names, **kwargs):
        nonlocal ak_calls, ak
        if ak_calls >= int(cfg.get("max_akshare_calls", 10)):
            raise SourceUnavailable("本轮 AKShare 调用预算已用完")
        if ak is None:
            try:
                ak = importlib.import_module("akshare")
            except ImportError as exc:
                raise SourceUnavailable("缺少可选依赖 AKShare，请安装 requirements.txt 后重试") from exc
        fn = next((getattr(ak, name) for name in names if hasattr(ak, name)), None)
        if fn is None:
            raise SourceUnavailable("当前 AKShare 版本缺少接口：" + "/".join(names))
        ak_calls += 1
        with gate.guard_requests():
            return fn(**kwargs)

    market_errors = []
    market_successes = []
    if cfg["akshare"].get("enabled", True) and usable("eastmoney", cfg["cache_seconds"]["quotes"]):
        try:
            frame = ak_call(["index_zh_spot_em", "stock_zh_index_spot_em"], symbol="沪深重要指数")
            lookup = {str(_get(r, "代码", "code")): r for r in _validated_records(frame, ["代码", "最新价", "涨跌幅"])}
            for item in market:
                r = lookup.get(item["id"][2:])
                if r:
                    item.update(price=_number(_get(r, "最新价", "price")), change_pct=_number(_get(r, "涨跌幅", "change_pct")), open=_number(_get(r, "今开", "open")), previous_close=_number(_get(r, "昨收", "preclose")), amount=_number(_get(r, "成交额", "amount")), as_of=stamp, stale=False, source="eastmoney", url="https://quote.eastmoney.com/zs" + item["id"][2:] + ".html", timestamp_quality="retrieved_only")
            market_successes.append("指数快照")
        except Exception as exc:
            market_errors.append(exc)

        for kind, api in [("industry", "stock_board_industry_name_em"), ("concept", "stock_board_concept_name_em")]:
            try:
                records = _validated_records(ak_call([api]), ["板块代码", "板块名称", "最新价", "涨跌幅"])
                by_name = {str(_get(r, "板块名称", "名称")): r for r in records}
                by_code = {str(_get(r, "板块代码", "代码")): r for r in records}
                selected = [s for s in sectors if s.get("kind") == kind]
                used_codes = set()
                for item in selected:
                    row = by_code.get(str(item.get("code"))) or by_name.get(item.get("provider_name", item["name"]))
                    if row:
                        item["code"] = str(_get(row, "板块代码", "代码"))
                        used_codes.add(item["code"])
                candidates = records if kind == "industry" else sorted(records, key=lambda r: _number(r.get("换手率")) or 0, reverse=True)[:int(sector_cfg.get("dynamic_hot_concepts", 10))]
                for r in candidates:
                    code = str(_get(r, "板块代码", "代码"))
                    name = str(_get(r, "板块名称", "名称"))
                    if code not in used_codes and code and name:
                        item = _seed_sector({"id": "em_" + code.lower(), "code": code, "name": name, "provider_name": name, "kind": kind, "group": "行业全景" if kind == "industry" else "动态热门", "keywords": [name], "enabled": True, "dynamic": True}, old_sectors.get("em_" + code.lower()))
                        sectors.append(item)
                        selected.append(item)
                        used_codes.add(code)
                for item in selected:
                    r = by_code.get(str(item.get("code")))
                    if r:
                        item.update(price=_number(r.get("最新价")), change_pct=_number(r.get("涨跌幅")), market_cap=_number(r.get("总市值")), turnover_rate=_number(r.get("换手率")), advancing=_number(r.get("上涨家数")), declining=_number(r.get("下跌家数")), as_of=stamp, stale=False, timestamp_quality="retrieved_only", url="https://quote.eastmoney.com/bk/90." + item["code"] + ".html", cap_unit="CNY")
                        # A price leader is not necessarily a market-cap leader.
                        item["price_leader"] = {"name": _text(r.get("领涨股票"), 60), "code": str(r.get("领涨股票-代码") or ""), "change_pct": _number(r.get("领涨股票-涨跌幅"))}
                        _cap_snapshot(item, now, cfg)
                market_successes.append("全部行业快照" if kind == "industry" else "概念快照筛选")
            except Exception as exc:
                market_errors.append(exc)

        active = [s for s in sectors if s.get("code") and s.get("enabled", True)]
        # Every matched sector participates; only each run's calls are limited.
        active.sort(key=lambda s: s["id"])
        tasks = [(m, "index") for m in market] + [(s, s["kind"]) for s in active]
        tasks.sort(key=lambda x: x[0].get("history_attempt_at") or x[0].get("history_updated_at") or "")
        completed = 0
        for item, kind in tasks:
            if completed >= int(cfg.get("max_history_calls", 3)):
                break
            updated = _dt(item.get("history_updated_at")) if item.get("history_updated_at") else None
            if updated and (now - updated).total_seconds() < cfg["cache_seconds"]["history"]:
                continue
            completed += 1
            item["history_attempt_at"] = stamp
            try:
                start = _incremental_start(item, (now - timedelta(days=int(cfg.get("history_days", 1000)))).date(), now.date(), cfg.get("history_overlap_days", 7)).strftime("%Y%m%d")
                end = now.strftime("%Y%m%d")
                if kind == "index":
                    frame = ak_call(["index_zh_daily_em", "stock_zh_index_daily_em"], symbol=item["id"], start_date=start, end_date=end)
                else:
                    fn = "stock_board_industry_hist_em" if kind == "industry" else "stock_board_concept_hist_em"
                    frame = ak_call([fn], symbol=item["code"], start_date=start, end_date=end, period="日k", adjust="")
                bars = _history(frame, "eastmoney", now, int(cfg.get("max_history_rows", 900)))
                if not bars:
                    raise SourceUnavailable("未返回可用的已完成日K")
                merged = {r["date"]: r for r in item.get("history", [])}
                merged.update({r["date"]: r for r in bars})
                item["history"] = sorted(merged.values(), key=lambda r: r["date"])
                item["history_updated_at"] = stamp
                item.pop("history_error", None)
                history[item["id"]] = item["history"]
                market_successes.append(item["name"] + "历史")
            except Exception as exc:
                item["history_error"] = _text(exc, 180)
                market_errors.append(exc)
        constituents = sorted(active, key=lambda s: s.get("stocks_attempt_at") or s.get("stocks_updated_at") or "")
        for item in constituents[:int(cfg.get("max_constituent_calls", 2))]:
            updated = _dt(item.get("stocks_updated_at")) if item.get("stocks_updated_at") else None
            if updated and (now - updated).total_seconds() < cfg["cache_seconds"]["constituents"]:
                continue
            item["stocks_attempt_at"] = stamp
            try:
                fn = "stock_board_industry_cons_em" if item["kind"] == "industry" else "stock_board_concept_cons_em"
                records = _validated_records(ak_call([fn], symbol=item["code"]), ["代码", "名称", "最新价"])
                ranked = sorted(records, key=lambda r: _number(r.get("成交额")) or 0, reverse=True)
                item["stocks"] = [{"code": str(r.get("代码", "")), "name": r.get("名称", ""), "price": _number(r.get("最新价")), "change_pct": _number(r.get("涨跌幅")), "market_cap": _number(r.get("总市值")), "amount": _number(r.get("成交额")), "as_of": stamp, "rank_basis": "turnover_amount"} for r in ranked[:10]]
                item["stocks_updated_at"] = stamp
                item["stocks_note"] = "按成交额排序的活跃成分股；市值字段缺失时不推算，非投资推荐"
                market_successes.append(item["name"] + "成分")
            except Exception as exc:
                item["stocks_error"] = _text(exc, 180)
                market_errors.append(exc)
        if market_successes:
            success("eastmoney", "、".join(market_successes))
            if market_errors:
                states["eastmoney"].update(status="stale", message="部分更新成功；" + _text(market_errors[0], 180), partial=True)
                limited = next((e for e in market_errors if isinstance(e, RateLimited)), None)
                if limited:
                    states["eastmoney"]["retry_after"] = (now + timedelta(seconds=limited.retry_seconds)).isoformat(timespec="seconds")
        elif market_errors:
            failure("eastmoney", market_errors[0])

    news_cfg = _merge(cfg, {"request_budget_seconds": total_seconds / 3, "max_http_requests": max(1, total_requests - market_cfg["max_http_requests"])})
    gate = news_gate = _Gate(news_cfg)
    # Shared domain cooldowns retain robots and Retry-After constraints across budgets.
    news_gate.last = market_gate.last
    news_gate.robots = market_gate.robots
    news_gate.blocked = market_gate.blocked
    old_news = {n["id"]: n for n in news}
    for spec in cfg["feeds"]:
        sid = spec["id"]
        if not spec.get("enabled", True):
            states[sid].update(status="pending", message="配置中已停用")
            continue
        if not usable(sid, cfg["cache_seconds"]["news"]):
            continue
        try:
            if spec["type"] == "akshare_news":
                rows = _records(ak_call(["stock_news_cjzc_em"]))
                raw_news = [{"title": r.get("标题"), "url": r.get("链接"), "summary": r.get("摘要"), "published_at": r.get("发布时间")} for r in rows]
            elif spec["type"] == "rss":
                body, _ = gate.get(spec["url"])
                raw_news = _parse_rss(body)
            elif spec["type"] == "federal_register":
                query = urllib.parse.urlencode({"per_page": cfg["max_news_per_source"], "order": "newest", "conditions[term]": spec.get("query", "China")})
                body, _ = gate.get(spec["url"] + "?" + query)
                payload = json.loads(body)
                raw_news = [{"title": r.get("title"), "url": r.get("html_url"), "summary": r.get("abstract"), "published_at": r.get("publication_date")} for r in payload.get("results", [])]
            elif spec["type"] == "gdelt":
                query = urllib.parse.urlencode({"query": spec["query"], "mode": "artlist", "format": "json", "sort": "DateDesc", "maxrecords": cfg["max_news_per_source"], "timespan": "24h"})
                body, headers = gate.get(spec["url"] + "?" + query)
                payload = json.loads(body)
                raw_news = []
                for r in payload.get("articles", []):
                    date = r.get("seendate")
                    try:
                        date = datetime.strptime(date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
                    except (ValueError, TypeError):
                        date = None
                    raw_news.append({"title": r.get("title"), "url": r.get("url"), "published_at": None, "source": r.get("domain"), "gdelt_seen_at": date})
            else:
                raise SourceUnavailable("未知来源适配器类型")
            added = 0
            for raw in raw_news[:int(cfg["max_news_per_source"])]:
                item = _news_item(raw, spec, sectors, cfg, now, old_news)
                if item:
                    if raw.get("gdelt_seen_at"):
                        item["source_seen_at"] = raw["gdelt_seen_at"]
                    old_news[item["id"]] = item
                    added += 1
            success(sid, f"本轮保留 {added} 条A股相关线索；过滤无关联内容，保留原文与时间")
        except Exception as exc:
            failure(sid, exc)
    result["news"] = sorted(old_news.values(), key=lambda n: (bool(n.get("published_at")), n.get("published_at") or n.get("first_seen_at") or ""), reverse=True)[:int(cfg["max_news"])]
    result["collection"] = {"http_requests": market_gate.count + news_gate.count, "market_http_requests": market_gate.count, "news_http_requests": news_gate.count, "akshare_calls": ak_calls, "budget": cfg["max_http_requests"], "budget_seconds": {"market": market_cfg["request_budget_seconds"], "news": news_cfg["request_budget_seconds"]}, "started_at": stamp, "note": "所有匹配板块均参与历史/成分轮转；新闻保留独立请求预算，仅展示A股相关研究线索"}
    return result
