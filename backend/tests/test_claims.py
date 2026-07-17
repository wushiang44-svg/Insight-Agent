from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app import routes as routes_module
from app.llm import DeepSeekClient
from app.models import Evidence, InsightType, Sentiment
from app.pipeline.claims import ClaimExtractionStats, extract_claims
from app.storage import Storage


def make_evidence(
    evidence_id: str = "ev_1",
    run_id: str = "run_1",
    title: str = "Review title",
    body: str = "Battery life is terrible, dies so fast.",
    source_url: str = "https://reddit.com/x",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        run_id=run_id,
        iteration=1,
        source_url=source_url,
        subreddit="gadgets",
        item_type="post",
        title=title,
        body=body,
        score=5,
        comment_count=0,
        created_at="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:00+00:00",
        search_query="battery",
        insight_type=InsightType.PAIN_POINT,
        aspect="battery",
        sentiment=Sentiment.NEGATIVE,
        quote="Battery life is terrible, dies so fast.",
        confidence=0.6,
    )


def no_llm() -> DeepSeekClient:
    return DeepSeekClient(api_key="")


class FakeLLM:
    """Duck-types the DeepSeekClient interface used by pipeline/claims.py."""

    def __init__(self, response: Any = None, available: bool = True, raise_exc: Exception | None = None):
        self._response = response
        self._available = available
        self._raise_exc = raise_exc
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def json_chat(self, model: str, system: str, user: str) -> Any:
        self.calls += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


class SequencedLLM:
    """Like FakeLLM but returns a different canned response per call, in order.
    Needed for Phase 1.6 tests: the first call is claim extraction, the second
    (only when there are ambiguous pairs) is the batched merge-verification
    call, and the two need different response shapes."""

    def __init__(self, responses: list[Any], available: bool = True):
        self._responses = list(responses)
        self._available = available
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def json_chat(self, model: str, system: str, user: str) -> Any:
        response = self._responses[self.calls] if self.calls < len(self._responses) else self._responses[-1]
        self.calls += 1
        return response


BATTERY_CASE_SOUND_REVIEW = (
    "Battery is great at home, but during winter commuting it drops quickly. "
    "I wish the case were smaller, although the sound quality is excellent."
)

BATTERY_CASE_SOUND_CLAIMS_RESPONSE = {
    "claims": [
        {
            "claim_type": "problem",
            "aspect_raw": "battery life",
            "statement": "Battery drains quickly during winter commuting",
            "sentiment": "negative",
            "severity": 0.75,
        },
        {
            "claim_type": "feature_request",
            "aspect_raw": "charging case size",
            "statement": "The user wants a smaller charging case",
            "sentiment": "negative",
        },
        {
            "claim_type": "praise",
            "aspect_raw": "sound quality",
            "statement": "Sound quality is excellent",
            "sentiment": "positive",
        },
    ]
}


# ---------------------------------------------------------------------------
# Zero / single / multi claim
# ---------------------------------------------------------------------------


def test_zero_claim_review_fallback_path() -> None:
    evidence = make_evidence(body="I bought this last month and haven't used it yet.")  # no ASPECT_PATTERNS match
    result = extract_claims("wireless earbuds", evidence, no_llm())

    assert result.succeeded is True
    assert result.claims == []
    assert result.stats == ClaimExtractionStats(fallback_claims=0)


def test_zero_claim_review_llm_path() -> None:
    evidence = make_evidence()
    llm = FakeLLM(response={"claims": []})
    result = extract_claims("wireless earbuds", evidence, llm)

    assert result.succeeded is True
    assert result.claims == []
    assert result.stats.llm_claims == 0


