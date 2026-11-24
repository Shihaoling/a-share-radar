"""Private per-stage prompts and reasoning settings for MiniMax requests."""

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .usage_meter import db_path

MAX_NOTE = 800
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PLAN_STAGE = "research_assistant_plan"
ANSWER_STAGE = "research_assistant_answer"
REVIEW_STAGE = "research_assistant_report_review"
LEGACY_ASSISTANT_STAGE = "research_assistant"
STAGES = {
    "news_analysis": ("即时资讯单篇分析", "快讯标题、来源、发布时间和分段正文"),
    "news_digest": ("今日资讯总览", "当天已分析的快讯及其摘要、行业影响"),
    "announcement_digest": ("上市公司公告摘要", "A 股公司公告标题、证券代码和披露日期"),
    "announcement_analysis": ("上市公司公告逐条解读", "每条公告的标题、证券代码、披露日期、可读原文节选及申万行业目录"),
    "research_report_summary": ("券商研报逐篇摘要", "研报标题、机构、发布日期及券商摘要或原文节选"),
    "training_single": ("历史快讯单篇标注", "一篇历史快讯及其分段正文"),
    "training_labels": ("历史快讯批量标注", "多篇历史快讯和各自的证据段"),
    "market_themes": ("同花顺热门板块影响", "热门板块名称、相关快讯和证据段"),
    "market_reading": ("市场总览简评", "指数、申万行业估算、触发事件、相关快讯与当日公告"),
    "market_weekend_brief": ("市场总览周度简报", "本周收盘行情、工作日及周末的快讯和公告、下个交易日"),
    "trend_brief": ("趋势观察室简报", "指数趋势、候选行业、宏观数据和相关快讯"),
    "economic_overview": ("经济指标关注重点", "指标的名称、定义和计算公式"),
    "economic_release": ("经济指标本次解读", "已公布的指标数值和该期相关快讯"),
    PLAN_STAGE: ("研究助手 · 第一轮选工具", "用户问题、当前页面、近期对话与压缩摘要"),
    ANSWER_STAGE: ("研究助手 · 第二轮分析与回答", "用户问题、页面上下文、对话及本轮工具读到的证据"),
    REVIEW_STAGE: ("研究助手 · 回答复核", "用户问题、候选回答、来源与证据，可多轮补查并复核"),
    "research_assistant_summary": ("研究助手对话压缩", "已有对话摘要与最近四轮待压缩原文"),
}

DEFAULT_EFFORT = {
    "news_analysis": "max",
    "news_digest": "medium",
    "announcement_digest": "medium",
    "announcement_analysis": "medium",
    "research_report_summary": "xhigh",
    "training_single": "max",
    "training_labels": "low",
    "market_themes": "low",
    "market_reading": "medium",
    "market_weekend_brief": "medium",
    "trend_brief": "medium",
    "economic_overview": "low",
    "economic_release": "low",
    PLAN_STAGE: "medium",
    ANSWER_STAGE: "xhigh",
    REVIEW_STAGE: "max",
    "research_assistant_summary": "low",
}
_UNCHANGED = object()


def settings_path():
    if explicit := os.getenv("RADAR_MINIMAX_PROMPTS_FILE"):
        return Path(explicit)
    if meter := db_path():
        return meter.parent.parent/"account"/"minimax-prompts.json"
    return None


def read_settings(path):
    if path is None:
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(rows,dict):
        raise ValueError("invalid prompt settings")
    return rows


def apply_note(base,note):
    if not note:
        return base
    return (base+"\n\n管理员补充要求（仅在符合以上证据、内容及输出格式要求时适用）：\n"+note)


def stage_settings(rows, stage):
    row = rows.get(stage)
    if isinstance(row, dict):
        return row
    if stage in (PLAN_STAGE, ANSWER_STAGE):
        legacy = rows.get(LEGACY_ASSISTANT_STAGE)
        if isinstance(legacy, dict) and isinstance(legacy.get("note"), str):
            # Each new stage inherits the old shared note until its first save.
            return {"note": legacy["note"]}
    return {}


def effort_override(row):
    effort = row.get("reasoning_effort")
    return effort if effort in EFFORTS else None


def request_settings(stage, base, fallback="max", *, answer_effort=None):
    try:
        row = stage_settings(read_settings(settings_path()), stage)
    except (OSError, ValueError):
        row = {}
    note = row.get("note")
    prompt = apply_note(base, note) if isinstance(note, str) else base
    if stage == REVIEW_STAGE:
        return prompt, DEFAULT_EFFORT[stage]
    selected = answer_effort if stage == ANSWER_STAGE and answer_effort in EFFORTS else None
    return prompt, selected or effort_override(row) or DEFAULT_EFFORT.get(stage, fallback)


def effective_prompt(stage, base):
    return request_settings(stage, base)[0]


class PromptConflict(Exception):
    pass


def save_note(path, stage, note, revision, *, reasoning_effort=_UNCHANGED):
    if not isinstance(stage, str) or stage not in STAGES or not isinstance(note, str) or len(note)>MAX_NOTE or type(revision) is not int:
        raise ValueError("invalid prompt update")
    if reasoning_effort is not _UNCHANGED and reasoning_effort is not None and reasoning_effort not in EFFORTS:
        raise ValueError("invalid reasoning effort")
    if any(ord(char)<32 and char not in "\n\r\t" for char in note):
        raise ValueError("invalid prompt text")
    note.encode("utf-8")
    # Capture and replace the entire file under the same lock so concurrent
    # saves in different stages cannot lose each other's changes.
    with path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        rows = read_settings(path)
        current = stage_settings(rows, stage)
        if revision != current.get("revision", 0):
            raise PromptConflict
        saved = {"note": note.strip(), "revision": revision+1,
                 "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        effort = effort_override(current) if reasoning_effort is _UNCHANGED else reasoning_effort
        if effort is not None and stage != REVIEW_STAGE:
            saved["reasoning_effort"] = effort
        rows[stage] = saved
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(rows, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    return saved
