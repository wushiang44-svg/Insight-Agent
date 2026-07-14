function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

export function ProgressBar({ percent, color = "var(--accent)" }: { percent: number; color?: string }) {
  return (
    <div className="progress-bar-track">
      <div className="progress-bar-fill" style={{ width: `${clampPercent(percent)}%`, background: color }} />
    </div>
  );
}
