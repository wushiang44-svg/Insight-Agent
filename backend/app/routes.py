from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .collectors.reddit import RedditCollector
from .llm import DeepSeekClient
from .models import DataSource, RunStatus
from .run_manager import RunManager
from .storage import DEFAULT_DB_PATH, Storage

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
    data_source: DataSource = Field(default=DataSource.REDDIT_API)
    uploaded_items: list[dict] = Field(default_factory=list)


@router.get("/config")
def get_config() -> dict:
    return {
        "reddit_configured": RedditCollector().available(),
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