def test_multi_claim_review_llm_path() -> None:
    evidence = make_evidence(body=BATTERY_CASE_SOUND_REVIEW)
    llm = FakeLLM(response=BATTERY_CASE_SOUND_CLAIMS_RESPONSE)
    result = extract_claims("wireless earbuds", evidence, llm)

    assert result.succeeded is True
    assert len(result.claims) == 3
    assert {claim.evidence_id for claim in result.claims} == {evidence.evidence_id}
    assert {claim.claim_type.value for claim in result.claims} == {"problem", "feature_request", "praise"}
    assert result.stats.llm_claims == 3
    # Phase 1.6 regression: three genuinely independent aspects (battery,
    # charging case size, sound) must never be merged into each other.
    assert all(claim.merge_count == 1 for claim in result.claims)
    assert result.stats.claims_merged == 0


def test_single_claim_review_llm_path() -> None:
    evidence = make_evidence()
    llm = FakeLLM(response={"claims": [BATTERY_CASE_SOUND_CLAIMS_RESPONSE["claims"][0]]})
    result = extract_claims("wireless earbuds", evidence, llm)

    assert result.succeeded is True
    assert len(result.claims) == 1
    assert result.claims[0].claim_type.value == "problem"


# ---------------------------------------------------------------------------
# Malformed / invalid LLM output
# ---------------------------------------------------------------------------


def test_malformed_top_level_response_falls_back_without_raising() -> None:
    evidence = make_evidence()  # has a "battery" aspect match, so fallback finds something
    llm = FakeLLM(response={"not_claims_at_all": True})
    result = extract_claims("wireless earbuds", evidence, llm)

    assert result.succeeded is True
    assert result.stats.llm_claims == 0
    assert result.stats.fallback_claims >= 1  # fell all the way through to the deterministic path


def test_one_invalid_claim_among_valid_ones_is_skipped_not_the_whole_batch() -> None:
    evidence = make_evidence()
    response = {
        "claims": [
            {"claim_type": "problem", "aspect_raw": "battery", "statement": "Battery is bad", "sentiment": "negative"},
            {"claim_type": "not_a_real_type", "aspect_raw": "x", "statement": "y", "sentiment": "negative"},
            {"claim_type": "praise", "aspect_raw": "sound", "statement": "Sound is great", "sentiment": "positive"},
        ]
    }
    llm = FakeLLM(response=response)
    result = extract_claims("wireless earbuds", evidence, llm)

    assert result.succeeded is True
    assert len(result.claims) == 2  # only the malformed middle entry was dropped
    assert result.stats.llm_claims == 2
    assert result.stats.invalid_claims == 1


def test_all_invalid_claims_falls_back() -> None:
    evidence = make_evidence()
    response = {"claims": [{"claim_type": "bogus", "aspect_raw": "x", "statement": "y", "sentiment": "negative"}]}
    llm = FakeLLM(response=response)
    result = extract_claims("wireless earbuds", evidence, llm)

    assert result.succeeded is True  # recovered via fallback
    assert result.stats.llm_claims == 0
    assert result.stats.fallback_claims >= 1


def test_duplicate_claims_in_one_response_collapse_to_one_id() -> None:
    evidence = make_evidence()
    claim = BATTERY_CASE_SOUND_CLAIMS_RESPONSE["claims"][0]
    llm = FakeLLM(response={"claims": [claim, dict(claim)]})  # exact duplicate
    result = extract_claims("wireless earbuds", evidence, llm)

    assert len(result.claims) == 1


# ---------------------------------------------------------------------------
# Traceability / non-LLM fallback quality signals
# ---------------------------------------------------------------------------


def test_source_traceability() -> None:
    evidence = make_evidence(evidence_id="ev_42", source_url="https://reddit.com/specific-post")
    llm = FakeLLM(response=BATTERY_CASE_SOUND_CLAIMS_RESPONSE)
    result = extract_claims("wireless earbuds", evidence, llm)

    assert all(claim.evidence_id == "ev_42" for claim in result.claims)
    # the claim itself never carries the raw source text/url -- the caller must join
    # back through evidence_id to Evidence.source_url, which is exactly what
    # routes.get_claims does (see test_get_claims_endpoint_enriches_with_original_text).


