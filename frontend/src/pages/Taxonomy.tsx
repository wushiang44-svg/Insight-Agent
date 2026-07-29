import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { CanonicalCategory, CanonicalCategoryStatus, CategoryAuditLogEntry } from "../api";
import { useLanguage } from "../lib/i18n";

// "floor_damage" -> "Floor Damage" -- display only, never fed back to the API
// or used to overwrite what's actually stored. Reuses the same simple
// underscore/hyphen-to-space + title-case transform aspect labels already get
// elsewhere in this app (see lib/aspectTranslations.ts's own titleCase,
// duplicated locally here since that module's dictionary is scoped to review
// aspects specifically, not canonical taxonomy labels).
function formatLabel(label: string): string {
  return label.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function isKnownStatus(status: string): status is CanonicalCategoryStatus {
  return status === "proposed" || status === "approved" || status === "deprecated";
}

// Renders neutrally for any status the frontend doesn't recognize yet (e.g. a
// future backend value) rather than crashing or mislabeling it as active.
function StatusPill({ status }: { status: string }) {
  const { t } = useLanguage();
  if (!isKnownStatus(status)) {
    return (
      <span className="severity-pill" style={{ background: "var(--track)", color: "var(--muted)" }}>
        {status}
      </span>
    );
  }
  const styles: Record<CanonicalCategoryStatus, { bg: string; fg: string }> = {
    proposed: { bg: "var(--sev-medium-bg)", fg: "var(--sev-medium-fg)" },
    approved: { bg: "var(--sev-low-bg)", fg: "var(--sev-low-fg)" },
    deprecated: { bg: "var(--track)", fg: "var(--muted)" },
  };
  const style = styles[status];
  return (
    <span className="severity-pill" style={{ background: style.bg, color: style.fg }}>
      {t(`taxonomy.status.${status}`)}
    </span>
  );
}

export function Taxonomy() {
  const { t } = useLanguage();
  const [searchParams, setSearchParams] = useSearchParams();
  const productCategoryParam = searchParams.get("product_category") ?? "";
  const [productCategoryInput, setProductCategoryInput] = useState(productCategoryParam);
  const [statusFilter, setStatusFilter] = useState<CanonicalCategoryStatus | "">("");
  const [labelLookup, setLabelLookup] = useState("");

  const [categories, setCategories] = useState<CanonicalCategory[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const activeProductCategory = productCategoryParam.trim();

  const loadCategories = useCallback(() => {
    if (!activeProductCategory) {
      setCategories(null);
      return;
    }
    setCategories(null);
    setListError(null);
    api
      .listCategories(activeProductCategory, {
        status: statusFilter || undefined,
        canonicalLabel: labelLookup.trim() || undefined,
      })
      .then(setCategories)
      .catch((err) => setListError(err instanceof Error ? err.message : String(err)));
  }, [activeProductCategory, statusFilter, labelLookup]);

  useEffect(() => {
    loadCategories();
  }, [loadCategories]);

  function handleBrowse(event: React.FormEvent) {
    event.preventDefault();
    setSearchParams(productCategoryInput.trim() ? { product_category: productCategoryInput.trim() } : {});
    setSelectedId(null);
  }

  return (
    <div className="page">
      <div className="page-header">
        <h1>{t("taxonomy.title")}</h1>
      </div>

      <section className="card">
        <form onSubmit={handleBrowse} className="form-row" style={{ alignItems: "flex-end" }}>
          <label>
            {t("taxonomy.productCategory")}
            <input
              value={productCategoryInput}
              onChange={(event) => setProductCategoryInput(event.target.value)}
              placeholder={t("taxonomy.productCategory.placeholder")}
            />
          </label>
          <button type="submit">{t("taxonomy.browse")}</button>
        </form>

        {activeProductCategory && (
          <div className="form-row" style={{ marginTop: "var(--space-3)" }}>
            <label>
              {t("taxonomy.filter.status")}
              <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as CanonicalCategoryStatus | "")}>
                <option value="">{t("taxonomy.filter.allStatuses")}</option>
                <option value="proposed">{t("taxonomy.status.proposed")}</option>
                <option value="approved">{t("taxonomy.status.approved")}</option>
                <option value="deprecated">{t("taxonomy.status.deprecated")}</option>
              </select>
            </label>
            <label>
              {t("taxonomy.filter.labelLookup")}
              <input
                value={labelLookup}
                onChange={(event) => setLabelLookup(event.target.value)}
                placeholder={t("taxonomy.filter.labelLookup.placeholder")}
              />
            </label>
          </div>
        )}
      </section>

      {!activeProductCategory && <p className="muted">{t("taxonomy.enterProductCategory")}</p>}

      {activeProductCategory && (
        <div className="two-col">
          <section className="card">
            {listError && <p className="error">{listError}</p>}
            {!listError && categories === null && <p className="muted">{t("taxonomy.loading")}</p>}
            {!listError && categories !== null && categories.length === 0 && <p className="muted">{t("taxonomy.empty")}</p>}
            {!listError && categories !== null && categories.length > 0 && (
              <div className="evidence-groups">
                {categories.map((category) => (
                  <button
                    type="button"
                    key={category.category_id}
                    className="taxonomy-list-row"
                    onClick={() => setSelectedId(category.category_id)}
                    style={{
                      display: "block",
                      width: "100%",
                      textAlign: "left",
                      background: selectedId === category.category_id ? "var(--track)" : "transparent",
                      border: "1px solid var(--border)",
                      borderRadius: 6,
                      padding: "var(--space-3)",
                      marginBottom: "var(--space-2)",
                      color: "var(--text)",
                    }}
                  >
                    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                      <StatusPill status={category.status} />
                      <span style={{ fontWeight: 600 }}>{formatLabel(category.canonical_label)}</span>
                    </div>
                    {category.alias_of && (
                      <div className="muted" style={{ marginTop: 4 }}>
                        {t("taxonomy.aliasArrow", { source: formatLabel(category.canonical_label), target: category.alias_of })}
                      </div>
                    )}
                  </button>
                ))}
              </div>
            )}
          </section>

          <section>
            {selectedId ? (
              <CategoryDetail categoryId={selectedId} onChanged={loadCategories} />
            ) : (
              <div className="card">
                <p className="muted">{t("taxonomy.selectACategory")}</p>
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function CategoryDetail({
  categoryId,
  onChanged,
}: {
  categoryId: string;
  onChanged: () => void;
}) {
  const { t } = useLanguage();
  const [category, setCategory] = useState<CanonicalCategory | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(() => {
    setCategory(null);
    setNotFound(false);
    setLoadError(null);
    api
      .getCategory(categoryId)
      .then(setCategory)
      .catch((err) => {
        const message = err instanceof Error ? err.message : String(err);
        if (message.toLowerCase().includes("not found")) setNotFound(true);
        else setLoadError(message);
      });
  }, [categoryId]);

  useEffect(() => {
    load();
  }, [load]);

  function handleChanged() {
    load();
    onChanged();
  }

  if (notFound) {
    return (
      <div className="card">
        <p className="error">{t("taxonomy.error.categoryGone")}</p>
      </div>
    );
  }
  if (loadError) return <div className="card error">{loadError}</div>;
  if (!category) return <div className="card muted">{t("taxonomy.loading")}</div>;

  return (
    <div className="card">
      <div className="section-header">
        <h2>{formatLabel(category.canonical_label)}</h2>
        <StatusPill status={category.status} />
      </div>

      <dl className="taxonomy-fields">
        <dt>{t("taxonomy.field.id")}</dt>
        <dd>{category.category_id}</dd>
        <dt>{t("taxonomy.productCategory")}</dt>
        <dd>{category.product_category}</dd>
        <dt>{t("taxonomy.field.canonicalLabel")}</dt>
        <dd>{category.canonical_label}</dd>
        <dt>{t("taxonomy.field.createdAt")}</dt>
        <dd>{category.created_at}</dd>
        <dt>{t("taxonomy.field.updatedAt")}</dt>
        <dd>{category.updated_at}</dd>
        {category.alias_of && (
          <>
            <dt>{t("taxonomy.field.aliasOf")}</dt>
            <dd>{category.alias_of}</dd>
          </>
        )}
      </dl>

      {!category.alias_of && category.status !== "deprecated" && (
        <div className="taxonomy-actions" style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)", marginTop: "var(--space-4)" }}>
          {category.status === "proposed" && <ApproveAction category={category} onDone={handleChanged} />}
          <RenameAction category={category} onDone={handleChanged} />
          {/* Merge/deprecate refresh the SAME detail view in place rather than
              clearing the selection -- the now-deprecated/aliased category's
              own detail (status pill, alias_of field) is exactly what shows it
              has become inactive, and the history below stays visible too. */}
          <MergeAction category={category} onDone={handleChanged} />
          <DeprecateAction category={category} onDone={handleChanged} />
        </div>
      )}

      <CategoryHistory categoryId={category.category_id} />
    </div>
  );
}

function ApproveAction({ category, onDone }: { category: CanonicalCategory; onDone: () => void }) {
  const { t } = useLanguage();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleApprove() {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await api.approveCategory(category.category_id);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <div>
      <button type="button" onClick={handleApprove} disabled={pending}>
        {pending ? t("taxonomy.approve.pending") : t("taxonomy.approve")}
      </button>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function RenameAction({ category, onDone }: { category: CanonicalCategory; onDone: () => void }) {
  const { t } = useLanguage();
  // Prefilled with the exact stored canonical_label -- never the display-
  // formatted version -- so an unedited submission sends back the original
  // string untouched.
  const [label, setLabel] = useState(category.canonical_label);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setLabel(category.canonical_label), [category.canonical_label]);

  async function handleRename(event: React.FormEvent) {
    event.preventDefault();
    if (pending) return;
    const trimmed = label.trim();
    if (!trimmed) {
      setError(t("taxonomy.rename.emptyError"));
      return;
    }
    setPending(true);
    setError(null);
    try {
      await api.renameCategory(category.category_id, trimmed);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={handleRename}>
      <label>
        {t("taxonomy.rename")}
        <input value={label} onChange={(event) => setLabel(event.target.value)} disabled={pending} />
      </label>
      <button type="submit" disabled={pending || !label.trim()} style={{ marginTop: "var(--space-2)" }}>
        {pending ? t("taxonomy.rename.pending") : t("taxonomy.rename.submit")}
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

function MergeAction({
  category,
  onDone,
}: {
  category: CanonicalCategory;
  onDone: () => void;
}) {
  const { t } = useLanguage();
  const [candidates, setCandidates] = useState<CanonicalCategory[] | null>(null);
  const [targetId, setTargetId] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listCategories(category.product_category)
      .then((all) =>
        setCandidates(
          // Basic preventative filtering only (excludes the source itself and
          // any deprecated/alias category) -- the backend remains the
          // authoritative validator for self-merge, cycles, and deprecated
          // targets; this list is a UX convenience, never a guarantee.
          all.filter((candidate) => candidate.category_id !== category.category_id && candidate.status !== "deprecated" && candidate.alias_of === null),
        ),
      )
      .catch(() => setCandidates([]));
  }, [category.product_category, category.category_id]);

  async function handleConfirmMerge() {
    if (!targetId || pending) return;
    setPending(true);
    setError(null);
    try {
      await api.mergeCategories(category.category_id, targetId);
      setConfirming(false);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  }

  const targetLabel = candidates?.find((c) => c.category_id === targetId);

  return (
    <div>
      <label>
        {t("taxonomy.merge.target")}
        <select
          value={targetId}
          onChange={(event) => {
            setTargetId(event.target.value);
            setConfirming(false);
          }}
          disabled={pending || candidates === null}
        >
          <option value="">{t("taxonomy.merge.targetPlaceholder")}</option>
          {(candidates ?? []).map((candidate) => (
            <option key={candidate.category_id} value={candidate.category_id}>
              {formatLabel(candidate.canonical_label)}
            </option>
          ))}
        </select>
      </label>
      {!confirming ? (
        <button type="button" onClick={() => setConfirming(true)} disabled={pending || !targetId} style={{ marginTop: "var(--space-2)" }}>
          {t("taxonomy.merge")}
        </button>
      ) : (
        <div style={{ marginTop: "var(--space-2)" }}>
          <p>
            {t("taxonomy.merge.confirm", {
              source: formatLabel(category.canonical_label),
              target: targetLabel ? formatLabel(targetLabel.canonical_label) : targetId,
            })}
          </p>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="button" onClick={handleConfirmMerge} disabled={pending}>
              {pending ? t("taxonomy.merge.pending") : t("taxonomy.merge.confirmButton")}
            </button>
            <button type="button" className="secondary" onClick={() => setConfirming(false)} disabled={pending}>
              {t("taxonomy.cancel")}
            </button>
          </div>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function DeprecateAction({ category, onDone }: { category: CanonicalCategory; onDone: () => void }) {
  const { t } = useLanguage();
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleConfirmDeprecate() {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await api.deprecateCategory(category.category_id);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPending(false);
    }
  }

  if (!confirming) {
    return (
      <div>
        <button type="button" className="secondary" onClick={() => setConfirming(true)}>
          {t("taxonomy.deprecate")}
        </button>
        {error && <p className="error">{error}</p>}
      </div>
    );
  }

  return (
    <div>
      <p>{t("taxonomy.deprecate.confirm")}</p>
      <div style={{ display: "flex", gap: 8 }}>
        <button type="button" onClick={handleConfirmDeprecate} disabled={pending}>
          {pending ? t("taxonomy.deprecate.pending") : t("taxonomy.deprecate.confirmButton")}
        </button>
        <button type="button" className="secondary" onClick={() => setConfirming(false)} disabled={pending}>
          {t("taxonomy.cancel")}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

const AUDIT_ACTION_KEYS: Record<string, string> = {
  approve: "taxonomy.history.action.approve",
  rename: "taxonomy.history.action.rename",
  merge: "taxonomy.history.action.merge",
  deprecate: "taxonomy.history.action.deprecate",
};

function formatAuditDetail(entry: CategoryAuditLogEntry): string {
  return Object.entries(entry.detail)
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(", ");
}

function CategoryHistory({ categoryId }: { categoryId: string }) {
  const { t } = useLanguage();
  const [history, setHistory] = useState<CategoryAuditLogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setHistory(null);
    setError(null);
    api
      .getCategoryHistory(categoryId)
      .then(setHistory)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [categoryId]);

  return (
    <section style={{ marginTop: "var(--space-4)" }}>
      <h3>{t("taxonomy.history")}</h3>
      {error && <p className="error">{error}</p>}
      {!error && history === null && <p className="muted">{t("taxonomy.loading")}</p>}
      {!error && history !== null && history.length === 0 && <p className="muted">{t("taxonomy.history.empty")}</p>}
      {!error && history !== null && history.length > 0 && (
        <ul className="roadmap-why">
          {history.map((entry) => (
            <li key={entry.id}>
              <span className="muted">{entry.created_at}</span> — {t(AUDIT_ACTION_KEYS[entry.action] ?? "taxonomy.history.action.unknown")}
              {Object.keys(entry.detail).length > 0 && <span className="muted"> ({formatAuditDetail(entry)})</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
