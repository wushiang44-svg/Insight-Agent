from __future__ import annotations

from pathlib import Path
from typing import Any

from app.llm import DeepSeekClient
from app.models import CollectedItem, Evidence, InsightType, ScreeningCategory, Sentiment
from app.pipeline.claims import extract_claims
from app.pipeline.screening import screen_item
from app.storage import Storage


def make_item(
    source_url: str = "https://reddit.com/x",
    title: str = "Review title",
    body: str = "Battery life is terrible, dies so fast.",
) -> CollectedItem:
    return CollectedItem(
        source_url=source_url,
        subreddit="gadgets",
        item_type="post",
        post_id="p1",
        comment_id=None,
        title=title,
        body=body,
        score=5,
        comment_count=0,
        created_at="2026-01-01T00:00:00+00:00",
        search_query="laptop",
    )


def make_evidence(
    evidence_id: str = "ev_1",
    run_id: str = "run_1",
    body: str = "Battery life is terrible, dies so fast.",
    screening_categories: list[str] | None = None,
    is_mixed_content: bool = False,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        run_id=run_id,
        iteration=1,
        source_url="https://reddit.com/x",
        subreddit="gadgets",
        item_type="post",
        title="Review title",
        body=body,
        score=5,
        comment_count=0,
        created_at="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:00+00:00",
        search_query="laptop",
        insight_type=InsightType.PAIN_POINT,
        aspect="battery",
        sentiment=Sentiment.NEGATIVE,
        quote=body,
        confidence=0.6,
        screening_categories=screening_categories,
        is_mixed_content=is_mixed_content,
    )


def no_llm() -> DeepSeekClient:
    return DeepSeekClient(api_key="")


class FakeLLM:
    """Duck-types the DeepSeekClient interface used by pipeline/screening.py
    and pipeline/claims.py."""

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


MIXED_CONTENT_BODY = "Shipping was terrible, but the battery only lasts three hours and the laptop overheats."


# ---------------------------------------------------------------------------
# Single-category cases
# ---------------------------------------------------------------------------


def test_pure_product_review_llm_path() -> None:
    item = make_item(body="The battery life is amazing and the screen is gorgeous.")
    response = {
        "categories": ["product_feedback"],
        "insight_type": "praise",
        "aspect": "battery",
        "sentiment": "positive",
        "quote": "The battery life is amazing",
        "confidence": 0.9,
    }
    result = screen_item("laptop", item, FakeLLM(response=response))

    assert result.categories == [ScreeningCategory.PRODUCT_FEEDBACK]
    assert result.has_product_signal is True
    assert result.is_mixed_content is False
    assert result.is_evidence_worthy is True


def test_pure_shipping_complaint_still_evidence_worthy() -> None:
    """Matches today's already-correct behavior: a pure shipping complaint (no
    product content) is not spam -- it's real feedback the legacy Report tracks
    -- so it must remain Evidence, just without product signal."""
    item = make_item(body="Shipping took three weeks and the box was crushed.")
    response = {
        "categories": ["shipping_logistics"],
        "insight_type": "pain_point",
        "aspect": "shipping",
        "sentiment": "negative",
        "quote": "Shipping took three weeks",
        "confidence": 0.85,
    }
    result = screen_item("laptop", item, FakeLLM(response=response))

    assert result.categories == [ScreeningCategory.SHIPPING_LOGISTICS]
    assert result.has_product_signal is False
    assert result.is_evidence_worthy is True


def test_seller_service_only_evidence_worthy_no_product_signal() -> None:
    item = make_item(body="Customer service was rude and unhelpful when I asked for a refund.")
    response = {
        "categories": ["seller_service"],
        "insight_type": "pain_point",
        "aspect": "customer_service",
        "sentiment": "negative",
        "confidence": 0.85,
    }
    result = screen_item("laptop", item, FakeLLM(response=response))

    assert result.categories == [ScreeningCategory.SELLER_SERVICE]
    assert result.has_product_signal is False
    assert result.is_evidence_worthy is True


def test_pure_spam_not_evidence_worthy() -> None:
    item = make_item(body="Check out my crypto channel, link in bio!!")
    response = {
        "categories": ["spam_or_irrelevant"],
        "insight_type": "noise",
        "aspect": "general",
        "sentiment": "neutral",
        "confidence": 0.9,
    }
    result = screen_item("laptop", item, FakeLLM(response=response))

    assert result.is_evidence_worthy is False
    assert result.has_product_signal is False


