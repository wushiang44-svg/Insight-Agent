from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .collectors._reddit_chrome import profile_status, read_profile_state
from .collectors.amazon import AmazonCollector
from .collectors.reddit import RedditCollector
from .collectors.reddit_browser import RedditBrowserCollector
from .collectors.youtube import YoutubeCollector
from .llm import DeepSeekClient
from .models import CategoryStatus, DataSource, RunStatus
from .run_manager import RunManager
from .storage import DEFAULT_DB_PATH, CategoryTransitionError, Storage, _normalize_category_text

router = APIRouter()
run_manager = RunManager(DEFAULT_DB_PATH)


def _storage() -> Storage:
    storage = Storage(DEFAULT_DB_PATH)
    storage.migrate()
    return storage


class CreateRunRequest(BaseModel):
    product_category: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    target_subreddits: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=6, ge=1, le=20)
    min_evidence_target: int = Field(default=25, ge=1, le=500)
    data_source: DataSource = Field(default=DataSource.REDDIT)
    uploaded_items: list[dict] = Field(default_factory=list)


@router.get("/config")
def get_config() -> dict:
    reddit_browser = RedditBrowserCollector()
    # profile_status()/read_profile_state() are pure filesystem reads on the
    # profile directory -- independent of whether Chrome itself is currently
    # locatable (reddit_browser_configured), so always compute both rather
    # than gating one on the other.
    profile_state = read_profile_state(reddit_browser.profile_dir)
    return {
        "reddit_configured": RedditCollector().available(),
        "reddit_browser_configured": reddit_browser.available(),
        "reddit_profile_status": profile_status(reddit_browser.profile_dir).value,
        "reddit_last_success_at": profile_state.get("last_success_at"),
        "reddit_last_challenge_at": profile_state.get("last_challenge_at"),
        "reddit_consecutive_challenge_count": profile_state.get("consecutive_challenge_count", 0),
        "amazon_configured": AmazonCollector().available(),
        "youtube_configured": YoutubeCollector().available(),
        "deepseek_configured": DeepSeekClient().available(),
    }


@router.post("/runs")
def create_run(payload: CreateRunRequest) -> dict:
    if payload.data_source == DataSource.JSON_UPLOAD and not payload.uploaded_items:
        raise HTTPException(status_code=400, detail="JSON upload mode requires at least one item — please upload a JSON file first.")
    storage = _storage()
    try:
        run = storage.create_run(
            product_category=payload.product_category.strip(),
            keywords=[item.strip() for item in payload.keywords if item.strip()],
            target_subreddits=[item.strip() for item in payload.target_subreddits if item.strip()],
            max_iterations=payload.max_iterations,
            min_evidence_target=payload.min_evidence_target,
            data_source=payload.data_source,
        )
        if payload.data_source == DataSource.JSON_UPLOAD:
            storage.save_uploaded_items(run.run_id, payload.uploaded_items)
    finally:
        storage.close()
    run_manager.start_run(run.run_id)
    return asdict(run)


@router.get("/runs")
def list_runs() -> list[dict]:
    storage = _storage()
    try:
        return [asdict(run) for run in storage.list_runs()]
    finally:
        storage.close()


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    storage = _storage()
    try:
        run = storage.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        trace_events = storage.list_trace_events(run_id)
        return {
            "run": asdict(run),
            "trace_events": [asdict(event) for event in trace_events],
            "is_running": run_manager.is_running(run_id),
        }
    finally:
        storage.close()


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: str) -> dict:
    storage = _storage()
    try:
        run = storage.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
    finally:
        storage.close()
    stopped = run_manager.stop_run(run_id)
    return {"stop_requested": stopped}


@router.get("/runs/{run_id}/claims")
def get_claims(run_id: str) -> list[dict]:
    storage = _storage()
    try:
        run = storage.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        # Pre-Phase-1 runs simply have zero claims -- a valid state, not an error.
        claims = storage.list_claims(run_id)
        evidence_by_id = {evidence.evidence_id: evidence for evidence in storage.list_evidence(run_id)}
        results = []
        for claim in claims:
            payload = asdict(claim)
            # `statement` is an AI-normalized interpretation, never the customer's own
            # words -- these two fields carry the real, untouched original text so API
            # consumers never have to (and never should) present `statement` as a quote.
            source_evidence = evidence_by_id.get(claim.evidence_id)
            payload["original_source_url"] = source_evidence.source_url if source_evidence else None
            # Prefer the claim's own verified excerpt (the specific span that supports
            # THIS claim) over the evidence-level quote (one sentence picked once for
            # the whole review by screening, which may not relate to this claim at
            # all -- see voc_insight_agent Phase 1 validation findings).
            payload["original_excerpt"] = claim.source_excerpt or (source_evidence.quote if source_evidence else None)
            results.append(payload)
        return results
    finally:
        storage.close()


@router.get("/runs/{run_id}/report")
def get_report(run_id: str) -> dict:
    storage = _storage()
    try:
        run = storage.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        report = storage.get_report(run_id)
        if report is None:
            if run.status == RunStatus.FAILED:
                raise HTTPException(status_code=409, detail=f"Run failed: {run.error}")
            raise HTTPException(status_code=409, detail="Report is not ready yet")
        return asdict(report)
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# Phase 3, Stage 8 -- Taxonomy Curation API. API-only (no frontend yet, per
# plan) -- the routes here are thin wrappers around Storage's already-
# validated transition methods (Stage 1), never raw SQL of their own.
# ---------------------------------------------------------------------------


