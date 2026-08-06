from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..llm import DeepSeekClient, fast_model, load_dotenv
from ..models import Claim, ClaimType, Evidence, Sentiment
from ..text import classify_insight_type, detect_aspects, detect_sentiment, find_aspect_excerpt, simple_similarity

_DEFAULT_MAX_CLAIMS_PER_REVIEW = 20
# Hardcoded, not user-configurable: bounds worst-case O(n^2) merge-pass cost and
# ambiguous-pair LLM-verification batch size against a pathological LLM response,
# independent of MAX_CLAIMS_PER_REVIEW (which controls granularity, not safety).
# Well above the largest genuine count observed in real validation data (18).
_RAW_INGESTION_CEILING = 40


def enable_claim_extraction() -> bool:
    """Kill switch for the second (claim-extraction) LLM call per evidence-worthy item.

    Claim extraction runs in addition to the screening call (screen_item(),
    pipeline/screening.py), not instead of it -- two fast-model calls per
    evidence-worthy item, not one. This flag lets that second call be turned
    off with no code change if cost becomes a problem, without touching
    screen_item()/the legacy aspect-report path.
    """
    load_dotenv()
    raw = os.environ.get("ENABLE_CLAIM_EXTRACTION", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def enable_claim_merge_llm_verification() -> bool:
    """Kill switch for ONLY the batched ambiguous-pair verification call inside
    the within-review merge pass (Phase 1.6). The lexical auto-merge tiers stay
    on regardless -- they're free and unambiguous. Off means every ambiguous
    pair defaults to "not merged" (same fail-closed behavior as a malformed
    verification response), never that merging stops entirely.
    """
    load_dotenv()
    raw = os.environ.get("ENABLE_CLAIM_MERGE_LLM_VERIFICATION", "true").strip().lower()
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
    # Phase 1.6 observability -- the full raw -> merged -> capped funnel for one
    # Evidence's extraction. raw_claims_extracted ~= final_claims_saved +
    # within_review_duplicates_removed + safety_cap_truncations (+ invalid_claims,
    # which are dropped before any of this and counted separately above).
    raw_claims_extracted: int = 0
    final_claims_saved: int = 0
    within_review_duplicates_removed: int = 0
    claims_merged: int = 0
    safety_cap_truncations: int = 0


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
    source_excerpt: str | None = Field(default=None, max_length=400)


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
        "verbatim copy of the review's wording.\n\n"
        "IMPORTANT -- `claim_type` classifies what KIND of claim this is, never the TOPIC "
        "it happens to be about. Do not pick a type just because it shares a word with the "
        "topic:\n"
        "- `noise` means the fragment carries NO real signal about the product at all "
        "(greetings, off-topic chat, 'just arrived'). A genuine complaint that the "
        "product itself is loud (e.g. \"this kettle sounds like a jet taking off\") is a "
        "`problem` with aspect_raw like \"noise level\", NOT claim_type `noise` -- if a "
        "claim is worth extracting at all, it is by definition not `noise`.\n"
        "- `shipping_issue` means a NEGATIVE complaint about the delivery/packaging "
        "PROCESS (late, damaged in transit, wrong item shipped). A POSITIVE comment about "
        "packaging or arrival (e.g. \"arrived well packaged and on time\") is `praise` "
        "with aspect_raw \"packaging\", NOT `shipping_issue` -- `shipping_issue` should "
        "essentially never carry a positive `sentiment`.\n"
        "- Stance matters as much as topic. A claim that DISPUTES, DEBUNKS, or ARGUES "
        "AGAINST a problem someone else raised -- rather than the author reporting their "
        "own firsthand experience -- is `observation`, not `problem`, with `sentiment` "
        "reflecting the commenter's OWN stance (typically neutral or positive when "
        "defending the product), never the sentiment of the claim being disputed. This "
        "includes IMPLICIT disputes, not just explicit disagreement -- e.g. a short reply "
        "asserting that visible damage \"looks old\" or already existed is disputing that "
        "the product caused it, even without an explicit \"that's not from it\" statement. "
        "Do NOT reclassify a genuine firsthand complaint as `observation` just because it is "
        "phrased with a hedge, caveat, or skeptical-sounding wording -- e.g. \"unless "
        "you're rougher on it than most people, this shouldn't happen, but mine did\" is "
        "still a real `problem`; only use `observation` when the substance of the text is "
        "actually arguing against someone else's claim, not reporting the author's own "
        "experience. If a comment both disputes another claim AND separately reports the "
        "author's own genuine experience, extract both as separate claims -- the rebuttal "
        "portion as `observation`, the firsthand portion with whatever `claim_type` "
        "actually fits it.\n"
        "- In general, `sentiment` must agree with `claim_type`: `problem` / "
        "`shipping_issue` / `seller_service_issue` are virtually always negative or "
        "neutral, `praise` is virtually always positive. If you notice a mismatch, "
        "re-check whether you picked the right `claim_type`.\n\n"
        "IMPORTANT -- granularity. A claim is ATOMIC when it carries one independent, "
        "verifiable fact, not one clause of English grammar. Several sentences can still "
        "be only ONE claim if they all restate the same underlying point. Do NOT create a "
        "separate claim for each of these:\n"
        "- Repeated paraphrases of the same issue (\"battery drains fast\", \"doesn't "
        "last through the day\", \"only lasts a few hours\") -> ONE `problem` claim.\n"
        "- A problem plus the reviewer's stated fix for that SAME problem (\"the backlight "
        "is too bright\" + \"it should be dimmable\") -> ONE `problem` claim; put the ask "
        "in `explicit_request`, do not also emit a separate `feature_request` claim for it.\n"
        "- Generic closing praise (\"reliable, looks great, performs well, I'd recommend "
        "it\") -> ONE `praise` claim, unless a sentence names a genuinely new, separately "
        "verifiable attribute (e.g. \"and the handle is comfortable\" IS new -- keep that "
        "one separate).\n"
        "- A comparison repeated against several named competitors that all reach the "
        "SAME conclusion (\"sounds better than the X, the Y, and the Z\") -> ONE "
        "`comparison` claim naming all of them, not one claim per competitor.\n"
        "DO still split out genuinely distinct product aspects (fit vs. sound vs. battery "
        "vs. price are independent and should stay separate claims), and DO still split "
        "comparisons whose conclusions actually differ (\"beats the X, but the Y still "
        "sounds better\" -> two claims, since the verdict differs per competitor).\n\n"
        "For `source_excerpt`, copy a short span (<=200 characters) of the review's own "
        "wording, EXACT and VERBATIM (character-for-character, not paraphrased), that most "
        "directly supports this specific claim -- unlike `statement`, this must be text "
        "that literally appears in the review. Omit it if no single short span captures "
        "the claim well. Return only JSON."
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
                        "source_excerpt": "optional -- a short VERBATIM span (<=200 chars) copied exactly from the review body that supports this claim",
                    }
                ]
            },
        },
        ensure_ascii=False,
    )
    parsed = llm.json_chat(fast_model(), system, user)
    top_level = RawClaimResponse.model_validate(parsed)  # raises if "claims" is missing / not a list

    claims: list[Claim] = []
    seen_ids: set[str] = set()
    invalid_count = 0
    for raw in top_level.claims:
        if len(claims) >= _RAW_INGESTION_CEILING:
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
            source_excerpt=item.source_excerpt,
        )
        if candidate.claim_id in seen_ids:  # exact duplicate within this response -- collapse, don't double-count
            continue
        seen_ids.add(candidate.claim_id)
        claims.append(candidate)

    if not claims and top_level.claims:
        # Every raw claim in a non-empty list failed validation -- the response is
        # unusable, not "genuinely zero claims". Force the caller to fall back.
        raise ValueError(f"all {len(top_level.claims)} raw claim(s) failed validation")

    stats = ClaimExtractionStats(invalid_claims=invalid_count, raw_claims_extracted=len(claims))
    merged = _merge_within_review(claims, llm, stats)
    final = _apply_safety_cap(merged, max_claims_per_review(), stats)
    stats.llm_claims = len(final)
    stats.final_claims_saved = len(final)
    return final, stats


