import type { CSSProperties, ReactNode } from "react";

export function KpiCard({
  label,
  value,
  sublabel,
  accent,
  size = "md",
  children,
}: {
  label: string;
  value: ReactNode;
  sublabel?: ReactNode;
  accent?: string;
  size?: "md" | "lg";
  children?: ReactNode;
}) {
  return (
    <div
      className={`kpi-card ${size === "lg" ? "kpi-card-lg" : ""}`}
      style={accent ? ({ "--kpi-accent": accent } as CSSProperties) : undefined}
    >
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sublabel && <div className="kpi-sublabel">{sublabel}</div>}
      {children}
    </div>
  );
}