def test_non_llm_fallback_behavior_is_marked_and_capped() -> None:
    evidence = make_evidence(body="Battery life is terrible, dies so fast.")
    result = extract_claims("wireless earbuds", evidence, no_llm())

    assert result.succeeded is True
    assert len(result.claims) >= 1
    assert all(claim.extraction_method == "fallback_rules" for claim in result.claims)
    assert all(claim.confidence <= 0.5 for claim in result.claims)
    assert all(claim.severity is None for claim in result.claims)  # fallback never claims severity judgment


def test_fallback_excerpt_is_the_actual_matching_sentence() -> None:
    """Regression for the Phase 1 validation finding that the shown 'original excerpt'
    often had nothing to do with the claim: the fallback path's excerpt should be a
    real sentence containing the matched aspect keyword, not a generic label."""
    evidence = make_evidence(
        body="I bought this last week. Battery life is terrible, dies so fast. Shipping was fine though."
    )
    result = extract_claims("wireless earbuds", evidence, no_llm())

    battery_claim = next(c for c in result.claims if c.aspect_raw == "battery")
    assert battery_claim.source_excerpt == "Battery life is terrible, dies so fast."


def test_llm_source_excerpt_verified_against_evidence_body() -> None:
    """A genuine verbatim excerpt (a real substring of the review) is trusted and
    stored on the Claim, distinct from Evidence.quote."""
    evidence = make_evidence(
        body=BATTERY_CASE_SOUND_REVIEW,
        source_url="https://reddit.com/x",
    )
    response = {
        "claims": [
            {
                **BATTERY_CASE_SOUND_CLAIMS_RESPONSE["claims"][0],
                "source_excerpt": "it drops quickly",  # real substring of the review body
            }
        ]
    }
    llm = FakeLLM(response=response)
    result = extract_claims("wireless earbuds", evidence, llm)

    assert result.claims[0].source_excerpt == "it drops quickly"


def test_llm_hallucinated_source_excerpt_is_dropped_not_trusted() -> None:
    """An LLM claiming a 'verbatim' quote that isn't actually in the source text must
    never be stored as if it were traceable -- fabricating a fake verbatim quote is
    worse than having none."""
    evidence = make_evidence(body=BATTERY_CASE_SOUND_REVIEW)
    response = {
        "claims": [
            {
                **BATTERY_CASE_SOUND_CLAIMS_RESPONSE["claims"][0],
                "source_excerpt": "this sentence was never in the review at all",
            }
        ]
    }
    llm = FakeLLM(response=response)
    result = extract_claims("wireless earbuds", evidence, llm)

    assert result.claims[0].source_excerpt is None


# ---------------------------------------------------------------------------
# Idempotency and safe-replace semantics
# ---------------------------------------------------------------------------


