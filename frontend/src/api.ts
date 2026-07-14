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
  | "sufficiency_check"
  | "summary";

export type DataSource = "reddit_api" | "reddit_scraper" | "json_upload" | "amazon" | "youtube";

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
}

export interface AppConfig {
  reddit_configured: boolean;
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
  getConfig: () => request<AppConfig>("/config"),
};
