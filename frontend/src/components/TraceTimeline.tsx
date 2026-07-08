import type { TraceEvent } from "../api";

const STEP_LABELS: Record<TraceEvent["step_type"], string> = {
  thought: "Thought",
  action_search: "Search",
  observation: "Observation / Filter",
  sufficiency_check: "Sufficiency Check",
  summary: "Summary",
};

const STEP_ICONS: Record<TraceEvent["step_type"], string> = {
  thought: "💭",
  action_search: "🔍",
  observation: "🧪",
  sufficiency_check: "⚖️",
  summary: "📄",
};

export function TraceTimeline({ events }: { events: TraceEvent[] }) {
  if (events.length === 0) {
    return <p className="muted">The agent hasn't produced any steps yet.</p>;
  }

  const grouped = new Map<number, TraceEvent[]>();
  for (const event of events) {
    const bucket = grouped.get(event.iteration) ?? [];
    bucket.push(event);
    grouped.set(event.iteration, bucket);
  }

  return (
    <div className="trace-timeline">
      {[...grouped.entries()].map(([iteration, iterationEvents]) => (
        <div key={iteration} className="trace-iteration">
          <div className="trace-iteration-header">Round {iteration}</div>
          <ol className="trace-steps">
            {iterationEvents.map((event, index) => (
              <li key={index} className={`trace-step trace-step-${event.step_type}`}>
                <span className="trace-step-icon">{STEP_ICONS[event.step_type]}</span>
                <div className="trace-step-body">
                  <div className="trace-step-title">{STEP_LABELS[event.step_type]}</div>
                  <div className="trace-step-message">{event.message}</div>
                  {Object.keys(event.payload).length > 0 && (
                    <pre className="trace-step-payload">{JSON.stringify(event.payload, null, 2)}</pre>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}
