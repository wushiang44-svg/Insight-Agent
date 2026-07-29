from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app import routes as routes_module
from app.models import Claim, ClaimType, Sentiment
from app.pipeline.taxonomy import categorize_claims
from app.llm import DeepSeekClient
from app.storage import Storage


def no_llm() -> DeepSeekClient:
    return DeepSeekClient(api_key="")


def use_storage(tmp_path: Path, name: str = "routes_test.sqlite3") -> tuple[Path, Storage]:
    db_path = tmp_path / name
    storage = Storage(db_path)
    storage.migrate()
    return db_path, storage


def point_routes_at(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setattr(routes_module, "DEFAULT_DB_PATH", db_path)


def make_claim(claim_id: str, run_id: str, evidence_id: str = "ev_1", aspect_raw: str = "battery life") -> Claim:
    return Claim(
        claim_id=claim_id,
        run_id=run_id,
        evidence_id=evidence_id,
        claim_type=ClaimType.PROBLEM,
        aspect_raw=aspect_raw,
        statement="statement",
        sentiment=Sentiment.NEGATIVE,
        confidence=0.8,
        extraction_method="llm",
    )


# ---------------------------------------------------------------------------
# Category query API
# ---------------------------------------------------------------------------


def test_list_categories_returns_all_for_product_category(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    storage.create_category("dog food", "battery life", "battery life")
    storage.create_category("dog food", "chew durability", "chew durability")
    storage.create_category("cat food", "litter smell", "litter smell")  # different product -- excluded
    storage.close()
    point_routes_at(monkeypatch, db_path)

    results = routes_module.list_categories("dog food")

    assert len(results) == 2
    assert {r["canonical_label"] for r in results} == {"battery life", "chew durability"}
    for r in results:
        assert {"canonical_label", "status", "alias_of", "created_at", "updated_at"} <= r.keys()


def test_list_categories_filters_by_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models import CategoryStatus

    db_path, storage = use_storage(tmp_path)
    proposed = storage.create_category("dog food", "battery life", "battery life")
    approved = storage.create_category("dog food", "chew durability", "chew durability")
    storage.approve_category(approved.category_id)
    storage.close()
    point_routes_at(monkeypatch, db_path)

    only_proposed = routes_module.list_categories("dog food", status=CategoryStatus.PROPOSED)
    only_approved = routes_module.list_categories("dog food", status=CategoryStatus.APPROVED)

    assert [r["category_id"] for r in only_proposed] == [proposed.category_id]
    assert [r["category_id"] for r in only_approved] == [approved.category_id]


def test_list_categories_lookup_by_canonical_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "Battery Life", "battery life")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    results = routes_module.list_categories("dog food", canonical_label="battery life")  # different case

    assert len(results) == 1
    assert results[0]["category_id"] == category.category_id


def test_list_categories_lookup_by_canonical_label_no_match_returns_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, storage = use_storage(tmp_path)
    storage.close()
    point_routes_at(monkeypatch, db_path)

    assert routes_module.list_categories("dog food", canonical_label="does not exist") == []


def test_get_category_by_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    result = routes_module.get_category(category.category_id)

    assert result["category_id"] == category.category_id
    assert result["alias_of"] is None


def test_get_category_not_found_raises_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.get_category("cc_does_not_exist")
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


def test_approve_category_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    result = routes_module.approve_category(category.category_id)

    assert result["status"] == "approved"


def test_approve_category_twice_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    first = routes_module.approve_category(category.category_id)
    second = routes_module.approve_category(category.category_id)

    assert first["status"] == "approved"
    assert second["status"] == "approved"


def test_approve_deprecated_category_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    storage.deprecate_category(category.category_id)
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.approve_category(category.category_id)
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


def test_rename_category_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "floor_damage", "floor_damage")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    result = routes_module.rename_category(category.category_id, routes_module.RenameCategoryRequest(canonical_label="Floor Damage"))

    assert result["canonical_label"] == "Floor Damage"
    assert result["category_id"] == category.category_id  # identity preserved


def test_rename_category_duplicate_label_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    storage.create_category("dog food", "battery life", "battery life")
    other = storage.create_category("dog food", "chew durability", "chew durability")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.rename_category(other.category_id, routes_module.RenameCategoryRequest(canonical_label="Battery Life"))
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def test_merge_categories_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    source = storage.create_category("dog food", "floor_damage", "floor_damage")
    target = storage.create_category("dog food", "floor damage", "floor damage 2")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    result = routes_module.merge_categories(source.category_id, target.category_id)

    assert result["alias_of"] == target.category_id
    assert result["status"] == "deprecated"


def test_merge_categories_self_merge_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.merge_categories(category.category_id, category.category_id)
    assert exc_info.value.status_code == 409


def test_merge_categories_cycle_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    a = storage.create_category("dog food", "a", "a")
    b = storage.create_category("dog food", "b", "b")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    routes_module.merge_categories(a.category_id, b.category_id)  # a -> b

    with pytest.raises(HTTPException) as exc_info:
        routes_module.merge_categories(b.category_id, a.category_id)  # would-be b -> a -> b cycle
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Deprecate
# ---------------------------------------------------------------------------


def test_deprecate_category_without_alias_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    result = routes_module.deprecate_category(category.category_id)

    assert result["status"] == "deprecated"
    assert result["alias_of"] is None


def test_deprecate_category_that_is_already_an_alias_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    source = storage.create_category("dog food", "floor_damage", "floor_damage")
    target = storage.create_category("dog food", "floor damage", "floor damage 2")
    storage.merge_category(source.category_id, target.category_id)
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.deprecate_category(source.category_id)  # already an alias -- act on target instead
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Manual claim categorization
# ---------------------------------------------------------------------------


