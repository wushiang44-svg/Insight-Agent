import type { ReportSource } from "../api";

// The five reasons pipeline/taxonomy.py + react_agent.py's eligibility gate
// can actually return (see docs/phase3_claims_taxonomy_plan.md's Stage 5
// section), plus "unknown" for anything malformed or not yet recognized --
// never crash on an unexpected string, always fall back to a neutral message.
export type FallbackReasonKind =
  | "claims_report_disabled"
  | "categorization_disabled"
  | "categorization_incomplete"
  | "no_claims"
  | "low_resolved_coverage"
  | "unknown";

export interface ParsedFallbackReason {
  kind: FallbackReasonKind;
  // Only set for "low_resolved_coverage", parsed from the "<ratio>" suffix
  // and converted to a whole percentage (0-100) for display. Never NaN --
  // a malformed/unparseable suffix reports as "unknown" instead.
  percent?: number;
}

const KNOWN_EXACT_REASONS: ReadonlySet<FallbackReasonKind> = new Set([
  "claims_report_disabled",
  "categorization_disabled",
  "categorization_incomplete",
  "no_claims",
]);

const LOW_COVERAGE_PREFIX = "low_resolved_coverage:";

export function parseFallbackReason(reason: string | null | undefined): ParsedFallbackReason {
  if (!reason) return { kind: "unknown" };
  if (KNOWN_EXACT_REASONS.has(reason as FallbackReasonKind)) {
    return { kind: reason as FallbackReasonKind };
  }
  if (reason.startsWith(LOW_COVERAGE_PREFIX)) {
    const raw = reason.slice(LOW_COVERAGE_PREFIX.length).trim();
    // Number("") is 0 in JS -- an empty suffix must NOT be read as a valid
    // zero ratio, so the emptiness check has to come before Number.isFinite.
    if (raw.length > 0) {
      const ratio = Number(raw);
      if (Number.isFinite(ratio)) {
        return { kind: "low_resolved_coverage", percent: Math.round(ratio * 100) };
      }
    }
  }
  return { kind: "unknown" };
}

// report_source is optional/missing on older Report payloads (pre-Stage-7) --
// treat that exactly like "legacy_evidence", not as an error state.
export function resolveReportSource(reportSource: ReportSource | null | undefined): ReportSource {
  return reportSource === "claims" ? "claims" : "legacy_evidence";
}
