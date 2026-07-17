from __future__ import annotations

from pathlib import Path

from app.models import (
    Claim,
    ClaimType,
    DataSource,
    Evidence,
    InsightType,
    Report,
    RunStatus,
    Sentiment,
    StepType,
    TraceEvent,
    utc_now,
)
from app.storage import Storage


def make_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    return storage


def test_create_and_get_run_roundtrip(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run(
        product_category="dog food",
        keywords=["kibble"],
        target_subreddits=["dogs"],
        max_iterations=6,
        min_evidence_target=25,
    )
    fetched = storage.get_run(run.run_id)
    assert fetched is not None
    assert fetched.product_category == "dog food"
    assert fetched.keywords == ["kibble"]
    assert fetched.status == RunStatus.PLANNING

    storage.update_run_progress(run.run_id, 2, 10, RunStatus.SEARCHING)
    updated = storage.get_run(run.run_id)
    assert updated.iteration_count == 2
    assert updated.evidence_count == 10
    assert updated.status == RunStatus.SEARCHING


def test_evidence_and_trace_event_roundtrip(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)

    evidence = Evidence(
        evidence_id="ev_1",
        run_id=run.run_id,
        iteration=1,
        source_url="https://reddit.com/x",
        subreddit="dogs",
        item_type="post",
        title="title",
        body="body",
        score=5,
        comment_count=0,
        created_at=utc_now(),
        fetched_at=utc_now(),
        search_query="dog food",
        insight_type=InsightType.PAIN_POINT,
        aspect="quality",
        sentiment=Sentiment.NEGATIVE,
        quote="quote",
        confidence=0.8,
    )
    storage.save_evidence(evidence)
    listed = storage.list_evidence(run.run_id)
    assert len(listed) == 1
    assert listed[0].insight_type == InsightType.PAIN_POINT

    event = TraceEvent(run_id=run.run_id, iteration=1, step_type=StepType.THOUGHT, message="msg", payload={"a": 1})
    storage.save_trace_event(event)
    events = storage.list_trace_events(run.run_id)
    assert len(events) == 1
    assert events[0].payload == {"a": 1}


def test_report_roundtrip(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    report = Report(
        run_id=run.run_id,
        generated_at=utc_now(),
        top_pain_points=[{"aspect": "quality", "count": 3}],
        feature_requests=[],
        praised_aspects=[],
        competitor_mentions=[],
        sentiment_breakdown={"negative": 3},
        recommended_actions=["fix quality"],
        summary_markdown="# report",
        subreddits=["dogfood"],
        subreddit_counts={"dogfood": 3},
    )
    storage.save_report(report)
    fetched = storage.get_report(run.run_id)
    assert fetched is not None
    assert fetched.top_pain_points == [{"aspect": "quality", "count": 3}]
    assert fetched.recommended_actions == ["fix quality"]


def test_create_run_with_json_upload_data_source_and_uploaded_items(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25, data_source=DataSource.JSON_UPLOAD)
    assert run.data_source == DataSource.JSON_UPLOAD

    fetched = storage.get_run(run.run_id)
    assert fetched is not None
    assert fetched.data_source == DataSource.JSON_UPLOAD
    assert storage.get_uploaded_items(run.run_id) == []

    raw_items = [{"title": "a", "body": "b"}, {"title": "c", "body": "d"}]
    storage.save_uploaded_items(run.run_id, raw_items)
    assert storage.get_uploaded_items(run.run_id) == raw_items


def test_default_data_source_is_reddit_api(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    assert run.data_source == DataSource.REDDIT_API


def test_new_runs_get_pipeline_version_v3(tmp_path: Path) -> None:
    """v3 = screened by screen_item() (Phase 2), not the old binary analyze_item()
    gate -- bumped from v2 because evidence_count/Report contents can genuinely
    differ from v1/v2 runs on the same source corpus."""
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    assert run.pipeline_version == "v3"
    assert storage.get_run(run.run_id).pipeline_version == "v3"


def make_claim(claim_id: str = "cl_1", run_id: str = "run_1", evidence_id: str = "ev_1") -> Claim:
    return Claim(
        claim_id=claim_id,
        run_id=run_id,
        evidence_id=evidence_id,
        claim_type=ClaimType.PROBLEM,
        aspect_raw="battery",
        statement="Battery drains quickly",
        sentiment=Sentiment.NEGATIVE,
        confidence=0.8,
        extraction_method="llm",
    )


def test_save_claim_and_list_claims_roundtrip(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    claim = make_claim(run_id=run.run_id)

    storage.save_claim(claim)

    listed = storage.list_claims(run.run_id)
    assert len(listed) == 1
    assert listed[0].claim_id == claim.claim_id
    assert listed[0].claim_type == ClaimType.PROBLEM

    for_evidence = storage.list_claims_for_evidence(claim.evidence_id)
    assert [c.claim_id for c in for_evidence] == [claim.claim_id]


def test_replace_claims_for_evidence_deletes_stale_rows_and_inserts_fresh_ones(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    first = make_claim(claim_id="cl_a", run_id=run.run_id, evidence_id="ev_shared")
    storage.replace_claims_for_evidence("ev_shared", [first])
    assert len(storage.list_claims_for_evidence("ev_shared")) == 1

    second = make_claim(claim_id="cl_b", run_id=run.run_id, evidence_id="ev_shared")
    storage.replace_claims_for_evidence("ev_shared", [second])

    stored = storage.list_claims_for_evidence("ev_shared")
    assert [c.claim_id for c in stored] == ["cl_b"]  # "cl_a" is gone, not unioned


def test_replace_claims_for_evidence_with_empty_list_clears_existing_claims(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.replace_claims_for_evidence("ev_x", [make_claim(run_id=run.run_id, evidence_id="ev_x")])
    assert storage.list_claims_for_evidence("ev_x")

    storage.replace_claims_for_evidence("ev_x", [])  # a legitimate "found nothing this time" result

    assert storage.list_claims_for_evidence("ev_x") == []
