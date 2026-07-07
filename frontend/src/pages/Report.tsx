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
              <th>方面</th>
              <th>出现次数</th>
              <th>代表性证据</th>
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
  if (!report) return <div className="page muted">加载中...</div>;

  return (
    <div className="page">
      <div className="page-header">
        <h1>商家优化报告</h1>
        <Link to={`/runs/${runId}`}>← 返回调研详情</Link>
      </div>

      <section className="card">
        <h2>情感分布</h2>
        <div className="sentiment-breakdown">
          {Object.entries(report.sentiment_breakdown).map(([sentiment, count]) => (
            <span key={sentiment} className={`sentiment-pill sentiment-${sentiment}`}>
              {sentiment}: {count}
            </span>
          ))}
        </div>
      </section>

      <AspectTable title="主要痛点" groups={report.top_pain_points} emptyText="未发现明显痛点。" />
      <AspectTable title="功能诉求" groups={report.feature_requests} emptyText="未发现明确的功能诉求。" />
      <AspectTable title="用户好评" groups={report.praised_aspects} emptyText="未发现明显好评。" />
      <AspectTable title="竞品对比" groups={report.competitor_mentions} emptyText="未发现竞品对比讨论。" />

      <section className="card">
        <h2>建议的产品优化行动</h2>
        <ul>
          {report.recommended_actions.map((action, index) => (
            <li key={index}>{action}</li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2>总结</h2>
        <pre className="summary-markdown">{report.summary_markdown}</pre>
      </section>
    </div>
  );
}
