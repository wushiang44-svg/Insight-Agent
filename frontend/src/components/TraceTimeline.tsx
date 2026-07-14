import { Icon } from "./Icon";
import type { IconName } from "./Icon";
import type { TraceEvent } from "../api";
import { useLanguage } from "../lib/i18n";
import type { SourceMeta } from "../lib/sources";

const STEP_ICONS: Record<TraceEvent["step_type"], IconName> = {
  thought: "target",
  action_search: "search",
  observation: "inbox",
  sufficiency_check: "check",
  summary: "doc",
};

interface Round {
  iteration: number;
  thought?: TraceEvent;
  search?: TraceEvent;
  observation?: TraceEvent;
  sufficiency?: TraceEvent;
  summary?: TraceEvent;
  events: TraceEvent[];
}

function groupByRound(events: TraceEvent[]): Round[] {
  const rounds = new Map<number, Round>();
  for (const event of events) {
    let round = rounds.get(event.iteration);
    if (!round) {
      round = { iteration: event.iteration, events: [] };
      rounds.set(event.iteration, round);
    }
    round.events.push(event);
    if (event.step_type === "thought") round.thought = event;
    if (event.step_type === "action_search") round.search = event;
    if (event.step_type === "observation") round.observation = event;
    if (event.step_type === "sufficiency_check") round.sufficiency = event;
    if (event.step_type === "summary") round.summary = event;
  }
  return [...rounds.values()].sort((a, b) => a.iteration - b.iteration);
}

function payloadString(event: TraceEvent | undefined, key: string): string | undefined {
  if (!event) return undefined;
  const value = event.payload[key];
  return value === undefined || value === null || value === "" ? undefined : String(value);
}

function payloadNumber(event: TraceEvent | undefined, key: string): number | undefined {
  if (!event) return undefined;
  const value = event.payload[key];
  return typeof value === "number" ? value : undefined;
}

function RoundField({ icon, label, value }: { icon: IconName; label: string; value: string }) {
  return (
    <div className="round-field">
      <Icon name={icon} className="round-field-icon" />
      <div className="round-field-label">{label}</div>
      <div className="round-field-value">{value}</div>
    </div>
  );
}

function RoundCard({ round, meta }: { round: Round; meta: SourceMeta }) {
  const { t } = useLanguage();
  const query = payloadString(round.thought, "query");
  const subreddit = payloadString(round.thought, "subreddit");
  const where = subreddit ? `${meta.citationPrefix}${subreddit}` : t("detail.acrossAll", { source: meta.label });

  const searchError = payloadString(round.search, "error");
  const resultsValue = round.search
    ? searchError
      ? t("trace.searchFailed", { error: searchError })
      : t("trace.resultsFound", { n: payloadNumber(round.search, "items_returned") ?? 0 })
    : t("trace.waitingForSearch");

  const kept = payloadNumber(round.observation, "new_evidence");
  const total = payloadNumber(round.observation, "total_evidence");
  const filteringValue = round.observation ? t("trace.itemsRetained", { kept: kept ?? 0, total: total ?? 0 }) : t("trace.filteringNotRun");

  const isRoundDone = Boolean(round.sufficiency || round.summary);
  const isSufficient = round.sufficiency ? Boolean(round.sufficiency.payload.sufficient) : undefined;

  return (
    <div className="round-card">
      <div className="round-header">
        <span className="round-title">{t("trace.round", { n: round.iteration })}</span>
        <span className={`round-status ${isRoundDone ? "round-status-done" : "round-status-active"}`}>
          {isRoundDone ? t("trace.complete") : t("trace.inProgress")}
        </span>
      </div>

      <div className="round-fields">
        {round.thought && <RoundField icon="target" label={t("trace.goal")} value={round.thought.message} />}
        <RoundField icon="search" label={t("trace.search")} value={query ? `"${query}" · ${where}` : t("trace.notPlanned")} />
        <RoundField icon="inbox" label={t("trace.results")} value={resultsValue} />
        <RoundField icon="filter" label={t("trace.filtering")} value={filteringValue} />
        {round.sufficiency && (
          <div className="round-field">
            <Icon name="check" className="round-field-icon" />
            <div className="round-field-label">{t("trace.decision")}</div>
            <div className="round-field-value">
              <span
                className="severity-pill"
                style={{
                  background: isSufficient ? "var(--sev-low-bg)" : "var(--sev-medium-bg)",
                  color: isSufficient ? "var(--sev-low-fg)" : "var(--sev-medium-fg)",
                  marginRight: 8,
                }}
              >
                {isSufficient ? t("trace.stop") : t("trace.continue")}
              </span>
              {round.sufficiency.message}
            </div>
          </div>
        )}
        {round.summary && <RoundField icon="doc" label={t("trace.summary")} value={round.summary.message} />}
      </div>

      <details className="tech-details">
        <summary>{t("trace.viewDetails")}</summary>
        <ol className="trace-steps">
          {round.events.map((event, index) => (
            <li key={index} className={`trace-step trace-step-${event.step_type}`}>
              <Icon name={STEP_ICONS[event.step_type]} className="trace-step-icon" />
              <div className="trace-step-body">
                <div className="trace-step-title">{t(`trace.step.${event.step_type}`)}</div>
                <div className="trace-step-message">{event.message}</div>
                {Object.keys(event.payload).length > 0 && (
                  <pre className="trace-step-payload">{JSON.stringify(event.payload, null, 2)}</pre>
                )}
              </div>
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}

export function TraceTimeline({ events, meta }: { events: TraceEvent[]; meta: SourceMeta }) {
  const { t } = useLanguage();
  if (events.length === 0) {
    return <p className="muted">{t("trace.empty")}</p>;
  }

  const rounds = groupByRound(events);

  return (
    <div className="trace-timeline">
      {rounds.map((round) => (
        <RoundCard key={round.iteration} round={round} meta={meta} />
      ))}
    </div>
  );
}
