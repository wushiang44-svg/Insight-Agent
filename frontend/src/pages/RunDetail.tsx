import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { RunDetail as RunDetailData } from "../api";
import { StatusBadge } from "../components/StatusBadge";
import { TraceTimeline } from "../components/TraceTimeline";

const ACTIVE_STATUSES = new Set(["planning", "searching", "summarizing"]);
const POLL_INTERVAL_MS = 2000;

const DATA_SOURCE_LABELS: Record<RunDetailData["run"]["data_source"], string> = {
  reddit_api: "Reddit API",
  json_upload: "JSON 上传",
};

export function RunDetail() {
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

  if (error) return <div className="page error">{error}</div>;
  if (!detail) return <div className="page muted">加载中...</div>;

  const { run, trace_events: traceEvents, is_running: isRunning } = detail;
  const isActive = ACTIVE_STATUSES.has(run.status);

  return (
    <div className="page">
      <div className="page-header">
        <h1>{run.product_category}</h1>
        <StatusBadge status={run.status} />
      </div>
      <div className="card run-summary">
        <div>数据来源：{DATA_SOURCE_LABELS[run.data_source]}</div>
        <div>
          搜索轮次：{run.iteration_count} / {run.max_iterations}
        </div>
        <div>已收集证据：{run.evidence_count}</div>
        <div>目标证据数：{run.min_evidence_target}</div>
        {run.error && <div className="error">错误：{run.error}</div>}
        {run.stop_reason && <div className="muted">停止原因：{run.stop_reason}</div>}
        <div className="actions">
          {isActive && isRunning && (
            <button onClick={handleStop} className="secondary">
              停止 Agent
            </button>
          )}
          {run.status === "completed" && <Link to={`/runs/${run.run_id}/report`}>查看商家报告 →</Link>}
        </div>
      </div>

      <h2>Agent 推理过程</h2>
      <TraceTimeline events={traceEvents} />
    </div>
  );
}