def test_manual_categorize_claim_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    category = storage.create_category("dog food", "battery life", "battery life")
    storage.save_claim(make_claim("cl_1", run.run_id))
    storage.close()
    point_routes_at(monkeypatch, db_path)

    result = routes_module.manually_categorize_claim(
        "cl_1", routes_module.ManualCategorizeClaimRequest(category_id=category.category_id)
    )

    assert result["categorization_method"] == "manual"
    assert result["categorization_status"] == "resolved"
    assert result["categorization_confidence"] == 1.0
    assert result["canonical_category"] == category.category_id


def test_manual_categorize_claim_rejects_alias_category(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    source = storage.create_category("dog food", "floor_damage", "floor_damage")
    target = storage.create_category("dog food", "floor damage", "floor damage 2")
    storage.merge_category(source.category_id, target.category_id)
    storage.save_claim(make_claim("cl_1", run.run_id))
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.manually_categorize_claim("cl_1", routes_module.ManualCategorizeClaimRequest(category_id=source.category_id))
    assert exc_info.value.status_code == 409


def test_manual_categorize_claim_rejects_wrong_product_category(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    other_category = storage.create_category("cat food", "litter smell", "litter smell")
    storage.save_claim(make_claim("cl_1", run.run_id))
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.manually_categorize_claim(
            "cl_1", routes_module.ManualCategorizeClaimRequest(category_id=other_category.category_id)
        )
    assert exc_info.value.status_code == 409


def test_manual_categorize_claim_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.manually_categorize_claim(
            "cl_does_not_exist", routes_module.ManualCategorizeClaimRequest(category_id=category.category_id)
        )
    assert exc_info.value.status_code == 404


def test_manual_categorize_claim_category_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.save_claim(make_claim("cl_1", run.run_id))
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.manually_categorize_claim("cl_1", routes_module.ManualCategorizeClaimRequest(category_id="cc_missing"))
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# The core invariant: manual categorization survives normal recategorization,
# only override_manual=True can change it
# ---------------------------------------------------------------------------


def test_manual_categorization_survives_normal_recategorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    manual_category = storage.create_category("dog food", "battery life", "battery life")
    storage.save_claim(make_claim("cl_1", run.run_id, aspect_raw="battery life"))
    storage.close()
    point_routes_at(monkeypatch, db_path)
    routes_module.manually_categorize_claim("cl_1", routes_module.ManualCategorizeClaimRequest(category_id=manual_category.category_id))

    storage = Storage(db_path)
    claim = storage.get_claim("cl_1")
    # A routine categorize_claims() pass (defaults: force=False, override_manual=False),
    # as run_react_loop always invokes it -- must never touch the manual claim.
    categorize_claims(run.run_id, "dog food", [claim], storage, no_llm())

    reloaded = storage.get_claim("cl_1")
    assert reloaded.categorization_method == "manual"
    assert reloaded.canonical_category == manual_category.category_id


def test_override_manual_true_replaces_manual_categorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    manual_category = storage.create_category("dog food", "battery life", "battery life")
    storage.save_claim(make_claim("cl_1", run.run_id, aspect_raw="battery life"))
    storage.close()
    point_routes_at(monkeypatch, db_path)
    routes_module.manually_categorize_claim("cl_1", routes_module.ManualCategorizeClaimRequest(category_id=manual_category.category_id))

    storage = Storage(db_path)
    claim = storage.get_claim("cl_1")
    stats = categorize_claims(run.run_id, "dog food", [claim], storage, no_llm(), force=True, override_manual=True)

    reloaded = storage.get_claim("cl_1")
    assert stats.skipped_manual_protected == 0  # not skipped this time
    assert reloaded.categorization_method != "manual"  # actually reprocessed


# ---------------------------------------------------------------------------
# Category history
# ---------------------------------------------------------------------------


def test_category_history_ordering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "floor_damage", "floor_damage")
    storage.approve_category(category.category_id)
    storage.rename_category(category.category_id, "Floor Damage")
    storage.deprecate_category(category.category_id)
    storage.close()
    point_routes_at(monkeypatch, db_path)

    history = routes_module.get_category_history(category.category_id)

    assert [entry["action"] for entry in history] == ["approve", "rename", "deprecate"]
    # Chronologically non-decreasing timestamps.
    timestamps = [entry["created_at"] for entry in history]
    assert timestamps == sorted(timestamps)


def test_audit_entries_recorded_for_every_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    category = storage.create_category("dog food", "battery life", "battery life")
    target = storage.create_category("dog food", "chew durability", "chew durability")
    storage.close()
    point_routes_at(monkeypatch, db_path)

    assert routes_module.get_category_history(category.category_id) == []

    routes_module.approve_category(category.category_id)
    assert len(routes_module.get_category_history(category.category_id)) == 1

    routes_module.rename_category(category.category_id, routes_module.RenameCategoryRequest(canonical_label="Battery Duration"))
    assert len(routes_module.get_category_history(category.category_id)) == 2

    routes_module.deprecate_category(target.category_id)  # target's own history is independent
    assert len(routes_module.get_category_history(category.category_id)) == 2  # unaffected by another category's action


def test_category_history_not_found_raises_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, storage = use_storage(tmp_path)
    storage.close()
    point_routes_at(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        routes_module.get_category_history("cc_does_not_exist")
    assert exc_info.value.status_code == 404
