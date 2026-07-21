from __future__ import annotations

from typing import Any

from app.collectors.reddit_browser import (
    RedditBrowserCollector,
    _comment_to_item,
    _normalize_subreddit,
    _parse_count,
    _post_to_item,
    classify_page,
    dedupe_comments,
    is_challenge,
    rank_and_select,
)

# --- Real fixture, captured verbatim from this session's live POC (a search-post-unit
# card's data-faceplate-tracking-context JSON + the surrounding card text) ---
REAL_SEARCH_CARD = {
    "title": "Curious about mechanical keyboards at work",
    "url": "https://www.reddit.com/r/MechanicalKeyboards/comments/1uweicu/curious_about_mechanical_keyboards_at_work/",
    "subreddit": "MechanicalKeyboards",
    "postId": "t3_1uweicu",
    "nsfw": False,
    "voteText": "119",
    "commentText": "268",
    "promoted": False,
}


def test_challenge_detected_from_js_challenge_url() -> None:
    assert is_challenge("https://www.reddit.com/?solution=x&js_challenge=1&token=y", "some body") is True


def test_challenge_detected_from_block_page_text() -> None:
    body = "You've been blocked by network security.\nIf you think you've been blocked by mistake..."
    assert is_challenge("https://www.reddit.com/", body) is True


def test_challenge_detected_from_captcha_text() -> None:
    assert is_challenge("https://www.reddit.com/", "Please verify you are human to continue") is True


def test_normal_page_is_not_a_challenge() -> None:
    body = "Curious about mechanical keyboards at work\nr/MechanicalKeyboards\n7d ago\n119 votes"
    assert is_challenge("https://www.reddit.com/r/MechanicalKeyboards/comments/1uweicu/", body) is False


def test_classify_page_defense_in_depth_catches_empty_title_even_without_known_phrases() -> None:
    """Durable against Reddit changing its block page's wording -- empty title
    + short body was true on every observed block page this session, even
    when none of the known phrases matched."""
    assert classify_page("https://www.reddit.com/", "", "Some new block message we've never seen before") is True


def test_classify_page_accepts_real_page_with_real_title() -> None:
    body = "mechanical keyboard - Reddit Search!\n" + ("real search results content " * 20)
    assert classify_page("https://www.reddit.com/search/?q=mechanical+keyboard", "mechanical keyboard - Reddit Search!", body) is False


def test_parse_count_handles_plain_numbers_and_commas() -> None:
    assert _parse_count("268") == 268
    assert _parse_count("1,234") == 1234


def test_parse_count_handles_k_and_m_suffixes() -> None:
    assert _parse_count("1.2k") == 1200
    assert _parse_count("3m") == 3_000_000


def test_normalize_subreddit_strips_dom_prefix() -> None:
    """Regression test for a real bug caught by the live smoke test's own
    printed output ("r/r/keyboards") -- Reddit's DOM attribute is prefixed,
    the rest of the codebase's convention (PRAW, scraper, frontend) is not."""
    assert _normalize_subreddit("r/MechanicalKeyboards") == "MechanicalKeyboards"


def test_normalize_subreddit_leaves_unprefixed_value_alone() -> None:
    assert _normalize_subreddit("MechanicalKeyboards") == "MechanicalKeyboards"


def test_parse_count_handles_missing_value() -> None:
    assert _parse_count(None) == 0
    assert _parse_count("") == 0


def test_rank_and_select_excludes_promoted_and_nsfw() -> None:
    results = [
        {**REAL_SEARCH_CARD, "url": "https://x/1", "promoted": True},
        {**REAL_SEARCH_CARD, "url": "https://x/2", "nsfw": True, "promoted": False},
        {**REAL_SEARCH_CARD, "url": "https://x/3", "promoted": False, "nsfw": False},
    ]
    selected = rank_and_select("mechanical keyboard", results, max_posts=5, min_comment_count=0)
    urls = [r["url"] for r in selected]
    assert urls == ["https://x/3"]


def test_rank_and_select_excludes_below_min_comment_count() -> None:
    results = [
        {**REAL_SEARCH_CARD, "url": "https://x/low", "commentText": "2"},
        {**REAL_SEARCH_CARD, "url": "https://x/high", "commentText": "50"},
    ]
    selected = rank_and_select("mechanical keyboard", results, max_posts=5, min_comment_count=5)
    urls = [r["url"] for r in selected]
    assert urls == ["https://x/high"]


