from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Callable

from .collectors.base import Collector
from .llm import DeepSeekClient, fast_model, pro_model
from .models import (
    CollectedItem,
    Evidence,
    InsightType,
    Report,
    RunStatus,
    Sentiment,
    StepType,
    TraceEvent,
    utc_now,
)
from .storage import Storage
from .text import (
    classify_insight_type,
    detect_aspects,
    detect_sentiment,
    short_quote,
)

DIMINISHING_RETURNS_WINDOW = 2


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
                trace(
                    iteration,
                    StepType.ACTION_SEARCH,
                    f'Searched "{thought["query"]}", got {len(items)} result(s)',
                    {"query": thought["query"], "subreddit": thought["subreddit"], "items_returned": len(items)},
                )
            except Exception as exc:  # noqa: BLE001 - a failed search should not crash the whole run
                items = []
                trace(iteration, StepType.ACTION_SEARCH, f"Search failed: {exc}", {"query": thought["query"], "error": str(exc)})

            tried_queries.append({"query": thought["query"], "subreddit": thought["subreddit"]})

            new_evidence: list[Evidence] = []
            for item in items:
                if item.source_url in seen_urls:
                    continue
                seen_urls.add(item.source_url)
                analysis = analyze_item(run.product_category, item, llm)
                if not analysis["is_relevant"]:
                    continue
                evidence = _build_evidence(run_id, iteration, item, analysis)
                storage.save_evidence(evidence)
                new_evidence.append(evidence)

            collected.extend(new_evidence)
            new_counts.append(len(new_evidence))
            trace(
                iteration,
                StepType.OBSERVATION,
                f"Analyzed {len(items)} result(s), kept {len(new_evidence)} relevant item(s) (total {len(collected)})",
                {"items_analyzed": len(items), "new_evidence": len(new_evidence), "total_evidence": len(collected)},
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
# Observe: relevance + structured analysis of one Reddit item
# ---------------------------------------------------------------------------

def analyze_item(product_category: str, item: CollectedItem, llm: DeepSeekClient) -> dict[str, Any]:
    if llm.available():
        try:
            return _analyze_item_llm(product_category, item, llm)
        except Exception:
            pass
    return _analyze_item_fallback(item)


def _analyze_item_llm(product_category: str, item: CollectedItem, llm: DeepSeekClient) -> dict[str, Any]:
    system = (
        "You are an evidence analyst mining Reddit for product feedback on behalf of a merchant. Decide whether "
        "this post/comment is genuinely about the given product category (not spam, not an unrelated tangent). "
        "If relevant, classify it and extract one short representative quote (verbatim or lightly trimmed, in the "
        "original language of the text). Write all reasoning/quote-adjacent text fields in English "
        "except the quote field, which should stay in the original language. Return only JSON."
    )
    user = json.dumps(
        {
            "product_category": product_category,
            "post": {
                "title": item.title,
                "body": item.body[:1800],
                "subreddit": item.subreddit,
                "item_type": item.item_type,
                "score": item.score,
                "search_query": item.search_query,
            },
            "expected_json": {
                "is_relevant": "true only if this is genuine user discussion about the product category",
                "insight_type": "pain_point | feature_request | comparison | praise | question | noise",
                "aspect": "short label such as battery, price, durability, shipping, customer_service, quality, comfort, ease_of_use",
                "sentiment": "negative | neutral | positive",
                "quote": "short representative quote from the text, original language",
                "confidence": "0 to 1",
            },
        },
        ensure_ascii=False,
    )
    parsed = llm.json_chat(fast_model(), system, user)
    is_relevant = bool(parsed.get("is_relevant"))
    if not is_relevant:
        return {"is_relevant": False}
    insight_type = str(parsed.get("insight_type") or "noise").strip().lower()
    if insight_type not in {item.value for item in InsightType}:
        insight_type = InsightType.NOISE.value
    sentiment = str(parsed.get("sentiment") or "neutral").strip().lower()
    if sentiment not in {item.value for item in Sentiment}:
        sentiment = Sentiment.NEUTRAL.value
    try:
        confidence = max(0.0, min(float(parsed.get("confidence", 0.5)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "is_relevant": True,
        "insight_type": insight_type,
        "aspect": str(parsed.get("aspect") or "general").strip() or "general",
        "sentiment": sentiment,
        "quote": str(parsed.get("quote") or short_quote(f"{item.title} {item.body}")),
        "confidence": round(confidence, 2),
    }


def _analyze_item_fallback(item: CollectedItem) -> dict[str, Any]:
    text = f"{item.title}\n{item.body}"
    aspects = detect_aspects(text)
    insight_type = classify_insight_type(text)
    if insight_type == "noise" and not aspects:
        return {"is_relevant": False}
    return {
        "is_relevant": True,
        "insight_type": insight_type,
        "aspect": aspects[0] if aspects else "general",
        "sentiment": detect_sentiment(text),
        "quote": short_quote(text),
        "confidence": 0.6 if aspects else 0.35,
    }


def _build_evidence(run_id: str, iteration: int, item: CollectedItem, analysis: dict[str, Any]) -> Evidence:
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
        insight_type=InsightType(analysis["insight_type"]),
        aspect=analysis["aspect"],
        sentiment=Sentiment(analysis["sentiment"]),
        quote=analysis["quote"],
        confidence=analysis["confidence"],
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
        "markdown summary (a few sections, no more than ~300 words). Respond entirely in English. "
        "Return only JSON."
    )
    user = json.dumps(
        {
            "product_category": product_category,
            "evidence_count": len(collected),
            "top_pain_points": pain_points[:8],
            "feature_requests": feature_requests[:8],
            "praised_aspects": praised[:5],
            "expected_json": {
                "recommended_actions": ["3 to 6 concrete, actionable product-improvement recommendations"],
                "summary_markdown": "a short Markdown-formatted summary report",
            },
        },
        ensure_ascii=False,
    )
    parsed = llm.json_chat(pro_model(), system, user)
    actions = parsed.get("recommended_actions")
    return {
        "recommended_actions": [str(item) for item in actions] if isinstance(actions, list) else [],
        "summary_markdown": str(parsed.get("summary_markdown") or ""),
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
    return {"recommended_actions": actions, "summary_markdown": "\n".join(lines)}
