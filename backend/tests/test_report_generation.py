from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from app.llm import DeepSeekClient
from app.models import CanonicalCategory, CategoryStatus, Claim, ClaimType, Evidence, InsightType, Sentiment
from app.react_agent import (
    AggregateGroup,
    ReportInputs,
    ResolvedCategory,
    _aggregate_by_aspect,
    _aggregate_claims_by_category,
    _build_report_inputs,
    _build_report_inputs_from_evidence,
    _resolve_categories,
    _summarize_fallback,
    _summarize_llm,
    _thread_dampening_cap,
    _thread_key,
    _weighted_count,
    summarize,
)


def no_llm() -> DeepSeekClient:
    return DeepSeekClient(api_key="")


class _AssertNotCalledLLM:
    """Duck-types the DeepSeekClient interface used by summarize(): available()
    is True, so the P0 content guard is the only thing standing between an
    empty report and a real LLM call. json_chat() fails the test immediately
    if it's ever invoked -- proving the guard, not luck, is what prevented it."""

    def available(self) -> bool:
        return True

    def json_chat(self, model: str, system: str, user: str) -> dict:
        raise AssertionError("_summarize_llm() must not be called when there is no report content")


class _CannedLLM:
    """Duck-types the DeepSeekClient interface: available() is True and
    json_chat() returns a fixed response while counting calls, so tests can
    confirm the LLM path still runs (unchanged) whenever there IS report
    content -- the P0 guard must never suppress a legitimate call."""

    def __init__(self, response: dict[str, object]):
        self._response = response
        self.calls = 0

    def available(self) -> bool:
        return True

    def json_chat(self, model: str, system: str, user: str) -> dict[str, object]:
        self.calls += 1
        return self._response


_CANNED_LLM_RESPONSE = {
    "recommended_actions_en": ["do the thing"],
    "recommended_actions_zh": ["做那件事"],
    "summary_markdown_en": "# summary",
    "summary_markdown_zh": "# 总结",
}


class _CapturingLLM:
    """Duck-types the DeepSeekClient interface: available() is True,
    json_chat() records every (model, system, user) call it receives -- so
    tests can assert on the exact prompt text _summarize_llm() builds --
    and returns a canned response."""

    def __init__(self, response: dict[str, object]):
        self._response = response
        self.calls: list[tuple[str, str, str]] = []

    def available(self) -> bool:
        return True

    def json_chat(self, model: str, system: str, user: str) -> dict[str, object]:
        self.calls.append((model, system, user))
        return self._response


def make_category(
    category_id: str,
    canonical_label: str = "battery life",
    status: CategoryStatus = CategoryStatus.APPROVED,
    alias_of: str | None = None,
) -> CanonicalCategory:
    return CanonicalCategory(
        category_id=category_id,
        product_category="dog food",
        canonical_label=canonical_label,
        normalized_label=canonical_label.lower(),
        status=status,
        first_seen_aspect_raw=canonical_label,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        alias_of=alias_of,
    )


def make_claim(
    claim_id: str,
    evidence_id: str = "ev_1",
    claim_type: ClaimType = ClaimType.PROBLEM,
    aspect_raw: str = "battery life",
    canonical_category: str | None = None,
    confidence: float = 0.8,
    sentiment: Sentiment = Sentiment.NEGATIVE,
) -> Claim:
    return Claim(
        claim_id=claim_id,
        run_id="run_1",
        evidence_id=evidence_id,
        claim_type=claim_type,
        aspect_raw=aspect_raw,
        statement="statement",
        sentiment=sentiment,
        confidence=confidence,
        extraction_method="llm",
        canonical_category=canonical_category,
    )


def make_evidence(
    evidence_id: str = "ev_1",
    source_url: str = "https://reddit.com/r/dogfood/comments/abc123/some_title/",
    subreddit: str = "dogfood",
    quote: str = "the evidence quote",
    score: int = 5,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        run_id="run_1",
        iteration=1,
        source_url=source_url,
        subreddit=subreddit,
        item_type="post",
        title="title",
        body="body",
        score=score,
        comment_count=0,
        created_at="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:00+00:00",
        search_query="query",
        insight_type=InsightType.PAIN_POINT,
        aspect="battery",
        sentiment=Sentiment.NEGATIVE,
        quote=quote,
        confidence=0.6,
    )


# ---------------------------------------------------------------------------
# _resolve_categories
# ---------------------------------------------------------------------------


def test_resolve_categories_approved() -> None:
    category = make_category("cc_a", status=CategoryStatus.APPROVED)
    claim = make_claim("cl_1", canonical_category="cc_a")

    resolved = _resolve_categories([claim], [category])

    assert resolved["cl_1"] == ResolvedCategory(category_id="cc_a", label="battery life", status="approved")


def test_resolve_categories_proposed() -> None:
    category = make_category("cc_a", status=CategoryStatus.PROPOSED)
    claim = make_claim("cl_1", canonical_category="cc_a")

    resolved = _resolve_categories([claim], [category])

    assert resolved["cl_1"].status == "proposed"
    assert resolved["cl_1"].category_id == "cc_a"


def test_resolve_categories_uncategorized_when_no_canonical_category() -> None:
    claim = make_claim("cl_1", canonical_category=None)

    resolved = _resolve_categories([claim], [])

    assert resolved["cl_1"] == ResolvedCategory(category_id=None, label="Uncategorized", status="uncategorized")


def test_resolve_categories_uncategorized_when_category_id_not_found() -> None:
    claim = make_claim("cl_1", canonical_category="cc_does_not_exist")

    resolved = _resolve_categories([claim], [])

    assert resolved["cl_1"].status == "uncategorized"


def test_resolve_categories_deprecated_without_alias_is_uncategorized() -> None:
    category = make_category("cc_a", status=CategoryStatus.DEPRECATED, alias_of=None)
    claim = make_claim("cl_1", canonical_category="cc_a")

    resolved = _resolve_categories([claim], [category])

    assert resolved["cl_1"].status == "uncategorized"


