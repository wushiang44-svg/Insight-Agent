import { describe, expect, it } from "vitest";
import { parseFallbackReason, resolveReportSource } from "./reportSource";

describe("parseFallbackReason", () => {
  it("maps every known exact reason correctly", () => {
    expect(parseFallbackReason("claims_report_disabled")).toEqual({ kind: "claims_report_disabled" });
    expect(parseFallbackReason("categorization_disabled")).toEqual({ kind: "categorization_disabled" });
    expect(parseFallbackReason("categorization_incomplete")).toEqual({ kind: "categorization_incomplete" });
    expect(parseFallbackReason("no_claims")).toEqual({ kind: "no_claims" });
  });

  it("parses low_resolved_coverage's ratio suffix as a whole percentage", () => {
    expect(parseFallbackReason("low_resolved_coverage:0.42")).toEqual({ kind: "low_resolved_coverage", percent: 42 });
    expect(parseFallbackReason("low_resolved_coverage:0.7")).toEqual({ kind: "low_resolved_coverage", percent: 70 });
    expect(parseFallbackReason("low_resolved_coverage:1.0")).toEqual({ kind: "low_resolved_coverage", percent: 100 });
    expect(parseFallbackReason("low_resolved_coverage:0")).toEqual({ kind: "low_resolved_coverage", percent: 0 });
  });

  it("does not crash on a malformed ratio -- falls back to unknown", () => {
    expect(parseFallbackReason("low_resolved_coverage:not-a-number")).toEqual({ kind: "unknown" });
    expect(parseFallbackReason("low_resolved_coverage:")).toEqual({ kind: "unknown" });
    expect(parseFallbackReason("low_resolved_coverage:NaN")).toEqual({ kind: "unknown" });
  });

  it("treats an unrecognized reason string as unknown, not a crash", () => {
    expect(parseFallbackReason("some_future_reason_not_yet_known")).toEqual({ kind: "unknown" });
  });

  it("treats null/undefined/empty as unknown", () => {
    expect(parseFallbackReason(null)).toEqual({ kind: "unknown" });
    expect(parseFallbackReason(undefined)).toEqual({ kind: "unknown" });
    expect(parseFallbackReason("")).toEqual({ kind: "unknown" });
  });
});

describe("resolveReportSource", () => {
  it("passes through a real report_source value", () => {
    expect(resolveReportSource("claims")).toBe("claims");
    expect(resolveReportSource("legacy_evidence")).toBe("legacy_evidence");
  });

  it("treats a missing report_source as legacy_evidence, not an error", () => {
    expect(resolveReportSource(null)).toBe("legacy_evidence");
    expect(resolveReportSource(undefined)).toBe("legacy_evidence");
  });
});
