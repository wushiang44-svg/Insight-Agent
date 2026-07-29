from __future__ import annotations

from pathlib import Path

import pytest

from app.collectors.json_upload import JsonUploadCollector
from app.llm import DeepSeekClient
from app.models import Claim, ClaimType, CollectedItem, DataSource, RunStatus, Sentiment
from app.pipeline.taxonomy import CategorizationStats
from app import react_agent as react_agent_module
from app.react_agent import run_react_loop
from app.storage import Storage


def make_item(url: str, title: str, body: str, subreddit: str = "gadgets", score: int = 10) -> CollectedItem:
    return CollectedItem(
        source_url=url,
        subreddit=subreddit,
        item_type="post",
        post_id=url,
        comment_id=None,
        title=title,
        body=body,
        score=score,
        comment_count=0,
        created_at="2026-01-01T00:00:00+00:00",
        search_query="test query",
    )


NOISE_ITEM = make_item("https://reddit.com/noise", "Random post", "Went for a walk today and had a sandwich.")

PAIN_ITEM_1 = make_item("https://reddit.com/pain1", "Battery issue", "I hate how fast the battery dies on this thing.")
PAIN_ITEM_2 = make_item("https://reddit.com/pain2", "Battery issue again", "The battery life is terrible, so annoying.")


class FakeCollector:
    """Duck-types the Collector interface: one canned batch of items per successive `search()` call."""

    def __init__(self, batches: list[list[CollectedItem]]):
        self.batches = batches
        self.calls = 0

    def available(self) -> bool:
        return True

    def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
        batch = self.batches[self.calls] if self.calls < len(self.batches) else []
        self.calls += 1
        return batch


def no_llm() -> DeepSeekClient:
    return DeepSeekClient(api_key="")


def run_loop(tmp_path: Path, batches: list[list[CollectedItem]], max_iterations: int, min_evidence_target: int) -> tuple[Storage, str]:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    run = storage.create_run(
        product_category="wireless earbuds",
        keywords=["earbuds"],
        target_subreddits=[],
        max_iterations=max_iterations,
        min_evidence_target=min_evidence_target,
    )
    collector = FakeCollector(batches)
    run_react_loop(run.run_id, storage, collector, no_llm(), should_stop=lambda: False)
    return storage, run.run_id


def test_stops_at_iteration_cap_when_target_never_reached(tmp_path: Path) -> None:
    batches = [[PAIN_ITEM_1], [PAIN_ITEM_2], [NOISE_ITEM]]
    storage, run_id = run_loop(tmp_path, batches, max_iterations=3, min_evidence_target=1000)

    run = storage.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert run.iteration_count == 3

    events = storage.list_trace_events(run_id)
    sufficiency_events = [event for event in events if event.step_type.value == "sufficiency_check"]
    assert len(sufficiency_events) == 3
    assert "maximum iteration cap" in sufficiency_events[-1].message
    assert storage.get_report(run_id) is not None


def test_stops_early_on_diminishing_returns(tmp_path: Path) -> None:
    batches = [[NOISE_ITEM], [NOISE_ITEM]]
    storage, run_id = run_loop(tmp_path, batches, max_iterations=5, min_evidence_target=100)

    run = storage.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert run.iteration_count == 2  # stopped well before max_iterations=5

    events = storage.list_trace_events(run_id)
    sufficiency_events = [event for event in events if event.step_type.value == "sufficiency_check"]
    assert sufficiency_events[0].payload["sufficient"] is False
    assert sufficiency_events[-1].payload["sufficient"] is True
    assert "diminishing returns" in sufficiency_events[-1].message


def test_deduplicates_evidence_by_url_across_iterations(tmp_path: Path) -> None:
    new_item = make_item("https://reddit.com/pain3", "New battery complaint", "Battery drains so fast, I hate it.")
    batches = [[PAIN_ITEM_1], [PAIN_ITEM_1, new_item]]  # PAIN_ITEM_1 repeated
    storage, run_id = run_loop(tmp_path, batches, max_iterations=2, min_evidence_target=1)

    evidence = storage.list_evidence(run_id)
    urls = [item.source_url for item in evidence]
    assert urls.count("https://reddit.com/pain1") == 1
    assert "https://reddit.com/pain3" in urls