def test_rank_and_select_dedupes_by_url() -> None:
    results = [
        {**REAL_SEARCH_CARD, "url": "https://x/dupe"},
        {**REAL_SEARCH_CARD, "url": "https://x/dupe"},
    ]
    selected = rank_and_select("mechanical keyboard", results, max_posts=5, min_comment_count=0)
    assert len(selected) == 1


def test_rank_and_select_query_relevance_beats_pure_comment_count() -> None:
    """The exact scenario from plan review: a high-comment, off-topic result
    must not outrank a low-comment, on-topic one -- proves relevance sorts
    before discussion-depth, not the other way around."""
    off_topic_high_engagement = {
        "title": "Show us your MacBook setup!",
        "url": "https://x/off-topic",
        "subreddit": "MacBook",
        "postId": "t3_a",
        "nsfw": False,
        "voteText": "900",
        "commentText": "500",
        "promoted": False,
    }
    on_topic_low_engagement = {
        "title": "M4 MacBook Pro battery life after six months",
        "url": "https://x/on-topic",
        "subreddit": "MacBook",
        "postId": "t3_b",
        "nsfw": False,
        "voteText": "40",
        "commentText": "80",
        "promoted": False,
    }
    selected = rank_and_select(
        "MacBook battery life", [off_topic_high_engagement, on_topic_low_engagement], max_posts=2, min_comment_count=0
    )
    assert [r["url"] for r in selected][0] == "https://x/on-topic"


def test_rank_and_select_uses_comment_count_as_tiebreaker_among_equally_relevant_results() -> None:
    a = {**REAL_SEARCH_CARD, "url": "https://x/a", "title": "Best mechanical keyboard", "commentText": "10"}
    b = {**REAL_SEARCH_CARD, "url": "https://x/b", "title": "Best mechanical keyboard", "commentText": "90"}
    selected = rank_and_select("best mechanical keyboard", [a, b], max_posts=2, min_comment_count=0)
    # identical titles -> tied relevance score -- higher comment count must win the tiebreak
    assert selected[0]["url"] == "https://x/b"


def test_rank_and_select_respects_max_posts() -> None:
    results = [{**REAL_SEARCH_CARD, "url": f"https://x/{i}"} for i in range(10)]
    selected = rank_and_select("mechanical keyboard", results, max_posts=3, min_comment_count=0)
    assert len(selected) == 3


# --- Real fixture, captured verbatim from a shreddit-comment element in the live POC ---
REAL_COMMENT = {
    "thingid": "t1_o3343zn",
    "permalink": "/r/MechKeyboards/comments/1qtho3t/comment/o3343zn/",
    "score": "4",
    "created": "2026-02-02T02:30:43.294000+0000",
    "body": "I'm a beginner too and this might be worth checking out.",
}


def test_dedupe_comments_removes_true_duplicates() -> None:
    """The POC never actually observed a duplicate (0% rate across 118
    comments) -- this fixture deliberately forces the path so the dedup
    logic is genuinely exercised, not just assumed safe."""
    raw = [REAL_COMMENT, dict(REAL_COMMENT), {**REAL_COMMENT, "thingid": "t1_different"}]
    unique = dedupe_comments(raw)
    assert len(unique) == 2


def test_dedupe_comments_falls_back_to_body_when_thingid_missing() -> None:
    raw = [{"thingid": None, "body": "same text"}, {"thingid": None, "body": "same text"}]
    assert len(dedupe_comments(raw)) == 1


def test_comment_to_item_maps_all_collected_item_fields() -> None:
    item = _comment_to_item(REAL_COMMENT, subreddit="MechKeyboards", post_id="t3_1qtho3t", post_title="New to mech keyboards", query="mechanical keyboard")

    assert item.source_url == "https://www.reddit.com/r/MechKeyboards/comments/1qtho3t/comment/o3343zn/"
    assert item.subreddit == "MechKeyboards"
    assert item.item_type == "comment"
    assert item.post_id == "t3_1qtho3t"
    assert item.comment_id == "t1_o3343zn"
    assert item.title == "New to mech keyboards"
    assert item.body == "I'm a beginner too and this might be worth checking out."
    assert item.score == 4
    assert item.comment_count == 0
    assert item.created_at == "2026-02-02T02:30:43.294000+0000"
    assert item.search_query == "mechanical keyboard"