def test_resolve_categories_alias_resolves_one_hop_to_target() -> None:
    target = make_category("cc_target", canonical_label="floor damage", status=CategoryStatus.APPROVED)
    source = make_category("cc_source", canonical_label="floor_damage", alias_of="cc_target")
    claim = make_claim("cl_1", canonical_category="cc_source")

    resolved = _resolve_categories([claim], [target, source])

    # Resolves to the TARGET's identity, not the source alias's own label.
    assert resolved["cl_1"] == ResolvedCategory(category_id="cc_target", label="floor damage", status="approved")


def test_resolve_categories_alias_whose_target_is_later_deprecated_is_uncategorized() -> None:
    target = make_category("cc_target", status=CategoryStatus.DEPRECATED)
    source = make_category("cc_source", alias_of="cc_target")
    claim = make_claim("cl_1", canonical_category="cc_source")

    resolved = _resolve_categories([claim], [target, source])

    assert resolved["cl_1"].status == "uncategorized"


def test_resolve_categories_alias_target_missing_is_uncategorized() -> None:
    source = make_category("cc_source", alias_of="cc_does_not_exist")
    claim = make_claim("cl_1", canonical_category="cc_source")

    resolved = _resolve_categories([claim], [source])

    assert resolved["cl_1"].status == "uncategorized"


# ---------------------------------------------------------------------------
# _thread_key
# ---------------------------------------------------------------------------


def test_thread_key_shares_reddit_permalink_prefix() -> None:
    a = make_evidence(evidence_id="ev_a", source_url="https://www.reddit.com/r/dogfood/comments/abc123/some_title/")
    b = make_evidence(evidence_id="ev_b", source_url="https://www.reddit.com/r/dogfood/comments/abc123/some_title/comment_xyz/")
    assert _thread_key(a) == _thread_key(b)


def test_thread_key_falls_back_to_evidence_id_for_non_reddit_sources() -> None:
    evidence = make_evidence(evidence_id="ev_amazon", source_url="https://www.amazon.com/reviews/R123ABC")
    assert _thread_key(evidence) == "ev_amazon"


# ---------------------------------------------------------------------------
# _aggregate_claims_by_category
# ---------------------------------------------------------------------------


def test_aggregate_groups_by_claim_type_and_resolved_category() -> None:
    category = make_category("cc_a")
    ev = make_evidence("ev_1")
    claim_a = make_claim("cl_a", evidence_id="ev_1", canonical_category="cc_a", confidence=0.9)
    claim_b = make_claim("cl_b", evidence_id="ev_1", canonical_category="cc_a", confidence=0.5)
    resolved = _resolve_categories([claim_a, claim_b], [category])

    aggregated = _aggregate_claims_by_category([claim_a, claim_b], resolved, {"ev_1": ev})

    [group] = list(aggregated.values())
    assert group.count == 2
    assert group.category_status == "approved"
    assert group.label == "battery life"
    assert group.avg_confidence == pytest.approx(0.7)


def test_aggregate_preserves_category_status_for_proposed_groups() -> None:
    category = make_category("cc_a", status=CategoryStatus.PROPOSED)
    claim = make_claim("cl_1", canonical_category="cc_a")
    resolved = _resolve_categories([claim], [category])

    aggregated = _aggregate_claims_by_category([claim], resolved, {"ev_1": make_evidence("ev_1")})

    [group] = list(aggregated.values())
    assert group.category_status == "proposed"


def test_uncategorized_claims_share_one_bucket_per_claim_type_never_split_by_aspect() -> None:
    claim_a = make_claim("cl_a", evidence_id="ev_a", aspect_raw="floor damage", canonical_category=None)
    claim_b = make_claim("cl_b", evidence_id="ev_b", aspect_raw="totally different aspect", canonical_category=None)
    resolved = _resolve_categories([claim_a, claim_b], [])
    evidence_by_id = {"ev_a": make_evidence("ev_a"), "ev_b": make_evidence("ev_b")}

    aggregated = _aggregate_claims_by_category([claim_a, claim_b], resolved, evidence_by_id)

    # One single group for this claim_type, despite two totally different
    # aspect_raw strings -- never split back out by aspect_raw.
    assert len(aggregated) == 1
    [group] = list(aggregated.values())
    assert group.count == 2
    assert group.category_key == "uncategorized"
    assert group.label == "Uncategorized"


def test_uncategorized_bucket_is_separate_per_claim_type() -> None:
    problem = make_claim("cl_a", evidence_id="ev_a", claim_type=ClaimType.PROBLEM, canonical_category=None)
    praise = make_claim("cl_b", evidence_id="ev_b", claim_type=ClaimType.PRAISE, canonical_category=None)
    resolved = _resolve_categories([problem, praise], [])
    evidence_by_id = {"ev_a": make_evidence("ev_a"), "ev_b": make_evidence("ev_b")}

    aggregated = _aggregate_claims_by_category([problem, praise], resolved, evidence_by_id)

    assert len(aggregated) == 2  # (PROBLEM, uncategorized) and (PRAISE, uncategorized) stay separate


def test_example_quotes_prefer_source_excerpt_over_evidence_quote() -> None:
    from dataclasses import replace

    category = make_category("cc_a")
    claim = replace(make_claim("cl_1", canonical_category="cc_a"), source_excerpt="the specific excerpt")
    resolved = _resolve_categories([claim], [category])
    evidence = make_evidence("ev_1", quote="the whole-review quote")

    aggregated = _aggregate_claims_by_category([claim], resolved, {"ev_1": evidence})

    [group] = list(aggregated.values())
    assert group.example_quotes[0]["quote"] == "the specific excerpt"


# ---------------------------------------------------------------------------
# _build_report_inputs -- shipping/service thresholds
# ---------------------------------------------------------------------------


def _synthetic_thread_counts(count: int, thread_count: int) -> dict[str, int]:
    """Test-only helper: spreads `count` claims as evenly as possible across
    up to `thread_count` synthetic thread keys. Purely for constructing a
    physically-plausible AggregateGroup fixture -- production code always
    derives this from real evidence via _aggregate_claims_by_category."""
    if count <= 0 or thread_count <= 0:
        return {}
    buckets = min(thread_count, count)
    base, remainder = divmod(count, buckets)
    return {f"t{i}": base + (1 if i < remainder else 0) for i in range(buckets)}


