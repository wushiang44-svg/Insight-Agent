import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { AspectGroup, Report as ReportData, RunRecord } from "../api";
import { BarRank } from "../components/BarRank";
import { Donut } from "../components/Donut";
import { Icon } from "../components/Icon";
import { KpiCard } from "../components/KpiCard";
import { CATEGORY_STYLES, SENTIMENT_COLORS } from "../lib/categories";
import type { Category } from "../lib/categories";
import { healthLabel, healthScore, priorityScore, severityFromScore, starRating, SEVERITY_STYLES } from "../lib/insights";
import type { SeverityLevel } from "../lib/insights";
import { useLanguage } from "../lib/i18n";
import type { Language } from "../lib/i18n";
import { useSourceMeta } from "../lib/sources";
import type { SourceMeta } from "../lib/sources";
import { translateAspect } from "../lib/aspectTranslations";

interface PriorityItem {
  key: string;
  aspect: string;
  category: Extract<Category, "pain_point" | "feature_request">;
  count: number;
  percentOfType: number;
  score: number;
  severity: SeverityLevel;
  group: AspectGroup;
}

function buildPriorities(painPoints: AspectGroup[], featureRequests: AspectGroup[]): PriorityItem[] {
  const painTotal = painPoints.reduce((sum, group) => sum + group.count, 0);
  const featureTotal = featureRequests.reduce((sum, group) => sum + group.count, 0);
  const painMax = Math.max(...painPoints.map((group) => group.count), 1);
  const featureMax = Math.max(...featureRequests.map((group) => group.count), 1);

  const toItem = (
    group: AspectGroup,
    category: PriorityItem["category"],
    totalForType: number,
    maxForType: number,
  ): PriorityItem => {
    const score = priorityScore(group, maxForType);
    return {
      key: `${category}:${group.aspect}`,
      aspect: group.aspect,
      category,
      count: group.count,
      percentOfType: totalForType > 0 ? Math.round((group.count / totalForType) * 100) : 0,
      score,
      severity: severityFromScore(score),
      group,
    };
  };

  return [
    ...painPoints.map((group) => toItem(group, "pain_point", painTotal, painMax)),
    ...featureRequests.map((group) => toItem(group, "feature_request", featureTotal, featureMax)),
  ].sort((a, b) => b.score - a.score);
}

function whyBullets(item: PriorityItem, meta: SourceMeta, t: (key: string, vars?: Record<string, string | number>) => string): string[] {
  const bullets: string[] = [];
  const key = item.category === "pain_point" ? "why.percentOfComplaints" : "why.percentOfFeatureRequests";
  bullets.push(t(key, { percent: item.percentOfType, count: item.count, s: item.count === 1 ? "" : "s" }));

  const groupCount = item.group.subreddit_count ?? 1;
  if (groupCount > 1) {
    bullets.push(t("why.mentionedAcross", { count: groupCount, group: meta.groupLabel.toLowerCase() }));
  }

  if (item.category === "pain_point") {
    const sentimentCounts = item.group.sentiment_counts ?? {};
    const negative = sentimentCounts.negative ?? 0;
    const negativePercent = item.count > 0 ? Math.round((negative / item.count) * 100) : 0;
    if (negativePercent >= 50) {
      bullets.push(t("why.negativeSentiment", { percent: negativePercent }));
    }
  }

  if ((item.group.avg_confidence ?? 0) >= 0.8) {
    bullets.push(t("why.highConfidence"));
  }

  return bullets;
}