# ---------------------------------------------------------------------------
# Fallback path -- deterministic, reuses text.py's regex primitives.
# Deliberately crude: one low-confidence claim per matched aspect keyword, no
# multi-claim splitting or semantic reasoning. Must not be mistaken for the
# LLM path's quality -- extraction_method="fallback_rules" marks every claim
# produced here. No merge pass runs here: detect_aspects() already returns each
# aspect at most once (so fallback claims can never share an aspect_raw), and
# there's no LLM available in this path anyway to arbitrate ambiguous pairs.
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

    raw_claims = [
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
            source_excerpt=find_aspect_excerpt(text, aspect),
        )
        for aspect in aspects[:_RAW_INGESTION_CEILING]
    ]
    stats = ClaimExtractionStats(raw_claims_extracted=len(raw_claims))
    final = _apply_safety_cap(raw_claims, max_claims_per_review(), stats)
    stats.fallback_claims = len(final)
    stats.final_claims_saved = len(final)
    return final, stats


# ---------------------------------------------------------------------------
# Phase 1.6 -- within-review merge pass. Only ever called on Claims that
# already share one evidence_id (extract_claims() is always scoped to one
# Evidence), so this never compares claims across different reviews. Lexical
# only (text.py's simple_similarity, no embeddings/new dependency -- real
# semantic/embedding-based dedup is deliberately left to the already-planned,
# cross-review "semantic dedup" pipeline phase) except for a small, batched LLM
# check on genuinely ambiguous pairs.
# ---------------------------------------------------------------------------