def test_rerun_idempotency_leaves_only_the_latest_set(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    evidence = make_evidence()

    first = extract_claims("wireless earbuds", evidence, FakeLLM(response=BATTERY_CASE_SOUND_CLAIMS_RESPONSE))
    assert first.succeeded
    storage.replace_claims_for_evidence(evidence.evidence_id, first.claims)
    assert len(storage.list_claims_for_evidence(evidence.evidence_id)) == 3

    # Simulate a retry where the LLM returns a smaller, reordered set.
    second_response = {"claims": [BATTERY_CASE_SOUND_CLAIMS_RESPONSE["claims"][2]]}
    second = extract_claims("wireless earbuds", evidence, FakeLLM(response=second_response))
    assert second.succeeded
    storage.replace_claims_for_evidence(evidence.evidence_id, second.claims)

    stored = storage.list_claims_for_evidence(evidence.evidence_id)
    assert len(stored) == 1  # stale claims from the first attempt are gone, not unioned
    assert stored[0].claim_type.value == "praise"


def test_fallback_failure_preserves_previously_stored_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    evidence = make_evidence(body="Battery life is terrible, dies so fast.")

    seeded = extract_claims("wireless earbuds", evidence, no_llm())
    assert seeded.succeeded and seeded.claims
    storage.replace_claims_for_evidence(evidence.evidence_id, seeded.claims)
    before = storage.list_claims_for_evidence(evidence.evidence_id)
    assert before

    def _raise(_evidence: Evidence) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.pipeline.claims._extract_claims_fallback", _raise)
    failed = extract_claims("wireless earbuds", evidence, no_llm())
    assert failed.succeeded is False
    assert failed.stats.extraction_failures == 1

    # The caller (react_agent.py) must not call replace_claims_for_evidence on a
    # failed result -- simulate that contract here and confirm nothing changed.
    if failed.succeeded:
        storage.replace_claims_for_evidence(evidence.evidence_id, failed.claims)  # pragma: no cover - must not run

    after = storage.list_claims_for_evidence(evidence.evidence_id)
    assert [c.claim_id for c in after] == [c.claim_id for c in before]


# ---------------------------------------------------------------------------
# Phase 1.6 -- within-review Claim granularity / deduplication
# ---------------------------------------------------------------------------

REPEATED_BATTERY_COMPLAINTS_BODY = (
    "Battery only lasts four hours. The battery does not last through the whole day. "
    "Battery life drains within a few hours of use. The battery drains way too fast for daily use."
)

REPEATED_BATTERY_COMPLAINTS_RESPONSE = {
    "claims": [
        {
            "claim_type": "problem",
            "aspect_raw": "battery life",
            "statement": "Battery only lasts four hours.",
            "sentiment": "negative",
            "confidence": 0.9,
            "source_excerpt": "Battery only lasts four hours.",
        },
        {
            "claim_type": "problem",
            "aspect_raw": "battery life",
            "statement": "The battery does not last through the whole day.",
            "sentiment": "negative",
            "confidence": 0.85,
            "source_excerpt": "does not last through the whole day",
        },
        {
            "claim_type": "problem",
            "aspect_raw": "battery life",
            "statement": "Battery life drains within a few hours of use.",
            "sentiment": "negative",
            "confidence": 0.8,
            "source_excerpt": "drains within a few hours of use",
        },
        {
            "claim_type": "problem",
            "aspect_raw": "battery life",
            "statement": "The battery drains way too fast for daily use.",
            "sentiment": "negative",
            "confidence": 0.7,
            "source_excerpt": "drains way too fast for daily use",
        },
    ]
}

# still_ambiguous enumerates pairs in (i, j) order for i in range(n) for j in range(i+1, n);
# for n=4 that's exactly 6 pairs, all confirmed "same claim" here.
ALL_SAME_VERDICT_RESPONSE = {"verdicts": [{"pair_index": i, "same_claim": True} for i in range(6)]}


def test_repeated_battery_complaints_collapse_via_llm_verification() -> None:
    """Same broad aspect, low raw word overlap ("lasts" vs "last" vs "drains") --
    exactly the case lexical similarity alone can't safely resolve, so this must
    go through (and be confirmed by) the batched LLM verifier, not a threshold."""
    evidence = make_evidence(body=REPEATED_BATTERY_COMPLAINTS_BODY)
    llm = SequencedLLM([REPEATED_BATTERY_COMPLAINTS_RESPONSE, ALL_SAME_VERDICT_RESPONSE])
    result = extract_claims("wireless earbuds", evidence, llm)

    assert len(result.claims) == 1
    survivor = result.claims[0]
    assert survivor.merge_count == 4
    assert survivor.confidence == 0.9  # highest-confidence raw claim wins as primary
    assert survivor.statement == "Battery only lasts four hours."
    assert len(survivor.merged_claim_ids or []) == 3
    assert result.stats.claims_merged == 1
    assert result.stats.within_review_duplicates_removed == 3
    # every absorbed claim's excerpt must still independently verify as real
    # source text, not just the survivor's own.
    haystack = " ".join(evidence.body.split()).lower()
    for excerpt in survivor.merged_excerpts or []:
        assert excerpt.lower() in haystack


def test_ambiguous_pairs_use_one_batched_call_not_one_per_pair() -> None:
    evidence = make_evidence(body=REPEATED_BATTERY_COMPLAINTS_BODY)
    llm = SequencedLLM([REPEATED_BATTERY_COMPLAINTS_RESPONSE, ALL_SAME_VERDICT_RESPONSE])
    extract_claims("wireless earbuds", evidence, llm)

    assert llm.calls == 2  # 1 extraction call + exactly 1 batched verification call, never 6


def test_malformed_verification_response_fails_closed_keeps_claims_separate() -> None:
    evidence = make_evidence(body=REPEATED_BATTERY_COMPLAINTS_BODY)
    llm = SequencedLLM([REPEATED_BATTERY_COMPLAINTS_RESPONSE, {"not_verdicts_at_all": True}])
    result = extract_claims("wireless earbuds", evidence, llm)

    assert len(result.claims) == 4  # verification response was unusable -> nothing merges
    assert result.stats.claims_merged == 0


PROBLEM_FEATURE_REQUEST_SAME_ISSUE_RESPONSE = {
    "claims": [
        {
            "claim_type": "problem",
            "aspect_raw": "battery life",
            "statement": "Battery only lasts four hours.",
            "sentiment": "negative",
            "confidence": 0.9,
        },
        {
            "claim_type": "feature_request",
            "aspect_raw": "battery life",
            "statement": "I wish the battery lasted longer.",
            "sentiment": "neutral",
            "confidence": 0.7,
        },
    ]
}


def test_problem_and_direct_resolution_feature_request_merge_and_populate_explicit_request() -> None:
    evidence = make_evidence()
    llm = SequencedLLM(
        [PROBLEM_FEATURE_REQUEST_SAME_ISSUE_RESPONSE, {"verdicts": [{"pair_index": 0, "same_claim": True}]}]
    )
    result = extract_claims("wireless earbuds", evidence, llm)

    assert len(result.claims) == 1
    survivor = result.claims[0]
    assert survivor.claim_type.value == "problem"  # the problem, not the request, is the surviving type
    assert survivor.merge_count == 2
    assert survivor.explicit_request == "I wish the battery lasted longer."


PROBLEM_UNRELATED_FEATURE_REQUEST_RESPONSE = {
    "claims": [
        {
            "claim_type": "problem",
            "aspect_raw": "battery life",
            "statement": "Battery only lasts four hours.",
            "sentiment": "negative",
            "confidence": 0.9,
        },
        {
            "claim_type": "feature_request",
            "aspect_raw": "battery design",
            "statement": "I want a removable battery.",
            "sentiment": "neutral",
            "confidence": 0.7,
        },
    ]
}


def test_problem_and_unrelated_feature_request_never_auto_merge() -> None:
    """Same broad topic (battery) but a materially different ask -- must never
    merge on aspect/lexical grounds alone; the LLM verifier is asked and must
    say "different", matching the corrected Phase 1.6 spec's own example."""
    evidence = make_evidence()
    llm = SequencedLLM(
        [PROBLEM_UNRELATED_FEATURE_REQUEST_RESPONSE, {"verdicts": [{"pair_index": 0, "same_claim": False}]}]
    )
    result = extract_claims("wireless earbuds", evidence, llm)

    assert len(result.claims) == 2
    assert llm.calls == 2  # the pair was a candidate worth asking about, even though the answer is "no"


REPEATED_GENERIC_PRAISE_RESPONSE = {
    "claims": [
        {
            "claim_type": "praise",
            "aspect_raw": "reliability",
            "statement": "The kettle is very reliable and works great every time.",
            "sentiment": "positive",
            "confidence": 0.9,
        },
        {
            "claim_type": "praise",
            "aspect_raw": "reliability",
            "statement": "This kettle is very reliable and works great every time.",
            "sentiment": "positive",
            "confidence": 0.85,
        },
        {
            "claim_type": "praise",
            "aspect_raw": "reliability",
            "statement": "The kettle has been very reliable and works great every time.",
            "sentiment": "positive",
            "confidence": 0.8,
        },
    ]
}


def test_repeated_generic_praise_auto_merges_without_llm_call() -> None:
    """Near-identical restatements are the one case the plan allows to merge on
    lexical grounds alone -- no verification call should be spent on something
    this obvious."""
    evidence = make_evidence()
    llm = FakeLLM(response=REPEATED_GENERIC_PRAISE_RESPONSE)
    result = extract_claims("wireless earbuds", evidence, llm)

    assert len(result.claims) == 1
    assert result.claims[0].merge_count == 3
    assert llm.calls == 1


SAME_CONCLUSION_COMPARISON_RESPONSE = {
    "claims": [
        {
            "claim_type": "comparison",
            "aspect_raw": "sound quality",
            "statement": "These earbuds sound better than the Soundpeats TrueAir 2.",
            "sentiment": "positive",
            "confidence": 0.9,
        },
        {
            "claim_type": "comparison",
            "aspect_raw": "sound quality",
            "statement": "These earbuds sound better than the Lavabean earbuds.",
            "sentiment": "positive",
            "confidence": 0.85,
        },
    ]
}


def test_same_conclusion_competitor_comparisons_merge_via_llm_verification() -> None:
    """Two named competitors, same direction of conclusion -- moderate lexical
    similarity (different competitor names pull the score down), so this must
    go through the LLM verifier rather than auto-merge on the score alone."""
    evidence = make_evidence()
    llm = SequencedLLM(
        [SAME_CONCLUSION_COMPARISON_RESPONSE, {"verdicts": [{"pair_index": 0, "same_claim": True}]}]
    )
    result = extract_claims("wireless earbuds", evidence, llm)

    assert len(result.claims) == 1
    assert result.claims[0].merge_count == 2


DIFFERENT_CONCLUSION_COMPARISON_RESPONSE = {
    "claims": [
        {
            "claim_type": "comparison",
            "aspect_raw": "sound quality",
            "statement": "These earbuds sound better than the Soundpeats TrueAir 2.",
            "sentiment": "positive",
            "confidence": 0.9,
        },
        {
            "claim_type": "comparison",
            "aspect_raw": "sound quality",
            "statement": "The TOZO NC2 still sounds better than these earbuds.",
            "sentiment": "negative",
            "confidence": 0.85,
        },
    ]
}


def test_different_conclusion_competitor_comparisons_never_merge_no_llm_call() -> None:
    """A materially different conclusion (loses to one competitor, beats another)
    must never merge, and the sentiment mismatch alone is enough to know that --
    no verification call should be spent confirming the obvious."""
    evidence = make_evidence()
    llm = FakeLLM(response=DIFFERENT_CONCLUSION_COMPARISON_RESPONSE)
    result = extract_claims("wireless earbuds", evidence, llm)

    assert len(result.claims) == 2
    assert llm.calls == 1


_DISTINCT_ASPECT_STATEMENTS = [
    ("battery life", "Battery easily lasts a full two-day trip without needing a recharge."),
    ("sound quality", "Vocals sound crisp and the bass has real punch without distortion."),
    ("comfort", "Even after four hours of wear the tips never irritate my ears."),
    ("build quality", "The hinge feels solid and does not wobble like my old pair did."),
    ("price", "At this price point nothing else on the market comes close."),
    ("packaging", "Arrived in a sturdy box with foam inserts protecting every piece."),
    ("touch controls", "Double tapping to skip tracks works reliably every single time."),
    ("charging case", "The case snaps shut magnetically and fits easily in a pocket."),
    ("microphone quality", "Callers say my voice comes through clearly even on windy days."),
    ("bluetooth range", "I can walk two rooms away and the connection never drops."),
]
COMPLEX_REVIEW_RESPONSE = {
    "claims": [
        {"claim_type": "praise", "aspect_raw": aspect, "statement": statement, "sentiment": "positive", "confidence": 0.9}
        for aspect, statement in _DISTINCT_ASPECT_STATEMENTS
    ]
}


def test_complex_review_with_ten_independent_claims_survives_uncapped() -> None:
    evidence = make_evidence()
    llm = FakeLLM(response=COMPLEX_REVIEW_RESPONSE)
    result = extract_claims("wireless earbuds", evidence, llm)

    assert len(result.claims) == 10  # well under the new cap of 20 -- nothing merged or truncated
    assert result.stats.claims_merged == 0
    assert result.stats.safety_cap_truncations == 0


def test_merge_idempotency_and_traceability(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    evidence = make_evidence(body=REPEATED_BATTERY_COMPLAINTS_BODY)

    def run_once() -> Any:
        llm = SequencedLLM([REPEATED_BATTERY_COMPLAINTS_RESPONSE, ALL_SAME_VERDICT_RESPONSE])
        return extract_claims("wireless earbuds", evidence, llm)

    first = run_once()
    assert first.succeeded
    storage.replace_claims_for_evidence(evidence.evidence_id, first.claims)
    first_ids = {c.claim_id for c in storage.list_claims_for_evidence(evidence.evidence_id)}

    second = run_once()
    assert second.succeeded
    storage.replace_claims_for_evidence(evidence.evidence_id, second.claims)
    second_ids = {c.claim_id for c in storage.list_claims_for_evidence(evidence.evidence_id)}

    assert first_ids == second_ids  # deterministic primary selection -- reruns collapse onto the same id

    stored = storage.list_claims_for_evidence(evidence.evidence_id)
    assert len(stored) == 1
    survivor = stored[0]
    assert survivor.merge_count == 4
    haystack = " ".join(evidence.body.split()).lower()
    if survivor.source_excerpt:
        assert survivor.source_excerpt.lower() in haystack
    for excerpt in survivor.merged_excerpts or []:
        assert excerpt.lower() in haystack


def _pathological_distinct_claim(i: int) -> dict:
    return {
        "claim_type": "observation",
        "aspect_raw": f"quirk-{i}",
        "statement": f"Micro observation {i}: a small unrelated detail was mentioned once.",
        "sentiment": "neutral",
        "confidence": round(0.5 + (i % 10) * 0.01, 2),
    }


PATHOLOGICAL_RESPONSE = {"claims": [_pathological_distinct_claim(i) for i in range(25)]}


def test_safety_cap_still_truncates_after_merge_when_genuinely_excessive() -> None:
    """25 raw claims that the (stubbed) verifier confirms are all genuinely
    distinct -- merging can't reduce them, so the safety cap must still do its
    job as the last-resort backstop."""
    evidence = make_evidence()
    llm = SequencedLLM([PATHOLOGICAL_RESPONSE, {"verdicts": []}])  # empty verdicts -> every ambiguous pair fails closed
    result = extract_claims("wireless earbuds", evidence, llm)

    assert result.stats.raw_claims_extracted == 25
    assert len(result.claims) == 20  # new default MAX_CLAIMS_PER_REVIEW
    assert result.stats.safety_cap_truncations == 5


# ---------------------------------------------------------------------------
# Mixed shipping + real-product-signal review -- Phase 1 documented this as a
# known gap in analyze_item()'s binary relevance gate; Phase 2 (pipeline/screening.py,
# see tests/test_screening.py) replaced that gate with screen_item(), which never
# lets a category judgment suppress extract_claims(). analyze_item() itself has
# been retired -- these tests now exercise extract_claims() directly against a
# mixed-content Evidence, which is the part of this codebase Phase 1 actually owns.
# ---------------------------------------------------------------------------

MIXED_SHIPPING_AND_PRODUCT_BODY = (
    "Shipping took forever and the box arrived crushed. Aside from that, "
    "the battery life is terrible and dies within an hour."
)


def test_mixed_review_still_surfaces_the_product_claim() -> None:
    """A review that's mostly a shipping complaint but also contains real
    product content must still yield the product Claim once it reaches
    extract_claims() -- regardless of what upstream screening decided about
    the review's other categories (see test_screening.py for the screening
    layer's own coverage of this)."""
    evidence = make_evidence(body=MIXED_SHIPPING_AND_PRODUCT_BODY)
    result = extract_claims("wireless earbuds", evidence, no_llm())
    aspects_found = {claim.aspect_raw for claim in result.claims}
    assert "battery" in aspects_found


# ---------------------------------------------------------------------------
# API enrichment: GET /runs/{run_id}/claims
# ---------------------------------------------------------------------------


def test_get_claims_endpoint_enriches_with_original_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "routes_test.sqlite3"
    monkeypatch.setattr(routes_module, "DEFAULT_DB_PATH", db_path)

    storage = Storage(db_path)
    storage.migrate()
    run = storage.create_run("wireless earbuds", [], [], 6, 25)
    evidence = make_evidence(evidence_id="ev_route", run_id=run.run_id, source_url="https://reddit.com/route-test")
    storage.save_evidence(evidence)
    result = extract_claims("wireless earbuds", evidence, FakeLLM(response=BATTERY_CASE_SOUND_CLAIMS_RESPONSE))
    storage.replace_claims_for_evidence(evidence.evidence_id, result.claims)
    storage.close()

    response = routes_module.get_claims(run.run_id)

    assert len(response) == 3
    for claim in response:
        assert claim["original_source_url"] == "https://reddit.com/route-test"
        assert claim["original_excerpt"] == evidence.quote
        assert claim["statement"] != claim["original_excerpt"]  # never presented as the same thing


def test_get_claims_endpoint_prefers_claim_level_excerpt_over_evidence_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of per-claim source_excerpt: once a claim has its own verified
    excerpt, the API must surface THAT, not the review-wide Evidence.quote (which may
    describe a completely different part of the review)."""
    db_path = tmp_path / "routes_test3.sqlite3"
    monkeypatch.setattr(routes_module, "DEFAULT_DB_PATH", db_path)

    storage = Storage(db_path)
    storage.migrate()
    run = storage.create_run("wireless earbuds", [], [], 6, 25)
    evidence = make_evidence(
        evidence_id="ev_route2",
        run_id=run.run_id,
        body=BATTERY_CASE_SOUND_REVIEW,
    )  # note: make_evidence's default quote is unrelated to the body override above,
    # which is exactly the traceability gap this test guards against
    storage.save_evidence(evidence)
    response_payload = {
        "claims": [{**BATTERY_CASE_SOUND_CLAIMS_RESPONSE["claims"][2], "source_excerpt": "sound quality is excellent"}]
    }
    result = extract_claims("wireless earbuds", evidence, FakeLLM(response=response_payload))
    storage.replace_claims_for_evidence(evidence.evidence_id, result.claims)
    storage.close()

    response = routes_module.get_claims(run.run_id)

    assert len(response) == 1
    assert response[0]["original_excerpt"] == "sound quality is excellent"
    assert response[0]["original_excerpt"] != evidence.quote


def test_get_claims_endpoint_returns_empty_list_for_pre_phase_1_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "routes_test2.sqlite3"
    monkeypatch.setattr(routes_module, "DEFAULT_DB_PATH", db_path)

    storage = Storage(db_path)
    storage.migrate()
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.close()

    assert routes_module.get_claims(run.run_id) == []