function AspectSection({
  category,
  groups,
  meta,
}: {
  category: Category;
  groups: AspectGroup[];
  meta: SourceMeta;
}) {
  const { t, language } = useLanguage();
  const style = CATEGORY_STYLES[category];
  const total = groups.reduce((sum, group) => sum + group.count, 0);

  return (
    <section className="card">
      <div className="section-header">
        <span className="section-dot" style={{ background: style.color }} />
        <h2>{t(`category.${category}.section`)}</h2>
        <span className="section-count">{groups.length === 1 ? t("report.aspect.one") : t("report.aspect.many", { n: groups.length })}</span>
      </div>

      {groups.length === 0 ? (
        <p className="muted">{t(`category.${category}.empty`)}</p>
      ) : (
        <>
          <BarRank
            color={style.color}
            items={groups.map((group) => ({
              key: group.aspect,
              label: translateAspect(group.aspect, language),
              value: group.count,
              displayValue: total > 0 ? `${Math.round((group.count / total) * 100)}%` : `${group.count}`,
            }))}
          />

          <div className="evidence-groups" style={{ marginTop: "var(--space-4)" }}>
            {groups.map((group) => (
              <details className="evidence-group" key={group.aspect}>
                <summary>
                  <span className="evidence-group-dot" style={{ background: style.color }} />
                  <span className="evidence-group-aspect">{translateAspect(group.aspect, language)}</span>
                  <span className="evidence-group-count">
                    {group.example_quotes.length} {group.example_quotes.length === 1 ? meta.itemNounSingular : meta.itemNoun}
                  </span>
                  <Icon name="chevron-right" className="evidence-group-chevron" />
                </summary>
                <div className="evidence-group-body">
                  {group.example_quotes.map((quote, index) => (
                    <div className="evidence-quote" key={index}>
                      <a href={quote.source_url} target="_blank" rel="noreferrer" title={t("report.quoteOriginal")}>
                        {quote.quote}
                      </a>{" "}
                      <span className="evidence-quote-source muted">
                        ({meta.citationPrefix}
                        {quote.subreddit})
                      </span>
                    </div>
                  ))}
                </div>
              </details>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function pickNarrative(report: ReportData, language: Language): { summary: string; actions: string[]; isFallbackToEnglish: boolean } {
  if (language === "zh" && report.summary_markdown_zh) {
    return { summary: report.summary_markdown_zh, actions: report.recommended_actions_zh, isFallbackToEnglish: false };
  }
  return { summary: report.summary_markdown, actions: report.recommended_actions, isFallbackToEnglish: language === "zh" };
}

export function Report() {
  const { t, language } = useLanguage();
  const { runId } = useParams<{ runId: string }>();
  const [report, setReport] = useState<ReportData | null>(null);
  const [run, setRun] = useState<RunRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAllPriorities, setShowAllPriorities] = useState(false);
  const meta = useSourceMeta(run?.data_source);

  useEffect(() => {
    if (!runId) return;
    Promise.all([api.getReport(runId), api.getRun(runId)])
      .then(([reportData, runData]) => {
        setReport(reportData);
        setRun(runData.run);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [runId]);

  const priorities = useMemo(
    () => (report ? buildPriorities(report.top_pain_points, report.feature_requests) : []),
    [report],
  );
  const roadmap = priorities.slice(0, 5);
  const visiblePriorities = showAllPriorities ? priorities : priorities.slice(0, 5);

  if (error) return <div className="page error">{error}</div>;
  if (!report) return <div className="page muted">{t("detail.loading")}</div>;

  const health = healthScore(report.sentiment_breakdown);
  const healthInfo = healthLabel(health);
  const healthStyle = SEVERITY_STYLES[healthInfo.severity];
  const narrative = pickNarrative(report, language);

  const totalSentiment = Object.values(report.sentiment_breakdown).reduce((sum, value) => sum + value, 0);
  const positivePercent = totalSentiment > 0 ? Math.round(((report.sentiment_breakdown.positive ?? 0) / totalSentiment) * 100) : 0;
  const neutralPercent = totalSentiment > 0 ? Math.round(((report.sentiment_breakdown.neutral ?? 0) / totalSentiment) * 100) : 0;
  const negativePercent = totalSentiment > 0 ? Math.round(((report.sentiment_breakdown.negative ?? 0) / totalSentiment) * 100) : 0;

  const subredditItems = Object.entries(report.subreddit_counts)
    .sort((a, b) => b[1] - a[1])
    .map(([subreddit, count]) => ({ key: subreddit, label: `${meta.citationPrefix}${subreddit}`, value: count }));

  return (
    <div className="page">
      <div className="page-header">
        <h1>{run ? t("report.title", { category: run.product_category }) : t("report.titleFallback")}</h1>
        <Link to={`/runs/${runId}`}>{t("report.backToRun")}</Link>
      </div>

      <div className="kpi-grid">
        <KpiCard label={t("report.health")} value={`${health}/100`} size="lg" accent={healthStyle.bg}>
          <div className="kpi-delta" style={{ color: healthStyle.bg }}>
            {t(healthInfo.key)}
          </div>
        </KpiCard>

        <KpiCard label={t("report.sentiment")} value={`${positivePercent}% positive`} sublabel={`${positivePercent}% pos · ${neutralPercent}% neu · ${negativePercent}% neg`}>
          <div className="stack-bar">
            {positivePercent > 0 && <div className="stack-bar-segment" style={{ width: `${positivePercent}%`, background: SENTIMENT_COLORS.positive }} />}
            {neutralPercent > 0 && <div className="stack-bar-segment" style={{ width: `${neutralPercent}%`, background: SENTIMENT_COLORS.neutral }} />}
            {negativePercent > 0 && <div className="stack-bar-segment" style={{ width: `${negativePercent}%`, background: SENTIMENT_COLORS.negative }} />}
          </div>
        </KpiCard>

        <KpiCard label={t("report.evidenceCollected")} value={totalSentiment} sublabel={t("report.itemsAnalyzed", { noun: meta.itemNoun })} />
        <KpiCard label={t("report.covered", { group: meta.groupLabel })} value={report.subreddits.length} sublabel={meta.groupLabel.toLowerCase()} />
        {run && <KpiCard label={t("report.researchRounds")} value={`${run.iteration_count} / ${run.max_iterations}`} sublabel={t("report.iterations")} />}
      </div>

      <section className="card">
        <div className="section-header">
          <h2>{t("report.topPriorities")}</h2>
          <span className="section-count">{t("report.topPrioritiesHint")}</span>
        </div>
        {priorities.length === 0 ? (
          <p className="muted">{t("report.noPriorities")}</p>
        ) : (
          <>
            <div className="priority-list">
              {visiblePriorities.map((item, index) => {
                const severity = SEVERITY_STYLES[item.severity];
                const catStyle = CATEGORY_STYLES[item.category];
                const topQuote = item.group.example_quotes[0];
                return (
                  <div className="priority-row" key={item.key}>
                    <div className="priority-row-head">
                      <span className="rank-badge" style={{ background: severity.bg, color: severity.fg }}>
                        {index + 1}
                      </span>
                      <span className="priority-title">{translateAspect(item.aspect, language)}</span>
                      <span className="severity-pill" style={{ background: severity.bg, color: severity.fg }}>
                        {t(`severity.${item.severity}`)}
                      </span>
                      <span className="priority-score">{t("report.priority", { n: item.score })}</span>
                    </div>
                    <BarRank
                      color={catStyle.color}
                      items={[{ key: item.key, label: t(`category.${item.category}.label`), value: item.score, displayValue: `${item.score}` }]}
                    />
                    <div className="priority-meta">{whyBullets(item, meta, t).join(" · ")}</div>
                    {topQuote && (
                      <p className="priority-quote">
                        "{topQuote.quote}" — {meta.citationPrefix}
                        {topQuote.subreddit}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
            {priorities.length > 5 && (
              <button className="show-more-button" onClick={() => setShowAllPriorities((value) => !value)} style={{ marginTop: "var(--space-3)" }}>
                {showAllPriorities ? t("report.showFewer") : t("report.showMore", { n: priorities.length - 5 })}
              </button>
            )}
          </>
        )}
      </section>

      <div className="two-col">
        <section className="card">
          <h2>{t("report.sentiment")}</h2>
          <Donut breakdown={report.sentiment_breakdown} />
        </section>
        <section className="card">
          <h2>{t("report.coverage", { group: meta.groupLabel })}</h2>
          {subredditItems.length === 0 ? (
            <p className="muted">{t("report.noGroupData", { group: meta.groupLabelSingular })}</p>
          ) : (
            <BarRank items={subredditItems} color="var(--cat-feature)" />
          )}
        </section>
      </div>

      <AspectSection category="pain_point" groups={report.top_pain_points} meta={meta} />
      <AspectSection category="feature_request" groups={report.feature_requests} meta={meta} />
      <AspectSection category="praise" groups={report.praised_aspects} meta={meta} />
      <AspectSection category="comparison" groups={report.competitor_mentions} meta={meta} />

      <section className="card">
        <div className="section-header">
          <h2>{t("report.roadmap")}</h2>
        </div>
        {roadmap.length === 0 ? (
          <p className="muted">{t("report.noRoadmap")}</p>
        ) : (
          <div className="roadmap-list">
            {roadmap.map((item, index) => {
              const stars = starRating(item.score);
              const action = item.category === "pain_point" ? t("report.fix") : t("report.build");
              const topQuote = item.group.example_quotes[0];
              return (
                <div className="roadmap-card" key={item.key}>
                  <div className="roadmap-head">
                    <div>
                      <div className="roadmap-priority-label">{t("report.priority", { n: index + 1 })}</div>
                      <div className="roadmap-title">
                        {action}: {translateAspect(item.aspect, language)}
                      </div>
                    </div>
                    <div>
                      <div className="round-field-label" style={{ width: "auto" }}>
                        {t("report.expectedImpact")}
                      </div>
                      <span className="impact-dots">
                        {Array.from({ length: 5 }, (_, dotIndex) => (
                          <span key={dotIndex} className={`impact-dot ${dotIndex < stars ? "impact-dot-filled" : ""}`} />
                        ))}
                      </span>
                    </div>
                  </div>
                  <ul className="roadmap-why">
                    {whyBullets(item, meta, t).map((bullet, bulletIndex) => (
                      <li key={bulletIndex}>{bullet}</li>
                    ))}
                  </ul>
                  {topQuote && (
                    <div className="roadmap-evidence">
                      "{topQuote.quote}" — {meta.citationPrefix}
                      {topQuote.subreddit}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="card">
        <details className="tech-details">
          <summary>{t("report.fullSummary")}</summary>
          {narrative.isFallbackToEnglish && <p className="muted">{t("report.narrativeFallbackNote")}</p>}
          {narrative.actions.length > 0 && (
            <>
              <div className="round-field-label" style={{ width: "auto", marginBottom: 6 }}>
                {t("report.recommendedActions")}
              </div>
              <ul className="roadmap-why" style={{ marginBottom: 12 }}>
                {narrative.actions.map((action, index) => (
                  <li key={index}>{action}</li>
                ))}
              </ul>
            </>
          )}
          <pre className="summary-markdown">{narrative.summary}</pre>
        </details>
      </section>

      <section className="card">
        <details className="tech-details">
          <summary>{t("report.techAppendix")}</summary>
          <pre className="trace-step-payload">
            {JSON.stringify(
              { run_id: report.run_id, generated_at: report.generated_at, subreddits: report.subreddits, subreddit_counts: report.subreddit_counts },
              null,
              2,
            )}
          </pre>
        </details>
      </section>
    </div>
  );
}
