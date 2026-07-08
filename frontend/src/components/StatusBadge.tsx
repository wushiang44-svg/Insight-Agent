import type { RunStatus } from "../api";

const LABELS: Record<RunStatus, string> = {
  planning: "Planning",
  searching: "Searching",
  summarizing: "Summarizing",
  completed: "Completed",
  failed: "Failed",
  stopped: "Stopped",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return <span className={`status-badge status-${status}`}>{LABELS[status]}</span>;
}
