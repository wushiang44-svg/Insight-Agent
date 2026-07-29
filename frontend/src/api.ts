const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export type RunStatus =
  | "planning"
  | "searching"
  | "summarizing"
  | "completed"
  | "failed"
  | "stopped";

export type StepType =
  | "thought"
  | "action_search"
  | "observation"
  | "screening"
  | "sufficiency_check"
  | "claim_extraction"
  | "summary";

export type ClaimType =
  | "problem"
  | "feature_request"
  | "praise"
  | "comparison"
  | "question"
  | "observation"
  | "shipping_issue"
  | "seller_service_issue"
  | "noise";

// "reddit" is the canonical, user-facing Reddit source for new runs (routed to the
// browser+CDP collector). "reddit_api"/"reddit_scraper" are legacy values kept only
// so pre-existing stored runs still display/replay correctly -- not offered as new-run
// choices anymore (see CreateRun.tsx's SOURCE_OPTIONS).
export type DataSource = "reddit" | "reddit_api" | "reddit_scraper" | "json_upload" | "amazon" | "youtube";

export interface RunRecord {
  run_id: string;
  product_category: string;
  keywords: string[];
  target_subreddits: string[];
  status: RunStatus;
  iteration_count: number;
  max_iterations: number;
  min_evidence_target: number;
  evidence_count: number;
  created_at: string;
  updated_at: string;
  data_source: DataSource;
  stop_reason: string | null;
  error: string | null;
  pipeline_version: string;
}

export interface AppConfig {
  reddit_configured: boolean;
  reddit_browser_configured: boolean;
  amazon_configured: boolean;
  youtube_configured: boolean;
  deepseek_configured: boolean;
}

export interface TraceEvent {
  run_id: string;
  iteration: number;
  step_type: StepType;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface RunDetail {
  run: RunRecord;
  trace_events: TraceEvent[];
  is_running: boolean;
}

export interface ExampleQuote {
  quote: string;
  source_url: string;
  subreddit: string;
}

// Phase 3 (Customer Demand Intelligence Pipeline): only present on aggregate
// groups the Claims-based report path produced (report_source === "claims").
// "approved"/"proposed" reflect the resolved canonical category's review
// state; "uncategorized" is the deliberate overflow bucket for Claims
// categorization couldn't resolve -- never split back out by aspect. null and
// missing both mean "no taxonomy involved" (a legacy Evidence-based entry, or
// an older payload from before this field existed) -- both must render
// identically to each other and to a genuinely absent field.
export type CategoryStatusOrNull = "approved" | "proposed" | "uncategorized" | null;

export interface AspectGroup {
  aspect: string;
  count: number;
  subreddit_count?: number;
  avg_confidence?: number;
  sentiment_counts?: Record<string, number>;
  example_quotes: ExampleQuote[];
  category_status?: CategoryStatusOrNull;
}

export type ReportSource = "claims" | "legacy_evidence";

export interface Report {
  run_id: string;
  generated_at: string;
  top_pain_points: AspectGroup[];
  feature_requests: AspectGroup[];
  praised_aspects: AspectGroup[];
  competitor_mentions: AspectGroup[];
  sentiment_breakdown: Record<string, number>;
  recommended_actions: string[];
  summary_markdown: string;
  subreddits: string[];
  subreddit_counts: Record<string, number>;
  recommended_actions_zh: string[];
  summary_markdown_zh: string;
  // Phase 3, Stage 7 -- optional/backward-compatible: a Report fetched from a
  // run that predates these columns simply omits them. shipping_issues/
  // seller_service_issues are absent (not just empty) on every legacy-path
  // report and on any report generated before this field existed; treat
  // "missing" and "empty array" identically everywhere they're read.
  shipping_issues?: AspectGroup[];
  seller_service_issues?: AspectGroup[];
  report_source?: ReportSource;
  fallback_reason?: string | null;
}

export interface Claim {
  claim_id: string;
  run_id: string;
  evidence_id: string;
  claim_type: ClaimType;
  aspect_raw: string;
  // AI-normalized interpretation -- never the customer's verbatim words. Pair
  // with original_excerpt below, never present this as a direct quote.
  statement: string;
  sentiment: "positive" | "neutral" | "negative";
  confidence: number;
  extraction_method: "llm" | "fallback_rules";
  created_at: string;
  subject: string | null;
  explicit_request: string | null;
  severity: number | null;
  canonical_category: string | null;
  original_source_url: string | null;
  original_excerpt: string | null;
  // Phase 1.6 -- within-review dedup. merge_count is 1 for every claim from a
  // run before this shipped; > 1 means this claim absorbed other near-duplicate
  // claims from the same review (merged_claim_ids/merged_excerpts hold provenance).
  merge_count: number;
  merged_claim_ids: string[] | null;
  merged_excerpts: string[] | null;
  // Phase 3 categorization provenance -- all three null means categorization
  // hasn't run for this claim yet (pre-Phase-3 claim, or the batch step was
  // disabled). method="manual" means a reviewer assigned this claim directly
  // (see api.manuallyCategorizeClaim below) -- automatic recategorization
  // never overwrites it without an explicit override the merchant-facing UI
  // does not expose.
  categorization_status: "resolved" | "unresolved" | null;
  categorization_method: "lexical_match" | "llm_match" | "proposed_new" | "manual" | null;
  categorization_confidence: number | null;
}

export interface CreateRunInput {
  product_category: string;
  keywords: string[];
  target_subreddits: string[];
  max_iterations: number;
  min_evidence_target: number;
  data_source: DataSource;
  uploaded_items: Record<string, unknown>[];
}

// ---------------------------------------------------------------------------
// Phase 3, Stage 8 -- Taxonomy Curation API types. Field lists match exactly
// what the backend's routes.py/models.py return -- see CanonicalCategory /
// CategoryAuditLogEntry in app/models.py and the /categories* routes. Nothing
// here is invented beyond what those endpoints actually send back.
// ---------------------------------------------------------------------------

export type CanonicalCategoryStatus = "proposed" | "approved" | "deprecated";

export interface CanonicalCategory {
  category_id: string;
  product_category: string;
  canonical_label: string;
  normalized_label: string;
  status: CanonicalCategoryStatus;
  alias_of: string | null;
  first_seen_aspect_raw: string;
  created_at: string;
  updated_at: string;
}

export type CategoryAuditAction = "approve" | "merge" | "deprecate" | "rename";

export interface CategoryAuditLogEntry {
  id: number;
  category_id: string;
  action: CategoryAuditAction;
  // Shape depends on `action`: merge -> {target_category_id}, rename ->
  // {old_label, new_label}, approve/deprecate -> {from_status, to_status}.
  // No `actor` field -- the backend has no auth/identity system, so one is
  // never returned and the frontend must never fabricate one.
  detail: Record<string, unknown>;
  created_at: string;
}

// POST /claims/{id}/categorize returns asdict(claim) directly (routes.py),
// which is the RAW Claim shape -- unlike GET /runs/{run_id}/claims, it does
// NOT include the original_source_url/original_excerpt enrichment (those are
// computed only by that list endpoint). Only the fields this app actually
// reads from the response are declared; callers that need the full enriched
// shape re-fetch via api.getClaims() afterward (see RunDetail.tsx).
export interface ManualCategorizeClaimResponse {
  claim_id: string;
  canonical_category: string | null;
  categorization_status: string | null;
  categorization_method: string | null;
  categorization_confidence: number | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  createRun: (input: CreateRunInput) =>
    request<RunRecord>("/runs", { method: "POST", body: JSON.stringify(input) }),
  listRuns: () => request<RunRecord[]>("/runs"),
  getRun: (runId: string) => request<RunDetail>(`/runs/${runId}`),
  stopRun: (runId: string) => request<{ stop_requested: boolean }>(`/runs/${runId}/stop`, { method: "POST" }),
  getReport: (runId: string) => request<Report>(`/runs/${runId}/report`),
  getClaims: (runId: string) => request<Claim[]>(`/runs/${runId}/claims`),
  getConfig: () => request<AppConfig>("/config"),

