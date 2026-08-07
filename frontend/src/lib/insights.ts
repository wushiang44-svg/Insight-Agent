export type SeverityLevel = "critical" | "high" | "medium" | "low";

export interface SeverityStyle {
  bg: string;
  fg: string;
}

// Fixed status scale (never themed) — always paired with a text label, never color
// alone. The label text lives in i18n.ts under `severity.<level>` — look it up
// with `t()` rather than reading it here.
export const SEVERITY_STYLES: Record<SeverityLevel, SeverityStyle> = {
  critical: { bg: "var(--sev-critical-bg)", fg: "var(--sev-critical-fg)" },
  high: { bg: "var(--sev-high-bg)", fg: "var(--sev-high-fg)" },
  medium: { bg: "var(--sev-medium-bg)", fg: "var(--sev-medium-fg)" },
  low: { bg: "var(--sev-low-bg)", fg: "var(--sev-low-fg)" },
};

export function severityFromScore(score: number): SeverityLevel {
  if (score >= 85) return "critical";
  if (score >= 70) return "high";
  if (score >= 50) return "medium";
  return "low";
}

export function starRating(score: number): number {
  if (score >= 85) return 5;
  if (score >= 70) return 4;
  if (score >= 50) return 3;
  if (score >= 30) return 2;
  return 1;
}

export interface SentimentBreakdown {
  positive?: number;
  neutral?: number;
  negative?: number;
  [key: string]: number | undefined;
}

/**
 * Product Health (0-100): a weighted read of the sentiment mix alone.
 * Positive counts fully, neutral counts half, negative counts zero.
 * Directional business metric, not a scientific one.
 */
export function healthScore(breakdown: SentimentBreakdown): number {
  const positive = breakdown.positive ?? 0;
  const neutral = breakdown.neutral ?? 0;
  const negative = breakdown.negative ?? 0;
  const total = positive + neutral + negative;
  if (total === 0) return 0;
  return Math.round((100 * (positive * 1 + neutral * 0.5 + negative * 0)) / total);
}

/** Returns an i18n key (`t("health.good")` etc.) rather than display text, so this stays language-agnostic. */
export function healthLabel(score: number): { key: string; severity: SeverityLevel } {
  if (score >= 70) return { key: "health.good", severity: "low" };
  if (score >= 45) return { key: "health.attention", severity: "medium" };
  return { key: "health.atRisk", severity: "critical" };
}

export interface AggregateForScoring {
  count: number;
  subreddit_count?: number;
  avg_confidence?: number;
  sentiment_counts?: Record<string, number>;
  // Milestone 3 / A3 -- backend-computed thread-concentration-dampened
  // ranking signal (backend/app/react_agent.py's _weighted_count()).
  // Optional/backward-compatible: an older payload without it falls back to
  // raw `count`, matching pre-Milestone-3 behavior exactly, never crashing.
  weighted_count?: number;
  thread_count?: number;
}

/**
 * Priority Score (0-100), per aspect group: how it stacks up against the
 * single most-mentioned aspect of its type (55%), how many distinct
 * communities mention it (20%), how negative it reads (15%), and how
 * confident the analyst was (10%). Frequency is normalized against the
 * *max weighted count* for the type rather than the sum — with many aspects
 * in play, a sum-based share buries every aspect near zero even when one is
 * a real, widely-corroborated complaint. Directional, not mathematically
 * precise — tuned to rank "what to fix first," not to be audited.
 *
 * Milestone 3 / A3: frequency dominance is computed from `weighted_count`
 * (thread-dampened), not raw `count` — the SAME signal backend/app/
 * react_agent.py's _build_report_inputs() already sorts by, computed once
 * there and consumed here rather than re-derived, per the approved
 * backend/frontend consistency strategy (never re-implement the dampening
 * math in TypeScript; only the tiny comparator below is duplicated, and an
 * end-to-end test proves it agrees with the backend's own order).
 */
export function priorityScore(group: AggregateForScoring, maxWeightedCountForType: number): number {
  const weightedCount = group.weighted_count ?? group.count;
  const frequencyDominance = maxWeightedCountForType > 0 ? weightedCount / maxWeightedCountForType : 0;
  const subredditBonus = Math.min(group.subreddit_count ?? 1, 4) / 4;
  const sentimentCounts = group.sentiment_counts ?? {};
  const negativeCount = sentimentCounts.negative ?? 0;
  const negativeIntensity = group.count > 0 ? negativeCount / group.count : 0;
  const confidence = group.avg_confidence ?? 0.5;

  const raw = 55 * frequencyDominance + 20 * subredditBonus + 15 * negativeIntensity + 10 * confidence;
  return Math.max(0, Math.min(100, Math.round(raw)));
}

export interface RankableAggregate {
  aspect: string;
  count: number;
  weighted_count?: number;
  thread_count?: number;
}

/**
 * Milestone 3 / A3 deterministic tiebreak — mirrors backend/app/
 * react_agent.py's _build_report_inputs() sort key EXACTLY:
 * weighted_count desc -> thread_count desc -> raw count desc -> label asc.
 * Used whenever priorityScore() lands two groups on the same score (common —
 * see the real run_55025c50e81b replay, where most of the top pain-point
 * categories tie at the same dampened score), so the frontend "roadmap"
 * ordering can never silently diverge from the backend's bar-chart/narrative
 * ordering for groups of the same type. This is the one piece of logic
 * intentionally duplicated across languages (the dampening math itself is
 * not) — see insights.test.ts's consistency test for the empirical proof
 * both sides agree.
 */
export function compareByThreadDampenedRank(a: RankableAggregate, b: RankableAggregate): number {
  const weightedA = a.weighted_count ?? a.count;
  const weightedB = b.weighted_count ?? b.count;
  if (weightedA !== weightedB) return weightedB - weightedA;
  const threadsA = a.thread_count ?? 1;
  const threadsB = b.thread_count ?? 1;
  if (threadsA !== threadsB) return threadsB - threadsA;
  if (a.count !== b.count) return b.count - a.count;
  return a.aspect.localeCompare(b.aspect);
}
