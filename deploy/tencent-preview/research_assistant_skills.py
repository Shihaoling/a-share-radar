"""Read-only evidence skills backed by the same published snapshots as the site."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
import json
import math
from pathlib import Path
import re
import socket
import unicodedata
from urllib.parse import urlencode

from global_indices import INDICES as GLOBAL_INDICES
from research_metric_catalog import ALIASES as METRIC_ALIASES, CATALOG, CODES, REPORT_ID, valid_subject
from research_valuation import asset_metrics

STATE = Path("/var/lib/ashare-radar-preview")
SITE = Path("/srv/ashare-radar-preview/public/data")
MAX_FILE = 16_000_000
RESEARCH_SOCKET = "/run/ashare-asset-research/research.sock"
STATES = {-2: "大幅下降", -1: "抖动下降", 0: "波动", 1: "波动上升", 2: "快速上升"}
ALIASES = {
    "886069.TI": ("具身智能", "聚生智能"),
    "886033.TI": ("CPO",),
    "885728.TI": ("AI",),
    "885957.TI": ("东数西算",),
    "sz399006": ("创业板",),
    "sz399001": ("深成指",),
    "sh000001": ("沪指",),
    "HKTECH": ("恒生科技", "恒科"),
    "HKAH": ("恒生AH股H指数",),
    "DJI": ("道指", "道琼斯"),
    "SPX": ("标普500", "标普", "S&P 500"),
    "IXIC": ("纳指", "纳斯达克", "纳斯达克指数"),
}


@dataclass
class Evidence:
    label: str
    href: str
    latest: str
    count: int
    facts: dict

    def source(self):
        return {"label": self.label, "href": self.href, "date": self.latest, "count": self.count}


def source_items(ident, facts):
    """Concrete articles/announcements/reports behind an evidence skill."""
    rows = []
    if ident == "news":
        for row in facts.get("articles", [])[:5]:
            ident_value = row.get("id")
            if not isinstance(ident_value, str) or not ident_value or not row.get("title"):
                continue
            rows.append({
                "id": ident_value, "kind": "资讯", "title": row["title"],
                "date": date_of(row.get("published_at", "")),
                "meta": row.get("source", ""), "excerpt": row.get("published_summary", ""),
                "href": "/#news", "ref_href": "/data/cloud-articles/" + sha256(ident_value.encode()).hexdigest() + ".json",
            })
    elif ident == "announcements":
        for row in facts.get("titles", [])[:5]:
            ident_value = row.get("id")
            if not isinstance(ident_value, str) or not ident_value or not row.get("title"):
                continue
            rows.append({
                "id": ident_value, "kind": "公告", "title": row["title"],
                "date": date_of(row.get("date", "")),
                "meta": " · ".join(filter(None, (row.get("name", ""), row.get("code", "")))),
                "excerpt": row.get("summary", ""), "href": "/#news-announcements", "ref_href": "",
            })
    elif ident == "reports":
        seen = set()
        for detail in facts.get("matched_reports", []):
            report = detail.get("report") or {}
            ident_value = report.get("id")
            if not isinstance(ident_value, str) or not REPORT_ID.fullmatch(ident_value) or ident_value in seen:
                continue
            seen.add(ident_value)
            rows.append({
                "id": ident_value, "kind": "研报", "title": report.get("title", ""),
                "date": date_of(report.get("date", "")),
                "meta": " · ".join(filter(None, (report.get("broker", ""), report.get("author", "")))),
                "excerpt": (detail.get("published_summary") or "")[:220],
                "href": detail.get("href") or "/reports/?" + urlencode({"id": ident_value}), "ref_href": "",
            })
        for row in facts.get("titles", []):
            ident_value = row.get("id")
            if not isinstance(ident_value, str) or not REPORT_ID.fullmatch(ident_value) or ident_value in seen or len(rows) >= 5:
                continue
            seen.add(ident_value)
            rows.append({
                "id": ident_value, "kind": "研报", "title": row.get("title", ""),
                "date": date_of(row.get("date", "")), "meta": "研报目录",
                "excerpt": "", "href": row.get("href") or "/reports/?" + urlencode({"id": ident_value}), "ref_href": "",
            })
    return [row for row in rows if row["title"]]


class ResearchConnection(HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(RESEARCH_SOCKET)


def research_record(kind, code):
    if not valid_subject(kind, code):
        return {}
    try:
        with closing(ResearchConnection("localhost", timeout=2)) as client:
            client.request("GET", f"/data/asset-research/{kind}/{code}.json")
            with client.getresponse() as response:
                if response.status != 200:
                    return {}
                raw = response.read(2_000_001)
                value = json.loads(raw) if len(raw) <= 2_000_000 else {}
        return value if isinstance(value, dict) else {}
    except (OSError, HTTPException, ValueError):
        return {}


def read(where, filename):
    try:
        with (where / filename).open("rb") as stream:
            raw = stream.read(MAX_FILE + 1)
        value = json.loads(raw) if len(raw) <= MAX_FILE else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def normalized(text):
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def relevance(terms, *fields):
    """Rank exact identifiers/titles ahead of matches in supporting fields."""
    fields = [normalized(value) for value in fields if isinstance(value, str) and value]
    return max((4 if word in fields else
                3 if fields and word in fields[0] else
                1 if any(word in field for field in fields) else 0
                for word in map(normalized, terms) if word), default=0)


def report_sections(pages, question, visible_page, *, limit=4, chars=4200):
    query = normalized(question)
    terms = set(re.findall(r"[a-z0-9]{2,}", query))
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        terms.update(run[i:i+2] for i in range(len(run)-1))
    explicit = re.search(r"第(\d{1,3})页",query)
    requested = int(explicit[1]) if explicit else visible_page
    ranked = []
    for row in pages:
        number, text = row["page"], row["text"]
        body = normalized(text)
        score = sum(term in body for term in terms)
        ranked.append((number != requested,-score,number,text))
    return [{"page":number,"text":text[:chars]} for _,_,number,text in sorted(ranked)[:limit]]


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def date_of(value):
    return value[:10] if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value[:10]) else ""


def clock(value, now):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(now.tzinfo) if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def project(row, fields):
    return {key: row[key] for key in fields if key in row and row[key] is not None}


def matches(row, term):
    code = row.get("id", row.get("code", ""))
    term = normalized(term)
    return bool(term and any(term in normalized(value) for value in
                (code, row.get("name", ""), *ALIASES.get(code, ()))))


class Published:
    def __init__(self, now, state=STATE, site=SITE):
        self.now, self.state, self.site = now, state, site
        self.out = state / "quote-output"
        self.cache = {}
        self.asset_cache = {}

    def load(self, where, filename):
        key = (where, filename)
        if key not in self.cache:
            self.cache[key] = read(where, filename)
        return self.cache[key]

    def global_history(self, code, requested=None):
        market = self.load(self.out, "daily-market.json").get("global_indices", {}).get(code, {})
        return [row for row in market.get("history", []) if (day := date_of(row.get("date")))
                and day <= self.now.date().isoformat() and (not requested or day == requested)]

    def market(self, terms, requested):
        live = self.load(self.out, "live-quotes.json")
        daily = self.load(self.out, "daily-market.json")
        ths = self.load(self.site, "ths-sector-catalog.json")
        trends = self.load(self.out, "trends.json")
        active = self.load(self.out, "stock-quotes.json")
        today = self.now.date().isoformat()
        index_catalog = {r["id"]: r["name"] for r in trends.get("market", []) + live.get("market", [])}
        sw_catalog = {r["code"]: r["name"] for r in ths.get("sw_items", [])}
        bars = daily.get("bars", {})
        estimate = {r["id"]: r for r in active.get("board_estimates", [])}
        live_indices = {r["id"]: r for r in live.get("market", [])}
        day = requested or max((value for value in (
            date_of(daily.get("trade_date")), *(date_of(r.get("quote_date")) for r in live_indices.values())
        ) if value <= today), default="")

        def quote(code, name, observed, basis, *, include_sample=False):
            suffix = code[2:] + "." + code[:2].upper() if code[:2] in {"sh", "sz"} else code
            close = bars.get(code, {}).get(day) or bars.get(suffix, {}).get(day, {})
            fields = {"id": code, "name": name}
            when = clock(observed.get("quote_timestamp"), self.now)
            sampled = {}
            if observed.get("quote_date") == day and when and when <= self.now and number(observed.get("change_pct")):
                sampled = {"as_of": when.isoformat(), "basis": basis,
                           **project(observed, ("price", "change_pct", "amount", "sample_count", "sample_size", "weight_coverage"))}
            if number(close.get("change_pct")):
                return {**fields, "as_of": day, "basis": "已发布收盘日线",
                        **project(close, ("close", "change_pct", "amount")),
                        **({"reference_sample": sampled} if include_sample and sampled else {})}
            return {**fields, **sampled} if sampled else None

        indices = [r for code, name in index_catalog.items()
                   if (r := quote(code, name, live_indices.get(code, {}), "页面行情快照", include_sample=True))]
        industries = [r for code, name in sw_catalog.items() if (r := quote(
            code, name, estimate.get(code, {}), "盘后权重股估算" if estimate.get(code, {}).get("estimate_phase") == "close" else "盘中权重股估算"))]
        industries.sort(key=lambda r: r["change_pct"], reverse=True)
        breadth = {label: sum(predicate(r["change_pct"]) for r in industries) for label, predicate in
                   (("上涨", lambda v: v > 0), ("下跌", lambda v: v < 0), ("持平", lambda v: v == 0))}
        references = self.load(self.out, "sector-reference.json").get("quotes", {})
        samples = self.load(self.out, "board-live-summary.json")
        hot = self.load(self.out, "sector-hot.json")
        hot_rows = hot.get("items", []) if hot.get("trade_date") == day else []
        market_boards = []
        for row in ths.get("items", []):
            code = row["code"]
            if not any(matches(row, term) for term in terms) and not any(item["code"] == code for item in hot_rows):
                continue
            official = references.get(code, {})
            sampled = samples.get("items", {}).get(code, {})
            match = next((item for item in hot_rows if item["code"] == code), {})
            fields = {"id": code, "name": row["name"], "rank": match.get("rank")}
            at = clock(sampled.get("quote_at"), self.now)
            if official.get("quote_date") == day and number(official.get("change_pct")):
                market_boards.append({**fields, "as_of": day, "basis": "同花顺收盘行情",
                                      **project(official, ("price", "change_pct"))})
            elif (samples.get("trade_date") == day and at and at <= self.now and at.date().isoformat() == day
                  and number(sampled.get("change_pct")) and number(sampled.get("quoted_count"))
                  and number(sampled.get("total_count")) and sampled["total_count"] > 0
                  and sampled["quoted_count"] / sampled["total_count"] >= .8):
                market_boards.append({**fields, "as_of": at.isoformat(), "basis": "成分股等权估算",
                                      **project(sampled, ("change_pct", "quoted_count", "total_count"))})
            elif number(match.get("change_pct")):
                market_boards.append({**fields, "as_of": day, "basis": "同花顺热榜快照",
                                      **project(match, ("change_pct",))})
        market_boards.sort(key=lambda row: (not any(matches(row, t) for t in terms), row["rank"] or 100))
        matched = [r for r in industries if any(matches(r, t) for t in terms)]
        selected = list({r["id"]: r for r in [*matched, *industries[:4], *industries[-4:]]}.values())
        global_quotes = []
        for code, (name, market) in GLOBAL_INDICES.items():
            if history := self.global_history(code, requested):
                bar = history[-1]
                global_quotes.append({"id": code, "name": name, "market": market, "as_of": bar["date"],
                                      "basis": "当地交易日收盘日线", **project(bar, ("close", "change_pct"))})
        facts = {"session": day, "indices": indices, "industry_coverage": [len(industries), len(sw_catalog)],
                 "industry_breadth": breadth, "selected_industries": selected[:16], "ths_boards": market_boards[:14],
                 "global_indices": global_quotes}
        domestic_count = len(indices) + len(industries) + len(market_boards[:14])
        latest = max((row["as_of"] for row in global_quotes), default="") if not domestic_count else day
        return Evidence("指数与板块行情", "./#overview", latest, domestic_count + len(global_quotes), facts)

    def trend(self, terms, requested):
        packet = self.load(self.out, "trends.json")
        market = packet.get("market", [])
        boards = packet.get("boards", [])
        selected = []
        for term in terms:
            candidates = [row for row in market + boards if matches(row, term)]
            candidates.sort(key=lambda row: normalized(row["name"]) != normalized(term))
            selected.extend(candidates[:4])
        if not terms:
            selected = market
        selected = list({row["id"]: row for row in selected}.values())[:12]

        def trend_row(row):
            windows = {length: {**project(item, ("start_date", "return_pct", "reason")),
                                "trend": STATES.get(item.get("state"))}
                       for length, item in row.get("windows", {}).items() if length in {"5", "10", "20"}}
            return {**project(row, ("id", "name", "as_of", "group")), "windows": windows}

        result = []
        for row in selected:
            if not row.get("as_of") or (requested and row["as_of"] != requested):
                continue
            leaders = []
            for leader in row.get("leaders", [])[:3]:
                record = packet.get("stocks", {}).get(leader["id"], {})
                if record.get("as_of") == row["as_of"]:
                    leaders.append(trend_row({**leader, **record}))
            result.append({**trend_row(row), "leaders": leaders,
                           **project(row, ("rank_basis", "rank_date", "rank_coverage"))})
        latest = max((row["as_of"] for row in result), default="")
        return Evidence("趋势观察室", "./#trend", latest, len(result),
                        {"snapshot_expected_date": packet.get("expected_date"), "requested": requested, "rows": result})

    def news(self, terms, requested):
        packet = self.load(self.state / "request-usage", "public-news.json")
        articles = {}
        for row in packet.get("recent_articles", []) + packet.get("articles", []):
            published = clock(row.get("published_at"), self.now)
            if not published or published > self.now or (requested and published.date().isoformat() != requested):
                continue
            review = row.get("llm_analysis") or {}
            if row.get("credibility") == "rumor" or review.get("fact_status") == "rumor" or not row.get("title"):
                continue
            summary = review.get("summary") or row.get("summary") or ""
            ident = row.get("id")
            if not isinstance(ident, str) or not ident:
                continue
            score = relevance(terms, row["title"], ident, summary)
            if terms and not score:
                continue
            articles[ident] = (score, published, row, {**project(row, ("id", "title", "source", "credibility")),
                                                       "published_at": published.isoformat(),
                                                       "published_summary": summary[:300],
                                                       "fact_status": review.get("fact_status")})
        ranked = sorted(articles.values(), key=lambda item: (item[0], item[1]), reverse=True)[:14]
        if terms or requested:
            for _, _, row, fields in ranked[:4]:
                filename = sha256(row["id"].encode()).hexdigest() + ".json"
                if row.get("article_text_url") == "./data/cloud-articles/" + filename:
                    body = self.load(self.state / "news-output" / "articles", filename)
                    if body.get("id") == row["id"] and body.get("status") == "complete" and isinstance(body.get("full_text"), str):
                        fields["original_excerpt"] = body["full_text"][:2200]
        latest = max((at.date().isoformat() for _, at, _, _ in ranked), default="")
        return Evidence("A股关联资讯", "./#news", latest, len(ranked), {"articles": [fields for _, _, _, fields in ranked]})

    def announcements(self, terms, requested):
        today = self.now.date().isoformat()
        monday = (self.now.date() - timedelta(days=self.now.weekday())).isoformat()
        latest = self.load(self.out, "company-announcements.json")
        day = date_of(latest.get("trade_date"))
        if requested:
            packet = latest if requested == day else self.load(self.out / "company-announcements", requested + ".json")
        elif terms:
            packet = self.load(self.out, "company-announcements-week.json")
            if packet.get("week_start") != monday:
                packet = latest if day >= monday else {}
        else:
            packet = latest
        rows = []
        for row in packet.get("announcements", []):
            row_day = date_of(row.get("date"))
            published = clock(row.get("published_at"), self.now)
            if not row_day or row_day > today or (published and published > self.now):
                continue
            target = requested or (None if terms else day)
            if (target and row_day != target) or (not target and row_day < monday):
                continue
            code = row.get("code", "")
            score = relevance(terms, row.get("title"), row.get("id"), row.get("name"), code, code.split(".")[0])
            if score or not terms:
                rows.append((score, published.isoformat() if published else row_day, row))
        rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = []
        for _, _, row in rows[:10]:
            fields = project(row, ("id", "title", "name", "code", "date", "published_at"))
            if (terms or requested) and (ident := fields.get("id")):
                analysis = self.load(self.out / "announcement-analysis", fields["date"] + ".json")
                if analysis.get("source") == "announcement_analysis" and analysis.get("date") == fields["date"]:
                    summary = analysis.get("items", {}).get(ident) or {}
                    fields.update(project(summary, ("summary", "stock", "sector", "evidence")))
                    fields["summary_basis"] = summary.get("basis")
            selected.append(fields)
        digest = packet.get("digest") if rows and not terms else None
        as_of = max((row["date"] for row in selected), default="")
        return Evidence("上市公司公告", "/#news-announcements", as_of, len(rows),
                        {"total":len(rows), "titles":selected,
                         "title_based_digest":digest.get("summary") if digest else None})

    def report_detail(self, ident, requested, question, visible_page=None, *, primary=False):
        if not isinstance(ident, str) or not REPORT_ID.fullmatch(ident):
            return {}
        packet = self.load(self.site / "reports" / "items", ident + ".json")
        report = packet.get("report") or {}
        day = date_of(report.get("date"))
        if (packet.get("source") != "primary_research_reports" or report.get("id") != ident
                or not day or day > self.now.date().isoformat() or (requested and requested != day)):
            return {}
        digest = packet.get("summary") or {}
        original = self.load(self.state / "research-reports" / "evidence", ident + ".json")
        pages = original.get("pages", []) if original.get("id") == ident and original.get("url") == report.get("url") else []
        options = {} if primary else {"limit": 2, "chars": 2000}
        return {
            "report": project(report, ("id", "title", "date", "broker", "author", "company", "code", "type", "sectors")),
            "href": "/reports/?" + urlencode({"id": ident}),
            "published_summary": digest.get("text"), "summary_basis": digest.get("basis"),
            "original_pdf_total_pages": original.get("total_pages") if pages else None,
            "original_pdf_indexed_pages": len(pages),
            "original_pdf_excerpts": report_sections(pages, question, visible_page, **options),
        }

    def reports(self, terms, requested, context=None, question=""):
        context = context or {}
        folder = self.site / "reports"
        if not terms and context.get("page") == "report-reader":
            detail = self.report_detail(context.get("report_id"), requested, question, context.get("report_page"), primary=True)
            if not detail:
                return Evidence("当前研报", "/#reports", "", 0, {})
            report = detail["report"]
            return Evidence("当前研报 · " + report["title"], detail["href"], report["date"], 1, detail)

        ids = []
        words = []
        for term in terms:
            link = re.fullmatch(r"(?:https?://[^/]+)?/reports/\?id=([a-f0-9]{24})", term)
            if link or REPORT_ID.fullmatch(term):
                ids.append(link[1] if link else term)
            else:
                words.append(term)

        index = self.load(folder,"index.json")
        rows = []
        page_count = max(0,min(index.get("page_count",0),100))
        scan = page_count if words or requested else min(page_count, 1) if not ids else 0
        for number in range(1, scan + 1):
            for report in self.load(folder/"pages",str(number)+".json").get("reports",[]):
                ident, day = report.get("id", ""), date_of(report.get("date"))
                if (not REPORT_ID.fullmatch(ident) or ident in ids or not day
                        or day > self.now.date().isoformat() or (requested and day != requested)):
                    continue
                score = relevance(words, report.get("title"), *report.get("sectors", []))
                if words and score < 4:
                    metadata = read(folder / "items", ident + ".json").get("report") or {}
                    code = metadata.get("code") or ""
                    score = max(score, relevance(words, report.get("title"), metadata.get("company"),
                                                 code, code.split(".")[0]))
                if words and not score:
                    continue
                rows.append((score, day, report))
        rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
        targets = list(dict.fromkeys([*ids, *(row["id"] for _, _, row in rows[:3])]))[:3] if terms else []
        details = []
        for ident in targets:
            if detail := self.report_detail(ident, requested, question + " " + " ".join(words),
                                            primary=len(targets) == 1):
                details.append(detail)
        direct = [item["report"] for item in details if item["report"]["id"] in ids]
        titles = [
            {**project(report, ("id", "title", "date", "sectors")), "href": "/reports/?" + urlencode({"id": report["id"]})}
            for report in [*direct, *(row for _, _, row in rows[:10])]][:10]
        latest = max((row["date"] for row in titles),default="")
        count = (len(rows) + len(direct)) if terms or requested else index.get("count",0)
        href = details[0]["href"] if len(details) == 1 else "/#reports"
        return Evidence("查阅本站研报" if terms else "研报目录", href,latest,count,{
            "total":count,"titles":titles,"matched_reports":details,
            "popular_sectors":[row["name"] for row in index.get("groups",[])],
        })

    def macro(self, terms, requested, context=None):
        packet = self.load(self.out, "economic-calendar.json")
        indicators = packet.get("indicators", {})
        subject = [(code, row) for code, row in indicators.items() if not terms or
                   any(normalized(t) in normalized(code + " " + row["name"] + " " + row["country"]) for t in terms)]
        selected = {code for code, _ in subject}
        if not terms and context and context.get("indicator") in indicators:
            current = context["indicator"]
            subject = [(current, indicators[current])]
            selected = {current}
            requested = requested or context.get("indicator_date")
        output = []
        for event in packet.get("events", []):
            code = event.get("indicator")
            when = clock(event.get("at"), self.now)
            if code not in selected or not when or (requested and when.date().isoformat() != requested):
                continue
            if not requested and not self.now - timedelta(days=45) <= when <= self.now + timedelta(days=21):
                continue
            published = when <= self.now
            info = indicators[code]
            release = event.get("release_analysis") or {}
            output.append((when, {"name": info["name"], "country": info["country"], "at": when.isoformat(),
                                  "period": event.get("period"), "status": "已过公布时点" if published else "日程待公布",
                                  "levels": [project(m, ("label", "value", "unit", "basis")) for m in event.get("release_levels", [])] if published else [],
                                  "metrics": [project(m, ("label", "value", "unit", "basis"))
                                              for m in (event.get("release_data") or {}).get("metrics", [])] if published else [],
                                  "published_summary": project(release, ("summary", "impact")) if published else {}}))
        passed = sorted((pair for pair in output if pair[0] <= self.now), key=lambda pair: pair[0], reverse=True)[:9]
        upcoming = sorted((pair for pair in output if pair[0] > self.now), key=lambda pair: pair[0])[:5]
        latest = passed[0][0].date().isoformat() if passed else ""
        return Evidence("重要经济指标日历", "./#overview", latest, len(passed) + len(upcoming),
                        {"events": [row for _, row in passed + upcoming],
                         "definitions": [{"code": code, **project(row, ("name", "definition", "formula"))} for code, row in subject]})

    def subjects(self, terms, context):
        if not terms:
            return [(row["type"], row["code"]) for row in (context or {}).get("subjects", [])[:3]]
        found = {}
        catalogs = self.load(self.site, "ths-sector-catalog.json")
        dashboard = self.load(self.site, "view.json")
        names = {(kind, code): row.get("name", "") for kind, rows in (
            ("sector", catalogs.get("items", []) + catalogs.get("sw_items", []) + dashboard.get("sectors", [])),
            ("index", dashboard.get("market", [])),
            ("stock", self.load(self.out, "board-stock-quotes.json").get("stocks", [])
                      + self.load(self.out, "stock-quotes.json").get("stocks", [])),
        ) for row in rows if (code := self.subject_code(kind, row.get("code") or row.get("id", "")))}
        names.update({("index", code): name for code, (name, _) in GLOBAL_INDICES.items()})
        current = [(row["type"], row["code"]) for row in (context or {}).get("subjects", [])]
        for term in terms:
            text = normalized(term)
            code = term.upper()
            exact_current = [item for item in current if normalized(names.get(item, "")) == text or item[1] == code]
            exact_named = [item for item, name in names.items() if normalized(name) == text]
            alias_named = [item for item in names if any(normalized(alias) == text for alias in (
                *ALIASES.get(item[1], ()), *ALIASES.get(item[1][-2:].lower() + item[1][:6], ())))]
            explicit = [item for item in names if item[1] == code]
            if not explicit:
                candidates = [(kind, code) for kind in CODES if valid_subject(kind, code)]
                if len(candidates) == 1:
                    explicit = candidates
                else:
                    explicit = [item for item in candidates if self.load(
                        self.out, f"asset-research/{item[0]}/{item[1]}.json").get("code") == code]
                    if not explicit and ("stock", code) in candidates:
                        stock_rows = self.load(self.out, "valuation-stock.json").get("items", {})
                        if code in stock_rows:
                            explicit = [("stock", code)]
                if re.fullmatch(r"[0-9]{5,6}", code):
                    explicit = [item for item in names if item[1].split(".")[0] == code]
                    if not explicit:
                        stocks = self.load(self.out, "valuation-stock.json").get("items", {})
                        explicit = [("stock", code+suffix) for suffix in (".SH", ".SZ", ".BJ") if code+suffix in stocks]
            for item in exact_current or exact_named or alias_named or explicit:
                found.setdefault(item, names.get(item))
        return list(found)[:6]

    @staticmethod
    def subject_code(kind, code):
        if kind == "index" and re.fullmatch(r"(?:sh|sz)[0-9]{6}", code, re.I):
            code = code[2:] + "." + code[:2].upper()
        return code if valid_subject(kind, code) else None

    def metric_definitions(self, terms, requested, context=None):
        rows = list(CATALOG.values())
        if terms:
            def matching(row, term):
                word = normalized(term)
                names = (row["key"], *METRIC_ALIASES.get(row["key"], ()))
                if word in normalized(row["label"]) or any(word == normalized(name) for name in names):
                    return True
                return any(re.search(r"[\u4e00-\u9fff]", name) and normalized(name) in word for name in names)

            rows = [row for row in rows if any(matching(row, term) for term in terms)]
        return Evidence("站内指标公式与释义", "/metrics/", "", len(rows), {"metrics": rows})

    def instrument(self, terms, requested, context=None):
        subjects = self.subjects(terms, context)
        data = []
        through = self.now.date().isoformat()
        daily = self.load(self.out, "daily-market.json")
        stocks = {
            row["id"]: row for file in ("board-stock-quotes.json", "stock-quotes.json")
            for row in self.load(self.out, file).get("stocks", []) if row.get("id")
        } if any(kind == "stock" for kind, _ in subjects) else {}
        for kind, code in subjects:
            if kind == "index" and code in GLOBAL_INDICES:
                name, market = GLOBAL_INDICES[code]
                history = self.global_history(code, requested)
                quote = {}
                if history:
                    bar = history[-1]
                    quote = {"price": bar["close"], "quote_at": bar["date"], "quote_basis": "当地交易日收盘点位"}
                    if number(bar.get("change_pct")):
                        quote.update(change_pct=bar["change_pct"], change_at=bar["date"], change_basis="当地交易日收盘涨跌幅")
                data.append({"type": kind, "code": code, "name": name, "market": market, "quote": quote,
                             "metrics": [], "recent_daily": history[-22:], "href": "/#overview"})
                continue
            target = kind, code
            if target not in self.asset_cache:
                cached = self.load(self.out, f"asset-research/{kind}/{code}.json")
                fetched = research_record(kind, code) if self.state == STATE else {}
                self.asset_cache[target] = fetched or cached
            packet = self.asset_cache[target]
            if packet.get("code") != code or packet.get("type") != kind:
                packet = {}
            roster = self.load(self.out, f"sector-members/{code}.json") if kind == "sector" else None
            valuation = asset_metrics(kind, code, through, lambda name: self.load(self.out, name), roster)
            saved = packet.get("metrics", [])
            if requested:
                valuation = [row for row in valuation if date_of(row.get("as_of")) == requested]
                saved = [row for row in saved if date_of(row.get("as_of")) == requested]
            fields = ("key", "label", "value", "unit", "as_of", "basis", "missing_reason",
                      "sample_count", "total_count", "coverage_pct", "membership_as_of")
            quote = project(packet, ("price", "change_pct", "quote_at", "change_at", "quote_basis", "change_basis"))
            for value, stamp, basis in (("price", "quote_at", "quote_basis"), ("change_pct", "change_at", "change_basis")):
                at = quote.get(stamp)
                day, observed = date_of(at), clock(at, self.now)
                if not number(quote.get(value)) or not day or day > through or (observed and observed > self.now) or (requested and requested != day):
                    for key in (value, stamp, basis):
                        quote.pop(key, None)
            snapshot = stocks.get(code) or daily.get("stocks", {}).get(code, {}) if kind == "stock" else {}
            at = clock(snapshot.get("quote_timestamp"), self.now)
            if at and at <= self.now and (not requested or requested == at.date().isoformat()):
                for key, stamp, basis in (("price", "quote_at", "quote_basis"), ("change_pct", "change_at", "change_basis")):
                    if key not in quote and number(snapshot.get(key)):
                        quote.update({key: snapshot[key], stamp: at.isoformat(), basis: "已发布行情快照"})
            if requested and requested <= through:
                legacy_id = code[-2:].lower() + code[:6] if kind == "index" else code
                bars = daily.get("bars", {})
                bar = bars.get(code, {}).get(requested) or bars.get(legacy_id, {}).get(requested, {})
                history = next((row for row in (packet.get("chart") or {}).get("daily", {}).get("rows", [])
                                if row.get("day") == requested), {})
                close = bar.get("close", history.get("raw_close"))
                if number(close):
                    quote.update(price=close, quote_at=requested, quote_basis="已发布收盘价")
                if number(bar.get("change_pct")):
                    quote.update(change_pct=bar["change_pct"], change_at=requested, change_basis="已发布收盘日线")
            metrics = [project(row, fields) for row in valuation]
            metrics.extend(project(row, fields) for row in saved
                           if row.get("key") in CATALOG and row.get("group") != "valuation")
            for metric in metrics:
                metric.update(project(CATALOG[metric["key"]], ("formula", "description")))
            name = packet.get("name") or snapshot.get("name") or code
            href = "/sector/?" + urlencode({"code": code}) if kind == "sector" else "/asset/?" + urlencode({"type": kind, "code": code})
            data.append({"type": kind, "code": code, "name": name, "quote": quote, "metrics": metrics, "href": href})
        available = [row for row in data if row["quote"] or any(number(m.get("value")) for m in row["metrics"])]
        latest = max((date_of(stamp) for item in available for stamp in [
            item["quote"].get("quote_at"), item["quote"].get("change_at"),
            *(m.get("as_of") for m in item["metrics"] if number(m.get("value")))]), default="")
        href = data[0]["href"] if len(data) == 1 else "/#overview"
        return Evidence("研究详情行情与指标", href, latest, len(available), {"instruments": data})


SKILLS = {
    "market": ("核对指数与板块行情", Published.market),
    "trend": ("比较 5／10／20 日收盘趋势", Published.trend),
    "news": ("查阅已发布快讯", Published.news),
    "announcements": ("查阅上市公司公告", Published.announcements),
    "reports": ("查阅本站研报", Published.reports),
    "macro": ("核对经济指标与公布时间", Published.macro),
    "metrics": ("查阅指标公式与释义", Published.metric_definitions),
    "instrument": ("核对研究详情行情与指标", Published.instrument),
}
