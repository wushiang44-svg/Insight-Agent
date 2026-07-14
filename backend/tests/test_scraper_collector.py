from __future__ import annotations

from typing import Any

from app.collectors.scraper import RedditScraperCollector


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Records requested URLs and serves canned payloads keyed by URL suffix."""

    def __init__(self, responses: dict[str, Any]):
        self._responses = responses
        self.requested_urls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> FakeResponse:
        self.requested_urls.append(url)
        for suffix, payload in self._responses.items():
            if url.endswith(suffix):
                return FakeResponse(payload)
        return FakeResponse({}, status_code=404)


def make_post_listing(post_id: str = "abc123") -> dict[str, Any]:
    return {
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": post_id,
                        "permalink": f"/r/headphones/comments/{post_id}/battery_life/",
                        "subreddit": "headphones",
                        "title": "Battery life is terrible",
                        "selftext": "Dies by lunch every day.",
                        "score": 42,
                        "num_comments": 2,
                        "created_utc": 1700000000,
                    },
                }
            ]
        }
    }


def make_comments_listing(post_id: str = "abc123") -> list[dict[str, Any]]:
    return [
        {"data": {"children": []}},  # the post itself, at index 0
        {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c1",
                            "permalink": f"/r/headphones/comments/{post_id}/battery_life/c1/",
                            "subreddit": "headphones",
                            "body": "Same issue here, very annoying.",
                            "score": 10,
                            "created_utc": 1700000100,
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c2",
                            "permalink": f"/r/headphones/comments/{post_id}/battery_life/c2/",
                            "body": "[deleted]",
                            "score": 1,
                            "created_utc": 1700000200,
                        },
                    },
                ]
            }
        },
    ]


def test_search_normalizes_post_and_comments_without_network_calls() -> None:
    session = FakeSession(
        {
            "/search.json": make_post_listing(),
            "/comments/abc123.json": make_comments_listing(),
        }
    )
    collector = RedditScraperCollector(session=session, request_delay=0)  # type: ignore[arg-type]

    items = collector.search("battery life", limit=10)

    assert [item.item_type for item in items] == ["post", "comment"]
    post, comment = items
    assert post.title == "Battery life is terrible"
    assert post.subreddit == "headphones"
    assert post.source_url == "https://www.reddit.com/r/headphones/comments/abc123/battery_life/"
    assert comment.body == "Same issue here, very annoying."
    assert comment.post_id == "abc123"


def test_search_filters_out_deleted_and_removed_comments() -> None:
    session = FakeSession(
        {
            "/search.json": make_post_listing(),
            "/comments/abc123.json": make_comments_listing(),
        }
    )
    collector = RedditScraperCollector(session=session, request_delay=0)  # type: ignore[arg-type]

    items = collector.search("battery life")

    comment_bodies = [item.body for item in items if item.item_type == "comment"]
    assert "[deleted]" not in comment_bodies
    assert comment_bodies == ["Same issue here, very annoying."]


def test_search_scopes_to_subreddit_when_provided() -> None:
    session = FakeSession({"/r/headphones/search.json": make_post_listing(), "/comments/abc123.json": [{}, {"data": {"children": []}}]})
    collector = RedditScraperCollector(session=session, request_delay=0)  # type: ignore[arg-type]

    collector.search("battery life", subreddit="headphones")

    assert any("/r/headphones/search.json" in url for url in session.requested_urls)


def test_available_is_always_true_since_no_credentials_are_required() -> None:
    assert RedditScraperCollector(session=FakeSession({}), request_delay=0).available() is True  # type: ignore[arg-type]


def test_get_returns_none_on_non_200_status() -> None:
    session = FakeSession({})  # every URL falls through to the 404 default
    collector = RedditScraperCollector(session=session, request_delay=0)  # type: ignore[arg-type]

    items = collector.search("anything")

    assert items == []
