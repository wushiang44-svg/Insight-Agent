from __future__ import annotations

from typing import Any

from ..models import CollectedItem, DataSource, utc_now
from .base import CollectorContext
from .registry import register_collector


class JsonUploadCollector:
    """Serves pre-collected items from an uploaded JSON array.

    Fallback collector for development and demos when the Reddit API isn't
    available. Since there's no live search, `query` is ignored for filtering
    (relevance filtering already happens downstream in `analyze_item`); each
    call instead hands out the next unseen slice of the uploaded corpus,
    optionally narrowed by subreddit. Once the corpus is exhausted, `search`
    returns [] on every subsequent call, which the existing "diminishing
    returns" sufficiency check already treats as a stop signal.
    """

    def __init__(self, items: list[CollectedItem]):
        self._items = items
        self._returned_urls: set[str] = set()

    def available(self) -> bool:
        return bool(self._items)

    def search(self, query: str, subreddit: str = "", limit: int = 25) -> list[CollectedItem]:
        pool = self._items
        if subreddit.strip():
            target = subreddit.strip().lower()
            pool = [item for item in pool if item.subreddit.strip().lower() == target]
        remaining = [item for item in pool if item.source_url not in self._returned_urls]
        batch = remaining[:limit]
        for item in batch:
            self._returned_urls.add(item.source_url)
        return batch


def normalize_uploaded_item(raw: dict[str, Any], index: int) -> CollectedItem:
    """Normalizes one uploaded JSON object into the common CollectedItem shape.

    Accepts the same fields documented in the demo's "Ingest Custom Reddit Data"
    schema: source_url, subreddit, post_id, comment_id, title, body, score,
    comment_count, created_at, language. All fields except title/body are optional.
    """
    source_url = str(raw.get("source_url") or raw.get("url") or f"upload://item-{index}")
    comment_id = raw.get("comment_id")
    post_id = raw.get("post_id")
    subreddit = str(raw.get("subreddit") or "unknown").strip()
    if subreddit.lower().startswith("r/"):
        subreddit = subreddit[2:]
    return CollectedItem(
        source_url=source_url,
        subreddit=subreddit or "unknown",
        item_type="comment" if comment_id not in (None, "") else "post",
        post_id=str(post_id) if post_id not in (None, "") else None,
        comment_id=str(comment_id) if comment_id not in (None, "") else None,
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or raw.get("text") or ""),
        score=_int_or_zero(raw.get("score")),
        comment_count=_int_or_zero(raw.get("comment_count")),
        created_at=str(raw.get("created_at") or utc_now()),
        search_query="uploaded_json",
    )


def _int_or_zero(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _build(context: CollectorContext) -> JsonUploadCollector:
    uploaded = context.storage.get_uploaded_items(context.run.run_id)
    items = [normalize_uploaded_item(raw, index) for index, raw in enumerate(uploaded)]
    return JsonUploadCollector(items)


register_collector(DataSource.JSON_UPLOAD, _build)