def test_post_to_item_maps_all_collected_item_fields() -> None:
    post_meta = {"title": "Curious about mechanical keyboards at work", "bodyText": "Do folks support noisy keyboards at work?", "score": "119", "commentCountAttr": "268"}
    item = _post_to_item(post_meta, post_url="https://www.reddit.com/r/MechanicalKeyboards/comments/1uweicu/x/", post_id="t3_1uweicu", subreddit="MechanicalKeyboards", query="mechanical keyboard")

    assert item.source_url == "https://www.reddit.com/r/MechanicalKeyboards/comments/1uweicu/x/"
    assert item.item_type == "post"
    assert item.comment_id is None
    assert item.post_id == "t3_1uweicu"
    assert item.title == "Curious about mechanical keyboards at work"
    assert item.body == "Do folks support noisy keyboards at work?"
    assert item.score == 119
    assert item.comment_count == 268
    assert item.search_query == "mechanical keyboard"


# --- Synthetic fixtures: neither POC's actual result set ever contained a
# promoted post, so this exercises a code path real data never triggered ---
SYNTHETIC_PROMOTED_CARD = {
    "title": "Try the new XYZ Keyboard - Sponsored",
    "url": "https://x/promoted",
    "subreddit": "ads",
    "postId": "t3_ad1",
    "nsfw": False,
    "voteText": None,
    "commentText": None,
    "promoted": True,
}


def test_synthetic_promoted_post_excluded() -> None:
    selected = rank_and_select("keyboard", [SYNTHETIC_PROMOTED_CARD, REAL_SEARCH_CARD], max_posts=5, min_comment_count=0)
    urls = [r["url"] for r in selected]
    assert SYNTHETIC_PROMOTED_CARD["url"] not in urls


# --- Challenge-return contract: guards against the return-vs-raise
# inconsistency caught during plan review (see docs/reddit_browser_collector_plan.md §8) ---
class FakeCdpSession:
    """Scripted sequence of eval_json/open/click responses, in call order, so
    the collector's search() flow can be driven without any real Chrome/CDP."""

    def __init__(self, script: list[Any]):
        self._script = list(script)
        self.calls: list[str] = []

    def open(self, url: str) -> None:
        self.calls.append(f"open:{url}")

    def wait_networkidle(self, timeout: float = 15.0) -> None:
        self.calls.append("wait")

    def eval_json(self, js: str) -> Any:
        self.calls.append("eval")
        return self._script.pop(0) if self._script else None

    def click(self, selector: str) -> str:
        self.calls.append(f"click:{selector}")
        return ""


def test_search_returns_partial_results_on_mid_run_challenge_without_raising(monkeypatch: Any, tmp_path: Any) -> None:
    """The exact contract fixed during plan review: a challenge mid-run must
    be a normal return of whatever was already collected, never an exception
    that would let react_agent.py's except-branch silently discard it."""
    collector = RedditBrowserCollector(profile_dir=tmp_path / "profile", cdp_port=9222)
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "Local State").write_text("{}", encoding="utf-8")
    (tmp_path / "profile" / "reddit_collector_state.json").write_text(
        '{"initialized_at": "2026-01-01T00:00:00+00:00", "last_success_at": "2026-01-01T00:00:00+00:00"}',
        encoding="utf-8",
    )

    session = FakeCdpSession(
        script=[
            # search page: url, title, body -- normal
            "https://www.reddit.com/search/?q=x",
            "x - Reddit Search!",
            "some real search page body " * 20,
            # search results extraction
            [dict(REAL_SEARCH_CARD)],
            # post navigation: url, title, body -- CHALLENGE this time
            "https://www.reddit.com/?js_challenge=1&token=y",
            "",
            "You've been blocked by network security.",
        ]
    )
    # Patch the names as imported into reddit_browser's own namespace
    # (`from ._reddit_chrome import ensure_running`) -- patching
    # _reddit_chrome.ensure_running itself would NOT intercept this call,
    # since reddit_browser already holds its own separate reference to the
    # original function. Missing this distinction is exactly what let this
    # test accidentally launch two real stray Chrome processes on its first
    # run (caught and cleaned up during implementation).
    monkeypatch.setattr("app.collectors.reddit_browser.ensure_running", lambda *a, **k: None)
    monkeypatch.setattr("app.collectors.reddit_browser.CdpSession", lambda *a, **k: session)

    items = collector.search("x")

    assert items == []  # nothing was ever successfully collected before the challenge in this script
    assert collector.last_search_stats["challenge_detected"] is True
    assert collector._reddit_disabled_for_run is True

    # Every subsequent call in the same run must be a cheap no-op -- zero further Chrome/CDP interaction.
    calls_before = len(session.calls)
    more_items = collector.search("y")
    assert more_items == []
    assert len(session.calls) == calls_before  # no new eval/open/click calls were made