def test_json_upload_collector_drives_the_same_loop_to_a_non_empty_report(tmp_path: Path) -> None:
    """Proves the data source is pluggable: swapping FakeReddit for the real
    JsonUploadCollector runs the identical run_react_loop unmodified."""
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    run = storage.create_run(
        product_category="wireless earbuds",
        keywords=[],
        target_subreddits=[],
        max_iterations=5,
        min_evidence_target=2,
        data_source=DataSource.JSON_UPLOAD,
    )
    uploaded_items = [PAIN_ITEM_1, PAIN_ITEM_2, make_item("https://reddit.com/pain3", "Battery again", "Battery hate it, dies fast.", subreddit="earbuds")]
    run_react_loop(run.run_id, storage, JsonUploadCollector(uploaded_items), no_llm(), should_stop=lambda: False)

    finished = storage.get_run(run.run_id)
    assert finished.status == RunStatus.COMPLETED
    assert len(storage.list_evidence(run.run_id)) == 3
    report = storage.get_report(run.run_id)
    assert report is not None
    assert report.top_pain_points  # non-empty: this data source actually produced usable evidence


def test_mixed_content_review_produces_evidence_and_real_product_claims(tmp_path: Path) -> None:
    """Phase 2 regression, end-to-end through the real loop (fallback screening
    path, no LLM key needed): a review that's mostly a shipping complaint but
    also contains real product content must not be discarded, and its product
    Claims must actually get extracted -- not just survive screening."""
    mixed_item = make_item(
        "https://reddit.com/mixed",
        "Disappointed",
        "Shipping took forever and the box arrived crushed. Aside from that, "
        "the battery life is terrible and dies within an hour.",
    )
    storage, run_id = run_loop(tmp_path, [[mixed_item]], max_iterations=1, min_evidence_target=1)

    evidence = storage.list_evidence(run_id)
    assert len(evidence) == 1
    assert evidence[0].is_mixed_content is True
    assert "shipping_logistics" in evidence[0].screening_categories
    assert "product_feedback" in evidence[0].screening_categories

    claims = storage.list_claims_for_evidence(evidence[0].evidence_id)
    aspects = {c.aspect_raw for c in claims}
    assert "battery" in aspects  # the real product signal survived, not just the shipping complaint


def test_pure_shipping_evidence_still_reaches_claim_extraction(tmp_path: Path) -> None:
    """extract_claims() must never be skipped based on screening's
    has_product_signal judgment -- a pure shipping complaint (no product
    content at all) is still evidence-worthy and still gets a claim-extraction
    attempt, even though it correctly yields no product Claims."""
    shipping_item = make_item(
        "https://reddit.com/shipping-only",
        "Shipping complaint",
        "Shipping took three weeks and the box was crushed on arrival.",
    )
    storage, run_id = run_loop(tmp_path, [[shipping_item]], max_iterations=1, min_evidence_target=1)

    evidence = storage.list_evidence(run_id)
    assert len(evidence) == 1
    assert evidence[0].is_mixed_content is False

    events = storage.list_trace_events(run_id)
    claim_events = [e for e in events if e.step_type.value == "claim_extraction"]
    assert claim_events[0].payload["source_items_processed"] == 1  # extraction was attempted, not skipped