def test_low_information_not_evidence_worthy() -> None:
    item = make_item(body="ok")
    response = {
        "categories": ["low_information"],
        "insight_type": "noise",
        "aspect": "general",
        "sentiment": "neutral",
        "confidence": 0.3,
    }
    result = screen_item("laptop", item, FakeLLM(response=response))

    assert result.is_evidence_worthy is False


# ---------------------------------------------------------------------------
# The core fix -- mixed content
# ---------------------------------------------------------------------------


def test_mixed_content_review_is_evidence_worthy_and_has_product_signal() -> None:
    item = make_item(body=MIXED_CONTENT_BODY)
    response = {
        "categories": ["shipping_logistics", "product_feedback"],
        "insight_type": "pain_point",
        "aspect": "battery",
        "sentiment": "negative",
        "quote": "the battery only lasts three hours",
        "confidence": 0.9,
    }
    result = screen_item("laptop", item, FakeLLM(response=response))

    assert set(result.categories) == {ScreeningCategory.SHIPPING_LOGISTICS, ScreeningCategory.PRODUCT_FEEDBACK}
    assert result.has_product_signal is True
    assert result.is_mixed_content is True
    assert result.is_evidence_worthy is True


def test_mixed_content_review_reaches_extract_claims_and_yields_real_claims() -> None:
    """The actual regression test for the brief's bug: a mixed review's
    screening result, fed into extract_claims() exactly as react_agent.py
    would, must yield the real product Claims -- not just pass screening."""
    evidence = make_evidence(
        body=MIXED_CONTENT_BODY,
        screening_categories=["shipping_logistics", "product_feedback"],
        is_mixed_content=True,
    )
    claims_response = {
        "claims": [
            {
                "claim_type": "problem",
                "aspect_raw": "battery life",
                "statement": "Battery only lasts three hours.",
                "sentiment": "negative",
            },
            {
                "claim_type": "problem",
                "aspect_raw": "overheating",
                "statement": "The laptop overheats.",
                "sentiment": "negative",
            },
        ]
    }
    result = extract_claims("laptop", evidence, FakeLLM(response=claims_response))

    aspects = {c.aspect_raw for c in result.claims}
    assert "battery life" in aspects
    assert "overheating" in aspects


def test_extract_claims_is_reachable_even_when_has_product_signal_is_false() -> None:
    """The central correctness requirement of Phase 2: extract_claims() is never
    signal-gated. A pure shipping/service Evidence (has_product_signal=False)
    must still be a valid, callable input to extract_claims() -- react_agent.py
    calls it unconditionally for every evidence-worthy item."""
    evidence = make_evidence(
        body="Shipping took three weeks and the box was crushed.",
        screening_categories=["shipping_logistics"],
        is_mixed_content=False,
    )
    llm = FakeLLM(response={"claims": []})
    result = extract_claims("laptop", evidence, llm)

    assert llm.calls == 1  # the call happened -- not skipped based on screening's has_product_signal
    assert result.succeeded is True


# ---------------------------------------------------------------------------
# Regression: real-data false negative found during Phase 2 validation.
# A genuine, long product review whose opening is a self-declared off-topic
# tangent ("this is not a review, but I had to warn others...") got screened
# as spam_or_irrelevant, even though it contains real buried product signal
# ("my water boiler is still running strong", "the button is still a bit
# difficult to press"). Root cause traced to the prompt letting the tangent's
# self-description dominate; fixed by an explicit prompt instruction, not a
# code change. This pins the CONTRACT screen_item() must honor: given a
# response that correctly reports product_feedback alongside a dominant
# off-topic tangent, the derivation logic must treat it as evidence-worthy
# with product signal, not silently lose it. The real prompt's actual
# real-world effectiveness on this exact text is verified separately with a
# live DeepSeek call (see the Phase 2 tuning validation), not here -- this
# test only pins the deterministic part: what the system does with a
# correctly-shaped response.
# ---------------------------------------------------------------------------

TANGENT_WITH_BURIED_PRODUCT_SIGNAL_BODY = (
    "This is not a review, but I had to warn others: do NOT pour boiling water down your "
    "bathroom pipes. It ruined a wax ring under my toilet and I had to replace it, which had "
    "nothing to do with this product -- I just wanted to share the warning since so many "
    "people boil water for cleaning. Anyway, while I'm here: my water boiler is still running "
    "strong after all this time. The button is still a bit difficult to press, but otherwise "
    "no complaints. Hope the plumbing warning helps someone."
)


