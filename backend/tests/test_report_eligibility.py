from __future__ import annotations

import pytest

from app.pipeline.taxonomy import CategorizationStats
from app.react_agent import (
    _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO,
    _claims_report_eligible,
    _resolved_ratio,
    claims_report_min_resolved_ratio,
    enable_claims_based_report,
)


class ExplodingStorage:
    """A Storage stand-in that raises on ANY attribute access -- proves
    _claims_report_eligible truly never re-queries the database, reading
    only the CategorizationStats it was handed."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"_claims_report_eligible must not touch storage, but accessed {name!r}")


def make_stats(
    claims_total: int = 10,
    unresolved_failures: int = 0,
    completed: bool = True,
) -> CategorizationStats:
    return CategorizationStats(claims_total=claims_total, unresolved_failures=unresolved_failures, completed=completed)


def eligible(cat_stats: CategorizationStats | None) -> tuple[bool, str | None]:
    return _claims_report_eligible("run_1", ExplodingStorage(), cat_stats)


# ---------------------------------------------------------------------------
# Each rejection branch, in the documented precedence order
# ---------------------------------------------------------------------------


def test_claims_report_kill_switch_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_CLAIMS_REPORT", "false")
    # A fully healthy cat_stats -- the disabled switch must win regardless of
    # how good the categorization outcome was.
    assert eligible(make_stats(claims_total=10, unresolved_failures=0, completed=True)) == (False, "claims_report_disabled")


def test_categorization_disabled_when_cat_stats_is_none() -> None:
    assert eligible(None) == (False, "categorization_disabled")


def test_categorization_incomplete() -> None:
    stats = make_stats(claims_total=10, unresolved_failures=0, completed=False)
    assert eligible(stats) == (False, "categorization_incomplete")


def test_no_claims() -> None:
    stats = make_stats(claims_total=0, unresolved_failures=0, completed=True)
    assert eligible(stats) == (False, "no_claims")


# ---------------------------------------------------------------------------
# Resolved-ratio threshold, at the exact boundary
# ---------------------------------------------------------------------------


def test_resolved_ratio_just_below_threshold_is_not_eligible() -> None:
    # 100 total, 31 unresolved -> ratio = 0.69, below the default 0.7 minimum.
    stats = make_stats(claims_total=100, unresolved_failures=31, completed=True)
    result = eligible(stats)
    assert result == (False, "low_resolved_coverage:0.69")


def test_resolved_ratio_exactly_at_threshold_is_eligible() -> None:
    # 100 total, 30 unresolved -> ratio = 0.70, exactly the default minimum --
    # the boundary is inclusive (>=), not a rejection.
    stats = make_stats(claims_total=100, unresolved_failures=30, completed=True)
    assert eligible(stats) == (True, None)


def test_resolved_ratio_just_above_threshold_is_eligible() -> None:
    stats = make_stats(claims_total=100, unresolved_failures=29, completed=True)  # ratio = 0.71
    assert eligible(stats) == (True, None)


def test_all_claims_unresolved() -> None:
    stats = make_stats(claims_total=10, unresolved_failures=10, completed=True)  # ratio = 0.0
    assert eligible(stats) == (False, "low_resolved_coverage:0.00")


def test_no_claims_unresolved() -> None:
    stats = make_stats(claims_total=10, unresolved_failures=0, completed=True)  # ratio = 1.0
    assert eligible(stats) == (True, None)


# ---------------------------------------------------------------------------
# Depends only on the CategorizationStats passed in -- never re-queries
# ---------------------------------------------------------------------------


def test_eligibility_never_touches_storage() -> None:
    # ExplodingStorage() would raise if _claims_report_eligible read anything
    # off it -- reaching a normal return proves it didn't.
    stats = make_stats(claims_total=5, unresolved_failures=0, completed=True)
    assert eligible(stats) == (True, None)


def test_resolved_ratio_helper_matches_the_documented_formula() -> None:
    stats = make_stats(claims_total=40, unresolved_failures=10)
    assert _resolved_ratio(stats) == pytest.approx(0.75)


def test_resolved_ratio_helper_is_none_for_no_claims() -> None:
    assert _resolved_ratio(make_stats(claims_total=0)) is None


def test_resolved_ratio_helper_is_none_for_no_categorization() -> None:
    assert _resolved_ratio(None) is None


# ---------------------------------------------------------------------------
# ENABLE_CLAIMS_REPORT
# ---------------------------------------------------------------------------


def test_enable_claims_based_report_defaults_to_true() -> None:
    assert enable_claims_based_report() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_enable_claims_based_report_recognizes_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ENABLE_CLAIMS_REPORT", value)
    assert enable_claims_based_report() is False


# ---------------------------------------------------------------------------
# CLAIMS_REPORT_MIN_RESOLVED_RATIO -- configuration validation
# ---------------------------------------------------------------------------


def test_min_resolved_ratio_defaults_when_unset() -> None:
    assert claims_report_min_resolved_ratio() == _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO
    assert _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO == 0.7


def test_min_resolved_ratio_accepts_a_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "0.85")
    assert claims_report_min_resolved_ratio() == 0.85


def test_min_resolved_ratio_falls_back_to_default_for_non_numeric_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "not-a-number")
    assert claims_report_min_resolved_ratio() == _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO


def test_min_resolved_ratio_falls_back_to_default_for_nan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "nan")
    assert claims_report_min_resolved_ratio() == _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO


def test_min_resolved_ratio_falls_back_to_default_for_infinity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "inf")
    assert claims_report_min_resolved_ratio() == _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO


def test_min_resolved_ratio_falls_back_to_default_for_negative_infinity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "-inf")
    assert claims_report_min_resolved_ratio() == _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO


def test_min_resolved_ratio_below_zero_is_clamped_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "-0.5")
    assert claims_report_min_resolved_ratio() == 0.0


def test_min_resolved_ratio_above_one_is_clamped_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "1.5")
    assert claims_report_min_resolved_ratio() == 1.0


def test_min_resolved_ratio_empty_string_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "")
    assert claims_report_min_resolved_ratio() == _DEFAULT_CLAIMS_REPORT_MIN_RESOLVED_RATIO


def test_a_clamped_min_ratio_is_actually_used_by_the_eligibility_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # CLAIMS_REPORT_MIN_RESOLVED_RATIO="1.5" clamps to 1.0 -- only a perfect
    # (ratio == 1.0) categorization outcome should then be eligible.
    monkeypatch.setenv("CLAIMS_REPORT_MIN_RESOLVED_RATIO", "1.5")
    almost_perfect = make_stats(claims_total=100, unresolved_failures=1, completed=True)  # ratio = 0.99
    perfect = make_stats(claims_total=100, unresolved_failures=0, completed=True)  # ratio = 1.0
    assert eligible(almost_perfect) == (False, "low_resolved_coverage:0.99")
    assert eligible(perfect) == (True, None)