def test_requires_subreddit_diversity_even_after_evidence_floor_met(tmp_path: Path) -> None:
    same_subreddit_pair = [
        make_item("https://reddit.com/a", "Battery complaint A", "Battery life is terrible, hate it.", subreddit="gadgets"),
        make_item("https://reddit.com/b", "Battery complaint B", "Battery dies fast, so annoying.", subreddit="gadgets"),
    ]
    other_subreddit_item = make_item(
        "https://reddit.com/c", "Battery complaint C", "Battery is bad, hate this product.", subreddit="earbuds"
    )
    batches = [same_subreddit_pair, [other_subreddit_item]]
    storage, run_id = run_loop(tmp_path, batches, max_iterations=3, min_evidence_target=2)

    run = storage.get_run(run_id)
    assert run.iteration_count == 2  # floor met after iteration 1 but not sufficient until 2nd subreddit appears
    assert run.status == RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Stage 4 -- categorize_claims() orchestration (categorization only; the
# Report is still built purely from Evidence, per Stage 4's scope)
# ---------------------------------------------------------------------------


def test_categorization_runs_exactly_once_per_completed_run(tmp_path: Path) -> None:
    batches = [[PAIN_ITEM_1], [PAIN_ITEM_2], [NOISE_ITEM]]
    storage, run_id = run_loop(tmp_path, batches, max_iterations=3, min_evidence_target=1000)

    events = storage.list_trace_events(run_id)
    cat_events = [e for e in events if e.step_type.value == "categorization"]
    assert len(cat_events) == 1  # once for the whole run, not once per iteration (3 iterations happened here)


def test_categorization_only_touches_claims_from_the_current_run(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    # A different product_category than the run under test, deliberately --
    # create_run()'s run_id is a hash of (product_category, current second),
    # so two runs with the SAME product_category created in the same second
    # would collide.
    other_run = storage.create_run("cat food", [], [], 6, 25)
    other_claim = Claim(
        claim_id="cl_other",
        run_id=other_run.run_id,
        evidence_id="ev_other",
        claim_type=ClaimType.PROBLEM,
        aspect_raw="battery life",
        statement="Battery drains quickly",
        sentiment=Sentiment.NEGATIVE,
        confidence=0.8,
        extraction_method="llm",
    )
    storage.save_claim(other_claim)

    run = storage.create_run("wireless earbuds", ["earbuds"], [], 1, 1)
    run_react_loop(run.run_id, storage, FakeCollector([[PAIN_ITEM_1]]), no_llm(), should_stop=lambda: False)

    untouched = storage.list_claims(other_run.run_id)[0]
    assert untouched.categorization_status is None  # a different run's Claims were never in scope


def test_categorization_runs_after_claim_extraction_and_before_summary(tmp_path: Path) -> None:
    storage, run_id = run_loop(tmp_path, [[PAIN_ITEM_1]], max_iterations=1, min_evidence_target=1)

    step_order = [e.step_type.value for e in storage.list_trace_events(run_id)]
    assert step_order.index("claim_extraction") < step_order.index("categorization") < step_order.index("summary")


def test_categorization_trace_event_records_the_full_stats_payload(tmp_path: Path) -> None:
    storage, run_id = run_loop(tmp_path, [[PAIN_ITEM_1]], max_iterations=1, min_evidence_target=1)

    cat_event = next(e for e in storage.list_trace_events(run_id) if e.step_type.value == "categorization")
    expected_keys = {
        "claims_total", "distinct_aspects", "lexical_matched", "llm_matched", "new_categories_proposed",
        "unresolved_failures", "skipped_already_resolved", "skipped_manual_protected", "completed", "error",
    }
    assert expected_keys <= cat_event.payload.keys()
    assert cat_event.payload["completed"] is True


def test_disabling_the_kill_switch_skips_categorization_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_CLAIM_CATEGORIZATION", "false")
    storage, run_id = run_loop(tmp_path, [[PAIN_ITEM_1]], max_iterations=1, min_evidence_target=1)

    run = storage.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    cat_events = [e for e in storage.list_trace_events(run_id) if e.step_type.value == "categorization"]
    assert cat_events == []  # skipped cleanly -- no trace event at all, not an empty/error one
    assert storage.get_report(run_id) is not None  # the rest of the run still completed normally
    claims = storage.list_claims(run_id)
    assert claims and all(c.categorization_status is None for c in claims)  # never touched


def test_incomplete_categorization_does_not_crash_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_categorize_claims(*args: object, **kwargs: object) -> CategorizationStats:
        return CategorizationStats(claims_total=1, completed=False, error="RuntimeError: simulated failure")

    # Patched on react_agent's own module -- that's the name run_react_loop
    # actually calls (imported via `from .pipeline.taxonomy import
    # categorize_claims`), not app.pipeline.taxonomy's copy of the name.
    monkeypatch.setattr(react_agent_module, "categorize_claims", _fake_categorize_claims)

    storage, run_id = run_loop(tmp_path, [[PAIN_ITEM_1]], max_iterations=1, min_evidence_target=1)

    run = storage.get_run(run_id)
    assert run.status == RunStatus.COMPLETED  # did not crash or fail the run
    cat_event = next(e for e in storage.list_trace_events(run_id) if e.step_type.value == "categorization")
    assert cat_event.payload["completed"] is False
    assert "INCOMPLETE" in cat_event.message
    assert storage.get_report(run_id) is not None  # summarize() still ran normally afterward


# ---------------------------------------------------------------------------
# Stage 5 -- Claims-report eligibility gate orchestration (wiring only; the
# gate's own branch logic is covered exhaustively in test_report_eligibility.py)
# ---------------------------------------------------------------------------


def test_claims_report_eligibility_trace_event_has_the_expected_payload(tmp_path: Path) -> None:
    storage, run_id = run_loop(tmp_path, [[PAIN_ITEM_1]], max_iterations=1, min_evidence_target=1)

    events = storage.list_trace_events(run_id)
    eligibility_events = [e for e in events if e.step_type.value == "claims_report_eligibility"]
    assert len(eligibility_events) == 1  # once per run, same as categorization
    payload = eligibility_events[0].payload
    assert {"eligible", "fallback_reason", "claims_total", "unresolved_failures", "resolved_ratio"} <= payload.keys()

    step_order = [e.step_type.value for e in events]
    assert step_order.index("categorization") < step_order.index("claims_report_eligibility") < step_order.index("summary")


def test_claims_report_eligibility_still_traced_when_categorization_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_CLAIM_CATEGORIZATION", "false")
    storage, run_id = run_loop(tmp_path, [[PAIN_ITEM_1]], max_iterations=1, min_evidence_target=1)

    eligibility_events = [e for e in storage.list_trace_events(run_id) if e.step_type.value == "claims_report_eligibility"]
    assert len(eligibility_events) == 1  # still recorded -- "why not eligible" must never be a silent question
    assert eligibility_events[0].payload["eligible"] is False
    assert eligibility_events[0].payload["fallback_reason"] == "categorization_disabled"


# ---------------------------------------------------------------------------
# Stage 6 -- eligibility gate is the single decision point for whether claims
# are loaded and the Claims-based report path is actually taken
# ---------------------------------------------------------------------------


def test_report_takes_the_legacy_evidence_path_when_claims_report_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_CLAIMS_REPORT", "false")
    storage, run_id = run_loop(tmp_path, [[PAIN_ITEM_1]], max_iterations=1, min_evidence_target=1)

    eligibility_events = [e for e in storage.list_trace_events(run_id) if e.step_type.value == "claims_report_eligibility"]
    assert eligibility_events[0].payload["eligible"] is False
    assert eligibility_events[0].payload["fallback_reason"] == "claims_report_disabled"

    report = storage.get_report(run_id)
    assert report is not None
    assert report.top_pain_points  # a claim was actually extracted for PAIN_ITEM_1
    assert "category_status" not in report.top_pain_points[0]  # legacy dict shape, not the Claims-path one


def test_report_takes_the_claims_path_when_eligible(tmp_path: Path) -> None:
    # A fresh product_category with no LLM configured: every claim resolves
    # via Tier 1's zero-candidate propose-new (deterministic, no LLM needed),
    # so unresolved_failures stays 0 and the resolved ratio is 1.0 -- clears
    # the default 0.7 minimum, making this run eligible without any mocking.
    storage, run_id = run_loop(tmp_path, [[PAIN_ITEM_1]], max_iterations=1, min_evidence_target=1)

    eligibility_events = [e for e in storage.list_trace_events(run_id) if e.step_type.value == "claims_report_eligibility"]
    assert eligibility_events[0].payload["eligible"] is True

    report = storage.get_report(run_id)
    assert report is not None
    assert report.top_pain_points
    assert "category_status" in report.top_pain_points[0]  # Claims-path dict shape
