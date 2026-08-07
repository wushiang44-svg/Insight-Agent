import type { TraceEvent } from "../api";

// Mirrors the CHALLENGE_REASON_* constants in
// backend/app/collectors/reddit_browser.py -- these are the stable, public
// reason codes classify_page()/_search_locked() attach to an `action_search`
// trace event's payload (challenge_reason) whenever challenge_detected is
// true. Kept as a manually-synced literal union (no shared codegen between
// backend/frontend in this project) rather than re-deriving from prose --
// falls back to "unknown" for anything not recognized here, never crashes.
export type RedditChallengeReason =
  | "js_challenge_url"
  | "block_phrase"
  | "captcha_phrase"
  | "empty_title_short_body"
  | "run_disabled_after_earlier_challenge"
  | "unknown";

const KNOWN_REASONS: ReadonlySet<RedditChallengeReason> = new Set([
  "js_challenge_url",
  "block_phrase",
  "captcha_phrase",
  "empty_title_short_body",
  "run_disabled_after_earlier_challenge",
]);

export interface RedditChallengeSummary {
  detected: boolean;
  reasons: RedditChallengeReason[];
  // Iterations where an ACTUAL challenge was newly hit -- excludes
  // "run_disabled_after_earlier_challenge" occurrences, which are just the
  // cheap no-op skip on every iteration after the first real one, and would
  // otherwise make a single early challenge look like it recurred all run.
  iterations: number[];
}

const EMPTY_SUMMARY: RedditChallengeSummary = { detected: false, reasons: [], iterations: [] };

// Derives "did this run hit a Reddit challenge" read-time from the run's own
// trace events -- deliberately no new backend field/schema (Milestone 2's
// approved Option A): every fact this needs is already in trace_events via
// B2's diagnostic capture, and Report.tsx already fetches trace_events via
// api.getRun() for other purposes today.
export function summarizeRedditChallenges(traceEvents: TraceEvent[] | undefined): RedditChallengeSummary {
  if (!traceEvents || traceEvents.length === 0) return EMPTY_SUMMARY;

  const challengeEvents = traceEvents.filter(
    (event) => event.step_type === "action_search" && event.payload?.challenge_detected === true,
  );
  if (challengeEvents.length === 0) return EMPTY_SUMMARY;

  const reasons = new Set<RedditChallengeReason>();
  const iterations = new Set<number>();
  for (const event of challengeEvents) {
    const raw = event.payload?.challenge_reason;
    const reason: RedditChallengeReason = typeof raw === "string" && KNOWN_REASONS.has(raw as RedditChallengeReason)
      ? (raw as RedditChallengeReason)
      : "unknown";
    reasons.add(reason);
    if (reason !== "run_disabled_after_earlier_challenge") {
      iterations.add(event.iteration);
    }
  }

  return { detected: true, reasons: Array.from(reasons), iterations: Array.from(iterations).sort((a, b) => a - b) };
}