# Lexical similarity (text.py's Jaccard-over-keywords) is used ONLY as a
# candidate filter here -- "is this pair even worth a closer look" -- never as
# standalone proof that two claims express the same underlying assertion.
# Genuine paraphrases routinely score low on raw word overlap (e.g. "lasts" vs
# "lasted"), and superficially similar claims routinely differ in meaningful
# context (different named competitors, "while gaming" vs "in freezing
# temperatures") that a bag-of-words score can't see. So the auto-merge tier is
# reserved for near-identical restatements only; everything else that looks
# even loosely related is deferred to the batched LLM verifier rather than
# decided by a threshold. Under-merging (leaving a redundant claim in place) is
# the accepted failure mode; over-merging (silently destroying a distinct
# signal) is not.
_OBVIOUS_DUPLICATE_SIM = 0.75
_DIFFERENT_ASPECT_CANDIDATE_SIM = 0.5
_PROBLEM_FEATURE_REQUEST_CANDIDATE_SIM = 0.15

PairDecision = Literal["merge", "ambiguous", "skip"]


def _pair_decision(a: Claim, b: Claim) -> PairDecision:
    """One row of the Phase 1.6 pairwise rule table. "merge" fires only for
    near-identical restatements; everything else that could plausibly be a
    duplicate is "ambiguous" (deferred to the LLM verifier), never merged on
    lexical grounds alone."""
    types = {a.claim_type, b.claim_type}
    same_aspect = _normalize(a.aspect_raw) == _normalize(b.aspect_raw)
    sim = simple_similarity(a.statement, b.statement)

    if types == {ClaimType.PROBLEM, ClaimType.FEATURE_REQUEST}:
        # Same aspect (or any lexical overlap) is NEVER enough on its own to
        # merge a problem with a feature_request -- "battery only lasts four
        # hours" + "I want a removable battery" share an aspect but are two
        # different signals; only "I wish the battery lasted longer" is a
        # direct resolution of the same problem, and telling those apart needs
        # real judgment, not a word-overlap score. Same-aspect/lexical overlap
        # only qualifies the pair as a CANDIDATE worth asking the LLM about --
        # it never merges without that confirmation.
        return "ambiguous" if (same_aspect or sim >= _PROBLEM_FEATURE_REQUEST_CANDIDATE_SIM) else "skip"

    if a.claim_type != b.claim_type:
        return "skip"

    # Materially different comparison conclusions must never merge, regardless
    # of how similar the wording is (e.g. "beats X" vs "loses to Y").
    if a.claim_type == ClaimType.COMPARISON and a.sentiment != b.sentiment:
        return "skip"

    if same_aspect:
        if sim >= _OBVIOUS_DUPLICATE_SIM:
            return "merge"
        # Different named competitors, different usage conditions ("during
        # gaming" vs "in cold weather"), etc. will land here too -- same
        # aspect, moderate similarity, but potentially materially different
        # context. Defer to the LLM rather than guess from the score alone.
        return "ambiguous" if a.sentiment == b.sentiment else "skip"

    return "ambiguous" if sim >= _DIFFERENT_ASPECT_CANDIDATE_SIM else "skip"


