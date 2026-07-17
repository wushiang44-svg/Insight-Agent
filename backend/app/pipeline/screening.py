from __future__ import annotations

import json
import os
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..llm import DeepSeekClient, fast_model, load_dotenv
from ..models import CollectedItem, InsightType, ScreeningCategory, Sentiment
from ..text import (
    classify_insight_type,
    detect_aspects,
    detect_sentiment,
    screening_categories_for_aspects,
    short_quote,
)

# Starting heuristic for the fallback path only -- text shorter than this with
# no matched aspect is treated as too thin to carry signal, rather than spam.
# Flagged in the Phase 2 plan as needing real-data validation/tuning.
_LOW_INFORMATION_MAX_CHARS = 40

_DISCARD_ONLY = {ScreeningCategory.SPAM_OR_IRRELEVANT, ScreeningCategory.LOW_INFORMATION}


def enable_screening_v2() -> bool:
    """Kill switch for screen_item()'s LLM path (Phase 2). Off means every item
    is screened by the deterministic fallback -- mirrors enable_claim_extraction()'s
    pattern in pipeline/claims.py.
    """
    load_dotenv()
    raw = os.environ.get("ENABLE_SCREENING_V2", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


@dataclass
class ScreeningResult:
    categories: list[ScreeningCategory]  # multi-label -- all that materially apply
    # has_product_signal/is_mixed_content/is_evidence_worthy are always derived
    # from `categories` (see _derive below), never asked of the model as
    # separate fields -- a single source of truth, no contradiction risk.
    #
    # has_product_signal is METADATA ONLY. It must never gate Claim extraction:
    # doing so would recreate a smaller-scale version of the exact bug Phase 2
    # exists to fix (a coarse upstream judgment silently discarding real
    # product signal). is_evidence_worthy is the only hard discard signal.
    has_product_signal: bool
    is_mixed_content: bool
    is_evidence_worthy: bool
    # Legacy-compatible fields -- same semantics as the old analyze_item(),
    # still feed Evidence/the aggregate Report/check_sufficiency unchanged.
    insight_type: InsightType
    aspect: str
    sentiment: Sentiment
    quote: str
    confidence: float
    extraction_method: str  # "llm" | "fallback_rules"


def _derive(categories: list[ScreeningCategory]) -> tuple[bool, bool, bool]:
    has_product_signal = ScreeningCategory.PRODUCT_FEEDBACK in categories
    is_mixed_content = len(categories) > 1
    is_evidence_worthy = bool(categories) and not set(categories) <= _DISCARD_ONLY
    return has_product_signal, is_mixed_content, is_evidence_worthy


def screen_item(product_category: str, item: CollectedItem, llm: DeepSeekClient) -> ScreeningResult:
    """Classifies one CollectedItem's feedback type(s) -- multi-label, replacing
    the old analyze_item()'s single binary is_relevant verdict. Never raises --
    always returns a ScreeningResult, mirroring the llm.available() -> try ->
    except -> fallback shape used throughout this codebase.
    """
    if enable_screening_v2() and llm.available():
        try:
            return _screen_item_llm(product_category, item, llm)
        except Exception:
            pass  # fall through to the deterministic path below
    return _screen_item_fallback(item)


# ---------------------------------------------------------------------------
# LLM path -- strict Pydantic validation, no silent coercion of invalid output
# ---------------------------------------------------------------------------


class RawScreening(BaseModel):
    categories: list[ScreeningCategory] = Field(min_length=1)  # unrecognized value or empty list raises -> fallback
    insight_type: InsightType = InsightType.NOISE
    aspect: str = "general"
    sentiment: Sentiment = Sentiment.NEUTRAL
    quote: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)


