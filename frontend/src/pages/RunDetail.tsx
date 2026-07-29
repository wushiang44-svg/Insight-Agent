import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { CanonicalCategory, Claim, RunDetail as RunDetailData } from "../api";
import { KpiCard } from "../components/KpiCard";
import { StatusBadge } from "../components/StatusBadge";
import { TraceTimeline } from "../components/TraceTimeline";
import { translateAspect } from "../lib/aspectTranslations";
import { useLanguage } from "../lib/i18n";
import { useSourceMeta } from "../lib/sources";

const ACTIVE_STATUSES = new Set(["planning", "searching", "summarizing"]);
const POLL_INTERVAL_MS = 2000;

// "v1" is the only pipeline_version that predates the Claims table entirely --
// every later version ("v2", "v3", ...) has Claims. Checking !== "v1" instead of
// pinning to one exact version keeps this from silently breaking again the next
// time pipeline_version is bumped (as it was v2 -> v3 for Phase 2 screening).
function hasClaimsPipeline(pipelineVersion: string): boolean {
  return pipelineVersion !== "v1";
}

interface ClaimStatsTotals {
  sourceItemsProcessed: number;
  itemsWithClaims: number;
  claimsTotal: number;
  llmClaims: number;
  fallbackClaims: number;
  invalidClaims: number;
  extractionFailures: number;
  // Phase 1.6 -- within-review dedup funnel.
  rawClaimsExtracted: number;
  duplicatesRemoved: number;
  claimsMerged: number;
  safetyCapTruncations: number;
}