def test_tangent_heavy_review_with_buried_product_signal_is_evidence_worthy() -> None:
    """Regression for the traced kettle false negative: a review whose visible
    text is dominated by an unrelated tangent (and even says so itself) must
    still be product_feedback + evidence-worthy when the model correctly
    reports genuine buried product signal, per the corrected prompt contract."""
    item = make_item(body=TANGENT_WITH_BURIED_PRODUCT_SIGNAL_BODY)
    response = {
        "categories": ["product_feedback"],
        "insight_type": "praise",
        "aspect": "durability",
        "sentiment": "positive",
        "quote": "my water boiler is still running strong",
        "confidence": 0.7,
    }
    result = screen_item("kettle", item, FakeLLM(response=response))

    assert ScreeningCategory.PRODUCT_FEEDBACK in result.categories
    assert result.has_product_signal is True
    assert result.is_evidence_worthy is True


def test_tangent_heavy_review_reaches_extract_claims_and_yields_the_buried_claim() -> None:
    """End-to-end version of the same regression: the buried product signal
    must survive all the way to a real extracted Claim, not just pass
    screening -- the same standard applied to the brief's mixed-content case."""
    evidence = make_evidence(
        body=TANGENT_WITH_BURIED_PRODUCT_SIGNAL_BODY,
        screening_categories=["product_feedback"],
        is_mixed_content=False,
    )
    claims_response = {
        "claims": [
            {
                "claim_type": "praise",
                "aspect_raw": "durability",
                "statement": "The water boiler is still running strong after a long time.",
                "sentiment": "positive",
            },
            {
                "claim_type": "problem",
                "aspect_raw": "button",
                "statement": "The button is still a bit difficult to press.",
                "sentiment": "negative",
            },
        ]
    }
    result = extract_claims("kettle", evidence, FakeLLM(response=claims_response))

    aspects = {c.aspect_raw for c in result.claims}
    assert "durability" in aspects
    assert "button" in aspects


# ---------------------------------------------------------------------------
# Milestone 1 / A1b: rebuttal & stance -- sentiment-only correction.
#
# Real traced example (run_55025c50e81b, ev_cc49804b000a, "robot vacuum
# cleaners"): a comment disputing another commenter's floor-damage claim was
# stored with sentiment=negative, which feeds directly into the report-wide
# Product Health score / sentiment donut (react_agent.py's sentiment_breakdown
# is computed from Evidence.sentiment unconditionally, on every report,
# regardless of report_source). This section pins the sentiment-only
# correction contract -- deliberately NOT touching insight_type selection,
# per Milestone 1's scope fence.
# ---------------------------------------------------------------------------

REBUTTAL_DISPUTES_FLOOR_DAMAGE_BODY = (
    "Is it possible you started paying attention to your floors now that they're getting "
    "cleaned by a robot? Those look old like they were done by something with enough "
    "weight to create dents and ridges..."
)


def test_rebuttal_gets_corrected_sentiment_not_negative() -> None:
    item = make_item(body=REBUTTAL_DISPUTES_FLOOR_DAMAGE_BODY)
    response = {
        "categories": ["product_feedback"],
        "insight_type": "pain_point",
        "aspect": "floor_damage",
        "sentiment": "neutral",
        "quote": "Those look old like they were done by something with enough weight",
        "confidence": 0.7,
    }
    result = screen_item("robot vacuum cleaners", item, FakeLLM(response=response))

    assert result.sentiment != Sentiment.NEGATIVE


def test_screening_insight_type_is_not_altered_by_the_sentiment_fix() -> None:
    """Documents the accepted A1b scope boundary: this milestone corrects
    `sentiment` only. screening.py's InsightType enum has no equivalent to
    Claim's `observation` bucket, and adding one is explicitly out of scope
    (see the Milestone 1 plan) -- a rebuttal can still legally come back as
    `insight_type=pain_point`, and screen_item() must pass whatever
    `insight_type` the model returns through unchanged, never silently
    coerce it based on the sentiment correction."""
    item = make_item(body=REBUTTAL_DISPUTES_FLOOR_DAMAGE_BODY)
    response = {
        "categories": ["product_feedback"],
        "insight_type": "pain_point",
        "aspect": "floor_damage",
        "sentiment": "neutral",
        "confidence": 0.7,
    }
    result = screen_item("robot vacuum cleaners", item, FakeLLM(response=response))

    assert result.insight_type == InsightType.PAIN_POINT  # unchanged -- known, accepted limitation