def _screen_item_llm(product_category: str, item: CollectedItem, llm: DeepSeekClient) -> ScreeningResult:
    system = (
        "You are a review-screening agent for a customer-feedback analysis system. "
        "Classify the given post/comment about the given product category into one or "
        "more categories -- many reviews genuinely belong to more than one when they "
        "cover more than one topic, and you must list every category that materially "
        "applies, not just whichever seems dominant:\n"
        "- `product_feedback`: any genuine opinion, complaint, praise, comparison, or "
        "question about the product itself (its features, quality, performance).\n"
        "- `shipping_logistics`: about delivery, packaging, or the shipping process.\n"
        "- `seller_service`: about the seller/merchant's customer service, warranty, "
        "returns, or support -- not the product itself.\n"
        "- `spam_or_irrelevant`: not genuine feedback at all (spam, off-topic, "
        "unrelated tangent).\n"
        "- `low_information`: too short or vague to carry any real signal.\n\n"
        "IMPORTANT: a review that is mostly about shipping or service can STILL also "
        "contain real product feedback -- e.g. \"Shipping was terrible, but the battery "
        "only lasts three hours\" is BOTH `shipping_logistics` AND `product_feedback`, "
        "never just whichever reads as the dominant tone. Always check for product "
        "signal independently of everything else the text is about. Only use "
        "`spam_or_irrelevant`/`low_information` when there is truly nothing else "
        "present -- never as a way to summarize a review whose dominant topic happens "
        "to be shipping or service.\n\n"
        "IMPORTANT: classify based on actual content, never on a self-declared label the "
        "text applies to itself. A reviewer writing \"this is not a review\" or going on "
        "a long unrelated tangent (a safety warning, a personal story, an off-topic rant) "
        "does NOT make the whole text spam or irrelevant if the product is still genuinely "
        "discussed somewhere in it. Include `product_feedback` whenever the text contains "
        "ANY concrete product experience, evaluation, problem, feature request, comparison, "
        "or usage observation -- e.g. \"my water boiler is still running strong\" or \"the "
        "button is still a bit hard to press\" -- even if that is a small fraction of a "
        "much longer, mostly off-topic text. Reserve `spam_or_irrelevant` for text with NO "
        "meaningful product-related signal anywhere in it, not for text that is merely "
        "dominated by something else.\n\n"
        "Also provide: `insight_type` (pain_point | feature_request | comparison | "
        "praise | question | noise -- the single most representative type for the "
        "review as a whole), `aspect` (short label for the single most prominent "
        "topic, e.g. battery, price, durability, shipping, customer_service), "
        "`sentiment`, a short representative `quote` (verbatim or lightly trimmed, "
        "original language), and `confidence` (0 to 1). Return only JSON."
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
                "categories": [
                    "one or more of: product_feedback, shipping_logistics, seller_service, "
                    "spam_or_irrelevant, low_information"
                ],
                "insight_type": "pain_point | feature_request | comparison | praise | question | noise",
                "aspect": "short label, e.g. battery, price, durability, shipping, customer_service",
                "sentiment": "negative | neutral | positive",
                "quote": "short representative quote, original language",
                "confidence": "0 to 1",
            },
        },
        ensure_ascii=False,
    )
    parsed = llm.json_chat(fast_model(), system, user)
    raw = RawScreening.model_validate(parsed)

    categories = list(dict.fromkeys(raw.categories))  # dedupe, preserve order
    has_product_signal, is_mixed_content, is_evidence_worthy = _derive(categories)
    return ScreeningResult(
        categories=categories,
        has_product_signal=has_product_signal,
        is_mixed_content=is_mixed_content,
        is_evidence_worthy=is_evidence_worthy,
        insight_type=raw.insight_type,
        aspect=raw.aspect,
        sentiment=raw.sentiment,
        quote=raw.quote or short_quote(f"{item.title}\n{item.body}"),
        confidence=round(max(0.0, min(raw.confidence, 1.0)), 2),
        extraction_method="llm",
    )


# ---------------------------------------------------------------------------
# Fallback path -- deterministic, reuses text.py's regex primitives. Builds
# the full multi-label category set from detect_aspects()'s complete match
# list (screening_categories_for_aspects), not just the first aspect the way
# the old analyze_item() fallback did -- a real correctness improvement using
# data the fallback was already computing, no new pattern-matching needed.
# ---------------------------------------------------------------------------


def _screen_item_fallback(item: CollectedItem) -> ScreeningResult:
    text = f"{item.title}\n{item.body}"
    aspects = detect_aspects(text)
    category_values = screening_categories_for_aspects(aspects)
    if not category_values:
        category_values = (
            [ScreeningCategory.LOW_INFORMATION.value]
            if len(text.strip()) < _LOW_INFORMATION_MAX_CHARS
            else [ScreeningCategory.SPAM_OR_IRRELEVANT.value]
        )
    categories = [ScreeningCategory(value) for value in category_values]
    has_product_signal, is_mixed_content, is_evidence_worthy = _derive(categories)

    return ScreeningResult(
        categories=categories,
        has_product_signal=has_product_signal,
        is_mixed_content=is_mixed_content,
        is_evidence_worthy=is_evidence_worthy,
        insight_type=InsightType(classify_insight_type(text)),
        aspect=aspects[0] if aspects else "general",
        sentiment=Sentiment(detect_sentiment(text)),
        quote=short_quote(text),
        confidence=0.6 if aspects else 0.35,
        extraction_method="fallback_rules",
    )