class RenameCategoryRequest(BaseModel):
    canonical_label: str = Field(min_length=1)


class ManualCategorizeClaimRequest(BaseModel):
    category_id: str = Field(min_length=1)


def _category_transition_error_to_http(exc: CategoryTransitionError) -> HTTPException:
    status_code = 404 if exc.code == "not_found" else 409
    return HTTPException(status_code=status_code, detail=str(exc))


@router.get("/categories")
def list_categories(product_category: str, status: CategoryStatus | None = None, canonical_label: str | None = None) -> list[dict]:
    """Browse a product_category's taxonomy. `canonical_label`, if given, is
    an exact (normalized) lookup and ignores `status` -- returns 0 or 1
    entries, never more, since (product_category, normalized_label) is
    unique by construction."""
    storage = _storage()
    try:
        if canonical_label is not None:
            category = storage.get_category_by_label(product_category, canonical_label)
            return [asdict(category)] if category is not None else []
        return [asdict(category) for category in storage.list_categories(product_category, status)]
    finally:
        storage.close()


@router.get("/categories/{category_id}")
def get_category(category_id: str) -> dict:
    storage = _storage()
    try:
        category = storage.get_category(category_id)
        if category is None:
            raise HTTPException(status_code=404, detail="Category not found")
        return asdict(category)
    finally:
        storage.close()


@router.post("/categories/{category_id}/approve")
def approve_category(category_id: str) -> dict:
    storage = _storage()
    try:
        try:
            category = storage.approve_category(category_id)
        except CategoryTransitionError as exc:
            raise _category_transition_error_to_http(exc) from exc
        return asdict(category)
    finally:
        storage.close()


@router.post("/categories/{category_id}/rename")
def rename_category(category_id: str, payload: RenameCategoryRequest) -> dict:
    storage = _storage()
    try:
        try:
            category = storage.rename_category(category_id, payload.canonical_label.strip())
        except CategoryTransitionError as exc:
            raise _category_transition_error_to_http(exc) from exc
        return asdict(category)
    finally:
        storage.close()


@router.post("/categories/{source_id}/merge/{target_id}")
def merge_categories(source_id: str, target_id: str) -> dict:
    storage = _storage()
    try:
        try:
            category = storage.merge_category(source_id, target_id)
        except CategoryTransitionError as exc:
            raise _category_transition_error_to_http(exc) from exc
        return asdict(category)
    finally:
        storage.close()


@router.post("/categories/{category_id}/deprecate")
def deprecate_category(category_id: str) -> dict:
    storage = _storage()
    try:
        try:
            category = storage.deprecate_category(category_id)
        except CategoryTransitionError as exc:
            raise _category_transition_error_to_http(exc) from exc
        return asdict(category)
    finally:
        storage.close()


@router.get("/categories/{category_id}/history")
def get_category_history(category_id: str) -> list[dict]:
    """Audit trail for one category -- timestamp (created_at), action, and
    action-specific old/new values inside `detail` (e.g. merge's
    target_category_id, rename's old_label/new_label, approve/deprecate's
    from_status/to_status). No `actor` field: this app has no auth/identity
    system, so attributing an entry to a specific person would be dishonest,
    not just incomplete -- see models.CategoryAuditLogEntry's docstring."""
    storage = _storage()
    try:
        category = storage.get_category(category_id)
        if category is None:
            raise HTTPException(status_code=404, detail="Category not found")
        return [asdict(entry) for entry in storage.list_category_audit_log(category_id)]
    finally:
        storage.close()


@router.post("/claims/{claim_id}/categorize")
def manually_categorize_claim(claim_id: str, payload: ManualCategorizeClaimRequest) -> dict:
    """A reviewer's direct, explicit assignment of one Claim to a canonical
    category -- categorization_method="manual", categorization_status=
    "resolved" (this codebase's existing vocabulary for "has a definitive
    canonical_category", not a new "categorized" literal), confidence=1.0
    (human-asserted, not a model's confidence score). override_manual=True
    unconditionally: unlike categorize_claims()'s automatic batch pass (which
    must never clobber a manual decision without that flag), a human acting
    through THIS endpoint is always allowed to change a claim's category,
    including correcting an earlier manual assignment -- that protection
    exists to guard against the automatic pipeline, not against a reviewer's
    own explicit action."""
    storage = _storage()
    try:
        claim = storage.get_claim(claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="Claim not found")
        run = storage.get_run(claim.run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found for this claim")
        category = storage.get_category(payload.category_id)
        if category is None:
            raise HTTPException(status_code=404, detail="Category not found")
        if category.alias_of is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Category {payload.category_id} is an alias of {category.alias_of}; assign to the merge target instead",
            )
        if category.product_category != _normalize_category_text(run.product_category):
            raise HTTPException(status_code=409, detail="Category does not belong to this claim's product_category")
        storage.set_claims_categorization(
            [claim_id],
            canonical_category=category.category_id,
            status="resolved",
            method="manual",
            confidence=1.0,
            override_manual=True,
        )
        updated = storage.get_claim(claim_id)
        return asdict(updated)
    finally:
        storage.close()
