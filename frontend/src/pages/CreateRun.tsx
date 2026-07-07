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
        throw new Error("JSON 文件必须是一个对象数组");
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
      setError("请填写产品类目");
      return;
    }
    if (dataSource === "json_upload" && uploadedItems.length === 0) {
      setError("JSON 上传模式需要先选择一个 JSON 文件");
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
      <h1>新建一次调研</h1>
      <p className="muted">
        输入你要调研的产品类目，Agent 会持续搜索、筛选、分析相关帖子和评论，
        并在收集到足够信息后生成一份商家可读的产品优化报告。
      </p>
      <form className="card form" onSubmit={handleSubmit}>
        <label>
          产品类目 *
          <input
            value={productCategory}
            onChange={(event) => setProductCategory(event.target.value)}
            placeholder="例如：wireless earbuds"
          />
        </label>

        <div>
          <div style={{ marginBottom: 6, fontSize: 14, color: "var(--text-h)" }}>数据来源</div>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6, display: "flex" }}>
            <input
              type="radio"
              name="data_source"
              checked={dataSource === "reddit_api"}
              onChange={() => setDataSource("reddit_api")}
            />
            Reddit API（实时抓取）
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6, display: "flex", marginTop: 4 }}>
            <input
              type="radio"
              name="data_source"
              checked={dataSource === "json_upload"}
              onChange={() => setDataSource("json_upload")}
            />
            JSON 上传（离线 / 演示数据）
          </label>
        </div>

        {showRedditWarning && (
          <p className="error">
            未检测到 Reddit API 凭证——Reddit Data API 目前需要走审批流程，可能暂时申请不到。
            建议改用「JSON 上传」模式，先用准备好的示例数据跑通完整流程。
          </p>
        )}

        {dataSource === "json_upload" && (
          <div>
            <label>
              上传 JSON 文件（Reddit 帖子/评论数组）
              <input type="file" accept="application/json,.json" onChange={handleFileChange} />
            </label>
            {uploadedFileName && (
              <p className="muted">已选择 {uploadedFileName}，共 {uploadedItems.length} 条数据。</p>
            )}
            {uploadError && <p className="error">{uploadError}</p>}
            <details>
              <summary className="muted" style={{ cursor: "pointer" }}>
                查看 JSON 格式示例
              </summary>
              <pre className="trace-step-payload">{SAMPLE_JSON}</pre>
            </details>
          </div>
        )}

        <label>
          关键词（可选，逗号分隔）
          <input value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder="battery, comfort" />
        </label>
        <label>
          目标 subreddit（可选，逗号分隔，不带 r/）
          <input value={subreddits} onChange={(event) => setSubreddits(event.target.value)} placeholder="headphones, gadgets" />
        </label>
        <div className="form-row">
          <label>
            最大搜索轮次
            <input
              type="number"
              min={1}
              max={20}
              value={maxIterations}
              onChange={(event) => setMaxIterations(Number(event.target.value))}
            />
          </label>
          <label>
            目标证据数量
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
          {submitting ? "创建中..." : "启动 Agent"}
        </button>
      </form>
    </div>
  );
}
