import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { RunRecord } from "../api";
import { StatusBadge } from "../components/StatusBadge";

const DATA_SOURCE_LABELS: Record<RunRecord["data_source"], string> = {
  reddit_api: "Reddit API",
  json_upload: "JSON Upload",
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
        <h1>Research Runs</h1>
        <Link className="button-link" to="/new">
          + New Run
        </Link>
      </div>
      {error && <p className="error">{error}</p>}
      {runs === null && !error && <p className="muted">Loading...</p>}
      {runs !== null && runs.length === 0 && (
        <p className="muted">No research runs yet. Click the button above to start your first one.</p>
      )}
      {runs !== null && runs.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Product Category</th>
              <th>Data Source</th>
              <th>Status</th>
              <th>Iterations</th>
              <th>Evidence</th>
              <th>Created At</th>
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
