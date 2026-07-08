import { useEffect, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { AppConfig, DataSource } from "../api";

function parseList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

const SAMPLE_JSON = `[
  {
    "source_url": "https://reddit.com/r/headphones/comments/abc",
    "subreddit": "headphones",
    "post_id": "abc",
    "comment_id": null,
    "title": "Battery life on these earbuds is terrible",
    "body": "I charge them every night and they still die by lunch.",
    "score": 42,
    "comment_count": 18,
    "created_at": "2026-05-08T01:00:00+00:00"
  }
]`;

export function CreateRun() {
  const navigate = useNavigate();
  const [productCategory, setProductCategory] = useState("");
  const [keywords, setKeywords] = useState("");
  const [subreddits, setSubreddits] = useState("");
  const [maxIterations, setMaxIterations] = useState(6);
  const [minEvidenceTarget, setMinEvidenceTarget] = useState(25);
  const [dataSource, setDataSource] = useState<DataSource>("reddit_api");
  const [uploadedItems, setUploadedItems] = useState<Record<string, unknown>[]>([]);
  const [uploadedFileName, setUploadedFileName] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getConfig()
      .then((data) => {
        setConfig(data);
        if (!data.reddit_configured) {
          setDataSource("json_upload");
        }
      })
      .catch(() => setConfig(null));
  }, []);

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setUploadError(null);
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "object" || item === null)) {
        throw new Error("The JSON file must be an array of objects");
      }
      setUploadedItems(parsed as Record<string, unknown>[]);
      setUploadedFileName(file.name);
    } catch (err) {
      setUploadedItems([]);
      setUploadedFileName(null);
      setUploadError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!productCategory.trim()) {
      setError("Please enter a product category");
      return;
    }
    if (dataSource === "json_upload" && uploadedItems.length === 0) {
      setError("JSON upload mode requires selecting a JSON file first");
      return;
    }
    setSubmitting(true);
    try {
      const run = await api.createRun({
        product_category: productCategory.trim(),
        keywords: parseList(keywords),
        target_subreddits: parseList(subreddits),
        max_iterations: maxIterations,
        min_evidence_target: minEvidenceTarget,
        data_source: dataSource,
        uploaded_items: dataSource === "json_upload" ? uploadedItems : [],
      });
      navigate(`/runs/${run.run_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const showRedditWarning = dataSource === "reddit_api" && config !== null && !config.reddit_configured;

  return (
    <div className="page">
      <h1>New Research Run</h1>
      <p className="muted">
        Enter the product category you want to research. The agent will keep searching, filtering, and
        analyzing relevant posts and comments, then generate a merchant-readable product improvement report
        once it has collected enough information.
      </p>
      <form className="card form" onSubmit={handleSubmit}>
        <label>
          Product category *
          <input
            value={productCategory}
            onChange={(event) => setProductCategory(event.target.value)}
            placeholder="e.g. wireless earbuds"
          />
        </label>

        <div>
          <div style={{ marginBottom: 6, fontSize: 14, color: "var(--text-h)" }}>Data source</div>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6, display: "flex" }}>
            <input
              type="radio"
              name="data_source"
              checked={dataSource === "reddit_api"}
              onChange={() => setDataSource("reddit_api")}
            />
            Reddit API (live search)
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6, display: "flex", marginTop: 4 }}>
            <input
              type="radio"
              name="data_source"
              checked={dataSource === "json_upload"}
              onChange={() => setDataSource("json_upload")}
            />
            JSON upload (offline / demo data)
          </label>
        </div>

        {showRedditWarning && (
          <p className="error">
            No Reddit API credentials detected — the Reddit Data API currently requires an approval process
            that may take a while. Consider switching to "JSON upload" mode to try the full flow with prepared
            sample data first.
          </p>
        )}

        {dataSource === "json_upload" && (
          <div>
            <label>
              Upload a JSON file (array of Reddit posts/comments)
              <input type="file" accept="application/json,.json" onChange={handleFileChange} />
            </label>
            {uploadedFileName && (
              <p className="muted">Selected {uploadedFileName}, {uploadedItems.length} item(s).</p>
            )}
            {uploadError && <p className="error">{uploadError}</p>}
            <details>
              <summary className="muted" style={{ cursor: "pointer" }}>
                View JSON format example
              </summary>
              <pre className="trace-step-payload">{SAMPLE_JSON}</pre>
            </details>
          </div>
        )}

        <label>
          Keywords (optional, comma-separated)
          <input value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder="battery, comfort" />
        </label>
        <label>
          Target subreddits (optional, comma-separated, without r/)
          <input value={subreddits} onChange={(event) => setSubreddits(event.target.value)} placeholder="headphones, gadgets" />
        </label>
        <div className="form-row">
          <label>
            Max iterations
            <input
              type="number"
              min={1}
              max={20}
              value={maxIterations}
              onChange={(event) => setMaxIterations(Number(event.target.value))}
            />
          </label>
          <label>
            Target evidence count
            <input
              type="number"
              min={1}
              max={500}
              value={minEvidenceTarget}
              onChange={(event) => setMinEvidenceTarget(Number(event.target.value))}
            />
          </label>
        </div>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={submitting}>
          {submitting ? "Creating..." : "Start Agent"}
        </button>
      </form>
    </div>
  );
}
