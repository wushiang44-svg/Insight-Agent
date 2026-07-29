from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from ..llm import DeepSeekClient, fast_model, load_dotenv
from ..models import CanonicalCategory, CategoryStatus, Claim
from ..storage import Storage
from ..text import simple_similarity
from .claims import _normalize

# Report-safe cap on a canonical_label's length -- "not a blind copy of
# aspect_raw" (see _fallback_label / the LLM proposed_label path below).
_MAX_CANONICAL_LABEL_LENGTH = 60

# Starting heuristics, not yet validated on real proposed-category volume --
# flagged for tuning the same way screening.py's _LOW_INFORMATION_MAX_CHARS is.
# Aspect labels are short (2-4 word) phrases, so raw keyword-overlap ratios run
# lower/noisier than claims.py's full-sentence claim-merge thresholds; the
# candidate bar is set lower than claims.py's 0.5 for that reason.
_LEXICAL_AUTO_MATCH_SIM = 0.75  # >= this -> auto-match, no LLM call
_LEXICAL_CANDIDATE_SIM = 0.3  # >= this (but below auto-match) -> ambiguous, ask LLM; below -> propose new

# Bounds one arbitration prompt's size (same purpose as claims.py's
# _RAW_INGESTION_CEILING = 40) -- a run with many distinct ambiguous aspects
# makes multiple sequential batched calls instead of one unbounded prompt.
_MAX_ARBITRATION_BATCH_SIZE = 40

# Explicit policy for an LLM-reported confidence that's missing or invalid
# (non-numeric, NaN, +/-Infinity): fall back to this same neutral default the
# Pydantic model already uses for a genuinely missing field, rather than
# propagate a value that could corrupt downstream reports/rankings.
_DEFAULT_ARBITRATION_CONFIDENCE = 0.7


