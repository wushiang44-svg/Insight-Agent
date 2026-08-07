from __future__ import annotations

import json

import pytest

from app.models import DataSource, Evidence, InsightType, Sentiment
from app.pipeline.source_profiles import get_source_profile
from app.react_agent import (
    _check_sufficiency_llm,
    _plan_next_query_llm,
    _sanitize_planner_subreddit,
    check_sufficiency,
    plan_next_query,
)

# ---------------------------------------------------------------------------
# Milestone 4 / D2 -- source-aware search planning. Covers every DataSource
# value for both plan_next_query/_plan_next_query_llm and
# check_sufficiency/_check_sufficiency_llm, per the approved plan's
# validation requirements.
# ---------------------------------------------------------------------------

ALL_DATA_SOURCES = list(DataSource)
REDDIT_FAMILY = (DataSource.REDDIT, DataSource.REDDIT_API, DataSource.REDDIT_SCRAPER)
NON_REDDIT_SOURCES = tuple(source for source in ALL_DATA_SOURCES if source not in REDDIT_FAMILY)


class _CapturingLLM:
    """Duck-types the DeepSeekClient interface used by plan_next_query()/
    check_sufficiency(): available() is True, json_chat() records every
    (model, system, user) call it receives -- so tests can assert on the
    exact prompt text the planner/sufficiency functions build -- and returns
    a canned response. Same pattern as test_report_generation.py's
    _CapturingLLM."""

    def __init__(self, response: dict[str, object]):
        self._response = response
        self.calls: list[tuple[str, str, str]] = []

    def available(self) -> bool:
        return True

    def json_chat(self, model: str, system: str, user: str) -> dict[str, object]:
        self.calls.append((model, system, user))
        return self._response


def make_evidence(evidence_id: str = "ev_1", subreddit: str = "gadgets", aspect: str = "battery") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        run_id="run_1",
        iteration=1,
        source_url=f"https://example.com/{evidence_id}",
        subreddit=subreddit,
        item_type="post",
        title="title",
        body="body",
        score=5,
        comment_count=0,
        created_at="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:00+00:00",
        search_query="query",
        insight_type=InsightType.PAIN_POINT,
        aspect=aspect,
        sentiment=Sentiment.NEGATIVE,
        quote="quote",
        confidence=0.6,
    )


# ---------------------------------------------------------------------------
# plan_next_query / _plan_next_query_llm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data_source", ALL_DATA_SOURCES)
def test_planner_output_json_contract_is_unchanged_for_every_source(data_source: DataSource) -> None:
    """The LLM JSON response shape must remain exactly {query, subreddit,
    reasoning} regardless of data_source -- Decision 4."""
    llm = _CapturingLLM({"query": "some query", "subreddit": "", "reasoning": "why"})
    result = _plan_next_query_llm("widgets", ["widget"], [], [], [], 1, [], llm, data_source)
    assert set(result.keys()) == {"query", "subreddit", "reasoning"}


@pytest.mark.parametrize("data_source", NON_REDDIT_SOURCES)
def test_non_reddit_planner_prompt_never_mentions_reddit(data_source: DataSource) -> None:
    llm = _CapturingLLM({"query": "some query", "subreddit": "", "reasoning": "why"})
    _plan_next_query_llm("widgets", ["widget"], [], [], [], 1, [], llm, data_source)
    system, user = llm.calls[0][1], llm.calls[0][2]
    payload = json.loads(user)
    # Case-sensitive "Reddit" (the proper noun), not the case-insensitive
    # substring "reddit" -- the retained field name "subreddit" (Decision 4:
    # never rename it) and the generic word "subreddit" used to explain its
    # ABSENCE for these sources both legitimately contain "reddit" as a
    # substring ("sub" + "reddit"). What must never appear is the platform
    # name itself.
    assert "Reddit" not in system
    expected_json = payload["expected_json"]
    assert "Reddit" not in expected_json["query"]
    assert "Reddit" not in expected_json["subreddit"]
    assert "Reddit" not in expected_json["reasoning"]


