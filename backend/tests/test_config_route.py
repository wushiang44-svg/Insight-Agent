from __future__ import annotations

from pathlib import Path

import pytest

from app import routes as routes_module


def point_reddit_profile_at(monkeypatch: pytest.MonkeyPatch, profile_dir: Path) -> None:
    monkeypatch.setenv("REDDIT_CHROME_PROFILE_DIR", str(profile_dir))


# ---------------------------------------------------------------------------
# Milestone 2 / B1: GET /config surfaces profile_status(), not just the
# binary reddit_browser_configured flag -- previously a user got no warning
# that a configured profile was in a known CHALLENGED state before starting
# a run (see the run_214c214cd516 investigation this milestone follows up on).
# ---------------------------------------------------------------------------


def test_config_reports_not_initialized_for_a_fresh_profile_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    point_reddit_profile_at(monkeypatch, tmp_path / "brand_new_profile")

    config = routes_module.get_config()

    assert config["reddit_profile_status"] == "not_initialized"
    assert config["reddit_last_success_at"] is None
    assert config["reddit_consecutive_challenge_count"] == 0


def test_config_reports_unknown_for_a_preexisting_chrome_profile_without_our_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")  # real Chrome data, no our-metadata file
    point_reddit_profile_at(monkeypatch, profile_dir)

    config = routes_module.get_config()

    assert config["reddit_profile_status"] == "unknown"


def test_config_reports_healthy_after_a_recorded_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")
    (profile_dir / "reddit_collector_state.json").write_text(
        '{"last_success_at": "2026-08-01T00:00:00+00:00"}', encoding="utf-8"
    )
    point_reddit_profile_at(monkeypatch, profile_dir)

    config = routes_module.get_config()

    assert config["reddit_profile_status"] == "healthy"
    assert config["reddit_last_success_at"] == "2026-08-01T00:00:00+00:00"


def test_config_reports_challenged_and_the_consecutive_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")
    (profile_dir / "reddit_collector_state.json").write_text(
        '{"last_success_at": "2026-08-01T00:00:00+00:00", "last_challenge_at": "2026-08-06T00:00:00+00:00", '
        '"consecutive_challenge_count": 3}',
        encoding="utf-8",
    )
    point_reddit_profile_at(monkeypatch, profile_dir)

    config = routes_module.get_config()

    assert config["reddit_profile_status"] == "challenged"
    assert config["reddit_consecutive_challenge_count"] == 3
    assert config["reddit_last_challenge_at"] == "2026-08-06T00:00:00+00:00"


def test_config_still_returns_the_other_source_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The new Reddit profile fields must be additive -- every existing
    /config key stays present and unaffected."""
    point_reddit_profile_at(monkeypatch, tmp_path / "profile")

    config = routes_module.get_config()

    assert "reddit_configured" in config
    assert "reddit_browser_configured" in config
    assert "amazon_configured" in config
    assert "youtube_configured" in config
    assert "deepseek_configured" in config
