from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .collectors.base import Collector
from .llm import DeepSeekClient, fast_model, load_dotenv, pro_model
from .models import (
    CanonicalCategory,
    CategoryStatus,
    Claim,
    ClaimType,
    CollectedItem,
    Evidence,
    InsightType,
    Report,
    ReportSource,
    RunStatus,
    StepType,
    TraceEvent,
    utc_now,
)
from .pipeline.claims import enable_claim_extraction, extract_claims
from .pipeline.screening import ScreeningResult, screen_item
from .pipeline.taxonomy import CategorizationStats, categorize_claims, enable_claim_categorization
from .storage import Storage

DIMINISHING_RETURNS_WINDOW = 2

# Explicit policy for CLAIMS_REPORT_MIN_RESOLVED_RATIO if it's set to something
# unusable: never let a misconfigured env var crash the run or silently gate
# every run closed/open -- fall back to this default for anything that isn't a
# finite number, and clamp anything finite but outside [0.0, 1.0].
_DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO = 0.7


@dataclass
class IterationClaimStats:
    """Structured counts for one iteration's CLAIM_EXTRACTION trace event --
    kept as a real dataclass (not an ad hoc dict) so the field names are pinned
    down and RunDetail's stats panel can read exact counts instead of parsing
    the human-readable trace message."""

    source_items_processed: int = 0
    items_with_claims: int = 0
    claims_total: int = 0
    llm_claims: int = 0
    fallback_claims: int = 0
    invalid_claims: int = 0
    extraction_failures: int = 0
    extraction_disabled: int = 0
    # Phase 1.6 -- within-review dedup funnel, summed across every item this iteration.
    raw_claims_extracted: int = 0
    final_claims_saved: int = 0
    within_review_duplicates_removed: int = 0
    claims_merged: int = 0
    safety_cap_truncations: int = 0


@dataclass
class IterationScreeningStats:
    """Structured counts for one iteration's SCREENING trace event (Phase 2).
    is_evidence_worthy is the only hard discard signal; has_product_signal_count
    is observability only -- it never gates Claim extraction (see
    pipeline/screening.py's ScreeningResult docstring for why)."""

    items_screened: int = 0
    evidence_worthy: int = 0
    discarded: int = 0
    mixed_content: int = 0
    has_product_signal_count: int = 0
    llm_screened: int = 0
    fallback_screened: int = 0


# ---------------------------------------------------------------------------
# Phase 3, Stage 5 -- Claims-report eligibility gate. Determines ONLY whether
# the (not-yet-built) Claims-based report path would be usable this run --
# does not itself change what summarize() does. See docs/phase3_claims_taxonomy_plan.md.
# ---------------------------------------------------------------------------


