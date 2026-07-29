import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { RunDetail } from "./RunDetail";
import { LanguageProvider } from "../lib/i18n";
import { mockFetchWith } from "../test/mockFetch";
import type { CanonicalCategory, Claim, RunRecord, TraceEvent } from "../api";

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

function makeClaimExtractionEvent(): TraceEvent {
  return {
    run_id: "run_1",
    iteration: 1,
    step_type: "claim_extraction",
    message: "Extracted 1 claim",
    payload: { source_items_processed: 1, items_with_claims: 1, claims_total: 1 },
    created_at: "2026-01-01T00:00:00+00:00",
  };
}

function makeClaim(overrides: Partial<Claim> = {}): Claim {
  return {
    claim_id: "cl_1",
    run_id: "run_1",
    evidence_id: "ev_1",
    claim_type: "problem",
    aspect_raw: "battery life",
    statement: "Battery drains quickly",
    sentiment: "negative",
    confidence: 0.8,
    extraction_method: "llm",
    created_at: "2026-01-01T00:00:00+00:00",
    subject: null,
    explicit_request: null,
    severity: null,
    canonical_category: null,
    original_source_url: "https://reddit.com/x",
    original_excerpt: "the battery drains fast",
    merge_count: 1,
    merged_claim_ids: null,
    merged_excerpts: null,
    categorization_status: null,
    categorization_method: null,
    categorization_confidence: null,
    ...overrides,
  };
}

function makeCategory(overrides: Partial<CanonicalCategory> = {}): CanonicalCategory {
  return {
    category_id: "cc_1",
    product_category: "wireless earbuds",
    canonical_label: "battery_life",
    normalized_label: "battery life",
    status: "approved",
    alias_of: null,
    first_seen_aspect_raw: "battery_life",
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...overrides,
  };
}

function renderRunDetail() {
  return render(
    <MemoryRouter initialEntries={["/runs/run_1"]}>
      <LanguageProvider>
        <Routes>
          <Route path="/runs/:runId" element={<RunDetail />} />
        </Routes>
      </LanguageProvider>
    </MemoryRouter>,
  );
}

describe("RunDetail -- manual Claim categorization control", () => {
  it("sends POST /claims/{id}/categorize with the selected category and refreshes the claim", async () => {
    const user = userEvent.setup();
    let categorizeRequestBody: unknown = null;
    let assigned = false;

    mockFetchWith((url, method, init) => {
      if (url.endsWith("/runs/run_1") && method === "GET") {
        return { body: { run: makeRun(), trace_events: [makeClaimExtractionEvent()], is_running: false } };
      }
      if (url.endsWith("/runs/run_1/claims")) {
        return { body: [makeClaim({ categorization_method: assigned ? "manual" : null, canonical_category: assigned ? "cc_1" : null })] };
      }
      if (url.includes("/categories?")) {
        return { body: [makeCategory()] };
      }
      if (url.endsWith("/claims/cl_1/categorize") && method === "POST") {
        categorizeRequestBody = init?.body ? JSON.parse(init.body as string) : null;
        assigned = true;
        return { body: { claim_id: "cl_1", canonical_category: "cc_1", categorization_status: "resolved", categorization_method: "manual", categorization_confidence: 1.0 } };
      }
      return undefined;
    });

    renderRunDetail();

    await waitFor(() => expect(screen.getByText(/View extracted claims/)).toBeTruthy());
    await user.click(screen.getByText(/View extracted claims/));

    const select = await screen.findByText("Assign to category…");
    await waitFor(() => expect((select as HTMLOptionElement).closest("select")).not.toBeNull());
    const selectEl = select.closest("select")!;
    await user.selectOptions(selectEl, "cc_1");
    await user.click(screen.getByRole("button", { name: "Assign" }));

    await waitFor(() => expect(categorizeRequestBody).toEqual({ category_id: "cc_1" }));
    await waitFor(() => expect(screen.getByText("Manually categorized")).toBeTruthy());
  });

  it("shows a 'manually categorized' indicator once a claim's categorization_method is manual", async () => {
    mockFetchWith((url, method) => {
      if (url.endsWith("/runs/run_1") && method === "GET") {
        return { body: { run: makeRun(), trace_events: [makeClaimExtractionEvent()], is_running: false } };
      }
      if (url.endsWith("/runs/run_1/claims")) {
        return { body: [makeClaim({ categorization_method: "manual", categorization_status: "resolved", categorization_confidence: 1.0, canonical_category: "cc_1" })] };
      }
      if (url.includes("/categories?")) return { body: [makeCategory()] };
      return undefined;
    });

    renderRunDetail();
    await waitFor(() => expect(screen.getByText(/View extracted claims/)).toBeTruthy());
    const user = userEvent.setup();
    await user.click(screen.getByText(/View extracted claims/));

    await waitFor(() => expect(screen.getByText("Manually categorized")).toBeTruthy());
  });
});
