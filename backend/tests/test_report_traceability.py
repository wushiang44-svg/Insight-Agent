from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from app import routes as routes_module
from app.models import Report, ReportSource
from app.storage import Storage


def make_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    return storage


def make_report(
    run_id: str = "run_1",
    report_source: ReportSource | str = ReportSource.LEGACY_EVIDENCE,
    fallback_reason: str | None = None,
    top_pain_points: list[dict] | None = None,
    shipping_issues: list[dict] | None = None,
    seller_service_issues: list[dict] | None = None,
) -> Report:
    return Report(
        run_id=run_id,
        generated_at="2026-01-01T00:00:00+00:00",
        top_pain_points=top_pain_points if top_pain_points is not None else [],
        feature_requests=[],
        praised_aspects=[],
        competitor_mentions=[],
        sentiment_breakdown={},
        recommended_actions=[],
        summary_markdown="",
        subreddits=[],
        subreddit_counts={},
        shipping_issues=shipping_issues if shipping_issues is not None else [],
        seller_service_issues=seller_service_issues if seller_service_issues is not None else [],
        report_source=report_source,
        fallback_reason=fallback_reason,
    )


# ---------------------------------------------------------------------------
# report_source / fallback_reason -- construction validation
# ---------------------------------------------------------------------------


def test_invalid_report_source_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        make_report(report_source="not_a_real_value")


def test_valid_report_source_strings_are_coerced_to_the_enum() -> None:
    report = make_report(report_source="claims")
    assert report.report_source is ReportSource.CLAIMS


# ---------------------------------------------------------------------------
# report_source / fallback_reason -- storage round trip
# ---------------------------------------------------------------------------


