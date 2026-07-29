from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .models import (
    CanonicalCategory,
    CategoryAuditAction,
    CategoryAuditLogEntry,
    CategoryStatus,
    Claim,
    ClaimType,
    DataSource,
    Evidence,
    InsightType,
    Report,
    RunRecord,
    RunStatus,
    Sentiment,
    StepType,
    TraceEvent,
    utc_now,
)

DEFAULT_DB_PATH = Path("data/reddit_insight_agent.sqlite3")


class CategoryTransitionError(Exception):
    """Raised by a canonical_categories transition method (approve/merge/
    deprecate/rename) when the requested change is invalid -- e.g. acting on an
    already-deprecated category, merging into an alias, or a label collision.
    `code` lets routes.py map this to the right HTTP status without parsing the
    message: "not_found" -> 404, "conflict" (default) -> 409."""

    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(message)
        self.code = code


def _normalize_category_text(text: str) -> str:
    """Lower/whitespace-collapse a product_category or canonical_label for use
    as a matching/uniqueness key. Deliberately independent of
    pipeline/claims.py's own _normalize() -- storage.py stays free of pipeline
    imports, and this is a trivial enough operation not to share."""
    return re.sub(r"\s+", " ", text.strip().lower())


class Storage:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                product_category TEXT NOT NULL,
                keywords TEXT NOT NULL,
                target_subreddits TEXT NOT NULL,
                status TEXT NOT NULL,
                iteration_count INTEGER NOT NULL,
                max_iterations INTEGER NOT NULL,
                min_evidence_target INTEGER NOT NULL,
                evidence_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                data_source TEXT NOT NULL DEFAULT 'reddit_api',
                stop_reason TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                iteration INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                subreddit TEXT NOT NULL,
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                score INTEGER NOT NULL,
                comment_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                search_query TEXT NOT NULL,
                insight_type TEXT NOT NULL,
                aspect TEXT NOT NULL,
                sentiment TEXT NOT NULL,
                quote TEXT NOT NULL,
                confidence REAL NOT NULL,
                screening_categories TEXT,
                is_mixed_content INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);

            CREATE TABLE IF NOT EXISTS trace_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                iteration INTEGER NOT NULL,
                step_type TEXT NOT NULL,
                message TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trace_run ON trace_events(run_id);

            CREATE TABLE IF NOT EXISTS reports (
                run_id TEXT PRIMARY KEY,
                generated_at TEXT NOT NULL,
                top_pain_points TEXT NOT NULL,
                feature_requests TEXT NOT NULL,
                praised_aspects TEXT NOT NULL,
                competitor_mentions TEXT NOT NULL,
                sentiment_breakdown TEXT NOT NULL,
                recommended_actions TEXT NOT NULL,
                summary_markdown TEXT NOT NULL,
                subreddits TEXT NOT NULL DEFAULT '[]',
                subreddit_counts TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS run_uploads (
                run_id TEXT PRIMARY KEY,
                items TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                claim_type TEXT NOT NULL,
                aspect_raw TEXT NOT NULL,
                statement TEXT NOT NULL,
                sentiment TEXT NOT NULL,
                confidence REAL NOT NULL,
                extraction_method TEXT NOT NULL,
                created_at TEXT NOT NULL,
                subject TEXT,
                explicit_request TEXT,
                severity REAL,
                canonical_category TEXT,
                source_excerpt TEXT,
                merge_count INTEGER NOT NULL DEFAULT 1,
                merged_claim_ids TEXT,
                merged_excerpts TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_claims_run ON claims(run_id);
            CREATE INDEX IF NOT EXISTS idx_claims_evidence ON claims(evidence_id);

            CREATE TABLE IF NOT EXISTS canonical_categories (
                category_id TEXT PRIMARY KEY,
                product_category TEXT NOT NULL,
                canonical_label TEXT NOT NULL,
                normalized_label TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                alias_of TEXT,
                first_seen_aspect_raw TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_categories_product ON canonical_categories(product_category);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_product_label
                ON canonical_categories(product_category, normalized_label);

            CREATE TABLE IF NOT EXISTS category_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_category_audit_category ON category_audit_log(category_id);
            """
        )
        try:
            self.conn.execute("ALTER TABLE runs ADD COLUMN data_source TEXT NOT NULL DEFAULT 'reddit_api'")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE reports ADD COLUMN subreddits TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE reports ADD COLUMN subreddit_counts TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE reports ADD COLUMN recommended_actions_zh TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE reports ADD COLUMN summary_markdown_zh TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            # Phase 3, Stage 7. Evidence has no shipping_issue/seller_service_issue
            # concept of its own, so reports saved before this migration (and every
            # legacy-path report going forward) correctly default to '[]', not a
            # backfill gap.
            self.conn.execute("ALTER TABLE reports ADD COLUMN shipping_issues TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE reports ADD COLUMN seller_service_issues TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            # Every report ever generated before this migration was, definitionally,
            # built from Evidence (the Claims path didn't exist yet) -- "legacy_evidence"
            # is the accurate default for old rows, not a placeholder.
            self.conn.execute("ALTER TABLE reports ADD COLUMN report_source TEXT NOT NULL DEFAULT 'legacy_evidence'")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            # NULL (not a string) is the correct default -- a report_source of
            # "legacy_evidence" only sometimes has a real fallback_reason (the Claims
            # path was actually attempted and rejected); an old row predating this
            # column has no reason to report at all, not an empty one.
            self.conn.execute("ALTER TABLE reports ADD COLUMN fallback_reason TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            # Runs created before Phase 1 (Claim extraction) default to "v1" (legacy
            # aspect-only pipeline) — they simply have zero rows in `claims`, which is
            # a valid state, not an error. New runs are created with "v2" explicitly.
            self.conn.execute("ALTER TABLE runs ADD COLUMN pipeline_version TEXT NOT NULL DEFAULT 'v1'")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            # Claims saved before this migration have no per-claim excerpt; routes.py
            # falls back to the parent Evidence's quote for those rows (NULL, not '').
            self.conn.execute("ALTER TABLE claims ADD COLUMN source_excerpt TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            # Phase 1.6 (within-review dedup). Old claim rows default to
            # merge_count=1 -- accurately "not merged", since nothing was
            # merged when they were created. merged_claim_ids/merged_excerpts
            # stay NULL for them (no provenance to backfill).
            self.conn.execute("ALTER TABLE claims ADD COLUMN merge_count INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE claims ADD COLUMN merged_claim_ids TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE claims ADD COLUMN merged_excerpts TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            # Phase 2 (review screening). Evidence saved before this migration has
            # no screening data -- screening_categories stays NULL, is_mixed_content
            # defaults to 0/False, both accurately representing "not screened under
            # the new system" rather than needing a backfill.
            self.conn.execute("ALTER TABLE evidence ADD COLUMN screening_categories TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE evidence ADD COLUMN is_mixed_content INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            # Phase 3 categorization provenance. Claims saved before this
            # migration simply have all three NULL -- "categorization hasn't
            # run for this claim yet", a valid state (see models.Claim's
            # docstring), not something to backfill.
            self.conn.execute("ALTER TABLE claims ADD COLUMN categorization_status TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE claims ADD COLUMN categorization_method TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        try:
            self.conn.execute("ALTER TABLE claims ADD COLUMN categorization_confidence REAL")
        except sqlite3.OperationalError:
            pass  # column already exists on databases created after this migration was added
        self.conn.commit()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def create_run(
        self,
        product_category: str,
        keywords: list[str],
        target_subreddits: list[str],
        max_iterations: int,
        min_evidence_target: int,
        data_source: DataSource = DataSource.REDDIT_API,
    ) -> RunRecord:
        now = utc_now()
        run_id = f"run_{hashlib.sha1(f'{product_category}{now}'.encode()).hexdigest()[:12]}"
        run = RunRecord(
            run_id=run_id,
            product_category=product_category,
            keywords=keywords,
            target_subreddits=target_subreddits,
            status=RunStatus.PLANNING,
            iteration_count=0,
            max_iterations=max_iterations,
            min_evidence_target=min_evidence_target,
            evidence_count=0,
            created_at=now,
            updated_at=now,
            data_source=data_source,
            pipeline_version="v4",
        )
        self.conn.execute(
            """
            INSERT INTO runs (
                run_id, product_category, keywords, target_subreddits, status,
                iteration_count, max_iterations, min_evidence_target, evidence_count,
                created_at, updated_at, data_source, stop_reason, error, pipeline_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.product_category,
                json.dumps(run.keywords),
                json.dumps(run.target_subreddits),
                run.status.value,
                run.iteration_count,
                run.max_iterations,
                run.min_evidence_target,
                run.evidence_count,
                run.created_at,
                run.updated_at,
                run.data_source.value,
                run.stop_reason,
                run.error,
                run.pipeline_version,
            ),
        )
        self.conn.commit()
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self) -> list[RunRecord]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [self._row_to_run(row) for row in rows]

    def update_run_progress(self, run_id: str, iteration_count: int, evidence_count: int, status: RunStatus) -> None:
        self.conn.execute(
            "UPDATE runs SET iteration_count = ?, evidence_count = ?, status = ?, updated_at = ? WHERE run_id = ?",
            (iteration_count, evidence_count, status.value, utc_now(), run_id),
        )
        self.conn.commit()

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        stop_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET status = ?, stop_reason = ?, error = ?, updated_at = ? WHERE run_id = ?",
            (status.value, stop_reason, error, utc_now(), run_id),
        )
        self.conn.commit()

    def _row_to_run(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            product_category=row["product_category"],
            keywords=json.loads(row["keywords"]),
            target_subreddits=json.loads(row["target_subreddits"]),
            status=RunStatus(row["status"]),
            iteration_count=row["iteration_count"],
            max_iterations=row["max_iterations"],
            min_evidence_target=row["min_evidence_target"],
            evidence_count=row["evidence_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            data_source=DataSource(row["data_source"]),
            stop_reason=row["stop_reason"],
            error=row["error"],
            pipeline_version=row["pipeline_version"] if "pipeline_version" in row.keys() else "v1",
        )

    # ------------------------------------------------------------------
    # Uploaded JSON items (data_source = json_upload)
    # ------------------------------------------------------------------

    def save_uploaded_items(self, run_id: str, items: list[dict[str, Any]]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO run_uploads (run_id, items) VALUES (?, ?)",
            (run_id, json.dumps(items)),
        )
        self.conn.commit()

    def get_uploaded_items(self, run_id: str) -> list[dict[str, Any]]:
        row = self.conn.execute("SELECT items FROM run_uploads WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["items"]) if row else []

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    def save_evidence(self, evidence: Evidence) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO evidence (
                evidence_id, run_id, iteration, source_url, subreddit, item_type, title, body,
                score, comment_count, created_at, fetched_at, search_query, insight_type,
                aspect, sentiment, quote, confidence, screening_categories, is_mixed_content
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_id,
                evidence.run_id,
                evidence.iteration,
                evidence.source_url,
                evidence.subreddit,
                evidence.item_type,
                evidence.title,
                evidence.body,
                evidence.score,
                evidence.comment_count,
                evidence.created_at,
                evidence.fetched_at,
                evidence.search_query,
                evidence.insight_type.value,
                evidence.aspect,
                evidence.sentiment.value,
                evidence.quote,
                evidence.confidence,
                json.dumps(evidence.screening_categories) if evidence.screening_categories is not None else None,
                int(evidence.is_mixed_content),
            ),
        )
        self.conn.commit()

    def list_evidence(self, run_id: str) -> list[Evidence]:
        rows = self.conn.execute(
            "SELECT * FROM evidence WHERE run_id = ? ORDER BY iteration ASC, created_at ASC", (run_id,)
        ).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def _row_to_evidence(self, row: sqlite3.Row) -> Evidence:
        return Evidence(
            evidence_id=row["evidence_id"],
            run_id=row["run_id"],
            iteration=row["iteration"],
            source_url=row["source_url"],
            subreddit=row["subreddit"],
            item_type=row["item_type"],
            title=row["title"],
            body=row["body"],
            score=row["score"],
            comment_count=row["comment_count"],
            created_at=row["created_at"],
            fetched_at=row["fetched_at"],
            search_query=row["search_query"],
            insight_type=InsightType(row["insight_type"]),
            aspect=row["aspect"],
            sentiment=Sentiment(row["sentiment"]),
            quote=row["quote"],
            confidence=row["confidence"],
            screening_categories=json.loads(row["screening_categories"]) if row["screening_categories"] else None,
            is_mixed_content=bool(row["is_mixed_content"]),
        )

    # ------------------------------------------------------------------
    # Trace events
    # ------------------------------------------------------------------

    def save_trace_event(self, event: TraceEvent) -> None:
        self.conn.execute(
            """
            INSERT INTO trace_events (run_id, iteration, step_type, message, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.run_id,
                event.iteration,
                event.step_type.value,
                event.message,
                json.dumps(event.payload),
                event.created_at,
            ),
        )
        self.conn.commit()

    def list_trace_events(self, run_id: str) -> list[TraceEvent]:
        rows = self.conn.execute(
            "SELECT * FROM trace_events WHERE run_id = ? ORDER BY id ASC", (run_id,)
        ).fetchall()
        return [
            TraceEvent(
                run_id=row["run_id"],
                iteration=row["iteration"],
                step_type=StepType(row["step_type"]),
                message=row["message"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def save_report(self, report: Report) -> None:
        # No separate validation needed here: Report.__post_init__ already
        # guarantees report_source is a real ReportSource member on every
        # live Report instance (it runs at construction, before this method
        # could ever be reached), so an invalid value can never arrive here
        # to persist in the first place.
        self.conn.execute(
            """
            INSERT OR REPLACE INTO reports (
                run_id, generated_at, top_pain_points, feature_requests, praised_aspects,
                competitor_mentions, sentiment_breakdown, recommended_actions, summary_markdown,
                subreddits, subreddit_counts, recommended_actions_zh, summary_markdown_zh,
                shipping_issues, seller_service_issues, report_source, fallback_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.run_id,
                report.generated_at,
                json.dumps(report.top_pain_points),
                json.dumps(report.feature_requests),
                json.dumps(report.praised_aspects),
                json.dumps(report.competitor_mentions),
                json.dumps(report.sentiment_breakdown),
                json.dumps(report.recommended_actions),
                report.summary_markdown,
                json.dumps(report.subreddits),
                json.dumps(report.subreddit_counts),
                json.dumps(report.recommended_actions_zh),
                report.summary_markdown_zh,
                json.dumps(report.shipping_issues),
                json.dumps(report.seller_service_issues),
                report.report_source.value,
                report.fallback_reason,
            ),
        )
        self.conn.commit()

    def get_report(self, run_id: str) -> Report | None:
        row = self.conn.execute("SELECT * FROM reports WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return Report(
            run_id=row["run_id"],
            generated_at=row["generated_at"],
            top_pain_points=json.loads(row["top_pain_points"]),
            feature_requests=json.loads(row["feature_requests"]),
            praised_aspects=json.loads(row["praised_aspects"]),
            competitor_mentions=json.loads(row["competitor_mentions"]),
            sentiment_breakdown=json.loads(row["sentiment_breakdown"]),
            recommended_actions=json.loads(row["recommended_actions"]),
            summary_markdown=row["summary_markdown"],
            subreddits=json.loads(row["subreddits"]),
            subreddit_counts=json.loads(row["subreddit_counts"]),
            recommended_actions_zh=json.loads(row["recommended_actions_zh"]) if row["recommended_actions_zh"] else [],
            summary_markdown_zh=row["summary_markdown_zh"] or "",
            shipping_issues=json.loads(row["shipping_issues"]) if row["shipping_issues"] else [],
            seller_service_issues=json.loads(row["seller_service_issues"]) if row["seller_service_issues"] else [],
            report_source=row["report_source"] or "legacy_evidence",  # Report.__post_init__ coerces this to ReportSource
            fallback_reason=row["fallback_reason"],
        )

    # ------------------------------------------------------------------
    # Claims
    # ------------------------------------------------------------------

    def save_claim(self, claim: Claim) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO claims (
                claim_id, run_id, evidence_id, claim_type, aspect_raw, statement,
                sentiment, confidence, extraction_method, created_at, subject,
                explicit_request, severity, canonical_category, source_excerpt,
                merge_count, merged_claim_ids, merged_excerpts,
                categorization_status, categorization_method, categorization_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim.claim_id,
                claim.run_id,
                claim.evidence_id,
                claim.claim_type.value,
                claim.aspect_raw,
                claim.statement,
                claim.sentiment.value,
                claim.confidence,
                claim.extraction_method,
                claim.created_at,
                claim.subject,
                claim.explicit_request,
                claim.severity,
                claim.canonical_category,
                claim.source_excerpt,
                claim.merge_count,
                json.dumps(claim.merged_claim_ids) if claim.merged_claim_ids is not None else None,
                json.dumps(claim.merged_excerpts) if claim.merged_excerpts is not None else None,
                claim.categorization_status,
                claim.categorization_method,
                claim.categorization_confidence,
            ),
        )
        self.conn.commit()

    def replace_claims_for_evidence(self, evidence_id: str, claims: list[Claim]) -> None:
        """Deletes all existing claims for `evidence_id`, then inserts `claims`, in one
        transaction. This is the only safe way to (re-)store claims for a piece of
        evidence: call it ONLY after extraction has already succeeded (see
        pipeline/claims.py's `ClaimExtractionResult.succeeded`) — never call it on a
        failed extraction, or a transient LLM/fallback failure would silently erase
        claims a previous successful run already stored. An empty `claims` list is a
        legitimate "extraction succeeded, found nothing" result and does clear
        whatever was there before."""
        with self.conn:
            self.conn.execute("DELETE FROM claims WHERE evidence_id = ?", (evidence_id,))
            for claim in claims:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO claims (
                        claim_id, run_id, evidence_id, claim_type, aspect_raw, statement,
                        sentiment, confidence, extraction_method, created_at, subject,
                        explicit_request, severity, canonical_category, source_excerpt,
                        merge_count, merged_claim_ids, merged_excerpts,
                        categorization_status, categorization_method, categorization_confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim.claim_id,
                        claim.run_id,
                        claim.evidence_id,
                        claim.claim_type.value,
                        claim.aspect_raw,
                        claim.statement,
                        claim.sentiment.value,
                        claim.confidence,
                        claim.extraction_method,
                        claim.created_at,
                        claim.subject,
                        claim.explicit_request,
                        claim.severity,
                        claim.canonical_category,
                        claim.source_excerpt,
                        claim.merge_count,
                        json.dumps(claim.merged_claim_ids) if claim.merged_claim_ids is not None else None,
                        json.dumps(claim.merged_excerpts) if claim.merged_excerpts is not None else None,
                        claim.categorization_status,
                        claim.categorization_method,
                        claim.categorization_confidence,
                    ),
                )

    def get_claim(self, claim_id: str) -> Claim | None:
        row = self.conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
        return self._row_to_claim(row) if row is not None else None

    def list_claims(self, run_id: str) -> list[Claim]:
        rows = self.conn.execute(
            "SELECT * FROM claims WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
        ).fetchall()
        return [self._row_to_claim(row) for row in rows]

    def list_claims_for_evidence(self, evidence_id: str) -> list[Claim]:
        rows = self.conn.execute(
            "SELECT * FROM claims WHERE evidence_id = ? ORDER BY created_at ASC", (evidence_id,)
        ).fetchall()
        return [self._row_to_claim(row) for row in rows]

    def _row_to_claim(self, row: sqlite3.Row) -> Claim:
        return Claim(
            claim_id=row["claim_id"],
            run_id=row["run_id"],
            evidence_id=row["evidence_id"],
            claim_type=ClaimType(row["claim_type"]),
            aspect_raw=row["aspect_raw"],
            statement=row["statement"],
            sentiment=Sentiment(row["sentiment"]),
            confidence=row["confidence"],
            extraction_method=row["extraction_method"],
            created_at=row["created_at"],
            subject=row["subject"],
            explicit_request=row["explicit_request"],
            severity=row["severity"],
            canonical_category=row["canonical_category"],
            source_excerpt=row["source_excerpt"],
            merge_count=row["merge_count"],
            merged_claim_ids=json.loads(row["merged_claim_ids"]) if row["merged_claim_ids"] else None,
            merged_excerpts=json.loads(row["merged_excerpts"]) if row["merged_excerpts"] else None,
            categorization_status=row["categorization_status"],
            categorization_method=row["categorization_method"],
            categorization_confidence=row["categorization_confidence"],
        )

    def set_claims_categorization(
        self,
        claim_ids: list[str],
        canonical_category: str | None,
        status: str,
        method: str | None,
        confidence: float | None,
        *,
        override_manual: bool = False,
    ) -> int:
        """Bulk-writes Phase 3 categorization results onto exactly the given
        claim_ids -- never accepts an aspect_raw or any other loose match key,
        by construction, so an unscoped "every claim sharing this string,
        across every run and product category" update is not representable
        through this API at all (see pipeline/taxonomy.py's categorize_claims,
        which is the only intended caller).

        Refuses to overwrite a categorization_method='manual' row unless
        override_manual=True -- enforced here, in the WHERE clause itself, not
        just by trusting the caller to have pre-filtered. `IS NOT 'manual'`
        (not `!= 'manual'`) is deliberate: SQL's `!=` against NULL evaluates to
        NULL/false, which would incorrectly exclude never-categorized rows
        (categorization_method IS NULL) from ever being updated.

        Returns the number of rows actually updated, which can be fewer than
        len(claim_ids) when some were manual-protected.
        """
        if not claim_ids:
            return 0
        placeholders = ",".join("?" for _ in claim_ids)
        cursor = self.conn.execute(
            f"""
            UPDATE claims
            SET canonical_category = ?, categorization_status = ?,
                categorization_method = ?, categorization_confidence = ?
            WHERE claim_id IN ({placeholders})
              AND (categorization_method IS NOT 'manual' OR ?)
            """,
            (canonical_category, status, method, confidence, *claim_ids, 1 if override_manual else 0),
        )
        self.conn.commit()
        return cursor.rowcount

    def list_claims_by_status(self, run_id: str, categorization_status: str | None) -> list[Claim]:
        """Fetches claims for one run filtered by categorization_status --
        e.g. `list_claims_by_status(run_id, "unresolved")` for the retry-
        unresolved maintenance path, or `list_claims_by_status(run_id, None)`
        for claims categorization has never touched at all."""
        if categorization_status is None:
            rows = self.conn.execute(
                "SELECT * FROM claims WHERE run_id = ? AND categorization_status IS NULL ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM claims WHERE run_id = ? AND categorization_status = ? ORDER BY created_at ASC",
                (run_id, categorization_status),
            ).fetchall()
        return [self._row_to_claim(row) for row in rows]

    # ------------------------------------------------------------------
    # Canonical categories (Phase 3 taxonomy)
    # ------------------------------------------------------------------

    def create_category(self, product_category: str, canonical_label: str, first_seen_aspect_raw: str) -> CanonicalCategory:
        """Creates a new proposed category, or returns the existing one if the
        same (product_category, normalized_label) already exists -- idempotent
        by construction (category_id is a deterministic hash of both), so a
        caller proposing "the same new category" twice (e.g. two claims in the
        same batch minting it independently) never races into a duplicate."""
        normalized_product = _normalize_category_text(product_category)
        normalized_label = _normalize_category_text(canonical_label)
        category_id = "cc_" + hashlib.sha1(f"{normalized_product}|{normalized_label}".encode("utf-8")).hexdigest()[:16]
        now = utc_now()
        try:
            self.conn.execute(
                """
                INSERT INTO canonical_categories (
                    category_id, product_category, canonical_label, normalized_label,
                    status, alias_of, first_seen_aspect_raw, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    category_id,
                    normalized_product,
                    canonical_label,
                    normalized_label,
                    CategoryStatus.PROPOSED.value,
                    first_seen_aspect_raw,
                    now,
                    now,
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            # Same content -> same category_id (PK collision) is the expected,
            # common case here; the (product_category, normalized_label) unique
            # index is a defensive backstop independent of how the id happens to
            # be derived. Either way, "already exists" is success, not an error.
            existing = self.get_category(category_id)
            if existing is not None:
                return existing
            raise
        return self.get_category(category_id)  # type: ignore[return-value]

    def create_category_and_categorize_claims(
        self,
        product_category: str,
        canonical_label: str,
        first_seen_aspect_raw: str,
        claim_ids: list[str],
        *,
        override_manual: bool = False,
    ) -> tuple[CanonicalCategory, int]:
        """Atomically resolves-or-creates a category for one aspect group AND
        writes categorization_status='resolved'/method='proposed_new' onto
        exactly the given claim_ids, sharing ONE commit/rollback for both
        writes -- Phase 3's per-aspect-group transactional-write requirement.
        Without this, a plain create_category() (which commits immediately)
        followed by a separate set_claims_categorization() call could leave a
        newly-created proposed category committed with zero claims pointing
        at it, if the second call then failed. Idempotent the same way
        create_category() is: an already-existing (product_category,
        normalized_label) row is reused, not duplicated. Same claim_id-only
        scoping and override_manual guard as set_claims_categorization() --
        see that method's docstring for why `IS NOT` (not `!=`) matters.
        Returns (the resolved-or-created category, rows actually updated).
        """
        normalized_product = _normalize_category_text(product_category)
        normalized_label = _normalize_category_text(canonical_label)
        category_id = "cc_" + hashlib.sha1(f"{normalized_product}|{normalized_label}".encode("utf-8")).hexdigest()[:16]
        now = utc_now()
        try:
            self.conn.execute(
                """
                INSERT INTO canonical_categories (
                    category_id, product_category, canonical_label, normalized_label,
                    status, alias_of, first_seen_aspect_raw, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(category_id) DO NOTHING
                """,
                (
                    category_id,
                    normalized_product,
                    canonical_label,
                    normalized_label,
                    CategoryStatus.PROPOSED.value,
                    first_seen_aspect_raw,
                    now,
                    now,
                ),
            )
            rows_updated = 0
            if claim_ids:
                placeholders = ",".join("?" for _ in claim_ids)
                cursor = self.conn.execute(
                    f"""
                    UPDATE claims
                    SET canonical_category = ?, categorization_status = 'resolved',
                        categorization_method = 'proposed_new', categorization_confidence = NULL
                    WHERE claim_id IN ({placeholders})
                      AND (categorization_method IS NOT 'manual' OR ?)
                    """,
                    (category_id, *claim_ids, 1 if override_manual else 0),
                )
                rows_updated = cursor.rowcount
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        category = self.get_category(category_id)
        assert category is not None
        return category, rows_updated

    def get_category(self, category_id: str) -> CanonicalCategory | None:
        row = self.conn.execute("SELECT * FROM canonical_categories WHERE category_id = ?", (category_id,)).fetchone()
        return self._row_to_category(row) if row is not None else None

    def list_categories(self, product_category: str, status: CategoryStatus | None = None) -> list[CanonicalCategory]:
        normalized_product = _normalize_category_text(product_category)
        if status is not None:
            rows = self.conn.execute(
                "SELECT * FROM canonical_categories WHERE product_category = ? AND status = ? ORDER BY created_at ASC",
                (normalized_product, status.value),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM canonical_categories WHERE product_category = ? ORDER BY created_at ASC",
                (normalized_product,),
            ).fetchall()
        return [self._row_to_category(row) for row in rows]

    def get_category_by_label(self, product_category: str, canonical_label: str) -> CanonicalCategory | None:
        """Exact lookup by (product_category, canonical_label), normalized the
        same way create_category() normalizes at write time -- e.g. "Battery
        Life" and "battery life" resolve to the same row."""
        normalized_product = _normalize_category_text(product_category)
        normalized_label = _normalize_category_text(canonical_label)
        row = self.conn.execute(
            "SELECT * FROM canonical_categories WHERE product_category = ? AND normalized_label = ?",
            (normalized_product, normalized_label),
        ).fetchone()
        return self._row_to_category(row) if row is not None else None

    def approve_category(self, category_id: str) -> CanonicalCategory:
        category = self._get_category_for_transition(category_id, allow_deprecated=False)
        now = utc_now()
        try:
            self.conn.execute(
                "UPDATE canonical_categories SET status = ?, updated_at = ? WHERE category_id = ?",
                (CategoryStatus.APPROVED.value, now, category_id),
            )
            self._write_category_audit_log(
                category_id,
                CategoryAuditAction.APPROVE,
                {"from_status": category.status.value, "to_status": CategoryStatus.APPROVED.value},
            )
            self.conn.commit()
        except Exception:
            # If the audit write fails after the status UPDATE already ran, the
            # UPDATE must not be left dangling uncommitted in this connection's
            # transaction -- roll back so the state change and its audit record
            # either both land or neither does.
            self.conn.rollback()
            raise
        return self.get_category(category_id)  # type: ignore[return-value]

    def deprecate_category(self, category_id: str) -> CanonicalCategory:
        category = self._get_category_for_transition(category_id, allow_deprecated=False)
        now = utc_now()
        try:
            self.conn.execute(
                "UPDATE canonical_categories SET status = ?, updated_at = ? WHERE category_id = ?",
                (CategoryStatus.DEPRECATED.value, now, category_id),
            )
            self._write_category_audit_log(
                category_id,
                CategoryAuditAction.DEPRECATE,
                {"from_status": category.status.value, "to_status": CategoryStatus.DEPRECATED.value},
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_category(category_id)  # type: ignore[return-value]

    def rename_category(self, category_id: str, new_canonical_label: str) -> CanonicalCategory:
        category = self._get_category_for_transition(category_id, allow_deprecated=False)
        new_normalized = _normalize_category_text(new_canonical_label)
        now = utc_now()
        try:
            self.conn.execute(
                "UPDATE canonical_categories SET canonical_label = ?, normalized_label = ?, updated_at = ? WHERE category_id = ?",
                (new_canonical_label, new_normalized, now, category_id),
            )
            self._write_category_audit_log(
                category_id,
                CategoryAuditAction.RENAME,
                {"old_label": category.canonical_label, "new_label": new_canonical_label},
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise CategoryTransitionError(
                f"A category with the normalized label '{new_normalized}' already exists for "
                f"product_category '{category.product_category}'"
            ) from exc
        except Exception:
            self.conn.rollback()
            raise
        return self.get_category(category_id)  # type: ignore[return-value]

    def merge_category(self, category_id: str, target_category_id: str) -> CanonicalCategory:
        """Sets category_id's alias_of to target_category_id AND marks it
        deprecated -- a category-to-category merge only (see
        models.CanonicalCategory's docstring; this is never a string-synonym
        mechanism). Enforces the "exactly one hop deep" invariant from ALL
        directions: the target must not itself already be an alias (no
        A->B->C chains), the target must not itself be deprecated (rejects
        merging into an already-merged-away or standalone-retired category --
        also what makes a cycle like B->A after an earlier A->B structurally
        impossible, since A would already be deprecated by then), and
        category_id must not itself already be a merge target for other
        categories (no orphaned A->B, B->C leaving A stranded) -- rejecting
        outright in all cases rather than silently cascading a rewrite,
        matching the same fail-closed philosophy already used for claim
        merging (pipeline/claims.py). Marking the source deprecated is safe
        for existing claim resolution: _resolve_categories() (react_agent.py)
        always follows alias_of to the target BEFORE consulting status, so a
        deprecated-with-an-alias source never falls through to
        "uncategorized" -- only a deprecated category with no alias does."""
        if category_id == target_category_id:
            raise CategoryTransitionError("A category cannot be merged into itself")
        category = self._get_category_for_transition(category_id, allow_deprecated=True)
        target = self.get_category(target_category_id)
        if target is None:
            raise CategoryTransitionError(f"Target category {target_category_id} not found", code="not_found")
        if target.product_category != category.product_category:
            raise CategoryTransitionError("Cannot merge categories belonging to different product categories")
        if target.status == CategoryStatus.DEPRECATED:
            raise CategoryTransitionError(
                f"Target {target_category_id} is deprecated; merge into an active category instead"
            )
        if target.alias_of is not None:
            raise CategoryTransitionError(
                f"Target {target_category_id} is itself an alias of {target.alias_of}; "
                "merge into the root of that chain instead"
            )
        dependents = self.conn.execute(
            "SELECT COUNT(*) FROM canonical_categories WHERE alias_of = ?", (category_id,)
        ).fetchone()[0]
        if dependents:
            raise CategoryTransitionError(
                f"Category {category_id} is itself a merge target for {dependents} other categor{'y' if dependents == 1 else 'ies'}; "
                "merge those directly into the new target instead of chaining through this one"
            )
        now = utc_now()
        try:
            self.conn.execute(
                "UPDATE canonical_categories SET alias_of = ?, status = ?, updated_at = ? WHERE category_id = ?",
                (target_category_id, CategoryStatus.DEPRECATED.value, now, category_id),
            )
            self._write_category_audit_log(category_id, CategoryAuditAction.MERGE, {"target_category_id": target_category_id})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_category(category_id)  # type: ignore[return-value]

    def _get_category_for_transition(self, category_id: str, *, allow_deprecated: bool) -> CanonicalCategory:
        category = self.get_category(category_id)
        if category is None:
            raise CategoryTransitionError(f"Category {category_id} not found", code="not_found")
        if category.alias_of is not None:
            raise CategoryTransitionError(
                f"Category {category_id} is an alias of {category.alias_of}; act on the merge target instead"
            )
        if not allow_deprecated and category.status == CategoryStatus.DEPRECATED:
            raise CategoryTransitionError(f"Category {category_id} is already deprecated")
        return category

    def _write_category_audit_log(self, category_id: str, action: CategoryAuditAction, detail: dict[str, Any]) -> None:
        # Never commits itself -- always called from within a transition method
        # that commits once, after both this insert and its own UPDATE, so the
        # state change and its audit record land in the same transaction.
        self.conn.execute(
            "INSERT INTO category_audit_log (category_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (category_id, action.value, json.dumps(detail, ensure_ascii=False), utc_now()),
        )

    def list_category_audit_log(self, category_id: str) -> list[CategoryAuditLogEntry]:
        rows = self.conn.execute(
            "SELECT * FROM category_audit_log WHERE category_id = ? ORDER BY created_at ASC, id ASC", (category_id,)
        ).fetchall()
        return [self._row_to_audit_log_entry(row) for row in rows]

    def _row_to_category(self, row: sqlite3.Row) -> CanonicalCategory:
        return CanonicalCategory(
            category_id=row["category_id"],
            product_category=row["product_category"],
            canonical_label=row["canonical_label"],
            normalized_label=row["normalized_label"],
            status=CategoryStatus(row["status"]),
            alias_of=row["alias_of"],
            first_seen_aspect_raw=row["first_seen_aspect_raw"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_audit_log_entry(self, row: sqlite3.Row) -> CategoryAuditLogEntry:
        return CategoryAuditLogEntry(
            id=row["id"],
            category_id=row["category_id"],
            action=CategoryAuditAction(row["action"]),
            detail=json.loads(row["detail"]),
            created_at=row["created_at"],
        )
