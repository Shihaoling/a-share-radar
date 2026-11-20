"""Read dated official releases and select contemporaneous news for the calendar."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from csv import DictReader
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from io import StringIO
import json
import re
import sqlite3
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from market_insights import ARTICLE_ID
from pipeline.news_trust import apply_source_whitelist
from pipeline.sentiment import _is_rumor
from trend_brief import timestamp

NBS_RELEASES = "https://www.stats.gov.cn/sj/zxfb/"
ALFRED = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"
HOSTS = {"www.stats.gov.cn", "www.bea.gov", "www.bls.gov", "blsmon1.bls.gov", "alfred.stlouisfed.org"}
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]
NUMBER = r"[−+-]?\d[\d,]*(?:\.\d+)?"
CN_CHANGE = rf"(?:由[^。；，,]{{1,28}}?转为)?(?:(上涨|上升|增长|提高|回升|下降|下跌|回落|降低)\s*({NUMBER})\s*[%％]|持平)"
NEWS_TERMS = {
    "cn_pmi": (r"采购经理|制造业PMI|中国制造业|出口订单", r"出口管制|供应链|关税|外贸|工业增加值"),
    "cn_cpi": (r"居民消费价格|中国.{0,8}CPI|核心CPI|食品价格|猪肉价格", r"消费需求|国内油价|成品油|通胀"),
    "cn_ppi": (r"工业生产者|中国.{0,8}PPI|出厂价格|工业品价格", r"原油|布伦特|供给|煤炭|天然气|铜价"),
    "cn_retail": (r"社会消费品|社零|中国零售|以旧换新|消费品零售额", r"促消费|汽车销量|家电|餐饮|消费补贴"),
    "us_gdp": (r"美国.{0,8}GDP|美国.{0,12}(经济增长|经济增速)|实际GDP", r"美联储|美国.{0,8}(消费|贸易|投资)|关税|美国政府停摆"),
    "us_pce": (r"核心PCE|PCE物价|PCE.{0,8}价格|个人消费支出|美国.{0,8}通胀", r"美联储|美国CPI|美国消费|布伦特"),
    "us_jobs": (r"非农|美国.{0,8}(失业率|就业)|ADP", r"美联储|初请失业金|美国.{0,8}(工资|裁员)"),
    "us_cpi": (r"美国.{0,8}(CPI|通胀|消费者价格)|美国核心CPI", r"美联储|美国PCE|美国.{0,8}房租|布伦特|原油"),
}


class ReleasePage(HTMLParser):
    def __init__(self, raw):
        super().__init__()
        self.blocks, self.links = [], []
        self.block, self.link, self.href = None, None, None
        self.ignored = None
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        if tag in {"script","style"}:
            self.ignored = tag
        if tag in ("p", "pre", "h1"):
            self.block = []
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.link = []

    def handle_data(self, text):
        if self.ignored:
            return
        if self.block is not None:
            self.block.append(text)
        if self.link is not None:
            self.link.append(text)

    def handle_endtag(self, tag):
        if tag==self.ignored:
            self.ignored = None
        if tag in ("p", "pre", "h1") and self.block is not None:
            self.blocks.append(" ".join("".join(self.block).split()))
            self.block = None
        if tag == "a" and self.link is not None:
            self.links.append((self.href, " ".join("".join(self.link).split())))
            self.link = self.href = None


def download(url):
    address = urlsplit(url)
    if address.scheme != "https" or address.hostname not in HOSTS:
        raise ValueError("unrecognized_release_source")
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; GuanlanCalendar/1.0)"})
    with urlopen(request, timeout=8) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname not in HOSTS:
            raise ValueError("unrecognized_release_redirect")
        raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("official_release_too_large")
    return raw.decode("utf-8-sig", "replace")


def fetch_release(url):
    return ReleasePage(download(url))


def point(label, number, unit="%"):
    return {"label": label, "value": float(number.replace(",", "").replace("−", "-")), "unit": unit}


def cn_change(label, text, pattern):
    match = re.search(pattern + CN_CHANGE, text)
    if not match:
        return None
    rate = match[2] or "0"
    if match[1] in {"下降","下跌","回落","降低"}:
        rate = "-" + rate.lstrip("−+-")
    return point(label, rate)


def en_change(label, text, pattern):
    match = re.search(pattern + rf"\s+(increased|rose|advanced|decreased|fell|declined|was unchanged)\b"
                      rf"(?:\s+(?:by\s+)?({NUMBER})\s+percent)?", text, re.I)
    if not match or not match[2] and match[1].lower() != "was unchanged":
        return None
    rate = match[2] or "0"
    if match[1].lower() in {"decreased", "fell", "declined"}:
        rate = "-" + rate.lstrip("−+-")
    return point(label, rate)


def chinese_points(code, period, text):
    months = re.fullmatch(r"\d{4} 年 (\d{1,2})(?:–(\d{1,2}))? 月", period)
    if not months:
        return []
    month = months[2] or months[1]
    month_label = f"{months[1]}[—–－-]{month}" if months[2] else rf"(?<![\d—–－-]){month}"
    if code == "cn_pmi":
        match = re.search(rf"{month_label}月份?，?制造业采购经理指数\s*[（(]PMI[）)]\s*为\s*({NUMBER})\s*[%％]", text)
        if not match:
            return []
        row = [point("制造业 PMI", match[1])]
        change = cn_change("较上月", text[match.end():match.end()+45].replace("个百分点", "%"), r"^[，,]比上月")
        if change:
            row.append({**change, "unit": "个百分点"})
        return row
    if code == "cn_retail":
        match = re.search(rf"{month_label}月份?[，,]\s*社会消费品零售总额\s*({NUMBER})\s*亿元", text)
        if not match:
            return []
        return [point("零售总额", match[1], "亿元"), cn_change("同比", text[match.end():match.end()+32], r"^[，,]\s*同比")]
    subject = "居民消费价格" if code == "cn_cpi" else "工业生产者出厂价格"
    sections = re.findall(rf"{month_label}月份?[，,]\s*(?:全国)?{subject}([^。]{{1,105}})", text)
    if not sections:
        return []
    changes = "。".join(sections[:4])
    if code == "cn_cpi":
        return [cn_change("同比", changes, r"同比"), cn_change("环比", changes, r"环比"),
                cn_change("核心 CPI 同比", text[:1700], r"(?:核心CPI|核心 CPI)\s*同比")]
    return [cn_change("同比", changes, r"同比"), cn_change("环比", changes, r"环比")]


def english_points(code, period, text):
    if code == "us_gdp":
        match = re.search(rf"Real gross domestic product \(GDP\) (increased|decreased) at an annual rate of ({NUMBER}) percent", text, re.I)
        if match:
            sign = "-" if match[1].lower() == "decreased" else ""
            return [point("实际 GDP 环比折年率", sign + match[2].lstrip("−+-"))]
        return []
    month = re.fullmatch(r"\d{4} 年 (?:\d{1,2}–)?(\d{1,2}) 月", period)
    if not month:
        return []
    name = MONTHS[int(month[1])-1]
    if code == "us_jobs":
        match = re.search(rf"Total nonfarm payroll employment (rose|increased|declined|decreased|fell) by ({NUMBER}) in {name}", text, re.I)
        if not match:
            return []
        rate = re.search(rf"(?:the )?unemployment rate [^.;]{{0,45}}? (?:at|to|was) ({NUMBER}) percent", text[match.end():match.end()+230], re.I)
        prefix = "-" if match[1].lower() in {"declined", "decreased", "fell"} else ""
        return [point("非农就业净增", prefix + match[2].lstrip("−+-"), "人"),
                point("失业率", rate[1]) if rate else None]
    if code == "us_cpi":
        top = text.split("Table A.", 1)[0][:6500]
        return [en_change("CPI 环比（季调）", top, r"Consumer Price Index for All Urban Consumers \(CPI-U\)"),
                en_change("CPI 同比", top, r"over the last 12 months, the all items index"),
                en_change("核心 CPI 环比（季调）", top, r"the index for all items less food and energy")]
    monthly = r"From the preceding month, (?:the )?PCE price index(?: for \w+)?"
    yearly = r"From the same month one year ago, (?:the )?PCE price index(?: for \w+)?"
    return [en_change("PCE 价格环比", text, monthly),
            en_change("PCE 价格同比", text, yearly),
            en_change("核心 PCE 价格环比", text, monthly + r"[^.]*?(?:\.\d+[^.]*)?\. Excluding food and energy, the PCE price index"),
            en_change("核心 PCE 价格同比", text, yearly + r"[^.]*?(?:\.\d+[^.]*)?\. Excluding food and energy, the PCE price index")]


def nbs_links(pages, events):
    found = {}
    for index, page in pages:
        for href, title in page.links:
            if not href or "解读" in title or "答记者问" in title:
                continue
            title = title.replace(" ", "")
            match = re.match(r"(\d{4})年(?:(\d{1,2})[—–－-])?(\d{1,2})月", title)
            half_year = re.match(r"(\d{4})年上半年社会消费品零售总额",title)
            if not match and not half_year:
                continue
            year,first,last = match.groups() if match else (half_year[1],"1","6")
            if "采购经理指数运行情况" in title:
                code = "cn_pmi"
            elif "居民消费价格" in title:
                code = "cn_cpi"
            elif "工业生产者出厂价格" in title:
                code = "cn_ppi"
            elif "社会消费品零售总额" in title:
                code = "cn_retail"
            else:
                continue
            url = urljoin(index,href)
            publication = re.search(r"/(\d{6})/t(\d{8})_", url)
            if not publication or urlsplit(url).hostname != "www.stats.gov.cn":
                continue
            for row in events.get(code, []):
                stamp = timestamp(row["at"])
                period = f"{year} 年 {int(last)} 月"
                if code == "cn_retail" and first == "1" and last == "2":
                    period = f"{year} 年 1–2 月"
                if code == "cn_retail" and first and stamp.strftime("%Y%m%d") == publication[2]:
                    period = row["period"]
                if row["period"] == period and stamp.strftime("%Y%m%d") == publication[2]:
                    found[release_id(row)] = url
    return found


def release_id(row):
    return f'{row["indicator"]}:{row["at"]}'


def alfred_result(code, period, released):
    match = re.fullmatch(r"(\d{4}) 年 (\d{1,2}) 月", period)
    if not match:
        return None
    year, month = map(int,match.groups())
    current = released.date().replace(year=year,month=month,day=1)
    previous = (current-timedelta(days=1)).replace(day=1)
    last_year = current.replace(year=year-1)
    vintage = released.strftime("%Y%m%d")
    series = ("PAYEMS","UNRATE") if code=="us_jobs" else ("CPIAUCSL","CPIAUCNS","CPILFESL")
    points, sources = [], []
    for ident in series:
        url = ALFRED+"?"+urlencode({"id":ident,"vintage_date":released.date(),"cosd":last_year,"coed":current})
        try:
            reader = DictReader(StringIO(download(url)))
            column = f"{ident}_{vintage}"
            if column not in (reader.fieldnames or []):
                continue
            values = {row["observation_date"]:row.get(column) for row in reader}
            level = Decimal(values.get(current.isoformat()) or ".")
            if not level.is_finite():
                continue
            if ident=="UNRATE":
                points.append(point("失业率",str(level)))
            else:
                baseline = last_year if ident=="CPIAUCNS" else previous
                earlier = Decimal(values.get(baseline.isoformat()) or ".")
                if ident=="PAYEMS":
                    points.append(point("非农就业净增",str((level-earlier)*1000),"人"))
                elif earlier>0:
                    label = {"CPIAUCSL":"CPI 环比（季调）","CPIAUCNS":"CPI 同比","CPILFESL":"核心 CPI 环比（季调）"}[ident]
                    change = ((level/earlier-1)*100).quantize(Decimal("0.1"),rounding=ROUND_HALF_UP)
                    points.append(point(label,str(change)))
            sources.append(url)
        except (InvalidOperation, ValueError, OSError):
            continue
    if not points or not sources or series[0] not in sources[0]:
        return None
    return {"metrics":points,"source_url":sources[0],"source_label":"圣路易斯联储 ALFRED · 发布日版本",
            "note":"按发布日版本计算。缺少上年或上月可比值的口径不展示。","source_links":sources}


def collect_one(row, nbs_urls, fallback):
    code = row["indicator"]
    url = row.get("release_url") or nbs_urls.get(release_id(row))
    if code in {"us_cpi", "us_jobs"}:
        token = "cpi" if code == "us_cpi" else "empsit"
        date = timestamp(row["at"]).strftime("%m%d%Y")
        url = f"https://www.bls.gov/news.release/archives/{token}_{date}.htm"
    if url:
        try:
            page = fetch_release(url)
            body = " ".join(page.blocks)
            points = chinese_points(code, row["period"], body) if code.startswith("cn_") else english_points(code, row["period"], body)
            if points and points[0]:
                return {"metrics": [item for item in points if item], "source_url": url}
        except (ValueError, OSError):
            pass
    seed = fallback.get(release_id(row))
    if seed:
        return seed
    if code in {"us_cpi","us_jobs"}:
        return alfred_result(code,row["period"],timestamp(row["at"]))
    return None


def collect_releases(events, now, fallback, backfill=False):
    due = [row for row in events if not row.get("release_data") and timestamp(row["at"]) <= now
           and (backfill or release_id(row) in fallback or timestamp(row["at"]) >= now-timedelta(days=14))]
    if not due:
        return
    missing = {}
    for row in due:
        if row["indicator"].startswith("cn_") and not row.get("release_url"):
            missing.setdefault(row["indicator"], []).append(row)

    def index_page(url):
        try:
            return url,fetch_release(url)
        except (ValueError, OSError):
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        urls = [NBS_RELEASES] + [urljoin(NBS_RELEASES,f"index_{n}.html") for n in range(1,17)] if missing else []
        listings = filter(None,executor.map(index_page,urls))
        nbs_urls = nbs_links(listings,missing)
        results = executor.map(lambda row:collect_one(row,nbs_urls,fallback),due)
        for row, data in zip(due, results):
            if data:
                row["release_data"] = data
                if not data.get("source_label"):
                    row.setdefault("release_url", data["source_url"])


def news_for_release(state, official_news, row, now):
    at = timestamp(row["at"])
    start = at-timedelta(days=6)
    end = min(now,at+timedelta(days=4))
    days = (start.date().isoformat(),end.date().isoformat())
    direct, related = (re.compile(p,re.I) for p in NEWS_TERMS[row["indicator"]])
    articles = [item for item in official_news if row["indicator"] in item["indicators"]]
    db_path = state/"news/news.sqlite3"
    try:
        if db_path.exists():
            with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro",uri=True,timeout=4)) as db:
                for ident, raw in db.execute("SELECT id,payload FROM articles WHERE day BETWEEN ? AND ? ORDER BY published DESC,id",
                                             days):
                    if ARTICLE_ID.fullmatch(ident):
                        articles.append(json.loads(raw))
    except (OSError, sqlite3.Error, ValueError):
        pass
    archive = state/"training-news/dataset.sqlite3"
    try:
        if archive.exists():
            with closing(sqlite3.connect(f"{archive.as_uri()}?mode=ro",uri=True,timeout=4)) as db:
                for published,title,body,label in db.execute(
                    "SELECT published_at,title,body,label FROM articles WHERE day BETWEEN ? AND ?",days
                ):
                    if not (direct.search(title) or related.search(title)):
                        continue
                    articles.append({"published_at":published,"title":title,"content":body,
                                     "source":"财联社（历史快讯）","credibility":"reliable",
                                     "archive":True,"llm_analysis":json.loads(label) if label else {}})
    except (OSError, sqlite3.Error, ValueError):
        pass
    selected = {}
    for article in articles:
        published = article.get("published_at") or ""
        day_only = isinstance(published,str) and len(published)==10
        posted = timestamp(published,date_only=day_only)
        if day_only and posted:
            posted -= timedelta(seconds=1)
        if not posted or not start <= posted <= end:
            continue
        title, url = article.get("title") or "", article.get("url") or ""
        if not title or (not article.get("archive") and not url.startswith(("https://","http://"))) or url == row.get("release_url"):
            continue
        if _is_rumor(article) or (article.get("llm_analysis") or {}).get("fact_status")=="rumor":
            continue
        apply_source_whitelist({"news":[article]})
        if article.get("credibility") not in {"official","reliable","trusted"} and not article.get("source_verified"):
            continue
        body = " ".join((article.get("full_text") or article.get("content") or article.get("summary") or title).split())
        match = direct.search(body[:1800]) or related.search(body[:1200])
        curated = "indicators" in article
        score = 8 if curated else 6 if direct.search(title) else 4 if related.search(title) else 2 if match else 0
        if not score:
            continue
        center = match.start() if match else 0
        text = body[max(0,center-70):center+230]
        rank = (score, bool(url), -abs((posted-at).total_seconds()))
        headline = re.sub(r"\W","",title.casefold())
        item = {"title":title[:150],"source":str(article.get("source") or "资讯来源")[:60],
                "published_at":published if day_only else posted.isoformat(),"url":url,"evidence":text}
        if headline not in selected or rank>selected[headline][0]:
            selected[headline] = (rank,item)
    ordered = sorted(selected.values(),key=lambda value:value[0],reverse=True)[:5]
    return [{"id":f"N{i}",**item} for i,(_,item) in enumerate(ordered,1)]
