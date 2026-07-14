export interface BarRankItem {
  key: string;
  label: string;
  value: number;
  displayValue?: string;
  color?: string;
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
            {item.label}
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
