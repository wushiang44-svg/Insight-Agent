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
    SCREENING = "screening"
    SUFFICIENCY_CHECK = "sufficiency_check"
    CLAIM_EXTRACTION = "claim_extraction"
    CATEGORIZATION = "categorization"
    CLAIMS_REPORT_ELIGIBILITY = "claims_report_eligibility"
    SUMMARY = "summary"


class ScreeningCategory(StrEnum):
    """What kind of feedback a review/comment is, per Phase 2's screen_item().
    Multi-label -- a review's ScreeningResult.categories can (and often should)
    contain more than one of these. Replaces analyze_item()'s single binary
    is_relevant verdict, which could discard a mixed review's real product
    signal because the dominant tone read as shipping/service noise. Only
    SPAM_OR_IRRELEVANT/LOW_INFORMATION-only content is discarded before Claim
    extraction -- see pipeline/screening.py."""

    PRODUCT_FEEDBACK = "product_feedback"
    SHIPPING_LOGISTICS = "shipping_logistics"
    SELLER_SERVICE = "seller_service"
    SPAM_OR_IRRELEVANT = "spam_or_irrelevant"
    LOW_INFORMATION = "low_information"


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


class CategoryStatus(StrEnum):
    """Lifecycle of a canonical_categories row (Phase 3 taxonomy). proposed ->
    approved is a human decision (see routes.py's curation endpoints);
    deprecated retires a category without deleting history. Never inferred
    from claims data -- always an explicit transition through Storage's
    transition methods, each of which writes a category_audit_log row."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class CategoryAuditAction(StrEnum):
    APPROVE = "approve"
    MERGE = "merge"
    DEPRECATE = "deprecate"
    RENAME = "rename"


class DataSource(StrEnum):
    """Which collector backend a run uses. Add one entry here per new collector
    (e.g. AMAZON, YOUTUBE) and register a factory in app/collectors — nothing
    else needs to change."""

    REDDIT = "reddit"
    # REDDIT_API / REDDIT_SCRAPER: superseded by REDDIT (the browser+CDP
    # collector) for new runs. Kept only so pre-existing stored runs using
    # these values keep resolving to their original collectors -- not offered
    # as new-run choices in the frontend anymore.
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
    # atomic Claims (Customer Demand Intelligence Pipeline, Phase 1+). "v3": runs
    # screened by screen_item() (Phase 2) instead of the old binary analyze_item()
    # gate -- evidence_count/Report contents can genuinely differ from v1/v2 runs
    # on the same source corpus, since mixed-content reviews that used to be
    # silently dropped now survive. "v4": runs whose Claims also go through
    # pipeline/taxonomy.py's categorize_claims() batch step (Phase 3) and,
    # when the Claims-report eligibility gate says so, get a Report built from
    # categorized Claims instead of raw Evidence (react_agent.py's
    # summarize()) -- still informational only, not itself the branch
    # decision (that's `report_inputs`/`claims` being non-empty). Existing DB
    # rows keep whatever version they were created with; new runs are created
    # as "v4".
    pipeline_version: str = "v4"


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
    # Phase 2 (review screening): the full multi-label category set screen_item()
    # assigned, e.g. ["shipping_logistics", "product_feedback"] for a mixed
    # review. Nullable -- Evidence created before Phase 2 has no screening data.
    # insight_type/aspect/sentiment above stay populated with equivalent legacy
    # semantics regardless, so the aggregate Report and check_sufficiency() never
    # need to know this field exists.
    screening_categories: list[str] | None = None
    is_mixed_content: bool = False


@dataclass(slots=True)
class TraceEvent:
    run_id: str
    iteration: int
    step_type: StepType
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


class ReportSource(StrEnum):
    """Which pipeline actually produced a Report -- Phase 3, Stage 7. Never
    constructed from an arbitrary string without validation: Report.__post_init__
    coerces `report_source` through this enum on every construction (both a
    fresh summarize() call and a row read back from storage), so an invalid
    value can never silently enter the database or exist as a live Report
    instance -- it raises ValueError immediately instead."""

    CLAIMS = "claims"
    LEGACY_EVIDENCE = "legacy_evidence"


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
    # Phase 3, Stage 6/7: new report sections, populated only by the Claims
    # path (react_agent._aggregate_claims_by_category / _build_report_inputs)
    # -- Evidence has no shipping_issue/seller_service_issue concept of its
    # own, so the legacy path always leaves these empty. Same aggregate-entry
    # dict shape as top_pain_points etc. (aspect/count/subreddit_count/
    # avg_confidence/sentiment_counts/example_quotes), plus category_status.
    shipping_issues: list[dict[str, Any]] = field(default_factory=list)
    seller_service_issues: list[dict[str, Any]] = field(default_factory=list)
    # Which pipeline produced this Report and, when it's the legacy fallback,
    # exactly why the Claims path wasn't used -- passed through verbatim from
    # Stage 5's _claims_report_eligible(), never recomputed or re-inferred
    # here. fallback_reason is None precisely when report_source is "claims".
    # Old report rows (saved before this field existed) read back as
    # "legacy_evidence"/None, which is accurate -- nothing retroactive, same
    # convention as every prior additive Report field.
    report_source: ReportSource = ReportSource.LEGACY_EVIDENCE
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        # Validates (and coerces a plain string into) report_source on EVERY
        # construction -- a fresh Report from summarize() and a row read back
        # from storage both go through this, so an invalid value can never
        # exist as a live Report instance, let alone reach the database.
        self.report_source = ReportSource(self.report_source)


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
    # once for the whole review by the earlier screening stage, which may not
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
    # Points at a canonical_categories.category_id once Phase 3's categorization
    # batch step runs -- see pipeline/taxonomy.py (added in a later Phase 3 stage;
    # this field is not yet written by anything as of the current stage).
    canonical_category: str | None = None
    # Phase 3 categorization provenance. All three NULL means "categorization
    # hasn't run for this claim yet" (pre-Phase-3 claim, or the kill switch was
    # off). status="unresolved" is an infrastructure failure (e.g. the LLM call
    # itself errored) -- deliberately distinct from a genuine no-match, which
    # resolves normally via method="proposed_new". An infra failure must never
    # mint a new category and must stay retryable, never silently downgrade to
    # a proposal. method="manual" marks a human's direct single-claim override
    # (see Storage.set_claims_categorization's override_manual guard) --
    # categorize_claims() never touches a manual claim without that explicit,
    # separately-named flag, regardless of its own `force` setting.
    categorization_status: str | None = None  # "resolved" | "unresolved" | None
    categorization_method: str | None = None  # "lexical_match" | "llm_match" | "proposed_new" | "manual" | None
    categorization_confidence: float | None = None  # populated only for lexical_match / llm_match


@dataclass(slots=True)
class CanonicalCategory:
    """One entry in a product_category's normalized topic taxonomy (Phase 3).
    Claim.canonical_category points here by category_id. alias_of is a
    category-to-category MERGE only -- never a string-synonym mechanism (no
    separate table of known aspect_raw variants exists; matching is against
    canonical_label alone, see pipeline/taxonomy.py). Resolved through alias_of
    at report-read time, exactly one hop deep -- Storage's merge_category()
    refuses to create a longer chain, so callers never need to loop."""

    category_id: str
    product_category: str
    canonical_label: str
    normalized_label: str
    status: CategoryStatus
    first_seen_aspect_raw: str
    created_at: str
    updated_at: str
    alias_of: str | None = None


@dataclass(slots=True)
class CategoryAuditLogEntry:
    """Durable record of a category-level lifecycle transition (approve/merge/
    deprecate/rename). Not user-attributed -- this app has no auth/identity
    system, so the log records what happened and when, not who. Reuses
    TraceEvent's action+JSON-detail shape rather than bespoke per-action
    columns."""

    category_id: str
    action: CategoryAuditAction
    detail: dict[str, Any]
    created_at: str = field(default_factory=utc_now)
    id: int | None = None
