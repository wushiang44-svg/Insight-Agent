import type { RunStatus } from "../api";

const LABELS: Record<RunStatus, string> = {
  planning: "规划中",
  searching: "搜索中",
  summarizing: "生成报告中",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return <span className={`status-badge status-${status}`}>{LABELS[status]}</span>;
}
