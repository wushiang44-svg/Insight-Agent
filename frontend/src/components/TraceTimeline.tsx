import type { TraceEvent } from "../api";

const STEP_LABELS: Record<TraceEvent["step_type"], string> = {
  thought: "思考",
  action_search: "搜索",
  observation: "观察 / 筛选",
  sufficiency_check: "信息是否充分",
  summary: "生成报告",
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
    return <p className="muted">Agent 尚未产生任何步骤。</p>;
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
          <div className="trace-iteration-header">第 {iteration} 轮</div>
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
