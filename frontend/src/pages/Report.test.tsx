import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { Report, buildPriorities } from "./Report";
import { LanguageProvider } from "../lib/i18n";
import { mockFetchWith } from "../test/mockFetch";
import type { AspectGroup, Report as ReportData, RunRecord, TraceEvent } from "../api";

function makeAspectGroup(overrides: Partial<AspectGroup> = {}): AspectGroup {
  return {
    aspect: "battery life",
    count: 5,
    subreddit_count: 2,
    avg_confidence: 0.8,
    sentiment_counts: { negative: 5 },
    example_quotes: [],
    ...overrides,
  };
}

function makeReport(overrides: Partial<ReportData> = {}): ReportData {
  return {
    run_id: "run_1",
    generated_at: "2026-01-01T00:00:00+00:00",
    top_pain_points: [],
    feature_requests: [],
    praised_aspects: [],
    competitor_mentions: [],
    sentiment_breakdown: { positive: 1, neutral: 1, negative: 1 },
    recommended_actions: [],
    summary_markdown: "# summary",
    subreddits: ["gadgets"],
    subreddit_counts: { gadgets: 3 },
    recommended_actions_zh: [],
    summary_markdown_zh: "",
    ...overrides,
  };
}

function makeRun(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: "run_1",
    product_category: "wireless earbuds",
    keywords: [],
    target_subreddits: [],
    status: "completed",
    iteration_count: 3,
    max_iterations: 6,
    min_evidence_target: 25,
    evidence_count: 10,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    data_source: "reddit",
    stop_reason: null,
    error: null,
    pipeline_version: "v4",
    ...overrides,
  };
}

function makeChallengeEvent(iteration: number, payload: Record<string, unknown>): TraceEvent {
  return {
    run_id: "run_1",
    iteration,
    step_type: "action_search",
    message: "Searched",
    payload,
    created_at: "2026-01-01T00:00:00+00:00",
  };
}

async function renderReport(report: ReportData, run: RunRecord = makeRun(), traceEvents: TraceEvent[] = []) {
  mockFetchWith((url) => {
    if (url.includes("/report")) return { body: report };
    if (url.includes("/runs/run_1")) return { body: { run, trace_events: traceEvents, is_running: false } };
    return undefined;
  });

  const utils = render(
    <MemoryRouter initialEntries={["/runs/run_1/report"]}>
      <LanguageProvider>
        <Routes>
          <Route path="/runs/:runId/report" element={<Report />} />
        </Routes>
      </LanguageProvider>
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.queryByText(/Loading/i)).toBeNull());
  return utils;
}

describe("Report -- category_status badges", () => {
  it("shows a Pending review badge for a proposed entry", async () => {
    await renderReport(makeReport({ top_pain_points: [makeAspectGroup({ category_status: "proposed" })] }));
    expect(screen.getAllByText("Pending review").length).toBeGreaterThan(0);
  });

  it("shows no badge for an approved entry", async () => {
    await renderReport(makeReport({ top_pain_points: [makeAspectGroup({ category_status: "approved" })] }));
    expect(screen.queryByText("Pending review")).toBeNull();
  });

  it("shows no Pending review badge for an uncategorized entry", async () => {
    await renderReport(makeReport({ top_pain_points: [makeAspectGroup({ category_status: "uncategorized" })] }));
    expect(screen.queryByText("Pending review")).toBeNull();
  });

  it("uses the localized neutral label for an uncategorized entry, not a raw internal string", async () => {
    await renderReport(makeReport({ top_pain_points: [makeAspectGroup({ aspect: "uncategorized", category_status: "uncategorized" })] }));
    expect(screen.getAllByText("Uncategorized").length).toBeGreaterThan(0);
    expect(screen.queryByText("__uncategorized__")).toBeNull();
  });

  it("remains compatible when category_status is explicitly null", async () => {
    await renderReport(makeReport({ top_pain_points: [makeAspectGroup({ category_status: null })] }));
    expect(screen.queryByText("Pending review")).toBeNull();
    expect(screen.getAllByText("Battery Life").length).toBeGreaterThan(0); // real aspect label preserved
  });

  it("remains compatible when category_status is entirely missing (older payload shape)", async () => {
    const group = makeAspectGroup();
    delete (group as Partial<AspectGroup>).category_status;
    await renderReport(makeReport({ top_pain_points: [group] }));
    expect(screen.queryByText("Pending review")).toBeNull();
    expect(screen.getAllByText("Battery Life").length).toBeGreaterThan(0);
  });
});

