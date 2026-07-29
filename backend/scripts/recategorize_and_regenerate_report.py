"""Maintenance script: re-run Categorization -> Eligibility -> Aggregation ->
Report for one existing run, against whatever Claims/taxonomy state already
exists in the database right now.

This mirrors react_agent.run_react_loop's own Phase 3 wiring exactly (the
block right after RunStatus.SUMMARIZING) -- categorize_claims() -> eligibility
-> summarize() -> save_report() -- just invoked standalone against an already-
completed run instead of from inside a live ReAct loop. Useful after curating
the taxonomy (approve/rename/merge/deprecate) to regenerate a run's report
without re-collecting or re-extracting anything, since Claims are already
stored and Categorization/Aggregation are cheap, in-process steps. Uses the
real DeepSeek client -- no mocking.

Always calls categorize_claims() with the safe defaults (force=False,
override_manual=False): claims already categorized are left alone, and
manually-categorized claims (categorization_method == "manual") are always
protected, never overwritten. This script does not expose a way to change
that -- a deliberate scoping choice, not an oversight.

Usage:
    python scripts/recategorize_and_regenerate_report.py <run_id>

Example:
    python scripts/recategorize_and_regenerate_report.py run_55025c50e81b
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict

sys.path.insert(0, ".")

from app.llm import DeepSeekClient
from app.pipeline.taxonomy import categorize_claims
from app.react_agent import _claims_report_eligible, _resolved_ratio, summarize
from app.storage import DEFAULT_DB_PATH, Storage


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: python {sys.argv[0]} <run_id>", file=sys.stderr)
        raise SystemExit(1)
    run_id = sys.argv[1]

    storage = Storage(DEFAULT_DB_PATH)
    llm = DeepSeekClient()
    run = storage.get_run(run_id)
    if run is None:
        print(f"no such run: {run_id}", file=sys.stderr)
        raise SystemExit(1)
    print(f"run: {run.run_id} product_category={run.product_category!r} pipeline_version={run.pipeline_version}")

    run_claims = storage.list_claims(run_id)
    print(f"claims loaded: {len(run_claims)}")

    cat_stats = categorize_claims(run_id, run.product_category, run_claims, storage, llm)
    print("\n--- CategorizationStats ---")
    print(json.dumps(asdict(cat_stats), indent=2, default=str))

    eligible, fallback_reason = _claims_report_eligible(run_id, storage, cat_stats)
    resolved_ratio = _resolved_ratio(cat_stats)
    print("\n--- Eligibility ---")
    print(json.dumps({"eligible": eligible, "fallback_reason": fallback_reason, "resolved_ratio": resolved_ratio}, indent=2))

    claims = []
    categories = []
    if eligible:
        claims = storage.list_claims(run_id)
        categories = storage.list_categories(run.product_category)
    print(f"\ncategories in taxonomy for {run.product_category!r}: {len(categories)}")

    collected = storage.list_evidence(run_id)
    report = summarize(run_id, run.product_category, collected, claims, categories, llm, fallback_reason)
    storage.save_report(report)

    print("\n--- Report ---")
    print(f"report_source={report.report_source} fallback_reason={report.fallback_reason}")
    print(f"top_pain_points={len(report.top_pain_points)} feature_requests={len(report.feature_requests)} "
          f"praised_aspects={len(report.praised_aspects)} competitor_mentions={len(report.competitor_mentions)} "
          f"shipping_issues={len(report.shipping_issues)} seller_service_issues={len(report.seller_service_issues)}")

    print("\n--- Sample top_pain_points (first 5) ---")
    for entry in report.top_pain_points[:5]:
        print(json.dumps(entry, ensure_ascii=False))


if __name__ == "__main__":
    main()
