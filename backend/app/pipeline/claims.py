from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError

from ..llm import DeepSeekClient, fast_model, load_dotenv
from ..models import Claim, ClaimType, Evidence, Sentiment
from ..text import classify_insight_type, detect_aspects, detect_sentiment

_DEFAULT_MAX_CLAIMS_PER_REVIEW = 8


def enable_claim_extraction() -> bool:
    """Kill switch for the second (claim-extraction) LLM call per relevant item.

    Claim extraction runs in addition to the existing analyze_item() call, not
    instead of it -- two fast-model calls per relevant item, not one. This flag
    lets that second call be turned off with no code change if cost becomes a
    problem, without touching analyze_item()/the legacy aspect-report path.
    """
    load_dotenv()
    raw = os.environ.get("ENABLE_CLAIM_EXTRACTION", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def max_claims_per_review() -> int:
    load_dotenv()
    try:
        return max(1, int(os.environ.get("MAX_CLAIMS_PER_REVIEW", str(_DEFAULT_MAX_CLAIMS_PER_REVIEW))))
    except ValueError:
        return _DEFAULT_MAX_CLAIMS_PER_REVIEW


@dataclass
class ClaimExtractionStats:
    llm_claims: int = 0
    fallback_claims: int = 0
    invalid_claims: int = 0
    extraction_failures: int = 0


@dataclass
class ClaimExtractionResult:
    claims: list[Claim]
    # False only if BOTH the LLM path and the fallback path raised -- the only
    # case where the caller must NOT call storage.replace_claims_for_evidence(),
    # so a transient failure never erases claims a previous successful
    # extraction already stored. True (even with an empty `claims` list) means
    # "extraction genuinely completed" and IS safe to replace with.
    succeeded: bool
    stats: ClaimExtractionStats


def extract_claims(product_category: str, evidence: Evidence, llm: DeepSeekClient) -> ClaimExtractionResult:
    """Extracts zero or more atomic Claims from one piece of Evidence.

    Mirrors the llm.available() -> try -> except -> fallback shape used
    throughout react_agent.py. Never raises -- always returns a
    ClaimExtractionResult, so the caller can decide what to do with a failure
    without an extra try/except of its own.
    """
    if llm.available():
        try:
            claims, stats = _extract_claims_llm(product_category, evidence, llm)
            return ClaimExtractionResult(claims=claims, succeeded=True, stats=stats)
        except Exception:
            pass  # fall through to the deterministic path below
    try:
        claims, stats = _extract_claims_fallback(evidence)
        return ClaimExtractionResult(claims=claims, succeeded=True, stats=stats)
    except Exception:
        return ClaimExtractionResult(claims=[], succeeded=False, stats=ClaimExtractionStats(extraction_failures=1))


# ---------------------------------------------------------------------------
# LLM path -- strict, per-item Pydantic validation (no silent coercion)
# ---------------------------------------------------------------------------


class RawClaim(BaseModel):
    claim_type: ClaimType  # unrecognized value raises ValidationError, is skipped -- never coerced to NOISE
    aspect_raw: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    sentiment: Sentiment = Sentiment.NEUTRAL
    confidence: float = Field(default=0.7, ge=0, le=1)
    severity: float | None = Field(default=None, ge=0, le=1)
    subject: str | None = None
    explicit_request: str | None = None


class RawClaimResponse(BaseModel):
    claims: list[dict]  # validated item-by-item in _extract_claims_llm, not as a batch


def _extract_claims_llm(product_category: str, evidence: Evidence, llm: DeepSeekClient) -> tuple[list[Claim], ClaimExtractionStats]:
    system = (
        "You are an atomic-claim extraction agent for a customer-feedback analysis system. "
        "Given one review/comment about the given product category, break it into zero or "
        "more independent atomic claims -- each claim is one distinct opinion, problem, "
        "feature request, comparison, or observation, not a summary of the whole review. "
        "A review may yield zero claims (e.g. pure noise/shipping/unrelated content) or "
        "several (e.g. one complaint plus one feature request plus one praise, if the text "
        "genuinely contains all three). Write `statement` as a short, neutral, English "
        "paraphrase of the claim in your own words -- it is an interpretation, never a "
        "verbatim copy of the review's wording. Return only JSON."
    )
    user = json.dumps(
        {
            "product_category": product_category,
            "review": {"title": evidence.title, "body": evidence.body[:1800]},
            "expected_json": {
                "claims": [
                    {
                        "claim_type": "problem | feature_request | praise | comparison | question | observation | shipping_issue | seller_service_issue | noise",
                        "aspect_raw": "short label, e.g. battery life, charging case size",
                        "statement": "a short English paraphrase of the claim, NOT a verbatim quote",
                        "sentiment": "negative | neutral | positive",
                        "confidence": "0 to 1, how confident you are this is a genuine, distinct claim",
                        "severity": "0 to 1, optional, how serious the issue reads (omit if not applicable)",
                        "subject": "optional -- who/what the claim is about",
                        "explicit_request": "optional -- if the claim is a direct request, what was explicitly asked for",
                    }
                ]
            },
        },
        ensure_ascii=False,
    )
    parsed = llm.json_chat(fast_model(), system, user)
    top_level = RawClaimResponse.model_validate(parsed)  # raises if "claims" is missing / not a list

    limit = max_claims_per_review()
    claims: list[Claim] = []
    seen_ids: set[str] = set()
    invalid_count = 0
    for raw in top_level.claims:
        if len(claims) >= limit:
            break
        try:
            item = RawClaim.model_validate(raw)
        except ValidationError:
            # Skip only this one malformed claim -- do not reject the whole batch.
            invalid_count += 1
            continue
        candidate = _build_claim(
            evidence=evidence,
            claim_type=item.claim_type,
            aspect_raw=item.aspect_raw,
            statement=item.statement,
            sentiment=item.sentiment,
            confidence=item.confidence,
            extraction_method="llm",
            subject=item.subject,
            explicit_request=item.explicit_request,
            severity=item.severity,
        )
        if candidate.claim_id in seen_ids:  # exact duplicate within this response -- collapse, don't double-count
            continue
        seen_ids.add(candidate.claim_id)
        claims.append(candidate)

    if not claims and top_level.claims:
        # Every raw claim in a non-empty list failed validation -- the response is
        # unusable, not "genuinely zero claims". Force the caller to fall back.
        raise ValueError(f"all {len(top_level.claims)} raw claim(s) failed validation")

    return claims, ClaimExtractionStats(llm_claims=len(claims), invalid_claims=invalid_count)


# ---------------------------------------------------------------------------
# Fallback path -- deterministic, reuses text.py's regex primitives.
# Deliberately crude: one low-confidence claim per matched aspect keyword, no
# multi-claim splitting or semantic reasoning. Must not be mistaken for the
# LLM path's quality -- extraction_method="fallback_rules" marks every claim
# produced here.
# ---------------------------------------------------------------------------

_INSIGHT_TYPE_TO_CLAIM_TYPE: dict[str, ClaimType] = {
    "pain_point": ClaimType.PROBLEM,
    "feature_request": ClaimType.FEATURE_REQUEST,
    "comparison": ClaimType.COMPARISON,
    "praise": ClaimType.PRAISE,
    "question": ClaimType.QUESTION,
    "noise": ClaimType.OBSERVATION,
}


def _extract_claims_fallback(evidence: Evidence) -> tuple[list[Claim], ClaimExtractionStats]:
    text = f"{evidence.title}\n{evidence.body}"
    aspects = detect_aspects(text)
    sentiment_str = detect_sentiment(text)
    claim_type = _INSIGHT_TYPE_TO_CLAIM_TYPE.get(classify_insight_type(text), ClaimType.OBSERVATION)
    sentiment = Sentiment(sentiment_str)
    limit = max_claims_per_review()

    claims = [
        _build_claim(
            evidence=evidence,
            claim_type=claim_type,
            aspect_raw=aspect,
            statement=f"Review mentions {aspect.replace('_', ' ')}; overall tone reads {sentiment_str}.",
            sentiment=sentiment,
            confidence=0.5,
            extraction_method="fallback_rules",
            subject=None,
            explicit_request=None,
            severity=None,
        )
        for aspect in aspects[:limit]
    ]
    return claims, ClaimExtractionStats(fallback_claims=len(claims))


# ---------------------------------------------------------------------------
# Shared claim construction -- stable, content-derived identity
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _claim_id(evidence_id: str, claim_type: ClaimType, aspect_raw: str, statement: str) -> str:
    """Hashes normalized CONTENT fields only -- never a list index or other
    positional data. Two claims extracted from the same evidence with the same
    type/aspect/statement (even across separate LLM calls, e.g. a rerun)
    collapse onto the same id, so duplicates never create duplicate rows and
    reruns are idempotent by construction."""
    key = f"{evidence_id}|{claim_type.value}|{_normalize(aspect_raw)}|{_normalize(statement)}"
    return "cl_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _build_claim(
    evidence: Evidence,
    claim_type: ClaimType,
    aspect_raw: str,
    statement: str,
    sentiment: Sentiment,
    confidence: float,
    extraction_method: str,
    subject: str | None,
    explicit_request: str | None,
    severity: float | None,
) -> Claim:
    return Claim(
        claim_id=_claim_id(evidence.evidence_id, claim_type, aspect_raw, statement),
        run_id=evidence.run_id,
        evidence_id=evidence.evidence_id,
        claim_type=claim_type,
        aspect_raw=aspect_raw,
        statement=statement,
        sentiment=sentiment,
        confidence=round(max(0.0, min(confidence, 1.0)), 2),
        extraction_method=extraction_method,
        subject=subject,
        explicit_request=explicit_request,
        severity=None if severity is None else round(max(0.0, min(severity, 1.0)), 2),
    )
