from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.llm import DeepSeekClient
from app.models import Claim, ClaimType, Sentiment
from app.pipeline.taxonomy import (
    _DEFAULT_ARBITRATION_CONFIDENCE,
    _MAX_ARBITRATION_BATCH_SIZE,
    CategorizationStats,
    categorize_claims,
)
from app.storage import Storage


def make_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "test.sqlite3")
    storage.migrate()
    return storage


def make_claim(
    claim_id: str = "cl_1",
    run_id: str = "run_1",
    evidence_id: str = "ev_1",
    aspect_raw: str = "battery life",
    categorization_status: str | None = None,
    categorization_method: str | None = None,
    categorization_confidence: float | None = None,
) -> Claim:
    return Claim(
        claim_id=claim_id,
        run_id=run_id,
        evidence_id=evidence_id,
        claim_type=ClaimType.PROBLEM,
        aspect_raw=aspect_raw,
        statement="Battery drains quickly",
        sentiment=Sentiment.NEGATIVE,
        confidence=0.8,
        extraction_method="llm",
        categorization_status=categorization_status,
        categorization_method=categorization_method,
        categorization_confidence=categorization_confidence,
    )


def no_llm() -> DeepSeekClient:
    return DeepSeekClient(api_key="")


class FakeLLM:
    """Duck-types the DeepSeekClient interface used by pipeline/taxonomy.py."""

    def __init__(self, response: Any = None, available: bool = True, raise_exc: Exception | None = None):
        self._response = response
        self._available = available
        self._raise_exc = raise_exc
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def json_chat(self, model: str, system: str, user: str) -> Any:
        self.calls += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


class CountingFakeLLM:
    """Records how many items each call's prompt actually contained, to prove
    arbitration batches are bounded/chunked rather than one unbounded call."""

    def __init__(self) -> None:
        self.call_item_counts: list[int] = []

    def available(self) -> bool:
        return True

    def json_chat(self, model: str, system: str, user: str) -> Any:
        payload = json.loads(user)
        self.call_item_counts.append(len(payload["items"]))
        return {"verdicts": []}  # no verdicts needed -- this test only checks batching


# ---------------------------------------------------------------------------
# Default selection: only uncategorized/unresolved by default
# ---------------------------------------------------------------------------


