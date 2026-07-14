import { SENTIMENT_COLORS } from "../lib/categories";

const ORDER: Array<"negative" | "neutral" | "positive"> = ["negative", "neutral", "positive"];
const LABELS: Record<(typeof ORDER)[number], string> = {
  negative: "Negative",
  neutral: "Neutral",
  positive: "Positive",
};

export function Donut({ breakdown }: { breakdown: Record<string, number> }) {
  const total = ORDER.reduce((sum, key) => sum + (breakdown[key] ?? 0), 0);

  let cursor = 0;
  const stops: string[] = [];
  for (const key of ORDER) {
    const value = breakdown[key] ?? 0;
    if (value === 0) continue;
    const start = (cursor / Math.max(total, 1)) * 360;
    cursor += value;
    const end = (cursor / Math.max(total, 1)) * 360;
    stops.push(`${SENTIMENT_COLORS[key]} ${start}deg ${end}deg`);
  }

  const gradient = total > 0 ? `conic-gradient(${stops.join(", ")})` : "conic-gradient(var(--track) 0deg 360deg)";
  const dominant = ORDER.reduce((best, key) => ((breakdown[key] ?? 0) > (breakdown[best] ?? 0) ? key : best), "neutral" as (typeof ORDER)[number]);
  const dominantPercent = total > 0 ? Math.round(((breakdown[dominant] ?? 0) / total) * 100) : 0;

  return (
    <div className="donut-row">
      <div className="donut" style={{ background: gradient }}>
        <div className="donut-center">
          <div className="donut-center-value">{dominantPercent}%</div>
          <div className="donut-center-label">{LABELS[dominant]}</div>
        </div>
      </div>
      <div className="legend">
        {ORDER.map((key) => {
          const value = breakdown[key] ?? 0;
          const percent = total > 0 ? Math.round((value / total) * 100) : 0;
          return (
            <div className="legend-row" key={key}>
              <span className="legend-dot" style={{ background: SENTIMENT_COLORS[key] }} />
              <span>{LABELS[key]}</span>
              <span className="legend-value">{percent}%</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