  // Phase 3, Stage 8 -- Taxonomy Curation API. `status`/`canonicalLabel` are
  // mutually exclusive on the backend (GET /categories) -- passing a label
  // does an exact lookup and ignores `status`.
  listCategories: (productCategory: string, opts?: { status?: CanonicalCategoryStatus; canonicalLabel?: string }) => {
    const params = new URLSearchParams({ product_category: productCategory });
    if (opts?.status) params.set("status", opts.status);
    if (opts?.canonicalLabel) params.set("canonical_label", opts.canonicalLabel);
    return request<CanonicalCategory[]>(`/categories?${params.toString()}`);
  },
  getCategory: (categoryId: string) => request<CanonicalCategory>(`/categories/${encodeURIComponent(categoryId)}`),
  approveCategory: (categoryId: string) =>
    request<CanonicalCategory>(`/categories/${encodeURIComponent(categoryId)}/approve`, { method: "POST" }),
  renameCategory: (categoryId: string, canonicalLabel: string) =>
    request<CanonicalCategory>(`/categories/${encodeURIComponent(categoryId)}/rename`, {
      method: "POST",
      body: JSON.stringify({ canonical_label: canonicalLabel }),
    }),
  mergeCategories: (sourceId: string, targetId: string) =>
    request<CanonicalCategory>(`/categories/${encodeURIComponent(sourceId)}/merge/${encodeURIComponent(targetId)}`, {
      method: "POST",
    }),
  deprecateCategory: (categoryId: string) =>
    request<CanonicalCategory>(`/categories/${encodeURIComponent(categoryId)}/deprecate`, { method: "POST" }),
  getCategoryHistory: (categoryId: string) =>
    request<CategoryAuditLogEntry[]>(`/categories/${encodeURIComponent(categoryId)}/history`),
  manuallyCategorizeClaim: (claimId: string, categoryId: string) =>
    request<ManualCategorizeClaimResponse>(`/claims/${encodeURIComponent(claimId)}/categorize`, {
      method: "POST",
      body: JSON.stringify({ category_id: categoryId }),
    }),
};
