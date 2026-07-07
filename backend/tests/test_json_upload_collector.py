from __future__ import annotations

from app.collectors.json_upload import JsonUploadCollector, normalize_uploaded_item
from app.models import CollectedItem


def make_item(url: str, subreddit: str = "gadgets") -> CollectedItem:
    return CollectedItem(
        source_url=url,
        subreddit=subreddit,
        item_type="post",
        post_id=url,
        comment_id=None,
        title="title",
        body="body",
        score=1,
        comment_count=0,
        created_at="2026-01-01T00:00:00+00:00",
        search_query="uploaded_json",
    )


def test_normalize_uploaded_item_infers_post_vs_comment() -> None:
    post = normalize_uploaded_item({"source_url": "https://reddit.com/p1", "title": "t", "body": "b"}, 0)
    assert post.item_type == "post"

    comment = normalize_uploaded_item(
        {"source_url": "https://reddit.com/p1/c1", "comment_id": "c1", "body": "reply"}, 1
    )
    assert comment.item_type == "comment"


def test_normalize_uploaded_item_strips_r_prefix_and_defaults_missing_fields() -> None:
    item = normalize_uploaded_item({"subreddit": "r/Headphones", "body": "hello"}, 5)
    assert item.subreddit == "Headphones"
    assert item.source_url == "upload://item-5"
    assert item.score == 0
    assert item.comment_count == 0


def test_search_paginates_without_repeating_items() -> None:
    items = [make_item(f"https://reddit.com/{i}") for i in range(5)]
    collector = JsonUploadCollector(items)

    first_batch = collector.search("any query", limit=3)
    second_batch = collector.search("different query", limit=3)
    third_batch = collector.search("yet another query", limit=3)

    assert [item.source_url for item in first_batch] == [f"https://reddit.com/{i}" for i in range(3)]
    assert [item.source_url for item in second_batch] == [f"https://reddit.com/{i}" for i in range(3, 5)]
    assert third_batch == []  # exhausted -> empty, which the ReAct loop treats as diminishing returns


def test_search_filters_by_subreddit() -> None:
    items = [make_item("https://reddit.com/a", subreddit="dogs"), make_item("https://reddit.com/b", subreddit="cats")]
    collector = JsonUploadCollector(items)

    result = collector.search("query", subreddit="cats", limit=10)

    assert [item.source_url for item in result] == ["https://reddit.com/b"]


def test_available_reflects_whether_any_items_were_uploaded() -> None:
    assert JsonUploadCollector([]).available() is False
    assert JsonUploadCollector([make_item("https://reddit.com/a")]).available() is True