def test_amazon_planner_prompt_uses_amazon_noun_and_never_requests_a_subreddit() -> None:
    llm = _CapturingLLM({"query": "some query", "subreddit": "", "reasoning": "why"})
    _plan_next_query_llm("widgets", ["widget"], [], [], [], 1, [], llm, DataSource.AMAZON)
    system, user = llm.calls[0][1], llm.calls[0][2]
    assert "Amazon" in system
    assert "leave this empty" in user
    assert "subreddit or category concept to narrow by" in user


def test_youtube_planner_prompt_uses_youtube_noun_and_never_requests_a_subreddit() -> None:
    llm = _CapturingLLM({"query": "some query", "subreddit": "", "reasoning": "why"})
    _plan_next_query_llm("widgets", ["widget"], [], [], [], 1, [], llm, DataSource.YOUTUBE)
    system, user = llm.calls[0][1], llm.calls[0][2]
    assert "YouTube" in system
    assert "leave this empty" in user
    assert "subreddit or category concept to narrow by" in user


@pytest.mark.parametrize("data_source", (DataSource.AMAZON, DataSource.YOUTUBE))
def test_amazon_and_youtube_sanitize_an_invented_subreddit_back_to_empty(data_source: DataSource) -> None:
    """Even if the LLM disobeys the prompt and invents a group value, the
    result must still be empty -- a deterministic guard, not just prompt
    wording (Decision 3: "do not request or invent a subreddit/group
    filter"). The collector ignores this value anyway for these sources, but
    the contract itself must stay clean."""
    llm = _CapturingLLM({"query": "some query", "subreddit": "r/amazonreviews", "reasoning": "why"})
    result = _plan_next_query_llm("widgets", ["widget"], [], [], [], 1, [], llm, data_source)
    assert result["subreddit"] == ""


def test_reddit_planner_prompt_preserves_all_existing_instructions() -> None:
    """Decision 5: not byte-for-byte, but every existing Reddit-specific
    planning instruction must survive semantically."""
    llm = _CapturingLLM({"query": "some query", "subreddit": "gadgets", "reasoning": "why"})
    _plan_next_query_llm("widgets", ["widget"], ["gadgets"], [], [], 1, [], llm, DataSource.REDDIT)
    system, user = llm.calls[0][1], llm.calls[0][2]
    assert "Reddit" in system
    assert "search-planning agent" in system
    assert "Avoid repeating previous queries" in system
    assert "If missing aspects are given, target them" in system
    assert "Write the reasoning field in English" in system
    assert "Return only JSON" in system
    assert '"target_subreddits": ["gadgets"]' in user


def test_reddit_subreddit_field_guidance_text_is_byte_identical_to_pre_milestone_4() -> None:
    """The one piece of prompt text with zero reason to change for Reddit --
    locking it byte-for-byte removes any risk of accidentally drifting
    Reddit's own, already-validated behavior."""
    llm = _CapturingLLM({"query": "some query", "subreddit": "", "reasoning": "why"})
    _plan_next_query_llm("widgets", [], [], [], [], 1, [], llm, DataSource.REDDIT)
    _, user = llm.calls[0][1], llm.calls[0][2]
    assert "optional subreddit without r/ prefix, empty string to search all of Reddit" in user


@pytest.mark.parametrize("data_source", (DataSource.REDDIT_API, DataSource.REDDIT_SCRAPER))
def test_legacy_reddit_sources_get_the_same_planning_behavior_as_reddit(data_source: DataSource) -> None:
    llm = _CapturingLLM({"query": "some query", "subreddit": "gadgets", "reasoning": "why"})
    result = _plan_next_query_llm("widgets", [], [], [], [], 1, [], llm, data_source)
    system, user = llm.calls[0][1], llm.calls[0][2]
    assert "Reddit" in system
    assert "optional subreddit without r/ prefix" in user
    # Legacy sources keep Reddit's unrestricted ("always") grouping behavior --
    # any value the LLM proposes survives sanitization.
    assert result["subreddit"] == "gadgets"


def test_reddit_planner_allows_any_llm_proposed_subreddit_unrestricted() -> None:
    """Reddit's grouping_mode is "always" -- unlike JSON upload, the LLM is
    free to propose a subreddit outside target_subreddits (unchanged from
    pre-Milestone-4 behavior)."""
    llm = _CapturingLLM({"query": "some query", "subreddit": "a_subreddit_never_listed", "reasoning": "why"})
    result = _plan_next_query_llm("widgets", [], ["only_this_one"], [], [], 1, [], llm, DataSource.REDDIT)
    assert result["subreddit"] == "a_subreddit_never_listed"


