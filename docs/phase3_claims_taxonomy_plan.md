# Phase 3 — Claims as the Primary Input to the Merchant Report

**Revision 2** addressed six review findings (proposed-category visibility, alias-model precision,
categorization provenance, failure-vs-no-match separation, unscoped bulk updates, `summarize()`
decomposition) plus three secondary considerations (rename op, label uniqueness, source-specific thread
counting).

**Revision 3** added three final clarifications before implementation began: (1) proposed categories get
a visible "Pending review" badge in the merchant-facing `Report.tsx`, not just a backend `category_status`
field nobody renders; (2) the Claims-report path is gated by an explicit eligibility check (categorization
completed + a minimum resolved ratio), not merely "claims is non-empty," with a traceable fallback
reason recorded on the `Report` itself; (3) new-category `canonical_label` generation is concretely
specified (LLM-assisted cleanup where already-available, deterministic normalized fallback otherwise).
**Stage 1 (`canonical_categories`/`category_audit_log` tables + `Storage` CRUD/transition methods) is
implemented and tested** — see the Implementation order below for exact scope and status.

**Revision 4** (this revision) locks in rerun-safety requirements for Stage 2/3, before either is
implemented: `categorize_claims()` must default to processing only uncategorized/unresolved claims,
requires an explicit `force=True` for a full reclassification pass, and even under `force` must never
touch a `categorization_method="manual"` claim without a *second*, separately-named `override_manual=True`
— enforced at both the selection layer (`categorize_claims()`) and the write layer
(`storage.set_claims_categorization()`'s `WHERE` clause itself), not just by caller discipline. Also
specifies "transactional writes where appropriate" concretely: atomic per aspect-group (category
resolution/creation + the claims that share it write together or not at all), not one all-or-nothing
transaction across a whole run's categorization pass — consistent with this codebase's existing
failure-isolation philosophy elsewhere.

## Context

The Customer Demand Intelligence Pipeline (Phase 1 → 1.5 → 1.6 → 2, committed through `61c0aa6`/`c06b0d3`)
extracts clean, atomic, properly-typed Claims from every piece of Evidence — but a real end-to-end
validation run (robot vacuum cleaners, 155 evidence, 285 claims, `run_55025c50e81b`) confirmed the
merchant-facing report never uses them. `summarize()` (`react_agent.py:480`) builds the entire `Report`
from `list[Evidence]`, grouping by `Evidence.aspect` — a free-text string written once per review by
`screen_item()`. That free-text aspect naming visibly fragmented the shipped report (`"floor_damage"`
and `"floor damage"` appeared as two separate top-5 pain points that were clearly one ~20-item issue).
`Claim.canonical_category` exists in the schema specifically to solve this and has never been populated
anywhere in the codebase (confirmed by grep — every reference is plumbing, zero assignment sites).

Phase 3 closes this gap: build the taxonomy that gives Claims a normalized topic identity, categorize
claims against it as a separate batch step, and rebuild the report on top of categorized Claims instead
of raw Evidence. Two previously-deferred validation findings are addressed as part of this (aspect
fragmentation, and conditional surfacing of shipping/service claims); thread-concentration dampening
(#5) stays explicitly deferred, and independent-user support counts are recorded as a known v1
limitation since no collector captures author identity today.

## Data model

**New table `canonical_categories`** (added to `Storage.migrate()`'s `CREATE TABLE IF NOT EXISTS` block,
same as `claims` was originally added):

```sql
CREATE TABLE IF NOT EXISTS canonical_categories (
    category_id TEXT PRIMARY KEY,        -- "cc_" + sha1(product_category|seed label)[:16]
    product_category TEXT NOT NULL,      -- scoping key, normalized (lower/stripped) at write time
    canonical_label TEXT NOT NULL,       -- English display label, e.g. "floor damage"
    normalized_label TEXT NOT NULL,      -- lower/whitespace-collapsed canonical_label, computed at write time
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | deprecated
    alias_of TEXT,                       -- category-to-category MERGE target only (see below) — NULL = not merged
    first_seen_aspect_raw TEXT NOT NULL, -- provenance for a human reviewing proposals
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_categories_product ON canonical_categories(product_category);
CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_product_label
    ON canonical_categories(product_category, normalized_label);
```

**Uniqueness** (secondary consideration, now built in): the unique index on `(product_category,
normalized_label)` is a DB-level backstop against two independent "propose new category" writes minting
near-duplicate rows (e.g. a race, or the matching tiers missing each other on the same run). The
propose-new write path must handle the resulting `IntegrityError` by re-`SELECT`ing the existing row and
treating it as a match instead of surfacing the constraint violation — noted explicitly so this isn't
missed during implementation.

`Claim.canonical_category` (`models.py:257`) already stores a plain string and needs no migration — it
becomes a foreign-key-shaped reference to `category_id`, resolved through `alias_of` chains at **read
time** (never eagerly rewritten on merge — see "Report generation architecture" below). A `category_id`
that resolves to `status = 'deprecated'` with no `alias_of` falls back to an explicit "uncategorized"
bucket per claim_type at aggregation time (never silently dropped — see "Claims-based aggregation").

No stored support counts (claim/evidence/thread counts) on this table — computed on demand via joins
against `claims`/`evidence` when building the report or listing proposals, to avoid staleness after a
merge or a fresh categorization run.

### Category alias model — `alias_of` is category-to-category merge only

`alias_of` means exactly one thing: "this `canonical_categories` row has been merged into another
`canonical_categories` row." It is **not** a string-synonym mechanism. There is no separate table
storing known raw-string variants (e.g. "connectivity" / "app connectivity" / "wifi drops") beyond the
single `canonical_label` a category is matched against — that is a deliberate scope decision, not an
oversight:

- **Built now**: category-level merge (`alias_of`), one hop deep (enforced — see Validation below),
  resolved at read time.
- **Explicitly deferred, not built**: a `category_aliases` table (`category_id`, `alias_text`,
  `source_claim_id`) that would accumulate known-good variant phrasings per category over time to widen
  what the lexical-match tier can catch on its own, without an LLM call. If real proposed-category
  volume shows the lexical + LLM tiers still miss too many recurring phrasings even against
  `canonical_label`, this is the natural follow-up — not part of Phase 3.

### Categorization provenance on `Claim`

Three new nullable columns on `claims` (additive migration, same `try/except sqlite3.OperationalError`
pattern as every prior claims-table addition):

```sql
ALTER TABLE claims ADD COLUMN categorization_status TEXT;      -- "resolved" | "unresolved" | NULL (not attempted)
ALTER TABLE claims ADD COLUMN categorization_method TEXT;      -- "lexical_match" | "llm_match" | "proposed_new" | "manual" | NULL
ALTER TABLE claims ADD COLUMN categorization_confidence REAL;  -- populated only for lexical_match / llm_match
```

Matching fields added to the `Claim` dataclass (`models.py`), `_row_to_claim`, and the insert/replace
statements in `storage.py`. Semantics:

- `categorization_status = NULL` — categorization hasn't run for this claim yet (old claims predating
  Phase 3, or the kill switch was off when it was extracted).
- `categorization_status = "unresolved"` — categorization was attempted and **failed at the
  infrastructure level** (LLM call raised/timed out/returned malformed JSON). `canonical_category` stays
  `NULL`. This is the retryable state — see "Normalization flow" below for why this is a distinct state
  from a genuine no-match.
- `categorization_status = "resolved"` — a `canonical_category` was assigned, either by matching an
  existing category (`categorization_method = "lexical_match" | "llm_match"`, `categorization_confidence`
  populated) or by minting a new proposed category because no existing one fit
  (`categorization_method = "proposed_new"`, confidence `NULL` — there is nothing to score a confidence
  against when the outcome is "this is new").
- `categorization_method = "manual"` — reserved for a human directly reassigning one claim's category
  (see the new `POST /claims/{id}/categorize` route below). No separate audit table for this — the field
  itself is the observability the review asked for; a full history log is disproportionate for a
  single-claim correction (unlike category-level actions, which get `category_audit_log` because they
  can each affect many claims at once through alias resolution).

## Taxonomy lifecycle

Statuses: `proposed` (auto-created, unreviewed) → `approved` (human-confirmed) → `deprecated` (retired).
Lifecycle is exposed via new routes (`routes.py`) as stable application-level operations, not direct
database edits — API only this phase, frontend deferred until real proposed-category volume shows what
the actual curation workflow needs to look like:

- `GET /categories?product_category=X&status=proposed` — list categories with computed support counts
  (claim_count, distinct evidence_count, distinct thread_count — see below), for manual review.
- `POST /categories/{id}/approve` — `proposed → approved`.
- `POST /categories/{id}/merge` — `{"into": target_category_id}` — sets `alias_of`.
- `POST /categories/{id}/deprecate` — `status → deprecated`.
- `POST /categories/{id}/rename` — `{"canonical_label": "new label"}` (secondary consideration, now
  included) — updates `canonical_label`/`normalized_label`/`updated_at`. Rejected with 409 if the new
  `normalized_label` collides with a different existing category in the same `product_category` (the
  unique index catches this at the DB layer; the route translates that into a clean error rather than a
  raw constraint failure).
- `POST /claims/{id}/categorize` — `{"category_id": "..."}` — manual single-claim override, sets
  `canonical_category`, `categorization_status="resolved"`, `categorization_method="manual"`,
  `categorization_confidence=NULL`. Validates the target category belongs to the claim's run's
  `product_category` and is not itself an alias.

**Validation** (each endpoint goes through a single `storage`-layer transition function, not a raw
`UPDATE` — so every caller, including a future frontend, gets the same guarantees):
- `approve`/`deprecate`/`rename` reject a category that is already `deprecated` (409, not a silent
  no-op) or that is itself an alias (`alias_of IS NOT NULL`) — an alias has no independent identity to
  change; act on its merge target instead.
- `merge`'s target must exist, belong to the *same* `product_category`, and not already be an alias of
  something else — chained aliases (A→B→C) are rejected outright rather than silently resolved, so the
  alias graph the aggregation-time resolver walks is always exactly one hop deep.
- `merge` rejects `into == id` (self-merge) and rejects creating a cycle.
- A category with `alias_of` set cannot itself become a merge target — callers must merge into the root
  of a chain, never into an alias.
- `rename` rejects a collision with another category's `normalized_label` in the same `product_category`.

**Auditability**: every category-level transition (`approve`/`merge`/`deprecate`/`rename`) is recorded
in a new, purely additive `category_audit_log` table — not user-attributed (this app has no auth/user-
identity system anywhere today, so "who" isn't a field that can be honestly populated), but a durable
record of *what* happened and *when*. Reuses the same `action` + JSON `detail` payload shape
`TraceEvent` already established (`models.py:180`) rather than inventing bespoke per-action columns:

```sql
CREATE TABLE IF NOT EXISTS category_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id TEXT NOT NULL,
    action TEXT NOT NULL,     -- "approve" | "merge" | "deprecate" | "rename"
    detail TEXT NOT NULL,     -- JSON, shape depends on action: merge->{"target_category_id"},
                               -- rename->{"old_label","new_label"}, approve/deprecate->{"from_status","to_status"}
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_category_audit_category ON category_audit_log(category_id);
```

`GET /categories/{id}/history` exposes this log per category.

## Normalization flow (`pipeline/taxonomy.py`, new module)

A separate batch step, mirroring `pipeline/screening.py`/`pipeline/claims.py`'s existing shape (kill
switch, stats dataclass, LLM-path/fallback split) but operating once per run over *all* claims collected
**for that run**, not per-item — this is what makes it "post-extraction," not bolted onto
`extract_claims()`. `aspect_raw` is never modified; only `Claim.canonical_category` and the three new
provenance columns are written.

```python
def categorize_claims(
    run_id: str, product_category: str, claims: list[Claim], storage: Storage, llm: DeepSeekClient,
    *, force: bool = False, override_manual: bool = False,
) -> CategorizationStats:
```

`categorize_claims` asserts every `claim.run_id == run_id` before writing anything — a defensive check,
not just "trust the caller passed the right list" (see the scoping guarantee below).

### Rerun safety — default selection, `force`, and manual protection

A rerun of `categorize_claims()` (the routine per-run call, or the maintenance retry entry point) must
never silently reclassify a claim someone already resolved, and must never touch a human's manual
override without a second, explicit signal. Selection happens **before** step 1 below, as its own
filtering pass over the input `claims` list:

```python
def _select_claims_to_process(claims: list[Claim], force: bool, override_manual: bool) -> list[Claim]:
    selected = []
    for c in claims:
        if c.categorization_method == "manual" and not override_manual:
            continue  # always skipped unless explicitly overridden, independent of `force`
        if not force and c.categorization_status not in (None, "unresolved"):
            continue  # default: only uncategorized or previously-unresolved claims
        selected.append(c)
    return selected
```

- **Default** (`force=False, override_manual=False`) — the routine, safe mode used by the live per-run
  wiring in `run_react_loop` (see "Report generation architecture" below) and by the retry-unresolved
  maintenance entry point: only claims with `categorization_status in (NULL, "unresolved")` are
  considered at all; anything already `"resolved"` (lexical/LLM match, a prior proposal, or a manual
  override) is left untouched. This is what makes the routine call idempotent and rerun-safe by default
  — no flag needed to "just retry what's actually unresolved."
- **`force=True`** — widens selection to every claim in the input list, including already-`"resolved"`
  ones, for a deliberate full reclassification (e.g. after a taxonomy overhaul). Still does **not** touch
  `categorization_method == "manual"` claims.
- **`override_manual=True`** — the only way a manual assignment is ever reconsidered, and only takes
  effect together with `force=True` in practice (there's nothing to "override" on a manual claim if it's
  already excluded by the default's `categorization_status` filter — a manual claim's status is always
  `"resolved"`). Two independent, separately-named flags rather than one, so a caller can never widen
  scope to "reprocess everything" and accidentally sweep up manual corrections as a side effect.
- Skipped counts are observable, not silent: `CategorizationStats` gains `skipped_already_resolved` and
  `skipped_manual_protected`.
- The live per-run call in `run_react_loop` always uses the default (`force=False, override_manual=False`)
  — force/override are only ever passed explicitly by a human running the maintenance entry point, never
  by the automatic per-run wiring.

`storage.set_claims_categorization()` enforces the manual-protection guarantee **again, at the write
layer itself** — not just by trusting `categorize_claims()`'s pre-filtering — so a future caller can't
accidentally clobber a manual assignment by forgetting to filter first:

```python
def set_claims_categorization(
    self, claim_ids: list[str], canonical_category: str | None, status: str,
    method: str | None, confidence: float | None, *, override_manual: bool = False,
) -> int:  # returns rows actually updated, which can be fewer than len(claim_ids) if some were manual-protected
```

implemented with the guard baked directly into the `WHERE` clause (`AND (categorization_method IS NOT
'manual' OR :override_manual)`), so the protection holds regardless of caller discipline — the same
"the primitive itself can't misbehave" principle already used for the `claim_id`-only write scoping.

### Transactional writes

"Transactional" at the granularity of **one logical unit of work**, not the whole run's categorization
pass — consistent with the rest of this codebase's failure-isolation philosophy (one evidence item's
`extract_claims()` failure never aborts the whole run; the same should hold here). The unit that must be
atomic is **"resolve or create the category for aspect X, then write that result onto every claim
sharing aspect X"** — a mid-write DB failure must never leave a new `canonical_categories` row committed
with no claim pointing at it yet, or vice versa. Concretely: `create_category()`'s existing
self-committing form (Stage 1, already built) is right for its own direct callers (a single human action
= a complete transaction), but `categorize_claims()` needs an internal, non-committing variant (e.g. a
`commit: bool = True` parameter, or a `_get_or_create_category_uncommitted()` helper) so the
category-resolution write and the `set_claims_categorization()` write for that same aspect group share
one `commit()`/`rollback()` at the Stage 3 call site. Different aspect groups within the same run remain
independently atomic, not bundled into one giant all-or-nothing transaction — a failure on one aspect
group's write leaves every other group's already-completed work intact, matching `completed=False`'s
existing role as the run-level signal for "something aborted partway," not a reason to roll back
everything that already succeeded.