def enable_claim_categorization() -> bool:
    """Kill switch for the whole Phase 3 categorization batch step. Checked by
    the caller (run_react_loop's per-run wiring, or a maintenance script) --
    categorize_claims() itself does not re-check this, matching the existing
    convention (enable_claim_extraction()/enable_screening_v2() are checked at
    their call sites too, never inside the pipeline function itself)."""
    load_dotenv()
    raw = os.environ.get("ENABLE_CLAIM_CATEGORIZATION", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


@dataclass
class CategorizationStats:
    claims_total: int = 0
    distinct_aspects: int = 0
    lexical_matched: int = 0
    llm_matched: int = 0
    new_categories_proposed: int = 0
    unresolved_failures: int = 0
    skipped_already_resolved: int = 0
    skipped_manual_protected: int = 0
    # True only if the batch step ran through the entire selected list without
    # an unhandled exception escaping it (a DB error mid-loop, a bug -- NOT a
    # per-claim/per-aspect LLM failure, which is already caught internally and
    # counted in unresolved_failures instead). This is the "categorization
    # completed successfully" signal the Claims-report eligibility gate reads;
    # a distinct question from "how many claims actually resolved". Every
    # counter above reflects exactly what was actually written before any
    # failure -- writes happen inline, per aspect, as each is resolved, never
    # batched up and only recorded at the end, so completed=False never hides
    # or invalidates the partial counts.
    completed: bool = False
    # Populated only when completed=False -- a short, traceable description of
    # the unexpected failure (f"{type(exc).__name__}: {exc}"), so a caller
    # doesn't just see "something went wrong" with no way to diagnose it.
    error: str | None = None


def _normalize_for_matching(text: str) -> str:
    """Like claims.py's _normalize(), but also folds underscores/hyphens to
    spaces before collapsing whitespace. Deliberately NOT the same transform
    as _normalize() (claim-merge dedup has no reason to care about
    underscore-vs-space) -- this is specifically what makes "floor_damage" and
    "floor damage" group and match identically, which is the exact real
    fragmentation case (see the Reddit Browser Collector validation finding:
    "floor_damage"/"floor damage" as two separate top-5 pain points) this
    phase exists to fix. Plain _normalize() alone does not fold underscores,
    and text.py's simple_similarity tokenizes "floor_damage" as one glued
    token via its own regex -- without this, the two strings would score zero
    lexical similarity against each other."""
    return _normalize(text.replace("_", " ").replace("-", " "))


def _sanitize_confidence(value: object, default: float = _DEFAULT_ARBITRATION_CONFIDENCE) -> float:
    """Explicit policy for an LLM-reported confidence: clamp a valid finite
    number into [0, 1]; fall back to `default` for anything else (NaN,
    +/-Infinity, or a non-numeric value that somehow survived Pydantic
    validation) rather than let min()/max()'s undefined behavior on NaN
    silently produce a bad value."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return default
    return round(max(0.0, min(float(value), 1.0)), 2)


def _select_claims_to_process(
    claims: list[Claim], force: bool, override_manual: bool
) -> tuple[list[Claim], int, int]:
    """Default: only uncategorized/unresolved claims. force=True widens to
    every claim, but never a categorization_method='manual' one unless
    override_manual=True too -- two independent flags, checked in this order,
    so a manual claim is never swept up by force alone."""
    selected: list[Claim] = []
    skipped_already_resolved = 0
    skipped_manual_protected = 0
    for claim in claims:
        if claim.categorization_method == "manual" and not override_manual:
            skipped_manual_protected += 1
            continue
        if not force and claim.categorization_status not in (None, "unresolved"):
            skipped_already_resolved += 1
            continue
        selected.append(claim)
    return selected, skipped_already_resolved, skipped_manual_protected


def _fallback_label(aspect_raw: str) -> str:
    """Deterministic canonical_label fallback -- used when the LLM tier wasn't
    reached at all (zero candidates, so no arbitration call happens), the LLM
    omitted/emptied proposed_label, or the batch failed outright before
    reaching this aspect. Never a blind copy of aspect_raw: normalizes case/
    whitespace/underscores, truncates to _MAX_CANONICAL_LABEL_LENGTH at a word
    boundary (never mid-word), and guards against an empty result."""
    cleaned = _normalize_for_matching(aspect_raw)
    if len(cleaned) > _MAX_CANONICAL_LABEL_LENGTH:
        truncated = cleaned[:_MAX_CANONICAL_LABEL_LENGTH]
        cleaned = truncated.rsplit(" ", 1)[0] if " " in truncated else truncated
    return cleaned or "uncategorized topic"


def _best_lexical_match(aspect_raw: str, candidates: list[CanonicalCategory]) -> tuple[CanonicalCategory | None, float]:
    if not candidates:
        return None, 0.0
    normalized_aspect = _normalize_for_matching(aspect_raw)
    scored = [(c, simple_similarity(normalized_aspect, _normalize_for_matching(c.canonical_label))) for c in candidates]
    return max(scored, key=lambda pair: pair[1])


# ---------------------------------------------------------------------------
# Tier 2 -- batched LLM arbitration for lexically-ambiguous aspects only.
# One call handles at most _MAX_ARBITRATION_BATCH_SIZE items -- the caller
# (_categorize_selected_claims) is responsible for chunking a larger set into
# several such calls, so this function's own contract (and its tests) stay
# simple: exactly one call, whatever size items it's given.
# ---------------------------------------------------------------------------


class _ArbitrationVerdict(BaseModel):
    aspect_index: int
    same_topic: bool
    confidence: float = _DEFAULT_ARBITRATION_CONFIDENCE
    proposed_label: str | None = None


class _ArbitrationResponse(BaseModel):
    verdicts: list[dict]


def _arbitrate_aspects_llm(
    items: list[tuple[str, CanonicalCategory]], llm: DeepSeekClient
) -> list[tuple[bool, float, str | None] | None]:
    """One LLM call for every lexically-ambiguous (aspect_raw, nearest
    existing category) pair in `items`. Returns one entry per item, in order:
    a (same_topic, confidence, proposed_label) tuple on a genuine answer, or
    None if no usable verdict was obtained for that specific item.

    None covers BOTH "the whole call failed" (network/timeout/malformed
    top-level JSON) and "this one aspect_index was missing or malformed in an
    otherwise-valid response" -- either way, the caller must treat None as
    unresolved/retryable, never as a confirmed no-match, and never as license
    to propose a new category. A missing single verdict does not reject the
    whole batch (mirrors claims.py's "skip only this one malformed claim"
    philosophy), but it also must not be silently defaulted to a match or a
    no-match -- it genuinely has no answer. `confidence` is always sanitized
    into [0, 1] before being returned -- see _sanitize_confidence.
    """
    if not items:
        return []
    system = (
        "You are checking whether a customer-feedback topic label (aspect) is the SAME underlying "
        "topic as an existing canonical category in a product taxonomy, or a genuinely DIFFERENT topic "
        "that deserves its own new category. Two topics are the SAME only if a reader would consider "
        "them one concept referred to differently (e.g. \"floor damage\" and \"scratches on hardwood "
        "floors\" are the same topic; \"battery life\" and \"charging speed\" are DIFFERENT topics, even "
        "though both relate to the battery). Default to DIFFERENT when genuinely uncertain -- a missed "
        "match just leaves a near-duplicate category sitting unreviewed, but a wrong match silently "
        "mixes two distinct customer signals into one bucket. When you answer DIFFERENT, also suggest a "
        "short (a few words), report-safe proposed_label for the new category -- concise, not a "
        "restatement of the whole aspect text. Answer every item. Return only JSON."
    )
    user = json.dumps(
        {
            "items": [
                {"aspect_index": i, "aspect_raw": aspect_raw, "existing_category_label": candidate.canonical_label}
                for i, (aspect_raw, candidate) in enumerate(items)
            ],
            "expected_json": {
                "verdicts": [
                    {
                        "aspect_index": 0,
                        "same_topic": True,
                        "confidence": "0 to 1, how confident you are in this verdict",
                        "proposed_label": "only meaningful when same_topic is false -- a short new category label",
                    }
                ]
            },
        },
        ensure_ascii=False,
    )
    try:
        parsed = llm.json_chat(fast_model(), system, user)
        top = _ArbitrationResponse.model_validate(parsed)
    except Exception:
        return [None] * len(items)

    verdict_by_index: dict[int, tuple[bool, float, str | None]] = {}
    for raw in top.verdicts:
        try:
            v = _ArbitrationVerdict.model_validate(raw)
        except ValidationError:
            continue  # skip only this one malformed verdict, never reject the whole batch
        label = (v.proposed_label or "").strip() or None
        verdict_by_index[v.aspect_index] = (v.same_topic, _sanitize_confidence(v.confidence), label)
    return [verdict_by_index.get(i) for i in range(len(items))]


def _propose_or_reuse_category(
    product_category: str,
    label: str,
    first_seen_aspect_raw: str,
    claim_ids: list[str],
    known_by_label: dict[str, CanonicalCategory],
    storage: Storage,
    override_manual: bool,
    stats: CategorizationStats,
) -> CanonicalCategory:
    """Creates a new proposed category for `label`, or reuses one already in
    `known_by_label` -- either because it existed in the DB before this
    categorization pass started, or because an EARLIER aspect in this SAME
    pass already created it. `known_by_label` is mutated in place (keyed by
    _normalize_for_matching, not storage's own differently-normalized
    normalized_label column, so the two stay internally consistent) so the
    caller's subsequently-processed aspects see the addition immediately --
    this is what lets a later aspect in the same run match a category a
    prior aspect just proposed, instead of creating an avoidable
    near-duplicate. Only increments new_categories_proposed on a genuine new
    creation, never on reuse."""
    normalized_label = _normalize_for_matching(label)
    existing = known_by_label.get(normalized_label)
    if existing is not None:
        storage.set_claims_categorization(
            claim_ids,
            canonical_category=existing.category_id,
            status="resolved",
            method="proposed_new",
            confidence=None,
            override_manual=override_manual,
        )
        return existing
    category, _rows = storage.create_category_and_categorize_claims(
        product_category, label, first_seen_aspect_raw, claim_ids, override_manual=override_manual
    )
    known_by_label[_normalize_for_matching(category.canonical_label)] = category
    stats.new_categories_proposed += 1
    return category


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def categorize_claims(
    run_id: str,
    product_category: str,
    claims: list[Claim],
    storage: Storage,
    llm: DeepSeekClient,
    *,
    force: bool = False,
    override_manual: bool = False,
) -> CategorizationStats:
    """Categorizes zero or more Claims against product_category's canonical
    taxonomy -- a separate, post-extraction batch step over *all* claims
    handed to it (never per-item, never bolted onto extract_claims()).
    aspect_raw is never modified; only Claim.canonical_category and its
    categorization_* provenance fields are written, and only via
    storage.set_claims_categorization()/create_category_and_categorize_claims()
    -- both scoped to explicit claim_ids, never to aspect_raw.

    Never raises for an expected operational failure (an individual LLM call
    erroring) -- see CategorizationStats.completed's docstring for what does
    propagate. Does raise immediately, before any writes, if any claim in
    `claims` does not belong to `run_id` -- a caller-contract violation, not a
    retryable runtime condition.
    """
    for claim in claims:
        if claim.run_id != run_id:
            raise ValueError(
                f"categorize_claims received claim {claim.claim_id} from run {claim.run_id}, "
                f"expected only claims belonging to run {run_id}"
            )

    stats = CategorizationStats(claims_total=len(claims))
    selected, stats.skipped_already_resolved, stats.skipped_manual_protected = _select_claims_to_process(
        claims, force, override_manual
    )
    stats.distinct_aspects = len({_normalize_for_matching(c.aspect_raw) for c in selected})

    try:
        _categorize_selected_claims(product_category, selected, storage, llm, override_manual, stats)
        stats.completed = True
    except Exception as exc:
        stats.completed = False
        stats.error = f"{type(exc).__name__}: {exc}"
    return stats


def _categorize_selected_claims(
    product_category: str,
    selected: list[Claim],
    storage: Storage,
    llm: DeepSeekClient,
    override_manual: bool,
    stats: CategorizationStats,
) -> None:
    if not selected:
        return

    groups: dict[str, list[Claim]] = {}
    representative_aspect_raw: dict[str, str] = {}
    for claim in selected:
        key = _normalize_for_matching(claim.aspect_raw)
        groups.setdefault(key, []).append(claim)
        representative_aspect_raw.setdefault(key, claim.aspect_raw)

    working_candidates: list[CanonicalCategory] = [
        c for c in storage.list_categories(product_category) if c.status != CategoryStatus.DEPRECATED
    ]
    known_by_label: dict[str, CanonicalCategory] = {
        _normalize_for_matching(c.canonical_label): c for c in working_candidates
    }

    # --- Phase A: sequential ------------------------------------------------
    # Resolves every aspect that doesn't need the LLM at all (auto-match, or
    # no viable candidate whatsoever), writing immediately as each is decided
    # -- not deferred to a later "apply" pass. A category proposed for an
    # EARLIER aspect here becomes part of working_candidates/known_by_label
    # right away, so a LATER aspect's own Tier-1 decision in this same loop
    # can match it instead of independently proposing a near-duplicate.
    ambiguous_keys: list[str] = []
    ambiguous_candidates: dict[str, CanonicalCategory] = {}
    for key, aspect_raw in representative_aspect_raw.items():
        claim_ids = [c.claim_id for c in groups[key]]
        best_candidate, best_sim = _best_lexical_match(aspect_raw, working_candidates)
        if best_candidate is not None and best_sim >= _LEXICAL_AUTO_MATCH_SIM:
            storage.set_claims_categorization(
                claim_ids,
                canonical_category=best_candidate.category_id,
                status="resolved",
                method="lexical_match",
                confidence=round(best_sim, 2),
                override_manual=override_manual,
            )
            # Incremented only after the write succeeds -- if it raises, this
            # aspect's outcome must not be counted as if it had landed, so
            # CategorizationStats stays accurate for whatever completed=False
            # leaves partially done.
            stats.lexical_matched += 1
        elif best_candidate is not None and best_sim >= _LEXICAL_CANDIDATE_SIM:
            ambiguous_keys.append(key)
            ambiguous_candidates[key] = best_candidate
        else:
            category = _propose_or_reuse_category(
                product_category, _fallback_label(aspect_raw), aspect_raw, claim_ids,
                known_by_label, storage, override_manual, stats,
            )
            working_candidates.append(category)

    # --- Phase B: batched, bounded-size LLM arbitration ---------------------
    if not ambiguous_keys:
        return

    resolved_verdicts: dict[str, tuple[bool, float, str | None] | None] = {}
    if llm.available():
        for start in range(0, len(ambiguous_keys), _MAX_ARBITRATION_BATCH_SIZE):
            batch_keys = ambiguous_keys[start : start + _MAX_ARBITRATION_BATCH_SIZE]
            items = [(representative_aspect_raw[k], ambiguous_candidates[k]) for k in batch_keys]
            batch_verdicts = _arbitrate_aspects_llm(items, llm)
            for k, v in zip(batch_keys, batch_verdicts):
                resolved_verdicts[k] = v
    else:
        # No LLM configured -- an ambiguous aspect can't be arbitrated at all.
        # Leave it unresolved (retryable) rather than guessing either way;
        # never silently propose a new category just because the LLM tier
        # wasn't reachable.
        resolved_verdicts = {k: None for k in ambiguous_keys}

    for key in ambiguous_keys:
        verdict = resolved_verdicts[key]
        claim_ids = [c.claim_id for c in groups[key]]
        aspect_raw = representative_aspect_raw[key]

        if verdict is None:
            storage.set_claims_categorization(
                claim_ids,
                canonical_category=None,
                status="unresolved",
                method=None,
                confidence=None,
                override_manual=override_manual,
            )
            stats.unresolved_failures += len(claim_ids)  # after the write succeeds -- see the Phase A comment above
            continue

        same_topic, confidence, proposed_label = verdict
        if same_topic:
            storage.set_claims_categorization(
                claim_ids,
                canonical_category=ambiguous_candidates[key].category_id,
                status="resolved",
                method="llm_match",
                confidence=confidence,
                override_manual=override_manual,
            )
            stats.llm_matched += 1
        else:
            label = proposed_label or _fallback_label(aspect_raw)
            category = _propose_or_reuse_category(
                product_category, label, aspect_raw, claim_ids, known_by_label, storage, override_manual, stats
            )
            working_candidates.append(category)
