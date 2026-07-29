export interface BarRankItem {
  key: string;
  label: string;
  value: number;
  displayValue?: string;
  color?: string;
  // Generic, optional pill rendered after the label -- e.g. a review-status
  // marker. This component has no idea what the badge text means or why it's
  // there; deciding whether/what to pass is entirely the caller's business
  // logic (see Report.tsx's AspectSection, which only sets this for a
  // Claims-path entry with category_status === "proposed").
  badge?: string;
}

/**
 * Horizontal magnitude comparison. One hue per row (identity comes from the
 * caller — usually the whole list shares one category color), length encodes
 * the value. Bars share a track so relative magnitude reads at a glance.
 */
export function BarRank({ items, color = "var(--accent)" }: { items: BarRankItem[]; color?: string }) {
  const max = Math.max(...items.map((item) => item.value), 1);
  return (
    <div className="bar-rank">
      {items.map((item) => (
        <div className="bar-rank-row" key={item.key}>
          <div className="bar-rank-label" title={item.label}>
            <span className="bar-rank-label-text">{item.label}</span>
            {item.badge && <span className="bar-rank-badge">{item.badge}</span>}
          </div>
          <div className="bar-rank-track">
            <div
              className="bar-rank-fill"
              style={{ width: `${Math.max((item.value / max) * 100, 3)}%`, background: item.color ?? color }}
            />
          </div>
          <div className="bar-rank-value">{item.displayValue ?? item.value}</div>
        </div>
      ))}
    </div>
  );
}
