import { useEffect, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { AppConfig, DataSource } from "../api";
import { useLanguage } from "../lib/i18n";
import { useSourceMeta } from "../lib/sources";

const SOURCE_OPTIONS: DataSource[] = ["reddit_api", "reddit_scraper", "amazon", "youtube", "json_upload"];

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
  const { t } = useLanguage();
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
      setError(t("create.error.noCategory"));
      return;
    }
    if (dataSource === "json_upload" && uploadedItems.length === 0) {
      setError(t("create.error.noUpload"));
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

  const selectedMeta = useSourceMeta(dataSource);
  // Reddit's "not configured" case has its own, more detailed message above (approval
  // process, alternatives) — this generic one covers amazon/youtube instead.
  const showSourceWarning =
    dataSource !== "reddit_api" &&
    config !== null &&
    selectedMeta.configKey !== undefined &&
    !config[selectedMeta.configKey];
  const notConfiguredHint = dataSource === "amazon" ? t("create.hint.amazon") : t("create.hint.notInstalled");

  return (
    <div className="page">
      <h1>{t("create.title")}</h1>
      <p className="muted">{t("create.intro")}</p>
      <form className="card form" onSubmit={handleSubmit}>
        <label>
          {t("create.productCategory")}
          <input
            value={productCategory}
            onChange={(event) => setProductCategory(event.target.value)}
            placeholder={t("create.productCategory.placeholder")}
          />
        </label>

        <div>
          <div className="field-group-label">{t("create.dataSource")}</div>
          <div className="radio-group">
            {SOURCE_OPTIONS.map((option) => (
              <label className="radio-option" key={option}>
                <input
                  type="radio"
                  name="data_source"
                  checked={dataSource === option}
                  onChange={() => setDataSource(option)}
                />
                {t(`create.source.${option}`)}
              </label>
            ))}
          </div>
        </div>

        {showRedditWarning && <p className="error">{t("create.redditWarning")}</p>}

        {showSourceWarning && (
          <p className="error">{t("create.sourceWarning", { source: selectedMeta.label, hint: notConfiguredHint })}</p>
        )}

        {dataSource === "reddit_scraper" && <p className="muted">{t("create.note.reddit_scraper")}</p>}

        {dataSource === "amazon" && <p className="muted">{t("create.note.amazon")}</p>}

        {dataSource === "youtube" && <p className="muted">{t("create.note.youtube")}</p>}

        {dataSource === "json_upload" && (
          <div>
            <label>
              {t("create.upload.label")}
              <input type="file" accept="application/json,.json" onChange={handleFileChange} />
            </label>
            {uploadedFileName && (
              <p className="muted">{t("create.upload.selected", { name: uploadedFileName, count: uploadedItems.length })}</p>
            )}
            {uploadError && <p className="error">{uploadError}</p>}
            <details className="tech-details">
              <summary>{t("create.upload.viewExample")}</summary>
              <pre className="trace-step-payload">{SAMPLE_JSON}</pre>
            </details>
          </div>
        )}

        <label>
          {t("create.keywords")}
          <input value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder={t("create.keywords.placeholder")} />
        </label>
        {(dataSource === "reddit_api" || dataSource === "reddit_scraper") && (
          <label>
            {t("create.subreddits")}
            <input
              value={subreddits}
              onChange={(event) => setSubreddits(event.target.value)}
              placeholder={t("create.subreddits.placeholder")}
            />
          </label>
        )}
        <div className="form-row">
          <label>
            {t("create.maxIterations")}
            <input
              type="number"
              min={1}
              max={20}
              value={maxIterations}
              onChange={(event) => setMaxIterations(Number(event.target.value))}
            />
          </label>
          <label>
            {t("create.targetEvidence")}
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
          {submitting ? t("create.submitting") : t("create.submit")}
        </button>
      </form>
    </div>
  );
}