def _merge_within_review(claims: list[Claim], llm: DeepSeekClient, stats: ClaimExtractionStats) -> list[Claim]:
    n = len(claims)
    if n < 2:
        return claims

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    ambiguous_pairs: list[tuple[int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            decision = _pair_decision(claims[i], claims[j])
            if decision == "merge":
                union(i, j)
            elif decision == "ambiguous":
                ambiguous_pairs.append((i, j))

    # Skip asking about pairs already unioned via transitivity through a
    # confident merge elsewhere in the group.
    still_ambiguous = [(i, j) for i, j in ambiguous_pairs if find(i) != find(j)]
    if still_ambiguous and llm.available() and enable_claim_merge_llm_verification():
        verdicts = _verify_ambiguous_pairs_llm([(claims[i], claims[j]) for i, j in still_ambiguous], llm)
        for (i, j), same in zip(still_ambiguous, verdicts):
            if same:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    merged: list[Claim] = []
    for indices in groups.values():
        group = [claims[k] for k in indices]
        if len(group) == 1:
            merged.append(group[0])
            continue
        merged.append(_build_merged_claim(group))
        stats.claims_merged += 1
        stats.within_review_duplicates_removed += len(group) - 1

    return merged


def _build_merged_claim(group: list[Claim]) -> Claim:
    """Collapses a confirmed-duplicate group into one Claim. The surviving
    claim's identity-bearing fields (claim_type/aspect_raw/statement, hence its
    claim_id) always come verbatim from the chosen primary -- nothing here ever
    synthesizes new claim text, which would risk fabricating a claim nobody
    actually made."""
    has_problem = any(c.claim_type == ClaimType.PROBLEM for c in group)
    has_feature_request = any(c.claim_type == ClaimType.FEATURE_REQUEST for c in group)
    mixed_problem_group = has_problem and has_feature_request

    def primary_key(c: Claim) -> tuple[int, float, str]:
        type_rank = 0 if (mixed_problem_group and c.claim_type == ClaimType.PROBLEM) else 1
        return (type_rank, -c.confidence, c.claim_id)

    primary = min(group, key=primary_key)
    others = [c for c in group if c is not primary]

    explicit_request = primary.explicit_request
    if not explicit_request:
        for c in others:
            if c.claim_type == ClaimType.FEATURE_REQUEST:
                explicit_request = c.explicit_request or c.statement
                break
        else:
            for c in others:
                if c.explicit_request:
                    explicit_request = c.explicit_request
                    break

    merged_excerpts: list[str] = []
    seen = {_normalize(primary.source_excerpt)} if primary.source_excerpt else set()
    for c in others:
        if c.source_excerpt and _normalize(c.source_excerpt) not in seen:
            merged_excerpts.append(c.source_excerpt)
            seen.add(_normalize(c.source_excerpt))

    severities = [c.severity for c in group if c.severity is not None]

    return Claim(
        claim_id=primary.claim_id,
        run_id=primary.run_id,
        evidence_id=primary.evidence_id,
        claim_type=primary.claim_type,
        aspect_raw=primary.aspect_raw,
        statement=primary.statement,
        sentiment=primary.sentiment,
        confidence=max(c.confidence for c in group),
        extraction_method=primary.extraction_method,
        created_at=primary.created_at,
        subject=primary.subject,
        explicit_request=explicit_request,
        severity=max(severities) if severities else None,
        source_excerpt=primary.source_excerpt,
        merge_count=len(group),
        merged_claim_ids=[c.claim_id for c in others] or None,
        merged_excerpts=merged_excerpts or None,
        canonical_category=primary.canonical_category,
    )


# ---------------------------------------------------------------------------
# Tier 4 -- batched LLM verification for genuinely ambiguous pairs only.
# ---------------------------------------------------------------------------


class _PairVerdict(BaseModel):
    pair_index: int
    same_claim: bool


class _AmbiguityResponse(BaseModel):
    verdicts: list[dict]


def _verify_ambiguous_pairs_llm(pairs: list[tuple[Claim, Claim]], llm: DeepSeekClient) -> list[bool]:
    """Returns one bool per pair (True = same underlying claim, should merge).
    Fails CLOSED (all False = "not merged") on any error, timeout, or malformed
    response -- an unconfirmed merge silently destroying a real, distinct
    signal is worse than a redundant claim surviving for a human (or a later
    phase) to notice. One batched call for the whole review, never one call
    per pair, so cost stays bounded regardless of how many ambiguous pairs a
    review has."""
    if not pairs:
        return []
    system = (
        "You are checking whether pairs of atomic claims extracted from the SAME "
        "customer review actually describe the same underlying point, or are "
        "genuinely distinct. Default to DIFFERENT when uncertain -- a missed "
        "duplicate is a minor cosmetic issue, but wrongly merging two distinct "
        "claims silently destroys a real customer signal, so only say the same "
        "claim when you are confident.\n\n"
        "Two claims are the SAME point only if a reader would find them pure "
        "redundant restatements of one fact -- same complaint, same praise, or "
        "the same comparison conclusion, just worded differently, with no added "
        "specifics. They are DIFFERENT if either one adds a distinguishing fact "
        "the other lacks -- including a different named competitor/product, a "
        "different specific condition or trigger (e.g. \"while gaming\" vs \"in "
        "freezing temperatures\", even though both are about the same aspect and "
        "read as similar sentences), or a different concrete ask. A problem and "
        "a feature_request are the SAME point only when the request is clearly "
        "and directly the fix for that exact problem (\"battery only lasts four "
        "hours\" + \"I wish the battery lasted longer\" -- same point); if the "
        "request describes a different kind of fix or an unrelated want "
        "(\"battery only lasts four hours\" + \"I want a removable battery\"), "
        "they are DIFFERENT. Answer every pair. Return only JSON."
    )
    user = json.dumps(
        {
            "pairs": [{"pair_index": i, "claim_a": a.statement, "claim_b": b.statement} for i, (a, b) in enumerate(pairs)],
            "expected_json": {"verdicts": [{"pair_index": 0, "same_claim": True}]},
        },
        ensure_ascii=False,
    )
    try:
        parsed = llm.json_chat(fast_model(), system, user)
        top = _AmbiguityResponse.model_validate(parsed)
        verdict_by_index: dict[int, bool] = {}
        for raw in top.verdicts:
            v = _PairVerdict.model_validate(raw)
            verdict_by_index[v.pair_index] = v.same_claim
        return [verdict_by_index.get(i, False) for i in range(len(pairs))]
    except Exception:
        return [False] * len(pairs)


def _apply_safety_cap(claims: list[Claim], limit: int, stats: ClaimExtractionStats) -> list[Claim]:
    """The safety valve of last resort -- only reached after merging, so it
    should rarely trigger. When it does, keeps the highest-confidence claims
    (deterministic, content-derived ordering) rather than "whatever the model
    emitted first", which used to be the only granularity control."""
    if len(claims) <= limit:
        return claims
    stats.safety_cap_truncations += len(claims) - limit
    ordered = sorted(claims, key=lambda c: (-c.confidence, c.claim_id))
    return ordered[:limit]


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


def _verified_excerpt(evidence: Evidence, source_excerpt: str | None) -> str | None:
    """Only trust an excerpt as "verbatim" if it's actually a substring of the
    source text -- an LLM claiming a quote is verbatim doesn't make it so. Falls
    back to None (never a fabricated quote) rather than the unverified text, so
    callers can safely present whatever survives here as genuinely traceable."""
    if not source_excerpt:
        return None
    haystack = _normalize(f"{evidence.title}\n{evidence.body}")
    if _normalize(source_excerpt) not in haystack:
        return None
    return source_excerpt.strip()


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
    source_excerpt: str | None = None,
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
        source_excerpt=_verified_excerpt(evidence, source_excerpt),
    )
