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

export interface AspectGroup {
  aspect: string;
  count: number;
  subreddit_count?: number;
  avg_confidence?: number;
  sentiment_counts?: Record<string, number>;
  example_quotes: ExampleQuote[];
}

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
};