1. Collect distinct `aspect_raw` values **within this selected claim list only** (case/whitespace-normalized
   via the same `_normalize()` helper already in `pipeline/claims.py:565` — reuse it, don't duplicate
   it). Never query the database by `aspect_raw` at any point in this flow.
2. For each distinct aspect, load candidate categories for this `product_category` (one
   `storage.list_categories(product_category)` call, cached for the whole batch) with
   `status != 'deprecated'` — both `approved` and `proposed` are matchable, which is what prevents
   near-duplicate proposals from piling up.
3. Tiered decision, directly mirroring the already-approved `_pair_decision`/`_merge_within_review`
   pattern in `pipeline/claims.py:330-420` (lexical `simple_similarity` from `text.py` as a cheap
   candidate filter, batched LLM arbitration only for genuinely ambiguous cases) — **with genuine
   no-match and infrastructure failure now handled as two distinct outcomes, not conflated**:
   - High similarity against an existing category's `canonical_label` → **auto-match**
     (`categorization_status="resolved"`, `method="lexical_match"`, `confidence=`similarity score), no
     LLM call.
   - Moderate similarity against one or more candidates → batched into one LLM call for the whole run
     (mirrors `_verify_ambiguous_pairs_llm`'s one-call-per-review batching), asking whether the aspect is
     the same underlying topic as each nearest candidate.
     - LLM call succeeds and confirms a match → **matched** (`status="resolved"`, `method="llm_match"`,
       `confidence=`LLM-reported score).
     - LLM call succeeds and confirms **no** candidate matches → **genuine no-match**, proceed to
       propose-new below.
     - LLM call **raises/times out/returns malformed JSON** → **infrastructure failure**, NOT a
       no-match. `categorization_status="unresolved"`, `method=NULL`, `canonical_category` stays `NULL`.
       No new category is created. This claim is retried on the next categorization pass (see below).
   - No good match found by either tier, reached via a genuine (non-error) "no" — **propose a new
     category** (`status="resolved"`, `method="proposed_new"`, new `canonical_categories` row with
     `status="proposed"`), with its `canonical_label` chosen per the algorithm below.
4. Write results back **scoped strictly to the selected claim set, by explicit `claim_id`**: group the
   already-in-memory claims by normalized `aspect_raw` (a Python-side grouping, not a SQL `WHERE
   aspect_raw = ?`), then bulk-update by `WHERE claim_id IN (...)` for exactly the claim IDs in that
   group, via `storage.set_claims_categorization()` (signature above) — it never accepts an `aspect_raw`
   or any other loose match key, by construction, so an unscoped "update every claim that happens to
   share this string, across every run and product category" is not representable through this API at
   all. `categorize_claims()` passes its own `override_manual` argument straight through, so the
   write-layer guard and the selection-layer filter always agree.

### Canonical label generation for new proposals

`canonical_label` is never a blind copy of `aspect_raw` — a review can produce an `aspect_raw` that is a
whole clause ("the customer kept mentioning the charging cable feels flimsy and cheap"), which is not
report-safe as a category label. Generation is layered, reusing an LLM call that's already happening
where possible rather than adding new LLM traffic:

- **When the aspect went through the ambiguous-candidate LLM tier** (step 3's second bullet) and came
  back a genuine no-match: the same batched arbitration prompt also asks for a `proposed_label` per
  aspect it says "no match" to (one extra field on an already-planned call, not a new call). If the
  model returns a non-empty, reasonably short label, it's used, run through the same `normalized_label`
  computation as any other category.
- **Deterministic fallback** — used whenever the LLM path wasn't taken at all (an aspect with zero
  candidates skips arbitration entirely, so no call happens to ask), the LLM omitted/emptied the field,
  or the whole categorization call failed (in which case the claim is `unresolved`, not proposed, per
  the failure-vs-no-match split above — so this fallback specifically applies to the "zero candidates"
  case, and to a well-formed LLM response that just didn't include a usable label): normalize
  `aspect_raw` (`_normalize()` from `claims.py`, reused), replace `_`/`-` with spaces, truncate to
  `_MAX_CANONICAL_LABEL_LENGTH` (60) chars at a word boundary (never mid-word), and guard against an
  empty result (falls back to the literal string `"uncategorized topic"` in the vanishingly rare case
  `aspect_raw` normalizes to nothing).
- Whichever source wins, the result goes through the same unique-index-protected write path already
  specified — no special-casing needed there.

Kill switch: `enable_claim_categorization()` (`ENABLE_CLAIM_CATEGORIZATION`, default true), same
naming/behavior convention as `enable_claim_extraction()`/`enable_screening_v2()`.

**Retry path for unresolved claims**: `categorize_claims()`'s own default selection (above) already makes
the *routine* call safely re-runnable — every ordinary invocation naturally only picks up
uncategorized/unresolved claims, so a maintenance entry point retrying failures doesn't need `force` at
all, just needs to gather the right input set: fetch claims explicitly via
`storage.list_claims_by_status(run_id, categorization_status="unresolved")` (or, for a wider sweep, an
explicit `claim_ids` list obtained the same way across several runs) and call `categorize_claims()`
against exactly that explicit set with the default flags. `force`/`override_manual` are reserved for a
deliberate, human-initiated full reclassification (e.g. after a taxonomy overhaul) — never used by this
routine retry path, and never by the live per-run wiring. Still never touches anything by `aspect_raw`
alone. This is also how a taxonomy change (approve/merge/deprecate) gets new proposals reconsidered —
though note that a merge or deprecate **never requires touching the `claims` table at all**, because
resolution happens at read time (next section), not by rewriting `canonical_category` eagerly.

`CategorizationStats` (trace payload, run-level not per-iteration): `claims_total`, `distinct_aspects`,
`lexical_matched`, `llm_matched`, `new_categories_proposed`, `unresolved_failures`,
`skipped_already_resolved`, `skipped_manual_protected`, and **`completed: bool`** — `True` only if the
batch step ran through the entire selected list without an unhandled exception escaping it. Per-claim/
per-aspect failures (an individual LLM call erroring) are already caught and recorded as
`unresolved_failures` internally and never propagate; `completed=False` is reserved for a genuinely
unexpected failure that aborts the batch partway through (e.g. a bug, a DB error mid-loop) —
`categorize_claims()` wraps its own top-level loop in a `try/except` specifically to set this flag and
return partial stats rather than letting the exception crash the whole run. This is what "categorization
completed successfully" means concretely for the eligibility gate below — a distinct signal from "how
many claims actually resolved," which is `unresolved_failures`/`claims_total`.

## Report generation architecture

`summarize()` (`react_agent.py:480`) currently does everything itself. Splitting it into named stages,
per the review: **load → resolve taxonomy → aggregate → build report inputs → generate narrative**.
Loading moves out of `summarize()` entirely — it becomes a pure function of already-loaded data, with no
`Storage`/DB dependency, which also makes it trivially unit-testable with in-memory fixtures.

**1. Load + eligibility gate** (`run_react_loop`, not `summarize()`): after the iteration loop ends and
before summarization. The Claims path is **never selected merely because claims exist** — a dedicated
gate decides, and the reason for either outcome is captured for the `Report` itself, not just a trace
line:

```python
storage.update_run_progress(run_id, iteration, len(collected), RunStatus.SUMMARIZING)
claims: list[Claim] = []
categories: list[CanonicalCategory] = []
cat_stats: CategorizationStats | None = None
if enable_claim_categorization():
    run_claims = storage.list_claims(run_id)          # scoped to this run_id by construction
    cat_stats = categorize_claims(run_id, run.product_category, run_claims, storage, llm)
    trace(iteration, StepType.CATEGORIZATION, ..., asdict(cat_stats))

eligible, fallback_reason = _claims_report_eligible(run_id, storage, cat_stats)
if eligible:
    claims = storage.list_claims(run_id)               # re-read post-categorization
    categories = storage.list_categories(run.product_category)

report = summarize(run_id, run.product_category, collected, claims, categories, llm, fallback_reason)
trace(iteration, StepType.SUMMARY, ..., {
    "evidence_count": len(collected),
    "report_source": "claims" if claims else "legacy_evidence",
    "fallback_reason": fallback_reason,
})
```

`_claims_report_eligible(run_id, storage, cat_stats) -> tuple[bool, str | None]`:

```python
def _claims_report_eligible(run_id, storage, cat_stats) -> tuple[bool, str | None]:
    if not enable_claims_based_report():
        return False, "claims_report_disabled"
    if cat_stats is None:
        return False, "categorization_disabled"
    if not cat_stats.completed:
        return False, "categorization_incomplete"
    if cat_stats.claims_total == 0:
        return False, "no_claims"
    resolved_ratio = (cat_stats.claims_total - cat_stats.unresolved_failures) / cat_stats.claims_total
    if resolved_ratio < claims_report_min_resolved_ratio():
        return False, f"low_resolved_coverage:{resolved_ratio:.2f}"
    return True, None
```

`claims_report_min_resolved_ratio()` reads `CLAIMS_REPORT_MIN_RESOLVED_RATIO` (default `0.7`), same
env-var/kill-switch convention as everywhere else in this codebase, tunable after real-data validation.
This single function covers every case the review named — categorization disabled, the batch step
failing outright, and merely-low coverage — each with its own distinguishable reason string, rather than
inferring intent from an empty list.

**2. Resolve taxonomy** — `_resolve_categories(claims: list[Claim], categories: list[CanonicalCategory])
-> dict[claim_id, ResolvedCategory]`: pure, in-memory, no LLM/DB calls. Builds a `category_id ->
CanonicalCategory` index, walks each claim's `canonical_category` through `alias_of` exactly one hop
(validated one-hop-only at merge time, so this is a single dict lookup, not a loop), and produces a
`ResolvedCategory(label, status)` per claim — `status` is `"approved"` or `"proposed"` for a live
category, or `"uncategorized"` when `canonical_category` is `NULL`, or when it resolves to a
`deprecated` category with no `alias_of`.

**3. Aggregate** — `_aggregate_claims_by_category(claims, resolved, evidence_by_id) ->
dict[(claim_type, category_key), AggregateGroup]`: pure grouping/counting logic, no I/O. `category_key`
is the resolved category's `id` for approved/proposed groups, or the literal string `"uncategorized"`
(one bucket per claim_type, **not** further split by `aspect_raw` — splitting an already-lower-trust,
categorization-failed population by the very free-text field that caused the fragmentation problem in
the first place would reintroduce it for exactly this population). Each group carries a
`category_status` field (`"approved" | "proposed" | "uncategorized"`) straight through to the final
`Report` payload — see "proposed-category visibility" below.

**4. Build report inputs** — `_build_report_inputs(aggregated) -> ReportInputs`: turns the full
aggregate set into the specific lists that become `Report` fields. This is where the
shipping/seller-service **support-threshold gating** is applied (a group only lands in
`shipping_issues`/`seller_service_issues` if it clears the configured minimums — see below); the four
existing sections (`top_pain_points`, `feature_requests`, `praised_aspects`, `competitor_mentions`) are
unfiltered, sorted by count descending, same as `_aggregate_by_aspect()` today. No truncation happens
here — the existing `[:8]`-for-the-LLM-prompt truncation stays inside `_summarize_llm()`, unchanged.

**5. Generate narrative** — `_summarize_llm()`/`_summarize_fallback()` (`react_agent.py:537,583`),
essentially unchanged; they already take pre-aggregated lists + the LLM client, so they don't care
whether those lists came from Claims or legacy Evidence aggregation.

`summarize()` itself becomes a thin orchestrator:

```python
def summarize(
    run_id: str, product_category: str, collected: list[Evidence],
    claims: list[Claim], categories: list[CanonicalCategory], llm: DeepSeekClient,
    fallback_reason: str | None,
) -> Report:
    if claims:
        resolved = _resolve_categories(claims, categories)
        evidence_by_id = {e.evidence_id: e for e in collected}
        aggregated = _aggregate_claims_by_category(claims, resolved, evidence_by_id)
        report_inputs = _build_report_inputs(aggregated)
        report_source = "claims"
    else:
        report_inputs = _build_report_inputs_from_evidence(collected)  # wraps the untouched _aggregate_by_aspect()
        report_source = "legacy_evidence"
    narrative = _summarize_llm(...) if llm.available() else _summarize_fallback(...)
    return Report(
        run_id=run_id, generated_at=utc_now(), **report_inputs,
        report_source=report_source, fallback_reason=fallback_reason,
        ...narrative fields...,
    )
```

`summarize()` no longer decides eligibility itself — the caller (`run_react_loop`) already resolved that
via `_claims_report_eligible()` and passes either a populated or empty `claims` list plus whatever
`fallback_reason` applies (`None` when the Claims path was actually used). `summarize()` just branches
on "were claims handed to me," which by construction only happens when they passed the gate — and stamps
`report_source`/`fallback_reason` onto the `Report` either way, so **every** report (not just fallback
ones) is traceable about which path produced it.

Two new additive columns on `Report` (`models.py:190`, `reports` table migration): `report_source: str`
(`"claims" | "legacy_evidence"`) and `fallback_reason: str | None`. Old report rows (migrated via
`ALTER TABLE ... DEFAULT 'legacy_evidence'` / `DEFAULT NULL`) read back as legacy with no reason, which
is accurate — nothing retroactive, same convention as every prior additive `Report` field.

The legacy `_aggregate_by_aspect()` path stays completely untouched, just wrapped by
`_build_report_inputs_from_evidence()` so both branches converge on the same `ReportInputs` shape before
narrative generation — exactly the same "keep the old path registered but unused" precedent already set
when `REDDIT_API`/`REDDIT_SCRAPER` were kept alive for backward replay after the Reddit Browser
Collector shipped.

## Proposed-category visibility in the report

Directly addressing the review's first point: **a `"proposed"` category is not hidden from the report,
but it is never silently indistinguishable from a reviewed one.** Excluding all `proposed` categories
outright would leave a product line's *first* run with a near-empty report (everything is proposed on a
cold taxonomy) — bad for usability and not what "don't silently become stable" actually requires.
Instead, every aggregate group in every section (including `shipping_issues`/`seller_service_issues`,
independent of the support-threshold gate) carries the `category_status` field from the aggregation
stage straight through to the stored `Report`. A `"proposed"` group is real data, counted and ranked
normally, but explicitly marked as unreviewed — non-silent by construction.

**This phase does render that distinction, just not a curation UI.** `category_status` reaches the
frontend `AspectGroup` type (`api.ts`) and `Report.tsx`'s `AspectSection`/`BarRank` rendering shows a
small "Pending review" badge next to any item where `category_status === "proposed"` — a minimal,
read-only affordance so a merchant reading the report can visually tell a provisional category from an
approved one, without needing the (still-deferred) approve/merge/deprecate admin surface. Concretely:
`BarRank`'s item type (`components/BarRank.tsx`) gains an optional `badge?: string` rendered as a small
pill after the label — generic, not coupled to `category_status` specifically, so the component stays
reusable — and `AspectSection` populates it only for `"proposed"` groups. New EN/ZH i18n keys
(`report.category.pendingReview`) follow the existing dictionary-based i18n convention. `"uncategorized"`
groups (see the aggregation stage) render with their own neutral label, not the pending-review badge —
they aren't awaiting review, they're the deliberate overflow bucket for claims categorization couldn't
resolve.

Note: `Report` rows are snapshots generated once at run completion (already true today for every
existing field), not live-rendered from current taxonomy state — if a category is renamed or approved
after a report was generated, that specific stored report keeps its original label/status until the run
is backfilled again. This is consistent with how `Report` has always behaved, not a new limitation
introduced here.

## Claims-based aggregation — remaining details

`ClaimType → Report section` mapping (preserves current report parity, adds two new gated sections):

| ClaimType | Report field | Gating |
|---|---|---|
| `PROBLEM` | `top_pain_points` | always (same as today's `pain_point`) |
| `FEATURE_REQUEST` | `feature_requests` | always |
| `PRAISE` | `praised_aspects` | always |
| `COMPARISON` | `competitor_mentions` | always |
| `SHIPPING_ISSUE` | `shipping_issues` (new) | support threshold, see below |
| `SELLER_SERVICE_ISSUE` | `seller_service_issues` (new) | support threshold |
| `QUESTION` | — (excluded) | matches today's gap — `QUESTION`/`NOISE` already never reach the report |
| `OBSERVATION` | — (excluded from ranking/default report) | stored/categorized but not surfaced unless a future phase maps it to a real insight |
| `NOISE` | — (excluded) | never a real extracted claim by design |

**Support threshold**: a `(claim_type, category)` group surfaces in `shipping_issues`/
`seller_service_issues` only if it clears all three configurable minimums (`SHIPPING_ISSUE_MIN_CLAIMS`/
`_MIN_EVIDENCE`/`_MIN_THREADS`-style env vars, small defaults, tunable after real-data validation — same
"flagged for tuning" convention as `_LOW_INFORMATION_MAX_CHARS` in `screening.py`). Independent-user
count is not a factor — no collector captures author identity today; recorded as a known v1 limitation.

**Thread count is best-effort and source-specific, not a universally reliable signal** (secondary
consideration, now called out explicitly): there is no stored thread identifier — `Evidence` carries
`source_url` but not `post_id`/`comment_id` (those exist on `CollectedItem`, `models.py:139-140`, but
stop there). A `thread_key` is derived from `source_url` only for `DataSource.REDDIT`, where permalinks
are known (from the finding-#5 measurement) to share a common path prefix per post. For every other
source (Amazon, YouTube, JSON upload) — where no such structural guarantee has been verified —
`thread_key` falls back to the claim's own `evidence_id`, meaning "distinct thread count" degenerates
to "distinct evidence count" for those sources rather than producing a fabricated or wrong number. This
is used only for the shipping/service threshold check, not for any ranking/dampening logic
(thread-concentration dampening, finding #5, stays deferred).

## Backward compatibility / migration strategy

- v1/v2/v3 runs: reports already generated stay exactly as they are (non-retroactive, same as every
  prior additive `Report` field).
- v3 runs with claims already stored but no report rebuilt (e.g. the existing `run_55025c50e81b`
  robot-vacuum validation run, 285 claims): can be backfilled via a one-off script calling
  `categorize_claims()` + the new `summarize()` path directly, same established pattern used for
  `run_dbd48cbae519`'s MacBook report backfill. This is also the natural first real-data validation
  target — no need to wait for a fresh live run to see categorization quality on real data.
- New tables + new `Report`/`claims` columns are purely additive.
- `RunRecord.pipeline_version` bumps to `"v4"` for new runs, informational only — the actual branch is
  driven by `_claims_report_eligible()` in `run_react_loop`, not a version string check inside
  `summarize()`.

## Tests

- `test_taxonomy.py` (new, mirrors `test_claims.py`/`test_screening.py`'s fake-LLM/dataclass-driven
  style): auto-match on high lexical similarity; ambiguous case routed to (stubbed) LLM and matched;
  **LLM call raising/timing out → `categorization_status="unresolved"`, no new category created**
  (the specific case the review flagged); genuine LLM "no match" → new `proposed` category created;
  matching against `proposed` (not just `approved`) categories; unique-index collision on propose-new
  handled by re-fetch-and-match, not a crash; bulk write only ever touches the exact `claim_id`s passed
  in (a fixture asserts an out-of-scope claim with a matching `aspect_raw` in a *different* run is
  untouched after `categorize_claims()` runs).
- `test_taxonomy.py` rerun-safety additions: default call (`force=False`) skips an already-`"resolved"`
  claim entirely (no LLM/lexical work attempted for it, `skipped_already_resolved` incremented);
  `force=True` reprocesses an already-resolved non-manual claim; a `method="manual"` claim is left
  untouched under `force=True` alone (`skipped_manual_protected` incremented); only
  `force=True, override_manual=True` together actually reprocess a manual claim;
  `storage.set_claims_categorization()` itself refuses to overwrite a manual row when called directly
  with `override_manual=False`, independent of whatever `categorize_claims()` did or didn't filter (a
  write-layer test, not just a selection-layer one); a simulated mid-batch DB failure on one aspect
  group's write leaves an earlier, already-committed aspect group's claims correctly categorized rather
  than rolling back the whole run's progress.
- `test_storage.py` additions: `canonical_categories`/`category_audit_log` CRUD round-trip
  (create/list/approve/merge/deprecate/rename), unique-label-per-product_category constraint enforced,
  migration idempotency (calling `migrate()` twice on an existing v3 DB doesn't error).
- `test_react_agent.py` additions: `_resolve_categories`/`_aggregate_claims_by_category`/
  `_build_report_inputs` each unit-tested in isolation with in-memory fixtures (no DB/LLM); alias
  resolution one-hop; deprecated-with-no-alias and never-categorized claims both land in the
  `"uncategorized"` bucket (not dropped, not split by `aspect_raw`); `category_status` present on every
  aggregate group including `"proposed"` ones; shipping/service sections stay empty below threshold and
  populate once cleared; non-Reddit source falls back to evidence-count-as-thread-count.
- `test_react_agent.py` (or a new `test_report_eligibility.py`) — `_claims_report_eligible()`, one test
  per reason: `enable_claims_based_report()` off → `"claims_report_disabled"`; categorization kill switch
  off (`cat_stats=None`) → `"categorization_disabled"`; `cat_stats.completed=False` →
  `"categorization_incomplete"`; `claims_total=0` → `"no_claims"`; resolved ratio below
  `claims_report_min_resolved_ratio()` → `"low_resolved_coverage:<ratio>"`; ratio at/above threshold →
  eligible, `fallback_reason is None`. `summarize()`'s `report_source`/`fallback_reason` land correctly
  on the returned `Report` in both branches.
- `test_taxonomy.py` additions for label generation: LLM-supplied `proposed_label` used when present and
  non-empty; empty/missing field or LLM-unavailable → deterministic fallback; fallback truncates a long
  `aspect_raw` at a word boundary, never mid-word, and never exceeds `_MAX_CANONICAL_LABEL_LENGTH`; an
  `aspect_raw` that normalizes to nothing falls back to `"uncategorized topic"` rather than an empty
  label.
- `test_routes.py` (new, or additions if a routes test module already exists — confirm during
  implementation): one test per curation endpoint's happy path, plus the rejection cases (approve-an-
  already-deprecated-category, merge-into-a-chained-alias, self-merge, merge-across-product_categories,
  act-on-an-alias-directly, rename-collision) each asserting the specific 409/422 and that no row
  changed as a side effect of the rejected call; `category_audit_log` gains exactly one row per
  successful transition, zero rows on a rejected one; `POST /claims/{id}/categorize` manual override
  sets `categorization_method="manual"` and is reflected in the next aggregation.
- Real-data validation (not pytest — real DeepSeek calls, matches how every prior phase was actually
  validated): backfill `run_55025c50e81b`'s 285 stored claims through the new categorization +
  aggregation path, manually inspect the resulting category list and report sections for the exact
  fragmentation case already on record (`floor_damage`/`floor damage` should collapse to one category,
  both starting as `proposed` and visibly marked as such), then a fresh live end-to-end run for
  full-pipeline confirmation.

## Implementation order

1. `canonical_categories` + `category_audit_log` tables, migration, unique-label index, `Storage` CRUD/
   transition methods (`create_category`, `get_category`, `list_categories`, `approve_category`,
   `merge_category`, `deprecate_category`, `rename_category`, each enforcing the validation rules above
   and writing to `category_audit_log`) — storage-layer unit tests only, nothing wired in yet.
2. `claims` table migration for the three provenance columns; `Storage.set_claims_categorization
   (claim_ids, ...)` (explicit-ID-only write primitive) and `list_claims_by_status(run_id, status)`.
3. `pipeline/taxonomy.py`: the matching engine (normalize → match-existing tiers → LLM arbitration →
   propose-new, with infra-failure vs. no-match kept distinct throughout, `completed` flag, and the
   layered `canonical_label` generation — LLM-assisted where already-available, deterministic fallback
   otherwise) + unit tests with a fake LLM, not yet called from anywhere live.
4. Wire `categorize_claims()` into `run_react_loop` right before the report-loading step; new
   `StepType.CATEGORIZATION`, `CategorizationStats`, trace event, kill switch, `pipeline_version` bump.
5. `_claims_report_eligible()` + `claims_report_min_resolved_ratio()` kill-switch-style config, wired
   into `run_react_loop` right after categorization; unit tests for every fallback-reason branch.
6. `_resolve_categories()`, `_aggregate_claims_by_category()`, `_build_report_inputs()` (and
   `_build_report_inputs_from_evidence()` wrapping the untouched legacy path) as separate, independently
   unit-tested functions; `summarize()` rewritten as the thin orchestrator above, taking the gate's
   `fallback_reason` and stamping `report_source`/`fallback_reason` onto every `Report`; `thread_key`
   derivation with the Reddit-only/evidence-id-fallback split.
7. `Report` model + `reports` table migration for `shipping_issues`/`seller_service_issues`/
   `report_source`/`fallback_reason`.
8. Human-curation routes: list/approve/merge/deprecate/rename/history + `POST /claims/{id}/categorize`,
   route-level tests (API-only — no curation admin UI this phase).
9. Frontend: extend `Report.tsx` with two more `AspectSection` instances (the component is already
   generic over category/groups/meta — no redesign) plus the "Pending review" badge on `"proposed"`
   groups (`BarRank`'s new optional `badge` prop), EN/ZH i18n keys. No change needed to `AspectGroup`'s
   shape (`api.ts`) beyond the new `category_status` field.
10. Real-data validation: backfill the existing robot-vacuum run first (fastest signal, no new collection
    needed), tune similarity/support/eligibility thresholds against it, then one fresh live run.

## Final-validation addendum (Stage 10 + architecture review, 2026-07-28)

Recorded here so a future session doesn't have to re-derive these from scratch. No code or threshold
behavior changed as a result of this addendum — architecture-review conclusions only.

- **The existing resolved ratio measures assignment coverage, not taxonomy curation quality.**
  `_resolved_ratio()` (`(claims_total - unresolved_failures) / claims_total`) counts a claim as
  "resolved" the moment it has any non-`unresolved` `categorization_status` — including a brand-new,
  unreviewed `proposed_new` category nobody has looked at yet. Real data from the robot-vacuum backfill
  confirmed this concretely: 285/285 claims assigned (`resolved_ratio = 1.0`), but only 42 of 214
  distinct aspects (3 lexical + 39 LLM) matched anything with prior standing — the other 171 became
  brand-new `proposed` categories. "Fully resolved" and "fully curated" are different claims about the
  same run; the current metric only asserts the former.
- **Eligibility intentionally stays based on assignment coverage, not curation quality.** This is not an
  oversight — gating `CLAIMS_REPORT_MIN_RESOLVED_RATIO` on approval quality instead would directly
  contradict the Revision 2 decision above ("proposed categories must not silently become stable report
  categories... keeping proposed categories IN the report... never hiding data"): a product line's
  first-ever run would become ineligible almost by default, exactly what that decision was written to
  avoid. The ratio's actual job is operational — did categorization run to completion without
  LLM/infra failures — and it does that job correctly.
- **`approved_ratio` / `proposed_ratio` / `uncategorized_ratio` are recommended future observability
  metrics, not yet implemented.** Recommended computation point: report-generation time, from
  `_resolve_categories()`'s actual output (Stage 6) — not from `CategorizationStats` — because taxonomy
  status can drift between when a claim was categorized and when a report is generated or later
  reviewed (a category approved today can be deprecated next week). Intended use: surfaced in the
  `CLAIMS_REPORT_ELIGIBILITY` trace payload and/or on `Report` itself for transparency, explicitly
  **not** as a second eligibility gate.
- **Reports are intentional point-in-time snapshots**, consistent with `report_source`/`fallback_reason`
  (Stage 7) and every other `Report` field: `category_status` is stamped into the report's JSON blob at
  generation time and never re-read live. Confirmed twice during Stage 10's own live curation testing:
  approving "floor damage" after its report was generated left the saved report's badge as "Pending
  review" (unchanged); deprecating "repair service" with no merge target created a claim that would now
  resolve to `uncategorized` on any future regeneration, but the already-saved report doesn't reflect
  it. The UI now carries a short note (`report.details.snapshotNote`) next to the report source label
  saying category statuses reflect the taxonomy state as of generation time. A `regenerate report`
  action (re-running `summarize()` from already-stored claims/categories — cheap, no new LLM/collection
  calls needed) is a reasonable near-term follow-up, not yet built;
  `backend/scripts/recategorize_and_regenerate_report.py` is the ad hoc maintenance-script version of
  this today.
- **Stage 10 did not complete a fresh, uninterrupted v4 Collector-to-Report run.** The one live attempt
  (`run_89b02b9b1e3e`, "robot vacuum cleaners" via Reddit) returned 0 evidence — both search iterations
  hit Reddit's `challenge_detected: true` wall, a pre-existing, out-of-Phase-3-scope Chrome-profile trust
  issue (the dedicated profile needs a one-time manual reddit.com visit to re-warm, which requires a
  human). What Stage 10 actually validated end-to-end, live, with real DeepSeek calls, is the *new*
  Phase 3 downstream pipeline — Categorization → Eligibility → Aggregation → Persistence → API →
  Taxonomy Curation → Frontend — run against `run_55025c50e81b`'s 285 claims / 155 evidence, genuinely
  collected via Reddit in a separate prior live run on 2026-07-21 (Collectors/Screening/Claim Extraction
  are unchanged by Phase 3 and were validated by that earlier run, not re-executed this session). Net:
  Phase 3 has been proven correct on real data in two separate live passes stitched together via stored
  data, not yet in one unbroken fresh run starting cold from a Collector invocation. Closing this gap
  (one real run, once the Reddit profile is re-warmed) is a recommended follow-up before treating Phase 3
  as fully production-validated end to end.
