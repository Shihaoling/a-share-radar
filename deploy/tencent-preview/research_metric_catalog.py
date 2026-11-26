"""Shared research metric definitions and allowlisted browser context."""

from datetime import date
import re

from pipeline.metrics import HELP
from global_indices import INDICES as GLOBAL_INDICES
from research_factors import METRICS as PRICE_METRICS
from research_valuation import METRICS as VALUATION_METRICS

PAGES = {
    "overview": "市场总览", "sectors": "板块雷达", "news": "A股关联资讯",
    "trend": "趋势观察室", "settings": "数据设置", "asset": "研究详情",
    "sector": "板块详情", "account": "账户", "admin": "控制台",
    "training-progress": "采集进度", "training-monitor": "训练进度", "metrics": "指标口径",
    "reports": "研报", "report-reader": "研报阅读", "funds": "基金专区",
}
REPORT_ID = re.compile(r"[a-f0-9]{24}")
CODES = {
    "index": re.compile(r"[0-9]{6}\.(?:SH|SZ)"),
    "stock": re.compile(r"(?:[0-9]{6}\.(?:SH|SZ|BJ|NQ)|[0-9]{5,6}\.HK)"),
    "sector": re.compile(r"[0-9]{6}(?:\.(?:SH|SZ|SI)|R?\.TI)"),
}
MODES = {
    "今日走势", "分时", "指数分时", "板块分时", "热度分时", "成分股分时", "部分成分分时", "热榜分时", "权重估算分时", "日线", "市值",
    "近 5 日", "近 10 日", "近 20 日", "20日动量", "20日年化波动率", "60日最大回撤", "相对沪深300",
}
SUBJECT_PAGES = {
    "overview": {"index", "sector"}, "sectors": {"sector"}, "trend": {"sector"}, "asset": {"stock", "index"}, "sector": {"sector"},
}
QUOTES = {
    "change_pct": ("涨跌幅", "%", "（当前价格 ÷ 参考昨收 − 1）× 100", "衡量相对参考昨收的变化。收盘时用收盘价。"),
    "relative_daily": ("日线相对沪深300", "个百分点", "标的当日日收益率 − 沪深300当日日收益率", "比较同日的标的和大盘表现。"),
    "period_return": ("5／10／20日累计涨跌幅", "%", "（区间末收盘 ÷ 区间起点收盘 − 1）× 100，或按同口径日收益连乘",
                      "观察最近相应交易日的总体涨跌。判断档位时还结合走势稳定性与相对波动。"),
    "market_amount": ("成交额", "元", "Σ（每笔成交价格 × 每笔成交数量）", "衡量成交金额规模，展示时可能折合为亿元。"),
    "volume": ("成交量", "股或手", "Σ 每笔成交数量", "衡量交易数量。应核对数据源用股还是手。"),
    "breadth_weighted": ("成分股估算涨跌幅", "%", "Σ 已获成分股涨跌幅 × 权重 ÷ Σ 已获成分权重；等权时取算术平均",
                        "估算受覆盖和权重影响。按页面的数据口径区分权重股估算、等权样本与发布方行情。"),
}
CATALOG = {
    **PRICE_METRICS, **VALUATION_METRICS,
    **{k: dict(key=k, label=v[0], unit=v[1], formula=v[2], description=v[3], group="quote")
       for k, v in QUOTES.items()},
    "constituent_pe_ttm": dict(key="constituent_pe_ttm", label=HELP["pe_ttm"]["title"]+"（完整财务口径）",
                              formula=HELP["pe_ttm"]["formula"], description=HELP["pe_ttm"]["meaning"]+HELP["pe_ttm"]["caveats"], group="quote"),
    "sharpe": dict(key="sharpe", label=HELP["sharpe"]["title"], formula=HELP["sharpe"]["formula"],
                   description=HELP["sharpe"]["meaning"]+HELP["sharpe"]["caveats"], group="quote"),
}
ALIASES = {
    "pe": ("市盈率", "pe"), "pe_ttm": ("市盈率", "ttm", "pe", "滚动市盈率", "pettm"),
    "pb": ("市净率", "pb"), "ps_ttm": ("市销率", "ps", "psttm"),
    "sample_pe_ttm": ("市盈率", "pe", "pe ttm"), "sample_pb": ("市净率", "pb"),
    "constituent_pe_ttm": ("市盈率", "pe", "pe ttm"), "dv_ttm": ("股息率", "分红"),
    "volatility20_pct": ("波动率", "volatility"), "beta60": ("贝塔", "beta", "β"),
    "momentum20_pct": ("动量", "momentum"), "drawdown60_pct": ("回撤", "drawdown"),
    "market_cap": ("市值", "总市值"), "member_market_cap": ("市值", "总市值"),
}


def valid_subject(kind, code):
    return (isinstance(kind, str) and kind in CODES and isinstance(code, str)
            and (CODES[kind].fullmatch(code) or (kind == "index" and code in GLOBAL_INDICES)))


def parse_page(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("page"), str) or raw["page"] not in PAGES:
        return None
    page = raw["page"]
    output = {"page": page, "title": PAGES[page]}
    if page == "report-reader":
        report_id, report_page = raw.get("report_id"), raw.get("report_page")
        if isinstance(report_id,str) and REPORT_ID.fullmatch(report_id):
            output["report_id"] = report_id
        if type(report_page) is int and 1 <= report_page <= 1000:
            output["report_page"] = report_page
    subjects = raw.get("subjects")
    if isinstance(subjects, list):
        output["subjects"] = []
        for item in subjects[:3]:
            if not isinstance(item, dict):
                continue
            kind, code = item.get("type"), item.get("code")
            if valid_subject(kind, code) and kind in SUBJECT_PAGES.get(page, set()):
                row = {"type": kind, "code": code}
                if row not in output["subjects"]:
                    output["subjects"].append(row)
    mode = raw.get("chart")
    if page in SUBJECT_PAGES and isinstance(mode, str) and mode in MODES:
        output["chart"] = mode
    study = raw.get("study")
    studies = {"momentum20": "momentum20_pct", "volatility20": "volatility20_pct", "beta60": "beta60", "drawdown60": "drawdown60_pct"}
    if page in ("asset", "sector") and mode == "日线" and isinstance(study, str) and study in studies:
        output["study"] = PRICE_METRICS[studies[study]]["label"]
    window = raw.get("window")
    if page == "trend" and type(window) is int and window in (5, 10, 20):
        output["window"] = window
    if page == "overview":
        indicator, when = raw.get("indicator"), raw.get("indicator_date")
        if isinstance(indicator, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,39}", indicator):
            output["indicator"] = indicator
            try:
                if date.fromisoformat(when).isoformat() == when:
                    output["indicator_date"] = when
            except (ValueError, TypeError):
                pass
    return output
