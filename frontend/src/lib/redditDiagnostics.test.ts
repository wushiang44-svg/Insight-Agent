import { describe, expect, it } from "vitest";
import { summarizeRedditChallenges } from "./redditDiagnostics";
import type { TraceEvent } from "../api";

function makeEvent(iteration: number, payload: Record<string, unknown>): TraceEvent {
  return {
    run_id: "run_1",
    iteration,
    step_type: "action_search",
    message: "Searched",
    payload,
    created_at: "2026-01-01T00:00:00+00:00",
  };
}

describe("summarizeRedditChallenges", () => {
  it("reports not detected for an empty or undefined trace list", () => {
    expect(summarizeRedditChallenges([])).toEqual({ detected: false, reasons: [], iterations: [] });
    expect(summarizeRedditChallenges(undefined)).toEqual({ detected: false, reasons: [], iterations: [] });
  });

  it("reports not detected when no action_search event has challenge_detected", () => {
    const events = [makeEvent(1, { challenge_detected: false, items_returned: 5 })];
    expect(summarizeRedditChallenges(events).detected).toBe(false);
  });

  it("ignores non-action_search events even if they happen to carry a similar-looking payload", () => {
    const events: TraceEvent[] = [
      { ...makeEvent(1, { challenge_detected: true, challenge_reason: "block_phrase" }), step_type: "thought" },
    ];
    expect(summarizeRedditChallenges(events).detected).toBe(false);
  });

  it("detects a real challenge and captures its stable reason code", () => {
    const events = [makeEvent(1, { challenge_detected: true, challenge_reason: "block_phrase" })];
    const summary = summarizeRedditChallenges(events);
    expect(summary.detected).toBe(true);
    expect(summary.reasons).toEqual(["block_phrase"]);
    expect(summary.iterations).toEqual([1]);
  });

  it("falls back to 'unknown' for a reason string it doesn't recognize, never crashes", () => {
    const events = [makeEvent(1, { challenge_detected: true, challenge_reason: "some_future_reason" })];
    expect(summarizeRedditChallenges(events).reasons).toEqual(["unknown"]);
  });

  it("excludes run_disabled_after_earlier_challenge iterations from the iteration list", () => {
    // Matches the real backend contract: iteration 1 hits a real challenge,
    // every later iteration in the same run is a cheap short-circuit -- only
    // iteration 1 should count as a genuine occurrence.
    const events = [
      makeEvent(1, { challenge_detected: true, challenge_reason: "captcha_phrase" }),
      makeEvent(2, { challenge_detected: true, challenge_reason: "run_disabled_after_earlier_challenge" }),
      makeEvent(3, { challenge_detected: true, challenge_reason: "run_disabled_after_earlier_challenge" }),
    ];
    const summary = summarizeRedditChallenges(events);
    expect(summary.detected).toBe(true);
    expect(summary.iterations).toEqual([1]);
    expect(summary.reasons.sort()).toEqual(["captcha_phrase", "run_disabled_after_earlier_challenge"].sort());
  });

  it("dedupes repeated reasons and sorts iterations", () => {
    const events = [
      makeEvent(3, { challenge_detected: true, challenge_reason: "js_challenge_url" }),
      makeEvent(1, { challenge_detected: true, challenge_reason: "js_challenge_url" }),
    ];
    const summary = summarizeRedditChallenges(events);
    expect(summary.reasons).toEqual(["js_challenge_url"]);
    expect(summary.iterations).toEqual([1, 3]);
  });
});
