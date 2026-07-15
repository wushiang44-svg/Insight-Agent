from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app import routes as routes_module
from app.llm import DeepSeekClient
from app.models import Evidence, InsightType, Sentiment
from app.pipeline.claims import ClaimExtractionStats, extract_claims
from app.react_agent import analyze_item
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
# Mixed shipping + real-product-signal review (documents the known Phase 1 gap
# in analyze_item's binary relevance gate -- Phase 2 screening is the real fix)
# ---------------------------------------------------------------------------

MIXED_SHIPPING_AND_PRODUCT_BODY = (
    "Shipping took forever and the box arrived crushed. Aside from that, "
    "the battery life is terrible and dies within an hour."
)


def test_mixed_review_fallback_path_still_surfaces_the_product_claim() -> None:
    """Under the deterministic fallback, detect_aspects matches multiple aspects
    (shipping AND battery), so the review passes analyze_item's relevance gate and
    extract_claims surfaces the non-shipping claim too -- today's actually-achievable
    behavior, not a claim that mixed content is handled perfectly."""
    evidence = make_evidence(body=MIXED_SHIPPING_AND_PRODUCT_BODY)
    analysis = analyze_item("wireless earbuds", _evidence_to_item(evidence), no_llm())
    assert analysis["is_relevant"] is True

    result = extract_claims("wireless earbuds", evidence, no_llm())
    aspects_found = {claim.aspect_raw for claim in result.claims}
    assert "battery" in aspects_found


def test_mixed_review_llm_relevance_gate_can_drop_the_product_claim_known_limitation() -> None:
    """Documented Phase 1 limitation: if analyze_item's LLM path judges a genuinely
    mixed review as 'not relevant' (e.g. dominant tone reads as shipping/service
    noise), extract_claims never runs at all and the real product signal is lost.
    Phase 2 (review quality screening) replaces this all-or-nothing gate. This test
    pins today's behavior so Phase 2's fix has something to flip."""
    from app.models import CollectedItem

    item = CollectedItem(
        source_url="https://reddit.com/mixed",
        subreddit="gadgets",
        item_type="post",
        post_id="p1",
        comment_id=None,
        title="Disappointed",
        body=MIXED_SHIPPING_AND_PRODUCT_BODY,
        score=1,
        comment_count=0,
        created_at="2026-01-01T00:00:00+00:00",
        search_query="earbuds",
    )
    llm = FakeLLM(response={"is_relevant": False})
    analysis = analyze_item("wireless earbuds", item, llm)
    assert analysis["is_relevant"] is False  # known gap: extract_claims is never reached from here


def _evidence_to_item(evidence: Evidence):
    from app.models import CollectedItem

    return CollectedItem(
        source_url=evidence.source_url,
        subreddit=evidence.subreddit,
        item_type=evidence.item_type,
        post_id=None,
        comment_id=None,
        title=evidence.title,
        body=evidence.body,
        score=evidence.score,
        comment_count=evidence.comment_count,
        created_at=evidence.created_at,
        search_query=evidence.search_query,
    )


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


def test_get_claims_endpoint_returns_empty_list_for_pre_phase_1_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "routes_test2.sqlite3"
    monkeypatch.setattr(routes_module, "DEFAULT_DB_PATH", db_path)

    storage = Storage(db_path)
    storage.migrate()
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.close()

    assert routes_module.get_claims(run.run_id) == []
