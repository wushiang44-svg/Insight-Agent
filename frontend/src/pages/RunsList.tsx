import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { RunRecord } from "../api";
import { StatusBadge } from "../components/StatusBadge";
import { useLanguage } from "../lib/i18n";
import { useSourceMeta } from "../lib/sources";

function DataSourceCell({ dataSource }: { dataSource: RunRecord["data_source"] }) {
  return <td>{useSourceMeta(dataSource).label}</td>;
}

export function RunsList() {
  const { t } = useLanguage();
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
        <h1>{t("runs.title")}</h1>
        <Link className="button-link" to="/new">
          {t("nav.newRun")}
        </Link>
      </div>
      {error && <p className="error">{error}</p>}
      {runs === null && !error && <p className="muted">{t("runs.loading")}</p>}
      {runs !== null && runs.length === 0 && <p className="muted">{t("runs.empty")}</p>}
      {runs !== null && runs.length > 0 && (
        <div className="card table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>{t("runs.col.productCategory")}</th>
                <th>{t("runs.col.dataSource")}</th>
                <th>{t("runs.col.status")}</th>
                <th>{t("runs.col.iterations")}</th>
                <th>{t("runs.col.evidence")}</th>
                <th>{t("runs.col.createdAt")}</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id}>
                  <td>
                    <Link to={`/runs/${run.run_id}`}>{run.product_category}</Link>
                  </td>
                  <DataSourceCell dataSource={run.data_source} />
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
        </div>
      )}
    </div>
  );
}