describe("Report -- shipping/seller-service sections", () => {
  it("shows the Shipping Issues section when non-empty", async () => {
    await renderReport(makeReport({ shipping_issues: [makeAspectGroup({ aspect: "late delivery" })] }));
    expect(screen.getByText("Shipping Issues")).toBeTruthy();
  });

  it("shows the Seller Service Issues section when non-empty", async () => {
    await renderReport(makeReport({ seller_service_issues: [makeAspectGroup({ aspect: "unresponsive support" })] }));
    expect(screen.getByText("Seller Service Issues")).toBeTruthy();
  });

  it("hides both sections when their arrays are empty", async () => {
    await renderReport(makeReport({ shipping_issues: [], seller_service_issues: [] }));
    expect(screen.queryByText("Shipping Issues")).toBeNull();
    expect(screen.queryByText("Seller Service Issues")).toBeNull();
  });

  it("hides both sections when the fields are entirely absent (older payload)", async () => {
    const report = makeReport();
    delete (report as Partial<ReportData>).shipping_issues;
    delete (report as Partial<ReportData>).seller_service_issues;
    await renderReport(report);
    expect(screen.queryByText("Shipping Issues")).toBeNull();
    expect(screen.queryByText("Seller Service Issues")).toBeNull();
  });
});

describe("Report -- report source / fallback reason metadata", () => {
  it("displays the Claims-based report source", async () => {
    const { container } = await renderReport(makeReport({ report_source: "claims", fallback_reason: null }));
    expect(container.textContent).toContain("Claims-based report");
  });

  it("displays the legacy evidence report source", async () => {
    const { container } = await renderReport(makeReport({ report_source: "legacy_evidence", fallback_reason: "no_claims" }));
    expect(container.textContent).toContain("Legacy evidence report");
  });

  it("remains compatible when report_source is entirely missing (older payload) -- reads as legacy, no crash", async () => {
    const report = makeReport();
    delete (report as Partial<ReportData>).report_source;
    const { container } = await renderReport(report);
    expect(container.textContent).toContain("Legacy evidence report");
  });

  it("shows the category-status snapshot note for a Claims-based report", async () => {
    const { container } = await renderReport(makeReport({ report_source: "claims", fallback_reason: null }));
    expect(container.textContent).toContain("Category statuses reflect the taxonomy state when this report was generated.");
  });

  it("hides the category-status snapshot note for a legacy evidence report", async () => {
    const { container } = await renderReport(makeReport({ report_source: "legacy_evidence", fallback_reason: "no_claims" }));
    expect(container.textContent).not.toContain("Category statuses reflect the taxonomy state when this report was generated.");
  });

  const fallbackCases: [string, string][] = [
    ["claims_report_disabled", "turned off for this backend"],
    ["categorization_disabled", "turned off for this run"],
    ["categorization_incomplete", "did not finish"],
    ["no_claims", "No claims were available"],
  ];
  for (const [reason, expectedSubstring] of fallbackCases) {
    it(`maps the fallback reason "${reason}" to its readable message`, async () => {
      const { container } = await renderReport(makeReport({ report_source: "legacy_evidence", fallback_reason: reason }));
      expect(container.textContent).toContain(expectedSubstring);
    });
  }

  it("formats low_resolved_coverage's ratio as a whole-number percentage", async () => {
    const { container } = await renderReport(
      makeReport({ report_source: "legacy_evidence", fallback_reason: "low_resolved_coverage:0.42" }),
    );
    expect(container.textContent).toContain("42%");
  });

  it("does not crash on a malformed fallback ratio", async () => {
    const { container } = await renderReport(
      makeReport({ report_source: "legacy_evidence", fallback_reason: "low_resolved_coverage:garbage" }),
    );
    expect(container.textContent).toContain("not used for this run"); // generic fallback text, not a crash
  });

  it("does not crash on an unrecognized fallback reason", async () => {
    const { container } = await renderReport(
      makeReport({ report_source: "legacy_evidence", fallback_reason: "some_future_reason" }),
    );
    expect(container.textContent).toContain("not used for this run");
  });
});

describe("Report -- old payloads render successfully", () => {
  it("renders without crashing when every Stage 7 field is entirely absent", async () => {
    const oldReport = makeReport({
      top_pain_points: [makeAspectGroup()],
    });
    delete (oldReport as Partial<ReportData>).shipping_issues;
    delete (oldReport as Partial<ReportData>).seller_service_issues;
    delete (oldReport as Partial<ReportData>).report_source;
    delete (oldReport as Partial<ReportData>).fallback_reason;

    const { container } = await renderReport(oldReport);

    expect(screen.getAllByText("Battery Life").length).toBeGreaterThan(0);
    expect(container.textContent).toContain("Legacy evidence report");
    expect(screen.queryByText("Shipping Issues")).toBeNull();
  });
});