def make_group(
    claim_type: ClaimType,
    category_key: str = "cc_a",
    count: int = 5,
    evidence_count: int = 5,
    thread_count: int = 5,
    thread_counts: dict[str, int] | None = None,
) -> AggregateGroup:
    return AggregateGroup(
        claim_type=claim_type,
        category_key=category_key,
        label="some label",
        category_status="approved",
        count=count,
        subreddit_count=1,
        avg_confidence=0.8,
        sentiment_counts={"negative": count},
        example_quotes=[],
        evidence_count=evidence_count,
        thread_count=thread_count,
        thread_counts=thread_counts if thread_counts is not None else _synthetic_thread_counts(count, thread_count),
    )


def test_shipping_issue_below_threshold_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIPPING_SERVICE_MIN_CLAIMS", "3")
    group = make_group(ClaimType.SHIPPING_ISSUE, count=2, evidence_count=5, thread_count=5)

    inputs = _build_report_inputs({(ClaimType.SHIPPING_ISSUE, "cc_a"): group})

    assert inputs.shipping_issues == []


def test_shipping_issue_at_threshold_is_included(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIPPING_SERVICE_MIN_CLAIMS", "3")
    monkeypatch.setenv("SHIPPING_SERVICE_MIN_EVIDENCE", "3")
    monkeypatch.setenv("SHIPPING_SERVICE_MIN_THREADS", "1")
    group = make_group(ClaimType.SHIPPING_ISSUE, count=3, evidence_count=3, thread_count=1)

    inputs = _build_report_inputs({(ClaimType.SHIPPING_ISSUE, "cc_a"): group})

    assert len(inputs.shipping_issues) == 1


def test_seller_service_issue_below_thread_threshold_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIPPING_SERVICE_MIN_THREADS", "2")
    group = make_group(ClaimType.SELLER_SERVICE_ISSUE, count=10, evidence_count=10, thread_count=1)

    inputs = _build_report_inputs({(ClaimType.SELLER_SERVICE_ISSUE, "cc_a"): group})

    assert inputs.seller_service_issues == []


def test_the_four_always_surfaced_sections_are_never_threshold_gated() -> None:
    # count=1, evidence_count=1, thread_count=1 -- would fail any nonzero
    # shipping/service threshold, but pain_points/etc. have no gate at all.
    group = make_group(ClaimType.PROBLEM, count=1, evidence_count=1, thread_count=1)

    inputs = _build_report_inputs({(ClaimType.PROBLEM, "cc_a"): group})

    assert len(inputs.top_pain_points) == 1


def test_question_observation_noise_claim_types_never_reach_the_report() -> None:
    aggregated = {
        (ClaimType.QUESTION, "cc_a"): make_group(ClaimType.QUESTION),
        (ClaimType.OBSERVATION, "cc_a"): make_group(ClaimType.OBSERVATION),
        (ClaimType.NOISE, "cc_a"): make_group(ClaimType.NOISE),
    }

    inputs = _build_report_inputs(aggregated)

    all_entries = (
        inputs.top_pain_points + inputs.feature_requests + inputs.praised_aspects
        + inputs.competitor_mentions + inputs.shipping_issues + inputs.seller_service_issues
    )
    assert all_entries == []


def test_report_inputs_sections_sorted_by_count_descending() -> None:
    small = make_group(ClaimType.PROBLEM, category_key="cc_small", count=1)
    big = make_group(ClaimType.PROBLEM, category_key="cc_big", count=9)

    inputs = _build_report_inputs({(ClaimType.PROBLEM, "cc_small"): small, (ClaimType.PROBLEM, "cc_big"): big})

    assert [entry["count"] for entry in inputs.top_pain_points] == [9, 1]


# ---------------------------------------------------------------------------
# Milestone 3 / A3 -- thread-concentration dampening
# ---------------------------------------------------------------------------


def test_weighted_count_caps_each_thread_independently() -> None:
    # One thread with 10 claims, one with 1 -- capped at 3, the big thread's
    # extra 7 claims stop moving the ranking needle past the cap.
    assert _weighted_count({"thread_a": 10, "thread_b": 1}, cap=3) == 4


def test_weighted_count_is_a_no_op_when_no_thread_exceeds_the_cap() -> None:
    # Real-run finding: a group with 3 threads of 1 claim each is completely
    # unaffected by dampening -- weighted_count == raw count.
    thread_counts = {"a": 1, "b": 1, "c": 1}
    assert _weighted_count(thread_counts, cap=3) == sum(thread_counts.values())


def test_weighted_count_empty_thread_counts_is_zero() -> None:
    assert _weighted_count({}, cap=3) == 0


def test_thread_dampening_cap_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREAD_DAMPENING_CAP", "5")
    assert _thread_dampening_cap() == 5


def test_thread_dampening_cap_defaults_to_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THREAD_DAMPENING_CAP", raising=False)
    assert _thread_dampening_cap() == 3


def _make_thread_claims(
    claim_prefix: str,
    thread_id: str,
    n: int,
    category_id: str,
    claim_type: ClaimType,
    evidence_by_id: dict[str, Evidence],
) -> list[Claim]:
    """Builds n claims, each from its own comment within the SAME Reddit
    thread (shared /comments/<thread_id>/ prefix), all resolved to
    category_id -- the exact real shape _thread_key() groups on."""
    claims = []
    for i in range(n):
        evidence_id = f"ev_{claim_prefix}_{i}"
        evidence_by_id[evidence_id] = make_evidence(
            evidence_id=evidence_id,
            source_url=f"https://reddit.com/r/dogfood/comments/{thread_id}/comment/c{i}/",
        )
        claims.append(
            make_claim(f"cl_{claim_prefix}_{i}", evidence_id=evidence_id, claim_type=claim_type, canonical_category=category_id)
        )
    return claims


def test_dampened_sort_matches_the_real_run_55025c50e81b_finding() -> None:
    """Reproduces the exact real-data finding from the Milestone 3 plan
    (run_55025c50e81b): floor damage (11 claims, 2 threads) stays #1;
    app real time position update (9 claims, ALL from 1 thread) must rank
    BELOW navigation/collision avoidance (3 claims, 3 independent threads)
    despite having 3x the raw mentions."""
    evidence_by_id: dict[str, Evidence] = {}
    floor = make_category("cc_floor", "floor damage")
    app_pos = make_category("cc_app", "app real time position update")
    nav = make_category("cc_nav", "navigation/collision avoidance")

    claims: list[Claim] = []
    claims += _make_thread_claims("floor_a", "t_floor_a", 10, "cc_floor", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("floor_b", "t_floor_b", 1, "cc_floor", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("app", "t_app", 9, "cc_app", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("nav_a", "t_nav_a", 1, "cc_nav", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("nav_b", "t_nav_b", 1, "cc_nav", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("nav_c", "t_nav_c", 1, "cc_nav", ClaimType.PROBLEM, evidence_by_id)

    resolved = _resolve_categories(claims, [floor, app_pos, nav])
    aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
    inputs = _build_report_inputs(aggregated)

    labels_in_order = [entry["aspect"] for entry in inputs.top_pain_points]
    assert labels_in_order == ["floor damage", "navigation/collision avoidance", "app real time position update"]

    by_label = {entry["aspect"]: entry for entry in inputs.top_pain_points}
    # Raw counts must stay exactly as collected, regardless of rank.
    assert by_label["floor damage"]["count"] == 11
    assert by_label["app real time position update"]["count"] == 9
    assert by_label["navigation/collision avoidance"]["count"] == 3
    # weighted_count/thread_count reflect the dampening that reordered them.
    assert by_label["floor damage"]["weighted_count"] == 4
    assert by_label["app real time position update"]["weighted_count"] == 3
    assert by_label["navigation/collision avoidance"]["weighted_count"] == 3
    assert by_label["navigation/collision avoidance"]["thread_count"] == 3
    assert by_label["app real time position update"]["thread_count"] == 1


def test_tiebreak_uses_thread_count_before_raw_count() -> None:
    """The exact contradiction caught during plan review: two categories tied
    on weighted_count must NOT fall through to raw count first -- the more
    thread-diverse one must win."""
    evidence_by_id: dict[str, Evidence] = {}
    cat_diverse = make_category("cc_diverse", "diverse issue")
    cat_single = make_category("cc_single", "single-thread issue")

    claims: list[Claim] = []
    # Both weighted_count == 3. cat_single has the higher RAW count (9 vs 3),
    # but only 1 thread -- must rank BELOW the 3-thread group.
    claims += _make_thread_claims("single", "t_single", 9, "cc_single", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("d1", "t_d1", 1, "cc_diverse", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("d2", "t_d2", 1, "cc_diverse", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("d3", "t_d3", 1, "cc_diverse", ClaimType.PROBLEM, evidence_by_id)

    resolved = _resolve_categories(claims, [cat_diverse, cat_single])
    aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
    inputs = _build_report_inputs(aggregated)

    by_label = {entry["aspect"]: entry for entry in inputs.top_pain_points}
    assert by_label["diverse issue"]["weighted_count"] == by_label["single-thread issue"]["weighted_count"] == 3
    assert [entry["aspect"] for entry in inputs.top_pain_points] == ["diverse issue", "single-thread issue"]


def test_tiebreak_uses_raw_count_when_weighted_and_thread_count_tie() -> None:
    evidence_by_id: dict[str, Evidence] = {}
    cat_big = make_category("cc_big", "big raw count")
    cat_small = make_category("cc_small", "small raw count")

    claims: list[Claim] = []
    # Both: weighted = min(x,3) + min(1,3), thread_count=2 -- x=10 vs x=3
    # give the SAME weighted total (4) despite very different raw counts.
    claims += _make_thread_claims("big1", "t_big1", 10, "cc_big", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("big2", "t_big2", 1, "cc_big", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("small1", "t_small1", 3, "cc_small", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("small2", "t_small2", 1, "cc_small", ClaimType.PROBLEM, evidence_by_id)

    resolved = _resolve_categories(claims, [cat_big, cat_small])
    aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
    inputs = _build_report_inputs(aggregated)

    by_label = {entry["aspect"]: entry for entry in inputs.top_pain_points}
    assert by_label["big raw count"]["weighted_count"] == by_label["small raw count"]["weighted_count"] == 4
    assert by_label["big raw count"]["thread_count"] == by_label["small raw count"]["thread_count"] == 2
    assert [entry["aspect"] for entry in inputs.top_pain_points] == ["big raw count", "small raw count"]  # 11 > 4


def test_tiebreak_falls_back_to_label_ascending_when_everything_else_ties() -> None:
    evidence_by_id: dict[str, Evidence] = {}
    cat_z = make_category("cc_z", "zzz issue")
    cat_a = make_category("cc_a2", "aaa issue")

    claims: list[Claim] = []
    claims += _make_thread_claims("z", "t_z", 3, "cc_z", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("a", "t_a", 3, "cc_a2", ClaimType.PROBLEM, evidence_by_id)

    resolved = _resolve_categories(claims, [cat_z, cat_a])
    aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
    inputs = _build_report_inputs(aggregated)

    assert [entry["aspect"] for entry in inputs.top_pain_points] == ["aaa issue", "zzz issue"]


def test_dampening_applies_uniformly_to_all_four_always_surfaced_sections() -> None:
    """feature_requests/praised_aspects/competitor_mentions get the same
    treatment as top_pain_points -- not special-cased to pain points only."""
    for claim_type, section_name in [
        (ClaimType.FEATURE_REQUEST, "feature_requests"),
        (ClaimType.PRAISE, "praised_aspects"),
        (ClaimType.COMPARISON, "competitor_mentions"),
    ]:
        evidence_by_id: dict[str, Evidence] = {}
        cat_diverse = make_category("cc_diverse", "diverse issue")
        cat_single = make_category("cc_single", "single-thread issue")
        claims: list[Claim] = []
        claims += _make_thread_claims("single", "t_single", 9, "cc_single", claim_type, evidence_by_id)
        claims += _make_thread_claims("d1", "t_d1", 1, "cc_diverse", claim_type, evidence_by_id)
        claims += _make_thread_claims("d2", "t_d2", 1, "cc_diverse", claim_type, evidence_by_id)
        claims += _make_thread_claims("d3", "t_d3", 1, "cc_diverse", claim_type, evidence_by_id)

        resolved = _resolve_categories(claims, [cat_diverse, cat_single])
        aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
        inputs = _build_report_inputs(aggregated)

        entries = getattr(inputs, section_name)
        assert [entry["aspect"] for entry in entries] == ["diverse issue", "single-thread issue"], section_name


def test_shipping_and_seller_service_sections_are_not_dampened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Milestone 3 explicitly excludes the two threshold-gated sections from
    dampened sorting -- their internal order stays plain raw-count
    descending, unchanged from before this milestone."""
    monkeypatch.setenv("SHIPPING_SERVICE_MIN_CLAIMS", "1")
    monkeypatch.setenv("SHIPPING_SERVICE_MIN_EVIDENCE", "1")
    monkeypatch.setenv("SHIPPING_SERVICE_MIN_THREADS", "1")

    big_single_thread = make_group(
        ClaimType.SHIPPING_ISSUE, category_key="cc_big", count=9, evidence_count=9, thread_count=1,
        thread_counts={"t1": 9},
    )
    small_multi_thread = make_group(
        ClaimType.SHIPPING_ISSUE, category_key="cc_small", count=3, evidence_count=3, thread_count=3,
        thread_counts={"t1": 1, "t2": 1, "t3": 1},
    )

    inputs = _build_report_inputs(
        {
            (ClaimType.SHIPPING_ISSUE, "cc_big"): big_single_thread,
            (ClaimType.SHIPPING_ISSUE, "cc_small"): small_multi_thread,
        }
    )

    # If this section were dampened, the 1-thread/9-claim group would drop
    # behind the 3-thread/3-claim one -- it must NOT, per the approved scope.
    assert [entry["count"] for entry in inputs.shipping_issues] == [9, 3]


def test_raw_count_field_is_never_altered_by_dampening() -> None:
    evidence_by_id: dict[str, Evidence] = {}
    category = make_category("cc_floor", "floor damage")
    claims: list[Claim] = []
    claims += _make_thread_claims("a", "t_a", 10, "cc_floor", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("b", "t_b", 1, "cc_floor", ClaimType.PROBLEM, evidence_by_id)

    resolved = _resolve_categories(claims, [category])
    aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
    inputs = _build_report_inputs(aggregated)

    assert inputs.top_pain_points[0]["count"] == 11  # the true, uncapped total -- never the dampened 4


def test_non_reddit_source_dampening_is_a_no_op_for_the_common_one_claim_per_evidence_case() -> None:
    """Milestone 3 requirement: non-Reddit sources (thread_key degenerates to
    evidence_id) stay behaviorally unchanged in the common case -- every
    'thread' has exactly 1 claim, so no thread can ever exceed the cap and
    weighted_count == raw count always."""
    evidence_by_id = {
        "ev_1": make_evidence("ev_1", source_url="https://amazon.com/reviews/r1"),
        "ev_2": make_evidence("ev_2", source_url="https://amazon.com/reviews/r2"),
        "ev_3": make_evidence("ev_3", source_url="https://amazon.com/reviews/r3"),
    }
    category = make_category("cc_a", "battery life")
    claims = [make_claim(f"cl_{i}", evidence_id=f"ev_{i}", canonical_category="cc_a") for i in (1, 2, 3)]
    resolved = _resolve_categories(claims, [category])
    aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
    [group] = list(aggregated.values())

    assert group.thread_count == group.count == 3
    assert _weighted_count(group.thread_counts, cap=3) == group.count


def test_non_reddit_source_can_still_dampen_when_multiple_claims_share_one_evidence_item() -> None:
    """Named, accepted edge case: if several claims are extracted from the
    SAME non-Reddit evidence item (one review yielding multiple claims in
    the same category), that evidence_id is one 'thread' with >1 claim, and
    dampening CAN apply -- intentional, not a bug. _thread_key()'s non-Reddit
    fallback is evidence_id, and the formula is defined in terms of claims
    per thread, not per source, so this follows the same rule Reddit does."""
    evidence_by_id = {"ev_1": make_evidence("ev_1", source_url="https://amazon.com/reviews/r1")}
    category = make_category("cc_a", "battery life")
    claims = [make_claim(f"cl_{i}", evidence_id="ev_1", canonical_category="cc_a") for i in range(5)]
    resolved = _resolve_categories(claims, [category])
    aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
    [group] = list(aggregated.values())

    assert group.thread_count == 1
    assert _weighted_count(group.thread_counts, cap=3) == 3


def test_summarize_end_to_end_reflects_dampened_order() -> None:
    """Confirms the dampened order actually reaches the final Report object
    through the full summarize() call, not just _build_report_inputs() in
    isolation."""
    evidence_by_id: dict[str, Evidence] = {}
    floor = make_category("cc_floor", "floor damage")
    app_pos = make_category("cc_app", "app real time position update")

    claims: list[Claim] = []
    claims += _make_thread_claims("floor_a", "t_floor_a", 10, "cc_floor", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("floor_b", "t_floor_b", 1, "cc_floor", ClaimType.PROBLEM, evidence_by_id)
    claims += _make_thread_claims("app", "t_app", 9, "cc_app", ClaimType.PROBLEM, evidence_by_id)

    report = summarize("run_1", "robot vacuum cleaners", list(evidence_by_id.values()), claims, [floor, app_pos], no_llm())

    assert [entry["aspect"] for entry in report.top_pain_points] == ["floor damage", "app real time position update"]
    assert report.top_pain_points[0]["count"] == 11
    assert report.top_pain_points[1]["count"] == 9


# ---------------------------------------------------------------------------
# Legacy Evidence path -- unchanged, wrapped into the same ReportInputs shape
# ---------------------------------------------------------------------------


def test_build_report_inputs_from_evidence_matches_the_untouched_aggregate_by_aspect() -> None:
    evidence = [
        Evidence(
            evidence_id="ev_1", run_id="run_1", iteration=1, source_url="https://reddit.com/x", subreddit="dogfood",
            item_type="post", title="t", body="b", score=5, comment_count=0,
            created_at="2026-01-01T00:00:00+00:00", fetched_at="2026-01-01T00:00:00+00:00", search_query="q",
            insight_type=InsightType.PAIN_POINT, aspect="battery", sentiment=Sentiment.NEGATIVE,
            quote="battery is bad", confidence=0.7,
        )
    ]

    inputs = _build_report_inputs_from_evidence(evidence)

    assert inputs.top_pain_points == _aggregate_by_aspect(evidence, InsightType.PAIN_POINT)
    assert inputs.feature_requests == _aggregate_by_aspect(evidence, InsightType.FEATURE_REQUEST)
    assert inputs.praised_aspects == _aggregate_by_aspect(evidence, InsightType.PRAISE)
    assert inputs.competitor_mentions == _aggregate_by_aspect(evidence, InsightType.COMPARISON)
    assert inputs.shipping_issues == []
    assert inputs.seller_service_issues == []


def test_legacy_dicts_have_no_category_status_key_claims_dicts_do() -> None:
    """A known, intentional interface difference at the per-entry level (the
    ReportInputs CONTAINER shape is identical either way, but legacy dict
    entries have no concept of category review status)."""
    legacy_entry = _aggregate_by_aspect(
        [
            Evidence(
                evidence_id="ev_1", run_id="run_1", iteration=1, source_url="https://reddit.com/x", subreddit="dogfood",
                item_type="post", title="t", body="b", score=5, comment_count=0,
                created_at="2026-01-01T00:00:00+00:00", fetched_at="2026-01-01T00:00:00+00:00", search_query="q",
                insight_type=InsightType.PAIN_POINT, aspect="battery", sentiment=Sentiment.NEGATIVE,
                quote="battery is bad", confidence=0.7,
            )
        ],
        InsightType.PAIN_POINT,
    )[0]
    assert "category_status" not in legacy_entry

    category = make_category("cc_a")
    claim = make_claim("cl_1", canonical_category="cc_a")
    resolved = _resolve_categories([claim], [category])
    aggregated = _aggregate_claims_by_category([claim], resolved, {"ev_1": make_evidence("ev_1")})
    claims_inputs = _build_report_inputs(aggregated)
    assert "category_status" in claims_inputs.top_pain_points[0]


# ---------------------------------------------------------------------------
# ReportInputs interface identical between both branches
# ---------------------------------------------------------------------------


def test_both_branches_return_the_same_reportinputs_field_shape() -> None:
    claims_inputs = _build_report_inputs({})
    legacy_inputs = _build_report_inputs_from_evidence([])

    assert isinstance(claims_inputs, ReportInputs)
    assert isinstance(legacy_inputs, ReportInputs)
    claims_field_names = {f.name for f in fields(claims_inputs)}
    legacy_field_names = {f.name for f in fields(legacy_inputs)}
    assert claims_field_names == legacy_field_names == {
        "top_pain_points", "feature_requests", "praised_aspects",
        "competitor_mentions", "shipping_issues", "seller_service_issues",
    }


# ---------------------------------------------------------------------------
# summarize() -- branch selection
# ---------------------------------------------------------------------------


def test_summarize_uses_legacy_path_when_claims_is_empty() -> None:
    evidence = [
        Evidence(
            evidence_id="ev_1", run_id="run_1", iteration=1, source_url="https://reddit.com/x", subreddit="dogfood",
            item_type="post", title="t", body="b", score=5, comment_count=0,
            created_at="2026-01-01T00:00:00+00:00", fetched_at="2026-01-01T00:00:00+00:00", search_query="q",
            insight_type=InsightType.PAIN_POINT, aspect="battery", sentiment=Sentiment.NEGATIVE,
            quote="battery is bad", confidence=0.7,
        )
    ]

    report = summarize("run_1", "dog food", evidence, [], [], no_llm())

    assert report.top_pain_points == _aggregate_by_aspect(evidence, InsightType.PAIN_POINT)
    assert "category_status" not in report.top_pain_points[0]  # legacy shape, no taxonomy involved


def test_summarize_uses_claims_path_when_claims_is_non_empty() -> None:
    category = make_category("cc_a", canonical_label="battery life")
    claim = make_claim("cl_1", evidence_id="ev_1", canonical_category="cc_a")
    evidence = [make_evidence("ev_1")]

    report = summarize("run_1", "dog food", evidence, [claim], [category], no_llm())

    assert len(report.top_pain_points) == 1
    assert report.top_pain_points[0]["aspect"] == "battery life"
    assert report.top_pain_points[0]["category_status"] == "approved"  # Claims-path shape


# ---------------------------------------------------------------------------
# Stage 7 -- report_source / fallback_reason: passed through, never recomputed
# ---------------------------------------------------------------------------


def test_summarize_sets_report_source_claims_and_forces_fallback_reason_none() -> None:
    category = make_category("cc_a")
    claim = make_claim("cl_1", evidence_id="ev_1", canonical_category="cc_a")
    evidence = [make_evidence("ev_1")]

    # A caller bug would never legitimately do this (claims populated AND a
    # fallback_reason passed at the same time -- the real caller only ever
    # combines them the other way around), but summarize() must not "look
    # at" fallback_reason to decide anything -- only at whether claims is
    # non-empty -- and must not let a stray non-None value leak through onto
    # a report_source="claims" Report.
    report = summarize("run_1", "dog food", evidence, [claim], [category], no_llm(), fallback_reason="no_claims")

    assert report.report_source == "claims"
    assert report.fallback_reason is None


def test_summarize_passes_through_the_exact_fallback_reason_string_unchanged() -> None:
    report = summarize("run_1", "dog food", [], [], [], no_llm(), fallback_reason="low_resolved_coverage:0.55")

    assert report.report_source == "legacy_evidence"
    assert report.fallback_reason == "low_resolved_coverage:0.55"  # verbatim, not reformatted or re-derived


def test_summarize_branch_selection_depends_only_on_whether_claims_is_non_empty() -> None:
    """summarize() must not recompute or re-infer eligibility -- passing a
    non-empty `claims` list always takes the Claims path regardless of what
    fallback_reason says, and vice versa. The real eligibility decision
    already happened once, in _claims_report_eligible()."""
    category = make_category("cc_a")
    claim = make_claim("cl_1", evidence_id="ev_1", canonical_category="cc_a")
    evidence = [make_evidence("ev_1")]

    with_claims_and_a_reason = summarize(
        "run_1", "dog food", evidence, [claim], [category], no_llm(), fallback_reason="categorization_incomplete"
    )
    without_claims_and_no_reason = summarize("run_1", "dog food", evidence, [], [], no_llm(), fallback_reason=None)

    assert with_claims_and_a_reason.report_source == "claims"  # claims present -> claims path, reason ignored
    assert without_claims_and_no_reason.report_source == "legacy_evidence"  # claims empty -> legacy path


def test_legacy_report_output_is_behaviorally_unchanged_apart_from_new_metadata_fields() -> None:
    """The actual report CONTENT for the legacy path (aspects/counts/quotes/
    narrative) must be identical to what summarize() always produced -- Stage
    7 only adds new metadata (report_source/fallback_reason) and new, always-
    empty-for-legacy sections (shipping_issues/seller_service_issues), never
    changes existing field values."""
    evidence = [make_evidence("ev_1")]

    report = summarize("run_1", "dog food", evidence, [], [], no_llm())

    assert report.top_pain_points == _aggregate_by_aspect(evidence, InsightType.PAIN_POINT)
    assert report.feature_requests == _aggregate_by_aspect(evidence, InsightType.FEATURE_REQUEST)
    assert report.praised_aspects == _aggregate_by_aspect(evidence, InsightType.PRAISE)
    assert report.competitor_mentions == _aggregate_by_aspect(evidence, InsightType.COMPARISON)
    # New fields, correctly defaulted for the legacy path -- not a content change.
    assert report.shipping_issues == []
    assert report.seller_service_issues == []
    assert report.report_source == "legacy_evidence"


# ---------------------------------------------------------------------------
# Phase 4, P0 -- summarize() must not call the LLM when top_pain_points,
# feature_requests, and praised_aspects are all empty (docs/phase4_merchant_
# decision_support_plan.md). Confirmed against two real reports
# (run_89b02b9b1e3e, run_66b65bc32dc8) where the LLM fabricated a full
# narrative from zero collected evidence. An available()-True LLM that fails
# the test if called is used throughout, so these tests fail loudly (not
# silently pass) if the guard regresses.
# ---------------------------------------------------------------------------


def test_summarize_skips_llm_when_legacy_evidence_is_empty() -> None:
    llm = _AssertNotCalledLLM()

    report = summarize("run_1", "dog food", [], [], [], llm)

    # Must match _summarize_fallback()'s own empty-input output exactly --
    # P0 reuses that function rather than inventing new empty-state copy.
    expected = _summarize_fallback("dog food", [], [])
    assert report.recommended_actions == expected["recommended_actions"]
    assert report.recommended_actions_zh == expected["recommended_actions_zh"]
    assert report.summary_markdown == expected["summary_markdown"]
    assert report.summary_markdown_zh == expected["summary_markdown_zh"]


def test_summarize_skips_llm_when_claims_resolve_to_no_reportable_sections() -> None:
    # QUESTION is one of the claim types that deliberately never reaches the
    # report (see _CLAIM_TYPE_SECTION) -- claims is non-empty (Claims path is
    # taken) but every reportable section still ends up empty.
    category = make_category("cc_a")
    claim = make_claim("cl_1", evidence_id="ev_1", claim_type=ClaimType.QUESTION, canonical_category="cc_a")
    evidence = [make_evidence("ev_1")]
    llm = _AssertNotCalledLLM()

    report = summarize("run_1", "dog food", evidence, [claim], [category], llm)

    assert report.report_source == "claims"
    assert report.top_pain_points == []
    assert report.feature_requests == []
    assert report.praised_aspects == []
    expected = _summarize_fallback("dog food", [], [])
    assert report.recommended_actions == expected["recommended_actions"]


def test_summarize_still_calls_llm_when_pain_points_are_non_empty() -> None:
    """Regression check: existing non-empty runs must behave identically --
    the LLM path must still run exactly as before when there IS content."""
    evidence = [make_evidence("ev_1")]
    llm = _CannedLLM(_CANNED_LLM_RESPONSE)

    report = summarize("run_1", "dog food", evidence, [], [], llm)

    assert llm.calls == 1
    assert report.recommended_actions == ["do the thing"]
    assert report.summary_markdown == "# summary"


def test_summarize_still_calls_llm_when_only_praised_aspects_are_non_empty() -> None:
    """Boundary case: pain_points and feature_requests are empty but
    praised_aspects is not -- the guard checks all three, so this must NOT
    be treated as "no report content"."""
    evidence = [
        Evidence(
            evidence_id="ev_1", run_id="run_1", iteration=1, source_url="https://reddit.com/x", subreddit="dogfood",
            item_type="post", title="t", body="b", score=5, comment_count=0,
            created_at="2026-01-01T00:00:00+00:00", fetched_at="2026-01-01T00:00:00+00:00", search_query="q",
            insight_type=InsightType.PRAISE, aspect="taste", sentiment=Sentiment.POSITIVE,
            quote="my dog loves it", confidence=0.9,
        )
    ]
    llm = _CannedLLM(_CANNED_LLM_RESPONSE)

    report = summarize("run_1", "dog food", evidence, [], [], llm)

    assert llm.calls == 1
    assert report.praised_aspects != []
    assert report.top_pain_points == []
    assert report.feature_requests == []
    assert report.recommended_actions == ["do the thing"]


def test_summarize_still_falls_back_on_llm_exception_when_content_is_non_empty() -> None:
    """Regression check: the existing try/except around _summarize_llm() --
    unrelated to P0 -- must still catch a real LLM failure and fall back,
    exactly as before."""

    class _RaisingLLM:
        def available(self) -> bool:
            return True

        def json_chat(self, model: str, system: str, user: str) -> dict:
            raise RuntimeError("simulated LLM failure")

    evidence = [make_evidence("ev_1")]

    report = summarize("run_1", "dog food", evidence, [], [], _RaisingLLM())

    pain_points = _aggregate_by_aspect(evidence, InsightType.PAIN_POINT)
    expected = _summarize_fallback("dog food", pain_points, [])
    assert report.recommended_actions == expected["recommended_actions"]


# ---------------------------------------------------------------------------
# Phase 4, P1 -- _summarize_llm()'s prompt must contain enforceable grounding
# rules (docs/phase4_merchant_decision_support_plan.md). Verified against a
# _CapturingLLM that records the exact system/user text sent, rather than
# calling a real model -- these tests check the instructions are present, not
# that a live LLM obeys them (that's the deferred real-run validation step).
# ---------------------------------------------------------------------------


def _call_summarize_llm_and_capture() -> tuple[str, str]:
    """Invokes _summarize_llm() with one of each supplied section and
    returns (system, user) exactly as sent to the LLM client."""
    pain_points = [{"aspect": "floor damage", "count": 10, "subreddit_count": 1, "avg_confidence": 0.8,
                     "sentiment_counts": {"negative": 10}, "example_quotes": []}]
    feature_requests = [{"aspect": "self emptying", "count": 2, "subreddit_count": 1, "avg_confidence": 0.7,
                          "sentiment_counts": {"negative": 2}, "example_quotes": []}]
    praised = [{"aspect": "reliability", "count": 6, "subreddit_count": 1, "avg_confidence": 0.9,
                "sentiment_counts": {"positive": 6}, "example_quotes": []}]
    llm = _CapturingLLM(_CANNED_LLM_RESPONSE)

    _summarize_llm("robot vacuum cleaners", [], pain_points, feature_requests, praised, llm)

    assert len(llm.calls) == 1
    _model, system, user = llm.calls[0]
    return system, user


def test_prompt_forbids_general_product_knowledge_and_requires_supplied_evidence_only() -> None:
    system, _user = _call_summarize_llm_and_capture()
    lowered = system.lower()

    assert "only" in lowered
    assert "general knowledge" in lowered
    assert "not directly supported" in lowered or "not directly present" in lowered or "supported by the supplied" in lowered


def test_prompt_forbids_pricing_roi_cost_and_market_strategy() -> None:
    system, _user = _call_summarize_llm_and_capture()
    lowered = system.lower()

    for phrase in [
        "return on investment",
        "pricing strategy",
        "cost",
        "market or business strategy",
        "product positioning",
        "competitor's strategy",
    ]:
        assert phrase in lowered, f"expected prompt to forbid {phrase!r}"


def test_prompt_forbids_invented_quantitative_specs_as_a_general_rule_not_a_fixed_list() -> None:
    system, _user = _call_summarize_llm_and_capture()
    lowered = system.lower()

    # The general rule must be stated (measurement/percentage/duration/etc as a category)...
    for phrase in ["measurement", "percentage", "duration", "capacity", "frequency", "threshold", "target value"]:
        assert phrase in lowered, f"expected prompt to name {phrase!r} as a prohibited category"
    # ...and explicitly framed as non-exhaustive, not "only these examples are banned".
    assert "not only the categories" in lowered or "general rule" in lowered
    # The illustrative examples may appear, but only as examples of the general rule.
    assert "5200 mah" in lowered
    assert "5-10 hz" in lowered


def test_prompt_requires_every_recommendation_to_name_a_supplied_aggregate_label() -> None:
    system, _user = _call_summarize_llm_and_capture()
    lowered = system.lower()

    assert "must explicitly name the exact" in lowered
    assert "aspect" in lowered
    assert "never invent a new label" in lowered


def test_prompt_requires_conservative_style_with_acceptable_and_unacceptable_examples() -> None:
    system, _user = _call_summarize_llm_and_capture()

    assert "Acceptable style:" in system
    assert "Unacceptable style:" in system
    assert "consider prioritizing" in system.lower()


def test_prompt_does_not_add_an_empty_input_instruction_p0_remains_sole_guard() -> None:
    """P1 must not duplicate P0's job -- no 'if there is no evidence' style
    instruction belongs in this prompt, since summarize() never reaches
    _summarize_llm() at all when there's nothing to summarize (P0)."""
    system, _user = _call_summarize_llm_and_capture()
    lowered = system.lower()

    assert "no evidence" not in lowered
    assert "no data" not in lowered


def test_prompt_output_schema_is_unchanged_from_pre_p1() -> None:
    """P1 is prompt-text only -- the expected_json shape sent to the model,
    and the dict _summarize_llm() returns, must have exactly the same keys
    as before (no structured {text, grounded_in} objects, no new fields)."""
    _system, user = _call_summarize_llm_and_capture()
    parsed_user = json.loads(user)

    assert set(parsed_user["expected_json"].keys()) == {
        "recommended_actions_en", "recommended_actions_zh", "summary_markdown_en", "summary_markdown_zh",
    }
    assert isinstance(parsed_user["expected_json"]["recommended_actions_en"], list)
    assert isinstance(parsed_user["expected_json"]["recommended_actions_zh"], list)

    result = _summarize_llm(
        "robot vacuum cleaners", [],
        [{"aspect": "floor damage", "count": 1, "subreddit_count": 1, "avg_confidence": 0.8,
          "sentiment_counts": {}, "example_quotes": []}],
        [], [], _CapturingLLM(_CANNED_LLM_RESPONSE),
    )
    assert set(result.keys()) == {"recommended_actions", "recommended_actions_zh", "summary_markdown", "summary_markdown_zh"}
    assert isinstance(result["recommended_actions"], list)
