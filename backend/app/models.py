from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class RunStatus(StrEnum):
    PLANNING = "planning"
    SEARCHING = "searching"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class InsightType(StrEnum):
    PAIN_POINT = "pain_point"
    FEATURE_REQUEST = "feature_request"
    COMPARISON = "comparison"
    PRAISE = "praise"
    QUESTION = "question"
    NOISE = "noise"


class Sentiment(StrEnum):
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    POSITIVE = "positive"


class StepType(StrEnum):
    THOUGHT = "thought"
    ACTION_SEARCH = "action_search"
    OBSERVATION = "observation"
    SUFFICIENCY_CHECK = "sufficiency_check"
    SUMMARY = "summary"


class DataSource(StrEnum):
    """Which collector backend a run uses. Add one entry here per new collector
    (e.g. AMAZON, YOUTUBE) and register a factory in app/collectors — nothing
    else needs to change."""

    REDDIT_API = "reddit_api"
    REDDIT_SCRAPER = "reddit_scraper"
    JSON_UPLOAD = "json_upload"
    AMAZON = "amazon"
    YOUTUBE = "youtube"


@dataclass(slots=True)
class RunRecord:
    run_id: str
    product_category: str
    keywords: list[str]
    target_subreddits: list[str]
    status: RunStatus
    iteration_count: int
    max_iterations: int
    min_evidence_target: int
    evidence_count: int
    created_at: str
    updated_at: str
    data_source: DataSource = DataSource.REDDIT_API
    stop_reason: str | None = None
    error: str | None = None


@dataclass(slots=True)
class CollectedItem:
    """One post/comment/review/etc. normalized by a collector, before relevance analysis.

    This is the common currency every `Collector` implementation (Reddit, JSON
    upload, and future Amazon/YouTube/etc. collectors) must produce, and the
    only shape `react_agent` knows about — it has no idea which collector
    produced an item.
    """

    source_url: str
    subreddit: str  # source-agnostic grouping label: subreddit, store category, channel name, ...
    item_type: str  # "post" | "comment"
    post_id: str | None
    comment_id: str | None
    title: str
    body: str
    score: int
    comment_count: int
    created_at: str
    search_query: str


@dataclass(slots=True)
class Evidence:
    evidence_id: str
    run_id: str
    iteration: int
    source_url: str
    subreddit: str
    item_type: str
    title: str
    body: str
    score: int
    comment_count: int
    created_at: str
    fetched_at: str
    search_query: str
    insight_type: InsightType
    aspect: str
    sentiment: Sentiment
    quote: str
    confidence: float


@dataclass(slots=True)
class TraceEvent:
    run_id: str
    iteration: int
    step_type: StepType
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Report:
    run_id: str
    generated_at: str
    top_pain_points: list[dict[str, Any]]
    feature_requests: list[dict[str, Any]]
    praised_aspects: list[dict[str, Any]]
    competitor_mentions: list[dict[str, Any]]
    sentiment_breakdown: dict[str, int]
    recommended_actions: list[str]
    summary_markdown: str
    subreddits: list[str]
    subreddit_counts: dict[str, int]
    # Chinese counterparts of the two narrative (LLM-written) fields above, generated
    # alongside the English version in the same call — not translated after the fact.
    # Empty on reports generated before this field existed, or by the no-LLM-key
    # fallback narrative (see react_agent._summarize_fallback for its own bilingual
    # template, which *is* populated even without an LLM). Every other field
    # (aspects, quotes, source URLs) is language-agnostic or is a direct customer
    # quote that's deliberately never translated.
    recommended_actions_zh: list[str] = field(default_factory=list)
    summary_markdown_zh: str = ""