describe("Report -- Reddit challenge diagnostics (Milestone 2 / B3, Option A)", () => {
  it("shows the prominent challenge banner when evidence is zero and a challenge was recorded", async () => {
    const { container } = await renderReport(
      makeReport({ sentiment_breakdown: {} }),
      makeRun({ data_source: "reddit" }),
      [makeChallengeEvent(1, { challenge_detected: true, challenge_reason: "js_challenge_url" })],
    );
    expect(container.textContent).toContain("Reddit blocked this run");
  });

  it("does not show the banner when a challenge occurred but evidence is still healthy -- shows the quiet note instead", async () => {
    const { container } = await renderReport(
      makeReport({ sentiment_breakdown: { positive: 3, neutral: 2, negative: 1 } }),
      makeRun({ data_source: "reddit" }),
      [makeChallengeEvent(1, { challenge_detected: true, challenge_reason: "block_phrase" })],
    );
    expect(container.textContent).not.toContain("Reddit blocked this run");
    expect(container.textContent).toContain("Reddit was blocked on 1 iteration(s)");
  });

  it("shows neither message when no challenge was ever recorded, even with zero evidence", async () => {
    const { container } = await renderReport(makeReport({ sentiment_breakdown: {} }), makeRun({ data_source: "reddit" }), []);
    expect(container.textContent).not.toContain("Reddit blocked this run");
    expect(container.textContent).not.toContain("iteration(s)");
  });

  it("never shows Reddit-specific messaging for a non-Reddit data source", async () => {
    const { container } = await renderReport(
      makeReport({ sentiment_breakdown: {} }),
      makeRun({ data_source: "amazon" }),
      [makeChallengeEvent(1, { challenge_detected: true, challenge_reason: "js_challenge_url" })],
    );
    expect(container.textContent).not.toContain("Reddit blocked this run");
  });

  it("does not crash and shows nothing extra for older payloads with no trace_events at all", async () => {
    const { container } = await renderReport(makeReport(), makeRun({ data_source: "reddit" }), []);
    expect(container.textContent).not.toContain("Reddit blocked this run");
  });
});

// ---------------------------------------------------------------------------
// Milestone 3 / A3, requirement 7: end-to-end backend/frontend ordering
// consistency. The fixture below is the EXACT backend-sorted shape (already
// verified against the real run_55025c50e81b replay and against
// backend/tests/test_report_generation.py's own
// test_dampened_sort_matches_the_real_run_55025c50e81b_finding /
// test_tiebreak_uses_thread_count_before_raw_count) -- this test proves the
// frontend's own independent priority ranking (buildPriorities(), which
// recomputes priorityScore() rather than just trusting array order) lands on
// the identical order for the pain-point subset, not just that the two
// happen to look similar.
// ---------------------------------------------------------------------------

describe("Report -- backend/frontend ordering consistency (Milestone 3 / A3)", () => {
  it("buildPriorities()'s pain-point order matches the backend's already-dampened-sorted array order", () => {
    // Same shape as run_55025c50e81b's real top_pain_points, already in the
    // backend's correct weighted_count -> thread_count -> count -> label
    // order. sentiment_counts/subreddit_count/avg_confidence are held
    // UNIFORM across every entry (proportional negative count, same
    // confidence/subreddit spread) so priorityScore()'s other three terms
    // can't accidentally reorder things -- this test isolates the
    // weighted_count/thread_count/count/label ranking behavior specifically.
    const uniform = (count: number) => ({ subreddit_count: 1, avg_confidence: 0.8, sentiment_counts: { negative: count } });
    const backendOrderedPainPoints: AspectGroup[] = [
      makeAspectGroup({ aspect: "floor damage", count: 11, weighted_count: 4, thread_count: 2, ...uniform(11) }),
      makeAspectGroup({ aspect: "mop performance", count: 3, weighted_count: 3, thread_count: 3, ...uniform(3) }),
      makeAspectGroup({ aspect: "navigation/collision avoidance", count: 3, weighted_count: 3, thread_count: 3, ...uniform(3) }),
      makeAspectGroup({ aspect: "price vs value", count: 3, weighted_count: 3, thread_count: 3, ...uniform(3) }),
      makeAspectGroup({ aspect: "durability", count: 3, weighted_count: 3, thread_count: 2, ...uniform(3) }),
      makeAspectGroup({ aspect: "reliability", count: 3, weighted_count: 3, thread_count: 2, ...uniform(3) }),
      makeAspectGroup({ aspect: "app real time position update", count: 9, weighted_count: 3, thread_count: 1, ...uniform(9) }),
      makeAspectGroup({ aspect: "security/privacy", count: 2, weighted_count: 2, thread_count: 1, ...uniform(2) }),
    ];
    const backendOrderIndex = new Map(backendOrderedPainPoints.map((g, i) => [g.aspect, i]));

    // Feed them in DELIBERATELY SHUFFLED (not backend order) -- proves
    // buildPriorities() derives the order itself from weighted_count/
    // thread_count/count/label, rather than just preserving whatever array
    // order it happened to receive.
    const shuffled = [...backendOrderedPainPoints].reverse();
    const priorities = buildPriorities(shuffled, []);
    const painPointOrder = priorities.filter((p) => p.category === "pain_point").map((p) => p.aspect);

    expect(painPointOrder).toEqual(backendOrderedPainPoints.map((g) => g.aspect));
    // Belt-and-suspenders: also confirm it's monotonic against the backend's
    // own index, not just coincidentally equal for this one array length.
    for (let i = 1; i < painPointOrder.length; i++) {
      expect(backendOrderIndex.get(painPointOrder[i])!).toBeGreaterThan(backendOrderIndex.get(painPointOrder[i - 1])!);
    }
  });

  it("preserves raw count in the priority item even though ranking uses weighted_count", () => {
    const priorities = buildPriorities(
      [makeAspectGroup({ aspect: "app real time position update", count: 9, weighted_count: 3, thread_count: 1 })],
      [],
    );
    expect(priorities[0].count).toBe(9); // never the dampened 3
  });
});
