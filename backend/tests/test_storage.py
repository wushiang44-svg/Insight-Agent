from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.models import (
    CategoryAuditAction,
    CategoryStatus,
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
from app.storage import CategoryTransitionError, Storage


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


def test_new_runs_get_pipeline_version_v4(tmp_path: Path) -> None:
    """v4 = Claims also go through pipeline/taxonomy.py's categorize_claims()
    batch step (Phase 3, Stage 4) -- bumped from v3 (Phase 2 screening)."""
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    assert run.pipeline_version == "v4"
    assert storage.get_run(run.run_id).pipeline_version == "v4"


def make_claim(
    claim_id: str = "cl_1",
    run_id: str = "run_1",
    evidence_id: str = "ev_1",
    categorization_status: str | None = None,
    categorization_method: str | None = None,
    categorization_confidence: float | None = None,
) -> Claim:
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
        categorization_status=categorization_status,
        categorization_method=categorization_method,
        categorization_confidence=categorization_confidence,
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


def test_migration_is_idempotent_for_canonical_category_tables(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.migrate()  # calling migrate() a second time on an already-migrated DB must not error
    storage.create_category("dog food", "battery life", "battery life")
    assert len(storage.list_categories("dog food")) == 1


def test_create_category_is_idempotent_for_the_same_product_and_label(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    first = storage.create_category("dog food", "floor damage", "floor_damage")
    second = storage.create_category("dog food", "Floor Damage", "floor damage")  # same normalized label

    assert first.category_id == second.category_id
    assert storage.list_categories("dog food") == [first]  # only one row, not two


def test_create_category_scopes_and_normalizes_product_category(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.create_category("Dog Food", "battery life", "battery life")

    # Differently-cased/whitespaced product_category still resolves to the same scope.
    same_scope = storage.list_categories("  dog food  ")
    other_scope = storage.list_categories("cat food")

    assert len(same_scope) == 1
    assert other_scope == []


def test_new_category_defaults_to_proposed(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    assert category.status == CategoryStatus.PROPOSED
    assert category.alias_of is None


def test_list_categories_filters_by_status(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    proposed = storage.create_category("dog food", "battery life", "battery life")
    approved = storage.create_category("dog food", "chew durability", "chew durability")
    storage.approve_category(approved.category_id)

    only_proposed = storage.list_categories("dog food", status=CategoryStatus.PROPOSED)
    only_approved = storage.list_categories("dog food", status=CategoryStatus.APPROVED)

    assert [c.category_id for c in only_proposed] == [proposed.category_id]
    assert [c.category_id for c in only_approved] == [approved.category_id]


def test_approve_category_transitions_status_and_logs_audit(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")

    updated = storage.approve_category(category.category_id)

    assert updated.status == CategoryStatus.APPROVED
    log = storage.list_category_audit_log(category.category_id)
    assert len(log) == 1
    assert log[0].action == CategoryAuditAction.APPROVE
    assert log[0].detail == {"from_status": "proposed", "to_status": "approved"}


def test_approve_category_rejects_already_deprecated(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    storage.deprecate_category(category.category_id)

    with pytest.raises(CategoryTransitionError) as exc_info:
        storage.approve_category(category.category_id)
    assert exc_info.value.code == "conflict"
    # The rejected call must not have changed anything.
    assert storage.get_category(category.category_id).status == CategoryStatus.DEPRECATED
    assert len(storage.list_category_audit_log(category.category_id)) == 1  # only the deprecate entry


def test_transition_on_unknown_category_raises_not_found(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    with pytest.raises(CategoryTransitionError) as exc_info:
        storage.approve_category("cc_does_not_exist")
    assert exc_info.value.code == "not_found"


def test_rename_category_updates_label_and_logs_audit(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    category = storage.create_category("dog food", "floor_damage", "floor_damage")

    updated = storage.rename_category(category.category_id, "Floor Damage")

    assert updated.canonical_label == "Floor Damage"
    assert updated.normalized_label == "floor damage"
    log = storage.list_category_audit_log(category.category_id)
    assert log[-1].action == CategoryAuditAction.RENAME
    assert log[-1].detail == {"old_label": "floor_damage", "new_label": "Floor Damage"}


def test_rename_category_rejects_collision_with_another_categorys_label(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.create_category("dog food", "battery life", "battery life")
    other = storage.create_category("dog food", "chew durability", "chew durability")

    with pytest.raises(CategoryTransitionError) as exc_info:
        storage.rename_category(other.category_id, "Battery Life")  # collides once normalized
    assert exc_info.value.code == "conflict"
    assert storage.get_category(other.category_id).canonical_label == "chew durability"  # unchanged


def test_merge_category_sets_alias_of_and_logs_audit(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = storage.create_category("dog food", "floor_damage", "floor_damage")
    target = storage.create_category("dog food", "floor damage", "floor damage 2")
    storage.approve_category(target.category_id)

    updated = storage.merge_category(source.category_id, target.category_id)

    assert updated.alias_of == target.category_id
    assert updated.status == CategoryStatus.DEPRECATED  # source is retired, not just re-pointed
    assert storage.get_category(target.category_id).status == CategoryStatus.APPROVED  # target untouched
    log = storage.list_category_audit_log(source.category_id)
    assert log[-1].action == CategoryAuditAction.MERGE
    assert log[-1].detail == {"target_category_id": target.category_id}


def test_merge_category_rejects_merging_into_a_deprecated_target(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = storage.create_category("dog food", "battery life", "battery life")
    deprecated_target = storage.create_category("dog food", "chew durability", "chew durability")
    storage.deprecate_category(deprecated_target.category_id)  # deprecated directly, never merged/aliased

    with pytest.raises(CategoryTransitionError):
        storage.merge_category(source.category_id, deprecated_target.category_id)

    assert storage.get_category(source.category_id).alias_of is None  # unchanged


def test_merge_category_rejects_self_merge(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    with pytest.raises(CategoryTransitionError):
        storage.merge_category(category.category_id, category.category_id)


def test_merge_category_rejects_merging_into_an_alias(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    a = storage.create_category("dog food", "a", "a")
    b = storage.create_category("dog food", "b", "b")
    c = storage.create_category("dog food", "c", "c")
    storage.merge_category(a.category_id, b.category_id)  # a -> b

    with pytest.raises(CategoryTransitionError):
        storage.merge_category(c.category_id, a.category_id)  # would-be c -> a -> b chain, rejected

    assert storage.get_category(c.category_id).alias_of is None  # unchanged


def test_merge_category_rejects_merging_a_category_that_is_itself_a_merge_target(tmp_path: Path) -> None:
    """The other half of the one-hop invariant: B already has A pointing at it
    (A.alias_of = B). Merging B into C would orphan A (A would still point at B,
    but B now points at C -- a de facto A->B->C chain) unless rejected outright."""
    storage = make_storage(tmp_path)
    a = storage.create_category("dog food", "a", "a")
    b = storage.create_category("dog food", "b", "b")
    c = storage.create_category("dog food", "c", "c")
    storage.merge_category(a.category_id, b.category_id)  # a -> b

    with pytest.raises(CategoryTransitionError):
        storage.merge_category(b.category_id, c.category_id)  # b is itself a merge target for a

    assert storage.get_category(b.category_id).alias_of is None  # unchanged


def test_merge_category_rejects_cross_product_category_merge(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    dog = storage.create_category("dog food", "battery life", "battery life")
    cat = storage.create_category("cat food", "battery life", "battery life")
    with pytest.raises(CategoryTransitionError):
        storage.merge_category(dog.category_id, cat.category_id)


def test_transition_on_an_alias_is_rejected(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = storage.create_category("dog food", "floor_damage", "floor_damage")
    target = storage.create_category("dog food", "floor damage", "floor damage 2")
    storage.merge_category(source.category_id, target.category_id)

    with pytest.raises(CategoryTransitionError):
        storage.approve_category(source.category_id)  # source is now an alias -- act on target instead


def test_approve_category_rolls_back_the_status_update_if_the_audit_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")

    def _boom(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated audit-log write failure")

    monkeypatch.setattr(storage, "_write_category_audit_log", _boom)

    with pytest.raises(sqlite3.OperationalError):
        storage.approve_category(category.category_id)

    # The status UPDATE must not be left committed just because the audit
    # write that was supposed to accompany it failed.
    assert storage.get_category(category.category_id).status == CategoryStatus.PROPOSED
    assert storage.list_category_audit_log(category.category_id) == []


def test_merge_category_rolls_back_the_alias_update_if_the_audit_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_storage(tmp_path)
    source = storage.create_category("dog food", "floor_damage", "floor_damage")
    target = storage.create_category("dog food", "floor damage", "floor damage 2")

    def _boom(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated audit-log write failure")

    monkeypatch.setattr(storage, "_write_category_audit_log", _boom)

    with pytest.raises(sqlite3.OperationalError):
        storage.merge_category(source.category_id, target.category_id)

    assert storage.get_category(source.category_id).alias_of is None
    assert storage.list_category_audit_log(source.category_id) == []


# ------------------------------------------------------------------
# Stage 2 -- Claim categorization provenance
# ------------------------------------------------------------------


def test_migration_is_idempotent_for_claims_categorization_columns(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.save_claim(make_claim(run_id=run.run_id))

    storage.migrate()  # calling migrate() a second time on an already-migrated DB must not error

    assert len(storage.list_claims(run.run_id)) == 1


def test_save_claim_roundtrips_categorization_provenance_fields(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    claim = make_claim(
        run_id=run.run_id,
        categorization_status="resolved",
        categorization_method="llm_match",
        categorization_confidence=0.83,
    )

    storage.save_claim(claim)

    [loaded] = storage.list_claims(run.run_id)
    assert loaded.categorization_status == "resolved"
    assert loaded.categorization_method == "llm_match"
    assert loaded.categorization_confidence == 0.83


def test_save_claim_roundtrips_null_categorization_provenance_by_default(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.save_claim(make_claim(run_id=run.run_id))

    [loaded] = storage.list_claims(run.run_id)
    assert loaded.categorization_status is None
    assert loaded.categorization_method is None
    assert loaded.categorization_confidence is None


def test_replace_claims_for_evidence_roundtrips_categorization_provenance_fields(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    claim = make_claim(
        run_id=run.run_id,
        evidence_id="ev_x",
        categorization_status="unresolved",
        categorization_method=None,
        categorization_confidence=None,
    )

    storage.replace_claims_for_evidence("ev_x", [claim])

    [loaded] = storage.list_claims_for_evidence("ev_x")
    assert loaded.categorization_status == "unresolved"
    assert loaded.categorization_method is None


def test_set_claims_categorization_updates_only_the_given_claim_ids(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    other_run = storage.create_run("cat food", [], [], 6, 25)
    target_a = make_claim(claim_id="cl_a", run_id=run.run_id, evidence_id="ev_a")
    target_b = make_claim(claim_id="cl_b", run_id=run.run_id, evidence_id="ev_b")
    # Same aspect_raw as the two targets, but a DIFFERENT run -- must never be
    # touched by an update scoped to cl_a/cl_b, proving the write is keyed
    # purely by claim_id and never by any shared string like aspect_raw.
    untouched = make_claim(claim_id="cl_other_run", run_id=other_run.run_id, evidence_id="ev_c")
    for claim in (target_a, target_b, untouched):
        storage.save_claim(claim)

    updated_count = storage.set_claims_categorization(
        ["cl_a", "cl_b"], canonical_category="cc_battery", status="resolved", method="lexical_match", confidence=0.9
    )

    assert updated_count == 2
    a, b = storage.list_claims(run.run_id)
    assert a.categorization_status == "resolved" and a.canonical_category == "cc_battery"
    assert b.categorization_status == "resolved" and b.canonical_category == "cc_battery"
    [other] = storage.list_claims(other_run.run_id)
    assert other.categorization_status is None
    assert other.canonical_category is None


def test_set_claims_categorization_default_protects_manually_categorized_claims(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    manual = make_claim(
        claim_id="cl_manual", run_id=run.run_id, evidence_id="ev_a",
        categorization_status="resolved", categorization_method="manual", categorization_confidence=None,
    )
    manual = replace(manual, canonical_category="cc_human_chosen")
    automatic = make_claim(claim_id="cl_auto", run_id=run.run_id, evidence_id="ev_b")
    storage.save_claim(manual)
    storage.save_claim(automatic)

    updated_count = storage.set_claims_categorization(
        ["cl_manual", "cl_auto"], canonical_category="cc_battery", status="resolved",
        method="lexical_match", confidence=0.9,
        # override_manual defaults to False
    )

    assert updated_count == 1  # only cl_auto -- cl_manual was protected
    still_manual, now_auto = storage.list_claims(run.run_id)
    manual_row = next(c for c in (still_manual, now_auto) if c.claim_id == "cl_manual")
    auto_row = next(c for c in (still_manual, now_auto) if c.claim_id == "cl_auto")
    assert manual_row.canonical_category == "cc_human_chosen"  # unchanged
    assert manual_row.categorization_method == "manual"  # unchanged
    assert auto_row.canonical_category == "cc_battery"  # updated normally


def test_set_claims_categorization_override_manual_allows_overwriting_manual_claims(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    manual = make_claim(
        claim_id="cl_manual", run_id=run.run_id, evidence_id="ev_a",
        categorization_status="resolved", categorization_method="manual", categorization_confidence=None,
    )
    storage.save_claim(manual)

    updated_count = storage.set_claims_categorization(
        ["cl_manual"], canonical_category="cc_battery", status="resolved",
        method="lexical_match", confidence=0.9, override_manual=True,
    )

    assert updated_count == 1
    [loaded] = storage.list_claims(run.run_id)
    assert loaded.canonical_category == "cc_battery"
    assert loaded.categorization_method == "lexical_match"


def test_set_claims_categorization_with_empty_claim_ids_is_a_safe_noop(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    assert storage.set_claims_categorization([], canonical_category="cc_x", status="resolved", method="lexical_match", confidence=0.5) == 0


def test_list_claims_by_status_filters_by_run_and_status(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    other_run = storage.create_run("cat food", [], [], 6, 25)
    storage.save_claim(make_claim(claim_id="cl_unresolved", run_id=run.run_id, evidence_id="ev_a", categorization_status="unresolved"))
    storage.save_claim(make_claim(claim_id="cl_resolved", run_id=run.run_id, evidence_id="ev_b", categorization_status="resolved"))
    storage.save_claim(make_claim(claim_id="cl_untouched", run_id=run.run_id, evidence_id="ev_c"))  # categorization_status None
    storage.save_claim(make_claim(claim_id="cl_other_run_unresolved", run_id=other_run.run_id, evidence_id="ev_d", categorization_status="unresolved"))

    unresolved_in_run = storage.list_claims_by_status(run.run_id, "unresolved")
    untouched_in_run = storage.list_claims_by_status(run.run_id, None)

    assert [c.claim_id for c in unresolved_in_run] == ["cl_unresolved"]
    assert [c.claim_id for c in untouched_in_run] == ["cl_untouched"]
