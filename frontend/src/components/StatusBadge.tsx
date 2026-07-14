import type { RunStatus } from "../api";
import { useLanguage } from "../lib/i18n";

export function StatusBadge({ status }: { status: RunStatus }) {
  const { t } = useLanguage();
  return <span className={`status-badge status-${status}`}>{t(`status.${status}`)}</span>;
}
