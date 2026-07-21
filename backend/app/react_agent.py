from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .collectors.base import Collector
from .llm import DeepSeekClient, fast_model, pro_model
from .models import (
    CollectedItem,
    Evidence,
    InsightType,
    Report,
    RunStatus,
    StepType,
    TraceEvent,
    utc_now,
)
from .pipeline.claims import enable_claim_extraction, extract_claims
from .pipeline.screening import ScreeningResult, screen_item
from .storage import Storage

DIMINISHING_RETURNS_WINDOW = 2


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
        report = summarize(run_id, run.product_category, collected, llm)
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
# Summarize: turn collected evidence into a merchant-facing report
# ---------------------------------------------------------------------------

def summarize(run_id: str, product_category: str, collected: list[Evidence], llm: DeepSeekClient) -> Report:
    pain_points = _aggregate_by_aspect(collected, InsightType.PAIN_POINT)
    feature_requests = _aggregate_by_aspect(collected, InsightType.FEATURE_REQUEST)
    praised = _aggregate_by_aspect(collected, InsightType.PRAISE)
    comparisons = _aggregate_by_aspect(collected, InsightType.COMPARISON)
    sentiment_breakdown = dict(Counter(item.sentiment.value for item in collected))

    if llm.available():
        try:
            narrative = _summarize_llm(product_category, collected, pain_points, feature_requests, praised, llm)
        except Exception:
            narrative = _summarize_fallback(product_category, pain_points, feature_requests)
    else:
        narrative = _summarize_fallback(product_category, pain_points, feature_requests)

    return Report(
        run_id=run_id,
        generated_at=utc_now(),
        top_pain_points=pain_points,
        feature_requests=feature_requests,
        praised_aspects=praised,
        competitor_mentions=comparisons,
        sentiment_breakdown=sentiment_breakdown,
        recommended_actions=narrative["recommended_actions"],
        summary_markdown=narrative["summary_markdown"],
        subreddits=sorted({item.subreddit for item in collected}),
        subreddit_counts=dict(Counter(item.subreddit for item in collected)),
        recommended_actions_zh=narrative["recommended_actions_zh"],
        summary_markdown_zh=narrative["summary_markdown_zh"],
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
    system = (
        "You are a senior product analyst preparing a report for a merchant, based on aggregated Reddit evidence "
        "about their product category. Write concrete, specific, actionable recommendations the merchant can use "
        "to improve the product, grounded in the aggregated pain points and feature requests. Also write a short "
        "markdown summary (a few sections, no more than ~300 words). Write the full report TWICE, once in English "
        "and once in Simplified Chinese — both genuinely composed in that language with matching content and "
        "structure, not a translation note or placeholder. Return only JSON."
    )
    user = json.dumps(
        {
            "product_category": product_category,
            "evidence_count": len(collected),
            "top_pain_points": pain_points[:8],
            "feature_requests": feature_requests[:8],
            "praised_aspects": praised[:5],
            "expected_json": {
                "recommended_actions_en": ["3 to 6 concrete, actionable product-improvement recommendations, in English"],
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