def test_json_upload_planner_offers_narrowing_when_target_groups_are_supplied() -> None:
    llm = _CapturingLLM({"query": "some query", "subreddit": "electronics", "reasoning": "why"})
    result = _plan_next_query_llm("widgets", [], ["electronics", "home"], [], [], 1, [], llm, DataSource.JSON_UPLOAD)
    system, user = llm.calls[0][1], llm.calls[0][2]
    assert "Reddit" not in system
    assert "electronics, home" in user
    assert result["subreddit"] == "electronics"


def test_json_upload_planner_leaves_subreddit_empty_and_does_not_invent_one_without_target_groups() -> None:
    llm = _CapturingLLM({"query": "some query", "subreddit": "an_invented_category", "reasoning": "why"})
    result = _plan_next_query_llm("widgets", [], [], [], [], 1, [], llm, DataSource.JSON_UPLOAD)
    _, user = llm.calls[0][1], llm.calls[0][2]
    assert "do not invent one" in user
    # Even though the LLM disobeyed and invented a category, the result must
    # still come back empty -- json_upload.py's collector does an exact match
    # and would otherwise silently return zero items for a hallucinated value.
    assert result["subreddit"] == ""


def test_json_upload_planner_rejects_an_llm_proposed_category_outside_the_supplied_list() -> None:
    llm = _CapturingLLM({"query": "some query", "subreddit": "not_a_real_category", "reasoning": "why"})
    result = _plan_next_query_llm("widgets", [], ["electronics"], [], [], 1, [], llm, DataSource.JSON_UPLOAD)
    assert result["subreddit"] == ""


def test_json_upload_planner_accepts_a_supplied_category_case_insensitively() -> None:
    """json_upload.py's own collector match is case-insensitive
    (item.subreddit.strip().lower() == target) -- the sanitizer must accept
    whatever casing the LLM returns as long as it matches one of the
    supplied options case-insensitively."""
    llm = _CapturingLLM({"query": "some query", "subreddit": "ELECTRONICS", "reasoning": "why"})
    result = _plan_next_query_llm("widgets", [], ["electronics"], [], [], 1, [], llm, DataSource.JSON_UPLOAD)
    assert result["subreddit"] == "ELECTRONICS"


def test_sanitize_planner_subreddit_matches_the_three_grouping_modes_directly() -> None:
    reddit_profile = get_source_profile(DataSource.REDDIT)
    amazon_profile = get_source_profile(DataSource.AMAZON)
    upload_profile = get_source_profile(DataSource.JSON_UPLOAD)
    assert _sanitize_planner_subreddit("anything", reddit_profile, []) == "anything"
    assert _sanitize_planner_subreddit("anything", amazon_profile, []) == ""
    assert _sanitize_planner_subreddit("catA", upload_profile, ["catA"]) == "catA"
    assert _sanitize_planner_subreddit("catB", upload_profile, ["catA"]) == ""
    assert _sanitize_planner_subreddit("", upload_profile, ["catA"]) == ""


def test_plan_next_query_dispatcher_threads_data_source_into_the_llm_path() -> None:
    """Confirms the public dispatcher (the function run_react_loop actually
    calls) forwards data_source through to _plan_next_query_llm, end to end."""
    llm = _CapturingLLM({"query": "some query", "subreddit": "", "reasoning": "why"})
    plan_next_query("widgets", [], [], [], [], 1, [], llm, DataSource.AMAZON)
    system = llm.calls[0][1]
    assert "Amazon" in system
    assert "Reddit" not in system


# ---------------------------------------------------------------------------
# check_sufficiency / _check_sufficiency_llm
# ---------------------------------------------------------------------------

_SUFFICIENCY_RESPONSE = {"sufficient": False, "reason": "keep going", "missing_aspects": ["battery"]}