def test_default_selection_only_processes_uncategorized_and_unresolved_claims(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    never_touched = make_claim(claim_id="cl_none", run_id=run.run_id, evidence_id="ev_a", categorization_status=None)
    previously_failed = make_claim(claim_id="cl_unresolved", run_id=run.run_id, evidence_id="ev_b", categorization_status="unresolved")
    already_resolved = make_claim(
        claim_id="cl_resolved", run_id=run.run_id, evidence_id="ev_c",
        categorization_status="resolved", categorization_method="lexical_match", categorization_confidence=0.9,
    )
    for c in (never_touched, previously_failed, already_resolved):
        storage.save_claim(c)

    stats = categorize_claims(run.run_id, "dog food", [never_touched, previously_failed, already_resolved], storage, no_llm())

    assert stats.skipped_already_resolved == 1
    assert stats.skipped_manual_protected == 0
    loaded = {c.claim_id: c for c in storage.list_claims(run.run_id)}
    assert loaded["cl_none"].categorization_status == "resolved"  # newly categorized
    assert loaded["cl_unresolved"].categorization_status == "resolved"  # retried, now categorized
    assert loaded["cl_resolved"].categorization_method == "lexical_match"  # untouched, exactly as it was


# ---------------------------------------------------------------------------
# force / override_manual combinations
# ---------------------------------------------------------------------------


def test_force_true_reclassifies_non_manual_resolved_claims(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    resolved = make_claim(
        run_id=run.run_id, categorization_status="resolved", categorization_method="lexical_match", categorization_confidence=0.5
    )
    storage.save_claim(resolved)

    stats = categorize_claims(run.run_id, "dog food", [resolved], storage, no_llm(), force=True)

    assert stats.skipped_already_resolved == 0  # force=True means it was NOT skipped
    assert stats.new_categories_proposed == 1  # actually reprocessed


def test_manual_claim_protected_by_default(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    manual = make_claim(run_id=run.run_id, categorization_status="resolved", categorization_method="manual", categorization_confidence=None)
    storage.save_claim(manual)

    stats = categorize_claims(run.run_id, "dog food", [manual], storage, no_llm())

    assert stats.skipped_manual_protected == 1
    assert storage.list_claims(run.run_id)[0].categorization_method == "manual"  # unchanged


def test_manual_claim_still_protected_under_force_alone(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    manual = make_claim(run_id=run.run_id, categorization_status="resolved", categorization_method="manual", categorization_confidence=None)
    storage.save_claim(manual)

    stats = categorize_claims(run.run_id, "dog food", [manual], storage, no_llm(), force=True)

    assert stats.skipped_manual_protected == 1
    assert storage.list_claims(run.run_id)[0].categorization_method == "manual"  # still unchanged


def test_manual_claim_reprocessed_only_with_force_and_override_manual_together(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    manual = make_claim(run_id=run.run_id, categorization_status="resolved", categorization_method="manual", categorization_confidence=None)
    storage.save_claim(manual)

    stats = categorize_claims(run.run_id, "dog food", [manual], storage, no_llm(), force=True, override_manual=True)

    assert stats.skipped_manual_protected == 0
    assert stats.new_categories_proposed == 1
    assert storage.list_claims(run.run_id)[0].categorization_method == "proposed_new"


def test_override_manual_alone_without_force_still_skips_a_resolved_manual_claim(tmp_path: Path) -> None:
    """Subtle ordering case: override_manual=True lets a manual claim past the
    manual-protection gate, but with force=False it is then still excluded by
    the "only uncategorized/unresolved" gate (a manual claim's status is
    always "resolved"). Net effect: still skipped, but for the
    skipped_already_resolved reason, not skipped_manual_protected -- the two
    gates are independent and evaluated in order."""
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    manual = make_claim(run_id=run.run_id, categorization_status="resolved", categorization_method="manual", categorization_confidence=None)
    storage.save_claim(manual)

    stats = categorize_claims(run.run_id, "dog food", [manual], storage, no_llm(), force=False, override_manual=True)

    assert stats.skipped_manual_protected == 0
    assert stats.skipped_already_resolved == 1
    assert storage.list_claims(run.run_id)[0].categorization_method == "manual"  # unchanged


# ---------------------------------------------------------------------------
# Lexical high-confidence matching
# ---------------------------------------------------------------------------


def test_high_lexical_similarity_auto_matches_without_an_llm_call(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    category = storage.create_category("dog food", "battery life", "battery life")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM()

    stats = categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert llm.calls == 0
    assert stats.lexical_matched == 1
    loaded = storage.list_claims(run.run_id)[0]
    assert loaded.canonical_category == category.category_id
    assert loaded.categorization_method == "lexical_match"
    assert loaded.categorization_confidence == 1.0


def test_underscore_and_space_variants_of_the_same_aspect_match_each_other(tmp_path: Path) -> None:
    """The exact real fragmentation case (floor_damage / floor damage) that
    motivated Phase 3 -- must resolve to the SAME category, not two."""
    storage = make_storage(tmp_path)
    run = storage.create_run("robot vacuums", [], [], 6, 25)
    category = storage.create_category("robot vacuums", "floor damage", "floor_damage")
    claim = make_claim(aspect_raw="floor_damage", run_id=run.run_id)
    storage.save_claim(claim)

    stats = categorize_claims(run.run_id, "robot vacuums", [claim], storage, no_llm())

    assert stats.lexical_matched == 1
    assert storage.list_claims(run.run_id)[0].canonical_category == category.category_id


# ---------------------------------------------------------------------------
# Batched LLM arbitration for ambiguous matches
# ---------------------------------------------------------------------------


def test_ambiguous_aspect_is_batched_to_a_single_llm_call(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    claim_a = make_claim(claim_id="cl_a", run_id=run.run_id, evidence_id="ev_a", aspect_raw="battery life")
    claim_b = make_claim(claim_id="cl_b", run_id=run.run_id, evidence_id="ev_b", aspect_raw="handle comfort")
    storage.save_claim(claim_a)
    storage.save_claim(claim_b)
    llm = FakeLLM(
        response={
            "verdicts": [
                {"aspect_index": 0, "same_topic": True, "confidence": 0.8},
            ]
        }
    )

    # claim_b's aspect ("handle comfort") has zero lexical overlap with the only
    # candidate -- it resolves via Tier 1 directly (propose_new), so only
    # claim_a's aspect is genuinely ambiguous and goes to the LLM tier.
    categorize_claims(run.run_id, "dog food", [claim_a, claim_b], storage, llm)

    assert llm.calls == 1  # one batched call, not one per ambiguous aspect


def test_llm_confirms_a_match_for_an_ambiguous_aspect(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    category = storage.create_category("dog food", "battery performance", "battery performance")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(response={"verdicts": [{"aspect_index": 0, "same_topic": True, "confidence": 0.85}]})

    stats = categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert stats.llm_matched == 1
    loaded = storage.list_claims(run.run_id)[0]
    assert loaded.canonical_category == category.category_id
    assert loaded.categorization_method == "llm_match"
    assert loaded.categorization_confidence == 0.85


# ---------------------------------------------------------------------------
# Genuine no-match creates a proposed category
# ---------------------------------------------------------------------------


def test_llm_confirms_no_match_creates_a_proposed_category_with_llm_label(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery type", "battery type")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(
        response={"verdicts": [{"aspect_index": 0, "same_topic": False, "confidence": 0.7, "proposed_label": "Battery Duration"}]}
    )

    stats = categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert stats.new_categories_proposed == 1
    loaded = storage.list_claims(run.run_id)[0]
    assert loaded.categorization_status == "resolved"
    assert loaded.categorization_method == "proposed_new"
    categories = storage.list_categories("dog food")
    new_category = next(c for c in categories if c.category_id == loaded.canonical_category)
    assert new_category.canonical_label == "Battery Duration"
    assert new_category.status.value == "proposed"


def test_zero_candidates_proposes_a_new_category_without_any_llm_call(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM()

    stats = categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert llm.calls == 0
    assert stats.new_categories_proposed == 1
    categories = storage.list_categories("dog food")
    assert categories[0].canonical_label == "battery life"  # deterministic fallback label


# ---------------------------------------------------------------------------
# Infrastructure / malformed-response failure -> unresolved, no category
# ---------------------------------------------------------------------------


def test_llm_call_raising_leaves_the_claim_unresolved_and_creates_no_category(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(raise_exc=RuntimeError("simulated network failure"))

    stats = categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert stats.unresolved_failures == 1
    assert stats.new_categories_proposed == 0
    assert stats.completed is True  # an LLM failure is expected/handled, not a batch abort
    loaded = storage.list_claims(run.run_id)[0]
    assert loaded.categorization_status == "unresolved"
    assert loaded.canonical_category is None
    assert len(storage.list_categories("dog food")) == 1  # only the pre-existing one -- nothing new


def test_malformed_top_level_response_leaves_the_claim_unresolved(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(response={"not_verdicts_at_all": []})  # missing the required "verdicts" key

    stats = categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert stats.unresolved_failures == 1
    assert storage.list_claims(run.run_id)[0].categorization_status == "unresolved"


def test_partial_response_leaves_only_the_missing_item_unresolved(tmp_path: Path) -> None:
    """A well-formed response that's simply missing a verdict for one aspect
    must not fail the whole batch -- the other items with real verdicts still
    resolve normally."""
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    storage.create_category("dog food", "packaging style", "packaging style")
    claim_a = make_claim(claim_id="cl_a", run_id=run.run_id, evidence_id="ev_a", aspect_raw="battery life")
    claim_b = make_claim(claim_id="cl_b", run_id=run.run_id, evidence_id="ev_b", aspect_raw="packaging design")
    storage.save_claim(claim_a)
    storage.save_claim(claim_b)
    # Only aspect_index 0 gets a verdict; aspect_index 1 is silently missing.
    llm = FakeLLM(response={"verdicts": [{"aspect_index": 0, "same_topic": True, "confidence": 0.8}]})

    categorize_claims(run.run_id, "dog food", [claim_a, claim_b], storage, llm)

    loaded = {c.claim_id: c for c in storage.list_claims(run.run_id)}
    assert loaded["cl_a"].categorization_status == "resolved"
    assert loaded["cl_b"].categorization_status == "unresolved"


def test_ambiguous_aspect_with_no_llm_available_is_left_unresolved(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)

    stats = categorize_claims(run.run_id, "dog food", [claim], storage, no_llm())

    assert stats.unresolved_failures == 1
    assert storage.list_claims(run.run_id)[0].categorization_status == "unresolved"


# ---------------------------------------------------------------------------
# Proposed-label generation: LLM result vs. deterministic fallback
# ---------------------------------------------------------------------------


def test_deterministic_fallback_used_when_llm_omits_the_proposed_label(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery type", "battery type")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(response={"verdicts": [{"aspect_index": 0, "same_topic": False, "confidence": 0.6}]})  # no proposed_label

    categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    categories = storage.list_categories("dog food")
    new_category = next(c for c in categories if c.first_seen_aspect_raw == "battery life")
    assert new_category.canonical_label == "battery life"  # deterministic fallback, not blank


def test_fallback_label_truncates_a_long_aspect_at_a_word_boundary(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    long_aspect = "the customer kept mentioning how the charging cable that comes in the box feels flimsy and cheap"
    claim = make_claim(aspect_raw=long_aspect, run_id=run.run_id)
    storage.save_claim(claim)

    categorize_claims(run.run_id, "dog food", [claim], storage, no_llm())

    [category] = storage.list_categories("dog food")
    assert len(category.canonical_label) <= 60
    assert not category.canonical_label.endswith(" ")
    assert long_aspect.startswith(category.canonical_label.rsplit(" ", 1)[0])  # cut cleanly, not mid-word


def test_fallback_label_for_an_aspect_that_normalizes_to_nothing(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    claim = make_claim(aspect_raw="___---___", run_id=run.run_id)
    storage.save_claim(claim)

    categorize_claims(run.run_id, "dog food", [claim], storage, no_llm())

    [category] = storage.list_categories("dog food")
    assert category.canonical_label == "uncategorized topic"


# ---------------------------------------------------------------------------
# Writes scoped to explicit claim IDs; stats accuracy; completed flag
# ---------------------------------------------------------------------------


def test_only_the_claims_passed_in_are_ever_touched(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    included = make_claim(claim_id="cl_included", run_id=run.run_id, evidence_id="ev_a", aspect_raw="battery life")
    excluded = make_claim(claim_id="cl_excluded", run_id=run.run_id, evidence_id="ev_b", aspect_raw="battery life")
    storage.save_claim(included)
    storage.save_claim(excluded)

    # Only `included` is handed to categorize_claims -- `excluded` shares the
    # exact same run and aspect_raw, but must never be written to, since the
    # write path is scoped by explicit claim_id, never by aspect_raw.
    categorize_claims(run.run_id, "dog food", [included], storage, no_llm())

    loaded = {c.claim_id: c for c in storage.list_claims(run.run_id)}
    assert loaded["cl_included"].categorization_status == "resolved"
    assert loaded["cl_excluded"].categorization_status is None


def test_run_id_mismatch_raises_immediately_without_writing_anything(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    other_run = storage.create_run("cat food", [], [], 6, 25)
    wrong_run_claim = make_claim(run_id=other_run.run_id, evidence_id="ev_a")
    storage.save_claim(wrong_run_claim)

    with pytest.raises(ValueError):
        categorize_claims(run.run_id, "dog food", [wrong_run_claim], storage, no_llm())

    assert storage.list_claims(other_run.run_id)[0].categorization_status is None  # nothing written


def test_categorization_stats_counts_are_accurate(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery life", "battery life")
    lexical_match = make_claim(claim_id="cl_lex", run_id=run.run_id, evidence_id="ev_a", aspect_raw="battery life")
    new_category = make_claim(claim_id="cl_new", run_id=run.run_id, evidence_id="ev_b", aspect_raw="chew durability")
    manual = make_claim(
        claim_id="cl_manual", run_id=run.run_id, evidence_id="ev_c", aspect_raw="packaging",
        categorization_status="resolved", categorization_method="manual",
    )
    already_resolved = make_claim(
        claim_id="cl_done", run_id=run.run_id, evidence_id="ev_d", aspect_raw="smell",
        categorization_status="resolved", categorization_method="lexical_match", categorization_confidence=0.9,
    )
    all_claims = [lexical_match, new_category, manual, already_resolved]
    for c in all_claims:
        storage.save_claim(c)

    stats = categorize_claims(run.run_id, "dog food", all_claims, storage, no_llm())

    assert stats.claims_total == 4
    assert stats.skipped_manual_protected == 1
    assert stats.skipped_already_resolved == 1
    assert stats.distinct_aspects == 2  # only the 2 selected claims' aspects
    assert stats.lexical_matched == 1
    assert stats.new_categories_proposed == 1
    assert stats.unresolved_failures == 0
    assert stats.completed is True


def test_unexpected_db_failure_mid_batch_sets_completed_false_and_preserves_earlier_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    category_a = storage.create_category("dog food", "battery life", "battery life")
    storage.create_category("dog food", "customer service", "customer service")
    claim_a = make_claim(claim_id="cl_a", run_id=run.run_id, evidence_id="ev_a", aspect_raw="battery life")
    claim_b = make_claim(claim_id="cl_b", run_id=run.run_id, evidence_id="ev_b", aspect_raw="customer service")
    storage.save_claim(claim_a)
    storage.save_claim(claim_b)

    original = storage.set_claims_categorization
    call_count = {"n": 0}

    def _flaky_after_first_call(*args: object, **kwargs: object) -> int:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original(*args, **kwargs)  # type: ignore[arg-type]
        raise sqlite3.OperationalError("simulated mid-batch DB failure")

    monkeypatch.setattr(storage, "set_claims_categorization", _flaky_after_first_call)

    stats = categorize_claims(run.run_id, "dog food", [claim_a, claim_b], storage, no_llm())

    assert stats.completed is False
    assert stats.error is not None and "OperationalError" in stats.error  # traceable, not swallowed
    assert stats.lexical_matched == 1  # the first group's success is still reflected in partial stats
    loaded = {c.claim_id: c for c in storage.list_claims(run.run_id)}
    assert loaded["cl_a"].categorization_status == "resolved"  # first group's write committed, preserved
    assert loaded["cl_a"].canonical_category == category_a.category_id
    assert loaded["cl_b"].categorization_status is None  # second group's write never landed


# ---------------------------------------------------------------------------
# Bounded arbitration batch size
# ---------------------------------------------------------------------------


def test_arbitration_calls_are_bounded_and_chunked(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    n = _MAX_ARBITRATION_BATCH_SIZE + 5
    claims = [
        make_claim(claim_id=f"cl_{i}", run_id=run.run_id, evidence_id=f"ev_{i}", aspect_raw=f"battery topic{i:03d}")
        for i in range(n)
    ]
    for c in claims:
        storage.save_claim(c)
    llm = CountingFakeLLM()

    categorize_claims(run.run_id, "dog food", claims, storage, llm)

    assert len(llm.call_item_counts) == 2  # n items, cap _MAX_ARBITRATION_BATCH_SIZE -> 2 calls
    assert llm.call_item_counts[0] == _MAX_ARBITRATION_BATCH_SIZE
    assert llm.call_item_counts[1] == 5
    assert max(llm.call_item_counts) <= _MAX_ARBITRATION_BATCH_SIZE


# ---------------------------------------------------------------------------
# In-memory candidate set grows within one run
# ---------------------------------------------------------------------------


def test_a_category_proposed_for_an_earlier_aspect_is_matched_by_a_later_one_in_the_same_run(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    # "handle grip quality" (3 keywords) vs "handle grip quality issue" (4
    # keywords, 3 shared) scores exactly 3/4 = 0.75 -- at the auto-match bar,
    # so the second is resolved via Tier 1 lexical matching alone, no LLM
    # needed, proving Phase A's own sequential candidate growth (not just
    # Tier 2's dedup-by-label safety net).
    first = make_claim(claim_id="cl_first", run_id=run.run_id, evidence_id="ev_a", aspect_raw="handle grip quality")
    second = make_claim(claim_id="cl_second", run_id=run.run_id, evidence_id="ev_b", aspect_raw="handle grip quality issue")
    storage.save_claim(first)
    storage.save_claim(second)
    llm = FakeLLM()

    stats = categorize_claims(run.run_id, "dog food", [first, second], storage, llm, force=True)

    assert llm.calls == 0  # resolved entirely by Tier 1, no arbitration needed
    categories = storage.list_categories("dog food")
    assert len(categories) == 1  # not two near-duplicate proposals
    assert stats.new_categories_proposed == 1
    assert stats.lexical_matched == 1
    loaded = {c.claim_id: c for c in storage.list_claims(run.run_id)}
    assert loaded["cl_first"].canonical_category == categories[0].category_id
    assert loaded["cl_second"].canonical_category == categories[0].category_id


# ---------------------------------------------------------------------------
# LLM-reported confidence: clamped/validated, explicit policy for bad values
# ---------------------------------------------------------------------------


def test_llm_confidence_above_one_is_clamped(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(response={"verdicts": [{"aspect_index": 0, "same_topic": True, "confidence": 1.7}]})

    categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert storage.list_claims(run.run_id)[0].categorization_confidence == 1.0


def test_llm_confidence_below_zero_is_clamped(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(response={"verdicts": [{"aspect_index": 0, "same_topic": True, "confidence": -0.4}]})

    categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert storage.list_claims(run.run_id)[0].categorization_confidence == 0.0


def test_llm_confidence_nan_falls_back_to_the_explicit_default(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(response={"verdicts": [{"aspect_index": 0, "same_topic": True, "confidence": float("nan")}]})

    categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert storage.list_claims(run.run_id)[0].categorization_confidence == _DEFAULT_ARBITRATION_CONFIDENCE


def test_llm_confidence_infinity_falls_back_to_the_explicit_default(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(response={"verdicts": [{"aspect_index": 0, "same_topic": True, "confidence": float("inf")}]})

    categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert storage.list_claims(run.run_id)[0].categorization_confidence == _DEFAULT_ARBITRATION_CONFIDENCE


def test_llm_confidence_missing_uses_the_documented_default(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run = storage.create_run("dog food", [], [], 6, 25)
    storage.create_category("dog food", "battery performance", "battery performance")
    claim = make_claim(aspect_raw="battery life", run_id=run.run_id)
    storage.save_claim(claim)
    llm = FakeLLM(response={"verdicts": [{"aspect_index": 0, "same_topic": True}]})  # confidence field omitted entirely

    categorize_claims(run.run_id, "dog food", [claim], storage, llm)

    assert storage.list_claims(run.run_id)[0].categorization_confidence == _DEFAULT_ARBITRATION_CONFIDENCE


def test_categorization_stats_is_a_fresh_dataclass_instance() -> None:
    a = CategorizationStats()
    b = CategorizationStats()
    a.claims_total = 5
    assert b.claims_total == 0  # no shared mutable default across instances
