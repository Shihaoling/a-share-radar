"""Show the same built-in templates used by the live MiniMax workers."""

from functools import lru_cache

from pipeline.minimax_prompts import (
    ANSWER_STAGE, DEFAULT_EFFORT, EFFORTS, MAX_NOTE, PLAN_STAGE, REVIEW_STAGE, STAGES,
    apply_note, effort_override, read_settings, stage_settings,
)


@lru_cache(maxsize=1)
def templates():
    from pipeline.minimax_news import system_prompt
    from cloud_news import DIGEST_PROMPT
    from company_announcements import PROMPT as ANNOUNCEMENT_PROMPT
    from announcement_analysis import PROMPT as ANNOUNCEMENT_ANALYSIS_PROMPT
    from economic_calendar import PROMPT as ECONOMIC_PROMPT, RELEASE_PROMPT
    from market_insights import THEME_PROMPT
    from market_reading import PROMPT as MARKET_PROMPT
    from research_assistant import ANSWER_PROMPT, PLAN_PROMPT, REVIEW_PROMPT, SUMMARY_PROMPT
    from research_report_library import PROMPT as RESEARCH_REPORT_PROMPT
    from training_news import BATCH_PROMPT
    from trend_brief import PROMPT as TREND_PROMPT
    from weekend_brief import PROMPT as WEEKEND_PROMPT

    return {"news_analysis":system_prompt(),"news_digest":DIGEST_PROMPT,"announcement_digest":ANNOUNCEMENT_PROMPT,
            "announcement_analysis":ANNOUNCEMENT_ANALYSIS_PROMPT,
            "research_report_summary":RESEARCH_REPORT_PROMPT,
            "training_single":system_prompt(),
            "training_labels":BATCH_PROMPT,"market_themes":THEME_PROMPT,"market_reading":MARKET_PROMPT,
            "market_weekend_brief":WEEKEND_PROMPT,"trend_brief":TREND_PROMPT,
            "economic_overview":ECONOMIC_PROMPT,"economic_release":RELEASE_PROMPT,
            PLAN_STAGE:PLAN_PROMPT,ANSWER_STAGE:ANSWER_PROMPT,REVIEW_STAGE:REVIEW_PROMPT,
            "research_assistant_summary":SUMMARY_PROMPT}


def prompt_item(stage,row):
    label, inputs = STAGES[stage]
    base = templates()[stage]
    note = row.get("note","")
    fixed = stage == REVIEW_STAGE
    override = None if fixed else effort_override(row)
    return {"id":stage,"label":label,"inputs":inputs,"system_prompt":apply_note(base,note),
            "note":note,"revision":row.get("revision",0),"saved_at":row.get("saved_at"),
            "reasoning_effort":override or DEFAULT_EFFORT[stage],
            "reasoning_effort_override":override,"default_reasoning_effort":DEFAULT_EFFORT[stage],
            "reasoning_effort_fixed":fixed}


def catalog(path):
    rows = read_settings(path)
    return {"max_note":MAX_NOTE,"reasoning_efforts":list(EFFORTS),
            "stages":[prompt_item(stage,stage_settings(rows,stage)) for stage in STAGES]}
