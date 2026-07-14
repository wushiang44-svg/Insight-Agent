import type { ReactNode } from "react";

export type IconName =
  | "target"
  | "search"
  | "inbox"
  | "filter"
  | "check"
  | "doc"
  | "trophy"
  | "trend-up"
  | "alert-triangle"
  | "lightbulb"
  | "chevron-right"
  | "layers";

const PATHS: Record<IconName, ReactNode> = {
  target: (
    <>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="5" />
      <circle cx="12" cy="12" r="1" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </>
  ),
  inbox: (
    <>
      <path d="M3 12h4l2 3h6l2-3h4" />
      <path d="M5 12 3.5 6.5A2 2 0 0 1 5.4 4h13.2a2 2 0 0 1 1.9 2.5L19 12" />
      <path d="M3 12v6a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6" />
    </>
  ),
  filter: <path d="M4 5h16l-6 7.5V19l-4 2v-8.5z" />,
  check: <path d="M20 6 9 17l-5-5" />,
  doc: (
    <>
      <path d="M7 3h7l4 4v14a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" />
      <path d="M14 3v4h4" />
      <path d="M9 13h6M9 17h6" />
    </>
  ),
  trophy: (
    <>
      <path d="M8 4h8v6a4 4 0 0 1-8 0z" />
      <path d="M8 5H5a2 2 0 0 0 2 4M16 5h3a2 2 0 0 1-2 4" />
      <path d="M12 14v3M9 21h6M9.5 21c0-2 .8-3 2.5-4 1.7 1 2.5 2 2.5 4" />
    </>
  ),
  "trend-up": (
    <>
      <path d="M3 17l6-6 4 4 8-8" />
      <path d="M15 7h6v6" />
    </>
  ),
  "alert-triangle": (
    <>
      <path d="M12 3.5 21.5 20h-19z" />
      <path d="M12 10v4" />
      <path d="M12 17h.01" />
    </>
  ),
  lightbulb: (
    <>
      <path d="M9 18h6M10 21h4" />
      <path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.3 1 2.1h5c0-.8.4-1.6 1-2.1A6 6 0 0 0 12 3z" />
    </>
  ),
  "chevron-right": <path d="M9 6l6 6-6 6" />,
  layers: (
    <>
      <path d="M12 3 3 8l9 5 9-5z" />
      <path d="M3 13l9 5 9-5" />
    </>
  ),
};

export function Icon({ name, className }: { name: IconName; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={`icon ${className ?? ""}`} aria-hidden="true">
      {PATHS[name]}
    </svg>
  );
}
