import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { RunRecord } from "../api";
import { StatusBadge } from "../components/StatusBadge";

const DATA_SOURCE_LABELS: Record<RunRecord["data_source"], string> = {
  reddit_api: "Reddit API",
  json_upload: "JSON 上传",
};

export function RunsList() {
  const [runs, setRuns] = useState<RunRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listRuns()
      .then((data) => {
        if (!cancelled) setRuns(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="page">
      <div className="page-header">
        <h1>调研任务</h1>
        <Link className="button-link" to="/new">
          + 新建调研
        </Link>
      </div>
      {error && <p className="error">{error}</p>}
      {runs === null && !error && <p className="muted">加载中...</p>}
      {runs !== null && runs.length === 0 && (
        <p className="muted">还没有任何调研任务，点击右上角开始第一次调研。</p>
      )}
      {runs !== null && runs.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>产品类目</th>
              <th>数据来源</th>
              <th>状态</th>
              <th>轮次</th>
              <th>证据数</th>
              <th>创建时间</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td>
                  <Link to={`/runs/${run.run_id}`}>{run.product_category}</Link>
                </td>
                <td>{DATA_SOURCE_LABELS[run.data_source]}</td>
                <td>
                  <StatusBadge status={run.status} />
                </td>
                <td>
                  {run.iteration_count} / {run.max_iterations}
                </td>
                <td>{run.evidence_count}</td>
                <td>{run.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
