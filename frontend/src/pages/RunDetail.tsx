import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { RunDetail as RunDetailData } from "../api";
import { StatusBadge } from "../components/StatusBadge";
import { TraceTimeline } from "../components/TraceTimeline";
import { useLanguage } from "../lib/i18n";
import { useSourceMeta } from "../lib/sources";

const ACTIVE_STATUSES = new Set(["planning", "searching", "summarizing"]);
const POLL_INTERVAL_MS = 2000;

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

export function RunDetail() {
  const { t } = useLanguage();
  const { runId } = useParams<{ runId: string }>();
  const [detail, setDetail] = useState<RunDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;

    async function poll() {
      try {
        const data = await api.getRun(runId!);
        if (cancelled) return;
        setDetail(data);
        setError(null);
        if (ACTIVE_STATUSES.has(data.run.status)) {
          timerRef.current = window.setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    }

    poll();
    return () => {
      cancelled = true;
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [runId]);

  async function handleStop() {
    if (!runId) return;
    await api.stopRun(runId);
  }

  const meta = useSourceMeta(detail?.run.data_source);

  if (error) return <div className="page error">{error}</div>;
  if (!detail) return <div className="page muted">{t("detail.loading")}</div>;

  const { run, trace_events: traceEvents, is_running: isRunning } = detail;
  const isActive = ACTIVE_STATUSES.has(run.status);

  const roundProgress = clampPercent((run.iteration_count / Math.max(run.max_iterations, 1)) * 100);
  const evidenceProgress = clampPercent((run.evidence_count / Math.max(run.min_evidence_target, 1)) * 100);

  const lastThought = [...traceEvents].reverse().find((event) => event.step_type === "thought");
  const focusQuery = lastThought ? String(lastThought.payload.query ?? "") : "";
  const focusGroup = lastThought ? String(lastThought.payload.subreddit ?? "") : "";
  const currentFocus = focusQuery
    ? `"${focusQuery}" ${focusGroup ? t("detail.in", { group: `${meta.citationPrefix}${focusGroup}` }) : t("detail.acrossAll", { source: meta.label })}`
    : t("detail.notStarted");

  return (
    <div className="page">
      <div className="page-header">
        <h1>{run.product_category}</h1>
        <StatusBadge status={run.status} />
      </div>

      <div className="card progress-card">
        <div className="progress-row">
          <div className="progress-label">
            <span>{t("detail.round")}</span>
            <span>
              {run.iteration_count} / {run.max_iterations}
            </span>
          </div>
          <div className="progress-bar-track">
            <div className="progress-bar-fill" style={{ width: `${roundProgress}%` }} />
          </div>
        </div>

        <div className="progress-row">
          <div className="progress-label">
            <span>{t("detail.evidenceCollected")}</span>
            <span>
              {run.evidence_count} / {run.min_evidence_target}
            </span>
          </div>
          <div className="progress-bar-track">
            <div className="progress-bar-fill progress-bar-fill-alt" style={{ width: `${evidenceProgress}%` }} />
          </div>
        </div>

        <div className="progress-meta">
          <div>
            <span className="muted">{t("detail.currentFocus")}</span>
            {currentFocus}
          </div>
          <div>
            <span className="muted">{t("detail.dataSource")}</span>
            {meta.label}
          </div>
        </div>

        {run.error && (
          <div className="error">
            {t("detail.error")}
            {run.error}
          </div>
        )}
        {run.stop_reason && (
          <div className="muted">
            {t("detail.stopReason")}
            {run.stop_reason}
          </div>
        )}

        <div className="actions">
          {isActive && isRunning && (
            <button onClick={handleStop} className="secondary">
              {t("detail.stopAgent")}
            </button>
          )}
          {run.status === "completed" && <Link to={`/runs/${run.run_id}/report`}>{t("detail.viewReport")}</Link>}
        </div>
      </div>

      <h2>{t("detail.timeline")}</h2>
      <TraceTimeline events={traceEvents} meta={meta} />
    </div>
  );
}
