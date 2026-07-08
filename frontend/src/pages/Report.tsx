import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { AspectGroup, Report as ReportData } from "../api";

function AspectTable({ title, groups, emptyText }: { title: string; groups: AspectGroup[]; emptyText: string }) {
  return (
    <section className="card">
      <h2>{title}</h2>
      {groups.length === 0 ? (
        <p className="muted">{emptyText}</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Aspect</th>
              <th>Count</th>
              <th>Representative Evidence</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <tr key={group.aspect}>
                <td>{group.aspect}</td>
                <td>{group.count}</td>
                <td>
                  <ul className="quote-list">
                    {group.example_quotes.map((quote, index) => (
                      <li key={index}>
                        <a href={quote.source_url} target="_blank" rel="noreferrer">
                          {quote.quote}
                        </a>
                        <span className="muted"> (r/{quote.subreddit})</span>
                      </li>
                    ))}
                  </ul>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function Report() {
  const { runId } = useParams<{ runId: string }>();
  const [report, setReport] = useState<ReportData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId) return;
    api
      .getReport(runId)
      .then(setReport)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [runId]);

  if (error) return <div className="page error">{error}</div>;
  if (!report) return <div className="page muted">Loading...</div>;

  return (
    <div className="page">
      <div className="page-header">
        <h1>Merchant Optimization Report</h1>
        <Link to={`/runs/${runId}`}>← Back to run detail</Link>
      </div>

      <section className="card">
        <h2>Sentiment Breakdown</h2>
        <div className="sentiment-breakdown">
          {Object.entries(report.sentiment_breakdown).map(([sentiment, count]) => (
            <span key={sentiment} className={`sentiment-pill sentiment-${sentiment}`}>
              {sentiment}: {count}
            </span>
          ))}
        </div>
      </section>

      <AspectTable title="Top Pain Points" groups={report.top_pain_points} emptyText="No significant pain points found." />
      <AspectTable title="Feature Requests" groups={report.feature_requests} emptyText="No clear feature requests found." />
      <AspectTable title="Praise" groups={report.praised_aspects} emptyText="No significant praise found." />
      <AspectTable title="Competitor Mentions" groups={report.competitor_mentions} emptyText="No competitor comparisons found." />

      <section className="card">
        <h2>Recommended Product Improvements</h2>
        <ul>
          {report.recommended_actions.map((action, index) => (
            <li key={index}>{action}</li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2>Summary</h2>
        <pre className="summary-markdown">{report.summary_markdown}</pre>
      </section>
    </div>
  );
}
