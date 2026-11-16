"""Point-in-time news tone for broad indices and SW2021 level-one industries.

The optional, pinned Apache-2.0 Chinese FinBERT runs genuine CPU inference.
Without it, bilingual financial rules are explicitly identified as rules.
Neither classifier confidence nor news tone is a stock-return probability.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import json
import math
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .calendar import shanghai_now

RULE_VERSION = "financial-tone-rules-v2"
PIPELINE_VERSION = "sw2021-news-tone-v1"
MODEL_ID = "yiyanghkust/finbert-tone-chinese"
MODEL_REVISION = "e91b1a3af10e1e8c9c03429d3cd7d5e9a1c8000d"
MODEL_LICENSE = "Apache-2.0"
MODEL_URL = f"https://huggingface.co/{MODEL_ID}/tree/{MODEL_REVISION}"

# Fixed SW2021 L1 taxonomy, not mutable Eastmoney board names. Multiple industry
# associations are possible; they are topic evidence, never a causal price claim.
SW2021 = {
    "801010": ("农林牧渔", ["农业", "养殖", "种业", "生猪", "猪肉", "饲料", "粮食", "agriculture", "livestock", "pork", "grain"]),
    "801020": ("煤炭", ["煤炭", "焦煤", "动力煤", "焦炭", "coal", "coking coal"]),
    "801030": ("基础化工", ["基础化工", "化肥", "农药", "氟化工", "纯碱", "钛白粉", "化学原料", "chemicals", "fertilizer", "pesticide"]),
    "801040": ("钢铁", ["钢铁", "钢材", "铁矿石", "粗钢", "螺纹钢", "steel", "iron ore"]),
    "801050": ("有色金属", ["有色金属", "铜价", "铝价", "锂矿", "稀土", "黄金", "白银", "copper", "aluminium", "aluminum", "rare earth", "gold", "lithium"]),
    "801080": ("电子", ["半导体", "芯片", "集成电路", "消费电子", "晶圆", "存储器", "光刻", "semiconductor", "chip", "wafer", "consumer electronics"]),
    "801110": ("家用电器", ["家用电器", "家电", "空调", "冰箱", "洗衣机", "home appliance", "air conditioner"]),
    "801120": ("食品饮料", ["食品饮料", "白酒", "啤酒", "乳制品", "调味品", "饮料", "food and beverage", "dairy", "liquor", "beer"]),
    "801130": ("纺织服饰", ["纺织", "服装", "服饰", "棉花", "textile", "apparel", "cotton", "garment"]),
    "801140": ("轻工制造", ["造纸", "纸浆", "家具", "包装", "家居", "paper pulp", "furniture", "packaging"]),
    "801150": ("医药生物", ["医药", "创新药", "生物医药", "医疗器械", "疫苗", "集采", "pharmaceutical", "drug approval", "biotech", "vaccine", "medical device"]),
    "801160": ("公用事业", ["公用事业", "电价", "火电", "水电", "核电", "电力供应", "燃气", "utilities", "electricity tariff", "nuclear power", "power generation"]),
    "801170": ("交通运输", ["交通运输", "航运", "航空", "港口", "快递", "物流", "运价", "shipping", "airline", "freight", "logistics"]),
    "801180": ("房地产", ["房地产", "地产", "住房", "楼市", "房贷", "real estate", "property developer", "housing", "mortgage"]),
    "801200": ("商贸零售", ["零售", "百货", "商超", "电商", "免税", "retail", "e-commerce", "supermarket", "duty-free"]),
    "801210": ("社会服务", ["旅游", "酒店", "餐饮", "教育培训", "景区", "tourism", "hotel", "restaurant", "travel demand"]),
    "801230": ("综合", ["综合行业", "多元化集团", "diversified conglomerate"]),
    "801710": ("建筑材料", ["建筑材料", "建材", "水泥", "玻璃", "玻纤", "cement", "building materials", "fiberglass"]),
    "801720": ("建筑装饰", ["建筑装饰", "基建", "工程建设", "工程承包", "基础设施", "infrastructure", "construction contract"]),
    "801730": ("电力设备", ["电力设备", "光伏", "风电", "储能", "动力电池", "电网设备", "新能源电池", "solar", "photovoltaic", "wind power", "energy storage", "power battery"]),
    "801740": ("国防军工", ["国防军工", "军工", "军费", "航空发动机", "卫星", "导弹", "defense spending", "defence spending", "military", "missile", "aerospace"]),
    "801750": ("计算机", ["计算机", "人工智能", "软件", "云计算", "网络安全", "数据中心", "算力", "artificial intelligence", "software", "cloud computing", "cybersecurity", "data center"]),
    "801760": ("传媒", ["传媒", "游戏", "影视", "电影票房", "广告", "出版", "video game", "box office", "advertising", "publishing"]),
    "801770": ("通信", ["通信", "电信", "光通信", "光模块", "5G", "6G", "telecom", "optical module", "optical communication"]),
    "801780": ("银行", ["银行", "净息差", "不良贷款", "信贷", "bank", "banking", "net interest margin", "nonperforming loan"]),
    "801790": ("非银金融", ["非银金融", "证券公司", "券商", "保险", "保费", "资管", "brokerage", "insurer", "insurance premium", "asset management"]),
    "801880": ("汽车", ["汽车", "新能源车", "智能驾驶", "自动驾驶", "汽车零部件", "automobile", "automaker", "electric vehicle", "autonomous driving"]),
    "801890": ("机械设备", ["机械设备", "机器人", "机床", "工程机械", "工业自动化", "robotics", "machine tool", "industrial machinery", "automation"]),
    "801950": ("美容护理", ["美容护理", "化妆品", "医美", "护肤", "cosmetics", "skin care", "beauty care"]),
    "801960": ("石油石化", ["石油", "石化", "原油", "油价", "炼油", "油气", "天然气", "crude oil", "oil price", "refinery", "petrochemical", "natural gas"]),
    "801970": ("环保", ["环保", "污水", "固废", "垃圾处理", "碳交易", "节能减排", "wastewater", "waste management", "carbon trading", "pollution control"]),
}
SW_NAMES = {name: code for code, (name, _) in SW2021.items()}
MACRO_TERMS = ["A股", "中国股市", "沪深市场", "中国经济", "中国财政", "货币政策", "财政政策", "人民银行", "美联储", "欧洲央行",
               "利率决议", "降息", "加息", "降准", "通胀", "人民币汇率", "流动性", "贸易关税", "中国关税", "外汇储备",
               "a-shares", "a-share", "chinese stocks", "china economy", "chinese economy", "federal reserve", "monetary policy",
               "fiscal policy", "interest rate", "rate cut", "rate hike", "inflation", "renminbi", "yuan", "china tariff"]
RUMOR_TERMS = ["网传", "据传", "小道消息", "未经证实", "传闻", "rumor", "rumour", "unconfirmed"]
NEUTRAL_TERMS = ["基本持平", "维持不变", "保持不变", "保持稳定", "unchanged", "remained stable", "held steady"]
AMBIGUOUS_TERMS = ["降息", "加息", "降准", "油价上涨", "油价下跌", "rate cut", "rate hike", "oil prices rise", "oil prices fall"]

# Specific phrases supersede contained words; "亏损增长" cannot be counted as
# positive growth, and "削减成本/cost cuts" cannot be counted as a negative cut.
RULES = [
    (1, 2.0, ["扭亏为盈", "超出预期", "超预期", "上调盈利预期", "上调评级", "创历史新高", "beat expectations", "beats expectations", "raised guidance", "rating upgrade", "returned to profit"]),
    (1, 1.5, ["利润增长", "盈利增长", "营收增长", "收入增长", "订单增长", "订单增加", "需求回暖", "需求复苏", "需求增长", "净流入", "降本增效", "成本下降", "削减成本", "扩大产能", "获批", "profit growth", "profits rise", "earnings growth", "revenue growth", "demand recovery", "demand growth", "cost cuts", "lower costs", "net inflow", "wins contract"]),
    (1, 1.0, ["盈利改善", "利润改善", "复苏", "提振", "突破", "增长", "上涨", "recovery", "boost", "expansion", "gains"]),
    (-1, 2.0, ["低于预期", "不及预期", "下调盈利预期", "下调评级", "债务违约", "破产", "亏损扩大", "亏损增长", "利润暴跌", "missed expectations", "misses expectations", "cut guidance", "rating downgrade", "debt default", "bankruptcy", "widening losses"]),
    (-1, 1.5, ["利润下降", "盈利下降", "营收下降", "收入下降", "订单下降", "需求疲软", "需求下降", "成本上升", "供应中断", "净流出", "裁员", "出口限制", "限制出口", "制裁", "利润下滑", "profit decline", "profits fall", "revenue decline", "weak demand", "falling demand", "higher costs", "supply disruption", "net outflow", "layoffs", "export restriction", "sanctions"]),
    (-1, 1.0, ["亏损", "下跌", "暴跌", "下降", "下滑", "losses", "slump", "plunge", "decline"]),
]


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _timestamp(value):
    if not value:
        return None
    try:
        return shanghai_now(value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def _contains(text, term):
    return bool(re.search(_pattern(term), text, flags=re.I))


def _pattern(term):
    escaped = re.escape(term)
    return r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])" if re.fullmatch(r"[A-Za-z0-9 _-]+", term) else escaped


def _body(news):
    title = str(news.get("title") or "")[:1000]
    summary = str(news.get("summary") or news.get("content") or news.get("text") or "")[:10000]
    return title, summary, title + "\n" + summary


def _canonical_url(value):
    if not value:
        return None
    parts = urlsplit(str(value))
    if parts.scheme not in ("http", "https"):
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_") and k.lower() not in ("spm", "from", "ref")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(sorted(query)), ""))


def _dedup_key(news):
    title, summary, _ = _body(news)
    normalized = re.sub(r"[^a-z0-9\u3400-\u9fff]", "", unicodedata.normalize("NFKC", title).lower())
    if len(normalized) >= 8:
        return "title:" + _hash(normalized)
    url = _canonical_url(news.get("original_url") or news.get("url"))
    return "url:" + _hash(url) if url else "text:" + _hash([normalized, summary])


def _merge_stock_api_news(result, now):
    """Normalize the collector's separate feed while preserving existing news.

    Existing records, IDs, first-seen timestamps and model caches win duplicate
    matches. The same headline on a genuinely later publication day is a new
    event; cached old headlines must not suppress a later recurring announcement.
    """
    existing = result.setdefault("news", [])
    known_ids = {item.get("id") for item in existing if item.get("id")}
    known_urls = {_canonical_url(item.get("url")) for item in existing if _canonical_url(item.get("url"))}
    titles = {}

    def publication_day(item):
        stamp = _timestamp(item.get("published_at") or item.get("datetime"))
        return stamp.date().isoformat() if stamp else None

    def merge_key(item):
        title = re.sub(r"[^a-z0-9\u3400-\u9fff]", "", unicodedata.normalize("NFKC", _body(item)[0]).lower())
        return "headline:" + _hash(title) if len(title) >= 6 else _dedup_key(item)

    for item in existing:
        titles.setdefault(merge_key(item), set()).add(publication_day(item))
    added, duplicates = 0, 0
    incoming = result.get("stock_api_news", [])
    if not isinstance(incoming, list):
        incoming = []
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        title, content, body = _body(item)
        if not body.strip():
            continue
        item["title"] = title or content[:90]
        item.setdefault("summary", content[:800])
        item["source"] = item.get("source_name") or item.get("source") or "财经快讯"
        item.setdefault("source_id", "stock_api:" + str(raw.get("source") or "news"))
        item.setdefault("published_at", item.get("datetime"))
        item.setdefault("url", None)
        item.setdefault("category", "财经快讯")
        item.setdefault("credibility", "unverified")
        item.setdefault("verification", "unverified")
        item["id"] = item.get("id") or "stock-api-news-" + _hash([raw.get("source"), item.get("published_at"), item["title"], content])
        if not item.get("first_seen_at"):
            fetched = _timestamp(item.get("fetched_at"))
            item["first_seen_at"] = fetched.isoformat() if fetched else now.isoformat()
            item["first_seen_basis"] = "stock_api_fetched_at" if fetched else "first_observed_by_sentiment_pipeline"
        key, day, url = merge_key(item), publication_day(item), _canonical_url(item.get("url"))
        title_duplicate = key in titles and (day in titles[key] or day is None or None in titles[key])
        if item["id"] in known_ids or (url and url in known_urls) or title_duplicate:
            duplicates += 1
            continue
        existing.append(item)
        known_ids.add(item["id"])
        if url:
            known_urls.add(url)
        titles.setdefault(key, set()).add(day)
        added += 1
    return added, duplicates


def _negated(text, start):
    prefix = text[max(0, start - 22):start].lower()
    return bool(re.search(r"(?:\bnot\b|\bno\b|\bnever\b|\bwithout\b)[\w\s]{0,16}$", prefix)
                or re.search(r"(?:没有|并未|未能|未出现|未实现|不会|并不|不再|尚未|未)(?:明显|持续|实现|出现)?$", prefix))


def _rules_sentiment(news):
    title, summary, _ = _body(news)
    evidence, seen_terms = [], set()
    for scope, text, scope_weight in (("title", title, 1.5), ("summary", summary, 1.0)):
        candidates = []
        for polarity, strength, terms in RULES:
            for term in terms:
                for match in re.finditer(_pattern(term), text, flags=re.I):
                    candidates.append((match.start(), match.end(), term, polarity, strength))
        occupied = []
        for start, end, term, polarity, strength in sorted(candidates, key=lambda r: (-(r[1] - r[0]), -r[4], r[0])):
            if term in seen_terms or any(start < b and end > a for a, b in occupied):
                continue
            negated = _negated(text, start)
            evidence.append({"term": term, "polarity": polarity, "weight": 0.0 if negated else strength * scope_weight,
                             "negated": negated, "scope": scope, "start": start, "end": end,
                             "snippet": text[max(0, start - 24):min(len(text), end + 24)]})
            occupied.append((start, end))
            seen_terms.add(term)
    positive = sum(e["weight"] for e in evidence if e["polarity"] > 0)
    negative = sum(e["weight"] for e in evidence if e["polarity"] < 0)
    body = title + " " + summary
    neutral = [term for term in NEUTRAL_TERMS if _contains(body, term)]
    ambiguous = [term for term in AMBIGUOUS_TERMS if _contains(body, term)]
    # Commodity/rate direction alone is not a signed profitability implication.
    if ambiguous and not any(e["weight"] >= 2 or e["term"] not in ("上涨", "下跌", "增长", "下降") for e in evidence if e["weight"]):
        positive = negative = 0.0
    if positive + negative:
        score = (positive - negative) / (positive + negative + 1.5)
        label = "mixed" if positive and negative and abs(score) <= .3 else "positive" if score > 0 else "negative" if score < 0 else "mixed"
        confidence = min(.85, .35 + .08 * len([e for e in evidence if e["weight"]]) + .15 * abs(score))
    elif neutral:
        score, label, confidence = 0.0, "neutral", .45
    else:
        score, label, confidence = None, "unknown", None
    return {"label": label, "score": score, "confidence": confidence, "confidence_kind": "heuristic_evidence",
            "method": RULE_VERSION, "model_version": RULE_VERSION,
            "evidence": {"matches": evidence, "neutral_terms": neutral, "ambiguous_events": ambiguous},
            "note": "双语财经规则语气；置信为启发式证据强度，未经准确率校准，不表示A股涨跌概率"}


@lru_cache(maxsize=2)
def _load_model(cache_dir, local_files_only):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    options = {"revision": MODEL_REVISION, "token": False, "trust_remote_code": False,
               "local_files_only": local_files_only}
    if cache_dir:
        options["cache_dir"] = cache_dir
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, **options)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID, use_safetensors=True, **options).to("cpu").eval()
    labels = {int(k): str(v).lower() for k, v in model.config.id2label.items()}
    if labels != {0: "neutral", 1: "positive", 2: "negative"}:
        raise ValueError("Pinned FinBERT label mapping differs from verified config")
    return torch, tokenizer, model


def _finbert_scores(items, config):
    """Real CPU inference, anonymous download and no remote model code."""
    torch, tokenizer, model = _load_model(config.get("model_cache_dir"), bool(config.get("model_local_files_only", False)))
    if config.get("torch_threads"):
        torch.set_num_threads(max(1, int(config["torch_threads"])))
    scores = {}
    batch_size = max(1, min(16, int(config.get("model_batch_size", 4))))
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        encoded = tokenizer([_body(item)[2] for item in batch], padding=True, truncation=True,
                            max_length=512, return_tensors="pt")
        with torch.inference_mode():
            probabilities = model(**encoded).logits.softmax(dim=-1).cpu().tolist()
        for item, values in zip(batch, probabilities):
            probabilities_named = dict(zip(("neutral", "positive", "negative"), values))
            label = max(probabilities_named, key=probabilities_named.get)
            confidence = probabilities_named[label]
            if confidence < float(config.get("min_model_confidence", .45)):
                label = "unknown"
            scores[item["id"]] = {"label": label, "score": values[1] - values[2] if label != "unknown" else None,
                                  "confidence": confidence, "confidence_kind": "model_softmax_uncalibrated",
                                  "method": "finbert_chinese_cpu", "model_version": MODEL_ID + "@" + MODEL_REVISION,
                                  "evidence": {"model_probabilities": probabilities_named, "input_excerpt": _body(item)[2][:320],
                                               "token_limit": 512, "attribution": "文本分类输出；非词级因果解释"},
                                  "note": "中文财经文本分类；softmax置信未经本项目校准，不是上涨或下跌概率"}
    return scores


def _sw_code(entity):
    for key in ("index_code", "sw_code", "code", "id"):
        match = re.search(r"(?<!\d)(801\d{3})(?!\d)", str(entity.get(key) or ""))
        if match and match.group(1) in SW2021:
            return match.group(1)
    return SW_NAMES.get(entity.get("name"))


def _association(entity, news, is_market):
    body = _body(news)[2]
    evidence = []
    if is_market:
        macro = [word for word in MACRO_TERMS if _contains(body, word)]
        explicit = [word for word in [entity.get("name"), *entity.get("aliases", [])] if word and len(word) >= 3 and _contains(body, word)]
        if macro:
            evidence.append({"method": "macro_to_broad_index", "terms": macro[:10], "weight": .65,
                             "note": "宏观话题关联，传导方向需另行判断"})
        if explicit:
            evidence.append({"method": "explicit_index_name", "terms": explicit, "weight": 1.0})
    else:
        code = _sw_code(entity)
        if code:
            name, words = SW2021[code]
            names = [] if name == "综合" else [name]
            if name == "电子" and re.search(r"电子(?:商务|邮件|邮箱|政务|签名)", body):
                names = []
            terms = [word for word in [*names, *words] if _contains(body, word)]
            if terms:
                evidence.append({"method": "sw2021_l1_keywords", "sw_code": code + ".SI", "industry": name,
                                 "terms": sorted(set(terms))[:12], "weight": .75})
    members = entity.get("constituents") or entity.get("stocks") or []
    for stock in members:
        names = [str(stock.get("name") or "")]
        stripped = re.sub(r"^\*?ST", "", names[0], flags=re.I)
        if stripped != names[0] and len(stripped) >= 3:
            names.append(stripped)
        named = [name for name in names if len(name) >= 3 and _contains(body, name)]
        code = str(stock.get("code") or stock.get("ts_code") or "").split(".")[0]
        coded = bool(re.fullmatch(r"\d{6}", code) and re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", body))
        if named or coded:
            evidence.append({"method": "constituent_name" if named else "constituent_code", "terms": named or [code],
                             "stock_code": code or None, "weight": 1.0 if named else .85})
        if len(evidence) >= 12:
            break
    return evidence


def _is_rumor(news):
    if news.get("verification") in ("refuted", "debunked", "false") and news.get("credibility") in ("official", "trusted"):
        return False
    return news.get("credibility") == "rumor" or news.get("verification") in ("rumor", "unverified_rumor") or any(_contains(_body(news)[2], word) for word in RUMOR_TERMS)


def _source_weight(news):
    return {"official": 1.0, "trusted": .9, "reliable": .9, "rumor": .15}.get(news.get("credibility"), .55)


def _score_article(news, scoring, now):
    news.update(sentiment=scoring["label"], sentiment_score=scoring["score"], sentiment_method=scoring["method"],
                sentiment_confidence=scoring["confidence"], sentiment_confidence_kind=scoring["confidence_kind"],
                sentiment_evidence=scoring["evidence"], sentiment_note=scoring["note"],
                sentiment_model={"version": scoring["model_version"], "model_id": MODEL_ID if scoring["method"] == "finbert_chinese_cpu" else None,
                                 "revision": MODEL_REVISION if scoring["method"] == "finbert_chinese_cpu" else None,
                                 "license": MODEL_LICENSE if scoring["method"] == "finbert_chinese_cpu" else None},
                sentiment_scored_at=now.isoformat(), sentiment_input_hash=_hash(_body(news)[2]))


def aggregate_entity_sentiment(dashboard, now=None, config=None):
    """Deep copy, classify news, associate SW31/indices and append PIT snapshots.

    enable_model=False by default; true activates pinned CPU FinBERT, falling
    back visibly on dependency/download/inference errors. English remains rules.
    New snapshots are available only at this execution's now, never backdated
    to an article's publication date. No coverage is unknown, never neutral.
    """
    result = deepcopy(dashboard)
    config = config or {}
    if isinstance(config.get("sentiment"), dict):
        config = config["sentiment"]
    now = shanghai_now(now)
    stock_api_added, stock_api_duplicates = _merge_stock_api_news(result, now)
    window_hours = max(1, float(config.get("window_hours", 72)))
    window_start = now - timedelta(hours=window_hours)
    include_rumors = bool(config.get("include_rumors", False))
    enabled = bool(config.get("enable_model", config.get("model_enabled", False)))
    reuse_only = bool(config.get("reuse_only", config.get("cache_only", False)))
    allow_model_load = enabled and not reuse_only
    news_items = result.setdefault("news", [])
    eligible, excluded, model_items = [], [], []
    for news in news_items:
        title, summary, body = _body(news)
        news.setdefault("id", "news-" + _hash([news.get("source"), news.get("url"), title, summary]))
        first_seen = _timestamp(news.get("first_seen_at"))
        published = _timestamp(news.get("published_at") or news.get("datetime"))
        reason = None
        if first_seen is None and not news.get("first_seen_at"):
            # First observed here now: never infer historical availability from publication time.
            first_seen = now
            news["first_seen_at"] = now.isoformat()
            news["first_seen_basis"] = "first_observed_by_sentiment_pipeline"
        if first_seen is None:
            reason = "invalid_first_seen_at"
        elif first_seen > now or (published and published > now):
            reason = "future_information"
        elif first_seen < window_start:
            reason = "outside_news_window"
        elif not body.strip():
            reason = "empty_text"
        rumor = _is_rumor(news)
        if not reason and not include_rumors and (rumor or news.get("include_in_sentiment") is False):
            reason = "rumor_or_explicitly_excluded"
        news["sentiment_eligible"] = reason is None
        news["sentiment_exclusion_reason"] = reason
        news["sentiment_is_rumor"] = rumor
        # Do not overwrite the source's explicit inclusion policy with a transient
        # future/window exclusion, or a future article could never become eligible.
        news["sentiment_associations"] = {}
        if reason == "future_information" or first_seen is None:
            _score_article(news, {"label": "unknown", "score": None, "confidence": None, "confidence_kind": "heuristic_evidence",
                                  "method": "unavailable", "model_version": PIPELINE_VERSION, "evidence": {},
                                  "note": "消息尚未在本预测时点可知，未使用"}, now)
        else:
            cached_model = news.get("sentiment_model") or {}
            cached_time = _timestamp(news.get("sentiment_scored_at"))
            cache_ok = (cached_model.get("revision") == MODEL_REVISION and news.get("sentiment_input_hash") == _hash(body)
                        and cached_time is not None and cached_time <= now and news.get("sentiment_method") == "finbert_chinese_cpu")
            if not cache_ok:
                _score_article(news, _rules_sentiment(news), now)
                if allow_model_load and reason is None and len(re.findall(r"[\u3400-\u9fff]", body)) >= 8:
                    model_items.append(news)
        (eligible if reason is None else excluded).append(news)
    model_error, model_loaded = None, False
    model_cache_used = sum(n.get("sentiment_method") == "finbert_chinese_cpu" for n in eligible)
    model_items.sort(key=lambda item: item.get("first_seen_at") or "", reverse=True)
    max_model_articles = max(0, int(config.get("max_model_articles", 120)))
    model_deferred = max(0, len(model_items) - max_model_articles)
    model_items = model_items[:max_model_articles]
    if model_items:
        try:
            model_scores = _finbert_scores(model_items, config)
            for news in model_items:
                _score_article(news, model_scores[news["id"]], now)
            model_loaded = True
        except Exception as exc:
            # Error text is deliberately bounded; model loaders may include paths.
            model_error = type(exc).__name__ + ": 中文模型未可用，已使用明确标识的双语金融规则"
    clusters = {}
    for news in eligible:
        clusters.setdefault(_dedup_key(news), []).append(news)
    representatives = []
    for key, group in clusters.items():
        group.sort(key=lambda item: (-_source_weight(item), item.get("first_seen_at") or "", str(item["id"])))
        selected = group[0]
        selected["sentiment_duplicate_sources"] = sorted({str(item.get("source") or item.get("source_id") or "未知来源") for item in group})
        selected["sentiment_duplicate_ids"] = [item["id"] for item in group[1:]]
        for item in group:
            item["sentiment_dedup_key"] = key
            item["sentiment_duplicate_of"] = None if item is selected else selected["id"]
        representatives.append(selected)
    entity_entries = [(entity, True) for entity in result.get("market", [])] + [(entity, False) for entity in result.get("sectors", [])]
    for entity, is_market in entity_entries:
        evidence = []
        for news in representatives:
            associations = _association(entity, news, is_market)
            if not associations:
                continue
            identity = str(entity.get("id") or entity.get("index_code") or entity.get("code"))
            news["sentiment_associations"][identity] = associations
            if is_market:
                news["market_ids"] = sorted(set(news.get("market_ids", [])) | {identity})
            else:
                news["sector_ids"] = sorted(set(news.get("sector_ids", [])) | {identity})
            evidence.append({"news_id": news["id"], "title": news.get("title") or _body(news)[1][:90],
                             "url": news.get("url") or None, "source": news.get("source") or news.get("source_id") or "未知来源",
                             "published_at": news.get("published_at") or news.get("datetime"), "first_seen_at": news["first_seen_at"],
                             "label": news["sentiment"], "score": news["sentiment_score"], "confidence": news.get("sentiment_confidence"),
                             "method": news["sentiment_method"], "model_version": news["sentiment_model"]["version"],
                             "input_hash": news.get("sentiment_input_hash"),
                             "credibility": news.get("credibility", "unverified"), "associations": associations,
                             "duplicate_count": len(news.get("sentiment_duplicate_ids", [])),
                             "aggregation_weight": _source_weight(news) * max(a["weight"] for a in associations)})
        scored = [e for e in evidence if isinstance(e["score"], (float, int)) and math.isfinite(e["score"])]
        if scored:
            weight = sum(e["aggregation_weight"] for e in scored)
            score = sum(e["score"] * e["aggregation_weight"] for e in scored) / weight
            labels = {e["label"] for e in scored}
            if ("positive" in labels and "negative" in labels and abs(score) < .3) or "mixed" in labels:
                label = "mixed"
            elif labels == {"neutral"}:
                label = "neutral"
            else:
                label = "positive" if score > .1 else "negative" if score < -.1 else "mixed"
            confidence = sum((e["confidence"] or 0) * e["aggregation_weight"] for e in scored) / weight
        else:
            label, score, confidence = "unknown", None, None
        methods = sorted({e["method"] for e in evidence})
        versions = sorted({e["model_version"] for e in evidence})
        sentiment = {"label": label, "score": score, "confidence": confidence, "confidence_kind": "heuristic_evidence",
                     "method": "+".join(methods) if methods else "no_coverage", "model_version": "+".join(versions) or PIPELINE_VERSION,
                     "version": PIPELINE_VERSION, "as_of": now.isoformat(), "available_at": now.isoformat(), "date": now.date().isoformat(),
                     "window_start": window_start.isoformat(), "window_hours": window_hours,
                     "article_count": len(evidence), "scored_article_count": len(scored),
                     "source_count": len({e["source"] for e in evidence}), "evidence": evidence,
                     "note": "新闻语气与证据汇总，不是涨跌预测；行业关联不代表因果方向。未知不作中性。"}
        sentiment["feature_hash"] = _hash({"entity": entity.get("id"), "date": sentiment["date"], "version": PIPELINE_VERSION,
                                           "evidence": [{k: e[k] for k in ("news_id", "score", "method", "model_version", "input_hash", "associations", "aggregation_weight")} for e in evidence]})
        snapshot = {key: sentiment[key] for key in ("date", "as_of", "available_at", "window_start", "score", "label", "article_count",
                                                    "scored_article_count", "source_count", "confidence", "method", "model_version", "feature_hash")}
        snapshot["news_ids"] = [e["news_id"] for e in evidence]
        snapshot["id"] = "sentiment-" + sentiment["feature_hash"]
        history = entity.setdefault("sentiment_history", [])
        if not any(row.get("id") == snapshot["id"] for row in history):
            history.append(snapshot)
        entity["sentiment"] = sentiment
    result["sentiment_summary"] = {"as_of": now.isoformat(), "version": PIPELINE_VERSION, "taxonomy": "SW2021_L1_31",
                                   "stock_api_news_merged_count": stock_api_added, "stock_api_news_duplicate_count": stock_api_duplicates,
                                   "news_count": len(news_items), "eligible_count": len(eligible), "unique_news_count": len(representatives),
                                   "duplicate_count": len(eligible) - len(representatives), "excluded_count": len(excluded),
                                   "rumors_excluded": sum(n.get("sentiment_exclusion_reason") == "rumor_or_explicitly_excluded" for n in excluded),
                                   "model_requested": enabled, "model_loaded": model_loaded, "model_id": MODEL_ID,
                                   "reuse_only": reuse_only, "model_inference_count": len(model_items) if model_error is None else 0,
                                   "model_cache_used_count": model_cache_used, "model_used": model_loaded or model_cache_used > 0,
                                   "model_deferred_count": model_deferred,
                                   "model_revision": MODEL_REVISION, "model_license": MODEL_LICENSE, "model_source": MODEL_URL,
                                   "model_error": model_error, "fallback_method": RULE_VERSION,
                                   "labels": {label: sum(e["sentiment"]["label"] == label for e, _ in entity_entries)
                                              for label in ("positive", "negative", "mixed", "neutral", "unknown")}}
    return result