def test_hedged_firsthand_complaint_stays_negative_in_screening() -> None:
    """False-positive guard: a genuine firsthand complaint phrased with a
    hedge must not have its sentiment softened just because it superficially
    resembles rebuttal-style wording."""
    item = make_item(
        body=(
            "Unless you're way rougher on your floors than most people, you shouldn't see "
            "this kind of damage -- but mine showed up after about two months of normal use."
        )
    )
    response = {
        "categories": ["product_feedback"],
        "insight_type": "pain_point",
        "aspect": "floor_damage",
        "sentiment": "negative",
        "confidence": 0.7,
    }
    result = screen_item("robot vacuum cleaners", item, FakeLLM(response=response))

    assert result.sentiment == Sentiment.NEGATIVE


# ---------------------------------------------------------------------------
# Fallback path (no LLM)
# ---------------------------------------------------------------------------


def test_fallback_produces_multi_label_categories_not_just_first_match() -> None:
    """Regression for the old analyze_item() fallback's aspects[0]-only bug:
    detect_aspects() already returns every match -- the fallback screening must
    use all of them, not silently drop everything but the first."""
    item = make_item(
        body=(
            "Shipping took forever and the box arrived crushed. Aside from that, "
            "the battery life is terrible and dies within an hour."
        )
    )
    result = screen_item("laptop", item, no_llm())

    assert result.extraction_method == "fallback_rules"
    assert ScreeningCategory.SHIPPING_LOGISTICS in result.categories
    assert ScreeningCategory.PRODUCT_FEEDBACK in result.categories
    assert result.is_mixed_content is True
    assert result.is_evidence_worthy is True


def test_fallback_pure_low_information_not_evidence_worthy() -> None:
    item = make_item(body="meh")
    result = screen_item("laptop", item, no_llm())

    assert result.categories == [ScreeningCategory.LOW_INFORMATION]
    assert result.is_evidence_worthy is False


def test_fallback_no_aspect_match_longer_text_is_spam_or_irrelevant() -> None:
    item = make_item(body="Went for a walk today and had a sandwich, lovely weather this week.")
    result = screen_item("laptop", item, no_llm())

    assert result.categories == [ScreeningCategory.SPAM_OR_IRRELEVANT]
    assert result.is_evidence_worthy is False


def test_malformed_llm_response_falls_back_to_deterministic_screening() -> None:
    item = make_item(body="Battery life is terrible, dies so fast.")
    llm = FakeLLM(response={"not_categories_at_all": True})
    result = screen_item("laptop", item, llm)

    assert result.extraction_method == "fallback_rules"
    assert result.is_evidence_worthy is True


def test_empty_categories_list_from_llm_falls_back() -> None:
    """RawScreening requires categories to be non-empty -- an LLM response with
    an empty list must not be silently accepted as 'nothing here'."""
    item = make_item(body="Battery life is terrible, dies so fast.")
    llm = FakeLLM(response={"categories": [], "insight_type": "noise", "aspect": "general", "sentiment": "neutral", "confidence": 0.5})
    result = screen_item("laptop", item, llm)

    assert result.extraction_method == "fallback_rules"


# ---------------------------------------------------------------------------
# Migration / backward compatibility
# ---------------------------------------------------------------------------


def test_old_evidence_row_without_screening_data_reads_back_fine(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    evidence = make_evidence(evidence_id="ev_old", run_id="run_1")  # screening_categories/is_mixed_content left at defaults
    storage.save_evidence(evidence)

    loaded = storage.list_evidence("run_1")[0]

    assert loaded.screening_categories is None
    assert loaded.is_mixed_content is False


def test_evidence_with_screening_data_round_trips(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    evidence = make_evidence(
        evidence_id="ev_mixed",
        run_id="run_1",
        body=MIXED_CONTENT_BODY,
        screening_categories=["shipping_logistics", "product_feedback"],
        is_mixed_content=True,
    )
    storage.save_evidence(evidence)

    loaded = storage.list_evidence("run_1")[0]

    assert loaded.screening_categories == ["shipping_logistics", "product_feedback"]
    assert loaded.is_mixed_content is True
