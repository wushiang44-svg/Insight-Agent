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
    CLAIM_EXTRACTION = "claim_extraction"
    SUMMARY = "summary"


class ClaimType(StrEnum):
    """One atomic claim extracted from a review/comment. See pipeline/claims.py.

    Scoped to Phase 1 (extraction only) of the Customer Demand Intelligence
    Pipeline — judgment fields (is_customer_need, need_type, reliability_score,
    ...) deliberately live on future ClaimAssessment/ReliabilityAssessment
    tables, not here, so this model only owns what Phase 1 actually computes.
    """

    PROBLEM = "problem"
    FEATURE_REQUEST = "feature_request"
    PRAISE = "praise"
    COMPARISON = "comparison"
    QUESTION = "question"
    OBSERVATION = "observation"
    SHIPPING_ISSUE = "shipping_issue"
    SELLER_SERVICE_ISSUE = "seller_service_issue"
    NOISE = "noise"


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
    # "v1": legacy aspect-only pipeline (pre-Claim). "v2": runs that also extract
    # atomic Claims (Customer Demand Intelligence Pipeline, Phase 1+). Existing
    # DB rows default to "v1" via migration; new runs are created as "v2".
    pipeline_version: str = "v2"


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


@dataclass(slots=True)
class Claim:
    """One atomic claim extracted from a single review/comment (Evidence).

    Replaces the "one review = one insight" assumption: a review can produce
    zero, one, or many Claims. `statement` is an AI-normalized interpretation,
    never the customer's verbatim words — the original text stays on the
    parent `Evidence` row (`body`/`quote`/`source_url`), and every Claim links
    back to it via `evidence_id`. Never render `statement` as if it were a
    direct quote; pair it with the parent Evidence's own text instead.
    """

    claim_id: str
    run_id: str
    evidence_id: str
    claim_type: ClaimType
    aspect_raw: str
    statement: str
    sentiment: Sentiment
    confidence: float
    extraction_method: str  # "llm" | "fallback_rules"
    created_at: str = field(default_factory=utc_now)
    subject: str | None = None
    explicit_request: str | None = None
    severity: float | None = None
    # A short verbatim span from the parent Evidence's own text that specifically
    # supports THIS claim -- distinct from Evidence.quote (a single quote picked
    # once for the whole review by the earlier analyze_item() stage, which may not
    # relate to any given claim). Nullable: the fallback path only sets it when it
    # can find a matching sentence, and the LLM path nulls it out if the model's
    # claimed excerpt isn't actually a substring of the source text (never trust an
    # unverified "verbatim" quote). Callers should fall back to Evidence.quote when
    # this is None, not fabricate one.
    source_excerpt: str | None = None
    # Phase 1.6 (within-review dedup): 1 means this claim was never merged with
    # another raw claim from the same Evidence. >1 means this claim is the
    # survivor of a merge -- merged_claim_ids/merged_excerpts hold provenance
    # for the raw claims that got absorbed into it. Never populated across
    # different Evidence rows -- cross-review merging is out of scope here and
    # remains a future phase's job (see claims.py's _merge_within_review docstring).
    merge_count: int = 1
    merged_claim_ids: list[str] | None = None
    merged_excerpts: list[str] | None = None
    # Nullable; populated by a future clustering phase (Phase 5), not Phase 1.
    canonical_category: str | None = None