def test_claims_path_report_round_trips_report_source_claims(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.create_run("dog food", [], [], 6, 25)  # not strictly required by save_report, but realistic
    report = make_report(run_id="run_1", report_source=ReportSource.CLAIMS, fallback_reason=None)

    storage.save_report(report)
    loaded = storage.get_report("run_1")

    assert loaded is not None
    assert loaded.report_source is ReportSource.CLAIMS
    assert loaded.fallback_reason is None


def test_legacy_fallback_report_round_trips_report_source_legacy_evidence(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    report = make_report(report_source=ReportSource.LEGACY_EVIDENCE, fallback_reason="no_claims")

    storage.save_report(report)
    loaded = storage.get_report("run_1")

    assert loaded.report_source is ReportSource.LEGACY_EVIDENCE
    assert loaded.fallback_reason == "no_claims"


@pytest.mark.parametrize(
    "reason",
    [
        "claims_report_disabled",
        "categorization_disabled",
        "categorization_incomplete",
        "no_claims",
        "low_resolved_coverage:0.42",
    ],
)
def test_every_stage5_fallback_reason_round_trips_unchanged(tmp_path: Path, reason: str) -> None:
    storage = make_storage(tmp_path)
    report = make_report(fallback_reason=reason)

    storage.save_report(report)
    loaded = storage.get_report("run_1")

    assert loaded.fallback_reason == reason  # exact string, never altered/reformatted on the way through


# ---------------------------------------------------------------------------
# shipping_issues / seller_service_issues persistence
# ---------------------------------------------------------------------------


def test_shipping_issues_persistence_round_trip(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    entry = {"aspect": "late delivery", "count": 3, "subreddit_count": 2, "avg_confidence": 0.8,
              "sentiment_counts": {"negative": 3}, "example_quotes": [], "category_status": "approved"}
    report = make_report(shipping_issues=[entry])

    storage.save_report(report)
    loaded = storage.get_report("run_1")

    assert loaded.shipping_issues == [entry]


def test_seller_service_issues_persistence_round_trip(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    entry = {"aspect": "unresponsive support", "count": 4, "subreddit_count": 1, "avg_confidence": 0.7,
              "sentiment_counts": {"negative": 4}, "example_quotes": [], "category_status": "proposed"}
    report = make_report(seller_service_issues=[entry])

    storage.save_report(report)
    loaded = storage.get_report("run_1")

    assert loaded.seller_service_issues == [entry]


def test_shipping_and_seller_service_issues_default_to_empty_list(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.save_report(make_report())
    loaded = storage.get_report("run_1")

    assert loaded.shipping_issues == []
    assert loaded.seller_service_issues == []


# ---------------------------------------------------------------------------
# category_status persistence within existing sections
# ---------------------------------------------------------------------------


def test_category_status_persists_for_approved_proposed_and_uncategorized_groups(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    entries = [
        {"aspect": "battery life", "count": 5, "subreddit_count": 2, "avg_confidence": 0.8,
         "sentiment_counts": {"negative": 5}, "example_quotes": [], "category_status": "approved"},
        {"aspect": "floor damage", "count": 3, "subreddit_count": 1, "avg_confidence": 0.7,
         "sentiment_counts": {"negative": 3}, "example_quotes": [], "category_status": "proposed"},
        {"aspect": "Uncategorized", "count": 2, "subreddit_count": 1, "avg_confidence": 0.6,
         "sentiment_counts": {"negative": 2}, "example_quotes": [], "category_status": "uncategorized"},
    ]
    report = make_report(top_pain_points=entries)

    storage.save_report(report)
    loaded = storage.get_report("run_1")

    statuses = {entry["aspect"]: entry["category_status"] for entry in loaded.top_pain_points}
    assert statuses == {"battery life": "approved", "floor damage": "proposed", "Uncategorized": "uncategorized"}


def test_legacy_entries_without_category_status_key_load_successfully(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    legacy_entry = {  # exactly _aggregate_by_aspect()'s shape -- no category_status key at all
        "aspect": "battery", "count": 2, "subreddit_count": 1, "avg_confidence": 0.6,
        "sentiment_counts": {"negative": 2}, "example_quotes": [],
    }
    report = make_report(top_pain_points=[legacy_entry])

    storage.save_report(report)
    loaded = storage.get_report("run_1")

    assert loaded.top_pain_points == [legacy_entry]
    assert loaded.top_pain_points[0].get("category_status") is None  # missing key reads back as None, doesn't crash


# ---------------------------------------------------------------------------
# Old reports (pre-Stage-7 rows) -- backward-compatible defaults
# ---------------------------------------------------------------------------


def test_old_report_rows_load_with_backward_compatible_defaults(tmp_path: Path) -> None:
    """Simulates a report saved by pre-Stage-7 code: an INSERT touching only
    the columns that existed before this stage. get_report() must still load
    it successfully, with the new columns' DEFAULTs applied."""
    storage = make_storage(tmp_path)
    storage.conn.execute(
        """
        INSERT INTO reports (
            run_id, generated_at, top_pain_points, feature_requests, praised_aspects,
            competitor_mentions, sentiment_breakdown, recommended_actions, summary_markdown,
            subreddits, subreddit_counts, recommended_actions_zh, summary_markdown_zh
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run_old", "2026-01-01T00:00:00+00:00", "[]", "[]", "[]", "[]", "{}", "[]", "", "[]", "{}", "[]", ""),
    )
    storage.conn.commit()

    loaded = storage.get_report("run_old")

    assert loaded is not None
    assert loaded.report_source is ReportSource.LEGACY_EVIDENCE
    assert loaded.fallback_reason is None
    assert loaded.shipping_issues == []
    assert loaded.seller_service_issues == []


def test_migration_is_idempotent_for_report_traceability_columns(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.migrate()  # calling migrate() a second time on an already-migrated DB must not error

    storage.save_report(make_report())
    assert storage.get_report("run_1") is not None


# ---------------------------------------------------------------------------
# Serialization -- asdict() / API response include all new fields
# ---------------------------------------------------------------------------


def test_report_asdict_includes_all_new_fields() -> None:
    report = make_report(report_source=ReportSource.CLAIMS, fallback_reason=None, shipping_issues=[{"aspect": "x"}])
    payload = asdict(report)

    assert payload["report_source"] == ReportSource.CLAIMS
    assert payload["fallback_reason"] is None
    assert payload["shipping_issues"] == [{"aspect": "x"}]
    assert payload["seller_service_issues"] == []


def test_get_report_route_response_includes_all_new_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "routes_test.sqlite3"
    monkeypatch.setattr(routes_module, "DEFAULT_DB_PATH", db_path)

    storage = Storage(db_path)
    storage.migrate()
    run = storage.create_run("dog food", [], [], 6, 25)
    report = make_report(
        run_id=run.run_id,
        report_source=ReportSource.CLAIMS,
        fallback_reason=None,
        shipping_issues=[{"aspect": "late delivery", "count": 3}],
    )
    storage.save_report(report)
    storage.close()

    response = routes_module.get_report(run.run_id)

    assert response["report_source"] == "claims"
    assert response["fallback_reason"] is None
    assert response["shipping_issues"] == [{"aspect": "late delivery", "count": 3}]
    assert response["seller_service_issues"] == []