@pytest.mark.parametrize("data_source", ALL_DATA_SOURCES)
def test_sufficiency_output_json_contract_is_unchanged_for_every_source(data_source: DataSource) -> None:
    llm = _CapturingLLM(_SUFFICIENCY_RESPONSE)
    collected = [make_evidence("ev_1"), make_evidence("ev_2")]
    result = _check_sufficiency_llm("widgets", collected, 2, 6, 1, llm, data_source)
    assert set(result.keys()) == {"sufficient", "reason", "missing_aspects"}


@pytest.mark.parametrize("data_source", NON_REDDIT_SOURCES)
def test_non_reddit_sufficiency_prompt_never_mentions_reddit(data_source: DataSource) -> None:
    llm = _CapturingLLM(_SUFFICIENCY_RESPONSE)
    collected = [make_evidence("ev_1")]
    _check_sufficiency_llm("widgets", collected, 2, 6, 1, llm, data_source)
    system = llm.calls[0][1]
    assert "reddit" not in system.lower()


def test_reddit_sufficiency_prompt_preserves_subreddit_diversity_wording() -> None:
    llm = _CapturingLLM(_SUFFICIENCY_RESPONSE)
    collected = [make_evidence("ev_1")]
    _check_sufficiency_llm("widgets", collected, 2, 6, 1, llm, DataSource.REDDIT)
    system = llm.calls[0][1]
    assert "Reddit" in system
    assert "subreddit diversity" in system
    assert "aspect coverage" in system
    assert "Return only JSON" in system


@pytest.mark.parametrize("data_source", (DataSource.REDDIT_API, DataSource.REDDIT_SCRAPER))
def test_legacy_reddit_sources_get_the_same_sufficiency_behavior_as_reddit(data_source: DataSource) -> None:
    llm = _CapturingLLM(_SUFFICIENCY_RESPONSE)
    collected = [make_evidence("ev_1")]
    _check_sufficiency_llm("widgets", collected, 2, 6, 1, llm, data_source)
    system = llm.calls[0][1]
    assert "subreddit diversity" in system


def test_amazon_sufficiency_prompt_uses_product_diversity_not_subreddit() -> None:
    """Prompt-quality follow-up: "source diversity" was too generic given a
    more precise, already-known concept exists -- AmazonCollector groups
    CollectedItem.subreddit by product title, so the real diversity
    dimension is "how many distinct products", not a vague "source"."""
    llm = _CapturingLLM(_SUFFICIENCY_RESPONSE)
    collected = [make_evidence("ev_1")]
    _check_sufficiency_llm("widgets", collected, 2, 6, 1, llm, DataSource.AMAZON)
    system = llm.calls[0][1]
    assert "Amazon" in system
    assert "product diversity" in system
    assert "subreddit" not in system.lower()


def test_youtube_sufficiency_prompt_uses_video_diversity_not_subreddit() -> None:
    """Same rationale as the Amazon case above -- YoutubeCollector groups
    CollectedItem.subreddit by video title."""
    llm = _CapturingLLM(_SUFFICIENCY_RESPONSE)
    collected = [make_evidence("ev_1")]
    _check_sufficiency_llm("widgets", collected, 2, 6, 1, llm, DataSource.YOUTUBE)
    system = llm.calls[0][1]
    assert "YouTube" in system
    assert "video diversity" in system
    assert "subreddit" not in system.lower()


def test_json_upload_sufficiency_prompt_uses_category_diversity() -> None:
    llm = _CapturingLLM(_SUFFICIENCY_RESPONSE)
    collected = [make_evidence("ev_1")]
    _check_sufficiency_llm("widgets", collected, 2, 6, 1, llm, DataSource.JSON_UPLOAD)
    system = llm.calls[0][1]
    assert "category diversity" in system
    assert "Reddit" not in system


def test_check_sufficiency_dispatcher_threads_data_source_into_the_llm_path() -> None:
    llm = _CapturingLLM(_SUFFICIENCY_RESPONSE)
    # min_evidence_target=1 and evidence_count=1 so the pre-LLM gates in
    # check_sufficiency() don't short-circuit before reaching the LLM path.
    collected = [make_evidence("ev_1")]
    check_sufficiency("widgets", collected, 2, 6, 1, [1, 1], llm, DataSource.YOUTUBE)
    assert len(llm.calls) == 1
    assert "YouTube" in llm.calls[0][1]
