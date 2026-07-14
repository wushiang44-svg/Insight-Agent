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
}

/**
 * Priority Score (0-100), per aspect group: how it stacks up against the
 * single most-mentioned aspect of its type (55%), how many distinct
 * communities mention it (20%), how negative it reads (15%), and how
 * confident the analyst was (10%). Frequency is normalized against the
 * *max* count for the type rather than the sum — with many aspects in play,
 * a sum-based share buries every aspect near zero even when one is a real,
 * widely-corroborated complaint. Directional, not mathematically precise —
 * tuned to rank "what to fix first," not to be audited.
 */
export function priorityScore(group: AggregateForScoring, maxCountForType: number): number {
  const frequencyDominance = maxCountForType > 0 ? group.count / maxCountForType : 0;
  const subredditBonus = Math.min(group.subreddit_count ?? 1, 4) / 4;
  const sentimentCounts = group.sentiment_counts ?? {};
  const negativeCount = sentimentCounts.negative ?? 0;
  const negativeIntensity = group.count > 0 ? negativeCount / group.count : 0;
  const confidence = group.avg_confidence ?? 0.5;

  const raw = 55 * frequencyDominance + 20 * subredditBonus + 15 * negativeIntensity + 10 * confidence;
  return Math.max(0, Math.min(100, Math.round(raw)));
}