def enable_claims_based_report() -> bool:
    """Kill switch for the Claims-report path -- independent of
    enable_claim_categorization() (Phase 3, pipeline/taxonomy.py), which
    controls whether categorization runs at all. This one controls only
    whether a (future) Claims-based report is ever considered eligible,
    regardless of how well categorization went."""
    load_dotenv()
    raw = os.environ.get("ENABLE_CLAIMS_REPORT", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def claims_report_min_resolved_ratio() -> float:
    """Reads CLAIMS_REPORT_MIN_RESOLVED_RATIO. Explicit policy for a
    misconfigured value, so a bad env var can never crash the run or silently
    produce a nonsensical gate:
    - unset/empty -> the default (_DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO)
    - not parseable as a float (e.g. "high", "") -> the default
    - parses but is NaN or +/-Infinity -> the default
    - a valid finite number outside [0.0, 1.0] -> CLAMPED into range (not
      rejected -- consistent with pipeline/taxonomy.py's _sanitize_confidence,
      which clamps rather than rejects an out-of-range-but-finite LLM value)
    """
    load_dotenv()
    raw = os.environ.get("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "").strip()
    if not raw:
        return _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO
    if not math.isfinite(value):
        return _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO
    return max(0.0, min(value, 1.0))


def _resolved_ratio(cat_stats: CategorizationStats | None) -> float | None:
    """The fraction of this run's categorization attempt that did NOT end up
    unresolved -- None when there's nothing to compute a ratio over
    (categorization never ran, or there were zero claims). Deliberately the
    exact formula specified for this gate: claims that were skipped this pass
    (already resolved earlier, or manually protected) count toward the
    numerator just like a freshly-resolved claim would -- only a genuine
    unresolved_failures counts against it."""
    if cat_stats is None or cat_stats.claims_total == 0:
        return None
    return (cat_stats.claims_total - cat_stats.unresolved_failures) / cat_stats.claims_total


def _claims_report_eligible(
    run_id: str, storage: Storage, cat_stats: CategorizationStats | None
) -> tuple[bool, str | None]:
    """Decides whether the Claims-based report path is eligible for this run.
    Reads ONLY the CategorizationStats this run's own categorize_claims() call
    already produced -- never re-queries the database for claims (run_id/
    storage are accepted for interface stability and future use, e.g. logging,
    not for an extra query here). `claims` being non-empty is deliberately NOT
    the test on its own -- see the four explicit rejection reasons below.

    Returns (True, None) when eligible, or (False, reason) where reason is one
    of: "claims_report_disabled", "categorization_disabled",
    "categorization_incomplete", "no_claims", or
    "low_resolved_coverage:<ratio formatted to 2 decimal places>".
    """
    if not enable_claims_based_report():
        return False, "claims_report_disabled"
    if cat_stats is None:
        return False, "categorization_disabled"
    if not cat_stats.completed:
        return False, "categorization_incomplete"
    if cat_stats.claims_total == 0:
        return False, "no_claims"
    ratio = _resolved_ratio(cat_stats)
    assert ratio is not None  # claims_total == 0 already returned above
    if ratio < claims_report_min_resolved_ratio():
        return False, f"low_resolved_coverage:{ratio:.2f}"
    return True, None


def run_react_loop(
    run_id: str,
    storage: Storage,
    collector: Collector,
    llm: DeepSeekClient,
    should_stop: Callable[[], bool],
    save_trace: Callable[[TraceEvent], None] | None = None,
) -> None:
    """Runs one full ReAct loop for `run_id`: (search -> filter/analyze -> sufficiency check)* -> summarize."""

    def trace(iteration: int, step_type: StepType, message: str, payload: dict[str, Any] | None = None) -> None:
        event = TraceEvent(run_id=run_id, iteration=iteration, step_type=step_type, message=message, payload=payload or {})
        storage.save_trace_event(event)
        if save_trace is not None:
            save_trace(event)

    run = storage.get_run(run_id)
    if run is None:
        raise ValueError(f"Unknown run: {run_id}")

    try:
        seen_urls: set[str] = set()
        collected: list[Evidence] = []
        tried_queries: list[dict[str, str]] = []
        new_counts: list[int] = []
        missing_aspects: list[str] = []
        iteration = 0

        storage.update_run_progress(run_id, 0, 0, RunStatus.SEARCHING)

        for iteration in range(1, run.max_iterations + 1):
            if should_stop():
                storage.update_run_status(run_id, RunStatus.STOPPED, stop_reason="Manually stopped by user")
                return

            thought = plan_next_query(
                run.product_category, run.keywords, run.target_subreddits, tried_queries, collected, iteration, missing_aspects, llm
            )
            trace(iteration, StepType.THOUGHT, thought["reasoning"], {"query": thought["query"], "subreddit": thought["subreddit"]})

            try:
                items = collector.search(thought["query"], subreddit=thought["subreddit"], limit=25)
                search_payload = {"query": thought["query"], "subreddit": thought["subreddit"], "items_returned": len(items)}
                # Optional, duck-typed enrichment (same pattern as run_manager.py's
                # getattr(collector, "close", ...) call) -- a no-op for every collector
                # that doesn't set this, currently only RedditBrowserCollector does.
                extra_stats = getattr(collector, "last_search_stats", None)
                if extra_stats:
                    search_payload.update(extra_stats)
                trace(
                    iteration,
                    StepType.ACTION_SEARCH,
                    f'Searched "{thought["query"]}", got {len(items)} result(s)',
                    search_payload,
                )
            except Exception as exc:  # noqa: BLE001 - a failed search should not crash the whole run
                items = []
                trace(iteration, StepType.ACTION_SEARCH, f"Search failed: {exc}", {"query": thought["query"], "error": str(exc)})

            tried_queries.append({"query": thought["query"], "subreddit": thought["subreddit"]})

            new_evidence: list[Evidence] = []
            claim_stats = IterationClaimStats()
            screening_stats = IterationScreeningStats()
            for item in items:
                if item.source_url in seen_urls:
                    continue
                seen_urls.add(item.source_url)
                screening = screen_item(run.product_category, item, llm)
                screening_stats.items_screened += 1
                if screening.extraction_method == "llm":
                    screening_stats.llm_screened += 1
                else:
                    screening_stats.fallback_screened += 1
                if screening.is_mixed_content:
                    screening_stats.mixed_content += 1
                if screening.has_product_signal:
                    screening_stats.has_product_signal_count += 1
                if not screening.is_evidence_worthy:
                    # The only hard discard point in Phase 2 -- categories were a
                    # subset of {spam_or_irrelevant, low_information}. Every other
                    # evidence-worthy item (product/shipping/service, alone or
                    # mixed) proceeds to extract_claims() below unconditionally --
                    # has_product_signal is never used to skip that call.
                    screening_stats.discarded += 1
                    continue
                screening_stats.evidence_worthy += 1
                evidence = _build_evidence(run_id, iteration, item, screening)
                storage.save_evidence(evidence)
                new_evidence.append(evidence)

                claim_stats.source_items_processed += 1
                if not enable_claim_extraction():
                    claim_stats.extraction_disabled += 1
                    continue
                result = extract_claims(run.product_category, evidence, llm)
                claim_stats.llm_claims += result.stats.llm_claims
                claim_stats.fallback_claims += result.stats.fallback_claims
                claim_stats.invalid_claims += result.stats.invalid_claims
                claim_stats.raw_claims_extracted += result.stats.raw_claims_extracted
                claim_stats.final_claims_saved += result.stats.final_claims_saved
                claim_stats.within_review_duplicates_removed += result.stats.within_review_duplicates_removed
                claim_stats.claims_merged += result.stats.claims_merged
                claim_stats.safety_cap_truncations += result.stats.safety_cap_truncations
                if result.succeeded:
                    # Only ever replace on a successful extraction (even an empty one) --
                    # a failed extraction must never erase claims a previous run stored.
                    storage.replace_claims_for_evidence(evidence.evidence_id, result.claims)
                    claim_stats.claims_total += len(result.claims)
                    if result.claims:
                        claim_stats.items_with_claims += 1
                else:
                    claim_stats.extraction_failures += 1

            collected.extend(new_evidence)
            new_counts.append(len(new_evidence))
            trace(
                iteration,
                StepType.OBSERVATION,
                f"Analyzed {len(items)} result(s), kept {len(new_evidence)} relevant item(s) (total {len(collected)})",
                {"items_analyzed": len(items), "new_evidence": len(new_evidence), "total_evidence": len(collected)},
            )
            trace(
                iteration,
                StepType.SCREENING,
                (
                    f"Screened {screening_stats.items_screened} item(s): "
                    f"{screening_stats.evidence_worthy} evidence-worthy "
                    f"({screening_stats.mixed_content} mixed content, "
                    f"{screening_stats.has_product_signal_count} with product signal), "
                    f"{screening_stats.discarded} discarded (spam/low-information only)"
                ),
                asdict(screening_stats),
            )
            trace(
                iteration,
                StepType.CLAIM_EXTRACTION,
                (
                    f"Extracted {claim_stats.claims_total} claim(s) from "
                    f"{claim_stats.items_with_claims}/{claim_stats.source_items_processed} item(s) "
                    f"({claim_stats.llm_claims} LLM, {claim_stats.fallback_claims} fallback, "
                    f"{claim_stats.invalid_claims} invalid skipped, {claim_stats.extraction_failures} failed, "
                    f"{claim_stats.extraction_disabled} disabled)"
                ),
                asdict(claim_stats),
            )
            storage.update_run_progress(run_id, iteration, len(collected), RunStatus.SEARCHING)

            verdict = check_sufficiency(
                run.product_category, collected, iteration, run.max_iterations, run.min_evidence_target, new_counts, llm
            )
            missing_aspects = verdict.get("missing_aspects", [])
            trace(
                iteration,
                StepType.SUFFICIENCY_CHECK,
                verdict["reason"],
                {"sufficient": verdict["sufficient"], "missing_aspects": missing_aspects},
            )
            if verdict["sufficient"]:
                break

        storage.update_run_progress(run_id, iteration, len(collected), RunStatus.SUMMARIZING)

        # Phase 3 (Customer Demand Intelligence Pipeline): categorizes this
        # run's Claims against the product_category's canonical taxonomy --
        # a separate, run-level batch step, deliberately run once here (never
        # per-iteration, never bolted onto extract_claims()). Always the safe
        # default (force=False, override_manual=False); a human-run
        # maintenance entry point is the only caller ever expected to pass
        # anything else.
        cat_stats: CategorizationStats | None = None
        if enable_claim_categorization():
            run_claims = storage.list_claims(run_id)
            cat_stats = categorize_claims(run_id, run.product_category, run_claims, storage, llm)
            trace(
                iteration,
                StepType.CATEGORIZATION,
                (
                    f"Categorized claims against the taxonomy: {cat_stats.lexical_matched} lexical match(es), "
                    f"{cat_stats.llm_matched} LLM match(es), {cat_stats.new_categories_proposed} new categor"
                    f"{'y' if cat_stats.new_categories_proposed == 1 else 'ies'} proposed, "
                    f"{cat_stats.unresolved_failures} unresolved, {cat_stats.skipped_already_resolved} already "
                    f"categorized, {cat_stats.skipped_manual_protected} manually protected"
                    + ("" if cat_stats.completed else f" -- INCOMPLETE: {cat_stats.error}")
                ),
                asdict(cat_stats),
            )

        # Stage 5: decides ONLY whether the (not-yet-built) Claims-based report
        # path would be eligible this run -- always traced, regardless of
        # whether categorization ran, so "why wasn't it eligible" is never a
        # silent question.
        eligible, fallback_reason = _claims_report_eligible(run_id, storage, cat_stats)
        trace(
            iteration,
            StepType.CLAIMS_REPORT_ELIGIBILITY,
            "Claims-based report is eligible" if eligible else f"Claims-based report not eligible: {fallback_reason}",
            {
                "eligible": eligible,
                "fallback_reason": fallback_reason,
                "claims_total": cat_stats.claims_total if cat_stats is not None else 0,
                "unresolved_failures": cat_stats.unresolved_failures if cat_stats is not None else 0,
                "resolved_ratio": _resolved_ratio(cat_stats),
            },
        )

        # Stage 6: the eligibility gate above is the single decision point --
        # Claims/categories are loaded ONLY when eligible, never loaded first
        # and then ignored. When not eligible, summarize() gets empty lists
        # and takes the legacy Evidence-based path by construction (branching
        # on "was I handed any claims").
        claims: list[Claim] = []
        categories: list[CanonicalCategory] = []
        if eligible:
            claims = storage.list_claims(run_id)
            categories = storage.list_categories(run.product_category)

        report = summarize(run_id, run.product_category, collected, claims, categories, llm, fallback_reason)
        storage.save_report(report)
        trace(iteration, StepType.SUMMARY, f"Generated the merchant report based on {len(collected)} piece(s) of evidence", {"evidence_count": len(collected)})
        storage.update_run_status(run_id, RunStatus.COMPLETED)
    except Exception as exc:  # noqa: BLE001 - background loop must never crash silently
        storage.update_run_status(run_id, RunStatus.FAILED, error=str(exc))
        raise


# ---------------------------------------------------------------------------
# Reason: decide the next search query
# ---------------------------------------------------------------------------

def plan_next_query(
    product_category: str,
    keywords: list[str],
    target_subreddits: list[str],
    tried_queries: list[dict[str, str]],
    collected: list[Evidence],
    iteration: int,
    missing_aspects: list[str],
    llm: DeepSeekClient,
) -> dict[str, str]:
    if llm.available():
        try:
            return _plan_next_query_llm(product_category, keywords, target_subreddits, tried_queries, collected, iteration, missing_aspects, llm)
        except Exception:
            pass
    return _plan_next_query_fallback(product_category, keywords, target_subreddits, tried_queries, iteration, missing_aspects)


def _plan_next_query_llm(
    product_category: str,
    keywords: list[str],
    target_subreddits: list[str],
    tried_queries: list[dict[str, str]],
    collected: list[Evidence],
    iteration: int,
    missing_aspects: list[str],
    llm: DeepSeekClient,
) -> dict[str, str]:
    system = (
        "You are a search-planning agent inside a Reddit product-feedback research tool for merchants. "
        "Decide the single best next Reddit search query to surface real user opinions (complaints, feature "
        "requests, comparisons, praise) about the given product category. Avoid repeating previous queries. "
        "If missing aspects are given, target them. Write the reasoning field in English. Return only JSON."
    )
    user = json.dumps(
        {
            "product_category": product_category,
            "keywords": keywords,
            "target_subreddits": target_subreddits,
            "iteration": iteration,
            "previously_tried_queries": tried_queries,
            "evidence_collected_so_far": len(collected),
            "aspects_covered": sorted({item.aspect for item in collected if item.aspect}),
            "missing_aspects_to_target": missing_aspects,
            "expected_json": {
                "query": "a specific Reddit search query in English",
                "subreddit": "optional subreddit without r/ prefix, empty string to search all of Reddit",
                "reasoning": "a short explanation in English of why this search was chosen",
            },
        },
        ensure_ascii=False,
    )
    parsed = llm.json_chat(fast_model(), system, user)
    query = str(parsed.get("query") or "").strip()
    if not query:
        raise ValueError("planner returned an empty query")
    return {
        "query": query,
        "subreddit": str(parsed.get("subreddit") or "").strip(),
        "reasoning": str(parsed.get("reasoning") or "AI planned the next search step."),
    }


_FALLBACK_QUERY_TEMPLATES = [
    "{category} complaints",
    "{category} problems",
    "{category} review",
    "best {category}",
    "{category} vs alternative",
    "{category} broke",
    "{category} worth it",
    "{category} customer service",
]


def _plan_next_query_fallback(
    product_category: str,
    keywords: list[str],
    target_subreddits: list[str],
    tried_queries: list[dict[str, str]],
    iteration: int,
    missing_aspects: list[str],
) -> dict[str, str]:
    base = product_category.strip() or " ".join(keywords) or "product"
    tried_query_texts = {item["query"] for item in tried_queries}
    subreddit = target_subreddits[(iteration - 1) % len(target_subreddits)] if target_subreddits else ""
    if missing_aspects:
        candidate = f"{base} {missing_aspects[0]}"
        if candidate not in tried_query_texts:
            return {"query": candidate, "subreddit": subreddit, "reasoning": f"Covering an under-discussed aspect: {missing_aspects[0]}"}
    for template in _FALLBACK_QUERY_TEMPLATES:
        candidate = template.format(category=base)
        if candidate not in tried_query_texts:
            return {"query": candidate, "subreddit": subreddit, "reasoning": "Cycling through preset query templates (no LLM configured)"}
    return {
        "query": f"{base} feedback {iteration}",
        "subreddit": subreddit,
        "reasoning": "Preset templates exhausted; appending an iteration number to avoid repeating queries",
    }


# ---------------------------------------------------------------------------
# Observe: build an Evidence row from one screened item
# ---------------------------------------------------------------------------
# Phase 2 replaced the old analyze_item()/_analyze_item_llm()/_analyze_item_fallback()
# (a single binary is_relevant verdict that could silently discard a mixed
# review's real product signal) with pipeline/screening.py's screen_item() --
# see that module for the LLM + fallback logic. This section only builds the
# Evidence row from its ScreeningResult.


def _build_evidence(run_id: str, iteration: int, item: CollectedItem, screening: ScreeningResult) -> Evidence:
    evidence_id = f"ev_{hashlib.sha1((run_id + item.source_url).encode()).hexdigest()[:12]}"
    return Evidence(
        evidence_id=evidence_id,
        run_id=run_id,
        iteration=iteration,
        source_url=item.source_url,
        subreddit=item.subreddit,
        item_type=item.item_type,
        title=item.title,
        body=item.body,
        score=item.score,
        comment_count=item.comment_count,
        created_at=item.created_at,
        fetched_at=utc_now(),
        search_query=item.search_query,
        insight_type=screening.insight_type,
        aspect=screening.aspect,
        sentiment=screening.sentiment,
        quote=screening.quote,
        confidence=screening.confidence,
        screening_categories=[c.value for c in screening.categories],
        is_mixed_content=screening.is_mixed_content,
    )


# ---------------------------------------------------------------------------
# Judge: has enough evidence been collected to write a solid report?
# ---------------------------------------------------------------------------

def check_sufficiency(
    product_category: str,
    collected: list[Evidence],
    iteration: int,
    max_iterations: int,
    min_evidence_target: int,
    new_counts: list[int],
    llm: DeepSeekClient,
) -> dict[str, Any]:
    if iteration >= max_iterations:
        return {"sufficient": True, "reason": "Reached the maximum iteration cap; moving to the summary stage.", "missing_aspects": []}

    if len(new_counts) >= DIMINISHING_RETURNS_WINDOW and all(count == 0 for count in new_counts[-DIMINISHING_RETURNS_WINDOW:]):
        return {
            "sufficient": True,
            "reason": "No new relevant evidence in the last two rounds (diminishing returns); moving to the summary stage early.",
            "missing_aspects": [],
        }

    if len(collected) < min_evidence_target:
        return {
            "sufficient": False,
            "reason": f"Collected {len(collected)} piece(s) of evidence, short of the {min_evidence_target} target; continuing to search.",
            "missing_aspects": [],
        }

    if llm.available():
        try:
            return _check_sufficiency_llm(product_category, collected, iteration, max_iterations, min_evidence_target, llm)
        except Exception:
            pass
    return _check_sufficiency_fallback(collected, min_evidence_target)


def _check_sufficiency_llm(
    product_category: str,
    collected: list[Evidence],
    iteration: int,
    max_iterations: int,
    min_evidence_target: int,
    llm: DeepSeekClient,
) -> dict[str, Any]:
    aspect_counts = Counter(item.aspect for item in collected)
    subreddit_counts = Counter(item.subreddit for item in collected)
    system = (
        "You are the sufficiency-judging step of a Reddit product-feedback ReAct agent. Decide whether the "
        "evidence collected so far is broad and deep enough to write a solid, actionable merchant report, or "
        "whether the agent should keep searching. Consider evidence volume, subreddit diversity, and aspect "
        "coverage (are pain points concentrated on very few aspects, suggesting more digging would surface more "
        "useful angles?). Write reason and missing_aspects in English. Return only JSON."
    )
    user = json.dumps(
        {
            "product_category": product_category,
            "iteration": iteration,
            "max_iterations": max_iterations,
            "min_evidence_target": min_evidence_target,
            "evidence_count": len(collected),
            "subreddit_counts": dict(subreddit_counts),
            "aspect_counts": dict(aspect_counts),
            "expected_json": {
                "sufficient": "true if ready to summarize, false to keep searching",
                "reason": "a short explanation in English",
                "missing_aspects": ["aspect labels that are not yet well covered and worth searching further"],
            },
        },
        ensure_ascii=False,
    )
    parsed = llm.json_chat(fast_model(), system, user)
    return {
        "sufficient": bool(parsed.get("sufficient")),
        "reason": str(parsed.get("reason") or "AI judged whether the current evidence is sufficient."),
        "missing_aspects": [str(item) for item in parsed.get("missing_aspects", []) if isinstance(item, (str, int, float))],
    }


def _check_sufficiency_fallback(collected: list[Evidence], min_evidence_target: int) -> dict[str, Any]:
    subreddit_count = len({item.subreddit for item in collected})
    if len(collected) >= min_evidence_target and subreddit_count >= 2:
        return {
            "sufficient": True,
            "reason": f"Collected {len(collected)} piece(s) of evidence across {subreddit_count} subreddit(s); judged sufficient (rule-based fallback, no LLM configured).",
            "missing_aspects": [],
        }
    aspect_counts = Counter(item.aspect for item in collected)
    weak_aspects = [aspect for aspect, count in aspect_counts.most_common() if count <= 1]
    return {
        "sufficient": False,
        "reason": "Not enough subreddit or aspect diversity yet; continuing to search (rule-based fallback, no LLM configured).",
        "missing_aspects": weak_aspects[:3],
    }


# ---------------------------------------------------------------------------
# Phase 3, Stage 6 -- Claims-based aggregation pipeline: resolve taxonomy ->
# aggregate -> build report inputs. Pure, in-memory, no Storage/LLM calls
# anywhere in this section -- everything here is a function of already-loaded
# data, which is what makes it independently unit-testable. Does NOT yet
# change the Report schema (no report_source/fallback_reason persistence,
# no shipping_issues/seller_service_issues columns) -- that's a later stage.
# ---------------------------------------------------------------------------

_UNCATEGORIZED_LABEL = "Uncategorized"


@dataclass
class ResolvedCategory:
    category_id: str | None  # None for "uncategorized"
    label: str
    status: str  # "approved" | "proposed" | "uncategorized"


def _resolve_categories(claims: list[Claim], categories: list[CanonicalCategory]) -> dict[str, ResolvedCategory]:
    """Resolves each claim's canonical_category through alias_of exactly one
    hop (merge_category() never lets an alias's own alias_of be set, so a
    single dict lookup is always enough -- no loop needed). Pure, in-memory,
    no DB/LLM calls. Falls back to "uncategorized" for: no canonical_category
    at all, a canonical_category that isn't in `categories`, a category whose
    alias_of points at something not in `categories`, or a category (after
    resolving through alias_of, if any) that is deprecated with no further
    alias -- deprecated-with-no-alias claims are never silently dropped, they
    land in the same explicit uncategorized bucket as never-categorized ones.
    """
    by_id = {c.category_id: c for c in categories}
    uncategorized = ResolvedCategory(category_id=None, label=_UNCATEGORIZED_LABEL, status="uncategorized")

    def resolve_one(category_id: str | None) -> ResolvedCategory:
        if category_id is None:
            return uncategorized
        category = by_id.get(category_id)
        if category is None:
            return uncategorized
        if category.alias_of is not None:
            target = by_id.get(category.alias_of)
            if target is None:
                return uncategorized
            category = target
        if category.status == CategoryStatus.DEPRECATED:
            return uncategorized
        return ResolvedCategory(category_id=category.category_id, label=category.canonical_label, status=category.status.value)

    return {claim.claim_id: resolve_one(claim.canonical_category) for claim in claims}


_REDDIT_THREAD_PATTERN = re.compile(r"^(https?://[^/]+/r/[^/]+/comments/[^/]+)")


def _thread_key(evidence: Evidence) -> str:
    """Best-effort, source-specific -- see the plan doc's "thread count"
    section. Reddit permalinks share a stable prefix per post
    (scheme+host+/r/<sub>/comments/<post_id>); any URL that doesn't match
    (Amazon, YouTube, JSON upload, or anything unexpected) falls back to the
    evidence's own id, which degenerates "distinct thread count" to "distinct
    evidence count" for those sources rather than fabricating a wrong number.
    Detected structurally from the URL itself rather than a passed-in
    DataSource flag, so this function's signature stays exactly what the
    aggregation step needs."""
    match = _REDDIT_THREAD_PATTERN.match(evidence.source_url)
    return match.group(1) if match else evidence.evidence_id


@dataclass
class AggregateGroup:
    claim_type: ClaimType
    category_key: str  # a category_id, or the literal string "uncategorized"
    label: str
    category_status: str  # "approved" | "proposed" | "uncategorized"
    count: int
    subreddit_count: int
    avg_confidence: float
    sentiment_counts: dict[str, int]
    example_quotes: list[dict[str, Any]]
    # Support-threshold inputs (Stage 6 requirement) -- irrelevant to the four
    # always-surfaced sections, only consulted by _build_report_inputs for
    # shipping_issues/seller_service_issues gating.
    evidence_count: int
    thread_count: int
    # Milestone 3 / A3: claim count per distinct thread -- the raw
    # distribution _weighted_count() dampens. Kept separate from thread_count
    # (which only needs the distinct-key count) because (count, thread_count)
    # alone cannot reconstruct the per-thread split (e.g. 9 claims across 3
    # threads could be 3-3-3 or 7-1-1 -- materially different once capped).
    # Internal only -- never serialized via _aggregate_group_to_dict.
    thread_counts: dict[str, int]


def _aggregate_claims_by_category(
    claims: list[Claim], resolved: dict[str, ResolvedCategory], evidence_by_id: dict[str, Evidence]
) -> dict[tuple[ClaimType, str], AggregateGroup]:
    """Groups by (claim_type, resolved_category) -- category_key is the
    resolved category's id for approved/proposed groups, or the literal
    string "uncategorized". Every claim whose resolution is "uncategorized"
    (regardless of its own aspect_raw) shares ONE bucket per claim_type --
    never split back out by aspect_raw, which would reintroduce exactly the
    fragmentation problem this phase exists to fix, for precisely the
    lowest-trust population. Pure grouping/counting, no I/O."""
    grouped: dict[tuple[ClaimType, str], list[Claim]] = {}
    for claim in claims:
        resolution = resolved.get(claim.claim_id)
        if resolution is None:
            continue  # defensive: every claim passed in is expected to have a resolution
        category_key = resolution.category_id or "uncategorized"
        grouped.setdefault((claim.claim_type, category_key), []).append(claim)

    aggregated: dict[tuple[ClaimType, str], AggregateGroup] = {}
    for (claim_type, category_key), group_claims in grouped.items():
        label = resolved[group_claims[0].claim_id].label
        category_status = resolved[group_claims[0].claim_id].status
        evidences = [evidence_by_id[c.evidence_id] for c in group_claims if c.evidence_id in evidence_by_id]
        # One tally per claim (not per distinct evidence) -- a thread's weight
        # in the dampening formula is its CLAIM count, matching the approved
        # formula ("claims_from_thread") exactly, including the case where
        # several claims share one evidence item.
        thread_counts: dict[str, int] = dict(Counter(_thread_key(e) for e in evidences))

        quote_candidates = sorted(group_claims, key=lambda c: c.confidence, reverse=True)[:3]
        example_quotes = []
        for c in quote_candidates:
            evidence = evidence_by_id.get(c.evidence_id)
            example_quotes.append(
                {
                    "quote": c.source_excerpt or (evidence.quote if evidence else ""),
                    "source_url": evidence.source_url if evidence else "",
                    "subreddit": evidence.subreddit if evidence else "",
                }
            )

        aggregated[(claim_type, category_key)] = AggregateGroup(
            claim_type=claim_type,
            category_key=category_key,
            label=label,
            category_status=category_status,
            count=len(group_claims),
            subreddit_count=len({e.subreddit for e in evidences}),
            avg_confidence=round(sum(c.confidence for c in group_claims) / len(group_claims), 2),
            sentiment_counts=dict(Counter(c.sentiment.value for c in group_claims)),
            example_quotes=example_quotes,
            evidence_count=len({c.evidence_id for c in group_claims}),
            thread_count=len(thread_counts),
            thread_counts=thread_counts,
        )
    return aggregated


@dataclass
class ReportInputs:
    top_pain_points: list[dict[str, Any]]
    feature_requests: list[dict[str, Any]]
    praised_aspects: list[dict[str, Any]]
    competitor_mentions: list[dict[str, Any]]
    shipping_issues: list[dict[str, Any]]
    seller_service_issues: list[dict[str, Any]]


_CLAIM_TYPE_SECTION: dict[ClaimType, str] = {
    ClaimType.PROBLEM: "top_pain_points",
    ClaimType.FEATURE_REQUEST: "feature_requests",
    ClaimType.PRAISE: "praised_aspects",
    ClaimType.COMPARISON: "competitor_mentions",
    ClaimType.SHIPPING_ISSUE: "shipping_issues",
    ClaimType.SELLER_SERVICE_ISSUE: "seller_service_issues",
    # QUESTION, OBSERVATION, NOISE are deliberately absent -- never reach the
    # report, matching today's InsightType.QUESTION/NOISE gap exactly.
}
_GATED_SECTIONS = {"shipping_issues", "seller_service_issues"}

# Small starting defaults, not yet validated on real proposed-category volume
# -- flagged for tuning the same way screening.py's _LOW_INFORMATION_MAX_CHARS
# and pipeline/taxonomy.py's lexical-similarity thresholds are. Shared across
# both gated sections rather than six separate per-section env vars.
_SHIPPING_SERVICE_MIN_CLAIMS_DEFAULT = 2
_SHIPPING_SERVICE_MIN_EVIDENCE_DEFAULT = 2
_SHIPPING_SERVICE_MIN_THREADS_DEFAULT = 1


def _read_min_count_env(name: str, default: int) -> int:
    """Same safe-fallback philosophy as claims_report_min_resolved_ratio():
    unset/non-numeric -> default; a valid-but-negative count is clamped to 0
    (meaning "no minimum"), never rejected or crashing the run."""
    load_dotenv()
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def _shipping_service_min_claims() -> int:
    return _read_min_count_env("SHIPPING_SERVICE_MIN_CLAIMS", _SHIPPING_SERVICE_MIN_CLAIMS_DEFAULT)


def _shipping_service_min_evidence() -> int:
    return _read_min_count_env("SHIPPING_SERVICE_MIN_EVIDENCE", _SHIPPING_SERVICE_MIN_EVIDENCE_DEFAULT)


def _shipping_service_min_threads() -> int:
    return _read_min_count_env("SHIPPING_SERVICE_MIN_THREADS", _SHIPPING_SERVICE_MIN_THREADS_DEFAULT)


# Milestone 3 / A3 -- thread-concentration dampening. Validated against real
# stored data (run_55025c50e81b): with this default, a category whose 9
# claims all came from one Reddit thread dampens to the same rank-relevant
# weight as categories with only 3 claims each spread across 3 independent
# threads -- correctly demoting a single-conversation echo below genuinely
# corroborated (if smaller) issues. Fixed and env-configurable, same pattern
# as the shipping/service thresholds above; deliberately NOT auto-tuned --
# a fixed, human-set constant in v1.x, not a target for adaptive/learned
# adjustment (that boundary is a deliberate v1.x/v2 line, not an oversight).
_THREAD_DAMPENING_CAP_DEFAULT = 3
_DAMPENED_SECTIONS = {"top_pain_points", "feature_requests", "praised_aspects", "competitor_mentions"}


def _thread_dampening_cap() -> int:
    return _read_min_count_env("THREAD_DAMPENING_CAP", _THREAD_DAMPENING_CAP_DEFAULT)


def _weighted_count(thread_counts: dict[str, int], cap: int) -> int:
    """weighted_count = sum(min(claims_from_thread, cap) for each distinct
    thread). Ranking-only -- never displayed as a "mentions" count; the raw,
    uncapped `count` on every report entry is untouched by this function."""
    return sum(min(n, cap) for n in thread_counts.values())


def _aggregate_group_to_dict(group: AggregateGroup, weighted_count: int) -> dict[str, Any]:
    return {
        "aspect": group.label,
        # The literal, uncapped mentions count -- never adjusted by thread
        # dampening. Milestone 3 requirement: what a merchant reads as "how
        # many mentions" must never change.
        "count": group.count,
        "subreddit_count": group.subreddit_count,
        "avg_confidence": group.avg_confidence,
        "sentiment_counts": group.sentiment_counts,
        "example_quotes": group.example_quotes,
        "category_status": group.category_status,
        "thread_count": group.thread_count,
        # Ranking-only signal (Milestone 3 / A3) -- ties for "how many mentions"
        # to real diversity of source threads, capped per thread. Never meant
        # to be read as a mentions count itself.
        "weighted_count": weighted_count,
    }


def _build_report_inputs(aggregated: dict[tuple[ClaimType, str], AggregateGroup]) -> ReportInputs:
    """Turns the full aggregate set into the specific lists that become
    Report fields. This is where the shipping/seller-service support-
    threshold gating is applied -- a group only lands in shipping_issues/
    seller_service_issues if it clears all three configured minimums (claim
    count, distinct evidence count, distinct thread count); those two
    sections' own internal order is intentionally left as plain count-
    descending, unchanged from before Milestone 3 -- shipping/seller-service
    threshold *behavior* is explicitly out of this milestone's scope.

    The four always-surfaced sections (Milestone 3 / A3) are sorted by
    weighted_count descending -> thread_count descending -> raw count
    descending -> canonical label ascending, computed once per group here
    (the only place the configured cap is read) and passed into
    _aggregate_group_to_dict so it's never recomputed. No truncation happens
    here -- the existing [:8]-for-the-LLM-prompt truncation stays inside
    _summarize_llm(), unchanged."""
    sections: dict[str, list[AggregateGroup]] = {name: [] for name in set(_CLAIM_TYPE_SECTION.values())}
    min_claims = _shipping_service_min_claims()
    min_evidence = _shipping_service_min_evidence()
    min_threads = _shipping_service_min_threads()
    cap = _thread_dampening_cap()

    for (claim_type, _category_key), group in aggregated.items():
        section_name = _CLAIM_TYPE_SECTION.get(claim_type)
        if section_name is None:
            continue
        if section_name in _GATED_SECTIONS and (
            group.count < min_claims or group.evidence_count < min_evidence or group.thread_count < min_threads
        ):
            continue
        sections[section_name].append(group)

    result_sections: dict[str, list[dict[str, Any]]] = {}
    for name, groups in sections.items():
        if name in _DAMPENED_SECTIONS:
            groups.sort(key=lambda g: (-_weighted_count(g.thread_counts, cap), -g.thread_count, -g.count, g.label))
        else:
            groups.sort(key=lambda g: -g.count)  # unchanged: shipping/seller-service keep plain count order
        result_sections[name] = [_aggregate_group_to_dict(g, _weighted_count(g.thread_counts, cap)) for g in groups]

    return ReportInputs(
        top_pain_points=result_sections["top_pain_points"],
        feature_requests=result_sections["feature_requests"],
        praised_aspects=result_sections["praised_aspects"],
        competitor_mentions=result_sections["competitor_mentions"],
        shipping_issues=result_sections["shipping_issues"],
        seller_service_issues=result_sections["seller_service_issues"],
    )


def _build_report_inputs_from_evidence(collected: list[Evidence]) -> ReportInputs:
    """Legacy path, wrapping the untouched _aggregate_by_aspect() so both
    branches converge on the same ReportInputs shape before narrative
    generation. Evidence has no shipping_issue/seller_service_issue concept
    of its own (InsightType doesn't distinguish them from pain_point), so
    those two sections are simply empty here -- not a regression, the legacy
    report never had them either."""
    return ReportInputs(
        top_pain_points=_aggregate_by_aspect(collected, InsightType.PAIN_POINT),
        feature_requests=_aggregate_by_aspect(collected, InsightType.FEATURE_REQUEST),
        praised_aspects=_aggregate_by_aspect(collected, InsightType.PRAISE),
        competitor_mentions=_aggregate_by_aspect(collected, InsightType.COMPARISON),
        shipping_issues=[],
        seller_service_issues=[],
    )


# ---------------------------------------------------------------------------
# Summarize: turn collected evidence (or, when eligible, categorized Claims)
# into a merchant-facing report
# ---------------------------------------------------------------------------

def summarize(
    run_id: str,
    product_category: str,
    collected: list[Evidence],
    claims: list[Claim],
    categories: list[CanonicalCategory],
    llm: DeepSeekClient,
    fallback_reason: str | None = None,
) -> Report:
    """Thin orchestrator: resolve -> aggregate -> build report inputs ->
    generate narrative. Does not decide eligibility itself, and does not
    recompute or re-infer it -- the caller (run_react_loop) already resolved
    that once via _claims_report_eligible() and passes both the resulting
    `claims` (populated only when eligible, otherwise empty) and whatever
    `fallback_reason` that same call produced (None when the Claims path was
    actually used). This function just branches on "was I handed any claims,"
    which by construction only happens when they passed the gate, and stamps
    report_source/fallback_reason onto the returned Report either way, so
    every report -- not just fallback ones -- is traceable about which path
    produced it."""
    if claims:
        resolved = _resolve_categories(claims, categories)
        evidence_by_id = {e.evidence_id: e for e in collected}
        aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
        report_inputs = _build_report_inputs(aggregated)
        report_source = ReportSource.CLAIMS
    else:
        report_inputs = _build_report_inputs_from_evidence(collected)
        report_source = ReportSource.LEGACY_EVIDENCE

    # fallback_reason is only ever meaningful for the legacy path -- forced to
    # None here (regardless of what was passed in) whenever claims actually
    # produced the report, so the documented invariant on Report itself
    # ("fallback_reason is None precisely when report_source is claims")
    # can't be violated by a caller mistake, without this function
    # recomputing or re-inferring eligibility itself.
    if report_source is ReportSource.CLAIMS:
        fallback_reason = None

    sentiment_breakdown = dict(Counter(item.sentiment.value for item in collected))

    # Phase 4, P0 (docs/phase4_merchant_decision_support_plan.md) -- real zero-evidence
    # runs (run_89b02b9b1e3e, run_66b65bc32dc8) showed _summarize_llm() will confidently
    # fabricate a full narrative from the model's own prior knowledge when handed empty
    # pain_points/feature_requests/praised_aspects, rather than declining. Those are
    # exactly the three inputs _summarize_llm() is given below, so when all three are
    # empty there is nothing for it to ground a narrative in -- skip the LLM call
    # entirely and fall through to the same deterministic _summarize_fallback() already
    # used when no LLM is configured, which correctly reports "not enough evidence"
    # instead of inventing one.
    has_report_content = bool(
        report_inputs.top_pain_points or report_inputs.feature_requests or report_inputs.praised_aspects
    )

    if has_report_content and llm.available():
        try:
            narrative = _summarize_llm(
                product_category, collected, report_inputs.top_pain_points, report_inputs.feature_requests,
                report_inputs.praised_aspects, llm,
            )
        except Exception:
            narrative = _summarize_fallback(product_category, report_inputs.top_pain_points, report_inputs.feature_requests)
    else:
        narrative = _summarize_fallback(product_category, report_inputs.top_pain_points, report_inputs.feature_requests)

    return Report(
        run_id=run_id,
        generated_at=utc_now(),
        top_pain_points=report_inputs.top_pain_points,
        feature_requests=report_inputs.feature_requests,
        praised_aspects=report_inputs.praised_aspects,
        competitor_mentions=report_inputs.competitor_mentions,
        sentiment_breakdown=sentiment_breakdown,
        recommended_actions=narrative["recommended_actions"],
        summary_markdown=narrative["summary_markdown"],
        subreddits=sorted({item.subreddit for item in collected}),
        subreddit_counts=dict(Counter(item.subreddit for item in collected)),
        recommended_actions_zh=narrative["recommended_actions_zh"],
        summary_markdown_zh=narrative["summary_markdown_zh"],
        shipping_issues=report_inputs.shipping_issues,
        seller_service_issues=report_inputs.seller_service_issues,
        report_source=report_source,
        fallback_reason=fallback_reason,
    )


def _aggregate_by_aspect(collected: list[Evidence], insight_type: InsightType) -> list[dict[str, Any]]:
    matching = [item for item in collected if item.insight_type == insight_type]
    grouped: dict[str, list[Evidence]] = {}
    for item in matching:
        grouped.setdefault(item.aspect, []).append(item)
    aggregated = []
    for aspect, items in grouped.items():
        items_sorted = sorted(items, key=lambda evidence: evidence.score, reverse=True)
        aggregated.append(
            {
                "aspect": aspect,
                "count": len(items),
                "subreddit_count": len({evidence.subreddit for evidence in items}),
                "avg_confidence": round(sum(evidence.confidence for evidence in items) / len(items), 2),
                "sentiment_counts": dict(Counter(evidence.sentiment.value for evidence in items)),
                "example_quotes": [
                    {"quote": evidence.quote, "source_url": evidence.source_url, "subreddit": evidence.subreddit}
                    for evidence in items_sorted[:3]
                ],
            }
        )
    aggregated.sort(key=lambda entry: entry["count"], reverse=True)
    return aggregated


def _summarize_llm(
    product_category: str,
    collected: list[Evidence],
    pain_points: list[dict[str, Any]],
    feature_requests: list[dict[str, Any]],
    praised: list[dict[str, Any]],
    llm: DeepSeekClient,
) -> dict[str, Any]:
    # Phase 4, P1 (docs/phase4_merchant_decision_support_plan.md): rewritten after real
    # reports showed this prompt let the model invent unsupported pricing/strategy advice
    # (run_55025c50e81b: "reassess pricing strategy... mid-tier options") and fabricated
    # false-precision specs never present in the input (run_55025c50e81b: "5200 mAh",
    # "5-10 Hz"; run_89b02b9b1e3e: "under 55 dB", "2.5 hours"). Evidence-boundary,
    # prohibited-content, grounding, and conservative-style rules below address those
    # findings directly. Does NOT add an empty-input instruction -- P0 (the guard in
    # summarize(), above) is the sole control-flow handler for that case; this prompt is
    # only ever reached when there is at least one pain point, feature request, or
    # praised aspect to ground a narrative in.
    system = (
        "You are a senior product analyst preparing a report for a merchant, based ONLY on the aggregated "
        "Reddit evidence explicitly supplied to you below: the labels, counts, sentiment breakdowns, "
        "confidence values, and example quotes in top_pain_points, feature_requests, and praised_aspects. "
        "Do not use general knowledge about this product category, this company, competitors, or the "
        "market. If a claim is not directly supported by the supplied data, do not make it.\n\n"
        "Do not invent, estimate, or assume any of the following unless the exact figure or claim already "
        "appears in the supplied data: return on investment or revenue impact; pricing strategy or price "
        "changes; implementation, manufacturing, or unit cost; market or business strategy; product "
        "positioning; a competitor's strategy or intentions; technical specifications; or any numeric "
        "measurement, percentage, duration, capacity, frequency, threshold, or target value. This is a "
        "general rule covering every unsupported number or claim, not only the categories just listed — "
        "for example, writing '5200 mAh', '5-10 Hz', '80% in 30 minutes', 'under 55 dB', '2.5 hours', "
        "'lower the price', or 'introduce a mid-tier model' is prohibited unless that exact figure or idea "
        "is already present in the supplied pain points, feature requests, or praised aspects.\n\n"
        "Every entry in recommended_actions_en/recommended_actions_zh must explicitly name the exact "
        "aggregate label (the 'aspect' value) it addresses, taken verbatim from top_pain_points, "
        "feature_requests, or praised_aspects below, so it can be matched back to that input entry. Never "
        "invent a new label and never silently paraphrase a supplied label into wording that no longer "
        "matches it.\n\n"
        "Keep each recommendation short and conservative: state what the evidence suggests investigating "
        "or prioritizing, do not claim certainty the evidence doesn't support, do not prescribe a specific "
        "engineering solution unless that exact solution is already present in the supplied data, and do "
        "not present business or product strategy as settled fact. Acceptable style: 'Consider "
        "prioritizing improvements related to \"floor damage\" because it is one of the most frequently "
        "supported pain points in this run.' Unacceptable style: 'Increase sensor frequency to 10 Hz and "
        "reduce the price by 20%.'\n\n"
        "Also write a short Markdown summary (a few sections, no more than ~300 words), grounded the same "
        "way. Write the full report TWICE, once in English and once in Simplified Chinese — both genuinely "
        "composed in that language with matching content and structure, not a translation note or "
        "placeholder. Return only JSON."
    )
    user = json.dumps(
        {
            "product_category": product_category,
            "evidence_count": len(collected),
            "top_pain_points": pain_points[:8],
            "feature_requests": feature_requests[:8],
            "praised_aspects": praised[:5],
            "expected_json": {
                "recommended_actions_en": [
                    "3 to 6 short, conservative recommendations in English; each must name the exact "
                    "aggregate label (aspect) it addresses from top_pain_points/feature_requests/"
                    "praised_aspects above, and must not invent pricing, cost, strategy, or specs not "
                    "present in the supplied data"
                ],
                "recommended_actions_zh": ["the same 3 to 6 recommendations, written in Simplified Chinese"],
                "summary_markdown_en": "a short Markdown-formatted summary report, in English",
                "summary_markdown_zh": "the same summary report, written in Simplified Chinese",
            },
        },
        ensure_ascii=False,
    )
    parsed = llm.json_chat(pro_model(), system, user)

    def _actions(key: str) -> list[str]:
        value = parsed.get(key)
        return [str(item) for item in value] if isinstance(value, list) else []

    return {
        "recommended_actions": _actions("recommended_actions_en"),
        "recommended_actions_zh": _actions("recommended_actions_zh"),
        "summary_markdown": str(parsed.get("summary_markdown_en") or ""),
        "summary_markdown_zh": str(parsed.get("summary_markdown_zh") or ""),
    }


def _summarize_fallback(product_category: str, pain_points: list[dict[str, Any]], feature_requests: list[dict[str, Any]]) -> dict[str, Any]:
    actions = [
        f'"{entry["aspect"]}" has a high volume of feedback ({entry["count"]} item(s)); recommend investigating and improving it first.'
        for entry in pain_points[:5]
    ]
    actions.extend(
        f'Users repeatedly requested a "{entry["aspect"]}" feature ({entry["count"]} mention(s)); consider adding it to the product roadmap.'
        for entry in feature_requests[:3]
    )
    if not actions:
        actions = ["Not enough negative or feature-request feedback was collected; consider broadening the search or extending the observation window."]
    lines = [f"# {product_category} Reddit User Feedback Report", ""]
    lines.append("## Top Pain Points")
    for entry in pain_points[:5]:
        lines.append(f"- **{entry['aspect']}**: {entry['count']} piece(s) of evidence")
    lines.append("")
    lines.append("## Feature Requests")
    for entry in feature_requests[:5]:
        lines.append(f"- **{entry['aspect']}**: {entry['count']} piece(s) of evidence")

    # Hardcoded, not translated at request time: this path only runs when there's no
    # DeepSeek key to call in the first place (see summarize()'s caller), so producing
    # the Chinese version has to be a template too, same as the English one above.
    actions_zh = [
        f'"{entry["aspect"]}" 相关反馈量较高({entry["count"]} 条),建议优先调查并改进。'
        for entry in pain_points[:5]
    ]
    actions_zh.extend(
        f'用户多次提出希望增加"{entry["aspect"]}"功能({entry["count"]} 次提及),建议纳入产品路线图。'
        for entry in feature_requests[:3]
    )
    if not actions_zh:
        actions_zh = ["收集到的负面反馈或功能请求样本不足,建议扩大搜索范围或延长观察窗口。"]
    lines_zh = [f"# {product_category} 用户反馈报告(规则兜底,未使用大模型)", ""]
    lines_zh.append("## 主要痛点")
    for entry in pain_points[:5]:
        lines_zh.append(f"- **{entry['aspect']}**:{entry['count']} 条相关证据")
    lines_zh.append("")
    lines_zh.append("## 功能请求")
    for entry in feature_requests[:5]:
        lines_zh.append(f"- **{entry['aspect']}**:{entry['count']} 条相关证据")

    return {
        "recommended_actions": actions,
        "recommended_actions_zh": actions_zh,
        "summary_markdown": "\n".join(lines),
        "summary_markdown_zh": "\n".join(lines_zh),
    }