function sumClaimStats(detail: RunDetailData): ClaimStatsTotals {
  const totals: ClaimStatsTotals = {
    sourceItemsProcessed: 0,
    itemsWithClaims: 0,
    claimsTotal: 0,
    llmClaims: 0,
    fallbackClaims: 0,
    invalidClaims: 0,
    extractionFailures: 0,
    rawClaimsExtracted: 0,
    duplicatesRemoved: 0,
    claimsMerged: 0,
    safetyCapTruncations: 0,
  };
  for (const event of detail.trace_events) {
    if (event.step_type !== "claim_extraction") continue;
    const payload = event.payload;
    totals.sourceItemsProcessed += Number(payload.source_items_processed ?? 0);
    totals.itemsWithClaims += Number(payload.items_with_claims ?? 0);
    totals.claimsTotal += Number(payload.claims_total ?? 0);
    totals.llmClaims += Number(payload.llm_claims ?? 0);
    totals.fallbackClaims += Number(payload.fallback_claims ?? 0);
    totals.invalidClaims += Number(payload.invalid_claims ?? 0);
    totals.extractionFailures += Number(payload.extraction_failures ?? 0);
    totals.rawClaimsExtracted += Number(payload.raw_claims_extracted ?? 0);
    totals.duplicatesRemoved += Number(payload.within_review_duplicates_removed ?? 0);
    totals.claimsMerged += Number(payload.claims_merged ?? 0);
    totals.safetyCapTruncations += Number(payload.safety_cap_truncations ?? 0);
  }
  return totals;
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

export function RunDetail() {
  const { t } = useLanguage();
  const { runId } = useParams<{ runId: string }>();
  const [detail, setDetail] = useState<RunDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [claims, setClaims] = useState<Claim[] | null>(null);
  const [claimsError, setClaimsError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;

    async function poll() {
      try {
        const data = await api.getRun(runId!);
        if (cancelled) return;
        setDetail(data);
        setError(null);
        if (ACTIVE_STATUSES.has(data.run.status)) {
          timerRef.current = window.setTimeout(poll, POLL_INTERVAL_MS);
        }
        if (hasClaimsPipeline(data.run.pipeline_version)) {
          try {
            const claimsData = await api.getClaims(runId!);
            if (!cancelled) {
              setClaims(claimsData);
              setClaimsError(null);
            }
          } catch (err) {
            if (!cancelled) setClaimsError(err instanceof Error ? err.message : String(err));
          }
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    }

    poll();
    return () => {
      cancelled = true;
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [runId]);

  // Re-fetches only the Claims list -- used after a manual categorization
  // succeeds, so the assignment is reflected without waiting for (or
  // triggering an extra) full run-status poll cycle.
  async function refreshClaims() {
    if (!runId) return;
    try {
      const claimsData = await api.getClaims(runId);
      setClaims(claimsData);
      setClaimsError(null);
    } catch (err) {
      setClaimsError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleStop() {
    if (!runId) return;
    await api.stopRun(runId);
  }

  const meta = useSourceMeta(detail?.run.data_source);

  if (error) return <div className="page error">{error}</div>;
  if (!detail) return <div className="page muted">{t("detail.loading")}</div>;

  const { run, trace_events: traceEvents, is_running: isRunning } = detail;
  const isActive = ACTIVE_STATUSES.has(run.status);

  const roundProgress = clampPercent((run.iteration_count / Math.max(run.max_iterations, 1)) * 100);
  const evidenceProgress = clampPercent((run.evidence_count / Math.max(run.min_evidence_target, 1)) * 100);

  const lastThought = [...traceEvents].reverse().find((event) => event.step_type === "thought");
  const focusQuery = lastThought ? String(lastThought.payload.query ?? "") : "";
  const focusGroup = lastThought ? String(lastThought.payload.subreddit ?? "") : "";
  const currentFocus = focusQuery
    ? `"${focusQuery}" ${focusGroup ? t("detail.in", { group: `${meta.citationPrefix}${focusGroup}` }) : t("detail.acrossAll", { source: meta.label })}`
    : t("detail.notStarted");

  return (
    <div className="page">
      <div className="page-header">
        <h1>{run.product_category}</h1>
        <StatusBadge status={run.status} />
      </div>

      <div className="card progress-card">
        <div className="progress-row">
          <div className="progress-label">
            <span>{t("detail.round")}</span>
            <span>
              {run.iteration_count} / {run.max_iterations}
            </span>
          </div>
          <div className="progress-bar-track">
            <div className="progress-bar-fill" style={{ width: `${roundProgress}%` }} />
          </div>
        </div>

        <div className="progress-row">
          <div className="progress-label">
            <span>{t("detail.evidenceCollected")}</span>
            <span>
              {run.evidence_count} / {run.min_evidence_target}
            </span>
          </div>
          <div className="progress-bar-track">
            <div className="progress-bar-fill progress-bar-fill-alt" style={{ width: `${evidenceProgress}%` }} />
          </div>
        </div>

        <div className="progress-meta">
          <div>
            <span className="muted">{t("detail.currentFocus")}</span>
            {currentFocus}
          </div>
          <div>
            <span className="muted">{t("detail.dataSource")}</span>
            {meta.label}
          </div>
        </div>

        {run.error && (
          <div className="error">
            {t("detail.error")}
            {run.error}
          </div>
        )}
        {run.stop_reason && (
          <div className="muted">
            {t("detail.stopReason")}
            {run.stop_reason}
          </div>
        )}

        <div className="actions">
          {isActive && isRunning && (
            <button onClick={handleStop} className="secondary">
              {t("detail.stopAgent")}
            </button>
          )}
          {run.status === "completed" && <Link to={`/runs/${run.run_id}/report`}>{t("detail.viewReport")}</Link>}
        </div>
      </div>

      <h2>{t("detail.timeline")}</h2>
      <TraceTimeline events={traceEvents} meta={meta} />

      {hasClaimsPipeline(run.pipeline_version) && <ScreeningSection detail={detail} />}
      {hasClaimsPipeline(run.pipeline_version) && (
        <ClaimExtractionSection
          detail={detail}
          claims={claims}
          claimsError={claimsError}
          productCategory={run.product_category}
          onClaimAssigned={refreshClaims}
        />
      )}
    </div>
  );
}

interface ScreeningStatsTotals {
  itemsScreened: number;
  evidenceWorthy: number;
  discarded: number;
  mixedContent: number;
  hasProductSignalCount: number;
}

function sumScreeningStats(detail: RunDetailData): ScreeningStatsTotals {
  const totals: ScreeningStatsTotals = {
    itemsScreened: 0,
    evidenceWorthy: 0,
    discarded: 0,
    mixedContent: 0,
    hasProductSignalCount: 0,
  };
  for (const event of detail.trace_events) {
    if (event.step_type !== "screening") continue;
    const payload = event.payload;
    totals.itemsScreened += Number(payload.items_screened ?? 0);
    totals.evidenceWorthy += Number(payload.evidence_worthy ?? 0);
    totals.discarded += Number(payload.discarded ?? 0);
    totals.mixedContent += Number(payload.mixed_content ?? 0);
    totals.hasProductSignalCount += Number(payload.has_product_signal_count ?? 0);
  }
  return totals;
}

function ScreeningSection({ detail }: { detail: RunDetailData }) {
  const { t } = useLanguage();
  const stats = sumScreeningStats(detail);

  if (stats.itemsScreened === 0) return null;

  return (
    <section className="card" style={{ marginTop: "var(--space-4)" }}>
      <h2>{t("detail.screening.title")}</h2>
      <div className="kpi-grid">
        <KpiCard label={t("detail.screening.itemsScreened")} value={stats.itemsScreened} />
        <KpiCard label={t("detail.screening.evidenceWorthy")} value={stats.evidenceWorthy} />
        <KpiCard label={t("detail.screening.discarded")} value={stats.discarded} />
        <KpiCard label={t("detail.screening.mixedContent")} value={stats.mixedContent} />
        <KpiCard label={t("detail.screening.hasProductSignal")} value={stats.hasProductSignalCount} />
      </div>
    </section>
  );
}

function ClaimExtractionSection({
  detail,
  claims,
  claimsError,
  productCategory,
  onClaimAssigned,
}: {
  detail: RunDetailData;
  claims: Claim[] | null;
  claimsError: string | null;
  productCategory: string;
  onClaimAssigned: () => void;
}) {
  const { t, language } = useLanguage();
  const stats = sumClaimStats(detail);

  if (stats.sourceItemsProcessed === 0) {
    return (
      <section className="card" style={{ marginTop: "var(--space-4)" }}>
        <h2>{t("detail.claims.title")}</h2>
        <p className="muted">{t("detail.claims.notStarted")}</p>
      </section>
    );
  }

  return (
    <section className="card" style={{ marginTop: "var(--space-4)" }}>
      <h2>{t("detail.claims.title")}</h2>
      <div className="kpi-grid">
        <KpiCard label={t("detail.claims.processedItems")} value={stats.sourceItemsProcessed} />
        <KpiCard label={t("detail.claims.itemsWithClaims")} value={stats.itemsWithClaims} />
        <KpiCard label={t("detail.claims.totalClaims")} value={stats.claimsTotal} />
        <KpiCard label={t("detail.claims.llmClaims")} value={stats.llmClaims} />
        <KpiCard label={t("detail.claims.fallbackClaims")} value={stats.fallbackClaims} />
        <KpiCard label={t("detail.claims.invalidClaims")} value={stats.invalidClaims} />
        <KpiCard label={t("detail.claims.extractionFailures")} value={stats.extractionFailures} />
        <KpiCard label={t("detail.claims.rawClaims")} value={stats.rawClaimsExtracted} />
        <KpiCard label={t("detail.claims.duplicatesRemoved")} value={stats.duplicatesRemoved} />
        <KpiCard label={t("detail.claims.claimsMerged")} value={stats.claimsMerged} />
        <KpiCard label={t("detail.claims.safetyCapTruncations")} value={stats.safetyCapTruncations} />
      </div>

      <details className="tech-details" style={{ marginTop: "var(--space-4)" }}>
        <summary>{t("detail.claims.viewClaims", { n: claims?.length ?? 0 })}</summary>
        {claimsError && <p className="error">{claimsError}</p>}
        {!claimsError && claims === null && <p className="muted">{t("detail.claims.loading")}</p>}
        {!claimsError && claims !== null && claims.length === 0 && <p className="muted">{t("detail.claims.empty")}</p>}
        {!claimsError && claims !== null && claims.length > 0 && (
          <div className="evidence-groups">
            {claims.map((claim) => (
              <div className="evidence-group" key={claim.claim_id} style={{ padding: "var(--space-3)" }}>
                <div className="evidence-group-body" style={{ display: "block" }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6, flexWrap: "wrap" }}>
                    <span className="severity-pill" style={{ background: "var(--sev-medium-bg)", color: "var(--sev-medium-fg)" }}>
                      {t(`claimType.${claim.claim_type}`)}
                    </span>
                    <span className="evidence-group-aspect">{translateAspect(claim.aspect_raw, language)}</span>
                    <span className="muted">{claim.extraction_method === "llm" ? "LLM" : "fallback_rules"}</span>
                    <span className="muted">{Math.round(claim.confidence * 100)}%</span>
                  </div>
                  <div className="evidence-quote">{claim.statement}</div>
                  {claim.merge_count > 1 && (
                    <div className="muted" style={{ marginTop: 6 }}>
                      {t("detail.claims.mergedFrom", { n: claim.merge_count })}
                    </div>
                  )}
                  <div className="muted" style={{ marginTop: 6 }}>
                    {t("detail.claims.notVerbatim")}
                    {claim.original_excerpt && (
                      <>
                        {" "}
                        {t("detail.claims.originalText")} "{claim.original_excerpt}"{" "}
                        {claim.original_source_url && (
                          <a href={claim.original_source_url} target="_blank" rel="noreferrer">
                            {t("detail.claims.viewOriginal")}
                          </a>
                        )}
                      </>
                    )}
                  </div>
                  {claim.merged_excerpts && claim.merged_excerpts.length > 0 && (
                    <div className="muted" style={{ marginTop: 6 }}>
                      {t("detail.claims.additionalExcerpts")}{" "}
                      {claim.merged_excerpts.map((excerpt, i) => (
                        <span key={i}>
                          "{excerpt}"{i < claim.merged_excerpts!.length - 1 ? "; " : ""}
                        </span>
                      ))}
                    </div>
                  )}
                  <ManualCategorizeControl claim={claim} productCategory={productCategory} onAssigned={onClaimAssigned} />
                </div>
              </div>
            ))}
          </div>
        )}
      </details>
    </section>
  );
}

// Phase 3, Stage 8/9: the only existing Claim-level review surface in this
// frontend is this per-claim block inside ClaimExtractionSection -- this
// control is mounted here rather than in a new, unrelated Claims dashboard.
// override_manual is never exposed here: this control always calls
// POST /claims/{id}/categorize (which the backend always applies with
// override_manual=True on its own side, since a reviewer's direct action is
// always allowed to change a claim's category) -- there is no merchant-facing
// way to pass override_manual for an *automatic* recategorization pass, by
// design, matching the plan's "not a normal merchant action" requirement.
function ManualCategorizeControl({
  claim,
  productCategory,
  onAssigned,
}: {
  claim: Claim;
  productCategory: string;
  onAssigned: () => void;
}) {
  const { t } = useLanguage();
  const [categories, setCategories] = useState<CanonicalCategory[] | null>(null);
  const [selectedCategoryId, setSelectedCategoryId] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .listCategories(productCategory)
      .then((data) => {
        if (!cancelled) setCategories(data);
      })
      .catch(() => {
        if (!cancelled) setCategories([]);
      });
    return () => {
      cancelled = true;
    };
  }, [productCategory]);

  // Deprecated/alias categories are never valid manual-assignment targets --
  // the backend rejects them (409) too; filtering them out of the dropdown
  // is a UX convenience, not the source of truth.
  const assignableCategories = (categories ?? []).filter((category) => category.status !== "deprecated" && category.alias_of === null);

  async function handleAssign() {
    if (!selectedCategoryId || pending) return;
    setPending(true);
    setError(null);
    setSuccess(false);
    try {
      await api.manuallyCategorizeClaim(claim.claim_id, selectedCategoryId);
      setSuccess(true);
      onAssigned();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="manual-categorize" style={{ marginTop: 8, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      {claim.categorization_method === "manual" && (
        <span className="severity-pill" style={{ background: "var(--sev-low-bg)", color: "var(--sev-low-fg)" }}>
          {t("taxonomy.manualAssignment.current")}
        </span>
      )}
      <select
        value={selectedCategoryId}
        onChange={(event) => setSelectedCategoryId(event.target.value)}
        disabled={pending || categories === null}
      >
        <option value="">{t("taxonomy.manualAssignment.placeholder")}</option>
        {assignableCategories.map((category) => (
          <option key={category.category_id} value={category.category_id}>
            {category.canonical_label} ({t(`taxonomy.status.${category.status}`)})
          </option>
        ))}
      </select>
      <button type="button" className="secondary" onClick={handleAssign} disabled={pending || !selectedCategoryId}>
        {pending ? t("taxonomy.manualAssignment.pending") : t("taxonomy.manualAssignment.action")}
      </button>
      {success && <span className="muted">{t("taxonomy.manualAssignment.success")}</span>}
      {error && <span className="error">{error}</span>}
    </div>
  );
}
