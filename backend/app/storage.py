from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import (
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
                confidence REAL NOT NULL
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
        )
        self.conn.execute(
            """
            INSERT INTO runs (
                run_id, product_category, keywords, target_subreddits, status,
                iteration_count, max_iterations, min_evidence_target, evidence_count,
                created_at, updated_at, data_source, stop_reason, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                aspect, sentiment, quote, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self.conn.execute(
            """
            INSERT OR REPLACE INTO reports (
                run_id, generated_at, top_pain_points, feature_requests, praised_aspects,
                competitor_mentions, sentiment_breakdown, recommended_actions, summary_markdown,
                subreddits, subreddit_counts, recommended_actions_zh, summary_markdown_zh
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        )
